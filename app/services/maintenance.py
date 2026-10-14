"""Maintenance mode: while it is on, only admins can use the bot."""
from __future__ import annotations

from app.services import access, settings

DEFAULT_MESSAGE = "🛠 বট এখন একটু মেইনটেন্যান্সে আছে। কিছুক্ষণ পর আবার চেষ্টা করুন।"


def active() -> bool:
    return settings.get_bool("MAINTENANCE_MODE", False)


def message() -> str:
    return settings.get_str("MAINTENANCE_MESSAGE") or DEFAULT_MESSAGE


def blocks(user_id: int) -> bool:
    """True when this user must be turned away right now (admins never are)."""
    return active() and not access.is_admin(user_id)
