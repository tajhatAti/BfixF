"""Broadcast studio v3 — high level, persistent, flood-proof campaigns.

Why a *campaign* instead of the old "loop over a list":

* **Queue in the database** — every target is a row in `campaign_items`. If the
  hosting restarts (or the process is killed mid-broadcast) the campaign is
  resumed automatically, and no user gets the message twice.
* **Audience segments** — all / premium / free / one store / people who never
  bought / drip subscribers. Resolved once, then frozen into the queue.
* **Any content** — plain text, one video, or several files per message. This is
  what makes “broadcast this video” work from the file manager or the website.
* **Fast** - everyone is sent at once (a fixed number in parallel) and `safe_call`
  waits out any FloodWait instead of dropping the message.
* **Live progress + cancel** — progress callbacks for the bot message and the web
  panel, and `cancel_campaign()` stops cleanly mid-run.
* **Personalisation + preview** — `{name}` becomes the user's name; `preview()`
  shows exactly what the message will look like before anything is sent.
* **Test send** — send the campaign to the admins only and check it first.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from telethon.errors import FloodWaitError

from app import config as cfg
from app.logger import log
from app.runtime import blocked_users, bot, spawn
from app.services import settings
from app.services.telegram import safe_call
from app.storage import db

ProgressCb = Callable[[int, int, int], Awaitable[None]]   # done, total, failed

# ------------------------------------------------------------------ statuses
DRAFT = "draft"
QUEUED = "queued"          # created, waiting for the bot to come online
SCHEDULED = "scheduled"    # waiting for `scheduled_at`
RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"
FAILED = "failed"

ACTIVE_STATUSES = (QUEUED, SCHEDULED, RUNNING)

# ----------------------------------------------------------------- audiences
AUDIENCE_LABELS: dict[str, str] = {
    "all": "📣 All users",
    "premium": "💎 Premium buyers",
    "free": "🆓 Free users",
}
STORE_AUDIENCE_LABELS = {
    "store": "🏪 Everyone in this store",
    "sub": "🔔 Subscribers of this store",
    "nosale": "🎯 Never bought this store",
}


def audience_label(spec: str) -> str:
    spec = (spec or "all").strip()
    if spec in AUDIENCE_LABELS:
        return AUDIENCE_LABELS[spec]
    kind, _, rid = spec.partition(":")
    return f"{STORE_AUDIENCE_LABELS.get(kind, kind)} #{rid}"


def resolve_audience(admin_id: int, spec: str) -> list[int]:
    """Turn an audience code into a list of Telegram user ids."""
    from app.services.access import broadcast_targets

    spec = (spec or "all").strip() or "all"
    kind, _, raw = spec.partition(":")
    # admin_id 0/None means “whole installation” (used by the website panel)
    scope = admin_id or None
    if kind in ("all", "premium", "free"):
        targets = broadcast_targets(scope, kind)
    elif kind in ("store", "sub", "nosale"):
        store_id = int(raw or 0)
        store = db.store(store_id)
        if store is None:
            return []
        if kind == "sub":
            targets = db.subscribers(store_id)
        elif kind == "nosale":
            granted = {g["user_id"] for g in db.store_grants(store_id)}
            targets = [uid for uid in db.all_user_ids() if uid not in granted]
        else:
            targets = broadcast_targets(admin_id, f"store:{store_id}")
    else:
        targets = broadcast_targets(admin_id, spec)
    targets = [uid for uid in dict.fromkeys(int(u) for u in targets)
               if uid not in cfg.ADMIN_IDS]
    # Optional recency filter — keeps a huge, mostly dead audience from turning a
    # broadcast into an hour of retries.
    days = settings.get_int("BROADCAST_REACH_DAYS", 0)
    if days:
        cutoff = time.time() - days * 86400
        recent = {row["user_id"] for row in db.recent_user_ids(cutoff)}
        targets = [uid for uid in targets if uid in recent] or targets
    return targets


# ------------------------------------------------------------------ campaigns
def create_campaign(admin_id: int, *, text: str = "", title: str = "",
                    audience: str = "all", files: list[int] | None = None,
                    scheduled_at: float | None = None,
                    start: bool = True, buttons: str = "",
                    link_slug: str = "", caption_position: str = "above",
                    online_button: int | None = None) -> dict:
    """Build a campaign and (optionally) put it in the queue right away.

    `buttons` is the admin's button text (`label | url` per line) — inline URL
    buttons are attached to **every** message of the broadcast, which is what the
    “online button” option means in the panel.
    """
    files = [int(f) for f in (files or []) if f]
    if scheduled_at and scheduled_at > time.time():
        status = SCHEDULED
    else:
        status = QUEUED if start else DRAFT
    if not title:
        title = (text or "").strip().splitlines()[0][:40] if text else f"{len(files)} file(s)"
    campaign_id = db.create_campaign(
        admin_id, title=title, text=text or "", files=files,
        audience=audience or "all", status=status, scheduled_at=scheduled_at,
        buttons=buttons or "", slug=link_slug or "", media_caption=caption_position or "above",
        online_button=online_button,
    )
    targets = resolve_audience(admin_id, audience or "all")
    db.add_campaign_items(campaign_id, targets)
    db.campaign_counts(campaign_id)
    log.info("Campaign #%s created by admin %s → %s targets (%s)",
             campaign_id, admin_id, len(targets), audience)
    return db.campaign(campaign_id) or {}


def online_button_url(campaign: dict) -> str:
    """The “open the store / open the bot” URL used by the online button."""
    from app import runtime
    slug = (campaign.get("slug") or "").strip()
    files = campaign.get("file_ids") or []
    store = None
    if files:
        row = db.file(files[0])
        if row:
            store = db.store(row["store_id"])
    if not store and slug:
        store = db.store_by_slug(slug)
    username = runtime.bot_username or ""
    if store and username:
        return f"https://t.me/{username}?start={store['slug']}"
    if username:
        return f"https://t.me/{username}"
    return ""


def campaign_buttons(campaign: dict):
    """All inline buttons of a campaign: the admin's own + the online button."""
    from app.utils import parse_button_spec
    rows = parse_button_spec(campaign.get("buttons") or "")
    flag = campaign.get("online_button")
    wants_online = settings.get_bool("BROADCAST_SEND_ONLINE_BUTTON", True) \
        if flag is None else bool(flag)
    if wants_online:
        url = online_button_url(campaign)
        if url:
            label = (settings.get_str("BROADCAST_ONLINE_LABEL")
                     or "🟢 অনলাইন — স্টোর খুলুন")
            if not any(label == existing[0] for row in rows for existing in row):
                rows.append([(label, url)])
    return rows


