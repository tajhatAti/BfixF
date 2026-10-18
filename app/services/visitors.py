"""Visitor analytics — who is on the website *right now*, and what they did.

The owner asked for one screen that answers, live:

    কতজন এখন অনলাইনে? কোথা থেকে এসেছে? কোন পেজ দেখছে? কোন ভিডিও চালাচ্ছে?
    কে বট থেকে এসেছে আর কে লিংক থেকে?

How it works, in plain terms
    1. Every visit gets a random id (`bf_vid`) in a first-party cookie. No
       third-party tracker, no external service, nothing leaves the server.
    2. Each request is folded into one **visit row** — a visit is “one person,
       one session”, closed after `VISIT_GAP_MINUTES` (30 min) of silence, which
       is the same rule Google Analytics uses.
    3. Pages and actions are kept as separate rows, so the owner sees a real
       timeline (“প্রথমে হোম → সিনেমা হাউস → ভিডিও চালালো → লিংক ক্লিক”).
    4. Administrators themselves are marked `is_admin = 1` and left out of the
       visitor numbers — otherwise the owner's own clicks would double everything.

Writes are deliberately cheap and throttled (a page view per navigation, one
`last_seen` touch per 15 s, one “online” read from memory) so a busy site never
beats up the SQLite file that the bot is also using.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta

from app.logger import log
from app.storage import db

#: A visit is closed after this many quiet minutes; a later hit starts a new one.
VISIT_GAP_MINUTES = 30
#: Everything older than this is pruned by the retention job.
KEEP_DAYS = 90
#: “এখন অনলাইন” = seen inside this window.
ONLINE_WINDOW = 300                     # 5 minutes
#: How often a visit row's `last_seen` is written (seconds).
TOUCH_INTERVAL = 15
#: Never store a full user-agent string longer than this.
UA_LIMIT = 300

COOKIE = "bf_vid"

_SAFE_VID = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
_BOTS = re.compile(r"(bot|crawler|spider|crawl|facebookexternalhit|preview|monitor|"
                   r"uptime|wget|curl|python-requests|headless)", re.I)
_GAME = {"mobile": "📱 মোবাইল", "tablet": "📱 ট্যাব", "desktop": "💻 ডেস্কটপ",
         "unknown": "❔ অজানা"}

#: in-memory mirrors of things that do not deserve a database write
_last_touch: dict[str, float] = {}
_seen: dict[str, float] = {}            # vid -> last request ts (for “online now”)

#: Friendly names for the actions other modules record.
EVENT_LABELS = {
    "page": "পেজ দেখছে",
    "store_open": "স্টোর খুলেছে",
    "media_open": "মিডিয়া খুলেছে",
    "media_play": "ভিডিও/ছবি চালালো",
    "media_failed": "মিডিয়া চলেনি",
    "search": "সার্চ করছে",
    "link_open": "লিংক খুলেছে",
    "draft_open": "ব্রডকাস্ট লিংক খুলেছে",
    "bot_open": "বটে গেল",
    "login": "অ্যাডমিন লগইন",
    "logout": "লগআউট",
    "signup": "নতুন হিসেবে এসেছে",
    "checkout": "কেনার পেজে গেছে",
    "coupon": "কুপন ব্যবহার করলো",
    "prepare": "মিডিয়া আনতে বললো",
}


# --------------------------------------------------------------------- helpers
def _now() -> float:
    return time.time()


def day_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else _now()))


def device_kind(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if not ua:
        return "unknown"
    if any(token in ua for token in ("ipad", "tablet", "kindle", "playbook")):
        return "tablet"
    if any(token in ua for token in ("mobi", "android", "iphone", "ipod", "windows phone")):
        return "mobile"
    if any(token in ua for token in ("windows", "mac os", "linux", "x11", "cros")):
        return "desktop"
    return "unknown"


def device_label(user_agent: str) -> str:
    return _GAME.get(device_kind(user_agent), _GAME["unknown"])


def browser_name(user_agent: str) -> str:
    ua = user_agent or ""
    for token, name in (("Edg", "Edge"), ("OPR", "Opera"), ("SamsungBrowser", "Samsung"),
                        ("Firefox", "Firefox"), ("Chrome", "Chrome"), ("Safari", "Safari"),
                        ("TelegramBot", "Telegram"), ("Telegram", "Telegram")):
        if token.lower() in ua.lower():
            return name
    return "অন্য"


def operating_system(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    for token, name in (("android", "Android"), ("iphone", "iOS"), ("ipad", "iPadOS"),
                        ("windows", "Windows"), ("mac os", "macOS"), ("linux", "Linux"),
                        ("cros", "ChromeOS")):
        if token in ua:
            return name
    return ""


def is_bot(user_agent: str) -> bool:
    return bool(_BOTS.search(user_agent or ""))


def new_vid() -> str:
    import secrets
    return secrets.token_urlsafe(12)


def valid_vid(vid: str | None) -> bool:
    return bool(vid and _SAFE_VID.match(vid))


def source_of(referrer: str, query: str = "") -> str:
    """“কোথা থেকে এসেছে?” — Telegram, Google, Facebook, সরাসরি…"""
    ref = (referrer or "").lower()
    if "t.me" in ref or "telegram" in ref:
        return "টেলিগ্রাম"
    if "google." in ref:
        return "গুগল"
    if "facebook." in ref or "fb." in ref:
        return "ফেসবুক"
    if "youtube." in ref or "youtu.be" in ref:
        return "ইউটিউব"
    if "bing." in ref:
        return "বিং"
    if "instagram." in ref:
        return "ইনস্টাগ্রাম"
    if "whatsapp" in ref:
        return "হোয়াটসঅ্যাপ"
    if ref and "://" in ref:
        return "আরেক সাইট"
    if query:
        return "লিংক থেকে"
    return "সরাসরি"


def _clean(text: str | None, limit: int = UA_LIMIT) -> str:
    return (text or "")[:limit]


def client_ip(request) -> str:
    """Real visitor IP behind a hosting proxy (X-Forwarded-For → X-Real-IP)."""
    for header in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        value = (request.headers.get(header) or "").strip()
        if value:
            return value.split(",")[0].strip()[:45]
    client = getattr(request, "client", None)
    return _clean(getattr(client, "host", ""), 45) if client else ""


def masked_ip(ip: str) -> str:
    """Show the network, not the person: 103.12.44.9 → 103.12.x.x"""
    if not ip:
        return "—"
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:2]) + "::…"
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.x.x"
    return ip


def country_of(request) -> str:
    """Cloudflare / hosting headers when they exist, otherwise nothing."""
    for header in ("cf-ipcountry", "x-country", "x-vercel-ip-country"):
        value = (request.headers.get(header) or "").strip().upper()
        if value and value not in ("XX", "T1"):
            return value[:2]
    lang = (request.headers.get("accept-language") or "").split(",")[0].strip()
    return lang[:12]


# ---------------------------------------------------------------------- writes
def ensure_visit(request, *, user_id: int = 0, is_admin: bool = False) -> str:
    """Return this visit's id, opening a new visit when the last one went quiet."""
    store_vid = getattr(request.state, "bf_vid", "") or ""
    vid = store_vid or _cookie_vid(request)
    now = _now()
    if not valid_vid(vid):
        vid = new_vid()
    request.state.bf_vid = vid
    last = _last_touch.get(vid, 0)
    fresh = db.visitors.visit(vid)
    if fresh is None or (now - float(fresh.get("last_seen") or 0)) > VISIT_GAP_MINUTES * 60:
        db.visitors.open_visit(
            vid, user_id=user_id, is_admin=1 if is_admin else 0,
            ip=client_ip(request), country=country_of(request),
            device=_GAME.get(device_kind(_ua(request)), ""), device_kind=device_kind(_ua(request)),
            os=operating_system(_ua(request)), browser=browser_name(_ua(request)),
            referrer=_clean(request.headers.get("referer"), 300),
            source=source_of(request.headers.get("referer") or "",
                             request.url.query or ""),
            landing=request.url.path[:120], user_agent=_clean(_ua(request)),
            offline=False)
    return vid


