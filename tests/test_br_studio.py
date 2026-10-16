"""`/br` studio & `/bm` media inbox, driven through the real handlers.

These are the flows the owner actually taps: open the studio, drop a video, type
the message, press send. Everything runs offline against a fake Telegram client.
"""
from __future__ import annotations

import time
from contextlib import suppress
from types import SimpleNamespace

import pytest
from telethon.tl.types import Document, DocumentAttributeVideo, MessageMediaDocument

from telethon import events

from app import config as cfg, runtime
from app.handlers import br as br_handlers
from app.handlers import state
from app.services import broadcast, quickcast
from app.services.quickcast import Content
from app.storage import db
from tests.test_quickcast import FakeClient, FakeMessage, video_document

ADMIN_ID = 900
USERS = (21, 22)


async def call(handler, event, *args) -> None:
    """Run a handler the way Telethon does — `StopPropagation` is normal here."""
    with suppress(events.StopPropagation):
        await handler(event, *args)


class MatchStub:
    """Stands in for `event.pattern_match` (`group(1)`, `group(2)`, `group(3)`)."""

    def __init__(self, *groups) -> None:
        self._groups = groups

    def group(self, index: int):
        return self._groups[index - 1] if 0 < index <= len(self._groups) else None


class BrEvent:
    """A private message from the admin, as Telethon would deliver it."""

    def __init__(self, user_id: int = ADMIN_ID, text: str = "", media=None,
                 groups=("", "", "")) -> None:
        self.sender_id = user_id
        self.chat_id = user_id
        self.is_private = True
        self.out = False
        self.raw_text = text
        self.pattern_match = MatchStub(*groups)
        self.message = SimpleNamespace(id=77, chat_id=user_id, media=media,
                                       entities=None, forward=None,
                                       file=SimpleNamespace(name="promo.mp4", size=2048))
        self.sent: list[tuple] = []
        self.edits: list[tuple] = []
        self.answers: list[tuple] = []

    async def respond(self, text=None, buttons=None, **_kwargs):
        self.sent.append((text, buttons))
        return SimpleNamespace(id=1)

    async def edit(self, text=None, buttons=None, **_kwargs):
        self.edits.append((text, buttons))
        return SimpleNamespace(id=1)

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))


@pytest.fixture()
def studio(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "studio.sqlite3"))
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    runtime.blocked_users.clear()
    state.pending_input.clear()
    state.flow_ctx.clear()
    br_handlers.sessions.clear()
    for uid in USERS:
        db.set_user_peer(uid, 1000 + uid, f"U{uid}")
    db.set_user_peer(ADMIN_ID, 999, "Owner")
    client = FakeClient(known={uid: 1000 + uid for uid in (*USERS, ADMIN_ID)})
    runtime.set_client(client)
    yield client
    runtime.set_client(None)


def _video() -> MessageMediaDocument:
    return MessageMediaDocument(document=video_document())


# ------------------------------------------------------------------ the studio
@pytest.mark.asyncio
async def test_br_opens_the_studio_with_a_keyboard(studio):
    event = BrEvent()
    await call(br_handlers.br_start, event)

    assert event.sent, "the studio said nothing"
    text, buttons = event.sent[-1]
    assert "ব্রডকাস্ট" in text and buttons, "no inline keyboard"
    assert ADMIN_ID in br_handlers.sessions                     # studio stays open


@pytest.mark.asyncio
async def test_text_media_and_buttons_land_in_the_draft(studio):
    """Every message the admin drops is one draft item (ported from broadcast.py)."""
    await call(br_handlers.br_start, BrEvent())

    await call(br_handlers.br_collect, BrEvent(text="<b>আজকের ভিডিও</b>"))
    await call(br_handlers.br_collect, BrEvent(media=_video()))
    await call(br_handlers.br_collect, BrEvent(text="স্টোর | https://t.me/x"))

    draft = quickcast.load_draft(ADMIN_ID)
    assert draft is not None
    # first a text message, then the video — in that order, each on its own
    assert [item.kind for item in draft.items] == ["text", "video"]
    assert "আজকের ভিডিও" in draft.items[0].text
    assert draft.items[1].media_key.endswith(":77")
    assert draft.buttons == [[("স্টোর", "https://t.me/x")]]

    card = br_handlers._card(draft, ADMIN_ID)
    assert "আলাদা মেসেজ" in card and "স্টোর" not in card.split("কনটেন্ট")[0]


