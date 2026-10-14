"""Extra admin pages: channels, composer, analytics, user detail, limited links.

Split out of `web/dashboard.py` so the route file stays readable — every function
here returns a ready HTML *body*, and the routes only wire forms to it.
"""
from __future__ import annotations

import time

from app import config as cfg, runtime
from app.services import channels, forcejoin, settings
from app.storage import db
from app.utils import esc, fmt_ts


def _start(ts) -> str:
    return fmt_ts(ts, "%d %b %Y") if ts else "—"


def _pill(text: str) -> str:
    return f'<span class="pill">{esc(text)}</span>'


# ------------------------------------------------------------------- channels
def channels_body(flash: str = "", warn: str = "") -> str:
    rows = []
    for row in db.join_channels():
        scope = "🌍 সব স্টোরে" if row.get("store_id") is None else f"🏪 স্টোর #{row['store_id']}"
        invite = row.get("invite") or forcejoin.join_url(row.get("ref") or "")
        rows.append(
            f"<tr><td><b>{esc(row.get('title') or row.get('ref') or '')}</b>"
            f"<div class='muted'>{esc(row.get('ref') or '')}</div></td>"
            f"<td>{_pill(row.get('kind') or '—')}</td>"
            f"<td>{esc(scope)}</td>"
            f"<td>{'✅' if row.get('chat_id') else '⏳'} {esc(str(row.get('chat_id') or '—'))}</td>"
            f"<td>{'✅' if row.get('enabled') else '🚫'}</td>"
            f"<td><a class='btn grey' href='/admin/channels/check/{row['id']}'>🔎 চেক</a> "
            f"<a class='btn grey' href='/admin/channels/toggle/{row['id']}'>"
            f"{'🚫 বন্ধ' if row.get('enabled') else '✅ চালু'}</a> "
            f"<a class='btn bad' href='/admin/channels/delete/{row['id']}'>🗑</a></td></tr>")
        if invite:
            rows[-1] = rows[-1].replace("<td>✅", f"<td>{_pill('🔗')} ✅", 1)

    stores = "".join(
        f"<option value='{s['id']}'>{esc(s['name'])}</option>" for s in db.all_stores())

    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
    return f"""{note}{warnbox}
    <h3>📡 চ্যানেল ম্যানেজমেন্ট</h3>
    <p class="muted">যে চ্যানেলগুলোতে বট পোস্ট করতে পারবে — ফোর্স-জয়েন, ক্যাশ এবং
       “চ্যানেলে পাঠান” সবখানে এগুলো ব্যবহার হয়। যেকোনো রেফারেন্স চলবে:
       <code>@username</code>, <code>https://t.me/+invite</code>, <code>-100…</code>।</p>
    <div class="card">
      <form method=post action="/admin/channels/add">
        <label>চ্যানেল লিংক / ইউজারনেম / আইডি</label>
        <input name=ref placeholder="@mychannel অথবা https://t.me/+AbCdEfGh" required>
        <label>কোন স্টোরের জন্য? (খালি = সব স্টোরে)</label>
        <select name=store_id><option value="">🌍 সব স্টোরে (ডিফল্ট)</option>{stores}</select>
        <button class="ok">➕ যোগ করুন</button>
      </form>
    </div>
    <table><tr><th>চ্যানেল</th><th>ধরন</th><th>কার জন্য</th><th>ID</th><th>চালু</th><th></th></tr>
    {''.join(rows) or "<tr><td colspan=6 class='muted'>এখনো কোনো চ্যানেল যোগ করা হয়নি</td></tr>"}</table>
    <p class="row"><a class="btn" href="/admin/channels/compose">✍️ চ্যানেলে পোস্ট করুন</a>
       <a class="btn grey" href="/admin/channels?refresh=1">🔄 আবার রিজলভ করুন</a></p>"""


def compose_body(composer: dict, store_id: int | None, flash: str = "", warn: str = "") -> str:
    store_options = "".join(
        f"<option value='{s['id']}'{' selected' if store_id == s['id'] else ''}>"
        f"{esc(s['name'])}</option>" for s in db.all_stores())
    channel_options = []
    for row in db.join_channels():
        label = row.get("title") or row.get("ref")
        channel_options.append(
            f"<option value='{esc(str(row.get('chat_id') or row.get('ref')))}'>"
            f"{esc(label)}</option>")
    files = db.newest_files(200)
    file_options = "".join(
        f"<option value='{f['id']}'>#{f['id']} · {esc(f['name'])} · {esc(f['kind'])}</option>"
        for f in files)
    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
    preview = channels.composer_preview(composer["text"], composer["buttons"], store_id)
    preview_buttons = "".join(
        f"<div class='row'>{''.join(f'<span class=pill>{esc(label)} → {esc(url)}</span>' for label, url in row)}</div>"
        for row in preview["buttons"]) or '<span class="muted">কোনো বাটন নেই</span>'
    return f"""{note}{warnbox}
    <h3>✍️ চ্যানেলে মেসেজ + ইনলাইন বাটন</h3>
    <form method=post action="/admin/channels/compose">
      <label>চ্যানেল</label>
      <select name=target>{''.join(channel_options) or '<option value="">— আগে চ্যানেল যোগ করুন —</option>'}</select>
      <label>স্টোর (বাটন/প্রিভিউর জন্য)</label>
      <select name=store_id><option value="">— কোনোটিই না —</option>{store_options}</select>
      <label>মেসেজ (HTML চলে: &lt;b&gt;, &lt;i&gt;, &lt;a href&gt;)</label>
      <textarea name=text rows=7>{esc(composer['text'])}</textarea>
      <label>ফাইল (একাধিক হলে Ctrl/Cmd চেপে বাছুন)</label>
      <select name=file_ids multiple size=6>{file_options}</select>
      <label>লিমিটেড লিংক মোড — চ্যানেলে ভিডিও নয়, কতবার খোলা যাবে এমন বাটন যাবে
             (0 = লিংক মোড বন্ধ, -1 = বন্ধ; খালি রাখলে ডিফল্ট
             {settings.get_int('LINK_DEFAULT_LIMIT', 100)})</label>
      <input name=link_limit type=number value="-1">
      <label>ইনলাইন বাটন — প্রতি লাইনে: <code>লেবেল | লিংক</code> (একই লাইনে <code>&amp;&amp;</code> দিলে পাশাপাশি)</label>
      <textarea name=buttons rows=4>{esc(composer['buttons'])}</textarea>
      <div class="chk"><input type=checkbox name=send_test value=1 id=st>
        <label for=st>প্রথমে আমাকে টেস্ট পাঠান (বটের DM-এ), চ্যানেলে নয়</label></div>
      <button class="ok">📤 চ্যানেলে পাঠান</button>
    </form>
    <div class="card"><h4>প্রিভিউ</h4>
      <div>{composer['text']}</div>
      <div class="muted">বাটন:</div>{preview_buttons}
    </div>"""


