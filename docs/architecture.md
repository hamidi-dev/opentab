# Architecture

OpenTab translates local harness records into one accounting model, then presents
that model in a terminal or browser. The distinction matters: the UI should not
need to know whether usage came from SQLite, a JSONL transcript, or a pulled
machine summary.

This is the contributor's map. [Contributing](../CONTRIBUTING.md) covers setup,
checks and commit conventions; the [documentation index](README.md) covers usage.

## Design constraints

- **Read-only sources.** OpenTab never modifies a harness's records or settings.
  Its own files and explicit exports are listed in [Privacy](privacy.md).
- **Python 3.9+, standard library at runtime.** The only runtime dependency is
  `windows-curses`, needed on native Windows. Ruff and hatchling are development tools.
- **Accounting before presentation.** Recorded dollars and list-price estimates
  stay separate. Discarded conversation branches can still represent billed work;
  replayed history must not become new spend.
- **Bounded detail work.** Rollups make history browsable; expensive per-session
  queries and raw content are loaded only when needed.

## Package map

The project uses a `src/` layout. Its distribution is `opentab-ai`; its import
package and installed command are both `opentab`.

| Module | Responsibility |
|--------|----------------|
| `cli/main.py`, `__main__.py` | Commands, argument routing and startup |
| `api/service.py`, `api/json_cli.py`, `api/mcp.py` | Headless accounting service, JSON commands and stdio MCP adapter |
| `conversations/reader.py` | Shared conversation input validation, bounded text windows, anchors and snapshot-bound cursors |
| `conversations/index.py` | Explicit private SQLite/FTS5 text index, source-bound root replacement and grouped lexical candidates; service owns visibility and live verification |
| `accounting/models.py` | Workflow, qualified session identity and summary records |
| `accounting/tools.py` | Numeric per-call projection of recorded usage rows; ordered repeated calls and proportional attribution |
| `stores/` | Harness readers, combined views, portable summaries and warm caches |
| `remote_content.py` | Opt-in keyed SSH traces, snapshot/live identity validation, bounded transport and cancelable jobs |
| `tui/app.py` | Application state, accounting projections, keyboard/mouse navigation |
| `tui/renderer.py`, `tui/components/` | Screen composition and painting; reusable stateless layouts |
| `tui/views/` | Pure Prices, Trends, Tools, Turns and Subagents presentation from explicit inputs |
| `tui/trace.py` | Pure recorded-event formatting and trace output hit/scroll geometry |
| `tui/exporting.py` | TUI CSV dataset construction and formula-safe serialization |
| `tui/search_workspace.py`, `tui/search_layout.py` | Conversation-search interaction state and pure width-aware result/reader layout |
| `tui/search_worker.py` | Source-owning serial background service for local search, reads and explicit indexing |
| `tui/bindings.py`, `tui/keymap.py` | Configurable bindings, contextual actions and help |
| `web/report.py`, `web/page.py` | Report payload, HTTP server and self-contained HTML/CSS/JS |
| `accounting/pricing.py`, `data/models.json` | Rate lookup, cost calculations and generated catalog |
| `presentation/formatting.py`, `presentation/heatmap.py`, `presentation/themes.py` | Text, charts and shared colour palettes |
| `presentation/whats_new.py` | Shared release-note content, validation and announcement rules |
| `sources.py` | Harness discovery, selection and store construction |
| `persistence/paths.py`, `persistence/state.py`, `persistence/notes.py` | XDG locations, preferences and authored notes |
| `persistence/usage_cache.py` | Indexed, transactional OpenTab-owned scalar accounting cache; no source writes |
| `demo.py` | In-memory anonymization and spend scaling |
| `util.py` | Shared parsing, path, terminal, content and launcher helpers |
| `cli/doctor.py` | Read-only environment and harness diagnosis |
| `diagnostics.py` | Opt-in bounded JSONL cache decisions and nested phase timings; no source content |

Imports flow from shared helpers to stores, then to the TUI, application adapters
and CLI. Stores never import the TUI. Annotation-only back-references use
`if TYPE_CHECKING` rather than introducing runtime cycles. `__init__.py` also
re-exports the public API, which callers and tests access as `opentab.<name>`.

Package initializers stay lightweight: conversation readers do not load the index,
and importing the web page does not load the HTTP server. Internal imports use the
package paths above; documented root-level exports remain stable.

## One store contract

Every backend implements four core methods, plus demo configuration:

| Method | Meaning |
|--------|---------|
| `workflows()` | Root-session rollups, including descendant usage |
| `summary()` | Aggregate totals |
| `workflow_nodes(id)` | One session's recursive execution tree |
| `model_breakdown()` | Usage split by root session and model |

