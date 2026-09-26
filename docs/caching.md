# Startup and caching

Persistent rollups avoid rereading unchanged history; in-memory memos avoid
repeating queries while navigating. Neither is an archive: if a harness deletes
its history, the cache must not preserve it as recorded usage.

See [Architecture](architecture.md) for the package map and [Pricing](pricing.md)
for recorded versus estimated spend.

## Debugging a slow run

Use `opentab --debug` for the TUI, `opentab --debug --timings` for startup only,
or `opentab web --debug` for the browser server. The diagnostic stays active for
reloads and navigation throughout that process. `--debug-log FILE` implies debug
mode and selects a **new** file; an existing destination is refused.

The default is a unique JSONL file under `$XDG_STATE_HOME/opentab/debug/`
(`~/.local/state/opentab/debug/` without an override). Its path is printed to stderr
at startup; stdout and the curses display remain unchanged. Each record is flushed
immediately, so `tail -f FILE` shows progress while the application is still running.

Events cover:

- Source detection/build, persistent-cache reads/writes and rejected payloads.
  Cache reads separate file reading, JSON decoding and payload validation.
- Fingerprint hits/misses, changed input kinds (DB/WAL/SHM/file), size/mtime changes,
  splice rejection reasons and affected-file/session counts. Input lists are capped
  at 20 changes per decision, with the omitted count recorded.
- A repeated catalog read before persistence emits `cache.decision: memory_hit` and
  avoids duplicate decoding. Explicit conversation-index completion logs counts of
  skipped oversized messages/parts, limited roots and its `complete` verdict.
- OpenCode scalar-cache restoration, in-memory reuse, revision refreshes and counts
  of reused/decoded native messages and reread legacy messages. `usage.native_summary`
  separates payload fetching, decoding, projection and insertion; metadata/legacy
  query events include SQLite execute/fetch time as `sql_ms`. Progress is emitted
  every 1,024 native rows, without per-message content or IDs.
  Reread reasons distinguish rows absent from the active scalar cache (`not_cached`),
  changed revisions, unreliable revisions and recent/future timestamps. `not_cached`
  does not necessarily mean a newly created source message. A rollup hit explicitly
   records that scalar-cache restoration is deferred. Session entry loads only its
   executions from the indexed scalar sidecar. `usage.slow_row` reports a hashed
   row locator, input size and decoding time for large/slow reads; streamed time
   includes incremental Blob reads. It never includes message content.
- Deferred model loading, reload invalidation, session memo readiness and node,
  Turns, Tools, Context and trace reads. Turns separates message queries, tool
  attribution and readable-content markers. Changes file/diff reads are timed on
  their worker; web session extras have their own outer phase.
- Indexed scalar sidecar reads distinguish all-history and subtree scope, row
  fetching and tuple reconstruction. `usage.sidecar_written` reports rows actually
  upserted, deleted and unchanged, after the transaction commits. Sidecar rejection
  and fallback events explain missing/invalid files without exposing their paths.
- `usage.decode_strategy` identifies streamed oversized reads versus the older
  interpreter fallback. `usage.large_row.start/end` brackets oversized decoding;
  `size_unit` distinguishes fetched text characters from streamed bytes. Native
  summaries count streamed and unusable projections as well as reused/decoded rows.
  `sql_scalar_projection` reports the bounded native-row fast path; native summaries
  split reread rows into `sql_projected`, `streamed` and `python_full_decode`.
  SQL scalar extraction is included in `payload_fetch_ms`; subsequent Python
  decoding sees only those fields. Compare the whole refresh as well as each phase
  when evaluating this change: moving work into SQLite changes its timing bucket.
- Changes separates reader opening, metadata validation, snapshot queries, native
  edit queries and selected patch-body reads. `opencode.changes_strategy` records
  the requested optimization; `sql.plan` records the **observed** plan, including
  candidate/native materialization flags and counts of scans, indexed searches,
  correlated subqueries and temporary sorts. These bounded summaries contain no SQL,
  parameters or arbitrary query-plan text. A MATERIALIZED hint is not proof that
  a particular SQLite version materialized the relation; compare the observed flags.
  `prepare_ms` is the extra debug-only plan preparation cost; query `execute_ms`
  and `fetch_ms` exclude it, while the enclosing wall-clock span includes it.
