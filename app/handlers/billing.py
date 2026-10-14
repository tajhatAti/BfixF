"""Buying access: plans, payment, coupons, trials + the admin approval desk."""
from __future__ import annotations

import time

from telethon.tl.custom import Button

from app import i18n, ui
from app.handlers.router import route
from app.handlers.state import ask, end_flow, flow as remember, ctx as flow_ctx
from app.runtime import bot
from app import config as cfg
from app.services import access, billing, settings
from app.services.telegram import safe_call
from app.storage import db
from app.utils import esc, fmt_ts, safe_int


# =============================================================== user side
def paywall_buttons(event, store: dict, user_id: int) -> list[list[Button]]:
    """Clear, user-friendly paywall: unlock, trial, contact, back."""
    plans = db.plans(store["id"], only_active=True)
    cheapest = min(plans, key=lambda p: p["price"]) if plans else None
    label = i18n.t(user_id, "unlock_button")
    if cheapest:
        # Language-aware suffix
        if i18n.lang_of(user_id) == "en":
            label += f" · from {billing.price_text(cheapest['price'])}"
        else:
            label += f" · {billing.price_text(cheapest['price'])} থেকে শুরু"
    rows: list[list[Button]] = []
    # Primary action: always show unlock/buy, even if no plans (it will show contact)
    rows.append([Button.inline(f"💎 {label}", f"buy:{store['id']}")])

    # Trial if available
    if billing.trial_available(user_id, store):
        rows.append([Button.inline(
            i18n.t(user_id, "trial_button", h=billing.trial_hours()), f"bt:{store['id']}")])

    # Contact admin — inline, direct
    contact_label = i18n.t(user_id, "contact_button")
    rows.append([Button.inline(f"{contact_label} (সরাসরি কথা)", f"ct:{store['id']}")])

    # If support contact configured, add URL button
    try:
        from app.services import support as support_svc
        line = support_svc.support_line()
        if line and "@" in line:
            # Extract first @username
            import re
            m = re.search(r"@(\w+)", line)
            if m:
                rows.append([Button.url(f"💬 @{m.group(1)} এ মেসেজ করুন", f"https://t.me/{m.group(1)}")])
    except Exception:
        pass

    rows.append([Button.inline(i18n.t(user_id, "back_stores"), "bs:0")])
    return rows


@route("buy:")
async def choose_plan(event, rest: str) -> None:
    user_id = event.sender_id
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    plans = db.plans(store["id"], only_active=True)
    ctx = flow_ctx(user_id)
    percent = int(ctx.get("percent") or 0)
    coupon_code = ctx.get("coupon") or ""

    if not plans:
        await ui.render(event, i18n.t(user_id, "no_plans") + f"\n\n🏪 <b>{esc(store['name'])}</b>\n" +
                        (f"<i>{esc(store.get('description') or '')}</i>\n\n" if store.get("description") else ""),
                        [[Button.inline(i18n.t(user_id, "contact_button"), f"ct:{store['id']}"),
                          Button.inline(i18n.t(user_id, "back_stores"), "bs:0")]],
                        edit=True)
        await event.answer()
        return

    # Build header with store info + what you get
    stats = db.store_stats(store["id"])
    header = [
        f"💎 <b>{esc(store['name'])}</b> — প্ল্যান বেছে নিন",
        f"📂 {stats['files']} ফাইল · 👁 {stats['views']} ভিউ · 💎 {stats['grants']} জন মেম্বার",
    ]
    if store.get("description"):
        header.append(f"<i>{esc(store['description'][:200])}</i>")
    header.append("")
    header.append("✅ যা পাবেন:")
    header.append("• সব ফাইল আনলিমিটেড ডাউনলোড")
    header.append("• নতুন ফাইল যোগ হলে সাথে সাথে পাবেন")
    header.append("• 🔔 ডেইলি আপডেট সাবস্ক্রাইব করতে পারবেন")
    if coupon_code:
        header.append("")
        header.append(f"🎟️ কুপন <code>{esc(coupon_code)}</code> প্রয়োগ হয়েছে — {percent}% ছাড়!")
    header.append("")
    header.append("⬇️ নিচ থেকে একটি প্ল্যান বেছে নিন:")

    rows: list[list[Button]] = []
    for plan in plans:
        price = float(plan["price"])
        if percent:
            price = round(price * (100 - percent) / 100, 2)
            label = f"{plan['name']} — {billing.price_text(price)} (ছাড় {percent}%)"
        else:
            label = billing.plan_label(plan)
        rows.append([Button.inline(label, f"plan:{plan['id']}")])

    rows.append([Button.inline("🎟️ কুপন আছে? এখানে চাপ দিন", f"cu:{store['id']}")])
    rows.append([Button.inline("📞 অ্যাডমিনের সাথে কথা বলুন", f"ct:{store['id']}"),
                 Button.inline(i18n.t(user_id, "back_stores"), "bs:0")])

    await ui.render(event, "\n".join(header), rows, edit=True)
    await event.answer()


