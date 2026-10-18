"""Public media feed — the auto-playing reel and gallery of the website.

What a visitor sees must work on **any** install, not only on a perfectly warmed-up
one, so this service is deliberately forgiving:

* **Source resolution** — a file is streamed from its bot-side mirror when there is
  one, and straight from the original chat/message otherwise. The old rule
  (“only files with a mirror”) left brand-new installs with an empty home page.
* **Videos *and* photos** — a photo is a perfect reel slide (no bandwidth, instant
  autoplay), and plenty of stores are mostly photos.
* **Thumbnails** — Telegram's own preview (`thumb=-1`, then `-2`, then the largest
  in `document.thumbs`), cached in memory and on disk so a hot reel costs nothing.
  When Telegram has no preview at all the site asks for `/thumb` and gets a
  generated placeholder instead of a broken image.
* **Pins** — the owner can hand-pick items (`/reel add` in the bot, or the website
  panel); pinned media is always shown first, in the chosen order.
* **Range streaming** — the browser seeks without the server ever buffering a whole
  file, one HTTP answer is capped at `MAX_RESPONSE` bytes.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from app.runtime import bot
from app.services import media_cache, settings
from app.storage import db

CHUNK = 512 * 1024                    # Telegram download request size (multiple of 4096)
MAX_RESPONSE = 4 * 1024 * 1024        # one HTTP answer never carries more than this
_MSG_TTL = 600
_CACHE_DIR = Path(os.getenv("FEED_CACHE_DIR", ".cache/thumbs"))
_msg_cache: dict[str, tuple[float, object]] = {}
_thumb_cache: dict[str, tuple[float, bytes]] = {}
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")

#: photos/videos a visitor may watch
WATCHABLE = ("Video", "Photo")


def enabled() -> bool:
    return settings.get_bool("FEED_ENABLED", True)


def count() -> int:
    """How many items the reel shows (1–8, default 4)."""
    return max(1, min(8, settings.get_int("FEED_COUNT", 4)))


def gallery_count() -> int:
    return max(4, min(120, settings.get_int("FEED_GALLERY_COUNT", 36)))


def wall_count() -> int:
    """How many clips autoplay *side by side* on the simple public page (0 = off)."""
    return max(0, min(8, settings.get_int("FEED_WALL", 4)))


def autoplay() -> bool:
    """Autoplay at all (always muted — see `muted`)."""
    return settings.get_bool("FEED_AUTOPLAY", True)


def muted() -> bool:
    """Start muted. Browsers block unmuted autoplay, so this defaults to True."""
    return settings.get_bool("FEED_MUTED", True)


def loop() -> bool:
    return settings.get_bool("FEED_LOOP", True)


def playback() -> dict:
    """Everything the templates and the JS need to know about playback."""
    return {"autoplay": autoplay(), "muted": muted(), "loop": loop(),
            "wall": wall_count(), "count": count()}


# ------------------------------------------------------------------ item model
def _key(item: dict) -> str:
    if item.get("pin_id"):
        return f"pin:{int(item['pin_id'])}"
    return f"file:{int(item.get('id') or 0)}"


def stream_path(item: dict) -> str:
    """Relative URL the browser streams from."""
    return f"reel/{int(item['pin_id'])}" if item.get("pin_id") else f"media/{int(item['id'])}"


def thumb_path(item: dict) -> str:
    return (f"reel/{int(item['pin_id'])}/thumb" if item.get("pin_id")
            else f"media/{int(item['id'])}/thumb")


def _kind_of(item: dict) -> str:
    kind = (item.get("kind") or "Video")
    return "photo" if str(kind).lower().startswith("photo") else "video"


def _sources(item: dict) -> list[tuple[int, int]]:
    """Where the media can be read from, most preferred first."""
    out: list[tuple[int, int]] = []
    for chat, msg in ((item.get("mirror_chat"), item.get("mirror_msg")),
                      (item.get("chat_id"), item.get("msg_id"))):
        try:
            if chat and msg:
                pair = (int(chat), int(msg))
                if pair not in out:
                    out.append(pair)
        except (TypeError, ValueError):
            continue
    return out


# ----------------------------------------------------------- demo / preview mode
# A file row with `chat_id = 0` is not a Telegram file at all: it points at a clip
# inside `FEED_DEMO_DIR`, which is how `tools/seed_demo.py` lets an owner see the
# reel and the wall working *before* connecting a bot session. Production rows never
# look like this (the bot always stores the real chat), so the branch stays dormant.
def demo_dir() -> Path:
    return Path(os.getenv("FEED_DEMO_DIR", "demo_media"))


def is_demo(item: dict) -> bool:
    return int(item.get("chat_id") or 0) == 0 and int(item.get("msg_id") or 0) > 0


def demo_path(item: dict, suffix: str = ".mp4") -> Path | None:
    if not is_demo(item):
        return None
    return demo_dir() / f"{int(item['msg_id'])}{suffix}"


def cached_size(item: dict) -> int:
    """Bytes of this clip already on disk (`media_cache`) — 0 when there are none.

    A clip whose bytes are here plays instantly: no Telegram, no mirror, no wait.
    """
    if not item or is_demo(item):
        return 0
    try:
        return media_cache.local_size(item)
    except Exception:
        return 0


def cached_thumb(item: dict) -> bytes | None:
    try:
        return media_cache.local_thumb(item)
    except Exception:
        return None


def iter_cached(item: dict, start: int, end: int):
    """Range-stream a cached clip — the site works even with Telegram unreachable."""
    return media_cache.iter_bytes(item, start, end)


async def prepare(item: dict, store: dict | None = None) -> int:
    """Fetch this clip from Telegram *now* and cache it. Returns the size or 0.

    The website calls this when a visitor presses “চালান” on something the cache
    does not have yet, so a play button never ends in “লোড হলো না”.
    """
    if not item:
        return 0
    size = cached_size(item)
    if size:
        return size
    payload = await media_cache.fetch_now(item, store)
    return len(payload or b"")


def _cached_locally(item: dict) -> bool:
    return cached_size(item) > 0


def _decorate(item: dict, bots: str = "", index: int = 0) -> dict:
    """Add the fields the templates and the JSON API need."""
    row = dict(item)
    row["pin_id"] = int(row.get("pin_id") or 0)
    row["key"] = _key(row)
    row["media_kind"] = _kind_of(row)
    row["stream"] = stream_path(row)
    row["thumb"] = thumb_path(row)
    row["store_name"] = row.get("store_name") or ""
    file_id = int(row.get("id") or 0)
    if bots:
        payload = f"s{row['store_id']}" if row.get("store_id") else ""
        row["bot_link"] = (f"https://t.me/{bots}?start=f{file_id}" if file_id
                           else f"https://t.me/{bots}")
    else:
        row["bot_link"] = ""
    row["index"] = index
    return row


# ------------------------------------------------------------------- listings
def pins(limit: int = 12) -> list[dict]:
    rows = []
    for pin in db.feed_pins(limit=limit):
        rows.append({"pin_id": pin["id"], "chat_id": pin["chat_id"], "msg_id": pin["msg_id"],
                     "kind": pin["kind"], "name": pin.get("title") or "", "views": 0,
                     "store_name": "", "store_id": 0, "views_count": 0})
    return rows


def latest(bots: str = "", limit: int | None = None) -> list[dict]:
    """The reel: pinned media first, then the newest free videos/photos."""
    if not enabled():
        return []
    limit = limit or count()
    items: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for pin in pins(limit=limit):
        pair = (int(pin["chat_id"]), int(pin["msg_id"]))
        if pair in seen:
            continue
        seen.add(pair)
        items.append(pin)
        if len(items) >= limit:
            break
    if len(items) < limit:
        for row in db.recent_public_media(limit=limit * 3):
            pair = (int(row.get("chat_id") or 0), int(row.get("msg_id") or 0))
            if pair in seen:
                continue
            seen.add(pair)
            items.append(row)
            if len(items) >= limit:
                break
    return [_decorate(item, bots, index) for index, item in enumerate(items)]


def gallery(bots: str = "", limit: int | None = None) -> list[dict]:
    """Everything a visitor may browse (free stores, videos + photos + audio)."""
    limit = limit or gallery_count()
    items: list[dict] = pins(limit=min(limit, 12))
    have = {(int(i["chat_id"]), int(i["msg_id"])) for i in items}
    for row in db.public_gallery(limit=limit * 2):
        pair = (int(row.get("chat_id") or 0), int(row.get("msg_id") or 0))
        if pair in have:
            continue
        have.add(pair)
        items.append(row)
        if len(items) >= limit:
            break
    return [_decorate(item, bots, index) for index, item in enumerate(items)]


def playable_rank(item: dict) -> int:
    """0 = plays without Telegram, 1 = needs the bot/user session.

    The wall is the feature people look at first, so a clip that is *guaranteed* to
    play must win a seat over one that may 404 when no session is connected: local
    demo clips and already-mirrored (cached) files come first.
    """
    if is_demo(item):
        path = demo_path(item)
        return 0 if path and path.is_file() else 1
    if _cached_locally(item):
        return 0                                   # bytes are here — instant play
    return 0 if item.get("mirror_msg") else 1


def wall(bots: str = "", limit: int | None = None) -> list[dict]:
    """The items of the “several clips at once” wall.

    Deliberately the same items as the reel, just as many as FEED_WALL asks for —
    the visitor scrolls into a row of clips that are already playing instead of a
    black rectangle that never loads. Among equally new items the ones that can
    play right now come first (see `playable_rank`); the order inside each group
    stays newest-first.
    """
    limit = wall_count() if limit is None else max(0, limit)
    if not limit or not enabled():
        return []
    pool = latest(bots, limit=max(limit * 3, limit))
    pool.sort(key=playable_rank)                 # stable → newest first in each group
    return pool[:limit]


def stats() -> dict:
    plays = db.feed_play_counts()
    return {"enabled": enabled(), "count": count(), "wall": wall_count(),
            "autoplay": autoplay(), "muted": muted(),
            "shown": len(latest()), "pins": len(db.feed_pins(limit=200)),
            "plays": sum(plays.values()), "top": sorted(plays.items(),
                                                        key=lambda kv: -kv[1])[:5]}


def note_play(key: str) -> None:
    try:
        db.bump_feed_play(key)
    except Exception:
        pass


# -------------------------------------------------------------------- reading
def eligible(row: dict | None) -> bool:
    """A video/photo a visitor may stream: free store, not locked, has a source."""
    if not row or row.get('locked') or not enabled():
        return False
    if str(row.get("kind") or "") not in WATCHABLE:
        return False
    store_id = row.get("store_id")
    if store_id:
        store = db.store(int(store_id))
        if not store or store.get("is_premium"):
            return False
    return bool(_sources(row)) or is_demo(row)


def eligible_pin(pin_id: int) -> dict | None:
    """A pinned item, when it still exists and the reel is on."""
    if not enabled():
        return None
    for pin in db.feed_pins(limit=200):
        if int(pin["id"]) == int(pin_id):
            return {"pin_id": pin["id"], "chat_id": pin["chat_id"], "msg_id": pin["msg_id"],
                    "kind": pin["kind"], "name": pin.get("title") or ""}
    return None


def _connected(client) -> bool:
    """Telethon's `is_connected()` — tolerant of test stubs that expose a bool."""
    check = getattr(client, "is_connected", None)
    if check is None:
        return True                                   # unknown stub: give it a try
    try:
        return bool(check() if callable(check) else check)
    except Exception:
        return True


