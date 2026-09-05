# Privacy — what it touches

Local by default, no telemetry, no OpenTab account — OpenTab opens every harness file
**read-only**, so it doesn't modify any of them. Your tools already keep the ledger;
OpenTab is just the reader.

## Everything it reads

Your tools' own records, read-only:

- OpenCode's SQLite database
- the JSONL transcripts of Claude Code / Codex / pi-agent / omp / OpenClaw / zaly
- Hermes' SQLite database and rotating agent logs
- the Copilot CLI's OpenTelemetry export
- VS Code's Copilot Chat session store
- Gemini CLI's chat records and project mappings, and Antigravity's conversation records
- a CSV/JSONL of logged API requests (`--csv`/`--jsonl`)

Discovery and diagnostics also inspect settings such as transcript retention. See
[Sources](sources.md) for paths and overrides; these files are never changed.

To tell a **subscription** route from a **metered** one — the difference between $0 and
real spend — it also reads how each provider is logged in, from pi's `auth.json`, zaly's
`auth.json`, OpenClaw's `openclaw.json`, and omp's `agent.db`. The JSON files are parsed
as whole documents, but only provider names and login types (`oauth` vs API key) are
used. Credentials are not used to authenticate, retained in usage data, displayed or
exported. For omp, the SQL query selects only provider and credential-type columns,
not the column containing secrets.

To fold git worktrees into their main repo it also reads project `.git` files (no
`git` process is spawned; disable with `--no-worktrees`).

## Everything it writes

Nothing near your tools' data — only its own files, split across the standard XDG
base directories (each honors an **absolute** `XDG_*_HOME` override; relative values
are ignored, and the defaults are shown):

- **Config** — `~/.config/opentab/`: `keymap.conf` (your key bindings), `remotes.json`
  (the saved machine list for `--pull`/`--remote`), and an optional `launcher` hook.
- **State** — `~/.local/state/opentab/state.json`: a small preferences file (your last
  harness, range, and sort; disable with `--no-state`).
- **Data** — `~/.local/share/opentab/notes.json`: your session notes, saved on every
  edit, plus the `notes.json.lock` sidecar used to coordinate writers.
- **Cache** — `~/.cache/opentab/`: `cache/` (a warm-start rollup, one JSON per backend,
  rewritten after a parse when that backend's files change — off under
  `--demo`/`--no-cache`). Changed file fingerprints trigger rebuilding, not a stale
  preview; [fingerprint limitations](caching.md#when-a-splice-must-fall-back) still
  apply to rewritten history. This directory also holds `prices.json` (the optional
  model-price cache, written **only** on an explicit refresh) and `remotes/`
  (summaries pulled from other machines). Local rollups and prices can be regenerated; deleting pulled
  summaries removes offline history until you pull or copy them again.
- Only when you ask: an `opentab-*.csv` export (on `e`, in the current directory),
  an HTML report (`opentab web --html FILE`), or a machine summary
  (`opentab export FILE`; stdout when no file is supplied).

Upgrading from a version that kept everything under `~/.config/opentab/`? The first run
attempts to relocate state, notes and caches to their new homes. Migration is
best-effort, not a guarantee that the old directory will be emptied: state and notes
readers can fall back to an intact legacy file if migration fails. Existing files at
the new locations take precedence. Legacy `requests.csv` / `requests.jsonl` logs are
not moved and remain discoverable when no counterpart exists in the data directory.

### Notes are authored data

`n` edits a session note; saving an empty note removes that note. Notes are disabled
under `--demo` and `--no-state`. Each edit re-reads the file, merges just that session's
change, and atomically replaces the file rather than waiting until quit. The stable
`notes.json.lock` sidecar protects the whole read-modify-write on systems supporting
advisory locks; locking is best-effort elsewhere, including native Windows.

An unreadable or malformed notes file is **not treated as an empty notebook**:
OpenTab reports the problem and refuses to overwrite it. Unknown entries in the
`notes` map survive edits, and missing session IDs are never garbage-collected, even
after transcripts rotate away. Back up `notes.json`; unlike a rollup cache, it cannot
be rebuilt from harness records.

## Network

No outbound requests by default. Network activity is explicitly requested:

- Price refresh (`--refresh-models`, or `r` in the `P` overlay) fetches models.dev
  list prices into the local cache. Otherwise the bundled snapshot suffices. See
  [Pricing](pricing.md#refreshing-rates).
- `opentab pull` and fleet refresh fetch summaries over SSH or HTTP(S). Resuming a
  remote session can also invoke SSH. See [Multiple machines](machines.md).
- `opentab web` starts a local HTTP server; browser requests load the report and
  session details. Binding beyond localhost exposes that service to other machines.

## External programs

Only for the action you request:

- `o` invokes the platform file opener for a project directory.
- `L` invokes tmux, Herdr's CLI, or your [launcher hook](keys.md#custom-launchers)
  (`~/.config/opentab/launcher`); its copy target invokes a clipboard tool.
  Session launch/copy and directory opening are disabled in demo mode.
- `K` opens `keymap.conf` using `$VISUAL` or `$EDITOR`, falling back to `vi`, and
  reloads bindings on return. This is a configuration edit, not a session launch.
- SSH pulls run the saved remote export command, including a custom `cmd` if set.
- `opentab web` asks the default browser to open the report; `web --headless` does not.

Launcher hooks, editors and custom remote commands are programs you choose, not
sandboxed OpenTab operations. Configure only commands you trust.

## Demo mode

`opentab --demo` selectively disguises your real data for demonstrations; it is
**not a fully private or synthetic dataset**. With all categories selected, session
titles and project paths become deterministic, plausible fakes, and unpriced usage
gets a synthetic price derived from its real token counts. These transformations
happen in memory, never in the source records. Demo splits into four
independent scopes — `titles` (session/prompt/model/machine names), `paths` (project
directories), `turns` (the expandable full prompt text) and `spend` (dollars and token
magnitudes) — so `--demo titles,turns,spend` (or unchecking **Paths** in the `D` picker)
keeps your real project paths. Only selected categories are transformed. With
`spend` enabled, a hidden per-process factor scales costs and token counts rather
than showing their real magnitudes.

The *shape* of your data stays real, including activity dates and relationships
between sessions; unselected categories stay real too. A `DEMO — synthetic` header
identifies the mode. Real notes and raw turn traces are unavailable, session
launch/copy and directory opening are disabled, and demo preferences are not saved.
Inspect any screenshot or exported file before sharing it: demo is not a promise
that every identifying field has been removed.

## The served browser

`opentab web` binds to localhost by default. Reports include session titles,
project paths and spend; live session details and fleet JSON can also contain
**full user prompts**. Raw turn traces (assistant narration, reasoning and tool
arguments/results) and authored notes are absent from web and fleet payloads.
This does not make those payloads anonymous. TUI CSV exports can include notes.
`--bind` warns beyond localhost; see [the web browser](web.md#security).
