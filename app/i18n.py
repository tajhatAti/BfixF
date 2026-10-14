"""Localization (বাংলা / English).

Usage:  t(user_id, "home_banner")   or   t(user_id, "items", n=12)

The default language comes from `DEFAULT_LANG` (config.env); every user can
switch with the 🌐 button, and their choice is stored in the database.
"""
from __future__ import annotations

from app.services import settings
from app.storage import db

STRINGS: dict[str, dict[str, str]] = {
    # ------------------------------------------------------------------ common
    "home_banner": {
        "bn": "🎬 <b>স্টোরে স্বাগতম</b>\nনিচ থেকে একটা ক্যাটাগরি বেছে নিন।",
        "en": "🎬 <b>Welcome to the store</b>\nPick a category below to browse.",
    },
    "pick_category": {"bn": "🏪 <b>ক্যাটাগরি বেছে নিন:</b>", "en": "🏪 <b>Choose a category:</b>"},
    "items": {"bn": "📂 {n} টি আইটেম", "en": "📂 {n} items"},
    "views": {"bn": "👁 {n} ভিউ", "en": "👁 {n} views"},
    "back_stores": {"bn": "🔙 স্টোরে ফিরুন", "en": "🔙 Back to stores"},
    "help_button": {"bn": "ℹ️ সাহায্য", "en": "ℹ️ Help"},
    "invite_button": {"bn": "🎁 ইনভাইট ও আয়", "en": "🎁 Invite & Earn"},
    "favorites_button": {"bn": "⭐ আমার ফেভারিট", "en": "⭐ My favorites"},
    "request_button": {"bn": "🙋 কনটেন্ট চাই", "en": "🙋 Request content"},
    "contact_button": {"bn": "📞 অ্যাডমিনের সাথে কথা বলুন", "en": "📞 Contact admin"},
    "language_button": {"bn": "🌐 ভাষা: বাংলা", "en": "🌐 Language: English"},
    "search_button": {"bn": "🔍 এই স্টোরে সার্চ", "en": "🔍 Search this store"},
    "search_all": {"bn": "🔎 সব স্টোরে সার্চ", "en": "🔎 Search all stores"},
    "search_prompt": {
        "bn": "🔍 <b>{store}</b> এর ভিতরে কী খুঁজবেন লিখে পাঠান\n<i>(/cancel লিখলে বাতিল)</i>",
        "en": "🔍 Send a keyword to search inside <b>{store}</b>\n<i>(send /cancel to stop)</i>",
    },
    "no_results": {"bn": "❌ “{kw}” এর জন্য কিছু পাওয়া গেল না।", "en": "❌ Nothing found for “{kw}”."},
    "files_sent_ok": {"bn": "✅ পাঠানো হয়েছে!", "en": "✅ Sent!"},
    "renewal_notice": {
        "bn": "⏳ <b>{store}</b> স্টোরে আপনার অ্যাক্সেস {date} তারিখে শেষ হবে।\n"
              "আরও দেখতে চাইলে এখনই বাড়িয়ে নিন — অ্যাডমিনকে মেসেজ দিন।",
        "en": "⏳ Your access to <b>{store}</b> ends on {date}.\n"
              "Message the admin to renew and keep watching.",
    },
    "expired_notice": {
        "bn": "🔒 <b>{store}</b> স্টোরের অ্যাক্সেসের সময় শেষ। আবার নিতে অ্যাডমিনকে মেসেজ দিন।",
        "en": "🔒 Your access to <b>{store}</b> has ended. Message the admin to renew.",
    },
    "file_removed": {"bn": "এই ফাইলটি সরিয়ে ফেলা হয়েছে।", "en": "This file was removed."},
    "premium_locked": {
        "bn": "🔒 <b>{store}</b> একটি প্রিমিয়াম স্টোর।\nঅ্যাক্সেস পেতে নিচের বাটনে চাপ দিন।",
        "en": "🔒 <b>{store}</b> is a premium store.\nTap the button below to get access.",
    },
    "unlock_button": {"bn": "🔓 অ্যাক্সেস নিন", "en": "🔓 Unlock access"},
    "trial_button": {"bn": "🎁 {h} ঘণ্টা ফ্রি ট্রায়াল", "en": "🎁 {h}h free trial"},
    "trial_used": {"bn": "এই স্টোরে ট্রায়াল আগেই নেওয়া হয়েছে।", "en": "Trial already used for this store."},
    "trial_granted": {"bn": "🎉 {h} ঘণ্টার ফ্রি অ্যাক্সেস চালু হয়েছে!", "en": "🎉 {h}h free access activated!"},
    "my_access_button": {"bn": "💎 আমার অ্যাক্সেস", "en": "💎 My access"},
    "no_access_yet": {"bn": "এখনো কোনো প্রিমিয়াম অ্যাক্সেস নেই।", "en": "You have no premium access yet."},
    # ------------------------------------------------------------------- plans
    "plans_title": {"bn": "💎 <b>{store}</b> — প্ল্যান বেছে নিন", "en": "💎 <b>{store}</b> — choose a plan"},
    "no_plans": {
        "bn": "ℹ️ এই স্টোরের জন্য এখনো কোনো প্ল্যান সেট করা নেই।\nঅ্যাডমিনের সাথে কথা বলুন।",
        "en": "ℹ️ No plans set for this store yet.\nPlease contact the admin.",
    },
    "pay_instruction": {
        "bn": ("💳 <b>{plan}</b> — {price}\n\n"
               "নিচের যেকোনো একটাতে টাকা পাঠান:\n{methods}\n"
               "{note}\n\n"
               "পাঠানোর পর <b>পেমেন্টের স্ক্রিনশট</b> এই চ্যাটে পাঠান — "
               "অ্যাডমিন যাচাই করে অ্যাক্সেস চালু করে দেবেন।"),
        "en": ("💳 <b>{plan}</b> — {price}\n\n"
               "Send the payment to any of these:\n{methods}\n"
               "{note}\n\n"
               "After paying, send the <b>payment screenshot</b> in this chat — "
               "the admin will verify and unlock your access."),
    },
    "no_payment_methods": {
        "bn": "⚠️ অ্যাডমিন এখনো পেমেন্ট নাম্বার সেট করেননি — সরাসরি যোগাযোগ করুন।",
        "en": "⚠️ The admin has not set payment numbers yet — please contact them.",
    },
    "payment_received": {
        "bn": "🧾 আপনার পেমেন্ট রিকোয়েস্ট (#{order}) অ্যাডমিনের কাছে পাঠানো হয়েছে।\n"
              "যাচাই হলে সাথে সাথে জানিয়ে দেওয়া হবে।",
        "en": "🧾 Your payment request (#{order}) was sent to the admin.\n"
              "You'll be notified as soon as it's verified.",
    },
    "order_approved": {
        "bn": "✅ পেমেন্ট যাচাই হয়েছে! <b>{store}</b> এ আপনার অ্যাক্সেস চালু — {days} দিন।",
        "en": "✅ Payment verified! Your <b>{store}</b> access is active for {days} days.",
    },
    "order_rejected": {
        "bn": "❌ দুঃখিত, আপনার পেমেন্ট (#{order}) যাচাই করা যায়নি।\n{note}\nসাহায্যের জন্য অ্যাডমিনের সাথে কথা বলুন।",
        "en": "❌ Sorry, your payment (#{order}) could not be verified.\n{note}\nContact the admin for help.",
    },
    "coupon_button": {"bn": "🎟️ কুপন কোড", "en": "🎟️ Coupon code"},
    "coupon_ask": {"bn": "🎟️ কুপন কোডটি লিখে পাঠান।", "en": "🎟️ Send the coupon code."},
    "coupon_applied": {"bn": "✅ কুপন প্রয়োগ হয়েছে — {desc}", "en": "✅ Coupon applied — {desc}"},
    "coupon_free": {"bn": "{days} দিন ফ্রি অ্যাক্সেস চালু!", "en": "{days} days of free access unlocked!"},
    "coupon_percent": {"bn": "{percent}% ছাড়, এখন দাম {price}", "en": "{percent}% off, price is now {price}"},
    # ----------------------------------------------------------------- support
    "contact_title": {
        "bn": "📞 <b>অ্যাডমিনের সাথে যোগাযোগ</b>\n{note}\n\nনিচে আপনার প্রশ্ন/সমস্যা লিখে পাঠান।",
        "en": "📞 <b>Contact the admin</b>\n{note}\n\nWrite your question or problem below.",
    },
    "contact_sent": {
        "bn": "✅ আপনার মেসেজ অ্যাডমিনের কাছে পৌঁছেছে (টিকেট #{ticket})।\nউত্তর এলে এখানেই পাবেন।",
        "en": "✅ Your message reached the admin (ticket #{ticket}).\nYou'll get the reply right here.",
    },
    "contact_cooldown": {
        "bn": "⏳ একটু ধীরে — {seconds} সেকেন্ড পরে আবার পাঠান।",
        "en": "⏳ Slow down — try again in {seconds} seconds.",
    },
    "contact_reply": {"bn": "📩 <b>অ্যাডমিনের উত্তর:</b>\n{text}", "en": "📩 <b>Admin reply:</b>\n{text}"},
    # --------------------------------------------------------------- requests
    "request_title": {
        "bn": "🙋 <b>কনটেন্ট রিকোয়েস্ট</b>\nযে ফাইল/মুভি খুঁজে পাচ্ছেন না, তার নাম লিখে পাঠান।",
        "en": "🙋 <b>Content request</b>\nSend the name of the file/movie you couldn't find.",
    },
    "request_saved": {"bn": "✅ রিকোয়েস্ট জমা হয়েছে — ধন্যবাদ!", "en": "✅ Request saved — thank you!"},
    # -------------------------------------------------------------- favorites
    "fav_added": {"bn": "⭐ ফেভারিটে যোগ হয়েছে", "en": "⭐ Added to favorites"},
    "fav_removed": {"bn": "ফেভারিট থেকে সরানো হয়েছে", "en": "Removed from favorites"},
    "fav_empty": {"bn": "⭐ এখনো কিছু ফেভারিটে যোগ করা হয়নি।", "en": "⭐ No favorites yet."},
    "fav_title": {"bn": "⭐ <b>আমার ফেভারিট</b>", "en": "⭐ <b>My favorites</b>"},
    # ------------------------------------------------------------ file labels
    "renew_button": {"bn": "🔄 রিনিউ করুন", "en": "🔄 Renew"},
}

