# Conversation search

Search retained OpenCode, Claude Code and Codex text across the local sessions
OpenTab can already discover. This is **lexical search**, not semantic memory:
identifiers and shared wording work best. It does not summarize conversations,
learn a personal profile, call an embedding model, or use the network.

## Build deliberately

Nothing is indexed during normal startup, reload, accounting queries or TUI/web
browsing. Building an index is a separate action that **persists sensitive text**:

```sh
opentab conversations index --project ~/work/my-project --allow-raw-content
opentab conversations status
opentab conversations search "retry sqlite locking" --project ~/work/my-project --allow-raw-content
```

`--harness` selects which sources to load; `--from-harness` filters the loaded
catalog. Index/search also accept `--machine`, `--project` (an exact normalized
project path), and `--session SESSION_KEY`. Saved project/session ignores apply;
`--no-state` disables saved ignores as it does elsewhere. Remote summaries are
unsupported, not silently fetched over SSH.

Index refresh first compares cheap source manifests for the scoped roots. An
unchanged manifest and unchanged catalog metadata reuse that root without opening
its full conversation reader. A missing, changed or unverifiable manifest takes
the authoritative path: OpenTab reads the root and all addressable executions,
checks the manifest again, and only then stores the text and manifest atomically.
The first refresh after upgrading an older index therefore reads each selected
root once to seed manifests. `--rebuild` always bypasses the shortcut.
`CONVERSATION_READER_VERSION` is also part of each root-content fingerprint; bump
this single token whenever discovery, extraction, ownership, or chunk projection
semantics change so unchanged source snapshots still rebuild their passages.

Claude manifests cover every main/resumed transcript and owned subagent sidecar
with device, inode, size, mtime and ctime stamps. Each explicit refresh discovers
the transcript tree once and reuses a session-to-path lookup while directory stamps
remain unchanged. File stamps are still checked per root. Directory changes or
uncertain discovery disable the lookup and manifest shortcut for the rest of that
refresh, falling back to live discovery and full reads. The lookup holds no text,
is cleared even after errors, and never changes standalone reads or startup caching.
Hidden entries remain excluded as in the original glob search.

Codex discovers rollout heads,
live/archive filename winners and ownership once per stable refresh, then derives
root-specific manifests; additions, deletion, replacement, reparenting and winner
changes invalidate affected roots.

OpenCode hashes each root's execution membership and message/part row identities,
ownership, creation/update stamps, and durable event sequences where available.
Every manifest is read from a fresh read-only SQLite snapshot using session-indexed
metadata queries; it does not extract JSON or read conversation bodies. Ordinary
edits, imported rows, deletions and reparenting invalidate the affected roots, not
unrelated history. It uses every row's revision, not just counts or the newest
timestamp, and does not trust `session.time_updated` to track text changes. Recent
same-millisecond, missing or future row-update stamps disable the shortcut.

Schemas lacking the required row revisions or unique IDs retain the conservative
main-DB/WAL fingerprint, including the duplicated 48-byte WAL-index header but not
the later `-shm` reader/lock region. The first refresh after upgrading from global
OpenCode manifests reads each selected root once to seed root-local manifests;
unchanged text need not be rewritten. Scoped refreshes never mark untouched roots
current. Neither path changes the accounting rollup cache.

This is incremental source verification, not an incremental parser or watcher.
Manifests are change detectors, not content-integrity hashes: they rely on source
paths/filesystem metadata, Codex ownership heads, and OpenCode's writer-maintained
row revisions. Direct SQL edits/restores that preserve OpenCode row revisions and
event sequences can evade its shortcut, just as file rewrites preserving filesystem
stamps can evade file manifests. Use `--rebuild` when investigating rewritten
history or a writer that bypasses those revision updates. Rebuild still performs
the full readers; search always independently verifies returned evidence live.

The result includes `updated`, `unchanged`, `removed`, `unsupported`, `errors`,
`complete`, and index counts. A finished command can report a partial build via
`complete: false`; inspect these fields rather than assuming every root succeeded.
Here `complete` means no root-level failures or unsupported readers, not complete
historical retention or inclusion of every character; source limitations remain.
Failed source reads remove that root's old text instead of retaining apparently
current evidence. A refresh prunes removed/ignored roots only inside its requested
scope and loaded source origins. Changing `--db` or a harness directory does not
treat another origin's roots as deleted. A native `--session` selector is resolved
to a qualified key before pruning. One qualified root has one indexed snapshot;
indexing the same key from another origin replaces that key, not unrelated roots.

Use `--rebuild` to replace the selected scope even when snapshots match. To clear
all indexed content, independent of source discovery:

```sh
opentab conversations clear --allow-raw-content
```

Clear leaves an empty database file to coordinate concurrent readers/writers. It
attempts compaction, not forensic secure erasure or deletion of OS backups. Index
status and clear do not create a missing index or scan harness data.

## Search and inspect

```sh
opentab conversations search "ERR_CONNECTION_RESET" --allow-raw-content
opentab conversations search "release navigation" --since 2026-09-08 --until 2026-09-08 --allow-raw-content
opentab conversations search "cached layout" --session SESSION_KEY --allow-raw-content
opentab conversations search "sqlite locking" --exclude-session CURRENT_ROOT_KEY --limit 5 --max-chars 3000 --allow-raw-content
```

