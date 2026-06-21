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
Agent** sessions (`~/.hermes/state.db`, SQLite) via a fourth (`HermesStore`), a
**CSV of logged API requests** (e.g. GitHub Copilot in IntelliJ; `--csv`, default
`~/.config/opentab/requests.csv`) via a fifth (`CsvStore`), the **GitHub Copilot
CLI**'s OpenTelemetry export (`~/.copilot/otel/**/*.jsonl` plus
`$COPILOT_OTEL_FILE_EXPORTER_PATH`) via a sixth (`CopilotStore`), **pi-agent**
sessions (`~/.pi/agent/sessions/**/*.jsonl`, or `$PI_AGENT_DIR`) via a seventh
(`PiStore`), and **OpenClaw** gateway sessions
(`~/.openclaw/agents/**/sessions/*.jsonl`, or `$OPENCLAW_DIR`) via an eighth
(`OpenClawStore`), and can merge any of them (`CombinedStore`). Pick with
`--source {auto,opencode,claude,codex,hermes,csv,copilot,pi,openclaw,all}`, or switch live in the
TUI with **`c`**. Claude Code, Codex, and the Copilot CLI never record a per-message
cost, so their sessions behave like an OpenCode *subscription* session: **$0 in normal
mode** and an estimate (tokens × API list price) only under the **`$`** what-if view.
Hermes, the CSV/Copilot-in-IntelliJ source, pi, and OpenClaw are mixed: Hermes/CSV are often $0
(subscription routes / no cost column) but **can** record real cost, while **pi and OpenClaw record a
per-message cost for every route but only *metered* (non-subscription) routes are real
spend** — their subscription/OAuth routes (e.g. openai-codex, github-copilot) carry a list-price estimate, not
spend, so they stay unpriced like the token-only backends — see the
`ClaudeStore`/`CodexStore`/`HermesStore`/`CsvStore`/`CopilotStore`/`PiStore`/`OpenClawStore` notes under
Architecture.

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
  not write to it. The only files it writes are `~/.config/opentab/state.json` (prefs),
  `~/.config/opentab/prices.json` (the optional models.dev price cache, only on
  `--refresh-models` / `r` in the `P` overlay), and `opentab-*.csv` exports (on `e`).
- **Python 3.9+.** `MIN_PYTHON = (3, 9)`; `target-version = py39`. Don't use newer syntax.

## Architecture

Three layers, all in `opentab`:

- **`Store`** — owns the sqlite connection and every SQL query; returns plain
  `Workflow`/`sqlite3.Row` data. It is **schema-adaptive**: OpenCode's schema varies by
  version, so `Store` probes columns (`_has_session_token_columns`, `_has_session_cost_column`,
  `_needs_message_usage`) and builds cost/token SQL expressions dynamically
  (`_cost_expr`, `_token_exprs`, `_message_usage_cte/_join`). When per-session cost
  columns are absent it falls back to aggregating the `message` table. Always go through
  these helpers when touching SQL — never hard-code column names. On top of the four-method
  contract `Store` adds two **optional per-session opt-ins**, both fetched **lazily on
  drill-in** and cached (never startup scans, unlike `model_breakdown`): `tool_breakdown`
  (the Tools tab; see below) — per-`(tool, model)` attribution from the `part` table,
  gated by `supports_tool_breakdown`/`supports_tools`; and `message_timeline(workflow_id)`
  (the **Turns** tab) — every assistant message (one LLM step) in the session tree ordered
  by `$.time.created`, with subagent turns (depth > 0) interleaved by time, gated by
  `supports_message_timeline`/`supports_turns`. Both filter to the session subtree first
  so they stay ~per-session, not whole-table.
