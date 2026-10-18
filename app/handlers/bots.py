"""Several bots on one database — the owner's “একাধিক বট” system, from Telegram.

    /bots                     list them, with a live running dot
    forward a @BotFather message → the token is learned and the bot starts
    /bots add <token>         add by hand (same thing, typed)
    /bots start|stop|restart <id>
    /bots drop <id>           forget one

The interesting part is the forward: the owner writes `/newbot` in @BotFather,
BotFather answers with a token, the owner forwards that very message here — and
this install already knows its own API ID/hash, so the new bot starts without
asking for anything. That is what “বারবার API আইডি/হ্যাশ দিতে হচ্ছে কেন” was about.
"""
from __future__ import annotations

from telethon import Button, events

from app import config as cfg
from app.handlers.router import route
from app.handlers.state import peek, take
from app.logger import log
from app.runtime import bot
from app.services import botrunner, credentials
from app.storage import db
from app.utils import esc

WAITING = "bots_wait_token"


def _is_admin(event) -> bool:
    return bool(event.is_private and event.sender_id in set(credentials.admins()))


def _dots(row: dict) -> str:
    if row.get("main"):
        return "🟢 মূল বট"
    return "🟢 চালু" if row.get("running") else "⚪️ বন্ধ"


def _listing() -> str:
    rows = botrunner.status()
    if not rows:
        return "কোনো বট নেই।"
    lines = []
    for row in rows:
        name = row.get("label") or f"বট #{row.get('id')}"
        user = f" · @{row['username']}" if row.get("username") else ""
        token = row.get("masked") or botrunner.mask_token(row.get("token") or "")
        extra = ""
        if row.get("last_error"):
            extra = f"\n     ⚠️ {esc(str(row['last_error'])[:80])}"
        elif row.get("admin_id"):
            extra = f"\n     অ্যাডমিন: <code>{row['admin_id']}</code>"
        lines.append(f"{_dots(row)} <b>{esc(name)}</b>{user}\n     <code>{token}</code>{extra}")
    return "\n\n".join(lines)


def _help() -> str:
    return (
        "🤖 <b>বট ম্যানেজার</b>\n"
        "একই ডেটাবেজে একাধিক বট চালানো যায় — একটা বন্ধ/ব্লক হলে বাকিগুলো চলতেই থাকবে।\n\n"
        "<b>নতুন বট যোগ করার সহজ রাস্তা</b>\n"
        "১. টেলিগ্রামে <b>@BotFather</b> খুলুন → <code>/newbot</code>\n"
        "২. নাম ঠিক করুন, ও টোকেনটা পাঠাবে\n"
        "৩. <b>ওই মেসেজটা এখানে ফরওয়ার্ড করুন</b> — বাকি সব (API আইডি/হ্যাশ, অ্যাডমিন)\n"
        "   এই ইনস্টল থেকেই নিয়ে নতুন বট নিজে থেকে চালু হয়ে যাবে।\n\n"
        "অথবা: <code>/bots add 1234567890:AAH…</code>\n"
        "<code>/bots start 2</code> · <code>/bots stop 2</code> · <code>/bots restart 2</code>\n"
        "· <code>/bots drop 2</code>")


@bot.on(events.NewMessage(func=lambda e: _is_admin(e) and (e.raw_text or "").strip()
                          .lower().startswith("/bots")))
