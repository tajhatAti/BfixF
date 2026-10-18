"""Admin design system — one file, one look, reusable classes.

The owner asked for a black-and-yellow, genuinely *designed* admin panel. Instead of
patching colours here and there, everything the admin screens need lives in this
module:

* :data:`THEME_CSS` — the design system. Tokens, shell (rail sidebar + topbar +
  content), cards, KPI tiles, tables, forms, alerts, empty states, tabs,
  breadcrumbs, progress, print rules, mobile rules.
* :func:`page_head` — the title / subtitle / action row every page starts with.
* :func:`kpi` / :func:`kpi_row` — the numbers row on the dashboards.
* :func:`note` / :func:`empty` — the two blocks that make a page feel finished.
* :func:`tabs` / :func:`crumbs` — navigation inside and above a page.
* :data:`PALETTE_JS` — the Ctrl+K command palette (plain JavaScript, no CDN, works
  offline; it searches the same navigation the sidebar shows).

Class names used by the older pages (`.grid`, `.card`, `.pill`, `.flash`,
`.warnbox`, `.quick`, `.settings-section`, `.logline`, …) are all styled here too,
so nothing had to be rewritten just to look right.
"""
from __future__ import annotations

from html import escape

#: Brand shown in the rail when the owner has not set their own title.
DEFAULT_BRAND = "Store Bot"

