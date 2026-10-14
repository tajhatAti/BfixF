"""Configuration loader for the store bot.

Priority (highest first):
    1. Real environment variables  (export BOT_TOKEN=... / panel env settings)
    2. A local `config.env` file    (KEY=VALUE lines, git-ignored)
    3. Safe defaults below

⚠️  Never put real tokens in this file or anywhere else inside the repo.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


# Load config.env once, but never overwrite real env vars.
_file_values = _parse_env_file(Path(os.getenv("BOT_ENV_FILE", BASE_DIR / "config.env")))
for _k, _v in _file_values.items():
    os.environ.setdefault(_k, _v)


def get_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def get_int(name: str, default: int) -> int:
    try:
        return int(float(get_str(name) or default))
    except (TypeError, ValueError):
        return default


def get_float(name: str, default: float) -> float:
    try:
        return float(get_str(name) or default)
    except (TypeError, ValueError):
        return default


def get_bool(name: str, default: bool = False) -> bool:
    raw = get_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "ha", "hae"}


def get_id_list(name: str, default: str = "") -> list[int]:
    raw = get_str(name) or default
    ids: list[int] = []
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            continue
    return ids


# ---------------------------------------------------------------- credentials
API_ID = get_int("API_ID", 0)
API_HASH = get_str("API_HASH")
BOT_TOKEN = get_str("BOT_TOKEN")

# Admins who may use the control panel. First one is the "primary" admin and
# receives the pre-filled STRING_SESSION on first boot.
ADMIN_IDS: list[int] = get_id_list("ADMIN_IDS")

# Optional pre-filled userbot session (only used the very first time).
STRING_SESSION = get_str("STRING_SESSION")

# ------------------------------------------------------------------ behaviour
TZ_OFFSET = get_float("TZ_OFFSET", 6.0)          # Bangladesh Standard Time
FORCE_CHANNEL = get_str("FORCE_CHANNEL")          # "@mychannel" or ""
CUSTOM_CAPTION = get_str("CUSTOM_CAPTION")

GROUPS_PAGE_SIZE = get_int("GROUPS_PAGE_SIZE", 8)
CONTENT_PAGE_SIZE = get_int("CONTENT_PAGE_SIZE", 12)
USERS_PAGE_SIZE = get_int("USERS_PAGE_SIZE", 10)
MANAGE_PAGE_SIZE = get_int("MANAGE_PAGE_SIZE", 10)
SEARCH_RESULT_LIMIT = get_int("SEARCH_RESULT_LIMIT", 12)

FORCE_JOIN_CACHE_SECONDS = get_int("FORCE_JOIN_CACHE_SECONDS", 600)
JIT_TIMEOUT_SECONDS = get_int("JIT_TIMEOUT_SECONDS", 10)
CLEANUP_JIT_COPY = get_bool("CLEANUP_JIT_COPY", True)
# Old behaviour: forward through the owner's account when the bot has no copy.
# Off by default — the media-cache channel replaces it.
ALLOW_JIT_FORWARD = get_bool("ALLOW_JIT_FORWARD", False)
DRIP_SEND_DELAY = get_float("DRIP_SEND_DELAY", 0.5)
FLOOD_SAFETY_MAX = get_int("FLOOD_SAFETY_MAX", 900)   # wait up to 15 min on FloodWait

# ------------------------------------------------------ broadcast studio (v3)
# A campaign sends up to BROADCAST_MAX_FILES files per user.
BROADCAST_MAX_FILES = get_int("BROADCAST_MAX_FILES", 5)
# Explicit queued jobs always start when the bot reconnects; this gates only jobs
# whose status was `running` when the process stopped.
BROADCAST_AUTO_RESUME = get_bool("BROADCAST_AUTO_RESUME", True)

# ---------------------------------------------------------------------- paths
DB_FILE = get_str("DB_FILE", str(BASE_DIR / "bot_data.sqlite3"))
LEGACY_DB_FILE = get_str("LEGACY_DB_FILE", str(BASE_DIR / "bot_db.json"))
BACKUP_DIR = get_str("BACKUP_DIR", str(BASE_DIR / "backups"))
LOG_FILE = get_str("LOG_FILE", str(BASE_DIR / "logs" / "bot.log"))
SESSION_STATE_FILE = get_str("SESSION_STATE_FILE", str(BASE_DIR / "bot_master.session"))

# How long a pending question ("send me the channel link", "how many clicks?")
# stays armed. Kept short on purpose — see app/handlers/state.py.
FLOW_TIMEOUT_SECONDS = get_int("FLOW_TIMEOUT_SECONDS", 600)
BACKUP_HOUR = get_int("BACKUP_HOUR", 4)           # local hour for the daily JSON export
KEEP_BACKUPS = get_int("KEEP_BACKUPS", 7)


# ------------------------------------------------------------------ payments
CURRENCY = get_str("CURRENCY", "৳")
PAY_BKASH = get_str("PAY_BKASH")          # e.g. "01712-345678 (personal)"
PAY_NAGAD = get_str("PAY_NAGAD")
PAY_ROCKET = get_str("PAY_ROCKET")
PAY_UPI = get_str("PAY_UPI")
PAY_CRYPTO = get_str("PAY_CRYPTO")
PAY_NOTE = get_str("PAY_NOTE")            # extra instructions shown to buyers
TRIAL_HOURS = get_int("TRIAL_HOURS", 24)  # free trial length (0 disables trials)
AUTO_APPROVE_ZERO = get_bool("AUTO_APPROVE_ZERO", True)  # auto-grant free plans

# ------------------------------------------------------------------- support
SUPPORT_CONTACT = get_str("SUPPORT_CONTACT")   # "@username" or numeric id
SUPPORT_NOTE = get_str("SUPPORT_NOTE")         # shown under the contact button
TICKET_COOLDOWN = get_int("TICKET_COOLDOWN", 60)   # seconds between user tickets

# -------------------------------------------------------------------- inline
INLINE_ENABLED = get_bool("INLINE_ENABLED", True)
INLINE_RESULTS = get_int("INLINE_RESULTS", 20)

# --------------------------------------------------------------- web dashboard
# The product is website *and* bot: the site is on unless the owner switches it
# off, and any hosted run (a panel always injects $PORT) is always on — that is
# what makes the live URL appear.
WEB_ENABLED = get_bool("WEB_ENABLED", True)
# Hosting panels inject HOST=0.0.0.0 for web-capable jobs (the runner does it),
# so follow that when the owner did not set WEB_HOST explicitly.
WEB_HOST = get_str("WEB_HOST", get_str("HOST", "0.0.0.0"))
# Hosting panels (CodeNest, Render, Railway, …) inject $PORT and route to it.
WEB_PORT = get_int("PORT", get_int("WEB_PORT", 8080))
WEB_SESSION_HOURS = get_int("WEB_SESSION_HOURS", 72)
WEB_TITLE = get_str("WEB_TITLE", "Ahad OrgEx")
SITE_URL = get_str("SITE_URL", "https://ahadorgex.onrender.com")
#: True when a hosting panel gave us a port to listen on (CodeNest/RunSpace,
#: Render, Railway…). The website must then start even if WEB_ENABLED was
#: switched off, otherwise the panel shows “no web listener”.
HOSTED = bool(get_str("PORT"))
# Public site settings
SITE_ENABLED = get_bool("SITE_ENABLED", True)
SITE_TAGLINE = get_str("SITE_TAGLINE", "")
SITE_CONTACT = get_str("SITE_CONTACT", "")

# ----------------------------------------------------------------- localization
DEFAULT_LANG = get_str("DEFAULT_LANG", "bn")


def validate() -> list[str]:
    """Return a list of human readable problems (empty list == all good)."""
    problems: list[str] = []
    if not API_ID:
        problems.append("API_ID is missing")
    if not API_HASH:
        problems.append("API_HASH is missing")
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN is missing")
    if not ADMIN_IDS:
        problems.append("ADMIN_IDS is empty — add at least one Telegram numeric ID")
    if ":" not in BOT_TOKEN and BOT_TOKEN:
        problems.append("BOT_TOKEN does not look like a bot token")
    return problems
