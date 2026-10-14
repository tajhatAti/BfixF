"""User facing handlers: browsing stores, deep links, search, referrals."""
from __future__ import annotations

import asyncio
import time

from telethon import events
from telethon.tl.custom import Button

from app import config as cfg, i18n, keyboards, runtime, texts, ui
from app.handlers.router import route
from app.handlers import state
from app.logger import log
from app.runtime import bot, spawn
from app.services import access, flow
from app.services.sender import deliver
from app.storage import db
from app.utils import esc, safe_int

from app.payloads import (KIND_CODE, KIND_FILE, KIND_LEGACY_BATCH, KIND_LINK,
                          KIND_REFERRAL, KIND_SLUG, KIND_STORE, resolve_payload)


async def apply_referral(user_id: int, referrer_id: int, is_new: bool) -> None:
    """Reward the inviter once, and only for genuinely new users."""
    if not referrer_id or referrer_id == user_id or not is_new:
        return
    if not db.set_referrer(user_id, referrer_id):
        return
    for cfg_row in db.all_referral_cfgs():
        store = db.store(cfg_row["store_id"])
        if store is None or not cfg_row["days"]:
            continue
        access.grant_days(store["id"], referrer_id, cfg_row["days"], source="referral")
        try:
            await bot.send_message(
                referrer_id,
                f"🎁 <b>Referral reward!</b>\n"
                f"A friend joined with your link — you got <b>{cfg_row['days']} days</b> "
                f"of <b>{esc(store['name'])}</b>.",
            )
        except Exception as exc:
            log.debug("could not notify referrer %s: %s", referrer_id, exc)
    db.mark_rewarded(user_id)


async def deliver_link_token(event, user_id: int, token: str) -> None:
    """Kept for old call sites — the real work lives in `flow.open_link`
    (click limits, per-store gate and analytics all happen there)."""
    await flow.open_link(event, token)


async def handle_payload(event, user_id: int, payload: str, is_new: bool) -> bool:
    """Returns True when the payload fully handled the message."""
    resolved = resolve_payload(payload)
    if resolved is None:
        return False
    kind, value = resolved

    if kind == KIND_REFERRAL:
        await apply_referral(user_id, int(value), is_new)
        return False                      # fall through to the normal home screen

    if kind == KIND_FILE:
        # One tap: the video goes out right here (gate first, if a channel is set).
        await flow.open_file(event, int(value), ref="start")
        return True

    if kind == KIND_STORE:
        if not await flow.open_store(event, int(value), 0, edit=False):
            await event.respond("⚠️ This store no longer exists.")
        return True

    if kind == KIND_SLUG:
        store = db.store_by_slug(str(value))
        if store is None:
            await event.respond("⚠️ This store link is invalid or the store was deleted.")
            return True
        await flow.open_store(event, store["id"], 0, edit=False)
        return True

    if kind == KIND_LINK:
        await flow.open_link(event, str(value))
        return True

    if kind == KIND_LEGACY_BATCH:
        file_rows = [db.file_by_code(part) for part in str(value).split("_AND_")]
        file_rows = [row for row in file_rows if row]
        if not file_rows:
            await event.respond(texts.NOT_FOUND)
            return True
        if not (await flow.gate_check(user_id, file_rows[0]["store_id"]))["ok"]:
            await flow.show_join_gate(event, user_id, file_rows[0],
                                      (await flow.gate_check(user_id, file_rows[0]["store_id"]))["missing"])
            return True
        await event.respond(f"📦 পাঠাতে শুরু করলাম — মোট <b>{len(file_rows)}</b> টি ফাইল…")
        for row in file_rows:
            state = await flow.open_file(event, row["id"], ref="batch")
            if state == flow.GATE_JOIN:
                return True
            await asyncio.sleep(0.4)
        return True

    if kind == KIND_CODE:
        file_row = db.file_by_code(str(value)) or db.file_by_uid(str(value))
        if file_row:
            await flow.open_file(event, file_row["id"], ref="code")
            return True
        store = db.store_by_slug(str(value))
        if store:
            await flow.open_store(event, store["id"], 0, edit=False)
            return True
        link = db.link(str(value))
        if link:
            await flow.open_link(event, str(value))
            return True
        await event.respond("⚠️ This link is invalid or the content was removed.")
        return True

    return False


