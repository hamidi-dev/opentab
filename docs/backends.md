# Backend accounting

OpenTab is a ledger of recorded AI work, not a reconstruction of the conversation
currently visible in a harness. A discarded answer still used tokens; a replayed
answer did not use them again. Most backend complexity comes from telling those
two cases apart, then assigning the usage to the right session and model.

For data locations, setup and supported features, see [Data harnesses](sources.md).
This contributor guide focuses on accounting; [Pricing](pricing.md) explains rates
and estimation, and [Caching](caching.md) covers warm starts and incremental parsing.

## The mental model

A backend translates one harness's records into a shared accounting model:

- **Workflows** are root sessions. Their totals include tracked descendants, while
  root-only figures describe the work the main session did itself.
- **Model rows** split a root's usage by model, retaining both subtree and root-only
  shares. They are the basis for model attribution, not a node's dominant-model label.
- **Nodes** describe individual sessions in the subagent tree. Adding already-folded
  workflow totals to child nodes would count delegated work twice.
- **Turns** describe the finest usage boundary the harness records. That is usually
  one model call, but can be a multi-call turn, or a log entry without a saved answer.

The core methods are `workflows`, `summary`, `model_breakdown` and `workflow_nodes`.
Optional methods supply session extras, with per-session `supports_*` checks.
The frontends need not know the record format; see [Architecture](architecture.md).

Recorded dollars and list-price estimates remain separate. Token-only harnesses
return zero cost and keep their tokens in `unpriced_*` fields. Mixed harnesses make
that decision at the smallest reliable billing boundary, before aggregation: one
paid call must not hide the unpriced calls beside it. The `$` view adds an estimate
for unpriced usage to recorded spend; it does not replace recorded spend wholesale.
`records_cost` alone is not evidence that any loaded session actually paid money.

## Token conventions

OpenTab's normalized token categories are additive:

```text
total = input + output + reasoning + cache_read + cache_write
```

- **Input means uncached input.** OpenAI-style records often include cache reads in
  their input count; subtract those before assigning the remainder to `input`.
  Codex can also include cache writes in that same input budget.
- **Anthropic-style input is already uncached.** Claude Code, pi, omp, OpenClaw and
  zaly use separate cache counts. Hermes' database uses this convention too, even
  though its log does not. Subtracting the cache again loses real input tokens.
- **Reasoning is additive here, not universally upstream.** Claude, Codex, Hermes,
  pi/omp and zaly already include it in output, so their normalized `reasoning` is
  zero. Gemini and Antigravity record extra thinking tokens, which belong in that
  column and are priced at the output rate. Copilot folds its reasoning into output.
- **The one-hour cache-write count is a subset**, not a sixth category. Claude's
  `cache_write_1h` retains the long-TTL portion of `cache_write` for pricing without
  increasing the token total. Most harnesses do not preserve that TTL distinction.

A Tools row means "usage in model calls that invoked this tool", not the tool's
output size. A call's usage is split evenly among its tool calls, including repeated
names. Context curves need a per-request prompt size; cumulative turn deltas cannot
honestly substitute for one. Content-based composition is an estimate, not a token
ledger, and cannot recover system prompts or tool schemas absent from the records.

Timeline metadata remains separate from token arithmetic. Optional `tools` lists
preserve call order and repeated names; `depth` and `agent` identify delegated
steps. `effort` records reasoning configuration, not reasoning token counts:
OpenCode uses `variant`, Claude assistant records carry the setting, Codex gets it
from `turn_context`, and omp/zaly update a running value from settings events.
An absent value stays unknown rather than inheriting an invented default.

Lazy content reads still need record ownership checks. A tool-call id may be reused;
even a call outside the selected turn must invalidate an older binding with that
id. Otherwise a later result can appear under the wrong call. `TraceContent` applies
selection and preview limits without bypassing this bookkeeping.

## OpenCode

Reader: [`stores/opencode.py`](../src/opentab/stores/opencode.py)

OpenCode supplies recorded cost and normalized token categories. Subscription calls
can still record zero dollars. Sessions link through `parent_id`; recursive queries
fold descendants into root workflows and expose their own usage as nodes.

