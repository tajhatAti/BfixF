"""Inline keyboard builders."""
from __future__ import annotations
from telethon.tl.custom import Button
from app import config as cfg
from app.keyboards_helpers import cut, kind_icon, pairs

def admin_panel(admin_id: int, active_store_name: str | None, session_ok: bool,
                premium_on: bool | None, force_channel: str, caption_set: bool,
                pending_orders: int = 0, open_tickets: int = 0) -> list[list[Button]]:
    store_label = active_store_name or "Not set"
    alerts = []
    if pending_orders:
        alerts.append(f"🧾 {pending_orders} pending")
    if open_tickets:
        alerts.append(f"📩 {open_tickets} tickets")
    rows: list[list[Button]] = []
    if alerts:
        rows.append([Button.inline("🔔 " + " · ".join(alerts) + " — tap to review", "aod")])
    lock_icon = "🔒" if premium_on else ("🌐" if premium_on is not None else "—")
    rows.append([
        Button.inline(f"📌 {cut(store_label, 18)} {lock_icon}", "adm:stores"),
        Button.inline("🔄 Switch" if active_store_name else "➕ New store", "adm:active" if active_store_name else "adm:new")
    ])
    rows.append([
        Button.inline("🏪 Stores", "adm:stores"),
        Button.inline("👥 Users & Access", "adm:users"),
    ])
    rows.append([
        Button.inline(f"💰 Sales ({pending_orders})" if pending_orders else "💰 Sales", "adm:sales"),
        Button.inline("📦 Content", "adm:content"),
    ])
    rows.append([
        Button.inline("📢 Growth", "adm:growth"),
        Button.inline("🛠 Settings", "adm:settings"),
    ])
    rows.append([
        Button.inline("📡 Channels", "ch:home"),
        Button.inline("📊 Analytics", "an:home"),
        Button.inline("🔗 Links", "lk:home"),
    ])
    rows.append([
        Button.inline(f"📩 Support ({open_tickets})" if open_tickets else "📩 Support", "supq"),
        Button.inline("📊 Stats", "adm:stats"),
    ])
    session_label = "📲 Session ✅" if session_ok else "📲 Session ❌"
    rows.append([
        Button.inline(session_label, "adm:conn"),
        Button.inline("🔍 Quick scan", "adm:scan"),
        Button.inline("🤖 Bot scan", "adm:bscan"),
    ])
    rows.append([
        Button.inline("👁 User view", "adm:prev"),
        Button.inline("🆘 Help", "adm:help"),
    ])
    return rows

def admin_stores_menu(stores: list[dict], active_id: int | None) -> list[list[Button]]:
    rows: list[list[Button]] = []
    if stores:
        for first, second in pairs(stores):
            row = []
            for s in (first, second):
                if s is None:
                    continue
                tick = "✅ " if active_id and s["id"] == active_id else ""
                lock = "🔒" if s["is_premium"] else "🌐"
                row.append(Button.inline(f"{tick}{lock} {cut(s['name'], 16)}", f"sss:{s['id']}"))
            rows.append(row)
    rows.append([Button.inline("➕ New store", "adm:new"), Button.inline("📌 Set active", "adm:active")])
    rows.append([Button.inline("🗑 Delete store", "adm:del"), Button.inline("🔗 All store links", "adm:links")])
    rows.append([Button.inline("🔙 Back to panel", "adm:back")])
    return rows

def admin_users_menu() -> list[list[Button]]:
    return [
        [Button.inline("👥 All users", "adm:users_list"), Button.inline("🔍 Search user", "adm:users_search")],
        [Button.inline("👑 Grant access", "adm:gp"), Button.inline("🚫 Revoke access", "adm:revoke_menu")],
        [Button.inline("📢 Broadcast", "adm:bc"), Button.inline("🎁 Referral info", "adm:ref_info")],
        [Button.inline("🔙 Back to panel", "adm:back")],
    ]

def admin_sales_menu(pending_orders: int = 0) -> list[list[Button]]:
    return [
        [Button.inline(f"🧾 Orders ({pending_orders})" if pending_orders else "🧾 Orders", "aod"),
         Button.inline("📅 Revenue", "ar14")],
        [Button.inline("🎟️ Coupons", "acp"), Button.inline("💎 Plans & pricing", "adm:plans")],
        [Button.inline("💳 Payment settings", "apm"), Button.inline("🌐 Win-back", "winb")],
        [Button.inline("🔙 Back to panel", "adm:back")],
    ]

