"""Catch-all message handlers: pending input, search, uploads, JIT forwards.

They are written as small guards so a message can never be handled twice.
"""
from __future__ import annotations

import re
import time

from telethon import events
from telethon.tl.custom import Button

from app import config as cfg, keyboards, runtime, texts
from app.handlers import state as flow_state
from app.handlers.state import (ask, batch_mode, ctx as flow_ctx_state,
                               flow as remember_flow, link_gen, peek, take)
from app.logger import log
from app.runtime import bot, spawn
from app import i18n
from app.services import access, broadcast, flow, media_guard, scanner
from app.services.telegram import safe_call
from app.storage import db
from app.utils import esc, safe_int

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

#: pending-input flows that any user (not just an admin) may complete
USER_ACTIONS = {
    "contact_message",     # talking to the admin
    "payment_proof",       # sending the payment screenshot / txn id
    "coupon_code",         # redeeming a coupon
    "global_search",       # searching across stores
    "content_request",     # asking for a file that is missing
    "user_search",         # admin searching users (admin-only but handled here)
    "bulk_grant",          # admin bulk granting
    "bulk_grant_days",     # days for bulk grant
    "admin_file_search",   # admin searching files
    "ban_user",            # ban
    "unban_user",          # unban
    "set_welcome",         # welcome message
    "setting_edit",        # ব্যানেল/ওয়েবসাইট থেকে সেটিং বদলানো (অ্যাডমিন)
    "add_channel",         # চ্যানেল ম্যানেজমেন্ট
    "store_forcejoin",     # স্টোর-ভিত্তিক ফোর্স জয়েন
    "link_limit",          # লিমিটেড লিংকের ক্লিক সংখ্যা
}
KIND_MAP = (("video", "Video"), ("photo", "Photo"), ("audio", "Audio"),
            ("document", "Document"), ("voice", "Audio"))


# ------------------------------------------------------------------ JIT catch
@bot.on(events.NewMessage(incoming=True))
async def catch_jit_forward(event) -> None:
    """Bots cannot read history, so we catch the forwarded copy the instant it
    lands in the bot's DM."""
    if not event.media:
        return
    future = runtime.pending_forwards.get(event.sender_id)
    if future is not None and not future.done():
        runtime.recent_jit_ids.append(event.message.id)
        future.set_result(event.message)


async def ask_again(event, action: str, message: str, **ctx) -> None:
    """Re-arm a question after a wrong answer — but only a couple of times.

    The owner's complaint “যেকোনো টেক্সট দিলে ওটা বারে বারে সেভ হইতেছে” came from a
    question that stayed armed forever: every random message became the answer and
    overwrote the value. Now a flow gives up after a few tries and says so.
    """
    admin_id = event.sender_id
    tries = flow_state.bump_tries(admin_id, action)
    if tries >= flow_state.MAX_TRIES:
        flow_state.clear(admin_id)
        await event.respond(
            "⚠️ <b>বারবার ভুল হচ্ছে — কাজটি বাতিল করলাম।</b>\n\n"
            "কিছুই সেভ হয়নি। আবার শুরু করতে প্যানেলের বাটনে চাপ দিন।",
            buttons=flow_state.cancel_buttons())
        return
    ask(admin_id, action, **ctx)          # a fresh question resets the counter…
    flow_state.wrong_answers[(admin_id, action)] = tries   # …so put the count back
    left = flow_state.tries_left(admin_id, action)
    await event.respond(f"{message}\n\n<i>আর {left} বার চেষ্টা করা যাবে · "
                        f"বাতিল করতে নিচের বাটনে চাপ দিন</i>",
                        buttons=flow_state.cancel_buttons())


# --------------------------------------------------------------- media upload
def _kind_of(message) -> str:
    for attribute, kind in KIND_MAP:
        if getattr(message, attribute, None):
            return kind
    return "File"


