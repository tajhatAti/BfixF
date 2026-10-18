"""Durable post-scan video archive, shared with the existing per-store cache.

The example downloader is a reference, never imported or exposed as a website.
Bots coordinate this worker with a shared database lease. Progress/items live in SQLite and backups.
Pause/Stop stop scheduling new items; at most three in-flight uploads may finish.
"""
from __future__ import annotations

import asyncio
import uuid
import time

from telethon.errors import FloodWaitError
from telethon.tl.custom import Button

from app import runtime
from app.logger import log
from app.services import media_cache, settings
from app.storage import db
from app.utils import esc

_task: asyncio.Task | None = None
_owner = uuid.uuid4().hex


def job(store_id: int) -> dict | None:
    row = db._one('SELECT * FROM video_copy_jobs WHERE store_id=?', (int(store_id),))
    return dict(row) if row else None


def enqueue(store_id: int, admin_id: int, source_chat: int) -> dict:
    """Rescans add missing videos, never reset completed/paused work."""
    store = db.store(store_id)
    if not store or int(store['admin_id']) != int(admin_id):
        raise ValueError('স্টোরের মালিক মেলেনি')
    now = time.time()
    db._run('INSERT OR IGNORE INTO video_copy_jobs(store_id,admin_id,updated_at) VALUES(?,?,?)',
            (store_id, admin_id, now))
    rows = db._q("SELECT * FROM files WHERE store_id=? AND chat_id=? AND kind='Video' ORDER BY msg_id,id",
                 (store_id, source_chat))
    added = 0
    for row in rows:
        # A prior independent copy is already durable. A source-as-mirror isn't.
        done = media_cache.is_archived(dict(row), store)
        cursor = db._run('INSERT OR IGNORE INTO video_copy_items(store_id,file_id,state) VALUES(?,?,?)',
                        (store_id, row['id'], 'done' if done else 'pending'))
        added += cursor.rowcount
        if not done:
            # A channel reset invalidates a previous destination, not the source.
            added += db._run("UPDATE video_copy_items SET state='pending',attempts=0,ready_at=0 "
                             "WHERE store_id=? AND file_id=? AND state='done'",
                             (store_id, row['id'])).rowcount
    current = job(store_id)
    if current['state'] in ('done', 'stopped') and added:
        db._run("UPDATE video_copy_jobs SET state='queued',updated_at=? WHERE store_id=?", (now, store_id))
    return summary(store_id)


def summary(store_id: int) -> dict:
    out = job(store_id) or {}
    counts = {row['state']: row['n'] for row in db._q(
        'SELECT state,COUNT(*) n FROM video_copy_items WHERE store_id=? GROUP BY state', (store_id,))}
    out.update(counts=counts, total=sum(counts.values()))
    return out


def control(store_id: int, action: str) -> bool:
    if action not in ('pause', 'resume', 'stop', 'retry') or not job(store_id):
        return False
    if action == 'retry':
        db._run("UPDATE video_copy_items SET state='pending',attempts=0,ready_at=0,last_error='' "
                "WHERE store_id=? AND state='failed'", (store_id,))
    state = {'pause': 'paused', 'resume': 'queued', 'stop': 'stopped', 'retry': 'queued'}[action]
    db._run('UPDATE video_copy_jobs SET state=?,updated_at=? WHERE store_id=?',
            (state, time.time(), store_id))
    return True


def buttons(store_id: int):
    rows = [[Button.inline('🔄 লাইভ অবস্থা', f'vc:view:{store_id}')],
            [Button.inline('⏸ বিরতি', f'vc:pause:{store_id}'),
             Button.inline('▶ চালান', f'vc:resume:{store_id}')],
            [Button.inline('⏹ বন্ধ', f'vc:stop:{store_id}'),
             Button.inline('♻ ব্যর্থগুলো আবার', f'vc:retry:{store_id}')]]
    target = db.get_meta(f'store_cache_channel:{store_id}', '')
    if target.startswith('-100'):
        rows.insert(0, [Button.url('📣 কপি যে চ্যানেলে যাচ্ছে', f'https://t.me/c/{target[4:]}/1')])
    return rows


