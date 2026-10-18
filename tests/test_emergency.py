"""Emergency group, maintenance mode and the Telegram backup."""
import asyncio
import contextlib

import pytest

from app import config as cfg
from app.services import backup, maintenance, settings
from app.storage import db

ADMIN_ID = 777


def test_emergency_settings_come_first_and_hold_the_critical_keys():
    first = settings.all_groups()[0]
    assert first.key == "emergency"
    keys = {item.key for item in first.settings}
    assert {"STORAGE_CHANNEL", "MAINTENANCE_MODE", "BACKUP_TO_TELEGRAM"} <= keys
    # each key lives in exactly one group
    all_keys = [item.key for g in settings.all_groups() for item in g.settings]
    assert all_keys.count("STORAGE_CHANNEL") == 1


def test_maintenance_blocks_users_but_never_admins(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "maint.sqlite3"))
    settings._cache.clear()
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    assert maintenance.blocks(5) is False
    settings.set("MAINTENANCE_MODE", "1")
    assert maintenance.blocks(5) is True and maintenance.blocks(ADMIN_ID) is False
    assert maintenance.message()


@pytest.mark.asyncio
async def test_backup_is_written_and_sent_to_every_admin(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "backup.sqlite3"))
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID, ADMIN_ID + 1])
    monkeypatch.setattr(cfg, "BACKUP_DIR", str(tmp_path / "backups"))
    sent = []

    class FakeBot:
        async def send_file(self, chat_id, path, **kwargs):
            sent.append((chat_id, path))

    monkeypatch.setattr(backup, "bot", FakeBot())
    result = await backup.send_backup("test")

    assert result["sent"] == 2 and {chat for chat, _p in sent} == {ADMIN_ID, ADMIN_ID + 1}
    assert all(path.endswith(".sqlite3") for _c, path in sent)


# ------------------------------------------------------------ /storageset (v3.2)
@pytest.mark.asyncio
async def test_storageset_shows_and_changes_the_cache_channel(tmp_path, monkeypatch):
    """The one-command cure for a stale channel hash (see media_cache._resolve_peer)."""
    db.bind(str(tmp_path / "storageset.sqlite3"))
    settings._cache.clear()
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    import app.handlers.guard as guard

    events_sent: list[str] = []

    class Event:
        sender_id = ADMIN_ID
        is_private, out, is_reply = True, False, False
        pattern_match = None

        def __init__(self, group):
            self.pattern_match = type("M", (), {"group": lambda _self, _i: group})()

        async def respond(self, text, buttons=None, **_kwargs):
            events_sent.append(text)
            return type("S", (), {"id": 1})()

    from telethon import events as tl_events

    async def call(group):
        # Telethon turns the handler's StopPropagation into “handled”, so the test does
        # the same rather than treating it as a failure.
        with contextlib.suppress(tl_events.StopPropagation):
            await guard.storageset_command(Event(group))

    await call(None)
    assert "ক্যাশ চ্যানেল" in events_sent[-1]

    await call("@mycache")
    assert settings.get_str("STORAGE_CHANNEL") == "@mycache"

    await call("auto")
    assert settings.get_str("STORAGE_CHANNEL") == ""
    assert "প্রতিটা স্টোরের" in events_sent[-1]