# ------------------------------------------------------------------ analytics
def analytics_body(days: int = 14) -> str:
    counts = db.event_counts()
    by_day = db.events_by_day(days)
    users = db.user_count()
    stores = db.all_stores()
    links = db.link_count()
    mirrored = db.count_mirrors()
    files = db.count_files()

    day_rows = "".join(
        f"<tr><td>{esc(str(r['day']))}</td><td>{r['users']}</td><td>{r['joined']}</td>"
        f"<td>{r['delivered']}</td><td>{r['paid']}</td></tr>" for r in by_day)
    store_rows = []
    for store in stores:
        funnel = db.store_funnel(store["id"])
        store_rows.append(
            f"<tr><td><b>{esc(store['name'])}</b><div class='muted'>#{store['id']}</div></td>"
            f"<td>{funnel['came']}</td><td>{funnel['joined']}</td><td>{funnel['watched']}</td>"
            f"<td>{funnel['paid']}</td>"
            f"<td><a class='btn grey' href='/admin/analytics/store/{store['id']}'>বিস্তারিত</a></td></tr>")
    top_files = db.top_files(10)
    file_rows = "".join(
        f"<tr><td>#{f['id']} · {esc(f['name'])}</td><td>{esc(f.get('store_name') or '')}</td>"
        f"<td>{db.unique_viewers(f['id'])}</td><td>{f['views']}</td>"
        f"<td><a class='btn grey' href='/admin/analytics/file/{f['id']}'>কে দেখেছে</a></td></tr>"
        for f in top_files)
    event_rows = "".join(
        f"<tr><td>{esc(fmt_ts(e['ts'], '%d %b %H:%M'))}</td>"
        f"<td>{_user_link(e.get('user_id'), e.get('user_name'))}</td>"
        f"<td>{_pill(e['name'])}</td><td>{esc(e.get('store_name') or '—')}</td>"
        f"<td>{esc(e.get('file_name') or '—')}</td><td>{esc((e.get('detail') or '')[:60])}</td></tr>"
        for e in db.events(limit=60))

    return f"""<h3>📊 অ্যানালিটিক্স</h3>
    <div class="grid">
      {_card("ইউজার", users)}{_card("ফাইল", files)}{_card("ক্যাশড", mirrored)}
      {_card("লিংক", links)}{_card("স্টোর", len(stores))}
      {_card("ভিডিও পাঠানো", counts.get("deliver", 0))}
      {_card("স্টোর ওপেন", counts.get("open_store", 0))}
      {_card("পেমেন্ট শুরু", counts.get("pay_start", 0))}
    </div>
    <h4>📈 শেষ {days} দিন</h4>
    <table><tr><th>দিন</th><th>ইউজার</th><th>নতুন</th><th>ডেলিভারি</th><th>পেমেন্ট</th></tr>
    {day_rows or "<tr><td colspan=5 class='muted'>কোনো ডেটা নেই</td></tr>"}</table>
    <h4>🏪 স্টোর ফানেল (এসেছে → জয়েন → দেখেছে → কিনেছে)</h4>
    <table><tr><th>স্টোর</th><th>এসেছে</th><th>জয়েন</th><th>দেখেছে</th><th>কিনেছে</th><th></th></tr>
    {''.join(store_rows) or "<tr><td colspan=6 class='muted'>স্টোর নেই</td></tr>"}</table>
    <h4>🔥 জনপ্রিয় ফাইল</h4>
    <table><tr><th>ফাইল</th><th>স্টোর</th><th>ইউনিক</th><th>মোট</th><th></th></tr>
    {file_rows or "<tr><td colspan=5 class='muted'>এখনো কেউ দেখেনি</td></tr>"}</table>
    <h4>🕒 সর্বশেষ ইভেন্ট</h4>
    <table><tr><th>সময়</th><th>ইউজার</th><th>কাজ</th><th>স্টোর</th><th>ফাইল</th><th>বিস্তারিত</th></tr>
    {event_rows or "<tr><td colspan=6 class='muted'>ইভেন্ট নেই</td></tr>"}</table>"""


