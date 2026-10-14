"""Link customisation — the admin sends a link back to the bot and gets a menu.

    send any link the bot made (multi-link, batch link, limited link)
        → ⚙️ menu for THAT link:
            ➕ add videos          — more videos into the same link
            📢 force-join channel  — only people opening this link must join
                                      (the bot has to be an admin there)
            🔘 inline buttons      — `label | url` lines
            ✍️ caption             — shown under every video of the link

Everything is stored per link (`links.caption`, `links.buttons`, `link_channels`)
and applied by `app/services/flow.py` on every click.
"""
from __future__ import annotations

import re

from telethon import events
from telethon.tl.custom import Button

from app import runtime, ui
from app.handlers.router import route
from app.handlers.state import (ask, batch_mode, cancel_buttons, clear as clear_state,
                                link_add_mode, peek)
from app.logger import log
from app.runtime import bot
from app.services import access, linkopts
from app.storage import db
from app.utils import esc, parse_button_spec

START_RE = re.compile(r"start=(\S+)")
TOKEN_RE = re.compile(r"^[A-Za-z0-9]{6,32}$")


# ------------------------------------------------------------------- helpers
def extract_token(text: str) -> str | None:
    """Pull a link token out of whatever the admin pasted (None when not a link)."""
    text = (text or "").strip()
    if not text:
        return None
    match = START_RE.search(text)
    candidates = []
    if match:
        payload = match.group(1).strip().rstrip(").,")
        if payload.startswith("t"):
            candidates.append(payload[1:])         # ?start=t<token>
        candidates.append(payload)                 # ?start=<token>
    elif TOKEN_RE.match(text):
        candidates.append(text)                    # the bare token
        if text.startswith("t"):
            candidates.append(text[1:])
    for token in candidates:
        if token and db.link(token):
            return token
    return None


def _link_url(token: str) -> str:
    return f"https://t.me/{runtime.bot_username}?start=t{token}" if runtime.bot_username else token


def panel_text(token: str) -> str:
    link = db.link(token) or {}
    files = db.link_file_ids(token)
    channels = db.link_channels(token)
    rows = parse_button_spec(link.get("buttons") or "")
    caption = (link.get("caption") or "").strip()

    limit = int(link.get("max_clicks") or 0)
    clicks = int(link.get("clicks") or 0)
    lines = [
        "⚙️ <b>লিংক সেটিংস</b>",
        f"<code>{esc(_link_url(token))}</code>",
        "",
        f"🎬 ভিডিও/ফাইল: <b>{len(files)}</b> টি · 👁 ক্লিক: <b>{clicks}</b>"
        + (f"/{limit}" if limit else ""),
    ]
    if channels:
        lines.append("📢 ফোর্স জয়েন (শুধু এই লিংকের জন্য):")
        lines += [f"   • {esc(c.get('title') or c.get('ref') or '')}" for c in channels]
    else:
        lines.append("📢 ফোর্স জয়েন: <i>নেই</i>")
    lines.append(f"🔘 ইনলাইন বাটন: <b>{sum(len(r) for r in rows)}</b> টি" if rows
                 else "🔘 ইনলাইন বাটন: <i>নেই</i>")
    if caption:
        preview = caption if len(caption) <= 160 else caption[:160] + "…"
        lines.append(f"✍️ ক্যাপশন: {preview}")
    else:
        lines.append("✍️ ক্যাপশন: <i>নেই (ডিফল্ট)</i>")
    return "\n".join(lines)


def panel_buttons(token: str):
    rows = [
        [Button.inline("➕ ভিডিও যোগ", f"loa:{token}"),
         Button.inline("📢 ফোর্স চ্যানেল", f"lof:{token}")],
        [Button.inline("🔘 ইনলাইন বাটন", f"lob:{token}"),
         Button.inline("✍️ ক্যাপশন", f"loc:{token}")],
    ]
    if db.link_channels(token):
        rows.append([Button.inline("🗑 চ্যানেল সরান", f"lor:{token}")])
    rows.append([Button.inline("🔄 রিফ্রেশ", f"lop:{token}")])
    return rows