- `changes.queued`, `changes.execute` and `changes.completed` correlate worker and
  request numbers, queue wait, execution, cancellation and stale-result discard.
  `app.changes_result` records adoption by the TUI. Cache hits on reopening a diff,
  cache invalidation and file/patch result counts are logged at transitions rather
  than on every paint. Unavailable patches have static rejection reasons, including
  stale/unowned keys, source changes, invalid locators and absent recorded content.

`*.start`/`*.end` records pair by `seq`/`span`, with `parent` identifying nested
work. They include wall-clock Unix `time`, monotonic `elapsed_ms`, process/thread
IDs, elapsed `duration_ms`, and peak **process** RSS where supported. Durations
include children, so do not sum nested phases. Peak RSS is a lifetime high-water
mark, not current RSS, filesystem cache, or Windows' total WSL memory. Logging and
per-row clocks add overhead; use debug mode to locate work, then compare normal
runs with the same measurement boundaries.

On Linux, span starts and ends also report current `rss_mib`, anonymous/file-backed
RSS and process swap from `/proc/self/status`, when available. End records include
`rss_change_mib` (end minus start); a negative change can reveal released memory
even when peak RSS stays high. These are process snapshots, not per-span peak
allocations. Nested/concurrent changes must not be summed, and file-backed process
RSS is not the system-wide filesystem cache. Other platforms may expose only peak
RSS. Inspect Windows VmmemWSL separately when diagnosing total WSL memory.

For a navigation regression, run the full TUI with `--debug`, open the affected
session and Changes list, open a recorded patch, leave/revisit it, then reload with
`r`. Check `app.session_ready`, queue/result events and matching query start/end
records. `--debug --timings` covers startup/accounting only; it cannot verify
navigation, worker queueing or reload. Retain a normal cache-enabled run first;
use a separate new log for any deliberate `--no-cache` comparison.
An inner `cache.disk_read.end` with `FileNotFoundError`, followed by a
`cache.reject` reason `missing`, is an ordinary first-run cache miss. Follow the
outer outcome before treating every inner error record as an application failure.

End records also include `thread_cpu_ms`, `process_cpu_ms` and
`wall_minus_thread_cpu_ms`. High wall time with little thread CPU suggests waiting
(I/O, locks or scheduling); it does not by itself identify a slow disk. Process CPU
includes other threads and can exceed wall time. These fields help separate Python/
SQLite computation from host contention without changing cache behavior.
Virtualized clocks can also disagree; treat CPU/wall differences as diagnostic
evidence rather than exact I/O-wait accounting.

Logging is off by default. Logs contain static operation/reason labels, runtime
versions, counts and timing metadata; source paths and session IDs are run-local
salted hashes. SQL, arguments, prompts, titles, model names, tool output, notes,
and exception messages are not recorded. Each run stops logging at approximately
20 MiB with a `debug.limit` record; old logs remain until removed manually.
Explicit debug logging also works with `--demo` and `--no-state`; those options
still suppress their usual preference writes.

## Startup: rollups before detail

`App.__init__` calls `store.workflows()` to obtain session rollups. It leaves
`_model_by_root` empty until the model breakdown is loaded. The TUI paints first,
then `App.run()` calls `_ensure_models()` before handling ordinary navigation.
A startup warning gets input first, so its modal does not appear frozen behind
a blocking scan. This is deferred synchronous work, not a background worker.

`_load_model_cache()` groups `store.model_breakdown()` by root session, fills
model counts, reconciles unpriced tokens, and computes API-equivalent costs.
Day, month, project, and session views then aggregate those rows in memory;
they do not query the backend once per visible session.

Deferral matters particularly for OpenCode's message-table scan. A cold file
backend may already parse its corpus for `workflows()` and reuse it for model
rows; a warm cache avoids that parse. Keep model loading out of `App.__init__`,
and tolerate missing model rows in the first frame. The web command instead
loads models explicitly before building its report.

OpenCode v2 accounting materializes a scalar-only temporary table, avoiding the
full message JSON normalization needed by detail readers. Worked-time events and
the deferred model aggregation share these rows. Native aggregate residuals reuse
the same numeric usage, rather than running another whole-history message scan.
Small messages use the standard JSON decoder one at a time; messages over 8 MiB
use a validating scanner that skips inline content without decoding it into an
object tree. Only accounting fields reach the temporary table or persistent cache.
On Python 3.11+, oversized TEXT cells stream through SQLite's read-only Blob API
in 64 KiB byte chunks. Python 3.9/3.10 lack that API and still fetch a whole
oversized source cell as a Python string before bounded-batch validation; this
older-runtime allocation is unresolved. Neither runtime discards accounting fields.
The source cell's JSON is validated even when most of its content is discarded.
SQLite's source mapping and page cache are bounded to 64 MiB and 16 MiB per reader.

