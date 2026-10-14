"""One safe flow for every delivery — store link, file link, limited link, search.

Why this module exists
----------------------
Users used to be pushed through a chain of inline buttons ("open store → choose
file → press get → join channel → press get again"). People got confused and
gave up (“ইউজার যেন ভয় না পায়”). From v3 every entry point funnels through
`open_file()`:

    click link  →  access check  →  force-join check (global + this store)
                     │                     │
                     │                     └─ not joined → one message with the
                     │                        join buttons and a single
                     │                        “✅ আমি জয়েন করেছি” button
                     ↓
                send the video **directly** (one tap, no extra buttons)

Everything that happens is written to the `events` table, which is what powers
the per-user and per-store analytics screen in the admin panel.
"""
from __future__ import annotations

from app import runtime
from app.logger import log
from app.services import access, forcejoin
from app.services.sender import deliver
from app.storage import db
from app.utils import esc

GATE_OK = "ok"                 # user may receive the file right now
GATE_JOIN = "join"             # cannot deliver yet — join prompt was shown
GATE_DENIED = "denied"         # paid store and this user has no access
GATE_LIMIT = "limit"           # one-time link is used up
GATE_GONE = "gone"             # file deleted / not found


def log(name: str, user_id: int | None = None, store_id: int | None = None,
        file_id: int | None = None, ref: str | None = None,
        detail: str | None = None) -> None:
    try:
        db.log_event(name, user_id=user_id, store_id=store_id, file_id=file_id,
                     ref=ref, detail=detail)
    except Exception as exc:               # analytics must never break delivery
        log.debug("event log failed (%s): %s", name, exc)


async def gate_check(user_id: int, store_id: int | None) -> dict:
    """Subscription check for the default channel **and** this store's channels.

    Fail-open rule stays: if Telegram refuses to answer we let the user through.
    """
    if access.is_admin(user_id):
        return {"ok": True, "missing": [], "skipped": 0, "checked": 0}
    if not forcejoin.targets(store_id):
        return {"ok": True, "missing": [], "skipped": 0, "checked": 0}
    result = await forcejoin.check_many(user_id, store_id)
    if result["missing"]:
        return result
    if result["checked"]:
        runtime.mark_join(user_id, True)
    return result


async def show_join_gate(event, user_id: int, file_row: dict, missing: list[dict],
                         token: str = "", again: bool = False, chat_id: int | None = None):
    """The only message a not-yet-joined user sees: channels + one check button."""
    from app.services import sender

    store_id = (file_row or {}).get("store_id")
    file_id = (file_row or {}).get("id") or 0
    if file_id:
        db.join_prompt_set(user_id, file_id, token or "")
    log("join_block", user_id, store_id, file_id, token)
    text = forcejoin.gate_text(missing, "again" if again else "fresh")
    buttons = forcejoin.gate_keyboard(user_id, file_id, missing, token)
    target = chat_id if chat_id is not None else event.chat_id
    return await sender.send_text(target, text, buttons=buttons)


def _banned(user_id: int) -> bool:
    if access.is_admin(user_id):
        return False
    try:
        return bool(db.is_banned(user_id))
    except Exception:
        return False


async def open_file(event, file_id: int, *, token: str = "", ref: str = "",
                    page: int = 0, chat_id: int | None = None,
                    caption: str | None = None, buttons=None,
                    via_link: bool = False) -> str:
    """Deliver one file straight to the user (or explain exactly why not)."""
    from app.services import sender
    from app.texts import NOT_FOUND, access_denied

    user_id = event.sender_id
    chat = chat_id if chat_id is not None else event.chat_id
    file_row = db.file(int(file_id)) if file_id else None
    if not file_row:
        log("miss", user_id, None, int(file_id) if file_id else None, ref)
        await sender.send_text(chat, NOT_FOUND)
        return GATE_GONE

    store = db.store(file_row["store_id"])
    if store is None:
        await sender.send_text(chat, NOT_FOUND)
        return GATE_GONE

    log("view_file", user_id, store["id"], file_row["id"], ref or token)
    # A shared link is a deliberate gift: whoever holds it gets its files, even from
    # a premium store. Banned users are still refused.
    if not (via_link and not _banned(user_id)) and not access.has_access(store, user_id):
        log("denied", user_id, store["id"], file_row["id"], ref or token)
        await sender.send_text(chat, access_denied(store["name"]))
        return GATE_DENIED

    gate = await gate_check(user_id, store["id"])
    if gate["missing"]:
        await show_join_gate(event, user_id, file_row, gate["missing"], token, chat_id=chat)
        return GATE_JOIN

    result = (await deliver(chat, file_row, caption, buttons=buttons) if buttons is not None
              else await deliver(chat, file_row, caption))
    if result.ok:
        log("deliver", user_id, store["id"], file_row["id"], ref or token)
        return GATE_OK
    await _delivery_error(chat, result, user_id)
    return GATE_DENIED


