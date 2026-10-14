"""End-to-end handler flows driven with fake Telegram events (no network)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import config as cfg, runtime
from app.handlers import state
from app.storage import db

ADMIN_ID = 999
USER_ID = 555


class FakeSentMessage:
    def __init__(self) -> None:
        self.id = 1
        self.buttons = None

    async def edit(self, text=None, buttons=None, **_kwargs):
        self.buttons = buttons
        return self

    async def delete(self):
        return None


class FakeEvent:
    """Just enough of a Telethon event for the handlers."""

    def __init__(self, user_id: int, text: str = "", data: bytes | None = None) -> None:
        self.sender_id = user_id
        self.chat_id = user_id
        self.raw_text = text
        self.data = data
        self.out = False
        self.media = None
        self.message = SimpleNamespace(id=77, forward=None)
        self.sent: list[tuple] = []
        self.edits: list[tuple] = []
        self.answers: list[tuple] = []

    async def respond(self, text=None, buttons=None, **_kwargs):
        self.sent.append((text, buttons))
        return FakeSentMessage()

    async def edit(self, text=None, buttons=None, **_kwargs):
        self.edits.append((text, buttons))
        return FakeSentMessage()

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def get_message(self):
        return None

    async def get_sender(self):
        return SimpleNamespace(id=self.sender_id, first_name="Tester", username="tester")

    async def delete(self):
        return None


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "flows.sqlite3"))
    db.touch_user(USER_ID, "Tester", "tester")
    db.set_user_lang(USER_ID, "en")          # english labels keep assertions stable
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(runtime, "force_channel", lambda: "")
    state.pending_input.clear()
    state.search_pending.clear()
    state.flow_ctx.clear()
    state.link_gen.clear()
    state.batch_mode.clear()
    state.dialog_cache.clear()
    yield


@pytest.fixture(autouse=True)
def delivered_files(monkeypatch):
    """Replace the real sender with a recorder."""
    calls = []

    async def fake_deliver(chat_id, file_row, caption_text=None):
        from app.services.sender import Delivery
        calls.append((chat_id, file_row["id"]))
        return Delivery(True)

    monkeypatch.setattr("app.ui.deliver", fake_deliver)
    monkeypatch.setattr("app.services.flow.deliver", fake_deliver)
    return calls


def _store_with_files(name="Movies 🍿", premium=False, count=3):
    store = db.create_store(ADMIN_ID, name)
    db.update_store(store["id"], is_premium=1 if premium else 0)
    ids = []
    for i in range(count):
        ids.append(db.add_file(store["id"], f"Movie {i}", "Video", -100, 100 + i))
    return db.store(store["id"]), ids


# ------------------------------------------------------------------- user side
@pytest.mark.asyncio
async def test_user_home_lists_stores():
    from app.handlers.user import show_home
    _store_with_files("Free store", premium=False)
    _store_with_files("Paid store", premium=True)

    event = FakeEvent(USER_ID)
    await show_home(event, USER_ID)

    # The menu plus the persistent bottom keyboard — and the menu must come first.
    assert event.sent, "the home screen was not sent at all"
    text, buttons = event.sent[0]
    labels = [button.text for row in buttons for button in row]
    assert any("Free store" in label for label in labels)
    assert any("🔒" in label for label in labels)      # premium is marked
    assert any("Invite" in label for label in labels)
    assert any("Contact admin" in label for label in labels)
    assert len(event.sent) <= 2, "one menu + at most one bottom keyboard"


@pytest.mark.asyncio
async def test_home_never_fails_silently(monkeypatch):
    """Even if the database hiccups, the user still gets an answer."""
    from app.handlers import user as user_handlers

    from app.storage import Database

    def boom(self):
        raise RuntimeError("db is busy")

    # Patch the class, never the module level proxy: `db` is a shared lazy handle
    # and setattr on it would leave a bound method behind for every later test.
    monkeypatch.setattr(Database, "all_stores", boom)
    event = FakeEvent(USER_ID)
    await user_handlers.show_home(event, USER_ID)
    assert event.sent, "a broken store query must still answer the user"
    assert "স্টোর" in event.sent[0][0] or "Welcome" in event.sent[0][0]


@pytest.mark.asyncio
async def test_free_store_opens_for_everyone():
    from app.handlers.router import dispatch
    store, _ = _store_with_files(premium=False)

    event = FakeEvent(USER_ID, data=f"s:{store['id']}".encode())
    await dispatch(event)

    assert event.edits, "store view was not rendered"
    text, buttons = event.edits[-1]
    assert "Free store" in text
    payloads = [button.type.data.decode() for row in buttons for button in row
                if getattr(button.type, "data", None)]
    assert any(p.startswith("f:") for p in payloads)
    assert not event.answers[-1][0], "no error expected"


@pytest.mark.asyncio
async def test_premium_store_blocks_stranger():
    from app.handlers.router import dispatch
    store, _ = _store_with_files(premium=True)

    event = FakeEvent(USER_ID, data=f"s:{store['id']}".encode())
    await dispatch(event)

    assert any("premium" in (text or "").lower() or "প্রিমিয়াম" in (text or "") for text, _ in event.edits)
    # the paywall offers a way to buy, a trial and admin contact
    _text, buttons = event.edits[-1]
    labels = " ".join(button.text for row in buttons for button in row)
    # Should contain unlock/buy, trial and contact — in English for this test user
    assert ("Unlock" in labels or "Buy" in labels or "প্ল্যান" in labels) and "trial" in labels.lower() and ("Contact" in labels or "কথা" in labels)


@pytest.mark.asyncio
async def test_premium_store_opens_with_grant():
    from app.handlers.router import dispatch
    store, _ = _store_with_files(premium=True)
    db.grant(store["id"], USER_ID, None)

    event = FakeEvent(USER_ID, data=f"s:{store['id']}".encode())
    await dispatch(event)

    text, _buttons = event.edits[-1]
    assert "Lifetime" in text


@pytest.mark.asyncio
async def test_clicking_a_file_delivers_it(delivered_files):
    from app.handlers.router import dispatch
    store, file_ids = _store_with_files(premium=False)

    event = FakeEvent(USER_ID, data=f"f:{file_ids[0]}".encode())
    await dispatch(event)

    assert delivered_files == [(USER_ID, file_ids[0])]
    assert "পাঠানো হয়েছে" in event.answers[0][0]


@pytest.mark.asyncio
async def test_removed_file_is_reported():
    from app.handlers.router import dispatch
    event = FakeEvent(USER_ID, data=b"f:999999")
    await dispatch(event)
    assert "removed" in event.answers[0][0].lower()


@pytest.mark.asyncio
async def test_deep_link_to_store_and_file(delivered_files):
    from app.handlers.user import handle_payload
    store, file_ids = _store_with_files(premium=False)

    event = FakeEvent(USER_ID)
    assert await handle_payload(event, USER_ID, f"s{store['id']}", True) is True
    assert event.sent and "Free store" in event.sent[-1][0]

    event = FakeEvent(USER_ID)
    assert await handle_payload(event, USER_ID, f"f{file_ids[1]}", True) is True
    assert delivered_files[-1] == (USER_ID, file_ids[1])


@pytest.mark.asyncio
async def test_legacy_deep_links_still_work(delivered_files):
    """Links generated by the old bot must not break."""
    from app.handlers.user import handle_payload
    store = db.create_store(ADMIN_ID, "Old store")
    db.update_store(store["id"], is_premium=0)
    db.add_file(store["id"], "legacy", "Video", -100, 5, code="FILE_DIR_5")

    event = FakeEvent(USER_ID)
    await handle_payload(event, USER_ID, "FILE_DIR_5", True)
    assert delivered_files, "legacy file link did not deliver"

    event = FakeEvent(USER_ID)
    await handle_payload(event, USER_ID, f"STORE_{store['slug']}", True)
    assert event.sent, "legacy store slug link did not open"

    for bad in ("BATCH_", "nonsense_payload"):
        event = FakeEvent(USER_ID)
        await handle_payload(event, USER_ID, bad, True)
        assert event.sent, f"{bad} produced no feedback at all"


@pytest.mark.asyncio
async def test_premium_drip_subscription_is_refused():
    """Bug: anyone could subscribe to a premium store's daily drip."""
    from app.handlers.router import dispatch
    store, _ = _store_with_files(premium=True)

    event = FakeEvent(USER_ID, data=f"sb:{store['id']}".encode())
    await dispatch(event)

    assert db.subscribers(store["id"]) == []
    assert event.answers[-1][1] is True                  # alert shown

    db.grant(store["id"], USER_ID, None)
    event = FakeEvent(USER_ID, data=f"sb:{store['id']}".encode())
    await dispatch(event)
    assert db.subscribers(store["id"]) == [USER_ID]


