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
import shutil
import tempfile

from telethon import utils
from telethon.errors import FloodWaitError

from app import runtime
from app.logger import log
from app.services import peers, settings, native_media
from app.services.telegram import safe_call
from app.services.textclean import clean_text
from app.storage import db

CHUNK = 1024 * 1024
MAX_RANGED_THREADS = 4
_reserved_downloads: dict[object, int] = {}


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


def _is_stale_channel_error(exc: Exception) -> bool:
    """“Invalid channel object” — the saved access hash does not fit this account."""
    text = str(exc).lower()
    return ("invalid channel" in text or "channel invalid" in text or "channel_invalid" in text
            or type(exc).__name__ == "ChannelInvalidError")


def _forget_channels(file_row: dict | None, store: dict | None) -> None:
    """Forget every saved channel hash this file's copy job depends on."""
    ids = []
    if file_row:
        ids.append(file_row.get("chat_id"))
    try:
        ids.append(configured_storage())
        if store and store.get("id"):
            ids.append(_as_peer(db.get_meta(_store_meta_key(store["id"]), "")))
    except Exception:
        pass
    for value in ids:
        if isinstance(value, int) and value < 0:
            peers.forget_chat(value)


async def _load_dialogs(client, force: bool = False) -> bool:
    """A fresh StringSession knows no channels at all ("Could not find the input
    entity…"). Reading the dialog list teaches it every chat the account is in.

    ``limit=None`` matters: the default stops at 100 dialogs, so an account that is
    in more chats than that never learns the cache channel — that *is* the
    “Could not find the input entity for PeerChannel” in the logs.
    """
    import time
    key = id(client)
    if not force and time.time() - _dialogs_at.get(key, 0) < 300:
        return False
    try:
        if client is runtime.get_client():
            return False                       # bots cannot list dialogs
    except Exception:
        pass
    try:
        dialogs = list(await client.get_dialogs(limit=None) or [])
    except Exception as exc:
        log.debug("could not load dialogs: %s", exc)
        return False
    # The default walk skips the Archive folder, so a channel the owner archived
    # was never learned. Read folder 1 too (best effort).
    try:
        dialogs += list(await client.get_dialogs(limit=None, folder=1) or [])
    except Exception as exc:
        log.debug("could not load archived dialogs: %s", exc)
    _dialogs_at[key] = time.time()
    learned = 0
    for dialog in dialogs or []:
        entity = getattr(dialog, "entity", None)
        if entity is None:
            continue
        try:
            peer_id = int(utils.get_peer_id(entity))
        except Exception:
            continue
        if peer_id < 0:
            learned += 1                       # the session itself now knows it
    log.info("userbot dialogs loaded (%s chats known)", learned)
    return True


def _is_bot(client) -> bool:
    try:
        return client is runtime.get_client()
    except Exception:
        return False


async def _resolve_peer(client, peer):
    """Make sure `client` knows the channel (a fresh StringSession has no cache).

    Telegram access hashes are **per account**: the hash the bot learned is not
    valid for the owner's userbot ("Invalid channel object"). So the SQLite copy
    (filled from the bot's own updates) is used for the bot only; the userbot always
    asks its own session, then its own dialog list (archive folder included).
    """
    numeric = (isinstance(peer, (int, str)) and str(peer).lstrip("-").isdigit()
               and int(peer) < 0)
    cached = peers.chat_entity(peer) if (numeric and _is_bot(client)) else None
    if cached is not None:
        return cached
    try:
        entity = await client.get_input_entity(peer)
    except Exception:
        await _load_dialogs(client, force=True)
        try:
            entity = await client.get_input_entity(peer)
        except Exception:
            await _note_channel_problem(peer)
            raise
    _remember_entity(entity, client)
    return entity


def _remember_entity(entity, client=None) -> None:
    """Store the (id, access_hash) Telegram handed the *bot* (never a userbot's)."""
    if entity is None or not _is_bot(client):
        return
    try:
        peer_id = int(utils.get_peer_id(entity))
    except Exception:
        return
    if peer_id < 0:
        peers.remember_chat(peer_id, getattr(entity, "access_hash", None),
                            getattr(entity, "title", None),
                            getattr(entity, "username", None),
                            "channel" if peers.is_channel_id(peer_id) else "chat")