def admin_content_menu() -> list[list[Button]]:
    return [
        [Button.inline("🔍 Scan (session)", "adm:scan"), Button.inline("🤖 Bot scan (easy)", "adm:bscan")],
        [Button.inline("🗂 Manage files", "adm:files"), Button.inline("🔍 File search", "adm:file_search")],
        [Button.inline("🎬 Multi-link", "adm:ml"), Button.inline("📦 Batch link", "adm:batch")],
        [Button.inline("🔗 Store links", "adm:links"), Button.inline("🛠 Store settings", "adm:sset")],
        [Button.inline("🔙 Back to panel", "adm:back")],
    ]

def admin_growth_menu(force_channel: str = "", caption_set: bool = False) -> list[list[Button]]:
    channel_label = f"📢 Force Join: {cut(force_channel, 12)}" if force_channel else "📢 Force Join: Off"
    caption_label = "📝 Caption: set" if caption_set else "📝 Caption: off"
    return [
        [Button.inline("📢 Broadcast", "adm:bc"), Button.inline("🎁 Referral reward", "adm:ref")],
        [Button.inline("🏆 Referral leaderboard", "adm:ref_leaderboard"), Button.inline("📅 Daily drip", "adm:drip")],
        [Button.inline(channel_label, "adm:fj"), Button.inline(caption_label, "adm:cap")],
        [Button.inline("🔗 Share & Invite", "adm:links"), Button.inline("👋 Welcome msg", "adm:welcome_set")],
        [Button.inline("🔙 Back to panel", "adm:back")],
    ]

def admin_settings_menu(session_ok: bool = False) -> list[list[Button]]:
    session_label = "📲 Session: ✅ Connected" if session_ok else "📲 Session: ❌ Connect"
    return [
        [Button.inline(session_label, "adm:conn"), Button.inline("🔄 Replace session", "adm:repl")],
        [Button.inline("💾 Backup now", "adm:backup"), Button.inline("📥 Download DB", "adm:dbdl")],
        [Button.inline("🔍 File search", "adm:file_search"), Button.inline("🔗 Links manage", "adm:links_manage")],
        [Button.inline("🚫 Ban user", "adm:ban_menu"), Button.inline("✅ Unban user", "adm:unban_menu")],
        [Button.inline("👋 Welcome", "adm:welcome_set"), Button.inline("🌐 Language", "adm:lang_info")],
        [Button.inline("📝 বটের মেসেজ এডিট", "tx:home"), Button.inline("📡 Channels", "ch:home")],
        [Button.inline("📢 Force Join", "adm:fj"), Button.inline("🛠 Admin help", "adm:help")],
        [Button.inline("🔙 Back to panel", "adm:back")],
    ]

def store_picker(stores: list[dict], prefix: str, back: str | None = None,
                 mark_active: int | None = None) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for first, second in pairs(stores):
        row = []
        for store in (first, second):
            if store is None:
                continue
            tick = "✅ " if mark_active and store["id"] == mark_active else ""
            lock = "🔒" if store["is_premium"] else "🌐"
            row.append(Button.inline(f"{tick}{lock} {cut(store['name'], 18)}", f"{prefix}{store['id']}"))
        rows.append(row)
    if back:
        rows.append([Button.inline("🔙 Back", back)])
    return rows

def user_store_list(stores: list[dict]) -> list[list[Button]]:
    order = sorted(stores, key=lambda s: (s["is_premium"], s["id"]))
    rows: list[list[Button]] = []
    for first, second in pairs(order):
        row = []
        for store in (first, second):
            if store is None:
                continue
            icon = "🔒" if store["is_premium"] else "🏪"
            row.append(Button.inline(f"{icon} {cut(store['name'], 18)}", f"s:{store['id']}"))
        rows.append(row)
    return rows

