"""Render every bot screen once, offline.

Every route is called exactly like a real button press, with a fake event and a
seeded database. This is the test that would have caught the ``NameError`` bugs
that made screens simply “not open” in production.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import config as cfg, runtime
from app.handlers import state
from app.services import billing, support
from app.storage import db
from tests.test_flows import ADMIN_ID, USER_ID, FakeEvent

#: routes that exist to *do* something (and would talk to Telegram) — not screens
SKIP = {
    "fjlink", "fjtest", "gs", "rf", "lang", "supq", "lgc", "lgg", "scs", "winb",
    "acp", "acn", "aod", "apln", "nbcnew", "nbcsetup", "nbcsize", "ch:add",
    "ch:post", "adm:setmenu", "dg:run", "fvs", "an:web",
}

SCREENS = [
    "adm:back", "adm:settings", "adm:setg:force_join", "adm:setg:payments",
    "adm:setg:content", "adm:setg:broadcast", "adm:setg:support", "adm:setg:site",
    "apm", "ar14", "an:home", "ch:home", "lk:home", "s:1", "f:1", "fms:1",
    "sga:1", "apl:1", "fml:1", "bs:1", "bt:1", "wbs:all", "fm:1", "sst:1",
    "ssn:1", "desc:1", "cover:1", "sh:1", "st:1", "hp", "ct:0", "mya:0",
    "supc:back", "sb:1", "dr:1", "gst:1", "gbulk:1", "nbcgo:", "sfj:1",
    "rq:1", "up:0", "cu:0", "ast:1", "rqd:1", "shr:1", "scp:1", "gd:1",
    "lg:1", "lgp:1", "lgt:1", "plan:1", "adm:setk:BROADCAST_MAX_FILES",
    "tx:home", "tx:g:0", "tx:k:home_banner",
]


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "screens.sqlite3"))
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(cfg, "PAY_BKASH", "01712-345678")
    monkeypatch.setattr(cfg, "TRIAL_HOURS", 24)
    for holder in (state.pending_input, state.search_pending, state.flow_ctx, state.link_gen):
        holder.clear()
    runtime.bot_username = "test_bot"
    runtime.force_join_cache.clear()
    runtime.force_join_ok.clear()

    store = db.create_store(ADMIN_ID, "Movies")
    db.update_store(store["id"], is_premium=0, description="demo")
    file_id = db.add_file(store["id"], "Movie 1", "Video", -100, 10)
    db.set_mirror(file_id, ADMIN_ID, 500)
    db.set_active_store(ADMIN_ID, store["id"])
    plan_id = db.add_plan(store["id"], "1 Month", 30, 199)
    plan = {"id": plan_id, "name": "1 Month", "days": 30}
    order_id = db.create_order(USER_ID, store["id"], plan, "bkash", 199, "৳", "TXN1")
    db.toggle_sub(store["id"], USER_ID)
    db.upsert_drip(store["id"], ADMIN_ID, 2, "10:00")
    ticket_id = asyncio.run(support.open_ticket(USER_ID, "help me")) or 0
    db.create_link(ADMIN_ID, [file_id], None, kind="limited", max_clicks=5)
    db.set_store_forcejoin(store["id"], "@store_channel")
    db.add_join_channel(None, "@global_channel")
    db.touch_user(USER_ID, "Rahim", "rahim")
    db.touch_user(ADMIN_ID, "Owner", "owner")
    db.grant(store["id"], USER_ID, time.time() + 86400, "test")
    return {"store": db.store(store["id"]), "file": file_id, "order": order_id,
            "ticket": ticket_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", SCREENS)
async def test_every_screen_renders(seeded, key):
    from app.handlers import router

    if key in SKIP:
        pytest.skip("action, not a screen")
    resolved = router.resolve(key)
    assert resolved is not None, f"no handler is registered for {key!r}"
    handler, rest = resolved
    event = FakeEvent(ADMIN_ID, data=key.encode())
    try:
        await handler(event, rest)
    except Exception as exc:                       # noqa: BLE001 - that's the point
        pytest.fail(f"{key!r} crashed: {type(exc).__name__}: {exc}")
    assert event.sent or event.edits or event.answers, f"{key!r} rendered nothing"


# --------------------------------------------- flows must never stay armed forever
@pytest.mark.asyncio
async def test_a_stale_question_is_dropped_and_says_so(seeded):
    """The bug behind “any text keeps getting saved”: a question from yesterday
    was still armed, so every later message became its answer."""
    import time as _time

    from app.handlers import messages as messages_module
    from app.handlers import state

    state.ask(ADMIN_ID, "create_store")
    state.pending_input[ADMIN_ID]["at"] = _time.time() - 7200      # two hours ago
    event = FakeEvent(ADMIN_ID, text="আমার নতুন ভিডিও")
    await messages_module.pending_input(event)

    assert event.sent, "the bot stayed silent after dropping a stale question"
    assert "সময় শেষ" in event.sent[-1][0]
    assert state.peek(ADMIN_ID) is None                            # nothing stays armed
    assert not db.store_by_name(ADMIN_ID, "আমার নতুন ভিডিও")        # nothing was saved


@pytest.mark.asyncio
async def test_a_wrong_answer_is_capped_then_cancelled(seeded):
    from app.handlers import messages as messages_module
    from app.handlers import state

    state.ask(ADMIN_ID, "link_limit", file_id=1)
    replies = []
    for _ in range(3):
        event = FakeEvent(ADMIN_ID, text="এটা তো সংখ্যা না")
        await messages_module.pending_input(event)
        replies.extend(text for text, _ in event.sent)

    assert any("বারবার ভুল" in text for text in replies), replies
    assert state.peek(ADMIN_ID) is None
    assert len(db.links(limit=5)) == 1                             # only the fixture's link
    assert any("আর" in text and "বার চেষ্টা" in text for text in replies)


@pytest.mark.asyncio
async def test_the_cancel_button_stops_a_flow(seeded):
    from app.handlers import state
    from app.handlers import user as user_handlers

    state.ask(ADMIN_ID, "create_store")
    event = FakeEvent(ADMIN_ID, data=b"cd:flow")
    await user_handlers.cancel_flow(event, "")
    assert state.peek(ADMIN_ID) is None
    assert event.answers and "বাতিল" in event.answers[-1][0]


@pytest.mark.asyncio
async def test_tapping_any_button_drops_an_armed_question(seeded):
    """A tap means “I moved on” — the armed question must not eat the next text."""
    from app.handlers import router, state

    state.ask(ADMIN_ID, "create_store")
    event = FakeEvent(ADMIN_ID, data=b"adm:back")
    await router.dispatch(event)
    assert state.peek(ADMIN_ID) is None
    assert event.edits or event.sent                    # the panel really rendered
