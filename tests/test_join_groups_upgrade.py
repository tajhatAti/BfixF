"""Regression coverage for real membership calls, gates and group promotion."""
import asyncio
from types import SimpleNamespace as NS
import pytest
from telethon import events
from telethon.errors import UserNotParticipantError, ChatAdminRequiredError
from telethon.tl import types
from app import config as cfg, runtime
from app.handlers import groups, join_gate
from app.services import forcejoin, flow, settings, linkopts, sender, feed, error_reports
from app.storage import db

OWNER, USER = 424242, 999


@pytest.fixture
def clean(tmp_path, monkeypatch):
    db.bind(str(tmp_path / 'groups.sqlite3'))
    settings._cache.clear()
    forcejoin.clear_cache()
    groups._locks.clear()
    monkeypatch.setattr(cfg, 'ADMIN_IDS', [OWNER])
    monkeypatch.setattr(runtime, 'bot_username', 'MyDesiBot')
    old = runtime.get_client()
    yield
    runtime.set_client(old)


class Event:
    sender_id = USER
    chat_id = -10012345
    is_group = True
    is_private = False
    raw_text = '/start'
    out = False
    data = b''
    def __init__(self):
        self.replies = []
        self.answers = []
    async def respond(self, text, **kw):
        self.replies.append((text, kw))
    async def answer(self, text='', **kw):
        self.answers.append(text)
    async def get_sender(self):
        return NS(first_name='Visitor', username='visitor')


class MemberClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []
    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize('joined', [True, False])
async def test_membership_uses_callable_client_not_coroutine(clean, joined):
    client = MemberClient(NS(participant=NS(user_id=USER)) if joined else UserNotParticipantError(None))
    assert await forcejoin._member_of(client, -100123, USER) is joined
    assert len(client.requests) == 1
    assert client.requests[0].participant == USER


@pytest.mark.parametrize('outcome', [None, NS(), ChatAdminRequiredError(None)])
async def test_uncertain_membership_never_counts_as_joined(clean, outcome):
    client = MemberClient(outcome)
    assert await forcejoin._member_of(client, -100123, USER) is None


async def test_required_channel_resolution_failure_blocks(clean, monkeypatch):
    settings.set('FORCE_CHANNEL', '@required_channel')
    runtime.set_client(MemberClient(None))
    async def unresolved(target):
        return {**target, 'ok': False, 'error': 'test'}
    monkeypatch.setattr(forcejoin, 'ensure_target', unresolved)
    result = await forcejoin.check_many(USER)
    assert result['ok'] is False and result['skipped'] == 1
    assert result['missing'][0]['unverified']


async def test_disconnected_bot_cannot_bypass_channel(clean):
    settings.set('FORCE_CHANNEL', '@required_channel')
    runtime.set_client(None)
    result = await forcejoin.check_many(USER)
    assert result['missing'] and not result['ok']


async def test_link_specific_channel_unknown_is_blocked(clean, monkeypatch):
    monkeypatch.setattr(linkopts, 'targets', lambda token: [{'chat_id': -1001, 'ref': '@required'}])
    runtime.set_client(MemberClient(None))
    assert await linkopts.missing_channels(USER, 'token')


async def missing(user_id, store_id=None):
    return {'ok': False, 'missing': [{'ref': '@required', 'title': 'Required'}], 'checked': 1, 'skipped': 0}


async def joined(user_id, store_id=None):
    return {'ok': True, 'missing': [], 'checked': 1, 'skipped': 0}


async def test_private_start_saves_payload_and_blocks_before_home(clean, monkeypatch):
    monkeypatch.setattr(flow, 'gate_check', missing)
    event = Event()
    event.is_private = True
    event.chat_id = USER
    event.raw_text = '/start f123'
    with pytest.raises(events.StopPropagation):
        await join_gate.global_message_gate(event)
    assert db.get_meta(f'join_start:{USER}') == 'f123'
    assert event.replies and 'চ্যানেল' in event.replies[0][0]


async def test_private_join_retry_is_owner_bound(clean, monkeypatch):
    event = Event()
    await join_gate.resume_start(event, str(OWNER))
    assert not event.replies and 'আপনার জন্য নয়' in event.answers[0]


async def test_private_join_retry_resumes_original_payload(clean, monkeypatch):
    from app.handlers import user
    monkeypatch.setattr(flow, 'gate_check', joined)
    seen = []
    async def handle(event, uid, payload, is_new):
        seen.append((uid, payload))
        return True
    monkeypatch.setattr(user, 'handle_payload', handle)
    db.set_meta(f'join_start:{USER}', 'f123')
    event = Event()
    event.chat_id = USER
    event.is_private = True
    await join_gate.resume_start(event, str(USER))
    assert seen == [(USER, 'f123')]
    assert db.get_meta(f'join_start:{USER}') == ''


async def test_group_gate_never_sends_video_before_join(clean, monkeypatch):
    monkeypatch.setattr(flow, 'gate_check', missing)
    async def never(*args, **kwargs):
        pytest.fail('video leaked before join')
    monkeypatch.setattr(sender, 'deliver', never)
    event = Event()
    await groups.promote(event, USER)
    assert 'চ্যানেল' in event.replies[0][0]


