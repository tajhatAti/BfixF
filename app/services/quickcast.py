"""`/br` — the broadcast an admin builds right inside Telegram.

The admin sends `/br`, drops a video / photo / text / buttons, presses send and
the whole audience gets it. What used to be a three-step, memory-only flow is now
a real studio:

* **Media** — one or many videos/photos (they go out as a single album), plus any
  store file chosen from the panel. Only `(chat_id, msg_id)` is remembered, so a
  restart between “attach” and “send” loses nothing.
* **Options** — audience (all / premium / free / one store), silent, forward
  protection, the automatic “🟢 online” button, scheduling.
* **Drafts** — every step is written to the database (`br_drafts`), so a forgotten
  session or a killed process can never lose the content.
* **Delivery** — the very same campaign engine as the website studio
  (`app/services/broadcast.py`): a per-user queue in SQLite, access-hash based
  addressing (`app/services/peers.py`), batching, FloodWait safety, live progress,
  cancel, and a “why did this person not get it” explanation per recipient.

`send_all()` is kept as the one-call entry point used by older code paths and by
the tests.
"""
from __future__ import annotations

import json
import re
import time
from html import unescape
from dataclasses import dataclass, field
from typing import Any

from telethon import Button
from telethon.extensions import html as tl_html

from app.logger import log
from app.runtime import blocked_users, bot
from app.services import settings
from app.services.broadcast import SEND_AT_ONCE, resolve_audience  # noqa: F401 (re-export)
from app.storage import db
from app.utils import parse_button_spec

#: Telegram's caption limit for photos / videos
CAPTION_LIMIT = 1024
#: How many media items one broadcast may carry (Telegram albums stop at 10)
MAX_MEDIA = 10
#: How many *separate messages* a multi-item draft may hold (ported from the
#: standalone `broadcast.py`, which let an admin stack as many as they liked).
MAX_ITEMS = 20
_HTML_TAG = re.compile(r"</?(b|i|u|s|a|em|strong|code|pre|strike|del|ins|tg-spoiler|blockquote)\b[^>]*>",
                       re.I)


@dataclass
class DraftItem:
    """One message of a *multi-message* draft (the standalone bot's model).

    The archive collected everything an admin sent — text, photo, video, GIF,
    file, audio, voice — as a list, showed “➕ Added #3”, and then `/send` played
    the list back to every user, one Telegram message each. A `DraftItem` is that
    single collected message: the kind of media, where the bot can read it from
    (`media_key` = `chat_id:msg_id`), and the text/caption.
    """
    kind: str = "text"                 # text|photo|video|animation|document|audio|voice
    text: str = ""                     # HTML caption / message text
    media_key: str = ""                # "chat_id:msg_id" — a message the bot can read
    file_id: int = 0                   # a store file id, when attached from the panel
    name: str = ""                     # original file name (for the UI)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "media_key": self.media_key,
                "file_id": int(self.file_id or 0), "name": self.name}

    @classmethod
    def from_dict(cls, raw: dict) -> "DraftItem":
        return cls(kind=(raw.get("kind") or "text")[:16], text=raw.get("text") or "",
                   media_key=str(raw.get("media_key") or ""),
                   file_id=int(raw.get("file_id") or 0),
                   name=(raw.get("name") or "")[:200])

    @property
    def is_media(self) -> bool:
        return self.kind != "text" and bool(self.media_key or self.file_id)

    def icon(self) -> str:
        return {"text": "💬", "photo": "🖼", "video": "🎬", "animation": "🎞",
                "document": "📄", "audio": "🎵", "voice": "🎤"}.get(self.kind, "📦")

    def line(self, index: int) -> str:
        plain = unescape(re.sub(r"<[^>]+>", "", self.text or ""))
        first = plain.strip().splitlines()[:1]
        summary = (first[0][:60] if first else "(ক্যাপশন নেই)")
        return f"{index}. {self.icon()} <b>{self.kind}</b> — {esc_line(summary)}"


