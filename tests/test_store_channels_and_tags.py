"""Tag removal and one private cache channel per store."""
import pytest

from app.services import media_cache, media_guard, settings
from app.services.textclean import clean_text, strip_tags
from app.storage import db


def test_strip_tags_removes_mentions_and_links_only():
    assert strip_tags("Movie Night @cool_channel") == "Movie Night"
    assert "t.me" not in strip_tags("Join https://t.me/+AbCdEf123 now")
    assert strip_tags("@spam_channel\nReal title\nt.me/spam") == "Real title"
    assert strip_tags("mail me at a@b.com") == "mail me at a@b.com"       # not a Telegram tag
    assert strip_tags("Price #1 deal") == "Price #1 deal"                # hashtags stay
    assert strip_tags("") == ""


def test_setting_switches_cleaning_off(tmp_path):
    db.bind(str(tmp_path / "tags.sqlite3"))
    settings._cache.clear()
    assert clean_text("Show @someone") == "Show"
    settings.set("STRIP_TAGS", "0")
    assert clean_text("Show @someone") == "Show @someone"


def test_names_from_captions_are_clean(tmp_path):
    db.bind(str(tmp_path / "names.sqlite3"))
    settings._cache.clear()

    class Msg:
        message = "@their_channel\nSeason 1 Episode 4 - t.me/their_channel"

    assert media_guard.display_name(Msg(), "Video", 3) == "Season 1 Episode 4"

    class OnlyTag:
        message = "@their_channel"

    assert media_guard.display_name(OnlyTag(), "Video", 3) == "Video 3"


@pytest.mark.asyncio
async def test_each_store_gets_its_own_channel_once(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "channels.sqlite3"))
    settings._cache.clear()
    created = []

    async def fake_create(client, title, about, remember=False):
        created.append(title)
        return -1001000000000 - len(created)

    monkeypatch.setattr(media_cache, "_create_cache_channel", fake_create)
    one, two = {"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}

    first = await media_cache.store_cache_peer(object(), one)
    again = await media_cache.store_cache_peer(object(), one)
    other = await media_cache.store_cache_peer(object(), two)

    assert first == again and first != other
    assert len(created) == 2 and created[0].startswith("Alpha")

    settings.set("PER_STORE_CHANNEL", "0")
    assert await media_cache.store_cache_peer(object(), {"id": 3, "name": "Off"}) is None


# ------------------------------------------------- channel peers (the hash cache)
@pytest.mark.asyncio
async def test_a_restarted_session_opens_the_cache_channel_from_the_database(tmp_path, monkeypatch):
    """`Could not find the input entity for PeerChannel` must not need a dialog walk.

    The pair is in SQLite, so `_resolve_peer` answers with `InputPeerChannel`
    immediately — even for a client that has never seen the channel.
    """
    db.bind(str(tmp_path / "peers.sqlite3"))
    settings._cache.clear()
    from telethon.tl.types import InputPeerChannel

    from app.services import peers

    peers._chat_hashes.clear()
    db.set_chat_peer(-1001900000001, 1234567890123, "Store Media Cache")

    class FreshSession:
        def __init__(self):
            self.asked = []

        async def get_input_entity(self, peer):
            self.asked.append(peer)
            raise ValueError(f"Could not find the input entity for {peer}")

        async def get_dialogs(self, limit=None):
            raise ValueError("no dialogs either")

    client = FreshSession()
    monkeypatch.setattr(media_cache.runtime, "get_client", lambda: client)   # the bot
    resolved = await media_cache._resolve_peer(client, -1001900000001)

    assert isinstance(resolved, InputPeerChannel) and resolved.access_hash == 1234567890123
    assert client.asked == [], "the cached pair should be used before asking Telegram"


@pytest.mark.asyncio
async def test_dialogs_teach_the_database_about_channels(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "dialogs.sqlite3"))
    settings._cache.clear()
    from app.services import peers
    peers._chat_hashes.clear()
    media_cache._dialogs_at.clear()

    from telethon.tl.types import Channel

    entity = Channel(id=1900000005, title="Cache channel", photo=None, date=None,
                     creator=None, left=None, broadcast=True, verified=None,
                     megagroup=None, restricted=None, signatures=None,
                     username="cacheref", access_hash=42)

    class Dialog:
        pass

    Dialog.entity = entity

    class Client:
        async def get_dialogs(self, limit=None):
            assert limit is None, "must walk every dialog, not just the first 100"
            return [Dialog()]

    assert await media_cache._load_dialogs(Client(), force=True) is True
    # a userbot's hash is not the bot's hash — it must never reach the shared table
    assert db.chat_peer(-1001900000005) is None
