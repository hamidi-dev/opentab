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
days drop Models. A session adds **Turns** (per-turn cost over time, every harness
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
| `p` / `t` | Switch to the Projects / Time browse mode |
| `Tab` / `Shift-Tab` | Cycle focus Years → Months → Days (Time mode); Shift-Tab at the top steps back out |
| `1` / `2` / `3` / `0` | Jump straight to a panel — **each panel wears its number in its title**, lazygit-style: the sidebar top to bottom (`[1] Years`, `[2] Months`, `[3] Days`; in Projects mode `[1] Projects`) and `[0]` the detail pane on the right, what `Enter` drills into. A digit jumps from anywhere: it steps out of a zoomed detail or an open session to get there |
| `Enter` / `+` | Drill into the selection; on a Sessions / Projects / Harnesses tab, open it in this scope; on the Turns tab, fold/unfold the selected `▸` prompt |
| `Esc` | Step back out — session → zoom → browse |
| `h` / `l` | Switch detail tabs |
| `j` / `k` | Move in the list (`↑`/`↓` too), or scroll the detail pane; on the Turns tab, move the `▸` prompt cursor |
| `PgDn` / `PgUp` | Half a page (`Ctrl-D` / `Ctrl-U` too) |
| `g` / `G` | Jump to the top / bottom |
| Mouse | Wheel scrolls · click selects (anywhere in the preview pane focuses it) · double-click drills · click a tab, or a column header to sort (again to reverse) |

On the Turns tab, `j`/`k` move a cursor over the `▸` prompt headers and `Enter`
(or a click) folds/unfolds the selected one — its full prompt text and per-turn
rows; `g`/`G` jump to the first/last prompt. `z` unfolds every prompt at once.

## Scope & filter

| Key | Action |
|-----|--------|
| `R` | Set the date range — `all` · `30d` (or `30`) · `2m` · `1y` · `2026` · `2026-05` · `start..end` |
| `a` | Back to all time, keeping the current selection where possible |
| `s` | Sort picker for the visible list (`j`/`k` move · `Enter` · `Esc`) |
| `f` or `/` | Live filter — fuzzy (fzf-style) over sessions (title/project/id/**note**) and projects; model lists (`P`, `w`) match word-anchored (letters may scatter inside a word, a new word only joins at its first letter — `opus48` works, `opus` no longer drags in `qwen3-c`**`o`**`der-`**`p`**`l`**`us`**), routes by substring. Non-ASCII (`ä`, `界`) can be typed. While filtering: `↑`/`↓` select · `Enter` keep · `Esc` cancel · `Ctrl-U` clear |
| `x` | Clear the filter |

## Sessions & projects

| Key | Action |
|-----|--------|
| `i` / `I` | Ignore / unignore the selection; `I` reveals hidden rows so they can be unignored |
| `b` / `B` | Bookmark ★ the selected session (remembered between runs); `B` shows only bookmarks, within the active range |
| `n` | Note ✎ on the selected session — *why* it cost what it did, which no token count records. Opens a prompt seeded with the existing note (`Enter` saves · `Ctrl-U` clears · `Ctrl-W` kills a word · `Esc` cancels); saving an empty note removes it. An annotated session shows a `✎` in every list and the note in its **Overview**; `f`/`/` searches note text too, and `e` exports it as a `note` column. Notes live in their own `~/.config/opentab/notes.json` and are written the moment you save. Off under `--demo` / `--no-state` |
| `o` | Open the selected session's / project's directory |
| `L` | Launch the session in its own tool — `opencode --session` / `claude --resume` / `codex resume`. Then `w` window · `s` split · `v` vsplit · `p` popup · `y` copy the command (`w`/`s`/`v`/`p` need tmux or a [launcher hook](#custom-launchers); `y` copies anywhere) |
| `e` | Export the current list to a CSV in the working directory |

## Views & overlays

| Key | Action |
|-----|--------|
| `T` | Trends — Daily · Weekly · Monthly · Calendar · Models · Providers · Harnesses. `h`/`l` tabs · `j`/`k` page months/weeks/years. On the charts and Calendar: `Enter` focuses, arrows pick a bar/day, `Enter` drills in, `Esc` back. On Models/Providers/Harnesses: `j`/`k` pick a row · `Enter` its sessions · `Enter` again opens one |
| `P` | Model prices — the table behind the `$` estimate; see [Pricing](pricing.md) for the views, sorting, and pinning |
| `$` | Toggle what-if prices — what unpriced usage would cost at API list rates |
| `w` | What-if **model** — arm one priced model as a comparison target (`j`/`k` move · `f` filter · `h`/`l` (or `Tab`, or a click) switch the tier tabs between the models **you've used** and the **whole models.dev catalog**, cheapest-for-your-mix first · `Enter` arm · `Esc` cancel): *"what if the expensive model had done the subagents' work too?"*. Used few models? The catalog tier is the point — it offers every model with a list price, and opens directly when nothing you've used is priceable. The selected session's **Subagents** tab then shows its whole tree (root included) with a **What-if** column — that node's tokens at the target's list rates — and a `TOTAL (list rates)  your models … → all at … …  saved …` line; its **Overview** carries the same session comparison (Your models / All at *target* / Change). **Both sides are priced at list rates** — the only apples-to-apples basis for a rate substitution — so a session that delegated nothing (no tree to show) still answers, and repricing a single-model session at the model it already used is exactly a $0 change. There is deliberately **no per-node Δ**: a node can mix models, so no honest per-node baseline exists; the exact comparison lives at session level, where the tokens are split per model. The Cost column keeps its ordinary meaning (recorded spend, `$`-estimated where nothing was recorded), so it does **not** add up to the TOTAL. A rate substitution, not a rerun. **Session-scoped** — the sessions list, the day/month/project rollups and Trends keep showing actual spend, and `$` keeps working as always. Works in demo too; `w` again clears it. The [web browser](web.md#w--the-what-if-model) mirrors all of it, on the same key |
| `H` | Harness picker (`j`/`k` move · `Enter` switch · `Esc` cancel) |
| `M` | Machine filter (fleet only) — narrow **every** view to one box; the harness picker's twin (`j`/`k` move · `Enter` arm/clear · `Esc` cancel) |
| `C` | Colour-theme picker — `j`/`k` live-preview · `Enter` keep · `Esc` revert (themes are shared with the web browser) |
| `D` | Toggle real / demo data (demo anonymizes titles and paths) |
| `r` / `q` / `?` | Reload the data · quit · help |

The global toggles stay live *inside* the overlays: `?`, `C`, `H`, `M` (fleet), and `D`
work from anywhere, Trends and Prices included.

## What persists between runs

The active **harness, range, sort, ignored projects, bookmarks, pinned price rows,
theme, and `$` what-if view are remembered between runs**, stored in
`~/.config/opentab/state.json`. Pass `--no-state` to disable; `--demo` never
persists.

**Session notes (`n`) are kept apart**, in `~/.config/opentab/notes.json`. Everything
in `state.json` is a preference opentab can regenerate or shrug off; a note is the one
thing you wrote, so it gets its own file, is saved on the edit rather than at quit, and
a note whose session has since disappeared (a rotated transcript, a harness you didn't
merge in this run) is **kept, never pruned**.

A `w` **what-if target model is deliberately not remembered**: it's a transient
analysis mode, and a persisted one would silently re-frame every future launch's
Subagents tab.

Two formatting rules worth knowing: sub-cent costs render as `<$0.01` so they aren't
confused with a red `$0.00`, which specifically means *unpriced* (tokens with no
local price); and git worktrees fold into their main repo (`--no-worktrees` keeps
them split).

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
