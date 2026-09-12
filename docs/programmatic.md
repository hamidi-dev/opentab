# Programmatic access

OpenTab exposes the same accounting model through a JSON command line, a Python
service, and a dependency-free MCP server. All three read the configured harness
records through the normal store interface; none writes to those records.

## JSON command line

Resource commands write exactly one JSON document to stdout. Diagnostics belong
on stderr, and a domain or input error exits nonzero. Every response carries a
string `schema_version` and either `data` or `error`:

```json
{"schema_version":"1","ok":true,"data":{"sessions":[]}}
```

Start with these commands; each subcommand has complete `--help` output:

```sh
opentab usage summary --range 30d --group-by project
opentab sessions list --range 7d --from-harness claude --limit 20
opentab sessions get SESSION_KEY
opentab sessions nodes SESSION_KEY
opentab sessions turns SESSION_KEY
opentab sessions tools SESSION_KEY
opentab sessions context SESSION_KEY
opentab models list --range 30d
opentab models list --catalog --search sonnet
opentab models compare SESSION_KEY openai/gpt-5
opentab models pin anthropic/claude-sonnet-4-5
opentab notes set SESSION_KEY "investigate cache churn"
opentab bookmarks add SESSION_KEY
opentab ignore project add ~/work/generated-client
opentab sources list
opentab doctor --json
```

`sessions list` returns an opaque `session_key` beginning with `ot1_`. Use that key
for later detail and mutation calls: unlike a harness-native ID, it identifies the
machine and harness as well, so merged stores can route colliding IDs correctly.
A native ID is accepted only when it resolves to exactly one visible session.

For `models list`, `--search` matches model names by case-insensitive substring,
both for used models and with `--catalog`. It does not search session text. Other
session filters, such as `--range` and `--project`, still scope used-model usage.
Catalog mode has no session catalog to filter: with `--catalog`, only `--search`,
`--limit`, `--offset`, `--no-state`, and `--pretty` apply. Session and source
selections that would change the request are rejected before OpenTab discovers or
opens a store; explicitly supplying a default-valued no-op remains compatible.
For `sessions list` and `usage summary`, `--search` fuzzy-matches session titles,
projects, IDs, and notes instead.

Programmatic commands expose only options they use. Store-backed commands accept
the relevant harness paths, cache, remote-summary, and state controls; session
query options appear only on `sessions list`, `usage summary`, and used-model
listing. `sources list` accepts source-discovery paths but not cache or state flags,
while preference-only commands avoid source discovery. UI/web options such as
`--theme`, `--port`, `--bind`, `--demo`, and `--no-worktrees` are rejected.

Session query dates preserve the established precedence: `--since` or `--until`
override `--days`, which overrides `--range`. This permits one-sided explicit date
bounds while keeping existing `--days` scripts working. Detail and mutation
commands do not accept date options because they address one resource directly.

Recorded spend and API-equivalent costs are separate fields. API-equivalent cost
preserves recorded dollars and adds list-rate estimates for the unpriced portion;
it is not an all-token repricing or a subscription bill. `unpriced_tokens` means
tokens without attributed recorded dollars, **not** tokens lacking a known model
rate. Unknown models use fallback rates; recognized local providers price to zero.
See [Pricing](pricing.md). The JSON API never applies the TUI's session-only what-if
rate globally; model comparison is an explicit `models compare` operation.

An aggregated Node or Tool row from an older backend can mix metered and unpriced
calls without retaining their token split. In that case `api_equivalent_cost_usd`
is `null` and `api_equivalent_cost_complete` is `false`, rather than silently
reporting only the recorded portion. Session and model rollups retain exact splits.
Node labels can also name only the dominant model. Root nodes use exact per-model
splits where available; other nodes return an incomplete result when their usage
cannot be attributed to one known model, rather than pricing a model mix at one rate.

### Reading a usage summary

CLI and MCP summaries retain `range`, `totals`, `group_by`, and `groups`, and include
three explanatory objects so callers do not need the source checkout:

| Object | Meaning |
|--------|---------|
| `date_scope` | Resolved inclusive `since`/`until` bounds (`null` means unbounded), selection basis, usage basis, and date convention |
| `accounting` | Definitions of cost fields, token categories, reconciliation, and whole-session filtering |
| `scope` | Selected source, requested session filters, matching machines/harnesses, and effective state/ignore policy |

