"""Run **several bots** on one database — the “একটার সমস্যা হলে অন্যটা চলবে” system.

Why it works this way
    A Telegram bot token can only be polled by one process at a time, so a second
    bot cannot simply be a second client inside this process (Telegram would answer
    “Conflict: terminated by other getUpdates request”). Each extra bot therefore
    runs in its **own small process** (`bot.py`) that talks to the *same* SQLite
    database and the *same* userbot sessions.

    That is exactly the owner's request: several bots, one database. A user who
    presses `/start` on bot #2 lands in the same stores, orders and broadcasts as
    bot #1 — and if bot #1 is blocked, banned or rate-limited, the others keep
    serving.

How the owner adds one (no API id/hash typing)
    1. `/newbot` in @BotFather → BotFather answers with a token,
    2. forward that message to any of the running bots,
    3. the bot recognises BotFather, takes the token, and starts the new bot
       with the API ID/hash **already saved in this install**.
    The same can be done from the panel (⚙ বটগুলো) by pasting the token.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from app import config as cfg
from app.logger import log
from app.storage import db

#: BotFather's own user id — the only account whose messages are trusted for tokens.
BOTFATHER_ID = 93372553
TOKEN_RE = re.compile(r"(\d{6,12}:[A-Za-z0-9_-]{30,})")
#: “Done! Congratulations on your new bot…” — BotFather's token message.
TOKEN_HINTS = ("use this token to access the http api",
               "revoke the current token and generate a new one",
               "congratulations on your new bot")

STATE_DIR = ".run"


def _state_dir() -> Path:
    path = Path(cfg.BASE_DIR) / STATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def pid_file(bot_id: int) -> Path:
    return _state_dir() / f"bot-{int(bot_id)}.pid"


def log_file(bot_id: int) -> Path:
    return _state_dir() / f"bot-{int(bot_id)}.log"


def session_path(bot_id: int) -> str:
    """Each bot gets its own session state file — its token is a different account."""
    return str(_state_dir() / f"bot-{int(bot_id)}.session")


def token_of(message) -> str:
    """Pull a bot token out of a forwarded BotFather message ('' when there is none)."""
    text = (getattr(message, "raw_text", None) or getattr(message, "text", None) or "")
    lowered = text.lower()
    if not any(hint in lowered for hint in TOKEN_HINTS):
        return ""
    found = TOKEN_RE.search(text)
    return found.group(1) if found else ""


def is_from_botfather(message) -> bool:
    sender = getattr(message, "from_id", None)
    sender_id = getattr(sender, "user_id", None) or getattr(message, "sender_id", None)
    if sender_id and int(sender_id) == BOTFATHER_ID:
        return True
    forward = getattr(message, "fwd_from", None)
    from_id = getattr(forward, "from_id", None) if forward else None
    peer = getattr(from_id, "user_id", None)
    return bool(peer and int(peer) == BOTFATHER_ID)


# ------------------------------------------------------------------- processes
def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid(bot_id: int) -> int:
    try:
        return int(pid_file(bot_id).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def is_running(bot_row: dict) -> bool:
    return _alive(read_pid(bot_row.get("id") or 0))


def _python() -> str:
    """The interpreter to launch the extra bot with (same venv as this process)."""
    candidate = Path(cfg.BASE_DIR) / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else (sys.executable or "python3")


def start(bot_row: dict, *, wait: float = 1.5) -> dict:
    """Launch one extra bot in its own process. Safe to call twice."""
    bot_id = int(bot_row.get("id") or 0)
    if not bot_id:
        return {"ok": False, "error": "বট আইডি নেই"}
    if is_running(bot_row):
        db.bots_store.update_bot(bot_id, enabled=1)
        return {"ok": True, "already": True, "pid": read_pid(bot_id)}
    token = (bot_row.get("token") or "").strip()
    if not TOKEN_RE.fullmatch(token or ""):
        return {"ok": False, "error": "টোকেনটা ঠিক নয়"}
    env = dict(os.environ)
    env.update({
        "BOT_TOKEN": token,
        "API_ID": str(bot_row.get("api_id") or cfg.API_ID or ""),
        "API_HASH": (bot_row.get("api_hash") or cfg.API_HASH or ""),
        "ADMIN_IDS": ",".join(str(x) for x in (db_bot_admins(bot_row) or [])),
        "DB_FILE": str(cfg.DB_FILE),
        "SESSION_STATE_FILE": session_path(bot_id),
        "BOT_LABEL": str(bot_row.get("label") or f"bot{bot_id}"),
        "SKIP_WEBSITE": "1",          # the main process already serves the website
        "BOT_EXTRA": "1",
    })
    handle = open(log_file(bot_id), "ab", buffering=0)
    try:
        process = subprocess.Popen(
            [_python(), str(Path(cfg.BASE_DIR) / "bot.py")],
            cwd=str(cfg.BASE_DIR), env=env, stdout=handle, stderr=handle,
            start_new_session=True)
    except Exception as exc:
        handle.close()
        log.error("extra bot %s could not start: %s", bot_id, exc)
        db.bots_store.update_bot(bot_id, last_error=str(exc)[:200])
        return {"ok": False, "error": str(exc)}
    handle.close()
    _children[process.pid] = process
    db.bots_store.update_bot(bot_id, enabled=1)
    pid_file(bot_id).write_text(str(process.pid))
    if wait:
        time.sleep(min(3.0, wait))
        if process.poll() is not None:
            db.bots_store.update_bot(bot_id, last_error=tail(bot_id, 3)[-200:] or "প্রসেস বন্ধ হয়ে গেছে")
            return {"ok": False, "error": "বট চালু হয়ে সাথে সাথে বন্ধ হয়ে গেল",
                    "log": tail(bot_id)}
    db.bots_store.update_bot(bot_id, last_start=time.time(), last_error="")
    log.info("extra bot %s started (pid %s, label %s)", bot_id, process.pid,
             bot_row.get("label") or "")
    return {"ok": True, "pid": process.pid}


def db_bot_admins(bot_row: dict) -> list[int]:
    """Who may use this bot: its own admin, then the install's admins."""
    out: list[int] = []
    main = int(bot_row.get("admin_id") or 0)
    if main:
        out.append(main)
    from app.services import credentials
    for user_id in credentials.admins():
        if user_id not in out:
            out.append(user_id)
    return out


