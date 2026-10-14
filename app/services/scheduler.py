"""Background workers: daily drip, expired-link cleanup, backups, renewal
reminders and a session watchdog."""
from __future__ import annotations

import asyncio
import time

from app import config as cfg, i18n, runtime
from app.logger import log
from app.services import settings
from app.runtime import (blocked_users, bot, register_client,
                         unregister_client, user_clients)
from app.services.access import has_access
from app.services.billing import price_text
from app.services.sender import deliver, mirror_ready
from app.services.telegram import safe_call
from app.storage import db
from app.utils import esc, fmt_ts, local_date_str, local_time_str


async def drip_loop() -> None:
    """Every day at the configured time, push N never-sent-before files to the
    store's subscribers. Access is checked at send time, so a user who lost
    premium is dropped instead of getting the files for free."""
    while True:
        try:
            today = local_date_str()
            now_hm = local_time_str()
            for row in db.all_drips():
                store_id = row["store_id"]
                if row["last_sent_date"] == today:
                    continue
                if now_hm < (row["send_time"] or "00:00"):
                    continue
                store = db.store(store_id)
                if store is None:
                    db.set_drip_enabled(store_id, False)
                    continue

                subscribers = db.subscribers(store_id)
                if not subscribers:
                    db.set_drip_last_sent(store_id, today)
                    continue

                already = db.sent_file_ids(store_id)
                fresh = [f for f in db.files_of(store_id) if f["id"] not in already][: max(1, row["count"])]
                if not fresh:
                    db.set_drip_last_sent(store_id, today)
                    log.info("Drip: no fresh files left in store %s", store_id)
                    continue

                delivered = 0
                for user_id in list(subscribers):
                    if user_id in blocked_users:
                        continue
                    if not has_access(store, user_id):
                        db.drop_sub(store_id, user_id)
                        continue
                    try:
                        await safe_call(
                            bot.send_message, user_id,
                            f"📅 <b>Today's drop — {esc(store['name'])}</b>",
                            what="drip_notice", retries=1,
                        )
                        for file_row in fresh:
                            await deliver(user_id, file_row)
                            await asyncio.sleep(cfg.DRIP_SEND_DELAY)
                        delivered += 1
                    except Exception as exc:
                        log.debug("drip to %s failed: %s", user_id, exc)

                db.mark_sent(store_id, [f["id"] for f in fresh])
                db.set_drip_last_sent(store_id, today)
                log.info("Drip sent: store=%s files=%s users=%s", store_id, len(fresh), delivered)
        except Exception as exc:  # never let a worker die
            log.error("drip loop error: %s", exc)
        await asyncio.sleep(30)


async def gc_loop() -> None:
    """Hourly housekeeping: drop expired deep links and stale caches."""
    while True:
        try:
            removed = db.purge_expired_links()
            if removed:
                log.info("Removed %s expired link(s)", removed)
        except Exception as exc:
            log.error("gc loop error: %s", exc)
        await asyncio.sleep(3600)


async def backup_loop() -> None:
    """One JSON + SQLite snapshot per day (keeps the last KEEP_BACKUPS)."""
    while True:
        try:
            today = local_date_str()
            if int(local_time_str()[:2]) >= cfg.BACKUP_HOUR and db.get_meta("last_backup") != today:
                path = db.backup_json(cfg.BACKUP_DIR, cfg.KEEP_BACKUPS)
                db.set_meta("last_backup", today)
                log.info("Daily backup written to %s", path)
                if settings.get_bool("BACKUP_TO_TELEGRAM", True):
                    from app.services import backup as backup_service
                    sent = await backup_service.send_existing(path, "daily")
                    log.info("Daily backup sent to %s admin(s)", sent)
        except Exception as exc:
            log.error("backup loop error: %s", exc)
        await asyncio.sleep(600)


async def reminder_loop() -> None:
    """Once a day, nudge users whose premium access is about to end."""
    while True:
        try:
            today = local_date_str()
            if int(local_time_str()[:2]) >= 10 and db.get_meta("last_reminder") != today:
                db.set_meta("last_reminder", today)
                for grant in db.expired_grants(within_seconds=3 * 86400):
                    user_id = grant["user_id"]
                    if user_id in blocked_users:
                        continue
                    try:
                        await safe_call(
                            bot.send_message, user_id,
                            i18n.t(user_id, "renewal_notice",
                                   store=esc(grant["store_name"]),
                                   date=fmt_ts(grant["expires_at"])),
                            what="renewal_notice", retries=1,
                        )
                    except Exception:
                        pass
        except Exception as exc:
            log.error("reminder loop error: %s", exc)
        await asyncio.sleep(1800)