The optional session interface extends this without making the UI format-aware:

| Data | Method | Availability |
|------|--------|--------------|
| Turns | `message_timeline(id)` | `supports_turns(id)` |
| Tool attribution | `tool_breakdown(id)` | `supports_tools(id)` |
| Estimated context composition | `context_breakdown(id)` | `supports_context(id)` |
| Recorded turn content | `turn_content(id, content_key=None)` | `supports_turn_content(id)` |
| Conversation records | `conversation_source(root_id, execution_id=None)` | `supports_conversation(root_id)`; local OpenCode, Claude Code and Codex only |
| Received subagent prompt | `node_prompt(root_id, node_id)` | Optional method; `None` when unavailable |
| Execution turns | `node_timeline(root_id, node_id)` | Optional method; `None` unavailable, `[]` valid empty |
| Execution turn content | `node_turn_content(root_id, node_id, content_key=None)` | Optional method; owned previews or keyed full content |
| Recorded file changes | `session_change_files(id)`, `session_change_diff(id, key)` | Optional local OpenCode TUI contract; [semantics and controls](keys.md#session-changes) |

The node readers resolve exact root/node IDs within the owning leaf store, never
by agent name or sibling matches; ambiguous ownership fails closed. Execution
timelines contain only the node's own rows and prompts, with `depth=0` and original
content keys unchanged. Local OpenCode, Claude Code, Codex, OMP and Hermes implement
them; Gemini, Antigravity and Remote do not, and demo blocks the nested drill.

The measured Context curve uses turn token counts rather than another store
query; `supports_context_curve` can opt out when those rows do not describe
individual request sizes. Capabilities are per session: Hermes may retain an old
session summary after the log supplying its Turns and Tools has rotated away.

`CombinedStore` concatenates rollups and routes session extras to their exact owning
backend. Programmatic callers use a qualified session key containing machine,
harness and native ID; a bare native ID is rejected when it is ambiguous.
`CachedStore` wraps eligible leaves independently, so a change to one
harness need not invalidate the others. UI code consumes these interfaces, not
SQL columns or transcript records.

Conversation reads are a separate service path used by the TUI, CLI and MCP, not an accounting
timeline or search index. Each leaf returns `records`, `snapshot`, `execution_id`,
`executions`, `limitations`, and `ordering`. The source contains only the selected
execution's retained user/assistant text occurrences, including zero-usage messages;
it does not reconstruct the active branch or merge descendants. Cached and
machine-tagged wrappers delegate the optional methods without caching their raw
results. Remote summaries have no conversation capability or transport fallback.

`OpenTabService.session_conversation` gates raw permission first, rechecks demo,
validates the shared window options before reading, and resolves a catalog root to
its exact qualified owner. It never forwards native IDs through CombinedStore's
detail routing. Duplicate fully qualified identities fail closed for this raw
operation. Child selection uses an exact `execution_id` from the returned execution
list under that root. Roots absent from the existing session catalog are not
addressable; conversation search does not create a separate catalog. The shared `window`
function receives the qualified root key and selected source, bounds the response,
and validates continuation scope. `ConversationError` codes/messages become stable
`ServiceError` values for both adapters. MCP confirmation precedes lazy service
creation. Conversation capabilities are advertised without raw reads, with support
separate from permission. None of this raw data enters rollup/web/fleet caches or
payloads; see [Programmatic access](programmatic.md#reading-conversation-records).

An optional `conversation_manifest(root_id)` store hook supplies a cheap,
JSON-compatible source identity for explicit index refreshes. The service stores it
only after a successful full read whose pre/post manifests match, and includes one
shared reader-semantics version in both the manifest and root-content fingerprint,
so behavior changes invalidate the read shortcut and rebuild indexed passages.
Claude reuses one directory-stamp-guarded transcript lookup per explicit refresh,
while fingerprinting each root's files freshly; uncertain discovery falls back to
live reads and the lookup is released in `finally`. OpenCode hashes its execution tree and
message/part row revisions in fresh, root-scoped SQLite metadata reads, falling back
to a global database/WAL token for older or unconstrained schemas. Codex reuses one
refresh-local rollout-head/ownership discovery. Missing hooks or uncertain stamps
fall back to `conversation_source`; they never authorize indexed text. This hook is
not used by search, whose selected candidates retain live snapshot verification.

See [Backend accounting](backends.md) for normalization, deduplication, subtree
ownership and each format's limitations.

## From records to a screen

```text
harness records -> store -> root rollups + per-model rows
                       -> selected session's nodes / turns / tools / context

root rollups -> App -> range, project and machine projections
                   -> recorded / API-equivalent cost snapshots
                   -> Renderer or web report payload

selected turn -> lazy local reader or explicit SSH request -> TUI trace
Ctrl-F -> workspace -> source-owned serial worker -> local service/index + bounded reader
```

The TUI starts with workflow rollups. Its heavier per-model load runs after the
first paint and is reused for every scope, rather than queried once per row.
Opening a session paints a loading frame before fetching its extras. Raw traces
are a further opt-in read and never part of the rollup cache.
The TUI's nested Subagents reader loads one execution's timeline and content lazily
and releases its scoped raw-content cache on leaving. It does not replace root
timelines or change session Turns/Context, web views or ordinary exports. Only an
explicit nested CSV export uses execution-scoped numeric turn rows; the reader
adds no raw-content path to web, fleet, CLI or MCP.

`RemoteStore` keeps ordinary fleet reads offline. Only the managed default summary
directory can associate a session's winning source file with a saved SSH connection.
It generates snapshot-bound content keys without transport; URL entries and arbitrary
imports cannot enable traces. `remote_content.py` freezes provenance, configuration
and turn identities into a keyed request, resolves an exact unique live turn (never
an ordinal), then fetches its content through a compatible remote OpenTab JSON CLI.
The two commands share a 30-second deadline and enforce response caps. The TUI runs
that request in a cancelable worker and adopts results only for the current selection;
it retains one remote turn's preview/full content in memory, never on disk. See
[Machines](machines.md#read-a-remote-turn) for setup and failure semantics.

Reload has two jobs: refresh backend data and invalidate App's derived projections
and detail memos. Range and ignore changes only invalidate the projections they
affect. [Startup and caching](caching.md) explains these lifetimes, the incremental
cache's safety conditions, and the separate `cost` / `--goto` fast paths.

## Presentation adapters

`App` owns state and navigation; `Renderer` owns drawing. The renderer delegates
unknown attributes to its App, allowing drawing methods to consume shared state
without copying it. Most content builders return plain text lines; terminal
colour and geometry are applied when painting. [TUI internals](tui.md) covers
the view stack, selection invariants, common table framing and terminal pitfalls.

The browser starts with a headless App and serializes the same accounting model.
Recorded and API-equivalent costs both travel in the payload, so `$` is a field
swap in either frontend. The `w` comparison instead substitutes rates for one
session without changing global rollups; see [Pricing](pricing.md).

Static HTML carries rollups; the live server supplies session extras on demand.
The server is single-threaded because SQLite connections belong to their creating
thread. Raw traces, content keys and notes are absent from web/fleet payloads;
the TUI's locally authorized search and explicitly gated CLI/MCP reads are separate
content paths. Search results and indexed text are likewise excluded. See
[Web](web.md) for serialization, browser state and security boundaries, and
[Machines](machines.md) for portable summaries.

The JSON CLI and MCP server instead use `OpenTabService`. It owns filtering,
pagination, stable serialization, exact session routing, mutations to OpenTab's own
state, and lazy detail reads without depending on curses or browser state. Adapters are
thin: `api/json_cli.py` maps argparse actions to service calls and emits one versioned
document; `api/mcp.py` validates tool inputs and maps the same calls to structured MCP
results. See [Programmatic access](programmatic.md) for their public contract.

TUI search creates its own `OpenTabService` and store in a serial worker thread.
See [TUI internals](tui.md) for lifecycle and navigation.

## Diagnostics that do not repair

`cli/doctor.py` separates report construction (`build_report`) from text rendering.
Its rows carry a status, label, explanation and optional remedy. `BAD` produces a
nonzero exit code; `WARN` does not. This distinguishes a broken invocation from a
working setup with a limitation, such as short transcript retention.

The report borrows discovery, colour-path and file-readability verdicts from the
same helpers the application uses. Reimplementing those checks would let the
report disagree with the program it diagnoses. Availability checks can inspect
record markers, but the default output never displays session titles or prompts.

Path lookup needs particular care: ordinary `paths.migrated()` calls can relocate
legacy files. Doctor uses the look-only `paths.resolved()` path and runs before
startup migration, so diagnosis cannot move someone's notes or rewrite a cache.
Public output also folds home paths, counts rather than names pulled machines,
and prints only selected environment values. `--full` relaxes path redaction for
local investigation. Remedies use the detected shell's assignment syntax.

The [troubleshooting guide](troubleshooting.md) explains how to interpret the report.

## Where to document a change

Keep user controls and setup in the user guides, implementation concepts in these
contributor guides, and a local algorithm's rationale beside its code. A useful
design note explains the invariant and the consequence of breaking it; it need
not retain every debugging step or repeat the feature tour. Regression tests
belong to the module whose behavior they exercise, as described in
[Contributing](../CONTRIBUTING.md#tests--checks).
