"""API list prices (the bundled models.dev catalog + the refreshed cache) and $ costing."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from opentab import __version__, paths
from opentab.util import anchored_fuzzy_match

# Local providers have no per-token API bill; list-price mode must not invent one.
LOCAL_PROVIDERS = frozenset(
    {"ollama", "lmstudio", "lm-studio", "llamacpp", "llama.cpp", "llama-cpp", "mlx", "local"}
)

# Per-million-token list prices for API-equivalent estimates. The newest explicit
# models.dev refresh or bundled release snapshot wins, followed by family and generic
# fallbacks. Normal pricing remains offline and approximate.

# Specific substring families must precede generic ones.
MODEL_PRICE_FALLBACKS = (
    ("claude-3-opus", 15.0, 75.0, 1.5, 18.75),
    ("claude-3-5-haiku", 0.8, 4.0, 0.08, 1.0),
    ("fable", 10.0, 50.0, 1.0, 12.5),
    ("haiku", 1.0, 5.0, 0.1, 1.25),
    ("sonnet", 3.0, 15.0, 0.3, 3.75),
    ("opus", 5.0, 25.0, 0.5, 6.25),
    ("gpt-6-astra", 10.0, 50.0, 1.0, 12.5),
    ("gpt-6", 10.0, 50.0, 1.0, 12.5),
    ("gpt-5.6-luna", 0.2, 1.2, 0.02, 0.25),
    ("gpt-5.6-terra", 2.0, 12.0, 0.2, 2.5),
    ("gpt-5.6", 4.0, 20.0, 0.4, 5.0),
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
FALLBACK_PRICE = (2.0, 8.0, 0.2, 0.0)

# Context limits fall back by specific-first family when the catalog lacks an id.
# Approximation affects only the Context percentage, never cost.
MODEL_CONTEXT_FALLBACKS = (
    ("gpt-4o-mini", 128_000),
    ("gpt-4o", 128_000),
    ("gpt-4.1", 1_047_576),
    ("gpt-5", 400_000),
    ("o1-", 200_000),
    ("o3-", 200_000),
    ("o4-", 200_000),
    ("gemini", 1_048_576),
    ("grok", 256_000),
    ("deepseek", 131_072),
    ("kimi", 262_144),
    ("qwen", 131_072),
    ("glm", 131_072),
    ("llama", 131_072),
    ("claude", 200_000),
)
DEFAULT_CONTEXT_WINDOW = 200_000


def is_local_provider(name: str) -> bool:
    return str(name).split("/", 1)[0].lower() in LOCAL_PROVIDERS


# Infer vendor from the bare model name, never a gateway route carrying many vendors.
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

# Only unambiguous vendor-route aliases belong here. Subscription, regional, and access
# routes are not interchangeable; ambiguous families use the normal completeness rule.
_VENDOR_PROVIDER_IDS = {
    "qwen": ("alibaba",),
    "moonshot": ("moonshotai",),
}


def model_family(name: str) -> str:
    bare = str(name).rsplit("/", 1)[-1].lower()
    for fam, _label, prefixes in _MODEL_FAMILIES:
        if bare.startswith(prefixes):
            return fam
    return ""


def _openai_paid_cache_writes(name: str) -> bool:
    """Whether OpenAI charges cache creation separately for this model generation."""
    if model_family(name) != "openai":
        return False
    bare = str(name).rsplit("/", 1)[-1].lower()
    match = re.match(r"^gpt-(\d+)(?:[.-](\d{1,2})(?=[.-]|$))?", bare)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major > 5 or (major == 5 and minor >= 6)


def _with_openai_cache_write(
    name: str, row: tuple[float, ...]
) -> tuple[float, float, float, float]:
    # GPT-5.6+ cache writes cost 1.25x input. Older catalogs and some resale cards
    # omit the fourth rate; never turn recorded writes into free tokens because of it.
    if row[0] > 0 and row[3] <= 0 and _openai_paid_cache_writes(name):
        return (row[0], row[1], row[2], row[0] * 1.25)
    return (row[0], row[1], row[2], row[3])


def is_vendor_route(provider_id: str, name: str) -> bool:
    """Identify an unambiguous vendor-owned route over gateway resale cards."""
    fam = model_family(name)
    if not fam:
        return False
    pid = str(provider_id).lower()
    return pid == fam or pid in _VENDOR_PROVIDER_IDS.get(fam, ())


def family_label(family: str) -> str:
    return _FAMILY_LABELS.get(family, "Other")


# Fold separator, date-pin, and reasoning-effort aliases of the same billed model.
_MODEL_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_MODEL_EFFORT_SUFFIX = re.compile(r"-(?:minimal|low|medium|high|xhigh)$")


def display_model(bare: str) -> str:
    return _MODEL_EFFORT_SUFFIX.sub("", _MODEL_DATE_SUFFIX.sub("", str(bare)))


def dots_to_dashes(text: str) -> str:
    return re.sub(r"(?<=\d)\.(?=\d)", "-", text)


def _gpt_version_to_dots(text: str) -> str:
    return re.sub(r"^(gpt-\d+)-(\d{1,2})(?=-|$)", r"\1.\2", text)


def canonical_model(name: str) -> str:
    return dots_to_dashes(display_model(str(name).rsplit("/", 1)[-1].lower()))


def model_matches(query: str, bare: str, routes: Iterable[str] = (), family: str = "") -> bool:
    """Match model ids with anchored fuzzy search and route/vendor fields by substring.

    Fields stay separate to prevent route characters from satisfying a model query.
    Callers preserve their ranking rather than sorting by match quality.
    """
    if not query:
        return True
    q = dots_to_dashes(query.lower())
    if anchored_fuzzy_match(q, dots_to_dashes(str(bare).lower())):
        return True
    fields = [str(r).lower() for r in routes]
    if family:
        fields.append(str(family).lower())
    return any(q in f for f in fields)


def effective_price(
    price: tuple[float, float, float, float], mix: tuple[float, float, float, float]
) -> tuple[float, bool]:
    # Missing cache-read rates fall back to input rather than pretending reads are free.
    ir, orr, crr, cwr = price
    approx = crr <= 0 < ir
    cr = ir if approx else crr
    return mix[0] * ir + mix[1] * orr + mix[2] * cr + mix[3] * cwr, approx


# Both layers share one on-disk schema, {"source", "fetched_at", "providers":
# {pid: {"name", "models": {mid: {"cost": [in, out, cr, cw], "status"?,
# "limit"?}}}}}. Only explicit refreshes write the cache; normal runs stay offline.
MODELS_DEV_URL = "https://models.dev/api.json"
_MODEL_STATUSES = ("alpha", "beta", "deprecated")
# Parsed layer: prices, context limits, provider tree, metadata, vendor-route flags.
_BUNDLED: tuple[dict, dict, dict, dict | None] | None = None
_PRICE_CACHE: tuple[dict, dict, dict, dict | None] | None = None


def price_cache_path() -> str:
    return os.path.join(paths.cache_dir(), "prices.json")


def _catalog_entry(cost: dict, m: dict) -> dict | None:
    # One models.dev cost card -> one snapshot row. Status and context window come off the
    # model, which a mode shares with its base. No input/output rate means no row at all.
    inp, out = cost.get("input"), cost.get("output")
    if not isinstance(inp, (int, float)) or not isinstance(out, (int, float)):
        return None
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
    limit = m.get("limit")
    ctx = limit.get("context") if isinstance(limit, dict) else None
    if isinstance(ctx, (int, float)) and ctx > 0:
        entry["limit"] = int(ctx)
    return entry


def _mode_rows(mid: str, m: dict, models: dict):
    """Priority processing is a MODE on the base model in models.dev, a model of its own in
    every harness that logs it. OpenAI files "GPT-5.6 Sol Fast" as
    experimental.modes.fast -- a `service_tier: priority` request flag plus its own, 2x
    rate card -- while OpenCode writes modelID "gpt-5.6-sol-fast". With no row of that
    name the id fell through to whichever gateway happened to list the spelling (vercel,
    at OpenAI's *base* rate), so every fast turn priced at half. Emit the priced modes as
    real ids on the provider that owns them, so the vendor route wins the bare-id rank.
    """
    modes = (m.get("experimental") or {}).get("modes") if isinstance(m, dict) else None
    if not isinstance(modes, dict):
        return
    for name in sorted(modes, key=str):
        mode = modes[name]
        cost = mode.get("cost") if isinstance(mode, dict) else None
        # A mode that only flips a request flag (openai's "pro") bills at the base rate and
        # needs no row; a spelling the provider already sells keeps its own card.
        if not isinstance(cost, dict) or f"{mid}-{name}" in models:
            continue
        entry = _catalog_entry(cost, m)
        if entry is not None:
            yield f"{mid}-{name}", entry


def prune_models_dev(data: dict) -> dict:
    # Keep numerically priced models, lifecycle status, and context limits. Sorted keys
    # keep generated release snapshots stable.
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
            if not isinstance(m, dict):
                continue
            # Modes are emitted even when the base card is unpriced: a gateway can quote a
            # fast rate for a model it lists no base rate for.
            for smid, sentry in _mode_rows(str(mid), m, models):
                kept[smid] = sentry
            cost = m.get("cost")
            if not isinstance(cost, dict):
                continue
            entry = _catalog_entry(cost, m)
            if entry is None:
                continue
            kept[str(mid)] = entry
        if kept:
            name = p.get("name")
            providers[str(pid)] = {
                "name": name if isinstance(name, str) and name else str(pid),
                "models": kept,
            }
    return providers


def _parse_catalog(data) -> tuple[dict, dict, dict, dict | None, dict]:
    # Accept provider-keyed and legacy flat layers. Bare-id collisions prefer a priced
    # vendor-owned route, then the most complete resale card; retain which route won.
    prices: dict[str, tuple[float, float, float, float]] = {}
    limits: dict[str, int] = {}
    rank: dict[str, tuple] = {}
    providers: dict = {}
    if not isinstance(data, dict):
        return {}, {}, {}, None, {}
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
                limit = m.get("limit")
                if isinstance(limit, (int, float)) and limit > 0:
                    entry["limit"] = int(limit)
                kept[str(mid)] = entry
                bare = str(mid).rsplit("/", 1)[-1].lower()
                # Subscription-only zero cards must not shadow published metered rates.
                priced = sum(1 for v in row if v > 0)
                score = (is_vendor_route(pid, bare) and priced > 0, priced)
                if bare not in rank or score > rank[bare]:
                    prices[bare], rank[bare] = row, score
                    if "limit" in entry:
                        limits[bare] = entry["limit"]
                    else:
                        limits.pop(bare, None)
            if kept:
                name = p.get("name") if isinstance(p, dict) else None
                providers[str(pid)] = {
                    "name": name if isinstance(name, str) and name else str(pid),
                    "models": kept,
                }
    else:
        models = data.get("models")
        if isinstance(models, dict):
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
    return prices, limits, providers, meta, {bare: r[0] for bare, r in rank.items()}


def _load_bundled() -> tuple[dict, dict, dict, dict | None, dict]:
    # importlib.resources supports zipped installs; bad data degrades to fallbacks.
    global _BUNDLED
    if _BUNDLED is None:
        try:
            from importlib.resources import files

            text = files("opentab").joinpath("data").joinpath("models.json").read_text("utf-8")
            _BUNDLED = _parse_catalog(json.loads(text))
        except Exception:  # noqa: BLE001 -- packaging-dependent (zip/dir/missing), all non-fatal
            _BUNDLED = ({}, {}, {}, None, {})
    return _BUNDLED


def _load_price_cache() -> tuple[dict, dict, dict, dict | None, dict]:
    global _PRICE_CACHE
    if _PRICE_CACHE is None:
        try:
            with open(price_cache_path()) as fh:
                _PRICE_CACHE = _parse_catalog(json.load(fh))
        except (OSError, ValueError):
            _PRICE_CACHE = ({}, {}, {}, None, {})
    return _PRICE_CACHE


def _layers() -> list[tuple[dict, dict, dict, dict | None, dict]]:
    # Newest fetch wins; ISO-8601 UTC timestamps compare lexically and ties keep cache first.
    layers = [layer for layer in (_load_price_cache(), _load_bundled()) if layer[3]]
    layers.sort(key=lambda layer: str(layer[3].get("fetched_at") or ""), reverse=True)
    return layers


def price_cache_meta() -> dict | None:
    return _load_price_cache()[3]


def price_source_meta() -> dict | None:
    layers = _layers()
    if not layers:
        return None
    kind = "cache" if layers[0] is _load_price_cache() else "bundled"
    return dict(layers[0][3] or {}, kind=kind)


def catalog_models() -> list[tuple[str, str, tuple[float, float, float, float], str]]:
    # A legacy flat cache can win lookups but cannot replace the provider-tree view.
    for _prices, _limits, tree, _meta, _vendor in _layers():
        if tree:
            return [
                (pid, mid, _with_openai_cache_write(mid, tuple(m["cost"])), m.get("status", ""))
                for pid, p in tree.items()
                for mid, m in p["models"].items()
            ]
    return []


def invalidate_price_cache() -> None:
    global _PRICE_CACHE, _BUNDLED
    _PRICE_CACHE = _BUNDLED = None


def refresh_model_prices(url: str = MODELS_DEV_URL, dest: str | None = None) -> tuple[int, str]:
    # The only runtime network path, reached only through an explicit refresh.
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
    if is_local_provider(name):
        return (0.0, 0.0, 0.0, 0.0)
    mid = _gpt_version_to_dots(str(name).rsplit("/", 1)[-1].lower())
    plain = display_model(mid)
    for prices, _limits, _tree, _meta, vendor in _layers():
        row = prices.get(mid)
        if row is None:
            # Prefer a real unpinned catalog card before family guesses.
            row = prices.get(plain)
            if row is not None:
                return _with_openai_cache_write(mid, row)
            continue
        if plain == mid or vendor.get(mid) or not model_family(mid):
            # Preserve authoritative dated vendor cards; unknown vendors cannot be folded safely.
            return _with_openai_cache_write(mid, row)
        alt = prices.get(plain)
        if alt is None:
            return _with_openai_cache_write(mid, row)
        # Prefer vendor-owned or more complete plain cards over incomplete resale aliases.
        if vendor.get(plain) or sum(1 for v in alt if v > 0) > sum(1 for v in row if v > 0):
            return _with_openai_cache_write(mid, alt)
        return _with_openai_cache_write(mid, row)
    row = next(
        (tuple(p) for needle, *p in MODEL_PRICE_FALLBACKS if needle in mid),
        FALLBACK_PRICE,
    )
    return _with_openai_cache_write(mid, row)


def has_known_price(name: str) -> bool:
    """Return whether a catalog or family rule, rather than the generic guess, priced it.

    Compare provenance, not tuple equality: a real rate card may equal FALLBACK_PRICE.
    Local models have no API rate and cannot be what-if targets.
    """
    if is_local_provider(name):
        return False
    mid = _gpt_version_to_dots(str(name).rsplit("/", 1)[-1].lower())
    plain = display_model(mid)
    if any(mid in prices or plain in prices for prices, _limits, _tree, _meta, _v in _layers()):
        return True
    return any(needle in mid for needle, *_p in MODEL_PRICE_FALLBACKS)


def has_catalog_row(name: str) -> bool:
    """Whether the catalog carries this EXACT id, not a family rule that merely reaches it.

    has_known_price() answers "can this be priced at all", and its family fallbacks match
    any spelling containing the needle -- "claude-haiku-4-5-fast" included, at the plain
    model's rate. A caller asking whether an id genuinely EXISTS (a mode suffix worth
    splitting a model row over) has to compare against the rows themselves.
    """
    mid = _gpt_version_to_dots(str(name).rsplit("/", 1)[-1].lower())
    return any(mid in prices for prices, _limits, _tree, _meta, _vendor in _layers())


def model_context_window(name: str) -> int:
    # Local models still have context windows, so unlike price resolution they do not short-circuit.
    mid = _gpt_version_to_dots(str(name).rsplit("/", 1)[-1].lower())
    for _prices, limits, _tree, _meta, _vendor in _layers():
        if mid in limits:
            return limits[mid]
    return next(
        (w for needle, w in MODEL_CONTEXT_FALLBACKS if needle in mid), DEFAULT_CONTEXT_WINDOW
    )


# Argument-order labels. Input is the uncached residual, disjoint from cache reads/writes;
# reasoning bills at output rates but remains visible as its own category.
TOKEN_TYPES = ("Uncached input", "Output", "Reasoning", "Cache read", "Cache write")


# models.dev carries Anthropic's 5-minute write rate; derive the 2x-input one-hour tier.
CACHE_WRITE_1H_MULTIPLIER = 2.0


def cache_write_1h_price(name: str) -> float:
    """Derive Anthropic's one-hour write rate from input, never below the short tier.

    Family gating prevents a supplied TTL count from inflating another vendor's writes.
    Extend catalog pruning before replacing this with a published one-hour field.
    """
    inp, _out, _cr, cw = model_price(name)
    if model_family(name) != "anthropic" or not inp:
        return cw
    return max(inp * CACHE_WRITE_1H_MULTIPLIER, cw)


def api_equivalent_cost(
    name: str,
    inp: float,
    out: float,
    reasoning: float,
    cache_read: float,
    cache_write: float,
    cache_write_1h: float = 0.0,
) -> float:
    # ``cache_write_1h`` replaces the rate for a subset of total writes; it is not an
    # additive sixth token type. Reasoning bills at the output rate.
    ir, orr, crr, cwr = model_price(name)
    cost = inp * ir + (out + reasoning) * orr + cache_read * crr
    long = min(max(cache_write_1h, 0.0), cache_write)
    if long:
        cost += (cache_write - long) * cwr + long * cache_write_1h_price(name)
    else:
        cost += cache_write * cwr
    return cost / 1e6


# Cache lifetime is measured between consecutive requests because hits refresh the entry.
CACHE_TTL_SHORT = 300
CACHE_TTL_LONG = 3600
# Ignore prefixes below documented cacheability thresholds.
CACHE_MISS_MIN_PREFIX = 5000
CACHE_MISS_COLD_RATIO = 0.5
CACHE_MISS_KEPT_RATIO = 0.6


def cache_ttl_seconds(name: str, cache_write_1h: float = 0.0, cache_write: float = 0.0):
    """Return a documented cache lifetime, or None for opportunistic providers.

    Gate by model family rather than access route so gateway-sold models keep their TTL.
    """
    if model_family(name) == "anthropic":
        # Use the majority recorded tier; normalized-away splits imply the default short tier.
        return CACHE_TTL_LONG if cache_write_1h > cache_write * 0.5 else CACHE_TTL_SHORT
    return None


@dataclass
class CacheMiss:
    """One turn that paid again for a context the previous turn had already cached."""

    index: int
    cause: str
    idle: float
    ttl: int
    repaid: int
    cost: float
    detail: str = ""


# Order causes by user actionability; do not blame long-running agents for human delay.
CACHE_MISS_CAUSES = ("waited", "reasoning", "agent", "invalidated", "compacted", "switched")


def cache_misses(rows) -> list[CacheMiss]:
    """Find main-thread turns that re-bought context using the shared turn-row shape.

    Subagent windows are not compared, but their timestamps distinguish agent work from
    human idle time.
    """
    main = [(i, r) for i, r in enumerate(rows) if not r.get("depth")]
    busy = sorted(t for t in (_row_epoch(r) for r in rows if r.get("depth")) if t is not None)
    out: list[CacheMiss] = []
    for (_pi, prev), (ci, cur) in zip(main, main[1:]):
        prefix = _int(prev.get("cache_read")) + _int(prev.get("cache_write"))
        if prefix < CACHE_MISS_MIN_PREFIX:
            continue
        if _int(cur.get("cache_read")) >= prefix * CACHE_MISS_COLD_RATIO:
            continue
        write, inp = _int(cur.get("cache_write")), _int(cur.get("input"))
        repaid = min(prefix, write + inp)
        if repaid <= 0:
            continue
        model = cur.get("model_name") or ""
        # The previous turn owns the expired entry's TTL; the current turn may buy another tier.
        ttl = cache_ttl_seconds(
            prev.get("model_name") or model,
            _int(prev.get("cache_write_1h")),
            _int(prev.get("cache_write")),
        )
        a, b = _row_epoch(prev), _row_epoch(cur)
        idle = (b - a) if (a is not None and b is not None) else 0.0
        cause = _miss_cause(prev, cur, prefix, repaid, idle, ttl, busy, a, b)
        out.append(
            CacheMiss(
                index=ci,
                cause=cause,
                idle=idle,
                ttl=ttl or 0,
                repaid=repaid,
                cost=_repay_cost(model, repaid, write, _int(cur.get("cache_write_1h"))),
                detail=(f"{_effort(prev)} → {_effort(cur)}" if cause == "reasoning" else ""),
            )
        )
    return out


def _effort(row) -> str:
    return str(row.get("effort") or "").strip()


def _miss_cause(prev, cur, prefix, repaid, idle, ttl, busy, a, b) -> str:
    if (cur.get("model_name") or "") != (prev.get("model_name") or ""):
        return "switched"
    if _int(cur.get("cache_read")) + repaid < prefix * CACHE_MISS_KEPT_RATIO:
        return "compacted"
    if ttl is None or idle <= ttl:
        # A recorded effort change identifies one prefix invalidation cause. Require both
        # sides so a backend omitting the field is not misclassified as a user switch.
        if _effort(cur) and _effort(prev) and _effort(cur) != _effort(prev):
            return "reasoning"
        return "invalidated"
    if a is not None and b is not None and any(a < t < b for t in busy):
        return "agent"
    if cur.get("prompt_id") == prev.get("prompt_id"):
        return "agent"
    return "waited"


def _repay_cost(model: str, repaid: int, write: int, write_1h: int) -> float:
    # Bill re-bought writes and uncached input at their own rates, then subtract a hit.
    ir, _out, crr, cwr = model_price(model)
    w = min(write, repaid)
    w1h = min(write_1h, w)
    paid = (w - w1h) * cwr + w1h * cache_write_1h_price(model) + (repaid - w) * ir
    # A missing cache-read rate falls back to input rather than inventing free hits.
    return max(0.0, paid - repaid * (crr if crr > 0 else ir)) / 1e6


def _int(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _row_epoch(row):
    # Shared turn rows retain only canonical local time; DST may shift the rare spanning gap.
    try:
        return datetime.strptime(str(row.get("time") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None