#: One sentence the panel, the bot and the log all reuse — the cure is always this.
_CHANNEL_FIX = ("ঠিক করার উপায়: বটে <code>/storageset probe</code> (অথবা <code>/storageset "
                "@channelname</code>) দিন, ওয়েবসাইটের 📡 Channels পেজে “♻️ রিসেট” চাপুন, "
                "নাহলে <code>/storageset auto</code> দিয়ে প্রতি স্টোরের আলাদা চ্যানেলে "
                "ফিরে যান — সেশন নিজেই নতুন চ্যানেল বানিয়ে বটকে অ্যাডমিন করে দেবে।")


async def _note_channel_problem(peer) -> None:
    """Turn “Could not find the input entity for PeerChannel(…)” into a to-do list."""
    from app.services import settings as settings_service
    text = (f"ক্যাশ চ্যানেল ({peer}) খুলতে পারছে না — Telegram অ্যাক্সেস হ্যাশ জানে না। "
            + _CHANNEL_FIX.replace("<code>", "").replace("</code>", ""))
    log.warning("storage channel unreachable: %s", text)
    try:
        settings_service.set("STORAGE_CHANNEL_WARNING", text[:400])
    except Exception:
        pass


async def _create_cache_channel(client, title: str, about: str, remember: bool = False):
    """Create a private channel with the owner's session and put the bot in it as admin."""
    from telethon import functions
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
    except FloodWaitError:
        raise
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


async def store_cache_peer(client, store: dict | None, *, dedicated: bool = False):
    """One private cache channel per store, created once by the owner's session.

    The videos of a store are copied there (no caption, so no foreign tags) and every
    user delivery is a plain bot send from that copy. Off when the panel's
    STORAGE_CHANNEL is set by hand or the PER_STORE_CHANNEL setting is switched off.
    """
    if not store or (not dedicated and settings.get_str("STORAGE_CHANNEL")):
        return None
    if not dedicated and not settings.get_bool("PER_STORE_CHANNEL", True):
        return None
    bound = db.impl
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
            f"Auto-created for the store '{store.get('name')}'. Videos live here once. Do not delete.",
            remember=True)
        if peer_id and db.impl is bound:
            db.set_meta(key, str(peer_id))
        elif db.impl is not bound:
            return None
        return peer_id


async def provision_store(store_id: int) -> dict:
    """Give a brand-new store its own channel(s) without anyone asking.

    The media cache is one private channel **per store** (`store_cache_peer`), so a
    store's videos never travel through another store's chat. When
    `AUTO_STORE_PUBLISH_CHANNEL` is on, a public publishing channel is created too —
    the same one the store page's “📣 পাবলিশ চ্যানেল” button makes by hand.

    Runs in the background after a store is created; every failure is only logged,
    because a store must never fail to exist because Telegram was busy.
    """
    from app.storage import db as _db
    store = _db.store(int(store_id))
    if store is None:
        return {"ok": False, "error": "স্টোর নেই"}
    client = None
    try:
        preferred = int(store.get("admin_id") or 0)
        client = runtime.user_clients.get(preferred) or next(iter(runtime.user_clients.values()), None)
    except Exception:
        client = None
    if client is None:
        log.debug("store %s: no user session yet — channels will be created on first use", store_id)
        return {"ok": False, "error": "সেশন নেই"}

    out: dict = {"ok": True, "cache": None, "publish": None}
    if not settings.get_str("STORAGE_CHANNEL"):
        try:
            out["cache"] = await store_cache_peer(client, store)
        except Exception as exc:
            log.warning("store %s: cache channel failed: %s", store_id, exc)
    if settings.get_bool("AUTO_STORE_PUBLISH_CHANNEL", False) and not store.get("publish_channel_id"):
        try:
            from app.services import channels
            result = await channels.create_store_channel(int(store_id), title=store["name"])
            out["publish"] = result.get("channel_id")
            if not result.get("ok"):
                log.warning("store %s: publish channel failed: %s", store_id,
                            result.get("error"))
        except Exception as exc:
            log.warning("store %s: publish channel failed: %s", store_id, exc)
    log.info("store %s provisioned: cache=%s publish=%s", store_id, out["cache"], out["publish"])
    return out


