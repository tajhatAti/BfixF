import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
import time
from collections import deque

BASE = os.path.dirname(os.path.abspath(__file__))

# ---- cryptg = Telethon er tgcrypto. Eita na thakle download/upload ~400 KB/s e atke thake ----
# Telethon import howar AGE eita thakte hobe, tai ekhane nije install kore nei.
CRYPTG_ERR = ""


def _has_cryptg():
    try:
        import cryptg  # noqa: F401
        return True
    except ImportError:
        return False


CRYPTG = _has_cryptg()
if not CRYPTG:
    try:
        _libdir = os.path.join(BASE, "pylibs_extra")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
             "--target", _libdir, "cryptg"],
            timeout=300, check=True, capture_output=True,
        )
        if _libdir not in sys.path:
            sys.path.insert(0, _libdir)
        importlib.invalidate_caches()
        CRYPTG = _has_cryptg()
        if not CRYPTG:
            CRYPTG_ERR = "install hoyeche kintu import hoyni"
    except Exception as _e:
        CRYPTG_ERR = str(_e)[-300:]

from aiohttp import web
from telethon import TelegramClient
from telethon.errors import FloodWaitError

# FloodWait + "non-premium accounts e wait lagbe" (FloodPremiumWait) dui tai dhorbo
FLOOD = (FloodWaitError,)
try:
    from telethon.errors import FloodError as _FloodBase
    FLOOD = FLOOD + (_FloodBase,)
except ImportError:
    pass
try:
    from telethon.errors import FloodPremiumWaitError as _FloodPrem
    FLOOD = FLOOD + (_FloodPrem,)
except ImportError:
    pass
from telethon.sessions import StringSession
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import (
    InputMessagesFilterPhotoVideo,
    InputMessagesFilterPhotos,
    InputMessagesFilterVideo,
)


# ---------- config (sob ekhane, env lagbe na) ----------
API_ID = 37109385
API_HASH = ''
SESSION = ""  # chaile ekhane session string boshao, na hole site e paste koro

DEFAULT_CFG = {
    "source": "GetUrFileConvBot",
    "target": "AhadConrner",
    "start_no": 1,      # koto number media theke suru (1 = shobcheye purano)
    "limit": 0,         # koyta pathabe, 0 = sob
    "delay": 1.5,       # prottek file er por delay (sec)
    "photos": True,
    "videos": True,
    "caption": True,
    "auto": False,      # restart hole nije Resume
    "conn": 1,          # ek file e koyta connection diye download (1-4). 1 = shobcheye nirapod
    "parallel": 2,      # ekshathe koyta file (1-8). Backup session thakle shegulo bhag kore nibe
    "restricted": False,  # forward-block (restricted) source: download kore abar upload
    "max_mb": 0,        # ekhane MB er beshi hole file skip (0 = limit nai)
    "min_mb": 0,        # ekhane MB er kom hole file skip (0 = limit nai)
    "sender": "all",    # all = sob | me = shudhu amar pathano | others = shudhu onnoder
    "clean_caption": False,  # caption theke @mention ar t.me link muche dey
    "caption_extra": "",     # caption er shesh e extra lekha
}

PORT = int(os.environ.get("PORT", 10000))  # host nije dey

SESSION_FILE = os.path.join(BASE, "session.txt")
CONFIG_FILE = os.path.join(BASE, "config.json")
PROGRESS_FILE = os.path.join(BASE, "progress.json")
EXTRA_FILE = os.path.join(BASE, "backup_sessions.json")
FAILED_FILE = os.path.join(BASE, "failed.json")