async def owner_report_loop() -> None:
    """One short DM a day telling the owner what actually happened.

    Counts users, deliveries, blocks, money and the media-cache health — the five
    numbers that decide whether the store is growing. Everything is read from the
    `events` table, so it costs nothing to produce.
    """
    while True:
        try:
            if settings.get_bool("OWNER_REPORT_ENABLED", True):
                today = local_date_str()
                hour = int(local_time_str()[:2])
                if hour >= settings.get_int("OWNER_REPORT_HOUR", 21) \
                        and db.get_meta("owner_report_day") != today:
                    db.set_meta("owner_report_day", today)
                    await send_owner_report()
        except Exception as exc:
            log.error("owner report error: %s", exc)
        await asyncio.sleep(1800)


async def send_owner_report(days: int = 1) -> str:
    """Build and send the daily digest to every admin using owner_report_loop."""
    since = time.time() - days * 86400
    stats = db.stats()
    day = db.event_counts(since=since)          # {"deliver": 3, "join_block": 1, …}
    orders = db.orders_since(since)
    revenue = sum(float(o.get("amount") or 0) for o in orders
                  if (o.get("status") or "") == "approved")
    cache = mirror_ready()
    lines = [
        "📊 <b>দৈনিক রিপোর্ট</b>",
        "",
        f"👥 মোট ইউজার: <b>{stats['users']}</b> · নতুন: <b>{day.get('start', 0)}</b>",
        f"🎬 ভিডিও পাঠানো: <b>{day.get('deliver', 0)}</b> · স্টোর খোলা: <b>{day.get('open_store', 0)}</b>",
        f"🔐 চ্যানেল গেটে আটকেছে: <b>{day.get('join_block', 0)}</b> · "
        f"জয়েন করেছে: <b>{day.get('join_retry', 0) + day.get('join_click', 0)}</b>",
        f"🔗 লিমিট শেষ: <b>{day.get('limit_block', 0)}</b> · লিংক ব্যবহার: <b>{day.get('limit_use', 0)}</b>",
        f"💳 অর্ডার: <b>{len(orders)}</b> · আয়: <b>{esc(price_text(revenue))}</b>",
        f"📊 মোট ফাইল: <b>{stats['files']}</b> · ক্যাশ তৈরি: <b>{cache['mirrored']}</b>"
        f" / বাকি: <b>{cache['missing']}</b>",
    ]
    if day.get("paid"):
        lines.append(f"✅ আজ পেমেন্ট গৃহীত: <b>{day['paid']}</b>")
    warnings = []
    if cache["missing"]:
        warnings.append(f"📥 {cache['missing']} টি ফাইলের বট-কপি এখনো তৈরি হয়নি — "
                        f"ফাইল পেজ থেকে ক্যাশ করুন")
    if not runtime.user_clients:
        warnings.append("🔑 কোনো ইউজারবট সেশন নেই — প্রাইভেট চ্যানেলের ফাইল পাঠাতে পারবে না")
    if db.count_mirrors() == 0 and stats["files"]:
        warnings.append("🎞 মিডিয়া ক্যাশ একদম খালি — প্রথম ক্লিকগুলো ধীর হবে")
    if warnings:
        lines.append("")
        lines.extend(f"⚠️ {text}" for text in warnings)
    if day.get("empty"):
        lines.append("")
        lines.append("💡 আজ কেউ কিছু খোলেনি — নতুন ব্রডকাস্ট বা ডাইজেস্ট দেওয়ার ভালো সময়।")
    text = "\n".join(lines)
    for admin_id in cfg.ADMIN_IDS:
        try:
            await safe_call(bot.send_message, admin_id, text, what="owner_report",
                            retries=1, link_preview=False)
        except Exception:
            continue
    db.set_meta("owner_report_text", text[:4000])
    return text


async def session_watchdog() -> None:
    """Make sure the userbot sessions stay online; alert the admins if not."""
    while True:
        try:
            for row in db.all_sessions():
                admin_id = row["admin_id"]
                client = user_clients.get(admin_id)
                if client is not None and client.is_connected():
                    continue
                try:
                    from telethon import TelegramClient
                    from telethon.sessions import StringSession
                    client = TelegramClient(StringSession(row["session_str"]),
                                            cfg.API_ID, cfg.API_HASH)
                    await client.connect()
                    if await client.is_user_authorized():
                        me = await client.get_me()
                        register_client(admin_id, client, me.id, me.first_name or "")
                        db.save_session(admin_id, row["session_str"], me.id, me.first_name or "")
                        log.info("Watchdog reconnected session for admin %s", admin_id)
                        await _notify_admin(admin_id, "♻️ <b>Session reconnected</b> — files can be served again.")
                    else:
                        unregister_client(admin_id)
                        log.warning("Session for admin %s is no longer authorized", admin_id)
                        await _notify_admin(
                            admin_id,
                            "⚠️ <b>Session logged out.</b> Send /session to connect a new one.",
                        )
                except Exception as exc:
                    log.warning("watchdog could not restore session for %s: %s", admin_id, exc)
        except Exception as exc:
            log.error("watchdog error: %s", exc)
        await asyncio.sleep(120)


