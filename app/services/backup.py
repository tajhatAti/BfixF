"""Send the database backup to the admins' Telegram, so it survives a lost server disk."""
from __future__ import annotations

import os
import time
from pathlib import Path

from app import config as cfg
from app.logger import log
from app.runtime import bot
from app.services.telegram import safe_call
from app.storage import db


async def send_existing(json_path: str, reason: str = "daily", admin_ids=None) -> int:
    """Send the SQLite copy written next to `json_path` (else the JSON) to every admin."""
    base = Path(json_path)
    sqlite_copy = base.with_suffix(".sqlite3")
    file_path = sqlite_copy if sqlite_copy.exists() else base
    if not file_path.exists():
        return 0
    caption = f"🗄 ব্যাকআপ ({reason}) · {time.strftime('%d %b %Y %H:%M')}"
    sent = 0
    for admin_id in (admin_ids or cfg.ADMIN_IDS):
        try:
            await safe_call(bot.send_file, int(admin_id), str(file_path), caption=caption,
                            force_document=True, what="backup", retries=1)
            sent += 1
        except Exception as exc:
            log.warning("backup could not be sent to %s: %s", admin_id, exc)
    return sent


async def send_backup(reason: str = "manual", admin_ids=None) -> dict:
    """Write a fresh backup and send it. Returns {path, sent, size_mb}."""
    path = db.backup_json(cfg.BACKUP_DIR, cfg.KEEP_BACKUPS)
    sent = await send_existing(path, reason, admin_ids)
    sqlite_copy = Path(path).with_suffix(".sqlite3")
    size = os.path.getsize(sqlite_copy) if sqlite_copy.exists() else os.path.getsize(path)
    return {"path": path, "sent": sent, "size_mb": round(size / 1048576, 2)}
