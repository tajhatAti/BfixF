"""বটের ভিতর দিয়ে হোস্টিং বদল: `/backup`, `/restore`, পেস্ট করা কোড।

নতুন জায়গায় শুধু বট চালু করে ফাইলটা পাঠালেই পুরো ইনস্টল ফিরে আসে — এটাই
এই টেস্টগুলোর দাবি।
"""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace

import pytest
from telethon import events

from app import config as cfg, runtime
from app.handlers import portable as portable_handlers
from app.handlers import state
from app.services import portable, settings
from app.storage import db
from tests.test_quickcast import FakeClient

ADMIN_ID = 900


async def call(handler, event, *args) -> None:
    with suppress(events.StopPropagation):
        await handler(event, *args)


class Ev:
    """A private message from the admin, as Telethon delivers it."""

    def __init__(self, text: str = "", document=None, client=None) -> None:
        self.sender_id = ADMIN_ID
        self.chat_id = ADMIN_ID
        self.is_private = True
        self.out = False
        self.raw_text = text
        self.client = client
        self.message = SimpleNamespace(id=7, chat_id=ADMIN_ID, document=document,
                                       file=SimpleNamespace(name="bfixf-backup.bfixf.gz",
                                                            size=2048))
        self.reply_to_msg = None
        self.sent: list[tuple] = []
        self.edits: list[tuple] = []

    async def respond(self, text=None, buttons=None, **_kwargs):
        self.sent.append((text, buttons))
        outer = self

        class Status:
            async def edit(self, text=None, buttons=None, **_k):
                outer.edits.append((text, buttons))
                return self

        return Status()

    async def edit(self, text=None, buttons=None, **_kwargs):
        self.edits.append((text, buttons))
        return SimpleNamespace(id=1)

    async def answer(self, text=None, alert=False):
        self.answers = getattr(self, "answers", [])
        self.answers.append((text, alert))


async def _noop() -> None:
    return None


class BytesClient(FakeClient):
    """A client whose `download_media` hands back the bytes we prepared."""

    def __init__(self, payload: bytes, **kwargs) -> None:
        super().__init__(**kwargs)
        self.payload = payload
        self.downloads: list = []

    async def download_media(self, message, file=None, **kwargs):
        self.downloads.append(message)
        return self.payload


@pytest.fixture()
def bot_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    db.bind(str(tmp_path / "bot.sqlite3"))
    settings._cache.clear()
    portable_handlers._pending.clear()
    portable_handlers._staged.clear()
    portable_handlers._parts.clear()
    state.clear(ADMIN_ID)
    db.touch_user(11, "রহিম", "rahim")
    db.set_user_peer(11, 909, "রহিম")
    yield
    portable_handlers._pending.clear()
    portable_handlers._staged.clear()
    portable_handlers._parts.clear()
    state.clear(ADMIN_ID)


@pytest.mark.asyncio
async def test_backup_sends_one_portable_file(bot_ctx, monkeypatch):
    sent: list[dict] = []

    async def fake_safe_call(func, *args, **kwargs):
        sent.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(id=1)

    from app.services import telegram as telegram_service
    monkeypatch.setattr(telegram_service, "safe_call", fake_safe_call)

    event = Ev("/backup")
    await call(portable_handlers.backup_command, event)
    assert sent, "no file was sent"
    payload = sent[0]["args"][1]
    assert isinstance(payload, (bytes, bytearray)) and payload[:2] == b"\x1f\x8b"
    assert portable.describe(bytes(payload))["counts"]["users"] == 1
    assert sent[0]["kwargs"].get("file_name", "").endswith(portable.SUFFIX)
    # the caption teaches the move *and* warns about the token
    caption = sent[0]["kwargs"].get("caption", "")
    assert "/restore" in caption and "⚠️" in caption
    assert any(("পাঠানো হয়েছে" in (text or "")) for text, _ in event.sent + event.edits)


