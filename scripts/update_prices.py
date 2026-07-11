#!/usr/bin/env python3
"""Refresh the bundled models.dev price catalog (src/opentab/data/models.json).

Runtime OpenTab stays offline and stdlib-only: this dev helper is the release-time
fetcher that snapshots every models.dev provider's list prices into the JSON file
bundled with the package. Run it before cutting a release and commit the result.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from opentab.pricing import MODELS_DEV_URL, prune_models_dev  # noqa: E402


def kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=MODELS_DEV_URL)
    parser.add_argument(
        "--target",
        type=Path,
        default=SRC / "opentab" / "data" / "models.json",
        help="where to write the pruned catalog (default: the bundled snapshot)",
    )
    args = parser.parse_args()

    req = Request(args.url, headers={"User-Agent": "opentab-price-updater/2.0"})
    with urlopen(req, timeout=30) as resp:
        raw = resp.read()
    providers = prune_models_dev(json.loads(raw))
    models = sum(len(p["models"]) for p in providers.values())
    if not models:
        raise SystemExit("no priced models found in the models.dev response")
    payload = {
        "source": args.url,
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "providers": providers,
    }
    # indent=1 + sorted keys: one leaf per line, so the per-release regeneration
    # diffs as changed models rather than one opaque blob.
    text = json.dumps(payload, indent=1, sort_keys=True) + "\n"
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(text)

    deprecated = sum(
        1
        for p in providers.values()
        for m in p["models"].values()
        if m.get("status") == "deprecated"
    )
    print(f"wrote {args.target}")
    print(f"  raw api.json:    {kb(len(raw))}")
    print(f"  pruned catalog:  {kb(len(text))}  (gzipped {kb(len(gzip.compress(text.encode())))})")
    print(f"  providers:       {len(providers)}")
    print(f"  priced models:   {models}  ({deprecated} deprecated)")
    top = sorted(providers.items(), key=lambda kv: -len(kv[1]["models"]))[:8]
    for pid, p in top:
        print(f"    {pid:<28} {len(p['models']):>4} models")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
