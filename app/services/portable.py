"""এক ফাইলে পুরো ইনস্টল — এক জায়গা থেকে আরেক জায়গায় নেওয়ার সিস্টেম।

সমস্যা: হোস্টিং বদলালে / নতুন বোর্ডে / নতুন হোস্টে বট আবার চালু করলে পুরোনো
ডেটাবেজ (ইউজার, আইডি, অ্যাক্সেস-হ্যাশ, স্টোর, ফাইল, সেটিংস, টেক্সট, অর্ডার,
কুপন, রিল) হারিয়ে যায় — সব আবার করে গোছাতে হয়।

সমাধান এখানে: **একটা পোর্টেবল ব্যাকআপ ফাইল** যাতে পুরো ডেটাবেজ + সব সেটিং
একসাথে থাকে, আর সেটা তিন ভাবে ফিরিয়ে আনা যায় —

1. বটে ফাইল পাঠিয়ে (`/restore` তারপর ফাইল, বা ফাইলে রিপ্লাই করে `/restore`),
2. ওয়েবসাইটের ``/admin/backup`` পেজ থেকে আপলোড করে,
3. খুব ছোট ইনস্টলের জন্য **কোড পেস্ট** করে (``BFIXF1:…`` — কপি করে চ্যাটে পেস্ট)।

নীতিগুলো:

* ফাইলটা আগে **যাচাই** হয় (SQLite হেডার, `integrity_check`, দরকারি টেবিল আছে কি),
  তারপরই ছোঁয়া হয়।
* ফেরানোর আগে বর্তমান ডেটাবেজের একটা **সেফটি কপি** নিজে থেকেই লেখা হয় —
  ভুল ফাইল দিলে আবার পুরোনোতে ফেরা যায়।
* কাজ শেষে সব cache পরিষ্কার হয় আর ডেটাবেজ আবার খোলা হয়, তাই রিস্টার্ট ছাড়াই
  নতুন ডেটা কাজ করে।
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import shutil
import sqlite3
import string
import time
from pathlib import Path

from app import config as cfg
from app.logger import log
from app.storage import db

#: Envelope marker — changing it means “old importers must ask for a newer build”.
MAGIC = "BFIXF-BACKUP/1"
#: Prefix for the paste-in code (base64 of the gzipped envelope).
TEXT_PREFIX = "BFIXF1:"
#: A paste-in code bigger than this is refused (Telegram messages are 4096 chars).
TEXT_CODE_LIMIT = 3500
#: The file extension used everywhere the owner sees it.
SUFFIX = ".bfixf.gz"

#: Tables a real store-bot database must have, otherwise it is not our backup.
REQUIRED_TABLES = ("users", "stores", "files")


class BackupError(RuntimeError):
    """Bad/foreign/corrupt backup — the message is safe to show the owner."""


# ----------------------------------------------------------------- small helpers
def _sqlite_bytes(path: str) -> bytes:
    """A consistent copy of the live database (WAL safe) as plain SQLite bytes."""
    if str(path).startswith(('postgresql://', 'postgres://')):
        if str(db.path) != str(path):
            raise BackupError('Active database does not match snapshot source')
        return db.conn.snapshot()
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(":memory:")
        try:
            src.backup(dest)                 # online snapshot, no file locking games
            dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return dest.serialize()          # Python 3.11+: bytes of the whole db
        finally:
            dest.close()
    finally:
        src.close()


def _counts(path_or_conn) -> dict:
    """Row counts for the dashboard/report — never raises."""
    out: dict[str, int] = {}
    close = False
    conn = path_or_conn
    if isinstance(conn, str) and conn.startswith(('postgresql://', 'postgres://')):
        conn = db.conn
    if isinstance(conn, (str, Path)):
        try:
            conn = sqlite3.connect(str(conn))
            close = True
        except Exception:
            return out
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("users", "stores", "files", "grants", "orders", "coupons",
                      "plans", "campaigns", "settings", "texts", "events"):
            if table in names:
                try:
                    out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except Exception:
                    continue
    except Exception:
        return out
    finally:
        if close:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _fingerprint(data: bytes) -> str:
    """Short, stable id for a backup so the owner can compare two files."""
    import hashlib
    return hashlib.sha256(data).hexdigest()[:12]


# --------------------------------------------------------------------- export
def export_envelope(*, note: str = "") -> dict:
    """Build the JSON envelope (database inside, base64 + gzip).

    Everything that matters lives in the database (users **including their
    `access_hash`**, stores, files, caches, settings, editable texts, orders,
    coupons, campaigns, drafts, reel pins, audit trail), so one snapshot is a
    complete move — nothing else has to be copied except the bot token, which is
    also stored in the database when it was set from the website.
    """
    path = str(db.path)
    raw = _sqlite_bytes(path)
    before = _counts_bytes(raw)
    envelope = {
        "magic": MAGIC,
        "app": "BfixF store bot",
        "created_at": time.time(),
        "created_at_text": time.strftime("%d %b %Y · %H:%M"),
        "note": (note or "")[:200],
        "source_host": os.uname().nodename if hasattr(os, "uname") else "",
        "python": os.sys.version.split()[0],
        "db_bytes": len(raw),
        "counts": before,
        "credentials_included": _has_credentials(path),
        "db": base64.b64encode(gzip.compress(raw, 6)).decode("ascii"),
    }
    envelope["checksum"] = _fingerprint(envelope["db"].encode("ascii"))
    return envelope


def _has_credentials(path: str) -> bool:
    """Is the bot token/API keys inside this database (so the new host needs none)?"""
    try:
        if str(path).startswith(('postgresql://', 'postgres://')):
            return all(db.get_meta(key, '') for key in ('cred:BOT_TOKEN', 'cred:API_ID', 'cred:API_HASH'))
        conn = sqlite3.connect(str(path))
        try:
            rows = {row[0] for row in conn.execute(
                "SELECT key FROM meta WHERE key LIKE 'cred:%'")}
        finally:
            conn.close()
        return {"cred:BOT_TOKEN", "cred:API_ID", "cred:API_HASH"} <= rows
    except Exception:
        return False


def export_bytes(*, note: str = "") -> bytes:
    """The backup file the owner downloads / sends to the bot."""
    envelope = export_envelope(note=note)
    body = gzip.compress(json.dumps(envelope, ensure_ascii=False).encode("utf-8"), 9)
    log.info("portable backup built: %s bytes, checksum %s",
             len(body), envelope["checksum"])
    return body


def filename(when: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(when or time.time()))
    return f"bfixf-backup-{stamp}{SUFFIX}"


def text_codes(data: bytes | None = None, *, chunk: int = 1800) -> list[str]:
    """Copy-paste codes for a database — one line, or a few numbered pieces.

    A small install fits in a single message (`BFIXF1:…`). A bigger one is split
    into `BFIXF1:1/7:…`, `BFIXF1:2/7:…` and so on; the bot collects the pieces and
    joins them (Telegram allows 4096 characters per message, and the owner may
    want to paste the whole thing into a text box as well).
    """
    data = data if data is not None else export_bytes()
    blob = base64.b64encode(data).decode("ascii")
    if len(blob) + len(TEXT_PREFIX) <= TEXT_CODE_LIMIT:
        return [TEXT_PREFIX + blob]
    chunk -= chunk % 4                                   # keep base64 groups whole
    chunk = max(400, chunk)
    pieces = [blob[i:i + chunk] for i in range(0, len(blob), chunk)]
    total = len(pieces)
    return [f"{TEXT_PREFIX}{index + 1}/{total}:{piece}"
            for index, piece in enumerate(pieces)]


def text_code(data: bytes | None = None) -> str:
    """A single-line code when it fits, else an empty string (use `text_codes`)."""
    codes = text_codes(data)
    return codes[0] if len(codes) == 1 else ""


# --------------------------------------------------------------------- inspect
def _decode(data: bytes) -> tuple[dict, bytes]:
    """`bytes` in → `(envelope, sqlite_bytes)`. Raises `BackupError` when unusable."""
    if not data:
        raise BackupError("ফাইলটা খালি।")
    payload = data
    if payload[:2] == b"\x1f\x8b":                      # gzip (our .bfixf.gz file)
        try:
            payload = gzip.decompress(payload)
        except Exception as exc:
            raise BackupError(f"ফাইলটা পড়া যাচ্ছে না ({exc})।") from exc
    text = payload.lstrip()
    if text[:15] == b"SQLite format 3":                 # a bare .sqlite3 file
        return ({"magic": "raw-sqlite", "counts": _counts_path_bytes(payload)}, payload)
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise BackupError("এটা আমাদের ব্যাকআপ ফাইল না (JSON পড়া যায়নি)।") from exc
    if not isinstance(envelope, dict) or "db" not in envelope:
        raise BackupError("ব্যাকআপ ফাইলে ডেটাবেজ অংশ নেই।")
    if str(envelope.get("magic") or "") not in (MAGIC, "raw-sqlite"):
        raise BackupError(f"অজানা ব্যাকআপ ধরন: {envelope.get('magic')!r}")
    checksum = str(envelope.get("checksum") or "")
    if checksum and _fingerprint(str(envelope["db"]).encode("ascii")) != checksum:
        raise BackupError("ফাইলের checksum মিলছে না — ফাইলটা অসম্পূর্ণ/বদলে গেছে।")
    try:
        raw = gzip.decompress(base64.b64decode(envelope["db"]))
    except Exception as exc:
        raise BackupError(f"ভিতরের ডেটাবেজ খোলা যাচ্ছে না ({exc})।") from exc
    return envelope, raw


def _counts_path_bytes(raw: bytes) -> dict:
    tmp = None
    try:
        import tempfile
        handle, tmp = tempfile.mkstemp(suffix=".sqlite3")
        with os.fdopen(handle, "wb") as fh:
            fh.write(raw)
        return _counts(tmp)
    except Exception:
        return {}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def decode_text(text: str) -> bytes:
    """Turn pasted `BFIXF1:…` code(s) back into the backup file bytes.

    Accepts one line, several numbered pieces (in any order), or the whole thing
    pasted as one blob — the pieces are sorted by their number before joining.
    """
    body = (text or "").strip()
    if TEXT_PREFIX not in body:
        raise BackupError("ব্যাকআপ কোড নয় (BFIXF1: দিয়ে শুরু হয় না)।")
    parts: list[tuple[int, str]] = []
    for chunk in body.split(TEXT_PREFIX)[1:]:
        chunk = chunk.strip()
        index = len(parts)                                  # default: file order
        marker, sep, rest = chunk.partition(":")
        if sep and "/" in marker and marker.replace("/", "").isdigit():
            index = int(marker.split("/", 1)[0]) - 1
            chunk = rest
        parts.append((index, _b64_tokens(chunk)))
    parts.sort(key=lambda item: item[0])
    blob = "".join(payload for _index, payload in parts)
    return _decode_blob(blob)


#: A long run of base64 characters — used to pick the code out of surrounding prose.
_B64_RUN = re.compile(r"[A-Za-z0-9+/=]{60,}")
#: The characters a code may contain (everything else is chatter).
_B64_CHARS = set(string.ascii_letters + string.digits + "+/=")


def _b64_tokens(text: str) -> str:
    """Keep only the code tokens of a pasted message.

    Chat clients wrap long lines and people add a word around the code; a word
    written in Bengali (or any non-base64 character) is dropped here, so the
    pieces can be joined no matter how the paste arrived.
    """
    keep = []
    for token in (text or "").split():
        if token and all(char in _B64_CHARS for char in token):
            keep.append(token)
    return "".join(keep)


def _looks_binary(data: bytes) -> bool:
    head = (data or b"")[:16]
    return (head[:2] == b"\x1f\x8b" or head[:15] == b"SQLite format 3"
            or head[:1] == b"{")


def _decode_blob(blob: str) -> bytes:
    """base64 → bytes, tolerating line breaks and text around the code."""
    squeezed = "".join((blob or "").split())
    candidates = [squeezed]
    # Someone pasted a sentence along with the code (or a chat client wrapped it):
    # take every long base64-ish run and keep the one that decodes to a backup.
    candidates += [run for run in _B64_RUN.findall(squeezed)]
    for candidate in candidates:
        padded = candidate + "=" * (-len(candidate) % 4)
        if len(padded) < 60 or len(padded) % 4:
            continue
        try:
            data = base64.b64decode(padded, validate=False)
        except Exception:
            continue
        if _looks_binary(data):
            return data
    raise BackupError("কোডটা পড়া যাচ্ছে না — পুরোটা কপি হয়নি (কয়েক টুকরো হলে "
                      "সবগুলো একসাথে পাঠান)।")


def looks_like_backup(data: bytes) -> bool:
    """Cheap check used by the bot/website to route an upload to the importer."""
    head = (data or b"")[:16]
    return head[:2] == b"\x1f\x8b" or head[:15] == b"SQLite format 3" or head[:1] == b"{"


def describe(data: bytes) -> dict:
    """What is inside a backup — shown *before* anything is restored."""
    envelope, raw = _decode(data)
    tables = _table_names(raw)
    missing = [name for name in REQUIRED_TABLES if name not in tables]
    report = {
        "ok": not missing,
        "magic": envelope.get("magic"),
        "created_at": envelope.get("created_at"),
        "created_at_text": envelope.get("created_at_text") or "",
        "note": envelope.get("note") or "",
        "source_host": envelope.get("source_host") or "",
        "checksum": envelope.get("checksum") or "",
        "size": len(raw),
        "tables": len(tables),
        "counts": envelope.get("counts") or _counts_bytes(raw),
        "credentials_included": bool(envelope.get("credentials_included")),
        "missing_tables": missing,
    }
    if missing:
        report["error"] = ("এই ব্যাকআপে দরকারি টেবিল নেই: " + ", ".join(missing))
    return report


def _table_names(raw: bytes) -> set[str]:
    conn = _connect_bytes(raw)
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _connect_bytes(raw: bytes) -> sqlite3.Connection:
    """Open SQLite bytes from memory *and* run an integrity check on them."""
    handle, tmp = _temp_file(raw)
    try:
        conn = sqlite3.connect(tmp)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        except Exception as exc:
            raise BackupError(f"ডেটাবেজ যাচাই করা যায়নি ({exc})।") from exc
        if not result or str(result[0]).lower() != "ok":
            raise BackupError(f"ডেটাবেজটা নষ্ট: {result[0] if result else 'unknown'}")
        # keep the connection pointing at a private copy: unlink now, SQLite keeps it
        return conn
    finally:
        if handle is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _temp_file(raw: bytes) -> tuple[int, str]:
    import tempfile
    handle, path = tempfile.mkstemp(suffix=".sqlite3")
    with os.fdopen(handle, "wb") as fh:
        fh.write(raw)
    return handle, path


def _counts_bytes(raw: bytes) -> dict:
    conn = _connect_bytes(raw)
    try:
        return _counts(conn)
    finally:
        conn.close()


# ===================================================================== importers
# Real life: the owner's data is usually *not* a brand new `.bfixf.gz` — it is the
# database of an older version of this bot, the old `bot_db.json`, or the `users.db`
# of the standalone broadcast bot. All of those are understood here, so moving over
# never means typing the customer list again.
KIND_FULL = "full"            # our envelope, or any SQLite with users+stores+files
KIND_USERS = "users"          # a foreign database that only has a user list
KIND_LEGACY_JSON = "legacy-json"    # the old bot_db.json
KIND_EXPORT_JSON = "export-json"    # our own /admin/download/json
KIND_UNKNOWN = "unknown"

# `imported` dict-এর কীগুলো মানুষের পড়ার মতো বাংলায় দেখানোর জন্য
IMPORT_LABELS = {
    "users": "ইউজার", "stores": "স্টোর", "files": "ফাইল", "grants": "গ্রান্ট",
    "orders": "অর্ডার", "coupons": "কুপন", "plans": "প্ল্যান", "campaigns": "ক্যাম্পেইন",
    "events": "ইভেন্ট", "subscriptions": "সাবস্ক্রিপশন", "tickets": "টিকেট",
    "added": "নতুন", "updated": "হালনাগাদ", "blocked": "ব্যান", "skipped": "বাদ",
}
_BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


def bn_number(value) -> str:
    """`12` → `১২` — বাংলা লেখার ভিতরে ইংরেজি সংখ্যা ঢুকতে দেব না।"""
    return str(value).translate(_BN_DIGITS)


def import_summary(report: dict) -> list[str]:
    """`{'added': 3, 'blocked': 1}` → `['নতুন ৩', 'ব্যান ১']`।"""
    parts = []
    for key, value in (report.get("imported") or {}).items():
        if not value:
            continue
        label = IMPORT_LABELS.get(str(key), str(key))
        parts.append(f"{label} {bn_number(value)}")
    return parts


def _sqlite_tables(raw: bytes) -> set[str]:
    conn = _connect_bytes(raw)
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _looks_like_users_db(tables: set[str]) -> bool:
    """A standalone bot's `users.db`: it has users, but no stores/files of ours."""
    return "users" in tables and not {"stores", "files"} & tables