def _card(label: str, value) -> str:
    return f'<div class="card"><div class="l">{esc(label)}</div><div class="n">{value}</div></div>'


def _user_link(user_id, name=None) -> str:
    if not user_id:
        return '<span class="muted">—</span>'
    label = esc(name or str(user_id))
    return f"<a href='/admin/users/{user_id}'>{label}</a>"


def user_detail_body(user_id: int) -> str:
    data = db.user_analytics(user_id, limit=150)
    user = data["user"] or {}
    counts = data["counts"]
    grants = db.user_grants(user_id)
    orders = db.orders(user_id=user_id)
    tickets = db.tickets(status=None, user_id=user_id, limit=20)

    grant_rows = "".join(
        f"<tr><td>{esc(g['store_name'])}</td><td>{_start(g.get('granted_at'))}</td>"
        f"<td>{'♾ লাইফটাইম' if g['expires_at'] is None else _start(g['expires_at'])}</td>"
        f"<td>{esc(g.get('source') or '')}</td></tr>" for g in grants)
    order_rows = "".join(
        f"<tr><td>#{o['id']}</td><td>{esc(str(o.get('plan_name') or ''))}</td>"
        f"<td>{esc(str(o.get('status') or ''))}</td><td>{_start(o.get('created_at'))}</td></tr>"
        for o in orders)
    store_rows = "".join(
        f"<tr><td>{esc(r['name'])}</td><td>{r['hits']}</td>"
        f"<td>{_start(r['last_ts'])}</td></tr>" for r in data["stores"])
    video_rows = "".join(
        f"<tr><td>#{r['id']} · {esc(r['name'])}</td><td>{r['hits']}</td>"
        f"<td>{_start(r['last_ts'])}</td></tr>" for r in data["videos"])
    search_text = ", ".join(f"“{esc(r['ref'])}”×{r['c']}" for r in data["searches"]) or "—"
    event_rows = "".join(
        f"<tr><td>{esc(fmt_ts(e['ts'], '%d %b %H:%M:%S'))}</td><td>{_pill(e['name'])}</td>"
        f"<td>{esc(e.get('store_name') or '—')}</td><td>{esc(e.get('file_name') or '—')}</td>"
        f"<td>{esc((e.get('detail') or '')[:80])}</td></tr>" for e in data["events"])

    return f"""<h3>👤 ইউজার #{user_id}</h3>
    <div class="grid">
      {_card("স্টোর ওপেন", counts.get("open_store", 0))}
      {_card("ভিডিও দেখেছে", counts.get("deliver", 0))}
      {_card("লিংক ক্লিক", counts.get("view_file", 0))}
      {_card("জয়েন ব্লক", counts.get("join_block", 0))}
      {_card("পেমেন্ট শুরু", counts.get("pay_start", 0))}
      {_card("কিনেছে", counts.get("paid", 0))}
    </div>
    <table>
      <tr><th>নাম</th><td>{esc(user.get("name") or "—")}</td>
          <th>ইউজারনেম</th><td>{esc('@' + (user.get('username') or '') if user.get('username') else '—')}</td></tr>
      <tr><th>জয়েন</th><td>{_start(user.get('joined_at'))}</td>
          <th>শেষ দেখা</th><td>{_start(user.get('last_seen'))}</td></tr>
      <tr><th>সার্চ</th><td colspan=3>{search_text}</td></tr>
    </table>
    <p class="row">
      <a class="btn" href="tg://user?id={user_id}">💬 টেলিগ্রামে মেসেজ</a>
      <form method=post action="/admin/users/{user_id}/message" style="display:inline">
        <input name=text placeholder="ড্যাশবোর্ড থেকে মেসেজ পাঠান" style="width:320px">
        <button class="ok">📨 পাঠান</button>
      </form>
      <a class="btn warn" href="/admin/users/{user_id}/ban">🚫 ব্যান</a>
      <a class="btn grey" href="/admin/users/{user_id}/unban">✅ আনব্যান</a>
    </p>
    <h4>🏪 কোন স্টোরে কতবার</h4>
    <table><tr><th>স্টোর</th><th>হিট</th><th>শেষ</th></tr>{store_rows or "<tr><td colspan=3 class='muted'>—</td></tr>"}</table>
    <h4>🎬 কোন ভিডিও দেখেছে</h4>
    <table><tr><th>ভিডিও</th><th>বার</th><th>শেষ</th></tr>{video_rows or "<tr><td colspan=3 class='muted'>—</td></tr>"}</table>
    <h4>💎 অ্যাক্সেস</h4>
    <table><tr><th>স্টোর</th><th>দেওয়া</th><th>শেষ</th><th>সোর্স</th></tr>{grant_rows or "<tr><td colspan=4 class='muted'>কোনো অ্যাক্সেস নেই</td></tr>"}</table>
    <h4>🧾 অর্ডার</h4>
    <table><tr><th>#</th><th>প্ল্যান</th><th>স্টেটাস</th><th>সময়</th></tr>{order_rows or "<tr><td colspan=4 class='muted'>—</td></tr>"}</table>
    <h4>📩 সাপোর্ট টিকিট</h4>
    <table><tr><th>#</th><th>স্টেটাস</th><th>মেসেজ</th><th>সময়</th></tr>
    {''.join(f"<tr><td>{t['id']}</td><td>{esc(str(t.get('status') or ''))}</td>"
             f"<td>{esc((t.get('message') or '')[:80])}</td><td>{_start(t.get('created_at'))}</td></tr>"
             for t in tickets) or "<tr><td colspan=4 class='muted'>—</td></tr>"}</table>
    <h4>🕒 পুরো ইতিহাস</h4>
    <table><tr><th>সময়</th><th>কাজ</th><th>স্টোর</th><th>ফাইল</th><th>বিস্তারিত</th></tr>
    {event_rows or "<tr><td colspan=5 class='muted'>—</td></tr>"}</table>"""