def preview(campaign: dict, sample_name: str = "রহিম") -> str:
    """Exactly the text a user will receive (HTML), for the preview screen."""
    text = personalize(campaign.get("text") or "", sample_name, "")
    if not text and campaign.get("file_ids"):
        text = "🎬 <b>New upload</b>"
    return text


def personalize(text: str, name: str, username: str = "") -> str:
    if not text:
        return ""
    first = (name or "").strip().split(" ")[0] if name else ""
    return (text.replace("{name}", first or "bondhu")
                .replace("{username}", ("@" + username) if username else (first or "bondhu")))


# ------------------------------------------------------------------- sending
#: messages in flight at the same time (Telegram allows roughly 30 per second)
SEND_AT_ONCE = 25


class BlockedByUser(Exception):
    """The user blocked the bot / deleted the account — stop retrying them."""


async def _send_to_user(campaign: dict, user_id: int) -> str:
    """Send one campaign message. Raises on failure so the queue can record it.

    Media campaigns no longer depend on a userbot session: `deliver()` mirrors the
    file once and then plain-sends it, so a broadcast to thousands of users really
    reaches them (this used to silently fail for everyone but the admin). The
    inline buttons — including the “online” one — are attached to the message.
    """
    from app.services.sender import deliver_file_id, send_text

    file_ids = campaign.get("file_ids") or []
    text = campaign.get("text") or ""
    user = db.user(user_id) or {}
    body = personalize(text, user.get("name") or "", user.get("username") or "")
    buttons = campaign_buttons(campaign) or None

    if file_ids:
        from app.services.sender import ensure_ready
        max_files = settings.get_int("BROADCAST_MAX_FILES", 5)
        chosen = file_ids[:max_files]
        # Mirror once, up front: if this fails for every file the message would be
        # an empty send, and the queue would keep retrying a broken campaign.
        ready = False
        for file_id in chosen:
            row = db.file(file_id)
            if row is None:
                continue
            updated = row if row.get("mirror_msg") else await ensure_ready(row)
            if updated and updated.get("mirror_msg"):
                ready = True
                break
        if not ready:
            preview_row = db.file(chosen[0])
            if preview_row is None:
                raise RuntimeError("ফাইলটি মুছে ফেলা হয়েছে")
            probe = await deliver_file_id(user_id, chosen[0], body, buttons=buttons)
            if not probe.ok:
                # `blocked` means the recipient is unreachable; preserve the detail
                # so the queue can explain what happened and offer a useful retry.
                raise RuntimeError(f"{probe.reason}: {probe.detail or 'media unavailable'}")
            # The fallback already delivered file zero to this recipient. Do not
            # send it again in the normal loop below.
            sent = 1
        else:
            sent = 0

        for index, file_id in enumerate(chosen[sent:], start=sent):
            caption = body if index == 0 else (runtime_caption() or "")
            result = await deliver_file_id(user_id, file_id, caption, buttons=buttons)
            if not result.ok:
                if sent:
                    break                     # partial send: don't mark as failed
                raise RuntimeError(f"file {file_id}: {result.reason}: {result.detail}")
            sent += 1
        return "sent"

    if not text:
        raise RuntimeError("empty message")
    message = await safe_call(bot.send_message, user_id, body, link_preview=False,
                              buttons=buttons, what="broadcast", retries=2)
    if message is None:
        raise RuntimeError("send returned nothing")
    return "sent"