def inspect(data: bytes, *, allow_users_import: bool = True) -> dict:
    """What *kind* of backup is this, and what will happen when it is applied?

    The answer drives the wording everywhere (panel, bot, tests): a full backup
    replaces the install, an older/foreign file is *merged* into it — nothing the
    owner already has is thrown away.
    """
    if not data:
        raise BackupError("ফাইলটা খালি।")
    payload = data
    if payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except Exception as exc:
            raise BackupError(f"ফাইলটা পড়া যাচ্ছে না ({exc})।") from exc

    # 1) a SQLite database (ours, an older version of ours, or a foreign one)
    if payload[:15] == b"SQLite format 3":
        tables = _sqlite_tables(payload)
        if {"users", "stores", "files"} <= tables:
            return {"kind": KIND_FULL, "label": "পুরো ইনস্টল ব্যাকআপ",
                    "merge": False, "counts": _counts_bytes(payload),
                    "size": len(payload), "tables": sorted(tables)}
        if allow_users_import and _looks_like_users_db(tables):
            conn = _connect_bytes(payload)
            try:
                counts = _counts(conn)
                columns = [row[1] for row in conn.execute("PRAGMA table_info(users)")]
            finally:
                conn.close()
            return {"kind": KIND_USERS, "label": "শুধু ইউজার তালিকা (পুরোনো বট)",
                    "merge": True, "counts": counts, "size": len(payload),
                    "tables": sorted(tables), "user_columns": sorted(columns)}
        raise BackupError("এই ডেটাবেজে আমাদের দরকারি টেবিল নেই (users/stores/files)।")

    # 2) JSON: our envelope, our export, or the legacy bot_db.json
    try:
        document = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise BackupError("এটা আমাদের ব্যাকআপ ফাইল না (JSON পড়া যায়নি)।") from exc
    if not isinstance(document, dict):
        raise BackupError("ব্যাকআপ ফাইলটা অদ্ভুত — JSON অবজেক্ট না।")

    if str(document.get("magic") or "") == MAGIC:
        checksum = str(document.get("checksum") or "")
        if checksum and _fingerprint(str(document["db"]).encode("ascii")) != checksum:
            raise BackupError("ফাইলের checksum মিলছে না — ফাইলটা অসম্পূর্ণ/বদলে গেছে।")
        try:
            raw = gzip.decompress(base64.b64decode(document["db"]))
        except Exception as exc:
            raise BackupError(f"ভিতরের ডেটাবেজ খোলা যাচ্ছে না ({exc})।") from exc
        info = inspect(raw, allow_users_import=allow_users_import)
        info.update({"magic": MAGIC, "created_at": document.get("created_at"),
                     "created_at_text": document.get("created_at_text") or "",
                     "note": document.get("note") or "",
                     "source_host": document.get("source_host") or "",
                     "checksum": checksum,
                     "credentials_included": bool(document.get("credentials_included"))})
        return info

    if "user_details" in document or "premium_access" in document:
        counts = {"users": len(document.get("user_details") or {}),
                  "stores": sum(len(v or {}) for v in (document.get("stores") or {}).values()),
                  "files": sum(len(f or {}) for v in (document.get("stores") or {}).values()
                               for f in (v or {}).values())}
        return {"kind": KIND_LEGACY_JSON, "label": "পুরোনো bot_db.json ব্যাকআপ",
                "merge": True, "counts": counts, "size": len(payload),
                "created_at_text": "", "credentials_included": False}

    if "stores" in document and "users" in document:
        counts = {"users": len(document.get("users") or {}),
                  "stores": len(document.get("stores") or {}),
                  "files": sum(len((s or {}).get("files") or {})
                               for s in (document.get("stores") or {}).values())}
        return {"kind": KIND_EXPORT_JSON, "label": "JSON এক্সপোর্ট",
                "merge": True, "counts": counts, "size": len(payload),
                "created_at_text": "", "credentials_included": False}

    raise BackupError("চেনা যায়নি — এটা ব্যাকআপ ফাইল না।")


