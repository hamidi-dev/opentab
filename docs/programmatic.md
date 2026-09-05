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
For `sessions list` and `usage summary`, `--search` fuzzy-matches session titles,
projects, IDs, and notes instead.

Recorded spend and API-equivalent list-price estimates are separate fields. The
JSON API never applies the TUI's session-only what-if rate globally; model comparison
is an explicit `models compare` operation.

An aggregated Node or Tool row from an older backend can mix metered and unpriced
calls without retaining their token split. In that case `api_equivalent_cost_usd`
is `null` and `api_equivalent_cost_complete` is `false`, rather than silently
reporting only the recorded portion. Session and model rollups retain exact splits.
Node labels can also name only the dominant model. Root nodes use exact per-model
splits where available; other nodes return an incomplete result when their usage
cannot be attributed to one known model, rather than pricing a model mix at one rate.

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

A typical MCP client entry is:

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

The server advertises tools for summaries, session discovery and detail, model
prices and comparisons, notes, bookmarks/ignores/pins, source discovery, and reload.
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
opentab sessions content SESSION_KEY CONTENT_KEY --allow-raw-content
opentab mcp --allow-raw-content
```

The MCP raw-content tool additionally requires `confirm_raw: true`. Raw traces are
never inserted into rollup caches or fleet exports.
