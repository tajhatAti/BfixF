"""First command gate: private diagnostics/help, independent of pending flows."""
from telethon import events
from app import config as cfg, runtime
from app.runtime import bot
from app.services import access, command_catalog as catalog, error_reports
from app.release import identity
from app.storage import db
from app.utils import esc


@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.out), priority=-100)
async def command_gate(event):
    raw = (event.raw_text or '').strip()
    if not raw.startswith('/'):
        return
    first, *rest = raw.split(maxsplit=1)
    command, sep, addressed = first[1:].partition('@')
    command = command.lower()
    if sep and runtime.bot_username and addressed.lower() != runtime.bot_username.lower():
        raise events.StopPropagation
    admin = access.is_admin(event.sender_id)
    if command in catalog.ALL_NAMES:
        from app.handlers.state import take
        take(event.sender_id)  # stale media/session prompts must never swallow commands
    if command == 'whoami':
        await event.respond(f'আপনার Telegram ID: <code>{event.sender_id}</code>\n'
                            f'অ্যাডমিন অনুমতি: {"আছে" if admin else "নেই"}\n'
                            f'Build: <code>{esc(identity())}</code>')
        raise events.StopPropagation
    if command in ('help', 'commands'):
        for page in catalog.help_pages(admin):
            await event.respond(page)
        raise events.StopPropagation
    if command in catalog.ADMIN_NAMES and not admin:
        await event.respond('এই কাজের অনুমতি নেই। নিজের ID দেখতে /whoami দিন।')
        raise events.StopPropagation
    if command in ('version', 'status'):
        text = (f'🧩 Build: <code>{esc(identity())}</code>\n'
                f'বট: @{esc(runtime.bot_username)} · আপনার ID: {event.sender_id}\n'
                f'Handlers: {len(runtime.handler_registrations)}\n'
                f'Download worker heartbeat: {esc(db.get_meta("video_copy_last_tick", "এখনো নেই"))}\n'
                'কাজ দেখতে /downloads · সব কমান্ড /help')
        await event.respond(text)
        raise events.StopPropagation
    if command == 'errors':
        if not cfg.ADMIN_IDS or event.sender_id != cfg.ADMIN_IDS[0]:
            await event.respond('ত্রুটির বিস্তারিত শুধু মূল মালিক দেখতে পারবেন।')
        else:
            rows = error_reports.recent(8)
            text = '\n\n'.join(f'<b>{esc(r["ref"] or "error")}</b>\n<pre>{esc(r["detail"][:330])}</pre>' for r in rows)
            await event.respond('⚠️ <b>মালিকের error inbox</b>\n\n' + (text or 'কোনো ত্রুটি রেকর্ড নেই।'))
        raise events.StopPropagation
    # Legacy handlers such as /session didn't accept @bot or mixed case.
    if command in catalog.ALL_NAMES and (sep or first != '/' + command):
        event.message.message = '/' + command + (' ' + rest[0] if rest else '')