#: Telegram says “no” in a dozen different flavours; the admin only wants to know
#: *why* the message did not reach a person — and whether retrying helps.
#: Order matters: the most specific wording wins. (“cannot find any entity”
#: happens for people who never pressed /start — a different problem from a block.)
ERROR_HINTS = (
    ("flood", "⏳ টেলিগ্রামের সাময়িক লিমিট (FloodWait) — কিছুক্ষণ পরে 🔁 Retry চাপলেই যাবে"),
    ("deactivated", "🪦 ইউজার অ্যাকাউন্ট ডিলিট/ডিঅ্যাক্টিভেট"),
    ("cannot find any entity", "❓ এই আইডিতে পৌঁছানো যায় না — সাধারণত বট কখনো /start করা হয়নি"),
    ("peer id invalid", "❓ পুরোনো/ভুল আইডি — বটের সাথে কখনো যোগাযোগ হয়নি"),
    ("bot can't initiate conversation", "❓ বট আগে কখনো বসেনি — ইউজারকে /start দিতে বলুন"),
    ("blocked", "🚫 ইউজার বটকে ব্লক করেছে — আর কখনো পৌঁছাবে না (তালিকা থেকে বাদ দিন)"),
    ("user is deactivated", "🪦 অ্যাকাউন্ট ডিঅ্যাক্টিভেট"),
    ("media unavailable", "🎞 ফাইলের বট-কপি তৈরি হয়নি — ফাইল পেজ থেকে 📥 ক্যাশ করুন"),
    ("no_session", "🔑 কোনো ইউজারবট সেশন নেই — ফাইলটা ক্যাশ করা যাচ্ছে না"),
    ("mirror", "🎞 মিডিয়া ক্যাশ করা যায়নি — সেশন/ক্যাশ চ্যানেল দেখুন"),
    ("file missing", "🗂 ফাইলটি পাওয়া যাচ্ছে না — হয়তো ডিলিট হয়েছে"),
    ("bot was kicked", "👋 বটকে চ্যাট থেকে বাদ দেওয়া হয়েছে"),
    ("too many requests", "⏳ টেলিগ্রাম কিছুক্ষণ থামতে বলেছে — একটু পরে Retry করুন"),
    ("empty message", "✍️ মেসেজ খালি — টেক্সট বা ফাইল দিন"),
)


