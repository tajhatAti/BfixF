"""One free video per explicit group /start; no unsolicited group blasting."""
import asyncio
import time
from telethon import events, Button
from app import runtime
from app.runtime import bot
from app.handlers.router import route
from app.logger import log
from app.services import access, flow, forcejoin, settings, sender
from app.storage import db

_locks = {}


def buttons(file_id=0):
    username = runtime.bot_username
    if not username:
        return []
    home = f'https://t.me/{username}'
    rows = [[Button.url('🟢 অনলাইন — বটে খুলুন', home + (f'?start=f{file_id}' if file_id else '?start=home'))],
            [Button.url('➕ নিজের গ্রুপে যোগ করুন', home + '?startgroup=promo')]]
    seen = set()
    for target in forcejoin.global_targets():
        url = forcejoin.invite_of(target)
        if url and url not in seen:
            seen.add(url)
            rows.append([Button.url('📢 চ্যানেল জয়েন করুন', url)])
    extra = settings.get_str('GROUP_PROMO_URL')
    if extra.startswith(('https://t.me/', 'http://t.me/')):
        rows.append([Button.url('👥 আমাদের গ্রুপ', extra)])
    return rows


async def promote(event, user_id, store_id=None):
    if not settings.get_bool('GROUP_PROMO_ENABLED', True):
        return
    if not user_id:
        return
    raw = (getattr(event, 'raw_text', '') or '').strip().split(maxsplit=1)
    if len(raw) > 1 and raw[1].startswith('s') and raw[1][1:].isdigit():
        store_id = int(raw[1][1:])
    gate = await flow.gate_check(user_id, store_id)
    if gate['missing']:
        await show_join_prompt(event, user_id, store_id, gate['missing'])
        return
    if db.is_banned(user_id) and not access.is_admin(user_id):
        return
    lock = _locks.setdefault(event.chat_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        key = f'group_promo_time:{event.chat_id}'
        last = float(db.get_meta(key, '0') or 0)
        cooldown = max(30, settings.get_int('GROUP_PROMO_COOLDOWN', 60))
        if time.time() - last < cooldown:
            return
        candidates = [row for row in db.public_gallery(limit=100, kind='Video')
                      if (not store_id or row['store_id'] == store_id)]
        # A per-store required channel must never be bypassed by a generic /start.
        chosen = None
        blocked = None
        previous = db.get_meta(f'group_promo_file:{event.chat_id}', '')
        candidates.sort(key=lambda r: str(r['id']) == previous)
        for row in candidates:
            store_gate = await flow.gate_check(user_id, row['store_id'])
            if not store_gate['missing']:
                chosen = row
                break
            if blocked is None:
                blocked = (row['store_id'], store_gate['missing'])
        if chosen is None and blocked:
            await show_join_prompt(event, user_id, blocked[0], blocked[1])
            return
        if chosen is None:
            await event.respond('🎬 এখন শেয়ার করার মতো ফ্রি ভিডিও নেই। বটে সংগ্রহ দেখুন।', buttons=buttons())
            db.set_meta(key, str(time.time()))
            return
        result = await sender.deliver(event.chat_id, chosen, caption_text='', buttons=buttons(chosen['id']))
        if result.ok:
            db.set_meta(key, str(time.time()))
            db.set_meta(f'group_promo_file:{event.chat_id}', str(chosen['id']))
            db.log_event('group_promo', user_id, chosen['store_id'], chosen['id'], str(event.chat_id))
        else:
            log.warning('Group promotion delivery failed (%s): %s', result.reason, result.detail)
            await event.respond('ভিডিওটি এখন প্রস্তুত করা যায়নি। বটে খুলে চেষ্টা করুন।', buttons=buttons(chosen['id']))


@bot.on(events.NewMessage(incoming=True, func=lambda e: bool(getattr(e, 'is_group', False)),
                          pattern=r'(?i)^/start(?:@([\w]+))?(?:\s+[^\n]*)?\s*$'))
async def group_start(event):
    addressed = event.pattern_match.group(1)
    if addressed and addressed.lower() != runtime.bot_username.lower():
        return
    await promote(event, event.sender_id)
    raise events.StopPropagation


@bot.on(events.ChatAction())
async def added_to_group(event):
    if not getattr(event, 'user_added', False) or runtime.bot_id not in (event.user_ids or []):
        return
    if not getattr(event, 'is_group', False):
        return
    adder = await event.get_added_by()
    await promote(event, getattr(adder, 'id', None))


async def show_join_prompt(event, user_id, store_id, missing):
    rows = []
    for target in missing:
        url = forcejoin.invite_of(target)
        if url:
            rows.append([Button.url('📢 চ্যানেল জয়েন করুন', url)])
    rows.append([Button.inline('✅ জয়েন করেছি — ভিডিও দিন', f'gpj:{user_id}:{store_id or 0}')])
    if runtime.bot_username:
        rows.append([Button.url('🤖 ব্যক্তিগত চ্যাটে খুলুন', f'https://t.me/{runtime.bot_username}?start=home')])
    await event.respond(forcejoin.gate_text(missing), buttons=rows)


@route('gpj:')
async def group_join_retry(event, rest):
    parts = rest.split(':')
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await event.answer('আবার /start দিন।', alert=True)
        return
    uid, store_id = map(int, parts)
    if uid != event.sender_id:
        await event.answer('নিজে /start দিয়ে শুরু করুন।', alert=True)
        return
    if not getattr(event, 'is_group', False):
        await event.answer('এই বাটনটি গ্রুপের জন্য।', alert=True)
        return
    await event.answer('সদস্যপদ পরীক্ষা করছি…')
    await promote(event, uid, store_id or None)