- **`ClaudeStore`** — a second backend implementing the **same four methods** `App`
  calls (`workflows`, `summary`, `workflow_nodes`, `model_breakdown`) plus the
  `demo`/`demo_scale` attributes, over Claude Code's JSONL transcripts instead of SQL.
  That four-method surface is the whole `App`↔store contract, so `App`/`Renderer` are
  backend-agnostic — keep them that way (don't reach past these methods into SQL or
  JSONL). (The per-session **opt-ins** ride on top of the four, never required: `App`
  calls each via `getattr` and hides its tab unless the **selected session's** backend
  reports the matching `supports_*(workflow_id)` gate — so a Claude/Codex/Hermes/CSV
  session never shows an empty tab in the merged view, `CombinedStore` routing by owning
  backend. `tool_breakdown`/`supports_tools` (Tools) is **OpenCode-only**; but
  `message_timeline`/`supports_turns` (the **Turns** tab — per-turn cost over time)
  `ClaudeStore` **also** implements, since it's already message-based, so Turns shows on
  both OpenCode and Claude sessions, Tools only on OpenCode.) Claude Code stores **no per-message cost**, only tokens, so a Claude session
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
  `source_name`). Session **title** precedence is `custom-title` → `ai-title` (Claude's
  own generated title, present for most sessions) → first **real** user prompt →
  `(untitled)`; `_prompt_text` skips Claude Code's injected `user` messages (the
  local-command caveat, `<command-name>` and other `<…>` wrappers) and `_ingest` skips
  `isMeta`/sidechain ones, so a slash-command-started session titles from the actual
  prompt — but a genuinely one-word session (e.g. an "ok" resume stub with no
  `ai-title`) honestly keeps that word.
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
  (do not double-count reasoning, which would inflate the total). **Cost
  is mixed**, unlike Claude/Codex: subscription routes (`billing_mode =
  'subscription_included'`, e.g. openai-codex) record $0 → their tokens are unpriced and
  `$` estimates them; **metered** routes (OpenRouter, Nous, direct API keys) record a real
  per-session cost in `actual_cost_usd` / `estimated_cost_usd` (actual
  preferred) → those price as real spend in normal mode with `unpriced_*` zeroed. Because
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
  column. Copilot's usage-based **credit** billing isn't in the raw API exchange, so a
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
- **`CopilotStore`** — a sixth backend over the **GitHub Copilot CLI**'s OpenTelemetry
  file export (`~/.copilot/otel/**/*.jsonl`, plus the file named by
  `$COPILOT_OTEL_FILE_EXPORTER_PATH`), implementing the same four methods. The Copilot
  CLI records **no token usage** in its transcripts or its `session-store.db`; tokens land
  **only** in the OTEL export, which is **opt-in** (set `COPILOT_OTEL_FILE_EXPORTER_PATH`
  before launching/resuming a session, or point `--copilot-dir` at it). With export off
  there's nothing to read and the source never appears — the OTEL export is the only place
  these tokens are recorded. The export carries tokens but **no cost**; since June 2026 Copilot
  bills **usage-based** (tokens × list API rates → AI credits at 1¢ each), so the `$`
  list-price estimate ≈ the real bill. With no recorded cost it's a token-only backend:
  **`records_cost = False`** (class attr), every token unpriced, `$` estimates at list
  price, same UI nudges as `ClaudeStore`/`CodexStore`.
  OTEL follows the **GenAI semantic conventions**, where one LLM call can be logged up to
  four ways (a `chat` span, a `gen_ai.client.inference…` log, a `copilot_chat.agent.turn`
  log, an `invoke_agent` summary span), so `_parse_file` **dedups per file**: keep the
  highest-fidelity record per call (chat span > inference log > agent-turn log >
  agent-summary span) and drop the rest by matching **trace id / response id**
  (`_classify`/`_emit`). Token accounting is **OpenAI-style**
  (`gen_ai.usage.input_tokens` **includes** the cached read → split into uncached +
  `cache_read`; `cache_write` from `cache_creation`; reasoning **folded into output**, never
  priced twice; a record carrying only `total_tokens` back-fills the gap). Models are
  **mixed-provider** (gpt-5.x/claude-sonnet/gemini), so each id is provider-prefixed
  (`_infer_provider`/`_prefix_model`, same as `CsvStore`). OTEL carries **no cwd**, so each
  session's directory/title is enriched **read-only, best effort** from the sibling
  `session-store.db` (`_load_meta`, keyed by session id; `cwd` → **git root**, `summary` →
  title). No subagent tree (one depth-0 node); sessions with no recorded usage are dropped.
- **`PiStore`** — a seventh backend over **pi-agent** sessions
  (`~/.pi/agent/sessions/<project>/*.jsonl`, dir from `$PI_AGENT_DIR`/`--pi-dir`),
  implementing the same four methods. pi writes a per-message `usage.cost.total` — but a
  **list-price figure for every provider**, including subscription/OAuth routes (e.g.
  openai-codex on a ChatGPT plan) whose real marginal cost is $0. So only **metered**
  routes (OpenRouter, a direct API key) are trusted as spend; a message is classed
  **subscription** when its provider is an OAuth login (`auth.json` type `oauth`, read
  read-only) or matches a plan marker (`_SUBSCRIPTION_MARKERS`) and its tokens are left
  **unpriced** (the `$` view estimates them), mirroring `HermesStore`'s billing_mode split.
  Metered + subscription accumulate independently per message, so a session (or model)
  mixing both is split right. Cost is mixed, so — like `CsvStore`/`HermesStore` —
  **`records_cost` is a per-instance attr** (True iff any *metered* message has a cost), set
  by a cheap early-exit probe in `__init__`. Each session file is NDJSON: a `session` record carries the canonical
  id + **cwd** (so dirs fold to the **git root** with no path-decoding of the project dir
  name), `user` messages give the title (first text part), `assistant` messages carry
  `usage`. Token accounting is **Anthropic-style** (`input` is already *uncached*;
  `cacheRead`/`cacheWrite` are separate and never folded in; total =
  input+output+cacheRead+cacheWrite; a `totalTokens`-only record back-fills output). Models
  are recorded already provider-qualified (e.g. `moonshotai/kimi-k2.6`), used verbatim for
  pricing and the Providers rollup. Assistant messages dedupe by their stable `id`
  (resumed/forked files overlap). No subagent tree (one depth-0 node); sessions with no
  recorded usage are dropped.