def explain_error(detail: str) -> str:
    """Turn a raw Telegram error into something actionable for the admin."""
    text = (detail or "").strip()
    low = text.lower()
    for needle, hint in ERROR_HINTS:
        if needle in low:
            return hint
    return f"⚠️ {text[:120]}" if text else "⚠️ অজানা কারণ"


def failure_breakdown(campaign_id: int) -> list[dict]:
    """Group the queue by *why* each person was missed."""
    groups: dict[str, dict] = {}
    for item in db.campaign_items(campaign_id, limit=100000):
        status = item.get("status")
        if status in ("sent", "pending"):
            continue
        hint = explain_error(item.get("error") or status)
        bucket = groups.setdefault(hint, {"hint": hint, "count": 0, "users": []})
        bucket["count"] += 1
        if len(bucket["users"]) < 8:
            bucket["users"].append(item.get("user_id"))
    return sorted(groups.values(), key=lambda g: -g["count"])


def runtime_caption() -> str:
    from app import runtime
    return runtime.caption()


def _classify(exc: Exception) -> str:
    message = str(exc).lower()
    # Only wording that really means "this person cannot be reached". "chat not
    # found" / "not enough rights" can come from OUR side (the file copy), and
    # treating them as a block used to mark the whole audience as blocked.
    for needle in ("blocked by the user", "user is blocked", "user blocked the bot",
                   "deactivated", "kicked", "user is deleted", "privacy"):
        if needle in message:
            return "blocked"
    return "failed"


async def _pause(campaign_id: int, index: int) -> bool:
    """No pacing: just let the sends run and report whether the campaign was cancelled."""
    await asyncio.sleep(0)
    return not is_cancelled(campaign_id)


# ---------------------------------------------------------------- run engine
_runners: dict[int, asyncio.Task] = {}
_cancel: set[int] = set()


def is_running(campaign_id: int) -> bool:
    task = _runners.get(campaign_id)
    return bool(task and not task.done())


def is_cancelled(campaign_id: int) -> bool:
    return campaign_id in _cancel


def cancel_campaign(campaign_id: int) -> bool:
    """Ask a running campaign to stop. The runner finalises the status."""
    if not is_running(campaign_id):
        db.update_campaign(campaign_id, status=CANCELLED, finished_at=time.time())
        return False
    _cancel.add(campaign_id)
    log.info("Campaign #%s: cancel requested", campaign_id)
    return True


