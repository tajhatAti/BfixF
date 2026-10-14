"""Remove other channels' tags from captions and names.

A video copied from someone's channel usually carries `@their_channel`, a t.me link or
a "join us" line. `strip_tags()` drops the @mentions and Telegram links and tidies what
is left, so the name shown in the store (and any caption the bot posts) stays clean.
The switch is the panel setting `STRIP_TAGS` (on by default).
"""
from __future__ import annotations

import re

_LINK = re.compile(r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/\S+|tg://\S+", re.I)
_MENTION = re.compile(r"(?<![\w/])@[A-Za-z][A-Za-z0-9_]{3,31}\b")
_EDGE = " \t-–—|:•·,"


def strip_tags(text: str) -> str:
    """@mentions and t.me links removed; empty lines and dangling separators tidied."""
    if not text:
        return ""
    out = _MENTION.sub("", _LINK.sub("", text))
    out = re.sub(r"[ \t]{2,}", " ", out)
    lines = [line.strip(_EDGE) for line in out.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def clean_text(text: str) -> str:
    """`strip_tags` when the STRIP_TAGS setting is on, otherwise the text unchanged."""
    try:
        from app.services import settings
        enabled = settings.get_bool("STRIP_TAGS", True)
    except Exception:
        enabled = True
    return strip_tags(text) if enabled else (text or "")
