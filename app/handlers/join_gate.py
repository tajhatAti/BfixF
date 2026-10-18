"""Global private gate before ordinary bot operations; resume /start after joining."""
from telethon import events, Button
from app import runtime
from app.runtime import bot
from app.handlers.router import route
from app.services import access, flow, forcejoin
from app.storage import db


def _private_user(event):
    return bool(event.is_private and not getattr(event, 'out', False)
                and not access.is_admin(event.sender_id))


async def require_join(event):
    result = await flow.gate_check(event.sender_id, None)
    if not result['missing']:
        return False
    rows = forcejoin.gate_keyboard(event.sender_id, 0, result['missing'])[:-1]
    rows.append([Button.inline('✅ জয়েন করেছি — পরীক্ষা করুন', f'jg:{event.sender_id}')])
    await event.respond(forcejoin.gate_text(result['missing']), buttons=rows)
    return True


@bot.on(events.NewMessage(func=_private_user), priority=-80)
async def global_message_gate(event):
    raw = (event.raw_text or '').strip()
    if raw.split(maxsplit=1)[0:1] and raw.split(maxsplit=1)[0].split('@')[0].lower() == '/start':
        parts = raw.split(maxsplit=1)
        db.set_meta(f'join_start:{event.sender_id}', parts[1][:128] if len(parts) > 1 else '')
    if await require_join(event):
        raise events.StopPropagation


@bot.on(events.CallbackQuery(func=_private_user), priority=-80)
async def global_callback_gate(event):
    if (event.data or b'').startswith(b'jg:'):
        return  # this route checks again, then resumes the saved start payload
    if await require_join(event):
        await event.answer('আগে চ্যানেল জয়েন করুন।', alert=True)
        raise events.StopPropagation


@route('jg:')
async def resume_start(event, rest):
    if rest != str(event.sender_id):
        await event.answer('এই বাটনটি আপনার জন্য নয়।', alert=True)
        return
    await event.answer('সদস্যপদ পরীক্ষা করছি…')
    if await require_join(event):
        return
    sender = await event.get_sender()
    is_new = db.touch_user(event.sender_id, getattr(sender, 'first_name', ''),
                           getattr(sender, 'username', ''))
    key = f'join_start:{event.sender_id}'
    payload = db.get_meta(key, '')
    db.set_meta(key, '')
    from app.handlers.user import handle_payload, show_home
    if payload and await handle_payload(event, event.sender_id, payload, is_new):
        return
    await show_home(event, event.sender_id)
