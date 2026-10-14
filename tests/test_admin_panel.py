"""The admin action trail (`/admin/audit`) — who did what, straight from `events`."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient      # noqa: E402

from app import config as cfg                  # noqa: E402
from app.services import audit, settings       # noqa: E402
from app.storage import db                     # noqa: E402
from tests.test_flows import ADMIN_ID, USER_ID  # noqa: E402
from tests.test_web import _auth               # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "audit.sqlite3"))
    settings._cache.clear()
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(cfg, "BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setattr(cfg, "API_ID", 12345)
    monkeypatch.setattr(cfg, "API_HASH", "0" * 32)
    import web.dashboard as dashboard
    monkeypatch.setattr(dashboard, "ADMIN_LOGIN_PASSWORD", "secret")
    dashboard._LOGIN_FAILURES.clear()
    return TestClient(dashboard.app)


# ------------------------------------------------------------------- the service
def test_record_and_read_back(tmp_path):
    db.bind(str(tmp_path / "audit2.sqlite3"))
    audit.record("settings_saved", ref="broadcast", detail="BROADCAST_BATCH=25")
    rows = audit.recent()
    assert rows and rows[0]["name"] == "settings_saved"
    assert audit.label("settings_saved").startswith("⚙")


def test_an_audit_row_does_not_pollute_the_conversion_funnel(tmp_path):
    """A grant is an admin action, not a visitor walking through the funnel."""
    db.bind(str(tmp_path / "audit3.sqlite3"))
    store = db.create_store(ADMIN_ID, "Funnel")
    db.log_event("open_store", USER_ID, store["id"], None, "x")
    db.log_event("deliver", USER_ID, store["id"], None, "x")
    db.grant(store["id"], USER_ID, None, "test")
    assert db.store_funnel(store["id"]) == {"came": 1, "watched": 1, "joined": 0, "paid": 0}


def test_the_store_of_an_audit_row_is_read_from_the_ref(tmp_path):
    db.bind(str(tmp_path / "audit4.sqlite3"))
    store = db.create_store(ADMIN_ID, "Netflix")
    db.grant(store["id"], USER_ID, None, "test")
    row = next(r for r in audit.recent() if r["name"] == "grant")
    assert row["store_name"] == "Netflix"
    assert row["ref"] == "test"          # the machine part is stripped for display


def test_filtering_by_name_and_store(tmp_path):
    db.bind(str(tmp_path / "audit5.sqlite3"))
    first = db.create_store(ADMIN_ID, "One")
    second = db.create_store(ADMIN_ID, "Two")
    db.ban_user(USER_ID, "spam", ADMIN_ID)
    assert [r["name"] for r in audit.recent(name="ban")] == ["user_banned"]
    assert {r["store_name"] for r in audit.recent(store_id=second["id"])} == {"Two"}
    assert first["name"] == "One"


# ----------------------------------------------------------------------- the page
def test_the_audit_page_is_reachable_and_shows_the_trail(client):
    store = db.create_store(ADMIN_ID, "Cinema")
    db.ban_user(USER_ID, "spam", ADMIN_ID)
    page = client.get("/admin/audit", headers=_auth())
    assert page.status_code == 200
    assert "অ্যাডমিন অ্যাকশন লগ" in page.text
    assert "user_banned" in page.text or "ইউজার ব্যান" in page.text
    assert "Cinema" in client.get(f"/admin/audit?name=store", headers=_auth()).text


def test_the_nav_links_to_the_audit_page(client):
    page = client.get("/admin", headers=_auth())
    assert page.status_code == 200
    assert "admin/audit" in page.text


# ------------------------------------------------------- analytics helpers (v3.2)
def test_dead_files_report_ignores_watched_files(tmp_path):
    db.bind(str(tmp_path / "dead.sqlite3"))
    store = db.create_store(ADMIN_ID, "Dead")
    watched = db.add_file(store["id"], "Hit", "Video", -100, 1, size=1000)
    db.add_file(store["id"], "Nobody", "Video", -100, 2, size=1000)
    db.bump_views(watched)
    names = {row["name"] for row in db.dead_files(days=30)}
    assert names == {"Nobody"}


def test_conversion_rate_is_paid_over_came(tmp_path):
    db.bind(str(tmp_path / "conv.sqlite3"))
    store = db.create_store(ADMIN_ID, "Conv")
    db.log_event("open_store", 11, store["id"], None, "x")
    db.log_event("open_store", 12, store["id"], None, "x")
    db.log_event("paid", 11, store["id"], None, "plan:1")
    rate = db.conversion_rate(days=30)
    assert rate["came"] == 2 and rate["paid"] == 1 and rate["rate"] == 50.0
