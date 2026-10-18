# Telegram / group promotion update

## চালানোর আগে

- নতুন build: `desi-2026.10.18-supabase / <hash>`। `/version` দিয়ে মিলিয়ে নিন।
- Telegram-এর command menu-তে শুধু `/start`। Admin-ও একই menu পাবেন। `/help` লিখলে
  পূর্ণ admin catalogue; সাধারণ ইউজার admin catalogue দেখবেন না। Help বাটন নিজে
  থেকে পূর্ণ admin list ঢেলে দেবে না। Startup notice-ও সংক্ষিপ্ত।
- Error alert মালিকের DM-এ সংক্ষিপ্ত; redacted বিস্তারিত owner-only `/errors`-এ।

## Force Join

সাইটের Settings → Force Join অথবা বটের Settings থেকে channel দিন ও enabled রাখুন।
**বটকে প্রতিটি বাধ্যতামূলক চ্যানেলের admin করতে হবে।** Private channel হলে invite
link-ও দিতে হবে। শুধু numeric ID দিলে join button বানানোর জন্য invite link লাগতে পারে।

দুটি প্রকৃত bug ঠিক হয়েছে: `GetParticipantRequest`-এর parameter `participant`,
`user_id` নয়; `safe_call`-এ callable client দিতে হবে, আগে বানানো coroutine নয়।
সদস্যপদ যাচাই সম্ভব না হলে এখন required gate আটকাবে; ভুল permission বা network outage
আগের মতো গোপনে gate bypass করবে না। Admin-এর নিজস্ব bypass বজায় আছে।

Private `/start` এবং deep-link payload global gate-এ আটকে থাকলে payload DB-তে থাকে;
সঠিক ব্যবহারকারী join-check চাপলে মূল কাজ আবার শুরু হয়। Global gate অন্য private
content operations/callback-এরও আগে চলে। `/help` ও `/whoami` diagnostics পাওয়া যায়।
স্টোর ও limited-link-এর নিজস্ব channels-ও যাচাই হয়।

## Group /start ও promotion

- গ্রুপে `/start` বা `/start@YourBot` দিলে আগে sender-এর join যাচাই হয়।
- Join prompt-এ channel URL ও sender-bound recheck button। যোগ দিয়ে check চাপলে
  আবার membership যাচাই করে ভিডিও দেয়; অন্যের button দিয়ে gate পার হওয়া যায় না।
- সফল হলে একটি free/unlocked Video পাঠায়; paid/locked/photo নয়। পরেরবার সম্ভব হলে
  অন্য ভিডিও। Store-specific join requirement-ও বজায় থাকে। `/start s123` দিয়ে একটি
  নির্দিষ্ট store বাছা যায়; সেটি premium হলে কোনো premium content বের হয় না।
- ভিডিও caption ফাঁকা; inline buttons-এ bot deep link, force-channel link এবং নিজের
  গ্রুপে bot যোগ করার Telegram chooser। `GROUP_PROMO_URL`-এ নিজের public group link
  দিলে সেটির বাটনও আসে। Settings → Force Join-এ promotion controls আছে।
- একই গ্রুপে cooldown ডিফল্ট ৬০ সেকেন্ড, সর্বনিম্ন ৩০। Disable switch আছে। বটকে
  গ্রুপে যোগ করার event পেলেও adder-এর permission/join পরীক্ষা করে একই flow চলে।
- সব গ্রুপে একসঙ্গে স্বয়ংক্রিয় যোগ করা হয় না; ব্যবহারকারী নিজের অনুমোদিত group
  বেছে Telegram-এ confirm করবেন। Video পোস্টের অনুমতি থাকতে হবে।

## Caption / UI / playback

নতুন archive forward-এ `drop_author` ও `drop_media_captions`; download/upload-এ empty
caption। Restricted downloader আগে থেকেই empty-caption ছিল। পুরোনো published
messages, IDs, links বা captions retroactively edit করা হয়নি।

Public stream ও thumbnail এখন যে session দিয়ে source message পাওয়া গেছে সেই
session-ই ব্যবহার করে। Autoplay browser আটকালেই বাস্তব tap-to-play button আসে;
video না চললে ৯ সেকেন্ড পরে নীরবে পরের slide নয়। বারবার codec error হলে একই
prepare request অনন্তবার ঘুরবে না। সব Telegram file-এর codec browser-compatible,
এমন দাবি নয়; transcoding এখানে যোগ হয়নি।

Setup/login/পুরোনো form surfaces-সহ black/yellow theme এবং mobile touch targets,
16px inputs, table overflow ও single-column layout উন্নত করা হয়েছে। বাস্তব ফোনের
browser ও live Telegram/network-এ deployment verification এখনও প্রয়োজন।

## Database URL — PostgreSQL support

এই সংস্করণে PostgreSQL adapter, পুরোনো SQLite bootstrap/import এবং PostgreSQL থেকে
পুরোনো `.bfixf.gz` backup/restore যোগ হয়েছে। Environment variable দিতে হবে না:
`database.private.json` অথবা `/setup`-এ private configuration upload করুন। বিস্তারিত:
[SUPABASE_SETUP.md](SUPABASE_SETUP.md)। Supabase-এর public tables ছোঁয়া হয় না;
অ্যাপের নিজস্ব `bfixf` schema ব্যবহার হয়। Live Supabase connection এখনও এখানে যাচাই হয়নি।

Reference checked: Telethon's forwarding implementation documents that removing
media captions requires dropping the author too:
https://github.com/LonamiWebs/Telethon/blob/v1/telethon/client/messages.py
