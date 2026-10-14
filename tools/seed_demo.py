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
    "🎵 গানের ভান্ডার": ("free", [
        ("ফুওয়াদ আল মাহমুদ — নির্বাচিত", "Audio", 120 * 1024 ** 2, 720),
        ("Best of Coke Studio 2024", "Audio", 260 * 1024 ** 2, 530),
        ("Lo-fi Study Beats Vol.3", "Audio", 180 * 1024 ** 2, 350),
    ]),
}

NAMES = ["রহিম", "করিম", "সাদিয়া", "ইসরাত", "নাবিলা", "তানিম", "ফারহান", "লামিয়া",
         "নাঈম", "মিম", "রাফি", "সাদ", "অহনা", "রিয়াদ", "তাসনিম", "হাসান",
         "জান্নাত", "শাকিব", "প্রিয়া", "মেহেদী", "সুমাইয়া", "আরিফ", "নাফিসা", "রুবেল"]


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
