"""Throttled, SQLite-backed byte progress shared by bots and the website.

Only byte callbacks update counters: queued/waiting is never presented as a
running download. ETA is an estimate for the current phase, not the whole queue.
"""
from __future__ import annotations

import time
from app.storage import db
from app.utils import esc

LABELS = {'prepare': 'প্রস্তুতি/সোর্স খোলা', 'channel': 'গন্তব্য চ্যানেল প্রস্তুত',
          'forward': 'Telegram-এ সরাসরি কপি', 'download': 'ডাউনলোড',
          'thumb': 'থাম্বনেইল', 'upload': 'আপলোড', 'done': 'সম্পন্ন',
          'failed': 'ব্যর্থ', 'waiting': 'অপেক্ষায়', 'flood': 'Telegram অপেক্ষা'}


class Tracker:
    def __init__(self, scope: str, key: int, owner: int, name: str):
        self.scope, self.key, self.owner, self.name = scope, key, owner, name
        self.bound = db.impl
        self.last_write = 0.0
        self.started = time.time()
        self.stage = ''

    def phase(self, stage: str, total: int = 0, detail: str = '') -> None:
        if db.impl is not self.bound:
            return
        self.stage, self.started, self.last_write = stage, time.time(), 0.0
        self._write(0, total, detail)

    def callback(self, current: int, total: int) -> None:
        if db.impl is not self.bound:
            return
        now = time.time()
        if now - self.last_write >= 1 or (total and current >= total):
            self._write(current, total, '')

    def _write(self, current, total, detail):
        now = time.time()
        self.last_write = now
        db._run('INSERT INTO transfer_progress(scope,file_key,owner_id,name,stage,current,total,started_at,updated_at,detail) '
                'VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,file_key) DO UPDATE SET '
                'owner_id=excluded.owner_id,name=excluded.name,stage=excluded.stage,current=excluded.current,'
                'total=excluded.total,started_at=excluded.started_at,updated_at=excluded.updated_at,detail=excluded.detail',
                (self.scope, self.key, self.owner, self.name[:100], self.stage, int(current), int(total), self.started, now, detail[:500]))


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f'{seconds // 3600}ঘ {seconds % 3600 // 60}মি' if seconds >= 3600 else f'{seconds // 60}মি {seconds % 60}সে'


def render(scope: str | None = None, owner: int | None = None) -> str:
    where, args = [], []
    if scope:
        where.append('scope=?'); args.append(scope)
    if owner is not None:
        where.append('owner_id=?'); args.append(owner)
    sql = 'SELECT * FROM transfer_progress' + (' WHERE ' + ' AND '.join(where) if where else '')
    rows = db._q(sql + " ORDER BY CASE WHEN stage IN ('done','failed') THEN 1 ELSE 0 END,updated_at DESC LIMIT 3", tuple(args))
    lines = []
    now = time.time()
    for row in rows:
        current, total = row['current'], row['total']
        elapsed = max(.001, row['updated_at'] - row['started_at'])
        speed = current / elapsed if current else 0
        lines.append(f"🎬 <b>{esc(row['name'])}</b> · {LABELS.get(row['stage'], 'অপেক্ষায়')}")
        if row['stage'] in ('download', 'upload'):
            pct = f'{min(100, current * 100 / total):.1f}%' if total else 'সাইজ জানা যায়নি'
            eta = duration((total - current) / speed) if total and speed else 'হিসাব হচ্ছে'
            lines.append(f'{current / 1048576:.1f} / {total / 1048576:.1f} MiB · {pct}\n'
                         f'গতি {speed / 1048576:.2f} MiB/s · এই ধাপে বাকি ≈ {eta}')
        if row['detail']:
            lines.append(esc(row['detail']))
        age = now - row['updated_at']
        if row['stage'] not in ('done', 'failed') and age > 30:
            lines.append(f'⚠️ নতুন byte progress আসেনি {duration(age)} ধরে; সংযোগ/worker পরীক্ষা করুন।')
    return '\n'.join(lines) or 'এখন কোনো সক্রিয় transfer নেই।'
