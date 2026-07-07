"""The self-contained browser page: inline CSS + JS around one embedded JSON payload.

`render_html(payload)` is a pure function from the `opentab.web.build_payload()`
shape to a single HTML string -- no template engine, no network, no external
assets, so the file works from disk, from GitHub Pages (`--demo --html`), or from
`--serve` unchanged. The page deliberately mirrors the TUI: a lazygit-style
sidebar (Months / Days, or Projects -- with the same eighth-block cost bars) next
to a tabbed detail pane whose tabs are the TUI's own per-scope tab tuples, TUI
box borders, and the TUI keymap (`j`/`k`, `Tab`, `h`/`l`, `Esc`, `$`, `p`/`t`).
Selection is hash-routed (deep-linkable, browser back = step out); the active
tab is transient UI state. The $ what-if toggle swaps which of the two embedded
cost fields every view reads -- the exact analogue of App._apply_price_mode().

Assembly uses token replacement (never str.format: the CSS/JS are full of braces).
__PAYLOAD__ is substituted last so user-controlled strings (session titles) can
never collide with the other tokens, and "</" is escaped in the JSON blob so a
title containing "</script>" cannot break out of the data block.
"""

from __future__ import annotations

import html
import json

from opentab import themes

_FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
    "%3Crect width='16' height='16' rx='3' fill='%23e0a458'/%3E"
    "%3Ctext x='8' y='12.5' font-size='11' text-anchor='middle' font-family='monospace' "
    "font-weight='bold' fill='%230b0c0f'%3E%24%3C/text%3E%3C/svg%3E"
)

_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>__TITLE__</title>
<link rel="icon" href="__FAVICON__">
<style>__CSS__</style>
</head>
<body>
<header id="hdr">
  <div class="brand"><a href="#/">OpenTab</a><span class="sub">browse your AI spend</span></div>
  <div id="hchips" class="chips"></div>
  <div id="hright"></div>
</header>
<div id="app">
  <aside id="side"></aside>
  <section id="main">
    <nav id="tabbar"></nav>
    <div id="crumbs"></div>
    <div id="view"></div>
  </section>
</div>
<footer id="foot"><span id="hints"></span><span id="stamp"></span></footer>
<div id="trends" hidden></div>
<div id="prices" hidden></div>
<div id="rangepick" hidden></div>
<div id="themepick" hidden></div>
<div id="tip" hidden></div>
<script type="application/json" id="opentab-data">__PAYLOAD__</script>
<script>__JS__</script>
</body>
</html>
"""

_CSS = r"""
/* Role tokens (not hues): a theme fills these slots. The values here are the
   default "opentab" theme so the page renders before the theme JS runs; applyTheme
   overrides them on :root at load. See the THEMES map in the script. */
:root{
  --bg:#0b0c0f; --bg-glow:#151823; --panel:#12141a; --panel2:#181b23;
  --line:#242836; --line2:#1b1e28; --axis:#383c48;
  --ink:#dcdad2; --ink2:#9b998f; --mut:#6a695f;
  --accent:#e0a458; --accent-bright:#ffc06e; --good:#62d391; --bad:#e07070;
  --scan:rgba(255,255,255,.014); --scrim:rgba(6,7,9,.72);
  --mono:ui-monospace,"SF Mono",Menlo,"Cascadia Code","JetBrains Mono",Consolas,"DejaVu Sans Mono",monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scrollbar-color:var(--line) var(--bg)}
body{
  font-family:var(--mono);font-size:13px;line-height:1.5;color:var(--ink);
  background:radial-gradient(1100px 480px at 50% -120px,var(--bg-glow) 0%,var(--bg) 62%) fixed var(--bg);
  max-width:1560px;margin:0 auto;padding:16px 24px 30px;
}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:99;
  background:repeating-linear-gradient(0deg,var(--scan) 0 1px,transparent 1px 3px)}
a{color:var(--accent);text-decoration:none}
a:hover{color:var(--accent-bright);text-decoration:underline}

/* header */
#hdr{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:4px 0 14px}
.brand{font-size:19px;font-weight:700;letter-spacing:.5px;white-space:nowrap}
.brand a{color:var(--ink)}
.brand a:hover{color:var(--accent-bright);text-decoration:none}
.brand .sub{color:var(--mut);font-size:12px;font-weight:400;margin-left:10px}
.chips{display:flex;gap:6px;flex-wrap:wrap;flex:1}
.chip{border:1px solid var(--line);border-radius:20px;padding:1px 10px;font-size:11px;color:var(--ink2);background:var(--panel)}
.chip b{color:var(--ink);font-weight:600}
.chip.demo{color:var(--accent);border-color:var(--accent)}
#hright{display:flex;align-items:center;gap:8px;margin-left:auto}
.badge{font-size:10px;letter-spacing:.12em;text-transform:uppercase;border:1px solid;border-radius:3px;padding:2px 8px}
.badge.est{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 55%,transparent);background:color-mix(in srgb,var(--accent) 9%,transparent)}
.badge.sub{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 50%,transparent);background:color-mix(in srgb,var(--bad) 8%,transparent)}
.seg{display:flex;border:1px solid var(--line);border-radius:4px;overflow:hidden}
.seg button{font:inherit;font-size:11px;padding:3px 10px;border:0;background:var(--panel);color:var(--ink2);cursor:pointer}
.seg button.on{background:var(--accent);color:#141009;font-weight:700}
.seg button:not(.on):hover{color:var(--ink)}
.hbtn{font:inherit;font-size:11px;padding:3px 10px;border:1px solid var(--line);border-radius:4px;background:var(--panel);color:var(--ink2);cursor:pointer}
.hbtn:hover{color:var(--accent);border-color:var(--accent)}

/* app layout: lazygit-style sidebar + detail pane */
#app{display:grid;grid-template-columns:302px minmax(0,1fr);gap:16px;align-items:start}
#side{position:sticky;top:12px;max-height:calc(100vh - 24px);overflow-y:auto;
  scrollbar-width:thin;padding:2px 2px 2px 0}
#main{min-width:0}
@media (max-width:900px){
  #app{grid-template-columns:1fr}
  #side{position:static;max-height:none;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:0 12px}
}

/* TUI box: border with the title sitting in the border line */
.pane{position:relative;border:1px solid var(--line);border-radius:6px;background:var(--panel);
  padding:14px 14px 10px;margin:10px 0 16px}
.pane>h3{position:absolute;top:-9px;left:10px;background:var(--bg);padding:0 7px;
  font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:var(--ink2);font-weight:600}
.pane>h3::before{content:"\258d";color:var(--accent);margin-right:6px}
.pane.focus{border-color:color-mix(in srgb,var(--accent) 60%,transparent)}
.pane.focus>h3{color:var(--accent)}
.hint{color:var(--mut);font-size:12px}

/* sidebar lists */
.rows{margin:0 -6px}
.row{display:flex;align-items:baseline;gap:8px;padding:2px 8px;border-radius:3px;cursor:pointer;
  font-size:12.5px;white-space:nowrap}
.row:hover{background:var(--panel2)}
.row.sel{background:color-mix(in srgb,var(--accent) 14%,transparent);box-shadow:inset 2px 0 var(--accent)}
.row .lab{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}
.row.sel .lab{color:var(--accent-bright)}
.row .n{color:var(--mut);font-size:11px}
.row .cost{color:var(--good)}
.row .cost.zero{color:var(--mut)}
.row .tb{color:var(--accent);opacity:.9;white-space:pre;font-size:11px}
.mode{display:flex;gap:2px;margin:0 0 12px}
.mode button{flex:1;font:inherit;font-size:11px;padding:3px 0;border:1px solid var(--line);
  background:var(--panel);color:var(--ink2);cursor:pointer}
.mode button:first-child{border-radius:4px 0 0 4px}
.mode button:last-child{border-radius:0 4px 4px 0}
.mode button.on{border-color:var(--accent);color:var(--accent)}

/* detail tab bar -- the TUI's Overview │ Models │ Projects │ Sessions */
#tabbar{display:flex;gap:2px;flex-wrap:wrap;align-items:center;border:1px solid var(--line);
  border-radius:6px;background:var(--panel);padding:4px;margin:10px 0 12px}
#tabbar button{font:inherit;font-size:12px;padding:4px 14px;border:0;border-radius:4px;
  background:none;color:var(--ink2);cursor:pointer}
#tabbar button.on{background:var(--accent);color:#141009;font-weight:700}
#tabbar button:not(.on):hover{color:var(--ink)}
#tabbar .note{margin-left:auto;color:var(--mut);font-size:11px;padding:0 8px}

/* breadcrumbs / footer */
#crumbs{padding:0 2px 10px;color:var(--mut);min-height:16px;font-size:12px}
#crumbs .sep{margin:0 7px;color:var(--line)}
#crumbs .here{color:var(--ink)}
#foot{display:flex;gap:14px;flex-wrap:wrap;justify-content:space-between;margin-top:18px;
  padding-top:12px;border-top:1px solid var(--line2);color:var(--mut);font-size:11px}
#foot kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:3px;padding:0 5px;
  font-family:inherit;font-size:10.5px;color:var(--ink2);background:var(--panel);margin-right:2px}

/* side-by-side panel pairs inside the detail pane */
.cols{display:grid;grid-template-columns:1fr;gap:0 14px;align-items:start}
@media (min-width:1250px){.cols{grid-template-columns:1fr 1fr}}
.cols .pane{min-width:0}
button.showall{display:block;width:100%;font:inherit;font-size:11px;margin-top:6px;
  padding:5px;border:1px dashed var(--line);border-radius:4px;background:none;
  color:var(--mut);cursor:pointer}
button.showall:hover{color:var(--accent);border-color:var(--accent)}

/* stat tiles */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:10px 0 16px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 14px}
.tile .k{font-size:10px;text-transform:uppercase;letter-spacing:.14em;color:var(--mut)}
.tile .v{font-size:22px;font-weight:700;margin-top:2px;color:var(--ink)}
.tile .v.money{color:var(--good)}
.tile .n{font-size:11px;color:var(--mut)}

/* tables */
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--mut);font-weight:600;
  text-align:left;padding:4px 10px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--ink2)}
th.sorted{color:var(--accent)}
td{padding:4.5px 10px;border-bottom:1px solid var(--line2);white-space:nowrap;vertical-align:baseline}
tr:last-child td{border-bottom:0}
th.r,td.r{text-align:right}
td.grow{white-space:normal;overflow-wrap:anywhere;min-width:160px}
tbody tr.rowlink{cursor:pointer}
tbody tr.rowlink:hover{background:var(--panel2)}
tbody tr.rowlink:hover td:first-child{box-shadow:inset 2px 0 var(--accent)}
.m{color:var(--good)}
.m-zero{color:var(--bad)}
.mut{color:var(--mut)}
.dim{color:var(--ink2)}
.bar{display:inline-block;width:86px;height:7px;border-radius:2px;background:var(--line2);
  vertical-align:baseline;margin-left:8px;position:relative;overflow:hidden}
.bar i{position:absolute;inset:0;right:auto;width:var(--w);background:var(--accent);border-radius:2px}
input.filter{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);
  border-radius:4px;padding:4px 10px;width:260px;max-width:100%;margin-bottom:10px}
input.filter:focus{outline:none;border-color:var(--accent)}
.ychips{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.ychips button{font:inherit;font-size:11px;padding:2px 10px;border:1px solid var(--line);
  border-radius:4px;background:var(--panel);color:var(--ink2);cursor:pointer}
.ychips button.on{border-color:var(--accent);color:var(--accent)}

/* charts */
.chart{width:100%;height:auto;display:block}
.chart text{font-family:var(--mono)}
.bargroup{cursor:pointer}
.bargroup rect.hit{fill:transparent}
.bargroup:hover path{fill:var(--accent-bright)}
.cal-wrap{overflow-x:auto}
.cal-legend{display:flex;align-items:center;gap:4px;color:var(--mut);font-size:11px;margin-top:8px}
.cal-legend span{width:11px;height:11px;border-radius:2px;display:inline-block}

/* turns */
tr.prompt-row td{color:var(--accent);padding-top:9px;font-weight:600}
tr.prompt-row td:first-child{white-space:normal;overflow-wrap:anywhere}
td.indent{color:var(--ink2)}

/* tooltip */
#tip{position:fixed;z-index:100;pointer-events:none;background:var(--panel2);border:1px solid var(--line);
  border-radius:4px;padding:5px 10px;font-size:11.5px;color:var(--ink);white-space:pre-line;
  box-shadow:0 4px 16px rgba(0,0,0,.5);max-width:320px}

