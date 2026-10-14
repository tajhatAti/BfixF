"""All user facing strings in one place (HTML formatting).

Keeping them here makes it easy to add another language later and guarantees the
whole bot uses the same wording.
"""
from __future__ import annotations

from app.utils import esc

HOME_BANNER = "🎬 <b>Welcome to the Store</b>\nPick a category below to browse."
ADMIN_BANNER = "⚙️ <b>Admin Control Panel</b>"
BACK_PANEL = "🔙 Back to Panel"
BACK_STORES = "🔙 Back to Stores"

PREMIUM_LOCK = (
    "🔒 <b>{store}</b> is a premium store.\n"
    "You don't have access yet — tap the button below to request or buy access."
)
NO_ACCESS_ALERT = "🔒 Premium store — you don't have access."
FORCE_JOIN = "⚠️ <b>Please join our channel first to unlock the files.</b>"
NOT_FOUND = "❌ File not found, or the admin session is disconnected."
SESSION_NEEDED = (
    "❌ <b>Could not deliver this file.</b>\n"
    "The admin needs to connect a userbot session (/session) for private channels."
)
FLOOD_WAIT = "⏳ Telegram is rate limiting us. The file will be sent automatically in a moment — please wait."
CANCELLED = "🚫 Cancelled. Nothing was changed."
UNKNOWN_INPUT = "🤷 I didn't understand that. Send /cancel to abort."

HELP_TEXT = (
    "ℹ️ <b>How to use this bot</b>\n\n"
    "• /start — browse all stores\n"
    "• Tap a category → tap a file to receive it\n"
    "• 🔍 Search inside a store by keyword\n"
    "• 🔔 Subscribe for daily updates from a store\n"
    "• 🎁 Invite &amp; Earn — share your link and unlock access\n\n"
    "Received files are saved in this chat. Tap ⋮ on a file to download it."
)

ADMIN_HELP = (
    "🛠️ <b>Admin quick help</b>\n\n"
    "• 📌 Active store — every new upload/scan goes here\n"
    "• 🔒/🌐 tap the store button to switch premium ↔ free\n"
    "• 🗂 Manage Files — rename, delete or move files\n"
    "• 📊 Stats — users, views, top files\n"
    "• 💾 Backup — write a JSON snapshot right now\n"
    "• /cancel — abort whatever the bot is asking you"
)


def access_denied(store_name: str) -> str:
    return PREMIUM_LOCK.format(store=esc(store_name))


# ------------------------------------------- persistent bottom (reply) keyboard
# These four texts are always visible under the message box. They are handled by
# a dedicated handler and ignored by every "waiting for your input" flow, so a
# user can always get back to the store list without knowing any command.
REPLY_HOME = "🏠 Store list"
REPLY_SEARCH = "🔍 Search"
REPLY_ACCESS = "💎 My access"
REPLY_HELP = "🆘 Help"

REPLY_BUTTONS = {REPLY_HOME, REPLY_SEARCH, REPLY_ACCESS, REPLY_HELP}
