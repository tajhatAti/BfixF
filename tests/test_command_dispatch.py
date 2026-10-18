"""Dispatch through real Telethon event filters on a freshly bound client.

Unlike direct handler unit tests this verifies import caching, registration order,
StopPropagation and pending conversations together, with no Telegram network.
"""
import asyncio
import inspect
from types import SimpleNamespace as NS

import pytest
from telethon import events

from app import config as cfg, runtime
from app.handler_loader import load_handlers, HANDLER_MODULES
from app.handlers import state
from app.services import settings, command_catalog, error_reports
from app.storage import db

OWNER = 424242


class Client:
    def __init__(self):
        self.handlers = []
        self.messages = []
    def on(self, builder):
        def wrap(callback):
            self.handlers.append((callback, builder))
            return callback
        return wrap
    def remove_event_handler(self, callback):
        self.handlers = [(cb,b) for cb,b in self.handlers if cb != callback]
    async def send_message(self, chat, text, **kwargs):
        self.messages.append((chat,text))
        return NS(id=1)
    async def edit_message(self, *args, **kwargs):
        pass
    def is_connected(self):
        return True


class Event:
    def __init__(self, text, sender=OWNER):
        self.sender_id = self.chat_id = sender
        self.is_private, self.out, self.media = True, False, None
        self.message = NS(message=text, out=False, fwd_from=None, media=None,
                          document=None, sender_id=sender, id=10)
        self.replies = []
        self.pattern_match = None
        self.id = self.message_id = 10
    @property
    def raw_text(self):
        return self.message.message
    async def respond(self, text, **kwargs):
        self.replies.append(text)
    async def get_sender(self):
        return NS(id=self.sender_id, first_name='Admin', username='tester', access_hash=123)


async def dispatch(client, event):
    for callback, builder in list(client.handlers):
        if not isinstance(builder, events.NewMessage):
            continue
        builder.resolved = True
        passed = builder.filter(event)
        if inspect.isawaitable(passed):
            passed = await passed
        if not passed:
            continue
        try:
            await callback(event)
        except events.StopPropagation:
            break
    await asyncio.sleep(0)
    return '\n'.join(event.replies)