@pytest.mark.asyncio
async def test_a_typed_html_message_keeps_its_formatting(studio):
    """The archive kept the admin's own HTML (and converted Telegram's styling)."""
    await call(br_handlers.br_start, BrEvent())
    await call(br_handlers.br_collect, BrEvent(text="<b>bold</b> & <i>italic</i>"))
    draft = quickcast.load_draft(ADMIN_ID)
    assert draft.items[0].text == "<b>bold</b> & <i>italic</i>"

    await call(br_handlers.br_collect, BrEvent(text="<plain> & unsafe"))
    draft = quickcast.load_draft(ADMIN_ID)
    assert draft.items[1].text == "&lt;plain&gt; &amp; unsafe"


@pytest.mark.asyncio
async def test_braudience_and_options_commands(studio):
    await call(br_handlers.br_start, BrEvent())

    await call(br_handlers.br_command,
               BrEvent(text="/braudience premium", groups=("audience", None, "premium")))
    await call(br_handlers.br_command, BrEvent(text="/brsilent on", groups=("silent", None, "on")))

    draft = quickcast.load_draft(ADMIN_ID)
    assert draft.audience == "premium" and draft.silent is True


@pytest.mark.asyncio
async def test_brsend_actually_delivers_and_reports(studio):
    await call(br_handlers.br_start, BrEvent())
    await call(br_handlers.br_collect, BrEvent(text="hello everyone"))

    event = BrEvent(text="/brsend", groups=("send", None, ""))
    await call(br_handlers.br_command, event)

    assert studio.sent_messages, "nothing was sent to Telegram"
    assert sorted(uid for uid, _ in studio.sent_messages) == sorted(USERS)
    joined = (" ".join(str(text) for text, _buttons in event.sent) + " "
              + " ".join(str(edit[0]) for edit in event.edits))
    assert "শেষ" in joined or "গেছে" in joined or "পাঠানো" in joined
    campaign = db.campaigns(limit=1)[0]
    assert campaign["origin"] == "br"


@pytest.mark.asyncio
async def test_brtest_only_touches_admins(studio):
    await call(br_handlers.br_start, BrEvent())
    await call(br_handlers.br_collect, BrEvent(text="test run"))

    await call(br_handlers.br_command, BrEvent(text="/brtest", groups=("test", None, "")))

    assert all(uid == ADMIN_ID for uid, _ in studio.sent_messages)
    assert len(studio.sent_messages) == 1


@pytest.mark.asyncio
async def test_an_empty_studio_refuses_to_send(studio):
    await call(br_handlers.br_start, BrEvent())
    event = BrEvent(text="/brsend", groups=("send", None, ""))
    await call(br_handlers.br_command, event)

    assert not studio.sent_messages
    assert any("কিছু পাঠাননি" in (text or "") for text, _ in event.sent)


# -------------------------------------------------------------------- inbox
@pytest.mark.asyncio
async def test_bm_inbox_stores_media_for_the_website(studio):
    await call(br_handlers.bm_command, BrEvent(text="/bm add", groups=("bm", None, "add")))
    assert (state.peek(ADMIN_ID) or {}).get("action") == "bc_media"

    await call(br_handlers.bm_capture, BrEvent(media=_video()))

    items = db.bc_media_items()
    assert items and items[0]["kind"] == "video" and items[0]["name"] == "promo.mp4"
    assert quickcast.load_draft(ADMIN_ID).media, "the inbox item did not reach the draft"


@pytest.mark.asyncio
async def test_inbox_button_attaches_to_the_draft(studio):
    await call(br_handlers.bm_command, BrEvent(text="/bm add", groups=("bm", None, "add")))
    await call(br_handlers.bm_capture, BrEvent(media=_video()))
    quickcast.drop_draft(ADMIN_ID)

    item_id = db.bc_media_items()[0]["id"]
    event = BrEvent(text="", groups=())
    await call(br_handlers.bm_callback, event, f"use:{item_id}")

    draft = quickcast.load_draft(ADMIN_ID)
    assert draft and draft.media and draft.media[0].msg_id == 77
    assert event.answers, "the button did not answer"