HTML = r"""<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ahad Forwarder</title>
<style>
  :root{--bg:#10131a;--panel:#181d27;--line:#262d3b;--text:#e8ebf2;--muted:#8a94a8;--ok:#4ade80;--warn:#fbbf24;--err:#f87171;--info:#7dd3fc}
  *{box-sizing:border-box}
  html,body{margin:0}
  body{background:var(--bg);color:var(--text);font-family:"Segoe UI",system-ui,"Noto Sans Bengali",sans-serif;padding:16px;max-width:760px;margin:0 auto;line-height:1.45}
  h1{font-size:20px;margin:4px 0 2px}
  h2{font-size:15px;margin:22px 0 8px}
  a{color:var(--info);word-break:break-all}
  .muted{color:var(--muted);font-size:13px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
  .badge{display:inline-block;padding:3px 12px;border-radius:99px;font-size:13px;font-weight:600}
  .b-need_session,.b-done,.b-idle{background:#1d3550;color:var(--info)}
  .b-connecting,.b-paused{background:#3a3515;color:var(--warn)}
  .b-running{background:#0d3d2a;color:var(--ok)}
  .b-flood{background:#4a2c0a;color:var(--warn)}
  .b-error{background:#4a1515;color:var(--err)}
  textarea,input[type=text],input[type=number],select{width:100%;background:#0c0f15;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px;font-size:14px}
  textarea{min-height:120px;font-family:ui-monospace,Consolas,monospace;font-size:12px;resize:vertical}
  textarea:focus,input:focus,select:focus,button:focus-visible{outline:2px solid var(--info);outline-offset:2px}
  label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px}
  .two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .checks{display:flex;flex-wrap:wrap;gap:14px;margin-top:12px;font-size:13px}
  .checks label{display:flex;align-items:center;gap:6px;margin:0;color:var(--text);font-size:13px}
  button{background:var(--info);color:#06202e;border:0;border-radius:8px;padding:10px 16px;font-size:14px;font-weight:700;cursor:pointer}
  button:disabled{opacity:.5;cursor:wait}
  button.sec{background:#2a3244;color:var(--text)}
  button.red{background:#5a2222;color:#ffd0d0}
  button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line);font-weight:500;padding:5px 12px;font-size:12px}
  .btns{display:flex;flex-wrap:wrap;gap:8px}
  .msg{font-size:13px;margin-top:8px;min-height:18px}
  .msg.e{color:var(--err)} .msg.g{color:var(--ok)}
  .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:12px}
  @media(min-width:560px){.grid{grid-template-columns:repeat(3,1fr)}}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}
  .stat b{display:block;font-size:22px}
  .stat span{font-size:12px;color:var(--muted)}
  .bar{height:8px;background:#232a38;border-radius:99px;overflow:hidden;margin:8px 0}
  .bar div{height:100%;width:0;background:var(--ok);transition:width .6s}
  .row{display:flex;justify-content:space-between;gap:10px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-bottom:5px;font-size:13px}
  .row .n{word-break:break-all}
  .row .m{color:var(--muted);font-size:11px;text-align:right;white-space:nowrap}
  .row.bad{border-color:#5a2222;color:var(--err)}
  .row.skip{border-color:#4a4220}
  .hide{display:none !important}
  .top{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
  .route{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:4px}
  .av{width:26px;height:26px;border-radius:50%;object-fit:cover;background:#2a3244}
  .cur{display:flex;gap:12px;align-items:flex-start}
  .cur img{width:92px;height:92px;object-fit:cover;border-radius:10px;background:#0c0f15;flex:none}
  .cur .info{min-width:0;flex:1}
  .cur .nm{font-weight:600;word-break:break-all}
  .pill{display:inline-block;background:#232a38;border-radius:99px;padding:2px 10px;font-size:12px;margin:4px 6px 0 0}
</style>
</head>
<body>

<div class="top">
  <div>
    <h1>Forwarder</h1>
    <div class="route muted">
      <img id="avS" class="av hide" alt=""><span id="srcName">-</span>
      <span>→</span>
      <img id="avT" class="av hide" alt=""><span id="tgtName">-</span>
    </div>
    <div class="muted" id="cg" style="margin-top:4px"></div>
  </div>
  <div><span id="badge" class="badge b-need_session">...</span></div>
</div>

<div id="loginView" class="hide">
  <h2>Session String paste koro</h2>
  <div class="panel">
    <textarea id="sess" placeholder="1BVts..." spellcheck="false"></textarea>
    <div style="margin-top:10px"><button id="btn" onclick="connect()">Connect</button></div>
    <div class="msg e" id="err"></div>
  </div>
</div>

<div id="dashView" class="hide">
  <div class="top" style="margin-top:10px">
    <div class="muted">Login: <span id="me">-</span> <span id="floodtxt" style="color:var(--warn)"></span></div>
    <button class="ghost" onclick="logout()">Session bodlao</button>
  </div>

  <div id="actCard" class="hide">
    <h2>Ekhon cholche</h2>
    <div id="actList"></div>
  </div>

  <h2>Ekta file (single link)</h2>
  <div class="panel">
    <label>Telegram message er link, shudhu oi ekta file jabe (size limit dhorbe na)</label>
    <input type="text" id="s_link" placeholder="https://t.me/c/123456/78  ba  https://t.me/username/78">
    <div class="btns" style="margin-top:8px"><button id="s_btn" onclick="sendSingle()">Ei file ta pathao</button></div>
    <div class="msg" id="s_msg"></div>
  </div>

  <h2>Control</h2>
  <div class="panel">
    <div class="btns" id="ctrl"></div>
    <div class="msg" id="ctrlmsg"></div>
  </div>

  <h2>Settings</h2>
  <div class="panel">
    <label>Source (jekhan theke nibe)</label>
    <input type="text" id="f_source" placeholder="@username / t.me link / -100123...">
    <label>Destination (jekhane pathabe)</label>
    <input type="text" id="f_target" placeholder="@channel / t.me link / -100123...">
    <div class="two">
      <div><label>Koto number media theke suru</label><input type="number" id="f_start" min="1" value="1"></div>
      <div><label>Koyta pathabe (0 = sob)</label><input type="number" id="f_limit" min="0" value="0"></div>
    </div>
    <div class="two">
      <div><label>Max file size (MB, 0 = limit nai)</label><input type="number" id="f_max" min="0" step="any" value="0"></div>
      <div><label>Min file size (MB, 0 = limit nai)</label><input type="number" id="f_min" min="0" step="any" value="0"></div>
    </div>
    <div class="two">
      <div><label>Ekshathe koyta file (1-8)</label><input type="number" id="f_parallel" min="1" max="8" value="2"></div>
      <div><label>Ek file e koyta connection (1-4)</label><input type="number" id="f_conn" min="1" max="4" value="1"></div>
      <div><label>Prottek file er por delay (sec)</label><input type="number" id="f_delay" min="0" step="0.1" value="1.5"></div>
    </div>
    <div class="two">
      <div><label>Kar pathano file nibe</label>
        <select id="f_sender"><option value="all">Sob</option><option value="me">Shudhu amar pathano</option><option value="others">Shudhu onnoder (bot/onno)</option></select>
      </div>
      <div></div>
    </div>
    <label>Caption er shesh e extra lekha (khali rakhle kichu na)</label>
    <input type="text" id="f_extra" placeholder="jemon: @AhadConrner">
    <div class="checks">
      <label><input type="checkbox" id="f_photos"> Chhobi</label>
      <label><input type="checkbox" id="f_videos"> Video</label>
      <label><input type="checkbox" id="f_caption"> Caption rakho</label>
      <label><input type="checkbox" id="f_clean"> Caption theke @mention/link muche dao</label>
      <label><input type="checkbox" id="f_restricted"> Restricted source (download + upload)</label>
      <label><input type="checkbox" id="f_auto"> Restart hole nije Resume</label>
    </div>
    <div class="muted" style="margin-top:10px">
      Media number gona hoy purano theke (1 = shobcheye purano), shudhu tick deya type gulo diye.
      Cholar somoy Source, type, start number ba limit badle dile notun settings e abar suru hobe.
      Destination, size limit, delay, caption, sender badlale cholte cholte-i lagu hobe.<br><br>
      Ekshathe koyta file = ek sathe koyta file download/upload cholbe. Telegram non-premium account e beshi speed e download korle "3 second wait" dey, tai sob session mile 3-6 ta rakho (prottek session e 2-3 ta file). Backup session thakle file gulo session gulor moddhe bhag hoy.<br><br>Ek file e koyta connection: 1 = shobcheye nirapod (boro 1 MB chunk e download, Pyrogram er moto). 2-4 dile ek ek file er download aro fast hote pare, kintu eita notun, kono problem hole 1 kore dao.<br><br>Size limit file download er <b>age</b> check hoy. Limit er baire file gulo download/upload hoy na, "Skip kora file" list e dekhabe.<br><br>
      Restricted source tick dile forward-block thaklew file download kore abar upload hobe (slow, server er data/disk lagbe). Private group/channel hole tomar account ke age join kora thakte hobe. Source e t.me/c/... link ba invite link (t.me/+...) dileo cholbe.
    </div>
    <div style="margin-top:12px"><button id="savebtn" onclick="saveSettings()">Save settings</button></div>
    <div class="msg" id="setmsg"></div>
  </div>

  <h2>Backup session (FloodWait hole eta niye same file cholbe)</h2>
  <div class="panel">
    <div id="poolList"></div>
    <label>Notun backup session string</label>
    <textarea id="bk_sess" style="min-height:70px" placeholder="1BVts..." spellcheck="false"></textarea>
    <div style="margin-top:10px"><button id="bk_btn" class="sec" onclick="addBackup()">Backup add koro</button></div>
    <div class="msg" id="bk_msg"></div>
    <div class="muted">Backup account ke source e join thakte hobe, ar destination e pathate parte hobe.</div>
  </div>

  <div class="panel" style="margin-top:12px">
    <div class="top"><span id="ptxt">0 / 0</span><span id="ppct">0%</span></div>
    <div class="bar"><div id="pbar"></div></div>
    <div class="muted">Ekhon: <span id="current">-</span></div>
  </div>

  <div class="grid">
    <div class="stat"><b id="sent">0</b><span>Mot pathano</span></div>
    <div class="stat"><b id="remain">0</b><span>Baki</span></div>
    <div class="stat"><b id="eta">-</b><span>Sesh hote lagbe (ETA)</span></div>
    <div class="stat"><b id="skipped_n">0</b><span>Skip kora</span></div>
    <div class="stat"><b id="failed">0</b><span>Failed</span></div>
    <div class="stat"><b id="pos">0</b><span>Shesh media #</span></div>
    <div class="stat"><b id="media_total">0</b><span>Source e mot media</span></div>
    <div class="stat"><b id="speed">-</b><span>File / minute</span></div>
    <div class="stat"><b id="mbps">-</b><span>Gor speed</span></div>
    <div class="stat"><b id="bytes">0 B</b><span>Mot data</span></div>
    <div class="stat"><b id="videos">0</b><span>Video</span></div>
    <div class="stat"><b id="photos">0</b><span>Chhobi</span></div>
    <div class="stat"><b id="from_me">0</b><span>Amar pathano</span></div>
    <div class="stat"><b id="from_bot">0</b><span>Onnoder pathano</span></div>
    <div class="stat"><b id="flood_count">0</b><span>FloodWait</span></div>
    <div class="stat"><b id="switches">0</b><span>Session bodle abar</span></div>
    <div class="stat"><b id="uptime">0s</b><span>Ei run cholche</span></div>
  </div>

  <h2>Failed file (Control theke "Failed gulo abar" chaple abar cheshta hobe)</h2>
  <div id="failedList"><div class="muted">Kono failed file nai</div></div>

  <h2>Skip kora file (size limit er karone)</h2>
  <div id="skipped"><div class="muted">Kichu skip hoyni</div></div>

  <h2>Pathano file (sorboshesh 200)</h2>
  <div id="recent"><div class="muted">Ekhono kichu jayni</div></div>

  <h2>Error / Log</h2>
  <div id="errors"><div class="muted">Kono error nai</div></div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtSize = b => { if(!b) return "0 B"; const u=["B","KB","MB","GB","TB"]; let i=0; while(b>=1024&&i<4){b/=1024;i++} return b.toFixed(i?1:0)+" "+u[i]; };
const fmtDur = s => { s=Math.floor(s); const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60), x=s%60; return (d?d+"d ":"")+(h?h+"h ":"")+(m?m+"m ":"")+(d?"":x+"s"); };
const fmtTime = t => new Date(t*1000).toLocaleTimeString();
const linkHtml = l => l ? ` · <a href="${esc(l)}" target="_blank" rel="noopener">link</a>` : "";
let busy = false, formInit = false, avV = -1, lastStart = 0;

async function post(url, body){
  const r = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
  return r.json();
}

async function connect(){
  const session = $("sess").value.trim();
  if(!session){ $("err").textContent = "Session string paste koro"; return; }
  busy = true; $("btn").disabled = true; $("btn").textContent = "Connecting..."; $("err").textContent = "";
  try{
    const r = await post("api/connect",{session});
    if(!r.ok) $("err").textContent = r.error || "Connect hoyni"; else $("sess").value = "";
  }catch(e){ $("err").textContent = "Server e connect kora jachchhe na"; }
  busy = false; $("btn").disabled = false; $("btn").textContent = "Connect";
  load();
}

async function logout(){
  if(!confirm("Session muche notun session dibe?")) return;
  await post("api/logout"); formInit = false; load();
}

function readForm(){
  return {
    source: $("f_source").value.trim(), target: $("f_target").value.trim(),
    start_no: parseInt($("f_start").value)||1, limit: parseInt($("f_limit").value)||0,
    parallel: parseInt($("f_parallel").value)||1, conn: parseInt($("f_conn").value)||1,
    delay: parseFloat($("f_delay").value)||0,
    max_mb: parseFloat($("f_max").value)||0, min_mb: parseFloat($("f_min").value)||0,
    sender: $("f_sender").value, caption_extra: $("f_extra").value,
    photos: $("f_photos").checked, videos: $("f_videos").checked,
    caption: $("f_caption").checked, clean_caption: $("f_clean").checked,
    restricted: $("f_restricted").checked, auto: $("f_auto").checked
  };
}
function fillForm(c){
  $("f_source").value = c.source; $("f_target").value = c.target;
  $("f_start").value = c.start_no; $("f_limit").value = c.limit; $("f_delay").value = c.delay; $("f_parallel").value = c.parallel; $("f_conn").value = c.conn;
  $("f_max").value = c.max_mb; $("f_min").value = c.min_mb;
  $("f_sender").value = c.sender; $("f_extra").value = c.caption_extra;
  $("f_photos").checked = c.photos; $("f_videos").checked = c.videos;
  $("f_caption").checked = c.caption; $("f_clean").checked = c.clean_caption;
  $("f_restricted").checked = c.restricted; $("f_auto").checked = c.auto;
}

async function saveSettings(){
  const m = $("setmsg"); m.className = "msg"; m.textContent = "Check kora hocche...";
  $("savebtn").disabled = true;
  try{
    const r = await post("api/settings", readForm());
    if(r.ok){ m.className = "msg g"; m.textContent = r.restarted ? "Save hoyeche, notun settings e abar suru hoyeche" : "Save hoyeche"; }
    else { m.className = "msg e"; m.textContent = r.error; }
  }catch(e){ m.className = "msg e"; m.textContent = "Server error"; }
  $("savebtn").disabled = false; load();
}

async function sendSingle(){
  const link = $("s_link").value.trim(), m = $("s_msg");
  if(!link){ m.className = "msg e"; m.textContent = "Link dao"; return; }
  $("s_btn").disabled = true;
  try{
    const r = await post("api/single", {link});
    if(r.ok){ m.className = "msg"; m.textContent = "Shuru hoyeche..."; $("s_link").value = ""; }
    else { m.className = "msg e"; m.textContent = r.error; }
  }catch(e){ m.className = "msg e"; m.textContent = "Server error"; }
  $("s_btn").disabled = false; load();
}
function renderSingle(d){
  const sg = d.single, m = $("s_msg");
  if(!sg || sg.state === "idle") return;
  if(sg.state === "running"){ m.className = "msg"; m.textContent = "Cholche: " + sg.msg; }
  else if(sg.state === "done"){ m.className = "msg g"; m.textContent = "✅ Hoyeche: " + sg.msg; }
  else { m.className = "msg e"; m.textContent = "❌ " + (sg.reason || "Hoyni"); }
}
async function act(a, n){
  if(a==="reset" && !confirm("Ei source er progress muche felbe?")) return;
  if(a==="stop" && !confirm("Thamabe? (pore Resume kora jabe)")) return;
  const r = await post("api/control", n===undefined ? {action:a} : {action:a, n:n});
  $("ctrlmsg").className = "msg " + (r.ok?"g":"e"); $("ctrlmsg").textContent = r.ok ? "" : r.error;
  load();
}

function renderCtrl(d){
  const s = d.status, b = [];
  if(s==="running"||s==="flood"){
    b.push(`<button class="sec" onclick="act('pause')">Pause</button>`);
    b.push(`<button class="red" onclick="act('stop')">Stop</button>`);
  } else if(s==="paused"){
    b.push(`<button onclick="act('continue')">Continue</button>`);
    b.push(`<button class="red" onclick="act('stop')">Stop</button>`);
  } else {
    b.push(`<button onclick="act('start')">Start (#${d.cfg.start_no} theke)</button>`);
    if(d.resume_pos>0) b.push(`<button class="sec" onclick="act('resume')">Resume (#${d.resume_pos+1} theke)</button>`);
    if(d.resume_pos>0) b.push(`<button class="ghost" onclick="act('reset')">Progress reset</button>`);
    if(d.failed_saved>0) b.push(`<button class="sec" onclick="act('retry')">Failed gulo abar (${d.failed_saved})</button>`);
  }
  const html = b.join("");
  if($("ctrl").dataset.h !== html){ $("ctrl").innerHTML = html; $("ctrl").dataset.h = html; }
}

const STAGE = {wait:"Prostuti", download:"Download", upload:"Upload", copy:"Copy hocche"};

function mkCard(n){
  const el = document.createElement("div");
  el.className = "panel cur"; el.id = "act_" + n; el.style.marginBottom = "8px";
  el.innerHTML = `<img alt="" style="visibility:hidden" onerror="this.style.visibility='hidden'">
    <div class="info"><div class="nm"></div>
    <div><span class="pill sz"></span><span class="pill kd"></span><span class="pill by"></span></div>
    <div class="muted st" style="margin-top:6px"></div>
    <div class="bar"><div class="fill"></div></div>
    <a class="lk hide" target="_blank" rel="noopener" style="font-size:12px"></a>
    <div style="margin-top:8px"><button class="red" style="padding:6px 14px;font-size:13px">Ei file skip koro</button></div></div>`;
  el.querySelector("button").onclick = () => act("skip", n);
  return el;
}

function renderActive(d){
  const keys = Object.keys(d.active).map(Number).sort((a,b)=>a-b);
  $("actCard").classList.toggle("hide", !keys.length);
  const list = $("actList");
  if(lastStart !== d.started){ list.innerHTML = ""; lastStart = d.started; }
  for(const el of Array.from(list.children)){
    if(!d.active[el.id.slice(4)]) el.remove();
  }
  for(const n of keys){
    const c = d.active[n];
    let el = $("act_" + n);
    if(!el){ el = mkCard(n); list.appendChild(el); }
    if(c.has_thumb && !el.dataset.img){
      el.dataset.img = "1";
      const im = el.querySelector("img");
      im.src = "api/thumb?n=" + n + "&r=" + d.started;
      im.style.visibility = "visible";
    }
    el.querySelector(".nm").textContent = "#" + c.no + " · " + c.name;
    el.querySelector(".sz").textContent = c.size ? (c.size/1048576).toFixed(1) + " MB" : "size jana nai";
    el.querySelector(".kd").textContent = c.kind === "video" ? "Video" : "Chhobi";
    el.querySelector(".by").textContent = c.by === "me" ? "Amar pathano" : "Onnoder pathano";
    const dl = (c.stage==="download"||c.stage==="upload");
    el.querySelector(".st").textContent = (STAGE[c.stage]||c.stage) + (dl ? " " + c.pct + "%" + (c.speed ? " · " + fmtSize(c.speed) + "/s" : "") : "");
    el.querySelector(".fill").style.width = (c.stage==="copy" ? 100 : c.pct) + "%";
    const a = el.querySelector(".lk");
    if(c.link){ a.href = c.link; a.textContent = c.link; a.classList.remove("hide"); } else a.classList.add("hide");
  }
}

const POOLST = {ready:["Ready","var(--ok)"], busy:["Kaj korche","var(--info)"], flood:["FloodWait","var(--warn)"], error:["Error","var(--err)"]};
function renderPool(d){
  const html = d.pool.map(s => {
    const st = POOLST[s.status] || [s.status,"var(--muted)"];
    const extra = s.status==="flood" ? " " + s.left + "s" : (s.status==="error" && s.msg ? " · " + esc(s.msg) : "");
    return `<div class="row"><div class="n">${esc(s.name)} <span class="muted">${s.primary?"(main)":"(backup)"}</span><br>
      <span style="color:${st[1]}">${st[0]}${extra}</span> <span class="muted">· ${s.sent} file</span></div>
      <div class="m">${s.primary ? "" : `<button class="ghost" onclick="removeBackup(${s.id})">Remove</button>`}</div></div>`;
  }).join("");
  if($("poolList").dataset.h !== html){ $("poolList").innerHTML = html; $("poolList").dataset.h = html; }
}

async function addBackup(){
  const session = $("bk_sess").value.trim(), m = $("bk_msg");
  if(!session){ m.className = "msg e"; m.textContent = "Session string paste koro"; return; }
  $("bk_btn").disabled = true; m.className = "msg"; m.textContent = "Connect kora hocche...";
  try{
    const r = await post("api/backup/add",{session});
    if(r.ok){ m.className = "msg g"; m.textContent = "Add hoyeche: " + r.name; $("bk_sess").value = ""; }
    else { m.className = "msg e"; m.textContent = r.error; }
  }catch(e){ m.className = "msg e"; m.textContent = "Server error"; }
  $("bk_btn").disabled = false; load();
}
async function removeBackup(id){
  if(!confirm("Ei backup session muche felbe?")) return;
  await post("api/backup/remove",{id}); load();
}

async function load(){
  try{
    const d = await (await fetch("api/stats")).json();
    const b = $("badge"); b.textContent = d.status.replace("_"," "); b.className = "badge b-" + d.status;
    $("cg").innerHTML = d.cryptg ? 'Speed boost (cryptg): <span style="color:var(--ok)">ON</span>' : 'Speed boost (cryptg): <span style="color:var(--err)">OFF - speed kom hobe (~400 KB/s)</span>' + (d.cryptg_err ? '<br><span style="font-size:11px">' + esc(d.cryptg_err) + '</span>' : '');
    $("srcName").textContent = d.source_name || ("@" + d.cfg.source);
    $("tgtName").textContent = d.target_name || ("@" + d.cfg.target);
    if(avV !== d.avatar_v){
      avV = d.avatar_v;
      for(const [id,w,has] of [["avS","source",d.has_src_av],["avT","target",d.has_tgt_av]]){
        $(id).classList.toggle("hide", !has);
        if(has) $(id).src = "api/avatar?w=" + w + "&v=" + d.avatar_v;
      }
    }

    const showLogin = (d.status === "need_session" || d.status === "connecting");
    $("loginView").classList.toggle("hide", !showLogin);
    $("dashView").classList.toggle("hide", showLogin);
    if(showLogin){ if(!busy && d.error_msg) $("err").textContent = d.error_msg; return; }

    if(!formInit){ fillForm(d.cfg); formInit = true; }
    renderCtrl(d);
    renderActive(d);
    renderSingle(d);
    renderPool(d);
    $("me").textContent = d.me || "-";
    const left = Math.max(0, Math.ceil(d.flood_until - d.now));
    $("floodtxt").textContent = (d.status==="flood" && left) ? " · " + left + "s wait" : "";

    const pct = d.total ? Math.min(100, Math.round(d.sent/d.total*100)) : (d.status==="done"?100:0);
    const remain = Math.max(0, d.total - d.sent - d.failed - d.skipped_n);
    const up = d.now - d.started;
    const active = (d.status==="running"||d.status==="flood"||d.status==="paused");
    $("ptxt").textContent = d.sent + " / " + d.total;
    $("ppct").textContent = pct + "%";
    $("pbar").style.width = pct + "%";
    $("current").textContent = d.current || "-";

    $("sent").textContent = d.sent;
    $("remain").textContent = remain;
    $("eta").textContent = (active && d.sent > 0 && up > 30 && remain > 0) ? fmtDur(remain / (d.sent / up)) : "-";
    $("skipped_n").textContent = d.skipped_n;
    $("failed").textContent = d.failed;
    $("pos").textContent = d.pos;
    $("media_total").textContent = d.media_total;
    $("speed").textContent = (up > 30 && d.sent) ? (d.sent / (up/60)).toFixed(1) : "-";
    $("mbps").textContent = (up > 30 && d.bytes) ? fmtSize(d.bytes / up) + "/s" : "-";
    $("bytes").textContent = fmtSize(d.bytes);
    $("videos").textContent = d.videos;
    $("photos").textContent = d.photos;
    $("from_me").textContent = d.from_me;
    $("from_bot").textContent = d.from_bot;
    $("flood_count").textContent = d.flood_count;
    $("switches").textContent = d.switches;
    $("uptime").textContent = active ? fmtDur(up) : "-";

    $("failedList").innerHTML = d.failed_list.length ? d.failed_list.slice().reverse().map(r =>
      `<div class="row bad"><div class="n">#${r.no} · ${esc(r.name)}<br><span class="muted">${esc(r.reason)}${linkHtml(r.link)}</span></div>
       <div class="m">${fmtSize(r.size)}</div></div>`
    ).join("") : '<div class="muted">Kono failed file nai</div>';

    $("skipped").innerHTML = d.skipped.length ? d.skipped.map(r =>
      `<div class="row skip"><div class="n">#${r.no} · ${esc(r.name)}<br><span class="muted">${esc(r.reason)}${linkHtml(r.link)}</span></div>
       <div class="m">${fmtSize(r.size)}<br>${fmtTime(r.t)}</div></div>`
    ).join("") : '<div class="muted">Kichu skip hoyni</div>';

    $("recent").innerHTML = d.recent.length ? d.recent.map(r =>
      `<div class="row"><div class="n">#${r.no} · ${r.kind==="video"?"Video":"Chhobi"} · ${esc(r.name)}<span class="muted">${linkHtml(r.link)}</span></div>
       <div class="m">${fmtSize(r.size)}<br>${r.by==="me"?"ami":"onno"} · ${fmtTime(r.t)}</div></div>`
    ).join("") : '<div class="muted">Ekhono kichu jayni</div>';

    $("errors").innerHTML = d.errors.length ? d.errors.map(e =>
      `<div class="row bad"><div class="n">${esc(e.msg)}</div><div class="m">${fmtTime(e.t)}</div></div>`
    ).join("") : '<div class="muted">Kono error nai</div>';
  }catch(e){
    const b = $("badge"); b.textContent = "offline"; b.className = "badge b-error";
  }
}
load(); setInterval(load, 2500);
</script>
</body>
</html>
"""