def _apply_users_import(raw: bytes, source: str) -> dict:
    """Merge a foreign user list into this install (nothing is deleted)."""
    conn = _connect_bytes(raw)
    try:
        # the column order matters: `PRAGMA table_info` gives it, a set would not
        columns = [row[1] for row in conn.execute("PRAGMA table_info(users)")]
        rows = list(conn.execute("SELECT * FROM users"))
    finally:
        conn.close()
    before = _counts(str(db.path)).get("users", 0)
    added = updated = blocked = 0
    for row in rows:
        values = {name: row[index] for index, name in enumerate(columns)}
        uid = _int_or_zero(values.get("user_id") or values.get("id") or
                           (row[0] if row else 0))
        if uid <= 0:
            continue
        name = str(values.get("name") or "").strip()
        username = str(values.get("username") or "").strip().lstrip("@")
        existed = db.user(uid) is not None
        try:
            db.touch_user(uid, name or None, username or None)
        except Exception:
            continue
        if existed:
            updated += 1
        else:
            added += 1
        if _int_or_zero(values.get("blocked")):
            db.ban_user(uid, "পুরোনো বটের তালিকায় ব্লকড", 0)
            blocked += 1
    after = _counts(str(db.path))
    log.info("user list imported from %s: +%s new, %s updated", source, added, updated)
    return {"ok": True, "kind": KIND_USERS, "source": source, "merge": True,
            "imported": {"added": added, "updated": updated, "blocked": blocked},
            "counts_before": {"users": before}, "counts_after": after,
            "safety_copy": "", "credentials_included": False,
            "created_at_text": ""}


