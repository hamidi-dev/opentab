# Conversation search

Search the text of your past conversations across OpenCode, Claude Code, Codex,
Hermes, Copilot, pi and omp. It finds words, not meanings: identifiers and exact
wording work best. Everything stays on this machine; nothing calls a model or the
network.

Search reads a local index that you build yourself. Press `Ctrl-F` in the TUI, or run
`opentab conversations` from a terminal.

## Build the index

Search needs an index first. The first time you press `Ctrl-F`, OpenTab explains this
and offers to build one; `I` builds or refreshes it later. From a terminal:

```sh
opentab conversations index --harness all --allow-raw-content
opentab conversations status
```

The index stores the user and assistant text of each session, its title and source
identifiers, **as plaintext**, without removing secrets. Only index projects whose
text you are willing to keep on disk. It lives at
`~/.cache/opentab/conversations/index.sqlite3` (or under `$XDG_CACHE_HOME`).

| Option | Effect |
|--------|--------|
| `--allow-raw-content` | Required: your consent to read conversations and write the index |
| `--harness NAME` | Index one harness, or `all` (the default loads every present one) |
| `--project PATH` | Only sessions in this project |
| `--session SESSION_KEY` | Only this session |
| `--from-harness NAME`, `--machine NAME` | Filter what was loaded; never load more |
| `--rebuild` | Re-read and replace the selected scope even if nothing changed, e.g. after history was rewritten in place |
| `--db`, `--claude-dir`, `--codex-dir`, `--hermes-db`, `--copilot-dir`, `--pi-dir`, `--omp-dir` | Read a harness from another path |
| `--no-state` | Ignore saved project and session ignores (they apply by default) |

Indexing ignores date filters: it always stores whole sessions. Harnesses without
conversation text are rejected, and pulled machines are never indexed or searched.

### Keep it fresh

The index never updates by itself: nothing is indexed at startup, on reload, or while
you browse. Run the index command again when you need newer results, or schedule it
with cron or your system's task scheduler. A refresh skips sessions whose sources
haven't changed, so repeat runs are fast.

Check the result instead of assuming everything worked:

| Field | Meaning |
|-------|---------|
| `updated`, `unchanged`, `removed` | Sessions written, skipped as unchanged, or dropped |
| `unsupported`, `errors` | Sessions that couldn't be indexed, and why |
| `skipped_messages`, `skipped_parts`, `limited_roots` | Oversized content left out (over 8 MiB per OpenCode message) |
| `complete` | `false` if anything failed or was skipped; `true` still doesn't mean full history |

If a source can't be read, its old text is removed rather than kept as if current.

### Clear the index

```sh
opentab conversations clear --allow-raw-content
```

This empties the index and leaves your harness records and notes untouched. It is not
secure erasure: backups and the empty database file remain.

## Search in the TUI

Press `Ctrl-F`, type a query and press `Enter`. Opened from a session, search starts
inside that session; otherwise it covers all local sessions. `Enter` on a result opens
the **Conversation** tab at that message.

| Key | Action |
|-----|--------|
| `Ctrl-F` or `/` | Edit the query |
| `Enter` | Run the query; on a result, open its conversation |
| `h` / `l` | Switch between the Results and Conversation tabs |
| `j` / `k`, `J` / `K` | Move through results; scroll the preview |
| `[` / `]` | Read earlier / later messages |
| `Ctrl-G` | Filters: project (`p`), harness (`H`), UTC dates (`d`) |
| `s` / `a` / `S` | Search this session / all sessions / choose |
| `R` | Clear the project, harness and date filters |
| `I` | Build or refresh the index |
| `L` | Resume or copy the selected session |
| `Esc` | Leave the reader, then close search |
| `?` | Search help |

While you edit the query, letters type into it: press `Enter` or `Tab` before using
letter shortcuts (`Ctrl-G` works either way). Click a result to select it,
double-click to read it; the wheel scrolls whatever is under the pointer.

Active filters show as chips under the query; click a chip's `x` to remove it. Date
filters use the date each **message** was written, in UTC, not when the session
started. The welcome popup's **Got it** is remembered unless you use `--no-state`.

## Search from the command line

