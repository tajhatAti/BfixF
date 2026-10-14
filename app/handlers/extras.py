"""User experience extras: favorites, sorting/filters, requests, language,
share cards and the global search page."""
from __future__ import annotations

from telethon.tl.custom import Button

from app import i18n, runtime, ui
from app.handlers.router import route
from app.handlers.state import ask
from app.services import access
from app.storage import db
from app.utils import esc, safe_int

SORT_MODES = {
    "default": "🆕 নতুন আগে",
    "popular": "🔥 জনপ্রিয় আগে",
    "az": "🔤 নাম অনুযায়ী",
}
FILTER_MODES = {"all": "সব", "video": "🎬 ভিডিও", "photo": "🖼 ছবি"}


def sorted_files(store_id: int, user_id: int, kind: str | None = None) -> list[dict]:
    mode = db.user_sort(user_id)
    if mode == "popular":
        files = db.popular_files(store_id, limit=1000)
    elif mode == "az":
        files = sorted(db.files_of(store_id), key=lambda f: f["name"].lower())
    else:
        files = db.files_of(store_id, newest_first=True)
    if kind and kind != "all":
        wanted = "Video" if kind == "video" else "Photo"
        files = [f for f in files if f["kind"] == wanted]
    return files


def view_controls(user_id: int, store_id: int) -> list[list[Button]]:
    mode = db.user_sort(user_id)
    return [[
        Button.inline(SORT_MODES.get(mode, SORT_MODES["default"]), f"st:{store_id}"),
        Button.inline("🔥" if mode == "popular" else "🔤", f"stq:{store_id}"),
    ]]


# ------------------------------------------------------------------ sorting
@route("st:")
async def cycle_sort(event, rest: str) -> None:
    """Cycle through the sort modes without leaving the store view."""
    user_id = event.sender_id
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    order = ["default", "popular", "az"]
    current = db.user_sort(user_id)
    mode = order[(order.index(current) + 1) % len(order)] if current in order else "default"
    db.set_user_sort(user_id, mode)
    await event.answer(f"সাজানো: {SORT_MODES[mode]}")
    await ui.show_store(event, store, 0, edit=True)


@route("stq:")
async def quick_popular(event, rest: str) -> None:
    """One tap: switch to 🔥 popular."""
    user_id = event.sender_id
    store = db.store(safe_int(rest))
    if store is None:
        return
    db.set_user_sort(user_id, "popular")
    await event.answer("🔥 জনপ্রিয় ফাইল আগে")
    await ui.show_store(event, store, 0, edit=True)


# ----------------------------------------------------------------- favorites
@route("fv:")
async def toggle_favorite(event, rest: str) -> None:
    user_id = event.sender_id
    file_row = db.file(safe_int(rest))
    if file_row is None:
        await event.answer(i18n.t(user_id, "file_removed"), alert=True)
        return
    added = db.toggle_favorite(user_id, file_row["id"])
    await event.answer(i18n.t(user_id, "fav_added" if added else "fav_removed"), alert=True)


@route("fvs")
async def favorites_list(event, rest: str) -> None:
    user_id = event.sender_id
    files = db.favorites(user_id, 20)
    rows: list[list[Button]] = []
    for file_row in files:
        rows.append([Button.inline(
            f"{'🎬' if file_row['kind'] == 'Video' else '🖼'} "
            f"{esc(file_row['name'])[:26]}", f"f:{file_row['id']}")])
    rows.append([Button.inline(i18n.t(user_id, "back_stores"), "bs:0")])
    text = i18n.t(user_id, "fav_title") + ("\n\n" + i18n.t(user_id, "fav_empty") if not files else "")
    await ui.render(event, text, rows, edit=True)
    await event.answer()


# ------------------------------------------------------------------ requests
@route("rq:")
async def request_content(event, rest: str) -> None:
    user_id = event.sender_id
    store_id = safe_int(rest) or None
    ask(user_id, "content_request", store_id=store_id)
    await ui.render(event, i18n.t(user_id, "request_title"),
                    [[Button.inline(i18n.t(user_id, "back_stores"), "bs:0")]], edit=True)
    await event.answer()