def _ua(request) -> str:
    return request.headers.get("user-agent") or ""


def _cookie_vid(request) -> str:
    try:
        return request.cookies.get(COOKIE) or ""
    except Exception:
        return ""


def touch(request, *, vid: str = "", event: str = "", path: str = "", title: str = "",
          status: int = 200, ms: int = 0, detail: str = "", store_id: int = 0,
          file_id: int = 0, user_id: int = 0) -> None:
    """Remember one request. Cheap: throttled updates + one row per real action."""
    if not valid_vid(vid):
        return
    now = _now()
    _seen[vid] = now
    is_admin = bool(_cookie_vid(request) and path.startswith("/admin")) or user_id in _admin_ids()
    try:
        if now - _last_touch.get(vid, 0) >= TOUCH_INTERVAL:
            _last_touch[vid] = now
            db.visitors.touch_visit(vid, path=path, user_id=user_id)
        if event:
            db.visitors.add_event(vid, event, detail=detail, path=path,
                                  store_id=store_id, file_id=file_id,
                                  user_id=user_id)
        if path:
            db.visitors.add_page_view(vid, path=path, title=title, status=status,
                                      ms=ms, is_admin=1 if is_admin else 0,
                                      user_id=user_id)
    except Exception as exc:                  # analytics must never break the site
        log.debug("visitor tracking skipped: %s", exc)


def _admin_ids() -> list[int]:
    from app import config as cfg
    return list(cfg.ADMIN_IDS or [])


def note(request=None, *, name: str, detail: str = "", store_id: int = 0,
         file_id: int = 0, user_id: int = 0, vid: str = "") -> None:
    """Record a meaningful action (a video started, a store opened…).

    Called from the request handlers *and* the bot, so a single screen shows
    website activity and bot activity together.
    """
    vid = vid or (getattr(getattr(request, "state", None), "bf_vid", "") or "")
    if not valid_vid(vid):
        try:
            db.visitors.add_event("", name, detail=detail, path="",
                                  store_id=store_id, file_id=file_id, user_id=user_id)
        except Exception as exc:
            log.debug("event skipped: %s", exc)
        return
    try:
        db.visitors.add_event(vid, name, detail=detail, store_id=store_id,
                              file_id=file_id, user_id=user_id)
    except Exception as exc:
        log.debug("event skipped: %s", exc)


