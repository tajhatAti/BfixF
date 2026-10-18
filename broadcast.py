"""
Simple Telegram broadcast bot (aiogram 3.x)
pip install aiogram

Admin flow:
  /new        start a draft
  (send any messages: text, photo, video, gif, file, audio, voice)
  /buttons    add inline buttons (see format below)
  /preview    send the draft to yourself
  /send       broadcast to all users + get a report (counts + CSV of who got it / who didn't)
  /cancel     discard draft
  /stats      user count

Buttons format (one line = one row, && = same row):
  /buttons Website | https://example.com && Channel | https://t.me/xxx
  Support | https://t.me/support

Text styling: use Telegram's native formatting, OR type raw HTML:
  <b>bold</b> <i>italic</i> <u>under</u> <s>strike</s> <code>mono</code>
  <a href="https://x.com">link</a> <tg-spoiler>spoiler</tg-spoiler>
"""
import asyncio
import csv
import io
import logging
import re
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

TOKEN = "8981959911:AAHKqzU1JoKgRs-sjG-JQ_77EL_Ap5HRxjM"
ADMINS = {8751228520}

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

db = sqlite3.connect("users.db")
db.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, blocked INTEGER DEFAULT 0, "
           "username TEXT, name TEXT)")
for col in ("username", "name"):  # migrate older db
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
    except sqlite3.OperationalError:
        pass
db.commit()

drafts: dict[int, dict] = {}  # admin_id -> {"items": [], "kb": None}


# ---------- helpers ----------
HTML_TAG = re.compile(
    r"</?(b|strong|i|em|u|ins|s|strike|del|a|code|pre|tg-spoiler|span|blockquote|tg-emoji)\b[^>]*>",
    re.I)


def extract(m: Message):
    """Return (type, file_id, html_text) for a message, or None."""
    raw = m.text or m.caption or ""
    if not raw:
        html = ""
    elif HTML_TAG.search(raw):
        html = raw  # you typed HTML tags yourself -> used exactly as written
    else:
        html = m.html_text or ""  # native Telegram formatting (bold/italic...) kept, safely escaped
    if m.photo:
        return "photo", m.photo[-1].file_id, html
    if m.video:
        return "video", m.video.file_id, html
    if m.animation:
        return "animation", m.animation.file_id, html
    if m.document:
        return "document", m.document.file_id, html
    if m.audio:
        return "audio", m.audio.file_id, html
    if m.voice:
        return "voice", m.voice.file_id, html
    if m.text:
        return "text", None, html
    return None


async def send_item(chat_id: int, item, kb):
    kind, fid, html = item
    if kind == "text":
        return await bot.send_message(chat_id, html, reply_markup=kb)
    send = getattr(bot, f"send_{kind}")
    return await send(chat_id, caption=html or None, reply_markup=kb, **{kind: fid})


async def send_draft(chat_id: int, d: dict):
    last = len(d["items"]) - 1
    for i, item in enumerate(d["items"]):
        await send_item(chat_id, item, d["kb"] if i == last else None)


def parse_buttons(text: str):
    rows = []
    for line in text.splitlines():
        row = []
        for part in line.split("&&"):
            if "|" in part:
                label, url = (x.strip() for x in part.split("|", 1))
                if label and url:
                    row.append(InlineKeyboardButton(text=label, url=url))
        if row:
            rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def is_admin(m: Message):
    return m.from_user and m.from_user.id in ADMINS


ADMIN_HELP = (
    "👑 <b>Admin Panel</b>\n\n"
    "<b>ব্রডকাস্ট করার নিয়ম:</b>\n"
    "1️⃣ /new — নতুন ব্রডকাস্ট শুরু\n"
    "2️⃣ এখন যা পাঠাতে চান পাঠান (টেক্সট, ছবি, ভিডিও, GIF, ফাইল — একাধিক মেসেজও চলবে)\n"
    "3️⃣ /buttons — ইনলাইন বাটন যোগ করুন\n"
    "4️⃣ /preview — আগে নিজে দেখে নিন\n"
    "5️⃣ /send — সবাইকে পাঠান + রিপোর্ট পাবেন\n\n"
    "/cancel — ড্রাফট বাতিল\n"
    "/stats — ইউজার সংখ্যা\n\n"
    "<b>বাটন ফরম্যাট:</b>\n"
    "<code>/buttons Website | https://example.com && Channel | https://t.me/xxx</code>\n"
    "(নতুন লাইন = নতুন সারি, && = একই সারিতে)\n\n"
    "<b>HTML স্টাইল:</b> মেসেজে সরাসরি লিখুন\n"
    "<code>&lt;b&gt;bold&lt;/b&gt; &lt;i&gt;italic&lt;/i&gt; &lt;u&gt;underline&lt;/u&gt;</code>"
)


