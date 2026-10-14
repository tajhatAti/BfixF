"""Small helpers shared across the bot (time, slugs, tokens, formatting)."""
from __future__ import annotations

import html
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, Sequence, TypeVar

from app import config as cfg

T = TypeVar("T")


# ------------------------------------------------------------------ time bits
def _tz():
    from app.services import settings
    try:
        offset = settings.get_float("TZ_OFFSET", 6.0)
    except Exception:            # settings not ready (very early import)
        offset = cfg.TZ_OFFSET
    return timezone(timedelta(hours=offset))


def now_ts() -> float:
    """Current unix timestamp."""
    return time.time()


def local_now() -> datetime:
    """Now, expressed in the configured timezone (server TZ does not matter)."""
    return datetime.now(timezone.utc).astimezone(_tz())


def local_date_str() -> str:
    return local_now().strftime("%Y-%m-%d")


def local_time_str() -> str:
    return local_now().strftime("%H:%M")


def fmt_ts(ts: float | int | None, pattern: str = "%Y-%m-%d") -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=_tz()).strftime(pattern)
    except (ValueError, OSError, OverflowError):
        return "—"


def human_delta(seconds: float | int) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "0m"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def remaining_text(expires_at: float | None) -> str:
    if expires_at is None:
        return "♾ Lifetime"
    left = expires_at - time.time()
    if left <= 0:
        return "⌛ Expired"
    return f"{human_delta(left)} left"


# ----------------------------------------------------------------- text/data
def esc(value: object) -> str:
    """Escape user supplied text before it goes into an HTML formatted message."""
    return html.escape(str(value if value is not None else ""))


def slugify(name: str, max_len: int = 24) -> str:
    """URL slug that also works for Bengali (and any Unicode) store names.

    Old versions kept only `[a-zA-Z0-9]`, so every Bangla store ended up with the
    same useless slug ("store", "store_2", …) and its public link looked broken.
    """
    def keep(ch: str) -> str:
        if ch.isalnum():
            return ch
        # Bangla vowel signs / hasanta are combining marks, not "alnum" — they
        # belong to the word, otherwise "সিনেমা" would become "স_ন_ম".
        if unicodedata.category(ch) in ("Mn", "Mc"):
            return ch
        return "_"

    cleaned = "".join(keep(ch) for ch in unicodedata.normalize("NFC", name or ""))
    base = re.sub(r"_{2,}", "_", cleaned).strip("_").lower()
    return base[:max_len] or "store"


def new_token(length: int = 10) -> str:
    """URL safe token used in deep links (?start=...) so links never blow the
    64 character payload limit."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def money(amount: float, currency: str = "৳") -> str:
    if float(amount).is_integer():
        return f"{currency}{int(amount)}"
    return f"{currency}{amount:.2f}"


def human_size(size: int | None) -> str:
    if not size:
        return ""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < step or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= step
    return ""


def chunked(seq: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def button_data(button) -> bytes | None:
    """Read the callback payload of an inline button.

    Newer Telethon layers wrap the payload in `button.type.data`, older ones keep
    it on `button.data`, so we accept both.
    """
    data = getattr(button, "data", None)
    if data is None:
        data = getattr(getattr(button, "type", None), "data", None)
    if isinstance(data, str):
        return data.encode()
    return data if isinstance(data, bytes) else None


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def unique_keep_order(items: Iterable[T]) -> list[T]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def parse_button_spec(spec: str) -> list[list[tuple[str, str]]]:
    """Turn the admin's button text into rows of (label, url).

    One button per line as `label | url`; put `&&` between buttons to keep them
    on the same row::

        📢 Join channel | https://t.me/mychannel
        🛒 Store | https://t.me/mybot?start=s1 && 💬 Admin | https://t.me/admin

    Lines without a link are ignored, so a stray line never breaks a broadcast.
    """
    rows: list[list[tuple[str, str]]] = []
    for raw_line in (spec or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        row: list[tuple[str, str]] = []
        for part in line.split("&&"):
            part = part.strip()
            if not part:
                continue
            if "|" in part:
                label, _, url = part.partition("|")
                label, url = label.strip(), url.strip()
            else:
                tokens = part.rsplit(None, 1)
                if len(tokens) == 2 and tokens[1].startswith(("http://", "https://", "tg://")):
                    label, url = tokens[0].strip(), tokens[1].strip()
                else:
                    label, url = "", part.split()[-1] if part.split() else ""
            if not url.startswith(("http://", "https://", "tg://")):
                continue
            row.append((label[:60] or "🔗", url))
        if row:
            rows.append(row)
    return rows


def button_rows(buttons):
    """Build a TL inline keyboard from ([label, url] | [label, data] | Button).

    Works both with plain pairs from `parse_button_spec()` and with ready-made
    Telethon `Button` objects.
    """
    if not buttons:
        return None
    try:
        from telethon.tl.custom import Button
        from telethon.tl.types import KeyboardInlineButtonRow, ReplyInlineMarkup
    except Exception:                                     # pragma: no cover
        return None

    def as_button(item):
        """Accept a ready Button, a raw KeyboardInlineButton or a (label, value) pair."""
        if hasattr(item, "text") and hasattr(item, "type"):
            return item                                    # already a TL button
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            label, value = str(item[0])[:60], str(item[1])
            if value.startswith(("http://", "https://", "tg://")):
                return Button.url(label, value)
            return Button.inline(label, value.encode())
        return None

    if hasattr(buttons, "text") and hasattr(buttons, "type"):
        buttons = [buttons]                                # a single button
    rows = []
    for row in buttons:
        if hasattr(row, "text") and hasattr(row, "type"):
            row = [row]
        elif isinstance(row, (list, tuple)) and len(row) == 2 \
                and isinstance(row[0], str) and isinstance(row[1], str):
            row = [row]                                   # a bare (label, value) pair
        built = [b for b in (as_button(item) for item in row) if b is not None]
        if built:
            rows.append(KeyboardInlineButtonRow(built))
    return ReplyInlineMarkup(rows) if rows else None
