"""`config.env.example` must never document a key nothing reads.

A stale example file is how an owner spends an evening setting a variable that
has no effect. Every key in the template has to be reachable from the code —
either as an attribute on `app.config`, a key in the settings registry, or a
plain `os.getenv("…")` lookup.
"""
from __future__ import annotations

import re
from pathlib import Path

from app import config as cfg
from app.services import settings

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for folder in ("app", "web", "tools")
    for path in sorted((ROOT / folder).rglob("*.py"))
) + (ROOT / "run.py").read_text(encoding="utf-8") + (ROOT / "bot.py").read_text(encoding="utf-8")


def _template_keys() -> list[str]:
    keys = []
    for raw in (ROOT / "config.env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            line = line.lstrip("# ").strip()
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key and key.isupper() and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            keys.append(key)
    return sorted(set(keys))


def test_every_documented_key_is_actually_read():
    registry = {s.key for g in settings.REGISTRY for s in g.settings}
    config_attrs = {name for name in dir(cfg) if name.isupper()}
    dead = [key for key in _template_keys()
            if key not in registry and key not in config_attrs and f'"{key}"' not in SOURCE
            and f"'{key}'" not in SOURCE]
    assert dead == [], f"config.env.example documents keys nothing reads: {dead}"


def test_registry_has_no_duplicate_keys():
    keys = [s.key for g in settings.REGISTRY for s in g.settings]
    assert len(keys) == len(set(keys)), "a setting key is registered twice"


def test_store_specific_settings_are_not_env_only():
    """Owner-level settings the user asked to control from the panel must be in
    the registry (so /admin/settings or the bot can edit them)."""
    registry = {s.key for g in settings.REGISTRY for s in g.settings}
    for key in ("FORCE_CHANNEL", "FORCE_JOIN_EXTRA", "SUPPORT_CONTACT", "PAY_BKASH",
                "PAY_NAGAD", "PAY_NOTE", "CUSTOM_CAPTION", "STORAGE_CHANNEL",
                "CONTENT_MEDIA_KINDS", "LINK_DEFAULT_LIMIT", "BROADCAST_MAX_FILES",
                "AUTO_POST_ENABLED", "OWNER_REPORT_ENABLED"):
        assert key in registry, f"{key} can only be set in config.env — must be panel-editable"