def store_analytics_body(store_id: int) -> str:
    data = db.store_analytics(store_id)
    store = data["store"]
    funnel = data["funnel"]
    if not store:
        return '<div class="warnbox">স্টোর পাওয়া যায়নি</div>'
    file_rows = "".join(
        f"<tr><td>#{f['id']} · {esc(f['name'])}</td><td>{esc(f['kind'])}</td>"
        f"<td>{f['views']}</td><td>{f['uniq']}</td><td>{f['sends']}</td>"
        f"<td>{_start(f['last_ts'])}</td>"
        f"<td><a class='btn grey' href='/admin/analytics/file/{f['id']}'>কে দেখেছে</a></td></tr>"
        for f in data["files"])
    watch_rows = "".join(
        f"<tr><td>{_user_link(w['user_id'], w['name'])}</td><td>{esc('@' + (w['username'] or '') if w['username'] else '')}</td>"
        f"<td>{w['videos']}</td><td>{w['sends']}</td><td>{_start(w['last_ts'])}</td></tr>"
        for w in data["watchers"])
    event_rows = "".join(
        f"<tr><td>{esc(fmt_ts(e['ts'], '%d %b %H:%M'))}</td>"
        f"<td>{_user_link(e.get('user_id'), e.get('user_name'))}</td>"
        f"<td>{_pill(e['name'])}</td><td>{esc(e.get('file_name') or '—')}</td></tr>"
        for e in data["events"][:80])
    return f"""<h3>🏪 {esc(store['name'])} — অ্যানালিটিক্স</h3>
    <div class="grid">
      {_card("এসেছে", funnel['came'])}{_card("জয়েন করেছে", funnel['joined'])}
      {_card("ভিডিও দেখেছে", funnel['watched'])}{_card("কিনেছে", funnel['paid'])}
      {_card("মোট ভিউ", store.get('views') or 0)}
    </div>
    <h4>🎬 কোন ভিডিও কতজন দেখেছে</h4>
    <table><tr><th>ফাইল</th><th>ধরন</th><th>ভিউ</th><th>ইউনিক</th><th>মোট পাঠানো</th><th>শেষ</th><th></th></tr>
    {file_rows or "<tr><td colspan=7 class='muted'>ফাইল নেই</td></tr>"}</table>
    <h4>👥 কে কী দেখেছে</h4>
    <table><tr><th>ইউজার</th><th>ইউজারনেম</th><th>ভিন্ন ভিডিও</th><th>মোট পাঠানো</th><th>শেষ</th></tr>
    {watch_rows or "<tr><td colspan=5 class='muted'>এখনো কেউ দেখেনি</td></tr>"}</table>
    <h4>🕒 ইভেন্ট</h4>
    <table><tr><th>সময়</th><th>ইউজার</th><th>কাজ</th><th>ফাইল</th></tr>
    {event_rows or "<tr><td colspan=4 class='muted'>—</td></tr>"}</table>"""


def file_watchers_body(file_id: int) -> str:
    row = db.file(file_id)
    if row is None:
        return '<div class="warnbox">ফাইল পাওয়া যায়নি</div>'
    store = db.store(row["store_id"]) or {}
    rows = "".join(
        f"<tr><td>{_user_link(w['user_id'], w['name'])}</td>"
        f"<td>{esc('@' + (w['username'] or '') if w['username'] else '')}</td>"
        f"<td>{w['hits']}</td><td>{_start(w['last_ts'])}</td></tr>"
        for w in db.file_watchers(file_id))
    prompt_rows = "".join(
        f"<tr><td>{_user_link(p['user_id'], p.get('user_name'))}</td>"
        f"<td>{p['attempts']}</td><td>{_start(p['created'])}</td></tr>"
        for p in db.join_prompts(200) if p["file_id"] == file_id)
    mirror = "✅ ক্যাশড" if row.get("mirror_msg") else "⏳ ক্যাশ হয়নি"
    return f"""<h3>🎬 {esc(row['name'])} <span class="muted">#{file_id}</span></h3>
    <p class="row">{_pill(store.get('name') or '?')}{_pill(row['kind'])}
       {_pill('ভিউ ' + str(row['views']))}{_pill(mirror)}
       <a class="btn grey" href="/admin/files">← ফাইল লিস্ট</a></p>
    <h4>👥 কারা পেয়েছে ({len(db.file_watchers(file_id))})</h4>
    <table><tr><th>ইউজার</th><th>ইউজারনেম</th><th>বার</th><th>শেষ</th></tr>
    {rows or "<tr><td colspan=4 class='muted'>এখনো কেউ পায়নি</td></tr>"}</table>
    <h4>🔐 জয়েন করে নেয়নি (কতবার চেষ্টা)</h4>
    <table><tr><th>ইউজার</th><th>চেষ্টা</th><th>শুরু</th></tr>
    {prompt_rows or "<tr><td colspan=3 class='muted'>—</td></tr>"}</table>"""


