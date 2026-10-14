"""Tiny helpers used by the keyboard builders (kept separate to avoid clutter)."""
from __future__ import annotations

from typing import Iterator, Sequence, TypeVar

T = TypeVar("T")

KIND_ICONS = {
    "Video": "🎬",
    "Photo": "🖼",
    "Audio": "🎵",
    "Document": "📄",
    "File": "📁",
}


def cut(text: str, limit: int) -> str:
    """Trim a label so buttons stay readable and payloads stay short."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def kind_icon(kind: str | None) -> str:
    return KIND_ICONS.get(kind or "File", "📁")


def pairs(seq: Sequence[T]) -> Iterator[tuple]:
    """Yield (item, item|None) tuples so we can build two-column keyboards."""
    seq = list(seq)
    for i in range(0, len(seq), 2):
        if i + 1 < len(seq):
            yield seq[i], seq[i + 1]
        else:
            yield seq[i], None