def esc_line(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")



@dataclass
class MediaItem:
    """One attached video/photo — a pointer to a message the bot can read."""
    chat_id: int = 0
    msg_id: int = 0
    kind: str = "video"
    name: str = ""

    def as_dict(self) -> dict:
        return {"chat_id": int(self.chat_id), "msg_id": int(self.msg_id),
                "kind": self.kind, "name": self.name}

    @classmethod
    def from_dict(cls, raw: dict) -> "MediaItem":
        return cls(chat_id=int(raw.get("chat_id") or raw.get("chat") or 0),
                   msg_id=int(raw.get("msg_id") or raw.get("msg") or 0),
                   kind=(raw.get("kind") or "video")[:24],
                   name=(raw.get("name") or "")[:200])


@dataclass
class Content:
    text: str = ""                       # already HTML
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    media: list[MediaItem] = field(default_factory=list)
    audience: str = "all"
    silent: bool = False
    protect: bool = True
    online_button: int | None = None     # None = follow the global setting
    items: list[DraftItem] = field(default_factory=list)   # multi-message draft

    # ---------------------------------------------------------------- helpers
    def is_empty(self) -> bool:
        return not self.media and not self.items and not self.text.strip()

    def summary(self) -> str:
        parts = []
        if self.items:
            kinds: dict[str, int] = {}
            for item in self.items:
                kinds[item.kind] = kinds.get(item.kind, 0) + 1
            detail = ", ".join(f"{kind}×{count}" for kind, count in kinds.items())
            parts.append(f"✅ আলাদা মেসেজ ({len(self.items)}টি — {detail})")
        if self.media:
            kinds = ", ".join(item.kind for item in self.media)
            parts.append(f"✅ মিডিয়া ({len(self.media)}টি — {kinds})")
        if self.text.strip():
            chars = len(re.sub(r"<[^>]+>", "", self.text))
            parts.append(f"✅ টেক্সট ({chars} অক্ষর)")
        if self.buttons:
            parts.append(f"✅ বাটন ({sum(len(r) for r in self.buttons)}টি)")
        if self.silent:
            parts.append("🔕 সাইলেন্ট")
        if not self.protect:
            parts.append("↗️ ফরওয়ার্ড চালু")
        return " · ".join(parts) or "কিছু নেই"

    def button_lines(self) -> str:
        return "\n".join(f"{label} | {url}" for row in self.buttons for label, url in row)

    def as_dict(self) -> dict:
        return {"text": self.text, "buttons": self.button_lines(),
                "media": [item.as_dict() for item in self.media],
                "items": [item.as_dict() for item in self.items],
                "audience": self.audience, "silent": self.silent,
                "protect": self.protect, "online_button": self.online_button,
                "updated": time.time()}

    @classmethod
    def from_dict(cls, raw: dict) -> "Content":
        content = cls()
        raw = raw or {}
        content.text = raw.get("text") or ""
        content.buttons = parse_button_spec(raw.get("buttons") or "")
        content.media = [MediaItem.from_dict(item) for item in (raw.get("media") or [])
                         if isinstance(item, dict)][:MAX_MEDIA]
        content.items = [DraftItem.from_dict(item) for item in (raw.get("items") or [])
                         if isinstance(item, dict)][:MAX_ITEMS]
        content.audience = raw.get("audience") or "all"
        content.silent = bool(raw.get("silent"))
        content.protect = bool(raw.get("protect", True))
        content.online_button = raw.get("online_button")
        return content


# ------------------------------------------------------------------ text/html
def to_html(text: str, entities=None) -> str:
    """Typed HTML tags are kept as written; Telegram's own formatting is converted."""
    text = (text or "").strip()
    if not text:
        return ""
    if _HTML_TAG.search(text):
        return text
    return tl_html.unparse(text, entities or [])


def looks_like_buttons(text: str) -> bool:
    """True when every non-empty line is a `label | link` button line."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return bool(lines) and len(parse_button_spec(text)) == len(lines)


def add_text(content: Content, text: str, entities=None) -> str:
    """Put a typed message into the right slot. Returns what it was ('buttons'/'text')."""
    if looks_like_buttons(text):
        content.buttons = parse_button_spec(text)
        return "buttons"
    content.text = to_html(text, entities)
    return "text"


def add_media(content: Content, message) -> MediaItem | None:
    """Attach the media of a message (the video/photo the admin just sent)."""
    from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

    media = getattr(message, "media", None)
    if not isinstance(media, (MessageMediaPhoto, MessageMediaDocument)):
        return None
    kind = "photo" if isinstance(media, MessageMediaPhoto) else _kind_of(media)
    name = (getattr(message, "file", None) and getattr(message.file, "name", "")) or ""
    item = MediaItem(chat_id=int(getattr(message, "chat_id", 0) or 0),
                     msg_id=int(getattr(message, "id", 0) or 0), kind=kind, name=name[:200])
    if not item.msg_id:
        return None
    content.media.append(item)
    del content.media[:-MAX_MEDIA]            # newest 10 win
    # The same item also lands in the media inbox (`/bm`), so the *website* studio
    # can attach it later by name instead of asking the owner to forward it again.
    try:
        size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
        db.add_bc_media(item.chat_id, item.msg_id, item.kind, item.name, size)
    except Exception as exc:
        log.debug("could not add %s to the media inbox: %s", item.msg_id, exc)
    return item


def _kind_of(media) -> str:
    mime = (getattr(getattr(media, "document", None), "mime_type", "") or "").lower()
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def add_to_inbox(message) -> MediaItem | None:
    """Remember a video/photo the owner forwarded for broadcasts (see `/bm`).

    The media inbox is how the *website* studio can attach something that is not a
    store file: the owner forwards it to the bot once, the site lists it by name.
    """
    from app.storage import db
    from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

    media = getattr(message, "media", None)
    if not isinstance(media, (MessageMediaPhoto, MessageMediaDocument)):
        return None
    kind = "photo" if isinstance(media, MessageMediaPhoto) else _kind_of(media)
    name = (getattr(message, "file", None) and getattr(message.file, "name", "")) or ""
    size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
    item_id = db.add_bc_media(int(message.chat_id), int(message.id), kind, name[:200], size)
    return MediaItem(chat_id=int(message.chat_id), msg_id=int(message.id),
                     kind=kind, name=name[:200] or f"#{item_id}")


# ------------------------------------------------- multi-message draft collector
# Ported from the standalone `broadcast.py`: while a draft is open, *everything*
# the admin sends is collected — text, photo, video, GIF, file, audio, voice —
# and later sent as that exact list of Telegram messages, one per item.
def media_kind_of(message) -> str:
    """Which kind of item this message is (the archive's `extract()` type)."""
    from telethon.tl.types import (MessageMediaDocument, MessageMediaPhoto)

    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        document = getattr(media, "document", None)
        attributes = getattr(document, "attributes", None) or []
        for attribute in attributes:
            name = type(attribute).__name__
            if name == "DocumentAttributeAnimated":
                return "animation"                     # a GIF
            if name == "DocumentAttributeAudio":
                return "voice" if getattr(attribute, "voice", False) else "audio"
            if name == "DocumentAttributeVideo":
                return "video"
        mime = (getattr(document, "mime_type", "") or "").lower()
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("audio/"):
            return "audio"
        return "document"
    return "text"


def add_item(content: Content, message, *, keep=False, text: str | None = None,
             entities=None) -> DraftItem | None:
    """Collect one message into the multi-message draft (the archive's `➕ Added`).

    The admin's own HTML tags are kept exactly as typed; Telegram's native
    formatting (bold/italic/…) is converted to HTML so nothing is lost. `text`
    lets a caller pass the raw text it already has (`event.raw_text`), which is
    what the bot handler does — the message object is not always the source.

    Everything goes into `content.items`; `content.text`/`content.media` stay for
    code that still builds a single message by hand.
    """
    from telethon.tl.types import MessageMediaWebPage

    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaWebPage):
        media = None                                  # a link preview is not media
    raw_text = (text if text is not None else
                (getattr(message, "text", None) or getattr(message, "caption", None) or "")
                ).strip()
    if entities is None:
        entities = getattr(message, "entities", None)
    kind = media_kind_of(message) if media is not None else "text"
    html = to_html(raw_text, entities) if raw_text else ""
    name = (getattr(message, "file", None) and getattr(message.file, "name", "")) or ""
    item = DraftItem(kind=kind, text=html,
                     media_key=(f"{int(getattr(message, 'chat_id', 0) or 0)}:"
                                f"{int(getattr(message, 'id', 0) or 0)}"
                                if kind != "text" else ""),
                     name=name[:200])
    if kind == "text" and not item.text:
        return None
    if kind != "text" and not item.media_key.strip(":").split(":")[-1]:
        return None
    if not keep:
        content.media.clear()
        content.text = ""
    content.items.append(item)
    del content.items[:-MAX_ITEMS]                    # newest 20 win
    if kind != "text":
        try:
            size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
            db.add_bc_media(int(getattr(message, "chat_id", 0) or 0),
                            int(getattr(message, "id", 0) or 0), kind, item.name, size)
        except Exception as exc:
            log.debug("could not add item %s to the media inbox: %s", item.media_key, exc)
    return item


def drop_item(content: Content) -> DraftItem | None:
    """Remove the last collected item (archive’s “undo”)."""
    return content.items.pop() if content.items else None


def item_lines(content: Content, limit: int = 20) -> list[str]:
    return [item.line(index) for index, item in enumerate(content.items[:limit], start=1)]


def item_kinds(content: Content) -> dict[str, int]:
    tally: dict[str, int] = {}
    for item in content.items:
        tally[item.kind] = tally.get(item.kind, 0) + 1
    return tally



def markup_for(content: Content):
    return [[Button.url(label, url) for label, url in row] for row in content.buttons] or None


def preview(content: Content, name: str = "রহিম") -> str:
    from app.services.broadcast import personalize
    text = personalize(content.text, name, "")
    if not text and content.media:
        text = "🎬 <b>নতুন ভিডিও</b>"
    return text


# -------------------------------------------------------------------- drafts
def load_draft(admin_id: int) -> Content | None:
    """The last saved draft of this admin (None when there is nothing saved)."""
    row = db.br_draft(admin_id)
    if not row or not row.get("data"):
        return None
    try:
        return Content.from_dict(json.loads(row["data"]))
    except Exception as exc:
        log.debug("broken /br draft for %s: %s", admin_id, exc)
        return None


def save_draft(admin_id: int, content: Content) -> None:
    try:
        db.save_br_draft(admin_id, json.dumps(content.as_dict()))
    except Exception as exc:
        log.debug("could not save /br draft: %s", exc)


def drop_draft(admin_id: int) -> None:
    try:
        db.clear_br_draft(admin_id)
    except Exception:
        pass


# ------------------------------------------------------------------- sending
def slow_pause(targets: int, minutes: float) -> float:
    """Seconds between two sends when the owner wants the list spread over N minutes.

    Telegram allows ~30 messages/second in total, and its own advice for very large
    broadcasts is to spread them out — this turns “৩০ মিনিটে সবাইকে” into the exact
    per-message pause, capped so a huge audience still finishes in a sane time.
    """
    targets = max(1, int(targets))
    seconds = max(0.0, float(minutes) * 60.0)
    if not seconds:
        return 0.0
    return max(0.05, min(300.0, seconds / targets))


async def send_all(admin_id: int, content: Content, *, audience: str | None = None,
                   progress=None, notify_admin: bool = False,
                   test: list[int] | None = None,
                   scheduled_at: float | None = None,
                   slow_minutes: float | None = None) -> dict:
    """Send `content` to an audience through the persistent campaign engine.

    * ``test`` — send to these ids only (the admin checks it first, nobody else).
    * ``scheduled_at`` — queue it and let the scheduler fire it later.

    Returns ``{campaign_id, sent, failed, blocked, total, pending, seconds, status}``
    — the same numbers the website shows on the campaign page, because it *is* the
    same campaign.
    """
    from app.services import broadcast

    started = time.time()
    audience = audience or content.audience or "all"
    media = [item.as_dict() for item in content.media]
    items = [item.as_dict() for item in content.items]
    targets = target_count(content, admin_id)
    pause = slow_pause(targets, slow_minutes) if slow_minutes else 0.0
    title = ""
    if content.items:
        # Multi-message draft (ported from the standalone `broadcast.py`): the title
        # is the first thing the admin typed, and every item keeps its own text.
        first_text = next((i.text for i in content.items if i.text), "")
        title = (re.sub(r"<[^>]+>", "", first_text).strip().splitlines() or [""])[0][:40]
        title = title or f"{len(items)} item draft"
    elif content.text:
        title = content.text.strip().splitlines()[0][:40]
    else:
        title = f"{len(media)} media item(s)"
    campaign = broadcast.create_campaign(
        admin_id, text=content.text, audience=audience, files=[],
        start=False, buttons=content.button_lines(), media=media,
        silent=content.silent, protect=content.protect,
        online_button=content.online_button, origin="br",
        scheduled_at=scheduled_at, pause_seconds=pause, title=title,
    )
    campaign_id = campaign["id"]
    if items:
        db.set_campaign_groups(campaign_id, items)
    if content.button_lines():
        broadcast.store_buttons(campaign_id, content.button_lines())
    if scheduled_at and scheduled_at > time.time() + 5:
        db.update_campaign(campaign_id, status=broadcast.SCHEDULED)
        broadcast.forget_payload(campaign_id)
        return {"campaign_id": campaign_id, "sent": 0, "failed": 0, "blocked": 0,
                "pending": campaign["total"], "total": campaign["total"],
                "seconds": 0.0, "status": broadcast.SCHEDULED,
                "scheduled_at": scheduled_at}

    if test:
        outcome = await broadcast.test_send(campaign_id, list(test))
        return {"campaign_id": campaign_id, "sent": outcome.get("sent", 0),
                "failed": outcome.get("failed", 0), "blocked": 0, "pending": 0,
                "total": len(test), "seconds": time.time() - started,
                "status": "test", "error": (outcome.get("errors") or [""])[0]}

    db.update_campaign(campaign_id, status=broadcast.QUEUED)
    result = await broadcast.run_campaign(campaign_id, progress=progress,
                                          notify_admin=notify_admin)
    counts = db.campaign_counts(campaign_id, sync=False)
    return {
        "campaign_id": campaign_id,
        "sent": counts["sent"], "failed": counts["failed"],
        "blocked": counts["blocked"], "pending": counts["pending"],
        "total": counts["total"], "seconds": time.time() - started,
        "status": result.get("status") or "",
    }


# ------------------------------------------------------------------- reporting
def report_campaign(campaign_id: int) -> tuple[bytes, dict]:
    """CSV report of a campaign — who received it and who did not (archive’s /send)."""
    from app.services import broadcast
    return broadcast.report_bytes(campaign_id)


def failure_lines(campaign_id: int, limit: int = 30) -> list[str]:
    from app.services import broadcast
    return broadcast.failure_lines(campaign_id, limit=limit)


def campaign_items(campaign_id: int) -> list[dict]:
    """The multi-message plan of a campaign (empty for single-message campaigns)."""
    return db.campaign_groups(campaign_id)


def describe_campaign(campaign_id: int) -> str:
    rows = db.campaign_groups(campaign_id)
    if not rows:
        return ""
    tally: dict[str, int] = {}
    for row in rows:
        tally[row.get("kind") or "text"] = tally.get(row.get("kind") or "text", 0) + 1
    icons = {"text": "💬", "photo": "🖼", "video": "🎬", "animation": "🎞",
             "document": "📄", "audio": "🎵", "voice": "🎤"}
    return (f"{len(rows)} টি মেসেজ — "
            + " · ".join(f"{icons.get(kind, '📦')} {count}" for kind, count in tally.items()))


def share_draft(campaign_id: int) -> str:
    """Give the draft a public link so a colleague can look before it is sent."""
    from secrets import token_urlsafe

    rows = db.campaign_links(campaign_id, kind="draft")
    token = rows[0]["token"] if rows else token_urlsafe(9).replace("-", "").replace("_", "")
    db.add_campaign_link(campaign_id, token, label="খসড়া", url="", kind="draft")
    return token


def target_count(content: Content, admin_id: int) -> int:
    try:
        return len(resolve_audience(admin_id, content.audience or "all"))
    except Exception:
        return 0


def audience_label(spec: str) -> str:
    from app.services.broadcast import audience_label as label
    return label(spec)


def dashboard() -> dict:
    """Small live picture for the studio header (used by /br and the panel)."""
    from app.services import broadcast, peers
    return {
        "peers": peers.stats(),
        "running": [c for c in db.campaigns(limit=5)
                    if c["status"] in broadcast.ACTIVE_STATUSES],
        "campaigns": db.campaign_count(),
        "sends_at_once": SEND_AT_ONCE,
        "reach_days": settings.get_int("BROADCAST_REACH_DAYS", 0),
    }


__all__ = ["Content", "MediaItem", "DraftItem", "add_media", "add_text",
           "add_item", "drop_item", "item_lines", "item_kinds", "media_kind_of",
           "report_campaign", "failure_lines", "campaign_items", "describe_campaign",
           "share_draft", "preview", "send_all",
           "load_draft", "save_draft", "drop_draft", "target_count", "audience_label",
           "dashboard", "markup_for", "looks_like_buttons", "to_html", "CAPTION_LIMIT",
           "MAX_MEDIA", "SEND_AT_ONCE", "blocked_users", "bot"]
