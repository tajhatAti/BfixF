"""Single callback-query router.

Every button payload is a short prefix (`s:12`, `f:345`, `adm:new`) that maps to
exactly one handler. Registering routes in one place means two handlers can never
fight over the same click, and the payload stays far below Telegram's 64 byte
limit.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from telethon import events

from app.logger import log
from app.runtime import bot

Handler = Callable[[events.CallbackQuery.Event, str], Awaitable[None]]

_exact: dict[str, Handler] = {}
_prefix: dict[str, Handler] = {}

ROUTE_COUNT = 0


def route(key: str):
    """Register a handler. Keys ending with ':' match as a prefix."""
    def decorator(func: Handler) -> Handler:
        global ROUTE_COUNT
        table = _prefix if key.endswith(":") else _exact
        if key in table:
            # Two handlers fighting over one button is a bug, not a feature.
            raise RuntimeError(f"duplicate callback route registered: {key!r}")
        table[key] = func
        ROUTE_COUNT += 1
        return func
    return decorator


def resolve(data: str) -> tuple[Handler, str] | None:
    if data in _exact:
        return _exact[data], ""
    best_key = ""
    for key in _prefix:
        if data.startswith(key) and len(key) > len(best_key):
            best_key = key
    if best_key:
        return _prefix[best_key], data[len(best_key):]
    return None


@bot.on(events.CallbackQuery())
async def dispatch(event: events.CallbackQuery.Event) -> None:
    try:
        data = event.data.decode(errors="ignore")
    except Exception:
        await event.answer()
        return

    if data == "noop":
        await event.answer()          # stops the endless loading spinner
        return
    if not data.startswith("cd:"):
        # Tapping a button means “I moved on”: drop any *question* that was still
        # armed, so a later text can never be mistaken for its answer. The button
        # flow context (flow_ctx) stays — many flows continue through buttons.
        try:
            from app.handlers.state import take as _take_question
            _take_question(event.sender_id)
        except Exception:
            pass

    match = resolve(data)
    if match is None:
        log.debug("Unrouted callback data: %s", data)
        await event.answer()
        return

    handler, rest = match
    try:
        await handler(event, rest)
    except Exception as exc:
        log.exception("Callback %s failed: %s", data, exc)
        try:
            await event.answer("⚠️ Something went wrong. Please try again.", alert=True)
        except Exception:
            pass
