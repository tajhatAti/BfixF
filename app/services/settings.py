"""One place for every setting the owner can change — from the bot *or* the website.

Why this exists
    Values like the payment numbers, the force-join channel or the support
    contact used to live only in `config.env`, so changing them meant editing a
    file and restarting the hosting. Now they live in the database:

        website /admin/settings  ─┐
        bot  ⚙️ Settings screen  ─┼──►  settings.set("PAY_BKASH", "017…")
        (first boot / export)    ─┘     stored in the `meta` table

Precedence (highest wins)
    1. a value saved from the panel / website   → `meta` key `set:<NAME>`
    2. the environment variable / `config.env`  → the old behaviour, still works
    3. the built-in default below

So an existing `config.env` keeps working exactly as before, and anything the
owner saves in the UI simply overrides it — no restart, no file editing.
`settings.source("PAY_BKASH")` tells the UI which of the three is in play.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app import config as cfg
from app.logger import log
from app.storage import db

META_PREFIX = "set:"
CACHE_TTL = 10.0                      # seconds — keeps hot reads (caption, tz) cheap


@dataclass(frozen=True)
class Setting:
    key: str
    label: str                        # বাংলা নাম (প্যানেলে যা দেখাবে)
    group: str
    kind: str = "text"                # text | long | int | float | bool | choice
    hint: str = ""
    placeholder: str = ""
    default: Any = ""
    env: str = ""                     # equivalent environment variable name
    choices: tuple = ()               # for kind="choice": the allowed values


@dataclass
class Group:
    key: str
    label: str
    icon: str = "⚙️"
    blurb: str = ""
    settings: list[Setting] = field(default_factory=list)


def _s(key: str, label: str, group: str, **kwargs) -> Setting:
    return Setting(key=key, label=label, group=group, env=key, **kwargs)


# ------------------------------------------------------------------- registry
REGISTRY: list[Group] = [
    Group(
        key="emergency", label="জরুরি সেটিংস (Emergency)", icon="🚨",
        blurb="যেসব জিনিস বিগড়ালে ভিডিও বা বট আটকে যায় — সব এক জায়গায়। "
              "সমস্যা হলে আগে এখানে দেখুন (দ্রুত বাটনের জন্য: /admin/emergency)।",
        settings=[
            _s("STORAGE_CHANNEL", "মিডিয়া ক্যাশ চ্যানেল (ঐচ্ছিক)", "content",
               placeholder="@my_storage অথবা -1001234567890",
               hint="এখানে একবার করে ফাইল কপি হয় — তারপর সব ইউজার সরাসরি বট থেকে পায়"),
            _s("PER_STORE_CHANNEL", "প্রতি স্টোরের জন্য আলাদা ক্যাশ চ্যানেল", "content",
               kind="bool", default=True,
               hint="প্রথম ভিডিও দিলেই সেশন দিয়ে চ্যানেল খুলে সেখানে আপলোড হবে"),
            _s("VIDEO_COPY_DELAY", "ভিডিও কপির মধ্যে বিরতি (সেকেন্ড)", "content",
               kind="int", default=0, hint="সর্বোচ্চ ৩টি ভিডিও একসঙ্গে; ০–৩০ সেকেন্ড অতিরিক্ত বিরতি, FloodWait সবসময় মানবে"),
            _s("MIRROR_MAX_MB", "ক্যাশ করার সর্বোচ্চ ফাইল সাইজ (MB)", "content",
               kind="int", default=1900,
               hint="এর চেয়ে বড় ফাইল বট সরাসরি পাঠাবে না (সেশন লাগবে)"),
            _s("MEDIA_CACHE_MAX_MB", "ওয়েবসাইট প্লেয়ারের জন্য সর্বোচ্চ ফাইল (MB)", "content",
               kind="int", default=64,
               hint="এই সাইজ পর্যন্ত ফাইল সাইটে ক্যাশ হয়ে সাথে সাথে চলে (বড় ফাইল সরাসরি "
                    "টেলিগ্রাম থেকে স্ট্রিম হয়)"),
            _s("MEDIA_CACHE_TOTAL_MB", "ওয়েবে ক্যাশ রাখার মোট সীমা (MB)", "content",
               kind="int", default=1024,
               hint="সীমা ছাড়ালে সবচেয়ে পুরোনো ক্যাশ নিজে থেকেই মুছে যায়"),
            _s("VISITOR_KEEP_DAYS", "ভিজিটর হিসাব কত দিন রাখবেন", "content",
               kind="int", default=90,
               hint="এর পুরোনো ভিজিট/ক্লিকের রেকর্ড নিজে থেকেই মুছে যায় "
                    "(/admin/visitors পেজেও বদলানো যায়)"),
            _s("MAINTENANCE_MODE", "মেইনটেন্যান্স মোড (ইউজারদের জন্য বট বন্ধ)", "emergency",
               kind="bool", default=False,
               hint="চালু থাকলে শুধু admin বট ব্যবহার করতে পারবে। বটে: /maint on | /maint off"),
            _s("MAINTENANCE_MESSAGE", "মেইনটেন্যান্সের বার্তা", "emergency", kind="long",
               default="🛠 বট এখন একটু মেইনটেন্যান্সে আছে। কিছুক্ষণ পর আবার চেষ্টা করুন।"),
            _s("BACKUP_TO_TELEGRAM", "প্রতিদিনের ব্যাকআপ টেলিগ্রামে admin-এর কাছে পাঠান", "emergency",
               kind="bool", default=True,
               hint="হোস্টিং ডিস্ক মুছে গেলেও ডাটা আপনার টেলিগ্রামে থেকে যাবে। এখনই: /backup"),
        ],
    ),
    Group(
        key="force_join", label="চ্যানেল জয়েন (Force Join)", icon="📢",
        blurb="ইউজার ফাইল পেতে আগে চ্যানেলে জয়েন করতে হবে। প্রাইভেট ইনভাইট লিংক, "
              "@username বা চ্যানেল আইডি — যেকোনোটা চলবে।",
        settings=[
            _s("GROUP_PROMO_ENABLED", "গ্রুপে /start দিলে ফ্রি ভিডিও", "force_join", kind="bool", default=True),
            _s("GROUP_PROMO_COOLDOWN", "একই গ্রুপে ভিডিওর বিরতি (সেকেন্ড)", "force_join", kind="int", default=60),
            _s("GROUP_PROMO_URL", "প্রমোশন বাটনে নিজের গ্রুপের লিংক", "force_join", placeholder="https://t.me/yourgroup"),
            _s("FORCE_JOIN_ENABLED", "জয়েন বাধ্যতামূলক", "force_join", kind="bool",
               default=True, hint="বন্ধ করলে কেউ আটকাবে না"),
            _s("FORCE_CHANNEL", "চ্যানেল (লিংক / @username / আইডি)", "force_join",
               placeholder="@mychannel অথবা https://t.me/+AbCdEfGh",
               hint="প্রাইভেট চ্যানেল হলে ইনভাইট লিংক দিন — বট সেই লিংক থেকে চ্যানেল খুঁজে নেবে"),
            _s("FORCE_JOIN_NOTE", "জয়েন করার সময় ইউজারকে যা দেখাবে", "force_join",
               kind="long", hint="যেমন: জয়েন করে আবার /start দিন"),
            _s("FORCE_JOIN_ALWAYS", "সব স্টোরে ডিফল্ট চ্যানেলও বাধ্যতামূলক", "force_join",
               kind="bool", default=True,
               hint="বন্ধ করলে স্টোর-ভিত্তিক চ্যানেল আলাদা থাকবে"),
            _s("FORCE_JOIN_EXTRA", "ডিফল্ট চ্যানেলের সাথে যুক্ত আরও চ্যানেল", "force_join",
               kind="long", placeholder="@channel1, https://t.me/+AbCdEfGh",
               hint="কমা/নতুন লাইনে আলাদা করুন — সবগুলোতেই জয়েন করতে হবে"),
        ],
    ),
    Group(
        key="payments", label="পেমেন্ট", icon="💳",
        blurb="এই নাম্বারগুলো ইউজার প্রিমিয়াম কেনার সময় দেখবে। যেটা খালি রাখবেন সেটা "
              "লুকানো থাকবে।",
        settings=[
            _s("CURRENCY", "মুদ্রা প্রতীক", "payments", default="৳", placeholder="৳ / ₹ / $"),
            _s("PAY_BKASH", "bKash", "payments", placeholder="01712-345678 (Personal)"),
            _s("PAY_NAGAD", "Nagad", "payments", placeholder="01812-345678 (Personal)"),
            _s("PAY_ROCKET", "Rocket", "payments", placeholder="017123456789-1"),
            _s("PAY_UPI", "UPI / অন্যান্য", "payments", placeholder="yourname@upi"),
            _s("PAY_CRYPTO", "Crypto", "payments", placeholder="USDT TRC20: T…"),
            _s("PAY_NOTE", "পেমেন্ট নির্দেশনা (ঐচ্ছিক)", "payments", kind="long",
               hint="যেমন: Send Money করে ট্রানজেকশন আইডি পাঠান — স্ক্রিনশটও চলবে"),
            _s("TRIAL_HOURS", "ফ্রি ট্রায়াল (ঘণ্টা)", "payments", kind="int", default=24,
               hint="0 দিলে ট্রায়াল বন্ধ"),
            _s("AUTO_APPROVE_ZERO", "ফ্রি প্ল্যান অটো-অ্যাপ্রুভ", "payments", kind="bool",
               default=True),
        ],
    ),
    Group(
        key="support", label="সাপোর্ট / যোগাযোগ", icon="📞",
        blurb="ইউজার “📞 অ্যাডমিনের সাথে কথা বলুন” চাপলে যে তথ্য দেখবে (মেসেজ সবসময় "
              "বটেই আসে, তাই কিছু সেট না করলেও কাজ করে)।",
        settings=[
            _s("SUPPORT_CONTACT", "কন্টাক্ট (যেকোনো কিছু)", "support",
               placeholder="@myusername / 01712-345678 / https://t.me/mychat"),
            _s("SUPPORT_NOTE", "সাপোর্ট সময়/নির্দেশনা", "support",
               hint="যেমন: সকাল ১০টা – রাত ১০টা"),
            _s("TICKET_COOLDOWN", "একই ইউজার কত সেকেন্ড পর আবার মেসেজ দিতে পারবে",
               "support", kind="int", default=60),
        ],
    ),
    Group(
        key="content", label="কনটেন্ট ও ভাষা", icon="📝",
        blurb="ফাইল পাঠানোর সময়ের ক্যাপশন, স্বাগতম বার্তা ও ডিফল্ট ভাষা।",
        settings=[
            _s("CUSTOM_CAPTION", "প্রতি ফাইলের ক্যাপশন", "content", kind="long",
               hint="{name} দিলে ফাইলের নাম বসবে"),
            _s("STRIP_TAGS", "নাম ও ক্যাপশন থেকে @ট্যাগ ও t.me লিংক মুছুন", "content",
               kind="bool", default=True,
               hint="অন্য চ্যানেলের ট্যাগ স্টোরে দেখাবে না"),
            _s("DEFAULT_LANG", "ডিফল্ট ভাষা", "content", default="bn",
               placeholder="bn অথবা en"),
            _s("CONTENT_MEDIA_KINDS", "স্টোরে কী কী গ্রহণযোগ্য", "content",
               default="video,photo",
               hint="ডিফল্ট: video,photo — টেক্সট কখনোই সেভ হয় না"),
            _s("LINK_DEFAULT_LIMIT", "লিমিটেড লিংকের ডিফল্ট ক্লিক", "content",
               kind="int", default=100,
               hint="0 = আনলিমিটেড; লিংক বানানোর সময় আলাদাভাবে বদলানো যায়"),
            _s("BROADCAST_SEND_ONLINE_BUTTON", "ব্রডকাস্টে “অনলাইন” বাটন যোগ করুন",
               "content", kind="bool", default=True,
               hint="মেসেজের নিচে স্টোর খোলার ইনলাইন বাটন বসবে"),
            _s("DELIVER_FORMAT_SEPARATOR", "প্রতিটি ফাইলের আগে আলাদা মেসেজ",
               "content", kind="bool", default=False),
        ],
    ),
    Group(
        key="broadcast", label="ব্রডকাস্ট গতি", icon="📣",
        blurb="ধীর গতি = অ্যাকাউন্ট নিরাপদ; দ্রুত গতি = সময় কম।",
        settings=[
            _s("BROADCAST_MAX_FILES", "এক মেসেজে সর্বোচ্চ ফাইল", "broadcast",
               kind="int", default=5),
            _s("BROADCAST_AUTO_RESUME", "রিস্টার্টের পর মাঝপথে থেমে যাওয়া ব্রডকাস্ট চালু করবে",
               "broadcast", kind="bool", default=True,
               hint="Admin-এর নিশ্চিত করা queued campaign bot online হলেই সবসময় শুরু হবে"),
            _s("BROADCAST_REACH_DAYS", "কত দিন সক্রিয় ইউজারকে পাঠাবে", "broadcast",
               kind="int", default=0,
               hint="0 = সব ইউজার (ব্লক করা বাদে)"),
            _s("BROADCAST_BATCH", "এক ব্যাচে কতজনকে (ফ্লাড এড়াতে)", "broadcast", kind="int",
               default=25, hint="Telegram-এর সীমা ~৩০ মেসেজ/সেকেন্ড; ২৫ নিরাপদ"),
            _s("BROADCAST_BATCH_PAUSE", "প্রতি ব্যাচের পরে বিরতি (সেকেন্ড)", "broadcast",
               kind="float", default=1.0),
            _s("BROADCAST_ADAPTIVE", "ফ্লাড খেলে নিজে থেকেই ধীরে চলবে", "broadcast",
               kind="bool", default=True,
               hint="Telegram 429 দিলে বাকি ব্রডকাস্টের গতি নিজে থেকেই কমানো হয়"),
            _s("BROADCAST_TO_BLOCKED", "ব্লক করা ইউজারকেও আবার চেষ্টা করবে",
               "broadcast", kind="bool", default=False),
            _s("BROADCAST_BUTTONS_ON", "বাটন কোন মেসেজে বসবে", "broadcast", kind="choice",
               default="last", choices=("last", "each", "none"),
               hint="last = শেষ মেসেজে (আগের মতো) · each = প্রতিটি মেসেজে · "
                    "none = বাটন ছাড়া"),
            _s("DIGEST_ENABLED", "সাপ্তাহিক নতুন-ভিডিও ডাইজেস্ট", "broadcast",
               kind="bool", default=False,
               hint="চালু করলে সপ্তাহে একবার নতুন আপলোড নিজে থেকেই সব ইউজারকে যাবে"),
            _s("DIGEST_WEEKDAY", "ডাইজেস্টের দিন (0=সোম … 6=রবি)", "broadcast",
               kind="int", default=4, hint="4 = শুক্রবার"),
            _s("DIGEST_HOUR", "ডাইজেস্টের সময় (ঘণ্টা, টাইমজোন অনুযায়ী)", "broadcast",
               kind="int", default=10),
            _s("DIGEST_MAX_FILES", "ডাইজেস্টে সর্বোচ্চ ভিডিও", "broadcast",
               kind="int", default=2),
            _s("BROADCAST_ONLINE_LABEL", "“অনলাইন” বাটনের লেখা", "broadcast",
               default="🟢 অনলাইন — স্টোর খুলুন"),
            _s("BROADCAST_DEFAULT_BUTTONS", "ব্রডকাস্টের ডিফল্ট বাটন", "broadcast",
               kind="long", placeholder="📢 জয়েন | https://t.me/mychannel",
               hint="প্রতি লাইনে একটা: লেবেল | লিংক  ·  একই লাইনে && দিলে পাশাপাশি"),
            _s("FLOW_TIMEOUT_SECONDS", "প্রশ্ন কত সেকেন্ড পর্যন্ত সক্রিয় থাকবে", "content",
               kind="int", default=600,
               hint="এই সময়ের মধ্যে উত্তর না দিলে বট প্রশ্নটা বাতিল করে দেয় "
                    "(আগে পুরোনো প্রশ্ন দিনের পর দিন সক্রিয় থাকত — টেক্সট বারবার সেভ হচ্ছিল)"),
            _s("OWNER_REPORT_ENABLED", "দৈনিক রিপোর্ট (অ্যাডমিনকে DM)", "broadcast",
               kind="bool", default=True,
               hint="প্রতিদিন ইউজার/ডেলিভারি/গেট/আয়/ক্যাশের হিসাব সরাসরি মেসেজে"),
            _s("OWNER_REPORT_BACKUP", "রিপোর্টের সঙ্গে পূর্ণ ব্যাকআপ", "broadcast",
               kind="bool", default=True, hint="টোকেনসহ ব্যক্তিগত ব্যাকআপ — নিরাপদে রাখুন"),
            _s("OWNER_REPORT_HOUR", "রিপোর্ট পাঠানোর সময় (০-২৩)", "broadcast",
               kind="int", default=21),
            _s("AUTO_POST_ENABLED", "নতুন ভিডিও অটো-পোস্ট (চ্যানেলে)", "broadcast",
               kind="bool", default=False,
               hint="স্টোরে ভিডিও যোগ করা মাত্রই চ্যানেলে ইনলাইন বাটনসহ পোস্ট হবে"),
            _s("AUTO_POST_CHANNEL", "অটো-পোস্টের চ্যানেল", "broadcast",
               placeholder="@mychannel অথবা -100…",
               hint="খালি থাকলে 📡 Channels-এর প্রথম চ্যানেলে যাবে"),
            _s("AUTO_POST_LINK_LIMIT", "অটো-পোস্টে লিমিটেড লিংক", "broadcast",
               kind="int", default=0,
               hint="0 = ভিডিও নিজেই পোস্ট হবে · 100 লিখলে “ভিডিও নিন (১০০ বার)” বাটন যাবে"),
            _s("AUTO_POST_TEXT", "অটো-পোস্টের লেখা", "broadcast", kind="long",
               default="🎬 <b>নতুন যোগ হলো</b>\n{name}\n\n👇 নিচের বাটনে চাপ দিন।",
               hint="{name} = ফাইলের নাম"),
            _s("CHANNEL_POST_FOOTER", "চ্যানেল পোস্টের নিচের লেখা", "broadcast",
               kind="long", hint="প্রতিটি চ্যানেল পোস্টের শেষে বসবে"),
        ],
    ),
    Group(
        key="alerts", label="নোটিফিকেশন (অ্যাডমিন অ্যালার্ট)", icon="🔔",
        blurb="নতুন ইউজার, নতুন অর্ডার, নতুন মেসেজ বা আপলোডে আটকে গেলে অ্যাডমিনকে "
              "সাথে সাথে জানাবে। অনেক ভিড় হলে ALERT_CHANNEL-এ একটা প্রাইভেট গ্রুপ "
              "দিন — সব অ্যালার্ট সেখানেই জমা হবে।",
        settings=[
            _s("ALERT_ENABLED", "সব নোটিফিকেশন চালু", "alerts", kind="bool", default=True),
            _s("ALERT_NEW_USER", "নতুন ইউজার বটে এলেই জানাও", "alerts", kind="bool",
               default=True),
            _s("ALERT_ORDER", "নতুন অর্ডার/পেমেন্ট এলেই জানাও", "alerts", kind="bool",
               default=True),
            _s("ALERT_TICKET", "নতুন সাপোর্ট মেসেজ এলেই জানাও", "alerts", kind="bool",
               default=True),
            _s("ALERT_STUCK_UPLOAD", "কেউ আপলোডে আটকে গেলে জানাও", "alerts", kind="bool",
               default=True,
               hint="ভিডিও/ছবির বদলে অন্য কিছু পাঠালে বা আপলোডে সমস্যা হলে অ্যালার্ট"),
            _s("ALERT_BROADCAST", "ব্রডকাস্ট শেষ হলে রিপোর্ট", "alerts", kind="bool",
               default=True),
            _s("ALERT_CHANNEL", "অ্যালার্ট কোথায় যাবে", "alerts",
               placeholder="@my_admin_inbox / -1001234567890 (খালি = অ্যাডমিনের DM)"),
        ],
    ),
    Group(
        key="site", label="ওয়েবসাইট", icon="🌐",
        blurb="পাবলিক সাইটের নাম, ট্যাগলাইন ও টাইমজোন।",
        settings=[
            _s("WEB_TITLE", "সাইটের নাম", "site", default="Desi Site"),
            _s("SITE_URL", "সাইটের পাবলিক URL", "site",
               default="https://codenestauth5.onrender.com/live/u3-esi-38a23a", placeholder="https://example.com"),
            _s("SITE_TAGLINE", "সাইটের ট্যাগলাইন", "site", kind="long"),
            _s("SITE_CONTACT", "সাইটে দেখানোর যোগাযোগ", "site"),
            _s("FEED_ENABLED", "হোম পেজে সদ্য যোগ হওয়া ভিডিও (অটো-প্লে)", "site",
               kind="bool", default=True,
               hint="শুধু ফ্রি স্টোরের ভিডিও দেখায়; প্রিমিয়াম স্টোরের ভিডিও কখনো না"),
            _s("FEED_COUNT", "হোম পেজে কয়টি মিডিয়া (১–৮)", "site", kind="int", default=4),
            _s("FEED_WALL", "একসাথে কয়টি অটো-প্লে হবে (ওয়াল, ০ = বন্ধ)", "site", kind="int",
               default=4, hint="৩–৪ দিলে হোম পেজে ঠিক ততগুলো ভিডিও একসাথে নিজে নিজেই চলতে থাকে"),
            _s("FEED_AUTOPLAY", "নিজে থেকেই চালু হবে (মিউট)", "site", kind="bool", default=True,
               hint="ব্রাউজার শুধু মিউট ভিডিও নিজে চালু করতে দেয় — সাউন্ড বাটন দিয়ে চালু করা যায়"),
            _s("FEED_MUTED", "শুরুতে সাউন্ড বন্ধ", "site", kind="bool", default=True),
            _s("FEED_LOOP", "শেষ হলে আবার শুরু (লুপ)", "site", kind="bool", default=True),
            _s("FEED_GALLERY_COUNT", "মিডিয়া পেজে কয়টি আইটেম", "site", kind="int", default=36),
            _s("AUTO_STORE_PUBLISH_CHANNEL", "নতুন স্টোর বানালেই আলাদা পাবলিশ চ্যানেল বানাও",
               "site", kind="bool", default=False,
               hint="প্রতি স্টোরের ভিডিও আলাদা চ্যানেলে যায়; ক্যাশ চ্যানেল সবসময় নিজে থেকেই তৈরি হয়"),
            _s("STORAGE_CHANNEL_WARNING", "ক্যাশ চ্যানেল সমস্যা (বট নিজে লিখে রাখে)", "site",
               kind="long", hint="খালি থাকলে সব ঠিক আছে; কোনো চ্যানেল খুঁজে না পেলে বট "
                                 "এখানে সমাধান লিখে রাখে"),
            _s("TZ_OFFSET", "টাইমজোন (UTC থেকে ঘণ্টা)", "site", kind="float",
               default=6.0, hint="বাংলাদেশ = 6"),
        ],
    ),
]

_BY_KEY: dict[str, Setting] = {s.key: s for g in REGISTRY for s in g.settings}
_GROUPS: dict[str, Group] = {g.key: g for g in REGISTRY}
_cache: dict[str, tuple[float, Any, Any]] = {}     # key -> (expiry, value, env snapshot)


def setting(key: str) -> Setting | None:
    return _BY_KEY.get(key)


def keys() -> list[str]:
    return list(_BY_KEY)


def group_of(key: str) -> Group | None:
    item = _BY_KEY.get(key)
    return _GROUPS.get(item.group) if item else None


def all_groups() -> list[Group]:
    return REGISTRY


# ------------------------------------------------------------------ coercion
def _env_value(key: str, default: Any) -> Any:
    raw = getattr(cfg, key, None)
    return default if raw in (None, "") else raw


def _coerce(item: Setting, raw: Any) -> Any:
    if isinstance(raw, str):
        raw = raw.strip()
    if item.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on", "ha", "hae", "চালু"}
    if item.kind == "int":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return int(item.default or 0)
    if item.kind == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(item.default or 0)
    return "" if raw is None else str(raw)


# ---------------------------------------------------------------------- read
def get(key: str, default: Any = None) -> Any:
    """DB override → environment → built-in default (caller default wins last)."""
    item = _BY_KEY.get(key)
    if item is None:
        return default
    # The registry default is the real fallback. `default` is only used when the
    # registry has nothing either — otherwise every `get_int(...)` call (which
    # passes 0) would silently wipe out defaults like TRIAL_HOURS = 24.
    fallback = item.default
    if fallback in (None, ""):
        fallback = default
    env_now = _env_value(key, fallback)
    cached = _cache.get(key)
    # The cache also remembers the environment value it was built from, so a
    # changed env (tests, or a real env edit) is picked up immediately.
    if cached and cached[0] > time.time() and cached[2] == env_now:
        return cached[1]

    stored = db.get_meta(META_PREFIX + key, "")
    if stored != "":
        value = _coerce(item, stored)
    else:
        value = _coerce(item, env_now)
    _cache[key] = (time.time() + CACHE_TTL, value, env_now)
    return value


def get_str(key: str, default: str = "") -> str:
    value = get(key, default)
    text = "" if value is None else str(value)
    if key == "WEB_TITLE" and text.lower().replace(" ", "") == "ahadorgex":
        return "Desi Site"
    return text


def get_int(key: str, default: int = 0) -> int:
    return _coerce(_BY_KEY[key], get(key, default)) if key in _BY_KEY else int(default)


def get_float(key: str, default: float = 0.0) -> float:
    return _coerce(_BY_KEY[key], get(key, default)) if key in _BY_KEY else float(default)


def get_bool(key: str, default: bool = False) -> bool:
    return bool(_coerce(_BY_KEY[key], get(key, default))) if key in _BY_KEY else default


def source(key: str) -> str:
    """`db` (panel), `env` (config.env) or `default` — shown in the UI."""
    item = _BY_KEY.get(key)
    if item is None:
        return "unknown"
    if db.get_meta(META_PREFIX + key, "") != "":
        return "db"
    if getattr(cfg, key, "") not in (None, ""):
        return "env"
    return "default"


def as_dict(group: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in _BY_KEY.items():
        if group and item.group != group:
            continue
        out[key] = get(key)
    return out


# ---------------------------------------------------------------------- write
def validate(key: str, raw: Any) -> tuple[bool, Any, str]:
    """Return (ok, cleaned_value, error_message)."""
    item = _BY_KEY.get(key)
    if item is None:
        return False, raw, "অজানা সেটিং"
    value = str(raw or "").strip()
    if item.kind == "int":
        if value and not value.lstrip("-").isdigit():
            return False, raw, "সংখ্যা দিন"
        value = int(value) if value else 0
        if item.key in ("TRIAL_HOURS", "TICKET_COOLDOWN",
                        "BROADCAST_MAX_FILES") and value < 0:
            return False, raw, "ঋণাত্মক সংখ্যা চলবে না"
        return True, value, ""
    if item.kind == "float":
        try:
            number = float(value) if value else float(item.default or 0)
        except ValueError:
            return False, raw, "সংখ্যা দিন"
        if number < 0:
            return False, raw, "ঋণাত্মক সংখ্যা চলবে না"
        return True, number, ""
    if item.kind == "bool":
        return True, _coerce(item, value), ""
    if item.kind == "choice":
        allowed = tuple(item.choices or ())
        if value not in allowed:
            return False, raw, "এই অপশনগুলোর একটি দিন: " + ", ".join(allowed)
        return True, value, ""
    if item.key == "FORCE_CHANNEL" and value:
        from app.services import forcejoin
        cleaned = forcejoin.normalize(value)
        if not cleaned:
            return False, raw, "চ্যানেল চেনা গেল না — @username বা t.me লিংক দিন"
        return True, cleaned, ""
    if item.key == "DEFAULT_LANG" and value and value not in ("bn", "en"):
        return False, raw, "bn অথবা en দিন"
    return True, value, ""


def set(key: str, value: Any, *, validate_first: bool = True) -> tuple[bool, str]:
    """Save a setting (empty value clears the override → back to env/default)."""
    item = _BY_KEY.get(key)
    if item is None:
        return False, "অজানা সেটিং"
    if validate_first:
        ok, cleaned, error = validate(key, value)
        if not ok:
            return False, error
        value = cleaned
    text = "" if value is None else str(value)
    if item.kind == "bool":
        text = "true" if _coerce(item, value) else "false"
    if text.strip() == "" and item.kind not in ("bool",):
        db.set_meta(META_PREFIX + key, "")
    else:
        db.set_meta(META_PREFIX + key, text)
    _cache.pop(key, None)
    log.info("Setting %s updated from the panel", key)
    return True, ""


def set_many(values: dict[str, Any]) -> dict[str, str]:
    """Save a whole form; returns {key: error} for the fields that failed."""
    errors: dict[str, str] = {}
    for key, raw in values.items():
        ok, message = set(key, raw)
        if not ok:
            errors[key] = message
    return errors


def reset(key: str) -> None:
    """Forget the panel value — the environment value (if any) applies again."""
    db.set_meta(META_PREFIX + key, "")
    _cache.pop(key, None)


def env_preview() -> str:
    """A ready-to-paste `config.env` snippet of the current values."""
    lines = ["# নিজের মানগুলো (প্যানেল থেকে এক্সপোর্ট করা)", ""]
    last_group = ""
    for group in REGISTRY:
        lines.append(f"# ---------- {group.label} ----------")
        for item in group.settings:
            value = get(item.key)
            if item.kind == "bool":
                value = "true" if value else "false"
            if value == "" and item.default == "":
                lines.append(f"# {item.key}=")
            else:
                lines.append(f"{item.key}={value}")
        lines.append("")
        last_group = group.key
    return "\n".join(lines).rstrip() + "\n"
