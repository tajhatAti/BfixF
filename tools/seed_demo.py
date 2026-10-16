"""Create a little demo content so the website/panel is not empty while testing.

    DB_FILE=preview.sqlite3 python tools/seed_demo.py

It only touches the database the `DB_FILE` environment variable points to, so it
can never touch production data by accident. Never run it on your live database.
"""
from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as cfg          # noqa: E402
from app.storage import db             # noqa: E402

ADMIN_ID = 111_000_001

DEMO = {
    "🎬 সিনেমা হাউস": ("premium", [
        ("Interstellar (2014) 1080p", "Video", 2.4 * 1024 ** 3, 4_512),
        ("Dune Part Two (2024)", "Video", 3.1 * 1024 ** 3, 3_980),
        ("Oppenheimer (2023) BD", "Video", 2.8 * 1024 ** 3, 3_410),
        ("The Batman (2022)", "Video", 2.2 * 1024 ** 3, 2_870),
        ("Spider-Man No Way Home", "Video", 2.0 * 1024 ** 3, 2_150),
        ("সরকারি বাঙ্ক (২০২৪)", "Video", 1.1 * 1024 ** 3, 1_980),
    ]),
    "🏪 ফ্রি কনটেন্ট": ("free", [
        ("কুরিয়ার সিরিজ S01E01", "Video", 0.7 * 1024 ** 3, 1_240),
        ("বার্তা E01 (ওয়েব সিরিজ)", "Video", 0.5 * 1024 ** 3, 980),
        ("Facebook Marketing Guide.pdf", "Document", 4.2 * 1024 ** 2, 640),
        ("Netflix Intro Pack.zip", "Document", 25 * 1024 ** 2, 410),
    ]),
    "🎞 রিল ডেমো (ফ্রি ভিডিও)": ("free", [
        ("কুরিয়ার সিরিজ S01E02", "Video", 0.6 * 1024 ** 3, 890),
        ("পদ্মা সেতু ডকুমেন্টারি", "Video", 0.4 * 1024 ** 3, 760),
        ("বাংলা কার্টুন — ঈদ স্পেশাল", "Video", 0.35 * 1024 ** 3, 640),
        ("কক্সবাজার ট্রাভেল ভ্লগ", "Video", 0.5 * 1024 ** 3, 520),
        ("ওয়েব সিরিজ পোস্টার", "Photo", 3 * 1024 ** 2, 410),
    ]),
    "🎵 গানের ভান্ডার": ("free", [
        ("ফুওয়াদ আল মাহমুদ — নির্বাচিত", "Audio", 120 * 1024 ** 2, 720),
        ("Best of Coke Studio 2024", "Audio", 260 * 1024 ** 2, 530),
        ("Lo-fi Study Beats Vol.3", "Audio", 180 * 1024 ** 2, 350),
    ]),
}

#: The clips the seeded preview install streams from disk (`chat_id = 0` in `files`,
#: see `app/services/feed.py`). Real Telegram needs a bot session; this is how the
#: reel and the “several at once” wall can be checked before one exists.
DEMO_CLIPS = [
    ("কুরিয়ার সিরিজ S01E01", "gradients=s=360x640:c0=0x0b1f3a:c1=0x1f6feb:d=6:speed=.10"),
    ("বার্তা E01 (ওয়েব সিরিজ)", "gradients=s=360x640:c0=0x241d12:c1=0xc9a45c:d=6:speed=.16"),
    ("পদ্মা সেতু ডকুমেন্টারি", "testsrc2=size=360x640:rate=25"),
    ("বাংলা কার্টুন — ঈদ স্পেশাল", "gradients=s=360x640:c0=0x2a1020:c1=0xe05c8a:d=6:speed=.22"),
]