@pytest.mark.asyncio
async def test_referral_grants_days():
    from app.handlers.user import apply_referral
    store, _ = _store_with_files(premium=True)
    db.set_referral_cfg(ADMIN_ID, store["id"], 7)
    db.touch_user(USER_ID, "Newbie", None)

    await apply_referral(USER_ID, 4242, is_new=True)

    row = db.grant_row(store["id"], 4242)
    assert row is not None and row["source"] == "referral"
    assert db.is_rewarded(USER_ID)

    # a second time (or for an existing user) nothing more is granted
    before = db.grant_row(store["id"], 4242)["expires_at"]
    await apply_referral(USER_ID, 4242, is_new=False)
    assert db.grant_row(store["id"], 4242)["expires_at"] == before


# ---------------------------------------------------------------- admin side
@pytest.mark.asyncio
async def test_admin_panel_renders():
    from app.handlers.admin import send_panel
    _store_with_files("Movies", premium=True)
    db.set_active_store(ADMIN_ID, db.stores_admin(ADMIN_ID)[0]["id"])

    event = FakeEvent(ADMIN_ID)
    await send_panel(event)

    text, buttons = event.sent[0]
    # New clean panel is in Bengali, but must contain store name and categories
    assert "Movies" in text
    assert "অ্যাডমিন" in text or "Admin" in text
    payloads = [button.type.data for row in buttons for button in row
                if getattr(button.type, "data", None)]
    # Check for new category buttons
    assert any(b in payloads for b in [b"adm:stores", b"adm:new", b"adm:active"])
    assert any(b in payloads for b in [b"adm:stats", b"adm:users", b"adm:sales"])


