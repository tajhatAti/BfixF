"""`/backup` আর `/restore` — বটের ভিতর দিয়েই হোস্টিং বদলানোর সিস্টেম।

নতুন জায়গায় বট চালু করে মালিক যা করবেন:

1. `/restore` পাঠান → বট বলে দেয় “ফাইলটা এখন পাঠান”,
2. পুরোনো জায়গা থেকে নামানো ব্যাকআপ ফাইলটা ওই চ্যাটেই পাঠান (বা ফাইলে রিপ্লাই
   করে `/restore` লিখুন),
3. বট দেখায় ব্যাকআপে কী আছে (কত ইউজার, কত স্টোর, কবে বানানো) → **✅ ফিরিয়ে আনুন**
   চাপলেই পুরো ইনস্টল আগের অবস্থায় ফিরে আসে।

ছোট ইনস্টল হলে ফাইলের দরকারও নেই: `/restore` তারপর `BFIXF1:…` কোডগুলো পরপর
পেস্ট করলেই বট টুকরোগুলো জোড়া লাগিয়ে ফিরিয়ে আনে। `/backup` আবার ফাইল + কোড
দুটোই পাঠায়।
"""
from __future__ import annotations

from telethon import Button, events

from app.handlers.router import route
from app.handlers.state import clear as clear_flow, peek, take
from app.logger import log
from app.runtime import bot
from app.services import portable
from app.utils import esc

__all__ = ["backup_command", "restore_command", "restore_document", "restore_text",
           "restore_confirm"]


def _client(event):
    """The Telethon client of this update (falls back to the shared bot)."""
    return getattr(event, "client", None) or bot

#: admin_id -> backup bytes waiting for the “✅ ফিরিয়ে আনুন” tap.
_pending: dict[int, bytes] = {}
#: admin_id -> backup bytes being collected from pasted `BFIXF1:` pieces.
_staged: dict[int, bytes] = {}
#: admin_id -> {index: code} while the pieces arrive one message at a time.
_parts: dict[int, dict[int, str]] = {}


def _is_admin_private(event) -> bool:
    try:
        from app import config as cfg
        return bool(event.is_private and not event.out
                    and event.sender_id in cfg.ADMIN_IDS)
    except Exception:
        return False


def _confirm_buttons(admin_id: int) -> list[list[Button]]:
    return [[Button.inline("✅ ফিরিয়ে আনুন", "bk:go"),
             Button.inline("✖️ বাতিল", "bk:no")]]


def _summary(data: bytes) -> tuple[bool, str]:
    """Read the file and describe it — never touches the live database."""
    try:
        info = portable.inspect(data)
    except portable.BackupError as exc:
        return False, f"❌ {esc(str(exc))}"
    counts = info.get("counts") or {}
    bits = " · ".join(f"{label}: <b>{counts.get(key, 0)}</b>"
                      for key, label in (("users", "ইউজার"), ("stores", "স্টোর"),
                                         ("files", "ফাইল"), ("orders", "অর্ডার"))
                      if key in counts)
    made = (f"🕘 বানানো: {esc(info.get('created_at_text') or '')}<br>"
            if info.get("created_at_text") else "")
    if info.get("merge"):
        # an older version's database / another bot's user list — added, not swapped
        note = ("ℹ️ এটা <b>পুরোনো ফরম্যাট</b> — এই ইনস্টলের ডেটা মুছবে না, শুধু "
                "<b>যোগ</b> হবে।" if info["kind"] != portable.KIND_USERS else
                "ℹ️ এটা পুরোনো বটের <b>ইউজার তালিকা</b> — আপনার বর্তমান ডেটার সাথে "
                "যোগ হবে, কিছু মোছবে না।")
        return True, (f"🔎 <b>{esc(info.get('label') or 'পুরোনো ব্যাকআপ')}</b><br>"
                      f"{made}{bits}<br>{note}")
    creds = ("🔑 টোকেনও আছে — নতুন হোস্টে কিছু সেট করতে হবে না।"
             if info.get("credentials_included") else
             "🔑 টোকেন নেই — /setup-এ একবার টোকেন/API আইডি দিতে হবে।")
    return True, (f"🔎 <b>ব্যাকআপে যা আছে</b><br>{made}{bits}<br>{creds}<br>"
                  f"<span class='muted'>সাইজ: {info.get('size', 0) // 1024} KB · "
                  f"checksum {esc(info.get('checksum') or '')}</span>")