@route("plan:")
async def plan_detail(event, rest: str) -> None:
    user_id = event.sender_id
    plan = db.plan(safe_int(rest))
    if plan is None:
        await event.answer("Plan not found.", alert=True)
        return
    store = db.store(plan["store_id"])
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    remember(user_id, store_id=store["id"], plan_id=plan["id"])
    methods = billing.methods_text()
    if not methods:
        await ui.render(event,
                        i18n.t(user_id, "no_payment_methods") + f"\n\n🏪 <b>{esc(store['name'])}</b>",
                        [[Button.inline(i18n.t(user_id, "contact_button"), f"ct:{store['id']}"),
                          Button.inline("🔙 প্ল্যানে ফিরুন", f"buy:{store['id']}")]],
                        edit=True)
        await event.answer()
        return
    percent = int(flow_ctx(user_id).get("percent") or 0)
    coupon_code = flow_ctx(user_id).get("coupon") or ""
    price = float(plan["price"]) * (100 - percent) / 100 if percent else float(plan["price"])

    # Clear payment instructions
    lines = [
        f"💳 <b>{esc(plan['name'])}</b> — {billing.price_text(price)}",
        f"🏪 {esc(store['name'])} · {plan['days']} দিন",
        "",
        "💰 <b>পেমেন্ট করুন এই নাম্বারে:</b>",
        methods,
    ]
    pay_note = settings.get_str("PAY_NOTE")
    if pay_note:
        lines.append("")
        lines.append(f"📝 {esc(pay_note)}")
    if percent:
        lines.insert(0, f"🎟️ কুপন <code>{esc(coupon_code)}</code> — <b>{percent}% ছাড়</b> প্রয়োগ হয়েছে!")
        lines.insert(1, f"আগের দাম: <s>{billing.price_text(plan['price'])}</s> → এখন: <b>{billing.price_text(price)}</b>")
        lines.insert(2, "")
    lines.extend([
        "",
        "📌 <b>পেমেন্ট করার পর:</b>",
        "1️⃣ স্ক্রিনশট বা Transaction ID এই চ্যাটে পাঠান",
        "2️⃣ অ্যাডমিন ৫-১০ মিনিটের মধ্যে যাচাই করে অ্যাক্সেস চালু করে দেবে",
        "3️⃣ অ্যাক্সেস চালু হলে এখানেই নোটিফিকেশন পাবেন",
    ])

    rows = [
        [Button.inline("📸 আমি পেমেন্ট করেছি — প্রমাণ পাঠাবো", f"paid:{plan['id']}")],
        [Button.inline("📞 অ্যাডমিনের সাথে কথা বলুন", f"ct:{store['id']}"),
         Button.inline("🎟️ কুপন", f"cu:{store['id']}")],
        [Button.inline("🔙 অন্য প্ল্যান দেখুন", f"buy:{store['id']}"),
         Button.inline(i18n.t(user_id, "back_stores"), "bs:0")],
    ]
    await ui.render(event, "\n".join(lines), rows, edit=True)
    await event.answer()


