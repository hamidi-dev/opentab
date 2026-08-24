# Data harnesses

OpenTab reads the local records each AI coding tool keeps. This page covers every
harness in detail: where its data lives, how cost is derived, and the quirks of each
tool's records.

## Picking a harness

Pick one with `--harness`, point its flag at a non-default location, or just pass a
file path (`opentab requests.csv`, `opentab path/to/opencode.db`) and the harness is
inferred from the extension:

```sh
opentab --harness opencode                   # OpenCode only
opentab --harness claude --claude-dir /path  # Claude Code (default ~/.claude/projects)
opentab --harness codex --codex-dir /path    # Codex (default ~/.codex/sessions)
opentab --harness hermes                     # Hermes Agent (default ~/.hermes/state.db)
opentab --harness copilot                    # GitHub Copilot CLI (default ~/.copilot/otel)
opentab --harness vscode                     # Copilot Chat in VS Code (every installed variant)
opentab --harness pi                         # pi-agent (default ~/.pi/agent/sessions)
opentab --harness omp                        # omp (Oh My Pi; pi-agent fork, default ~/.omp/agent/sessions)
opentab --harness openclaw                   # OpenClaw gateway (default ~/.openclaw)
opentab --harness zaly                       # zaly (default ~/.local/share/zaly)
opentab --csv requests.csv                   # a CSV of logged API requests (or --jsonl)
opentab --harness all                        # all present harnesses, merged
```

`--harness auto` (the default) restores your last-used harness, else **merges every
present harness** when more than one exists. The active harness shows as a header chip;
**switch live with `H`** from anywhere, overlays included. (`--source` still works as a
deprecated alias.)

## What each harness supports

Every harness feeds the same browser — months, days, projects, sessions, models,
trends. What each tool's records support on top:

| Harness | Cost | Subagent tree | Turns | Tools | Context |
|--------|------|:---:|:---:|:---:|:---:|
| OpenCode | real recorded | ✓ | ✓ | ✓ | ✓ |
| Claude Code | tokens only — `$` estimates | ✓ | ✓ | ✓ | ✓ |
| Codex CLI | tokens only — `$` estimates | ✓ | ✓ | ✓ | — ³ |
| Hermes Agent | mixed — metered real, rest estimated | ✓ | ✓ ⁵ | — | — |
| GitHub Copilot CLI | tokens only — `$` estimates | — | ✓ ¹ | — | ✓ |
| Copilot Chat in VS Code | tokens only — `$` estimates | — | ✓ | — | ✓ |
| pi-agent | mixed — metered real, rest estimated | — | ✓ | ✓ | ✓ |
| omp | mixed — metered real, rest estimated | ✓ | ✓ | ✓ | ✓ |
| OpenClaw | mixed — metered real, rest estimated | — | ✓ | ✓ | ✓ |
| zaly | mixed — metered real, rest estimated | — | ✓ | ✓ | ✓ |
| CSV / JSONL request logs | mixed — per-row cost column | — | ✓ | ✓ ² | ✓ ⁴ |

<sub>**Subagent tree** — recursive per-subagent cost under the session that delegated ·
**Turns** — the per-turn cost timeline inside a session · **Tools** — token attribution
per tool call and MCP server · **Context** — the context-window growth curve, measured
from recorded usage (it rides on Turns); Claude Code and zaly log full message content,
so they add the estimated breakdown of what filled it ·
¹ headerless: the OTEL export captures no prompt text · ² with the optional `tool`
column · ³ Codex records per-turn deltas of a cumulative total, not per-request prompt
sizes, so an honest curve isn't derivable · ⁴ only with a real `session_id` column — a
synthetic per-day session interleaves unrelated conversations · ⁵ Hermes stores no
per-message usage, so its turns are read from the agent log (`~/.hermes/logs/agent.log*`)
and joined to the session by id; because that log rotates, only sessions inside the
retained window offer the tab, and a resumed session's turns can exceed the total Hermes
itself accumulated.</sub>