THEME_CSS = """
/* ============================================================================
   Desi Site admin — black and yellow workspace.
   Black surfaces, yellow actions, status colours for real feedback. No gradients, no
   blue, no shadows except a hairline. Everything is a rule or a square.
   ========================================================================== */
:root {
  color-scheme: dark;
  --bg:#0c0c0c; --surface:#171717; --surface-2:#1c1c1c; --surface-3:#242424;
  --rail:#080808; --rail-ink:#ffe45c; --rail-muted:#b5ab78;
  --ink:#faf5dc; --ink-soft:#dfd7b8; --muted:#b6af96; --faint:#918a73;
  --line:#333025; --line-2:#49422c; --line-3:#facc15;
  --accent:#facc15; --accent-ink:#111111;
  --ok:#a9df91; --ok-bg:#152619; --warn:#facc15; --warn-bg:#2b2512;
  --bad:#ff9c8f; --bad-bg:#301b18;
  --r:4px; --r-sm:3px; --r-pill:999px;
  --font:"Hind Siliguri","Bricolage Grotesque",system-ui,"Noto Sans Bengali",sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,"Cascadia Mono",monospace;
  --rail-w:236px; --top-w:60px;
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; scroll-behavior:smooth; }
body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--font);
       font-size:14.5px; line-height:1.62; }
h1,h2,h3,h4,h5 { margin:0; font-weight:700; letter-spacing:-.01em; line-height:1.3;
                 font-family:"Bricolage Grotesque",var(--font); }
a { color:var(--ink); text-decoration:underline; text-decoration-color:var(--line-2);
    text-underline-offset:3px; }
a:hover { text-decoration-color:var(--ink); }
::selection { background:var(--accent); color:var(--accent-ink); }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

/* ------------------------------------------------------------------ shell */
.shell { display:grid; grid-template-columns:var(--rail-w) minmax(0,1fr); min-height:100vh; }
aside.side { position:sticky; top:0; align-self:start; height:100vh; overflow-y:auto;
             background:var(--rail); color:var(--rail-ink); padding:16px 12px 26px; }
aside.side .brand { display:flex; align-items:center; gap:10px; padding:6px 8px 14px;
                    color:#fff; font-weight:700; font-size:14.5px;
                    border-bottom:1px solid #1e1e1e; margin-bottom:12px; }
aside.side .brand small { display:block; font-weight:500; color:var(--rail-muted);
                          font-size:10.5px; letter-spacing:.06em; text-transform:uppercase; }
aside.side .brand .logo { flex:0 0 auto; width:30px; height:30px; display:grid; place-items:center;
                          background:var(--accent); color:var(--accent-ink); border-radius:var(--r); font-size:15px; }
aside.side a { display:flex; align-items:center; gap:10px; padding:8px 10px; margin:1px 0;
               color:#b6b6b0; border-radius:var(--r-sm); font-size:13.5px;
               text-decoration:none; border-left:2px solid transparent; }
aside.side a .ic { width:16px; text-align:center; font-size:13px; opacity:.9; }
aside.side a:hover { color:#fff; background:#1a1a1a; }
aside.side a.on { color:var(--accent-ink); background:var(--accent); font-weight:700; }
aside.side .grp { margin:16px 10px 5px; color:#6e6e69; font-size:9.5px; font-weight:800;
                  letter-spacing:.14em; text-transform:uppercase; }
aside.side .rail-foot { margin-top:18px; padding:12px 10px 0; border-top:1px solid #1e1e1e;
                        color:var(--rail-muted); font-size:11px; display:grid; gap:6px; }
aside.side .rail-foot a { color:#9d9d97; font-size:12.5px; }
details.navgrp { margin:0; border:0; background:transparent; }
details.navgrp > summary { display:flex; align-items:center; justify-content:space-between;
    cursor:pointer; list-style:none; padding:7px 10px 5px; color:#7f7f7a; font-size:9.5px;
    font-weight:800; letter-spacing:.14em; text-transform:uppercase; }
details.navgrp > summary::-webkit-details-marker { display:none; }
details.navgrp > summary:after { content:"+"; color:#5a5a55; font-size:11px; }
details.navgrp[open] > summary:after { content:"–"; }
details.navgrp > summary:hover { color:#fff; }

.topbar { position:sticky; top:0; z-index:15; min-height:var(--top-w); display:flex; gap:12px;
          align-items:center; justify-content:space-between; padding:0 22px;
          background:rgba(12,12,12,.94); backdrop-filter:blur(8px);
          border-bottom:1px solid var(--line-2); }
.topbar .row { gap:10px; }
.topbar .crumbs { display:flex; align-items:center; gap:8px; color:var(--muted); font-size:12px; }
.topbar .crumbs b { color:var(--ink); font-size:15px; font-weight:700; letter-spacing:-.01em; }
.topbar .crumbs span.sep { color:var(--faint); }
.burger { display:none; }
.status { display:inline-flex; align-items:center; gap:7px; padding:6px 10px; font-size:12px;
          color:var(--muted); background:var(--surface); border:1px solid var(--line-2);
          border-radius:var(--r-pill); text-decoration:none; }
.status i { width:7px; height:7px; border-radius:50%; background:var(--faint); }
.status.on { color:var(--ok); } .status.on i { background:var(--ok); }
.status.err { color:var(--bad); } .status.err i { background:var(--bad); }
.content { min-width:0; display:flex; flex-direction:column; }
.content main { width:min(1240px,100%); padding:22px 22px 40px; }
.content footer { margin-top:auto; padding:16px 22px 26px; color:var(--faint); font-size:11.5px;
                  border-top:1px solid var(--line); }

/* ------------------------------------------------------------- page header */
.page-heading { display:flex; flex-wrap:wrap; gap:14px; align-items:flex-end;
                justify-content:space-between; margin:2px 0 18px;
                padding-bottom:12px; border-bottom:1px solid var(--line-3); }
.page-heading h2 { font-size:24px; }
.page-heading .eyebrow, .eyebrow { margin:0 0 4px; color:var(--muted); font-size:10.5px;
                font-weight:800; letter-spacing:.16em; text-transform:uppercase; }
.page-heading .sub { margin:6px 0 0; color:var(--muted); font-size:13px; max-width:70ch; }
.page-heading .actions { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.crumbs { display:flex; flex-wrap:wrap; gap:6px; align-items:center; color:var(--muted);
          font-size:11.5px; }
.crumbs a { color:var(--muted); text-decoration:none; }
.crumbs a:hover { color:var(--ink); text-decoration:underline; }
.crumbs .sep { color:var(--faint); }

/* ------------------------------------------------------------- containers */
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }
.grid.two { grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); }
.card { padding:16px 18px; background:var(--surface); border:1px solid var(--line-2);
        border-radius:var(--r); }
.card + .card, .card + details, details + .card { margin-top:12px; }
.card > h4:first-child, .card > h3:first-child { margin-top:0; }
.card h4 { font-size:14.5px; margin:0 0 6px; }
.card h4 + .muted { margin-top:-2px; }
.card .l { color:var(--muted); font-size:10.5px; font-weight:800; letter-spacing:.1em;
           text-transform:uppercase; }
.card .n { margin-top:2px; font-size:26px; font-weight:800; letter-spacing:-.03em;
           font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
details { margin:12px 0; padding:14px 16px; background:var(--surface);
          border:1px solid var(--line-2); border-radius:var(--r); }
details summary { cursor:pointer; font-weight:700; }
details p { color:var(--muted); margin:8px 0 2px; }
hr { border:0; border-top:1px solid var(--line); margin:18px 0; }

/* --------------------------------------------------------------- KPI tiles */
.kpi-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr));
           gap:12px; margin:0 0 18px; }
.kpi { padding:14px 16px; background:var(--surface); border:1px solid var(--line-2);
       border-radius:var(--r); display:flex; flex-direction:column; gap:2px; }
.kpi .k-label { color:var(--muted); font-size:10.5px; font-weight:800;
                letter-spacing:.1em; text-transform:uppercase; }
.kpi .k-value { font-size:27px; font-weight:800; letter-spacing:-.03em;
                font-variant-numeric:tabular-nums; }
.kpi .k-sub { color:var(--muted); font-size:11.5px; }
.kpi .k-sub b { color:var(--ink); }
.kpi.dark { background:var(--rail); border-color:var(--rail); color:#fff; }
.kpi.dark .k-label, .kpi.dark .k-sub { color:var(--rail-muted); }
.kpi.dark .k-sub b { color:#fff; }

/* ------------------------------------------------------------------ tables */
.table-wrap { overflow-x:auto; border:1px solid var(--line-2); border-radius:var(--r);
              background:var(--surface); margin:12px 0; }
.table-wrap > table { border:0; border-radius:0; margin:0; box-shadow:none; }
table { width:100%; margin:12px 0; border-collapse:separate; border-spacing:0;
        background:var(--surface); border:1px solid var(--line-2); border-radius:var(--r);
        font-size:13px; overflow:hidden; }
th, td { padding:10px 12px; text-align:left; vertical-align:top;
         border-bottom:1px solid var(--line); }
th { position:sticky; top:0; z-index:1; background:var(--surface-3); color:var(--ink-soft);
     font-size:10px; font-weight:800; letter-spacing:.1em; text-transform:uppercase;
     border-bottom:1px solid var(--line-2); }
tbody tr:nth-child(even) td { background:var(--surface-2); }
tbody tr:hover td { background:#302a16; }
tr:last-child td { border-bottom:0; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
td .muted, .muted { color:var(--muted); font-size:12px; }
table .nowrap { white-space:nowrap; }

/* ------------------------------------------------------------------- tags */
.pill, .badge { display:inline-flex; align-items:center; gap:5px; max-width:100%;
    padding:2px 8px; background:var(--surface-3); color:var(--ink-soft);
    border:1px solid var(--line-2); border-radius:var(--r-pill); font-size:11px;
    font-weight:700; white-space:nowrap; }
.pill.ok, .badge.ok { color:var(--ok); background:var(--ok-bg); border-color:#cfe3d6; }
.pill.warn, .badge.warn { color:var(--warn); background:var(--warn-bg); border-color:#e8dcc1; }
.pill.bad, .badge.bad { color:var(--bad); background:var(--bad-bg); border-color:#eed3cf; }
.pill.plain { background:transparent; border-style:dashed; color:var(--muted); font-weight:600; }

/* ---------------------------------------------------------------- buttons */
.row { display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
.row form { margin:0; }
a.btn, button, input[type=submit] { display:inline-flex; align-items:center;
    justify-content:center; gap:7px; min-height:36px; padding:7px 13px;
    border:1px solid var(--line-3); border-radius:var(--r-sm); background:var(--accent);
    color:var(--accent-ink); font:inherit; font-size:12.5px; font-weight:700; line-height:1.3;
    text-decoration:none; cursor:pointer; transition:background .12s,border-color .12s; }
a.btn:hover, button:hover, input[type=submit]:hover { background:#ffe45c; color:#111; }
a.btn.grey, button.grey { background:var(--surface); color:var(--ink); border-color:var(--line-2); }
a.btn.grey:hover, button.grey:hover { border-color:var(--ink); background:var(--surface-3); }
a.btn.ok, button.ok { background:var(--ink); }
a.btn.bad, button.bad { background:var(--bad); border-color:var(--bad); }
a.btn.bad:hover, button.bad:hover { background:#8d1c13; }
a.btn.warn, button.warn { background:var(--surface); color:var(--warn); border-color:#e0d3b6; }
a.btn.warn:hover, button.warn:hover { background:var(--warn-bg); color:var(--warn); }
button[disabled], a.btn[disabled] { opacity:.5; cursor:not-allowed; }
.btn-row { display:flex; flex-wrap:wrap; gap:8px; margin:12px 0; }
.btn-row.tight { margin:8px 0 0; }

/* ------------------------------------------------------------------ forms */
label { display:block; margin:0 0 4px; color:var(--ink-soft); font-size:12px; font-weight:700; }
input, textarea, select { display:block; width:100%; margin:0 0 14px; padding:9px 11px;
    background:var(--surface); color:var(--ink); border:1px solid var(--line-2);
    border-radius:var(--r-sm); font:inherit; font-size:13.5px; }
input:focus, textarea:focus, select:focus { outline:0; border-color:var(--ink);
    box-shadow:0 0 0 2px rgba(0,0,0,.07); }
textarea { min-height:78px; resize:vertical; }
input[type=checkbox], input[type=radio] { display:inline-block; width:auto; margin:0;
    accent-color:var(--ink); }
input[type=number] { max-width:220px; }
.chk { display:flex; align-items:center; gap:9px; margin:8px 0; color:var(--ink-soft);
       font-size:13px; }
.chk label { margin:0; font-weight:600; }
.form-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
             gap:0 18px; }
code, kbd, samp { padding:1px 5px; background:var(--surface-3); color:var(--ink);
    border:1px solid var(--line); border-radius:var(--r-sm); font-family:var(--mono);
    font-size:12.5px; }
pre { margin:12px 0; padding:12px 14px; background:var(--surface-3);
      border:1px solid var(--line); border-radius:var(--r); overflow:auto;
      font-family:var(--mono); font-size:12.5px; }
.kv { display:grid; grid-template-columns:minmax(120px,auto) 1fr; gap:6px 16px; font-size:13px; }
.kv div:nth-child(odd) { color:var(--muted); }

/* ------------------------------------------------------------------ alerts */
.flash, .warnbox, .note { padding:11px 14px; margin:0 0 16px; border:1px solid var(--line-2);
    border-left:3px solid var(--ink); border-radius:var(--r); background:var(--surface);
    font-size:13px; }
.warnbox, .note.warn { border-left-color:var(--warn); background:var(--warn-bg); color:var(--warn); }
.note.bad { border-left-color:var(--bad); background:var(--bad-bg); color:var(--bad); }
.note.ok { border-left-color:var(--ok); background:var(--ok-bg); color:var(--ok); }
.note b { color:inherit; }
.note p:last-child { margin-bottom:0; }
.empty { padding:30px 20px; text-align:center; background:var(--surface);
    border:1px dashed var(--line-2); border-radius:var(--r); color:var(--muted); }
.empty .e-icon { font-size:26px; display:block; margin-bottom:6px; }
.empty b { display:block; color:var(--ink); font-size:14px; margin-bottom:2px; }
.empty .e-actions { margin-top:12px; display:flex; gap:8px; justify-content:center;
    flex-wrap:wrap; }

/* --------------------------------------------------------------- progress */
.bar { height:8px; overflow:hidden; background:var(--surface-3);
       border:1px solid var(--line-2); border-radius:var(--r-pill); margin:10px 0; }
.bar > i { display:block; height:100%; background:var(--ink); transition:width .3s; }

/* ------------------------------------------------------------------- tabs */
.tabs { display:flex; flex-wrap:wrap; gap:4px; margin:0 0 16px;
        border-bottom:1px solid var(--line-2); }
.tabs a { padding:8px 12px; color:var(--muted); font-size:13px; font-weight:600;
          text-decoration:none; border-bottom:2px solid transparent; margin-bottom:-1px; }
.tabs a:hover { color:var(--ink); }
.tabs a.on { color:var(--ink); border-bottom-color:var(--ink); }

/* --------------------------------------------------------- misc utilities */
.logline { padding:4px 2px; color:var(--ink-soft); border-bottom:1px solid var(--line);
    white-space:pre-wrap; word-break:break-word; font-family:var(--mono); font-size:12px; }
.logline:first-child { color:var(--ink); font-weight:600; }
.steps { padding-left:20px; }
.steps li { margin:8px 0; }
/* The shared base stylesheet (`web.dashboard.CSS`) still paints the old blue/teal
   gradient on a few elements the setup wizard and old snippets use. Inside the
   panel everything must be black & white, so they are flattened here. */
.step .num, .hero, .tick, .spin {
  background:var(--ink); background-image:none; color:#fff; border-color:var(--ink);
  box-shadow:none; }
.hero { color:var(--ink); background:var(--surface); border:1px solid var(--line-2); }
.hero h2, .hero h1 { color:var(--ink); }
.hero .muted, .hero p { color:var(--muted); }
.spin { background:transparent; border-color:var(--line-2); border-top-color:var(--ink); }
.tick { background:var(--ok-bg); color:var(--ok); }
.fe { color:var(--bad); }
.wiz .head .step, .wiz .num { background-image:none; }
.quick { display:grid; grid-template-columns:repeat(auto-fill,minmax(196px,1fr));
         gap:10px; margin:0 0 18px; }
.quick a { display:flex; gap:11px; align-items:center; padding:12px 13px;
    background:var(--surface); border:1px solid var(--line-2); border-radius:var(--r);
    text-decoration:none; }
.quick a:hover { border-color:var(--ink); }
.quick .qi { width:32px; height:32px; flex:0 0 auto; display:grid; place-items:center;
    background:var(--ink); color:#fff; border-radius:var(--r-sm); font-size:14px; }
.quick b { display:block; font-size:13px; color:var(--ink); }
.quick span { display:block; font-size:11px; color:var(--muted); }
.settings-section { padding:0; border:0; background:transparent; margin:0 0 10px; }
.settings-section > summary { display:flex; align-items:center; justify-content:space-between;
    gap:12px; padding:13px 16px; background:var(--surface); border:1px solid var(--line-2);
    border-radius:var(--r); font-size:14px; }
.settings-section[open] > summary { border-radius:var(--r) var(--r) 0 0;
    border-bottom-color:var(--line); }
.settings-section .settings-inner { margin:0; padding:14px 16px; background:var(--surface);
    border:1px solid var(--line-2); border-top:0; border-radius:0 0 var(--r) var(--r); }
.settings-section .settings-inner h4 { margin-top:0; }
.jump { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 16px; }
.jump a { padding:4px 9px; background:var(--surface); border:1px solid var(--line-2);
    border-radius:var(--r-pill); font-size:12px; text-decoration:none; color:var(--ink-soft); }
.jump a:hover { border-color:var(--ink); }

/* --------------------------------------------------- command palette (JS) */
.palette { position:fixed; inset:0; z-index:60; display:none; padding:10vh 16px 16px;
    background:rgba(0,0,0,.42); }
.palette.open { display:block; }
.palette .panel { width:min(620px,100%); margin:0 auto; background:var(--surface);
    border:1px solid var(--line-3); border-radius:var(--r); overflow:hidden; }
.palette input { margin:0; border:0; border-bottom:1px solid var(--line-2);
    border-radius:0; font-size:15px; padding:15px 16px; }
.palette input:focus { box-shadow:none; }
.palette ul { list-style:none; margin:0; padding:6px; max-height:52vh; overflow:auto; }
.palette li a { display:flex; gap:10px; align-items:center; padding:9px 11px;
    border-radius:var(--r-sm); text-decoration:none; color:var(--ink); font-size:13.5px; }
.palette li a .p-group { margin-left:auto; color:var(--faint); font-size:11px; }
.palette li a:hover, .palette li a.on { background:var(--ink); color:#fff; }
.palette li a.on .p-group { color:#cfcfc8; }
.palette .hint { padding:9px 14px; border-top:1px solid var(--line);
    color:var(--muted); font-size:11.5px; }
.palette .hint kbd { font-size:11px; }
.kbd-btn { display:inline-flex; align-items:center; gap:6px; padding:5px 9px;
    background:var(--surface); border:1px solid var(--line-2); border-radius:var(--r-pill);
    color:var(--muted); font-size:11.5px; cursor:pointer; }

/* --------------------------------------------------------------- notebook */
.timeline { list-style:none; margin:12px 0; padding:0; }
.share-links { list-style:none; margin:10px 0 0; padding:0; }
.share-links li { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  padding:8px 0; border-bottom:1px solid var(--line-3); font-size:13px; }
.share-links li:last-child { border-bottom:0; }
.share-links code { background:var(--surface-2); border:1px solid var(--line-3); border-radius:6px;
  padding:4px 8px; font-size:12.5px; word-break:break-all; }
.timeline li { position:relative; padding:0 0 14px 18px; border-left:1px solid var(--line-2); }
.timeline li:before { content:""; position:absolute; left:-4px; top:6px; width:7px; height:7px;
    background:var(--ink); border-radius:50%; }
.timeline li:last-child { padding-bottom:0; }
.timeline .t-when { color:var(--muted); font-size:11.5px; }
/* a plain bar chart for the visitors page — height = visitors, no colour games */
.visitors-chart { display:flex; align-items:flex-end; gap:4px; height:130px; margin:12px 0 6px; }
.visitors-chart .bar { flex:1 1 0; display:flex; flex-direction:column; justify-content:flex-end;
    align-items:center; gap:4px; height:100%; }
.visitors-chart .bar i { display:block; width:100%; background:var(--ink); border-radius:3px 3px 0 0;
    min-height:3px; }
.visitors-chart .bar span { font-size:9.5px; color:var(--muted); }

/* ---------------------------------------------------------------- mobile */
@media (max-width:900px) {
  .shell { grid-template-columns:minmax(0,1fr); }
  aside.side { position:fixed; top:0; left:0; bottom:0; width:min(280px,86vw); height:100vh;
      z-index:40; transform:translateX(-102%); transition:transform .18s ease;
      box-shadow:0 0 0 1px #1e1e1e; }
  body.nav aside.side { transform:none; }
  .burger { display:inline-flex; }
  .topbar { padding:0 14px; gap:8px; }
  .topbar .crumbs b { font-size:13.5px; }
  .content main { padding:16px 14px 32px; }
  .page-heading { align-items:flex-start; }
  th, td { padding:9px 10px; }
  .kpi .k-value { font-size:23px; }
}
@media print {
  aside.side, .topbar .status, .btn, button, .kbd-btn { display:none !important; }
  body { background:#fff; }
  .card, table, .kpi { border-color:#000; }
  a { text-decoration:none; color:#000; }
}
"""