@route("paid:")
async def ask_for_proof(event, rest: str) -> None:
    """The buyer taps 'I have paid' → we wait for the payment screenshot."""
    user_id = event.sender_id
    plan = db.plan(safe_int(rest))
    if plan is None:
        await event.answer("Plan not found.", alert=True)
        return
    store = db.store(plan["store_id"])
    ctx = flow_ctx(user_id)
    coupon = ctx.get("coupon")
    percent = int(ctx.get("percent") or 0)
    order = billing.create_order_from_plan(user_id, store["id"], plan, method="manual",
                                           discount_percent=percent, coupon=coupon)
    end_flow(user_id)

    if order and order["status"] == "approved":
        await ui.render(event, i18n.t(user_id, "order_approved", store=esc(store["name"]),
                                      days=order["days"] or 0),
                        [[Button.inline("🏪 স্টোর খুলুন", f"s:{store['id']}")]], edit=True)
        await event.answer()
        return

    ask(user_id, "payment_proof", order_id=order["id"], store_id=store["id"])
    await event.respond(
        f"🧾 Order <code>#{order['id']}</code> — {billing.price_text(order['amount'])}\n\n"
        "এখন <b>পেমেন্টের স্ক্রিনশট বা ট্রানজেকশন আইডি</b> পাঠান।\n"
        "<i>(/cancel লিখলে বাতিল হবে)</i>"
    )
    await event.answer()


@route("cu:")
async def coupon_ask(event, rest: str) -> None:
    user_id = event.sender_id
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    remember(user_id, store_id=store["id"])
    ask(user_id, "coupon_code", store_id=store["id"])
    await event.respond(i18n.t(user_id, "coupon_ask"))
    await event.answer()


@route("bt:")
async def start_trial(event, rest: str) -> None:
    user_id = event.sender_id
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    if not billing.grant_trial(user_id, store):
        await event.answer(i18n.t(user_id, "trial_used"), alert=True)
        return
    await event.answer(i18n.t(user_id, "trial_granted", h=billing.trial_hours()),
                       alert=True)
    await ui.show_store(event, store, 0, edit=True)


@route("mya:")
async def my_access(event, rest: str) -> None:
    user_id = event.sender_id
    grants = db.user_grants(user_id)
    orders = db.user_orders(user_id, 5)
    lines = ["💎 <b>আমার অ্যাক্সেস</b>", ""]
    if not grants and not orders:
        lines.append(i18n.t(user_id, "no_access_yet"))

    active, expired = [], []
    for grant in grants:
        entry = f"🏪 <b>{esc(grant['store_name'])}</b> — {access.access_note(db.store(grant['store_id']), user_id)}"
        (active if access.has_access(grant["store_id"], user_id) else expired).append(entry)
    if active:
        lines.append("<b>চালু আছে:</b>")
        lines.extend(active)
    if expired:
        lines.append("")
        lines.append("<b>শেষ হয়ে গেছে:</b>")
        lines.extend(expired)

    if orders:
        lines.append("")
        lines.append("<b>সাম্প্রতিক অর্ডার:</b>")
        for order in orders:
            icon = {"approved": "✅", "rejected": "❌", "pending": "⏳"}.get(order["status"], "•")
            lines.append(f"{icon} #{order['id']} · {esc(order['store_name'] or '')} · "
                         f"{billing.price_text(order['amount'] or 0)} · {order['status']}")
    lines.append("")
    lines.append(i18n.t(user_id, "help_button") + " — /help")
    await ui.render(event, "\n".join(lines),
                    [[Button.inline(i18n.t(user_id, "back_stores"), "bs:0")]], edit=True)
    await event.answer()


