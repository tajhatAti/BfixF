"""Persist technical errors; privately alert only the first/owner admin.

Alerts of the same kind (file/store numbers ignored) are coalesced for 30 minutes
and report how many were held back; every occurrence stays in the DB.
Notification failures never feed back into logging. No raw message/session input
is included in handler context. /errors is owner-only.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time

from app import config as cfg
from app.storage import db

_sending = contextvars.ContextVar('error_alert_sending', default=False)
#: alert key -> [time last sent, how many identical ones were held back since]
_seen: dict[str, list] = {}
ALERT_WINDOW = 30 * 60

_ENTITY_RAW = re.compile(r'Could not find the input entity for .*?(?:entities\.html[^\n]*|$)', re.I)
_ENTITY_TEXT = ('এই সেশনের অ্যাকাউন্ট চ্যানেলটি চেনে না — অ্যাকাউন্টটি চ্যানেলের সদস্য কিনা '
                '(আর্কাইভ ফোল্ডারসহ) দেখুন')
_INVALID_TEXT = 'চ্যানেলের সংরক্ষিত অ্যাক্সেস কোড পুরনো — বট নিজে আবার শিখে নেবে'
_INTERNAL_TEXT = 'Telegram-এ সাময়িক সমস্যা চলছে, কিছুক্ষণ পরে আবার চেষ্টা হবে'


def friendly_error(value) -> str:
    """Plain Bengali for a raw Telegram error; unknown errors come back unchanged."""
    text = str(value)
    low = text.lower()
    if 'input entity' in low or 'cannot find any entity' in low:
        return _ENTITY_TEXT
    if 'invalid channel' in low or 'channel invalid' in low or 'channel_invalid' in low:
        return _INVALID_TEXT
    if 'internal issues' in low or 'rpccallfail' in low:
        return _INTERNAL_TEXT
    return text


def _friendly_alert(text: str) -> str:
    """Swap the raw Telethon sentences inside an alert for plain Bengali."""
    text = _ENTITY_RAW.sub(_ENTITY_TEXT, text)
    text = re.sub(r'Invalid channel object[^\n]*', _INVALID_TEXT, text, flags=re.I)
    text = re.sub(r'Telegram is having internal issues[^\n]*?(?=\(caused|\n|$)', _INTERNAL_TEXT + ' ',
                  text, flags=re.I)
    return text


#: Known harmless channel/copy noise: still saved in the DB (/errors), never sent to Telegram.
_QUIET = ('mirror of file', 'video archive store=', 'source read failed', 'storage channel unreachable',
          'input entity', 'invalid channel', 'channel invalid', 'peer id invalid',
          'internal issues', 'rpccallfail')


def _alert_key(text: str) -> str:
    """Same kind of error = same key, whatever the file/store/channel numbers are."""
    return re.sub(r'\d+', '#', text)


def redact(value: str) -> str:
    text = str(value)
    for name in ('BOT_TOKEN', 'API_HASH', 'STRING_SESSION'):
        secret = str(getattr(cfg, name, '') or '')
        if secret:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(://[^/\s:@]+:)[^@\s/]+(@)', r'\1[REDACTED]\2', text)
    text = re.sub(r'\b\d{6,12}:[A-Za-z0-9_-]{25,}', '[BOT_TOKEN]', text)
    text = re.sub(r'(?<!\w)1[A-Za-z0-9+/=]{200,}', '[SESSION]', text)
    text = re.sub(r'(?i)((?:password|api_hash|session_str|token)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', text)
    return text


class OwnerErrors(logging.Handler):
    def emit(self, record):
        if _sending.get() or getattr(record, '_bfix_owner_reported', False):
            return
        try:
            record._bfix_owner_reported = True
            text = redact(self.format(record))[:3500]
            # Don't accidentally create a second/default DB during early startup.
            if getattr(db, '_impl', None) is not None:
                db.log_event('admin_error', None, None, None, record.name, text)
            if any(mark in text.lower() for mark in _QUIET):
                return
            alert = _friendly_alert(text)
            key = _alert_key(alert)
            now = time.time()
            entry = _seen.get(key)
            if entry and now - entry[0] < ALERT_WINDOW:
                entry[1] += 1
                return
            if len(_seen) > 500:
                _seen.clear()
            held = entry[1] if entry else 0
            _seen[key] = [now, 0]
            if held:
                alert += f'\n(একই ধরনের আরও {held}টি সতর্কতা চেপে রাখা হয়েছিল)'
            text = alert
            loop = asyncio.get_running_loop()
            token = _sending.set(True)
            try:
                loop.create_task(deliver(text))
            finally:
                _sending.reset(token)
        except Exception:
            pass


async def deliver(text):
    from app import logger
    try:
        await logger.notify_admins(text)
    except Exception:
        pass


def install():
    for name in ('', 'storebot', 'telethon', 'asyncio', 'uvicorn.error'):
        logger = logging.getLogger(name)
        if not any(isinstance(h, OwnerErrors) for h in logger.handlers):
            handler = OwnerErrors(level=logging.WARNING)
            handler.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))
            logger.addHandler(handler)


def recent(limit=10):
    return db._q("SELECT ts,ref,detail FROM events WHERE name='admin_error' ORDER BY id DESC LIMIT ?", (limit,))
