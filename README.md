<h1 align="center">OpenTab</h1>

<p align="center"><em>Your AI coding tools keep a tab. OpenTab opens it.</em></p>

<p align="center"><sub>Anonymized demo data — click the reel for the full-quality video.</sub></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/49531f8f-606d-4dea-9918-6588e3c3e69e"><img src="https://github.com/user-attachments/assets/2580e2ae-05ee-414a-98a6-a7005131dbc5" alt="OpenTab — trends, a calendar spend heatmap, drill-downs across OpenCode / Claude Code / Codex, and live theming" width="900"></a>
  <br><sub><b>One reel, every view</b> — trends, a calendar spend heatmap, drill from a month down to a single session, and live theming</sub>
</p>

<p align="center">
  <a href="https://www.youtube.com/watch?v=EsJPw4y5zgU"><img src="https://i.ytimg.com/vi/EsJPw4y5zgU/maxresdefault.jpg" alt="Watch the full OpenTab tour on YouTube" width="900"></a>
  <br><sub><b>▶ The full tour on YouTube</b> — every view, walked through</sub>
</p>

<p align="center">
  <img src="https://github.com/user-attachments/assets/b497c617-8a6c-4132-b6e2-aaf5078b8a4e" alt="OpenTab web browser — the same data as a self-contained page" width="900">
  <br><sub><b>Also a web browser</b> — <code>opentab --web</code> renders the same data as one self-contained, shareable page</sub>
</p>

A terminal browser for your AI coding spend. It reads the records your tools already keep
on disk — a dozen of them, from OpenCode and Claude Code to Codex and Copilot — and shows
where the tokens and the money went: by month, day, project, session, and model, all the
way down the subagent tree. One tool at a time, or all of them merged.

Plenty of things will print you a token total. This one is built to be *poked at*: drill
in, filter as you type, rescope the dates, sort, keyboard or mouse. And it's just a
reader — no backend, no telemetry, no account, every file opened **read-only**. The
runtime is the Python standard library, so `pipx install opentab-ai` pulls in nothing else.

## Install

Python **3.9+** and a terminal — macOS, Linux, WSL, and native Windows. The PyPI package
is **`opentab-ai`**; the command it installs is **`opentab`**.

```sh
pipx install opentab-ai                     # recommended
brew install hamidi-dev/tap/opentab         # Homebrew (macOS / Linux)
pip install --user opentab-ai               # pip
curl -fsSL https://raw.githubusercontent.com/hamidi-dev/opentab/main/install.sh | bash
```

Upgrade with `pipx upgrade opentab-ai`, `brew upgrade opentab`, or
`pip install -U --user opentab-ai`.

## Usage

```sh
opentab                          # the browser, all time
opentab web                      # the same browser, in your web browser
opentab --demo                   # anonymized — safe for screenshots and live demos
opentab doctor                   # what's found, what isn't, and why
opentab pull laptop workstation  # every machine, merged into one tab
```

`?` shows the full keymap in-app; it's also in **[docs/keys.md](docs/keys.md)**, along with
the rest of the reference in **[docs/](docs/README.md)**.

## What you get

- **Drill, don't scroll** — month → day → project → session → model, down the recursive
  subagent tree, with a live fzf-style filter and live date-range scoping.
- **Trends** — daily / weekly / monthly charts, a calendar spend heatmap, and model /
  provider / harness rankings. Every one of them navigable down to a single session.
- **Inside a session** — *Turns*: what each prompt cost, in order, with the tool calls it
  made. *Tools*: token attribution per tool and MCP server, over a treemap where area is
  total spend and shade is cost *per call*. *Context*: the window filling up over time,
  compaction markers included, plus an estimate of what filled it.
- **Honest money** — subscription usage shows its true `$0`; **`$`** reprices it at API
  list rates and **`P`** shows the per-model table behind the estimate. **`w`** arms a
  what-if model: *what if the expensive one had done the subagents' work too?*
