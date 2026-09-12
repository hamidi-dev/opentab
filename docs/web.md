# The web browser

A second frontend over the same data — the TUI in your web browser, deliberately
mirroring it: the same lazygit-style sidebar and detail tabs, the same eighth-block
cost bars, and familiar navigation keys. It's curses-free, so it also works where
the TUI can't.

```sh
opentab web                     # serve locally and open the default browser
opentab web --headless          # serve without opening a browser
opentab web --html report.html  # write a static file and exit
```

The older `--web`, `--serve`, and `--html` flags remain available.

## One self-contained file: `--html`

`opentab web --html` writes the browser as **one self-contained HTML file**
(default `opentab-report.html`) — no server, no dependencies, works from disk or any
static host.

For `opentab web`, server-only options do not apply to a static file: `--html` cannot be combined
with `--headless`, `--port`, or `--bind`. OpenTab rejects those combinations
rather than silently ignoring them.

- The top browse bar switches between **Time**, **Projects**, **Harnesses**, and
  **Machines** (`t`/`p`/`u`/`m`). Harnesses is available even when the report contains
  only one source; its synthetic **all harnesses** row and source rows show spend and
  session counts within the active range. This is report navigation, not source switching.
- The same sidebar (Years appear with >1 year of data), the same per-scope detail
  tabs, Trends (`T`) and the price table (`P`) as overlays, live range scoping (`R`)
  and colour themes (`C`).
- Driven by the TUI keys (`j`/`k`, `1`–`9`, `Tab`, `h`/`l`, `Esc`, `$`, `w`, `p`/`t`/`u`/`m`, `T`, `P`,
  `R`, `W`) or the mouse; every table sorts on a header click. `W` (or the visible
  header action) opens the bundled offline release history without checking for updates.
  Its Previous/Next buttons and `h`/`l` or arrow keys stop at the history ends;
  the full-release link follows the entry currently shown.
- Time, project, harness, machine and session scopes have **shareable deep links**
  (`#/m/2026-06`, `#/h/<harness>`, `#/s/<session>`, …), and the browser's back button steps out.
  In-place drills and overlay state are not encoded in the URL.
- `$` toggles the what-if estimate instantly — both cost snapshots travel in the
  page, so it's a client-side swap, never a reprice.
