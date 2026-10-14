"""Content management: file manager, multi-file link generator, user list, scans.

v2.2: cleaner navigation, store access list with revoke, user search,
bulk actions, improved scan flow.
"""
from __future__ import annotations

import time

from telethon.tl.custom import Button

from app import config as cfg, keyboards, runtime, ui
from app.handlers.router import route
from app.handlers.state import ask, dialog_cache, flow, link_gen
from app.logger import log
from app.runtime import spawn
from app.services import access, billing, broadcast, scanner
from app.storage import db
from app.utils import esc, fmt_ts, safe_int


# --------------------------------------------------------------- store lookup
def _active_store(admin_id: int) -> dict | None:
    store_id = db.active_store_id(admin_id)
    return db.store(store_id) if store_id else None


# ------------------------------------------------------------- file manager
async def show_file_manager(event, store: dict, page: int = 0, edit: bool = True) -> None:
    files = db.files_of(store["id"])
    total = len(files)
    start = page * cfg.MANAGE_PAGE_SIZE
    chunk = files[start:start + cfg.MANAGE_PAGE_SIZE]
    flow(event.sender_id, store_id=store["id"])
    await ui.render(
        event,
        f"🗂 <b>{esc(store['name'])}</b> — {total} files\n"
        "Tap a file to rename, move or delete it.\n"
        "<i>Tip: use 🔍 Search to find files quickly.</i>",
        keyboards.file_manager(store, chunk, page, total),
        edit=edit,
    )


