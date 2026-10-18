"""`/br` — the broadcast studio that lives inside Telegram.

A complete flow, but still three taps for the simple case:

```
/br                      → studio opens (a saved draft is restored automatically)
[video / photo]          → attached (up to 10 — they go out as one album)
text (HTML allowed)      → the message
Name | https://link      → inline buttons, one per line
🚀 Send now              → everyone gets it, live progress, ⏹ stop any time
```

Everything is written to the database on every step (`br_drafts`), the send runs
through the persistent campaign engine (`app/services/broadcast.py`) and the
recipient is addressed by the cached `(user_id, access_hash)` pair
(`app/services/peers.py`) — so restarts, fresh sessions and hosting panels can no
longer break a broadcast.

Commands: `/brsend` `/brtest` `/brpreview` `/braudience` `/brsilent` `/brprotect`
`/brschedule` `/brstatus` `/brclear` `/brcancel` `/brhelp`.
"""
from __future__ import annotations

import time

from telethon import Button, events

from app import config as cfg, runtime
from app.storage import db
from app.logger import log
from app.runtime import bot
from app.handlers.router import route
from app.handlers.state import ask, peek, take
from app.services import access, broadcast, quickcast
from app.services.quickcast import Content
from app.utils import esc

SESSION_TTL = 3600                        # a forgotten studio closes after an hour
sessions: dict[int, float] = {}           # admin_id -> last activity

INBOX_HELP = (
    "📥 <b>ব্রডকাস্ট মিডিয়া ইনবক্স</b>\n\n"
    "স্টোরে না থাকা ভিডিও/ছবি (ব্যানার, প্রোমো, অ্যানাউন্সমেন্ট) এখানে রেখে দিন — "
    "বটের <b>/br</b> এবং <b>ওয়েবসাইটের ব্রডকাস্ট স্টুডিও</b> দুটোতেই বাছাই করা যাবে।\n\n"
    "• <code>/bm</code> — ইনবক্স খুলুন\n"
    "• <code>/bm add</code> — এরপর ভিডিও/ছবি ফরওয়ার্ড/পাঠান\n"
    "• <code>/bm list</code> — তালিকা (বাদ দেওয়া যাবে)\n"
    "• <code>/br</code>-এর ভিতরে <b>📥 ইনবক্স</b> বাটন চাপলেই সরাসরি যোগ হয়ে যায়"
)

INTRO = (
    "📣 <b>ব্রডকাস্ট স্টুডিও</b>\n\n"
    "যা পাঠাতে পারেন:\n"
    "• 🎬 <b>ভিডিও/ছবি</b> (একাধিকও — একসাথে অ্যালবাম হয়ে যাবে)\n"
    "• ✍️ <b>টেক্সট</b> (HTML চলবে: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
    "<code>&lt;a href=\"…\"&gt;</code>)\n"
    "• 🔗 <b>বাটন</b> — <code>নাম | লিংক</code>, প্রতি লাইনে একটা\n\n"
    "শেষ হলে <b>🚀 পাঠান</b> চাপুন (বা <code>/brsend</code>) — বাকিটা বট নিজেই দেখবে।"
)


# ------------------------------------------------------------------- helpers
def _is_admin_private(event) -> bool:
    return bool(event.is_private and not event.out and access.is_admin(event.sender_id))


def _open(admin_id: int) -> bool:
    """True while this admin has the studio open (auto-closed after an hour)."""
    opened = sessions.get(int(admin_id))
    if opened is None:
        return False
    if time.time() - opened > SESSION_TTL:
        sessions.pop(int(admin_id), None)
        return False
    return True


def _touch(admin_id: int) -> None:
    sessions[int(admin_id)] = time.time()


def _close(admin_id: int) -> None:
    sessions.pop(int(admin_id), None)


def _audience_buttons(content: Content, admin_id: int) -> list[list[Button]]:
    rows = [[Button.inline(("✅ " if content.audience == "all" else "") + "📣 সবাই",
                           "br:aud:all"),
             Button.inline(("✅ " if content.audience == "premium" else "") + "💎 প্রিমিয়াম",
                           "br:aud:premium"),
             Button.inline(("✅ " if content.audience == "free" else "") + "🆓 ফ্রি",
                           "br:aud:free")]]
    for store in db_stores(admin_id)[:8]:
        code = f"store:{store['id']}"
        mark = "✅ " if content.audience == code else ""
        rows.append([Button.inline(f"{mark}🏪 {store['name'][:34]}", f"br:aud:{code}")])
    rows.append([Button.inline("⬅️ ফিরে যান", "br:home")])
    return rows


def db_stores(admin_id: int):
    return db.stores_admin(admin_id)


def _keyboard(content: Content, admin_id: int) -> list[list[Button]]:
    aud_label = quickcast.audience_label(content.audience)
    rows = [
        [Button.inline(f"🎯 {aud_label[:30]}", "br:audmenu")],
        [Button.inline("📥 ইনবক্স থেকে মিডিয়া", "br:inbox")],
        [Button.inline("🔕 সাইলেন্ট" if not content.silent else "🔔 সাইলেন্ট বন্ধ",
                       "br:silent"),
         Button.inline("↗️ ফরওয়ার্ড চালু" if content.protect else "🔒 ফরওয়ার্ড বন্ধ",
                       "br:protect")],
        [Button.inline("👁 প্রিভিউ", "br:preview"),
         Button.inline(f"🧪 টেস্ট ({len(cfg.ADMIN_IDS)} জন)", "br:test")],
        [Button.inline("🧭 কারা পাবে", "br:who"),
         Button.inline("📊 কেন মিস", "br:why")],
        [Button.inline("🚀 পাঠান", "br:send"),
         Button.inline("🧹 খালি করুন", "br:clear")],
        [Button.inline("❌ বন্ধ", "br:close")],
    ]
    if content.media:
        rows.insert(3, [Button.inline(f"🖼 মিডিয়া ({len(content.media)}টি) — একটা বাদ দিন",
                                      "br:media")])
    return rows