def clients() -> list:
    """Every client that may read a public item for us — bot first, then the owners'.

    A public page must not go black just because the bot session is restarting: the
    owner's userbot (the one that scanned the channels) can read exactly the same
    free media. Only free, unlocked videos/photos ever reach this point (`eligible`).
    """
    from app import runtime
    out: list = []
    # The module-level `bot` (a lazy proxy in production, a stub in tests).
    for candidate in (bot, getattr(runtime, "get_client", lambda: None)()):
        if candidate is not None and candidate not in out and _connected(candidate):
            out.append(candidate)
    for client in list((getattr(runtime, "user_clients", None) or {}).values()):
        if client is not None and client not in out and _connected(client):
            out.append(client)
    return out


def demo_frame(item: dict) -> bytes | None:
    """The poster frame `seed_demo` extracted next to a demo clip, if it exists."""
    path = demo_path(item, ".jpg")
    if not path or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def local_size(item: dict) -> int:
    path = demo_path(item)
    if not path or not path.is_file():
        return 0
    return path.stat().st_size


async def iter_local(item: dict, start: int, end: int):
    """The demo-clip twin of `iter_range` — plain file reads, same chunk size."""
    path = demo_path(item)
    if not path or not path.is_file():
        return
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = handle.read(min(CHUNK, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


async def message_for(row: dict):
    """The live Telegram message to stream from (mirror first, then the original).

    Tries the bot, then any connected user client — a message fetched with a client
    carries that client, so `iter_range` keeps working with either.
    """
    if is_demo(row):
        return None                     # local clip — see `iter_local`
    key = _key(row)
    hit = _msg_cache.get(key)
    if hit and hit[0] > time.time():
        source_client = getattr(hit[1], '_client', None)
        if source_client is None or _connected(source_client):
            return hit[1]
    for client in clients():
        for chat, msg_id in _sources(row):
            try:
                message = await client.get_messages(chat, ids=msg_id)
            except Exception:
                continue
            if message is not None and getattr(message, "media", None):
                if len(_msg_cache) > 120:
                    _msg_cache.clear()
                _msg_cache[key] = (time.time() + _MSG_TTL, message)
                return message
    return None


def parse_range(header: str | None, size: int) -> tuple[int, int]:
    """Inclusive (start, end) to serve. No header = from the start. Raises ValueError
    when the range cannot be satisfied. One answer is capped at MAX_RESPONSE bytes."""
    start, end = 0, size - 1
    match = _RANGE.match((header or "").strip())
    if match and (match.group(1) or match.group(2)):
        first, last = match.groups()
        if first == "":
            start = max(0, size - int(last))
        else:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise ValueError("range not satisfiable")
    return start, min(end, start + MAX_RESPONSE - 1)


async def iter_range(msg, start: int, end: int):
    """Yield bytes start..end (inclusive) of the message's media."""
    aligned = (start // CHUNK) * CHUNK
    skip = start - aligned
    remaining = end - start + 1
    client = getattr(msg, '_client', None) or bot
    async for chunk in client.iter_download(msg.media, offset=aligned, request_size=CHUNK):
        if skip:
            chunk, skip = chunk[skip:], 0
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        if chunk:
            remaining -= len(chunk)
            yield chunk
        if remaining <= 0:
            break


def media_size(msg) -> int:
    return int(getattr(getattr(msg, "file", None), "size", 0) or 0)


def media_mime(msg) -> str:
    return getattr(getattr(msg, "file", None), "mime_type", None) or "video/mp4"


# ---------------------------------------------------------------- thumbnails
def _cache_key(key: str) -> str:
    """Version the cache key with the database, so two installs never share thumbs."""
    return f"{getattr(db, 'path', '')}|{key}"


def _disk_path(key: str) -> Path:
    digest = hashlib.sha1(_cache_key(key).encode()).hexdigest()[:16]
    return _CACHE_DIR / f"{digest}.jpg"


def _read_disk(key: str) -> bytes | None:
    path = _disk_path(key)
    try:
        if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
            data = path.read_bytes()
            return data or None
    except OSError:
        return None
    return None


def _write_disk(key: str, data: bytes) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _disk_path(key).write_bytes(data)
    except OSError:
        pass


async def thumb_for(row: dict) -> bytes | None:
    """A JPEG preview of the media (Telegram's own; memory + disk cached)."""
    key = _key(row)
    hit = _thumb_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    cached = _read_disk(key)
    if cached:
        _thumb_cache[key] = (time.time() + _MSG_TTL, cached)
        return cached
    message = await message_for(row)
    if message is None:
        return None
    data = None
    for thumb in (-1, -2, 0):
        try:
            client = getattr(message, '_client', None) or bot
            data = await client.download_media(message, file=bytes, thumb=thumb)
        except Exception:
            data = None
        if data:
            break
    if not data:
        return None
    data = bytes(data)
    if len(_thumb_cache) > 120:
        _thumb_cache.clear()
    _thumb_cache[key] = (time.time() + _MSG_TTL, data)
    _write_disk(key, data)
    return data


def placeholder_svg(name: str = "", kind: str = "video", accent: str = "#c9a45c") -> str:
    """A generated poster for media Telegram has no preview for.

    Never a broken image and never a grey box: the poster carries the item's name,
    a gradient and a play/wall badge, so the reel still looks intentional.
    Returns *markup* — ready to be served as `image/svg+xml` (see the plate route).
    """
    label = (name or "মিডিয়া").strip()[:38]
    label = (label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    badge = "🖼" if kind == "photo" else "▶"
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 720 1280'>"
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0' stop-color='%230f1013'/><stop offset='.55' stop-color='%231a1720'/>"
        "<stop offset='1' stop-color='%23241d12'/></linearGradient></defs>"
        "<rect width='720' height='1280' fill='url(%23g)'/>"
        "<circle cx='360' cy='560' r='96' fill='none' stroke='%23c9a45c' stroke-width='6' opacity='.85'/>"
        f"<text x='360' y='600' font-family='sans-serif' font-size='84' fill='{accent}'"
        f" text-anchor='middle'>{badge}</text>"
        f"<text x='360' y='790' font-family='sans-serif' font-size='44' fill='%23e8e3d6'"
        f" text-anchor='middle'>{label}</text>"
        "<text x='360' y='1210' font-family='sans-serif' font-size='30' fill='%23c9a45c'"
        " text-anchor='middle' opacity='.75'>Desi Site</text></svg>")


def placeholder_data_url(name: str = "", kind: str = "video") -> str:
    """The same poster as a `data:` URL for a `poster=` / `src=` attribute."""
    markup = placeholder_svg(name, kind).replace("#", "%23")
    return "data:image/svg+xml;utf8," + markup


#: Kept for older callers: the default poster used when nothing else is available.
PLACEHOLDER = placeholder_data_url()