```sh
opentab conversations search "ERR_CONNECTION_RESET" --allow-raw-content
opentab conversations search "release navigation" --since 2026-09-08 --until 2026-09-08 --allow-raw-content
opentab conversations search "sqlite locking" --exclude-session SESSION_KEY --limit 5 --allow-raw-content
```

| Option | Default | Effect |
|--------|---------|--------|
| `--limit` | 10 | Hits to return, 1–100 |
| `--max-chars` | 6000 | Total excerpt text, 1–120,000 characters |
| `--since`, `--until` | none | Inclusive UTC message dates (`YYYY-MM-DD`) |
| `--session` | all | Search inside one session |
| `--exclude-session` | none | Leave out a session and its subagents, such as the one you're in |
| `--project`, `--harness`, `--from-harness`, `--machine` | all | Same as for indexing |

Each hit has a `session_key`, `execution_id`, `anchor`, the native `message_id`, a
timestamp, an excerpt and its limitations. Read the conversation around a hit with:

```sh
opentab sessions conversation SESSION_KEY --anchor ANCHOR --before 2 --allow-raw-content
```

For a hit in a subagent, add `--execution-id EXECUTION_ID` and keep the root's
`SESSION_KEY`. See [Reading conversation records](programmatic.md#reading-conversation-records).

`opentab conversations --help` walks through index → search → read; add
`--help-all` for every option.

## How matching works

- Your query is split into words; punctuation separates words, and accents are
  ignored. Search operators are treated as plain text.
- OpenTab first looks for passages containing all your words. Only if none match does
  it accept any word. `match_mode` says which happened.
- Results rank by how well the words match, with message text above session titles;
  not by date or cost. `rank` is an ordering, not a confidence score.
- `match_fields` shows whether a hit matched the body, the title or both. A title-only
  hit finds the session but its excerpt may not contain your words.
- Results group by session. Inside one session (`--session`) they group by message.

## Freshness and coverage

Before returning text, OpenTab checks each hit against the live source. Text that was
deleted, changed or moved is withheld, so refresh the index to find new text.

| Field | Meaning |
|-------|---------|
| `unindexed_roots` | Sessions in scope with no index entry: unsupported harnesses, never indexed, or removed after a failed read |
| `stale_metadata_roots_skipped`, `stale_executions_skipped` | Hits skipped because the source changed |
| `limited` | The search hit one of its limits |
| `excerpt_truncated` | The excerpt was cut to fit `--max-chars` |
| `overlong_tokens_omitted` | A run of over 2,000 characters without a break wasn't indexed |

Search considers up to 1,000 candidates and checks 20–100 of them live, depending on
`--limit`. It is not exhaustive.

## Use it from Python or MCP

The MCP server offers three tools. The server must be started with
`--allow-raw-content` ([MCP setup](programmatic.md#mcp-server)):

| Tool | Requires | Does |
|------|----------|------|
| `opentab_index_conversations` | `confirm_index: true` | Builds or refreshes the plaintext index |
| `opentab_search_conversations` | `confirm_raw: true` | Searches the index and checks hits live |
| `opentab_conversation_index_status` | nothing | Counts only; reads no conversations |

Clearing is CLI-only. Text returned through MCP enters your agent's context. In
Python, use `OpenTabService(..., allow_raw_content=True)` with
`.index_conversations(...)` and `.search_conversations(query, ...)`.

## Good to know

- Demo mode can't index or search; demo isn't a way to anonymize indexed text.
- Passages are up to 2,000 characters, so "all words" means all words in one passage,
  not anywhere in the conversation.
- There is no semantic or vector search, stemming, or translation.
- `--no-cache` affects usage accounting only, not this index.
- Indexed text never goes into the web report, machine exports or caches.
- With `--since` or `--until`, messages without a usable timestamp are left out.
- The index is created owner-only on macOS and Linux; on Windows it relies on your
  account folder's permissions. An incompatible index file is never wiped
  automatically.
- Slow refresh? Add `--debug` (or `--debug-log FILE`) to `index` or `search` for
  timed stages on stderr. Logs contain no prompts, paths or queries.
- The same reader limits apply as for
  [conversation records](programmatic.md#reading-conversation-records).

See also [Privacy](privacy.md).
