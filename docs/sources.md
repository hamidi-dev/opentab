# Data sources

OpenTab reads the local records each AI coding tool keeps. This page covers every
source in detail: where its data lives, how cost is derived, and the quirks of each
tool's records.

## Picking a source

Pick one with `--source`, point its flag at a non-default location, or just pass a
file path (`opentab requests.csv`, `opentab path/to/opencode.db`) and the source is
inferred from the extension:

```sh
opentab --source opencode                    # OpenCode only
opentab --source claude --claude-dir /path   # Claude Code (default ~/.claude/projects)
opentab --source codex --codex-dir /path     # Codex (default ~/.codex/sessions)
opentab --source hermes                      # Hermes Agent (default ~/.hermes/state.db)
opentab --source copilot                     # GitHub Copilot CLI (default ~/.copilot/otel)
opentab --source vscode                      # Copilot Chat in VS Code (every installed variant)
opentab --source pi                          # pi-agent (default ~/.pi/agent/sessions)
opentab --source openclaw                    # OpenClaw gateway (default ~/.openclaw)
opentab --source zaly                        # zaly (default ~/.local/share/zaly)
opentab --csv requests.csv                   # a CSV of logged API requests (or --jsonl)
opentab --source all                         # all present sources, merged
```

`--source auto` (the default) restores your last-used source, else **merges every
present source** when more than one exists. The active source shows as a header chip;
**switch live with `c`** from anywhere, overlays included.

## What each source supports

Every source feeds the same browser — months, days, projects, sessions, models,
trends. What each tool's records support on top:

| Source | Cost | Subagent tree | Turns | Tools |
|--------|------|:---:|:---:|:---:|
| OpenCode | real recorded | ✓ | ✓ | ✓ |
| Claude Code | tokens only — `$` estimates | ✓ | ✓ | ✓ |
| Codex CLI | tokens only — `$` estimates | ✓ | ✓ | ✓ |
| Hermes Agent | mixed — metered real, rest estimated | ✓ | — | — |
| GitHub Copilot CLI | tokens only — `$` estimates | — | ✓ ¹ | — |
| Copilot Chat in VS Code | tokens only — `$` estimates | — | ✓ | — |
| pi-agent | mixed — metered real, rest estimated | — | ✓ | ✓ |
| OpenClaw | mixed — metered real, rest estimated | — | ✓ | — |
| zaly | mixed — metered real, rest estimated | — | ✓ | ✓ |
| CSV / JSONL request logs | mixed — per-row cost column | — | ✓ | ✓ ² |

<sub>**Subagent tree** — recursive per-subagent cost under the session that delegated ·
**Turns** — the per-turn cost timeline inside a session · **Tools** — token attribution
per tool call and MCP server · ¹ headerless: the OTEL export captures no prompt text ·
² with the optional `tool` column.</sub>

### Token-only sources

The whole TUI works the same everywhere — with two differences for the token-only
tools (Claude Code, Codex, and Copilot, CLI and VS Code alike):

- Their sessions work like OpenCode subscription sessions: **$0 in normal mode** and an
  **estimate** (tokens × API list price) under the **`$`** view. Since that view would
  otherwise be a wall of `$0.00`, the estimate **starts on by default** there (header
  tag: `ESTIMATED`); press `$` for the recorded numbers, and your choice is remembered.
- Projects roll up to their **git root**, so sessions started in subdirectories group
  under the repo instead of bare folder names.

See [Pricing & the `$` view](pricing.md) for how the estimate is priced.

## OpenCode

*SQLite database · records real cost*

- **Reads** `~/.local/share/opencode/opencode.db`, read-only (`--db`, or just
  `opentab path/to.db`). Adapts to OpenCode's schema across versions.
- **Cost**: OpenCode records real per-message cost, so metered spend is real recorded
  money; subscription sessions record a truthful `$0` and get the `$` estimate.
- **Extras**: the recursive subagent cost tree, and the Tools tab's token attribution
  per tool call and MCP server.

## Claude Code

*JSONL transcripts · tokens only, `$` estimates*

- **Reads** `~/.claude/projects/**/*.jsonl` (`--claude-dir`).
- **Cost**: Claude Code records tokens but no per-message cost — sessions show `$0`
  recorded, and the `$` view (on by default here) estimates them at API list rates.
- **Notes**: subagent (Task) work shows as a cost tree under its session; resumed and
  forked sessions are deduplicated instead of double-counted; projects roll up to their
  git root. Session titles come from Claude Code's own title when set, else the first
  real user prompt (injected command wrappers are skipped).

## Codex CLI

*Rollout JSONL · tokens only, `$` estimates*

- **Reads** `~/.codex/sessions/**/rollout-*.jsonl` (`--codex-dir`).
- **Cost**: tokens only, like Claude Code — `$0` recorded, estimated under `$`.
- **Notes**: Codex logs a *cumulative* token counter, twice per turn — OpenTab derives
  per-turn deltas from it, skips the duplicate echoes, and detects context-compaction
  resets, so turns sum exactly to the session total. Threads spawned by Codex's
  collab/multi-agent mode fold into a subagent cost tree under the session that
  spawned them, labeled with each agent's nickname.

## Hermes Agent

*SQLite database · mixed: metered real, subscription estimated*

- **Reads** `~/.hermes/state.db`, read-only (`--hermes-db`).
- **Cost**: mixed per session — metered routes carry Hermes' real recorded cost;
  subscription routes record `$0` and get the `$` estimate.
- **Notes**: multi-provider, with Hermes' own normalized token accounting; subagent
  sessions form a cost tree. No Turns tab (Hermes stores no per-message usage).

## GitHub Copilot CLI

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

## Copilot Chat in VS Code

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
  installing VS Code never surfaces the source.

## pi-agent

*Session JSONL · mixed: metered real, subscription estimated*

- **Reads** `~/.pi/agent/sessions/**/*.jsonl` (`--pi-dir`, honors `$PI_AGENT_DIR`).
- **Cost**: pi writes a list-price figure for *every* route, so OpenTab counts only
  **metered** routes (OpenRouter, a direct API key) as real spend; OAuth/subscription
  routes stay `$0` and are estimated under `$`. The split is read from pi's
  `auth.json`, read-only.

## OpenClaw

*Gateway session JSONL · mixed: metered real, plan routes estimated*

- **Reads** `~/.openclaw/agents/<agent>/sessions/*.jsonl` (`--openclaw-dir`, honors
  `$OPENCLAW_DIR`) — point it at a mounted copy if OpenClaw runs on a server.
- **Cost**: like pi, per-message cost is list-price for every provider — only metered
  routes (a direct Anthropic/OpenRouter key) count as real spend; plan routes
  (openai-codex, github-copilot) are estimated under `$`. The split is read from
  `openclaw.json`, read-only.
- **Notes**: one project per agent; archived sessions are included and deduplicated.

## zaly

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
  per row/line — auto-discovered at `~/.config/opentab/requests.csv` /
  `requests.jsonl` if present. Log your own gateway or proxy traffic and browse it
  like any other source.
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
source's.

## The merged view (`--source all`)

`--source all` merges every present source: the same repo across tools rolls up into
one project row, every session row shows its origin (a `Src` column,
`[oc]`/`[cc]`/`[cx]`/`[cp]`/`[vs]`/`[pi]`/`[ocl]`/`[csv]`/`[jl]` tags elsewhere), and
Trends gains a **Sources** tab. `$` reprices the unpriced usage across all of them.

With more than one source present, `--demo` **defaults to this merged view** and
anonymizes every backend under one shared scale, so the cross-tool proportion stays
truthful.
