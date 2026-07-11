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
coding tools already keep on disk and shows where your tokens and money went: by month,
day, project, session, and model, down to the subagent tree. Browse one tool at a time,
or merge them all.

Your tools already keep this ledger; OpenTab is just the reader. No backend, no telemetry,
no accounts — it opens those files **read-only**. Standard-library-only at runtime
(`curses` + `sqlite3`): `pipx install opentab-ai` and there's nothing else to pull in.

## Features

- **One tab for every tool** — [OpenCode](https://opencode.ai),
  [Claude Code](https://claude.com/claude-code), [Codex](https://developers.openai.com/codex),
  Hermes, GitHub Copilot (its CLI and Copilot Chat in VS Code), pi-agent, OpenClaw,
  [zaly](https://github.com/folke/zaly), and
  CSV/JSONL logs of your own API requests — [each detailed in the docs](docs/sources.md).
- **Drill, don't scroll** — month → day → project → session → model, down the recursive
  subagent tree, with a live fuzzy filter (fzf-style) and live date-range scoping.
- **Trends** — daily / weekly / monthly charts, a calendar spend heatmap, and model /
  provider / source rankings; every one navigable down to a single session.
- **Turns and Tools** — per-turn cost over time inside a session, and token attribution
  per tool call.
- **Honest `$` what-if** — subscription usage shows its true `$0`, and `$` reprices it at
  API list rates; `P` shows the exact per-model table behind the estimate.
- **A web twin** — the same browser as one self-contained HTML file (`--html`), or served
  live with per-session drill-in (`--serve`, `--web`).
- **Lazygit-style driving** — keyboard and mouse: scroll, click to select, double-click to
  drill, click a column header to sort.
- **Themes** — Catppuccin, Tokyo Night, Gruvbox, Nord, Dracula, Rosé Pine and more, light
  and dark, shared by the TUI (`C`) and the web page.
- **Quality of life** — git worktrees fold into their repo, CSV export of any view, and
  your source, range, sort, and `$` view are remembered between runs.
- **Private by construction** — local-only, read-only, no telemetry, no accounts; a demo
  mode anonymizes everything for screenshots and live demos.

## Install

Python **3.9+** and a terminal — nothing else. Already true on macOS, Linux, and WSL;
native Windows works too (see [Windows](#windows)).

Try it first, nothing installed:

```sh
uvx --from opentab-ai opentab --demo     # or: pipx run --spec opentab-ai opentab --demo
```

`--demo` runs the full TUI on your real usage, anonymized in memory — titles, paths, and
absolute numbers replaced with synthetic ones — so trying it out (and sharing the screen)
is safe. It reads your tools' own records, so it needs at least one AI coding tool's
history on disk. Drop `--demo` to see the real numbers.

Then install for real:

```sh
pipx install opentab-ai
```

The PyPI distribution is **`opentab-ai`**; the command it installs is **`opentab`**.
Upgrade later with `pipx upgrade opentab-ai`.

<details>
<summary><strong>Other ways to install</strong> — Homebrew, install script, pip, from source</summary>

**Homebrew (macOS / Linux):**

```sh
brew install hamidi-dev/tap/opentab      # upgrade later with `brew upgrade opentab`
```

**Install script** (installs via pipx; re-run to update):

```sh
curl -fsSL https://raw.githubusercontent.com/hamidi-dev/opentab/main/install.sh | bash
```

**pip:** plain `pip install --user opentab-ai` works too.

**From source:**

```sh
git clone https://github.com/hamidi-dev/opentab && cd opentab
pipx install .        # or `pip install -e .` for a live-editable checkout
```

</details>

## Usage

```sh
opentab                          # open the browser, all time
opentab --days 30                # start within a window (rescope live with R)
opentab --since 2026-05-01 --until 2026-05-31
opentab --source claude          # one tool only (switch live with c)
opentab --demo                   # safe for live demos / screenshots
opentab --web                    # the same browser, in your web browser
```

Everything is discoverable in-app — **`?` shows the full keymap**, every panel and
overlay documented. The full reference lives in **[docs/](docs/README.md)**: data
sources, keys, pricing, the web browser, Windows/WSL, and privacy.

## Data sources

OpenTab reads the local records each AI coding tool keeps. Pick one with `--source`,
point its flag at a non-default location, or just pass a file path (`opentab
requests.csv`, `opentab path/to/opencode.db`) and the source is inferred. `--source
auto` (the default) restores your last-used source, else **merges every present
source** when more than one exists; **switch live with `c`**.

Every source feeds the same browser — months, days, projects, sessions, models, trends.
What each tool's records support on top:

| Source | Cost | Subagent tree | Turns | Tools |
|--------|------|:---:|:---:|:---:|
| OpenCode | real recorded | ✓ | ✓ | ✓ |
| Claude Code | tokens only — `$` estimates | ✓ | ✓ | ✓ |
| Codex CLI | tokens only — `$` estimates | ✓ | ✓ | ✓ |
| Hermes Agent | mixed — metered real, rest estimated | ✓ | — | — |
| GitHub Copilot CLI | tokens only — `$` estimates | — | ✓ ¹ | — |
| Copilot Chat in VS Code | tokens only — `$` estimates | — | ✓ | — |
| pi-agent | mixed — metered real, rest estimated | — | ✓ | ✓ |
| OpenClaw | mixed — metered real, rest estimated | — | ✓ | — |
| zaly | mixed — metered real, rest estimated | — | ✓ | ✓ |
| CSV / JSONL request logs | mixed — per-row cost column | — | ✓ | ✓ ² |

<sub>**Subagent tree** — recursive per-subagent cost under the session that delegated ·
**Turns** — the per-turn cost timeline inside a session · **Tools** — token attribution
per tool call and MCP server · ¹ headerless: the OTEL export captures no prompt text ·
² with the optional `tool` column.</sub>

**[docs/sources.md](docs/sources.md)** has the full detail per source — where each
tool's records live, its flags and env vars, how cost is derived, quirks (Copilot's
opt-in OTEL export, Codex's cumulative counters, the pi/OpenClaw/zaly
metered-vs-subscription split, …), the CSV/JSONL schema, and the merged
`--source all` view.

## Keys

OpenTab opens on a stacked **Months / Days** (or Projects) sidebar, lazygit-style:
drill from a month or day into its detail tabs, from the Sessions tab into a
single session — cost split, model mix, subagent tree — and step back out with
`Esc`. The short version:

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
| `L` | Relaunch the session in its own tool — tmux window/split/popup, or [your own launcher](docs/keys.md#custom-launchers) |
| `e` / `o` | Export the current view to CSV / open the project's directory |
| `?` / `q` | Help / quit |

The active **source, range, sort, ignored projects, and `$` what-if view are
remembered between runs** (stored in `~/.config/opentab/state.json`; pass `--no-state`
to disable, and `--demo` does not persist). The complete keymap — bookmarks, ignore
lists, the sort picker, overlay keys, custom launcher hooks — is in
**[docs/keys.md](docs/keys.md)**.

## Web browser (`--html` / `--serve` / `--web`)

`opentab --html` writes the whole browser as **one self-contained HTML file** — no
server, no dependencies, works from disk or any static host. It's the TUI in the
browser: the same sidebar, detail tabs, Trends and price-table overlays, live range
scoping and colour themes, driven by the same keys or the mouse, with every view a
shareable deep link. `opentab --serve` serves it live on `http://localhost:8321`
and adds the per-session Turns/Tools drill-in; `opentab --web` also opens it in
your default browser. Details, deep links, and security notes:
**[docs/web.md](docs/web.md)**.

## Demo mode

`opentab --demo` is for showing the tool to other people without leaking your real
work: session titles and project paths become deterministic, plausible fakes, and
sessions recorded with no cost get a synthetic price derived from their real token
counts — all transformed in memory on load, nothing written back. The *shape* of your
data stays real (the proportions between sessions and months, the model mix), the
absolute numbers do not, and a `DEMO — synthetic` header tag keeps synthetic figures
from ever being mistaken for real ones.

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

## A note on cost accuracy

The numbers come straight from each tool's own data (cost/tokens per message, rolled up
per session) — *local attribution* of what your tools recorded. Some sessions show tokens
with a `$0.00` local cost: the usage was recorded but no per-token price, normal whenever
billing isn't per token (subscription plans, credit/token plans). That money isn't
missing, it's billed elsewhere — by your subscription or account credits — so OpenTab
surfaces it as "unpriced tokens" rather than guessing.

Press `$` (non-demo) for the **what-if** view: real recorded spend plus what `$0.00`
subscription/credit usage _would have cost_ at published API list prices, from a
**models.dev snapshot bundled with each release** — nothing is fetched at runtime, so
the TUI stays offline. `P` shows the exact per-model rates behind it, including the
**whole models.dev catalog** blended to one `eff $/M` figure at *your* token mix.
How the estimate is priced, the `P` views, pinning, and refreshing rates
(`--refresh-models`): **[docs/pricing.md](docs/pricing.md)**.

## What it touches

Local-only, no network, no telemetry, no accounts — it opens every source file
**read-only**, so it doesn't modify any of them. It writes only its own files (prefs
and caches under `~/.config/opentab/`, plus the CSV/HTML exports you explicitly ask
for), and runs external programs only on the key you press. The full list of
everything it reads, writes, and runs: **[docs/privacy.md](docs/privacy.md)**.

## Windows

OpenTab uses Python's `curses`, which native Windows Python doesn't bundle — so
`opentab-ai` declares `windows-curses` as a Windows-only dependency and pipx pulls
it in for you: `pipx install opentab-ai` and run. Under WSL, `curses` is already
there, so a plain `opentab` works — and it can read the Windows-side OpenCode
database and VS Code store through `/mnt/c`. Details: **[docs/windows.md](docs/windows.md)**.

## Development

CI runs Ruff, unit tests, and ShellCheck. See [CONTRIBUTING.md](CONTRIBUTING.md) for local
setup, the test/lint commands, the pre-push hooks, and commit conventions, and
[docs/architecture.md](docs/architecture.md) for how the code is put together.

## License

MIT — see [LICENSE](LICENSE).