def _card(content: Content, admin_id: int, note: str = "") -> str:
    targets = quickcast.target_count(content, admin_id)
    reach = quickcast.dashboard()["peers"]
    lines = [note] if note else []
    lines.append("📣 <b>ব্রডকাস্ট স্টুডিও</b>")
    lines.append("")
    lines.append(f"🧩 <b>কনটেন্ট:</b> {content.summary()}")
    if content.text:
        plain = content.text if len(content.text) < 500 else content.text[:500] + "…"
        lines.append(f"\n<blockquote>{plain}</blockquote>")
    lines.append("")
    lines.append(f"🎯 অডিয়েন্স: <b>{quickcast.audience_label(content.audience)}</b> "
                 f"(~{targets} জন)")
    lines.append(f"📡 পৌঁছানো যাবে: <b>{reach['reachable']}</b>/{reach['total']} জন "
                 f"(বাকিরা বটে /start দেয়নি)")
    if not content.is_empty():
        lines.append("\nপাঠাতে তৈরি — <b>🚀 পাঠান</b> চাপুন।")
    waiting = [c for c in db.campaigns(limit=20)
               if c["status"] in (broadcast.SCHEDULED,) and (c.get("scheduled_at") or 0) > time.time()]
    if waiting:
        lines.append(f"🗓 অপেক্ষায় {len(waiting)}টি শিডিউল — দেখতে <code>/brqueue</code>")
    lines.append("\n<i>ভিডিও/ছবি, টেক্সট বা বাটন পাঠিয়ে আরও যোগ করুন।</i>")
    return "\n".join(lines)


async def _show(event, content: Content, admin_id: int, note: str = "") -> None:
    await event.respond(_card(content, admin_id, note), buttons=_keyboard(content, admin_id))


# ------------------------------------------------------------------ commands
@bot.on(events.NewMessage(func=_is_admin_private, pattern=r"(?i)^/br(@\w+)?\s*$"))
async def br_start(event) -> None:
    admin_id = event.sender_id
    _touch(admin_id)
    draft = quickcast.load_draft(admin_id)
    if draft is not None and not draft.is_empty():
        await _show(event, draft, admin_id,
                    "♻️ <b>সেভ করা খসড়া ফিরে এসেছে</b> — আগের কনটেন্ট নিচে আছে।")
    else:
        await event.respond(INTRO)
        await _show(event, draft or Content(), admin_id)
    raise events.StopPropagation


@bot.on(events.NewMessage(func=_is_admin_private,
                          pattern=r"(?i)^/br(send|test|preview|status|clear|cancel|help|"
                                  r"audience|silent|protect|schedule|queue|who|why|retry|"
                                  r"cancelqueue|slow|items|drop|csv|report|share)"
                                  r"(@\w+)?(\s+[\s\S]*)?$"))