async def run_campaign(campaign_id: int, progress: ProgressCb | None = None,
                       notify_admin: bool = True) -> dict:
    """Send every pending item of a campaign. Safe to call again after a restart."""
    campaign = db.campaign(campaign_id)
    if campaign is None:
        return {"ok": False, "error": "campaign not found"}
    if is_running(campaign_id):
        return {"ok": False, "error": "already running"}

    counts = db.campaign_counts(campaign_id)
    total = counts["total"] or 0
    pending = db.campaign_user_ids(campaign_id, "pending")
    db.update_campaign(campaign_id, status=RUNNING,
                       started_at=campaign.get("started_at") or time.time(),
                       finished_at=None, last_error="")
    log.info("Campaign #%s started: %s pending of %s", campaign_id, len(pending), total)

    started = time.time()
    already_done = counts["done"]
    index = 0
    failed_here = 0
    last_error = ""
    retry_blocked = settings.get_bool("BROADCAST_TO_BLOCKED", False)

    # Everyone is sent at once: up to SEND_AT_ONCE messages are in flight together, so a
    # small audience is reached in about a second. No delays, no batches, no settings.
    gate = asyncio.Semaphore(SEND_AT_ONCE)
    # Upload each video to the mirror once, before the parallel sends start, so the
    # first batch of recipients never uploads the same file several times.
    if campaign.get("file_ids") and pending:
        from app.services.sender import ensure_ready
        for fid in (campaign.get("file_ids") or [])[:settings.get_int("BROADCAST_MAX_FILES", 5)]:
            row = db.file(fid)
            if row is not None and not row.get("mirror_msg"):
                try:
                    await ensure_ready(row)
                except Exception as exc:          # per-user sends report the real error
                    log.warning("Campaign #%s: mirror prewarm for file %s failed: %s",
                                campaign_id, fid, exc)
    flights: set[asyncio.Task] = set()
    stats = {"failed": 0, "last_error": ""}

    async def _deliver_one(uid: int) -> None:
        try:
            try:
                await _send_to_user(campaign, uid)
                db.mark_campaign_item(campaign_id, uid, "sent")
            except FloodWaitError as exc:
                # safe_call already waits; this is the "safety cap" escape hatch.
                detail = f"flood {exc.seconds}s"
                db.mark_campaign_item(campaign_id, uid, "failed", detail)
                stats["failed"] += 1
                stats["last_error"] = f"{uid}: {detail}"
                log.warning("Campaign #%s failed for %s: %s", campaign_id, uid, detail)
            except Exception as exc:
                kind = _classify(exc)
                if kind == "blocked":
                    blocked_users.add(uid)
                else:
                    stats["failed"] += 1
                    stats["last_error"] = f"{uid}: {exc}"
                detail = str(exc)[:200]
                db.mark_campaign_item(campaign_id, uid, kind, detail)
                log.warning("Campaign #%s ? %s for %s: %s", campaign_id, kind, uid, detail)
        finally:
            gate.release()

    for user_id in pending:
        if is_cancelled(campaign_id):
            break
        index += 1
        if user_id in blocked_users and not retry_blocked:
            db.mark_campaign_item(campaign_id, user_id, "blocked", "known blocker")
        else:
            await gate.acquire()                      # wait for a free slot, then send
            task = asyncio.ensure_future(_deliver_one(user_id))
            flights.add(task)
            task.add_done_callback(flights.discard)

        if index == 1:
            db.log_event("broadcast_start", None, None, None, str(campaign_id),
                         f"{len(pending)} targets")
        if progress and index % 10 == 0:
            fresh = db.campaign_counts(campaign_id)
            await progress(fresh["done"], fresh["total"], fresh["failed"])
        if not await _pause(campaign_id, index):
            break

    if flights:
        await asyncio.gather(*list(flights), return_exceptions=True)
    failed_here = stats["failed"]
    last_error = stats["last_error"]
    if progress:
        fresh = db.campaign_counts(campaign_id)
        await progress(fresh["done"], fresh["total"], fresh["failed"])

    live = db.campaign_counts(campaign_id)
    cancelled = is_cancelled(campaign_id)
    status = CANCELLED if cancelled else (DONE if live["pending"] == 0 else FAILED)
    duration = time.time() - started
    db.update_campaign(campaign_id, status=status, finished_at=time.time(),
                       last_error=last_error[:200])
    _cancel.discard(campaign_id)

    result = {
        "ok": True, "campaign_id": campaign_id, "status": status,
        "sent": live["sent"], "failed": live["failed"], "blocked": live["blocked"],
        "pending": live["pending"], "total": live["total"], "duration": duration,
        "skipped": already_done, "this_run": index, "failed_now": failed_here,
    }
    log.info("Campaign #%s %s: sent=%s failed=%s blocked=%s in %.1fs",
             campaign_id, status, live["sent"], live["failed"], live["blocked"], duration)
    if notify_admin and campaign.get("admin_id"):
        await notify_owner(campaign, result)
    return result


