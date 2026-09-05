# Pricing & the `$` view

## Where the numbers come from

The numbers come straight from each tool's own data (cost/tokens per message, rolled
up per session) — *local attribution* of what your tools recorded. Some sessions show
tokens with a `$0.00` local cost: the usage was recorded but no per-token price,
normal whenever billing isn't per token (subscription plans, credit/token plans).
That money isn't missing, it's billed elsewhere — by your subscription or account
credits — so OpenTab surfaces it as "unpriced tokens" rather than guessing.

Two formatting rules keep this honest: sub-cent costs render as `<$0.01`, while a red
`$0.00` specifically means *unpriced*. And the `$` view **starts on by default** —
most sessions bill nothing per call, so the recorded view would be a wall of `$0.00`;
press `$` for the recorded numbers, and your choice is remembered.

## The `$` what-if view

Press `$` (non-demo) for the **what-if** view: real recorded spend plus what `$0.00`
subscription/credit usage *would have cost* at published API list prices. It's a
toggle — press `$` again for the recorded numbers — and your choice is remembered
between runs.

The estimate uses a **models.dev snapshot bundled with each release** — every
provider, so open models on paid routes (Kimi, DeepSeek, Qwen, … via
OpenRouter/Together/etc.) price out of the box — with family fallbacks for version
churn and a mid-range fallback for unknown models. Ordinary price lookup stays
offline; refreshing rates is an explicit action.

## Comparing models with `w`

`$` fills in unpriced usage. **`w` asks a different question:** what would this
session's recorded tokens cost if one model had produced them all?

Press `w` to choose from your used models or, on the second tab, the full priced
catalog. `Tab` / `h` / `l` switch tiers, `f` filters, and `Enter` selects. Used models
are ranked by token usage; catalog models are ranked by effective price at your
app-wide token mix, not just this session's mix. When no used model is priceable,
the catalog opens directly. Press `w` again to clear the comparison. It also works
in demo mode and is never remembered between runs.

The session's Overview compares **Your models**, **All at target**, and **Change**.
Subagents adds a What-if column beside the ordinary Cost column, including the root
node, followed by the same session comparison. A session without subagents still
has its Overview comparison. Other sessions, rollups and Trends do not change.

Both sides use **list rates**, with the baseline calculated from the session's
per-model token splits. Comparing against recorded dollars would make a subscription
session's baseline zero; pricing every node at its dominant model would misprice
nodes that switched models. Consequently, comparing a single-model session with
that same model produces zero change. There is no per-node saving: node totals
alone cannot supply an honest mixed-model baseline.

The ordinary Cost column retains its recorded/API-equivalent meaning and need not
add up to the list-rate comparison. This is a **rate substitution, not a rerun**:
it does not predict another model's token use, output quality or cache behavior.

## Token economics

Overview's token-economics card compares the token mix with its share of list-rate
cost. A cache-heavy session can have mostly cache-read tokens while output accounts
for most of its price. Five categories are additive: uncached input, output,
reasoning, cache reads and cache writes. Reasoning uses the output rate, but only
gets a separate count when the harness records it outside output.

The calculation uses each producing model's rates, not one blended model label.
It excludes local-model tokens from both distributions and reports them separately.
Unknown prices are marked approximate. This is a token-type explanation at list
rates, **not a decomposition of a provider invoice**, even when recorded dollars
are available for that model.