async def campaign_loop() -> None:
    """Broadcast studio worker.

    * launches campaigns whose scheduled time has arrived,
    * resumes campaigns that were interrupted by a restart (queue is in SQLite),
    * never starts anything while the bot is offline — it just waits.
    """
    from app import runtime
    from app.services import broadcast

    while True:
        try:
            if runtime.bot_online():
                # Explicitly queued campaigns have already been approved by the
                # admin, so they always start once Telegram is online. The setting
                # only controls interrupted `running` campaigns after a restart.
                broadcast.resume_unfinished(
                    include_running=settings.get_bool("BROADCAST_AUTO_RESUME", True))
                for campaign in db.due_campaigns():
                    if broadcast.is_running(campaign["id"]):
                        continue
                    log.info("Scheduled campaign #%s is due — starting", campaign["id"])
                    broadcast.start_campaign(campaign["id"])
        except Exception as exc:  # never let a worker die
            log.error("campaign loop error: %s", exc)
        await asyncio.sleep(20)


async def _notify_admin(admin_id: int, text: str) -> None:
    try:
        await safe_call(bot.send_message, admin_id, text, what="admin_notice",
                        retries=1, raise_after_retries=False)
    except Exception:
        pass


async def mirror_loop() -> None:
    """Keep the bot-side media cache warm.

    Every file that the bot cannot read directly is copied **once**, in the
    background. That is what makes a click instant and a broadcast cheap — and
    it removes the old “forward to the board and delete it again” churn that
    ended in FloodWait every time a few users clicked the same video.
    """
    from app.services.media_cache import warm_mirrors
    await asyncio.sleep(45)                       # let the bot settle after boot
    while True:
        try:
            if not db.missing_mirrors(1):
                await asyncio.sleep(900)
                continue
            result = await warm_mirrors(limit=8, delay=2.0)
            if result["mirrored"]:
                log.info("Media cache: %s file(s) mirrored, %s waiting",
                         result["mirrored"], result["missing"])
        except Exception as exc:                  # never let a worker die
            log.error("mirror loop error: %s", exc)
        await asyncio.sleep(120)


async def digest_loop() -> None:
    """Weekly “new this week” broadcast — the helpful nudge that sells content.

    Runs only when the owner switches it on in the panel. For every store that got
    new files in the last 7 days it queues one campaign to that store's audience
    (free stores: everyone; paid stores: buyers + drip subscribers).
    """
    while True:
        try:
            if settings.get_bool("DIGEST_ENABLED", False):
                now = local_time_str()
                weekday = int(time.strftime("%w", time.localtime()))
                hour = int(now[:2])
                wanted_day = settings.get_int("DIGEST_WEEKDAY", 4)
                wanted_hour = settings.get_int("DIGEST_HOUR", 10)
                today = local_date_str()
                if weekday == wanted_day % 7 and hour == wanted_hour \
                        and db.get_meta("digest_last") != today:
                    db.set_meta("digest_last", today)
                    await run_digest()
        except Exception as exc:
            log.error("digest loop error: %s", exc)
        await asyncio.sleep(600)


async def run_digest(force: bool = False) -> dict:
    """Queue the weekly digest campaigns. Returns a small report."""
    from app import runtime
    from app.services import broadcast

    week_ago = time.time() - 7 * 86400
    max_files = max(1, settings.get_int("DIGEST_MAX_FILES", 2))
    queued = []
    for store in db.all_stores():
        fresh = [f for f in db.files_of(store["id"], newest_first=True)
                 if (f.get("created_at") or 0) >= week_ago][:max_files]
        if not fresh:
            continue
        audience = f"store:{store['id']}"
        targets = broadcast.resolve_audience(store["admin_id"], audience)
        if not targets:
            continue
        title = f"সাপ্তাহিক ডাইজেস্ট — {store['name']}"
        text = (f"🆕 <b>এই সপ্তাহের নতুন ভিডিও</b>\n🏪 <b>{store['name']}</b>\n\n"
                f"{len(fresh)} টি নতুন কনটেন্ট যোগ হয়েছে — নিচের বাটনে চাপ দিন।")
        campaign = broadcast.create_campaign(
            store["admin_id"], title=title, text=text, audience=audience,
            files=[f["id"] for f in fresh], start=True)
        queued.append({"store": store["name"], "campaign": campaign["id"],
                       "targets": len(targets), "files": len(fresh)})
        log.info("Digest queued for store %s → %s users", store["id"], len(targets))
    if queued and runtime.bot_online():
        for item in queued:
            if not broadcast.is_running(item["campaign"]):
                broadcast.start_campaign(item["campaign"])
    return {"ok": True, "queued": queued}


def start_all() -> list[asyncio.Task]:
    from app.runtime import spawn
    tasks = [
        spawn(drip_loop()),
        spawn(gc_loop()),
        spawn(backup_loop()),
        spawn(reminder_loop()),
        spawn(session_watchdog()),
        spawn(campaign_loop()),
        spawn(mirror_loop()),
        spawn(digest_loop()),
        spawn(owner_report_loop()),
    ]
    log.info("Started %s background workers", len(tasks))
    return tasks