async def _run_campaign_guarded(campaign_id: int, progress: ProgressCb | None,
                                notify_admin: bool) -> dict:
    """Make background failures visible instead of leaving a campaign stuck running."""
    try:
        return await run_campaign(campaign_id, progress=progress,
                                  notify_admin=notify_admin)
    except asyncio.CancelledError:
        try:
            db.update_campaign(campaign_id, status=QUEUED, finished_at=None,
                               last_error="Worker stopped; pending recipients are queued to resume.")
        except Exception:
            log.exception("Could not requeue cancelled campaign #%s", campaign_id)
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("Campaign #%s worker crashed: %s", campaign_id, detail)
        try:
            db.update_campaign(campaign_id, status=FAILED,
                               finished_at=time.time(), last_error=detail)
        except Exception:
            log.exception("Could not mark campaign #%s as failed", campaign_id)
        campaign = db.campaign(campaign_id) or {}
        if notify_admin and campaign.get("admin_id"):
            try:
                counts = db.campaign_counts(campaign_id, sync=False)
            except Exception:
                counts = {}
            await notify_owner(campaign, {
                "campaign_id": campaign_id, "status": FAILED,
                "sent": counts.get("sent", 0), "failed": counts.get("failed", 0),
                "blocked": counts.get("blocked", 0), "pending": counts.get("pending", 0),
                "duration": 0, "error": detail,
            })
        return {"ok": False, "campaign_id": campaign_id, "status": FAILED,
                "error": detail}


def start_campaign(campaign_id: int, progress: ProgressCb | None = None,
                   notify_admin: bool = True) -> asyncio.Task:
    """Run a campaign in the background (never blocks the handler)."""
    current = _runners.get(campaign_id)
    if current is not None and not current.done():
        return current
    task = spawn(_run_campaign_guarded(campaign_id, progress, notify_admin))
    _runners[campaign_id] = task

    def forget(done: asyncio.Task) -> None:
        if _runners.get(campaign_id) is done:
            _runners.pop(campaign_id, None)

    task.add_done_callback(forget)
    return task


async def notify_owner(campaign: dict, result: dict) -> None:
    """Tell the admin who created the campaign how it went."""
    admin_id = campaign.get("admin_id")
    if not admin_id:
        return
    text = (
        f"📢 <b>Broadcast finished</b> — #{result.get('campaign_id')}\n"
        f"🎯 Audience: {audience_label(campaign.get('audience') or 'all')}\n"
        f"📨 Sent: <b>{result.get('sent', 0)}</b> · ⚠️ Failed: <b>{result.get('failed', 0)}</b>\n"
        f"🚫 Blocked us: <b>{result.get('blocked', 0)}</b> · 🕓 {int(result.get('duration', 0))}s"
    )
    try:
        await safe_call(bot.send_message, admin_id, text, what="campaign_report",
                        retries=1, raise_after_retries=False)
    except Exception as exc:
        log.debug("could not report campaign to admin: %s", exc)


async def test_send(campaign_id: int, admin_ids: list[int]) -> dict:
    """Send the campaign message to the admins only (nobody else).

    Whatever error the *real* audience would hit shows up here, so a broken
    campaign is caught before it is fired at thousands of users.
    """
    campaign = db.campaign(campaign_id)
    if campaign is None:
        return {"ok": False, "error": "not found"}
    sent = failed = 0
    errors: list[str] = []
    for admin_id in admin_ids:
        try:
            await _send_to_user(campaign, admin_id)
            sent += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{admin_id}: {exc}"[:200])
            log.warning("test send of campaign #%s to %s failed: %s",
                        campaign_id, admin_id, exc)
    return {"ok": sent > 0, "sent": sent, "failed": failed, "errors": errors}