async def br_command(event) -> None:
    admin_id = event.sender_id
    command = (event.pattern_match.group(1) or "").lower()
    rest = (event.pattern_match.group(3) or "").strip()
    _touch(admin_id)
    content = quickcast.load_draft(admin_id) or Content()

    if command == "help":
        await event.respond(
            "📣 <b>/br হেল্প</b>\n\n"
            "<code>/br</code> — স্টুডিও খুলুন (খসড়া ফিরে আসে)\n"
            "<code>/brsend</code> — এখনই সবার কাছে পাঠান\n"
            "<code>/brtest</code> — শুধু অ্যাডমিনদের কাছে টেস্ট\n"
            "<code>/brpreview</code> — ইউজার যা দেখবে\n"
            "<code>/braudience all|premium|free|store:12</code> — অডিয়েন্স বদলান\n"
            "<code>/brsilent on|off</code> · <code>/brprotect on|off</code>\n"
            "<code>/brschedule 30</code> — ৩০ মিনিট পরে (মিনিটে)\n"
            "<code>/brslow 30</code> — পুরো তালিকা ৩০ মিনিট ধরে ধীরে ধীরে (ফ্লাড-নিরাপদ)\n"
            "<code>/brstatus</code> — চলতি/শেষ হওয়া ক্যাম্পেইন\n"
            "<code>/brwho</code> — কারা পাবে (নাম সহ)\n"
            "<code>/brwhy</code> — কারা কেন পায়নি, এক নজরে\n"
            "<code>/brretry</code> — শেষ ক্যাম্পেইনের ব্যর্থদের আবার পাঠান\n"
            "<code>/brqueue</code> — শিডিউল করা / অপেক্ষায় থাকা ব্রডকাস্ট\n"
            "<code>/brcancelqueue 12</code> — অপেক্ষায় থাকা #১২ বাতিল\n"
            "<code>/britems</code> — খসড়ার সব মেসেজ এক নজরে (ক্রম সহ)\n"
            "<code>/brdrop</code> — শেষ যোগ করা মেসেজটা বাদ দিন\n"
            "<code>/brcsv</code> — কে পেয়েছে/পায়নি তার CSV রিপোর্ট\n"
            "<code>/brshare</code> — খসড়ার শেয়ার লিংক\n"
            "<code>/brclear</code> — খসড়া মুছুন · <code>/brcancel</code> — বন্ধ করুন\n\n"
            "<b>বহু-মেসেজ ব্রডকাস্ট (এই স্টুডিওর নতুন ক্ষমতা):</b> স্টুডিও খোলা থাকলে যা খুশি "
            "পাঠাতে থাকুন — টেক্সট, ছবি, ভিডিও, GIF, ফাইল, অডিও, ভয়েস। প্রতিটা আলাদা "
            "মেসেজ হিসেবে <b>এই ক্রমেই</b> সব ইউজারের কাছে যাবে (➕ Added #ন)। "
            "বাটন বসে শেষ মেসেজে (<code>/admin/settings</code>-এ “প্রতিটি মেসেজে”ও করা যায়)।\n"
            "একসাথে ১০টা পর্যন্ত ভিডিও/ছবি দিলে অ্যালবামও হয়ে যায়; "
            "বাটন: <code>নাম | লিংক</code> লাইনে লাইনে।")
        raise events.StopPropagation

    if command == "clear":
        quickcast.drop_draft(admin_id)
        await event.respond("🧹 খসড়া মুছে ফেলা হয়েছে। নতুন করে শুরু করুন: /br")
        raise events.StopPropagation

    if command == "cancel":
        _close(admin_id)
        await event.respond("❌ স্টুডিও বন্ধ করা হলো (খসড়া সেভ আছে — /br দিলে ফিরে আসবে)।")
        raise events.StopPropagation

    if command == "audience":
        if not rest:
            await event.respond(f"এখন: <b>{quickcast.audience_label(content.audience)}</b>\n"
                                "বদলাতে: <code>/braudience all</code> · "
                                "<code>premium</code> · <code>free</code> · "
                                "<code>store:12</code>")
        else:
            content.audience = rest.strip()
            quickcast.save_draft(admin_id, content)
            await event.respond(f"🎯 অডিয়েন্স: <b>{quickcast.audience_label(content.audience)}</b> "
                                f"(~{quickcast.target_count(content, admin_id)} জন)")
        raise events.StopPropagation

    if command in ("silent", "protect"):
        value = rest.lower() in ("on", "1", "true", "yes", "চালু")
        if command == "silent":
            content.silent = value
        else:
            content.protect = value
        quickcast.save_draft(admin_id, content)
        await event.respond(f"{'🔕' if command == 'silent' else '🔒'} {command}: "
                            f"<b>{'চালু' if value else 'বন্ধ'}</b>")
        raise events.StopPropagation

    if command == "schedule":
        minutes = 0
        try:
            minutes = int(rest.split()[0])
        except Exception:
            minutes = 0
        if minutes <= 0:
            await event.respond("কত মিনিট পরে? যেমন: <code>/brschedule 30</code>")
            raise events.StopPropagation
        await _send(event, content, admin_id, when=time.time() + minutes * 60)
        raise events.StopPropagation

    if command == "slow":
        minutes = 0.0
        try:
            minutes = float(rest.split()[0])
        except Exception:
            minutes = 0.0
        if minutes <= 0:
            await event.respond("কত মিনিট ধরে পাঠাবে? যেমন: <code>/brslow 30</code>\n"
                                "(বড় তালিকায় Telegram নিজেই ধীরে পাঠাতে বলে — ~৩০ মেসেজ/সেকেন্ড সীমা)")
            raise events.StopPropagation
        await _send(event, content, admin_id, slow_minutes=minutes)
        raise events.StopPropagation

    if command == "preview":
        await event.respond("👁 <b>ইউজার যা দেখবে</b>\n\n" + (quickcast.preview(content) or "—")
                            + (f"\n\n🔗 বাটন: {content.button_lines()}" if content.buttons else ""))
        raise events.StopPropagation

    if command == "status":
        await _status(event, admin_id)
        raise events.StopPropagation

    if command == "who":
        await event.respond(_who_text(content, admin_id), buttons=_keyboard(content, admin_id))
        raise events.StopPropagation

    if command == "why":
        await event.respond(_why_text(rest), buttons=[[Button.inline("🔁 ব্যর্থদের আবার পাঠান",
                                                                    "br:retrylast")]])
        raise events.StopPropagation

    if command == "retry":
        await _retry_last(event)
        raise events.StopPropagation

    if command == "queue":
        await event.respond(_queue_text(), buttons=_queue_buttons())
        raise events.StopPropagation

    if command == "cancelqueue":
        await _cancel_queued(event, rest)
        raise events.StopPropagation

    if command == "items":
        lines = quickcast.item_lines(content)
        if not lines:
            await event.respond(
                "📭 এখনো কিছু যোগ করা হয়নি। স্টুডিও খোলা থাকলে যা খুশি পাঠান — "
                "টেক্সট, ছবি, ভিডিও, GIF, ফাইল, অডিও, ভয়েস — প্রতিটা আলাদা মেসেজ হয়ে যাবে।\n\n"
                "যেমন:\n1️⃣ প্রথম মেসেজ লিখুন\n2️⃣ তারপর ভিডিও দিন\n3️⃣ শেষে বাটন: "
                "<code>নাম | লিংক</code>\nতারপর <code>/brsend</code>",
                buttons=_keyboard(content, admin_id))
            raise events.StopPropagation
        await event.respond("📋 <b>খসড়ার মেসেজগুলো (এই ক্রমেই যাবে)</b>\n\n"
                            + "\n".join(lines)
                            + f"\n\n<b>মোট {len(content.items)}টি মেসেজ</b> — "
                              "<code>/brdrop</code> দিয়ে শেষটা বাদ দিন, "
                              "<code>/brsend</code> দিয়ে পাঠান।",
                            buttons=_keyboard(content, admin_id))
        raise events.StopPropagation

    if command == "drop":
        dropped = quickcast.drop_item(content)
        quickcast.save_draft(admin_id, content)
        if dropped is None:
            await event.respond("বাদ দেওয়ার মতো কিছু নেই।")
        else:
            await event.respond(f"🗑 <b>{dropped.kind}</b> বাদ দেওয়া হলো — এখন বাকি "
                                f"<b>{len(content.items)}</b>টি মেসেজ।",
                                buttons=_keyboard(content, admin_id))
        raise events.StopPropagation

    if command in ("csv", "report"):
        campaign_id = _last_campaign_id(rest)
        if not campaign_id:
            await event.respond("কোনো ক্যাম্পেইন পাওয়া যায়নি — আগে <code>/brsend</code> করুন।")
            raise events.StopPropagation
        from app.services import broadcast
        outcome = await broadcast.send_report(admin_id, campaign_id)
        if not outcome.get("delivered"):
            await event.respond("❌ রিপোর্ট পাঠানো গেল না: "
                                f"<code>{esc(str(outcome.get('error') or 'unknown'))}</code>")
        else:
            lines = broadcast.failure_lines(campaign_id, limit=8)
            if lines:
                await event.respond("❌ <b>যারা পায়নি</b>\n" + "\n".join(lines))
        raise events.StopPropagation

    if command == "share":
        campaign_id = _last_campaign_id(rest)
        if not campaign_id:
            newest = db.campaigns(limit=1)
            campaign_id = int(newest[0]["id"]) if newest else 0
        token = quickcast.share_draft(campaign_id) if campaign_id else 0
        if not token:
            await event.respond("🔗 শেয়ার লিংক বানাতে আগে একটা খসড়া সেভ করুন "
                                "(<code>/brsend</code> বা প্যানেল থেকে খসড়া রাখুন)।")
            raise events.StopPropagation
        from app.services.basepath import link as base_link
        url = base_link(f"draft/{token}")
        await event.respond(f"🔗 <b>খসড়ার লিংক</b> — যে কেউ দেখতে পারবে (পাঠানো যাবে না):\n"
                            f"<code>{esc(url)}</code>")
        raise events.StopPropagation

    if command in ("send", "test"):
        await _send(event, content, admin_id, test=(command == "test"))
        raise events.StopPropagation