# Final shared rules also cover older setup/login snippets from dashboard.CSS.
THEME_CSS += """
.wiz, .wiz .head, .wiz .box, .login, .login-box, details, .settings-section {
  background:var(--surface); color:var(--ink); border-color:var(--line); }
.step .num, .wiz .logo, .step .logo { background:var(--accent); color:#111; }
code, pre, .share-links code { background:var(--surface-2); color:var(--accent); }
.status, .burger, .howto span { background:var(--surface-2); color:var(--ink); }
.hero, .flash, .warnbox { background:var(--surface); border-color:var(--line-2); }
img, video { max-width:100%; }
.card, .box, .content, main, .wiz { min-width:0; overflow-wrap:anywhere; }
.table-wrap { max-width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }
@media(max-width:600px) {
  body { overflow-x:hidden; }
  input, select, textarea { font-size:16px; min-height:44px; }
  button, a.btn { min-height:44px; }
  .wiz { width:calc(100% - 24px); margin:16px auto; padding:16px; }
  .wiz .head { padding:12px 0; }
  .row { gap:8px; }
  .row > input, .row > select { max-width:100%; }
  .page-heading, .topbar { flex-wrap:wrap; gap:12px; }
  .grid, .kpis, .settings-grid { grid-template-columns:minmax(0,1fr); }
  table { display:block; overflow-x:auto; max-width:100%; }
}
"""