/* Trends overlay (T) -- the TUI's full-screen Trends, as a modal */
#trends{position:fixed;inset:0;z-index:200;background:var(--scrim);
  display:flex;align-items:flex-start;justify-content:center;padding:26px 20px;overflow-y:auto}
#trends[hidden]{display:none}
.tr-panel{position:relative;width:100%;max-width:1180px;background:var(--panel);
  border:1px solid var(--line);border-radius:8px;padding:16px 18px 20px;box-shadow:0 10px 40px rgba(0,0,0,.6);
  animation:rise .18s ease both}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.tr-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.tr-head h3{font-size:12px;text-transform:uppercase;letter-spacing:.14em;color:var(--ink)}
.tr-head h3::before{content:"\258d";color:var(--accent);margin-right:7px}
.tr-tabs{display:flex;gap:2px;flex-wrap:wrap;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px}
.tr-tabs button{font:inherit;font-size:12px;padding:3px 12px;border:0;border-radius:4px;background:none;color:var(--ink2);cursor:pointer}
.tr-tabs button.on{background:var(--accent);color:#141009;font-weight:700}
.tr-tabs button:not(.on):hover{color:var(--ink)}
.tr-close{margin-left:auto;font:inherit;font-size:12px;padding:3px 11px;border:1px solid var(--line);border-radius:4px;
  background:var(--bg);color:var(--ink2);cursor:pointer}
.tr-close:hover{color:var(--accent);border-color:var(--accent)}
.tr-nav{display:flex;align-items:center;gap:10px;margin-bottom:8px;color:var(--ink2);font-size:12.5px}
.tr-nav button{font:inherit;font-size:13px;line-height:1;padding:2px 9px;border:1px solid var(--line);border-radius:4px;
  background:var(--panel2);color:var(--ink2);cursor:pointer}
.tr-nav button:hover:not(:disabled){color:var(--accent);border-color:var(--accent)}
.tr-nav button:disabled{opacity:.35;cursor:default}
.tr-nav .lbl{color:var(--ink);font-weight:600}
.tr-nav .pos{color:var(--mut);font-size:11px}
.tr-chart{width:100%;height:auto;display:block}
.tr-chart text{font-family:var(--mono)}
.tr-chart .bg{cursor:pointer}
.tr-chart .bg rect.hit{fill:transparent}
.tr-chart .bg:hover path{fill:var(--accent-bright)}
.tr-summary{display:flex;gap:22px;flex-wrap:wrap;color:var(--ink2);font-size:12px;margin-top:6px}
.tr-summary b{color:var(--ink)}
.tr-note{color:var(--mut);font-size:11px;margin-top:4px}
/* ranked horizontal bars (Models / Providers / Sources) */
.rank{width:100%;font-size:12.5px;border-collapse:collapse}
.rank th{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--mut);font-weight:600;text-align:right;padding:3px 8px}
.rank th.l{text-align:left}
.rank td{padding:4px 8px;white-space:nowrap;text-align:right;border-bottom:1px solid var(--line2)}
.rank td.l{text-align:left;white-space:normal;overflow-wrap:anywhere}
.rank tr:last-child td{border-bottom:0}
.rank .hb{position:relative;width:100%;min-width:120px;height:14px;background:var(--line2);border-radius:2px;overflow:hidden}
.rank .hb i{position:absolute;inset:0;right:auto;width:var(--w);background:var(--accent);border-radius:2px}
.rank td.bar{width:38%}

/* Prices overlay (P) -- the models.dev list-price reference behind $ */
#prices,#rangepick{position:fixed;inset:0;z-index:200;background:var(--scrim);
  display:flex;align-items:flex-start;justify-content:center;padding:26px 20px;overflow-y:auto}
#prices[hidden],#rangepick[hidden]{display:none}
.pr-intro{color:var(--ink2);font-size:12px;margin:2px 0 12px;line-height:1.5}
.pr-views{display:flex;gap:2px;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px}
.pr-views button{font:inherit;font-size:12px;padding:3px 12px;border:0;border-radius:4px;background:none;color:var(--ink2);cursor:pointer}
.pr-views button.on{background:var(--accent);color:#141009;font-weight:700}
.pr-views button:not(.on):hover{color:var(--ink)}
table.prices{width:100%;border-collapse:collapse;font-size:12.5px}
table.prices th{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);font-weight:600;
  text-align:right;padding:4px 9px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap}
table.prices th.l{text-align:left}
table.prices th:hover{color:var(--ink2)}
table.prices th.sorted{color:var(--accent)}
table.prices td{padding:4px 9px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line2);font-variant-numeric:tabular-nums}
table.prices td.l{text-align:left;white-space:normal;overflow-wrap:anywhere}
table.prices tr:last-child td{border-bottom:0}
table.prices tr.grp td{color:var(--accent);font-weight:600;padding-top:9px;border-bottom:0}
table.prices .tag{color:var(--mut);font-size:11px;margin-left:7px}
.pr-use{display:inline-flex;align-items:center;gap:6px;justify-content:flex-end}
.pr-use .hb{position:relative;width:64px;height:8px;background:var(--line2);border-radius:2px;overflow:hidden;display:inline-block}
.pr-use .hb i{position:absolute;inset:0;right:auto;width:var(--w);background:var(--accent);border-radius:2px}
/* range picker */
.rp-panel{max-width:520px}
.rp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px;margin:6px 0 14px}
.rp-grid button{font:inherit;font-size:12px;padding:7px 6px;border:1px solid var(--line);border-radius:5px;background:var(--panel2);color:var(--ink);cursor:pointer}
.rp-grid button.on{border-color:var(--accent);color:var(--accent)}
.rp-grid button:hover{border-color:var(--accent)}
.rp-custom{display:flex;align-items:center;gap:8px;flex-wrap:wrap;border-top:1px solid var(--line2);padding-top:12px;color:var(--ink2);font-size:12px}
.rp-custom input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:4px 8px}
.rp-custom input:focus{outline:none;border-color:var(--accent)}
.rp-custom button{font:inherit;font-size:12px;padding:4px 12px;border:1px solid var(--accent);border-radius:4px;background:var(--accent);color:#141009;font-weight:700;cursor:pointer}
.chip.click{cursor:pointer}
.chip.click:hover{border-color:var(--accent);color:var(--ink)}
/* theme picker */
#themepick{position:fixed;inset:0;z-index:200;background:var(--scrim);display:flex;align-items:flex-start;justify-content:center;padding:26px 20px;overflow-y:auto}
#themepick[hidden]{display:none}
.th-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:4px}
@media (max-width:520px){.th-grid{grid-template-columns:1fr}}
.th-row{display:flex;align-items:center;gap:10px;font:inherit;font-size:12.5px;padding:8px 11px;border:1px solid var(--line);border-radius:6px;background:var(--panel2);color:var(--ink);cursor:pointer;text-align:left}
.th-row:hover{border-color:var(--accent)}
.th-row.on{border-color:var(--accent);color:var(--accent)}
.th-sw{display:inline-flex;gap:3px;flex:none}
.th-sw i{width:13px;height:13px;border-radius:3px;display:inline-block;box-shadow:inset 0 0 0 1px rgba(128,128,128,.25)}
.th-name{flex:1;overflow:hidden;text-overflow:ellipsis}
.th-mode{color:var(--mut);font-size:9.5px;text-transform:uppercase;letter-spacing:.1em;flex:none}

/* session meta */
.meta{display:grid;grid-template-columns:auto 1fr;gap:2px 16px;font-size:12px;margin-bottom:2px}
.meta dt{color:var(--mut);text-transform:uppercase;font-size:10px;letter-spacing:.1em;padding-top:2px}
.meta dd{color:var(--ink2);overflow-wrap:anywhere}
h2.title{font-size:15px;margin:2px 0 10px;overflow-wrap:anywhere}
"""

_JS = r"""
'use strict';
const DATA = JSON.parse(document.getElementById('opentab-data').textContent);
const META = DATA.meta;
const ALL_W = DATA.workflows;      // every embedded session
let W = ALL_W;                     // the active, range-filtered set (R rescopes it)
let RANGE = { kind: 'all', label: META.range };  // client-side date scope
let MODE = META.startApi ? 'api' : 'real';

/* ---------- themes ---------- */
// The palettes are the single source of truth in opentab/themes.py, injected here
// as JSON (so the web browser and the curses TUI never drift). Each entry: `css`
// fills the :root role slots (applyTheme writes them live, so all HTML re-themes
// via CSS vars), `heat`/`priceHeat` are the ramps the SVG charts read through TH,
// and `dark` drives the scanline/scrim/color-scheme.
const THEMES = __THEMES__;
let TH = THEMES.opentab;             // the active theme object (charts read it)
const thc = k => TH.css[k];          // theme color for an SVG chart slot
const CUR = { theme: 'opentab' };
function applyTheme(id) {
  const t = THEMES[id] ? id : 'opentab';
  TH = THEMES[t]; CUR.theme = t;
  const r = document.documentElement, st = r.style;
  for (const k in TH.css) st.setProperty('--' + k, TH.css[k]);
  st.setProperty('--scan', TH.dark ? 'rgba(255,255,255,.014)' : 'rgba(0,0,0,.022)');
  st.setProperty('--scrim', TH.dark ? 'rgba(6,7,9,.72)' : 'rgba(60,62,74,.4)');
  st.colorScheme = TH.dark ? 'dark' : 'light';
  r.setAttribute('data-theme', t);
  try { localStorage.setItem('opentab-theme', t); } catch (e) { /* file:// may block storage */ }
}
let THEMEPICK = false;
function openTheme() { THEMEPICK = true; renderTheme(); }
function closeTheme() { THEMEPICK = false; renderTheme(); }
function renderTheme() {
  const host = document.getElementById('themepick');
  if (!THEMEPICK) { host.hidden = true; host.textContent = ''; return; }
  host.hidden = false; host.textContent = '';
  const rows = Object.entries(THEMES).map(([id, t]) => h('button', {
    class: 'th-row' + (id === CUR.theme ? ' on' : ''), onclick: () => { applyTheme(id); render(false); },
  }, h('span', { class: 'th-sw' }, ['bg', 'panel', 'accent', 'good', 'bad'].map(k => h('i', { style: 'background:' + t.css[k] }))),
    h('span', { class: 'th-name' }, t.name), h('span', { class: 'th-mode' }, t.dark ? 'dark' : 'light')));
  const panel = h('div', { class: 'tr-panel rp-panel' },
    h('div', { class: 'tr-head' }, h('h3', null, 'Theme'), h('button', { class: 'tr-close', style: 'margin-left:auto', onclick: closeTheme }, 'esc ✕')),
    h('div', { class: 'th-grid' }, rows));
  panel.addEventListener('click', e => e.stopPropagation());
  host.appendChild(panel);
}

let TAB = 'Overview';       // active detail tab (transient, resets on scope change)
let BROWSE = 'time';        // sidebar mode: 'time' (Months/Days) | 'projects', like the TUI
let FOCUS = 'months';       // which sidebar list j/k drives
let FILTER = '';
const SORT = {};
const EXPANDED = new Set(); // table ids whose "show all" is open (reset per view)
const VIEW = { calYear: null };
let EXTRAS = { id: null, loading: false, turns: [], tools: [] }; // per-session Turns/Tools (serve)
// The Trends overlay (T) -- mirrors the TUI's 7-tab Trends over the whole range.
const TREND_TABS = ['Daily', 'Weekly', 'Monthly', 'Calendar', 'Models', 'Providers', 'Sources'];
let TRENDS = { open: false, tab: 'Daily', monthIdx: 0, weekIdx: 0, yearIdx: 0 };
// The P prices overlay: the models.dev list-price reference behind $ (app-wide,
// never range-scoped -- like the TUI). eff sorts cheapest-first; others high→low.
const PRICE_VIEWS = [['flat', 'flat list'], ['family', 'by vendor'], ['provider', 'by provider']];
let PRICES = { open: false, view: 'flat', sort: 'eff', desc: false };

/* ---------- formatting (mirrors opentab.formatting) ---------- */
const money = v => (v > 0 && v < 0.005) ? '<$0.01'
  : '$' + v.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const moneyLabel = v => v <= 0 ? '' : v < 0.005 ? '<$.01' : v < 10 ? '$' + v.toFixed(2)
  : v < 1000 ? '$' + Math.round(v) : v < 10000 ? '$' + (v / 1000).toFixed(1) + 'k'
  : '$' + Math.round(v / 1000) + 'k';
