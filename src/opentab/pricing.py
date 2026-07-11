"""API list prices (the bundled models.dev catalog + the refreshed cache) and $ costing."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from opentab import __version__

# Providers whose models run on your own hardware. There is no per-token API bill,
# so the "$" what-if view must price them at $0 (pricing them at cloud list rates
# would invent spend that never existed). Demo mode also remaps them to a
# recognizable cloud model so screenshots show a clean provider/model mix instead
# of whatever local tags you happen to run.
LOCAL_PROVIDERS = frozenset(
    {"ollama", "lmstudio", "lm-studio", "llamacpp", "llama.cpp", "llama-cpp", "mlx", "local"}
)

# Per-1M-token list prices (input, output, cache_read, cache_write) powering the
# "$" API-equivalent toggle and the P overlay. opentab ships a *bundled* models.dev
# snapshot (src/opentab/data/models.json -- every provider, regenerated each release
# by scripts/update_prices.py), so pricing works offline out of the box; the explicit
# --refresh-models opt-in (or `r` in the P overlay) fetches a fresh copy into
# ~/.config/opentab/prices.json. Between the two layers the newest fetch wins a
# lookup, so a stale cache never shadows a fresher release (and a fresh refresh beats
# any release). Lookup order: bare model id in the newest layer, then the older
# layer, then the hand-kept MODEL_PRICE_FALLBACKS (substring, to catch dotted/dated/
# effort-suffixed ids and models too new for any snapshot), then FALLBACK_PRICE so an
# unknown model still yields a plausible estimate, not $0. Approximate by design
# (list prices, a point in time); real invoices differ.

# Substring families, specific before generic (first hit wins). Hand-maintained.
MODEL_PRICE_FALLBACKS = (
    ("claude-3-opus", 15.0, 75.0, 1.5, 18.75),
    ("claude-3-5-haiku", 0.8, 4.0, 0.08, 1.0),
    ("fable", 10.0, 50.0, 1.0, 12.5),
    ("haiku", 1.0, 5.0, 0.1, 1.25),
    ("sonnet", 3.0, 15.0, 0.3, 3.75),
    ("opus", 5.0, 25.0, 0.5, 6.25),
    ("gpt-5.5", 5.0, 30.0, 0.5, 0.0),
    ("gpt-5.4", 2.5, 15.0, 0.25, 0.0),
    ("gpt-5.3", 1.75, 14.0, 0.175, 0.0),
    ("gpt-5.2", 1.75, 14.0, 0.175, 0.0),
    ("gpt-5.1", 1.25, 10.0, 0.125, 0.0),
    ("gpt-5-nano", 0.05, 0.4, 0.005, 0.0),
    ("gpt-5-mini", 0.25, 2.0, 0.025, 0.0),
    ("gpt-5", 1.25, 10.0, 0.125, 0.0),
    ("o1-preview", 15.0, 60.0, 7.5, 0.0),
    ("o1-mini", 1.1, 4.4, 0.55, 0.0),
    ("gpt-4o-mini", 0.15, 0.6, 0.075, 0.0),
    ("gpt-4o", 2.5, 10.0, 1.25, 0.0),
    ("gpt-4.1-mini", 0.4, 1.6, 0.1, 0.0),
    ("gpt-4.1", 2.0, 8.0, 0.5, 0.0),
    ("gemini-3-pro", 2.0, 12.0, 0.2, 0.0),
    ("gemini-3-flash", 0.5, 3.0, 0.05, 0.0),
    ("gemini-2.5-flash-lite", 0.1, 0.4, 0.01, 0.0),
    ("gemini-2.5-flash", 0.3, 2.5, 0.03, 0.0),
    ("gemini", 1.25, 10.0, 0.125, 0.0),
)
FALLBACK_PRICE = (2.0, 8.0, 0.2, 0.0)  # unknown model: a mid-range estimate


def is_local_provider(name: str) -> bool:
    # name is "provider/model"; local-hardware providers have no API token cost.
    return str(name).split("/", 1)[0].lower() in LOCAL_PROVIDERS


# The vendor/family behind a model, inferred from the *bare* model name -- (family,
# label, name-prefixes). Order matters only for display grouping, not matching (the
# prefixes are disjoint). The route in a "route/model" id is how you *access* the
# model (a gateway like github-copilot or openrouter carries many vendors), so the
# actual vendor must come from the model name, never the prefix.
_MODEL_FAMILIES = (
    ("anthropic", "Anthropic", ("claude",)),
    ("openai", "OpenAI", ("gpt", "chatgpt", "o1", "o3", "o4", "codex", "davinci", "dall-e")),
    ("google", "Google", ("gemini", "gemma", "palm")),
    ("meta", "Meta", ("llama",)),
    ("mistral", "Mistral", ("mistral", "mixtral", "codestral", "ministral", "magistral", "devstral", "pixtral")),
    ("deepseek", "DeepSeek", ("deepseek",)),
    ("qwen", "Qwen", ("qwen", "qwq")),
    ("moonshot", "Moonshot", ("kimi", "moonshot")),
    ("xai", "xAI", ("grok",)),
    ("zhipu", "Zhipu", ("glm",)),
    ("cohere", "Cohere", ("command",)),
    ("microsoft", "Microsoft", ("phi",)),
)  # fmt: skip
_FAMILY_LABELS = {fam: label for fam, label, _p in _MODEL_FAMILIES}


def model_family(name: str) -> str:
    # The vendor family for a model id (e.g. "anthropic" for github-copilot/claude-*),
    # inferred from the bare model name, or "" (Other) when unrecognized. See
    # _MODEL_FAMILIES for why the route prefix is deliberately ignored.
    bare = str(name).rsplit("/", 1)[-1].lower()
    for fam, _label, prefixes in _MODEL_FAMILIES:
        if bare.startswith(prefixes):
            return fam
    return ""


def family_label(family: str) -> str:
    # Human label for a family key ("anthropic" -> "Anthropic"); "" -> "Other".
    return _FAMILY_LABELS.get(family, "Other")


# One model, many spellings: routes disagree on separators ("claude-sonnet-4.5" vs
# "claude-sonnet-4-5"), pin releases with a date ("-20250929", "-2024-08-06"), and
# Codex appends the reasoning effort ("gpt-5.2-xhigh") -- all the same billed model
# at the same list price. canonical_model() folds them to one grouping key so the P
# overlay shows one row per model instead of one per spelling.
_MODEL_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_MODEL_EFFORT_SUFFIX = re.compile(r"-(?:minimal|low|medium|high|xhigh)$")


def display_model(bare: str) -> str:
    # The human spelling of a bare model id: release-date and reasoning-effort
    # suffixes stripped, the id's own separator style kept.
    return _MODEL_EFFORT_SUFFIX.sub("", _MODEL_DATE_SUFFIX.sub("", str(bare)))


def canonical_model(name: str) -> str:
    # The alias-folding key for a model id (route prefix ignored): the display
    # spelling, lowercased, with version dots normalized to dashes ("4.5" == "4-5").
    bare = display_model(str(name).rsplit("/", 1)[-1].lower())
    return re.sub(r"(?<=\d)\.(?=\d)", "-", bare)


def effective_price(
    price: tuple[float, float, float, float], mix: tuple[float, float, float, float]
) -> tuple[float, bool]:
    # What 1M tokens of `mix` (input/output/cache-read/cache-write shares) cost at
    # `price` -- the P overlay's single comparable "eff $/M" figure. No provider
    # reads cache for free, so a 0 cache-read rate is a missing datum, not a
    # discount: bill those reads at the full input rate (an upper bound) and flag
    # the result as approximate so the UI can mark it.
    ir, orr, crr, cwr = price
    approx = crr <= 0 < ir
    cr = ir if approx else crr
    return mix[0] * ir + mix[1] * orr + mix[2] * cr + mix[3] * cwr, approx


# --- the models.dev catalog: bundled snapshot + optional refreshed cache -----
# Both layers share one on-disk schema, {"source", "fetched_at", "providers":
# {pid: {"name", "models": {mid: {"cost": [in, out, cr, cw], "status"?}}}}}:
# the bundled file is written by scripts/update_prices.py at release time, the
# cache by refresh_model_prices() on the explicit --refresh-models / `r` opt-in.
# Normal runs fetch nothing; opentab stays offline and stdlib-only by default.
MODELS_DEV_URL = "https://models.dev/api.json"
_MODEL_STATUSES = ("alpha", "beta", "deprecated")
# One parsed layer: (bare-id price map, provider tree, meta-or-None).
_BUNDLED: tuple[dict, dict, dict | None] | None = None
_PRICE_CACHE: tuple[dict, dict, dict | None] | None = None


def price_cache_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "opentab", "prices.json")


def prune_models_dev(data: dict) -> dict:
    # Reduce a raw models.dev api.json to the catalog schema's "providers" tree.
    # Only models with numeric input+output rates survive (no cost object means
    # unpriced/local); the alpha/beta/deprecated lifecycle flag rides along so the
    # P overlay's models.dev view can mark those rows. Keys are sorted so the
    # bundled snapshot diffs cleanly between releases. Shared by
    # scripts/update_prices.py (bundled snapshot) and refresh_model_prices (cache).
    providers: dict[str, dict] = {}
    if not isinstance(data, dict):
        return providers
    for pid in sorted(data, key=str):
        p = data[pid]
        models = p.get("models") if isinstance(p, dict) else None
        if not isinstance(models, dict):
            continue
        kept: dict[str, dict] = {}
        for mid in sorted(models, key=str):
            m = models[mid]
            cost = m.get("cost") if isinstance(m, dict) else None
            if not isinstance(cost, dict):
                continue
            inp, out = cost.get("input"), cost.get("output")
            if not isinstance(inp, (int, float)) or not isinstance(out, (int, float)):
                continue
            cr, cw = cost.get("cache_read"), cost.get("cache_write")
            entry: dict = {
                "cost": [
                    float(inp),
                    float(out),
                    float(cr) if isinstance(cr, (int, float)) else 0.0,
                    float(cw) if isinstance(cw, (int, float)) else 0.0,
                ]
            }
            if m.get("status") in _MODEL_STATUSES:
                entry["status"] = m["status"]
            kept[str(mid)] = entry
        if kept:
            name = p.get("name")
            providers[str(pid)] = {
                "name": name if isinstance(name, str) and name else str(pid),
                "models": kept,
            }
    return providers


def _parse_catalog(data) -> tuple[dict, dict, dict | None]:
    # One catalog layer -> (bare-id price map, provider tree, meta). Accepts the
    # provider-keyed schema and the legacy flat {"models": {id: [4 rates]}} cache
    # written before the bundled catalog existed. The price map keys by *bare*
    # model id (last path segment, matching model_price's lookup); on a cross-
    # provider collision the model's own vendor route wins (openrouter also lists
    # anthropic/claude-*, often at a markup), ties to the most completely priced.
    prices: dict[str, tuple[float, float, float, float]] = {}
    rank: dict[str, tuple] = {}
    providers: dict = {}
    if not isinstance(data, dict):
        return {}, {}, None
    tree = data.get("providers")
    if isinstance(tree, dict):
        for pid, p in tree.items():
            models = p.get("models") if isinstance(p, dict) else None
            if not isinstance(models, dict):
                continue
            kept: dict[str, dict] = {}
            for mid, m in models.items():
                cost = m.get("cost") if isinstance(m, dict) else None
                if not (isinstance(cost, (list, tuple)) and len(cost) == 4):
                    continue
                try:
                    row = tuple(float(x) for x in cost)
                except (TypeError, ValueError):
                    continue
                entry: dict = {"cost": row}
                if m.get("status") in _MODEL_STATUSES:
                    entry["status"] = m["status"]
                kept[str(mid)] = entry
                bare = str(mid).rsplit("/", 1)[-1].lower()
                score = (str(pid).lower() == model_family(bare), sum(1 for v in row if v > 0))
                if bare not in rank or score > rank[bare]:
                    prices[bare], rank[bare] = row, score
            if kept:
                name = p.get("name") if isinstance(p, dict) else None
                providers[str(pid)] = {
                    "name": name if isinstance(name, str) and name else str(pid),
                    "models": kept,
                }
    else:
        models = data.get("models")
        if isinstance(models, dict):  # legacy flat cache (already keyed by bare id)
            for mid, row in models.items():
                if isinstance(row, (list, tuple)) and len(row) == 4:
                    try:
                        prices[str(mid).lower()] = tuple(float(x) for x in row)
                    except (TypeError, ValueError):
                        continue
    meta = (
        {"fetched_at": data.get("fetched_at"), "source": data.get("source"), "count": len(prices)}
        if prices
        else None
    )
    return prices, providers, meta


def _load_bundled() -> tuple[dict, dict, dict | None]:
    # Parse the release-bundled snapshot once, lazily. importlib.resources (not a
    # path join) so a zipped install still resolves; a missing/garbled file
    # degrades to the hand-kept fallbacks, never a crash.
    global _BUNDLED
    if _BUNDLED is None:
        try:
            from importlib.resources import files

            text = files("opentab").joinpath("data").joinpath("models.json").read_text("utf-8")
            _BUNDLED = _parse_catalog(json.loads(text))
        except Exception:  # noqa: BLE001 -- packaging-dependent (zip/dir/missing), all non-fatal
            _BUNDLED = ({}, {}, None)
    return _BUNDLED


def _load_price_cache() -> tuple[dict, dict, dict | None]:
    # Read the user cache once, lazily; a missing/garbled file means "no overlay".
    global _PRICE_CACHE
    if _PRICE_CACHE is None:
        try:
            with open(price_cache_path()) as fh:
                _PRICE_CACHE = _parse_catalog(json.load(fh))
        except (OSError, ValueError):
            _PRICE_CACHE = ({}, {}, None)
    return _PRICE_CACHE


def _layers() -> list[tuple[dict, dict, dict | None]]:
    # The catalog layers, newest models.dev fetch first -- the newer of the user
    # cache and the bundled release snapshot wins a lookup, so a year-old cache
    # can't shadow a fresher release and a fresh refresh beats any release.
    # ISO-8601 UTC timestamps compare lexically; a tie keeps the cache first.
    layers = [layer for layer in (_load_price_cache(), _load_bundled()) if layer[2]]
    layers.sort(key=lambda layer: str(layer[2].get("fetched_at") or ""), reverse=True)
    return layers


def price_cache_meta() -> dict | None:
    # Meta of the *user cache* only (gates the one-time fetch prompt).
    return _load_price_cache()[2]


def price_source_meta() -> dict | None:
    # The layer model_price() reads first -- what the P overlay's source line
    # shows. kind: "cache" (a --refresh-models fetch) or "bundled" (the snapshot
    # shipped with this release). None only when both layers are absent/garbled.
    layers = _layers()
    if not layers:
        return None
    kind = "cache" if layers[0] is _load_price_cache() else "bundled"
    return dict(layers[0][2] or {}, kind=kind)


def catalog_models() -> list[tuple[str, str, tuple[float, float, float, float], str]]:
    # Every model in the newest layer that carries provider structure, as
    # (provider_id, model_id, (in, out, cacheR, cacheW), status) rows -- the P
    # overlay's models.dev view. A legacy flat cache has no provider tree, so the
    # bundled snapshot backs the view even when a newer flat cache wins lookups.
    for _prices, tree, _meta in _layers():
        if tree:
            return [
                (pid, mid, tuple(m["cost"]), m.get("status", ""))
                for pid, p in tree.items()
                for mid, m in p["models"].items()
            ]
    return []


def invalidate_price_cache() -> None:
    global _PRICE_CACHE, _BUNDLED
    _PRICE_CACHE = _BUNDLED = None


def refresh_model_prices(url: str = MODELS_DEV_URL, dest: str | None = None) -> tuple[int, str]:
    # Fetch every provider's list prices from models.dev and write the local cache
    # (same provider-keyed schema as the bundled snapshot). Returns (model_count,
    # path); raises OSError/ValueError on network or parse failure. The one place
    # runtime opentab touches the network -- only on explicit refresh.
    from urllib.request import Request, urlopen

    req = Request(url, headers={"User-Agent": f"opentab/{__version__}"})
    with urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if not isinstance(data, dict):
        raise ValueError("unexpected models.dev response")
    providers = prune_models_dev(data)
    count = sum(len(p["models"]) for p in providers.values())
    if not count:
        raise ValueError("no priced models found in the models.dev response")
    path = dest or price_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "source": url,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "providers": providers,
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    invalidate_price_cache()
    return count, path


def model_price(name: str) -> tuple[float, float, float, float]:
    # (input, output, cache_read, cache_write) per 1M tokens for the model.
    if is_local_provider(name):
        return (0.0, 0.0, 0.0, 0.0)  # runs on your hardware -- no per-token API bill
    mid = str(name).rsplit("/", 1)[-1].lower()
    for prices, _tree, _meta in _layers():
        if mid in prices:
            return prices[mid]
    return next((tuple(p) for needle, *p in MODEL_PRICE_FALLBACKS if needle in mid), FALLBACK_PRICE)


def api_equivalent_cost(
    name: str, inp: float, out: float, reasoning: float, cache_read: float, cache_write: float
) -> float:
    # What this usage would cost at API list prices. Reasoning tokens bill as
    # output; cache reads/writes at their own discounted/surcharged rates.
    ir, orr, crr, cwr = model_price(name)
    return (inp * ir + (out + reasoning) * orr + cache_read * crr + cache_write * cwr) / 1e6