- Schema versions differ. The reader probes session columns and uses `_cost_expr`
  and `_token_exprs`, falling back to assistant-message JSON aggregates when session
  summary columns are missing. A query that directly assumes `session.cost` exists
  bypasses that adaptation.
- Per-model usage comes from assistant messages. Tool names and trace content live
  separately in `part`; grouped scans join them by message id. A per-row correlated
  lookup is particularly costly on the corpus-wide timeline export.
- The database is read-only; message/part table availability gates session extras.

## Claude Code

Reader: [`stores/claude.py`](../src/opentab/stores/claude.py)

Claude records tokens, not per-message dollars. Sidechain messages become depth-one
Task nodes grouped by their parent prompt. Input and cache counts are separate;
`usage.cache_creation` additionally exposes the one-hour cache-write subset.

**Deduplication is about ownership as well as totals.** Usage is claimed once by
`(message.id, requestId)`, across files. Background sessions can replay a parent's
history under a different session id, so originals must be parsed before replays.
Otherwise the corpus total looks right while the wrong session owns the calls.

- Replay detection looks for the presence of a **top-level `sessionKind` key** in
  tail records, not its truthiness. A nested key is not a marker. A substring is only
  the fast gate; strictly decoded, parsed records can disprove that candidate.
- A marker-bearing line that straddles the read window or cannot be decoded or
  parsed is **unknown**, not proof of absence. The reader widens its tail read and,
  at the parse budget or end of file, conservatively treats that unreadable marker
  as a replay. The separate search for a complete oversized tail record stays uncapped.
- Ordinary detail reads parse only the session's transcripts and sidecars. Replay
  detail requires corpus ordering. The `opentab cost` fast path deliberately avoids
  that full scan and can over-report a replay session read in isolation.

Streamed content blocks repeat a message's full usage. Tokens count once, but later
blocks still contribute tool names and content. Context composition walks records
deduplicated by record UUID, rather than discarding every repeated message id.
The parser also recovers bounded cases of literal control characters and split JSON
strings; this is not a general repair mechanism for corrupt transcripts.

Claude Code normally deletes transcripts after 30 days. OpenTab warns but does not
preserve vanished cache rows as history or change the harness's retention settings.
Readable thinking is also unavailable in the observed transcript format: empty
thinking blocks and signatures are not reasoning prose.

## Codex CLI

Reader: [`stores/codex.py`](../src/opentab/stores/codex.py)

Codex records cumulative usage, often echoed after the next `turn_context`. The
reader follows the total monotonically within each segment: growth contributes a
delta, equality contributes nothing, and shrinkage starts a fresh compaction segment.
Each accepted delta belongs to the model then in force. Totals add across resets,
so the last raw counter alone is not necessarily the whole session's usage.

- Invalid cumulative components are skipped **without changing the baseline**.
  Coercing a corrupt counter to zero would manufacture a reset and bill the next
  valid record's entire running total again. Skips and echoes consume no pending tools.
- Input includes cache reads and, when supplied, `cache_write_input_tokens`.
  Both are split out of the delta and capped to its input budget. Reasoning stays
  inside output. A write key missing during ordinary growth retains its prior
  baseline; a real total reset does not.
- Mixing older keyless records into a file with write counters can hide writes.
  If the returning write delta exceeds that turn's input, the excess cannot be
  reassigned retroactively and remains accounted as uncached input, understating
  write pricing rather than inventing negative input.
- Spawned threads link through `session_meta.source.subagent.thread_spawn` and its
  `parent_thread_id`. Orphaned threads remain roots; a subagent marker without a
  parent id is not enough to infer a relationship.

Discovery includes the sibling `archived_sessions` directory, even when the live
tree is empty. Archive/live copies deduplicate by **rollout filename**, live first;
a resumed rollout with a different filename stays. Only JSONL is read: compressed
`.jsonl.zst` rollouts are not supported. Archiving itself is a move, not expiry.

Tools and readable trace items accumulate until an accepted usage delta. Such a
delta can span multiple requests, so Codex deliberately offers no Context curve.
Encrypted reasoning remains opaque; only recorded readable summaries can be shown.

## Hermes Agent