const hTok = v => v >= 1e9 ? (v / 1e9).toFixed(1) + 'B' : v >= 1e6 ? (v / 1e6).toFixed(1) + 'M'
  : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : String(v);
const pct = (p, w) => w <= 0 ? '-' : (p > 0 && 100 * p / w < 1) ? '<1%' : Math.round(100 * p / w) + '%';
const cost = w => MODE === 'api' ? w.api : w.real;
const rootCost = w => MODE === 'api' ? w.apiRoot : w.realRoot;
const mCost = r => MODE === 'api' ? r.api : r.real;
const shortPath = p => META.home && p.startsWith(META.home) ? '~' + p.slice(META.home.length) : p;
const projName = p => { const parts = shortPath(p).split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : (p || '(no project)'); };
const MN = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const monthLabel = m => MN[+m.slice(5, 7) - 1] + ' ’' + m.slice(2, 4);
const dt = s => (s || '').slice(0, 16).replace('T', ' ');
// The Monday ('YYYY-MM-DD') of the ISO week a date falls in -- matches heatmap.week_key
// so the Weekly trend buckets the same way the TUI does; '' for an undated row.
function weekMonday(dateStr) {
  const iso = (dateStr || '').slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) return '';
  const d = new Date(iso + 'T00:00:00');
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); // back to Monday (Mon=0)
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
function addDays(iso, n) {
  const d = new Date(iso + 'T00:00:00'); d.setDate(d.getDate() + n);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
function daysInMonth(m) { return new Date(+m.slice(0, 4), +m.slice(5, 7), 0).getDate(); }
/* the TUI's eighth-block cost bar (formatting.cost_bar), verbatim in the sidebar */
const EIGHTHS = ' ▏▎▍▌▋▊▉';
function tbar(v, peak, cells = 7) {
  if (peak <= 0 || v <= 0) return ' '.repeat(cells);
  const e = Math.max(1, Math.min(Math.round((v / peak) * cells * 8), cells * 8));
  const full = Math.floor(e / 8), rem = e % 8;
  if (full >= cells) return '█'.repeat(cells);
  return ('█'.repeat(full) + EIGHTHS[rem]).padEnd(cells);
}

/* ---------- DOM helpers (children become text nodes: XSS-safe by default) ---------- */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === 'class') el.className = v;
    else if (k === 'onclick') el.addEventListener('click', v);
    else if (k === 'oninput') el.addEventListener('input', v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(9)) if (kid !== null && kid !== undefined)
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
const SVGNS = 'http://www.w3.org/2000/svg';
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === 'onclick') { el.addEventListener('click', v); }
    else if (k === 'tip') bindTip(el, v);
    else if (k === 'text') el.textContent = v;
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(9)) if (kid) el.appendChild(kid);
  return el;
}

/* ---------- tooltip ---------- */
const TIP = document.getElementById('tip');
function bindTip(el, text) {
  el.addEventListener('mouseenter', () => { TIP.textContent = typeof text === 'function' ? text() : text; TIP.hidden = false; });
  el.addEventListener('mousemove', e => {
    const r = TIP.getBoundingClientRect();
    TIP.style.left = Math.min(e.clientX + 14, innerWidth - r.width - 8) + 'px';
    TIP.style.top = Math.min(e.clientY + 14, innerHeight - r.height - 8) + 'px';
  });
  el.addEventListener('mouseleave', () => { TIP.hidden = true; });
}

/* ---------- aggregation ---------- */
const sum = (rows, f) => rows.reduce((a, r) => a + f(r), 0);
function groupBy(rows, keyFn) {
  const m = new Map();
  for (const r of rows) { const k = keyFn(r); if (!m.has(k)) m.set(k, []); m.get(k).push(r); }
  return m;
}
function scopeStats(ws) {
  return { cost: sum(ws, cost), sessions: ws.length, tokens: sum(ws, w => w.tokens),
    days: new Set(ws.map(w => w.date.slice(0, 10)).filter(d => /^\d/.test(d))).size,
    subagents: sum(ws, w => w.subagents) };
}
/* A rare session may carry no timestamp: it stays in totals and tables but is
   left out of time-keyed groupings (same slice-based keys the TUI groups by). */
function monthRows(ws) {
  return [...groupBy(ws, w => w.date.slice(0, 7))].filter(([m]) => /^\d{4}-\d{2}$/.test(m))
    .map(([month, g]) =>
      ({ month, cost: sum(g, cost), sessions: g.length, tokens: sum(g, w => w.tokens) }))
    .sort((a, b) => a.month < b.month ? -1 : 1);
}
function dayRows(ws) {
  return [...groupBy(ws, w => w.date.slice(0, 10))].filter(([d]) => /^\d{4}-\d{2}-\d{2}$/.test(d))
    .map(([day, g]) =>
      ({ day, cost: sum(g, cost), sessions: g.length, tokens: sum(g, w => w.tokens) }));
}
function projectRows(ws) {
  return [...groupBy(ws, w => w.project)].map(([project, g]) =>
    ({ project, cost: sum(g, cost), sessions: g.length, tokens: sum(g, w => w.tokens),
       last: g.reduce((a, w) => w.date > a ? w.date : a, '') }));
}
function sourceRows(ws) {
  return [...groupBy(ws, w => w.source || META.source)].map(([source, g]) =>
    ({ source, cost: sum(g, cost), sessions: g.length, tokens: sum(g, w => w.tokens) }));
}
function modelAgg(ws) {
  const m = new Map();
  for (const w of ws) {
    const rows = DATA.models[w.id]; if (!rows) continue;
    for (const r of rows) {
      let a = m.get(r.model);
      if (!a) { a = { model: r.model, runs: 0, real: 0, api: 0, tokens: 0, cacheRead: 0, cacheWrite: 0, output: 0 }; m.set(r.model, a); }
      a.runs += r.runs; a.real += r.real; a.api += r.api; a.tokens += r.tokens;
      a.cacheRead += r.cacheRead; a.cacheWrite += r.cacheWrite; a.output += r.output;
    }
  }
  return [...m.values()];
}

/* ---------- cells ---------- */
const moneyCell = v => h('span', { class: v === 0 ? 'm-zero' : 'm' }, money(v));
/* provider prefix dimmed, with a clean break opportunity at the "/" so a long id
   wraps between route and model instead of mid-token */
function modelCell(model) {
  const i = model.lastIndexOf('/');
  if (i < 0) return model;
  return [h('span', { class: 'mut' }, model.slice(0, i + 1)), h('wbr'), model.slice(i + 1)];
}
function barCell(v, peak) {
  const w = peak > 0 && v > 0 ? Math.max(2, Math.round(100 * v / peak)) : 0;
  return [moneyCell(v), h('span', { class: 'bar' }, h('i', { style: '--w:' + w + '%' }))];
}

/* ---------- sortable table ---------- */
/* opts.collapse: show only the top N rows (post-sort) with a "show all" toggle,
   so a tab stays scannable instead of a 900-row dump. */
function table(id, cols, rows, opts = {}) {
  const st = SORT[id] || opts.defaultSort;
  let sorted = rows.slice();
  if (st) {
    const c = cols.find(c => c.key === st.key);
    if (c) {
      const sv = c.sortVal || (r => r[c.key]);
      sorted.sort((a, b) => { const x = sv(a), y = sv(b); return (x < y ? -1 : x > y ? 1 : 0) * (st.desc ? -1 : 1); });
    }
  }
  const collapse = opts.collapse || Infinity;
  const open = EXPANDED.has(id);
  const shown = open ? sorted : sorted.slice(0, collapse);
  const head = h('tr', null, cols.map(c => h('th', {
    class: (c.align === 'r' ? 'r' : '') + (st && st.key === c.key ? ' sorted' : ''),
    onclick: () => {
      const cur = SORT[id];
      SORT[id] = { key: c.key, desc: cur && cur.key === c.key ? !cur.desc : c.asc !== true };
      render(false);
    },
  }, c.label, st && st.key === c.key ? (st.desc ? ' ▾' : ' ▴') : '')));
  const body = shown.map(r => h('tr',
    opts.onRow ? { class: 'rowlink', onclick: () => opts.onRow(r) } : null,
    cols.map(c => h('td', { class: [c.align === 'r' ? 'r' : '', c.cls || ''].join(' ').trim() || null },
      c.fmt ? c.fmt(r) : String(r[c.key] ?? '')))));
  const toggle = sorted.length > collapse
    ? h('button', { class: 'showall', onclick: () => { open ? EXPANDED.delete(id) : EXPANDED.add(id); render(false); } },
        open ? '▴ show top ' + collapse : '▾ show all ' + sorted.length)
    : null;
  return h('div', null,
    h('div', { class: 'scroll' }, h('table', null, h('thead', null, head), h('tbody', null, body))),
    toggle);
}

/* ---------- charts ---------- */
function roundTop(x, y, w, hgt, r) {
  r = Math.max(0, Math.min(r, w / 2, hgt));
  return 'M' + x + ',' + (y + hgt) + 'v' + -(hgt - r) + 'q0,' + -r + ' ' + r + ',' + -r
    + 'h' + (w - 2 * r) + 'q' + r + ',0 ' + r + ',' + r + 'v' + (hgt - r) + 'z';
}
function barChart(rows) {
  const VW = 1000, VH = 190, padT = 22, padB = 22, padX = 6;
  const peak = Math.max(...rows.map(r => r.cost), 1e-9);
  const n = rows.length;
  const gap = 2;
  const bw = Math.max(3, Math.min(46, (VW - 2 * padX) / n - gap));
  const step = bw + gap;
  // A value on top of every bar when they're wide enough to fit the label without
  // colliding; when too narrow (many months) fall back to labelling just the tallest.
  const valueEach = step >= 34;
  const x0 = (VW - (n * step - gap)) / 2;
  const plotH = VH - padT - padB;
  const svg = s('svg', { viewBox: '0 0 ' + VW + ' ' + VH, class: 'chart', role: 'img',
    'aria-label': 'spend by month' });
  for (const f of [0.5, 1]) {
    const y = padT + (1 - f) * plotH;
    svg.appendChild(s('line', { x1: x0, y1: y, x2: VW - x0, y2: y, stroke: thc('line'), 'stroke-width': 1 }));
    // The midline gets an axis label only when the bars aren't individually labelled;
    // with per-bar values it's redundant and collides with the rightmost bar's label.
    if (f !== 1 && !valueEach) svg.appendChild(s('text', { x: VW - x0, y: y - 4, 'text-anchor': 'end', 'font-size': 10, fill: thc('mut'), text: moneyLabel(peak * f) }));
  }
  svg.appendChild(s('line', { x1: x0, y1: VH - padB, x2: VW - x0, y2: VH - padB, stroke: thc('axis'), 'stroke-width': 1 }));
  const peakIdx = rows.findIndex(r => r.cost === Math.max(...rows.map(q => q.cost)));
  const labelEvery = Math.max(1, Math.ceil(n / 14));
  rows.forEach((r, i) => {
    const x = x0 + i * step;
    const hgt = Math.max(r.cost > 0 ? 2 : 0, plotH * r.cost / peak);
    const y = VH - padB - hgt;
    const g = s('g', { class: 'bargroup', tip: () => monthLabel(r.month) + '\n' + money(r.cost) + ' · ' + r.sessions + ' session' + (r.sessions === 1 ? '' : 's'),
      onclick: () => { go('m', r.month); } });
    g.appendChild(s('rect', { class: 'hit', x, y: padT, width: step, height: VH - padT - padB }));
    if (hgt > 0) g.appendChild(s('path', { d: roundTop(x, y, bw, hgt, 3), fill: thc('accent') }));
    if (r.cost > 0 && (valueEach || i === peakIdx))
      g.appendChild(s('text', { x: x + bw / 2, y: y - 5, 'text-anchor': 'middle', 'font-size': 10, fill: thc('ink2'), text: moneyLabel(r.cost) }));
    if (i % labelEvery === 0)
      g.appendChild(s('text', { x: x + bw / 2, y: VH - 7, 'text-anchor': 'middle', 'font-size': 9.5, fill: thc('mut'), text: monthLabel(r.month) }));
    svg.appendChild(g);
  });
  return svg;
}