@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.out))
async def media_upload(event) -> None:
    if not event.media:
        return
    admin_id = event.sender_id

    # The strict filter comes FIRST, before every other branch: a text message
    # with a link preview, a sticker or a random document must never be stored.
    kind, reason = media_guard.classify(event.message)
    if kind is None and not _has_admin_flow(admin_id):
        if access.is_admin(admin_id):
            # A question is open (or files are simply expected): the owner wanted an
            # alert when someone is “stuck” here, including himself on another device.
            if reason not in ("text",) or pending_cover_open(admin_id):
                spawn(_alert_stuck_upload(event, reason))
            await _text_guard_reply(event, reason)
        return

    # Admin is setting a store cover image.
    pending_cover = peek(admin_id)
    if pending_cover and pending_cover["action"] == "store_cover" and access.is_admin(admin_id):
        take(admin_id)
        store_id = pending_cover["ctx"].get("store_id")
        store = db.store(safe_int(store_id))
        if store is None:
            await event.respond("⚠️ Store not found.")
            return
        db.update_store(store["id"], cover=f"{event.chat_id}:{event.message.id}")
        await event.respond(f"🖼 Cover set for <b>{esc(store['name'])}</b>.")
        # Return to store settings so the admin sees the change immediately.
        try:
            from app.services import billing as billing_svc
            stats = db.store_stats(store["id"])
            plans = db.plans(store["id"])
            drip = db.drip(store["id"])
            sales = billing_svc.store_sales(store["id"])
            lines = [
                f"🛠 <b>{esc(store['name'])}</b>",
                f"📂 {stats['files']} files · 👁 {stats['views']} views · 💎 {stats['grants']} granted",
                f"🔔 {stats['subs']} drip subscribers · 💰 {billing_svc.price_text(sales['revenue'])} earned",
                f"💎 Plans: {len(plans)} · 📅 Drip: "
                f"{'on ' + str(drip['count']) + '/day at ' + drip['send_time'] if drip and drip['enabled'] else 'off'}",
                f"🌐 Lock: {'🔒 premium' if store['is_premium'] else '🆓 free for everyone'}",
                "",
                f"📝 {esc((store.get('description') or '(no description)')[:160])}",
                f"🖼 Cover: {'✅ set' if store.get('cover') else 'not set'} (updated just now)",
            ]
            # Re-fetch store to get new cover value
            store = db.store(store["id"])
            buttons = [
                [Button.inline("✏️ Rename store", f"ssn:{store['id']}"),
                 Button.inline("🔒/🌐 Toggle premium", f"sst:{store['id']}")],
                [Button.inline("📝 Set description", f"desc:{store['id']}"),
                 Button.inline("🖼 Set cover", f"cover:{store['id']}")],
                [Button.inline("💎 Plans & pricing", f"apl:{store['id']}")],
                [Button.inline("🗂 Files", f"fms:{store['id']}"), Button.inline("👥 Access", f"sga:{store['id']}")],
                [Button.inline("🔗 Share link", f"shr:{store['id']}"), Button.inline("👁 Preview", f"s:{store['id']}")],
                [Button.inline("🔙 Back to panel", "adm:back")],
            ]
            await event.respond("\n".join(lines), buttons=buttons)
        except Exception:
            pass
        return

    # A buyer sending the payment screenshot: treat it as proof, not an upload.
    pending = peek(admin_id)
    if pending and pending["action"] == "payment_proof":
        take(admin_id)
        order_id = pending["ctx"].get("order_id")
        order = db.order(order_id) if order_id else None
        if order:
            db.update_order_proof(order["id"], "photo")
            await event.respond("🧾 পেমেন্টের স্ক্রিনশট পেয়েছি — অ্যাডমিন যাচাই করছেন।")
            await notify_admins_about_order(order)
        return

    if not access.is_admin(admin_id):
        return
    if event.message.id in runtime.recent_jit_ids:
        return                                    # this is a JIT copy, not an upload

    store_id = db.active_store_id(admin_id)
    store = db.store(store_id) if store_id else None
    if store is None:
        await event.respond("⚠️ আগে প্যানেল থেকে একটা <b>অ্যাকটিভ স্টোর</b> বেছে নিন (/admin)।")
        return

    count = db.store_stats(store["id"])["files"]
    file_id = db.add_file(
        store_id=store["id"],
        name=media_guard.display_name(event.message, kind, count + 1),
        kind=kind,
        chat_id=event.chat_id,
        msg_id=event.message.id,
        size=getattr(getattr(event.message, "file", None), "size", None),
        duration=media_guard.duration_of(event.message),
    )
    if file_id is None:
        await event.respond("ℹ️ এই ফাইলটি অলরেডি স্টোরে আছে — আবার সেভ করা হয়নি।")
        return
    db.set_file_uid(file_id, f"{file_id}x{int(time.time()) % 100000:05d}")
    db.log_event("add_file", admin_id, store["id"], file_id, "upload", kind)

    # Mirror it in the background: doing it now means every later click and every
    # broadcast is a plain bot send — no forward/delete churn, no FloodWait.
    spawn(mirror_soon(file_id))

    if admin_id in flow_state.link_add_mode:
        mode = flow_state.link_add_mode[admin_id]
        db.link_add_files(mode["token"], [file_id])
        mode["added"] = int(mode.get("added", 0)) + 1
        await event.respond(
            f"➕ <b>লিংকে যোগ হয়েছে</b> — এই লিংকে এখন {len(db.link_file_ids(mode['token']))} টি "
            f"({kind})। আরও পাঠান, শেষ হলে /done দিন।")
        return

    if admin_id in batch_mode:
        batch_mode[admin_id].append(file_id)
        await event.respond(
            f"➕ <b>যোগ হয়েছে</b> — এই ব্যাচে {len(batch_mode[admin_id])} টি "
            f"({kind})। সব পাঠানো শেষ হলে /done দিন।")
        return

    link = f"https://t.me/{runtime.bot_username}?start=f{file_id}" if runtime.bot_username else ""
    buttons = []
    if link:
        buttons.append([Button.url("🔗 লিংক কপি করার জন্য খুলুন", link)])
    buttons.append([Button.inline("📛 নাম বদলান", f"rf:{file_id}"),
                    Button.inline("🗂 ফাইল লিস্ট", f"fms:{store['id']}")])
    await event.respond(
        f"✅ <b>{esc(store['name'])}</b> স্টোরে সেভ হয়েছে — <b>{kind}</b>\n"
        f"নাম: {esc(media_guard.display_name(event.message, kind, count + 1))}\n"
        + (f"🔗 <code>{link}</code>\n" if link else "")
        + "\n<i>এখনই চেক হচ্ছে যে সব ইউজার সরাসরি এই ফাইল পাবে কি না (একবার ক্যাশ হবে)।</i>",
        buttons=buttons,
    )


def pending_cover_open(admin_id: int) -> bool:
    """True while the bot is waiting for *media* from this admin."""
    row = peek(admin_id)
    return bool(row) and row.get("action") in ("store_cover", "upload_media")


def _has_admin_flow(admin_id: int) -> bool:
    pending = peek(admin_id)
    return bool(pending) and pending.get("action") in ("store_cover", "payment_proof")


def _is_add_channel_forward(admin_id: int) -> bool:
    """A forwarded post while the “add channel” flow is waiting."""
    pending = peek(admin_id)
    return bool(pending) and pending.get("action") == "add_channel"


_last_text_nudge: dict[int, float] = {}


async def _alert_stuck_upload(event, reason: str) -> None:
    """Tell the owner when files were expected but something else arrived.

    Rate-limited per user inside `alerts`, so a chatty user cannot spam the owner.
    """
    try:
        from app.services import alerts
        sender = await event.get_sender()
        name = getattr(sender, "first_name", "") or ""
        labels = {"text": "টেক্সট / লিংক", "sticker": "স্টিকার বা GIF",
                  "audio": "অডিও / ভয়েস", "document": "ডকুমেন্ট",
                  "file": "ফাইল", "empty": "খালি মেসেজ", "other": "অন্য কিছু"}
        spawn(alerts.stuck_upload(event.sender_id,
                                  labels.get(reason, reason or "অন্য কিছু"), name))
    except Exception as exc:
        log.debug("stuck-upload alert skipped: %s", exc)


async def _text_guard_reply(event, reason: str) -> None:
    """Tell the admin — clearly and once — that text is never saved as content."""
    admin_id = event.sender_id
    now = time.time()
    if now - _last_text_nudge.get(admin_id, 0) < 20:
        return                                    # don't spam on every stray text
    _last_text_nudge[admin_id] = now
    await event.respond(media_guard.reject_text(reason))


async def mirror_soon(file_id: int) -> None:
    """Prepare the bot-side copy of a freshly added file (background).

    After the copy exists, the optional “auto-post to channel” step runs — the
    channel gets a message (or a limited link) with the online button. Everything
    is background work: the admin's upload never waits on it.
    """
    from app.services import channels as channels_service
    from app.services.sender import ensure_ready
    row = db.file(file_id)
    if row is None:
        return
    try:
        updated = await ensure_ready(row)
        if updated and updated.get("mirror_msg"):
            db.log_event("mirror_ok", None, row["store_id"], file_id, "auto")
    except Exception as exc:
        log.debug("background mirror failed for %s: %s", file_id, exc)
    try:
        result = await channels_service.auto_post_new_file(file_id, row["store_id"])
        if result.get("ok"):
            db.log_event("auto_post_ok", None, row["store_id"], file_id, "channel")
    except Exception as exc:
        log.debug("auto channel post skipped for %s: %s", file_id, exc)


