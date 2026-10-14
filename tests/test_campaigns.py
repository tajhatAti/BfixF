"""Broadcast studio tests — queue, resume, cancel, blocked users, personalisation."""
from __future__ import annotations

import time

import pytest

from app import config as cfg, runtime
from app.services import broadcast
from app.storage import Database, db

ADMIN_ID = 999


class FakeBot:
    """Records messages instead of talking to Telegram."""

    def __init__(self, fail_for: dict[int, str] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or {}

    async def send_message(self, chat_id, text=None, **_kwargs):
        if chat_id in self.fail_for:
            raise RuntimeError(self.fail_for[chat_id])
        self.sent.append((chat_id, text))
        return True


@pytest.fixture()
def clean(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "campaigns.sqlite3"))
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    runtime.blocked_users.clear()
    for user_id in (11, 12, 13, 14):
        db.touch_user(user_id, f"User{user_id}", f"u{user_id}")
    return monkeypatch


def _fake_bot(monkeypatch, fail_for=None) -> FakeBot:
    fake = FakeBot(fail_for)
    monkeypatch.setattr(broadcast, "bot", fake)
    return fake


def _store(name="Premium", premium=True):
    store = db.create_store(ADMIN_ID, name)
    db.update_store(store["id"], is_premium=1 if premium else 0)
    return db.store(store["id"])


# ------------------------------------------------------------------ audiences
def test_audience_segments_resolve(clean):
    store = _store("Premium Store")
    db.grant(store["id"], 11, time.time() + 86400, "test")
    # 12 never bought anything, 13 is subscribed but has no access
    db.toggle_sub(store["id"], 13)

    everyone = set(broadcast.resolve_audience(ADMIN_ID, "all"))
    assert {11, 12, 13, 14} <= everyone
    assert ADMIN_ID not in everyone                     # admins never get spammed

    premium = broadcast.resolve_audience(ADMIN_ID, "premium")
    assert 11 in premium and 12 not in premium

    never_bought = broadcast.resolve_audience(ADMIN_ID, f"nosale:{store['id']}")
    assert 11 not in never_bought and 12 in never_bought

    subscribers = broadcast.resolve_audience(ADMIN_ID, f"sub:{store['id']}")
    assert subscribers == [13]


def test_audience_labels_are_human_readable():
    assert "All" in broadcast.audience_label("all")
    assert "#7" in broadcast.audience_label("store:7")


# ----------------------------------------------------------------- the campaign
def test_campaign_fills_a_persistent_queue(clean):
    _store()
    campaign = broadcast.create_campaign(ADMIN_ID, text="hello", audience="all", start=False)
    counts = db.campaign_counts(campaign["id"])
    assert counts["total"] == 4
    assert counts["pending"] == 4
    assert db.campaign_user_ids(campaign["id"], "pending") == [11, 12, 13, 14]


@pytest.mark.asyncio
async def test_run_campaign_sends_and_finishes(clean, monkeypatch):
    fake = _fake_bot(monkeypatch)
    _store()
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi {name}", audience="all")

    seen: list[tuple[int, int, int]] = []

    async def progress(done, total, failed):
        seen.append((done, total, failed))

    result = await broadcast.run_campaign(campaign["id"], progress=progress, notify_admin=False)

    assert result["status"] == broadcast.DONE
    assert result["sent"] == 4 and result["failed"] == 0
    assert len(fake.sent) == 4
    assert any("{name}" not in text for _uid, text in fake.sent)   # personalised
    assert db.campaign(campaign["id"])["status"] == "done"
    assert db.campaign_counts(campaign["id"])["pending"] == 0
    assert seen, "progress was never reported"