function heatLevel(v, thresholds) {
  if (v <= 0) return 0;
  let lvl = 1;
  for (const t of thresholds) if (v >= t) lvl++;
  return Math.min(lvl, TH.heat.length - 1);
}
function calendar(year, byDate, onDay) {
  onDay = onDay || (date => go('d', date));
  const CELL = 11, GAP = 2, STEP = CELL + GAP, padL = 30, padT = 16;
  const first = new Date(+year, 0, 1);
  const today = new Date();
  const last = +year === today.getFullYear() ? today : new Date(+year, 11, 31);
  const start = new Date(first);
  start.setDate(start.getDate() - ((first.getDay() + 6) % 7)); // back to Monday
  const vals = [...byDate.values()].map(d => d.cost).filter(v => v > 0).sort((a, b) => a - b);
  const q = f => vals.length ? vals[Math.min(vals.length - 1, Math.floor(f * vals.length))] : 0;
  const thresholds = [q(0.25), q(0.5), q(0.75), q(0.93)];
  const weeks = Math.ceil(((last - start) / 86400000 + 1) / 7);
  const VW = padL + weeks * STEP, VH = padT + 7 * STEP + 2;
  const svg = s('svg', { class: 'cal', width: VW, height: VH, viewBox: '0 0 ' + VW + ' ' + VH, role: 'img', 'aria-label': 'daily spend calendar ' + year });
  [['Mon', 0], ['Wed', 2], ['Fri', 4]].forEach(([lbl, row]) =>
    svg.appendChild(s('text', { x: padL - 6, y: padT + row * STEP + CELL - 2, 'text-anchor': 'end', 'font-size': 9, fill: thc('mut'), text: lbl })));
  let col = 0, lastMonth = -1;
  for (let d = new Date(start); d <= last; d.setDate(d.getDate() + 1)) {
    const row = (d.getDay() + 6) % 7;
    if (row === 0 && d > start) col++;
    if (d < first) continue;
    const date = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    if (d.getMonth() !== lastMonth) {
      lastMonth = d.getMonth();
      svg.appendChild(s('text', { x: padL + col * STEP, y: padT - 6, 'font-size': 9, fill: thc('mut'), text: MN[lastMonth] }));
    }
    const info = byDate.get(date);
    const v = info ? info.cost : 0;
    const attrs = { x: padL + col * STEP, y: padT + row * STEP, width: CELL, height: CELL, rx: 2,
      fill: TH.heat[heatLevel(v, thresholds)],
      tip: () => date + '\n' + (info ? money(v) + ' · ' + info.sessions + ' session' + (info.sessions === 1 ? '' : 's') : 'no usage') };
    if (info) { attrs.onclick = () => { onDay(date); }; attrs.style = 'cursor:pointer'; }
    svg.appendChild(s('rect', attrs));
  }
  const legend = h('div', { class: 'cal-legend' }, 'less',
    TH.heat.map(c => h('span', { style: 'background:' + c })), 'more');
  return h('div', null, h('div', { class: 'cal-wrap' }, svg), legend);
}

