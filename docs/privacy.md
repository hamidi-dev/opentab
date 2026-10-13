# Privacy

OpenTab runs locally, with no account and no telemetry. It reads your tools' records
read-only and writes only its own files. This page lists exactly what it reads, writes
and connects to.

## In one minute

- **Reads** your coding tools' records, read-only. It never changes them.
- **Writes** only its own files: preferences, notes, caches and exports you ask for.
- **Connects** to nothing unless you ask, such as a price refresh, a pull from another
  machine or a remote turn read.
- **Shares** nothing on its own. A web report, export or MCP client only gets what you
  hand it, and those contain real titles, paths and spend.
- **Demo mode** (`--demo`) disguises titles, paths, prompts and spend for screenshots.
  It is selective, not a guarantee of anonymity.

## What it reads

| What | Why |
|------|-----|
| Each harness's records: databases, transcripts, logs, exports | Usage, sessions and traces. Paths per harness: [Data harnesses](sources.md) |
| Harness settings such as transcript retention | Warnings and `opentab doctor` |
| pi's and zaly's `auth.json`, OpenClaw's `openclaw.json`, omp's `agent.db` | To tell subscription routes ($0) from metered ones (real spend) |
| Project `.git` files | To fold git worktrees into their main repository. Turn off with `--no-worktrees` |

From the login files OpenTab uses only provider names and login types (OAuth or API
key). The JSON files are parsed whole, but credentials are never used, kept, shown or
exported. For omp the query selects only the provider and credential-type columns.
No `git` process is started.

## What it writes

Only files under OpenTab's own directories. Each honors an **absolute** `XDG_*_HOME`
override; relative values are ignored.

| Directory (default) | Files | When |
|---------------------|-------|------|
| Config `~/.config/opentab/` | `keymap.conf`, `remotes.json` (saved machines), optional `launcher` hook | When you set them up |
| State `~/.local/state/opentab/` | `state.json`: last harness, sorts, diff layout, bookmarks, ignores, pins, and which notices you've seen (search intro, last announced version, the star request's launch count up to 3, reminder date, dismissal) | On exit; off with `--no-state` |
| State `…/debug/` | A JSONL debug log | Only with `--debug` or `--debug-log FILE` |
| Data `~/.local/share/opentab/` | `notes.json`: your session notes | On every note edit |
| Cache `~/.cache/opentab/cache/` | A warm-start summary per harness | After parsing changed files; off with `--no-cache` or `--demo` |
| Cache `~/.cache/opentab/` | `prices.json` | Only on an explicit price refresh |
| Cache `…/remotes/` | Summaries pulled from other machines | When you pull |
| Cache `…/conversations/index.sqlite3` | Plaintext conversation text for search | Only when you confirm Build or Update |
| Where you choose | `opentab-*.csv` (`e`), `opentab web --html FILE`, `opentab export FILE` | Only when you ask |

`state.json` and `notes.json` each have a `.lock` file beside them while being written.

### Debug logs

Debug logs hold versions, timings, counts and cache decisions, with hashed source and
session identifiers. They contain no source paths, SQL, prompts, tool output, titles,
notes or credentials. Each file is owner-only on POSIX and stops growing near 20 MiB;
delete old ones yourself. Logging works with `--demo` and `--no-state`.

### Caches

