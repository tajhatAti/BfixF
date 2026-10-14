"""Admin contact: user ↔ admin messaging (ticket system)."""
from __future__ import annotations

import time

from app import config as cfg
from app.logger import log
from app.services import settings
from app.runtime import bot
from app.services.telegram import safe_call
from app.storage import db
from app.utils import esc, fmt_ts

_last_ticket: dict[int, float] = {}


def support_line() -> str:
    """One line shown to users, e.g. '@myusername · 10:00-22:00'."""
    parts = [settings.get_str("SUPPORT_CONTACT"), settings.get_str("SUPPORT_NOTE")]
    return " · ".join(part for part in parts if part)


def cooldown_left(user_id: int) -> int:
    cooldown = settings.get_int("TICKET_COOLDOWN", 60)
    last = _last_ticket.get(user_id, 0)
    elapsed = time.time() - last
    if elapsed >= cooldown:
        return 0
    return int(cooldown - elapsed)


async def open_ticket(user_id: int, message: str, username: str = "") -> int | None:
    """Store the message and forward it to every admin with reply buttons."""
    left = cooldown_left(user_id)
    if left:
        return None
    _last_ticket[user_id] = time.time()

    ticket_id = db.open_ticket(user_id, username, message)
    user = db.user(user_id) or {}
    header = (
        f"📩 <b>New message</b> (#{ticket_id})\n"
        f"👤 {esc(user.get('name') or 'User')} · <code>{user_id}</code>"
        f"{' · @' + esc(username) if username else ''}\n"
        f"🕒 {fmt_ts(time.time(), '%Y-%m-%d %H:%M')}\n\n"
        f"{esc(message)}"
    )
    from telethon.tl.custom import Button
    buttons = [
        [Button.inline("✍️ Reply", f"supr:{ticket_id}"), Button.inline("✅ Close", f"supc:{ticket_id}")],
    ]
    for admin_id in cfg.ADMIN_IDS:
        try:
            await safe_call(bot.send_message, admin_id, header, buttons=buttons,
                            what="ticket_notify", retries=1, raise_after_retries=False)
        except Exception as exc:
            log.debug("could not notify admin %s about ticket %s: %s", admin_id, ticket_id, exc)
    return ticket_id


async def send_reply(ticket_id: int, admin_id: int, text: str) -> bool:
    ticket = db.ticket(ticket_id)
    if ticket is None:
        return False
    db.answer_ticket(ticket_id, admin_id, text)
    from app.i18n import t
    body = t(ticket["user_id"], "contact_reply", text=esc(text))
    try:
        await safe_call(bot.send_message, ticket["user_id"], body,
                        what="ticket_reply", retries=1, raise_after_retries=False)
        return True
    except Exception as exc:
        log.warning("ticket reply to %s failed: %s", ticket["user_id"], exc)
        return False


def stats_line() -> str:
    return (f"📩 open tickets: {db.open_ticket_count()} · "
            f"🙋 open requests: {db.open_request_count()} · "
            f"🧾 pending orders: {db.pending_order_count()}")


def support_button_label() -> str:
    return "📞 Admin contact"
