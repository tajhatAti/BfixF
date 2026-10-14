"""Media guard — decides what may enter a store.

The old bot accepted *anything* that had `event.media` set. Telegram sets that on
a plain text message too, as soon as it carries a link preview
(`MessageMediaWebPage`). So pasting a link — or any text that got a preview —
created a new store item ("Item_1", "Item_2", …), over and over. That is exactly
the “যেকোনো টেক্সট দিলে বারে বারে সেভ হইতেছে” bug.

Rule from now on:

    ✅ video            → saved
    ✅ photo            → saved
    ❌ text / link preview, sticker, GIF, file, audio, voice, poll
                        → politely refused, **nothing is written to the database**

The allow-list lives in the settings registry (`CONTENT_MEDIA_KINDS`) so an admin
can widen it from the panel later, but the default is strictly video + photo.
"""
from __future__ import annotations

from app.services import settings

VIDEO, PHOTO = "Video", "Photo"
KIND_BY_NAME = {"video": VIDEO, "photo": PHOTO, "audio": "Audio",
                "document": "Document", "voice": "Audio"}

REJECT_TEXT = (
    "❌ <b>শুধু ভিডিও বা ছবি নেওয়া হয়</b>\n\n"
    "টেক্সট, লিংক, স্টিকার বা অন্য কোনো ফাইল সেভ হয় না — তাই ভুল করেও কিছু "
    "সেভ হয়ে যায়নি।\n\n"
    "👉 ভিডিও/ছবি <b>সরাসরি এই চ্যাটে পাঠান</b> (forward করলেও চলবে), "
    "সাথে সাথে সেভ হয়ে যাবে।"
)


def allowed_kinds() -> set[str]:
    raw = settings.get_str("CONTENT_MEDIA_KINDS") or "video,photo"
    kinds = {KIND_BY_NAME.get(part.strip().lower(), part.strip().capitalize())
             for part in raw.split(",") if part.strip()}
    return kinds or {VIDEO, PHOTO}


def classify(message) -> tuple[str | None, str]:
    """Return (kind, reason). `kind` is None when the message must be refused."""
    if message is None:
        return None, "empty"
    media = getattr(message, "media", None)
    if media is None:
        return None, "text"
    # A text message with a link preview is NOT media. Telegram sets `.media` on
    # those, which is exactly how stray text used to become a store item.
    if media.__class__.__name__ in ("MessageMediaWebPage", "MessageMediaEmpty"):
        return None, "text"
    if getattr(media, "webpage", None) is not None:
        return None, "text"
    if getattr(message, "sticker", None) or getattr(message, "gif", None):
        return None, "sticker"
    if getattr(message, "poll", None) or getattr(message, "geo", None) \
            or getattr(message, "contact", None):
        return None, "other"

    kind = None
    if getattr(message, "video", None):
        kind = VIDEO
    elif getattr(message, "photo", None):
        kind = PHOTO
    elif getattr(message, "audio", None) or getattr(message, "voice", None):
        kind = "Audio"
    elif getattr(message, "document", None):
        kind = "Document"
    elif getattr(message, "file", None):
        kind = "File"
    if kind is None:
        return None, "other"
    if kind not in allowed_kinds():
        return None, kind.lower()
    return kind, ""


def is_plain_text(message) -> bool:
    """True for anything that must never become a store item."""
    kind, _reason = classify(message)
    return kind is None


def reject_text(reason: str) -> str:
    from app.services import bot_texts
    if reason in ("text", "empty"):
        return bot_texts.render("media_only")
    label = {"sticker": "স্টিকার / GIF", "audio": "অডিও / ভয়েস",
             "document": "ডকুমেন্ট / ফাইল", "file": "ফাইল",
             "other": "এই ধরনের কনটেন্ট"}.get(reason, "এই কনটেন্ট")
    kinds = ", ".join(sorted(allowed_kinds()))
    return (f"❌ <b>{label} নেওয়া হয় না</b>\n\n"
            f"এই স্টোরে শুধু <b>{kinds}</b> রাখা যায়, তাই কিছুই সেভ হয়নি।")


def display_name(message, kind: str, index: int) -> str:
    """A readable item name: caption if present, otherwise “Video 3”."""
    from app.services.textclean import clean_text
    caption = clean_text((getattr(message, "message", "") or "").strip()).splitlines()
    if caption:
        name = caption[0].strip()[:60]
        if name:
            return name
    return f"{kind} {index}"


def duration_of(message) -> int | None:
    video = getattr(message, "video", None)
    return getattr(video, "duration", None) if video else None
