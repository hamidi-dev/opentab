"""The self-contained browser page: inline CSS + JS around one embedded JSON payload.

`render_html(payload)` is a pure function from the `opentab.web.build_payload()`
shape to a single HTML string -- no template engine, no network, no external
assets, so the file works from disk, from GitHub Pages (`--demo --html`), or from
`--serve` unchanged. The page deliberately mirrors the TUI: a lazygit-style
sidebar (Months / Days, or Projects -- with the same eighth-block cost bars) next
to a tabbed detail pane whose tabs are the TUI's own per-scope tab tuples, TUI
box borders, and the TUI keymap (`j`/`k`, `Tab`, `h`/`l`, `Esc`, `$`, `w`, `p`/`t`).
Selection is hash-routed (deep-linkable, browser back = step out); the active
tab is transient UI state. The $ what-if toggle swaps which of the two embedded
cost fields every view reads -- the exact analogue of App._apply_price_mode().
`w` arms a what-if *model*: a session-scoped rate substitution (its Subagents tree
and its Overview, nothing else) computed client-side off the per-model token splits --
so it, unlike $, is a reprice, and one the payload can only ship the ingredients for.

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
    "%3Crect width='16' height='16' rx='3' fill='%237aa2f7'/%3E"
    "%3Ctext x='8' y='12.5' font-size='11' text-anchor='middle' font-family='monospace' "
    "font-weight='bold' fill='%231a1b26'%3E%24%3C/text%3E%3C/svg%3E"
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
<div id="whatifpick" hidden></div>
<div id="themepick" hidden></div>
<div id="tip" hidden></div>
<script type="application/json" id="opentab-data">__PAYLOAD__</script>
<script>__JS__</script>
</body>
</html>
"""

