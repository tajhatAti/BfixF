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


# --------------------------------------------------------------- reel pins
def test_pinned_media_leads_the_reel_and_streams(site):
    free_id = _video("Free", premium=False)
    pin_id = db.add_feed_pin(-100, 42, "video", "Owner picked this")
    pins = feed.latest("my_bot")
    assert pins[0]["pin_id"] == pin_id and pins[0]["name"] == "Owner picked this"
    assert any(item.get("id") == free_id for item in pins)

    stream = site.get(f"/reel/{pin_id}", headers={"Range": "bytes=0-9"})
    assert stream.status_code == 206 and stream.content == DATA[:10]
    assert site.get(f"/reel/{pin_id}/thumb").content.startswith(b"\xff\xd8")
    db.delete_feed_pin(pin_id)


def test_a_photo_is_a_valid_reel_item(site):
    store = db.create_store(ADMIN_ID, "Photos")
    db.update_store(store["id"], is_premium=0)
    file_id = db.add_file(store_id=store["id"], name="Cover art", kind="Photo",
                          chat_id=-100, msg_id=9, size=4096)
    kinds = {item.get("media_kind") for item in feed.latest()}
    assert "photo" in kinds
    assert feed.eligible(db.file(file_id))


def test_media_without_a_mirror_is_still_shown(site):
    """A brand-new install has no media cache yet — the reel must not be empty."""
    store = db.create_store(ADMIN_ID, "Fresh")
    db.update_store(store["id"], is_premium=0)
    db.add_file(store_id=store["id"], name="Just added", kind="Video",
                chat_id=-100, msg_id=3, size=len(DATA))
    assert feed.latest()                     # no mirror_msg set at all


def test_the_play_beacon_is_recorded(site):
    site.get("/reel/play/file%3A7")
    assert db.feed_play_counts().get("file:7") == 1


def test_watch_page_plays_every_free_media(site, monkeypatch):
    import web.dashboard as dashboard
    _video("Free", premium=False)
    monkeypatch.setattr(dashboard, "_bot_username", lambda: "my_bot")
    page = site.get("/watch").text
    assert "watch-hero" in page and "<video" in page
    assert "media/" in page


def test_a_generated_poster_replaces_a_missing_thumbnail(site, monkeypatch):
    file_id = _video("Free", premium=False)

    async def no_thumb(message, file=None, thumb=None):
        return None

    monkeypatch.setattr(feed.bot, "download_media", no_thumb, raising=False)
    response = site.get(f"/media/{file_id}/thumb")
    assert response.status_code == 200 and "svg" in response.headers["content-type"]


def test_store_cover_is_served_as_an_image(site):
    store = db.create_store(ADMIN_ID, "Covered")
    db.update_store(store["id"], is_premium=0, cover="-100:5")
    assert site.get(f"/cover/{store['id']}").content.startswith(b"\xff\xd8")

    db.update_store(store["id"], cover="")
    assert site.get(f"/cover/{store['id']}").status_code == 404


# ------------------------------------------------- the “several at once” wall
def test_wall_plays_several_clips_together(site):
    for index in range(5):
        store = db.create_store(ADMIN_ID, f"Wall {index}")
        db.update_store(store["id"], is_premium=0)
        db.add_file(store_id=store["id"], name=f"Clip {index}", kind="Video",
                    chat_id=-100, msg_id=100 + index, size=len(DATA), duration=9)
    page = site.get("/").text
    assert page.count('class="cell"') == 4, "FEED_WALL should autoplay 4 clips at once"
    assert 'data-autoplay="1"' in page and 'data-muted="1"' in page
    assert page.count("<video") >= 4
    assert feed.wall_count() == 4


def test_the_wall_can_be_turned_off(site):
    _video("Free", premium=False)
    settings.set("FEED_WALL", "0")
    assert 'class="cell"' not in site.get("/").text


def test_a_missing_preview_is_real_svg_not_a_data_url(site):
    """The plate route must serve SVG markup — a `data:` URL as the body is not an image."""
    file_id = _video("Free", premium=False)

    async def no_thumb(message, file=None, thumb=None):
        return None

    import web.dashboard as dashboard
    monkey = pytest.MonkeyPatch()
    monkey.setattr(feed.bot, "download_media", no_thumb, raising=False)
    try:
        body = site.get(f"/media/{file_id}/thumb").content.decode("utf-8")
    finally:
        monkey.undo()
    assert body.lstrip().startswith("<svg") and body.rstrip().endswith("</svg>")
    assert not body.startswith("data:") and dashboard is not None


