"""Rate-independent numeric request buckets; never infer tiers from aggregate tokens."""
from __future__ import annotations

import math

from opentab.accounting.pricing import (
    cache_write_1h_price,
    copilot_cache_write_model,
    copilot_price_estimated,
    is_copilot_model,
    model_price,
    model_price_source,
    model_tiers,
)
from opentab.util import model_row_1h_write, model_row_split

FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write", "cache_write_1h")


def inferred_cache_write(row: dict) -> float:
    """Rate-independent estimate; only individual requests can supply a missing split.

    OpenCode's Copilot Responses adapter loses GPT-5.6+ writes. The deliberately
    approximate estimate treats its remaining input as writes, never changes records,
    and never applies the zero-write test after differently classified calls coalesce.
    """
    if not copilot_cache_write_model(str(row.get("model_name") or "")):
        return 0.0
    if "inferred_cache_write" in row:
        return min(float(row.get("input") or 0), max(0.0, float(row["inferred_cache_write"])))
    if request_context(row) is not None and not row.get("cache_write"):
        return float(row.get("input") or 0)
    return 0.0


def row_estimate_reasons(row: dict) -> list[str]:
    reasons = []
    if copilot_price_estimated(str(row.get("model_name") or "")):
        reasons.append("copilot_rate_fallback")
    buckets = row.get("pricing")
    inferred = (
        sum(b.get("inferred_cache_write", 0) for b in buckets)
        if isinstance(buckets, list)
        else inferred_cache_write(row)
    )
    if inferred:
        reasons.append("cache_write_inferred")
    return reasons


def row_estimate_metadata(row: dict) -> dict:
    name = str(row.get("model_name") or "")
    return {
        "basis": "copilot_list_prices" if is_copilot_model(name) else "recorded_plus_unpriced",
        "price_source": model_price_source(name),
        "approximation_reasons": row_estimate_reasons(row),
    }


def valid_pricing(row: dict) -> bool:
    try:
        return _valid_pricing(row)
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_pricing(row: dict) -> bool:
    if "pricing" not in row:
        return True
    buckets = row["pricing"]
    if not isinstance(buckets, list):
        return False
    totals = {k: [0.0] * 6 for k in ("tok", "unpriced", "root_unpriced")}
    root_totals = [0.0] * 6
    root_complete = True
    numeric_types = (int, float)
    isfinite = math.isfinite
    inferred_fields = (
        ("inferred_cache_write", "tok"),
        ("root_inferred_cache_write", "root_tok"),
        ("unpriced_inferred_cache_write", "unpriced"),
        ("root_unpriced_inferred_cache_write", "root_unpriced"),
    )
    for bucket in buckets:
        if not isinstance(bucket, dict):
            return False
        value = bucket.get("context")
        if value is not None and (
            (
                type(value) not in numeric_types
                and (isinstance(value, bool) or not isinstance(value, numeric_types))
            )
            or not isfinite(value)
            or value < 0
        ):
            return False
        for key in totals:
            tok = bucket.get(key)
            if not isinstance(tok, list) or len(tok) != 6:
                return False
            # Validate and accumulate together, preserving bucket order and float
            # addition semantics without a generator and replacement list per split.
            total = totals[key]
            for i, v in enumerate(tok):
                if (
                    (
                        type(v) not in numeric_types
                        and (isinstance(v, bool) or not isinstance(v, numeric_types))
                    )
                    or not isfinite(v)
                    or v < 0
                ):
                    return False
                total[i] += v
            if tok[5] > tok[4] + 1e-6:
                return False
        tok = bucket["tok"]
        unpriced = bucket["unpriced"]
        root_unpriced = bucket["root_unpriced"]
        root = bucket.get("root_tok")
        root_complete = root_complete and root is not None
        if root is not None:
            if not isinstance(root, list) or len(root) != 6:
                return False
            for i, v in enumerate(root):
                if (
                    (
                        type(v) not in numeric_types
                        and (isinstance(v, bool) or not isinstance(v, numeric_types))
                    )
                    or not isfinite(v)
                    or v < 0
                    or v > tok[i] + 1e-6
                    or root_unpriced[i] > v + 1e-6
                ):
                    return False
                root_totals[i] += v
            if root[5] > root[4] + 1e-6:
                return False
        elif "root_tok" in bucket:
            # Missing legacy ownership is allowed; explicit null was rejected by
            # the inferred-write input bound and remains malformed.
            return False
        for field, key in inferred_fields:
            value = bucket.get(field, 0)
            if (
                (
                    type(value) not in numeric_types
                    and (isinstance(value, bool) or not isinstance(value, numeric_types))
                )
                or not isfinite(value)
                or value < 0
                or value > (bucket[key][0] if key in bucket else 0) + 1e-6
            ):
                return False
        for i in range(6):
            if root_unpriced[i] > unpriced[i] + 1e-6 or unpriced[i] > tok[i] + 1e-6:
                return False
    expected = [*model_row_split(row), model_row_1h_write(row)]
    if any(not math.isfinite(v) or v < 0 for v in expected):
        return False
    if any(abs(a - b) > 1e-6 for a, b in zip(totals["tok"], expected)):
        return False
    for key, prefix in (("unpriced", "unpriced_"), ("root_unpriced", "root_unpriced_")):
        if prefix + "input" in row and any(
            abs(totals[key][i] - float(row.get(prefix + field) or 0)) > 1e-6
            for i, field in enumerate(FIELDS)
        ):
            return False
    if "root_input" in row and (
        not root_complete
        or any(
            abs(root_totals[i] - float(row.get("root_" + field) or 0)) > 1e-6
            for i, field in enumerate(FIELDS)
        )
    ):
        return False
    return True


