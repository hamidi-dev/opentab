# The web browser

`opentab web` shows the same data as the terminal app in your web browser, with the
same sidebar, detail tabs, cost bars and keys. It needs no curses, so it also works
where the terminal app can't.

| Command | What it does |
|---------|--------------|
| `opentab web` | Serve on `http://localhost:8321` and open your default browser |
| `opentab web --headless` | Serve without opening a browser |
| `opentab web --port 9000` | Serve on another port |
| `opentab web --html report.html` | Write one self-contained HTML file and exit |

Stop the server with `Ctrl-C`. The older `--web`, `--serve` and `--html` flags still
work.

## Served live

The live server reads your data as you browse. Opening a session loads its **Turns**,
**Tools** and **Context** tabs on demand. A tab only appears when the harness recorded
that data; the server can't add detail a harness or an older machine export didn't
keep.

| Tab | Shows |
|-----|-------|
| **Turns** | Cost per prompt; open a prompt for per-call cost and context charts, with the same turn numbers as its table |
| **Tools** | Which tools and servers the spend went to, down to each call |
| **Context** | Measured request sizes and, where available, an estimate of what filled the window |

The refresh button re-reads local data. It doesn't re-pull other machines; a pulled
machine has its own refresh button ([fleet refresh](machines.md#refresh-and-offline-history)).

### Turns

The overview charts total cost per prompt, grouped like the table. Opening a prompt
charts per-call cost and context with their own scales; when context wasn't recorded,
only cost is shown. `$` updates costs at both levels.

Each prompt also gets a colored token breakdown with exact counts. Expand a turn's
token count for its own breakdown (`Tab` and `Enter` work too). These counts are the
answering turn's usage, not the size of what you typed:

- One-hour cache writes are part of cache writes, not extra.
- Reasoning can be zero when the harness counts it as output.
- Recorded totals are shown as recorded; any gap to the category sum is shown.

### Raw turn traces (opt-in)

Run `opentab web --allow-raw-content` to open individual turns into narration,
reasoning and tool calls with recorded arguments, parameters, status and output.
Open a prompt in **Turns**, then select a turn below its charts. **Subagents**
execution details also list their own turns, including work done entirely in children.

Traces start collapsed with the TUI's caps: 40 prose lines, 6 parameters, 10 lines
per argument value and 6 output lines. Expand one output or use **Expand all**
without losing your scroll position. **Previous turn** / **Next turn** at the top
step through the session's turns (or only the selected execution's turns), showing
the current turn number; Back still returns to the originating turn list.
`[` / `]` select the previous/next tool call; `j` / `k` scroll the trace. **Back**, browser Back and `Esc`
return to the owning turn list. Missing reasoning text is labeled explicitly.
Remote sessions have no web trace transport and show an unavailable message.

The live endpoint `/api/trace?session=…&key=…` (optional `node=…` for an execution)
checks the current session/turn ownership before reading full events. Without the
flag it returns 403 before reading any store. Raw events are never put in the served
page cache, rollup caches or fleet summaries. Demo traces use synthetic fixtures only.

### Tools explorer

The **Tools** tab pairs a spend treemap with rankings by tool and by server. Select a
row or tile to see its share, per-call averages, a token breakdown, the models used and
a list of every call in order.

| Action | Result |
|--------|--------|
| Click a call | Open its prompt in **Turns** with that turn highlighted |
| `Esc`, browser Back or the Back button | Return to the call, then to the ranking |
| `j` / `k`, arrows, `Enter` | Move and open in focused lists |
| `$` | Switch cost basis without losing your place |

Costs are **model usage shared across a turn's tool calls**, not fees for running the
tool. Times are the turn's time; status and duration aren't guessed. Sources with only
totals keep the rankings but have no call list. Open the owning turn for raw traces
when `--allow-raw-content` is enabled.

## One self-contained file: `--html`

`opentab web --html` writes the browser as **one HTML file** (default
`opentab-report.html`). It needs no server and works from disk or any static host.
`--html` can't be combined with `--headless`, `--port` or `--bind`; OpenTab refuses
rather than ignoring them.

By default the file leaves out the per-session **Turns**, **Tools** and **Context**
tabs. To include them:

```sh
opentab web --html report.html --include-details
```

This reads every session, so it takes longer and makes a bigger file. It includes
recorded user prompts, per-turn usage, tool attribution and context data. Without
`--allow-raw-content` it excludes tool arguments and results and reasoning text.
Notes and the separate subagent received-prompt reader are always excluded. With
this option the file is written readable only by you (`0600` on macOS and Linux),
also when it replaces an existing file.

| Option | Effect |
|--------|--------|
| `--allow-raw-content` | Enable raw root and subagent turn traces; with `--html` requires `--include-details` or `--details-dir` |
| `--include-details` | Add Turns, Tools and Context (needs `--html`) |
| `--details-dir DIR` | Instead, write one small details file per session to `DIR`, loaded when a session is opened; serve the report over HTTP (e.g. `python3 -m http.server`) |
| `--harness all` | Include every discovered harness; otherwise only the selected one is in the file |
| `--demo` | Disguise selected data, e.g. `opentab web --demo --html demo.html` |
| date options | Set the starting range; other sessions stay in the file |

To embed full traces in one offline file:

```sh
opentab web --html report.html --include-details --allow-raw-content
```

The same flag works with `--details-dir DIR`. Trace detail files are `0600` and the
directory is `0700`, including an existing directory. Full events are stored once
per turn (shared between root and execution views when identical); collapsed previews are computed by the browser.
Trace exports can be substantially larger: there is no additional export size cap,
so use `--details-dir` for large catalogs. Inline reports work from `file://`;
per-session detail files require HTTP.

A report is a snapshot: generate it again to include new usage. Before publishing a
demo report, review it: [demo mode is selective, not fully private](privacy.md#demo-mode).

## Getting around

Keys match the terminal app, and the mouse works everywhere.

| Key | Action |
|-----|--------|
| `t` / `p` / `u` / `m` | Browse by Time, Projects, Harnesses or Machines |
| `j` / `k`, `1`–`9`, `Tab`, `h` / `l` | Move, jump to a panel, switch tabs |
| `Esc` | Step back out |
| `$` | Switch between recorded spend and the estimate ([pricing](pricing.md)) |
| `#` | Chart tokens instead of cost over time; also the **charts $ / tokens** switch in the header |
| `w` | Compare a session against one model (see [below](#w--compare-model-rates)) |
| `T` / `P` | Trends / price table |
| `R` | Set the date range |
| `C` | Pick a color theme |
| `W` | What's new in each release |

Click any table header to sort. Harnesses is available even when the report has one
source; switching there is navigation, not a change of data source.

**Shareable links.** Time, project, harness, machine and session views have their own
URLs (`#/m/2026-06`, `#/h/<harness>`, `#/s/<session>`), and the browser's Back button
steps out. Drills inside a view and open overlays aren't part of the URL.

## Drilling a model

Click a row in **Models** to focus on that model within the current view:

- **Economics** splits what it cost here by token type.
- **Sessions** lists the sessions that used it, with **Model list** and **Model tok**
  showing this model's share next to each session's own cost.

Both use list rates so token types compare fairly; they aren't a split of your bill.
Unknown rates are marked approximate, and local models have no list price.

A `model: … ✕` chip appears in the breadcrumb; `Esc` or the chip removes it. If you open
a session from the list, `Esc` or Back returns to the model. Going anywhere else drops
the drill, and the URL doesn't include it.

The Models and Harnesses tables show Input (uncached), CacheR, CacheW and Output next to
total tokens, plus Reason when recorded separately. Missing breakdowns show `-`.

In **Trends → Models**, a model opens on **Economics** for the active range; `h` / `l`
switch to **Sessions**, and `Esc` returns to the ranking.

## `w` — compare model rates

The browser has the terminal app's [session-only model comparison](pricing.md#comparing-models-with-w).
Choose from your used models or the full catalog (`Tab` switches, `f` filters, `Enter`
selects).

- **Overview** shows **Your models**, **All at target** and **Change**.
- **Subagents** adds a Compared column per execution and the same comparison.

Both sides use list rates, not your recorded bill. The sidebar, totals, Trends and
Prices don't change, and `$` still works on its own. Press `w` again to clear it. It
works in demo mode and isn't remembered between visits.

## Subagent executions

**Subagents** works in live and static reports. Its overview counts direct and nested
executions, shows the deepest level, and groups work by agent. Cost and token shares
are out of all executions, root included. A flame graph falls back to token widths
when nothing recorded spend.

Click an execution for its details: full title, agent, main model, start, depth, cost
and exact token counts.

- **Cache hit** is cache reads ÷ (input + cache reads + cache writes).
- Token categories need not add up to the recorded total.
- The main model isn't the full model mix, so `w` compares per session, with no
  per-execution saving.

`Esc`, browser Back or **Back to executions** returns to the list. Details have no
shareable link.

### Received prompt (live only)

In a live report you can load an execution's **Received prompt**: the first user
message it recorded (for a subagent, its own first message). It isn't the full system
prompt or context. It loads only when you ask, and it's kept in page memory only until
you navigate away.

The separate panel is unavailable in static reports, demo mode, executions on other
machines and missing local records. Opt-in trace exports can still carry recorded
prompts in their execution turn details. After a server reload or machine refresh, refresh the
browser tab before loading more prompts.

## Security

The server listens on **localhost only** by default and has **no login**. Anyone who
can reach it can read session titles, project paths and spend, and full recorded user
prompts through live Turns and subagent executions. They can also trigger reloads and
saved-machine refreshes. Raw turn traces are served only with `--allow-raw-content`;
notes are never served. Startup explicitly announces “raw traces enabled”. With a
non-localhost bind, an additional warning says anyone reaching the port can read
tool output.

To reach it from another machine, use a private VPN such as Tailscale and restrict who
can reach the port; never expose a public interface. `--bind` warns when you listen
beyond localhost.

A static HTML file contains the data itself. Treat the file as sensitive even though it
has no server behind it. See [Privacy](privacy.md).

## Themes and release notes

`C` opens the same theme picker as the terminal app, and the bundled palettes
(Catppuccin, Tokyo Night, Gruvbox, Nord, Dracula, Rosé Pine and more) look the same in
both. The page remembers the choice in the browser.

`W` shows the bundled release notes, starting at the installed version, offline. It
never checks for updates or keeps an unread marker.

## Good to know

- Tables scroll sideways on small screens; detail text wraps.
- A report built for one harness says so in the Harnesses sidebar.

For how the browser is built, see [Web browser boundaries](architecture.md#web-browser-boundaries) and
[CONTRIBUTING.md](../CONTRIBUTING.md).