#: Ctrl+K / “/” command palette. No dependency, no network: it searches the same
#: navigation the sidebar renders, so a page can never be missing from it.
PALETTE_JS = """<div class="palette" id="palette" role="dialog" aria-modal="true" aria-label="কমান্ড">
  <div class="panel">
    <input id="palette-input" placeholder="যেকোনো পেজ বা কাজ খুঁজুন… (↑↓, Enter)" autocomplete="off">
    <ul id="palette-list"></ul>
    <div class="hint"><kbd>Ctrl</kbd>+<kbd>K</kbd> খুলুন · <kbd>Esc</kbd> বন্ধ · <kbd>↑</kbd><kbd>↓</kbd> বাছুন · <kbd>Enter</kbd> যান</div>
  </div>
</div>
<script>(function(){
  var box = document.getElementById('palette');
  if (!box) return;
  var input = document.getElementById('palette-input');
  var list = document.getElementById('palette-list');
  var items = [], cursor = 0;
  function load(){
    var out = [];
    document.querySelectorAll('aside.side a[href]').forEach(function(a){
      if (a.closest('.rail-foot')) return;
      var group = a.closest('details');
      var gname = group && group.querySelector('summary') ? group.querySelector('summary').textContent.trim() : '';
      out.push({href:a.getAttribute('href'), icon:(a.querySelector('.ic')||{}).textContent||'', label:a.textContent.replace(/\\s+/g,' ').trim(), group:gname});
    });
    document.querySelectorAll('.quick a[href], .kpi a[href]').forEach(function(a){
      out.push({href:a.getAttribute('href'), icon:'＋', label:a.textContent.replace(/\\s+/g,' ').trim(), group:'দ্রুত কাজ'});
    });
    var seen = {};
    return out.filter(function(x){ if (seen[x.href]) return false; seen[x.href]=1; return true; });
  }
  function render(){
    var q = input.value.trim().toLowerCase();
    var rows = items.filter(function(x){ return !q || (x.label + ' ' + x.group + ' ' + x.href).toLowerCase().indexOf(q) >= 0; });
    if (!rows.length) { list.innerHTML = '<li><a href="#">কিছু পাওয়া যায়নি</a></li>'; return; }
    cursor = Math.min(cursor, rows.length - 1);
    list.innerHTML = rows.map(function(x, i){
      return '<li><a class="' + (i === cursor ? 'on' : '') + '" href="' + x.href + '">' +
             '<span class="ic">' + x.icon + '</span><span>' + x.label + '</span>' +
             '<span class="p-group">' + (x.group || '') + '</span></a></li>';
    }).join('');
  }
  function open(){ items = load(); cursor = 0; input.value = ''; render(); box.classList.add('open'); input.focus(); }
  function close(){ box.classList.remove('open'); }
  function move(step){
    var links = list.querySelectorAll('a');
    if (!links.length) return;
    cursor = (cursor + step + links.length) % links.length;
    render();
    var on = list.querySelector('a.on'); if (on) on.scrollIntoView({block:'nearest'});
  }
  document.addEventListener('keydown', function(e){
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName||''));
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); box.classList.contains('open') ? close() : open(); return; }
    if (e.key === '/' && !typing && !box.classList.contains('open')) { e.preventDefault(); open(); return; }
    if (!box.classList.contains('open')) return;
    if (e.key === 'Escape') { close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter') { var on = list.querySelector('a.on') || list.querySelector('a'); if (on) { e.preventDefault(); on.click(); } }
  });
  input.addEventListener('input', function(){ cursor = 0; render(); });
  box.addEventListener('click', function(e){ if (e.target === box) close(); });
  var trigger = document.getElementById('palette-open');
  if (trigger) trigger.addEventListener('click', function(e){ e.preventDefault(); open(); });
  document.body.appendChild(box);
})();</script>"""


