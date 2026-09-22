import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import opentab as ot
from opentab.conversations.reader import ConversationError, window
from opentab.presentation.formatting import iso_to_local

from tests._support import PI_SID, _pi_args, _pi_assistant, _pi_session, _pi_user, _pi_write


def test_pi_conversation_retains_zero_usage_multipart_and_replayed_occurrences():
    with tempfile.TemporaryDirectory() as tmp:
        text = "  Grüße 👋\n\n```py\n  x = 1\n```\n"
        user = _pi_user(text)
        answer = {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "timestamp": "2026-05-15T08:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE THOUGHT"},
                    {"type": "text", "text": "Checking"},
                    {"type": "toolCall", "arguments": {"text": "PRIVATE ARGS"}},
                    {"type": "image", "text": "PRIVATE IMAGE"},
                    {"type": "text", "text": "Done\n"},
                ],
            },
        }
        rows = [
            _pi_session(PI_SID, tmp),
            user,
            answer,
            {"type": "message", "message": {"role": "toolResult", "content": "PRIVATE RESULT"}},
            {"type": "message", "message": {"role": "system", "content": "PRIVATE SYSTEM"}},
            _pi_user("Unanswered", mid="last"),
        ]
        _pi_write(tmp, "project", PI_SID, rows)
        _pi_write(tmp, "project", PI_SID, [_pi_session(PI_SID, tmp), user], ts_prefix="2026-05-16")
        store = ot.PiStore(tmp, _pi_args())
        before = {p: p.read_bytes() for p in Path(tmp).rglob("*.jsonl")}
        with patch.object(store, "_parse", side_effect=AssertionError("usage parse")):
            source = store.conversation_source(PI_SID)
            assert [r["message_id"] for r in source["records"]] == ["u1", "a1", "last", "u1"]
            assert [p["text"] for p in source["records"][1]["parts"]] == ["Checking", "Done\n"]
            assert source["records"][0]["parts"][0]["text"] == text
            assert source["records"][1]["parent_id"] == "u1"
            assert "PRIVATE" not in str(source)
            assert len({r["id"] for r in source["records"]}) == 4
            assert source["executions"] == [{"id": PI_SID, "parent_id": None}]
            assert (
                window(source, root_key=PI_SID, anchor=source["records"][-1]["id"])["records"][0][
                    "parts"
                ][0]["text"]
                == text
            )
            try:
                window(source, root_key=PI_SID, anchor="u1")
            except ConversationError as exc:
                assert exc.code == "ambiguous_anchor"
            else:
                raise AssertionError("native replay anchor was not ambiguous")
        assert {p: p.read_bytes() for p in before} == before
        store.demo = True
        with patch.object(store, "_files", side_effect=AssertionError("demo discovery")):
            assert not store.supports_conversation(PI_SID)
            try:
                store.conversation_source(PI_SID)
            except ConversationError:
                pass
            else:
                raise AssertionError("demo read allowed")


def test_pi_conversation_is_fresh_and_rejects_foreign_headers_missing_sources_and_limits():
    with tempfile.TemporaryDirectory() as tmp:
        rows = [_pi_session(PI_SID, tmp), _pi_user("initial")]
        _pi_write(tmp, "project", PI_SID, rows)
        store = ot.PiStore(tmp, _pi_args())
        path = next(Path(tmp).rglob("*.jsonl"))
        source = store.conversation_source(PI_SID)
        with path.open("a") as fh:
            fh.write("PRIVATE malformed\n")
            fh.write(json.dumps(_pi_user("new text", mid="new")) + "\n")
        fresh = store.conversation_source(PI_SID)
        assert fresh["snapshot"] != source["snapshot"]
        assert fresh["records"][-1]["parts"][0]["text"] == "new text"
        assert "malformed_jsonl_records_skipped" in fresh["limitations"]
        with patch("opentab.conversations.reader.MAX_LINE_BYTES", 1):
            try:
                store.conversation_source(PI_SID)
            except ConversationError as exc:
                assert exc.code == "conversation_too_large"
            else:
                raise AssertionError("ignored line budget")
        for bad_rows in (
            [_pi_session("foreign", tmp), _pi_user("PRIVATE foreign")],
            rows + [_pi_session("foreign", tmp)],
            [_pi_user("PRIVATE before header"), *rows],
            [_pi_user("PRIVATE no header")],
        ):
            path.write_text("\n".join(json.dumps(row) for row in bad_rows))
            try:
                store.conversation_source(PI_SID)
            except ConversationError as exc:
                assert exc.code == "invalid_execution"
                assert "PRIVATE" not in exc.message
            else:
                raise AssertionError("foreign/missing metadata accepted")
        path.unlink()
        try:
            store.conversation_source(PI_SID)
        except ConversationError as exc:
            assert exc.code == "invalid_execution"
        else:
            raise AssertionError("missing source accepted")