MB = 1024 * 1024

client = None            # main (primary) session
worker_task = None
pause_ev = asyncio.Event()
pause_ev.set()
thumbs = {}              # n -> thumbnail bytes (chole thaka file)
avatars = {"source": None, "target": None}
send_tasks = {}          # n -> send task (skip korar jonno)
skip_flags = {}          # n -> True hole user skip koreche
dl_cache = {}            # n -> {"path":..., "thumb":...} (flood hole same file abar download na kore)
pool = []                # session slot (main + backup)
slot_seq = [0]
flood_waiters = [0]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


fail_reason = {}         # n -> error text
failed_store = load_json(FAILED_FILE, {})   # source key -> [ {no,id,name,size,link,reason} ]


def add_failed(key, rec):
    lst = failed_store.setdefault(key, [])
    if not any(r["id"] == rec["id"] for r in lst):
        lst.append(rec)
        del lst[:-1000]
        save_json(FAILED_FILE, failed_store)


def remove_failed(key, mid):
    lst = failed_store.get(key, [])
    new = [r for r in lst if r["id"] != mid]
    if len(new) != len(lst):
        failed_store[key] = new
        save_json(FAILED_FILE, failed_store)


cfg = dict(DEFAULT_CFG)
cfg.update(load_json(CONFIG_FILE, {}))
progress = load_json(PROGRESS_FILE, {})

