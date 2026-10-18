"""Persist technical errors; privately alert only the first/owner admin.

Identical alerts are coalesced for a minute, but occurrences stay in the DB.
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
_seen: dict[str, float] = {}


def redact(value: str) -> str:
    text = str(value)
    for name in ('BOT_TOKEN', 'API_HASH', 'STRING_SESSION'):
        secret = str(getattr(cfg, name, '') or '')
        if secret:
            text = text.replace(secret, '[REDACTED]')
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
            now = time.time()
            if now - _seen.get(text, 0) < 60:
                return
            if len(_seen) > 500:
                _seen.clear()
            _seen[text] = now
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
