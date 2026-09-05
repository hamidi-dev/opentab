# Working on OpenTab

OpenTab browses local AI usage and spend in a curses TUI and a self-contained web
report. It reads multiple harnesses through a shared store interface. The PyPI
distribution is `opentab-ai`; the import package and command are `opentab`.

## Read for the task

Start with [Architecture](docs/architecture.md) for the package map. Read the
relevant topic, not the entire documentation tree:

| Working on | Reference |
|------------|-----------|
| Setup, tests, commits, releases | [Contributing](CONTRIBUTING.md) |
| A store, token accounting, deduplication, subagents, retention | [Backend accounting](docs/backends.md); [harness setup and schemas](docs/sources.md) |
| Startup, reload, caches, `cost`, `--goto` | [Startup and caching](docs/caching.md) |
| TUI state, navigation, tables, colours, traces | [TUI internals](docs/tui.md); [user controls](docs/keys.md) |
| Costs, model rates, `$`, `P`, `w` | [Pricing](docs/pricing.md) |
| Notes, XDG files, demo, export privacy | [Privacy](docs/privacy.md) |
| HTML payload, browser state, live server | [Web browser](docs/web.md) |
| Fleet summaries, pulls, remote sessions | [Multiple machines](docs/machines.md) |
| Environment diagnosis, portability | [Troubleshooting](docs/troubleshooting.md); [Windows/WSL](docs/windows.md) |

## Non-negotiable constraints

- Python 3.9+. Standard library only at runtime, except Windows-only
  `windows-curses`. No new runtime dependencies.
- Harness databases, transcripts and auth files are read-only. Writes belong to
  OpenTab's own files or explicitly requested exports; use `paths.py` for XDG paths.
- Notes are authored data: locked read-modify-write, atomic save on each edit,
  no overwrite of malformed files, preserve unknown entries, never prune missing IDs.
- Keep imports acyclic and stores independent of the TUI. Use `TYPE_CHECKING` for
  annotation-only back-references; preserve the top-level public API exports.
- Recorded cost, API-equivalent estimates (`$`), and session-only rate comparisons
  (`w`) are distinct. Never turn `w` into an app-wide repricing mode.
- Keep the model scan deferred until after the first TUI paint. Session extras
  and content stay lazy; raw traces never enter rollup caches or web/fleet payloads.
- Demo must not expose real notes or traces, persist preferences, or launch/copy
  sessions. Treat selective anonymization as selective, not fully private.
- Do not hand-edit `src/opentab/data/models.json`; regenerate it with
  `scripts/update_prices.py`.

## Development loop

```sh
pip install -e .
python3 run_tests.py                     # custom stdlib runner, not pytest
python3 run_tests.py pricing             # module/test substring filter
ruff check src/opentab tests run_tests.py
ruff format --check src/opentab tests run_tests.py
python3 -m compileall -q src/opentab
python3 -m opentab --demo
```

Tests belong to the module they exercise; shared builders live in
`tests/_support.py`. `tests/__init__.py` isolates all XDG roots before imports.
Do not wrap fixed-width TUI strings to satisfy line length (`E501` is ignored).
Check both frontends when changing a feature they share.

Use Conventional Commits and the scope vocabulary in `CONTRIBUTING.md`; no AI
attribution or co-author trailers. Version is the manual `__version__` constant in
`src/opentab/__init__.py`, not derived from a tag.

## Keep this file small

This is a task router and a short set of guardrails, not a design history. Put
explanations in the relevant human-readable doc, tests next to the affected module,
and local implementation rationale beside the code. Update existing sections
instead of appending bug diaries or duplicating whole feature descriptions here.
