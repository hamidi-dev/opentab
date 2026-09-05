# OpenTab documentation

The [top-level README](../README.md) covers installation and a tour of the features.
These pages hold the full detail:

| Page | What's in it |
|------|--------------|
| [Data harnesses](sources.md) | Every supported tool — where its records live, how cost is derived, quirks, the CSV/JSONL schema, and the merged `all` view |
| [Keys & navigation](keys.md) | The complete keymap, the browse → zoom → session model, overlays, custom launcher hooks, and what persists between runs |
| [Pricing & the `$` view](pricing.md) | How costs are attributed, the `$` what-if estimate, the `P` price table and models.dev catalog, pinning, and refreshing rates |
| [The web browser](web.md) | `--html`, `--serve`, and `--web` — the self-contained page, the live server, deep links, and security notes |
| [Multiple machines](machines.md) | SSH pulls, browsing and resuming remote sessions, portable exports, and saved machines |
| [Programmatic access](programmatic.md) | Versioned JSON commands, the headless Python service, MCP tools, stable session keys, and raw-content gates |
| [Windows & WSL](windows.md) | Running natively on Windows, and reading Windows-side data from WSL |
| [Privacy — what it touches](privacy.md) | Everything OpenTab reads, writes, and runs; network policy; demo mode |
| [Troubleshooting](troubleshooting.md) | `opentab doctor`, colours that won't change, garbled frames, a harness that won't show up |
| [Architecture](architecture.md) | For contributors: the package map, store contract, data flow and diagnostics |

## Contributor guides

Start with [Architecture](architecture.md), then follow the area you are changing:

| Guide | What it explains |
|-------|------------------|
| [Backend accounting](backends.md) | Token conventions, deduplication, subagent ownership and format-specific limitations |
| [Startup and caching](caching.md) | Deferred work, lazy detail, incremental rollups, invalidation and cost polling |
| [TUI internals](tui.md) | Navigation state, shared tables, terminal geometry, colours and the trace reader |

[Contributing](../CONTRIBUTING.md) covers setup, test organization and checks.
The pricing, privacy and web guides also explain their implementation boundaries.

In the TUI, press **`?`** for help with the current view; [Keys & navigation](keys.md)
is the complete control reference.