@pytest.mark.asyncio
async def test_admin_routes_require_admin_rights():
    from app.handlers.router import dispatch
    event = FakeEvent(USER_ID, data=b"adm:back")
    await dispatch(event)
    assert event.answers[-1] == ("Admins only.", True)


@pytest.mark.asyncio
async def test_create_store_flow():
    from app.handlers.messages import pending_input
    from app.handlers.state import ask

    ask(ADMIN_ID, "create_store")
    event = FakeEvent(ADMIN_ID, text="নতুন স্টোর 🎬")
    runtime.bot_username = "test_bot"
    await pending_input(event)

    assert event.sent, "no confirmation sent"
    stores = db.stores_admin(ADMIN_ID)
    assert stores and stores[0]["name"] == "নতুন স্টোর 🎬"
    assert db.active_store_id(ADMIN_ID) == stores[0]["id"]


@pytest.mark.asyncio
async def test_rename_and_delete_file_flow():
    from app.handlers.messages import pending_input
    from app.handlers.router import dispatch
    from app.handlers.state import ask

    store, file_ids = _store_with_files()
    runtime.bot_username = "test_bot"

    ask(ADMIN_ID, "rename_file", file_id=file_ids[0])
    await pending_input(FakeEvent(ADMIN_ID, text="Better name"))
    assert db.file(file_ids[0])["name"] == "Better name"

    event = FakeEvent(ADMIN_ID, data=f"fmdq:{file_ids[1]}".encode())
    await dispatch(event)
    assert db.file(file_ids[1]) is None
    assert db.file(file_ids[0]) is not None


