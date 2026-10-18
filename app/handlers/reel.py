"""`/reel` — manage the auto-playing media reel on the public website.

The home page shows a vertical, auto-playing reel of 3–8 videos/photos. This
handler is how the owner curates it without touching a computer:

```
/reel                 → the manager (current items, links, on/off)
/reel add             → then send (or forward) a video/photo — pinned to the top
/reel count 4         → how many items the reel shows (1–8)
/reel on | /reel off  → switch the reel on the home page
/reel list            → every pinned item with reorder / delete buttons
/reel clear           → remove all pins (the newest free videos still show)
```

Pinned items are remembered as `(chat_id, msg_id)` in `feed_pins`, so the website
streams them straight from Telegram — no disk, no re-upload.
"""
from __future__ import annotations

from telethon import Button, events
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

from app import runtime
from app.handlers.router import route
from app.handlers.state import ask, end_flow, peek, take
from app.logger import log
from app.runtime import bot
from app.services import access, feed, settings
from app.storage import db

HELP = (
    "🎞 <b>রিল ম্যানেজার</b>\n\n"
    "হোম পেজের অটো-প্লে রিল এখান থেকে সাজান:\n"
    "• <code>/reel add</code> → এরপর ভিডিও/ছবি পাঠান (সরাসরি বটে ফরওয়ার্ড করলেও হবে)\n"
    "• <code>/reel count 4</code> → রিলে কয়টা মিডিয়া দেখাবে (১–৮)\n"
    "• <code>/reel on</code> / <code>/reel off</code> → রিল চালু/বন্ধ\n"
    "• <code>/reel list</code> → সব পিন করা মিডিয়া (উপর-নিচ, ডিলিট)\n"
    "• <code>/reel clear</code> → সব পিন মুছে ফেলুন\n\n"
    "পিন করা মিডিয়া আগে দেখায়, তারপর ফ্রি স্টোরের নতুন ভিডিওগুলো নিজে থেকেই যোগ হয়।"
)


def _is_admin_private(event) -> bool:
    return bool(event.is_private and not event.out and access.is_admin(event.sender_id))


def _site() -> str:
    from app import config as cfg
    url = settings.get_str("SITE_URL", cfg.SITE_URL) or ""
    return url.rstrip("/")


def _card() -> str:
    pins = db.feed_pins(limit=20)
    public = db.recent_public_media(limit=8)
    counts = db.feed_play_counts()
    lines = [HELP, ""]
    lines.append(f"🔘 অবস্থা: <b>{'চালু ✅' if feed.enabled() else 'বন্ধ ⛔'}</b> · "
                 f"রিলে দেখাবে <b>{feed.count()}</b>টা")
    plays = sum(counts.values())
    lines.append(f"📌 পিন করা: <b>{len(pins)}</b>টা · ▶️ এখন দেখা যাচ্ছে "
                 f"<b>{len(feed.latest())}</b>টা · মোট প্লে: <b>{plays}</b>")
    if pins:
        preview = "\n".join(
            f"{index + 1}. {esc(pin.get('title') or pin.get('kind') or 'media')}"
            for index, pin in enumerate(pins[:6]))
        lines.append(f"\n<blockquote>{preview}</blockquote>")
    site = _site()
    if site:
        lines.append(f'\n🔗 <a href="{site}">ওয়েবসাইটে রিল দেখুন</a>')
    return "\n".join(lines)


def esc(text: str) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _buttons() -> list[list[Button]]:
    on = feed.enabled()
    return [
        [Button.inline("➕ মিডিয়া যোগ করুন", "re:add"),
         Button.inline("🔘 চালু" if not on else "⛔ বন্ধ", "re:toggle")],
        [Button.inline("📋 তালিকা", "re:list"),
         Button.inline(f"🔢 সংখ্যা ({feed.count()})", "re:count")],
        [Button.inline("🧹 সব পিন মুছুন", "re:clear")],
    ]


# ------------------------------------------------------------------ commands
@bot.on(events.NewMessage(func=_is_admin_private,
                          pattern=r"(?i)^/reel(@\w+)?(?:\s+([\s\S]*))?$"))
async def reel_command(event) -> None:
    admin_id = event.sender_id
    argument = (event.pattern_match.group(2) or "").strip()
    word, _, rest = argument.partition(" ")
    word = word.lower()

    if word in ("add", "যোগ", "pin"):
        ask(admin_id, "reel_add")
        await event.respond("🎞 ভিডিও বা ছবি পাঠান (বা বটে ফরওয়ার্ড করুন) — "
                            "সেটাই রিলের প্রথমে যোগ হবে। বাতিল: /cancel")
        raise events.StopPropagation

    if word in ("count", "সংখ্যা"):
        try:
            value = int(rest.strip() or word)
        except ValueError:
            value = 0
        if 1 <= value <= 8:
            settings.set("FEED_COUNT", str(value))
            settings.set("FEED_ENABLED", "1")
            await event.respond(f"🔢 রিলে এখন <b>{value}</b>টা মিডিয়া দেখাবে।")
        else:
            rows = [[Button.inline(f"{n}টা", f"re:set:{n}")] for n in (3, 4, 5, 6, 8)]
            await event.respond("🔢 কয়টা মিডিয়া একসাথে দেখাবে?", buttons=rows)
        raise events.StopPropagation

    if word in ("on", "off", "চালু", "বন্ধ"):
        enabled = word in ("on", "চালু")
        settings.set("FEED_ENABLED", "1" if enabled else "0")
        await event.respond("✅ রিল চালু করা হলো।" if enabled else
                            "⛔ রিল বন্ধ করা হলো (হোম পেজ থেকে সরবে)।")
        raise events.StopPropagation

    if word in ("clear", "reset", "মুছুন"):
        for pin in db.feed_pins(limit=200):
            db.delete_feed_pin(pin["id"])
        await event.respond("🧹 সব পিন মুছে ফেলা হলো। ফ্রি স্টোরের নতুন ভিডিও আবার দেখাবে।")
        raise events.StopPropagation

    if word in ("list", "তালিকা"):
        await event.respond("📋 <b>রিলের মিডিয়া</b>", buttons=_list_buttons())
        raise events.StopPropagation

    await event.respond(_card(), buttons=_buttons())
    raise events.StopPropagation


