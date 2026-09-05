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

- The same sidebar (Years appear with >1 year of data), the same per-scope detail
  tabs, Trends (`T`) and the price table (`P`) as overlays, live range scoping (`R`)
  and colour themes (`C`).
- Driven by the TUI keys (`j`/`k`, `Tab`, `h`/`l`, `Esc`, `$`, `w`, `p`/`t`, `T`, `P`,
  `R`) or the mouse; every table sorts on a header click.
- Time, project, machine and session scopes have **shareable deep links**
  (`#/m/2026-06`, `#/s/<session>`, …), and the browser's back button steps out.
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

## `w` — the what-if model

The browser mirrors the TUI's [session-only rate comparison](pricing.md#comparing-models-with-w).
Choose from your used models or the full catalog (`Tab` switches tiers, `f` filters,
`Enter` selects). Overview shows **Your models / All at target / Change**; Subagents
adds a per-node What-if column and the same session comparison.

Both sides use list rates, not the recorded bill. The sidebar, rollups, Trends and
Prices remain unchanged, and `$` still works independently. Press `w` again to
clear the target. It works in demo and is never remembered between visits.

## Served live

`opentab web` serves the browser on `http://localhost:8321` (`--port` changes the
port) and opens it in your default browser. `opentab web --headless` serves without
launching a browser. Stop either with `Ctrl-C`.

Opening a session fetches its **Turns** timeline, **Tools** attribution and
**Context** details on demand. Context charts measured request sizes and, where
available, an estimated composition of what filled the window. Tabs appear only
when that session supplies the relevant data; a live server cannot invent detail
that a harness or an older fleet export did not retain.

The page's refresh button re-reads local data; it does not automatically re-pull
remote machines. A pulled machine's own refresh button requests a new summary.
See [fleet refresh](machines.md#refresh-and-offline-history) for the distinction.

## Security

The server binds to **localhost only** by default and has no authentication.
Anyone who can reach it can read session titles, project paths, spend and, through
live Turns, full user prompts. Raw turn traces and authored notes are not served.
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
- **Sequential store access.** HTTP requests are handled sequentially, not by a
  thread-per-request server. SQLite-backed stores share connections; parallelizing
  handlers would change their access assumptions.
- **Capabilities and mutations.** Static pages make no session-detail requests;
  live extras honor per-session capabilities. Reload (`/api/reload`) and remote
  refresh (`/api/refresh`) are POST-only. Refresh accepts one nonempty machine name,
  never an arbitrary URL or shell command from the browser, and demo blocks it.

Check static and live views together, including a session with no optional detail.
