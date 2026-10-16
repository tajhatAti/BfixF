"""Learn every user's Telegram access hash from ordinary traffic.

Registered **first** in ``HANDLER_MODULES`` and deliberately never raises
``StopPropagation``: it only listens, writes a row to the `users` table and lets
every other handler run exactly as before.

Why it matters: `/br`, the campaign studio and every single button that sends a
message need ``(user_id, access_hash)``. Telethon keeps that in the session file,
which hosting panels wipe — the cache in the database does not get wiped
(``app/services/peers.py``).
"""
from __future__ import annotations

from telethon import events

from app.logger import log
from app.runtime import bot
from app.services import peers


@bot.on(events.NewMessage())
async def learn_sender(event) -> None:
    await peers.learn_from_event(event)


@bot.on(events.CallbackQuery())
async def learn_clicker(event) -> None:
    await peers.learn_from_event(event)


@bot.on(events.ChatAction())
async def learn_action(event) -> None:
    """Someone joined a group/channel the bot is in — also worth remembering."""
    try:
        user = await event.get_user()
    except Exception:
        user = None
    if user is not None and getattr(user, "id", None):
        peers.remember(user.id, getattr(user, "access_hash", None),
                       getattr(user, "first_name", None), getattr(user, "username", None))
        log.debug("remembered peer %s from a chat action", user.id)
