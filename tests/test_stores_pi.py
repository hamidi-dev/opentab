"""The pi-agent backend: metered vs subscription routes (stores/pi.py)."""

import json
import os
import tempfile

import opentab as ot

from tests._support import PI_SID, _pi_args, _pi_assistant, _pi_session, _pi_user, _pi_write


def test_pi_store_meters_cost_splits_cache_and_rolls_up_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        # Session ran in <repo>/sub; cwd comes from the `session` record and folds to root.
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        # pi records a real per-message cost -> metered; tokens are Anthropic-style
        # (input excludes the cached read, so input stays 339, never subtracted).
        rows = [
            _pi_session(PI_SID, sub),
            _pi_user("hi"),
            _pi_assistant("moonshotai/kimi-k2.6", 339, 33, cache_read=768, cost=0.00048495),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        assert store.records_cost is True  # a recorded cost -> metered
        wfs = store.workflows()
        assert len(wfs) == 1
        w = wfs[0]
        assert w.id == PI_SID
        assert w.source == "Pi"
        assert w.subagents == 0
        assert w.directory == repo  # folded to the git root, not bare "sub"
        assert w.title == "hi"  # first user text
        assert w.created_at.startswith("2026-05-15")
        assert w.total_cost == 0.000485  # recorded spend (rounded to 6dp), not estimated
        assert w.total_tokens == 1140  # 339 + 33 + 768 (+0)
        assert w.unpriced_tokens == 0  # priced -> nothing left for "$" to estimate

        row = next(r for r in store.model_breakdown() if r["root_id"] == PI_SID)
        assert row["model_name"] == "moonshotai/kimi-k2.6"  # used verbatim (already prefixed)
        assert row["input"] == 339 and row["cache_read"] == 768  # input not reduced
        assert row["unpriced_input"] == 0  # priced row -> unpriced split zeroed

        nodes = store.workflow_nodes(PI_SID)
        assert len(nodes) == 1 and nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["cost"] == 0.000485


def test_pi_store_dedupes_assistant_messages_by_id():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        a = _pi_assistant("anthropic/claude-sonnet-4", 100, 50, cost=0.01, mid="dupe")
        rows = [_pi_session(PI_SID, cwd), _pi_user("go"), a, dict(a)]  # same id twice
        _pi_write(root, "--proj--", PI_SID, rows)
        row = next(
            r for r in ot.PiStore(root, _pi_args()).model_breakdown() if r["root_id"] == PI_SID
        )
        assert row["runs"] == 1  # the duplicate assistant step was not double-counted
        assert row["tokens_total"] == 150
        assert abs(row["cost"] - 0.01) < 1e-9


def test_pi_store_unpriced_session_estimates_under_dollar():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # A subscription-route session: usage but no cost -> records_cost False, the tokens
        # stay unpriced so the "$" what-if estimates them at list price.
        rows = [
            _pi_session(PI_SID, cwd),
            _pi_user("estimate me"),
            _pi_assistant("anthropic/claude-sonnet-4", 1000, 500, cache_read=200),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        assert store.records_cost is False  # no recorded cost anywhere
        w = store.workflows()[0]
        assert w.total_cost == 0.0
        assert w.total_tokens == w.unpriced_tokens == 1700
        row = next(r for r in store.model_breakdown() if r["root_id"] == PI_SID)
        assert row["unpriced_input"] == 1000 and row["unpriced_cache_read"] == 200
        est = ot.api_equivalent_cost("anthropic/claude-sonnet-4", 1000, 500, 0, 200, 0)
        assert est > 0


def test_pi_store_falls_back_to_total_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # Only totalTokens recorded (no input/output split) -> back-fills as output.
        a = {
            "type": "message",
            "id": "a1",
            "timestamp": "2026-05-15T07:32:36.257Z",
            "message": {
                "role": "assistant",
                "model": "openai/gpt-5",
                "usage": {"totalTokens": 333},
            },
        }
        _pi_write(root, "--proj--", PI_SID, [_pi_session(PI_SID, cwd), a])
        row = next(
            r for r in ot.PiStore(root, _pi_args()).model_breakdown() if r["root_id"] == PI_SID
        )
        assert row["output"] == 333 and row["tokens_total"] == 333
        assert row["model_name"] == "openai/gpt-5"


def test_pi_store_subscription_route_cost_is_not_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # auth.json marks openai-codex as an OAuth (ChatGPT-plan) login -> subscription.
        # pi still writes a list-price cost, but it is NOT what the user pays, so it must be
        # dropped (tokens unpriced, estimated under "$"), not counted as real spend.
        with open(os.path.join(tmp, "auth.json"), "w") as fh:
            json.dump({"openai-codex": {"type": "oauth", "access": "x"}}, fh)
        rows = [
            _pi_session(PI_SID, cwd),
            _pi_user("whats the repo about?"),
            _pi_assistant(
                "gpt-5.5",
                8289,
                231,
                cost=0.048375,
                provider="openai-codex",
                api="openai-codex-responses",
            ),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        assert store.records_cost is False  # subscription-only setup -> nothing metered
        w = store.workflows()[0]
        assert w.total_cost == 0.0  # the $0.048 list-price cost is not real spend
        assert w.total_tokens == w.unpriced_tokens == 8520  # all of it estimable under "$"
        row = next(r for r in store.model_breakdown() if r["root_id"] == PI_SID)
        assert row["cost"] == 0.0 and row["unpriced_input"] == 8289
        est = ot.api_equivalent_cost("openai/gpt-5.5", 8289, 231, 0, 0, 0)
        assert est > 0  # the "$" view still estimates the plan usage


def test_pi_store_mixes_metered_and_subscription_in_one_session():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # One session, two routes: openrouter (metered, real cost) + a codex turn
        # (subscription, recognized by the provider marker -- no auth.json needed). Only
        # the openrouter spend is real; the codex tokens are unpriced.
        rows = [
            _pi_session(PI_SID, cwd),
            _pi_user("go"),
            _pi_assistant(
                "moonshotai/kimi-k2.6", 8000, 300, cost=0.0071, provider="openrouter", mid="m1"
            ),
            _pi_assistant("gpt-5.5", 5000, 200, cost=0.03, provider="openai-codex", mid="m2"),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        assert store.records_cost is True  # the openrouter turn is genuinely metered
        w = store.workflows()[0]
        assert w.total_cost == 0.0071  # openrouter only; the codex $0.03 is excluded
        assert w.total_tokens == 13500  # 8300 + 5200
        assert w.unpriced_tokens == 5200  # just the codex (subscription) turn
        rows_out = {r["model_name"]: r for r in store.model_breakdown() if r["root_id"] == PI_SID}
        assert rows_out["moonshotai/kimi-k2.6"]["unpriced_input"] == 0  # metered -> priced
        assert rows_out["gpt-5.5"]["cost"] == 0.0  # subscription -> no real cost
        assert rows_out["gpt-5.5"]["unpriced_input"] == 5000


def test_pi_turns_timeline_groups_by_prompt_and_meters_cost():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _pi_session(PI_SID, cwd, ts="2026-05-15T07:32:15.949Z"),
            _pi_user("first ask", mid="u1", ts="2026-05-15T07:32:20.000Z"),
            _pi_assistant(
                "anthropic/claude-sonnet-4",
                100,
                50,
                cost=0.01,
                mid="a1",
                ts="2026-05-15T07:32:30.000Z",
            ),
            _pi_user("second ask\nwith detail", mid="u2", ts="2026-05-15T07:33:00.000Z"),
            _pi_assistant(
                "openai/gpt-5.2",
                10,
                5,
                cost=0.5,
                provider="openai-codex",  # plan route: its cost is an estimate, not spend
                mid="a2",
                ts="2026-05-15T07:33:10.000Z",
            ),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        store.workflows()
        assert store.supports_turns(PI_SID)
        t = store.message_timeline(PI_SID)
        assert [r["prompt_title"] for r in t] == ["first ask", "second ask with detail"]
        assert t[1]["prompt_full"] == "second ask\nwith detail"  # raw, line breaks kept
        assert t[0]["cost"] == 0.01 and t[0]["model_name"] == "anthropic/claude-sonnet-4"
        assert t[1]["cost"] == 0.0  # subscription turn stays $0 (the "$" view estimates)
        assert t[0]["tokens_total"] == 150 and t[0]["time"].startswith("2026-05-15")


def test_pi_tool_breakdown_splits_metered_cost_across_tool_calls():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _pi_session(PI_SID, cwd),
            # Metered step calling two tools: cost and tokens split evenly.
            _pi_assistant(
                "anthropic/claude-sonnet-4",
                100,
                50,
                cost=0.01,
                mid="a1",
                tools=["bash", "read"],
            ),
            # Subscription step: stays $0 so the "$" view estimates it.
            _pi_assistant(
                "openai/gpt-5.2",
                10,
                5,
                cost=0.5,
                provider="openai-codex",
                mid="a2",
                ts="2026-05-15T07:33:10.000Z",
                tools=["edit"],
            ),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        store.workflows()
        assert store.supports_tools(PI_SID)
        rows = {r["tool"]: r for r in store.tool_breakdown(PI_SID)}
        assert rows["bash"]["tokens_total"] == 75 and rows["read"]["tokens_total"] == 75
        assert abs(rows["bash"]["cost"] - 0.005) < 1e-9  # the metered cost, split
        assert rows["edit"]["cost"] == 0.0  # plan route: estimate, not spend
