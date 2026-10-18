"""Telegram peer cache — the reason broadcasts keep working after a restart.

The problem
-----------
A bot cannot send anything to a bare numeric user id: Telegram needs the pair
``(user_id, access_hash)``. Telethon hides that behind its entity cache, which
lives **only in the .session file**. Hosting panels recreate the disk (or the
owner finishes the setup on another machine), the session file is fresh, and
every single send dies with::

    ValueError: Could not find the input entity for PeerUser(user_id=…)

That is why “the broadcast system does not work at all” while the users are
clearly in the database.

The fix
-------
Whenever *any* update arrives (a `/start`, a button tap, an upload) we store the
access hash in the ``users`` table. Sending then builds

    ``InputPeerUser(user_id, access_hash)``

ourselves — no session cache, no dialog list, no re-discovery, and it keeps
working on a brand new host with an empty session.

If the hash is stale, Telegram answers ``PEER_ID_INVALID``; ``forget()`` drops it
and the next attempt re-discovers it from a live update.
"""
from __future__ import annotations

from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser, User as TgUser

#: “Could not find the input entity for PeerChannel” — the same disease as users,
#: but for the media-cache channel and every store channel.
CHANNEL_HINTS = ("input entity", "peer id invalid", "cannot find any entity",
                 "channel invalid", "invalid channel")

from app.logger import log
from app.runtime import bot
from app.storage import db


class Unreachable(Exception):
    """This user cannot be messaged right now — with a reason meant for a human."""


#: Telegram's ways of saying “I do not know this peer”.
UNREACHABLE_HINTS = ("could not find the input entity", "peer id invalid",
                     "cannot find any entity", "bot can't initiate conversation",
                     "user is deactivated", "user not found")


def unreachable_reason(detail: str) -> str:
    """A friendly explanation when an error really means “cannot be reached”."""
    low = (detail or "").lower()
    if "input entity" in low or "peer id invalid" in low or "cannot find any entity" in low:
        return ("বট কখনো এই ইউজারের কাছ থেকে আপডেট পায়নি (ওরা বটে /start দেয়নি) — "
                "Telegram অ্যাক্সেস হ্যাশ দেয় না")
    if "initiate conversation" in low:
        return "ইউজার আগে কখনো বটে /start দেয়নি"
    if "deactivated" in low or "user not found" in low:
        return "ইউজার অ্যাকাউন্ট ডিলিট বা ডিঅ্যাক্টিভেট করা হয়েছে"
    return ""


#: marked channel id -> access_hash (fast path for the cache/store channels)
_chat_hashes: dict[int, int] = {}
#: user_id -> access_hash validated in this process (fast path, no DB hit)
_hashes: dict[int, int] = {}
#: user ids that failed completely — remembered so a 5 000-user broadcast does
#: not spend a network round trip on each of them again
_hopeless: dict[int, str] = {}
#: which database the caches above belong to (tests bind a fresh file per test)
_bound_db: str = ""


def _check_binding() -> None:
    """Drop the in-memory caches when the storage layer points at another file."""
    global _bound_db
    path = str(getattr(db, "path", "") or "")
    if path != _bound_db:
        _bound_db = path
        _hashes.clear()
        _hopeless.clear()
        _chat_hashes.clear()


# ------------------------------------------------------------- channels & groups
def split_peer(peer_id) -> tuple[int, int]:
    """`-1001234567890` → `(1234567890, 1)` (channel) — `-987` → `(987, 0)` (chat)."""
    value = int(peer_id)
    if value < 0 and str(abs(value)).startswith("100") and abs(value) > 10 ** 10:
        return int(str(abs(value))[3:]), 1
    return abs(value), 0