@bot.on(events.NewMessage(func=lambda e: _is_admin_private(e) and _open(e.sender_id)))
async def br_collect(event) -> None:
    """Everything the admin drops while the studio is open: media, text, buttons.

    Ported from the standalone `broadcast.py`: *any* message becomes one item of the
    draft — text, photo, video, GIF, file, audio, voice — and each item is later sent
    as its own Telegram message, in the same order (“➕ Added #3” there, here too).
    """
    admin_id = event.sender_id
    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        return                                  # any other command works as usual
    _touch(admin_id)
    content = quickcast.load_draft(admin_id) or Content()

    # `নাম | লিংক` lines are buttons, not content (kept from the studio flow).
    if text and quickcast.looks_like_buttons(text):
        what = quickcast.add_text(content, text, event.message.entities)
        quickcast.save_draft(admin_id, content)
        await event.respond("🔗 বাটন যোগ হলো — " + content.summary(),
                            buttons=_keyboard(content, admin_id))
        raise events.StopPropagation

    item = quickcast.add_item(content, event.message, text=text,
                              entities=getattr(event.message, "entities", None))
    quickcast.save_draft(admin_id, content)
    if item is not None:
        note = f"➕ যোগ হলো #{len(content.items)} — {item.icon()} <b>{item.kind}</b>"
        if item.kind == "text":
            note += " (আলাদা মেসেজ হিসেবে যাবে)"
        await event.respond(f"{note}\n{content.summary()}",
                            buttons=_keyboard(content, admin_id))
        raise events.StopPropagation
    raise events.StopPropagation


# ---------------------------------------------------------------------- send
async def _send(event, content: Content, admin_id: int, test: bool = False,
                when: float | None = None, slow_minutes: float | None = None) -> None:
    from app.services.sender import edit_text

    if content.is_empty():
        await event.respond("কিছু পাঠাননি — আগে ভিডিও/ছবি বা টেক্সট দিন।",
                            buttons=_keyboard(content, admin_id))
        return
    targets = quickcast.target_count(content, admin_id)
    if not test and not targets:
        await event.respond(f"🎯 <b>{quickcast.audience_label(content.audience)}</b>-এ কেউ নেই। "
                            "অডিয়েন্স বদলে দেখুন: /braudience")
        return
    if test and not cfg.ADMIN_IDS:
        await event.respond("⚠️ ADMIN_IDS সেট নেই — টেস্ট পাঠানো যাবে না।")
        return

    scheduled = bool(when and when > time.time() + 5)
    if slow_minutes:
        pause = quickcast.slow_pause(targets, slow_minutes)
        eta = int(targets * pause / 60) + 1
        await event.respond(f"🐢 <b>ধীর মোড</b> — {int(slow_minutes)} মিনিটের লক্ষ্য, "
                            f"প্রতি মেসেজের পরে {pause:.2f} সেকেন্ড (আসলে ~{eta} মিনিট লাগবে)।")
    status = await event.respond(
        (f"🗓 {int((when - time.time()) / 60)} মিনিট পরে পাঠানো হবে…" if scheduled else
         ("🧪 অ্যাডমিনদের কাছে টেস্ট পাঠানো হচ্ছে…" if test else
          f"🚀 <b>{targets}</b> জনের কাছে পাঠানো শুরু হচ্ছে…")))

    progress_log = {"at": 0.0}

    async def progress(done: int, total: int, failed: int) -> None:
        now = time.time()
        if done < total and now - progress_log["at"] < 2.5:
            return
        progress_log["at"] = now
        percent = int(done * 100 / (total or 1))
        bar = "█" * int(percent / 10) + "░" * (10 - int(percent / 10))
        await edit_text(
            status,
            f"🚀 পাঠানো হচ্ছে…\n<code>{bar}</code> {percent}%\n\n"
            f"✅ গেছে: <b>{done - failed}</b> · ⚠️ ব্যর্থ: {failed} · 🎯 মোট: {total}",
            buttons=[[Button.inline("⏹ থামান", "br:stop")]])

    result = await quickcast.send_all(
        admin_id, content, audience=content.audience, progress=progress,
        test=[admin_id] if test else None, scheduled_at=when,
        slow_minutes=slow_minutes)
    campaign_id = result.get("campaign_id")

    if scheduled:
        await edit_text(status, f"🗓 ক্যাম্পেইন #{campaign_id} শিডিউল করা হলো — "
                                f"{int((when - time.time()) / 60)} মিনিট পরে যাবে।")
        return
    if test:
        ok = result.get("sent", 0) > 0
        await edit_text(status, "🧪 টেস্ট সফল — এভাবেই সবার কাছে যাবে ✅" if ok else
                        f"❌ টেস্ট ব্যর্থ: {result.get('error') or 'কারণ অজানা'}")
        return

    reasons = broadcast.failure_breakdown(campaign_id) if campaign_id else []
    why = ""
    if reasons:
        why = "\n\n<b>কেন সবাই পায়নি:</b>\n" + "\n".join(
            f"• {group['hint'][:110]} — {group['count']} জন" for group in reasons[:3])
    await edit_text(
        status,
        f"✅ <b>ব্রডকাস্ট শেষ</b> — {result.get('sent', 0)} জনের কাছে গেছে\n"
        f"⚠️ ব্যর্থ: {result.get('failed', 0)} · 🚫 ব্লকড: {result.get('blocked', 0)}\n"
        f"⏱ {result.get('seconds', 0):.1f} সেকেন্ড · ক্যাম্পেইন #{campaign_id}{why}",
        buttons=[[Button.inline("🔁 ব্যর্থদের আবার পাঠান", f"br:retry:{campaign_id}"),
                  Button.inline("📊 বিস্তারিত", f"br:info:{campaign_id}")]])


