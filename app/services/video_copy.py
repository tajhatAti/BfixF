"""Durable post-scan video archive, shared with the existing per-store cache.

The example downloader is a reference, never imported or exposed as a website.
Only the main bot runs this worker. Progress/items live in SQLite and backups.
Pause/Stop stop scheduling new items; at most three in-flight uploads may finish.
"""
from __future__ import annotations

import asyncio
import os
import uuid
import time

from telethon.errors import FloodWaitError
from telethon.tl.custom import Button

from app import runtime
from app.logger import log
from app.services import media_cache, settings
from app.storage import db

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
    return [[Button.inline('⏸ বিরতি', f'vc:pause:{store_id}'),
             Button.inline('▶ চালান', f'vc:resume:{store_id}')],
            [Button.inline('⏹ বন্ধ', f'vc:stop:{store_id}'),
             Button.inline('♻ ব্যর্থগুলো আবার', f'vc:retry:{store_id}')]]


def describe(store_id: int) -> str:
    info = summary(store_id)
    counts = info.get('counts', {})
    states = {'queued': 'চলছে', 'paused': 'বিরতিতে', 'stopped': 'বন্ধ', 'done': 'শেষ'}
    return (f"📥 <b>ভিডিও কপি — স্টোর #{store_id}</b>\n"
            f"অবস্থা: {states.get(info.get('state'), 'অপেক্ষায়')}\n"
            f"✅ {counts.get('done', 0)}/{info.get('total', 0)} · "
            f"❌ {counts.get('failed', 0)} · ⏳ {counts.get('pending', 0)}\n"
            'সর্বোচ্চ ৩টি একসঙ্গে · আপলোড শেষে অস্থায়ী ফাইল মুছে যাবে\n'
            'বিরতি/বন্ধ দিলে চলমান আপলোড শেষ হতে পারে।')


async def report(store_id: int) -> None:
    info = job(store_id)
    if not info:
        return
    try:
        if info.get('message_id'):
            await asyncio.wait_for(runtime.bot.edit_message(info['chat_id'], info['message_id'], describe(store_id),
                                                            buttons=buttons(store_id)), timeout=10)
        else:
            msg = await asyncio.wait_for(runtime.bot.send_message(
                info['admin_id'], describe(store_id), buttons=buttons(store_id)), timeout=10)
            if msg:
                db._run('UPDATE video_copy_jobs SET chat_id=?,message_id=? WHERE store_id=?',
                        (info['admin_id'], msg.id, store_id))
    except Exception as exc:
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
    db._run("UPDATE video_copy_items SET state='running',attempts=attempts+1 WHERE store_id=? AND file_id=?",
            (store_id, file_id))
    try:
        updated = await media_cache.ensure_mirror(row, store, archive=True)
        if db.impl is not bound:
            return  # a website restore replaced the database while we uploaded
        if not media_cache.is_archived(updated, store):
            raise RuntimeError(media_cache.mirror_problem(file_id) or 'ভিডিও কপি সম্পন্ন হয়নি')
        db._run("UPDATE video_copy_items SET state='done',last_error='' WHERE store_id=? AND file_id=?",
                (store_id, file_id))
    except asyncio.CancelledError:
        if db.impl is bound:
            db._run("UPDATE video_copy_items SET state='pending' WHERE store_id=? AND file_id=?", (store_id, file_id))
        raise
    except FloodWaitError as exc:
        if db.impl is not bound:
            return
        # Flood waiting is not a failed-file attempt. Persist it across restarts.
        until = time.time() + max(1, exc.seconds) + 1
        db.set_meta(f'video_copy_flood:{store["admin_id"]}', str(until))
        db._run("UPDATE video_copy_items SET state='pending',attempts=MAX(0,attempts-1),ready_at=? "
                "WHERE store_id=? AND file_id=?", (until, store_id, file_id))
    except Exception as exc:
        if db.impl is not bound:
            return
        log.warning('Video archive store=%s file=%s: %s', store_id, file_id, exc)
        # Detailed diagnostic is admin-only, never a public media response.
        attempts = db._one('SELECT attempts FROM video_copy_items WHERE store_id=? AND file_id=?',
                           (store_id, file_id))['attempts']
        db._run("UPDATE video_copy_items SET state=?,ready_at=?,last_error=? WHERE store_id=? AND file_id=?",
                ('failed' if attempts >= 3 else 'pending', time.time() + 60,
                 str(exc)[:500], store_id, file_id))
    finally:
        # This paces every slot, not just the task that finished last.
        await asyncio.sleep(max(3, min(30, settings.get_int('VIDEO_COPY_DELAY', 4))))


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
                done, tasks = await asyncio.wait(tasks, timeout=20)
                for task in done:
                    task.result()
                for store_id in stores:
                    await report(store_id)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def kick() -> None:
    """Called after scanning and by the existing scheduler on every restart."""
    global _task
    if os.getenv('BOT_EXTRA') == '1':
        return  # enqueue from any bot, but one main scheduler performs the work
    if _task is not None and not _task.done():
        return
    _task = runtime.spawn(_run())
