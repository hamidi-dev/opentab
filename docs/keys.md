# Keys & navigation

OpenTab opens on a stacked **Months / Days** (or Projects) sidebar, lazygit-style:
drill from a month or day into its detail tabs, from the Sessions tab into a single
session — cost split, model mix, subagent tree — and step back out with `Esc`.

Press **`?`** in the app for the cheat sheet: a small panel that floats over what you
were looking at and lists, lazygit-style, the keys that work **where you are** (this
view, this tab, this overlay — Trends, the price table and a model's session list each
have their own), then Navigation, then the Global keys. One short line per key; the long
form is *this* page. The footer strip reads the same table, so it can only ever offer a
key that does something here — and `j/k` in Trends says whether it is paging a month or
walking a list, because there it depends on the tab.

## How the views nest

Three levels: **browse** → **zoom** → **session**. `Enter` (or `+`) drills in, `Esc`
steps back out. Zoom is not full-screen: the detail pane takes focus *beside* the
sidebar, which stays clickable to re-scope in place; `+` maximizes/restores the
detail pane (remembered between runs). The session view is full-screen.

Detail tabs per scope: years/months get Overview · Models · Projects · Sessions;
days drop Models. Drilling a row of the **Models** tab replaces them with that model's
own two: **Economics** (what it cost here, split by token type) and **Sessions** (the
sessions that used it, with the cost and tokens *it* accounts for) — see
[Scope & filter](#scope--filter). A session adds **Turns** (per-turn cost over time, every harness
that records per-step usage), **Tools** (per-tool / MCP spend) and **Context** (the
context window's growth curve, % of the model's window, compaction markers, and —
on harnesses whose logs carry content — an estimated breakdown of what filled it)
when its harness supports them, and **Harnesses** joins in the merged `all` view.
The Context tab also overlays how the session evolved: what it spent (with a
per-turn and per-hour burn rate), its wall-clock span with clock times pinned to
the chart's edges, and the clock time — and how far into the session — of each
compaction.

## Move around

