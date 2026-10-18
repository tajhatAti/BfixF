"""Admin-owned, captionless post downloader in its own unprotected channel.

Never changes protection on stores/source channels. A session must already have
legitimate source access. Only exact registered broadcast channels accept links.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from telethon import functions
from telethon.errors import FloodWaitError, ChatNotModifiedError

from app import runtime
from app.logger import log
from app.services import media_cache, native_media, settings
from app.services.telegram import safe_call
from app.storage import db

_task: asyncio.Task | None = None
_owner = uuid.uuid4().hex
_setup_locks: dict[int, asyncio.Lock] = {}


def parse_link(text: str) -> tuple[str | int, int]:
    value = (text or '').strip()
    if value.startswith('t.me/'):
        value = 'https://' + value
    parsed = urlsplit(value)
    if parsed.scheme not in ('https', 'http') or parsed.netloc.lower() not in ('t.me', 'www.t.me'):
        raise ValueError('শুধু Telegram পোস্টের লিংক দিন।')
    path = parsed.path.strip('/').split('/')
    if path and path[0] == 's':
        path = path[1:]
    if len(path) == 3 and path[0] == 'c' and path[1].isdigit() and path[2].isdigit():
        peer, message = int('-100' + path[1]), int(path[2])
    elif len(path) == 2 and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', path[0]) and path[1].isdigit():
        peer, message = '@' + path[0], int(path[1])
    else:
        raise ValueError('পোস্টের পুরো লিংক দিন, যেমন https://t.me/channel/123')
    if message <= 0:
        raise ValueError('পোস্ট নম্বর ঠিক নয়।')
    return peer, message


def channel_for(admin_id: int) -> dict | None:
    row = db._one('SELECT * FROM restricted_channels WHERE admin_id=?', (admin_id,))
    return dict(row) if row else None


def channel(chat_id: int) -> dict | None:
    row = db._one('SELECT * FROM restricted_channels WHERE chat_id=? AND enabled=1', (chat_id,))
    return dict(row) if row else None


async def setup(admin_id: int) -> dict:
    async with _setup_locks.setdefault(admin_id, asyncio.Lock()):
        existing = channel_for(admin_id)
        if existing and existing.get('link'):
            db._run('UPDATE restricted_channels SET enabled=1 WHERE admin_id=?', (admin_id,))
            return channel_for(admin_id)
        client = runtime.user_clients.get(admin_id)
        if client is None:
            raise ValueError('আগে /session দিয়ে নিজের Telegram সেশন কানেক্ট করুন।')
        bound = db.impl
        peer_id = existing['chat_id'] if existing else await media_cache._create_cache_channel(
            client, 'Desi Site · Restricted download',
            'Admin-only post downloader. Captionless copies may be forwarded.', remember=True)
        if not peer_id or db.impl is not bound:
            raise ValueError('চ্যানেল তৈরি করা যায়নি। সেশন ও বটের অনুমতি দেখুন।')
        # Keep an incomplete setup, so retry never creates a pile of channels.
        db._run('INSERT OR IGNORE INTO restricted_channels(chat_id,admin_id,link,enabled) VALUES(?,?,?,0)',
                (peer_id, admin_id, ''))
        peer = await media_cache._resolve_peer(client, peer_id)
        try:
            await safe_call(client, functions.messages.ToggleNoForwardsRequest(peer=peer, enabled=False),
                            what='restricted_unprotect', retries=1)
        except ChatNotModifiedError:
            pass  # a new channel already has forwarding enabled
        invite = await safe_call(client, functions.messages.ExportChatInviteRequest(peer),
                                 what='restricted_invite', retries=1)
        if db.impl is not bound:
            raise ValueError('ডেটাবেজ restore হয়েছে; আবার চেষ্টা করুন।')
        db._run('UPDATE restricted_channels SET link=?,enabled=1 WHERE admin_id=?',
                (invite.link, admin_id))
        return channel_for(admin_id)


def enqueue(target: int, request_id: int, text: str) -> bool:
    info = channel(target)
    if not info:
        return False
    peer, message = parse_link(text)
    cursor = db._run('INSERT OR IGNORE INTO restricted_jobs(target,request_id,admin_id,source,source_msg,state) '
                     "VALUES(?,?,?,?,?,'pending')", (target, request_id, info['admin_id'], str(peer), message))
    return bool(cursor.rowcount)


async def album(client, peer, message_id: int) -> list:
    anchor = await safe_call(client.get_messages, peer, ids=message_id, what='restricted_post', retries=1)
    if not anchor or not getattr(anchor, 'media', None):
        raise ValueError('পোস্টটি নেই অথবা মিডিয়া নেই; সেশনের অ্যাকাউন্টের অ্যাক্সেস পরীক্ষা করুন।')
    grouped = getattr(anchor, 'grouped_id', None)
    messages = [anchor]
    if grouped:
        # Telegram albums contain at most 10 adjacent media messages. Resolve
        # both sides so a link to the middle/last item still gets the whole album.
        nearby = await safe_call(client.get_messages, peer,
                                 ids=list(range(max(1, message_id - 10), message_id + 11)),
                                 what='restricted_album', retries=1)
        messages = [m for m in nearby if m and getattr(m, 'grouped_id', None) == grouped]
    result = {m.id: m for m in messages if getattr(m, 'media', None) and (
        getattr(m, 'photo', None) or getattr(m, 'document', None) or native_media.is_video(m))}
    if not result:
        raise ValueError('এই পোস্টে ডাউনলোডযোগ্য মিডিয়া পাওয়া যায়নি।')
    return [result[k] for k in sorted(result)]


async def transfer(client, peer, message, target: int):
    """True photo/video uploads with no caption/protection; all temps scoped."""
    from app.services.transfer_progress import Tracker
    info = channel(target) or {}
    tracker = Tracker(f'channel:{target}', message.id, info.get('admin_id', 0), f'পোস্টের মিডিয়া #{message.id}')
    tracker.phase('prepare')
    size = int(getattr(getattr(message, 'file', None), 'size', 0) or 0)
    max_mb = settings.get_int('MIRROR_MAX_MB', 1900)
    if max_mb and size > max_mb * 1024 * 1024:
        raise ValueError('ফাইলটি MIRROR_MAX_MB সীমার চেয়ে বড়।')
    required = size or (max_mb or 1900) * 1024 * 1024
    with tempfile.TemporaryDirectory(prefix='bfixf-restricted-') as directory:
        free = shutil.disk_usage(directory).free
        reservation = (target, message.id)
        if required + sum(media_cache._reserved_downloads.values()) + 64 * 1024 * 1024 > free:
            raise ValueError('সার্ভারে যথেষ্ট ফাঁকা ডিস্ক নেই।')
        media_cache._reserved_downloads[reservation] = required
        try:
            path = str(Path(directory) / ('media' + native_media.extension(message)))
            tracker.phase('download', size)
            if not await media_cache._download(client, message, path, size, progress=tracker.callback):
                raise ValueError('মিডিয়া ডাউনলোড করা যায়নি।')
            kwargs = native_media.upload_options(message)
            if native_media.is_video(message):
                thumb = str(Path(directory) / 'thumb.jpg')
                try:
                    await safe_call(client.download_media, message, file=thumb, thumb=-1,
                                    what='restricted_thumb', retries=1)
                except FloodWaitError:
                    raise
                except Exception as exc:
                    log.debug('Optional thumbnail unavailable: %s', exc)
                if Path(thumb).exists() and Path(thumb).stat().st_size:
                    kwargs['thumb'] = thumb
            tracker.phase('upload', Path(path).stat().st_size)
            kwargs['progress_callback'] = tracker.callback
            sent = await safe_call(client.send_file, peer, path, caption='',
                                   **kwargs, what='restricted_upload', retries=1)
            if not sent or not getattr(sent, 'media', None):
                raise RuntimeError('মিডিয়া আপলোড নিশ্চিত করা যায়নি।')
            tracker.phase('done', detail='ডাউনলোড চ্যানেলে আপলোড হয়েছে।')
            return sent
        except BaseException as exc:
            tracker.phase('waiting' if isinstance(exc, asyncio.CancelledError) else 'failed', detail=str(exc))
            raise
        finally:
            media_cache._reserved_downloads.pop(reservation, None)


async def process(job: dict) -> None:
    client = runtime.user_clients.get(job['admin_id'])
    if not client:
        return  # wait for owner session, no attempts consumed
    if not channel(job['target']):
        return
    bound = db.impl
    peer_ref = int(job['source']) if job['source'].lstrip('-').isdigit() else job['source']
    db._run("UPDATE restricted_jobs SET state='running',attempts=attempts+1 WHERE id=?", (job['id'],))
    try:
        if hasattr(client, 'is_connected') and not client.is_connected():
            await client.connect()
        source = await media_cache._resolve_peer(client, peer_ref)
        target = await media_cache._resolve_peer(client, job['target'])
        messages = await album(client, source, job['source_msg'])
        for message in messages:
            db._run('INSERT OR IGNORE INTO restricted_items(job_id,source_msg) VALUES(?,?)', (job['id'], message.id))
        semaphore = asyncio.Semaphore(3)
        async def one(message):
            async with semaphore:
                if not channel(job['target']):
                    raise ValueError('ডাউনলোড চ্যানেল বন্ধ করা হয়েছে।')
                row = db._one('SELECT sent_msg FROM restricted_items WHERE job_id=? AND source_msg=?',
                              (job['id'], message.id))
                if row['sent_msg']:
                    return
                sent = await transfer(client, target, message, job['target'])
                if db.impl is bound:
                    db._run('UPDATE restricted_items SET sent_msg=? WHERE job_id=? AND source_msg=?',
                            (sent.id, job['id'], message.id))
        tasks = [asyncio.create_task(one(m)) for m in messages]
        # Let each active transfer finish/clean up before retrying a partial album.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if db.impl is not bound:
            return
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]
        db._run("UPDATE restricted_jobs SET state='done',last_error='' WHERE id=?", (job['id'],))
        try:
            await runtime.bot.send_message(job['target'], f'✅ {len(messages)}টি মিডিয়া পাঠানো হয়েছে—ক্যাপশন ছাড়া।',
                                           reply_to=job['request_id'])
        except Exception:
            pass  # notification failure must not retry successfully uploaded media
    except FloodWaitError as exc:
        if db.impl is bound:
            db._run("UPDATE restricted_jobs SET state='pending',attempts=MAX(0,attempts-1),ready_at=? WHERE id=?",
                    (time.time() + exc.seconds + 1, job['id']))
    except Exception as exc:
        log.warning('Restricted job %s: %s', job['id'], exc)
        if db.impl is bound:
            attempts = db._one('SELECT attempts FROM restricted_jobs WHERE id=?', (job['id'],))['attempts']
            db._run('UPDATE restricted_jobs SET state=?,ready_at=?,last_error=? WHERE id=?',
                    ('failed' if attempts >= 3 else 'pending', time.time() + 60, str(exc)[:500], job['id']))
            if attempts >= 3:
                try:
                    await runtime.bot.send_message(job['admin_id'],
                        '⚠️ Restricted download শেষ করা যায়নি। সোর্সের অ্যাক্সেস/সেশন পরীক্ষা করুন। '
                        'অসম্পন্ন মিডিয়া আবার পাঠাতে /restricted retry দিন।')
                except Exception:
                    pass


def _claim() -> bool:
    now = time.time()
    return bool(db._run(
        "INSERT INTO video_copy_lease(id,owner,until) VALUES(1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,until=excluded.until "
        "WHERE video_copy_lease.until<? OR video_copy_lease.owner=?",
        (_owner, now + 120, now, _owner)).rowcount)


async def _run() -> None:
    # Shared lease with store archiving: at most three archive/post transfers
    # globally, even if the website and bot are launched as separate processes.
    if not _claim():
        return
    bound = db.impl
    worker = asyncio.current_task()
    async def heartbeat():
        while True:
            await asyncio.sleep(20)
            if db.impl is not bound or not _claim():
                worker.cancel()
                return
    guard = asyncio.create_task(heartbeat())
    try:
        db._run("UPDATE restricted_jobs SET state='pending' WHERE state='running'")
        rows = db._q("SELECT * FROM restricted_jobs WHERE state='pending' AND ready_at<=? ORDER BY id", (time.time(),))
        for row in rows:
            if db.impl is not bound:
                return
            await process(dict(row))
    finally:
        guard.cancel()
        await asyncio.gather(guard, return_exceptions=True)
        if db.impl is bound:
            db._run('DELETE FROM video_copy_lease WHERE owner=?', (_owner,))


def kick() -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = runtime.spawn(_run())


def retry(admin_id: int) -> int:
    """Keep sent-item checkpoints; only retry failed jobs owned by this admin."""
    return db._run("UPDATE restricted_jobs SET state='pending',attempts=0,ready_at=0,last_error='' "
                   "WHERE admin_id=? AND state='failed'", (admin_id,)).rowcount