def provision_store_later(store_id: int) -> None:
    """Fire-and-forget version for the store-creation flows."""
    try:
        runtime.spawn(provision_store(int(store_id)))
    except Exception as exc:                       # no event loop (tests) — fine
        log.debug("could not schedule store provisioning: %s", exc)


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
            peer = await _resolve_peer(client, chat_id)
            msg = await safe_call(client.get_messages, peer, ids=msg_id,
                                  what="mirror_fetch", retries=1)
            break
        except FloodWaitError:
            raise
        except Exception as exc:
            if attempt == 1:
                stale = _is_stale_channel_error(exc)
                if stale:
                    peers.forget_chat(chat_id)   # wrong hash: forget it, learn it again
                if (stale or _is_entity_error(exc)) and (
                        await _load_dialogs(client, force=True) or stale):
                    continue                  # retry once with a refreshed cache
            _fetch_errors[(int(chat_id), int(msg_id))] = str(exc)[:160]
            log.debug("mirror fetch %s/%s failed: %s", chat_id, msg_id, exc)
            return None
    if msg is None or not getattr(msg, "media", None):
        _fetch_errors[(int(chat_id), int(msg_id))] = "মেসেজটা নেই বা তাতে মিডিয়া নেই"
        return None
    _fetch_errors.pop((int(chat_id), int(msg_id)), None)
    return msg


async def _download(client, message, path: str, size: int | None = None,
                    progress=None) -> bool:
    """Download a message's media to `path` (ranged for big files)."""
    try:
        if size and size > 5 * CHUNK:
            await _download_ranged(client, message, path, size, progress=progress)
            return os.path.exists(path) and os.path.getsize(path) > 0
    except FloodWaitError:
        raise
    except Exception as exc:
        log.debug("ranged download failed (%s) — falling back", exc)
    try:
        options = {"progress_callback": progress} if progress else {}
        await safe_call(client.download_media, message, file=path, what="mirror_download", retries=1, **options)
        return os.path.exists(path) and os.path.getsize(path) > 0
    except FloodWaitError:
        raise
    except Exception as exc:
        log.warning("download failed: %s", exc)
        return False


