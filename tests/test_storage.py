"""Storage tests — including regression tests for the bugs found in v1."""
from __future__ import annotations

import json
import time

import pytest

from app.storage import Database


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "test.sqlite3"))


def test_store_and_file_roundtrip(db):
    store = db.create_store(111, "Movies 🍿")
    assert store["slug"] == "movies"
    assert store["is_premium"] == 1

    file_id = db.add_file(store["id"], "First movie", "Video", -100123, 42, code="FILE_-100123_42")
    assert file_id

    row = db.file(file_id)
    assert row["name"] == "First movie"
    assert row["views"] == 0

    db.bump_views(file_id)
    assert db.file(file_id)["views"] == 1


def test_duplicate_files_are_skipped(db):
    """Bug: scanning the same channel twice must not duplicate entries."""
    store = db.create_store(111, "Movies")
    assert db.add_file(store["id"], "a", "Video", -100, 1, code="FILE_-100_1")
    assert db.add_file(store["id"], "a", "Video", -100, 1, code="FILE_-100_1") is None
    assert db.add_file(store["id"], "b", "Video", -100, 1) is None      # same message
    assert db.add_file(store["id"], "b", "Video", -100, 2) is not None
    assert db.store_stats(store["id"])["files"] == 2


def test_legacy_file_codes_keep_working(db):
    store = db.create_store(5, "Old store")
    db.add_file(store["id"], "legacy", "Video", -1009, 7, code="FILE_DIR_7")
    assert db.file_by_code("FILE_DIR_7")["name"] == "legacy"
    assert db.file_by_code("nope") is None


def test_delete_store_removes_everything(db):
    """Bug: deleting a store used to leave orphan grants, drip rows and links."""
    store = db.create_store(111, "Movies")
    file_id = db.add_file(store["id"], "x", "Video", -100, 1) or 0
    token = db.create_link(111, [file_id], None)
    db.grant(store["id"], 555, None)
    db.upsert_drip(store["id"], 111, 3, "19:30")
    db.toggle_sub(store["id"], 555)
    db.mark_sent(store["id"], [file_id])
    db.set_referral_cfg(111, store["id"], 7)
    db.set_active_store(111, store["id"])

    db.delete_store(store["id"])

    assert db.store(store["id"]) is None
    assert db.file(file_id) is None
    assert db.link(token) is None
    assert db.store_grants(store["id"]) == []
    assert db.drip(store["id"]) is None
    assert db.subscribers(store["id"]) == []
    assert db.sent_file_ids(store["id"]) == set()
    assert db.referral_cfg(111) is None
    assert db.active_store_id(111) is None


def test_links_expire_and_are_purged(db):
    store = db.create_store(1, "S")
    fid = db.add_file(store["id"], "x", "Video", -1, 1)
    live = db.create_link(1, [fid], time.time() + 3600)
    dead = db.create_link(1, [fid], time.time() - 10)

    assert db.link(live) is not None
    removed = db.purge_expired_links()
    assert removed == 1
    assert db.link(dead) is None
    assert db.link(live) is not None
    assert db.link_file_ids(live) == [fid]


def test_grants_are_extended_not_overwritten(db):
    store = db.create_store(1, "S")
    future = db.extend_grant(store["id"], 99, 10)
    extended = db.extend_grant(store["id"], 99, 5)
    assert extended > future
    assert extended - future == pytest.approx(5 * 86400, abs=2)

    db.grant(store["id"], 98, None)                 # lifetime
    assert db.extend_grant(store["id"], 98, 5) == pytest.approx(time.time(), abs=5)


def test_users_survive_a_restart(db, tmp_path):
    """Bug: `all_bot_users` was memory only, so broadcasts reached nobody after a
    restart. Users now live in the database."""
    db.touch_user(7, "Rahim", "rahim")
    db.touch_user(8, "Karim", None)
    db.touch_user(7, "Rahim", "rahim")               # repeat visitor

    reopened = Database(db.path)
    assert sorted(reopened.all_user_ids()) == [7, 8]
    assert reopened.user_count() == 2
    assert reopened.user(7)["name"] == "Rahim"


