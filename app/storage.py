"""SQLite storage layer.

Why SQLite: it needs no server (works on any web-hosting disk quota), gives us
atomic writes (no more corrupted JSON after a crash), indexed lookups even with
hundreds of thousands of files, and lets us increment a view counter without
rewriting the whole database file on every click.

An existing `bot_db.json` is imported automatically on the first run.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from app import config as cfg
from app.logger import log
from app.utils import slugify, unique_keep_order

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_progress (
    scope TEXT NOT NULL, file_key INTEGER NOT NULL, owner_id INTEGER NOT NULL,
    name TEXT DEFAULT '', stage TEXT DEFAULT 'waiting', current INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0, started_at REAL DEFAULT 0, updated_at REAL DEFAULT 0,
    detail TEXT DEFAULT '', PRIMARY KEY(scope,file_key)
);

CREATE INDEX IF NOT EXISTS transfer_progress_owner ON transfer_progress(owner_id,updated_at DESC);
CREATE TABLE IF NOT EXISTS restricted_channels (
    chat_id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL UNIQUE,
    link TEXT DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS restricted_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target INTEGER NOT NULL, request_id INTEGER NOT NULL, admin_id INTEGER NOT NULL,
    source TEXT NOT NULL, source_msg INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
    ready_at REAL DEFAULT 0, last_error TEXT DEFAULT '',
    UNIQUE(target,request_id)
);
CREATE TABLE IF NOT EXISTS restricted_items (
    job_id INTEGER NOT NULL, source_msg INTEGER NOT NULL, sent_msg INTEGER DEFAULT 0,
    PRIMARY KEY(job_id,source_msg)
);

CREATE TABLE IF NOT EXISTS video_copy_lease (
    id INTEGER PRIMARY KEY CHECK(id=1),
    owner TEXT NOT NULL,
    until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS video_copy_jobs (
    store_id INTEGER PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    chat_id INTEGER DEFAULT 0,
    message_id INTEGER DEFAULT 0,
    updated_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS video_copy_items (
    store_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    ready_at REAL DEFAULT 0,
    last_error TEXT DEFAULT '',
    PRIMARY KEY(store_id, file_id)
);
CREATE INDEX IF NOT EXISTS video_copy_due ON video_copy_items(state, ready_at, store_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS users (
    user_id   INTEGER PRIMARY KEY,
    name      TEXT,
    username  TEXT,
    joined_at REAL,
    last_seen REAL,
    lang      TEXT DEFAULT 'bn',
    sort_pref TEXT DEFAULT 'default'
);
CREATE TABLE IF NOT EXISTS admin_state (
    admin_id        INTEGER PRIMARY KEY,
    active_store_id INTEGER
);
CREATE TABLE IF NOT EXISTS stores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    slug        TEXT    UNIQUE,
    created_at  REAL,
    is_premium  INTEGER DEFAULT 1,
    cover       TEXT,
    description TEXT,
    publish_channel_id INTEGER,
    publish_channel_title TEXT DEFAULT '',
    publish_channel_link TEXT DEFAULT '',
    auto_post_enabled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'File',
    chat_id    INTEGER,
    msg_id     INTEGER,
    code       TEXT    UNIQUE,
    size       INTEGER,
    duration   INTEGER,
    created_at REAL,
    views      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS links (
    token      TEXT PRIMARY KEY,
    admin_id   INTEGER,
    kind       TEXT,
    created_at REAL,
    expires_at REAL
);
CREATE TABLE IF NOT EXISTS link_files (
    token   TEXT,
    file_id INTEGER,
    PRIMARY KEY (token, file_id)
);
CREATE TABLE IF NOT EXISTS link_channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token      TEXT,
    ref        TEXT,
    title      TEXT,
    chat_id    INTEGER,
    invite     TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_link_channels_token ON link_channels(token);
CREATE TABLE IF NOT EXISTS link_uses (
    token   TEXT,
    user_id INTEGER,
    used_at REAL,
    times   INTEGER DEFAULT 1,
    PRIMARY KEY (token, user_id)
);
CREATE TABLE IF NOT EXISTS grants (
    store_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    expires_at REAL,
    granted_at REAL,
    source     TEXT,
    PRIMARY KEY (store_id, user_id)
);
CREATE TABLE IF NOT EXISTS referrals (
    user_id     INTEGER PRIMARY KEY,
    referrer_id INTEGER,
    rewarded    INTEGER DEFAULT 0,
    created_at  REAL
);
CREATE TABLE IF NOT EXISTS referral_cfg (
    admin_id INTEGER PRIMARY KEY,
    store_id INTEGER,
    days     INTEGER
);
CREATE TABLE IF NOT EXISTS drip (
    store_id       INTEGER PRIMARY KEY,
    admin_id       INTEGER,
    count          INTEGER DEFAULT 3,
    send_time      TEXT,
    last_sent_date TEXT,
    enabled        INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drip_subs (
    store_id INTEGER,
    user_id  INTEGER,
    PRIMARY KEY (store_id, user_id)
);
CREATE TABLE IF NOT EXISTS drip_sent (
    store_id INTEGER,
    file_id  INTEGER,
    PRIMARY KEY (store_id, file_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    admin_id    INTEGER PRIMARY KEY,
    session_str TEXT,
    owner_id    INTEGER,
    owner_name  TEXT,
    added_at    REAL
);
CREATE TABLE IF NOT EXISTS bot_chats (
    chat_id INTEGER PRIMARY KEY,
    title   TEXT
);
-- Channel/group peers the owner's session (or the bot) has met. Telegram needs the
-- pair (channel_id, access_hash) exactly like users, and this is what lets the
-- media-cache channel survive a wiped session: “Could not find the input entity for
-- PeerChannel(…)” happens when this table has no row yet.
CREATE TABLE IF NOT EXISTS chat_peers (
    peer_id     INTEGER PRIMARY KEY,   -- marked id (-100…), as Telegram reports it
    access_hash INTEGER,
    title       TEXT DEFAULT '',
    username    TEXT DEFAULT '',
    kind        TEXT DEFAULT 'channel',
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_files_store   ON files(store_id);
CREATE INDEX IF NOT EXISTS idx_files_code    ON files(code);
CREATE INDEX IF NOT EXISTS idx_files_created ON files(created_at);
CREATE INDEX IF NOT EXISTS idx_stores_admin  ON stores(admin_id);
CREATE INDEX IF NOT EXISTS idx_grants_user   ON grants(user_id);
CREATE INDEX IF NOT EXISTS idx_links_exp     ON links(expires_at);
CREATE INDEX IF NOT EXISTS idx_refs_referrer ON referrals(referrer_id);

-- ===================== v2.1: business, support, engagement =====================
CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    days       INTEGER NOT NULL DEFAULT 30,
    price      REAL    NOT NULL DEFAULT 0,
    active     INTEGER DEFAULT 1,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    store_id   INTEGER,
    plan_id    INTEGER,
    plan_name  TEXT,
    days       INTEGER,
    amount     REAL,
    currency   TEXT,
    method     TEXT,
    status     TEXT DEFAULT 'pending',
    txn_ref    TEXT,
    note       TEXT,
    proof      TEXT,
    created_at REAL,
    decided_at REAL,
    decided_by INTEGER,
    admin_note TEXT
);
CREATE TABLE IF NOT EXISTS coupons (
    code       TEXT PRIMARY KEY,
    store_id   INTEGER,
    percent    INTEGER DEFAULT 0,
    days       INTEGER DEFAULT 0,
    max_uses   INTEGER DEFAULT 0,
    used       INTEGER DEFAULT 0,
    expires_at REAL,
    active     INTEGER DEFAULT 1,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS trials (
    user_id  INTEGER,
    store_id INTEGER,
    used_at  REAL,
    PRIMARY KEY (user_id, store_id)
);
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    username    TEXT,
    message     TEXT,
    status      TEXT DEFAULT 'open',
    created_at  REAL,
    answered_at REAL,
    answered_by INTEGER,
    reply       TEXT
);
CREATE TABLE IF NOT EXISTS requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    store_id   INTEGER,
    keyword    TEXT,
    status     TEXT DEFAULT 'open',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS favorites (
    user_id    INTEGER,
    file_id    INTEGER,
    created_at REAL,
    PRIMARY KEY (user_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_plans_store  ON plans(store_id);
CREATE INDEX IF NOT EXISTS idx_orders_user  ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(status);
CREATE INDEX IF NOT EXISTS idx_tickets_st   ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_fav_user     ON favorites(user_id);

-- ===================== v2.2: bans, auto-sync, welcome =====================
CREATE TABLE IF NOT EXISTS banned (
    user_id   INTEGER PRIMARY KEY,
    reason    TEXT,
    banned_at REAL,
    banned_by INTEGER
);
CREATE TABLE IF NOT EXISTS auto_sync (
    store_id    INTEGER PRIMARY KEY,
    chat_id     INTEGER,
    title       TEXT,
    last_scan   REAL,
    interval_h  INTEGER DEFAULT 24,
    enabled     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS welcome_cfg (
    admin_id INTEGER PRIMARY KEY,
    text     TEXT
);

-- ===================== v2.3: broadcast studio (campaign queue) =====================
CREATE TABLE IF NOT EXISTS campaigns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id     INTEGER,
    title        TEXT,
    text         TEXT,
    files        TEXT,                 -- JSON list of file ids to attach ([] = text only)
    audience     TEXT DEFAULT 'all',   -- all | premium | free | store:<id> | nosale:<id> | sub:<id>
    status       TEXT DEFAULT 'draft', -- draft|queued|scheduled|running|paused|done|cancelled|failed
    total        INTEGER DEFAULT 0,
    sent         INTEGER DEFAULT 0,
    failed       INTEGER DEFAULT 0,
    blocked      INTEGER DEFAULT 0,
    scheduled_at REAL,
    created_at   REAL,
    started_at   REAL,
    finished_at  REAL,
    last_error   TEXT
);
CREATE TABLE IF NOT EXISTS campaign_items (
    campaign_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    status      TEXT DEFAULT 'pending',   -- pending|sent|failed|blocked
    attempts    INTEGER DEFAULT 0,
    error       TEXT,
    updated_at  REAL,
    PRIMARY KEY (campaign_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_camp_status ON campaigns(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_camp_items  ON campaign_items(campaign_id, status);

-- v3.4: a broadcast is not always one message. Every item an admin sent while
-- drafting (text, photo, video, GIF, document, audio, voice) is one row here and
-- goes out as its own message — exactly like the original draft.
CREATE TABLE IF NOT EXISTS campaign_groups (
    campaign_id INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    kind        TEXT,
    file_id     TEXT,
    text        TEXT,
    media_key   TEXT,
    parse_mode  TEXT DEFAULT '',
    created_at  REAL,
    PRIMARY KEY (campaign_id, idx)
);
-- v3.4: reusable “label | url” buttons + a public draft link for each campaign.
CREATE TABLE IF NOT EXISTS campaign_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER,
    token       TEXT UNIQUE,
    label       TEXT,
    url         TEXT,
    kind        TEXT DEFAULT 'button',
    clicks      INTEGER DEFAULT 0,
    created_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_camp_links ON campaign_links(campaign_id, kind);
CREATE INDEX IF NOT EXISTS idx_camp_groups ON campaign_groups(campaign_id, idx);

-- ===================== v3: analytics, limited links, per-store force join =====================
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL,
    user_id  INTEGER,
    name     TEXT,                      -- start|open_store|view_file|deliver|join_click|join_block|limit_block|pay_start|paid|cancel|search|broadcast_open
    store_id INTEGER,
    file_id  INTEGER,
    ref      TEXT,                      -- slug / token / campaign id / payload
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts    ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_user  ON events(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_file  ON events(file_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_store ON events(store_id, ts DESC);

CREATE TABLE IF NOT EXISTS join_channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id   INTEGER,                 -- NULL = global / default channel
    ref        TEXT NOT NULL,           -- @username, invite hash or chat id
    title      TEXT DEFAULT '',
    chat_id    INTEGER,
    kind       TEXT DEFAULT '',         -- username|invite|id
    invite     TEXT DEFAULT '',         -- cached join URL
    added_at   REAL,
    last_check REAL,
    error      TEXT DEFAULT '',
    enabled    INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jc_store ON join_channels(store_id, id);

CREATE TABLE IF NOT EXISTS join_prompts (
    user_id  INTEGER NOT NULL,
    file_id  INTEGER NOT NULL,
    token    TEXT,
    attempts INTEGER DEFAULT 0,
    created  REAL,
    PRIMARY KEY (user_id, file_id)
);

CREATE TABLE IF NOT EXISTS file_views (
    file_id  INTEGER PRIMARY KEY,
    unique_n INTEGER DEFAULT 0,
    last_ts  REAL
);

-- ===================== v3.1: media broadcasts, drafts, public reel =====================
-- Media attached to a broadcast that is *not* one of the store files: the admin
-- forwards / uploads a video to the bot (or picks one in the website composer) and
-- the campaign remembers only `(chat_id, msg_id)` — Telegram's own copy is the
-- storage, so the queue survives a restart without re-uploading anything.
CREATE TABLE IF NOT EXISTS campaign_media (
    campaign_id INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    chat_id     INTEGER,
    msg_id      INTEGER,
    kind        TEXT DEFAULT 'video',   -- video|photo|audio|document
    name        TEXT DEFAULT '',
    item_id     INTEGER DEFAULT 0,      -- inbox (`bc_media`) row, for previews
    PRIMARY KEY (campaign_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_campaign_media ON campaign_media(campaign_id, idx);

-- `/br` drafts: whatever the admin collected (text, buttons, media, options) is
-- saved here on every step, so a restart, a closed app or a forgotten session
-- never loses the content.
CREATE TABLE IF NOT EXISTS br_drafts (
    admin_id   INTEGER PRIMARY KEY,
    data       TEXT,
    updated_at REAL
);

-- “Media inbox”: videos/photos the owner forwards to the bot for broadcasts that
-- are not store files (a banner, an announcement video, a promo clip…). The
-- website's broadcast studio can attach any of them to a campaign.
CREATE TABLE IF NOT EXISTS bc_media (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER,
    msg_id   INTEGER,
    kind     TEXT DEFAULT 'video',
    name     TEXT DEFAULT '',
    size     INTEGER DEFAULT 0,
    added_at REAL,
    used     INTEGER DEFAULT 0
);

-- ==================== extra bots (several bots, one database) =================
-- A second/third bot token cannot be polled from the same process, so each row
-- here is run by its own small `bot.py` process against this same database.
CREATE TABLE IF NOT EXISTS bots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT DEFAULT '',
    username    TEXT DEFAULT '',
    token       TEXT NOT NULL,
    api_id      INTEGER DEFAULT 0,
    api_hash    TEXT DEFAULT '',
    admin_id    INTEGER DEFAULT 0,
    note        TEXT DEFAULT '',
    enabled     INTEGER DEFAULT 1,
    created_at  REAL,
    last_start  REAL DEFAULT 0,
    last_error  TEXT DEFAULT '',
    origin      TEXT DEFAULT 'panel'
);

-- ==================== visitor analytics (who is on the site right now) ========
-- One row per *visit* (a person + a session, closed after 30 quiet minutes), so
-- “কতজন এখন অনলাইনে / কোথা থেকে এসেছে” needs no external service at all.
CREATE TABLE IF NOT EXISTS visits (
    vid         TEXT PRIMARY KEY,      -- random id kept in the visitor's cookie
    user_id     INTEGER DEFAULT 0,     -- the Telegram id once we know it
    is_admin    INTEGER DEFAULT 0,     -- the owner's own visits are counted separately
    started_at  REAL,
    last_seen   REAL,
    ended_at    REAL DEFAULT 0,
    pageviews   INTEGER DEFAULT 0,
    ip          TEXT DEFAULT '',
    country     TEXT DEFAULT '',
    device      TEXT DEFAULT '',
    device_kind TEXT DEFAULT '',
    os          TEXT DEFAULT '',
    browser     TEXT DEFAULT '',
    referrer    TEXT DEFAULT '',
    source      TEXT DEFAULT '',
    landing     TEXT DEFAULT '',
    user_agent  TEXT DEFAULT '',
    bot         INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS page_views (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    vid      TEXT,
    user_id  INTEGER DEFAULT 0,
    path     TEXT DEFAULT '',
    title    TEXT DEFAULT '',
    status   INTEGER DEFAULT 200,
    ms       INTEGER DEFAULT 0,
    is_admin INTEGER DEFAULT 0,
    ts       REAL
);
CREATE TABLE IF NOT EXISTS visit_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    vid      TEXT DEFAULT '',
    user_id  INTEGER DEFAULT 0,
    name     TEXT DEFAULT '',          -- media_play, store_open, link_open, login…
    detail   TEXT DEFAULT '',
    path     TEXT DEFAULT '',
    store_id INTEGER DEFAULT 0,
    file_id  INTEGER DEFAULT 0,
    ts       REAL
);
CREATE INDEX IF NOT EXISTS idx_visits_seen   ON visits(last_seen);
CREATE INDEX IF NOT EXISTS idx_views_ts      ON page_views(ts);
CREATE INDEX IF NOT EXISTS idx_views_vid     ON page_views(vid);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON visit_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_name   ON visit_events(name);

-- Hand-picked media for the public auto-playing reel (home page). Pinned items
-- come first, then the newest free videos.
CREATE TABLE IF NOT EXISTS feed_pins (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER,
    msg_id   INTEGER,
    kind     TEXT DEFAULT 'video',
    title    TEXT DEFAULT '',
    position INTEGER DEFAULT 0,
    added_at REAL
);
CREATE INDEX IF NOT EXISTS idx_feed_pins ON feed_pins(position, id);

-- Play counter for the public reel — "how many people actually watched".
CREATE TABLE IF NOT EXISTS feed_plays (
    item    TEXT PRIMARY KEY,           -- file:<id> / pin:<id>
    plays   INTEGER DEFAULT 0,
    last_ts REAL
);
"""


