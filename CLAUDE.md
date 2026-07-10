# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OpenTab is a terminal UI (curses) that reads OpenCode's SQLite database **read-only**
and browses your AI spend by month / day / project / session / model, including the
recursive subagent tree. It's a **src-layout Python package** (`src/opentab/`) that
installs the `opentab` command; runtime is **standard-library only**, the single
dependency being `windows-curses`, pulled in only on native Windows (which ships no
stdlib `curses`). Distributed on PyPI as **`opentab-ai`** (`pipx install opentab-ai`),
but the import package and the command both stay `opentab`.

It also reads, via additional backends implementing the same contract: **Claude Code**
transcripts (`~/.claude/projects/**/*.jsonl`, `ClaudeStore`), **Codex CLI** rollouts
(`~/.codex/sessions/**/rollout-*.jsonl`, `CodexStore`), **Hermes Agent** sessions
(`~/.hermes/state.db`, `HermesStore`), a **CSV of logged API requests** (`--csv`, default
`~/.config/opentab/requests.csv`, `CsvStore`), the **GitHub Copilot CLI** OpenTelemetry
export (`~/.copilot/otel/**/*.jsonl` + `$COPILOT_OTEL_FILE_EXPORTER_PATH`, `CopilotStore`),
**Copilot Chat in VS Code** (`<User>/workspaceStorage/*/chatSessions/*.json[l]` across the
Code/Insiders/VSCodium variants, or `--vscode-dir` — from WSL, pointed at the Windows-side
store — `VscodeStore`),
**pi-agent** sessions (`~/.pi/agent/sessions/**/*.jsonl` or `$PI_AGENT_DIR`, `PiStore`),
**OpenClaw** gateway sessions (`~/.openclaw/agents/**/sessions/*.jsonl` or `$OPENCLAW_DIR`,
`OpenClawStore`), and a **JSONL/NDJSON of logged API requests** (the per-line twin of the
CSV source; `--jsonl`, default `~/.config/opentab/requests.jsonl`, `JsonlStore`).
`CombinedStore` merges them. Pick with
`--source {auto,opencode,claude,codex,hermes,csv,jsonl,copilot,vscode,pi,openclaw,all}` or
switch live with **`c`**.

Cost model: Claude Code, Codex, and Copilot (CLI and VS Code) record **no per-message
cost**, so they behave like an OpenCode *subscription* session — **$0 in normal mode**, a
list-price estimate (tokens × API price) only under **`$`**. Hermes, CSV/JSONL, pi, and
OpenClaw are **mixed**: subscription/OAuth routes stay $0/estimated, metered routes record
real spend. See each backend's note under Architecture.

## Commands

```sh
pip install -e .                              # editable install (provides the `opentab` command)
python3 test_opentab.py                       # run the whole unit suite (custom runner, not pytest)
python3 -c "import test_opentab as t; t.test_trend_daily_shows_one_navigable_month()"  # run one test
ruff check src/opentab test_opentab.py        # lint (matches CI)
ruff format src/opentab test_opentab.py       # autoformat
ruff format --check src/opentab test_opentab.py   # format check (matches CI)
python3 -m compileall -q src/opentab          # byte-compile smoke check
python3 -m opentab --demo                     # run the TUI with anonymized/synthetic data
opentab --status "$PWD"                       # one-shot: current session's cost incl. subagents (tmux status line; OpenCode + Claude Code)
opentab --demo --html demo.html               # one-shot: write the self-contained HTML browser
opentab --serve                               # same browser served on http://localhost:8321 (+ live Turns/Tools)
opentab --web                                 # --serve, and open it in the default browser
```

`test_opentab.py` is **not** pytest — it has its own runner at the bottom that just runs
every `test_*` function in sorted order (no name filtering, no argv). It prepends `src/`
to `sys.path` itself, so `python3 test_opentab.py` works **without** an install. To run a
single test, import it and call it directly (as above), or use the locally-installed
`pytest` (`pytest test_opentab.py -k NAME`) which also discovers these functions. CI
installs the package (`pip install -e .`), runs ruff + `python3 test_opentab.py` +
`opentab --help` (with a native-Windows import smoke job), and shellchecks `install.sh`.
Install dev hooks with `git config core.hooksPath hooks` (the pre-push hook runs the same
checks).

`ruff.toml` deliberately ignores `E501` (long lines): the f-strings build fixed-width
TUI columns, so do **not** wrap them to satisfy line length.

## Commit conventions