Drilling a Models row narrows this card to the model's contribution in the current
scope. Its Sessions tab shows **Model list** and **Model tok**, not whole-session
totals. `$` does not change those model-attributed figures; see
[model navigation](keys.md#drilling-a-model).

### Cache-write lifetimes

Anthropic cache writes can buy five minutes or one hour of reuse. The recorded
`cache_write_1h` value is a **subset of total cache writes**, not an extra token
category. If 100,000 writes include 20,000 one-hour tokens, OpenTab prices 80,000
at the short rate and 20,000 at the long rate, still totaling 100,000 tokens.

The catalog supplies the short-write rate. OpenTab derives Anthropic's long rate
as twice the input rate, never below the short rate; other model families retain
their ordinary write rate. Claude records the TTL split, while most harnesses
discard it. Those records cannot support an exact long-TTL adjustment.

### Cache misses are estimates

Cache-miss analysis compares consecutive main-thread turns for a previously cached
prefix that was bought again. It requires a substantial prefix and a large drop
in reuse, rather than labeling every uncached token a miss. Subagents' independent
context windows are excluded from that comparison.

The reported cause is an inference: a model switch, compaction, a recorded reasoning
effort change, other prefix invalidation, or a cache lifetime exceeded while the
agent worked or the user waited. Only Anthropic gets an explicit five-minute or
one-hour TTL; OpenTab does not invent a fixed expiry for opportunistic providers.
The previous turn owns the expired entry's lifetime; the new turn may buy another
tier. The estimated penalty is what the replacement cost above a cache hit, not
the entire turn's bill. It is useful diagnostic evidence, not a provider receipt.

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
| `f` | Fuzzy filter over model/vendor/route — word-anchored, so `opus48` and `snt45` match while mid-word scatter (`opus` → `qwen3-c`**`o`**`der-`**`p`**`l`**`us`**) doesn't — what tames the catalog's ~5k rows |
| `r` | Refresh rates from models.dev in place (see below) |
| `p` / `h` / `l` | Cycle / switch the four views |

The web browser has the same overlay on `P`, with clickable ☆/★ pins kept in
`localStorage`.

## Refreshing rates

Want rates fresher than your release? Refresh from models.dev:

```sh
opentab --refresh-models     # fetch every provider's list prices into a local cache
```

This writes `~/.cache/opentab/prices.json` through an explicitly requested network
fetch (stdlib `urllib`, no dependency). The newer of the cache and the bundled
snapshot wins; you can also press **`r`** inside
`P` to refresh in place. The `P` overlay's source line names which layer is serving
the rates.

When OpenTab notices models it has no built-in price for, it offers this fetch
**once** on startup (`y` now, `n` not now, `d` never — remembered in `state.json`,
suppressed under `--no-state`/`--demo`).

## Contributing to pricing

[`pricing.py`](../src/opentab/pricing.py) owns rate resolution and arithmetic.
App groups the per-model rows once and keeps recorded and API-equivalent workflow
snapshots; `_apply_price_mode()` selects one rather than recomputing spend on every
toggle. The estimate applies only to the `unpriced_*` portion of mixed-billing rows.
Keep the root-only split as well as the subtree split so delegated usage does not
leak into the root's own cost.

In the TUI, `whatif_session_totals()` supplies one comparison to Overview and
Subagents, while `token_economics()` supplies the token-type card. The browser
mirrors those calculations in `webpage.py` with `whatifTotals()` and
`tokenEconomics()`, using serialized model splits and rates. Changes to this
arithmetic need updates and checks in **both implementations**.

The one-hour write subset must survive every intermediate aggregation, including
root-only rows, model scopes, nodes, caches and exports. Adding it to token totals
instead would double-count usage.

Rate lookup has two catalog layers with the same schema: the bundled snapshot and
the user's cache, newest fetch first. `model_price()` prefers catalog matches and
unpinned model cards, then family rules, then the generic fallback. Local providers
short-circuit to zero price but still have context windows. `has_known_price()`
checks the rate's provenance, not whether its numeric tuple happens to equal the
fallback; `has_catalog_row()` answers the stricter question of whether an exact
catalog entry exists.

One distinction is intentional: **eff $/M** rankings substitute input price when a
cache-read rate is missing, so absent metadata cannot make a model look free to
use. The API-equivalent total and token-economics decomposition retain the catalog's
zero-rate arithmetic, with the latter flagging missing rates. Cache-penalty analysis
also uses the conservative input-rate substitute for its hypothetical hit.

The bundled `src/opentab/data/models.json` is generated, not hand-edited. Maintainers
refresh it with `python3 scripts/update_prices.py`; the script and runtime refresh
share `prune_models_dev()`. Review the generated diff and pricing tests together
when changing catalog pruning, aliases or fallback rules.
