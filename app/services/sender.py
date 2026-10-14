"""File delivery service.

Two strategies, tried in order:

1. **Direct** — the bot itself can read the source message (it is an admin in the
   channel, or the file was uploaded to the bot). Zero session usage, fastest.
2. **JIT forwarding** — a userbot session forwards the message into the bot's DM
   and we catch it as a live update. Bots cannot fetch history, so we subscribe
   *before* forwarding. The temporary copy is deleted right after sending, so the
   bot DM no longer fills up with duplicates.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from telethon.errors import FloodWaitError

from app import config as cfg
from app.logger import log
from app import runtime
from app.runtime import (bot, bot_entity_cache, jit_locks, session_owners,
                         user_clients, wait_for_forward)
from app.storage import db
from app.services.telegram import safe_call

OK, NOT_FOUND, NO_SESSION, FLOOD, ERROR = "ok", "not_found", "no_session", "flood", "error"
BLOCKED, MISSING = "blocked", "missing"

#: Telegram error fragments that mean “this user can never be reached”.
BLOCKED_HINTS = ("blocked by the user", "user is deactivated", "bot was kicked",
                 "chat not found", "peer id invalid", "cannot find any entity",
                 "bot can't initiate conversation")


def classify_failure(detail: str) -> str:
    """Map a raw Telegram error onto a reason the broadcast queue understands."""
    low = (detail or "").lower()
    if low.startswith("source:"):
        # Problem reading OUR copy of the file — says nothing about the recipient.
        return MISSING
    if "flood" in low or "too many requests" in low:
        return FLOOD
    if any(hint in low for hint in BLOCKED_HINTS):
        return BLOCKED
    if "not found" in low or "deleted" in low:
        return MISSING
    return ERROR


@dataclass
class Delivery:
    ok: bool
    reason: str = OK
    detail: str = ""


# (chat, message) -> (expires_at, message): the mirrored copy is read once per few minutes,
# not once per recipient - this is what makes a broadcast fast.
_SOURCE_CACHE: dict[tuple[int, int], tuple[float, object]] = {}


async def _send_media(chat_id: int, media, caption_text: str, buttons=None) -> None:
    """Send media with forward-protection.

    Note: `send_file(..., noforwards=True)` is silently ignored by Telethon (it
    accepts **kwargs but never forwards them), so we build the request ourselves
    and fall back to `send_file` only if that fails.
    """
    try:
        from telethon import utils
        from telethon.extensions import html
        from telethon.tl import functions

        peer = await safe_call(bot.get_input_entity, chat_id, what="get_input_entity", retries=1)
        input_media = utils.get_input_media(media)
        text, entities = html.parse(caption_text or "")
        if buttons:
            from app.utils import button_rows
            reply_markup = button_rows(buttons)
            if reply_markup is not None:
                await safe_call(
                    bot, functions.messages.SendMediaRequest(
                        peer=peer, media=input_media, message=text,
                        entities=entities, noforwards=True, reply_markup=reply_markup,
                    ),
                    what="send_media(noforwards+buttons)",
                )
                return
        await safe_call(
            bot, functions.messages.SendMediaRequest(
                peer=peer, media=input_media, message=text,
                entities=entities, noforwards=True,
            ),
            what="send_media(noforwards)",
        )
        return
    except Exception as exc:
        log.debug("noforwards send failed (%s) — falling back to send_file", exc)

    await safe_call(
        bot.send_file, chat_id, media,
        caption=caption_text or None, buttons=buttons or None, what="send_file",
    )


async def _try_direct(file_row: dict, chat_id: int, caption_text: str,
                      buttons=None) -> tuple[bool, str]:
    """Bot-side send. Tries the mirror (best) and then the original location.

    Reading the mirror first is what makes a broadcast to thousands of users
    cheap: one message, thousands of plain sends, zero userbot traffic.

    Returns `(sent, last_error)` — the error text is what lets a broadcast tell
    “blocked by the user” apart from “file is gone” afterwards.
    """
    from app.services.media_cache import mirror_targets

    last_error = ""
    for source_chat, source_msg in mirror_targets(file_row):
        if not source_msg:
            continue
        try:
            key = (int(source_chat), int(source_msg))
            hit = _SOURCE_CACHE.get(key)
            if hit and hit[0] > time.time():
                msg = hit[1]
            else:
                msg = await safe_call(bot.get_messages, source_chat, ids=source_msg,
                                      what="get_messages", retries=1)
                if msg and getattr(msg, "media", None):
                    _SOURCE_CACHE[key] = (time.time() + 300, msg)
                else:
                    _SOURCE_CACHE.pop(key, None)
        except Exception as exc:
            last_error = ("source: " + str(exc))[:200]
            log.debug("direct fetch failed for file %s at %s/%s: %s",
                      file_row["id"], source_chat, source_msg, exc)
            continue
        if not msg or not getattr(msg, "media", None):
            if file_row.get("mirror_msg") and int(source_chat) == int(file_row.get("mirror_chat") or 0):
                db.clear_mirror(file_row["id"])       # mirror is gone — re-create later
            last_error = last_error or "source: media not available"
            continue
        try:
            await _send_media(chat_id, msg.media, caption_text, buttons)
        except FloodWaitError:
            raise
        except Exception as exc:
            last_error = str(exc)[:200]
            if classify_failure(last_error) == BLOCKED:
                # No point walking the other sources: the *user* is unreachable.
                return False, last_error
            log.debug("send of %s to %s failed: %s", file_row["id"], chat_id, exc)
            continue
        db.bump_views(file_row["id"])
        return True, ""
    return False, last_error


async def ensure_ready(file_row: dict) -> dict:
    """Make the file sendable (creates the one-time mirror if needed)."""
    from app.services.media_cache import ensure_mirror
    if not file_row:
        return file_row
    if file_row.get("mirror_msg"):
        return file_row
    store = db.store(file_row["store_id"])
    return await ensure_mirror(file_row, store)


async def _try_jit(file_row: dict, chat_id: int, caption_text: str, buttons=None) -> Delivery:
    store = db.store(file_row["store_id"])
    if not store:
        return Delivery(False, NOT_FOUND, "store removed")
    admin_id = store["admin_id"]
    client = user_clients.get(admin_id)
    if client is None:
        return Delivery(False, NO_SESSION, "no session for this store owner")
    if not runtime.bot_username:
        return Delivery(False, ERROR, "bot username not resolved yet")

    try:
        async with jit_locks[admin_id]:
            # Resolve the bot entity once per session — resolving on every click
            # is what used to trigger username FloodWaits.
            if admin_id not in bot_entity_cache:
                bot_entity_cache[admin_id] = await safe_call(
                    client.get_entity, runtime.bot_username, what="get_entity")
            bot_entity = bot_entity_cache[admin_id]

            fetch_peer = session_owners.get(admin_id, admin_id)
            waiter = asyncio.ensure_future(wait_for_forward(fetch_peer))
            try:
                forwarded = await safe_call(
                    client.forward_messages,
                    bot_entity, file_row["msg_id"], file_row["chat_id"],
                    what="forward_messages",
                )
            except Exception:
                waiter.cancel()
                raise
            incoming = await waiter

            if not incoming or not getattr(incoming, "media", None):
                return Delivery(False, ERROR, "forwarded copy never arrived")

            try:
                await _send_media(chat_id, incoming.media, caption_text, buttons)
                # Remember this copy: from now on the bot can serve the file
                # itself and the userbot never has to forward it again.
                try:
                    if not file_row.get("mirror_msg"):
                        db.set_mirror(file_row["id"], event_chat_id(incoming), incoming.id)
                except Exception as exc:
                    log.debug("could not remember the mirrored copy: %s", exc)
            finally:
                # Only the *extra* outgoing forward (same chat, second copy) is
                # deleted. The first copy stays as the cache — deleting it was
                # what made the bot forward the same video again and again.
                if cfg.CLEANUP_JIT_COPY and forwarded is not None \
                        and getattr(forwarded, "id", None) != getattr(incoming, "id", None):
                    try:
                        await safe_call(forwarded.delete, what="delete_copy",
                                        raise_after_retries=False)
                    except Exception:
                        pass
            db.bump_views(file_row["id"])
            return Delivery(True)
    except FloodWaitError:
        return Delivery(False, FLOOD)
    except Exception as exc:
        log.warning("JIT delivery failed for file %s: %s", file_row["id"], exc)
        return Delivery(False, ERROR, str(exc)[:200])


async def send_text(chat_id: int, text: str, buttons=None, link_preview: bool = False):
    """Send a plain message as the bot. Never raises — a broken request must not
    take down the flow that was only trying to explain something."""
    if not text:
        return None
    try:
        kwargs = {"link_preview": link_preview}
        if buttons is not None:
            kwargs["buttons"] = buttons
        return await safe_call(bot.send_message, chat_id, text, what="send_text", retries=2,
                               **kwargs)
    except Exception as first:
        log.debug("send_text with buttons failed (%s) — retrying without buttons", first)
        try:
            return await safe_call(bot.send_message, chat_id, text, what="send_text",
                                   retries=1, link_preview=False)
        except Exception as exc:
            log.warning("send_text failed for %s: %s", chat_id, exc)
            return None


async def edit_text(message, text: str, buttons=None):
    """Edit a message we own; falls back to sending a fresh one."""
    try:
        return await safe_call(message.edit, text, buttons=buttons, link_preview=False,
                               what="edit_text", retries=1, raise_after_retries=False)
    except Exception as exc:
        log.debug("edit_text failed (%s) — sending instead", exc)
        try:
            return await send_text(message.chat_id, text, buttons=buttons)
        except Exception:
            return None


def event_chat_id(message) -> int:
    """The chat a message really lives in (works for forwarded copies too)."""
    for attr in ("chat_id", "peer_id"):
        value = getattr(message, attr, None)
        if isinstance(value, int):
            return value
        if value is not None:
            for sub in ("user_id", "chat_id", "channel_id"):
                inner = getattr(value, sub, None)
                if inner:
                    return int(inner) if sub != "user_id" else int(inner)
    return 0


async def deliver(chat_id: int, file_row: dict, caption_text: str | None = None,
                  buttons=None) -> Delivery:
    """Send one stored file to `chat_id`.

    Order: bot-side (mirror first) → create the mirror once → live forward as the
    very last resort. `buttons` lets callers attach inline URL buttons (the
    “online button” used by broadcasts and channel posts).
    """
    if not file_row:
        return Delivery(False, NOT_FOUND)
    if caption_text is None:
        caption_text = runtime.caption()
    caption = caption_text or ""

    last_error = ""
    try:
        sent, last_error = await _try_direct(file_row, chat_id, caption, buttons)
        if sent:
            return Delivery(True)
        if classify_failure(last_error) == BLOCKED:
            return Delivery(False, BLOCKED, last_error)
    except FloodWaitError:
        return Delivery(False, FLOOD)
    except Exception as exc:
        last_error = str(exc)[:200]
        log.debug("direct send failed for file %s: %s", file_row["id"], exc)

    # The copy exists but the bot cannot open its channel ("Could not find the input
    # entity"): re-add the bot as admin, wake the channel for it, try once more.
    if file_row.get("mirror_msg") and "input entity" in last_error.lower():
        from app.services import media_cache
        if await media_cache.heal_storage_access():
            try:
                sent, error = await _try_direct(file_row, chat_id, caption, buttons)
                if sent:
                    return Delivery(True)
                last_error = error or last_error
            except FloodWaitError:
                return Delivery(False, FLOOD)
            except Exception as exc:
                last_error = str(exc)[:200]

    # Not readable yet — create the mirror once, then send from it.
    try:
        updated = await ensure_ready(file_row)
        if updated.get("mirror_msg"):
            sent, error = await _try_direct(updated, chat_id, caption, buttons)
            if sent:
                return Delivery(True)
            last_error = error or last_error
            if classify_failure(last_error) == BLOCKED:
                return Delivery(False, BLOCKED, last_error)
    except FloodWaitError:
        return Delivery(False, FLOOD)
    except Exception as exc:
        last_error = str(exc)[:200]
        log.debug("mirror path failed for file %s: %s", file_row["id"], exc)

    if not cfg.ALLOW_JIT_FORWARD:
        # Forwarding through the owner's account is what spammed their chats.
        from app.services.media_cache import mirror_problem
        reason = mirror_problem(file_row["id"])
        detail = f"source: {reason}" if reason else (
            last_error or "source: ফাইলের কোনো কপি নেই (ক্যাশ চ্যানেল তৈরি হয়নি)")
        return Delivery(False, MISSING, detail)

    result = await _try_jit(file_row, chat_id, caption, buttons)
    if result.ok or result.reason in (NOT_FOUND, NO_SESSION, FLOOD, BLOCKED):
        return result
    # Retry the direct path once more: the userbot may have just pulled the file
    # into the bot's reach (e.g. right after the session reconnected).
    try:
        sent, error = await _try_direct(file_row, chat_id, caption, buttons)
        if sent:
            return Delivery(True)
        if error:
            result.detail = f"{result.detail} | {error}".strip(" |")
    except Exception:
        pass
    if not result.detail and last_error:
        result.detail = last_error
    return result


async def deliver_file_id(chat_id: int, file_id: int, caption_text: str | None = None,
                          buttons=None) -> Delivery:
    row = db.file(file_id)
    if not row:
        return Delivery(False, NOT_FOUND)
    return await deliver(chat_id, row, caption_text, buttons=buttons)


async def deliver_many(chat_id: int, file_rows: list[dict],
                       caption_text: str | None = None, delay: float | None = None,
                       buttons=None) -> int:
    sent = 0
    for row in file_rows:
        result = await deliver(chat_id, row, caption_text, buttons=buttons)
        if result.ok:
            sent += 1
        await asyncio.sleep(delay if delay is not None else 0.4)
    return sent


def mirror_ready() -> dict:
    from app.services.media_cache import stats
    return stats()