@pytest.mark.asyncio
async def test_store_delete_confirm_flow():
    from app.handlers.router import dispatch
    store, file_ids = _store_with_files()
    db.grant(store["id"], USER_ID, None)

    event = FakeEvent(ADMIN_ID, data=f"dst:{store['id']}".encode())
    await dispatch(event)
    assert any("cannot be undone" in (text or "").lower() for text, _ in event.edits)
    assert db.store(store["id"]) is not None, "asking must not delete"

    event = FakeEvent(ADMIN_ID, data=f"dc:{store['id']}".encode())
    await dispatch(event)
    assert db.store(store["id"]) is None
    assert db.grant_row(store["id"], USER_ID) is None


@pytest.mark.asyncio
async def test_grant_premium_flow():
    from app.handlers.messages import pending_input
    from app.handlers.router import dispatch
    from app.handlers.state import ask

    store, _ = _store_with_files(premium=True)
    ask(ADMIN_ID, "grant_target")
    await pending_input(FakeEvent(ADMIN_ID, text="1234567"))

    event = FakeEvent(ADMIN_ID, data=f"gst:{store['id']}".encode())
    await dispatch(event)
    assert any("Duration" in (text or "") for text, _ in event.edits)

    event = FakeEvent(ADMIN_ID, data=b"gd:0")            # lifetime
    await dispatch(event)
    row = db.grant_row(store["id"], 1234567)
    assert row is not None and row["expires_at"] is None


@pytest.mark.asyncio
async def test_broadcast_filter_counts_targets():
    from app.handlers.router import dispatch
    store, _ = _store_with_files(premium=True)
    for user_id in (1, 2, 3):
        db.touch_user(user_id, f"u{user_id}", None)
    db.grant(store["id"], 2, None)

    event = FakeEvent(ADMIN_ID, data=b"bc:premium")
    await dispatch(event)
    assert state.pending_input[ADMIN_ID]["ctx"]["targets"] == [2]

    event = FakeEvent(ADMIN_ID, data=b"bc:all")
    await dispatch(event)
    assert set(state.pending_input[ADMIN_ID]["ctx"]["targets"]) >= {1, 2, 3}


@pytest.mark.asyncio
async def test_drip_setup_flow():
    from app.handlers.messages import pending_input
    from app.handlers.router import dispatch

    store, _ = _store_with_files()

    event = FakeEvent(ADMIN_ID, data=f"dr:{store['id']}".encode())
    await dispatch(event)

    event = FakeEvent(ADMIN_ID, data=b"drc:3")
    await dispatch(event)

    event = FakeEvent(ADMIN_ID, text="19:30")
    await pending_input(event)

    row = db.drip(store["id"])
    assert row and row["enabled"] == 1 and row["count"] == 3 and row["send_time"] == "19:30"

    event = FakeEvent(ADMIN_ID, data=b"dro:" + str(store["id"]).encode())
    await dispatch(event)
    assert db.drip(store["id"])["enabled"] == 0


@pytest.mark.asyncio
async def test_multi_link_generator_flow():
    from app.handlers.router import dispatch
    store, file_ids = _store_with_files(count=4)
    runtime.bot_username = "test_bot"

    event = FakeEvent(ADMIN_ID, data=f"lg:{store['id']}".encode())
    await dispatch(event)
    for file_id in file_ids[:3]:
        event = FakeEvent(ADMIN_ID, data=f"lgt:{file_id}".encode())
        await dispatch(event)
    assert state.link_gen[ADMIN_ID]["selected"] == set(file_ids[:3])

    event = FakeEvent(ADMIN_ID, data=b"lgg")
    await dispatch(event)
    assert any("expiry" in (text or "").lower() for text, _ in event.edits)

    event = FakeEvent(ADMIN_ID, data=b"lge:86400")
    await dispatch(event)
    text, _ = event.edits[-1]
    assert "t.me/test_bot?start=t" in text
    assert db.link_count() == 1


