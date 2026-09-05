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
| `cli.py`, `programmatic.py`, `__main__.py` | Commands, argument routing, startup, JSON envelopes and one-shot operations |
| `service.py`, `mcp.py` | Headless accounting API and stdio MCP adapter |
| `models.py` | Workflow, qualified session identity and summary records |
| `stores/` | Harness readers, combined views, portable summaries and warm caches |
| `tui/app.py` | Application state, accounting projections, keyboard/mouse navigation |
| `tui/renderer.py` | Terminal layout and painting |
| `tui/bindings.py`, `tui/keymap.py` | Configurable bindings, contextual actions and help |
| `web.py`, `webpage.py` | Report payload, HTTP server and self-contained HTML/CSS/JS |
| `pricing.py`, `data/models.json` | Rate lookup, cost calculations and generated catalog |
| `formatting.py`, `heatmap.py`, `themes.py` | Text, charts and shared colour palettes |
| `sources.py` | Harness discovery, selection and store construction |
| `paths.py`, `state.py`, `notes.py` | XDG locations, preferences and authored notes |
| `demo.py` | In-memory anonymization and spend scaling |
| `util.py` | Shared parsing, path, terminal, content and launcher helpers |
| `doctor.py` | Read-only environment and harness diagnosis |

Imports flow from shared helpers to stores, then to the TUI, application adapters
and CLI. Stores never import the TUI. Annotation-only back-references use
`if TYPE_CHECKING` rather than introducing runtime cycles. `__init__.py` also
re-exports the public API, which callers and tests access as `opentab.<name>`.

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

See [Backend accounting](backends.md) for normalization, deduplication, subtree
ownership and each format's limitations.

## From records to a screen

```text
harness records -> store -> root rollups + per-model rows
                       -> selected session's nodes / turns / tools / context

root rollups -> App -> range, project and machine projections
                   -> recorded / API-equivalent cost snapshots
                   -> Renderer or web report payload

selected turn -> lazy content reader -> local TUI trace
```

The TUI starts with workflow rollups. Its heavier per-model load runs after the
first paint and is reused for every scope, rather than queried once per row.
Opening a session paints a loading frame before fetching its extras. Raw traces
are a further opt-in read and never part of the rollup cache.

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
thread. Raw traces and notes remain local to the TUI. See [Web](web.md) for
serialization, browser state and security boundaries, and [Machines](machines.md)
for portable summaries.

The JSON CLI and MCP server instead use `OpenTabService`. It owns filtering,
pagination, stable serialization, exact session routing, mutations to OpenTab's own
state, and lazy detail reads without depending on curses or browser state. Adapters are
thin: `programmatic.py` maps argparse actions to service calls and emits one versioned
document; `mcp.py` validates tool inputs and maps the same calls to structured MCP
results. See [Programmatic access](programmatic.md) for their public contract.

## Diagnostics that do not repair

`doctor.py` separates report construction (`build_report`) from text rendering.
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
