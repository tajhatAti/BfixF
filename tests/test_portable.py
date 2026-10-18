"""এক জায়গা থেকে আরেক জায়গায় পুরো ইনস্টল নেওয়ার সিস্টেম।

এই টেস্টগুলোই বলে দেয় “হোস্টিং বদলালে কিছু হারাবে না” কথাটা সত্যি: ব্যাকআপ →
নতুন (খালি) ইনস্টলে ফিরিয়ে আনা → ইউজার আইডি, অ্যাক্সেস-হ্যাশ, স্টোর, ফাইল,
সেটিংস সব ফিরে আসে; আর ভুল/নষ্ট ফাইল দিলে কিছুই বদলায় না।
"""
from __future__ import annotations

import json
import os

import pytest

from app import config as cfg
from app.services import portable, settings
from app.storage import db


@pytest.fixture()
def install(tmp_path, monkeypatch):
    """A little install with everything that must survive a move."""
    monkeypatch.setattr(cfg, "BACKUP_DIR", str(tmp_path / "backups"))
    db.bind(str(tmp_path / "host-a.sqlite3"))
    settings._cache.clear()
    db.touch_user(101, "রহিম", "rahim")
    db.set_user_peer(101, 550123, "রহিম")
    db.touch_user(102, "করিম", "karim")
    store = db.create_store(101, "সিনেমা হাউস")
    db.add_file(store["id"], "Interstellar", "Video", -100, 11, size=2_000_000)
    db.add_file(store["id"], "Dune", "Video", -100, 12, size=3_000_000)
    db.grant(store["id"], 102, None, "test")
    settings.set("WEB_TITLE", "আমার স্টোর")
    settings.set("BROADCAST_DEFAULT_BUTTONS", "সাইট | https://t.me/x")
    db.set_meta("cred:BOT_TOKEN", "1234567890:" + "A" * 35)
    db.set_meta("cred:API_ID", "12345")
    db.set_meta("cred:API_HASH", "0" * 32)
    db.set_meta("cred:ADMIN_IDS", "101")
    return store


def test_backup_holds_everything_and_says_so(install):
    data = portable.export_bytes(note="test")
    info = portable.describe(data)
    assert info["ok"] is True
    assert info["counts"]["users"] == 2
    assert info["counts"]["stores"] == 1
    assert info["counts"]["files"] == 2
    assert info["credentials_included"] is True          # token travelled along
    assert portable.filename().endswith(portable.SUFFIX)


def test_restore_brings_everything_back_on_another_host(install, tmp_path, monkeypatch):
    data = portable.export_bytes()
    # “নতুন হোস্ট”: একেবারে খালি ডেটাবেজ, অন্য ফাইলনাম
    db.bind(str(tmp_path / "host-b.sqlite3"))
    settings._cache.clear()
    assert db.stats()["users"] == 0

    report = portable.restore(data, source="test")
    assert report["ok"] is True
    assert report["counts_after"]["users"] == 2
    assert db.stats()["users"] == 2 and db.stats()["files"] == 2
    peer = db.user_peer(101)
    assert peer["access_hash"] == 550123                  # broadcasts keep working
    assert peer["name"] == "রহিম"
    assert db.all_stores()[0]["name"] == "সিনেমা হাউস"
    assert settings.get_str("WEB_TITLE", "") == "আমার স্টোর"
    assert db.get_meta("cred:BOT_TOKEN", "").endswith("A" * 5)
    assert report["credentials_included"] is True
    assert os.path.isfile(report["safety_copy"])           # আগের অবস্থা সেভ করা আছে


def test_a_restore_can_be_undone_with_its_safety_copy(install, tmp_path):
    first = portable.export_bytes()
    db.touch_user(999, "extra", "x")                      # something we will lose
    second = portable.export_bytes()
    report = portable.restore(first, source="test")
    assert db.user(999) is None
    portable.restore(open(report["safety_copy"], "rb").read(), source="safety")
    assert db.user(999) is not None                       # back to the safety copy
    assert portable.describe(second)["counts"]["users"] == 3


