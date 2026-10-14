"""Editable bot texts — change any message the bot sends, straight from the panel.

The owner asked for this in plain words: “একটা লাইন বদলানোর জন্য পুরো সিস্টেম
বানাতে হয় — তাই বটের মেসেজগুলো প্যানেল থেকেই এডিট করা যাবে।”

How it works
------------
Every user-facing string already lives in one of two places:

* ``app.i18n.STRINGS`` — ~120 keys, each with a ``bn`` and an ``en`` version
  (“🎬 স্টোরে স্বাগতম”, “🔒 প্রিমিয়াম স্টোর”, …);
* ``app.services.bot_texts.SYSTEM`` — the messages that are not translated
  (the upload refusal, the force-join gate, help texts, …).

This module puts an *override* in front of both. An override lives in the
database (``meta`` table, key ``bot_text:<lang>:<key>``), so it survives a
restart, needs no redeploy and is editable from the website (⚙️ Panel →
📝 বটের মেসেজ) as well as from the bot.

Nothing is ever lost: the built-in default is the fallback, and ``-`` (or the
♻️ reset button) removes the override. Placeholders like ``{store}`` keep
working — they are filled in by the caller exactly as before.
"""
from __future__ import annotations

from app.logger import log
from app.storage import db

META_PREFIX = "bot_text:"

#: label shown in the panel, in the order the groups appear
GROUPS: dict[str, str] = {
    "start": "🏠 শুরু / মেনু",
    "store": "🏪 স্টোর ব্রাউজ",
    "gate": "🔐 চ্যানেল গেট",
    "plans": "💎 প্ল্যান / পেমেন্ট",
    "files": "🎬 ফাইল পাঠানো",
    "contact": "📞 যোগাযোগ / সাপোর্ট",
    "access": "💎 অ্যাক্সেস / রিনিউ",
    "errors": "⚠️ ভুল / লিমিট",
    "admin": "⚙️ অ্যাডমিন প্যানেল",
}

#: key → group. Anything not listed lands in “অন্যান্য”, so a new i18n key is
#: still editable the moment it is added.
KEY_GROUPS: dict[str, str] = {
    # start / menu
    "home_banner": "start", "pick_category": "start", "back_stores": "start",
    "help_button": "start", "invite_button": "start", "favorites_button": "start",
    "request_button": "start", "contact_button": "start", "language_button": "start",
    "welcome_back": "start", "start_title": "start",
    # store browsing
    "items": "store", "views": "store", "search_button": "store",
    "search_all": "store", "search_prompt": "store", "no_results": "store",
    # force join / gate
    "gate_title": "gate", "gate_again": "gate", "join_button": "gate",
    "joined_ok": "gate", "gate_note": "gate",
    # plans / payment
    "plans_title": "plans", "no_plans": "plans", "pay_instruction": "plans",
    "no_payment_methods": "plans", "payment_received": "plans",
    "order_approved": "plans", "order_rejected": "plans", "coupon_button": "plans",
    "coupon_ask": "plans", "coupon_applied": "plans", "unlock_button": "plans",
    "premium_locked": "plans", "trial_button": "plans", "trial_used": "plans",
    "trial_granted": "plans", "buy_button": "plans",
    # files
    "files_sent_ok": "files", "file_removed": "files", "not_found": "files",
    "session_needed": "files", "flood_wait": "files", "media_only": "files",
    "upload_saved": "files", "upload_hint": "files",
    # contact / support
    "contact_title": "contact", "contact_saved": "contact",
    "request_saved": "contact", "ticket_reply": "contact",
    # access
    "my_access_button": "access", "no_access_yet": "access",
    "renew_button": "access", "renewal_notice": "access", "expired_notice": "access",
    "access_denied": "access",
    # errors / limits
    "unknown_input": "errors", "cancelled": "errors", "limit_reached": "errors",
    "limit_block": "errors", "flow_expired": "errors", "flow_giveup": "errors",
    # admin
    "panel_title": "admin", "admin_help": "admin",
}