**Ranges select sessions by their root start date, not calls occurring within the
range.** Each selected session contributes its whole recorded usage, including
tracked descendants and activity after the range ends. Day/month/year groups also
use the root start date. A session started August 30 and resumed September 2 stays
in the August 30 bucket, including the September activity; it is excluded by a
September-only query. Dates are used as supplied by stores, without a service-level
timezone conversion. Relative bounds are resolved once per request; a single date
means *since* that date, not just that day.

All filters select whole sessions. In particular, filtering by `model` retains
other models used by those sessions. `scope.machines` and `scope.harnesses` are
sorted identities present **after filtering**, not a list of all installed or
configured sources. Empty results have empty identity lists. Harness/machine
filters do not load extra sources or pull remote data. Saved project and session
ignores apply when `saved_ignores_applied` is true; `include_ignored` or `--no-state`
bypasses them. Summaries include all matching sessions and groups: MCP `limit` and
`offset` are accepted but ignored, and groups always sort by API-equivalent cost,
then tokens, descending. The CLI does not accept ineffective summary
`--sort`/`--reverse` options.

Totals and every group expose the same normalized token fields as session model
usage: `input_tokens` (uncached), `output_tokens`, `reasoning_tokens`,
`cache_read_tokens`, and `cache_write_tokens`. These five categories are additive;
`cache_write_1h_tokens` is a **subset** of cache writes, not a sixth category.
Repeated context and cache reads count toward usage, so `tokens` does not mean
newly generated text. Reasoning already included in a source's output is not added
again. See [Token conventions](backends.md#token-conventions).

`token_breakdown_complete` indicates whether the normalized model categories
reconcile with reported totals. Legacy uncached input may be inferred from the
remaining token count. Missing or inconsistent model rows leave the original
`tokens` total intact and set this flag to false; zero category counts in that case
do not imply zero usage. True means reconciliation, not proof that every source
record was retained. Model/provider groups describe available model rows, so their
categories may reconcile even when the overall session totals do not.

## Python service

`OpenTabService` is the presentation-independent boundary used by the JSON CLI and
MCP server. Construct it with any store implementing OpenTab's store contract:

```python
from opentab import OpenTabService, SessionQuery

service = OpenTabService(store, args, source_key="all")
page = service.list_sessions(SessionQuery(range="30d", limit=20))
session = service.get_session(page["sessions"][0]["session_key"])
```

The service returns plain dictionaries and raises `ServiceError` with a stable
`code`, human-readable `message`, and optional `details`. Session extras stay lazy:
listing sessions does not read turns, traces, or model details for every session.

`--no-state` makes authored notes and preferences invisible and rejects mutations.
CLI and MCP changes share the TUI's authored files. TUI preference saves merge only
local set changes, so an already-open TUI does not undo external bookmark, ignore,
or pin changes when it exits. External preference changes are not live-refreshed
into that TUI's current view.

Programmatic data access currently rejects `--demo`: demo transformation lives in
the interactive presentation path, and returning partially transformed detail would
be less safe than failing explicitly. Use the TUI or web frontend for demo output.

## MCP server

Run the newline-delimited JSON-RPC server over stdio:

```sh
opentab mcp
```

A typical MCP client entry for usage queries and session metadata is:

```json
{
  "mcpServers": {
    "opentab": {
      "command": "opentab",
      "args": ["mcp"]
    }
  }
}
```

**Optional: enable conversation retrieval and raw traces.** Add `--allow-raw-content`
to the server arguments and restart the MCP server:

```json
{
  "mcpServers": {
    "opentab": {
      "command": "opentab",
      "args": ["mcp", "--allow-raw-content"]
    }
  }
}
```

Without this flag, conversation reads, search, indexing, and raw trace reads are
disabled; ordinary usage queries still work. Enabling it does not index anything
automatically. When you explicitly ask your agent to build or refresh the local
conversation index, it can call `opentab_index_conversations` with
`confirm_index: true`. This persists sensitive conversation text in plaintext.
Conversation search and reads require `confirm_raw: true`; returned text can enter
the agent's model context. See [conversation search](conversation-search.md) for
setup, supported sources, and refresh behavior.

The server advertises tools for summaries, session discovery and detail, model
prices and comparisons, notes, bookmarks/ignores/pins, source discovery, reload,
and conversation reads, search, indexing, and index status.
Tool inputs reject unknown fields, wrong JSON types, unsupported enum values, and
out-of-range pagination before they reach a store. Domain failures are successful
JSON-RPC responses with `isError: true`, allowing the client to inspect the stable
OpenTab error code without losing the MCP session.

The server supports the established MCP initialize flow and the newer discovery
metadata flow. It remains alive after malformed input and never writes protocol
diagnostics to stdout.

## Raw content

Titles, directories, dates, token counts, costs, and authored notes are sensitive
even though they are structured. Treat captured JSON and MCP responses as local
data, just like the interactive views.

Full prompts, content keys, reasoning, commands, tool arguments, and tool results
have an additional gate. They are unavailable unless the individual operation asks
for them and the process was explicitly started with `--allow-raw-content`:

```sh
opentab sessions turns SESSION_KEY --include-prompts --allow-raw-content
opentab sessions turns SESSION_KEY --include-content-keys --allow-raw-content
opentab sessions content SESSION_KEY CONTENT_KEY --allow-raw-content
opentab mcp --allow-raw-content
```

The MCP raw-content tool additionally requires `confirm_raw: true`. Permission is
checked before reading raw content, including before any remote trace transport.
`--include-content-keys` (MCP: `include_content_keys: true`) lists opaque keys without
fetching traces, even when raw-content permission is enabled. Requesting full
prompts is separate from requesting keys.

### Reading conversation records

`sessions conversation` is a public **record-reading API, not conversation search**.
It reads retained user/assistant text from local OpenCode, Claude Code, and Codex
records, independently of usage-bearing turns. Zero-usage messages inside an
accessible session are preserved. Tools, reasoning, and attachments are not part
of this text-only view; use the separately gated turn-content API for raw traces.

```sh
opentab sessions conversation SESSION_KEY --allow-raw-content
opentab sessions conversation SESSION_KEY --allow-raw-content --tail --limit 10
opentab sessions conversation SESSION_KEY --allow-raw-content --cursor NEXT_CURSOR
opentab sessions conversation SESSION_KEY --allow-raw-content --anchor ANCHOR --before 3
opentab sessions conversation SESSION_KEY --allow-raw-content --execution-id CHILD_ID
```

The qualified **root must already be present in the selected session catalog**.
There is no separate conversation-only catalog; sessions absent from usage
discovery cannot be addressed through this API or conversation search. Unique
native root IDs also work, but qualified keys avoid machine/harness collisions.
Even a fully qualified key is rejected if it has duplicate catalog entries.

Default scope is **only the root execution**, not a merged descendant conversation.
Use precise child IDs from the response's `executions` list as `--execution-id`;
keep the catalog root as `SESSION_KEY`. Do not substitute an agent name, a sibling
ID, or a child ID as the root selector. Each read returns only the selected
execution's records. These are **retained original occurrences, not active-branch
reconstruction**: replayed prompts, resumed rollouts, and discarded branches may
remain as separate occurrences. Consult `ordering`, `origin`, source locators, and
`limitations` rather than assuming a normalized transcript or complete retention.

| Option | Default | Bounds / behavior |
|--------|---------|-------------------|
| `limit` | 20 | 1..100 records |
| `max_chars` | 20000 | 1..120000 text characters |
| `before` | 0 | 0..99 records before an anchor; nonzero requires `anchor` |
| `anchor` | absent | Nonempty returned record anchor, at most 1024 characters |
| `cursor` | absent | Opaque `next_cursor`, at most 8192 characters |
| `tail` | false | Select the last window |
| `execution_id` | root | Nonempty exact execution ID from `executions` |

`anchor`, `cursor`, and `tail=true` are mutually exclusive. Windows return bounded
structured `records`, execution metadata, and `next_cursor` for continuation.
Keep anchors/cursors opaque and tied to the same qualified root and execution;
changed source snapshots can invalidate continuation. The text budget is not a
byte cap on the entire JSON envelope. Python callers use the same options:

```python
service = OpenTabService(store, args, allow_raw_content=True)
result = service.session_conversation(session_key, limit=20, max_chars=20000)
```

MCP exposes `opentab_get_session_conversation` with these same option names,
`session`, and required `confirm_raw: true`. The server must also have been started
with `--allow-raw-content`. Confirmation is checked before lazy service creation;
the service checks permission before resolution and validates options before a
conversation reader runs. Demo is rejected even if enabled after construction.
Shared reader/window errors retain their stable code and message as `ServiceError`,
CLI error envelopes, or MCP tool errors.

Session detail reports `capabilities.conversation_supported` independently of raw
permission, and `capabilities.conversation` only when supported and enabled.
Capability checks do not read conversation text or guarantee retained source
availability. This API is **local only**: remote summaries do not support it and
never trigger SSH conversation reads. Conversation records, anchors, and cursors
are not added to rollup caches, web reports, or fleet exports. OpenTab does not
persist content merely by reading a conversation; CLI/MCP clients can retain their
requested output. The separate [conversation search index](conversation-search.md)
persists text only through an explicit indexing operation. Its `conversations
index/search/status/clear` commands and MCP tools do not change the metadata-only
meaning of `sessions list --search`.

**Reading the response:** `records[].id` identifies a source occurrence, while
`message_id` and `record_id` preserve native IDs where available. An exact native
message/record ID can also be an anchor, but duplicate occurrences produce
`ambiguous_anchor`; use the source-qualified returned `id` instead. Each record
retains its role, raw timestamp, parent/execution IDs and ordered text `parts`.

Large messages continue across pages rather than disappearing behind a fixed
first-80-message limit. Each part reports `text_offset`, `text_total_chars` and
`truncated`; a continued part keeps its ID. Reassemble by record/part identity and
offset, not by appending whole rendered messages. `record_complete` means that
this response contains the entire text-only record. `has_more` / `next_cursor`
describe forward continuation; `has_earlier` says the window starts after the
beginning. `history_completeness` remains `unknown`, even at the end of a page
sequence: missing/deleted history and omitted nontext are not reconstructed.

Reads are fresh and bounded at source as well as output. OpenCode permits up to
256 MiB of selected text; JSONL permits up to 256 MiB of selected file data and
8 MiB per physical line. An oversized **excluded tool-result line** can therefore
make a JSONL source unavailable with `conversation_too_large`. JSONL parse errors
are surfaced as a skipped-record limitation, not repaired or silently considered
complete. Budgets and strict parsing bound failure cases; they are not an archive
or a promise to read every damaged/oversized transcript. Unflagged wrapper-like
text remains verbatim; explicit synthetic flags are excluded where recorded.

### Remote content

Load the managed fleet with `--source remote` to address its sessions through the
same qualified session keys. For example, after pulling a machine:

```sh
opentab sessions list --source remote
opentab sessions turns SESSION_KEY --source remote --include-content-keys --allow-raw-content
opentab sessions content SESSION_KEY CONTENT_KEY --source remote --allow-raw-content
opentab mcp --source remote --allow-raw-content
```

Listing sessions, capabilities and turn keys stays offline. Only the keyed content
call connects over SSH, routed to the exact owning store. There is no remote bulk
content read: `RemoteStore.turn_content(id)` without a key returns an empty mapping.
Raw-content permission does not make URL entries or arbitrary snapshot imports
trace-capable. The summary must be loaded through the managed default cache
directory and associated with a saved SSH entry and compatible remote OpenTab CLI.
The trace command is derived from the export `cmd` where that is a plain OpenTab
invocation, and configured as an explicit `trace_cmd` argv prefix where it is not;
see [remote setup](machines.md#saved-connections).

Remote content keys identify a frozen snapshot, not a remote row ordinal. OpenTab
compares the selected turn's identity and accounting fields against the live remote
timeline and requests its unique live key. Stale or ambiguous matches fail; refresh
the summary and obtain new keys rather than retrying with guessed indices. The
timeline lookup and content read share a 30-second deadline, with per-response caps
of 16 MiB stdout and 64 KiB stderr, 100,000 timeline rows and 10,000 content events.
CLI/MCP transport failures use an `operation_failed` envelope with a safe message,
not remote stderr or response payloads.

Remote reads have no raw-content disk cache. The TUI separately retains only one
selected remote turn's preview and full content until navigation or reload. Raw
traces never enter rollup caches; traces and content keys never enter web payloads
or fleet exports. Callers remain responsible for any CLI/MCP output they capture.