@pytest.mark.asyncio
async def test_restore_asks_for_the_file_then_confirms(bot_ctx):
    event = Ev("/restore")
    await call(portable_handlers.restore_command, event)
    assert state.peek(ADMIN_ID, fresh=False) is not None       # the flow is armed
    assert any("ফাইলটা" in (text or "") or "পাঠিয়ে দিন" in (text or "")
               for text, _ in event.sent)

    backup = portable.export_bytes()
    client = BytesClient(backup)
    document_event = Ev("", document=SimpleNamespace(id=1), client=client)
    await call(portable_handlers.restore_document, document_event)
    assert portable_handlers._pending[ADMIN_ID] == backup       # waiting for the tap
    summary, buttons = document_event.sent[-1]
    assert "ইউজার" in summary and buttons and len(buttons[0]) == 2   # ✅ / ✖️
    assert "বদলে যাবে" in summary                               # the warning is there


@pytest.mark.asyncio
async def test_the_confirm_button_restores_the_whole_install(bot_ctx):
    backup = portable.export_bytes()
    db.touch_user(777, "extra", "x")                            # lost when we restore
    assert db.user(777) is not None

    client = BytesClient(backup)
    await call(portable_handlers.restore_document,
               Ev("", document=SimpleNamespace(id=1), client=client))
    event = Ev("")
    await call(portable_handlers.restore_confirm, event, "go")

    assert db.user(777) is None                                 # back to the snapshot
    assert db.user(11) is not None and db.user_peer(11)["access_hash"] == 909
    text = " ".join(t or "" for t, _ in event.sent)
    assert "ফিরিয়ে আনা হয়েছে" in text
    assert "before-restore" in text                             # safety copy path is shown
    assert portable.local_backups()                             # …and it is on disk


@pytest.mark.asyncio
async def test_the_cancel_button_keeps_the_database(bot_ctx):
    await call(portable_handlers.restore_document,
               Ev("", document=SimpleNamespace(id=1),
                  client=BytesClient(portable.export_bytes())))
    db.touch_user(777, "extra", "x")
    event = Ev("")
    await call(portable_handlers.restore_confirm, event, "no")
    assert db.user(777) is not None and not portable_handlers._pending


@pytest.mark.asyncio
async def test_a_bad_file_is_reported_and_never_touches_the_data(bot_ctx):
    client = BytesClient(b"this is not a backup")
    event = Ev("", document=SimpleNamespace(id=1), client=client)
    await call(portable_handlers.restore_document, event)
    assert not portable_handlers._pending
    assert any("❌" in (text or "") for text, _ in event.sent)
    assert db.user(11) is not None


@pytest.mark.asyncio
async def test_pasted_codes_are_collected_and_then_offered(bot_ctx):
    data = portable.export_bytes()
    codes = portable.text_codes(data, chunk=600)
    assert len(codes) > 2
    for index, code in enumerate(codes[:-1]):
        event = Ev(code)
        await call(portable_handlers.restore_text, event)
        assert any("টুকরো" in (text or "") for text, _ in event.sent)
    last = Ev(codes[-1])
    await call(portable_handlers.restore_text, last)
    assert portable_handlers._pending[ADMIN_ID] == data
    summary, buttons = last.sent[-1]
    assert buttons and len(buttons[0]) == 2                     # ✅ / ✖️
    assert "ব্যাকআপে যা আছে" in summary


@pytest.mark.asyncio
async def test_a_restore_can_be_undone_from_the_safety_copy(bot_ctx):
    before = portable.export_bytes()
    db.touch_user(777, "extra", "x")                            # exists only “before”
    report = portable.restore(before, source="test")
    assert db.user(777) is None                                 # the backup had no 777
    portable.restore(open(report["safety_copy"], "rb").read(), source="safety")
    assert db.user(777) is not None                             # safety copy brought it back