def _json_int_list(raw: Any) -> list[int]:
    """Read the `campaigns.files` JSON column tolerantly (never raises)."""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    out: list[int] = []
    for item in data if isinstance(data, (list, tuple)) else []:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


class Database:
    def __init__(self, path: str):
        self.path = path
        self.backend = 'postgresql' if str(path).startswith(('postgresql://', 'postgres://')) else 'sqlite'
        if self.backend == 'postgresql':
            from app.postgres import Connection
            self.conn = Connection(path, SCHEMA, sslmode=getattr(cfg, 'PG_SSLMODE', 'require'))
            try:
                with self.conn.transaction():
                    self.conn.raw.execute('SELECT pg_advisory_xact_lock(182861, 1)')
                    self.conn.executescript(SCHEMA)
                    self._upgrade()
            except Exception:
                self.conn.close()
                raise
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=10000")
            self.conn.executescript(SCHEMA)
            self._upgrade()
            self.conn.commit()

    @property
    def display_path(self):
        if self.backend == 'postgresql':
            from urllib.parse import urlsplit
            return 'PostgreSQL · ' + (urlsplit(self.path).hostname or '') + ' / bfixf'
        return self.path

    def _upgrade(self) -> None:
        """Add columns that were introduced after the first release."""
        for table, column, ddl in (
            ("users", "lang", "ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'bn'"),
            ("users", "sort_pref", "ALTER TABLE users ADD COLUMN sort_pref TEXT DEFAULT 'default'"),
            ("orders", "proof", "ALTER TABLE orders ADD COLUMN proof TEXT"),
            ("campaigns", "buttons", "ALTER TABLE campaigns ADD COLUMN buttons TEXT DEFAULT ''"),
            ("campaigns", "slug", "ALTER TABLE campaigns ADD COLUMN slug TEXT DEFAULT ''"),
            ("campaigns", "media_caption", "ALTER TABLE campaigns ADD COLUMN media_caption TEXT DEFAULT 'above'"),
            ("campaigns", "limit_note", "ALTER TABLE campaigns ADD COLUMN limit_note TEXT DEFAULT ''"),
            ("campaigns", "online_button", "ALTER TABLE campaigns ADD COLUMN online_button INTEGER DEFAULT 1"),
            ("links", "max_clicks", "ALTER TABLE links ADD COLUMN max_clicks INTEGER DEFAULT 0"),
            ("links", "clicks", "ALTER TABLE links ADD COLUMN clicks INTEGER DEFAULT 0"),
            ("links", "kind2", "ALTER TABLE links ADD COLUMN kind2 TEXT DEFAULT ''"),
            ("links", "per_user_limit", "ALTER TABLE links ADD COLUMN per_user_limit INTEGER DEFAULT 0"),
            ("links", "caption", "ALTER TABLE links ADD COLUMN caption TEXT DEFAULT ''"),
            ("links", "buttons", "ALTER TABLE links ADD COLUMN buttons TEXT DEFAULT ''"),
            ("link_uses", "times", "ALTER TABLE link_uses ADD COLUMN times INTEGER DEFAULT 1"),
            ("files", "uid", "ALTER TABLE files ADD COLUMN uid TEXT"),
            ("files", "mirror_chat", "ALTER TABLE files ADD COLUMN mirror_chat INTEGER"),
            ("files", "mirror_msg", "ALTER TABLE files ADD COLUMN mirror_msg INTEGER"),
            ("files", "mirror_ts", "ALTER TABLE files ADD COLUMN mirror_ts REAL"),
            ("files", "bytes", "ALTER TABLE files ADD COLUMN bytes INTEGER"),
            ("files", "locked", "ALTER TABLE files ADD COLUMN locked INTEGER DEFAULT 0"),
            ("stores", "forcejoin", "ALTER TABLE stores ADD COLUMN forcejoin TEXT DEFAULT ''"),
            ("stores", "publish_channel_id", "ALTER TABLE stores ADD COLUMN publish_channel_id INTEGER"),
            ("stores", "publish_channel_title", "ALTER TABLE stores ADD COLUMN publish_channel_title TEXT DEFAULT ''"),
            ("stores", "publish_channel_link", "ALTER TABLE stores ADD COLUMN publish_channel_link TEXT DEFAULT ''"),
            ("stores", "auto_post_enabled", "ALTER TABLE stores ADD COLUMN auto_post_enabled INTEGER DEFAULT 0"),
            # ------------------------------------------------ v3.1 broadcast reach
            # Telegram needs (user_id + access_hash) to send to somebody. Telethon
            # keeps that in the session file only; hosting panels wipe the disk, so
            # the hash is cached in the database and reused forever after.
            ("users", "access_hash", "ALTER TABLE users ADD COLUMN access_hash INTEGER"),
            ("users", "reachable", "ALTER TABLE users ADD COLUMN reachable INTEGER DEFAULT 1"),
            ("users", "last_error", "ALTER TABLE users ADD COLUMN last_error TEXT DEFAULT ''"),
            ("campaigns", "silent", "ALTER TABLE campaigns ADD COLUMN silent INTEGER DEFAULT 0"),
            ("campaigns", "protect", "ALTER TABLE campaigns ADD COLUMN protect INTEGER DEFAULT 1"),
            ("campaigns", "origin", "ALTER TABLE campaigns ADD COLUMN origin TEXT DEFAULT 'panel'"),
            # The campaign page shows the hand-picked media as tiles; the inbox id
            # links each tile to `/inbox/<id>` so the owner can check the actual file.
            ("campaign_media", "item_id",
             "ALTER TABLE campaign_media ADD COLUMN item_id INTEGER DEFAULT 0"),
            # Slow mode: spread a big list over N minutes instead of one burst
            # (Telegram allows ~30 messages/second in total).
            ("campaigns", "pause_seconds",
             "ALTER TABLE campaigns ADD COLUMN pause_seconds REAL DEFAULT 0"),
            # -------------------------------------------- v3.4 media-group drafts
            # The `/br` studio can draft several items (text, photo, video, GIF,
            # file, audio, voice — one admin message each) and send them exactly
            # like that. `campaign_groups` keeps that list; `items` (below) keeps
            # an older single-file list alive for existing campaigns.
            ("campaign_items", "draft_id",
             "ALTER TABLE campaign_items ADD COLUMN draft_id INTEGER DEFAULT 0"),
        ):
            columns = {row["name"] for row in
                       self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                try:
                    self.conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass

    # ------------------------------------------------------------------ basics
    def _q(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def _run(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    # -------------------------------------------------------------------- meta
    def get_meta(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._run(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # ------------------------------------------------------------------- users
    def touch_user(self, user_id: int, name: str | None = None,
                   username: str | None = None) -> bool:
        """Register/refresh a user. Returns True when the user is brand new."""
        now = time.time()
        row = self._one("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if row is None:
            self._run(
                "INSERT INTO users(user_id, name, username, joined_at, last_seen) "
                "VALUES(?, ?, ?, ?, ?)",
                (user_id, name or "Unknown", username, now, now),
            )
            return True
        self._run(
            "UPDATE users SET last_seen = ?, "
            "name = COALESCE(?, name), username = COALESCE(?, username) "
            "WHERE user_id = ?",
            (now, name, username, user_id),
        )
        return False

    def user(self, user_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM users WHERE user_id = ?", (user_id,)))

    # ------------------------------------------------- Telegram peer (access hash)
    # A bot can only message a user it knows the `access_hash` of. Telethon caches
    # that inside the .session file; when the file is gone (new host, wiped disk,
    # "session created empty") every broadcast silently fails with
    # “Could not find the input entity for PeerUser”. Remembering the hash in the
    # database is what makes `/br` keep working after a restart.
    # -------------------------------------------------------- channel/chat peers
    def set_chat_peer(self, peer_id: int, access_hash: int | None,
                      title: str | None = None, username: str | None = None,
                      kind: str = "channel") -> None:
        """Remember a channel/group's access hash (see `app/services/peers.py`)."""
        self._run(
            "INSERT INTO chat_peers(peer_id, access_hash, title, username, kind, updated_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(peer_id) DO UPDATE SET"
            " access_hash = COALESCE(excluded.access_hash, access_hash),"
            " title = COALESCE(NULLIF(excluded.title, ''), title),"
            " username = COALESCE(NULLIF(excluded.username, ''), username),"
            " kind = excluded.kind, updated_at = excluded.updated_at",
            (int(peer_id), access_hash, (title or "")[:200], (username or "")[:64],
             (kind or "channel")[:16], time.time()),
        )

    def chat_peer(self, peer_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM chat_peers WHERE peer_id = ?",
                                    (int(peer_id),)))

    def chat_peers(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM chat_peers ORDER BY updated_at DESC LIMIT ?", (int(limit),))]

    def set_user_peer(self, user_id: int, access_hash: int | None,
                      name: str | None = None, username: str | None = None) -> None:
        user_id = int(user_id)
        now = time.time()
        row = self._one("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if row is None:
            self._run(
                "INSERT INTO users(user_id, name, username, joined_at, last_seen,"
                " access_hash, reachable) VALUES(?,?,?,?,?,?,1)",
                (user_id, name or "Unknown", username, now, now, access_hash),
            )
            return
        self._run(
            "UPDATE users SET access_hash = COALESCE(?, access_hash),"
            " name = COALESCE(?, name), username = COALESCE(?, username),"
            " last_seen = ?, reachable = 1, last_error = '' WHERE user_id = ?",
            (access_hash, name, username, now, user_id),
        )

    def user_peer(self, user_id: int) -> dict | None:
        """{access_hash, reachable, name, username} or None."""
        row = self._one(
            "SELECT access_hash, reachable, name, username, last_error FROM users"
            " WHERE user_id = ?", (int(user_id),))
        return self._dict(row)

    def mark_unreachable(self, user_id: int, reason: str = "") -> None:
        """Remember why a user could not be reached, so the panel can explain it."""
        try:
            self._run("UPDATE users SET reachable = 0, last_error = ? WHERE user_id = ?",
                      ((reason or "")[:200], int(user_id)))
        except Exception:
            pass

    def mark_reachable(self, user_id: int) -> None:
        try:
            self._run("UPDATE users SET reachable = 1, last_error = '' WHERE user_id = ?",
                      (int(user_id),))
        except Exception:
            pass

    def reachability(self) -> dict:
        """Small summary for the dashboard: how many users we can actually reach."""
        row = self._one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN COALESCE(reachable,1) = 1 THEN 1 ELSE 0 END) AS reachable,"
            " SUM(CASE WHEN access_hash IS NULL THEN 1 ELSE 0 END) AS missing_hash"
            " FROM users")
        return {"total": int(row["total"] or 0), "reachable": int(row["reachable"] or 0),
                "missing_hash": int(row["missing_hash"] or 0)}

    def delete_user(self, user_id: int) -> bool:
        """Remove one user and every trace of them (grants, orders, events…).

        Nothing that belongs to a *store* is touched — only this person's own data.
        """
        row = self._one("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if not row:
            return False
        for table, column in (
            ("users", "user_id"), ("grants", "user_id"), ("orders", "user_id"),
            ("events", "user_id"), ("file_views", "user_id"), ("favorites", "user_id"),
            ("drip_subs", "user_id"), ("drip_sent", "user_id"), ("link_uses", "user_id"),
            ("join_prompts", "user_id"), ("tickets", "user_id"), ("referrals", "user_id"),
            ("requests", "user_id"), ("banned", "user_id"),
        ):
            try:
                self._run(f"DELETE FROM {table} WHERE {column} = ?", (user_id,))
            except Exception:
                pass                      # a table that does not exist in this build
        return True

    def delete_users(self, user_ids: Iterable[int]) -> int:
        return sum(1 for uid in unique_keep_order([int(i) for i in user_ids if i])
                   if self.delete_user(uid))

    def purge_user_ids(self, *, days: int = 0, never_started: bool = False,
                       keep_admins: Iterable[int] = ()) -> list[int]:
        """Which users *would* be deleted — the panel shows this before deleting.

        ``days`` = nobody who was active in the last N days (0 = ignore activity).
        ``never_started`` = only people who joined but never sent a message/started.
        """
        keep = {int(i) for i in keep_admins if i}
        rows = self._q("SELECT user_id, joined_at, last_seen FROM users")
        cutoff = time.time() - days * 86400 if days else 0
        out: list[int] = []
        for row in rows:
            uid = row["user_id"]
            if uid in keep:
                continue
            seen = row["last_seen"] or row["joined_at"] or 0
            if cutoff and seen >= cutoff:
                continue
            if never_started and seen:
                continue
            out.append(uid)
        return out

    def all_user_ids(self) -> list[int]:
        return [r["user_id"] for r in self._q("SELECT user_id FROM users ORDER BY user_id")]

    def recent_user_ids(self, since: float) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT user_id FROM users WHERE COALESCE(last_seen, joined_at, 0) >= ?", (since,))]

    def user_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM users")["c"]

    def all_users_brief(self) -> list[dict]:
        """`user_id, username, name` for everybody — used by broadcast reports.

        A report lists thousands of rows; returning just these three columns keeps
        it cheap instead of loading every user record with all its settings.
        """
        return [dict(r) for r in self._q(
            "SELECT user_id, COALESCE(username, '') AS username, COALESCE(name, '') AS name"
            " FROM users ORDER BY user_id")]

    def users_brief(self, user_ids: Iterable[int]) -> dict[int, dict]:
        """The same, but only for the given ids (a report often needs a fraction)."""
        wanted = [int(uid) for uid in dict.fromkeys(user_ids) if uid]
        out: dict[int, dict] = {}
        for start in range(0, len(wanted), 400):          # stay under SQLite's limit
            chunk = wanted[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            for row in self._q(
                    f"SELECT user_id, COALESCE(username, '') AS username,"
                    f" COALESCE(name, '') AS name FROM users WHERE user_id IN ({marks})",
                    tuple(chunk)):
                out[int(row["user_id"])] = dict(row)
        return out

    # ------------------------------------------------------------ admin state
    def active_store_id(self, admin_id: int) -> int | None:
        row = self._one("SELECT active_store_id FROM admin_state WHERE admin_id = ?", (admin_id,))
        return row["active_store_id"] if row else None

    def set_active_store(self, admin_id: int, store_id: int | None) -> None:
        self._run(
            "INSERT INTO admin_state(admin_id, active_store_id) VALUES(?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET active_store_id = excluded.active_store_id",
            (admin_id, store_id),
        )

    # ------------------------------------------------------------------ stores
    def _free_slug(self, name: str) -> str:
        base = slugify(name)
        slug, i = base, 1
        while self._one("SELECT 1 FROM stores WHERE slug = ?", (slug,)):
            i += 1
            slug = f"{base[:17]}_{i}"
        return slug

    def create_store(self, admin_id: int, name: str) -> dict:
        row = self._one(
            "SELECT * FROM stores WHERE admin_id = ? AND name = ?", (admin_id, name)
        )
        if row:
            return dict(row)
        self._run(
            "INSERT INTO stores(admin_id, name, slug, created_at, is_premium) "
            "VALUES(?, ?, ?, ?, 1)",
            (admin_id, name, self._free_slug(name), time.time()),
        )
        store = dict(self._one(
            "SELECT * FROM stores WHERE admin_id = ? AND name = ?", (admin_id, name)
        ))
        # Audit trail (services/audit) reads these names straight off `events`.
        # NOTE: audit rows keep `store_id` empty on purpose — the funnel/analytics
        # queries count any event that carries a store id, and an admin action is
        # not a visitor walking through the funnel. The store lives in `ref`.
        self.log_event("store_created", admin_id, None, None, f"s{store['id']} {name}")
        return store

    def store(self, store_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM stores WHERE id = ?", (store_id,)))

    def store_by_name(self, admin_id: int, name: str) -> dict | None:
        return self._dict(
            self._one("SELECT * FROM stores WHERE admin_id = ? AND name = ?", (admin_id, name))
        )

    def store_by_slug(self, slug: str) -> dict | None:
        return self._dict(self._one("SELECT * FROM stores WHERE slug = ?", (slug,)))

    def stores_admin(self, admin_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM stores WHERE admin_id = ? ORDER BY id", (admin_id,)
        )]

    def all_stores(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM stores ORDER BY id")]

    def update_store(self, store_id: int, **fields: Any) -> None:
        allowed = {
            "name", "slug", "is_premium", "cover", "description", "forcejoin",
            "publish_channel_id", "publish_channel_title", "publish_channel_link",
            "auto_post_enabled",
        }
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        params.append(store_id)
        self._run(f"UPDATE stores SET {', '.join(sets)} WHERE id = ?", params)

    def delete_store(self, store_id: int) -> None:
        """Delete a store *and everything that pointed at it* (no orphan data)."""
        file_ids = [r["id"] for r in self._q("SELECT id FROM files WHERE store_id = ?", (store_id,))]
        tokens = [r["token"] for r in self._q(
            "SELECT DISTINCT token FROM link_files WHERE file_id IN "
            "(SELECT id FROM files WHERE store_id = ?)", (store_id,)
        )] if file_ids else []
        self._run("DELETE FROM favorites WHERE file_id IN "
                  "(SELECT id FROM files WHERE store_id = ?)", (store_id,))
        self._run("DELETE FROM requests WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM plans WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM files WHERE store_id = ?", (store_id,))
        if file_ids:
            marks = ",".join("?" for _ in file_ids)
            self._run(f"DELETE FROM link_files WHERE file_id IN ({marks})", file_ids)
            self._run(f"DELETE FROM drip_sent WHERE file_id IN ({marks})", file_ids)
        for token in tokens:
            if not self._one("SELECT 1 FROM link_files WHERE token = ?", (token,)):
                self._run("DELETE FROM links WHERE token = ?", (token,))
        self._run("DELETE FROM grants WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM drip WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM drip_subs WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM drip_sent WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM referral_cfg WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM admin_state WHERE active_store_id = ?", (store_id,))
        self._run("DELETE FROM stores WHERE id = ?", (store_id,))
        self.log_event("store_deleted", None, None, None, f"s{store_id}")

    def store_stats(self, store_id: int) -> dict:
        row = self._one(
            "SELECT COUNT(*) AS files, COALESCE(SUM(views), 0) AS views "
            "FROM files WHERE store_id = ?", (store_id,)
        )
        grants = self._one("SELECT COUNT(*) AS c FROM grants WHERE store_id = ?", (store_id,))["c"]
        subs = self._one("SELECT COUNT(*) AS c FROM drip_subs WHERE store_id = ?", (store_id,))["c"]
        return {"files": row["files"], "views": row["views"], "grants": grants, "subs": subs}

    # ------------------------------------------------------------------- files
    def add_file(self, store_id: int, name: str, kind: str, chat_id: int,
                 msg_id: int, code: str | None = None, size: int | None = None,
                 duration: int | None = None) -> int | None:
        """Add media to a store. Returns the new id, or None for duplicates."""
        if code and self._one("SELECT 1 FROM files WHERE code = ?", (code,)):
            return None
        if self._one(
            "SELECT 1 FROM files WHERE store_id = ? AND chat_id = ? AND msg_id = ?",
            (store_id, chat_id, msg_id),
        ):
            return None
        cur = self._run(
            "INSERT INTO files(store_id, name, kind, chat_id, msg_id, code, size, duration, created_at, views) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (store_id, name, kind, chat_id, msg_id, code, size, duration, time.time()),
        )
        return cur.lastrowid

    def file(self, file_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM files WHERE id = ?", (file_id,)))

    def file_by_code(self, code: str) -> dict | None:
        return self._dict(self._one("SELECT * FROM files WHERE code = ?", (code,)))

    def files_of(self, store_id: int, newest_first: bool = False) -> list[dict]:
        order = "created_at DESC, id DESC" if newest_first else "id"
        return [dict(r) for r in self._q(
            f"SELECT * FROM files WHERE store_id = ? ORDER BY {order}", (store_id,)
        )]

    def search_files(self, store_id: int, keyword: str, limit: int = 20) -> list[dict]:
        like = f"%{keyword.lower()}%"
        return [dict(r) for r in self._q(
            "SELECT * FROM files WHERE store_id = ? AND LOWER(name) LIKE ? LIMIT ?",
            (store_id, like, limit),
        )]

    def latest_files(self, store_id: int, limit: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM files WHERE store_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (store_id, limit),
        )]

    def rename_file(self, file_id: int, name: str) -> None:
        self._run("UPDATE files SET name = ? WHERE id = ?", (name, file_id))

    def delete_file(self, file_id: int) -> int | None:
        row = self._one("SELECT store_id FROM files WHERE id = ?", (file_id,))
        if not row:
            return None
        store_id = row["store_id"]
        self._run("DELETE FROM files WHERE id = ?", (file_id,))
        self._run("DELETE FROM favorites WHERE file_id = ?", (file_id,))
        self._run("DELETE FROM link_files WHERE file_id = ?", (file_id,))
        self._run("DELETE FROM drip_sent WHERE file_id = ?", (file_id,))
        return store_id

    def delete_files(self, file_ids: Iterable[int]) -> int:
        """🗑 Delete many files at once — “ভিডিও একসাথে অনেকগুলো মুছে ফেলব”. """
        count = 0
        for file_id in unique_keep_order([int(i) for i in file_ids if i]):
            if self.delete_file(file_id) is not None:
                count += 1
        return count

    def move_file(self, file_id: int, store_id: int) -> bool:
        if not self._one("SELECT 1 FROM stores WHERE id = ?", (store_id,)):
            return False
        self._run("UPDATE files SET store_id = ? WHERE id = ?", (store_id, file_id))
        return True

    def bump_views(self, file_id: int) -> None:
        self._run("UPDATE files SET views = views + 1 WHERE id = ?", (file_id,))

    def total_files(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM files")["c"]

    def total_views(self) -> int:
        return self._one("SELECT COALESCE(SUM(views), 0) AS c FROM files")["c"]

    def top_files(self, limit: int = 5) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT f.*, s.name AS store_name FROM files f "
            "LEFT JOIN stores s ON s.id = f.store_id "
            "ORDER BY f.views DESC LIMIT ?", (limit,)
        )]

    def dead_files(self, days: int = 30, limit: int = 40) -> list[dict]:
        """Files nobody watched at all in the window — the “এই ফাইলগুলো বেকার” report.

        A file counts as dead when it has zero `deliver` events since `days` ago
        *and* zero lifetime views, so brand-new uploads are not accused of being
        unwanted before anyone has had a chance to see them.
        """
        since = time.time() - max(1, int(days)) * 86400
        return [dict(r) for r in self._q(
            "SELECT f.*, s.name AS store_name, "
            "  (SELECT COUNT(*) FROM events e WHERE e.file_id = f.id "
            "     AND e.name = 'deliver' AND e.ts >= ?) AS recent_delivers, "
            "  (SELECT COUNT(*) FROM events e WHERE e.file_id = f.id "
            "     AND e.name = 'open_store') AS store_opens "
            "FROM files f LEFT JOIN stores s ON s.id = f.store_id "
            "WHERE COALESCE(f.views, 0) = 0 "
            "ORDER BY f.created_at ASC LIMIT ?", (since, max(1, int(limit))))]

    def conversion_rate(self, days: int = 30) -> dict:
        """Free → paid conversion: how many people who came actually bought."""
        since = time.time() - max(1, int(days)) * 86400
        came = self._one("SELECT COUNT(DISTINCT user_id) AS c FROM events "
                         "WHERE ts >= ? AND name IN ('start','open_store')", (since,))
        paid = self._one("SELECT COUNT(DISTINCT user_id) AS c FROM events "
                         "WHERE ts >= ? AND name = 'paid'", (since,))
        came_n = int((came or {"c": 0})["c"] or 0)
        paid_n = int((paid or {"c": 0})["c"] or 0)
        return {"came": came_n, "paid": paid_n,
                "rate": (paid_n / came_n * 100) if came_n else 0.0}

    def store_id_of_file(self, file_id: int) -> int | None:
        row = self._one("SELECT store_id FROM files WHERE id = ?", (file_id,))
        return row["store_id"] if row else None

    # ------------------------------------------------------------------- links
    def create_link(self, admin_id: int, file_ids: Iterable[int],
                    expires_at: float | None, kind: str = "multi",
                    max_clicks: int = 0, note: str = "",
                    per_user_limit: int = 0) -> str:
        from app.utils import new_token
        token = new_token(10)
        self._run(
            "INSERT INTO links(token, admin_id, kind, created_at, expires_at, max_clicks, "
            "kind2, per_user_limit) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (token, admin_id, kind, time.time(), expires_at, max(0, int(max_clicks or 0)),
             note, max(0, int(per_user_limit or 0))),
        )
        for fid in unique_keep_order(file_ids):
            self._run("INSERT OR IGNORE INTO link_files(token, file_id) VALUES(?, ?)", (token, fid))
        return token

    def link(self, token: str) -> dict | None:
        return self._dict(self._one("SELECT * FROM links WHERE token = ?", (token,)))

    def link_file_ids(self, token: str) -> list[int]:
        return [r["file_id"] for r in self._q(
            "SELECT lf.file_id FROM link_files lf JOIN files f ON f.id = lf.file_id "
            "WHERE lf.token = ? ORDER BY f.id", (token,)
        )]

    # ---- per-link customisation: caption, inline buttons, own force-join channels
    def link_update(self, token: str, **fields) -> None:
        """Set `caption` and/or `buttons` of one link."""
        allowed = {"caption", "buttons"}
        pairs = [(k, v) for k, v in fields.items() if k in allowed]
        if not pairs:
            return
        sets = ", ".join(f"{k} = ?" for k, _ in pairs)
        self._run(f"UPDATE links SET {sets} WHERE token = ?",
                  tuple(v for _, v in pairs) + (token,))

    def link_add_files(self, token: str, file_ids: Iterable[int]) -> int:
        before = len(self.link_file_ids(token))
        for fid in unique_keep_order(file_ids):
            self._run("INSERT OR IGNORE INTO link_files(token, file_id) VALUES(?, ?)", (token, fid))
        return len(self.link_file_ids(token)) - before

    def link_channels(self, token: str) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM link_channels WHERE token = ? ORDER BY id", (token,))]

    def link_channel_add(self, token: str, ref: str, title: str, chat_id: int | None,
                         invite: str = "") -> int:
        self._run("DELETE FROM link_channels WHERE token = ? AND ref = ?", (token, ref))
        cur = self._run(
            "INSERT INTO link_channels(token, ref, title, chat_id, invite, created_at) "
            "VALUES(?,?,?,?,?,?)", (token, ref, title, chat_id, invite, time.time()))
        return int(cur.lastrowid)

    def link_channel(self, row_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM link_channels WHERE id = ?", (row_id,)))

    def link_channel_remove(self, row_id: int) -> None:
        self._run("DELETE FROM link_channels WHERE id = ?", (row_id,))

    def delete_link(self, token: str) -> None:
        self._run("DELETE FROM link_channels WHERE token = ?", (token,))
        self._run("DELETE FROM link_files WHERE token = ?", (token,))
        self._run("DELETE FROM link_uses WHERE token = ?", (token,))
        self._run("DELETE FROM links WHERE token = ?", (token,))

    def link_clicks(self, token: str) -> int:
        row = self._one("SELECT clicks FROM links WHERE token = ?", (token,))
        return int(row["clicks"] or 0) if row else 0

    def link_take(self, token: str, user_id: int | None = None) -> dict:
        """Consume one click of a link. Atomic — two users clicking at the same
        moment can never take more units than the link has left.

        When the link has a `per_user_limit`, one person cannot consume the whole
        batch: the same user is refused after their own N clicks.
        """
        now = time.time()
        row = self._one("SELECT * FROM links WHERE token = ?", (token,))
        if not row:
            return {"ok": False, "reason": "missing"}
        info = dict(row)
        if info.get("expires_at") and info["expires_at"] < now:
            return {"ok": False, "reason": "expired", "link": info}
        per_user = int(info.get("per_user_limit") or 0)
        used_by_me = self.link_uses_of(token, user_id) if (per_user and user_id) else 0
        if per_user and user_id and used_by_me >= per_user:
            return {"ok": False, "reason": "user_limit", "link": info,
                    "per_user_limit": per_user, "used_by_user": used_by_me}
        limit = int(info.get("max_clicks") or 0)
        cur = self.conn.execute(
            "UPDATE links SET clicks = clicks + 1 WHERE token = ? AND (max_clicks = 0 OR clicks < max_clicks)",
            (token,),
        )
        if cur.rowcount == 0:
            return {"ok": False, "reason": "limit", "link": info,
                    "left": 0, "clicks": int(info.get("clicks") or 0), "limit": limit}
        self.conn.commit()
        if user_id:
            self.record_link_use(token, int(user_id))
        clicks = self.link_clicks(token)
        return {"ok": True, "link": info, "clicks": clicks, "limit": limit,
                "left": max(0, limit - clicks) if limit else -1,
                "per_user_limit": per_user,
                "used_by_user": used_by_me + 1 if (per_user and user_id) else 0}

    # ------------------------------------------------------------ link usage
    def record_link_use(self, token: str, user_id: int) -> None:
        """Remember who took the link (drives the per-user limit and the report)."""
        self._run(
            "INSERT INTO link_uses(token, user_id, used_at, times) VALUES(?, ?, ?, 1) "
            "ON CONFLICT(token, user_id) DO UPDATE SET used_at = excluded.used_at, "
            "times = COALESCE(link_uses.times, 0) + 1",
            (token, int(user_id), time.time()),
        )

    def link_uses_of(self, token: str, user_id: int | None) -> int:
        if not user_id:
            return 0
        row = self._one("SELECT times FROM link_uses WHERE token = ? AND user_id = ?",
                        (token, int(user_id)))
        return int(row["times"] or 0) if row else 0

    def link_uses(self, token: str) -> list[dict]:
        """Who took this link, and how many times (for the admin panel)."""
        rows = self._q(
            "SELECT u.user_id, COALESCE(u.times, 1) AS times, u.used_at, us.name, us.username "
            "FROM link_uses u LEFT JOIN users us ON us.user_id = u.user_id "
            "WHERE u.token = ? ORDER BY u.used_at DESC", (token,),
        )
        return [dict(r) for r in rows]

    def purge_expired_links(self) -> int:
        now = time.time()
        rows = self._q("SELECT token FROM links WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
        for row in rows:
            self.delete_link(row["token"])
        return len(rows)

    def links(self, limit: int = 100, admin_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM links"
        params: list[Any] = []
        if admin_id:
            sql += " WHERE admin_id = ?"
            params.append(int(admin_id))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._q(sql, params)]

    def link_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM links")["c"]

    # ------------------------------------------------------------------ grants
    def grant(self, store_id: int, user_id: int, expires_at: float | None,
              source: str = "admin") -> None:
        self._run(
            "INSERT INTO grants(store_id, user_id, expires_at, granted_at, source) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(store_id, user_id) DO UPDATE SET "
            "expires_at = excluded.expires_at, granted_at = excluded.granted_at, "
            "source = excluded.source",
            (store_id, user_id, expires_at, time.time(), source),
        )
        # “কে এই অ্যাক্সেসটা দিল” — shown on /admin/audit.
        self.log_event("grant", user_id, None, None, f"s{store_id} {source}",
                       "lifetime" if expires_at is None else "expires")

    def extend_grant(self, store_id: int, user_id: int, days: int, source: str = "referral") -> float:
        """Add days on top of a still valid grant (instead of overwriting it)."""
        row = self._one("SELECT expires_at FROM grants WHERE store_id = ? AND user_id = ?",
                        (store_id, user_id))
        now = time.time()
        if row is None:
            base = now
        elif row["expires_at"] is None:
            return now  # already lifetime, nothing to extend
        else:
            base = max(row["expires_at"], now)
        new_expiry = base + days * 86400
        self.grant(store_id, user_id, new_expiry, source)
        return new_expiry

    def revoke(self, store_id: int, user_id: int, source: str = "admin") -> None:
        self._run("DELETE FROM grants WHERE store_id = ? AND user_id = ?", (store_id, user_id))
        self.log_event("revoke", user_id, None, None, f"s{store_id} {source}")

    def grant_row(self, store_id: int, user_id: int) -> dict | None:
        return self._dict(self._one(
            "SELECT * FROM grants WHERE store_id = ? AND user_id = ?", (store_id, user_id)
        ))

    def store_grants(self, store_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT g.*, u.name AS user_name FROM grants g "
            "LEFT JOIN users u ON u.user_id = g.user_id WHERE g.store_id = ? "
            "ORDER BY g.granted_at DESC", (store_id,)
        )]

    def user_grants(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT g.*, s.name AS store_name, s.is_premium FROM grants g "
            "JOIN stores s ON s.id = g.store_id WHERE g.user_id = ?", (user_id,)
        )]

    def grant_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM grants")["c"]

    def paid_users(self) -> set[int]:
        return {r["user_id"] for r in self._q("SELECT DISTINCT user_id FROM grants")}

    def expired_grants(self, within_seconds: float = 3 * 86400) -> list[dict]:
        """Grants that expire within `within_seconds` (used for renewal reminders)."""
        now = time.time()
        return [dict(r) for r in self._q(
            "SELECT g.*, s.name AS store_name FROM grants g JOIN stores s ON s.id = g.store_id "
            "WHERE g.expires_at IS NOT NULL AND g.expires_at > ? AND g.expires_at < ?",
            (now, now + within_seconds),
        )]

    # --------------------------------------------------------------- referrals
    def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        if user_id == referrer_id:
            return False
        if self._one("SELECT 1 FROM referrals WHERE user_id = ?", (user_id,)):
            return False
        self._run(
            "INSERT INTO referrals(user_id, referrer_id, rewarded, created_at) VALUES(?, ?, 0, ?)",
            (user_id, referrer_id, time.time()),
        )
        return True

    def referrer_of(self, user_id: int) -> int | None:
        row = self._one("SELECT referrer_id FROM referrals WHERE user_id = ?", (user_id,))
        return row["referrer_id"] if row else None

    def is_rewarded(self, user_id: int) -> bool:
        row = self._one("SELECT rewarded FROM referrals WHERE user_id = ?", (user_id,))
        return bool(row and row["rewarded"])

    def mark_rewarded(self, user_id: int) -> None:
        self._run("UPDATE referrals SET rewarded = 1 WHERE user_id = ?", (user_id,))

    def invite_count(self, referrer_id: int) -> int:
        return self._one(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (referrer_id,)
        )["c"]

    def top_referrers(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT referrer_id, COUNT(*) AS invites FROM referrals GROUP BY referrer_id ORDER BY invites DESC LIMIT ?",
            (limit,)
        )]

    def set_referral_cfg(self, admin_id: int, store_id: int, days: int) -> None:
        self._run(
            "INSERT INTO referral_cfg(admin_id, store_id, days) VALUES(?, ?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET store_id = excluded.store_id, days = excluded.days",
            (admin_id, store_id, days),
        )

    def referral_cfg(self, admin_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM referral_cfg WHERE admin_id = ?", (admin_id,)))

    def all_referral_cfgs(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM referral_cfg")]

    # -------------------------------------------------------------------- drip
    def upsert_drip(self, store_id: int, admin_id: int, count: int, send_time: str) -> None:
        self._run(
            "INSERT INTO drip(store_id, admin_id, count, send_time, last_sent_date, enabled) "
            "VALUES(?, ?, ?, ?, '', 1) "
            "ON CONFLICT(store_id) DO UPDATE SET count = excluded.count, "
            "send_time = excluded.send_time, enabled = 1",
            (store_id, admin_id, count, send_time),
        )

    def set_drip_enabled(self, store_id: int, enabled: bool) -> None:
        self._run("UPDATE drip SET enabled = ? WHERE store_id = ?", (1 if enabled else 0, store_id))

    def set_drip_last_sent(self, store_id: int, date_str: str) -> None:
        self._run("UPDATE drip SET last_sent_date = ? WHERE store_id = ?", (date_str, store_id))

    def drip(self, store_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM drip WHERE store_id = ?", (store_id,)))

    def all_drips(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM drip WHERE enabled = 1")]

    def toggle_sub(self, store_id: int, user_id: int) -> bool:
        if self.is_sub(store_id, user_id):
            self._run("DELETE FROM drip_subs WHERE store_id = ? AND user_id = ?", (store_id, user_id))
            return False
        self._run("INSERT OR IGNORE INTO drip_subs(store_id, user_id) VALUES(?, ?)", (store_id, user_id))
        return True

    def is_sub(self, store_id: int, user_id: int) -> bool:
        return bool(self._one(
            "SELECT 1 FROM drip_subs WHERE store_id = ? AND user_id = ?", (store_id, user_id)
        ))

    def subscribers(self, store_id: int) -> list[int]:
        return [r["user_id"] for r in self._q(
            "SELECT user_id FROM drip_subs WHERE store_id = ?", (store_id,)
        )]

    def drop_sub(self, store_id: int, user_id: int) -> None:
        self._run("DELETE FROM drip_subs WHERE store_id = ? AND user_id = ?", (store_id, user_id))

    def sent_file_ids(self, store_id: int) -> set[int]:
        return {r["file_id"] for r in self._q(
            "SELECT file_id FROM drip_sent WHERE store_id = ?", (store_id,)
        )}

    def mark_sent(self, store_id: int, file_ids: Iterable[int]) -> None:
        for fid in file_ids:
            self._run("INSERT OR IGNORE INTO drip_sent(store_id, file_id) VALUES(?, ?)",
                      (store_id, fid))

    # ---------------------------------------------------------------- sessions
    def save_session(self, admin_id: int, session_str: str, owner_id: int | None = None,
                     owner_name: str | None = None) -> None:
        self._run(
            "INSERT INTO sessions(admin_id, session_str, owner_id, owner_name, added_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET session_str = excluded.session_str, "
            "owner_id = excluded.owner_id, owner_name = excluded.owner_name",
            (admin_id, session_str, owner_id, owner_name, time.time()),
        )

    def session(self, admin_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM sessions WHERE admin_id = ?", (admin_id,)))

    def all_sessions(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM sessions ORDER BY admin_id")]

    def delete_session(self, admin_id: int) -> None:
        self._run("DELETE FROM sessions WHERE admin_id = ?", (admin_id,))

    # --------------------------------------------------------------- bot chats
    def add_bot_chat(self, chat_id: int, title: str = "") -> None:
        self._run("INSERT OR IGNORE INTO bot_chats(chat_id, title) VALUES(?, ?)", (chat_id, title))
        if title:
            # The channel list (📡 Channels / composer dropdown) reads titles from
            # meta, so a scanned channel shows up with its real name.
            self.set_meta(f"chat_title:{chat_id}", title)
            self._run("UPDATE bot_chats SET title = ? WHERE chat_id = ? AND (title IS NULL OR title = '')",
                      (title, chat_id))

    def bot_chats(self) -> set[int]:
        return {r["chat_id"] for r in self._q("SELECT chat_id FROM bot_chats")}

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict:
        return {
            "users": self.user_count(),
            "files": self.total_files(),
            "views": self.total_views(),
            "stores": self._one("SELECT COUNT(*) AS c FROM stores")["c"],
            "grants": self.grant_count(),
            "links": self.link_count(),
            "paid_users": len(self.paid_users()),
            "referrals": self._one("SELECT COUNT(*) AS c FROM referrals")["c"],
            "orders_pending": self.pending_order_count(),
            "tickets_open": self.open_ticket_count(),
            "requests_open": self.open_request_count(),
            "revenue": self.revenue()["total"],
        }

    # ------------------------------------------------------- export / backup
    def export_dict(self) -> dict:
        """Human readable snapshot (same shape as the old bot_db.json + extras)."""
        stores_out: dict[str, dict] = {}
        for store in self.all_stores():
            files_out = {}
            for f in self.files_of(store["id"]):
                files_out[str(f["id"])] = {
                    "id": f["id"], "code": f["code"], "name": f["name"], "type": f["kind"],
                    "msg_id": f["msg_id"], "chat_id": f["chat_id"], "views": f["views"],
                    "created_at": f["created_at"],
                }
            stores_out[str(store["id"])] = {
                "name": store["name"], "slug": store["slug"], "admin_id": store["admin_id"],
                "is_premium": store["is_premium"], "files": files_out,
            }
        return {
            "version": 2,
            "exported_at": time.time(),
            "stores": stores_out,
            "users": {str(u["user_id"]): {"name": u["name"], "joined": u["joined_at"]}
                      for u in self._q("SELECT * FROM users")},
            "grants": [
                {"store_id": g["store_id"], "user_id": g["user_id"],
                 "expires_at": g["expires_at"], "source": g["source"]}
                for g in self._q("SELECT * FROM grants")
            ],
            "referrals": {str(r["user_id"]): r["referrer_id"] for r in self._q("SELECT * FROM referrals")},
            "drip": {str(d["store_id"]): dict(d) for d in self._q("SELECT * FROM drip")},
            "stats": self.stats(),
        }

    def backup_json(self, directory: str, keep: int = 7) -> str:
        directory_path = Path(directory)
        directory_path.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = directory_path / f"bot_data-{stamp}.json"
        target.write_text(json.dumps(self.export_dict(), ensure_ascii=False, indent=1),
                          encoding="utf-8")
        # copy the raw sqlite file too — it is the real backup
        try:
            if self.backend == 'postgresql':
                (directory_path / f"bot_data-{stamp}.sqlite3").write_bytes(self.conn.snapshot())
            else:
                shutil.copy2(self.path, directory_path / f"bot_data-{stamp}.sqlite3")
        except Exception as exc:  # pragma: no cover
            log.warning("sqlite backup copy failed: %s", exc)
        backups = sorted(directory_path.glob("bot_data-*"))
        for old in backups[: max(0, len(backups) - keep * 2)]:
            try:
                old.unlink()
            except OSError:
                pass
        return str(target)


    # ------------------------------------------------------------------ plans
    def add_plan(self, store_id: int, name: str, days: int, price: float) -> int:
        cur = self._run(
            "INSERT INTO plans(store_id, name, days, price, active, created_at) VALUES(?, ?, ?, ?, 1, ?)",
            (store_id, name, days, price, time.time()),
        )
        return cur.lastrowid

    def plans(self, store_id: int, only_active: bool = False) -> list[dict]:
        sql = "SELECT * FROM plans WHERE store_id = ?"
        if only_active:
            sql += " AND active = 1"
        sql += " ORDER BY price, days"
        return [dict(r) for r in self._q(sql, (store_id,))]

    def plan(self, plan_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM plans WHERE id = ?", (plan_id,)))

    def delete_plan(self, plan_id: int) -> None:
        self._run("DELETE FROM plans WHERE id = ?", (plan_id,))

    def toggle_plan(self, plan_id: int) -> None:
        self._run("UPDATE plans SET active = 1 - active WHERE id = ?", (plan_id,))

    # ----------------------------------------------------------------- orders
    def create_order(self, user_id: int, store_id: int, plan: dict, method: str,
                     amount: float, currency: str, txn_ref: str = "",
                     note: str = "", proof: str = "") -> int:
        cur = self._run(
            "INSERT INTO orders(user_id, store_id, plan_id, plan_name, days, amount, currency, "
            "method, status, txn_ref, note, proof, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (user_id, store_id, plan["id"], plan["name"], plan["days"], amount, currency,
             method, txn_ref, note, proof, time.time()),
        )
        return cur.lastrowid

    def order(self, order_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM orders WHERE id = ?", (order_id,)))

    def orders(self, status: str | None = None, user_id: int | None = None,
               limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM orders WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._q(sql, params)]

    def update_order_proof(self, order_id: int, proof: str) -> None:
        self._run("UPDATE orders SET proof = ? WHERE id = ?", (proof, order_id))

    def decide_order(self, order_id: int, status: str, admin_id: int,
                     admin_note: str = "") -> None:
        self._run(
            "UPDATE orders SET status = ?, decided_at = ?, decided_by = ?, admin_note = ? "
            "WHERE id = ?",
            (status, time.time(), admin_id, admin_note, order_id),
        )

    def orders_since(self, since: float, status: str | None = None) -> list[dict]:
        """Orders created after `since` — powers the daily owner report."""
        sql = "SELECT * FROM orders WHERE created_at >= ?"
        params: list[Any] = [float(since)]
        if status:
            sql += " AND status = ?"
            params.append(status)
        return [dict(r) for r in self._q(sql + " ORDER BY created_at DESC", params)]

    def pending_order_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM orders WHERE status = 'pending'")["c"]

    def revenue(self) -> dict:
        row = self._one(
            "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count, "
            "COALESCE(AVG(amount), 0) AS avg FROM orders WHERE status = 'approved'"
        )
        pending = self.pending_order_count()
        return {"total": row["total"], "count": row["count"], "avg": row["avg"], "pending": pending}

    def revenue_by_day(self, days: int = 14) -> list[tuple[str, float]]:
        since = time.time() - days * 86400
        rows = self._q(
            "SELECT date(decided_at, 'unixepoch') AS d, SUM(amount) AS total "
            "FROM orders WHERE status = 'approved' AND decided_at > ? GROUP BY d ORDER BY d",
            (since,),
        )
        return [(r["d"], r["total"]) for r in rows if r["d"]]

    def user_orders(self, user_id: int, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT o.*, s.name AS store_name FROM orders o LEFT JOIN stores s ON s.id = o.store_id "
            "WHERE o.user_id = ? ORDER BY o.created_at DESC LIMIT ?", (user_id, limit)
        )]

    # ---------------------------------------------------------------- coupons
    def add_coupon(self, code: str, store_id: int | None, percent: int = 0,
                   days: int = 0, max_uses: int = 0,
                   expires_at: float | None = None) -> None:
        self._run(
            "INSERT INTO coupons(code, store_id, percent, days, max_uses, used, expires_at, active, created_at) "
            "VALUES(?, ?, ?, ?, ?, 0, ?, 1, ?) ON CONFLICT(code) DO UPDATE SET "
            "store_id = excluded.store_id, percent = excluded.percent, days = excluded.days, "
            "max_uses = excluded.max_uses, expires_at = excluded.expires_at, active = 1",
            (code.upper(), store_id, percent, days, max_uses, expires_at, time.time()),
        )

    def coupon(self, code: str) -> dict | None:
        return self._dict(self._one("SELECT * FROM coupons WHERE code = ?", (code.upper(),)))

    def coupons(self, store_id: int | None = None) -> list[dict]:
        if store_id:
            return [dict(r) for r in self._q(
                "SELECT * FROM coupons WHERE store_id = ? OR store_id IS NULL ORDER BY created_at DESC",
                (store_id,))]
        return [dict(r) for r in self._q("SELECT * FROM coupons ORDER BY created_at DESC")]

    def use_coupon(self, code: str) -> None:
        self._run("UPDATE coupons SET used = used + 1 WHERE code = ?", (code.upper(),))

    def delete_coupon(self, code: str) -> None:
        self._run("DELETE FROM coupons WHERE code = ?", (code.upper(),))

    def coupon_valid(self, code: str, store_id: int) -> tuple[bool, str]:
        row = self.coupon(code)
        if row is None or not row["active"]:
            return False, "Coupon not found."
        if row["store_id"] not in (None, store_id):
            return False, "This coupon is for another store."
        if row["expires_at"] and row["expires_at"] < time.time():
            return False, "This coupon has expired."
        if row["max_uses"] and row["used"] >= row["max_uses"]:
            return False, "This coupon has reached its limit."
        return True, ""

    # ----------------------------------------------------------------- trials
    def trial_used(self, user_id: int, store_id: int) -> bool:
        return bool(self._one("SELECT 1 FROM trials WHERE user_id = ? AND store_id = ?",
                              (user_id, store_id)))

    def mark_trial(self, user_id: int, store_id: int) -> None:
        self._run("INSERT OR REPLACE INTO trials(user_id, store_id, used_at) VALUES(?, ?, ?)",
                  (user_id, store_id, time.time()))

    # ---------------------------------------------------------------- tickets
    def open_ticket(self, user_id: int, username: str, message: str) -> int:
        cur = self._run(
            "INSERT INTO tickets(user_id, username, message, status, created_at) "
            "VALUES(?, ?, ?, 'open', ?)", (user_id, username, message, time.time()))
        return cur.lastrowid

    def ticket(self, ticket_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM tickets WHERE id = ?", (ticket_id,)))

    def tickets(self, status: str | None = "open", limit: int = 20,
                user_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM tickets WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if user_id:
            sql += " AND user_id = ?"
            params.append(int(user_id))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._q(sql, params)]

    def answer_ticket(self, ticket_id: int, admin_id: int, reply: str) -> None:
        self._run(
            "UPDATE tickets SET status = 'answered', answered_at = ?, answered_by = ?, reply = ? "
            "WHERE id = ?", (time.time(), admin_id, reply, ticket_id))

    def close_ticket(self, ticket_id: int) -> None:
        self._run("UPDATE tickets SET status = 'closed' WHERE id = ?", (ticket_id,))

    def open_ticket_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM tickets WHERE status = 'open'")["c"]

    def ticket_count_for(self, user_id: int) -> int:
        return self._one("SELECT COUNT(*) AS c FROM tickets WHERE user_id = ?", (user_id,))["c"]

    # --------------------------------------------------------------- requests
    def add_request(self, user_id: int, store_id: int | None, keyword: str) -> int:
        cur = self._run(
            "INSERT INTO requests(user_id, store_id, keyword, status, created_at) "
            "VALUES(?, ?, ?, 'open', ?)", (user_id, store_id, keyword, time.time()))
        return cur.lastrowid

    def requests(self, status: str | None = "open", limit: int = 20) -> list[dict]:
        if status:
            return [dict(r) for r in self._q(
                "SELECT r.*, s.name AS store_name FROM requests r LEFT JOIN stores s ON s.id = r.store_id "
                "WHERE r.status = ? ORDER BY r.created_at DESC LIMIT ?", (status, limit))]
        return [dict(r) for r in self._q(
            "SELECT r.*, s.name AS store_name FROM requests r LEFT JOIN stores s ON s.id = r.store_id "
            "ORDER BY r.created_at DESC LIMIT ?", (limit,))]

    def close_request(self, request_id: int) -> None:
        self._run("UPDATE requests SET status = 'done' WHERE id = ?", (request_id,))

    def open_request_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM requests WHERE status = 'open'")["c"]

    # -------------------------------------------------------------- favorites
    def toggle_favorite(self, user_id: int, file_id: int) -> bool:
        if self._one("SELECT 1 FROM favorites WHERE user_id = ? AND file_id = ?", (user_id, file_id)):
            self._run("DELETE FROM favorites WHERE user_id = ? AND file_id = ?", (user_id, file_id))
            return False
        self._run("INSERT INTO favorites(user_id, file_id, created_at) VALUES(?, ?, ?)",
                  (user_id, file_id, time.time()))
        return True

    def is_favorite(self, user_id: int, file_id: int) -> bool:
        return bool(self._one("SELECT 1 FROM favorites WHERE user_id = ? AND file_id = ?",
                              (user_id, file_id)))

    def favorites(self, user_id: int, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT f.*, s.name AS store_name FROM favorites fv "
            "JOIN files f ON f.id = fv.file_id LEFT JOIN stores s ON s.id = f.store_id "
            "WHERE fv.user_id = ? ORDER BY fv.created_at DESC LIMIT ?", (user_id, limit))]

    # ------------------------------------------------------------- searching
    def search_all_stores(self, keyword: str, limit: int = 20) -> list[dict]:
        """Cross-store search (inline mode, global search)."""
        like = f"%{keyword.lower()}%"
        return [dict(r) for r in self._q(
            "SELECT f.*, s.name AS store_name, s.is_premium, s.admin_id FROM files f "
            "JOIN stores s ON s.id = f.store_id WHERE LOWER(f.name) LIKE ? "
            "ORDER BY f.views DESC LIMIT ?", (like, limit))]

    def popular_files(self, store_id: int, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM files WHERE store_id = ? ORDER BY views DESC, id DESC LIMIT ?",
            (store_id, limit))]

    def newest_files(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT f.*, s.name AS store_name FROM files f JOIN stores s ON s.id = f.store_id "
            "ORDER BY f.created_at DESC LIMIT ?", (limit,))]

    # -------------------------------------------------- user preferences / i18n
    def set_user_lang(self, user_id: int, lang: str) -> None:
        self._run("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))

    def user_lang(self, user_id: int) -> str:
        row = self._one("SELECT lang FROM users WHERE user_id = ?", (user_id,))
        return (row["lang"] if row and row["lang"] else "") or "bn"

    def set_user_sort(self, user_id: int, mode: str) -> None:
        self._run("UPDATE users SET sort_pref = ? WHERE user_id = ?", (mode, user_id))

    def user_sort(self, user_id: int) -> str:
        row = self._one("SELECT sort_pref FROM users WHERE user_id = ?", (user_id,))
        return (row["sort_pref"] if row and row["sort_pref"] else "") or "default"

    # -------------------------------------------------------- store extras
    def set_store_meta(self, store_id: int, cover: str | None = None,
                       description: str | None = None) -> None:
        if cover is not None:
            self.update_store(store_id, cover=cover)
        if description is not None:
            self.update_store(store_id, description=description)

    def reset_store_sales(self, store_id: int) -> None:
        self._run("DELETE FROM orders WHERE store_id = ? AND status != 'approved'", (store_id,))

    # ------------------------------------------------------------------ bans
    def ban_user(self, user_id: int, reason: str = "", banned_by: int = 0) -> None:
        self._run(
            "INSERT INTO banned(user_id, reason, banned_at, banned_by) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason, banned_at = excluded.banned_at, banned_by = excluded.banned_by",
            (user_id, reason, time.time(), banned_by),
        )
        self.log_event("user_banned", user_id, None, None, reason or "admin",
                       f"by {banned_by}")
        # Also revoke all grants
        self._run("DELETE FROM grants WHERE user_id = ?", (user_id,))

    def unban_user(self, user_id: int) -> None:
        self._run("DELETE FROM banned WHERE user_id = ?", (user_id,))
        self.log_event("user_unbanned", user_id, None, None, "admin")

    def is_banned(self, user_id: int) -> bool:
        return bool(self._one("SELECT 1 FROM banned WHERE user_id = ?", (user_id,)))

    def banned_users(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM banned ORDER BY banned_at DESC")]

    # -------------------------------------------------------------- auto sync
    def set_auto_sync(self, store_id: int, chat_id: int, title: str, interval_h: int = 24, enabled: bool = True) -> None:
        self._run(
            "INSERT INTO auto_sync(store_id, chat_id, title, last_scan, interval_h, enabled) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(store_id) DO UPDATE SET chat_id = excluded.chat_id, title = excluded.title, interval_h = excluded.interval_h, enabled = excluded.enabled",
            (store_id, chat_id, title, time.time(), interval_h, 1 if enabled else 0),
        )

    def auto_sync_cfg(self, store_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM auto_sync WHERE store_id = ?", (store_id,)))

    def all_auto_sync(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM auto_sync WHERE enabled = 1")]

    def disable_auto_sync(self, store_id: int) -> None:
        self._run("UPDATE auto_sync SET enabled = 0 WHERE store_id = ?", (store_id,))

    # ================================================================= v3: events
    # Every interesting user action lands here, so the admin panel can answer
    # “who clicked what, who watched which video, how many converted”.
    def log_event(self, name: str, user_id: int | None = None, store_id: int | None = None,
                  file_id: int | None = None, ref: str | None = None,
                  detail: str | None = None) -> None:
        try:
            self._run(
                "INSERT INTO events(ts, user_id, name, store_id, file_id, ref, detail) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (time.time(), user_id, name, store_id, file_id, ref, detail),
            )
        except sqlite3.Error:
            pass

    def events(self, limit: int = 200, user_id: int | None = None,
               name: str | None = None, store_id: int | None = None,
               file_id: int | None = None, since: float | None = None) -> list[dict]:
        sql = ("SELECT e.*, u.name AS user_name, u.username AS user_username, "
               "f.name AS file_name, s.name AS store_name "
               "FROM events e LEFT JOIN users u ON u.user_id = e.user_id "
               "LEFT JOIN files f ON f.id = e.file_id "
               "LEFT JOIN stores s ON s.id = e.store_id WHERE 1=1")
        params: list[Any] = []
        if user_id:
            sql += " AND e.user_id = ?"; params.append(int(user_id))
        if name:
            sql += " AND e.name = ?"; params.append(name)
        if store_id:
            sql += " AND e.store_id = ?"; params.append(int(store_id))
        if file_id:
            sql += " AND e.file_id = ?"; params.append(int(file_id))
        if since:
            sql += " AND e.ts >= ?"; params.append(float(since))
        sql += " ORDER BY e.ts DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._q(sql, params)]

    def event_counts(self, since: float | None = None) -> dict:
        sql = "SELECT name, COUNT(*) AS c FROM events"
        params: list[Any] = []
        if since:
            sql += " WHERE ts >= ?"; params.append(float(since))
        sql += " GROUP BY name"
        return {r["name"]: r["c"] for r in self._q(sql, params)}

    def events_by_day(self, days: int = 14) -> list[dict]:
        since = time.time() - days * 86400
        rows = self._q(
            "SELECT date(ts, 'unixepoch', '+6 hours') AS day, "
            "SUM(CASE WHEN name = 'start' THEN 1 ELSE 0 END) AS joined, "
            "SUM(CASE WHEN name = 'deliver' THEN 1 ELSE 0 END) AS delivered, "
            "SUM(CASE WHEN name = 'paid' THEN 1 ELSE 0 END) AS paid, "
            "COUNT(DISTINCT user_id) AS users "
            "FROM events WHERE ts >= ? GROUP BY day ORDER BY day", (since,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------- per-user analytics
    def user_analytics(self, user_id: int, limit: int = 120) -> dict:
        user = self.user(user_id) or {}
        out = {
            "user": user,
            "counts": {},
            "stores": [],
            "videos": [],
            "events": self.events(limit=limit, user_id=user_id),
            "searches": [dict(r) for r in self._q(
                "SELECT ref, COUNT(*) AS c FROM events WHERE user_id = ? AND name = 'search' "
                "GROUP BY ref ORDER BY c DESC LIMIT 10", (user_id,))],
        }
        for row in self._q("SELECT name, COUNT(*) AS c FROM events WHERE user_id = ? GROUP BY name",
                           (user_id,)):
            out["counts"][row["name"]] = row["c"]
        out["stores"] = [dict(r) for r in self._q(
            "SELECT e.store_id AS id, COALESCE(s.name, '?') AS name, COUNT(*) AS hits, "
            "COUNT(DISTINCT e.name) AS kinds, MAX(e.ts) AS last_ts "
            "FROM events e LEFT JOIN stores s ON s.id = e.store_id "
            "WHERE e.user_id = ? AND e.store_id IS NOT NULL GROUP BY e.store_id, s.name ORDER BY hits DESC",
            (user_id,))]
        out["videos"] = [dict(r) for r in self._q(
            "SELECT e.file_id AS id, COALESCE(f.name, 'deleted') AS name, COUNT(*) AS hits, "
            "MAX(e.ts) AS last_ts FROM events e LEFT JOIN files f ON f.id = e.file_id "
            "WHERE e.user_id = ? AND e.file_id IS NOT NULL AND e.name IN ('deliver','view_file') "
            "GROUP BY e.file_id, f.name ORDER BY hits DESC LIMIT 40", (user_id,))]
        orders = self.orders_for(user_id) if hasattr(self, "orders_for") else []
        out["orders"] = [dict(o) for o in orders]
        grants = self.grants_for(user_id) if hasattr(self, "grants_for") else []
        out["grants"] = [dict(g) for g in grants]
        return out

    # ------------------------------------------------------ per-store analytics
    def store_analytics(self, store_id: int, limit: int = 300) -> dict:
        store = self.store(store_id) or {}
        out = {"store": store, "counts": {}, "files": [], "funnel": {}, "events": []}
        for row in self._q("SELECT name, COUNT(*) AS c FROM events WHERE store_id = ? GROUP BY name",
                           (store_id,)):
            out["counts"][row["name"]] = row["c"]
        out["files"] = [dict(r) for r in self._q(
            "SELECT f.id, f.name, f.kind, f.views, "
            "(SELECT COUNT(DISTINCT e.user_id) FROM events e WHERE e.file_id = f.id AND e.name = 'deliver') AS uniq, "
            "(SELECT COUNT(*) FROM events e WHERE e.file_id = f.id AND e.name = 'deliver') AS sends, "
            "(SELECT MAX(e.ts) FROM events e WHERE e.file_id = f.id AND e.name = 'deliver') AS last_ts "
            "FROM files f WHERE f.store_id = ? ORDER BY f.id DESC", (store_id,))]
        out["events"] = self.events(limit=limit, store_id=store_id)
        out["watchers"] = [dict(r) for r in self._q(
            "SELECT e.user_id, COALESCE(u.name, '?') AS name, u.username AS username, "
            "COUNT(DISTINCT e.file_id) AS videos, COUNT(*) AS sends, MAX(e.ts) AS last_ts "
            "FROM events e LEFT JOIN users u ON u.user_id = e.user_id "
            "WHERE e.store_id = ? AND e.name = 'deliver' AND e.user_id IS NOT NULL "
            "GROUP BY e.user_id, u.name, u.username ORDER BY videos DESC, last_ts DESC LIMIT ?", (store_id, limit))]
        out["funnel"] = self.store_funnel(store_id)
        return out

    def store_funnel(self, store_id: int) -> dict:
        """How many came, how many watched, how many paid — the convert funnel."""
        def count(sql: str) -> int:
            row = self._one(sql, (store_id,))
            return int(row[0] or 0) if row else 0

        return {
            "came": count("SELECT COUNT(DISTINCT user_id) FROM events "
                          "WHERE store_id = ? AND user_id IS NOT NULL"),
            "watched": count("SELECT COUNT(DISTINCT user_id) FROM events "
                             "WHERE store_id = ? AND name = 'deliver' AND user_id IS NOT NULL"),
            "joined": count("SELECT COUNT(DISTINCT user_id) FROM events WHERE store_id = ? "
                            "AND name IN ('join_click','join_block') AND user_id IS NOT NULL"),
            "paid": count("SELECT COUNT(DISTINCT user_id) FROM events "
                          "WHERE store_id = ? AND name = 'paid' AND user_id IS NOT NULL"),
        }

    def file_watchers(self, file_id: int, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT e.user_id, COALESCE(u.name, '?') AS name, u.username AS username, "
            "COUNT(*) AS hits, MAX(e.ts) AS last_ts FROM events e "
            "LEFT JOIN users u ON u.user_id = e.user_id "
            "WHERE e.file_id = ? AND e.name = 'deliver' AND e.user_id IS NOT NULL "
            "GROUP BY e.user_id, u.name, u.username ORDER BY last_ts DESC LIMIT ?", (file_id, limit))]

    def unique_viewers(self, file_id: int) -> int:
        row = self._one("SELECT COUNT(DISTINCT user_id) AS c FROM events "
                        "WHERE file_id = ? AND name = 'deliver' AND user_id IS NOT NULL", (file_id,))
        return int(row["c"]) if row else 0

    # ------------------------------------------------------ per-store force join
    def store_forcejoin(self, store_id: int) -> str:
        row = self._one("SELECT forcejoin FROM stores WHERE id = ?", (store_id,))
        return (row["forcejoin"] or "") if row else ""

    def set_store_forcejoin(self, store_id: int, value: str) -> None:
        self._run("UPDATE stores SET forcejoin = ? WHERE id = ?", ((value or "").strip(), store_id))

    # -------------------------------------------------------- join channel list
    def add_join_channel(self, store_id: int | None, ref: str, title: str = "",
                         chat_id: int | None = None, kind: str = "",
                         invite: str = "") -> int:
        cur = self._run(
            "INSERT INTO join_channels(store_id, ref, title, chat_id, kind, invite, added_at, last_check, enabled) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 0, 1)",
            (store_id, ref, title, chat_id, kind, invite, time.time()))
        return cur.lastrowid

    def join_channels(self, store_id: int | None = None, enabled_only: bool = False,
                      global_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM join_channels WHERE 1=1"
        params: list[Any] = []
        if store_id is not None:
            sql += " AND store_id = ?"; params.append(store_id)
        if global_only:
            sql += " AND store_id IS NULL"
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY id"
        return [dict(r) for r in self._q(sql, params)]

    def join_channel(self, channel_id: int) -> dict | None:
        row = self._one("SELECT * FROM join_channels WHERE id = ?", (channel_id,))
        return dict(row) if row else None

    def update_join_channel(self, channel_id: int, **fields: Any) -> None:
        allowed = {"ref", "title", "chat_id", "kind", "invite", "enabled", "last_check", "error"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?"); params.append(value)
        if not sets:
            return
        params.append(channel_id)
        self._run(f"UPDATE join_channels SET {', '.join(sets)} WHERE id = ?", params)

    def delete_join_channel(self, channel_id: int) -> None:
        self._run("DELETE FROM join_channels WHERE id = ?", (channel_id,))

    def remove_join_channels(self, store_id: int | None) -> None:
        if store_id is None:
            self._run("DELETE FROM join_channels WHERE store_id IS NULL")
        else:
            self._run("DELETE FROM join_channels WHERE store_id = ?", (store_id,))

    # --------------------------------------------------- join prompts (gate txt)
    def join_prompt_set(self, user_id: int, file_id: int, token: str = "") -> None:
        self._run(
            "INSERT INTO join_prompts(user_id, file_id, token, attempts, created) VALUES(?, ?, ?, 1, ?) "
            "ON CONFLICT(user_id, file_id) DO UPDATE SET attempts = attempts + 1, token = excluded.token",
            (user_id, file_id, token, time.time()))

    def join_prompt(self, user_id: int, file_id: int) -> dict | None:
        row = self._one("SELECT * FROM join_prompts WHERE user_id = ? AND file_id = ?",
                        (user_id, file_id))
        return dict(row) if row else None

    def join_prompts(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT p.*, COALESCE(f.name, 'deleted') AS file_name, COALESCE(u.name, '?') AS user_name "
            "FROM join_prompts p LEFT JOIN files f ON f.id = p.file_id "
            "LEFT JOIN users u ON u.user_id = p.user_id ORDER BY p.created DESC LIMIT ?", (limit,))]

    def clear_join_prompt(self, user_id: int, file_id: int) -> None:
        self._run("DELETE FROM join_prompts WHERE user_id = ? AND file_id = ?", (user_id, file_id))

    # ---------------------------------------------------------- file helpers v3
    def set_file_bytes(self, file_id: int, size: int | None) -> None:
        if size:
            self._run("UPDATE files SET bytes = ? WHERE id = ?", (int(size), file_id))

    def count_files(self) -> int:
        return int(self._one("SELECT COUNT(*) AS c FROM files")["c"])

    def count_mirrors(self) -> int:
        return int(self._one("SELECT COUNT(*) AS c FROM files WHERE mirror_msg IS NOT NULL")["c"])

    def set_mirror(self, file_id: int, chat_id: int, msg_id: int) -> None:
        """Remember the bot-side copy of a file so **every** later delivery is a
        plain send — no repeated forward/delete churn (that was the FloodWait)."""
        self._run("UPDATE files SET mirror_chat = ?, mirror_msg = ?, mirror_ts = ? WHERE id = ?",
                  (chat_id, msg_id, time.time(), file_id))

    def clear_mirror(self, file_id: int) -> None:
        self._run("UPDATE files SET mirror_chat = NULL, mirror_msg = NULL WHERE id = ?", (file_id,))

    def mirrored_files(self, limit: int = 500) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM files WHERE mirror_msg IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,))]

    def recent_public_videos(self, limit: int = 3) -> list[dict]:
        """Newest videos of FREE stores that already have a bot-side copy (website feed)."""
        return [dict(r) for r in self._q(
            "SELECT f.* FROM files f JOIN stores s ON s.id = f.store_id "
            "WHERE f.kind = 'Video' AND s.is_premium = 0 "
            "AND f.mirror_msg IS NOT NULL AND f.mirror_msg != 0 "
            "ORDER BY f.id DESC LIMIT ?", (limit,))]

    def recent_public_media(self, limit: int = 8) -> list[dict]:
        """Video *and* photos of free stores, with or without a mirror copy.

        The public reel used to require `mirror_msg` (a bot-side copy created by the
        media cache), so a fresh install showed an empty page even though the store
        was full of videos. The feed service now resolves the source itself, so the
        query only needs files the bot is allowed to stream.
        """
        return [dict(r) for r in self._q(
            "SELECT f.*, s.name AS store_name, s.slug AS store_slug,"
            " s.is_premium AS store_premium FROM files f JOIN stores s ON s.id = f.store_id "
            "WHERE s.is_premium = 0 AND f.kind IN ('Video', 'Photo') "
            "AND (f.locked IS NULL OR f.locked = 0) "
            "AND f.chat_id IS NOT NULL AND f.msg_id IS NOT NULL "
            "ORDER BY f.id DESC LIMIT ?", (int(limit),))]

    def public_gallery(self, limit: int = 60, kind: str = "") -> list[dict]:
        """Everything a visitor may browse (free stores only) — used by /watch."""
        sql = ("SELECT f.*, s.name AS store_name, s.slug AS store_slug"
               " FROM files f JOIN stores s ON s.id = f.store_id"
               " WHERE s.is_premium = 0 AND f.kind IN ('Video', 'Photo', 'Audio')"
               " AND (f.locked IS NULL OR f.locked = 0)")
        params: list[Any] = []
        if kind:
            sql += " AND f.kind = ?"
            params.append(kind)
        sql += " ORDER BY f.id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._q(sql, params)]

    def missing_mirrors(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM files WHERE (mirror_msg IS NULL OR mirror_msg = 0) ORDER BY id LIMIT ?",
            (limit,))]

    def set_file_uid(self, file_id: int, uid: str) -> None:
        self._run("UPDATE files SET uid = ? WHERE id = ?", (uid, file_id))

    def file_by_uid(self, uid: str) -> dict | None:
        row = self._one("SELECT * FROM files WHERE uid = ?", (uid,))
        return dict(row) if row else None

    def file_by_code(self, code: str) -> dict | None:
        row = self._one("SELECT * FROM files WHERE code = ?", (code,))
        return dict(row) if row else None

    def backfill_uids(self) -> int:
        n = 0
        for row in self._q("SELECT id FROM files WHERE uid IS NULL OR uid = ''"):
            self.set_file_uid(row["id"], f"{row['id']}{int(time.time()) % 100000:05d}{row['id'] % 97:02d}")
            n += 1
        return n

    def set_file_locked(self, file_id: int, locked: int) -> None:
        self._run("UPDATE files SET locked = ? WHERE id = ?", (1 if locked else 0, file_id))

    def duplicate_files(self) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT store_id, chat_id, msg_id, COUNT(*) AS c, GROUP_CONCAT(id) AS ids, "
            "MIN(name) AS name FROM files GROUP BY store_id, chat_id, msg_id HAVING c > 1 "
            "ORDER BY c DESC")]

    def purge_duplicate_files(self) -> int:
        removed = 0
        for row in self.duplicate_files():
            ids = [int(x) for x in str(row["ids"]).split(",")][1:]
            for fid in ids:
                self._run("DELETE FROM link_files WHERE file_id = ?", (fid,))
                self._run("DELETE FROM files WHERE id = ?", (fid,))
                removed += 1
        return removed

    def store_labels(self) -> dict:
        return {r["id"]: r["name"] for r in self._q("SELECT id, name FROM stores")}

    def attach_meta_chunk(self, rows: list[tuple[str, str]]) -> None:
        for key, value in rows:
            self.set_meta(key, value)

    # -------------------------------------------------------------- welcome
    def set_welcome(self, admin_id: int, text: str) -> None:
        self._run(
            "INSERT INTO welcome_cfg(admin_id, text) VALUES(?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET text = excluded.text",
            (admin_id, text),
        )

    def welcome_text(self, admin_id: int) -> str:
        row = self._one("SELECT text FROM welcome_cfg WHERE admin_id = ?", (admin_id,))
        return row["text"] if row else ""

    # =================================================== broadcaststudio (v2.3)
    # A campaign is a saved broadcast + its per-user queue. The queue lives in
    # SQLite, so a restart/hosting sleep never loses progress: the worker picks
    # up exactly where it stopped.
    CAMPAIGN_FIELDS = {"title", "text", "files", "audience", "status", "total",
                       "sent", "failed", "blocked", "scheduled_at", "started_at",
                       "finished_at", "last_error", "silent", "protect", "origin"}

    def create_campaign(self, admin_id: int, *, title: str = "", text: str = "",
                        files: list[int] | None = None, audience: str = "all",
                        status: str = "draft", scheduled_at: float | None = None,
                        buttons: str = "", slug: str = "",
                        media_caption: str = "above", online_button: int | None = None,
                        silent: int = 0, protect: int = 1, origin: str = "panel",
                        media: list[dict] | None = None,
                        pause_seconds: float = 0.0) -> int:
        cur = self._run(
            "INSERT INTO campaigns(admin_id, title, text, files, audience, status,"
            " total, scheduled_at, created_at, buttons, slug, media_caption, online_button,"
            " silent, protect, origin, pause_seconds)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (admin_id, title or "", text or "", json.dumps(files or []),
             audience, status, 0, scheduled_at, time.time(),
             buttons or "", slug or "", media_caption or "above",
             None if online_button is None else (1 if online_button else 0),
             1 if silent else 0, 1 if protect else 0, origin or "panel",
             float(pause_seconds or 0.0)),
        )
        campaign_id = int(cur.lastrowid)
        if media:
            self.add_campaign_media(campaign_id, media)
        return campaign_id

    # ------------------------------------------------- uploaded broadcast media
    def add_campaign_media(self, campaign_id: int, items: list[dict]) -> int:
        """Remember `(chat_id, msg_id)` pairs that go out with this campaign."""
        added = 0
        for idx, item in enumerate(items or []):
            chat_id = item.get("chat_id") or item.get("chat") or 0
            msg_id = item.get("msg_id") or item.get("msg") or 0
            if not chat_id or not msg_id:
                continue
            item_id = int(item.get("item_id") or 0)
            if not item_id:
                try:
                    item_id = self.bc_media_id_for(chat_id, msg_id)
                except Exception:
                    item_id = 0
            self._run(
                "INSERT OR REPLACE INTO campaign_media"
                "(campaign_id, idx, chat_id, msg_id, kind, name, item_id) VALUES(?,?,?,?,?,?,?)",
                (int(campaign_id), int(item.get("idx", idx)), int(chat_id), int(msg_id),
                 (item.get("kind") or "video")[:24], (item.get("name") or "")[:200],
                 item_id))
            added += 1
        return added

    def campaign_media(self, campaign_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM campaign_media WHERE campaign_id = ? ORDER BY idx",
            (int(campaign_id),))]

    # ------------------------------------------------- multi-item draft groups
    # Ported from the standalone `broadcast.py` archive: a draft is a *list* of
    # messages („➕ Added #3“). Storing the list makes `/send` reproduce the draft
    # exactly — several media in one go instead of only one file per campaign.
    def set_campaign_groups(self, campaign_id: int, items: list[dict]) -> int:
        """Replace the draft list of a campaign (ordered, 1-based idx)."""
        self._run("DELETE FROM campaign_groups WHERE campaign_id = ?", (int(campaign_id),))
        now = time.time()
        added = 0
        for idx, item in enumerate(items or []):
            if not item:
                continue
            kind = str(item.get("kind") or "text")[:16]
            text = item.get("text") or ""
            file_id = item.get("file_id") or ""
            media_key = str(item.get("media_key") or "")
            # A media item may carry no caption at all — the pointer is the content,
            # so “no text and no file id” is not an empty item when media_key is set.
            if not text and not file_id and not media_key:
                continue
            self._run(
                "INSERT OR REPLACE INTO campaign_groups"
                "(campaign_id, idx, kind, file_id, text, media_key, parse_mode, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (int(campaign_id), int(item.get("idx", idx)), kind, str(file_id),
                 text, media_key, str(item.get("parse_mode") or ""), now),
            )
            added += 1
        return added

    def campaign_groups(self, campaign_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM campaign_groups WHERE campaign_id = ? ORDER BY idx",
            (int(campaign_id),))]

    def count_campaign_groups(self, campaign_id: int) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM campaign_groups WHERE campaign_id = ?",
                        (int(campaign_id),))
        return int(row["n"] if row else 0)

    # ------------------------------------------------ rich draft links (buttons)
    def add_campaign_link(self, campaign_id: int, token: str, *, label: str = "",
                          url: str = "", kind: str = "button") -> int:
        cur = self._run(
            "INSERT OR REPLACE INTO campaign_links(campaign_id, token, label, url, kind,"
            " clicks, created_at) VALUES(?,?,?,?,?, COALESCE((SELECT clicks FROM"
            " campaign_links WHERE token = ?), 0), ?)",
            (int(campaign_id), token, label or "", url or "", kind or "button", token,
             time.time()),
        )
        return int(cur.lastrowid or 0)

    def campaign_links(self, campaign_id: int, kind: str | None = "button") -> list[dict]:
        sql = "SELECT * FROM campaign_links WHERE campaign_id = ?"
        params: list[Any] = [int(campaign_id)]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id"
        return [dict(r) for r in self._q(sql, params)]

    def campaign_by_link(self, token: str) -> dict | None:
        row = self._one("SELECT * FROM campaign_links WHERE token = ?", (str(token),))
        if row is None:
            return None
        return self.campaign(int(row["campaign_id"]))

    def hit_campaign_link(self, token: str) -> int:
        self._run("UPDATE campaign_links SET clicks = clicks + 1 WHERE token = ?", (str(token),))
        row = self._one("SELECT clicks FROM campaign_links WHERE token = ?", (str(token),))
        return int(row["clicks"] if row else 0)

    def clear_campaign_media(self, campaign_id: int) -> None:
        self._run("DELETE FROM campaign_media WHERE campaign_id = ?", (int(campaign_id),))

    # ------------------------------------------------------------ `/br` drafts
    def save_br_draft(self, admin_id: int, data: str) -> None:
        self._run("INSERT INTO br_drafts(admin_id, data, updated_at) VALUES(?,?,?)"
                  " ON CONFLICT(admin_id) DO UPDATE SET data = excluded.data,"
                  " updated_at = excluded.updated_at",
                  (int(admin_id), data, time.time()))

    def br_draft(self, admin_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM br_drafts WHERE admin_id = ?",
                                    (int(admin_id),)))

    def clear_br_draft(self, admin_id: int) -> None:
        self._run("DELETE FROM br_drafts WHERE admin_id = ?", (int(admin_id),))

    # --------------------------------------------------- broadcast media inbox
    def add_bc_media(self, chat_id: int, msg_id: int, kind: str = "video",
                     name: str = "", size: int = 0) -> int:
        existing = self._one(
            "SELECT id FROM bc_media WHERE chat_id = ? AND msg_id = ?",
            (int(chat_id), int(msg_id)))
        if existing:
            return int(existing["id"])
        cur = self._run(
            "INSERT INTO bc_media(chat_id, msg_id, kind, name, size, added_at)"
            " VALUES(?,?,?,?,?,?)",
            (int(chat_id), int(msg_id), (kind or "video")[:24], (name or "")[:200],
             int(size or 0), time.time()))
        return int(cur.lastrowid)

    def bc_media_items(self, limit: int = 40) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM bc_media ORDER BY id DESC LIMIT ?", (int(limit),))]

    def bc_media_item(self, item_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM bc_media WHERE id = ?", (int(item_id),)))

    def bc_media_id_for(self, chat_id: int, msg_id: int) -> int:
        """Inbox id of one (chat, message) pair — 0 when it is not in the inbox.

        Used to link a campaign's attached media back to `/inbox/<id>`; the old
        “scan the first 60 rows” trick silently produced `/inbox/0` links for
        anything added earlier.
        """
        row = self._one("SELECT id FROM bc_media WHERE chat_id = ? AND msg_id = ?"
                        " ORDER BY id LIMIT 1", (int(chat_id), int(msg_id)))
        return int(row["id"]) if row else 0

    def delete_bc_media(self, item_id: int) -> None:
        self._run("DELETE FROM bc_media WHERE id = ?", (int(item_id),))

    def touch_bc_media(self, item_id: int) -> None:
        self._run("UPDATE bc_media SET used = used + 1 WHERE id = ?", (int(item_id),))

    # ------------------------------------------------- public reel (home page)
    def add_feed_pin(self, chat_id: int, msg_id: int, kind: str = "video",
                     title: str = "") -> int:
        row = self._one("SELECT COALESCE(MAX(position), 0) AS p FROM feed_pins")
        cur = self._run(
            "INSERT INTO feed_pins(chat_id, msg_id, kind, title, position, added_at)"
            " VALUES(?,?,?,?,?,?)",
            (int(chat_id), int(msg_id), (kind or "video")[:24], (title or "")[:200],
             int(row["p"] or 0) + 1, time.time()))
        return int(cur.lastrowid)

    def feed_pins(self, limit: int = 12) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM feed_pins ORDER BY position, id DESC LIMIT ?", (int(limit),))]

    def delete_feed_pin(self, pin_id: int) -> None:
        self._run("DELETE FROM feed_pins WHERE id = ?", (int(pin_id),))

    def move_feed_pin(self, pin_id: int, delta: int) -> None:
        row = self._one("SELECT position FROM feed_pins WHERE id = ?", (int(pin_id),))
        if row is None:
            return
        self._run("UPDATE feed_pins SET position = ? WHERE id = ?",
                  (max(0, int(row["position"] or 0) + int(delta)), int(pin_id)))

    def bump_feed_play(self, item: str) -> None:
        self._run("INSERT INTO feed_plays(item, plays, last_ts) VALUES(?, 1, ?)"
                  " ON CONFLICT(item) DO UPDATE SET plays = plays + 1,"
                  " last_ts = excluded.last_ts", (item[:80], time.time()))

    def feed_play_counts(self) -> dict[str, int]:
        return {r["item"]: int(r["plays"] or 0) for r in
                self._q("SELECT item, plays FROM feed_plays")}

    def campaign(self, campaign_id: int) -> dict | None:
        row = self._dict(self._one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)))
        if row is None:
            return None
        row["file_ids"] = _json_int_list(row.get("files"))
        return row

    def campaign_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS c FROM campaigns")
        return int(row["c"] or 0)

    def campaigns(self, limit: int = 50, status: str | None = None,
                  admin_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM campaigns"
        params: list[Any] = []
        where = []
        if status:
            where.append("status = ?")
            params.append(status)
        if admin_id is not None:
            where.append("admin_id = ?")
            params.append(admin_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = []
        for row in self._q(sql, params):
            item = dict(row)
            item["file_ids"] = _json_int_list(item.get("files"))
            rows.append(item)
        return rows

    def update_campaign(self, campaign_id: int, **fields: Any) -> None:
        sets, params = [], []
        for key, value in fields.items():
            if key not in self.CAMPAIGN_FIELDS:
                continue
            if key == "files" and isinstance(value, (list, tuple)):
                value = json.dumps(list(value))
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        params.append(campaign_id)
        self._run(f"UPDATE campaigns SET {', '.join(sets)} WHERE id = ?", params)

    def delete_campaign(self, campaign_id: int) -> None:
        self._run("DELETE FROM campaign_items WHERE campaign_id = ?", (campaign_id,))
        self._run("DELETE FROM campaigns WHERE id = ?", (campaign_id,))

    def add_campaign_items(self, campaign_id: int, user_ids: Iterable[int]) -> int:
        """Fill the queue. Existing rows are kept (resume/retry safe)."""
        now = time.time()
        before = self._one("SELECT COUNT(*) AS n FROM campaign_items WHERE campaign_id = ?",
                           (campaign_id,))["n"]
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO campaign_items(campaign_id, user_id, status, updated_at)"
                " VALUES(?,?, 'pending', ?)",
                [(campaign_id, int(uid), now) for uid in user_ids],
            )
        after = self._one("SELECT COUNT(*) AS n FROM campaign_items WHERE campaign_id = ?",
                          (campaign_id,))["n"]
        self.update_campaign(campaign_id, total=after)
        return after - before

    def campaign_items(self, campaign_id: int, status: str | None = None,
                       limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM campaign_items WHERE campaign_id = ?"
        params: list[Any] = [campaign_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY user_id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in self._q(sql, params)]

    def campaign_user_ids(self, campaign_id: int, status: str | None = "pending",
                          limit: int | None = None) -> list[int]:
        sql = "SELECT user_id FROM campaign_items WHERE campaign_id = ?"
        params: list[Any] = [campaign_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY user_id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [int(r["user_id"]) for r in self._q(sql, params)]

    def mark_campaign_item(self, campaign_id: int, user_id: int, status: str,
                           error: str = "") -> None:
        self._run(
            "UPDATE campaign_items SET status = ?, error = ?,"
            " attempts = attempts + 1, updated_at = ?"
            " WHERE campaign_id = ? AND user_id = ?",
            (status, (error or "")[:200], time.time(), campaign_id, user_id),
        )

    def rearm_campaign_items(self, campaign_id: int, statuses: tuple[str, ...] = ("failed",)) -> int:
        """Put failed/blocked items back in the queue (used by “retry”)."""
        marks = ",".join("?" for _ in statuses)
        cur = self._run(
            f"UPDATE campaign_items SET status = 'pending', updated_at = ?"
            f" WHERE campaign_id = ? AND status IN ({marks})",
            (time.time(), campaign_id, *statuses),
        )
        return int(cur.rowcount or 0)

    def campaign_counts(self, campaign_id: int, sync: bool = True) -> dict:
        rows = self._q(
            "SELECT status, COUNT(*) AS n FROM campaign_items WHERE campaign_id = ?"
            " GROUP BY status", (campaign_id,),
        )
        counts = {row["status"]: row["n"] for row in rows}
        counts.setdefault("pending", 0)
        counts.setdefault("sent", 0)
        counts.setdefault("failed", 0)
        counts.setdefault("blocked", 0)
        counts["total"] = sum(counts[k] for k in ("pending", "sent", "failed", "blocked"))
        counts["done"] = counts["sent"] + counts["failed"] + counts["blocked"]
        if sync:
            self.update_campaign(campaign_id, sent=counts["sent"], failed=counts["failed"],
                                 blocked=counts["blocked"], total=counts["total"])
        return counts

    def due_campaigns(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        rows = self._q("SELECT * FROM campaigns WHERE status = 'scheduled' AND"
                       " (scheduled_at IS NULL OR scheduled_at <= ?) ORDER BY id", (now,))
        return [self._with_files(dict(r)) for r in rows]

    def queued_campaigns(self) -> list[dict]:
        return [self._with_files(dict(r)) for r in
                self._q("SELECT * FROM campaigns WHERE status IN ('queued','running')"
                        " ORDER BY id")]

    @staticmethod
    def _with_files(row: dict) -> dict:
        row["file_ids"] = _json_int_list(row.get("files"))
        return row

    # ------------------------------------------------------------------- users
    def migrate_legacy(self, legacy_path: str) -> dict | None:
        """Import the old JSON database. Safe to call repeatedly."""
        path = Path(legacy_path)
        if not path.exists():
            return None
        if self.get_meta("legacy_imported") == "yes":
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("Legacy import failed to read %s: %s", path, exc)
            return None

        counters = {"users": 0, "stores": 0, "files": 0, "grants": 0, "links": 0, "sessions": 0}
        slug_to_store: dict[str, int] = {}
        name_to_store: dict[tuple[int, str], int] = {}
        old_fid_map: dict[str, int] = {}

        # users
        for raw_uid, info in (data.get("user_details") or {}).items():
            uid = int(raw_uid)
            if not self.user(uid):
                self.touch_user(uid, (info or {}).get("name"), None)
                joined = (info or {}).get("joined")
                if joined:
                    self._run("UPDATE users SET joined_at = ? WHERE user_id = ?", (joined, uid))
                counters["users"] += 1

        # stores + files
        for raw_admin, store_map in (data.get("stores") or {}).items():
            admin_id = int(raw_admin)
            for store_name, files in (store_map or {}).items():
                store = self.create_store(admin_id, store_name)
                name_to_store[(admin_id, store_name)] = store["id"]
                counters["stores"] += 1
                for old_fid, meta in (files or {}).items():
                    meta = meta or {}
                    new_id = self.add_file(
                        store["id"],
                        meta.get("name") or old_fid,
                        meta.get("type") or "File",
                        int(meta.get("chat_id") or 0),
                        int(meta.get("msg_id") or 0),
                        code=old_fid,
                        size=meta.get("size"),
                    )
                    if new_id:
                        old_fid_map[old_fid] = new_id
                        if meta.get("views"):
                            self._run("UPDATE files SET views = ? WHERE id = ?",
                                      (int(meta["views"]), new_id))
                        counters["files"] += 1

        # slugs
        for slug, pair in (data.get("store_slugs") or {}).items():
            try:
                admin_id, store_name = int(pair[0]), pair[1]
            except (TypeError, ValueError, IndexError):
                continue
            store_id = name_to_store.get((admin_id, store_name))
            if store_id and not self._one("SELECT 1 FROM stores WHERE slug = ?", (slug,)):
                self.update_store(store_id, slug=slug)
                slug_to_store[slug] = store_id

        # premium grants
        for raw_admin, store_map in (data.get("premium_access") or {}).items():
            for store_name, users in (store_map or {}).items():
                store_id = name_to_store.get((int(raw_admin), store_name))
                if not store_id:
                    continue
                for raw_uid, exp in (users or {}).items():
                    self.grant(store_id, int(raw_uid), exp, source="legacy")
                    counters["grants"] += 1

        # temp links
        for token, info in (data.get("temp_links") or {}).items():
            info = info or {}
            fids = [old_fid_map[f] for f in (info.get("fids") or []) if f in old_fid_map]
            if not fids:
                continue
            self._run(
                "INSERT OR REPLACE INTO links(token, admin_id, kind, created_at, expires_at) "
                "VALUES(?, ?, 'legacy', ?, ?)",
                (token, int(info.get("admin_uid") or 0), time.time(), info.get("expires_at")),
            )
            for fid in fids:
                self._run("INSERT OR IGNORE INTO link_files(token, file_id) VALUES(?, ?)", (token, fid))
            counters["links"] += 1

        # sessions
        for raw_admin, session_str in (data.get("admin_sessions") or {}).items():
            if session_str:
                self.save_session(int(raw_admin), session_str)
                counters["sessions"] += 1

        # bot admin chats
        for chat_id in (data.get("bot_admin_chats") or []):
            self.add_bot_chat(int(chat_id))

        # referrals
        for raw_uid, raw_ref in (data.get("referred_by") or {}).items():
            self._run(
                "INSERT OR IGNORE INTO referrals(user_id, referrer_id, rewarded, created_at) "
                "VALUES(?, ?, ?, ?)",
                (int(raw_uid), int(raw_ref),
                 1 if int(raw_uid) in {int(x) for x in (data.get("referral_rewarded") or [])} else 0,
                 time.time()),
            )
        for raw_admin, conf in (data.get("referral_config") or {}).items():
            store_id = name_to_store.get((int(raw_admin), (conf or {}).get("store")))
            if store_id:
                self.set_referral_cfg(int(raw_admin), store_id, int((conf or {}).get("days") or 0))

        # drip
        def _split_key(key: str) -> tuple[int, str]:
            admin_part, _, store_part = key.partition(":")
            try:
                return int(admin_part), store_part
            except ValueError:
                return 0, store_part

        sent_raw = data.get("drip_sent_ids") or {}
        subs_raw = data.get("drip_subscribers") or {}
        for key, conf in (data.get("drip_config") or {}).items():
            admin_id, store_name = _split_key(key)
            store_id = name_to_store.get((admin_id, store_name)) or name_to_store.get(
                (int((conf or {}).get("admin_uid") or 0), (conf or {}).get("store") or store_name)
            )
            if not store_id:
                continue
            self.upsert_drip(store_id, admin_id, int(conf.get("count") or 3), conf.get("time") or "19:30")
            self.set_drip_enabled(store_id, bool(conf.get("enabled")))
            self.set_drip_last_sent(store_id, conf.get("last_sent_date") or "")
            for old_fid in sent_raw.get(key, []):
                if old_fid in old_fid_map:
                    self.mark_sent(store_id, [old_fid_map[old_fid]])
            for uid in subs_raw.get(key, []):
                self._run("INSERT OR IGNORE INTO drip_subs(store_id, user_id) VALUES(?, ?)",
                          (store_id, int(uid)))

        # settings
        if data.get("force_channel"):
            self.set_meta("force_channel", data["force_channel"])
        if data.get("custom_caption"):
            self.set_meta("custom_caption", data["custom_caption"])
        if data.get("active_store"):
            for raw_admin, store_name in data["active_store"].items():
                store_id = name_to_store.get((int(raw_admin), store_name))
                if store_id:
                    self.set_active_store(int(raw_admin), store_id)

        self.set_meta("legacy_imported", "yes")
        log.info("Legacy JSON imported: %s", counters)
        return counters


class _LazyDB:
    """Module level handle so handlers can just do `db.files_of(...)`."""

    def __init__(self) -> None:
        self._impl: Database | None = None

    def bind(self, path: str) -> Database:
        candidate = Database(path)
        try:
            if candidate.backend == 'postgresql' and str(path) == str(cfg.DB_FILE):
                source = Path(getattr(cfg, 'SQLITE_BOOTSTRAP_FILE', 'bot_data.sqlite3'))
                if source.is_file() and source.stat().st_size and not any(
                        candidate._one(f'SELECT 1 FROM "{table}" LIMIT 1') for table in candidate.conn.metadata):
                    # Startup runs before the bot/web workers. Import is transactional,
                    # never deletes the source and refuses a populated destination.
                    original = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
                    snapshot = sqlite3.connect(':memory:')
                    try:
                        original.backup(snapshot)
                        candidate.conn.import_sqlite(snapshot.serialize())
                        log.info('Existing SQLite database imported into PostgreSQL; original retained')
                    finally:
                        original.close()
                        snapshot.close()
        except Exception:
            candidate.conn.close()
            raise
        self._impl = candidate
        return self._impl

    @property
    def impl(self) -> Database:
        if self._impl is None:
            self.bind(cfg.DB_FILE)
        assert self._impl is not None
        return self._impl

    def __getattr__(self, item: str):
        return getattr(self.impl, item)


db = _LazyDB()


# ============================================================================
# Visitor analytics — who is on the website now, and what they are doing.
# The tables live in SCHEMA (see `visits`, `page_views`, `visit_events`); this
# helper keeps every query in one place so the panel stays readable.
# ============================================================================
class _VisitorStore:
    """Thin query layer over the three analytics tables."""

    def __init__(self, database: "Database") -> None:
        self._db = database

    # ---------------------------------------------------------------- writes
    def visit(self, vid: str) -> dict | None:
        return self._db._dict(self._db._one("SELECT * FROM visits WHERE vid = ?",
                                            (str(vid),)))

    def open_visit(self, vid: str, *, user_id: int = 0, is_admin: int = 0, ip: str = "",
                   country: str = "", device: str = "", device_kind: str = "",
                   os: str = "", browser: str = "", referrer: str = "", source: str = "",
                   landing: str = "", user_agent: str = "", bot: int = 0,
                   offline: bool = False) -> None:
        now = time.time()
        self._db._run(
            "INSERT INTO visits(vid, user_id, is_admin, started_at, last_seen, ended_at,"
            " pageviews, ip, country, device, device_kind, os, browser, referrer, source,"
            " landing, user_agent, bot) VALUES(?,?,?,?,?,0,0,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(vid) DO UPDATE SET user_id = excluded.user_id,"
            " last_seen = excluded.last_seen, ended_at = 0, ip = excluded.ip,"
            " country = excluded.country, device = excluded.device,"
            " device_kind = excluded.device_kind, os = excluded.os, browser = excluded.browser,"
            " referrer = excluded.referrer, source = excluded.source,"
            " landing = excluded.landing, user_agent = excluded.user_agent",
            (str(vid), int(user_id or 0), int(is_admin or 0), now, now, str(ip or ""),
             str(country or ""), str(device or ""), str(device_kind or ""), str(os or ""),
             str(browser or ""), str(referrer or ""), str(source or ""), str(landing or ""),
             str(user_agent or ""), int(bot or 0)))

    def touch_visit(self, vid: str, path: str = "", user_id: int = 0) -> None:
        if user_id:
            self._db._run("UPDATE visits SET last_seen = ?, user_id = ? WHERE vid = ?",
                          (time.time(), int(user_id), str(vid)))
            return
        self._db._run("UPDATE visits SET last_seen = ? WHERE vid = ?", (time.time(), str(vid)))

    def close_visit(self, vid: str) -> None:
        self._db._run("UPDATE visits SET ended_at = ? WHERE vid = ?", (time.time(), str(vid)))

    def add_page_view(self, vid: str, *, path: str = "", title: str = "", status: int = 200,
                      ms: int = 0, is_admin: int = 0, user_id: int = 0) -> None:
        self._db._run(
            "INSERT INTO page_views(vid, user_id, path, title, status, ms, is_admin, ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (str(vid), int(user_id or 0), str(path or "")[:160], str(title or "")[:120],
             int(status or 200), int(ms or 0), int(is_admin or 0), time.time()))
        self._db._run("UPDATE visits SET pageviews = pageviews + 1 WHERE vid = ?", (str(vid),))

    def add_event(self, vid: str, name: str, *, detail: str = "", path: str = "",
                  store_id: int = 0, file_id: int = 0, user_id: int = 0) -> None:
        self._db._run(
            "INSERT INTO visit_events(vid, user_id, name, detail, path, store_id, file_id, ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (str(vid or ""), int(user_id or 0), str(name or "")[:40], str(detail or "")[:200],
             str(path or "")[:160], int(store_id or 0), int(file_id or 0), time.time()))

    # ----------------------------------------------------------------- reads
    def live_visits(self, seconds: int = 300) -> list[dict]:
        since = time.time() - max(30, int(seconds))
        return [dict(row) for row in self._db._q(
            "SELECT * FROM visits WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT 300",
            (since,))]

    def recent_visits(self, seconds: int = 86400, limit: int = 50) -> list[dict]:
        since = time.time() - max(60, int(seconds))
        return [dict(row) for row in self._db._q(
            "SELECT v.*, u.name AS user_name, u.username AS user_username FROM visits v"
            " LEFT JOIN users u ON u.user_id = v.user_id"
            " WHERE v.last_seen >= ? ORDER BY v.last_seen DESC LIMIT ?",
            (since, int(limit)))]

    def media_watchers(self, file_id: int, limit: int = 200) -> list[dict]:
        """Who opened/played one particular file (the “কারা দেখল” view)."""
        return [dict(row) for row in self._db._q(
            "SELECT e.*, u.name AS user_name, u.username AS user_username FROM visit_events e"
            " LEFT JOIN users u ON u.user_id = e.user_id WHERE e.file_id = ?"
            " ORDER BY e.id DESC LIMIT ?", (int(file_id), int(limit)))]

    def top_paths(self, seconds: int = 0, limit: int = 10) -> list[dict]:
        sql = ("SELECT path, COUNT(*) AS hits, COUNT(DISTINCT vid) AS people FROM page_views"
               " WHERE is_admin = 0 AND path NOT LIKE '/admin%'")
        params: list = []
        if seconds:
            sql += " AND ts >= ?"
            params.append(time.time() - int(seconds))
        sql += " GROUP BY path ORDER BY hits DESC LIMIT ?"
        params.append(int(limit))
        return [dict(row) for row in self._db._q(sql, tuple(params))]

    def top_media(self, limit: int = 10) -> list[dict]:
        rows = self._db._q(
            "SELECT e.file_id, COUNT(*) AS plays, COUNT(DISTINCT e.vid) AS people"
            " FROM visit_events e WHERE e.name IN ('media_play', 'media_open', 'prepare')"
            " AND e.file_id > 0 GROUP BY e.file_id ORDER BY plays DESC LIMIT ?",
            (int(limit),))
        out = []
        for row in rows:
            item = dict(row)
            file_row = self._db.file(item["file_id"]) if item.get("file_id") else None
            item["name"] = (file_row or {}).get("name") or f"ফাইল #{item['file_id']}"
            item["kind"] = (file_row or {}).get("kind") or ""
            out.append(item)
        return out

    def top_sources(self, limit: int = 8) -> list[dict]:
        return [dict(row) for row in self._db._q(
            "SELECT source, COUNT(*) AS people FROM visits WHERE is_admin = 0"
            " GROUP BY source ORDER BY people DESC LIMIT ?", (int(limit),))]

    def top_countries(self, limit: int = 8) -> list[dict]:
        return [dict(row) for row in self._db._q(
            "SELECT country, COUNT(*) AS people FROM visits WHERE is_admin = 0"
            " AND country <> '' GROUP BY country ORDER BY people DESC LIMIT ?", (int(limit),))]

    def devices(self, limit: int = 8) -> list[dict]:
        return [dict(row) for row in self._db._q(
            "SELECT device, COUNT(*) AS people FROM visits WHERE is_admin = 0"
            " GROUP BY device ORDER BY people DESC LIMIT ?", (int(limit),))]

    def recent_events(self, limit: int = 40) -> list[dict]:
        return [dict(row) for row in self._db._q(
            "SELECT e.*, u.name AS user_name, u.username AS user_username FROM visit_events e"
            " LEFT JOIN users u ON u.user_id = e.user_id"
            " ORDER BY e.id DESC LIMIT ?", (int(limit),))]

    def events_of(self, vid: str, limit: int = 200) -> list[dict]:
        return [dict(row) for row in self._db._q(
            "SELECT * FROM visit_events WHERE vid = ? ORDER BY id DESC LIMIT ?",
            (str(vid), int(limit)))]

    def pages_of(self, vid: str, limit: int = 200) -> list[dict]:
        return [dict(row) for row in self._db._q(
            "SELECT * FROM page_views WHERE vid = ? ORDER BY id DESC LIMIT ?",
            (str(vid), int(limit)))]

    def per_day(self, days: int = 14) -> list[dict]:
        since = time.time() - int(days) * 86400
        by_day: dict[str, dict] = {}
        for row in self._db._q(
                "SELECT started_at, is_admin, pageviews FROM visits WHERE started_at >= ?",
                (since,)):
            key = time.strftime("%Y-%m-%d", time.localtime(float(row["started_at"] or 0)))
            bucket = by_day.setdefault(key, {"day": key, "visitors": 0, "views": 0, "events": 0})
            if not int(row["is_admin"] or 0):
                bucket["visitors"] += 1
            bucket["views"] += int(row["pageviews"] or 0)
        for row in self._db._q(
                "SELECT ts FROM visit_events WHERE ts >= ?", (since,)):
            key = time.strftime("%Y-%m-%d", time.localtime(float(row["ts"])))
            bucket = by_day.setdefault(key, {"day": key, "visitors": 0, "views": 0, "events": 0})
            bucket["events"] += 1
        return list(by_day.values())

    def summary(self, day: str) -> dict:
        start = time.mktime(time.strptime(day, "%Y-%m-%d"))
        end = start + 86400
        visitors = self._db._one(
            "SELECT COUNT(DISTINCT vid) AS people, COUNT(*) AS visits, SUM(pageviews) AS views"
            " FROM visits WHERE started_at >= ? AND started_at < ? AND is_admin = 0",
            (start, end))
        events = self._db._one("SELECT COUNT(*) AS n FROM visit_events WHERE ts >= ? AND ts < ?",
                               (start, end))
        new_people = self._db._one(
            "SELECT COUNT(*) AS n FROM visits WHERE started_at >= ? AND started_at < ?"
            " AND is_admin = 0 AND vid NOT IN (SELECT vid FROM visits WHERE started_at < ?)",
            (start, end, start))
        return {"day": day, "people": int((visitors or {})["people"] or 0) if visitors else 0,
                "visits": int((visitors or {})["visits"] or 0) if visitors else 0,
                "views": int((visitors or {})["views"] or 0) if visitors else 0,
                "events": int((events or {})["n"] or 0) if events else 0,
                "new_people": int((new_people or {})["n"] or 0) if new_people else 0}

    def totals(self) -> dict:
        row = self._db._one(
            "SELECT COUNT(*) AS visits, COUNT(DISTINCT vid) AS people, SUM(pageviews) AS views,"
            " MIN(started_at) AS first_at, MAX(last_seen) AS last_at FROM visits WHERE is_admin = 0")
        events = self._db._one("SELECT COUNT(*) AS n FROM visit_events")
        return {"visits": int((row or {})["visits"] or 0) if row else 0,
                "people": int((row or {})["people"] or 0) if row else 0,
                "views": int((row or {})["views"] or 0) if row else 0,
                "events": int((events or {})["n"] or 0) if events else 0,
                "first_at": float((row or {})["first_at"] or 0) if row else 0.0,
                "last_at": float((row or {})["last_at"] or 0) if row else 0.0}

    def linked_users(self, limit: int = 30) -> list[dict]:
        return [dict(row) for row in self._db._q(
            "SELECT v.vid, v.user_id, v.last_seen, v.device, v.source, v.landing,"
            " u.name AS user_name, u.username AS user_username FROM visits v"
            " JOIN users u ON u.user_id = v.user_id WHERE v.user_id > 0 AND v.is_admin = 0"
            " ORDER BY v.last_seen DESC LIMIT ?", (int(limit),))]

    # ------------------------------------------------------------- retention
    def prune(self, days: int = 90) -> dict:
        edge = time.time() - max(1, int(days)) * 86400
        removed = 0
        for table, column in (("page_views", "ts"), ("visit_events", "ts"),
                              ("visits", "last_seen")):
            cursor = self._db._run(f"DELETE FROM {table} WHERE {column} < ?", (edge,))
            removed += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        return {"days": int(days), "removed": removed, "before": edge}

    def forget(self, vid: str) -> int:
        removed = 0
        for sql in ("DELETE FROM page_views WHERE vid = ?", "DELETE FROM visit_events WHERE vid = ?",
                    "DELETE FROM visits WHERE vid = ?"):
            cursor = self._db._run(sql, (str(vid),))
            removed += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        return removed


Database.visitors = property(lambda self: _VisitorStore(self))          # type: ignore[attr-defined]

class _BotStore:
    """Extra bots: add, list, edit, remove (each one runs in its own process)."""

    def __init__(self, database: "Database") -> None:
        self._db = database

    def add_bot(self, token: str, *, label: str = "", admin_id: int = 0, api_id: int = 0,
                api_hash: str = "", note: str = "", enabled: int = 1,
                origin: str = "panel") -> int:
        cursor = self._db._run(
            "INSERT INTO bots(label, token, admin_id, api_id, api_hash, note, enabled,"
            " created_at, origin) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(label or ""), str(token or ""), int(admin_id or 0), int(api_id or 0),
             str(api_hash or ""), str(note or ""), int(enabled or 0), time.time(),
             str(origin or "panel")))
        return int(cursor.lastrowid or 0)

    def bots(self) -> list[dict]:
        return [dict(row) for row in self._db._q("SELECT * FROM bots ORDER BY id")]

    def bot(self, bot_id: int) -> dict | None:
        return self._db._dict(self._db._one("SELECT * FROM bots WHERE id = ?", (int(bot_id),)))

    def bot_by_token(self, token: str) -> dict | None:
        return self._db._dict(self._db._one("SELECT * FROM bots WHERE token = ?", (str(token),)))

    def update_bot(self, bot_id: int, **fields) -> None:
        allowed = {"label", "username", "token", "api_id", "api_hash", "admin_id", "note",
                   "enabled", "last_start", "last_error", "origin"}
        pairs = [(key, value) for key, value in fields.items() if key in allowed]
        if not pairs:
            return
        sql = "UPDATE bots SET " + ", ".join(f"{key} = ?" for key, _ in pairs) + " WHERE id = ?"
        self._db._run(sql, tuple(value for _, value in pairs) + (int(bot_id),))

    def delete_bot(self, bot_id: int) -> None:
        self._db._run("DELETE FROM bots WHERE id = ?", (int(bot_id),))


Database.bots_store = property(lambda self: _BotStore(self))            # type: ignore[attr-defined]
