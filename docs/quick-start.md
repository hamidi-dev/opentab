# Quick start

Install OpenTab, open it on your own history, and follow one session from the
month it happened in down to a single turn. It takes about five minutes.

## Install and open

```sh
pipx install opentab-ai
opentab
```

OpenTab finds the history your coding agents already keep on this machine. It only
reads it; nothing is changed or sent anywhere. Other ways to install:
[Overview & install](../README.md#install).

![The OpenTab terminal app across all harnesses](assets/harness-browse.png)

## Find a session

1. On the left, pick a month and a day with `j` / `k`. `Tab` moves between the
   Years, Months and Days panels.
2. Press `Enter` to move into the day on the right, then `l` until you reach
   **Sessions**.
3. Pick a session you remember and press `Enter`.

`Esc` always steps back out. Press `?` anywhere for the keys that work there. The
mouse works too.

## Read the session

A session opens on **Overview**. Switch tabs with `h` / `l`:

| Tab | What it shows |
|-----|---------------|
| Overview | Cost, tokens and models for the whole session |
| Subagents | The helpers it started, and what each cost |
| Turns | Every prompt, then each model call and tool call inside it |
| Tools | Which tools and MCP servers ran, and what they cost |
| Changes | The files it edited, with diffs ([Changes](changes.md)) |
| Context | What filled the context window over time |

Not every harness records every tab; [Data harnesses](sources.md) lists what each
one supports.

## Understand the money

OpenTab shows two kinds of numbers, and labels which one you are looking at:

- **Recorded spend**: what the tool logged as cost.
- **Estimate**: your tokens priced at API list rates, for usage with no recorded
  cost, such as a subscription.

Press `$` to switch between them. [Pricing & the $ view](pricing.md) explains
both, and `w` compares one session at another model's prices.

## Change what you see

| Key | Action |
|-----|--------|
| `t` / `p` / `u` / `m` | Browse by Time, Projects, Harnesses or Machines |
| `H` | Show one harness, or all of them |
| `R` | Set a date range |
| `T` | Trends: charts, calendar heatmap and rankings |
| `P` | The model price table |
| `q` | Quit |

## Open it in the browser

```sh
opentab web                        # opens a local page in your browser
opentab web --html report.html     # one self-contained file you can keep
```

[The web browser](web.md) covers both.

## Next

- Something missing or slow? Run `opentab doctor`, then see
  [Troubleshooting](troubleshooting.md).
- Showing your screen? `opentab --demo` scrambles titles, paths and amounts first
  ([Privacy](privacy.md#demo-mode)).
- Every key: [Keys & navigation](keys.md).
