# Data harnesses

OpenTab reads the records each AI coding tool already keeps on your machine, read-only.
This page lists every supported harness: where its records live, what to set up, and
how its cost is counted.

Investigating a harness's numbers or adding one? See
[Backend accounting](backends.md).

## Picking a harness

By default OpenTab restores the harness you used last. With no saved choice and more
than one harness present, it **merges all of them**. Press `H` anywhere to switch.

| Harness | `--harness` | Reads by default | Override |
|---------|-------------|------------------|----------|
| [OpenCode](#opencode) | `opencode` | `~/.local/share/opencode/opencode.db` | `--db` |
| [Claude Code](#claude-code) | `claude` | `~/.claude/projects` | `--claude-dir` |
| [Codex CLI](#codex-cli) | `codex` | `~/.codex/sessions` | `--codex-dir` |
| [Hermes Agent](#hermes-agent) | `hermes` | `~/.hermes/state.db` | `--hermes-db` |
| [GitHub Copilot CLI](#github-copilot-cli) | `copilot` | `~/.copilot/otel` | `--copilot-dir` |
| [Copilot Chat in VS Code](#copilot-chat-in-vs-code) | `vscode` | every installed VS Code variant | `--vscode-dir` |
| [pi-agent](#pi-agent) | `pi` | `~/.pi/agent/sessions` | `--pi-dir`, `$PI_AGENT_DIR` |
| [omp](#omp) | `omp` | `~/.omp/agent/sessions` | `--omp-dir`, `$OMP_AGENT_DIR` |
| [OpenClaw](#openclaw) | `openclaw` | `~/.openclaw` | `--openclaw-dir`, `$OPENCLAW_DIR` |
| [zaly](#zaly) | `zaly` | `~/.local/share/zaly` | `--zaly-dir`, `$ZALY_DATA`, `$ZALY_ROOT` |
| [Gemini CLI](#gemini-cli) | `gemini` | `~/.gemini` | `--gemini-dir`, `$GEMINI_CLI_HOME` |
| [Antigravity](#antigravity) | `antigravity` | `~/.gemini` | `--antigravity-dir`, `$GEMINI_CLI_HOME` |
| [CSV / JSONL logs](#csv--jsonl-request-logs) | — | `~/.local/share/opentab/requests.csv` / `.jsonl` | `--csv`, `--jsonl` |
| All of the above, merged | `all` | | |

```sh
opentab --harness claude --claude-dir /path   # one harness, non-default location
opentab path/to/opencode.db                   # or pass a file; the extension picks the harness
opentab requests.csv
opentab --harness all                         # every present harness, merged
```

`opentab --help` shows everyday options. `--help-all` (for example
`opentab sessions list --help-all`) lists every source-path option. `--source` still
works as an old alias for `--harness`.

## What each harness supports

Every harness feeds the same views: months, days, projects, sessions, models and
trends. On top of that:

| Harness | Cost | Subagent tree | Turns | Trace | Tools | Context |
|--------|------|:---:|:---:|:---:|:---:|:---:|
| OpenCode | recorded | ✓ | ✓ | ✓ | ✓ | ✓ |
| Claude Code | estimated | ✓ | ✓ | ✓ | ✓ | ✓ |
| Codex CLI | estimated | ✓ | ✓ | ✓ | ✓ | — ¹ |
| Hermes Agent | mixed | ✓ | ✓ ² | ✓ ² | ✓ ² | ✓ ² |
| GitHub Copilot CLI | mixed | ✓ ³ | ✓ | ✓ ³ | ✓ ³ | ✓ |
| Copilot Chat in VS Code | estimated | — | ✓ | — | — | ✓ |
| pi-agent | mixed | — | ✓ | ✓ | ✓ | ✓ |
| omp | mixed | ✓ | ✓ | ✓ | ✓ | ✓ |
| OpenClaw | mixed | — | ✓ | ✓ | ✓ | ✓ |
| zaly | mixed | — | ✓ | ✓ | ✓ | ✓ |
| Gemini CLI | estimated | ✓ | ✓ | ✓ | ✓ | ✓ |
| Antigravity | estimated | ✓ | ✓ | — | ✓ | ✓ |
| CSV / JSONL request logs | per row | — | ✓ | — | ✓ ⁴ | ✓ ⁵ |

¹ Codex logs a running total, not each request's prompt size, so no honest curve
exists. ² Only for sessions still covered by Hermes' rotating agent log.
³ When matching saved session events are kept; otherwise Turns have no prompt
headers. ⁴ With a `tool` column. ⁵ Only with a real `session_id` column.

| Column | Meaning |
|--------|---------|
| **Cost** | `recorded`: the tool stores real spend. `estimated`: tokens only, priced by the `$` view. `mixed`: metered routes are recorded, subscription routes estimated |
| **Subagent tree** | Each subagent's cost, nested under the session that delegated |
| **Turns** | The per-turn cost timeline inside a session |
| **Trace** | One turn's narration, readable reasoning and exact tool calls and results; TUI only |
| **Tools** | Tokens attributed to each tool call and MCP server |
| **Context** | How the context window filled up; Claude Code and zaly also show what filled it |

Some features depend on the harness:

| Feature | Harnesses |
|---------|-----------|
| Turn traces | OpenCode, Claude Code, Codex, Hermes, Copilot, pi, omp, OpenClaw, zaly, Gemini |
| Conversation reads, `Ctrl-F` search and `y` copy | OpenCode, Claude Code, Codex, Hermes, Copilot, pi, omp |
| [Changes](changes.md) | OpenCode, Copilot |

Traces load only when you open them and never go into web reports or remote
summaries. Every harness also tracks when a session was last active, including its
subagents, which feeds the **Last Activity** sort ([Scope & filter](filters.md)).

### Token-only harnesses

Claude Code, Codex, Gemini, Antigravity and Copilot Chat in VS Code record tokens but
no price. Their sessions show **$0** recorded. The `$` view, on by default for these
harnesses, prices the tokens at API list rates and the header reads `ESTIMATED`. Press
`$` for the recorded numbers; OpenTab remembers your choice.

Their projects roll up to the **git root**, so sessions started in subfolders group
under the repository. See [Pricing & the `$` view](pricing.md).

## [OpenCode](https://opencode.ai)

*SQLite database · records real cost*

- **Reads** `~/.local/share/opencode/opencode.db`, read-only. Use `--db` or pass the
  file path. Both v1 and v2 databases work, including upgraded ones.
- **Cost**: OpenCode stores real per-message cost. Subscription sessions record `$0`
  and get the `$` estimate.
- **Good to know**: a session migrated to v2 counts once. Resuming a migrated session
  in v1 is not supported; later writes to its old copy are ignored.

## [Claude Code](https://claude.com/claude-code)

*JSONL transcripts · tokens only, `$` estimates*

- **Reads** `~/.claude/projects/**/*.jsonl` (`--claude-dir`).
- **Retention warning**: Claude Code deletes transcripts after **30 days** by default,
  and OpenTab cannot recover usage once a file is gone. For long history set
  `"cleanupPeriodDays": 3650` in `$CLAUDE_CONFIG_DIR/settings.json` (default
  `~/.claude/settings.json`). The TUI, web reports and `opentab doctor` warn while
  retention is short.
- **Cost**: tokens only; estimated under `$`.
- **Good to know**: subagent (Task) work appears as a cost tree. Resumed and forked
  sessions count once. The title is Claude Code's own title, else your first prompt.

## [Codex CLI](https://developers.openai.com/codex)

*Rollout JSONL · tokens only, `$` estimates*

- **Reads** `~/.codex/sessions/**/rollout-*.jsonl` (`--codex-dir`) and the sibling
  `~/.codex/archived_sessions/`, so archived threads keep their spend, even when every
  thread is archived. Codex itself deletes nothing.
- **Cost**: tokens only; estimated under `$`.
- **Good to know**: turns add up exactly to the session total, across context
  compactions. Threads from Codex's multi-agent mode appear as subagents, named by
  their nickname. Traces show assistant text, readable reasoning summaries and exact
  tool arguments and outputs; encrypted reasoning stays hidden.

## [Hermes Agent](https://hermes-agent.nousresearch.com/)

*SQLite database · mixed: metered real, subscription estimated*

- **Reads** `~/.hermes/state.db`, read-only (`--hermes-db`), plus the rotating
  `~/.hermes/logs/agent.log*` files for per-call usage.
- **Cost**: metered routes carry Hermes' recorded cost; subscription routes record
  `$0` and are estimated under `$`.
- **Good to know**:
  - Turns, Trace, Tools and Context come from the agent log, so they exist only for
    sessions the rotating log still covers. A resumed session's turns can add up to
    more than Hermes' own total.
  - Hermes 0.20.6+ records extra calls (titling, approval, vision, compression, web
    extraction) separately; OpenTab adds them to the session under the model that ran
    them.
  - A node that mixed models is priced under its main model, so its `$` can be
    slightly off; per-model and per-session totals are exact.
  - MCP tools group under one `mcp` entry instead of one per server.
  - Conversation text is read from the database (user and assistant only). Archived
    sessions are excluded.

## [GitHub Copilot CLI](https://github.com/github/copilot-cli)

*OpenTelemetry export · opt-in · AI-credit charges recorded, the rest estimated*

- **Reads** `~/.copilot/otel/**/*.jsonl` (`--copilot-dir`) and the file in
  `$COPILOT_OTEL_FILE_EXPORTER_PATH`. Titles and projects come from the sibling
  `session-store.db`; session details from `session-state/<id>/events.jsonl`.
- **Set up**: turn on the OpenTelemetry export before you start or resume a session.
  Sessions from then on appear.

  ```sh
  export COPILOT_OTEL_FILE_EXPORTER_PATH=~/.copilot/otel/usage.jsonl
  ```

- **Cost**: GitHub's AI-credit charge on each call (1 credit = $0.01) counts as real
  spend, including credits your plan covers. Calls without a charge are `$0` and
  estimated under `$`. Old premium-request multipliers are not treated as dollars.

### Copilot clients

The same runtime serves the CLI, the GitHub Copilot desktop app, the JetBrains plugin
and VS Code's *Copilot CLI* sessions. The session overview shows which one ran as
**Client**. A client appears only once it writes the export:

| Client | How to turn on the export |
|--------|---------------------------|
| CLI, desktop app | Set `COPILOT_OTEL_FILE_EXPORTER_PATH` as above |
| JetBrains | *Settings → Tools → GitHub Copilot → Chat → Open Telemetry*: enable it, set **Output file**, restart the IDE. A path your shell exports takes precedence |
| VS Code *Copilot CLI* sessions | Set `"chat.agentHost.otel.enabled": true` and `"chat.agentHost.otel.exporterType": "file"`, then quit and reopen VS Code. These are separate from the Copilot extension's `github.copilot.chat.otel.*` |

### Good to know

- Without matching saved events, Turns have no prompt headers; the Context curve
  still works. Event logs alone don't add sessions.
- Subagents with a recorded identity get their own nodes, Turns and traces.
- Sessions the desktop app starts with its `create_session` tool fold into the session
  that created them, which then includes their cost.
- A session the client created and deleted again (such as the desktop app naming a
  chat) is titled "(background call)" under the project "(Copilot background calls)".
  Its charge is real; its parent is unknown.
- A first request can be almost all cache writes; those are part of input, not extra.
- `opentab cost --harness copilot SESSION_ID` and
  `opentab --harness copilot --goto SESSION_ID --tab turns` accept root sessions; a
  child ID resolves to its root, and a directory picks its latest usage.
- [Changes](changes.md) lists successful file edits, creates, moves and deletions with
  their recorded diffs.

## [Copilot Chat in VS Code](https://code.visualstudio.com/docs/copilot/chat/copilot-chat)

*VS Code's chat-session store · nothing to enable · tokens only*

- **Reads** VS Code's own store (`<User>/workspaceStorage/*/chatSessions` plus
  empty-window sessions) for Code, Code&nbsp;-&nbsp;Insiders and VSCodium. Point
  `--vscode-dir` at one User directory for a portable or remote copy.
- **Set up**: nothing on Linux, macOS or Windows. From WSL, the Windows-side store is
  not scanned by default because reading `/mnt/c` slows every start; see
  [Windows & WSL](windows.md#reading-the-windows-side-vs-code-store).
- **Cost**: VS Code records no charge; estimated under `$`.
- **Good to know**:
  - Chats run as *Copilot CLI* sessions appear under
    [GitHub Copilot CLI](#github-copilot-cli) instead.
  - VS Code records only a turn's last tool round as input, so long agentic turns
    under-count input.
  - Projects come from the workspace folder, rolled up to its git root; empty-window
    chats group under "(no workspace)".
  - Chats with no tokens are ignored, so installing VS Code alone adds nothing.

## [pi-agent](https://pi.dev)

*Session JSONL · mixed: metered real, subscription estimated*

- **Reads** `~/.pi/agent/sessions/**/*.jsonl` (`--pi-dir`, honors `$PI_AGENT_DIR`).
- **Cost**: pi writes a list price for every route, so only **metered** routes
  (OpenRouter, a direct API key) count as real spend. Login and subscription routes
  stay `$0` and are estimated under `$`. OpenTab reads which is which from pi's
  `auth.json`, read-only.
- **Good to know**: traces show assistant text and thinking, exact tool arguments and
  their results.

## [omp](https://omp.sh)

*Session JSONL · a pi-agent fork · mixed: metered real, subscription estimated*

- **Reads** `~/.omp/agent/sessions/**/*.jsonl` (`--omp-dir`). `$OMP_AGENT_DIR` is
  OpenTab's own override; omp has no such variable.
- **Cost**: like pi, only **metered** routes count as real spend. OpenTab reads the
  route type from omp's `agent.db`, read-only: only the provider and credential-type
  columns, never stored login tokens.
- **Good to know**: unlike pi, subagents started with the `task` tool appear as a cost
  tree, nested at their real depth. Models show as `provider/model`. The title is
  omp's own, else your first prompt.

## [OpenClaw](https://github.com/openclaw/openclaw)

*Gateway session JSONL · mixed: metered real, plan routes estimated*

- **Reads** `~/.openclaw/agents/<agent>/sessions/*.jsonl` (`--openclaw-dir`, honors
  `$OPENCLAW_DIR`). If OpenClaw runs on a server, point it at a mounted copy.
- **Cost**: only metered routes (a direct Anthropic or OpenRouter key) count as real
  spend; plan routes (openai-codex, github-copilot) are estimated under `$`. OpenTab
  reads which is which from `openclaw.json`, read-only.
- **Good to know**: one project per agent. Archived sessions are included and count
  once. Traces show assistant text and thinking with exact tool arguments and results.

## [zaly](https://github.com/folke/zaly)

*Session JSONL · mixed: metered real, plan routes estimated*

- **Reads** `~/.local/share/zaly/sessions/*/*/session.jsonl` (`--zaly-dir`, honors
  `$ZALY_DATA` and `$ZALY_ROOT`).
- **Cost**: only metered routes (a direct API key) count as real spend. Plan logins
  (ChatGPT-plan `openai-codex`, Claude Pro/Max) and local models stay `$0` and are
  estimated under `$`. OpenTab reads which is which from zaly's `auth.json`, read-only.
- **Missing**: zaly doesn't keep subagent transcripts, so their usage can't be shown.
- **Good to know**: projects fold to the git root. Resumes and forks count once;
  abandoned regenerations do count, since each was a real call.

## [Gemini CLI](https://github.com/google-gemini/gemini-cli)

*Chat recordings · tokens only*

- **Reads** `~/.gemini/tmp/*/chats/**/*.json[l]` (`--gemini-dir`). `$GEMINI_CLI_HOME`
  replaces your home directory, so the store becomes `$GEMINI_CLI_HOME/.gemini`.
- **Retention warning**: Gemini CLI deletes chats **older than 30 days by default**,
  with their subagent transcripts, on every launch. OpenTab cannot recover usage once a
  file is gone. For long history set
  `"general": {"sessionRetention": {"enabled": false}}` in `~/.gemini/settings.json`.
  The TUI, web reports and `opentab doctor` warn while it is on.
- **Cost**: tokens only; estimated under `$`.

### Good to know

- Settings precedence is defaults < system-defaults < user < workspace < system. A
  project's `<project>/.gemini/settings.json` overrides yours, so OpenTab checks
  registered projects too.
- Thinking tokens count as reasoning, priced at the output rate.
- Subagents, including agents started by agents, nest under the session that started
  them, so `opentab cost` prices the whole tree. A subagent is titled with the task it
  was given.
- A turn you rewound still counts: the call was made and billed.
- Projects resolve to their real folder and fold to the git root. Sessions OpenTab
  cannot place group under `(unknown)`.
- Gemini's injected session context is skipped, so your first real prompt titles the
  session.

## [Antigravity](https://antigravity.google/)

*Conversation databases · tokens only*

- **Reads** `~/.gemini/antigravity/conversations/*.db` and the `antigravity-cli`
  spelling (`--antigravity-dir`, honors `$GEMINI_CLI_HOME`), read-only.
- **Cost**: tokens only; estimated under `$`.
- **Missing**: the desktop app's older `.pb` conversation files are not read.
- **Good to know**:
  - Small extra model calls (routing or availability checks) are counted too, so the
    total matches what was billed.
  - Thinking counts as reasoning, priced at the output rate.
  - A delegated agent nests under the conversation that started it, under the name it
    was given.
  - Projects come from the workspace, folded to the git root; the title is your first
    message.

## CSV / JSONL request logs

*Bring your own ledger · mixed per row*

Log your own gateway or proxy traffic, one API request per row (CSV, `--csv`) or line
(NDJSON, `--jsonl`), and browse it like any other harness. OpenTab finds
`~/.local/share/opentab/requests.csv` or `requests.jsonl` on its own, and still finds
one left in the old `~/.config/opentab/`.

- **Cost**: only a positive cost marks a row as real spend. Missing, zero or negative
  cost leaves its tokens estimated under `$`.
- **Good to know**: each request is one turn, grouped under its `prompt`. A stable
  `request_id` prevents double counting when a file is rewritten or appended. Without
  `session_id`, requests group into one session per date and project. Malformed rows
  are skipped.

### Schema

Column names (CSV) and keys (JSONL) match case-insensitively. A request needs a
timestamp, a model and token counts; cost-only rows may leave tokens out.

| Field | Accepted names | Notes |
|-------|----------------|-------|
| timestamp | `timestamp` `time` `ts` `date` `created_at` `datetime` | ISO-8601 or epoch (s/ms/µs). **Required** |
| model | `model` `model_id` `model_name` | e.g. `gpt-4o`, `claude-sonnet-4`. **Required** |
| input | `input_tokens` `input` `prompt_tokens` | As logged; may include cache reads and writes. Default 0 |
| output | `output_tokens` `output` `completion_tokens` | Includes reasoning. Default 0 |
| cached | `cached_tokens` `cached` `cache_read` `cache_read_tokens` | Cached part of input. Default 0 |
| cache write | `cache_write_tokens` `cache_write_input_tokens` `cache_write` | Cache-written part of input. Default 0 |
| session | `session_id` `session` `conversation_id` `conversation` | Groups requests into a session |
| request | `request_id` `id` `req_id` | Stable per request; prevents double counting |
| prompt | `prompt` `prompt_text` `user_prompt` | The user message; groups Turns |
| prompt_id | `prompt_id` | Stable prompt ID (optional) |
| tool | `tool` `tool_name` `tools` | Tool calls made: `Bash;Read`, or a JSON list in JSONL. Feeds the Tools tab |
| project | `project` `repo` `repository` `workspace` `directory` `dir` `cwd` `folder` | A path folds to its git root; a bare name is used as-is |
| title | `title` `name` `label` | Session label. Default: the first prompt |
| cost | `cost_usd` `cost` (USD) · `credits` `credit` (× $0.01) | Only a positive value counts as real spend |

Models get a provider prefix from their family (`claude-*` → `anthropic/`, `gpt-*` and
`o3` → `openai/`, `gemini-*` → `google/`) so they price and group like other harnesses.

### Duration usage, events and traces (JSONL)

Voice APIs can bill by the second instead of by the token. Log that as a
`usage_snapshot`:

```json
{"record_type":"usage_snapshot","timestamp":"2026-09-27T08:01:34Z","model":"gpt-live-1","session_id":"voice-123","request_id":"voice-123:usage","project":"Iris Live","title":"Voice conversation","prompt":"Voice usage · 94s · confirmed","duration_seconds":94,"rate_per_minute":0.05,"usage_status":"confirmed"}
```

| Record | What it does |
|--------|--------------|
| `usage_snapshot` | `duration_seconds × rate_per_minute / 60` is an **estimated** charge in the `$` view. Requires timestamp, model, session ID, request ID, non-negative seconds and rate, and `usage_status` (`running`, `unconfirmed`, `confirmed`). A later snapshot with the same session and request ID **replaces** the earlier one; a confirmed snapshot wins over later unconfirmed ones |
| `event` | A zero-cost, zero-token Turns row for speech, delegation, errors or lifecycle steps. Needs timestamp, model, session ID and a stable request ID; describe it with `event_kind`, `role`, `prompt`, `prompt_id` and `tools`. Not counted as a model call |

- A positive `cost_usd` overrides the duration estimate for that row. Seconds are never
  turned into tokens.
- Don't reuse one request ID for both ordinary requests and snapshots.
- Optional `response` (text) and `details` (JSON) are private trace content: open the
  Turns row's trace to read them. They never go into caches, web reports or remote
  summaries, so keep `prompt` a short label.
- Without a final event, usage shows as running or unconfirmed. Duration sessions have
  no context curve and no `w` rate comparison, and duration is not active working time.

For GPT-Live, keep the cumulative `session.usage.updated` snapshots and the final
`session.closed` usage; intermediate updates can be zero-cost events. Speech fragments
have no official turn boundaries, so label any grouping you apply and keep the
original offsets in `details`. Delegated backend work keeps its cost in its own
harness; link it with a session ID in `details` rather than copying its spend.

## The merged view (`--harness all`)

`--harness all` merges every present harness. The same repository across tools becomes
one project row, every session shows where it came from (a `Hns` column, or tags such
as `[oc]`, `[cc]`, `[cx]`, `[cp]`, `[vs]`, `[pi]`, `[omp]`, `[ocl]`, `[csv]`, `[jl]`), and
Trends gains a **Harnesses** tab. `$` reprices unpriced usage across all of them.

With more than one harness present, `--demo` uses this merged view and scales every
harness the same way, so the proportions between tools stay true.