def request_context(row: dict) -> float | None:
    """Missing explicit context means an aggregate, not a large single request."""
    value = row.get("context_tokens")
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return (
        float(value)
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        else None
    )


def attach_pricing(rows: list[dict], turns, *, per_request: bool = True) -> None:
    """Keep context, token mix, billing and root ownership before calls are rolled up."""
    by_model = {}
    for turn in turns:
        name = turn.get("model_name")
        context = request_context(turn)
        if context is None and per_request and "context_tokens" not in turn:
            context = sum(float(turn.get(k) or 0) for k in ("input", "cache_read", "cache_write"))
            turn["context_tokens"] = context
        tok = [*model_row_split(turn), model_row_1h_write(turn)]
        if not any(tok) and not turn.get("cost"):
            continue
        bucket = by_model.setdefault(name, {}).setdefault(
            context,
            {
                "context": context,
                "tok": [0.0] * 6,
                "unpriced": [0.0] * 6,
                "root_unpriced": [0.0] * 6,
                "root_tok": [0.0] * 6,
                "inferred_cache_write": 0.0,
                "root_inferred_cache_write": 0.0,
                "unpriced_inferred_cache_write": 0.0,
                "root_unpriced_inferred_cache_write": 0.0,
                "root_cost": 0.0,
            },
        )
        inferred = inferred_cache_write(turn)
        bucket["inferred_cache_write"] += inferred
        if not turn.get("depth"):
            bucket["root_inferred_cache_write"] += inferred
            bucket["root_cost"] += float(turn.get("cost") or 0)
        if not turn.get("cost"):
            bucket["unpriced_inferred_cache_write"] += inferred
            if not turn.get("depth"):
                bucket["root_unpriced_inferred_cache_write"] += inferred
        for i, value in enumerate(tok):
            bucket["tok"][i] += value
            if not turn.get("depth"):
                bucket["root_tok"][i] += value
            if not turn.get("cost"):
                bucket["unpriced"][i] += value
                if not turn.get("depth"):
                    bucket["root_unpriced"][i] += value
    for row in rows:
        buckets = list(by_model.get(row.get("model_name"), {}).values())
        # Summary residuals and partial histories remain explicitly unknown. Only use
        # call buckets when they reconcile with every aggregate category/billing split.
        totals = {
            key: [sum(b[key][i] for b in buckets) for i in range(6)]
            for key in ("tok", "unpriced", "root_unpriced")
        }
        expected = {"tok": [*model_row_split(row), model_row_1h_write(row)]}
        for key, prefix in (("unpriced", "unpriced_"), ("root_unpriced", "root_unpriced_")):
            expected[key] = [float(row.get(prefix + field) or 0) for field in FIELDS]
        if any(totals[k][i] > expected[k][i] + 1e-6 for k in totals for i in range(6)):
            continue
        residual = {
            key: [max(0.0, expected[key][i] - totals[key][i]) for i in range(6)] for key in totals
        }
        if any(v for values in residual.values() for v in values):
            buckets.append({"context": None, **residual})
        row["pricing"] = buckets
        if all("root_tok" in b for b in buckets):
            for i, field in enumerate(FIELDS):
                row["root_" + field] = sum(b["root_tok"][i] for b in buckets)
            row["root_cost"] = sum(b["root_cost"] for b in buckets)