def _apply_json_import(document: dict, kind: str, source: str) -> dict:
    """Import an old JSON backup (legacy `bot_db.json` or our export)."""
    before = _counts(str(db.path))
    if kind == KIND_LEGACY_JSON:
        import tempfile
        handle, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(document, fh, ensure_ascii=False)
        checksum = _fingerprint(json.dumps(document, sort_keys=True).encode("utf-8"))
        key = f"legacy_json:{checksum}"
        if db.get_meta(key) == "yes":
            os.unlink(path)
            return {"ok": True, "kind": kind, "source": source, "merge": True,
                    "imported": {"already": True}, "counts_before": before,
                    "counts_after": _counts(str(db.path)), "safety_copy": "",
                    "credentials_included": False, "created_at_text": ""}
        db.set_meta("legacy_imported", "")            # allow this run to import once
        try:
            counters = db.migrate_legacy(path) or {}
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        db.set_meta(key, "yes")
        imported = {key: value for key, value in (counters or {}).items() if value}
    else:
        imported = _import_export_document(document)
    after = _counts(str(db.path))
    return {"ok": True, "kind": kind, "source": source, "merge": True,
            "imported": imported, "counts_before": before, "counts_after": after,
            "safety_copy": "", "credentials_included": False, "created_at_text": ""}