state = {
    "status": "need_session",  # need_session|connecting|idle|running|paused|flood|done|error
    "started": time.time(),
    "me": "",
    "error_msg": "",
    "source_name": "",
    "target_name": "",
    "total": 0,
    "media_total": 0,
    "sent": 0, "photos": 0, "videos": 0,
    "from_me": 0, "from_bot": 0,
    "failed": 0, "skipped_n": 0, "bytes": 0,
    "pos": 0,
    "current": "",
    "active": {},
    "single": {"state": "idle", "msg": "", "reason": ""},
    "avatar_v": 0,
    "switches": 0,
    "flood_until": 0, "flood_count": 0,
    "recent": deque(maxlen=200),
    "skipped": deque(maxlen=100),
    "errors": deque(maxlen=30),
}


def reset_counters():
    for k in ("total", "sent", "photos", "videos", "from_me", "from_bot",
              "failed", "skipped_n", "bytes", "flood_count", "switches", "pos"):
        state[k] = 0
    state["current"] = ""
    state["active"] = {}
    thumbs.clear()
    state["flood_until"] = 0
    state["started"] = time.time()
    state["recent"].clear()
    state["skipped"].clear()


def log_error(text):
    state["errors"].appendleft({"t": time.time(), "msg": text})
    print(text, flush=True)