- The warm-start cache holds usage numbers, never message text or tool output. A
  changed source file is re-read, not shown stale; see
  [caching limits](caching.md#when-a-splice-must-fall-back).
- OpenCode v2 also gets a small usage database in the cache folder so unchanged
  messages aren't re-read. It holds identifiers and token numbers only; see
  [its assumptions](caching.md#opencode-reuse-unchanged-message-accounting).
- Local caches and prices can be rebuilt. Deleting pulled summaries removes that
  machine's offline history until you pull again.

### Exports and the search index

- `opentab web --html FILE --include-details` also embeds your prompts, turn usage,
  tool attribution and context metrics, and is written owner-only.
- The conversation index stores **plaintext** user and assistant text, owner-only, with
  no secret redaction; index only projects you're allowed to. It never goes into
  caches, web reports or remote summaries. Clearing it is not secure erasure. See
  [Conversation search](conversation-search.md).

### Upgrading from an older layout

Versions that kept everything in `~/.config/opentab/` are moved on first run, best
effort. If moving fails, OpenTab keeps reading the old state and notes files. Files
already at the new location win. Old `requests.csv` / `requests.jsonl` logs stay where
they are and are still found.

### Notes are authored data

Press `n` to edit a session note; saving it empty removes it. Notes are off under
`--demo` and `--no-state`.

- Each edit saves immediately, changing only that session's note.
- If `notes.json` is unreadable or malformed, OpenTab reports it and **refuses to
  overwrite it**.
- Entries OpenTab doesn't recognize survive edits. A file from a newer OpenTab is
  readable but not editable until you upgrade.
- Notes for sessions whose records are gone are kept.
- Locking is best-effort on systems without advisory locks, including native Windows.

Back up `notes.json`: unlike the caches, it can't be rebuilt.

## Network

No outbound requests by default. What's New is bundled and offline, and the star
request is local; OpenTab never checks whether you starred it.

| You do | OpenTab connects to |
|--------|---------------------|
| `--refresh-models`, or `r` in the `P` overlay | models.dev, for list prices ([Refreshing rates](pricing.md#refreshing-rates)) |
| `opentab pull`, fleet refresh | Your machines over SSH or HTTP(S) ([Multiple machines](machines.md)) |
| Resume a remote session | That machine over SSH |
| Open a remote turn, or request its content via the CLI or MCP | That machine over SSH, for that turn only ([Remote turns](machines.md#read-a-remote-turn)) |
| Open a GitHub link (What's New, `o` on the star request) | Your browser opens github.com |
| `opentab web` | Starts a local server. `--bind` beyond localhost exposes it to other machines |
| `opentab mcp` | No listener; talks to the MCP client over stdin/stdout. What the client sends on is up to it |

Browsing cached summaries never connects. Remote turn reads work only for saved SSH
connections, not URL entries or imported files.

## External programs

Only for the action you choose. These are programs you pick, not sandboxed: configure
only commands you trust.

| Action | Runs |
|--------|------|
| `o` | Your platform's file opener on a project folder |
| `L` | tmux, Herdr's CLI or your [launcher hook](configuration.md#custom-launchers); copy uses a clipboard tool |
| `y` | A clipboard tool, with the session's user and assistant text (no tools, thinking or attachments). `Ctrl-P` doesn't hide this explicit copy |
| `K` | `$VISUAL`, `$EDITOR` or `vi` on `keymap.conf` |
| `d` in Changes | Your `OPENTAB_DIFF_PAGER`, with the patch on stdin. It may keep or forward what it receives |
| `opentab pull` over SSH | The saved remote export command, including a custom `cmd` |
| Remote turn reads | The remote OpenTab, via the saved `trace_cmd`, the export `cmd`, or `opentab`. Arguments OpenTab adds are always quoted |
| `opentab web` | Your default browser (not with `--headless`) |

Launch, copy and folder opening are off in demo mode.

## Programmatic output

JSON commands and MCP tools show the same titles, project paths, dates, usage, spend
and notes as the app. They are local, not anonymous. Session keys encode the machine,
harness and session ID; that encoding is routing, not encryption.

| Content | How to get it |
|---------|---------------|
| Titles, paths, usage, spend, notes | Always |
| Full prompts, content keys, reasoning, commands, tool results | Start with `--allow-raw-content` and request the fields; MCP trace reads also need `confirm_raw: true` ([Raw content](programmatic.md#raw-content)) |
| Conversation text (OpenCode, Claude Code, Codex, Hermes, Copilot, pi, omp) | `--allow-raw-content`, plus `confirm_raw: true` for MCP. Root session by default; a child needs its exact ID ([Conversation reads](programmatic.md#reading-conversation-records)) |

- Conversation reads return retained text as recorded, including discarded branches
  and repeated prompts. They don't work on remote summaries or in demo mode.
- Opening `Ctrl-F` search in the TUI allows local text reads without a flag. Building
  the index needs its own confirmation. Search never uses the network and skips demo
  and remote data.
- The [Changes](changes.md) tab keeps paths and patches in memory until you quit,
  reload or switch harness. Nothing goes to disk, web, CSV, CLI or MCP output.
- A remote turn read fetches only that turn, checks it still matches, and keeps it in
  memory until you move on. It times out after 30 seconds, and errors don't include
  remote output.
- Whatever a CLI or MCP client receives, it can save or forward.

## Demo mode

`opentab --demo` disguises your real data in memory for demonstrations. It is **not a
fully private or synthetic dataset**: the shape of your data, including dates and how
sessions relate, stays real. A `DEMO — synthetic` header marks the mode.

| Scope | Disguises |
|-------|-----------|
| `titles` | Session, prompt, model and machine names |
| `paths` | Project folders |
| `turns` | Full prompt text in Turns |
| `spend` | Dollars and token counts, scaled by a hidden factor; unpriced usage gets a made-up price |

All four are on by default. Pick some with `--demo titles,turns,spend`, or untick them
in the `D` picker. In demo, notes and turn traces are hidden, launch, copy and folder
opening are off, and preferences aren't saved. Check every screenshot or file before
you share it.

`Ctrl-P` hides your prompts without demo: the root session's Turns prompts become the
same stable fakes. Subagent prompts, titles, paths, spend, traces and search stay real.
It resets on the next launch.

## The served browser

`opentab web` listens on localhost by default and warns when `--bind` goes beyond it
([Security](web.md#security)).

- Reports include titles, project paths and spend. Live session details and fleet data
  can include **full user prompts**.
- Turn traces (narration, reasoning, tool arguments and results) and notes are never in
  web or fleet data. TUI CSV exports can include notes.
- Opening a subagent in the TUI or live browser can read its first prompt. It stays in
  memory for that subagent only, is hidden in demo, and never goes into HTML, fleet
  summaries, caches or programmatic output.