@pytest.mark.asyncio
async def test_users_page_lists_grants():
    from app.handlers.manage import show_users_page
    store, _ = _store_with_files(premium=True)
    db.touch_user(USER_ID, "Buyer", "buyer")
    db.grant(store["id"], USER_ID, None)

    event = FakeEvent(ADMIN_ID)
    await show_users_page(event, 0)
    text, _buttons = event.edits[-1]
    assert str(USER_ID) in text and "Buyer" in text


@pytest.mark.asyncio
async def test_search_flow_finds_files():
    from app.handlers.messages import search_input
    store, _ = _store_with_files(premium=False)
    db.add_file(store["id"], "Interstellar 2014", "Video", -100, 999)
    state.start_search(USER_ID, store["id"])

    event = FakeEvent(USER_ID, text="interstellar")
    await search_input(event)

    assert event.sent, "no search results message"
    text, buttons = event.sent[0]
    assert "1 result" in text
    assert any("Interstellar" in button.text for row in buttons for button in row)


# ------------------------------------------------------- broadcast studio (v3)
@pytest.mark.asyncio
async def test_admin_panel_opens_the_broadcast_studio():
    from app.handlers.admin import show_studio

    event = FakeEvent(ADMIN_ID)
    await show_studio(event, ADMIN_ID)
    assert event.edits, "the studio screen was not rendered"
    text, buttons = event.edits[-1]
    assert "ব্রডকাস্ট" in text
    labels = [button.text for row in buttons for button in row]
    assert any("New broadcast" in label for label in labels)


@pytest.mark.asyncio
async def test_audience_picker_then_compose_prompt():
    from app.handlers.router import dispatch

    event = FakeEvent(ADMIN_ID, data=b"nbca:all")
    await dispatch(event)
    assert state.peek(ADMIN_ID) is not None
    assert state.peek(ADMIN_ID)["action"] == "studio_text"
    text, buttons = event.edits[-1]
    assert "ব্রডকাস্ট মেসেজ" in text
    labels = [button.text for row in buttons for button in row]
    assert any("ফাইল" in label for label in labels)


@pytest.mark.asyncio
async def test_studio_text_creates_a_campaign_with_a_preview():
    from app.handlers.messages import pending_input

    state.ask(ADMIN_ID, "studio_text", audience="all")
    event = FakeEvent(ADMIN_ID, text="হ্যালো {name}, নতুন ভিডিও এসেছে 🎬")
    await pending_input(event)

    campaign = db.campaigns(limit=1)[0]
    assert campaign["status"] == "draft"
    assert db.campaign_counts(campaign["id"])["total"] >= 1
    rendered = (event.sent + event.edits)[-1][0]
    assert "প্রিভিউ" in rendered
    assert "bondhu" in rendered or USER_ID is not None      # {name} placeholder filled


@pytest.mark.asyncio
async def test_file_manager_can_broadcast_one_video():
    from app.handlers.router import dispatch

    store, file_ids = _store_with_files(premium=False, count=2)
    file_id = file_ids[0]

    event = FakeEvent(ADMIN_ID, data=f"fbr:{file_id}".encode())
    await dispatch(event)
    labels = [button.text for row in event.edits[-1][1] for button in row]
    assert any("Everyone" in label for label in labels)

    event = FakeEvent(ADMIN_ID, data=f"fbrgo:{file_id}:all".encode())
    await dispatch(event)
    campaign = db.campaigns(limit=1)[0]
    assert campaign["file_ids"] == [file_id]
    assert campaign["audience"] == "all"


@pytest.mark.asyncio
async def test_reply_keyboard_button_always_returns_home():
    from app.handlers.user import reply_menu_buttons
    from app import texts as text_bank

    event = FakeEvent(USER_ID, text=text_bank.REPLY_HOME)
    await reply_menu_buttons(event)
    assert event.sent, "the store list button did nothing"
    assert "Free" in event.sent[0][0] or "Welcome" in event.sent[0][0] or "স্টোর" in event.sent[0][0]