def parse_ref(v):
    v = str(v).strip()
    v = re.sub(r"^https?://(t|telegram)\.me/", "", v)
    v = v.split("?")[0].strip("/").lstrip("@")
    m = re.match(r"c/(\d+)", v)
    if m:
        return int("-100" + m.group(1))
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def ref_key(v):
    return str(parse_ref(v)).lower()


def name_of(e):
    t = getattr(e, "title", None)
    if t:
        return t
    n = (getattr(e, "first_name", "") or "") + " " + (getattr(e, "last_name", "") or "")
    n = n.strip()
    u = getattr(e, "username", None)
    return n + (f" (@{u})" if u else "") if n else (f"@{u}" if u else str(getattr(e, "id", "")))


def msg_link(ent, mid):
    u = getattr(ent, "username", None)
    if u:
        return f"https://t.me/{u}/{mid}"
    if ent.__class__.__name__ == "Channel":
        return f"https://t.me/c/{ent.id}/{mid}"
    return ""


async def resolve(ref, cl=None):
    cl = cl or client
    r = parse_ref(ref)
    if not r:
        raise ValueError("Khali")
    if isinstance(r, str) and (r.startswith("+") or r.startswith("joinchat/")):
        h = r[1:] if r.startswith("+") else r.split("/", 1)[1]
        res = await cl(CheckChatInviteRequest(h))
        if hasattr(res, "chat"):
            return res.chat
        raise ValueError("Ei group/channel e tumi join kora nai, age join koro")
    try:
        return await cl.get_entity(r)
    except Exception:
        if isinstance(r, int):
            await cl.get_dialogs()
            return await cl.get_entity(r)
        raise


async def set_entity_info(which, ent):
    state[which + "_name"] = name_of(ent)
    try:
        data = await client.download_profile_photo(ent, file=bytes)
        avatars[which] = data or None
    except Exception:
        avatars[which] = None
    state["avatar_v"] += 1


async def get_thumb(msg):
    try:
        if msg.photo:
            try:
                return await client.download_media(msg, file=bytes, thumb=1)
            except Exception:
                return await client.download_media(msg, file=bytes, thumb=0)
        return await client.download_media(msg, file=bytes, thumb=-1)
    except Exception:
        return None


def pick_filter(c):
    if c["photos"] and c["videos"]:
        return InputMessagesFilterPhotoVideo
    if c["photos"]:
        return InputMessagesFilterPhotos
    return InputMessagesFilterVideo


def set_running_status():
    state["status"] = "running" if pause_ev.is_set() else "paused"


# ---------- session pool (main + backup) ----------
def new_slot(cl, name, primary=False, session="", uid=0):
    slot_seq[0] += 1
    return {
        "id": slot_seq[0], "client": cl, "name": name, "primary": primary,
        "session": session, "uid": uid,
        "flood_until": 0, "bad_until": 0, "bad_msg": "",
        "busy": 0, "sent": 0,
        "src": None, "tgt": None, "src_ref": None, "tgt_ref": None,
    }


def slot_ready(s, now=None):
    now = now or time.time()
    return bool(s["client"]) and s["flood_until"] <= now and s["bad_until"] <= now


def pick_slot():
    ready = [s for s in pool if slot_ready(s)]
    if not ready:
        return None
    return min(ready, key=lambda s: (s["busy"], s["id"]))


async def wait_for_slot():
    now = time.time()
    times = [max(s["flood_until"], s["bad_until"]) for s in pool if s["client"]]
    if not times:
        raise RuntimeError("Kono session nai")
    wait = max(1.0, min(times) - now)
    flood_waiters[0] += 1
    state["status"] = "flood"
    state["flood_until"] = now + wait
    try:
        await asyncio.sleep(wait + 0.5)
    finally:
        flood_waiters[0] -= 1
        if flood_waiters[0] <= 0:
            flood_waiters[0] = 0
            state["flood_until"] = 0
            if state["status"] == "flood":
                set_running_status()


async def slot_entities(slot, src_ref, tgt_ref):
    if slot["src_ref"] != src_ref:
        slot["src"] = await resolve(src_ref, slot["client"])
        slot["src_ref"] = src_ref
    if slot["tgt_ref"] != tgt_ref:
        slot["tgt"] = await resolve(tgt_ref, slot["client"])
        slot["tgt_ref"] = tgt_ref


async def connect_extra(session):
    cl = TelegramClient(StringSession(session.strip()), API_ID, API_HASH,
                          receive_updates=False, flood_sleep_threshold=20)
    await cl.connect()
    if not await cl.is_user_authorized():
        await cl.disconnect()
        raise ValueError("Session valid na (logout hoye geche ba bhul string)")
    me = await cl.get_me()
    return cl, name_of(me), me.id


async def load_extras():
    for s in load_json(EXTRA_FILE, []):
        try:
            cl, name, uid = await connect_extra(s)
            pool.append(new_slot(cl, name, session=s, uid=uid))
        except Exception as e:
            slot = new_slot(None, "(connect hoyni)", session=s)
            slot["bad_msg"] = str(e)
            pool.append(slot)
            log_error(f"Backup session connect fail: {e}")


def save_extras():
    save_json(EXTRA_FILE, [s["session"] for s in pool if not s["primary"]])


# ---------- download / upload ----------
def make_cb(stage, n):
    t0 = time.time()

    def cb(done, total):
        c = state["active"].get(n)
        if not c:
            return
        c["stage"] = stage
        c["pct"] = int(done * 100 / total) if total else 0
        dt = time.time() - t0
        c["speed"] = done / dt if dt > 0 else 0
    return cb


def cleanup_tmp(n):
    d = dl_cache.pop(n, None)
    if d:
        for p in (d.get("path"), d.get("thumb")):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


CHUNK = 1024 * 1024  # 1 MB request (Telethon default 128 KB, tai slow - Pyrogram o 1 MB use kore)


def flood_secs(e):
    s = getattr(e, "seconds", None)
    try:
        return max(1, int(s))
    except Exception:
        return 5


def _rm(p):
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


async def _aclose(it):
    try:
        r = it.close()
        if asyncio.iscoroutine(r):
            await r
    except Exception:
        pass


async def _dl_ranges(cl, doc, path, size, parts, cb, n):
    total_chunks = (size + CHUNK - 1) // CHUNK
    parts = max(1, min(parts, total_chunks))
    per = (total_chunks + parts - 1) // parts
    done = [0]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        async def part(a, b):
            start, end = a * CHUNK, min(size, b * CHUNK)
            off = start
            floods = 0
            while off < end:
                it = cl.iter_download(doc, offset=off, request_size=CHUNK)
                try:
                    async for chunk in it:
                        take = min(len(chunk), end - off)
                        if take <= 0:
                            break
                        os.pwrite(fd, chunk[:take], off)
                        off += take
                        done[0] += take
                        cb(done[0], size)
                        if off >= end:
                            break
                    else:
                        break  # iterator shesh
                except FLOOD as e:
                    floods += 1
                    if floods > 6:
                        raise
                    secs = flood_secs(e)
                    state["flood_count"] += 1
                    c = state["active"].get(n)
                    if c:
                        c["stage"] = "wait"
                    await asyncio.sleep(secs + 1)  # wait kore ekhan theke-i abar suru
                finally:
                    await _aclose(it)
            if off != end:
                raise IOError(f"part incomplete {off}/{end}")

        tasks = []
        for i in range(parts):
            a, b = i * per, min(total_chunks, (i + 1) * per)
            if a < b:
                tasks.append(asyncio.create_task(part(a, b)))
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    finally:
        os.close(fd)


