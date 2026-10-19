"""Free videos in groups: one video every GROUP_PROMO_GAP seconds, in a queue.

Whoever asks while a video is still "in its slot" is told how many seconds are left,
and gets *their* video at the next free slot — so any number of people can ask, and
the group still sees one video per slot. No other limit.
"""
import asyncio
import math
import random
import time
from telethon import events, Button
from app import runtime
from app.runtime import bot
from app.handlers.router import route
from app.logger import log
from app.services import access, flow, forcejoin, settings, sender
from app.storage import db

ANON_ADMIN_ID = 1087968824       # "GroupAnonymousBot": what an anonymous admin looks like
QUEUE_CAP = 200                  # safety net only: waiting requests per group

_next_slot: dict[int, float] = {}          # chat -> time the next video may go out
_queued: dict[int, int] = {}               # chat -> requests currently waiting
_bags: dict[tuple, dict] = {}              # (chat, store) -> shuffled "not shown yet" videos


def _is_tap(event) -> bool:
    return getattr(event, 'data', None) is not None


async def _notify(event, text: str):
    """A button tap gets a pop-up alert; a typed /start gets a reply. Returns that reply."""
    if _is_tap(event):
        try:
            await event.answer(text, alert=True)
            return None
        except Exception as exc:                 # already answered → fall back to a reply
            log.debug('pop-up failed: %s', exc)
    try:
        return await event.reply(text)
    except Exception as exc:
        log.debug('notice failed: %s', exc)
        return None


def buttons(file_id=0):
    username = runtime.bot_username
    if not username:
        return []
    home = f'https://t.me/{username}'
    rows = [[Button.inline('🎬 আরেকটা ভিডিও', 'gnext')]] if file_id else []
    rows += [[Button.url('🟢 অনলাইন — বটে খুলুন', home + (f'?start=f{file_id}' if file_id else '?start=home'))],
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


def _pick_order(chat_id: int, store_id, rows: list[dict]) -> list[dict]:
    """Every video is shown once before any repeats; new uploads join the current round."""
    key = (chat_id, store_id or 0)
    state = _bags.setdefault(key, {'bag': [], 'known': set()})
    by_id = {row['id']: row for row in rows}
    state['bag'] = [i for i in state['bag'] if i in by_id]
    for new_id in set(by_id) - state['known']:
        state['bag'].insert(random.randint(0, len(state['bag'])), new_id)
    state['known'] = set(by_id)
    if not state['bag']:
        ids = list(by_id)
        random.shuffle(ids)
        previous = db.get_meta(f'group_promo_file:{chat_id}', '')
        if len(ids) > 1 and str(ids[0]) == previous:
            ids.append(ids.pop(0))               # never the same video twice in a row
        state['bag'] = ids
    return [by_id[i] for i in state['bag']]


async def _send_one(event, user_id, store_id, anonymous: bool) -> None:
    async def gate_for(store):
        if anonymous:
            return {'missing': []}
        return await flow.gate_check(user_id, store)

    rows = [row for row in db.public_gallery(limit=5000, kind='Video')
            if (not store_id or row['store_id'] == store_id)]
    chosen, blocked = None, None
    for row in _pick_order(event.chat_id, store_id, rows):
        store_gate = await gate_for(row['store_id'])      # a per-store channel is never bypassed
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
        return
    result = await sender.deliver(event.chat_id, chosen, caption_text='', buttons=buttons(chosen['id']))
    if result.ok:
        state = _bags.get((event.chat_id, store_id or 0))
        if state and chosen['id'] in state['bag']:
            state['bag'].remove(chosen['id'])
        db.set_meta(f'group_promo_file:{event.chat_id}', str(chosen['id']))
        db.log_event('group_promo', None if anonymous else user_id, chosen['store_id'],
                     chosen['id'], str(event.chat_id))
    else:
        log.warning('Group promotion delivery failed (%s): %s', result.reason, result.detail)
        await event.respond('ভিডিওটি এখন প্রস্তুত করা যায়নি। বটে খুলে চেষ্টা করুন।', buttons=buttons(chosen['id']))


async def promote(event, user_id, store_id=None):
    if not settings.get_bool('GROUP_PROMO_ENABLED', True):
        return
    if not user_id:
        return
    raw = (getattr(event, 'raw_text', '') or '').strip().split(maxsplit=1)
    if len(raw) > 1 and raw[1].startswith('s') and raw[1][1:].isdigit():
        store_id = int(raw[1][1:])
    # An anonymous admin posts as the group itself: not a real user, so there is no
    # membership to check.
    anonymous = user_id == ANON_ADMIN_ID or user_id == event.chat_id or user_id < 0
    if not anonymous:
        gate = await flow.gate_check(user_id, store_id)
        if gate['missing']:
            await show_join_prompt(event, user_id, store_id, gate['missing'])
            return
        if db.is_banned(user_id) and not access.is_admin(user_id):
            return

    chat = event.chat_id
    gap = max(0, settings.get_int('GROUP_PROMO_GAP', 10))
    now = time.time()
    slot = max(now, _next_slot.get(chat, 0.0))
    wait = slot - now
    if wait > 0.5 and _queued.get(chat, 0) >= QUEUE_CAP:
        await _notify(event, '⏳ এখন অনেকে সিরিয়ালে আছে — একটু পরে আবার চেষ্টা করুন।')
        return
    _next_slot[chat] = slot + gap              # reserve this slot before any await
    note = None
    if wait > 0.5:
        _queued[chat] = _queued.get(chat, 0) + 1
        seconds = math.ceil(wait)
        note = await _notify(event, f'⏳ আপনার ভিডিও {seconds} সেকেন্ড পরে আসবে — একটু অপেক্ষা করুন। '
                                    f'(সিরিয়ালে {_queued[chat]} নম্বর)')
        try:
            await asyncio.sleep(wait)
        finally:
            _queued[chat] = max(0, _queued.get(chat, 1) - 1)
    try:
        await _send_one(event, user_id, store_id, anonymous)
    finally:
        if note is not None:
            try:
                await note.delete()
            except Exception:
                pass


@bot.on(events.NewMessage(incoming=True, func=lambda e: bool(getattr(e, 'is_group', False)),
                          pattern=r'(?i)^/start(?:@([\w]+))?(?:\s+[^\n]*)?\s*$'))
async def group_start(event):
    addressed = event.pattern_match.group(1)
    if addressed and addressed.lower() != runtime.bot_username.lower():
        return
    await promote(event, event.sender_id)
    raise events.StopPropagation


@route('gnext')
async def group_next_video(event, _rest=''):
    """The 🎬 button under a group video: same rules as /start, but too-early taps get a pop-up."""
    if not getattr(event, 'is_group', False):
        await event.answer('এই বাটনটি গ্রুপের জন্য।', alert=True)
        return
    await promote(event, event.sender_id)
    try:
        await event.answer()                    # stop the spinner (no-op if a pop-up was shown)
    except Exception:
        pass


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
