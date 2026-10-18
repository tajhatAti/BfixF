"""Admin action trail — “কে কী করল, কখন করল”.

Everything lands in the existing `events` table, so no extra storage is needed and
the numbers already collected (deliveries, scans, broadcasts) can be read side by
side with the admin actions. `/admin/audit` renders it.

The module is deliberately tiny: one `record()` call at each interesting place.
It never raises — an audit line must not be able to break an admin action.
"""
from __future__ import annotations

import logging

from app.storage import db

log = logging.getLogger(__name__)

#: Which event names the audit page shows. Order = how they are explained in the UI.
AUDIT_EVENTS: dict[str, str] = {
    "settings_saved": "⚙️ সেটিংস বদল",
    "store_created": "🏪 নতুন স্টোর",
    "store_edited": "✏️ স্টোর এডিট",
    "store_deleted": "🗑 স্টোর ডিলিট",
    "store_channel_created": "📡 পাবলিশ চ্যানেল",
    "cache_channel_created": "🗄 ক্যাশ চ্যানেল",
    "cache_channel_reset": "♻️ ক্যাশ রিসেট",
    "broadcast_start": "🚀 ব্রডকাস্ট শুরু",
    "broadcast_scheduled": "🕒 ব্রডকাস্ট শিডিউল",
    "broadcast_cancelled": "⛔ ব্রডকাস্ট বাতিল",
    "files_purged": "🧹 ফাইল মুছে ফেলা",
    "users_purged": "🧹 ইউজার মুছে ফেলা",
    "user_banned": "🚫 ইউজার ব্যান",
    "user_unbanned": "✅ ব্যান তুলে নেওয়া",
    "grant": "💎 অ্যাক্সেস দেওয়া",
    "revoke": "↩️ অ্যাক্সেস কাড়া",
    "link_created": "🔗 লিমিটেড লিংক",
    "draft_shared": "🔗 খসড়ার পাবলিক লিংক",
    "backup_downloaded": "⬇️ ব্যাকআপ নামানো",
    "backup_saved": "💾 ব্যাকআপ সেভ",
    "restore_done": "♻️ ইনস্টল ফিরিয়ে আনা",
    "broadcast_deleted": "🗑 ক্যাম্পেইন মুছে ফেলা",
    "broadcast_test": "🧪 টেস্ট মেসেজ",
    "broadcast_retry": "🔁 আবার পাঠানো",
    "session_start": "📲 নতুন session",
    "session_disconnect": "🔌 session খোলসা",
    "scan": "🔍 চ্যানেল স্ক্যান",
    "texts_saved": "💬 টেক্সট এডিট",
    "reel_saved": "🎞 রিল সাজানো",
    "add_file": "📄 ফাইল যোগ",
}

#: Older/other layers of the app log these; they are worth showing too.
EXTRA_EVENTS = ("auto_post", "channel_post", "mirror_ok")


def record(name: str, *, user_id: int | None = None, store_id: int | None = None,
           file_id: int | None = None, ref: str | None = None,
           detail: str | None = None) -> None:
    """Write one audit line. Unknown names are kept — the page just labels them."""
    try:
        db.log_event(name, user_id=user_id, store_id=store_id, file_id=file_id,
                     ref=ref, detail=(detail or "")[:300] or None)
    except Exception:                                     # pragma: no cover - never fatal
        log.debug("audit write failed for %s", name, exc_info=True)


def label(name: str) -> str:
    return AUDIT_EVENTS.get(str(name), "• " + str(name))


#: Audit rows never fill `events.store_id` (analytics counts those) — the store id
#: travels in `ref` with this prefix instead.
STORE_PREFIX = "s"


def store_from_ref(ref: str) -> int | None:
    """`"s12 session"` → 12. Returns None when the row is not about one store."""
    text = str(ref or "").strip()
    if not text.startswith(STORE_PREFIX):
        return None
    digits = text[1:].split(" ", 1)[0]
    return int(digits) if digits.isdigit() else None


def strip_ref(ref: str) -> str:
    """The ref without the machine part, for display."""
    text = str(ref or "")
    if store_from_ref(text) is None:
        return text
    return text.split(" ", 1)[1] if " " in text else ""


def recent(limit: int = 300, *, since: float | None = None,
           name: str = "", store_id: int | None = None) -> list[dict]:
    """Audit rows, newest first; `name` matches a substring of the event name."""
    wanted = set(AUDIT_EVENTS) | set(EXTRA_EVENTS)
    # store_id is filtered here rather than in SQL: audit rows keep the id in `ref`.
    rows = db.events(limit=max(50, min(limit * 4, 4000)), since=since)
    out = [row for row in rows if str(row.get("name") or "") in wanted]
    if store_id:
        out = [row for row in out
               if store_from_ref(row.get("ref") or "") == int(store_id)]
    for row in out:
        found = store_from_ref(row.get("ref") or "")
        if found and not row.get("store_name"):
            store = db.store(found)
            row["store_name"] = store["name"] if store else f"#{found}"
        row["ref"] = strip_ref(row.get("ref") or "")
    if name:
        needle = name.strip().lower()
        out = [row for row in out if needle in str(row.get("name") or "").lower()
               or needle in str(row.get("detail") or "").lower()
               or needle in str(row.get("store_name") or "").lower()]
    return out[:limit]


def counts(days: int = 30) -> list[tuple[str, int]]:
    """How many audit lines per action in the window — a quick “is it busy?” read."""
    import time as _time

    since = _time.time() - max(1, days) * 86400
    tally: dict[str, int] = {}
    for row in recent(limit=2000, since=since):
        key = str(row.get("name") or "?")
        tally[key] = tally.get(key, 0) + 1
    return sorted(tally.items(), key=lambda pair: pair[1], reverse=True)