# ------------------------------------------------------- seeded demo clips
def test_a_seeded_clip_streams_from_disk(site, tmp_path, monkeypatch):
    """A row with `chat_id = 0` points at `FEED_DEMO_DIR` — the preview install."""
    import os
    monkeypatch.setenv("FEED_DEMO_DIR", str(tmp_path / "clips"))
    (tmp_path / "clips").mkdir()
    (tmp_path / "clips" / "900.mp4").write_bytes(DATA)
    (tmp_path / "clips" / "900.jpg").write_bytes(b"\xff\xd8demo-frame")
    store = db.create_store(ADMIN_ID, "Preview")
    db.update_store(store["id"], is_premium=0)
    file_id = db.add_file(store["id"], "Demo clip", "Video", 0, msg_id=900, size=len(DATA))

    row = db.file(file_id)
    assert feed.eligible(row) and feed.is_demo(row) and os is not None

    stream = site.get(f"/media/{file_id}", headers={"Range": "bytes=0-99"})
    assert stream.status_code == 206 and stream.content == DATA[:100]
    assert stream.headers["content-type"].startswith("video/mp4")
    assert site.get(f"/media/{file_id}/thumb").content == b"\xff\xd8demo-frame"


def test_a_demo_row_without_its_clip_is_404_not_a_crash(site, tmp_path, monkeypatch):
    monkeypatch.setenv("FEED_DEMO_DIR", str(tmp_path / "missing"))
    store = db.create_store(ADMIN_ID, "Preview 2")
    db.update_store(store["id"], is_premium=0)
    file_id = db.add_file(store["id"], "Gone clip", "Video", 0, msg_id=902, size=10)
    assert site.get(f"/media/{file_id}").status_code == 404


def test_the_wall_prefers_clips_that_can_play_right_now(tmp_path, monkeypatch):
    """A cell that 404s is exactly the “autoplay doesn't work” complaint."""
    from app.services import feed, settings
    from app.storage import db
    db.bind(str(tmp_path / "wall.sqlite3"))
    settings._cache.clear()
    store = db.create_store(1, "রিল")                 # free store
    db.update_store(store["id"], is_premium=0)
    db.add_file(store["id"], "পুরোনো (বটের)", "Video", -100, 90, size=1000)
    db.add_file(store["id"], "নতুন (বটের)", "Video", -100, 91, size=1000)
    demo = db.add_file(store["id"], "ডেমো ক্লিপ", "Video", 0, 7, size=1000)
    media = tmp_path / "demo"
    media.mkdir()
    (media / "7.mp4").write_bytes(b"\x00" * 32)
    monkeypatch.setenv("FEED_DEMO_DIR", str(media))
    settings.set("FEED_WALL", "2")

    items = feed.wall(limit=2)
    keys = [item["key"] for item in items]
    assert f"file:{demo}" in keys                    # the playable one made the wall
    assert feed.playable_rank({"chat_id": 0, "msg_id": 7}) == 0
    assert feed.playable_rank({"chat_id": -100, "msg_id": 5}) == 1
    # a demo row whose clip is missing is not “playable” either
    assert feed.playable_rank({"chat_id": 0, "msg_id": 999}) == 1


def test_all_player_urls_keep_the_live_proxy_prefix(site):
    import re
    from urllib.parse import urljoin
    for name in ('Clip One', 'Clip Two', 'Clip Three', 'Clip Four'):
        _video(name, premium=False)
    for path in ('/', '/watch'):
        page = site.get(path, headers={'X-Forwarded-Prefix': '/live/u3-esi-38a23a'}).text
        assert '<base href="/live/u3-esi-38a23a/">' in page
        streams = re.findall(r'(?:data-stream|data-src)="([^"]+)"', page)
        assert streams
        for stream in streams:
            assert not stream.startswith('/media/')
            assert '/live/u3-esi-38a23a/' in urljoin('https://host/live/u3-esi-38a23a/', stream)
