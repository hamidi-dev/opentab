<h1 align="center">OpenTab</h1>

<p align="center"><em>Your AI coding tools keep a tab. OpenTab opens it.</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/19ad1687-a18a-417d-a2c4-62e3fa765970">
    <img src="https://github.com/user-attachments/assets/2d782465-e82c-4a98-b40f-f765eb5d28d2" alt="OpenTab — browse your AI coding spend" width="820">
  </a>
  <br>
  <sub><a href="https://github.com/user-attachments/assets/19ad1687-a18a-417d-a2c4-62e3fa765970">▶ Watch the full-quality video</a></sub>
</p>

A local, zero-dependency terminal UI for your AI coding spend. It reads the records
your coding tools already keep on disk — [OpenCode](https://opencode.ai)'s SQLite
database and [Claude Code](https://claude.com/claude-code)'s session transcripts — and
shows you where your tokens and money actually went: by month, day, project, session,
and model, down to the subagent tree on the sessions that spawned one. Browse one tool
at a time, or merge them into a single view.

Your tools already keep this ledger; OpenTab is just the reader for it. No backend, no
telemetry, no accounts — it opens those files **read-only**, so it only reads and leaves
your data untouched. Just `curses` + `sqlite3` from the Python standard library — no
`pip install` needed.

## Features

- Reads OpenCode and Claude Code — one tool at a time, or merged into a single view
- Cost by month, day, project, session, and model
- Trends overlay: daily / weekly / monthly spend charts + model- and provider-spend ranking
- Cost-share percentages and inline spend bars
- Per-session model mix and token breakdown
- Recursive subagent costs, on the sessions that delegated work
- "What-if" pricing (`$`): re-price unpriced subscription/credit usage at
  models.dev API list rates; `P` shows the price table behind it
- Git worktrees folded into their main repo
- Live fuzzy filter (fzf-style, title / project / id) and live date-range scoping
- CSV export of any view
- Keyboard- and mouse-driven (scroll, click to select, double-click to drill)
- Remembers your range, sort, ignored projects, and the `$` view between runs
- Read-only, local-only, zero dependencies
- Demo mode for screenshots and live demos

## Why this exists

Your coding tools already log every session — cost, token breakdown, model, and the
full parent/child subagent tree — into plain local files. OpenCode keeps a real SQLite
database:

```
~/.local/share/opencode/opencode.db
```

Claude Code keeps newline-delimited JSON transcripts:

```
~/.claude/projects/**/*.jsonl
```

That's the whole pitch: the data is already sitting on your disk, so you can *see your
own AI usage* without sending it anywhere. OpenTab is what that looks like when you do.

## What it touches

Local-only, no network, no telemetry, no accounts — it opens every source file
**read-only**, so it doesn't modify any of them. For full transparency, everything it
touches, all on your own machine:

- **Reads** your tools' own records, read-only: OpenCode's SQLite database and Claude
  Code's JSONL transcripts under `~/.claude/projects`. To fold git worktrees into their
  main repo it also reads the `.git` file of project directories (no `git` process is
  spawned; disable with `--no-worktrees`).
- **Writes** a small preferences file at `~/.config/opentab/state.json` (your last
  source, range, and sort; disable with `--no-state`), and — only when you press `e` — an
  `opentab-*.csv` export in the current directory.
- **Runs** external programs only on the key you press: your clipboard tool
  (`pbcopy`/`wl-copy`/`xclip`/`xsel`) for `y`, your file opener
  (`open`/`xdg-open`) for `o`, and for `L` either `tmux` or your own
  [launcher hook](#custom-launchers) (`~/.config/opentab/launcher`). All are
  disabled in `--demo`.

## Requirements

Python **3.9+** (standard library only — no `pip install` needed) and a Unix-like
OS with `curses` (macOS, Linux, WSL).

## Install

### Homebrew (macOS / Linux)

```sh
brew install hamidi-dev/tap/opentab
```

Upgrade later with `brew upgrade opentab`.

### Install script

One line (installs `opentab` into `~/.local/bin`; re-run to update):

```sh
curl -fsSL https://raw.githubusercontent.com/hamidi-dev/opentab/main/install.sh | bash
```

Prefer not to pipe a script into your shell? OpenTab is a single
self-contained file, so any of these work and are easy to audit first:

```sh
# clone (symlink install; `git pull` then auto-updates)
git clone https://github.com/hamidi-dev/opentab && cd opentab && ./install.sh

# or just drop the one file on your PATH
curl -fsSL https://raw.githubusercontent.com/hamidi-dev/opentab/main/opentab \
  -o ~/.local/bin/opentab && chmod +x ~/.local/bin/opentab
```

`BIN_DIR=~/bin` overrides the install target.

## Usage

```sh
opentab                          # open the browser, all time
opentab --days 30                # start within a window (change live with R)
opentab --since 2026-05-01 --until 2026-05-31
opentab --db /path/to/opencode.db  # default: ~/.local/share/opencode/opencode.db
opentab --source claude          # browse Claude Code spend instead (see below)
opentab --demo                   # safe for live demos / screenshots (see below)
```

### Data sources

OpenTab reads the local records each AI coding tool keeps. Today there are two
backends — **OpenCode** (its SQLite database) and **Claude Code** (its session
transcripts under `~/.claude/projects/**/*.jsonl`) — picked with `--source`:

```sh
opentab --source opencode                    # OpenCode only
opentab --source claude                      # Claude Code only (default ~/.claude/projects)
opentab --source claude --claude-dir /path   # non-standard Claude Code location
opentab --source all                         # OpenCode + Claude Code, merged
```

`--source auto` (the default) reads OpenCode when its database is present, otherwise
falls back to Claude Code (it never auto-merges). The active source shows as a chip in
the header, and you can **switch live with `c`** (OpenCode → Claude Code → all). The
whole TUI works the same — months, days, projects, sessions, models, trends — with two
differences, because Claude Code records **only tokens, no per-message cost**:

- A Claude session works like an OpenCode subscription session: it shows **$0 in
  normal mode** (nothing is recorded) and its **estimate** (tokens × API list price)
  under the **`$`** view. Since a Claude-only (or merged) view would otherwise be a
  wall of `$0.00`, the estimate view **starts on by default** there (header tag:
  `ESTIMATED — tokens × API list prices`); press `$` to see the recorded numbers, and
  your choice is remembered.
- Projects roll up to their **git root**, so sessions started in subdirectories
  (`frontend/`, `src/`, …) group under the repo instead of bare folder names.

`--source all` merges both into one view: the same repo worked in both tools rolls up
into a single project row, every session row shows its origin (a `Src` column in the
session tables, `[oc]` / `[cc]` tags elsewhere), and the Trends overlay
gains a **Sources** tab (spend by tool). `$` reprices the unpriced usage across both —
OpenCode's subscription/credit messages and all of Claude's. (When more than one source
is present, `--demo` **defaults to this merged view** — it shows off the most — and
anonymizes both backends under a single shared scale so the OpenCode-vs-Claude proportion
stays truthful.)

### Demo mode

`opentab --demo` is for showing the tool to other people without leaking your real
work. Everything is transformed in memory on load and nothing is written back:

- Session titles and project paths are replaced with deterministic, plausible
  fakes (stable across redraws).
- Sessions OpenCode recorded with no cost get a synthetic price derived from
  their real token counts, so there are no `$0.00 / unpriced` gaps on screen.

The *shape* of your data stays real — the relative proportions between sessions
and months, and the model mix (which models, in what ratio) — but the absolute
numbers do not. A `DEMO — synthetic` tag shows in the header so synthetic figures
are never mistaken for real ones.

OpenTab opens on a stacked **Months / Days** sidebar (lazygit-style). `Tab` flips
focus between the two panels. `Enter` **zooms** the focused month's or day's
detail full-screen (Overview / Models / Projects / Sessions, switch with `h`/`l`). On the
**Sessions** tab, `j`/`k` pick a session and `Enter` opens *that session's* own
detail — cost split, model mix, and subagent tree. `Esc` steps back out.

### Keys

| Key | Action |
|-----|--------|
| `Tab` | Flip focus between the Months and Days panels |
| `Enter` / `+` | Drill in: month/day zoom → (on Sessions tab) open a session |
| `Esc` | Step back out (session → zoom → browse) |
| `Shift-Tab` | Flip Months/Days focus while browsing; otherwise step back out |
| `j`/`k` or arrows | Move in the current list / scroll detail |
| `h`/`l` | Switch detail tabs |
| Mouse | Wheel scrolls; click a row or tab to select; double-click to drill in |
| `g` / `G` | Top / bottom |
| `R` | Set range (`all`, `30d` or `30`, `2m`, `1y`, `2026`, `2026-05`, `YYYY-MM-DD..YYYY-MM-DD`); `2m`/`1y` are whole calendar months |
| `a` | Show all time |
| `s` / `S` | Cycle sort forward/backward for visible session, project, or subagent lists |
| `i` | Ignore/unignore the selected project from project lists |
| `I` | Show/hide ignored projects so they can be unignored |
| `/` | Live fuzzy filter: the lists narrow and re-rank (best match first) as you type, fzf-style subsequence matching over title/project/id; `↑`/`↓` select while typing, `Enter` keeps the filter, `Esc` cancels, `Ctrl-U` clears the input, `x` clears it later |
| `T` | Trends overlay — Daily / Weekly / Monthly cost charts + Model, Provider, and Source spend ranking (`h`/`l` tabs, `j`/`k` month/week, `$` toggles what-if) |
| `$` | What-if pricing: re-price unpriced subscription/credit usage at models.dev API list prices |
| `P` | Show the models.dev API price table OpenTab uses for `$` |
| `e` | Export the current list (months/days/projects/sessions/subagents) to a CSV in the working dir |
| `y` | Copy the selected session id (or project path) to the clipboard |
| `o` | Open the selected session's / project's directory |
| `L` | Launch the selected session in its own tool (`opencode --session <id>` / `claude --resume <id>`). Inside tmux a one-key menu opens it in a new **w**indow, **s**plit, **v**split, or **p**opup (cd'd to the project); outside tmux (or with `y`) the `cd <project> && …` command is copied to the clipboard instead. See [Custom launchers](#custom-launchers) to route launches through your own tooling |
| `c` | Switch data source: OpenCode / Claude Code / all (when more than one is present) |
| `r` | Reload the database |
| `?` | Help; `q` quits |

The active **source, range, sort, ignored projects, and `$` what-if view are
remembered between runs** (stored in `~/.config/opentab/state.json`; pass `--no-state`
to disable, and `--demo` does not persist). An explicit `--source` overrides the saved
one. Sub-cent costs render as `<$0.01` so they aren't confused with a red
`$0.00`, which means *unpriced* (tokens with no local price). The Months and Days
lists show a small bar scaled to the largest spend in view.

### Custom launchers

If an executable exists at `~/.config/opentab/launcher` (or `$OPENTAB_LAUNCHER`
points at one), every `L`-menu launch is handed to it instead of the built-in
tmux commands — git-hooks style. It's called as

```sh
launcher <kind> <directory> <command>
# kind ∈ window | hsplit | vsplit | popup
# e.g. launcher window /repo/myproj 'claude --resume abc123'
```

and a nonzero exit shows its stderr as the launch error. The footer reads
"launch via launcher hook" when one is active. Use it to route launches through
your own popup manager, zellij, kitty tabs, a different multiplexer — anything:

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

## Windows

OpenTab uses Python's `curses`, which is **Unix-only** (not bundled with Windows
Python), so you run it from **WSL**. OpenCode itself does not have to run inside
WSL, though: even when OpenCode runs on native Windows it keeps its database
under your Windows home, at `%USERPROFILE%\.local\share\opencode\opencode.db`,
which WSL can read through `/mnt/c`. Point OpenTab at it:

```sh
# from inside WSL, reading the Windows-side OpenCode database
opentab --db /mnt/c/Users/<you>/.local/share/opencode/opencode.db
```

If OpenCode runs inside WSL, the default path (`~/.local/share/opencode/opencode.db`)
just works with a plain `opentab`. Either way, `--db` points at any non-standard
location.

Native Windows (cmd/PowerShell) is not supported; it would need
`pip install windows-curses`, which is untested here. OpenTab prints a short
hint instead of crashing if `curses` is missing.

## Development

CI runs Ruff, unit tests, and ShellCheck. To use the same pre-push checks locally:

```sh
pip install ruff==0.1.15
git config core.hooksPath hooks
```

Before pushing, the hook runs:

```sh
ruff check opentab test_opentab.py
ruff format --check opentab test_opentab.py
python3 -m py_compile opentab
python3 test_opentab.py
shellcheck install.sh hooks/pre-push  # when shellcheck is installed
```

To fix formatting manually:

```sh
ruff format opentab test_opentab.py
```

## A note on cost accuracy

The numbers come straight from each tool's own data (cost/tokens per message,
rolled up per session). They are *local attribution* of what your tools recorded.
Some sessions show tokens with a `$0.00` local cost — the tool recorded the usage
but no per-token price. That's normal whenever billing isn't per token:
subscription plans (Claude Code, Codex) and credit/token plans (GitHub Copilot)
both leave the per-message cost empty. Those tokens aren't missing money so much
as billed elsewhere — by your subscription or account credits — so the real total
lives with your provider, not this tool. OpenTab surfaces them as "unpriced
tokens" so you know where local attribution is incomplete.

Press `$` in non-demo mode for the **what-if** view: real recorded spend plus
what `$0.00` subscription/credit usage would have cost at published API list
prices. Press `P` to see the exact per-model rates behind that estimate. The
estimate uses an embedded table generated from models.dev for Anthropic, OpenAI,
and Google, with hand-kept family fallbacks for version or suffix churn and a
mid-range fallback for unknown models. Nothing is fetched at runtime, so the TUI
stays single-file, offline, and standard-library only. To refresh the embedded
table, run `python3 scripts/update_prices.py` and commit the changed `opentab`
file.

## License

MIT — see [LICENSE](LICENSE).