async def fast_download(cl, m, base, n):
    cb = make_cb("download", n)
    doc = getattr(m, "document", None)
    if not doc or not getattr(doc, "size", 0):
        return await cl.download_media(m, file=base, progress_callback=cb)
    size = doc.size
    ext = (m.file.ext or "") if m.file else ""
    path = base + ext
    parts = max(1, min(4, int(cfg.get("conn", 1))))
    try:
        await _dl_ranges(cl, doc, path, size, parts, cb, n)
        got = os.path.getsize(path)
        if got != size:
            raise IOError(f"size mismatch {got}/{size}")
        return path
    except asyncio.CancelledError:
        _rm(path)
        raise
    except FLOOD:
        _rm(path)
        raise
    except Exception as e:
        _rm(path)
        log_error(f"Fast download fail ({e}) -> normal download")
        return await cl.download_media(m, file=base, progress_callback=cb)


async def fast_upload(cl, target, path, caption, kwargs, n):
    if kwargs.get("attributes"):  # video/document: boro part (512 KB) diye upload
        handle = None
        try:
            handle = await cl.upload_file(
                path, part_size_kb=512, progress_callback=make_cb("upload", n)
            )
        except FLOOD:
            raise
        except Exception as e:
            log_error(f"Fast upload fail ({e}) -> normal upload")
        if handle is not None:
            try:
                await cl.send_file(target, handle, caption=caption, force_document=False, **kwargs)
                return
            except FLOOD:
                raise
            except Exception as e:
                log_error(f"Fast send fail ({e}) -> normal upload")
    await cl.send_file(target, path, caption=caption, progress_callback=make_cb("upload", n), **kwargs)


async def download_upload(cl, target, m, caption, n):
    d = dl_cache.setdefault(n, {})
    path = d.get("path")
    if not path or not os.path.exists(path):
        path = await fast_download(cl, m, os.path.join(BASE, f"tmp_{n}_{m.id}"), n)
        if not path:
            raise ValueError("download hoyni")
        d["path"] = path
    kwargs = {}
    if m.video and m.document:
        kwargs["attributes"] = m.document.attributes
        kwargs["supports_streaming"] = True
        th = d.get("thumb")
        if not th or not os.path.exists(th):
            try:
                th = await cl.download_media(
                    m, file=os.path.join(BASE, f"tmpthumb_{n}_{m.id}"), thumb=-1
                )
                d["thumb"] = th
            except Exception:
                th = None
        if th:
            kwargs["thumb"] = th
    await fast_upload(cl, target, path, caption, kwargs, n)


async def do_send(cl, target, m, caption, restricted, n):
    if restricted:
        await download_upload(cl, target, m, caption, n)
        return
    try:
        c = state["active"].get(n)
        if c:
            c["stage"] = "copy"
        await cl.send_file(target, m.media, caption=caption)
    except FLOOD:
        raise
    except Exception as e:
        log_error(f"Direct send fail ({m.id}): {e} -> download/upload")
        await download_upload(cl, target, m, caption, n)


async def send_with_pool(msg, caption, restricted, src_ref, tgt_ref, n):
    """Ekta file pathay. FloodWait hole onno session e SAME file abar chesta kore."""
    while True:
        slot = pick_slot()
        if slot is None:
            await wait_for_slot()
            continue
        slot["busy"] += 1
        try:
            cl = slot["client"]
            await slot_entities(slot, src_ref, tgt_ref)
            if slot["primary"]:
                m = msg
            else:
                m = await cl.get_messages(slot["src"], ids=msg.id)
                if not m:
                    raise ValueError("Ei session e message paoa jay na (source e join nai?)")
            await do_send(cl, slot["tgt"], m, caption, restricted, n)
            slot["sent"] += 1
            return True
        except FLOOD as e:
            slot["flood_until"] = time.time() + flood_secs(e) + 2
            state["flood_count"] += 1
            others = [s for s in pool if s is not slot and slot_ready(s)]
            if others:
                state["switches"] += 1
                log_error(f"FloodWait {flood_secs(e)}s ({slot['name']}) -> onno session e same file abar")
            else:
                log_error(f"FloodWait {flood_secs(e)}s ({slot['name']}) -> onno session nai, wait")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if slot["primary"]:
                log_error(f"Fail {msg.id}: {e}")
                fail_reason[n] = str(e)[:200]
                return False
            slot["bad_until"] = time.time() + 600
            slot["bad_msg"] = str(e)
            log_error(f"Backup session ({slot['name']}) e error: {e}")
        finally:
            slot["busy"] -= 1


def build_caption(msg, live):
    text = msg.text or ""
    if live["caption"]:
        if live["clean_caption"]:
            text = re.sub(r"(https?://)?(t|telegram)\.me/\S+|@\w+", "", text)
            text = re.sub(r"[ \t]+\n", "\n", text).strip()
    else:
        text = ""
    extra = (live.get("caption_extra") or "").strip()
    if extra:
        text = (text + "\n" + extra).strip()
    return text


def save_pos(key, msg_id, n):
    progress[key] = {"last_id": msg_id, "pos": n}
    save_json(PROGRESS_FILE, progress)


def advance_commit(key, commit):
    last = None
    while commit and commit[0][2]:
        last = commit.popleft()
    if last:
        save_pos(key, last[1], last[0])


async def run_item(msg, n, entry, commit, key, src_ref, link, fname, size, kind, single=False):
    state["active"][n] = {
        "no": n, "name": fname, "kind": kind, "size": size, "link": link,
        "by": "me" if msg.out else "bot", "stage": "wait", "pct": 0, "speed": 0,
        "has_thumb": False,
    }
    try:
        th = await get_thumb(msg)
        if th and n in state["active"]:
            thumbs[n] = th
            state["active"][n]["has_thumb"] = True

        live = cfg
        caption = build_caption(msg, live)
        skip_flags[n] = False
        task = asyncio.create_task(
            send_with_pool(msg, caption, live["restricted"], src_ref, live["target"], n)
        )
        send_tasks[n] = task
        try:
            ok = await task
        except asyncio.CancelledError:
            if not skip_flags.get(n):
                raise
            ok = None  # user ei file skip koreche

        if single:
            state["single"]["reason"] = "" if ok else ("Tumi skip korecho" if ok is None else fail_reason.pop(n, "error"))
            if ok:
                state["recent"].appendleft({
                    "no": 0, "id": msg.id, "name": fname, "kind": kind, "size": size,
                    "by": "me" if msg.out else "bot", "link": link, "t": time.time(),
                })
            return ok
        if ok is None:
            state["skipped_n"] += 1
            state["skipped"].appendleft({
                "no": n, "id": msg.id, "name": fname, "size": size,
                "reason": "Tumi skip korecho", "link": link, "t": time.time(),
            })
        elif ok:
            state["sent"] += 1
            state["bytes"] += size
            state["photos" if kind == "photo" else "videos"] += 1
            state["from_me" if msg.out else "from_bot"] += 1
            state["recent"].appendleft({
                "no": n, "id": msg.id, "name": fname, "kind": kind, "size": size,
                "by": "me" if msg.out else "bot", "link": link, "t": time.time(),
            })
            print(f"[{state['sent']}] #{n} msg {msg.id} ({size / MB:.1f} MB)", flush=True)
        else:
            state["failed"] += 1
            add_failed(key, {
                "no": n, "id": msg.id, "name": fname, "size": size, "link": link,
                "reason": fail_reason.pop(n, "error"), "t": time.time(),
            })
        if ok:
            remove_failed(key, msg.id)

        if entry is not None:
            entry[2] = True
            advance_commit(key, commit)
        if ok:
            await asyncio.sleep(float(cfg["delay"]))
    finally:
        send_tasks.pop(n, None)
        skip_flags.pop(n, None)
        cleanup_tmp(n)
        thumbs.pop(n, None)
        state["active"].pop(n, None)


