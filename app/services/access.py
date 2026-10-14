"""Access control: admins, premium grants, broadcast targeting."""
from __future__ import annotations

import time

from app import config as cfg
from app.storage import db
from app.utils import human_delta


def is_admin(user_id: int) -> bool:
    return user_id in cfg.ADMIN_IDS


def has_access(store: dict | int | None, user_id: int) -> bool:
    """Admins always pass. Banned users never pass. Free stores are open to everyone.
    Premium stores need a non-expired grant — checked in the database, not in memory."""
    if store is None:
        return False
    if is_admin(user_id):
        return True
    # Banned users cannot access anything
    try:
        if db.is_banned(user_id):
            return False
    except Exception:
        pass
    if isinstance(store, int):
        store = db.store(store)
        if store is None:
            return False
    if not store.get("is_premium"):
        return True
    row = db.grant_row(store["id"], user_id)
    if not row:
        return False
    expires_at = row["expires_at"]
    return expires_at is None or expires_at > time.time()


def access_note(store: dict, user_id: int) -> str:
    if is_admin(user_id):
        return "👑 Admin"
    if not store.get("is_premium"):
        return "🌐 Free store"
    row = db.grant_row(store["id"], user_id)
    if not row:
        return "🔒 No access"
    if row["expires_at"] is None:
        return "♾ Lifetime access"
    left = row["expires_at"] - time.time()
    if left <= 0:
        return "⌛ Expired"
    return f"⏳ {human_delta(left)} left"


def grant_access(store_id: int, user_id: int, expires_at: float | None,
                 source: str = "admin") -> None:
    db.grant(store_id, user_id, expires_at, source)


def grant_days(store_id: int, user_id: int, days: int, source: str = "admin") -> float:
    return db.extend_grant(store_id, user_id, days, source)


def is_premium_anywhere(user_id: int, admin_id: int | None = None) -> bool:
    """True when the user holds at least one still valid grant."""
    now = time.time()
    for grant in db.user_grants(user_id):
        if grant["expires_at"] is None or grant["expires_at"] > now:
            if admin_id is None:
                return True
            store = db.store(grant["store_id"])
            if store and store["admin_id"] == admin_id:
                return True
    return False


def accessible_stores(admin_id: int, user_id: int) -> list[dict]:
    return [s for s in db.stores_admin(admin_id) if has_access(s, user_id)]


def broadcast_targets(admin_id: int, filter_value: str) -> list[int]:
    """`all` | `premium` | `free` | `store:<store_id>`"""
    everyone = db.all_user_ids()
    if filter_value == "all":
        return everyone
    if filter_value == "premium":
        return [uid for uid in everyone if is_premium_anywhere(uid, admin_id)]
    if filter_value == "free":
        return [uid for uid in everyone if not is_premium_anywhere(uid, admin_id)]
    if filter_value.startswith("store:"):
        store_id = int(filter_value.split(":", 1)[1])
        store = db.store(store_id)
        if store is None:
            return []
        if not store["is_premium"]:
            # a free store: reach everyone who subscribed or ever interacted
            subs = set(db.subscribers(store_id))
            return sorted(subs) or everyone
        granted = {g["user_id"] for g in db.store_grants(store_id) if
                   g["expires_at"] is None or g["expires_at"] > time.time()}
        return sorted(granted | set(db.subscribers(store_id)))
    return everyone
