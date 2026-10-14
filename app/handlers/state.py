"""Short lived conversations state (who is typing what right now).

Every flow is *short lived* on purpose. The owner once hit a nasty bug: an old
step stayed armed for days, so every random text he typed was swallowed as the
answer (“যেকোনো টেক্সট দিলে বারে বারে সেভ হইতেছে”) — a store name, a channel or a
limit got overwritten over and over. Flows now

* expire after `FLOW_TIMEOUT_SECONDS` (default 10 minutes),
* give up after 3 invalid answers,
* are dropped as soon as the admin taps any button (they clearly moved on).
"""
from __future__ import annotations

import time

from app import config as cfg

# admin_id -> {"action": str, "ctx": dict}
pending_input: dict[int, dict] = {}

# user_id -> store_id while waiting for a search keyword
search_pending: dict[int, int] = {}
# user_id -> when that search was armed (so it expires like every other flow)
search_at: dict[int, float] = {}

# admin_id -> [file_id, ...] while in /done batch mode
batch_mode: dict[int, list[int]] = {}

# admin_id -> {"token": str, "added": int} while adding videos to an existing link
link_add_mode: dict[int, dict] = {}

# admin_id -> {"store_id": int, "page": int, "selected": set[int]} multi-link tool
link_gen: dict[int, dict] = {}

# admin_id -> [ (chat_id, title), ... ] cached dialog list for the scan picker
dialog_cache: dict[int, list[tuple[int, str]]] = {}


# admin_id -> context shared between the buttons of a multi-step flow
flow_ctx: dict[int, dict] = {}


#: how many wrong answers a flow tolerates before it gives up
MAX_TRIES = 3

# (user_id, action) -> wrong answers so far. Kept outside `pending_input`
# because the answer is removed from it *before* the handler validates it.
wrong_answers: dict[tuple[int, str], int] = {}


def flow_timeout() -> float:
    """Seconds a question stays armed (panel setting, 10 minutes by default)."""
    try:
        from app.services import settings
        value = settings.get_int("FLOW_TIMEOUT_SECONDS", 600)
    except Exception:
        value = getattr(cfg, "FLOW_TIMEOUT_SECONDS", 600)
    return float(max(30, value or 600))


def ask(admin_id: int, action: str, **ctx) -> None:
    pending_input[admin_id] = {"action": action, "ctx": ctx, "at": time.time()}
    wrong_answers.pop((admin_id, action), None)          # a fresh question starts clean


def pending_age(admin_id: int) -> float | None:
    row = pending_input.get(admin_id)
    if not row:
        return None
    return time.time() - float(row.get("at") or 0)


def bump_tries(admin_id: int, action: str = "") -> int:
    """Count a wrong answer for this question. Returns the new count."""
    key = (admin_id, action)
    wrong_answers[key] = int(wrong_answers.get(key, 0)) + 1
    return wrong_answers[key]


def tries_left(admin_id: int, action: str = "") -> int:
    return max(0, MAX_TRIES - int(wrong_answers.get((admin_id, action), 0)))


def drop_if_stale(admin_id: int) -> bool:
    """Forget a flow nobody answered in time. True when something was dropped."""
    age = pending_age(admin_id)
    if age is not None and age > flow_timeout():
        pending_input.pop(admin_id, None)
        return True
    return False


def flow(admin_id: int, **ctx) -> None:
    """Remember where a button flow is (merged, so later steps can add fields)."""
    flow_ctx.setdefault(admin_id, {}).update(ctx)


def ctx(admin_id: int) -> dict:
    return flow_ctx.get(admin_id, {})


def end_flow(admin_id: int) -> None:
    flow_ctx.pop(admin_id, None)


def take(admin_id: int) -> dict | None:
    return pending_input.pop(admin_id, None)


def peek(admin_id: int, *, fresh: bool = True) -> dict | None:
    """The armed question for this admin, if any — stale ones are dropped."""
    if fresh:
        drop_if_stale(admin_id)
    return pending_input.get(admin_id)


def clear(admin_id: int) -> None:
    pending_input.pop(admin_id, None)
    search_pending.pop(admin_id, None)
    search_at.pop(admin_id, None)
    flow_ctx.pop(admin_id, None)
    batch_mode.pop(admin_id, None)
    link_add_mode.pop(admin_id, None)
    link_gen.pop(admin_id, None)
    for key in [k for k in wrong_answers if k[0] == admin_id]:
        wrong_answers.pop(key, None)


def start_search(user_id: int, store_id: int) -> None:
    """Arm “send me a keyword” for this store."""
    search_pending[user_id] = store_id
    search_at[user_id] = time.time()


def take_search(user_id: int) -> int | None:
    """The store this user is searching in — expires like every other question."""
    if user_id not in search_pending:
        return None
    if time.time() - search_at.get(user_id, 0) > flow_timeout():
        search_pending.pop(user_id, None)
        search_at.pop(user_id, None)
        return None
    search_at.pop(user_id, None)
    return search_pending.pop(user_id, None)


def is_searching(user_id: int) -> bool:
    """Is a search armed right now? (Non-destructive — just asks.)"""
    if user_id not in search_pending:
        return False
    return time.time() - search_at.get(user_id, 0) <= flow_timeout()


def cancel_buttons():
    """A visible “cancel” row for every question the bot asks."""
    try:
        from telethon.tl.custom import Button
    except Exception:                                   # pragma: no cover
        return None
    return [[Button.inline("✖️ বাতিল", "cd:flow")]]
