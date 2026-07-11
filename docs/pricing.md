# Pricing & the `$` view

## Where the numbers come from

The numbers come straight from each tool's own data (cost/tokens per message, rolled
up per session) — *local attribution* of what your tools recorded. Some sessions show
tokens with a `$0.00` local cost: the usage was recorded but no per-token price,
normal whenever billing isn't per token (subscription plans, credit/token plans).
That money isn't missing, it's billed elsewhere — by your subscription or account
credits — so OpenTab surfaces it as "unpriced tokens" rather than guessing.

Two formatting rules keep this honest: sub-cent costs render as `<$0.01`, while a red
`$0.00` specifically means *unpriced*. And in the sources that record no cost at all
(Claude Code, Codex, Copilot), the `$` view starts on by default with an `ESTIMATED`
header tag — see [Data sources](sources.md#token-only-sources).

## The `$` what-if view

Press `$` (non-demo) for the **what-if** view: real recorded spend plus what `$0.00`
subscription/credit usage *would have cost* at published API list prices. It's a
toggle — press `$` again for the recorded numbers — and your choice is remembered
between runs.

The estimate uses a **models.dev snapshot bundled with each release** — every
provider, so open models on paid routes (Kimi, DeepSeek, Qwen, … via
OpenRouter/Together/etc.) price out of the box — with family fallbacks for version
churn and a mid-range fallback for unknown models. Nothing is fetched at runtime, so
the TUI stays offline.

## The `P` price table

`P` opens the per-model rate table behind the estimate. Each row shows a model you've
used, deduped to its canonical id (dots == dashes, date pins and reasoning-effort
suffixes folded together), with:

- **eff $/M** — the decision column: the model's list rates blended at **your**
  app-wide token mix (in practice cache-read-heavy), so models compare on what *you*
  would pay, cheapest first. A missing cache-read rate is never treated as free —
  those reads bill at the input rate, the eff value gets a `~` and the raw cell a `—`.
- **use** — your token share as a bar: which models you actually rely on.
- The four raw list rates (input / output / cache-read / cache-write), heat-shaded
  green→red per column.

`p` (or `h`/`l`, or a tab click) cycles four views:

1. **flat** — one ungrouped list (cheapest-for-your-mix is a cross-vendor question).
2. **by vendor** — grouped under `▸ Anthropic/OpenAI/…` headers, rows tagged with
   their access route(s).
3. **by provider** — one row per (route, model) under `▸ anthropic/github-copilot/…`
   headers, rows tagged with their vendor.
4. **models.dev** — the *whole* catalog (~5k rows): every model on every route,
   eff-sorted at your mix — a cheapest-for-your-mix leaderboard where the same model
   deliberately repeats across gateways (resale markups are the information). Models
   you've used keep their use bar on every route that resells them; $0-rate and local
   models are excluded (they'd own the cheap end); a status tag marks
   alpha/beta/deprecated.

Inside `P`:

| Key | Action |
|-----|--------|
| `j` / `k` | Select a row |
| `Space` | Pin the selected row to a ★ shortlist that floats first in every view — pinning one gateway's catalog row pins just that route, never every reseller of the same model. Persisted between runs |
| `Enter` | Drill into the sessions that used the model (aggregated across routes and alias spellings) |
| `s` (or a header click) | Sort by model / eff / use / a rate column |
| `f` | Fuzzy filter (fzf-style, over model/vendor/route) — what tames the catalog's ~5k rows |
| `r` | Refresh rates from models.dev in place (see below) |
| `p` / `h` / `l` | Cycle / switch the four views |

The web browser has the same overlay on `P`, with clickable ☆/★ pins kept in
`localStorage`.

## Refreshing rates

Want rates fresher than your release? Refresh from models.dev:

```sh
opentab --refresh-models     # fetch every provider's list prices into a local cache
```

This writes `~/.config/opentab/prices.json` — the one time runtime OpenTab touches
the network, and only on this explicit command (stdlib `urllib`, no dependency). The
newer of the cache and the bundled snapshot wins; you can also press **`r`** inside
`P` to refresh in place. The `P` overlay's source line names which layer is serving
the rates.

When OpenTab notices models it has no built-in price for, it offers this fetch
**once** on startup (`y` now, `n` not now, `d` never — remembered in `state.json`,
suppressed under `--no-state`/`--demo`).
