"""Shared UI helpers used by both the user and the admin handlers."""
from __future__ import annotations

from telethon import events
from telethon.errors import UserNotParticipantError
from telethon.tl.custom import Button
from telethon.tl.functions.channels import GetParticipantRequest

from app import config as cfg, keyboards, runtime, texts
from app.logger import log
from app.runtime import bot
from app.services import access, forcejoin
from app.services.sender import FLOOD, NO_SESSION, deliver
from app.services.telegram import safe_call
from app.storage import db
from app.utils import button_data, esc

Event = events.common.EventCommon


async def render(event, text: str, buttons=None, edit: bool = False):
    """Show a screen. Never lets an edit/show hiccup leave the user with nothing.

    Order: edit the message we are on → send a new message → plain text without
    buttons. `safe_call` retries network hiccups, which is what used to make the
    store list "sometimes not appear" on a bad connection.
    """
    if edit:
        try:
            return await safe_call(event.edit, text, buttons=buttons, link_preview=False,
                                   what="edit", retries=1, raise_after_retries=False)
        except Exception as exc:
            log.debug("edit failed, sending a new message instead: %s", exc)
    try:
        return await safe_call(event.respond, text, buttons=buttons, link_preview=False,
                               what="respond", retries=2, raise_after_retries=False)
    except Exception as exc:
        log.warning("render could not show the screen: %s", exc)
    # Last resort: plain text, no buttons — the user always gets an answer.
    try:
        return await event.respond(text)
    except Exception as exc:
        log.error("render failed completely: %s", exc)
        return None


def home_reply_keyboard():
    """Bottom keyboard that is always there, so a user can never get stuck.

    Reply keyboards work even when inline buttons fail (old message deleted,
    FloodWait on edits, …) — one tap and the menu is back.
    """
    try:
        from telethon.tl.custom import Button
    except Exception:  # pragma: no cover
        return None
    return [
        ["🏠 Store list", "🔍 Search"],
        ["💎 My access", "🆘 Help"],
    ]


async def push_reply_keyboard(event, text: str | None = None):
    """Show the persistent bottom keyboard (silently, on first contact)."""
    try:
        from telethon.tl.custom import Button
        return await event.respond(
            text or "নিচের বাটনগুলো সব সময় কাজ করবে 👇",
            buttons=[[Button.text("🏠 Store list", resize=True), Button.text("🔍 Search", resize=True)],
                     [Button.text("💎 My access", resize=True), Button.text("🆘 Help", resize=True)]],
        )
    except Exception as exc:
        log.debug("reply keyboard failed: %s", exc)
        return None


async def notify_join(event, channel: str) -> None:
    """Ask the user to join — handles @username *and* private invite links."""
    link = forcejoin.join_url(channel)
    note = forcejoin.note()
    text = texts.FORCE_JOIN + (f"\n\n{esc(note)}" if note else "")
    buttons = [[Button.url("📢 চ্যানেলে জয়েন করুন", link)]] if link else None
    await event.respond(text, buttons=buttons)


async def require_membership(event, user_id: int) -> bool:
    """Force-join gate with a 10 minute cache, so heavy browsing does not
    hammer Telegram (and trip a FloodWait).

    Works with @username, a private invite link or a channel id; if membership
    cannot be verified we let the user through rather than locking them out.
    """
    channel = forcejoin.switch()
    if not channel or access.is_admin(user_id):
        return True
    cached = runtime.cached_join(user_id)
    if cached is not None:
        if not cached:
            await notify_join(event, channel)
        return cached
    result = await forcejoin.is_member(user_id)
    if result is None:                       # could not check → fail open
        return True
    runtime.mark_join(user_id, result)
    if not result:
        await notify_join(event, channel)
    return result


async def show_store(event, store: dict, page: int = 0, edit: bool = True):
    from app.handlers.extras import store_header, sorted_files
    user_id = event.sender_id
    files = sorted_files(store["id"], user_id)
    total = len(files)
    page = max(0, min(page, max(0, (total - 1) // cfg.CONTENT_PAGE_SIZE)))
    start = page * cfg.CONTENT_PAGE_SIZE
    page_files = files[start:start + cfg.CONTENT_PAGE_SIZE]
    stats = db.store_stats(store["id"])
    subscribed = db.is_sub(store["id"], user_id)
    buttons = keyboards.store_content(store, page_files, page, total, subscribed)
    buttons.extend(view_extra_rows(store, user_id, page_files))

    header = store_header(store, user_id, total, stats["views"], page)
    if not page_files:
        header += "\n\n<i>এই স্টোরে এখন কিছু নেই — পরে আবার দেখুন।</i>"
    return await render(event, header, buttons, edit=edit)


def view_extra_rows(store: dict, user_id: int, page_files: list[dict]) -> list[list]:
    """Sort controls, share card, favorites and request shortcuts."""
    from telethon.tl.custom import Button
    from app.handlers.extras import SORT_MODES
    rows = [
        [Button.inline(SORT_MODES.get(db.user_sort(user_id), SORT_MODES["default"]),
                       f"st:{store['id']}"),
         Button.inline("🔗 শেয়ার", f"sh:{store['id']}")],
        [Button.inline("🙋 কনটেন্ট চাই", f"rq:{store['id']}")],
    ]
    for file_row in page_files[:2]:
        rows.append([Button.inline(f"⭐ {file_row['name'][:20]}", f"fv:{file_row['id']}")])
    return rows


async def deliver_to_user(event, user_id: int, file_row: dict,
                          store: dict | None = None) -> bool:
    if file_row is None:
        await event.respond(texts.NOT_FOUND)
        return False
    store = store or db.store(file_row["store_id"])
    if store is None:
        await event.respond(texts.NOT_FOUND)
        return False
    if not access.has_access(store, user_id):
        await event.respond(texts.access_denied(store["name"]))
        return False
    if not await require_membership(event, user_id):
        return False

    result = await deliver(event.chat_id, file_row)
    if result.ok:
        return True

    if result.reason == NO_SESSION:
        message = texts.SESSION_NEEDED
    elif result.reason == FLOOD:
        message = texts.FLOOD_WAIT
    else:
        message = texts.NOT_FOUND
    if access.is_admin(user_id) and result.detail:
        message += f"\n<code>{esc(result.detail)}</code>"
    await event.respond(message)
    return False


async def strip_button(event, data: bytes) -> None:
    """Remove an already delivered file's button from the list message."""
    try:
        message = await event.get_message()
    except Exception:
        return
    if message is None or not message.buttons:
        return
    rows = []
    for row in message.buttons:
        keep = [button for button in row if button_data(button) != data]
        if keep:
            rows.append(keep)
    try:
        if rows:
            await event.edit(buttons=rows)
        else:
            await event.edit("✅ <b>You've received everything in this list!</b>")
    except Exception as exc:
        log.debug("could not update buttons: %s", exc)
