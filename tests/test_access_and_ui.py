"""Access control, deep-link parsing and button payload safety."""
from __future__ import annotations

import time

import pytest

from app import config as cfg, keyboards
from app.payloads import (KIND_CODE, KIND_FILE, KIND_LINK, KIND_REFERRAL,
                          KIND_SLUG, KIND_STORE, resolve_payload)
from app.services import access
from app.storage import Database
from app.utils import button_data


@pytest.fixture()
def db(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr("app.services.access.db", database)
    return database


def _buttons(keyboard):
    for row in keyboard:
        for button in row:
            yield button


# ------------------------------------------------------------------- access
def test_free_store_is_open_to_everyone(db):
    store = db.create_store(1, "Free stuff")
    db.update_store(store["id"], is_premium=0)
    assert access.has_access(db.store(store["id"]), 999)


def test_premium_store_needs_a_grant(db):
    store = db.create_store(1, "Paid")
    assert not access.has_access(db.store(store["id"]), 999)

    db.grant(store["id"], 999, time.time() + 3600)
    assert access.has_access(db.store(store["id"]), 999)

    db.grant(store["id"], 999, time.time() - 5)      # expired
    assert not access.has_access(db.store(store["id"]), 999)

    db.grant(store["id"], 999, None)                 # lifetime
    assert access.has_access(db.store(store["id"]), 999)


def test_admins_always_have_access(db, monkeypatch):
    monkeypatch.setattr(cfg, "ADMIN_IDS", [4242])
    store = db.create_store(1, "Paid")
    assert access.has_access(db.store(store["id"]), 4242)


def test_broadcast_targets_filter_correctly(db, monkeypatch):
    monkeypatch.setattr(cfg, "ADMIN_IDS", [1])
    paid = db.create_store(1, "Paid")
    free = db.create_store(1, "Free")
    db.update_store(free["id"], is_premium=0)

    for user_id in (10, 11, 12):
        db.touch_user(user_id, f"User{user_id}", None)
    db.grant(paid["id"], 10, None)
    db.toggle_sub(free["id"], 12)

    assert sorted(access.broadcast_targets(1, "all")) == [10, 11, 12]
    assert access.broadcast_targets(1, "premium") == [10]
    assert sorted(access.broadcast_targets(1, "free")) == [11, 12]
    assert sorted(access.broadcast_targets(1, f"store:{free['id']}")) == [12]
    assert access.broadcast_targets(1, f"store:{paid['id']}") == [10]


# --------------------------------------------------------------- deep links
@pytest.mark.parametrize("payload,expected", [
    ("f12", (KIND_FILE, 12)),
    ("s3", (KIND_STORE, 3)),
    ("tAbCd1234", (KIND_LINK, "AbCd1234")),
    ("r8768764605", (KIND_REFERRAL, 8768764605)),
    ("ref_8768764605", (KIND_REFERRAL, 8768764605)),
    ("FILE_DIR_15", (KIND_CODE, "FILE_DIR_15")),
    ("FILE_-100123_99", (KIND_CODE, "FILE_-100123_99")),
    ("STORE_movies", (KIND_SLUG, "movies")),
    ("LINK_abc123", (KIND_LINK, "abc123")),
    ("BATCH_FILE_DIR_1_AND_FILE_DIR_2", ("legacy_batch", "FILE_DIR_1_AND_FILE_DIR_2")),
])
def test_payload_resolution(payload, expected):
    assert resolve_payload(payload) == expected


def test_empty_payload():
    assert resolve_payload("") is None
    assert resolve_payload("   ") is None


def test_links_fit_in_telegram_limits():
    """Bug: long / Bangla store names used to overflow the 64 byte callback data
    limit and break every button. Ids are short, so this can never happen now."""
    long_name = "সবচেয়ে ভালো মুভি কালেকশন বাংলা ডাবিং সহ 🍿🎬🔥"
    store = {"id": 987654, "name": long_name, "admin_id": 123456789, "is_premium": 1}
    files = [{"id": 1000000 + i, "name": f"খুব লম্বা নামের ফাইল {i}", "kind": "Video",
              "views": 12345} for i in range(6)]
    selected = {file["id"] for file in files[:3]}

    keyboards_to_check = [
        keyboards.user_store_list([store]),
        keyboards.store_content(store, files, 0, 500, subscribed=False),
        keyboards.search_results(store, files),
        keyboards.link_generator(store, files, 0, selected, 60),
        keyboards.admin_panel(1, long_name, True, True, "@channel", True),
        keyboards.store_picker([store], "dst:", "adm:back", mark_active=store["id"]),
        keyboards.file_manager(store, files, 0, 60),
        keyboards.file_actions(files[0]["id"], store["id"]),
        keyboards.expiry_choices(),
        keyboards.duration_choices("gd:"),
        keyboards.users_page(0, 100),
        keyboards.broadcast_filters([store]),
        keyboards.scan_chats([(1, long_name), (2, "Second")], 0),
        keyboards.move_targets([store], files[0]["id"], store["id"]),
    ]
    for keyboard in keyboards_to_check:
        for button in _buttons(keyboard):
            data = button_data(button)
            assert data is not None, f"button without payload: {button.text}"
            assert len(data) <= 64, f"payload too long for Telegram: {data!r}"
            assert button.text


def test_two_column_keyboards_handle_odd_counts():
    stores = [{"id": i, "name": f"S{i}", "is_premium": 0} for i in range(1, 6)]
    rows = keyboards.user_store_list(stores)
    assert [len(row) for row in rows] == [2, 2, 1]


@pytest.fixture(scope="module")
def registered_handlers(stub_bot):
    """Handler modules were imported by the session fixture in conftest."""
    return stub_bot.registered


def test_every_handler_module_registers(registered_handlers):
    from app.handlers import router
    assert len(registered_handlers) >= 8, "handlers did not register at all"
    assert router.ROUTE_COUNT >= 50, f"only {router.ROUTE_COUNT} routes registered"


def test_noop_never_reaches_a_route(registered_handlers):
    """The `1/3` page buttons carry data='noop'; the router answers them before
    matching so the loading spinner always stops."""
    from app.handlers.router import resolve
    assert resolve("noop") is None


def test_router_prefix_matching(registered_handlers):
    from app.handlers.router import resolve

    samples = {
        "f:12": "f:", "s:3": "s:", "sp:3:1": "sp:", "ss:3": "ss:", "sb:3": "sb:",
        "bs:0": "bs:", "rf": "rf", "hp": "hp",
        "adm:back": "adm:", "ast:5": "ast:", "dst:5": "dst:", "dc:5": "dc:",
        "gst:5": "gst:", "gd:3600": "gd:", "rst:5": "rst:", "rd:7": "rd:",
        "dr:5": "dr:", "drc:3": "drc:", "dro:5": "dro:", "bc:all": "bc:",
        "up:1": "up:", "sc:0": "sc:", "scp:1": "scp:", "scs": "scs",
        "fms:5": "fms:", "fmp:5:1": "fmp:", "fm:9": "fm:", "fmr:9": "fmr:",
        "fmd:9": "fmd:", "fmdq:9": "fmdq:", "fmm:9": "fmm:", "fmmv:9:5": "fmmv:",
        "fml:9": "fml:", "lg:5": "lg:", "lgt:9": "lgt:", "lgp:1": "lgp:",
        "lgg": "lgg", "lgc": "lgc", "lge:3600": "lge:",
    }
    for data, expected_prefix in samples.items():
        match = resolve(data)
        assert match is not None, f"no route for {data}"
        handler, rest = match
        assert rest == data[len(expected_prefix):], f"{data} -> unexpected match"
        assert handler is not None


def test_every_button_leads_somewhere(registered_handlers):
    """Guard against typos: every inline button a user can tap must resolve to a
    registered callback route (or be the deliberate 'noop')."""
    from app.handlers.router import resolve

    long_name = "বাংলা মুভি 🍿"
    store = {"id": 424242, "name": long_name, "admin_id": 999, "is_premium": 0,
             "slug": "bangla_muvi"}
    files = [{"id": 5000 + i, "name": f"File {i}", "kind": "Video", "views": i}
             for i in range(4)]

    keyboards_to_check = [
        keyboards.admin_panel(1, long_name, True, True, "@chan", False),
        keyboards.store_picker([store], "dst:", "adm:back", mark_active=store["id"]),
        keyboards.store_picker([store], "ast:", "adm:back"),
        keyboards.store_picker([store], "gst:", "adm:back"),
        keyboards.store_picker([store], "rst:", "adm:back"),
        keyboards.store_picker([store], "dr:", "adm:back"),
        keyboards.store_picker([store], "fms:", "adm:back"),
        keyboards.store_picker([store], "lg:", "adm:back"),
        keyboards.user_store_list([store]),
        keyboards.store_content(store, files, 0, 40, subscribed=False),
        keyboards.store_content(store, files, 3, 40, subscribed=True),
        keyboards.search_results(store, files),
        keyboards.link_generator(store, files, 0, {5000}, 40),
        keyboards.file_manager(store, files, 0, 40),
        keyboards.file_actions(5000, store["id"]),
        keyboards.move_targets([store], 5000, store["id"]),
        keyboards.expiry_choices(),
        keyboards.duration_choices("gd:"),
        keyboards.users_page(2, 40),
        keyboards.broadcast_filters([store]),
        keyboards.scan_chats([(1, long_name)], 0),
        keyboards.confirm("dc:7", "adm:back"),
        keyboards.subscription_buttons(store["id"], True, f"bs:{store['id']}"),
    ]
    unresolved = set()
    for keyboard in keyboards_to_check:
        for button in _buttons(keyboard):
            data = button_data(button)
            if data is None:
                continue                      # url buttons have no payload
            if data == b"noop":
                continue
            text = data.decode()
            if resolve(text) is None:
                unresolved.add(text)
    assert not unresolved, f"buttons without a handler: {sorted(unresolved)}"