def test_backup_writes_json(db, tmp_path):
    store = db.create_store(1, "S")
    db.add_file(store["id"], "x", "Video", -1, 1)
    db.touch_user(42, "User", None)
    path = db.backup_json(str(tmp_path / "backups"), keep=2)
    payload = json.loads(open(path, encoding="utf-8").read())
    assert payload["stats"]["files"] == 1
    assert payload["stats"]["users"] == 1


# --------------------------------------------------------------- migration
LEGACY = {
    "stores": {
        "8768764605": {
            "Movies": {
                "FILE_DIR_10": {"id": "FILE_DIR_10", "name": "Item_1", "type": "Video",
                                "msg_id": 10, "chat_id": -100123},
                "FILE_-100123_11": {"id": "FILE_-100123_11", "name": "Item_2", "type": "Photo",
                                    "msg_id": 11, "chat_id": -100123, "views": 5},
            }
        }
    },
    "active_store": {"8768764605": "Movies"},
    "force_channel": "@mychannel",
    "custom_caption": "Follow us!",
    "admin_sessions": {"8768764605": "SESSION_STRING"},
    "store_slugs": {"movies": [8768764605, "Movies"]},
    "temp_links": {"abc123": {"admin_uid": 8768764605,
                              "fids": ["FILE_DIR_10"],
                              "expires_at": None}},
    "bot_admin_chats": [-100999],
    "premium_access": {"8768764605": {"Movies": {"555": None, "556": 1893456000}}},
    "user_details": {"555": {"joined": 1700000000, "name": "Buyer"}},
    "referred_by": {"556": 555},
    "referral_rewarded": ["556"],
    "referral_config": {"8768764605": {"store": "Movies", "days": 7}},
    "drip_config": {"8768764605:Movies": {"admin_uid": 8768764605, "store": "Movies",
                                          "count": 3, "time": "19:30",
                                          "last_sent_date": "", "enabled": True}},
    "drip_sent_ids": {"8768764605:Movies": ["FILE_DIR_10"]},
    "drip_subscribers": {"8768764605:Movies": ["555"]},
}


def test_legacy_json_is_imported(tmp_path):
    legacy_path = tmp_path / "bot_db.json"
    legacy_path.write_text(json.dumps(LEGACY), encoding="utf-8")
    db = Database(str(tmp_path / "new.sqlite3"))

    counters = db.migrate_legacy(str(legacy_path))
    assert counters is not None
    assert counters["files"] == 2

    store = db.store_by_slug("movies")
    assert store["name"] == "Movies"
    assert store["admin_id"] == 8768764605
    assert db.store_stats(store["id"])["files"] == 2

    # old deep links still resolve
    legacy_file = db.file_by_code("FILE_DIR_10")
    assert legacy_file and legacy_file["views"] == 0
    assert db.file_by_code("FILE_-100123_11")["views"] == 5

    # links, grants, referrals, drip and settings all came across
    assert db.link("abc123") is not None
    assert db.link_file_ids("abc123") == [legacy_file["id"]]
    assert db.grant_row(store["id"], 555)["expires_at"] is None
    assert db.grant_row(store["id"], 556)["expires_at"] == 1893456000
    assert db.referrer_of(556) == 555 and db.is_rewarded(556)
    assert db.referral_cfg(8768764605)["days"] == 7
    assert db.drip(store["id"])["send_time"] == "19:30"
    assert db.sent_file_ids(store["id"]) == {legacy_file["id"]}
    assert db.subscribers(store["id"]) == [555]
    assert db.session(8768764605)["session_str"] == "SESSION_STRING"
    assert db.bot_chats() == {-100999}
    assert db.get_meta("force_channel") == "@mychannel"
    assert db.get_meta("custom_caption") == "Follow us!"
    assert db.active_store_id(8768764605) == store["id"]

    # importing twice must not duplicate anything
    assert db.migrate_legacy(str(legacy_path)) is None


def test_migration_handles_missing_legacy_file(db, tmp_path):
    assert db.migrate_legacy(str(tmp_path / "nope.json")) is None