Every harness also derives when a session was last active (not just when it started),
including activity from its subagent subtree where tracked. This timestamp
(`ended_at`) feeds the sessions list's **Last Activity** sort (`s`) — the alternative
to sorting by when a session started, offered everywhere except the Time overview's
Days pane (see [`docs/keys.md`](keys.md#scope--filter)).

### Token-only harnesses

The whole TUI works the same everywhere — with two differences for the token-only
tools (Claude Code, Codex, and Copilot, CLI and VS Code alike):

- Their sessions work like OpenCode subscription sessions: **$0 in normal mode** and an
  **estimate** (tokens × API list price) under the **`$`** view, which starts on by
  default (header tag: `ESTIMATED`, rather than the `WHAT-IF` a backend with recorded
  spend gets); press `$` for the recorded numbers, and your choice is remembered.
- Projects roll up to their **git root**, so sessions started in subdirectories group
  under the repo instead of bare folder names.

See [Pricing & the `$` view](pricing.md) for how the estimate is priced.

## [OpenCode](https://opencode.ai)

*SQLite database · records real cost*

- **Reads** `~/.local/share/opencode/opencode.db`, read-only (`--db`, or just
  `opentab path/to.db`). Adapts to OpenCode's schema across versions.
- **Cost**: OpenCode records real per-message cost, so metered spend is real recorded
  money; subscription sessions record a truthful `$0` and get the `$` estimate.
- **Extras**: the recursive subagent cost tree, and the Tools tab's token attribution
  per tool call and MCP server.

## [Claude Code](https://claude.com/claude-code)

*JSONL transcripts · tokens only, `$` estimates*

- **Reads** `~/.claude/projects/**/*.jsonl` (`--claude-dir`).
- **Cost**: Claude Code records tokens but no per-message cost — sessions show `$0`
  recorded, and the `$` view (on by default here) estimates them at API list rates.
- **Notes**: subagent (Task) work shows as a cost tree under its session; resumed and
  forked sessions are deduplicated instead of double-counted; projects roll up to their
  git root. Session titles come from Claude Code's own title when set, else the first
  real user prompt (injected command wrappers are skipped).

## [Codex CLI](https://developers.openai.com/codex)

*Rollout JSONL · tokens only, `$` estimates*

- **Reads** `~/.codex/sessions/**/rollout-*.jsonl` (`--codex-dir`).
- **Cost**: tokens only, like Claude Code — `$0` recorded, estimated under `$`.
- **Notes**: Codex logs a *cumulative* token counter, twice per turn — OpenTab derives
  per-turn deltas from it, skips the duplicate echoes, and detects context-compaction
  resets, so turns sum exactly to the session total. Threads spawned by Codex's
  collab/multi-agent mode fold into a subagent cost tree under the session that
  spawned them, labeled with each agent's nickname.

## [Hermes Agent](https://hermes-agent.nousresearch.com/)

*SQLite database · mixed: metered real, subscription estimated*

- **Reads** `~/.hermes/state.db`, read-only (`--hermes-db`), plus the rotating
  `~/.hermes/logs/agent.log*` files for per-call usage.
- **Cost**: mixed per session — metered routes carry Hermes' real recorded cost;
  subscription routes record `$0` and get the `$` estimate.
- **Notes**: multi-provider, with Hermes' own normalized token accounting; subagent
  sessions form a cost tree. Hermes stores no per-message usage in SQLite, so the Turns
  tab joins API calls from the agent log to prompts in the database. It is available only
  for sessions still covered by the rotating log.

## [GitHub Copilot CLI](https://github.com/github/copilot-cli)

*OpenTelemetry export · opt-in · tokens only, `$` estimates*

- **Reads** `~/.copilot/otel/**/*.jsonl` (`--copilot-dir`), plus the file named by
  `$COPILOT_OTEL_FILE_EXPORTER_PATH`.
- **Enable it**: the CLI records usage **only** when its OpenTelemetry export is on. Set
  the env var before launching/resuming a session — sessions after that show up:

  ```sh
  export COPILOT_OTEL_FILE_EXPORTER_PATH=~/.copilot/otel/usage.jsonl
  ```

- **Cost**: the export carries tokens but no cost — `$0` recorded, estimated under `$`.
- **Notes**: OTEL logs one call up to four ways across spans and logs; OpenTab
  deduplicates them and keeps the highest-fidelity record. The export has no working
  directory, so each session's project and title are enriched (read-only, best effort)
  from the CLI's own session store. Turns are headerless (the export captures no
  prompt text by default).

## [Copilot Chat in VS Code](https://code.visualstudio.com/docs/copilot/chat/copilot-chat)

*VS Code's chat-session store · nothing to enable · tokens only*

- **Reads** VS Code's own store, `<User>/workspaceStorage/*/chatSessions` plus
  empty-window sessions, across Code, Code&nbsp;-&nbsp;Insiders, and VSCodium. Point
  `--vscode-dir` at one User directory for a portable/remote copy — from WSL, at the
  Windows-side store (see [Windows & WSL](windows.md)).
- **Cost**: no dollar cost is recorded (Copilot credits are a quota unit, not USD) —
  `$0` recorded, estimated under `$`.
- **Notes**: token figures are VS Code's own; the recorded input covers a turn's final
  tool round, so long agentic turns under-count input. Projects come from each
  workspace's folder and roll up to the git root; empty-window sessions group under
  "(no workspace)". Sessions the panel merely opened (no tokens) are ignored — merely
  installing VS Code never surfaces the harness.

## [pi-agent](https://pi.dev)

*Session JSONL · mixed: metered real, subscription estimated*

- **Reads** `~/.pi/agent/sessions/**/*.jsonl` (`--pi-dir`, honors `$PI_AGENT_DIR`).
- **Cost**: pi writes a list-price figure for *every* route, so OpenTab counts only
  **metered** routes (OpenRouter, a direct API key) as real spend; OAuth/subscription
  routes stay `$0` and are estimated under `$`. The split is read from pi's
  `auth.json`, read-only.

## [omp](https://omp.sh)

*Session JSONL · a pi-agent fork · mixed: metered real, subscription estimated*

- **Reads** `~/.omp/agent/sessions/**/*.jsonl` (`--omp-dir`, honors `$OMP_AGENT_DIR` —
  opentab's own override; unlike `$PI_AGENT_DIR`, omp itself has no session-dir env var).
- **Cost**: like pi, omp writes a list-price figure for *every* route, so only
  **metered** routes count as real spend; OAuth/subscription routes stay `$0` and are
  estimated under `$`. The split is read from omp's own `agent.db` (SQLite) instead of
  an `auth.json` — just the provider and credential-type columns, read-only, never the
  stored OAuth tokens.
- **Notes**: omp is a rename/fork of pi-agent and writes the same record schema, so
  OpenTab parses it the same way — **with one addition pi lacks**: a session that
  delegates to the `task` tool writes each subagent to a sibling directory, and its
  usage folds into a subagent cost tree under the session that spawned it (like Codex's
  spawned threads), rather than being silently dropped. Nesting counts too — a subagent
  that itself delegates shows up at its true depth, not flattened onto the root or lost.
  Models are recorded as a bare id
  with a separate provider field; OpenTab qualifies them (`provider/model`) so they
  group correctly under Trends' Providers tab. Session titles come from omp's own title
  records when set, else the first real user prompt.

## [OpenClaw](https://github.com/openclaw/openclaw)

*Gateway session JSONL · mixed: metered real, plan routes estimated*

- **Reads** `~/.openclaw/agents/<agent>/sessions/*.jsonl` (`--openclaw-dir`, honors
  `$OPENCLAW_DIR`) — point it at a mounted copy if OpenClaw runs on a server.
- **Cost**: like pi, per-message cost is list-price for every provider — only metered
  routes (a direct Anthropic/OpenRouter key) count as real spend; plan routes
  (openai-codex, github-copilot) are estimated under `$`. The split is read from
  `openclaw.json`, read-only.
- **Notes**: one project per agent; archived sessions are included and deduplicated.
  Recorded `toolCall` blocks feed both per-turn tool names and the Tools tab.

## [zaly](https://github.com/folke/zaly)

*Session JSONL · mixed: metered real, plan routes estimated*

- **Reads** `~/.local/share/zaly/sessions/*/*/session.jsonl` (`--zaly-dir`, honors
  `$ZALY_DATA` and `$ZALY_ROOT`).
- **Cost**: zaly prices every message from its model catalog regardless of route, so —
  like pi and OpenClaw — only **metered** routes (a direct API key) count as real
  spend; OAuth/plan logins (a ChatGPT-plan `openai-codex`, Claude Pro/Max) and local
  models stay `$0` and are estimated under `$`. The split is read from zaly's
  `auth.json`, read-only.
- **Notes**: projects fold to the workspace's git root; resume/fork append to the same
  file, so nothing double-counts (abandoned regenerated branches *do* count — each was
  a real API call). Subagent transcripts are not persisted by zaly (they live in the
  temp dir), so their usage can't be shown.

## CSV / JSONL request logs

*Bring your own ledger · mixed per row*

- **Reads** any CSV (`--csv`) or NDJSON (`--jsonl`) of logged API requests, one request
  per row/line — auto-discovered at `~/.local/share/opentab/requests.csv` /
  `requests.jsonl` if present (a file left in the old `~/.config/opentab/` is still
  found). Log your own gateway or proxy traffic and browse it like any other harness.
- **Cost**: per row — a populated cost column is real spend; rows without one are
  estimated under `$`.
- **Notes**: each request is one turn on the Turns tab, grouped under its `prompt`;
  a stable `request_id` deduplicates regenerated/appended files; without a
  `session_id`, requests group into one synthetic session per (date, project).
  Malformed rows are skipped, never a crash.

### Schema

Headers (CSV) / keys (JSONL) are matched case-insensitively, with aliases. Required
are a timestamp, a model, and input/output token counts; everything else is optional:

| Field | Accepted names | Notes |
|-------|----------------|-------|
| timestamp | `timestamp` `time` `ts` `date` `created_at` `datetime` | ISO-8601 or epoch (s/ms/µs) — **required** |
| model | `model` `model_id` `model_name` | e.g. `gpt-4o`, `claude-sonnet-4` — **required** |
| input | `input_tokens` `input` `prompt_tokens` | as logged (may include the cached read) — **required** |
| output | `output_tokens` `output` `completion_tokens` | includes reasoning (priced once) — **required** |
| cached | `cached_tokens` `cached` `cache_read` `cache_read_tokens` | cached portion of input (default 0) |
| session | `session_id` `session` `conversation_id` `conversation` | groups requests into one session |
| request | `request_id` `id` `req_id` | stable per-request id — dedupes regenerated/appended files |
| prompt | `prompt` `prompt_text` `user_prompt` | the user message → Turns grouping |
| prompt_id | `prompt_id` | stable id for a prompt (optional) |
| tool | `tool` `tool_name` `tools` | tool call(s) the request made — `Bash;Read`, or a JSON list in JSONL → Tools tab |
| project | `project` `repo` `repository` `workspace` `directory` `dir` `cwd` `folder` | a path folds to its git root; a bare name is used as-is |
| title | `title` `name` `label` | session label (default: first prompt) |
| cost | `cost_usd` `cost` (USD) · `credits` `credit` (× $0.01) | presence marks the row as metered, real spend |

Models are provider-prefixed by inferred family (`claude-*` → `anthropic/`, `gpt-*`/
`o3` → `openai/`, `gemini-*` → `google/`) so they price and group like every other
harness's.

## The merged view (`--harness all`)

`--harness all` merges every present harness: the same repo across tools rolls up into
one project row, every session row shows its origin (a `Hns` column,
`[oc]`/`[cc]`/`[cx]`/`[cp]`/`[vs]`/`[pi]`/`[omp]`/`[ocl]`/`[csv]`/`[jl]` tags elsewhere), and
Trends gains a **Harnesses** tab. `$` reprices the unpriced usage across all of them.

With more than one harness present, `--demo` **defaults to this merged view** and
anonymizes every backend under one shared scale, so the cross-tool proportion stays
truthful.
