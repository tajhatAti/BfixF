"""Secrets watchdog.

The owner edits `config.env` directly (that is normal), but the repository is
**public** — anything committed there is visible to the whole internet, forever.
This little guard notices it and says so out loud at boot:

    ⚠️ config.env is tracked by git and contains BOT_TOKEN, STRING_SESSION …

It is deliberately read-only: it never deletes or rewrites anything. The fix
(revoke the token, remove the file from the repo, use hosting env vars) is a
one-minute human decision, so we only inform — at startup and once a day to the
admins on Telegram.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from app import config as cfg
from app.logger import log

#: values that must never end up in a public repository
SECRET_KEYS = ("BOT_TOKEN", "API_HASH", "STRING_SESSION", "ADMIN_LOGIN_PASSWORD")
PRIVATE_KEYS = ("API_ID", "ADMIN_IDS", "PAY_BKASH", "PAY_NAGAD", "PAY_ROCKET")

# A secret does not have to be in config.env to leak: `website.example.py`, a
# notebook, an “old copy” of the bot — any *tracked* file counts. These patterns
# find the shapes Telegram uses, file by file (`git grep`, so it is instant even
# on a big repo).
#: POSIX ERE (git grep -E) — no `(?:…)`, no lookahead; the last group is the value
PATTERNS = (
    # a literal assignment (with or without quotes):
    #   API_HASH = "…"   BOT_TOKEN=123:AA…   STRING_SESSION: '…'
    r'(API_HASH|BOT_TOKEN|STRING_SESSION|ADMIN_LOGIN_PASSWORD)[ \t]*[:=][ \t]*["\']?([A-Za-z0-9_.:+/=-]{4,})["\']?',
    # a bot token anywhere: 1234567890:AAE…
    r"([0-9]{8,10}:[A-Za-z0-9_-]{30,})",
)
#: never a real secret — the docs and this file itself talk *about* the patterns
SKIP_FILES = ("config.env.example", ".env.example",
              "app/services/secrets_guard.py", "README.md", "DEPLOY.md", "ENV.md",
              "FEATURES.md", "PLAN.md")
#: words that mean “this is a placeholder, not a credential”
PLACEHOLDER_HINTS = ("your", "example", "placeholder", "change", "xxxx", "todo",
                     "<", "…", "here", "abcd1234", "dummy")


def _mask(text: str) -> str:
    """Never echo a secret in full — just enough to recognise it."""
    raw = text.strip()
    if len(raw) <= 12:
        return "…"
    return f"{raw[:6]}…{raw[-4:]} ({len(raw)} chars)"


def looks_real(value: str, key: str = "") -> bool:
    """Enough signal that this is a credential and not a template value."""
    raw = (value or "").strip()
    # A short numeric admin PIN is still a credential; keep its preview masked.
    if key == "ADMIN_LOGIN_PASSWORD" and re.fullmatch(r"[0-9]{4,12}", raw):
        return True
    if len(raw) < 20:
        return False
    low = raw.lower()
    if any(hint in low for hint in PLACEHOLDER_HINTS):
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32}", raw):          # API_HASH shape
        return True
    if re.fullmatch(r"\d{8,10}:[A-Za-z0-9_-]{30,}", raw):    # bot token shape
        return True
    # long single-token value (STRING_SESSION) — no spaces, some digits
    return bool(re.fullmatch(r"[A-Za-z0-9_\-+/=:.]{24,}", raw)) and any(ch.isdigit() for ch in raw)


def _skip(path: str) -> bool:
    return Path(path).name in SKIP_FILES or path.startswith(("docs/", "tests/"))


def scan_repo(repo_dir: Path | None = None, limit: int = 25) -> list[dict]:
    """Every *tracked* file that looks like it holds a real credential.

    Returns [{"file", "line", "preview"}] — preview is masked, so the report can
    be shown in the panel or in a Telegram message without leaking anything new.
    """
    repo = Path(repo_dir or Path(cfg.BASE_DIR))
    findings: list[dict] = []
    seen: dict[tuple[str, int], int] = {}          # (file, line) → index in findings
    # Two places count: what is on disk *and* what the last commit holds. A file
    # deleted a minute ago is still readable by everyone until it is committed away.
    for where in (".", "HEAD"):
        for pattern in PATTERNS:
            try:
                result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    ["git", "-C", str(repo), "grep", "-nIE", pattern, where],
                    capture_output=True, timeout=20, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return findings
            if result.returncode not in (0, 1):    # 1 = simply “no match”
                continue
            for line in result.stdout.decode("utf-8", errors="ignore").splitlines():
                if where != "." and line.startswith(f"{where}:"):
                    line = line[len(where) + 1:]        # `git grep HEAD` prefixes it
                path, _, rest = line.partition(":")
                number, _, content = rest.partition(":")
                if not number.isdigit() or _skip(path):
                    continue
                found = re.search(pattern, content)
                # the *last* group is always the credential itself
                value = (found.group(found.lastindex) if found and found.lastindex
                         else (found.group(0) if found else ""))
                key_name = found.group(1) if found and found.lastindex and found.lastindex > 1 else ""
                if not looks_real(value, key_name):
                    continue
                key = (path, int(number))
                if key in seen:
                    if where == "HEAD":
                        findings[seen[key]]["committed"] = True
                    continue
                if len(findings) >= limit:
                    continue
                seen[key] = len(findings)
                findings.append({"file": path, "line": int(number),
                                 "preview": _mask(value),
                                 "committed": where == "HEAD"})
    return findings


#: archive members we are willing to read (a zip inside a public repo hides a
#: leaked token just as well as a `.py` file — the owner uploaded one already)
ARCHIVE_TEXT_SUFFIXES = (".py", ".txt", ".json", ".env", ".md", ".cfg", ".ini", ".yaml",
                        ".yml", ".js", ".ts", ".sh", ".example")
ARCHIVE_MAX_MEMBER = 256 * 1024          # bigger members are not a config file
ARCHIVE_MAX_TOTAL = 4 * 1024 * 1024      # never read a whole distribution


def _looks_like_archive(data: bytes) -> bool:
    return data[:2] == b"PK" or data[:4] == b"PK\x03\x04"


def scan_archives(repo_dir: Path | None = None, limit: int = 10) -> list[dict]:
    """Scan *tracked* zip files — a leaked token hidden inside one still counts.

    Returns the same shape as :func:`scan_repo`, with the member appended to the
    file name as ``archive.zip!inner/file.py``, so the security page can show
    exactly where the value lives.
    """
    import subprocess as _sp
    import zipfile

    repo = Path(repo_dir or Path(cfg.BASE_DIR))
    findings: list[dict] = []
    try:
        listed = _sp.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), "ls-files", "-z"],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, _sp.SubprocessError):
        return findings
    for raw in listed.stdout.split(b"\x00"):
        name = raw.decode("utf-8", errors="ignore").strip()
        if not name or _skip(name):
            continue
        path = repo / name
        try:
            if not path.is_file() or path.stat().st_size > ARCHIVE_MAX_TOTAL:
                continue
            with open(path, "rb") as handle:
                if not _looks_like_archive(handle.read(4)):
                    continue
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    if member.is_dir() or member.file_size > ARCHIVE_MAX_MEMBER:
                        continue
                    if not (member.filename.endswith(ARCHIVE_TEXT_SUFFIXES)
                            or "." not in Path(member.filename).name):
                        continue
                    try:
                        text = archive.read(member).decode("utf-8", errors="ignore")
                    except (KeyError, OSError, RuntimeError):
                        continue
                    for number, line in enumerate(text.splitlines(), 1):
                        for pattern in PATTERNS:
                            found = re.search(pattern, line)
                            if not found:
                                continue
                            value = (found.group(found.lastindex) if found.lastindex
                                     else found.group(0))
                            key_name = found.group(1) if found.lastindex and found.lastindex > 1 else ""
                            if not looks_real(value, key_name):
                                continue
                            findings.append({"file": f"{name}!{member.filename}",
                                             "line": number, "preview": _mask(value),
                                             "committed": True})
                            break
                        if len(findings) >= limit:
                            return findings
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
    return findings


def _git_tracked(repo_dir: Path, name: str) -> bool | None:
    """True/False when we can ask git, None when git (or the repo) is absent."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo_dir), "ls-files", "--error-unmatch", name],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    # 128 = not a git repo / no such file; anything else = file simply untracked
    return None if b"fatal" in result.stderr.lower() else False