def test_pi_conversation_detects_concurrent_source_change_and_valid_empty_execution():
    from opentab.conversations import pi as reader

    with tempfile.TemporaryDirectory() as tmp:
        _pi_write(tmp, "project", PI_SID, [_pi_session(PI_SID, tmp)])
        store = ot.PiStore(tmp, _pi_args())
        assert store.conversation_source(PI_SID)["records"] == []
        original = reader.read_jsonl

        def mutate(paths):
            result = original(paths)
            with paths[0].open("a") as fh:
                fh.write(json.dumps(_pi_user("concurrent")) + "\n")
            return result

        with patch.object(reader, "read_jsonl", side_effect=mutate):
            try:
                store.conversation_source(PI_SID)
            except ConversationError as exc:
                assert exc.code == "source_changed"
            else:
                raise AssertionError("concurrent mutation accepted")


def test_pi_conversation_allows_unrelated_activity_without_changing_snapshot():
    from opentab.conversations import pi as reader

    other = "019fa4fd-ffff-7000-a6e9-c9e0c7ce25fc"
    with tempfile.TemporaryDirectory() as tmp:
        _pi_write(tmp, "project", PI_SID, [_pi_session(PI_SID, tmp), _pi_user("selected")])
        # These directory names sort differently as path components vs plain strings.
        _pi_write(tmp, "project-other", other, [_pi_session(other, tmp), _pi_user("unrelated")])
        store = ot.PiStore(tmp, _pi_args())
        initial = store.conversation_source(PI_SID)
        original = reader.read_jsonl

        def mutate(paths):
            result = original(paths)
            _pi_write(
                tmp,
                "project-other",
                other,
                [_pi_session(other, tmp), _pi_user("changed unrelated")],
            )
            return result

        with patch.object(reader, "read_jsonl", side_effect=mutate):
            assert store.conversation_source(PI_SID) == initial


def test_pi_store_ended_at_reflects_the_latest_assistant_reply():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _pi_session(PI_SID, cwd, ts="2026-05-15T07:32:15.949Z"),
            _pi_user("go", ts="2026-05-15T07:32:20.000Z"),
            _pi_assistant(
                "anthropic/claude-sonnet-4",
                100,
                50,
                cost=0.01,
                mid="a1",
                ts="2026-05-15T07:40:00.000Z",  # the latest activity in the session
            ),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        w = ot.PiStore(root, _pi_args()).workflows()[0]

        # iso_to_local renders in the system's local TZ, so compare against its own
        # conversion of each raw UTC timestamp rather than a hardcoded wall-clock string.
        assert w.created_at == iso_to_local("2026-05-15T07:32:15.949Z")
        assert w.ended_at == iso_to_local("2026-05-15T07:40:00.000Z")


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


def test_pi_trace_obeys_a_zero_usage_messages_earlier_dedup_claim():
    with tempfile.TemporaryDirectory() as tmp:
        root, cwd = os.path.join(tmp, "sessions"), os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _pi_write(
            root,
            "--proj--",
            PI_SID,
            [
                _pi_session(PI_SID, cwd),
                _pi_assistant("model-x", 0, 0, mid="same"),
                _pi_assistant("model-x", 100, 10, mid="same"),
                _pi_assistant("model-x", 50, 5, mid="kept", tools=["read"]),
            ],
        )
        store = ot.PiStore(root, _pi_args())
        turns = store.message_timeline(PI_SID)
        assert [turn["content_key"] for turn in turns] == [f"{PI_SID}:kept"]
        assert set(store.turn_content(PI_SID)) == {f"{PI_SID}:kept"}


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