# --------------------------------------------------- reports & queue helpers
def _last_campaign_id(wanted: str = "") -> int:
    """The campaign the report commands talk about — `#12` / `12` or the newest."""
    text = (wanted or "").strip().lstrip("#")
    if text:
        try:
            return int(text.split()[0])
        except ValueError:
            return 0
    rows = db.campaigns(limit=8)
    for campaign in rows:
        if campaign["status"] != broadcast.SCHEDULED:
            return int(campaign["id"])
    return int(rows[0]["id"]) if rows else 0


def _who_text(content: Content, admin_id: int, limit: int = 12) -> str:
    """“কারা পাবে” — real names, not just a number, so the admin can sanity-check."""
    from app.services.broadcast import resolve_audience
    try:
        targets = resolve_audience(admin_id, content.audience or "all")
    except Exception as exc:
        return f"⚠️ অডিয়েন্স বের করা গেল না: {exc}"
    if not targets:
        return (f"🎯 <b>{quickcast.audience_label(content.audience)}</b> — কেউ নেই।\n"
                "অডিয়েন্স বদলান: /braudience")
    lines = [f"🎯 <b>{quickcast.audience_label(content.audience)}</b> — মোট <b>{len(targets)}</b> জন",
             ""]
    for uid in targets[:limit]:
        row = db.user(uid) or {}
        name = (row.get("first_name") or row.get("name") or "").strip() or "নাম নেই"
        username = row.get("username") or ""
        mark = "🔕" if uid in runtime.blocked_users else ""
        lines.append(f"{mark}• {name[:28]}{f' (@{username})' if username else ''} · <code>{uid}</code>")
    if len(targets) > limit:
        lines.append(f"… আরও {len(targets) - limit} জন")
    reach = quickcast.dashboard()["peers"]
    lines.append("")
    lines.append(f"📡 এখনই পৌঁছানো যাবে: <b>{reach['reachable']}</b>/{reach['total']} জন "
                 f"(বাকিরা বটে /start দেয়নি)")
    return "\n".join(lines)


def _why_text(wanted: str = "") -> str:
    campaign_id = _last_campaign_id(wanted)
    if not campaign_id:
        return "📭 এখনো কোনো ক্যাম্পেইন নেই।"
    counts = db.campaign_counts(campaign_id, sync=False)
    reasons = broadcast.failure_breakdown(campaign_id)
    head = (f"📊 <b>ক্যাম্পেইন #{campaign_id}</b>\n✅ গেছে {counts['sent']} · "
            f"⚠️ ব্যর্থ {counts['failed']} · 🚫 ব্লকড {counts['blocked']} · "
            f"⏳ বাকি {counts['pending']}\n")
    if not reasons:
        return head + "\n🎉 সবাই পেয়েছে — কিছু বাকি নেই।"
    body = "\n".join(f"• {group['hint'][:130]}\n   {group['count']} জন — "
                      f"{', '.join(str(u) for u in group['users'][:6])}"
                      f"{'…' if group['count'] > 6 else ''}" for group in reasons[:5])
    return head + "\n<b>কেন পায়নি:</b>\n" + body + \
        "\n\n<i>বটে /start দিলে বা ব্লক তুলে নিলে 🔁 দিয়ে আবার পাঠান।</i>"


async def _retry_last(event, campaign_id: int = 0) -> None:
    campaign_id = campaign_id or _last_campaign_id()
    if not campaign_id:
        await event.respond("📭 আবার পাঠানোর মতো ক্যাম্পেইন নেই।")
        return
    rearmed = db.rearm_campaign_items(campaign_id, ("failed",))
    if not rearmed:
        await event.respond(f"✅ ক্যাম্পেইন #{campaign_id}-এ ব্যর্থ কেউ নেই।")
        return
    db.update_campaign(campaign_id, status=broadcast.QUEUED, finished_at=None, last_error="")
    broadcast.forget_payload(campaign_id)
    broadcast.start_campaign(campaign_id)
    await event.respond(f"🔁 ক্যাম্পেইন #{campaign_id}: <b>{rearmed}</b> জনকে আবার পাঠানো হচ্ছে…",
                        buttons=[[Button.inline("📊 বিস্তারিত", f"br:info:{campaign_id}")]])