def _list_buttons() -> list[list[Button]]:
    rows = []
    for index, pin in enumerate(db.feed_pins(limit=20)):
        label = (pin.get("title") or pin.get("kind") or "media")[:26]
        rows.append([
            Button.inline(f"{index + 1}. {label}", "re:noop"),
            Button.inline("⬆️", f"re:up:{pin['id']}"),
            Button.inline("⬇️", f"re:down:{pin['id']}"),
            Button.inline("🗑", f"re:del:{pin['id']}"),
        ])
    rows.append([Button.inline("➕ নতুন যোগ করুন", "re:add")])
    return rows


# ------------------------------------------------------------------- capture
@bot.on(events.NewMessage(func=lambda e: _is_admin_private(e)
                          and (peek(e.sender_id) or {}).get("action") == "reel_add"))
async def reel_capture(event) -> None:
    """The media the admin sent right after `/reel add`."""
    if (event.raw_text or '').strip().startswith('/'):
        return  # slash commands always escape a pending media/token prompt

    admin_id = event.sender_id
    media = event.message.media
    if not isinstance(media, (MessageMediaPhoto, MessageMediaDocument)):
        await event.respond("🎞 এটা ভিডিও/ছবি নয় — একটা ভিডিও বা ছবি পাঠান।")
        raise events.StopPropagation
    take(admin_id)
    end_flow(admin_id)
    kind = "photo" if isinstance(media, MessageMediaPhoto) else "video"
    title = (event.raw_text or "").strip()[:120]
    pin_id = db.add_feed_pin(event.chat_id, event.message.id, kind, title)
    log.info("reel: admin %s pinned %s/%s as #%s", admin_id, event.chat_id,
             event.message.id, pin_id)
    await event.respond(f"✅ রিলে যোগ হলো (#{pin_id} · {kind}).\n"
                        + _card(), buttons=_buttons())
    raise events.StopPropagation


# ----------------------------------------------------------------- callbacks
@route("hint:reel")
async def reel_hint(event, rest: str) -> None:
    """The panel's “🎞 হোম রিল” button — opens the manager without typing /reel."""
    admin_id = int(event.sender_id)
    if not access.is_admin(admin_id):
        await event.answer("⛔ শুধু অ্যাডমিন", alert=True)
        return
    await event.answer()
    await event.respond(_card(), buttons=_buttons())


@route("re:")
async def reel_callback(event, rest: str) -> None:
    admin_id = int(event.sender_id)
    if not access.is_admin(admin_id):
        await event.answer("⛔ শুধু অ্যাডমিন", alert=True)
        return

    if rest == "add":
        ask(admin_id, "reel_add")
        await event.answer()
        await event.respond("🎞 ভিডিও বা ছবি পাঠান — রিলের প্রথমে যোগ হবে।")
        return

    if rest == "toggle":
        enabled = not feed.enabled()
        settings.set("FEED_ENABLED", "1" if enabled else "0")
        await event.answer("চালু ✅" if enabled else "বন্ধ ⛔")
        await event.edit(_card(), buttons=_buttons())
        return

    if rest == "list":
        await event.edit("📋 <b>রিলের মিডিয়া</b> — উপরে/নিচে সরান বা মুছুন।",
                         buttons=_list_buttons())
        return

    if rest == "count":
        rows = [[Button.inline(f"{n}টা", f"re:set:{n}")] for n in (3, 4, 5, 6, 8)]
        await event.edit("🔢 রিলে একসাথে কয়টা মিডিয়া অটো-প্লে করবে?", buttons=rows)
        return

    if rest.startswith("set:"):
        try:
            value = int(rest[4:])
        except ValueError:
            value = 3
        settings.set("FEED_COUNT", str(max(1, min(8, value))))
        settings.set("FEED_ENABLED", "1")
        await event.answer(f"{value}টা সেট হলো")
        await event.edit(_card(), buttons=_buttons())
        return

    if rest == "clear":
        for pin in db.feed_pins(limit=200):
            db.delete_feed_pin(pin["id"])
        await event.answer("সব পিন মুছে ফেলা হলো")
        await event.edit(_card(), buttons=_buttons())
        return

    if rest.startswith(("up:", "down:")):
        head, _, raw = rest.partition(":")
        try:
            db.move_feed_pin(int(raw), -1 if head == "up" else 1)
        except ValueError:
            pass
        await event.answer("সরানো হলো")
        await event.edit("📋 <b>রিলের মিডিয়া</b>", buttons=_list_buttons())
        return

    if rest.startswith("del:"):
        try:
            db.delete_feed_pin(int(rest[4:]))
        except ValueError:
            pass
        await event.answer("মুছে ফেলা হলো")
        await event.edit("📋 <b>রিলের মিডিয়া</b>", buttons=_list_buttons())
        return

    if rest == "site":
        site = _site()
        await event.answer(site or "SITE_URL সেট নেই", alert=True)
        return

    await event.answer()


@bot.on(events.NewMessage(func=_is_admin_private, pattern=r"(?i)^/reelhelp(@\w+)?\s*$"))
async def reel_help(event) -> None:
    await event.respond(HELP)
    raise events.StopPropagation