The query is tokenized using SQLite FTS5 `unicode61` with accent folding, then
quoted as literal terms. User-supplied SQL/FTS operators are not executed.
Punctuation separates tokens: this is not exact punctuation-preserving substring
search. All terms are tried first within a chunk (including its session title);
only when that yields no eligible candidates is any-term matching used. The
response names this choice as `match_mode`. No translation, stemming, query
rewriting, or claim of understanding paraphrases is involved.

Matches rank by BM25, with body text weighted above the session title, not by
recency or cost. `match_fields` distinguishes body, title, or both: a title-only
match is session discovery, not proof that the excerpt contains your words.
`rank` is a lexical ordering value, not a confidence/probability or answer score.

Global results group by root session. The candidate pool admits one execution
per root before extra executions, retaining alternatives when a root's best
match is stale but its child is unchanged. With `--session`, results group by record within that
root, including its indexed executions. Replayed occurrences remain attributable
to their sources rather than being treated as separate human decisions. Pass
`--exclude-session` to exclude the caller's current root and its children; MCP
cannot infer every client's current session automatically.

`since`/`until` are inclusive **UTC message dates**, unlike accounting filters that
use root-session start dates. Unknown/naive timestamps are excluded when these
filters are present. A resumed session can therefore match on the day of its
answer even when it began earlier.

Each hit includes a qualified `session_key`, `execution_id`, source `anchor`,
native `message_id`, timestamp, source locator, excerpt and limitations. Read its
surroundings with the [bounded conversation API](programmatic.md#reading-conversation-records):

```sh
opentab sessions conversation SESSION_KEY --execution-id EXECUTION_ID --anchor ANCHOR --before 2 --allow-raw-content
```

Result limits are 1..100 hits, default 10. The combined excerpt budget is
1..120,000 characters, default 6,000; metadata/envelope bytes are not included.
Smaller budgets retain the match where possible, not just a prefix of distant
context. `excerpt_truncated` marks further output-budget clipping; budgets smaller
than the matched token cannot show the whole token.

## Freshness and limits

Current catalog scope, source origin and saved ignores restrict candidates before
ranking. Candidate metadata is checked again from the same transaction that read
its chunk, and selected execution snapshots are verified live before returning
text. Deleted, changed, inaccessible or differently owned evidence is withheld.
Search itself does not rewrite the index: refresh explicitly to find new text.
Refresh manifests do not replace this per-result live verification.

`unindexed_roots` counts visible catalog roots in the requested scope that have no
index entry, after saved ignores and any session exclusion. It includes harnesses
without conversation readers, supported roots not yet indexed, and roots removed
after a failed read. It is not a count of missed refreshes: refreshing cannot make
an unsupported harness searchable. Use harness-scoped searches to narrow the count;
explicit refresh reports distinguish unsupported readers from source errors.

`stale_metadata_roots_skipped`, `stale_executions_skipped` and `limited` expose
additional coverage gaps. At most 1,000 fairly admitted execution candidates
(or grouped records for an in-session query) are considered,
and at most 20..100 distinct executions are checked live depending on the result
limit. These are bounded checks, not a guarantee of exhaustive results. Live
source checks can dominate query time; this is not a pure FTS latency benchmark.
Reading a validated snapshot does not freeze a concurrently running harness.

Text chunks are at most 2,000 characters with up to 200 characters of overlap.
Conservative boundaries split only at ASCII nonalphanumeric separators, avoiding
token fragments caused by Python and SQLite using different Unicode tables.
Unbroken spans over 2,000 characters are omitted with
`overlong_tokens_omitted`; this can omit a long non-ASCII span that SQLite itself
would tokenize more finely. AND is chunk-scoped, not whole-conversation-scoped.
The original source remains readable even when a span is not indexed.

Other [conversation reader limits](programmatic.md#reading-conversation-records)
still apply: catalog-only roots, supported retained formats, explicit synthetic
exclusions, source-size budgets, and no reconstruction of the active branch or
deleted history. No TUI search screen or vector index is included yet.

## Storage and APIs

The derived SQLite/FTS5 database is
`$XDG_CACHE_HOME/opentab/conversations/index.sqlite3` (default
`~/.cache/opentab/conversations/index.sqlite3`). It is separate from rollup caches
and authored notes. `--no-cache` controls accounting rollups,
not this explicitly requested index. No index contents enter web or fleet exports.

Source records stay read-only. The index stores decoded user/assistant text,
titles and source identifiers in plaintext without automatic secret redaction.
Only index projects whose recorded text you are willing to persist. POSIX creation
uses owner-only directories/files; insecure existing immediate paths and symlinks
are rejected, not silently repaired. On Windows, protection depends on the account
directory's ACLs. SQLite transactions coordinate writes; schema/corruption/FTS5
errors fail safely without dumping text. Incompatible files are never auto-wiped.
The manifest table is an additive migration from the shipped v1 index schema;
legacy indexes remain searchable and migrate only on an explicitly authorized
write open, preserving their roots and passages.

Python uses `OpenTabService(..., allow_raw_content=True).index_conversations(...)`
and `.search_conversations(query, ...)`. MCP adds:

- `opentab_index_conversations`, requiring `confirm_index: true` and process
  `--allow-raw-content`; it explicitly writes a persistent plaintext index.
- `opentab_search_conversations`, requiring `confirm_raw: true` and that process
  flag; it reads the existing index and verifies candidate source snapshots.
- `opentab_conversation_index_status`, counts only, without creating a service or
  reading source conversations. Clear is CLI-only.

All index actions reject demo mode. Local indexing makes no provider requests;
text later returned through MCP can enter the client's model context. Treat the
index and captured results as sensitive data, not as a backup or verified facts.
