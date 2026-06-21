#!/usr/bin/env python3
"""Refresh OpenTab's embedded API list-price table from models.dev.

Runtime OpenTab stays offline and stdlib-only. This dev helper is the one place
that touches the network, then rewrites the generated block in
../src/opentab/pricing.py.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

MODELS_DEV_URL = "https://models.dev/api.json"
DEFAULT_PROVIDERS = ("anthropic", "openai", "google")
BEGIN = "# ===== BEGIN GENERATED PRICES (scripts/update_prices.py) ====="
END = "# ===== END GENERATED PRICES ====="


def fetch_models(url: str) -> dict[str, Any]:
    req = Request(url, headers={"User-Agent": "opentab-price-updater/1.0"})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def as_price(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def fmt(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if "." in text else f"{text}.0"


def collect_prices(
    data: dict[str, Any], providers: tuple[str, ...]
) -> dict[str, tuple[float, float, float, float]]:
    prices: dict[str, tuple[float, float, float, float]] = {}
    for provider in providers:
        provider_data = data.get(provider)
        if not isinstance(provider_data, dict):
            raise SystemExit(f"provider not found in models.dev data: {provider}")
        models = provider_data.get("models", {})
        if not isinstance(models, dict):
            raise SystemExit(f"unexpected models.dev shape for provider: {provider}")
        for model_id, model in models.items():
            if not isinstance(model, dict):
                continue
            cost = model.get("cost", {})
            if not isinstance(cost, dict):
                continue
            inp = as_price(cost.get("input"))
            out = as_price(cost.get("output"))
            if inp is None or out is None:
                continue
            cache_read = as_price(cost.get("cache_read")) or 0.0
            cache_write = as_price(cost.get("cache_write")) or 0.0
            prices[str(model_id).lower()] = (inp, out, cache_read, cache_write)
    return dict(sorted(prices.items()))


def render_table(
    prices: dict[str, tuple[float, float, float, float]], providers: tuple[str, ...]
) -> str:
    today = datetime.now(UTC).date().isoformat()
    lines = [
        BEGIN,
        f"# Generated from models.dev on {today} for providers: {', '.join(providers)}.",
        "MODEL_PRICE_TABLE: dict[str, tuple[float, float, float, float]] = {",
    ]
    for model_id, (inp, out, cache_read, cache_write) in prices.items():
        lines.append(
            f'    "{model_id}": ({fmt(inp)}, {fmt(out)}, {fmt(cache_read)}, {fmt(cache_write)}),'
        )
    lines += ["}", END]
    return "\n".join(lines)


def replace_block(path: Path, block: str) -> None:
    text = path.read_text()
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise SystemExit(f"expected exactly one generated price block in {path}")
    start = text.index(BEGIN)
    end_start = text.find(END, start + len(BEGIN))
    if end_start == -1:
        raise SystemExit(f"generated price markers are out of order in {path}")
    end = end_start + len(END)
    path.write_text(text[:start] + block + text[end:])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=MODELS_DEV_URL)
    parser.add_argument(
        "--provider",
        action="append",
        choices=DEFAULT_PROVIDERS,
        help="provider to embed; repeatable (default: anthropic, openai, google)",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "src" / "opentab" / "pricing.py",
    )
    args = parser.parse_args()

    providers = tuple(args.provider or DEFAULT_PROVIDERS)
    prices = collect_prices(fetch_models(args.url), providers)
    if not prices:
        raise SystemExit("no priced models found")
    replace_block(args.target, render_table(prices, providers))
    print(f"embedded {len(prices)} model prices into {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
