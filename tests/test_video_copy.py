"""Post-scan archive integration; no live Telegram account required."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.types import InputPeerChannel

from app import config as cfg, runtime
from app.services import media_cache, scanner, settings, video_copy
from app.storage import db

ADMIN = 424242
SOURCE = -1003075551195
TARGET = -1004075551195


@pytest.fixture()
def clean(tmp_path, monkeypatch):
    db.bind(str(tmp_path / 'copy.sqlite3'))
    settings._cache.clear()
    monkeypatch.setattr(runtime, 'user_clients', {ADMIN: object()})
    monkeypatch.setattr(cfg, 'ADMIN_IDS', [ADMIN])
    monkeypatch.setattr(media_cache, '_file_locks', {})
    monkeypatch.setattr(media_cache, '_reserved_downloads', {})
    monkeypatch.setattr(video_copy, '_task', None)
    store = db.create_store(ADMIN, 'Archive store')
    db.set_meta(f'store_cache_channel:{store["id"]}', str(TARGET))
    return store


def add(store, n=1, kind='Video', chat=SOURCE):
    return db.add_file(store['id'], f'clip {n}', kind, chat, n, size=100, duration=12)


def item(store, file_id):
    return dict(db._one('SELECT * FROM video_copy_items WHERE store_id=? AND file_id=?',
                        (store['id'], file_id)))


async def no_sleep(*_args):
    pass


def test_queue_is_video_only_idempotent_and_keeps_source(clean):
    first = add(clean)
    add(clean, 2, kind='Photo')
    add(clean, 3, chat=SOURCE - 1)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    assert video_copy.summary(clean['id'])['total'] == 1
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    assert video_copy.summary(clean['id'])['total'] == 1
    assert db.file(first)['chat_id'] == SOURCE
    with pytest.raises(ValueError):
        video_copy.enqueue(clean['id'], ADMIN + 1, SOURCE)


def test_source_as_mirror_does_not_count_as_an_archived_copy(clean):
    first = add(clean)
    second = add(clean, 2)
    db.set_mirror(first, SOURCE, 1)
    db.set_mirror(second, TARGET, 5)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    assert item(clean, first)['state'] == 'pending'
    assert item(clean, second)['state'] == 'done'


def test_pause_stop_and_retry_are_persistent(clean):
    file_id = add(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    video_copy.control(clean['id'], 'pause')
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    assert video_copy.job(clean['id'])['state'] == 'paused'
    path = str(db.path)
    db.bind(path)
    assert video_copy.job(clean['id'])['state'] == 'paused'
    video_copy.control(clean['id'], 'stop')
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    assert video_copy.job(clean['id'])['state'] == 'stopped'
    db._run("UPDATE video_copy_items SET state='failed',attempts=3 WHERE file_id=?", (file_id,))
    video_copy.control(clean['id'], 'retry')
    assert item(clean, file_id)['attempts'] == 0
    assert video_copy.job(clean['id'])['state'] == 'queued'
    assert not video_copy.control(clean['id'], 'invalid')


@pytest.mark.asyncio
async def test_worker_has_exactly_three_slots_and_skips_completed_on_restart(clean, monkeypatch):
    for n in range(1, 9):
        add(clean, n)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    active = peak = 0
    calls = []
    original_sleep = asyncio.sleep
    async def mirror(row, store, **kwargs):
        nonlocal active, peak
        assert kwargs['archive'] is True
        active += 1
        peak = max(peak, active)
        calls.append(row['id'])
        await original_sleep(0.001)
        db.set_mirror(row['id'], TARGET, row['id'] + 100)
        active -= 1
        return db.file(row['id'])
    monkeypatch.setattr(media_cache, 'ensure_mirror', mirror)
    monkeypatch.setattr(video_copy, 'report', no_sleep)
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    await video_copy._run()
    assert peak == 3
    assert video_copy.summary(clean['id'])['counts'] == {'done': 8}
    assert video_copy.job(clean['id'])['state'] == 'done'
    await video_copy._run()
    assert len(calls) == 8


@pytest.mark.asyncio
async def test_pause_finishes_only_current_batch(clean, monkeypatch):
    for n in range(1, 7):
        add(clean, n)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    async def mirror(row, store, **kw):
        video_copy.control(clean['id'], 'pause')
        db.set_mirror(row['id'], TARGET, row['id'])
        return db.file(row['id'])
    monkeypatch.setattr(media_cache, 'ensure_mirror', mirror)
    monkeypatch.setattr(video_copy, 'report', no_sleep)
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    await video_copy._run()
    assert video_copy.summary(clean['id'])['counts'] == {'done': 3, 'pending': 3}


@pytest.mark.asyncio
async def test_three_failures_are_recoverable_and_do_not_block_later_files(clean, monkeypatch):
    file_id = add(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    async def failed(*args, **kw):
        raise RuntimeError('no access')
    monkeypatch.setattr(media_cache, 'ensure_mirror', failed)
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    for _ in range(3):
        await video_copy._copy(item(clean, file_id))
    row = item(clean, file_id)
    assert row['state'] == 'failed' and row['attempts'] == 3
    assert row['last_error'] == 'no access'
    video_copy.control(clean['id'], 'retry')
    assert item(clean, file_id)['state'] == 'pending'


@pytest.mark.asyncio
async def test_flood_wait_is_saved_without_exhausting_attempts(clean, monkeypatch):
    file_id = add(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    async def flood(*args, **kw):
        raise FloodWaitError(request=None, capture=120)
    monkeypatch.setattr(media_cache, 'ensure_mirror', flood)
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    await video_copy._copy(item(clean, file_id))
    row = item(clean, file_id)
    assert row['state'] == 'pending' and row['attempts'] == 0
    assert row['ready_at'] > 0
    assert float(db.get_meta(f'video_copy_flood:{ADMIN}')) == row['ready_at']


@pytest.mark.asyncio
async def test_missing_session_waits_instead_of_failing(clean, monkeypatch):
    file_id = add(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    monkeypatch.setattr(runtime, 'user_clients', {})
    await video_copy._copy(item(clean, file_id))
    assert item(clean, file_id)['attempts'] == 0
    await video_copy._run()
    assert video_copy.job(clean['id'])['state'] == 'queued'


class ArchiveClient:
    def __init__(self, forward_ok=True, upload_error=False, cancel=False):
        self.forward_ok = forward_ok
        self.upload_error = upload_error
        self.cancel = cancel
        self.events = []
        self.paths = []
        self.attrs = [NS(duration=12, w=320, h=240)]
        self.source = NS(id=1, media=object(), file=NS(size=100),
                         document=NS(attributes=self.attrs))

    async def forward_messages(self, target, msg_id, **kw):
        self.events.append('forward')
        assert kw['drop_author'] is True and kw['drop_media_captions'] is True
        if not self.forward_ok:
            raise RuntimeError('CHAT_FORWARDS_RESTRICTED')
        return NS(id=42, chat_id=TARGET, media=object())

    async def download_media(self, msg, file=None, thumb=None, progress_callback=None):
        self.events.append('thumb' if thumb is not None else 'download')
        self.paths.append(file)
        Path(file).write_bytes(b'jpg' if thumb is not None else b'v' * 100)
        return file

    async def send_file(self, target, path, **kw):
        self.events.append('upload')
        self.upload = kw
        assert kw['caption'] == ''
        assert Path(path).exists() and Path(kw['thumb']).exists()
        if self.cancel:
            raise asyncio.CancelledError()
        if self.upload_error:
            raise RuntimeError('upload failed')
        return NS(id=43, chat_id=TARGET, media=object())


async def prepare_mirror(clean, monkeypatch, client):
    file_id = add(clean)
    async def session(*args):
        return client
    async def peer(*args, **kw):
        assert kw.get('dedicated') is True
        return TARGET
    async def resolve(client, value):
        return value
    async def source(*args):
        return client.source
    monkeypatch.setattr(media_cache, 'live_session', session)
    monkeypatch.setattr(media_cache, 'store_cache_peer', peer)
    monkeypatch.setattr(media_cache, '_resolve_peer', resolve)
    monkeypatch.setattr(media_cache, '_media_from', source)
    return file_id


@pytest.mark.asyncio
async def test_forward_first_without_disk_download(clean, monkeypatch):
    client = ArchiveClient()
    file_id = await prepare_mirror(clean, monkeypatch, client)
    result = await media_cache.ensure_mirror(db.file(file_id), clean, archive=True)
    assert client.events == ['forward']
    assert result['mirror_chat'] == TARGET and result['mirror_msg'] == 42
    assert result['chat_id'] == SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize('fail,cancel', [(False, False), (True, False), (False, True)])
async def test_restricted_fallback_preserves_metadata_and_cleans_files(clean, monkeypatch, fail, cancel):
    client = ArchiveClient(forward_ok=False, upload_error=fail, cancel=cancel)
    file_id = await prepare_mirror(clean, monkeypatch, client)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await media_cache.ensure_mirror(db.file(file_id), clean, archive=True)
    else:
        result = await media_cache.ensure_mirror(db.file(file_id), clean, archive=True)
        assert bool(result.get('mirror_msg')) is not fail
    assert client.events == ['forward', 'download', 'thumb', 'upload']
    assert all(attr in client.upload['attributes'] for attr in client.attrs)
    assert client.upload['mime_type'] == 'video/mp4'
    assert client.upload['force_document'] is False
    assert client.upload['supports_streaming'] is True
    assert all(not Path(p).exists() for p in client.paths)
    assert not media_cache._reserved_downloads
    assert media_cache.local_bytes(db.file(file_id)) is None  # archive isn't a web byte cache


@pytest.mark.asyncio
async def test_low_disk_never_downloads(clean, monkeypatch):
    client = ArchiveClient(forward_ok=False)
    file_id = await prepare_mirror(clean, monkeypatch, client)
    monkeypatch.setattr(media_cache.shutil, 'disk_usage', lambda p: NS(free=1))
    result = await media_cache.ensure_mirror(db.file(file_id), clean, archive=True)
    assert not result.get('mirror_msg')
    assert client.events == ['forward']
    assert 'ডিস্ক' in media_cache.mirror_problem(file_id)


@pytest.mark.asyncio
async def test_scan_enqueues_copy_even_when_rescan_has_no_new_files(clean, monkeypatch):
    first = add(clean)
    async def messages():
        yield NS(id=1, media=object(), video=NS(duration=12), file=NS(size=100), message='clip')
        yield NS(id=2, media=object(), photo=object(), message='photo')
    kicked = []
    monkeypatch.setattr(video_copy, 'kick', lambda: kicked.append(True))
    result = await scanner._run(ADMIN, messages(), 'Source', SOURCE, clean['id'], None, archive=True)
    assert result['copy_queued'] is True
    assert video_copy.summary(clean['id'])['total'] == 1
    assert item(clean, first)['state'] == 'pending'
    assert kicked == [True]


@pytest.mark.asyncio
async def test_session_scan_uses_resolved_peer(clean, monkeypatch):
    class Client:
        def iter_messages(self, peer, **kw):
            assert isinstance(peer, InputPeerChannel)
            async def empty():
                if False:
                    yield None
            return empty()
    clients = {ADMIN: Client()}
    monkeypatch.setattr(runtime, 'user_clients', clients)
    monkeypatch.setattr(scanner, 'user_clients', clients)
    async def resolve(*args):
        return InputPeerChannel(3075551195, 1234)
    monkeypatch.setattr(media_cache, '_resolve_peer', resolve)
    monkeypatch.setattr(video_copy, 'kick', lambda: None)
    result = await scanner.scan_with_session(ADMIN, SOURCE, 'Source', clean['id'])
    assert result['copy_queued']


@pytest.mark.asyncio
async def test_scan_finished_message_is_valid_and_mentions_automation(clean):
    from app.handlers.manage import _scan_finished
    class Status:
        async def edit(self, text, **kw):
            assert 'ভিডিও কপি' in text
    await _scan_finished(Status(), clean, {'found': 1, 'scanned': 1, 'copy_queued': True})


@pytest.mark.asyncio
async def test_public_delivery_error_never_exposes_telegram_details(clean, monkeypatch):
    from app.services import flow, sender
    seen = []
    async def send(chat, text):
        seen.append(text)
    monkeypatch.setattr(sender, 'send_text', send)
    await flow._delivery_error(ADMIN, sender.Delivery(False, sender.MISSING,
                               'source: Could not find the input entity for PeerChannel(3075551195)'), ADMIN)
    assert 'প্রস্তুত' in seen[0]
    assert 'PeerChannel' not in seen[0] and 'source:' not in seen[0]


@pytest.mark.asyncio
async def test_crashed_running_item_is_resumed(clean, monkeypatch):
    file_id = add(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    db._run("UPDATE video_copy_items SET state='running' WHERE file_id=?", (file_id,))
    async def mirror(row, store, **kw):
        db.set_mirror(file_id, TARGET, 99)
        return db.file(file_id)
    monkeypatch.setattr(media_cache, 'ensure_mirror', mirror)
    monkeypatch.setattr(video_copy, 'report', no_sleep)
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    await video_copy._run()
    assert item(clean, file_id)['state'] == 'done'


@pytest.mark.asyncio
async def test_source_resolution_loads_dialogs_before_getting_media(clean):
    message = NS(media=object())
    class Client:
        known = False
        async def get_input_entity(self, peer):
            if not self.known:
                raise ValueError('Could not find the input entity for PeerChannel')
            return InputPeerChannel(3075551195, 123)
        async def get_dialogs(self, **kw):
            self.known = True
            assert kw['limit'] is None
            return []
        async def get_messages(self, peer, **kw):
            assert isinstance(peer, InputPeerChannel)
            return message
    assert await media_cache._media_from(Client(), SOURCE, 1) is message


@pytest.mark.asyncio
async def test_ranged_download_is_byte_exact(clean, tmp_path):
    data = bytes(range(256)) * (90 * 1024)
    class Client:
        async def iter_download(self, media, offset=0, request_size=0):
            for pos in range(offset, len(data), request_size):
                yield data[pos:pos + request_size]
    target = tmp_path / 'video.mp4'
    await media_cache._download_ranged(Client(), NS(media=object()), str(target), len(data))
    assert target.read_bytes() == data


@pytest.mark.asyncio
async def test_partial_ranged_download_is_not_accepted(clean, tmp_path):
    class Client:
        async def iter_download(self, media, **kw):
            yield b'short'
    with pytest.raises(IOError, match='Incomplete'):
        await media_cache._download_ranged(Client(), NS(media=object()), str(tmp_path / 'bad.mp4'), 1000)


@pytest.mark.asyncio
async def test_deleted_pending_file_does_not_leave_job_stuck(clean, monkeypatch):
    file_id = add(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    db._run('DELETE FROM files WHERE id=?', (file_id,))
    monkeypatch.setattr(video_copy, 'report', no_sleep)
    await video_copy._run()
    assert item(clean, file_id)['state'] == 'failed'
    assert video_copy.job(clean['id'])['state'] == 'done'


def test_admin_panel_shows_copy_status_and_controls(clean, monkeypatch):
    from fastapi.testclient import TestClient
    from web import dashboard
    from tests.test_web import _auth
    monkeypatch.setattr(dashboard, 'ADMIN_LOGIN_PASSWORD', 'secret')
    monkeypatch.setattr(cfg, 'BOT_TOKEN', '123456789:' + 'A' * 35)
    monkeypatch.setattr(cfg, 'API_ID', 12345)
    monkeypatch.setattr(cfg, 'API_HASH', '0' * 32)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    client = TestClient(dashboard.app)
    response = client.get('/admin/channels', headers=_auth())
    assert response.status_code == 200 and 'স্ক্যানের পর ভিডিও কপি' in response.text
    assert f"copy/pause" in response.text
    response = client.post(f"/admin/channels/store/{clean['id']}/copy/pause", headers=_auth(), follow_redirects=False)
    assert response.status_code == 303
    assert video_copy.job(clean['id'])['state'] == 'paused'
    client.cookies.clear()
    response = client.post(f"/admin/channels/store/{clean['id']}/copy/stop", follow_redirects=False)
    assert video_copy.job(clean['id'])['state'] == 'paused'  # unauthenticated mutation denied


def test_database_lease_prevents_multiple_process_workers(clean, monkeypatch):
    assert video_copy._claim()
    monkeypatch.setattr(video_copy, '_owner', 'another-process')
    assert not video_copy._claim()
    db._run('UPDATE video_copy_lease SET until=0 WHERE id=1')
    assert video_copy._claim()


def test_extra_bot_can_wake_transfer_worker_with_shared_lease(clean, monkeypatch):
    monkeypatch.setenv('BOT_EXTRA', '1')
    spawned = []
    def spawn(coro):
        spawned.append(True)
        coro.close()
        return NS(done=lambda: False)
    monkeypatch.setattr(runtime, 'spawn', spawn)
    video_copy.kick()
    video_copy.kick()
    assert spawned == [True]


def test_legacy_shared_mirror_is_queued_for_store_specific_archive(clean):
    file_id = add(clean)
    db.set_mirror(file_id, TARGET - 100, 5)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    assert item(clean, file_id)['state'] == 'pending'


def test_full_backup_restores_copy_progress_and_controls(clean):
    from app.services import portable
    file_id = add(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    video_copy.control(clean['id'], 'pause')
    backup = portable.export_bytes(note='copy-test')
    video_copy.control(clean['id'], 'stop')
    result = portable.restore(backup, keep_safety=False)
    assert result['ok']
    assert video_copy.job(clean['id'])['state'] == 'paused'
    assert item(clean, file_id)['state'] == 'pending'
    assert db.file(file_id)['chat_id'] == SOURCE


def test_channel_reset_requeues_previously_completed_copy(clean):
    file_id = add(clean)
    db.set_mirror(file_id, TARGET, 9)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    assert item(clean, file_id)['state'] == 'done'
    db._run("UPDATE video_copy_jobs SET state='done' WHERE store_id=?", (clean['id'],))
    db.set_meta(f'store_cache_channel:{clean["id"]}', '')
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    assert item(clean, file_id)['state'] == 'pending'
    assert video_copy.job(clean['id'])['state'] == 'queued'


@pytest.mark.asyncio
async def test_archive_uses_dedicated_store_channel_even_with_global_override(clean):
    settings.set('STORAGE_CHANNEL', '@shared_archive')
    settings.set('PER_STORE_CHANNEL', False)
    assert await media_cache.store_cache_peer(object(), clean) is None
    assert await media_cache.store_cache_peer(object(), clean, dedicated=True) == TARGET