@pytest.mark.asyncio
async def test_blocked_users_are_remembered_and_skipped(clean, monkeypatch):
    _fake_bot(monkeypatch, fail_for={12: "Forbidden: user blocked the bot"})
    _store()
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all")
    result = await broadcast.run_campaign(campaign["id"], notify_admin=False)

    assert result["sent"] == 3 and result["blocked"] == 1
    assert 12 in runtime.blocked_users

    # a second campaign skips them instantly, without even trying to send
    fake = _fake_bot(monkeypatch)
    second = broadcast.create_campaign(ADMIN_ID, text="again", audience="all")
    result2 = await broadcast.run_campaign(second["id"], notify_admin=False)
    assert result2["blocked"] == 1 and len(fake.sent) == 3


@pytest.mark.asyncio
async def test_campaign_resumes_after_a_restart(clean, monkeypatch):
    fake = _fake_bot(monkeypatch)
    _store()
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all")
    # pretend the process died after two users were served
    db.mark_campaign_item(campaign["id"], 11, "sent")
    db.mark_campaign_item(campaign["id"], 12, "sent")

    result = await broadcast.run_campaign(campaign["id"], notify_admin=False)
    assert result["sent"] == 4
    assert [uid for uid, _ in fake.sent] == [13, 14]      # nobody got it twice


@pytest.mark.asyncio
async def test_media_fallback_does_not_send_the_probe_file_twice(clean, monkeypatch):
    from app.services import sender
    store = _store()
    file_ids = [db.add_file(store["id"], name, "Video", -100, index)
                for index, name in enumerate(("First", "Second"), start=1)]
    campaign = broadcast.create_campaign(ADMIN_ID, text="caption", audience="all",
                                         files=file_ids, start=False, online_button=0)
    calls = []

    async def not_cached(row):
        return row

    async def deliver_once(chat_id, file_id, caption=None, buttons=None):
        calls.append((chat_id, file_id, caption))
        return sender.Delivery(True)

    monkeypatch.setattr(sender, "ensure_ready", not_cached)
    monkeypatch.setattr(sender, "deliver_file_id", deliver_once)
    assert await broadcast._send_to_user(campaign, 11) == "sent"
    assert [file_id for _chat, file_id, _caption in calls] == file_ids


@pytest.mark.asyncio
async def test_background_worker_crash_is_recorded_for_admin_retry(clean, monkeypatch):
    _store()
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all")

    async def broken_worker(*_args, **_kwargs):
        raise RuntimeError("simulated worker crash")

    monkeypatch.setattr(broadcast, "run_campaign", broken_worker)
    result = await broadcast.start_campaign(campaign["id"], notify_admin=False)
    stored = db.campaign(campaign["id"])
    assert result["status"] == broadcast.FAILED
    assert stored["status"] == broadcast.FAILED
    assert "simulated worker crash" in stored["last_error"]


def test_queued_campaigns_resume_even_when_running_campaign_resume_is_disabled(clean, monkeypatch):
    _store()
    queued = broadcast.create_campaign(ADMIN_ID, text="queued", audience="all")
    interrupted = broadcast.create_campaign(ADMIN_ID, text="interrupted", audience="all",
                                            start=False)
    db.update_campaign(interrupted["id"], status=broadcast.RUNNING)
    started = []
    monkeypatch.setattr(broadcast, "start_campaign", lambda campaign_id: started.append(campaign_id))

    assert broadcast.resume_unfinished(include_running=False) == 1
    assert started == [queued["id"]]


@pytest.mark.asyncio
async def test_cancel_stops_the_send(clean, monkeypatch):
    import asyncio

    _store()
    for extra in range(200, 260):                        # a longer queue to stop mid-way
        db.touch_user(extra, f"U{extra}", None)
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all")

    class SlowBot(FakeBot):
        async def send_message(self, chat_id, text=None, **kwargs):
            await asyncio.sleep(0.2)
            return await super().send_message(chat_id, text, **kwargs)

    monkeypatch.setattr(broadcast, "bot", SlowBot())
    task = broadcast.start_campaign(campaign["id"], notify_admin=False)
    await asyncio.sleep(0.1)
    broadcast.cancel_campaign(campaign["id"])
    await task

    counts = db.campaign_counts(campaign["id"])
    assert db.campaign(campaign["id"])["status"] == broadcast.CANCELLED
    assert counts["pending"] > 0, "cancel should leave the rest queued"
    assert counts["sent"] < counts["total"]