# ------------------------------------------------------------------ helpers
def crumbs(*parts: tuple) -> str:
    """`crumbs(("Home", "/admin"), ("Stores", ""))` → a breadcrumb line."""
    chunks = []
    for index, part in enumerate(parts):
        label, href = (part + ("",))[:2] if isinstance(part, (tuple, list)) else (str(part), "")
        if index:
            chunks.append('<span class="sep">›</span>')
        chunks.append(f'<a href="{escape(href)}">{escape(label)}</a>' if href
                      else f'<span>{escape(label)}</span>')
    return f'<nav class="crumbs">{"".join(chunks)}</nav>'


def page_head(title: str, subtitle: str = "", *, eyebrow: str = "",
              actions: str = "") -> str:
    """The heading block every admin page starts with (title, sub, actions)."""
    eyebrow_html = f'<p class="eyebrow">{escape(eyebrow)}</p>' if eyebrow else ""
    sub_html = f'<p class="sub">{escape(subtitle)}</p>' if subtitle else ""
    actions_html = f'<div class="actions">{actions}</div>' if actions else ""
    return (f'<div class="page-heading"><div>{eyebrow_html}'
            f'<h2>{escape(title)}</h2>{sub_html}</div>{actions_html}</div>')


def kpi(label: str, value, *, sub: str = "", dark: bool = False,
        href: str = "") -> str:
    """One number tile. `sub` is plain text (may contain <b>); `dark` inverts it."""
    inner = (f'<span class="k-label">{escape(label)}</span>'
             f'<span class="k-value">{value}</span>'
             + (f'<span class="k-sub">{sub}</span>' if sub else ""))
    css = "kpi dark" if dark else "kpi"
    if href:
        return f'<a class="{css}" style="text-decoration:none" href="{escape(href)}">{inner}</a>'
    return f'<div class="{css}">{inner}</div>'