async def show_home(event, user_id: int, edit: bool = False):
    """The main screen. Written so it can *never* fail silently.

    When something goes wrong the user still gets a plain message with a way
    back — that was the real reason a store list sometimes "did not come".
    """
    from app import i18n

    # Banned check (never let it break the home screen)
    try:
        if db.is_banned(user_id) and not access.is_admin(user_id):
            await ui.render(event, "🚫 <b>আপনি ব্যান হয়েছেন</b>\nঅ্যাডমিনের সাথে যোগাযোগ করুন।",
                            [[Button.inline(i18n.t(user_id, "contact_button"), "ct:0")]], edit=edit)
            return
    except Exception as exc:
        log.debug("ban check skipped: %s", exc)

    try:
        stores = db.all_stores()
    except Exception as exc:
        log.exception("could not read the store list: %s", exc)
        stores = []

    if not stores:
        await ui.render(
            event,
            "👋 <b>স্বাগতম!</b>\n\nএখনো কোনো স্টোর তৈরি হয়নি — অ্যাডমিন শীঘ্রই "
            "কনটেন্ট যোগ করবেন।\nনিচের বাটনে চাপ দিয়ে অ্যাডমিনকে জানাতে পারেন।",
            [[Button.inline(i18n.t(user_id, "contact_button"), "ct:0")],
             [Button.inline(i18n.t(user_id, "language_button"), "lang")]],
            edit=edit,
        )
        return

    # Custom welcome if the admin set one
    banner = ""
    try:
        if cfg.ADMIN_IDS:
            banner = db.welcome_text(cfg.ADMIN_IDS[0])
    except Exception as exc:
        log.debug("welcome text unavailable: %s", exc)
    if not banner:
        banner = i18n.t(user_id, "home_banner")

    buttons = keyboards.user_store_list(stores)
    buttons.extend([
        [Button.inline(i18n.t(user_id, "search_all"), "gs"),
         Button.inline(i18n.t(user_id, "my_access_button"), "mya:0")],
        [Button.inline(i18n.t(user_id, "invite_button"), "rf"),
         Button.inline(i18n.t(user_id, "help_button"), "hp")],
        [Button.inline(i18n.t(user_id, "request_button"), "rq:0"),
         Button.inline(i18n.t(user_id, "contact_button"), "ct:0")],
        [Button.inline(i18n.t(user_id, "language_button"), "lang")],
    ])
    await ui.render(event, banner, buttons, edit=edit)

    # Keep the bottom keyboard present, so the menu is always one tap away.
    if not edit:
        try:
            await ui.push_reply_keyboard(event)
        except Exception as exc:
            log.debug("reply keyboard skipped: %s", exc)



# ------------------------------------------------------------------- commands
@bot.on(events.NewMessage(func=lambda e: e.is_private,
                          pattern=r"^/(start|admin|admin@\w+)(\s|$)"))
async def start_handler(event: events.NewMessage.Event) -> None:
    user_id = event.sender_id
    sender = await event.get_sender()
    db.log_event("start", user_id, None, None,
                 (event.raw_text or "").split(maxsplit=1)[1][:40]
                 if len((event.raw_text or "").split(maxsplit=1)) > 1 else "start")
    is_new = db.touch_user(user_id,
                           getattr(sender, "first_name", None),
                           getattr(sender, "username", None))
    if is_new:
        # 🔔 “নতুন ইউজার এলেই জানাও” — fire and forget, never slow down /start
        from app.services import alerts
        spawn(alerts.new_user(user_id, getattr(sender, "first_name", "") or "",
                              getattr(sender, "username", "") or ""))

    parts = (event.raw_text or "").strip().split(maxsplit=1)
    command = parts[0].split("@")[0].lower()
    payload = parts[1].strip() if len(parts) > 1 else ""

    if payload and await handle_payload(event, user_id, payload, is_new):
        return

    if access.is_admin(user_id):
        from app.handlers.admin import send_panel     # local import avoids a cycle
        await send_panel(event)
        return

    if command == "/admin":
        return                                        # never leak the panel
    await show_home(event, user_id)


@route("cd:flow")
async def cancel_flow(event, rest: str) -> None:
    """The “✖️ বাতিল” button every question carries.

    Pressing any other button already drops a pending question (see
    `router.dispatch`), but this one is the one users actually look for.
    """
    state.clear(event.sender_id)
    await event.answer("✖️ বাতিল করা হলো — কিছুই সেভ হয়নি")
    try:
        await event.edit("✖️ <b>বাতিল করা হলো</b> — কিছুই সেভ হয়নি।", buttons=None)
    except Exception:
        await event.respond("✖️ <b>বাতিল করা হলো</b> — কিছুই সেভ হয়নি।")


