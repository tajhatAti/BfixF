"""Emergency switches in the bot: maintenance mode, /maint, /backup, /emergency.

Registered first (see HANDLER_MODULES) so a user turned away by maintenance mode never
reaches the normal handlers.
"""
from __future__ import annotations

import time

from telethon import events

from app.runtime import bot
from app.services import access, backup, maintenance, settings

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


@bot.on(events.NewMessage(func=_admin_private, pattern=r"(?i)^/backup(?:@\w+)?\s*$"))
async def backup_command(event) -> None:
    status = await event.respond("⏳ ব্যাকআপ বানানো হচ্ছে…")
    try:
        result = await backup.send_backup("manual", [event.sender_id])
        await status.edit(f"✅ ব্যাকআপ পাঠানো হয়েছে ({result['size_mb']} MB)" if result["sent"]
                          else "❌ ব্যাকআপ বানানো গেছে কিন্তু পাঠানো যায়নি।")
    except Exception as exc:
        await status.edit(f"❌ ব্যাকআপ ব্যর্থ: {str(exc)[:150]}")
    raise events.StopPropagation


@bot.on(events.NewMessage(func=_admin_private, pattern=r"(?i)^/emergency(?:@\w+)?\s*$"))
async def emergency_command(event) -> None:
    await event.respond(
        "🚨 <b>জরুরি</b>\n\n"
        "• <code>/maint on</code> / <code>/maint off</code> — ইউজারদের জন্য বট বন্ধ/চালু\n"
        "• <code>/backup</code> — ডাটাবেস ব্যাকআপ এখনই এখানে পাঠান\n"
        "• ওয়েবসাইটে <code>/admin/emergency</code> — স্ট্যাটাস, STORAGE_CHANNEL, ভিডিও আবার কপি, force-join বন্ধ করা")
    raise events.StopPropagation