# --------------------------------------------------------------- search input
@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.out))
async def search_input(event) -> None:
    from app.handlers.state import search_pending
    user_id = event.sender_id
    if user_id not in search_pending:
        return
    if peek(user_id) is not None:
        return                    # a different flow is waiting for this message
    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        return
    if text in texts.REPLY_BUTTONS:
        search_pending.pop(user_id, None)      # the bottom menu wins
        return
    store_id = search_pending.pop(user_id)
    store = db.store(store_id)
    if store is None:
        return
    if not text:
        return
    if not access.has_access(store, user_id):
        await event.respond(texts.access_denied(store["name"]))
        return

    db.log_event("search", user_id, store_id, None, text[:80], "store")
    matches = db.search_files(store_id, text.lower(), cfg.SEARCH_RESULT_LIMIT)
    if not matches:
        await event.respond(f"❌ Nothing found for “{esc(text)}” in <b>{esc(store['name'])}</b>.\n"
                            "<i>Tip: a shorter keyword usually works better.</i>")
        return
    await event.respond(
        f"🔍 <b>{len(matches)} result(s)</b> for “{esc(text)}” in <b>{esc(store['name'])}</b>:",
        buttons=keyboards.search_results(store, matches),
    )


# ------------------------------------------------------------- pending inputs
@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.out))
async def pending_input(event) -> None:
    admin_id = event.sender_id
    if flow_state.drop_if_stale(admin_id):
        # Nothing was saved — say so, otherwise it looks like the bot ate the text.
        await event.respond(
            "⌛️ <b>আগের প্রশ্নটির সময় শেষ হয়ে গেছে</b> — তাই আপনার লেখাটি "
            "কোথাও সেভ হয়নি।\n\nআবার শুরু করতে প্যানেলের বাটনে চাপ দিন।",
            buttons=flow_state.cancel_buttons())
        return
    pending = peek(admin_id)
    if pending is None:
        return
    # Some flows belong to ordinary users (payment proof, coupons, support,
    # requests); everything else is admin-only.
    if pending["action"] not in USER_ACTIONS and not access.is_admin(admin_id):
        return
    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        return                                     # /cancel and friends win
    if text in texts.REPLY_BUTTONS:
        take(admin_id)                             # the bottom menu wins
        return
    take(admin_id)
    action = pending["action"]
    ctx = pending["ctx"]
    answer = event.respond

    # ------------------------------------------------- per-link options (lk_*)
    if action.startswith("lk_"):
        from app.handlers.linkpanel import handle_input
        await handle_input(event, action, ctx, text)
        return

    # ------------------------------------------------------------- sessions
    if action == "session_input":
        try:
            await event.delete()                   # never leave a session string lying around
        except Exception:
            pass
        from app.handlers.admin import connect_session
        await connect_session(event, text)
        return

    # --------------------------------------------------------------- stores
    if action == "create_store":
        name = text[:60]
        if not name:
            await ask_again(event, action, "⚠️ Please send a real name.", **ctx)
            return
        store = db.create_store(admin_id, name)
        db.set_active_store(admin_id, store["id"])
        await answer(
            f"✅ Store <b>{esc(store['name'])}</b> created and set as active.\n"
            f"🔗 <code>https://t.me/{runtime.bot_username}?start=s{store['id']}</code>\n"
            f"<i>Short link:</i> <code>https://t.me/{runtime.bot_username}?start={store['slug']}</code>\n\n"
            "It is <b>premium</b> by default — tap the 🔒 button in the panel to make it free."
        )
        return

    if action == "set_channel":
        from app.services import forcejoin, settings as settings_service
        raw = text.strip()
        if raw.lower() in ("off", "none", "no", "disable", "বন্ধ", "-"):
            settings_service.set("FORCE_CHANNEL", "")
            settings_service.set("FORCE_JOIN_ENABLED", False)
            forcejoin.clear_cache()
            await answer("✅ ফোর্স-জয়েন বন্ধ করা হলো — সবাই সরাসরি ব্রাউজ করতে পারবে।")
        else:
            cleaned = forcejoin.normalize(raw)
            if not cleaned:
                await answer("⚠️ <b>চ্যানেল চেনা গেল না।</b>\n"
                             "যেকোনো একটা দিন: <code>@mychannel</code>, "
                             "<code>https://t.me/mychannel</code>, "
                             "<code>https://t.me/+ইনভাইট</code>, অথবা চ্যানেল আইডি "
                             "<code>-100…</code>")
            else:
                ok, error = settings_service.set("FORCE_CHANNEL", cleaned)
                if not ok:
                    await answer(f"⚠️ {esc(error)}")
                else:
                    settings_service.set("FORCE_JOIN_ENABLED", True)
                    forcejoin.clear_cache()
                    result = await forcejoin.resolve(refresh=True, use_cache=False)
                    if result.get("ok"):
                        await answer(f"✅ চ্যানেল সেট: <b>{esc(result['title'])}</b>\n"
                                     f"🔗 {esc(forcejoin.join_url()) or '—'}")
                    else:
                        target = forcejoin.parse(cleaned)
                        extra = ("" if target["kind"] != "invite" else
                                 "\nপ্রাইভেট লিংক সংরক্ষণ করা হয়েছে; বট চ্যানেলে "
                                 "অ্যাড থাকলে চেক কাজ করবে।")
                        await answer(f"⚠️ সেভ হয়েছে: <code>{esc(cleaned)}</code>\n"
                                     f"তবে চ্যানেল পাওয়া যায়নি ({esc(result.get('error') or '')})."
                                     f"{extra}")
        try:
            from app.handlers.admin import show_settings_group
            await show_settings_group(event, "force_join", edit=False)
        except Exception:
            pass
        return

    if action == "set_caption":
        new_cap = "" if text.lower() in ("clear", "off", "-", "none") else event.raw_text
        runtime.set_caption(new_cap)
        await answer("✅ Caption updated." if new_cap else "✅ Caption cleared.")
        try:
            from app.handlers.admin import send_panel
            await send_panel(event, edit=False)
        except Exception:
            pass
        return

    # ------------------------------------------------------- channels (v3)
    if action == "add_channel":
        from app.services import channels as channels_service
        target = text.strip()
        forwarded = getattr(event.message, "forward", None)
        ref = target
        if forwarded is not None and not target:
            try:
                origin = await event.get_chat()
                ref = getattr(origin, "username", "") or str(getattr(origin, "id", ""))
            except Exception:
                ref = ""
        result = await channels_service.add_channel(ref)
        if result.get("ok"):
            await answer(f"✅ চ্যানেল যোগ হয়েছে: <b>{esc(result.get('title') or ref)}</b>\n"
                         f"ID: <code>{result.get('chat_id')}</code>")
            try:
                from app.handlers.panel_v3 import channels_home
                await channels_home(event, "")
            except Exception:
                pass
        else:
            await answer(f"⚠️ চ্যানেল পাওয়া গেল না: {esc(str(result.get('error') or ''))}\n"
                         "বট যেন চ্যানেলের অ্যাডমিন হয়, অথবা প্রাইভেট হলে ইনভাইট লিংক দিন।")
        return

    if action == "store_forcejoin":
        from app.services import forcejoin as forcejoin_service
        store_id = safe_int(ctx.get("store_id"))
        raw = text.strip()
        if raw.lower() in ("off", "none", "-", "no", "disable", "বন্ধ"):
            db.set_store_forcejoin(store_id, "")
            forcejoin_service.clear_cache()
            await answer("✅ এই স্টোরের আলাদা চ্যানেল বন্ধ করা হলো।")
        else:
            cleaned = forcejoin_service.normalize(raw)
            if not cleaned:
                await ask_again(event, action, "⚠️ চ্যানেল চেনা গেল না — <code>@name</code>, "
                             "<code>https://t.me/+ইনভাইট</code> বা <code>-100…</code> দিন।", **ctx)
                return
            db.set_store_forcejoin(store_id, cleaned)
            forcejoin_service.clear_cache()
            store = db.store(store_id) or {}
            await answer(f"✅ <b>{esc(store.get('name') or '')}</b> — এই স্টোরে এখন "
                         f"<code>{esc(cleaned)}</code> চ্যানেলেও জয়েন লাগবে।")
            try:
                from app.handlers.panel_v3 import store_forcejoin_screen
                await store_forcejoin_screen(event, str(store_id))
            except Exception:
                pass
        return

    if action == "link_limit":
        file_id = safe_int(ctx.get("file_id"))
        # “100” = total clicks · “100/1” = total clicks / per person
        raw = (text or "").strip().replace(" ", "")
        total_part, _, per_user_part = raw.partition("/")
        limit = safe_int(total_part, -1)
        per_user = safe_int(per_user_part, 0) if per_user_part else 0
        if limit < 0 or per_user < 0:
            await ask_again(event, action, "⚠️ শুধু সংখ্যা লিখুন — যেমন <code>100</code> "
                         "(মোট ১০০ বার) বা <code>100/1</code> "
                         "(মোট ১০০, একজন সর্বোচ্চ ১ বার) — <code>0</code> = আনলিমিটেড।", **ctx)
            return
        file_row = db.file(file_id)
        if file_row is None:
            await answer("⚠️ ফাইলটি পাওয়া গেল না।")
            return
        token = db.create_link(admin_id, [file_id], None, kind="limited",
                               max_clicks=limit, note="bot", per_user_limit=per_user)
        link = (f"https://t.me/{runtime.bot_username}?start=t{token}"
                if runtime.bot_username else token)
        label = f"{limit} বার খোলা যাবে" if limit else "আনলিমিটেড"
        if per_user:
            label += f" · একজন সর্বোচ্চ {per_user} বার"
        await answer(
            f"🔗 <b>লিংক তৈরি হয়েছে</b> ({label})\n\n"
            f"<code>{link}</code>\n\n"
            f"📄 {esc(file_row['name'])}"
            + ("\n\n<i>লিমিট শেষ হলে ইউজারকে বলা হবে — অ্যাক্সেস দেওয়া হবে না।</i>"
               if limit else ""))
        return

    # ---------------------------------------------------------------- scans
    if action == "botscan_channel":
        await run_bot_scan(event, admin_id, text)
        return

    # ------------------------------------------------------------- broadcast
    if action == "broadcast":
        targets = ctx.get("targets") or []
        if not targets:
            await answer("⚠️ No targets left — start the broadcast again.")
            return
        if not (event.raw_text or "").strip():
            await ask_again(event, action, "⚠️ Broadcast message cannot be empty — send it as text.", **ctx)
            return
        status = await event.respond(f"📢 Broadcasting to <b>{len(targets)}</b> user(s)…")
        spawn(run_broadcast(admin_id, targets, event.raw_text, status))
        return

    # ---------------------------------------------------------------- grants
    if action == "grant_target":
        # Try to get user ID from forwarded message as well
        target = safe_int(text)
        if not target:
            # Check if this is a forwarded message
            fwd = getattr(event.message, "forward", None)
            if fwd and getattr(fwd, "from_id", None):
                try:
                    # from_id can be PeerUser, etc.
                    from_id = fwd.from_id
                    if hasattr(from_id, "user_id"):
                        target = int(from_id.user_id)
                    elif isinstance(from_id, int):
                        target = int(from_id)
                except Exception:
                    pass
        if not target:
            # Also try to parse from reply or mention?
            await ask_again(event, action, "⚠️ Please send a numeric Telegram user ID (or forward a message from them).\n"
                         "উদাহরণ: <code>123456789</code> — ইউজারের প্রোফাইল থেকে ID নিন, অথবা তার মেসেজ ফরওয়ার্ড করুন।", **ctx)
            return
        stores = db.stores_admin(admin_id)
        if not stores:
            await answer("⚠️ Create a store first.")
            return
        from app.handlers.state import flow as flow_fn
        flow_fn(admin_id, target=target)
        # If store already in flow context, go directly to duration picker
        flow_ctx = flow_ctx_state(admin_id)
        store_id = flow_ctx.get("store_id") or ctx.get("store_id")
        if store_id:
            store = db.store(safe_int(store_id))
            if store:
                await answer(f"👑 Granting access to <code>{target}</code> for <b>{esc(store['name'])}</b> — pick duration:",
                             buttons=keyboards.duration_choices("gd:"))
                return
        await answer(f"👑 Granting access to <code>{target}</code> — pick a store:",
                     buttons=keyboards.store_picker(stores, "gst:", back="adm:users"))
        return

    if action == "grant_days_value":
        flow_ctx = flow_ctx_state(admin_id)
        days = safe_int(text)
        store_id = ctx.get("store_id") or flow_ctx.get("store_id")
        target = ctx.get("target") or flow_ctx.get("target")
        if days <= 0 or not store_id or not target:
            await ask_again(event, action, "⚠️ Please send a positive number of days.", **ctx, store_id=store_id, target=target)
            return
        store = db.store(store_id)
        if store is None:
            await answer("⚠️ Store not found.")
            return
        expires_at = access.grant_days(store_id, target, days, source="admin")
        await answer(f"✅ <code>{target}</code> now has <b>{days} days</b> of "
                     f"<b>{esc(store['name'])}</b> (until {time.strftime('%Y-%m-%d', time.localtime(expires_at))}).")
        try:
            await bot.send_message(target, f"🎉 Your access to <b>{esc(store['name'])}</b> is active!")
        except Exception:
            pass
        # Show access list again
        try:
            from app.handlers.manage import show_store_access
            await show_store_access(event, store_id)
        except Exception:
            pass
        return

    if action == "referral_days_value":
        days = safe_int(text)
        store_id = ctx.get("store_id")
        if days <= 0 or not store_id:
            await ask_again(event, action, "⚠️ Please send a positive number of days.", **ctx)
            return
        db.set_referral_cfg(admin_id, store_id, days)
        await answer(f"✅ Referral reward set: <b>{days} days</b> per successful invite.")
        return

    # ------------------------------------------------------------------ drip
    if action == "drip_count":
        count = safe_int(text)
        if count <= 0:
            await ask_again(event, action, "⚠️ Please send a positive number.", **ctx)
            return
        await ask_again(event, "drip_time", "💬 Now send the daily send time as <code>HH:MM</code> (24h).", store_id=ctx.get("store_id"), count=count)
        return

    if action == "drip_time":
        match = TIME_RE.match(text)
        if not match:
            await ask_again(event, action, "⚠️ Send the time as <code>HH:MM</code>, e.g. <code>19:30</code>.", **ctx)
            return
        store_id = ctx.get("store_id")
        store = db.store(store_id) if store_id else None
        if store is None:
            await answer("⚠️ Store not found — start again from 📅 Daily drip.")
            return
        send_time = f"{int(match.group(1)):02d}:{match.group(2)}"
        count = ctx.get("count") or 3
        db.upsert_drip(store["id"], admin_id, count, send_time)
        await answer(f"✅ <b>Daily drip on</b> for <b>{esc(store['name'])}</b>: "
                     f"{count} file(s) every day at <b>{send_time}</b>.\n"
                     "<i>Users subscribe with the 🔔 button inside the store.</i>")
        return


    # ------------------------------------------------------- contact / tickets
    if action == "contact_message":
        from app.services import support
        user = db.user(admin_id) or {}
        ticket_id = await support.open_ticket(admin_id, event.raw_text.strip()[:1000],
                                              user.get("username") or "")
        if ticket_id is None:
            await answer(i18n.t(admin_id, "contact_cooldown",
                                seconds=support.cooldown_left(admin_id)))
            return
        await answer(i18n.t(admin_id, "contact_sent", ticket=ticket_id))
        return

    if action == "ticket_reply":
        from app.services import support
        ticket_id = ctx.get("ticket_id")
        ok = await support.send_reply(ticket_id, admin_id, event.raw_text.strip()[:1500])
        await answer("✅ Reply sent to the user." if ok else "⚠️ Could not deliver the reply.")
        return

    # ------------------------------------------------------- content requests
    if action == "content_request":
        db.add_request(admin_id, ctx.get("store_id"), event.raw_text.strip()[:200])
        await answer(i18n.t(admin_id, "request_saved"))
        return

    # ----------------------------------------------------------- plan editor
    if action == "plan_create":
        from app.services import billing
        raw = event.raw_text
        parts = [p.strip() for p in raw.replace(",", "|").split("|")]
        # Fix: check both pending ctx and long-lived flow_ctx, then active store
        flow_ctx = flow_ctx_state(admin_id)
        store_id = ctx.get("store_id") or flow_ctx.get("store_id") or db.active_store_id(admin_id)
        store = db.store(store_id) if store_id else None
        if store is None:
            await answer("⚠️ Pick a store first from the panel — then tap 💎 Plans again.")
            return
        if len(parts) < 3:
            ask(admin_id, action, **ctx, store_id=store_id)
            # Preserve flow_ctx too
            remember_flow(admin_id, store_id=store_id)
            await answer("⚠️ Format: <code>Name | days | price</code> — e.g. "
                         "<code>1 Month | 30 | 199</code>\n"
                         f"Store: <b>{esc(store['name'])}</b>")
            return
        try:
            days = int(float(parts[1]))
            price = float(parts[2])
        except ValueError:
            ask(admin_id, action, **ctx, store_id=store_id)
            remember_flow(admin_id, store_id=store_id)
            await answer("⚠️ Days and price must be numbers. Example: <code>1 Month | 30 | 199</code>")
            return
        db.add_plan(store["id"], parts[0][:40], max(1, days), max(0.0, price))
        await answer(f"✅ Plan added to <b>{esc(store['name'])}</b>: <b>{esc(parts[0][:40])}</b> · {days} days · "
                     f"{billing.price_text(price)}")
        # Return to plan list automatically
        try:
            from app.handlers.billing import plan_list as show_plan_list
            class _FakeEvent:
                def __init__(self, ev):
                    self.sender_id = ev.sender_id
                    self._ev = ev
                async def answer(self, *a, **kw):
                    pass
            # Build a minimal event-like object for plan_list — it only needs sender_id and ui.render via event
            # So we call plan_list with original event but override rest
            await show_plan_list(event, str(store["id"]))
        except Exception as exc:
            log.debug("could not show plan list after creation: %s", exc)
        return

    if action == "coupon_create":
        parts = event.raw_text.replace(",", " ").split()
        if not parts:
            await answer("⚠️ Send: <code>CODE percent days max_uses</code>\nExample: <code>EID50 50 0 100</code>")
            return
        code = parts[0].upper()[:20]
        percent = safe_int(parts[1], 0) if len(parts) > 1 else 0
        days = safe_int(parts[2], 0) if len(parts) > 2 else 0
        max_uses = safe_int(parts[3], 0) if len(parts) > 3 else 0
        # Optional store scope from flow context
        flow_ctx = flow_ctx_state(admin_id)
        store_id = ctx.get("store_id") or flow_ctx.get("store_id")
        db.add_coupon(code, store_id, percent=percent, days=days, max_uses=max_uses)
        scope = f" for store #{store_id}" if store_id else " (all stores)"
        await answer(f"✅ Coupon <code>{esc(code)}</code> created{scope} "
                     f"({percent}% off / {days} days, limit {max_uses or '∞'}).")
        try:
            from app.handlers.billing import coupon_list as show_coupon_list
            await show_coupon_list(event, "")
        except Exception:
            pass
        return

    # -------------------------------------------------------------- coupons
    if action == "coupon_code":
        from app.services import billing
        code = event.raw_text.strip().upper()
        flow_ctx = flow_ctx_state(admin_id)
        store_id = ctx.get("store_id") or flow_ctx.get("store_id")
        store = db.store(safe_int(store_id)) if store_id else None
        if store is None:
            await answer("⚠️ Store not found — স্টোর থেকে আবার চেষ্টা করুন।")
            return
        # Need a plan context for percent coupons — pick cheapest active plan
        plans = db.plans(store["id"], only_active=True)
        plan = plans[0] if plans else None
        if plan is None:
            ok, result = billing.apply_coupon(code, admin_id, store, plan=None)
            # For day coupons we can still apply even without plan
            if ok and result.startswith("days:"):
                days = result.split(":", 1)[1]
                await answer(i18n.t(admin_id, "coupon_applied",
                                    desc=i18n.t(admin_id, "coupon_free", days=days)) + f"\n\n🏪 <b>{esc(store['name'])}</b> খুলুন: /start")
                return
            # If percent coupon but no plan selected, show plans first
            await answer("🎟️ কুপনটি পার্সেন্ট ছাড়ের — প্রথমে একটি প্ল্যান বেছে নিন, তারপর কুপন প্রয়োগ করুন।")
            try:
                from app.handlers.billing import choose_plan
                await choose_plan(event, str(store["id"]))
            except Exception:
                pass
            return

        ok, result = billing.apply_coupon(code, admin_id, store, plan=plan)
        if not ok:
            await ask_again(event, action, f"❌ {esc(result)}\nআবার চেষ্টা করুন বা /cancel লিখুন।", **ctx, store_id=store["id"])
            return
        if result.startswith("days:"):
            days = result.split(":", 1)[1]
            await answer(i18n.t(admin_id, "coupon_applied",
                                desc=i18n.t(admin_id, "coupon_free", days=days)) + f"\n\n🏪 এখন স্টোর খুলুন: /start")
            return
        percent, price = result.split(":")[1:]
        # percent coupon: keep it in the flow and show the plans at the new price
        remember_flow(admin_id, coupon=code, store_id=store["id"], percent=int(percent))
        await answer(i18n.t(admin_id, "coupon_applied",
                            desc=i18n.t(admin_id, "coupon_percent", percent=percent,
                                        price=billing.price_text(float(price)))) + "\n\n⬇️ নিচে ডিসকাউন্ট সহ প্ল্যানগুলো:")
        try:
            from app.handlers.billing import choose_plan
            await choose_plan(event, str(store["id"]))
        except Exception:
            pass
        return

    # -------------------------------------------------------- payment proof
    if action == "payment_proof":
        from app.services import billing
        order_id = ctx.get("order_id")
        order = db.order(order_id) if order_id else None
        if order is None:
            await answer("⚠️ Order not found — please start again.")
            return
        proof = event.raw_text.strip() if event.raw_text else "photo"
        db.update_order_proof(order["id"], proof[:200])
        await answer("🧾 ধন্যবাদ! আপনার পেমেন্ট যাচাইয়ের জন্য পাঠানো হয়েছে।")
        await notify_admins_about_order(order)
        return

    # ------------------------------------------------------------- link tool
    if action == "link_expiry":
        minutes = safe_int(text)
        state = link_gen.get(admin_id)
        if minutes <= 0:
            await ask_again(event, action, "⚠️ Please send a positive number of minutes.", **ctx)
            return
        if not state or len(state["selected"]) < 2:
            await answer("⚠️ Selection expired — pick the files again.")
            return
        token = db.create_link(admin_id, state["selected"], time.time() + minutes * 60, kind="multi")
        link_gen.pop(admin_id, None)
        await answer(f"✅ <b>Link ready</b> — expires in {minutes} minutes\n"
                     f"<code>https://t.me/{runtime.bot_username}?start=t{token}</code>")
        return

    if action == "rename_store":
        flow_ctx = flow_ctx_state(admin_id)
        sid = safe_int(ctx.get("store_id") or flow_ctx.get("store_id"))
        store = db.store(sid)
        if store is None:
            await answer("⚠️ Store not found.")
            return
        new_name = event.raw_text.strip()[:60]
        if not new_name:
            await ask_again(event, action, "⚠️ Please send a name.", **ctx, store_id=sid)
            return
        db.update_store(store["id"], name=new_name)
        await answer(f"✅ Store renamed to <b>{esc(new_name)}</b>.")
        try:
            from app.handlers.manage import store_settings as show_store_settings
            await show_store_settings(event, str(store["id"]))
        except Exception:
            pass
        return

    if action == "store_description":
        flow_ctx = flow_ctx_state(admin_id)
        sid = safe_int(ctx.get("store_id") or flow_ctx.get("store_id"))
        store = db.store(sid)
        if store is None:
            await answer("⚠️ Store not found.")
            return
        raw_desc = event.raw_text.strip()
        value = "" if raw_desc.lower() in ("clear", "off", "-", "none") else raw_desc[:400]
        db.set_store_meta(store["id"], description=value)
        await answer("✅ Description updated." if value else "✅ Description cleared.")
        try:
            from app.handlers.manage import store_settings as show_store_settings
            await show_store_settings(event, str(store["id"]))
        except Exception:
            pass
        return

    if action == "global_search":
        from app.handlers.extras import global_results_buttons
        keyword = event.raw_text.strip()
        if len(keyword) < 2:
            await ask_again(event, action, "⚠️ লিখুন অন্তত ২ অক্ষর।")
            return
        matches = db.search_all_stores(keyword, cfg.SEARCH_RESULT_LIMIT)
        rows = global_results_buttons(admin_id, matches)
        hidden = len(matches) - len(rows)
        if not rows:
            await answer(f"❌ “{esc(keyword)}” এর জন্য কিছু পাওয়া গেল না "
                         f"(বা অ্যাক্সেস নেই)।")
            return
        await answer(f"🔎 <b>{esc(keyword)}</b> — {len(rows)} ফলাফল"
                     + (f" ({hidden} locked)" if hidden > 0 else ""),
                     buttons=rows)
        return

    if action == "rename_file":
        file_row = db.file(safe_int(ctx.get("file_id")))
        if file_row is None:
            await answer("⚠️ That file no longer exists.")
            return
        new_name = event.raw_text.strip()[:80]
        if not new_name:
            await ask_again(event, action, "⚠️ Please send a name.", **ctx)
            return
        db.rename_file(file_row["id"], new_name)
        await answer(f"✅ Renamed to <b>{esc(new_name)}</b>.")
        try:
            from app.handlers.manage import show_file_manager
            store = db.store(file_row["store_id"])
            if store:
                await show_file_manager(event, store, 0, edit=False)
        except Exception:
            pass
        return

    if action == "user_search":
        query = event.raw_text.strip()
        if not query:
            await ask_again(event, action, "⚠️ লিখুন কিছু — ID, নাম বা username।", **ctx)
            return
        try:
            from app.handlers.manage import show_user_search_results
            await show_user_search_results(event, query)
        except Exception as exc:
            log.warning("user search failed: %s", exc)
            await answer(f"❌ Search failed: {esc(str(exc)[:100])}")
        return

    if action == "bulk_grant":
        flow_ctx = flow_ctx_state(admin_id)
        store_id = ctx.get("store_id") or flow_ctx.get("store_id")
        store = db.store(safe_int(store_id)) if store_id else None
        if store is None:
            await answer("⚠️ Store not found — start again.")
            return
        raw = event.raw_text.strip()
        # Parse IDs: split by any non-digit? Actually allow spaces, commas, newlines
        import re
        ids = [safe_int(x) for x in re.split(r"[\s,]+", raw) if safe_int(x)]
        ids = list(dict.fromkeys(ids))  # unique preserve order
        if not ids:
            await ask_again(event, action, "⚠️ কোনো valid user ID পাওয়া যায়নি। আবার পাঠান।", **ctx, store_id=store_id)
            return
        if len(ids) > 100:
            await answer(f"⚠️ একসাথে সর্বোচ্চ ১০০ জন — আপনি পাঠিয়েছেন {len(ids)} জন। প্রথম ১০০ জন নেওয়া হলো।")
            ids = ids[:100]
        remember_flow(admin_id, bulk_ids=ids, store_id=store_id)
        await ask_again(event, "bulk_grant_days", 
            f"👥 {len(ids)} জন ইউজার পাওয়া গেছে — এবার কতদিনের অ্যাক্সেস দেবেন?\n"
            "দিনের সংখ্যা লিখে পাঠান (যেমন <code>30</code>), অথবা <code>0</code> লিখলে লাইফটাইম।"
        , store_id=store_id)
        return

    if action == "bulk_grant_days":
        flow_ctx = flow_ctx_state(admin_id)
        store_id = ctx.get("store_id") or flow_ctx.get("store_id")
        ids = flow_ctx.get("bulk_ids") or []
        store = db.store(safe_int(store_id)) if store_id else None
        if not store or not ids:
            await answer("⚠️ তথ্য হারিয়ে গেছে — আবার শুরু করুন।")
            return
        days = safe_int(text)
        if days < 0:
            await ask_again(event, action, "⚠️ দিনের সংখ্যা ০ বা তার বেশি হতে হবে (০ = লাইফটাইম)।", **ctx, store_id=store_id)
            return
        expires_at = None if days == 0 else time.time() + days * 86400
        granted = 0
        for uid in ids:
            try:
                db.grant(store_id, uid, expires_at, source="bulk_admin")
                granted += 1
            except Exception:
                pass
        await answer(f"✅ {granted}/{len(ids)} জনকে অ্যাক্সেস দেওয়া হয়েছে — {store['name']} · {'♾ Lifetime' if days == 0 else f'{days} days'}")
        try:
            from app.handlers.manage import show_store_access
            await show_store_access(event, store_id)
        except Exception:
            pass
        return

    if action == "admin_file_search":
        keyword = event.raw_text.strip()
        if len(keyword) < 2:
            await ask_again(event, action, "⚠️ অন্তত ২ অক্ষর লিখুন।")
            return
        matches = db.search_all_stores(keyword, 20)
        if not matches:
            await answer(f"❌ “{esc(keyword)}” এর জন্য কিছু পাওয়া যায়নি।")
            return
        lines = [f"🔍 <b>{esc(keyword)}</b> — {len(matches)} ফলাফল", ""]
        rows = []
        for f in matches[:10]:
            store = db.store(f["store_id"])
            lines.append(f"📄 {esc(f['name'])} · {esc(store['name']) if store else '?'} · 👁{f['views']}")
            rows.append([Button.inline(f"🗑 {f['name'][:18]}", f"fmd:{f['id']}"),
                         Button.inline("📂 Open", f"fms:{f['store_id']}")])
        rows.append([Button.inline("🔙 Back to content", "adm:content")])
        await answer("\n".join(lines), buttons=rows)
        return

    if action == "ban_user":
        uid = safe_int(text)
        if not uid:
            await ask_again(event, action, "⚠️ Valid user ID দিন।", **ctx)
            return
        if access.is_admin(uid):
            await answer("⚠️ অ্যাডমিনকে ব্যান করা যায় না।")
            return
        db.ban_user(uid, reason="banned by admin", banned_by=admin_id)
        try:
            runtime.blocked_users.add(uid)
        except Exception:
            pass
        await answer(f"🚫 User <code>{uid}</code> banned — all grants revoked.")
        return

    if action == "unban_user":
        uid = safe_int(text)
        if not uid:
            await ask_again(event, action, "⚠️ Valid user ID দিন।", **ctx)
            return
        db.unban_user(uid)
        try:
            runtime.blocked_users.discard(uid)
        except Exception:
            pass
        await answer(f"✅ User <code>{uid}</code> unbanned.")
        return

    if action == "set_welcome":
        raw = event.raw_text.strip()
        if raw.lower() in ("clear", "off", "none", "-"):
            db.set_welcome(admin_id, "")
            await answer("✅ Welcome message cleared — default will be used.")
        else:
            db.set_welcome(admin_id, raw[:1000])
            await answer(f"✅ Welcome message updated ({len(raw)} chars).")
        try:
            from app.handlers.admin import send_panel
            await send_panel(event, edit=False)
        except Exception:
            pass
        return

    # ------------------------------------------------------- bot texts (v3)
    if action == "text_edit":
        from app.services import bot_texts
        key = ctx.get("key") or ""
        lang = ctx.get("lang") or "bn"
        raw = (event.raw_text or "").strip()
        ok, error = bot_texts.set(key, "" if raw in ("", "-") else raw, lang)
        if not ok:
            await ask_again(event, action, f"⚠️ {esc(error)}", **ctx)
            return
        if raw and raw != "-":
            await event.respond("✅ সেভ হয়েছে — এখন থেকে বট এভাবে বলবে:\n\n"
                                f"<blockquote>{esc(raw)}</blockquote>")
        else:
            await event.respond("♻️ ডিফল্ট লেখা ফিরে এসেছে।")
        try:
            from app.handlers import panel_v3
            await panel_v3.texts_one(event, key)
        except Exception:
            pass
        return

    # --------------------------------------------------------- setting editor
    if action == "setting_edit":
        from app.services import settings as settings_service
        key = ctx.get("key") or ""
        item = settings_service.setting(key)
        if item is None:
            await answer("⚠️ সেটিংটি চেনা গেল না।")
            return
        value = text.strip()
        if value in ("-", "clear", "খালি", "empty"):
            value = ""
        ok, error = settings_service.set(key, value)
        if not ok:
            await ask_again(event, "setting_edit", f"⚠️ {esc(error)}", **ctx)
            return
        if key == "FORCE_CHANNEL":
            from app.services import forcejoin
            forcejoin.clear_cache()
            if value:
                settings_service.set("FORCE_JOIN_ENABLED", True)
                result = await forcejoin.resolve(refresh=True, use_cache=False)
                await answer(f"✅ সেভ হয়েছে। চ্যানেল: {esc(result.get('title') or '— পাওয়া যায়নি —')}")
            else:
                await answer("✅ ফোর্স-জয়েন বন্ধ করা হলো।")
        else:
            await answer("✅ সেভ হয়েছে — সাথে সাথেই কার্যকর।")
        from app.handlers.admin import show_settings_group
        await show_settings_group(event, item.group, edit=False)
        return

    # ------------------------------------------------------ broadcast studio
    if action == "studio_text":
        audience = ctx.get("audience") or "all"
        file_id = ctx.get("file_id") or 0
        body = (event.raw_text or "").strip()
        if not body and not file_id:
            await ask_again(event, "studio_text", "⚠️ খালি মেসেজ পাঠানো যাবে না — টেক্সট লিখুন বা 🎬 ফাইল বেছে নিন।", **ctx)
            return
        body = _safe_html(body)
        campaign = broadcast.create_campaign(
            admin_id, text=body, audience=audience,
            files=[file_id] if file_id else [], start=False,
        )
        from app.handlers.admin import _render_campaign
        await answer("🧾 ক্যাম্পেইন তৈরি — নিচে প্রিভিউ দেখে নিশ্চিত করুন।")
        await _render_campaign(event, campaign["id"], edit=False)
        return

    if action == "studio_time":
        from app.utils import local_now
        campaign_id = safe_int(ctx.get("campaign_id"))
        if db.campaign(campaign_id) is None:
            await answer("⚠️ ক্যাম্পেইনটি আর নেই।")
            return
        match = TIME_RE.match(text)
        if not match:
            await ask_again(event, "studio_time", "⚠️ সময় <b>HH:MM</b> ফরম্যাটে লিখুন (যেমন 21:30)।", **ctx)
            return
        hour, minute = int(match.group(1)), int(match.group(2))
        from datetime import timedelta
        when = local_now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when.timestamp() <= time.time():
            when = when + timedelta(days=1)     # this time already passed → tomorrow
        db.update_campaign(campaign_id, status="scheduled", scheduled_at=when.timestamp())
        from app.handlers.admin import _render_campaign
        from app.utils import fmt_ts
        await answer(f"📅 {fmt_ts(when.timestamp(), '%d %b %H:%M')} এ পাঠানো হবে।")
        await _render_campaign(event, campaign_id, edit=False)
        return

    await answer(texts.UNKNOWN_INPUT)