#: Texts that live outside i18n. key → (panel label, default text)
SYSTEM: dict[str, tuple[str, str]] = {
    "media_only": (
        "শুধু ভিডিও/ছবি না হলে যা বলে",
        "❌ <b>শুধু ভিডিও বা ছবি নেওয়া হয়</b>\n\n"
        "টেক্সট, লিংক, স্টিকার বা অন্য কোনো ফাইল সেভ হয় না — তাই ভুল করেও কিছু "
        "সেভ হয়ে যায়নি।\n\n"
        "👉 ভিডিও/ছবি <b>সরাসরি এই চ্যাটে পাঠান</b> (forward করলেও চলবে), "
        "সাথে সাথে সেভ হয়ে যাবে।"),
    "upload_saved": ("ভিডিও সেভ হলে নিশ্চিতকরণ", "✅ সেভ হয়েছে — <b>{store}</b> ({kind} #{n})"),
    "gate_title": (
        "চ্যানেল গেট — প্রথম মেসেজ (`{channels}` = চ্যানেলের তালিকা)",
        "🔐 <b>ফাইলটি পেতে আগে চ্যানেল জয়েন করুন</b>\n\n"
        "একবার জয়েন করলেই সব ভিডিও/ফাইল পাবেন:\n{channels}"),
    "gate_again": (
        "চ্যানেল গেট — আবার চেক (`{channels}`)",
        "⛔️ এখনো জয়েন করেননি!\n\nনিচের চ্যানেল জয়েন করে আবার “আমি জয়েন করেছি” "
        "চাপুন:\n{channels}"),
    "gate_note": ("গেটের নিচের ছোট নোট", ""),
    "join_button": ("“জয়েন করেছি” বাটনের লেখা", "✅ আমি জয়েন করেছি"),
    "flow_expired": (
        "প্রশ্নের সময় শেষ হলে যা বলে",
        "⌛️ <b>আগের প্রশ্নটির সময় শেষ হয়ে গেছে</b> — তাই আপনার লেখাটি কোথাও "
        "সেভ হয়নি।\n\nআবার শুরু করতে প্যানেলের বাটনে চাপ দিন।"),
    "flow_giveup": (
        "বারবার ভুল হলে যা বলে",
        "⚠️ <b>বারবার ভুল হচ্ছে — কাজটি বাতিল করলাম।</b>\n\nকিছুই সেভ হয়নি। "
        "আবার শুরু করতে প্যানেলের বাটনে চাপ দিন।"),
    "cancel_button": ("“বাতিল” বাটনের লেখা", "✖️ বাতিল"),
    "flow_cancelled": ("বাতিল হলে যা বলে", "✖️ <b>বাতিল করা হলো</b> — কিছুই সেভ হয়নি।"),
    "help_text": ("ইউজারের সাহায্য (/help)", ""),
    "admin_help": ("অ্যাডমিনের সাহায্য", ""),
    "stuck_upload": (
        "আপলোডে আটকে গেলে অ্যাডমিনের অ্যালার্ট (`{who}`, `{reason}`)",
        "⚠️ <b>একজন আপলোডে আটকে আছেন</b>\n\n{who}\nকারণ: {reason}\n\n"
        "তিনি ভিডিও/ছবি পাঠাতে চেয়েছিলেন কিন্তু অন্য কিছু পাঠিয়েছেন।"),
}
for _key in SYSTEM:
    KEY_GROUPS.setdefault(_key, "files")


# ------------------------------------------------------------------- internals
def _meta_key(key: str, lang: str) -> str:
    return f"{META_PREFIX}{lang}:{key}"


def override(key: str, lang: str = "bn") -> str:
    """The owner's own wording for this key (``""`` when not overridden)."""
    try:
        return db.get_meta(_meta_key(key, lang), "") or ""
    except Exception as exc:                     # database not ready yet
        log.debug("bot_texts.override(%s) failed: %s", key, exc)
        return ""


