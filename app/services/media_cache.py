"""Media cache — make every file reachable by the *bot* exactly once.

The old behaviour
-----------------
When the bot could not read a source message it asked the store owner's userbot
to forward the file into the bot's DM and **deleted the copy** right after the
send. With a few users clicking at once the userbot forwarded + deleted the same
video over and over → “বার বার বোর্ডে ফরওয়ার্ড হয়ে ডিলিট হচ্ছিল” → FloodWait, and
some users received nothing.

The new behaviour
-----------------
A file is pulled into a bot-readable place **once** and remembered
(`files.mirror_chat/mirror_msg`). After that every delivery — store click, search,
limited link, broadcast to 5,000 people — is a plain `send_file` by the bot: no
session traffic, no forward, no delete, no FloodWait.

Mirror target (never anyone's Saved Messages / private chat any more):
    1. the storage channel configured in the panel (`STORAGE_CHANNEL`)
    2. otherwise a private channel the owner's userbot creates **once, by itself**
       (the bot is added as admin) and remembers in the database.

Techniques borrowed from the owner's own forwarder (`website.example.py`):
   * ranged multi-connection download for big files
   * re-upload with `supports_streaming=True` + thumbnail (restricted sources)
   * FloodWait → wait, then retry on another session
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from telethon.errors import FloodWaitError

from app import runtime
from app.logger import log
from app.services import settings
from app.services.telegram import safe_call
from app.services.textclean import clean_text
from app.storage import db

CHUNK = 1024 * 1024
MAX_RANGED_THREADS = 4


def _owner_session(file_row: dict, store: dict | None):
    """The userbot session that should do the heavy lifting for this file."""
    from app.runtime import user_clients
    if store and store.get("admin_id") in user_clients:
        return user_clients[store["admin_id"]]
    for _admin_id, client in list(user_clients.items()):
        return client
    return None


_problems: dict[int, str] = {}                  # file id -> why the last mirror attempt failed
_fetch_errors: dict[tuple, str] = {}            # (chat, msg) -> why the userbot could not read it
_last_heal = 0.0


def mirror_problem(file_id: int) -> str:
    """Plain-language reason the last attempt to copy this file failed ('' = none)."""
    return _problems.get(int(file_id), "")


def _note(file_id: int, text: str) -> None:
    _problems[int(file_id)] = text
    log.warning("mirror of file %s failed: %s", file_id, text)


async def live_session(file_row: dict | None = None, store: dict | None = None):
    """The owner's userbot client — reconnected if it dropped. None when unusable."""
    client = _owner_session(file_row or {}, store)
    if client is None:
        return None
    try:
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            return None
    except Exception as exc:
        log.warning("userbot session cannot connect: %s", exc)
        return None
    return client


async def heal_storage_access() -> bool:
    """The bot says it cannot open the cache channel — put it right, once a minute.

    Re-adds the bot as admin (harmless when it already is) and makes the userbot
    post one silent message: Telegram then tells the bot the channel exists.
    """
    import time
    global _last_heal
    if time.time() - _last_heal < 45:
        return False
    _last_heal = time.time()
    client = await live_session()
    if client is None:
        return False
    target = await storage_peer(client)
    if target is None:
        return False
    try:
        peer = await _resolve_peer(client, target)
        if runtime.bot_username:
            try:
                await client.edit_admin(peer, runtime.bot_username, is_admin=True,
                                        post_messages=True, edit_messages=True,
                                        delete_messages=True, title="cache")
            except Exception as exc:
                log.debug("could not (re)promote the bot: %s", exc)
        sent = await client.send_message(peer, "·", silent=True)
        await asyncio.sleep(2)
        await client.delete_messages(peer, [sent.id])
        log.info("storage channel access refreshed for the bot")
        return True
    except Exception as exc:
        log.warning("could not refresh the storage channel access: %s", exc)
        return False


META_STORAGE = "auto_storage_channel"
_channel_lock = asyncio.Lock()
_file_locks: dict[int, asyncio.Lock] = {}
_warm_lock = asyncio.Lock()
_failed_recent: dict[int, float] = {}          # file id -> time of last failed mirror