- **`OpenClawStore`** — an eighth backend over **OpenClaw** gateway sessions
  (`~/.openclaw/agents/<agent>/sessions/<id>.jsonl`, root from `$OPENCLAW_DIR`/`--openclaw-dir`),
  implementing the same four methods. OpenClaw is a self-hosted multi-provider agent gateway;
  like pi it writes a per-message `usage.cost` (an **object** — only `.total` is read) that
  is a **list-price figure for every provider**, including subscription/OAuth
  routes (openai-codex on a ChatGPT plan, github-copilot) whose marginal cost is $0. So the
  **same billing split as `PiStore`/`HermesStore`**: only **metered** routes (a direct
  Anthropic key, OpenRouter) are real spend; a message is **subscription** when its provider's
  auth profile is an OAuth login (`openclaw.json` → `auth.profiles[*].mode == "oauth"`, read
  read-only — note github-copilot uses a static `token`, so it's caught by the `"copilot"`
  marker instead) or matches `_SUBSCRIPTION_MARKERS`, and its tokens stay **unpriced** (the
  `$` view estimates them). Metered + subscription accumulate independently per message, so a
  session (even one model) mixing both routes is split right; **`records_cost` is a per-instance
  attr** (True iff any *metered* message has a cost), set by a cheap early-exit probe in
  `__init__`. Parsing: each session file is NDJSON, and
  only `type:"message"` records with `message.role == "assistant"` + a `message.usage` object
  carry usage; `type:"model_change"` (and `type:"custom"` + customType `"model-snapshot"`)
  records set the current model/provider for following messages that omit their own. OpenClaw
  also writes a parallel **trace** schema (`session.started`/`model.completed`/…) in *separate*
  files that hold no `type:"message"` record, so reading only assistant messages never
  double-counts. Token accounting is **Anthropic-style** (`input` already *uncached*;
  `cacheRead`/`cacheWrite` separate, never folded; total = input+output+cacheRead+cacheWrite; a
  `totalTokens`-only record back-fills output). Models are recorded **bare** (gpt-5.3-codex,
  claude-opus-4-6), so they're provider-prefixed by inferred family (the `CsvStore` pattern)
  for pricing and the Providers rollup. The **project is the agent** (finance-os, homelab, …) —
  the directory under `agents/`, far more useful than OpenClaw's generic gateway cwd. Assistant
  messages dedupe by their stable record `id` across a session's live + archived
  (`.jsonl.reset.`/`.jsonl.deleted.`) files. No subagent tree (one depth-0 node); sessions with
  no recorded usage are dropped.
- **`CombinedStore`** — wraps several backends and concatenates the same four methods, for
  `--source all` (OpenCode + Claude Code + Codex + Hermes + CSV + Copilot CLI + pi + OpenClaw in one view).
  Workflow ids are
  globally unique across sources, so it routes `workflow_nodes` (and `tool_breakdown`)
  by an `id → backend` map built in `workflows()`; projects group by directory across all
  tools. `supports_tool_breakdown` is OR-ed across backends, but the Tools tab is gated
  per session by `supports_tools(workflow_id)` (routed to the owning backend) so it
  shows only on OpenCode sessions in the merged view, never empty on the others. `$` reprices
  every unpriced row across all backends. `records_cost` is the AND of its backends
  (False when any backend reports no recorded cost — Claude Code, Codex, the Copilot CLI, a
  subscription-only Hermes DB, or a CSV with no cost column; a metered pi or OpenClaw keeps it True);
  `combined = True` turns on the per-session origin markers — a `Src` column in the
  session tables (`Renderer.src_col`) and `[oc]`/`[cc]`/`[cx]`/`[hm]`/`[csv]`/`[cp]`/`[pi]`/`[ocl]` title
  tags in the picker and Top Sessions lists (`Renderer.source_tag`, abbreviations in `_source_abbrev`).
  Combined **demo** works: `CombinedStore.__init__` forces every sub-store to one shared
  `demo_scale` (each backend would otherwise draw its own random scale, distorting the
  cross-source ratio the Sources view shows); it's still private (a single hidden factor
  can't be inverted).