## Lazy session reads

Subagent nodes, Turns, Tools, and estimated Context composition are per-session
reads, gated by backend capabilities. Opening a session prefetches its supported
extras together; merely browsing the history does not fetch them all.

`session_data_ready()` checks readiness before the renderer paints a loading
placeholder. The event loop then runs `prefetch_session_data()` and repaints.
Keep these gates aligned: fetching less than readiness requires creates a
loading loop, while fetching during drawing hides the placeholder behind work.
The amount of parsing is backend-specific, not necessarily a whole-corpus scan.

OpenCode's mixed v1/v2 detail reads put explicit subtree filters inside **each
source branch**, before normalizing messages or expanding inline parts. SQLite
3.37 cannot push an `IN (SELECT ...)` filter through a UNION view when aggregation
or ordering prevents flattening. A filter outside the view is therefore insufficient.
Query-local subqueries reuse the compatibility projections and keep each alias
independently filterable; a shared CTE can materialize the whole selected session.
Native v2
prompt text is read directly from the message; legacy prompt lookups use the
original part table to avoid a correlated scan of the combined part view. Exercise
mixed databases with unrelated history when checking session-entry performance.

V2 Turns and Tools share a one-scope metadata memo keyed by source `data_version`,
root and own/subtree mode. It contains tool names, part types and readability
booleans, never bodies. Metadata projections avoid tool-output normalization.
Tools joins indexed numeric accounting rather than reparsing message tokens/costs.
Temporary writes use savepoints so they cannot leave a source snapshot pinned
between refreshes. Full traces and Changes retain their separate content readers.

On Blob-capable interpreters, changed native messages up to the 8 MiB decode bound
can use one multi-path SQLite extraction of the seven accounting fields. Python
then decodes only that projection, avoiding object creation and duplicate-key
callbacks for discarded inline content. The actual SQLite library must pass an
escaped-key lookup probe; older JSON1 semantics keep the established reader.
Missing `time` stays distinct from explicit JSON null because only absence allows
the native timestamp fallback. Non-finite/unsupported projected values also fall
back to the existing validator. Oversized messages retain bounded Blob streaming;
this optimization does not push giant cells through SQLite's JSON materialization.

Changes uses separate worker-owned connections for both file lists and keyed diffs.
Those connections receive the same read tuning as the main store. Its snapshot
message, native part, tool-message and prompt-message reads all need explicit
subtree filters, including the correlated uniqueness checks. A tree-first join
alone does not bound a UNION-view materialization. Keyed diffs still revalidate
the selected occurrence's ownership and revision before reading its patch body;
background execution and App memoization cannot substitute for scoped SQL.
For v2 keyed reads, a metadata-first candidate lookup additionally restricts source
tool/prompt message IDs and part-uniqueness reads, so unrelated messages inside the
same session do not need normalization. All occurrences of candidate IDs remain
visible to duplicate-ownership checks. File lists filter native completed edit candidates
before output normalization, then fence candidate evaluation before ownership and
metadata predicates. `LIMIT -1 OFFSET 0` prevents flattening even on SQLite 3.37,
which can inline these relations despite `MATERIALIZED`; the unlimited row count
preserves every candidate. A second fence reuses validated edits across projections.
The debug plan flags expose materialization; normalization-count tests check actual
evaluation for lists and keyed reads in pure and mixed schemas. The independent uniqueness
reader still sees all part kinds and statuses, using metadata without normalized
output: a read tool or unfinished edit with the same ID must invalidate ownership.
Tool-change message joins project only role and
parent metadata instead of copying full inline assistant content into temporary joins.
Snapshot reads retain their full projection; tool keys keep their original part bytes
and revisions. Validated edit tools are then reused for the three metadata projections.

For Claude, `_session()` first reuses an existing corpus parse, then its
single-entry `_one` memo. Otherwise it reads that session's transcripts,
including resumed copies and owned subagent sidecars, through `_parse_one()`.
The one-entry memo serves the burst of detail requests without accumulating
another corpus as the user opens more sessions.

Normal detail reads widen to `_parse()` if a transcript may replay another
session's history, no matching files exist, or the local parse cannot produce
the requested session. Replayed usage must be deduplicated in corpus order;
missing originals can survive as original-session records in another file.

