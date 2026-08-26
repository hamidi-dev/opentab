# Privacy — what it touches

Local-only, no telemetry, no accounts — OpenTab opens every harness file
**read-only**, so it doesn't modify any of them. Your tools already keep the ledger;
OpenTab is just the reader.

## Everything it reads

Your tools' own records, read-only:

- OpenCode's SQLite database
- the JSONL transcripts of Claude Code / Codex / pi-agent / omp / OpenClaw / zaly
- Hermes' SQLite database and rotating agent logs
- the Copilot CLI's OpenTelemetry export
- VS Code's Copilot Chat session store
- a CSV/JSONL of logged API requests (`--csv`/`--jsonl`)

To tell a **subscription** route from a **metered** one — the difference between $0 and
real spend — it also reads how each provider is logged in, from pi's `auth.json`, zaly's
`auth.json`, OpenClaw's `openclaw.json`, and omp's `agent.db`. Only the provider name and
its login *type* (`oauth` vs API key) are read; the access and refresh tokens stored
alongside them are never read, copied, or shown.

To fold git worktrees into their main repo it also reads project `.git` files (no
`git` process is spawned; disable with `--no-worktrees`).

## Everything it writes

Nothing near your tools' data — only its own files, split across the standard XDG
base directories (each honors its `XDG_*_HOME` override; the defaults are shown):

- **Config** — `~/.config/opentab/`: `keymap.conf` (your key bindings), `remotes.json`
  (the saved machine list for `--pull`/`--remote`), and an optional `launcher` hook.
- **State** — `~/.local/state/opentab/state.json`: a small preferences file (your last
  harness, range, and sort; disable with `--no-state`).
- **Data** — `~/.local/share/opentab/notes.json`: your session notes, the one thing you
  authored — saved on the edit and never pruned.
- **Cache** — `~/.cache/opentab/`: `cache/` (a warm-start rollup, one JSON per backend,
  rewritten after a parse when that backend's files change — off under
  `--demo`/`--no-cache`, and it never changes what you see: a stale rollup is never
  shown), `prices.json` (the optional model-price cache, written **only** when you run
  `--refresh-models` or press `r` in the `P` overlay), and `remotes/` (summaries pulled
  from other machines). Cache files are safe to delete; opentab regenerates them.
- Only when you ask: an `opentab-*.csv` export (on `e`) or the HTML browser file
  (on `--html`) in the current directory.

Upgrading from a version that kept everything under `~/.config/opentab/`? The first run
relocates it all automatically — `state.json` and `notes.json` to their new homes, and
the caches into `~/.cache/opentab/` — leaving the old config dir holding only real config
(`keymap.conf`, `launcher`, `remotes.json`). Nothing is lost.

## Network

None, by default. The one time runtime OpenTab touches the network is the explicit
price refresh (`--refresh-models`, or `r` in the `P` overlay) — a single fetch of
models.dev list prices with stdlib `urllib`, written to the local cache above. The
bundled price snapshot serves everything otherwise. See
[Pricing](pricing.md#refreshing-rates).

## External programs

Run only on the key you press: your file opener (`open`/`xdg-open`, or Explorer on
Windows) for `o`, and for `L` either `tmux`, `herdr`, your own
[launcher hook](keys.md#custom-launchers) (`~/.config/opentab/launcher`), or your
clipboard tool (`pbcopy`/`wl-copy`/`xclip`/`xsel`) for its copy target. All are
disabled in `--demo`.

When Herdr is selected, OpenTab invokes Herdr's CLI: `herdr tab create` or
`herdr pane split`, reads `result.root_pane.pane_id` or `result.pane.pane_id` from the
returned JSON, and then invokes `herdr pane run`. `herdr pane run` internally uses the
same input semantics as `pane.send_input`. OpenTab cannot create or control popups through
Herdr's general pane CLI/API, so it does not offer them; it only uses the CLI and never
opens a Herdr socket. It does not call the socket operations `layout.apply` or
`pane.current`.

## Demo mode

`opentab --demo` is for showing the tool to other people without leaking your real
work: session titles and project paths become deterministic, plausible fakes, and
sessions recorded with no cost get a synthetic price derived from their real token
counts — all transformed in memory on load, nothing written back. It splits into four
independent scopes — `titles` (session/prompt/model/machine names), `paths` (project
directories), `turns` (the expandable full prompt text) and `spend` (dollars and token
magnitudes) — so `--demo titles,turns,spend` (or unchecking **Paths** in the `D` picker)
keeps your real project names while hiding everything else. A single hidden
per-process factor scales every cost and token count, so token × list-price can't
recover your real dollars.

The *shape* of your data stays real (the proportions between sessions and months,
the model mix), the absolute numbers do not, and a `DEMO — synthetic` header tag
keeps synthetic figures from ever being mistaken for real ones. Demo mode never
persists state and disables the clipboard/file-opener side effects.

## The served browser

`--serve`/`--web` bind to localhost only by default — the page shows prompt titles,
project paths, and spend. `--bind` warns beyond localhost; see
[the web browser](web.md#security).