Reader: [`stores/hermes.py`](../src/opentab/stores/hermes.py)

Hermes has two accounting surfaces: session totals in SQLite and individual calls
in rotating logs. The database normalizes providers to uncached input plus separate
cache reads/writes; reasoning is already in output. Positive `actual_cost_usd` wins,
then positive `estimated_cost_usd`, else zero. The current reader selects by these
values, not by `billing_mode`. Archived sessions are excluded; parent links are
resolved with cycle-safe walks shared by the browser and cost lookup.

**Auxiliary calls require reconciliation.** `session_model_usage` separates the
main loop (`task=''`) from titling, approval, vision, compression and other tasks.
The reader uses these per-model buckets only when the main buckets match all four
session token counts exactly, and match cost within tolerance when session cost
columns exist. Then it adds the auxiliary buckets. Otherwise it keeps the summary:
missing auxiliary usage is preferable to counting the main loop twice.

**Turns use a causal join, not row positions.** The log's `in` includes cache reads,
unlike the database field. Call numbers reset on resume, so a repeated ordinal is
not a duplicate. Logs can record a call before Hermes rejects its answer, leaving
no corresponding assistant message in SQLite.

- Millisecond timestamps merge the call and message streams. A newer call replaces
  an unmatched pending call; an intervening non-assistant event cancels the match.
  Unmatched calls keep their usage but get no borrowed tools or trace content.
- Tool-call lists are validated as a whole. Dropping only a malformed member would
  give the surviving tools too large a share of the call's tokens.
- Logs contain neither per-call dollars nor cache writes. Turns therefore retain
  zero recorded cost even in a metered session, and cannot reproduce every database
  breakdown. Resumed log totals can exceed Hermes' own accumulated session totals.
- Log rotation removes Turns/Tools availability for old sessions while database
  rollups survive. A retained summary is not evidence that its detailed calls survive.

## GitHub Copilot CLI

Reader: [`stores/copilot.py`](../src/opentab/stores/copilot.py)

The opt-in OpenTelemetry export is the usage ledger; the CLI's session database
only enriches titles and projects. Export records carry tokens but no dollars.
Input includes cache reads, cache creation is separate, and reasoning folds into
output. Total-only records need back-filling rather than being discarded.

One call can appear across several files as a chat span, inference log, agent-turn
log and agent-summary span, in that fidelity order. Deduplication spans the entire
export, and response ids take precedence over trace ids: several distinct calls
can share a trace. A higher-fidelity record with **no response id** still covers its
trace conservatively, which can undercount named calls that cannot be disambiguated.

The parser also shares model/session context across a trace, preferring an actual
conversation id over a per-response fallback. Turns are headerless, and there is no
subagent tree, per-step Tools view or content trace from this reader.

## Copilot Chat in VS Code

Reader: [`stores/vscode.py`](../src/opentab/stores/vscode.py)

The reader replays the current JSONL journal's snapshot/set/append operations and
also reads legacy JSON sessions. Journal copies take precedence; `(sessionId,
requestId)` deduplicates requests across the two representations.

Output uses the larger of accumulated `completionTokens` and metadata output;
input similarly takes the larger prompt count. Output can cover several tool
rounds, but recorded input covers only the final round, so agentic input remains
undercounted. No cache split is recorded. `resolvedModel` wins over the requested
model, which matters for auto-routing; Copilot credits are quota units, not USD.

Workspace URIs supply projects; empty-window sessions have no workspace. Tokenless
files are ignored for both availability and accounting. The reader has no subagent
tree, Tools view or content trace.

## pi-agent

Reader: [`stores/pi.py`](../src/opentab/stores/pi.py)

pi's assistant `usage` uses uncached input and separate cache reads/writes. Repeated
assistant ids deduplicate resumed files; a `totalTokens` remainder is assigned to
output. There is no subagent tree in this reader.

`usage.cost.total` is list-priced even on plan routes. OpenTab counts positive cost
only on metered routes; OAuth providers from `auth.json` and known subscription
markers leave their tokens unpriced. This uses current authentication metadata,
not a historical billing snapshot, so changing login type can reclassify old usage.
Missing auth metadata leaves only the marker heuristic.