Turn traces are separate lazy reads, **not part of session prefetch**. Bounded
preview memos and selected-turn full expansion never enter rollup caches.
Claude arms `_want_trace` only around a content read, clearing it in `finally`
so the shared parser cannot retain tool output during corpus or subset parsing.
See [claude.py](../src/opentab/stores/claude.py).

## Persistent warm cache

The explicitly built [conversation search index](conversation-search.md) is a
separate sensitive-text store, not part of this warm accounting cache. Normal
startup/reload never creates it. Its refresh uses strong, reader-versioned source
manifests to avoid full conversation reads for unchanged roots, then re-reads and
pre/post-verifies changed roots. Search still verifies candidate snapshots live.
An explicit headless refresh persists a complete accounting rollup after its catalog
reload, without changing the TUI's deferred model scan. A repeated read of the same
source fingerprint reuses the in-process parsed catalog; changed inputs still reload.
Rejected disk scalar payloads cannot clear a fresher in-memory projection.
These manifests and their additive SQLite migration are independent of the weaker
rollup-cache fingerprint and source-parser splicing below.

`sources.make_store()` wraps eligible leaf stores in `CachedStore`; combined
views keep each leaf's cache independent. A changed Claude transcript should
not invalidate an unchanged Codex history. Stores without `cache_inputs()` are
not wrapped. Demo mode and `--no-cache` bypass this wrapper.

Rollups live under `$XDG_CACHE_HOME/opentab/cache`, defaulting to
`~/.cache/opentab/cache`. Filenames combine the source key and a hash of key/root.
These rebuildable files are separate from authored notes and UI preferences.

Each backend's `cache_inputs()` lists its dependencies. The default fingerprint is a
sorted list of `(path, size, mtime_ns)`, not a content hash. Include metadata that
changes accounting or attribution, not just transcripts: login state, project
registries, and SQLite WAL files can matter depending on the backend.

An optional `cache_fingerprint()` supplies backend-specific revisions in the same
three-field row shape. OpenCode fingerprints DB and WAL with size, mtime,
device and inode. This detects same-size WAL reuse, checkpoint changes and database
replacement even with preserved size/mtime. SHM is a derived WAL index with mutable
reader marks, so its timestamp is excluded from accounting invalidation. This does
not change the separate live conversation/patch revision checks. Old timestamp-only
rollups miss once on upgrading to the stronger revision format. Ctime is excluded:
SQLite may reset an existing WAL's ownership on a read-only open, changing ctime
without changing content. Debug events report mtime and file-identity changes
separately, without exposing source paths.

On a fingerprint hit, the wrapper supplies fresh `Workflow` objects and copies
of model rows from disk. It also serves cached `records_cost` state when the
fingerprint matches, avoiding a backend cost probe. Session-detail methods still
delegate to the underlying store; a warm rollup is not a cached Turns tab.

`workflows()` fingerprints on every call, including reload. On a miss it tries
an incremental splice, then falls back to the backend's normal parse. The cache
is written from `model_breakdown()` only after both workflow and model rows are
available for the same fingerprint. An incremental result already includes its
model rows: delegating that call would undo the optimization with a full parse.

Rollup JSON writes use a temporary file and atomic replacement and are best-effort: inability
to write the cache must not prevent browsing. Rows are stored before App's `$`
repricing, so changing price mode does not require a transcript parse.
`CACHE_VERSION` invalidates payloads when their shape or meaning changes.
The implementation is [stores/cached.py](../src/opentab/stores/cached.py).

## Incremental means whole affected sessions

### OpenCode: reuse unchanged message accounting

On a database fingerprint miss, OpenCode scans native message **metadata**, not
every message's JSON. An indexed `.json.usage.sqlite3` sidecar includes source identity, message row IDs,
session IDs, type, sequence and creation/update revisions, together with their
scalar accounting projection. Only new or revised messages need a payload read.
Workflow trees, worked time, model totals and aggregate residuals are recalculated
from that projection, so deletion, reparenting, model switches and aggregate-only
usage are not approximated by adding token deltas. V2 still owns migrated IDs;
legacy-only JSON is reread because older schemas need not record row revisions.

Reuse requires the same source file identity and stable positive update revisions.
Messages updated within two seconds of the previous projection, null revisions
and future revisions are reread. This relies on OpenCode updating `time_updated`
when a message changes; an external rewrite preserving revisions can defeat reuse.
`--no-cache` uses a fresh projection without restoring persistent message rows.
Missing, older or malformed accounting-cache payloads cause a fresh projection.
The first uncached build still reads retained history once; subsequent activity
does not rescan unchanged inline output. No prompt, tool output or trace is cached.
Root-scoped status reads use the same bounded projection for only that subtree.