@pytest.fixture()
async def client(tmp_path, monkeypatch):
    db.bind(str(tmp_path / 'dispatch.sqlite3'))
    settings._cache.clear()
    old = runtime.get_client()
    monkeypatch.setattr(cfg, 'ADMIN_IDS', [OWNER])
    monkeypatch.setattr(cfg, 'BOT_TOKEN', '123456789:' + 'A' * 35)
    monkeypatch.setattr(cfg, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(runtime, 'bot_username', 'MyDesiBot')
    monkeypatch.setattr(runtime, 'bot_id', 123)
    client = Client()
    load_handlers(client)
    existing = set(runtime.background_tasks)
    try:
        yield client
    finally:
        state.clear(OWNER)
        for task in runtime.background_tasks - existing:
            task.cancel()
        await asyncio.gather(*(runtime.background_tasks - existing), return_exceptions=True)
        runtime.set_client(old)


@pytest.mark.parametrize('prompt', ['bc_media', 'reel_add', 'bots_wait_token', 'session_input'])
async def test_help_escapes_every_pending_prompt_on_new_client(client, prompt):
    state.ask(OWNER, prompt)
    text = await dispatch(client, Event('/help'))
    assert '/downloads' in text and '/restricted' in text and '/brsend' in text
    assert 'Admin quick help' not in text
    assert state.peek(OWNER) is None


@pytest.mark.parametrize('command,expected', [
    ('/downloads', 'লাইভ অবস্থা'), ('/restricted', 'Restricted download'),
    ('/reel', 'রিল'), ('/session@MyDesiBot', 'String Session'),
    ('/version', 'desi-2026.10.18-group-join-recovery'), ('/whoami', str(OWNER)),
    ('/status', 'Handlers:'), ('/bots', 'বটগুলো'), ('/brhelp', '/brsend'),
])
async def test_commands_really_dispatch_after_cached_imports(client, command, expected):
    state.ask(OWNER, 'bc_media')
    text = await dispatch(client, Event(command))
    assert expected in text
    assert 'কমান্ডটি চালানো হয়নি' not in text


async def test_public_help_and_denied_commands_do_not_leak_catalog(client):
    text = await dispatch(client, Event('/help', sender=999))
    assert '/restricted' not in text and '/brsend' not in text
    text = await dispatch(client, Event('/restricted', sender=999))
    assert 'অনুমতি নেই' in text and 'Restricted download' not in text
    text = await dispatch(client, Event('/whoami', sender=999))
    assert '999' in text and 'অ্যাডমিন অনুমতি: নেই' in text


async def test_rebinding_does_not_duplicate_or_lose_handlers(client):
    count = len(client.handlers)
    load_handlers(client)
    assert len(client.handlers) == count
    other = Client()
    load_handlers(other)
    assert len(other.handlers) == count
    text = await dispatch(other, Event('/help'))
    assert text.count('/downloads') == 1


async def test_foreign_bot_suffix_is_ignored_and_unknown_command_answers(client):
    assert await dispatch(client, Event('/help@AnotherBot')) == ''
    text = await dispatch(client, Event('/not_a_real_command'))
    assert 'কমান্ডটি চালানো হয়নি' in text and '/help' in text


async def test_errors_are_owner_only_even_for_other_admin(client, monkeypatch):
    monkeypatch.setattr(cfg, 'ADMIN_IDS', [OWNER, 777])
    db.log_event('admin_error', None, None, None, 'worker', 'owner-private-error')
    text = await dispatch(client, Event('/errors', sender=777))
    assert 'owner-private-error' not in text
    assert 'মূল মালিক' in text
    text = await dispatch(client, Event('/errors'))
    assert 'owner-private-error' in text


def test_manifest_is_complete_and_used_by_all_entrypoints():
    from pathlib import Path
    assert 'app.handlers.reel' in HANDLER_MODULES
    assert 'app.handlers.commands' in HANDLER_MODULES
    assert 'app.handlers.command_fallback' in HANDLER_MODULES
    assert 'load_handlers(client)' in Path('bot.py').read_text()
    assert 'load_handlers(client)' in Path('run.py').read_text()


async def test_error_notification_only_targets_owner_and_redacts_secrets(client, monkeypatch):
    import bot
    monkeypatch.setattr(cfg, 'ADMIN_IDS', [OWNER, 777])
    await bot.admin_error_notifier('token=' + cfg.BOT_TOKEN + ' <private>')
    assert len(client.messages) == 1 and client.messages[0][0] == OWNER
    text = client.messages[0][1]
    assert cfg.BOT_TOKEN not in text and '&lt;private&gt;' in text


async def test_error_handler_persists_all_but_coalesces_repeated_alerts(client, monkeypatch):
    import logging
    from app import logger
    sent = []
    async def notifier(text):
        sent.append(text)
    monkeypatch.setattr(logger, 'error_notifier', notifier)
    monkeypatch.setattr(error_reports, '_seen', {})
    handler = error_reports.OwnerErrors()
    record = logging.LogRecord('test-worker', logging.ERROR, '', 1, 'failure ' + cfg.BOT_TOKEN, (), None)
    handler.emit(record)
    handler.emit(logging.makeLogRecord({k:v for k,v in record.__dict__.items() if k != '_bfix_owner_reported'}))
    await asyncio.sleep(0)
    assert len(sent) == 1 and cfg.BOT_TOKEN not in sent[0]
    rows = error_reports.recent()
    assert len(rows) == 2 and all(cfg.BOT_TOKEN not in r['detail'] for r in rows)


async def test_registered_wrapper_catches_exception_without_showing_traceback(client, monkeypatch):
    before = list(runtime.handler_registrations)
    try:
        @runtime.bot.on(events.NewMessage(pattern=r'^/crashprobe$'), priority=-110)
        async def broken(event):
            raise RuntimeError('SECRET TECHNICAL STACK')
        runtime.install_handlers(client)
        text = await dispatch(client, Event('/crashprobe', sender=999))
        assert 'সাময়িক সমস্যা' in text
        assert 'SECRET TECHNICAL STACK' not in text
    finally:
        runtime.handler_registrations[:] = before
        for callback, builder in list(client.handlers):
            if callback.__name__ == 'broken':
                client.remove_event_handler(callback)


async def test_mixed_case_own_suffix_and_maintenance_do_not_block_admin(client):
    settings.set('MAINTENANCE_MODE', '1')
    text = await dispatch(client, Event('/BRHELP@MyDesiBot'))
    assert '/brsend' in text
    text = await dispatch(client, Event('/start', sender=999))
    assert 'maintenance' in text.lower() or 'মেইনটেন্যান্স' in text
    text = await dispatch(client, Event('/help'))
    assert '/restricted' in text


async def test_startup_notice_and_private_telegram_menu_scopes(client, monkeypatch):
    from telethon.tl import types
    requests = []
    class MenuClient(Client):
        async def __call__(self, request):
            requests.append(request)
        async def get_input_entity(self, uid):
            return types.InputPeerUser(uid, 123)
    menu_client = MenuClient()
    await command_catalog.announce_and_install(menu_client)
    assert 'desi-2026.10.18-group-join-recovery' in menu_client.messages[0][1]
    assert 'MyDesiBot' in menu_client.messages[0][1]
    public = next(r for r in requests if isinstance(r.scope, types.BotCommandScopeDefault))
    assert [c.command for c in public.commands] == ['start']
    private = next(r for r in requests if isinstance(r.scope, types.BotCommandScopePeer))
    assert private.scope.peer.user_id == OWNER
    assert [c.command for c in private.commands] == ['start']


async def test_rejected_search_exposes_no_exception_to_ordinary_user(client, monkeypatch):
    from app.handlers import messages
    def fail(*args, **kwargs):
        raise RuntimeError('DATABASE_PASSWORD_SECRET')
    from app.services import access
    store = db.create_store(OWNER, 'Public search')
    state.search_pending[999] = store['id']
    monkeypatch.setattr(access, 'has_access', lambda *a: True)
    monkeypatch.setattr(db._impl, 'search_files', fail)
    event = Event('find something', sender=999)
    text = await dispatch(client, event)
    assert 'সাময়িক সমস্যা' in text
    assert 'DATABASE_PASSWORD_SECRET' not in text


async def test_private_start_really_stops_at_global_join_gate(client, monkeypatch):
    from app.services import forcejoin
    settings.set('FORCE_CHANNEL', '@required_channel')
    async def target(item):
        return {**item, 'ok': True, 'chat_id': -100123}
    async def not_joined(*args):
        return False
    monkeypatch.setattr(forcejoin, 'ensure_target', target)
    monkeypatch.setattr(forcejoin, '_member_of', not_joined)
    event = Event('/start f123', sender=999)
    text = await dispatch(client, event)
    assert 'চ্যানেল' in text
    assert db.get_meta('join_start:999') == 'f123'
    assert 'no longer exists' not in text


async def test_group_start_really_dispatches_but_ignores_foreign_bot(client, monkeypatch):
    from app.services import sender
    store = db.create_store(OWNER, 'Free promotion')
    db.update_store(store['id'], is_premium=0)
    file_id = db.add_file(store['id'], 'Video', 'Video', -100, 1)
    sent = []
    async def deliver(chat, file, caption_text=None, buttons=None):
        sent.append((chat, file['id'], caption_text))
        return sender.Delivery(True)
    monkeypatch.setattr(sender, 'deliver', deliver)
    event = Event('/start@OtherBot', sender=999)
    event.is_group = True
    event.is_private = False
    event.chat_id = -100999
    await dispatch(client, event)
    assert not sent
    event.message.message = '/start@MyDesiBot'
    await dispatch(client, event)
    assert sent == [(-100999, file_id, '')]