def _import_export_document(document: dict) -> dict:
    """Create stores/files/users/grants from `db.export_dict()` shaped JSON."""
    counters = {"users": 0, "stores": 0, "files": 0, "grants": 0}
    for raw_uid, info in (document.get("users") or {}).items():
        uid = _int_or_zero(raw_uid)
        if uid <= 0:
            continue
        if db.user(uid) is None:
            db.touch_user(uid, (info or {}).get("name"), (info or {}).get("username"))
            counters["users"] += 1
    for raw_store, store in (document.get("stores") or {}).items():
        store = store or {}
        name = str(store.get("name") or "").strip()
        admin_id = _int_or_zero(store.get("admin_id"))
        if not name or admin_id <= 0:
            continue
        existing = [row for row in db.stores_admin(admin_id) if row["name"] == name]
        row = existing[0] if existing else db.create_store(admin_id, name)
        if not existing:
            counters["stores"] += 1
        try:
            db.update_store(row["id"], is_premium=int(store.get("is_premium") or 0),
                            description=store.get("description") or "",
                            slug=store.get("slug") or None)
        except Exception:
            pass
        for old_fid, meta in (store.get("files") or {}).items():
            meta = meta or {}
            file_name = str(meta.get("name") or old_fid)
            created = db.add_file(row["id"], file_name, meta.get("type") or "Video",
                                  _int_or_zero(meta.get("chat_id")),
                                  _int_or_zero(meta.get("msg_id")),
                                  code=str(old_fid),
                                  size=meta.get("size"))
            if created:
                counters["files"] += 1
                if meta.get("views"):
                    db._run("UPDATE files SET views = ? WHERE id = ?",
                            (int(meta["views"]), created))
    for grant in (document.get("grants") or []):
        store_id = _int_or_zero((grant or {}).get("store_id"))
        user_id = _int_or_zero((grant or {}).get("user_id"))
        if store_id and user_id and db.grant_row(store_id, user_id) is None:
            db.grant(store_id, user_id, (grant or {}).get("expires_at"),
                     str((grant or {}).get("source") or "imported"))
            counters["grants"] += 1
    return counters


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def apply(data: bytes, *, source: str = "upload", keep_safety: bool = True) -> dict:
    """Do the right thing for whatever the owner gave us.

    * a full backup → `restore()` (replaces the install, with a safety copy),
    * an older / foreign file → `*_import()` (merges, never deletes).
    """
    info = inspect(data)
    if info["kind"] == KIND_FULL:
        report = restore(data, source=source, keep_safety=keep_safety)
        report.setdefault("kind", KIND_FULL)
        report["merge"] = False
        report["counts_before"] = report.get("counts_before") or info.get("counts") or {}
        return report
    payload = data
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    if payload[:15] == b"SQLite format 3":
        return _apply_users_import(payload, source)
    document = json.loads(payload.decode("utf-8"))
    return _apply_json_import(document, info["kind"], source)