def test_restore_never_touches_the_database_when_the_file_is_wrong(install):
    before = db.stats()["users"]
    for junk in (b"", b"not a backup at all", b'{"magic": "nope"}',
                 b'{"magic": "BFIXF-BACKUP/1", "db": "AAAA"}'):
        with pytest.raises(portable.BackupError):
            portable.restore(junk, source="test")
        assert db.stats()["users"] == before
    # a real SQLite file that is not ours (no users/stores tables) is refused too
    import sqlite3
    tmp = os.path.join(os.path.dirname(db.path), "foreign.sqlite3")
    conn = sqlite3.connect(tmp)
    conn.execute("CREATE TABLE something(x)")
    conn.commit()
    conn.close()
    with pytest.raises(portable.BackupError):
        portable.restore(open(tmp, "rb").read(), source="test")
    assert db.stats()["users"] == before


def test_a_tampered_backup_is_caught_by_its_checksum(install):
    envelope = portable.export_envelope()
    raw = json.loads(json.dumps(envelope))
    raw["db"] = raw["db"][:-8] + "AAAAAAAA"               # damaged payload
    data = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    with pytest.raises(portable.BackupError):
        portable.describe(data)


def test_the_paste_in_code_works_even_in_pieces_and_with_chatter(install):
    data = portable.export_bytes()
    codes = portable.text_codes(data, chunk=1200)
    assert len(codes) > 1
    assert portable.decode_text("\n".join(codes)) == data
    shuffled = list(reversed(codes))
    assert portable.decode_text("এই নিন:\n" + "\n".join(shuffled) + "\nধন্যবাদ") == data
    with pytest.raises(portable.BackupError):
        portable.decode_text("BFIXF1:কিছুই না")


def test_the_backup_file_needs_no_outside_files(install, tmp_path, monkeypatch):
    """Only the database is needed — media lives in Telegram, so a move is one file."""
    data = portable.export_bytes()
    # move to a fresh directory: nothing from the old host exists here
    monkeypatch.setattr(cfg, "BACKUP_DIR", str(tmp_path / "fresh" / "backups"))
    db.bind(str(tmp_path / "fresh" / "new.sqlite3"))
    portable.restore(data, source="test")
    assert db.stats()["files"] == 2
    assert db.files_of(db.all_stores()[0]["id"])[0]["name"] in ("Interstellar", "Dune")


