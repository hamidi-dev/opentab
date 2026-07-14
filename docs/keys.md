# Keys & navigation

OpenTab opens on a stacked **Months / Days** (or Projects) sidebar, lazygit-style:
drill from a month or day into its detail tabs, from the Sessions tab into a single
session — cost split, model mix, subagent tree — and step back out with `Esc`.

Everything below is also in the app itself — press **`?`** for the built-in help.

## How the views nest

Three levels: **browse** → **zoom** → **session**. `Enter` (or `+`) drills in, `Esc`
steps back out. Zoom is not full-screen: the detail pane takes focus *beside* the
sidebar, which stays clickable to re-scope in place; `+` maximizes/restores the
detail pane (remembered between runs). The session view is full-screen.

Detail tabs per scope: years/months get Overview · Models · Projects · Sessions;
days drop Models. A session adds **Turns** (per-turn cost over time, every source
that records per-step usage), **Tools** (per-tool / MCP spend) and **Context** (the
context window's growth curve, % of the model's window, compaction markers, and —
on sources whose logs carry content — an estimated breakdown of what filled it)
when its source supports them, and **Sources** joins in the merged `all` view.

## Move around

| Key | Action |
|-----|--------|
| `p` / `t` | Switch to the Projects / Time browse mode |
| `Tab` / `Shift-Tab` | Cycle focus Years → Months → Days (Time mode); Shift-Tab at the top steps back out |
| `Enter` / `+` | Drill into the selection; on a Sessions / Projects / Sources tab, open it in this scope |
| `Esc` | Step back out — session → zoom → browse |
| `h` / `l` | Switch detail tabs |
| `j` / `k` | Move in the list (`↑`/`↓` too), or scroll the detail pane |
| `PgDn` / `PgUp` | Half a page (`Ctrl-D` / `Ctrl-U` too) |
| `g` / `G` | Jump to the top / bottom |
| Mouse | Wheel scrolls · click selects (anywhere in the preview pane focuses it) · double-click drills · click a tab, or a column header to sort (again to reverse) |

On the Turns tab, `z` (or clicking a `▸` header) unfolds the whole prompt text.

## Scope & filter

| Key | Action |
|-----|--------|
| `R` | Set the date range — `all` · `30d` (or `30`) · `2m` · `1y` · `2026` · `2026-05` · `start..end` |
| `a` | Back to all time, keeping the current selection where possible |
| `s` | Sort picker for the visible list (`j`/`k` move · `Enter` · `Esc`) |
| `f` or `/` | Live filter — fuzzy (fzf-style) over sessions (title/project/id), projects, and Models; substring over Prices. While filtering: `↑`/`↓` select · `Enter` keep · `Esc` cancel · `Ctrl-U` clear |
| `x` | Clear the filter |

## Sessions & projects

| Key | Action |
|-----|--------|
| `i` / `I` | Ignore / unignore the selection; `I` reveals hidden rows so they can be unignored |
| `b` / `B` | Bookmark ★ the selected session (remembered between runs); `B` shows only bookmarks, within the active range |
| `o` | Open the selected session's / project's directory |
| `L` | Launch the session in its own tool — `opencode --session` / `claude --resume` / `codex resume`. Then `w` window · `s` split · `v` vsplit · `p` popup · `y` copy the command (`w`/`s`/`v`/`p` need tmux or a [launcher hook](#custom-launchers); `y` copies anywhere) |
| `e` | Export the current list to a CSV in the working directory |

## Views & overlays

| Key | Action |
|-----|--------|
| `T` | Trends — Daily · Weekly · Monthly · Calendar · Models · Providers · Sources. `h`/`l` tabs · `j`/`k` page months/weeks/years. On the charts and Calendar: `Enter` focuses, arrows pick a bar/day, `Enter` drills in, `Esc` back. On Models/Providers/Sources: `j`/`k` pick a row · `Enter` its sessions · `Enter` again opens one |
| `P` | Model prices — the table behind the `$` estimate; see [Pricing](pricing.md) for the views, sorting, and pinning |
| `$` | Toggle what-if prices — what unpriced usage would cost at API list rates |
| `w` | What-if **model** — arm one model you've used as a comparison target (`j`/`k` move · `f` filter · `Enter` arm · `Esc` cancel): *"what if the expensive model had done the subagents' work too?"*. The selected session's **Subagents** tab then shows its whole tree (root included) with each node's cost beside what its tokens would have cost at the target's list rates — a **What-if** and a **Δ** column and a `TOTAL … routing saved …` line — and its **Overview** carries an Actual / What-if / Change summary, so a session that delegated nothing (no tree to show) still answers. A rate substitution, not a rerun: same tokens, one price list. **Session-scoped** — the sessions list, the day/month/project rollups and Trends keep showing actual spend, and `$` keeps working as always. Works in demo too; `w` again clears it |
| `c` | Data-source picker (`j`/`k` move · `Enter` switch · `Esc` cancel) |
| `C` | Colour-theme picker — `j`/`k` live-preview · `Enter` keep · `Esc` revert (themes are shared with the web browser) |
| `D` | Toggle real / demo data (demo anonymizes titles and paths) |
| `r` / `q` / `?` | Reload the data · quit · help |

The global toggles stay live *inside* the overlays: `?`, `C`, `c`, and `D` work from
anywhere, Trends and Prices included.

## What persists between runs

The active **source, range, sort, ignored projects, bookmarks, pinned price rows,
theme, and `$` what-if view are remembered between runs**, stored in
`~/.config/opentab/state.json`. Pass `--no-state` to disable; `--demo` never
persists.

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