def _queue_text() -> str:
    rows = [c for c in db.campaigns(limit=30)
            if c["status"] in (broadcast.SCHEDULED, broadcast.QUEUED, broadcast.RUNNING)]
    if not rows:
        return "📭 অপেক্ষায় বা শিডিউলে কোনো ব্রডকাস্ট নেই।"
    lines = ["🗓 <b>অপেক্ষায় থাকা ব্রডকাস্ট</b>", ""]
    for campaign in rows:
        when = float(campaign.get("scheduled_at") or 0)
        left = f"{max(0, int((when - time.time()) / 60))} মিনিট পরে" if when > time.time() else ""
        counts = db.campaign_counts(campaign["id"], sync=False)
        lines.append(f"#{campaign['id']} · <b>{campaign['status']}</b> {left}\n"
                     f"   {quickcast.audience_label(campaign.get('audience') or 'all')} · "
                     f"⏳ {counts['pending']}/{counts['total']} · "
                     f"{(campaign.get('text') or '')[:48] or '—'}")
    return "\n".join(lines)


def _queue_buttons() -> list[list[Button]]:
    rows = []
    for campaign in db.campaigns(limit=6):
        if campaign["status"] in (broadcast.SCHEDULED, broadcast.QUEUED):
            rows.append([Button.inline(f"❌ #{campaign['id']} বাতিল",
                                       f"br:unsched:{campaign['id']}")])
    return rows


async def _cancel_queued(event, rest: str) -> None:
    campaign_id = _last_campaign_id(rest)
    if not campaign_id:
        await event.respond("কোন ক্যাম্পেইন? যেমন: <code>/brcancelqueue 12</code>")
        return
    campaign = db.campaign(campaign_id)
    if not campaign:
        await event.respond(f"❌ ক্যাম্পেইন #{campaign_id} নেই।")
        return
    if campaign["status"] not in (broadcast.SCHEDULED, broadcast.QUEUED):
        await event.respond(f"⚠️ #{campaign_id} এখন {campaign['status']} — বাতিল করা যায় না।")
        return
    broadcast.cancel_campaign(campaign_id)
    await event.respond(f"❌ ক্যাম্পেইন #{campaign_id} বাতিল করা হলো।")

async def _status(event, admin_id: int) -> None:
    campaigns = db.campaigns(limit=6)
    if not campaigns:
        await event.respond("📭 এখনো কোনো ক্যাম্পেইন নেই। /br দিয়ে শুরু করুন।")
        return
    lines = ["📊 <b>সর্বশেষ ক্যাম্পেইন</b>", ""]
    for campaign in campaigns:
        counts = db.campaign_counts(campaign["id"], sync=False)
        lines.append(
            f"#{campaign['id']} · <b>{campaign['status']}</b> · "
            f"✅ {counts['sent']} ⚠️ {counts['failed']} 🚫 {counts['blocked']} "
            f"⏳ {counts['pending']}/{counts['total']}\n"
            f"   {quickcast.audience_label(campaign.get('audience') or 'all')} · "
            f"{(campaign.get('text') or '')[:60] or '—'}")
    buttons = [[Button.inline(f"#{c['id']} বিস্তারিত", f"br:info:{c['id']}")] for c in campaigns[:4]]
    await event.respond("\n".join(lines), buttons=buttons)
    raise events.StopPropagation


# ------------------------------------------------------- media inbox (`/bm`)
@bot.on(events.NewMessage(func=_is_admin_private, pattern=r"(?i)^/bm(@\w+)?(?:\s+([\s\S]*))?$"))
async def bm_command(event) -> None:
    admin_id = event.sender_id
    argument = (event.pattern_match.group(2) or "").strip().lower()
    if argument in ("add", "যোগ"):
        ask(admin_id, "bc_media")
        await event.respond("📥 ভিডিও/ছবি পাঠান বা ফরওয়ার্ড করুন — ইনবক্সে জমা হবে।")
        raise events.StopPropagation
    if argument in ("list", "তালিকা"):
        await event.respond(_inbox_card(), buttons=_inbox_buttons())
        raise events.StopPropagation
    if not db_items():
        await event.respond(INBOX_HELP)
        ask(admin_id, "bc_media")
        await event.respond("📥 এখনই একটা ভিডিও/ছবি পাঠাতে পারেন।")
        raise events.StopPropagation
    await event.respond(_inbox_card(), buttons=_inbox_buttons())
    raise events.StopPropagation


def db_items() -> list[dict]:
    from app.storage import db
    return db.bc_media_items(limit=40)


def _inbox_card() -> str:
    from app.storage import db
    items = db.bc_media_items(limit=40)
    if not items:
        return INBOX_HELP
    lines = ["📥 <b>মিডিয়া ইনবক্স</b>", ""]
    for index, item in enumerate(items[:15], start=1):
        name = (item.get("name") or f"মিডিয়া #{item['id']}")[:44]
        size_mb = (item.get("size") or 0) / 1024 / 1024
        lines.append(f"{index}. <b>{name}</b> · {item.get('kind')}"
                     + (f" · {size_mb:.1f} MB" if size_mb else ""))
    lines.append("\n/br খুলে <b>📥 ইনবক্স থেকে মিডিয়া</b> চাপলে যেটা বাছবেন সেটাই যাবে।")
    return "\n".join(lines)


def _inbox_buttons() -> list[list[Button]]:
    from app.storage import db
    rows = []
    for item in db.bc_media_items(limit=8):
        label = (item.get("name") or f"#{item['id']}")[:26]
        rows.append([Button.inline(f"➕ {label}", f"bm:use:{item['id']}"),
                     Button.inline("🗑", f"bm:del:{item['id']}")])
    rows.append([Button.inline("📥 নতুন মিডিয়া পাঠান", "bm:add")])
    return rows


@bot.on(events.NewMessage(func=lambda e: _is_admin_private(e)
                          and (peek(e.sender_id) or {}).get("action") == "bc_media"))
