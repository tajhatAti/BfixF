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

    async def fake_create(client, title, about):
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