async def _download_ranged(client, message, path: str, size: int, progress=None) -> None:
    """Bounded aligned ranges; Telegram's limit counts chunks, not bytes.

    Never preallocate and call a partial file a successful download. Each range
    verifies its exact byte count, otherwise the caller retries a normal download.
    """
    block = 512 * 1024
    parts = max(1, min(MAX_RANGED_THREADS, max(1, size // (20 * CHUNK))))
    span = ((size + parts - 1) // parts + block - 1) // block * block

    completed = {}
    async def fetch(start: int, end: int) -> None:
        origin = start
        written = 0
        with open(path, "r+b") as handle:
            async for blob in client.iter_download(message.media, offset=start,
                                                   request_size=block):
                blob = blob[:end - start - written]
                handle.seek(start + written)
                handle.write(blob)
                written += len(blob)
                completed[origin] = written
                if progress:
                    progress(sum(completed.values()), size)
                if start + written >= end:
                    break
        if written != end - start:
            raise IOError("Incomplete media download")

    with open(path, "wb"):
        pass
    tasks = [asyncio.create_task(fetch(start, min(size, start + span)))
             for start in range(0, size, span)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _write_at(handle, offset: int, blob: bytes) -> None:
    handle.seek(offset)
    handle.write(blob)
    handle.flush()


def is_archived(file_row: dict, store: dict | None = None) -> bool:
    """A source-as-mirror/shared legacy channel is not this store's archive."""
    store_id = (store or {}).get('id') or file_row.get('store_id')
    if not store_id or not file_row.get('mirror_msg'):
        return False
    target = _as_peer(db.get_meta(_store_meta_key(store_id), ''))
    return (isinstance(target, int) and target != file_row.get('chat_id')
            and target == file_row.get('mirror_chat'))


async def ensure_mirror(file_row: dict, store: dict | None = None, force: bool = False,
                        *, archive: bool = False) -> dict:
    """Make sure the bot can send this file. Returns the (possibly updated) row.

    One lock per file: 50 people clicking the same new video create **one** copy,
    not 50 copies in the channel.
    """
    if file_row is None:
        return {}
    if file_row.get("mirror_chat") and file_row.get("mirror_msg") and not force and (
            not archive or is_archived(file_row, store)):
        return file_row
    lock = _file_locks.setdefault(int(file_row["id"]), asyncio.Lock())
    async with lock:
        fresh = db.file(file_row["id"]) or file_row
        if fresh.get("mirror_chat") and fresh.get("mirror_msg") and not force and (
                not archive or is_archived(fresh, store)):
            return fresh
        try:
            try:
                return await _ensure_mirror_locked(fresh, store, force, archive=archive)
            except Exception as exc:
                if not _is_stale_channel_error(exc):
                    raise
                # "Invalid channel object": drop the wrong saved hashes, let the
                # session learn the channels again, and try exactly once more.
                _forget_channels(fresh, store)
                client = await live_session(fresh, store)
                if client is not None:
                    await _load_dialogs(client, force=True)
                return await _ensure_mirror_locked(fresh, store, force, archive=archive)
        finally:
            if not lock.locked():
                _file_locks.pop(int(file_row["id"]), None)


async def _ensure_mirror_locked(file_row: dict, store: dict | None, force: bool,
                                *, archive: bool = False) -> dict:

    repository = db.impl
    tracker = None
    if archive:
        from app.services.transfer_progress import Tracker
        tracker = Tracker(f"store:{file_row['store_id']}", int(file_row['id']),
                          int((store or {}).get('admin_id') or 0), file_row.get('name') or 'ভিডিও')
        tracker.phase('prepare')
    # 1. Maybe the source itself is already bot-readable — then no mirror needed.
    try:
        from app.runtime import bot
        msg = None if archive else await _media_from(bot, file_row["chat_id"], file_row["msg_id"])
        if msg is not None:
            repository.set_mirror(file_row["id"], file_row["chat_id"], file_row["msg_id"])
            return repository.file(file_row["id"]) or file_row
    except Exception as exc:
        log.debug("source not readable by the bot: %s", exc)

    client = await live_session(file_row, store)
    if client is None:
        _note(file_row["id"], "ইউজারবট সেশন কানেক্ট নেই — ড্যাশবোর্ড/বট থেকে অ্যাডমিন সেশন কানেক্ট করুন")
        return file_row
    if tracker:
        tracker.phase('channel')
    peer = await store_cache_peer(client, store, dedicated=True) if archive else await store_cache_peer(client, store)
    if peer is None and not archive:
        peer = await storage_peer(client)
    if peer is None:
        _note(file_row["id"],
              "স্টোরের আলাদা চ্যানেল তৈরি করা যায়নি — মালিকের সেশন, চ্যানেল তৈরির সীমা "
              "এবং বটকে অ্যাডমিন করার অনুমতি পরীক্ষা করুন।" if archive else
              "ক্যাশ চ্যানেল বানানো যায়নি — STORAGE_CHANNEL ও বটের অ্যাডমিন অনুমতি পরীক্ষা করুন।")
        return file_row
    try:
        peer = await _resolve_peer(client, peer)
    except Exception as exc:
        from app.services.error_reports import friendly_error
        _note(file_row["id"], f"ইউজারবট ক্যাশ চ্যানেল খুলতে পারছে না ({friendly_error(exc)[:120]})")
        # Self-healing: the configured channel is unreachable *and* this store may
        # have its own cache channel — try that before giving up on the file.
        if store is not None and settings.get_bool("PER_STORE_CHANNEL", True):
            try:
                fallback = await store_cache_peer(client, store)
                if fallback and fallback != peer:
                    peer = await _resolve_peer(client, fallback)
                    log.info("cache channel switched to the store channel %s", peer)
            except Exception as second:
                log.debug("store cache channel also unreachable: %s", second)
        else:
            return file_row

    source = await _media_from(client, int(file_row["chat_id"]), int(file_row["msg_id"]))
    if source is None:
        why = _fetch_errors.get((int(file_row["chat_id"]), int(file_row["msg_id"])), "")
        log.info("Source read failed for file %s: %s", file_row["id"], why)
        _note(file_row["id"], "এই সেশনের অ্যাকাউন্ট চ্যানেলটি পড়তে পারছে না। "
                              "চ্যানেলে জয়েন করুন বা সেশন থেকে সঠিক চ্যানেলটি আবার বাছুন।")
        return file_row

    size = int(getattr(getattr(source, "file", None), "size", 0) or file_row.get("bytes") or 0)
    max_mb = settings.get_int("MIRROR_MAX_MB", 1900) or 0
    if max_mb and size and size > max_mb * 1024 * 1024:
        _note(file_row["id"], f"ফাইলটা {size // (1024 * 1024)} MB — MIRROR_MAX_MB ({max_mb}) এর চেয়ে বড়")
        return file_row

    # 2. Try forwarding before downloading; protected sources reject this.
    try:
        from app.runtime import bot
        if archive:
            if tracker:
                tracker.phase('forward')
            source_peer = await _resolve_peer(client, int(file_row["chat_id"]))
            copied = await safe_call(client.forward_messages, peer, source.id,
                                     from_peer=source_peer, drop_author=True, drop_media_captions=True,
                                     what="mirror_forward", retries=1)
            if isinstance(copied, list):
                copied = copied[0] if copied else None
        else:
            copied = await safe_call(client.send_file, peer, source.media,
                                     what="mirror_copy", retries=1)
        if copied is not None and getattr(copied, "media", None):
            _problems.pop(int(file_row["id"]), None)
            repository.set_mirror(file_row["id"], _mirror_chat_id(copied, peer), int(copied.id))
            log.info("mirrored file %s → %s/%s (copy)", file_row["id"],
                     _mirror_chat_id(copied, peer), copied.id)
            return repository.file(file_row["id"]) or file_row
    except FloodWaitError:
        raise
    except Exception as exc:
        log.debug("in-app mirror copy failed for %s: %s", file_row["id"], exc)

    # 3. …otherwise download and re-upload (works for restricted/forward-protected).
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix="bfixf-copy-",
                                      suffix=native_media.extension(source, file_row.get("kind") or ""))
    thumb_path = None
    tmp.close()
    try:
        free = shutil.disk_usage(os.path.dirname(tmp.name)).free
        required = size or max_mb * 1024 * 1024 or 2 * 1024 ** 3
        if required + sum(_reserved_downloads.values()) + 64 * 1024 * 1024 > free:
            _note(file_row['id'], "সার্ভারে পর্যাপ্ত ফাঁকা ডিস্ক নেই — ডিস্ক ফাঁকা করে আবার চেষ্টা করুন")
            return file_row
        _reserved_downloads[int(file_row['id'])] = required
        if tracker:
            tracker.phase('download', size)
        download_options = {'progress': tracker.callback} if tracker else {}
        if not await _download(client, source, tmp.name, size or None, **download_options):
            _note(file_row["id"], "ভিডিও ডাউনলোড করা যায়নি (নেটওয়ার্ক/ফাইল সমস্যা)")
            return file_row
        # Keep the bytes we just downloaded: the website player can then serve this
        # file instantly (and even while Telegram is unreachable) — see store_bytes.
        if not archive and os.path.getsize(tmp.name) <= _max_cache_bytes():
            try:
                with open(tmp.name, "rb") as handle:
                    store_bytes(file_row, handle.read())
            except OSError as exc:
                log.debug("caching the downloaded mirror failed: %s", exc)
        # Preserve the source's duration, dimensions and filename attributes.
        upload_options = native_media.upload_options(source, file_row.get("kind") or "")
        if tracker:
            tracker.phase('thumb')
        try:
            thumb_file = tempfile.NamedTemporaryFile(delete=False, prefix="bfixf-copy-", suffix=".jpg")
            thumb_path = thumb_file.name
            thumb_file.close()
            await safe_call(client.download_media, source, file=thumb_path, thumb=-1,
                            what="mirror_thumb", retries=1)
            if not os.path.getsize(thumb_path):
                os.unlink(thumb_path)
                thumb_path = None
        except FloodWaitError:
            raise
        except Exception:
            if thumb_path:
                try:
                    os.unlink(thumb_path)
                except OSError:
                    pass
            thumb_path = None
        if tracker:
            tracker.phase('upload', os.path.getsize(tmp.name))
            upload_options['progress_callback'] = tracker.callback
        sent = await safe_call(
            client.send_file, peer, tmp.name,
            caption="",
            thumb=thumb_path, **upload_options,
            what="mirror_upload", retries=1,
        )
        if sent is not None and getattr(sent, "media", None):
            _problems.pop(int(file_row["id"]), None)
            repository.set_mirror(file_row["id"], _mirror_chat_id(sent, peer), int(sent.id))
            repository.set_file_bytes(file_row["id"], os.path.getsize(tmp.name))
            log.info("mirrored file %s → %s/%s (re-upload, %.1f MB)", file_row["id"],
                     _mirror_chat_id(sent, peer), sent.id, os.path.getsize(tmp.name) / 1048576)
            return repository.file(file_row["id"]) or file_row
    except FloodWaitError:
        raise
    except Exception as exc:
        _note(file_row["id"], f"আপলোড ব্যর্থ: {str(exc)[:200]}")
    finally:
        _reserved_downloads.pop(int(file_row['id']), None)
        for path in (tmp.name, thumb_path):
            if path:
                try:
                    os.unlink(path)
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
                if now - _failed_recent.get(int(r["id"]), 0) > 3600
                and not db._one("SELECT 1 FROM video_copy_items WHERE file_id=? AND store_id=?",
                                (r['id'], r['store_id']))][:limit]
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
                               caption="",
                               what="copy_media", retries=1)
        return int(getattr(sent, "id", 0) or 0) if sent else None
    except Exception as exc:
        log.debug("copy_between_chats failed: %s", exc)
        return None


# ---------------------------------------------------------------- web byte cache
# Why this exists
#   The public site plays media straight from Telegram. When the mirror is not
#   ready yet — or the owner's userbot session is offline — the browser gets a
#   404, the player shows “চালানো যাচ্ছে না” and jumps to the next slide. That is
#   exactly the “মিডিয়া চলতেছে না” the owner reported.
#
#   The fix: any file the bot *has* downloaded for a store click (the mirror
#   re-upload path) is also kept on disk, small and ready. The website then
#   serves it instantly, with Range support, even with Telegram unreachable —
#   and the first visitor never waits for a mirror that is still being built.
#
#   Only `MEDIA_CACHE_MAX_MB` (default 64 MB) per file is kept; bigger videos are
#   streamed from Telegram as before, and a full cache is pruned oldest-first.
def bytes_dir() -> "Path":
    from pathlib import Path
    return Path(os.getenv("MEDIA_CACHE_DIR", "media_cache"))


def _max_cache_bytes() -> int:
    mb = settings.get_int("MEDIA_CACHE_MAX_MB", 64) or 64
    return max(1, mb) * 1024 * 1024


def _total_cache_bytes() -> int:
    budget = (settings.get_int("MEDIA_CACHE_TOTAL_MB", 1024) or 1024) * 1024 * 1024
    return max(64 * 1024 * 1024, budget)


def cache_key(file_row: dict) -> str:
    """A stable name for one media item — a store file *or* a hand-picked pin."""
    if not file_row:
        return ""
    file_id = int(file_row.get("id") or 0)
    if file_id:
        return f"f{file_id}"
    pin_id = int(file_row.get("pin_id") or 0)
    if pin_id:
        return f"p{pin_id}"
    chat = int(file_row.get("chat_id") or 0)
    msg = int(file_row.get("msg_id") or 0)
    return f"c{chat}_{msg}" if chat and msg else ""


def _bytes_path(file_row, suffix: str = ".bin"):
    key = file_row if isinstance(file_row, str) else cache_key(file_row)
    if not key:
        raise ValueError("no cache key")
    return bytes_dir() / f"{key}{suffix}"


def store_bytes(file_row: dict, payload: bytes, thumb: bytes = b"") -> bool:
    """Remember what we already downloaded, so the website can play it at once."""
    try:
        key = cache_key(file_row)
        if not key or not payload:
            return False
        if len(payload) > _max_cache_bytes():
            return False
        directory = bytes_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _bytes_path(key).write_bytes(payload)
        if thumb:
            _bytes_path(key, ".jpg").write_bytes(thumb[:1024 * 512])
        db.set_meta(f"webcache:{key}", str(len(payload)))
        _prune_cache()
        _cache_problems.pop(key, None)
        log.info("cached %s (%.2f MB) for the website player", key,
                 len(payload) / 1048576)
        return True
    except OSError as exc:
        log.debug("byte cache write failed: %s", exc)
        return False


def _prune_cache() -> int:
    """Keep the folder under the budget — oldest first, never a file in use."""
    budget = _total_cache_bytes()
    entries = []
    total = 0
    try:
        for path in bytes_dir().glob("*.bin"):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
    except OSError:
        return 0
    if total <= budget:
        return 0
    removed = 0
    for _mtime, size, path in sorted(entries):        # oldest first
        if total <= budget:
            break
        try:
            path.unlink()
            _bytes_path(path.stem, ".jpg").unlink(missing_ok=True)
            db.set_meta(f"webcache:{path.stem}", "")
            total -= size
            removed += 1
        except (OSError, ValueError):
            continue
    if removed:
        log.info("pruned %s cached clip(s) to stay inside the cache budget", removed)
    return removed


def local_bytes(file_row: dict) -> bytes | None:
    """The whole cached file, or None when it is not cached (yet)."""
    try:
        path = _bytes_path(file_row)
        if path.is_file():
            return path.read_bytes()
    except (OSError, TypeError, ValueError):
        return None
    return None


def local_size(file_row: dict) -> int:
    try:
        path = _bytes_path(file_row)
        return path.stat().st_size if path.is_file() else 0
    except (OSError, TypeError, ValueError):
        return 0


def local_thumb(file_row: dict) -> bytes | None:
    try:
        path = _bytes_path(file_row, ".jpg")
        return path.read_bytes() if path.is_file() else None
    except (OSError, TypeError, ValueError):
        return None


async def iter_bytes(file_row: dict, start: int, end: int):
    """Stream a byte range of a cached clip (max 1 MB per read)."""
    path = _bytes_path(file_row)
    step = 1024 * 1024
    with open(path, "rb") as handle:
        handle.seek(int(start))
        remaining = max(0, int(end) - int(start) + 1)
        while remaining > 0:
            chunk = handle.read(min(step, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def forget_bytes(file_row) -> bool:
    """Drop one cached clip (the panel's 🗑 button)."""
    removed = False
    try:
        key = cache_key(file_row) if isinstance(file_row, dict) else str(file_row)
    except Exception:
        return False
    for suffix in (".bin", ".jpg"):
        try:
            _bytes_path(key, suffix).unlink()
            removed = True
        except OSError:
            continue
    db.set_meta(f"webcache:{key}", "")
    return removed


def cached_count() -> int:
    try:
        return sum(1 for _ in bytes_dir().glob("*.bin"))
    except OSError:
        return 0


_cache_problems: dict[str, str] = {}


def cache_problem(file_row) -> str:
    """Why the last on-demand fetch for this item failed ('' = none)."""
    try:
        key = cache_key(file_row) if isinstance(file_row, dict) else str(file_row)
    except Exception:
        return ""
    return _cache_problems.get(key, "")


async def fetch_now(file_row: dict, store: dict | None = None,
                    timeout: float = 12.0) -> bytes | None:
    """Download a file the bot can read and cache it — right now, on demand.

    Called by the website when a visitor presses “চালান” on a clip that has no
    cache yet: the player waits for real bytes instead of showing “লোড হলো না”.
    """
    if not file_row:
        return None
    from app.runtime import bot
    cached = local_bytes(file_row)
    if cached:
        return cached
    try:
        message = await _media_from(bot, int(file_row["chat_id"]), int(file_row["msg_id"]))
    except Exception as exc:
        message = None
        log.debug("on-demand fetch: %s", exc)
    if message is None:
        try:
            client = await live_session(file_row, store)
            if client is not None:
                message = await _media_from(client, int(file_row["chat_id"]),
                                            int(file_row["msg_id"]))
        except Exception as exc:
            log.debug("on-demand fetch via the session: %s", exc)
    if message is None:
        _cache_problems[cache_key(file_row)] = "মিডিয়া এখন পড়া যাচ্ছে না"
        return None
    size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
    if size and size > _max_cache_bytes():
        _cache_problems[cache_key(file_row)] = (
            f"ফাইলটা {size // (1024 * 1024)} MB — স্বয়ংক্রিয় ক্যাশের সীমার চেয়ে বড়")
        return None
    try:
        payload = await asyncio.wait_for(
            bot.download_media(message, file=bytes), timeout=timeout)
    except Exception as exc:
        _cache_problems[cache_key(file_row)] = f"ডাউনলোড ব্যর্থ ({str(exc)[:60]})"
        return None
    if not payload:
        _cache_problems[cache_key(file_row)] = "খালি ফাইল এসেছে"
        return None
    payload = bytes(payload)
    thumb = b""
    try:
        thumb = await bot.download_media(message, file=bytes, thumb=-1) or b""
    except Exception:
        thumb = b""
    store_bytes(file_row, payload, bytes(thumb) if thumb else b"")
    return payload


def stats() -> dict:
    total = len(db.mirrored_files(limit=1_000_000)) and db.count_files()
    mirrored = db.count_mirrors()
    return {"files": total, "mirrored": mirrored, "missing": max(0, total - mirrored)}