async def show_panel(event, token: str, edit: bool = False):
    if not db.link(token):
        await event.respond("🔗 লিংকটি পাওয়া যায়নি — হয়তো মুছে ফেলা হয়েছে।")
        return None
    return await ui.render(event, panel_text(token), panel_buttons(token), edit=edit)


def _admin(event) -> bool:
    return access.is_admin(event.sender_id)


# ------------------------------------------------- admin pastes a link → menu
@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.out and bool(e.raw_text)))
async def link_pasted(event) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    if peek(admin_id) is not None or admin_id in batch_mode or admin_id in link_add_mode:
        return                                     # another flow is waiting for this text
    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        return
    token = extract_token(text)
    if token:
        await show_panel(event, token)


# ------------------------------------------------------------------ buttons
@route("lop:")
async def refresh(event, rest: str) -> None:
    if not _admin(event):
        return await event.answer("Admins only", alert=True)
    await event.answer()
    await show_panel(event, rest, edit=True)


@route("loa:")
async def add_videos(event, rest: str) -> None:
    if not _admin(event):
        return await event.answer("Admins only", alert=True)
    token = rest
    if not db.link(token):
        return await event.answer("লিংকটি নেই", alert=True)
    if not db.active_store_id(event.sender_id):
        return await event.answer("আগে প্যানেল থেকে একটা অ্যাকটিভ স্টোর বেছে নিন", alert=True)
    await event.answer()
    link_add_mode[event.sender_id] = {"token": token, "added": 0}
    await event.respond(
        "➕ <b>ভিডিও যোগ মোড চালু</b>\n\n"
        "যত খুশি ভিডিও/ছবি এখানে পাঠান বা ফরওয়ার্ড করুন — সবগুলো এই লিংকে যোগ হবে "
        "(আপনার অ্যাকটিভ স্টোরেও সেভ থাকবে)।\nশেষ হলে <b>/done</b> দিন।")


@route("lof:")
async def add_force(event, rest: str) -> None:
    if not _admin(event):
        return await event.answer("Admins only", alert=True)
    if not db.link(rest):
        return await event.answer("লিংকটি নেই", alert=True)
    await event.answer()
    ask(event.sender_id, "lk_force", token=rest)
    await event.respond(
        "📢 <b>এই লিংকের জন্য ফোর্স জয়েন চ্যানেল</b>\n\n"
        "চ্যানেলের <code>@username</code>, <code>https://t.me/…</code> বা প্রাইভেট ইনভাইট লিংক দিন।\n"
        "⚠️ বটকে আগে ওই চ্যানেলের <b>অ্যাডমিন</b> বানাতে হবে — না হলে যাচাই হবে না।\n"
        "আরেকটা চ্যানেল চাইলে এই বাটন আবার চাপুন।",
        buttons=cancel_buttons())


@route("lob:")
async def set_buttons(event, rest: str) -> None:
    if not _admin(event):
        return await event.answer("Admins only", alert=True)
    if not db.link(rest):
        return await event.answer("লিংকটি নেই", alert=True)
    await event.answer()
    ask(event.sender_id, "lk_buttons", token=rest)
    await event.respond(
        "🔘 <b>ইনলাইন বাটন</b>\n\nপ্রতি লাইনে একটা: <code>লেবেল | লিংক</code>\n"
        "একই সারিতে দুটো চাইলে মাঝে <code>&&</code> দিন:\n"
        "<code>📢 চ্যানেল | https://t.me/mychannel\n"
        "🛒 স্টোর | https://t.me/mybot?start=s1 &amp;&amp; 💬 অ্যাডমিন | https://t.me/admin</code>\n\n"
        "মুছতে <code>0</code> লিখুন।", buttons=cancel_buttons())


@route("loc:")
async def set_caption(event, rest: str) -> None:
    if not _admin(event):
        return await event.answer("Admins only", alert=True)
    if not db.link(rest):
        return await event.answer("লিংকটি নেই", alert=True)
    await event.answer()
    ask(event.sender_id, "lk_caption", token=rest)
    await event.respond(
        "✍️ <b>ক্যাপশন</b>\n\nএই লিংকের প্রতিটা ভিডিওর নিচে যে লেখা যাবে সেটা পাঠান "
        "(ডিফল্ট ক্যাপশনের বদলে এটা বসবে)।\nমুছতে <code>0</code> লিখুন।",
        buttons=cancel_buttons())


