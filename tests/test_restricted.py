"""Native video scan and isolated admin post downloader (offline Telegram mocks)."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

from app import config as cfg, runtime
from app.services import media_cache, native_media, restricted, scanner, settings, video_copy
from app.storage import db

ADMIN = 424242
TARGET = -1004000000011


@pytest.fixture()
def clean(tmp_path, monkeypatch):
    db.bind(str(tmp_path / 'restricted.sqlite3'))
    settings._cache.clear()
    monkeypatch.setattr(cfg, 'ADMIN_IDS', [ADMIN])
    monkeypatch.setattr(runtime, 'user_clients', {})
    monkeypatch.setattr(restricted, '_task', None)
    monkeypatch.setattr(media_cache, '_reserved_downloads', {})
    db._run('INSERT INTO restricted_channels(chat_id,admin_id,link) VALUES(?,?,?)',
            (TARGET, ADMIN, 'https://t.me/+testInvite'))


def video(n=1, group=None):
    attrs = [DocumentAttributeFilename('unhelpful.bin'),
             DocumentAttributeVideo(duration=12, w=640, h=360, supports_streaming=False)]
    return NS(id=n, media=object(), video=attrs[1], document=NS(attributes=attrs, mime_type='video/mp4'),
              file=NS(size=100, mime_type='video/mp4'), grouped_id=group, message='private caption')


def photo(n=2, group=None):
    return NS(id=n, media=object(), photo=object(), document=None, file=NS(size=50), grouped_id=group)


@pytest.mark.parametrize('link,peer,mid', [
    ('https://t.me/example_channel/123', '@example_channel', 123),
    ('https://t.me/c/3075551195/45', -1003075551195, 45),
    ('t.me/example_channel/12?single', '@example_channel', 12),
    ('https://t.me/s/example_channel/12', '@example_channel', 12),
])
def test_link_parser(link, peer, mid):
    assert restricted.parse_link(link) == (peer, mid)


@pytest.mark.parametrize('link', ['https://evil.com/name/1', 'https://t.me.evil.com/name/1',
                                  'https://t.me/+invite', 'https://t.me/name/0',
                                  'https://t.me/name/1/2', 'https://t.me/user@evil/123'])
def test_link_parser_rejects_nonpost_urls(link):
    with pytest.raises(ValueError):
        restricted.parse_link(link)


def test_scan_only_accepts_videos(clean):
    assert scanner._kind_of(video()) == 'Video'
    assert scanner._kind_of(photo()) is None
    assert scanner._kind_of(NS(media=None)) is None


def test_video_upload_has_real_filename_mime_and_streaming_attributes(clean):
    message = video()
    opts = native_media.upload_options(message)
    assert opts['force_document'] is False
    assert opts['mime_type'] == 'video/mp4'
    assert opts['supports_streaming'] is True
    names = [a.file_name for a in opts['attributes'] if isinstance(a, DocumentAttributeFilename)]
    assert names == ['video-1.mp4']
    attr = next(a for a in opts['attributes'] if isinstance(a, DocumentAttributeVideo))
    assert (attr.duration, attr.w, attr.h, attr.supports_streaming) == (12, 640, 360, True)
    assert message.document.attributes[1].supports_streaming is False  # no mutation of source
    assert native_media.extension(message) == '.mp4'


def test_photo_is_not_uploaded_as_document(clean):
    assert native_media.upload_options(photo()) == {'force_document': False}
    assert native_media.extension(photo()) == '.jpg'


def test_queue_deduplicates_channel_updates_and_ignores_disabled_channels(clean):
    assert restricted.enqueue(TARGET, 50, 'https://t.me/example/10')
    assert not restricted.enqueue(TARGET, 50, 'https://t.me/example/10')
    assert not restricted.enqueue(TARGET - 1, 50, 'https://t.me/example/10')
    db._run('UPDATE restricted_channels SET enabled=0')
    assert not restricted.enqueue(TARGET, 51, 'https://t.me/example/10')


@pytest.mark.asyncio
async def test_link_to_middle_of_album_includes_all_items_not_other_posts(clean):
    messages = [photo(10, 99), video(11, 99), photo(12, 99), video(13, 100)]
    class Client:
        async def get_messages(self, peer, ids):
            return messages[1] if isinstance(ids, int) else messages
    result = await restricted.album(Client(), 'peer', 11)
    assert [m.id for m in result] == [10, 11, 12]


class TransferClient:
    def __init__(self, fail=False, cancel=False):
        self.paths = []
        self.sent = []
        self.fail, self.cancel = fail, cancel
    async def download_media(self, message, file=None, thumb=None, progress_callback=None):
        self.paths.append(file)
        Path(file).write_bytes(b'x' * (10 if thumb is not None else message.file.size))
        return file
    async def send_file(self, peer, path, **kwargs):
        self.sent.append(kwargs)
        assert Path(path).exists()
        if self.cancel:
            raise asyncio.CancelledError()
        if self.fail:
            raise RuntimeError('upload error')
        return NS(id=100, media=object())


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['video', 'photo'])
async def test_transfer_is_captionless_native_media_and_cleans_download(clean, kind):
    client = TransferClient()
    message = video() if kind == 'video' else photo()
    sent = await restricted.transfer(client, 'target', message, TARGET)
    assert sent.id == 100
    opts = client.sent[0]
    assert opts['caption'] == '' and opts['force_document'] is False
    assert 'protect_content' not in opts and 'noforwards' not in opts
    assert all(not Path(p).exists() for p in client.paths)
    assert not media_cache._reserved_downloads


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_failed_or_cancelled_upload_also_removes_all_files(clean, cancel):
    client = TransferClient(fail=not cancel, cancel=cancel)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await restricted.transfer(client, 'target', video(), TARGET)
    assert all(not Path(p).exists() for p in client.paths)
    assert not media_cache._reserved_downloads


@pytest.mark.asyncio
async def test_album_transfers_have_three_slots_and_checkpoint_partial_success(clean, monkeypatch):
    restricted.enqueue(TARGET, 50, 'https://t.me/example/10')
    job = dict(db._one('SELECT * FROM restricted_jobs'))
    monkeypatch.setattr(runtime, 'user_clients', {ADMIN: object()})
    messages = [video(n, 99) for n in range(10, 18)]
    async def resolve(*args):
        return 'peer'
    async def album(*args):
        return messages
    async def send_message(*args, **kw):
        pass
    active = peak = 0
    calls = []
    async def transfer(client, target, message, chat):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(message.id)
        await asyncio.sleep(0.001)
        active -= 1
        if message.id == 12 and calls.count(12) == 1:
            raise RuntimeError('temporary upload failure')
        return NS(id=message.id + 100)
    monkeypatch.setattr(media_cache, '_resolve_peer', resolve)
    monkeypatch.setattr(restricted, 'album', album)
    monkeypatch.setattr(restricted, 'transfer', transfer)
    monkeypatch.setattr(runtime.bot, 'send_message', send_message)
    await restricted.process(job)
    assert peak == 3
    assert db._one('SELECT state FROM restricted_jobs')['state'] == 'pending'
    assert len(db._q('SELECT * FROM restricted_items WHERE sent_msg!=0')) == 7
    await restricted.process(dict(db._one('SELECT * FROM restricted_jobs')))
    assert db._one('SELECT state FROM restricted_jobs')['state'] == 'done'
    assert len(calls) == 9  # successful media are not uploaded again


def test_store_and_restricted_workers_share_global_three_slot_lease(clean):
    assert restricted._claim()
    assert not video_copy._claim()


@pytest.mark.asyncio
async def test_nonadmin_cannot_open_option_or_inject_channel_request(clean):
    from app.handlers import restricted as handler
    class Event:
        sender_id = ADMIN + 1
        chat_id = TARGET
        id = 80
        raw_text = 'https://t.me/example/10'
        async def answer(self, text, **kw):
            assert 'অ্যাডমিন' in text
        async def respond(self, *args, **kw):
            pytest.fail('nonadmin triggered downloader')
    await handler.callback(Event(), 'on')
    await handler.channel_link(Event())
    assert not db._q('SELECT * FROM restricted_jobs')


@pytest.mark.asyncio
async def test_disabled_channel_ignores_incoming_links(clean):
    from app.handlers import restricted as handler
    db._run('UPDATE restricted_channels SET enabled=0')
    await handler.channel_link(NS(chat_id=TARGET, sender_id=ADMIN))
    assert not db._q('SELECT * FROM restricted_jobs')


@pytest.mark.asyncio
async def test_channel_setup_does_not_touch_store_protection(clean, monkeypatch):
    from telethon.tl.functions.messages import ToggleNoForwardsRequest
    db._run('DELETE FROM restricted_channels')
    calls = []
    class Client:
        async def __call__(self, request):
            calls.append(request)
            return NS(link='https://t.me/+created')
    monkeypatch.setattr(runtime, 'user_clients', {ADMIN: Client()})
    async def create(*args, **kwargs):
        assert 'Restricted download' in args[1]
        return TARGET
    async def resolve(*args):
        return TARGET
    monkeypatch.setattr(media_cache, '_create_cache_channel', create)
    monkeypatch.setattr(media_cache, '_resolve_peer', resolve)
    info = await restricted.setup(ADMIN)
    changes = [r for r in calls if isinstance(r, ToggleNoForwardsRequest)]
    assert len(changes) == 1 and changes[0].peer == TARGET and changes[0].enabled is False
    assert info['link'] == 'https://t.me/+created'


def test_retry_is_owner_scoped_and_keeps_sent_media_checkpoints(clean):
    restricted.enqueue(TARGET, 10, 'https://t.me/example/10')
    job = db._one('SELECT id FROM restricted_jobs')['id']
    db._run("UPDATE restricted_jobs SET state='failed',attempts=3")
    db._run('INSERT INTO restricted_items(job_id,source_msg,sent_msg) VALUES(?,?,?)', (job, 10, 100))
    assert restricted.retry(ADMIN + 1) == 0
    assert restricted.retry(ADMIN) == 1
    assert db._one('SELECT sent_msg FROM restricted_items')['sent_msg'] == 100
    assert db._one('SELECT state FROM restricted_jobs')['state'] == 'pending'


def test_both_scanner_paths_use_telegram_video_filter():
    import inspect
    assert 'InputMessagesFilterVideo' in inspect.getsource(scanner.scan_with_session)
    assert 'InputMessagesFilterVideo' in inspect.getsource(scanner.scan_with_bot)


def test_offline_unavailable_source_never_bypasses_channel_registration(clean):
    assert restricted.channel(TARGET - 1) is None
    assert not restricted.enqueue(TARGET - 1, 1, 'https://t.me/example/10')


@pytest.mark.asyncio
async def test_authorized_channel_link_is_queued_once(clean, monkeypatch):
    from telethon import events
    from app.handlers import restricted as handler
    kicked = []
    monkeypatch.setattr(restricted, 'kick', lambda: kicked.append(True))
    class Event:
        sender_id = TARGET  # broadcast channel-as-sender
        chat_id = TARGET
        id = 90
        raw_text = 'https://t.me/example/10'
        async def respond(self, text):
            assert 'লিংক নেওয়া হয়েছে' in text
    for _ in range(2):
        with pytest.raises(events.StopPropagation):
            await handler.channel_link(Event())
    assert len(db._q('SELECT * FROM restricted_jobs')) == 1
    assert kicked == [True]


@pytest.mark.asyncio
async def test_absent_session_waits_without_consuming_attempt(clean):
    restricted.enqueue(TARGET, 90, 'https://t.me/example/10')
    await restricted.process(dict(db._one('SELECT * FROM restricted_jobs')))
    row = db._one('SELECT * FROM restricted_jobs')
    assert row['state'] == 'pending' and row['attempts'] == 0


def test_full_backup_preserves_registered_channel_and_successful_items(clean):
    from app.services import portable
    restricted.enqueue(TARGET, 90, 'https://t.me/example/10')
    job_id = db._one('SELECT id FROM restricted_jobs')['id']
    db._run('INSERT INTO restricted_items(job_id,source_msg,sent_msg) VALUES(?,?,?)', (job_id, 10, 500))
    data = portable.export_bytes(note='restricted checkpoint')
    db._run('DELETE FROM restricted_items')
    portable.restore(data, keep_safety=False)
    assert restricted.channel(TARGET)['admin_id'] == ADMIN
    assert db._one('SELECT sent_msg FROM restricted_items')['sent_msg'] == 500
