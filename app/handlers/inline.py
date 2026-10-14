"""Inline mode: type `@YourBot interstellar` in ANY chat and pick a result.

This is the biggest growth feature — people share files without leaving their
own groups. Requires inline mode to be enabled in @BotFather (/setinline).
"""
from __future__ import annotations

from telethon import events
from telethon.extensions import html as html_ext
from telethon.tl import types

from app import config as cfg, runtime
from app.logger import log
from app.runtime import bot
from app.services import access
from app.storage import db
from app.utils import esc

ICONS = {"Video": "🎬", "Photo": "🖼", "Audio": "🎵", "Document": "📄"}


def _visible(user_id: int, matches: list[dict]) -> list[dict]:
    """Only show files the searcher may actually open."""
    visible = []
    for file_row in matches:
        store = db.store(file_row["store_id"])
        if store is None:
            continue
        if access.has_access(store, user_id):
            visible.append(file_row)
    return visible


@bot.on(events.InlineQuery())
async def inline_search(event: events.InlineQuery.Event) -> None:
    if not cfg.INLINE_ENABLED:
        return

    user_id = event.sender_id
    query = (event.text or "").strip()
    if len(query) < 2:
        await event.answer(
            results=[],
            cache_time=30,
            switch_pm="🔎 Search the store",
            switch_pm_param="inline",
        )
        return

    try:
        matches = _visible(user_id, db.search_all_stores(query, cfg.INLINE_RESULTS))
    except Exception as exc:                      # never break the user's chat
        log.warning("inline search failed: %s", exc)
        matches = []

    results = []
    for file_row in matches:
        icon = ICONS.get(file_row["kind"], "📁")
        store_name = file_row.get("store_name") or "Store"
        title = f"{icon} {file_row['name']}"
        description = f"{store_name} · 👁 {file_row['views']} · tap to get the file"
        # tapping a result posts a text with the deep link; the bot's /start
        # payload then delivers the file privately (keeps files out of groups).
        link = f"https://t.me/{runtime.bot_username}?start=f{file_row['id']}"
        text = (
            f"🎬 <b>{esc(file_row['name'])}</b>\n"
            f"🏪 {esc(store_name)}\n\n"
            f"👉 <a href='{link}'>ফাইলটি নিতে এখানে চাপ দিন</a>\n"
            "<i>(link খুলে bot /start চাপলেই ফাইল পাবেন)</i>"
        )
        # inline results don't support parse_mode, so HTML is converted to
        # entities before sending.
        plain_text, entities = html_ext.parse(text)
        results.append(
            types.InputBotInlineResult(
                id=str(file_row["id"]),
                type="article",
                title=title[:90],
                description=description[:110],
                send_message=types.InputBotInlineMessageText(
                    message=plain_text,
                    no_webpage=True,
                    entities=entities,
                ),
            )
        )

    try:
        await event.answer(results=results[:cfg.INLINE_RESULTS], cache_time=20,
                           switch_pm="🔎 Search the store", switch_pm_param="inline")
    except Exception as exc:
        log.debug("inline answer failed: %s", exc)
