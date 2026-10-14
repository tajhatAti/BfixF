"""Per-link options: own force-join channels, caption and inline buttons.

Every shareable link (multi-link, batch link, limited link) can carry its own

* **force-join channels** — only people who click *this* link must join them,
  and the bot has to be an admin there (otherwise it could not check anything),
* **caption** — shown under every video of the link (replaces the default one),
* **inline buttons** — `label | url` lines, same syntax as broadcasts.

The admin sets all of it by sending the link back to the bot (see
`app/handlers/linkpanel.py`); `app/services/flow.py` applies it on every click.
"""
from __future__ import annotations

from app import runtime
from app.logger import log
from app.services import forcejoin
from app.storage import db
from app.utils import parse_button_spec


# ------------------------------------------------------------------ read side
def targets(token: str) -> list[dict]:
    """The link's channels in the shape `forcejoin` already understands."""
    out = []
    for row in db.link_channels(token):
        out.append({"id": 0, "store_id": None, "ref": row.get("ref") or "",
                    "title": row.get("title") or row.get("ref") or "",
                    "chat_id": row.get("chat_id"), "invite": row.get("invite") or "",
                    "kind": forcejoin.parse(row.get("ref") or "")["kind"]})
    return out


def caption_of(link: dict | None) -> str | None:
    """The link's own caption, or None to keep the default one."""
    text = ((link or {}).get("caption") or "").strip()
    return text or None


def buttons_of(link: dict | None):
    """Inline URL buttons of the link, or None."""
    rows = parse_button_spec((link or {}).get("buttons") or "")
    return rows or None


async def missing_channels(user_id: int, token: str) -> list[dict]:
    """Channels of this link the user has not joined yet.

    A channel we cannot verify (Telegram hiccup, bot removed) never blocks the
    user — locking people out by accident is worse than skipping one gate.
    """
    groups = targets(token)
    if not groups:
        return []
    client = runtime.get_client()
    if client is None:
        return []
    missing = []
    for target in groups:
        chat_id = target.get("chat_id")
        if not chat_id:
            continue
        answer = await forcejoin._member_of(client, chat_id, user_id)
        if answer is False:
            missing.append(target)
    return missing


def gate_keyboard(token: str, missing: list[dict]):
    from telethon import Button
    from app.services import bot_texts
    rows = []
    for target in missing[:6]:
        url = forcejoin.invite_of(target)
        if url:
            rows.append([Button.url(f"📢 {target.get('title') or target.get('ref')}"[:60], url)])
    rows.append([Button.inline(bot_texts.render("join_button"), f"fl:{token}".encode())])
    return rows


# ----------------------------------------------------------------- admin side
async def verify_channel(ref: str) -> dict:
    """Resolve a channel and make sure the bot is an admin there.

    Returns {ok, chat_id, title, invite, ref, error}. The bot must be an admin:
    only then can it see who joined, which is the whole point of the gate.
    """
    from telethon import utils
    from telethon.tl.types import PeerChannel

    norm = forcejoin.normalize(ref)
    if not norm:
        return {"ok": False, "error": "চ্যানেলের লিংক/ইউজারনেম বোঝা যায়নি — "
                                      "@username, t.me লিংক বা প্রাইভেট ইনভাইট লিংক দিন।"}
    # Resolve on our own (forcejoin.resolve would overwrite the *global* channel cache).
    from app.services.telegram import safe_call
    attempts = [runtime.get_client()] + list(runtime.user_clients.values())
    entity, error = None, ""
    for client in attempts:
        if client is None:
            continue
        try:
            entity = await safe_call(client.get_entity, norm, what="link_channel",
                                     retries=1, raise_after_retries=False)
        except Exception as exc:
            error = str(exc)[:160]
            entity = None
        if entity is not None:
            break
    if entity is None:
        return {"ok": False, "error": error or "চ্যানেল পাওয়া যায়নি — লিংক ঠিক আছে কিনা দেখুন"}
    info = {"chat_id": entity.id, "title": getattr(entity, "title", "") or ""}
    marked = int(utils.get_peer_id(PeerChannel(int(info["chat_id"]))))
    bot_client = runtime.get_client()
    try:
        perms = await bot_client.get_permissions(marked, "me")
        is_admin = bool(getattr(perms, "is_admin", False) or getattr(perms, "is_creator", False))
    except Exception as exc:
        log.debug("bot admin check failed for %s: %s", norm, exc)
        is_admin = False
    if not is_admin:
        return {"ok": False, "chat_id": marked, "title": info.get("title") or norm,
                "error": "বট এই চ্যানেলের <b>অ্যাডমিন নয়</b>। আগে বটকে চ্যানেলে অ্যাডমিন "
                         "বানান, তারপর আবার চ্যানেল দিন।"}
    invite = forcejoin.join_url(norm) or ""
    if not invite:
        try:
            from telethon.tl.functions.messages import ExportChatInviteRequest
            exported = await bot_client(ExportChatInviteRequest(peer=marked))
            invite = getattr(exported, "link", "") or ""
        except Exception as exc:
            log.debug("could not export an invite link for %s: %s", marked, exc)
    return {"ok": True, "chat_id": marked, "title": info.get("title") or norm,
            "invite": invite, "ref": norm, "error": ""}