def _as_peer(value):
    """`-100123…` stays an int, `@name` stays a string."""
    value = (str(value) if value is not None else "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def configured_storage():
    """The storage channel from the panel, else the auto-created one, else None."""
    return _as_peer(settings.get_str("STORAGE_CHANNEL")) or _as_peer(db.get_meta(META_STORAGE, ""))


_dialogs_at: dict[int, float] = {}             # id(client) -> when its dialogs were loaded


def _is_entity_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "input entity" in text or "peer id invalid" in text or "cannot find any entity" in text


async def _load_dialogs(client, force: bool = False) -> bool:
    """A fresh StringSession knows no channels at all ("Could not find the input
    entity…"). Reading the dialog list teaches it every chat the account is in."""
    import time
    key = id(client)
    if not force and time.time() - _dialogs_at.get(key, 0) < 300:
        return False
    try:
        if client is runtime.get_client():
            return False                       # bots cannot list dialogs
    except Exception:
        pass
    _dialogs_at[key] = time.time()
    try:
        await client.get_dialogs()
        log.info("userbot dialogs loaded (entity cache refreshed)")
        return True
    except Exception as exc:
        log.debug("could not load dialogs: %s", exc)
        return False


async def _resolve_peer(client, peer):
    """Make sure `client` knows the channel (a fresh StringSession has no cache)."""
    try:
        return await client.get_input_entity(peer)
    except Exception:
        await _load_dialogs(client, force=True)
        return await client.get_input_entity(peer)


async def _create_cache_channel(client, title: str, about: str):
    """Create a private channel with the owner's session and put the bot in it as admin."""
    from telethon import functions, utils
    try:
        result = await client(functions.channels.CreateChannelRequest(
            title=title[:120], about=about[:200], broadcast=True, megagroup=False))
        channel = result.chats[0]
        peer_id = utils.get_peer_id(channel)
        if runtime.bot_username:
            await client.edit_admin(channel, runtime.bot_username, is_admin=True,
                                    post_messages=True, edit_messages=True,
                                    delete_messages=True, title="cache")
        # The bot learns about the channel from this first post.
        await client.send_message(channel, "Media cache ready")
        await asyncio.sleep(2)
        log.info("Created the private cache channel %s (%s)", peer_id, title)
        return peer_id
    except Exception as exc:
        log.error("Could not create the cache channel %r: %s - set STORAGE_CHANNEL "
                  "by hand (a private channel where the bot is admin).", title, exc)
        return None


async def _create_storage_channel(client):
    """Create the shared private cache channel once and put the bot in it as admin."""
    peer_id = await _create_cache_channel(
        client, "Store Media Cache",
        "Auto-created by the store bot. Every video lives here once. Do not delete.")
    if peer_id:
        db.set_meta(META_STORAGE, str(peer_id))
    return peer_id


def _store_meta_key(store_id) -> str:
    return f"store_cache_channel:{int(store_id)}"


async def store_cache_peer(client, store: dict | None):
    """One private cache channel per store, created once by the owner's session.

    The videos of a store are copied there (no caption, so no foreign tags) and every
    user delivery is a plain bot send from that copy. Off when the panel's
    STORAGE_CHANNEL is set by hand or the PER_STORE_CHANNEL setting is switched off.
    """
    if not store or settings.get_str("STORAGE_CHANNEL"):
        return None
    if not settings.get_bool("PER_STORE_CHANNEL", True):
        return None
    key = _store_meta_key(store["id"])
    known = _as_peer(db.get_meta(key, ""))
    if known is not None:
        return known
    async with _channel_lock:
        known = _as_peer(db.get_meta(key, ""))
        if known is not None:
            return known
        peer_id = await _create_cache_channel(
            client, f"{str(store.get('name') or 'Store')[:80]} - media",
            f"Auto-created for the store '{store.get('name')}'. Videos live here once. Do not delete.")
        if peer_id:
            db.set_meta(key, str(peer_id))
        return peer_id


async def poke_storage_channel(client) -> None:
    """Let the bot (re)learn the cache channel after a restart.

    A bot can only open a channel it has seen in an update, and its session may be
    brand new (hosting wipes files). One silent post makes Telegram send the bot
    that update, then the post is removed again.
    """
    target = configured_storage()
    if target is None:
        return
    try:
        peer = await _resolve_peer(client, target)
        sent = await client.send_message(peer, "·", silent=True)
        await asyncio.sleep(1.5)
        await client.delete_messages(peer, [sent.id])
    except Exception as exc:
        log.debug("could not wake the storage channel for the bot: %s", exc)


async def storage_peer(client):
    """Where bot-side copies live. NEVER the owner's Saved Messages / DM."""
    target = configured_storage()
    if target is not None:
        return target
    async with _channel_lock:
        target = configured_storage()
        if target is not None:
            return target
        return await _create_storage_channel(client)


def _mirror_chat_id(message, fallback) -> int:
    """The real (marked) chat id of a message the userbot just posted."""
    value = getattr(message, "chat_id", None)
    if isinstance(value, int) and value:
        return value
    try:
        from telethon import utils
        return int(utils.get_peer_id(fallback))
    except Exception:
        return int(fallback) if isinstance(fallback, int) else 0


def mirror_targets(file_row: dict) -> list[tuple[int, int]]:
    """Every known bot-side location of this file, best first."""
    out: list[tuple[int, int]] = []
    if file_row.get("mirror_chat") and file_row.get("mirror_msg"):
        out.append((int(file_row["mirror_chat"]), int(file_row["mirror_msg"])))
    if file_row.get("chat_id") and file_row.get("msg_id"):
        out.append((int(file_row["chat_id"]), int(file_row["msg_id"])))
    return out


async def _media_from(client, chat_id: int, msg_id: int):
    msg = None
    for attempt in (1, 2):
        try:
            msg = await safe_call(client.get_messages, chat_id, ids=msg_id,
                                  what="mirror_fetch", retries=1)
            break
        except Exception as exc:
            if attempt == 1 and _is_entity_error(exc) and await _load_dialogs(client, force=True):
                continue                      # the cache was empty — retry once with it filled
            _fetch_errors[(int(chat_id), int(msg_id))] = str(exc)[:160]
            log.debug("mirror fetch %s/%s failed: %s", chat_id, msg_id, exc)
            return None
    if msg is None or not getattr(msg, "media", None):
        _fetch_errors[(int(chat_id), int(msg_id))] = "মেসেজটা নেই বা তাতে মিডিয়া নেই"
        return None
    _fetch_errors.pop((int(chat_id), int(msg_id)), None)
    return msg


async def _download(client, message, path: str, size: int | None = None) -> bool:
    """Download a message's media to `path` (ranged for big files)."""
    try:
        if size and size > 5 * CHUNK:
            await _download_ranged(client, message, path, size)
            return os.path.exists(path) and os.path.getsize(path) > 0
    except Exception as exc:
        log.debug("ranged download failed (%s) — falling back", exc)
    try:
        await client.download_media(message, file=path)
        return os.path.exists(path) and os.path.getsize(path) > 0
    except Exception as exc:
        log.warning("download failed: %s", exc)
        return False


async def _download_ranged(client, message, path: str, size: int) -> None:
    """Chunked parallel download (the trick from the owner's own forwarder).

    Telethon serialises `iter_download` per client, so the parallelism comes from
    splitting the file into ranges that are written at their own offsets.
    """
    parts = max(1, min(MAX_RANGED_THREADS, max(1, size // (20 * CHUNK))))
    chunk = size // parts

    lock = asyncio.Lock()

    async def fetch(start: int, length: int) -> None:
        handle = await asyncio.to_thread(open, path, "r+b")
        try:
            async for blob in client.iter_download(message.media, offset=start, limit=length,
                                                   chunk_size=CHUNK, request_size=CHUNK):
                async with lock:
                    await asyncio.to_thread(_write_at, handle, start, blob)
                start += len(blob)
        finally:
            await asyncio.to_thread(handle.close)

    # Pre-allocate so every worker writes at its own offset.
    with open(path, "wb") as handle:
        handle.truncate(size)
    await asyncio.gather(*[fetch(i * chunk, chunk if i < parts - 1 else size - i * chunk)
                           for i in range(parts)])


def _write_at(handle, offset: int, blob: bytes) -> None:
    handle.seek(offset)
    handle.write(blob)
    handle.flush()


async def ensure_mirror(file_row: dict, store: dict | None = None, force: bool = False) -> dict:
    """Make sure the bot can send this file. Returns the (possibly updated) row.

    One lock per file: 50 people clicking the same new video create **one** copy,
    not 50 copies in the channel.
    """
    if file_row is None:
        return {}
    if file_row.get("mirror_chat") and file_row.get("mirror_msg") and not force:
        return file_row
    lock = _file_locks.setdefault(int(file_row["id"]), asyncio.Lock())
    async with lock:
        fresh = db.file(file_row["id"]) or file_row
        if fresh.get("mirror_chat") and fresh.get("mirror_msg") and not force:
            return fresh
        try:
            return await _ensure_mirror_locked(fresh, store, force)
        finally:
            if not lock.locked():
                _file_locks.pop(int(file_row["id"]), None)


async def _ensure_mirror_locked(file_row: dict, store: dict | None, force: bool) -> dict:

    # 1. Maybe the source itself is already bot-readable — then no mirror needed.
    try:
        from app.runtime import bot
        msg = await _media_from(bot, file_row["chat_id"], file_row["msg_id"])
        if msg is not None:
            db.set_mirror(file_row["id"], file_row["chat_id"], file_row["msg_id"])
            return db.file(file_row["id"]) or file_row
    except Exception as exc:
        log.debug("source not readable by the bot: %s", exc)

    client = await live_session(file_row, store)
    if client is None:
        _note(file_row["id"], "ইউজারবট সেশন কানেক্ট নেই — ড্যাশবোর্ড/বট থেকে অ্যাডমিন সেশন কানেক্ট করুন")
        return file_row
    peer = await store_cache_peer(client, store)
    if peer is None:
        peer = await storage_peer(client)
    if peer is None:
        _note(file_row["id"], "ক্যাশ চ্যানেল বানানো যায়নি — একটা প্রাইভেট চ্যানেল বানিয়ে "
                              "সেটিংসে STORAGE_CHANNEL দিন (বট ওখানে অ্যাডমিন থাকবে)")
        return file_row
    try:
        peer = await _resolve_peer(client, peer)
    except Exception as exc:
        _note(file_row["id"], f"ইউজারবট ক্যাশ চ্যানেল খুলতে পারছে না ({str(exc)[:90]})")
        return file_row

    source = await _media_from(client, int(file_row["chat_id"]), int(file_row["msg_id"]))
    if source is None:
        why = _fetch_errors.get((int(file_row["chat_id"]), int(file_row["msg_id"])), "")
        _note(file_row["id"], f"ইউজারবট আসল ভিডিওটা ({file_row['chat_id']}) পড়তে পারছে না — "
                              f"এই সেশনের একাউন্ট কি ওই চ্যানেলে আছে? ({why[:90]})")
        return file_row

    size = int(getattr(getattr(source, "file", None), "size", 0) or file_row.get("bytes") or 0)
    max_mb = settings.get_int("MIRROR_MAX_MB", 1900) or 0
    if max_mb and size and size > max_mb * 1024 * 1024:
        _note(file_row["id"], f"ফাইলটা {size // (1024 * 1024)} MB — MIRROR_MAX_MB ({max_mb}) এর চেয়ে বড়")
        return file_row

    # 2. Try a cheap in-app copy (Telegram side, no download)…
    try:
        from app.runtime import bot
        copied = await safe_call(client.send_file, peer, source.media,
                                 what="mirror_copy", retries=1)
        if copied is not None and getattr(copied, "media", None):
            _problems.pop(int(file_row["id"]), None)
            db.set_mirror(file_row["id"], _mirror_chat_id(copied, peer), int(copied.id))
            log.info("mirrored file %s → %s/%s (copy)", file_row["id"],
                     _mirror_chat_id(copied, peer), copied.id)
            return db.file(file_row["id"]) or file_row
    except FloodWaitError:
        raise
    except Exception as exc:
        log.debug("in-app mirror copy failed for %s: %s", file_row["id"], exc)

    # 3. …otherwise download and re-upload (works for restricted/forward-protected).
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    tmp.close()
    try:
        if not await _download(client, source, tmp.name, size or None):
            _note(file_row["id"], "ভিডিও ডাউনলোড করা যায়নি (নেটওয়ার্ক/ফাইল সমস্যা)")
            return file_row
        sent = await safe_call(
            client.send_file, peer, tmp.name,
            caption=clean_text(file_row.get("name") or "")[:200],
            supports_streaming=file_row.get("kind") == "Video",
            attributes=None,
            what="mirror_upload", retries=1,
        )
        if sent is not None and getattr(sent, "media", None):
            _problems.pop(int(file_row["id"]), None)
            db.set_mirror(file_row["id"], _mirror_chat_id(sent, peer), int(sent.id))
            db.set_file_bytes(file_row["id"], os.path.getsize(tmp.name))
            log.info("mirrored file %s → %s/%s (re-upload, %.1f MB)", file_row["id"],
                     _mirror_chat_id(sent, peer), sent.id, os.path.getsize(tmp.name) / 1048576)
            return db.file(file_row["id"]) or file_row
    except FloodWaitError:
        raise
    except Exception as exc:
        log.warning("mirror upload failed for %s: %s", file_row["id"], exc)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return file_row


async def warm_mirrors(limit: int = 20, delay: float = 1.5) -> dict:
    """Pre-mirror files that still need it (called by the scheduler / a button).

    Doing it in the background keeps every later click instant and stops a
    broadcast from hammering the session. Only one warm-up runs at a time, and a
    file that just failed is skipped for an hour so it cannot block the rest.
    """
    import time
    from app.services.sender import mirror_ready
    if _warm_lock.locked():
        return {"mirrored": 0, "failed": 0, "ready": mirror_ready(), "busy": True}
    done = failed = 0
    async with _warm_lock:
        now = time.time()
        rows = [r for r in db.missing_mirrors(limit * 6)
                if now - _failed_recent.get(int(r["id"]), 0) > 3600][:limit]
        for row in rows:
            try:
                updated = await ensure_mirror(row, db.store(row["store_id"]))
                if updated.get("mirror_msg"):
                    done += 1
                else:
                    failed += 1
                    _failed_recent[int(row["id"])] = time.time()
            except FloodWaitError as exc:
                log.warning("mirror warm-up hit FloodWait (%ss) — stopping for now", exc.seconds)
                break
            except Exception as exc:
                failed += 1
                _failed_recent[int(row["id"])] = time.time()
                log.debug("mirror warm-up failed for %s: %s", row["id"], exc)
            await asyncio.sleep(delay)
    return {"mirrored": done, "failed": failed, "ready": mirror_ready()}


async def warm_all(delay: float = 2.0) -> None:
    """Copy every not-yet-mirrored file into the storage channel (after a scan)."""
    for _ in range(10000):
        result = await warm_mirrors(limit=10, delay=delay)
        if result.get("busy") or not result.get("mirrored"):
            break


def clear_private_mirrors() -> int:
    """Forget old mirrors that live in a private chat / Saved Messages.

    The bot can never read those, which is what made every delivery fall back to
    forwarding through the owner's account. They are re-created in the channel.
    """
    cur = db._run("UPDATE files SET mirror_chat = NULL, mirror_msg = NULL "
                  "WHERE mirror_chat IS NOT NULL AND mirror_chat > 0")
    try:
        return int(cur.rowcount or 0)
    except Exception:
        return 0


async def copy_between_chats(source_chat: int, msg_id: int, target_chat: int,
                             client=None) -> int | None:
    """Copy one message to another chat without a visible forward (used by the
    “add video” flow and by the channel composer)."""
    from app.runtime import bot
    worker = client or bot
    message = await _media_from(worker, source_chat, msg_id)
    if message is None:
        return None
    try:
        sent = await safe_call(worker.send_file, target_chat, message.media,
                               caption=getattr(message, "message", "") or None,
                               what="copy_media", retries=1)
        return int(getattr(sent, "id", 0) or 0) if sent else None
    except Exception as exc:
        log.debug("copy_between_chats failed: %s", exc)
        return None


def stats() -> dict:
    total = len(db.mirrored_files(limit=1_000_000)) and db.count_files()
    mirrored = db.count_mirrors()
    return {"files": total, "mirrored": mirrored, "missing": max(0, total - mirrored)}
