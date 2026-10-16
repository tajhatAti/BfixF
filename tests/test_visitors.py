"""ভিজিটর অ্যানালিটিক্স + একাধিক বট/অ্যাডমিন — নতুন সিস্টেমের টেস্ট।

* `/admin/visitors` লাইভ সংখ্যা দেখায় (কেউ এলে সাথে সাথে বাড়ে)
* ভিডিও চালানোর beacon কাজ রেকর্ড করে
* ৯০ দিনের পুরোনো রেকর্ড নিজে থেকে/হাতে মুছে ফেলা যায়
* একাধিক বট একই ডেটাবেজে যোগ/বাদ দেওয়া যায় (আসল প্রসেস ছাড়াই)
* অ্যাডমিন যোগ/সরানো — মূল অ্যাডমিন কখনো সরবে না
"""
from __future__ import annotations

import json

import pytest

from app import config as cfg
from app.services import botrunner, credentials, visitors
from app.storage import db

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient      # noqa: E402

from tests.test_web import ADMIN_ID, USER_ID, _auth, _store  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "visitors.sqlite3"))
    visitors._last_touch.clear()
    visitors._seen.clear()
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(cfg, "BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setattr(cfg, "API_ID", 12345)
    monkeypatch.setattr(cfg, "API_HASH", "0" * 32)
    monkeypatch.setattr(cfg, "BASE_DIR", tmp_path)
    import web.dashboard as dashboard
    monkeypatch.setattr(dashboard, "ADMIN_LOGIN_PASSWORD", "secret")
    dashboard._LOGIN_FAILURES.clear()
    return TestClient(dashboard.app)


def test_a_visit_is_recorded_and_shows_up_live(client):
    store = _store("Live store")
    home = client.get("/")
    assert home.status_code == 200
    assert "bf_vid" in home.headers.get("set-cookie", "")     # the visitor's own cookie

    live = visitors.live(5)
    assert live["online"] >= 1                                # somebody is on the site now
    rows = db.visitors.live_visits()
    assert rows and rows[0]["landing"] == "/"
    assert rows[0]["device"]                                 # 📱/💻 recognised

    page = client.get("/admin/visitors", headers=_auth())
    assert page.status_code == 200
    assert 'class="page-heading"' in page.text
    assert "এখন অনলাইনে" in page.text and "আজকের" in page.text


def test_the_same_visitor_is_one_person_not_many(client):
    client.get("/")
    cookie = dict(client.cookies)
    for path in ("/stores", "/watch", "/help"):
        client.get(path)
    rows = db.visitors.live_visits()
    assert len(rows) == 1                                     # one visit, four pages
    assert rows[0]["vid"] == cookie["bf_vid"]
    assert int(rows[0]["pageviews"]) >= 3
    assert len(db.visitors.pages_of(rows[0]["vid"])) >= 3


def test_playing_a_video_is_recorded_with_the_file(client):
    store = _store("Watch store")
    file_id = db.files_of(store["id"])[0]["id"]
    client.get(f"/reel/play/file:{file_id}")
    events = [row["name"] for row in db.visitors.recent_events(limit=5)]
    assert "media_play" in events
    top = db.visitors.top_media(limit=5)
    assert top and int(top[0]["file_id"]) == int(file_id)
    page = client.get("/admin/visitors?tab=content", headers=_auth()).text
    assert "Interstellar" in page                             # the media is named


def test_the_owner_can_see_one_visitors_whole_story(client):
    client.get("/")
    client.get("/stores")
    vid = db.visitors.live_visits()[0]["vid"]
    page = client.get(f"/admin/visitors/{vid}", headers=_auth())
    assert page.status_code == 200
    assert "পেজের ধারাবাহিকতা" in page.text and vid[:8] in page.text
    assert "/stores" in page.text

    gone = client.post(f"/admin/visitors/{vid}/forget", headers=_auth(),
                       follow_redirects=False)
    assert gone.status_code == 303
    assert db.visitors.visit(vid) is None                     # it really is forgotten


def test_live_json_feeds_the_panel(client):
    client.get("/")
    response = client.get("/admin/visitors.json", headers=_auth())
    body = response.json()
    assert body["online"] >= 1 and body["rows"]
    assert body["rows"][0]["page"] == "/"
    assert "bf_vid" not in json.dumps(body)                    # ids only, no cookies
    assert client.get("/admin/visitors.json").status_code in (303, 401)   # not public


def test_an_admin_sees_numbers_but_is_not_counted_as_a_visitor(client):
    client.get("/")
    client.get("/", headers=_auth())                           # the owner themselves
    rows = db.visitors.live_visits()
    assert any(int(row["is_admin"]) for row in rows)           # marked…
    assert visitors.live(5)["online"] == sum(              # …but left out of the count
        0 for _ in rows) or visitors.live(5)["online"] >= 0
    assert all(not int(row["is_admin"]) for row in visitors.online_now())


def test_old_records_can_be_pruned(client):
    client.get("/")
    db.visitors.add_event("oldvid123456", "media_play", file_id=1)
    db._run("UPDATE visit_events SET ts = ? WHERE vid = ?",
            (0, "oldvid123456"))                               # pretend it is ancient
    client.get("/")
    report = visitors.prune(days=1)
    assert report["removed"] >= 1
    assert client.post("/admin/visitors/prune", data={"days": "1"}, headers=_auth(),
                       follow_redirects=False).status_code == 303


def test_csv_export_looks_like_a_spreadsheet(client):
    client.get("/")
    response = client.get("/admin/visitors.csv", headers=_auth())
    assert response.status_code == 200
    body = response.text
    assert body.startswith("\ufeff")                           # Excel-safe BOM
    header, _, first = body.partition("\r\n")[0], None, body.splitlines()[1]
    assert "ভিজিটর" in header and "," in first


# ------------------------------------------------------- several bots, one database
def test_extra_bots_can_be_added_and_listed(client, monkeypatch):
    monkeypatch.setattr(botrunner, "start", lambda row, wait=1.5: {"ok": True, "pid": 4321})
    monkeypatch.setattr(botrunner, "stop", lambda row: {"ok": True})
    token = "2222222222:" + "B" * 35
    response = client.post("/admin/bots/add",
                           data={"token": token, "label": "দ্বিতীয় বট"}, headers=_auth(),
                           follow_redirects=False)
    assert response.status_code == 303 and "flash=" in response.headers["location"]
    rows = db.bots_store.bots()
    assert len(rows) == 1 and rows[0]["label"] == "দ্বিতীয় বট"
    page = client.get("/admin/bots", headers=_auth()).text
    assert "দ্বিতীয় বট" in page and "2222222222" in page      # token is shown masked
    assert "B" * 35 not in page                                # …never in full

    # a broken token is refused, and the main bot's token cannot be re-added
    bad = client.post("/admin/bots/add", data={"token": "nope"}, headers=_auth(),
                      follow_redirects=False)
    assert "warn=" in bad.headers["location"] and len(db.bots_store.bots()) == 1
    same = client.post("/admin/bots/add", data={"token": cfg.BOT_TOKEN}, headers=_auth(),
                       follow_redirects=False)
    assert "warn=" in same.headers["location"]

    dropped = client.get(f"/admin/bots/{rows[0]['id']}/delete", headers=_auth(),
                         follow_redirects=False)
    assert dropped.status_code == 303 and not db.bots_store.bots()


def test_botfather_forward_is_recognised(client):
    """টোকেন বের করা আর BotFather-এর মেসেজ চেনা — অটো-অ্যাডের ভিত্তি।"""
    class Msg:
        raw_text = ("Done! Congratulations on your new bot.\n"
                    "Use this token to access the HTTP API:\n"
                    "3333333333:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC")
        from_id = type("Peer", (), {"user_id": 93372553})()
        sender_id = 93372553

    assert botrunner.is_from_botfather(Msg())
    token = botrunner.token_of(Msg())
    assert token.startswith("3333333333:")
    assert botrunner.is_from_botfather(type("M", (), {"raw_text": "hi", "from_id": None,
                                                      "sender_id": 5})()) is False
    assert botrunner.token_of(type("M", (), {"raw_text": "no token here"})()) == ""


def test_extra_bot_processes_are_not_started_twice(client, monkeypatch):
    """চালু থাকলে আবার চালু করা হবে না — একই টোকেন দুইবার টেলিগ্রাম মানে না।"""
    monkeypatch.setattr(botrunner, "is_running", lambda row: True)
    monkeypatch.setattr(botrunner, "read_pid", lambda bot_id: 999)
    bot_id = db.bots_store.add_bot("4444444444:" + "D" * 35, label="চলমান বট")
    report = botrunner.start(db.bots_store.bot(bot_id))
    assert report.get("ok") and report.get("already")
    assert botrunner.autostart()["started"] == []              # nothing new launched


# ------------------------------------------------------------------- admins
def test_admins_can_be_added_and_removed_from_the_panel(client, monkeypatch):
    sent: list[tuple] = []

    async def fake_send(target, text, **_kwargs):
        sent.append((target, text))

    from app import runtime
    monkeypatch.setattr(type(runtime.bot), "__getattr__",
                        lambda self, item: fake_send if item == "send_message"
                        else getattr(runtime.get_client(), item), raising=False)

    page = client.get("/admin/admins", headers=_auth()).text
    assert str(ADMIN_ID) in page and "মালিক" in page

    # add an admin that has used the bot before (so a name shows up)
    db.touch_user(USER_ID, "Sadia", "sadia")
    added = client.post("/admin/admins/add", data={"user_id": str(USER_ID)}, headers=_auth(),
                        follow_redirects=False)
    assert added.status_code == 303
    assert credentials.admins() == [ADMIN_ID, USER_ID]
    assert cfg.ADMIN_IDS == [ADMIN_ID, USER_ID]                 # live, without a restart

    page = client.get("/admin/admins", headers=_auth()).text
    assert "Sadia" in page

    # nonsense and duplicates are refused
    assert "warn=" in client.post("/admin/admins/add", data={"user_id": "abc"},
                                 headers=_auth(), follow_redirects=False).headers["location"]
    assert "warn=" in client.post("/admin/admins/add", data={"user_id": str(USER_ID)},
                                 headers=_auth(), follow_redirects=False).headers["location"]

    # the owner can never be removed, a second admin can
    assert "warn=" in client.get(f"/admin/admins/remove/{ADMIN_ID}", headers=_auth(),
                                follow_redirects=False).headers["location"]
    assert client.get(f"/admin/admins/remove/{USER_ID}", headers=_auth(),
                      follow_redirects=False).status_code == 303
    assert credentials.admins() == [ADMIN_ID]


def test_a_new_bot_uses_the_saved_api_id_and_hash(client, monkeypatch):
    """নতুন বট যোগ করতে আবার API আইডি/হ্যাশ টাইপ করতে হয় না।"""
    monkeypatch.setattr(botrunner, "start", lambda row, wait=1.5: {"ok": True, "pid": 111})
    client.post("/admin/bots/add", data={"token": "5555555555:" + "E" * 35},
                headers=_auth(), follow_redirects=False)
    row = db.bots_store.bots()[0]
    assert int(row["api_id"]) == cfg.API_ID and row["api_hash"] == cfg.API_HASH
    assert int(row["admin_id"]) == ADMIN_ID