# ---------- handlers ----------
@dp.message(Command("start"))
async def start(m: Message):
    u = m.from_user
    db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (u.id,))
    db.execute("UPDATE users SET blocked=0, username=?, name=? WHERE id=?",
               (u.username or "", u.full_name, u.id))
    db.commit()
    if u.id in ADMINS:
        return await m.answer(ADMIN_HELP)
    await m.answer("✅ Subscribed. You'll receive updates here.")


@dp.message(Command("help"), is_admin)
async def help_cmd(m: Message):
    await m.answer(ADMIN_HELP)


@dp.message(Command("new"), is_admin)
async def new(m: Message):
    drafts[m.from_user.id] = {"items": [], "kb": None}
    await m.answer("Draft started. Send your messages (text/media/video).\n"
                   "Then /buttons, /preview, /send.")


@dp.message(Command("buttons"), is_admin)
async def buttons(m: Message):
    d = drafts.get(m.from_user.id)
    if not d:
        return await m.answer("Use /new first.")
    parts = m.text.split(maxsplit=1)
    kb = parse_buttons(parts[1]) if len(parts) > 1 else None
    if not kb:
        return await m.answer("Format:\n<code>/buttons Text | https://url && Text2 | https://url2</code>")
    d["kb"] = kb
    await m.answer("✅ Buttons set.")


@dp.message(Command("preview"), is_admin)
async def preview(m: Message):
    d = drafts.get(m.from_user.id)
    if not d or not d["items"]:
        return await m.answer("Draft is empty.")
    try:
        await send_draft(m.chat.id, d)
    except TelegramBadRequest as e:
        await m.answer(f"❌ {e.message}")


@dp.message(Command("cancel"), is_admin)
async def cancel(m: Message):
    drafts.pop(m.from_user.id, None)
    await m.answer("Draft discarded.")


@dp.message(Command("stats"), is_admin)
async def stats(m: Message):
    total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM users WHERE blocked=0").fetchone()[0]
    await m.answer(f"👥 Total: {total}\n✅ Active: {active}")


@dp.message(Command("send"), is_admin)
async def send(m: Message):
    d = drafts.get(m.from_user.id)
    if not d or not d["items"]:
        return await m.answer("Draft is empty.")
    users = db.execute("SELECT id, username, name, blocked FROM users").fetchall()
    status_msg = await m.answer(f"📤 Sending to {len(users)} users...")
    results = []  # (id, username, name, status, reason)
    for uid, uname, name, blocked in users:
        if blocked:
            results.append((uid, uname, name, "skipped", "previously blocked the bot"))
            continue
        status, reason = "success", ""
        while True:
            try:
                await send_draft(uid, d)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                continue
            except TelegramForbiddenError:
                db.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
                db.commit()
                status, reason = "failed", "blocked bot / account deactivated"
            except TelegramBadRequest as e:
                if "parse" in str(e).lower():  # broken HTML -> would fail for everyone
                    await m.answer(f"❌ Broadcast stopped: HTML error in your message.\n<code>{e.message}</code>\n"
                                   "Fix it and use /preview before /send.")
                    return
                status, reason = "failed", str(e)[:120]
            except Exception as e:
                status, reason = "failed", str(e)[:120]
            break
        results.append((uid, uname, name, status, reason))
        await asyncio.sleep(0.05)  # ~20 msg/sec, under Telegram's limit
    drafts.pop(m.from_user.id, None)

    ok = sum(r[3] == "success" for r in results)
    fail = sum(r[3] == "failed" for r in results)
    skip = sum(r[3] == "skipped" for r in results)
    await status_msg.edit_text(
        f"✅ Broadcast done\n\n✔️ Success: {ok}\n❌ Failed: {fail}\n⏭ Skipped: {skip}")

    # full report as CSV (opens in Excel / Sheets)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "name", "status", "reason"])
    for uid, uname, name, st, why in sorted(results, key=lambda r: r[3] != "success"):
        w.writerow([uid, f"@{uname}" if uname else "", name or "", st, why])
    fname = f"broadcast_report_{datetime.now():%Y%m%d_%H%M}.csv"
    await m.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), fname),
                            caption="📋 Full report: who received it and who didn't")

    # short readable list of failures right in chat
    bad = [r for r in results if r[3] == "failed"]
    if bad:
        lines = [f"• {r[2] or r[0]} ({'@' + r[1] if r[1] else r[0]}) — {r[4]}" for r in bad[:30]]
        more = f"\n…and {len(bad) - 30} more in the file" if len(bad) > 30 else ""
        await m.answer("❌ <b>Failed:</b>\n" + "\n".join(lines) + more)


@dp.message(is_admin)
async def collect(m: Message):
    d = drafts.get(m.from_user.id)
    if not d:
        return
    item = extract(m)
    if not item:
        return await m.answer("Unsupported message type.")
    d["items"].append(item)
    await m.answer(f"➕ Added #{len(d['items'])}")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