# ------------------------------------------------- reports, retry & the queue
def _callback_data(button) -> str:
    """The payload of an inline button, whichever telethon layer built it."""
    payload = getattr(button, "data", None)
    if payload is None:
        payload = getattr(getattr(button, "type", None), "data", None)
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload).decode("utf-8", "replace")
    return str(payload or "")


def _campaign_with(items: dict[int, str]) -> int:
    """A finished campaign whose recipients have the given statuses."""
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all", start=False)
    for uid, status in items.items():
        db.mark_campaign_item(campaign["id"], uid, status, "could not find the input entity")
    return int(campaign["id"])


@pytest.mark.asyncio
async def test_who_shows_real_names_not_only_a_count(studio):
    await call(br_handlers.br_start, BrEvent())
    event = BrEvent(text="/brwho", groups=("who", None, ""))
    await call(br_handlers.br_command, event)

    assert event.sent
    report = str(event.sent[-1][0])
    assert "মোট" in report and str(USERS[0]) in report
    assert "পৌঁছানো যাবে" in report        # the access-hash picture is right there


@pytest.mark.asyncio
async def test_why_explains_every_failed_recipient(studio):
    campaign_id = _campaign_with({USERS[0]: "failed", USERS[1]: "sent"})
    event = BrEvent(text="/brwhy", groups=("why", None, ""))
    await call(br_handlers.br_command, event)

    report = str(event.sent[-1][0])
    assert f"#{campaign_id}" in report
    assert "কেন পায়নি" in report and str(USERS[0]) in report
    assert str(USERS[1]) not in report, "a delivered user must not be listed as missing"


@pytest.mark.asyncio
async def test_retry_puts_the_failed_recipients_back_in_the_queue(studio, monkeypatch):
    campaign_id = _campaign_with({USERS[0]: "failed", USERS[1]: "sent"})
    started: list[int] = []
    monkeypatch.setattr(broadcast, "start_campaign",
                        lambda cid, progress=None: started.append(int(cid)))

    await call(br_handlers.br_command,
               BrEvent(text="/brretry", groups=("retry", None, "")))

    assert started == [campaign_id]
    assert db.campaign_user_ids(campaign_id, "pending") == [USERS[0]]
    assert str(USERS[0]) not in db.campaign_user_ids(campaign_id, "sent")


@pytest.mark.asyncio
async def test_retry_says_so_when_nobody_failed(studio):
    _campaign_with({USERS[0]: "sent", USERS[1]: "sent"})
    event = BrEvent(text="/brretry", groups=("retry", None, ""))
    await call(br_handlers.br_command, event)
    assert "ব্যর্থ কেউ নেই" in str(event.sent[-1][0])


@pytest.mark.asyncio
async def test_queue_lists_and_cancels_a_scheduled_broadcast(studio):
    campaign = broadcast.create_campaign(ADMIN_ID, text="later", audience="all", start=False)
    db.update_campaign(campaign["id"], status=broadcast.SCHEDULED,
                       scheduled_at=time.time() + 600)

    listing = BrEvent(text="/brqueue", groups=("queue", None, ""))
    await call(br_handlers.br_command, listing)
    assert f"#{campaign['id']}" in str(listing.sent[-1][0])
    buttons = listing.sent[-1][1]
    assert any(f"unsched:{campaign['id']}" in _callback_data(button)
               for row in buttons for button in row)

    cancel = BrEvent(text=f"/brcancelqueue {campaign['id']}",
                     groups=("cancelqueue", None, str(campaign["id"])))
    await call(br_handlers.br_command, cancel)
    assert db.campaign(campaign["id"])["status"] == broadcast.CANCELLED


