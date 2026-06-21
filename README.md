<h1 align="center">OpenTab</h1>

<p align="center"><em>Your AI coding tools keep a tab. OpenTab opens it.</em></p>

<p align="center"><sub>Anonymized demo data — click any clip for the full-quality video.</sub></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/fdfc2626-6ebb-4422-901c-6d2f67068160"><img src="https://github.com/user-attachments/assets/23c2e927-aa72-4bff-a972-e166166345a0" alt="Trends — daily/weekly/monthly charts and model/provider/source rankings" width="820"></a>
  <br><sub><b>Trends</b> — daily / weekly / monthly spend, plus model-, provider- and source-spend rankings</sub>
</p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/674534af-3047-4b18-9031-2cf99f1f0cf9"><img src="https://github.com/user-attachments/assets/d2a81e36-34f7-46d2-af0b-eb31545b9a55" alt="Spend heatmap — Trends to Calendar, GitHub-style daily-spend map" width="820"></a>
  <br><sub><b>Spend heatmap</b> — Trends → Calendar; <code>+</code>/<code>−</code> tune the shades, Enter drills a day</sub>
</p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/f11f3493-55fa-4722-b2f1-8cd96355bfab"><img src="https://github.com/user-attachments/assets/5c88abf8-1d44-4237-a527-f166082ab512" alt="Sessions ranked by cost, tagged by tool, drilled to a session breakdown" width="820"></a>
  <br><sub><b>Sessions</b> — a day's sessions ranked by cost and tagged by tool; open the priciest for its cost split, model mix and subagent tree</sub>
</p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/aad33047-3345-40c5-8539-b36b533207d6"><img src="https://github.com/user-attachments/assets/5733178c-dd5b-4aa3-bce0-dfcd7c002d41" alt="Projects across tools, source cycling, fuzzy filter and live range" width="820"></a>
  <br><sub><b>Projects &amp; sources</b> — group spend by repo across tools, isolate one with <code>c</code>, fuzzy-filter, and rescope the range live</sub>
</p>

