"""Deep-link payload parsing.

Supports the new short scheme (`f12`, `s3`, `tAbC123`, `r987654`) and every link
shape the previous bot version produced (`FILE_...`, `STORE_...`, `LINK_...`,
`BATCH_...`, `ref_...`), so links people already have keep working.
"""
from __future__ import annotations

import re

from app.utils import safe_int

FILE_PAYLOAD = re.compile(r"^f(\d+)$")
STORE_PAYLOAD = re.compile(r"^s(\d+)$")
TOKEN_PAYLOAD = re.compile(r"^t([A-Za-z0-9]{6,})$")
REF_PAYLOAD = re.compile(r"^r(\d{3,})$")

KIND_REFERRAL = "referral"
KIND_FILE = "file"
KIND_STORE = "store"
KIND_LINK = "link"
KIND_SLUG = "slug"
KIND_CODE = "code"
KIND_LEGACY_BATCH = "legacy_batch"


def resolve_payload(payload: str) -> tuple[str, object] | None:
    payload = (payload or "").strip()
    if not payload:
        return None
    match = REF_PAYLOAD.match(payload)
    if match:
        return KIND_REFERRAL, int(match.group(1))
    if payload.startswith("ref_"):
        return KIND_REFERRAL, safe_int(payload[4:])
    match = FILE_PAYLOAD.match(payload)
    if match:
        return KIND_FILE, int(match.group(1))
    match = STORE_PAYLOAD.match(payload)
    if match:
        return KIND_STORE, int(match.group(1))
    match = TOKEN_PAYLOAD.match(payload)
    if match:
        return KIND_LINK, match.group(1)
    if payload.startswith("FILE_"):
        return KIND_CODE, payload
    if payload.startswith("STORE_"):
        return KIND_SLUG, payload[6:]
    if payload.startswith("LINK_"):
        return KIND_LINK, payload[5:]
    if payload.startswith("BATCH_"):
        return KIND_LEGACY_BATCH, payload[6:]
    return KIND_CODE, payload
