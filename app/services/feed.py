"""Public video feed for the website's home page.

The newest videos of *free* stores play automatically in a vertical, scroll-snapping
reel; a tap opens the video in the bot. The bytes come from the bot's own cache copy
(`mirror_chat/mirror_msg`) and are streamed with HTTP Range support, so the browser can
seek without the server ever holding a whole file. Premium stores never appear here.
"""
from __future__ import annotations

import re
import time

from app.runtime import bot
from app.services import settings
from app.storage import db

CHUNK = 512 * 1024                    # Telegram download request size (multiple of 4096)
MAX_RESPONSE = 4 * 1024 * 1024        # one HTTP answer never carries more than this
_MSG_TTL = 600
_msg_cache: dict[int, tuple[float, object]] = {}
_thumb_cache: dict[int, tuple[float, bytes]] = {}
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def enabled() -> bool:
    return settings.get_bool("FEED_ENABLED", True)


def count() -> int:
    return max(1, min(6, settings.get_int("FEED_COUNT", 3)))


def latest() -> list[dict]:
    return db.recent_public_videos(count()) if enabled() else []


def eligible(row: dict | None) -> bool:
    """A video of a free store that already has a bot-side copy."""
    if not row or not enabled() or row.get("kind") != "Video":
        return False
    store = db.store(row["store_id"])
    return bool(store and not store.get("is_premium") and row.get("mirror_chat") and row.get("mirror_msg"))


async def message_for(row: dict):
    hit = _msg_cache.get(int(row["id"]))
    if hit and hit[0] > time.time():
        return hit[1]
    msg = await bot.get_messages(int(row["mirror_chat"]), ids=int(row["mirror_msg"]))
    if msg is None or not getattr(msg, "media", None):
        return None
    if len(_msg_cache) > 60:
        _msg_cache.clear()
    _msg_cache[int(row["id"])] = (time.time() + _MSG_TTL, msg)
    return msg


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
    async for chunk in bot.iter_download(msg.media, offset=aligned, request_size=CHUNK):
        if skip:
            chunk, skip = chunk[skip:], 0
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        if chunk:
            remaining -= len(chunk)
            yield chunk
        if remaining <= 0:
            break


async def thumb_for(row: dict) -> bytes | None:
    hit = _thumb_cache.get(int(row["id"]))
    if hit and hit[0] > time.time():
        return hit[1]
    msg = await message_for(row)
    if msg is None:
        return None
    data = await bot.download_media(msg, file=bytes, thumb=-1)
    if not data:
        return None
    if len(_thumb_cache) > 60:
        _thumb_cache.clear()
    _thumb_cache[int(row["id"])] = (time.time() + _MSG_TTL, bytes(data))
    return bytes(data)