async def bots_command(event) -> None:
    """`/bots …` — add, list, start, stop, drop."""
    if not _is_admin(event):
        return
    parts = (event.raw_text or "").strip().split()
    action = (parts[1].lower() if len(parts) > 1 else "list")
    argument = parts[2] if len(parts) > 2 else ""

    if action in ("list", "ls", ""):
        await event.respond("🤖 <b>বটগুলো</b>\n\n" + _listing()
                            + "\n\n<i>@" + "BotFather</i>-এর টোকেন মেসেজ ফরওয়ার্ড করলেই নতুন বট "
                              "যোগ হবে। বিস্তারিত: /bots help")
        return

    if action in ("help", "?", "how"):
        await event.respond(_help())
        return

    if action in ("add", "new", "যোগ"):
        if argument:
            await _add_token(event, argument)
            return
        from app.handlers.state import ask
        ask(event.sender_id, WAITING)
        await event.respond("🔑 BotFather-এর টোকেনটা পাঠান (অথবা পুরো মেসেজটা ফরওয়ার্ড করুন)।")
        return

    if not argument.isdigit():
        await event.respond("আইডিটা লিখুন, যেমন <code>/bots stop 2</code> — কোন আইডি জানতে "
                            "<code>/bots</code> দেখুন।")
        return
    row = db.bots_store.bot(int(argument))
    if row is None:
        await event.respond("এই আইডির কোনো বট নেই। <code>/bots</code> দেখুন।")
        return

    if action in ("start", "চালু"):
        report = botrunner.start(row)
        text = (f"🟢 <b>{esc(row.get('label') or '')}</b> চালু হয়েছে (pid {report.get('pid')})"
                if report.get("ok") else f"❌ চালু হলো না: {esc(report.get('error') or '')}")
        await event.respond(text)
    elif action in ("stop", "বন্ধ"):
        botrunner.stop(row)
        await event.respond(f"⚪️ <b>{esc(row.get('label') or '')}</b> বন্ধ করা হয়েছে।")
    elif action in ("restart", "রিস্টার্ট"):
        report = botrunner.restart(row)
        await event.respond("🔄 রিস্টার্ট হয়েছে।" if report.get("ok")
                            else f"❌ {esc(report.get('error') or '')}")
    elif action in ("drop", "delete", "rm", "বাদ"):
        botrunner.stop(row)
        db.bots_store.delete_bot(int(argument))
        await event.respond(f"🗑 <b>{esc(row.get('label') or '')}</b> বাদ দেওয়া হয়েছে "
                            "(টোকেনটা BotFather-এ revoke করে দিন)।")
    else:
        await event.respond(_help())


#: A token handed over by hand (after `/bots add`) or by forwarding BotFather.
@bot.on(events.NewMessage(func=lambda e: _is_admin(e) and peek(e.sender_id) is not None
                          and (peek(e.sender_id) or {}).get("action") == WAITING))
async def bots_token_here(event) -> None:
    if (event.raw_text or '').strip().startswith('/'):
        return  # slash commands always escape a pending media/token prompt

    if not _is_admin(event):
        return
    token = botrunner.token_of(event) or (event.raw_text or "").strip()
    take(event.sender_id)
    await _add_token(event, token)


@bot.on(events.NewMessage(func=lambda e: _is_admin(e) and e.message is not None
                          and botrunner.is_from_botfather(e.message)))
async def bots_botfather_forward(event) -> None:
    """The owner forwards BotFather's “Use this token…” message → bot added."""
    if not _is_admin(event):
        return
    token = botrunner.token_of(event.message)
    if not token:
        return
    from app.handlers.state import take as take_state
    take_state(event.sender_id)
    label = _guess_label(event.message)
    report = botrunner.add(token, label=label, origin="forward",
                           note="BotFather থেকে ফরওয়ার্ড")
    if not report.get("ok"):
        await event.respond(f"🤖 টোকেন পেয়েছি, কিন্তু যোগ করা যায়নি: {esc(report['error'])}")
        return
    row = db.bots_store.bot(int(report["id"]))
    started = botrunner.start(row) if row else {"ok": False}
    if started.get("ok"):
        await event.respond(
            f"✅ <b>নতুন বট যোগ হয়ে চালু হয়ে গেছে</b>\n"
            f"🏷 {esc(label)}\n🔑 <code>{botrunner.mask_token(token)}</code>\n"
            f"👤 অ্যাডমিন: <code>{(row or {}).get('admin_id') or ''}</code>\n\n"
            "এখন ওই বটেও <code>/start</code> দিলে একই স্টোর, অর্ডার আর ইউজার পাবেন — "
            "ডেটাবেজ একটাই।")
    else:
        await event.respond(
            "✅ টোকেন যোগ হয়েছে, কিন্তু চালু করা যায়নি: "
            f"{esc(started.get('error') or '')}\n<code>/bots start {report['id']}</code> দিয়ে "
            "আবার চেষ্টা করুন।")


