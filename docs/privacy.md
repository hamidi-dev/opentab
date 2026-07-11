# Privacy — what it touches

Local-only, no telemetry, no accounts — OpenTab opens every source file
**read-only**, so it doesn't modify any of them. Your tools already keep the ledger;
OpenTab is just the reader.

## Everything it reads

Your tools' own records, read-only:

- OpenCode's SQLite database
- the JSONL transcripts of Claude Code / Codex / pi-agent / OpenClaw / zaly
- Hermes' SQLite database
- the Copilot CLI's OpenTelemetry export
- VS Code's Copilot Chat session store
- a CSV/JSONL of logged API requests (`--csv`/`--jsonl`)

To fold git worktrees into their main repo it also reads project `.git` files (no
`git` process is spawned; disable with `--no-worktrees`).

## Everything it writes

Nothing near your tools' data — only its own files:

- `~/.config/opentab/state.json` — a small preferences file (your last source,
  range, and sort; disable with `--no-state`).
- `~/.config/opentab/prices.json` — the optional model-price cache, written **only**
  when you run `--refresh-models` or press `r` in the `P` overlay.
- `~/.config/opentab/cache/` — a warm-start rollup cache, one JSON per backend,
  rewritten after a parse when that backend's files change (off under
  `--demo`/`--no-cache`). It never changes what you see — a stale rollup is never
  shown.
- Only when you ask: an `opentab-*.csv` export (on `e`) or the HTML browser file
  (on `--html`) in the current directory.

## Network

None, by default. The one time runtime OpenTab touches the network is the explicit
price refresh (`--refresh-models`, or `r` in the `P` overlay) — a single fetch of
models.dev list prices with stdlib `urllib`, written to the local cache above. The
bundled price snapshot serves everything otherwise. See
[Pricing](pricing.md#refreshing-rates).

## External programs

Run only on the key you press: your file opener (`open`/`xdg-open`, or Explorer on
Windows) for `o`, and for `L` either `tmux`, your own
[launcher hook](keys.md#custom-launchers) (`~/.config/opentab/launcher`), or your
clipboard tool (`pbcopy`/`wl-copy`/`xclip`/`xsel`) for its copy target. All are
disabled in `--demo`.

## Demo mode

`opentab --demo` is for showing the tool to other people without leaking your real
work: session titles and project paths become deterministic, plausible fakes, and
sessions recorded with no cost get a synthetic price derived from their real token
counts — all transformed in memory on load, nothing written back. A single hidden
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
