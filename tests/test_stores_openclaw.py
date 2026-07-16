"""The OpenClaw gateway backend (stores/openclaw.py)."""

import json
import os
import tempfile

import opentab as ot

from tests._support import OCL_SID, _ocl_args, _ocl_msg, _ocl_user, _ocl_write


def _ocl_model_snapshot(provider, model_id, mid="mc1", ts="2026-04-27T15:59:00.000Z"):
    return {
        "type": "custom",
        "customType": "model-snapshot",
        "data": {"provider": provider, "modelApi": "x", "modelId": model_id},
        "id": mid,
        "timestamp": ts,
    }


def _ocl_oauth(root, profiles):
    # profiles: {provider: mode}; written in openclaw.json's auth.profiles shape.
    data = {
        "auth": {
            "profiles": {f"{p}:default": {"mode": m, "provider": p} for p, m in profiles.items()}
        }
    }
    with open(os.path.join(root, "openclaw.json"), "w") as fh:
        json.dump(data, fh)


def test_openclaw_store_meters_cost_splits_cache_and_uses_agent_as_project():
    with tempfile.TemporaryDirectory() as root:
        # A direct-Anthropic-key turn: provider isn't OAuth/plan -> metered, real spend.
        rows = [
            _ocl_user("summarize the budget"),
            _ocl_msg(
                "claude-opus-4-6", 1660, 55, cache_read=108928, cost=0.0228375, provider="anthropic"
            ),
        ]
        _ocl_write(root, "finance-os", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is True  # a metered cost -> real spend
        wfs = store.workflows()
        assert len(wfs) == 1
        w = wfs[0]
        assert w.id == OCL_SID
        assert w.source == "OpenClaw"
        assert w.subagents == 0
        assert w.directory == "finance-os"  # the agent name, not the gateway cwd
        assert w.title == "summarize the budget"
        assert w.total_cost == 0.022838  # recorded spend (rounded to 6dp), not estimated
        assert w.total_tokens == 110643  # 1660 + 55 + 108928 (input not reduced)
        assert w.unpriced_tokens == 0  # priced -> nothing left for "$" to estimate

        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["model_name"] == "anthropic/claude-opus-4-6"  # bare id -> provider-prefixed
        assert row["input"] == 1660 and row["cache_read"] == 108928
        assert row["unpriced_input"] == 0  # priced row -> unpriced split zeroed

        nodes = store.workflow_nodes(OCL_SID)
        assert len(nodes) == 1 and nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["cost"] == 0.022838


def test_openclaw_store_dedupes_messages_across_archive_files():
    with tempfile.TemporaryDirectory() as root:
        a = _ocl_msg("claude-sonnet-4-5", 100, 50, cost=0.01, provider="anthropic", mid="dupe")
        # Same assistant step lives in the live file and a .jsonl.reset archive -> count once.
        _ocl_write(root, "main", OCL_SID, [_ocl_user("go"), a])
        _ocl_write(root, "main", OCL_SID, [a], suffix=".jsonl.reset.2026-03-20T06-34-44.520Z")
        store = ot.OpenClawStore(root, _ocl_args())
        wfs = store.workflows()
        assert len(wfs) == 1  # the two files key to one session id
        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["runs"] == 1  # the archived duplicate was not double-counted
        assert row["tokens_total"] == 150
        assert abs(row["cost"] - 0.01) < 1e-9


def test_openclaw_store_unpriced_session_estimates_under_dollar():
    with tempfile.TemporaryDirectory() as root:
        # Usage but no recorded cost -> records_cost False, tokens unpriced for the "$" view.
        rows = [
            _ocl_user("estimate me"),
            _ocl_msg("claude-sonnet-4-5", 1000, 500, cache_read=200, provider="anthropic"),
        ]
        _ocl_write(root, "homelab", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is False  # no recorded cost anywhere
        w = store.workflows()[0]
        assert w.total_cost == 0.0
        assert w.total_tokens == w.unpriced_tokens == 1700
        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["unpriced_input"] == 1000 and row["unpriced_cache_read"] == 200
        est = ot.api_equivalent_cost("anthropic/claude-sonnet-4-5", 1000, 500, 0, 200, 0)
        assert est > 0


def test_openclaw_store_falls_back_to_total_tokens():
    with tempfile.TemporaryDirectory() as root:
        # Only totalTokens recorded (no input/output split) -> back-fills as output.
        a = {
            "type": "message",
            "id": "a1",
            "timestamp": "2026-04-27T16:00:16.401Z",
            "message": {"role": "assistant", "model": "gpt-5.2", "usage": {"totalTokens": 333}},
        }
        _ocl_write(root, "main", OCL_SID, [_ocl_user("hi"), a])
        row = next(
            r
            for r in ot.OpenClawStore(root, _ocl_args()).model_breakdown()
            if r["root_id"] == OCL_SID
        )
        assert row["output"] == 333 and row["tokens_total"] == 333
        assert row["model_name"] == "openai/gpt-5.2"  # gpt -> openai/


def test_openclaw_store_oauth_route_cost_is_not_real_spend():
    with tempfile.TemporaryDirectory() as root:
        # openclaw.json marks openai-codex as an OAuth (ChatGPT-plan) login -> subscription.
        # OpenClaw still writes a list-price cost, but it is NOT what the user pays.
        _ocl_oauth(root, {"openai-codex": "oauth", "anthropic": "token"})
        rows = [
            _ocl_user("whats this repo about?"),
            _ocl_msg(
                "gpt-5.3-codex",
                12594,
                57,
                cost=0.0228375,
                provider="openai-codex",
                api="openai-codex-responses",
            ),
        ]
        _ocl_write(root, "main", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is False  # OAuth route -> nothing metered
        w = store.workflows()[0]
        assert w.total_cost == 0.0  # the list-price cost is not real spend
        assert w.total_tokens == w.unpriced_tokens == 12651  # all estimable under "$"
        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["cost"] == 0.0 and row["unpriced_input"] == 12594
        assert ot.api_equivalent_cost("openai/gpt-5.3-codex", 12594, 57, 0, 0, 0) > 0


def test_openclaw_store_copilot_marker_is_subscription_without_openclaw_json():
    with tempfile.TemporaryDirectory() as root:
        # github-copilot logs in with a static token (mode != "oauth"), so the OAuth probe
        # misses it -- the "copilot" provider marker catches it instead. No openclaw.json.
        rows = [
            _ocl_user("draft a PR"),
            _ocl_msg(
                "gpt-4o", 800, 120, cost=0.005, provider="github-copilot", api="openai-completions"
            ),
        ]
        _ocl_write(root, "github-os", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is False  # copilot is a plan route -> not real spend
        w = store.workflows()[0]
        assert w.total_cost == 0.0
        assert w.total_tokens == w.unpriced_tokens == 920
        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["cost"] == 0.0 and row["unpriced_output"] == 120


def test_openclaw_store_model_snapshot_supplies_model_and_provider():
    with tempfile.TemporaryDirectory() as root:
        # A model-snapshot sets the current model+provider; the following assistant message
        # omits both, so it inherits them -- model for the label, provider for billing.
        rows = [
            _ocl_model_snapshot("openai-codex", "gpt-5.2"),
            _ocl_user("go"),
            _ocl_msg(None, 2000, 80, cost=0.011, provider=None),  # codex marker -> subscription
        ]
        _ocl_write(root, "main", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is False  # provider inherited from the snapshot -> codex plan
        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["model_name"] == "openai/gpt-5.2"  # model id from the snapshot
        assert row["cost"] == 0.0 and row["unpriced_input"] == 2000


def test_openclaw_store_mixes_metered_and_subscription_in_one_session():
    with tempfile.TemporaryDirectory() as root:
        # One session, two routes: anthropic (metered, real cost) + a codex turn
        # (subscription via the provider marker). Only the anthropic spend is real.
        rows = [
            _ocl_user("go"),
            _ocl_msg("claude-opus-4-6", 8000, 300, cost=0.0071, provider="anthropic", mid="m1"),
            _ocl_msg("gpt-5.3-codex", 5000, 200, cost=0.03, provider="openai-codex", mid="m2"),
        ]
        _ocl_write(root, "main", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is True  # the anthropic turn is genuinely metered
        w = store.workflows()[0]
        assert w.total_cost == 0.0071  # anthropic only; the codex $0.03 is excluded
        assert w.total_tokens == 13500  # 8300 + 5200
        assert w.unpriced_tokens == 5200  # just the codex (subscription) turn
        rows_out = {r["model_name"]: r for r in store.model_breakdown() if r["root_id"] == OCL_SID}
        assert rows_out["anthropic/claude-opus-4-6"]["unpriced_input"] == 0  # metered -> priced
        assert rows_out["openai/gpt-5.3-codex"]["cost"] == 0.0  # subscription -> no real cost
        assert rows_out["openai/gpt-5.3-codex"]["unpriced_input"] == 5000


def test_openclaw_turns_timeline_groups_by_prompt():
    with tempfile.TemporaryDirectory() as root:
        rows = [
            _ocl_user("build the dashboard", mid="u1", ts="2026-04-27T16:00:00.000Z"),
            _ocl_msg(
                "claude-opus-4-6",
                100,
                40,
                cost=0.02,
                provider="anthropic",
                mid="a1",
                ts="2026-04-27T16:00:16.401Z",
            ),
            _ocl_msg(
                "claude-opus-4-6",
                50,
                20,
                cost=0.01,
                provider="anthropic",
                mid="a2",
                ts="2026-04-27T16:01:00.000Z",
            ),
        ]
        _ocl_write(root, "finance-os", "ses-t1", rows)
        store = ot.OpenClawStore(root, _ocl_args())
        store.workflows()
        assert store.supports_turns("ses-t1")
        t = store.message_timeline("ses-t1")
        assert len(t) == 2  # chronological, both under the one prompt
        assert [r["prompt_title"] for r in t] == ["build the dashboard"] * 2
        assert t[0]["cost"] == 0.02 and t[1]["cost"] == 0.01  # metered: real spend
        assert t[0]["model_name"] == "anthropic/claude-opus-4-6"
        assert t[0]["time"] <= t[1]["time"] and t[0]["time"].startswith("2026-04-27")