ADMIN_STRINGS = {
    "panel_title": {"bn": "⚙️ <b>অ্যাডমিন কন্ট্রোল প্যানেল</b>", "en": "⚙️ <b>Admin control panel</b>"},
}


def lang_of(user_id: int) -> str:
    lang = db.user_lang(user_id) or settings.get_str("DEFAULT_LANG", "bn")
    return lang if lang in ("bn", "en") else "bn"


def t(user_id: int, key: str, **kwargs) -> str:
    """Translate `key` for the given user.

    The owner can rewrite any of these from the panel (⚙️ → 📝 বটের মেসেজ); the
    override is checked first, the built-in wording stays as the fallback.
    """
    from app.services import bot_texts
    lang = lang_of(user_id)
    entry = STRINGS.get(key) or ADMIN_STRINGS.get(key)
    if entry is None:
        own = bot_texts.override(key, lang) or bot_texts.override(key, "bn")
        return own.format(**kwargs) if (own and kwargs) else (own or key)
    own = bot_texts.override(key, lang)
    text = own or entry.get(lang) or entry.get("bn") or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def toggle_language(user_id: int) -> str:
    """Switch bn ⇄ en and return the new language code."""
    new_lang = "en" if lang_of(user_id) == "bn" else "bn"
    db.set_user_lang(user_id, new_lang)
    return new_lang