Warm rollup hits do not open the scalar sidecar. Detail readers load just their
selected executions, then verify revisions against the source. Full refreshes
load all scalar rows and populate the indexed TEMP table in batches. Sidecar
writes are transactional, updating changed rows and removing deleted rows; when
all native rows were reused, the sidecar is not rewritten. Rollup JSON encoding
uses the stdlib C encoder. Existing inline JSON accounting caches remain readable
and migrate on their next cache write; no cache deletion is required. A missing,
malformed or mismatched sidecar falls back to fresh accounting. Neither SQLite
indexes nor writes are applied to harness databases.

### File backends: reparse whole affected sessions

A live agent often changes just one transcript between launches. Incremental
caching keeps unaffected rollups rather than paying for the whole history on
every append. Currently Claude supplies the required backend hooks:

- `cache_provenance()`: session ID to files that produced its rows, after parsing.
- `parse_subset(paths)`: workflows, model rows, and provenance, or `None` to refuse.
- `sort_workflows(rows)`: the same deterministic ordering used by a full parse.

The unit of work is not simply a changed file. A session can span a main
transcript and sidecars, and a file can contribute to several session IDs.
Provenance therefore describes a many-to-many graph:

```text
changed file -> sessions it contributes to -> every file of those sessions
             -> any other sessions in those files -> ... until closure
```

`CachedStore._splice()` follows this component before requesting a subset.
Changing a sidecar also requires the unchanged main transcript; otherwise its
owner loses the main agent's work. The splice replaces all affected workflow,
model, and provenance rows, keeping the unaffected remainder.

Rebuild whole sessions, never add a token delta to an old summary. Titles depend
on record order, and `worked_seconds` depends on bursts across the event stream;
neither is additive. Claude reads subset files in `_files()` order, not the
alphabetical order of the request: cwd takes the first value, while AI/custom
titles take the last. Final rows use `sort_workflows()` with an ID tiebreak so
splicing does not shuffle sessions tied on cost and tokens.

## When a splice must fall back

The optimization must not turn uncertainty into plausible but incomplete usage.
The wrapper or Claude subset reader refuses a splice in these cases:

- Missing usable cache data, nonempty provenance, or either subset/sorting hook.
- An invalid old fingerprint or malformed provenance prevents a reliable closure.
- Any previously fingerprinted file disappeared, or a changed file shrank.
- Fingerprints differ but no changed files can be identified.
- A newly discovered contribution produces an existing cached session outside
  the affected set: the old graph did not include all of that session's files.
- A requested file disappears from the glob, or the read stream skips a requested
  file after listing it, including one that became unreadable.
- A selected Claude transcript is replay-capable, or the backend otherwise
  returns `None` rather than a trustworthy subset.
- Cached workflow fields no longer construct a `Workflow`, or a guarded payload
  shape/conversion error occurs while splicing.

Removal and shrinkage need special treatment because usage deduplication gives
credit to the first claimant. Removing that claimant can transfer credit to a
session *outside* the component. Claude's full parse sorts replay-capable files
last so original sessions claim their API calls first. Parsing a replay alone
would credit it with history it only copied.

Both fast paths trust `_replays_history()` to recognize replay files. Its tail
check looks for a top-level `sessionKind` key and treats unreadable marker lines
conservatively. Unmarked transcripts are assumed independently accountable;
provenance does not prove every possible cross-file dedup relationship.

Cache reading also guards the exact-hit path: `_read()` checks container shapes,
required model-row keys, and string root IDs. A rejected row rejects the payload
rather than silently dropping usage. Missing provenance alone still permits an
exact hit, but disables incremental misses. These are structural checks, not a
complete semantic validation of arbitrary hand-edited JSON.

**Accepted residual:** a rewrite can remove a dedup claimant while leaving the
file the same size or larger. Its changed stamp triggers a splice, but size and
mtime cannot reveal the lost claim outside that slice. Keeping per-session dedup
keys would increase cache size and hit-path work; the implementation instead
assumes normal append-only transcripts. Preserving size *and* mtime can also
produce an exact hit despite edited content. Use `--no-cache` when investigating
rewritten history; the rollup cache is not a content-integrity check.

## Invalidation has two owners