/* ---------- routing: #/ · #/y/2026 · #/m/2026-06 · #/d/2026-06-15 · #/p/<enc> · #/s/<id> ---------- */
function go(kind, arg) {
  location.hash = kind ? '#/' + kind + '/' + encodeURIComponent(arg) : '#/';
}
function curScope() {
  // Firefox returns location.hash pre-decoded, so treat everything after the
  // kind segment as the argument instead of splitting on every slash.
  const raw = location.hash.replace(/^#\/?/, '');
  const slash = raw.indexOf('/');
  const kind = slash < 0 ? raw : raw.slice(0, slash);
  let arg = slash < 0 ? '' : raw.slice(slash + 1);
  try { arg = decodeURIComponent(arg); } catch (e) { /* leave undecodable args as-is */ }
  if (kind === 'y' && arg) return { kind: 'y', year: arg };
  if (kind === 'm' && arg) return { kind: 'm', month: arg, year: arg.slice(0, 4) };
  if (kind === 'd' && arg) return { kind: 'd', day: arg, month: arg.slice(0, 7), year: arg.slice(0, 4) };
  if (kind === 'p' && arg) return { kind: 'p', project: arg };
  if (kind === 's' && arg) {
    const w = ALL_W.find(x => x.id === arg);  // any session, even outside the active range
    return { kind: 's', id: arg, session: w, month: w ? w.date.slice(0, 7) : null,
      day: w ? w.date.slice(0, 10) : null, year: w ? w.date.slice(0, 4) : null };
  }
  return { kind: 'all' };
}

/* ---------- range scoping (R): filter the active set client-side ---------- */
function isoToday() { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
function isoDaysAgo(n) { const d = new Date(); d.setDate(d.getDate() - n); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
function isoMonthsAgo(n) { const d = new Date(); d.setMonth(d.getMonth() - n, 1); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-01'; }
function filterRange(rows) {
  const r = RANGE, d = w => w.date.slice(0, 10);
  if (r.kind === 'days') { const cut = isoDaysAgo(r.n); return rows.filter(w => d(w) >= cut); }
  if (r.kind === 'months') { const cut = isoMonthsAgo(r.n); return rows.filter(w => d(w) >= cut); }
  if (r.kind === 'ytd') { const y = isoToday().slice(0, 4); return rows.filter(w => w.date.slice(0, 4) === y); }
  if (r.kind === 'since') return rows.filter(w => (!r.since || d(w) >= r.since) && (!r.until || d(w) <= r.until));
  return rows;  // 'all'
}
function applyRange(desc) {
  RANGE = desc;
  W = filterRange(ALL_W);
  closeRange();
  go('', '');       // reset to the all-time overview of the new range
  render(false);    // in case the hash was already '#/'
}
function rangeLabel() {
  const r = RANGE;
  if (r.kind === 'days') return 'last ' + r.n + 'd';
  if (r.kind === 'months') return 'last ' + r.n + 'm';
  if (r.kind === 'ytd') return isoToday().slice(0, 4);
  if (r.kind === 'since') return (r.since || '…') + '..' + (r.until || 'now');
  return r.label || 'all time';
}
const distinctYears = ws => [...new Set(ws.map(w => w.date.slice(0, 4)))]
  .filter(y => /^\d{4}$/.test(y)).sort().reverse();
// The year a scope belongs to (null == "all years"), so the sidebar's Years/Months
// panels can stay in sync however you got here (a deep link, a bar click, j/k).
const scopeYear = sc => sc.year || null;
// Switch the sidebar mode. When the current scope is incompatible with the new
// mode we reset to the all-time root (fires render via hashchange); otherwise we
// render in place, because go('','') on an unchanged hash would be a silent no-op.
function setBrowse(mode) {
  BROWSE = mode;
  FOCUS = mode === 'projects' ? 'projects' : 'months';
  const k = curScope().kind;
  const forced = (k === 'y' || k === 'm' || k === 'd') ? 'time' : k === 'p' ? 'projects' : null;
  if (forced && forced !== mode) go('', '');
  else render(false);
}
function scopeWorkflows(sc) {
  if (sc.kind === 'y') return W.filter(w => w.date.startsWith(sc.year));
  if (sc.kind === 'm') return W.filter(w => w.date.startsWith(sc.month));
  if (sc.kind === 'd') return W.filter(w => w.date.startsWith(sc.day));
  if (sc.kind === 'p') return W.filter(w => w.project === sc.project);
  if (sc.kind === 's') return sc.session ? [sc.session] : [];
  return W;
}
/* the TUI's per-scope tab tuples (App.year_tabs/month_tabs/day_tabs/project_tabs/
   workflow_tabs), with Sources injected after Overview in the merged view */
function tabsFor(sc) {
  if (sc.kind === 's') {
    const t = ['Overview', 'Models', 'Subagents'];
    if (EXTRAS.id === sc.id && EXTRAS.turns.length) t.push('Turns');
    if (EXTRAS.id === sc.id && EXTRAS.tools.length) t.push('Tools');
    return t;
  }
  const base = { all: ['Overview', 'Models', 'Projects', 'Sessions'],
    y: ['Overview', 'Models', 'Projects', 'Sessions'],
    m: ['Overview', 'Models', 'Projects', 'Sessions'],
    d: ['Overview', 'Projects', 'Sessions'],
    p: ['Overview', 'Models', 'Sessions'] }[sc.kind].slice();
  if (META.combined) base.splice(1, 0, 'Sources');
  return base;
}

/* ---------- sidebar (the lazygit panels) ---------- */
function sideRow(sel, onclick, lab, n, costV, peak) {
  return h('div', { class: 'row' + (sel ? ' sel' : ''), onclick },
    h('span', { class: 'lab' }, lab),
    n ? h('span', { class: 'n' }, n) : null,
    h('span', { class: 'cost' + (costV > 0 ? '' : ' zero') }, moneyLabel(costV) || '·'),
    h('span', { class: 'tb' }, tbar(costV, peak)));
}
function sidePane(title, focusKey, rows) {
  return h('section', { class: 'pane' + (FOCUS === focusKey ? ' focus' : ''),
    onclick: () => { FOCUS = focusKey; } }, h('h3', null, title), h('div', { class: 'rows' }, rows));
}
function renderSidebar(sc) {
  const side = document.getElementById('side');
  side.textContent = '';
  side.appendChild(h('div', { class: 'mode' },
    h('button', { class: BROWSE === 'time' ? 'on' : null, onclick: () => setBrowse('time') }, 'time'),
    h('button', { class: BROWSE === 'projects' ? 'on' : null, onclick: () => setBrowse('projects') }, 'projects')));
  if (BROWSE === 'projects') {
    const rows = projectRows(W).sort((a, b) => b.cost - a.cost);
    const peak = Math.max(...rows.map(r => r.cost), 0);
    side.appendChild(sidePane('Projects', 'projects', [
      sideRow(sc.kind === 'all', () => go('', ''), '∑ all projects', '', sum(W, cost), sum(W, cost)),
      rows.map(r => sideRow(sc.kind === 'p' && sc.project === r.project,
        () => go('p', r.project), projName(r.project), String(r.sessions), r.cost, peak))]));
    return;
  }
  // Years panel -- only worth showing with >1 year (App.years does the same); its
  // "∑ all years" row unscopes the Months panel to the whole history.
  const years = distinctYears(W);
  const selYear = scopeYear(sc);
  if (years.length > 1) {
    const yr = years.map(y => { const g = W.filter(w => w.date.startsWith(y));
      return { year: y, cost: sum(g, cost), sessions: g.length }; });
    const yPeak = Math.max(...yr.map(r => r.cost), 0);
    side.appendChild(sidePane('Years', 'years', [
      sideRow(!selYear, () => go('', ''), '∑ all years', '', sum(W, cost), sum(W, cost)),
      yr.map(r => sideRow(selYear === r.year, () => go('y', r.year),
        r.year, String(r.sessions), r.cost, yPeak))]));
  }
  // Months panel: scoped to the selected year (all months when "all years").
  const monthSrc = selYear ? W.filter(w => w.date.startsWith(selYear)) : W;
  const months = monthRows(monthSrc).slice().reverse(); // newest first, like the TUI
  const mPeak = Math.max(...months.map(r => r.cost), 0);
  const monthRowsUi = [];
  // With no Years panel there's no other way back to the all-time overview, so keep
  // the "∑ all time" row; with a Years panel that lives up there instead.
  if (years.length <= 1)
    monthRowsUi.push(sideRow(sc.kind === 'all', () => go('', ''), '∑ all time', '', sum(W, cost), sum(W, cost)));
  months.forEach(r => monthRowsUi.push(sideRow(sc.month === r.month, () => go('m', r.month),
    r.month, String(r.sessions), r.cost, mPeak)));
  side.appendChild(sidePane(selYear ? 'Months · ' + selYear : 'Months', 'months', monthRowsUi));
  const dayMonth = sc.month || (months.length ? months[0].month : null);
  if (dayMonth) {
    const days = dayRows(W.filter(w => w.date.startsWith(dayMonth))).sort((a, b) => b.day < a.day ? -1 : 1);
    const dPeak = Math.max(...days.map(r => r.cost), 0);
    side.appendChild(sidePane('Days · ' + dayMonth, 'days',
      days.map(r => sideRow(sc.kind === 'd' && sc.day === r.day, () => go('d', r.day),
        r.day.slice(5), String(r.sessions), r.cost, dPeak))));
  }
}

/* ---------- detail pane pieces ---------- */
const pane = (title, ...kids) => h('section', { class: 'pane' }, title ? h('h3', null, title) : null, ...kids);
const sideBySide = (...panes) => h('div', { class: 'cols' }, ...panes);
function tiles(items) {
  return h('div', { class: 'tiles' }, items.map(([k, v, note, moneyish]) =>
    h('div', { class: 'tile' }, h('div', { class: 'k' }, k),
      h('div', { class: 'v' + (moneyish ? ' money' : '') }, v),
      note ? h('div', { class: 'n' }, note) : null)));
}
function statTiles(ws) {
  const st = scopeStats(ws);
  return tiles([
    ['total spend' + (MODE === 'api' ? ' (est.)' : ''), money(st.cost), null, true],
    ['sessions', st.sessions.toLocaleString('en-US'), st.subagents ? '+' + st.subagents.toLocaleString('en-US') + ' subagents' : null],
    ['tokens', hTok(st.tokens)],
    ['active days', st.days.toLocaleString('en-US')],
  ]);
}
function modelsTable(id, rows, collapse) {
  const totalCost = sum(rows, mCost), totalTok = sum(rows, r => r.tokens);
  const share = r => totalCost > 0 ? mCost(r) / totalCost : (totalTok > 0 ? r.tokens / totalTok : 0);
  return table(id, [
    { key: 'model', label: 'Model', asc: true, cls: 'grow', fmt: r => modelCell(r.model) },
    { key: 'runs', label: 'Msgs', align: 'r' },
    { key: 'cost', label: 'Cost', align: 'r', sortVal: mCost, fmt: r => moneyCell(mCost(r)) },
    { key: 'share', label: 'Share', align: 'r', sortVal: share, fmt: r => [pct(share(r), 1), h('span', { class: 'bar' }, h('i', { style: '--w:' + Math.round(100 * share(r)) + '%' }))] },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
    { key: 'cacheRead', label: 'CacheR', align: 'r', fmt: r => hTok(r.cacheRead), cls: 'dim' },
    { key: 'cacheWrite', label: 'CacheW', align: 'r', fmt: r => hTok(r.cacheWrite), cls: 'dim' },
    { key: 'output', label: 'Output', align: 'r', fmt: r => hTok(r.output), cls: 'dim' },
  ], rows, { defaultSort: { key: 'cost', desc: true }, collapse: collapse || 25 });
}
function projectsTable(id, ws, collapse) {
  const rows = projectRows(ws);
  const peak = Math.max(...rows.map(r => r.cost), 0);
  return table(id, [
    { key: 'project', label: 'Project', asc: true, sortVal: r => projName(r.project).toLowerCase(),
      fmt: r => [projName(r.project), ' ', h('span', { class: 'mut' }, shortPath(r.project))], cls: 'grow' },
    { key: 'sessions', label: 'Sessions', align: 'r' },
    { key: 'cost', label: 'Cost', align: 'r', fmt: r => barCell(r.cost, peak) },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
    { key: 'last', label: 'Last active', align: 'r', fmt: r => h('span', { class: 'dim' }, dt(r.last).slice(0, 10)) },
  ], rows, { defaultSort: { key: 'cost', desc: true }, collapse: collapse || 25,
    onRow: r => { go('p', r.project); } });
}
function filterInput() {
  return h('input', { class: 'filter', id: 'filter-input', placeholder: 'filter sessions…', value: FILTER,
    oninput: e => { FILTER = e.target.value; render(false); const el = document.getElementById('filter-input');
      if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } } });
}
function sessionsTable(id, ws) {
  let rows = ws;
  if (FILTER) {
    const q = FILTER.toLowerCase();
    rows = ws.filter(w => (w.title + ' ' + w.project + ' ' + w.id).toLowerCase().includes(q));
  }
  const cols = [
    { key: 'date', label: 'Date', align: 'r', fmt: r => h('span', { class: 'dim' }, dt(r.date)) },
    { key: 'title', label: 'Title', asc: true, sortVal: r => r.title.toLowerCase(), cls: 'grow' },
    { key: 'project', label: 'Project', asc: true, sortVal: r => projName(r.project).toLowerCase(), fmt: r => h('span', { class: 'dim' }, projName(r.project)) },
    { key: 'cost', label: 'Cost', align: 'r', sortVal: cost, fmt: r => moneyCell(cost(r)) },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
    { key: 'subagents', label: 'Sub', align: 'r', fmt: r => r.subagents || h('span', { class: 'mut' }, '·') },
  ];
  if (META.combined) cols.push({ key: 'source', label: 'Src', fmt: r => h('span', { class: 'mut' }, r.source) });
  return h('div', null, filterInput(),
    table(id, cols, rows, { defaultSort: { key: 'cost', desc: true }, collapse: 25,
      onRow: r => { go('s', r.id); } }));
}
function sourcesTable(id, ws) {
  const rows = sourceRows(ws);
  const peak = Math.max(...rows.map(r => r.cost), 0);
  return table(id, [
    { key: 'source', label: 'Source', asc: true },
    { key: 'sessions', label: 'Sessions', align: 'r' },
    { key: 'cost', label: 'Cost', align: 'r', fmt: r => barCell(r.cost, peak) },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
  ], rows, { defaultSort: { key: 'cost', desc: true } });
}

/* turns stay chronological on purpose: the tab answers *when* the money went. */
function turnsTable(turns) {
  const rows = [];
  let cum = 0, lastPrompt = null;
  const groups = new Map();
  for (const t of turns) {
    const key = t.promptId || '';
    groups.set(key, (groups.get(key) || 0) + mCost(t));
  }
  for (const t of turns) {
    const key = t.promptId || '';
    if (key !== lastPrompt) {
      lastPrompt = key;
      const title = (t.promptTitle || '(prompt)').slice(0, 160) + ((t.promptTitle || '').length > 160 ? '…' : '');
      rows.push(h('tr', { class: 'prompt-row' },
        h('td', { colspan: 3 }, '▸ ' + title),
        h('td', { class: 'r' }, moneyCell(groups.get(key))),
        h('td', null, ''), h('td', null, '')));
    }
    cum += mCost(t);
    rows.push(h('tr', null,
      h('td', { class: 'dim' }, t.time.slice(5, 19).replace('T', ' ')),
      h('td', { class: 'indent' }, t.depth ? '↳ ' + t.agent : t.agent),
      h('td', { class: 'grow' }, modelCell(t.model)),
      h('td', { class: 'r' }, moneyCell(mCost(t))),
      h('td', { class: 'r' }, hTok(t.tokens)),
      h('td', { class: 'r dim' }, money(cum))));
  }
  return h('div', { class: 'scroll' }, h('table', null,
    h('thead', null, h('tr', null, h('th', null, 'Time'), h('th', null, 'Agent'), h('th', null, 'Model'),
      h('th', { class: 'r' }, 'Cost'), h('th', { class: 'r' }, 'Tokens'), h('th', { class: 'r' }, 'Cumulative'))),
    h('tbody', null, rows)));
}
function toolsTable(toolRows) {
  const agg = new Map();
  for (const r of toolRows) {
    let a = agg.get(r.tool);
    if (!a) { a = { tool: r.tool, ns: r.ns, real: 0, api: 0, tokens: 0 }; agg.set(r.tool, a); }
    a.real += r.real; a.api += r.api; a.tokens += r.tokens;
  }
  const rows = [...agg.values()];
  const peak = Math.max(...rows.map(mCost), 0);
  return table('t-s-tools', [
    { key: 'tool', label: 'Tool', asc: true, cls: 'grow' },
    { key: 'ns', label: 'Server', asc: true, fmt: r => h('span', { class: 'dim' }, r.ns) },
    { key: 'cost', label: 'Cost', align: 'r', sortVal: mCost, fmt: r => barCell(mCost(r), peak) },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
  ], rows, { defaultSort: { key: 'cost', desc: true } });
}

/* ---------- the detail pane ---------- */
function scopeLabel(sc) {
  if (sc.kind === 'y') return sc.year;
  if (sc.kind === 'm') return monthLabel(sc.month);
  if (sc.kind === 'd') return sc.day;
  if (sc.kind === 'p') return projName(sc.project);
  if (sc.kind === 's') return sc.id;
  return 'all time';
}
function renderOverview(root, sc, ws) {
  if (sc.kind === 's') { renderSessionOverview(root, sc); return; }
  root.appendChild(statTiles(ws));
  if (sc.kind === 'all') {
    const months = monthRows(ws);
    if (months.length) root.appendChild(pane('Spend by month', barChart(months)));
    const years = [...new Set(ws.map(w => w.date.slice(0, 4)))].filter(y => /^\d{4}$/.test(y)).sort().reverse();
    if (years.length) {
      const year = VIEW.calYear && years.includes(VIEW.calYear) ? VIEW.calYear : years[0];
      const byDate = new Map(dayRows(ws.filter(w => w.date.startsWith(year))).map(r => [r.day, r]));
      root.appendChild(pane('Calendar · daily spend',
        years.length > 1 ? h('div', { class: 'ychips' }, years.map(y =>
          h('button', { class: y === year ? 'on' : null, onclick: () => { VIEW.calYear = y; render(false); } }, y))) : null,
        calendar(year, byDate)));
    }
    return;
  }
  if (sc.kind === 'y') {
    const months = monthRows(ws);
    if (months.length) root.appendChild(pane('Spend by month', barChart(months)));
  }
  if (sc.kind === 'p') {
    const months = monthRows(ws);
    if (months.length > 1) root.appendChild(pane('Spend by month', barChart(months)));
  }
  const panes = [];
  if (sc.kind !== 'p') panes.push(pane('Top projects', projectsTable('t-ov-projects', ws, 8)));
  if (sc.kind !== 'd') panes.push(pane('Top models', modelsTable('t-ov-models', modelAgg(ws), 8)));
  if (panes.length === 2) root.appendChild(sideBySide(...panes));
  else panes.forEach(p => root.appendChild(p));
}
function renderSessionOverview(root, sc) {
  const w = sc.session;
  if (!w) { root.appendChild(pane(null, h('div', { class: 'hint' }, 'session not found: ' + sc.id))); return; }
  root.appendChild(h('h2', { class: 'title' }, w.title));
  root.appendChild(h('dl', { class: 'meta' },
    h('dt', null, 'project'), h('dd', null, h('a', { href: '#/p/' + encodeURIComponent(w.project) }, shortPath(w.project))),
    h('dt', null, 'date'), h('dd', null, dt(w.date)),
    META.combined && w.source ? [h('dt', null, 'source'), h('dd', null, w.source)] : null,
    h('dt', null, 'id'), h('dd', null, w.id)));
  root.appendChild(tiles([
    ['total' + (MODE === 'api' ? ' (est.)' : ''), money(cost(w)), 'subagent subtree included', true],
    ['root only', money(rootCost(w)), null, true],
    ['subagents', String(w.subagents)],
    ['tokens', hTok(w.tokens)],
  ]));
  if (EXTRAS.id === sc.id && EXTRAS.loading)
    root.appendChild(h('div', { class: 'hint' }, 'loading turns & tools…'));
  if (!META.serve)
    root.appendChild(h('div', { class: 'hint' }, 'the per-turn timeline and tool attribution are fetched live — run: opentab --serve'));
}
function renderDetail(sc, ws) {
  const root = document.getElementById('view');
  root.textContent = '';
  if (TAB === 'Overview') renderOverview(root, sc, ws);
  else if (TAB === 'Models') {
    const rows = sc.kind === 's' ? (DATA.models[sc.id] || []).map(r => ({ ...r })) : modelAgg(ws);
    root.appendChild(pane('Models · ' + scopeLabel(sc), modelsTable('t-tab-models', rows)));
  } else if (TAB === 'Projects') root.appendChild(pane('Projects · ' + scopeLabel(sc), projectsTable('t-tab-projects', ws)));
  else if (TAB === 'Sessions') root.appendChild(pane('Sessions · ' + scopeLabel(sc), sessionsTable('t-tab-sessions', ws)));
  else if (TAB === 'Sources') root.appendChild(pane('Sources · ' + scopeLabel(sc), sourcesTable('t-tab-sources', ws)));
  else if (TAB === 'Subagents') {
    const nodes = DATA.nodes[sc.id];
    root.appendChild(pane('Session tree', nodes ? table('t-s-nodes', [
      { key: 'title', label: 'Title', asc: true, cls: 'grow', fmt: r => [r.depth ? h('span', { class: 'mut' }, '└ '.padStart(r.depth * 2 + 2, ' ')) : null, r.title] },
      { key: 'agent', label: 'Agent', asc: true, fmt: r => h('span', { class: 'dim' }, r.agent) },
      { key: 'model', label: 'Model', asc: true, fmt: r => modelCell(r.model) },
      { key: 'cost', label: 'Cost', align: 'r', sortVal: mCost, fmt: r => moneyCell(mCost(r)) },
      { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
    ], nodes) : h('div', { class: 'hint' }, 'no subagents in this session')));
  } else if (TAB === 'Turns') root.appendChild(pane('Turns · cost over time', turnsTable(EXTRAS.turns)));
  else if (TAB === 'Tools') root.appendChild(pane('Tools', toolsTable(EXTRAS.tools)));
}

/* ---------- chrome ---------- */
function renderTabs(sc, tabs) {
  const bar = document.getElementById('tabbar');
  bar.textContent = '';
  tabs.forEach(t => bar.appendChild(h('button', { class: t === TAB ? 'on' : null,
    onclick: () => { TAB = t; render(false); } }, t)));
  if (sc.kind === 's' && EXTRAS.id === sc.id && EXTRAS.loading)
    bar.appendChild(h('span', { class: 'note' }, 'loading turns & tools…'));
  else bar.appendChild(h('span', { class: 'note' }, scopeLabel(sc)));
}
function renderCrumbs(sc) {
  const el = document.getElementById('crumbs');
  el.textContent = '';
  const items = [['all time', sc.kind === 'all' ? null : '#/']];
  if (sc.kind === 'p') items.push([projName(sc.project), null]);
  // year hop in the chain only when there's more than one year (else it's noise)
  if (sc.year && distinctYears(W).length > 1) items.push([sc.year, sc.kind === 'y' ? null : '#/y/' + sc.year]);
  if (sc.month && sc.kind !== 'm') items.push([monthLabel(sc.month), '#/m/' + sc.month]);
  else if (sc.kind === 'm') items.push([monthLabel(sc.month), null]);
  if (sc.day && sc.kind === 's') items.push([sc.day, '#/d/' + sc.day]);
  else if (sc.kind === 'd') items.push([sc.day, null]);
  if (sc.kind === 's') items.push([sc.id, null]);
  items.forEach(([label, href], i) => {
    if (i) el.appendChild(h('span', { class: 'sep' }, '/'));
    el.appendChild(href ? h('a', { href }, label) : h('span', { class: 'here' }, label));
  });
}
function chrome() {
  const chips = document.getElementById('hchips');
  chips.textContent = '';
  chips.appendChild(h('span', { class: 'chip' }, 'source ', h('b', null, META.source)));
  chips.appendChild(h('span', { class: 'chip click', title: 'Set range (R)', onclick: openRange }, 'range ', h('b', null, rangeLabel())));
  chips.appendChild(h('span', { class: 'chip' }, META.serve ? 'live · ' + META.generated : META.generated));
  if (META.demo) chips.appendChild(h('span', { class: 'chip demo' }, 'demo data'));
  const right = document.getElementById('hright');
  right.textContent = '';
  if (!META.demo) {
    right.appendChild(h('div', { class: 'seg' },
      h('button', { class: MODE === 'real' ? 'on' : null, onclick: () => { MODE = 'real'; render(false); } }, 'actual $'),
      h('button', { class: MODE === 'api' ? 'on' : null, onclick: () => { MODE = 'api'; render(false); } }, 'what-if $')));
  }
  if (META.demo) { /* demo costs are synthetic: neither badge is true */ }
  else if (MODE === 'api') right.appendChild(h('span', { class: 'badge est' }, 'estimated · list prices'));
  else if (!META.recordsCost) right.appendChild(h('span', { class: 'badge sub' }, '$0 recorded · subscription'));
  right.appendChild(h('button', { class: 'hbtn', title: 'Trends (T)', onclick: openTrends }, '▚ trends'));
  right.appendChild(h('button', { class: 'hbtn', title: 'Model prices (P)', onclick: openPrices }, '$/M prices'));
  right.appendChild(h('button', { class: 'hbtn', title: 'Theme', onclick: openTheme }, '◑ theme'));
  if (META.serve) right.appendChild(h('button', { class: 'hbtn', title: 're-read the data sources',
    onclick: () => fetch('/api/reload', { method: 'POST' }).then(() => location.reload()) }, '↻ refresh'));
  const hints = document.getElementById('hints');
  hints.textContent = '';
  [['j/k', 'move'], ['Tab', 'panel'], ['h/l', 'tabs'], ['Esc', 'back'], ['$', 'what-if'], ['p/t', 'projects/time'], ['T', 'trends'], ['P', 'prices'], ['R', 'range']]
    .forEach(([k, lbl]) => hints.append(h('kbd', null, k), ' ' + lbl + '   '));
  document.getElementById('stamp').textContent =
    'generated by OpenTab v' + META.version + ' · ' + META.range + ' · ' + META.generated
    + (META.demo ? ' · demo data (anonymized, rescaled)' : '');
}

/* ---------- session extras (the --serve drill-in fetch) ---------- */
function ensureExtras(sc) {
  if (sc.kind !== 's' || !META.serve || EXTRAS.id === sc.id) return;
  EXTRAS = { id: sc.id, loading: true, turns: [], tools: [] };
  fetch('/api/session/' + encodeURIComponent(sc.id)).then(r => r.json()).then(x => {
    EXTRAS = { id: sc.id, loading: false, turns: x.turns || [], tools: x.tools || [] };
    render(false);
  }).catch(err => {
    console.error('session extras failed:', err);
    EXTRAS = { id: sc.id, loading: false, turns: [], tools: [] };
    render(false);
  });
}

/* ---------- Trends overlay (T): the TUI's 7-tab Trends, over the whole range ---------- */
const trendMonths = () => [...new Set(W.map(w => w.date.slice(0, 7)))].filter(m => /^\d{4}-\d{2}$/.test(m)).sort().reverse();
const trendWeeks = () => [...new Set(W.map(w => weekMonday(w.date)).filter(Boolean))].sort().reverse();
const trendYears = () => distinctYears(W);
const trendCount = u => (u === 'month' ? trendMonths() : u === 'week' ? trendWeeks() : trendYears()).length;
function monthSpan(first, last) {
  const out = []; let y = +first.slice(0, 4), m = +first.slice(5, 7);
  const ly = +last.slice(0, 4), lm = +last.slice(5, 7);
  while (y < ly || (y === ly && m <= lm)) { out.push(y + '-' + String(m).padStart(2, '0')); if (++m > 12) { m = 1; y++; } }
  return out;
}
function providerAgg(ws) {
  const m = new Map();
  for (const r of modelAgg(ws)) {
    const prov = r.model.split('/')[0] || 'unknown';
    let a = m.get(prov);
    if (!a) { a = { name: prov, real: 0, api: 0, tokens: 0, runs: 0 }; m.set(prov, a); }
    a.real += r.real; a.api += r.api; a.tokens += r.tokens; a.runs += r.runs;
  }
  return [...m.values()];
}
/* vertical bar chart (Daily/Weekly/Monthly) -- pairs: [{label,value,tip?,nav?}] */
function trendChart(pairs, opts = {}) {
  const VW = 1040, VH = 300, padT = 30, padB = 42, padX = 12;
  const vals = pairs.map(p => p.value);
  const peak = Math.max(...vals, 1e-9), total = vals.reduce((a, v) => a + v, 0), n = pairs.length;
  const gap = n > 40 ? 1.5 : 3;
  const bw = Math.max(2, Math.min(52, (VW - 2 * padX) / n - gap));
  const step = bw + gap, x0 = (VW - (n * step - gap)) / 2, plotH = VH - padT - padB;
  // Label every bar with its own value when the bars are wide enough to fit the text
  // without colliding; when too narrow (a fully packed month, like Weekly over many
  // weeks) fall back to labelling just the tallest. Daily keeps bars wide by charting
  // only its active days (trendDaily), so the common case labels every bar.
  const valueEach = step >= 38;
  const svg = s('svg', { viewBox: '0 0 ' + VW + ' ' + VH, class: 'tr-chart', role: 'img', 'aria-label': opts.aria || 'trend chart' });
  for (const f of [0.5, 1]) {
    const y = padT + (1 - f) * plotH;
    svg.appendChild(s('line', { x1: x0, y1: y, x2: VW - x0, y2: y, stroke: thc('line'), 'stroke-width': 1 }));
    // The midline gets an axis label only when the bars aren't individually labelled;
    // with per-bar values it's redundant and collides with the rightmost bar's label.
    if (f !== 1 && !valueEach) svg.appendChild(s('text', { x: VW - x0, y: y - 4, 'text-anchor': 'end', 'font-size': 11, fill: thc('mut'), text: moneyLabel(peak * f) }));
  }
  svg.appendChild(s('line', { x1: x0, y1: VH - padB, x2: VW - x0, y2: VH - padB, stroke: thc('axis'), 'stroke-width': 1 }));
  const peakVal = Math.max(...vals), peakIdx = vals.indexOf(peakVal), tickEvery = Math.max(1, Math.ceil(n / 18));
  pairs.forEach((p, i) => {
    const x = x0 + i * step;
    const hgt = Math.max(p.value > 0 ? 2 : 0, plotH * p.value / peak), y = VH - padB - hgt;
    const g = s('g', { class: 'bg', tip: () => (p.tip || (p.label + '\n' + money(p.value))), onclick: p.nav || null });
    g.appendChild(s('rect', { class: 'hit', x, y: padT, width: step, height: VH - padT - padB }));
    if (hgt > 0) g.appendChild(s('path', { d: roundTop(x, y, bw, hgt, Math.min(3, bw / 2)), fill: thc('accent') }));
    if (p.value > 0 && (valueEach || i === peakIdx))
      g.appendChild(s('text', { x: x + bw / 2, y: y - 5, 'text-anchor': 'middle', 'font-size': 11, fill: thc('ink2'), text: moneyLabel(p.value) }));
    if (i % tickEvery === 0 || i === n - 1)
      g.appendChild(s('text', { x: x + bw / 2, y: VH - 8, 'text-anchor': 'middle', 'font-size': 10, fill: thc('mut'), text: p.label }));
    svg.appendChild(g);
  });
  const summary = h('div', { class: 'tr-summary' });
  if (total > 0)
    summary.append(h('span', null, 'peak ', h('b', null, money(peakVal)), ' on ' + pairs[peakIdx].label),
      h('span', null, 'total ', h('b', null, money(total))), h('span', null, 'avg ', h('b', null, money(total / n))));
  else summary.append(h('span', { class: 'mut' }, 'no spend in view'));
  return h('div', null, svg, summary);
}
/* the ◀ ▶ pager shared by Daily(month)/Weekly(week)/Calendar(year) */
function trendNav(label, idx, count, unit) {
  if (count <= 1) return h('div', { class: 'tr-nav' }, h('span', { class: 'lbl' }, label));
  return h('div', { class: 'tr-nav' },
    h('button', { onclick: () => stepTrend(unit, -1), disabled: idx <= 0 ? '' : null, title: 'newer' }, '◀'),
    h('span', { class: 'lbl' }, label),
    h('button', { onclick: () => stepTrend(unit, 1), disabled: idx >= count - 1 ? '' : null, title: 'older' }, '▶'),
    h('span', { class: 'pos' }, (idx + 1) + ' / ' + count + ' · j/k'));
}
function stepTrend(unit, dir) {
  const key = unit === 'month' ? 'monthIdx' : unit === 'week' ? 'weekIdx' : 'yearIdx';
  TRENDS[key] = Math.max(0, Math.min(trendCount(unit) - 1, TRENDS[key] + dir));
  renderTrends();
}
/* ranked horizontal bars (Models / Providers / Sources) */
function rankedBars(rows, cfg) {
  const peak = Math.max(...rows.map(r => r.cost), 0), total = rows.reduce((a, r) => a + r.cost, 0);
  const head = h('tr', null, h('th', { class: 'l' }, cfg.nameLabel), h('th', { class: 'l' }, ''),
    h('th', null, 'Cost'), h('th', null, 'Share'), cfg.extra.map(c => h('th', null, c.label)));
  const body = rows.map(r => h('tr', null,
    h('td', { class: 'l' }, cfg.nameFmt ? cfg.nameFmt(r) : r.name),
    h('td', { class: 'bar' }, h('div', { class: 'hb' }, h('i', { style: '--w:' + (peak > 0 ? Math.max(2, Math.round(100 * r.cost / peak)) : 0) + '%' }))),
    h('td', null, moneyCell(r.cost)), h('td', { class: 'mut' }, pct(r.cost, total)),
    cfg.extra.map(c => h('td', { class: c.cls || null }, c.get(r)))));
  return h('table', { class: 'rank' }, h('thead', null, head), h('tbody', null, body));
}
function trendDaily() {
  const months = trendMonths();
  if (!months.length) return h('div', { class: 'hint' }, 'No spend in the active range.');
  const idx = Math.max(0, Math.min(TRENDS.monthIdx, months.length - 1)), month = months[idx];
  const byDay = new Map();
  W.filter(w => w.date.startsWith(month)).forEach(w => { const d = +w.date.slice(8, 10); byDay.set(d, (byDay.get(d) || 0) + cost(w)); });
  // Chart only up to the last day that has spend, not the whole calendar month: an
  // in-progress month (e.g. the current one) shouldn't reserve its empty trailing days,
  // which squeeze the bars narrow. Trimming keeps them as wide as Weekly/Monthly so each
  // bar carries its own label instead of colliding.
  let last = 0;
  for (let d = 1; d <= daysInMonth(month); d++) if ((byDay.get(d) || 0) > 0) last = d;
  const pairs = [];
  for (let d = 1; d <= last; d++) {
    const date = month + '-' + String(d).padStart(2, '0'), v = byDay.get(d) || 0;
    pairs.push({ label: String(d), value: v, tip: date + '\n' + money(v), nav: v > 0 ? (() => { closeTrends(); go('d', date); }) : null });
  }
  return h('div', null, trendNav('Daily spend · ' + month, idx, months.length, 'month'), trendChart(pairs, { aria: 'daily spend ' + month }));
}
function trendWeekly() {
  const weeks = trendWeeks();
  if (!weeks.length) return h('div', { class: 'hint' }, 'No spend in the active range.');
  const idx = Math.max(0, Math.min(TRENDS.weekIdx, weeks.length - 1)), monday = weeks[idx];
  const byDate = new Map();
  W.filter(w => weekMonday(w.date) === monday).forEach(w => { const dd = w.date.slice(0, 10); byDate.set(dd, (byDate.get(dd) || 0) + cost(w)); });
  const pairs = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((nm, i) => {
    const date = addDays(monday, i), v = byDate.get(date) || 0;
    return { label: nm, value: v, tip: date + '\n' + money(v), nav: v > 0 ? (() => { closeTrends(); go('d', date); }) : null };
  });
  return h('div', null, trendNav('Weekly spend · ' + monday + ' – ' + addDays(monday, 6), idx, weeks.length, 'week'), trendChart(pairs, {}));
}
function trendMonthly() {
  const rows = monthRows(W);
  if (!rows.length) return h('div', { class: 'hint' }, 'No spend in the active range.');
  const byM = new Map(rows.map(r => [r.month, r]));
  const pairs = monthSpan(rows[0].month, rows[rows.length - 1].month).map(m => {
    const r = byM.get(m), v = r ? r.cost : 0;
    return { label: monthLabel(m), value: v, tip: monthLabel(m) + '\n' + money(v), nav: v > 0 ? (() => { closeTrends(); go('m', m); }) : null };
  });
  return h('div', null, h('div', { class: 'tr-nav' }, h('span', { class: 'lbl' }, 'Monthly spend')), trendChart(pairs, {}));
}
function trendCalendar() {
  const years = trendYears();
  if (!years.length) return h('div', { class: 'hint' }, 'No spend in the active range.');
  const idx = Math.max(0, Math.min(TRENDS.yearIdx, years.length - 1)), year = years[idx];
  const byDate = new Map(dayRows(W.filter(w => w.date.startsWith(year))).map(r => [r.day, r]));
  const total = [...byDate.values()].reduce((a, r) => a + r.cost, 0);
  return h('div', null,
    trendNav('Spend calendar · ' + year, idx, years.length, 'year'),
    calendar(year, byDate, date => { closeTrends(); go('d', date); }),
    h('div', { class: 'tr-summary' }, h('span', null, 'total ', h('b', null, money(total))), h('span', { class: 'mut' }, byDate.size + ' active days')));
}
function trendModels() {
  const rows = modelAgg(W).map(r => ({ name: r.model, cost: mCost(r), runs: r.runs, tokens: r.tokens }))
    .filter(r => r.cost > 0).sort((a, b) => b.cost - a.cost);
  if (!rows.length) return h('div', { class: 'hint' }, 'No priced model spend in the active range.');
  return rankedBars(rows, { nameLabel: 'Model', nameFmt: r => modelCell(r.name),
    extra: [{ label: 'Tokens', get: r => hTok(r.tokens), cls: 'mut' }, { label: 'Msgs', get: r => String(r.runs), cls: 'mut' }] });
}
function trendProviders() {
  const rows = providerAgg(W).map(r => ({ name: r.name, cost: MODE === 'api' ? r.api : r.real, runs: r.runs, tokens: r.tokens }))
    .filter(r => r.cost > 0 || r.tokens > 0).sort((a, b) => b.cost - a.cost || b.tokens - a.tokens);
  if (!rows.length) return h('div', { class: 'hint' }, 'No model usage in the active range.');
  return rankedBars(rows, { nameLabel: 'Provider',
    extra: [{ label: 'Tokens', get: r => hTok(r.tokens), cls: 'mut' }, { label: 'Msgs', get: r => String(r.runs), cls: 'mut' }] });
}
function trendSources() {
  const rows = sourceRows(W).map(r => ({ name: r.source, cost: r.cost, sessions: r.sessions, tokens: r.tokens }))
    .sort((a, b) => b.cost - a.cost || b.tokens - a.tokens);
  if (!rows.length) return h('div', { class: 'hint' }, 'No sessions in the active range.');
  return rankedBars(rows, { nameLabel: 'Source',
    extra: [{ label: 'Tokens', get: r => hTok(r.tokens), cls: 'mut' }, { label: 'Sess', get: r => String(r.sessions), cls: 'mut' }] });
}
function openTrends() { TRENDS.open = true; if (!TREND_TABS.includes(TRENDS.tab)) TRENDS.tab = 'Daily'; renderTrends(); }
function closeTrends() { TRENDS.open = false; renderTrends(); }
function renderTrends() {
  const host = document.getElementById('trends');
  if (!TRENDS.open) { host.hidden = true; host.textContent = ''; return; }
  host.hidden = false; host.textContent = '';
  const tab = TRENDS.tab;
  const body = ({ Daily: trendDaily, Weekly: trendWeekly, Monthly: trendMonthly, Calendar: trendCalendar,
    Models: trendModels, Providers: trendProviders, Sources: trendSources }[tab])();
  const footer = h('div', { class: 'tr-nav', style: 'margin-top:14px' });
  if (META.demo) footer.append(h('span', { class: 'tr-note' }, 'h/l tabs · j/k page · esc close'));
  else if (MODE === 'api') footer.append(h('span', { class: 'badge est' }, 'estimated · list prices'));
  else if (!META.recordsCost) footer.append(h('span', { class: 'tr-note' }, 'press $ to estimate subscription/credit usage at API list prices'));
  else footer.append(h('span', { class: 'tr-note' }, 'h/l tabs · j/k page · $ what-if · esc close'));
  const panel = h('div', { class: 'tr-panel' },
    h('div', { class: 'tr-head' },
      h('h3', null, 'Trends · ' + rangeLabel()),
      h('div', { class: 'tr-tabs' }, TREND_TABS.map(t => h('button', { class: t === tab ? 'on' : null,
        onclick: () => { TRENDS.tab = t; renderTrends(); } }, t))),
      h('button', { class: 'tr-close', onclick: closeTrends }, 'esc ✕')),
    body, footer);
  panel.addEventListener('click', e => e.stopPropagation());
  host.appendChild(panel);
}

/* ---------- Prices overlay (P): models.dev list prices behind $ ---------- */
// log position of a value in a column's [lo,hi] of positive rates -> heat level (matches _price_heat_level)
function priceHeatColor(v, rng) {
  if (!rng || v <= 0) return null;
  const [lo, hi] = rng;
  if (v <= lo) return TH.priceHeat[0];
  const frac = (Math.log(v) - Math.log(lo)) / (Math.log(hi) - Math.log(lo));
  return TH.priceHeat[Math.max(0, Math.min(4, Math.round(frac * 4)))];
}
// (min,max) of positive values per heat column: eff + the 4 raw price rates; null == degenerate
function priceRanges(rows) {
  const cols = [rows.map(r => r.eff).filter(v => v > 0), [], [], [], []];
  rows.forEach(r => r.price.forEach((v, i) => { if (v > 0) cols[i + 1].push(v); }));
  return cols.map(vals => { if (!vals.length) return null; const lo = Math.min(...vals), hi = Math.max(...vals); return hi > lo ? [lo, hi] : null; });
}
const priceRows = () => PRICES.view === 'provider' ? DATA.prices.byRoute : DATA.prices.byModel;
function priceIntro() {
  const m = DATA.prices.mix;
  if (!m) return null;
  const p = f => Math.round(f * 100) + '%';
  return h('div', { class: 'pr-intro' },
    'eff $/M prices each model’s list rates at your token mix: ',
    h('b', null, p(m[0]) + ' input'), ' · ' + p(m[1]) + ' output · ' + p(m[2]) + ' cacheR · ' + p(m[3]) + ' cacheW',
    DATA.prices.mixTokens ? ' (' + hTok(DATA.prices.mixTokens) + ' tokens).' : '.');
}
function renderPrices() {
  const host = document.getElementById('prices');
  if (!PRICES.open) { host.hidden = true; host.textContent = ''; return; }
  host.hidden = false; host.textContent = '';
  let rows = priceRows().slice();
  const ASC = new Set(['model', 'eff']);  // natural order per column (else high→low)
  const key = PRICES.sort;
  const val = { model: r => r.model.toLowerCase(), eff: r => r.eff, use: r => r.share,
    input: r => r.price[0], output: r => r.price[1], cache_read: r => r.price[2], cache_write: r => r.price[3] }[key];
  const flip = PRICES.desc ? -1 : 1;
  if (PRICES.view === 'flat') {
    rows.sort((a, b) => { const x = val(a), y = val(b); return (x < y ? -1 : x > y ? 1 : 0) * flip; });
  } else {
    // grouped: order groups by total spend (empty/Other last), sort within each group
    const gkey = r => PRICES.view === 'family' ? (r.familyLabel || 'Other') : (r.routes[0] || '(direct)');
    const spend = new Map();
    rows.forEach(r => spend.set(gkey(r), (spend.get(gkey(r)) || 0) + r.spend));
    rows.sort((a, b) => {
      const ga = gkey(a), gb = gkey(b);
      if (ga !== gb) { const sa = spend.get(ga), sb = spend.get(gb); if (sa !== sb) return sb - sa; return ga < gb ? -1 : 1; }
      const x = val(a), y = val(b); return (x < y ? -1 : x > y ? 1 : 0) * flip;
    });
  }
  const ranges = priceRanges(rows);
  const usePeak = Math.max(...rows.map(r => r.share), 0);
  const COLS = [['model', 'Model', 'l'], ['eff', 'eff $/M'], ['use', 'use'], ['input', 'in/M'], ['output', 'out/M'], ['cache_read', 'cacheR'], ['cache_write', 'cacheW']];
  const th = COLS.map(([k, label, cls]) => h('th', {
    class: (cls === 'l' ? 'l' : '') + (key === k ? ' sorted' : ''),
    onclick: () => { PRICES.desc = key === k ? !PRICES.desc : !ASC.has(k); PRICES.sort = k; renderPrices(); },
  }, label, key === k ? (PRICES.desc ? ' ▾' : ' ▴') : ''));
  const heatTd = (v, rng, text) => { const c = priceHeatColor(v, rng); return h('td', { style: c ? 'color:' + c + ';font-weight:600' : null }, text); };
  const body = [];
  let lastGrp = null;
  rows.forEach(r => {
    if (PRICES.view !== 'flat') {
      const g = PRICES.view === 'family' ? (r.familyLabel || 'Other') : (r.routes[0] || '(direct)');
      if (g !== lastGrp) { lastGrp = g; body.push(h('tr', { class: 'grp' }, h('td', { colspan: 7 }, '▸ ' + g))); }
    }
    const [ir, orr, crr, cwr] = r.price;
    const crCell = crr <= 0 && ir > 0 ? h('td', { class: 'mut' }, '—') : heatTd(crr, ranges[3], crr.toFixed(2));
    const tag = PRICES.view === 'provider' ? r.familyLabel : (r.routes.length ? r.routes.join(' ') : '');
    body.push(h('tr', null,
      h('td', { class: 'l' }, modelCell(r.model), tag ? h('span', { class: 'tag' }, tag) : null),
      heatTd(r.eff, ranges[0], (r.approx ? '~' : '') + '$' + r.eff.toFixed(2)),
      h('td', null, h('span', { class: 'pr-use' }, pct(r.share, 1),
        h('span', { class: 'hb' }, h('i', { style: '--w:' + (usePeak > 0 ? Math.round(100 * r.share / usePeak) : 0) + '%' })))),
      heatTd(ir, ranges[1], ir.toFixed(2)),
      heatTd(orr, ranges[2], orr.toFixed(2)),
      crCell,
      heatTd(cwr, ranges[4], cwr.toFixed(2))));
  });
  const panel = h('div', { class: 'tr-panel' },
    h('div', { class: 'tr-head' },
      h('h3', null, 'Model prices'),
      h('div', { class: 'pr-views' }, PRICE_VIEWS.map(([v, label]) => h('button', { class: v === PRICES.view ? 'on' : null,
        onclick: () => { PRICES.view = v; renderPrices(); } }, label))),
      h('button', { class: 'tr-close', onclick: closePrices }, 'esc ✕')),
    priceIntro(),
    rows.length ? h('div', { class: 'scroll' }, h('table', { class: 'prices' }, h('thead', null, h('tr', null, th)), h('tbody', null, body)))
      : h('div', { class: 'hint' }, 'No priced models on record (local-only usage, or no models.dev rates). ' + (META.demo ? '' : 'Run opentab --refresh-models to price open models.')),
    h('div', { class: 'tr-nav', style: 'margin-top:12px' }, h('span', { class: 'tr-note' }, 'cheapest-for-your-mix first · click a header to sort · p cycles view · esc close')));
  panel.addEventListener('click', e => e.stopPropagation());
  host.appendChild(panel);
}
function openPrices() { PRICES.open = true; renderPrices(); }
function closePrices() { PRICES.open = false; renderPrices(); }

/* ---------- Range picker (R): scope the active set by date, client-side ---------- */
function renderRange() {
  const host = document.getElementById('rangepick');
  if (!RANGE.pick) { host.hidden = true; host.textContent = ''; return; }
  host.hidden = false; host.textContent = '';
  const presets = [
    ['All time', { kind: 'all', label: 'all time' }], ['Last 7 days', { kind: 'days', n: 7 }],
    ['Last 30 days', { kind: 'days', n: 30 }], ['Last 90 days', { kind: 'days', n: 90 }],
    ['Last 6 months', { kind: 'months', n: 6 }], ['Last 12 months', { kind: 'months', n: 12 }],
    ['This year', { kind: 'ytd' }],
  ];
  const same = d => d.kind === RANGE.kind && d.n === RANGE.n;
  const since = h('input', { type: 'date', id: 'rp-since', value: RANGE.kind === 'since' ? (RANGE.since || '') : '' });
  const until = h('input', { type: 'date', id: 'rp-until', value: RANGE.kind === 'since' ? (RANGE.until || '') : '' });
  const panel = h('div', { class: 'tr-panel rp-panel' },
    h('div', { class: 'tr-head' }, h('h3', null, 'Range'), h('button', { class: 'tr-close', style: 'margin-left:auto', onclick: closeRange }, 'esc ✕')),
    h('div', { class: 'rp-grid' }, presets.map(([label, desc]) =>
      h('button', { class: same(desc) ? 'on' : null, onclick: () => applyRange(desc) }, label))),
    h('div', { class: 'rp-custom' }, 'from', since, 'to', until,
      h('button', { onclick: () => { const s = since.value, u = until.value; if (s || u) applyRange({ kind: 'since', since: s, until: u }); } }, 'apply')));
  panel.addEventListener('click', e => e.stopPropagation());
  host.appendChild(panel);
}
function openRange() { RANGE.pick = true; renderRange(); }
function closeRange() { RANGE.pick = false; renderRange(); }

/* ---------- keyboard: the TUI keymap ---------- */
// Which sidebar panels Tab cycles through, in order, given the data.
function focusOrder() {
  if (BROWSE === 'projects') return ['projects'];
  return distinctYears(W).length > 1 ? ['years', 'months', 'days'] : ['months', 'days'];
}
function sidebarList(sc) {
  if (BROWSE === 'projects') {
    const rows = projectRows(W).sort((a, b) => b.cost - a.cost);
    return { rows: [{ go: () => go('', '') }, ...rows.map(r => ({ go: () => go('p', r.project) }))],
      index: sc.kind === 'p' ? 1 + rows.findIndex(r => r.project === sc.project) : 0 };
  }
  if (FOCUS === 'years' && distinctYears(W).length > 1) {
    const years = distinctYears(W);
    const selYear = scopeYear(sc);
    return { rows: [{ go: () => go('', '') }, ...years.map(y => ({ go: () => go('y', y) }))],
      index: selYear ? 1 + years.indexOf(selYear) : 0 };
  }
  if (FOCUS === 'days') {
    const month = sc.month || (monthRows(W).length ? monthRows(W)[monthRows(W).length - 1].month : null);
    if (!month) return null;
    const days = dayRows(W.filter(w => w.date.startsWith(month))).sort((a, b) => b.day < a.day ? -1 : 1);
    // -1 when no day is selected yet (viewing the month), so the first j/k lands
    // on the first day instead of skipping it.
    return { rows: days.map(r => ({ go: () => go('d', r.day) })),
      index: days.findIndex(r => r.day === sc.day) };
  }
  // Months, scoped to the selected year like the sidebar (App.months does the same).
  const selYear = scopeYear(sc);
  const src = selYear ? W.filter(w => w.date.startsWith(selYear)) : W;
  const months = monthRows(src).slice().reverse();
  const hasAll = distinctYears(W).length <= 1;  // the "∑ all time" row is only shown then
  const monthGo = months.map(r => ({ go: () => go('m', r.month) }));
  const rows = hasAll ? [{ go: () => go('', '') }, ...monthGo] : monthGo;
  const mi = months.findIndex(r => r.month === sc.month);
  const index = sc.kind === 'm' || sc.kind === 'd' || sc.kind === 's'
    ? (hasAll ? 1 : 0) + Math.max(0, mi) : (hasAll ? 0 : -1);
  return { rows, index };
}
document.addEventListener('keydown', e => {
  if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) {
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  // While the Trends overlay is open it owns the keyboard (its own tab/page keys).
  if (TRENDS.open) {
    if (e.key === 'Escape' || e.key === 'T') { closeTrends(); e.preventDefault(); }
    else if (e.key === 'h' || e.key === 'ArrowLeft' || e.key === 'l' || e.key === 'ArrowRight') {
      const i = TREND_TABS.indexOf(TRENDS.tab), step = (e.key === 'h' || e.key === 'ArrowLeft') ? -1 : 1;
      TRENDS.tab = TREND_TABS[(i + step + TREND_TABS.length) % TREND_TABS.length]; renderTrends(); e.preventDefault();
    } else if (e.key === 'j' || e.key === 'ArrowDown' || e.key === 'k' || e.key === 'ArrowUp') {
      const unit = { Daily: 'month', Weekly: 'week', Calendar: 'year' }[TRENDS.tab];
      if (unit) { stepTrend(unit, (e.key === 'j' || e.key === 'ArrowDown') ? 1 : -1); e.preventDefault(); }
    } else if (e.key === '$' && !META.demo) { MODE = MODE === 'api' ? 'real' : 'api'; chrome(); renderTrends(); e.preventDefault(); }
    return;
  }
  if (PRICES.open) {
    if (e.key === 'Escape' || e.key === 'P') closePrices();
    else if (e.key === 'p') { const i = PRICE_VIEWS.findIndex(v => v[0] === PRICES.view); PRICES.view = PRICE_VIEWS[(i + 1) % PRICE_VIEWS.length][0]; renderPrices(); }
    e.preventDefault(); return;
  }
  if (RANGE.pick) { if (e.key === 'Escape') closeRange(); e.preventDefault(); return; }
  if (THEMEPICK) { if (e.key === 'Escape') closeTheme(); e.preventDefault(); return; }
  const sc = curScope();
  const tabs = tabsFor(sc);
  if (e.key === 'T') {
    openTrends();
  } else if (e.key === 'P') {
    openPrices();
  } else if (e.key === 'R') {
    openRange();
  } else if (e.key === 'a') {
    applyRange({ kind: 'all', label: 'all time' });
  } else if (e.key === 'j' || e.key === 'ArrowDown' || e.key === 'k' || e.key === 'ArrowUp') {
    const list = sidebarList(sc);
    if (!list || !list.rows.length) return;
    const step = (e.key === 'j' || e.key === 'ArrowDown') ? 1 : -1;
    const next = Math.max(0, Math.min(list.rows.length - 1, list.index + step));
    if (next !== list.index) list.rows[next].go();
    e.preventDefault();
  } else if (e.key === 'Tab' && BROWSE === 'time') {
    const order = focusOrder();
    const cur = order.indexOf(FOCUS);
    FOCUS = order[((cur < 0 ? 0 : cur) + (e.shiftKey ? -1 : 1) + order.length) % order.length];
    render(false);
    e.preventDefault();
  } else if (e.key === 'h' || e.key === 'ArrowLeft' || e.key === 'l' || e.key === 'ArrowRight') {
    const i = tabs.indexOf(TAB);
    const step = (e.key === 'h' || e.key === 'ArrowLeft') ? -1 : 1;
    TAB = tabs[(i + step + tabs.length) % tabs.length];
    render(false);
  } else if (e.key === 'Escape') {
    const multiYear = distinctYears(W).length > 1;
    if (sc.kind === 's') sc.day ? go('d', sc.day) : go('', '');
    else if (sc.kind === 'd') go('m', sc.month);
    else if (sc.kind === 'm') multiYear ? go('y', sc.year) : go('', '');
    else if (sc.kind === 'y' || sc.kind === 'p') go('', '');
  } else if (e.key === '$' && !META.demo) {
    MODE = MODE === 'api' ? 'real' : 'api';
    render(false);
  } else if (e.key === 'p' && BROWSE !== 'projects') {
    setBrowse('projects');
  } else if (e.key === 't' && BROWSE !== 'time') {
    setBrowse('time');
  }
});

/* ---------- render ---------- */
function render(scrollTop = true) {
  const sc = curScope();
  if (sc.kind === 'p') BROWSE = 'projects';
  else if (sc.kind === 'y' || sc.kind === 'm' || sc.kind === 'd') BROWSE = 'time';
  // Keep FOCUS valid for the current mode/data (e.g. 'years' with only one year,
  // or a stale 'projects'/'days' after a mode switch) so Tab/j/k never wedge.
  const order = focusOrder();
  if (!order.includes(FOCUS)) FOCUS = order[0];
  ensureExtras(sc);
  const tabs = tabsFor(sc);
  if (!tabs.includes(TAB)) TAB = 'Overview';
  const ws = scopeWorkflows(sc);
  chrome();
  renderSidebar(sc);
  renderTabs(sc, tabs);
  renderCrumbs(sc);
  renderDetail(sc, ws);
  renderTrends();  // keep the overlays in sync with a live $/range/theme/data change
  renderPrices();
  renderRange();
  renderTheme();
  if (scrollTop) window.scrollTo(0, 0);
}
document.getElementById('trends').addEventListener('click', closeTrends);  // click the backdrop to close
document.getElementById('prices').addEventListener('click', closePrices);
document.getElementById('rangepick').addEventListener('click', closeRange);
document.getElementById('themepick').addEventListener('click', closeTheme);
// Navigation resets the scoped table state, but keeps the active tab when it
// still exists in the new scope (render() falls back to Overview otherwise) --
// so month->month on the Sessions tab stays on Sessions.
window.addEventListener('hashchange', () => { FILTER = ''; EXPANDED.clear(); render(); });
// Theme precedence: the viewer's saved choice, else the page's baked-in default
// (--theme / meta), else opentab. Applied before the first paint so charts pick it up.
applyTheme((function () { try { return localStorage.getItem('opentab-theme'); } catch (e) { return null; } })() || META.theme || 'opentab');
render();
"""


def render_html(payload: dict) -> str:
    """Wrap a build_payload() dict in the complete self-contained page."""
    meta = payload.get("meta", {})
    title = "OpenTab — AI spend browser" + (" (demo)" if meta.get("demo") else "")
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    js = _JS.replace("__THEMES__", json.dumps(themes.web_payload(), separators=(",", ":")))
    page = _SHELL.replace("__TITLE__", html.escape(title))
    page = page.replace("__FAVICON__", _FAVICON)
    page = page.replace("__CSS__", _CSS)
    page = page.replace("__JS__", js)
    # Payload last: session titles are user text and could contain any of the
    # tokens above; nothing is substituted after this.
    return page.replace("__PAYLOAD__", blob)