Follow [Conventional Commits](https://www.conventionalcommits.org) — `type(scope): subject`.
**The full rules and the scope vocabulary table are canonical in
[`CONTRIBUTING.md`](CONTRIBUTING.md)**; the essentials:

- Types: `feat` `fix` `perf` `refactor` `docs` `test` `chore` (breaking → `type!:` and/or a
  `BREAKING CHANGE:` footer); releases use `chore(release): vX.Y.Z`.
- Subject: imperative mood, lowercase first word (`add`, never `adds`/`added`), no trailing
  period, ≤72 chars.
- Exactly **one** lowercase scope from the CONTRIBUTING vocabulary — don't coin a synonym
  for an existing one (`tui` not `ui`, `pricing` not `prices`, `sources` not `source`).
- **No AI attribution** — no `Co-Authored-By`, no "Generated with …", no 🤖.

## Hard constraints

- **Standard library only at runtime.** `curses` + `sqlite3` + stdlib. The **only**
  third-party runtime dependency is `windows-curses` (Windows-only, declared in
  `pyproject.toml`); never add another. ruff/hatchling are dev/build-only tooling.
- **Modular `src/` package, acyclic layering.** Program logic lives across `src/opentab/`
  (layout below). Keep the import graph acyclic: leaves (`models`, `formatting`, `heatmap`,
  `pricing`, `demo`, `util`, `webpage`) → `stores/*` → `tui/*` → `sources`/`state`/`web` →
  `cli`. Stores never import the TUI; annotation-only back-references (e.g. `App` inside
  `tui/renderer.py`, `state.py`, and `web.py`) go under `if TYPE_CHECKING:` so they don't
  create an import cycle. The top-level `__init__.py` re-exports the public API (and `os`/`sys`/`csv`/
  `curses`/`datetime`) so callers and tests reach everything as `opentab.<name>`.
- **Read-only on the OpenCode DB.** The tool opens the database read-only and must
  not write to it. The only files it writes are `~/.config/opentab/state.json` (prefs),
  `~/.config/opentab/prices.json` (the optional models.dev price cache, only on
  `--refresh-models` / `r` in the `P` overlay), the warm-start rollup cache under
  `~/.config/opentab/cache/` (one JSON per backend, rewritten after a parse when that
  backend's files change; off under `--demo` / `--no-cache`), `opentab-*.csv` exports
  (on `e`), and the HTML browser (only on `--html`, default `opentab-report.html`).
- **Python 3.9+.** `MIN_PYTHON = (3, 9)`; `target-version = py39`. Don't use newer syntax.

## Architecture

The package is laid out under `src/opentab/`:

```
src/opentab/
  __init__.py        re-exports the public API (and a few stdlib modules) as opentab.*
  __main__.py        python -m opentab
  cli.py             parse_args + main (entry point: opentab = opentab.cli:main)
  models.py          Workflow / DaySummary / MonthSummary / YearSummary / ProjectSummary
  formatting.py      money/pct/tokens/cost_bar/short_path/iso_to_local + paint regexes
  heatmap.py         heat_* / calendar_cells / week_key / month_range + HEAT_*/BAR_* consts
  pricing.py         MODEL_PRICE_TABLE (the GENERATED block) + model_price/cache/$ costing
  demo.py            demo_* anonymisation
  util.py            clipboard/launchers/git_root/fuzzy/parse_range/tool_namespace
  sources.py         make_store/resolve_source/available_sources/source_cycle + path routing
  state.py           load_state/save_state/apply_state
  themes.py          THEMES palettes (single source for the web browser + the TUI) + hex math
  stores/            opencode, claude, codex, hermes, csv_source, jsonl_source, copilot, vscode, pi, openclaw, combined, cached
  tui/               renderer (Renderer), app (App)
  web.py             build_payload/session_extras + html_command/serve_command (ReportServer)
  webpage.py         render_html: the self-contained browser page (inline CSS/JS strings)
```

Three logical layers (the class names below live in the files above — `Store` in
`stores/opencode.py`, `Renderer`/`App` in `tui/`, etc.):

- **`Store`** (`stores/opencode.py`) — owns the sqlite connection and every query; returns
  `Workflow`/`sqlite3.Row`. **Schema-adaptive**: OpenCode's schema varies by version, so it
  probes columns (`_has_session_token_columns`, `_has_session_cost_column`,
  `_needs_message_usage`) and builds cost/token SQL dynamically (`_cost_expr`,
  `_token_exprs`, `_message_usage_cte/_join`), falling back to aggregating the `message`
  table when per-session cost columns are absent — **always go through these helpers, never
  hard-code column names**. On top of the four-method contract it adds two **per-session
  opt-ins**, both fetched **lazily on drill-in** (never startup scans, unlike
  `model_breakdown`) and filtered to the session subtree: `tool_breakdown` (Tools tab,
  gated by `supports_tools`) and `message_timeline` (Turns tab — assistant messages ordered
  by `$.time.created`, subagent turns interleaved, gated by `supports_turns`). A third
  extra, `recent_roots` (roots newest-subtree-activity-first), feeds the curses-free
  `--status` one-shot (`cli.status_line`/`status_command`): the current session's cost for
  a tmux status line, `~`-prefixed when it contains a list-price estimate for $0 rows. Its
  target is a directory (project's latest session) or a session id (exactly that session;
  `root_of` walks a subagent id up to its root). **`--status` consults OpenCode + Claude
  Code**: a `ses_…` id names OpenCode, a bare UUID names Claude, and a directory (or no
  target) prices the newest root across both (`cli._status_line_all`). `ClaudeStore`
  implements the status trio without the full-tree parse — `recent_roots` orders
  transcripts by mtime with the cwd read lazily from the file head (`_TranscriptRoot`),
  `root_of` just confirms the transcript (a Claude id is already its root), and
  `status_nodes` (the `getattr` opt-in `_price_root` prefers over `workflow_nodes`)
  parses only that session's own transcript.
- **`ClaudeStore`** — second backend over Claude Code JSONL, same four methods
  (`workflows`/`summary`/`workflow_nodes`/`model_breakdown`) + `demo`/`demo_scale`. **That
  four-method surface is the whole `App`↔store contract — keep `App`/`Renderer`
  backend-agnostic** (don't reach into SQL/JSONL). The per-session opt-ins ride on top via
  `getattr`, each gated by `supports_*(workflow_id)` so the merged view hides an
  unsupported tab rather than showing it empty (`CombinedStore` routes by owning backend).
  Turns is implemented by every backend except Hermes; Tools by OpenCode, Claude, Codex,
  pi, and CSV/JSONL (the others record no per-step tool calls with usage). Records
  **no per-message cost** → a *subscription* backend: `model_breakdown` reports `cost=0`
  with tokens in the `unpriced_*`/`root_unpriced_*` splits, so the normal `$` machinery
  gives **$0 / list-price estimate** with no special-casing. `records_cost=False` drives
  three UI nudges (the `$` view **starts on**, a saved pref in `state.json` overriding;
  header reads "ESTIMATED"; normal mode shows the "press $" hint). Dedupes resumed/forked
  overlap on `(message.id, requestId)`; folds `cwd` to **git root**; sidechain (Task)
  messages become depth-1 nodes grouped by `parentUuid`. Title precedence: `custom-title` →
  `ai-title` → first real user prompt → `(untitled)` (`_prompt_text` skips injected `<…>`
  command wrappers, `_ingest` skips `isMeta`/sidechain). Implements **Tools**: each
  step's `tool_use` block names ride on its turn row, aggregated by
  `util.tool_rows_from_turns` (MCP names `mcp__server__tool` roll up to their server).
- **`CodexStore`** — third backend over Codex rollout JSONL, same methods; another
  *subscription* backend (`records_cost=False`, same $0/$ behavior + nudges). Codex's token
  accounting is the tricky bit: each turn logs a **cumulative** `total_token_usage`, written
  **twice** (turn result + an echo after the next `turn_context`), so per-turn deltas come
  off the **monotonic cumulative total** — larger total = new turn (`delta = total − prev`),
  equal = duplicate echo (skip), smaller = context-compaction reset (new total is fresh).
  Deltas sum back to the final total, each attributed to that turn's `turn_context.model`
  (`openai/`-prefixed). OpenAI-style tokens (input includes the cache read → uncached +
  `cache_read`, no cache-write; reasoning folded into output). No subagent tree; `cwd` → git
  root; usage-less sessions dropped. Implements **Tools**: the `function_call`/
  `custom_tool_call`/`local_shell_call` records since the previous accepted delta are that
  turn's calls (the duplicate echo doesn't consume them), split evenly.
- **`HermesStore`** — fourth backend over Hermes' SQLite (`~/.hermes/state.db`), same
  methods. **Multi-provider** but Hermes **normalizes every provider's usage to one
  canonical shape**, so no per-provider token special-casing: `input_tokens` is *uncached*,
  `cache_read`/`cache_write` separate, `output_tokens` already includes reasoning (priced
  once); total = input+output+cache_read+cache_write, matching Hermes' own `total_tokens`
  (don't double-count reasoning). **Mixed cost**: subscription routes
  (`billing_mode='subscription_included'`) record $0 → unpriced/estimated; metered routes
  record real cost in `actual_cost_usd`/`estimated_cost_usd` (actual preferred) → real
  spend, `unpriced_*` zeroed. So **`records_cost` is a per-instance attr** (probed in
  `__init__` so `CombinedStore` can read it before `workflows()`). `parent_session_id` forms
  a subagent tree; model label from `billing_provider` (`_PROVIDER_ALIASES`); `cwd` → git
  root; archived excluded; SQL schema-adaptive (`_probe_columns`/`_select_sql`). **No Turns**:
state.db has a `messages` table but its `token_count` is never populated (verified against
a real DB, 2026-07), so there is no per-message usage to timeline.
- **`CsvStore`** — fifth backend over a **CSV of logged API requests** (one row per
  request, generic), same methods; the simplest backend. Headers matched
  **case-insensitively with aliases** (`_FIELD_ALIASES`/`_resolve_headers`): required are a
  timestamp (ISO-8601 or epoch via `_parse_ts`), `model`, and input/output tokens; optional
  `cached_tokens`, `session_id`, `project`, `title`, `cost_usd`/`credits`, `tool`. **Mixed cost
  per-row** like Hermes: no cost column → subscription (every token unpriced, `$`-estimated,
  same nudges); a positive `cost_usd`/`credits` (`credits` × $0.01) → real spend,
  `unpriced_*` zeroed — so **`records_cost` is a per-instance attr** (`_probe_records_cost`).
  OpenAI-style tokens (input includes the cache read → uncached + `cache_read`, no
  cache-write); models provider-prefixed (`_infer_provider`/`_prefix_model`:
  claude→`anthropic/`, gpt/o3→`openai/`, gemini→`google/`). No subagent tree; no
  `session_id` → one synthetic session per **(date, project)**; `project` → git root;
  malformed rows skipped, never crash; usage-less sessions dropped. Implements **Turns**
(each row is one turn; the optional `prompt`/`prompt_id`/`request_id` columns feed the ▸
grouping and the regenerate/append dedup) and **Tools** (the optional `tool` column —
`Bash;Read`, or a JSON list in JSONL — gated per session on it actually being used) —
`JsonlStore` inherits this machinery.
- **`CopilotStore`** — sixth backend over the **GitHub Copilot CLI** OpenTelemetry export
  (`~/.copilot/otel/**/*.jsonl` + `$COPILOT_OTEL_FILE_EXPORTER_PATH`), same methods. The CLI
  records tokens **only** in this **opt-in** OTEL export (set the env var, or point
  `--copilot-dir`); with it off the source never appears. Export carries tokens but **no
  cost** → token-only backend (**`records_cost=False`**, $0/list-price estimate, same
  nudges). OTEL follows the **GenAI semantic conventions** where one call is logged up to
  four ways — spans and logs land in *different* files — so `_parse` **dedups across all
  files** by trace/response id, keeping the
  highest-fidelity record (chat span > inference log > agent-turn log > agent-summary span;
  `_classify`/`_emit`). OpenAI-style tokens (input includes cache read; `cache_write` from
  `cache_creation`; reasoning folded into output; a `total_tokens`-only record back-fills).
  Models provider-prefixed (as `CsvStore`). OTEL has **no cwd**, so each session's dir/title
  is enriched **read-only, best effort** from the sibling `session-store.db` (`_load_meta`).
  No subagent tree; usage-less sessions dropped. **Turns** rows are one per kept call and
  **headerless** (OTEL captures no prompt content unless content capture is enabled).
- **`VscodeStore`** — backend over **Copilot Chat in VS Code**, read from VS Code core's
  own chat-session store (`<User>/workspaceStorage/<hash>/chatSessions/*` +
  `globalStorage/emptyWindowChatSessions/*`, across the Code / Code - Insiders / VSCodium
  variants; `--vscode-dir` narrows to one User or chatSessions dir), same methods + the
  **Turns** opt-in (each chat request = one prompt = one turn). Two on-disk shapes, both
  read: the current **journal `.jsonl`** (NDJSON patches replayed into the session object:
  kind 0 snapshot, 1 set-at-path, 2 append, default path `["requests"]`) and the older
  plain `.json`; a migrated session in both dedupes by `(sessionId, requestId)`, journal
  first. Token fields per request (serialized by VS Code's `chatModel.ts`): output =
  **max(`completionTokens`, `metadata.outputTokens`)** — the top-level one *accumulates
  across the turn's tool-call rounds* (setUsage sums), the metadata one is a single round
  and undercounts agentic turns; input = max(`metadata.promptTokens`, `promptTokens`), the
  final round's context (per-round prompts are not recorded, so input under-counts
  many-round turns; no cache split exists). No dollar cost recorded (`copilotCredits` is a
  premium-request quota unit, not USD) → token-only backend (**`records_cost=False`**, same
  $0/`$`-estimate nudges). `metadata.resolvedModel` (covers the "auto" router) →
  `modelId` minus the `copilot/` prefix, then provider-prefixed (the `CsvStore` pattern).
  Project = the workspace's `workspace.json` folder/workspace URI → git root
  (`_uri_to_path`: `file://` and `vscode-remote://` both handled — a Remote-WSL URI's path
  is directly local inside the distro, and a Windows drive path folds onto its WSL mount
  via `util.windows_to_wsl_path` so the git-root walk can reach it); empty-window sessions
  group under "(no workspace)". **The Windows-side store is deliberately NOT auto-scanned
  from WSL** (parsing every session over the drvfs/9p mount would slow every startup —
  don't re-add it): WSL users opt in with `--vscode-dir /mnt/c/Users/<you>/AppData/Roaming/
  Code/User` (example in its `--help`), which the `_uri_to_path` handling then makes
  useful. **Availability requires recorded
  tokens** (`_vscode_available` scans for the token markers): merely opening the chat
  panel writes empty session files, and file presence alone would surface the source for
  every VS Code install. Title: `customTitle` → `computedTitle` → first prompt. No
  subagent tree; usage-less sessions dropped.
- **`PiStore`** — seventh backend over **pi-agent** NDJSON
  (`~/.pi/agent/sessions/<project>/*.jsonl`, `$PI_AGENT_DIR`/`--pi-dir`), same methods. pi
  writes a per-message `usage.cost.total`, but a **list-price figure for every provider**
  including subscription/OAuth routes (marginal cost $0), so only **metered** routes
  (OpenRouter, direct key) count as spend: a message is **subscription** when its provider
  is an OAuth login (`auth.json` type `oauth`, read read-only) or matches
  `_SUBSCRIPTION_MARKERS`, its tokens staying unpriced/`$`-estimated — the Hermes billing
  split. Metered + subscription accumulate independently per message; **`records_cost` is a
  per-instance attr** (any metered cost). Anthropic-style tokens (`input` already uncached;
  `cacheRead`/`cacheWrite` separate; total = input+output+cacheRead+cacheWrite;
  `totalTokens`-only back-fills output). A `session` record gives id + **cwd** (→ git root),
  `user` gives the title, `assistant` carries `usage`; models already provider-qualified;
  assistant messages dedupe by `id`. No subagent tree; usage-less sessions dropped.
  Implements **Turns** (one row per assistant message, ▸-grouped by user prompt; a
  subscription turn stays $0 so `$` estimates it, a metered turn carries real spend) and
  **Tools** (the step's `toolCall` content blocks; a metered step's real cost splits
  across them).
- **`OpenClawStore`** — eighth backend over **OpenClaw** gateway NDJSON
  (`~/.openclaw/agents/<agent>/sessions/<id>.jsonl`, `$OPENCLAW_DIR`/`--openclaw-dir`), same
  methods. Like pi it writes a per-message `usage.cost` (an **object**, only `.total` read)
  that's list-price for every provider, so the **same metered-vs-subscription split**:
  subscription when the provider's auth profile is OAuth (`openclaw.json` →
  `auth.profiles[*].mode=="oauth"`; a static-token provider is caught by the `"copilot"`
  marker) or matches `_SUBSCRIPTION_MARKERS`; **`records_cost` per-instance** (any metered
  cost). Only `type:"message"` records with `message.role=="assistant"` + a `message.usage`
  object carry usage; `type:"model_change"`/`"model-snapshot"` set the current model for
  following messages. OpenClaw also writes a parallel **trace** schema in *separate* files
  (no `type:"message"`), so reading only assistant messages never double-counts.
  Anthropic-style tokens (as pi). Models recorded **bare** → provider-prefixed by inferred
  family (the `CsvStore` pattern); **the project is the agent** (the `agents/` dir, not
  OpenClaw's generic cwd); messages dedupe by record `id` across live + archived
  (`.jsonl.reset.`/`.jsonl.deleted.`). No subagent tree; usage-less sessions dropped.
  Implements **Turns** exactly like pi (epoch-seconds timestamps).
- **`JsonlStore`** — ninth backend over a **JSONL/NDJSON of logged API requests** (one JSON
  object per line, generic). **Subclasses `CsvStore`** — its per-line twin — inheriting
  OpenAI-style token accounting, mixed per-row cost (`records_cost` per-instance;
  `cost_usd`/`credits`), provider-prefixed models, synthetic `(date, project)` sessions,
  git-root fold, and demo; only the parser (`json.loads` per line) and the `_KEYS` alias map
  differ. Like CSV it keeps each line as a turn for **Turns**
  (`message_timeline`/`supports_turns`): each request is one LLM step ordered by time, the
  optional per-line `prompt` (else `prompt_id`) grouping consecutive same-prompt turns under
  one `▸` header (the `ClaudeStore` pattern); a stable `request_id` dedupes a
  regenerated/appended file; first `prompt`/`title` seeds the title. No subagent tree. **The
  on-disk schema producers must follow is documented in the `JsonlStore` docstring.**
- **`CombinedStore`** — wraps several backends and concatenates the four methods for
  `--source all`. Workflow ids are globally unique, so it routes
  `workflow_nodes`/`tool_breakdown`/`message_timeline`/`supports_*` by an `id → backend` map
  built in `workflows()`; projects group by directory across tools. `$` reprices every
  unpriced row across backends. `records_cost` is the **AND** of its backends (False if any
  reports none). `combined=True` turns on per-session origin markers — a `Src` column
  (`Renderer.src_col`) and `[oc]`/`[cc]`/… title tags (`source_tag`/`_source_abbrev`).
  Combined **demo** forces every sub-store to one shared `demo_scale` (else each draws its
  own, distorting the Sources ratio); still private.
- **`CachedStore`** (`stores/cached.py`) — the **warm-start cache**, a transparent wrapper
  `make_store()` puts around every *leaf* backend (not the merged view — its sub-stores are
  cached individually, so one backend changing doesn't cold-start the others). It
  fingerprints the backend's `cache_inputs()` (each store lists the files whose
  `(path, size, mtime_ns)` identify its data) and, when that matches the on-disk cache
  (`~/.config/opentab/cache/<source>-<hash>.json`, one per `key|root`), returns cached
  `workflows()`/`model_breakdown()` **without parsing** — the ~0.8s→~50ms warm start.
  **Only those two methods are intercepted**; everything else (`workflow_nodes`, the
  Turns/Tools extras, `supports_*`, `records_cost`, `demo`, `source_name`, …) delegates to
  the wrapped store via `__getattr__`, which parses lazily the first time you drill in — so
  a warm start paints instantly and pays the parse only if you open a session. `workflows()`
  re-fingerprints every call, so reload (`r`) after an edit re-parses; a changed
  size/mtime misses and rewrites (atomic temp+replace, best-effort — a cache it can't write
  never blocks launch, and a stale rollup is never shown). Cache stores the raw pre-`$`
  rows, so the `$` what-if reprices from them unchanged. **Off under `--demo`** (never
  persists; per-process scale mustn't be baked in) **and `--no-cache`**; `CACHE_VERSION`
  bumps invalidate old files.
- **Source selection** — `make_store()`/`resolve_source()`/`available_sources()`/
  `source_cycle()`. `main` resolves the start source from `--source`; on `auto` it restores
  the last-used source from `state.json`, else **merges every present source (`all`)** when
  ≥2 exist (single source when only one). `c` narrows to one source and remembers it
  (`App.cycle_source` → cached build + `_reload_for_source`; saved as `app.source_key`).
  **Pointing opentab at a file** is a shortcut: a bare positional `PATH` (and `--csv`/
  `--jsonl`) routes via `_route_path_arg()` — with an explicit `--source` the path fills
  that slot (`_PATH_SLOT`), else the source is inferred (`_infer_source_from_path`:
  `.csv`→csv, `.jsonl`/`.ndjson`→jsonl, `.db`/`.sqlite`→opencode; a single `.jsonl` *file*
  is the request-log source, while the dir-based JSONL backends want `--source`; a directory
  is ambiguous → error). `--csv`/`--jsonl` default to **None** so an explicit flag is
  detectable; the real defaults are applied afterward for auto-discovery. The merged view
  adds a **Sources** tab (Trends, plus a per-scope tab after Overview in Month/Day/Project;
  `Renderer.source_table` in previews, and in a zoom a navigable picker
  (`draw_sources_picker`, `source_index`) whose Enter narrows Sessions to that source —
  `zoom_source`, the `zoom_project` drill pattern; Esc pops it back to the Sources tab).
- **`App`** — all state and the keyboard/mouse state machine; stays curses-free except
  the modal prompt line. Holds the view stack and selection indices.
- **`Renderer`** — all drawing. `Renderer.__getattr__` delegates unknown attributes to
  its `App`, so renderer methods read app state directly as if it were `self`. Drawing
  methods return `list[str]` of plain text; `write_rich` re-colors money/token spans by
  regex at paint time (so a line is just a string until it hits the screen).

### View state machine (`App`)

`self.view` is `"browse"` → `"zoom"` → `"session"` (drill in with Enter/`+`, out with
Esc). `self.browse_mode` is `"time"` (Months/Days sidebar, `self.focus` flips between
them) or `"projects"`. **Zoom is lazygit-style, not full-screen**: it renders the same
split as browse with the roles swapped — sidebar inactive (still registering click
regions, so a row click re-scopes the detail in place; a sidebar double-click is
swallowed, never "open the selected session"), detail active. `+` toggles
`zoom_maximized` (full-width detail; a saved pref in `state.json`) — in browse `+`
stays an Enter alias. In browse the preview pane registers a trailing catch-all
`"detail"` region, so a click anywhere in it focuses (zooms) it — appended after the
pane's real regions, which win (hit() is first-match). The session view is always
full-screen. The web needs no counterpart: its sidebar is permanent. Overlays are separate booleans on top of any
view: `self.trends` (T), `self.help` (?), `self.show_prices` (P). Detail tabs per zoom level are the class
tuples `month_tabs`/`day_tabs`/`project_tabs`/`workflow_tabs`. `current_tabs()` is the
source of truth (don't index a class tuple directly): it appends, in order, a **Turns**
tab (when `supports_turns(id)` — every backend except Hermes, whose DB records no
per-message usage) and a **Tools** tab (when
`supports_tools(id)` — OpenCode, Claude, Codex, pi, and CSV/JSONL sessions whose log
carries the optional `tool` column) to the *selected* session's `workflow_tabs`, each
gated per-session so the merged view hides an unsupported tab rather than showing it
empty, and injects **Sources** in the merged view — so `draw_detail` dispatches the
session tabs by **name**, not by a fixed `self.tab` index.

**Every Trends tab is navigable + drillable**, all through one modal pattern
(`trend_focus`, shared with the Calendar grid so arrows are never trapped): on the bar
charts Enter focuses, arrows walk `trend_cursor` (a date, or `YYYY-MM` on Monthly; ▲
marker), Enter drills into that day/month; on the ranked tabs j/k move `trend_row_index`
and Enter opens `trend_drill` — an in-overlay, **range-scoped** sessions list (unlike the
app-wide P drill; models match the row's exact spelling, not the canonical id) where
Enter jumps into the session itself (`drill_into_month`/`drill_into_session` mirror
`drill_into_date`). Esc out of any drilled scope returns to the overlay via
`_trend_return` (the generalized `_cal_return`); mouse mirrors keys (click wakes/selects,
double-click drills — bar hit-testing via `_trend_bar_geom`, rows via the
`trendrow`/`trendses` regions).

**Global toggles stay live inside the overlays.** Trends and the P table swallow
mistyped keys (closing is explicit — Esc/q/their own toggle), but the overlay-wide
`_trend_common_key`/`_prices_common_key` handlers let `?`, `C` (theme), `c` (source) and
`D` (demo) act from anywhere inside them (Trends also floats `P`, and `C` works from
help). The `C`/`c` pickers float *above* the overlays: `handle_key` (and `handle_mouse`)
check `theme_menu`/`source_menu` **before** the overlay branches — keep that dispatch
order, it's what lets the picker own the keys while the overlay behind is the live
preview swatch. A source/demo swap under an open overlay goes through
`_reload_for_source`, which re-anchors every overlay cursor (trend drill/cursors, the P
drill) so nothing dangles into the old dataset; the overlay itself stays open. The web
mirrors all of this in its keydown chain: `THEMEPICK` → `PRICES` → `TRENDS` top-down
(stacking = DOM order of the `#themepick`/`#prices`/`#trends` hosts), `C` opens the theme
panel from anywhere, and `render(false)` re-renders every open overlay so a theme click
re-colors the SVG charts behind the picker.

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
- The **Tools tab** (per-tool / MCP-server token attribution; OpenCode, Claude, Codex,
  pi, and CSV/JSONL logs with the optional `tool` column) is the
  opposite trade-off: a *per-session* fetch (`store.tool_breakdown` — OpenCode scans its
  `part` table; the file backends aggregate the `tools` names their in-memory turn rows
  carry via `util.tool_rows_from_turns`), cheap enough to fetch **lazily when you drill
  into a session** and memoize in
  `_tool_by_session` (cleared on reload / source switch). Each assistant message is one
  LLM step whose recorded tokens/cost are attributed to the tools it invoked that step
  (Codex: the `function_call`/`custom_tool_call` records since the previous accepted
  cumulative delta belong to that turn), split evenly across parallel tool calls — i.e.
  "tokens in turns that used this tool",
  **not** the tool's own output size. `detail_tools` aggregates those `(tool, model)`
  rows per tool and per server (`tool_namespace`: built-ins matched case-insensitively
  across the tools' spellings, `server_*`/`mcp__server__*` MCP names to their server) and
  reprices `$0` rows under `$` exactly like `_priced_nodes` does for subagents.
- The **Turns tab** (per-turn cost over time; **every backend except Hermes** — its
  state.db has a `messages` table but `token_count` is never populated, so there is no
  per-message usage to show) is the same lazy
  per-session trade-off: `store.message_timeline` returns every assistant message (one
  LLM step) in the session subtree ordered by time, memoized in `_turns_by_session`
  (cleared on reload / source switch, demo-scaled by `_scale_demo_turns`). It also pulls
  the `user` messages so each turn carries the **owning prompt** (the most recent user
  message in time owns every turn until the next): OpenCode titles it from
  `summary.title` → first text part, Claude from the first real prompt text (reusing
  `_prompt_text`'s wrapper/tool-result skipping); pi/OpenClaw/Codex reuse Claude's
  lockstep prompt-assignment, CSV/JSONL group by their per-row `prompt` column, and
  Copilot's rows are **headerless** (OTEL captures no prompt content by default). `detail_turns` reprices `$0` turns
  under `$` like the Tools tab, **groups** turns under a `▸ <prompt>` header (rendered in
  the orange accent via a `draw_detail` prefix case) carrying that prompt's subtotal, and
  renders a running **Cumulative** column across the whole session — the point of the tab
  is *when* the money was spent, so rows are chronological (never cost-sorted) and the
  header-vs-rows split *is* the user-vs-llm distinction. Subagent (Task) turns are
  interleaved by time and tagged in the Agent column; demo anonymizes prompt titles
  (stable per `prompt_id`). The Time column shows date + clock (`MM-DD HH:MM:SS`) on every
  row — turns can be seconds apart and a resumed session spans days. Every timeline row
  also carries **`prompt_full`** — the raw prompt uncapped, line breaks kept
  (`prompt_title` stays the capped one-liner; demo mirrors the fake into both) — and each
  `▸` header **unfolds to it**: in the TUI `z` toggles all groups and a click one (the
  `turnline` region → `_turn_header_at`; `▾` + dim `│` lines), in the web the header
  hovers the full text and a click toggles a `pre-wrap` row beneath (`promptFull` in
  `session_extras`).
- Subagent costs are recursive: `workflow_nodes` walks `session.parent_id` with a
  recursive CTE so a root session's cost includes its whole subagent subtree.
- **Drilling into a session never freezes mid-draw**: when its lazy fetches
  (nodes/Turns/Tools) aren't memoized (`App.session_data_ready`), `draw_detail` paints
  one "Loading session — reading … records…" frame (skipping the tabs — their
  `supports_*` gates could trigger the same parse) and sets `_session_loading`;
  `run()`'s prefetch tick then does the blocking work (`prefetch_session_data`, on a
  warm start the whole backend parse) and repaints — the first-frame model-scan
  pattern. The web mirrors it: `tabsFor` shows placeholder `Turns ⋯`/`Tools ⋯` tabs
  (`.ld`, pulsing) while the `/api/session` fetch is in flight, and their panes render
  a loading hint.
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
`src/opentab/pricing.py`; never hand-edit that block. `model_price()` adds family fallbacks for
version/suffix churn. `P` shows this table for the models **you've used**
(`priced_model_entries`), one row per model **deduped to the canonical id**
(`canonical_model` folds alias spellings: dots==dashes, date pins and reasoning-effort
suffixes stripped — the row displays its most-used alias via `display_model` and takes
the most completely-priced alias's rates, `_best_alias_price`). The decision column is
**eff $/M** (`effective_price`): each model's list rates priced at **your app-wide
token mix** (`price_token_mix`, cache-read-heavy in practice), cheapest-first by
default; a missing cache-read rate is never a free lunch — those reads bill at the
input rate, the eff value gets a `~` and the raw cell a `—`. Beside it sits **use**,
your token share as a bar (revealed preference — the closest offline proxy for "which
models do I actually rely on"). Three **layouts `p` cycles** (`prices_view`, a saved
pref, default flat): **flat** — one ungrouped list (cheapest-for-the-mix is a
cross-vendor question); **by vendor** — grouped under `▸ Anthropic/OpenAI/…` headers
(`model_family`/`family_label` infer the vendor from the model *name*, not the access
route), rows tagged with their route(s); **by provider** — one row per `(route, model)`
under `▸ anthropic/github-copilot/…` headers, rows tagged with their vendor. All three
are **sortable** by model/eff/use/price column (`s` picker or a header click;
`prices_sort`, default `eff`) and **heat-shaded** green→red per column, eff included
(`_price_heat_level`, pairs `PRICE_HEAT_BASE_PAIR..`). Local models are excluded (no
API rate). `Enter` drills into the sessions that used a model (aggregated across
routes and alias spellings by canonical id).

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

### The web browser (`--html` / `--serve`)

A second frontend over the same data, stdlib-only (`http.server`), curses-free (works on
native Windows). `cli.web_command` builds the usual **headless App** — rollups, worktree
folding, saved prefs via `apply_state`, the real/API cost snapshots — and hands it to
`web.build_payload()`, which serializes the visible dataset (`all_workflows`, per-root
model rows, and subagent `workflow_nodes` for sessions that have any) to plain JSON.
**Every cost travels twice (`real`/`api`)** so the page's `$` toggle is a client-side
field swap, never a reprice; `webpage.render_html()` wraps the blob in one self-contained
page (drill-in = deep links, browser back = step out; token replacement with `__PAYLOAD__`
substituted **last** and `</` escaped so a session title can't break out of the data
block). **The page deliberately mirrors the TUI**: a lazygit-style Years/Months/Days (or
Projects) sidebar with the same eighth-block cost bars (`formatting.cost_bar` reimplemented
in JS) — the Years panel appears only with >1 year (like `App.years`) and its "∑ all
years" row unscopes Months to the whole history — a tabbed detail pane whose per-scope tabs
are the App's own tab tuples (`year_tabs`/`month_tabs`/`day_tabs`/`project_tabs`/
`workflow_tabs`, Sources injected in the merged view), box borders with the title in the
border line, and the TUI keymap (`j`/`k`, `Tab` cycles Years→Months→Days, `h`/`l`, `Esc`,
`$`, `p`/`t`, `T`). Scopes are hash-routed (`#/y/2026` · `#/m/2026-06` · `#/d/…` · `#/p/…`
· `#/s/…`); the active tab is transient state (preserved across sibling navigation when the
new scope still has it). The `p`/`t` mode switch renders in place when already at the root
(a hash-unchanged `go()` wouldn't fire `hashchange`). **`T` (or the header button) opens the
Trends overlay** — a modal mirroring the TUI's 7-tab Trends over the whole range
(`App.trend_tabs`): Daily/Weekly/Monthly bar charts (each with a `◀ ▶`/`j`/`k` pager over
months/weeks, bars drill through to that scope and close), the Calendar heatmap (year
pager), and Model/Provider/Source ranked bars (`providerAgg` rolls model ids to their route
prefix, exactly like `trend_providers`) whose rows **drill to an in-overlay sessions list**
(`trendDrillRows`, range-scoped like the TUI's Trends drill) — session rows deep-link into
the session and close; Esc backs out of the drill first, h/l leave it and switch tabs. It
reads the whole `W`, reacts to `$` live, and is transient (not hash-routed), matching the
TUI overlay. **`P` opens the prices overlay** —
the models.dev list-price reference behind `$` (`build_payload` serializes
`App.priced_model_entries` for the flat/provider row sets + `price_token_mix`), with the
`eff $/M` blend, per-column green→red heat (log position in the column's [min,max], the
`_price_heat_level` rule), the `use` share bar, `~`/`—` markers, three layouts, and
header-click sort; it is **app-wide, never range-scoped** (like the TUI). **`R` (or the
range chip) rescopes client-side**: `ALL_W` is the full embedded set and `W = filterRange(
ALL_W)` is the active window (presets: last N days/months, this year, custom `since..until`;
`a` resets). Range narrows the main views and Trends but not Prices; a session deep-link
still resolves against `ALL_W` so it opens regardless of the active window.
**Themes are one source, two frontends.** The palettes live in `opentab/themes.py`
(`THEMES`: one entry per theme = a role-token palette + calendar/price heat ramps + a
`dark` flag, plus the hex→terminal-color math). Neither frontend hard-codes them:
- **Web** — `render_html` injects `themes.web_payload()` (roles reshaped to CSS-var names)
  as the JS `THEMES`. The CSS uses **semantic role tokens** (`--accent`/`--good`/`--bad`/
  `--bg`/… — not hues); `applyTheme(id)` writes them onto `:root` so all HTML re-themes via
  the CSS-var cascade while the SVG charts read the same entry through `TH`/`thc()`;
  translucent accents are `color-mix()` so they follow. Precedence: `localStorage` →
  `--theme` (`meta.theme`) → `opentab`; the picker persists the viewer's choice.
- **TUI** — `Renderer.init_theme_colors()` maps the active theme's role hexes onto the
  fixed curses color-pair layout (pairs 1–7 + the two heat ramps): exact via `init_color`
  on true-color terminals (custom indices from `_THEME_COLOR_BASE`; the heat ramps get
  fixed reusable slots so per-frame re-inits don't leak indices), nearest-256 otherwise,
  and the generated ANSI ramp on 8-color. Every pair paints an **explicit theme
  background** (not `-1`/terminal default), and `draw()` sets the window background to
  `_BASE_PAIR` (ink-on-bg) before each `erase()` — so the theme's bg fills every cell the
  way neovim's `Normal` group does, and a **light theme actually renders a light screen**
  instead of coloured text on the terminal's own dark background. (`assume_default_colors`
  is *not* enough: it only changes what `-1` *means*; ncurses still erases to the terminal
  default, so the screen stayed dark — hence colouring every cell.) `C` (Colours) opens the
  picker; `j`/`k` **live-preview** each theme
  (`select_theme(announce=False)` re-inits pairs in place — the whole UI is the swatch),
  `Enter` keeps it, `Esc` reverts to the theme active on open. The choice persists to
  `state.json`, and `--theme` seeds both (state wins unless a non-default `--theme` is
  passed, like the range flags). Bundled: opentab, Catppuccin Mocha/Latte, Tokyo Night/Day,
  Gruvbox, Nord, Dracula, Rosé Pine. **Adding a theme is one `THEMES` entry** — the
  `--theme` choices come from `themes.THEME_IDS`, the web injection and both pickers
  enumerate `THEMES`. The two lazy
per-session extras keep their TUI trade-off: the static
export **omits Turns/Tools** (embedding them would be a startup-wide scan), while
`--serve` (`web.ReportServer`) exposes them as `/api/session/<id>` fetched on drill-in,
plus `/api/reload`. The server is **deliberately single-threaded** — the stores' sqlite
connections are bound to their creating thread — and binds 127.0.0.1 by default (the
browser leaks prompt titles/paths/spend; `--bind` warns beyond localhost). **`--web` is
`--serve` plus opening it in the default browser** (`web.open_report`, stdlib
`webbrowser` → `open`/`xdg-open`/Windows shell-association, so it's cross-platform),
launched from a daemon thread once the socket is listening (a console-browser fallback
can't block `serve_forever`, and it never touches the sqlite-bound store); a headless box
with no browser is a no-op, never a crash. `--demo` works
unchanged (stores transform before serialization), which is the shareable-page story:
`opentab --demo --html demo.html`.

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
  `_agg_rows`/`_mix_rows`. Keep them rendering through that one helper. In wide panes the
  CacheR/CacheW/Output cells also carry their attributed share of the row's Cost —
  `811.6k($10)` — split by list rates and scaled so the cells (plus the implicit input
  remainder) sum to the Cost column (`_price_split_cells`); unpriced $0.00 rows and narrow
  panes stay plain counts, and the Tools-tab reuse passes `price_split=False` (tool names
  aren't models).
- Versioning is a manual constant: `__version__` in `src/opentab/__init__.py` (surfaced by
  `--version`, and read by hatchling at build time via `[tool.hatch.version]`). It is not
  derived from the git tag, so bump it when cutting a release.