def _git_history_names(repo_dir: Path, name: str, limit: int = 200) -> bool:
    """True when `name` appears in commit history — even after `git rm`.

    A file removed today is still readable in yesterday's commit, so the tokens
    inside it must be revoked (that part is a human decision — we only tell).
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo_dir), "log", f"--max-count={limit}",
             "--name-only", "--pretty=format:"],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return name.encode() in result.stdout


def _env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def audit(repo_dir: Path | None = None, env_file: Path | None = None) -> dict:
    """Look for secrets that are committed / publicly visible.

    Returns {"tracked": bool, "secrets": [...], "private": [...], "path": str}
    """
    repo_dir = Path(repo_dir or Path(cfg.BASE_DIR))
    env_file = Path(env_file or Path(os.getenv("BOT_ENV_FILE", repo_dir / "config.env")))
    tracked = _git_tracked(repo_dir, env_file.name)
    in_history = _git_history_names(repo_dir, env_file.name) if tracked is not None else False
    values = _env_file_values(env_file)
    present = {key for key, value in values.items() if value and value.lower() not in
               ("your_token_here", "change_me")}
    env_name = env_file.name
    repo_files = [row for row in scan_repo(repo_dir) if Path(row["file"]).name != env_name] \
        if tracked is not None else []
    if tracked is not None:
        # zips hide text files just fine — `website-model-info` in this very repo
        # is a zip holding a hard-coded API_HASH.
        repo_files += [row for row in scan_archives(repo_dir)
                       if Path(row["file"].split("!")[0]).name != env_name]
    return {
        "tracked": bool(tracked),
        "git_available": tracked is not None,
        "path": str(env_file),
        "secrets": sorted(present & set(SECRET_KEYS)),
        "private": sorted(present & set(PRIVATE_KEYS)),
        "in_environment": sorted(key for key in SECRET_KEYS if os.getenv(key)),
        "in_history": in_history,
        "repo_files": repo_files,
    }


def warning_lines(report: dict) -> list[str]:
    """Human readable warnings (empty list = nothing to worry about).

    We only shout when the file is really committed: an untracked `config.env`
    with secrets is the intended setup, so it stays quiet.
    """
    if report.get("secrets") and report.get("in_history") and not report.get("tracked"):
        # The file was removed from the tree — good — but an older commit still
        # holds it, so the leaked token has to be revoked once and for all.
        return [
            f"🚨 {report['path']} আগের কোনো কমিটে গিট ইতিহাসে রয়ে গেছে "
            f"({', '.join(report['secrets'])})।",
            "   ✅ ফাইলটি এখন আর কমিট হয় না — কিন্তু পুরোনো কমিটে মানটা এখনো পড়া যায়।",
            "   ✅ তাই একবার অবশ্যই করুন: BotFather → /mybots → API Token → Revoke,",
            "      নতুন টোকেন config.env-এ বসান; Telegram → Devices → অচেনা সেশন Terminate।",
            "   বিস্তারিত: README.md → 💾 ডেটা, ব্যাকআপ ও নিরাপত্তা",
        ]
    files = report.get("repo_files") or []
    if files:
        listed = "\n".join(f"   • <code>{f['file']}</code>: line {f['line']} — "
                           f"{f['preview']}" for f in files[:5])
        more = f"\n   … আরও {len(files) - 5} টি" if len(files) > 5 else ""
        return [
            "🚨 রিপোর ভেতরে সরাসরি লেখা গোপন তথ্য পাওয়া গেছে "
            f"({len(files)} জায়গায়) — রিপো পাবলিক হলে সবাই দেখছে:",
            listed + more,
            "   ✅ যা করবেন: মানগুলো মুছে ফেলুন, শুধু <code>config.env</code> বা হোস্টিং "
            "প্যানেলের Environment Variables-এ রাখুন;",
            "   ✅ BotFather → /mybots → API Token → Revoke (একবার টোকেন ফাঁস হলে "
            "নতুন টোকেন নেওয়াই নিরাপদ);",
            "   বিস্তারিত: README.md → 💾 ডেটা, ব্যাকআপ ও নিরাপত্তা",
        ]
    if not report.get("secrets") or not report.get("tracked"):
        return []
    where = "আপনার GitHub রিপোতে (পাবলিক হলে সবাই দেখছে)"
    lines = [
        f"🚨 {report['path']} — এখানে থাকা গোপন তথ্য {where} আছে:",
        "   " + ", ".join(report["secrets"]),
        "   ✅ যা করবেন: BotFather → /mybots → API Token → Revoke, নতুন টোকেন দিন;",
        "   ✅ Telegram → Settings → Devices → অচেনা সেশন Terminate করুন;",
        "   ✅ `git rm --cached config.env` করে ফাইলটি রিপো থেকে বাদ দিন (ফাইলটা ডিস্কে থাকবে);",
        "   ✅ হোস্টিং প্যানেলের Environment Variables-এ টোকেনগুলো রাখুন।",
        "   বিস্তারিত: README.md → 💾 ডেটা, ব্যাকআপ ও নিরাপত্তা",
    ]
    return lines


def log_report() -> dict:
    """Check at startup and put the warning in the log."""
    report = audit()
    lines = warning_lines(report)
    for line in lines:
        log.warning(line)
    if not lines and report.get("private"):
        log.info("config.env holds private values (%s) — it is not committed, all good.",
                 ", ".join(report["private"]))
    return report


async def notify_admins_once_per_day() -> bool:
    """Send the same warning to the admins on Telegram (max once per day)."""
    from app.runtime import bot_online
    from app.services.telegram import safe_call
    from app.storage import db
    from app.utils import local_date_str

    if not bot_online():
        return False
    report = audit()
    lines = warning_lines(report)
    if not lines:
        return False
    today = local_date_str()
    if db.get_meta("secrets_warning_day") == today:
        return False
    db.set_meta("secrets_warning_day", today)

    from app.runtime import bot
    text = "<b>⚠️ নিরাপত্তা সতর্কতা</b>\n\n" + "\n".join(lines)
    for admin_id in cfg.ADMIN_IDS[:3]:
        try:
            await safe_call(bot.send_message, admin_id, text, what="secrets_warning",
                            retries=1, raise_after_retries=False)
        except Exception:
            pass
    return True
