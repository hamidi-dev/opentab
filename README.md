<h1 align="center">OpenTab</h1>

<p align="center"><em>OpenCode keeps a tab. OpenTab opens it.</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/89bd4092-5233-48fe-b287-d72a7505ca21">
    <img src="https://github.com/user-attachments/assets/67d0949a-2fb8-4e95-9dbf-6a24b301d580" alt="OpenTab — browse your OpenCode spend" width="820">
  </a>
  <br>
  <sub><a href="https://github.com/user-attachments/assets/89bd4092-5233-48fe-b287-d72a7505ca21">▶ Watch the full-quality video</a></sub>
</p>

A local, zero-dependency terminal UI for your [OpenCode](https://opencode.ai)
spend. It reads OpenCode's own SQLite database — the one already on your disk —
and shows you where your tokens and money actually went: by month, day, project,
session, and model, down to the subagent tree on the sessions that spawned one.

OpenCode already keeps this ledger; OpenTab is just the reader for it. No backend,
no telemetry, no accounts — it opens the database **read-only**, so today it only
reads and leaves your data untouched. Just `curses` + `sqlite3` from the Python
standard library — no `pip install` needed.

## Features

- Cost by month, day, project, session, and model
- Trends overlay: daily / weekly / monthly spend charts + model- and provider-spend ranking
- Cost-share percentages and inline spend bars
- Per-session model mix and token breakdown
- Recursive subagent costs, on the sessions that delegated work
- "What-if" pricing (`$`): re-price unpriced subscription/credit usage at
  models.dev API list rates; `P` shows the price table behind it
- Git worktrees folded into their main repo
- Filter (title / project / id) and live date-range scoping
- CSV export of any view
- Keyboard- and mouse-driven (scroll, click to select, double-click to drill)
- Remembers your range, sort, ignored projects, and the `$` view between runs
- Read-only, local-only, zero dependencies
- Demo mode for screenshots and live demos

## Why this exists

OpenCode logs every session — cost, token breakdown, model, and the full
parent/child subagent tree — into a plain SQLite file:

```
~/.local/share/opencode/opencode.db
```

That's the whole pitch: because it's a real database, you can *query your own
AI usage*. OpenTab is what that looks like when you do.

## What it touches

Local-only, no network, no telemetry, no accounts — and it opens the OpenCode
database **read-only**, so as it stands it doesn't modify it. For full transparency,
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
opentab --demo                   # safe for live demos / screenshots (see below)
```

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
| `/` | Filter sessions (title/project/id) and the project list; `Esc` cancels; `x` clears |
| `T` | Trends overlay — Daily / Weekly / Monthly cost charts + Model and Provider spend ranking (`h`/`l` tabs, `j`/`k` month/week, `$` toggles what-if) |
| `$` | What-if pricing: re-price unpriced subscription/credit usage at models.dev API list prices |
| `P` | Show the models.dev API price table OpenTab uses for `$` |
| `e` | Export the current list (months/days/projects/sessions/subagents) to a CSV in the working dir |
| `y` | Copy the selected session id (or project path) to the clipboard |
| `o` | Open the selected session's / project's directory |
| `r` | Reload the database |
| `?` | Help; `q` quits |

The active **range, sort, ignored projects, and `$` what-if view are remembered
between runs** (stored in `~/.config/opentab/state.json`; pass `--no-state` to
disable, and `--demo` does not persist). Sub-cent costs render as `<$0.01` so they aren't confused with a red
`$0.00`, which means *unpriced* (tokens with no local price). The Months and Days
lists show a small bar scaled to the largest spend in view.

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

The numbers come straight from OpenCode's own data (cost/tokens per message,
rolled up per session). They are *local attribution* of what OpenCode recorded.
Some sessions show tokens with a `$0.00` local cost — OpenCode recorded the usage
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
