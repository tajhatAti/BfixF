"""Website video feed: only free videos, Range streaming, hero + reel on the home page."""
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient      # noqa: E402

from app import config as cfg                  # noqa: E402
from app.services import feed, settings        # noqa: E402
from app.storage import db                     # noqa: E402

ADMIN_ID = 4242
DATA = bytes(range(256)) * 4096                # 1 MiB of known bytes


class _File:
    size = len(DATA)
    mime_type = "video/mp4"


class _Msg:
    media = object()
    file = _File()


class FakeBot:
    async def get_messages(self, chat, ids=None):
        return _Msg()

    async def iter_download(self, media, offset=0, request_size=0):
        for pos in range(offset, len(DATA), request_size):
            yield DATA[pos:pos + request_size]

    async def download_media(self, msg, file=None, thumb=None):
        return b"\xff\xd8jpeg"


def _video(store_name, premium):
    store = db.create_store(ADMIN_ID, store_name)
    db.update_store(store["id"], is_premium=1 if premium else 0)
    file_id = db.add_file(store_id=store["id"], name=f"{store_name} clip", kind="Video",
                          chat_id=-100, msg_id=len(store_name), size=len(DATA), duration=9)
    db.set_mirror(file_id, -100999, 7)
    return file_id


@pytest.fixture()
def site(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "feed.sqlite3"))
    settings._cache.clear()
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(cfg, "BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setattr(cfg, "API_ID", 12345)
    monkeypatch.setattr(cfg, "API_HASH", "0" * 32)
    monkeypatch.setattr(feed, "bot", FakeBot())
    feed._msg_cache.clear()
    feed._thumb_cache.clear()
    import web.dashboard as dashboard
    return TestClient(dashboard.app)


def test_parse_range_caps_each_answer():
    assert feed.parse_range("bytes=0-99", 1000) == (0, 99)
    assert feed.parse_range(None, 1000) == (0, 999)
    assert feed.parse_range("bytes=500-", 1000) == (500, 999)
    assert feed.parse_range("bytes=-100", 1000) == (900, 999)
    start, end = feed.parse_range("bytes=0-", 50 * 1024 * 1024)
    assert end - start + 1 == feed.MAX_RESPONSE
    with pytest.raises(ValueError):
        feed.parse_range("bytes=5000-", 1000)


@pytest.mark.asyncio
async def test_iter_range_returns_exactly_the_requested_bytes(monkeypatch):
    monkeypatch.setattr(feed, "bot", FakeBot())
    start, end = 700_000, 700_999                      # not aligned to the 512 KiB chunk
    got = b"".join([chunk async for chunk in feed.iter_range(_Msg(), start, end)])
    assert got == DATA[start:end + 1]


def test_only_free_videos_are_listed_and_streamable(site):
    free_id = _video("Free", premium=False)
    premium_id = _video("Paid", premium=True)

    assert [row["id"] for row in feed.latest()] == [free_id]
    ok = site.get(f"/media/{free_id}", headers={"Range": "bytes=10-19"})
    assert ok.status_code == 206 and ok.content == DATA[10:20]
    assert ok.headers["content-range"] == f"bytes 10-19/{len(DATA)}"
    assert site.get(f"/media/{premium_id}").status_code == 404
    assert site.get(f"/media/{premium_id}/thumb").status_code == 404
    assert site.get(f"/media/{free_id}/thumb").content.startswith(b"\xff\xd8")


def test_home_shows_the_reel_with_a_bot_link_and_can_be_switched_off(site, monkeypatch):
    free_id = _video("Free", premium=False)
    _video("Paid", premium=True)
    import web.dashboard as dashboard
    monkeypatch.setattr(dashboard, "_bot_username", lambda: "my_bot")

    page = site.get("/").text
    assert "<video" in page and f"media/{free_id}" in page
    assert f"https://t.me/my_bot?start=f{free_id}" in page
    assert "Paid clip" not in page

    settings.set("FEED_ENABLED", "0")
    assert "<video" not in site.get("/").text