def pricing_samples(row: dict, prefix: str = ""):
    key = {
        "": "tok",
        "root_": "root_tok",
        "unpriced_": "unpriced",
        "root_unpriced_": "root_unpriced",
    }[prefix]
    buckets = row.get("pricing")
    if isinstance(buckets, list):
        eligible = copilot_cache_write_model(str(row.get("model_name") or ""))
        for bucket in buckets:
            tok = list(bucket.get(key, [0.0] * 6))
            inferred = (
                min(tok[0], bucket.get(prefix + "inferred_cache_write", 0)) if eligible else 0
            )
            tok[0] -= inferred
            tok[4] += inferred
            yield bucket.get("context"), tok
        return
    if prefix:
        tok = [float(row.get(prefix + field) or 0) for field in FIELDS]
    else:
        tok = [*model_row_split(row), model_row_1h_write(row)]
    if not prefix:
        inferred = inferred_cache_write(row)
        tok[0] -= inferred
        tok[4] += inferred
    yield request_context(row), tok


def estimated_token_split(row: dict) -> tuple:
    tokens = [0.0] * 5
    for _context, tok in pricing_samples(row):
        tokens = [a + b for a, b in zip(tokens, tok)]
    return tuple(tokens)


def row_cost_parts(row: dict, model: str | None = None, prefix: str = "") -> tuple:
    name = model if model is not None else str(row.get("model_name") or "")
    cost = [0.0] * 5
    # Thousands of request contexts usually share only one or two rate bands.
    # Resolve aliases/catalog cards once per used band, not once per request.
    # Keep this memo local so price refreshes cannot leave stale dollar estimates.
    thresholds = tuple(size for size, _price in reversed(model_tiers(name)))
    rates = {}
    long_rates = {}
    for context, tok in pricing_samples(row, prefix):
        if not any(tok[:5]):
            continue
        band = next((size for size in thresholds if context is not None and context > size), None)
        if band not in rates:
            rates[band] = model_price(name, context)
        ir, out, cr, cw = rates[band]
        long = min(max(tok[5], 0.0), tok[4])
        if long and band not in long_rates:
            long_rates[band] = cache_write_1h_price(name, context)
        values = (
            tok[0] * ir,
            tok[1] * out,
            tok[2] * out,
            tok[3] * cr,
            (tok[4] - long) * cw + (long * long_rates[band] if long else 0),
        )
        cost = [a + b / 1e6 for a, b in zip(cost, values)]
    return tuple(cost)


def row_list_cost(row: dict, model: str | None = None, prefix: str = "") -> float:
    return sum(row_cost_parts(row, model, prefix))