In [tui/app.py](../src/opentab/tui/app.py), reload and harness/demo replacement
clear node, Turns, Tools, Context, and trace memos, release expanded trace content,
and rebuild the model cache. Changes to range or ignored projects/sessions
invalidate derived workflow projections via `_invalidate_workflow_cache()`;
showing ignored items also changes the projection cache key. Neither requires
new source parsing.

The backend has its own parsed-state memos. Claude's `workflows()` clears
`_sessions`, `_one`, and `_trace_one`; a subset parse must clear them too because
a successful splice bypasses `workflows()`. Never install the subset into
`_sessions`: detail readers interpret that map as the complete corpus and would
report every unparsed session as missing. The wrapper likewise clears its fresh
workflow/model/provenance stashes together on a hit, keeping them one answer.

## Cost polling and direct entry

`opentab launch` reads only existing, version-compatible rollup JSON for the
configured local resumable harness roots. It does not check harness availability,
fingerprint records, or update caches by default. `--refresh` explicitly loads
local stores headlessly and completes both workflow and model reads to persist
their normal caches before opening fzf. Missing or stale sessions remain absent
until a refresh; a missing directory or executable at selection time is reported.
Ignored sessions and projects are hidden unless `--no-state` is used. fzf sorts
matches by relevance, breaking ties in the original newest-activity order.
The picker uses aligned tool, compact project, updated-time and session columns;
narrow terminals give priority to the title. Long titles remain fully searchable.
The picker uses its own fzf options instead of `FZF_DEFAULT_OPTS` or
`FZF_DEFAULT_OPTS_FILE`, so global bindings cannot change its selection protocol.

`opentab cost` is a curses-free fast path using raw stores, not persistent
rollups. `--harness` limits which local tool it queries.

Resolution in [cli/main.py](../src/opentab/cli/main.py) uses the interactive backends in
`_STATUS_SOURCES`, excluding request logs, Copilot, VS Code, and pulled summaries.
`auto`/`all` ignore the TUI's saved single-harness preference; an explicit local
harness limits the search. ID-like targets are probed through each `root_of()`:
never infer the owning backend from UUID shape, and never reinterpret an
unclaimed ID as a directory. Subagent IDs resolve to their root where supported.
Paths select the newest matching project root via `recent_roots()`, with project
paths normalized through git-root resolution. With no target, `cost` selects
the newest root across eligible backends, not just the current directory.

`_price_root()` prefers `status_nodes()` over `workflow_nodes()`, sums subtree
nodes, and adds list-price estimates for zero-cost nodes with tokens. A positive
estimated portion prefixes the output with `~`. Claude's status path deliberately
never widens to the corpus: it may overcount replayed history when pricing a
replay transcript alone. Do not promise parity with the browser for that case.

Several targets, or `--batch`, produce ordered `<target>\t<price>` lines from one
process. `_StatusPricer` shares each backend's recent-root list and each resolved
root's price, avoiding repeated imports, discovery, and parsing for split panes.
Lazy root fields retain early-stop head reads rather than eagerly resolving every
project. Use `opentab cost --batch - < targets.txt` for newline-separated input;
`-` must stand alone and stdin must not be a terminal. Unmatched targets are
omitted. Single-target read errors yield an empty successful segment; batch read
errors skip that target and return exit status 1 so partial tables are detectable.

`opentab --goto "$PWD"` uses the same ID/project rules but opens the TUI. Bare
`--goto` defaults to cwd, unlike bare `cost`. Startup prevents a saved harness
selection from hiding the resolved backend, and `App.goto_session()` clears a
narrowed date range when necessary before drilling in. An unresolved target
leaves normal TUI startup in place with a notice, rather than exiting solely
because lookup failed. Detail loading still follows the lazy path above.

Use `opentab --harness all --timings` to inspect `cached`, `incremental`, and
`parsed` results; compare with `--no-cache` on representative data. Corpus size,
active files, filesystem cache, and replay fallbacks determine the benefit.

For SQLite benchmarks, record the Python **and SQLite** versions: query planning
and JSON memory use can differ substantially between runtimes. A repeated launch
is not proof of a warm-cache hit. Closing every database connection can checkpoint
the WAL, changing accounting inputs even without new messages. Reader-only SHM
timestamp churn no longer invalidates OpenCode rollups. To reproduce an idle running
harness, keep a separate connection open on the benchmark copy and verify the
reported cache-hit state.
Distinguish an OpenTab cache miss from a cold OS filesystem cache, and process
anonymous memory from mapped source pages and filesystem cache.