def kpi_row(*tiles: str) -> str:
    return f'<div class="kpi-row">{"".join(tiles)}</div>'


def note(text: str, kind: str = "", *, title: str = "") -> str:
    """An inline message block: `kind` is "" | "ok" | "warn" | "bad"."""
    css = f"note {kind}".strip()
    heading = f'<b>{escape(title)}</b><br>' if title else ""
    return f'<div class="{css}">{heading}{text}</div>'


def empty(icon: str, title: str, text: str = "", actions: str = "") -> str:
    actions_html = f'<div class="e-actions">{actions}</div>' if actions else ""
    body = f"<b>{escape(title)}</b>{escape(text)}" if text else f"<b>{escape(title)}</b>"
    return (f'<div class="empty"><span class="e-icon">{icon}</span>{body}'
            f'{actions_html}</div>')


def tabs(items: list[tuple[str, str]], active: str = "") -> str:
    """Anchor tabs: `[("সব", "/admin/users"), ("প্রিমিয়াম", "?filter=premium")]`."""
    links = "".join(
        f'<a class="{"on" if href == active else ""}" href="{escape(href)}">{escape(label)}</a>'
        for label, href in items)
    return f'<nav class="tabs">{links}</nav>'


def table(headers: list[str], rows: list[list], *, empty_row: str = "কিছু নেই",
          numeric: tuple = ()) -> str:
    """A wrapped table with a sticky header. `numeric` = indexes aligned right."""
    head = "".join(
        f'<th class="num">{escape(str(h))}</th>' if index in numeric
        else f"<th>{escape(str(h))}</th>"
        for index, h in enumerate(headers))
    if rows:
        body = "".join(
            "<tr>" + "".join(
                f'<td class="num">{cell}</td>' if index in numeric
                else f"<td>{cell}</td>" for index, cell in enumerate(row)) + "</tr>"
            for row in rows)
    else:
        body = f'<tr><td colspan="{len(headers)}" class="muted">{escape(empty_row)}</td></tr>'
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


