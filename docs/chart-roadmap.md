# Chart roadmap

Ten proposed visualizations, ranked by insight-per-effort. Each one was prototyped
against real data (1,116 sessions, Oct 2025 – Jul 2026) before landing here, so the
"what it showed" notes are observed, not hypothetical.

Conventions for every entry:

- **Scope** — a chart answers for the *current* scope (the drilled year/month/day/
  project/machine, or one session), never a hardcoded range. The web computes it
  client-side off the embedded payload so drill-in and filtering stay free.
- **Both frontends unless noted.** Where curses can't carry a form, the entry says so
  and names the terminal-native substitute rather than shipping a degraded copy.
- **Colour budget (TUI).** Pairs 1–7 are base, 8–18 the calendar heat ramp, 20–24 the
  price heat ramp, 25 the tab chip, **26–30 the token-type categorical ramp** (chart 1),
  32 the background. 31 and 33+ are free. An 8-colour terminal is fine — six ANSI hues
  minus black/white — but a *pair-starved* one (`COLOR_PAIRS` small) makes `_set_pair`
  skip the block silently, so any multi-series chart needs a glyph fallback the way
  `_heat_ansi_ramp` does. Check the return value of `_set_pair`; don't assume.
- **Per-segment colour on one line** is available: `write_rich` paints a line once and
  then overpaints runs. Chart 1 generalised that into `_token_runs` (column runs keyed by
  line text) + `_paint_token_runs`, which sees through a ruled box's gutter. Reuse it.
  `_token_stack_line` is the shared 100%-bar builder (cumulative rounding, a one-cell
  floor per positive value, an optional `share_fmt`); `_stack_widths` is its geometry,
  split out so a caller can position things UNDER a bar without recomputing it (compute
  it twice and every label lands a cell to the left); `_token_legend_lines` is the shared
  colour key. Keys are the line TEXT, so two lines in one box must never render identical
  strings — `_legend_names` re-uniquifies after clipping for exactly that reason.

---

## 1. Token economics — where the money actually goes ✅ done

Per token type (input / output / reasoning / cache read / cache write): share of tokens
sent against share of dollars billed.

**The same chart in both frontends** — two 100%-stacked bars, one above the other over
the same five types, then the colour key, then the numbers, all in one frame. The reading
is the gap between the bars, which is why they share a scale and a colour per type.

- **Web** — a stat row, the two bars, the legend, and a fixed-order table. Deliberately
  *not* the sortable `table()`: its headers install click handlers unconditionally, so a
  click would re-rank the five rows away from cost order and keep that ranking for every
  later scope (one shared table id).
- **TUI** — the same sections inside one `_sectioned_box`. Per-segment colour on a single
  line works because `write_rich` already recolours runs after painting a line: the bars
  stash their column runs in `_token_runs` keyed by the line's TEXT (the box is spliced
  into six Overviews and cannot know its final indices), and `_paint_token_runs` looks
  through the box gutter and shifts by it.
- **Colour** — both frontends gained their first CATEGORICAL ramp (`TOKEN_SERIES_DARK` /
  `TOKEN_SERIES_LIGHT`, five slots each, validated as a set; identical hexes in
  `heatmap.py` and `webpage.py`). Every other ramp in the project is *sequential* --
  pressing one into categorical duty would say "more" where it means "different".
  - TUI pairs live at **26-30**. 8 colours was never the constraint: five distinct ANSI
    hues exist. A **pair-starved** terminal is -- `_set_pair` skips the block and every
    segment would render in the terminal default. There the glyphs (`█▓▒░▚`) carry
    identity, and the in-segment percentage is dropped, because the fill IS the identity.
  - Web labels on a fill pick their ink from that fill's luminance, not from the theme:
    one theme-wide choice is unreadable on the ramp's lighter slots.

- **Data** — the per-model breakdown rows (`model_mix`), split by `model_row_split` and
  priced with `api_equivalent_cost`'s own decomposition, so the pieces sum to the "$"
  figure the rest of the UI shows. Always at **list rates** (no backend attributes
  recorded spend per token type), like the `w` what-if baseline.
- **What it showed** — cache reads are 94.8% of tokens and 59.2% of spend; output is
  0.49% of tokens and 15.5% of spend. An output token costs 50× a cache-read token.
  Cache reads billed at the input rate instead would have been **+$30,928**.
- **Where** — Overview of every scope, plus the session Overview.
- **Effort** — medium. `App.token_economics` (shared) + a sectioned box with a paint
  side-channel (TUI) + a pane with its own bars (web).

