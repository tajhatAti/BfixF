"""v3 features: media filter, limited links, force-join gate, analytics, channels.

These are the behaviours the owner asked for by name:

* “যেকোনো টেক্সট দিলে বারে বারে সেভ হইতেছে” → only video/photo may be stored.
* “লিংকে ক্লিক করলে সোজা ভিডিও আসবে” → one tap, gate first.
* “১০০ বার খোলা যাবে, ১০১তম বারে লিমিট শেষ” → link click limit.
* “অ্যাডমিন প্যানেলে কে কী দেখল / কতজন কনভার্ট হল” → `events` analytics.
* “চ্যানেলে ইনলাইন বাটন দিয়ে মেসেজ পাঠানো” → channel composer.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app import config as cfg, runtime
from app.handlers import state
from app.services import broadcast, channels, flow, forcejoin, media_guard
from app.storage import db

ADMIN_ID = 999
USER_ID = 555


# --------------------------------------------------------------------- fixtures
class FakeMessage:
    """A Telegram message good enough for the media guard."""

    def __init__(self, *, video=False, photo=False, audio=False, document=False,
                 sticker=False, media=True, text="", caption="", media_class="MessageMediaDocument"):
        self.video = SimpleNamespace(duration=42) if video else None
        self.photo = SimpleNamespace(id=1) if photo else None
        self.audio = object() if audio else None
        self.document = object() if document else None
        self.sticker = object() if sticker else None
        self.gif = None
        self.poll = None
        self.geo = None
        self.contact = None
        self.file = SimpleNamespace(size=1024) if (video or photo or audio or document) else None
        self.media = _media_object(media_class) if media else None
        self.message = caption
        self.raw_text = text
        self.id = 42

    class _Media:
        pass


class _WebPage:
    """Mimics tl.MessageMediaWebPage (has .webpage)."""

    def __init__(self):
        self.webpage = object()


def _media_object(media_class: str):
    if media_class == "MessageMediaWebPage":
        cls = type("MessageMediaWebPage", (), {})
        obj = cls()
        obj.webpage = object()
        return obj
    return type(media_class, (), {"document": object()})() if media_class != "MessageMediaPhoto" \
        else type(media_class, (), {"photo": object()})()


def _plain_text_message(text="https://example.com/a/b"):
    """A text message with a link preview — Telegram sets `.media` for these."""
    return FakeMessage(media=True, text=text, media_class="MessageMediaWebPage")


def _video_message(caption="New video"):
    return FakeMessage(video=True, caption=caption)


@pytest.fixture()
def clean(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "v3.sqlite3"))
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    state.pending_input.clear()
    state.search_pending.clear()
    state.flow_ctx.clear()
    state.batch_mode.clear()
    runtime.force_join_ok.clear()
    runtime.force_join_cache.clear()
    db.touch_user(USER_ID, "Tester", "tester")
    db.touch_user(ADMIN_ID, "Owner", "owner")
    return monkeypatch


def test_bot_online_requires_a_live_telegram_connection(monkeypatch):
    class FakeClient:
        def __init__(self, connected):
            self.connected = connected

        def is_connected(self):
            return self.connected

    monkeypatch.setattr(runtime, "_client", FakeClient(False))
    monkeypatch.setattr(runtime, "bot_id", 123)
    assert runtime.bot_online() is False
    monkeypatch.setattr(runtime, "_client", FakeClient(True))
    assert runtime.bot_online() is True


def _store_with_video(name="Movies", premium=False):
    store = db.create_store(ADMIN_ID, name)
    db.update_store(store["id"], is_premium=1 if premium else 0)
    file_id = db.add_file(store["id"], "Movie 1", "Video", -100, 10)
    db.set_mirror(file_id, ADMIN_ID, 500)
    return db.store(store["id"]), file_id


# --------------------------------------------------------------- media guard
def test_plain_text_is_never_media():
    assert media_guard.classify(_plain_text_message())[0] is None
    assert media_guard.is_plain_text(_plain_text_message())


def test_text_with_link_preview_is_refused_with_clear_reason():
    kind, reason = media_guard.classify(_plain_text_message())
    assert kind is None and reason == "text"
    assert "সেভ হয়" in media_guard.REJECT_TEXT


def test_video_and_photo_are_accepted():
    assert media_guard.classify(_video_message())[0] == "Video"
    assert media_guard.classify(FakeMessage(photo=True, media_class="MessageMediaPhoto"))[0] == "Photo"


def test_audio_document_and_stickers_are_refused():
    assert media_guard.classify(FakeMessage(audio=True))[0] is None
    assert media_guard.classify(FakeMessage(document=True))[0] is None
    assert media_guard.classify(FakeMessage(sticker=True))[0] is None


def test_allowed_kinds_can_be_widened_from_the_panel(clean):
    from app.services import settings
    assert media_guard.allowed_kinds() == {"Video", "Photo"}
    settings.set("CONTENT_MEDIA_KINDS", "video,photo,document")
    assert "Document" in media_guard.allowed_kinds()
    settings.reset("CONTENT_MEDIA_KINDS")


def test_item_name_comes_from_the_caption():
    assert media_guard.display_name(_video_message("Episode 07 HD"), "Video", 3) == "Episode 07 HD"
    assert media_guard.display_name(_video_message(""), "Video", 3) == "Video 3"
    assert media_guard.duration_of(_video_message()) == 42


# --------------------------------------------------------------- limited links
def test_limited_link_allows_n_clicks_then_refuses(clean):
    store, file_id = _store_with_video()
    token = db.create_link(ADMIN_ID, [file_id], None, kind="limited", max_clicks=3)
    assert db.link_clicks(token) == 0
    for expected in (1, 2, 3):
        got = db.link_take(token)
        assert got["ok"] and got["clicks"] == expected
    blocked = db.link_take(token)
    assert blocked["ok"] is False and blocked["reason"] == "limit"
    assert blocked["limit"] == 3 and blocked["clicks"] == 3


def test_unlimited_link_never_runs_out(clean):
    store, file_id = _store_with_video()
    token = db.create_link(ADMIN_ID, [file_id], None)
    for _ in range(50):
        assert db.link_take(token)["ok"] is True


def test_link_take_reports_expired(clean):
    store, file_id = _store_with_video()
    token = db.create_link(ADMIN_ID, [file_id], time.time() - 10)
    assert db.link_take(token)["reason"] == "expired"


# ------------------------------------------------------------- force-join gate
class GateEvent:
    """Event that records what the flow tried to send."""

    def __init__(self, user_id, chat_id=None):
        self.sender_id = user_id
        self.chat_id = chat_id or user_id
        self.sent: list[tuple] = []
        self.answers: list[tuple] = []

    async def respond(self, text=None, buttons=None, **_kwargs):
        self.sent.append((text, buttons))
        return SimpleNamespace(id=1)

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def edit(self, text=None, buttons=None, **_kwargs):
        return SimpleNamespace(id=1)


@pytest.fixture()
def collected_sends(monkeypatch):
    sent: list[tuple] = []

    async def fake_send_text(chat_id, text, buttons=None, link_preview=False):
        sent.append((chat_id, text, buttons))
        return SimpleNamespace(id=1, chat_id=chat_id)

    monkeypatch.setattr("app.services.sender.send_text", fake_send_text)
    return sent


@pytest.fixture()
def recorded_deliveries(monkeypatch):
    calls = []

    async def fake_deliver(chat_id, file_row, caption_text=None, buttons=None):
        from app.services.sender import Delivery
        calls.append((chat_id, file_row["id"]))
        return Delivery(True)

    monkeypatch.setattr("app.services.flow.deliver", fake_deliver)
    return calls


@pytest.mark.asyncio
async def test_store_channel_gate_blocks_before_delivery(clean, monkeypatch, collected_sends,
                                                         recorded_deliveries):
    store, file_id = _store_with_video()
    db.set_store_forcejoin(store["id"], "@store_only")
    monkeypatch.setattr(runtime, "get_client", lambda: SimpleNamespace())

    async def fake_check_many(user_id, store_id=None):
        return {"ok": False, "missing": [{"id": 0, "ref": "@store_only", "title": "Store only",
                                          "chat_id": -100123, "invite": "https://t.me/store_only"}],
                "skipped": 0, "checked": 1}

    monkeypatch.setattr(forcejoin, "check_many", fake_check_many)

    event = GateEvent(USER_ID)
    state_value = await flow.open_file(event, file_id)
    assert state_value == flow.GATE_JOIN
    assert recorded_deliveries == []                     # nothing sent before joining
    assert collected_sends and "জয়েন" in collected_sends[-1][1]
    assert db.join_prompt(USER_ID, file_id) is not None
    assert db.events(name="join_block", user_id=USER_ID, file_id=file_id)


@pytest.mark.asyncio
async def test_joining_first_then_receiving_the_video(clean, monkeypatch, collected_sends,
                                                      recorded_deliveries):
    store, file_id = _store_with_video()
    db.set_store_forcejoin(store["id"], "@store_only")

    async def not_joined(user_id, store_id=None):
        return {"ok": False, "skipped": 0, "checked": 1,
                "missing": [{"id": 0, "ref": "@store_only", "title": "Store only",
                             "chat_id": -100123, "invite": "https://t.me/store_only"}]}

    monkeypatch.setattr(forcejoin, "check_many", not_joined)
    event = GateEvent(USER_ID)
    assert await flow.open_file(event, file_id) == flow.GATE_JOIN

    async def joined(user_id, store_id=None):
        return {"ok": True, "missing": [], "skipped": 0, "checked": 1}

    monkeypatch.setattr(forcejoin, "check_many", joined)
    state_value = await flow.confirm_join(event, USER_ID, file_id)
    assert state_value == flow.GATE_OK
    assert recorded_deliveries == [(USER_ID, file_id)]
    assert db.join_prompt(USER_ID, file_id) is None
    assert db.events(name="deliver", user_id=USER_ID, file_id=file_id)


@pytest.mark.asyncio
async def test_fail_open_when_membership_cannot_be_checked(clean, monkeypatch,
                                                           collected_sends):
    store, file_id = _store_with_video()
    db.set_store_forcejoin(store["id"], "@store_only")

    async def unknown(user_id, store_id=None):
        return {"ok": True, "missing": [], "skipped": 1, "checked": 0}

    monkeypatch.setattr(forcejoin, "check_many", unknown)
    gate = await flow.gate_check(USER_ID, store["id"])
    assert gate["ok"] is True                            # never lock a buyer out


def test_global_and_store_targets_are_merged(clean):
    from app.services import settings
    store, _file_id = _store_with_video()
    settings.set("FORCE_CHANNEL", "@global_channel")
    settings.set("FORCE_JOIN_EXTRA", "@extra_one\nhttps://t.me/+AbCdEfGhIjK")
    db.set_store_forcejoin(store["id"], "@store_only")

    refs = {t["ref"] for t in forcejoin.targets(store["id"])}
    assert "@global_channel" in refs
    assert "@extra_one" in refs
    assert "https://t.me/+AbCdEfGhIjK" in refs
    assert "@store_only" in refs
    # …and a different store does not inherit this store's channel
    other = db.create_store(ADMIN_ID, "Other")
    refs_other = {t["ref"] for t in forcejoin.targets(other["id"])}
    assert "@store_only" not in refs_other
    settings.reset("FORCE_JOIN_EXTRA")
    settings.reset("FORCE_CHANNEL")


def test_gate_keyboard_has_one_join_button_per_channel():
    rows = forcejoin.gate_keyboard(USER_ID, 7, [
        {"ref": "@a_channel", "title": "A", "invite": "https://t.me/a_channel"},
        {"ref": "@b_channel", "title": "B", "invite": "https://t.me/b_channel"},
    ], token="tok123")
    assert len(rows) == 3                                # two channels + re-check
    assert all(len(row) == 1 for row in rows)


# ------------------------------------------------------------------- analytics
def test_events_are_recorded_and_aggregated_per_user(clean):
    store, file_id = _store_with_video()
    db.log_event("start", USER_ID, None, None, "start")
    db.log_event("open_store", USER_ID, store["id"], None, store["slug"])
    db.log_event("deliver", USER_ID, store["id"], file_id, "button")
    db.log_event("pay_start", USER_ID, store["id"], None, "plan:1")
    db.log_event("paid", USER_ID, store["id"], None, "plan:1")

    stats = db.user_analytics(USER_ID)
    assert stats["counts"]["start"] == 1
    assert stats["counts"]["deliver"] == 1
    assert stats["videos"][0]["id"] == file_id
    assert stats["stores"][0]["id"] == store["id"]


def test_store_analytics_shows_who_watched_what(clean):
    store, file_id = _store_with_video()
    other = db.create_store(ADMIN_ID, "Second")
    db.touch_user(777, "Second user", "second")
    db.log_event("open_store", USER_ID, store["id"], None, "x")
    db.log_event("deliver", USER_ID, store["id"], file_id, "x")
    db.log_event("deliver", 777, store["id"], file_id, "x")

    stats = db.store_analytics(store["id"])
    assert stats["counts"]["deliver"] == 2
    watchers = {row["user_id"] for row in stats["watchers"]}
    assert watchers == {USER_ID, 777}
    file_row = next(f for f in stats["files"] if f["id"] == file_id)
    assert file_row["uniq"] == 2 and file_row["sends"] == 2
    others = db.store_analytics(other["id"])
    assert others["counts"] == {}


def test_funnel_counts_came_watched_paid(clean):
    store, file_id = _store_with_video()
    db.log_event("open_store", USER_ID, store["id"], None, "x")
    db.log_event("deliver", USER_ID, store["id"], file_id, "x")
    db.log_event("paid", USER_ID, store["id"], None, "plan:1")
    db.log_event("open_store", 777, store["id"], None, "x")
    funnel = db.store_funnel(store["id"])
    assert funnel == {"came": 2, "watched": 1, "joined": 0, "paid": 1}


def test_file_watchers_list(clean):
    store, file_id = _store_with_video()
    db.log_event("deliver", USER_ID, store["id"], file_id, "x")
    db.log_event("deliver", USER_ID, store["id"], file_id, "x")
    rows = db.file_watchers(file_id)
    assert rows[0]["user_id"] == USER_ID and rows[0]["hits"] == 2
    assert db.unique_viewers(file_id) == 1


# -------------------------------------------------------- mirror & delivery fix
def test_mirror_targets_prefers_the_cached_copy(clean):
    from app.services.sender import event_chat_id
    store, file_id = _store_with_video()
    row = db.file(file_id)
    from app.services.media_cache import mirror_targets
    assert mirror_targets(row)[0] == (ADMIN_ID, 500)
    assert db.count_mirrors() == 1
    db.clear_mirror(file_id)
    assert db.file(file_id)["mirror_msg"] is None
    assert event_chat_id(SimpleNamespace(chat_id=77)) == 77


@pytest.mark.asyncio
async def test_deliver_uses_the_mirror_first(clean, monkeypatch):
    from app.services import sender
    store, file_id = _store_with_video()
    row = db.file(file_id)
    fetched: list[tuple] = []

    class Bot:
        async def get_messages(self, chat_id, ids=None, **_kw):
            fetched.append((chat_id, ids))
            return SimpleNamespace(media=object())

    async def fake_send_media(chat_id, media, caption_text, buttons=None):
        fetched.append(("sent", chat_id))

    monkeypatch.setattr(sender, "bot", Bot())
    monkeypatch.setattr(sender, "_send_media", fake_send_media)
    result = await sender.deliver(USER_ID, row)
    assert result.ok
    assert fetched == [(ADMIN_ID, 500), ("sent", USER_ID)]


def test_media_cache_stats(clean):
    from app.services.media_cache import stats
    store, file_id = _store_with_video()
    db.add_file(store["id"], "Second", "Video", -100, 11)
    assert stats() == {"files": 2, "mirrored": 1, "missing": 1}


# --------------------------------------------------------------------- broadcast
def test_campaign_buttons_include_the_online_button(clean):
    store, file_id = _store_with_video()
    campaign = broadcast.create_campaign(
        ADMIN_ID, text="hi", audience="all", files=[file_id], start=False,
        buttons="📢 Join | https://t.me/mychannel", online_button=1)
    from app.services import settings
    settings.set("BROADCAST_SEND_ONLINE_BUTTON", True)
    rows = broadcast.campaign_buttons(campaign)
    labels = [label for row in rows for label, _url in row]
    assert any("অনলাইন" in label for label in labels)
    assert any("Join" in label for label in labels)
    assert broadcast.online_button_url(campaign).startswith("https://t.me/")


def test_campaign_buttons_respect_the_switch(clean):
    """Both the per-campaign switch and the global default must work."""
    from app.services import settings
    store, file_id = _store_with_video()
    off = broadcast.create_campaign(ADMIN_ID, text="hi", files=[file_id], start=False,
                                    online_button=0)
    labels = [label for row in broadcast.campaign_buttons(off) for label, _ in row]
    assert not any("অনলাইন" in label for label in labels)

    settings.set("BROADCAST_SEND_ONLINE_BUTTON", False)
    follows_global = broadcast.create_campaign(ADMIN_ID, text="hi", files=[file_id],
                                               start=False)      # no explicit choice
    assert not any("অনলাইন" in label
                   for row in broadcast.campaign_buttons(follows_global) for label, _ in row)
    settings.reset("BROADCAST_SEND_ONLINE_BUTTON")


@pytest.mark.asyncio
async def test_media_campaign_really_sends_media_to_a_normal_user(clean, monkeypatch):
    """The bug the owner reported: only the admin got the test message."""
    from app.services import sender
    store, file_id = _store_with_video()
    campaign = broadcast.create_campaign(ADMIN_ID, text="New!", audience="all",
                                         files=[file_id], start=False)
    delivered: list[tuple] = []

    async def fake_deliver_file_id(chat_id, file_id_arg, caption_text=None, buttons=None):
        from app.services.sender import Delivery
        delivered.append((chat_id, file_id_arg, buttons))
        return Delivery(True)

    async def fake_ensure_ready(row):
        return row

    monkeypatch.setattr(sender, "deliver_file_id", fake_deliver_file_id)
    monkeypatch.setattr(sender, "ensure_ready", fake_ensure_ready)
    await broadcast._send_to_user(campaign, USER_ID)
    assert delivered and delivered[0][0] == USER_ID and delivered[0][1] == file_id
    assert delivered[0][2], "inline buttons (online button) must be attached"


@pytest.mark.asyncio
async def test_campaign_with_dead_file_fails_loudly(clean, monkeypatch):
    from app.services import sender
    store, file_id = _store_with_video()
    db.clear_mirror(file_id)
    campaign = broadcast.create_campaign(ADMIN_ID, text="New!", audience="all",
                                         files=[file_id], start=False)

    async def fake_ensure_ready(row):
        return row                                  # mirror impossible

    async def fake_deliver_file_id(chat_id, file_id_arg, caption_text=None, buttons=None):
        from app.services.sender import Delivery
        return Delivery(False, "no_session", "no userbot session")

    monkeypatch.setattr(sender, "ensure_ready", fake_ensure_ready)
    monkeypatch.setattr(sender, "deliver_file_id", fake_deliver_file_id)
    with pytest.raises(RuntimeError):
        await broadcast._send_to_user(campaign, USER_ID)


def test_audience_never_includes_admins(clean):
    store, _file_id = _store_with_video()
    db.touch_user(1234, "Normal", "normal")
    targets = broadcast.resolve_audience(ADMIN_ID, "all")
    assert ADMIN_ID not in targets and 1234 in targets


def test_text_campaign_personalises(clean):
    campaign = broadcast.create_campaign(ADMIN_ID, text="হ্যালো {name}", start=False)
    assert broadcast.preview(campaign, "Rahim") == "হ্যালো Rahim"


# --------------------------------------------------------------- channel posts
def test_parse_button_spec_builds_buttons_and_rows():
    from app.utils import parse_button_spec
    rows = parse_button_spec("📢 Join | https://t.me/mychannel\n"
                             "🛒 Store | https://t.me/bot?start=s1 && 💬 Admin | https://t.me/admin")
    assert rows[0] == [("📢 Join", "https://t.me/mychannel")]
    assert len(rows[1]) == 2
    assert parse_button_spec("no link here") == []


def test_composer_defaults_offer_a_store_button(clean):
    store, _file_id = _store_with_video()
    runtime.bot_username = "test_bot"
    payload = channels.composer_defaults(store["id"])
    assert store["name"] in payload["text"]
    assert "start=" in payload["buttons"]


@pytest.mark.asyncio
async def test_publish_posts_text_with_buttons(clean, monkeypatch):
    runtime.bot_username = "test_bot"
    sent: list[tuple] = []

    async def fake_send_text(peer, text, buttons=None, link_preview=False):
        sent.append((peer, text, buttons))
        return SimpleNamespace(id=321)

    monkeypatch.setattr(channels, "send_text", fake_send_text)
    result = await channels.publish(-100123456, text="নতুন ভিডিও",
                                    buttons="🟢 খুলুন | https://t.me/test_bot?start=s1")
    assert result["ok"] and result["message_id"] == 321
    assert sent[0][0] == -100123456
    labels = [label for label, _url in sent[0][2][0]]
    assert any("খুলুন" in label for label in labels)


@pytest.mark.asyncio
async def test_publish_with_files_uses_the_delivery_engine(clean, monkeypatch):
    store, file_id = _store_with_video()
    calls: list[tuple] = []

    async def fake_deliver(peer, row, caption=None, buttons=None):
        from app.services.sender import Delivery
        calls.append((peer, row["id"], caption, buttons))
        return Delivery(True)

    monkeypatch.setattr(channels, "deliver", fake_deliver)
    result = await channels.publish(-100999, text="Episode", file_ids=[file_id],
                                    buttons="🎬 দেখুন | https://t.me/test_bot?start=f1")
    assert result["ok"] and result["sent"] == 1
    assert calls[0][1] == file_id and calls[0][3]


@pytest.mark.asyncio
async def test_publish_without_content_is_rejected(clean):
    result = await channels.publish(-100999, text="", file_ids=[])
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_channel_footer_is_appended(clean, monkeypatch):
    from app.services import settings
    settings.set("CHANNEL_POST_FOOTER", "— Ahad Store")
    sent: list[tuple] = []

    async def fake_send_text(peer, text, buttons=None, link_preview=False):
        sent.append((peer, text))
        return SimpleNamespace(id=1)

    monkeypatch.setattr(channels, "send_text", fake_send_text)
    await channels.publish(-1001, text="Hello", buttons="")
    assert "Ahad Store" in sent[0][1]
    settings.reset("CHANNEL_POST_FOOTER")


def test_list_channels_merges_sources(clean):
    import asyncio
    store, _file_id = _store_with_video()
    db.set_store_forcejoin(store["id"], "@store_only")
    db.add_join_channel(None, "@global_one", title="Global One")
    db.add_bot_chat(-100777, "Scanned channel")
    listed = asyncio.run(_list_channels())
    refs = {row["ref"] for row in listed}
    assert "@global_one" in refs
    assert str(-100777) in refs or -100777 in {row["chat_id"] for row in listed}


async def _list_channels():
    return await channels.list_channels()


# ================================================ bot-side v3 screens (dispatch)
@pytest.fixture()
def bot_state(tmp_path, monkeypatch):
    from tests.test_flows import FakeEvent  # noqa: F401 — same harness as the flow tests
    db.bind(str(tmp_path / "botv3.sqlite3"))
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    runtime.bot_username = "test_bot"
    state.pending_input.clear()
    state.flow_ctx.clear()
    state.batch_mode.clear()
    state.search_pending.clear()
    db.touch_user(ADMIN_ID, "Owner", "owner")
    db.touch_user(USER_ID, "Tester", "tester")
    return monkeypatch


@pytest.mark.asyncio
async def test_bot_channels_screen_lists_channels(bot_state):
    from app.handlers.router import dispatch
    from tests.test_flows import FakeEvent
    db.add_join_channel(None, "@my_channel", title="My channel", chat_id=-100999)
    event = FakeEvent(ADMIN_ID, data=b"ch:home")
    await dispatch(event)
    assert event.edits and "চ্যানেল ম্যানেজমেন্ট" in event.edits[-1][0]
    labels = [btn.text for row in event.edits[-1][1] for btn in row]
    assert any("My channel" in label for label in labels)


@pytest.mark.asyncio
async def test_bot_add_channel_flow_creates_the_channel(bot_state, monkeypatch):
    from app.handlers import messages as messages_module
    from app.handlers.state import ask, pending_input as _pi  # noqa: F401
    from tests.test_flows import FakeEvent
    from app.services import channels as channels_service

    async def fake_add(ref, store_id=None, title=""):
        return {"ok": True, "id": 5, "chat_id": -100321, "title": "Typed channel"}
    monkeypatch.setattr(channels_service, "add_channel", fake_add)
    monkeypatch.setattr(messages_module, "channels", channels_service, raising=False)
    ask(ADMIN_ID, "add_channel")
    event = FakeEvent(ADMIN_ID, text="@typed_channel")
    await messages_module.pending_input(event)
    assert any("যোগ হয়েছে" in (text or "") for text, _ in event.sent)


@pytest.mark.asyncio
async def test_bot_limited_link_flow(bot_state):
    from app.handlers import messages as messages_module
    from app.handlers.state import ask
    from tests.test_flows import FakeEvent
    store, file_id = _store_with_video()
    db.set_active_store(ADMIN_ID, store["id"])
    ask(ADMIN_ID, "link_limit", file_id=file_id)
    event = FakeEvent(ADMIN_ID, text="100")
    await messages_module.pending_input(event)
    links = db.links(limit=5)
    assert links and links[0]["max_clicks"] == 100
    assert db.link_file_ids(links[0]["token"]) == [file_id]
    assert any("লিংক তৈরি" in (text or "") for text, _ in event.sent)


@pytest.mark.asyncio
async def test_bot_store_forcejoin_flow(bot_state):
    from app.handlers import messages as messages_module
    from app.handlers.state import ask
    from tests.test_flows import FakeEvent
    store, _file_id = _store_with_video()
    ask(ADMIN_ID, "store_forcejoin", store_id=store["id"])
    event = FakeEvent(ADMIN_ID, text="https://t.me/+AbCdEfGhIjK")
    await messages_module.pending_input(event)
    assert db.store_forcejoin(store["id"]) == "https://t.me/+AbCdEfGhIjK"
    assert "https://t.me/+AbCdEfGhIjK" in {t["ref"] for t in forcejoin.targets(store["id"])}


@pytest.mark.asyncio
async def test_bot_analytics_screens(bot_state):
    from app.handlers.router import dispatch
    from tests.test_flows import FakeEvent
    store, file_id = _store_with_video()
    db.touch_user(777, "Watcher", "watcher")
    db.log_event("deliver", USER_ID, store["id"], file_id, "button")
    db.log_event("paid", USER_ID, store["id"], None, "plan:1")

    event = FakeEvent(ADMIN_ID, data=b"an:home")
    await dispatch(event)
    assert "অ্যানালিটিক্স" in event.edits[-1][0]

    event = FakeEvent(ADMIN_ID, data=f"an:store:{store['id']}".encode())
    await dispatch(event)
    assert "ভিডিও দেখেছে" in event.edits[-1][0]

    event = FakeEvent(ADMIN_ID, data=f"an:file:{file_id}".encode())
    await dispatch(event)
    assert "যারা পেয়েছে" in event.edits[-1][0]
    assert "Tester" in event.edits[-1][0]


@pytest.mark.asyncio
async def test_join_confirm_button_delivers_the_video(bot_state, monkeypatch, collected_sends,
                                                      recorded_deliveries):
    from app.handlers.router import dispatch
    from tests.test_flows import FakeEvent
    store, file_id = _store_with_video()
    db.set_store_forcejoin(store["id"], "@store_only")

    async def joined(user_id, store_id=None):
        return {"ok": True, "missing": [], "skipped": 0, "checked": 1}

    monkeypatch.setattr(forcejoin, "check_many", joined)
    event = FakeEvent(USER_ID, data=f"fj:{USER_ID}:{file_id}".encode())
    await dispatch(event)
    assert recorded_deliveries == [(USER_ID, file_id)]


@pytest.mark.asyncio
async def test_limited_link_click_refuses_after_the_limit(bot_state, monkeypatch,
                                                          collected_sends,
                                                          recorded_deliveries):
    store, file_id = _store_with_video()
    token = db.create_link(ADMIN_ID, [file_id], None, kind="limited", max_clicks=1)

    event = GateEvent(USER_ID)
    assert await flow.open_link(event, token) == flow.GATE_OK
    assert recorded_deliveries == [(USER_ID, file_id)]

    after = GateEvent(USER_ID)
    assert await flow.open_link(after, token) == flow.GATE_LIMIT
    assert "লিমিট" in collected_sends[-1][1]
    assert recorded_deliveries == [(USER_ID, file_id)]          # nothing more sent
    assert db.events(name="limit_block", user_id=USER_ID)


# ============================================ uploads: the “text keeps saving” bug
@pytest.mark.asyncio
async def test_admin_text_message_is_never_saved(bot_state, monkeypatch):
    """A text (or link-preview) message must not create a store item."""
    from app.handlers import messages as messages_module
    from tests.test_flows import FakeEvent
    store, _file_id = _store_with_video()
    db.set_active_store(ADMIN_ID, store["id"])
    before = len(db.files_of(store["id"]))

    event = FakeEvent(ADMIN_ID, text="https://example.com/whatever")
    event.media = object()                       # Telegram sets .media for link previews
    event.message = SimpleNamespace(id=91, media=event.media, message="", file=None,
                                    video=None, photo=None, audio=None, voice=None,
                                    document=None, sticker=None, gif=None, poll=None,
                                    geo=None, contact=None)
    event.message.media = SimpleNamespace(webpage=object())
    await messages_module.media_upload(event)

    assert len(db.files_of(store["id"])) == before        # nothing saved
    assert any("সেভ হয় না" in (text or "") for text, _ in event.sent)


@pytest.mark.asyncio
async def test_admin_video_message_is_saved_once(bot_state, monkeypatch):
    from app.handlers import messages as messages_module
    from tests.test_flows import FakeEvent
    store, _file_id = _store_with_video()
    db.set_active_store(ADMIN_ID, store["id"])
    before = len(db.files_of(store["id"]))

    async def no_mirror(file_id):
        return None
    monkeypatch.setattr(messages_module, "spawn", lambda coro: coro.close())

    event = FakeEvent(ADMIN_ID)
    event.media = object()
    event.message = SimpleNamespace(
        id=123, message="Episode 9 HD", file=SimpleNamespace(size=555),
        video=SimpleNamespace(duration=120), photo=None, audio=None, voice=None,
        document=None, sticker=None, gif=None, poll=None, geo=None, contact=None,
    )
    event.message.media = SimpleNamespace(document=object())
    await messages_module.media_upload(event)
    files = db.files_of(store["id"])
    assert len(files) == before + 1
    assert files[-1]["name"] == "Episode 9 HD" and files[-1]["kind"] == "Video"

    # sending the very same message again must not duplicate it
    await messages_module.media_upload(event)
    assert len(db.files_of(store["id"])) == before + 1


@pytest.mark.asyncio
async def test_scanner_ignores_audio_and_documents(clean):
    from app.services import scanner
    store, _file_id = _store_with_video()
    audio = FakeMessage(audio=True)
    document = FakeMessage(document=True)
    video = _video_message("Scan me")
    assert scanner._kind_of(audio) is None
    assert scanner._kind_of(document) is None
    assert scanner._kind_of(video) == "Video"


@pytest.mark.asyncio
async def test_store_open_is_gated_and_logged(clean, monkeypatch, collected_sends):
    store, file_id = _store_with_video()
    db.set_store_forcejoin(store["id"], "@scoped")

    async def missing(user_id, store_id=None):
        return {"ok": False, "checked": 1, "skipped": 0,
                "missing": [{"ref": "@scoped", "title": "Scoped",
                             "invite": "https://t.me/scoped", "id": 0}]}

    monkeypatch.setattr(forcejoin, "check_many", missing)
    event = GateEvent(USER_ID)
    await flow.open_store(event, store["id"])
    assert collected_sends and "জয়েন" in collected_sends[-1][1]
    assert db.events(name="open_store", user_id=USER_ID)
    assert db.events(name="join_block", user_id=USER_ID)


def test_gate_text_escapes_channel_titles_but_keeps_html():
    text = forcejoin.gate_text([{"title": "A <b>bad</b> title", "ref": "@x"}])
    assert "<b>ফাইলটি পেতে আগে চ্যানেল জয়েন করুন</b>" in text   # our own markup kept
    assert "&lt;b&gt;bad&lt;/b&gt;" in text                       # user data escaped
    again = forcejoin.gate_text([{"title": "T", "ref": "@x"}], "again")
    assert "এখনো জয়েন করেননি" in again


# ===================================================== broadcast: real delivery
class MediaClient:
    """Offline stand-in for the bot used by the delivery path."""

    def __init__(self, blocked: set[int] | None = None) -> None:
        self.blocked = blocked or set()
        self.media_sent: list[tuple[int, object]] = []
        self.texts: list[tuple[int, str]] = []
        self.parse_mode = "html"

    async def get_messages(self, source=None, ids=None, **_kw):
        return SimpleNamespace(media="<media>", id=ids)

    async def get_input_entity(self, entity):
        return entity

    async def send_message(self, chat_id, text=None, **_kw):
        if chat_id in self.blocked:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.texts.append((chat_id, text))
        return SimpleNamespace(id=len(self.texts))

    async def __call__(self, _request):
        return True


@pytest.mark.asyncio
async def test_media_broadcast_really_reaches_users(clean, monkeypatch):
    """The exact complaint: “only my test message arrives, nobody else gets it”.

    A media campaign must go out as the **bot** (mirror → plain send) for every
    user, and everyone who cannot be reached must show up with a reason.
    """
    from app.services import broadcast, sender
    store, file_id = _store_with_video()
    db.set_mirror(file_id, 999000, 42)                      # pre-warmed copy
    for user_id in (701, 702, 703):
        db.touch_user(user_id, f"বন্ধু{user_id}")

    client = MediaClient(blocked={702})
    monkeypatch.setattr(runtime, "_client", client)

    async def fake_media(chat_id, media, caption_text, buttons=None):
        if chat_id in client.blocked:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        client.media_sent.append((chat_id, media))

    monkeypatch.setattr(sender, "_send_media", fake_media)

    admin_id = 999
    db.touch_user(admin_id, "Admin", "admin")
    campaign = broadcast.create_campaign(admin_id, text="নতুন ভিডিও!",
                                         audience="all", files=[file_id], start=False)
    result = await broadcast.run_campaign(campaign["id"])

    delivered = {chat for chat, _ in client.media_sent}
    assert {701, 703, USER_ID} <= delivered, delivered
    assert 702 not in delivered                       # blocked → skipped
    assert result["status"] == "done" and result["sent"] >= 3
    rows = {item["user_id"]: item for item in db.campaign_items(campaign["id"], limit=999)}
    assert rows[702]["status"] == "blocked"
    hint = broadcast.explain_error(rows[702]["error"])
    assert "ব্লক" in hint
    assert "blocked" not in hint.lower()              # explained in the admin's own words


@pytest.mark.asyncio
async def test_broadcast_explains_why_someone_was_missed(clean, monkeypatch):
    from app.services import broadcast, sender
    store, file_id = _store_with_video()
    db.set_mirror(file_id, 999000, 43)
    for user_id, reason in ((801, "Cannot find any entity corresponding to 801"),
                            (802, "Forbidden: bot was blocked by the user")):
        db.touch_user(user_id, f"U{user_id}")

    client = MediaClient()
    monkeypatch.setattr(runtime, "_client", client)

    async def fake_media(chat_id, media, caption_text, buttons=None):
        if chat_id in (801, 802):
            raise RuntimeError(dict(((801, "Cannot find any entity corresponding to 801"),
                                     (802, "Forbidden: bot was blocked by the user")))[chat_id])

    monkeypatch.setattr(sender, "_send_media", fake_media)
    campaign = broadcast.create_campaign(999, text="x", audience="all",
                                         files=[file_id], start=False)
    await broadcast.run_campaign(campaign["id"])
    breakdown = broadcast.failure_breakdown(campaign["id"])
    hints = " ".join(group["hint"] for group in breakdown)
    assert "পৌঁছানো যায় না" in hints and "ব্লক" in hints


# ------------------------------------------------------- entry points stay sane
def test_entry_points_can_reach_the_scheduler():
    """Regression: `bot.py` called `scheduler.start_all()` without importing it,
    so the deployed bot crashed with `NameError` right after connecting."""
    import bot as bot_entry
    from app.services import scheduler

    assert bot_entry.scheduler is scheduler
    # …and the function it calls really exists.
    assert callable(scheduler.start_all)
    assert callable(scheduler.run_digest)


# ------------------------------------------------- screens must render (no NameError)
@pytest.mark.asyncio
async def test_payment_settings_screen_renders(clean):
    """Regression: `settings.get(k)` inside a comprehension crashed the whole
    💳 Payment settings screen in the bot with `NameError: name 'k'`."""
    from app.handlers import billing as billing_handlers
    from tests.test_flows import FakeEvent

    event = FakeEvent(ADMIN_ID)
    await billing_handlers.payment_settings(event, "")
    assert event.edits, "screen never rendered"
    text, buttons = event.edits[-1]
    assert "পেমেন্ট" in text
    assert any("bKash" in row[0].text for row in buttons if row)


# ------------------------------------------------------ scheduler / digest / cache
@pytest.mark.asyncio
async def test_digest_queues_a_campaign_with_the_weeks_new_videos(clean):
    from app.services import settings, scheduler
    store, file_id = _store_with_video()
    for user_id in (901, 902):
        db.touch_user(user_id, f"U{user_id}")
    settings.set("DIGEST_MAX_FILES", 2)
    # store:1 audience needs a *reason* to include people — a free store is open
    db.update_store(store["id"], is_premium=0)

    report = await scheduler.run_digest(force=True)
    assert report["ok"] and report["queued"]
    entry = report["queued"][0]
    assert entry["store"] == "Movies"
    assert entry["files"] == 1                      # only the fresh video
    campaign = db.campaign(entry["campaign"])
    assert campaign is not None
    assert campaign["status"] in ("queued", "running", "done")
    assert campaign["file_ids"] == [file_id]        # newest_first really works
    settings.reset("DIGEST_MAX_FILES")


def test_scheduler_starts_every_worker(clean):
    import asyncio
    from app.services import scheduler

    async def run():
        tasks = scheduler.start_all()
        assert len(tasks) == 9
        assert all(isinstance(t, asyncio.Task) for t in tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_duplicate_cleaner_removes_only_the_extras(clean):
    store, file_id = _store_with_video()
    # A duplicate rows can only exist from an older version / manual import, so
    # create one straight in the table the way the old buggy code did.
    db._run(
        "INSERT INTO files(store_id, name, kind, chat_id, msg_id, created_at, views) "
        "VALUES(?, ?, ?, ?, ?, ?, 0)",
        (store["id"], "Movie 1 copy", "Video", -100, 10, time.time()))
    assert len(db.duplicate_files()) == 1
    removed = db.purge_duplicate_files()
    assert removed == 1
    assert db.duplicate_files() == []
    assert len(db.files_of(store["id"])) == 1
    assert db.file(file_id) is not None             # the original stays


# ------------------------------------------------------- per-user link limits
def test_per_user_limit_stops_one_person_burning_every_click(clean):
    store, file_id = _store_with_video()
    token = db.create_link(ADMIN_ID, [file_id], None, kind="limited",
                           max_clicks=100, per_user_limit=2)
    assert db.link_take(token, user_id=21)["ok"]
    assert db.link_take(token, user_id=21)["ok"]
    third = db.link_take(token, user_id=21)
    assert third["ok"] is False and third["reason"] == "user_limit"
    assert third["per_user_limit"] == 2 and third["used_by_user"] == 2
    # …but the rest of the batch is untouched for everybody else
    assert db.link_take(token, user_id=22)["ok"]
    uses = {row["user_id"]: row["times"] for row in db.link_uses(token)}
    assert uses == {21: 2, 22: 1}
    assert db.link_clicks(token) == 3


@pytest.mark.asyncio
async def test_repeat_visitor_gets_a_kind_explanation(clean, collected_sends,
                                                      recorded_deliveries):
    store, file_id = _store_with_video()
    db.update_store(store["id"], is_premium=0)
    token = db.create_link(ADMIN_ID, [file_id], None, kind="limited",
                           max_clicks=50, per_user_limit=1)
    event = GateEvent(USER_ID)
    assert await flow.open_link(event, token) == flow.GATE_OK
    assert recorded_deliveries == [(USER_ID, file_id)]

    again = GateEvent(USER_ID)
    state_value = await flow.open_link(again, token)
    assert state_value == flow.GATE_LIMIT
    assert recorded_deliveries == [(USER_ID, file_id)]          # nothing new sent
    assert "আপনি ইতিমধ্যেই নিয়ে নিয়েছেন" in collected_sends[-1][1]
    blocks = db.events(name="limit_block", user_id=USER_ID)
    assert blocks, "the refusal must show up in the analytics"
    assert any("per_user" in (row.get("detail") or "") for row in blocks)


@pytest.mark.asyncio
async def test_bot_link_flow_accepts_total_slash_per_user(clean, monkeypatch):
    """`100/1` in the bot = 100 clicks total, one per person."""
    from app.handlers import messages as messages_module
    from tests.test_flows import FakeEvent
    store, file_id = _store_with_video()
    state.ask(ADMIN_ID, "link_limit", file_id=file_id)
    event = FakeEvent(ADMIN_ID, text="100/1")
    await messages_module.pending_input(event)
    link = db.links(limit=1)[0]
    assert link["max_clicks"] == 100 and link["per_user_limit"] == 1
    assert "একজন সর্বোচ্চ 1 বার" in event.sent[-1][0]


@pytest.mark.asyncio
async def test_owner_report_reads_real_numbers_and_warns(clean, monkeypatch):
    """The daily DM: the five numbers that matter, plus what needs fixing."""
    from app.services import scheduler

    store, file_id = _store_with_video()
    db.log_event("deliver", USER_ID, store["id"], file_id, "direct")
    db.log_event("join_block", USER_ID, store["id"], file_id, "@chan")
    db.log_event("limit_block", USER_ID, store["id"], file_id, "tok", "per_user=1")

    sent: list[tuple[int, str]] = []

    class ReportBot:
        async def send_message(self, chat_id, text=None, **_kw):
            sent.append((chat_id, text))
            return True

    monkeypatch.setattr(scheduler, "bot", ReportBot())
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    text = await scheduler.send_owner_report()
    assert sent and sent[0][0] == ADMIN_ID
    assert "দৈনিক রিপোর্ট" in text
    assert "ভিডিও পাঠানো: <b>1</b>" in text
    assert "চ্যানেল গেটে আটকেছে" in text
    assert "লিমিট শেষ" in text
    assert "ইউজারবট সেশন নেই" in text                # no session → real warning
    assert "ক্যাশ তৈরি: <b>1</b>" in text            # the mirrored file is counted
    assert db.get_meta("owner_report_text")


@pytest.mark.asyncio
async def test_renewal_notice_is_bengali_by_default(clean, monkeypatch):
    import time as _time
    from app.services import scheduler

    store, _file_id = _store_with_video()
    db.grant(store["id"], USER_ID, _time.time() + 3600, "test")
    sent: list[tuple[int, str]] = []

    class ReminderBot:
        async def send_message(self, chat_id, text=None, **_kw):
            sent.append((chat_id, text))
            return True

    monkeypatch.setattr(scheduler, "bot", ReminderBot())
    for grant in db.expired_grants(within_seconds=3 * 86400):
        from app import i18n
        from app.utils import esc as _esc, fmt_ts as _fmt
        await ReminderBot().send_message(
            grant["user_id"],
            i18n.t(grant["user_id"], "renewal_notice", store=_esc(grant["store_name"]),
                   date=_fmt(grant["expires_at"])))
    assert sent and "অ্যাক্সেস" in sent[0][1]


# ---------------------------------------------------- channel composer (posts)
@pytest.mark.asyncio
async def test_channel_post_carries_inline_buttons_and_footer(clean, monkeypatch):
    from app.services import channels as channels_service, settings
    store, file_id = _store_with_video()
    settings.set("CHANNEL_POST_FOOTER", "📢 আমাদের চ্যানেল")
    captured = []

    async def fake_send_text(peer, body, buttons=None, link_preview=False):
        captured.append((peer, body, buttons))
        return SimpleNamespace(id=77)

    monkeypatch.setattr(channels_service, "send_text", fake_send_text)
    result = await channels_service.publish(
        "@my_channel", text="নতুন ভিডিও এসেছে 🎬",
        buttons="🔗 সাইট | https://example.com && 📢 চ্যানেল | https://t.me/my_channel")
    assert result["ok"] and result["message_id"] == 77
    peer, body, buttons = captured[0]
    assert peer == "@my_channel" and "📢 আমাদের চ্যানেল" in body
    assert len(buttons) == 1 and len(buttons[0]) == 2           # && = same row
    assert buttons[0][0][0] == "🔗 সাইট" and buttons[0][1][1] == "https://t.me/my_channel"
    settings.reset("CHANNEL_POST_FOOTER")


@pytest.mark.asyncio
async def test_channel_post_in_link_mode_hides_the_media(clean, monkeypatch):
    """“ভিডিও না দিয়ে লিংক দিন (১০০ বার)” — the channel gets the button only."""
    from app.services import channels as channels_service
    store, file_id = _store_with_video()
    delivered = []

    async def fake_deliver(*_args, **_kwargs):
        delivered.append(_args)
        from app.services.sender import Delivery
        return Delivery(True)

    async def fake_send_text(peer, body, buttons=None, link_preview=False):
        return SimpleNamespace(id=88)

    monkeypatch.setattr(channels_service, "deliver", fake_deliver)
    monkeypatch.setattr(channels_service, "send_text", fake_send_text)
    monkeypatch.setattr(runtime, "bot_username", "test_bot")
    result = await channels_service.publish("@c", text="নতুন", file_ids=[file_id],
                                            link_limit=100)
    assert result["ok"] and delivered == []                    # no media in the channel
    link = db.links(limit=1)[0]
    assert link["max_clicks"] == 100
    assert link_file_ids_link(link["token"]) == [file_id]


def link_file_ids_link(token: str) -> list[int]:
    """The link points at the same file the channel post was about."""
    return db.link_file_ids(token)


@pytest.mark.asyncio
async def test_channel_post_with_file_delivers_it_with_buttons(clean, monkeypatch):
    from app.services import channels as channels_service
    from app.services.sender import Delivery
    store, file_id = _store_with_video()
    calls = []

    async def fake_deliver(peer, row, caption, buttons=None):
        calls.append((peer, row["id"], caption, buttons))
        return Delivery(True)

    monkeypatch.setattr(channels_service, "deliver", fake_deliver)
    result = await channels_service.publish("@c", text="একদম নতুন", file_ids=[file_id],
                                            buttons="🔗 দেখুন | https://example.com")
    assert result["ok"] and result["sent"] == 1
    peer, sent_file, caption, buttons = calls[0]
    assert sent_file == file_id and "একদম নতুন" in caption
    assert buttons and buttons[0][0][1] == "https://example.com"


# ------------------------------------------------- 📝 editable bot texts (panel)
def test_every_i18n_key_is_listed_and_editable(clean):
    """“একটা লাইন বদলানোর জন্য পুরো সিস্টেম বানাতে হয়” — no more."""
    from app import i18n
    from app.services import bot_texts

    catalog = [row for group in bot_texts.catalog() for row in group["rows"]]
    keys = {row["key"] for row in catalog}
    assert set(i18n.STRINGS) <= keys                     # nothing is left out
    assert {"media_only", "gate_title", "join_button"} <= keys
    for key in list(i18n.STRINGS)[:10]:                  # every one has a default
        assert bot_texts.default(key, "bn")


def test_an_owner_edit_wins_over_the_builtin_text(clean):
    from app import i18n
    from app.services import bot_texts

    bot_texts.set("home_banner", "স্বাগতম ভাই! 👋")
    assert i18n.t(USER_ID, "home_banner") == "স্বাগতম ভাই! 👋"
    bot_texts.set("media_only", "❌ শুধু ভিডিও বা ছবি দিন ভাই।")
    from app.services import media_guard
    assert "শুধু ভিডিও বা ছবি দিন ভাই" in media_guard.reject_text("text")
    bot_texts.set("join_button", "🔔 জয়েন করলাম")
    from app.services import forcejoin
    buttons = forcejoin.gate_keyboard(USER_ID, 1, [{"title": "A", "ref": "@a",
                                                    "invite": "https://t.me/a"}])
    assert buttons[-1][0].text == "🔔 জয়েন করলাম"
    assert bot_texts.set("home_banner", "-")[0] is True
    assert i18n.t(USER_ID, "home_banner") == i18n.STRINGS["home_banner"]["bn"]
    assert "স্টোরে স্বাগতম" in i18n.t(USER_ID, "home_banner")


def test_placeholders_still_work_after_an_edit(clean):
    from app.services import bot_texts

    bot_texts.set("gate_title", "আগে {channels} জয়েন করুন")
    text = bot_texts.render("gate_title", channels="  • My Channel")
    assert "My Channel" in text
    bot_texts.set("gate_title", "-")


def test_the_panel_lists_grouped_texts_with_a_reset(clean):
    from app.services import bot_texts

    groups = bot_texts.catalog()
    assert groups and all(group["group"] and group["rows"] for group in groups)
    row = next(r for g in groups for r in g["rows"] if r["key"] == "home_banner")
    assert row["edited"] is False and row["default"]
    bot_texts.set("home_banner", "নতুন লেখা")
    row = next(r for g in bot_texts.catalog() for r in g["rows"] if r["key"] == "home_banner")
    assert row["edited"] is True and row["value"] == "নতুন লেখা"
    assert bot_texts.reset_all() >= 1
    assert bot_texts.override("home_banner") == ""


# ------------------------------------------------------------------- 🔔 alerts
def test_alerts_are_switchable_one_by_one(clean):
    from app.services import alerts, settings as settings_service

    assert alerts.enabled("new_user") is True
    settings_service.set("ALERT_NEW_USER", "false")
    assert alerts.enabled("new_user") is False
    assert alerts.enabled("order") is True               # only that one is off
    settings_service.set("ALERT_ENABLED", "false")
    assert alerts.enabled("order") is False
    settings_service.set("ALERT_ENABLED", "true")
    settings_service.set("ALERT_NEW_USER", "true")


def test_the_same_alert_is_not_repeated_within_the_cooldown(clean):
    from app.services import alerts

    alerts._last_sent.clear()
    assert alerts.throttled("stuck", "42") is False
    alerts.mark("stuck", "42")
    assert alerts.throttled("stuck", "42") is True
    assert alerts.throttled("stuck", "43") is False      # another user gets through
    alerts._last_sent.clear()


@pytest.mark.asyncio
async def test_a_new_order_and_new_user_alert_reach_the_admin_inbox(clean, monkeypatch):
    from app import runtime
    from app.services import alerts, settings as settings_service

    sent: list[tuple] = []

    class StubBot:
        async def send_message(self, target, text, **kwargs):
            sent.append((target, text))
            return object()

    monkeypatch.setattr(runtime, "bot", StubBot(), raising=False)
    settings_service.set("ALERT_CHANNEL", "@admin_inbox")
    await alerts.new_user(USER_ID, "Rahim", "rahim")
    await alerts.new_order({"id": 5, "user_id": USER_ID, "amount": 199, "currency": "৳"})
    assert sent and all(target == "@admin_inbox" for target, _ in sent)
    assert any("নতুন ইউজার" in text for _, text in sent)
    assert any("নতুন অর্ডার #5" in text for _, text in sent)
    settings_service.set("ALERT_CHANNEL", "")
    alerts._last_sent.clear()


# ------------------------------------------- hosting panel live URL (/live/<job>)
@pytest.fixture()
def client_factory(tmp_path, monkeypatch):
    """A TestClient on a throw-away database (FastAPI is optional in this file)."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from app import config as cfg

    def make():
        db.bind(str(tmp_path / "panel.sqlite3"))
        monkeypatch.setattr(cfg, "BOT_TOKEN", "123456789:" + "A" * 35)
        monkeypatch.setattr(cfg, "API_ID", 12345)
        monkeypatch.setattr(cfg, "API_HASH", "0" * 32)
        monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
        import web.dashboard as dashboard
        monkeypatch.setattr(dashboard, "ADMIN_LOGIN_PASSWORD", "secret")
        return TestClient(dashboard.app)

    return make

