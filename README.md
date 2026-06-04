# OpenTab

> OpenCode keeps a tab. OpenTab opens it.

https://github.com/user-attachments/assets/6b63e1fc-effc-4b0f-bda1-7eb84f0b603f

A local, dependency-free terminal UI for your [OpenCode](https://opencode.ai)
spend. `opentab` reads OpenCode's own SQLite database and shows you exactly where
your tokens and money went — by month, day, project, session, model, and subagent —
including the recursive cost of every subagent a session spawned.

No backend. No telemetry. No accounts. Just `curses` + `sqlite3` from the
Python standard library. No `pip install`, ever.

## Features

- Session cost breakdown
- Model attribution
- Recursive subagent costs
- Monthly, daily, and project views
- Cost-share percentages and inline spend bars
- Spend-trend sparklines (daily per month, monthly per project)
- Git worktrees folded into their main repo
- CSV export of any view
- Remembers your range and sort between runs
- Local-only
- Zero dependencies
- Demo mode for screenshots

## Why this exists

OpenCode logs every session — cost, token breakdown, model, and the full
parent/child subagent tree — into a plain SQLite file:

```
~/.local/share/opencode/opencode.db
```

That's the whole pitch: because it's a real database, you can *query your own
AI usage*. `opentab` is what that looks like when you do.

## What it touches

Local-only, no network, no telemetry, no accounts — and it opens the OpenCode
database **read-only**, so it physically cannot modify it. For full transparency,
everything it touches, all on your own machine:

- **Reads** the OpenCode SQLite DB (read-only). To fold git worktrees into their
  main repo it also reads the `.git` file of project directories (no `git`
  process is spawned; disable with `--no-worktrees`).
- **Writes** a small preferences file at `~/.config/opentab/state.json` (your last
  range and sort; disable with `--no-state`), and — only when you press `e` — an
  `opentab-*.csv` export in the current directory.
- **Runs** external programs only on the key you press: your clipboard tool
  (`pbcopy`/`wl-copy`/`xclip`/`xsel`) for `y`, and your file opener
  (`open`/`xdg-open`) for `o`. Both are disabled in `--demo`.

## Requirements

Python **3.9+** (standard library only — no `pip install`, ever) and a Unix-like
OS with `curses` (macOS, Linux, WSL).

## Install

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

`BIN_DIR=~/bin` overrides the install target. A Homebrew tap
(`brew install hamidi-dev/tap/opentab`) is planned once the first release is tagged.

## Usage

```sh
opentab                          # open the browser, all time
opentab --days 30                # start within a window (change live with R)
opentab --since 2026-05-01 --until 2026-05-31
opentab --db /path/to/opencode.db  # default: ~/.local/share/opencode/opencode.db
opentab --demo                   # safe for live demos / screenshots (see below)
```

### Demo mode

`opentab --demo` is for showing the tool to other people without leaking your real
work. It **never writes to the database** — everything is transformed in memory
on load:

- Session titles and project paths are replaced with deterministic, plausible
  fakes (stable across redraws).
- Sessions OpenCode recorded with no cost get a synthetic price derived from
  their real token counts, so there are no `$0.00 / unpriced` gaps on screen.

The *shape* of your data stays real — token counts, model mix, and already-priced
costs are untouched. A `DEMO — synthetic` tag shows in the header so synthetic
numbers are never mistaken for real ones.

`opentab` opens on a stacked **Months / Days** sidebar (lazygit-style). `Tab` flips
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
| `g` / `G` | Top / bottom |
| `R` | Set range (`all`, `30d` or `30`, `2m`, `1y`, `2026`, `2026-05`, `YYYY-MM-DD..YYYY-MM-DD`) |
| `a` | Show all time |
| `s` / `S` | Cycle sort forward/backward for visible session, project, or subagent lists |
| `/` | Filter sessions (title/project/id) and the project list; `Esc` cancels; `x` clears |
| `e` | Export the current list (months/days/projects/sessions/subagents) to a CSV in the working dir |
| `y` | Copy the selected session id (or project path) to the clipboard |
| `o` | Open the selected session's / project's directory |
| `r` | Reload the database |
| `?` | Help; `q` quits |

The active **range and sort are remembered between runs** (stored in
`~/.config/opentab/state.json`; pass `--no-state` to disable, and `--demo` never
persists). Sub-cent costs render as `<$0.01` so they aren't confused with a red
`$0.00`, which means *unpriced* (tokens with no local price). The Months and Days
lists show a small bar scaled to the largest spend in view.

## Windows

`opentab` uses Python's `curses`, which is **Unix-only** (not bundled with Windows
Python). The supported way to run it on Windows is **WSL** — and that's the
natural fit, since OpenCode on Windows usually runs inside WSL, so its database
already lives in the WSL filesystem where `opentab` can read it.

If OpenCode's DB is somewhere non-standard, point `opentab` at it:

```sh
opentab --db /path/to/opencode.db
```

Native Windows (cmd/PowerShell) is not supported; it would need
`pip install windows-curses`, which is untested here. `opentab` prints a short
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

The numbers come straight from OpenCode's own data (cost/tokens per message,
rolled up per session). They are *local attribution* of what OpenCode recorded.
For providers with token-based or credit-based billing (e.g. GitHub Copilot),
some sessions show tokens with a `$0.00` local cost — those need account-level
reconciliation against your provider, not this tool. `opentab` surfaces these as
"unpriced tokens" so you know where attribution is incomplete.

## Not affiliated

This project is not affiliated with or endorsed by GitHub, Microsoft, OpenCode,
SST, or any provider. It only reads a local database you already have.

## License

MIT — see [LICENSE](LICENSE).
