"""API list-price table, the optional price cache, and $ what-if costing."""
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
# "$" API-equivalent toggle. Prices are *embedded* (fetched only on the explicit
# --refresh-models opt-in, see the price-cache block below), so opentab stays a
# single offline stdlib-only script by default.
#
# MODEL_PRICE_TABLE is generated from models.dev by scripts/update_prices.py -- run
# that to refresh; do NOT hand-edit the block between the markers. Lookup is
# exact model id first, then the hand-kept MODEL_PRICE_FALLBACKS (substring, to
# catch dotted/dated/effort-suffixed ids and models too new for the table), then
# FALLBACK_PRICE so an unknown model still yields a plausible estimate, not $0.
# Approximate by design (list prices, a point in time); real invoices differ.

# ===== BEGIN GENERATED PRICES (scripts/update_prices.py) =====
# Generated from models.dev on 2026-06-13 for providers: anthropic, openai, google.
MODEL_PRICE_TABLE: dict[str, tuple[float, float, float, float]] = {
    "claude-3-5-haiku-20241022": (0.8, 4.0, 0.08, 1.0),
    "claude-3-5-haiku-latest": (0.8, 4.0, 0.08, 1.0),
    "claude-3-5-sonnet-20240620": (3.0, 15.0, 0.3, 3.75),
    "claude-3-5-sonnet-20241022": (3.0, 15.0, 0.3, 3.75),
    "claude-3-7-sonnet-20250219": (3.0, 15.0, 0.3, 3.75),
    "claude-3-haiku-20240307": (0.25, 1.25, 0.03, 0.3),
    "claude-3-opus-20240229": (15.0, 75.0, 1.5, 18.75),
    "claude-3-sonnet-20240229": (3.0, 15.0, 0.3, 0.3),
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.1, 1.25),
    "claude-opus-4-0": (15.0, 75.0, 1.5, 18.75),
    "claude-opus-4-1": (15.0, 75.0, 1.5, 18.75),
    "claude-opus-4-1-20250805": (15.0, 75.0, 1.5, 18.75),
    "claude-opus-4-20250514": (15.0, 75.0, 1.5, 18.75),
    "claude-opus-4-5": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-5-20251101": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-6": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-7": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-4-0": (3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-20250514": (3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-5": (3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-5-20250929": (3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75),
    "gemini-2.0-flash": (0.1, 0.4, 0.025, 0.0),
    "gemini-2.0-flash-lite": (0.075, 0.3, 0.0, 0.0),
    "gemini-2.5-flash": (0.3, 2.5, 0.03, 0.0),
    "gemini-2.5-flash-image": (0.3, 30.0, 0.075, 0.0),
    "gemini-2.5-flash-lite": (0.1, 0.4, 0.01, 0.0),
    "gemini-2.5-flash-preview-tts": (0.5, 10.0, 0.0, 0.0),
    "gemini-2.5-pro": (1.25, 10.0, 0.125, 0.0),
    "gemini-2.5-pro-preview-tts": (1.0, 20.0, 0.0, 0.0),
    "gemini-3-flash-preview": (0.5, 3.0, 0.05, 0.0),
    "gemini-3-pro-image-preview": (2.0, 120.0, 0.0, 0.0),
    "gemini-3-pro-preview": (2.0, 12.0, 0.2, 0.0),
    "gemini-3.1-flash-image-preview": (0.5, 60.0, 0.0, 0.0),
    "gemini-3.1-flash-lite": (0.25, 1.5, 0.025, 0.0),
    "gemini-3.1-flash-lite-preview": (0.25, 1.5, 0.025, 0.0),
    "gemini-3.1-pro-preview": (2.0, 12.0, 0.2, 0.0),
    "gemini-3.1-pro-preview-customtools": (2.0, 12.0, 0.2, 0.0),
    "gemini-3.5-flash": (1.5, 9.0, 0.15, 0.0),
    "gemini-embedding-001": (0.15, 0.0, 0.0, 0.0),
    "gemini-flash-latest": (0.3, 2.5, 0.075, 0.0),
    "gemini-flash-lite-latest": (0.1, 0.4, 0.025, 0.0),
    "gpt-3.5-turbo": (0.5, 1.5, 0.0, 0.0),
    "gpt-4": (30.0, 60.0, 0.0, 0.0),
    "gpt-4-turbo": (10.0, 30.0, 0.0, 0.0),
    "gpt-4.1": (2.0, 8.0, 0.5, 0.0),
    "gpt-4.1-mini": (0.4, 1.6, 0.1, 0.0),
    "gpt-4.1-nano": (0.1, 0.4, 0.025, 0.0),
    "gpt-4o": (2.5, 10.0, 1.25, 0.0),
    "gpt-4o-2024-05-13": (5.0, 15.0, 0.0, 0.0),
    "gpt-4o-2024-08-06": (2.5, 10.0, 1.25, 0.0),
    "gpt-4o-2024-11-20": (2.5, 10.0, 1.25, 0.0),
    "gpt-4o-mini": (0.15, 0.6, 0.075, 0.0),
    "gpt-5": (1.25, 10.0, 0.125, 0.0),
    "gpt-5-chat-latest": (1.25, 10.0, 0.125, 0.0),
    "gpt-5-codex": (1.25, 10.0, 0.125, 0.0),
    "gpt-5-mini": (0.25, 2.0, 0.025, 0.0),
    "gpt-5-nano": (0.05, 0.4, 0.005, 0.0),
    "gpt-5-pro": (15.0, 120.0, 0.0, 0.0),
    "gpt-5.1": (1.25, 10.0, 0.125, 0.0),
    "gpt-5.1-chat-latest": (1.25, 10.0, 0.125, 0.0),
    "gpt-5.1-codex": (1.25, 10.0, 0.125, 0.0),
    "gpt-5.1-codex-max": (1.25, 10.0, 0.125, 0.0),
    "gpt-5.1-codex-mini": (0.25, 2.0, 0.025, 0.0),
    "gpt-5.2": (1.75, 14.0, 0.175, 0.0),
    "gpt-5.2-chat-latest": (1.75, 14.0, 0.175, 0.0),
    "gpt-5.2-codex": (1.75, 14.0, 0.175, 0.0),
    "gpt-5.2-pro": (21.0, 168.0, 0.0, 0.0),
    "gpt-5.3-chat-latest": (1.75, 14.0, 0.175, 0.0),
    "gpt-5.3-codex": (1.75, 14.0, 0.175, 0.0),
    "gpt-5.3-codex-spark": (1.75, 14.0, 0.175, 0.0),
    "gpt-5.4": (2.5, 15.0, 0.25, 0.0),
    "gpt-5.4-mini": (0.75, 4.5, 0.075, 0.0),
    "gpt-5.4-nano": (0.2, 1.25, 0.02, 0.0),
    "gpt-5.4-pro": (30.0, 180.0, 0.0, 0.0),
    "gpt-5.5": (5.0, 30.0, 0.5, 0.0),
    "gpt-5.5-pro": (30.0, 180.0, 0.0, 0.0),
    "o1": (15.0, 60.0, 7.5, 0.0),
    "o1-pro": (150.0, 600.0, 0.0, 0.0),
    "o3": (2.0, 8.0, 0.5, 0.0),
    "o3-deep-research": (10.0, 40.0, 2.5, 0.0),
    "o3-mini": (1.1, 4.4, 0.55, 0.0),
    "o3-pro": (20.0, 80.0, 0.0, 0.0),
    "o4-mini": (1.1, 4.4, 0.275, 0.0),
    "o4-mini-deep-research": (2.0, 8.0, 0.5, 0.0),
    "text-embedding-3-large": (0.13, 0.0, 0.0, 0.0),
    "text-embedding-3-small": (0.02, 0.0, 0.0, 0.0),
    "text-embedding-ada-002": (0.1, 0.0, 0.0, 0.0),
}
# ===== END GENERATED PRICES =====

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


# --- optional models.dev price cache (overlays the embedded table) -----------
# opentab ships an offline price snapshot (the generated MODEL_PRICE_TABLE, only
# anthropic/openai/google), so open models served by paid routes (kimi/deepseek/qwen on
# OpenRouter, Together, ...) have no embedded price and show as unpriced. `--refresh-models`
# (and `r` in the P overlay) fetches *every* models.dev provider into a local JSON cache;
# model_price() reads that cache first, so a refresh prices the long tail and freshens the
# big three -- with the embedded table as the offline fallback for anyone who never refreshes.
# Normal runs fetch nothing (the cache is just a local file); this keeps opentab offline and
# stdlib-only by default.
MODELS_DEV_URL = "https://models.dev/api.json"
_PRICE_CACHE: dict[str, tuple[float, float, float, float]] | None = None
_PRICE_CACHE_META: dict | None = None


def price_cache_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "opentab", "prices.json")