async def worker(mode):
    pending = set()
    try:
        c = dict(cfg)
        source = await resolve(c["source"])
        target = await resolve(c["target"])
        await set_entity_info("source", source)
        await set_entity_info("target", target)
        flt = pick_filter(c)
        key = ref_key(c["source"])
        prog = progress.get(key, {})

        min_id, n, from_no = 0, 0, max(1, int(c["start_no"]))
        if mode == "resume" and prog.get("last_id"):
            min_id, n = prog["last_id"], prog.get("pos", 0)
            from_no = n + 1

        res = await client.get_messages(source, limit=0, filter=flt)
        media_total = res.total
        planned = max(0, media_total - (from_no - 1))
        if c["limit"]:
            planned = min(planned, int(c["limit"]))
        state["media_total"] = media_total
        state["total"] = planned
        set_running_status()

        parallel = max(1, min(8, int(c.get("parallel", 1))))
        sem = asyncio.Semaphore(parallel)
        commit = deque()
        taken = 0

        async for msg in client.iter_messages(source, reverse=True, min_id=min_id, filter=flt):
            n += 1
            if n < from_no:
                if n % 100 == 0:
                    state["current"] = f"#{from_no} porjonto skip kora hocche ({n})"
                continue
            if c["limit"] and taken >= int(c["limit"]):
                break

            await pause_ev.wait()
            live = cfg
            if live["target"] != c["target"]:
                target = await resolve(live["target"])
                c["target"] = live["target"]
                await set_entity_info("target", target)

            f = msg.file
            fname = f.name if f and f.name else f"{msg.id}{f.ext if f and f.ext else ''}"
            size = f.size if f and f.size else 0
            kind = "photo" if msg.photo else "video"
            link = msg_link(source, msg.id)
            size_mb = size / MB
            state["pos"] = n
            state["current"] = ""

            # ---- filters ----
            if (live["sender"] == "me" and not msg.out) or (live["sender"] == "others" and msg.out):
                state["skipped_n"] += 1
                commit.append([n, msg.id, True])
                advance_commit(key, commit)
                continue
            reason = None
            mx, mn = float(live["max_mb"]), float(live["min_mb"])
            if size and mx and size_mb > mx:
                reason = f"Boro: {size_mb:.1f} MB > {mx:g} MB"
            elif size and mn and size_mb < mn:
                reason = f"Chhoto: {size_mb:.1f} MB < {mn:g} MB"
            if reason:
                state["skipped_n"] += 1
                state["skipped"].appendleft({
                    "no": n, "id": msg.id, "name": fname, "size": size,
                    "reason": reason, "link": link, "t": time.time(),
                })
                commit.append([n, msg.id, True])
                advance_commit(key, commit)
                continue

            taken += 1
            await sem.acquire()
            entry = [n, msg.id, False]
            commit.append(entry)
            t = asyncio.create_task(
                run_item(msg, n, entry, commit, key, c["source"], link, fname, size, kind)
            )
            pending.add(t)

            def _done(tt):
                pending.discard(tt)
                sem.release()
            t.add_done_callback(_done)

        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)
        state["current"] = ""
        state["status"] = "done"
        print("Done.", flush=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        state["status"] = "error"
        log_error(f"Worker crash: {e}")
    finally:
        for t in list(pending):
            t.cancel()


def parse_msg_link(text):
    t = (text or "").strip()
    t = re.sub(r"^https?://", "", t)
    t = re.sub(r"^(www\.)?(t|telegram)\.me/", "", t).split("?")[0].split("#")[0].strip("/")
    t = re.sub(r"^s/", "", t)
    parts = t.split("/")
    if len(parts) >= 3 and parts[0] == "c" and parts[1].isdigit() and parts[-1].isdigit():
        return int("-100" + parts[1]), int(parts[-1])
    if len(parts) >= 2 and parts[-1].isdigit() and not parts[0].isdigit() and parts[0] != "c":
        return parts[0], int(parts[-1])
    raise ValueError("Link ta thik na. Format: https://t.me/c/123456/78 ba https://t.me/username/78")


async def run_single(ref, mid):
    sg = state["single"]
    try:
        src = await resolve(ref)
        msg = await client.get_messages(src, ids=mid)
        if not msg or not msg.media or not (msg.photo or msg.video or msg.document):
            raise ValueError("Ei message e kono photo/video/file nai")
        f = msg.file
        fname = f.name if f and f.name else f"{msg.id}{f.ext if f and f.ext else ''}"
        size = f.size if f and f.size else 0
        kind = "photo" if msg.photo else "video"
        n = 10_000_000 + (mid % 1_000_000)
        while n in state["active"]:
            n += 1
        sg.update(state="running", msg=f"{fname} ({size / MB:.1f} MB)", reason="")
        ok = await run_item(msg, n, None, None, "single", ref, msg_link(src, mid), fname, size, kind, single=True)
        if ok:
            sg["state"] = "done"
        else:
            sg["state"] = "error"
            sg["reason"] = sg.get("reason") or "Pathano jayni"
    except asyncio.CancelledError:
        sg.update(state="error", reason="Bondho kora hoyeche")
        raise
    except Exception as e:
        sg.update(state="error", reason=f"{type(e).__name__}: {e}")


async def retry_worker():
    pending = set()
    try:
        c = dict(cfg)
        source = await resolve(c["source"])
        target = await resolve(c["target"])
        await set_entity_info("source", source)
        await set_entity_info("target", target)
        key = ref_key(c["source"])
        items = list(failed_store.get(key, []))
        state["media_total"] = len(items)
        state["total"] = len(items)
        set_running_status()
        sem = asyncio.Semaphore(max(1, min(8, int(c.get("parallel", 1)))))
        for rec in items:
            await pause_ev.wait()
            msg = await client.get_messages(source, ids=rec["id"])
            if not msg or not msg.media:
                remove_failed(key, rec["id"])
                state["skipped_n"] += 1
                continue
            f = msg.file
            fname = f.name if f and f.name else f"{msg.id}{f.ext if f and f.ext else ''}"
            size = f.size if f and f.size else 0
            kind = "photo" if msg.photo else "video"
            await sem.acquire()
            t = asyncio.create_task(
                run_item(msg, rec["no"], None, None, key, c["source"],
                         msg_link(source, msg.id), fname, size, kind)
            )
            pending.add(t)

            def _done(tt):
                pending.discard(tt)
                sem.release()
            t.add_done_callback(_done)
        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)
        state["current"] = ""
        state["status"] = "done"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        state["status"] = "error"
        log_error(f"Retry crash: {e}")
    finally:
        for t in list(pending):
            t.cancel()


async def stop_worker():
    global worker_task
    if worker_task and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except BaseException:
            pass
    worker_task = None
    await asyncio.sleep(0)
    state["active"] = {}
    pause_ev.set()
    flood_waiters[0] = 0


def worker_alive():
    return worker_task is not None and not worker_task.done()


async def start_worker(mode):
    global worker_task
    await stop_worker()
    reset_counters()
    state["status"] = "running"
    worker_task = asyncio.create_task(retry_worker() if mode == "retry" else worker(mode))


async def stop_all():
    global client
    await stop_worker()
    for s in list(pool):
        if s["client"]:
            try:
                await s["client"].disconnect()
            except Exception:
                pass
    pool.clear()
    client = None


async def connect_session(session, from_startup=False):
    global client
    await stop_all()
    state["status"] = "connecting"
    state["error_msg"] = ""
    try:
        c = TelegramClient(StringSession(session.strip()), API_ID, API_HASH,
                           receive_updates=False, flood_sleep_threshold=20)
        await c.connect()
        if not await c.is_user_authorized():
            await c.disconnect()
            raise ValueError("Session valid na (logout hoye geche ba bhul string)")
        me = await c.get_me()
    except Exception as e:
        state["status"] = "need_session"
        state["error_msg"] = str(e)
        log_error(f"Connect fail: {e}")
        return False

    client = c
    with open(SESSION_FILE, "w") as f:
        f.write(session.strip())
    state["me"] = (me.first_name or "") + (f" (@{me.username})" if me.username else "")
    pool.append(new_slot(c, name_of(me), primary=True, session=session.strip(), uid=me.id))
    asyncio.create_task(load_extras())
    state["status"] = "idle"
    if from_startup and cfg.get("auto"):
        await start_worker("resume")
    return True


# ---------- web ----------
async def index(request):
    return web.Response(text=HTML, content_type="text/html")