def stop(bot_row: dict) -> dict:
    """Stop an extra bot (SIGTERM, then SIGKILL if it does not leave)."""
    bot_id = int(bot_row.get("id") or 0)
    db.bots_store.update_bot(bot_id, enabled=0)
    pid = read_pid(bot_id)
    if not pid or not _alive(pid):
        pid_file(bot_id).unlink(missing_ok=True)
        return {"ok": True, "already": True}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            break
        for _ in range(12):
            if not _alive(pid):
                break
            time.sleep(0.25)
        if not _alive(pid):
            break
    pid_file(bot_id).unlink(missing_ok=True)
    log.info("extra bot %s stopped (pid %s)", bot_id, pid)
    return {"ok": True, "pid": pid}


def restart(bot_row: dict) -> dict:
    stop(bot_row)
    return start(bot_row)


def tail(bot_id: int, lines: int = 40) -> str:
    """The last lines of this bot's own log — the panel shows them."""
    try:
        data = log_file(bot_id).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-max(1, int(lines)):])


def status() -> list[dict]:
    """Every bot: the main one plus each extra, with a live running flag."""
    rows = []
    main_token = (cfg.BOT_TOKEN or "").strip()
    if main_token:
        rows.append({"id": 0, "label": "মূল বট", "token": main_token, "main": True,
                     "username": "", "admin_id": (cfg.ADMIN_IDS or [0])[0],
                     "enabled": 1, "running": True, "pid": os.getpid(),
                     "last_start": 0, "last_error": "", "note": "run.py চালাচ্ছে"})
    for row in db.bots_store.bots():
        item = dict(row)
        item["main"] = False
        item["running"] = is_running(item)
        item["pid"] = read_pid(item["id"])
        item["masked"] = mask_token(item.get("token") or "")
        item["own"] = (item.get("token") or "").strip() == main_token
        rows.append(item)
    return rows


