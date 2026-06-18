# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OpenTab is a single-file, zero-dependency terminal UI (curses) that reads OpenCode's
SQLite database **read-only** and browses your AI spend by month / day / project /
session / model, including the recursive subagent tree. Standard library only — no
`pip install` at runtime. The entire program is the executable `opentab` (a Python
script, no `.py` extension).

It also reads **Claude Code** transcripts (`~/.claude/projects/**/*.jsonl`) via a
second backend (`ClaudeStore`), **Codex CLI** rollouts
(`~/.codex/sessions/**/rollout-*.jsonl`) via a third (`CodexStore`), **Hermes
Agent** sessions (`~/.hermes/state.db`, SQLite) via a fourth (`HermesStore`), and a
**CSV of logged API requests** (e.g. GitHub Copilot in IntelliJ; `--csv`, default
`~/.config/opentab/requests.csv`) via a fifth (`CsvStore`), and can merge any of them
(`CombinedStore`). Pick with `--source {auto,opencode,claude,codex,hermes,csv,all}`,
or switch live in the TUI with **`c`**. Claude Code and Codex never record a per-message
cost, so their sessions behave like an OpenCode *subscription* session: **$0 in normal
mode** and an estimate (tokens × API list price) only under the **`$`** what-if view.
Hermes and the CSV/Copilot source are usually the same (subscription routes / no cost
column record $0) but **can** be metered — see the
`ClaudeStore`/`CodexStore`/`HermesStore`/`CsvStore` notes under Architecture.

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
- **`ClaudeStore`** — a second backend implementing the **same four methods** `App`
  calls (`workflows`, `summary`, `workflow_nodes`, `model_breakdown`) plus the
  `demo`/`demo_scale` attributes, over Claude Code's JSONL transcripts instead of SQL.
  That four-method surface is the whole `App`↔store contract, so `App`/`Renderer` are
  backend-agnostic — keep them that way (don't reach past these methods into SQL or
  JSONL). Claude Code stores **no per-message cost**, only tokens, so a Claude session
  is exactly an OpenCode *subscription* session: `model_breakdown` reports `cost = 0`
  and puts the full token counts in the `unpriced_*` / `root_unpriced_*` splits, so the
  ordinary `_compute_api_costs` + `$` machinery shows **$0 in normal mode and the
  list-price estimate under `$`** — no special-casing. `records_cost = False` drives
  three UI nudges: the `$` view **starts on by default** (an explicit saved pref in
  `state.json` overrides — see `App.__init__` + `apply_state`), the header tag reads
  "ESTIMATED" instead of "WHAT-IF", and normal mode shows the "press $" hint.
  It dedupes resumed/forked
  message overlap on `(message.id, requestId)` and folds each session's `cwd` to its
  **git root** (`git_root()`) so subdir launches roll up to the repo. Sidechain
  (Task-subagent) messages become depth-1 nodes grouped by `parentUuid`, mirroring
  `Store`'s recursive subtree. Each `Workflow` is stamped with `.source` (the backend's
  `source_name`).
- **`CodexStore`** — a third backend over Codex CLI rollout transcripts
  (`~/.codex/sessions/**/rollout-*.jsonl`), implementing the same four methods. Like
  Claude Code it records **no per-message cost**, so it is another *subscription*
  backend (`records_cost = False`, same `$0`/`$`-estimate behavior and UI nudges as
  `ClaudeStore`). Codex's token accounting differs and `CodexStore` is the only place
  that knows it: each turn logs a **cumulative** `total_token_usage` in a `token_count`
  event and Codex writes that count **twice** (the turn result, then an echo after the
  next `turn_context`), so it drives per-turn deltas off the **monotonic cumulative
  total** — a larger total is a new turn (`delta = total − prev`), an **equal** total is
  the duplicate echo (skip), a **smaller** total is a context-compaction reset (the new
  total is fresh usage). The accepted deltas sum back to the authoritative final total
  and each is attributed to the model active at that turn (`turn_context.model`, prefixed
  `openai/`). OpenAI's `input_tokens` **includes** the cached read and there is no
  cache-write, so input is split into uncached + `cache_read` (cache_write stays 0) and
  reasoning is folded into output (never priced twice), exactly matching the
  `unpriced_*` row schema the `$` machinery expects. Codex has **no subagent tree**
  (every session is one depth-0 node); `cwd` folds to its **git root**; sessions with no
  recorded usage are dropped.
- **`HermesStore`** — a fourth backend over Hermes Agent's SQLite state DB
  (`~/.hermes/state.db`), implementing the same four methods. Hermes is **multi-provider**
  (OpenAI / Anthropic / Google / OpenRouter / Nous / local / …) but **normalizes every
  provider's usage to one canonical shape before writing the row**, so there is **no
  per-provider token special-casing** here: `input_tokens` is the *uncached* prompt
  (cache_read / cache_write are tracked separately, never folded in) and `output_tokens`
  already *includes* reasoning as a subset (priced once via output). Total = input +
  output + cache_read + cache_write — matching Hermes' own `total_tokens` **exactly**
  (cross-checked against ccusage, which runs *high* by double-counting reasoning). **Cost
  is mixed**, unlike Claude/Codex: subscription routes (`billing_mode =
  'subscription_included'`, e.g. openai-codex) record $0 → their tokens are unpriced and
  `$` estimates them; **metered** routes (OpenRouter, Nous, direct API keys) record a real
  per-session cost in `actual_cost_usd` / `estimated_cost_usd` (actual preferred, mirroring
  ccusage) → those price as real spend in normal mode with `unpriced_*` zeroed. Because
  cost is mixed, **`records_cost` is a per-DB instance attr** (True iff any live session
  has a recorded cost), computed by a cheap probe in `__init__` so `CombinedStore` can read
  it before `workflows()`. Sessions with a `parent_session_id` form a subagent tree rolled
  into the root's totals; the model label is derived from `billing_provider` (mapped via
  `_PROVIDER_ALIASES`, else inferred from the name) rather than a hard-coded prefix; `cwd`
  folds to its **git root**; archived sessions are excluded. The SQL is **schema-adaptive**
  (`_probe_columns`/`_select_sql`, like `Store`) — missing optional columns degrade
  gracefully instead of crashing.
