"""Run the Telegram bot **and** the website in one process.

This is the entry point hosting panels expect (CodeNest, Render, Railway, a VPS…):

    python run.py

What it does
    1. binds the SQLite database,
    2. starts the website (public store front + admin panel) on `$PORT`
       — so the hosting panel gets its HTTP port and the site is live even while
       the bot is reconnecting,
    3. starts the Telegram bot in the same event loop, with automatic retry: if
       Telegram is unreachable the site keeps working and the bot connects by
       itself as soon as the network is back.

Everything the panel does (broadcasts, orders, files) talks to the same database
as the bot, so the two halves never disagree.
"""
from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient

from app import config as cfg, runtime
from app.logger import log, setup_logging
from app.services import credentials, secrets_guard
from app.storage import db

from app.handler_loader import HANDLER_MODULES, load_handlers


async def secrets_watchdog() -> None:
    """Warn (log + Telegram) when secrets sit inside a committed config.env.

    Purely informative — it can never take the bot or the website down.
    """
    try:
        report = secrets_guard.log_report()
        if report.get("secrets"):
            await secrets_guard.notify_admins_once_per_day()
    except Exception as exc:
        log.debug("secrets watchdog skipped: %s", exc)


async def bot_worker() -> None:
    """Wait for the credentials saved on the website, then connect the bot.

    Nothing is read from environment variables any more: the owner pastes the bot
    token on the website's setup page, the page stores it in the database and
    `credentials.ready` wakes this worker. The website stays up the whole time.
    """

    from telethon import errors

    from bot import admin_error_notifier, on_startup

    credentials.load()
    while not credentials.is_configured():
        credentials.set_state("waiting", "ওয়েবসাইটে গিয়ে বটের তথ্য দিন")
        log.info("Waiting for the bot token — open the website and finish the setup page.")
        ready = credentials._event()
        ready.clear()
        await ready.wait()
        credentials.load()

    credentials.set_state("connecting", "টেলিগ্রামে সংযোগ হচ্ছে…")
    client = TelegramClient(cfg.SESSION_STATE_FILE, cfg.API_ID, cfg.API_HASH,
                            device_model="Store Bot v2")
    client.parse_mode = "html"
    runtime.set_client(client)

    # Handlers register themselves on import — they must be imported *after* the
    # client exists, otherwise the buttons would answer to nobody.
    load_handlers(client)

    import app.logger as logger_module
    logger_module.error_notifier = admin_error_notifier

    started = False
    while True:
        try:
            if not client.is_connected():
                await client.connect()
            if not await client.is_user_authorized():
                log.info("Signing in to Telegram…")
                await client.start(bot_token=cfg.BOT_TOKEN)
            if not started:
                await on_startup(client)
                started = True
                credentials.mark_started(cfg.BOT_TOKEN)
                log.info("Bot is online — website + bot are both running ✅")
            credentials.set_state("online", "", runtime.bot_username)
            await client.run_until_disconnected()
        except (errors.ApiIdInvalidError, errors.AccessTokenInvalidError,
                errors.AccessTokenExpiredError) as exc:
            # Wrong credentials never fix themselves — wait for new ones from the website.
            credentials.set_state("error", "API ID/Hash বা বট টোকেন টেলিগ্রাম মানেনি — "
                                           "ওয়েবসাইটে আবার দিন।")
            log.error("Telegram rejected the credentials: %s", exc)
            ready = credentials._event()
            ready.clear()
            await ready.wait()
            credentials.load()
            credentials.mark_started("")
            credentials.schedule_restart(delay=0.5)
            await asyncio.sleep(30)
        except Exception as exc:
            credentials.set_state("error", f"সংযোগে সমস্যা: {str(exc)[:120]} — আবার চেষ্টা চলছে")
            log.error("Telegram connection problem: %s", exc)
            log.error("The website stays online; retrying the bot in 15 seconds…")
        await asyncio.sleep(15)


async def web_worker() -> None:
    """Serve the store front + admin panel on WEB_HOST:WEB_PORT ($PORT)."""
    import uvicorn

    from web.dashboard import app as web_app

    config = uvicorn.Config(web_app, host=cfg.WEB_HOST, port=cfg.WEB_PORT,
                            log_level="info", access_log=False)
    server = uvicorn.Server(config)
    log.info("Website listening on http://%s:%s", cfg.WEB_HOST, cfg.WEB_PORT)
    await server.serve()


def initialize_database() -> bool:
    try:
        db.bind(cfg.DB_FILE)
        cfg.DATABASE_UNAVAILABLE = False
        return True
    except Exception:
        if not str(cfg.DB_FILE).startswith(('postgresql://', 'postgres://')):
            raise
        # Recovery UI only, never a second writable application database.
        cfg.DATABASE_UNAVAILABLE = True
        db.bind(':memory:')
        log.exception('PostgreSQL unavailable; website is in database recovery mode')
        return False


async def run() -> int:
    tasks = []
    if not initialize_database():
        tasks.append(asyncio.create_task(web_worker()))
        while cfg.DATABASE_UNAVAILABLE:
            await asyncio.sleep(10)
            if not cfg.DATABASE_UNAVAILABLE:
                break
            try:
                from app import database_config
                async with database_config.switch_lock():
                    if cfg.DATABASE_UNAVAILABLE:
                        await asyncio.to_thread(db.bind, cfg.DB_FILE)
                        cfg.DATABASE_UNAVAILABLE = False
            except Exception:
                log.warning('PostgreSQL still unavailable; bot remains stopped')
    imported = db.migrate_legacy(cfg.LEGACY_DB_FILE)
    if imported:
        log.info("Imported the legacy bot_db.json: %s", imported)

    # The website is the only place the bot is configured, so it always runs.
    if not tasks:
        tasks.append(asyncio.create_task(web_worker()))
    tasks.append(asyncio.create_task(secrets_watchdog()))
    credentials.load()
    tasks.append(asyncio.create_task(bot_worker()))
    # Several bots, one database: bring the extra ones up in their own processes
    # (see app/services/botrunner.py). A blocked or restarting main bot no longer
    # takes the whole service down with it.
    try:
        from app.services import botrunner
        autostart = botrunner.autostart()
        tasks.append(asyncio.create_task(botrunner.supervise()))
        if autostart.get("started") or autostart.get("failed"):
            log.info("extra bots: %s", autostart)
    except Exception as exc:
        log.warning("extra bots could not autostart: %s", exc)

    await asyncio.gather(*tasks)
    return 0


def main() -> int:
    setup_logging()
    log.info("Starting store bot + website (run.py)…")
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutting down on user request")
        return 0


if __name__ == "__main__":
    sys.exit(main())