def _safe_html(raw: str) -> str:
    """Let admins use <b>/<a> tags, but never let broken HTML kill the send."""
    if not raw:
        return ""
    try:
        from telethon.extensions import html as tl_html
        tl_html.parse(raw)
        return raw
    except Exception:
        return esc(raw)


# ------------------------------------------------------------------- helpers
async def run_broadcast(admin_id: int, targets: list[int], text: str, status) -> None:
    async def progress(done: int, total: int, failed: int) -> None:
        try:
            await status.edit(f"📢 Broadcasting… <b>{done}/{total}</b> (failed: {failed})")
        except Exception:
            pass

    try:
        result = await broadcast.run_broadcast(targets, text, progress)
    except Exception as exc:
        log.exception("broadcast crashed: %s", exc)
        await status.edit(f"❌ Broadcast failed: <code>{esc(str(exc)[:200])}</code>")
        return
    await status.edit(
        f"✅ <b>Broadcast finished</b>\n"
        f"📨 Sent: <b>{result['sent']}</b>\n"
        f"⚠️ Failed: <b>{result['failed']}</b>\n"
        f"🚫 Skipped (blocked): <b>{result['skipped']}</b>\n"
        f"⏱ Took {int(result['duration'])}s",
        buttons=[[Button.inline("🔙 Back to panel", "adm:back")]],
    )


