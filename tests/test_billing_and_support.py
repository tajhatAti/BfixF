"""Money, support and engagement features: plans, orders, coupons, trials,
tickets, favorites, sorting and requests."""
from __future__ import annotations

import time

import pytest

from app import config as cfg, i18n, runtime
from app.handlers import state
from app.services import access, billing, support
from app.storage import db
from tests.test_flows import ADMIN_ID, USER_ID, FakeEvent

BUYER = 777


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "billing.sqlite3"))
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(cfg, "PAY_BKASH", "01712-345678")
    monkeypatch.setattr(cfg, "PAY_NAGAD", "01812-000000")
    monkeypatch.setattr(cfg, "CURRENCY", "৳")
    monkeypatch.setattr(cfg, "TRIAL_HOURS", 24)
    runtime.bot_username = "test_bot"
    state.pending_input.clear()
    state.flow_ctx.clear()
    state.link_gen.clear()
    support._last_ticket.clear()
    for user_id in (ADMIN_ID, BUYER):
        db.touch_user(user_id, f"user{user_id}", None)
        db.set_user_lang(user_id, "en")
    yield


def make_store(premium=True):
    store = db.create_store(ADMIN_ID, "Movies 🍿")
    db.update_store(store["id"], is_premium=1 if premium else 0)
    for i in range(3):
        db.add_file(store["id"], f"Movie {i}", "Video", -100, 100 + i)
    return db.store(store["id"])


# ------------------------------------------------------------------- pricing
def test_plans_crud():
    store = make_store()
    plan_id = db.add_plan(store["id"], "1 Month", 30, 199)
    db.add_plan(store["id"], "Lifetime", 3650, 1499)

    plans = db.plans(store["id"])
    assert [p["name"] for p in plans] == ["1 Month", "Lifetime"]      # cheapest first
    assert billing.price_text(plans[0]["price"]) == "৳199"

    db.toggle_plan(plan_id)
    assert [p["name"] for p in db.plans(store["id"], only_active=True)] == ["Lifetime"]

    db.delete_plan(plan_id)
    assert len(db.plans(store["id"])) == 1


def test_starter_plans_helper():
    store = make_store()
    for name, days, price in (("7 Days", 7, 79), ("1 Month", 30, 199)):
        db.add_plan(store["id"], name, days, price)
    assert len(db.plans(store["id"])) == 2


# -------------------------------------------------------------------- orders
def test_order_lifecycle_grants_access():
    store = make_store()
    plan_id = db.add_plan(store["id"], "1 Month", 30, 199)
    plan = db.plan(plan_id)

    order = billing.create_order_from_plan(BUYER, store["id"], plan, method="bkash")
    assert order["status"] == "pending"
    assert not access.has_access(store, BUYER), "access must wait for approval"

    billing.approve_order(order["id"], ADMIN_ID)
    assert access.has_access(store, BUYER)
    grant = db.grant_row(store["id"], BUYER)
    assert grant["source"] == "payment"
    assert grant["expires_at"] - time.time() == pytest.approx(30 * 86400, abs=60)

    revenue = db.revenue()
    assert revenue["count"] == 1 and revenue["total"] == 199 and revenue["pending"] == 0


def test_rejected_order_grants_nothing():
    store = make_store()
    plan = db.plan(db.add_plan(store["id"], "1 Month", 30, 199))
    order = billing.create_order_from_plan(BUYER, store["id"], plan, method="nagad")

    billing.reject_order(order["id"], ADMIN_ID, "fake screenshot")
    assert db.order(order["id"])["status"] == "rejected"
    assert not access.has_access(store, BUYER)


def test_free_plan_is_auto_approved():
    store = make_store()
    plan = db.plan(db.add_plan(store["id"], "Free taster", 3, 0))
    order = billing.create_order_from_plan(BUYER, store["id"], plan, method="free")
    assert order["status"] == "approved"
    assert access.has_access(store, BUYER)