def waiting_reason(store_id: int) -> str:
    info = summary(store_id)
    store = db.store(store_id)
    if not info or not store:
        return 'কোনো কপি-জব নেই। স্ক্যান করা ভিডিও যোগ করতে /downloads দিন।'
    counts, now = info['counts'], time.time()
    if info['state'] == 'paused':
        return 'আপনি Pause করেছেন। চালাতে ▶ চালান চাপুন।'
    if info['state'] == 'stopped':
        return 'আপনি Stop করেছেন। চালাতে ▶ চালান চাপুন।'
    if not counts.get('pending') and not counts.get('running'):
        return ('ব্যর্থ ভিডিও আছে—♻ ব্যর্থগুলো আবার চাপুন।' if counts.get('failed') else 'সব ভিডিওর কপি সম্পন্ন।')
    if store['admin_id'] not in runtime.user_clients:
        return (f"স্টোরের মালিক {store['admin_id']}-এর সেশন এই worker-এ কানেক্ট নেই। "
                'যে বটে কাজ চালাবেন সেখানে মালিকের অ্যাকাউন্ট থেকে /session দিন।')
    until = float(db.get_meta(f"video_copy_flood:{store['admin_id']}", '0') or 0)
    if until > now:
        return f'Telegram FloodWait: আরও {int(until-now)} সেকেন্ড অপেক্ষা।'
    row = db._one("SELECT MIN(ready_at) due FROM video_copy_items WHERE store_id=? AND state='pending'", (store_id,))
    if row and row['due'] and row['due'] > now:
        return f"ব্যর্থ চেষ্টার পর স্বয়ংক্রিয় retry: আরও {int(row['due']-now)} সেকেন্ড।"
    lease = db._one('SELECT owner,until FROM video_copy_lease WHERE id=1')
    if lease and lease['owner'] != _owner and lease['until'] > now:
        return 'অন্য download worker চলছে/আগের worker-এর lock ছাড়ার অপেক্ষা (সর্বোচ্চ প্রায় ২ মিনিট)।'
    tick = float(db.get_meta('video_copy_last_tick', '0') or 0)
    if now - tick > 90:
        return 'download worker-এর সাম্প্রতিক heartbeat নেই। বট/হোস্ট restart ও লগ দেখুন।'
    return 'ফাইল/চ্যানেল প্রস্তুত হচ্ছে বা খালি transfer slot-এর অপেক্ষায়।'


def describe(store_id: int) -> str:
    from app.services import transfer_progress
    info = summary(store_id)
    counts = info.get('counts', {})
    store = db.store(store_id) or {}
    target = db.get_meta(f'store_cache_channel:{store_id}', '')
    states = {'queued': 'কিউতে', 'paused': 'বিরতিতে', 'stopped': 'বন্ধ', 'done': 'শেষ'}
    errors = db._q("SELECT file_id,last_error FROM video_copy_items WHERE store_id=? AND last_error!='' "
                   'ORDER BY file_id DESC LIMIT 2', (store_id,))
    details = ''   # per-file errors stay in the DB; the card shows counts only
    destination = f'<code>{esc(target)}</code>' if target else 'এখনো তৈরি হয়নি'
    publish = store.get('publish_channel_id')
    if publish and str(publish) != target:
        destination += f' (পাবলিশ চ্যানেল {publish} আলাদা)'
    return (f"📥 <b>ভিডিও কপি — {esc(store.get('name') or str(store_id))}</b>\n"
            f"অবস্থা: {states.get(info.get('state'), 'অপেক্ষায়')}\n"
            f"✅ {counts.get('done', 0)}/{info.get('total', 0)} · "
            f"❌ {counts.get('failed', 0)} · ⬇⬆ {counts.get('running', 0)} · ⏳ {counts.get('pending', 0)}\n"
            f"গন্তব্য স্টোর-ক্যাশ চ্যানেল: {destination}\n"
            f"{esc(waiting_reason(store_id))}\n\n"
            f"{transfer_progress.render(scope=f'store:{store_id}')}"
            f"{details}\n\nসর্বোচ্চ ৩টি একসঙ্গে · ETA শুধু বর্তমান ধাপের আনুমানিক সময়।\n"
            'বিরতি/বন্ধ দিলে চলমান আপলোড শেষ হতে পারে।')


async def report(store_id: int) -> None:
    info = job(store_id)
    if not info:
        return
    text = describe(store_id)
    try:
        report_bot = db.get_meta(f'copy_report_bot:{store_id}', '')
        if info.get('message_id') and report_bot == runtime.bot_username:
            await asyncio.wait_for(runtime.bot.edit_message(info['chat_id'], info['message_id'], text,
                                                            buttons=buttons(store_id)), timeout=10)
        else:
            msg = await asyncio.wait_for(runtime.bot.send_message(
                info['admin_id'], text, buttons=buttons(store_id)), timeout=10)
            if msg:
                db._run('UPDATE video_copy_jobs SET chat_id=?,message_id=? WHERE store_id=?',
                        (info['admin_id'], msg.id, store_id))
                db.set_meta(f'copy_report_bot:{store_id}', runtime.bot_username)
    except Exception as exc:
        from telethon.errors import MessageIdInvalidError, MessageNotModifiedError
        if isinstance(exc, MessageNotModifiedError):
            return
        if isinstance(exc, MessageIdInvalidError):
            db._run('UPDATE video_copy_jobs SET message_id=0 WHERE store_id=?', (store_id,))
        log.debug('Video copy progress update: %s', exc)


