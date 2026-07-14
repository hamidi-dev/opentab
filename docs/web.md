# The web browser (`--html` / `--serve` / `--web`)

A second frontend over the same data — the TUI in your web browser, deliberately
mirroring it: the same lazygit-style sidebar and detail tabs, the same eighth-block
cost bars, the same keymap. It's curses-free, so it also works where the TUI can't.

## One self-contained file: `--html`

`opentab --html` writes the whole browser as **one self-contained HTML file**
(default `opentab-report.html`) — no server, no dependencies, works from disk or any
static host.

- The same sidebar (Years appear with >1 year of data), the same per-scope detail
  tabs, Trends (`T`) and the price table (`P`) as overlays, live range scoping (`R`)
  and colour themes (`C`).
- Driven by the TUI keys (`j`/`k`, `Tab`, `h`/`l`, `Esc`, `$`, `w`, `p`/`t`, `T`, `P`,
  `R`) or the mouse; every table sorts on a header click.
- Every view is a **shareable deep link** (`#/m/2026-06`, `#/s/<session>`, …) and the
  browser's back button steps out.
- `$` toggles the what-if estimate instantly — both cost snapshots travel in the
  page, so it's a client-side swap, never a reprice.
- `w` arms a what-if **model** — the TUI's `w`, mirrored (see below).
- Combine with `--demo` for a page you can publish:
  `opentab --demo --html demo.html`.

## `w` — the what-if model

`w` opens a picker over the models you've actually used — the ones with a real list
price (`j`/`k` move · `f` filters, fzf-style · `Enter` arms · `Esc` cancels) — and
**arms one as a comparison target**: *"what if the expensive model had done the
subagents' work too?"*.

It is **session-scoped**, exactly as in the TUI. Open a session and:

- its **Subagents** tab shows the whole tree — root row included — with each node's
  Cost beside a **What-if** column: that node's tokens at the target's list rates.
  Under it, the session comparison:
  `TOTAL (list rates)  your models $310.46 → all at anthropic/claude-opus-4-5 $104.34   saved $206.12 (66%)`;
- its **Overview** carries the same three figures (Your models / All at *target* /
  Change) — so a session that delegated nothing (no tree to table) still answers.
  Both read one reducer, so they can't disagree.

**Both sides are priced at list rates** — the only apples-to-apples basis for a rate
substitution — and the baseline is computed from the session's *per-model* token
splits, so every token is priced at the model that actually produced it. That is why
arming a model a single-model session already used is exactly a **$0 change**, and
why a subscription backend (which records $0) never reports a 100% saving. The Cost
column keeps its ordinary meaning — recorded spend, `$`-estimated where nothing was
recorded — so it does **not** add up to the TOTAL. There is deliberately **no
per-node Δ**: a node can mix models, so it has no honest baseline of its own.

Everything else on the page — the sidebar, the rollups, Trends, Prices — keeps
showing your actual spend, and `$` keeps working as always. `w` again clears the
target; it works in `--demo` too, and it is never remembered between visits (unlike
the theme and the price pins).

The static file omits the per-session Turns/Tools/Context tabs (embedding them would
mean scanning every session up front) — that's what the server is for.

## Served live: `--serve` and `--web`

`opentab --serve` serves the same browser on `http://localhost:8321` (`--port`) and
adds what a static file can't have: the per-session **Turns** timeline and **Tools**
attribution fetched live on drill-in, plus a refresh button that re-reads your data.

`opentab --web` is the same thing but also opens it in your default web browser —
cross-platform: `open` on macOS, `xdg-open` on Linux, the shell association on
Windows. On a headless box with no browser it just serves.

## Security

The server binds to **localhost only** by default — the page shows prompt titles,
project paths, and spend. If you want it reachable from another machine, put it
behind something like Tailscale (`--bind`, which warns beyond localhost), never a
public interface.

## Themes

The web page and the TUI share one theme source: `C` opens the same picker in both,
the bundled palettes (Catppuccin Mocha/Latte, Tokyo Night/Day, Gruvbox, Nord,
Dracula, Rosé Pine, …) render identically, and the page remembers the viewer's
choice in `localStorage`.