def test_local_backups_lists_what_sits_on_the_server(install):
    import time
    path = os.path.join(str(cfg.BACKUP_DIR), portable.filename())
    os.makedirs(str(cfg.BACKUP_DIR), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(portable.export_bytes(note="saved"))
    rows = portable.local_backups()
    assert rows and rows[0]["name"].endswith(portable.SUFFIX)
    assert rows[0]["size"] > 0 and rows[0]["mtime"] <= time.time() + 1
    assert "সেফটি" not in json.dumps(rows, ensure_ascii=False)


def test_the_migration_checklist_covers_the_real_steps():
    steps = " ".join(portable.migration_steps())
    assert "/restore" in steps or "restore" in steps
    assert "setup" in steps                # token/API id when the backup has none
    assert "health" in steps or "self-test" in steps


@pytest.mark.asyncio
async def test_a_restore_wakes_the_bot_worker(tmp_path, monkeypatch):
    """নতুন হোস্টে রিস্টোরের পর বট নিজেই চালু হয় — রিস্টার্ট লাগে না।"""
    from app.services import credentials
    monkeypatch.setattr(cfg, "BACKUP_DIR", str(tmp_path / "backups"))
    db.bind(str(tmp_path / "wake.sqlite3"))
    settings._cache.clear()
    monkeypatch.setattr(cfg, "BOT_TOKEN", "")
    monkeypatch.setattr(cfg, "API_ID", 0)
    monkeypatch.setattr(cfg, "API_HASH", "")
    monkeypatch.setattr(cfg, "ADMIN_IDS", [])
    assert credentials.is_configured() is False

    db.set_meta("cred:BOT_TOKEN", "1234567890:" + "A" * 35)
    db.set_meta("cred:API_ID", "12345")
    db.set_meta("cred:API_HASH", "0" * 32)
    db.set_meta("cred:ADMIN_IDS", "101")
    assert credentials.load() is True                # what the restore route calls
    credentials.poke()
    assert credentials._event().is_set()             # `run.bot_worker` can wake up


# ---------------------------------------- পুরোনো ভার্সন / পুরোনো বটের ডেটা আনা
OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE users (user_id INTEGER PRIMARY KEY, name TEXT, username TEXT,
  joined_at REAL, last_seen REAL, lang TEXT DEFAULT 'bn', sort_pref TEXT DEFAULT 'default');
CREATE TABLE stores (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
  name TEXT NOT NULL, slug TEXT UNIQUE, created_at REAL, is_premium INTEGER DEFAULT 1,
  cover TEXT, description TEXT);
CREATE TABLE files (id INTEGER PRIMARY KEY AUTOINCREMENT, store_id INTEGER NOT NULL,
  name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'File', chat_id INTEGER, msg_id INTEGER,
  code TEXT UNIQUE, size INTEGER, duration INTEGER, created_at REAL, views INTEGER DEFAULT 0);
CREATE TABLE grants (store_id INTEGER NOT NULL, user_id INTEGER NOT NULL, expires_at REAL,
  granted_at REAL, source TEXT, PRIMARY KEY (store_id, user_id));
"""


def _old_version_database(tmp_path) -> str:
    """A database exactly like the project's first release (no access_hash, 33 tables)."""
    import sqlite3
    path = str(tmp_path / "old-version.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO users VALUES (101, 'পুরোনো রহিম', 'rahim', 111.0, 222.0, 'bn', 'default')")
    conn.execute("INSERT INTO users VALUES (102, 'পুরোনো করিম', 'karim', 111.0, 222.0, 'bn', 'default')")
    conn.execute("INSERT INTO stores (admin_id, name, slug, created_at, is_premium) "
                 "VALUES (101, 'পুরোনো স্টোর', 'old-store', 333.0, 1)")
    conn.execute("INSERT INTO files (store_id, name, kind, chat_id, msg_id, code, size, "
                 "created_at, views) VALUES (1, 'পুরোনো ভিডিও', 'Video', -100, 55, 'f1', 999, 444.0, 7)")
    conn.execute("INSERT INTO grants VALUES (1, 102, NULL, 555.0, 'legacy')")
    conn.commit()
    conn.close()
    return path


def test_an_older_versions_database_restores_and_upgrades_itself(install, tmp_path):
    """“আগের ভার্সনের SQLite ফাইলটা নতুন ওয়েবসাইটে দিলে কাজ করবে?” — হ্যাঁ।"""
    blob = open(_old_version_database(tmp_path), "rb").read()
    info = portable.inspect(blob)
    assert info["kind"] == portable.KIND_FULL        # পুরোনো হলেও এটা পুরো ইনস্টল
    assert info["merge"] is False and info["counts"]["users"] == 2

    report = portable.apply(blob, source="old version")
    assert report["ok"] and report["counts_after"]["users"] == 2
    user = db.user(101)
    assert user["name"] == "পুরোনো রহিম" and user["username"] == "rahim"
    assert user["last_seen"] == 222.0                     # পুরোনো ডেটা অটুট
    assert "access_hash" in user                          # নতুন কলাম নিজেই যোগ হয়েছে
    store = db.all_stores()[0]
    assert store["name"] == "পুরোনো স্টোর" and store["slug"] == "old-store"
    files = db.files_of(store["id"])
    assert files[0]["name"] == "পুরোনো ভিডিও" and files[0]["views"] == 7
    assert db.grant_row(store["id"], 102) is not None     # গ্রান্টও এসেছে
    assert portable.local_backups()                       # নিরাপত্তার সেফটি কপি আছে


def test_a_standalone_bots_users_db_is_merged_not_replaced(install, tmp_path):
    """পুরোনো আলাদা বটের `users.db` — ইউজার আইডিগুলো যোগ হয়, কিছু মোছে না।"""
    import sqlite3
    path = str(tmp_path / "users.db")
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY, blocked INTEGER DEFAULT 0,"
                       " username TEXT, name TEXT);")
    conn.executemany("INSERT INTO users VALUES (?,?,?,?)",
                     [(9001, 0, "olduser1", "পুরোনো নাম ১"),
                      (9002, 1, "olduser2", "পুরোনো নাম ২"),
                      (9003, 0, "", "পুরোনো নাম ৩")])
    conn.commit()
    conn.close()
    blob = open(path, "rb").read()

    info = portable.inspect(blob)
    assert info["kind"] == portable.KIND_USERS and info["merge"] is True
    report = portable.apply(blob, source="users.db")

    assert report["imported"]["added"] == 3
    assert report["imported"]["blocked"] == 1             # blocked → ব্যান হিসেবে
    assert db.user(101) is not None                       # আগের ইনস্টল অটুট
    assert db.user(9001)["name"] == "পুরোনো নাম ১"
    assert db.is_banned(9002) is True and db.user(9003) is not None
    # দুইবার পাঠালেও কেউ দুবার যোগ হয় না
    portable.apply(blob, source="users.db")
    assert db.stats()["users"] == 5