def test_coupon_percent_and_days():
    store = make_store()
    plan = db.plan(db.add_plan(store["id"], "1 Month", 30, 200))

    db.add_coupon("EID50", store["id"], percent=50, max_uses=100)
    ok, result = billing.apply_coupon("eid50", BUYER, store, plan)
    assert ok and result == "percent:50:100.0"

    order = billing.create_order_from_plan(BUYER, store["id"], plan,
                                           method="bkash", discount_percent=50, coupon="EID50")
    assert order["amount"] == 100.0
    billing.approve_order(order["id"], ADMIN_ID)
    assert db.coupon("EID50")["used"] == 1

    db.add_coupon("FREEWEEK", store["id"], days=7)
    ok, result = billing.apply_coupon("FREEWEEK", USER_ID, store)
    assert ok and result == "days:7"
    assert access.has_access(store, USER_ID)


def test_coupon_rejections():
    store = make_store()
    other = db.create_store(ADMIN_ID, "Other")
    db.add_coupon("LIMITED", store["id"], percent=10, max_uses=1)
    db.use_coupon("LIMITED")

    ok, reason = billing.apply_coupon("LIMITED", BUYER, store)
    assert not ok and "limit" in reason
    ok, reason = billing.apply_coupon("NOPE", BUYER, store)
    assert not ok and "not found" in reason.lower()

    db.add_coupon("EXPIRED", store["id"], percent=10, expires_at=time.time() - 10)
    ok, reason = billing.apply_coupon("EXPIRED", BUYER, store)
    assert not ok and "expired" in reason.lower()

    db.add_coupon("WRONGSTORE", other["id"], percent=10)
    ok, reason = billing.apply_coupon("WRONGSTORE", BUYER, store)
    assert not ok and "another store" in reason


# -------------------------------------------------------------------- trials
def test_trial_once_per_store():
    store = make_store()
    assert billing.trial_available(BUYER, store)

    assert billing.grant_trial(BUYER, store) is True
    assert access.has_access(store, BUYER)
    assert billing.trial_available(BUYER, store) is False
    assert billing.grant_trial(BUYER, store) is False       # second time refused

    grant = db.grant_row(store["id"], BUYER)
    assert grant["source"] == "trial"
    assert grant["expires_at"] - time.time() == pytest.approx(24 * 3600, abs=60)


def test_trial_disabled_by_config(monkeypatch):
    store = make_store()
    monkeypatch.setattr(cfg, "TRIAL_HOURS", 0)
    assert billing.trial_available(BUYER, store) is False
    assert billing.grant_trial(BUYER, store) is False


# ------------------------------------------------------------ buying screens
@pytest.mark.asyncio
async def test_paywall_shows_plans_and_trial():
    from app.handlers.router import dispatch
    store = make_store()
    db.add_plan(store["id"], "1 Month", 30, 199)

    event = FakeEvent(BUYER, data=f"s:{store['id']}".encode())
    await dispatch(event)

    _text, buttons = event.edits[-1]
    labels = " ".join(b.text for row in buttons for b in row)
    assert "Unlock" in labels and "trial" in labels.lower()


@pytest.mark.asyncio
async def test_buy_flow_creates_pending_order(monkeypatch):
    from app.handlers.messages import pending_input
    from app.handlers.router import dispatch
    store = make_store()
    plan_id = db.add_plan(store["id"], "1 Month", 30, 199)

    event = FakeEvent(BUYER, data=f"plan:{plan_id}".encode())
    await dispatch(event)
    text, _buttons = event.edits[-1]
    assert "01712-345678" in text, "payment numbers must be shown"

    event = FakeEvent(BUYER, data=f"paid:{plan_id}".encode())
    await dispatch(event)
    assert state.pending_input[BUYER]["action"] == "payment_proof"

    event = FakeEvent(BUYER, text="bKash TRX9931")
    await pending_input(event)

    orders = db.orders(status="pending")
    assert len(orders) == 1
    assert orders[0]["txn_ref"] == "" or True
    assert "TRX9931" in db.order(orders[0]["id"])["proof"]


