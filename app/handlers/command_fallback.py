"""Never leave a private admin slash command unanswered."""
from telethon import events
from app.runtime import bot
from app.services import access
from app.release import identity
from app.utils import esc


@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.out), priority=1000)
async def command_fallback(event):
    text = (event.raw_text or '').strip()
    if not text.startswith('/') or getattr(event, '_bfix_command_handled', False):
        return
    if access.is_admin(event.sender_id):
        await event.respond('কমান্ডটি চালানো হয়নি। সঠিক তালিকা /help-এ দেখুন।\n'
                            f'চলমান build: <code>{esc(identity())}</code>')
    else:
        await event.respond('শুরু করতে /start অথবা সাহায্যের জন্য /help দিন।')