def default(key: str, lang: str = "bn") -> str:
    """The built-in wording — also what ♻️ reset restores."""
    from app import i18n, texts
    if key in i18n.STRINGS:
        entry = i18n.STRINGS[key]
        return entry.get(lang) or entry.get("bn") or ""
    if key in i18n.ADMIN_STRINGS:
        entry = i18n.ADMIN_STRINGS[key]
        return entry.get(lang) or entry.get("bn") or ""
    builtin = {
        "help_text": texts.HELP_TEXT,
        "admin_help": texts.ADMIN_HELP,
        "not_found": texts.NOT_FOUND,
        "session_needed": texts.SESSION_NEEDED,
        "flood_wait": texts.FLOOD_WAIT,
        "unknown_input": texts.UNKNOWN_INPUT,
        "cancelled": texts.CANCELLED,
        "access_denied": texts.PREMIUM_LOCK,
    }
    if key in builtin and builtin[key]:
        return builtin[key]
    if key == "gate_note":
        from app.services.forcejoin import note
        return note()
    if key in SYSTEM and SYSTEM[key][1]:
        return SYSTEM[key][1]
    if key == "limit_reached":
        return ("🔒 <b>এই লিংকের লিমিট শেষ</b>\n\nসর্বোচ্চ {max} বারের লিংকটি "
                "{used} বার ব্যবহার হয়ে গেছে।")
    if key == "limit_block":
        return ("🙋 <b>আপনি ইতিমধ্যেই নিয়ে নিয়েছেন</b>\n\nব্যক্তিগত সীমা: "
                "সর্বোচ্চ {per_user} বার।")
    return SYSTEM.get(key, ("", ""))[1]


def is_overridden(key: str, lang: str = "bn") -> bool:
    return bool(override(key, lang))


def set(key: str, value: str, lang: str = "bn") -> tuple[bool, str]:
    """Save (or with an empty value / ``-``, reset) an owner edit."""
    lang = lang if lang in ("bn", "en") else "bn"
    known = key in i18n_keys() or key in SYSTEM
    if not known:
        return False, "এই মেসেজটি চেনা গেল না"
    value = (value or "").strip()
    if value in ("-", "reset", "♻️", "ডিফল্ট"):
        db.set_meta(_meta_key(key, lang), "")
        return True, ""
    if not value:
        db.set_meta(_meta_key(key, lang), "")
        return True, ""
    if len(value) > 4000:
        return False, "মেসেজ অনেক বড় (সর্বোচ্চ ৪০০০ অক্ষর)"
    db.set_meta(_meta_key(key, lang), value)
    return True, ""


def reset_all() -> int:
    """Drop every override (used by the ♻️ reset-all button)."""
    count = 0
    for key in i18n_keys():
        for lang in ("bn", "en"):
            if override(key, lang):
                db.set_meta(_meta_key(key, lang), "")
                count += 1
    for key in SYSTEM:
        for lang in ("bn", "en"):
            if override(key, lang):
                db.set_meta(_meta_key(key, lang), "")
                count += 1
    return count


def i18n_keys() -> list[str]:
    from app import i18n
    return [*i18n.STRINGS.keys(), *i18n.ADMIN_STRINGS.keys()]


def render(key: str, lang: str = "bn", **kwargs) -> str:
    """The text to actually send: owner override → built-in default."""
    text = override(key, lang) or default(key, lang)
    if kwargs and text:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def label(key: str) -> str:
    """A human title for the panel (the text itself would be too long)."""
    if key in SYSTEM:
        return SYSTEM[key][0]
    text = default(key, "bn")
    return text.splitlines()[0][:60] if text else key


def catalog(lang: str = "bn") -> list[dict]:
    """Everything the panel needs, grouped for display."""
    rows = []
    for key in i18n_keys() + [k for k in SYSTEM if k not in i18n_keys()]:
        text = default(key, lang)
        if not text and key not in SYSTEM:
            continue
        rows.append({
            "key": key,
            "group": GROUPS.get(KEY_GROUPS.get(key, "other"), "📦 অন্যান্য"),
            "label": label(key),
            "default": text,
            "value": override(key, lang),
            "edited": bool(override(key, lang)),
        })
    rows.sort(key=lambda row: (row["group"], row["key"]))
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
    return [{"group": name, "rows": items} for name, items in groups.items()]