def unpriced_cost(row: dict, *, root: bool = False) -> float:
    whole = not root and not row.get("real_cost", row.get("cost")) and "unpriced_input" not in row
    return row_list_cost(row, prefix="" if whole else "root_unpriced_" if root else "unpriced_")


def row_api_cost(row: dict, *, root: bool = False) -> float:
    """One estimate policy: revalue Copilot fully; retain other providers' billing."""
    prefix = "root_" if root else ""
    recorded = float(row.get("real_" + prefix + "cost", row.get(prefix + "cost")) or 0)
    if is_copilot_model(str(row.get("model_name") or "")):
        if root and "root_input" not in row:
            # Older portable summaries lack full paid-root ownership. Keep the
            # available recorded/unpriced split rather than inventing a fraction.
            return recorded + unpriced_cost(row, root=True)
        return row_list_cost(row, prefix=prefix)
    return recorded + float(row.get(prefix + "estimated_cost") or 0) + unpriced_cost(row, root=root)


def row_api_delta(row: dict, *, root: bool = False) -> float:
    prefix = "root_" if root else ""
    recorded = float(row.get("real_" + prefix + "cost", row.get(prefix + "cost")) or 0)
    return row_api_cost(row, root=root) - recorded


def detail_api_cost(row: dict) -> float:
    """Detail rows keep their historical non-Copilot positive-cost behavior."""
    if is_copilot_model(str(row.get("model_name") or "")):
        return row_api_cost(row)
    return (float(row.get("cost") or 0) or row_list_cost(row)) + float(
        row.get("estimated_cost") or 0
    )


def unknown_tier_context(row: dict, model: str | None = None) -> bool:
    name = model if model is not None else str(row.get("model_name") or "")
    return bool(model_tiers(name)) and any(
        context is None and any(tok[:5]) for context, tok in pricing_samples(row)
    )


def node_token_row(node: dict) -> dict:
    return {
        "model_name": node.get("model_name", ""),
        **{field: node.get("tokens_" + field, 0) for field in FIELDS},
        **{key: node[key] for key in ("context_tokens", "inferred_cache_write") if key in node},
    }


def attach_node_pricing(node: dict, turns, *, per_request: bool = True) -> None:
    turns = list(turns)
    models = {}
    for turn in turns:
        name = turn["model_name"]
        row = models.setdefault(name, {"model_name": name})
        row["cost"] = row.get("cost", 0) + float(turn.get("cost") or 0)
        for field in FIELDS:
            value = float(turn.get(field) or 0)
            row[field] = row.get(field, 0) + value
            if not turn.get("cost"):
                row["unpriced_" + field] = row.get("unpriced_" + field, 0) + value
                row["root_unpriced_" + field] = row.get("root_unpriced_" + field, 0) + value
    own = [{**t, "depth": 0} for t in turns]
    rows = list(models.values())
    attach_pricing(rows, own, per_request=per_request)
    total = [sum(row.get(field, 0) for row in rows) for field in FIELDS]
    if all(
        abs(total[i] - float(node.get("tokens_" + field) or 0)) < 1e-6
        for i, field in enumerate(FIELDS)
    ):
        node["model_pricing"] = rows


def node_list_cost(node: dict, target: str | None = None) -> float:
    rows = node.get("model_pricing")
    return (
        sum(row_list_cost(row, target) for row in rows)
        if isinstance(rows, list)
        else row_list_cost(node_token_row(node), target)
    )


def node_api_cost(node: dict) -> float:
    recorded = float(node.get("cost") or 0)
    rows = node.get("model_pricing")
    if isinstance(rows, list):
        if any(is_copilot_model(str(r.get("model_name") or "")) and "cost" not in r for r in rows):
            if all(is_copilot_model(str(r.get("model_name") or "")) for r in rows):
                return sum(row_list_cost(r) for r in rows)
            return recorded + sum(unpriced_cost(r) for r in rows)
        return recorded + sum(row_api_delta(row) for row in rows)
    return detail_api_cost({**node_token_row(node), "cost": recorded})
