from opentab.accounting.tiers import (
    FIELDS,
    attach_node_pricing,
    attach_pricing,
    node_api_cost,
    node_list_cost,
    row_cost_parts,
    row_list_cost,
    unknown_tier_context,
    unpriced_cost,
    valid_pricing,
)
from opentab.util import tool_rows_from_turns

from tests._support import tier_prices


def _turn(name, inp, *, read=0, write=0, output=1000, cost=0, depth=0):
    return {
        "model_name": name,
        "input": inp,
        "cache_read": read,
        "cache_write": write,
        "output": output,
        "cost": cost,
        "depth": depth,
        "tools": ["Bash", "Read"],
    }


def _row(name, turns):
    row = {"model_name": name, "cost": sum(t["cost"] for t in turns)}
    for field in FIELDS:
        row[field] = sum(t.get(field, 0) for t in turns)
        row["unpriced_" + field] = sum(t.get(field, 0) for t in turns if not t["cost"])
        row["root_unpriced_" + field] = sum(
            t.get(field, 0) for t in turns if not t["cost"] and not t["depth"]
        )
    attach_pricing([row], turns)
    return row


def test_request_buckets_do_not_price_small_calls_as_one_large_context():
    with tier_prices() as name:
        turns = [_turn(name, 200000), _turn(name, 200000)]
        row = _row(name, turns)
        assert abs(row_list_cost(row) - 1.64) < 1e-9
        assert valid_pricing(row) and not unknown_tier_context(row)
        assert len(row["pricing"]) == 1  # identical contexts coalesce without changing eligibility


def test_mixed_billing_and_children_retain_independent_context_tiers():
    with tier_prices() as name:
        turns = [
            _turn(name, 100000, cost=7),
            _turn(name, 1000, read=299000),
            _turn(name, 1000, write=299000, depth=1),
        ]
        row = _row(name, turns)
        assert abs(unpriced_cost(row) - (0.2772 + 3.028)) < 1e-9
        assert abs(unpriced_cost(row, root=True) - 0.2772) < 1e-9
        assert abs(row_list_cost(row) - (0.42 + 0.2772 + 3.028)) < 1e-9
        assert valid_pricing(row)


def test_summary_context_is_unknown_and_never_inferred_from_monthly_tokens():
    with tier_prices() as name:
        row = {"model_name": name, "input": 600000, "output": 1000}
        assert unknown_tier_context(row)
        assert abs(row_list_cost(row) - 2.42) < 1e-9
        row["context_tokens"] = 600000
        assert abs(row_list_cost(row) - 7.24) < 1e-9
        assert not unknown_tier_context(row)


def test_tool_shares_use_the_original_request_context_not_the_fractional_share():
    with tier_prices() as name:
        turns = [_turn(name, 300000)]
        _row(name, turns)
        tools = tool_rows_from_turns(turns)
        assert all(abs(row_list_cost(t) - 1.215) < 1e-9 for t in tools)
        assert all(valid_pricing(t) for t in tools)


def test_node_cost_and_target_keep_request_tiers_and_paid_usage_separate():
    with tier_prices() as name:
        turns = [_turn(name, 100000, cost=7), _turn(name, 300000)]
        node = {
            "cost": 7,
            "model_name": name,
            **{"tokens_" + k: sum(t.get(k, 0) for t in turns) for k in FIELDS},
        }
        attach_node_pricing(node, turns)
        assert abs(node_api_cost(node) - 9.43) < 1e-9
        assert abs(node_list_cost(node, name) - 2.85) < 1e-9


def test_malformed_or_inconsistent_portable_buckets_are_rejected():
    with tier_prices() as name:
        row = _row(name, [_turn(name, 300000)])
        assert valid_pricing(row)
        row["pricing"][0]["context"] = float("nan")
        assert not valid_pricing(row)
        row["pricing"][0]["context"] = 300000
        row["pricing"][0]["tok"][0] -= 1
        assert not valid_pricing(row)


def test_token_cost_parts_include_reasoning_at_the_selected_output_rate():
    with tier_prices() as name:
        row = {
            "model_name": name,
            "context_tokens": 300000,
            "input": 1000,
            "cache_read": 299000,
            "output": 1000,
            "reasoning": 100,
        }
        assert row_cost_parts(row) == (0.008, 0.03, 0.003, 0.2392, 0)
