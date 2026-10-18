"""The website: a public store front **and** the full admin panel.

One FastAPI app, two halves:

    Public  (no login)
        /                 store front — every store with an “Open in Telegram” link
        /stores           all stores
        /s/<slug>         one store (+ file list for free stores)
        /search?q=        search free content
        /health           JSON health check for uptime monitors

    Admin   (login only at /admin — fixed password form + HttpOnly cookie)
        /admin                    dashboard: users, views, revenue, alerts
        /admin/broadcast          📢 broadcast studio (create + history + live progress)
        /admin/broadcast/<id>     live progress of one campaign
        /admin/files              every file + 📢 “broadcast this video”
        /admin/stores|users|orders|download|settings

How to run it
    # only the website
    python -m web.dashboard
    # website + bot in one process (what hosting panels like CodeNest expect)
    python run.py

The panel talks to the *same* SQLite file as the bot, so nothing needs syncing.
Broadcasts started here are queued in the database; if the bot is offline the
campaign waits and starts by itself the moment the bot is online again.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import os
import re
import secrets
import sys
import time
from math import ceil
from datetime import datetime, timedelta
from urllib.parse import quote_plus

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, File, Form, Request, UploadFile                    # noqa: E402
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,     # noqa: E402
                               RedirectResponse, Response)

from app import config as cfg                                                 # noqa: E402
from app import runtime                                                       # noqa: E402
from app.logger import log, setup_logging                                     # noqa: E402
from app.services import quickcast as quickcast_service                          # noqa: E402
from app.services import audit as audit_service                                   # noqa: E402
from web import adminui as ui                                                    # noqa: E402
from app.services import portable as portable_service                          # noqa: E402
from app.services import (access, basepath, billing, bot_texts, broadcast,    # noqa: E402
                          channels, credentials, feed as feed_service, forcejoin,
                          media_cache, secrets_guard, settings, visitors)
from app.storage import db                                                    # noqa: E402
from app.utils import esc, fmt_ts, human_size, money                          # noqa: E402
from web import panels                                                        # noqa: E402

STARTED_AT = time.time()
COOKIE_NAME = "sb_session"
# Fixed password explicitly requested by the owner. This is tracked source in a
# public repository, so anyone who can view the repository can read it. Rotate it
# and move it to a private secret store before exposing the repository to others.
ADMIN_LOGIN_PASSWORD = "706609"
# Temporary phone/code login state. String sessions are persisted only after Telegram
# confirms authorization; pending codes are never written to the database or logs.
PENDING_SESSIONS: dict[int, dict] = {}

# --------------------------------------------------------------------- helpers
def _bot_username() -> str:
    """Bot username for deep links — works even while the bot is offline."""
    return runtime.bot_username or db.get_meta("bot_username", "") or ""


def _panel_password() -> str:
    """Return the owner-configured fixed login password."""
    return ADMIN_LOGIN_PASSWORD


def _panel_secret() -> str:
    """Use a random database-persisted key so the short PIN cannot forge cookies."""
    saved = db.get_meta("web_generated_secret", "")
    if not saved:
        saved = secrets.token_urlsafe(32)
        db.set_meta("web_generated_secret", saved)
    return saved


def _session_token() -> str:
    secret = _panel_secret()
    raw = b"bfix-admin-session-v1"
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _secure_equal(left: str, right: str) -> bool:
    return hmac.compare_digest((left or "").encode("utf-8"), (right or "").encode("utf-8"))


def _cookie_ok(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE_NAME, "")
    return bool(cookie) and _secure_equal(cookie, _session_token())


def _authed(request: Request) -> bool:
    """Admin-only pages accept the signed, HttpOnly browser session cookie."""
    return _cookie_ok(request)


_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_MAX_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 15 * 60


def _login_key(request: Request) -> str:
    """Rate-limit the password form by the address seen by the ASGI server."""
    return request.client.host if request.client else "unknown"


def _login_retry_after(key: str, now: float | None = None) -> int:
    now = time.monotonic() if now is None else now
    attempts = [stamp for stamp in _LOGIN_FAILURES.get(key, [])
                if now - stamp < _LOGIN_WINDOW_SECONDS]
    if attempts:
        _LOGIN_FAILURES[key] = attempts
    else:
        _LOGIN_FAILURES.pop(key, None)
    if len(attempts) < _LOGIN_MAX_FAILURES:
        return 0
    return max(1, ceil(_LOGIN_WINDOW_SECONDS - (now - attempts[0])))


def _record_login_failure(key: str, now: float | None = None) -> None:
    _login_retry_after(key, now)
    _LOGIN_FAILURES.setdefault(key, []).append(time.monotonic() if now is None else now)


def deep_link(payload: str) -> str:
    username = _bot_username()
    if not username:
        return ""
    return f"https://t.me/{username}?start={quote_plus(payload)}"


def _start(ts: float | None, pattern: str = "%d %b %H:%M") -> str:
    return fmt_ts(ts, pattern) if ts else "—"


# ---------------------------------------------------------------------- layout
CSS = """
 :root {
   color-scheme: light;
   --bg:#f4f7fb; --surface:#ffffff; --surface-2:#f8fafc; --sidebar:#fff;
   --ink:#172033; --muted:#64748b; --line:#e4eaf2; --line-strong:#d3dce8;
   --accent:#245bd6; --accent-dark:#1747b0; --accent-soft:#edf4ff;
   --teal:#0f766e; --ok:#13795b; --ok-soft:#e7f7f0;
   --warn:#a75d0a; --warn-soft:#fff6e8; --bad:#c2413a; --bad-soft:#fff0ee;
   --r:16px; --shadow:0 8px 24px rgba(23,32,51,.055); --shadow-lg:0 18px 48px rgba(23,32,51,.12);
 }
 * { box-sizing:border-box; }
 html { scroll-behavior:smooth; }
 body { margin:0; color:var(--ink); background:var(--bg); line-height:1.6; font-size:15px;
        font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Bengali","Hind Siliguri",sans-serif; }
 a { color:var(--accent); text-decoration:none; }
 a:hover { color:var(--accent-dark); }
 h1,h2,h3,h4 { color:var(--ink); letter-spacing:-.02em; line-height:1.3; }
 h2 { font-size:clamp(24px,3vw,32px); margin:0 0 14px; }
 h3 { font-size:19px; margin:26px 0 10px; }
 h4 { font-size:16px; margin:22px 0 10px; }
 p { margin:8px 0 14px; }
 code { color:#31415b; background:#f1f5f9; border:1px solid var(--line); padding:2px 6px; border-radius:6px; font-size:.9em; }

 /* Public site */
 header { position:sticky; top:0; z-index:20; min-height:68px; padding:12px clamp(16px,4vw,48px);
          display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap;
          background:rgba(255,255,255,.94); backdrop-filter:blur(16px); border-bottom:1px solid var(--line); }
 header h1 { margin:0; display:flex; align-items:center; gap:10px; font-size:17px; font-weight:800; }
 .logo { width:36px; height:36px; display:inline-grid; place-items:center; flex:0 0 auto; border-radius:11px;
         color:#fff; font-size:18px; background:linear-gradient(145deg,#245bd6,#0f766e); box-shadow:0 5px 12px rgba(36,91,214,.18); }
 nav.top { display:flex; gap:4px; align-items:center; flex-wrap:wrap; }
 nav.top a { color:var(--muted); padding:8px 12px; border-radius:9px; font-size:14px; }
 nav.top a.on, nav.top a:hover { color:var(--ink); background:var(--surface-2); }
 main { width:min(1180px,100%); margin:0 auto; padding:26px 24px 48px; }
 .hero { position:relative; overflow:hidden; padding:clamp(28px,5vw,56px); margin:8px 0 24px;
         border:1px solid #dbe8fa; border-radius:24px; background:linear-gradient(120deg,#fff 4%,#f1f6ff 62%,#edf8f6); }
 .hero:after { content:""; position:absolute; width:250px; height:250px; right:-82px; top:-110px; border-radius:50%;
               border:36px solid rgba(36,91,214,.055); pointer-events:none; }
 .hero h2 { max-width:760px; font-size:clamp(32px,5vw,52px); line-height:1.12; margin:0 0 14px; color:#172b4d; letter-spacing:-.045em; }
 .hero p { max-width:680px; color:#52637c; font-size:17px; }
 .hero .row { margin-top:22px; }
 footer { color:var(--muted); font-size:13px; text-align:center; padding:30px 16px; }
 .howto { display:flex; align-items:center; flex-wrap:wrap; gap:10px; padding:14px 16px; margin:24px 0;
          color:#42536c; border:1px solid var(--line); border-radius:14px; background:var(--surface); }
 .howto b { color:var(--ink); margin-right:8px; }
 .howto span { background:#f5f8fc; border:1px solid var(--line); padding:5px 10px; border-radius:999px; font-size:13px; }
 .stores { display:grid; grid-template-columns:repeat(auto-fill,minmax(min(100%,270px),1fr)); gap:16px; margin-top:16px; }
 .store { min-height:204px; display:flex; flex-direction:column; gap:12px; padding:20px;
          background:var(--surface); border:1px solid var(--line); border-radius:var(--r); box-shadow:var(--shadow);
          transition:transform .18s,border-color .18s,box-shadow .18s; }
 .store:hover { transform:translateY(-2px); border-color:#c3d5f5; box-shadow:0 14px 30px rgba(23,32,51,.09); }
 .store h3 { margin:0; font-size:18px; }
 .store .row:last-child { margin-top:auto; }

 /* Private workspace */
 .shell { min-height:100vh; display:flex; }
 aside.side { width:252px; flex:0 0 252px; position:sticky; top:0; height:100vh; overflow-y:auto;
              padding:20px 14px 18px; background:var(--sidebar); border-right:1px solid var(--line); }
 aside.side .brand { display:flex; align-items:center; gap:11px; margin:0 8px 26px; font-size:16px; font-weight:800; }
 aside.side .brand .logo { width:38px; height:38px; border-radius:12px; }
 aside.side .grp { margin:20px 10px 7px; color:#8793a5; font-size:10px; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }
 aside.side a { display:flex; align-items:center; gap:11px; margin:2px 0; padding:9px 11px; color:#4a5870;
                font-size:14px; border-radius:10px; transition:background .15s,color .15s; }
 aside.side a:hover { color:var(--ink); background:#f5f7fb; }
 aside.side a.on { color:#1747b0; background:var(--accent-soft); font-weight:700; }
 aside.side a .ic { width:22px; text-align:center; font-size:16px; }
 .content { flex:1; min-width:0; }
 .topbar { position:sticky; top:0; z-index:15; min-height:66px; display:flex; align-items:center; justify-content:space-between;
           gap:14px; padding:12px clamp(16px,3vw,32px); background:rgba(255,255,255,.94); backdrop-filter:blur(14px);
           border-bottom:1px solid var(--line); }
 .topbar h1 { font-size:17px; margin:0; font-weight:750; }
 .topbar .row { gap:10px; }
 .content main { width:min(1360px,100%); padding:28px clamp(16px,3vw,36px) 48px; }
 .burger { display:none; padding:8px 10px; color:var(--ink); background:#fff; border:1px solid var(--line-strong); box-shadow:none; }
 .status { display:inline-flex; align-items:center; gap:8px; padding:6px 11px; color:#56647a; background:#fff;
           border:1px solid var(--line); border-radius:999px; font-size:12px; white-space:nowrap; }
 .status i { width:8px; height:8px; border-radius:50%; background:#d79533; }
 .status.on i { background:#15976b; box-shadow:0 0 0 3px #e5f6ee; }
 .status.err i { background:#cf4c44; box-shadow:0 0 0 3px #fff0ee; }
 .page-heading { display:flex; align-items:center; justify-content:space-between; gap:16px; margin:0 0 20px; }
 .page-heading h2 { margin:0; }
 .eyebrow { color:#7b8aa0; font-size:10px; font-weight:800; letter-spacing:.15em; margin:0 0 7px; }

 /* Components and data */
 .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; }
 .card { padding:18px; background:var(--surface); border:1px solid var(--line); border-radius:var(--r); box-shadow:var(--shadow); }
 .card .l { color:var(--muted); font-size:12px; font-weight:600; }
 .card .n { color:#1e3557; font-size:27px; font-weight:800; margin-top:4px; letter-spacing:-.03em; overflow-wrap:anywhere; }
 table { width:100%; margin-top:12px; border-collapse:separate; border-spacing:0; overflow:hidden; font-size:13.5px;
         background:var(--surface); border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow); }
 th,td { text-align:left; vertical-align:top; padding:12px 14px; border-bottom:1px solid #edf0f5; }
 th { color:#718096; background:#f8fafc; font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:.075em; }
 tr:last-child td { border-bottom:0; }
 tbody tr:hover td { background:#fbfdff; }
 .row { display:flex; flex-wrap:wrap; align-items:center; gap:9px; }
 .row form { margin:0; }
 a.btn,button { display:inline-flex; justify-content:center; align-items:center; gap:7px; padding:9px 14px; min-height:40px;
                 border:1px solid transparent; border-radius:10px; color:#fff; background:var(--accent); text-decoration:none;
                 font:inherit; font-size:13px; font-weight:700; line-height:1.3; cursor:pointer;
                 box-shadow:0 3px 8px rgba(36,91,214,.14); transition:background .15s,transform .15s,box-shadow .15s; }
 a.btn:hover,button:hover { color:#fff; background:var(--accent-dark); transform:translateY(-1px); box-shadow:0 6px 14px rgba(36,91,214,.17); }
 a.ok,button.ok { color:#fff; background:var(--ok); box-shadow:none; }
 a.ok:hover,button.ok:hover { background:#0f684d; }
 a.bad,button.bad { color:#fff; background:var(--bad); box-shadow:none; }
 a.bad:hover,button.bad:hover { background:#a9342f; }
 a.grey,button.grey { color:#34445d; background:#fff; border-color:var(--line-strong); box-shadow:none; }
 a.grey:hover,button.grey:hover { color:#173e88; background:#f2f6fd; }
 a.warn,button.warn { color:#77440d; background:var(--warn-soft); border-color:#f4dfbd; box-shadow:none; }
 a.warn:hover,button.warn:hover { color:#683804; background:#ffedcf; }
 input,textarea,select { display:block; width:100%; margin:6px 0 13px; padding:10px 12px; color:var(--ink); background:#fff;
                           border:1px solid var(--line-strong); border-radius:10px; font:inherit; font-size:14px; }
 input:focus,textarea:focus,select:focus { outline:0; border-color:#7da2ec; box-shadow:0 0 0 3px rgba(36,91,214,.12); }
 input[type=checkbox],input[type=radio] { width:auto; margin:0; accent-color:var(--accent); }
 input[type=number] { max-width:260px; }
 textarea { resize:vertical; min-height:76px; }
 label { color:#55647a; font-size:12px; font-weight:700; }
 .chk { display:flex; align-items:center; gap:9px; margin:9px 0; color:#42536c; font-size:13px; }
 .muted { color:var(--muted); font-size:12.5px; }
 .pill { display:inline-flex; align-items:center; max-width:100%; padding:3px 9px; color:#506078; background:#f5f7fb;
         border:1px solid var(--line); border-radius:999px; font-size:11px; font-weight:650; overflow-wrap:anywhere; }
 .flash,.warnbox { padding:12px 15px; margin:0 0 16px; border-radius:12px; font-weight:600; font-size:14px; }
 .flash { color:#146247; background:var(--ok-soft); border:1px solid #c6ecd9; }
 .warnbox { color:#854e10; background:var(--warn-soft); border:1px solid #f1dfc1; }
 .bar { height:10px; overflow:hidden; background:#edf1f6; border:1px solid var(--line); border-radius:999px; }
 .bar>i { display:block; height:100%; background:linear-gradient(90deg,#245bd6,#12a27c); transition:width .25s; }
 .logline { padding:5px 2px; color:#55647a; border-bottom:1px solid #edf0f5; white-space:pre-wrap; word-break:break-word;
            font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
 .logline:first-child { color:var(--ink); }
 .badge { display:inline-block; padding:2px 7px; border-radius:999px; color:#46566c; background:#f2f5f9; font-size:11px; }
 .kv { display:grid; grid-template-columns:auto 1fr; gap:7px 16px; font-size:13px; }
 .kv div:nth-child(odd) { color:var(--muted); }
 .steps { padding-left:22px; }
 .steps li { margin:9px 0; }
 details { margin:10px 0; padding:14px 16px; background:#fff; border:1px solid var(--line); border-radius:12px; }
 details summary { cursor:pointer; font-weight:700; }
 details p { color:var(--muted); margin:9px 0 2px; }
 .settings-section { padding:0; border:0; background:transparent; }
 .settings-section summary { display:flex; align-items:center; justify-content:space-between; gap:12px;
                             padding:14px 16px; background:#fff; border:1px solid var(--line); border-radius:12px;
                             box-shadow:var(--shadow); }
 .settings-section[open] summary { border-radius:12px 12px 0 0; }
 .settings-section .settings-inner { margin-top:0; border-radius:0 0 12px 12px; }

 /* Setup and login */
 .wiz { width:min(640px,100%); margin:0 auto; padding:34px 18px 60px; }
 .wiz .head { text-align:center; margin-bottom:22px; }
 .wiz .head .logo { width:58px; height:58px; margin:0 auto 14px; border-radius:18px; font-size:26px; }
 .wiz .head h2 { margin:0 0 6px; font-size:28px; }
 .wiz .head p { color:var(--muted); margin:0; }
 .step { display:flex; gap:14px; margin:16px 0; }
 .step .num { display:grid; place-items:center; flex:0 0 34px; height:34px; margin-top:2px; border-radius:50%;
              color:#fff; font-weight:800; background:linear-gradient(145deg,#245bd6,#0f766e); }
 .step .box { flex:1; min-width:0; }
 .step h4 { margin:2px 0; font-size:15px; }
 .step .help { color:var(--muted); font-size:13px; margin:0 0 4px; }
 .fe { min-height:0; margin:0; color:#b33e38; font-size:12px; }
 input.bad-in { border-color:var(--bad); }
 .big { width:100%; min-height:48px; margin-top:12px; font-size:15px; }
 .prog { display:none; padding:28px 10px; text-align:center; }
 .spin { width:44px; height:44px; margin:0 auto 16px; border:4px solid #e8edf5; border-top-color:var(--accent); border-radius:50%; animation:sp .9s linear infinite; }
 @keyframes sp { to { transform:rotate(360deg); } }
 .tick { display:grid; place-items:center; width:58px; height:58px; margin:0 auto 14px; border-radius:50%;
          color:var(--ok); background:var(--ok-soft); font-size:30px; }

 @media (max-width:900px) {
   aside.side { position:fixed; left:0; top:0; z-index:40; width:266px; height:100vh; transform:translateX(-102%);
                box-shadow:var(--shadow-lg); transition:transform .2s ease; }
   body.nav aside.side { transform:translateX(0); }
   .burger { display:inline-flex; }
   .topbar { padding:11px 14px; }
   .content main { padding:20px 14px 40px; }
   .hero { padding:30px 22px; }
 }
 @media (max-width:620px) {
   header { gap:8px; padding:10px 14px; }
   header h1 { font-size:15px; }
   header .btn { margin-left:auto; }
   nav.top { width:100%; order:3; overflow-x:auto; flex-wrap:nowrap; }
   nav.top a { white-space:nowrap; }
   main { padding:18px 14px 34px; }
   .topbar h1 { font-size:15px; }
   .status { font-size:10.5px; padding:5px 8px; }
   .grid { grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
   .grid .card { padding:14px; }
   .card .n { font-size:22px; }
   .page-heading { align-items:flex-start; }
   table { display:block; overflow-x:auto; white-space:nowrap; }
   th,td { padding:10px 11px; }
   .row { align-items:stretch; }
   .row > .btn,.row > button { flex:0 0 auto; }
 }
"""

#: Sidebar map. One row per thing the owner actually does — grouped so the eye can
#: stop at a group instead of scanning a list of twenty identical rows. Every entry
#: is a real route (a dead link in the panel is worse than no link at all).
ADMIN_NAV = (
    ("⚠ জরুরি", (("/admin/emergency", "⚠", "Emergency center"),)),
    ("📦 কনটেন্ট", (("/admin", "▣", "Overview"), ("/admin/stores", "▦", "Stores"),
                     ("/admin/files", "▤", "Videos & files"),
                     ("/admin/channels", "◉", "Channels & cache"),
                     ("/admin/reel", "🎞", "Home page reel"))),
    ("📣 অডিয়েন্স", (("/admin/broadcast", "↗", "Broadcasts"), ("/admin/users", "♙", "Users"),
                      ("/admin/orders", "▧", "Orders"), ("/admin/links", "⌁", "Share links"),
                      ("/admin/digest", "◫", "Weekly digest"))),
    ("💾 ডেটা", (("/admin/backup", "🗄", "Backup & move host"),
                   ("/admin/download", "⇩", "Exports"))),
    ("📊 হিসাব", (("/admin/analytics", "▥", "Analytics"), ("/admin/visitors", "👣", "Visitors (live)"),
                   ("/admin/self-test", "✓", "System health"),
                   ("/admin/logs", "≡", "Activity logs"),
                   ("/admin/audit", "🧾", "Admin audit"))),
    ("⚙ সেটিংস", (("/setup", "⚙", "Bot connection"), ("/admin/bots", "🤖", "Bots"),
                    ("/admin/admins", "👥", "Admins"), ("/admin/session", "◌", "Telegram session"),
                    ("/admin/settings", "☰", "Preferences"), ("/admin/texts", "✎", "Bot messages"),
                    ("/admin/security", "◇", "Security"))),
)

#: Shortcuts on the overview page — the same rows, one tap away, no menu hunting.
ADMIN_QUICK = (
    ("＋", "Create store", "/admin/stores/new", "নতুন স্টোর ও ফাইল গোছানোর শুরু"),
    ("▤", "Videos &amp; files", "/admin/files", "নতুন মিডিয়া, ক্যাশ, এক-ক্লিক ব্রডকাস্ট"),
    ("↗", "New broadcast", "/admin/broadcast", "সবাইকে /br স্টুডিও থেকে"),
    ("🎞", "Home page reel", "/admin/reel", "অটো-প্লে, ওয়াল ও পিন করা মিডিয়া"),
    ("◉", "Channels", "/admin/channels", "প্রতি স্টোরের আলাদা চ্যানেল"),
    ("📲", "Telegram session", "/admin/session", "বট ও ইউজারবট সংযোগ"),
    ("🧾", "Admin audit", "/admin/audit", "কে কী করল — সম্পূর্ণ হিসাব"),
    ("👣", "Visitors (live)", "/admin/visitors", "এখন কে অনলাইনে, কোথা থেকে, কী দেখছে"),
)

# Shared black/yellow palette for public pages, setup and the admin workspace.
PUBLIC_THEME_CSS = """
 :root { color-scheme:dark; --bg:#0b0b0d; --surface:#141417; --surface-2:#1a1a1e; --ink:#f3efe6; --muted:#9d988c;
   --line:#232328; --line-strong:#33333a; --accent:#c9a45c; --accent-dark:#dcb978; --accent-soft:#1d1810;
   --r:20px; --shadow:none; --shadow-lg:0 18px 44px rgba(0,0,0,.5); }
 body { background:var(--bg); font-family:"Hind Siliguri","Bricolage Grotesque",system-ui,"Noto Sans Bengali",sans-serif; }
 h1,h2,h3,h4 { color:var(--ink); font-family:"Bricolage Grotesque","Hind Siliguri",sans-serif; font-weight:600; letter-spacing:-.015em; }
 a { color:var(--accent); } a:hover { color:var(--accent-dark); }
 code { color:var(--accent); background:var(--surface-2); border-color:var(--line); }
 header { background:rgba(11,11,13,.9); border-bottom:1px solid var(--line); }
 header h1 { font-weight:700; }
 .logo { background:transparent; color:var(--accent); border:1px solid var(--accent); box-shadow:none; border-radius:50%; }
 nav.top a { color:var(--muted); border-radius:999px; padding:7px 16px; }
 nav.top a.on, nav.top a:hover { color:var(--accent); background:var(--surface-2); }
 .hero { border:0; border-radius:0; background:transparent; padding:clamp(36px,7vw,88px) 0 clamp(28px,5vw,56px); margin:0; border-bottom:1px solid var(--line); }
 .hero:after { display:none; }
 .hero h2 { color:var(--ink); } .hero h2 span { color:var(--accent); } .hero p { color:var(--muted); }
 .eyebrow { color:var(--accent); letter-spacing:.22em; }
 a.btn,button { color:#14110a; background:var(--accent); border-radius:999px; box-shadow:none; letter-spacing:.01em; }
 a.btn:hover,button:hover { color:#14110a; background:var(--accent-dark); box-shadow:none; }
 a.grey,button.grey { color:var(--ink); background:transparent; border-color:var(--line-strong); }
 a.grey:hover,button.grey:hover { color:var(--accent); background:transparent; border-color:var(--accent); }
 .card, .store, .howto, details { background:var(--surface); border:1px solid var(--line); border-radius:var(--r); box-shadow:none; }
 .card .n { color:var(--accent); font-family:"Bricolage Grotesque",sans-serif; }
 .store:hover { border-color:var(--accent); box-shadow:none; transform:translateY(-2px); }
 .howto { color:var(--muted); } .howto b { color:var(--ink); } .howto span { background:transparent; border-color:var(--line-strong); color:var(--ink); }
 .pill, .badge { color:var(--accent); background:transparent; border-color:var(--line-strong); }
 table { background:var(--surface); border-color:var(--line); } th { color:var(--accent); background:var(--surface-2); }
 th,td { border-bottom-color:var(--line); } tbody tr:hover td { background:var(--surface-2); }
 input,textarea,select { color:var(--ink); background:var(--surface-2); border-color:var(--line-strong); border-radius:14px; }
 input:focus,textarea:focus,select:focus { border-color:var(--accent); box-shadow:none; }
 label { color:#c9c4b8; } footer { color:var(--muted); border-top:1px solid var(--line); }
 .warnbox { color:var(--accent-dark); background:var(--accent-soft); border-color:#3a3017; }

 .landing { display:grid; gap:28px; align-items:center; margin-bottom:8px; }
 @media (min-width:900px) { .landing.has-feed { grid-template-columns:minmax(0,1.05fr) minmax(0,.95fr); } }
 /* ---------------------------------------------------------- auto-play reel */
 .reel-wrap { position:relative; width:min(100%,400px); margin:0 auto; }
 .reel { height:min(82vh,720px); overflow-y:auto; overscroll-behavior-y:contain; scroll-snap-type:y mandatory;
         border-radius:30px; border:1px solid var(--line-strong); background:#000; scrollbar-width:none;
         -webkit-overflow-scrolling:touch; }
 .reel::-webkit-scrollbar { display:none; }
 .clip { position:relative; display:block; height:100%; scroll-snap-align:start; scroll-snap-stop:always;
         color:#fff; background:#0a0a0a; overflow:hidden; }
 .clip .media { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; background:#0a0a0a; }
 .clip[data-media-kind="photo"] .media { animation:kenburns 18s ease-in-out infinite alternate; }
 @keyframes kenburns { from { transform:scale(1); } to { transform:scale(1.08); } }
 .clip .shade { position:absolute; inset:0; background:linear-gradient(180deg,rgba(0,0,0,.28) 0%,
                rgba(0,0,0,0) 30%, rgba(0,0,0,.15) 55%, rgba(0,0,0,.88) 100%); pointer-events:none; }
 .clip .cap { position:absolute; left:0; right:0; bottom:0; padding:64px 18px 22px; }
 .clip .cap b { display:block; font-size:16px; line-height:1.35; color:#fff; }
 .clip .cap .sub { display:block; margin-top:3px; color:rgba(255,255,255,.72); font-size:12px; }
 .clip .cap .cta { display:flex; gap:8px; align-items:center; margin-top:12px; }
 .clip .cap .cta a { padding:8px 15px; border-radius:999px; background:var(--accent); color:#14110a;
                     font-size:12px; font-weight:700; }
 .clip .cap .cta a.ghost { background:rgba(255,255,255,.14); color:#fff; }
 .reel-top { position:absolute; top:12px; left:12px; right:12px; z-index:4; display:flex; gap:8px;
             align-items:center; justify-content:space-between; pointer-events:none; }
 .reel-top .count, .reel-top .badge { pointer-events:auto; padding:5px 11px; border-radius:999px;
             font-size:11.5px; font-weight:700; color:#fff; background:rgba(0,0,0,.55);
             border:1px solid rgba(255,255,255,.22); }
 button.snd, button.pp { position:absolute; z-index:5; width:42px; min-height:42px; padding:0;
             border-radius:50%; color:#fff; background:rgba(0,0,0,.55);
             border:1px solid rgba(255,255,255,.26); font-size:16px; }
 button.snd { top:14px; right:14px; }
 button.snd:hover, button.pp:hover { background:rgba(0,0,0,.78); color:#fff; }
 .reel-progress { position:absolute; left:14px; right:14px; bottom:8px; height:3px; z-index:5;
                  border-radius:999px; background:rgba(255,255,255,.22); overflow:hidden; }
 .reel-progress i { display:block; height:100%; width:0; background:var(--accent); transition:width .2s linear; }
 .reel-dots { display:flex; justify-content:center; gap:7px; margin:12px 0 0; }
 .reel-dots button { width:9px; min-height:9px; padding:0; border-radius:50%;
                     background:var(--line-strong); border:0; box-shadow:none; }
 .reel-dots button.on { background:var(--accent); transform:scale(1.25); }
 .reel-note { text-align:center; margin:10px 0 0; color:var(--muted); font-size:12px; }
 .clip .fail { position:absolute; inset:0; z-index:3; opacity:0; pointer-events:none;
               display:flex; flex-direction:column; align-items:center; justify-content:center; gap:8px;
               text-align:center; padding:22px; background:linear-gradient(180deg,rgba(0,0,0,.25),rgba(0,0,0,.88));
               color:#fff; font-size:13px; transition:opacity .3s ease; }
 .clip.failed .fail { opacity:1; pointer-events:auto; }
 .clip.failed .loading { display:none; }
 /* ------------------------------------ “several clips at once” autoplay wall */
 .awall { display:grid; grid-template-columns:repeat(auto-fit,minmax(148px,1fr)); gap:10px;
          margin:14px 0 0; }
 .awall .cell { position:relative; overflow:hidden; border-radius:16px; background:#0b0b0e;
                border:1px solid var(--line); aspect-ratio:9/16; cursor:pointer; }
 .awall .cell video, .awall .cell img { position:absolute; inset:0; width:100%; height:100%;
                object-fit:cover; opacity:0; transition:opacity .45s ease; background:#0b0b0e; }
 .awall .cell.ready video, .awall .cell.ready img { opacity:1; }
 .awall .cell .tag2 { position:absolute; left:8px; right:8px; bottom:8px; z-index:2; font-size:11px;
                color:#fff; text-shadow:0 1px 8px rgba(0,0,0,.9); overflow:hidden;
                text-overflow:ellipsis; white-space:nowrap; }
 .awall .cell .badge2 { position:absolute; left:8px; top:8px; z-index:2; font-size:10px; }
 .awall .fail { position:absolute; inset:0; z-index:3; opacity:0; display:flex; flex-direction:column;
                align-items:center; justify-content:center; gap:6px; padding:10px; text-align:center;
                background:linear-gradient(180deg,rgba(0,0,0,.3),rgba(0,0,0,.88)); color:#fff; font-size:12px; }
 .awall .cell.failed .fail { opacity:1; }
 .awall-note { text-align:center; color:var(--muted); font-size:12px; margin:10px 0 0; }
 .clip .loading { position:absolute; inset:0; display:grid; place-items:center; z-index:2;
                  color:rgba(255,255,255,.75); font-size:26px; }

 /* ------------------------------------------------------------ media gallery */
 .wall { display:grid; grid-template-columns:repeat(auto-fill,minmax(min(100%,168px),1fr)); gap:12px;
         margin-top:14px; }
 .tile { position:relative; display:block; aspect-ratio:9/16; overflow:hidden; border-radius:16px;
         background:#15151a center/cover no-repeat; border:1px solid var(--line); }
 .tile img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; opacity:0;
             transition:opacity .35s; }
 .tile img.ready { opacity:1; }
 .tile .tag { position:absolute; left:8px; bottom:8px; right:8px; z-index:2; font-size:11.5px; color:#fff;
              text-shadow:0 1px 6px rgba(0,0,0,.85); overflow:hidden; display:-webkit-box;
              -webkit-line-clamp:2; -webkit-box-orient:vertical; }
 .tile .play { position:absolute; top:8px; right:8px; z-index:2; width:30px; height:30px; border-radius:50%;
               display:grid; place-items:center; background:rgba(0,0,0,.55); color:#fff; font-size:12px; }
 .tile:hover { border-color:var(--accent); }

 /* ------------------------------------------------------------- store covers */
 .store-cover { position:relative; margin:-20px -20px 12px; height:132px; border-radius:19px 19px 0 0;
                background:#1a1a1e center/cover no-repeat; }
 .store-cover .badge { position:absolute; left:12px; top:12px; }
 .watch-hero { border-radius:22px; overflow:hidden; background:#000; border:1px solid var(--line); }
 .watch-hero video, .watch-hero img { width:100%; max-height:70vh; object-fit:contain; background:#000; display:block; }
 ::selection { background:var(--accent); color:#14110a; }
"""

#: The admin look lives in `web/adminui.py` (design system + components).



#: Bumped by hand when the panel changes so the owner can see what is deployed.
PANEL_VERSION = "3.4"

#: The report of the last database restore, so /admin/backup can show what it did.
_LAST_RESTORE: dict = {}

#: One line under the title of every admin page — what the page is *for*. Empty
#: values simply print nothing, so a new page can be added before its blurb.
PAGE_BLURBS: dict[str, str] = {
    "/admin/stores": "স্টোর তৈরি, এডিট, প্রিমিয়াম/ফ্রি আর প্রতিটা স্টোরের চ্যানেল।",
    "/admin/files": "সব ভিডিও/ছবি — ক্যাশ করা, এক-ক্লিকে ব্রডকাস্ট, ডুপ্লিকেট পরিষ্কার।",
    "/admin/users": "কে কতটা অ্যাক্টিভ, কে কী কিনেছে — সার্চ, ব্যান, গ্র্যান্ট, মেসেজ।",
    "/admin/orders": "পেমেন্ট অর্ডার — কে কী পাঠিয়েছে, অনুমোদন, বাতিল।",
    "/admin/links": "লিমিটেড লিংক — কতবার, কতজন, কে নিয়েছে তার হিসাব।",
    "/admin/analytics": "ফানেল, ডেড ফাইল, কনভার্শন — সংখ্যায় কনটেন্টের অবস্থা।",
    "/admin/visitors": ("লাইভ ভিজিটর — এখন কে সাইটে, কোথা থেকে এসেছে, কোন পেজ দেখছে, "
                        "কোন ভিডিও চালাচ্ছে, কে বট থেকে আর কে লিংক থেকে।"),
    "/admin/bots": ("একই ডেটাবেজে একাধিক বট — একটা বন্ধ হলে অন্যটা চলবে; BotFather-এর "
                    "টোকেন পেস্ট বা ফরওয়ার্ড করলেই নতুন বট চালু।"),
    "/admin/admins": ("কে এই প্যানেল ও বট চালাতে পারবে — আইডি দিয়ে যোগ/সরান, যোগ হলে "
                      "স্বাগতম মেসেজও যায়।"),
    "/admin/settings": "বট, সাইট, পেমেন্ট, ব্রডকাস্ট — সব সেটিং এক জায়গায়, রিস্টার্ট ছাড়াই।",
    "/admin/texts": "বটের প্রতিটি মেসেজের ভাষা নিজে লিখুন (বাংলা/English)।",
    "/admin/channels": "যেসব চ্যানেল বট ব্যবহার করে — ফোর্স-জয়েন গেট আর প্রতি স্টোরের ক্যাশ।",
    "/admin/session": "Telegram সেশন — বট ও ইউজারবট সংযোগ, অবস্থা ও পুনরায় লগইন।",
    "/admin/self-test": "নিজে নিজে পরীক্ষা — কী ঠিক, কী নষ্ট, আর তার সমাধান।",
    "/admin/logs": "সার্ভারের লাইভ লগ — সমস্যা হলে প্রথমে এখানেই দেখুন।",
    "/admin/emergency": "জরুরি সুইচ — ক্যাশ আবার কপি, মেইনটেন্যান্স, ব্যাকআপ।",
    "/admin/security": "পাসওয়ার্ড, সেশন, সিক্রেট ও অ্যাক্সেস নিয়ন্ত্রণ।",
    "/admin/digest": "সাপ্তাহিক নতুন-ভিডিও ডাইজেস্ট পাঠানোর নিয়ন্ত্রণ।",
    "/admin/reel": "হোম পেজের অটো-প্লে রিল — কোনটা আগে, কতগুলো, সাউন্ড।",
    "/admin/download": "SQLite/JSON/CSV — ডেটা ব্যাকআপ ও এক্সপোর্ট।",
    "/admin/backup": "পুরো ইনস্টল এক ফাইলে নামান, আর নতুন হোস্টে ফিরিয়ে আনুন "
                     "(ইউজার আইডি, অ্যাক্সেস-হ্যাশ, সেটিংস — সবসহ)।",
    "/setup": "BotFather টোকেন, API আইডি/হ্যাশ ও অ্যাডমিন আইডি — একবারই লাগে।",
}


def status_tag(value: str) -> str:
    """Status pill for a campaign/order row (labels live in `web.adminui`)."""
    return ui.tag(str(value or ""))


def broadcast_today(stats: dict) -> int:
    """How many messages went out today — the “is it alive?” number on the overview."""
    try:
        events = db.events_by_day(1)
        return int(events[0].get("delivered", 0)) if events else 0
    except Exception:
        return 0


def _nav_trail(active: str, title: str) -> tuple[str, str]:
    """Where the current page sits in the sidebar (group, page label)."""
    for group, items in ADMIN_NAV:
        for href, _icon, label in items:
            if href == active or (href != "/admin" and active.startswith(href + "/")):
                clean = group.split(" ", 1)[-1]
                return clean, label
    return "পেজ", title


def _status_pill() -> str:
    from app.services import credentials
    st = credentials.state
    if runtime.bot_online():
        user = _bot_username()
        return f'<a class="status on" href="setup"><i></i>বট অনলাইন{(" · @" + html.escape(user)) if user else ""}</a>'
    if st["state"] == "error":
        return '<a class="status err" href="setup"><i></i>বটে সমস্যা — ঠিক করুন</a>'
    if st["state"] == "waiting":
        return '<a class="status" href="setup"><i></i>বট সেট করা হয়নি</a>'
    return '<a class="status" href="setup"><i></i>বট সংযোগ হচ্ছে…</a>'


_FEED_JS = """<script>(function(){
  var reel = document.querySelector('.reel');
  if (!reel) return;
  var slides = [].slice.call(reel.querySelectorAll('.clip'));
  var bar = document.querySelector('.reel-progress > i');
  var counter = document.querySelector('.reel-top .count');
  var dots = [].slice.call(document.querySelectorAll('.reel-dots button'));
  var snd = document.getElementById('snd');
  var pp = document.getElementById('pp');
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var muted = true, current = 0, advance = null;

  function mediaOf(slide) { return slide.querySelector('video, img.media'); }
  function load(slide) {
    var m = mediaOf(slide);
    if (!m || m.dataset.loaded) return;
    if (m.dataset.src) m.setAttribute('src', m.dataset.src);
    m.dataset.loaded = '1';
    var ld = slide.querySelector('.loading'); if (ld) ld.remove();
  }
  function neighbours(index) { [index - 1, index, index + 1].forEach(function(i){
      if (slides[i]) load(slides[i]); }); }
  function start(slide, index) {
    var v = slide.querySelector('video');
    load(slide);
    if (!v || reduce) return;
    v.muted = muted;
    var p = window.bfixfPlay(v);
    if (p && p.catch) p.catch(function(){});
    slide.classList.add('playing');
    if (v.dataset.counted !== '1') {
      v.dataset.counted = '1';
      try { fetch('reel/play/' + encodeURIComponent(slide.dataset.key),
                  {keepalive:true, cache:'no-store'}); } catch (e) {}
    }
  }
  function stop(slide) {
    var v = slide.querySelector('video');
    if (v) v.pause();
    slide.classList.remove('playing');
  }
  function showFail(slide, reason) {
    slide.classList.add('failed');
    var note = slide.querySelector('.fail .why');
    if (note) note.textContent = '😕 ' + (reason || 'এই মুহূর্তে চলছে না');
    var v = slide.querySelector('video');
    if (v) { v.pause(); v.style.display = 'none'; }
    clearTimeout(advance);
  }
  function schedule(slide) {
    clearTimeout(advance);
    if (reduce) return;
    if (slide && slide.classList.contains('failed')) return;   // the visitor decides
    var v = slide.querySelector('video');
    if (v) return;  // only an ended video advances; never skip a stalled/blocked clip
    advance = setTimeout(function(){ go(current + 1); }, 5000);
  }
  function go(index) {
    if (!slides.length) return;
    current = (index + slides.length) % slides.length;
    slides[current].scrollIntoView({behavior: reduce ? 'auto' : 'smooth', block: 'start'});
  }
  function focusSlide(index) {
    current = index;
    slides.forEach(function(s, i){ if (i === index) start(s, i); else stop(s); });
    neighbours(index);
    if (counter) counter.textContent = (index + 1) + ' / ' + slides.length;
    dots.forEach(function(d, i){ d.classList.toggle('on', i === index); });
    schedule(slides[index]);
  }

  var io = new IntersectionObserver(function(entries){
    entries.forEach(function(entry){
      var index = slides.indexOf(entry.target);
      if (index < 0) return;
      if (entry.isIntersecting && entry.intersectionRatio > 0.55) focusSlide(index);
      else if (entry.intersectionRatio < 0.2) stop(entry.target);
    });
  }, {root: reel, threshold: [0.2, 0.55, 0.9]});
  slides.forEach(function(s){ io.observe(s); });

  reel.addEventListener('scroll', function(){
    var max = reel.scrollHeight - reel.clientHeight;
    if (bar) bar.style.width = (max > 0 ? Math.min(100, Math.max(4, reel.scrollTop / max * 100)) : 4) + '%';
  }, {passive: true});

  slides.forEach(function(slide, index){
    var v = slide.querySelector('video');
    if (v) {
      v.addEventListener('click', function(ev){
        ev.preventDefault(); ev.stopPropagation();
        if (v.paused) { window.bfixfPlay(v); slide.classList.add('playing'); }
        else { v.pause(); slide.classList.remove('playing'); }
      });
      v.addEventListener('ended', function(){ go(index + 1); });
      v.addEventListener('error', function(){
        // Never “skip because it could not play”: first ask the site to fetch the
        // file from Telegram right now, then play the very same slide. Only when
        // that also fails does the card say why — and then the visitor chooses.
        if (slide.dataset.asked === '1') { showFail(slide, 'ফাইলটা আনা যাচ্ছে না'); return; }
        slide.dataset.asked = '1';
        clearTimeout(advance);
        var note = slide.querySelector('.fail .why');
        if (note) note.textContent = '📥 টেলিগ্রাম থেকে আনছি…';
        v.style.display = '';
        window.bfixfPrepare(slide.dataset.stream || v.dataset.src || v.getAttribute('src'), function(j){
          if (j && j.ok) {
            slide.dataset.asked = '1';
            v.style.display = '';
            v.dataset.loaded = '';
            v.setAttribute('src', window.bfixfFresh(slide.dataset.stream || v.getAttribute('src')));
            v.load();
            var p2 = window.bfixfPlay(v); if (p2 && p2.catch) p2.catch(function(){});
            slide.classList.remove('failed');
            return;
          }
          showFail(slide, (j && j.reason) || 'এই মুহূর্তে চালানো যাচ্ছে না');
        });
      });
      var retry = slide.querySelector('.fail .retry');
      if (retry) retry.addEventListener('click', function(ev){
        ev.preventDefault(); ev.stopPropagation();
        slide.dataset.asked = '';
        slide.classList.remove('failed');
        if (v) { v.dataset.loaded = ''; v.style.display = ''; }
        if (typeof focusSlide === 'function') focusSlide(slides.indexOf(slide));
      });
      var next = slide.querySelector('.fail .next');
      if (next) next.addEventListener('click', function(ev){
        ev.preventDefault(); ev.stopPropagation();
        go(index + 1);
      });
    }
    slide.addEventListener('keydown', function(ev){
      if (ev.key === 'ArrowDown') { ev.preventDefault(); go(current + 1); }
      if (ev.key === 'ArrowUp') { ev.preventDefault(); go(current - 1); }
      if (ev.key === ' ') { ev.preventDefault(); var pv = slide.querySelector('video');
                            if (pv) { pv.paused ? window.bfixfPlay(pv) : pv.pause(); } }
    });
    slide.addEventListener('touchstart', function(ev){
      slide.dataset.y = ev.touches[0].clientY;
    }, {passive: true});
    slide.addEventListener('touchend', function(ev){
      var dy = (slide.dataset.y || 0) - ev.changedTouches[0].clientY;
      if (Math.abs(dy) > 60) go(current + (dy > 0 ? 1 : -1));
    }, {passive: true});
  });

  if (snd) snd.addEventListener('click', function(ev){
    ev.preventDefault(); ev.stopPropagation();
    muted = !muted;
    slides.forEach(function(s){ var v = s.querySelector('video'); if (v) v.muted = muted; });
    snd.textContent = muted ? '🔇' : '🔊';
  });
  if (pp) pp.addEventListener('click', function(ev){
    ev.preventDefault(); ev.stopPropagation();
    var v = slides[current] && slides[current].querySelector('video');
    if (!v) return;
    if (v.paused) { window.bfixfPlay(v); pp.textContent = '⏸'; }
    else { v.pause(); pp.textContent = '▶'; }
  });
  document.addEventListener('visibilitychange', function(){
    if (document.hidden) { slides.forEach(stop); clearTimeout(advance); }
    else if (slides[current]) focusSlide(current);
  });

  focusSlide(0);
})();</script>"""


#: Shared player helper. When a clip cannot start (mirror still being built, the
#: owner's session offline, a mirror channel that moved…), the page asks the
#: *server* to fetch it right then (`/media/<id>/prepare` → Telegram → disk) and
#: plays it as soon as the bytes are here. Only if that also fails does the card
#: explain why and offer “আবার চালান” / “পরেরটা” — never a silent skip.
_PLAYER_HELPER_JS = """<script>window.bfixfPrepare = function(url, cb){
  var base = (url || '').split('?')[0];
  if (!base) { cb({ok:false, reason:''}); return; }
  fetch(base + '/prepare', {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){ cb(j || {ok:false, reason:''}); })
    .catch(function(){ cb({ok:false, reason:'সার্ভারে যোগাযোগ করা যাচ্ছে না'}); });
};
window.bfixfPlay = function(video){
  if (!video) return Promise.resolve(false);
  video.playsInline = true;
  var promise;
  try { promise = video.play(); } catch (e) { promise = Promise.reject(e); }
  return Promise.resolve(promise).then(function(){
    var old = video.parentElement && video.parentElement.querySelector('.play-prompt');
    if (old) old.remove();
    return true;
  }).catch(function(error){
    if (error && error.name === 'AbortError') return false;
    var parent = video.parentElement;
    if (!parent) return false;
    var prompt = parent.querySelector('.play-prompt');
    if (!prompt) {
      prompt = document.createElement('button'); prompt.type = 'button';
      prompt.className = 'play-prompt'; parent.appendChild(prompt);
      prompt.addEventListener('click', function(event){
        event.preventDefault(); event.stopPropagation(); video.muted = true;
        window.bfixfPlay(video);
      });
    }
    prompt.textContent = error && error.name === 'NotAllowedError' ? '▶ ট্যাপ করে চালান' : '▶ আবার প্লে করুন';
    return false;
  });
};
window.bfixfFresh = function(url){ return (url || '').split('?')[0] + '?r=' + Date.now(); };
</script>"""

def _reel_slide(item: dict, bots: str, first: bool) -> str:
    """One slide of the auto-playing reel — video or photo, with a poster frame."""
    kind = item.get("media_kind") or "video"
    stream = item.get("stream") or ""
    thumb = item.get("thumb") or ""
    name = esc(item.get("name") or ("নতুন মিডিয়া" if kind == "video" else "নতুন ছবি"))
    store = esc(item.get("store_name") or "")
    key = esc(item.get("key") or "")
    link = item.get("bot_link") or ""
    source = f'src="{esc(stream)}"' if first else f'data-src="{esc(stream)}"'
    if kind == "photo":
        media = (f'<img class="media" alt="{name}" loading="{"eager" if first else "lazy"}" '
                 f'decoding="async" poster="{esc(thumb)}" {source}>')
    else:
        media = (f'<video class="media" muted loop playsinline preload="{"metadata" if first else "none"}" '
                 f'{"autoplay" if first else ""} poster="{esc(thumb)}" {source}></video>')
    cta = (f'<div class="cta"><a href="{esc(link)}" target="_blank" rel="noopener">'
           f'▶ বটে পুরোটা দেখুন</a></div>') if link else ""
    sub = f'<span class="sub">{store}</span>' if store else ""
    # A slide that cannot start right away is *fetched*, not skipped: the card
    # explains what is happening and the visitor can retry or move on by hand.
    fail = (f'<div class="fail"><span class="why">😕 এটা এই মুহূর্তে লোড হলো না</span>'
            + (f'<a class="btn" href="{esc(link)}" target="_blank" rel="noopener">'
               f'🤖 বটে দেখুন</a>' if link else "")
            + '<div class="row"><button type="button" class="btn retry">▶ আবার চালান</button>'
              '<button type="button" class="btn grey next">⏭ পরেরটা</button></div></div>')
    return (f'<article class="clip" tabindex="0" data-key="{key}" data-media-kind="{kind}" '
            f'data-stream="{esc(stream)}">'
            f'<div class="loading">⏳</div>{media}'
            f'<div class="shade"></div>'
            f'<div class="cap"><b>{name}</b>{sub}{cta}</div>{fail}</article>')


def _feed_section(bots: str) -> str:
    """The auto-playing reel of the newest (or hand-picked) free media.

    Always renders when there is anything at all to show — a missing thumbnail or a
    missing bot-side copy no longer hides the whole section.
    """
    rows = feed_service.latest(bots)
    if not rows:
        return ""
    clips = "".join(_reel_slide(item, bots, index == 0) for index, item in enumerate(rows))
    dots = "".join(f'<button type="button" class="{"on" if i == 0 else ""}" '
                   f'aria-label="slide {i + 1}"></button>' for i in range(len(rows)))
    return (
        '<div class="reel-wrap">'
        '<div class="reel-top">'
        f'<span class="count">1 / {len(rows)}</span>'
        '<span class="badge">🔊 ট্যাপ করে সাউন্ড</span></div>'
        '<button class="snd" id="snd" type="button" aria-label="Sound">🔇</button>'
        f'<div class="reel" tabindex="0">{clips}'
        '<div class="reel-progress"><i></i></div></div>'
        f'<div class="reel-dots">{dots}</div>'
        '<p class="reel-note">নিজে থেকেই চলছে · উপরে-নিচে স্ক্রল বা সোয়াইপ করুন · '
        'ভিডিওতে ট্যাপ = প্লে/পজ</p></div>' + _PLAYER_HELPER_JS + _FEED_JS)


_WALL_JS = """<script>(function(){
  var wall = document.querySelector('.awall');
  if (!wall) return;
  var cells = [].slice.call(wall.querySelectorAll('.cell'));
  if (!cells.length) return;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var conn = navigator.connection || {};
  var autoplay = wall.dataset.autoplay === '1' && !reduce && !conn.saveData;
  function mediaOf(cell){ return cell.querySelector('video, img'); }
  function why(cell, text){ var w = cell.querySelector('.fail .why'); if (w && text) w.textContent = text; }
  function state(cell, name){ cell.classList.remove('loading','ready','failed'); if (name) cell.classList.add(name); }
  function mention(cell, text){ var w = cell.querySelector('.fail .why'); if (w) w.textContent = text || ''; }
  function play(cell){
    var v = cell.querySelector('video');
    if (!v || !autoplay) return;
    v.muted = wall.dataset.muted !== '0';
    v.loop = wall.dataset.loop !== '0';
    var p = window.bfixfPlay(v);
    if (p && p.catch) p.catch(function(){});
  }
  function stop(cell){ var v = cell.querySelector('video'); if (v) v.pause(); }
  function load(cell, fresh){
    var m = mediaOf(cell);
    if (!m) return;
    var url = m.dataset.src || m.getAttribute('src') || cell.dataset.stream || '';
    if (!url) { fail(cell, 'লিংক পাওয়া যায়নি'); return; }
    if (fresh) url = window.bfixfFresh(url);
    if (m.dataset.busy === '1') return;
    m.dataset.busy = '1';
    var settled = false;
    function good(){ settled = true; m.dataset.busy = '0'; state(cell, 'ready'); if (autoplay) play(cell); }
    function bad(){
      if (settled) return;
      settled = true; m.dataset.busy = '0';
      if (cell.dataset.asked !== '1') ask(cell); else fail(cell, 'ফাইলটা আনা যাচ্ছে না');
    }
    m.addEventListener('loadeddata', good, {once: true});
    m.addEventListener('loadedmetadata', good, {once: true});
    m.addEventListener('error', bad, {once: true});
    m.setAttribute('src', url);
    if (m.tagName === 'IMG' && m.complete && m.naturalWidth) good();
  }
  // The clip is not on this server yet → the site fetches it from Telegram now.
  function ask(cell){
    cell.dataset.asked = '1';
    state(cell, 'loading');
    mention(cell, '📥 টেলিগ্রাম থেকে আনছি…');
    var stream = cell.dataset.stream || (mediaOf(cell) || {}).dataset && mediaOf(cell).dataset.src || '';
    window.bfixfPrepare(stream, function(j){
      if (j && j.ok) { mention(cell, ''); load(cell, true); return; }
      fail(cell, (j && j.reason) || 'এখন চালানো যাচ্ছে না');
    });
  }
  function fail(cell, reason){
    state(cell, 'failed');
    mention(cell, '😕 ' + (reason || 'চলছে না'));
    var v = cell.querySelector('video'); if (v) v.pause();
  }
  function restart(cell){
    var m = mediaOf(cell);
    cell.dataset.asked = '';
    if (m) { m.dataset.busy = '0'; m.dataset.loaded = ''; }
    state(cell, 'loading');
    mention(cell, '📥 আবার চেষ্টা করছি…');
    ask(cell);
  }
  cells.forEach(function(cell){
      var retry = cell.querySelector('.fail .retry');
      if (retry) retry.addEventListener('click', function(ev){
        ev.preventDefault(); ev.stopPropagation(); restart(cell);
      });
      cell.addEventListener('click', function(ev){
        if (ev.target && ev.target.closest && ev.target.closest('a')) return;
        if (ev.target && ev.target.closest && ev.target.closest('.retry')) return;
        var v = cell.querySelector('video');
        if (!v) return;
        ev.preventDefault();
        if (v.paused) { v.muted = wall.dataset.muted !== '0'; window.bfixfPlay(v); }
        else v.pause();
      });
      var v = cell.querySelector('video');
      if (v) v.addEventListener('ended', function(){ if (!v.loop) play(cell); });
  });
  // Every cell of the wall is meant to run *together* — load them all as soon as
  // the row is on screen, then keep them playing while it stays visible.
  var io = new IntersectionObserver(function(entries){
    entries.forEach(function(entry){
      if (entry.isIntersecting) { load(entry.target); play(entry.target); }
      else stop(entry.target);
    });
  }, {threshold: 0.25});
  cells.forEach(function(cell){ io.observe(cell); });
  document.addEventListener('visibilitychange', function(){
    cells.forEach(function(cell){
      if (document.hidden) { stop(cell); return; }
      var box = cell.getBoundingClientRect();
      if (box.top < window.innerHeight && box.bottom > 0) { load(cell); play(cell); }
    });
  });
})();</script>"""


def _campaign_media_tile(item: dict) -> str:
    """One tile of “সংযুক্ত মিডিয়া” — linked to the inbox when we know that row."""
    item_id = int(item.get("item_id") or 0)
    badge = "🖼" if (item.get("kind") or "") == "photo" else "🎬"
    name = esc((item.get("name") or "")[:40])
    body = (f'<img loading="lazy" src="/inbox/{item_id}/thumb" alt="">' if item_id
            else '<span class="play">📎</span>')
    inner = f'{body}<span class="play">{badge}</span><span class="tag">{name}</span>'
    if not item_id:                       # not from the inbox — nothing to open
        return f'<span class="tile" style="aspect-ratio:1/1;cursor:default">{inner}</span>'
    return (f'<a class="tile" style="aspect-ratio:1/1" href="/inbox/{item_id}" '
            f'target="_blank" rel="noopener">{inner}</a>')


def _feed_wall_section(bots: str) -> str:
    """3–4 clips that autoplay *together* — the “একসাথে অটো-প্লে” view.

    Muted, looping, poster-first (so nothing is ever a black box), and paused the
    moment the wall scrolls out of view or the tab goes to the background.
    """
    rows = feed_service.wall(bots)
    if not rows:
        return ""
    play = feed_service.playback()
    cells = []
    for index, item in enumerate(rows):
        kind = item.get("media_kind") or "video"
        name = esc(item.get("name") or ("ভিডিও" if kind == "video" else "ছবি"))
        store = esc(item.get("store_name") or "")
        source = item.get("stream") or ""
        thumb = esc(item.get("thumb") or "")
        key = esc(item.get("key") or "")
        link = item.get("bot_link") or f"/watch?item={key}"
        eager = index == 0
        if kind == "photo":
            media = (f'<img alt="{name}" decoding="async" '
                     f'{"src" if eager else "data-src"}="{esc(source)}">')
        else:
            media = (f'<video muted loop playsinline '
                     f'preload="{"metadata" if eager else "none"}" '
                     f'{"src" if eager else "data-src"}="{esc(source)}" '
                     f'poster="{thumb}"></video>')
        cells.append(
            f'<div class="cell" data-key="{key}" data-stream="{esc(source)}">{media}'
            f'<span class="pill badge2">{"🖼" if kind == "photo" else "🎬"}</span>'
            f'<span class="tag2">{name}{f" · {store}" if store else ""}</span>'
            f'<span class="fail"><span class="why">😕 চলছে না</span>'
            f'<button type="button" class="btn retry">▶ আবার চালান</button>'
            f'<a class="btn grey" href="{esc(link)}" target="_blank" rel="noopener">🤖 বটে</a>'
            f'</span></div>')
    flags = (f'data-autoplay="{"1" if play["autoplay"] else "0"}" '
             f'data-muted="{"1" if play["muted"] else "0"}" '
             f'data-loop="{"1" if play["loop"] else "0"}"')
    return ('<div class="page-heading" style="margin-top:26px"><div>'
            '<p class="eyebrow">AUTOPLAY · TOGETHER</p>'
            f'<h2>🎬 {len(cells)}টি মিডিয়া একসাথে চলছে</h2></div>'
            '<a class="btn grey" href="/watch">সব দেখুন →</a></div>'
            f'<div class="awall" {flags}>{"".join(cells)}</div>'
            '<p class="awall-note">নিজে থেকেই চলছে (মিউট) · যেকোনো একটা ট্যাপ করলে '
            'প্লে/পজ · 📱 মোবাইল ডেটা বাঁচাতে ছবিগুলো স্ক্রল করলেই লোড হয়</p>'
            + _PLAYER_HELPER_JS + _WALL_JS)

PUBLIC_THEME_CSS += """
:root { --accent:#facc15; --accent-dark:#ffe45c; }
.status, .burger, details, .share-links code { background:var(--surface); color:var(--ink); }
.hero, .wiz, .card { background:var(--surface); color:var(--ink); }
code, pre { background:var(--surface-2); color:var(--accent); }
.play-prompt { position:absolute; z-index:12; top:45%; left:50%; transform:translate(-50%,-50%);
  max-width:90%; background:#facc15; color:#111; min-height:48px; padding:12px 20px; }
@media(max-width:600px) {
  main { padding:14px; } body { overflow-x:hidden; }
  .landing, .watch-grid { grid-template-columns:minmax(0,1fr); }
  header { flex-wrap:wrap; gap:12px; padding:14px; }
  .top { flex-wrap:wrap; } input, textarea, select { font-size:16px; }
  button, a.btn { min-height:44px; }
  .card, .store, .watch-main { min-width:0; overflow-wrap:anywhere; }
  video { max-width:100%; } .reel { max-height:75svh; }
}
"""


def _gallery_section(bots: str, limit: int = 12) -> str:
    """A wall of thumbnails — every free video/photo, one tap away."""
    items = feed_service.gallery(bots, limit=limit)
    if not items:
        return ""
    tiles = []
    for item in items:
        link = item.get("bot_link") or f"/watch?item={esc(item.get('key') or '')}"
        name = esc(item.get("name") or "")
        kind = item.get("media_kind")
        badge = "🖼" if kind == "photo" else "▶"
        tiles.append(
            f'<a class="tile" href="{esc(link)}" target="_blank" rel="noopener">'
            f'<img loading="lazy" decoding="async" alt="{name}" src="{esc(item.get("thumb") or "")}" '
            f'onload="this.classList.add(\'ready\')" onerror="this.remove()">'
            f'<span class="play">{badge}</span><span class="tag">{name}</span></a>')
    return ('<div class="page-heading"><div><p class="eyebrow">MEDIA WALL</p>'
            '<h2 id="media">🎞 নতুন ভিডিও ও ছবি</h2></div>'
            f'<a class="btn grey" href="/watch">সব দেখুন →</a></div>'
            f'<div class="wall">{"".join(tiles)}</div>')


def store_cover_url(store: dict) -> str:
    """Where the store's cover image lives (empty when none is set)."""
    if not store or not (store.get("cover") or "").strip():
        return ""
    return f"cover/{int(store['id'])}"


def layout(title: str, body: str, active: str = "",
           canonical_path: str | None = None) -> str:
    brand = html.escape(settings.get_str("WEB_TITLE", "Desi Site") or "Desi Site")
    site_url = (settings.get_str("SITE_URL", cfg.SITE_URL) or cfg.SITE_URL).rstrip("/")
    base = basepath.current() or ""
    private_page = active.startswith("/admin") or title in {"Sign in", "Not found"}
    if private_page:
        seo = '<meta name="robots" content="noindex,nofollow">'
    else:
        canonical_path = canonical_path or (active if active.startswith("/") else "/")
        canonical_url = site_url + canonical_path
        seo = (f'<meta property="og:url" content="{html.escape(canonical_url)}">'
               f'<link rel="canonical" href="{html.escape(canonical_url)}">')
    head = f"""<!doctype html><html lang="bn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<base href="__BASEPATH__/">
<title>{html.escape(title)} · {brand}</title>
<meta name="description" content="Telegram store — simple access to your videos and learning content.">
<meta property="og:site_name" content="{brand}">{seo}
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎬</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@600;700;800&family=Hind+Siliguri:wght@400;500;600&display=swap">
<style>{CSS}</style><style>{ui.THEME_CSS if (active.startswith(("/admin", "/setup")) or title == "Sign in") else PUBLIC_THEME_CSS}</style></head>"""
    if active.startswith("/admin"):
        # A page that did not write its own heading still gets one, so every screen
        # starts the same way (title + one-line explanation + the section it is in).
        if 'class="page-heading"' not in body:
            group, label = _nav_trail(active, title)
            blurb = PAGE_BLURBS.get(active, "")
            body = ui.page_head(title, blurb, eyebrow=group) + body

        def _nav_on(href: str) -> bool:
            if href == "/admin":
                return active == href
            return active == href or active.startswith(href + "/")

        def _group(name: str, items: tuple) -> str:
            """One sidebar group. `<details>` = collapsible without a line of JS,
            and the group holding the open page starts expanded."""
            opened = " open" if any(_nav_on(href) for href, _ic, _label in items) else ""
            links = "".join(
                f'<a class="{"on" if _nav_on(href) else ""}" href="{href}">'
                f'<span class="ic">{ic}</span>{label}</a>'
                for href, ic, label in items)
            return f'<details class="navgrp"{opened}><summary>{name}</summary>{links}</details>'

        groups = "".join(_group(name, items) for name, items in ADMIN_NAV)
        # The shell: black rail · hairline topbar with a breadcrumb, the palette
        # trigger and the bot state · the page body · a quiet footer.
        crumb_html = ui.crumbs(("Overview", "/admin"), _nav_trail(active, title))
        page = f"""{head}<body>
<div class="shell">
  <aside class="side">
    <div class="brand"><span class="logo">🎬</span>
      <span>{brand}<small>control room</small></span></div>
    {groups}
    <div class="rail-foot">
      <a href="/" target="_blank" rel="noopener"><span class="ic">↗</span>পাবলিক সাইট দেখুন</a>
      <a href="/admin/audit"><span class="ic">🧾</span>অ্যাডমিন অ্যাকশন লগ</a>
      <a href="/admin/logout"><span class="ic">⇥</span>সাইন আউট</a>
      <span>v{PANEL_VERSION} · বট + সাইট একই ডেটাবেজ</span>
    </div>
  </aside>
  <div class="content">
    <div class="topbar">
      <div class="row">
        <button class="burger" aria-label="মেনু" onclick="document.body.classList.toggle('nav')">☰</button>
        <div class="crumbs">{crumb_html}</div>
      </div>
      <div class="row">
        <button class="kbd-btn" id="palette-open" type="button">🔎 <span>খুঁজুন</span>
          <kbd>Ctrl</kbd><kbd>K</kbd></button>
        {_status_pill()}
      </div>
    </div>
    <main>{body}</main>
    <footer>Telegram store bot · প্রতিটি ফাইল বট নিজেই পাঠায় ·
      সমস্যা হলে <a href="/admin/emergency">জরুরি পেজ</a> · লগ <a href="/admin/logs">এখানে</a></footer>
  </div>
</div>
{ui.PALETTE_JS}
<script>document.addEventListener('click',function(e){{if(document.body.classList.contains('nav')&&!e.target.closest('aside')&&!e.target.closest('.burger'))document.body.classList.remove('nav')}})</script>
</body></html>"""
    else:
        pub = (("/", "🏪 স্টোর"), ("/watch", "🎞 মিডিয়া"), ("/search", "🔍 সার্চ"),
               ("/help", "❓ সাহায্য"))
        nav = "".join(f'<a class="{"on" if href == active else ""}" href="{href}">{label}</a>'
                      for href, label in pub)
        backups = []
        for extra in db.bots_store.bots():
            username = (extra.get("username") or "").lstrip("@")
            if extra.get("enabled") and re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
                backups.append(f'<a href="https://t.me/{username}">🤖 বিকল্প বট @{username}</a>')
        backup_links = " · ".join(backups)
        page = f"""{head}<body>
<header>
  <h1><span class="logo">◈</span>{brand}</h1>
  <nav class="top">{nav}</nav>
</header>
<main>{body}</main>
<footer>{brand} · ফাইল বট নিজেই পাঠায়<br>{backup_links}</footer>
</body></html>"""
    page = basepath.relativize(page)
    return page.replace("__BASEPATH__", base)



SETUP_JS = """
const $ = (s) => document.querySelector(s);
const form = $('#f'), prog = $('#prog'), done = $('#done');
function fe(field, msg) {
  document.querySelectorAll('[data-fe]').forEach(e => e.textContent = '');
  document.querySelectorAll('input').forEach(e => e.classList.remove('bad-in'));
  if (!field) return;
  const box = document.querySelector('[data-fe="' + field + '"]'); if (box) box.textContent = msg;
  const inp = document.querySelector('[name="' + field + '"]'); if (inp) { inp.classList.add('bad-in'); inp.focus(); }
}
async function setupJSON(response) {
  if (response.redirected || response.status === 401 || response.status === 403) {
    throw new Error('লগইন সেশন শেষ হয়েছে। /admin/login থেকে আবার লগইন করে setup খুলুন।');
  }
  if (!response.ok) throw new Error('সার্ভার HTTP ' + response.status + ' দিয়েছে। Hosting logs দেখুন।');
  if (!(response.headers.get('content-type') || '').includes('application/json')) {
    throw new Error('সার্ভার থেকে JSON-এর বদলে অন্য পেজ এসেছে। সঠিক site URL এবং admin login পরীক্ষা করুন।');
  }
  return response.json();
}
async function poll(started) {
  try {
    const r = await fetch('setup/status', {cache: 'no-store'}); const j = await setupJSON(r);
    if (j.state === 'online') {
      prog.style.display = 'none'; done.style.display = 'block';
      $('#who').textContent = j.username ? '@' + j.username : '';
      if (j.username) { $('#tg').href = 'https://t.me/' + j.username; $('#tg').style.display = 'inline-block'; }
      return;
    }
    if (j.state === 'error') {
      prog.style.display = 'none'; form.style.display = 'block'; fe('token', j.detail); return;
    }
    $('#ptxt').textContent = j.detail || 'বট চালু হচ্ছে…';
  } catch (e) { $('#ptxt').textContent = 'সার্ভার রিস্টার্ট হচ্ছে — একটু অপেক্ষা করুন…'; }
  if (Date.now() - started < 90000) setTimeout(() => poll(started), 1500);
  else { $('#ptxt').textContent = 'অনেকক্ষণ লাগছে — পেজ রিফ্রেশ করে স্ট্যাটাস দেখুন।'; }
}
form.addEventListener('submit', async (ev) => {
  ev.preventDefault(); fe(null);
  const btn = $('#go'); btn.disabled = true; btn.textContent = 'যাচাই হচ্ছে…';
  try {
    const r = await fetch('setup/save', {method: 'POST', body: new URLSearchParams(new FormData(form))});
    const j = await setupJSON(r);
    if (!j.ok) { fe(j.field, j.error); btn.disabled = false; btn.textContent = '✅ যাচাই করে বট চালু করুন'; return; }
    form.style.display = 'none'; prog.style.display = 'block'; $('#ptxt').textContent = 'টোকেন ঠিক আছে ✓ বট চালু হচ্ছে…';
    poll(Date.now());
  } catch (e) { fe('token', e.message || 'সার্ভারের সাথে যোগাযোগ হয়নি — আবার চেষ্টা করুন।'); btn.disabled = false; btn.textContent = '✅ যাচাই করে বট চালু করুন'; }
});
"""


def setup_page() -> str:
    configured = credentials.is_configured()
    brand = html.escape(settings.get_str("WEB_TITLE", "Store") or "Store")
    base = basepath.current() or ""
    admins = ", ".join(str(i) for i in cfg.ADMIN_IDS)
    keep = ' placeholder="খালি রাখলে আগেরটাই থাকবে"' if configured else ""
    token_ph = html.escape(credentials.masked_token()) if configured else "1234567890:AAH…"
    back = ""
    database_notice = '<div class="warnbox">ডেটাবেজে সংযোগ নেই। বট ও সাধারণ কাজ নিরাপদে বন্ধ আছে। নিচে connection file দিয়ে আবার যুক্ত করুন।</div>' if cfg.DATABASE_UNAVAILABLE else ''
    page = f"""<!doctype html><html lang="bn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><base href="__BASEPATH__/">
<title>সেটআপ · {brand}</title><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@600;700;800&family=Hind+Siliguri:wght@400;500;600&display=swap"><style>{CSS}</style><style>{ui.THEME_CSS}</style></head><body>
<div class="wiz">
  {database_notice}
  <div class="head"><div class="logo">🤖</div>
    <h2>{"বট কানেকশন" if configured else "চলুন বটটা চালু করি"}</h2>
    <p>{"তথ্য বদলাতে চাইলে নতুন মান দিন — খালি ঘর আগের মতোই থাকবে।" if configured else "৩টা ছোট ধাপ — সব এখানেই, কোনো ফাইল বা ভেরিয়েবল ছুঁতে হবে না।"}</p>
    {('<p class="help">🔒 টোকেন, API ID ও হ্যাশ <b>ডেটাবেজে সেভ করা আছে</b> — একবার দিলে আর কখনো দিতে হবে না, হোস্ট বদলালেও ব্যাকআপের সাথে ফিরে আসে। নতুন বট যোগ করতে <a href="/admin/bots">⚙ বটগুলো</a> দেখুন।</p>' if configured else '')}</div>
  <div class="card">
  <form id="f" autocomplete="off">
    <div class="step"><div class="num">১</div><div class="box">
      <h4>বট টোকেন</h4>
      <p class="help">টেলিগ্রামে <b>@BotFather</b> → <code>/newbot</code> (বা /mybots → API Token) থেকে কপি করুন।</p>
      <input name="token" type="password" placeholder="{token_ph}" autocomplete="off" spellcheck="false">
      <p class="fe" data-fe="token"></p></div></div>
    <div class="step"><div class="num">২</div><div class="box">
      <h4>API ID ও API Hash</h4>
      <p class="help"><a href="https://my.telegram.org/apps" target="_blank" rel="noopener">my.telegram.org/apps</a> → লগইন → একটা অ্যাপ বানালেই পাবেন।</p>
      <input name="api_id" inputmode="numeric" placeholder="API ID (সংখ্যা)" value="{cfg.API_ID or ''}">
      <p class="fe" data-fe="api_id"></p>
      <input name="api_hash" type="password" placeholder="API Hash (৩২ অক্ষর){' — খালি = আগেরটা' if configured else ''}" autocomplete="off" spellcheck="false">
      <p class="fe" data-fe="api_hash"></p></div></div>
    <div class="step"><div class="num">৩</div><div class="box">
      <h4>অ্যাকাউন্টের মালিকের Telegram ID</h4>
      <p class="help"><b>@userinfobot</b>-কে /start দিন, যে সংখ্যাটা দেবে সেটা লিখুন। একাধিক হলে কমা দিয়ে।</p>
      <input name="admins" inputmode="numeric" placeholder="যেমন 123456789" value="{html.escape(admins)}">
      <p class="fe" data-fe="admins"></p></div></div>
    <button id="go" class="big" type="submit">✅ যাচাই করে বট চালু করুন</button>
  </form>
  <div class="prog" id="prog"><div class="spin"></div><b id="ptxt">বট চালু হচ্ছে…</b>
    <p class="muted">টেলিগ্রামের সাথে সংযোগ হচ্ছে, সাধারণত ১০-২০ সেকেন্ড লাগে।</p></div>
  <div class="prog" id="done"><div class="tick">✓</div><h2 style="margin:0 0 6px">বট চালু হয়েছে 🎉</h2>
    <p class="muted"><span id="who"></span> এখন অনলাইন। সাইটে ফিরে কাজ চালিয়ে যান।</p>
    <div class="row" style="justify-content:center;margin-top:14px">
      <a class="btn" href="/">🌐 সাইটে যান</a>
      <a class="btn grey" id="tg" style="display:none" target="_blank" rel="noopener">🤖 টেলিগ্রামে বট খুলুন</a></div></div>
  </div>
  <div class="card" id="database">
    <h3>☁️ Supabase / PostgreSQL</h3>
    <p>Environment variable লাগবে না। ব্যক্তিগত database configuration ফাইল দিন।
       বর্তমান database URL বা password এই পেজে দেখানো হয় না।</p>
    <form method="post" action="setup/database" enctype="multipart/form-data">
      <input type="file" name="file" accept=".json,.zip,application/json,application/zip" required>
      <label><input type="checkbox" name="confirm" value="yes" required> সংযোগ পরীক্ষা করে সেভ করুন</label>
      <button class="btn" type="submit">☁️ ডেটাবেজ যুক্ত করুন</button>
    </form>
    <p class="help">নতুন/অফলাইন install হলে এখনই যুক্ত হবে। বট চালু থাকলে নিরাপদে
       পরিবর্তনের জন্য restart করতে হবে। /var/data disk থাকলে connection file সেখানেই থাকে।</p>
  </div>
  <div class="card" id="restore">
    <div class="step"><div class="num">🔄</div><div class="box">
      <h4>হোস্টিং বদলেছেন? আগের ইনস্টল ফিরিয়ে আনুন</h4>
      <p class="help">আগের জায়গা থেকে নামানো ব্যাকআপ ফাইলটা এখানে দিন — পুরোনো
      ইউজার, আইডি, স্টোর, ফাইল, সেটিংস আর টোকেন সব ফিরে আসবে। নতুন করে টোকেন দিতে
      হবে না, উপরের ঘরগুলো খালি রাখলেই চলবে।</p>
      <form method="post" action="setup/restore" enctype="multipart/form-data">
        <input type="file" name="file" accept=".gz,.json,.sqlite3,.db,.bfixf,application/gzip">
        <textarea name="code" rows="2" placeholder="…অথবা ব্যাকআপ কোড পেস্ট করুন (BFIXF1:…)"></textarea>
        <label class="chk"><input type="checkbox" name="confirm" value="yes">
          হ্যাঁ, ব্যাকআপটা ফিরিয়ে আনুন</label>
        <button class="grey" type="submit">♻️ ইনস্টল ফিরিয়ে আনুন</button>
      </form>
      <p class="help" id="rn"></p>
    </div></div>
  </div>
  {back}
  <p class="muted" style="text-align:center;margin-top:18px">🔒 টোকেন শুধু এই সাইটের ডাটাবেসে থাকে — কোডে বা এনভায়রনমেন্টে নয়, আর এখানে কখনো পুরোটা দেখানো হয় না।</p>
</div>
<script>{SETUP_JS}</script></body></html>"""
    page = basepath.relativize(page)
    return page.replace("__BASEPATH__", base)


def card(label: str, value) -> str:
    return f'<div class="card"><div class="l">{esc(label)}</div><div class="n">{value}</div></div>'


def store_cards(stores: list[dict]) -> str:
    if not stores:
        return '<div class="card"><p class="muted">এখনো কোনো স্টোর নেই — শীঘ্রই এখানে কনটেন্ট যোগ করা হবে।</p></div>'
    blocks = []
    for store in stores:
        stats = db.store_stats(store["id"])
        lock = "🔒 Premium" if store["is_premium"] else "🆓 Free"
        link = deep_link(f"s{store['id']}") or deep_link(store["slug"] or "")
        desc = esc((store.get("description") or "")[:140])
        cover = store_cover_url(store)
        cover_html = (f'<div class="store-cover" style="background-image:url({cover})">'
                      f'<span class="badge pill">{lock}</span></div>') if cover else ""
        blocks.append(f"""<div class="store">
   {cover_html}
   <h3>{esc(store['name'])}</h3>
   <div class="row"><span class="pill">{lock}</span>
        <span class="pill">📂 {stats['files']} files</span>
        <span class="pill">👁 {stats['views']}</span></div>
   {f'<p class="muted">{desc}</p>' if desc else ''}
   <div class="row">
     {f'<a class="btn" href="{link}">▶️ Telegram-এ খুলুন</a>' if link else ''}
     <a class="btn grey" href="/s/{esc(store['slug'] or store['id'])}">বিস্তারিত</a>
   </div>
 </div>""")
    return f'<div class="stores">{"".join(blocks)}</div>'


# ================================================================= public site
def create_app() -> FastAPI:
    app = FastAPI(title="Store Bot Web", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------------- site
    @app.middleware("http")
    async def honor_proxy_prefix(request: Request, call_next):
        """Live behind CodeNest/RunSpace: keep every link inside /live/<job>/.

        The panel's gateway strips the prefix before forwarding it and tells us
        what it was in `X-Forwarded-Prefix`. We remember it for this request (so
        `layout()` can emit the right `<base href>`) and set Starlette's
        `root_path`, so anything the framework generates matches as well.
        """
        prefix = basepath.set_prefix(request.headers.get("x-forwarded-prefix")
                                     or request.headers.get("x-script-name"))
        if prefix:
            request.scope["root_path"] = prefix
        try:
            return await call_next(request)
        finally:
            basepath.set_prefix("")

    #: Requests that are never counted as “a page a visitor read”.
    _TRACK_SKIP = ("/media/", "/reel/", "/inbox/", "/cover/", "/health", "/favicon",
                   "/robots.txt", "/setup/status", "/admin/visitors.json")

    @app.middleware("http")
    async def track_visitors(request: Request, call_next):
        """Analytics for the owner: who is here, from where, doing what.

        One first-party cookie (`bf_vid`), no external service, nothing personal
        beyond what the browser itself sends. The owner's own requests are flagged
        as admin so they never inflate the visitor numbers.
        """
        path = request.url.path
        started = time.time()
        skip = any(path.startswith(prefix) for prefix in _TRACK_SKIP)
        vid = ""
        if not skip:
            try:
                if not visitors.is_bot(request.headers.get("user-agent") or ""):
                    vid = visitors.ensure_visit(request, is_admin=_authed(request))
            except Exception:
                log.debug("visitor session skipped", exc_info=True)
        response = await call_next(request)
        if vid:
            try:
                visitors.touch(request, vid=vid, path="" if skip else path,
                               status=int(getattr(response, "status_code", 200)),
                               ms=int((time.time() - started) * 1000),
                               event="login" if path == "/admin/login"
                               and request.method == "POST" else "")
                if not _authed(request) and visitors.valid_vid(vid):
                    response.set_cookie(visitors.COOKIE, vid, max_age=30 * 86400,
                                        httponly=True, samesite="lax")
            except Exception:
                log.debug("visitor tracking skipped", exc_info=True)
        return response

    @app.middleware("http")
    async def setup_gate(request: Request, call_next):
        """Until the bot is configured, every page leads to the setup wizard."""
        path = request.url.path
        # Visitors always get the simple public site. Only the admin area leads to the
        # (password-protected) setup page while the bot is not configured yet.
        if (not credentials.is_configured() and path.startswith("/admin")
                and not path.startswith(("/admin/login", "/admin/logout"))):
            return RedirectResponse("/setup", status_code=303)
        return await call_next(request)

    @app.middleware('http')
    async def database_recovery_gate(request: Request, call_next):
        if cfg.DATABASE_UNAVAILABLE and request.url.path not in (
                '/admin/login', '/admin/logout', '/setup', '/setup/database', '/health', '/favicon.ico'):
            return Response('Database unavailable. Owner: open /setup to reconnect.', status_code=503,
                            headers={'Retry-After': '10'})
        return await call_next(request)

    @app.post('/setup/database')
    async def setup_database(request: Request, confirm: str = Form(''), file: UploadFile | None = File(None)):
        blocked = _guard(request)
        if blocked:
            return blocked
        if confirm != 'yes' or file is None:
            return JSONResponse({'ok': False, 'error': 'ফাইল ও confirmation দিন।'}, status_code=400)
        from app import database_config
        try:
            config = database_config.parse(await file.read(16385))
        except (ValueError, UnicodeError):
            return JSONResponse({'ok': False, 'error': 'সঠিক database configuration JSON অথবা connection ZIP দিন।'}, status_code=400)
        try:
            await asyncio.to_thread(database_config.probe, config)
        except Exception:
            log.exception('PostgreSQL connection verification failed')
            return JSONResponse({'ok': False, 'error': 'ডেটাবেজে সংযোগ হয়নি। URL, password ও provider network settings পরীক্ষা করুন।'}, status_code=502)
        async with database_config.switch_lock():
            # Never switch underneath live bot/session workers. In that case only save
            # the configuration: next startup performs the atomic SQLite bootstrap.
            if not cfg.DATABASE_UNAVAILABLE and (runtime.bot_online() or runtime.user_clients or credentials.is_configured()):
                database_config.save(config, cfg.PRIVATE_DATABASE_FILE)
                return HTMLResponse(layout('Database saved', '<div class="card"><h2>সংযোগ সেভ হয়েছে</h2><p>বট/সাইট restart করুন। Environment variable লাগবে না। পুরোনো ডেটা মুছবেন না।</p></div>', '/setup'))
            old = (cfg.DATABASE_URL, cfg.DB_FILE, cfg.PG_SSLMODE, db._impl)
            try:
                cfg.DATABASE_URL = cfg.DB_FILE = config['database_url']
                cfg.PG_SSLMODE = config['sslmode']
                db.bind(cfg.DB_FILE)
                database_config.save(config, cfg.PRIVATE_DATABASE_FILE)
            except Exception:
                failed = db._impl
                cfg.DATABASE_URL, cfg.DB_FILE, cfg.PG_SSLMODE, db._impl = old
                if failed is not old[3] and failed is not None:
                    failed.conn.close()
                log.exception('PostgreSQL setup/import failed; previous database retained')
                return JSONResponse({'ok': False, 'error': 'ডেটাবেজ সেটআপ সম্পন্ন হয়নি। আগের ডেটা অক্ষত আছে।'}, status_code=502)
            cfg.DATABASE_UNAVAILABLE = False
            portable_service._clear_caches()
            credentials.load()
            credentials.poke()
            return RedirectResponse(basepath.url('/setup'), status_code=303)

    @app.post("/setup/restore")
    async def setup_restore(request: Request, confirm: str = Form(""),
                            code: str = Form(""),
                            file: UploadFile | None = File(None)):
        """Bring an old install back on a **new host** — before it is configured.

        This is the whole point of the portable backup: on a fresh server the bot is
        not configured yet, so the panel is not reachable. The restore therefore
        lives here too. It is only allowed while this install is still unclaimed
        (once it is configured, /admin/backup — behind the password — does it).
        """
        if credentials.is_configured():
            blocked = _guard(request)
            if blocked:
                return blocked
        if (confirm or "").strip().lower() not in ("yes", "on", "1"):
            return RedirectResponse(basepath.url("/setup?restore=1&warn=Tick+the+box"), status_code=303)
        blob = b""
        if file is not None and getattr(file, "filename", ""):
            blob = await file.read()
        elif (code or "").strip():
            try:
                blob = portable_service.decode_text(code)
            except portable_service.BackupError as exc:
                return RedirectResponse(basepath.url(f"/setup?restore=1&warn={str(exc)[:150]}"),
                                        status_code=303)
        if not blob:
            return RedirectResponse(basepath.url("/setup?restore=1&warn=No+file+or+code"), status_code=303)
        ok, message = _run_restore(blob, "setup page")
        if not ok:
            return RedirectResponse(basepath.url(f"/setup?restore=1&warn={message[:150]}"), status_code=303)
        # credentials may have come back with the database → the bot can start now.
        try:
            credentials.load()
            credentials.poke()          # wake run.bot_worker without a restart
        except Exception:
            log.debug("credential reload after restore failed", exc_info=True)
        return RedirectResponse(basepath.url("/admin"), status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_view(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        return setup_page()

    @app.post("/setup/save")
    async def setup_save(request: Request, token: str = Form(""), api_id: str = Form(""),
                         api_hash: str = Form(""), admins: str = Form("")) -> JSONResponse:
        # Bot setup is part of the admin area: always requires the admin password login.
        if not _authed(request):
            return _guard(request)
        return JSONResponse(await credentials.verify_and_save(token, api_id, api_hash, admins))

    @app.get("/setup/status")
    async def setup_status(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        st = dict(credentials.state)
        if runtime.bot_online():
            st = {"state": "online", "detail": "", "username": _bot_username()}
        return JSONResponse(st, headers={"Cache-Control": "no-store"})

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> str:
        stores = db.all_stores()
        stats = db.stats()
        bots = _bot_username()
        feed_html = _feed_section(bots)
        wall_html = _feed_wall_section(bots)
        body = f"""
        <div class="landing{' has-feed' if feed_html else ''}">
        <div class="hero">
          <p class="eyebrow">DESI SITE · OFFICIAL</p>
          <h2>ভিডিও ও learning content—<span>একটি পরিষ্কার জায়গায়।</span></h2>
          <p>{esc(settings.get_str("SITE_TAGLINE")) or "আপনার পছন্দের store বেছে নিন। Telegram bot ভিডিওটি সরাসরি পাঠাবে—সহজ, দ্রুত, নিরাপদ।"}</p>
          <div class="row">
            {f'<a class="btn" href="https://t.me/{bots}">🤖 Telegram-এ খুলুন</a>' if bots else ''}
            <a class="btn grey" href="#stores">Explore stores ↓</a>
          </div>
        </div>
        {feed_html}
        </div>
        {wall_html}
        <div class="grid">
          {card("Stores", stats['stores'])}
          {card("Videos & files", stats['files'])}
          {card("Community", stats['users'])}
          {card("Total views", stats['views'])}
        </div>
        {_gallery_section(bots)}
        <div class="howto">
          <b>শুরু করতে তিনটি ধাপ:</b>
          <span>01 · Store বাছুন</span><span>02 · Telegram-এ খুলুন</span>
          <span>03 · ভিডিও সরাসরি পান</span>
        </div>
        <div class="page-heading"><div><p class="eyebrow">SELECTED WORK</p><h2 id="stores">Featured collections</h2></div></div>
        {store_cards(stores)}
        """
        return layout("Home", body, "/")

    async def _stream_media(request: Request, row: dict, label: str):
        """Shared Range streaming for /media/<file> and /reel/<pin>.

        Two sources: a Telegram message (mirror first, then the original, read with
        whichever client is online) and — for the seeded preview install — a clip on
        disk (`chat_id = 0`).
        """
        from fastapi.responses import StreamingResponse
        cached = feed_service.cached_size(row)
        if cached:
            # The bytes are already on disk (mirror download or a previous visitor):
            # stream them straight away — this is what makes every clip play, even
            # while Telegram is unreachable or a mirror is still being built.
            try:
                start, end = feed_service.parse_range(request.headers.get("range"), cached)
            except ValueError:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{cached}"})
            return StreamingResponse(
                feed_service.iter_cached(row, start, end), status_code=206,
                media_type="video/mp4" if site_kind(row) == "video" else "image/jpeg",
                headers={"Accept-Ranges": "bytes",
                         "Content-Range": f"bytes {start}-{end}/{cached}",
                         "Content-Length": str(end - start + 1),
                         "Cache-Control": "public, max-age=3600"})
        if feed_service.is_demo(row):
            size = feed_service.local_size(row)
            if not size:
                return Response("Not found", status_code=404)
            try:
                start, end = feed_service.parse_range(request.headers.get("range"), size)
            except ValueError:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            return StreamingResponse(
                feed_service.iter_local(row, start, end), status_code=206,
                media_type="video/mp4" if str(site_kind(row)) == "video" else "image/jpeg",
                headers={"Accept-Ranges": "bytes",
                         "Content-Range": f"bytes {start}-{end}/{size}",
                         "Content-Length": str(end - start + 1),
                         "Cache-Control": "public, max-age=3600"})
        try:
            msg = await feed_service.message_for(row)
        except Exception as exc:
            log.debug("feed media %s unavailable: %s", label, exc)
            msg = None
        size = feed_service.media_size(msg) if msg is not None else 0
        if msg is None or not size:
            return Response("Not found", status_code=404)
        try:
            start, end = feed_service.parse_range(request.headers.get("range"), size)
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        return StreamingResponse(
            feed_service.iter_range(msg, start, end), status_code=206,
            media_type=feed_service.media_mime(msg),
            headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{size}",
                     "Content-Length": str(end - start + 1),
                     "Cache-Control": "public, max-age=3600"})

    def site_kind(row: dict) -> str:
        """`video` / `photo` for a row, however it was stored."""
        kind = row.get("media_kind") or ("photo" if str(row.get("kind") or "").lower()
                                         .startswith("photo") else "video")
        return "photo" if kind == "photo" else "video"

    async def _thumb_response(row: dict, label: str):
        cached_poster = feed_service.cached_thumb(row)
        if cached_poster:
            return Response(cached_poster, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=3600"})
        if feed_service.is_demo(row):
            frame = feed_service.demo_frame(row)
            if frame:
                return Response(frame, media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=3600"})
        try:
            data = await feed_service.thumb_for(row)
        except Exception as exc:
            log.debug("feed thumb %s unavailable: %s", label, exc)
            data = None
        if not data:
            # Never a broken image: the browser gets a generated poster instead.
            poster = feed_service.placeholder_svg(
                row.get("name") or row.get("store_name") or "", site_kind(row))
            return Response(poster, media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=600"})
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/media/{file_id}")
    async def feed_media(request: Request, file_id: int):
        """Stream one free video/photo (Range supported). Premium/unknown = 404."""
        row = db.file(file_id)
        if not feed_service.eligible(row):
            return Response("Not found", status_code=404)
        return await _stream_media(request, row, f"file {file_id}")

    @app.get("/media/{file_id}/thumb")
    async def feed_thumb(file_id: int):
        row = db.file(file_id)
        if not feed_service.eligible(row):
            return Response("Not found", status_code=404)
        return await _thumb_response(row, f"file {file_id}")

    #: One fetch per item at a time + a short cooldown, so a burst of visitors
    #: never turns into a burst of Telegram downloads.
    _PREPARE_LOCKS: dict[str, asyncio.Lock] = {}
    _PREPARE_LAST: dict[str, float] = {}

    async def _prepare_item(row: dict, label: str):
        """Make sure this clip is playable *now*; returns (ok, size, reason)."""
        size = feed_service.cached_size(row)
        if size:
            return True, size, ""
        key = media_cache.cache_key(row)
        lock = _PREPARE_LOCKS.setdefault(key, asyncio.Lock())
        if lock.locked():
            async with lock:                       # someone else is fetching it
                size = feed_service.cached_size(row)
            return (size > 0), size, media_cache.cache_problem(row)
        async with lock:
            size = feed_service.cached_size(row)
            if size:
                return True, size, ""
            if time.time() - _PREPARE_LAST.get(key, 0) < 5:
                return False, 0, media_cache.cache_problem(row) or "একটু পরে আবার চেষ্টা করুন"
            _PREPARE_LAST[key] = time.time()
            try:
                size = await feed_service.prepare(row)
            except Exception as exc:
                log.debug("prepare %s failed: %s", label, exc)
                size = 0
            reason = media_cache.cache_problem(row) or ("" if size else "মিডিয়া এখন পড়া যাচ্ছে না")
            if not size:
                visitors.note(name="media_failed", file_id=int(row.get("id") or 0),
                              detail=reason[:120])
                log.warning('Public media preparation failed (%s): %s', label, reason)
            return (size > 0), size, ("" if size else "ভিডিওটি এখন প্রস্তুত করা যায়নি। একটু পরে আবার চেষ্টা করুন।")

    @app.get("/media/{file_id}/prepare")
    async def feed_prepare(request: Request, file_id: int):
        """“চালান” on a clip the cache does not hold yet → fetch it, then play."""
        row = db.file(file_id)
        if not feed_service.eligible(row):
            return JSONResponse({"ok": False, "reason": "এই মিডিয়া সাইটে দেখানো হয় না"},
                                headers={"Cache-Control": "no-store"})
        ok, size, reason = await _prepare_item(row, f"file {file_id}")
        visitors.note(request, name="prepare" if ok else "media_failed", file_id=file_id,
                      detail=(reason or "")[:120])
        return JSONResponse({"ok": ok, "size": size, "reason": reason, "kind": "media"},
                            headers={"Cache-Control": "no-store"})

    @app.get("/reel/{pin_id}/prepare")
    async def reel_prepare(request: Request, pin_id: int):
        row = feed_service.eligible_pin(pin_id)
        if not row:
            return JSONResponse({"ok": False, "reason": "পিনটা পাওয়া যায়নি"},
                                headers={"Cache-Control": "no-store"})
        ok, size, reason = await _prepare_item(row, f"pin {pin_id}")
        visitors.note(request, name="prepare" if ok else "media_failed", file_id=pin_id,
                      detail=(reason or "")[:120])
        return JSONResponse({"ok": ok, "size": size, "reason": reason, "kind": "reel"},
                            headers={"Cache-Control": "no-store"})

    @app.get("/reel/{pin_id}")
    async def reel_media(request: Request, pin_id: int):
        """Stream a hand-picked reel item (pinned by the owner with /reel add)."""
        pin = feed_service.eligible_pin(pin_id)
        if not pin:
            return Response("Not found", status_code=404)
        return await _stream_media(request, pin, f"pin {pin_id}")

    @app.get("/reel/{pin_id}/thumb")
    async def reel_thumb(pin_id: int):
        pin = feed_service.eligible_pin(pin_id)
        if not pin:
            return Response("Not found", status_code=404)
        return await _thumb_response(pin, f"pin {pin_id}")

    @app.get("/reel/play/{key}")
    async def reel_play(request: Request, key: str):
        """Tiny beacon the reel sends when a slide actually starts playing."""
        feed_service.note_play(key)
        file_id = 0
        if key.startswith("file:"):
            try:
                file_id = int(key.split(":", 1)[1])
            except ValueError:
                file_id = 0
        visitors.note(request, name="media_play", file_id=file_id, detail=key[:60])
        return Response(status_code=204)

    @app.get("/inbox/{item_id}")
    async def inbox_media(item_id: int):
        row = db.bc_media_item(item_id)
        if not row:
            return Response("Not found", status_code=404)
        try:
            message = await feed_service.bot.get_messages(int(row["chat_id"]),
                                                          ids=int(row["msg_id"]))
        except Exception as exc:
            log.debug("inbox media %s unavailable: %s", item_id, exc)
            message = None
        if message is None or not getattr(message, "media", None):
            return Response("Not found", status_code=404)
        size = feed_service.media_size(message)
        if not size:
            return Response("Not found", status_code=404)
        start, end = 0, min(size - 1, feed_service.MAX_RESPONSE - 1)
        from fastapi.responses import StreamingResponse
        return StreamingResponse(feed_service.iter_range(message, start, end),
                                 media_type=feed_service.media_mime(message),
                                 headers={"Cache-Control": "private, max-age=600"})

    @app.get("/inbox/{item_id}/thumb")
    async def inbox_thumb(item_id: int):
        row = db.bc_media_item(item_id)
        if not row:
            return Response("Not found", status_code=404)
        item = {"pin_id": 0, "id": -int(item_id), "chat_id": row["chat_id"],
                "msg_id": row["msg_id"], "kind": row.get("kind") or "video"}
        return await _thumb_response(item, f"inbox {item_id}")

    @app.get("/cover/{store_id}")
    async def store_cover(store_id: int):
        """The cover image the owner set in the bot — a real image on the website."""
        store = db.store(store_id)
        if not store:
            return Response("Not found", status_code=404)
        raw = (store.get("cover") or "").strip()
        if not raw:
            return Response("Not found", status_code=404)
        chat, _, msg = raw.partition(":")
        try:
            chat_id, msg_id = int(chat), int(msg)
        except ValueError:
            return Response("Not found", status_code=404)
        data = None
        try:
            message = await feed_service.bot.get_messages(chat_id, ids=msg_id)
            if message is not None and getattr(message, "media", None):
                data = (await feed_service.bot.download_media(message, file=bytes, thumb=-1)
                        or await feed_service.bot.download_media(message, file=bytes))
        except Exception as exc:
            log.debug("cover %s unavailable: %s", store_id, exc)
            data = None
        if not data:
            return Response("Not found", status_code=404)
        return Response(bytes(data), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/watch", response_class=HTMLResponse)
    async def public_watch(request: Request, item: str = "") -> str:
        """Every free video & photo, with an instant inline player (no download)."""
        bots = _bot_username()
        items = feed_service.gallery(bots)
        if not items:
            return layout("Watch", "<h2>🎞 মিডিয়া</h2>"
                                  "<p class='muted'>এখনো কিছু যোগ করা হয়নি — শীঘ্রই আসছে।</p>",
                          "/watch")
        chosen = next((row for row in items if row.get("key") == item), items[0])
        if item:
            file_id = 0
            if str(item).startswith("file:"):
                try:
                    file_id = int(str(item).split(":", 1)[1])
                except ValueError:
                    file_id = 0
            visitors.note(request, name="media_open", file_id=file_id, detail=str(item)[:40])
        # The hero obeys the same playback switches as the home-page wall (Admin →
        # Home page reel) instead of hard-coding autoplay: browsers only allow a
        # muted autoplay, and some owners prefer click-to-play.
        play = feed_service.playback()
        attrs = ["id=\"hero-media\"", "controls", "playsinline",
                 f'preload="{ "auto" if play["autoplay"] else "metadata" }"']
        if play["autoplay"]:
            attrs.append("autoplay")
        if play["muted"]:
            attrs.append("muted")
        if play["loop"]:
            attrs.append("loop")
        if chosen.get("media_kind") == "photo":
            hero = (f'<img id="hero-media" data-stream="{esc(chosen.get("stream") or "")}" '
                    f'src="{esc(chosen.get("stream") or "")}" '
                    f'alt="{esc(chosen.get("name") or "")}">')
        else:
            hero = (f'<video {" ".join(attrs)} id="hero-media" '
                    f'data-stream="{esc(chosen.get("stream") or "")}" '
                    f'poster="{esc(chosen.get("thumb") or "")}" '
                    f'src="{esc(chosen.get("stream") or "")}"></video>')
        bot_link = chosen.get("bot_link") or ""
        tiles = []
        for row in items:
            active = " on" if row.get("key") == chosen.get("key") else ""
            tiles.append(
                f'<a class="tile{active}" href="/watch?item={esc(row.get("key") or "")}" '
                f'data-stream="{esc(row.get("stream") or "")}" '
                f'data-kind="{esc(row.get("media_kind") or "video")}" '
                f'data-thumb="{esc(row.get("thumb") or "")}">'
                f'<img loading="lazy" decoding="async" alt="{esc(row.get("name") or "")}" '
                f'src="{esc(row.get("thumb") or "")}" onerror="this.remove()">'
                f'<span class="play">{"🖼" if row.get("media_kind") == "photo" else "▶"}</span>'
                f'<span class="tag">{esc(row.get("name") or "")}</span></a>')
        body = f"""
        <div class="page-heading"><div><p class="eyebrow">WATCH · FREE COLLECTION</p>
          <h2>🎞 ভিডিও ও ছবি — এখানেই দেখুন</h2></div>
          {f'<a class="btn" href="{esc(bot_link)}" target="_blank" rel="noopener">🤖 বটে পূর্ণ কোয়ালিটি</a>' if bot_link else ''}
        </div>
        <div class="watch-hero">{hero}</div>
        <p class="note" id="hero-status" style="display:none"></p>
        <p class="muted" id="watch-title" style="margin-top:10px">{esc(chosen.get("name") or "")}
        {f'· {esc(chosen.get("store_name") or "")}' if chosen.get("store_name") else ''}</p>
        <div class="wall">{''.join(tiles)}</div>
        <script>
          document.querySelectorAll('.tile').forEach(function(tile){{
            tile.addEventListener('click', function(ev){{
              if (ev.metaKey || ev.ctrlKey) return;
              ev.preventDefault();
              var hero = document.getElementById('hero-media');
              var kind = tile.dataset.kind, stream = tile.dataset.stream;
              if (!hero || !stream) return;
              if (kind === 'photo') {{
                var img = document.createElement('img');
                img.id = 'hero-media'; img.src = stream; img.alt = '';
                hero.replaceWith(img);
              }} else if (hero.tagName === 'VIDEO') {{
                hero.src = stream; hero.poster = tile.dataset.thumb || '';
                window.bfixfPlay(hero);
              }} else {{
                var video = document.createElement('video');
                video.id = 'hero-media'; video.controls = true;
                video.muted = {str(play["muted"]).lower()};
                video.loop = {str(play["loop"]).lower()};
                video.playsInline = true;
                video.poster = tile.dataset.thumb || ''; video.src = stream;
                hero.replaceWith(video); window.bfixfPlay(video);
              }}
              var title = document.getElementById('watch-title');
              if (title) title.textContent = tile.querySelector('.tag').textContent;
              document.querySelectorAll('.tile').forEach(function(t){{ t.classList.remove('on'); }});
              tile.classList.add('on');
              window.history.replaceState(null, '',
                location.pathname + '?item=' + encodeURIComponent(tile.dataset.stream));
              fetch('reel/play/' + encodeURIComponent('file:' + stream.split('/')[1]),
                    {{keepalive:true, cache:'no-store'}}).catch(function(){{}});
              heroReady(stream);
            }});
          }});
          // If the hero cannot play, the site fetches the file from Telegram and
          // plays it — the visitor never sees a dead player.
          var heroStatus = document.getElementById('hero-status');
          function say(text) {{
            if (!heroStatus) return;
            heroStatus.style.display = text ? '' : 'none';
            heroStatus.textContent = text || '';
          }}
          function heroReady(stream) {{
            var hero = document.getElementById('hero-media');
            if (!hero) return;
            if (hero.tagName === 'IMG') {{
              hero.onerror = function(){{
                say('📥 ছবিটা টেলিগ্রাম থেকে আনছি…');
                window.bfixfPrepare(stream, function(j){{
                  if (j && j.ok) {{ hero.src = window.bfixfFresh(stream); say(''); }}
                  else say('😕 ' + ((j && j.reason) || 'ছবিটা আনা যাচ্ছে না'));
                }});
              }};
              return;
            }}
            hero.onerror = function(){{
              say('📥 ভিডিওটা টেলিগ্রাম থেকে আনছি… প্রথমবার একটু সময় লাগতে পারে');
              window.bfixfPrepare(stream, function(j){{
                if (j && j.ok) {{
                  hero.src = window.bfixfFresh(stream);
                  hero.load();
                  var p = window.bfixfPlay(hero); if (p && p.catch) p.catch(function(){{}});
                  say('');
                }} else {{
                  say('😕 ' + ((j && j.reason) || 'এই মুহূর্তে চালানো যাচ্ছে না'));
                }}
              }});
            }};
          }}
          heroReady({esc(chosen.get("stream") or "")!r}.replace(/^[/]/, ''));
        </script>
        """
        return layout("Watch", body.replace("</script>", "</script>" + _PLAYER_HELPER_JS, 1)
                      if _PLAYER_HELPER_JS not in body else body, "/watch")

    @app.get("/help", response_class=HTMLResponse)
    async def public_help() -> str:
        """A plain-language FAQ — the “users don't understand much” fix."""
        contact = (settings.get_str("SUPPORT_CONTACT") or "").strip()
        if contact.startswith(("http://", "https://", "tg://")):
            contact_link = f'<a class="btn" href="{esc(contact)}">📞 সাপোর্টে যোগাযোগ করুন</a>'
        elif contact.startswith("@"):
            contact_link = (f'<a class="btn" href="https://t.me/{esc(contact[1:])}">'
                            f'📞 সাপোর্টে যোগাযোগ করুন</a>')
        else:
            contact_link = ""
        bots = _bot_username()
        open_bot = f'<a class="btn" href="https://t.me/{bots}">🤖 বট খুলুন</a>' if bots else ""
        steps = """
        <ol class="steps">
          <li><b>বট খুলুন</b> — নিচের বাটনে চাপ দিলেই টেলিগ্রামে চলে যাবেন।</li>
          <li><b>স্টোর বেছে নিন</b> — 🏪 বাটনে চাপ দিলে সব স্টোর দেখতে পাবেন।</li>
          <li><b>ভিডিওর নামে চাপ দিন</b> — ভিডিও সাথে সাথে চ্যাটে চলে আসবে, আর কিছু করতে হবে না।</li>
        </ol>"""
        faq = """
        <details open><summary>ভিডিও পেতে কি টাকা লাগবে?</summary>
        <p>ফ্রি স্টোরের সব ভিডিও বিনা পয়সায়। 🔒 দিয়ে লেখা স্টোরগুলো প্রিমিয়াম —
        সেগুলোর জন্য বটের ভিতরে 💎 প্ল্যান থেকে কিনতে হয় (বিকাশ/নগদ/USDT)।</p></details>

        <details><summary>চ্যানেল জয়েন করতে বলছে কেন?</summary>
        <p>কিছু স্টোরে ভিডিওর আগে চ্যানেল জয়েন করা লাগে। একবার জয়েন করে
        “✅ আমি জয়েন করেছি” চাপ দিলেই ভিডিও চলে আসবে — বারবার লাগবে না।</p></details>

        <details><summary>“লিমিট শেষ” লিখছে — মানে কী?</summary>
        <p>কিছু লিংকে কতবার খোলা যাবে সেটা ঠিক করা থাকে (যেমন ১০০ বার)। সেটা শেষ হলে
        নতুন লিংকের জন্য সাপোর্টে যোগাযোগ করুন।</p></details>

        <details><summary>ভিডিও ডাউনলোড করলে কি সেভ হয়ে থাকবে?</summary>
        <p>হ্যাঁ। বট থেকে পাওয়া ভিডিও আপনার টেলিগ্রাম চ্যাটেই থাকে — পরে আবার দেখতে পারবেন,
        ইন্টারনেট ছাড়াও।</p></details>

        <details><summary>কিছু খুঁজে পাচ্ছি না</summary>
        <p>🔍 Search-এ নাম লিখে দেখুন, নাহলে বটে 🆘 Help বা সাপোর্ট বাটনে চাপ দিন —
        সরাসরি মেসেজ চলে যাবে।</p></details>
        """
        body = f"""
        <h2>❓ কীভাবে ব্যবহার করবেন</h2>
        {steps}
        <div class="row">{open_bot}{contact_link}</div>
        <h3>সাধারণ প্রশ্ন</h3>
        {faq}
        """
        return layout("Help", body, "/help")

    @app.get("/stores", response_class=HTMLResponse)
    async def public_stores() -> str:
        return layout("Stores", f"<h3>🏪 সব স্টোর</h3>{store_cards(db.all_stores())}", "/stores")

    @app.get("/search", response_class=HTMLResponse)
    async def public_search(request: Request, q: str = "") -> str:
        rows = []
        if q.strip():
            visitors.note(request, name="search", detail=q.strip()[:80])
            for store in db.all_stores():
                if store["is_premium"]:
                    continue
                for file_row in db.search_files(store["id"], q.strip().lower(), 30):
                    link = deep_link(f"f{file_row['id']}")
                    rows.append(
                        f"<tr><td>{esc(file_row['name'])}</td><td>{esc(store['name'])}</td>"
                        f"<td>{file_row['views']}</td>"
                        f"<td>{f'<a class=btn href={link}>পাঠান</a>' if link else ''}</td></tr>")
        table = ("<table><tr><th>Name</th><th>Store</th><th>Views</th><th></th></tr>"
                 f"{''.join(rows)}</table>") if rows else (
            '<p class="muted">কিছু পাওয়া যায়নি (প্রিমিয়াম স্টোরের কনটেন্ট নাম শুধু বটে দেখা যায়)।</p>')
        body = (f"<h3>🔍 সার্চ</h3>"
                f"<form method=get><input name=q placeholder='ফাইলের নাম লিখুন' value='{esc(q)}'></form>"
                f"{table}")
        return layout("Search", body, "/search")

    def _find_store(slug: str) -> dict | None:
        """Find a store from a URL slug — tolerant about -/_ and numeric ids."""
        candidates = [slug, slug.replace("-", "_"), slug.replace("_", "-")]
        for candidate in candidates:
            if not candidate:
                continue
            found = db.store_by_slug(candidate)
            if found:
                return found
            if candidate.isdigit():
                return db.store(int(candidate))
        return None

    @app.get("/s/{slug}", response_class=HTMLResponse)
    async def public_store(request: Request, slug: str) -> str:
        store = _find_store(slug)
        if store is None:
            return HTMLResponse(layout("Not found", "<h3>স্টোরটি পাওয়া যায়নি</h3>"), status_code=404)
        visitors.note(request, name="store_open", store_id=int(store["id"]),
                      detail=(store.get("name") or "")[:60])
        stats = db.store_stats(store["id"])
        link = deep_link(f"s{store['id']}")
        if store["is_premium"]:
            files_html = ('<p class="muted">🔒 এটি একটি প্রিমিয়াম স্টোর — ফাইলের তালিকা ও '
                          'ডাউনলোড বটের ভিতরে (অ্যাক্সেস কেনার পর)।</p>')
        else:
            rows_list = []
            for file_row in db.files_of(store["id"], newest_first=True)[:200]:
                send = deep_link(f"f{file_row['id']}")
                button = f'<a class="btn" href="{send}">পাঠান</a>' if send else ""
                rows_list.append(
                    f"<tr><td>{esc(file_row['name'])}</td><td>{esc(file_row['kind'])}</td>"
                    f"<td>{file_row['views']}</td><td>{button}</td></tr>")
            rows = "".join(rows_list)
            files_html = (f"<table><tr><th>Name</th><th>Type</th><th>Views</th><th></th></tr>{rows}</table>"
                          if rows else '<p class="muted">এই স্টোরে এখনো ফাইল নেই।</p>')
        cover = store_cover_url(store)
        cover_block = (f'<div class="store-cover" style="height:210px;margin:0 0 16px;'
                       f'border-radius:22px;background-image:url({cover})"></div>') if cover else ""
        body = f"""
        {cover_block}
        <h2>{esc(store['name'])} <span class="pill">{'🔒 Premium' if store['is_premium'] else '🆓 Free'}</span></h2>
        <p class="muted">{esc(store.get('description') or '')}</p>
        <div class="grid">{card("Files", stats['files'])}{card("Views", stats['views'])}{card("Members", stats['grants'])}</div>
        <p>{f'<a class="btn" href="{link}">▶️ বটে খুলুন</a>' if link else '<span class="muted">বট এখনো অনলাইন হয়নি</span>'}</p>
        {files_html}
        """
        return layout(store["name"], body, "/stores",
                      canonical_path=f"/s/{quote_plus(store['slug'])}")

    # ------------------------------------------------------------------- auth
    @app.get("/admin/login", response_class=HTMLResponse)
    async def login_open(request: Request):
        if _authed(request):
            return RedirectResponse("/admin", status_code=303)
        return await login_form()

    async def login_form(error: str = "") -> str:
        if error == "locked":
            note = '<div class="warnbox">অনেকবার ভুল পাসওয়ার্ড দেওয়া হয়েছে। কিছুক্ষণ পরে আবার চেষ্টা করুন।</div>'
        elif error:
            note = '<div class="warnbox">পাসওয়ার্ড মেলেনি। আবার চেষ্টা করুন।</div>'
        else:
            note = ""
        body = f"""
        {note}
        <div class="card" style="max-width:440px;margin:40px auto">
          <p class="eyebrow">ADMIN WORKSPACE</p>
          <h2>Sign in</h2>
          <p class="muted">আপনার dashboard-এ প্রবেশ করতে পাসওয়ার্ড দিন।</p>
          <form method=post action=/admin/login>
            <label>Admin password</label>
            <input name=password type=password inputmode=numeric autocomplete=current-password required autofocus>
            <button type=submit>Continue</button>
          </form>
        </div>"""
        return layout("Sign in", body)

    @app.post("/admin/login")
    async def login(request: Request, password: str = Form("")):
        source = _login_key(request)
        retry_after = _login_retry_after(source)
        if retry_after:
            return HTMLResponse(await login_form("locked"), status_code=429,
                                headers={"Retry-After": str(retry_after)})
        good_pass = _secure_equal(password or "", _panel_password())
        if not good_pass:
            _record_login_failure(source)
            return HTMLResponse(await login_form("1"), status_code=401)
        _LOGIN_FAILURES.pop(source, None)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            COOKIE_NAME, _session_token(), max_age=max(3600, cfg.WEB_SESSION_HOURS * 3600),
            httponly=True, secure=(request.url.scheme == "https" or
                                   request.headers.get("x-forwarded-proto", "").lower() == "https"),
            samesite="strict", path="/",
        )
        return response

    @app.get("/admin/logout")
    async def logout():
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    def _guard(request: Request):
        """Return a response when the request must be blocked, else None."""
        if _authed(request):
            return None
        if request.headers.get("Authorization"):
            return Response("Unauthorized", status_code=401)
        return RedirectResponse("/admin/login", status_code=303)

    # -------------------------------------------------------- Telegram session
    @app.get("/admin/session", response_class=HTMLResponse)
    async def admin_session(request: Request, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        admin_id = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        saved = db.session(admin_id) if admin_id else None
        online = runtime.is_online(admin_id) if admin_id else False
        pending = PENDING_SESSIONS.get(admin_id) if admin_id else None
        if pending and time.time() - pending.get("started_at", 0) > 600:
            PENDING_SESSIONS.pop(admin_id, None)
            try:
                await pending["client"].disconnect()
            except Exception:
                pass
            pending = None
            warn = warn or "লগইন কোডের সময় শেষ—নতুন কোড নিন।"

        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        if not admin_id:
            session_body = '<div class="warnbox">আগে ADMIN_IDS সেট করুন।</div>'
        elif pending and pending.get("step") == "password":
            session_body = f"""<div class="card">
              <h3>Two-step verification</h3>
              <p class="muted">Telegram আপনার account-এর 2FA password চেয়েছে। Password Telegram-এ যাচাই হবে;
              আমরা এটি database-এ রাখি না।</p>
              <form method=post action="/admin/session/password">
                <label>Telegram 2FA password</label>
                <input name=password type=password autocomplete=current-password required>
                <div class="row"><button class="ok">Verify and connect</button>
                  <button class="grey" formaction="/admin/session/cancel" formmethod=post>Cancel</button></div>
              </form></div>"""
        elif pending and pending.get("step") == "code":
            session_body = f"""<div class="card">
              <h3>Enter Telegram login code</h3>
              <p class="muted">Telegram app/SMS-এ পাঠানো কোডটি দিন। কোডটি শুধু এই login attempt-এর জন্য ব্যবহার হয়।</p>
              <form method=post action="/admin/session/verify">
                <label>Login code</label><input name=code inputmode=numeric autocomplete=one-time-code required>
                <div class="row"><button class="ok">Verify session</button>
                  <button class="grey" formaction="/admin/session/cancel" formmethod=post>Cancel</button></div>
              </form></div>"""
        else:
            owner = runtime.session_owners.get(admin_id) or (saved or {}).get("owner_id")
            owner_name = (saved or {}).get("owner_name") or ""
            if online:
                status = f"Connected as {esc(owner_name or 'Telegram account')} · ID {esc(str(owner or '—'))}"
            elif saved:
                status = f"Saved session: {esc(owner_name or 'Telegram account')} · reconnecting"
            else:
                status = "No active Telegram session"
            disconnect_form = (
                '<form method=post action="/admin/session/disconnect" '
                'onsubmit="return confirm(\'Session disconnect করবেন?\')">'
                '<button class="bad">Disconnect saved session</button></form>' if saved else "")
            session_body = f"""<div class="card">
              <div class="row" style="justify-content:space-between">
                <div><p class="eyebrow">ACCOUNT CONNECTION</p><h3 style="margin:0">{status}</h3>
                  <p class="muted">Sessionটি private source channel scan, file mirror এবং store channel তৈরিতে ব্যবহৃত হয়।</p></div>
                <span class="pill">{'Online' if online else ('Saved · reconnecting' if saved else 'Not connected')}</span>
              </div>
              <form method=post action="/admin/session/start">
                <label>Telegram phone number (international format)</label>
                <input name=phone type=tel placeholder="+8801XXXXXXXXX" autocomplete=tel required>
                <button class="ok">📲 Send login code</button>
              </form>
              {disconnect_form}
            </div>
            <div class="card" style="margin-top:14px"><b>Secure sign-in</b>
              <p class="muted">আপনার login code এই server-এ পাঠাবেন না—শুধু এই secure page-এ লিখুন।
              Authorized হলে sessionটি SQLite database-এ থাকবে; চাইলে যেকোনো সময় Disconnect করুন।</p>
              <p class="muted">API ID / API Hash দরকার হলে <a href="/setup">Bot connection</a> থেকে একবার সেট করুন।</p>
            </div>"""

        body = f"""{note}{warnbox}
        <div class="page-heading"><div><p class="eyebrow">TELEGRAM</p><h2>Session connection</h2>
          <p class="muted">Channel ও private content কাজ করাতে নিজের account একবার authorize করুন।</p></div></div>
        {session_body}"""
        return layout("Telegram session", body, "/admin/session")

    @app.post("/admin/session/start")
    async def admin_session_start(request: Request, phone: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        admin_id = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        phone = re.sub(r"[\s().-]", "", (phone or "").strip())
        if not admin_id or not cfg.API_ID or not cfg.API_HASH:
            return RedirectResponse("/admin/session?warn=API+ID/Hash+ও+ADMIN_IDS+আগে+সেট+করুন", status_code=303)
        if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
            return RedirectResponse("/admin/session?warn=দেশের+কোডসহ+সঠিক+ফোন+নম্বর+দিন", status_code=303)
        previous = PENDING_SESSIONS.pop(admin_id, None)
        if previous:
            try:
                await previous["client"].disconnect()
            except Exception:
                pass
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            client = TelegramClient(StringSession(), cfg.API_ID, cfg.API_HASH)
            await client.connect()
            sent = await client.send_code_request(phone)
            PENDING_SESSIONS[admin_id] = {
                "client": client,
                "phone": phone,
                "phone_code_hash": getattr(sent, "phone_code_hash", ""),
                "step": "code",
                "started_at": time.time(),
            }
            return RedirectResponse("/admin/session?flash=Telegram+login+code+পাঠানো+হয়েছে", status_code=303)
        except Exception as exc:
            log.warning("Telegram website login code request failed: %s", type(exc).__name__)
            return RedirectResponse("/admin/session?warn=Telegram+code+পাঠানো+যায়নি—নম্বর+ও+API+তথ্য+যাচাই+করুন", status_code=303)

    async def _complete_web_session(admin_id: int, pending: dict) -> str:
        client = pending["client"]
        me = await client.get_me()
        session_string = client.session.save()
        old = runtime.user_clients.get(admin_id)
        if old is not None and old is not client:
            try:
                await old.disconnect()
            except Exception:
                pass
        db.save_session(admin_id, session_string, me.id, me.first_name or "")
        runtime.register_client(admin_id, client, me.id, me.first_name or "")
        PENDING_SESSIONS.pop(admin_id, None)
        log.info("Telegram user session connected from website for admin %s", admin_id)
        audit_service.record("session_start", user_id=admin_id, ref=str(me.id),
                             detail=me.first_name or "")
        return me.first_name or "Telegram account"

    @app.post("/admin/session/verify")
    async def admin_session_verify(request: Request, code: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        admin_id = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        pending = PENDING_SESSIONS.get(admin_id)
        if not pending or pending.get("step") != "code":
            return RedirectResponse("/admin/session?warn=লগইন+চেষ্টা+পাওয়া+যায়নি—নতুন+কোড+নিন", status_code=303)
        if time.time() - pending.get("started_at", 0) > 600:
            try:
                await pending["client"].disconnect()
            except Exception:
                pass
            PENDING_SESSIONS.pop(admin_id, None)
            return RedirectResponse("/admin/session?warn=কোডের+মেয়াদ+শেষ—নতুন+কোড+নিন", status_code=303)
        try:
            from telethon.errors import SessionPasswordNeededError
            await pending["client"].sign_in(
                phone=pending["phone"], code=(code or "").strip(),
                phone_code_hash=pending["phone_code_hash"],
            )
            name = await _complete_web_session(admin_id, pending)
            return RedirectResponse("/admin/session?flash=Session+connected:+" + quote_plus(name), status_code=303)
        except SessionPasswordNeededError:
            pending["step"] = "password"
            return RedirectResponse("/admin/session", status_code=303)
        except Exception as exc:
            log.warning("Telegram website login code verification failed: %s", type(exc).__name__)
            return RedirectResponse("/admin/session?warn=কোড+সঠিক+নয়+বা+মেয়াদ+শেষ—আবার+চেষ্টা+করুন", status_code=303)

    @app.post("/admin/session/password")
    async def admin_session_password(request: Request, password: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        admin_id = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        pending = PENDING_SESSIONS.get(admin_id)
        if not pending or pending.get("step") != "password":
            return RedirectResponse("/admin/session?warn=2FA+login+চেষ্টা+পাওয়া+যায়নি", status_code=303)
        try:
            await pending["client"].sign_in(password=password)
            name = await _complete_web_session(admin_id, pending)
            return RedirectResponse("/admin/session?flash=Session+connected:+" + quote_plus(name), status_code=303)
        except Exception as exc:
            log.warning("Telegram website 2FA verification failed: %s", type(exc).__name__)
            return RedirectResponse("/admin/session?warn=2FA+password+যাচাই+হয়নি—আবার+চেষ্টা+করুন", status_code=303)

    @app.post("/admin/session/cancel")
    async def admin_session_cancel(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        admin_id = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        pending = PENDING_SESSIONS.pop(admin_id, None)
        if pending:
            try:
                await pending["client"].disconnect()
            except Exception:
                pass
        return RedirectResponse("/admin/session?flash=Login+cancelled", status_code=303)

    @app.post("/admin/session/disconnect")
    async def admin_session_disconnect(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        admin_id = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        client = runtime.user_clients.get(admin_id)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        runtime.unregister_client(admin_id)
        db.delete_session(admin_id)
        return RedirectResponse("/admin/session?flash=Telegram+session+disconnected", status_code=303)

    # -------------------------------------------------------------- dashboard
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_home(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        stats = db.stats()
        revenue = db.revenue()
        today = datetime.now().strftime("%Y-%m-%d")
        recent = db.campaigns(limit=5)
        campaign_rows = "".join(
            f"<tr><td>#{c['id']}</td><td>{esc(c['title'])}</td>"
            f"<td>{esc(broadcast.audience_label(c['audience']))}</td>"
            f"<td>{c['status']}</td><td>{c['sent']}/{c['total']}</td>"
            f"<td><a class='btn grey' href='/admin/broadcast/{c['id']}'>খুলুন</a></td></tr>"
            for c in recent) or "<tr><td colspan=6 class='muted'>এখনো ব্রডকাস্ট নেই</td></tr>"
        bot_state = ("🟢 চলছে" if runtime.bot_online() else "🔴 বট অফলাইন "
                     "(ব্রডকাস্ট কিউতে থাকবে)")
        # “Why did only half the audience get my broadcast?” — answered before it is
        # asked, with the one number that explains it (access hashes we hold).
        try:
            from app.services import peers as peers_service
            reach = peers_service.stats()
        except Exception:
            reach = {"total": 0, "reachable": 0}
        reach_note = ""
        if reach.get("total"):
            missing = max(0, int(reach["total"]) - int(reach["reachable"]))
            if missing:
                reach_note = (
                    f'<div class="warnbox">📡 <b>{reach["reachable"]}/{reach["total"]}</b> ইউজারের '
                    f'কাছে ব্রডকাস্ট পৌঁছাবে — {missing} জন এখনো বটে /start দেয়নি, তাই Telegram '
                    f'их অ্যাক্সেস হ্যাশ দেয় না। <a href="/admin/broadcast">ব্রডকাস্ট স্টুডিওতে</a> '
                    f'প্রতি-ইউজার কারণ দেখা যায়; /start দিলে তারা নিজে থেকেই তালিকায় যোগ হবে।'
                    f'</div>')
            else:
                reach_note = (f'<div class="flash">📡 সব {reach["total"]} ইউজারের কাছে '
                              f'ব্রডকাস্ট পৌঁছাবে ✅</div>')
        cache_warning = ""
        if settings.get_str("STORAGE_CHANNEL_WARNING", ""):
            cache_warning = ui.note(
                f"🎞 <b>ক্যাশ চ্যানেল সমস্যা</b> — "
                f"{esc(settings.get_str('STORAGE_CHANNEL_WARNING'))}<br>"
                f"<a href='/admin/channels'>📡 চ্যানেল পেজে ঠিক করুন</a> · "
                f"বটে <code>/storageset probe</code> চাপলেও দেখা যাবে।", "bad")
        try:
            recent_actions = audit_service.recent(limit=6)
        except Exception:
            recent_actions = []
        activity_block = ""
        if recent_actions:
            activity_block = (
                "<h3>🕒 এইমাত্র যা হলো</h3>"
                + ui.timeline([(fmt_ts(row.get("ts"), "%d %b · %H:%M"),
                                esc(audit_service.label(row.get("name")))
                                + (f" <span class='muted'>{esc(str(row.get('detail') or ''))[:70]}</span>"
                                   if row.get("detail") else ""))
                               for row in recent_actions])
                + '<p class="muted"><a href="/admin/audit">সম্পূর্ণ '
                  'অ্যাকশন লগ দেখুন →</a></p>')
        # ---- design system pieces (web/adminui.py) -------------------------
        currency = settings.get_str("CURRENCY", "৳")
        brand = settings.get_str("WEB_TITLE", "Store Bot") or "Store Bot"
        try:
            live_visitors = visitors.live(5)
        except Exception:                             # analytics must never block a page
            live_visitors = {"online": 0, "views": 0}
        kpis = ui.kpi_row(
            ui.kpi("ইউজার", stats["users"], sub=f"গ্রান্ট <b>{stats['grants']}</b>"),
            ui.kpi("ভিডিও/ফাইল", stats["files"], sub=f"স্টোর <b>{stats['stores']}</b>"),
            ui.kpi("ভিউ", stats["views"], sub=f"টিকিট <b>{stats['tickets_open']}</b>"),
            ui.kpi("আয়", money(revenue["total"], currency),
                   sub=f"⏳ পেন্ডিং <b>{revenue['pending']}</b>"),
            ui.kpi("👣 এখন অনলাইন", live_visitors.get("online", 0),
                   sub=f"৫ মিনিটে <b>{live_visitors.get('views', 0)}</b> পেজ · "
                       "<a href='/admin/visitors'>লাইভ দেখুন</a>",
                   dark=bool(live_visitors.get("online"))),
            ui.kpi("আজকের পাঠানো", broadcast_today(stats),
                   sub=f"বট <b>{'অনলাইন' if runtime.bot_online() else 'অফলাইন'}</b>"),
        )
        bot_note = ui.note(
            f"বট <b>{'অনলাইনে চলছে' if runtime.bot_online() else 'এখন অফলাইন'}</b> — "
            + ("ব্রডকাস্ট येখনই যাবে, ফাইল সাথে সাথে পৌঁছাবে।" if runtime.bot_online()
               else "যা পাঠাবেন সব কিউতে থাকবে; <code>/setup</code> বা "
                    "<a href='/admin/session'>Telegram session</a> দেখে ঠিক করুন।"),
            "" if runtime.bot_online() else "warn",
        )
        # `ADMIN_QUICK` labels carry their own markup (`Videos &amp; files`), so they
        # are used as author-written HTML — escaping them twice shows “&amp;amp;”.
        quick = "".join(
            f'<a href="{href}"><span class="qi">{icon}</span><span><b>{label}</b>'
            f'<span>{esc(note)}</span></span></a>' for icon, label, href, note in ADMIN_QUICK)
        campaign_rows = [
            [f"#{c['id']}", esc(c["title"]),
             esc(broadcast.audience_label(c["audience"])),
             status_tag(c["status"]), f"{c['sent']}/{c['total']}",
             f"<a class='btn grey' href='/admin/broadcast/{c['id']}'>খুলুন</a>"]
            for c in recent]
        body = f"""
        {ui.page_head("Welcome back",
                      f"{brand} · আজ {today} — কনটেন্ট, অডিয়েন্স আর ব্রডকাস্ট সব এক জায়গায়।",
                      eyebrow="OVERVIEW",
                      actions=("<a class='btn' href='/admin/broadcast'>📣 নতুন ব্রডকাস্ট</a>"
                               "<a class='btn grey' href='/admin/files'>▤ ফাইল</a>"
                               "<a class='btn grey' href='/admin/self-test'>✓ সিস্টেম চেক</a>"
                               "<a class='btn grey' href='/admin/emergency'>⚠ জরুরি</a>"))}
        {bot_note}
        {reach_note}
        {cache_warning}
        {kpis}
        <h3>⚡ দ্রুত কাজ</h3>
        <div class="quick">{quick}</div>
        <h3>📣 সর্বশেষ ব্রডকাস্ট</h3>
        {ui.table(["#", "ক্যাম্পেইন", "অডিয়েন্স", "অবস্থা", "পাঠানো", ""],
                  campaign_rows, empty_row="এখনো কোনো ব্রডকাস্ট নেই",
                  numeric=(4,))}
        {activity_block}"""
        return layout("Dashboard", body, "/admin")

    # ------------------------------------------------------- broadcast studio
    def _files_options(selected: int = 0) -> str:
        options = ['<option value="0">— কোনো ফাইল ছাড়া (শুধু টেক্সট) —</option>']
        for store in db.all_stores():
            for file_row in db.files_of(store["id"], newest_first=True)[:200]:
                mark = " selected" if file_row["id"] == selected else ""
                options.append(
                    f'<option value="{file_row["id"]}"{mark}>'
                    f'{esc(store["name"])} · {esc(file_row["name"][:60])}</option>')
        return "".join(options)

    def _media_options() -> str:
        """Videos/photos the owner forwarded to the bot (`/bm`) — not store files."""
        items = db.bc_media_items(limit=40)
        if not items:
            return ('<option value="">— ইনবক্স খালি: বটে /bm দিয়ে ভিডিও পাঠান —</option>')
        return "".join(
            f'<option value="{item["id"]}">'
            f'{"🖼" if item["kind"] == "photo" else "🎬"} '
            f'{esc((item.get("name") or "media")[:60])} · #{item["id"]}</option>'
            for item in items)

    def _audience_options() -> str:
        rows = ['<option value="all">📣 All users</option>',
                '<option value="premium">💎 Premium buyers</option>',
                '<option value="free">🆓 Free users</option>']
        for store in db.all_stores():
            for kind, label in (("store", "সবাই"), ("sub", "সাবস্ক্রাইবার"), ("nosale", "কখনো কেনেনি")):
                rows.append(f'<option value="{kind}:{store["id"]}">🏪 {esc(store["name"])} · {label}</option>')
        return "".join(rows)

    def _campaign_table(campaigns: list[dict]) -> str:
        rows = []
        for campaign in campaigns:
            counts = db.campaign_counts(campaign["id"], sync=False)
            rows.append(
                f"<tr><td>#{campaign['id']}</td>"
                f"<td>{esc(campaign['title'] or '')}<div class='muted'>{esc((campaign['text'] or '')[:70])}</div></td>"
                f"<td>{esc(broadcast.audience_label(campaign['audience']))}</td>"
                f"<td><span class='pill'>{campaign['status']}</span></td>"
                f"<td>✅ {counts['sent']} · ⚠️ {counts['failed']} · 🚫 {counts['blocked']}<div class='muted'>🕓 {counts['pending']}/{counts['total']}</div></td>"
                f"<td>{_start(campaign['created_at'])}</td>"
                f"<td><a class='btn grey' href='/admin/broadcast/{campaign['id']}'>খুলুন</a></td></tr>")
        return ("<table><tr><th>#</th><th>Message</th><th>Audience</th><th>Status</th>"
                "<th>Progress</th><th>Created</th><th></th></tr>"
                f"{''.join(rows)}</table>") if rows else "<p class='muted'>এখনো কোনো ক্যাম্পেইন নেই।</p>"

    @app.get("/admin/broadcast", response_class=HTMLResponse)
    async def studio(request: Request, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        bot_ready = runtime.bot_online()
        queue_warning = ("" if bot_ready else
                         '<div class="warnbox">বট সংযুক্ত নয়—এখন পাঠালে campaign শুধু কিউতে থাকবে। '
                         'Telegram bot worker চালু করতে <a href="/setup">Bot connection</a> দেখুন; '
                         'production start command হিসেবে <code>python run.py</code> ব্যবহার করুন।</div>')
        body = f"""
        {note}{warnbox}{queue_warning}
        {ui.page_head("ব্রডকাস্ট স্টুডিও",
                      "অডিয়েন্স বাছুন, মেসেজ লিখুন বা মিডিয়া দিন — কিউ ডেটাবেজে থাকে, "
                      "তাই বট রিস্টার্ট হলেও ব্রডকাস্ট থেমে যায় না। এক মেসেজ বা "
                      "বহু-মেসেজ — দুটোই এখান থেকে।",
                      eyebrow="BROADCAST",
                      actions=('<a class="btn grey" href="/admin/reel">🎞 রিল</a>'
                               '<a class="btn grey" href="/admin/users">👥 ইউজার</a>'
                               '<a class="btn grey" href="/admin/download">⇩ রিপোর্ট/এক্সপোর্ট</a>'))}
        <div class="card">
          <form method=post action="/admin/broadcast/new">
            <label>Audience</label>
            <select name=audience>{_audience_options()}</select>
            <label>Attach a file / video (optional)</label>
            <select name=file_id>{_files_options()}</select>
            <label>ব্রডকাস্ট মিডিয়া ইনবক্স থেকে (একাধিক বাছা যায় — অ্যালবাম হবে)</label>
            <select name=media_ids multiple size=5>{_media_options()}</select>
            <p class="muted">বটে <code>/bm</code> দিয়ে যেকোনো ভিডিও/ছবি ফরওয়ার্ড করলে সেটা
            এখানে আসে — স্টোরে যোগ করা লাগে না।</p>
            <label>📤 পাঠানোর ধরন</label>
            <select name=mode>
              <option value="single">এক মেসেজ — টেক্সট + অ্যালবাম (আগের মতো)</option>
              <option value="multi">বহু-মেসেজ — প্রতিটা আলাদা মেসেজ, ক্রম অনুযায়ী</option>
            </select>
            <p class="muted">“বহু-মেসেজ” দিলে উপরের টেক্সটটা <b>প্রথম মেসেজ</b> হয়, তারপর
            বাছা মিডিয়াগুলো একটার পর একটা আলাদা মেসেজ হয়ে যায় (বটের <code>/br</code>
            স্টুডিওর মতোই)। বাটন শেষ মেসেজে বসে — <code>/admin/settings</code> →
            “বাটন কোন মেসেজে বসবে” দিয়ে বদলানো যায়।</p>
            <label>প্রতিটা মিডিয়ার ক্যাপশন (ঐচ্ছিক — প্রতি লাইনে একটি, ক্রম অনুযায়ী)</label>
            <textarea name=captions rows=3 placeholder="১ম মিডিয়ার ক্যাপশন&#10;২য় মিডিয়ার ক্যাপশন"></textarea>
            <label>Message (HTML allowed, <code>{{name}}</code> = user's name)</label>
            <textarea name=text rows=6 placeholder="আজকের নতুন ভিডিও এসেছে 🎬"></textarea>
            <label>ইনলাইন বাটন — প্রতি লাইনে <code>লেবেল | লিংক</code>
                   (একই লাইনে <code>&amp;&amp;</code> দিলে পাশাপাশি)</label>
            <textarea name=buttons rows=3>{settings.get_str('BROADCAST_DEFAULT_BUTTONS') or "📢 জয়েন | https://t.me/mychannel"}</textarea>
            <div class="chk"><input type=checkbox name=online value=1 checked id=ob>
              <label for=ob>মেসেজের নিচে “🟢 অনলাইন — স্টোর খুলুন” বাটন দিন</label></div>
            <label>🐢 ধীর মোড — পুরো তালিকা কত মিনিট ধরে পাঠাবে? (০ = স্বাভাবিক গতি)</label>
            <input name=slow_minutes type=number min=0 max=720 value="0" step="1">
            <p class="muted">Telegram-এর সীমা ~৩০ মেসেজ/সেকেন্ড; বিশাল তালিকায় ধীর মোড
            নিরাপদ (৫০০০ জন → ৩০ মিনিট দিলে প্রতি মেসেজ ~০.৩৬ সেকেন্ড পরে যায়)।</p>
            <div class="row">
              <button class="ok" name=action value=start>🚀 তৈরি করে শুরু করুন</button>
              <button class="grey" name=action value=draft>📝 খসড়া রাখুন</button>
              <button class="warn" name=action value=test>🧪 আমাকে টেস্ট পাঠান</button>
            </div>
            <p class="muted">অডিয়েন্স সাইজ এখন: সব {len(broadcast.resolve_audience(0, 'all'))} জন ·
            প্রিমিয়াম {len(broadcast.resolve_audience(0, 'premium'))} জন ·
            ফ্রি {len(broadcast.resolve_audience(0, 'free'))} জন</p>
          </form>
        </div>
        <p class="row">
          <a class="btn grey" href="/admin/links">🔗 লিমিটেড লিংক বানান</a>
          <a class="btn grey" href="/admin/channels/compose">📡 চ্যানেলে পোস্ট</a>
          <a class="btn warn" href="/admin/digest">🆕 সাপ্তাহিক ডাইজেস্ট এখনই পাঠান</a>
        </p>
        <h3>📜 ক্যাম্পেইন</h3>
        {_campaign_table(db.campaigns(limit=40))}
        """
        return layout("Broadcast", body, "/admin/broadcast")

    @app.post("/admin/broadcast/new")
    async def studio_create(request: Request, audience: str = Form("all"),
                            file_id: int = Form(0), text: str = Form(""),
                            action: str = Form("start"), buttons: str = Form(""),
                            online: str = Form(""),
                            slow_minutes: float = Form(0),
                            mode: str = Form("single"), captions: str = Form(""),
                            media_ids: list[int] = Form(default=[])):
        blocked = _guard(request)
        if blocked:
            return blocked
        action = (action or "start").strip().lower()
        if action not in {"start", "draft", "test"}:
            return RedirectResponse("/admin/broadcast?warn=অজানা+ক্যাম্পেইন+অ্যাকশন", status_code=303)
        body = (text or "").strip()
        files = [file_id] if file_id else []
        media = []
        for item_id in (media_ids or [])[:10]:
            row = db.bc_media_item(int(item_id))
            if row:
                media.append({"chat_id": row["chat_id"], "msg_id": row["msg_id"],
                              "kind": row.get("kind") or "video",
                              "name": row.get("name") or ""})
                db.touch_bc_media(int(item_id))
        if not body and not files and not media:
            return RedirectResponse("/admin/broadcast?warn=মেসেজ+বা+ফাইল+কিছুই+দিননি",
                                    status_code=303)
        targets = broadcast.resolve_audience(0, audience)
        if not targets:
            return RedirectResponse("/admin/broadcast?warn=এই+অডিয়েন্সে+কেউ+নেই", status_code=303)
        owner = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        # “বহু-মেসেজ” mode: the text becomes message #1 and every chosen media item
        # becomes its own message (the same model as the bot's `/br` studio and the
        # standalone broadcast bot). Captions line up with the media, in order.
        draft_items: list[dict] = []
        if (mode or "").strip().lower() == "multi":
            if body:
                draft_items.append({"kind": "text", "text": body})
            caption_lines = [line.strip() for line in (captions or "").splitlines()]
            # The single "attach a file" select is a message of its own in this mode,
            # otherwise a store file would be silently dropped.
            for file_row in files:
                row = db.file(int(file_row)) or {}
                draft_items.append({
                    "kind": (row.get("kind") or "document").lower(),
                    "file_id": int(file_row),
                    "text": caption_lines[0] if caption_lines else "",
                    "name": row.get("name") or "",
                })
            for index, item in enumerate(media):
                at = index + (1 if files else 0)
                draft_items.append({
                    "kind": (item.get("kind") or "video"),
                    "media_key": f"{int(item.get('chat_id') or 0)}:{int(item.get('msg_id') or 0)}",
                    "text": caption_lines[at] if at < len(caption_lines) else "",
                    "name": item.get("name") or "",
                })
            if not draft_items:
                return RedirectResponse(
                    "/admin/broadcast?warn=বহু-মেসেজ+মোডে+অন্তত+একটা+মেসেজ+বা+মিডিয়া+দিন",
                    status_code=303)
        # Tests and drafts are always non-sendable until an admin explicitly starts them.
        # Slow mode: spread the audience over N minutes (Telegram's own advice for
        # big lists — the global ceiling is ~30 messages/second).
        pause = (quickcast_service.slow_pause(len(targets), slow_minutes)
                 if slow_minutes and slow_minutes > 0 else 0.0)
        campaign = broadcast.create_campaign(
            owner, text=body if not draft_items else "", audience=audience,
            files=files if not draft_items else [], start=(action == "start"),
            buttons=buttons, media=media if not draft_items else [],
            origin="panel", pause_seconds=pause,
            online_button=1 if online else 0,
            title=((re.sub(r"<[^>]+>", "", body).strip().splitlines() or [""])[0][:40]
                   if draft_items else ""))
        if draft_items:
            db.set_campaign_groups(campaign["id"], draft_items)
        campaign_id = campaign["id"]
        if action == "test":
            if not cfg.ADMIN_IDS:
                return RedirectResponse("/admin/broadcast?warn=ADMIN_IDS+সেট+নেই,+টেস্ট+পাঠানো+যাবে+না",
                                        status_code=303)
            if not runtime.bot_online():
                return RedirectResponse(
                    "/admin/broadcast?warn=বট+অফলাইন+—+টেস্ট+পাঠানো+যাবে+না", status_code=303)
            outcome = await broadcast.test_send(campaign_id, [cfg.ADMIN_IDS[0]])
            if not outcome.get("ok"):
                detail = (outcome.get("errors") or ["unknown"])[0]
                return RedirectResponse(
                    f"/admin/broadcast/{campaign_id}?warn=টেস্ট+ব্যর্থ:+{esc(detail)}"[:400],
                    status_code=303)
            return RedirectResponse(
                f"/admin/broadcast/{campaign_id}?flash=🧪+টেস্ট+সফল+(#{campaign_id})",
                status_code=303)
        if action == "draft":
            return RedirectResponse(
                f"/admin/broadcast?flash=📝+খসড়া+সেভ+(#{campaign_id})", status_code=303)
        # start: only if the bot is online in this process, otherwise it stays queued
        if runtime.bot_online():
            broadcast.start_campaign(campaign_id)
            return RedirectResponse(
                f"/admin/broadcast/{campaign_id}?flash=🚀+শুরু+হয়েছে", status_code=303)
        return RedirectResponse(
            f"/admin/broadcast/{campaign_id}?warn=বট+অফলাইন+—+অনলাইন+হলেই+অটো+শুরু+হবে",
            status_code=303)

    @app.get("/admin/broadcast/{campaign_id}", response_class=HTMLResponse)
    async def studio_detail(request: Request, campaign_id: int, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        campaign = db.campaign(campaign_id)
        if campaign is None:
            return RedirectResponse("/admin/broadcast?warn=ক্যাম্পেইন+নেই", status_code=303)
        counts = db.campaign_counts(campaign_id)
        progress = broadcast.campaign_progress(campaign_id)
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        if campaign["status"] == broadcast.RUNNING and progress.get("flood_wait"):
            queue_diagnostic = f"Telegram rate limit: {progress['flood_wait']} — worker অপেক্ষা করছে।"
        elif campaign["status"] == broadcast.QUEUED and not progress["bot_online"]:
            queue_diagnostic = "বট অফলাইন। কিউ সেভ আছে; Telegram bot online হলেই পাঠানো শুরু হবে।"
        elif campaign["status"] == broadcast.QUEUED:
            queue_diagnostic = "বট অনলাইন—worker কিউ নিচ্ছে। কয়েক সেকেন্ড পরেও queued থাকলে System health/Activity logs দেখুন।"
        elif campaign["status"] == broadcast.RUNNING and not progress["running"]:
            resume_note = "Auto-resume বন্ধ" if not progress["auto_resume"] else "Auto-resume চালু"
            queue_diagnostic = (f"ক্যাম্পেইন running দেখাচ্ছে কিন্তু active worker নেই ({resume_note})। "
                                "Send now চাপলে pending item থেকে আবার শুরু হবে।")
        elif campaign["status"] == broadcast.FAILED:
            queue_diagnostic = campaign.get("last_error") or "Worker থেমে গেছে; error নিচে কিউ-তে দেখুন।"
        else:
            queue_diagnostic = campaign.get("last_error") or "ক্যাম্পেইনের অবস্থা স্বয়ংক্রিয়ভাবে আপডেট হবে।"
        items = db.campaign_items(campaign_id, limit=200)
        sample_rows = []
        for item in items[:60]:
            user = db.user(item["user_id"]) or {}
            who = esc(user.get("name") or "") or "—"
            if user.get("username"):
                who += f" <span class='muted'>@{esc(user['username'])}</span>"
            flag = {"sent": "✅", "pending": "⏳", "blocked": "🚫",
                    "failed": "⚠️"}.get(item["status"], "•")
            note = broadcast.explain_error(item["error"]) if item["error"] else ""
            sample_rows.append(
                f"<tr><td><a href='/admin/users/{item['user_id']}'>{item['user_id']}</a><br>"
                f"<span class='muted'>{who}</span></td>"
                f"<td>{flag} {esc(item['status'])}</td><td class='muted'>{esc(note)}</td></tr>")
        sample = "".join(sample_rows)

        # “Why did only the test message arrive?” — answered right on this page.
        reasons = broadcast.failure_breakdown(campaign_id)
        reason_html = "".join(
            f"<li><b>{esc(group['hint'])}</b> — {group['count']} জন "
            f"<span class='muted'>({', '.join(str(u) for u in group['users'])}…)</span></li>"
            for group in reasons)
        why = (f"<div class='card'><h4>❓ কেন সবাই পায়নি</h4><ul>{reason_html}</ul>"
               f"<p class='muted'>একই কারণে আটকে থাকা সবাইকে এক ক্লিকে আবার পাঠাতে "
               f"🔁 Retry failed চাপুন। ইউজার বটকে ব্লক করলে সেটা টেলিগ্রামের নিয়ম — "
               f"শুধু বট-ই মেসেজ পাঠাতে পারে না, কোনো উপায় নেই।</p></div>") if reasons else ""
        # Multi-message drafts (ported from `broadcast.py`): the campaign is a list
        # of messages, so show that list — what goes out, in which order.
        plan = db.campaign_groups(campaign_id)
        items_preview = ""
        if plan:
            kind_icon = {"text": "💬", "photo": "🖼", "video": "🎬", "animation": "🎞",
                         "document": "📄", "audio": "🎵", "voice": "🎤"}
            rows = "".join(
                f"<tr><td class='muted'>{index}</td>"
                f"<td>{kind_icon.get(row.get('kind') or 'text', '📦')} "
                f"{esc(row.get('kind') or 'text')}</td>"
                f"<td>{esc(re.sub(r'<[^>]+>', '', row.get('text') or '')[:160]) or '<span class=muted>—</span>'}</td>"
                f"<td class='muted'>{esc(row.get('media_key') or '')}</td></tr>"
                for index, row in enumerate(plan, start=1))
            items_preview = (f'<div class="card"><h4>🧵 বহু-মেসেজ খসড়া — {len(plan)}টি '
                             f'আলাদা মেসেজ (এই ক্রমেই যাবে)</h4>'
                             f'<table><tr><th>#</th><th>ধরন</th><th>টেক্সট/ক্যাপশন</th>'
                             f'<th>মিডিয়া সোর্স</th></tr>{rows}</table></div>')
        attached = db.campaign_media(campaign_id)
        media_preview = ""
        if attached:
            tiles = "".join(_campaign_media_tile(item) for item in attached)
            media_preview = (f'<div class="card"><h4>সংযুক্ত মিডিয়া ({len(attached)}টি) — '
                             f'একসাথে অ্যালবাম হয়ে যাবে</h4><div class="wall">{tiles}</div></div>')
        # The share link used to be shown once, in a flash message, and then lost.
        # Keep it on the page (and easy to copy) for as long as it exists.
        share_tokens = db.campaign_links(campaign_id, kind="draft")
        share_card = ""
        if share_tokens:
            links = "".join(
                f"<li><code>/draft/{esc(row['token'])}</code> — "
                f"{int(row.get('clicks') or 0)} বার খোলা হয়েছে "
                f"<a class='btn grey' href='/draft/{esc(row['token'])}' target='_blank'>↗ খুলুন</a></li>"
                for row in share_tokens)
            share_card = (f"<div class='card'><h4>🔗 খসড়ার পাবলিক লিংক "
                          f"({len(share_tokens)}টি)</h4><ul class='share-links'>{links}</ul>"
                          f"<p class='muted'>লিংকটা যে কেউ খুলতে পারে — শুধু পড়ার জন্য, "
                          f"কোনো অ্যাডমিন তথ্য বা বাটন সেখানে যায় না।</p></div>")
        missing_media = [fid for fid in (campaign.get("file_ids") or [])
                         if not (db.file(fid) or {}).get("mirror_msg")]
        cache_warn = (f"<div class='warnbox'>🎞 {len(missing_media)} টি ফাইলের বট-কপি এখনো "
                      f"তৈরি হয়নি — প্রথম পাঠানোর সময় তৈরি হবে (একটু ধীর লাগতে পারে)। "
                      f"আগেই সবার জন্য তৈরি করতে <a href='/admin/files'>📥 ফাইল পেজ</a> থেকে "
                      f"“সব ফাইল ক্যাশ করুন” চাপুন।</div>") if missing_media else ""
        body = f"""
        {note}{warnbox}
        <h3>📢 ক্যাম্পেইন #{campaign_id}</h3>
        <div class="grid">
          {card("Status", f'<span data-campaign-status>{esc(campaign["status"])}</span>')} {card("Total", counts['total'])}
          {card("Sent", counts['sent'])} {card("Failed", counts['failed'])}
          {card("Blocked", counts['blocked'])} {card("Pending", counts['pending'])}
        </div>
        <p class="muted" data-campaign-diagnostic>{esc(queue_diagnostic)}</p>
        <div class="bar" style="margin:14px 0"><i style="width:{progress['percent']}%"></i></div>
        <p class="muted">Audience: {esc(broadcast.audience_label(campaign['audience']))} ·
        Created {_start(campaign['created_at'])} ·
        Started {_start(campaign['started_at'])} · Finished {_start(campaign['finished_at'])}</p>
        {items_preview}
        {share_card}
        <div class="card"><h4>প্রিভিউ</h4><div>{broadcast.preview(campaign) or '<span class=muted>— (শুধু ফাইল) —</span>'}</div></div>
        {media_preview}
        <div class="row" style="margin:14px 0">
          <form method=post action="/admin/broadcast/{campaign_id}/start"><button class="ok">🚀 Send now</button></form>
          <form method=post action="/admin/broadcast/{campaign_id}/cancel"><button class="bad">⏹ Stop</button></form>
          <form method=post action="/admin/broadcast/{campaign_id}/retry"><button class="warn">🔁 Retry failed</button></form>
          <form method=post action="/admin/broadcast/{campaign_id}/test"><button class="grey">🧪 Test to me</button></form>
          <a class="btn grey" href="/admin/broadcast/{campaign_id}/report.csv">📊 রিপোর্ট CSV</a>
          <form method=post action="/admin/broadcast/{campaign_id}/share"><button class="grey">🔗 খসড়ার লিংক</button></form>
          <form method=post action="/admin/broadcast/{campaign_id}/delete"><button class="bad">🗑 Delete</button></form>
        </div>
        {cache_warn}{why}
        <h4>কিউ (প্রথম ৬০ জন) — কে পেল, কে পেল না</h4>
        <table><tr><th>ইউজার</th><th>অবস্থা</th><th>কারণ / নোট</th></tr>{sample or "<tr><td colspan=3 class=muted>খালি</td></tr>"}</table>
        <script>
          const bar = document.querySelector('.bar > i');
          const statusEl = document.querySelector('[data-campaign-status]');
          const diagnosticEl = document.querySelector('[data-campaign-diagnostic]');
          const cards = document.querySelectorAll('.card .n');
          let timer;
          async function tick() {{
            try {{
              const r = await fetch('admin/api/campaign/{campaign_id}', {{cache:'no-store'}});
              if (!r.ok) throw new Error('status ' + r.status);
              const d = await r.json();
              if (bar) bar.style.width = d.percent + '%';
              if (statusEl) statusEl.textContent = d.status || 'unknown';
              if (cards[1]) cards[1].textContent = d.total;
              if (cards[2]) cards[2].textContent = d.sent;
              if (cards[3]) cards[3].textContent = d.failed;
              if (cards[4]) cards[4].textContent = d.blocked;
              if (cards[5]) cards[5].textContent = d.pending;
              if (diagnosticEl) {{
                const message = d.flood_wait ? 'Telegram rate limit: ' + d.flood_wait + ' — worker is waiting safely.' :
                  d.last_error ||
                  (d.status === 'queued' && !d.bot_online ? 'Bot offline — campaign is safely queued.' :
                   d.status === 'queued' ? 'Bot online — waiting for the queue worker.' :
                   d.status === 'running' && !d.running ? (d.auto_resume ? 'Interrupted worker; use Send now to resume pending recipients.' : 'Auto-resume is off; use Send now to resume pending recipients.') :
                   'Campaign status is updating automatically.');
                diagnosticEl.textContent = message;
              }}
              if (!d.active) return;
              timer = setTimeout(tick, d.status === 'queued' && !d.bot_online ? 15000 : 5000);
            }} catch (e) {{
              timer = setTimeout(tick, 15000);
            }}
          }}
          tick();
        </script>
        """
        return layout(f"Campaign {campaign_id}", body, "/admin/broadcast")

    @app.get("/admin/broadcast/{campaign_id}/report.csv")
    async def admin_campaign_report(request: Request, campaign_id: int):
        """Who received the broadcast and who did not — the archive's `/send` CSV."""
        blocked = _guard(request)
        if blocked:
            return blocked
        campaign = db.campaign(campaign_id)
        if campaign is None:
            return RedirectResponse("/admin/broadcast?warn=ক্যাম্পেইন+নেই", status_code=303)
        data, _summary = broadcast.report_bytes(campaign_id)
        headers = {"Content-Disposition": f'attachment; filename="{broadcast.report_filename(campaign_id)}"'}
        return Response(content=data, media_type="text/csv; charset=utf-8", headers=headers)

    @app.post("/admin/broadcast/{campaign_id}/share")
    async def admin_campaign_share(request: Request, campaign_id: int):
        """Give the draft a public link someone else can open (nothing gets sent)."""
        blocked = _guard(request)
        if blocked:
            return blocked
        campaign = db.campaign(campaign_id)
        if campaign is None:
            return RedirectResponse("/admin/broadcast?warn=ক্যাম্পেইন+নেই", status_code=303)
        token = quickcast_service.share_draft(campaign_id)
        audit_service.record("draft_shared", ref=str(campaign_id), detail=token)
        return RedirectResponse(f"/admin/broadcast/{campaign_id}?flash=🔗+লিংক+তৈরি:+draft/{token}",
                                status_code=303)

    @app.get("/draft/{token}", response_class=HTMLResponse)
    async def public_draft(request: Request, token: str):
        """A read-only look at a draft — for a colleague, before anything is sent."""
        campaign = db.campaign_by_link(token)
        visitors.note(request, name="draft_open", detail=str(token)[:40])
        if campaign is None:
            return layout("খসড়া নেই", "<h2>🔍 এই খসড়ার লিংক আর কাজ করে না</h2>"
                                      "<p class='muted'>সম্ভবত মুছে ফেলা হয়েছে।</p>", "/")
        db.hit_campaign_link(token)
        plan = db.campaign_groups(int(campaign["id"]))
        if not plan:
            plan = [{"kind": "text", "text": campaign.get("text") or ""}]
        kind_icon = {"text": "💬", "photo": "🖼", "video": "🎬", "animation": "🎞",
                     "document": "📄", "audio": "🎵", "voice": "🎤"}
        tiles = "".join(
            f'<div class="card"><div class="muted">{index} · '
            f'{kind_icon.get(row.get("kind") or "text", "📦")} {esc(row.get("kind") or "text")}'
            f'</div><div style="margin-top:8px">{body or "<span class=muted>—</span>"}</div></div>'
            for index, row in enumerate(plan, start=1)
            for body in [(row.get("text") or "").replace("\n", "<br>")])
        buttons = db.campaign_links(int(campaign["id"]), kind="button")
        button_html = ""
        if buttons:
            button_html = ('<p class="row">' + "".join(
                f'<a class="btn grey" href="{esc(row["url"])}" rel="noopener" '
                f'target="_blank">{esc(row["label"])}</a>' for row in buttons) + "</p>")
        body = f"""
        <div class="page-heading"><div><p class="eyebrow">DRAFT PREVIEW</p>
          <h2>👀 খসড়া — {esc(campaign.get('title') or 'ব্রডকাস্ট')}</h2></div></div>
        <p class="muted">এটা শুধু দেখার জন্য — এখান থেকে কিছু পাঠানো যায় না। মালিক
        অ্যাডমিন প্যানেল বা বট থেকে অনুমোদন দিলে পাঠানো হবে।</p>
        {tiles}
        {button_html}"""
        return layout("খসড়া", body, "/")

    @app.post("/admin/broadcast/{campaign_id}/start")
    async def studio_start(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        campaign = db.campaign(campaign_id)
        if campaign is None:
            return RedirectResponse("/admin/broadcast?warn=ক্যাম্পেইন+পাওয়া+যায়নি", status_code=303)
        if not runtime.bot_online():
            # Explicitly queued campaigns always resume when Telegram comes online.
            db.update_campaign(campaign_id, status=broadcast.QUEUED,
                               scheduled_at=None, finished_at=None,
                               last_error="Waiting for the Telegram bot to come online.")
            return RedirectResponse(
                f"/admin/broadcast/{campaign_id}?warn=বট+অফলাইন+—+কিউতে+রাখা+হয়েছে",
                status_code=303)
        db.rearm_campaign_items(campaign_id, ("failed",))
        db.update_campaign(campaign_id, status=broadcast.QUEUED,
                           scheduled_at=None, finished_at=None, last_error="")
        db.campaign_counts(campaign_id)
        broadcast.start_campaign(campaign_id)
        audit_service.record("broadcast_start", ref=str(campaign_id),
                             detail=f"audience={campaign.get('audience')}")
        return RedirectResponse(f"/admin/broadcast/{campaign_id}?flash=🚀+শুরু+হয়েছে",
                                status_code=303)

    @app.post("/admin/broadcast/{campaign_id}/cancel")
    async def studio_cancel(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        broadcast.cancel_campaign(campaign_id)
        audit_service.record("broadcast_cancelled", ref=str(campaign_id))
        return RedirectResponse(f"/admin/broadcast/{campaign_id}?flash=⏹+থামানো+হলো",
                                status_code=303)

    @app.post("/admin/broadcast/{campaign_id}/retry")
    async def studio_retry(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        campaign = db.campaign(campaign_id)
        if campaign is None:
            return RedirectResponse("/admin/broadcast?warn=ক্যাম্পেইন+পাওয়া+যায়নি", status_code=303)
        # Blocked recipients are not blindly retried; Telegram will reject them
        # until they restart the bot. Preserve their blocked state for a clear report.
        rearmed = db.rearm_campaign_items(campaign_id, ("failed",))
        counts = db.campaign_counts(campaign_id)
        pending = counts["pending"]
        if rearmed or pending:
            if runtime.bot_online():
                db.update_campaign(campaign_id, status=broadcast.QUEUED,
                                   finished_at=None, last_error="")
                broadcast.start_campaign(campaign_id)
            else:
                db.update_campaign(campaign_id, status=broadcast.QUEUED,
                                   finished_at=None,
                                   last_error="Waiting for the Telegram bot to come online.")
        if not (rearmed or pending):
            note = "কোনো+ফেইল+বা+pending+recipient+নেই;+blocked+ইউজার+বট+আবার+start+না+করা+পর্যন্ত+বাদ"
        elif runtime.bot_online():
            note = f"🔁+{rearmed}+টি+ফেইল,+{pending}+টি+pending+কিউতে+চালু"
        else:
            note = f"🔁+{rearmed}+টি+ফেইল,+{pending}+টি+pending+কিউতে;+বট+অনলাইন+হলে+শুরু+হবে"
        audit_service.record("broadcast_retry", ref=str(campaign_id),
                             detail=f"rearmed={rearmed} pending={pending}")
        return RedirectResponse(
            f"/admin/broadcast/{campaign_id}?flash={note}", status_code=303)

    @app.post("/admin/broadcast/{campaign_id}/test")
    async def studio_test(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        if not cfg.ADMIN_IDS:
            return RedirectResponse(
                f"/admin/broadcast/{campaign_id}?warn=ADMIN_IDS+সেট+নেই", status_code=303)
        result = await broadcast.test_send(campaign_id, [cfg.ADMIN_IDS[0]])
        message = "🧪+টেস্ট+পাঠানো+হয়েছে" if result.get("ok") else "❌+টেস্ট+পাঠানো+যায়নি"
        audit_service.record("broadcast_test", ref=str(campaign_id),
                             detail="ok" if result.get("ok") else
                                    str((result.get("errors") or ["failed"])[0]))
        return RedirectResponse(f"/admin/broadcast/{campaign_id}?flash={message}", status_code=303)

    @app.post("/admin/broadcast/{campaign_id}/delete")
    async def studio_delete(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        row = db.campaign(campaign_id) or {}
        db.delete_campaign(campaign_id)
        audit_service.record("broadcast_deleted", ref=str(campaign_id),
                             detail=(row.get("title") or "")[:80])
        return RedirectResponse("/admin/broadcast?flash=🗑+মুছে+ফেলা+হয়েছে", status_code=303)

    @app.get("/admin/api/campaign/{campaign_id}")
    async def campaign_api(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        return JSONResponse(broadcast.campaign_progress(campaign_id))

    # --------------------------------------------------------------- content
    @app.get("/admin/stores", response_class=HTMLResponse)
    async def admin_stores(request: Request, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = []
        for store in db.all_stores():
            stats = db.store_stats(store["id"])
            sales = billing.store_sales(store["id"])
            drip = db.drip(store["id"])
            drip_txt = f"{drip['count']}/day {drip['send_time']}" if drip and drip["enabled"] else "off"
            rows.append(
                f"<tr><td><b>{esc(store['name'])}</b><div class='muted'>#{store['id']} · "
                f"{'🔒 premium' if store['is_premium'] else '🆓 free'}</div></td>"
                f"<td>{stats['files']}</td><td>{stats['views']}</td><td>{stats['grants']}</td>"
                f"<td>{money(sales['revenue'], settings.get_str('CURRENCY', '৳'))}</td>"
                f"<td>{len(db.plans(store['id'], only_active=True))}</td><td>{drip_txt}</td>"
                f"<td>{'✅ ' + esc(store.get('publish_channel_title') or 'linked') if store.get('publish_channel_id') else '—'}</td>"
                f"<td><a class='btn' href='/admin/stores/{store['id']}'>Open</a> "
                f"<a class='btn grey' href='/admin/analytics/store/{store['id']}'>Stats</a> "
                f"<a class='btn grey' href='/s/{esc(store['slug'] or store['id'])}'>Public page</a></td></tr>")
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        body = f"""{note}{warnbox}
        <div class="page-heading"><div><p class="eyebrow">CONTENT WORKSPACE</p><h2>Stores</h2>
          <p class="muted">প্রতিটি store-এর ভিডিও, access, Telegram channel ও analytics এক জায়গায়।</p></div>
          <a class="btn" href="/admin/stores/new">＋ Create store</a></div>
        <table><tr><th>Store</th><th>Files</th><th>Views</th><th>Granted</th><th>Revenue</th>
        <th>Plans</th><th>Drip</th><th>Video channel</th><th>Actions</th></tr>
        {''.join(rows) or "<tr><td colspan=9 class='muted'>এখনো store নেই — Create store দিয়ে শুরু করুন।</td></tr>"}</table>"""
        return layout("Stores", body, "/admin/stores")

    @app.get("/admin/stores/new", response_class=HTMLResponse)
    async def admin_store_new(request: Request, warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        body = f"""{warnbox}
        <div class="page-heading"><div><p class="eyebrow">CONTENT WORKSPACE</p><h2>Create a store</h2>
          <p class="muted">প্রতিটি store-এ আলাদা ভিডিও, access, analytics ও Telegram publish channel রাখা যাবে।</p></div></div>
        <div class="card" style="max-width:760px">
          <form method=post action="/admin/stores/new">
            <label>Store name</label><input name=name maxlength=100 placeholder="e.g. Beginner · Level 1" required>
            <label>Description</label><textarea name=description rows=4 placeholder="এই store-এ কী থাকবে…"></textarea>
            <label>Access</label>
            <select name=is_premium><option value="0">Free — সবাই দেখতে পারবে</option>
              <option value="1" selected>Premium — access / plan লাগবে</option></select>
            <label class="chk"><input type=checkbox name=create_channel value=1 checked>
              নিজের Telegram session দিয়ে এই store-এর জন্য private channel তৈরি করুন</label>
            <p class="muted">Channel creation-এর জন্য আগে Telegram session connect থাকতে হবে। Bot-কে channel admin করা গেলে
            নতুন ভিডিওগুলো channel-এ auto-post হবে।</p>
            <div class="row"><button class="ok">＋ Store তৈরি করুন</button>
              <a class="btn grey" href="/admin/stores">Cancel</a></div>
          </form>
        </div>"""
        return layout("Create store", body, "/admin/stores")

    @app.post("/admin/stores/new")
    async def admin_store_create(request: Request, name: str = Form(""),
                                 description: str = Form(""), is_premium: str = Form("1"),
                                 create_channel: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        name = (name or "").strip()[:100]
        if not name:
            return RedirectResponse("/admin/stores/new?warn=Store+name+প্রয়োজন", status_code=303)
        if not cfg.ADMIN_IDS:
            return RedirectResponse("/admin/stores/new?warn=আগে+ADMIN_IDS+সেট+করুন", status_code=303)
        admin_id = cfg.ADMIN_IDS[0]
        existing = db.store_by_name(admin_id, name)
        if existing:
            return RedirectResponse(f"/admin/stores/{existing['id']}?warn=এই+নামে+store+আগেই+আছে",
                                    status_code=303)
        store = db.create_store(admin_id, name)
        db.update_store(store["id"], description=(description or "").strip()[:2000],
                        is_premium=1 if str(is_premium) == "1" else 0)
        message = "Store তৈরি হয়েছে।"
        from app.services import media_cache
        media_cache.provision_store_later(store["id"])      # per-store channel, in background
        if create_channel:
            result = await channels.create_store_channel(store["id"], title=name)
            if not result.get("ok"):
                return RedirectResponse(
                    f"/admin/stores/{store['id']}?warn=" + quote_plus(
                        f"Store তৈরি হয়েছে, কিন্তু channel হয়নি: {result.get('error') or 'unknown'}"),
                    status_code=303)
            message += " Telegram channel তৈরি হয়েছে।"
            if result.get("warning"):
                message += " " + result["warning"]
        audit_service.record("store_created", store_id=store["id"], ref=name,
                             detail=f"premium={store['is_premium']} channel={bool(create_channel)}")
        return RedirectResponse(f"/admin/stores/{store['id']}?flash=" + quote_plus(message),
                                status_code=303)

    @app.post("/admin/stores/{store_id}/edit")
    async def admin_store_edit(request: Request, store_id: int, name: str = Form(""),
                               description: str = Form(""), is_premium: str = Form("1")):
        blocked = _guard(request)
        if blocked:
            return blocked
        store = db.store(store_id)
        name = (name or "").strip()[:100]
        if not store:
            return RedirectResponse("/admin/stores?warn=Store+পাওয়া+যায়নি", status_code=303)
        if not name:
            return RedirectResponse(f"/admin/stores/{store_id}?warn=Store+name+প্রয়োজন", status_code=303)
        duplicate = db.store_by_name(store["admin_id"], name)
        if duplicate and duplicate["id"] != store_id:
            return RedirectResponse(f"/admin/stores/{store_id}?warn=এই+নামে+আরেকটি+store+আছে", status_code=303)
        db.update_store(store_id, name=name, description=(description or "").strip()[:2000],
                        is_premium=1 if str(is_premium) == "1" else 0)
        audit_service.record("store_edited", store_id=store_id, ref=name,
                             detail=f"premium={is_premium == '1'}")
        return RedirectResponse(f"/admin/stores/{store_id}?flash=তথ্য+সংরক্ষণ+হয়েছে", status_code=303)

    @app.post("/admin/stores/{store_id}/channel/create")
    async def admin_store_channel_create(request: Request, store_id: int, title: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        store = db.store(store_id)
        if not store:
            return RedirectResponse("/admin/stores?warn=Store+পাওয়া+যায়নি", status_code=303)
        result = await channels.create_store_channel(store_id, title=title)
        if not result.get("ok"):
            return RedirectResponse(f"/admin/stores/{store_id}?warn=" + quote_plus(result.get("error") or "Channel তৈরি হয়নি"),
                                    status_code=303)
        message = f"Telegram channel তৈরি হয়েছে: {result.get('title') or store['name']}"
        files = db.files_of(store_id, newest_first=True)
        if files and result.get("auto_post"):
            post = await channels.auto_post_new_file(files[0]["id"], store_id)
            if post.get("ok"):
                message += " · সর্বশেষ ভিডিও পোস্ট করা হয়েছে।"
            else:
                message += " · ভিডিও পোস্ট হয়নি: " + str(post.get("error") or "unknown")[:100]
        if result.get("warning"):
            message += " " + result["warning"]
        return RedirectResponse(f"/admin/stores/{store_id}?flash=" + quote_plus(message), status_code=303)

    @app.post("/admin/stores/{store_id}/channel/autopost")
    async def admin_store_channel_autopost(request: Request, store_id: int,
                                           enabled: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        store = db.store(store_id)
        if not store:
            return RedirectResponse("/admin/stores?warn=Store+পাওয়া+যায়নি", status_code=303)
        if not store.get("publish_channel_id"):
            return RedirectResponse(f"/admin/stores/{store_id}?warn=আগে+একটি+publish+channel+তৈরি+করুন", status_code=303)
        if enabled:
            result = await channels.enable_store_channel(store_id)
            if not result.get("ok"):
                return RedirectResponse(f"/admin/stores/{store_id}?warn=" +
                                        quote_plus(result.get("error") or "Bot access চালু হয়নি"), status_code=303)
            message = "নতুন upload-এর auto-post চালু।"
        else:
            db.update_store(store_id, auto_post_enabled=0)
            message = "নতুন upload-এর auto-post বন্ধ।"
        return RedirectResponse(f"/admin/stores/{store_id}?flash=" + quote_plus(message), status_code=303)

    @app.post("/admin/stores/{store_id}/channel/post-latest")
    async def admin_store_channel_post_latest(request: Request, store_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        store = db.store(store_id)
        files = db.files_of(store_id, newest_first=True)
        if not store or not store.get("publish_channel_id"):
            return RedirectResponse(f"/admin/stores/{store_id}?warn=Publish+channel+সেট+নেই", status_code=303)
        if not files:
            return RedirectResponse(f"/admin/stores/{store_id}?warn=পোস্ট+করার+মতো+ভিডিও+নেই", status_code=303)
        defaults = channels.composer_defaults(store_id)
        result = await channels.publish(
            store["publish_channel_id"], text=defaults["text"],
            file_ids=[files[0]["id"]], buttons=defaults["buttons"],
            store_id=store_id, link_limit=-1,
        )
        if result.get("ok"):
            message = "সর্বশেষ ভিডিও channel-এ পোস্ট হয়েছে।"
            return RedirectResponse(f"/admin/stores/{store_id}?flash=" + quote_plus(message), status_code=303)
        return RedirectResponse(f"/admin/stores/{store_id}?warn=" +
                                quote_plus(result.get("error") or "Video পোস্ট করা যায়নি"), status_code=303)

    @app.get("/admin/files", response_class=HTMLResponse)
    async def admin_files(request: Request, q: str = "", store: int = 0, flash: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        if q:
            matches = db.search_all_stores(q, 200)
        elif store:
            matches = db.files_of(store)
        else:
            matches = db.newest_files(200)
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        rows = []
        for row in matches:
            store_row = db.store(row["store_id"]) or {}
            rows.append(
                f"<tr><td>{row['id']}</td><td>{esc(row['name'])}</td>"
                f"<td>{esc(store_row.get('name') or '')}</td><td>{esc(row['kind'])}</td>"
                f"<td>{row['views']}</td><td>{human_size(row.get('size'))}</td>"
                f"<td><a class='btn' href='/admin/files/broadcast/{row['id']}'>📢</a> "
                f"<a class='btn bad' href='/admin/files/delete/{row['id']}'>🗑</a></td></tr>")
        body = f"""{note}
        {panels.files_tools_body()}
        <h3>🗂 ফাইল</h3>
        <form method=get><input name=q placeholder="সব স্টোরে সার্চ" value="{esc(q)}"></form>
        <table><tr><th>ID</th><th>Name</th><th>Store</th><th>Type</th><th>Views</th><th>Size</th><th></th></tr>
        {''.join(rows)}</table>"""
        return layout("Files", body, "/admin/files")

    @app.post("/admin/files/bulk-delete")
    async def admin_files_bulk_delete(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        raw = str(form.get("ids", ""))
        ids = [int(part) for part in re.findall(r"\d+", raw)]
        if not ids:
            return RedirectResponse("/admin/files?flash=কোনো+ID+দেননি",
                                    status_code=303)
        count = db.delete_files(ids)
        db.log_event("files_purged", user_id=0, detail=f"ids={ids[:40]} n={count}")
        return RedirectResponse(f"/admin/files?flash=🗑+{count}+টি+ফাইল+মুছে+ফেলা+হলো",
                                status_code=303)

    @app.get("/admin/files/broadcast/{file_id}", response_class=HTMLResponse)
    async def file_broadcast(request: Request, file_id: int):
        """📢 per-video broadcast — the same campaign engine as the studio."""
        blocked = _guard(request)
        if blocked:
            return blocked
        file_row = db.file(file_id)
        if file_row is None:
            return RedirectResponse("/admin/files?flash=ফাইল+নেই", status_code=303)
        store_row = db.store(file_row["store_id"]) or {}
        body = f"""
        <h3>📢 ব্রডকাস্ট: {esc(file_row['name'])}</h3>
        <p class="muted">🏪 {esc(store_row.get('name') or '')} · 👁 {file_row['views']} views ·
        {esc(file_row['kind'])} {human_size(file_row.get('size'))}</p>
        <div class="card">
          <form method=post action="/admin/files/broadcast/{file_id}">
            <label>Audience</label><select name=audience>{_audience_options()}</select>
            <label>Caption (optional, <code>{{name}}</code> সমর্থিত)</label>
            <textarea name=text rows=4 placeholder="এই ভিডিওটি আপনার জন্য 🎬"></textarea>
            <label>ইনলাইন বাটন — প্রতি লাইনে একটা: <code>লেবেল | https://লিংক</code>
                   (একই লাইনে <code>&amp;&amp;</code> দিলে পাশাপাশি)</label>
            <textarea name=buttons rows=2 placeholder="🟢 স্টোর খুলুন | https://t.me/your_bot"></textarea>
            <label class="chk"><input type=checkbox name=online value=1 checked>
                   🟢 “অনলাইন — স্টোর খুলুন” বাটন যোগ করুন</label>
            <div class="row">
              <button class="ok" name=action value=start>🚀 শুরু করুন</button>
              <button class="warn" name=action value=test>🧪 আমাকে টেস্ট পাঠান</button>
              <button class="grey" name=action value=draft>📝 খসড়া রাখুন</button>
              <a class="btn grey" href="/admin/broadcast">📢 পুরো স্টুডিও</a>
            </div>
          </form>
        </div>"""
        return layout("File broadcast", body, "/admin/files")

    @app.post("/admin/files/broadcast/{file_id}")
    async def file_broadcast_post(request: Request, file_id: int, audience: str = Form("all"),
                                  text: str = Form(""), action: str = Form("start"),
                                  buttons: str = Form(""), online: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        file_row = db.file(file_id)
        if file_row is None:
            return RedirectResponse("/admin/files?flash=ফাইল+নেই", status_code=303)
        if not broadcast.resolve_audience(0, audience):
            return RedirectResponse("/admin/files?flash=এই+অডিয়েন্সে+কেউ+নেই", status_code=303)
        action = (action or "start").strip().lower()
        if action not in {"start", "draft", "test"}:
            return RedirectResponse("/admin/files?flash=অজানা+ক্যাম্পেইন+অ্যাকশন", status_code=303)
        owner = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        campaign = broadcast.create_campaign(
            owner, text=(text or "").strip(), title=file_row["name"][:40],
            audience=audience, files=[file_id], start=(action == "start"),
            buttons=buttons.strip(), online_button=1 if online else 0)
        campaign_id = campaign["id"]
        if action == "draft":
            return RedirectResponse(f"/admin/broadcast/{campaign_id}?flash=📝+খসড়া+সেভ+হয়েছে",
                                    status_code=303)
        if action == "test":
            if not cfg.ADMIN_IDS or not runtime.bot_online():
                return RedirectResponse(
                    f"/admin/broadcast/{campaign_id}?warn=বট+অনলাইন+নয়—টেস্ট+পাঠানো+যায়নি",
                    status_code=303)
            result = await broadcast.test_send(campaign_id, [cfg.ADMIN_IDS[0]])
            if not result.get("ok"):
                detail = (result.get("errors") or ["Test send failed"])[0]
                return RedirectResponse(
                    f"/admin/broadcast/{campaign_id}?warn={quote_plus(detail)}", status_code=303)
            return RedirectResponse(f"/admin/broadcast/{campaign_id}?flash=🧪+টেস্ট+পাঠানো+হয়েছে",
                                    status_code=303)
        if runtime.bot_online():
            broadcast.start_campaign(campaign_id)
            return RedirectResponse(f"/admin/broadcast/{campaign_id}?flash=🚀+শুরু+হয়েছে",
                                    status_code=303)
        return RedirectResponse(
            f"/admin/broadcast/{campaign_id}?warn=বট+অফলাইন+—+কিউতে+রাখা+হয়েছে",
            status_code=303)

    @app.get("/admin/files/delete/{file_id}")
    async def admin_delete_file(request: Request, file_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.delete_file(file_id)
        return RedirectResponse("/admin/files?flash=🗑+মুছে+ফেলা+হয়েছে", status_code=303)

    # ---------------------------------------------------------------- orders
    @app.get("/admin/orders", response_class=HTMLResponse)
    async def admin_orders(request: Request, flash: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = []
        for order in db.orders(limit=60):
            store_row = db.store(order["store_id"]) or {}
            user = db.user(order["user_id"]) or {}
            actions = ""
            if order["status"] == "pending":
                actions = (f"<a class='btn ok' href='/admin/orders/approve/{order['id']}'>✅</a> "
                           f"<a class='btn bad' href='/admin/orders/reject/{order['id']}'>❌</a>")
            rows.append(
                f"<tr><td>#{order['id']}</td><td>{esc(store_row.get('name') or '')}</td>"
                f"<td>{esc(user.get('name') or '')}<div class='muted'>{order['user_id']}</div></td>"
                f"<td>{esc(order['plan_name'] or '')}</td>"
                f"<td>{money(order['amount'] or 0, order['currency'] or '')}</td>"
                f"<td>{order['status']}<div class='muted'>{esc((order['proof'] or '')[:60])}</div></td>"
                f"<td>{_start(order['created_at'])}</td><td>{actions}</td></tr>")
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        return layout("Orders", f"""{note}<h3>🧾 অর্ডার</h3>
        <table><tr><th>#</th><th>Store</th><th>User</th><th>Plan</th><th>Amount</th><th>Status</th>
        <th>Created</th><th></th></tr>{''.join(rows)}</table>""", "/admin/orders")

    @app.get("/admin/orders/approve/{order_id}")
    async def admin_approve(request: Request, order_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        billing.approve_order(order_id, admin_id=0, note="approved from dashboard")
        return RedirectResponse(f"/admin/orders?flash=Approved+%23{order_id}", status_code=303)

    @app.get("/admin/orders/reject/{order_id}")
    async def admin_reject(request: Request, order_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        billing.reject_order(order_id, admin_id=0, note="rejected from dashboard")
        return RedirectResponse(f"/admin/orders?flash=Rejected+%23{order_id}", status_code=303)

    # ----------------------------------------------------------------- users
    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users(request: Request, q: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        everyone = db.all_user_ids()
        if q:
            needle = q.strip().lower()
            everyone = [uid for uid in everyone
                        if needle in str(uid)
                        or needle in ((db.user(uid) or {}).get("name") or "").lower()]
        rows = []
        for uid in everyone[:300]:
            user = db.user(uid) or {}
            grants = db.user_grants(uid)
            badges = ", ".join(
                f"{esc(g['store_name'])} "
                f"{'♾' if g['expires_at'] is None else _start(g['expires_at'], '%d %b')}"
                for g in grants) or '<span class="muted">free</span>'
            box = (f'<input type="checkbox" name="ids" value="{uid}">')
            rows.append(f"<tr><td>{box} <a href='/admin/users/{uid}'>{uid}</a></td>"
                        f"<td>{esc(user.get('name') or '')}</td>"
                        f"<td>{esc(user.get('username') or '')}</td>"
                        f"<td>{_start(user.get('joined_at'))}</td><td>{badges}</td>"
                        f"<td><a class='btn bad' href='/admin/users/{uid}/delete"
                        f"?confirm=1'>🗑</a></td></tr>")
        return layout("Users", f"""<h3>👥 ইউজার ({len(everyone)})</h3>
        <form method=get><input name=q placeholder="id বা নাম" value="{esc(q)}"></form>
        {panels.users_tools_body(len(everyone))}
        <form method=post action="/admin/users/bulk-delete"
              onsubmit="return confirm('নির্বাচিত ইউজারদের সব ডেটা মুছে যাবে। চালিয়ে যাবেন?')">
        <table><tr><th></th><th>ID</th><th>Name</th><th>Username</th><th>Joined</th>
        <th>Access</th><th></th></tr>
        {''.join(rows)}</table>
        <div class="row" style="margin-top:10px">
          <button class="btn bad" type="submit">🗑 নির্বাচিত ইউজার মুছুন</button>
        </div></form>""", "/admin/users")

    @app.post("/admin/users/bulk-delete")
    async def admin_users_bulk_delete(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        ids = [int(part) for part in re.findall(r"\d+", str(form.get("ids", "")))]
        admins = set(cfg.ADMIN_IDS)
        ids = [uid for uid in ids if uid not in admins]
        count = db.delete_users(ids)
        db.log_event("users_purged", user_id=0, detail=f"n={count}")
        return RedirectResponse(f"/admin/users?flash=🗑+{count}+জন+ইউজার+মুছে+ফেলা+হলো",
                                status_code=303)

    @app.get("/admin/users/purge", response_class=HTMLResponse)
    async def admin_users_purge_preview(request: Request, days: int = 90,
                                        never_started: int = 0):
        """🧹 পুরোনো ইউজার একসাথে পরিষ্কার — আগে দেখায় কতজন যাবে, তারপর মোছে।"""
        blocked = _guard(request)
        if blocked:
            return blocked
        ids = db.purge_user_ids(days=days, never_started=bool(never_started),
                                keep_admins=cfg.ADMIN_IDS)
        sample = "".join(
            f"<tr><td>{uid}</td><td>{esc((db.user(uid) or {}).get('name') or '')}</td>"
            f"<td>{_start((db.user(uid) or {}).get('last_seen'))}</td></tr>"
            for uid in ids[:100])
        body = f"""
        <h3>🧹 পুরোনো ইউজার পরিষ্কার</h3>
        <p class="muted">যারা অনেক দিন বট ব্যবহার করেননি (বা কখনো <code>/start</code> দেননি)
        — এখান থেকে একবারে মুছে ফেলা যায়। অ্যাডমিনরা কখনো মুছবে না।</p>
        <form method=get class="row">
          <label>কত দিন অ্যাকটিভ না <input name="days" value="{days}" size="5"></label>
          <label><input type="checkbox" name="never_started" value="1"
            {'checked' if never_started else ''}> শুধু যারা কখনো শুরুই করেনি</label>
          <button class="btn grey" type="submit">🔍 কতজন হবে দেখুন</button>
        </form>
        <p><b>{len(ids)}</b> জন ইউজার মুছে ফেলা হবে।</p>
        <form method=post action="/admin/users/purge"
              onsubmit="return confirm('{len(ids)} জন ইউজারের সব ডেটা মুছে যাবে। নিশ্চিত?')">
          <input type="hidden" name="days" value="{days}">
          <input type="hidden" name="never_started" value="{1 if never_started else 0}">
          <button class="btn bad" type="submit">🗑 {len(ids)} জন মুছে ফেলুন</button>
        </form>
        {f'<table><tr><th>ID</th><th>নাম</th><th>শেষ দেখা</th></tr>{sample}</table>' if ids else ''}
        """
        return layout("Purge users", body, "/admin/users")

    @app.post("/admin/users/purge")
    async def admin_users_purge(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        days = int(str(form.get("days", "90")) or 90)
        never = str(form.get("never_started", "0")) == "1"
        ids = db.purge_user_ids(days=days, never_started=never, keep_admins=cfg.ADMIN_IDS)
        count = db.delete_users(ids)
        db.log_event("users_purged", user_id=0, detail=f"days={days} n={count}")
        return RedirectResponse(f"/admin/users?flash=🧹+{count}+জন+পুরোনো+ইউজার+মুছে+ফেলা+হলো",
                                status_code=303)

    @app.get("/admin/users/{user_id}/delete")
    async def admin_user_delete(request: Request, user_id: int, confirm: int = 0):
        blocked = _guard(request)
        if blocked:
            return blocked
        if user_id in set(cfg.ADMIN_IDS):
            return RedirectResponse("/admin/users?flash=অ্যাডমিনকে+মোছা+যাবে+না",
                                    status_code=303)
        ok = db.delete_user(user_id)
        label = "🗑+ইউজার+মুছে+ফেলা+হলো" if ok else "ইউজার+নেই"
        return RedirectResponse(f"/admin/users?flash={label}", status_code=303)

    # -------------------------------------------------------------- download
    @app.get("/admin/download", response_class=HTMLResponse)
    async def admin_download(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        size = os.path.getsize(cfg.DB_FILE) if os.path.exists(cfg.DB_FILE) else 0
        body = f"""
        <h3>📥 এক্সপোর্ট</h3>
        <div class="card">
          <p class="muted">DB: <code>{esc(db.display_path)}</code> · {human_size(size)}</p>
          <p><a class="btn" href="/admin/download/db">🗄 SQLite</a>
             <a class="btn grey" href="/admin/download/json">📄 JSON</a></p>
          <p><a class="btn grey" href="/admin/download/csv/users">👥 Users CSV</a>
             <a class="btn grey" href="/admin/download/csv/orders">🧾 Orders CSV</a>
             <a class="btn grey" href="/admin/download/csv/files">🗂 Files CSV</a></p>
        </div>"""
        return layout("Export", body, "/admin/download")

    # ---------------------------------------------------------------- portable
    # “হোস্টিং বদলালেও কিছু হারাবে না” — one file holds the whole install and can be
    # given back from the panel, from the setup screen (new host, before anything is
    # configured) or by sending it to the bot (`/restore`).
    def _backup_kpis() -> str:
        counts = db.stats()
        size = os.path.getsize(str(db.path)) if os.path.exists(str(db.path)) else 0
        local = portable_service.local_backups(limit=99)
        return ui.kpi_row(
            ui.kpi("ডেটাবেজ", human_size(size), sub=esc(os.path.basename(str(db.path)))),
            ui.kpi("ইউজার", counts.get("users", 0), sub="আইডি + অ্যাক্সেস-হ্যাশ সহ"),
            ui.kpi("ফাইল", counts.get("files", 0), sub=f"{counts.get('stores', 0)} স্টোরে"),
            ui.kpi("সেভ করা ব্যাকআপ", len(local), sub="সার্ভারে রাখা ফাইল", dark=True))

    def _backup_local_table() -> str:
        rows = []
        for row in portable_service.local_backups(limit=10):
            pill = ' <span class="pill warn">সেফটি</span>' if row["safety"] else ""
            rows.append(
                f"<tr><td><code>{esc(row['name'])}</code>{pill}</td>"
                f"<td class='num'>{human_size(row['size'])}</td>"
                f"<td class='muted'>{esc(row['mtime_text'])}</td>"
                f"<td><form method=post action='/admin/backup/restore-local'>"
                f"<input type=hidden name=name value='{esc(row['name'])}'>"
                f"<input type=hidden name=confirm value=yes>"
                f"<button class='grey'>↩︎ ফিরিয়ে আনুন</button></form></td></tr>")
        return ui.table(("ফাইল", "সাইজ", "কখন", ""), "".join(rows), numeric=(1,),
                        empty_row="এখনো কোনো ব্যাকআপ ফাইল সেভ করা হয়নি।")

    def _restore_card() -> str:
        """What the last restore did — kept on screen until the next one."""
        report = _LAST_RESTORE.get("report")
        if not report:
            return ""
        after = report.get("counts_after") or {}
        before = report.get("counts_before") or {}
        pairs = "".join(
            f"<li>{label}: {before.get(key, 0)} → <b>{after.get(key, 0)}</b></li>"
            for key, label in (("users", "ইউজার"), ("stores", "স্টোর"),
                               ("files", "ফাইল"), ("grants", "অ্যাক্সেস"),
                               ("orders", "অর্ডার")))
        creds = ("এই ব্যাকআপে বট টোকেন/API আইডিও ছিল — সেটাও ফিরে এসেছে, নতুন হোস্টে "
                 "আলাদা করে কিছু দিতে হবে না।" if report.get("credentials_included") else
                 "ব্যাকআপে টোকেন ছিল না — <code>/setup</code>-এ একবার টোকেন/API আইডি দিন।")
        if report.get("merge"):
            bits = "".join(f"<li>{esc(part)}</li>"
                           for part in portable_service.import_summary(report))
            return (f"<div class='note ok'><b>✅ {esc(report.get('kind_label') or 'পুরোনো ব্যাকআপ')} "
                    f"যোগ করা হয়েছে</b> ({esc(report.get('source') or '')})"
                    f"<ul>{bits or '<li>সবই আগে থেকে ছিল — নতুন কিছু যোগ হয়নি</li>'}</ul>"
                    f"<p>বর্তমান ডেটার কিছুই মুছে যায়নি — নতুনগুলো যোগ হয়েছে।</p>"
                    f"<p class='muted'>পুরোনো ইনস্টলের ইউজারদের অ্যাক্সেস-হ্যাশ থাকে না; "
                    f"তারা আবার <code>/start</code> দিলে বা ব্রডকাস্টের সময় নিজে থেকেই "
                    f"সেটা শিখে নেওয়া হয়।</p></div>")
        return (f"<div class='note ok'><b>✅ ইনস্টল ফিরিয়ে আনা হয়েছে</b> "
                f"({esc(report.get('created_at_text') or '')} · {esc(report.get('source') or '')})"
                f"<ul>{pairs}</ul><p>{creds}</p>"
                f"<p class='muted'>রিস্টোরের আগের অবস্থা সেভ করা আছে: "
                f"<code>{esc(os.path.basename(report.get('safety_copy') or '—'))}</code> — "
                f"ভুল হয়ে গেলে ওটা দিয়েই ফেরা যায়।</p></div>")

    def _run_restore(blob: bytes, source: str) -> tuple[bool, str]:
        """Apply whatever the owner gave us (full backup *or* an older format)."""
        try:
            info = portable_service.inspect(blob)
            report = portable_service.apply(blob, source=source)
        except portable_service.BackupError as exc:
            return False, str(exc)
        except Exception as exc:                              # pragma: no cover - defensive
            log.exception("restore failed")
            return False, f"রিস্টোর ব্যর্থ: {exc}"
        report["kind_label"] = info.get("label") or ""
        _LAST_RESTORE["report"] = report
        audit_service.record("restore_done", ref=source,
                             detail=f"{info.get('kind')} {report.get('counts_after', {})}"[:250])
        after = report.get("counts_after") or {}
        if report.get("merge"):
            bits = ", ".join(portable_service.import_summary(report))
            return True, (f"✅ {info.get('label') or 'পুরোনো ব্যাকআপ'} যোগ করা হয়েছে"
                          + (f" ({bits})" if bits else "")
                          + f" — এখন মোট {after.get('users', 0)} ইউজার, "
                            f"{after.get('stores', 0)} স্টোর (আগের ডেটা মুছিনি)")
        return True, (f"✅ ইনস্টল ফিরিয়ে আনা হয়েছে — {after.get('users', 0)} ইউজার, "
                      f"{after.get('stores', 0)} স্টোর, {after.get('files', 0)} ফাইল")

    def _restore_forms(action: str, *, heading: str, hint: str) -> str:
        return f"""<div class="card"><h4>{heading}</h4>
          <p class="muted">{hint}</p>
          <form method=post action="{action}" enctype="multipart/form-data">
            <label>ব্যাকআপ ফাইল (.bfixf.gz / .sqlite3 / .json / পুরোনো users.db)</label>
            <input type="file" name="file"
                   accept=".gz,.json,.sqlite3,.db,.bfixf,application/gzip">
            <label>…অথবা ব্যাকআপ কোড পেস্ট করুন (BFIXF1:…)</label>
            <textarea name="code" rows=3
              placeholder="BFIXF1:1/5:H4sIA… (কয়েক টুকরো হলে সবগুলো একসাথে পেস্ট করুন)"></textarea>
            <label class="chk"><input type="checkbox" name="confirm" value="yes">
              আমি বুঝেছি — এখনকার ডেটা এই ব্যাকআপ দিয়ে বদলে যাবে (আগেরটার সেফটি কপি
              নিজে থেকেই সেভ হবে)</label>
            <button>♻️ ফিরিয়ে আনুন</button>
          </form></div>"""

    @app.get("/admin/backup", response_class=HTMLResponse)
    async def admin_backup(request: Request, flash: str = "", warn: str = ""):
        """Download / upload / paste — the whole “move to another host” job in one page."""
        blocked = _guard(request)
        if blocked:
            return blocked
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        steps = "".join(f"<li>{step}</li>" for step in portable_service.migration_steps())
        codes = portable_service.text_codes()
        code_hint = (f"পুরো ইনস্টল {len(codes)} টুকরো টেক্সট কোডেও নেওয়া যায় — খুব দরকার "
                     f"হলে ডাউনলোড না করে কোড কপি করুন।" if len(codes) > 1 else
                     "ছোট ইনস্টল হলে এক লাইন টেক্সট কোডেও নেওয়া যায়।")
        body = f"""
        {ui.page_head("ব্যাকআপ ও হোস্টিং বদল",
                      "একটা ফাইলেই পুরো ইনস্টল — ইউজার আইডি, অ্যাক্সেস-হ্যাশ, স্টোর, "
                      "ফাইল, সেটিংস, টেক্সট, অর্ডার, কুপন। নতুন হোস্টে সেটা ফিরিয়ে "
                      "দিলে সব আগের মতোই হয়ে যায়।",
                      eyebrow="SETTINGS",
                      actions=('<a class="btn grey" href="/admin/download">📥 অন্যান্য এক্সপোর্ট</a>'
                               '<a class="btn grey" href="/admin/self-test">🩺 সেলফ-টেস্ট</a>'))}
        {note}{warnbox}{_restore_card()}{_backup_kpis()}
        <div class="grid two">
          <div class="card"><h4>⬇️ ১. এখনকার ইনস্টল নামান</h4>
            <p class="muted">ডেটাবেজ gzip হয়ে ছোট থাকে; বট টোকেন (ওয়েবসাইট থেকে দিলে)
            ফাইলের ভিতরেই যায়।</p>
            <div class="row">
              <a class="btn" href="/admin/backup/download">🗄 পুরো ব্যাকআপ নামান</a>
              <form method=post action="/admin/backup/save">
                <button class="grey">💾 সার্ভারেও সেভ করুন</button></form>
            </div>
            <p class="muted">⚠️ ফাইল/কোডে টোকেন থাকতে পারে — শুধু নিজের কাছে রাখুন।<br>
            {code_hint}</p>
          </div>
          {_restore_forms("/admin/backup/restore", heading="⬆️ ২. অন্য জায়গার ডেটা ফিরিয়ে আনুন",
                          hint="নতুন ব্যাকআপ (.bfixf.gz), পুরোনো ভার্সনের ডেটাবেজ (.sqlite3), "
                               "পুরোনো bot_db.json, আর পুরোনো বটের users.db — সবই চলে। "
                               "পুরো ব্যাকআপ হলে এই ইনস্টলের ডেটা বদলে যায় (আগেরটা নিজে থেকেই "
                               "সেভ হয়); পুরোনো ফাইল হলে শুধু <b>যোগ</b> হয় — কিছু মোছে না।")}
        </div>
        <div class="card"><h4>🗂 সার্ভারে সেভ করা ব্যাকআপ</h4>{_backup_local_table()}</div>
        <div class="card"><h4>🚚 নতুন হোস্টে নেওয়ার ধাপ</h4><ol class="steps">{steps}</ol></div>
        """
        return layout("Backup", body, "/admin/backup")

    @app.get("/admin/backup/download")
    async def admin_backup_download(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        data = portable_service.export_bytes(note="panel download")
        audit_service.record("backup_downloaded", ref="panel", detail=f"{len(data)} bytes")
        headers = {"Content-Disposition":
                   f'attachment; filename="{portable_service.filename()}"'}
        return Response(content=data, media_type="application/gzip", headers=headers)

    @app.post("/admin/backup/save")
    async def admin_backup_save(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        directory = str(cfg.BACKUP_DIR)
        os.makedirs(directory, exist_ok=True)
        name = portable_service.filename()
        with open(os.path.join(directory, name), "wb") as handle:
            handle.write(portable_service.export_bytes(note="saved from panel"))
        audit_service.record("backup_saved", ref="panel", detail=name)
        return RedirectResponse(f"/admin/backup?flash=💾 ব্যাকআপ সেভ হলো: {name}",
                                status_code=303)

    @app.post("/admin/backup/restore")
    async def admin_backup_restore(request: Request, confirm: str = Form(""),
                                   code: str = Form(""),
                                   file: UploadFile | None = File(None)):
        blocked = _guard(request)
        if blocked:
            return blocked
        if (confirm or "").strip().lower() not in ("yes", "on", "1"):
            return RedirectResponse(
                "/admin/backup?warn=নিশ্চিত করার বক্সে টিক দিন (ডেটা বদলে যাবে)",
                status_code=303)
        blob = b""
        if file is not None and getattr(file, "filename", ""):
            blob = await file.read()
        elif (code or "").strip():
            try:
                blob = portable_service.decode_text(code)
            except portable_service.BackupError as exc:
                return RedirectResponse(f"/admin/backup?warn={str(exc)[:160]}",
                                        status_code=303)
        if not blob:
            return RedirectResponse("/admin/backup?warn=ফাইল বা ব্যাকআপ কোড কিছুই দিননি",
                                    status_code=303)
        ok, message = _run_restore(blob, "panel upload")
        key = "flash" if ok else "warn"
        return RedirectResponse(f"/admin/backup?{key}={message}", status_code=303)

    @app.post("/admin/backup/restore-local")
    async def admin_backup_restore_local(request: Request, name: str = Form(""),
                                         confirm: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        if (confirm or "").strip().lower() not in ("yes", "on", "1"):
            return RedirectResponse("/admin/backup?warn=নিশ্চিত করার টিক দিন",
                                    status_code=303)
        safe = os.path.basename(name or "")
        path = os.path.join(str(cfg.BACKUP_DIR), safe)
        if not safe.endswith(portable_service.SUFFIX) or not os.path.isfile(path):
            return RedirectResponse("/admin/backup?warn=ফাইলটা সার্ভারে নেই",
                                    status_code=303)
        with open(path, "rb") as handle:
            ok, message = _run_restore(handle.read(), f"local {safe}")
        key = "flash" if ok else "warn"
        return RedirectResponse(f"/admin/backup?{key}={message}", status_code=303)

    @app.get("/admin/download/db")
    async def download_db(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        if db.backend == 'postgresql':
            snapshot = await asyncio.to_thread(db.conn.snapshot)
            return Response(snapshot, media_type='application/octet-stream',
                            headers={'Content-Disposition': 'attachment; filename="bot_data.sqlite3"'})
        if not os.path.exists(cfg.DB_FILE):
            return JSONResponse({"error": "DB file not found"}, status_code=404)
        return FileResponse(cfg.DB_FILE, filename="bot_data.sqlite3",
                            media_type="application/octet-stream")

    @app.get("/admin/download/json")
    async def download_json(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        import json
        import tempfile
        data = db.export_dict()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8")
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.close()
        return FileResponse(tmp.name, filename=f"bot_data-{int(time.time())}.json",
                            media_type="application/json")

    def _csv_response(header: list[str], rows: list[list], filename: str) -> FileResponse:
        import csv
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w",
                                          encoding="utf-8", newline="")
        writer = csv.writer(tmp)
        writer.writerow(header)
        writer.writerows(rows)
        tmp.close()
        return FileResponse(tmp.name, filename=filename, media_type="text/csv")

    @app.get("/admin/download/csv/users")
    async def csv_users(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = []
        for uid in db.all_user_ids():
            user = db.user(uid) or {}
            grants = ";".join(f"{g['store_name']}:{g['store_id']}" for g in db.user_grants(uid))
            rows.append([uid, user.get("name"), user.get("username"), user.get("joined_at"),
                         user.get("last_seen"), grants])
        return _csv_response(["user_id", "name", "username", "joined_at", "last_seen", "grants"],
                             rows, "users.csv")

    @app.get("/admin/download/csv/orders")
    async def csv_orders(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = [[o["id"], o["user_id"], o["store_id"], o["plan_name"], o["days"], o["amount"],
                 o["currency"], o["method"], o["status"], o["created_at"], o.get("proof")]
                for o in db.orders(limit=10000)]
        return _csv_response(["id", "user_id", "store_id", "plan_name", "days", "amount",
                              "currency", "method", "status", "created_at", "proof"],
                             rows, "orders.csv")

    @app.get("/admin/download/csv/files")
    async def csv_files(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = []
        for store in db.all_stores():
            for f in db.files_of(store["id"]):
                rows.append([f["id"], f["store_id"], store["name"], f["name"], f["kind"],
                             f["views"], f["created_at"]])
        return _csv_response(["id", "store_id", "store_name", "name", "kind", "views",
                              "created_at"], rows, "files.csv")

    # -------------------------------------------------------------- settings
    def _field(item, key_prefix: str = "") -> str:
        """One form row, rendered from the settings registry."""
        name = f"{key_prefix}{item.key}"
        value = settings.get(item.key)
        if item.kind == "bool":
            checked = " checked" if value else ""
            return (f'<label class="chk"><input type="checkbox" name="{name}" '
                    f'value="1"{checked}> {esc(item.label)}</label>')
        if item.kind == "choice":
            options = "".join(
                f'<option value="{esc(choice)}"'
                f'{" selected" if str(value) == str(choice) else ""}>{esc(choice)}</option>'
                for choice in (item.choices or ()))
            inner = f'<select name="{name}">{options}</select>'
        elif item.kind == "long":
            inner = (f'<textarea name="{name}" rows="3" placeholder="{esc(item.placeholder)}">'
                     f'{esc(value)}</textarea>')
        else:
            shown = "" if value is None else value
            input_type = "number" if item.kind in ("int", "float") else "text"
            step = ' step="any"' if item.kind == "float" else ""
            inner = (f'<input type="{input_type}"{step} name="{name}" '
                     f'value="{esc(shown)}" placeholder="{esc(item.placeholder)}">')
        badge = {"db": "প্যানেল", "env": "config.env", "default": "ডিফল্ট"}.get(
            settings.source(item.key), "")
        hint = f'<div class="muted">{esc(item.hint)}</div>' if item.hint else ""
        return (f'<label>{esc(item.label)} <span class="pill">{badge}</span></label>'
                f'{inner}{hint}')

    @app.get("/admin/settings", response_class=HTMLResponse)
    async def admin_settings(request: Request, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        bot_ok = runtime.bot_online()
        groups_html = []
        for group in settings.all_groups():
            fields = "".join(_field(item) for item in group.settings)
            extra = ""
            if group.key == "force_join":
                link = forcejoin.join_url()
                resolved_id = db.get_meta("force_channel_id", "")
                extra = (f'<p class="muted">🔗 জয়েন লিংক: '
                         f'<code>{esc(link) or "—"}</code> · '
                         f'🆔 <code>{esc(resolved_id) or "— এখনো রেজলভ হয়নি —"}</code></p>'
                         f'<p class="muted">প্রাইভেট চ্যানেল হলে ইনভাইট লিংক '
                         f'(<code>t.me/+…</code>) দিন — বট সেটাও বুঝবে।</p>')
            opened = " open" if group.key in ("site", "emergency") else ""
            groups_html.append(f"""
            <details class="settings-section" id="g-{group.key}"{opened}>
              <summary><span>{group.icon} {esc(group.label)}</span>
                <span class="muted">{len(group.settings)} settings · click to expand</span></summary>
              <div class="card settings-inner">
                <p class="muted">{esc(group.blurb)}</p>
                <form method=post action="/admin/settings/save">
                  <input type="hidden" name="__group" value="{group.key}">
                  {fields}
                  {extra}
                  <div class="row" style="margin-top:10px">
                    <button class="ok" type="submit">💾 সেভ করুন</button>
                  </div>
                </form>
              </div>
            </details>""")
        # A jump-list, so 12 collapsed groups do not have to be opened one by one.
        index = "".join(
            f'<a class="btn grey" href="#g-{group.key}">{group.icon} {esc(group.label)}'
            f'<span class="muted"> ({len(group.settings)})</span></a>'
            for group in settings.all_groups())
        body = f"""
        {note}{warnbox}
        <h3>⚙️ সেটিংস</h3>
        <p class="muted">সব কিছু এখান থেকেই বদলানো যায় — বট রিস্টার্ট বা ফাইল এডিট লাগে না।
        বটের ⚙️ Settings স্ক্রিনেও একই সেটিংস আছে। badge বলে দেয় মানটা কোথা থেকে আসছে
        (প্যানেল / config.env / ডিফল্ট); কিছু সেভ করলে সেটা environment মানকে ওভাররাইড করে।</p>
        <div class="grid">
          {card("Bot", f"@{_bot_username() or '—'}")}
          {card("Bot online", "✅" if bot_ok else "❌")}
          {card("Uptime", f"{int(time.time() - STARTED_AT)}s")}
          {card("DB size", human_size(os.path.getsize(cfg.DB_FILE) if os.path.exists(cfg.DB_FILE) else 0))}
        </div>
        <p class="row" style="margin:14px 0">
          <a class="btn grey" href="/admin/settings/export">⬇️ config.env হিসেবে এক্সপোর্ট</a>
          <a class="btn grey" href="/admin/download">📥 ডেটা এক্সপোর্ট</a>
        </p>
        <div class="row" style="margin:0 0 14px">{index}</div>
        {''.join(groups_html)}
        """
        return layout("Settings", body, "/admin/settings")

    @app.post("/admin/settings/save")
    async def admin_settings_save(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        group_key = str(form.get("__group", "")).strip()
        group = next((g for g in settings.all_groups() if g.key == group_key), None)
        if group is None:
            return RedirectResponse("/admin/settings?warn=অজানা+গ্রুপ", status_code=303)
        values: dict[str, str] = {}
        for item in group.settings:
            raw = form.get(item.key)
            if item.kind == "bool":
                values[item.key] = "1" if raw is not None else "0"
            else:
                values[item.key] = "" if raw is None else str(raw)
        errors = settings.set_many(values)
        if group_key == "force_join":
            forcejoin.clear_cache()
            if not errors and settings.get_str("FORCE_CHANNEL"):
                settings.set("FORCE_JOIN_ENABLED", "1")
        if errors:
            joined = "+".join(f"{key}:{msg}" for key, msg in errors.items())
            return RedirectResponse(f"/admin/settings?warn={joined}", status_code=303)
        changed = [key for key, value in values.items() if value != settings.get_str(key)]
        audit_service.record("settings_saved", ref=group_key,
                             detail=", ".join(changed[:12]) or "কোনো পরিবর্তন নেই")
        return RedirectResponse(
            f"/admin/settings?flash=✅+{group.label}+সেভ+হলো", status_code=303)

    @app.get("/admin/settings/forcejoin/check")
    async def admin_forcejoin_check(request: Request):
        """Resolve the channel now (the bot or a session has to be online)."""
        blocked = _guard(request)
        if blocked:
            return blocked
        result = await forcejoin.resolve(refresh=True, use_cache=False)
        if result.get("ok"):
            message = (f"✅+চ্যানেল+পাওয়া+গেছে:+{result['title']}+"
                       f"({result['chat_id']})")
        else:
            message = f"⚠️+চ্যানেল+পাওয়া+যায়নি:+{result.get('error') or '?'}"
        return RedirectResponse(f"/admin/settings?flash={message}", status_code=303)

    # ------------------------------------------------------------ emergency center
    def _emergency_state() -> dict:
        from app.services import media_cache
        admin_id = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        manual = settings.get_str("STORAGE_CHANNEL")
        auto = db.get_meta(media_cache.META_STORAGE, "")
        try:
            mirrors = media_cache.stats()
        except Exception:
            mirrors = {"files": 0, "mirrored": 0, "missing": 0}
        return {
            "bot": runtime.bot_online(),
            "session": bool(admin_id and runtime.is_online(admin_id)),
            "storage_manual": manual,
            "storage_auto": auto,
            "per_store": settings.get_bool("PER_STORE_CHANNEL", True),
            "mirrors": mirrors,
            "maintenance": settings.get_bool("MAINTENANCE_MODE", False),
            "force_join": settings.get_bool("FORCE_JOIN_ENABLED", True),
            "force_channel": settings.get_str("FORCE_CHANNEL"),
        }

    def _flag(ok: bool, yes: str, no: str) -> str:
        return (f'<span class="pill" style="color:var(--ok)">● {esc(yes)}</span>' if ok
                else f'<span class="pill" style="color:var(--bad)">● {esc(no)}</span>')

    @app.get("/admin/emergency", response_class=HTMLResponse)
    async def admin_emergency(request: Request, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        st = _emergency_state()
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        storage = (f"হাতে সেট: <code>{esc(st['storage_manual'])}</code>" if st["storage_manual"]
                   else (f"অটো চ্যানেল: <code>{esc(st['storage_auto'])}</code>" if st["storage_auto"]
                         else "এখনও কোনো চ্যানেল নেই"))
        mirrors = st["mirrors"]
        body = f"""
        <div class="page-heading"><div><p class="eyebrow">EMERGENCY</p><h2>Emergency center</h2></div></div>
        {note}{warnbox}
        <div class="grid">
          <div class="card"><div class="l">Bot</div><div class="n">{_flag(st['bot'], 'অনলাইন', 'অফলাইন')}</div></div>
          <div class="card"><div class="l">Telegram session</div><div class="n">{_flag(st['session'], 'চালু', 'বন্ধ')}</div></div>
          <div class="card"><div class="l">Maintenance</div><div class="n">{_flag(not st['maintenance'], 'স্বাভাবিক', 'বট বন্ধ (ইউজারদের জন্য)')}</div></div>
          <div class="card"><div class="l">Force join</div><div class="n">{_flag(st['force_join'], 'চালু', 'বন্ধ')}</div></div>
          <div class="card"><div class="l">ভিডিও কপি হয়েছে</div><div class="n">{mirrors['mirrored']}/{mirrors['files']}</div>
            <p class="muted">বাকি: {mirrors['missing']}</p></div>
        </div>

        <h3>🛠 মেইনটেন্যান্স মোড</h3>
        <div class="card"><p class="muted">চালু করলে শুধু admin বট ব্যবহার করতে পারবে, ইউজাররা বার্তা পাবে।</p>
          <form method="post" action="/admin/emergency/maintenance" class="row">
            <input type="hidden" name="value" value="{'0' if st['maintenance'] else '1'}">
            <button class="{'ok' if st['maintenance'] else 'bad'}" type="submit">{'বট আবার চালু করুন' if st['maintenance'] else 'বট বন্ধ করুন (মেইনটেন্যান্স)'}</button>
          </form></div>

        <h3>🔗 Force join</h3>
        <div class="card"><p class="muted">Force-join চ্যানেলে সমস্যা হলে ইউজাররা আটকে যায় — এখান থেকে এক ক্লিকে বন্ধ করুন।
          চ্যানেল: <code>{esc(st['force_channel']) or '—'}</code></p>
          <form method="post" action="/admin/emergency/forcejoin" class="row">
            <input type="hidden" name="value" value="{'0' if st['force_join'] else '1'}">
            <button class="{'bad' if st['force_join'] else 'ok'}" type="submit">{'Force join বন্ধ করুন' if st['force_join'] else 'Force join চালু করুন'}</button>
          </form></div>

        <h3>📦 STORAGE_CHANNEL (ভিডিও ক্যাশ চ্যানেল)</h3>
        <div class="card"><p class="muted">এখন: {storage}. প্রতি স্টোরে আলাদা চ্যানেল: {'চালু' if st['per_store'] else 'বন্ধ'}।
          খালি রাখলে বট নিজে চ্যানেল খুলবে। হাতে দিলে (যেমন <code>-1001234567890</code>) সব ভিডিও সেখানেই যাবে।</p>
          <form method="post" action="/admin/emergency/storage">
            <input name="value" value="{esc(st['storage_manual'])}" placeholder="-100… অথবা @channel (খালি = অটো)">
            <button type="submit">সেভ করুন</button>
          </form></div>

        <h3>♻️ ভিডিও আবার কপি করুন</h3>
        <div class="card"><p class="muted">যেসব ভিডিও চ্যানেলে কপি হয়নি ({mirrors['missing']}টি) সেগুলো এখনই ব্যাকগ্রাউন্ডে কপি শুরু হবে।</p>
          <form method="post" action="/admin/emergency/remirror"><button type="submit">কপি শুরু করুন</button></form></div>

        <h3>🗄 ব্যাকআপ</h3>
        <div class="card"><p class="muted">ডাটাবেসের ব্যাকআপ এখনই আপনার টেলিগ্রামে (admin) পাঠানো হবে।</p>
          <form method="post" action="/admin/emergency/backup"><button type="submit">এখনই ব্যাকআপ পাঠান</button></form></div>

        <p class="muted"><a href="/admin/settings">⚙ সব জরুরি সেটিংস দেখুন</a> · বটে: <code>/emergency</code></p>
        """
        return layout("Emergency", body, "/admin/emergency")

    @app.post("/admin/emergency/{action}")
    async def admin_emergency_action(request: Request, action: str):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        value = str(form.get("value", "")).strip()
        message = ""
        if action == "maintenance":
            settings.set("MAINTENANCE_MODE", "1" if value == "1" else "0")
            message = "বট বন্ধ করা হয়েছে (মেইনটেন্যান্স)" if value == "1" else "বট আবার চালু"
        elif action == "forcejoin":
            settings.set("FORCE_JOIN_ENABLED", "1" if value == "1" else "0")
            forcejoin.clear_cache()
            message = "Force join চালু" if value == "1" else "Force join বন্ধ"
        elif action == "storage":
            ok, error = settings.set("STORAGE_CHANNEL", value)
            if not ok:
                return RedirectResponse(f"/admin/emergency?warn={quote_plus(str(error))}", status_code=303)
            message = "STORAGE_CHANNEL সেভ হয়েছে" if value else "STORAGE_CHANNEL খালি — বট নিজে চ্যানেল খুলবে"
        elif action == "remirror":
            from app.services import media_cache
            runtime.spawn(media_cache.warm_mirrors(limit=1000, delay=1.0))
            message = "ভিডিও কপি ব্যাকগ্রাউন্ডে শুরু হয়েছে"
        elif action == "backup":
            from app.services import backup as backup_service
            try:
                result = await backup_service.send_backup("manual")
                message = (f"ব্যাকআপ পাঠানো হয়েছে ({result['size_mb']} MB)" if result["sent"]
                           else "ব্যাকআপ বানানো গেছে কিন্তু টেলিগ্রামে পাঠানো যায়নি")
            except Exception as exc:
                return RedirectResponse(f"/admin/emergency?warn={quote_plus('ব্যাকআপ ব্যর্থ: ' + str(exc)[:120])}",
                                        status_code=303)
        else:
            return RedirectResponse("/admin/emergency?warn=" + quote_plus("অজানা কাজ"), status_code=303)
        return RedirectResponse(f"/admin/emergency?flash={quote_plus(message)}", status_code=303)

    @app.get("/admin/settings/export")
    async def admin_settings_export(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        body = settings.env_preview()
        return Response(body, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition":
                                 'attachment; filename="config.env"'})

    # ----------------------------------------------------------------- health
    # ================================================= channels / composer (v3)
    @app.get("/admin/channels", response_class=HTMLResponse)
    async def admin_channels(request: Request, flash: str = "", warn: str = "",
                             refresh: int = 0):
        blocked = _guard(request)
        if blocked:
            return blocked
        if refresh:
            for row in db.join_channels():
                if not row.get("chat_id"):
                    info = await channels.add_channel(row["ref"], row.get("store_id"))
                    if info.get("chat_id"):
                        db.update_join_channel(row["id"], chat_id=info["chat_id"],
                                               title=info.get("title") or row.get("title"))
        warn = warn or settings.get_str("STORAGE_CHANNEL_WARNING", "")   # cache-channel help
        body = panels.channels_body(flash, warn)
        return layout("Channels", body, "/admin/channels")

    @app.post("/admin/channels/store/{store_id}/cache")
    async def admin_store_cache_channel(request: Request, store_id: int):
        """Create (or re-create) one store's private media-cache channel."""
        blocked = _guard(request)
        if blocked:
            return blocked
        from app.services import media_cache
        store = db.store(store_id)
        if not store:
            return RedirectResponse("/admin/channels?warn=স্টোর+পাওয়া+যায়নি", status_code=303)
        client = next(iter(runtime.user_clients.values()), None)
        if client is None:
            return RedirectResponse(
                "/admin/channels?warn=আগে+Telegram+session+যুক্ত+করুন", status_code=303)
        db.set_meta(f"store_cache_channel:{store_id}", "")       # force a fresh one
        peer = await media_cache.store_cache_peer(client, store)
        if not peer:
            return RedirectResponse(
                "/admin/channels?warn=চ্যানেল+বানানো+গেল+না+—+session/লগ+দেখুন", status_code=303)
        audit_service.record("cache_channel_created", store_id=store_id, ref=str(peer),
                             detail=store["name"])
        return RedirectResponse(
            f"/admin/channels?flash=✅+{esc(store['name'])}+এর+ক্যাশ+চ্যানেল+তৈরি+হলো",
            status_code=303)

    @app.post("/admin/channels/store/{store_id}/copy/{action}")
    async def admin_video_copy_control(request: Request, store_id: int, action: str):
        blocked = _guard(request)
        if blocked:
            return blocked
        from app.services import video_copy
        if not db.store(store_id) or not video_copy.control(store_id, action):
            return RedirectResponse("/admin/channels?warn=কপি+কাজটি+পাওয়া+যায়নি", status_code=303)
        if action in ('resume', 'retry'):
            video_copy.kick()
        audit_service.record("video_copy_control", store_id=store_id, detail=action)
        return RedirectResponse("/admin/channels?flash=কপি+সেটিং+সেভ+হয়েছে", status_code=303)

    @app.post("/admin/channels/store/{store_id}/cache/reset")
    async def admin_store_cache_reset(request: Request, store_id: int):
        """Forget a store's cache channel — the next mirror creates a fresh one.

        This is the one-click cure for a stale pair: the channel may have been
        deleted, or this session never learned it (“Could not find the input entity
        for PeerChannel”). Mirrored copies stay reachable, only the pointer moves.
        """
        blocked = _guard(request)
        if blocked:
            return blocked
        db.set_meta(f"store_cache_channel:{int(store_id)}", "")
        audit_service.record("cache_channel_reset", store_id=store_id, ref=str(store_id))
        return RedirectResponse(
            "/admin/channels?flash=♻️+রিসেট+হলো+—+পরের+ক্যাশ+থেকে+নতুন+চ্যানেল",
            status_code=303)

    @app.post("/admin/channels/add")
    async def admin_channels_add(request: Request, ref: str = Form(""),
                                 store_id: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        scope = int(store_id) if store_id.strip().isdigit() else None
        result = await channels.add_channel(ref, scope)
        if result.get("ok"):
            return RedirectResponse(
                f"/admin/channels?flash=✅+যোগ+হয়েছে:+{esc(result.get('title') or ref)}",
                status_code=303)
        return RedirectResponse(
            f"/admin/channels?warn=চ্যানেল+পাওয়া+গেল+ন:+{esc(result.get('error') or '')}"[:400],
            status_code=303)

    @app.get("/admin/channels/toggle/{channel_id}")
    async def admin_channels_toggle(request: Request, channel_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        row = db.join_channel(channel_id)
        if row:
            db.update_join_channel(channel_id, enabled=0 if row.get("enabled") else 1)
        return RedirectResponse("/admin/channels?flash=✅+আপডেট", status_code=303)

    @app.get("/admin/channels/check/{channel_id}")
    async def admin_channels_check(request: Request, channel_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        row = db.join_channel(channel_id)
        if row is None:
            return RedirectResponse("/admin/channels?warn=চ্যানেল+নেই", status_code=303)
        info = await channels.channel_info(row.get("chat_id") or row["ref"])
        if info.get("ok"):
            db.update_join_channel(channel_id, chat_id=info["chat_id"],
                                   title=info.get("title") or row.get("title"),
                                   last_check=time.time(), error="")
            if info.get("chat_id"):
                db.set_meta(f"chat_title:{info['chat_id']}", info.get("title") or "")
            return RedirectResponse(
                f"/admin/channels?flash=✅+{info.get('title')}+·+{info.get('members')}+সদস্য",
                status_code=303)
        db.update_join_channel(channel_id, last_check=time.time(),
                               error=str(info.get("error") or "")[:180])
        return RedirectResponse(
            f"/admin/channels?warn={esc(str(info.get('error') or 'পাওয়া যায়নি'))}"[:400],
            status_code=303)

    @app.get("/admin/channels/delete/{channel_id}")
    async def admin_channels_delete(request: Request, channel_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.delete_join_channel(channel_id)
        return RedirectResponse("/admin/channels?flash=🗑+মুছে+ফেলা+হলো", status_code=303)

    @app.get("/admin/channels/compose", response_class=HTMLResponse)
    async def admin_compose(request: Request, store: int = 0, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        composer = channels.composer_defaults(store or None)
        return layout("Channel post", panels.compose_body(composer, store or None, flash, warn),
                      "/admin/channels")

    @app.post("/admin/channels/compose")
    async def admin_compose_post(request: Request, target: str = Form(""),
                                 text: str = Form(""), buttons: str = Form(""),
                                 store_id: str = Form(""), file_ids: list[str] = Form(default=[]),
                                 send_test: str = Form(""), link_limit: int = Form(-1)):
        blocked = _guard(request)
        if blocked:
            return blocked
        ids = [int(f) for f in file_ids if str(f).strip().isdigit()]
        scope = int(store_id) if store_id.strip().isdigit() else None
        composer = {"text": text, "buttons": buttons, "store_id": scope}
        if send_test:
            ok = await broadcast.test_send_text(text or "(ফাইল পোস্ট)", buttons or "")
            return layout("Channel post",
                          panels.compose_body(composer, scope,
                                              "🧪 টেস্ট পাঠানো হয়েছে" if ok
                                              else "", "" if ok else "টেস্ট পাঠানো যায়নি"),
                          "/admin/channels")
        if not target.strip():
            return layout("Channel post",
                          panels.compose_body(composer, scope, "",
                                              "চ্যানেল বাছুন বা আগে একটা যোগ করুন"),
                          "/admin/channels")
        result = await channels.publish(target, text=text, file_ids=ids, buttons=buttons,
                                       store_id=scope, link_limit=link_limit)
        if result.get("ok"):
            return layout("Channel post",
                          panels.compose_body(composer, scope,
                                              f"✅ পাঠানো হয়েছে (মেসেজ #{result.get('message_id')})",
                                              ""),
                          "/admin/channels")
        return layout("Channel post",
                      panels.compose_body(composer, scope, "",
                                          f"❌ {result.get('error') or 'পাঠানো যায়নি'}"),
                      "/admin/channels")

    # ============================================================ analytics (v3)
    @app.get("/admin/texts", response_class=HTMLResponse)
    async def admin_texts(request: Request, flash: str = "", warn: str = "",
                          lang: str = "bn", q: str = ""):
        """📝 বটের মেসেজ — change any message the bot sends, from the website."""
        blocked = _guard(request)
        if blocked:
            return blocked
        lang = lang if lang in ("bn", "en") else "bn"
        needle = q.strip().lower()
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        blocks = []
        edited_total = 0
        for group in bot_texts.catalog(lang):
            cards = []
            for row in group["rows"]:
                if needle and needle not in row["key"].lower() \
                        and needle not in row["label"].lower():
                    continue
                if row["edited"]:
                    edited_total += 1
                badge = ('<span class="badge">✏️ এডিট করা</span>' if row["edited"]
                         else '<span class="badge">ডিফল্ট</span>')
                default = esc(row["default"]) or '<span class="muted">(খালি)</span>'
                value = esc(row["value"])
                cards.append(f"""
                <div class="card" style="margin-bottom:12px">
                  <b>{esc(row['label'])}</b> {badge}
                  <p class="muted">কী: <code>{esc(row['key'])}</code></p>
                  <p class="muted">এখন যা যায়:</p>
                  <pre style="white-space:pre-wrap">{esc(row['value'] or row['default'])}</pre>
                  <form method=post action="/admin/texts/save">
                    <input type="hidden" name="key" value="{esc(row['key'])}">
                    <input type="hidden" name="lang" value="{lang}">
                    <textarea name="value" rows="4" placeholder="নতুন লেখা… (খালি রাখলে ডিফল্ট ফিরে আসবে)">{value}</textarea>
                    <p class="muted">ডিফল্ট: {default}</p>
                    <div class="row">
                      <button class="ok" type="submit">💾 সেভ</button>
                      <button class="grey" type="submit" name="value" value="-">♻️ ডিফল্ট ফিরিয়ে দিন</button>
                    </div>
                  </form>
                </div>""")
            if cards:
                blocks.append(f"<h3>{esc(group['group'])}</h3>" + "".join(cards))
        body = f"""
        {note}{warnbox}
        <h3>📝 বটের মেসেজ</h3>
        <p class="muted">বট ইউজারকে যা যা লেখে, সব এখান থেকে বদলানো যায় — কোড এডিট বা
        রিস্টার্ট লাগে না। <code>{{store}}</code>, <code>{{n}}</code> এর মতো
        প্লেসহোল্ডার আগের মতোই কাজ করবে। খালি রেখে সেভ করলে (বা ♻️ চাপলে) ডিফল্ট
        লেখা ফিরে আসে।</p>
        <p class="row">
          <a class="btn {'ok' if lang == 'bn' else 'grey'}" href="/admin/texts?lang=bn">🇧🇩 বাংলা</a>
          <a class="btn {'ok' if lang == 'en' else 'grey'}" href="/admin/texts?lang=en">🇬🇧 English</a>
          <a class="btn grey" href="/admin/settings">🛠 সেটিংস</a>
        </p>
        <form method=get action="/admin/texts" class="row">
          <input type="hidden" name="lang" value="{lang}">
          <input name="q" value="{esc(q)}" placeholder="🔍 মেসেজ খুঁজুন (কী বা নাম)">
          <button class="btn grey" type="submit">খুঁজুন</button>
        </form>
        <p class="muted">{edited_total} টি মেসেজ এখন নিজের লেখায় চলছে।</p>
        {''.join(blocks) or '<p class="muted">কিছু পাওয়া গেল না।</p>'}
        """
        return layout("Bot texts", body, "/admin/texts")

    @app.post("/admin/texts/save")
    async def admin_texts_save(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        key = str(form.get("key", "")).strip()
        lang = str(form.get("lang", "bn")).strip() or "bn"
        value = str(form.get("value", ""))
        ok, error = bot_texts.set(key, value, lang)
        if not ok:
            return RedirectResponse(f"/admin/texts?lang={lang}&warn={esc(error)}",
                                    status_code=303)
        audit_service.record("texts_saved", ref=f"{key}:{lang}",
                             detail=value.strip()[:80] or "ডিফল্ট")
        label = "♻️ ডিফল্ট ফিরে এসেছে" if value.strip() in ("", "-") else "✅ সেভ হয়েছে"
        return RedirectResponse(f"/admin/texts?lang={lang}&flash={esc(label)}",
                                status_code=303)

    @app.get("/admin/stores/{store_id}", response_class=HTMLResponse)
    async def admin_store_detail(request: Request, store_id: int, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        store = db.store(store_id)
        if store is None:
            return RedirectResponse("/admin/stores?warn=স্টোর+নেই", status_code=303)
        return layout(store["name"], panels.store_admin_body(store, flash, warn),
                      "/admin/stores")

    @app.get("/admin/reel", response_class=HTMLResponse)
    async def admin_reel(request: Request, flash: str = "", warn: str = ""):
        """Curate the auto-playing reel / media wall of the public home page."""
        blocked = _guard(request)
        if blocked:
            return blocked
        from app.services import feed as feed_service
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        info = feed_service.stats()
        showing = feed_service.latest(_bot_username())
        rows = "".join(
            f"<tr><td>#{pin['id']}</td><td>{esc(pin.get('title') or '—')}</td>"
            f"<td><span class='pill'>{esc(pin.get('kind') or '')}</span></td>"
            f"<td>{_start(pin.get('added_at'))}</td>"
            f"<td><div class='row'>"
            f"<form method=post action='/admin/reel/up/{pin['id']}'><button class='grey'>⬆️</button></form>"
            f"<form method=post action='/admin/reel/down/{pin['id']}'><button class='grey'>⬇️</button></form>"
            f"<form method=post action='/admin/reel/delete/{pin['id']}'><button class='bad'>🗑</button></form>"
            f"</div></td></tr>"
            for pin in db.feed_pins(limit=50))
        preview = "".join(
            f'<a class="tile" style="aspect-ratio:9/16" href="{esc(item.get("stream") or "")}" '
            f'target="_blank" rel="noopener">'
            f'<img loading="lazy" src="{esc(item.get("thumb") or "")}" alt="">'
            f'<span class="play">{"🖼" if item.get("media_kind") == "photo" else "▶"}</span>'
            f'<span class="tag">{esc(item.get("name") or "")}</span></a>'
            for item in showing)
        body = f"""
        {note}{warnbox}
        <h3>🎞 হোম পেজের রিল</h3>
        <p class="muted">ভিজিটর প্রথমেই যেগুলো দেখে — অটো-প্লে, মিউট হয়ে চলে, নিচে-উপরে
        স্ক্রল করলে পরেরটা। এখানে পিন করা মিডিয়া আগে, তারপর ফ্রি স্টোরের নতুন ভিডিও/ছবি।</p>
        <div class="grid">
          {card("Reel", "চালু ✅" if info['enabled'] else "বন্ধ ⛔")}
          {card("Reel slides", info['count'])}
          {card("Playing at once", info['wall'])}
          {card("Pinned", info['pins'])}
          {card("Total plays", info['plays'])}
        </div>
        <div class="card">
          <h4>সেটিংস</h4>
          <form method=post action="/admin/reel/save">
            <div class="chk"><input type=checkbox name=enabled value=1 {'checked' if info['enabled'] else ''} id=on>
              <label for=on>হোম পেজে রিল দেখাও</label></div>
            <div class="chk"><input type=checkbox name=autoplay value=1 {'checked' if info['autoplay'] else ''} id=ap>
              <label for=ap>নিজে থেকেই চালু হবে (মিউট — ব্রাউজারের নিয়ম)</label></div>
            <div class="chk"><input type=checkbox name=muted value=1 {'checked' if info['muted'] else ''} id=mu>
              <label for=mu>শুরুতে সাউন্ড বন্ধ রাখো</label></div>
            <label>রিলে কয়টা মিডিয়া (১–৮)</label>
            <input name=count type=number min=1 max=8 value="{info['count']}">
            <label>একসাথে কয়টা অটো-প্লে হবে (ওয়াল, ০ = বন্ধ)</label>
            <input name=wall type=number min=0 max=8 value="{info['wall']}">
            <label>মিডিয়া পেজে কতগুলো মিডিয়া (৪–১২০)</label>
            <input name=gallery type=number min=4 max=120 value="{feed_service.gallery_count()}">
            <button class="ok">💾 সেভ করুন</button>
          </form>
        </div>
        <div class="card">
          <h4>এখন যা দেখাচ্ছে ({len(showing)})</h4>
          <div class="wall">{preview or '<p class="muted">এখনো কিছু নেই — ফ্রি স্টোরে ভিডিও/ছবি যোগ করুন, বা বটে <code>/reel add</code> দিয়ে পিন করুন।</p>'}</div>
        </div>
        <h4>📌 পিন করা মিডিয়া</h4>
        <p class="muted">বটে <code>/reel add</code> দিলে বা ইনবক্সের মিডিয়া পিন করলে এখানে আসে।
        উপরে/নিচে সাজানো যায়।</p>
        <table><tr><th>#</th><th>নাম</th><th>ধরন</th><th>যোগ হয়েছে</th><th></th></tr>
        {rows or '<tr><td colspan=5 class=muted>কোনো পিন নেই</td></tr>'}</table>
        <div class="row">
          <a class="btn grey" href="/" target="_blank">🌐 হোম পেজ দেখুন</a>
          <a class="btn grey" href="/admin/files">📥 ফাইল পেজ</a>
        </div>
        """
        return layout("Home reel", body, "/admin/reel")

    @app.post("/admin/reel/save")
    async def admin_reel_save(request: Request, count: int = Form(4), gallery: int = Form(36),
                              wall: int = Form(4), enabled: str = Form(""),
                              autoplay: str = Form(""), muted: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        from app.services import settings as settings_service
        settings_service.set("FEED_ENABLED", "1" if enabled else "0")
        settings_service.set("FEED_AUTOPLAY", "1" if autoplay else "0")
        settings_service.set("FEED_MUTED", "1" if muted else "0")
        settings_service.set("FEED_COUNT", str(max(1, min(8, int(count or 4)))))
        settings_service.set("FEED_WALL", str(max(0, min(8, int(wall or 0)))))
        settings_service.set("FEED_GALLERY_COUNT", str(max(4, min(120, int(gallery or 36)))))
        audit_service.record("reel_saved", ref=f"wall={wall}",
                             detail=f"autoplay={bool(autoplay)} muted={bool(muted)} "
                                    f"count={count} gallery={gallery}")
        return RedirectResponse("/admin/reel?flash=সেভ+হলো", status_code=303)

    @app.post("/admin/reel/up/{pin_id}")
    async def admin_reel_up(request: Request, pin_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.move_feed_pin(pin_id, -1)
        return RedirectResponse("/admin/reel?flash=সরানো+হলো", status_code=303)

    @app.post("/admin/reel/down/{pin_id}")
    async def admin_reel_down(request: Request, pin_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.move_feed_pin(pin_id, 1)
        return RedirectResponse("/admin/reel?flash=সরানো+হলো", status_code=303)

    @app.post("/admin/reel/delete/{pin_id}")
    async def admin_reel_delete(request: Request, pin_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.delete_feed_pin(pin_id)
        return RedirectResponse("/admin/reel?flash=মুছে+ফেলা+হলো", status_code=303)

    @app.get("/admin/analytics", response_class=HTMLResponse)
    async def admin_analytics(request: Request, days: int = 14):
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("Analytics", panels.analytics_body(days), "/admin/analytics")

    @app.get("/admin/analytics/store/{store_id}", response_class=HTMLResponse)
    async def admin_store_analytics(request: Request, store_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("Store analytics", panels.store_analytics_body(store_id),
                      "/admin/analytics")

    @app.get("/admin/analytics/file/{file_id}", response_class=HTMLResponse)
    async def admin_file_watchers(request: Request, file_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("File watchers", panels.file_watchers_body(file_id), "/admin/analytics")

    @app.get("/admin/users/{user_id}", response_class=HTMLResponse)
    async def admin_user_detail(request: Request, user_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout(f"User {user_id}", panels.user_detail_body(user_id), "/admin/users")

    @app.post("/admin/users/{user_id}/message")
    async def admin_user_message(request: Request, user_id: int, text: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        sent = await broadcast.test_send_text(text, "")
        return RedirectResponse(
            f"/admin/users/{user_id}?flash=" + ("✅+পাঠানো+হয়েছে" if sent else "❌+পাঠানো+যায়নি"),
            status_code=303)

    @app.get("/admin/users/{user_id}/ban")
    async def admin_user_ban(request: Request, user_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.ban_user(user_id, reason="banned from the website panel")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    @app.get("/admin/users/{user_id}/unban")
    async def admin_user_unban(request: Request, user_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.unban_user(user_id)
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    # ======================================================= limited links (v3)
    @app.get("/admin/links", response_class=HTMLResponse)
    async def admin_links(request: Request, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("Links", panels.links_body(flash, warn), "/admin/links")

    @app.post("/admin/links/new")
    async def admin_links_new(request: Request, file_ids: list[str] = Form(default=[]),
                              store_id: str = Form(""), max_clicks: int = Form(0),
                              note: str = Form(""), per_user: int = Form(0)):
        blocked = _guard(request)
        if blocked:
            return blocked
        ids = [int(f) for f in file_ids if str(f).strip().isdigit()]
        if not ids:
            return RedirectResponse("/admin/links?warn=ফাইল+বাছুন", status_code=303)
        token = db.create_link(0, ids, None, kind="limited", max_clicks=max(0, max_clicks),
                               note=note, per_user_limit=max(0, per_user))
        label = f"{max_clicks} ক্লিক" if max_clicks else "আনলিমিটেড"
        if per_user:
            label += f" · প্রতি ইউজার {per_user}"
        audit_service.record("link_created", ref=token, detail=label)
        return RedirectResponse(
            f"/admin/links?flash=🔗+তৈরি:+{label}+·+{token}", status_code=303)

    @app.get("/admin/links/delete/{token}")
    async def admin_links_delete(request: Request, token: str):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.delete_link(token)
        return RedirectResponse("/admin/links?flash=🗑+মুছে+ফেলা+হলো", status_code=303)

    @app.post("/admin/stores/{store_id}/forcejoin")
    async def admin_store_forcejoin(request: Request, store_id: int, ref: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        if ref.strip().lower() in ("off", "none", "-", "বন্ধ", "no"):
            ref = ""
        cleaned = forcejoin.normalize(ref) if ref.strip() else ""
        db.set_store_forcejoin(store_id, cleaned)
        forcejoin.clear_cache()
        if cleaned:
            info = await channels.add_channel(cleaned, store_id)
            note = "✅ সেভ হয়েছে" + (f" · {info.get('title')}" if info.get("ok") else
                                    " (চ্যানেল এখনো পাওয়া যায়নি)")
        else:
            note = "✅ এই স্টোরের আলাদা চ্যানেল বন্ধ করা হলো"
        return RedirectResponse(f"/admin/stores?flash={note}", status_code=303)

    def _digest_body(flash: str = "", warn: str = "") -> str:
        """The weekly digest screen: what it is, when it runs, and who got it."""
        from app.services import scheduler as scheduler_service

        week_ago = time.time() - 7 * 86400
        ready: list[tuple[str, int, int]] = []
        for store in db.all_stores():
            fresh = [f for f in db.files_of(store["id"], newest_first=True)
                     if (f.get("created_at") or 0) >= week_ago]
            if fresh:
                ready.append((store["name"], len(fresh),
                              len(broadcast.resolve_audience(store["admin_id"],
                                                             f"store:{store['id']}"))))
        rows = "".join(
            f"<tr><td>{esc(name)}</td><td class='num'>{count}</td>"
            f"<td class='num'>{targets}</td>"
            f"<td>{status_tag('ready' if targets else 'empty')}</td></tr>"
            for name, count, targets in ready)
        table = ui.table(
            ("স্টোর", "নতুন ফাইল (৭ দিন)", "অডিয়েন্স", "অবস্থা"), rows,
            numeric=(1, 2),
            empty_row="গত ৭ দিনে নতুন ফাইল নেই — তাই এই সপ্তাহে ডাইজেস্ট যাবে না।")
        days = ("সোমবার", "মঙ্গলবার", "বুধবার", "বৃহস্পতিবার", "শুক্রবার",
                "শনিবার", "রবিবার")
        weekday = settings.get_int("DIGEST_WEEKDAY", 4)
        hour = settings.get_int("DIGEST_HOUR", 20)
        enabled = settings.get_bool("DIGEST_ENABLED", False)
        max_files = settings.get_int("DIGEST_MAX_FILES", 2)
        note = ui.note(
            f"ডাইজেস্ট চালু আছে — প্রতি <b>{days[weekday % 7]}</b> "
            f"<b>{hour}:00</b>-এ, প্রতি স্টোরের সর্বোচ্চ <b>{max_files}</b>টি নতুন "
            "ফাইল নিয়ে ক্যাম্পেইন তৈরি হবে।" if enabled else
            "ডাইজেস্ট এখন <b>বন্ধ</b>। চালু করতে নিচের সেটিংসে টিক দিন।",
            "ok" if enabled else "warn")
        recent = [c for c in db.campaigns(limit=40)
                  if (c.get("title") or "").startswith("সাপ্তাহিক ডাইজেস্ট")][:8]
        history = ui.timeline([
            (fmt_ts(c.get("created_at") or 0, "%d %b %H:%M"),
             f"#{c['id']} — {esc(c.get('title') or '')} "
             f"<span class='pill'>{esc(c.get('status') or '')}</span>")
            for c in recent]) if recent else ""
        return f"""<form method=post action="/admin/digest/save">
          <div class="card"><h4>⚙️ কখন পাঠাবে</h4>
            <div class="form-grid">
              <div><label>অবস্থা</label>
                <select name=enabled><option value="1"{' selected' if enabled else ''}>
                  চালু</option><option value="0"{'' if enabled else ' selected'}>বন্ধ</option></select></div>
              <div><label>দিন</label>
                <select name=weekday>{''.join(
                  f'<option value="{i}"{" selected" if i == weekday % 7 else ""}>{d}</option>'
                  for i, d in enumerate(days))}</select></div>
              <div><label>সময় (ঘণ্টা)</label>
                <input name=hour type=number min=0 max=23 value="{hour}"></div>
              <div><label>সর্বোচ্চ ফাইল</label>
                <input name=max_files type=number min=1 max=10 value="{max_files}"></div>
            </div>
            <div class="row"><button>💾 সেভ করুন</button></div>
          </div></form>
          <form method=post action="/admin/digest/run">
            <div class="card"><h4>🧪 এই সপ্তাহে কী যাবে</h4>
              {table}
              <p class="muted">“এখনই পাঠান” চাপলে প্রতি স্টোরের জন্য একটি ক্যাম্পেইন
              তৈরি হয় — আগে <a href="/admin/broadcast">ব্রডকাস্ট স্টুডিওতে</a> খসড়া
              হিসেবে দেখে নিতে পারেন, অথবা সরাসরি পাঠাতে পারেন।</p>
              <div class="row">
                <button name=action value="draft" class="grey">📝 খসড়া বানান</button>
                <button name=action value="send">🚀 এখনই পাঠান</button>
              </div>
            </div></form>
          {f'<div class="card"><h4>🕘 আগের ডাইজেস্ট</h4>{history}</div>' if history else ''}
        """

    @app.get("/admin/digest", response_class=HTMLResponse)
    async def admin_digest_page(request: Request, flash: str = "", warn: str = ""):
        """A real page (it used to be a GET that broadcast on click)."""
        blocked = _guard(request)
        if blocked:
            return blocked
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        head = ui.page_head(
            "সাপ্তাহিক ডাইজেস্ট",
            "সপ্তাহে একদিন, প্রতি স্টোরের নতুন ফাইল নিয়ে নিজে থেকেই ব্রডকাস্ট — "
            "কেউ কিছু না করলেও।",
            eyebrow="AUDIENCE",
            actions='<a class="btn grey" href="/admin/broadcast">📢 ব্রডকাস্ট</a>')
        return layout("Weekly digest", head + note + warnbox + _digest_body(),
                      "/admin/digest")

    @app.post("/admin/digest/save")
    async def admin_digest_save(request: Request, enabled: str = Form("0"),
                                weekday: int = Form(4), hour: int = Form(20),
                                max_files: int = Form(2)):
        blocked = _guard(request)
        if blocked:
            return blocked
        settings.set("DIGEST_ENABLED", "1" if enabled in ("1", "on", "true") else "0")
        settings.set("DIGEST_WEEKDAY", str(max(0, min(6, int(weekday)))))
        settings.set("DIGEST_HOUR", str(max(0, min(23, int(hour)))))
        settings.set("DIGEST_MAX_FILES", str(max(1, min(10, int(max_files)))))
        audit_service.record("settings_saved", ref="digest",
                             detail=f"enabled={settings.get_bool('DIGEST_ENABLED', False)}")
        return RedirectResponse("/admin/digest?flash=💾+ডাইজেস্ট+সেটিং+সেভ+হলো",
                                status_code=303)

    @app.post("/admin/digest/run")
    async def admin_digest_run(request: Request, action: str = Form("send")):
        """Send (or draft) this week's digest. Post-only — a link must never do it."""
        blocked = _guard(request)
        if blocked:
            return blocked
        from app.services.scheduler import run_digest
        if (action or "send").strip().lower() == "draft":
            result = await run_digest(force=True, start=False)
        else:
            result = await run_digest(force=True)
        queued = result.get("queued") or []
        if not queued:
            return RedirectResponse(
                "/admin/digest?warn=এই+সপ্তাহে+নতুন+ফাইল+নেই+(বা+অডিয়েন্স+খালি)",
                status_code=303)
        total = sum(item["targets"] for item in queued)
        verb = "খসড়া+হয়েছে" if (action or "").strip().lower() == "draft" else "কিউ+হয়েছে"
        return RedirectResponse(
            f"/admin/digest?flash=🆕+ডাইজেস্ট+{verb}:+{len(queued)}+স্টোর,+{total}+ইউজার",
            status_code=303)

    # ==================================================== tools: test / logs (v3)
    @app.get("/admin/security", response_class=HTMLResponse)
    async def admin_security(request: Request, flash: str = ""):
        """🔐 টোকেন/সিক্রেট কোথাও পাবলিকভাবে পড়া যায় কি না — আর ঠিক করার ধাপে ধাপে।"""
        blocked = _guard(request)
        if blocked:
            return blocked
        report = secrets_guard.audit()
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        files = report.get("repo_files") or []
        rows = "".join(
            f"<tr><td><code>{esc(item['file'])}</code></td><td>{item['line']}</td>"
            f"<td><code>{esc(item['preview'])}</code></td>"
            f"<td>{'🚨 গত কমিটেও আছে' if item.get('committed') else '⚠️ ফাইলে আছে'}</td></tr>"
            for item in files)
        env_names = ", ".join(report.get("secrets") or []) or "— কিছু নেই —"
        private_names = ", ".join(report.get("private") or []) or "— কিছু নেই —"
        env_keys = ", ".join(report.get("in_environment") or []) or "— কিছু নেই —"
        clean = not files and not report.get("tracked")
        box = ('<div class="flash">✅ দারুণ — রিপোতে সরাসরি কোনো টোকেন/সিক্রেট পাওয়া যায়নি, '
               'আর config.env কমিটও হয় না।</div>' if clean else
               '<div class="warnbox">🚨 নিচের বিষয়গুলো এখনই ঠিক করা দরকার।</div>')
        history = ("⚠️ হ্যাঁ — পুরোনো কমিটে config.env-এর মান এখনো পড়া যায়"
                   if report.get("in_history") else "না")
        body = f"""
        {note}{box}
        <h3>🔐 নিরাপত্তা (টোকেন ও সিক্রেট)</h3>
        <p class="muted">রিপো পাবলিক (GitHub) — তাই <code>config.env</code> কখনো কমিট করা যায় না।
        টোকেন শুধু হোস্টিং প্যানেলের <b>Environment Variables</b> বা ডিস্কের
        <code>config.env</code>-এ থাকবে (ফাইলটি <code>.gitignore</code>-এ আছে)।</p>
        <div class="grid">
          {card("config.env কমিট হয়েছে?", "🚨 হ্যাঁ" if report.get("tracked") else "✅ না")}
          {card("গিট ইতিহাসে পড়া যায়?", history)}
          {card("env-এ থাকা গোপন মান", esc(env_names))}
          {card("সার্ভারে সেট করা env", esc(env_keys))}
        </div>
        <p class="muted">ব্যক্তিগত (কম গোপন, তবু পাবলিক না করা ভালো): {esc(private_names)}</p>
        <h4>🚨 রিপোর ফাইলে সরাসরি লেখা সিক্রেট</h4>
        {f'<table><tr><th>ফাইল</th><th>লাইন</th><th>মান (লুকানো)</th><th>অবস্থা</th></tr>'
          f'{rows}</table>' if files else '<p class="muted">কিছু পাওয়া যায়নি।</p>'}
        <h4>✅ ঠিক করার ধাপ</h4>
        <ol>
          <li>BotFather → <code>/mybots</code> → আপনার বট → API Token → <b>Revoke</b>,
              তারপর নতুন টোকেন কপি করুন।</li>
          <li>পুরোনো টোকেন/হ্যাশ রিপো থেকে <b>মুছে ফেলুন</b> (উপরের তালিকার ফাইল), সেভ করে কমিট করুন।</li>
          <li>Telegram → Settings → Devices → অচেনা সেশন <b>Terminate</b>।</li>
          <li>নতুন টোকেন হোস্টিং প্যানেল → Environment Variables-এ বসান
              (<code>BOT_TOKEN</code>, <code>API_HASH</code>, <code>API_ID</code>),
              ডিস্কের <code>config.env</code>-এও রাখতে পারেন — কিন্তু কমিট করবেন না।</li>
          <li>রিস্টার্ট দিন — বট নতুন টোকেন নিয়েই চলবে।</li>
        </ol>
        <p class="row">
          <a class="btn grey" href="/admin/security">🔄 আবার পরীক্ষা করুন</a>
          <a class="btn grey" href="/admin/self-test">🩺 Health</a>
        </p>
        """
        return layout("Security", body, "/admin/security")

    @app.get("/admin/self-test", response_class=HTMLResponse)
    async def admin_self_test(request: Request, flash: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        checks = await panels.self_test()
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        return layout("Self test", note + panels.self_test_body(checks), "/admin/self-test")

    # ------------------------------------------------------------ extra bots
    @app.get("/admin/bots", response_class=HTMLResponse)
    async def admin_bots(request: Request, flash: str = "", warn: str = ""):
        """🤖 Several bots, one database — add, start, stop, drop."""
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("Bots", panels.bots_body(flash=flash, warn=warn), "/admin/bots")

    @app.post("/admin/bots/add")
    async def admin_bots_add(request: Request, token: str = Form(""), label: str = Form(""),
                             admin_id: str = Form(""), api_id: str = Form(""),
                             note: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        from app.services import botrunner
        try:
            report = botrunner.add(
                token, label=label.strip(), note=note.strip(),
                admin_id=int(admin_id) if admin_id.strip().isdigit() else 0,
                api_id=int(api_id) if api_id.strip().isdigit() else 0)
        except Exception as exc:                       # never a 500 in the panel
            log.warning("adding a bot failed: %s", exc)
            return RedirectResponse(f"/admin/bots?warn=বট যোগ করা যায়নি: {str(exc)[:120]}",
                                    status_code=303)
        if not report.get("ok"):
            return RedirectResponse(f"/admin/bots?warn={report['error'][:160]}", status_code=303)
        row = db.bots_store.bot(int(report["id"]))
        started = botrunner.start(row) if row else {"ok": False}
        audit_service.record("bot_added", ref=str(report["id"]),
                             detail=(label or "")[:80])
        if started.get("ok"):
            return RedirectResponse(
                f"/admin/bots?flash=✅ বট #{report['id']} যোগ হয়ে চালু হয়েছে "
                f"(pid {started.get('pid')})", status_code=303)
        return RedirectResponse(
            f"/admin/bots?warn=বট যোগ হয়েছে কিন্তু চালু হয়নি: "
            f"{(started.get('error') or '')[:120]}", status_code=303)

    @app.get("/admin/bots/{bot_id}/{action}")
    async def admin_bots_action(request: Request, bot_id: int, action: str):
        blocked = _guard(request)
        if blocked:
            return blocked
        from app.services import botrunner
        row = db.bots_store.bot(bot_id)
        if row is None:
            return RedirectResponse("/admin/bots?warn=বটটা খুঁজে পাওয়া যায়নি", status_code=303)
        if action == "start":
            report = botrunner.start(row)
        elif action == "stop":
            report = botrunner.stop(row)
        elif action == "restart":
            report = botrunner.restart(row)
        elif action == "delete":
            botrunner.stop(row)
            db.bots_store.delete_bot(bot_id)
            audit_service.record("bot_removed", ref=str(bot_id), detail=action)
            return RedirectResponse(
                f"/admin/bots?flash=🗑 বট #{bot_id} বাদ দেওয়া হয়েছে "
                "(BotFather-এ /revoke দিয়ে টোকেনটা বাতিল করে দিন)", status_code=303)
        else:
            return RedirectResponse("/admin/bots?warn=চেনা যায়নি", status_code=303)
        if action != "stop":
            audit_service.record(f"bot_{action}", ref=str(bot_id))
        if report.get("ok"):
            return RedirectResponse(f"/admin/bots?flash=✅ {action} ঠিক হয়েছে", status_code=303)
        return RedirectResponse(f"/admin/bots?warn={(report.get('error') or '')[:140]}",
                                status_code=303)

    # ---------------------------------------------------------------- admins
    @app.get("/admin/admins", response_class=HTMLResponse)
    async def admin_admins(request: Request, flash: str = "", warn: str = ""):
        """👥 Who may run this panel and the bot — add or remove admins."""
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("Admins", panels.admins_body(flash=flash, warn=warn), "/admin/admins")

    @app.post("/admin/admins/add")
    async def admin_admins_add(request: Request, user_id: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        if not user_id.strip().isdigit():
            return RedirectResponse("/admin/admins?warn=টেলিগ্রাম আইডি শুধু সংখ্যা হবে",
                                    status_code=303)
        added = credentials.add_admin(int(user_id.strip()))
        audit_service.record("admin_added", ref=user_id.strip())
        if not added:
            return RedirectResponse("/admin/admins?warn=ইনি তো আগেই অ্যাডমিন", status_code=303)
        # A friendly hello, so the new admin knows what just happened.
        from app.runtime import bot
        try:
            await bot.send_message(
                int(user_id.strip()),
                "👋 <b>আপনাকে অ্যাডমিন করা হয়েছে</b>\n"
                "এখন থেকে আপনি এই বটের সব কিছু চালাতে পারবেন — স্টোর, ব্রডকাস্ট, ব্যাকআপ।\n"
                "শুরু করতে বটে <code>/start</code> দিন।")
        except Exception as exc:
            log.debug("welcome message to the new admin failed: %s", exc)
            return RedirectResponse(
                "/admin/admins?flash=✅ যোগ হয়েছে (স্বাগতম মেসেজটা যায়নি — উনি বটে /start "
                "দিলে পাবেন)", status_code=303)
        return RedirectResponse(f"/admin/admins?flash=✅ {user_id} এখন অ্যাডমিন — স্বাগতম "
                                "মেসেজ পাঠানো হয়েছে", status_code=303)

    @app.get("/admin/admins/remove/{user_id}")
    async def admin_admins_remove(request: Request, user_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        removed = credentials.remove_admin(user_id)
        if not removed:
            return RedirectResponse("/admin/admins?warn=মূল অ্যাডমিনকে সরানো যায় না",
                                    status_code=303)
        audit_service.record("admin_removed", ref=str(user_id))
        return RedirectResponse(f"/admin/admins?flash=🗑 {user_id} কে সরানো হয়েছে",
                                status_code=303)

    # -------------------------------------------------------------- visitors
    @app.get("/admin/visitors", response_class=HTMLResponse)
    async def admin_visitors(request: Request, days: int = 14, tab: str = "live"):
        """📈 Live visitor analytics — who is here now and what they are doing."""
        blocked = _guard(request)
        if blocked:
            return blocked
        body = panels.visitors_body(days=days, tab=tab or "live")
        return layout("Visitors", body, "/admin/visitors")

    @app.get("/admin/visitors.json")
    async def admin_visitors_json(request: Request, minutes: int = 5):
        """The live numbers, for the page's own auto-refresh (and any script)."""
        blocked = _guard(request)
        if blocked:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        data = visitors.live(minutes=minutes)
        return JSONResponse({
            "online": data["online"], "window_minutes": data["window_minutes"],
            "views": data["views"], "top_source": data.get("source"),
            "top_country": data.get("country"), "top_device": data.get("device"),
            "rows": [{"vid": row.get("vid"), "page": row.get("landing"),
                      "device": row.get("device"), "browser": row.get("browser"),
                      "country": row.get("country"), "idle": row.get("idle"),
                      "user_id": row.get("user_id"), "ip": row.get("ip")}
                     for row in data["rows"]],
            "now": int(time.time()),
        }, headers={"Cache-Control": "no-store"})

    @app.get("/admin/visitors.csv")
    async def admin_visitors_csv(request: Request, days: int = 30):
        """A spreadsheet of the visits — for the owner's own record keeping."""
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = visitors.csv_rows(days=days)
        body = "\r\n".join(",".join('"' + str(cell).replace('"', '""') + '"' for cell in row)
                            for row in rows)
        return Response("\ufeff" + body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="visitors-{days}d.csv"'})

    @app.post("/admin/visitors/prune")
    async def admin_visitors_prune(request: Request, days: int = Form(90)):
        blocked = _guard(request)
        if blocked:
            return blocked
        report = visitors.prune(days=max(1, min(3650, int(days or 90))))
        audit_service.record("visitors_pruned", detail=f"{report['removed']} rows / {days} days")
        return RedirectResponse(
            f"/admin/visitors?flash=🧹 {report['removed']}টি পুরোনো রেকর্ড মুছে ফেলা হয়েছে",
            status_code=303)

    @app.get("/admin/visitors/media/{file_id}", response_class=HTMLResponse)
    async def admin_visitor_media(request: Request, file_id: int):
        """Who watched this particular video — names, times, devices."""
        blocked = _guard(request)
        if blocked:
            return blocked
        row = db.file(file_id)
        events = db.visitors.media_watchers(file_id, limit=200)
        body = (f"<div class='card'><h3>🎬 {esc((row or {}).get('name') or f'ফাইল #{file_id}')}</h3>"
                f"<p class='muted'>এই মিডিয়া কে কে চালিয়েছে / খুলেছে</p>"
                + ui.table(["সময়", " ভিজিটর", "ইউজার", "কী করল", "পেজ"],
                           [[fmt_ts(row2.get("ts")), f"<a href='/admin/visitors/"
                             f"{esc(row2.get('vid') or '')}'>{esc(str(row2.get('vid') or ''))[:8]}…</a>",
                             esc(row2.get("user_name") or (row2.get("user_id") and
                                                           f"#{row2['user_id']}") or "—"),
                             esc(visitors.EVENT_LABELS.get(row2.get("name") or "",
                                                           row2.get("name") or "")),
                             f"<code>{esc(row2.get('path') or '')}</code>"]
                            for row2 in events],
                           empty_row="এখনো কেউ দেখেনি")
                + "</div>")
        return layout("Visitors", body, "/admin/visitors")

    @app.get("/admin/visitors/{vid}", response_class=HTMLResponse)
    async def admin_visitor_detail(request: Request, vid: str):
        """One visitor's whole stay: every page, every action, in order."""
        blocked = _guard(request)
        if blocked:
            return blocked
        data = visitors.who_is_this(vid)
        visit = data.get("visit") or {}
        if not visit:
            return layout("Visitors", ui.empty("🔍", "এই ভিজিটর আর নেই",
                                               "৯০ দিনের পুরোনো রেকর্ড মুছে যেতে পারে।"),
                          "/admin/visitors")
        who = visit.get("user_id") and (f"#{visit['user_id']}") or "অজানা ভিজিটর"
        head = ui.kpi_row(
            ui.kpi("পেজভিউ", visit.get("pageviews") or 0),
            ui.kpi("শুরু", fmt_ts(visit.get("started_at"), "%d %b %H:%M")),
            ui.kpi("সর্বশেষ", fmt_ts(visit.get("last_seen"), "%d %b %H:%M")),
            ui.kpi("ডিভাইস", visit.get("device") or "—", sub=visit.get("browser") or ""),
        )
        pages = ui.timeline([(fmt_ts(row.get("ts"), "%H:%M:%S"),
                              f"<code>{esc(row.get('path') or '')}</code> "
                              f"<span class='muted'>{int(row.get('ms') or 0)} ms · "
                              f"{int(row.get('status') or 0)}</span>")
                             for row in data.get("pages") or []]) or ui.empty("📄", "পেজ নেই")
        events = ui.timeline([(fmt_ts(row.get("ts"), "%H:%M:%S"),
                               esc(visitors.EVENT_LABELS.get(row.get("name") or "",
                                                             row.get("name") or ""))
                               + f" <span class='muted'>{esc(row.get('detail') or '')}</span>")
                              for row in data.get("events") or []]) or ui.empty("🕐", "কাজ নেই")
        body = (head
                + f"""<div class="card"><h3>👤 {esc(who)}</h3>
                <div class="kv">
                  <div><span>ভিজিট আইডি</span><b><code>{esc(vid)}</code></b></div>
                  <div><span>আইপি</span><b><code>{esc(visit.get('ip') or '—')}</code></b></div>
                  <div><span>দেশ/ভাষা</span><b>{esc(visit.get('country') or '—')}</b></div>
                  <div><span>সোর্স</span><b>{esc(visit.get('source') or '—')}</b></div>
                  <div><span>রেফারার</span><b>{esc((visit.get('referrer') or '—')[:80])}</b></div>
                  <div><span>ল্যান্ডিং</span><b><code>{esc(visit.get('landing') or '')}</code></b></div>
                  <div><span>ব্রাউজার</span><b>{esc(visit.get('os') or '')} · {esc(visit.get('browser') or '')}</b></div>
                  <div><span>ইউজার-এজেন্ট</span><b>{esc((visit.get('user_agent') or '')[:90])}</b></div>
                </div>
                <form method=post action="/admin/visitors/{esc(vid)}/forget">
                  <button class="btn bad">🗑 এই ভিজিটরকে মুছে ফেলুন</button>
                </form></div>
                <div class="card"><h3>📄 পেজের ধারাবাহিকতা</h3>{pages}</div>
                <div class="card"><h3>🕐 কাজ</h3>{events}</div>""")
        return layout("Visitors", body, "/admin/visitors")

    @app.post("/admin/visitors/{vid}/forget")
    async def admin_visitor_forget(request: Request, vid: str):
        blocked = _guard(request)
        if blocked:
            return blocked
        removed = visitors.forget(vid)
        audit_service.record("visitor_forgotten", ref=vid, detail=f"{removed} rows")
        return RedirectResponse("/admin/visitors?flash=🗑 রেকর্ড মুছে ফেলা হয়েছে",
                                status_code=303)

    @app.get("/admin/audit", response_class=HTMLResponse)
    async def admin_audit(request: Request, name: str = "", days: int = 30):
        """Who did what — the trail an owner needs when several people share the panel."""
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("Admin audit", panels.audit_body(name=name, days=days), "/admin/audit")

    @app.get("/admin/logs", response_class=HTMLResponse)
    async def admin_logs(request: Request, level: str = "", lines: int = 300):
        blocked = _guard(request)
        if blocked:
            return blocked
        content: list[str] = []
        try:
            from pathlib import Path
            path = Path(cfg.LOG_FILE)
            if path.exists():
                raw = path.read_text(errors="ignore").splitlines()[-max(50, min(lines, 2000)):]
                if level:
                    raw = [line for line in raw if level.upper() in line.upper()]
                content = raw
        except Exception as exc:
            content = [f"লগ পড়া যায়নি: {exc}"]
        return layout("Logs", panels.logs_body(content, level), "/admin/settings")

    @app.get("/admin/cache/warm")
    async def admin_cache_warm(request: Request, limit: int = 15):
        blocked = _guard(request)
        if blocked:
            return blocked
        from app.services.media_cache import warm_mirrors
        result = await warm_mirrors(limit=limit)
        return RedirectResponse(
            f"/admin/self-test?flash=ক্যাশ:+{result['mirrored']}+হয়েছে,+{result['missing']}+বাকি",
            status_code=303)

    @app.get("/admin/files/dedupe")
    async def admin_files_dedupe(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        removed = db.purge_duplicate_files()
        return RedirectResponse(f"/admin/files?flash=🧹+{removed}+টি+ডুপ্লিকেট+মুছে+ফেলা+হলো",
                                status_code=303)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        """Who am I, where am I listening, and is the bot online?

        Hosting panels and the live-URL gateway poll this, so it also reports the
        port we bound and the prefix we are served under (`/live/<job>`), which is
        exactly what to check when a panel says “no web listener yet”.
        """
        if not _authed(request):
            # Public callers (hosting panels, visitors) only learn that the site is up.
            return JSONResponse({"ok": True, "bot_online": runtime.bot_online()})
        stats = db.stats()
        return JSONResponse({
            "ok": True,
            "uptime_seconds": int(time.time() - STARTED_AT),
            "bot_online": runtime.bot_online(),
            "bot_username": _bot_username(),
            "users": stats["users"],
            "files": stats["files"],
            "stores": stats["stores"],
            "pending_orders": stats["orders_pending"],
            "time": int(time.time()),
            "host": cfg.WEB_HOST,
            "port": cfg.WEB_PORT,
            "from_port_env": cfg.HOSTED,
            "url_prefix": basepath.current() or basepath.normalize(
                request.headers.get("x-forwarded-prefix") or ""),
            "web_enabled": cfg.WEB_ENABLED,
        })

    return app


app = create_app()


def main() -> int:
    setup_logging()
    db.bind(cfg.DB_FILE)
    db.migrate_legacy(cfg.LEGACY_DB_FILE)
    # The website alone must also honour credentials saved from the setup wizard:
    # without this, `python -m web.dashboard` bounced every admin page to /setup
    # even though the bot token was already in the database.
    credentials.load()
    import uvicorn
    log.info("Website on http://%s:%s (password-only admin login enabled)",
             cfg.WEB_HOST, cfg.WEB_PORT)
    uvicorn.run(app, host=cfg.WEB_HOST, port=cfg.WEB_PORT, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
