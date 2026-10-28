# Pricing & the `$` view

OpenTab shows two kinds of cost: what your tools **recorded**, and an **estimate** at
published API list prices for usage that has no recorded cost, such as a subscription.
Press `$` to switch between them.

A third tool, `w`, compares one session against a single model. `P` opens the rate
table behind all of it.

| Key | What it shows |
|-----|---------------|
| `$` | Recorded spend, or the API-equivalent estimate (the default) |
| `w` | One session's tokens repriced as if one model had produced them all |
| `P` | The per-model list-price table |

## Recorded spend and `$0.00`

Recorded numbers come straight from each tool's own data: cost and tokens per message,
added up per session. Many sessions show tokens with a `$0.00` cost. That usage is
billed elsewhere, by a subscription or credits, so OpenTab calls it **unpriced
tokens** rather than guessing.

- A red `$0.00` means unpriced, not free.
- Costs under a cent show as `<$0.01`.

## The `$` estimate

The estimate is recorded spend plus what the unpriced usage would cost at API list
prices. It is **on by default**, because most subscription sessions would otherwise
read `$0.00`. Press `$` for recorded numbers; OpenTab remembers your choice. Demo mode
always shows recorded numbers.

Rates come from a **models.dev snapshot bundled with each release**. It covers every
provider, so open models on paid routes (Kimi, DeepSeek, Qwen via OpenRouter and
others) price out of the box. Unknown versions fall back to their model family, and
unknown models to a mid-range rate. Lookup stays offline; [refreshing
rates](#refreshing-rates) is an explicit action.

A few cases price differently:

| Usage | How the estimate prices it |
|-------|----------------------------|
| GitHub Copilot | Fully repriced, see [below](#copilot-list-price-estimates) |
| Claude Code fast mode (`usage.speed: fast`) | Its own `-fast` model, at the base model's rates until a fast rate exists |
| Duration-based JSONL records | Reported seconds × the producer's per-minute USD rate; no token count is invented ([JSONL duration records](sources.md#duration-usage-events-and-traces-jsonl)) |

For duration records, Overview shows the reported duration and whether it is running,
unconfirmed or finalized.

## Copilot list-price estimates

For the `github-copilot` provider, the estimate **replaces all recorded costs** with
models.dev's Copilot route rates, including per-request context tiers. OpenCode can
store locally calculated Copilot costs that are not real charges, so they are not
kept. Other providers keep their recorded dollars and only fill in unpriced usage.
Recorded mode (`$`) always shows the original dollars and tokens.

These are **list-price estimates, not a Copilot invoice**.

- **Inferred cache writes.** OpenCode's Copilot adapter can drop GPT-5.6+ cache-write
  counts into ordinary input. For such requests that report zero writes, the estimate
  treats their uncached input as cache writes. Token totals and source files don't
  change, and Token economics and `w` use the same allocation.
- **Approximations are marked.** Token economics shows `~` for inferred writes and for
  missing Copilot rates, which fall back to ordinary rates.
- **Where to check.** `P` shows the loaded snapshot date. Programmatic model estimates
  include their price source and the reasons they are approximate.
- **Older data.** Summaries without the per-request detail can't recover it.

## Comparing models with `w`

`$` fills in unpriced usage. **`w` asks a different question:** what would this
session's recorded tokens cost if one model had produced them all?

| Key | Action |
|-----|--------|
| `w` | Pick a target model; press again to clear it |
| `Tab` / `h` / `l` | Switch between your used models and the full priced catalog |
| `f` | Filter the list |
| `Enter` | Select the target |

Your used models are ranked by token usage. Catalog models are ranked by effective
price at your overall token mix; embedding, image, speech and other non-chat models
are left out. If none of your models has a price, the catalog opens
directly. `w` works in demo mode and is never remembered between runs.

Where it shows:

- **Overview** compares **Your models**, **All at target** and **Change**.
- **Subagents** adds a Compared column next to Cost, including the root, then the same
  comparison.
- Other sessions, totals and Trends don't change.

Both sides use **list rates**, with your side calculated from the session's per-model
token split. So comparing a single-model session with that same model shows zero
change, and a subscription session's baseline isn't zero. There is no per-node saving:
node totals alone can't give an honest mixed-model baseline.

This is a **rate substitution, not a rerun**. It doesn't predict another model's token
use, quality or cache behavior. The ordinary Cost column keeps its meaning and need not
add up to the comparison.

## Token economics

The token-economics card on Overview compares each token type's share of the tokens
with its share of the list-price cost. A cache-heavy session can be mostly cache-read
tokens while output accounts for most of the price.

| Type | Priced at |
|------|-----------|
| Uncached input | Input rate |
| Output | Output rate |
| Reasoning | Output rate, or the card's own reasoning rate where models.dev publishes one (some Qwen and DeepSeek routes); counted separately only when the harness records it apart from output |
| Cache read | Cache-read rate |
| Cache write | Cache-write rate ([lifetimes](#cache-write-lifetimes)) |

Each token is priced at the rate of the model that produced it. Local-model tokens are
left out and reported separately, and unknown prices are marked approximate. This
explains token types at list rates; it is **not a breakdown of a provider invoice**.

- **A drilled model** narrows the card to that model in the current scope. Its Sessions
  tab shows **Model list** and **Model tok**, and `$` doesn't change them ([drilling into a
  model](filters.md#drill-into-a-model)).
- **Trends → Models** opens Economics and Sessions for the selected model in the
  Trends range, independent of the sidebar scope and `$`.

### Context pricing tiers

Some models charge more for long requests. OpenTab prices each request at the highest
tier its **input context** exceeds: uncached input plus cache reads and writes, not
output.

- The threshold is strict: a request exactly at the boundary uses the lower rate.
- The tier prices the **whole** request, output included, not just tokens above it.
- Requests are judged one at a time. Two 200,000-token requests both use base rates.

For example, GPT-5.6 Sol's 272,000-token tier doubles input, read and write rates and
raises output from $20/M to $30/M. Rates refresh without rereading transcripts.

When OpenTab can't tell individual request sizes, tiered usage is priced at base rates
and marked `~` with an explanation in Token economics. That happens for summary-only
records, older exports, Codex deltas that span several requests, and VS Code chat.
CSV/JSONL producers can supply `context_tokens` to avoid it.

The `P` table shows **base rates** with a `↑272k` tag on rows whose rates rise; tiers
apply per request, not to monthly totals. `Enter` on a row lists each tier's rates and
how much of your list cost ran at them ([model card](#the-model-card)).

### Cache-write lifetimes

Anthropic cache writes buy either five minutes or one hour of reuse. The one-hour count
is **part of** total cache writes, not extra. Of 100,000 writes with 20,000 one-hour
tokens, 80,000 are priced at the short rate and 20,000 at the long rate.

| Provider | Cache-write rate | Lifetime |
|----------|------------------|----------|
| Anthropic | Catalog rate for 5 minutes; 2× input (never below the short rate) for 1 hour | 5 minutes or 1 hour |
| OpenAI GPT-5.6+ | 1.25× input when the catalog omits it; reads cost 0.1× input | At least 30 minutes after the last write or reuse |
| Older OpenAI models | No write charge | None to measure |

Only Claude records the one-hour split; other harnesses discard it, so their writes
can't get an exact long-lifetime adjustment.

### Cache misses are estimates

Cache-miss analysis flags a turn that paid again for a large, previously cached prefix.
It needs a substantial prefix and a large drop in reuse, so ordinary uncached tokens
aren't called misses. Subagents are excluded because they have their own context.

The cause shown is an inference:

- a model switch, a compaction or a reasoning-effort change;
- another change to the prefix;
- the cache lifetime ran out while the agent worked or you waited.

Anthropic uses its 5-minute or 1-hour lifetime and GPT-5.6+ its 30-minute minimum,
labeled "guaranteed" because a longer gap doesn't prove when the entry died. OpenTab
invents no lifetime for providers that cache opportunistically. Codex gets markers when
every turn in the session records its request context; otherwise they are off.

The penalty is what rebuying the prefix cost above a cache hit, not the whole turn's
bill. Treat it as diagnostic evidence, not a receipt.

## The `P` price table

`P` opens the per-model rates behind the estimate. Each row is a model you've used,
with spelling variants merged (dots and dashes, date pins and reasoning-effort
suffixes).

| Column | Meaning |
|--------|---------|
| **eff $/M** | List rates blended at **your** overall token mix, cheapest first. A missing cache-read rate is never treated as free: those reads use the input rate and the value gets a `~` |
| **use** | Your share of tokens, as a bar |
| input / output / cache-read / cache-write | The raw list rates, shaded green to red per column; `—` when missing |

The table covers all time, regardless of the selected range. It leaves out ignored
sessions and projects; press `I` to include them, also in the browse totals.

### Four views

Press `p` (or `h` / `l`, or click a tab) to switch:

| View | Shows |
|------|-------|
| **flat** | One list across vendors |
| **by vendor** | Grouped under Anthropic, OpenAI and so on, with each row's access routes |
| **by provider** | One row per route and model, grouped by route, with each row's vendor |
| **models.dev** | The whole catalog (about 5,000 rows) ranked at your mix |

In the models.dev view the same model repeats across gateways on purpose: resale
markups are the point. Your models keep their use bar on every route that sells them.
Free and local models are left out, and a tag marks alpha, beta and deprecated models
and non-chat ones such as `embedding`. In every view, `↑272k` marks rates that rise
above that request context.

### The model card

`Enter` opens a row's sessions under a card with what models.dev knows about it:

| Line | Shows |
|------|-------|
| **List rates** | The row's rates, plus a reasoning rate where the card publishes one that differs from output |
| **Above 272k** | Each context tier's rates; a request over the threshold bills whole at them |
| **Your use** | The share of your list cost that ran at long-context rates, and the share whose request size is unknown, which is priced at base |
| **Model** | Release date, knowledge cutoff, context window, maximum output, open weights, model type |
| **Cheapest route** | For open-weight models, a provider selling the same model at least 5% cheaper at your mix. Free cards, local servers and subscription routes such as Copilot don't count, and hosts can differ in quantization and limits. Closed models get no such line: a far cheaper route for one is grey-market resale |

Lines without data are left out. Your use follows the table's scope: all time, the
current identity filters, ignored sessions per `I`.

### Keys

| Key | Action |
|-----|--------|
| `j` / `k` | Select a row |
| `Space` | Pin the row to a ★ shortlist shown first in every view; pinning a catalog row pins only that route. Remembered between runs |
| `Enter` | Show the model card and the sessions that used the model, across routes and spellings |
| `s` (or click a header) | Sort by model, eff, use or a rate |
| `~` (or click the header again) | Reverse the sort |
| `f` | Filter by model, vendor or route; matches word starts, so `opus48` and `snt45` work |
| `r` | Refresh rates from models.dev |
| `p` / `h` / `l` | Switch views |

The web browser has the same table on `P`, with clickable ☆/★ pins saved in the browser.
Click a row to expand its model card. In the models.dev view the browser's card has
rates, tiers and type only, because the facts for thousands of catalog rows would bloat
every report.

## Refreshing rates

For rates newer than your release, fetch them from models.dev:

```sh
opentab --refresh-models
```

Or press `r` inside `P`. The fetch runs in the background, so you can keep navigating,
and the current data is repriced when it finishes. Rates are saved to
`~/.cache/opentab/prices.json`; OpenTab uses whichever of that file and the bundled
snapshot is newer. The source line in `P` says which one is in use. Each catalog's
parsed form is kept beside the caches (`catalog/*.marshal`), keyed on the SHA-1 of
that file's bytes, of `pricing.py` (read through its loader, so zipped installs count;
an unidentifiable parser caches nothing), the release and the Python version, and
carrying a digest of its own payload, so a status-bar poll does not re-parse 2 MB of
rates and any edit or corruption parses afresh. Catalogs are strict UTF-8 text.

When OpenTab finds models it has no price for, it offers the fetch **once** at startup:
`y` now, `n` not now, `d` never. Your answer is remembered, except with `--no-state`
or `--demo`.

## Good to know

- Recorded cost, the `$` estimate and `w` are separate questions; none of them
  changes the others.
- Estimates use public list prices, so negotiated discounts and plan limits don't
  appear in them.

For how pricing is implemented, see [Pricing and the rate catalog](backends.md#pricing-and-the-rate-catalog) and
[CONTRIBUTING.md](../CONTRIBUTING.md).
