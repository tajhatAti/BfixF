"""Admin alerts — “নতুন অর্ডার, নতুন টিকিট, নতুন ইউজার, আটকে যাওয়া আপলোড”.

The owner asked for one thing only: he must *know* when something happens, without
sitting in the panel all day. Every alert goes through this module so there is one
place to configure it:

* ``ALERT_ENABLED``  — master switch (panel → ⚙️ সেটিংস → 🔔 নোটিফিকেশন)
* ``ALERT_NEW_USER`` / ``ALERT_ORDER`` / ``ALERT_TICKET`` / ``ALERT_STUCK_UPLOAD``
* ``ALERT_CHANNEL`` — a private group/channel to collect alerts in; empty = DM
  every admin. A busy bot means a busy DM list, so a dedicated “admin inbox”
  channel is often the better choice.

Repeat protection: the same kind of alert for the same user is throttled
(``COOLDOWN``), so a keyboard-smashing user cannot flood the owner's phone.
"""
from __future__ import annotations

import time

from app import config as cfg, runtime
from app.logger import log
from app.services import settings
from app.services.telegram import safe_call
from app.utils import esc

COOLDOWN = 600.0                      # seconds between identical alerts
_last_sent: dict[str, float] = {}


def enabled(kind: str = "") -> bool:
    if not settings.get_bool("ALERT_ENABLED", True):
        return False
    setting = {
        "new_user": "ALERT_NEW_USER",
        "order": "ALERT_ORDER",
        "ticket": "ALERT_TICKET",
        "stuck": "ALERT_STUCK_UPLOAD",
        "broadcast": "ALERT_BROADCAST",
    }.get(kind)
    if setting and not settings.get_bool(setting, True):
        return False
    return True


def throttle_key(kind: str, dedupe: str) -> str:
    return f"{kind}:{dedupe}"


def throttled(kind: str, dedupe: str = "") -> bool:
    """True when the same alert was sent recently — callers skip it."""
    if not dedupe:
        return False
    stamp = _last_sent.get(throttle_key(kind, dedupe), 0)
    return (time.time() - stamp) < COOLDOWN


def mark(kind: str, dedupe: str = "") -> None:
    if dedupe:
        _last_sent[throttle_key(kind, dedupe)] = time.time()


def _targets() -> list[int]:
    """A dedicated inbox channel if configured, otherwise every admin DM."""
    channel = (settings.get_str("ALERT_CHANNEL") or "").strip()
    ids: list[int] = []
    if channel:
        raw = channel.lstrip("@")
        if raw.lstrip("-").isdigit():
            ids.append(int(raw))
        else:
            ids.append(0)              # resolved by username below
    return ids or [int(i) for i in cfg.ADMIN_IDS]


async def send(kind: str, text: str, *, buttons=None, dedupe: str = "",
               silent: bool = False) -> int:
    """Deliver one alert to the admins (or the alert channel). Returns how many."""
    if not enabled(kind):
        return 0
    if throttled(kind, dedupe):
        log.debug("alert %s throttled (%s)", kind, dedupe)
        return 0
    bot = runtime.bot
    if bot is None:
        return 0
    channel = (settings.get_str("ALERT_CHANNEL") or "").strip()
    targets: list = []
    if channel:
        targets = [channel if not channel.lstrip("-").isdigit() else int(channel)]
    else:
        targets = [int(i) for i in cfg.ADMIN_IDS]
    sent = 0
    for target in targets:
        try:
            await safe_call(bot.send_message, target, text, buttons=buttons,
                            link_preview=False, silent=silent,
                            what=f"alert_{kind}", retries=1,
                            raise_after_retries=False)
            sent += 1
        except Exception as exc:
            log.debug("alert %s → %s failed: %s", kind, target, exc)
    if sent:
        mark(kind, dedupe)
    return sent


# ---------------------------------------------------------------- convenience
async def new_user(user_id: int, name: str = "", username: str = "") -> int:
    who = esc(name or "নতুন ইউজার")
    handle = f" · @{esc(username)}" if username else ""
    return await send(
        "new_user",
        f"👋 <b>নতুন ইউজার বটে এসেছেন</b>\n👤 {who}{handle} · <code>{user_id}</code>",
        dedupe=str(user_id))


async def new_order(order: dict) -> int:
    return await send(
        "order",
        f"🧾 <b>নতুন অর্ডার #{order.get('id')}</b>\n"
        f"👤 <code>{order.get('user_id')}</code> · 💰 {order.get('amount')} "
        f"{esc(order.get('currency') or '')}\nঅ্যাডমিন প্যানেল → Orders থেকে যাচাই করুন।",
        dedupe=str(order.get("id")))


async def stuck_upload(user_id: int, reason: str = "", name: str = "") -> int:
    """Something went wrong *while* files were being added — the owner asked for this."""
    from app.services import bot_texts

    who = f"{esc(name)} · <code>{user_id}</code>" if name else f"<code>{user_id}</code>"
    text = bot_texts.render("stuck_upload", who=who, reason=esc(reason or "অজানা"))
    return await send("stuck", text, dedupe=str(user_id))


async def broadcast_done(campaign: dict, result: dict) -> int:
    return await send(
        "broadcast",
        f"📢 <b>ব্রডকাস্ট #{campaign.get('id')} শেষ</b>\n"
        f"✅ গেছে: {result.get('sent', 0)} · 🚫 ব্লক: {result.get('blocked', 0)} · "
        f"⚠️ ব্যর্থ: {result.get('failed', 0)}",
        dedupe=f"campaign:{campaign.get('id')}")