async def test_group_rotates_only_free_unlocked_videos_without_captions(clean, monkeypatch):
    monkeypatch.setattr(flow, 'gate_check', joined)
    free = db.create_store(OWNER, 'Free')
    db.update_store(free['id'], is_premium=0)
    paid = db.create_store(OWNER, 'Paid')
    db.update_store(paid['id'], is_premium=1)
    first = db.add_file(free['id'], 'First', 'Video', -100, 1)
    second = db.add_file(free['id'], 'Second', 'Video', -100, 2)
    db.add_file(paid['id'], 'Premium', 'Video', -100, 3)
    locked = db.add_file(free['id'], 'Locked', 'Video', -100, 4)
    db._run('UPDATE files SET locked=1 WHERE id=?', (locked,))
    db.add_file(free['id'], 'Photo', 'Photo', -100, 5)
    sent = []
    async def deliver(chat, row, caption_text=None, buttons=None):
        sent.append((chat, row['id'], caption_text, buttons))
        return sender.Delivery(True)
    monkeypatch.setattr(sender, 'deliver', deliver)
    event = Event()
    await groups.promote(event, USER)
    await groups.promote(event, USER)  # cooldown: no duplicate send
    assert len(sent) == 1
    db.set_meta(f'group_promo_time:{event.chat_id}', '0')
    await groups.promote(event, USER)
    assert {item[1] for item in sent} == {first, second}
    assert all(item[2] == '' for item in sent)
    urls = [getattr(button, 'url', None) or button.type.url for row in sent[0][3] for button in row]
    assert any('startgroup=promo' in url for url in urls)
    assert any('?start=f' in url for url in urls)


async def test_group_never_bypasses_store_specific_join(clean, monkeypatch):
    free = db.create_store(OWNER, 'Scoped')
    db.update_store(free['id'], is_premium=0)
    db.add_file(free['id'], 'Private gate', 'Video', -100, 1)
    async def gate(uid, sid=None):
        return await (missing(uid, sid) if sid else joined(uid))
    monkeypatch.setattr(flow, 'gate_check', gate)
    event = Event()
    await groups.promote(event, USER)
    assert 'চ্যানেল' in event.replies[0][0]


async def test_public_range_download_uses_source_session_not_bot(clean, monkeypatch):
    calls = []
    class Session:
        async def iter_download(self, media, **kwargs):
            calls.append(kwargs['offset'])
            yield b'0123456789'
    class WrongClient:
        def iter_download(self, *args, **kwargs):
            pytest.fail('bot cannot read the session-owned source')
    monkeypatch.setattr(feed, 'bot', WrongClient())
    msg = NS(media='video', _client=Session())
    data = b''.join([chunk async for chunk in feed.iter_range(msg, 3, 6)])
    assert data == b'3456' and calls == [0]


def test_database_passwords_redacted_from_alerts():
    redacted = error_reports.redact('postgresql://someone:password-do-not-log@db.example/db')
    assert 'password-do-not-log' not in redacted


def test_no_caption_and_no_forward_author_for_new_archives():
    from pathlib import Path
    source = Path('app/services/media_cache.py').read_text()
    assert 'drop_author=True, drop_media_captions=True' in source
    assert 'caption=clean_text(file_row.get("name")' not in source


def test_dark_mobile_theme_covers_legacy_setup_forms():
    from web import adminui, dashboard
    assert '--accent:#facc15' in adminui.THEME_CSS
    assert 'font-size:16px; min-height:44px' in adminui.THEME_CSS
    assert '.wiz, .wiz .head, .wiz .box' in adminui.THEME_CSS
    assert 'only an ended video advances' in dashboard._FEED_JS
    assert 'slide.dataset.asked = \'1\'' in dashboard._FEED_JS


def test_browser_autoplay_rejection_offers_a_real_tap_to_play():
    import json, shutil, subprocess
    from web import dashboard
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    helper = dashboard._PLAYER_HELPER_JS.replace('<script>', '').replace('</script>', '')
    script = '''
const assert = require('assert');
global.window = {};
let prompt = null, listener = null;
global.document = {createElement: () => ({
  addEventListener: (name, cb) => { listener = cb; },
  remove: () => { prompt = null; }
})};
const parent = {querySelector: () => prompt, appendChild: item => {prompt = item;}};
let attempts = 0;
const video = {parentElement: parent, play: () => {
  attempts++;
  return attempts === 1 ? Promise.reject({name:'NotAllowedError'}) : Promise.resolve();
}};
''' + helper + '''
(async () => {
  assert.strictEqual(await window.bfixfPlay(video), false);
  assert(prompt.textContent.includes('ট্যাপ'));
  listener({preventDefault(){}, stopPropagation(){}});
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(prompt, null);
  assert.strictEqual(video.muted, true);
  assert.strictEqual(attempts, 2);
})().catch(e => {console.error(e); process.exitCode=1;});
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)


async def test_group_join_retry_is_owner_bound_and_resumes(clean, monkeypatch):
    calls = []
    async def promote(event, uid, store_id=None):
        calls.append((uid, store_id))
    monkeypatch.setattr(groups, 'promote', promote)
    event = Event()
    await groups.group_join_retry(event, f'{OWNER}:12')
    assert calls == []
    await groups.group_join_retry(event, f'{USER}:12')
    assert calls == [(USER, 12)]


async def test_public_thumb_uses_message_source_client(clean, monkeypatch):
    class Client:
        async def download_media(self, msg, **kw):
            return b'jpeg'
    async def message(row):
        return NS(_client=Client())
    monkeypatch.setattr(feed, 'message_for', message)
    monkeypatch.setattr(feed, '_thumb_cache', {})
    monkeypatch.setattr(feed, '_read_disk', lambda key: None)
    monkeypatch.setattr(feed, '_write_disk', lambda *a: None)
    assert await feed.thumb_for({'id': 1}) == b'jpeg'


def test_locked_public_file_cannot_be_streamed(clean):
    store = db.create_store(OWNER, 'Free')
    db.update_store(store['id'], is_premium=0)
    fid = db.add_file(store['id'], 'Video', 'Video', -100, 1)
    db._run('UPDATE files SET locked=1 WHERE id=?', (fid,))
    assert not feed.eligible(db.file(fid))