def test_pi_turn_content_reads_thinking_narration_exact_calls_and_results():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        assistant = _pi_assistant(
            "anthropic/claude-sonnet-4", 100, 50, cost=0.01, mid="a1", tools=["read"]
        )
        assistant["message"]["content"] = [
            {
                "type": "thinking",
                "thinking": "Inspect the file first.",
                "thinkingSignature": "opaque",
            },
            {"type": "text", "text": "I'll read it."},
            {
                "type": "toolCall",
                "id": "call-1",
                "name": "read",
                "arguments": {"file_path": "/repo/a.py", "limit": 20},
            },
        ]
        result = {
            "type": "message",
            "id": "r1",
            "parentId": "a1",
            "timestamp": "2026-05-15T07:32:37.000Z",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-1",
                "toolName": "read",
                "content": [
                    {"type": "text", "text": "1  import os"},
                    {"type": "image", "data": "not retained", "mimeType": "image/png"},
                ],
                "isError": False,
                "timestamp": 0,
            },
        }
        _pi_write(
            root, "--proj--", PI_SID, [_pi_session(PI_SID, cwd), _pi_user("go"), assistant, result]
        )
        store = ot.PiStore(root, _pi_args())

        (turn,) = store.message_timeline(PI_SID)
        assert turn["content_key"] == f"{PI_SID}:a1"
        assert turn["has_text"] is True and turn["has_reasoning"] is True
        assert store.supports_turn_content(PI_SID) is True
        events = store.turn_content(PI_SID)[turn["content_key"]]
        assert [e["kind"] for e in events] == ["reasoning", "text", "tool"]
        assert events[0]["text"] == "Inspect the file first."
        assert events[1]["text"] == "I'll read it."
        assert (events[2]["name"], events[2]["args"]) == ("read", "/repo/a.py")
        assert events[2]["params"] == [("limit", "20")]
        assert events[2]["output"] == "1  import os\n(image)"
        assert store.turn_content(PI_SID, content_key=turn["content_key"]) == {
            turn["content_key"]: events
        }


def test_pi_auth_json_is_fingerprinted_so_a_login_change_invalidates_the_warm_cache():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        auth = os.path.join(tmp, "auth.json")
        rows = [
            _pi_session(PI_SID, cwd),
            _pi_assistant("acme/model-x", 1000, 500, cost=1.23, provider="acme-cloud"),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        assert auth in ot.PiStore(root, _pi_args()).cache_inputs()

        args = type("A", (), {"demo": False, "no_cache": False})()
        cold = ot.CachedStore(ot.PiStore(root, _pi_args()), "pi|" + root, args)
        assert cold.workflows()[0].total_cost == 1.23 and cold.records_cost is True
        cold.model_breakdown()  # what App's deferred scan does -- this writes the cache
        warm = ot.CachedStore(ot.PiStore(root, _pi_args()), "pi|" + root, args)
        assert warm.workflows() and warm.served_from_cache  # unchanged corpus -> a hit

        with open(auth, "w") as fh:
            json.dump({"acme-cloud": {"type": "oauth"}}, fh)
        after = ot.CachedStore(ot.PiStore(root, _pi_args()), "pi|" + root, args)
        w = after.workflows()[0]
        assert after.served_from_cache is False  # the login change misses the fingerprint
        assert w.total_cost == 0.0 and w.unpriced_tokens == 1500
        assert after.records_cost is False  # and the whole frame flips to ESTIMATED


def test_pi_reload_re_reads_the_login_state():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _pi_session(PI_SID, cwd),
            _pi_assistant("acme/model-x", 1000, 500, cost=1.23, provider="acme-cloud"),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        assert store.workflows()[0].total_cost == 1.23
        with open(os.path.join(tmp, "auth.json"), "w") as fh:
            json.dump({"acme-cloud": {"type": "oauth"}}, fh)
        assert store.workflows()[0].total_cost == 0.0  # reload, same instance


def test_pi_survives_a_valid_json_line_that_is_not_an_object():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _pi_write(
            root,
            "--proj--",
            PI_SID,
            [
                ["type"],  # a list, not an object
                ["cost"],
                _pi_session(PI_SID, cwd),
                _pi_assistant("acme/model-x", 100, 50, cost=0.5, provider="openrouter"),
            ],
        )
        assert ot.PiStore(root, _pi_args()).records_cost is True  # the probe path
        w = ot.PiStore(root, _pi_args()).workflows()
        assert len(w) == 1 and w[0].total_tokens == 150


def test_pi_survives_a_token_count_json_parses_as_infinity():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        d = os.path.join(root, "--proj--")
        os.makedirs(d)
        with open(os.path.join(d, f"2026-05-15T07-32-15-949Z_{PI_SID}.jsonl"), "w") as fh:
            fh.write(json.dumps(_pi_session(PI_SID, cwd)) + "\n")
            fh.write(
                '{"type": "message", "id": "a1", "timestamp": "2026-05-15T07:32:36.257Z", '
                '"message": {"role": "assistant", "model": "acme/model-x", "provider": '
                '"openrouter", "usage": {"input": 1e400, "output": 50, "cost": '
                '{"total": 1e400}}}}\n'
            )
        w = ot.PiStore(root, _pi_args()).workflows()
        assert len(w) == 1
        assert w[0].total_tokens == 50  # the inf field drops to 0, the record survives
        assert w[0].total_cost == 0.0  # and no inf reaches a total
