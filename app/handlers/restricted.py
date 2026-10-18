"""Admin-only Restricted download setup and dedicated-channel link receiver."""
from telethon import Button, events

from app.handlers.router import route
from app.runtime import bot
from app.services import access, restricted
from app.storage import db
from app.utils import esc

HELP = ('📥 <b>Restricted download — শুধু অ্যাডমিন</b>\n\n'
        'আলাদা প্রাইভেট চ্যানেল তৈরি হবে, বট সেখানে অ্যাডমিন থাকবে। '
        'ওই চ্যানেলে একটি Telegram পোস্টের লিংক দিন—পোস্ট/অ্যালবামের ছবি, ভিডিও ও অন্যান্য '
        'ডাউনলোডযোগ্য মিডিয়া ক্যাপশন ছাড়া সেখানেই পাঠাবে।\n\n'
        '✅ এই চ্যানেলের মিডিয়া ফরওয়ার্ড করা যাবে; স্টোরের প্রটেকশন বদলাবে না।\n'
        '🔑 আপনার সেশনকে সোর্স চ্যানেলে আগে থেকেই অ্যাক্সেস থাকতে হবে।\n'
        '/restricted on — তৈরি/চালু · /restricted off — বন্ধ\n'
        '/restricted retry — ব্যর্থ পোস্টের বাকি মিডিয়া আবার')


async def show(event, action=''):
    if not access.is_admin(event.sender_id):
        return
    if action == 'on':
        try:
            info = await restricted.setup(event.sender_id)
        except Exception as exc:
            from app.logger import log
            log.warning('Restricted setup failed for %s: %s', event.sender_id, exc)
            await event.respond('চালু করা যায়নি। আগে /session কানেক্ট করুন এবং চ্যানেল তৈরির অনুমতি পরীক্ষা করুন।')
            return
    elif action == 'retry':
        restricted.retry(event.sender_id)
        restricted.kick()
    elif action == 'off':
        db._run('UPDATE restricted_channels SET enabled=0 WHERE admin_id=?', (event.sender_id,))
    info = restricted.channel_for(event.sender_id)
    text = HELP
    buttons = [[Button.inline('📥 চ্যানেল তৈরি / চালু', 'rdl:on'), Button.inline('⏹ বন্ধ', 'rdl:off')]]
    buttons.append([Button.inline('♻ অসম্পন্ন মিডিয়া আবার', 'rdl:retry')])
    if info:
        text += '\n\nঅবস্থা: ' + ('✅ চালু' if info['enabled'] else '⏹ বন্ধ')
        if info.get('link'):
            buttons.insert(0, [Button.url('📣 ডাউনলোড চ্যানেল খুলুন', info['link'])])
    await event.respond(text, buttons=buttons, link_preview=False)


@bot.on(events.NewMessage(pattern=r'^/restricted(?:@\w+)?(?:\s+(on|off|retry))?\s*$',
                          func=lambda e: e.is_private and not e.out))
async def command(event):
    if access.is_admin(event.sender_id):
        match = event.pattern_match
        await show(event, (match.group(1) or '') if match else '')
    raise events.StopPropagation


@route('rdl:')
async def callback(event, action):
    if not access.is_admin(event.sender_id):
        await event.answer('শুধু অ্যাডমিনের জন্য', alert=True)
        return
    await event.answer()
    await show(event, action if action in ('on', 'off', 'retry') else '')


@bot.on(events.NewMessage(func=lambda e: not e.is_private and bool(restricted.channel(e.chat_id))))
async def channel_link(event):
    info = restricted.channel(event.chat_id)
    if not info:
        return
    # A registered channel is created as broadcast=True: only its admins can post.
    # Allow anonymous channel-as-sender posts, but never unrelated user senders.
    sender = getattr(event, 'sender_id', None)
    if sender not in (info['admin_id'], event.chat_id) and not access.is_admin(sender):
        return
    text = (event.raw_text or '').strip()
    if not text.startswith(('https://t.me/', 'http://t.me/', 't.me/')):
        return  # output media has no caption, so it cannot loop back into this handler
    try:
        added = restricted.enqueue(event.chat_id, event.id, text)
    except ValueError as exc:
        await event.respond(esc(str(exc)))
        return
    if added:
        await event.respond('📥 লিংক নেওয়া হয়েছে। সর্বোচ্চ ৩টি মিডিয়া একসঙ্গে প্রস্তুত হবে।')
        restricted.kick()
    raise events.StopPropagation