# ============================================================== admin side
@route("aod")
async def orders_desk(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    pending = db.orders(status="pending", limit=10)
    revenue = db.revenue()
    lines = [
        "🧾 <b>Order desk</b>",
        f"💰 Approved revenue: <b>{billing.price_text(revenue['total'])}</b> "
        f"({revenue['count']} orders · avg {billing.price_text(revenue['avg'])})",
        f"⏳ Pending: <b>{revenue['pending']}</b>",
        "",
    ]
    rows: list[list[Button]] = []
    if not pending:
        lines.append("<i>No pending payments right now.</i>")
    for order in pending:
        store = db.store(order["store_id"])
        user = db.user(order["user_id"]) or {}
        lines.append(
            f"#{order['id']} · <b>{esc(store['name']) if store else '?'}</b> · "
            f"{esc(order['plan_name'] or '')} · {billing.price_text(order['amount'] or 0)}\n"
            f"   👤 {esc(user.get('name') or 'User')} <code>{order['user_id']}</code> · "
            f"{fmt_ts(order['created_at'], '%m-%d %H:%M')}"
        )
        rows.append([
            Button.inline(f"✅ #{order['id']}", f"aok:{order['id']}"),
            Button.inline(f"❌ #{order['id']}", f"ano:{order['id']}"),
            Button.inline("👤", f"aou:{order['user_id']}"),
        ])
    rows.append([Button.inline("📅 Revenue (14 days)", "ar14"),
                 Button.inline("🎟️ Coupons", "acp")])
    rows.append([Button.inline("🔙 Back to panel", "adm:back")])
    await ui.render(event, "\n".join(lines), rows, edit=True)
    await event.answer()


@route("aok:")
async def approve_order(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    order = billing.approve_order(safe_int(rest), admin_id)
    if order is None:
        await event.answer("Order not found.", alert=True)
        return
    store = db.store(order["store_id"])
    await event.answer("✅ Approved & access granted.", alert=True)
    await safe_call(bot.send_message, order["user_id"],
                    i18n.t(order["user_id"], "order_approved",
                           store=esc(store["name"] if store else ""), days=order["days"] or 0),
                    what="order_notice", retries=1, raise_after_retries=False)
    await orders_desk(event, "")


@route("ano:")
async def reject_order(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    order_id = safe_int(rest)
    billing.reject_order(order_id, admin_id)
    order = db.order(order_id)
    await event.answer("❌ Rejected.", alert=True)
    if order:
        await safe_call(bot.send_message, order["user_id"],
                        i18n.t(order["user_id"], "order_rejected",
                               order=order_id, note=""),
                        what="order_notice", retries=1, raise_after_retries=False)
    await orders_desk(event, "")


@route("aou:")
async def order_user_info(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    user_id = safe_int(rest)
    user = db.user(user_id) or {}
    grants = db.user_grants(user_id)
    orders = db.user_orders(user_id, 5)
    lines = [
        f"👤 <b>{esc(user.get('name') or 'Unknown')}</b> · <code>{user_id}</code>",
        f"Joined: {fmt_ts(user.get('joined_at'))} · Orders: {len(orders)}",
        "",
    ]
    for grant in grants:
        lines.append(f"🏪 {esc(grant['store_name'])} — {access.access_note(db.store(grant['store_id']), user_id)}")
    for order in orders:
        lines.append(f"🧾 #{order['id']} {order['status']} · {billing.price_text(order['amount'] or 0)}")
    rows = [
        [Button.inline("👑 Grant premium", f"gst:{user_id}")],
        [Button.inline("🔙 Back", "aod")],
    ]
    await ui.render(event, "\n".join(lines), rows, edit=True)
    await event.answer()


@route("ar14")
async def revenue_chart(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    rows_data = db.revenue_by_day(14)
    lines = ["📅 <b>Revenue — last 14 days</b>", ""]
    if not rows_data:
        lines.append("<i>No approved payments yet.</i>")
    else:
        top = max(total for _day, total in rows_data) or 1
        for day, total in rows_data:
            bar = "█" * max(1, int(12 * total / top))
            lines.append(f"<code>{day}</code> {bar} {billing.price_text(total)}")
    lines.append("")
    lines.append(f"Total: <b>{billing.price_text(db.revenue()['total'])}</b>")
    await ui.render(event, "\n".join(lines), [[Button.inline("🔙 Back", "aod")]], edit=True)
    await event.answer()


@route("acp")
async def coupon_list(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    rows_data = db.coupons()
    lines = ["🎟️ <b>Coupons</b>", ""]
    for coupon in rows_data[:10]:
        scope = "all stores" if coupon["store_id"] is None else f"store {coupon['store_id']}"
        lines.append(f"<code>{esc(coupon['code'])}</code> · {coupon['percent']}% off / "
                     f"{coupon['days']} days · used {coupon['used']}"
                     f"{'/' + str(coupon['max_uses']) if coupon['max_uses'] else ''} · {scope}")
    if not rows_data:
        lines.append("<i>No coupons yet.</i>")
    rows = [[Button.inline("➕ New coupon", "acn")],
            [Button.inline("🔙 Back", "aod")]]
    await ui.render(event, "\n".join(lines), rows, edit=True)
    await event.answer()


@route("acn")
async def new_coupon(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store_id = flow_ctx(admin_id).get("store_id")
    ask(admin_id, "coupon_create", store_id=store_id)
    store = db.store(store_id) if store_id else None
    scope_note = f"Store: <b>{esc(store['name'])}</b> (শুধু এই স্টোরে চলবে)" if store else "Scope: <b>সব স্টোরে</b> চলবে"
    await event.respond(
        f"🎟️ নতুন কুপন তৈরি — {scope_note}\n\n"
        "ফরম্যাট: <code>CODE percent days max_uses</code>\n\n"
        "উদাহরণ:\n"
        "<code>EID50 50 0 100</code> → ৫০% ছাড়, সীমা ১০০ বার\n"
        "<code>FREEWEEK 0 7 50</code> → ৭ দিন ফ্রি, সীমা ৫০ বার\n"
        "<code>WELCOME 20 0 0</code> → ২০% ছাড়, আনলিমিটেড\n\n"
        "<i>percent = ছাড়ের %, days = ফ্রি দিন (০ হলে ছাড়), max_uses = কতবার ব্যবহার হবে (০ = আনলিমিটেড)</i>"
    )
    await event.answer()


@route("acn:")
async def new_coupon_for_store(event, rest: str) -> None:
    """Create coupon scoped to a specific store."""
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    remember(admin_id, store_id=store["id"])
    ask(admin_id, "coupon_create", store_id=store["id"])
    await event.respond(
        f"🎟️ <b>{esc(store['name'])}</b> এর জন্য কুপন\n\n"
        "ফরম্যাট: <code>CODE percent days max_uses</code>\n"
        "উদাহরণ: <code>{store['slug'].upper()}50 50 0 100</code>"
    )
    await event.answer()


@route("apl:")
async def plan_list(event, rest: str) -> None:
    """Plan manager for one store (called from the store picker)."""
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    remember(admin_id, store_id=store["id"])
    plans = db.plans(store["id"])
    lines = [f"💎 <b>Plans — {esc(store['name'])}</b>", ""]
    for plan in plans:
        state = "✅" if plan["active"] else "⏸"
        lines.append(f"{state} {esc(plan['name'])} · {plan['days']} days · "
                     f"{billing.price_text(plan['price'])}")
    if not plans:
        lines.append("<i>No plans yet. A store without plans can't sell — add one!</i>")
    rows = [[Button.inline("➕ Add plan", "apln"), Button.inline("⚡ Starter set", "apls")]]
    for plan in plans:
        rows.append([
            Button.inline(f"{'⏸' if plan['active'] else '▶️'} {plan['name'][:16]}", f"aplx:{plan['id']}"),
            Button.inline("🗑", f"apld:{plan['id']}"),
        ])
    rows.append([Button.inline("🔙 Back to panel", "adm:back")])
    await ui.render(event, "\n".join(lines), rows, edit=True)
    await event.answer()


@route("apln")
async def new_plan(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store_id = flow_ctx(admin_id).get("store_id")
    if not store_id:
        await event.answer("Pick a store first from 💎 Plans & pricing.", alert=True)
        return
    ask(admin_id, "plan_create", store_id=store_id)
    store = db.store(store_id)
    await event.respond(
        f"➕ <b>{esc(store['name']) if store else ''}</b> — নতুন প্ল্যান যোগ করুন\n\n"
        "ফরম্যাট: <code>Name | days | price</code>\n\n"
        "উদাহরণ:\n"
        "<code>7 Days | 7 | 79</code>\n"
        "<code>1 Month | 30 | 199</code>\n"
        "<code>Lifetime | 3650 | 1499</code>\n\n"
        "<i>days = কতদিন অ্যাক্সেস থাকবে, price = দাম (০ দিলে ফ্রি)</i>"
    )
    await event.answer()


@route("apls")
async def starter_plans(event, rest: str) -> None:
    """One tap: 7d / 30d / 90d / lifetime starter pricing."""
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store_id = flow_ctx(admin_id).get("store_id")
    store = db.store(store_id) if store_id else None
    if store is None:
        await event.answer("Pick a store first.", alert=True)
        return
    for name, days, price in (("7 Days", 7, 79), ("1 Month", 30, 199),
                              ("3 Months", 90, 499), ("Lifetime", 3650, 1499)):
        db.add_plan(store["id"], name, days, price)
    await event.answer("⚡ Starter plans added.", alert=True)
    await plan_list(event, str(store["id"]))


@route("aplx:")
async def toggle_plan_state(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    plan = db.plan(safe_int(rest))
    if plan is None:
        return
    db.toggle_plan(plan["id"])
    await event.answer("Updated.", alert=True)
    await plan_list(event, str(plan["store_id"]))


@route("apld:")
async def remove_plan(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    plan = db.plan(safe_int(rest))
    if plan is None:
        return
    db.delete_plan(plan["id"])
    await event.answer("🗑 Plan removed.", alert=True)
    await plan_list(event, str(plan["store_id"]))


@route("apm")
async def payment_settings(event, rest: str) -> None:
    """Show the configured payment details and how to change them."""
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    lines = [
        "💳 <b>পেমেন্ট সেটিংস</b>",
        "",
        f"মুদ্রা: <b>{esc(settings.get_str('CURRENCY', '৳'))}</b>",
    ]
    methods = billing.payment_methods()
    if methods:
        lines.extend(f"• {name}: <code>{esc(value)}</code>" for name, value in methods)
    else:
        lines.append("<i>No payment numbers configured yet.</i>")
    lines.extend([
        "",
        "✏️ নিচের বাটনে চাপ দিয়ে নাম্বার যোগ/বদল করতে পারবেন — অথবা "
        "<b>ওয়েবসাইটের অ্যাডমিন প্যানেল → ⚙️ Settings → 💳 পেমেন্ট</b> থেকেও।",
        "<i>বট রিস্টার্ট করার দরকার নেই, সাথে সাথেই কার্যকর হয়।</i>",
    ])
    rows = [[Button.inline(f"{'✅' if settings.get(key) else '➕'} {label}",
                           f"pme:{key}") for key, label in (("PAY_BKASH", "bKash"),
                                                            ("PAY_NAGAD", "Nagad"))],
            [Button.inline(f"{'✅' if settings.get(key) else '➕'} {label}",
                           f"pme:{key}") for key, label in (("PAY_ROCKET", "Rocket"),
                                                            ("PAY_UPI", "UPI"))],
            [Button.inline(f"{'✅' if settings.get('PAY_CRYPTO') else '➕'} Crypto",
                           "pme:PAY_CRYPTO")],
            [Button.inline("📝 পেমেন্ট নির্দেশনা", "pme:PAY_NOTE"),
             Button.inline("🪙 মুদ্রা", "pme:CURRENCY")],
            [Button.inline("🎁 ট্রায়াল ঘণ্টা", "pme:TRIAL_HOURS"),
             Button.inline("🌐 ওয়েবসাইটে সেটিংস", "adm:settings")],
            [Button.inline("🔙 Back to panel", "adm:back")]]
    await ui.render(event, "\n".join(lines), rows, edit=True)
    await event.answer()


def admin_contact_text(user_id: int, store: dict | None = None) -> str:
    from app.services import support
    note = support.support_line()
    return i18n.t(user_id, "contact_title", note=esc(note) if note else "")


@route("ct:")
async def contact_admin(event, rest: str) -> None:
    """User wants to talk to the admin → start a ticket."""
    user_id = event.sender_id
    store = db.store(safe_int(rest)) if rest and safe_int(rest) else None
    ask(user_id, "contact_message", store_id=store["id"] if store else None)
    await ui.render(event, admin_contact_text(user_id, store),
                    [[Button.inline(i18n.t(user_id, "back_stores"),
                                    f"s:{store['id']}" if store else "bs:0")]],
                    edit=True)
    await event.answer()


@route("supr:")
async def reply_to_ticket(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    ticket = db.ticket(safe_int(rest))
    if ticket is None:
        await event.answer("Ticket not found.", alert=True)
        return
    ask(admin_id, "ticket_reply", ticket_id=ticket["id"])
    await event.respond(f"✍️ Reply to ticket #{ticket['id']} "
                        f"(<code>{ticket['user_id']}</code>):\n\n"
                        f"<i>{esc(ticket['message'][:300])}</i>")
    await event.answer()


@route("supc:")
async def close_ticket(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    ticket_id = safe_int(rest)
    ticket = db.ticket(ticket_id)
    db.close_ticket(ticket_id)
    await event.answer("✅ টিকেট বন্ধ করা হলো", alert=True)
    # ইউজারকে জানিয়ে দেওয়া — আগে সে বুঝতেই পারত না যে উত্তর আসবে না
    if ticket and ticket.get("user_id"):
        try:
            await bot.send_message(
                ticket["user_id"],
                "📮 <b>আপনার টিকেট #" + str(ticket_id) + " বন্ধ করা হয়েছে।</b>\n"
                "আবার কিছু জানতে চাইলে শুধু মেসেজ লিখে দিন — আমরা সবসময় আছি।",
            )
        except Exception:
            pass


@route("supq")
async def ticket_queue(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    from app.services import support
    tickets = db.tickets("open", 10)
    lines = ["📩 <b>Support queue</b>", support.stats_line(), ""]
    rows: list[list[Button]] = []
    if not tickets:
        lines.append("<i>No open tickets 🎉</i>")
    for ticket in tickets:
        user = db.user(ticket["user_id"]) or {}
        lines.append(f"#{ticket['id']} · {esc(user.get('name') or 'User')} "
                     f"<code>{ticket['user_id']}</code>\n   <i>{esc(ticket['message'][:200])}</i>")
        rows.append([
            Button.inline(f"✍️ #{ticket['id']}", f"supr:{ticket['id']}"),
            Button.inline("✅", f"supc:{ticket['id']}"),
        ])
    requests = db.requests("open", 10)
    if requests:
        lines.append("")
        lines.append("🙋 <b>Content requests</b>")
        for request in requests:
            lines.append(f"#{request['id']} · <i>{esc(request['keyword'])}</i> · "
                         f"<code>{request['user_id']}</code>")
            rows.append([Button.inline(f"✅ Request #{request['id']} done",
                                       f"rqd:{request['id']}")])
    rows.append([Button.inline("🔙 Back to panel", "adm:back")])
    await ui.render(event, "\n".join(lines), rows, edit=True)
    await event.answer()


@route("rqd:")
async def close_request(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    db.close_request(safe_int(rest))
    await event.answer("Marked as done.", alert=True)
    await ticket_queue(event, "")


# =========================================================== promo / growth
@route("winb")
async def winback(event, rest: str) -> None:
    """Message everyone whose access expired in the last 30 days."""
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    await ui.render(
        event,
        "🔁 <b>Win-back campaign</b>\n\n"
        "যাদের অ্যাক্সেস শেষ হয়ে গেছে তাদের অফার সহ মেসেজ পাঠাতে পারেন।\n"
        "নিচের যেকোনো একটি বেছে নিন (মেসেজ যাবে বটের মাধ্যমে, ব্লক করা ইউজার স্কিপ হবে):",
        [
            [Button.inline("🎁 20% off coupon + offer", "wbs:20")],
            [Button.inline("🔙 Back to panel", "adm:back")],
        ],
        edit=True,
    )
    await event.answer()


@route("wbs:")
async def winback_send(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    targets = [uid for uid in db.all_user_ids() if uid not in cfg.ADMIN_IDS]
    if not targets:
        await event.answer("No users to message.", alert=True)
        return
    code = f"WINBACK{int(time.time()) % 10000}"
    db.add_coupon(code, None, percent=20, max_uses=500,
                  expires_at=time.time() + 7 * 86400)
    text = (
        "🎁 <b>আমরা মিস করছি!</b>\n\n"
        "আপনার সাবস্ক্রিপশন শেষ হয়ে গেছে — ফিরে আসুন এবং "
        f"<code>{code}</code> কোড দিয়ে <b>২০% ছাড়</b> নিন (৭ দিনের জন্য সীমিত)।\n\n"
        "স্টোর খুলতে /start চাপুন।"
    )
    status = await event.respond(f"📢 Sending win-back offer to {len(targets)} user(s)…")
    from app.handlers.messages import run_broadcast
    await run_broadcast(admin_id, targets, text, status)


@route("pme:")
async def payment_edit(event, rest: str) -> None:
    """পেমেন্ট নাম্বার/মুদ্রা/ট্রায়াল — সব সরাসরি বট থেকেই বদলানো যায়."""
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    from app.handlers.admin import setting_edit
    await setting_edit(event, f"payments:{rest}")