async def _offer(event, admin_id: int, data: bytes, source: str) -> None:
    """Show what is inside and wait for the confirmation tap."""
    ok, summary = _summary(data)
    if not ok:
        await event.respond(summary)
        return
    _pending[admin_id] = data
    merge = bool(portable.inspect(data).get("merge"))
    if merge:
        note = ("✅ চাপলে উপরের ডেটাগুলো এই ইনস্টলে <b>যোগ</b> হবে — এখনকার কিছুই "
                "মুছে যাবে না।")
    else:
        note = ("⚠️ <b>এখনকার সব ডেটা এই ব্যাকআপ দিয়ে বদলে যাবে।</b> "
                "আগের অবস্থার সেফটি কপি নিজে থেকেই সেভ হয়ে যাবে, তাই ভুল হলে "
                "ফেরানো যাবে।")
    await event.respond(f"{summary}\n\n{note}\n\nসোর্স: <code>{esc(source)}</code>",
                        buttons=_confirm_buttons(admin_id))


def _restore_help() -> str:
    return ("♻️ <b>হোস্টিং বদল / পুরোনো ইনস্টল ফিরিয়ে আনা</b>\n\n"
            "১️⃣ আগের জায়গা থেকে <code>/backup</code> দিয়ে যে ফাইলটা পেয়েছিলেন, সেটা "
            "<b>এই চ্যাটে পাঠিয়ে দিন</b> — নাম যা-ই হোক, ভিতরটা দেখেই বুঝে নেবে।\n"
            "২️⃣ ফাইলটা আগে পাঠানো থাকলে সেটায় <b>রিপ্লাই</b> করে <code>/restore</code> "
            "লিখলেও হয়।\n"
            "৩️⃣ অথবা <code>BFIXF1:…</code> কোডগুলো (একটা/কয়েক টুকরো) পরপর পাঠান — "
            "নিজেই জোড়া লাগিয়ে নেবে।\n\n"
            "🧓 <b>পুরোনো ভার্সনের ডেটাবেজও চলবে:</b> এই বটের আগের ভার্সনের "
            "<code>.sqlite3</code>, পুরোনো <code>bot_db.json</code>, এমনকি পুরোনো "
            "আলাদা বটের <code>users.db</code> পাঠালেও বট চিনে নেয় — তখন শুধু "
            "<b>যোগ</b> হয়, আপনার বর্তমান ডেটা মুছে যায় না।\n\n"
            "তারপর বট দেখাবে ব্যাকআপে কত ইউজার/স্টোর/ফাইল আছে; <b>✅ ফিরিয়ে আনুন</b> "
            "চাপলেই পুরো ইনস্টল আগের মতো। আগের অবস্থার সেফটি কপি নিজে থেকেই সেভ হয়।")


@bot.on(events.NewMessage(func=lambda e: _is_admin_private(e)
                          and (e.raw_text or "").strip().lower().startswith("/restore")))
async def restore_command(event) -> None:
    """`/restore` — alone, with a document, or as a reply to one."""
    admin_id = event.sender_id
    clear_flow(admin_id)                 # never mix this with another flow
    document = None
    reply = getattr(event, "reply_to_msg", None)
    for candidate in (getattr(event, "message", None) and getattr(event.message, "document", None),
                      getattr(reply, "document", None) if reply else None):
        if candidate is not None:
            document = candidate
            break
    if document is not None:
        try:
            data = bytes(await _client(event).download_media(event.message, file=bytes))
        except Exception as exc:
            await event.respond(f"❌ ফাইলটা নামানো যাচ্ছে না: {esc(str(exc))}")
            raise events.StopPropagation
        await _offer(event, admin_id, data, "ফাইল")
        raise events.StopPropagation
    from app.handlers.state import ask
    ask(admin_id, "restore_file")
    await event.respond(_restore_help())
    raise events.StopPropagation


