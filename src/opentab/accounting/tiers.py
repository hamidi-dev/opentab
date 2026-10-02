"""Rate-independent numeric request buckets; never infer tiers from aggregate tokens."""
from __future__ import annotations

import math

from opentab.accounting.pricing import cache_write_1h_price, model_price, model_tiers
from opentab.util import model_row_1h_write, model_row_split

FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write", "cache_write_1h")


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
    for bucket in buckets:
        if not isinstance(bucket, dict):
            return False
        value = bucket.get("context")
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return False
        for key in totals:
            tok = bucket.get(key)
            if (
                not isinstance(tok, list)
                or len(tok) != 6
                or any(
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    or v < 0
                    for v in tok
                )
            ):
                return False
            if tok[5] > tok[4] + 1e-6:
                return False
            totals[key] = [a + b for a, b in zip(totals[key], tok)]
        if any(
            bucket["root_unpriced"][i] > bucket["unpriced"][i] + 1e-6
            or bucket["unpriced"][i] > bucket["tok"][i] + 1e-6
            for i in range(6)
        ):
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
        if not any(tok):
            continue
        bucket = by_model.setdefault(name, {}).setdefault(
            context,
            {
                "context": context,
                "tok": [0.0] * 6,
                "unpriced": [0.0] * 6,
                "root_unpriced": [0.0] * 6,
            },
        )
        for i, value in enumerate(tok):
            bucket["tok"][i] += value
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


def pricing_samples(row: dict, prefix: str = ""):
    key = {"": "tok", "unpriced_": "unpriced", "root_unpriced_": "root_unpriced"}[prefix]
    buckets = row.get("pricing")
    if isinstance(buckets, list):
        for bucket in buckets:
            yield bucket.get("context"), bucket[key]
        return
    if prefix:
        tok = [float(row.get(prefix + field) or 0) for field in FIELDS]
    else:
        tok = [*model_row_split(row), model_row_1h_write(row)]
    yield request_context(row), tok


def row_cost_parts(row: dict, model: str | None = None, prefix: str = "") -> tuple:
    name = model if model is not None else str(row.get("model_name") or "")
    cost = [0.0] * 5
    for context, tok in pricing_samples(row, prefix):
        ir, out, cr, cw = model_price(name, context)
        long = min(max(tok[5], 0.0), tok[4])
        values = (
            tok[0] * ir,
            tok[1] * out,
            tok[2] * out,
            tok[3] * cr,
            (tok[4] - long) * cw + (long * cache_write_1h_price(name, context) if long else 0),
        )
        cost = [a + b / 1e6 for a, b in zip(cost, values)]
    return tuple(cost)


def row_list_cost(row: dict, model: str | None = None, prefix: str = "") -> float:
    return sum(row_cost_parts(row, model, prefix))


def unpriced_cost(row: dict, *, root: bool = False) -> float:
    whole = not root and not row.get("real_cost", row.get("cost")) and "unpriced_input" not in row
    return row_list_cost(row, prefix="" if whole else "root_unpriced_" if root else "unpriced_")


def unknown_tier_context(row: dict, model: str | None = None) -> bool:
    name = model if model is not None else str(row.get("model_name") or "")
    return bool(model_tiers(name)) and any(
        context is None and any(tok[:5]) for context, tok in pricing_samples(row)
    )


def node_token_row(node: dict) -> dict:
    return {
        "model_name": node.get("model_name", ""),
        **{field: node.get("tokens_" + field, 0) for field in FIELDS},
    }


def attach_node_pricing(node: dict, turns, *, per_request: bool = True) -> None:
    turns = list(turns)
    models = {}
    for turn in turns:
        name = turn["model_name"]
        row = models.setdefault(name, {"model_name": name})
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
        return recorded + sum(row_list_cost(row, prefix="unpriced_") for row in rows)
    return recorded or node_list_cost(node)
