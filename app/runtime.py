"""Shared runtime state (clients, caches, settings).

Everything mutable that used to be a loose global now lives here, so modules can
import it without circular dependency headaches.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any

from telethon import TelegramClient

from app import config as cfg
from app.logger import log
from app.storage import db

# Telegram clients -----------------------------------------------------------
_client: TelegramClient | None = None


class _BotProxy:
    """Lazy proxy for the bot client.

    Modules do `from app.runtime import bot` at import time — which used to bind
    `None` when the client was created later (the scheduler and the support
    service were silently broken by that). The proxy resolves the real client on
    every call instead.
    """

    def __getattr__(self, item: str):
        return getattr(_require_client(), item)

    def __call__(self, *args, **kwargs):
        return _require_client()(*args, **kwargs)

    def __bool__(self) -> bool:
        return _client is not None


def _require_client() -> TelegramClient:
    if _client is None:
        raise RuntimeError("The bot client is not connected yet")
    return _client


def set_client(client: TelegramClient | None) -> None:
    global _client
    _client = client


def get_client() -> TelegramClient | None:
    return _client


bot = _BotProxy()

user_clients: dict[int, TelegramClient] = {}     # admin_id -> userbot client
session_owners: dict[int, int] = {}              # admin_id -> real Telegram uid
bot_username: str = ""
bot_id: int = 0

# Caches ---------------------------------------------------------------------
bot_entity_cache: dict[int, Any] = {}            # admin_id -> bot entity
jit_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
pending_forwards: dict[int, asyncio.Future] = {} # telegram uid -> future
force_join_cache: dict[int, float] = {}          # user_id -> valid until ts
force_join_ok: dict[int, bool] = {}

# Users who blocked the bot (skip on broadcast instead of erroring every time)
blocked_users: set[int] = set()

# Message ids of JIT forwards we created ourselves — the upload handler must
# never mistake them for a new manual upload.
recent_jit_ids: deque[int] = deque(maxlen=500)

# Long running tasks ---------------------------------------------------------
background_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


# Settings ---------------------------------------------------------------
# Everything below can be changed from the bot panel *or* the website; the
# `settings` service decides whether the value comes from the database, from
# config.env or from the built-in default.
def force_channel() -> str:
    from app.services import settings
    return settings.get_str("FORCE_CHANNEL")


def set_force_channel(value: str) -> None:
    from app.services import settings
    settings.set("FORCE_CHANNEL", value)


def force_join_on() -> bool:
    from app.services import settings
    return bool(settings.get_bool("FORCE_JOIN_ENABLED", True) and settings.get_str("FORCE_CHANNEL"))


def caption() -> str:
    from app.services import settings
    return settings.get_str("CUSTOM_CAPTION")


def set_caption(value: str) -> None:
    from app.services import settings
    settings.set("CUSTOM_CAPTION", value)


# Session helpers ------------------------------------------------------------
def register_client(admin_id: int, client: TelegramClient, owner_id: int,
                    owner_name: str = "") -> None:
    user_clients[admin_id] = client
    session_owners[admin_id] = owner_id
    bot_entity_cache.pop(admin_id, None)


def unregister_client(admin_id: int) -> None:
    user_clients.pop(admin_id, None)
    session_owners.pop(admin_id, None)
    bot_entity_cache.pop(admin_id, None)


def bot_online() -> bool:
    """True only while the Telegram client is connected and has finished signing in.

    A client object exists from the first moment we try to connect, and the stored
    bot id survives a network disconnect. Check both so campaigns remain queued
    rather than starting against a client that cannot send.
    """
    if _client is None or not bot_id:
        return False
    try:
        return bool(_client.is_connected())
    except Exception:
        return False


def is_online(admin_id: int) -> bool:
    client = user_clients.get(admin_id)
    if client is None:
        return False
    try:
        return bool(client.is_connected())
    except Exception:
        return False


def mark_join(uid: int, joined: bool) -> None:
    force_join_ok[uid] = joined
    force_join_cache[uid] = time.time() + cfg.FORCE_JOIN_CACHE_SECONDS


def cached_join(uid: int) -> bool | None:
    if uid in force_join_ok and force_join_cache.get(uid, 0) > time.time():
        return force_join_ok[uid]
    return None


async def wait_for_forward(sender_id: int, timeout: int | None = None) -> Any | None:
    """Wait for the next incoming media message from `sender_id`.

    Bots cannot call GetHistoryRequest, so we listen for the live update the
    moment the userbot forwards the file into the bot's DM.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    pending_forwards[sender_id] = future
    try:
        return await asyncio.wait_for(future, timeout=timeout or cfg.JIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return None
    finally:
        pending_forwards.pop(sender_id, None)


def log_session_summary() -> None:
    if not user_clients:
        log.warning("No userbot sessions connected — private channel files cannot be served")
        return
    for admin_id, owner in session_owners.items():
        log.info("Session ready for admin %s (owner uid %s)", admin_id, owner)