_CSS = r"""
/* Role tokens (not hues): a theme fills these slots. The values here are the
   default "tokyo-night" theme so the page renders before the theme JS runs; applyTheme
   overrides them on :root at load. See the THEMES map in the script. */
:root{
  --bg:#1a1b26; --bg-glow:#24283b; --panel:#1f2335; --panel2:#292e42;
  --line:#414868; --line2:#2a2e42; --axis:#545c7e;
  --ink:#c0caf5; --ink2:#a9b1d6; --mut:#565f89;
  --accent:#7aa2f7; --accent-bright:#9cb8ff; --good:#9ece6a; --bad:#f7768e;
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
  font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:var(--accent);font-weight:700}
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
/* top-level mode switch (Time · Projects · Machines): a segmented control, the active
   segment filled with the accent so it reads as a real tab, not just tinted text */
.mode{display:flex;gap:0;margin:0 0 12px;border:1px solid var(--line);border-radius:6px;
  overflow:hidden;background:var(--panel)}
.mode button{flex:1;font:inherit;font-size:11px;padding:5px 0;border:0;
  border-left:1px solid var(--line);background:transparent;color:var(--ink2);cursor:pointer;
  transition:background .12s,color .12s}
.mode button:first-child{border-left:0}
.mode button:not(.on):hover{background:var(--panel2);color:var(--ink)}
.mode button.on{background:var(--accent);color:var(--bg);font-weight:700}

/* detail tab bar -- the TUI's Overview │ Models │ Projects │ Sessions. Centered pill
   tabs: every tab is a visible raised chip (so an inactive tab reads as a clickable tab,
   not grey text), the active one filled with the accent and lifted by a soft glow. */
#tabbar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;justify-content:center;
  padding:2px;margin:12px 0 14px}
#tabbar button{font:inherit;font-size:12px;padding:5px 16px;border:1px solid var(--line);
  border-radius:20px;background:var(--panel2);color:var(--ink2);cursor:pointer;
  transition:background .13s,color .13s,border-color .13s,box-shadow .13s,transform .13s}
#tabbar button:not(.on):hover{color:var(--ink);border-color:var(--accent);
  background:color-mix(in srgb,var(--accent) 13%,var(--panel2));transform:translateY(-1px)}
#tabbar button.on{background:var(--accent);color:var(--bg);border-color:var(--accent);
  font-weight:700;box-shadow:0 3px 12px color-mix(in srgb,var(--accent) 38%,transparent)}
#tabbar button.ld{opacity:.5;font-style:italic;animation:tabpulse 1.2s ease-in-out infinite}
@keyframes tabpulse{50%{opacity:.85}}

/* breadcrumbs / footer */
#crumbs{padding:0 2px 10px;color:var(--mut);min-height:16px;font-size:12px}
#crumbs .sep{margin:0 7px;color:var(--line)}
#crumbs .here{color:var(--ink)}
#foot{display:flex;gap:14px;flex-wrap:wrap;justify-content:space-between;margin-top:18px;
  padding-top:12px;border-top:1px solid var(--line2);color:var(--mut);font-size:11px}
#foot kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:3px;padding:0 5px;
  font-family:inherit;font-size:10.5px;color:var(--ink2);background:var(--panel);margin-right:2px}

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
/* Token economics: two 100%-stacked bars, the same five token types measured twice
   (volume, then spend). The 2px gap between segments is the spacer that keeps adjacent
   fills apart without a border; the outer radius is on the track so only the two ends
   round. Segment labels ride INSIDE their segment, in the page background colour rather
   than a fixed near-black, so they stay legible on a light theme's lighter steps. */
.sbar{margin:2px 0 14px}
.sbar .lbl{display:flex;justify-content:space-between;font-size:11px;color:var(--mut);margin-bottom:5px}
.sbar .track{display:flex;gap:2px;height:32px;border-radius:4px;overflow:hidden}
.sbar .seg{display:flex;align-items:center;justify-content:center;min-width:0;overflow:hidden;
  white-space:nowrap;font-size:11px;font-weight:700}
.tk-legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:11.5px;color:var(--ink2)}
.tk-legend span{display:flex;align-items:center;gap:6px}
.tk-legend i{width:10px;height:10px;border-radius:2px;flex:none}
/* the swatch in the detail table's Type cell -- the table repeats the bars' colour key
   so a row can be matched to its segment without counting across the legend */
.lgd{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:7px;vertical-align:-1px}
/* Session flamegraph: one band partitioning the session, with the names positioned in
   rows beneath it rather than written into the fill -- text punched through a colour
   fights it, and only ever fits the segments that least needed a label. */
.flame{margin:2px 0 12px}
.flame .lbl{display:flex;justify-content:space-between;font-size:11px;color:var(--mut);margin-bottom:5px}
.flame .track{display:flex;gap:1px;height:38px;border-radius:4px;overflow:hidden}
/* The label rows share the band's flex ratios, so every name sits under its own slice by
   construction. overflow:hidden keeps a long one inside its slice instead of shoving the
   next along; the share threshold (NAMED) is what stops a sliver showing one letter. */
.flame .names{display:flex;gap:1px;margin-top:3px}
.flame .names > div{min-width:0;overflow:hidden;white-space:nowrap;font-size:11px;
  font-weight:600;padding-right:4px}
.flame .seg{display:flex;align-items:center;justify-content:center;min-width:0;overflow:hidden;
  white-space:nowrap;font-size:11px;font-weight:700;padding:0 2px}
.flame-head{font-size:12.5px;color:var(--ink2);margin:0 0 10px}
.flame-head b{color:var(--ink)}
/* the session Overview money card (mirrors the TUI's Money card: donut + stats, with the
   armed what-if as accent-highlighted rows below a rule) */
.money{display:flex;flex-wrap:wrap;gap:14px 28px;align-items:center;margin:8px 0 2px}
.money-legend{display:flex;gap:14px;font-size:11px;color:var(--mut);margin-bottom:8px}
.money-legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.money-legend .lg-root{background:var(--accent)}
.money-legend .lg-sub{background:var(--good)}
.money-stats{flex:1;min-width:200px;display:grid;grid-template-columns:1fr auto;gap:3px 18px;font-size:13px}
.money-stats .ms-k{color:var(--mut)}
.money-stats .ms-v{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink)}
.money-stats .ms-v.money{color:var(--good)}
.wi-rows{display:grid;grid-template-columns:1fr auto;gap:4px 18px;margin-top:12px;padding-top:11px;
  border-top:1px solid color-mix(in srgb,var(--accent) 45%,var(--line));font-size:13px}
.wi-rows .wi-k{color:var(--accent);font-weight:600}
.wi-rows .wi-v{text-align:right;font-variant-numeric:tabular-nums;color:var(--accent);font-weight:700}
.wi-rows .wi-v.wi-up{color:var(--bad)} .wi-rows .wi-v.wi-down{color:var(--good)}

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
tfoot td{border-top:2px solid var(--line);border-bottom:0;font-weight:700;padding-top:7px;padding-bottom:6px}
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

/* tools: a passive treemap above the exact table. Area and shade encode the same
   visible measure on purpose -- the first is proportion, the second preserves the
   hierarchy when adjacent tiles have similar geometry. */
.tool-map-wrap{margin:2px 0 18px}
.tool-map-head{display:flex;justify-content:space-between;gap:12px;align-items:baseline;
  margin:0 2px 6px;color:var(--ink2);font-size:12px}
.tool-map-head b{color:var(--ink);font-size:13px}
.tool-map{position:relative;height:clamp(150px,18vw,220px);overflow:hidden;
  border-radius:6px;background:var(--panel2)}
.tool-tile{position:absolute;border:2px solid var(--panel);background-clip:padding-box;
  padding:9px 11px;overflow:hidden;display:flex;flex-direction:column;justify-content:flex-start;
  line-height:1.25;container-type:size}
.tool-tile .tn{font-size:clamp(12px,2.3cqw,19px);font-weight:800;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.tool-tile .tv{font-size:clamp(10px,1.7cqw,14px);margin-top:3px;white-space:nowrap;opacity:.9}
.tool-tile .tr{font-size:clamp(10px,1.6cqw,13px);margin-top:2px;white-space:nowrap;opacity:.75;
  font-weight:600}
.tool-tile.tiny{padding:2px}
.tool-table{margin-top:2px}
@media (max-width:600px){.tool-map{height:170px}.tool-map-head{align-items:flex-start;flex-direction:column;gap:1px}}

/* turns */
tr.prompt-row td{color:var(--accent);padding-top:9px;font-weight:600}
tr.prompt-row td:first-child{white-space:normal;overflow-wrap:anywhere}
tr.prompt-row.rowlink{cursor:pointer}
/* the unfolded whole prompt (header click): its own line breaks kept */
tr.prompt-full-row td{padding:2px 10px 8px 22px}
.prompt-full{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--ink2);
  font-size:11.5px;border-left:2px solid var(--line);padding-left:10px}
td.indent{color:var(--ink2)}
/* a compaction between two turns: amber like the Context tab's ▼, and never folded away */
tr.compact-row td{color:var(--accent);font-size:11.5px;padding-top:5px;
  border-top:1px dashed color-mix(in srgb, var(--accent) 45%, transparent)}

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
#pr-filter{font:inherit;font-size:12px;background:var(--bg);color:var(--ink);border:1px solid var(--line);
  border-radius:6px;padding:5px 10px;width:150px;outline:none}
#pr-filter:focus{border-color:var(--accent)}
.pin{cursor:pointer;color:var(--mut);margin-right:7px;user-select:none}
.pin.on{color:var(--accent)}
.pin:hover{color:var(--ink)}
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
.wi-panel{max-width:760px}  /* wider than the range/price panels: catalog model ids are long */
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
/* what-if model picker (w) -- one model armed as a SESSION-scoped comparison target */
#whatifpick{position:fixed;inset:0;z-index:200;background:var(--scrim);display:flex;align-items:flex-start;justify-content:center;padding:26px 20px;overflow-y:auto}
#whatifpick[hidden]{display:none}
.wi-list{display:grid;gap:4px;margin-top:6px;max-height:54vh;overflow-y:auto}
.wi-row{display:flex;align-items:center;gap:10px;font:inherit;font-size:12.5px;padding:6px 11px;border:1px solid var(--line);
  border-radius:6px;background:var(--panel2);color:var(--ink);cursor:pointer;text-align:left}
.wi-row:hover{border-color:var(--accent)}
.wi-row.cur{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 14%,var(--panel2))}
.wi-row.on{color:var(--accent)}
.wi-row .wi-n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wi-row .wi-t{color:var(--mut);font-size:11px;flex:none}
.wi-tier{display:flex;align-items:center;gap:8px;margin-top:10px}
.wi-tier .tr-note{margin-left:auto}
#wi-filter{font:inherit;font-size:12px;background:var(--bg);color:var(--ink);border:1px solid var(--line);
  border-radius:6px;padding:5px 10px;width:170px;outline:none;margin-left:auto}
#wi-filter:focus{border-color:var(--accent)}
/* an armed target: the chip is the page's twin of the TUI's lit `w model` footer key --
   "a target is set", never a claim that the numbers on screen are counterfactual */
.chip.wi{color:var(--accent);border-color:var(--accent);cursor:pointer}
.wi-up{color:var(--bad)}    /* the target would have cost more than your models did */
.wi-down{color:var(--good)} /* ...and less: what running it all on the target would save */
.wi-total{margin-top:10px;font-size:13px;color:var(--ink)}
.wi-total b{color:var(--accent)}

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
h2.title{font-size:16px;margin:2px 0 12px;overflow-wrap:anywhere;font-weight:700;
  border-left:3px solid var(--accent);padding-left:11px;line-height:1.35}
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
let TH = THEMES['tokyo-night'];      // the active theme object (charts read it)
const thc = k => TH.css[k];          // theme color for an SVG chart slot
// The one CATEGORICAL ramp on the page -- five slots for the five token types. Not a
// theme field: a theme supplies chrome plus two SEQUENTIAL ramps (heat, priceHeat), and
// pressing a sequential ramp into categorical duty would say "more" where it means
// "different". Two steppings of the same five hues instead, picked for the light and the
// dark chart surface and validated as a set (lightness band, chroma floor, adjacent-pair
// separation under simulated colour-vision deficiency, contrast against the pane). Order
// is the safety mechanism, not decoration -- do not reorder without re-validating.
const TOK_SERIES_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181'];
const TOK_SERIES_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4'];
const tokSeries = () => TH.dark ? TOK_SERIES_DARK : TOK_SERIES_LIGHT;
// Ink for a label sitting ON a fill: picked per SEGMENT from that fill's luminance, not
// from the theme. One theme-wide choice fails on the ramp's own spread -- the light
// theme's near-white ink is unreadable on the yellow slot while it is fine on the blue.
function inkOn(hex) {
  const v = i => parseInt(hex.slice(1 + i * 2, 3 + i * 2), 16) / 255;
  const lin = c => c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  const L = 0.2126 * lin(v(0)) + 0.7152 * lin(v(1)) + 0.0722 * lin(v(2));
  // WCAG contrast against black is (L+0.05)/0.05, against white 1.05/(L+0.05).
  return (L + 0.05) / 0.05 > 1.05 / (L + 0.05) ? '#101014' : '#ffffff';
}
const CUR = { theme: 'tokyo-night' };
function applyTheme(id) {
  const t = THEMES[id] ? id : 'tokyo-night';
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
let BROWSE = 'time';        // sidebar mode: 'time' (Months/Days) | 'projects' | 'machines', like the TUI
let FOCUS = 'months';       // which sidebar list j/k drives
// The Machines-scope sub-drill (the TUI's zoom_source/zoom_project/zoom_model): clicking a
// row on the box's Harnesses/Projects/Models tab narrows its Sessions to that one dimension
// without leaving the machine axis. Transient like TAB (cleared on any scope change), and
// mutually exclusive -- only one dimension at a time -- so nothing composes. {dim, value}.
let MSUB = null;
function setMsub(dim, value) { MSUB = { dim, value }; TAB = 'Sessions'; render(false); }
function clearMsub() { MSUB = null; render(false); }
function msubFilter(ws) {
  if (!MSUB) return ws;
  // Fall back to META.source exactly like sourceRows() groups -- else a session with an
  // empty source shows under "remote" in the Harnesses table but the drill (which set
  // MSUB.value from that row) filters against "unknown" and opens an empty Sessions list.
  if (MSUB.dim === 'source') return ws.filter(w => (w.source || META.source) === MSUB.value);
  if (MSUB.dim === 'project') return ws.filter(w => w.project === MSUB.value);
  if (MSUB.dim === 'model') return ws.filter(w => (DATA.models[w.id] || []).some(x => x.model === MSUB.value));
  return ws;
}
// Per-machine niceties for the Machines mode (live vs pulled, export time/version);
// {} off the fleet view. Keyed by machine name (== w.machine, demo-scrambled under demo).
const MMETA = DATA.machineMeta || {};
let FILTER = '';
const SORT = {};
const EXPANDED = new Set(); // table ids whose "show all" is open (reset per view)
const VIEW = { calYear: null };
let EXTRAS = { id: null, loading: false, turns: [], tools: [], context: null }; // per-session Turns/Tools/Context (serve)
// The Trends overlay (T) -- mirrors the TUI's 7-tab Trends over the whole range.
const TREND_TABS = ['Daily', 'Weekly', 'Monthly', 'Calendar', 'Models', 'Providers', 'Harnesses'].concat(META.machines ? ['Machines'] : []);
let TRENDS = { open: false, tab: 'Daily', monthIdx: 0, weekIdx: 0, yearIdx: 0, drill: null };
// The P prices overlay: the models.dev list-price reference behind $ (app-wide,
// never range-scoped -- like the TUI). eff sorts cheapest-first; others high→low.
const PRICE_VIEWS = [['flat', 'flat list'], ['family', 'by vendor'], ['provider', 'by provider'], ['all', 'models.dev']];
let PRICES = { open: false, view: 'flat', sort: 'eff', desc: false, q: '' };
// The `w` what-if model: ONE model armed as a comparison target -- "what if the
// expensive model had done the subagents' work too?". SESSION-scoped, exactly like the
// TUI: its only effects are the selected session's Subagents tree and its Overview
// summary. The sidebar, the rollups, Trends, Prices and the $ toggle are all untouched
// by an armed target (an app-wide reprice would leave $ nothing to toggle). Deliberately
// TRANSIENT: never localStorage, never the hash -- unlike the theme and the price pins,
// which do persist. A remembered target would silently re-frame every later visit.
let WHATIF = { model: null, open: false, q: '', i: 0, cat: false };
const WI_MODELS = (DATA.whatif && DATA.whatif.models) || [];   // armable targets, most-used first
// List rates ($/M) for EVERY model you used -- not just the armable ones: the baseline
// prices each model's own tokens at its own rates, and a session can well contain a model
// you cannot arm (an unpriced id has no real rate card to substitute in, but its tokens
// still have to be counted, or the baseline would quietly drop them).
const WI_PRICE = new Map(Object.entries((DATA.whatif && DATA.whatif.rates) || {}));
const WI_UNPRICED = new Set((DATA.whatif && DATA.whatif.unpriced) || []);
// Models with no API rate at all (ollama, lmstudio, ...). The what-if never arms them,
// and tokenEconomics drops them from BOTH its rows -- App.token_economics' rule.
const WI_LOCAL = new Set((DATA.whatif && DATA.whatif.local) || []);
// The picker's second tier (Tab): the whole models.dev catalog, in the TUI's own rows and
// order (cheapest-for-your-mix first) so both frontends arm identical names at identical
// rates. The rates merge into WI_PRICE up front -- whatifTotals prices a catalog-armed
// target through the same map as a used one. eff/~ expand lazily like catalogRows().
((DATA.whatif && DATA.whatif.catalog) || []).forEach(c => { if (!WI_PRICE.has(c.m)) WI_PRICE.set(c.m, c.p); });
let WI_CATALOG = null;
function whatifCatalog() {
  if (!WI_CATALOG) {
    const mix = (DATA.prices && DATA.prices.mix) || [1, 0, 0, 0];
    WI_CATALOG = ((DATA.whatif && DATA.whatif.catalog) || []).map(c => {
      const [ir, orr, cr, cw] = c.p;
      const approx = cr <= 0 && ir > 0;
      const eff = mix[0] * ir + mix[1] * orr + mix[2] * (approx ? ir : cr) + mix[3] * cw;
      return { model: c.m, price: c.p, eff, approx };
    });
  }
  return WI_CATALOG;
}

/* ---------- formatting (mirrors opentab.formatting) ---------- */
const money = v => (v > 0 && v < 0.005) ? '<$0.01'
  : '$' + v.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const moneyLabel = v => v <= 0 ? '' : v < 0.005 ? '<$.01' : v < 10 ? '$' + v.toFixed(2)
  : v < 1000 ? '$' + Math.round(v) : v < 10000 ? '$' + (v / 1000).toFixed(1) + 'k'
  : '$' + Math.round(v / 1000) + 'k';
// Unit switches just before the boundary, mirroring formatting.human_tokens: rounding
// first would print "1000.0k" for 999,950 and the two frontends would disagree.
const hTok = v => v >= 999.95e9 ? (v / 1e12).toFixed(1) + 'T' : v >= 999.95e6 ? (v / 1e9).toFixed(1) + 'B'
  : v >= 999.95e3 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : String(v);
const pct = (p, w) => w <= 0 ? '-' : (p > 0 && 100 * p / w < 1) ? '<1%' : Math.round(100 * p / w) + '%';
// Mirrors formatting.human_duration exactly (s -> m -> "Hh Mm" -> "Dd Hh", zero
// remainders dropped) so the TUI and the web can never disagree about a span.
const hDur = s => { s = Math.max(0, Math.floor(s)); if (s < 60) return s + 's';
  const m = Math.floor(s / 60); if (m < 60) return m + 'm';
  const hh = Math.floor(m / 60), mm = m % 60;
  if (hh < 24) return mm ? hh + 'h ' + mm + 'm' : hh + 'h';
  const d = Math.floor(hh / 24), rh = hh % 24; return rh ? d + 'd ' + rh + 'h' : d + 'd'; };
const cost = w => MODE === 'api' ? w.api : w.real;
const rootCost = w => MODE === 'api' ? w.apiRoot : w.realRoot;
const mCost = r => MODE === 'api' ? r.api : r.real;
const shortPath = p => META.home && p.startsWith(META.home) ? '~' + p.slice(META.home.length) : p;
const projName = p => { const parts = shortPath(p).split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : (p || '(no project)'); };
const MN = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const monthLabel = m => MN[+m.slice(5, 7) - 1] + ' ’' + m.slice(2, 4);
const dt = s => (s || '').slice(0, 16).replace('T', ' ');
// "2h ago" for a machine summary's export time -- the twin of formatting.relative_age.
function relAge(iso) {
  if (!iso) return '';
  const t = Date.parse(iso); if (isNaN(t)) return '';
  const s = (Date.now() - t) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
// Re-pull one machine over ssh (serve only), then reload the page with the fresh data.
function refreshMachine(name) {
  fetch('/api/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ machine: name }) }).then(() => location.reload());
}
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
function machineRows(ws) {
  return [...groupBy(ws, w => w.machine || 'unknown')].map(([machine, g]) =>
    ({ machine, cost: sum(g, cost), sessions: g.length, tokens: sum(g, w => w.tokens) }));
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

/* ---------- model matching (mirrors pricing.model_matches) ---------- */
// The ONE rule behind every model-list filter -- the P overlay's `f` and the `w`
// picker's, which ask the same question of the same rows and must not answer it
// differently. Matched PER FIELD, with a different rule per field because the fields
// are typed differently:
//   * the model id by word-anchored fuzzy match (the mirror of
//     util.anchored_fuzzy_match): a substring, or a subsequence that may scatter
//     inside a word but only enters a word at its first character -- "opus48" ->
//     claude-opus-4-8, "snt45" -> claude-sonnet-4-5, dots==dashes so "opus4.5" finds
//     "opus-4-5"; a BARE subsequence let "opus" walk qwen3-cOder-PlUS, and with rows
//     kept in column order (below) instead of fzf's match-ranking, that junk sorted
//     to the top of the 5k-row catalog instead of out of sight;
//   * the route and the vendor label by plain SUBSTRING -- a short fixed vocabulary you
//     type in full ("openai", "copilot"); nobody abbreviates them, and the bare
//     subsequence over them was the same machine: "gpt" walked "github-copilot"
//     (g-ithub-co-p-ilo-t) and dragged every Claude model sold through Copilot into a
//     search for GPT.
// Callers keep their own row order: a filtered list still answers "which of these do I
// lean on", so rows are never re-ranked by match quality.
const dashDots = t => String(t).toLowerCase().replace(/(\d)\.(?=\d)/g, '$1-');
// The anchored walk, the line-for-line mirror of util.anchored_fuzzy_match: one pass
// over the text tracking every viable alignment at once (NOT a backtracking regex --
// a near-miss over a repeated-letter id made one enumerate alignments for seconds).
// done[i] = q[:i] fully matched before this point; inWord[i] = ...with its last char
// inside the CURRENT word, so q[i] may scatter onto any later char of that word; a
// new word admits q[i] only as its first char (start + done). A plain-subsequence
// pre-scan rejects most rows before the walk runs.
const anchoredFuzzy = (q, t) => {
  if (!q || t.includes(q)) return true;
  let pos = 0;
  for (const ch of q) { pos = t.indexOf(ch, pos) + 1; if (!pos) return false; }
  const qa = Array.from(q), n = qa.length;
  const prefixAt = new Map();
  for (let i = n; i >= 1; i--) {  // descending, so one text char never chains two query chars
    if (!prefixAt.has(qa[i - 1])) prefixAt.set(qa[i - 1], []);
    prefixAt.get(qa[i - 1]).push(i);
  }
  const done = new Array(n + 1).fill(false); done[0] = true;
  let inWord = new Array(n + 1).fill(false);
  let start = true;
  for (const c of t) {
    const boundary = ' -_/.'.includes(c);
    for (const i of (prefixAt.get(c) || [])) {
      // A boundary char typed in the query ("opus4-5", "-48") matches any later
      // text boundary: a separator is not a word, so it carries no anchoring of
      // its own -- order (done) is the whole requirement -- and done-without-inWord
      // is exactly "the next query char may enter the following word at its head".
      // Letters keep the anchored rule.
      const ok = boundary ? done[i - 1] : (inWord[i - 1] || (start && done[i - 1]));
      if (ok) {
        if (i === n) return true;
        done[i] = true;
        if (!boundary) inWord[i] = true;
      }
    }
    if (boundary) { inWord = new Array(n + 1).fill(false); start = true; }
    else start = false;
  }
  return false;
};
function modelMatches(q, model, routes, familyLabel) {
  if (!q) return true;
  const qq = dashDots(q);
  if (anchoredFuzzy(qq, dashDots(model || ''))) return true;
  const fields = (routes || []).map(r => String(r).toLowerCase());
  if (familyLabel) fields.push(String(familyLabel).toLowerCase());
  return fields.some(f => f.includes(qq));
}

/* ---------- the `w` what-if model (mirrors pricing.api_equivalent_cost) ---------- */
// One node's tokens at a target model's list rates: tok = [input, output, reasoning,
// cacheRead, cacheWrite] (the payload's split), rates = [in, out, cacheR, cacheW] in
// $/M. Reasoning bills as output; cache reads/writes at their own rates. NO
// missing-cache-read fallback -- api_equivalent_cost doesn't do one either (that's the
// eff $/M blend's rule, and applying it here would quietly inflate a target whose
// cache-read rate models.dev doesn't carry). A pure rate substitution: same tokens,
// one price list, not a simulated rerun.
function whatifCost(tok, rates) {
  if (!tok || !rates) return 0;
  const [inp, out, reason, cr, cw, cw1h] = tok, [ir, orr, crr, cwr, cwr1h] = rates;
  // cw1h is the 1h-TTL SUBSET of cw, which Anthropic bills at 2.00x input instead of the
  // 5m tier's 1.25x that the single catalog cache-write rate encodes. Replacement, not
  // addition: those tokens leave the 5m bucket and enter the 1h one, they are not extra
  // volume. Both default to 0/absent, which is exactly the old arithmetic.
  const long = Math.min(Math.max(cw1h || 0, 0), cw || 0);
  const write = (cw - long) * cwr + long * (cwr1h || cwr);
  return (inp * ir + (out + reason) * orr + cr * crr + write) / 1e6;
}
/* ---------- token economics (the mirror of App.token_economics) ---------- */
// The five token types, in the payload's `tok` order -- pricing.TOKEN_TYPES.
const TOK_TYPES = ['Uncached input', 'Output', 'Reasoning', 'Cache read', 'Cache write'];
// Where a scope's tokens went, and where its money went: the same five types measured
// twice, because a type's share of VOLUME and its share of SPEND differ by up to two
// orders of magnitude (an output token costs 50x a cache-read token). Computed here
// rather than shipped precomputed for the same reason the rollups are: the scope is
// whatever the page is showing after drill-in, sorting and the ignored filters, and only
// the client knows that.
//
// Always LIST rates, whatever the $ toggle says -- no backend attributes recorded spend
// per token type, so there is nothing else to decompose (same basis as the what-if
// baseline). The arithmetic is whatifCost's, kept in pieces instead of summed, so the
// five parts add up to the API-equivalent figure shown elsewhere.
function tokenEconomics(ws) {
  const tokens = [0, 0, 0, 0, 0], cost = [0, 0, 0, 0, 0];
  let saved = 0, local = 0, est = false, missingCache = false;
  ws.forEach(w => (DATA.models[w.id] || []).forEach(r => {
    // Only the first FIVE entries are token TYPES; a sixth, when present, is the 1h-TTL
    // subset of Cache write -- a pricing refinement, not extra volume, so it must never
    // be summed into a total or it double-counts every long write.
    const tok = (r.tok || [0, 0, 0, 0, 0]).slice(0, 5);
    const long1h = Math.min(Math.max((r.tok || [])[5] || 0, 0), tok[4] || 0);
    if (WI_LOCAL.has(r.model)) { local += tok.reduce((a, b) => a + b, 0); return; }
    // Unreachable by construction -- `rates` is built from the same per-model rows this
    // reduces over, so every model here has an entry. Guarded anyway: skipping one row
    // beats throwing and blanking the whole pane.
    const p = WI_PRICE.get(r.model);
    if (!p) return;
    const [ir, orr, crr, cwr, cwr1h] = p;
    tok.forEach((v, i) => { tokens[i] += v; });
    cost[0] += tok[0] * ir / 1e6;
    cost[1] += tok[1] * orr / 1e6;
    cost[2] += tok[2] * orr / 1e6;      // reasoning bills at the output rate
    cost[3] += tok[3] * crr / 1e6;
    // Cache write, split by TTL like whatifCost: the long-TTL subset at its own rate,
    // the rest at the 5m one. The Cache write ROW stays one row -- the tier changes what
    // those tokens cost, not what kind of token they are.
    cost[4] += ((tok[4] - long1h) * cwr + long1h * (cwr1h || cwr)) / 1e6;
    // Only a real, non-zero cache rate is a real discount: counting a MISSING one as
    // free would report the whole input cost as "saved", the opposite of what it means.
    if (crr > 0) saved += tok[3] * Math.max(0, ir - crr) / 1e6;
    else if (tok[3] > 0 && ir > 0) missingCache = true;
    if (r.tokens > 0 && WI_UNPRICED.has(r.model)) est = true;
  }));
  const totalTokens = tokens.reduce((a, b) => a + b, 0);
  if (totalTokens <= 0) return null;
  return { tokens, cost, saved, est, missingCache, local,
    totalTokens, totalCost: cost.reduce((a, b) => a + b, 0) };
}

// The ONE place a session's two what-if figures come from -- the Subagents tree's TOTAL
// and the Overview summary both read it, so they cannot drift (the TUI's
// App.whatif_session_totals, mirrored). Null when no target is armed, or the session has
// no per-model rows to price.
//
// Both sides are computed from the session's PER-MODEL rows (DATA.models[id]), the only
// place its tokens are split per model, and both at LIST rates:
//   * actual = each model's own tokens at its own list rates -- every token, exactly;
//   * whatif = the session's summed token split at the target's list rates.
// Apples-to-apples on purpose, and independent of the $ toggle: recorded cost is $0 on a
// subscription route and a few cents on a partially-metered one, so a recorded baseline
// would report savings that never happened. It also means arming a model a single-model
// session already used lands on exactly $0 change -- same tokens, same rates.
//
// NOT the node rows: workflow_nodes labels a node with its single dominant model, so
// pricing a node's whole split at that one label is wrong for any session that switched
// model mid-flight, and no per-node baseline is computable from what the stores expose.
function whatifTotals(id) {
  if (!WHATIF.model) return null;
  const rows = DATA.models[id];
  if (!rows || !rows.length) return null;
  // Six slots, not five: the trailing one is the 1h-TTL cache-write subset, and BOTH
  // sides of the comparison have to carry it -- that is what keeps arming a model a
  // single-model session already used an exactly $0 change.
  const tot = [0, 0, 0, 0, 0, 0];
  let actual = 0;
  rows.forEach(r => {
    actual += whatifCost(r.tok, WI_PRICE.get(r.model));
    r.tok.forEach((v, i) => { tot[i] += v; });
  });
  const whatif = whatifCost(tot, WI_PRICE.get(WHATIF.model));
  // `est`: one of this session's models has no real list rate, so its tokens are priced
  // at a generic guess and the baseline stops being a list price. Mirrors
  // App.whatif_baseline_is_estimated -- both frontends mark it `~` rather than quote it.
  // Zero-token rows don't count (an aborted turn names a model but contributes nothing,
  // so it cannot turn an exact baseline into an estimate) -- same rule as the Python.
  const est = rows.some(r => r.tokens > 0 && WI_UNPRICED.has(r.model));
  return { target: WHATIF.model, actual, whatif, delta: whatif - actual, est };
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
  /* opts.totals (key -> cell content) closes a multi-row table with a TOTAL row.
     It sums ALL rows, not the collapsed slice, and sits outside tbody so sorting
     and row clicks never touch it; a one-row table is its own total. */
  const foot = opts.totals && rows.length > 1
    ? h('tfoot', null, h('tr', null,
        cols.map(c => h('td', { class: [c.align === 'r' ? 'r' : '', c.cls || ''].join(' ').trim() || null },
          opts.totals[c.key] != null ? opts.totals[c.key] : ''))))
    : null;
  return h('div', null,
    h('div', { class: 'scroll' }, h('table', null, h('thead', null, head), h('tbody', null, body), foot)),
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
  if (kind === 'M' && arg) return { kind: 'M', machine: arg };
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
  FOCUS = mode === 'projects' ? 'projects' : mode === 'machines' ? 'machines' : 'months';
  const k = curScope().kind;
  const forced = (k === 'y' || k === 'm' || k === 'd') ? 'time'
    : k === 'p' ? 'projects' : k === 'M' ? 'machines' : null;
  if (forced && forced !== mode) go('', '');
  else render(false);
}
function scopeWorkflows(sc) {
  if (sc.kind === 'y') return W.filter(w => w.date.startsWith(sc.year));
  if (sc.kind === 'm') return W.filter(w => w.date.startsWith(sc.month));
  if (sc.kind === 'd') return W.filter(w => w.date.startsWith(sc.day));
  if (sc.kind === 'p') return W.filter(w => w.project === sc.project);
  if (sc.kind === 'M') return W.filter(w => (w.machine || 'unknown') === sc.machine);
  if (sc.kind === 's') return sc.session ? [sc.session] : [];
  return W;
}
/* the TUI's per-scope tab tuples (App.year_tabs/month_tabs/day_tabs/project_tabs/
   workflow_tabs), with Sources injected after Overview in the merged view */
function tabsFor(sc) {
  if (sc.kind === 's') {
    const t = ['Overview', 'Subagents'];   // a session's model mix lives in its Overview
    const mine = EXTRAS.id === sc.id;
    if (mine && EXTRAS.loading) t.push('Turns', 'Tools', 'Context'); // placeholders while the fetch runs
    else {
      if (mine && EXTRAS.turns.length) t.push('Turns');
      if (mine && EXTRAS.tools.length) t.push('Tools');
      if (mine && EXTRAS.context) t.push('Context');
    }
    return t;
  }
  const base = { all: ['Overview', 'Models', 'Projects', 'Sessions'],
    y: ['Overview', 'Models', 'Projects', 'Sessions'],
    m: ['Overview', 'Models', 'Projects', 'Sessions'],
    d: ['Overview', 'Projects', 'Sessions'],
    p: ['Overview', 'Models', 'Sessions'],
    M: ['Overview', 'Sessions', 'Models', 'Projects'] }[sc.kind].slice();
  if (META.combined) base.splice(1, 0, 'Harnesses');
  // The fleet's per-scope Machines breakdown, after Harnesses -- but not in the Machines
  // scope itself (sc.kind 'M'), which is already one box.
  if (META.machines && sc.kind !== 'M') {
    const cut = base.indexOf('Harnesses');
    base.splice(cut >= 0 ? cut + 1 : 1, 0, 'Machines');
  }
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
  // The top-level browse-mode tabs (the TUI's t/p/m strip): Machines only in a fleet.
  side.appendChild(h('div', { class: 'mode' },
    h('button', { class: BROWSE === 'time' ? 'on' : null, onclick: () => setBrowse('time') }, 'time'),
    h('button', { class: BROWSE === 'projects' ? 'on' : null, onclick: () => setBrowse('projects') }, 'projects'),
    META.machines ? h('button', { class: BROWSE === 'machines' ? 'on' : null, onclick: () => setBrowse('machines') }, 'machines') : null));
  if (BROWSE === 'machines') {
    // The live box floats first (● / ○), like App.machines.
    const rows = machineRows(W).sort((a, b) =>
      ((MMETA[b.machine] || {}).live ? 1 : 0) - ((MMETA[a.machine] || {}).live ? 1 : 0) || b.cost - a.cost);
    const peak = Math.max(...rows.map(r => r.cost), 0);
    side.appendChild(sidePane('Machines', 'machines', [
      sideRow(sc.kind === 'all', () => go('', ''), '∑ all machines', '', sum(W, cost), sum(W, cost)),
      rows.map(r => sideRow(sc.kind === 'M' && sc.machine === r.machine, () => go('M', r.machine),
        ((MMETA[r.machine] || {}).live ? '● ' : '○ ') + r.machine, String(r.sessions), r.cost, peak))]));
    return;
  }
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
function modelsTable(id, rows, collapse, onRow) {
  const totalCost = sum(rows, mCost), totalTok = sum(rows, r => r.tokens);
  // Share is a share OF COST. With nothing priced -- a subscription backend with `$`
  // off, i.e. the default view for Claude Code / Codex / Copilot -- it used to fall back
  // to a TOKEN share, so a column headed Share showed confident percentages next to a
  // Cost column reading $0.00 everywhere, meaning something else entirely. The TUI's
  // pct() prints "-" for a zero denominator; do the same rather than answer a different
  // question under the same heading.
  const share = r => totalCost > 0 ? mCost(r) / totalCost : null;
  return table(id, [
    { key: 'model', label: 'Model', asc: true, cls: 'grow', fmt: r => modelCell(r.model) },
    { key: 'runs', label: 'Msgs', align: 'r' },
    { key: 'cost', label: 'Cost', align: 'r', sortVal: mCost, fmt: r => moneyCell(mCost(r)) },
    { key: 'share', label: 'Share', align: 'r', sortVal: r => share(r) || 0, fmt: r => share(r) === null ? '-' : [pct(share(r), 1), h('span', { class: 'bar' }, h('i', { style: '--w:' + Math.round(100 * share(r)) + '%' }))] },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
    { key: 'cacheRead', label: 'CacheR', align: 'r', fmt: r => hTok(r.cacheRead), cls: 'dim' },
    { key: 'cacheWrite', label: 'CacheW', align: 'r', fmt: r => hTok(r.cacheWrite), cls: 'dim' },
    { key: 'output', label: 'Output', align: 'r', fmt: r => hTok(r.output), cls: 'dim' },
  ], rows, { defaultSort: { key: 'cost', desc: true }, collapse: collapse || 25, onRow: onRow || null,
    totals: { model: 'TOTAL', runs: String(sum(rows, r => r.runs)), cost: moneyCell(totalCost),
      tokens: hTok(totalTok), cacheRead: hTok(sum(rows, r => r.cacheRead)),
      cacheWrite: hTok(sum(rows, r => r.cacheWrite)), output: hTok(sum(rows, r => r.output)) } });
}
// NOT the shared pct(): it renders anything under 1% as "<1%", and here the sub-percent
// rows are the punchline -- output is half a percent of the tokens and a sixth of the
// bill, which "<1%" cannot say. Never rounds a present-but-tiny row down to "0.00%".
function tokShare(v, tot) {
  const s = tot > 0 ? 100 * v / tot : 0;
  if (s >= 10 || s === 0) return s.toFixed(0) + '%';
  if (s >= 1) return s.toFixed(1) + '%';
  return s >= 0.005 ? s.toFixed(2) + '%' : '<0.01%';
}

/* The Token economics pane. Two 100%-stacked bars over the SAME five token types --
   what you sent, then what you paid -- because the reading is the gap between them: a
   type's block is huge in one bar and a sliver in the other. Reading either bar alone
   gives the opposite answer, so they have to sit one above the other, sharing a scale
   and a colour per type.

   The table below is the same five rows with the numbers behind the bars; it is the
   accessibility path as much as the detail one (identity never rests on colour alone).
   Its order is FIXED by cost -- the ordering is part of what the bars say, so it does
   not go through the sortable `table()`, whose headers would silently re-rank it and
   keep that ranking across every later scope.

   (The TUI's box makes the opposite trade for a real constraint: an 8-colour terminal
   cannot promise five distinguishable fills, so it pairs a bar per measure on each row
   instead. Same numbers, same order, same notes.) */
function tokenEconomicsPane(ws, label) {
  const e = tokenEconomics(ws);
  if (!e) return null;
  const approx = e.est ? '~' : '';
  const SER = tokSeries();
  // Rows keep their TOKEN-TYPE index as `i` so a type owns one colour in both bars and
  // in the table, whatever the cost ordering does to their positions.
  const rows = TOK_TYPES.map((t, i) => ({ t, i, tok: e.tokens[i], cost: e.cost[i] }))
    .filter(r => r.tok > 0 || r.cost > 0)
    .sort((a, b) => b.cost - a.cost || b.tok - a.tok);
  const stack = (caption, total, get, fmt) => h('div', { class: 'sbar' },
    h('div', { class: 'lbl' }, h('span', null, caption), h('span', null, fmt(total))),
    h('div', { class: 'track' }, rows.map(r => {
      const v = get(r), share = total > 0 ? v / total : 0;
      return h('div', {
        class: 'seg',
        style: 'flex:' + share + ' 0 0;background:' + SER[r.i] + ';color:' + inkOn(SER[r.i]),
        title: r.t + ' · ' + fmt(v) + ' · ' + tokShare(v, total),
      // A label only where the segment can hold one; the legend and the table carry
      // the rest, so a 0.4% sliver never gets an unreadable smear of text.
      }, share > 0.075 ? tokShare(v, total) : null);
    })));
  const grid = h('div', { class: 'scroll' }, h('table', null,
    h('thead', null, h('tr', null,
      ['Type', 'Tokens', 'Volume', 'Cost', 'Spend'].map((c, i) =>
        h('th', { class: i ? 'r' : null }, c)))),
    h('tbody', null, rows.map(r => h('tr', null,
      h('td', null, h('span', { class: 'lgd', style: 'background:' + SER[r.i] }), r.t),
      h('td', { class: 'r' }, hTok(r.tok)),
      h('td', { class: 'r' }, tokShare(r.tok, e.totalTokens)),
      h('td', { class: 'r' }, moneyCell(r.cost)),
      h('td', { class: 'r' }, tokShare(r.cost, e.totalCost))))),
    h('tfoot', null, h('tr', null,
      h('td', null, 'TOTAL'), h('td', { class: 'r' }, hTok(e.totalTokens)),
      h('td', null, ''), h('td', { class: 'r' }, approx + money(e.totalCost)),
      h('td', null, '')))));
  const body = h('div', null,
    tiles([['spend · list rates', approx + money(e.totalCost), null, true],
      ['tokens', hTok(e.totalTokens)],
      ...(e.saved > 0 ? [['cache reads saved', money(e.saved), 'vs the input rate', true]] : [])]),
    stack('share of tokens sent', e.totalTokens, r => r.tok, hTok),
    stack('share of dollars billed', e.totalCost, r => r.cost, money),
    h('div', { class: 'tk-legend' }, rows.map(r =>
      h('span', null, h('i', { style: 'background:' + SER[r.i] }), r.t))),
    grid);
  const notes = [];
  if (e.saved > 0) notes.push('cache reads saved ' + money(e.saved)
    + ' against paying the input rate for them');
  if (e.est) notes.push('~ a model here has no known list rate — its tokens use a generic estimate');
  if (e.missingCache) notes.push('a model here has no cache-read rate on file — its reads '
    + 'price at $0, so Cache read is understated');
  if (e.local) notes.push(hTok(e.local) + ' local-model tokens excluded — no API rate to price them at');
  return pane('Token economics · ' + label,
    h('div', { class: 'hint' }, 'always at list rates — no backend records which token '
      + 'type your spend went to, so this is the only decomposition there is'),
    body,
    notes.map(n => h('div', { class: 'hint' }, n)));
}

/* ---------- the session flamegraph (the mirror of App.session_flame) ---------- */
// A session's spend as a hierarchy: the whole session on top, partitioned below into the
// root's own work and each subagent. Width is money, which is what the tree TABLE below
// cannot say -- a table ranks the nodes, an icicle shows the proportion, and "the root
// kept 42% and five subagents split the rest" is one glance instead of six subtractions.
//
// The Python side's docstrings are canonical for the two decisions that matter: widths
// are the Cost column's own meaning (so chart and table can never disagree about a node),
// and depth is one band because workflow_nodes records a node's depth but not its PARENT
// -- a nested execution joins the band as a marked sibling rather than being drawn under
// a parent the stores never named. Computed here, not shipped precomputed, because the
// widths follow the live $ toggle.
const FLAME_SELF_SLOT = 0, FLAME_CHILD_SLOTS = [1, 2, 3, 4];
// Below this share a segment is a few pixels wide and carries no text of any length.
// The TUI can measure its cells exactly; the page cannot measure a proportional
// layout before it lays out, so it thresholds on the share instead and leans on
// overflow:hidden to keep a long name inside its own slice either way.
const NAMED = 0.06;
const FLAME_DULL = new Set(['', '-', 'subagent', 'unknown', '(untitled)']);
// A share of one session's spend, BOTH ends guarded (Renderer._flame_pct's rule): an
// icicle prints the parts beside the whole, so "root kept 100%" above five visible
// subagent segments contradicts itself, and a sub-half-percent segment that exists must
// not read "0%". Math.round is half-up and Python's round() is half-to-even, so the
// Python floors (share + 0.5) to keep an exact 12.5% reading the same in both.
function fPct(frac) {
  if (frac >= 1) return '100%';
  if (frac <= 0) return '0%';
  const share = 100 * frac;
  if (share >= 99.5) return '>99%';
  if (share < 0.5) return '<1%';
  return Math.round(share) + '%';
}
// OpenCode records the agent in its own column for only some sessions; for the rest it
// writes "-" and leaves the name in the TITLE, as "(@code-reviewer)". Mining it back out
// is not a guess about a title's wording, it is reading a field the backend stored in the
// wrong place -- on real data it lifts the share of subagent nodes that can name their
// agent from 15% to 85%. (App._FLAME_AGENT_TAG.)
const FLAME_AGENT_TAG = /\(@([\w.-]+)/;
// The model's short display spelling: route prefix dropped, release-date and
// reasoning-effort suffixes stripped -- pricing.display_model, transcribed, because a
// segment has tens of pixels of text and not eighty characters.
const flameModel = m => String(m || '').split('/').pop()
  .replace(/-(?:\d{8}|\d{4}-\d{2}-\d{2})$/, '').replace(/-(?:minimal|low|medium|high|xhigh)$/, '');
// A segment names the AGENT that ran it, never the session's title: the title is a
// sentence that never fits, and it is one column away in the table below.
function flameLabel(n) {
  const agent = (n.agent || '').trim();
  let name = FLAME_DULL.has(agent.toLowerCase()) ? '' : agent;
  if (!name) {
    const tag = FLAME_AGENT_TAG.exec(n.title || '');
    name = tag ? tag[1] : 'subagent';
  }
  return (n.depth > 1 ? '↳ ' : '') + name;
}
// Unique names: most backends don't name their subagents (Claude Code writes "subagent"
// for every Task), and a key of six identical entries identifies nothing. Fall back to
// the start time -- distinct AND findable in the table's Started column -- at minute then
// second precision, and to a cost rank when a batch shares one timestamp exactly.
function flameLabels(rows) {
  // A Map, not a plain object: these keys are session titles, and a title of exactly
  // "constructor", "toString" or "__proto__" reads its value straight off
  // Object.prototype -- `(n[l] || 0) + 1` then yields NaN (or silently fails to store),
  // `n[l] > 1` is false, and two identically-named executions are never detected as
  // repeated. Python counts with list.count() and has no such hole, so this is exactly
  // the kind of one-sided hazard that makes the two frontends disagree.
  const base = rows.map(flameLabel), n = new Map();
  base.forEach(l => n.set(l, (n.get(l) || 0) + 1));
  const many = l => n.get(l) > 1;
  if (!base.some(many)) return base;
  for (const end of [16, 19]) {
    const stamped = base.map((l, i) => many(l) ? (l + ' ' + (rows[i].date || '').slice(11, end)).trim() : l);
    if (new Set(stamped).size === stamped.length) return stamped;
  }
  // Last rung: the cost rank -- which is still not a guarantee on its own (a node
  // genuinely titled "foo #1" beside two titled "foo" collides with a rank), so whatever
  // is left tied is separated here. Uniqueness is the contract; a ladder that ALMOST
  // reaches it just relocates the indistinguishable pair.
  const seen = new Set();
  return base.map((l, i) => {
    let name = many(l) ? l + ' #' + (i + 1) : l;
    while (seen.has(name)) name += ' ·';
    seen.add(name);
    return name;
  });
}
function sessionFlame(nodes) {
  if (!nodes || !nodes.length) return null;
  const paid = nodes.reduce((a, n) => a + mCost(n), 0);
  // Dollars unless there are none: a subscription backend with $ off records $0
  // everywhere, and a hierarchy of zeros is a blank frame. Tokens still answer "where
  // did the work go", which is the same question one price list away. (Costs arrive
  // rounded to 6dp by web._money6, so a whole session under a millionth of a dollar
  // reads as tokens here while the TUI still divides its raw floats. Both readings say
  // "this cost nothing"; the rounding is the payload's, shared with every other figure
  // on the page, and is not worth a second cost field.)
  const unit = paid > 0 ? 'cost' : 'tokens';
  const val = n => unit === 'cost' ? mCost(n) : n.tokens;
  const total = unit === 'cost' ? paid : nodes.reduce((a, n) => a + n.tokens, 0);
  if (!(total > 0)) return null;
  const segments = [];
  const own = nodes.filter(n => !n.depth).reduce((a, n) => a + val(n), 0);
  const rootNode = nodes.find(n => !n.depth);
  // Two names per execution: the bare `agent` (position identifies a slice under the
  // band, so five slices reading "code-reviewer" is the truth there) and `label`, which
  // carries whatever flameLabels had to add to tell them apart in the key.
  if (own > 0) segments.push({ label: 'root (self)', agent: 'root (self)',
    model: flameModel(rootNode && rootNode.model), value: own, share: own / total,
    slot: FLAME_SELF_SLOT, depth: 0 });
  // Cost-descending, tokens then title breaking ties. The title breaks it DESCENDING and
  // by CODE POINT, because the Python sorts the whole (value, tokens, title) tuple with
  // reverse=True. Neither shortcut is that: localeCompare's collation disagrees on case
  // and accents ("Z" vs "a"), and a bare `<` compares UTF-16 code UNITS, which ranks an
  // astral character below a high BMP one (an emoji title sorts under "�" in JS and
  // over it in Python). A tie ordered differently between the frontends hands the same
  // two segments different colours in the TUI and on the page.
  const byTitle = (a, b) => {
    const x = Array.from(String(a.title)), y = Array.from(String(b.title));
    for (let i = 0; i < Math.min(x.length, y.length); i++) {
      const d = y[i].codePointAt(0) - x[i].codePointAt(0);
      if (d) return d;
    }
    return y.length - x.length;
  };
  const kids = nodes.filter(n => n.depth > 0)
    .sort((a, b) => val(b) - val(a) || b.tokens - a.tokens || byTitle(a, b));
  const drawn = kids.filter(n => val(n) > 0), labels = flameLabels(drawn);
  drawn.forEach((n, i) => segments.push({ label: labels[i], agent: flameLabel(n),
    model: flameModel(n.model), value: val(n), share: val(n) / total,
    slot: FLAME_CHILD_SLOTS[i % FLAME_CHILD_SLOTS.length], depth: n.depth }));
  // `est` marks a WIDTH ON SCREEN as an estimate, which needs two guards beyond the $
  // mode (App.session_flame's rule -- both frontends must mark the same figures
  // approximate): the unit, since token widths were never priced at all, and val(n) > 0,
  // since an aborted $0/0-token child contributes no segment and must not put a "~" on a
  // chart whose every drawn width was recorded.
  // The model every drawn segment ran on, or '' when they differ: 85 of 135 real
  // delegating sessions are single-model end to end, and there the model belongs in the
  // caption once instead of under every segment. (SessionFlame.one_model.)
  const models = new Set(segments.map(s => s.model).filter(Boolean));
  return { segments, total, unit,
    est: unit === 'cost' && MODE === 'api' && nodes.some(n => !n.real && val(n) > 0),
    deep: drawn.filter(n => n.depth > 1).length, silent: kids.length - drawn.length,
    selfShare: own / total, oneModel: models.size === 1 ? [...models][0] : '' };
}
function flamePane(nodes) {
  const f = sessionFlame(nodes);
  if (!f || !f.segments.length) return null;
  const SER = tokSeries(), fmt = f.unit === 'cost' ? money : hTok, approx = f.est ? '~' : '';
  const kids = f.segments.filter(s => s.depth > 0);
  const own = f.total - kids.reduce((a, s) => a + s.value, 0);
  // The headline is the chart's finding as a sentence -- the part that survives being
  // read on a phone, where the thinner segments are a few pixels each.
  const parts = kids.length
    ? ['root kept ', h('b', null, fPct(f.selfShare)), ' (' + fmt(own) + ') · ',
       kids.length + ' subagent' + (kids.length === 1 ? '' : 's') + ' split ' + fmt(kids.reduce((a, s) => a + s.value, 0))]
    : ['root kept all ' + approx + fmt(f.total) + ' — no subagent recorded a share'];
  // The bare agent in the sentence: it points at one segment, so the handle that tells
  // five "code-reviewer" runs apart would be noise there.
  if (kids.length > 1) parts.push(' · biggest ' + kids[0].agent + ' ' + fPct(kids[0].share));
  const band = h('div', { class: 'track' }, f.segments.map(s => h('div', {
    class: 'seg',
    style: 'flex:' + s.share + ' 0 0;background:' + SER[s.slot] + ';color:' + inkOn(SER[s.slot]),
    title: s.label + (s.model ? ' · ' + s.model : '') + ' · ' + fmt(s.value) + ' · ' + fPct(s.share),
  // Only the share rides in the fill now; the names sit under the band, where they do
  // not have to fight the colour they were punched through. A sliver keeps neither.
  }, s.share > NAMED ? fPct(s.share) : null)));
  // The label rows: one flex cell per segment, sharing the band's own flex ratios, so a
  // name is under its slice by construction rather than by arithmetic. Below NAMED a
  // segment is too narrow for text of any length, and the key picks it up instead.
  const labelRow = textOf => h('div', { class: 'names' }, f.segments.map(s =>
    h('div', { style: 'flex:' + s.share + ' 0 0;color:' + SER[s.slot] },
      s.share > NAMED ? textOf(s) : null)));
  const notes = [];
  if (f.unit !== 'cost') notes.push('nothing here recorded a cost, so width is TOKENS — press $ to divide list-price dollars instead');
  else if (f.est) notes.push('widths include list-price estimates for what recorded no cost');
  if (f.deep) notes.push(f.deep + ' execution' + (f.deep === 1 ? '' : 's') + ' ran under another subagent (↳) — shown alongside, since the tree records depth but not parents');
  if (f.silent) notes.push(f.silent + ' subagent' + (f.silent === 1 ? '' : 's') + ' recorded no '
    + (f.unit === 'cost' ? 'spend' : 'tokens') + ' — no width to draw, still in the table below');
  // The key carries only what position could not -- the segments too thin to be named
  // under the band -- and picks up their model too, since it has a whole wrapping line
  // to spend where they had a few pixels. Name every segment and it disappears entirely.
  const rest = f.segments.filter(s => !(s.share > NAMED));
  const caption = 'session · width = ' + (f.unit === 'cost' ? 'dollars' : 'tokens')
    + (f.oneModel ? ' · all on ' + f.oneModel : '');
  return pane('Where the money went · ' + approx + fmt(f.total),
    h('div', { class: 'flame-head' }, ...parts),
    h('div', { class: 'flame' },
      h('div', { class: 'lbl' }, h('span', null, caption), h('span', null, approx + fmt(f.total))),
      band, labelRow(s => s.agent),
      // A second positioned row for the models, and only when the segments disagree
      // about them: a uniform tree said it once in the caption already.
      f.oneModel ? null : labelRow(s => s.model)),
    rest.length ? h('div', { class: 'tk-legend' }, rest.map(s =>
      h('span', { title: fmt(s.value) + ' · ' + fPct(s.share) }, h('i', { style: 'background:' + SER[s.slot] }),
        s.label + (!f.oneModel && s.model ? ' ' + s.model : '')))) : null,
    notes.map(n => h('div', { class: 'hint' }, n)));
}

function projectsTable(id, ws, collapse, onRow) {
  const rows = projectRows(ws);
  const peak = Math.max(...rows.map(r => r.cost), 0);
  // onRow: undefined -> the default project-scope drill (go); null -> a read-only
  // breakdown; a function -> a custom drill (the Machines scope narrows in place via
  // MSUB instead of jumping out of the machine axis to the project scope).
  return table(id, [
    { key: 'project', label: 'Project', asc: true, sortVal: r => projName(r.project).toLowerCase(),
      fmt: r => [projName(r.project), ' ', h('span', { class: 'mut' }, shortPath(r.project))], cls: 'grow' },
    { key: 'sessions', label: 'Sessions', align: 'r' },
    { key: 'cost', label: 'Cost', align: 'r', fmt: r => barCell(r.cost, peak) },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
    { key: 'last', label: 'Last active', align: 'r', fmt: r => h('span', { class: 'dim' }, dt(r.last).slice(0, 10)) },
  ], rows, { defaultSort: { key: 'cost', desc: true }, collapse: collapse || 25,
    onRow: onRow === undefined ? (r => { go('p', r.project); }) : onRow });
}
function filterInput() {
  return h('input', { class: 'filter', id: 'filter-input', placeholder: 'filter sessions…', value: FILTER,
    oninput: e => { FILTER = e.target.value; render(false); const el = document.getElementById('filter-input');
      if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } } });
}
function sessionCols() {
  const cols = [
    { key: 'date', label: 'Date', align: 'r', fmt: r => h('span', { class: 'dim' }, dt(r.date)) },
    { key: 'dur', label: 'Worked', align: 'r', sortVal: r => r.dur || 0,
      fmt: r => r.dur == null ? h('span', { class: 'mut' }, '·') : h('span', { class: 'dim' }, hDur(r.dur)) },
    { key: 'title', label: 'Title', asc: true, sortVal: r => r.title.toLowerCase(), cls: 'grow' },
    { key: 'project', label: 'Project', asc: true, sortVal: r => projName(r.project).toLowerCase(), fmt: r => h('span', { class: 'dim' }, projName(r.project)) },
    { key: 'cost', label: 'Cost', align: 'r', sortVal: cost, fmt: r => moneyCell(cost(r)) },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
    { key: 'subagents', label: 'Subagents', align: 'r', fmt: r => r.subagents || h('span', { class: 'mut' }, '·') },
  ];
  if (META.combined) cols.push({ key: 'source', label: 'Hns', asc: true, fmt: r => h('span', { class: 'mut' }, r.source) });
  if (META.machines) cols.push({ key: 'machine', label: 'Machine', asc: true, fmt: r => h('span', { class: 'mut' }, r.machine || '?') });
  return cols;
}
function sessionsTable(id, ws) {
  let rows = ws;
  if (FILTER) {
    const q = FILTER.toLowerCase();
    rows = ws.filter(w => (w.title + ' ' + w.project + ' ' + w.id).toLowerCase().includes(q));
  }
  return h('div', null, filterInput(),
    table(id, sessionCols(), rows, { defaultSort: { key: 'cost', desc: true }, collapse: 25,
      onRow: r => { go('s', r.id); } }));
}
/* the Overview's Top-sessions pane: the sessions table without the filter box,
   collapsed to the biggest few (the TUI's "# Top Sessions" section) */
function topSessionsTable(id, ws, n) {
  return table(id, sessionCols(), ws, { defaultSort: { key: 'cost', desc: true }, collapse: n,
    onRow: r => { go('s', r.id); } });
}
function sourcesTable(id, ws, onRow) {
  const rows = sourceRows(ws);
  const peak = Math.max(...rows.map(r => r.cost), 0);
  return table(id, [
    { key: 'source', label: 'Harness', asc: true },
    { key: 'sessions', label: 'Sessions', align: 'r' },
    { key: 'cost', label: 'Cost', align: 'r', fmt: r => barCell(r.cost, peak) },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
  ], rows, { defaultSort: { key: 'cost', desc: true }, onRow: onRow || null });
}
function machinesTable(id, ws) {
  // The per-scope Machines breakdown (fleet view), the sourcesTable twin: read-only, like
  // the web's Harnesses tab. The Machines MODE (#/M/<box>) is where a box drills in.
  const rows = machineRows(ws);
  const peak = Math.max(...rows.map(r => r.cost), 0);
  return table(id, [
    { key: 'machine', label: 'Machine', asc: true, fmt: r => [(MMETA[r.machine] || {}).live ? '● ' : '○ ', r.machine] },
    { key: 'sessions', label: 'Sessions', align: 'r' },
    { key: 'cost', label: 'Cost', align: 'r', fmt: r => barCell(r.cost, peak) },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
  ], rows, { defaultSort: { key: 'cost', desc: true }, onRow: r => { go('M', r.machine); } });
}

/* One rule, four views (util.CONTEXT_COMPACT_*): a >40% drop from OVER 50k of context is a
   clear, not just a smaller prompt. The Turns markers and the Context curve's ▼ both
   read it here, as their TUI twins read it from util -- two tabs on one page disagreeing
   about whether the window was cleared would be worse than not marking it at all. */
const isCompaction = (before, after) => before > 50000 && after < before * 0.6;
// {index into turns: [before, after]} for every main-thread turn whose context collapsed.
// Subagents run in their own windows (server ships their ctx as 0), so they neither
// trigger a marker nor break the main thread's chain.
function turnCompactions(turns) {
  const out = new Map();
  let prev = 0;
  turns.forEach((t, i) => {
    const v = t.ctx || 0;
    if (v <= 0) return;
    if (isCompaction(prev, v)) out.set(i, [prev, v]);
    prev = v;
  });
  return out;
}

/* turns stay chronological on purpose: the tab answers *when* the money went. */
function turnsTable(turns) {
  // Folded to prompts by default (the TUI's Turns fold): one ▸ row per user prompt with
  // its subtotal, the per-turn rows (and the full prompt text) hidden until you click the
  // header. A clean overview of every prompt without the per-turn noise.
  const rows = [];
  let cum = 0, lastPrompt = null, body = null, marker = null;
  const groups = new Map();
  for (const t of turns) {
    const key = t.promptId || '';
    groups.set(key, (groups.get(key) || 0) + mCost(t));
  }
  const comps = turnCompactions(turns);
  const freed = [...comps.values()].reduce((a, [b, af]) => a + b - af, 0);
  turns.forEach((t, i) => {
    const key = t.promptId || '';
    if (key !== lastPrompt) {
      lastPrompt = key;
      const title = (t.promptTitle || '(prompt)').slice(0, 160) + ((t.promptTitle || '').length > 160 ? '…' : '');
      const full = (t.promptFull || t.promptTitle || '').trim();
      body = [];   // this group's collapsible rows (full text + turns), hidden by default
      marker = h('span', null, '▸ ');
      const grp = body, mk = marker;   // capture for the toggle closure
      rows.push(h('tr', { class: 'prompt-row rowlink', title: full || null,
        onclick: () => { const open = !grp.length || grp[0].hidden;
          grp.forEach(r => r.hidden = !open); mk.textContent = open ? '▾ ' : '▸ '; } },
        h('td', { colspan: 3 }, marker, title),
        h('td', { class: 'r' }, moneyCell(groups.get(key))),
        h('td', null, ''), h('td', null, '')));
      if (full) {
        const fr = h('tr', { class: 'turn-fold', hidden: '' },
          h('td', { colspan: 6 }, h('div', { class: 'prompt-full' }, full)));
        body.push(fr); rows.push(fr);
      }
    }
    const c = comps.get(i);
    if (c)
      // NOT pushed into `body`: a compaction is a session-level event, and this table is
      // folded to prompts by default -- hiding the marker inside a collapsed group would
      // be hiding it outright (the TUI's detail_turns makes the same call).
      rows.push(h('tr', { class: 'compact-row' }, h('td', { colspan: 6 },
        '▼ context compacted before turn ' + (i + 1) + ' · ' + t.time.slice(5, 16).replace('T', ' ')
        + ' — ' + hTok(c[0]) + ' → ' + hTok(c[1]) + ' (~' + hTok(c[0] - c[1]) + ' freed)')));
    cum += mCost(t);
    const tr = h('tr', { class: 'turn-fold', hidden: '' },
      h('td', { class: 'dim' }, t.time.slice(5, 19).replace('T', ' ')),
      h('td', { class: 'indent' }, t.depth ? '↳ ' + t.agent : t.agent),
      h('td', { class: 'grow' }, modelCell(t.model)),
      h('td', { class: 'r' }, moneyCell(mCost(t))),
      h('td', { class: 'r' }, hTok(t.tokens)),
      h('td', { class: 'r dim' }, money(cum)));
    body.push(tr); rows.push(tr);
  });
  return h('div', null,
    h('div', { class: 'hint' }, groups.size + ' prompts — click a ▸ row to expand its turns'
      + (comps.size ? ' · ▼ ' + comps.size + ' compaction' + (comps.size > 1 ? 's' : '') + ', ~' + hTok(freed) + ' of context freed' : '')),
    h('div', { class: 'scroll' }, h('table', null,
      h('thead', null, h('tr', null, h('th', null, 'Time'), h('th', null, 'Agent'), h('th', null, 'Model'),
        h('th', { class: 'r' }, 'Cost'), h('th', { class: 'r' }, 'Tokens'), h('th', { class: 'r' }, 'Cumulative'))),
      h('tbody', null, rows))));
}
function toolsTable(toolRows) {
  const agg = new Map();
  for (const r of toolRows) {
    let a = agg.get(r.tool);
    if (!a) { a = { tool: r.tool, ns: r.ns, calls: 0, real: 0, api: 0, tokens: 0 }; agg.set(r.tool, a); }
    a.calls += r.calls || 0; a.real += r.real; a.api += r.api; a.tokens += r.tokens;
  }
  const rows = [...agg.values()];
  const peak = Math.max(...rows.map(mCost), 0);
  const grid = table('t-s-tools', [
    { key: 'tool', label: 'Tool', asc: true, cls: 'grow' },
    { key: 'ns', label: 'Server', asc: true, fmt: r => h('span', { class: 'dim' }, r.ns) },
    // The treemap shades by $/call, so the exact reading below it has to be able to
    // state that figure too -- the TUI's Tools table has carried Calls all along.
    { key: 'calls', label: 'Calls', align: 'r' },
    { key: 'cost', label: 'Cost', align: 'r', sortVal: mCost, fmt: r => barCell(mCost(r), peak) },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
  ], rows, { defaultSort: { key: 'cost', desc: true },
    totals: { tool: 'TOTAL', calls: sum(rows, r => r.calls),
      cost: moneyCell(sum(rows, mCost)), tokens: hTok(sum(rows, r => r.tokens)) } });
  return h('div', null, toolTreemap(rows), h('div', { class: 'tool-table' }, grid),
    h('div', { class: 'hint' }, 'cost and tokens belong to the LLM turns that invoked each tool; '
      + 'a multi-tool turn is split evenly'));
}

// Balanced binary treemap. It recursively halves the sorted weight along the current
// rectangle's long edge: enough structure for a convincing map without pulling a chart
// library into the self-contained page.
function binaryTreemap(items, x, y, w, h, out) {
  if (!items.length || w <= 0 || h <= 0) return out;
  if (items.length === 1) { out.push({ ...items[0], x, y, w, h }); return out; }
  const total = sum(items, r => r.value), half = total / 2;
  let acc = 0, split = 1, best = Infinity;
  for (let i = 1; i < items.length; i++) {
    acc += items[i - 1].value;
    const d = Math.abs(acc - half);
    if (d < best) { best = d; split = i; }
  }
  const left = items.slice(0, split), right = items.slice(split);
  const share = sum(left, r => r.value) / total;
  if (w >= h) {
    const cut = w * share;
    binaryTreemap(left, x, y, cut, h, out);
    binaryTreemap(right, x + cut, y, w - cut, h, out);
  } else {
    const cut = h * share;
    binaryTreemap(left, x, y, w, cut, out);
    binaryTreemap(right, x, y + cut, w, h - cut, out);
  }
  return out;
}
function toolTreemap(rows) {
  const dollars = sum(rows, mCost) > 0;
  const sortItems = (a, b) => b.value - a.value || (a.tool < b.tool ? -1 : a.tool > b.tool ? 1 : 0);
  const all = rows.map(r => ({ tool: r.tool, calls: r.calls || 0, value: dollars ? mCost(r) : r.tokens }))
    .filter(r => r.value > 0)
    .sort(sortItems);
  if (!all.length) return null;
  const total = sum(all, r => r.value), peak = all[0].value;
  // A tile too narrow to hold a name is a stripe, not a tile, and a row of those at the
  // right edge is most of what made this chart read as big and empty. The tail folds into
  // "Other" until every drawn tile can carry its own label -- measured against the REAL
  // container in draw(), since a percentage cannot know whether that is 40px or 240px.
  // (Renderer._TOOL_TILE_MIN, in pixels.) The long tail is read in the table below.
  const TILE_MIN = 70;
  const fold = keep => {
    const head = all.slice(0, keep), rest = all.slice(keep);
    if (!rest.length) return head;
    const out = head.concat([{ tool: 'Other', calls: sum(rest, r => r.calls),
      value: sum(rest, r => r.value) }]);
    out.sort(sortItems); // the folded tail can itself be the largest tile
    return out;
  };
  // Shade is the PER-CALL rate, not the area's own measure (Renderer._tool_treemap_box
  // is canonical): area already says what a tool cost in total, so colouring by the same
  // number spends the second channel saying it twice. $/call splits the two findings a
  // Cost column cannot tell apart -- "expensive because it ran 200 times" (big, cool)
  // from "expensive every single time" (small, hot).
  //
  // The SCALE comes off the FULL ranking, not the drawn tiles: whether per-call rates
  // exist and vary is a property of the data, so a tool keeps its colour when a resize
  // folds a neighbour away, and the caption below can be written before we measure. A
  // folded "Other" blends the rates it swallowed, which lands inside the range anyway.
  const byRate = all.every(r => r.calls > 0)
    && new Set(all.map(r => r.value / r.calls)).size > 1;
  const rates = all.map(r => r.value / r.calls);
  const rLo = byRate ? Math.min(...rates) : 0, rHi = byRate ? Math.max(...rates) : 0;
  // Log position, like Renderer._heat_position: per-call rates span orders of magnitude
  // and a linear ramp would flatten every tool but the priciest into one band.
  const level = r => {
    const n = TH.heat.length - 1;
    if (!byRate) return Math.max(0, Math.min(n, Math.round(Math.sqrt(r.value / peak) * n)));
    const v = r.value / r.calls;
    if (!(rHi > rLo && rLo > 0) || v <= rLo) return 0;
    return Math.max(0, Math.min(n, Math.round((Math.log(v) - Math.log(rLo)) / (Math.log(rHi) - Math.log(rLo)) * n)));
  };
  // money() floors at the cent, but a per-call rate usually lives below one and the whole
  // point of the figure is telling $0.0004 from $0.006 -- "<$0.01" for both erases it.
  const rateText = r => !r.calls ? '' : !dollars ? hTok(Math.round(r.value / r.calls)) + '/call'
    : (r.value / r.calls) >= 0.01 ? money(r.value / r.calls) + '/call'
    : (r.value / r.calls) < 0.0001 ? '<$0.0001/call'
    : '$' + (r.value / r.calls).toFixed(4).replace(/0+$/, '') + '/call';
  const map = h('div', { class: 'tool-map', 'aria-hidden': 'true' });
  // Measure after insertion: choosing split axes against a fake square makes wide panes
  // produce flat strips, and percentage thresholds cannot know whether a label has 40px
  // or 240px. ResizeObserver reflows this chart alone -- a global render on mobile-keyboard
  // resize would destroy expanded prompt rows and focused filters elsewhere on the page.
  let frame = 0;
  const draw = () => {
    if (!map.isConnected) return;
    const box = map.getBoundingClientRect();
    // Only the TAIL folds -- the tiles that individually cannot hold a label. Asking
    // instead that every tile in the folded set clear the floor lets one small tool drag
    // away everything ranked below it: measured on real data (18 tools, an 884px map)
    // that rule left three tiles, which is a bar chart with extra steps.
    let keep = 0;
    while (keep < Math.min(8, all.length)
      && all[keep].value / total * box.width >= TILE_MIN) keep++;
    const items = fold(Math.max(1, keep));
    const rects = binaryTreemap(items, 0, 0, box.width, box.height, []);
    const tiles_ = rects.map(r => {
      const fill = TH.heat[level(r)], roomy = r.w >= 66 && r.h >= 30;
      const amount = (dollars ? money(r.value) : hTok(r.value)) + ' · ' + fPct(r.value / total);
      // Each line has its own pixel gate and drops on its own, so a tile too short for
      // the rate still names itself -- the TUI's per-row degradation, in pixels.
      const details = (r.w >= 110 && r.h >= 46) ? h('span', { class: 'tv' }, amount) : null;
      const rate = rateText(r);
      const rateEl = (rate && r.w >= 110 && r.h >= 64)
        ? h('span', { class: 'tr' }, rate + ' · ' + r.calls + ' call' + (r.calls === 1 ? '' : 's'))
        : null;
      return h('div', {
        class: 'tool-tile' + (roomy ? '' : ' tiny'),
        style: 'left:' + r.x + 'px;top:' + r.y + 'px;width:' + r.w + 'px;height:' + r.h
          + 'px;background:' + fill + ';color:' + inkOn(fill),
        title: r.tool + ' · ' + amount + (rate ? ' · ' + rate : ''),
      }, roomy ? h('span', { class: 'tn' }, r.tool) : null, details, rateEl);
    });
    map.replaceChildren(...tiles_);
  };
  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(draw);
    });
    map._resizeObserver = observer;
    requestAnimationFrame(() => { if (map.isConnected) observer.observe(map); });
  } else {
    requestAnimationFrame(draw);
  }
  const amount = dollars ? money(total) : hTok(total) + ' tokens';
  const area = dollars ? 'visible cost' : 'tokens · no recorded cost';
  const unit = byRate ? 'area = ' + area + ' · shade = ' + (dollars ? '$' : 'tokens') + '/call'
    : 'area + shade = ' + area;
  // The finding as a sentence -- the flamegraph's headline, for the same reason: it is
  // what survives a phone-width map, and what a passive chart otherwise makes the reader
  // derive. It reads the FULL ranking, so it can still name the tool the fold swallowed
  // into "Other" -- which matters most when that tool is the hot one, since pricey-per-
  // call tools are usually small by total and fold first. (Renderer's headline.)
  const top = all[0], ofWhat = dollars ? 'the spend' : 'the tokens';
  const line = [top.tool + ' is ' + fPct(top.value / total) + ' of ' + ofWhat
    + (top.calls ? ', over ' + top.calls + ' calls' : '')];
  if (byRate) {
    const hot = all.reduce((a, b) => (b.value / b.calls) > (a.value / a.calls) ? b : a);
    if (hot !== top) line.push('priciest per call is ' + hot.tool + ' at ' + rateText(hot)
      + ' — ' + Math.round((hot.value / hot.calls) / (top.value / top.calls)) + '× ' + top.tool + "'s");
    else line.push('and the priciest per call, at ' + rateText(hot));
  }
  return h('div', { class: 'tool-map-wrap' },
    h('div', { class: 'tool-map-head' }, h('b', null, 'Tool-attributed spend · ' + amount), h('span', null, unit)),
    h('div', { class: 'flame-head' }, line.join(' · ')),
    map);
}

/* ---------- the Context tab (the TUI's detail_context, in SVG) ---------- */
// The server ships measured per-turn prompt sizes (points) + the model's window
// + the estimated composition rows; peak/final/compactions derive here, through the
// same isCompaction() the Turns table marks with.
function ctxHeatColor(v, window) {
  const lvl = Math.max(0, Math.min(4, Math.floor((window > 0 ? v / window : 0) * 5)));
  return TH.priceHeat[lvl];
}
function ctxChart(ctx, vs, comps) {
  const VW = 920, VH = 210, padL = 52, padR = 12, padT = 18, padB = 22;
  const ymax = Math.max(...vs) || 1, n = vs.length;
  const iw = VW - padL - padR, ih = VH - padT - padB;
  const x = i => padL + (n > 1 ? i * iw / (n - 1) : iw / 2);
  const y = v => padT + (1 - v / ymax) * ih;
  const svg = s('svg', { viewBox: '0 0 ' + VW + ' ' + VH, class: 'chart', preserveAspectRatio: 'none', style: 'width:100%;height:210px' });
  // Fullness gradient, banded like the TUI's heat rows: hard stops wherever a
  // window-fifth boundary crosses the chart's y range.
  const defs = s('defs', null);
  const grad = s('linearGradient', { id: 'ctxheat', x1: 0, y1: 1, x2: 0, y2: 0 });
  let prev = 0;
  for (let i = 1; i <= 5; i++) {
    const off = Math.min(1, (ctx.window * i / 5) / ymax);
    grad.appendChild(s('stop', { offset: prev, 'stop-color': TH.priceHeat[i - 1] }));
    grad.appendChild(s('stop', { offset: off, 'stop-color': TH.priceHeat[i - 1] }));
    prev = off;
    if (off >= 1) break;
  }
  if (prev < 1) { grad.appendChild(s('stop', { offset: prev, 'stop-color': TH.priceHeat[4] })); grad.appendChild(s('stop', { offset: 1, 'stop-color': TH.priceHeat[4] })); }
  defs.appendChild(grad);
  svg.appendChild(defs);
  // gridlines + y labels at max and half
  [1, 0.5].forEach(f => {
    const gy = y(ymax * f);
    svg.appendChild(s('line', { x1: padL, y1: gy, x2: VW - padR, y2: gy, stroke: thc('line'), 'stroke-width': 1 }));
    svg.appendChild(s('text', { x: padL - 6, y: gy + 3, 'text-anchor': 'end', 'font-size': 10, fill: thc('mut'), text: hTok(Math.round(ymax * f)) }));
  });
  svg.appendChild(s('line', { x1: padL, y1: VH - padB, x2: VW - padR, y2: VH - padB, stroke: thc('axis'), 'stroke-width': 1 }));
  // the measured area, filled with the fullness gradient
  const pts = vs.map((v, i) => x(i) + ',' + y(v)).join(' ');
  svg.appendChild(s('polygon', { points: padL + ',' + (VH - padB) + ' ' + pts + ' ' + x(n - 1) + ',' + (VH - padB), fill: 'url(#ctxheat)', 'fill-opacity': 0.55 }));
  svg.appendChild(s('polyline', { points: pts, fill: 'none', stroke: 'url(#ctxheat)', 'stroke-width': 2 }));
  // ▼ compaction markers ride above their drop point
  comps.forEach(i => svg.appendChild(s('text', { x: x(i), y: padT - 6, 'text-anchor': 'middle', 'font-size': 11, fill: thc('accent'), text: '▼' })));
  svg.appendChild(s('text', { x: padL, y: VH - 8, 'font-size': 10, fill: thc('mut'), text: 'turn 1' }));
  svg.appendChild(s('text', { x: VW - padR, y: VH - 8, 'text-anchor': 'end', 'font-size': 10, fill: thc('mut'), text: String(n) }));
  return svg;
}
function contextPane(ctx) {
  if (!ctx || !ctx.points || !ctx.points.length)
    return h('div', { class: 'hint' }, 'no per-turn context usage recorded for this session');
  const vs = ctx.points.map(p => p.v);
  const fin = vs[vs.length - 1], peak = Math.max(...vs), start = vs[0];
  const peakAt = vs.indexOf(peak) + 1;
  const comps = [];
  for (let i = 1; i < vs.length; i++) if (isCompaction(vs[i - 1], vs[i])) comps.push(i);
  const freed = comps.reduce((a, i) => a + vs[i - 1] - vs[i], 0);
  // end is measured against the live (last) model's window, peak against the window the
  // PEAK TURN actually ran in -- the TUI's split (renderer.detail_context). Measuring
  // both against ctx.window printed an impossible 120% when a session peaked on a big
  // model and ended on a smaller one.
  const pctOf = (v, w) => Math.round(100 * v / (w || ctx.window)) + '%';
  const peakWindow = (ctx.points[vs.indexOf(peak)] || {}).w;
  const wrap = h('div', null);
  wrap.appendChild(tiles([
    ['window', hTok(ctx.window), ctx.model],
    ['end', hTok(fin) + ' · ' + pctOf(fin, ctx.window), 'of the window'],
    ['peak', hTok(peak) + ' · ' + pctOf(peak, peakWindow), 'at turn ' + peakAt + ' of ' + vs.length],
    ['session start', hTok(start), 'system prompt + tools + first prompt'],
    comps.length ? ['compactions', String(comps.length), '~' + hTok(freed) + ' freed'] : null,
  ].filter(Boolean)));
  wrap.appendChild(ctxChart(ctx, vs, comps));
  if (ctx.mixedWindows)
    wrap.appendChild(h('div', { class: 'hint' }, 'this session switched between models with different windows — the chart scales to the last one'));
  if (comps.length)
    wrap.appendChild(h('div', { class: 'hint' }, comps.map(i => '▼ turn ' + (i + 1) + ' · ' + (ctx.points[i].t || '') + ' — ' + hTok(vs[i - 1]) + ' → ' + hTok(vs[i])).join('    ')));
  wrap.appendChild(h('div', { class: 'hint' }, 'measured per-turn prompt tokens; green → red = window fullness; subagents excluded'));
  return wrap;
}
function contextCompTable(comp) {
  // Category rows with their kinds nested beneath, biggest first (the TUI tree).
  const byCat = new Map();
  for (const r of comp) {
    let c = byCat.get(r.cat);
    if (!c) { c = { cat: r.cat, count: 0, est: 0, kinds: [] }; byCat.set(r.cat, c); }
    c.count += r.count; c.est += r.est;
    if (r.kind) c.kinds.push(r);
  }
  const cats = [...byCat.values()].sort((a, b) => b.est - a.est);
  const total = cats.reduce((a, c) => a + c.est, 0) || 1;
  const rows = [];
  const cells = (label, count, est, dim) => h('tr', null,
    h('td', { class: 'grow' + (dim ? ' dim' : '') }, label),
    h('td', { class: 'r' + (dim ? ' dim' : '') }, count.toLocaleString('en-US') + '×'),
    h('td', { class: 'r' + (dim ? ' dim' : '') }, '~' + hTok(est)),
    h('td', { class: 'r' + (dim ? ' dim' : '') }, Math.round(100 * est / total) + '%'));
  for (const c of cats) {
    rows.push(cells(c.cat, c.count, c.est, false));
    c.kinds.sort((a, b) => b.est - a.est).forEach(k => rows.push(cells('· ' + k.kind, k.count, k.est, true)));
  }
  const wrap = h('div', null, h('table', null,
    h('thead', null, h('tr', null,
      h('th', null, 'Category'), h('th', { class: 'r' }, 'Count'),
      h('th', { class: 'r' }, '~Tokens'), h('th', { class: 'r' }, 'Share'))),
    h('tbody', null, rows)));
  wrap.appendChild(h('div', { class: 'hint' }, 'a ~chars/4 estimate of everything sent, compacted or not — the system prompt and tool schemas live only in the measured session-start baseline'));
  return wrap;
  return wrap;
}

/* ---------- the `w` what-if views: a session's tree + its Overview summary ---------- */
// A share with its direction glued on -- except when there is no share to sign: pct()
// answers '-' for a zero denominator (undefined), and '+-' is not a percentage.
const signedPct = (part, whole, sign) => { const s = pct(Math.abs(part), whole); return s === '-' ? s : sign + s; };
// The payoff table (the Subagents pane with a target armed): the WHOLE tree -- root row
// included, because the question is about the whole session and the root is the model the
// delegation was made from -- each node's cost beside what its tokens would have cost had
// the target produced them.
//
// Two columns, not three. The per-node What-if is exact (one model, one rate card, that
// node's own tokens); a per-node BASELINE is not (a node's label is its dominant model
// only), so there is no per-node Δ -- nothing honest to subtract from. The exact
// comparison lives at session level, in the TOTAL line (whatifTotals, per-model rows,
// both sides at list rates). The Cost column keeps its ordinary meaning everywhere --
// recorded spend, $-estimated where nothing was recorded -- which is exactly why it does
// not add up to the TOTAL, and says so.
function whatifTree(nodes, t) {
  const rates = WI_PRICE.get(t.target);
  const rows = nodes.map(n => Object.assign({}, n, { wi: whatifCost(n.tok, rates) }));
  const saved = t.actual - t.whatif;   // signed from the target's side: what it would save
  const tbl = table('t-s-whatif', [
    { key: 'title', label: 'Title', asc: true, cls: 'grow', fmt: r => [r.depth ? h('span', { class: 'mut' }, '└ '.padStart(r.depth * 2 + 2, ' ')) : null, r.title] },
    { key: 'date', label: 'Started', fmt: r => h('span', { class: 'dim' }, dt(r.date)) },
    { key: 'agent', label: 'Agent', asc: true, fmt: r => h('span', { class: 'dim' }, r.agent) },
    { key: 'model', label: 'Model', asc: true, fmt: r => modelCell(r.model) },
    { key: 'cost', label: 'Cost', align: 'r', sortVal: mCost, fmt: r => moneyCell(mCost(r)) },
    { key: 'wi', label: 'What-if', align: 'r', fmt: r => h('span', { class: 'm' }, money(r.wi)) },
    { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
  ], rows, { defaultSort: { key: 'date', desc: false } });
  // The What-if column normally sums to the counterfactual (same tokens, same rate). It
  // won't when a session's node rollup disagrees with its message-level totals -- rare,
  // and not this feature's doing, but an unexplained mismatch reads as a bug, so it is
  // named only on the sessions where it is actually true. (The TUI does the same.)
  // Which way it drifts is not fixed -- a node rollup can overshoot the message totals as
  // easily as undershoot them -- so say the direction, don't assume it.
  const wiColumn = rows.reduce((a, r) => a + r.wi, 0);
  const drift = Math.abs(wiColumn - t.whatif) > 0.01 ? (wiColumn > t.whatif ? 'more' : 'less') : '';
  return h('div', null, tbl,
    h('div', { class: 'wi-total' }, 'TOTAL (list rates)  your models ', (t.est ? '~' : '') + money(t.actual), ' → all at ' + t.target + ' ',
      h('b', null, money(t.whatif)), '   ', (saved >= 0 ? 'saved ' : 'cost '),
      h('span', { class: saved >= 0 ? 'wi-down' : 'wi-up' }, money(Math.abs(saved))),
      ' (' + pct(Math.abs(saved), t.actual) + ')'),
    h('div', { class: 'hint' }, 'both sides priced at list rates — the only apples-to-apples basis. The Cost column is what was actually recorded ($0 where a subscription recorded none), so it does not add up to these.'),
    h('div', { class: 'hint' }, 'no per-node Δ: a node can mix models, so its baseline isn’t computable — the exact comparison exists at session level, where the tokens are split per model.'),
    t.est ? h('div', { class: 'hint' }, '~ your models include one with no known list rate — its tokens are priced at a generic estimate, so the baseline is not a real list price.') : null,
    drift ? h('div', { class: 'hint' }, 'this session’s node totals disagree with its message totals, so the What-if column adds up to slightly ' + drift + ' than the TOTAL. The TOTAL is the exact one.') : null);
}
// The armed target's effect on THIS session, in three figures, on the Overview -- where
// it exists because the Subagents pane cannot answer for a session that delegated
// nothing: a solo session has no tree to table, and `w` would otherwise silently do
// nothing on it. The wording stays neutral (change, never "routing saved"): with no
// delegation there is no routing decision to credit. Same whatifTotals as the tree, so
// the two can't disagree.
// A two-arc donut: root cost (accent) vs subagents (good), the root share in the middle.
// The TUI's proportion bar as an SVG; only drawn when there is a real split to show.
function donut(root, sub) {
  const R = 30, SW = 11, SZ = 84, c = SZ / 2, C = 2 * Math.PI * R;
  const t = root + sub, rf = t > 0 ? root / t : 1;
  const arc = (frac, off, col) => s('circle', { cx: c, cy: c, r: R, fill: 'none', stroke: col,
    'stroke-width': SW, 'stroke-dasharray': (frac * C).toFixed(2) + ' ' + C.toFixed(2),
    'stroke-dashoffset': (-off * C).toFixed(2) });
  return s('svg', { class: 'donut', width: SZ, height: SZ, viewBox: '0 0 ' + SZ + ' ' + SZ,
      role: 'img', 'aria-label': 'root vs subagents cost split' },
    s('g', { transform: 'rotate(-90 ' + c + ' ' + c + ')' },
      s('circle', { cx: c, cy: c, r: R, fill: 'none', stroke: 'var(--line)', 'stroke-width': SW }),
      arc(rf, 0, thc('accent')),
      sub > 0 ? arc(1 - rf, rf, thc('good')) : null),
    s('text', { x: c, y: c - 1, 'text-anchor': 'middle', 'font-size': 15, 'font-weight': 700,
      fill: 'var(--ink)', text: pct(root, t) }),
    s('text', { x: c, y: c + 13, 'text-anchor': 'middle', 'font-size': 9, fill: 'var(--mut)', text: 'root' }));
}

// The Overview Money card (the TUI's Money card): the cost split + shape stats, a root-vs-
// subagents donut, and -- when a `w` target is armed -- the what-if comparison as accent
// rows below a rule. Both what-if sides are list rates (whatifTotals), so the recorded
// rows above and the comparison below never quote the same number for different things.
function moneyCard(w, wi) {
  const total = cost(w), root = rootCost(w), sub = Math.max(0, total - root);
  const rangeTotal = W.reduce((a, x) => a + cost(x), 0);
  const nModels = (DATA.models[w.id] || []).length;
  const stat = (k, v, mon) => [h('span', { class: 'ms-k' }, k),
    h('span', { class: 'ms-v' + (mon ? ' money' : '') }, v)];
  const stats = h('div', { class: 'money-stats' },
    stat('Root', money(root), true), stat('Subagents', money(sub), true), stat('Total', money(total), true),
    stat('Share of range', pct(total, rangeTotal)), stat('Tokens', hTok(w.tokens)),
    stat('Models · Subagents', (nModels || w.subagents ? nModels : '·') + ' · ' + w.subagents));
  const left = (sub > 0 && total > 0) ? h('div', null,
    h('div', { class: 'money-legend' }, h('span', null, h('i', { class: 'lg lg-root' }), 'Root'),
      h('span', null, h('i', { class: 'lg lg-sub' }), 'Subagents')),
    donut(root, sub)) : null;
  const kids = [h('div', { class: 'money' }, left, stats)];
  if (wi) {
    const sign = wi.delta >= 0 ? '+' : '-';
    kids.push(h('div', { class: 'wi-rows' },
      h('span', { class: 'wi-k' }, '★ Your models (list)'),
      h('span', { class: 'wi-v' }, (wi.est ? '~' : '') + money(wi.actual)),
      h('span', { class: 'wi-k' }, '★ All at ' + wi.target), h('span', { class: 'wi-v' }, money(wi.whatif)),
      h('span', { class: 'wi-k' }, '★ Change'),
      h('span', { class: 'wi-v ' + (wi.delta >= 0 ? 'wi-up' : 'wi-down') },
        sign + money(Math.abs(wi.delta)) + ' (' + signedPct(wi.delta, wi.actual, sign) + ')')));
    kids.push(h('div', { class: 'hint' }, 'both sides at list rates — recorded spend is unchanged, here and everywhere else.'));
    if (wi.est) kids.push(h('div', { class: 'hint' }, '~ a model in your mix has no known list rate — its tokens use a generic estimate.'));
  }
  return pane('Money card' + (wi ? ' · what-if ' + wi.target : ''), ...kids);
}

/* ---------- the `w` target picker (the TUI's draw_whatif_menu) ---------- */
// The picker's rows: the active tier -- the models you have actually used, most-used
// first, or (after Tab) the whole models.dev catalog, cheapest-for-your-mix first --
// narrowed by the live filter through the one shared rule (modelMatches -- id by
// word-anchored fuzzy match, route by substring). The P overlay's filter is the same
// call: two model lists asking the same question must not answer it differently.
function whatifRows() {
  return (WHATIF.cat ? whatifCatalog() : WI_MODELS).filter(m => {
    const i = m.model.lastIndexOf('/');
    const route = i < 0 ? '' : m.model.slice(0, i), bare = i < 0 ? m.model : m.model.slice(i + 1);
    return modelMatches(WHATIF.q, bare, route ? [route] : [], '');
  });
}
// The DOM cap, like the P catalog's: the full catalog would mean thousands of buttons
// per keystroke. Everything keys and clicks act on is the same capped list, so the
// cursor can never land on an unrendered row -- the filter is the navigation.
const WI_CAP = 400;
function whatifShown() { const r = whatifRows(); return r.length > WI_CAP ? r.slice(0, WI_CAP) : r; }
function whatifFlip() {   // Tab: your models <-> the whole catalog; the query survives
  if (WHATIF.cat ? !WI_MODELS.length : !whatifCatalog().length) return;   // no empty tier
  WHATIF.cat = !WHATIF.cat; WHATIF.i = 0; renderWhatif();
}
function toggleWhatif() {   // `w`: with a target armed, disarm it; otherwise open the picker
  if (WHATIF.model) { WHATIF.model = null; render(false); return; }
  openWhatif();
}
function armWhatif(model) { WHATIF.model = model; WHATIF.open = false; WHATIF.q = ''; render(false); }
function openWhatif() {
  if (!WI_MODELS.length && !whatifCatalog().length) return;   // nothing anywhere to arm
  WHATIF.open = true; WHATIF.q = '';   // each open starts from the full list...
  // ...on your own models -- unless there are none (straight to the catalog: having used
  // few models is exactly when you need more to compare against), or the armed target
  // lives only there (reopen on the armed row, whichever tier holds it).
  WHATIF.cat = !WI_MODELS.length ||
    (!!WHATIF.model && !WI_MODELS.some(m => m.model === WHATIF.model) &&
     whatifCatalog().some(m => m.model === WHATIF.model));
  const i = whatifShown().findIndex(m => m.model === WHATIF.model);
  WHATIF.i = i < 0 ? 0 : i;
  renderWhatif();
}
function closeWhatif() { WHATIF.open = false; renderWhatif(); }   // cancel: pricing unchanged
function renderWhatif() {
  const host = document.getElementById('whatifpick');
  if (!WHATIF.open) { host.hidden = true; host.textContent = ''; return; }
  host.hidden = false; host.textContent = '';
  const rows = whatifShown(), all = whatifRows();
  if (rows.length) WHATIF.i = ((WHATIF.i % rows.length) + rows.length) % rows.length;
  const list = rows.map((m, i) => h('button', {
    class: 'wi-row' + (i === WHATIF.i ? ' cur' : '') + (m.model === WHATIF.model ? ' on' : ''),
    onclick: () => armWhatif(m.model),
  }, h('span', { class: 'wi-n' }, (m.model === WHATIF.model ? '● ' : '○ ') + m.model),
     h('span', { class: 'wi-t' }, WHATIF.cat ? (m.approx ? '~' : '') + '$' + m.eff.toFixed(2) + '/M'
       : hTok(m.tokens))));
  if (all.length > rows.length)
    list.push(h('div', { class: 'hint' }, '… ' + (all.length - rows.length) + ' more — filter to narrow'));
  // Not autofocused: j/k/Enter drive the list straight away (the TUI's picker), `f` (or a
  // click) starts the filter. Typing re-renders, so the caret is restored afterwards.
  const filter = h('input', { id: 'wi-filter', type: 'search', placeholder: 'f filter…', value: WHATIF.q,
    oninput: e => { WHATIF.q = e.target.value; WHATIF.i = 0; renderWhatif();
      const el = document.getElementById('wi-filter');
      if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } } });
  filter.addEventListener('keydown', e => {   // the picker owns its keys while the input has focus
    if (e.key === 'Escape') { WHATIF.q = ''; WHATIF.i = 0; renderWhatif(); e.preventDefault(); }
    else if (e.key === 'Enter') { const r = whatifShown(); if (r.length) armWhatif(r[WHATIF.i % r.length].model); e.preventDefault(); }
    else if (e.key === 'Tab') { whatifFlip(); const el = document.getElementById('wi-filter'); if (el) el.focus(); e.preventDefault(); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { stepWhatif(e.key === 'ArrowDown' ? 1 : -1); e.preventDefault(); }
    e.stopPropagation();
  });
  // The tier switch: the two lists the picker can show, Tab (or a click) flips --
  // rendered as the P overlay's segmented view switcher (.pr-views), same look.
  const tier = h('div', { class: 'wi-tier' },
    h('div', { class: 'pr-views' },
      h('button', { class: WHATIF.cat ? null : 'on', onclick: () => { if (WHATIF.cat) whatifFlip(); } }, 'your models'),
      h('button', { class: WHATIF.cat ? 'on' : null, onclick: () => { if (!WHATIF.cat) whatifFlip(); } }, 'models.dev')),
    h('span', { class: 'tr-note' }, WHATIF.cat ? 'eff $/M at your mix — cheapest first' : 'tokens you ran through each'));
  const panel = h('div', { class: 'tr-panel rp-panel wi-panel' },
    h('div', { class: 'tr-head' }, h('h3', null, 'What-if model'), filter,
      h('button', { class: 'tr-close', onclick: closeWhatif }, 'esc ✕')),
    h('div', { class: 'pr-intro' }, 'Compare a session’s tree against one model’s list rates — ',
      h('b', null, '“what if this model had done all of it?”'),
      ' The session’s Subagents tab and Overview reprice; every other view keeps its actual cost.'),
    tier,
    rows.length ? h('div', { class: 'wi-list' }, list)
      : h('div', { class: 'hint' }, 'no model matches — clear the filter to widen'),
    h('div', { class: 'tr-nav', style: 'margin-top:12px' },
      h('span', { class: 'tr-note' }, 'j/k move · h/l/Tab tier · f filter · Enter arms · w again clears it · esc cancels')));
  panel.addEventListener('click', e => e.stopPropagation());
  host.appendChild(panel);
}
function stepWhatif(dir) {
  const n = whatifShown().length;
  if (!n) return;
  WHATIF.i = ((WHATIF.i + dir) % n + n) % n;
  renderWhatif();
  const cur = document.querySelector('#whatifpick .wi-row.cur');
  if (cur) cur.scrollIntoView({ block: 'nearest' });
}

/* ---------- the detail pane ---------- */
function scopeLabel(sc) {
  if (sc.kind === 'y') return sc.year;
  if (sc.kind === 'm') return monthLabel(sc.month);
  if (sc.kind === 'd') return sc.day;
  if (sc.kind === 'p') return projName(sc.project);
  if (sc.kind === 'M') return sc.machine;
  if (sc.kind === 's') return sessionLabel(sc);
  return 'all time';
}
function sessionLabel(sc) {
  // The session's title, like the TUI's box border -- the raw id stays on the
  // Overview dl (and in the hash) for copy/paste.
  const t = ((sc.session && sc.session.title) || '').trim() || sc.id;
  return t.length > 60 ? t.slice(0, 59) + '…' : t;
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
  }
  if (sc.kind === 'y') {
    const months = monthRows(ws);
    if (months.length) root.appendChild(pane('Spend by month', barChart(months)));
  }
  if (sc.kind === 'p') {
    const months = monthRows(ws);
    if (months.length > 1) root.appendChild(pane('Spend by month', barChart(months)));
  }
  if (sc.kind === 'M') {
    // The niceties the plain rollup can't give: live vs pulled, when it was pulled and by
    // which opentab, plus a re-pull button under --serve (the TUI's F key).
    const meta = MMETA[sc.machine] || {};
    const dl = h('dl', { class: 'meta' },
      h('dt', null, 'status'), h('dd', null, meta.live ? '● live — full drill-in' : '○ pulled summary'));
    if (!meta.live && meta.exportedAt) {
      const age = relAge(meta.exportedAt);
      dl.append(h('dt', null, 'pulled'), h('dd', null, dt(meta.exportedAt) + (age ? ' (' + age + ')' : '')));
    }
    if (!meta.live && meta.version) dl.append(h('dt', null, 'opentab'), h('dd', null, meta.version));
    const card = h('section', { class: 'pane' }, h('h3', null, 'Machine · ' + sc.machine), dl);
    // No re-pull under demo: demo must make no network side effects (the server refuses
    // it too), so the button never appears on a shareable demo page.
    if (META.serve && meta.refreshable && !META.demo)
      card.appendChild(h('button', { class: 'hbtn', title: 're-pull this machine over ssh',
        onclick: () => refreshMachine(sc.machine) }, '↻ re-pull'));
    else if (!meta.live)
      card.appendChild(h('div', { class: 'hint' }, 'summary only — Turns/Tools/Context aren\'t exported (opentab --serve to re-pull)'));
    root.appendChild(card);
  }
  // Scoped to `ws`, so it answers for whatever the page is showing -- the drilled
  // year/month/day/project/machine, ignored projects already filtered out.
  const econ = tokenEconomicsPane(ws, scopeLabel(sc));
  if (econ) root.appendChild(econ);
  if (sc.kind !== 'p')
    root.appendChild(pane('Top projects', projectsTable('t-ov-projects', ws, 8, sc.kind === 'M' ? null : undefined)));
  root.appendChild(pane('Top sessions', topSessionsTable('t-ov-sessions', ws, 8)));
  // The models table CLOSES the Overview here as it does in the TUI (see _model_table):
  // it is the widest table on the page and the least likely answer to "where did the
  // money go", so the blocks that fit in a glance go above it. A day touches few models,
  // so its Overview carries the full mix -- the day scope has no Models tab (the TUI's
  // day_overview trade-off).
  root.appendChild(pane(sc.kind === 'd' ? 'Model mix' : 'Top models', modelsTable('t-ov-models', modelAgg(ws), 8)));
}
function renderSessionOverview(root, sc) {
  const w = sc.session;
  if (!w) { root.appendChild(pane(null, h('div', { class: 'hint' }, 'session not found: ' + sc.id))); return; }
  root.appendChild(h('h2', { class: 'title' }, w.title));
  root.appendChild(h('dl', { class: 'meta' },
    h('dt', null, 'project'), h('dd', null, h('a', { href: '#/p/' + encodeURIComponent(w.project) }, shortPath(w.project))),
    h('dt', null, 'date'), h('dd', null, dt(w.date)),
    w.dur != null ? [h('dt', null, 'worked'), h('dd', null, hDur(w.dur))] : null,
    META.combined && w.source ? [h('dt', null, 'harness'), h('dd', null, w.source)] : null,
    META.machines && w.machine ? [h('dt', null, 'machine'), h('dd', null, w.machine)] : null,
    h('dt', null, 'id'), h('dd', null, w.id)));
  // The Money card mirrors the TUI's: cost split + shape + a root/subagents
  // donut, and an armed `w` target answers for THIS session right here (a solo session
  // has no subagent tree for the Subagents tab, so its what-if lives here too).
  root.appendChild(moneyCard(w, whatifTotals(sc.id)));
  // Same pane as the scope Overviews, over this one session: the Money card says how
  // much and to which agent, this says which KIND of token the money went to.
  const econ = tokenEconomicsPane([w], 'this session');
  if (econ) root.appendChild(econ);
  if (EXTRAS.id === sc.id && EXTRAS.loading)
    root.appendChild(h('div', { class: 'hint' }, 'loading turns & tools…'));
  if (!META.serve)
    root.appendChild(h('div', { class: 'hint' }, 'the per-turn timeline, tool attribution and context curve are fetched live — run: opentab --serve'));
}
function renderDetail(sc, ws) {
  const root = document.getElementById('view');
  root.querySelectorAll('.tool-map').forEach(el => el._resizeObserver?.disconnect());
  root.textContent = '';
  // In the Machines scope, the Harnesses/Projects/Models tabs drill IN PLACE (MSUB) rather
  // than jumping to another scope, and the Sessions list reflects that sub-drill -- the
  // web twin of the TUI's Machines-mode zoom_source/zoom_project/zoom_model.
  const box = sc.kind === 'M';
  if (TAB === 'Overview') renderOverview(root, sc, ws);
  else if (TAB === 'Models') {
    const rows = sc.kind === 's' ? (DATA.models[sc.id] || []).map(r => ({ ...r })) : modelAgg(ws);
    root.appendChild(pane('Models · ' + scopeLabel(sc),
      modelsTable('t-tab-models', rows, undefined, box ? (r => setMsub('model', r.model)) : null)));
  } else if (TAB === 'Projects') root.appendChild(pane('Projects · ' + scopeLabel(sc),
    projectsTable('t-tab-projects', ws, undefined, box ? (r => setMsub('project', r.project)) : undefined)));
  else if (TAB === 'Sessions') root.appendChild(pane('Sessions · ' + scopeLabel(sc),
    sessionsTable('t-tab-sessions', box ? msubFilter(ws) : ws)));
  else if (TAB === 'Harnesses') root.appendChild(pane('Harnesses · ' + scopeLabel(sc),
    sourcesTable('t-tab-sources', ws, box ? (r => setMsub('source', r.source)) : null)));
  else if (TAB === 'Machines') root.appendChild(pane('Machines · ' + scopeLabel(sc), machinesTable('t-tab-machines', ws)));
  else if (TAB === 'Subagents') {
    // Nodes ride along only for a session that delegated. A solo session says "no
    // subagents" even with a target armed -- it has no tree to table, which is exactly
    // why its what-if lives on the Overview instead (the TUI makes the same split). A
    // session with no per-model rows has no computable baseline (whatifTotals is null),
    // so it keeps the ordinary tree rather than quoting half a comparison.
    const nodes = DATA.nodes[sc.id];
    const tree = nodes && nodes.some(n => n.depth > 0);
    const wi = tree ? whatifTotals(sc.id) : null;
    // The flamegraph rides ABOVE the tree on both variants: it answers "what share" where
    // the table answers "which node, how much", and it reads recorded/estimated spend
    // either way, so an armed target leaves it alone (the TUI splits it the same way).
    if (tree) { const fl = flamePane(nodes); if (fl) root.appendChild(fl); }
    if (!tree) root.appendChild(pane('Session tree', h('div', { class: 'hint' }, 'no subagents in this session')));
    else if (wi) root.appendChild(pane('Session tree · what-if ' + wi.target, whatifTree(nodes, wi)));
    else root.appendChild(pane('Session tree', table('t-s-nodes', [
      { key: 'title', label: 'Title', asc: true, cls: 'grow', fmt: r => [r.depth ? h('span', { class: 'mut' }, '└ '.padStart(r.depth * 2 + 2, ' ')) : null, r.title] },
      { key: 'date', label: 'Started', fmt: r => h('span', { class: 'dim' }, dt(r.date)) },
      { key: 'agent', label: 'Agent', asc: true, fmt: r => h('span', { class: 'dim' }, r.agent) },
      { key: 'model', label: 'Model', asc: true, fmt: r => modelCell(r.model) },
      { key: 'cost', label: 'Cost', align: 'r', sortVal: mCost, fmt: r => moneyCell(mCost(r)) },
      { key: 'tokens', label: 'Tokens', align: 'r', fmt: r => hTok(r.tokens) },
    ], nodes)));
  } else if (TAB === 'Turns') root.appendChild(pane('Turns · cost over time',
    EXTRAS.loading ? h('div', { class: 'hint' }, 'loading turns…') : turnsTable(EXTRAS.turns)));
  else if (TAB === 'Tools') root.appendChild(pane('Tools',
    EXTRAS.loading ? h('div', { class: 'hint' }, 'loading tools…') : toolsTable(EXTRAS.tools)));
  else if (TAB === 'Context') {
    root.appendChild(pane('Context · window usage',
      EXTRAS.loading ? h('div', { class: 'hint' }, 'loading context…') : contextPane(EXTRAS.context)));
    const c = EXTRAS.context;
    if (!EXTRAS.loading && c && c.comp && c.comp.length) {
      const total = c.comp.reduce((a, r) => a + r.est, 0);
      root.appendChild(pane('What filled it — ~' + hTok(total) + ' of content sent', contextCompTable(c.comp)));
    }
  }
}

/* ---------- chrome ---------- */
function renderTabs(sc, tabs) {
  const bar = document.getElementById('tabbar');
  bar.textContent = '';
  const loading = sc.kind === 's' && EXTRAS.id === sc.id && EXTRAS.loading;
  tabs.forEach(t => {
    const ld = loading && (t === 'Turns' || t === 'Tools' || t === 'Context'); // placeholder while fetching
    const cls = (t === TAB ? 'on ' : '') + (ld ? 'ld' : '');
    bar.appendChild(h('button', { class: cls.trim() || null,
      onclick: () => { TAB = t; render(false); } }, t + (ld ? ' ⋯' : '')));
  });
  // No trailing scope label here: it lives in the breadcrumb, and keeping it would pull
  // the centered tab row off-center. Loading is already shown by the pulsing ⋯ tabs.
}
function renderCrumbs(sc) {
  const el = document.getElementById('crumbs');
  el.textContent = '';
  const items = [['all time', sc.kind === 'all' ? null : '#/']];
  if (sc.kind === 'p') items.push([projName(sc.project), null]);
  if (sc.kind === 'M') items.push(['machines', '#/'], [sc.machine, null]);
  // year hop in the chain only when there's more than one year (else it's noise)
  if (sc.year && distinctYears(W).length > 1) items.push([sc.year, sc.kind === 'y' ? null : '#/y/' + sc.year]);
  if (sc.month && sc.kind !== 'm') items.push([monthLabel(sc.month), '#/m/' + sc.month]);
  else if (sc.kind === 'm') items.push([monthLabel(sc.month), null]);
  if (sc.day && sc.kind === 's') items.push([sc.day, '#/d/' + sc.day]);
  else if (sc.kind === 'd') items.push([sc.day, null]);
  if (sc.kind === 's') items.push([sessionLabel(sc), null]);
  items.forEach(([label, href], i) => {
    if (i) el.appendChild(h('span', { class: 'sep' }, '/'));
    el.appendChild(href ? h('a', { href }, label) : h('span', { class: 'here' }, label));
  });
  // The Machines-scope sub-drill (MSUB): a clearable chip, visible from any tab.
  if (sc.kind === 'M' && MSUB) {
    const lab = { source: 'harness', project: 'project', model: 'model' }[MSUB.dim];
    const val = MSUB.dim === 'project' ? projName(MSUB.value) : MSUB.value;
    el.appendChild(h('span', { class: 'sep' }, '·'));
    el.appendChild(h('a', { href: '#', title: 'clear this drill',
      onclick: e => { e.preventDefault(); clearMsub(); } }, lab + ': ' + val + ' ✕'));
  }
}
function chrome() {
  const chips = document.getElementById('hchips');
  chips.textContent = '';
  chips.appendChild(h('span', { class: 'chip' }, 'harness ', h('b', null, META.source)));
  chips.appendChild(h('span', { class: 'chip click', title: 'Set range (R)', onclick: openRange }, 'range ', h('b', null, rangeLabel())));
  chips.appendChild(h('span', { class: 'chip' }, META.serve ? 'live · ' + META.generated : META.generated));
  if (META.demo) chips.appendChild(h('span', { class: 'chip demo' }, 'demo data'));
  // An armed what-if target gets a chip, not a header badge: the header's numbers aren't
  // counterfactual anywhere (the target only reprices a session's Subagents tab and its
  // Overview), so tagging them would call recorded money a what-if. This is the page's
  // twin of the TUI's lit `w model` footer key -- an honest "a target is set".
  if (WHATIF.model) chips.appendChild(h('span', { class: 'chip wi click', title: 'what-if target (w changes it, w again clears it)',
    onclick: openWhatif }, 'what-if ', h('b', null, WHATIF.model)));
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
  right.appendChild(h('button', { class: 'hbtn', title: 'Theme (C)', onclick: openTheme }, '◑ theme'));
  if (META.serve) right.appendChild(h('button', { class: 'hbtn', title: 're-read the data sources',
    onclick: () => fetch('/api/reload', { method: 'POST' }).then(() => location.reload()) }, '↻ refresh'));
  const hints = document.getElementById('hints');
  hints.textContent = '';
  [['j/k', 'move'], ['Tab', 'panel'], ['h/l', 'tabs'], ['Esc', 'back'], ['$', 'what-if'], ['w', 'what-if model'],
   META.machines ? ['t/p/m', 'time/proj/machines'] : ['p/t', 'projects/time'], ['T', 'trends'], ['P', 'prices'], ['C', 'theme'], ['R', 'range']]
    .forEach(([k, lbl]) => hints.append(h('kbd', null, k), ' ' + lbl + '   '));
  document.getElementById('stamp').textContent =
    'generated by OpenTab v' + META.version + ' · ' + META.range + ' · ' + META.generated
    + (META.demo ? ' · demo data (anonymized, rescaled)' : '');
}

/* ---------- session extras (the --serve drill-in fetch) ---------- */
function ensureExtras(sc) {
  if (sc.kind !== 's' || !META.serve || EXTRAS.id === sc.id) return;
  EXTRAS = { id: sc.id, loading: true, turns: [], tools: [], context: null };
  fetch('/api/session/' + encodeURIComponent(sc.id)).then(r => r.json()).then(x => {
    EXTRAS = { id: sc.id, loading: false, turns: x.turns || [], tools: x.tools || [], context: x.context || null };
    render(false);
  }).catch(err => {
    console.error('session extras failed:', err);
    EXTRAS = { id: sc.id, loading: false, turns: [], tools: [], context: null };
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
  const body = rows.map(r => h('tr', cfg.onRow ? { class: 'rowlink', onclick: () => cfg.onRow(r) } : null,
    h('td', { class: 'l' }, cfg.nameFmt ? cfg.nameFmt(r) : r.name),
    h('td', { class: 'bar' }, h('div', { class: 'hb' }, h('i', { style: '--w:' + (peak > 0 ? Math.max(2, Math.round(100 * r.cost / peak)) : 0) + '%' }))),
    h('td', null, moneyCell(r.cost)), h('td', { class: 'mut' }, pct(r.cost, total)),
    cfg.extra.map(c => h('td', { class: c.cls || null }, c.get(r)))));
  return h('table', { class: 'rank' }, h('thead', null, head), h('tbody', null, body));
}
/* a ranked row's sessions (the TUI's Trends drill): every session in the active
   range that used the model / provider / source, most spend first, each row a
   deep link into the session itself */
function trendDrillRows() {
  const { kind, key } = TRENDS.drill, out = [];
  for (const w of W) {
    let c = 0, tok = 0;
    if (kind === 'source' || kind === 'machine') {
      const v = kind === 'machine' ? (w.machine || 'unknown') : (w.source || META.source);
      if (v !== key) continue;
      c = cost(w); tok = w.tokens;
    } else {
      for (const r of (DATA.models[w.id] || [])) {
        if (kind === 'model' ? r.model === key : (r.model.split('/')[0] || 'unknown') === key) { c += mCost(r); tok += r.tokens; }
      }
      if (!c && !tok) continue;
    }
    out.push({ id: w.id, date: w.date, title: w.title, cost: c, tokens: tok });
  }
  return out.sort((a, b) => b.cost - a.cost || b.tokens - a.tokens);
}
function trendDrill() {
  const { key } = TRENDS.drill;
  const rows = trendDrillRows();
  const back = h('button', { class: 'hbtn', onclick: () => { TRENDS.drill = null; renderTrends(); } }, '← back');
  if (!rows.length) return h('div', null, h('div', { class: 'tr-nav' }, back), h('div', { class: 'hint' }, 'No sessions used ' + key + ' in the active range.'));
  const total = rows.reduce((a, r) => a + r.cost, 0);
  const head = h('tr', null, h('th', { class: 'l' }, 'Started'), h('th', null, 'Cost'), h('th', null, 'Tokens'), h('th', { class: 'l' }, 'Title'));
  const body = rows.map(r => h('tr', { class: 'rowlink', onclick: () => { closeTrends(); go('s', r.id); } },
    h('td', { class: 'l mut' }, r.date.slice(0, 10)), h('td', null, moneyCell(r.cost)),
    h('td', { class: 'mut' }, hTok(r.tokens)), h('td', { class: 'l' }, r.title)));
  return h('div', null,
    h('div', { class: 'tr-nav' }, back, h('span', { class: 'lbl' }, 'Sessions · ' + key),
      h('span', { class: 'mut' }, rows.length + ' session(s) · ' + money(total) + ' · most spend first')),
    h('table', { class: 'rank' }, h('thead', null, head), h('tbody', null, body)));
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
    onRow: r => { TRENDS.drill = { kind: 'model', key: r.name }; renderTrends(); },
    extra: [{ label: 'Tokens', get: r => hTok(r.tokens), cls: 'mut' }, { label: 'Msgs', get: r => String(r.runs), cls: 'mut' }] });
}
function trendProviders() {
  const rows = providerAgg(W).map(r => ({ name: r.name, cost: MODE === 'api' ? r.api : r.real, runs: r.runs, tokens: r.tokens }))
    .filter(r => r.cost > 0 || r.tokens > 0).sort((a, b) => b.cost - a.cost || b.tokens - a.tokens);
  if (!rows.length) return h('div', { class: 'hint' }, 'No model usage in the active range.');
  return rankedBars(rows, { nameLabel: 'Provider',
    onRow: r => { TRENDS.drill = { kind: 'provider', key: r.name }; renderTrends(); },
    extra: [{ label: 'Tokens', get: r => hTok(r.tokens), cls: 'mut' }, { label: 'Msgs', get: r => String(r.runs), cls: 'mut' }] });
}
function trendSources() {
  const rows = sourceRows(W).map(r => ({ name: r.source, cost: r.cost, sessions: r.sessions, tokens: r.tokens }))
    .sort((a, b) => b.cost - a.cost || b.tokens - a.tokens);
  if (!rows.length) return h('div', { class: 'hint' }, 'No sessions in the active range.');
  return rankedBars(rows, { nameLabel: 'Harness',
    onRow: r => { TRENDS.drill = { kind: 'source', key: r.name }; renderTrends(); },
    extra: [{ label: 'Tokens', get: r => hTok(r.tokens), cls: 'mut' }, { label: 'Sess', get: r => String(r.sessions), cls: 'mut' }] });
}
function trendMachines() {
  const rows = machineRows(W).map(r => ({ name: r.machine, cost: r.cost, sessions: r.sessions, tokens: r.tokens }))
    .sort((a, b) => b.cost - a.cost || b.tokens - a.tokens);
  if (!rows.length) return h('div', { class: 'hint' }, 'No sessions in the active range.');
  return rankedBars(rows, { nameLabel: 'Machine',
    onRow: r => { TRENDS.drill = { kind: 'machine', key: r.name }; renderTrends(); },
    extra: [{ label: 'Tokens', get: r => hTok(r.tokens), cls: 'mut' }, { label: 'Sess', get: r => String(r.sessions), cls: 'mut' }] });
}
function openTrends() { TRENDS.open = true; TRENDS.drill = null; if (!TREND_TABS.includes(TRENDS.tab)) TRENDS.tab = 'Daily'; renderTrends(); }
function closeTrends() { TRENDS.open = false; TRENDS.drill = null; renderTrends(); }
function renderTrends() {
  const host = document.getElementById('trends');
  if (!TRENDS.open) { host.hidden = true; host.textContent = ''; return; }
  host.hidden = false; host.textContent = '';
  const tab = TRENDS.tab;
  const body = TRENDS.drill ? trendDrill()
    : ({ Daily: trendDaily, Weekly: trendWeekly, Monthly: trendMonthly, Calendar: trendCalendar,
        Models: trendModels, Providers: trendProviders, Harnesses: trendSources, Machines: trendMachines }[tab])();
  const footer = h('div', { class: 'tr-nav', style: 'margin-top:14px' });
  if (META.demo) footer.append(h('span', { class: 'tr-note' }, 'h/l tabs · j/k page · esc close'));
  else if (MODE === 'api') footer.append(h('span', { class: 'badge est' }, 'estimated · list prices'));
  else if (!META.recordsCost) footer.append(h('span', { class: 'tr-note' }, 'press $ to estimate subscription/credit usage at API list prices'));
  else footer.append(h('span', { class: 'tr-note' }, 'h/l tabs · j/k page · $ what-if · esc close'));
  const panel = h('div', { class: 'tr-panel' },
    h('div', { class: 'tr-head' },
      h('h3', null, 'Trends · ' + rangeLabel()),
      h('div', { class: 'tr-tabs' }, TREND_TABS.map(t => h('button', { class: t === tab ? 'on' : null,
        onclick: () => { TRENDS.tab = t; TRENDS.drill = null; renderTrends(); } }, t))),
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
// Canonical model id, mirroring pricing.canonical_model: date/effort suffixes
// stripped, version dots == dashes. Pins key by it, so one pin covers every
// spelling and every route that resells the model.
const canonId = m => m.toLowerCase().replace(/-(\d{8}|\d{4}-\d{2}-\d{2})$/, '')
  .replace(/-(minimal|low|medium|high|xhigh)$/, '').replace(/(\d)\.(?=\d)/g, '$1-');
// Pinned models: the browser keeps its own set in localStorage, seeded from the
// TUI's state.json pins the first time (DATA.prices.pinned). Pins are ROW-scoped
// "route/canon" keys -- pinning one gateway's catalog row must not light up the
// 20 other resellers of the same model; an aggregated flat/vendor row pins the
// routes it covers (the ones you actually use).
let PIN_SET = null;
function pins() {
  if (!PIN_SET) {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem('opentab-pins') || 'null'); } catch (e) { /* file:// may block storage */ }
    PIN_SET = new Set(Array.isArray(saved) ? saved : (DATA.prices.pinned || []));
  }
  return PIN_SET;
}
const pinKeys = r => (r.routes.length ? r.routes : ['']).map(rt => rt ? rt + '/' + canonId(r.model) : canonId(r.model));
function togglePin(r) {
  const p = pins(), ks = pinKeys(r);
  if (ks.every(k => p.has(k))) ks.forEach(k => p.delete(k)); else ks.forEach(k => p.add(k));
  try { localStorage.setItem('opentab-pins', JSON.stringify([...p])); } catch (e) { /* ditto */ }
}
// The models.dev catalog travels slim ({m, r, p, u?, s?}); eff and the ~ approx flag
// are pure functions of price + your mix, so they expand client-side, once, lazily.
function catalogRows() {
  if (!DATA.prices.catalogRows) {
    const mix = DATA.prices.mix || [1, 0, 0, 0];
    DATA.prices.catalogRows = (DATA.prices.catalog || []).map(c => {
      const [ir, orr, cr, cw] = c.p;
      const approx = cr <= 0 && ir > 0;
      const eff = mix[0] * ir + mix[1] * orr + mix[2] * (approx ? ir : cr) + mix[3] * cw;
      return { model: c.m, familyLabel: '', routes: [c.r], spend: 0, share: c.u || 0, price: c.p, eff, approx, status: c.s || '' };
    });
  }
  return DATA.prices.catalogRows;
}
const priceRows = () => PRICES.view === 'provider' ? DATA.prices.byRoute
  : PRICES.view === 'all' ? catalogRows() : DATA.prices.byModel;
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
  // The one shared rule (modelMatches, the mirror of pricing.model_matches): the model id
  // by word-anchored fuzzy match ("opus8" narrows to the claude-opus-4-8 rows,
  // dots==dashes), the route and the vendor label by substring. The `w` picker's filter
  // is the same call. Bare-subsequencing either field -- what this filter used to do --
  // made "gpt" match every Claude model sold through github-copilot, and "opus" match
  // half the catalog (qwen3-cOder-PlUS).
  if (PRICES.q) rows = rows.filter(r => modelMatches(PRICES.q, r.model, r.routes, r.familyLabel));
  const ASC = new Set(['model', 'eff']);  // natural order per column (else high→low)
  const key = PRICES.sort;
  const val = { model: r => r.model.toLowerCase(), eff: r => r.eff, use: r => r.share,
    input: r => r.price[0], output: r => r.price[1], cache_read: r => r.price[2], cache_write: r => r.price[3] }[key];
  const flip = PRICES.desc ? -1 : 1;
  if (PRICES.view === 'flat' || PRICES.view === 'all') {  // the catalog is a flat leaderboard too
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
  // The catalog view holds ~4.6k rows; rendering them all would lag every filter
  // keystroke, so cap the DOM and say what was cut (never a silent truncation).
  // Pinned models float first in every view (the ★ shortlist stays in sight
  // above the ~5k-row catalog), keeping the active sort within each block.
  const isPinned = r => pinKeys(r).some(k => pins().has(k));
  rows = rows.filter(isPinned).concat(rows.filter(r => !isPinned(r)));
  const CAP = 500;
  const cut = Math.max(0, rows.length - CAP);
  const shown = cut ? rows.slice(0, CAP) : rows;
  shown.forEach(r => {
    const pinnedRow = isPinned(r);
    if (pinnedRow) {
      if (lastGrp !== '★') { lastGrp = '★'; body.push(h('tr', { class: 'grp' }, h('td', { colspan: 7 }, '▸ ★ pinned'))); }
    } else if (PRICES.view === 'family' || PRICES.view === 'provider') {
      const g = PRICES.view === 'family' ? (r.familyLabel || 'Other') : (r.routes[0] || '(direct)');
      if (g !== lastGrp) { lastGrp = g; body.push(h('tr', { class: 'grp' }, h('td', { colspan: 7 }, '▸ ' + g))); }
    }
    const [ir, orr, crr, cwr] = r.price;
    const crCell = crr <= 0 && ir > 0 ? h('td', { class: 'mut' }, '—') : heatTd(crr, ranges[3], crr.toFixed(2));
    let tag = PRICES.view === 'provider' ? r.familyLabel : (r.routes.length ? r.routes.join(' ') : '');
    if (PRICES.view === 'all' && r.status) tag = tag ? tag + ' · ' + r.status : r.status;
    body.push(h('tr', null,
      h('td', { class: 'l' },
        h('span', { class: 'pin' + (pinnedRow ? ' on' : ''), title: pinnedRow ? 'unpin' : 'pin',
          onclick: ev => { ev.stopPropagation(); togglePin(r); renderPrices(); } }, pinnedRow ? '★' : '☆'),
        modelCell(r.model), tag ? h('span', { class: 'tag' }, tag) : null),
      heatTd(r.eff, ranges[0], (r.approx ? '~' : '') + '$' + r.eff.toFixed(2)),
      h('td', null, r.share > 0 ? h('span', { class: 'pr-use' }, pct(r.share, 1),
        h('span', { class: 'hb' }, h('i', { style: '--w:' + (usePeak > 0 ? Math.round(100 * r.share / usePeak) : 0) + '%' }))) : null),
      heatTd(ir, ranges[1], ir.toFixed(2)),
      heatTd(orr, ranges[2], orr.toFixed(2)),
      crCell,
      heatTd(cwr, ranges[4], cwr.toFixed(2))));
  });
  if (cut) body.push(h('tr', { class: 'grp' }, h('td', { colspan: 7, class: 'mut' }, '… ' + cut.toLocaleString() + ' more — filter or sort to narrow')));
  const panel = h('div', { class: 'tr-panel' },
    h('div', { class: 'tr-head' },
      h('h3', null, 'Model prices'),
      h('div', { class: 'pr-views' }, PRICE_VIEWS.map(([v, label]) => h('button', { class: v === PRICES.view ? 'on' : null,
        onclick: () => { PRICES.view = v; renderPrices(); } }, label))),
      h('input', { id: 'pr-filter', type: 'search', placeholder: 'f filter…', value: PRICES.q,
        oninput: e => { PRICES.q = e.target.value; renderPrices(); const el = document.getElementById('pr-filter');
          if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } } }),
      h('button', { class: 'tr-close', onclick: closePrices }, 'esc ✕')),
    priceIntro(),
    rows.length ? h('div', { class: 'scroll' }, h('table', { class: 'prices' }, h('thead', null, h('tr', null, th)), h('tbody', null, body)))
      : h('div', { class: 'hint' }, PRICES.q ? 'No models match the filter.'
        : 'No priced models on record (local-only usage, or no models.dev rates). ' + (META.demo ? '' : 'Run opentab --refresh-models to price open models.')),
    h('div', { class: 'tr-nav', style: 'margin-top:12px' }, h('span', { class: 'tr-note' }, 'cheapest-for-your-mix first · click a header to sort · p cycles view · f filters · esc close')));
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
  if (BROWSE === 'machines') return ['machines'];
  return distinctYears(W).length > 1 ? ['years', 'months', 'days'] : ['months', 'days'];
}
function sidebarList(sc) {
  if (BROWSE === 'machines') {
    const rows = machineRows(W).sort((a, b) =>
      ((MMETA[b.machine] || {}).live ? 1 : 0) - ((MMETA[a.machine] || {}).live ? 1 : 0) || b.cost - a.cost);
    return { rows: [{ go: () => go('', '') }, ...rows.map(r => ({ go: () => go('M', r.machine) }))],
      index: sc.kind === 'M' ? 1 + rows.findIndex(r => r.machine === sc.machine) : 0 };
  }
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
  // Overlay stacking order = DOM order (theme picker above the w picker above prices
  // above trends), so the keyboard checks run top-down: whatever floats highest owns the
  // keys. Mirrors the TUI, where handle_key checks theme_menu, then whatif_menu, then the
  // overlay branches.
  if (THEMEPICK) { if (e.key === 'Escape' || e.key === 'C') closeTheme(); e.preventDefault(); return; }
  // The `w` target picker: j/k move, Enter arms, `f` starts the filter, Tab flips to
  // the models.dev catalog and back, Esc/q cancels (pricing unchanged). `w` advances
  // the highlight like j, exactly as in the TUI.
  if (WHATIF.open) {
    const rows = whatifShown();
    if (e.key === 'Escape' || e.key === 'q') closeWhatif();
    else if (e.key === 'j' || e.key === 'ArrowDown' || e.key === 'w') stepWhatif(1);
    else if (e.key === 'k' || e.key === 'ArrowUp') stepWhatif(-1);
    else if (e.key === 'Tab' || e.key === 'h' || e.key === 'l' ||
             e.key === 'ArrowLeft' || e.key === 'ArrowRight') whatifFlip();  // tabs: h/l, like everywhere
    else if (e.key === 'Enter') { if (rows.length) armWhatif(rows[WHATIF.i % rows.length].model); }
    else if (e.key === 'f' || e.key === '/') { const el = document.getElementById('wi-filter'); if (el) el.focus(); }
    else if (e.key === 'C') openTheme();
    e.preventDefault(); return;
  }
  if (PRICES.open) {
    if (e.key === 'Escape' || e.key === 'P') closePrices();
    else if (e.key === 'p') { const i = PRICE_VIEWS.findIndex(v => v[0] === PRICES.view); PRICES.view = PRICE_VIEWS[(i + 1) % PRICE_VIEWS.length][0]; renderPrices(); }
    else if (e.key === 'f' || e.key === '/') { const el = document.getElementById('pr-filter'); if (el) el.focus(); }
    else if (e.key === 'C') openTheme();
    else if (e.key === '$' && !META.demo) { MODE = MODE === 'api' ? 'real' : 'api'; render(false); }
    e.preventDefault(); return;
  }
  // While the Trends overlay is open it owns the keyboard (its own tab/page keys).
  if (TRENDS.open) {
    // Esc first backs out of a ranked row's sessions drill, then closes; h/l
    // switch tabs even from inside a drill (leaving it) -- mirrors the TUI.
    if (e.key === 'Escape') { if (TRENDS.drill) { TRENDS.drill = null; renderTrends(); } else closeTrends(); e.preventDefault(); }
    else if (e.key === 'T') { closeTrends(); e.preventDefault(); }
    else if (e.key === 'h' || e.key === 'ArrowLeft' || e.key === 'l' || e.key === 'ArrowRight') {
      const i = TREND_TABS.indexOf(TRENDS.tab), step = (e.key === 'h' || e.key === 'ArrowLeft') ? -1 : 1;
      TRENDS.tab = TREND_TABS[(i + step + TREND_TABS.length) % TREND_TABS.length]; TRENDS.drill = null; renderTrends(); e.preventDefault();
    } else if (e.key === 'j' || e.key === 'ArrowDown' || e.key === 'k' || e.key === 'ArrowUp') {
      const unit = { Daily: 'month', Weekly: 'week', Calendar: 'year' }[TRENDS.tab];
      if (unit) { stepTrend(unit, (e.key === 'j' || e.key === 'ArrowDown') ? 1 : -1); e.preventDefault(); }
    } else if (e.key === '$' && !META.demo) { MODE = MODE === 'api' ? 'real' : 'api'; render(false); e.preventDefault(); }
    else if (e.key === 'P') { openPrices(); e.preventDefault(); }
    else if (e.key === 'C') { openTheme(); e.preventDefault(); }
    return;
  }
  if (RANGE.pick) { if (e.key === 'Escape') closeRange(); e.preventDefault(); return; }
  const sc = curScope();
  const tabs = tabsFor(sc);
  if (e.key === 'T') {
    openTrends();
  } else if (e.key === 'P') {
    openPrices();
  } else if (e.key === 'C') {
    openTheme();
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
    else if (sc.kind === 'y' || sc.kind === 'p' || sc.kind === 'M') go('', '');
  } else if (e.key === '$' && !META.demo) {
    MODE = MODE === 'api' ? 'real' : 'api';
    render(false);
  } else if (e.key === 'w') {
    // Arm a target model, or clear the armed one. Allowed in demo (unlike $): demo
    // already scales every token by a hidden per-process factor, so list rates on scaled
    // tokens recover no real dollars, while the ratio the feature exists to show stays
    // real.
    toggleWhatif();
  } else if (e.key === 'p' && BROWSE !== 'projects') {
    setBrowse('projects');
  } else if (e.key === 't' && BROWSE !== 'time') {
    setBrowse('time');
  } else if (e.key === 'm' && META.machines && BROWSE !== 'machines') {
    setBrowse('machines');
  }
});

/* ---------- render ---------- */
function render(scrollTop = true) {
  const sc = curScope();
  if (sc.kind === 'p') BROWSE = 'projects';
  else if (sc.kind === 'M') BROWSE = 'machines';
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
  renderWhatif();
  renderTheme();
  if (scrollTop) window.scrollTo(0, 0);
}
document.getElementById('trends').addEventListener('click', closeTrends);  // click the backdrop to close
document.getElementById('prices').addEventListener('click', closePrices);
document.getElementById('rangepick').addEventListener('click', closeRange);
document.getElementById('whatifpick').addEventListener('click', closeWhatif);
document.getElementById('themepick').addEventListener('click', closeTheme);
// Navigation resets the scoped table state, but keeps the active tab when it
// still exists in the new scope (render() falls back to Overview otherwise) --
// so month->month on the Sessions tab stays on Sessions.
window.addEventListener('hashchange', () => { FILTER = ''; EXPANDED.clear(); MSUB = null; render(); });
// Theme precedence: the viewer's saved choice, else the page's baked-in default
// (--theme / meta), else tokyo-night. Applied before the first paint so charts pick it up.
applyTheme((function () { try { return localStorage.getItem('opentab-theme'); } catch (e) { return null; } })() || META.theme || 'tokyo-night');
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
