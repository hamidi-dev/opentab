<h1 align="center">OpenTab</h1>

<p align="center"><em>Your AI coding tools keep a tab. OpenTab opens it.</em></p>

<p align="center"><sub>Anonymized demo data — click the reel for the full-quality video.</sub></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/6d384355-a14e-489e-86df-24f7873c79da"><img src="https://github.com/user-attachments/assets/51b013fc-46cf-48ce-b6c8-dec2426400fd" alt="OpenTab — trends, a calendar spend heatmap, drill-downs across OpenCode / Claude Code / Codex, and live theming" width="900"></a>
  <br><sub><b>One reel, every view</b> — trends, a calendar spend heatmap, drill from a month down to a single session, and live theming</sub>
</p>

<p align="center">
  <img src="https://github.com/user-attachments/assets/b497c617-8a6c-4132-b6e2-aaf5078b8a4e" alt="OpenTab web browser — the same data as a self-contained page" width="900">
  <br><sub><b>Also a web browser</b> — <code>opentab --web</code> renders the same data as one self-contained, shareable page</sub>
</p>

A local, standard-library terminal UI for your AI coding spend. It reads the records your
coding tools already keep on disk — [OpenCode](https://opencode.ai)'s SQLite database,
[Claude Code](https://claude.com/claude-code)'s and [Codex](https://developers.openai.com/codex)'s
session transcripts, plus Hermes, GitHub Copilot (CLI and VS Code), pi-agent, OpenClaw, and CSV/JSONL
logs of API requests — and shows where your tokens and money went: by month, day, project,
session, and model, down to the subagent tree. Browse one tool at a time, or merge them all.

Your tools already keep this ledger; OpenTab is just the reader. No backend, no telemetry,
no accounts — it opens those files **read-only**. Standard-library-only at runtime
(`curses` + `sqlite3`): `pipx install opentab-ai` and there's nothing else to pull in.

## Features

- Reads OpenCode, Claude Code, Codex, Hermes, GitHub Copilot (its CLI and Copilot Chat in VS Code), pi-agent, OpenClaw, and logged-request CSV/JSONL files — one tool at a time, or merged into a single view
- Cost by month, day, project, session, and model
- Trends overlay: daily / weekly / monthly charts, a calendar spend heatmap, and model-, provider- and source-spend rankings
- Cost-share percentages and inline spend bars
- Per-session model mix and token breakdown
- Per-turn cost over time, and token usage per tool call
- Recursive subagent costs, on the sessions that delegated work
- "What-if" pricing (`$`): re-price unpriced subscription/credit usage at
  models.dev API list rates; `P` shows the price table behind it
- Git worktrees folded into their main repo
- Live fuzzy filter (fzf-style, title / project / id) and live date-range scoping
- CSV export of any view
- A self-contained **HTML browser** (`--html`) — same drill-in, calendar heatmap, and
  `$` toggle in one shareable file — and a local live server for it (`--serve`, or
  `--web` to also pop it open in your browser)
- Keyboard- and mouse-driven (scroll, click to select, double-click to drill)
- Remembers your range, sort, ignored projects, and the `$` view between runs
- Colour themes (opentab, Catppuccin, Tokyo Night, Gruvbox, Nord, Dracula, Rosé Pine —
  light and dark) shared by the TUI (`C`) and the web browser, set with `--theme`
- Read-only, local-only, standard-library runtime (nothing extra to pull in)
- Demo mode for screenshots and live demos

## Why a browser, not just a usage CLI

Plenty of tools will print your token totals. OpenTab is built to *explore* them:

- **Interactive, not a one-shot report.** Drill month → day → project → session →
  model, fuzzy-filter the lists live, rescope the date range on the fly, sort, and
  navigate by keyboard or mouse — a lazygit-style browser, not a table you re-run with
  different flags.
- **Subagent cost trees.** When a session delegated work, OpenTab attributes the cost
  across its whole recursive subagent subtree — so you see *where* the spend went, not
  just the session total.
- **Standard-library runtime.** Just `curses` + `sqlite3` from the standard library:
  no Node, no `npx`, no service to run. `pipx install opentab-ai` and it runs anywhere
  Python 3.9+ exists, including a locked-down box (the sole dependency, `windows-curses`,
  is pulled in only on native Windows).
- **Honest cost for subscription usage.** Subscription/credit sessions show a truthful
  `$0` recorded, and the **`$`** view reprices their tokens at API list rates — a clear
  "what this would have cost metered" estimate you can toggle on and off.

If you just want a single number in your terminal, a usage CLI does the job. OpenTab is
for when you want to *poke at* the spend. (See also [A note on cost accuracy](#a-note-on-cost-accuracy).)

## What it touches

Local-only, no network, no telemetry, no accounts — it opens every source file
**read-only**, so it doesn't modify any of them. For full transparency, everything it
touches, all on your own machine:

- **Reads** your tools' own records, read-only: OpenCode's SQLite DB, the JSONL
  transcripts of Claude Code / Codex / pi-agent / OpenClaw, Hermes' SQLite DB, the Copilot
  CLI's OpenTelemetry export, VS Code's Copilot Chat session store, and a CSV/JSONL of
  logged API requests (`--csv`/`--jsonl`).
  To fold git worktrees into their main repo it also reads project `.git` files (no `git`
  process is spawned; disable with `--no-worktrees`).
- **Writes** a small preferences file at `~/.config/opentab/state.json` (your last
  source, range, and sort; disable with `--no-state`), an optional model-price cache at
  `~/.config/opentab/prices.json` (only when you run `--refresh-models` or press `r` in the
  `P` overlay), and — only when you press `e` or run `--html` — an `opentab-*.csv`
  export or the HTML browser file in the current directory.
- **Runs** external programs only on the key you press: your file opener
  (`open`/`xdg-open`, or Explorer on Windows) for `o`, and for `L` either `tmux`, your own
  [launcher hook](#custom-launchers) (`~/.config/opentab/launcher`), or your clipboard tool
  (`pbcopy`/`wl-copy`/`xclip`/`xsel`) for its copy target. All are
  disabled in `--demo`.

## Requirements

Python **3.9+** and a terminal with `curses` — already present on macOS, Linux,
and WSL. Native Windows works too: installing `opentab-ai` pulls in `windows-curses`
automatically (see [Windows](#windows)).

## Install

### Try it first (no install)

```sh
uvx --from opentab-ai opentab --demo     # or: pipx run --spec opentab-ai opentab --demo
```

One command, nothing installed: `--demo` runs the full TUI on anonymized synthetic
data, so it works even on a machine with no AI-tool history to read. Drop `--demo` to
point it at your real usage.

### pipx (recommended)

```sh
pipx install opentab-ai
```

Upgrade later with `pipx upgrade opentab-ai`. (Plain `pip install --user opentab-ai`
works too.) The PyPI distribution is **`opentab-ai`**; the command it installs is
**`opentab`**.

### Homebrew (macOS / Linux)

```sh
brew install hamidi-dev/tap/opentab
```

Upgrade later with `brew upgrade opentab`.

### Install script

One line (installs the `opentab` command via pipx; re-run to update):

```sh
curl -fsSL https://raw.githubusercontent.com/hamidi-dev/opentab/main/install.sh | bash
```

### From source

```sh
git clone https://github.com/hamidi-dev/opentab && cd opentab
pipx install .        # or `pip install -e .` for a live-editable checkout
```

## Usage

```sh
opentab                          # open the browser, all time
opentab --days 30                # start within a window (change live with R)
opentab --since 2026-05-01 --until 2026-05-31
opentab --db /path/to/opencode.db  # default: ~/.local/share/opencode/opencode.db
opentab --source claude          # browse Claude Code spend instead (see below)
opentab --demo                   # safe for live demos / screenshots (see below)
opentab --html                   # write opentab-report.html and exit (see below)
opentab --serve                  # same browser on http://localhost:8321, live
opentab --web                    # --serve, and open it in your browser
```

### Data sources

OpenTab reads the local records each AI coding tool keeps — **OpenCode** (SQLite),
**Claude Code** (`~/.claude/projects/**/*.jsonl`), **Codex**
(`~/.codex/sessions/**/rollout-*.jsonl`), the **GitHub Copilot CLI** (its OpenTelemetry
export under `~/.copilot/otel/`), **Copilot Chat in VS Code** (VS Code's own
`chatSessions` store), **pi-agent** (`~/.pi/agent/sessions/`), and **OpenClaw**
(`~/.openclaw/agents/**/sessions/`), plus a **Hermes** database and a generic **CSV** or
**JSONL** of logged API requests. Pick one with `--source`:

```sh
opentab --source opencode                    # OpenCode only
opentab --source claude --claude-dir /path   # Claude Code (default ~/.claude/projects)
opentab --source codex --codex-dir /path     # Codex (default ~/.codex/sessions)
opentab --source copilot                     # GitHub Copilot CLI (default ~/.copilot/otel)
opentab --source vscode                      # Copilot Chat in VS Code (every installed variant)
opentab --source pi                          # pi-agent (default ~/.pi/agent/sessions)
opentab --source openclaw                    # OpenClaw gateway (default ~/.openclaw; honors $OPENCLAW_DIR)
opentab --csv requests.csv                   # a CSV of logged API requests (or --jsonl requests.jsonl)
opentab --source all                         # all present sources, merged
```

> **GitHub Copilot CLI:** it records usage **only** when its OpenTelemetry export is
> enabled. Set `COPILOT_OTEL_FILE_EXPORTER_PATH` before launching/resuming a session
> (`export COPILOT_OTEL_FILE_EXPORTER_PATH=~/.copilot/otel/usage.jsonl`); sessions after
> that show up under `--source copilot`.

> **Copilot Chat in VS Code:** read from VS Code's own chat-session store
> (`<User>/workspaceStorage/*/chatSessions`, plus empty-window sessions), across Code,
> Code&nbsp;-&nbsp;Insiders, and VSCodium — nothing to enable. Projects come from each
> workspace's folder; sessions the panel merely opened (no tokens) are ignored. Point
> `--vscode-dir` at a User directory for a portable/remote copy — from WSL, at the
> Windows-side store (see [Windows](#windows)).

> **OpenClaw:** sessions live under `~/.openclaw/agents/<agent>/sessions/` (one project per
> agent); point `--openclaw-dir`/`$OPENCLAW_DIR` at a mounted copy if it runs on a server.
> It records a per-message cost for every provider, but only **metered** routes (a direct
> Anthropic/OpenRouter key) are real spend — plan routes (openai-codex, github-copilot) are
> estimated under `$`, read from `openclaw.json` (read-only).

`--source auto` (the default) restores your last-used source, else **merges every present
source** when more than one exists (a single source when only one is). The active source
shows as a header chip; **switch live with `c`** (cycles whichever sources are present,
plus `all`). The whole TUI works the same — months, days, projects, sessions, models,
trends — with two differences, because **Claude Code, Codex, and Copilot (CLI and VS
Code) record only tokens, no per-message cost**:

- Such a session works like an OpenCode subscription session: **\$0 in normal mode** and an
  **estimate** (tokens × API list price) under the **`$`** view. Since that view would
  otherwise be a wall of `$0.00`, the estimate **starts on by default** there (header tag:
  `ESTIMATED`); press `$` for the recorded numbers, and your choice is remembered.
- Projects roll up to their **git root**, so sessions started in subdirectories group under
  the repo instead of bare folder names.

`--source all` merges every present source: the same repo across tools rolls up into one
project row, every session row shows its origin (a `Src` column, `[oc]`/`[cc]`/`[cx]`/`[cp]`/`[vs]`/`[pi]`/`[ocl]`/`[csv]`/`[jl]`
tags elsewhere), and Trends gains a **Sources** tab. `$` reprices the unpriced usage across
all of them. (With more than one source present, `--demo` **defaults to this merged view**
and anonymizes every backend under one shared scale so the cross-tool proportion stays
truthful.)

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

### Web browser (`--html` / `--serve` / `--web`)

`opentab --html` writes the whole browser as **one self-contained HTML file** —
no server, no dependencies, works from disk or any static host. It's the TUI in
the browser: the same sidebar, detail tabs, Trends and price-table overlays,
live range scoping and colour themes — driven by the same keys or the mouse.
Every view is a shareable deep link (the browser's back button steps out) and
every table sorts on a header click. Combine with `--demo` for a page you can
publish.

`opentab --serve` serves the same browser on `http://localhost:8321` (`--port`)
and adds what a static file can't have: the per-session **Turns** timeline and
**Tools** attribution fetched live on drill-in, plus a refresh button that
re-reads your data. `opentab --web` is the same thing but also opens it in
your default web browser (cross-platform — `open` on macOS, `xdg-open` on Linux, the
shell association on Windows). It binds to localhost only — the browser shows prompt
titles, project paths, and spend, so if you want it on another machine put it behind
something like Tailscale (`--bind`), never a public interface.

### Keys

OpenTab opens on a stacked **Months / Days** (or Projects) sidebar, lazygit-style:
drill from a month or day into its detail tabs, from the Sessions tab into a
single session — cost split, model mix, subagent tree — and step back out with
`Esc`. Everything is discoverable in-app — **`?` shows the full keymap**, every
panel and overlay documented. The short version:

| Key | Action |
|-----|--------|
| `j`/`k` · `h`/`l` · `Enter` · `Esc` | Move · switch tabs · drill in · step back out (`Tab` flips the sidebar panels) |
| `+` | Maximize / restore the drilled-in detail pane (the sidebar stays clickable beside it) |
| Mouse | Wheel scrolls, click selects, double-click drills, a column-header click sorts |
| `T` | Trends — cost charts, the calendar heatmap, model/provider/source rankings; every tab drills down to a session |
| `$` / `P` | What-if pricing at API list rates, and the price table behind it |
| `R` / `a` | Scope to a date range (`30d`, `2026-05`, `start..end`, …) / back to all time |
| `f` | Live fuzzy filter, fzf-style |
| `c` / `C` / `D` | Switch data source · colour theme · demo mode — from anywhere, overlays included |
| `L` | Relaunch the session in its own tool — tmux window/split/popup, or [your own launcher](#custom-launchers) |
| `e` / `o` | Export the current view to CSV / open the project's directory |
| `?` / `q` | Help / quit |

The active **source, range, sort, ignored projects, and `$` what-if view are
remembered between runs** (stored in `~/.config/opentab/state.json`; pass `--no-state`
to disable, and `--demo` does not persist). Sub-cent costs render as `<$0.01` so
they aren't confused with a red `$0.00`, which means *unpriced* (tokens with no
local price).

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

OpenTab uses Python's `curses`, which native Windows Python doesn't bundle. Two
ways to run it:

**Native Windows (cmd / PowerShell).** Just install and run — `opentab-ai` declares
`windows-curses` as a Windows-only dependency, so pipx pulls in the curses shim for you:

```sh
pipx install opentab-ai
opentab
```

`windows-curses` is just an OS-level provider for the stdlib `curses` module — the lone
runtime dependency, and only on Windows. Confirmed working against the **OpenCode** source; the Claude Code and
Codex backends read plain JSON files and should behave the same, but are less
exercised on native Windows. The `o` key opens the selected directory in
Explorer (via `os.startfile`), so reveal-in-folder works natively too.

**WSL.** `curses` is already there, so a plain `opentab` works. OpenCode itself
doesn't have to run inside WSL — even on native Windows it keeps its database
under your Windows home, at `%USERPROFILE%\.local\share\opencode\opencode.db`,
which WSL reads through `/mnt/c`:

```sh
# from inside WSL, reading the Windows-side OpenCode database
opentab --db /mnt/c/Users/<you>/.local/share/opencode/opencode.db
```

If OpenCode runs inside WSL, the default path
(`~/.local/share/opencode/opencode.db`) just works. Either way, `--db` points
OpenTab at any non-standard location.

Copilot Chat in VS Code works the same way from WSL: chat sessions are stored by
the Windows-side VS Code (also for Remote-WSL windows), under the Windows profile.
That store is *not* scanned by default — reading through `/mnt/c` is slow enough
to drag down every startup — so opt in by pointing `--vscode-dir` at it, e.g. via
an alias:

```sh
alias opentab='opentab --vscode-dir "/mnt/c/Users/<you>/AppData/Roaming/Code/User"'
```

Remote-WSL workspaces then resolve back to their in-distro project directories,
and native Windows workspaces to their `/mnt/c/...` paths.

If `curses` is missing, OpenTab prints a short hint (install `windows-curses`)
instead of crashing.

## Development

CI runs Ruff, unit tests, and ShellCheck. See [CONTRIBUTING.md](CONTRIBUTING.md) for local
setup, the test/lint commands, the pre-push hooks, and commit conventions.

## A note on cost accuracy

The numbers come straight from each tool's own data (cost/tokens per message, rolled up
per session) — *local attribution* of what your tools recorded. Some sessions show tokens
with a `$0.00` local cost: the usage was recorded but no per-token price, normal whenever
billing isn't per token (subscription plans, credit/token plans). That money isn't
missing, it's billed elsewhere — by your subscription or account credits — so OpenTab
surfaces it as "unpriced tokens" rather than guessing.

Press `$` (non-demo) for the **what-if** view: real recorded spend plus what `$0.00`
subscription/credit usage _would have cost_ at published API list prices (`P` shows the
exact per-model rates). The estimate uses an embedded table generated from models.dev for
Anthropic/OpenAI/Google, with family fallbacks for version churn and a mid-range fallback
for unknown models. Nothing is fetched at runtime, so the TUI stays offline.

That embedded table only covers the big three, so **open models on paid routes** (Kimi,
DeepSeek, Qwen, … via OpenRouter/Together/etc.) show as unpriced. Refresh from models.dev
to price them:

```sh
opentab --refresh-models     # fetch every provider's list prices into a local cache
```

This writes `~/.config/opentab/prices.json` (the one time runtime OpenTab touches the
network, and only on this explicit command — stdlib `urllib`, no dependency). The cache
**overlays** the embedded table; you can also press **`r`** inside `P` to refresh in place.
When OpenTab notices models it has no built-in price for, it offers this fetch **once** on
startup (`y` now, `n` not now, `d` never — remembered in `state.json`, suppressed under
`--no-state`/`--demo`).

## License

MIT — see [LICENSE](LICENSE).