async def _copy(item: dict) -> None:
    bound = db.impl
    file_id, store_id = item['file_id'], item['store_id']
    row = db.file(file_id)
    store = db.store(store_id)
    if not row or not store or row['store_id'] != store_id:
        db._run("UPDATE video_copy_items SET state='failed',last_error='ফাইল/স্টোর নেই' WHERE file_id=? AND store_id=?",
                (file_id, store_id))
        return
    # Do not exhaust retries while an admin's session is temporarily offline.
    if runtime.user_clients.get(store['admin_id']) is None:
        return
    from app.services.transfer_progress import Tracker
    tracker = Tracker(f'store:{store_id}', file_id, store['admin_id'], row['name'])
    tracker.phase('prepare')
    db._run("UPDATE video_copy_items SET state='running',attempts=attempts+1 WHERE store_id=? AND file_id=?",
            (store_id, file_id))
    try:
        updated = await media_cache.ensure_mirror(row, store, archive=True)
        if db.impl is not bound:
            return  # a website restore replaced the database while we uploaded
        if not media_cache.is_archived(updated, store):
            raise RuntimeError(media_cache.mirror_problem(file_id) or 'ভিডিও কপি সম্পন্ন হয়নি')
        tracker.phase('done', detail='স্টোরের ক্যাশ চ্যানেলে কপি সংরক্ষিত হয়েছে।')
        db._run("UPDATE video_copy_items SET state='done',last_error='' WHERE store_id=? AND file_id=?",
                (store_id, file_id))
    except asyncio.CancelledError:
        if db.impl is bound:
            tracker.phase('waiting', detail='Transfer বন্ধ হয়েছে; পুনরায় শুরু করার অপেক্ষায়।')
            db._run("UPDATE video_copy_items SET state='pending' WHERE store_id=? AND file_id=?", (store_id, file_id))
        raise
    except FloodWaitError as exc:
        if db.impl is not bound:
            return
        # Flood waiting is not a failed-file attempt. Persist it across restarts.
        tracker.phase('flood', detail=f'Telegram অপেক্ষা: {exc.seconds} সেকেন্ড')
        until = time.time() + max(1, exc.seconds) + 1
        db.set_meta(f'video_copy_flood:{store["admin_id"]}', str(until))
        db._run("UPDATE video_copy_items SET state='pending',attempts=MAX(0,attempts-1),ready_at=? "
                "WHERE store_id=? AND file_id=?", (until, store_id, file_id))
    except Exception as exc:
        if db.impl is not bound:
            return
        from app.services.error_reports import friendly_error
        reason = friendly_error(exc)
        low = f'{exc} {reason}'.lower()
        permanent = any(m in low for m in ('input entity', 'invalid channel', 'চ্যানেলটি পড়তে পারছে না',
                                           'চ্যানেলটি চেনে না'))
        tracker.phase('failed', detail='স্কিপ করা হয়েছে')
        log.warning('Video archive store=%s file=%s: %s', store_id, file_id, reason)
        # Detailed diagnostic is admin-only, never a public media response.
        attempts = db._one('SELECT attempts FROM video_copy_items WHERE store_id=? AND file_id=?',
                           (store_id, file_id))['attempts']
        db._run("UPDATE video_copy_items SET state=?,ready_at=?,last_error=? WHERE store_id=? AND file_id=?",
                ('failed' if (permanent or attempts >= 3) else 'pending', time.time() + 60,
                 reason[:500], store_id, file_id))
    finally:
        # This paces every slot, not just the task that finished last.
        await asyncio.sleep(max(0, min(30, settings.get_int('VIDEO_COPY_DELAY', 0))))


def _claim() -> bool:
    now = time.time()
    return bool(db._run(
        "INSERT INTO video_copy_lease(id,owner,until) VALUES(1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,until=excluded.until "
        "WHERE video_copy_lease.until<? OR video_copy_lease.owner=?",
        (_owner, now + 120, now, _owner)).rowcount)