@bot.on(events.NewMessage(func=lambda e: _is_admin_private(e)
                          and e.message is not None and e.message.document is not None))
async def restore_document(event) -> None:
    """A document from an admin: a backup if we asked for one (or its name says so)."""
    admin_id = event.sender_id
    question = peek(admin_id)
    name = (getattr(event.message.file, "name", "") or "").lower()
    wanted = bool(question and question.get("action") == "restore_file")
    named = name.endswith(portable.SUFFIX) or name.endswith(
        (".sqlite3", ".db", ".bfixf", ".gz"))
    maybe_json = name.endswith(".json")            # পুরোনো bot_db.json / আমাদের এক্সপোর্ট
    if not (wanted or named or maybe_json):
        return                                    # normal uploads go to their own flow
    try:
        data = bytes(await _client(event).download_media(event.message, file=bytes))
    except Exception as exc:
        await event.respond(f"❌ ফাইলটা নামানো যাচ্ছে না: {esc(str(exc))}")
        raise events.StopPropagation
    if maybe_json and not (wanted or named):
        try:
            portable.inspect(data)
        except portable.BackupError:
            return                                # সাধারণ json → নিজের ফ্লোতে যাক
    take(admin_id)
    await _offer(event, admin_id, data, name or "ফাইল")
    raise events.StopPropagation


@bot.on(events.NewMessage(func=lambda e: _is_admin_private(e)
                          and (e.raw_text or "").strip().startswith(portable.TEXT_PREFIX)))
async def restore_text(event) -> None:
    """Pasted `BFIXF1:` code — one message or numbered pieces."""
    admin_id = event.sender_id
    text = (event.raw_text or "").strip()
    parts = _parts.setdefault(admin_id, {})
    total = 0
    for piece in text.split(portable.TEXT_PREFIX)[1:]:
        marker = piece.split(":", 1)[0] if ":" in piece else ""
        if "/" in marker and marker.replace("/", "").isdigit():
            index, count = (int(x) for x in marker.split("/", 1))
            total = max(total, count)
            parts[index] = piece.split(":", 1)[1]
        else:
            parts[1] = piece
            total = max(total, 1)
    have = len(parts)
    if total > have:
        await event.respond(f"🧩 কোডের টুকরো পেয়েছি: <b>{have}/{total}</b> — "
                            "বাকিগুলোও পাঠান।")
        raise events.StopPropagation
    blob = "\n".join(f"{portable.TEXT_PREFIX}{index}/{total}:{parts[index]}"
                     for index in sorted(parts))
    _parts.pop(admin_id, None)
    try:
        data = portable.decode_text(blob)
    except portable.BackupError as exc:
        _staged.pop(admin_id, None)
        await event.respond(f"❌ {esc(str(exc))}")
        raise events.StopPropagation
    _staged[admin_id] = data
    await _offer(event, admin_id, data, "পেস্ট করা কোড")
    raise events.StopPropagation