def _guess_label(message) -> str:
    """A friendly name for the new bot out of BotFather's own message.

    BotFather's text contains `t.me/<username>_bot`, which is exactly the name the
    owner recognises, so that comes first; otherwise the first short line.
    """
    text = (getattr(message, "raw_text", None) or getattr(message, "text", None) or "")
    import re
    found = re.search(r"t\.me/(@?[A-Za-z0-9_]{4,32})", text)
    if found:
        return "@" + found.group(1).lstrip("@")
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if "http api" in lowered or botrunner.TOKEN_RE.search(cleaned):
            continue
        if any(hint in lowered for hint in ("congratulations", "you will find it",
                                            "revoke the current token")):
            continue
        if 2 <= len(cleaned) <= 40:
            return cleaned[:40]
    return ""


async def _add_token(event, token: str) -> None:
    report = botrunner.add(token.strip(), origin="manual")
    if not report.get("ok"):
        await event.respond(f"❌ {esc(report['error'])}")
        return
    row = db.bots_store.bot(int(report["id"]))
    started = botrunner.start(row) if row else {"ok": False}
    if started.get("ok"):
        await event.respond(f"✅ বট #{report['id']} যোগ হয়ে চালু হয়েছে "
                            f"(pid {started.get('pid')})। /bots দিয়ে দেখুন।")
    else:
        log.warning("new bot %s did not start: %s", report["id"], started)
        await event.respond(f"✅ বট #{report['id']} যোগ হয়েছে, চালু করা যায়নি — "
                            f"<code>/bots start {report['id']}</code>")


def buttons_for(rows: list[dict]) -> list[list]:
    """Inline controls the panel-side listing also uses."""
    out = []
    for row in rows:
        if row.get("main"):
            continue
        bot_id = row.get("id")
        out.append([
            Button.inline(f"▶️ চালু #{bot_id}", data=f"bots:start:{bot_id}".encode()),
            Button.inline(f"⏹ বন্ধ #{bot_id}", data=f"bots:stop:{bot_id}".encode()),
            Button.inline(f"🗑 #{bot_id}", data=f"bots:drop:{bot_id}".encode()),
        ])
    return out


@route("bots:")
async def bots_buttons(event, rest: str) -> None:
    """▶️ চালু · ⏹ বন্ধ · 🗑 বাদ — the buttons under the bot list."""
    if event.sender_id not in set(credentials.admins()):
        await event.answer("শুধু অ্যাডমিন পারবেন", alert=True)
        return
    parts = (rest or "").split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await event.answer("বাটনটা চেনা যায়নি", alert=True)
        return
    action, raw_id = parts[0], parts[1]
    row = db.bots_store.bot(int(raw_id))
    if row is None:
        await event.answer("বটটা নেই", alert=True)
        return
    if action == "start":
        report = botrunner.start(row)
        await event.answer("চালু হচ্ছে…")
        await event.edit(f"🤖 <b>{esc(row.get('label') or '')}</b>\n\n" + _listing(),
                         buttons=buttons_for(botrunner.status()))
        if not report.get("ok"):
            await event.respond(f"❌ {esc(report.get('error') or '')}")
        return
    if action == "stop":
        botrunner.stop(row)
        await event.answer("বন্ধ করা হলো")
        await event.edit(f"🤖 <b>বটগুলো</b>\n\n" + _listing(),
                         buttons=buttons_for(botrunner.status()))
        return
    botrunner.stop(row)
    db.bots_store.delete_bot(int(raw_id))
    await event.answer("বাদ দেওয়া হলো")
    await event.edit(f"🤖 <b>বটগুলো</b>\n\n" + _listing(),
                     buttons=buttons_for(botrunner.status()))