async def run_bot_scan(event, admin_id: int, text: str) -> None:
    from app.handlers.manage import _scan_finished
    store_id = db.active_store_id(admin_id)
    store = db.store(store_id) if store_id else None
    if store is None:
        await event.respond("⚠️ Set an active store first.")
        return

    forwarded_from = event.message.forward.chat if event.message.forward else None
    entity = await scanner.resolve_bot_channel(admin_id, text, forwarded_from)
    if entity is None:
        await event.respond("⚠️ Could not detect the channel. Forward a message from it, "
                            "or send its @username.")
        return
    readable, error = await scanner.bot_can_read(entity)
    if not readable:
        await event.respond(f"❌ <b>The bot cannot read that channel yet.</b>\n"
                            f"Add the bot as an <b>admin</b> there, then try again.\n"
                            f"<code>{esc(error)}</code>")
        return

    title = getattr(entity, "title", "Channel")
    status = await event.respond(f"🤖 Scanning <b>{esc(title)}</b> → <b>{esc(store['name'])}</b>…\n"
                                 "Scanned: 0 · Added: 0",
                                 buttons=[[Button.inline("⏹ Stop", "scs")]])

    async def progress(scanned: int, found: int) -> None:
        try:
            await status.edit(
                f"🤖 Scanning <b>{esc(title)}</b> → <b>{esc(store['name'])}</b>…\n"
                f"Scanned: {scanned} · Added: {found}",
                buttons=[[Button.inline("⏹ Stop", "scs")]],
            )
        except Exception:
            pass

    async def worker():
        try:
            result = await scanner.scan_with_bot(admin_id, entity, title, store["id"], progress)
        except Exception as exc:
            log.exception("bot scan failed: %s", exc)
            await status.edit(f"❌ Scan failed: <code>{esc(str(exc)[:200])}</code>")
            return
        await _scan_finished(status, store, result)

    spawn(worker())