- **`CsvStore`** — a fifth backend over a **CSV of logged API requests** (one row per
  request; built for GitHub Copilot inside IntelliJ, but generic), implementing the same
  four methods. CSV is already tabular, so it's the simplest backend. Headers are matched
  **case-insensitively with aliases** (`_FIELD_ALIASES`/`_resolve_headers`): required are
  a timestamp (`timestamp`/`time`/…; ISO-8601 **or** epoch s/ms/us via `_parse_ts`), a
  `model`, and `input_tokens`/`output_tokens` (`prompt_tokens`/`completion_tokens` etc.);
  optional are `cached_tokens`, `session_id`, `project`, `title`, and a `cost_usd`/`credits`
  column. Copilot's premium-request **credit** billing isn't in the raw API exchange, so a
  Copilot row is treated like a *subscription* row: **records_cost defaults False**, every
  token unpriced, the `$` view estimates at list price (same UI nudges as
  `ClaudeStore`/`CodexStore`). But cost is handled per-row like `HermesStore` — if the CSV
  carries a `cost_usd`/`credits` column with positive values (`credits` × $0.01), those
  rows price as real spend and `unpriced_*` is zeroed, so **`records_cost` is a per-instance
  attr** set by a cheap `_probe_records_cost` in `__init__`. Token accounting is
  **OpenAI-style** (`input_tokens` includes the cached read → split into uncached +
  `cache_read`, `cache_write` stays 0); models are **mixed-provider**, so each id is
  provider-prefixed (`_infer_provider`/`_prefix_model`: claude→`anthropic/`, gpt/o3→`openai/`,
  gemini→`google/`) for pricing and the Providers rollup. No subagent tree (every session is
  one depth-0 node); when there's no `session_id`, requests group into one synthetic session
  per **(date, project)**; `project` folds to its **git root** when it's a path; `cwd`/file
  missing or malformed → empty, never a crash; sessions with no token usage are dropped.
- **`CombinedStore`** — wraps several backends and concatenates the same four methods,
  for `--source all` (OpenCode + Claude Code + Codex + Hermes + CSV/Copilot in one view).
  Workflow ids are
  globally unique across sources, so it routes `workflow_nodes` by an `id → backend` map
  built in `workflows()`; projects group by directory across all tools. `$` reprices
  every unpriced row across all backends. `records_cost` is the AND of its backends
  (False when any backend reports no recorded cost — Claude Code, Codex, a
  subscription-only Hermes DB, or a CSV with no cost column);
  `combined = True` turns on the per-session origin markers — a `Src` column in the
  session tables (`Renderer.src_col`) and `[oc]`/`[cc]`/`[cx]`/`[hm]`/`[csv]` title tags in the
  picker and Top Sessions lists (`Renderer.source_tag`, abbreviations in `_source_abbrev`).
  Combined **demo** works: `CombinedStore.__init__` forces every sub-store to one shared
  `demo_scale` (each backend would otherwise draw its own random scale, distorting the
  cross-source ratio the Sources view shows); it's still private (a single hidden factor
  can't be inverted).
- **Source selection** lives in `make_store()`/`resolve_source()`/`available_sources()`/
  `source_cycle()` (module level). `main` resolves the start source from
  `--source {auto,opencode,claude,codex,hermes,csv,all}`; on `auto` it restores the last-used
  source from `state.json` (when still available); failing that, **auto merges every
  present source (`all`)** when ≥2 exist — demo and non-demo alike — so you never need
  `--source` to see them together (single source when only one is present). `c` narrows to
  one source and that choice is remembered. **Pointing opentab at a file is a shortcut**
  for naming its source: a bare positional `PATH` and an explicit `--csv` are routed by
  `_route_path_arg()` (called at the end of `parse_args`) — `opentab requests.csv`,
  `opentab --csv requests.csv`, and `opentab --source csv requests.csv` all open that CSV
  on its own. With an explicit `--source` the positional fills *that* source's path slot
  (`_PATH_SLOT`); with no `--source` the source is inferred from the path
  (`_infer_source_from_path`: `.csv`→csv, `.db`/`.sqlite`→opencode, a directory is
  ambiguous→error) and a missing file errors. `--csv` defaults to **None** (not the path)
  so an explicit `--csv` is detectable; the real default `DEFAULT_CSV_PATH`
  (`~/.config/opentab/requests.csv`) is applied afterward for auto-discovery only, so a
  CSV merely *present* there still merges under bare `opentab`. `c` narrows to
  one source and that choice is remembered. The TUI switches live with **`c`**
  (`App.cycle_source` → cached build + `_reload_for_source`); the active source
  (`app.source_key`) is saved with the rest of the prefs. It shows as a header chip and
  the Trends overlay has a **Sources** tab (spend by tool). In the merged view only,
  `App.current_tabs` also injects a per-scope **Sources** tab right after Overview in the
  Month/Day/Project detail views (omitted with a single backend, where it'd be one 100%
  row); it and the Trends tab share `Renderer.source_table`.
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
`ClaudeStore` mirrors this; under `--source all` the `CombinedStore` makes every backend
share one factor (see above) and `--demo` **defaults to `all`** when >1 source is present.
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