#: Campaign / job status → (label, class). One place, so every badge agrees.
STATUS_TAGS: dict[str, tuple] = {
    "draft": ("📝 খসড়া", "plain"), "queued": ("⏳ কিউতে", "warn"),
    "scheduled": ("🕒 শিডিউল", "warn"), "running": ("▶️ চলছে", "ok"),
    "done": ("✅ শেষ", "ok"), "sent": ("✅ গেছে", "ok"),
    "cancelled": ("⛔ বাতিল", "plain"), "failed": ("⚠️ ব্যর্থ", "bad"),
    "blocked": ("🚫 ব্লক", "bad"), "pending": ("⏳ অপেক্ষায়", "warn"),
    "paused": ("⏸ থামানো", "plain"),
    "ready": ("✅ প্রস্তুত", "ok"), "empty": ("— কিছু নেই", "plain"), "open": ("🟡 খোলা", "warn"),
    "closed": ("✅ বন্ধ", "ok"), "paid": ("💰 পরিশোধিত", "ok"),
    "expired": ("🕓 শেষ", "plain"),
}


def tag(text: str, kind: str = "") -> str:
    """A small status pill: `tag("done")` looks the label up automatically."""
    if text in STATUS_TAGS:
        label, auto = STATUS_TAGS[text]
        return f'<span class="pill {auto}">{label}</span>'
    return f'<span class="pill {kind}">{escape(str(text))}</span>'


def timeline(entries: list[tuple[str, str]]) -> str:
    """`[("আজ ১০:৩০", "ব্রডকাস্ট #১২ শেষ")]` — a compact activity list."""
    items = "".join(
        f'<li><div class="t-when">{escape(when)}</div><div>{body}</div></li>'
        for when, body in entries)
    body = items or '<li class="muted">কিছু নেই</li>'
    return f'<ul class="timeline">{body}</ul>'