| Key | Action |
|-----|--------|
| `p` / `t` / `m` | Switch to the Projects / Time / Machines browse mode — Machines opens on `∑ all machines` (the whole fleet as one scope), then one row per box (just this one until you [`pull`](../README.md#fleet) another) |
| `Tab` / `Shift-Tab` | Cycle focus Years → Months → Days (Time mode); Shift-Tab at the top steps back out |
| `1` / `2` / `3` / `0` | Jump straight to a panel — **each panel wears its number in its title**, lazygit-style: the sidebar top to bottom (`[1] Years`, `[2] Months`, `[3] Days`; in Projects mode `[1] Projects`) and `[0]` the detail pane on the right, what `Enter` drills into. A digit jumps from anywhere: it steps out of a zoomed detail or an open session to get there |
| `Enter` | Drill into the selection; on Turns, open a prompt, then its selected turn |
| `+` | Focus the detail pane from browse; maximize / restore it in zoom or session |
| `Esc` | Step back out — turn → prompt → session → zoom → browse; returning from a turn keeps the selected row visible |
| `h` / `l` | Switch detail tabs |
| `j` / `k` | Move in the list (`↑`/`↓` too), or scroll the detail pane; on the Turns tab, move the `▸` prompt cursor |
| `PgDn` / `PgUp` | Half a page (`Ctrl-D` / `Ctrl-U` too) |
| `g` / `G` | Jump to the top / bottom |
| `[` / `]` | Inside a turn, read the previous / next turn of the same prompt |
| `z` | Inside a turn, expand its full recorded content / collapse to the preview |
| Mouse | Wheel scrolls · click selects (anywhere in the preview pane focuses it) · double-click drills · click a tab, or a column header to sort (again to reverse) |

On the Turns tab, `j`/`k` select a prompt and `Enter` (or a click) opens its full
text and per-turn rows; `g`/`G` jump to the first/last prompt. Inside a prompt,
`j`/`k` select a turn and `Enter` opens its recorded content. The Content column
shows text, thinking and tool names where space allows. Inside a turn, `j`/`k`
scroll while `[`/`]` step between turns. The prompt and turn identity stay visible
above the transcript. `z` reads the selected turn's full arguments, reasoning and
output from the local records; a second `z` returns to the capped preview.
Expansion is temporary and is released when you leave the turn. Recorded tool
errors are labeled explicitly. Sources that do not support content have no turn
detail; content is also unavailable in demo mode and remote summaries.
A `▼` line marks each **context compaction** — where the window was cleared
between two turns, with what it dropped from and to. The tab's title counts them
and the tokens they freed, and the lines stay visible while the prompts are
folded: a compaction is a session-level event, not a turn. The Context tab charts
the same events (one rule, both tabs) — and where that tab doesn't apply, because
the harness records cumulative-total deltas rather than per-request prompts
(Codex), neither tab marks anything.

## Scope & filter

| Key | Action |
|-----|--------|
| `R` | Set the date range — `all` · `30d` (or `30`) · `2m` · `1y` · `2026` · `2026-05` · `start..end` |
| `a` | Back to all time, keeping the current selection where possible |
| `s` | Sort picker for the visible list (`j`/`k` move · `Enter` · `Esc`). Sessions offer **Start Date** (`created_at`, default) and, everywhere except the Time overview's **Days** pane, **Last Activity** (`ended_at`, including subagent activity where tracked) — a single day's list is read by start time, and activity can run into a later day than the one the row is filed under, so ranking by it there is deliberately left out — that pane falls back to **Start Date**, keeping your choice for when you focus Months/Years again. The Date column follows whichever is active, and its header shows "Last act" under the latter. Projects offer the matching pair: **Recency** (the newest session's *start*) and **Last Activity** (the newest activity in any of the project's sessions, subagents included) |
| `f` or `/` | Live filter — fuzzy (fzf-style) over sessions (title/project/id/**note**) and projects; model lists (`P`, `w`) match word-anchored (letters may scatter inside a word, a new word only joins at its first letter — `opus48` works, `opus` no longer drags in `qwen3-c`**`o`**`der-`**`p`**`l`**`us`**), routes by substring. Non-ASCII (`ä`, `界`) can be typed. While filtering: `↑`/`↓` select · `Enter` keep · `Esc` cancel · `Ctrl-U` clear |
| `x` | Clear the filter |

### Drilling a model

`Enter` on a row of the **Models** tab drills into that model *within the scope you're
in* — the month, the year, the project, the box — which is the one thing Trends' and
`P`'s model drills can't do (both are app-wide). It opens two tabs of its own:

- **Economics** — how many sessions and messages used it here, its tokens, and its
  spend split by token type (uncached input / output / reasoning / cache read / cache
  write), the same chart every Overview carries.
- **Sessions** — the sessions that used it, but with the two metric columns re-pointed
  at the model: **Model list** and **Model tok** are what *this model* accounts for, not
  the session's whole bill. So the list ranks by how much of the spend was really this
  model's, and a cheap session that leaned on it sorts above an expensive one that
  barely touched it. `e` exports those columns alongside the session totals.

Both figures are **list rates** — no harness records recorded spend per model, so a
per-model figure can only ever be priced from the catalog (hence the column name, a `~`
where the rate is a guess, and no cost at all for a local model). `Esc` steps back to
the Models table you came from.

## Sessions & projects

Every session list carries a **Worked** column — how long the agent was *actually
working*, summing its bursts and dropping the idle gaps where it waited for your next
prompt (so a session you left open for hours shows minutes, not hours). It's derived
from the human turns the transcript logs, not a guessed timeout; blank when the
backend can't tell work from waiting (a source with no human-turn markers like Copilot
OTEL or VS Code, or an export from an older opentab). It's sortable like any column
(`s` picker or a header click), and a session's Overview spells it out: `Started: … ·
worked 2h 15m (until 14:15)`. The Context tab still has the richer wall-clock story
(burn rate, per-turn offsets).

| Key | Action |
|-----|--------|
| `i` / `I` | Ignore / unignore the selection; `I` reveals hidden rows so they can be unignored |
| `b` / `B` | Bookmark ★ the selected session (remembered between runs); `B` shows only bookmarks, within the active range |
| `n` | Note ✎ on the selected session — *why* it cost what it did, which no token count records. Opens a prompt seeded with the existing note (`Enter` saves · `Ctrl-U` clears · `Ctrl-W` kills a word · `Esc` cancels); saving an empty note removes it. An annotated session shows a `✎` in every list and the note in its **Overview**; `f`/`/` searches note text too, and `e` exports it as a `note` column. Notes live in their own `~/.local/share/opentab/notes.json` and are written the moment you save. Off under `--demo` / `--no-state` |
| `o` | Open the selected session's / project's directory |
| `L` | Launch the session in its own tool — `opencode --session` / `claude --resume` / `codex resume`. Then `w` window/tab · `s` right split · `v` lower split · `p` popup · `y` copy the command. tmux offers all spawn targets. Herdr offers a tab and both splits only when it provides a valid `HERDR_PANE_ID` for the current pane; otherwise it offers only the tab and copy. A [launcher hook](#custom-launchers) may offer all four. `y` copies anywhere. If tmux and Herdr are nested, OpenTab uses the innermost multiplexer. A session **pulled from another machine** reopens *there* only when its `remotes.json` entry has an SSH target: every available target wraps the command in `ssh -t <target> 'cd … && …'`, and `y` yanks that same line. A box reached by `url` (no SSH target) offers only the yank |
| `e` | Export the current list to a CSV in the working directory — whatever the pane is showing, including a model scope's attributed columns |

## Views & overlays

| Key | Action |
|-----|--------|
| `T` | Trends — Daily · Weekly · Monthly · Calendar · Models · Providers · Projects · Harnesses. `h`/`l` tabs · `j`/`k` page months/weeks/years. On the charts and Calendar: `Enter` focuses, arrows pick a bar/day, `Enter` drills in, `Esc` back. On the ranked tabs: `j`/`k` pick a row · `s` sorts its visible columns · `Enter` its sessions · `Enter` again opens one |
| `P` | Model prices — the table behind the `$` estimate; see [Pricing](pricing.md) for the views, sorting, and pinning |
| `$` | Toggle what-if prices — what unpriced usage would cost at API list rates |
| `w` | What-if **model** — arm one priced model as a comparison target (`j`/`k` move · `f` filter · `h`/`l` (or `Tab`, or a click) switch the tier tabs between the models **you've used** and the **whole models.dev catalog**, cheapest-for-your-mix first · `Enter` arm · `Esc` cancel): *"what if the expensive model had done the subagents' work too?"*. Used few models? The catalog tier is the point — it offers every model with a list price, and opens directly when nothing you've used is priceable. The selected session's **Subagents** tab then shows its whole tree (root included) with a **What-if** column — that node's tokens at the target's list rates — and a `TOTAL (list rates)  your models … → all at … …  saved …` line; its **Overview** carries the same session comparison (Your models / All at *target* / Change). **Both sides are priced at list rates** — the only apples-to-apples basis for a rate substitution — so a session that delegated nothing (no tree to show) still answers, and repricing a single-model session at the model it already used is exactly a $0 change. There is deliberately **no per-node Δ**: a node can mix models, so no honest per-node baseline exists; the exact comparison lives at session level, where the tokens are split per model. The Cost column keeps its ordinary meaning (recorded spend, `$`-estimated where nothing was recorded), so it does **not** add up to the TOTAL. A rate substitution, not a rerun. **Session-scoped** — the sessions list, the day/month/project rollups and Trends keep showing actual spend, and `$` keeps working as always. Works in demo too; `w` again clears it. The [web browser](web.md#w--the-what-if-model) mirrors all of it, on the same key |
| `H` | Harness picker (`j`/`k` move · `Enter` switch · `Esc` cancel) |
| `M` | Machine filter (fleet only) — narrow **every** view to one box; the harness picker's twin (`j`/`k` move · `Enter` arm/clear · `Esc` cancel) |
| `C` | Colour-theme picker — `j`/`k` live-preview · `Enter` keep · `Esc` revert (themes are shared with the web browser) |
| `D` | Demo (anonymize for a shareable screen) — opens a multi-check picker of what to scramble: **Titles** (session / prompt / model / machine names), **Paths** (project directories), **Turns** (the expandable full prompt text), **Spend** (dollars + token magnitudes). Paths are separate from titles because a project tree is often the one label a demo *wants* real — leave it unchecked to keep real project names on an otherwise anonymised screen. `j`/`k` move · `Space` toggle a category · `a` all/none · `Enter` apply · `Esc` cancel. **While demo is on, `D` switches it straight back off** (one press, no picker); the categories are remembered, so `D` again re-offers them. From the CLI: `--demo` (all) or `--demo titles,spend` |
| `r` / `q` / `?` | Reload the data · quit · help |

The global toggles stay live *inside* the overlays: `?`, `C`, `H`, `M` (fleet), and `D`
work from anywhere, Trends and Prices included.

## What persists between runs

The active **harness, range, sort, focused sidebar panel, ignored projects, bookmarks,
pinned price rows, theme, and `$` what-if view are remembered between runs**, stored in
`~/.local/state/opentab/state.json` (the XDG *state* dir — regenerable prefs). Pass
`--no-state` to disable; `--demo` never persists.

**Session notes (`n`) are kept apart**, in `~/.local/share/opentab/notes.json` (the XDG
*data* dir). Everything in `state.json` is a preference opentab can regenerate or shrug
off; a note is the one thing you wrote, so it gets its own file in the data dir, is saved
on the edit rather than at quit, and a note whose session has since disappeared (a
rotated transcript, a harness you didn't merge in this run) is **kept, never pruned**.

A `w` **what-if target model is deliberately not remembered**: it's a transient
analysis mode, and a persisted one would silently re-frame every future launch's
Subagents tab.

Two formatting rules worth knowing: sub-cent costs render as `<$0.01` so they aren't
confused with a red `$0.00`, which specifically means *unpriced* (tokens with no
local price); and git worktrees fold into their main repo (`--no-worktrees` keeps
them split).

## Remap any key

Every key above — and every key in every picker, overlay, pager, prompt and text
field — is remappable. The keymap lives at `~/.config/opentab/keymap.conf`, a fully
commented INI file installed on first run (also in the wheel as
`opentab/data/keymap.conf`; `opentab --keymap` prints the path). Press **`K`** inside
opentab to open it in `$EDITOR` (`$VISUAL` wins, `vi` as fallback): edit, save, quit,
and the new bindings are live the moment the editor returns — the footer chips, the
`?` cheat sheet, and every modal title re-label themselves from the file, so the UI
never advertises a key that isn't bound.

One line per action, first key shown in the UI, comma-separated aliases, empty value
unbinds:

```ini
[main]
# sort this list
sort = o
# step back out — session → zoom → browse
back = esc, backspace, h
# an empty value unbinds (comments are full-line only: # or ; at line start)
export =

[menu]
# one line re-teaches j/k in EVERY picker (sort, themes, launch, …)
down = n
up = e
```

Key syntax: a single character (case-sensitive — `S` is shift-s; non-ASCII like `ö`
works), named keys (`enter esc space tab shift-tab backspace delete insert up down
left right pgup pgdn home end f1`–`f12`, `comma` for a literal `,`), and control chords
(`ctrl-u` or `^u`, letters only). `Ctrl-C` is the hardwired panic quit and cannot be
rebound.

Contexts mirror what owns the keyboard: `[main]` for browse/zoom/session, `[trends]`
(+ `[trends.chart]` for a focused chart, `[trends.drill]` for a ranked row's session
list), `[prices]` (+ `[prices.sessions]`), `[help]`, `[notices]`, the shared `[menu]`
with per-picker overrides (`[menu.sort]`, `[menu.theme]`, `[menu.launch]`,
`[menu.whatif]`, …), `[filter]` for the live filter line, `[input]` for the note/range
prompts, and `[prompt.prices]`. A sub-context falls back to its family for anything it
doesn't name; anything the file doesn't name falls back to the built-in default — so
the file survives upgrades, and deleting a line (or the whole file) restores stock
behavior.

Typos never break the TUI: a bad key name, an unknown action, or two lines fighting
over one key each fall back sanely and land a precise warning as a toast (press `N`
for the list). Rebinding a key *takes it away* from whatever held it — bind `x` to
`down` and `x` no longer clears the filter (with a warning that `clear_filter` is now
unreachable, unless you bind or unbind it yourself).

## Custom launchers

If an executable exists at `~/.config/opentab/launcher` (or `$OPENTAB_LAUNCHER`
points at one), every `L`-menu launch is handed to it instead of the built-in
tmux commands — git-hooks style. It's called as

```sh
launcher <kind> <directory> <command>
# kind ∈ window | hsplit | vsplit | popup
# e.g. launcher window /repo/myproj 'claude --resume abc123'
```

and a nonzero exit shows its stderr as the launch error. The footer reads
"launch via launcher hook" when one is active.

For a session pulled from another machine the `<command>` is already the full
`ssh -t … 'cd … && …'` line and `<directory>` is your home — the hook runs it
locally, exactly as it runs a local one, and the `cd` happens on the far side.

**Example hook** — route launches through zellij (or kitty, or your own popup
manager):

```sh
#!/bin/sh
# ~/.config/opentab/launcher — example: zellij instead of tmux
kind=$1 dir=$2 cmd=$3
case $kind in
  window) exec zellij action new-tab --cwd "$dir" -- sh -c "$cmd" ;;
  popup)  exec zellij run --floating --cwd "$dir" -- sh -c "$cmd" ;;
  *)      exec zellij run --cwd "$dir" -- sh -c "$cmd" ;;
esac
```