- `w` arms a what-if **model** — the TUI's `w`, mirrored (see below).
- Combine with `--demo` to disguise selected data:
  `opentab web --demo --html demo.html`. Review it before publishing;
  [demo mode is selective, not fully private](privacy.md#demo-mode).

Static HTML omits the per-session **Turns / Tools / Context** tabs: embedding them
would require scanning every session up front. It is a snapshot, not a live view;
generate it again to include new usage.

A report generated from one selected harness contains only that loaded source. The
Harnesses sidebar says so explicitly; generate with `opentab web --harness all` to
compare all discovered harnesses in one report.

## Drilling a model

Clicking a row of the **Models** tab drills into that model inside the scope you're
looking at, exactly as the TUI does — the tab strip becomes **Economics** (what it cost
here, split by token type) and **Sessions** (the sessions that used it, with **Model
list** / **Model tok** columns carrying what *this model* accounts for, next to the
session's own cost). The decomposition and **Model list** use list rates so each
token type has a comparable price; they are not a split of the recorded bill.
This is a consistent list-rate comparison even when a harness records per-model spend.
Unknown rates are marked approximate; local models carry no list-price cost. The
breadcrumb grows a `model: … ✕` chip, and `Esc` (or the chip) pops the drill before
leaving the scope. Open one of those sessions and `Esc` (or the browser's Back button)
steps back **into** the model, not out to the session's day — the one hop a drill
survives. Anything else you navigate to drops it, like every other in-place drill.

It isn't a deep link, though: a drill lives in the page, not the URL, so a link you copy
from a model scope points at the scope, not at the model.

The **Models** ranking in Trends uses the same two-tab drill over the active `R` range:
it opens on **Economics**, while `h`/`l` switches between **Economics** and **Sessions**.
The other ranked Trends drills still open their session list directly. `Esc` returns to
the ranking; opening a model's session and returning with `Esc` or browser Back restores
that model drill on **Sessions**.

## `w` — the what-if model

The browser mirrors the TUI's [session-only rate comparison](pricing.md#comparing-models-with-w).
Choose from your used models or the full catalog (`Tab` switches tiers, `f` filters,
`Enter` selects). Overview shows **Your models / All at target / Change**; Subagents
adds a per-node What-if column and the same session comparison.

Both sides use list rates, not the recorded bill. The sidebar, rollups, Trends and
Prices remain unchanged, and `$` still works independently. Press `w` again to
clear the target. It works in demo and is never remembered between visits.

## Subagent executions

**Subagents** works in static and live reports; its overview and tables use only
the embedded node metrics.
Its delegation overview counts direct (depth 1) and nested (depth >1) executions,
shows maximum depth, and groups delegated work by the recorded agent label. Cost
and token shares use the sum of all nodes, including root, not session rollups.
Zero denominators display `-`; the flamegraph still falls back to token widths
when no node records spend.

Click an execution to read its wrapped full title, agent, representative model,
start, depth, current-mode cost/share and exact token categories and recorded total.
Cache hit is cache read divided by input + cache read + cache write; the 1h write
count is a subset, not extra tokens. Categories need not add up to the recorded
total. A representative model is not the execution's full model mix: `w` still
compares list-rate baselines only at session level, with per-node target costs but
no per-node savings. `$` updates costs and shares independently.

Tables scroll horizontally on small screens; detail text wraps. `Tab` uses native
focus within this tab; focused execution rows support `j`/`k` or arrows and
`Enter`/Space. **Back to executions**, `Esc`, or browser Back returns from detail
to the list without leaving the session. Selection uses the original payload
index, so duplicate/anonymous titles, sorting and price toggles cannot select a
different execution. Detail is not a shareable deep link, and no node IDs, raw
content, invented parent relationships, status, duration or turn links are added
to the embedded node metrics.

In a **live report**, opening one execution separately loads its **Received prompt**:
the first recorded user message in that execution (the child session for a subagent),
not the complete system prompt or context payload. The **Title** remains separately
labeled and is never used as a prompt fallback. Loading is explicit; available text
is shown in full, preserving whitespace and wrapping long lines. Static reports,
demo mode, unsupported nodes and unavailable local records explicitly say the prompt
is not available. Remote executions do not trigger network or SSH content reads.

Only the selected prompt is kept in browser memory, not browser storage or the report
payload. Navigation clears it and cancels pending work; responses for an old session,
execution or page snapshot cannot replace the current selection. A server reload or
machine refresh that invalidates the page also expires its execution indices: refresh
an older browser tab before requesting more prompts.

## Served live

`opentab web` serves the browser on `http://localhost:8321` (`--port` changes the
port) and opens it in your default browser. `opentab web --headless` serves without
launching a browser. Stop either with `Ctrl-C`.

Opening a session fetches its **Turns** timeline, **Tools** attribution and
**Context** details on demand. Context charts measured request sizes and, where
available, an estimated composition of what filled the window. Tabs appear only
when that session supplies the relevant data; a live server cannot invent detail
that a harness or an older fleet export did not retain.

The **Turns** overview charts total cost per prompt, using the same consecutive
prompt groups as the table, without a context curve. Opening a prompt shows
per-call cost and context charts for just that prompt, with independent scales
and the same session-global turn numbers as its drill table. If context is not
recorded, only cost is shown. `$` updates costs at both levels; compaction and
cache markers in the overview and the full-session **Context** tab are unchanged.

The page's refresh button re-reads local data; it does not automatically re-pull
remote machines. A pulled machine's own refresh button requests a new summary.
See [fleet refresh](machines.md#refresh-and-offline-history) for the distinction.

## Security

The server binds to **localhost only** by default and has no authentication.
Anyone who can reach it can read session titles, project paths, spend and, through
live Turns and explicitly opened Subagent executions, full recorded user prompts.
Raw turn traces and authored notes are not served.
If you need access from another machine, use a private VPN such as Tailscale and
restrict who can reach the port (`--bind` warns beyond localhost), never a public
interface. Reachable clients can also request reloads and saved-machine refreshes.

A static HTML file contains its data, not just a link to it. Treat the file itself
as sensitive even though it has no live endpoints. See [Privacy](privacy.md).

## Themes

The web page and the TUI share one theme source: `C` opens the same picker in both,
the bundled palettes (Catppuccin Mocha/Latte, Tokyo Night/Day, Gruvbox, Nord,
Dracula, Rosé Pine, …) render identically, and the page remembers the viewer's
choice in `localStorage`.

What's New is manual in static and live reports. It starts at the installed release,
does not infer that the viewer upgraded, stores no unread marker in the browser, and
restores focus to the control that opened it when closed.

## Contributing to the browser

`web.py` adapts a headless App into data; `webpage.py` embeds that data and renders
it in the browser. Keep these boundaries when adding a field or interaction:

- **Explicit payload fields.** Whitelist what the page needs rather than dumping
  store rows or App state. Notes, raw traces and their local content keys do not
  belong in either the initial payload or session extras.
- **Two cost snapshots.** Preserve recorded (`real`) and API-equivalent (`api`)
  values; `$` swaps fields in the browser. The session-only `w` comparison uses
  per-model token splits and list rates, not another global cost mode.
- **Text stays text.** `render_html()` escapes the title and `</` in embedded JSON,
  inserting the payload last. Browser helpers create text nodes for user content.
  Preserve those boundaries rather than interpolating prompts into HTML.
- **Release content stays shared.** Both frontends read the validated bundled resource
  described in [`whats-new.md`](whats-new.md); the browser must not fetch or persist it.
- **Sequential store access.** HTTP requests are handled sequentially, not by a
  thread-per-request server. SQLite-backed stores share connections; parallelizing
  handlers would change their access assumptions.
- **Capabilities and mutations.** Static pages make no session-detail requests;
  live extras honor per-session capabilities. Reload (`/api/reload`) and remote
  refresh (`/api/refresh`) are POST-only. Refresh accepts one nonempty machine name,
  never an arbitrary URL or shell command from the browser, and demo blocks it.
- **Execution prompts are opt-in.** Only `GET /api/node-prompt?session=<id>&node=<index>&snapshot=<nonce>`
  calls `App.read_node_prompt`, never payload building, session extras or prefetch.
  The live page adds a transient `meta.nodeSnapshot` nonce, retained only until page
  invalidation. The endpoint requires the current nonce and one unique workflow,
  then resolves the ordinal against the same memoized `session_node_rows` sequence
  used for the payload. Responses contain `text` (a string or null), with safe error
  text when needed, and are not cacheable. Demo is rejected before any reader call;
  existing Host protection applies. Neither raw node IDs nor received prompt text
  enter static/fleet payloads.

Check static and live views together, including a session with no optional detail.