@route("bk:")
async def restore_confirm(event, rest: str) -> None:
    """The ✅ / ✖️ buttons under the backup preview."""
    admin_id = event.sender_id
    if rest == "no":
        _pending.pop(admin_id, None)
        _staged.pop(admin_id, None)
        await event.answer("বাতিল করা হলো", alert=False)
        await event.respond("✖️ রিস্টোর বাতিল — ডেটা আগের মতোই আছে।")
        return
    data = _pending.pop(admin_id, None) or _staged.pop(admin_id, None)
    if not data:
        await event.answer("কোনো ব্যাকআপ অপেক্ষায় নেই", alert=True)
        return
    await event.answer("কাজ শুরু হচ্ছে…")
    try:
        report = portable.apply(data, source="telegram")
    except portable.BackupError as exc:
        await event.respond(f"❌ রিস্টোর হয়নি: {esc(str(exc))}")
        return
    except Exception as exc:                                  # pragma: no cover - defensive
        log.exception("telegram restore failed")
        await event.respond(f"❌ রিস্টোর ব্যর্থ: {esc(str(exc))}")
        return
    try:
        from app.services import audit
        audit.record("restore_done", ref="telegram",
                     detail=str(report.get("counts_after", {}))[:250])
    except Exception:
        pass
    # Credentials may have arrived with the database → let the bot pick them up.
    try:
        from app.services import credentials
        credentials.load()
        credentials.poke()
    except Exception:
        log.debug("credential reload after restore failed", exc_info=True)
    after = report.get("counts_after") or {}
    before = report.get("counts_before") or {}
    if report.get("merge"):
        bits = "\n".join(f"• {esc(part)}" for part in portable.import_summary(report))
        await event.respond(
            "✅ <b>যোগ করা হয়েছে</b> — বর্তমান ডেটা অক্ষত\n\n"
            + (bits or "• সবই আগে থেকে ছিল")
            + f"\n\n📊 এখন: {after.get('users', 0)} ইউজার, "
              f"{after.get('stores', 0)} স্টোর, {after.get('files', 0)} ফাইল"
            + "\nℹ️ পুরোনো ইনস্টলের ইউজারদের অ্যাক্সেস-হ্যাশ থাকে না — তারা আবার "
              "<code>/start</code> দিলে (বা ব্রডকাস্টের সময়) সেটা নিজে থেকেই "
              "শিখে নেওয়া হয়।")
        return
    await event.respond(
        "✅ <b>ইনস্টল ফিরিয়ে আনা হয়েছে</b>\n\n"
        + "\n".join(f"• {label}: {before.get(key, 0)} → <b>{after.get(key, 0)}</b>"
                    for key, label in (("users", "ইউজার"), ("stores", "স্টোর"),
                                       ("files", "ফাইল"), ("grants", "অ্যাক্সেস"),
                                       ("orders", "অর্ডার")))
        + (f"\n🔑 টোকেন ফিরে এসেছে — বট নিজেই চলবে।"
           if report.get("credentials_included") else "")
        + f"\n🛟 রিস্টোরের আগের অবস্থা সেভ আছে:\n<code>{esc(report.get('safety_copy') or '—')}</code>"
        + "\n\nএখন দেখে নিন: <code>/health</code> · <code>/admin/self-test</code> · "
          "<code>/brwho</code>")


@bot.on(events.NewMessage(func=lambda e: _is_admin_private(e)
                          and (e.raw_text or "").strip().lower().startswith("/backup")))
async def backup_command(event) -> None:
    """`/backup` — one file (or a paste code) that holds the whole install."""
    admin_id = event.sender_id
    status = await event.respond("⏳ পুরো ইনস্টলের ব্যাকআপ বানানো হচ্ছে…")
    try:
        result = await portable.send_backup(admin_id)
    except Exception as exc:
        log.exception("portable backup failed")
        await status.edit(f"❌ ব্যাকআপ ব্যর্থ: {esc(str(exc))}")
        raise events.StopPropagation
    codes = result.get("codes") or []
    lines = [f"✅ ব্যাকআপ পাঠানো হয়েছে ({result['bytes'] // 1024} KB)"]
    if len(codes) > 1:
        lines.append(f"🧩 ফাইল না নামিয়ে নিতে চাইলে {len(codes)} টুকরো কোড আছে — "
                     "সেগুলো <code>/restore</code>-এর পরে পরপর পেস্ট করলেই হবে।")
    counts = result.get("counts") or {}
    lines.append("📦 ভিতরে: " + ", ".join(f"{key} {value}" for key, value in counts.items()
                                           if key in ("users", "stores", "files")))
    try:
        await status.edit("\n".join(lines))
    except Exception:
        await event.respond("\n".join(lines))
    raise events.StopPropagation
