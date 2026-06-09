# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OpenTab is a single-file, zero-dependency terminal UI (curses) that reads OpenCode's
SQLite database **read-only** and browses your AI spend by month / day / project /
session / model, including the recursive subagent tree. Standard library only — no
`pip install` at runtime. The entire program is the executable `opentab` (a Python
script, no `.py` extension).

## Commands

```sh
python3 test_opentab.py                 # run the whole unit suite (custom runner, not pytest)
python3 -c "import test_opentab as t; t.test_trend_daily_shows_one_navigable_month()"  # run one test
ruff check opentab test_opentab.py      # lint (matches CI)
ruff format opentab test_opentab.py     # autoformat
ruff format --check opentab test_opentab.py   # format check (matches CI)
python3 -m py_compile opentab           # byte-compile smoke check
python3 opentab --demo                  # run the TUI with anonymized/synthetic data
```

`test_opentab.py` is **not** pytest — it has its own runner at the bottom that just runs
every `test_*` function in sorted order (no name filtering, no argv). To run a single
test, import it and call it directly (as above), or use the locally-installed `pytest`
(`pytest test_opentab.py -k NAME`) which also discovers these functions. CI runs ruff,
`python3 test_opentab.py`, and shellcheck on `install.sh`. Install dev hooks with
`git config core.hooksPath hooks` (the pre-push hook runs the same checks).

`ruff.toml` deliberately ignores `E501` (long lines): the f-strings build fixed-width
TUI columns, so do **not** wrap them to satisfy line length.

## Hard constraints

- **Standard library only at runtime.** `curses` + `sqlite3` + stdlib. Never add a
  third-party import to `opentab`. ruff is dev-only tooling.
- **Single file.** All program logic lives in `opentab`. Keep it that way.
- **Read-only on the OpenCode DB.** The tool opens the database read-only and must
  not write to it. The only files it writes are `~/.config/opentab/state.json` (prefs)
  and `opentab-*.csv` exports (on `e`).
- **Python 3.9+.** `MIN_PYTHON = (3, 9)`; `target-version = py39`. Don't use newer syntax.

## Architecture

Three layers, all in `opentab`:

- **`Store`** — owns the sqlite connection and every SQL query; returns plain
  `Workflow`/`sqlite3.Row` data. It is **schema-adaptive**: OpenCode's schema varies by
  version, so `Store` probes columns (`_has_session_token_columns`, `_has_session_cost_column`,
  `_needs_message_usage`) and builds cost/token SQL expressions dynamically
  (`_cost_expr`, `_token_exprs`, `_message_usage_cte/_join`). When per-session cost
  columns are absent it falls back to aggregating the `message` table. Always go through
  these helpers when touching SQL — never hard-code column names.
- **`App`** — all state and the keyboard/mouse state machine; stays curses-free except
  the modal prompt line. Holds the view stack and selection indices.
- **`Renderer`** — all drawing. `Renderer.__getattr__` delegates unknown attributes to
  its `App`, so renderer methods read app state directly as if it were `self`. Drawing
  methods return `list[str]` of plain text; `write_rich` re-colors money/token spans by
  regex at paint time (so a line is just a string until it hits the screen).

### View state machine (`App`)

`self.view` is `"browse"` → `"zoom"` → `"session"` (drill in with Enter/`+`, out with
Esc). `self.browse_mode` is `"time"` (Months/Days sidebar, `self.focus` flips between
them) or `"projects"`. Overlays are separate booleans on top of any view: `self.trends`
(T), `self.help` (?), `self.show_prices` (P). Detail tabs per zoom level are the class
tuples `month_tabs`/`day_tabs`/`project_tabs`/`workflow_tabs`, indexed by `self.tab`.

### Data flow & the deferred model scan

- `App.__init__` loads `store.workflows()` (fast per-root session rollup) so the first
  frame paints immediately. The **per-model breakdown** (`store.model_breakdown()`) is the
  one heavy scan over the whole `message` table and is **deferred**: `run()` loads it via
  `_load_model_cache()` right after the first paint but before any key is handled. Until
  then `_model_by_root` is empty and `model_mix()` tolerates that. Don't move this scan
  into `__init__`.
- The breakdown is computed once for every root session and cached in `_model_by_root`,
  then sliced per session/day/month (`model_mix`, `aggregate_models`) — never re-queried
  per workflow.
- Subagent costs are recursive: `workflow_nodes` walks `session.parent_id` with a
  recursive CTE so a root session's cost includes its whole subagent subtree.
- Range/projection: `ranged_workflows` (date-filtered) → `all_workflows` (also drops
  ignored projects) are cached properties; mutating range/ignored state must call
  `_invalidate_workflow_cache()`.

### The `$` what-if pricing model

Every `Workflow` carries two cost snapshots: real recorded cost and an API-equivalent
(real spend + what `$0.00` subscription/credit tokens *would* cost at list prices).
`$` (`toggle_api_prices`) swaps which one every panel reads via `_apply_price_mode()`;
`_snapshot_real_costs` and `_compute_api_costs` build the two sets. Prices come from
`MODEL_PRICE_TABLE`, a **generated** block between the `BEGIN/END GENERATED PRICES`
markers — regenerate with `python3 scripts/update_prices.py` and commit the changed
`opentab`; never hand-edit that block. `model_price()` adds family fallbacks for
version/suffix churn. `P` shows this table; nothing is fetched at runtime.

### Demo mode (`--demo`)

`Store` transforms rows in memory on load: `demo_title`/`demo_dir`/`demo_model` produce
deterministic fakes, `demo_cost` synthesizes prices for `$0.00` rows, and a single hidden
per-process factor scales every cost/token so token×list-price can't recover real dollars.
Demo never persists state and disables clipboard/file-opener side effects. The data's
*shape* (proportions, model mix) stays real; absolute numbers do not.

## Conventions seen in the code

- `money()` renders sub-cent nonzero costs as `<$0.01` so they're never confused with a
  red `$0.00`, which specifically means *unpriced* (tokens with no local price). Preserve
  this distinction when touching cost formatting.
- The "Models" detail tab and the Overview "Top Models" section now share `_model_table`
  (same columns: Model · Msgs · Cost · Share · Tokens · CacheR · CacheW · Output), fed by
  `_agg_rows`/`_mix_rows`. Keep them rendering through that one helper.
- Versioning is a manual constant: `__version__` in `opentab` (surfaced by `--version`).
  It is not derived from the git tag, so bump it when cutting a release.