async def _run() -> None:
    if not _claim():
        return
    bound = db.impl
    try:
        await _run_owned(bound)
    finally:
        if db.impl is bound:
            db._run('DELETE FROM video_copy_lease WHERE owner=?', (_owner,))


async def _run_owned(bound) -> None:
    # Only one main worker owns these rows. An interrupted upload resumes from
    # its last completed item (Telegram message and mirror stored before done).
    db._run("UPDATE video_copy_items SET state='pending' WHERE state='running'")
    while True:
        if db.impl is not bound or not _claim():
            return
        # An explicit post request gets a turn between store-copy batches.
        if any(r['admin_id'] in runtime.user_clients for r in db._q(
                "SELECT j.admin_id FROM restricted_jobs j JOIN restricted_channels c "
                "ON c.chat_id=j.target WHERE j.state='pending' AND j.ready_at<=? AND c.enabled=1",
                (time.time(),))):
            return
        db._run("UPDATE video_copy_items SET state='failed',last_error='ফাইলটি আর এই স্টোরে নেই' "
                "WHERE state IN ('pending','running') AND NOT EXISTS "
                "(SELECT 1 FROM files f WHERE f.id=video_copy_items.file_id "
                "AND f.store_id=video_copy_items.store_id)")
        now = time.time()
        candidates = db._q("SELECT i.* FROM video_copy_items i JOIN video_copy_jobs j ON j.store_id=i.store_id "
                           "JOIN files f ON f.id=i.file_id JOIN stores s ON s.id=j.store_id "
                           "WHERE j.state='queued' AND i.state='pending' AND i.ready_at<=? "
                           "ORDER BY f.msg_id,f.id", (now,))
        selected = []
        for row in candidates:
            store = db.store(row['store_id'])
            if not store or store['admin_id'] not in runtime.user_clients:
                continue
            until = float(db.get_meta(f'video_copy_flood:{store["admin_id"]}', '0') or 0)
            if now < until:
                continue
            selected.append(dict(row))
            if len(selected) == 3:
                break
        if not selected:
            for info in db._q("SELECT store_id FROM video_copy_jobs WHERE state='queued'"):
                counts = summary(info['store_id'])['counts']
                if not counts.get('pending') and not counts.get('running'):
                    db._run("UPDATE video_copy_jobs SET state='done' WHERE store_id=?", (info['store_id'],))
                await report(info['store_id'])
            return  # scheduler wakes it again when a retry/session is due
        stores = {item['store_id'] for item in selected}
        for store_id in stores:
            await report(store_id)
        tasks = {asyncio.create_task(_copy(item)) for item in selected}
        try:
            while tasks:
                if db.impl is not bound or not _claim():
                    return
                done, tasks = await asyncio.wait(tasks, timeout=5)
                for task in done:
                    task.result()
                for store_id in stores:
                    await report(store_id)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def kick() -> None:
    """Called after scanning and by the schedulers on every restart."""
    global _task
    db.set_meta('video_copy_last_tick', str(time.time()))
    if _task is not None and not _task.done():
        return
    backfill()
    _task = runtime.spawn(_run())


def backfill() -> int:
    """Recover pre-existing scanned videos without re-scanning or touching posts.

    Only scanner-owned FILE_* rows (or channels in the scan audit) qualify;
    arbitrary manually uploaded files/private bot DMs do not become archive jobs.
    Explicitly paused/stopped jobs stay paused/stopped.
    """
    groups = db._q("SELECT DISTINCT f.store_id,f.chat_id,s.admin_id FROM files f JOIN stores s ON s.id=f.store_id "
                   "WHERE f.kind='Video' AND f.chat_id<0 AND "
                   "(f.code LIKE 'FILE_%' OR EXISTS (SELECT 1 FROM events e WHERE e.name='scan' "
                   "AND e.store_id=f.store_id AND e.ref=CAST(f.chat_id AS TEXT))) "
                   "AND NOT EXISTS (SELECT 1 FROM video_copy_items i WHERE i.store_id=f.store_id AND i.file_id=f.id)")
    added = 0
    for row in groups:
        info = job(row['store_id'])
        if info and info['state'] in ('paused', 'stopped'):
            continue
        enqueue(row['store_id'], row['admin_id'], row['chat_id'])
        added += 1
    return added


async def monitor() -> None:
    """Extra bots also recover transfers; the shared lease enforces three slots.

    This is NOT a second broadcast/report scheduler. A scan in a backup bot no
    longer depends on the primary bot staying online or seeing its session.
    """
    from app.services import restricted
    while True:
        try:
            restricted.kick()
            kick()
        except Exception:
            log.exception('Transfer monitor failed')
        await asyncio.sleep(20)
