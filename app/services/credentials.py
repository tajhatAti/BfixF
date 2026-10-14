"""Bot credentials that live on the website — no environment variables, no code.

The owner opens the website once, pastes

    * the bot token            (from @BotFather)
    * API ID + API hash        (from https://my.telegram.org → API development tools)
    * their own Telegram ID    (the first admin)

and presses **verify**. The values are stored in the database (`meta` table), the
bot starts by itself a second later and the site shows its live status.

Precedence: database first, then an old `config.env` / environment value (so an
existing install keeps working), otherwise "not configured yet" and the website
shows the setup page instead of the dashboard.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request

from app import config as cfg
from app.logger import log
from app.storage import db

K_TOKEN, K_API_ID, K_API_HASH, K_ADMINS = (
    "cred:BOT_TOKEN", "cred:API_ID", "cred:API_HASH", "cred:ADMIN_IDS")

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# Set the moment usable credentials exist; `run.bot_worker` waits on it.
ready: asyncio.Event | None = None
# Live state the setup page polls: waiting | connecting | online | error
state: dict = {"state": "waiting", "detail": "", "username": ""}
_BOOT_TOKEN: str = ""        # token the running client was started with


def _event() -> asyncio.Event:
    global ready
    if ready is None:
        ready = asyncio.Event()
    return ready


def set_state(name: str, detail: str = "", username: str | None = None) -> None:
    state["state"] = name
    state["detail"] = detail
    if username is not None:
        state["username"] = username


def parse_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in re.split(r"[,\s;]+", raw or ""):
        if chunk.strip().lstrip("-").isdigit():
            value = int(chunk)
            if value > 0 and value not in ids:
                ids.append(value)
    return ids


def load() -> bool:
    """Copy the saved values into `cfg`. Returns True when everything is present."""
    token = db.get_meta(K_TOKEN, "") or cfg.BOT_TOKEN
    api_hash = db.get_meta(K_API_HASH, "") or cfg.API_HASH
    try:
        api_id = int(db.get_meta(K_API_ID, "") or cfg.API_ID or 0)
    except ValueError:
        api_id = 0
    admins = parse_admin_ids(db.get_meta(K_ADMINS, "")) or list(cfg.ADMIN_IDS)
    cfg.BOT_TOKEN, cfg.API_HASH, cfg.API_ID = token, api_hash, api_id
    cfg.ADMIN_IDS[:] = admins            # in place: modules hold a reference to this list
    ok = is_configured()
    if ok:
        _event().set()
    return ok


def is_configured() -> bool:
    return bool(cfg.BOT_TOKEN and cfg.API_HASH and cfg.API_ID and cfg.ADMIN_IDS
                and TOKEN_RE.match(cfg.BOT_TOKEN))


def masked_token() -> str:
    token = cfg.BOT_TOKEN or ""
    return f"{token.split(':')[0]}:••••••••{token[-4:]}" if ":" in token else ""


def _get_me(token: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "network": True, "description": str(exc)}


async def verify_and_save(token: str, api_id: str, api_hash: str, admin_ids: str) -> dict:
    """Check every field, ask Telegram who the bot is, then store everything."""
    token, api_hash = (token or "").strip(), (api_hash or "").strip()
    api_id = (api_id or "").strip()
    admins = parse_admin_ids(admin_ids)
    # A blank field means "keep what is already saved".
    if not token and cfg.BOT_TOKEN:
        token = cfg.BOT_TOKEN
    if not api_id and cfg.API_ID:
        api_id = str(cfg.API_ID)
    if not api_hash and cfg.API_HASH:
        api_hash = cfg.API_HASH
    if not admins and cfg.ADMIN_IDS:
        admins = list(cfg.ADMIN_IDS)
    if not TOKEN_RE.match(token):
        return {"ok": False, "field": "token",
                "error": "বট টোকেনটা ঠিক মনে হচ্ছে না। @BotFather থেকে পুরোটা কপি করুন "
                         "(যেমন 1234567890:AAH…)।"}
    if not api_id.isdigit():
        return {"ok": False, "field": "api_id", "error": "API ID শুধু সংখ্যা হবে।"}
    if not HASH_RE.match(api_hash):
        return {"ok": False, "field": "api_hash",
                "error": "API Hash হলো ৩২ অক্ষরের (০-৯, a-f) কোড — my.telegram.org থেকে কপি করুন।"}
    if not admins:
        return {"ok": False, "field": "admins",
                "error": "আপনার টেলিগ্রাম আইডি (শুধু সংখ্যা) দিন। @userinfobot-কে /start দিলে পাবেন।"}

    me = await asyncio.to_thread(_get_me, token)
    if not me.get("ok"):
        if me.get("network"):
            return {"ok": False, "field": "token",
                    "error": "টেলিগ্রামে পৌঁছানো যাচ্ছে না — সার্ভারের ইন্টারনেট দেখুন। "
                             f"({me.get('description', '')[:80]})"}
        return {"ok": False, "field": "token",
                "error": "টেলিগ্রাম এই টোকেনটা নেয়নি — ভুল বা বাতিল টোকেন। "
                         "@BotFather → /mybots → API Token থেকে আবার কপি করুন।"}
    bot = me.get("result") or {}

    changed = (token != cfg.BOT_TOKEN or int(api_id) != cfg.API_ID or api_hash != cfg.API_HASH)
    db.set_meta(K_TOKEN, token)
    db.set_meta(K_API_ID, api_id)
    db.set_meta(K_API_HASH, api_hash)
    db.set_meta(K_ADMINS, ",".join(str(i) for i in admins))
    was_running = bool(_BOOT_TOKEN)
    load()
    set_state("connecting", "বট চালু হচ্ছে…", bot.get("username") or "")
    log.info("Bot credentials saved from the website (bot @%s)", bot.get("username"))
    if was_running and changed:
        schedule_restart()
    return {"ok": True, "username": bot.get("username") or "", "name": bot.get("first_name") or "",
            "restarting": was_running and changed}


def mark_started(token: str) -> None:
    global _BOOT_TOKEN
    _BOOT_TOKEN = token


def schedule_restart(delay: float = 1.5) -> None:
    """A different bot / API key needs a fresh login: forget the old session and restart."""
    async def _go() -> None:
        await asyncio.sleep(delay)
        for suffix in ("", "-journal"):
            try:
                os.remove(cfg.SESSION_STATE_FILE + suffix)
            except OSError:
                pass
        log.info("Restarting to apply the new bot credentials…")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    try:
        asyncio.get_running_loop().create_task(_go())
    except RuntimeError:
        pass