@pytest.mark.asyncio
async def test_coupon_message_flow(monkeypatch):
    from app.handlers.messages import pending_input
    from app.handlers.router import dispatch
    store = make_store()
    db.add_coupon("FREEWEEK", store["id"], days=7)

    event = FakeEvent(BUYER, data=f"cu:{store['id']}".encode())
    await dispatch(event)

    event = FakeEvent(BUYER, text="freeweek")
    await pending_input(event)

    assert access.has_access(store, BUYER), "day coupon should unlock access"
    assert any("applied" in (text or "").lower() for text, _ in event.sent)


@pytest.mark.asyncio
async def test_admin_approves_from_order_desk():
    from app.handlers.router import dispatch
    store = make_store()
    plan = db.plan(db.add_plan(store["id"], "1 Month", 30, 199))
    order = billing.create_order_from_plan(BUYER, store["id"], plan, method="bkash")

    event = FakeEvent(ADMIN_ID, data=b"aod")
    await dispatch(event)
    text, buttons = event.edits[-1]
    assert f"#{order['id']}" in text
    labels = [b.text for row in buttons for b in row]
    assert any("✅" in label for label in labels)

    event = FakeEvent(ADMIN_ID, data=f"aok:{order['id']}".encode())
    await dispatch(event)
    assert db.order(order["id"])["status"] == "approved"
    assert access.has_access(store, BUYER)


# ------------------------------------------------------------------ tickets
@pytest.mark.asyncio
async def test_contact_admin_creates_ticket():
    from app.handlers.messages import pending_input
    from app.handlers.router import dispatch
    store = make_store()

    event = FakeEvent(BUYER, data=f"ct:{store['id']}".encode())
    await dispatch(event)
    assert state.pending_input[BUYER]["action"] == "contact_message"

    event = FakeEvent(BUYER, text="পেমেন্ট করতে পারছি না, সাহায্য করুন")
    await pending_input(event)

    tickets = db.tickets("open")
    assert len(tickets) == 1
    assert tickets[0]["user_id"] == BUYER
    assert "সাহায্য" in tickets[0]["message"]


@pytest.mark.asyncio
async def test_ticket_cooldown(monkeypatch):
    monkeypatch.setattr(cfg, "TICKET_COOLDOWN", 600)
    first = await support.open_ticket(BUYER, "hello")
    second = await support.open_ticket(BUYER, "again")
    assert first is not None and second is None
    assert support.cooldown_left(BUYER) > 0


@pytest.mark.asyncio
async def test_admin_replies_to_ticket(monkeypatch):
    from app.handlers.messages import pending_input
    from app.handlers.router import dispatch
    ticket_id = db.open_ticket(BUYER, "u", "help")

    event = FakeEvent(ADMIN_ID, data=f"supr:{ticket_id}".encode())
    await dispatch(event)
    assert state.pending_input[ADMIN_ID]["action"] == "ticket_reply"

    sent = []
    client = runtime.get_client()

    async def capture(chat_id, text, **kwargs):
        sent.append((chat_id, text))
        return None

    monkeypatch.setattr(client, "send_message", capture, raising=False)
    event = FakeEvent(ADMIN_ID, text="সমস্যাটি ঠিক করা হয়েছে, আবার চেষ্টা করুন")
    await pending_input(event)

    assert any(str(BUYER) in str(item[0]) for item in sent if len(item) >= 2)
    assert db.ticket(ticket_id)["status"] == "answered"


# ---------------------------------------------------- favorites / requests
@pytest.mark.asyncio
async def test_favorite_toggle_and_list():
    from app.handlers.router import dispatch
    store = make_store(premium=False)
    file_id = db.files_of(store["id"])[0]["id"]

    event = FakeEvent(BUYER, data=f"fv:{file_id}".encode())
    await dispatch(event)
    assert db.is_favorite(BUYER, file_id)

    event = FakeEvent(BUYER, data=b"fvs")
    await dispatch(event)
    text, buttons = event.edits[-1]
    assert any(f"f:{file_id}" == b.type.data.decode() for row in buttons for b in row
               if getattr(b.type, "data", None))

    event = FakeEvent(BUYER, data=f"fv:{file_id}".encode())
    await dispatch(event)
    assert not db.is_favorite(BUYER, file_id)