@pytest.mark.asyncio
async def test_an_ordinary_document_is_not_treated_as_a_backup(bot_ctx):
    """An admin sending any other file must reach its own flow, not wipe the DB."""
    client = BytesClient(b"a normal video")
    event = Ev("", document=SimpleNamespace(id=1), client=client)
    event.message.file = SimpleNamespace(name="holiday.mp4", size=99)
    await call(portable_handlers.restore_document, event)
    assert not portable_handlers._pending and not event.sent


@pytest.mark.asyncio
async def test_the_bot_merges_an_older_bots_user_list(bot_ctx, tmp_path):
    """পুরোনো বটের users.db বটে পাঠালে সেটা যোগ হয় — বদলে যায় না।"""
    import sqlite3
    db.touch_user(31337, "নতুন ইনস্টলের ইউজার", "newbie")
    path = str(tmp_path / "users.db")
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY, blocked INTEGER DEFAULT 0,"
                       " username TEXT, name TEXT);")
    conn.execute("INSERT INTO users VALUES (4242, 0, 'oldie', 'পুরোনো ইউজার')")
    conn.execute("INSERT INTO users VALUES (4243, 1, 'banned_guy', 'ব্লক করা')")
    conn.commit()
    conn.close()

    event = Ev("", document=SimpleNamespace(id=1), client=BytesClient(open(path, "rb").read()))
    await call(portable_handlers.restore_document, event)
    summary, buttons = event.sent[-1]
    assert "পুরোনো বটের" in summary and "ইউজার তালিকা" in summary   # চিনে নিয়েছে
    assert "যোগ" in summary and "মুছে যাবে না" in summary
    assert buttons and len(buttons[0]) == 2

    confirm = Ev("")
    await call(portable_handlers.restore_confirm, confirm, "go")
    text = " ".join(t or "" for t, _ in confirm.sent)
    assert "যোগ করা হয়েছে" in text and "অক্ষত" in text
    assert "নতুন ২" in text and "ব্যান ১" in text            # কতজন, কীভাবে যোগ হলো
    assert db.user(4242) is not None and db.user(31337) is not None
    assert db.is_banned(4243) is True                       # ব্লকড ছিল → ব্যান
    assert not portable_handlers._pending


@pytest.mark.asyncio
async def test_a_json_that_is_not_a_backup_keeps_its_own_flow(bot_ctx):
    """একটা সাধারণ json ফাইল ভুল করে রিস্টোর ফ্লোতে ঢুকে পড়বে না।"""
    from app.handlers import state as flow_state
    flow_state.ask(ADMIN_ID, "content_upload")          # some other flow is waiting
    event = Ev("", document=SimpleNamespace(id=1),
               client=BytesClient(b'{"hello": "world"}'))
    event.message.file = SimpleNamespace(name="content.json", size=20)
    await call(portable_handlers.restore_document, event)
    assert not event.sent                               # কিছুই বলল না, দখলও করল না
    assert flow_state.peek(ADMIN_ID, fresh=False) is not None


@pytest.mark.asyncio
async def test_the_legacy_json_backup_works_from_the_chat_too(bot_ctx, tmp_path):
    legacy = {"user_details": {"5050": {"name": "পুরোনো বটের ইউজার", "joined": 1.0}},
              "stores": {}, "premium_access": {}}
    event = Ev("", document=SimpleNamespace(id=1),
               client=BytesClient(json.dumps(legacy, ensure_ascii=False).encode("utf-8")))
    event.message.file = SimpleNamespace(name="bot_db.json", size=90)
    await call(portable_handlers.restore_document, event)
    summary, buttons = event.sent[-1]
    assert "যোগ" in summary and buttons and len(buttons[0]) == 2
    confirm = Ev("")
    await call(portable_handlers.restore_confirm, confirm, "go")
    text = " ".join(t or "" for t, _ in confirm.sent)
    assert "যোগ করা হয়েছে" in text and db.user(5050) is not None
    assert db.user(11) is not None                              # পুরোনো ইনস্টল অটুট