def test_the_legacy_json_backup_imports_stores_files_and_users(install, tmp_path):
    """পুরোনো `bot_db.json` — স্টোর, ফাইল, প্রিমিয়াম অ্যাক্সেস সব ফিরে আসে।"""
    legacy = {
        "user_details": {"7001": {"name": "লিগ্যাসি ইউজার", "joined": 1.0}},
        "stores": {"7001": {"লিগ্যাসি স্টোর": {"f999": {"name": "লিগ্যাসি ভিডিও",
                                                        "type": "Video", "chat_id": -100,
                                                        "msg_id": 77, "views": 3}}}},
        "premium_access": {"7001": {"লিগ্যাসি স্টোর": {"7001": 0}}},
        "store_slugs": {"legacy-slug": [7001, "লিগ্যাসি স্টোর"]},
    }
    blob = json.dumps(legacy, ensure_ascii=False).encode("utf-8")
    info = portable.inspect(blob)
    assert info["kind"] == portable.KIND_LEGACY_JSON and info["merge"] is True
    report = portable.apply(blob, source="bot_db.json")
    assert report["imported"]["stores"] == 1
    store = [row for row in db.all_stores() if row["name"] == "লিগ্যাসি স্টোর"][0]
    assert store["slug"] == "legacy-slug"
    assert db.files_of(store["id"])[0]["name"] == "লিগ্যাসি ভিডিও"
    assert db.user(7001) is not None
    assert db.grant_row(store["id"], 7001) is not None
    assert db.user(101) is not None                       # আগের ডেটা অটুট
    # একই ফাইল আবার দিলে স্টোর ডাবল হয় না
    portable.apply(blob, source="bot_db.json")
    assert len([row for row in db.all_stores() if row["name"] == "লিগ্যাসি স্টোর"]) == 1


def test_our_own_json_export_can_be_imported_too(install):
    """/admin/download/json — যারা ওটা রেখেছেন তাঁদের ডেটাও ফিরে আসে।"""
    document = {"version": 2,
                "users": {"8001": {"name": "এক্সপোর্ট ইউজার"}},
                "stores": {"1": {"name": "এক্সপোর্ট স্টোর", "slug": "x-store",
                                 "admin_id": 8001, "is_premium": 0,
                                 "files": {"f1": {"name": "এক্সপোর্ট ভিডিও",
                                                  "type": "Video", "chat_id": -100,
                                                  "msg_id": 5}}}},
                "grants": [{"store_id": 1, "user_id": 8001, "source": "x"}]}
    blob = json.dumps(document, ensure_ascii=False).encode("utf-8")
    assert portable.inspect(blob)["kind"] == portable.KIND_EXPORT_JSON
    report = portable.apply(blob, source="export.json")
    assert report["imported"]["stores"] == 1 and report["imported"]["files"] == 1
    store = [row for row in db.all_stores() if row["name"] == "এক্সপোর্ট স্টোর"][0]
    assert db.files_of(store["id"])[0]["name"] == "এক্সপোর্ট ভিডিও"


def test_a_foreign_database_without_users_is_still_refused(install, tmp_path):
    import sqlite3
    path = str(tmp_path / "random.sqlite3")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE notes(x)")
    conn.commit()
    conn.close()
    with pytest.raises(portable.BackupError):
        portable.inspect(open(path, "rb").read())
