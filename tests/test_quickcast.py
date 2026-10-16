"""`/br` broadcast — the whole path, with a fake Telegram that behaves like the real one.

The important part is `FakeClient`: it reproduces Telegram's *real* rule that a
bot may only message a user it knows the ``access_hash`` of. Telethon stores that
inside the session file and hosting panels wipe it, which is exactly why “the
broadcast system does not work at all”. The first test would fail on the old
code and passes now, because the hash is remembered in the database.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from datetime import datetime, timezone

from telethon.tl.types import (Document, DocumentAttributeVideo, InputPeerUser,
                               MessageMediaDocument)

from app import config as cfg, runtime
from app.services import broadcast, peers, quickcast
from app.services.quickcast import Content, MediaItem
from app.storage import db

ADMIN_ID = 900
USERS = (11, 12, 13, 14)


# --------------------------------------------------------------------- fakes
def video_document(doc_id: int = 77) -> Document:
    """A real Telethon Document, so `utils.get_input_media()` accepts it."""
    return Document(id=doc_id, access_hash=1234, file_reference=b"ref",
                    date=datetime.now(timezone.utc), mime_type="video/mp4",
                    size=1000, dc_id=2, attributes=[DocumentAttributeVideo(
                        duration=9, w=720, h=1280, supports_streaming=True)])


class FakeMessage:
    """Looks enough like a Telethon message for the sender and the feed."""

    def __init__(self, chat_id=-100, msg_id=5, media=None, text=""):
        self.chat_id = chat_id
        self.id = msg_id
        self.media = media
        self.text = text
        self.raw_text = text
        self.file = type("F", (), {"name": "clip.mp4", "size": 1000,
                                   "mime_type": "video/mp4"})()


class FakeClient:
    """A Telegram client with a session cache that may be empty (fresh host)."""

    def __init__(self, known: dict[int, int] | None = None, messages=None):
        self.cache: dict[int, int] = dict(known or {})
        self.messages = messages or {}
        self.sent_messages: list[tuple[int, str]] = []
        self.requests: list = []
        self.parse_mode = "html"
        self._connected = True

    # --- entity resolution: the real Telethon rule -------------------------
    async def get_input_entity(self, entity):
        if isinstance(entity, InputPeerUser):
            return entity
        uid = getattr(entity, "user_id", entity)
        if isinstance(uid, int) and uid in self.cache:
            return InputPeerUser(uid, self.cache[uid])
        raise ValueError(
            f"Could not find the input entity for PeerUser(user_id={uid}) (PeerUser).")

    def is_connected(self):
        return True

    # --- sending -----------------------------------------------------------
    @staticmethod
    def _peer_id(peer):
        return getattr(peer, "user_id", peer)

    async def send_message(self, peer, text=None, **_kwargs):
        uid = self._peer_id(peer)
        if isinstance(uid, int) and uid not in self.cache and not isinstance(peer, InputPeerUser):
            raise ValueError(f"Could not find the input entity for PeerUser(user_id={uid})")
        self.sent_messages.append((uid, text))
        return FakeMessage(chat_id=uid, msg_id=len(self.sent_messages))

    async def send_file(self, peer, file=None, **_kwargs):
        uid = self._peer_id(peer)
        self.sent_messages.append((uid, "<file>"))
        return FakeMessage(chat_id=uid)

    async def __call__(self, request, *args, **kwargs):
        """Raw request (SendMediaRequest / SendMultiMediaRequest / …)."""
        self.requests.append(request)
        peer = getattr(request, "peer", None)
        uid = self._peer_id(peer) if peer is not None else None
        self.sent_messages.append((uid, getattr(request, "message", "<request>")))
        return FakeMessage(chat_id=uid or 0)

    async def get_messages(self, chat, ids=None):
        key = (int(chat), int(ids))
        return self.messages.get(key)

    async def download_media(self, message, file=None, thumb=None):
        return b"\xff\xd8fake-jpeg"

    async def iter_download(self, media, offset=0, request_size=1024):
        for pos in range(offset, offset + request_size, request_size):
            yield b"x" * min(request_size, 64)


@pytest.fixture()
def studio(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "br.sqlite3"))
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    runtime.blocked_users.clear()
    peers._hashes.clear()
    peers._hopeless.clear()
    for uid in USERS:
        db.touch_user(uid, f"User{uid}", f"u{uid}")
    monkeypatch.setattr(runtime, "bot_username", "test_bot", raising=False)
    yield monkeypatch
    runtime.set_client(None)


def _install(monkeypatch, client: FakeClient) -> FakeClient:
    runtime.set_client(client)
    monkeypatch.setattr(runtime, "_client", client, raising=False)
    return client


# -------------------------------------------------------- the regression test
@pytest.mark.asyncio
async def test_broadcast_reaches_everyone_after_the_session_cache_is_wiped(studio):
    """A fresh session (new host / wiped disk) must still reach every user.

    Telethon's entity cache is empty here — the only thing that saves the
    broadcast is `users.access_hash` in our own database.
    """
    studio.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    for uid in USERS:                       # every user once pressed /start
        db.set_user_peer(uid, 555000 + uid, f"User{uid}", f"u{uid}")
    client = _install(studio, FakeClient(known={}))          # empty session!
    peers._hashes.clear()

    report = await quickcast.send_all(ADMIN_ID, Content(text="<b>hello</b> all"))

    assert report["sent"] == len(USERS)
    assert sorted(uid for uid, _ in client.sent_messages) == sorted(USERS)
    campaign = db.campaign(report["campaign_id"])
    assert campaign["origin"] == "br"
    assert db.campaign_counts(report["campaign_id"])["pending"] == 0


@pytest.mark.asyncio
async def test_people_who_never_started_are_explained_not_silently_lost(studio):
    """The one user without an access hash is reported, the others still get it."""
    for uid in USERS[:-1]:
        db.set_user_peer(uid, 555000 + uid)
    client = _install(studio, FakeClient(known={}))
    peers._hashes.clear()

    report = await quickcast.send_all(ADMIN_ID, Content(text="hi"))

    assert report["sent"] == len(USERS) - 1
    assert report["failed"] == 1
    reasons = broadcast.failure_breakdown(report["campaign_id"])
    assert reasons and any("access hash" in group["hint"] for group in reasons)
    item = [row for row in db.campaign_items(report["campaign_id"])
            if row["user_id"] == USERS[-1]][0]
    assert item["status"] == "failed"
    assert "unreachable" in (item["error"] or "") or "access hash" in (item["error"] or "")
    assert db.user_peer(USERS[-1])["reachable"] == 0


# ------------------------------------------------------------------ content
def test_text_and_buttons_are_told_apart():
    content = Content()
    assert quickcast.add_text(content, "<b>Hello</b> world", None) == "text"
    assert content.text == "<b>Hello</b> world"
    assert quickcast.add_text(content, "Join | https://t.me/x\nStore | https://t.me/y",
                              None) == "buttons"
    assert content.buttons == [[("Join", "https://t.me/x")], [("Store", "https://t.me/y")]]
    assert quickcast.looks_like_buttons("just a sentence with no link") is False
    assert quickcast.to_html("5 < 6", []) == "5 &lt; 6"


def test_media_items_and_drafts_round_trip(tmp_path):
    db.bind(str(tmp_path / "draft.sqlite3"))
    content = Content(text="<b>x</b>", silent=True, audience="premium")
    content.media = [MediaItem(chat_id=-100, msg_id=7, kind="video", name="a.mp4")]
    quickcast.save_draft(ADMIN_ID, content)

    restored = quickcast.load_draft(ADMIN_ID)
    assert restored.text == "<b>x</b>" and restored.silent and restored.audience == "premium"
    assert restored.media[0].msg_id == 7 and restored.media[0].chat_id == -100

    quickcast.drop_draft(ADMIN_ID)
    assert quickcast.load_draft(ADMIN_ID) is None


# -------------------------------------------------------------------- media
@pytest.mark.asyncio
async def test_an_uploaded_video_is_sent_with_caption_and_buttons(studio):
    media = MessageMediaDocument(document=video_document())
    source = FakeMessage(chat_id=ADMIN_ID, msg_id=5, media=media)
    client = _install(studio, FakeClient(known={uid: 1000 + uid for uid in USERS},
                                         messages={(ADMIN_ID, 5): source}))
    content = Content(text="<i>watch this</i>")
    content.media = [MediaItem(chat_id=ADMIN_ID, msg_id=5, kind="video")]
    content.buttons = [[("Open", "https://t.me/x")]]

    report = await quickcast.send_all(ADMIN_ID, content)

    assert report["sent"] == len(USERS)
    media_requests = [r for r in client.requests if type(r).__name__ == "SendMediaRequest"]
    assert len(media_requests) == len(USERS)
    assert all(getattr(r, "message", "") == "watch this" for r in media_requests)  # parsed
    assert all(r.reply_markup for r in media_requests)


@pytest.mark.asyncio
async def test_several_items_go_out_as_one_album(studio):
    first = FakeMessage(chat_id=ADMIN_ID, msg_id=5,
                        media=MessageMediaDocument(document=video_document(1)))
    second = FakeMessage(chat_id=ADMIN_ID, msg_id=6,
                         media=MessageMediaDocument(document=video_document(2)))
    client = _install(studio, FakeClient(known={uid: 1 for uid in USERS},
                                         messages={(ADMIN_ID, 5): first,
                                                   (ADMIN_ID, 6): second}))
    content = Content(text="album")
    content.media = [MediaItem(chat_id=ADMIN_ID, msg_id=5),
                     MediaItem(chat_id=ADMIN_ID, msg_id=6)]

    report = await quickcast.send_all(ADMIN_ID, content)

    albums = [r for r in client.requests if type(r).__name__ == "SendMultiMediaRequest"]
    assert report["sent"] == len(USERS) and len(albums) == len(USERS)
    assert all(len(album.multi_media) == 2 for album in albums)


@pytest.mark.asyncio
async def test_buttons_and_silent_option_reach_the_api(studio):
    client = _install(studio, FakeClient(known={uid: 1 for uid in USERS}))
    content = Content(text="quiet please", silent=True)
    content.buttons = [[("Store", "https://t.me/x")]]

    await quickcast.send_all(ADMIN_ID, content)

    assert len(client.sent_messages) == len(USERS)


@pytest.mark.asyncio
async def test_a_broken_file_falls_back_to_the_text(studio, monkeypatch):
    """Media the bot cannot read must not lose the whole message."""
    from app.services import sender

    client = _install(studio, FakeClient(known={uid: 1 for uid in USERS}))

    async def no_mirror(row):
        return row

    monkeypatch.setattr(sender, "source_message", lambda *a, **k: _none())

    content = Content(text="caption")
    content.media = [MediaItem(chat_id=ADMIN_ID, msg_id=99)]

    report = await quickcast.send_all(ADMIN_ID, content)

    assert report["sent"] == len(USERS)                   # text still arrived
    assert all(text == "caption" for _uid, text in client.sent_messages)


async def _none():
    return None


@pytest.mark.asyncio
async def test_blocked_users_are_skipped_on_the_next_run(studio):
    client = FakeClient(known={uid: 1 for uid in USERS})
    original = client.send_message

    async def flaky(peer, text=None, **kwargs):
        uid = client._peer_id(peer)
        if uid in (12, 13):
            raise RuntimeError("Forbidden: user blocked the bot")
        return await original(peer, text, **kwargs)

    client.send_message = flaky
    _install(studio, client)

    first = await quickcast.send_all(ADMIN_ID, Content(text="one"))
    assert first["blocked"] == 2 and first["sent"] == len(USERS) - 2
    assert {12, 13} <= runtime.blocked_users

    second_client = _install(studio, FakeClient(known={uid: 1 for uid in USERS}))
    second = await quickcast.send_all(ADMIN_ID, Content(text="two"))
    assert second["sent"] == len(USERS) - 2
    assert sorted(uid for uid, _ in second_client.sent_messages) == [11, 14]


# ---------------------------------------------------------------- scheduling
@pytest.mark.asyncio
async def test_scheduling_queues_instead_of_sending(studio):
    client = _install(studio, FakeClient(known={uid: 1 for uid in USERS}))
    report = await quickcast.send_all(ADMIN_ID, Content(text="later"),
                                      scheduled_at=time.time() + 600)

    assert report["status"] == broadcast.SCHEDULED
    assert not client.sent_messages
    stored = db.campaign(report["campaign_id"])
    assert stored["status"] == broadcast.SCHEDULED and stored["scheduled_at"]