- **Source selection** lives in `make_store()`/`resolve_source()`/`available_sources()`/
  `source_cycle()` (module level). `main` resolves the start source from
  `--source {auto,opencode,claude,codex,hermes,csv,copilot,pi,openclaw,all}`; on `auto` it restores the last-used
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
tuples `month_tabs`/`day_tabs`/`project_tabs`/`workflow_tabs`. `current_tabs()` is the
source of truth (don't index a class tuple directly): it appends, in order, a **Turns**
tab (when `supports_turns(id)` — OpenCode + Claude) and a **Tools** tab (when
`supports_tools(id)` — OpenCode only) to the *selected* session's `workflow_tabs`, each
gated per-session so the merged view hides an unsupported tab rather than showing it
empty, and injects **Sources** in the merged view — so `draw_detail` dispatches the
session tabs by **name**, not by a fixed `self.tab` index.

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
- The **Tools tab** (per-tool / MCP-server token attribution, OpenCode only) is the
  opposite trade-off: it's a *per-session* `part`-table scan (`store.tool_breakdown`),
  cheap enough to fetch **lazily when you drill into a session** and memoize in
  `_tool_by_session` (cleared on reload / source switch). Each assistant message is one
  LLM step whose recorded tokens/cost are attributed to the tools it invoked that step,
  split evenly across parallel tool calls — i.e. "tokens in turns that used this tool",
  **not** the tool's own output size. `detail_tools` aggregates those `(tool, model)`
  rows per tool and per server (`tool_namespace`: built-in vs `server_*` MCP prefix) and
  reprices `$0` rows under `$` exactly like `_priced_nodes` does for subagents.
- The **Turns tab** (per-turn cost over time, OpenCode + Claude) is the same lazy
  per-session trade-off: `store.message_timeline` returns every assistant message (one
  LLM step) in the session subtree ordered by time, memoized in `_turns_by_session`
  (cleared on reload / source switch, demo-scaled by `_scale_demo_turns`). It also pulls
  the `user` messages so each turn carries the **owning prompt** (the most recent user
  message in time owns every turn until the next): OpenCode titles it from
  `summary.title` → first text part, Claude from the first real prompt text (reusing
  `_prompt_text`'s wrapper/tool-result skipping). `detail_turns` reprices `$0` turns
  under `$` like the Tools tab, **groups** turns under a `▸ <prompt>` header (rendered in
  the orange accent via a `draw_detail` prefix case) carrying that prompt's subtotal, and
  renders a running **Cumulative** column across the whole session — the point of the tab
  is *when* the money was spent, so rows are chronological (never cost-sorted) and the
  header-vs-rows split *is* the user-vs-llm distinction. Subagent (Task) turns are
  interleaved by time and tagged in the Agent column; demo anonymizes prompt titles
  (stable per `prompt_id`). The Time column shows date + clock (`MM-DD HH:MM:SS`) on every
  row — turns can be seconds apart and a resumed session spans days.
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
version/suffix churn. `P` shows this table.

`model_price()` first consults an **optional local cache** that *overlays* the embedded
table: `_load_price_cache()` lazily reads `~/.config/opentab/prices.json` (a
`{fetched_at, source, models:{bare_id:[in,out,cr,cw]}}` map), keyed by the bare model id
(last path segment, matching the lookup). The cache is written **only** on the explicit
`--refresh-models` CLI command (`refresh_models_command`) or `r` in the `P` overlay
(`App.refresh_prices_action`), both calling `refresh_model_prices()` — the **one place
runtime opentab touches the network** (stdlib `urllib`, `MODELS_DEV_URL`), fetching *every*
models.dev provider so open models on paid routes (Kimi/DeepSeek/Qwen via OpenRouter/…),
absent from the big-three embedded table, get real prices. A refresh in the TUI clears the
in-process cache (`invalidate_price_cache`) and re-runs `_compute_api_costs`/`_apply_price_mode`
in place. With no cache (the default), nothing is fetched and the embedded table is used —
opentab stays offline and stdlib-only by default.

On startup (after the deferred model scan, `App.maybe_prompt_prices`), if usage includes
models with no built-in price (`unknown_priced_models` — used, non-local, resolving to
`FALLBACK_PRICE`) and there are unpriced tokens to estimate, opentab shows a one-time modal
(`draw_price_prompt`/`handle_price_prompt_key`) offering the fetch: `y` fetches now, `n` not
now, `d` never again (persisted as `prices_prompt_dismissed` in `state.json`). It's gated by
`allow_price_prompt` (off under `--no-state`/`--demo`) and skipped once a cache already
exists. The `c`/`L`/price-prompt pickers are all small centered modals via
`Renderer.draw_modal` (drawn after the body so context shows behind), unlike the full-body
help/prices/trends overlays.

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