async def bm_capture(event) -> None:
    if (event.raw_text or '').strip().startswith('/'):
        return  # slash commands always escape a pending media/token prompt

    admin_id = event.sender_id
    item = quickcast.add_to_inbox(event.message)
    if item is None:
        await event.respond("🎬 এটা ভিডিও/ছবি নয় — ভিডিও, ছবি বা ডকুমেন্ট পাঠান। "
                            "শেষ হলে /br দিয়ে সাজান।")
        raise events.StopPropagation
    take(admin_id)
    content = quickcast.load_draft(admin_id) or Content()
    content.media.append(item)
    del content.media[:-quickcast.MAX_MEDIA]
    quickcast.save_draft(admin_id, content)
    _touch(admin_id)
    await event.respond(f"📥 ইনবক্সে জমা হলো + খসড়ায় যোগ হলো ({len(content.media)}/"
                        f"{quickcast.MAX_MEDIA})", buttons=_keyboard(content, admin_id))
    raise events.StopPropagation


# ------------------------------------------------------------------ callbacks
def _from_callback(event) -> int:
    return int(event.sender_id)


def _edit(event, content: Content, admin_id: int, note: str = ""):
    return event.edit(_card(content, admin_id, note), buttons=_keyboard(content, admin_id))


@route("bm:")
async def bm_callback(event, rest: str) -> None:
    """Inbox buttons (kept separate so `br:` stays about the draft)."""
    admin_id = int(event.sender_id)
    if not access.is_admin(admin_id):
        await event.answer("⛔ শুধু অ্যাডমিন", alert=True)
        return
    from app.storage import db
    if rest == "add":
        ask(admin_id, "bc_media")
        await event.answer()
        await event.respond("📥 ভিডিও/ছবি পাঠান — ইনবক্সে জমা হবে।")
        return
    if rest.startswith("use:"):
        try:
            item_id = int(rest[4:])
        except ValueError:
            await event.answer("ভুল আইটেম", alert=True)
            return
        row = db.bc_media_item(item_id)
        if not row:
            await event.answer("আইটেমটি নেই", alert=True)
            return
        content = quickcast.load_draft(admin_id) or Content()
        content.media.append(quickcast.MediaItem(chat_id=row["chat_id"], msg_id=row["msg_id"],
                                                 kind=row["kind"], name=row.get("name") or ""))
        del content.media[:-quickcast.MAX_MEDIA]
        quickcast.save_draft(admin_id, content)
        db.touch_bc_media(item_id)
        await event.answer("খসড়ায় যোগ হলো")
        await event.edit(_card(content, admin_id), buttons=_keyboard(content, admin_id))
        return
    if rest.startswith("del:"):
        try:
            db.delete_bc_media(int(rest[4:]))
        except ValueError:
            pass
        await event.answer("মুছে ফেলা হলো")
        await event.edit(_inbox_card(), buttons=_inbox_buttons())
        return
    await event.answer()