def store_content(store: dict, files: list[dict], page: int, total: int,
                  subscribed: bool) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for first, second in pairs(files):
        row = []
        for item in (first, second):
            if item is None:
                continue
            row.append(Button.inline(
                f"{kind_icon(item['kind'])} {cut(item['name'], 24)} · 👁{item['views']}",
                f"f:{item['id']}",
            ))
        rows.append(row)
    total_pages = max(1, (total + cfg.CONTENT_PAGE_SIZE - 1) // cfg.CONTENT_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(Button.inline("« Prev", f"sp:{store['id']}:{page - 1}"))
    nav.append(Button.inline(f"{page + 1}/{total_pages}", "noop"))
    if (page + 1) * cfg.CONTENT_PAGE_SIZE < total:
        nav.append(Button.inline("Next »", f"sp:{store['id']}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Button.inline("🔍 Search this store", f"ss:{store['id']}")])
    sub_label = "🔕 Unsubscribe" if subscribed else "🔔 Subscribe daily"
    rows.append([Button.inline(sub_label, f"sb:{store['id']}")])
    rows.append([Button.inline("🔙 Back to stores", f"bs:{store['admin_id']}")])
    return rows

def search_results(store: dict, files: list[dict]) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for first, second in pairs(files):
        row = []
        for item in (first, second):
            if item is None:
                continue
            row.append(Button.inline(f"{kind_icon(item['kind'])} {cut(item['name'], 24)}",
                                     f"f:{item['id']}"))
        rows.append(row)
    rows.append([Button.inline("🏪 Back to store", f"s:{store['id']}")])
    return rows

def link_generator(store: dict, files: list[dict], page: int, selected: set[int],
                   total: int) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for first, second in pairs(files):
        row = []
        for item in (first, second):
            if item is None:
                continue
            mark = "✅" if item["id"] in selected else kind_icon(item["kind"])
            row.append(Button.inline(f"{mark} {cut(item['name'], 22)}", f"lgt:{item['id']}"))
        rows.append(row)
    total_pages = max(1, (total + cfg.CONTENT_PAGE_SIZE - 1) // cfg.CONTENT_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(Button.inline("« Prev", f"lgp:{page - 1}"))
    nav.append(Button.inline(f"{page + 1}/{total_pages}", "noop"))
    if (page + 1) * cfg.CONTENT_PAGE_SIZE < total:
        nav.append(Button.inline("Next »", f"lgp:{page + 1}"))
    rows.append(nav)
    rows.append([
        Button.inline(f"🔗 Generate ({len(selected)})", "lgg"),
        Button.inline("❌ Clear", "lgc"),
    ])
    rows.append([Button.inline("🔙 Back", "adm:content")])
    return rows

def expiry_choices() -> list[list[Button]]:
    return [
        [Button.inline("⏳ 1 hour", "lge:3600"), Button.inline("⏳ 6 hours", "lge:21600")],
        [Button.inline("⏳ 24 hours", "lge:86400"), Button.inline("⏳ 7 days", "lge:604800")],
        [Button.inline("♾ Never expires", "lge:0")],
        [Button.inline("✏️ Custom (minutes)", "lge:custom")],
    ]

def duration_choices(prefix: str) -> list[list[Button]]:
    return [
        [Button.inline("⏳ 7 days", f"{prefix}604800"), Button.inline("⏳ 30 days", f"{prefix}2592000")],
        [Button.inline("⏳ 90 days", f"{prefix}7776000"), Button.inline("♾ Lifetime", f"{prefix}0")],
        [Button.inline("✏️ Custom (days)", f"{prefix}custom")],
    ]

def scan_chats(dialogs: list[tuple[int, str]], page: int) -> list[list[Button]]:
    per_page = cfg.GROUPS_PAGE_SIZE
    start = page * per_page
    chunk = list(enumerate(dialogs))[start:start + per_page]
    rows: list[list[Button]] = []
    for first, second in pairs(chunk):
        row = []
        for entry in (first, second):
            if entry is None:
                continue
            index, (_chat_id, title) = entry
            row.append(Button.inline(cut(title, 18), f"sc:{index}"))
        rows.append(row)
    total_pages = max(1, (len(dialogs) + per_page - 1) // per_page)
    nav = []
    if page > 0:
        nav.append(Button.inline("«", f"scp:{page - 1}"))
    nav.append(Button.inline(f"{page + 1}/{total_pages}", "noop"))
    if start + per_page < len(dialogs):
        nav.append(Button.inline("»", f"scp:{page + 1}"))
    rows.append(nav)
    rows.append([Button.inline("🔙 Back to content", "adm:content")])
    return rows

def users_page(page: int, total: int) -> list[list[Button]]:
    total_pages = max(1, (total + cfg.USERS_PAGE_SIZE - 1) // cfg.USERS_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(Button.inline("« Prev", f"up:{page - 1}"))
    nav.append(Button.inline(f"{page + 1}/{total_pages}", "noop"))
    if (page + 1) * cfg.USERS_PAGE_SIZE < total:
        nav.append(Button.inline(f"Next »", f"up:{page + 1}"))
    return [nav, [Button.inline("🔙 Back", "adm:users")]]

def file_manager(store: dict, files: list[dict], page: int, total: int) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for first, second in pairs(files):
        row = []
        for item in (first, second):
            if item is None:
                continue
            row.append(Button.inline(f"{kind_icon(item['kind'])} {cut(item['name'], 22)}",
                                     f"fm:{item['id']}"))
        rows.append(row)
    total_pages = max(1, (total + cfg.MANAGE_PAGE_SIZE - 1) // cfg.MANAGE_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(Button.inline("« Prev", f"fmp:{store['id']}:{page - 1}"))
    nav.append(Button.inline(f"{page + 1}/{total_pages}", "noop"))
    if (page + 1) * cfg.MANAGE_PAGE_SIZE < total:
        nav.append(Button.inline(f"Next »", f"fmp:{store['id']}:{page + 1}"))
    rows.append(nav)
    rows.append([Button.inline("🔙 Back to store settings", f"sss:{store['id']}")])
    rows.append([Button.inline("🏠 Main panel", "adm:back")])
    return rows

def file_actions(file_id: int, store_id: int) -> list[list[Button]]:
    return [
        [Button.inline("✏️ Rename", f"fmr:{file_id}"), Button.inline("🔗 Get link", f"fml:{file_id}")],
        [Button.inline("📢 Broadcast this video", f"fbr:{file_id}")],
        [Button.inline("📦 Move", f"fmm:{file_id}"), Button.inline("🗑 Delete", f"fmd:{file_id}")],
        [Button.inline("🔙 Back to files", f"fm:{store_id}")],
    ]

def move_targets(stores: list[dict], file_id: int, back_store_id: int) -> list[list[Button]]:
    rows = [[Button.inline(f"{'🔒' if s['is_premium'] else '🌐'} {cut(s['name'], 20)}",
                           f"fmmv:{file_id}:{s['id']}")] for s in stores]
    rows.append([Button.inline("🔙 Cancel", f"fm:{back_store_id}")])
    return rows

def confirm(yes_data: str, no_data: str, yes_label: str = "✅ Yes, do it") -> list[list[Button]]:
    return [[Button.inline(yes_label, yes_data), Button.inline("❌ Cancel", no_data)]]

def subscription_buttons(store_id: int, subscribed: bool, back_data: str) -> list[list[Button]]:
    label = "🔕 Unsubscribe" if subscribed else "🔔 Subscribe daily"
    return [[Button.inline(label, f"sb:{store_id}")],
            [Button.inline("🔙 Back", back_data)]]

def broadcast_filters(stores: list[dict]) -> list[list[Button]]:
    rows = [
        [Button.inline("📣 All users", "bc:all")],
        [Button.inline("💎 Premium users", "bc:premium"), Button.inline("🆓 Free users", "bc:free")],
    ]
    for store in stores:
        rows.append([Button.inline(f"🏪 {cut(store['name'], 22)} subs",
                                   f"bc:store:{store['id']}")])
    rows.append([Button.inline("🔙 Back", "adm:growth")])
    return rows

def store_access_list(store: dict, grants: list[dict], page: int = 0, per_page: int = 8) -> list[list[Button]]:
    start = page * per_page
    chunk = grants[start:start + per_page]
    rows: list[list[Button]] = []
    for g in chunk:
        user_name = cut(g.get("user_name") or str(g["user_id"]), 12)
        rows.append([
            Button.inline(f"👤 {user_name} ({g['user_id']})", f"aou:{g['user_id']}"),
            Button.inline("🚫 Revoke", f"rvk:{store['id']}:{g['user_id']}"),
        ])
    total_pages = max(1, (len(grants) + per_page - 1) // per_page)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(Button.inline("« Prev", f"sga:{store['id']}:{page - 1}"))
        nav.append(Button.inline(f"{page + 1}/{total_pages}", "noop"))
        if (page + 1) * per_page < len(grants):
            nav.append(Button.inline("Next »", f"sga:{store['id']}:{page + 1}"))
        rows.append(nav)
    rows.append([Button.inline("👑 Grant new", f"gst:{store['id']}"), Button.inline("➕ Bulk grant", f"gbulk:{store['id']}")])
    rows.append([Button.inline("🔙 Back to store settings", f"sss:{store['id']}")])
    return rows


# ===================================================== broadcast studio (v3)
def broadcast_studio_menu(running: int = 0, scheduled: int = 0, total: int = 0) -> list[list[Button]]:
    """High-level broadcast entry: new, scheduled, history, per-file."""
    rows: list[list[Button]] = [
        [Button.inline("📣 New broadcast", "nbcnew")],
    ]
    second: list[Button] = []
    if running:
        second.append(Button.inline(f"⏳ Running ({running})", "nbclist:running"))
    if scheduled:
        second.append(Button.inline(f"📅 Scheduled ({scheduled})", "nbclist:scheduled"))
    if second:
        rows.append(second)
    rows.append([Button.inline(f"📜 History ({total})" if total else "📜 History", "nbclist:done")])
    rows.append([Button.inline("🎬 Broadcast one video", "adm:files"),
                 Button.inline("🔔 Daily drip", "adm:drip")])
    rows.append([Button.inline("📈 Audience size", "nbcsize"),
                 Button.inline("⚙️ Broadcast speed", "nbcsetup")])
    rows.append([Button.inline("🔙 Back", "adm:growth")])
    return rows


def broadcast_audience_picker(stores: list[dict], prefix: str = "nbca:",
                              back: str = "adm:bc") -> list[list[Button]]:
    """Pick who receives the campaign. Per store: everyone / subscribers / never bought."""
    rows: list[list[Button]] = [
        [Button.inline("📣 All users", f"{prefix}all"),
         Button.inline("💎 Premium buyers", f"{prefix}premium")],
        [Button.inline("🆓 Free users", f"{prefix}free")],
    ]
    for store in stores:
        rows.append([Button.inline(f"🏪 {cut(store['name'], 22)}", "noop")])
        rows.append([
            Button.inline("👥 Everyone", f"{prefix}store:{store['id']}"),
            Button.inline("🔔 Subscribers", f"{prefix}sub:{store['id']}"),
        ])
        rows.append([Button.inline("🎯 Never bought", f"{prefix}nosale:{store['id']}")])
    rows.append([Button.inline("🔙 Back", back)])
    return rows


def broadcast_confirm(campaign_id: int) -> list[list[Button]]:
    return [
        [Button.inline("🚀 Send now", f"nbcgo:{campaign_id}"),
         Button.inline("🧪 Test to me", f"nbctest:{campaign_id}")],
        [Button.inline("📅 Schedule", f"nbcwhen:{campaign_id}"),
         Button.inline("🗑 Discard", f"nbcdel:{campaign_id}")],
    ]


_STATUS_ICON = {"draft": "📝", "queued": "🕓", "scheduled": "📅", "running": "⏳",
                "done": "✅", "cancelled": "🚫", "failed": "❌", "paused": "⏸"}


def campaign_list(campaigns: list[dict], back: str = "adm:bc") -> list[list[Button]]:
    rows: list[list[Button]] = []
    for campaign in campaigns:
        icon = _STATUS_ICON.get(campaign.get("status", ""), "•")
        label = f"{icon} #{campaign['id']} {cut(campaign.get('title') or campaign.get('text') or 'campaign', 22)}"
        rows.append([Button.inline(label, f"nbcd:{campaign['id']}")])
    if not rows:
        rows.append([Button.inline("— nothing here yet —", "noop")])
    rows.append([Button.inline("📣 New broadcast", "nbcnew"),
                 Button.inline("🔙 Back", back)])
    return rows


def campaign_detail(campaign: dict) -> list[list[Button]]:
    cid = campaign["id"]
    status = campaign.get("status")
    rows: list[list[Button]] = []
    if status in ("queued", "scheduled", "draft"):
        rows.append([Button.inline("🚀 Send now", f"nbcgo:{cid}"),
                     Button.inline("🧪 Test to me", f"nbctest:{cid}")])
    if status == "running":
        rows.append([Button.inline("⏹ Stop", f"nbcstop:{cid}"),
                     Button.inline("🔄 Refresh", f"nbcd:{cid}")])
    if status in ("done", "cancelled") :
        rows.append([Button.inline("🔁 Retry failed", f"nbcagain:{cid}")])
    rows.append([Button.inline("🗑 Delete", f"nbcdel:{cid}"),
                 Button.inline("🔙 Back", "nbclist:all")])
    return rows


def file_broadcast_picker(file_id: int, stores: list[dict],
                          back: str | None = None) -> list[list[Button]]:
    """Audience picker for a single video/file (the `fbr:` flow)."""
    rows: list[list[Button]] = [
        [Button.inline("📣 Everyone", f"fbrgo:{file_id}:all"),
         Button.inline("💎 Premium", f"fbrgo:{file_id}:premium")],
        [Button.inline("🆓 Free users", f"fbrgo:{file_id}:free")],
    ]
    for store in stores:
        rows.append([Button.inline(f"🎯 Never bought {cut(store['name'], 16)}",
                                   f"fbrgo:{file_id}:nosale:{store['id']}")])
    rows.append([Button.inline("🧪 Test to me", f"fbrtest:{file_id}")])
    rows.append([Button.inline("🔙 Back", back or f"fm:{file_id}")])
    return rows
