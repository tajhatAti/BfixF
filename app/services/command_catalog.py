"""Telegram-first command directory and per-admin private BotCommand scopes."""
from __future__ import annotations
from app import config as cfg, runtime, texts
from app.release import identity
from app.utils import esc

# Every executable slash command; subcommands are documented with their parent.
ADMIN_COMMANDS = [
    ('admin', 'অ্যাডমিন প্যানেল'), ('help', 'সব কমান্ডের পূর্ণ তালিকা'),
    ('commands', 'সব কমান্ড'), ('version', 'চলমান কোডের build'), ('status', 'বট/worker পরীক্ষা'),
    ('errors', 'মালিকের private error inbox'), ('whoami', 'আপনার ID ও অনুমতি'),
    ('downloads', 'ডাউনলোড/আপলোড লাইভ প্রগ্রেস'),
    ('restricted', 'পোস্ট downloader: on | off | retry'),
    ('session', 'নিজের Telegram সেশন কানেক্ট'), ('content', 'কনটেন্ট/ব্যাচ মোড'), ('done', 'ব্যাচ শেষ'),
    ('bots', 'বট: add | list | start | stop | restart | drop'),
    ('backup', 'পূর্ণ ডেটাবেজ ব্যাকআপ'), ('restore', 'পুরোনো ডেটা ফিরিয়ে আনুন'),
    ('emergency', 'জরুরি নিয়ন্ত্রণ'), ('maint', 'maintenance: on | off'),
    ('storageset', 'স্টোর ক্যাশ: auto | probe | চ্যানেল'),
    ('reel', 'হোম রিল: add | list | count | on | off | clear'), ('reelhelp', 'রিল ব্যবহারের নিয়ম'),
    ('bm', 'মিডিয়া ইনবক্স'), ('br', 'ব্রডকাস্ট স্টুডিও'),
    ('brhelp', 'ব্রডকাস্টের বিস্তারিত সাহায্য'), ('brsend', 'এখন ব্রডকাস্ট পাঠান'),
    ('brtest', 'অ্যাডমিনকে টেস্ট পাঠান'), ('brpreview', 'খসড়া দেখুন'),
    ('brstatus', 'ব্রডকাস্ট প্রগ্রেস'), ('brclear', 'খসড়া মুছুন'), ('brcancel', 'ব্রডকাস্ট থামান'),
    ('braudience', 'অডিয়েন্স: all | premium | free | store:ID'),
    ('brsilent', 'silent: on | off'), ('brprotect', 'কনটেন্ট সুরক্ষা: on | off'),
    ('brschedule', 'মিনিট পরে পাঠান'), ('brqueue', 'অপেক্ষমাণ ব্রডকাস্ট'),
    ('brwho', 'কারা পাবে'), ('brwhy', 'কারা কেন পায়নি'), ('brretry', 'ব্যর্থদের আবার পাঠান'),
    ('brcancelqueue', 'ক্যাম্পেইন ID দিয়ে বাতিল'), ('brslow', 'মিনিট ধরে ধীরে পাঠান'),
    ('britems', 'খসড়ার সব আইটেম'), ('brdrop', 'শেষ আইটেম বাদ'), ('brcsv', 'ডেলিভারি CSV'),
    ('brreport', 'ব্রডকাস্ট রিপোর্ট'), ('brshare', 'খসড়ার শেয়ার লিংক'),
]
PUBLIC_COMMANDS = [('start', 'শুরু করুন'), ('help', 'সাহায্য'), ('menu', 'মেনু'),
                   ('home', 'হোম'), ('stores', 'স্টোর'), ('cancel', 'চলতি প্রশ্ন বাতিল'),
                   ('whoami', 'নিজের ID ও অনুমতি দেখুন')]
ADMIN_NAMES = {name for name, _ in ADMIN_COMMANDS} - {'help', 'commands', 'whoami'}
ALL_NAMES = {name for name, _ in ADMIN_COMMANDS + PUBLIC_COMMANDS}


def help_pages(admin: bool) -> list[str]:
    if not admin:
        return [texts.HELP_TEXT]
    lines = ['🛠 <b>অ্যাডমিনের সব কমান্ড — শুধু অ্যাডমিন</b>',
             'Telegram বটই মূল নিয়ন্ত্রণ; ওয়েবসাইট পাশাপাশিই আছে।',
             f'Build: <code>{esc(identity())}</code>']
    lines += [f'/{name} — {esc(description)}' for name, description in ADMIN_COMMANDS + PUBLIC_COMMANDS
              if name not in ('help', 'whoami') or (name, description) in ADMIN_COMMANDS]
    lines.append('স্ক্যান, স্টোর, ইউজার, চ্যানেল, পেমেন্ট ও সেটিংসের বাকি কাজ /admin-এর বাটনে।')
    pages, current = [], ''
    for line in lines:
        if len(current) + len(line) > 2900:
            pages.append(current); current = ''
        current += line + '\n'
    if current:
        pages.append(current)
    return pages


async def announce_and_install(client):
    import asyncio
    from telethon import functions, types
    from app.logger import log
    from app.services import error_reports
    error_reports.install()
    if cfg.ADMIN_IDS:
        try:
            await asyncio.wait_for(client.send_message(cfg.ADMIN_IDS[0],
                f'✅ <b>Desi Site বট চালু</b> @{esc(runtime.bot_username)}\n'
                f'Build: <code>{esc(identity())}</code>\n'
                f'Handlers: {len(runtime.handler_registrations)}\n'
                'বট প্রস্তুত।', link_preview=False), timeout=10)
        except Exception:
            log.warning('Startup owner notification could not be delivered')
    try:
        # Delete any legacy global admin command list; users see public help only.
        await asyncio.wait_for(client(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(), lang_code='',
            commands=[types.BotCommand('start', 'শুরু করুন')])), timeout=10)
        for admin_id in list(cfg.ADMIN_IDS):
            try:
                peer = await asyncio.wait_for(client.get_input_entity(admin_id), timeout=10)
                await asyncio.wait_for(client(functions.bots.SetBotCommandsRequest(
                    scope=types.BotCommandScopePeer(peer), lang_code='',
                    commands=[types.BotCommand('start', 'শুরু করুন')])), timeout=10)
            except Exception:
                log.warning('Private command menu unavailable for admin %s; /help remains available', admin_id)
    except Exception:
        log.warning('Telegram command menu setup failed; /help remains available')