def end_visit(vid: str) -> None:
    if not valid_vid(vid):
        return
    try:
        db.visitors.close_visit(vid)
    except Exception as exc:
        log.debug("visit close skipped: %s", exc)


# ----------------------------------------------------------------------- reads
def online_now() -> list[dict]:
    """Everyone seen in the last `ONLINE_WINDOW` seconds (admins excluded)."""
    rows = db.visitors.live_visits(seconds=ONLINE_WINDOW)
    out = []
    for row in rows:
        if int(row.get("is_admin") or 0):
            continue
        row = dict(row)
        row["idle"] = max(0, int(_now() - float(row.get("last_seen") or 0)))
        row["device_label"] = row.get("device") or _GAME.get(
            device_kind(row.get("user_agent") or ""), _GAME["unknown"])
        out.append(row)
    return out


def live(minutes: int = 5) -> dict:
    """The numbers for the “এখন কতজন” panel — memory first, database second."""
    window = max(60, min(3600, int(minutes) * 60))
    online = online_now()
    recent = db.visitors.recent_visits(seconds=window, limit=200)
    acting = [row for row in recent if not int(row.get("is_admin") or 0)]
    return {
        "online": len(online),
        "window_minutes": window // 60,
        "in_window": len(acting),
        "people": len({row.get("vid") for row in acting}),
        "views": sum(int(row.get("pageviews") or 0) for row in acting),
        "country": _top(acting, "country"),
        "device": _top(acting, "device"),
        "source": _top(acting, "source"),
        "pages": db.visitors.top_paths(seconds=window, limit=1),
        "rows": online,
    }


def _top(rows: list[dict], field: str) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        value = (row.get(field) or "").strip() or "—"
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return {}
    name = max(counts, key=lambda key: counts[key])
    return {"name": name, "count": counts[name]}


def today() -> dict:
    return db.visitors.summary(day_key())


def overview(days: int = 14) -> dict:
    """Everything the visitors page shows: totals, per-day, top pages/media."""
    days = max(1, min(90, int(days)))
    keys = [(datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(days - 1, -1, -1)]
    per_day = {row["day"]: row for row in db.visitors.per_day(days=days)}
    series = []
    for key in keys:
        row = per_day.get(key) or {}
        series.append({"day": key, "visitors": int(row.get("visitors") or 0),
                       "views": int(row.get("views") or 0),
                       "events": int(row.get("events") or 0)})
    return {
        "series": series,
        "totals": db.visitors.totals(),
        "today": today(),
        "live": live(5),
        "top_pages": db.visitors.top_paths(limit=12),
        "top_media": db.visitors.top_media(limit=12),
        "top_sources": db.visitors.top_sources(limit=8),
        "top_countries": db.visitors.top_countries(limit=8),
        "top_devices": db.visitors.devices(limit=8),
        "events": db.visitors.recent_events(limit=40),
        "visits": db.visitors.recent_visits(seconds=24 * 3600, limit=40),
        "bot_users": db.stats().get("users", 0),
        "linked_users": db.visitors.linked_users(),
    }


def who_is_this(vid: str) -> dict:
    """One visitor's whole story: pages, actions, first/last seen."""
    visit = db.visitors.visit(vid) or {}
    return {"visit": visit,
            "pages": db.visitors.pages_of(vid, limit=200),
            "events": db.visitors.events_of(vid, limit=200)}


def prune(days: int = KEEP_DAYS) -> dict:
    """Retention: keep the analytics useful, not endless."""
    return db.visitors.prune(days=days)


def forget(vid: str) -> int:
    return db.visitors.forget(vid)


def csv_rows(days: int = 30) -> list[list[str]]:
    """Export for the owner's own spreadsheet."""
    rows = [["সময়", "ভিজিটর", "ইউজার", "পেজ", "দেশ", "ডিভাইস", "ব্রাউজার",
             "সোর্স", "রেফারার", "আইপি", "সেকেন্ড", "ভিউ"]]
    for row in db.visitors.recent_visits(seconds=days * 86400, limit=5000):
        rows.append([
            time.strftime("%Y-%m-%d %H:%M", time.localtime(float(row.get("started_at") or 0))),
            str(row.get("vid") or ""),
            str(row.get("user_id") or ""),
            str(row.get("landing") or ""),
            str(row.get("country") or ""),
            str(row.get("device") or ""),
            str(row.get("browser") or ""),
            str(row.get("source") or ""),
            str(row.get("referrer") or "")[:80],
            masked_ip(str(row.get("ip") or "")),
            str(int(float(row.get("last_seen") or 0) - float(row.get("started_at") or 0))),
            str(row.get("pageviews") or 0),
        ])
    return rows


def short_hash(value: str) -> str:
    """Stable, non-reversible id — used for “same visitor came back” checks."""
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()[:12]
