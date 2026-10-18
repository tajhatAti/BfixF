"""Byte callbacks, automatic recovery and visible blocking reasons."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from app import config as cfg, runtime
from app.services import media_cache, scanner, settings, transfer_progress as progress, video_copy
from app.storage import db

ADMIN = 424242
SOURCE = -1003075551195
TARGET = -1004075551195


@pytest.fixture()
def clean(tmp_path, monkeypatch):
    db.bind(str(tmp_path / 'live.sqlite3'))
    settings._cache.clear()
    monkeypatch.setattr(cfg, 'ADMIN_IDS', [ADMIN])
    monkeypatch.setattr(runtime, 'user_clients', {})
    monkeypatch.setattr(video_copy, '_task', None)
    return db.create_store(ADMIN, 'Live uploads')


def video(store, n=1, scanned=True):
    return db.add_file(store['id'], f'Video {n}', 'Video', SOURCE, n,
                       code=f'FILE_{abs(SOURCE)}_{n}' if scanned else None)


def test_real_bytes_percent_rate_and_phase_eta_are_rendered(clean, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(progress.time, 'time', lambda: clock[0])
    tracker = progress.Tracker('store:1', 1, ADMIN, '<Video>')
    tracker.phase('download', 100 * 1048576)
    clock[0] += 10
    tracker.callback(50 * 1048576, 100 * 1048576)
    text = progress.render('store:1')
    assert '50.0 / 100.0 MiB' in text and '50.0%' in text
    assert '5.00 MiB/s' in text and '0মি 10সে' in text
    assert '&lt;Video&gt;' in text
    tracker.phase('upload', 100 * 1048576)
    row = db._one('SELECT * FROM transfer_progress')
    assert row['current'] == 0 and row['stage'] == 'upload'


def test_stalled_progress_never_pretends_to_be_active_speed(clean, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(progress.time, 'time', lambda: clock[0])
    tracker = progress.Tracker('store:1', 1, ADMIN, 'Video')
    tracker.phase('download', 100)
    clock[0] += 60
    assert 'নতুন byte progress আসেনি' in progress.render('store:1')


def test_owner_filter_and_database_restore_fence(clean, tmp_path):
    tracker = progress.Tracker('store:1', 1, ADMIN, 'private name')
    tracker.phase('upload')
    assert 'private name' not in progress.render(owner=ADMIN + 1)
    db.bind(str(tmp_path / 'restored.sqlite3'))
    tracker.callback(10, 100)
    assert not db._q('SELECT * FROM transfer_progress')


def test_backfill_recovers_old_scans_but_not_manual_uploads(clean):
    video(clean, 1)
    other = db.create_store(ADMIN, 'Manual uploads')
    video(other, 2, scanned=False)
    assert video_copy.backfill() == 1
    assert video_copy.summary(clean['id'])['total'] == 1
    assert not video_copy.job(other['id'])
    assert video_copy.backfill() == 0


def test_backfill_preserves_explicit_pause_and_stop(clean):
    video(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    video_copy.control(clean['id'], 'stop')
    video(clean, 2)
    video_copy.backfill()
    assert video_copy.job(clean['id'])['state'] == 'stopped'
    assert video_copy.summary(clean['id'])['total'] == 1


def test_backfill_uses_old_scan_audit_when_code_is_missing(clean):
    video(clean, scanned=False)
    db.log_event('scan', ADMIN, clean['id'], None, str(SOURCE), 'old scan')
    assert video_copy.backfill() == 1


@pytest.mark.asyncio
async def test_interrupted_scan_still_enqueues_saved_videos(clean, monkeypatch):
    async def messages():
        yield NS(id=1, media=object(), video=NS(duration=1), file=NS(size=100), message='video')
        raise OSError('connection lost on next page')
    kicked = []
    monkeypatch.setattr(video_copy, 'kick', lambda: kicked.append(True))
    with pytest.raises(OSError):
        await scanner._run(ADMIN, messages(), 'source', SOURCE, clean['id'], None, archive=True)
    assert video_copy.summary(clean['id'])['total'] == 1
    assert kicked == [True]


def test_waiting_reasons_show_session_pause_retry_and_target(clean, monkeypatch):
    fid = video(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    text = video_copy.describe(clean['id'])
    assert 'সেশন' in text and 'কিউতে' in text and 'চলছে' not in text
    monkeypatch.setattr(runtime, 'user_clients', {ADMIN: object()})
    db.set_meta(f'store_cache_channel:{clean["id"]}', str(TARGET))
    assert str(TARGET) in video_copy.describe(clean['id'])
    video_copy.control(clean['id'], 'pause')
    assert 'Pause' in video_copy.waiting_reason(clean['id'])
    video_copy.control(clean['id'], 'resume')
    db._run('UPDATE video_copy_items SET ready_at=? WHERE file_id=?', (progress.time.time() + 60, fid))
    assert 'retry' in video_copy.waiting_reason(clean['id'])


@pytest.mark.asyncio
async def test_pending_job_without_session_reports_why_instead_of_silence(clean, monkeypatch):
    video(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    reported = []
    async def report(store):
        reported.append(video_copy.waiting_reason(store))
    monkeypatch.setattr(video_copy, 'report', report)
    await video_copy._run()
    assert reported and 'সেশন' in reported[0]


@pytest.mark.asyncio
async def test_normal_download_passes_byte_callback(clean, tmp_path):
    class Client:
        async def download_media(self, message, file=None, progress_callback=None):
            from pathlib import Path
            assert progress_callback
            progress_callback(50, 100)
            Path(file).write_bytes(b'x' * 100)
            progress_callback(100, 100)
    tracker = progress.Tracker('store:1', 1, ADMIN, 'video')
    tracker.phase('download', 100)
    assert await media_cache._download(Client(), NS(), str(tmp_path / 'test.mp4'), 100, progress=tracker.callback)
    assert db._one('SELECT current FROM transfer_progress')['current'] == 100


@pytest.mark.asyncio
async def test_ranged_download_reports_aggregate_not_per_connection(clean, tmp_path):
    data = b'x' * (21 * 1048576)
    class Client:
        async def iter_download(self, media, offset=0, request_size=0):
            for n in range(offset, len(data), request_size):
                yield data[n:n+request_size]
    seen = []
    await media_cache._download_ranged(Client(), NS(media=object()), str(tmp_path / 'v.mp4'), len(data),
                                      progress=lambda current,total: seen.append((current,total)))
    assert seen[-1] == (len(data), len(data))
    assert all(seen[i][0] <= seen[i+1][0] for i in range(len(seen)-1))


@pytest.mark.asyncio
async def test_progress_option_is_admin_only(clean, monkeypatch):
    from app.handlers import manage
    class Event:
        sender_id = ADMIN + 1
        async def respond(self, *args, **kw):
            pytest.fail('non-admin saw private progress')
        async def answer(self, text, **kw):
            assert 'অ্যাডমিন' in text
    await manage.show_downloads(Event())
    await manage.downloads_panel(Event(), '')


@pytest.mark.asyncio
async def test_ui_view_follows_current_callback_message_for_auto_updates(clean, monkeypatch):
    from app.handlers import manage
    video(clean)
    video_copy.enqueue(clean['id'], ADMIN, SOURCE)
    class Event:
        sender_id = ADMIN
        chat_id = ADMIN
        message_id = 123
        async def answer(self, *args, **kw):
            pass
        async def edit(self, text, **kw):
            assert 'Live uploads' in text and 'সেশন' in text
    await manage.video_copy_control(Event(), f'view:{clean["id"]}')
    assert video_copy.job(clean['id'])['message_id'] == 123


@pytest.mark.asyncio
async def test_busy_legacy_warmup_does_not_block_queue_wakeups(clean, monkeypatch):
    from app.services import scheduler, restricted
    original_sleep = asyncio.sleep
    ticks, spawned = [], []
    gate = asyncio.Event()
    async def slow_warm(**kwargs):
        await gate.wait()
        return {'mirrored': 0}
    def spawn(coro):
        task = asyncio.create_task(coro)
        spawned.append(task)
        return task
    count = 0
    async def tick_sleep(seconds):
        nonlocal count
        count += 1
        if count >= 3:
            raise asyncio.CancelledError()
        await original_sleep(0)
    monkeypatch.setattr(media_cache, 'warm_mirrors', slow_warm)
    monkeypatch.setattr(video_copy, 'kick', lambda: ticks.append('copy'))
    monkeypatch.setattr(restricted, 'kick', lambda: ticks.append('post'))
    monkeypatch.setattr(runtime, 'spawn', spawn)
    monkeypatch.setattr(asyncio, 'sleep', tick_sleep)
    video(clean)
    try:
        with pytest.raises(asyncio.CancelledError):
            await scheduler.mirror_loop()
        assert ticks.count('copy') == 3  # warmup is still blocked, yet ticks continue
        assert len(spawned) == 1
    finally:
        for task in spawned:
            task.cancel()
        await asyncio.gather(*spawned, return_exceptions=True)


@pytest.mark.asyncio
async def test_scheduler_starts_without_waiting_for_cache_bootstrap(clean, monkeypatch):
    import bot
    monkeypatch.delenv('BOT_EXTRA', raising=False)
    monkeypatch.setattr(runtime, 'bot_id', runtime.bot_id)
    monkeypatch.setattr(runtime, 'bot_username', runtime.bot_username)
    order = []
    class Client:
        async def get_me(self):
            return NS(id=10, username='test_main')
    async def seed():
        order.append('sessions')
    async def prepare():
        pytest.fail('startup blocked waiting for channel bootstrap')
    def spawn(coro):
        order.append('background-menu' if coro.cr_code.co_name == 'announce_and_install' else 'background-prepare')
        coro.close()
    monkeypatch.setattr(bot, 'seed_and_reconnect_sessions', seed)
    monkeypatch.setattr(bot, 'prepare_media_cache', prepare)
    monkeypatch.setattr(bot.scheduler, 'start_all', lambda: order.append('scheduler'))
    monkeypatch.setattr(runtime, 'spawn', spawn)
    await bot.on_startup(Client())
    assert order == ['background-menu', 'sessions', 'scheduler', 'background-prepare']


def test_active_worker_keeps_heartbeat_when_kicked_again(clean, monkeypatch):
    monkeypatch.setattr(video_copy, '_task', NS(done=lambda: False))
    video_copy.kick()
    assert float(db.get_meta('video_copy_last_tick')) > 0


@pytest.mark.asyncio
async def test_scan_to_download_to_upload_starts_without_button_and_records_bytes(clean, monkeypatch):
    from pathlib import Path
    from telethon.tl.types import DocumentAttributeVideo
    source = NS(id=1, media=object(), video=NS(duration=12, w=320, h=240),
                file=NS(size=100, mime_type='video/mp4'),
                document=NS(attributes=[DocumentAttributeVideo(duration=12,w=320,h=240)], mime_type='video/mp4'),
                message='actual scanned clip')
    stages = []
    class Session:
        async def forward_messages(self, *args, **kwargs):
            raise RuntimeError('CHAT_FORWARDS_RESTRICTED')
        async def download_media(self, message, file=None, thumb=None, progress_callback=None):
            Path(file).write_bytes(b'x' * (1 if thumb is not None else 100))
            if progress_callback:
                progress_callback(50, 100)
                assert db._one("SELECT stage FROM transfer_progress")['stage'] == 'download'
                progress_callback(100, 100)
                stages.append('downloaded')
            return file
        async def send_file(self, target, path, progress_callback=None, **kw):
            assert target == TARGET
            assert kw['mime_type'] == 'video/mp4' and kw['force_document'] is False
            assert db._one('SELECT stage FROM transfer_progress')['stage'] == 'upload'
            progress_callback(50, 100)
            progress_callback(100, 100)
            stages.append('uploaded')
            return NS(id=50, media=object(), chat_id=TARGET)
    client = Session()
    monkeypatch.setattr(runtime, 'user_clients', {ADMIN: client})
    async def session(*args):
        return client
    async def target(*args, **kwargs):
        db.set_meta(f'store_cache_channel:{clean["id"]}', str(TARGET))
        return TARGET
    async def resolve(client, value):
        return value
    async def fetch(*args):
        return source
    async def report(*args):
        pass
    monkeypatch.setattr(media_cache, 'live_session', session)
    monkeypatch.setattr(media_cache, 'store_cache_peer', target)
    monkeypatch.setattr(media_cache, '_resolve_peer', resolve)
    monkeypatch.setattr(media_cache, '_media_from', fetch)
    monkeypatch.setattr(video_copy, 'report', report)
    async def iterator():
        yield source
    result = await scanner._run(ADMIN, iterator(), 'channel', SOURCE, clean['id'], None, archive=True)
    assert result['copy_queued'] is True
    await video_copy._task  # scanner kicked it itself; no Start/Resume call
    assert stages == ['downloaded', 'uploaded']
    assert video_copy.summary(clean['id'])['counts'] == {'done': 1}
    assert db._one('SELECT stage FROM transfer_progress')['stage'] == 'done'
