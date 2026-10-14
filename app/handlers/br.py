"""`/br` - the whole broadcast flow in three steps.

1. `/br`
2. send a video / photo, a text (HTML allowed) and buttons (`name | link`, one per line)
3. `/done` - everyone gets it at once.

Registered first (see HANDLER_MODULES), and it stops the event while a session is open,
so the upload / search handlers never see these messages.
"""
from __future__ import annotations

import time

from telethon import events
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

from app.runtime import bot
from app.services import access, quickcast
from app.services.quickcast import Content

SESSION_TTL = 1800                       # forget a forgotten session after 30 minutes
sessions: dict[int, dict] = {}           # admin_id -> {"content": Content, "at": float}

INTRO = (
    "📣 <b>ব্রডকাস্ট</b>\n\n"
    "পাঠান: ভিডিও/ছবি, টেক্সট (HTML চলবে), বাটন (<code>নাম | লিংক</code>, প্রতি লাইনে একটা)।\n"
    "শেষ হলে <b>/done</b> — সবার কাছে একসাথে চলে যাবে। বাতিল: <b>/cancel</b>"
)


def _is_admin_private(event) -> bool:
    return bool(event.is_private and not event.out and access.is_admin(event.sender_id))


def _live(admin_id: int):
    session = sessions.get(admin_id)
    if session and time.time() - session["at"] > SESSION_TTL:
        sessions.pop(admin_id, None)
        return None
    return session


@bot.on(events.NewMessage(func=_is_admin_private, pattern=r"(?i)^/br(@\w+)?\s*$"))
async def br_start(event) -> None:
    sessions[event.sender_id] = {"content": Content(), "at": time.time()}
    await event.respond(INTRO)
    raise events.StopPropagation


@bot.on(events.NewMessage(func=lambda e: _is_admin_private(e) and _live(e.sender_id) is not None))
async def br_collect(event) -> None:
    admin_id = event.sender_id
    session = _live(admin_id)
    if session is None:
        return
    text = (event.raw_text or "").strip()
    command = text.lower().split("@")[0] if text.startswith("/") else ""

    if command == "/cancel":
        sessions.pop(admin_id, None)
        await event.respond("❌ ব্রডকাস্ট বাতিল।")
        raise events.StopPropagation

    if command == "/done":
        content: Content = session["content"]
        if content.is_empty():
            await event.respond("কিছু পাঠাননি — আগে ভিডিও/ছবি বা টেক্সট দিন।")
            raise events.StopPropagation
        sessions.pop(admin_id, None)
        status = await event.respond("⏳ পাঠানো হচ্ছে…")
        report = await quickcast.send_all(admin_id, content)
        await status.edit(
            f"✅ <b>{report['sent']}</b> জনের কাছে গেছে"
            + (f" · ❌ {report['failed']} জনকে যায়নি" if report["failed"] else "")
            + f" · ⏱ {report['seconds']:.1f} সেকেন্ড")
        raise events.StopPropagation

    if text.startswith("/"):
        return                               # any other command works as usual

    content = session["content"]
    session["at"] = time.time()
    media = event.message.media
    if isinstance(media, (MessageMediaPhoto, MessageMediaDocument)):
        content.media = media
        if text:
            quickcast.add_text(content, text, event.message.entities)
    elif text:
        quickcast.add_text(content, text, event.message.entities)
    else:
        return
    await event.respond(f"{content.summary()}\nআরও দিন, নয়তো <b>/done</b>")
    raise events.StopPropagation