# ------------------------------------------------------------------ link tools
def links_body(flash: str = "", warn: str = "") -> str:
    stores = "".join(
        f"<option value='{s['id']}'>{esc(s['name'])}</option>" for s in db.all_stores())
    files = db.newest_files(300)
    file_options = "".join(
        f"<option value='{f['id']}'>#{f['id']} · {esc(f['name'])} · {esc(f['kind'])}</option>"
        for f in files)
    rows = []
    for link in db.links(limit=100):
        limit = int(link.get("max_clicks") or 0)
        clicks = int(link.get("clicks") or 0)
        left = "♾" if not limit else max(0, limit - clicks)
        url = ""
        if runtime.bot_username:
            url = f"https://t.me/{runtime.bot_username}?start=t{link['token']}"
        note = link.get("kind2") or ""
        open_link_html = (f"<a class='btn grey' href='{esc(url)}' target='_blank'>🔗</a> "
                          if url else "")
        delete_html = f"<a class='btn bad' href='/admin/links/delete/{link['token']}'>🗑</a>"
        per_user = int(link.get("per_user_limit") or 0)
        users = db.link_uses(link["token"])
        who = ", ".join(
            f"{esc(u.get('name') or ('user ' + str(u['user_id'])))}×{u.get('times') or 1}"
            for u in users[:6]) or "<span class='muted'>এখনো কেউ নেয়নি</span>"
        rows.append(
            f"<tr><td><code>{esc(link['token'])}</code><div class='muted'>{esc(note)}</div></td>"
            f"<td>{esc(link.get('kind') or '')}</td>"
            f"<td>{clicks}</td><td>{limit or '♾'}</td><td>{left}</td>"
            f"<td>{per_user or '♾'}</td>"
            f"<td class='muted'>{who}</td>"
            f"<td>{_start(link.get('created_at'))}</td>"
            f"<td>{'⌛ ' + _start(link['expires_at']) if link.get('expires_at') else '♾'}</td>"
            f"<td>{open_link_html}{delete_html}</td></tr>")
    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
    return f"""{note}{warnbox}
    <h3>🔗 লিংক (লিমিটেড / আনলিমিটেড)</h3>
    <p class="muted">লিমিট দিলে ঠিক ততবারই খোলা যাবে; তারপর ইউজারকে
       “লিমিট শেষ — অ্যাক্সেস দেওয়া হবে না” বলা হবে।</p>
    <div class="card">
      <form method=post action="/admin/links/new">
        <label>ফাইল বাছুন (একাধিক হলে Ctrl/Cmd)</label>
        <select name=file_ids multiple size=8 required>{file_options}</select>
        <label>স্টোর (খালি রাখলে সরাসরি ফাইল)</label>
        <select name=store_id><option value="">— সরাসরি —</option>{stores}</select>
        <label>কতবার খোলা যাবে (0 = আনলিমিটেড)</label>
        <input name=max_clicks type=number min=0 value="{settings.get_int('LINK_DEFAULT_LIMIT', 100)}">
        <label>একজন ইউজার সর্বোচ্চ কতবার নিতে পারবে (0 = সীমা নেই)</label>
        <input name=per_user type=number min=0 value="1">
        <label>নোট (নিজের জন্য)</label>
        <input name=note placeholder="যেমন: অমুক গ্রাহকের জন্য ১০০ ক্লিক">
        <button class="ok">🔗 লিংক বানান</button>
      </form>
    </div>
    <table><tr><th>টোকেন</th><th>ধরন</th><th>ব্যবহৃত</th><th>লিমিট</th><th>বাকি</th>
    <th>প্রতি ইউজার</th><th>কে নিয়েছে</th><th>তৈরি</th><th>মেয়াদ</th><th></th></tr>
    {''.join(rows) or "<tr><td colspan=10 class='muted'>কোনো লিংক নেই</td></tr>"}</table>"""


def store_forcejoin_body(store: dict) -> str:
    """Per-store force-join editor (inside the store page)."""
    rows = "".join(
        f"<tr><td>{esc(ch.get('title') or ch.get('ref'))}</td>"
        f"<td>{esc(ch.get('ref') or '')}</td>"
        f"<td>{'✅' if ch.get('chat_id') else '⏳'}</td>"
        f"<td><a class='btn bad' href='/admin/channels/delete/{ch['id']}'>🗑</a></td></tr>"
        for ch in db.join_channels(store["id"]))
    return f"""<h4>📢 ইউজারের জন্য বাধ্যতামূলক চ্যানেল</h4>
    <div class="card">
      <p class="muted">এই চ্যানেলটি শুধু ভিডিও পাওয়ার আগে join gate হিসেবে কাজ করে।
      ভিডিও প্রকাশের চ্যানেল আলাদা করে নিচে সেট করা যায়।</p>
      <form method=post action="/admin/stores/{store['id']}/forcejoin">
        <label>বাধ্যতামূলক চ্যানেল (খালি = বন্ধ)</label>
        <input name=ref value="{esc(store.get('forcejoin') or '')}"
               placeholder="@store_channel / https://t.me/+AbCdEfGhIjK / -100…">
        <button class="ok">💾 সেভ</button>
      </form>
      <table><tr><th>চ্যানেল</th><th>রেফারেন্স</th><th>ID</th><th></th></tr>
      {rows or "<tr><td colspan=4 class='muted'>এই স্টোরে আলাদা কোনো join channel নেই — ডিফল্ট চ্যানেল প্রযোজ্য</td></tr>"}</table>
    </div>"""


