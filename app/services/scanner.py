"""Channel scanning: pull only videos out of a channel into a store.

Two modes:
* **session** — uses the admin's userbot (works for channels the bot cannot see)
* **bot admin** — the bot itself is an admin of the channel, so no session needed

Both modes report progress and can be stopped, and both skip duplicates instead
of re-adding the same message over and over.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from app.logger import log
from app.runtime import bot, user_clients
from app.storage import db
from app.services.telegram import safe_call

ProgressCb = Callable[[int, int], Awaitable[None]]

SCANS: dict[int, str] = {}          # admin_id -> "running" | "stopped"


def is_stopped(admin_id: int) -> bool:
    return SCANS.get(admin_id) == "stopped"


def stop_scan(admin_id: int) -> None:
    SCANS[admin_id] = "stopped"


def start_scan(admin_id: int) -> None:
    SCANS[admin_id] = "running"


def _kind_of(message) -> str | None:
    """Scanning is video-only; manual uploads keep their separate policy."""
    from app.services.media_guard import classify
    kind, _reason = classify(message)
    return kind if kind == "Video" else None


def _store_message(store_id: int, title: str, chat_id: int, message, index: int) -> bool:
    kind = _kind_of(message)
    if kind is None:
        return False
    from app.services.textclean import clean_text
    caption = clean_text((getattr(message, "message", "") or "").strip()).splitlines()
    name = (caption[0].strip()[:60] if caption else "") or f"{title[:20]} #{index}"
    file_id = db.add_file(
        store_id=store_id,
        name=name,
        kind=kind,
        chat_id=chat_id,
        msg_id=message.id,
        code=f"FILE_{abs(chat_id)}_{message.id}",
        size=getattr(getattr(message, "file", None), "size", None),
        duration=getattr(getattr(message, "video", None), "duration", None),
    )
    if file_id is not None:
        db.set_file_uid(file_id, f"{file_id}x{int(message.id) % 100000:05d}")
    return file_id is not None


async def _run(admin_id: int, iterator, title: str, chat_id: int, store_id: int,
               progress: ProgressCb | None, *, archive: bool = False) -> dict:
    scanned = found = 0
    start_scan(admin_id)
    try:
        async for message in iterator:
            if is_stopped(admin_id):
                log.info("Scan stopped by admin %s at %s messages", admin_id, scanned)
                break
            scanned += 1
            if _store_message(store_id, title, chat_id, message, found + 1):
                found += 1
            if scanned % 100 == 0 and progress:
                await progress(scanned, found)
    except Exception as exc:
        log.warning("Scan error for admin %s: %s", admin_id, exc)
        raise
    finally:
        SCANS.pop(admin_id, None)
        if archive:
            # Saved videos must not be orphaned if pagination/network fails.
            from app.services import video_copy
            video_copy.enqueue(store_id, admin_id, chat_id)
            video_copy.kick()
    if found:
        db.log_event("scan", admin_id, store_id, None, str(chat_id),
                     f"scanned={scanned} found={found}")
        # Copy every new video into the private cache channel ONCE, in the
        # background — later clicks are plain bot sends, never Saved Messages.
        try:
            from app.runtime import spawn
            if not archive:
                from app.services.media_cache import warm_all
                spawn(warm_all())
        except Exception as exc:
            log.debug("could not start the media cache warm-up: %s", exc)
    return {"copy_queued": archive, "scanned": scanned, "found": found, "title": title, "store_id": store_id}


async def scan_with_session(admin_id: int, chat_id: int, title: str, store_id: int,
                            progress: ProgressCb | None = None) -> dict:
    client = user_clients.get(admin_id)
    if client is None:
        raise RuntimeError("No userbot session connected")
    from telethon import utils
    from app.services.media_cache import _resolve_peer
    try:
        entity = await _resolve_peer(client, chat_id)
    except Exception as exc:
        log.warning("Scan source resolution failed: %s", exc)
        raise RuntimeError("এই সেশনের অ্যাকাউন্ট দিয়ে চ্যানেলটি খোলা যাচ্ছে না। "
                           "চ্যানেলে জয়েন করুন, তারপর সেশন দিয়ে আবার চ্যানেল বাছুন।") from exc
    chat_id = utils.get_peer_id(entity)
    from telethon.tl.types import InputMessagesFilterVideo
    iterator = client.iter_messages(entity, limit=None, reverse=True, filter=InputMessagesFilterVideo)
    return await _run(admin_id, iterator, title, chat_id, store_id, progress, archive=True)


async def scan_with_bot(admin_id: int, entity, title: str, store_id: int,
                        progress: ProgressCb | None = None) -> dict:
    from telethon import utils
    from telethon.tl.types import InputMessagesFilterVideo
    chat_id = utils.get_peer_id(entity)
    db.add_bot_chat(chat_id, title)
    iterator = bot.iter_messages(entity, limit=None, reverse=True, filter=InputMessagesFilterVideo)
    return await _run(admin_id, iterator, title, chat_id, store_id, progress, archive=True)


async def list_dialogs(admin_id: int) -> list[tuple[int, str]]:
    """Channels/groups the userbot can see (used by the scan picker)."""
    client = user_clients.get(admin_id)
    if client is None:
        return []
    dialogs: list[tuple[int, str]] = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            dialogs.append((dialog.id, dialog.name or "Unnamed"))
    return dialogs


async def resolve_bot_channel(admin_id: int, text: str, forwarded_from=None):
    """Figure out which channel the admin means for a bot-admin scan."""
    if forwarded_from is not None:
        try:
            return await forwarded_from.get_chat()
        except Exception:
            return None
    if not text:
        return None
    try:
        return await safe_call(bot.get_entity, text, what="get_entity", retries=1)
    except Exception as exc:
        log.debug("could not resolve channel %r: %s", text, exc)
        return None


async def bot_can_read(entity) -> tuple[bool, str]:
    try:
        await bot.get_messages(entity, limit=1)
        return True, ""
    except Exception as exc:
        return False, str(exc)[:200]