async def _delivery_error(chat: int, result, user_id: int) -> None:
    from app.services import sender
    from app.services.sender import FLOOD, NO_SESSION
    from app import texts
    if result.reason == NO_SESSION:
        message = texts.SESSION_NEEDED
    elif result.reason == FLOOD:
        message = texts.FLOOD_WAIT
    else:
        message = texts.NOT_FOUND
    if access.is_admin(user_id) and result.detail:
        message += f"\n<code>{esc(result.detail)}</code>"
    await sender.send_text(chat, message)


# ------------------------------------------------------------------ link clicks
async def open_link(event, token: str, *, chat_id: int | None = None) -> str:
    """A `/start t<token>` click — honours the click limit of that link."""
    from app.services import sender

    user_id = event.sender_id
    chat = chat_id if chat_id is not None else event.chat_id
    link = db.link(token)
    if not link:
        await sender.send_text(chat, "🔗 <b>এই লিংকটি পাওয়া যায়নি</b> — হয়তো মুছে ফেলা হয়েছে।")
        return GATE_GONE

    is_admin = access.is_admin(user_id)

    # This link's own channels — checked BEFORE a click is used up, so a person
    # who still has to join does not burn one of the link's limited clicks.
    if not is_admin:
        from app.services import linkopts
        missing = await linkopts.missing_channels(user_id, token)
        if missing:
            log("join_block", user_id, None, None, token, "link channels")
            await sender.send_text(chat, forcejoin.gate_text(missing, "fresh"),
                                   buttons=linkopts.gate_keyboard(token, missing))
            return GATE_JOIN

    take = db.link_take(token, user_id=user_id) if not is_admin else {
        "ok": True, "clicks": db.link_clicks(token),
        "limit": int(link.get("max_clicks") or 0), "left": -1, "link": link}
    if not take["ok"]:
        if take["reason"] == "user_limit":
            per_user = int(take.get("per_user_limit") or 0)
            log("limit_block", user_id, None, None, token, f"per_user={per_user}")
            await sender.send_text(
                chat,
                f"🙋 <b>আপনি ইতিমধ্যেই নিয়ে নিয়েছেন</b>\n\n"
                f"এই লিংক থেকে একজন সর্বোচ্চ <b>{per_user}</b> বার নিতে পারেন — "
                f"আপনি সেটা ব্যবহার করে ফেলেছেন।\nনতুন করে দরকার হলে অ্যাডমিনকে বলুন।",
                buttons=contact_buttons())
            return GATE_LIMIT
        if take["reason"] == "limit":
            limit = int(take.get("limit") or 0)
            log("limit_block", user_id, None, None, token, f"limit={limit}")
            await sender.send_text(chat, _limit_text(limit, int(take.get("clicks") or 0)),
                                   buttons=contact_buttons())
            return GATE_LIMIT
        log("limit_block", user_id, None, None, token, take["reason"])
        await sender.send_text(chat, "⛔️ <b>এই লিংকের মেয়াদ শেষ</b> — আর কাজ করবে না।",
                               buttons=contact_buttons())
        return GATE_LIMIT

    file_ids = db.link_file_ids(token)
    if not file_ids:
        await sender.send_text(chat, "📭 <b>এই লিংকে এখন কোনো ফাইল নেই।</b>")
        return GATE_GONE

    first = db.file(file_ids[0])
    store = db.store(first["store_id"]) if first else None
    limit = int(take.get("limit") or 0)
    if limit:
        left = take.get("left")
        log("limit_use", user_id, (store or {}).get("id"), first["id"] if first else None,
            token, f"used={take['clicks']}/{limit}")
    # Deliver everything the link holds — filtering out what this user may not see.
    from app.services import linkopts
    link_caption = linkopts.caption_of(link)
    link_buttons = linkopts.buttons_of(link)
    delivered = 0
    for fid in file_ids:
        row = db.file(fid)
        if not row:
            continue
        file_store = db.store(row["store_id"])
        if file_store is None or _banned(user_id):
            continue
        state = await open_file(event, fid, token=token, ref="link", chat_id=chat,
                                caption=link_caption, buttons=link_buttons, via_link=True)
        if state == GATE_JOIN:
            return GATE_JOIN
        if state == GATE_OK:
            delivered += 1
    if not delivered:
        await sender.send_text(
            chat, "🔐 <b>এই লিংকের সব ফাইল আপনার অ্যাক্সেসের বাইরে।</b>\n\n"
                  "স্টোরটি প্রিমিয়াম হলে অ্যাডমিনের সাথে কথা বলুন — ইনলাইন মেনু থেকে "
                  "সরাসরি মেসেজ করতে পারবেন।")
        return GATE_DENIED
    if limit and take.get("left") and take["left"] > 0:
        await sender.send_text(chat, f"ℹ️ <i>এই লিংকটি আর {take['left']} বার খোলা যাবে।</i>")
    return GATE_OK


