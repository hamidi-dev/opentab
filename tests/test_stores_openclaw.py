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


def _ocl_result(call_id, text, mid, ts):
    return {
        "type": "message",
        "id": mid,
        "timestamp": ts,
        "message": {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": "read",
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "timestamp": 0,
        },
    }


def test_openclaw_store_ended_at_reflects_the_latest_assistant_reply():
    with tempfile.TemporaryDirectory() as root:
        rows = [
            _ocl_user("go", ts="2026-04-27T16:00:00.000Z"),
            _ocl_msg(
                "claude-opus-4-6",
                100,
                40,
                cost=0.02,
                provider="anthropic",
                mid="a1",
                ts="2026-04-27T16:30:00.000Z",  # the latest activity in the session
            ),
        ]
        _ocl_write(root, "finance-os", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        w = store.workflows()[0]

        # _fmt_epoch renders in the system's local TZ, so compare against its own
        # conversion of each raw timestamp rather than a hardcoded wall-clock string.
        assert w.created_at == store._fmt_epoch(store._epoch("2026-04-27T16:00:00.000Z"))
        assert w.ended_at == store._fmt_epoch(store._epoch("2026-04-27T16:30:00.000Z"))


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


def test_openclaw_json_is_fingerprinted_so_a_login_change_invalidates_the_warm_cache():
    with tempfile.TemporaryDirectory() as root:
        rows = [_ocl_msg("model-x", 1000, 500, cost=1.23, provider="acme-cloud")]
        _ocl_write(root, "bot", OCL_SID, rows)
        conf = os.path.join(root, "openclaw.json")
        assert conf in ot.OpenClawStore(root, _ocl_args()).cache_inputs()

        args = type("A", (), {"demo": False, "no_cache": False})()
        cold = ot.CachedStore(ot.OpenClawStore(root, _ocl_args()), "openclaw|" + root, args)
        assert cold.workflows()[0].total_cost == 1.23 and cold.records_cost is True
        cold.model_breakdown()  # what App's deferred scan does -- this writes the cache
        warm = ot.CachedStore(ot.OpenClawStore(root, _ocl_args()), "openclaw|" + root, args)
        assert warm.workflows() and warm.served_from_cache  # unchanged corpus -> a hit

        _ocl_oauth(root, {"acme-cloud": "oauth"})
        after = ot.CachedStore(ot.OpenClawStore(root, _ocl_args()), "openclaw|" + root, args)
        w = after.workflows()[0]
        assert after.served_from_cache is False  # the login change misses the fingerprint
        assert w.total_cost == 0.0 and w.unpriced_tokens == 1500
        assert after.records_cost is False  # and the whole frame flips to ESTIMATED


def test_openclaw_reload_re_reads_the_login_state():
    with tempfile.TemporaryDirectory() as root:
        _ocl_write(
            root, "bot", OCL_SID, [_ocl_msg("model-x", 1000, 500, cost=1.23, provider="acme-cloud")]
        )
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.workflows()[0].total_cost == 1.23
        _ocl_oauth(root, {"acme-cloud": "oauth"})
        assert store.workflows()[0].total_cost == 0.0  # reload, same instance


def test_openclaw_survives_junk_lines_that_are_valid_json_but_not_objects():
    with tempfile.TemporaryDirectory() as root:
        d = os.path.join(root, "agents", "bot", "sessions")
        os.makedirs(d)
        with open(os.path.join(d, f"{OCL_SID}.jsonl"), "w") as fh:
            fh.write('["cost"]\n')
            fh.write('["provider"]\n')
            fh.write(
                '{"type": "message", "id": "a1", "timestamp": "2026-04-27T16:00:16.401Z", '
                '"message": {"role": "assistant", "model": "model-x", "provider": '
                '"openrouter", "usage": {"input": 1e400, "output": 50, "cost": '
                '{"total": 1e400}}}}\n'
            )
            fh.write(
                json.dumps(
                    _ocl_msg(
                        "model-x",
                        100,
                        50,
                        cost=0.5,
                        provider="openrouter",
                        mid="a2",
                        ts="2026-04-27T16:01:00.000Z",
                    )
                )
                + "\n"
            )
        assert ot.OpenClawStore(root, _ocl_args()).records_cost is True  # the probe path
        w = ot.OpenClawStore(root, _ocl_args()).workflows()
        assert len(w) == 1
        assert w[0].total_tokens == 200  # inf -> 0, both records survive
        assert w[0].total_cost == 0.5  # and no inf reaches a total


def test_openclaw_turns_name_the_tools_each_step_called():
    with tempfile.TemporaryDirectory() as root:
        _ocl_write(
            root,
            "clawd",
            OCL_SID,
            [
                _ocl_user("do the thing", ts="2026-04-27T16:00:00.000Z"),
                _ocl_msg(
                    "model-x",
                    100,
                    10,
                    mid="a1",
                    ts="2026-04-27T16:00:10.000Z",
                    tools=["bash", "bash", "read"],
                ),
                _ocl_msg(
                    "model-x", 200, 20, mid="a2", ts="2026-04-27T16:00:20.000Z", tools=["edit"]
                ),
                _ocl_msg("model-x", 300, 30, mid="a3", ts="2026-04-27T16:00:30.000Z"),
            ],
        )
        st = ot.OpenClawStore(root, _ocl_args())
        rows = st.message_timeline(OCL_SID)
        # In call order, duplicates kept: two bash calls are two calls, two shares.
        assert [r["tools"] for r in rows] == [["bash", "bash", "read"], ["edit"], []]
        assert st.supports_tools(OCL_SID) is True

        # The step's tokens split evenly across its calls: a1's 110 over three calls
        # gives bash 2/3 and read 1/3; a3 called nothing and contributes nowhere.
        tools = {r["tool"]: r for r in st.tool_breakdown(OCL_SID)}
        assert set(tools) == {"bash", "read", "edit"}
        assert tools["bash"]["calls"] == 2
        assert round(tools["bash"]["tokens_total"]) == round(110 * 2 / 3)
        assert round(tools["read"]["tokens_total"]) == round(110 / 3)
        assert tools["edit"]["tokens_total"] == 220
        # "tokens in steps that used this tool" -- so the total is the tool-calling
        # steps' tokens (110 + 220), never the session's.
        assert round(sum(r["tokens_total"] for r in tools.values())) == 330


def test_openclaw_turn_content_pairs_reused_call_ids_causally():
    with tempfile.TemporaryDirectory() as root:
        first = _ocl_msg("model-x", 100, 10, mid="a1")
        first["message"]["content"] = [
            {"type": "thinking", "thinking": "Inspect the first file."},
            {"type": "text", "text": "Reading one."},
            {
                "type": "toolCall",
                "id": "call-1",
                "name": "read",
                "arguments": {"file_path": "/repo/one.py", "limit": 20},
            },
        ]
        second = _ocl_msg("model-x", 200, 20, mid="a2", ts="2026-04-27T16:01:00.000Z")
        second["message"]["content"] = [
            {
                "type": "toolCall",
                "id": "call-1",  # OpenClaw permits ids to repeat in later turns
                "name": "read",
                "arguments": {"file_path": "/repo/two.py"},
            }
        ]
        _ocl_write(
            root,
            "clawd",
            OCL_SID,
            [
                _ocl_user("go"),
                first,
                _ocl_result("call-1", "first output", "r1", "2026-04-27T16:00:20.000Z"),
                second,
                _ocl_result("call-1", "second output", "r2", "2026-04-27T16:01:10.000Z"),
            ],
        )
        store = ot.OpenClawStore(root, _ocl_args())

        turns = store.message_timeline(OCL_SID)
        assert [turn["content_key"] for turn in turns] == [f"{OCL_SID}:a1", f"{OCL_SID}:a2"]
        assert (turns[0]["has_text"], turns[0]["has_reasoning"]) == (True, True)
        assert (turns[1]["has_text"], turns[1]["has_reasoning"]) == (False, False)
        assert store.supports_turn_content(OCL_SID) is True
        trace = store.turn_content(OCL_SID)
        first_events = trace[turns[0]["content_key"]]
        second_events = trace[turns[1]["content_key"]]
        assert [event["kind"] for event in first_events] == ["reasoning", "text", "tool"]
        assert (first_events[2]["args"], first_events[2]["output"]) == (
            "/repo/one.py",
            "first output",
        )
        assert first_events[2]["params"] == [("limit", "20")]
        assert (second_events[0]["args"], second_events[0]["output"]) == (
            "/repo/two.py",
            "second output",
        )
        for turn in turns:
            key = turn["content_key"]
            assert store.turn_content(OCL_SID, content_key=key) == {key: trace[key]}


def test_openclaw_tool_blocks_survive_a_malformed_name():
    with tempfile.TemporaryDirectory() as root:
        msg = _ocl_msg("model-x", 100, 10, mid="a1")
        msg["message"]["content"] = [
            {"type": "toolCall", "id": "c1", "name": "bash"},
            {"type": "toolCall", "id": "c2"},  # no name
            {"type": "toolCall", "id": "c3", "name": ["x"]},  # not a string
            {"type": "toolCall", "id": "c4", "name": ""},  # empty
            {"type": "text", "text": "hello"},  # not a tool call
            "not-a-dict",
        ]
        bad = _ocl_msg("model-x", 100, 10, mid="a2", ts="2026-04-27T16:02:00.000Z")
        bad["message"]["content"] = "not-a-list"
        _ocl_write(root, "clawd", OCL_SID, [_ocl_user("go"), msg, bad])
        rows = ot.OpenClawStore(root, _ocl_args()).message_timeline(OCL_SID)
        assert [r["tools"] for r in rows] == [["bash"], []]


def test_expanding_an_unfinished_call_does_not_claim_a_later_reused_ids_result():
    with tempfile.TemporaryDirectory() as root:
        first = _ocl_msg("model-x", 100, 10, mid="a1")
        second = _ocl_msg("model-x", 200, 20, mid="a2", ts="2026-04-27T16:01:00.000Z")
        for record in (first, second):
            record["message"]["content"] = [
                {
                    "type": "toolCall",
                    "id": "shared",
                    "name": "read",
                    "arguments": {"path": record["id"]},
                }
            ]
        _ocl_write(
            root,
            "clawd",
            OCL_SID,
            [
                _ocl_user("go"),
                first,
                second,
                _ocl_result("shared", "second call only", "r2", "2026-04-27T16:01:10.000Z"),
            ],
        )
        store = ot.OpenClawStore(root, _ocl_args())
        key = f"{OCL_SID}:a1"
        full = store.turn_content(OCL_SID, content_key=key)
        assert list(full) == [key] and full[key][0]["output"] == ""
