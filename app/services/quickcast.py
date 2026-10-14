"""Minimal broadcast behind `/br`: collect the content, send it to everyone at once.

No queue, no segments, no pacing settings. The admin sends a video / photo, a text
(HTML allowed) and optional inline buttons, then `/done` - every user gets it in
about a second (up to SEND_AT_ONCE messages are in flight together).
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from telethon import Button
from telethon.extensions import html as tl_html

from app.logger import log
from app.runtime import blocked_users, bot
from app.services.broadcast import SEND_AT_ONCE, _classify, resolve_audience
from app.services.telegram import safe_call
from app.utils import parse_button_spec

#: Telegram's caption limit for photos / videos
CAPTION_LIMIT = 1024
_HTML_TAG = re.compile(r"</?(b|i|u|s|a|em|strong|code|pre|strike|del|ins|tg-spoiler|blockquote)\b[^>]*>",
                       re.I)


@dataclass
class Content:
    media: Any = None
    text: str = ""                      # already HTML
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return self.media is None and not self.text.strip()

    def summary(self) -> str:
        parts = []
        if self.media is not None:
            parts.append("✅ মিডিয়া")
        if self.text.strip():
            parts.append("✅ টেক্সট")
        if self.buttons:
            parts.append(f"✅ বাটন ({sum(len(r) for r in self.buttons)}টি)")
        return " · ".join(parts) or "কিছু নেই"


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


def _markup(buttons):
    return [[Button.url(label, url) for label, url in row] for row in buttons] or None


async def _send_one(uid: int, c: Content) -> None:
    markup = _markup(c.buttons)
    if c.media is None:
        await safe_call(bot.send_message, uid, c.text, parse_mode="html", link_preview=False,
                        buttons=markup, what="br", retries=2)
        return
    if len(c.text) > CAPTION_LIMIT:
        # Too long for a caption: media first, then the full text with the buttons.
        await safe_call(bot.send_file, uid, c.media, what="br", retries=2)
        await safe_call(bot.send_message, uid, c.text, parse_mode="html", link_preview=False,
                        buttons=markup, what="br", retries=2)
        return
    await safe_call(bot.send_file, uid, c.media, caption=c.text or None, parse_mode="html",
                    buttons=markup, what="br", retries=2)


async def send_all(admin_id: int, c: Content, targets: list[int] | None = None) -> dict:
    """Send `c` to every user at once. Returns {sent, failed, total, seconds}."""
    if targets is None:
        targets = resolve_audience(admin_id, "all")
    targets = [uid for uid in targets if uid not in blocked_users]
    gate = asyncio.Semaphore(SEND_AT_ONCE)
    result = {"sent": 0, "failed": 0}
    started = time.time()

    async def one(uid: int) -> None:
        async with gate:
            try:
                await _send_one(uid, c)
                result["sent"] += 1
            except Exception as exc:
                if _classify(exc) == "blocked":
                    blocked_users.add(uid)
                result["failed"] += 1
                log.debug("/br to %s failed: %s", uid, exc)

    await asyncio.gather(*(one(uid) for uid in targets))
    result.update(total=len(targets), seconds=time.time() - started)
    return result
