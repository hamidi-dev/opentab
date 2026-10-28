# Programmatic access

OpenTab's numbers are available to scripts and agents through a JSON command line, a
Python service and an MCP server. All three read the same records as the TUI and
never write to your harnesses' data.

```sh
opentab usage summary --range 30d --group-by project
opentab mcp
```

## JSON command line

Each command prints one JSON document to stdout. Diagnostics go to stderr, and errors
exit nonzero. Every response has a `schema_version` and either `data` or `error`:

```json
{"schema_version":"1","ok":true,"data":{"sessions":[]}}
```

`opentab --help` lists commands by task; `opentab COMMAND --help` shows examples and
common options, and `--help-all` lists every option. Help never opens your data.
`--pretty` indents the JSON. Bare `opentab` opens the TUI.

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

`sessions list` returns a `session_key` starting with `ot1_`. Use it for every later
call: unlike a harness's own ID, it also names the machine and harness, so it never
collides. A native ID works only when it matches exactly one session.

### Choosing sessions

| Option | Effect |
|--------|--------|
| `--range 30d`, `--range 2026-09-01..2026-09-07` | Sessions started in that range; a single date means *since* that date |
| `--days N`, `--since`, `--until` | Older forms; `--since`/`--until` win over `--days`, which wins over `--range` |
| `--harness NAME` | Which harnesses to load |
| `--from-harness NAME` | Filter the loaded sessions; never loads more |
| `--project PATH`, `--machine NAME`, `--model NAME` | Filter whole sessions |
| `--search TEXT` | Fuzzy match on titles, projects, IDs and notes (not conversation text) |
| `--bookmarked` | Only bookmarked sessions |
| `--include-ignored` | Include sessions and projects you ignored |
| `--limit`, `--offset` | Paging for `sessions list` (default 100 from 0) |
| `--sort`, `--reverse` | `sessions list` order: `cost` (default), `tokens`, `date`, `last_activity` descending; `title`, `project` ascending |
| `--no-state` | Ignore saved notes, bookmarks, ignores and pins; edits are rejected |

The default range is all time. Detail and edit commands take no dates, because they
address one session. TUI and web options (`--theme`, `--port`, `--bind`, `--demo`,
`--no-worktrees`) are rejected. Source path options are in [Sources](sources.md).

For `models list`, `--search` matches model names. With `--catalog` it browses the
price list without opening sessions, and only `--search`, `--limit`, `--offset`,
`--no-state` and `--pretty` apply.

Each model row carries its `*_usd_per_mtok` rates, including `reasoning_usd_per_mtok`
(the output rate unless the card publishes its own), and models.dev's card facts:
`context_tiers` (rates above `above_context_tokens`, per request), `release_date`,
`knowledge_cutoff`, `max_output_tokens`, `open_weights` and `model_type`. Facts the
catalog lacks are `null`; `model_type` is `null` only for a model with no catalog card.

### Dates select whole sessions

A range selects sessions by the day their **root session started**, and each one
counts its whole usage, including subagents and activity after the range ends. A
session started August 30 and resumed September 2 counts entirely on August 30, and
a September-only query leaves it out. Day, month and year groups work the same way.

Every filter selects whole sessions. Filtering by model keeps the other models those
sessions used.

### Costs in the output

| Field | Meaning |
|-------|---------|
| `recorded_cost_usd` | Dollars the harness recorded |
| `api_equivalent_cost_usd` | Usage priced at API list rates; not a subscription bill ([Pricing](pricing.md)) |
| `unpriced_tokens` | Tokens with no recorded dollars attached (not "unknown model") |
| `api_equivalent_cost_complete` | `false` when a row mixes priced and unpriced calls it can't split; the cost is then `null` |
| `price_source`, `estimate_reasons` | Which price snapshot was used, and why a value is estimated |

