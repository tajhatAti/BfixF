"""Entry point for the Telegram store bot (v2).

Run it with:  python bot.py
Configuration lives in `config.env` (see config.env.example) — never in the code.
"""
from __future__ import annotations

import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from app import config as cfg, runtime
from app.logger import log, setup_logging
from app.services import scheduler, secrets_guard
from app.storage import db


async def on_startup(client: TelegramClient) -> None:
    me = await client.get_me()
    runtime.bot_username = me.username or ""
    runtime.bot_id = me.id
    # Remember it in the database too: the public website can then build
    # t.me deep links even while the bot itself is offline.
    if runtime.bot_username and os.getenv("BOT_EXTRA") != "1":
        # The public links must keep pointing at the *main* bot. An extra bot
        # records its own name in the `bots` table instead.
        db.set_meta("bot_username", runtime.bot_username)
    try:
        if os.getenv("BOT_EXTRA") == "1":
            from app.services import botrunner
            row = db.bots_store.bot_by_token(cfg.BOT_TOKEN)
            if row is None:
                bot_id = botrunner.add(cfg.BOT_TOKEN, label=os.getenv("BOT_LABEL") or "",
                                       admin_id=(cfg.ADMIN_IDS or [0])[0],
                                       api_id=cfg.API_ID, api_hash=cfg.API_HASH,
                                       origin="env")["id"]
                row = db.bots_store.bot(bot_id)
            botrunner.learn_username(row, runtime.bot_username)
    except Exception as exc:
        log.debug("extra bot registration skipped: %s", exc)
    log.info("Bot online as @%s (id=%s)", runtime.bot_username, me.id)

    stats = db.stats()
    log.info("Database: %s users · %s files · %s stores · %s grants",
             stats["users"], stats["files"], stats["stores"], stats["grants"])

    from app.services.command_catalog import announce_and_install
    runtime.spawn(announce_and_install(client))
    await seed_and_reconnect_sessions()
    if os.getenv("BOT_EXTRA") != "1":
        scheduler.start_all()
    else:
        from app.services import video_copy
        runtime.spawn(video_copy.monitor())  # shared lease, no duplicate broadcasts
    runtime.spawn(prepare_media_cache())


async def prepare_media_cache() -> None:
    """Drop unreadable old mirrors and make sure the cache channel exists."""
    try:
        from app.services import media_cache
        cleared = media_cache.clear_private_mirrors()
        if cleared:
            log.info("Forgot %s old copy/copies that lived in a private chat — they are "
                     "re-created in the cache channel", cleared)
        client = next(iter(runtime.user_clients.values()), None)
        if client is not None:
            await media_cache.storage_peer(client)
            await media_cache.poke_storage_channel(client)
    except Exception as exc:
        log.warning("media cache setup skipped: %s", exc)