def store_publish_channel_body(store: dict) -> str:
    """Per-store publishing channel created by the connected Telegram session."""
    store_id = int(store["id"])
    channel_id = store.get("publish_channel_id")
    if not channel_id:
        return f"""<h4>📡 Telegram channel · ভিডিও প্রকাশ</h4>
        <div class="card">
          <p class="muted">স্টোর তৈরি হলে নিজের Telegram session দিয়ে একটি private channel
          তৈরি হবে। Bot-কে admin করা গেলে নতুন ভিডিওগুলো সেখানে auto-post হবে।</p>
          <form method=post action="/admin/stores/{store_id}/channel/create">
            <label>চ্যানেলের নাম</label>
            <input name=title maxlength=128 value="{esc(store['name'])}" required>
            <button class="ok">＋ Telegram channel তৈরি করুন</button>
          </form>
          <p class="muted">প্রথমে <a href="/admin/session">Telegram session</a> connect করুন।</p>
        </div>"""

    link = store.get("publish_channel_link") or ""
    link_html = (f'<a class="btn grey" href="{esc(link)}" target="_blank" rel="noopener">'
                 "↗ চ্যানেল খুলুন</a>" if link else "")
    auto_post = bool(store.get("auto_post_enabled"))
    status = "চালু — নতুন upload চ্যানেলে যাবে" if auto_post else "বন্ধ"
    latest = db.files_of(store_id, newest_first=True)
    latest_button = (
        f'<form method=post action="/admin/stores/{store_id}/channel/post-latest">'
        '<button class="btn" type="submit">▶ সর্বশেষ ভিডিও পোস্ট করুন</button></form>'
        if latest else '<span class="muted">পোস্ট করার মতো ভিডিও এখনো নেই।</span>')
    return f"""<h4>📡 Telegram channel · ভিডিও প্রকাশ</h4>
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <div><b>{esc(store.get('publish_channel_title') or store['name'])}</b>
          <div class="muted">ID {esc(str(channel_id))} · অটো-পোস্ট: {status}</div></div>
        {link_html}
      </div>
      <form method=post action="/admin/stores/{store_id}/channel/autopost" class="row">
        <label class="chk"><input type=checkbox name=enabled value=1{' checked' if auto_post else ''}>
          নতুন ভিডিও upload হলে এই চ্যানেলে পোস্ট করুন</label>
        <button class="ok" type="submit">💾 সংরক্ষণ</button>
      </form>
      <div class="row">{latest_button}</div>
      <p class="muted">এটি join gate নয়। Channel তৈরি ও পরিচালনায় আপনার session লাগে;
      post-এর জন্য bot-কে channel admin করা হয়।</p>
    </div>"""


def store_admin_body(store: dict, flash: str = "", warn: str = "") -> str:
    """Everything about one store in one page (the owner kept asking for this)."""
    stats = db.store_stats(store["id"])
    funnel = db.store_funnel(store["id"])
    plans = db.plans(store["id"])
    drip = db.drip(store["id"])
    link = ""
    if runtime.bot_username:
        link = f"https://t.me/{runtime.bot_username}?start=s{store['id']}"
    files = db.files_of(store["id"], newest_first=True)
    file_rows = "".join(
        f"<tr><td>#{f['id']}</td><td>{esc(f['name'])}</td><td>{esc(f['kind'])}</td>"
        f"<td>{f['views']}</td><td>{'✅' if f.get('mirror_msg') else '⏳'}</td>"
        f"<td><a class='btn' href='/admin/files/broadcast/{f['id']}'>📢</a> "
        f"<a class='btn grey' href='/admin/analytics/file/{f['id']}'>👥</a></td></tr>"
        for f in files[:100])
    grants = db.store_grants(store["id"])
    grant_rows = "".join(
        f"<tr><td>{_user_link(g['user_id'], g.get('user_name'))}</td>"
        f"<td>{_start(g.get('granted_at') or g.get('created_at'))}</td>"
        f"<td>{'♾' if g['expires_at'] is None else _start(g['expires_at'])}</td>"
        f"<td>{esc(g.get('source') or '')}</td></tr>" for g in grants[:100])
    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
    broadcast_button = (
        f'<a class="btn" href="/admin/files/broadcast/{files[0]["id"]}">📢 ব্রডকাস্ট</a>'
        if files else "")
    return f"""{note}{warnbox}
    <div class="page-heading"><div><p class="eyebrow">STORE WORKSPACE</p>
      <h2>{esc(store['name'])}</h2></div><span class="pill">#{store['id']}</span></div>
    <div class="grid">
      {_card("ফাইল", stats['files'])}{_card("ভিউ", stats['views'])}
      {_card("ক্যাশড", sum(1 for f in files if f.get('mirror_msg')))}
      {_card("অ্যাক্সেস", stats['grants'])}{_card("সাবস্ক্রাইবার", stats['subs'])}
      {_card("এসেছে", funnel['came'])}{_card("দেখেছে", funnel['watched'])}
      {_card("কিনেছে", funnel['paid'])}
    </div>
    <p class="row">
      {f'<a class="btn" href="{esc(link)}" target="_blank">▶️ বট লিংক</a>' if link else ''}
      <a class="btn grey" href="/s/{esc(store['slug'] or store['id'])}">🌐 সাইটে দেখুন</a>
      <a class="btn grey" href="/admin/files?store={store['id']}">🗂 ফাইল</a>
      <a class="btn grey" href="/admin/analytics/store/{store['id']}">📊 অ্যানালিটিক্স</a>
      {broadcast_button}
      <a class="btn" href="/admin/channels/compose?store={store['id']}">📡 চ্যানেলে পোস্ট</a>
    </p>
    <h4>✏️ স্টোরের তথ্য</h4>
    <div class="card">
      <form method=post action="/admin/stores/{store['id']}/edit">
        <label>নাম</label><input name=name value="{esc(store['name'])}" maxlength=100 required>
        <label>বর্ণনা</label><textarea name=description rows=3>{esc(store.get('description') or '')}</textarea>
        <label>স্টোরের ধরন</label>
        <select name=is_premium><option value="0"{' selected' if not store.get('is_premium') else ''}>Free — সবার জন্য</option>
          <option value="1"{' selected' if store.get('is_premium') else ''}>Premium — access লাগবে</option></select>
        <button class="ok" type=submit>💾 তথ্য সংরক্ষণ</button>
      </form>
    </div>
    {store_publish_channel_body(store)}
    {store_forcejoin_body(store)}
    <h4>💎 প্ল্যান ({len(plans)}) · 📅 ড্রিপ
      ({'চালু ' + str(drip['count']) + '/দিন ' + drip['send_time'] if drip and drip['enabled'] else 'বন্ধ'})</h4>
    <h4>📂 ফাইল (সর্বশেষ {min(len(files), 100)})</h4>
    <table><tr><th>#</th><th>নাম</th><th>ধরন</th><th>ভিউ</th><th>ক্যাশ</th><th></th></tr>
    {file_rows or "<tr><td colspan=6 class='muted'>ফাইল নেই</td></tr>"}</table>
    <h4>👥 অ্যাক্সেস দেওয়া ইউজার</h4>
    <table><tr><th>ইউজার</th><th>দেওয়া</th><th>শেষ</th><th>সোর্স</th></tr>
    {grant_rows or "<tr><td colspan=4 class='muted'>—</td></tr>"}</table>"""