GitHub Copilot usage is fully repriced at Copilot route rates; other harnesses keep
recorded dollars and add estimates for the unpriced part. Unknown models use fallback
rates, local providers price at zero. Copilot GPT-5.6+ requests with no recorded
cache writes count their uncached input as writes (`cache_write_inferred`); see
[Copilot estimates](pricing.md#copilot-list-price-estimates). The TUI's compare-model rate
is never applied here; use `models compare` instead.

### Reading a usage summary

`usage summary` returns `range`, `totals`, `group_by` and `groups`, plus three objects
that explain them:

| Object | Meaning |
|--------|---------|
| `date_scope` | The resolved `since`/`until` (`null` means open-ended) and how dates were applied |
| `accounting` | What each cost field and token category means |
| `scope` | The source, filters, matching machines and harnesses, and whether saved ignores applied |

Totals and groups share five token fields that add up: `input_tokens` (uncached),
`output_tokens`, `reasoning_tokens`, `cache_read_tokens` and `cache_write_tokens`.
`cache_write_1h_tokens` is part of the cache writes, not a sixth category. See
[Token conventions](backends.md#token-conventions).

- `token_breakdown_complete` is `false` when the categories don't add up to the
  reported total; zeros then don't mean zero usage.
- `scope.machines` and `scope.harnesses` list what matched **after** filtering.
- Summaries are never paged: MCP `limit`/`offset` are ignored. Groups always sort by
  cost, then tokens; the CLI rejects `--sort` and `--reverse` here.

## Python service

The CLI and MCP server are built on `OpenTabService`, which you can use directly with
any OpenTab store:

```python
from opentab import OpenTabService, SessionQuery

service = OpenTabService(store, args, source_key="all")
page = service.list_sessions(SessionQuery(range="30d", limit=20))
session = service.get_session(page["sessions"][0]["session_key"])
```

It returns plain dictionaries and raises `ServiceError` with a stable `code`, a
`message` and optional `details`. Listing sessions doesn't load their turns or traces.

## MCP server

`opentab mcp` runs an MCP server over stdio; it opens no network port. Add it to your
MCP client:

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

That's enough for usage and session questions. To let your agent read conversations,
search them or read raw traces, add `--allow-raw-content` and restart the server:

```json
"args": ["mcp", "--allow-raw-content"]
```

| Tool | Does |
|------|------|
| `opentab_usage_summary` | Totals and groups, like `usage summary` |
| `opentab_list_sessions`, `opentab_get_session` | Find sessions; one session's totals, note and models |
| `opentab_get_session_nodes`, `_turns`, `_tools`, `_context` | A session's subagent tree, turns, tool costs and context growth |
| `opentab_get_session_content` | One turn's raw content (needs the flag and `confirm_raw: true`) |
| `opentab_get_session_conversation` | Conversation text (needs the flag and `confirm_raw: true`) |
| `opentab_search_conversations`, `opentab_index_conversations`, `opentab_conversation_index_status` | [Conversation search](conversation-search.md) |
| `opentab_list_models`, `opentab_compare_model` | Used models, the price catalog, and a session-only comparison |
| `opentab_get_note`, `opentab_set_note` | Read or write a session note |
| `opentab_list_preferences`, `opentab_update_preference` | Bookmarks, ignores and pinned models |
| `opentab_list_sources`, `opentab_reload` | Present harnesses; reload changed records |

Enabling the flag indexes nothing by itself. Building the search index needs
`confirm_index: true` and stores conversation text as plaintext. Anything the server
returns can enter your agent's context.

Bad input (unknown fields, wrong types, out-of-range paging) is rejected before your
data is read. OpenTab errors come back as tool results with `isError: true`, so the
session keeps going.

## Raw content

Titles, folders, dates, costs and notes are already sensitive; treat captured JSON as
private. Full prompts, reasoning, commands, tool arguments and tool output are gated
further: the process must start with `--allow-raw-content` **and** each request must
ask for them.

```sh
opentab sessions turns SESSION_KEY --include-prompts --allow-raw-content
opentab sessions turns SESSION_KEY --include-content-keys --allow-raw-content
opentab sessions content SESSION_KEY CONTENT_KEY --allow-raw-content
opentab mcp --allow-raw-content
```

`--include-content-keys` (MCP `include_content_keys: true`) lists a key per turn
without reading any content; `sessions content` reads one. The MCP content tool also
needs `confirm_raw: true`. Permission is checked before anything is read, including
before any remote connection.

### Reading conversation records

`sessions conversation` reads the user and assistant text of one session, from local
OpenCode, Claude Code, Codex, Hermes, Copilot, pi and omp records. It reads records in
order; to find something, use [conversation search](conversation-search.md). Tools,
reasoning and attachments are not included; use the turn content above for those.

```sh
opentab sessions conversation SESSION_KEY --allow-raw-content
opentab sessions conversation SESSION_KEY --allow-raw-content --tail --limit 10
opentab sessions conversation SESSION_KEY --allow-raw-content --cursor NEXT_CURSOR
opentab sessions conversation SESSION_KEY --allow-raw-content --anchor ANCHOR --before 3
opentab sessions conversation SESSION_KEY --allow-raw-content --execution-id CHILD_ID
```

| Option | Default | Effect |
|--------|---------|--------|
| `--limit` | 20 | Records per window, 1–100 |
| `--max-chars` | 20000 | Text per window, 1–120,000 characters |
| `--anchor` | none | Start at a record from an earlier response |
| `--before` | 0 | Also return up to 99 records before the anchor |
| `--cursor` | none | Continue from `next_cursor` |
| `--tail` | off | Read the last window |
| `--execution-id` | root | Read one subagent, by an ID from `executions` |

`--anchor`, `--cursor` and `--tail` can't be combined. Python takes the same options
(`service.session_conversation(key, limit=20, max_chars=20000)`), and MCP's
`opentab_get_session_conversation` uses the same names plus `session` and
`confirm_raw: true`.

By default you get only the root session, not its subagents. Pass a subagent's ID from
`executions` as `--execution-id` and keep the root's `SESSION_KEY`. The session must
appear in `sessions list`; there is no separate conversation catalog.

**Reading the response.** Records come as they were stored, not as a cleaned-up
transcript: replayed prompts, resumed runs and abandoned branches can all appear.

| Field | Meaning |
|-------|---------|
| `records[].id` | This occurrence; use it as an anchor (native IDs can be `ambiguous_anchor`) |
| `message_id`, `record_id` | The harness's own IDs, where available |
| `parts[]` | Ordered text parts with `text_offset`, `text_total_chars` and `truncated` |
| `record_complete` | This response holds the whole record |
| `has_more`, `next_cursor` | More records follow |
| `has_earlier` | The window starts after the beginning |
| `history_completeness` | Always `unknown`: deleted history is never reconstructed |
| `limitations` | What was skipped or uncertain |

A long message continues on the next page with the same part ID; join parts by ID and
offset. Anchors and cursors belong to one session and subagent, and can stop working
when the source changes. Session detail reports
`capabilities.conversation_supported`, and `capabilities.conversation` when reading is
enabled.

**Limits.** Each read stays within 256 MiB of source text. OpenCode messages over
8 MiB, and JSONL lines over 8 MiB, are skipped and reported in `limitations`; an
oversized tool-output line can make a whole JSONL session unavailable
(`conversation_too_large`). Damaged lines are reported, not repaired.

**Per harness.** Hermes excludes archived sessions and their subagents; on older Hermes
databases without message IDs, anchors can change after the database is rewritten. pi
and omp need the session header within the first 64 KiB of the file.

Conversation reads are local only and are never cached, exported or added to the web
report. Only the [search index](conversation-search.md) stores conversation text, and
only when you build it.

### Remote content

Load pulled machines with `--harness remote` to use their sessions with the same keys:

```sh
opentab sessions list --harness remote
opentab sessions turns SESSION_KEY --harness remote --include-content-keys --allow-raw-content
opentab sessions content SESSION_KEY CONTENT_KEY --harness remote --allow-raw-content
opentab mcp --harness remote --allow-raw-content
```

Listing sessions and content keys stays offline. Only reading one keyed turn connects
over SSH, to that session's machine; there is no bulk read. It needs the same setup as
[reading a remote turn](machines.md#read-a-remote-turn) in the TUI:
a pulled summary, a [saved SSH connection](machines.md#saved-connections) and a
recent OpenTab on the remote.

A content key belongs to the pulled snapshot. If the remote turn has changed since,
the read fails; pull again for new keys rather than retrying. Each read gets 30
seconds and capped output, and failures return `operation_failed` without the remote's
output. Nothing fetched is cached on disk.

## Good to know

- Programmatic commands reject `--demo`; use the TUI or web report for demo output.
- CLI, MCP and the TUI share your notes and preferences. A running TUI keeps changes
  made elsewhere when it saves, but doesn't show them until you restart it.
- Ranges use the dates stores record, without timezone conversion.
- Reasoning already counted in output isn't added twice. Cache reads count as usage,
  so `tokens` is not "new text generated".