@bot.on(events.NewMessage(func=lambda e: e.is_private, pattern=r"^/help"))
async def help_handler(event: events.NewMessage.Event) -> None:
    user_id = event.sender_id
    text = texts.HELP_TEXT
    if access.is_admin(user_id):
        text += "\n\n" + texts.ADMIN_HELP
    await event.respond(text)


@bot.on(events.NewMessage(func=lambda e: e.is_private, pattern=r"^/cancel"))
async def cancel_handler(event: events.NewMessage.Event) -> None:
    from app.handlers.state import clear
    clear(event.sender_id)
    await event.respond(texts.CANCELLED)


# ------------------------------------------------------------------- callbacks
@route("hp")
async def help_button(event, _rest: str) -> None:
    text = texts.HELP_TEXT
    if access.is_admin(event.sender_id):
        text += "\n\n" + texts.ADMIN_HELP
    await ui.render(event, text, [[Button.inline("🔙 Back", "bs:0")]], edit=True)
    await event.answer()


@route("rf")
async def referral_button(event, _rest: str) -> None:
    user_id = event.sender_id
    if not runtime.bot_username:
        await event.answer("Bot is still starting up — try again in a moment.", alert=True)
        return
    lines = [
        "🎁 <b>Invite &amp; Earn</b>",
        "",
        f"Your personal link:\n<code>https://t.me/{runtime.bot_username}?start=r{user_id}</code>",
        f"Friends joined with it: <b>{db.invite_count(user_id)}</b>",
        "",
    ]
    rewards = []
    for cfg_row in db.all_referral_cfgs():
        store = db.store(cfg_row["store_id"])
        if store and cfg_row["days"]:
            rewards.append(f"• A friend joins → <b>{cfg_row['days']} days</b> of <b>{esc(store['name'])}</b>")
    lines.append("<b>Rewards:</b>")
    lines.extend(rewards or ["• Ask the admin about current referral rewards."])
    await ui.render(event, "\n".join(lines),
                    [[Button.inline("🔙 Back", "bs:0")]], edit=True)
    await event.answer()


@route("bs:")
async def back_to_stores(event, rest: str) -> None:
    if not await ui.require_membership(event, event.sender_id):
        await event.answer()
        return
    await show_home(event, event.sender_id, edit=True)
    await event.answer()


@route("s:")
async def open_store(event, rest: str) -> None:
    user_id = event.sender_id
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("This store no longer exists.", alert=True)
        return
    if not await ui.require_membership(event, user_id):
        await event.answer()
        return
    if not access.has_access(store, user_id):
        from app.handlers.billing import paywall_buttons
        from app.services import billing
        stats = db.store_stats(store["id"])
        text = (
            i18n.t(user_id, "premium_locked", store=esc(store["name"]))
            + f"\n\n{i18n.t(user_id, 'items', n=stats['files'])}"
            + billing.upsell_text(store, user_id)
        )
        await ui.render(event, text, paywall_buttons(event, store, user_id), edit=True)
        await event.answer()
        return
    await flow.open_store(event, store["id"], 0, edit=True)
    await event.answer()


@route("sp:")
async def store_page(event, rest: str) -> None:
    store_id, _, page = rest.partition(":")
    store = db.store(safe_int(store_id))
    if store is None:
        await event.answer("This store no longer exists.", alert=True)
        return
    if not access.has_access(store, event.sender_id):
        await event.answer(texts.NO_ACCESS_ALERT, alert=True)
        return
    await ui.show_store(event, store, safe_int(page), edit=True)
    await event.answer()


@route("ss:")
async def search_prompt(event, rest: str) -> None:
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("This store no longer exists.", alert=True)
        return
    if not access.has_access(store, event.sender_id):
        await event.answer(texts.NO_ACCESS_ALERT, alert=True)
        return
    state.start_search(event.sender_id, store["id"])
    await event.respond(f"🔍 Send a keyword to search inside <b>{esc(store['name'])}</b>\n"
                        f"<i>(send /cancel to stop)</i>")
    await event.answer()


@route("sb:")
async def toggle_subscription(event, rest: str) -> None:
    store = db.store(safe_int(rest))
    user_id = event.sender_id
    if store is None:
        await event.answer("This store no longer exists.", alert=True)
        return
    if not access.has_access(store, user_id):
        # Bug fix: previously anyone could subscribe to a premium store's daily
        # drip and receive the paid files for free.
        await event.answer(texts.NO_ACCESS_ALERT, alert=True)
        return
    subscribed = db.toggle_sub(store["id"], user_id)
    await event.answer("🔔 You'll get daily updates from this store!"
                       if subscribed else "🔕 Unsubscribed from daily updates.", alert=True)


