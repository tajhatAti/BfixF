"""Rate-limit aware wrapper around every Telegram API call.

A FloodWait used to abort whatever the bot was doing. Now every call goes
through `safe_call`, which waits out the flood and retries instead of dying.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from telethon.errors import FloodWaitError, RPCError, ServerError, TimedOutError

from app import config as cfg
from app.logger import log
from app.runtime import spawn

_last_flood: dict[str, float] = {}


async def safe_call(func: Callable[..., Awaitable[Any]], *args: Any,
                    what: str = "call", retries: int = 3,
                    raise_after_retries: bool = True, **kwargs: Any) -> Any:
    """Await `func(*args, **kwargs)` while surviving FloodWait/network hiccups."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await func(*args, **kwargs)
        except FloodWaitError as exc:
            wait = int(exc.seconds) + 1
            _last_flood[what] = time.time() + wait
            if wait > cfg.FLOOD_SAFETY_MAX:
                log.error("FloodWait of %ss on %s is longer than the safety cap", wait, what)
                if raise_after_retries:
                    raise
                return None
            log.warning("FloodWait %ss on %s — waiting it out", wait, what)
            await asyncio.sleep(wait)
        except (ServerError, TimedOutError, ConnectionError, OSError) as exc:
            if attempt > retries:
                log.error("%s failed after %s attempts: %s", what, attempt, exc)
                if raise_after_retries:
                    raise
                return None
            backoff = min(30, 1.5 * attempt)
            log.warning("%s hiccup (%s) — retrying in %.1fs", what, exc, backoff)
            await asyncio.sleep(backoff)
        except RPCError:
            raise


def flood_status() -> str | None:
    """Human readable note when we are currently waiting out a flood."""
    now = time.time()
    pending = {k: v - now for k, v in _last_flood.items() if v > now}
    if not pending:
        return None
    name, seconds = max(pending.items(), key=lambda item: item[1])
    return f"{name} in ~{int(seconds)}s"


def fire_and_forget(coro) -> None:
    spawn(coro)