## 2. Punchcard — hour of day × weekday

7×24 heat grid of spend. `created_at` already carries `HH:MM:SS`; nothing reads the time
half today.

- **Data** — `date[11:13]` + weekday. No payload change.
- **Where** — a Trends tab (TUI + web), scoped to the active range.
- **Effort** — very low in both. The TUI reuses `heat_level` / `heat_glyph` /
  `heat_palette` unchanged and it is *narrower* than the calendar (24 columns vs 53).
- **Note** — this is the cheapest remaining win; do it next.

## 3. Spend over time, stacked by model family

Monthly (or daily/weekly) spend split into model families, with a 100%-share toggle.

- **What it showed** — the share panel is the one that carries the story: Claude Fable
  went from nothing to ~half of monthly spend in a single month, which the dollar panel
  can't show because July dwarfs the nine months before it. Ship the share view.
- **Where** — Trends Daily/Weekly/Monthly, as a stacking mode on the existing charts.
- **Effort** — web medium (stacking in `trendChart`). **TUI: use per-month horizontal
  100% rows, not vertical stacks** — with `bar_w ≤ 4` and ~10 plot rows a 5-way vertical
  stack gets 2 rows per segment and the eighth-block precision at the boundaries is gone.

## 4. Worked time × cost scatter — one dot per session

Log-log, dot per session, coloured by harness, sized by tokens, clickable.

- **What it showed** — a tight band (cost tracks working time), which makes the
  departures the story. **It also surfaced a likely bug:** 21 sessions have >10h of
  "worked" time, up to 16 days, one with $74 against it (≈$0.19/active hour). Suspected
  cause: resumed Claude Code sessions log no human turn on resume, so the multi-day gap
  never gets marked idle in `formatting.worked_seconds`. **Fix that before shipping the
  chart.**
- **Where** — web only (a Trends tab).
- **TUI substitute** — a sortable `$/hour` column in the sessions table. Braille would
  give the resolution but a braille cell holds one colour, so the harness encoding
  collapses and there is no hover to recover the session title.
- **Effort** — web medium.

## 5. Concentration / Pareto curve

Sessions sorted most-expensive first, cumulative share of spend.

- **What it showed** — the top **3%** of sessions are half the bill (33 of 1,116).
- **Where** — web: a Trends tab. **TUI: ship the sentence, not the curve** — a stat line
  on the Overview carries the entire insight.
- **Effort** — very low (one sorted reduce).

## 6. Session flamegraph (icicle) ✅ done

The subagent tree as a spend hierarchy: width = dollars. Shipped as **"Where the money
went"** — one band partitioning the session into the root's own work and each subagent —
above the tree table on the Subagents tab, in both frontends, plus the finding as a
sentence (`root kept 42% ($37.62) · 5 subagents split $52.00 · biggest explore 19%`).

- **It is one band, and that is not a shortcut.** `workflow_nodes` gives a node a depth
  but **no parent**, so a depth-2 node cannot be placed under the depth-1 node it
  actually ran below. Rather than draw a nesting the stores don't record, deeper nodes
  join the same band as siblings, marked `↳` and named in a note. Measured on the
  corpus this costs nothing: **exactly one session of 1,117 nests past one level, and it
  spent $0**. `SessionFlame` is shaped so parent links would make it a real N-level
  icicle without the chart changing.
- **An unlabelled "whole session" bar above the band was tried and cut.** It was meant to
  be the denominator the band divides — the second level that makes an icicle an icicle.
  Nobody read it that way (the first question it got was "is that the what-if view?"),
  and it said nothing the caption's right-aligned total doesn't. With one real level of
  hierarchy there is no containment to draw, so don't re-add it.
- **Names go UNDER the band, not into the fill.** Text punched through a colour fights
  it, and it only ever fits the segments that least needed a label. Each name starts at
  its own segment's column, in its own segment's colour (`_flame_label_line` over
  `_stack_widths`), so position does the pointing; a name wider than its slice is dropped
  rather than shifted, because a label over the wrong slice is worse than none. Only the
  share still rides inside the fill. The key then carries **only** what position could
  not — and disappears entirely when every segment is named.