@route("f:")
async def receive_file(event, rest: str) -> None:
    user_id = event.sender_id
    file_row = db.file(safe_int(rest))
    if file_row is None:
        await event.answer("This file was removed.", alert=True)
        return
    state = await flow.open_file(event, file_row["id"], ref="button")
    if state == flow.GATE_JOIN:
        await event.answer("🔐 আগে চ্যানেল জয়েন করুন", alert=True)
        return
    if state != flow.GATE_OK:
        await event.answer("")
        return
    await event.answer("✅ ভিডিও পাঠানো হয়েছে!")
    await ui.strip_button(event, event.data)


@route("fj:")
async def join_confirm(event, rest: str) -> None:
    """“✅ আমি জয়েন করেছি” — re-check the channels and give the file right away."""
    parts = (rest or "").split(":")
    if len(parts) < 2:
        await event.answer("⚠️ পুরনো বাটন — লিংকটি আবার খুলুন", alert=True)
        return
    owner_id, file_id = safe_int(parts[0]), safe_int(parts[1])
    token = parts[2] if len(parts) > 2 else ""
    if owner_id and owner_id != event.sender_id:
        await event.answer("এই বাটনটি আপনার জন্য নয় 🙂", alert=True)
        return
    await event.answer("🔎 চেক করছি…")
    await flow.confirm_join(event, event.sender_id, file_id, token)


@route("fl:")
async def link_join_confirm(event, rest: str) -> None:
    """“✅ আমি জয়েন করেছি” on a link with its own channels — re-check, then deliver."""
    token = (rest or "").split(":")[0]
    if not token:
        await event.answer("⚠️ পুরনো বাটন — লিংকটি আবার খুলুন", alert=True)
        return
    await event.answer("🔎 চেক করছি…")
    state = await flow.open_link(event, token)
    if state == flow.GATE_JOIN:
        await event.answer("⛔️ এখনো সব চ্যানেল জয়েন করা হয়নি", alert=True)


@route("ul:")
async def open_limited_link(event, rest: str) -> None:
    """Close the loop of a limited link posted in a channel."""
    await event.answer("🔓 খুলছি…")
    state = await flow.open_link(event, rest.split(":")[0])
    if state == flow.GATE_JOIN:
        await event.answer("🔐 আগে চ্যানেল জয়েন করুন", alert=True)


# ------------------------------------------------- bottom (reply) keyboard (v2.3)
@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.out
                          and (e.raw_text or "").strip() in texts.REPLY_BUTTONS))
async def reply_menu_buttons(event) -> None:
    """Handle the four permanent bottom buttons.

    Registered on its own so a user never has to remember a command: even if
    inline buttons are broken (deleted message, FloodWait, old client), the
    bottom keyboard always brings the store list back.
    """
    from app.handlers import state
    user_id = event.sender_id
    label = (event.raw_text or "").strip()
    state.clear(user_id)                      # the menu button cancels any flow

    try:
        sender = await event.get_sender()
        db.touch_user(user_id, getattr(sender, "first_name", None),
                      getattr(sender, "username", None))
    except Exception:
        pass

    if label == texts.REPLY_HOME:
        await show_home(event, user_id)
        return
    if label == texts.REPLY_SEARCH:
        state.ask(user_id, "global_search")
        await event.respond("🔎 <b>সার্চ</b>\nযা খুঁজছেন লিখে পাঠান — সব স্টোরে খুঁজে দেব "
                            "(<i>/cancel লিখলে বাতিল</i>)।")
        return
    if label == texts.REPLY_ACCESS:
        from app.handlers.billing import my_access
        await my_access(event, "0")
        return
    if label == texts.REPLY_HELP:
        text = texts.HELP_TEXT
        if access.is_admin(user_id):
            text += "\n\n" + texts.ADMIN_HELP
        await event.respond(text,
                            buttons=[[Button.inline("🏠 স্টোর লিস্ট", "bs:0")]])
        return


@bot.on(events.NewMessage(func=lambda e: e.is_private,
                          pattern=r"^/(menu|home|stores|start@\w+)(\s|$)"))
async def menu_command(event) -> None:
    """`/menu` — always show the store list (works even if /start was lost)."""
    user_id = event.sender_id
    try:
        sender = await event.get_sender()
        db.touch_user(user_id, getattr(sender, "first_name", None),
                      getattr(sender, "username", None))
    except Exception:
        pass
    if access.is_admin(user_id) and (event.raw_text or "").strip().startswith("/menu"):
        from app.handlers.admin import send_panel
        await send_panel(event)
        return
    await show_home(event, user_id)