@route("fms:")
async def open_file_manager(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    await show_file_manager(event, store, 0, edit=True)
    await event.answer()


@route("fmp:")
async def file_manager_page(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    store_id, _, page = rest.partition(":")
    store = db.store(safe_int(store_id))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    await show_file_manager(event, store, safe_int(page), edit=True)
    await event.answer()


@route("fm:")
async def file_actions(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    file_row = db.file(safe_int(rest))
    if file_row is None:
        await event.answer("File no longer exists.", alert=True)
        return
    store = db.store(file_row["store_id"])
    flow(admin_id, file_id=file_row["id"], store_id=file_row["store_id"], page=0)
    text = (
        f"📄 <b>{esc(file_row['name'])}</b>\n"
        f"🏪 Store: {esc(store['name']) if store else '?'}\n"
        f"🎞 Type: {esc(file_row['kind'])} · 👁 {file_row['views']} views\n"
        f"🆔 <code>{file_row['id']}</code> · 📅 {fmt_ts(file_row['created_at'])}"
    )
    await ui.render(event, text,
                    keyboards.file_actions(file_row["id"], file_row["store_id"]),
                    edit=True)
    await event.answer()


@route("fmr:")
async def rename_file(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    file_row = db.file(safe_int(rest))
    if file_row is None:
        await event.answer("File no longer exists.", alert=True)
        return
    ask(admin_id, "rename_file", file_id=file_row["id"])
    await event.respond(f"✏️ Send the new name for <b>{esc(file_row['name'])}</b>.\n"
                        "<i>/cancel to keep it as it is.</i>")


@route("fml:")
async def file_link(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    file_row = db.file(safe_int(rest))
    if file_row is None:
        await event.answer("File no longer exists.", alert=True)
        return
    if not runtime.bot_username:
        await event.answer("Bot is still starting up.", alert=True)
        return
    link = f"https://t.me/{runtime.bot_username}?start=f{file_row['id']}"
    await event.respond(f"🔗 <b>{esc(file_row['name'])}</b>\n<code>{link}</code>\n\n"
                        "<i>Tap the link to copy it.</i>",
                        buttons=[[Button.inline("🔙 Back", f"fm:{file_row['store_id']}")]])


@route("fmm:")
async def move_file_picker(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    file_row = db.file(safe_int(rest))
    if file_row is None:
        await event.answer("File no longer exists.", alert=True)
        return
    stores = [s for s in db.stores_admin(admin_id) if s["id"] != file_row["store_id"]]
    if not stores:
        await event.answer("There is no other store to move it to.", alert=True)
        return
    await ui.render(event, "📦 <b>Move to which store?</b>",
                    keyboards.move_targets(stores, file_row["id"], file_row["store_id"]),
                    edit=True)
    await event.answer()


@route("fmmv:")
async def move_file(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    file_id, _, store_id = rest.partition(":")
    store = db.store(safe_int(store_id))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    if db.move_file(safe_int(file_id), store["id"]):
        await event.answer(f"📦 Moved to {store['name']}.", alert=True)
        await show_file_manager(event, store, 0, edit=True)
    else:
        await event.answer("Move failed.", alert=True)


@route("fmd:")
async def delete_file_confirm(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    file_row = db.file(safe_int(rest))
    if file_row is None:
        await event.answer("Already gone.", alert=True)
        return
    await ui.render(event, f"🗑 Delete <b>{esc(file_row['name'])}</b>?\n"
                           "<i>The file stays in the source channel — only the store entry is removed.</i>",
                    keyboards.confirm(f"fmdq:{file_row['id']}", f"fm:{file_row['id']}",
                                      "🗑 Yes, delete"), edit=True)
    await event.answer()


@route("fmdq:")
async def delete_file(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    store_id = db.delete_file(safe_int(rest))
    if store_id is None:
        await event.answer("Already gone.", alert=True)
        return
    store = db.store(store_id)
    await event.answer("🗑 Deleted.", alert=True)
    if store:
        await show_file_manager(event, store, 0, edit=True)


# --------------------------------------------------------- multi-link maker
def _show_link_gen(event, store: dict, page: int, selected: set[int], edit: bool = True):
    files = db.files_of(store["id"])
    start = page * cfg.CONTENT_PAGE_SIZE
    chunk = files[start:start + cfg.CONTENT_PAGE_SIZE]
    return ui.render(
        event,
        f"🎬 <b>{esc(store['name'])}</b> — pick 2 to 8 files\n"
        f"Selected: <b>{len(selected)}</b>",
        keyboards.link_generator(store, chunk, page, selected, len(files)),
        edit=edit,
    )


@route("lg:")
async def link_gen_store(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    link_gen[admin_id] = {"store_id": store["id"], "page": 0, "selected": set()}
    await _show_link_gen(event, store, 0, set(), edit=True)
    await event.answer()


@route("lgt:")
async def link_gen_toggle(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    state = link_gen.get(admin_id)
    if not state:
        await event.answer("Start again from 🎬 Multi-link.", alert=True)
        return
    file_id = safe_int(rest)
    selected = state["selected"]
    if file_id in selected:
        selected.discard(file_id)
    else:
        if len(selected) >= 8:
            await event.answer("Maximum 8 files per link.", alert=True)
            return
        selected.add(file_id)
    store = db.store(state["store_id"])
    await _show_link_gen(event, store, state["page"], selected, edit=True)
    await event.answer()


@route("lgp:")
async def link_gen_page(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    state = link_gen.get(admin_id)
    if not state:
        await event.answer("Start again from 🎬 Multi-link.", alert=True)
        return
    state["page"] = safe_int(rest)
    store = db.store(state["store_id"])
    await _show_link_gen(event, store, state["page"], state["selected"], edit=True)
    await event.answer()


@route("lgc")
async def link_gen_clear(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    state = link_gen.get(admin_id)
    if not state:
        await event.answer("Nothing selected.", alert=True)
        return
    state["selected"] = set()
    store = db.store(state["store_id"])
    await _show_link_gen(event, store, state["page"], set(), edit=True)
    await event.answer("Cleared.")


@route("lgg")
async def link_gen_generate(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    state = link_gen.get(admin_id)
    if not state or len(state["selected"]) < 2:
        await event.answer("Select at least 2 files first.", alert=True)
        return
    await ui.render(event, f"⏳ <b>Set the expiry</b> ({len(state['selected'])} files selected)",
                    keyboards.expiry_choices(), edit=True)
    await event.answer()


@route("lge:")
async def link_gen_expiry(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    state = link_gen.get(admin_id)
    if not state or len(state["selected"]) < 2:
        await event.answer("Selection lost — pick the files again.", alert=True)
        return
    if rest == "custom":
        ask(admin_id, "link_expiry")
        await event.respond("💬 Send the expiry in <b>minutes</b> (e.g. <code>90</code>).")
        return
    seconds = safe_int(rest)
    expires_at = time.time() + seconds if seconds > 0 else None
    token = db.create_link(admin_id, state["selected"], expires_at, kind="multi")
    link_gen.pop(admin_id, None)
    label = "never" if expires_at is None else (f"{seconds // 86400}d" if seconds >= 86400
                                                else f"{seconds // 3600}h" if seconds >= 3600
                                                else f"{seconds // 60}m")
    await ui.render(
        event,
        f"✅ <b>Link ready</b> — expires: <b>{label}</b>\n"
        f"<code>https://t.me/{runtime.bot_username}?start=t{token}</code>\n\n"
        "<i>⚠️ These files are delivered to anyone with the link, even for premium stores.</i>",
        [[Button.inline("🔙 Back to content", "adm:content")]],
        edit=True,
    )
    await event.answer()


# ------------------------------------------------------------------ scanning
@route("sc:")
async def start_scan(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store = _active_store(admin_id)
    if store is None:
        await event.answer("Set an active store first.", alert=True)
        return
    dialogs = dialog_cache.get(admin_id) or []
    index = safe_int(rest)
    if index >= len(dialogs):
        await event.answer("That channel list expired — tap 🔍 Scan channel again.", alert=True)
        return
    chat_id, title = dialogs[index]
    status = await event.respond(f"🔍 Scanning <b>{esc(title)}</b> → <b>{esc(store['name'])}</b>…\n"
                                 "Scanned: 0 · Added: 0",
                                 buttons=[[Button.inline("⏹ Stop", "scs")]])
    await event.answer()
    spawn(run_session_scan(event, admin_id, chat_id, title, store, status))


async def run_session_scan(event, admin_id: int, chat_id: int, title: str,
                           store: dict, status) -> None:
    async def progress(scanned: int, found: int) -> None:
        try:
            await status.edit(
                f"🔍 Scanning <b>{esc(title)}</b> → <b>{esc(store['name'])}</b>…\n"
                f"Scanned: {scanned} · Added: {found}",
                buttons=[[Button.inline("⏹ Stop", "scs")]],
            )
        except Exception:
            pass

    try:
        result = await scanner.scan_with_session(admin_id, chat_id, title, store["id"], progress)
    except Exception as exc:
        log.exception("scan failed: %s", exc)
        await status.edit(f"❌ Scan failed: <code>{esc(str(exc)[:200])}</code>")
        return
    await _scan_finished(status, store, result)


async def _scan_finished(status, store: dict, result: dict) -> None:
    await status.edit(
        "✅ <b>Scan finished</b>\n\n"
        f"🏪 Store: <b>{esc(store['name'])}</b>\n"
        f"📄 Messages scanned: <code>{result['scanned']}</code>\n"
        f"🎬 New files added: <code>{result['found']}</code>\n"
        "<i>Duplicates are skipped automatically.</i>",
        buttons=[
            [Button.inline("🗂 Open store files", f"fms:{store['id']}")],
            [Button.inline("🛠 Store settings", f"sss:{store['id']}")],
            [Button.inline("🔙 Main panel", "adm:back")],
        ],
    )


@route("scp:")
async def scan_page(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    dialogs = dialog_cache.get(admin_id) or []
    page = safe_int(rest)
    await ui.render(event, "🔍 <b>Pick a channel to scan:</b>",
                    keyboards.scan_chats(dialogs, page), edit=True)
    await event.answer()


@route("scs")
async def stop_scan(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    scanner.stop_scan(admin_id)
    await event.answer("⏹ Stopping after the current message…", alert=True)


# ------------------------------------------------------------------ user list
async def show_users_page(event, page: int = 0) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    uids = sorted(db.all_user_ids())
    total = len(uids)
    total_pages = max(1, (total + cfg.USERS_PAGE_SIZE - 1) // cfg.USERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * cfg.USERS_PAGE_SIZE
    lines = [f"👥 <b>Users</b> — {total} total", ""]
    for user_id in uids[start:start + cfg.USERS_PAGE_SIZE]:
        user = db.user(user_id) or {}
        grants = db.user_grants(user_id)
        if access.is_admin(user_id):
            status = "👑 admin"
        elif not grants:
            status = "🆓 free"
        else:
            parts = []
            for grant in grants:
                if grant["expires_at"] is None:
                    parts.append(f"{grant['store_name']} ♾")
                elif grant["expires_at"] > time.time():
                    parts.append(f"{grant['store_name']} → {fmt_ts(grant['expires_at'])}")
            status = "💎 " + ", ".join(parts) if parts else "⌛ expired"
        lines.append(f"<code>{user_id}</code> · {esc(user.get('name') or 'Unknown')}"
                     f" · {fmt_ts(user.get('joined_at'))} · {status}")
    lines.append("")
    lines.append(f"<i>Page {page + 1} of {total_pages}</i>")
    await ui.render(event, "\n".join(lines), keyboards.users_page(page, total), edit=True)


@route("up:")
async def users_page_nav(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    await show_users_page(event, safe_int(rest))
    await event.answer()


async def show_user_search_results(event, query: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    needle = query.strip().lower()
    if not needle:
        await event.respond("⚠️ লিখুন কিছু — ID, নাম বা username।")
        return
    matched = []
    for uid in db.all_user_ids():
        user = db.user(uid) or {}
        hay = f"{uid} {user.get('name') or ''} {user.get('username') or ''}".lower()
        if needle in hay:
            matched.append(uid)
        if len(matched) >= 20:
            break

    if not matched:
        await event.respond(f"❌ “{esc(query)}” এর জন্য কোনো ইউজার পাওয়া যায়নি।")
        return

    lines = [f"🔍 <b>Search: {esc(query)}</b> — {len(matched)} found", ""]
    rows: list[list[Button]] = []
    for uid in matched:
        user = db.user(uid) or {}
        grants = db.user_grants(uid)
        status = f"{len(grants)} grants" if grants else "free"
        lines.append(f"<code>{uid}</code> · {esc(user.get('name') or 'Unknown')} · {status}")
        rows.append([
            Button.inline(f"👤 {uid}", f"aou:{uid}"),
            Button.inline("👑 Grant", f"gst:{uid}"),
        ])
    rows.append([Button.inline("🔙 Back to users", "adm:users")])
    await event.respond("\n".join(lines), buttons=rows)


# ------------------------------------------------------------ store settings
@route("sss:")
async def store_settings(event, rest: str) -> None:
    """Everything about one store in a single screen — with proper back navigation."""
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    # Support pagination for access list: sss:12 or sss:12:1
    parts = rest.split(":")
    store_id = safe_int(parts[0])
    store = db.store(store_id)
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    flow(admin_id, store_id=store["id"])

    stats = db.store_stats(store["id"])
    plans = db.plans(store["id"])
    drip = db.drip(store["id"])
    sales = billing.store_sales(store["id"])
    lines = [
        f"🛠 <b>{esc(store['name'])}</b> — ID <code>{store['id']}</code>",
        f"📂 {stats['files']} files · 👁 {stats['views']} views · 💎 {stats['grants']} granted",
        f"🔔 {stats['subs']} drip subscribers · 💰 {billing.price_text(sales['revenue'])} earned "
        f"({sales['approved']} paid, {sales['pending']} pending)",
        f"💎 Plans: {len(plans)} · 📅 Drip: "
        f"{'on ' + str(drip['count']) + '/day at ' + drip['send_time'] if drip and drip['enabled'] else 'off'}",
        f"🌐 Lock: {'🔒 premium' if store['is_premium'] else '🆓 free for everyone'}",
        "",
        f"📝 {esc((store.get('description') or '(no description)')[:200])}",
        f"🖼 Cover: {'✅ set' if store.get('cover') else '❌ not set'}",
        f"🔗 Slug: <code>{esc(store['slug'])}</code>",
        f"📢 আলাদা চ্যানেল: {esc(store.get('forcejoin') or '— (ডিফল্ট চ্যানেল প্রযোজ্য)')}",
    ]
    buttons = [
        [Button.inline("✏️ Rename", f"ssn:{store['id']}"),
         Button.inline("🔒/🌐 Toggle", f"sst:{store['id']}")],
        [Button.inline("📝 Description", f"desc:{store['id']}"),
         Button.inline("🖼 Cover", f"cover:{store['id']}")],
        [Button.inline("💎 Plans", f"apl:{store['id']}"),
         Button.inline("🎟️ Coupon", f"acn:{store['id']}")],
        [Button.inline("🗂 Files", f"fms:{store['id']}"),
         Button.inline("👥 Access", f"sga:{store['id']}")],
        [Button.inline("📅 Drip", f"dr:{store['id']}"),
         Button.inline("🔗 Share", f"shr:{store['id']}")],
        [Button.inline("📢 আলাদা চ্যানেল", f"sfj:{store['id']}"),
         Button.inline("📊 এই স্টোরের স্ট্যাটস", f"an:store:{store['id']}")],
        [Button.inline("👁 Preview as user", f"s:{store['id']}")],
        [Button.inline("🔙 Back to stores", "adm:stores"),
         Button.inline("🏠 Main panel", "adm:back")],
    ]
    await ui.render(event, "\n".join(lines), buttons, edit=True)
    await event.answer()


@route("sst:")
async def toggle_store_premium(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        return
    new_state = 0 if store["is_premium"] else 1
    db.update_store(store["id"], is_premium=new_state)
    await event.answer(
        f"{store['name']} is now {'🔒 premium' if new_state else '🆓 free for everyone'}.",
        alert=True,
    )
    await store_settings(event, str(store["id"]))


@route("ssn:")
async def rename_store(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        return
    ask(admin_id, "rename_store", store_id=store["id"])
    flow(admin_id, store_id=store["id"])
    await event.respond(f"✏️ Send the new name for <b>{esc(store['name'])}</b>.\n<i>Current: {esc(store['name'])}</i>")


@route("shr:")
async def share_store(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        return
    bot_user = runtime.bot_username
    text = (
        f"🔗 <b>{esc(store['name'])}</b> — share links\n\n"
        f"স্টোর লিংক:\n<code>https://t.me/{bot_user}?start=s{store['id']}</code>\n\n"
        f"শর্ট লিংক:\n<code>https://t.me/{bot_user}?start={store['slug']}</code>\n\n"
        f"Buy link:\n<code>https://t.me/{bot_user}?start=buy{store['id']}</code>\n\n"
        "ইউজার লিংক খুলে /start চাপলেই স্টোর দেখতে পাবে।"
    )
    await ui.render(event, text, [[Button.inline("🔙 Back", f"sss:{store['id']}")]], edit=True)
    await event.answer()


# ------------------------------------------------------------ access management
async def show_store_access(event, store_id: int, page: int = 0) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store = db.store(store_id)
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    flow(admin_id, store_id=store_id)
    grants = db.store_grants(store_id)
    # Filter only still valid grants for cleaner view, but show all with status
    now = time.time()
    active = [g for g in grants if g["expires_at"] is None or g["expires_at"] > now]
    expired = [g for g in grants if g["expires_at"] is not None and g["expires_at"] <= now]

    lines = [
        f"👥 <b>Access — {esc(store['name'])}</b>",
        f"💎 Active: <b>{len(active)}</b> · ⌛ Expired: <b>{len(expired)}</b> · 📂 Files: {db.store_stats(store_id)['files']}",
        "",
    ]
    if not grants:
        lines.append("<i>No one has premium access yet. Grant access with 👑 button.</i>")
    else:
        for g in active[:5]:
            exp = "♾ Lifetime" if g["expires_at"] is None else fmt_ts(g["expires_at"])
            lines.append(f"✅ <code>{g['user_id']}</code> · {esc(g.get('user_name') or 'User')} · {exp}")

    await ui.render(event, "\n".join(lines),
                    keyboards.store_access_list(store, grants, page), edit=True)


@route("sga:")
async def store_access_route(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    parts = rest.split(":")
    store_id = safe_int(parts[0])
    page = safe_int(parts[1]) if len(parts) > 1 else 0
    await show_store_access(event, store_id, page)
    await event.answer()


@route("rvk:")
async def revoke_access(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    # format: store_id:user_id
    try:
        store_part, _, user_part = rest.partition(":")
        store_id = safe_int(store_part)
        user_id = safe_int(user_part)
    except Exception:
        await event.answer("Invalid payload.", alert=True)
        return
    store = db.store(store_id)
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    db.revoke(store_id, user_id)
    await event.answer(f"🚫 Revoked access for {user_id} from {store['name']}.", alert=True)
    await show_store_access(event, store_id)


@route("gbulk:")
async def bulk_grant_start(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    store_id = safe_int(rest)
    store = db.store(store_id)
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    flow(event.sender_id, store_id=store_id)
    ask(event.sender_id, "bulk_grant", store_id=store_id)
    await event.respond(
        f"👥 <b>Bulk grant — {esc(store['name'])}</b>\n\n"
        "Send user IDs separated by space, comma or new line.\n"
        "Example: <code>123456 789012 345678</code>\n\n"
        "Then you'll pick duration for all of them."
    )
    await event.answer()


# =============================================== per-file broadcast (v2.3)
# “প্রত্যেকটা ভিডিওর ক্ষেত্রে ব্রডকাস্টিং” — every file row has a 📢 button, and
# each campaign keeps its own queue, so a video can be pushed again any time.
@route("fbr:")
async def file_broadcast(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    file_row = db.file(safe_int(rest))
    if file_row is None:
        await event.answer("ফাইলটি আর নেই।", alert=True)
        return
    store = db.store(file_row["store_id"])
    text = (f"📢 <b>Broadcast: {esc(file_row['name'])}</b>\n"
            f"🏪 {esc(store['name']) if store else '?'} · 👁 {file_row['views']} views\n\n"
            "এই ভিডিওটি কারা পাবে?")
    await ui.render(event, text,
                    keyboards.file_broadcast_picker(file_row["id"], db.stores_admin(admin_id)),
                    edit=True)
    await event.answer()


@route("fbrgo:")
async def file_broadcast_go(event, rest: str) -> None:
    """Create the campaign for one file and show the usual confirm screen."""
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    parts = rest.split(":")
    file_id = safe_int(parts[0])
    audience = ":".join(parts[1:]) if len(parts) > 1 else "all"
    if db.file(file_id) is None:
        await event.answer("ফাইলটি আর নেই।", alert=True)
        return
    targets = broadcast.resolve_audience(admin_id, audience)
    if not targets:
        await event.answer("এই অডিয়েন্সে এখন কেউ নেই।", alert=True)
        return
    campaign = broadcast.create_campaign(
        admin_id, text="", title=db.file(file_id)["name"][:40],
        audience=audience, files=[file_id], start=False,
    )
    from app.handlers.admin import _render_campaign
    await _render_campaign(event, campaign["id"], edit=True)
    await event.answer("✅ ক্যাম্পেইন তৈরি — নিশ্চিত করুন")


@route("fbrtest:")
async def file_broadcast_test(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    from app.services.sender import deliver_file_id
    result = await deliver_file_id(admin_id, safe_int(rest))
    await event.answer("🧪 পাঠানো হয়েছে!" if result.ok
                       else f"❌ পাঠানো যায়নি ({result.reason})", alert=True)
