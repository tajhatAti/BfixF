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