def mask_token(token: str) -> str:
    text = (token or "").strip()
    if ":" not in text:
        return "—"
    head, _, tail_part = text.partition(":")
    return f"{head}:••••••{tail_part[-4:]}"


def autostart() -> dict:
    """Called at startup: bring every enabled extra bot up (a host reboot)."""
    started, failed = [], []
    for row in db.bots_store.bots():
        if not int(row.get("enabled") or 0):
            continue
        if (row.get("token") or "").strip() == (cfg.BOT_TOKEN or "").strip():
            continue                      # that is this process — never launch twice
        if is_running(row):
            continue                      # already up since the last start
        report = start(row, wait=0.2)
        (started if report.get("ok") else failed).append(row.get("id"))
    if started or failed:
        log.info("extra bots autostart: %s started, %s failed", started, failed)
    return {"started": started, "failed": failed}


def add(token: str, *, label: str = "", admin_id: int = 0, api_id: int = 0,
        api_hash: str = "", note: str = "", enabled: int = 1,
        origin: str = "panel") -> dict:
    """Validate and store a new bot (the panel and the BotFather forward both use it)."""
    token = (token or "").strip()
    if not TOKEN_RE.fullmatch(token):
        return {"ok": False, "error": "টোকেনটা ঠিক মনে হচ্ছে না — BotFather-এর পুরো লাইনটা দিন"}
    for row in db.bots_store.bots():
        if (row.get("token") or "").strip() == token:
            return {"ok": False, "error": "এই বট আগেই যোগ করা আছে", "id": row["id"]}
    if token == (cfg.BOT_TOKEN or "").strip():
        return {"ok": False, "error": "এটা তো মূল বটেরই টোকেন"}
    from app.services import credentials
    bot_id = db.bots_store.add_bot(token, label=label or f"বট {int(time.time()) % 1000}",
                        admin_id=admin_id or (credentials.admins() or [0])[0],
                        api_id=api_id or cfg.API_ID, api_hash=api_hash or cfg.API_HASH,
                        note=note, enabled=enabled, origin=origin)
    return {"ok": True, "id": bot_id}


def learn_username(bot_row: dict, username: str) -> None:
    if username:
        db.bots_store.update_bot(int(bot_row.get("id") or 0), username=username)

# Only run this supervisor in run.py, not in the child bots. Retry state is
# namespaced by database so restores/tests do not inherit another install's state.
_recovery: dict[tuple[str, int], tuple[int, float]] = {}
_children: dict[int, subprocess.Popen] = {}


def recover_once(now: float | None = None) -> dict:
    """Recover enabled crashed bots, with 60s–15min exponential backoff."""
    now = time.time() if now is None else now
    for pid, process in list(_children.items()):
        if process.poll() is not None:
            _children.pop(pid, None)  # reap zombies before checking pid files
    recovered, failed = [], []
    for row in db.bots_store.bots():
        key = (str(db.path), int(row['id']))
        if not row.get('enabled') or (row.get('token') or '') == cfg.BOT_TOKEN:
            _recovery.pop(key, None)
            continue
        if is_running(row):
            if now - float(row.get('last_start') or 0) > 300:
                _recovery.pop(key, None)
            continue
        attempts, next_at = _recovery.get(key, (0, 0))
        if now < next_at:
            continue
        result = start(row, wait=0.2)
        delay = min(900, 60 * 2 ** min(attempts, 4))
        _recovery[key] = (attempts + 1, now + delay)
        (recovered if result.get('ok') else failed).append(row['id'])
    return {'recovered': recovered, 'failed': failed}


async def supervise() -> None:
    """Independent of main bot connectivity; never launch a stopped bot."""
    import asyncio
    while True:
        try:
            report = recover_once()
            if report['recovered'] or report['failed']:
                log.warning('Bot recovery: %s', report)
        except Exception:
            log.exception('Bot recovery check failed')
        await asyncio.sleep(30)