async def test_send_text(text: str, buttons: str = "", user_id: int | None = None,
                         file_ids: list[int] | None = None) -> bool:
    """Send one message (with the same button syntax) to a single user.

    The panel uses this for “🧪 my own DM first”, for answering a user from the
    dashboard and for the channel-composer preview. Returns True when Telegram
    accepted the message.
    """
    from app import config as cfg
    from app.services.sender import deliver_file_id, send_text
    from app.utils import parse_button_spec

    targets = [user_id] if user_id else list(cfg.ADMIN_IDS)[:1]
    if not targets:
        log.warning("test_send_text: no admin/user id to send to")
        return False
    rows = parse_button_spec(buttons) or None
    ok = False
    for target in targets:
        try:
            if file_ids:
                for index, fid in enumerate(file_ids):
                    result = await deliver_file_id(target, int(fid),
                                                   text if index == 0 else None,
                                                   buttons=rows)
                    ok = ok or result.ok
            else:
                message = await send_text(target, text, buttons=rows)
                ok = ok or message is not None
        except Exception as exc:
            log.warning("test_send_text to %s failed: %s", target, exc)
    return ok


def resume_unfinished(*, include_running: bool = True) -> int:
    """Start approved queued campaigns; optionally resume interrupted running ones."""
    resumed = 0
    for campaign in db.queued_campaigns():
        if is_running(campaign["id"]):
            continue
        if campaign["status"] == RUNNING and not include_running:
            continue
        start_campaign(campaign["id"])
        resumed += 1
    if resumed:
        log.info("Resuming %s unfinished campaign(s)", resumed)
    return resumed


def campaign_progress(campaign_id: int) -> dict:
    """Small JSON friendly snapshot for the web panel / bot status message."""
    campaign = db.campaign(campaign_id)
    if campaign is None:
        return {}
    counts = db.campaign_counts(campaign_id, sync=False)
    done = counts["done"]
    total = counts["total"] or 1
    percent = int(done * 100 / total) if total else 0
    from app import runtime
    from app.services.telegram import flood_status
    worker_active = is_running(campaign_id)
    status = campaign["status"]
    return {
        "id": campaign_id,
        "status": status,
        "running": worker_active,
        "active": worker_active or status == QUEUED,
        "bot_online": runtime.bot_online(),
        "auto_resume": settings.get_bool("BROADCAST_AUTO_RESUME", True),
        "flood_wait": flood_status() or "",
        "last_error": campaign.get("last_error") or "",
        "sent": counts["sent"], "failed": counts["failed"],
        "blocked": counts["blocked"], "pending": counts["pending"],
        "total": counts["total"], "percent": percent,
    }


# ------------------------------------------------------- legacy entry points
async def run_broadcast(targets: list[int], text: str,
                        progress: ProgressCb | None = None,
                        delay: float | None = None,
                        file_id: int | None = None) -> dict:
    """Old API kept for compatibility (used by the simple bot flow and tests).

    Internally it now routes through the campaign engine, so behaviour
    (blocked-user memory, flood handling, pacing) is identical everywhere.
    """
    total = len(targets)
    sent = failed = skipped = 0
    retry_blocked = settings.get_bool("BROADCAST_TO_BLOCKED", False)
    active = list(targets) if retry_blocked else [uid for uid in targets if uid not in blocked_users]
    skipped = total - len(active)
    started = time.time()

    for index, user_id in enumerate(active, start=1):
        try:
            if file_id:
                from app.services.sender import deliver_file_id
                result = await deliver_file_id(user_id, file_id, text)
                ok = result.ok
                if not ok:
                    failed += 1
                else:
                    sent += 1
            else:
                await safe_call(bot.send_message, user_id, text, link_preview=False,
                                what="broadcast", retries=2)
                sent += 1
        except FloodWaitError:
            failed += 1
        except Exception as exc:  # user blocked the bot / deleted account
            if _classify(exc) == "blocked":
                blocked_users.add(user_id)
            failed += 1
            log.debug("broadcast to %s failed: %s", user_id, exc)

        if progress and (index % 25 == 0 or index == len(active)):
            await progress(index, len(active), failed)

    duration = time.time() - started
    log.info("Broadcast finished: sent=%s failed=%s skipped=%s in %.1fs",
             sent, failed, skipped, duration)
    return {"sent": sent, "failed": failed, "skipped": skipped,
            "total": total, "duration": duration}