async def seed_and_reconnect_sessions() -> None:
    """Bring every stored userbot session online (and seed the pre-filled one)."""
    from app.runtime import register_client

    if cfg.STRING_SESSION and cfg.ADMIN_IDS and db.session(cfg.ADMIN_IDS[0]) is None:
        db.save_session(cfg.ADMIN_IDS[0], cfg.STRING_SESSION)
        log.info("Seeded the pre-filled STRING_SESSION for admin %s", cfg.ADMIN_IDS[0])

    for row in db.all_sessions():
        admin_id = row["admin_id"]
        try:
            client = TelegramClient(StringSession(row["session_str"]), cfg.API_ID, cfg.API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                log.warning("Stored session for admin %s is no longer authorised", admin_id)
                continue
            me = await client.get_me()
            register_client(admin_id, client, me.id, me.first_name or "")
            db.save_session(admin_id, row["session_str"], me.id, me.first_name or "")
            log.info("Session online for admin %s (as %s, id=%s)", admin_id, me.first_name, me.id)
        except Exception as exc:
            log.error("Could not restore session for admin %s: %s", admin_id, exc)

    runtime.log_session_summary()


async def admin_error_notifier(text: str) -> None:
    client = runtime.get_client()
    if client is None:
        return
    from app.services.error_reports import redact
    from app.utils import esc
    for admin_id in cfg.ADMIN_IDS[:1]:  # technical diagnostics belong to the owner
        try:
            await asyncio.wait_for(client.send_message(
                admin_id, f"⚠️ <b>বটের সতর্কতা</b>\n{esc(redact(text).splitlines()[0][:240])}\nবিস্তারিত ব্যক্তিগত error inbox-এ সেভ হয়েছে।"), timeout=10)
        except Exception:
            pass


def main() -> int:
    setup_logging()

    if os.getenv("BOT_EXTRA") == "1":
        # Started by `app/services/botrunner.py` as an *extra* bot: this process
        # only polls its own token. The website already runs in the main process,
        # so it must not try to bind the port again.
        globals()["start_website"] = lambda: None
        log.info("Extra bot mode (%s): serving Telegram only", os.getenv("BOT_LABEL") or "bot")

    if cfg.HOSTED and os.getenv("BOT_EXTRA") != "1":
        # A hosting panel (CodeNest/RunSpace, Render, Railway…) sets $PORT and
        # waits for us to listen on it. `run.py` is exactly that product — the
        # website plus a bot that reconnects instead of dying — so reuse it
        # rather than keeping two entry points that drift apart.
        from run import main as run_main
        log.info("$PORT detected (%s) — starting the combined website + bot runner",
                 cfg.WEB_PORT)
        return run_main()

    # Local run: the website is optional (WEB_ENABLED), the bot is the point.
    website = start_website() if cfg.WEB_ENABLED else None

    problems = cfg.validate()
    if problems:
        log.error("Configuration problem(s): %s", "; ".join(problems))
        log.error("Copy config.env.example to config.env and fill in your values "
                  "(or set them in the hosting panel's environment variables).")
        if website is not None and os.getenv("PORT"):
            # Keep the process (and the port) alive so the panel's URL still works.
            log.error("The website keeps running — open the live URL, fix the values "
                      "in the panel, then restart the job.")
            website.join()
        return 2

    db.bind(cfg.DB_FILE)
    imported = db.migrate_legacy(cfg.LEGACY_DB_FILE)
    if imported:
        log.info("Imported the legacy bot_db.json: %s", imported)
    secrets_guard.log_report()

    def handle_loop_exception(loop, context):
        log.error("Unhandled loop error: %s (%s)", context.get("message"), context.get("exception"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(handle_loop_exception)

    client = TelegramClient(cfg.SESSION_STATE_FILE, cfg.API_ID, cfg.API_HASH,
                            device_model="Store Bot v2",
                            loop=loop)
    # Every message in this project uses HTML tags — with Telethon's markdown
    # default the users would see literal <b> and ** asterisks everywhere.
    client.parse_mode = "html"
    runtime.set_client(client)

    # Handlers register themselves on import (they need runtime.bot to exist),
    # so we import every handler module for its side effects.
    from app.handler_loader import load_handlers
    load_handlers(client)

    import app.logger as logger_module
    logger_module.error_notifier = admin_error_notifier

    log.info("Connecting to Telegram…")
    try:
        client.start(bot_token=cfg.BOT_TOKEN)
    except Exception as exc:
        log.error("Could not connect to Telegram: %s", exc)
        log.error("Check the server network/firewall, then verify BOT_TOKEN and API_ID/API_HASH "
                  "in config.env.")
        return 3

    log.info("Starting background workers and sessions…")
    loop.run_until_complete(on_startup(client))

    try:
        client.run_until_disconnected()
    except KeyboardInterrupt:
        log.info("Shutting down on user request")
    finally:
        try:
            loop.run_until_complete(disconnect_all(client))
        except Exception:
            pass
    return 0


def start_website() -> "threading.Thread | None":
    """Serve the store front + admin panel in a background thread.

    Hosting panels (CodeNest/RunSpace, Render, Railway…) pick `bot.py` as the
    entry point when a repository has one — and then expect the job to open a
    web port. Without this, the panel shows “The job is running, but no web
    listener yet”. A daemon thread keeps the site up no matter what the bot is
    doing, and the port it binds is `$PORT` (that is what the panel routes to).
    """
    try:
        import threading

        import uvicorn

        from web.dashboard import app as web_app

        server = uvicorn.Server(uvicorn.Config(web_app, host=cfg.WEB_HOST,
                                               port=cfg.WEB_PORT, log_level="info",
                                               access_log=False))
        thread = threading.Thread(target=server.run, name="website", daemon=True)
        thread.start()
        log.info("Website on http://%s:%s (password-only admin login) — panel URL: /live/<job>/",
                 cfg.WEB_HOST, cfg.WEB_PORT)
        return thread
    except Exception as exc:
        log.error("Website could not start: %s", exc)
        return None


async def disconnect_all(client: TelegramClient) -> None:
    for admin_id, userbot in list(runtime.user_clients.items()):
        try:
            await userbot.disconnect()
        except Exception:
            pass
    try:
        await client.disconnect()
    except Exception:
        pass
    log.info("Bye 👋")


if __name__ == "__main__":
    sys.exit(main())