@pytest.mark.asyncio
async def test_a_scheduled_campaign_still_gets_its_media_and_origin(studio):
    await call(br_handlers.br_start, BrEvent())
    await call(br_handlers.br_collect, BrEvent(text="আগামীকালের প্রোমো"))
    await call(br_handlers.br_collect, BrEvent(media=_video()))
    event = BrEvent(text="/brschedule 30", groups=("schedule", None, "30"))
    await call(br_handlers.br_command, event)

    campaign = db.campaigns(limit=1)[0]
    assert campaign["status"] == broadcast.SCHEDULED and campaign["origin"] == "br"
    # A multi-message draft keeps its list: the text, then the video (msg 77).
    plan = db.campaign_groups(campaign["id"])
    assert [row["kind"] for row in plan] == ["text", "video"]
    assert plan[1]["media_key"].endswith(":77")


@pytest.mark.asyncio
async def test_panel_hint_buttons_open_the_studio_and_the_inbox(studio):
    from app.handlers import router

    handler, rest = router.resolve("hint:br")
    event = BrEvent()
    await call(handler, event, rest)
    assert br_handlers.sessions.get(ADMIN_ID), "the /br studio was not opened"
    assert "স্টুডিও" in str(event.sent[-1][0])

    handler, rest = router.resolve("hint:bm")
    event = BrEvent()
    await call(handler, event, rest)
    assert state.pending_input.get(ADMIN_ID, {}).get("action") == "bc_media", \
        "the inbox capture was not armed"


@pytest.mark.asyncio
async def test_slow_mode_spreads_the_list_over_the_asked_minutes(studio, monkeypatch):
    """/brslow 30 — Telegram's own advice for big lists, expressed as a real pause."""
    await call(br_handlers.br_start, BrEvent())
    await call(br_handlers.br_collect, BrEvent(text="ধীরে যাবে"))

    seen: dict = {}

    async def fake_send_all(admin_id, content, **kwargs):
        seen.update(kwargs)
        return {"campaign_id": 1, "sent": 2, "failed": 0, "blocked": 0, "pending": 0,
                "total": 2, "seconds": 0.1, "status": "done"}

    monkeypatch.setattr(quickcast, "send_all", fake_send_all)
    event = BrEvent(text="/brslow 30", groups=("slow", None, "30"))
    await call(br_handlers.br_command, event)

    assert seen.get("slow_minutes") == 30.0
    assert any("ধীর মোড" in str(text) for text, _buttons in event.sent)


@pytest.mark.asyncio
async def test_slow_mode_needs_a_number(studio):
    await call(br_handlers.br_start, BrEvent())
    event = BrEvent(text="/brslow", groups=("slow", None, ""))
    await call(br_handlers.br_command, event)
    assert any("কত মিনিট" in str(text) for text, _buttons in event.sent)


def test_the_pause_between_sends_is_derived_from_the_window():
    assert quickcast.slow_pause(600, 10) == pytest.approx(1.0)
    assert quickcast.slow_pause(100000, 10) == 0.05       # never slower than a floor
    assert quickcast.slow_pause(2, 60) == 300.0           # and never slower than the cap
    assert quickcast.slow_pause(0, 0) == 0.0


# ------------------------------------------- multi-message drafts (broadcast.py)
@pytest.mark.asyncio
async def test_a_multi_message_draft_arrives_one_message_at_a_time(studio, monkeypatch):
    """Text + video + text → three separate messages per user, in that order."""
    source = SimpleNamespace(media=MessageMediaDocument(document=video_document()))
    client = FakeClient(known={uid: 1000 + uid for uid in (*USERS, ADMIN_ID)},
                        messages={(ADMIN_ID, 77): source})
    runtime.set_client(client)
    monkeypatch.setattr(runtime, "_client", client, raising=False)

    await call(br_handlers.br_start, BrEvent())
    await call(br_handlers.br_collect, BrEvent(text="প্রথম মেসেজ"))
    await call(br_handlers.br_collect, BrEvent(media=_video()))
    await call(br_handlers.br_collect, BrEvent(text="শেষ মেসেজ"))
    await call(br_handlers.br_collect, BrEvent(text="সাইট | https://t.me/x"))

    event = BrEvent(text="/brsend", groups=("send", None, ""))
    await call(br_handlers.br_command, event)

    per_user = {}
    for uid, text in client.sent_messages:
        per_user.setdefault(uid, []).append(text)
    assert sorted(per_user) == sorted(USERS)
    for uid, bodies in per_user.items():
        assert len(bodies) == 3, f"user {uid} got {bodies}"
        assert bodies[0] == "প্রথম মেসেজ" and bodies[2] == "শেষ মেসেজ"
    campaign = db.campaigns(limit=1)[0]
    plan = db.campaign_groups(campaign["id"])
    assert [row["kind"] for row in plan] == ["text", "video", "text"]