def contact_buttons() -> list[list]:
    """A way out of a dead end: talk to the admin (contact set in the panel)."""
    from telethon.tl.custom import Button
    from app.services import settings
    username = (settings.get_str("SUPPORT_CONTACT") or "").strip()
    if username.startswith(("http://", "https://", "tg://")):
        return [[Button.url("📞 অ্যাডমিনের সাথে যোগাযোগ", username)]]
    if username.startswith("@"):
        return [[Button.url("📞 অ্যাডমিনের সাথে যোগাযোগ", f"https://t.me/{username[1:]}")]]
    return [[Button.inline("📞 অ্যাডমিনের সাথে কথা বলুন", b"ct:0")]]


def _limit_text(limit: int, used: int) -> str:
    return (f"⛔️ <b>লিমিট শেষ — অ্যাক্সেস দেওয়া হবে না</b>\n\n"
            f"এই লিংকটি সর্বোচ্চ <b>{limit}</b> বার খোলার জন্য বানানো হয়েছিল "
            f"({used}/{limit} ব্যবহার হয়েছে)।\n\n"
            "নতুন লিংক দরকার হলে অ্যাডমিনের সাথে যোগাযোগ করুন।")


async def confirm_join(event, user_id: int, file_id: int, token: str = "") -> str:
    """“আমি জয়েন করেছি” button — re-check and then deliver immediately."""
    from app.services import sender

    file_row = db.file(file_id) if file_id else None
    store_id = file_row["store_id"] if file_row else None
    gate = await gate_check(user_id, store_id)
    if gate["missing"]:
        log("join_retry", user_id, store_id, file_id, token, "still missing")
        await show_join_gate(event, user_id, file_row or {}, gate["missing"], token,
                             again=True, chat_id=event.chat_id)
        return GATE_JOIN

    db.clear_join_prompt(user_id, file_id)
    log("join_click", user_id, store_id, file_id, token)
    if not file_row:
        await sender.send_text(event.chat_id, "✅ <b>জয়েন সফল!</b> এখন যেকোনো ফাইল খুলুন।")
        return GATE_OK
    return await open_file(event, file_row["id"], token=token, ref="join")


async def open_store(event, store_id: int, page: int = 0, edit: bool = True):
    """Store screen — logged for analytics, gated by the store's own channels."""
    from app.ui import show_store

    store = db.store(int(store_id))
    if not store:
        return None
    log("open_store", event.sender_id, store["id"], None, store.get("slug") or "")
    gate = await gate_check(event.sender_id, store["id"])
    if gate["missing"]:
        return await show_join_gate(event, event.sender_id, {"store_id": store["id"]},
                                    gate["missing"])
    return await show_store(event, store, page=page, edit=edit)