# ------------------------------------------------------------- self test / logs
async def self_test() -> list[dict]:
    """One click → is everything actually working? (the owner's “কেন কাজ করছে না?”)"""
    from app import config as cfg
    from app.services import broadcast, media_cache
    from app.services.telegram import flood_status

    checks: list[dict] = []

    def add(name: str, ok: bool | None, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    stats = db.stats()
    add("ডেটাবেজ", True, f"{stats['users']} ইউজার · {stats['files']} ফাইল · {stats['stores']} স্টোর")
    add("বট", runtime.bot_online(), f"@{runtime.bot_username}" if runtime.bot_username else "ইউজারনেম নেই")
    sessions = db.all_sessions()
    online = sum(1 for row in sessions if row["admin_id"] in runtime.user_clients)
    add("ইউজারবট সেশন", online > 0 or not sessions,
        f"{online}/{len(sessions)} কানেক্টেড" if sessions else "কোনো সেশন নেই — আপলোড করা ফাইল বট নিজেই পাঠাতে পারে")

    cache = media_cache.stats()
    add("মিডিয়া ক্যাশ", cache["missing"] == 0,
        f"{cache['mirrored']}/{cache['files']} ফাইল সরাসরি বট থেকে যাবে"
        + (f" · {cache['missing']} টি বাকি" if cache["missing"] else ""))

    for target in forcejoin.targets():
        info = await forcejoin.ensure_target(target)
        add(f"চ্যানেল: {target.get('title') or target.get('ref')}", bool(info.get("ok")),
            f"id {info.get('chat_id')}" if info.get("ok") else str(info.get("error") or "")[:90])

    running = [c for c in db.campaigns(limit=20) if c["status"] in broadcast.ACTIVE_STATUSES]
    add("ব্রডকাস্ট কিউ", True, f"{len(running)} চলমান · মোট {len(db.campaigns(limit=200))} ক্যাম্পেইন")
    add("ফোর্স-জয়েন", True,
        f"{len(forcejoin.targets())} চ্যানেল সেট" if forcejoin.targets() else "বন্ধ (কেউ আটকাবে না)")
    stores = db.all_stores()
    add("স্টোর লিস্ট", bool(stores), f"{len(stores)} টি স্টোর সাইটে দেখা যাবে"
        if stores else "এখনো কোনো স্টোর নেই — প্রথমেই একটা স্টোর বানান")
    slugged = [s for s in stores if (s.get("slug") or "").strip()]
    add("স্টোর ডিপ-লিংক", len(slugged) == len(stores) and bool(runtime.bot_username),
        f"{len(slugged)}/{len(stores)} স্টোরের slug ঠিক আছে"
        + ("" if runtime.bot_username else " · বট ইউজারনেম এখনো পাওয়া যায়নি"))
    add("টেলিগ্রামের চাপ", not bool(flood_status()), flood_status() or "স্বাভাবিক")
    add("ওয়েব লগইন", True,
        "Password source code-এ স্থির; public repository-তে এটি দেখা যায়।")
    return checks


def self_test_body(checks: list[dict]) -> str:
    rows = "".join(
        f"<tr><td>{'✅' if c['ok'] else ('⚠️' if c['ok'] is None else '❌')}</td>"
        f"<td>{esc(c['name'])}</td><td class='muted'>{esc(c['detail'])}</td></tr>"
        for c in checks)
    bad = [c for c in checks if c["ok"] is False]
    head = ("<div class='flash'>সব ঠিক আছে ✅</div>" if not bad else
            f"<div class='warnbox'>⚠️ {len(bad)} টি জিনিস দেখতে হবে — নিচে লাল ❌ দেখুন</div>")
    return f"""{head}
    <h3>🩺 সেলফ-টেস্ট</h3>
    <table><tr><th></th><th>কী</th><th>অবস্থা</th></tr>{rows}</table>
    <p class="row">
      <a class="btn" href="/admin/self-test">🔄 আবার চালান</a>
      <a class="btn grey" href="/admin/cache/warm">📥 সব ফাইল ক্যাশ করুন</a>
      <a class="btn grey" href="/admin/logs">📜 লাইভ লগ</a>
    </p>"""


def logs_body(lines: list[str], level: str = "") -> str:
    joined = "".join(f"<div class='logline'>{esc(line)}</div>" for line in lines)
    return f"""<h3>📜 লাইভ লগ <span class="muted">(সর্বশেষ {len(lines)} লাইন)</span></h3>
    <p class="row">
      <a class="btn grey" href="/admin/logs">সব</a>
      <a class="btn grey" href="/admin/logs?level=ERROR">শুধু ERROR</a>
      <a class="btn grey" href="/admin/logs?level=WARNING">WARNING</a>
      <a class="btn grey" href="/admin/logs?level=INFO">INFO</a>
      <a class="btn" href="/admin/logs">🔄 রিফ্রেশ</a>
    </p>
    <div class="card" style="max-height:70vh;overflow:auto;font-family:ui-monospace,monospace;
         font-size:12.5px;line-height:1.5">{joined or '<span class="muted">লগ খালি</span>'}</div>"""


def files_tools_body() -> str:
    dups = db.duplicate_files()
    dup_rows = "".join(
        f"<tr><td>{esc(str(d['name']))}</td><td>{esc(str(d['store_id']))}</td>"
        f"<td>{d['c']}</td></tr>" for d in dups[:50])
    cache = db.count_files(), db.count_mirrors()
    return f"""<div class="card">
      <h4>🧹 টুল</h4>
      <p class="muted">ক্যাশ করা ফাইল বট নিজেই পাঠায় — তাই ক্লিক আর ব্রডকাস্ট দুটোই দ্রুত ও
         নিরাপদ (কোনো forward/delete নেই)।</p>
      <p class="row">
        <a class="btn" href="/admin/cache/warm">📥 সব ফাইল ক্যাশ করুন</a>
        <span class="pill">ক্যাশড {cache[1]}/{cache[0]}</span>
        <span class="pill">ডুপ্লিকেট {len(dups)}</span>
        <a class="btn warn" href="/admin/files/dedupe">🧹 ডুপ্লিকেট পরিষ্কার</a>
      </p>
      {f'<table><tr><th>নাম</th><th>স্টোর</th><th>কপি</th></tr>{dup_rows}</table>' if dups else ''}
    </div>


<div class="card">
  <h4>🗑 একসাথে অনেক ভিডিও মুছুন (mass delete)</h4>
  <p class="muted">নিচের বক্সে ফাইলের ID গুলো কমা বা নতুন লাইনে লিখুন — সব একবারে মুছে যাবে।
  ফাইল মুছলে তার লিংক/ড্রিপ/ফেভারিটও পরিষ্কার হয় (কোনো ভাঙা লিংক থাকে না)।</p>
  <form method=post action="/admin/files/bulk-delete"
        onsubmit="return confirm('সবগুলো ফাইল মুছে ফেলবেন? এটা ফেরানো যাবে না।')">
    <textarea name="ids" rows="3" placeholder="12, 15, 18"></textarea>
    <div class="row" style="margin-top:8px">
      <button class="btn bad" type="submit">🗑 সব মুছে ফেলুন</button>
      <span class="muted">টেবিলের ID কলাম দেখে নিন — ভুল ID হলে কিছুই মুছবে না।</span>
    </div>
  </form>
</div>"""


def users_tools_body(total: int = 0) -> str:
    """🧹 Mass-delete helpers for the 👥 Users page."""
    everyone = db.all_user_ids()
    stale = len(db.purge_user_ids(days=90, keep_admins=cfg.ADMIN_IDS)) if everyone else 0
    fresh = len(everyone) - stale
    return f"""<div class="card">
      <h4>🧹 ইউজার পরিষ্কার (mass delete)</h4>
      <p class="muted">টেবিলের বাঁ দিকের বক্সে টিক দিয়ে একসাথে অনেক ইউজার মুছে ফেলা যায়।
      অ্যাডমিন অ্যাকাউন্ট কখনো মুছবে না।</p>
      <p class="row">
        <span class="pill">মোট {total or len(everyone)} জন</span>
        <span class="pill">৯০ দিন অ্যাকটিভ {fresh} জন</span>
        <span class="pill">পুরোনো {stale} জন</span>
        <a class="btn warn" href="/admin/users/purge?days=90">🧹 পুরোনো ইউজার দেখুন</a>
      </p>
    </div>"""


def now_label() -> str:
    return fmt_ts(time.time(), "%d %b %Y %H:%M")