- **A segment names the AGENT and its MODEL, never the session title.** A title is a
  sentence that never fits and is one column away in the table below. The agent column is
  populated for only 15% of real subagent nodes; OpenCode leaves the name in the *title*
  as `(@code-reviewer)` for most of the rest, and mining it back out (`_FLAME_AGENT_TAG`)
  takes that to **85%**, recovering exactly the names the column holds when it is set
  (`explore`, `code-reviewer`, `general`, `homelab`, `org`, `debugger`). Claude Code names
  no Task, so the honest answer there is `subagent`. The model is its short display
  spelling, and gets **its own positioned row only when the segments disagree** — 85 of
  135 delegating sessions run one model end to end, and there it goes in the caption once
  (`· all on claude-opus-4-8`), which is what buys the other 50 the room for a per-segment
  row.
- **Two names per execution.** `agent` is bare and repeats (five slices reading
  `code-reviewer` is the truth when position disambiguates them); `label` is what
  `App._flame_labels` had to add to tell them apart where there is no position — the start
  clock, then seconds, then a cost rank, then a guaranteed-unique pass.
- **Width is the Cost column's own number** (`App._priced_nodes`), so the chart and the
  table under it can never disagree about a node — same `$` gating, same estimate rule.
  A subscription session with `$` off has no dollars to divide, so the unit falls back to
  **tokens** and the caption says so.
- **Colour** — no new ramp and no new pairs: the categorical `TOKEN_SERIES` (26–30) from
  chart 1. Slot 0 is reserved for the root; children cycle 1–4, so no subagent can wear
  the root's colour — root-vs-delegated is the one distinction the chart makes.
- **What it showed** — delegation is rarer and shallower than it feels: 135 of 1,117
  sessions delegate at all, **70 of those to exactly one subagent**, and the **median
  root share is 83%** — the icicle's most common reading is "you barely delegated". The
  $89.62 session that motivated the entry is real (root 42%, five subagents splitting the
  rest almost evenly), and it is the exception.
- **Where** — the Subagents tab, above the tree table, on both variants: an armed `w`
  target does not touch it (the chart is recorded/estimated spend, the tree's TOTAL is
  the counterfactual).
- **Watch out** — `detail_subagents` registers its click-sortable header by **absolute
  line index**, so the chart is passed IN as the tab's head, never prepended afterwards.

## 7. Burn-up with a pace projection

Cumulative spend this month vs last month, dashed projection to month end.

- **Where** — Overview (month scope) or a Trends tab.
- **Effort** — web low. **TUI degraded**: two lines can't share a cell, so draw this
  month as a filled eighth-block area, last month as a dimmed baseline, and put the
  projection in text (`on pace ≈ $4,420`).

## 8. Model efficiency bubbles

Per model: tokens run through (log x) vs effective $/M (y), bubble area = spend.

- **Data** — `prices.byModel[].eff` is already computed.
- **Where** — web only, on the Prices overlay.
- **TUI substitute** — the Prices overlay's `eff $/M` column, sorted. Already exists.
- **Effort** — web low.

## 9. Turn trace — per-turn cost over the context curve

Two panels sharing one x axis: cost per turn above, context window used below.

- **Axis is the turn number, not the clock.** Sessions run in bursts; a wall-clock axis
  spends most of its width drawing the gaps.
- **Never a dual-axis chart** — dollars and tokens don't belong on one y scale.
- **What it showed** — the three $3–5 turns did *not* line up with the fullest window;
  expensive turns are their own event, not the tail of a context that grew too big.
- **Where** — the Turns tab (which is titled "cost over time" and currently draws a
  table), `--serve` only on the web.
- **Effort** — medium, and the TUI is over half done: the context curve at
  `renderer.py:3316` already buckets per turn; add a 3–4 row panel above it reusing the
  same `bucket()`, column count and x axis.

## 10. ~~Tool spend treemap~~ — done

Tool-attributed spend as area, one rectangle per shown tool with the exact tool/server
table directly below. The active theme's heat ramp shades the rectangles.

- **Where** — both Tools tabs (the web still requires live `--serve` session extras).
- **Shipped** — passive balanced-binary layout in both frontends; seven tools plus
  `Other`; area follows the live Cost column and falls back to tokens when every
  recorded cost is `$0`.

---

## Suggested order

1. ~~Token economics~~ (done)
2. ~~Session flamegraph~~ (done)
3. Turn trace — half-built in the TUI already, and the next one that *enriches an
   existing tab*: the Turns tab is titled "cost over time" and draws a table
4. ~~Tool spend treemap~~ (done)
5. Punchcard — cheapest remaining, lands in both frontends
6. Concentration (TUI stat + web curve)
7. Stacked share over time
8. Burn-up
9. Scatter — **after** the `worked_seconds` resume bug is fixed
10. Bubbles — web-only polish