A local, zero-dependency terminal UI for your AI coding spend. It reads the records
your coding tools already keep on disk — [OpenCode](https://opencode.ai)'s SQLite
database, [Claude Code](https://claude.com/claude-code)'s session transcripts, and
[Codex](https://developers.openai.com/codex)'s CLI rollouts — and shows you where your
tokens and money actually went: by month, day, project, session, and model, down to the
subagent tree on the sessions that spawned one. Browse one tool at a time, or merge them
into a single view.

Your tools already keep this ledger; OpenTab is just the reader for it. No backend, no
telemetry, no accounts — it opens those files **read-only**, so it only reads and leaves
your data untouched. Just `curses` + `sqlite3` from the Python standard library — no
`pip install` needed.

## Features

- Reads OpenCode, Claude Code, and Codex — one tool at a time, or merged into a single view
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

Claude Code and Codex keep newline-delimited JSON transcripts:

```
~/.claude/projects/**/*.jsonl       # Claude Code
~/.codex/sessions/**/rollout-*.jsonl  # Codex CLI
```

That's the whole pitch: the data is already sitting on your disk, so you can *see your
own AI usage* without sending it anywhere. OpenTab is what that looks like when you do.

## Why a browser, not just a usage CLI

Plenty of tools will print your token totals. OpenTab is built to *explore* them:

- **Interactive, not a one-shot report.** Drill month → day → project → session →
  model, fuzzy-filter the lists live, rescope the date range on the fly, sort, and
  navigate by keyboard or mouse — a lazygit-style browser, not a table you re-run with
  different flags.
- **Subagent cost trees.** When a session delegated work, OpenTab attributes the cost
  across its whole recursive subagent subtree — so you see *where* the spend went, not
  just the session total.
- **One file, zero dependencies.** Just `curses` + `sqlite3` from the standard library:
  no `pip install`, no Node, no `npx`, no build step. Copy one script and run it
  anywhere Python 3.9+ exists, including a locked-down box.
- **Honest cost for subscription usage.** Subscription/credit sessions show a truthful
  `$0` recorded, and the **`$`** view reprices their tokens at API list rates — a clear
  "what this would have cost metered" estimate you can toggle on and off.

If you just want a single number in your terminal, a usage CLI does the job. OpenTab is
for when you want to *poke at* the spend. (See also [A note on cost accuracy](#a-note-on-cost-accuracy).)

## What it touches

Local-only, no network, no telemetry, no accounts — it opens every source file
**read-only**, so it doesn't modify any of them. For full transparency, everything it
touches, all on your own machine:

- **Reads** your tools' own records, read-only: OpenCode's SQLite database, Claude
  Code's JSONL transcripts under `~/.claude/projects`, and Codex's CLI rollouts under
  `~/.codex/sessions`. To fold git worktrees into their main repo it also reads the
  `.git` file of project directories (no `git` process is spawned; disable with
  `--no-worktrees`).
- **Writes** a small preferences file at `~/.config/opentab/state.json` (your last
  source, range, and sort; disable with `--no-state`), and — only when you press `e` — an
  `opentab-*.csv` export in the current directory.
- **Runs** external programs only on the key you press: your clipboard tool
  (`pbcopy`/`wl-copy`/`xclip`/`xsel`) for `y`, your file opener
  (`open`/`xdg-open`) for `o`, and for `L` either `tmux` or your own
  [launcher hook](#custom-launchers) (`~/.config/opentab/launcher`). All are
  disabled in `--demo`.

## Requirements

Python **3.9+** and a terminal with `curses` — already present on macOS, Linux,
and WSL (standard library only, no `pip install`). Native Windows works too with
a one-time `pip install windows-curses` (see [Windows](#windows)).

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

OpenTab reads the local records each AI coding tool keeps — **OpenCode** (its SQLite
database), **Claude Code** (its session transcripts under
`~/.claude/projects/**/*.jsonl`), **Codex** (its CLI rollouts under
`~/.codex/sessions/**/rollout-*.jsonl`), the **GitHub Copilot CLI** (its OpenTelemetry
export under `~/.copilot/otel/`), **pi-agent** (its sessions under
`~/.pi/agent/sessions/`), and **OpenClaw** (its gateway sessions under
`~/.openclaw/agents/**/sessions/`), plus a **Hermes** database and a generic **CSV** of logged API
requests (e.g. GitHub Copilot in IntelliJ). Pick one with `--source`:

```sh
opentab --source opencode                    # OpenCode only
opentab --source claude                      # Claude Code only (default ~/.claude/projects)
opentab --source claude --claude-dir /path   # non-standard Claude Code location
opentab --source codex                       # Codex only (default ~/.codex/sessions)
opentab --source codex --codex-dir /path     # non-standard Codex sessions location
opentab --source copilot                     # GitHub Copilot CLI (default ~/.copilot/otel)
opentab --source pi                          # pi-agent (default ~/.pi/agent/sessions)
opentab --source openclaw                    # OpenClaw gateway (default ~/.openclaw; honors $OPENCLAW_DIR)
opentab --source all                         # all present sources, merged
```

> **GitHub Copilot CLI:** the CLI records usage **only** when its OpenTelemetry file
> export is enabled — there is no token count in its session files otherwise. Turn it on
> by setting `COPILOT_OTEL_FILE_EXPORTER_PATH` before you launch or resume a session:
>
> ```sh
> export COPILOT_OTEL_FILE_EXPORTER_PATH=~/.copilot/otel/usage.jsonl
> ```
>
> Sessions you run after that show up under `--source copilot`.

> **OpenClaw:** the gateway keeps its sessions under `~/.openclaw/agents/<agent>/sessions/`
> (one project per agent). If you run it on a server, point opentab at a mounted/synced
> copy with `--openclaw-dir /path` or `OPENCLAW_DIR=/path`. OpenClaw records a per-message
> cost for **every** provider, but that figure is a list-price estimate on plan routes
> (openai-codex, github-copilot) whose real cost is $0 — opentab counts only **metered**
> routes (a direct Anthropic/OpenRouter key) as spend and estimates the rest under `$`,
> reading `openclaw.json` (read-only) to tell which auth profiles are plan logins.

`--source auto` (the default) reads OpenCode when its database is present, otherwise
falls back to the first present source (it never auto-merges). The active source shows
as a chip in the header, and you can **switch live with `c`** (OpenCode → Claude Code →
Codex → Copilot → pi → OpenClaw → all, for whichever are present). The whole TUI works the same — months,
days, projects, sessions, models, trends — with two differences, because **Claude Code,
Codex, and the Copilot CLI record only tokens, no per-message cost**:

- A Claude Code, Codex, or Copilot CLI session works like an OpenCode subscription session: it shows
  **$0 in normal mode** (nothing is recorded) and its **estimate** (tokens × API list
  price) under the **`$`** view. Since such a view would otherwise be a wall of `$0.00`,
  the estimate view **starts on by default** there (header tag:
  `ESTIMATED — tokens × API list prices`); press `$` to see the recorded numbers, and
  your choice is remembered.
- Projects roll up to their **git root**, so sessions started in subdirectories
  (`frontend/`, `src/`, …) group under the repo instead of bare folder names.

`--source all` merges every present source into one view: the same repo worked in
multiple tools rolls up into a single project row, every session row shows its origin (a
`Src` column in the session tables, `[oc]` / `[cc]` / `[cx]` / `[cp]` / `[pi]` / `[ocl]` tags elsewhere), and the
Trends overlay gains a **Sources** tab (spend by tool). `$` reprices the unpriced usage
across all of them — OpenCode's subscription/credit messages plus all of Claude Code's,
Codex's, and the Copilot CLI's (pi and OpenClaw carry their own per-message cost on metered routes like
OpenRouter or a direct API key; their subscription/OAuth routes such as openai-codex are estimated like the rest). (When more than one source is present, `--demo` **defaults to this merged
view** — it shows off the most — and anonymizes every backend under a single shared
scale so the cross-tool proportion stays truthful.)

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
| `f` | Live fuzzy filter: the lists narrow and re-rank (best match first) as you type, fzf-style subsequence matching over title/project/id; `↑`/`↓` select while typing, `Enter` keeps the filter, `Esc` cancels, `Ctrl-U` clears the input, `x` clears it later |
| `T` | Trends overlay — Daily / Weekly / Monthly cost charts + Model, Provider, and Source spend ranking (`h`/`l` tabs, `j`/`k` month/week, `$` toggles what-if) |
| `$` | What-if pricing: re-price unpriced subscription/credit usage at models.dev API list prices |
| `P` | Show the models.dev API price table OpenTab uses for `$` |
| `e` | Export the current list (months/days/projects/sessions/subagents) to a CSV in the working dir |
| `y` | Copy the selected session id (or project path) to the clipboard |
| `o` | Open the selected session's / project's directory |
| `L` | Launch the selected session in its own tool (`opencode --session <id>` / `claude --resume <id>` / `codex resume <id>`). Inside tmux a one-key menu opens it in a new **w**indow, **s**plit, **v**split, or **p**opup (cd'd to the project); outside tmux (or with `y`) the `cd <project> && …` command is copied to the clipboard instead. See [Custom launchers](#custom-launchers) to route launches through your own tooling |
| `c` | Switch data source: OpenCode / Claude Code / Codex / all (when more than one is present) |
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

OpenTab uses Python's `curses`, which native Windows Python doesn't bundle. Two
ways to run it:

**Native Windows (cmd / PowerShell).** Install the curses shim once, then run
OpenTab normally:

```sh
pip install windows-curses
opentab
```

That's the one exception to "no `pip install`": `windows-curses` is just an
OS-level provider for the stdlib `curses` module, not a dependency of OpenTab's
own code. Confirmed working against the **OpenCode** source; the Claude Code and
Codex backends read plain JSON files and should behave the same, but are less
exercised on native Windows.

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

If `curses` is missing, OpenTab prints a short hint (install `windows-curses`)
instead of crashing.

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
what `$0.00` subscription/credit usage _would have cost_ at published API list
prices. Press `P` to see the exact per-model rates behind that estimate. The
estimate uses an embedded table generated from models.dev for Anthropic, OpenAI,
and Google, with hand-kept family fallbacks for version or suffix churn and a
mid-range fallback for unknown models. Nothing is fetched at runtime, so the TUI
stays single-file, offline, and standard-library only. To refresh the embedded
table, run `python3 scripts/update_prices.py` and commit the changed `opentab`
file.

## License

MIT — see [LICENSE](LICENSE).