NAMES = ["রহিম", "করিম", "সাদিয়া", "ইসরাত", "নাবিলা", "তানিম", "ফারহান", "লামিয়া",
         "নাঈম", "মিম", "রাফি", "সাদ", "অহনা", "রিয়াদ", "তাসনিম", "হাসান",
         "জান্নাত", "শাকিব", "প্রিয়া", "মেহেদী", "সুমাইয়া", "আরিফ", "নাফিসা", "রুবেল"]


def _ffmpeg() -> str | None:
    """A bundled ffmpeg, if the optional `imageio-ffmpeg` package is installed."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _run(ffmpeg: str, args: list[str]) -> bool:
    import subprocess
    try:
        done = subprocess.run([ffmpeg, "-v", "error", "-y", *args],
                              capture_output=True, timeout=120)
        return done.returncode == 0
    except Exception:
        return False


def seed_clips(admin_id: int) -> int:
    """Write a handful of tiny local clips + poster frames for the preview install.

    Silent, 9:16, six seconds each — long enough to show autoplay, the wall and the
    player, small enough (a few dozen KB) to be disposable.
    """
    from pathlib import Path
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        print("  • demo clips skipped — install imageio-ffmpeg for a playable preview")
        return 0
    out = Path(os.getenv("FEED_DEMO_DIR", "demo_media"))
    out.mkdir(parents=True, exist_ok=True)
    store = db.store_by_name(admin_id, "🎞 রিল ডেমো (ফ্রি ভিডিও)") \
        or db.create_store(admin_id, "🎞 রিল ডেমো (ফ্রি ভিডিও)")
    db.update_store(store["id"], is_premium=0)
    made = 0
    seconds = 6
    for index, (title, source) in enumerate(DEMO_CLIPS):
        msg_id = 900 + index
        clip = out / f"{msg_id}.mp4"
        frame = out / f"{msg_id}.jpg"
        if not clip.is_file() and not _run(ffmpeg, [
                "-f", "lavfi", "-i", source, "-t", str(seconds), "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "-an", str(clip)]):
            continue
        if not frame.is_file():
            _run(ffmpeg, ["-i", str(clip), "-frames:v", "1", "-q:v", "4", str(frame)])
        size = clip.stat().st_size if clip.is_file() else 0
        existing = next((row for row in db.files_of(store["id"])
                         if int(row.get("msg_id") or 0) == msg_id), None)
        if existing is None:
            db.add_file(store["id"], title, "Video", 0, msg_id=msg_id, size=size,
                        duration=seconds)
        made += 1
    # a poster photo too, so the reel/wall also shows the photo path
    poster = out / "990.jpg"
    if not poster.is_file():
        _run(ffmpeg, ["-f", "lavfi", "-i", "mandelbrot=s=720x1280:rate=1",
                      "-frames:v", "1", "-q:v", "4", str(poster)])
    if poster.is_file() and not next((row for row in db.files_of(store["id"])
                                      if int(row.get("msg_id") or 0) == 990), None):
        db.add_file(store["id"], "ওয়েব সিরিজ পোস্টার", "Photo", 0, msg_id=990,
                    size=poster.stat().st_size)
    print(f"  • demo clips in {out}: {made} videos + 1 photo")
    return made



def seed_visitors(admin_id: int = 0, days: int = 14) -> int:
    """Back-dated visitor history, so the 📈 ভিজিটর page has something to show.

    Real visits are recorded by the website itself (`app/services/visitors.py`);
    this only exists so an owner can *see* the page working before the site has
    had traffic. Never touches a production database (DB_FILE only).
    """
    import time

    pages = ["/", "/stores", "/watch", "/help", "/search?q=কুরিয়ার"]
    sources = ["সরাসরি", "টেলিগ্রাম", "গুগল", "ফেসবুক", "লিংক থেকে"]
    devices = [("📱 মোবাইল", "mobile", "Android", "Chrome"),
               ("💻 ডেস্কটপ", "desktop", "Windows", "Chrome"),
               ("📱 মোবাইল", "mobile", "iOS", "Safari")]
    countries = ["BD", "BD", "BD", "IN", "SA", "US"]
    names = ["রহিম", "করিম", "সাদিয়া", "ইসরাত", "নাবিলা", "তানিম", "ফারহান", "লামিয়া"]
    store_ids = [s["id"] for s in db.all_stores()] or [0]
    file_ids = [f["id"] for f in db.recent_public_media(limit=6)] or [0]
    made = 0
    for day in range(days - 1, -1, -1):
        for index in range(random.randint(3, 9)):
            when = time.time() - day * 86400 - random.randint(0, 20 * 3600)
            vid = f"demo{day:02d}{index:03d}{random.randint(1000, 9999)}"
            device, kind, os_name, browser = random.choice(devices)
            db.visitors.open_visit(
                vid, user_id=random.choice([0, 0, 0, admin_id]) if admin_id else 0,
                ip=f"103.{random.randint(1, 250)}.{random.randint(1, 250)}.{random.randint(1, 250)}",
                country=random.choice(countries), device=device, device_kind=kind,
                os=os_name, browser=browser, source=random.choice(sources),
                landing=random.choice(pages), user_agent="seeded")
            db._run("UPDATE visits SET started_at = ?, last_seen = ? WHERE vid = ?",
                    (when, when + random.randint(40, 900), vid))
            for step in range(random.randint(1, 6)):
                db._run("INSERT INTO page_views(vid, user_id, path, status, ms, is_admin, ts)"
                        " VALUES(?,?,?,200,?,0,?)",
                        (vid, 0, random.choice(pages), random.randint(40, 900),
                         when + step * 45))
            db._run("UPDATE visits SET pageviews = ? WHERE vid = ?",
                    (random.randint(1, 6), vid))
            for event in random.sample(["media_play", "store_open", "media_open", "bot_open",
                                        "search", "link_open"],
                                       k=random.randint(1, 3)):
                db._run("INSERT INTO visit_events(vid, user_id, name, detail, path, store_id,"
                        " file_id, ts) VALUES(?,?,?,?,?,?,?,?)",
                        (vid, 0, event, "", random.choice(pages),
                         random.choice(store_ids), random.choice(file_ids),
                         when + random.randint(20, 600)))
            made += 1
    # a few of them are “online right now”, so the live card is not empty
    for index in range(random.randint(2, 4)):
        vid = f"now{index}{random.randint(1000, 9999)}"
        db.visitors.open_visit(
            vid, ip=f"103.{random.randint(1, 250)}.{random.randint(1, 250)}.{random.randint(1, 250)}",
            country=random.choice(countries), device="📱 মোবাইল", device_kind="mobile",
            os="Android", browser="Chrome", source=random.choice(sources),
            landing=random.choice(pages), user_agent="seeded")
        db._run("INSERT INTO visit_events(vid, name, detail, path, file_id, ts)"
                " VALUES(?,?,?,?,?,?)",
                (vid, "media_play", "", "/", random.choice(file_ids), time.time()))
    print(f"  • visitors: {made + 4}টি ডেমো ভিজিট (শেষ {days} দিন)")
    return made

def main() -> int:
    db.bind(cfg.DB_FILE)
    print(f"Seeding demo data into {cfg.DB_FILE}")

    admin_id = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 111_000_001
    if cfg.ADMIN_IDS:
        db.touch_user(admin_id, "Admin", "admin")

    stores = {}
    for name, (mode, files) in DEMO.items():
        existing = db.store_by_name(admin_id, name)
        store = existing or db.create_store(admin_id, name)
        db.update_store(store["id"], is_premium=1 if mode == "premium" else 0,
                        description=("প্রিমিয়াম কালেকশন — নতুন ভিডিও প্রতি সপ্তাহে।"
                                     if mode == "premium" else
                                     "সবাই ফ্রি দেখতে পারবে।"))
        if not db.files_of(store["id"]):
            for index, (title, kind, size, views) in enumerate(files):
                file_id = db.add_file(store["id"], title, kind, -100_000 - store["id"],
                                      msg_id=100 + index, size=int(size))
                for _ in range(int(views / 120)):
                    db.bump_views(file_id)
        stores[name] = store
        print(f"  • {name}: {len(db.files_of(store['id']))} files")

    seed_clips(admin_id)
    seed_visitors(admin_id)

    # users + a few grants + one pending order
    premium = stores["🎬 সিনেমা হাউস"]
    all_users = db.all_user_ids()
    random.seed(7)
    for index, name in enumerate(NAMES):
        uid = 555_000_000 + index * 7 + 3
        db.touch_user(uid, name, f"user{index + 1}")
        if index % 3 == 0:
            db.grant(premium["id"], uid, time.time() + 30 * 86400, "demo")
        if index % 5 == 0:
            db.toggle_sub(stores["🎬 সিনেমা হাউস"]["id"], uid) if False else None

    if db.plans(premium["id"]) == []:
        db.add_plan(premium["id"], "১ মাস", 30, 199)
        db.add_plan(premium["id"], "৩ মাস", 90, 499)
        db.add_plan(premium["id"], "১ বছর", 365, 1499)

    users = [u for u in db.all_user_ids() if u != admin_id]
    print(f"  • users: {len(users)}")

    if not db.campaigns(limit=1):
        campaign = db.create_campaign(admin_id, title="আজকের নতুন ভিডিও",
                                      text="🎬 <b>আজকের নতুন ভিডিও!</b>\n\n"
                                           "Dune Part Two যোগ করা হয়েছে — এখনই দেখুন।",
                                      files=[db.files_of(premium["id"])[0]["id"]],
                                      audience="store:%d" % premium["id"], status="draft")
        print(f"  • demo campaign #{campaign}")

    # ------------------------------------------------ v3 demo: analytics & links
    # Per-user watch history, so the new analytics screens have something to show.
    if not db.events(limit=1):
        files = {row["id"]: row for row in db.newest_files(60)}
        for index, uid in enumerate(users[:18]):
            db.log_event("start", uid, None, None, "demo")
            for store in list(stores.values())[: 1 + index % 3]:
                store_files = db.files_of(store["id"])
                if not store_files:
                    continue
                db.log_event("open_store", uid, store["id"], None, store["slug"])
                for row in random.sample(store_files, min(2, len(store_files))):
                    db.log_event("deliver", uid, store["id"], row["id"], "demo")
                    db.bump_views(row["id"])
            if index % 4 == 0:
                db.log_event("join_block", uid, list(stores.values())[0]["id"], None, "demo")
            if index % 5 == 0:
                db.log_event("pay_start", uid, premium["id"], None, "plan:1")
                db.log_event("paid", uid, premium["id"], None, "order:demo")
        print("  • demo events logged")

    # Limited link (100 clicks) + unlimited link, so /admin/links is not empty.
    if not db.links(limit=1):
        newest = db.files_of(premium["id"])[:2]
        if newest:
            token = db.create_link(admin_id, [row["id"] for row in newest], None,
                                   kind="limited", max_clicks=100, note="ডেমো — ১০০ ক্লিক")
            print(f"  • limited link: https://t.me/<bot>?start=t{token} (100 clicks)")

    # A channel + a store-specific force channel for the demo panel.
    if not db.join_channels():
        db.add_join_channel(None, "@demo_channel", title="📢 ডেমো চ্যানেল", chat_id=-100_1111)
        db.set_store_forcejoin(premium["id"], "@demo_store_channel")
        db.add_join_channel(premium["id"], "@demo_store_channel",
                            title="🎬 সিনেমা হাউস — আলাদা চ্যানেল", chat_id=-100_2222)
        print("  • demo channels registered")

    print("Done. Open the website to see it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