Tools come from `toolCall` blocks; later `toolResult` records join by `toolCallId`.

## omp (Oh My Pi)

Reader: [`stores/omp.py`](../src/opentab/stores/omp.py), extending pi's parser

omp shares pi's token and mixed-cost rules, but reads authentication type from
SQLite. It selects only provider and credential type, never the credential payload.
Provider and model are recorded separately and joined for attribution.

The tree comes from **paths**, not UUID-shaped directory names: the parent of
`.../Parent/Child.jsonl` is `.../Parent.jsonl`, at every depth. Agent-named child
files get their id from the session record. All paths of a resumed session are
retained, since keeping only the latest would orphan children of earlier rollouts.

Missing parents leave roots. Usage-less intermediate sessions are spliced out,
with descendants attached to the nearest surviving ancestor; cost lookup protects
its requested root even if that root only delegated. Tree folding preserves both
the metered/unpriced split and the root's own share. `reasoningTokens` is already
part of output and must not be added again.

## OpenClaw

Reader: [`stores/openclaw.py`](../src/opentab/stores/openclaw.py)

Only canonical assistant `type:"message"` records with usage contribute tokens.
The separate trace format is not a second ledger. Input/cache accounting and the
list-price-versus-metered split resemble pi; OAuth profiles come from
`openclaw.json`, with marker fallbacks for plan and internal routes.

Live files and `.jsonl.reset.*` / `.jsonl.deleted.*` archives merge by session id,
with first-claimer record-id deduplication; lock files do not count as transcripts.
Models may come from preceding model-change/snapshot records. Projects represent
agents, not a user's working directory, and this reader does not build a subagent tree.

Trace results belong to the newest still-open matching `toolCallId`, which can be
reused later. Auth classification has the same historical limits as pi's.

## zaly

Reader: [`stores/zaly.py`](../src/opentab/stores/zaly.py)

A session is an append-only conversation DAG. Resume/fork stays in that file;
assistant message ids deduplicate usage, while abandoned regenerated branches count
because their calls happened. Usage lives in `message.meta.usage`, with a qualified
model in metadata. Input is already uncached and reasoning is included in output.

Cost is a **sum of component values**, not a `.total` field. The metered/plan split
uses current auth types and markers, as in pi. Auth lives in an independent **state**
directory; a session-directory override does not relocate it.

Settings supply workspace and canonical session id, which can differ from the
directory id. Lazy trace reads therefore use the retained path. Subagent transcripts
live in temporary storage and their usage is not folded into the parent: the reader
cannot report that delegated spend. Context composition is a chars/4 estimate.

## Gemini CLI

Reader: [`stores/gemini.py`](../src/opentab/stores/gemini.py)

Both legacy JSON chats and append-only JSONL recordings feed one parser. Prompt
tokens are **cache-inclusive by default, even without a total**. Only a recorded
total that closes with cache added separately selects the exclusive interpretation.
Thinking is additive reasoning; tool-use prompt tokens are additive input. No
cache-write count or dollar cost is recorded.

**Message ids are update keys.** Re-appending replaces the prior turn, backing its
usage out of the prior model before adding the replacement. A replacement with no
usage removes the old usage too. Repeated user ids update prompts. `$set.messages`
checkpoints merge by id rather than clearing history, and `$rewindTo` is ignored:
neither operation refunds the calls missing from Gemini's resumed conversation.

Children live under `chats/<parent id>/`, recursively, and use their filename as
their own id. The reader keeps usage-less ancestors that delegated spending work,
promotes children of missing parents and breaks malformed cycles. Browser and cost
lookup both walk to a surviving root instead of pricing a deleted parent's id.
Subagent summaries can hold results rather than titles, so children use their task
prompt. Injected context wrappers are not user prompts.

Projects resolve from `.project_root`, then the project registry, then a recorded
hash matched against registry paths. `.unreadable-*` backup files are excluded to
avoid counting a replaced recording twice.

### Retention is policy evaluation

Gemini normally deletes 30-day-old recordings and their subagent directories on
launch. `gemini_retention()` mirrors the settings pipeline rather than reading one
boolean. Precedence is **defaults < system-defaults < user < workspace < system**.
Workspace checks cover registered projects when the machine-wide result would
otherwise be safe; system overrides are reapplied above each workspace.