def test_the_panel_prefix_is_normalized_safely():
    from app.services import basepath

    assert basepath.normalize("/live/u16-b-2576c1") == "/live/u16-b-2576c1"
    assert basepath.normalize("/live/abc/") == "/live/abc"
    assert basepath.normalize("/") == ""
    assert basepath.normalize("live/abc") == ""          # must be root-absolute
    assert basepath.normalize("/live/../../etc") == ""   # no escaping the prefix
    assert basepath.normalize("/" + "x" * 400) == ""


def test_links_stay_inside_the_panel_prefix():
    from app.services import basepath

    html = ('<a href="/admin/files">f</a><a href=\'/admin/users\'>u</a>'
            '<form action="/admin/login"></form><form action=/admin/save></form>'
            '<img src="//cdn.example/p.png"><a href="https://x/y">out</a>'
            "<script>fetch('/admin/api/campaign/7'); location.href='/help';"
            "const path = '/admin/untouched';</script>")
    basepath.set_prefix("/live/job1")
    try:
        out = basepath.relativize(html)
        assert 'href="admin/files"' in out           # double quotes
        assert "href='admin/users'" in out           # single quotes
        assert 'action="admin/login"' in out
        assert 'action=admin/save' in out            # unquoted
        assert "fetch('admin/api/campaign/7')" in out
        assert "location.href='help'" in out
        # external links, protocol-relative assets and non-URL strings are kept
        assert 'src="//cdn.example/p.png"' in out
        assert 'href="https://x/y"' in out
        assert "const path = '/admin/untouched'" in out
        assert basepath.url("/health") == "/live/job1/health"
    finally:
        basepath.set_prefix("")


def test_the_page_carries_the_prefix_as_its_base_url(client_factory):
    """With `X-Forwarded-Prefix` the browser must stay under /live/<job>/."""
    client = client_factory()
    page = client.get("/help", headers={"X-Forwarded-Prefix": "/live/u16-b-2576c1"})
    assert page.status_code == 200
    assert '<base href="/live/u16-b-2576c1/">' in page.text
    assert 'href="help"' in page.text                  # public navigation stays prefixed
    assert 'href="admin"' not in page.text             # admin entry is intentionally hidden
    assert 'href="/admin"' not in page.text             # root-absolute would escape
    plain = client.get("/help")
    assert '<base href="/">' in plain.text              # normal run is unchanged