def remember_chat(peer_id: int, access_hash: int | None, title: str | None = None,
                  username: str | None = None, kind: str = "channel") -> None:
    """Save a channel/group the way `remember()` saves a user."""
    _check_binding()
    peer_id = int(peer_id)
    if access_hash:
        _chat_hashes[peer_id] = int(access_hash)
    try:
        db.set_chat_peer(peer_id, access_hash, title, username, kind)
    except Exception as exc:
        log.debug("could not remember chat %s: %s", peer_id, exc)


def forget_chat(peer_id: int) -> None:
    """Drop a channel's saved hash (Telegram said it is wrong for this account).

    Access hashes are per account: a hash saved from another session makes every
    call fail with "Invalid channel object". Forgetting it lets the session learn
    the channel again from its own dialogs.
    """
    try:
        peer_id = int(peer_id)
    except (TypeError, ValueError):
        return
    _chat_hashes.pop(peer_id, None)
    try:
        db._run("UPDATE chat_peers SET access_hash = NULL WHERE peer_id = ?", (peer_id,))
    except Exception as exc:
        log.debug("could not forget chat %s: %s", peer_id, exc)


def known_chat_hash(peer_id: int) -> int | None:
    _check_binding()
    peer_id = int(peer_id)
    if peer_id in _chat_hashes:
        return _chat_hashes[peer_id]
    try:
        row = db.chat_peer(peer_id)
    except Exception:
        row = None
    if row and row.get("access_hash"):
        _chat_hashes[peer_id] = int(row["access_hash"])
        return _chat_hashes[peer_id]
    return None


def chat_entity(peer) -> object | None:
    """An `InputPeerChannel` / `InputPeerChat` when the hash is already known.

    This is what the media cache needs: the userbot restarted with a fresh session,
    Telegram forgot every channel, and we still hold the pair in SQLite.
    """
    try:
        peer_id = int(peer)
    except (TypeError, ValueError):
        return None
    raw, is_channel = split_peer(peer_id)
    if is_channel:
        hashed = known_chat_hash(peer_id)
        return InputPeerChannel(raw, hashed) if hashed else None
    return InputPeerChat(raw)


def remember_from_message(message) -> None:
    """Learn a channel from any message object (userbot uploads, channel posts)."""
    chat = getattr(message, "chat", None)
    peer_id = getattr(message, "chat_id", None)
    if isinstance(peer_id, int) and peer_id and chat is not None:
        remember_chat(peer_id, getattr(chat, "access_hash", None),
                      getattr(chat, "title", None), getattr(chat, "username", None),
                      "channel" if is_channel_id(peer_id) else "chat")
        return
    peer = getattr(getattr(message, "peer_id", None), "channel_id", None)
    if peer:
        remember_chat(int(f"-100{peer}"), None, title=None)


def is_channel_id(peer_id) -> bool:
    return split_peer(peer_id)[1] == 1


def remember(user_id: int, access_hash: int | None, name: str | None = None,
             username: str | None = None) -> None:
    """Save what Telegram told us about a user (called from the update handlers)."""
    _check_binding()
    user_id = int(user_id)
    if access_hash:
        _hashes[user_id] = int(access_hash)
    _hopeless.pop(user_id, None)
    try:
        db.set_user_peer(user_id, access_hash, name, username)
    except Exception as exc:                     # never let bookkeeping break a flow
        log.debug("could not remember peer %s: %s", user_id, exc)


def forget(user_id: int) -> None:
    """Drop a cached hash (Telegram said it is invalid)."""
    user_id = int(user_id)
    _hashes.pop(user_id, None)
    try:
        db._run("UPDATE users SET access_hash = NULL WHERE user_id = ?", (user_id,))
    except Exception:
        pass


def known_hash(user_id: int) -> int | None:
    _check_binding()
    user_id = int(user_id)
    if user_id in _hashes:
        return _hashes[user_id]
    try:
        row = db.user_peer(user_id)
    except Exception:
        row = None
    if row and row.get("access_hash"):
        _hashes[user_id] = int(row["access_hash"])
        return _hashes[user_id]
    return None