def pool_info():
    now = time.time()
    out = []
    for s in pool:
        if not s["client"]:
            st = "error"
        elif s["flood_until"] > now:
            st = "flood"
        elif s["bad_until"] > now:
            st = "error"
        elif s["busy"] > 0:
            st = "busy"
        else:
            st = "ready"
        out.append({
            "id": s["id"], "name": s["name"], "primary": s["primary"], "status": st,
            "left": int(max(0, s["flood_until"] - now)), "msg": s["bad_msg"],
            "sent": s["sent"], "busy": s["busy"],
        })
    return out


async def api_stats(request):
    data = dict(state)
    data["recent"] = list(state["recent"])
    data["skipped"] = list(state["skipped"])
    data["errors"] = list(state["errors"])
    data["active"] = {str(k): v for k, v in state["active"].items()}
    if state["active"] and not state["current"]:
        data["current"] = f"{len(state['active'])} ta file cholche"
    data["now"] = time.time()
    data["cfg"] = cfg
    data["pool"] = pool_info()
    data["resume_pos"] = progress.get(ref_key(cfg["source"]), {}).get("pos", 0)
    fl = failed_store.get(ref_key(cfg["source"]), [])
    data["failed_list"] = fl[-100:]
    data["failed_saved"] = len(fl)
    data["cryptg"] = CRYPTG
    data["cryptg_err"] = CRYPTG_ERR
    data["has_src_av"] = bool(avatars["source"])
    data["has_tgt_av"] = bool(avatars["target"])
    return web.json_response(data)


async def api_thumb(request):
    try:
        n = int(request.query.get("n", "0"))
    except ValueError:
        n = 0
    d = thumbs.get(n)
    if not d:
        return web.Response(status=404)
    return web.Response(body=d, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def api_avatar(request):
    d = avatars.get(request.query.get("w"))
    if not d:
        return web.Response(status=404)
    return web.Response(body=d, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def api_connect(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"})
    session = (body.get("session") or "").strip()
    if not session:
        return web.json_response({"ok": False, "error": "Session string dao"})
    ok = await connect_session(session)
    return web.json_response({"ok": ok, "error": state["error_msg"]})


async def api_logout(request):
    await stop_all()
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    state["status"] = "need_session"
    state["me"] = ""
    state["error_msg"] = ""
    return web.json_response({"ok": True})


async def api_single(request):
    try:
        b = await request.json()
        ref, mid = parse_msg_link(b.get("link", ""))
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})
    if client is None:
        return web.json_response({"ok": False, "error": "Age session connect koro"})
    if state["single"]["state"] == "running":
        return web.json_response({"ok": False, "error": "Ekta single file ekhono cholche, shesh hole abar dao"})
    state["single"].update(state="running", msg="Prostuti...", reason="")
    asyncio.create_task(run_single(ref, mid))
    return web.json_response({"ok": True})


async def api_backup_add(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"})
    session = (body.get("session") or "").strip()
    if not session:
        return web.json_response({"ok": False, "error": "Session string dao"})
    if client is None:
        return web.json_response({"ok": False, "error": "Age main session connect koro"})
    if any(s["session"] == session for s in pool):
        return web.json_response({"ok": False, "error": "Ei session already deya ache"})
    try:
        cl, name, uid = await connect_extra(session)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})
    if any(s["uid"] == uid for s in pool if s["uid"]):
        await cl.disconnect()
        return web.json_response({"ok": False, "error": "Ei account already ache (main ba onno backup e)"})
    pool.append(new_slot(cl, name, session=session, uid=uid))
    save_extras()
    return web.json_response({"ok": True, "name": name})


async def api_backup_remove(request):
    try:
        body = await request.json()
        sid = int(body.get("id"))
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"})
    slot = next((s for s in pool if s["id"] == sid and not s["primary"]), None)
    if not slot:
        return web.json_response({"ok": False, "error": "Paoa jayni"})
    pool.remove(slot)
    if slot["client"]:
        try:
            await slot["client"].disconnect()
        except Exception:
            pass
    save_extras()
    return web.json_response({"ok": True})


async def api_settings(request):
    try:
        b = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"})
    new = dict(cfg)
    try:
        new["source"] = str(b.get("source", "")).strip()
        new["target"] = str(b.get("target", "")).strip()
        new["start_no"] = max(1, int(b.get("start_no", 1)))
        new["limit"] = max(0, int(b.get("limit", 0)))
        new["parallel"] = max(1, min(8, int(b.get("parallel", 1))))
        new["conn"] = max(1, min(4, int(b.get("conn", 1))))
        new["delay"] = max(0.0, float(b.get("delay", 1.5)))
        new["max_mb"] = max(0.0, float(b.get("max_mb", 0)))
        new["min_mb"] = max(0.0, float(b.get("min_mb", 0)))
        new["photos"] = bool(b.get("photos"))
        new["videos"] = bool(b.get("videos"))
        new["caption"] = bool(b.get("caption"))
        new["auto"] = bool(b.get("auto"))
        new["restricted"] = bool(b.get("restricted"))
        new["clean_caption"] = bool(b.get("clean_caption"))
        new["caption_extra"] = str(b.get("caption_extra", ""))[:500]
        new["sender"] = b.get("sender") if b.get("sender") in ("all", "me", "others") else "all"
    except Exception:
        return web.json_response({"ok": False, "error": "Number gulo thik koro"})
    if not new["source"] or not new["target"]:
        return web.json_response({"ok": False, "error": "Source ar Destination dite hobe"})
    if not new["photos"] and not new["videos"]:
        return web.json_response({"ok": False, "error": "Kompokkhe ekta type (chhobi/video) tick koro"})
    if new["max_mb"] and new["min_mb"] and new["min_mb"] > new["max_mb"]:
        return web.json_response({"ok": False, "error": "Min size Max size er cheye boro hote pare na"})

    if client:
        try:
            s = await resolve(new["source"])
            if new["source"] != cfg["source"] or not avatars["source"]:
                await set_entity_info("source", s)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"Source paoa jayni: {e}"})
        try:
            t = await resolve(new["target"])
            if new["target"] != cfg["target"] or not avatars["target"]:
                await set_entity_info("target", t)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"Destination paoa jayni: {e}"})

    heavy = ("source", "photos", "videos", "start_no", "limit", "parallel")
    changed_heavy = any(new[k] != cfg[k] for k in heavy)
    cfg.update(new)
    save_json(CONFIG_FILE, cfg)

    restarted = False
    if worker_alive() and changed_heavy:
        await start_worker("start")
        restarted = True
    return web.json_response({"ok": True, "restarted": restarted})


async def api_control(request):
    try:
        b = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"})
    a = b.get("action")
    if client is None:
        return web.json_response({"ok": False, "error": "Age session connect koro"})
    if a in ("start", "resume", "retry"):
        await start_worker(a)
    elif a == "skip":
        n = b.get("n")
        keys = list(send_tasks.keys()) if n is None else [int(n)]
        cnt = 0
        for k in keys:
            t = send_tasks.get(k)
            if t and not t.done():
                skip_flags[k] = True
                t.cancel()
                cnt += 1
        if not cnt:
            return web.json_response({"ok": False, "error": "Ekhon skip korar moto kono file cholche na"})
    elif a == "pause":
        if worker_alive():
            pause_ev.clear()
            state["status"] = "paused"
    elif a == "continue":
        if worker_alive():
            pause_ev.set()
            state["status"] = "running"
    elif a == "stop":
        await stop_worker()
        state["status"] = "idle"
        state["current"] = ""
    elif a == "reset":
        await stop_worker()
        progress.pop(ref_key(cfg["source"]), None)
        save_json(PROGRESS_FILE, progress)
        state["status"] = "idle"
        state["current"] = ""
    else:
        return web.json_response({"ok": False, "error": "unknown action"})
    return web.json_response({"ok": True})


async def on_startup(app):
    session = SESSION.strip()
    if not session and os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f:
            session = f.read().strip()
    if session:
        asyncio.create_task(connect_session(session, from_startup=True))


def make_app():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/thumb", api_thumb)
    app.router.add_get("/api/avatar", api_avatar)
    app.router.add_post("/api/connect", api_connect)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_post("/api/control", api_control)
    app.router.add_post("/api/single", api_single)
    app.router.add_post("/api/backup/add", api_backup_add)
    app.router.add_post("/api/backup/remove", api_backup_remove)
    app.on_startup.append(on_startup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
