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

from tests._support import copilot_prices, tier_prices


def test_copilot_estimate_reprices_positive_cost_and_preserves_wire_fixture_tokens():
    from opentab.accounting.tiers import row_api_cost

    with copilot_prices() as name:
        turns = [
            _turn(name, 2815, output=9, cost=0.005738),
            _turn(name, 49, read=2812, output=9, cost=0.0007684),
            _turn(name, 49, read=2858, output=9, cost=0.0007776),
            _turn(name, 49, read=2904, output=9, cost=0.0007868),
        ]
        row = _row(name, turns)
        assert abs(row_api_cost(row) - 0.0095518) < 1e-12
        assert abs(row["cost"] - 0.0080708) < 1e-12
        assert row["input"] == 2962 and row["cache_write"] == 0
        assert row_list_cost(row, name) == row_list_cost(row)
        assert valid_pricing(row)


def test_copilot_inference_is_per_request_with_full_paid_root_split():
    from opentab.accounting.tiers import row_api_cost

    with copilot_prices() as name:
        turns = [
            _turn(name, 100, write=0, output=0, cost=9),
            _turn(name, 40, write=60, output=0, cost=8, depth=1),
        ]
        row = _row(name, turns)
        # Both requests have the same context and coalesce, but only the first is inferred.
        assert len(row["pricing"]) == 1
        assert abs(row_api_cost(row) - 0.00048) < 1e-12
        assert abs(row_api_cost(row, root=True) - 0.00025) < 1e-12
        assert row["input"] == 140 and row["cache_write"] == 60
        assert valid_pricing(row)


def test_copilot_inference_excludes_older_gpt_and_other_routes():
    from opentab.accounting.tiers import row_api_cost

    with copilot_prices():
        older = _row(
            "github-copilot/gpt-5.4", [_turn("github-copilot/gpt-5.4", 1000, output=0, cost=5)]
        )
        direct = _row(
            "openai/gpt-5.6-terra", [_turn("openai/gpt-5.6-terra", 1000, output=0, cost=5)]
        )
        assert row_api_cost(older) == 0.002
        assert row_cost_parts(older)[4] == 0
        assert row_api_cost(direct) == 5
        assert row_cost_parts(direct)[4] == 0


def test_copilot_cache_write_generation_gate_includes_newer_gpt_families_only():
    from opentab.accounting.tiers import inferred_cache_write

    for name, expected in (
        ("github-copilot/gpt-5.5", 0),
        ("github-copilot/gpt-5-mini", 0),
        ("github-copilot/claude-sonnet-4.6", 0),
        ("openai/gpt-6.1-sol", 0),
        ("github-copilot/gpt-5.6-terra", 100),
        ("github-copilot/gpt-5-6-sol", 100),
        ("github-copilot/gpt-6-astra", 100),
        ("github-copilot/gpt-6.1-sol", 100),
    ):
        row = {"model_name": name, "input": 100, "context_tokens": 100, "cache_write": 0}
        assert inferred_cache_write(row) == expected, name
        row["cache_write"] = 1
        assert inferred_cache_write(row) == 0, name


def test_copilot_portable_metadata_rejects_inconsistent_root_and_inference_splits():
    import copy

    with copilot_prices() as name:
        row = _row(name, [_turn(name, 100, output=0, cost=1)])
        bad = copy.deepcopy(row)
        bad["pricing"][0]["root_tok"][0] = 99
        bad["pricing"][0]["root_inferred_cache_write"] = 99
        assert not valid_pricing(bad)
        for value in (-1, 101, float("nan"), True):
            bad = copy.deepcopy(row)
            bad["pricing"][0]["inferred_cache_write"] = value
            assert not valid_pricing(bad)
        # Targeting another model retains the producing model's inferred allocation.
        assert row_cost_parts(row, "openai/gpt-5.6-terra")[0] == 0


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


def test_pricing_catalog_work_is_bounded_by_rate_bands_not_request_count():
    from unittest.mock import patch

    from opentab.accounting import tiers

    with tier_prices() as name:
        turns = [_turn(name, n, write=1000) for n in range(100000, 601000, 1000)]
        for turn in turns:
            turn["cache_write_1h"] = 500
        row = _row(name, turns)
        expected = row_cost_parts(row)
        with patch.object(tiers, "model_price", wraps=tiers.model_price) as prices, patch.object(
            tiers, "cache_write_1h_price", wraps=tiers.cache_write_1h_price
        ) as long_prices:
            assert row_cost_parts(row) == expected
            assert prices.call_count <= 3, prices.call_count
            assert long_prices.call_count <= 3, long_prices.call_count


def test_empty_pricing_splits_do_not_resolve_catalog_rates():
    from unittest.mock import patch

    from opentab.accounting import tiers

    with copilot_prices() as name:
        row = _row(name, [_turn(name, n, cost=1) for n in range(1000, 1100)])
        with patch.object(tiers, "model_price", side_effect=AssertionError("empty split lookup")):
            assert row_cost_parts(row, prefix="unpriced_") == (0, 0, 0, 0, 0)


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
