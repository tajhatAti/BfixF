"""Logging setup: rotating file + console, plus admin-facing error alerts."""
from __future__ import annotations

import logging
import logging.handlers
import traceback
from pathlib import Path

from app import config as cfg

LOGGER_NAME = "storebot"
log = logging.getLogger(LOGGER_NAME)

# Set by bot.py once the Telegram clients exist; used to push crash reports.
error_notifier = None


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    if log.handlers:
        return log
    log.setLevel(level)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)

    try:
        Path(cfg.LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        log.addHandler(file_handler)
    except Exception as exc:  # pragma: no cover - disk issues on cheap hosting
        log.warning("Could not open log file %s: %s", cfg.LOG_FILE, exc)

    logging.getLogger("telethon").setLevel(logging.WARNING)
    return log


async def notify_admins(text: str) -> None:
    """Best effort admin alert; never raises."""
    if error_notifier is None:
        return
    try:
        await error_notifier(text)
    except Exception:  # pragma: no cover
        log.warning("notify_admins failed")


def describe_exception(exc: BaseException) -> str:
    tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    return tb[:300]