def _load_price_cache() -> dict[str, tuple[float, float, float, float]]:
    # Read the cache once, lazily; a missing/garbled file just means "no overlay".
    global _PRICE_CACHE, _PRICE_CACHE_META
    if _PRICE_CACHE is not None:
        return _PRICE_CACHE
    out: dict[str, tuple[float, float, float, float]] = {}
    try:
        with open(price_cache_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        _PRICE_CACHE, _PRICE_CACHE_META = out, None
        return out
    models = data.get("models") if isinstance(data, dict) else None
    if isinstance(models, dict):
        for mid, row in models.items():
            if isinstance(row, (list, tuple)) and len(row) == 4:
                try:
                    out[str(mid).lower()] = tuple(float(x) for x in row)
                except (TypeError, ValueError):
                    continue
    _PRICE_CACHE = out
    _PRICE_CACHE_META = (
        {
            "fetched_at": data.get("fetched_at"),
            "source": data.get("source"),
            "count": len(out),
        }
        if out
        else None
    )
    return out


def price_cache_meta() -> dict | None:
    _load_price_cache()
    return _PRICE_CACHE_META


def invalidate_price_cache() -> None:
    global _PRICE_CACHE, _PRICE_CACHE_META
    _PRICE_CACHE = _PRICE_CACHE_META = None


def refresh_model_prices(url: str = MODELS_DEV_URL, dest: str | None = None) -> tuple[int, str]:
    # Fetch every provider's list prices from models.dev and write the local cache.
    # Returns (model_count, path); raises OSError/ValueError on network or parse failure.
    # The one place runtime opentab touches the network -- only on explicit refresh.
    from urllib.request import Request, urlopen

    req = Request(url, headers={"User-Agent": f"opentab/{__version__}"})
    with urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if not isinstance(data, dict):
        raise ValueError("unexpected models.dev response")
    models: dict[str, list[float]] = {}
    for provider_data in data.values():
        if not isinstance(provider_data, dict):
            continue
        provider_models = provider_data.get("models")
        if not isinstance(provider_models, dict):
            continue
        for mid, model in provider_models.items():
            if not isinstance(model, dict):
                continue
            cost = model.get("cost")
            if not isinstance(cost, dict):
                continue
            inp, out = cost.get("input"), cost.get("output")
            if not isinstance(inp, (int, float)) or not isinstance(out, (int, float)):
                continue
            cr = cost.get("cache_read")
            cw = cost.get("cache_write")
            # Key by the bare model id (last path segment), matching model_price()'s
            # lookup -- models.dev ids for resold open models look like "vendor/model".
            key = str(mid).rsplit("/", 1)[-1].lower()
            models[key] = [
                float(inp),
                float(out),
                float(cr) if isinstance(cr, (int, float)) else 0.0,
                float(cw) if isinstance(cw, (int, float)) else 0.0,
            ]
    if not models:
        raise ValueError("no priced models found in the models.dev response")
    path = dest or price_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "source": url,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": models,
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    invalidate_price_cache()
    return len(models), path


def model_price(name: str) -> tuple[float, float, float, float]:
    # (input, output, cache_read, cache_write) per 1M tokens for the model.
    if is_local_provider(name):
        return (0.0, 0.0, 0.0, 0.0)  # runs on your hardware -- no per-token API bill
    mid = str(name).rsplit("/", 1)[-1].lower()
    cache = _load_price_cache()
    if mid in cache:  # a refreshed models.dev price wins over the embedded snapshot
        return cache[mid]
    if mid in MODEL_PRICE_TABLE:
        return MODEL_PRICE_TABLE[mid]
    return next((tuple(p) for needle, *p in MODEL_PRICE_FALLBACKS if needle in mid), FALLBACK_PRICE)


def api_equivalent_cost(
    name: str, inp: float, out: float, reasoning: float, cache_read: float, cache_write: float
) -> float:
    # What this usage would cost at API list prices. Reasoning tokens bill as
    # output; cache reads/writes at their own discounted/surcharged rates.
    ir, orr, crr, cwr = model_price(name)
    return (inp * ir + (out + reasoning) * orr + cache_read * crr + cache_write * cwr) / 1e6