def test_retry_rearms_failed_items(clean):
    _store()
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all")
    db.mark_campaign_item(campaign["id"], 11, "failed", "boom")
    db.mark_campaign_item(campaign["id"], 12, "blocked", "blocked")

    assert db.rearm_campaign_items(campaign["id"], ("failed", "blocked")) == 2
    counts = db.campaign_counts(campaign["id"])
    assert counts["pending"] == 4 and counts["failed"] == 0


def test_scheduling_and_due_detection(clean):
    _store()
    later = broadcast.create_campaign(ADMIN_ID, text="later", audience="all",
                                      scheduled_at=time.time() + 3600)
    assert db.campaign(later["id"])["status"] == broadcast.SCHEDULED
    assert db.due_campaigns() == []                       # not due yet
    assert [c["id"] for c in db.due_campaigns(now=time.time() + 7200)] == [later["id"]]

    past = broadcast.create_campaign(ADMIN_ID, text="now", audience="all",
                                     scheduled_at=time.time() - 5)
    # a time that already passed means "send it now" (queued, never lost)
    assert db.campaign(past["id"])["status"] == broadcast.QUEUED


def test_queue_survives_a_rebind(clean):
    """The queue lives in SQLite, so it must survive a process restart."""
    _store()
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all")
    path = db.impl.path
    db.mark_campaign_item(campaign["id"], 11, "sent")

    db.bind(path)                                        # simulate a restart
    assert db.campaign_user_ids(campaign["id"], "pending") == [12, 13, 14]
    assert [i["user_id"] for i in db.campaign_items(campaign["id"], "sent")] == [11]


def test_preview_personalises(clean):
    campaign = {"text": "হ্যালো {name}, নতুন ভিডিও এসেছে", "file_ids": []}
    assert "রহিম" in broadcast.preview(campaign, sample_name="রহিম")


@pytest.mark.asyncio
async def test_legacy_run_broadcast_still_works(clean, monkeypatch):
    fake = _fake_bot(monkeypatch)
    result = await broadcast.run_broadcast([11, 12, 13], "legacy")
    assert result["sent"] == 3 and len(fake.sent) == 3


@pytest.mark.asyncio
async def test_test_send_only_reaches_the_admin(clean, monkeypatch):
    fake = _fake_bot(monkeypatch)
    _store()
    campaign = broadcast.create_campaign(ADMIN_ID, text="hover", audience="all", start=False)
    result = await broadcast.test_send(campaign["id"], [ADMIN_ID])
    assert result["ok"] and [uid for uid, _ in fake.sent] == [ADMIN_ID]
    assert db.campaign_counts(campaign["id"])["sent"] == 0     # queue untouched


def test_campaigns_do_not_touch_other_admins_queues(clean):
    _store()
    mine = broadcast.create_campaign(ADMIN_ID, text="mine", audience="all")
    assert [c["id"] for c in db.campaigns(admin_id=ADMIN_ID)] == [mine["id"]]
    assert db.campaigns(admin_id=4242) == []


@pytest.mark.asyncio
async def test_campaign_sends_in_parallel_so_small_audiences_are_fast(clean, monkeypatch):
    import asyncio

    class SlowBot(FakeBot):
        async def send_message(self, chat_id, text=None, **kwargs):
            await asyncio.sleep(0.2)
            return await super().send_message(chat_id, text, **kwargs)

    fake = SlowBot()
    monkeypatch.setattr(broadcast, "bot", fake)
    _store()
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all")

    started = time.time()
    result = await broadcast.run_campaign(campaign["id"], notify_admin=False)

    assert result["sent"] == 4 and len(fake.sent) == 4
    assert time.time() - started < 0.6      # one-by-one would take 0.8s+