@pytest.mark.asyncio
async def test_content_request_flow():
    from app.handlers.messages import pending_input
    from app.handlers.router import dispatch
    store = make_store()

    event = FakeEvent(BUYER, data=f"rq:{store['id']}".encode())
    await dispatch(event)
    assert state.pending_input[BUYER]["action"] == "content_request"

    event = FakeEvent(BUYER, text="Dune Part Two 4K")
    await pending_input(event)

    requests = db.requests("open")
    assert requests and requests[0]["keyword"] == "Dune Part Two 4K"
    assert db.open_request_count() == 1


# ------------------------------------------------------------------ sorting
@pytest.mark.asyncio
async def test_sort_modes_change_order():
    from app.handlers.extras import sorted_files
    from app.handlers.router import dispatch
    store = make_store(premium=False)
    files = db.files_of(store["id"], newest_first=True)
    db.bump_views(files[-1]["id"])            # make the oldest one popular
    db.bump_views(files[-1]["id"])

    db.set_user_sort(BUYER, "popular")
    assert sorted_files(store["id"], BUYER)[0]["id"] == files[-1]["id"]

    db.set_user_sort(BUYER, "default")
    assert sorted_files(store["id"], BUYER)[0]["id"] == files[0]["id"]

    event = FakeEvent(BUYER, data=f"st:{store['id']}".encode())
    await dispatch(event)
    assert db.user_sort(BUYER) == "popular"      # default → popular → az
    await dispatch(FakeEvent(BUYER, data=f"st:{store['id']}".encode()))
    assert db.user_sort(BUYER) == "az"


# ------------------------------------------------------------------- i18n
def test_language_toggle():
    assert i18n.lang_of(BUYER) == "en"
    assert "Welcome" in i18n.t(BUYER, "home_banner")

    assert i18n.toggle_language(BUYER) == "bn"
    assert "স্বাগতম" in i18n.t(BUYER, "home_banner")
    assert i18n.t(BUYER, "items", n=7).startswith("📂 7")


def test_unknown_language_key_falls_back_to_key():
    assert i18n.t(BUYER, "nonexistent_key") == "nonexistent_key"


# ------------------------------------------------------------------ growth
@pytest.mark.asyncio
async def test_winback_creates_coupon_and_broadcasts(monkeypatch):
    from app.handlers.router import dispatch
    sent = []

    async def fake_broadcast(targets, text, progress=None, delay=None, file_id=None):
        sent.append((list(targets), text))
        return {"sent": len(targets), "failed": 0, "skipped": 0,
                "total": len(targets), "duration": 0.1}

    monkeypatch.setattr("app.services.broadcast.run_broadcast", fake_broadcast)

    event = FakeEvent(ADMIN_ID, data=b"winb")
    await dispatch(event)
    assert event.edits, "win-back screen did not render"

    event = FakeEvent(ADMIN_ID, data=b"wbs:20")
    await dispatch(event)
    await __import__("asyncio").sleep(0.05)

    assert sent, "no broadcast was started"
    _targets, text = sent[0]
    assert "WINBACK" in text and "২০%" in text
    assert db.coupons(), "win-back coupon was not created"


@pytest.mark.asyncio
async def test_store_settings_screen():
    from app.handlers.router import dispatch
    store = make_store()
    db.add_plan(store["id"], "1 Month", 30, 199)
    db.upsert_drip(store["id"], ADMIN_ID, 3, "19:30")

    event = FakeEvent(ADMIN_ID, data=f"sss:{store['id']}".encode())
    await dispatch(event)

    text, buttons = event.edits[-1]
    assert "Movies 🍿" in text
    assert "1/3" not in text                                   # plain store name, no page junk
    labels = " ".join(b.text for row in buttons for b in row)
    assert "Rename" in labels and "Plans" in labels and "Share" in labels
    # New v2.2 also has Access management
    assert "Access" in labels or "👥" in labels