@route("br:")
async def br_callback(event, rest: str) -> None:
    admin_id = _from_callback(event)
    if not access.is_admin(admin_id):
        await event.answer("⛔ শুধু অ্যাডমিন", alert=True)
        return
    from app.services.sender import edit_text

    content = quickcast.load_draft(admin_id) or Content()
    _touch(admin_id)

    if rest in ("home", ""):
        await _edit(event, content, admin_id)
        return

    if rest == "audmenu":
        await event.edit("🎯 <b>কাকে পাঠাবেন?</b>", buttons=_audience_buttons(content, admin_id))
        return

    if rest.startswith("aud:"):
        content.audience = rest[4:] or "all"
        quickcast.save_draft(admin_id, content)
        await event.answer(f"অডিয়েন্স: {quickcast.audience_label(content.audience)}")
        await _edit(event, content, admin_id)
        return

    if rest in ("silent", "protect"):
        if rest == "silent":
            content.silent = not content.silent
        else:
            content.protect = not content.protect
        quickcast.save_draft(admin_id, content)
        await event.answer("সেভ হয়েছে")
        await _edit(event, content, admin_id)
        return

    if rest == "inbox":
        items = db.bc_media_items(limit=8)
        if not items:
            ask(admin_id, "bc_media")
            await event.answer()
            await event.respond("📥 ইনবক্স খালি — এখন একটা ভিডিও/ছবি পাঠান, জমা হয়ে যাবে।")
            return
        await event.edit("📥 <b>ইনবক্স</b> — খসড়ায় যোগ করতে ট্যাপ করুন:\n\n"
                         + _inbox_card().split("\n", 1)[-1], buttons=_inbox_buttons())
        return

    if rest == "preview":
        await event.answer()
        await event.respond("👁 <b>ইউজার যা দেখবে</b>\n\n" + (quickcast.preview(content) or "—")
                            + (f"\n\n🔗 {content.button_lines()}" if content.buttons else ""))
        return

    if rest == "who":
        await event.answer()
        await event.respond(_who_text(content, admin_id), buttons=_keyboard(content, admin_id))
        return

    if rest == "why":
        await event.answer()
        await event.respond(_why_text(), buttons=[[Button.inline("🔁 ব্যর্থদের আবার পাঠান",
                                                                "br:retrylast")]])
        return

    if rest == "retrylast":
        await event.answer()
        await _retry_last(event)
        return

    if rest == "queue":
        await event.answer()
        await event.respond(_queue_text(), buttons=_queue_buttons())
        return

    if rest.startswith("unsched:"):
        try:
            campaign_id = int(rest[8:])
        except ValueError:
            await event.answer("ভুল ক্যাম্পেইন", alert=True)
            return
        broadcast.cancel_campaign(campaign_id)
        await event.answer(f"❌ #{campaign_id} বাতিল", alert=True)
        await event.edit(_queue_text(), buttons=_queue_buttons())
        return

    if rest == "media":
        if not content.media:
            await event.answer("কোনো মিডিয়া যোগ করা হয়নি", alert=True)
            return
        rows = [[Button.inline(f"🗑 {index + 1}. {item.kind} {item.name[:28] or ''}".strip(),
                               f"br:del:{index}")] for index, item in enumerate(content.media)]
        rows.append([Button.inline("⬅️ ফিরে যান", "br:home")])
        await event.edit("🖼 <b>সংযুক্ত মিডিয়া</b> (বাদ দিতে ট্যাপ করুন)", buttons=rows)
        return

    if rest.startswith("del:"):
        try:
            index = int(rest[4:])
        except ValueError:
            index = -1
        if 0 <= index < len(content.media):
            removed = content.media.pop(index)
            quickcast.save_draft(admin_id, content)
            await event.answer(f"{removed.kind} বাদ দেওয়া হলো")
        await _edit(event, content, admin_id)
        return

    if rest == "clear":
        quickcast.drop_draft(admin_id)
        await event.answer("খসড়া মুছে ফেলা হলো")
        await _edit(event, Content(), admin_id, "🧹 সব খালি — নতুন করে সাজান।")
        return

    if rest == "close":
        _close(admin_id)
        await event.answer("বন্ধ করা হলো (খসড়া সেভ আছে)")
        await event.edit("📣 স্টুডিও বন্ধ। আবার খুলতে <code>/br</code>।")
        return

    if rest in ("send", "test"):
        await event.answer()
        await _send(event, content, admin_id, test=(rest == "test"))
        return

    if rest == "stop":
        running = [c for c in db.campaigns(limit=10)
                   if c["status"] in broadcast.ACTIVE_STATUSES]
        stopped = False
        for campaign in running:
            broadcast.cancel_campaign(campaign["id"])
            stopped = True
        await event.answer("⏹ থামানো হলো" if stopped else "চলতি ক্যাম্পেইন নেই",
                           alert=not stopped)
        if stopped:
            await edit_text(event.message, "⏹ ব্রডকাস্ট থামানো হলো। বাকিরা পরে "
                                           "🔁 দিয়ে আবার পাঠাতে পারবেন।")
        return

    if rest.startswith("bmuse:"):
        try:
            item_id = int(rest[6:])
        except ValueError:
            await event.answer("ভুল আইটেম", alert=True)
            return
        row = db.bc_media_item(item_id)
        if not row:
            await event.answer("আইটেমটি নেই", alert=True)
            return
        content.media.append(quickcast.MediaItem(
            chat_id=row["chat_id"], msg_id=row["msg_id"], kind=row["kind"],
            name=row.get("name") or ""))
        del content.media[:-quickcast.MAX_MEDIA]
        db.touch_bc_media(item_id)
        quickcast.save_draft(admin_id, content)
        await event.answer("খসড়ায় যোগ হলো")
        await _edit(event, content, admin_id)
        return

    if rest.startswith("retry:"):
        try:
            campaign_id = int(rest[6:])
        except ValueError:
            await event.answer("ভুল ক্যাম্পেইন", alert=True)
            return
        rearmed = db.rearm_campaign_items(campaign_id, ("failed",))
        db.update_campaign(campaign_id, status=broadcast.QUEUED, finished_at=None,
                           last_error="")
        broadcast.forget_payload(campaign_id)
        broadcast.start_campaign(campaign_id)
        await event.answer(f"🔁 {rearmed} জনকে আবার পাঠানো হচ্ছে", alert=True)
        return

    if rest.startswith("info:"):
        try:
            campaign_id = int(rest[5:])
        except ValueError:
            await event.answer("ভুল ক্যাম্পেইন", alert=True)
            return
        counts = db.campaign_counts(campaign_id, sync=False)
        reasons = broadcast.failure_breakdown(campaign_id)
        text = (f"📊 <b>ক্যাম্পেইন #{campaign_id}</b>\n\n"
                f"🎯 অডিয়েন্স: {quickcast.audience_label((db.campaign(campaign_id) or {}).get('audience') or 'all')}\n"
                f"✅ গেছে: <b>{counts['sent']}</b> · ⚠️ ব্যর্থ: {counts['failed']} · "
                f"🚫 ব্লকড: {counts['blocked']} · ⏳ বাকি: {counts['pending']}\n")
        if reasons:
            text += "\n<b>কারণ:</b>\n" + "\n".join(
                f"• {group['hint'][:110]} — {group['count']} জন" for group in reasons[:4])
        await event.answer()
        await event.respond(text, buttons=[[Button.inline("🔁 ব্যর্থদের আবার পাঠান",
                                                          f"br:retry:{campaign_id}")]])
        return

    await event.answer()


@route("hint:")
async def br_hints(event, rest: str) -> None:
    """Panel buttons that open a command flow without typing the command."""
    admin_id = int(event.sender_id)
    if not access.is_admin(admin_id):
        await event.answer("⛔ শুধু অ্যাডমিন", alert=True)
        return
    if rest == "br":
        _touch(admin_id)
        content = quickcast.load_draft(admin_id) or Content()
        await event.answer()
        await event.respond(_card(content, admin_id,
                                  "📣 <b>/br স্টুডিও এখানেই খুলে গেল</b> — "
                                  "ভিডিও/ছবি, টেক্সট বা বাটন পাঠান, শেষে 🚀 চাপুন।"
                                  if content.is_empty() else
                                  "♻️ আগের খসড়াটাই লোড হলো।"),
                            buttons=_keyboard(content, admin_id))
        return
    if rest == "bm":
        ask(admin_id, "bc_media")
        await event.answer()
        await event.respond("📥 <b>মিডিয়া ইনবক্স</b> — এখনই ভিডিও/ছবি পাঠান বা ফরওয়ার্ড করুন।"
                            "\n\n" + _inbox_card(), buttons=_inbox_buttons())
        return
    await event.answer()