- **Every machine, one tab** — [`opentab pull`](#every-machine-one-tab) gathers each box's
  spend over SSH and merges it into one browser you can filter by machine.
- **A web twin** — `opentab web` serves the same browser live, `--html` writes it as one
  self-contained file: same keys, same overlays, every view a shareable deep link.
  [docs/web.md](docs/web.md)
- **Yours to drive** — lazygit-style keyboard *and* mouse, 30 bundled themes shared by the
  TUI and the web page, every key remappable, CSV export of any view, and your harness,
  range, sort and `$` view remembered between runs.
- **Private by construction** — local-only, read-only, no telemetry, no accounts.

## The tools it reads

[OpenCode](https://opencode.ai) · [Claude Code](https://claude.com/claude-code) ·
[Codex CLI](https://developers.openai.com/codex) ·
[GitHub Copilot](https://github.com/github/copilot-cli) (its CLI *and* Copilot Chat in
VS Code) · [pi-agent](https://pi.dev) · [omp](https://omp.sh) ·
[OpenClaw](https://github.com/openclaw/openclaw) ·
[zaly](https://github.com/folke/zaly) ·
[Gemini CLI](https://github.com/google-gemini/gemini-cli) ·
[Hermes](https://hermes-agent.nousresearch.com/) · and CSV/JSONL logs of your own API
requests.

Point it at nothing and `--harness auto` merges every tool it finds; `--harness NAME`
narrows to one, `H` switches live. You can also just hand it a file — `opentab
requests.csv`, `opentab path/to/opencode.db` — and the harness is inferred.

<details>
<summary><strong>What each tool's records support on top</strong> — cost, subagent tree, Turns, Tools, Context</summary>

| Harness | Cost | Subagent tree | Turns | Tools | Context |
|--------|------|:---:|:---:|:---:|:---:|
| OpenCode | real recorded | ✓ | ✓ | ✓ | ✓ |
| Claude Code | tokens only — `$` estimates | ✓ | ✓ | ✓ | ✓ |
| Codex CLI | tokens only — `$` estimates | ✓ | ✓ | ✓ | — |
| Hermes Agent | mixed — metered real, rest estimated | ✓ | ✓ | ✓ | ✓ |
| GitHub Copilot CLI | tokens only — `$` estimates | — | ✓ | — | ✓ |
| Copilot Chat in VS Code | tokens only — `$` estimates | — | ✓ | — | ✓ |
| pi-agent | mixed — metered real, rest estimated | — | ✓ | ✓ | ✓ |
| omp | mixed — metered real, rest estimated | ✓ | ✓ | ✓ | ✓ |
| OpenClaw | mixed — metered real, rest estimated | — | ✓ | ✓ | ✓ |
| zaly | mixed — metered real, rest estimated | — | ✓ | ✓ | ✓ |
| Gemini CLI | tokens only — `$` estimates | ✓ | ✓ | ✓ | ✓ |
| CSV / JSONL request logs | mixed — per-row cost column | — | ✓ | ✓ | ✓ |

<sub>**Subagent tree** — recursive per-subagent cost under the session that delegated ·
**Turns** — the per-turn cost timeline · **Tools** — token attribution per tool call and
MCP server · **Context** — the context-window growth curve (it rides on Turns). A `—` is
data the tool itself doesn't record, never a feature skipped; sources.md says which, and
why, for each one.</sub>

</details>

**[docs/sources.md](docs/sources.md)** has the rest: where every tool's records live, its
flags and env vars, how its cost is derived, the quirks (Copilot's opt-in export, Codex's
cumulative counters, the metered-vs-subscription split), the CSV/JSONL schema you can
write against, and the merged view.

## Every machine, one tab

Code on more than one box? **`opentab pull` gathers each machine's spend over SSH — all in
parallel — and opens them merged into one browser**, filterable and drillable by machine.

```sh
opentab pull laptop workstation gpu-box   # fetch all three, open the fleet
opentab pull                              # later: refresh every machine you've saved
```

**No agent, nothing to install, nothing listening** — the remote only needs `opentab` on
its `PATH`.

<details>
<summary><strong>How the pull works, and what the fleet adds</strong></summary>

Pull runs `opentab export -` on the far side, which prints that box's spend summary —
totals, per-model breakdown, Turns / Tools / Context, but **no transcripts** — and streams
it back over the SSH pipe. Each host is remembered, so a bare `opentab pull` refreshes the
lot; a host is any ssh target (`box`, `user@host`, `name=user@host` to label it) or a
`http://host:port` box already running `opentab web`.

Once pulled, the fleet is just another harness: **`M`** filters every view to one machine,
**`L`** reopens a session *on the box it ran on* over SSH, **`opentab remote`** reopens the
last pull with no SSH round-trip, and **`opentab forget <machine>`** drops one. Pair any of
it with `--demo` for a shareable fleet snapshot.

</details>

## Live prices in your sidebar

[**herdr-opentab**](https://github.com/hamidi-dev/herdr-opentab) puts what each running
agent has spent right next to it in the [Herdr](https://herdr.dev) sidebar — that pane's
own session, subagents included, kept up to date while it works.

<p align="center">
  <img src="https://raw.githubusercontent.com/hamidi-dev/herdr-opentab/main/docs/screenshot.png" alt="The Herdr sidebar with a price beside every agent" width="900">
  <br><sub>Amounts synthetic, everything else real.</sub>
</p>

## About the money

A session that shows tokens against `$0.00` isn't a bug: usage was recorded without a
per-token price, which is what a subscription or credit plan looks like from the outside.
That money isn't missing, it's billed elsewhere — so OpenTab reports **unpriced tokens**
rather than guessing. Press **`$`** and it reprices them at published API list rates, from
a models.dev snapshot bundled with each release (nothing is fetched at runtime); **`P`**
shows the rates behind the estimate. **[docs/pricing.md](docs/pricing.md)**

## Something looks off?

```sh
opentab doctor
```

Which copy of OpenTab is talking, every harness (found — or not found **and why**, with
the fix), your terminal's colour and glyph support, the price catalog, its own files. It
reports and never repairs, and it reads no transcript, so the output is safe to paste into
an issue as-is. **[docs/troubleshooting.md](docs/troubleshooting.md)**

## FAQ

<details>
<summary><strong>Does any of this leave my machine?</strong></summary>

No. No backend, no telemetry, no accounts, and nothing is fetched at runtime — the price
catalog ships bundled with each release. OpenTab opens your tools' files **read-only** and
writes only its own (prefs, notes, caches, plus the exports you ask for). External programs
run only on the key you press. The full list of everything it reads, writes and runs:
[docs/privacy.md](docs/privacy.md).

</details>

<details>
<summary><strong>Will the numbers match my provider's invoice?</strong></summary>

Only where your tool recorded real metered cost. Everything else is *local attribution* —
what your tools wrote down, rolled up per session — plus, under `$`, a list-price estimate
for unpriced tokens. An estimate is a yardstick, not a bill: it knows nothing of whatever
discount, plan or routing markup sits between you and the vendor.
[docs/pricing.md](docs/pricing.md)

</details>

<details>
<summary><strong>Can it damage my agents' history?</strong></summary>

It can't write to it. Every harness file — the OpenCode SQLite DB included — is opened
read-only; the only things OpenTab writes are its own config, prefs, notes, warm-start
cache and the CSV/HTML exports you explicitly ask for.

</details>

<details>
<summary><strong>My tool isn't supported. Anything I can do?</strong></summary>

If it can log its API requests, yes: point OpenTab at a CSV or JSONL of them
(`opentab requests.csv`) and you get the whole browser — Turns and Tools included, if your
log carries the optional columns. The schema is in
[docs/sources.md](docs/sources.md#schema). Otherwise, open an issue with a sample
transcript; each backend is one small parser against a fixed contract.

</details>

<details>
<summary><strong>Can I screenshot it without leaking client work?</strong></summary>

`--demo` (or `D` in-app) replaces titles, paths and absolute numbers with deterministic
fakes in memory, keeping the shape of the data real. Scramble only part of it with
`--demo titles,spend` — project paths are their own scope, so `--demo titles,turns,spend`
keeps your real project names on screen while everything else stays anonymous. Nothing is written back, and demo mode never persists state —
`opentab --demo --html demo.html` gives you a shareable page.

</details>

<details>
<summary><strong>Does it work on Windows?</strong></summary>

Yes. Native Windows Python doesn't bundle `curses`, so `opentab-ai` declares
`windows-curses` as a Windows-only dependency and pipx pulls it in for you. Under WSL a
plain `opentab` works — and it can read the Windows-side OpenCode database and VS Code
store through `/mnt/c`. [docs/windows.md](docs/windows.md)

</details>

<details>
<summary><strong>I switch themes with <code>C</code> and nothing changes.</strong></summary>

Your terminal is accepting palette writes and quietly ignoring them. Known hosts are
detected automatically; anywhere else, `export OPENTAB_NO_INIT_COLOR=1` for the nearest
256-colour fallback (matched in CIE Lab, so hues survive). Why it's an env var and not a
flag: [docs/troubleshooting.md](docs/troubleshooting.md#every-theme-looks-the-same).

</details>

## Development

CI runs Ruff, unit tests, and ShellCheck. [CONTRIBUTING.md](CONTRIBUTING.md) has the local
setup, test/lint commands, hooks and commit conventions;
[docs/architecture.md](docs/architecture.md) has how the code is put together.

## License

MIT — see [LICENSE](LICENSE).