- JSONC comments are accepted. Environment-dependent values that cannot be evaluated
  confidently warn; unreadable machine-wide settings are unverifiable, not defaults.
- Coercion belongs to each settings layer. A validation failure can retain raw
  values, so JavaScript number/truthiness rules matter: the string `"false"` is
  truthy if boolean coercion was lost. Non-object policy layers replace, not merge.
- Definite configurations that Gemini rejects disable cleanup and need no warning.
  Ambiguous live policies warn instead of claiming safety; a finite active count
  cap is not a long-history guarantee. See [retention setup](sources.md#gemini-cli).
- This is not Gemini's whole settings validator: unrelated invalid settings can
  affect coercion invisibly. Folder trust is not evaluated, unreadable workspace
  files are skipped, and unregistered projects are outside the workspace check.

Claude and Gemini warnings are queued in both frontends so neither hides the other.
Web reports show them per page; TUI dismissals can persist; `doctor` keeps reporting
unsafe retention. These checks neither repair settings nor back up deleted usage.

## Antigravity

Reader: [`stores/antigravity.py`](../src/opentab/stores/antigravity.py)

Each conversation is a read-only SQLite database containing protobuf blobs. Field
numbers are reverse-engineered, so a candidate usage message must prove itself:
response id at field 11, **field 3 = field 9 + field 10**, and at least one of fields
9/10 actually present. An accidental `0 = 0 + 0` is not validation. The wire reader
bounds-checks values and stops at unreadable data rather than taking down the harness.

- A bounded blob-tree search covers both generation and step metadata, including
  auxiliary calls absent from the generation table. Repeated response ids keep the
  richer token record and whichever copy supplies a timestamp.
- Input is fixed system-prompt tokens plus newly processed tokens; cache reads are
  separate and thinking is additive. No dollars or cache writes are recorded.
- Model attribution is per response. An unnamed auxiliary call inherits a model
  only when the conversation has exactly one; otherwise it stays unknown.
- Tool steps (type 132) belong to the last **generation** that asked for them, not
  an intervening auxiliary call. Subagent steps (type 101) name child databases.
  Those fields are meaningful only on their own step types. Parent claims use the
  same first-claimer, cycle-refusing resolver for full parsing and cost lookup.

Missing generation timestamps remain unknown, not the session's start time.
Activity considers the database WAL too. Older desktop `.pb` conversations are
unsupported; unreadable blobs and unfamiliar schemas can leave usage unreported.

## CSV and JSONL request logs

Readers: [`stores/csv_source.py`](../src/opentab/stores/csv_source.py) and
[`stores/jsonl_source.py`](../src/opentab/stores/jsonl_source.py)

JSONL subclasses the CSV accounting pipeline. Input includes cached reads; output
already includes reasoning. A **positive** cost is recorded spend (`credits` convert
at $0.01 each). Missing, zero or negative cost leaves usage unpriced, even when a
cost column exists. Positive cost-only requests survive without token counts.

Stable request ids deduplicate within a session; repeated ids are not update keys.
Without a session id, requests form synthetic date/project buckets. The reader
tracks that fact explicitly rather than guessing from an id prefix, and disables
Context curves for those buckets of potentially unrelated conversations. Neither
format expresses a subagent tree. See the [request schema](sources.md#schema) before
changing aliases or producer expectations.

## Merging and checking a change

[`CombinedStore`](../src/opentab/stores/combined.py) concatenates backend rollups and
routes detail to the workflow's owner. It assumes session ids are globally unique;
it is not cross-harness request deduplication. A request imported as CSV and also
read from its native harness can therefore count twice. Portable machine summaries
use a [separate reader](../src/opentab/stores/remote.py); see [Machines](machines.md).

Compare token categories, model ownership and subtree totals, not just one grand
total. Exercise repeats, model changes, resumes, missing parents and mixed billing.
Check the browser against `opentab cost`, then cached against uncached reads.
Tests live in `tests/test_stores_<backend>.py`, shared builders in `tests/_support.py`.
