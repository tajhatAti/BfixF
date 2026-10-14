"""`/br` - the minimal broadcast: collect content, send to everyone at once."""
import asyncio
import time

import pytest

from app import config as cfg
from app.runtime import blocked_users
from app.services import quickcast
from app.services.quickcast import Content
from app.storage import db

ADMIN_ID = 900


class FakeBot:
    def __init__(self, fail_for=None, delay=0.0):
        self.messages, self.files = [], []
        self.fail_for = fail_for or {}
        self.delay = delay

    async def send_message(self, chat_id, text=None, **kwargs):
        await asyncio.sleep(self.delay)
        if chat_id in self.fail_for:
            raise RuntimeError(self.fail_for[chat_id])
        self.messages.append((chat_id, text, kwargs))

    async def send_file(self, chat_id, media, **kwargs):
        await asyncio.sleep(self.delay)
        if chat_id in self.fail_for:
            raise RuntimeError(self.fail_for[chat_id])
        self.files.append((chat_id, media, kwargs))


@pytest.fixture()
def users(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "quick.sqlite3"))
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    blocked_users.clear()
    for uid in range(11, 23):                       # 12 users
        db.touch_user(uid, f"U{uid}", None)
    return monkeypatch


def test_html_text_and_buttons_are_told_apart():
    content = Content()
    assert quickcast.add_text(content, "<b>Hello</b> world", None) == "text"
    assert content.text == "<b>Hello</b> world"
    assert quickcast.add_text(content, "Join | https://t.me/x\nStore | https://t.me/y", None) == "buttons"
    assert content.buttons == [[("Join", "https://t.me/x")], [("Store", "https://t.me/y")]]
    assert quickcast.looks_like_buttons("just a sentence with no link") is False
    assert quickcast.to_html("5 < 6", []) == "5 &lt; 6"        # plain text is escaped


@pytest.mark.asyncio
async def test_text_reaches_everyone_at_once(users):
    fake = FakeBot(delay=0.2)
    users.setattr(quickcast, "bot", fake)
    content = Content(text="<b>hi</b>", buttons=[[("Open", "https://t.me/x")]])

    report = await quickcast.send_all(ADMIN_ID, content)

    assert report["sent"] == 12 and report["failed"] == 0
    assert report["seconds"] < 0.6          # one-by-one would take 2.4s
    assert all(kw["parse_mode"] == "html" and kw["buttons"] for _c, _t, kw in fake.messages)


@pytest.mark.asyncio
async def test_media_goes_with_caption_and_buttons(users):
    fake = FakeBot()
    users.setattr(quickcast, "bot", fake)
    content = Content(media="VIDEO", text="<i>watch</i>", buttons=[[("Play", "https://t.me/x")]])

    report = await quickcast.send_all(ADMIN_ID, content)

    assert report["sent"] == 12 and len(fake.files) == 12 and not fake.messages
    chat, media, kwargs = fake.files[0]
    assert media == "VIDEO" and kwargs["caption"] == "<i>watch</i>" and kwargs["buttons"]


@pytest.mark.asyncio
async def test_long_text_with_media_is_split_and_blocked_users_are_skipped(users):
    fake = FakeBot(fail_for={11: "Forbidden: user blocked the bot"})
    users.setattr(quickcast, "bot", fake)
    content = Content(media="PHOTO", text="x" * 1500)

    report = await quickcast.send_all(ADMIN_ID, content)

    assert report["failed"] == 1 and report["sent"] == 11
    assert 11 in blocked_users
    assert len(fake.messages) == 11 and len(fake.files) == 11   # media, then the long text