def _mark_hopeless(user_id: int, reason: str) -> None:
    _hopeless[int(user_id)] = reason
    try:
        db.mark_unreachable(user_id, reason)
    except Exception:
        pass


def hopeless_reason(user_id: int) -> str:
    _check_binding()
    return _hopeless.get(int(user_id), "")


async def peer_for(user_id: int):
    """The object Telethon needs to message this user.

    Order: our own ``InputPeerUser`` (instant, works on a fresh session) →
    Telethon's session cache → the raw id (last resort — Telethon may still know
    the entity from a dialog; if not, its own error explains what is wrong).
    """
    _check_binding()
    user_id = int(user_id)
    cached = known_hash(user_id)
    if cached:
        return InputPeerUser(user_id, cached)
    try:
        entity = await bot.get_input_entity(user_id)
    except ValueError as exc:
        # Telethon's own “I have never met this peer” — accurate and permanent
        # until the person contacts the bot again.
        reason = ("বট কখনো এই ইউজারের থেকে আপডেট পায়নি (ওরা /start দেয়নি) "
                  "— Telegram অ্যাক্সেস হ্যাশ দেয় না")
        log.debug("no cached peer for %s: %s", user_id, exc)
        _mark_hopeless(user_id, reason)
        return user_id                     # Telethon gets one more chance
    except Exception as exc:
        # Anything else (offline, a stubbed client, a transient RPC error) says
        # nothing about this user — do not write them off.
        log.debug("peer lookup for %s failed: %s", user_id, exc)
        return user_id
    if isinstance(entity, InputPeerUser):
        remember(user_id, entity.access_hash)
    return entity


def known_unreachable(user_id: int) -> str:
    """Why we already know this user cannot be messaged ('' when we don't)."""
    _check_binding()
    user_id = int(user_id)
    if user_id in _hashes:
        return ""
    if user_id in _hopeless:
        return _hopeless[user_id]
    try:
        row = db.user_peer(user_id)
    except Exception:
        return ""
    if row and not row.get("reachable") and not row.get("access_hash"):
        return row.get("last_error") or "বট এই ইউজারের কাছে পৌঁছাতে পারে না (বটে /start দেয়নি)"
    return ""


async def refresh(user_id: int):
    """Forget the stored hash and try once more (called after PEER_ID_INVALID)."""
    forget(user_id)
    return await peer_for(user_id)


async def learn_from_event(event) -> None:
    """Store the sender's access hash from any incoming update.

    Called by `app.handlers.peers`, which is registered *first*: every message,
    button tap or channel post teaches us one more reachable user. Costs nothing
    when the sender is already known (Telethon answers from its own cache).
    """
    # Channels and groups carry a hash exactly like users do — the cache channel is
    # the one that matters most (see `media_cache._resolve_peer`).
    try:
        chat_id = event.chat_id
    except Exception:
        chat_id = None
    if isinstance(chat_id, int) and chat_id < 0:
        try:
            chat = await event.get_chat()
        except Exception:
            chat = None
        if chat is not None:
            remember_chat(chat_id, getattr(chat, "access_hash", None),
                          getattr(chat, "title", None), getattr(chat, "username", None),
                          "channel" if is_channel_id(chat_id) else "chat")

    try:
        sender = await event.get_sender()
    except Exception:
        return
    if not isinstance(sender, TgUser):
        return
    name = " ".join(part for part in (getattr(sender, "first_name", "") or "",
                                      getattr(sender, "last_name", "") or "") if part)
    remember(sender.id, getattr(sender, "access_hash", None),
             name or None, getattr(sender, "username", None))


def stats() -> dict:
    """Small summary for the dashboard / self-test."""
    _check_binding()
    try:
        info = db.reachability()
    except Exception:
        info = {"total": 0, "reachable": 0, "missing_hash": 0}
    info["cached_here"] = len(_hashes)
    info["unreachable_here"] = len(_hopeless)
    return info