# ------------------------------------------------------------------ language
@route("lang")
async def switch_language(event, rest: str) -> None:
    user_id = event.sender_id
    new_lang = i18n.toggle_language(user_id)
    await event.answer("🌐 Language switched to English" if new_lang == "en" else "🌐 ভাষা বাংলা করা হয়েছে")
    from app.handlers.user import show_home
    await show_home(event, user_id, edit=True)


# --------------------------------------------------------------- share cards
@route("sh:")
async def share_card(event, rest: str) -> None:
    """A forwardable card with the store link — free marketing."""
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    stats = db.store_stats(store["id"])
    link = f"https://t.me/{runtime.bot_username}?start=s{store['id']}"
    text = (
        f"🏪 <b>{esc(store['name'])}</b>\n"
        f"📂 {stats['files']} items · 👁 {stats['views']} views\n"
        f"{esc(store['description'] or '')}\n\n"
        f"🔗 <code>{link}</code>\n\n"
        "<i>এই মেসেজটি কপি করে বন্ধুদের সাথে শেয়ার করুন।</i>"
    )
    await ui.render(event, text, [[Button.inline("🔙 Back", f"s:{store['id']}")]], edit=True)
    await event.answer()


# ------------------------------------------------------------ global search
@route("gs")
async def global_search(event, rest: str) -> None:
    user_id = event.sender_id
    ask(user_id, "global_search")
    await ui.render(
        event,
        "🔎 <b>সব স্টোরে সার্চ</b>\nযে নামটি খুঁজছেন লিখে পাঠান "
        "(যেমন <code>interstellar</code>)।",
        [[Button.inline(i18n.t(user_id, "back_stores"), "bs:0")]],
        edit=True,
    )
    await event.answer()


def global_results_buttons(user_id: int, matches: list[dict]) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for file_row in matches:
        store = db.store(file_row["store_id"])
        if store is None or not access.has_access(store, user_id):
            continue
        rows.append([Button.inline(
            f"{'🎬' if file_row['kind'] == 'Video' else '🖼'} "
            f"{esc(file_row['name'])[:24]} · {esc(file_row['store_name'])[:12]}",
            f"f:{file_row['id']}")])
    return rows


# ------------------------------------------------------------- store extras
def store_header(store: dict, user_id: int, total: int, views: int, page: int) -> str:
    """Richer store header: description, stats and access note."""
    lines = [f"🏪 <b>{esc(store['name'])}</b>"]
    if store.get("description"):
        lines.append(f"<i>{esc(store['description'][:200])}</i>")
    lines.append(i18n.t(user_id, "items", n=total) + " · " + i18n.t(user_id, "views", n=views))
    lines.append(f"🔑 {access.access_note(store, user_id)}")
    if total:
        lines.append(f"📄 পৃষ্ঠা {page + 1}")
    return "\n".join(lines)


@route("desc:")
async def set_description(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    ask(admin_id, "store_description", store_id=store["id"])
    current = store.get("description") or "(খালি)"
    await event.respond(f"📝 বর্তমান বর্ণনা:\n<i>{esc(current)}</i>\n\n"
                        "নতুন বর্ণনা লিখে পাঠান, বা <code>clear</code> দিলে মুছে যাবে।")
    await event.answer()


@route("cover:")
async def set_cover_prompt(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    ask(admin_id, "store_cover", store_id=store["id"])
    await event.respond("🖼 এই স্টোরের <b>কভার ছবি</b> পাঠান (একটি ছবি)।\n"
                        "<i>ইউজাররা স্টোর লিংক শেয়ার করলে এটি দেখতে পাবে।</i>")
    await event.answer()


@route("preview:")
async def user_preview(event, rest: str) -> None:
    """Admin sees exactly what a normal user sees."""
    from app.handlers.user import show_home
    await show_home(event, event.sender_id, edit=True)
    await event.answer()
