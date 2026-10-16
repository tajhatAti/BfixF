"""Emergency switches in the bot: maintenance mode, /maint, /emergency.

(`/backup` ও `/restore` — `app/handlers/portable.py`-তে।)

Registered first (see HANDLER_MODULES) so a user turned away by maintenance mode never
reaches the normal handlers.
"""
from __future__ import annotations

import time

from telethon import events

from app.runtime import bot
from app.services import access, maintenance, media_cache, settings
from app.storage import db

_told: dict[int, float] = {}               # user_id -> when we last answered during maintenance
TELL_EVERY = 60


def _admin_private(event) -> bool:
    return bool(event.is_private and not event.out and access.is_admin(event.sender_id))


@bot.on(events.NewMessage(func=lambda e: bool(e.is_private and not e.out
                                              and maintenance.blocks(e.sender_id))))
async def maintenance_message(event) -> None:
    now = time.time()
    if now - _told.get(event.sender_id, 0) > TELL_EVERY:
        _told[event.sender_id] = now
        await event.respond(maintenance.message())
    raise events.StopPropagation


@bot.on(events.CallbackQuery(func=lambda e: maintenance.blocks(e.sender_id)))
async def maintenance_callback(event) -> None:
    await event.answer(maintenance.message()[:190], alert=True)
    raise events.StopPropagation


@bot.on(events.NewMessage(func=_admin_private, pattern=r"(?i)^/maint(?:@\w+)?(?:\s+(on|off))?\s*$"))
async def maint_command(event) -> None:
    arg = (event.pattern_match.group(1) or "").lower()
    if arg:
        settings.set("MAINTENANCE_MODE", "1" if arg == "on" else "0")
    state = "চালু 🔴 (শুধু admin বট ব্যবহার করতে পারবে)" if maintenance.active() else "বন্ধ 🟢"
    await event.respond(f"🛠 মেইনটেন্যান্স মোড: <b>{state}</b>\n\n<code>/maint on</code> · <code>/maint off</code>")
    raise events.StopPropagation


HELP_TEXT = """📡 <b>ক্যাশ চ্যানেল</b> — যেখানে প্রতিটা ভিডিওর বট-কপি থাকে।

<code>/storageset</code> — এখন কোন চ্যানেল ব্যবহার হচ্ছে দেখুন
<code>/storageset @mychannel</code> — একটা শেয়ার্ড চ্যানেল দিন (বট ও ইউজারবট দুজনেই
  সেখানে অ্যাডমিন থাকতে হবে)
<code>/storageset -1001234567890</code> — আইডি দিয়েও দেওয়া যায়, অথবা কোনো চ্যানেলের
  মেসেজে <b>রিপ্লাই</b> করে <code>/storageset</code> লিখুন
<code>/storageset auto</code> — প্রতি স্টোরের জন্য আলাদা চ্যানেল (ডিফল্ট)
<code>/storageset probe</code> — এখন যা সেট আছে সেটা খোলা যায় কি না পরীক্ষা করুন

<i>লগে “Could not find the input entity for PeerChannel” এলে <code>/storageset probe</code>
বা <code>/storageset auto</code> চাপলেই সাধারণত ঠিক হয়ে যায়।</i>"""


def _current_storage() -> str:
    stored = settings.get_str("STORAGE_CHANNEL", "")
    auto = db.get_meta("auto_storage_channel", "")
    if stored:
        return f"<code>{stored}</code> (সেট করা)"
    if auto:
        return f"<code>{auto}</code> (অটো তৈরি)"
    return "🅰 অটো — প্রতি স্টোরের নিজের চ্যানেল"


async def _probe_storage() -> str:
    """Try to open the configured channel with a real session and report the truth."""
    target = media_cache.configured_storage()
    if target is None:
        return "ℹ️ শেয়ার্ড চ্যানেল সেট নেই — প্রতিটা স্টোরের নিজের ক্যাশ চ্যানেল ব্যবহৃত হচ্ছে।"
    from app import runtime as _runtime
    client = next(iter(_runtime.user_clients.values()), None)
    if client is None:
        return ("⚠️ কোনো Telegram session কানেক্ট নেই — চ্যানেল পরীক্ষা করা গেল না। "
                "ওয়েবসাইটের 📲 Telegram session পেজ থেকে যুক্ত করুন।")
    try:
        await media_cache._resolve_peer(client, target)
    except Exception as exc:
        return (f"❌ চ্যানেল খোলা যায়নি: <code>{str(exc)[:120]}</code>\n"
                f"{media_cache._CHANNEL_FIX}")
    return f"✅ চ্যানেল ঠিকঠাক খুলছে (<code>{target}</code>)"


@bot.on(events.NewMessage(func=_admin_private, pattern=r"(?i)^/storageset(?:@\w+)?(?:\s+([\s\S]*))?$"))
async def storageset_command(event) -> None:
    """Point the media cache at a channel without editing config.env by hand."""
    argument = (event.pattern_match.group(1) or "").strip()
    lowered = argument.lower()

    if not argument:
        await event.respond(HELP_TEXT + f"\n\nএখন: {_current_storage()}")
        raise events.StopPropagation

    if lowered in ("probe", "check", "টেস্ট"):
        await event.respond(await _probe_storage())
        raise events.StopPropagation

    if lowered in ("auto", "off", "none", "বন্ধ", "অটো"):
        settings.set("STORAGE_CHANNEL", "")
        settings.set("STORAGE_CHANNEL_WARNING", "")
        await event.respond("✅ এখন থেকে <b>প্রতিটা স্টোরের আলাদা ক্যাশ চ্যানেল</b> ব্যবহৃত হবে "
                            "(প্রয়োজনে নিজে থেকেই তৈরি হবে)।")
        raise events.StopPropagation

    # A reply inside the channel itself is the easiest way to name a private channel.
    target = argument
    if not target and event.is_reply:
        try:
            chat = await event.get_chat()
            target = str(getattr(chat, "id", "") or "")
            target = f"-100{target}" if target.isdigit() else target
        except Exception:
            target = ""
    if not target:
        await event.respond("কোন চ্যানেল? <code>/storageset @name</code> অথবা "
                            "চ্যানেলের মেসেজে রিপ্লাই করে লিখুন।\n\n" + _current_storage())
        raise events.StopPropagation

    settings.set("STORAGE_CHANNEL", target)
    probe = await _probe_storage()
    await event.respond(f"📡 ক্যাশ চ্যানেল সেট হলো: <code>{target}</code>\n{probe}")
    raise events.StopPropagation


@bot.on(events.NewMessage(func=_admin_private, pattern=r"(?i)^/emergency(?:@\w+)?\s*$"))
async def emergency_command(event) -> None:
    await event.respond(
        "🚨 <b>জরুরি</b>\n\n"
        "• <code>/maint on</code> / <code>/maint off</code> — ইউজারদের জন্য বট বন্ধ/চালু\n"
        "• <code>/backup</code> — পুরো ইনস্টলের ব্যাকআপ ফাইল এখনই এখানে পাঠান\n"
        "• <code>/restore</code> — ব্যাকআপ ফাইল ফিরিয়ে এনে সব ডেটা ফিরিয়ে আনুন\n"
        "• ওয়েবসাইটে <code>/admin/emergency</code> — স্ট্যাটাস, STORAGE_CHANNEL, ভিডিও আবার কপি, force-join বন্ধ করা")
    raise events.StopPropagation