@pytest.mark.asyncio
async def test_the_buttons_ride_on_the_last_message(studio, monkeypatch):
    """The archive put the keyboard on the final item only — same here."""
    await call(br_handlers.br_start, BrEvent())
    await call(br_handlers.br_collect, BrEvent(text="এক"))
    await call(br_handlers.br_collect, BrEvent(text="দুই"))
    await call(br_handlers.br_collect, BrEvent(text="সাইট | https://t.me/x"))

    seen: list = []

    async def fake_send_draft(chat_id, items, **kwargs):
        seen.append((chat_id, [item["kind"] for item in items], kwargs.get("buttons_on")))
        return len(items)

    from app.services import sender as sender_module
    monkeypatch.setattr(sender_module, "send_draft", fake_send_draft)

    await call(br_handlers.br_command, BrEvent(text="/brsend", groups=("send", None, "")))
    assert seen and all(entry[2] == "last" for entry in seen)


@pytest.mark.asyncio
async def test_britems_lists_and_brdrop_removes(studio):
    await call(br_handlers.br_start, BrEvent())
    await call(br_handlers.br_collect, BrEvent(text="প্রথম"))
    await call(br_handlers.br_collect, BrEvent(text="দ্বিতীয়"))

    listing = BrEvent(text="/britems", groups=("items", None, ""))
    await call(br_handlers.br_command, listing)
    body = str(listing.sent[-1][0])
    assert "1." in body and "2." in body and "প্রথম" in body

    drop = BrEvent(text="/brdrop", groups=("drop", None, ""))
    await call(br_handlers.br_command, drop)
    draft = quickcast.load_draft(ADMIN_ID)
    assert [item.text for item in draft.items] == ["প্রথম"]


@pytest.mark.asyncio
async def test_brcsv_sends_the_report_as_a_document(studio, monkeypatch):
    await call(br_handlers.br_start, BrEvent())
    await call(br_handlers.br_collect, BrEvent(text="রিপোর্ট টেস্ট"))
    await call(br_handlers.br_command, BrEvent(text="/brsend", groups=("send", None, "")))

    captured: dict = {}

    async def fake_send_file(*args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        return SimpleNamespace(id=1)

    import app.services.broadcast as broadcast_module
    monkeypatch.setattr(broadcast_module.bot, "send_file", fake_send_file, raising=False)

    event = BrEvent(text="/brcsv", groups=("csv", None, ""))
    await call(br_handlers.br_command, event)
    assert captured, "no report was sent"
    csv_bytes = captured["args"][1]
    assert csv_bytes.startswith(b"\xef\xbb\xbf"), "a BOM keeps Bangla readable in Excel"
    assert b"user_id" in csv_bytes and str(USERS[0]).encode() in csv_bytes


@pytest.mark.asyncio
async def test_the_report_lists_who_got_it_and_who_did_not(studio):
    """The archive's `/send` ending: counts *and* a per-user CSV."""
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all", start=False)
    db.add_campaign_items(campaign["id"], [USERS[0], USERS[1]])
    db.mark_campaign_item(campaign["id"], USERS[0], "sent")
    db.mark_campaign_item(campaign["id"], USERS[1], "failed", "blocked by user")

    data, summary = broadcast.report_bytes(campaign["id"])
    text = data.decode("utf-8-sig")
    assert summary["sent"] == 1 and summary["failed"] == 1
    assert f"{USERS[0]}" in text and f"{USERS[1]}" in text
    assert "blocked by user" in text
    assert text.splitlines()[1].split(",")[3] == "sent"      # successes first
    lines = broadcast.failure_lines(campaign["id"])
    assert lines and "blocked by user" in lines[0]