# ---------------------------------------------------------------- order alerts
async def notify_admins_about_order(order) -> None:
    """Tell every admin that a payment is waiting for verification."""
    from telethon.tl.custom import Button
    store = db.store(order["store_id"])
    user = db.user(order["user_id"]) or {}
    text = (
        f"🧾 <b>New payment #{order['id']}</b>\n"
        f"🏪 {esc(store['name']) if store else '?'}\n"
        f"💎 {esc(order['plan_name'] or '')} · {order['days']} days\n"
        f"💰 {order['amount']} {esc(order['currency'] or '')}\n"
        f"👤 {esc(user.get('name') or 'User')} <code>{order['user_id']}</code>\n"
        f"📝 {esc((order['proof'] or '')[:150])}"
    )
    buttons = [[Button.inline("✅ Approve", f"aok:{order['id']}"),
                Button.inline("❌ Reject", f"ano:{order['id']}")],
               [Button.inline("🧾 Order desk", "aod")]]
    for admin_id in cfg.ADMIN_IDS:
        try:
            await safe_call(bot.send_message, admin_id, text, buttons=buttons,
                            what="order_notify", retries=1, raise_after_retries=False)
        except Exception as exc:
            log.debug("could not notify admin %s about order %s: %s", admin_id, order["id"], exc)