# --------------------------------------------------------------------- restore
def safety_copy() -> str:
    """Write the *current* database next to the backups before a restore."""
    directory = Path(cfg.BACKUP_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"before-restore-{time.strftime('%Y%m%d-%H%M%S')}{SUFFIX}"
    envelope = export_envelope(note="automatic safety copy before a restore")
    target.write_bytes(gzip.compress(json.dumps(envelope, ensure_ascii=False).encode("utf-8"), 9))
    log.info("safety copy written: %s", target)
    return str(target)


def restore(data: bytes, *, source: str = "upload", keep_safety: bool = True) -> dict:
    """Put a backup back into this install. Returns a report for the owner.

    Order matters: decode → validate → safety copy → swap file → reopen → clear
    caches. Anything wrong before the swap leaves the install exactly as it was.
    """
    envelope, raw = _decode(data)
    conn = _connect_bytes(raw)                        # integrity check happens here
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        counts = _counts(conn)
    finally:
        conn.close()
    missing = [name for name in REQUIRED_TABLES if name not in tables]
    if missing:
        raise BackupError("এই ব্যাকআপটা এই বটের না — নেই: " + ", ".join(missing))

    if getattr(db, 'backend', 'sqlite') == 'postgresql':
        before = _counts(db.conn)
        safety = safety_copy() if keep_safety else ''
        try:
            db.conn.import_sqlite(raw, replace=True)
        except Exception as exc:
            raise BackupError('PostgreSQL restore ব্যর্থ; আগের ডেটা অক্ষত আছে।') from exc
        _clear_caches()
        return {'ok': True, 'source': source, 'created_at_text': envelope.get('created_at_text', ''),
                'checksum': envelope.get('checksum', ''), 'counts_before': before,
                'counts_after': _counts(db.conn), 'safety_copy': safety,
                'credentials_included': _has_credentials(str(db.path))}

    path = Path(str(db.path))
    before = _counts(path)
    safety = safety_copy() if keep_safety else ""
    tmp = path.with_suffix(path.suffix + ".incoming")
    tmp.write_bytes(raw)

    # Close our handle and swap the files, keeping the old one until the swap worked.
    try:
        db.impl.conn.close()
    except Exception:
        pass
    backup_old = path.with_suffix(path.suffix + ".replaced")
    try:
        if backup_old.exists():
            backup_old.unlink()
        if path.exists():
            shutil.move(str(path), str(backup_old))
        shutil.move(str(tmp), str(path))
    except Exception as exc:
        if backup_old.exists() and not path.exists():
            shutil.move(str(backup_old), str(path))
        raise BackupError(f"ডেটাবেজ পাল্টানো যায়নি ({exc})।") from exc
    # WAL/SHM of the old database must not survive into the new one.
    for side in ("-wal", "-shm"):
        try:
            Path(str(path) + side).unlink()
        except OSError:
            pass

    db.bind(str(path))                                # reopen + run migrations
    _clear_caches()
    try:
        db.impl._upgrade()
    except Exception as exc:                          # pragma: no cover - defensive
        log.warning("post-restore upgrade failed: %s", exc)
    if backup_old.exists():
        try:
            backup_old.unlink()
        except OSError:
            pass

    report = {
        "ok": True,
        "source": source,
        "created_at_text": envelope.get("created_at_text") or "",
        "checksum": envelope.get("checksum") or "",
        "counts_before": before,
        "counts_after": _counts(str(path)),
        "safety_copy": safety,
        "credentials_included": _has_credentials(str(path)),
    }
    log.warning("database restored from %s: %s users, %s files (safety: %s)",
                source, report["counts_after"].get("users"),
                report["counts_after"].get("files"), safety or "—")
    return report


def _clear_caches() -> None:
    """Forget everything that was read from the old database."""
    try:
        from app.services import settings as settings_service
        settings_service._cache.clear()
    except Exception:
        pass
    for module_name in ("app.services.peers", "app.services.feed",
                        "app.services.media_cache", "app.services.quickcast", "app.services.broadcast"):
        try:
            module = __import__(module_name, fromlist=["_clear_caches", "_cache"])
            for attr in ("_cache", "_CACHE", "CACHE", "_payload_cache", "_msg_cache", "_thumb_cache", "_hashes", "_chat_hashes"):
                cache = getattr(module, attr, None)
                if isinstance(cache, dict):
                    cache.clear()
        except Exception:
            continue
    try:
        from app import runtime
        runtime.bot_entity_cache.clear()
    except Exception:
        pass


def local_backups(limit: int = 12) -> list[dict]:
    """Backup files that already sit in `BACKUP_DIR` on this server."""
    directory = Path(cfg.BACKUP_DIR)
    if not directory.is_dir():
        return []
    rows = []
    for item in sorted(directory.glob(f"*{SUFFIX}"), key=lambda p: p.stat().st_mtime,
                       reverse=True)[:limit]:
        stat = item.stat()
        rows.append({"name": item.name, "path": str(item), "size": stat.st_size,
                     "mtime": stat.st_mtime,
                     "mtime_text": time.strftime("%d %b %Y · %H:%M",
                                                 time.localtime(stat.st_mtime)),
                     "safety": item.name.startswith("before-restore-")})
    return rows


async def send_backup(admin_id: int, *, note: str = "manual", caption_extra: str = "") -> dict:
    """Send the portable backup file to one admin (`/backup` in the bot)."""
    from app.runtime import bot
    from app.services.telegram import safe_call

    data = export_bytes(note=note)
    name = filename()
    code = text_code(data)
    codes = text_codes(data)
    caption = ("🗄 <b>পুরো ইনস্টলের ব্যাকআপ</b>\n"
               "এই একটা ফাইলেই আপনার সব ইউজার (আইডি + অ্যাক্সেস-হ্যাশ), স্টোর, "
               "ফাইল, সেটিংস, টেক্সট, অর্ডার — সব আছে।\n\n"
               "নতুন হোস্টে নিতে: সেখানে বট চালু করে <code>/restore</code> পাঠান, "
               "তারপর <b>এই ফাইলটাই</b> পাঠিয়ে দিন (বা ফাইলে রিপ্লাই করে "
               "<code>/restore</code>)। ওয়েবসাইট থেকেও করা যায় — "
               "<code>/admin/backup</code>।\n"
               "⚠️ ফাইলটাতে টোকেনও থাকতে পারে — কারো সাথে শেয়ার করবেন না।"
               + (f"\n\n{caption_extra}" if caption_extra else ""))
    sent = await safe_call(bot.send_file, int(admin_id), data, caption=caption,
                           force_document=True, file_name=name,
                           attributes=[], what="portable_backup", retries=1)
    return {"sent": bool(sent), "bytes": len(data), "name": name,
            "code": code, "codes": codes, "counts": _counts(str(db.path))}


def migration_steps() -> list[str]:
    """The “নতুন হোস্টে চালু করুন” checklist shown on the website."""
    return [
        "নতুন জায়গায় বটের ফাইলগুলো (GitHub থেকে) তুলুন — <code>README.md</code>-এর "
        "হোস্টিং অংশ দেখুন।",
        "⚠️ শুধু ডেটাবেজ পেলেই হবে — ফাইলগুলো Telegram-এই থাকে, তাই আবার আপলোড "
        "করতে হয় না।",
        "নতুন হোস্টে বট একবার চালু করুন → ওয়েবসাইট খুলবে → <b>“আগের ইনস্টল "
        "ফিরিয়ে আনুন”</b> কার্ডে এই ব্যাকআপ ফাইল দিন (বা বটে <code>/restore</code> "
        "পাঠিয়ে ফাইল পাঠান)।",
        "ব্যাকআপে টোকেন থাকলে নতুন হোস্টে কিছু সেট করার দরকারই নেই — বট নিজে থেকেই "
        "চালু হবে। টোকেন না থাকলে <code>/setup</code>-এ একবার টোকেন/API আইডি দিন।",
        "ভেরিয়েবল দিয়ে চালাতেন (<code>BOT_TOKEN=…</code>), তাহলে নতুন হোস্টেও ওই "
        "একই ভেরিয়েবলগুলো দিন — সেটিংস <code>/admin/settings</code>-এ কী কী লাগে "
        "দেখা যায়।",
        "সব ঠিক আছে কি না: <code>/health</code> খুলুন, <code>/admin/self-test</code> "
        "চালান, আর <code>/brwho</code> দিয়ে দেখুন কতজনকে পাঠানো যাবে।",
    ]