@route("lor:")
async def remove_menu(event, rest: str) -> None:
    if not _admin(event):
        return await event.answer("Admins only", alert=True)
    await event.answer()
    rows = [[Button.inline(f"🗑 {(c.get('title') or c.get('ref') or '')[:40]}", f"lox:{c['id']}")]
            for c in db.link_channels(rest)]
    rows.append([Button.inline("🔙 ফিরে যান", f"lop:{rest}")])
    await ui.render(event, "কোন চ্যানেলটা এই লিংক থেকে সরাবেন?", rows, edit=True)


@route("lox:")
async def remove_channel(event, rest: str) -> None:
    if not _admin(event):
        return await event.answer("Admins only", alert=True)
    row = db.link_channel(int(rest or 0)) if (rest or "").isdigit() else None
    if not row:
        return await event.answer("আগেই সরানো হয়েছে", alert=True)
    db.link_channel_remove(row["id"])
    await event.answer("✅ সরানো হয়েছে")
    await show_panel(event, row["token"], edit=True)


# ------------------------------------------------------- typed answers (lk_*)
async def handle_input(event, action: str, ctx: dict, text: str) -> None:
    """Called by `messages.pending_input` for the three `lk_*` questions."""
    from app.handlers.messages import ask_again

    token = ctx.get("token") or ""
    if not db.link(token):
        await event.respond("🔗 লিংকটি পাওয়া যায়নি — হয়তো মুছে ফেলা হয়েছে।")
        return

    if action == "lk_force":
        await event.respond("🔎 চ্যানেল আর বটের অ্যাডমিন-স্ট্যাটাস চেক করছি…")
        result = await linkopts.verify_channel(text)
        if not result.get("ok"):
            await ask_again(event, action, f"⚠️ {result.get('error') or 'যাচাই হয়নি'}", **ctx)
            return
        db.link_channel_add(token, result["ref"], result.get("title") or result["ref"],
                            result["chat_id"], result.get("invite") or "")
        log.info("link %s: force-join channel %s added", token, result["chat_id"])
        await event.respond(f"✅ <b>{esc(result.get('title') or result['ref'])}</b> যোগ হয়েছে — "
                            "এখন থেকে এই লিংকে ক্লিক করলে আগে এটা জয়েন করতে হবে।")
        await show_panel(event, token)
        return

    if action == "lk_buttons":
        if text.strip() in ("0", "-"):
            db.link_update(token, buttons="")
            await event.respond("🗑 বাটন মুছে ফেলা হয়েছে।")
        else:
            if not parse_button_spec(text):
                await ask_again(event, action,
                                "⚠️ কোনো বাটন বোঝা যায়নি — <code>লেবেল | https://লিংক</code> "
                                "ফরম্যাটে লিখুন।", **ctx)
                return
            db.link_update(token, buttons=text.strip())
            await event.respond("✅ বাটন সেভ হয়েছে।")
        await show_panel(event, token)
        return

    if action == "lk_caption":
        if text.strip() in ("0", "-"):
            db.link_update(token, caption="")
            await event.respond("🗑 ক্যাপশন মুছে ফেলা হয়েছে (ডিফল্ট ক্যাপশন ফিরে এল)।")
        else:
            caption = (getattr(event.message, "text", None) or text).strip()
            if len(text) > 1000:
                await ask_again(event, action, "⚠️ ক্যাপশন অনেক বড় — ১০০০ অক্ষরের মধ্যে লিখুন।",
                                **ctx)
                return
            db.link_update(token, caption=caption)
            await event.respond("✅ ক্যাপশন সেভ হয়েছে।")
        await show_panel(event, token)
        return


async def finish_add_videos(event) -> bool:
    """`/done` while in add-videos mode. Returns True when it handled the command."""
    admin_id = event.sender_id
    state = link_add_mode.pop(admin_id, None)
    if not state:
        return False
    await event.respond(f"✅ <b>{state.get('added', 0)}</b> টি ভিডিও/ফাইল লিংকে যোগ হয়েছে।")
    await show_panel(event, state["token"])
    return True
