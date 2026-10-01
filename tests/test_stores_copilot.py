import json
import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import opentab as ot

from tests._support import (
    _copilot_args,
    _event,
    _otel_chat,
    _write_events,
    _write_jsonl,
    _write_otel,
)

COPILOT_SID = "c623bce1-5906-429f-a517-d4fb2cee7cf7"


def test_copilot_store_splits_cache_folds_reasoning_and_stays_unpriced():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        # input_tokens (19452) includes 123 reads and 25 writes -> uncached 19304;
        # reasoning (128) folds into output for pricing; cache_creation -> cache_write.
        _write_otel(
            otel,
            [
                {"type": "metric", "name": "gen_ai.client.token.usage"},  # non-usage -> ignored
                _otel_chat(
                    COPILOT_SID,
                    "claude-sonnet-4",
                    19452,
                    281,
                    cache_read=123,
                    cache_create=25,
                    reasoning=128,
                ),
            ],
        )
        store = ot.CopilotStore(otel, _copilot_args(otel))
        assert store.records_cost is False  # subscription-style: $0 until "$" reprices
        workflows = store.workflows()
        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == COPILOT_SID
        assert w.source == "Copilot"
        assert w.subagents == 0  # no subagent tree
        assert w.total_cost == 0.0 and w.root_cost == 0.0
        # tokens_total = uncached(19304) + cache_read(123) + cache_write(25) + output(281+128)
        assert w.total_tokens == w.unpriced_tokens == 19304 + 123 + 25 + 409

        row = next(r for r in store.model_breakdown() if r["root_id"] == COPILOT_SID)
        assert row["model_name"] == "anthropic/claude-sonnet-4"  # mixed-provider prefix
        assert row["unpriced_input"] == 19304
        assert row["unpriced_cache_read"] == 123
        assert row["unpriced_cache_write"] == 25
        assert row["unpriced_output"] == 409  # reasoning folded in, priced once
        assert row["reasoning"] == 0  # folded, never double-counted

        # the (all-unpriced) usage reprices to a positive list-price estimate under "$"
        est = ot.api_equivalent_cost("anthropic/claude-sonnet-4", 19304, 409, 0, 123, 25)
        assert est > 0

        nodes = store.workflow_nodes(COPILOT_SID)
        assert len(nodes) == 1 and nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["model_name"] == "anthropic/claude-sonnet-4"
        assert nodes[0]["cost"] == 0.0


def test_copilot_ended_at_reflects_the_latest_call_not_the_first():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(
            otel,
            [
                _otel_chat(
                    COPILOT_SID, "gpt-5.4", 100, 50, trace="t1", span="s1", end=(1775934264, 0)
                ),
                _otel_chat(
                    COPILOT_SID, "gpt-5.4", 200, 80, trace="t2", span="s2", end=(1775934500, 0)
                ),
            ],
        )
        store = ot.CopilotStore(otel, _copilot_args(otel))
        (w,) = store.workflows()
        assert w.created_at == store._ms_to_local(1775934264 * 1000)
        assert w.ended_at == store._ms_to_local(1775934500 * 1000)
        assert w.ended_at != w.created_at


def test_copilot_cache_writes_match_shutdown_token_details_across_views():
    # Copilot CLI's real GPT-5.6 Terra export and session.shutdown.tokenDetails:
    # input_tokens=17984 includes 17981 writes; only 3 tokens are uncached.
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        chat = _otel_chat(COPILOT_SID, "gpt-5.6-terra", 17984, 8)
        chat["attributes"]["gen_ai.usage.cache_write.input_tokens"] = 17981
        _write_otel(otel, [chat])
        store = ot.CopilotStore(otel, _copilot_args(otel))
        (workflow,) = store.workflows()
        (model,) = store.model_breakdown()
        (node,) = store.workflow_nodes(COPILOT_SID)
        (turn,) = store.message_timeline(COPILOT_SID)
        assert workflow.total_tokens == workflow.unpriced_tokens == 17992
        assert store.summary([workflow])["tokens"] == 17992
        assert model["runs"] == 1 and model["input"] == model["unpriced_input"] == 3
        assert model["root_unpriced_input"] == 3
        assert node["tokens_input"] == 3 and node["tokens_cache_write"] == 17981
        assert node["tokens_total"] == model["tokens_total"] == turn["tokens_total"] == 17992
        assert turn["input"] == 3 and turn["cache_write"] == 17981
        assert (
            abs(
                ot.api_equivalent_cost(
                    model["model_name"],
                    model["input"],
                    model["output"],
                    model["reasoning"],
                    model["cache_read"],
                    model["cache_write"],
                )
                - 0.0450545
            )
            < 1e-10
        )
        assert workflow.total_cost == model["cost"] == turn["cost"] == 0


def test_copilot_cache_reads_and_writes_share_the_input_budget():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(
            otel,
            [
                _otel_chat("mixed", "gpt-5.6-terra", 1000, 10, cache_read=700, cache_create=200),
                _otel_chat(
                    "oversized",
                    "gpt-5.6-terra",
                    1000,
                    10,
                    cache_read=700,
                    cache_create=500,
                    trace="t2",
                    span="s2",
                ),
            ],
        )
        store = ot.CopilotStore(otel, _copilot_args(otel))
        rows = {r["root_id"]: r for r in store.model_breakdown()}
        assert rows["mixed"]["input"] == 100
        assert rows["mixed"]["cache_read"] == 700 and rows["mixed"]["cache_write"] == 200
        assert rows["oversized"]["input"] == 0 and rows["oversized"]["cache_write"] == 300
        assert all(r["tokens_total"] == 1010 for r in rows.values())


def test_copilot_store_dedupes_redundant_records_keeping_chat_span():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        # The same LLM call logged three ways for one (trace, response). Only the chat
        # span must count (60/10) -- the inference log and invoke_agent summary are
        # suppressed by matching trace id / response id.
        agent_summary = {
            "type": "span",
            "traceId": "trace-dupe",
            "spanId": "agent-1",
            "name": "invoke_agent GitHub Copilot",
            "attributes": {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.response.model": "gpt-5.4-mini",
                "gen_ai.conversation.id": "conv-dupe",
                "gen_ai.response.id": "resp-dupe",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 30,
            },
        }
        inference = {
            "hrTime": [1775934263, 0],
            "_body": "GenAI inference: gpt-5.4-mini",
            "attributes": {
                "event.name": "gen_ai.client.inference.operation.details",
                "gen_ai.response.model": "gpt-5.4-mini",
                "gen_ai.conversation.id": "conv-dupe",
                "gen_ai.response.id": "resp-dupe",
                "gen_ai.usage.input_tokens": 80,
                "gen_ai.usage.output_tokens": 20,
            },
        }
        chat = _otel_chat(
            "conv-dupe", "gpt-5.4-mini", 60, 10, trace="trace-dupe", span="chat-1", resp="resp-dupe"
        )
        _write_otel(otel, [agent_summary, inference, chat])
        store = ot.CopilotStore(otel, _copilot_args(otel))
        rows = [r for r in store.model_breakdown() if r["root_id"] == "conv-dupe"]
        assert len(rows) == 1
        assert rows[0]["model_name"] == "openai/gpt-5.4-mini"
        assert rows[0]["runs"] == 1  # only the chat span survived dedup
        assert rows[0]["unpriced_input"] == 60 and rows[0]["unpriced_output"] == 10
        assert rows[0]["tokens_total"] == 70


def test_copilot_store_dedupes_span_and_log_split_across_files():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        # OTEL exporters write spans and logs to DIFFERENT files: the same call appears
        # as a chat span in spans.jsonl and an inference log in logs.jsonl. It must
        # count once (the chat span's 60/10), never twice.
        chat = _otel_chat(
            "conv-x", "gpt-5.4", 60, 10, trace="trace-x", span="chat-1", resp="resp-x"
        )
        inference = {
            "traceId": "trace-x",
            "hrTime": [1775934263, 0],
            "_body": "GenAI inference: gpt-5.4",
            "attributes": {
                "event.name": "gen_ai.client.inference.operation.details",
                "gen_ai.response.model": "gpt-5.4",
                "gen_ai.conversation.id": "conv-x",
                "gen_ai.response.id": "resp-x",
                "gen_ai.usage.input_tokens": 80,
                "gen_ai.usage.output_tokens": 20,
            },
        }
        _write_otel(otel, [chat], name="spans.jsonl")
        _write_otel(otel, [inference], name="logs.jsonl")
        store = ot.CopilotStore(otel, _copilot_args(otel))
        rows = [r for r in store.model_breakdown() if r["root_id"] == "conv-x"]
        assert len(rows) == 1
        assert rows[0]["runs"] == 1  # the log in the other file was suppressed
        assert rows[0]["unpriced_input"] == 60 and rows[0]["unpriced_output"] == 10
        assert rows[0]["tokens_total"] == 70


def test_copilot_store_enriches_cwd_and_title_from_session_store_db():
    with tempfile.TemporaryDirectory() as tmp:
        copilot = os.path.join(tmp, ".copilot")
        otel = os.path.join(copilot, "otel")
        # The session ran in <repo>/sub; OTEL carries no cwd, so it must come from the
        # sibling session-store.db and fold to the git root, with the title from summary.
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(otel)  # also creates the .copilot dir that holds session-store.db
        db = sqlite3.connect(os.path.join(copilot, "session-store.db"))
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, summary TEXT)")
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            (COPILOT_SID, sub, "Refactor the date formatter"),
        )
        db.commit()
        db.close()
        _write_otel(otel, [_otel_chat(COPILOT_SID, "gpt-5.4", 100, 50)])
        w = ot.CopilotStore(otel, _copilot_args(otel)).workflows()[0]
        assert w.directory == repo  # folded to the git root, not the bare "sub"
        assert w.title == "Refactor the date formatter"
        assert w.created_at  # derived from the OTEL endTime


def test_copilot_store_reads_exporter_env_file_and_total_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")  # empty export dir
        os.makedirs(otel)
        # A single-file export pointed to by the documented env var, living OUTSIDE the
        # export dir, must still be read. The record logs only a grand total (no
        # input/output split) -> the total back-fills as output.
        extra = os.path.join(tmp, "elsewhere", "export.jsonl")
        os.makedirs(os.path.dirname(extra))
        rec = {
            "type": "span",
            "traceId": "t9",
            "spanId": "s9",
            "name": "chat gpt-5.4",
            "endTime": [1775934264, 0],
            "attributes": {
                "gen_ai.operation.name": "chat",
                "gen_ai.response.model": "gpt-5.4",
                "gen_ai.conversation.id": "env-sess",
                "gen_ai.usage.total_tokens": 250,
            },
        }
        _write_jsonl(extra, [rec])
        prev = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH")
        os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"] = extra
        try:
            store = ot.CopilotStore(otel, _copilot_args(otel))
            rows = store.model_breakdown()
        finally:
            if prev is None:
                del os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"]
            else:
                os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"] = prev
        assert len(rows) == 1
        assert rows[0]["model_name"] == "openai/gpt-5.4"
        assert rows[0]["unpriced_output"] == 250  # total back-filled as output
        assert rows[0]["tokens_total"] == 250


def test_copilot_store_does_not_double_count_exporter_file_inside_dir():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        # The env var points at a file that ALSO lives in --copilot-dir (the default
        # setup). It must be read once, not once via glob + once via the env var.
        _write_otel(otel, [_otel_chat("s1", "gpt-5.4", 100, 50)], name="usage.jsonl")
        inside = os.path.join(otel, "usage.jsonl")
        prev = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH")
        os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"] = inside
        try:
            rows = ot.CopilotStore(otel, _copilot_args(otel)).model_breakdown()
        finally:
            if prev is None:
                del os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"]
            else:
                os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"] = prev
        assert len(rows) == 1
        assert rows[0]["runs"] == 1  # not 2 -- the file was not read twice
        assert rows[0]["tokens_total"] == 150


def test_copilot_turns_timeline_is_headerless():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(
            otel,
            [
                _otel_chat("sess-1", "gpt-5", 1000, 100, cache_read=200, trace="t1", span="s1"),
                _otel_chat(
                    "sess-1", "claude-sonnet-4", 500, 50, trace="t2", span="s2", end=(1775934300, 0)
                ),
            ],
        )
        store = ot.CopilotStore(otel, _copilot_args(otel))
        store.workflows()
        assert store.supports_turns("sess-1")
        t = store.message_timeline("sess-1")
        assert len(t) == 2
        # OTEL captures no prompt content by default -> headerless rows (one group).
        assert all(r["prompt_id"] == "" and r["prompt_title"] == "" for r in t)
        assert t[0]["model_name"] == "openai/gpt-5" and t[0]["input"] == 800
        assert t[0]["time"] <= t[1]["time"] and t[0]["time"].startswith("2026-")


def _otel_inference(conv, resp, inp, out, trace="t1", model="gpt-5.4-mini"):
    # A GenAI inference LOG -- the same call a chat span also records, one fidelity down.
    attrs = {
        "event.name": "gen_ai.client.inference.operation.details",
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": inp,
        "gen_ai.usage.output_tokens": out,
    }
    if conv:
        attrs["gen_ai.conversation.id"] = conv
    if resp:
        attrs["gen_ai.response.id"] = resp
    return {
        "hrTime": [1775934263, 0],
        "_body": "GenAI inference",
        "traceId": trace,
        "attributes": attrs,
    }


def _tokens(otel):
    store = ot.CopilotStore(otel, _copilot_args(otel))
    rows = store.model_breakdown()
    return sum(r["runs"] for r in rows), sum(r["tokens_total"] for r in rows)


def test_copilot_dedup_keeps_distinct_calls_sharing_one_trace():
    # A trace groups a turn; response ids distinguish calls within it.
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        # chat(r1) + its duplicate inference(r1) + a DISTINCT second call inference(r2)
        _write_otel(
            otel,
            [
                _otel_chat("conv", "gpt-5.4-mini", 60, 10, trace="t1", span="s1", resp="r1"),
                _otel_inference("conv", "r1", 80, 20),
                _otel_inference("conv", "r2", 40, 10),
            ],
        )
        assert _tokens(otel) == (2, 120)  # both calls, chat winning r1 -- not (1, 70)

    # The duplicate shapes must still collapse.
    for rows, expect in (
        (
            [
                _otel_chat("c", "gpt-5.4-mini", 60, 10, trace="t1", span="s1", resp="r1"),
                _otel_inference("c", "r1", 80, 20),
            ],
            (1, 70),
        ),  # same response
        (
            [
                _otel_chat("c", "gpt-5.4-mini", 60, 10, trace="t1", span="s1"),
                _otel_inference("c", "r1", 80, 20),
            ],
            (1, 70),
        ),  # cover names no response
        (
            [
                _otel_chat("c", "gpt-5.4-mini", 60, 10, trace="t1", span="s1"),
                _otel_inference("c", None, 80, 20),
            ],
            (1, 70),
        ),  # neither does
    ):
        with tempfile.TemporaryDirectory() as tmp:
            otel = os.path.join(tmp, ".copilot", "otel")
            _write_otel(otel, rows)
            assert _tokens(otel) == expect


def test_copilot_record_joins_the_conversation_named_on_its_trace():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(
            otel,
            [
                _otel_chat("conv-real", "gpt-5.4-mini", 0, 0, trace="t9", span="s9"),
                _otel_inference(None, "resp-A", 60, 10, trace="t9"),
                _otel_inference(None, "resp-B", 40, 10, trace="t9"),
            ],
        )
        store = ot.CopilotStore(otel, _copilot_args(otel))
        assert {r["root_id"] for r in store.model_breakdown()} == {"conv-real"}


def test_copilot_cache_fingerprint_covers_the_session_store_db():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(otel, [_otel_chat(COPILOT_SID, "gpt-5.4-mini", 60, 10)])
        store = ot.CopilotStore(otel, _copilot_args(otel))
        db = os.path.join(tmp, ".copilot", "session-store.db")
        assert db not in store.cache_inputs()  # absent: nothing to fingerprint yet

        sqlite3.connect(db).close()
        assert db in ot.CopilotStore(otel, _copilot_args(otel)).cache_inputs()


def test_copilot_events_join_exact_calls_prompts_tools_and_reasoning_lazily():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(
            otel,
            [
                _otel_chat("s", "gpt-5.6-terra", 1000, 20, resp="r1"),
                _otel_chat("s", "gpt-5.6-terra", 1200, 30, resp="r2", trace="t2", span="s2"),
            ],
        )
        large = "output marker\n" * 1000
        events = [
            _event("user.message", {"messageId": "u1", "content": "Check the files"}, "u1"),
            _event(
                "assistant.message",
                {
                    "messageId": "a1",
                    "apiCallId": "r1",
                    "originatingMessageId": "u1",
                    "model": "gpt-5.6-terra",
                    "content": "I will inspect both files",
                    "reasoningText": "Check carefully",
                    "encryptedContent": "NEVER SHOW ENCRYPTED",
                    "toolRequests": [
                        {
                            "toolCallId": "c1",
                            "name": "bash",
                            "arguments": {"command": "cat a", "timeout": 10},
                        },
                        {"toolCallId": "c2", "name": "bash", "arguments": {"command": "cat b"}},
                    ],
                },
                "a1",
            ),
            _event("tool.execution_start", {"toolCallId": "c1", "toolName": "bash"}, "start1"),
            _event(
                "tool.execution_complete",
                {
                    "toolCallId": "c1",
                    "success": True,
                    "result": {"content": "short", "detailedContent": large},
                },
                "end1",
            ),
            _event("tool.execution_start", {"toolCallId": "c2", "toolName": "bash"}, "start2"),
            _event(
                "tool.execution_complete",
                {"toolCallId": "c2", "success": False, "error": {"message": "missing b"}},
                "end2",
            ),
            _event("user.message", {"messageId": "u2", "content": "Summarize"}, "u2"),
            # The persisted title-generation call is not a conversation answer.
            _event(
                "model.response",
                {"callId": "title", "response": {"content": "DO NOT SHOW TITLE"}},
                "title",
            ),
            _event(
                "assistant.message",
                {
                    "messageId": "a2",
                    "apiCallId": "r2",
                    "originatingMessageId": "u2",
                    "model": "gpt-5.6-terra",
                    "content": "Summary",
                    "toolRequests": [],
                },
                "a2",
            ),
            _event(
                "session.shutdown",
                {"systemTokens": 8351, "toolDefinitionsTokens": 11327, "conversationTokens": 41},
                "shutdown",
            ),
        ]
        path = _write_events(otel, "s", events)
        store = ot.CopilotStore(otel, _copilot_args(otel))
        assert os.path.realpath(path) in store.cache_inputs()
        assert store.workflows()[0].total_tokens == 2250
        # Accounting retains only scalar identities and usage, never the trace.
        numeric = json.dumps(store._sessions)
        assert (
            "Check the files" not in numeric
            and "cat a" not in numeric
            and "output marker" not in numeric
        )
        assert store.supports_tools("s") and store.supports_turn_content("s")
        assert store.records_reasoning
        turns = store.message_timeline("s")
        assert [r["prompt_full"] for r in turns] == ["Check the files", "Summarize"]
        assert turns[0]["tools"] == ["bash", "bash"] and turns[1]["tools"] == []
        assert turns[0]["has_text"] and turns[0]["has_reasoning"]
        (tool,) = store.tool_breakdown("s")
        assert tool["calls"] == 2 and tool["tokens_total"] == 1020
        preview = store.turn_content("s")
        first = preview[turns[0]["content_key"]]
        assert [e["kind"] for e in first] == ["reasoning", "text", "tool", "tool"]
        assert first[2]["args"] == "cat a" and first[2]["params"] == [("timeout", "10")]
        assert first[2]["output_dropped"] > 0 and first[3]["output"] == "missing b"
        full = store.turn_content("s", turns[0]["content_key"])
        assert set(full) == {turns[0]["content_key"]}
        assert full[turns[0]["content_key"]][2]["output"] == large
        assert "NEVER SHOW ENCRYPTED" not in json.dumps(full)
        assert "DO NOT SHOW TITLE" not in json.dumps(preview)
        assert store.turn_content("s", "foreign-key") == {}
        context = store.context_breakdown("s")
        assert sum(r["est_tokens"] for r in context) == 19719
        assert context[0]["category"] == "Tools"
        # Detail rereads see newly finalized content without caching raw bodies.
        events[1]["data"]["content"] = "Updated answer"
        _write_events(otel, "s", events)
        assert (
            store.turn_content("s", turns[0]["content_key"])[turns[0]["content_key"]][1]["text"]
            == "Updated answer"
        )


def test_copilot_subagents_keep_exact_nested_ownership_and_root_model_shares():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(
            otel,
            [
                _otel_chat("s", "gpt-5.6-terra", 100, 10, resp="root"),
                _otel_chat("s", "gpt-5.6-terra", 200, 20, resp="child", trace="tc", span="sc"),
                _otel_chat("s", "gpt-5.6-terra", 300, 30, resp="nested", trace="tn", span="sn"),
            ],
        )
        _write_events(
            otel,
            "s",
            [
                _event("user.message", {"messageId": "u", "content": "Investigate"}, "u"),
                _event(
                    "assistant.message",
                    {
                        "messageId": "a",
                        "apiCallId": "root",
                        "model": "gpt-5.6-terra",
                        "content": "Delegating",
                        "toolRequests": [
                            {
                                "toolCallId": "task1",
                                "name": "task",
                                "arguments": {"prompt": "Parent task hint"},
                            }
                        ],
                    },
                    "a",
                ),
                _event(
                    "subagent.started",
                    {"agentName": "explore", "toolCallId": "task1"},
                    "spawn",
                    "child-id",
                ),
                _event(
                    "user.message",
                    {"messageId": "cu", "content": "Actual child instructions"},
                    "cu",
                    "child-id",
                ),
                _event(
                    "assistant.message",
                    {
                        "messageId": "ca",
                        "apiCallId": "child",
                        "model": "gpt-5.6-terra",
                        "content": "Child reply",
                    },
                    "ca",
                    "child-id",
                ),
                _event(
                    "subagent.started",
                    {"agentName": "explore", "toolCallId": "task2", "parentId": "child-id"},
                    "spawn2",
                    "nested-id",
                ),
                _event(
                    "user.message",
                    {"messageId": "nu", "content": "Nested instructions"},
                    "nu",
                    "nested-id",
                ),
                _event(
                    "assistant.message",
                    {
                        "messageId": "na",
                        "apiCallId": "nested",
                        "model": "gpt-5.6-terra",
                        "content": "Nested reply",
                    },
                    "na",
                    "nested-id",
                ),
                _event(
                    "subagent.started",
                    {"agentName": "empty", "toolCallId": "empty-task"},
                    "empty",
                    "empty-id",
                ),
            ],
        )
        store = ot.CopilotStore(otel, _copilot_args(otel))
        (workflow,) = store.workflows()
        assert workflow.subagents == 3 and workflow.total_tokens == 660
        (model,) = store.model_breakdown()
        assert model["root_tokens_total"] == 110 and model["root_unpriced_input"] == 100
        nodes = store.workflow_nodes("s")
        assert [n["depth"] for n in nodes] == [0, 1, 2, 1]
        assert [n["tokens_total"] for n in nodes] == [110, 220, 330, 0]
        child, nested, empty = (n["id"] for n in nodes[1:])
        timeline = store.message_timeline("s")
        assert [r["depth"] for r in timeline] == [0, 1, 2]
        assert all(r["prompt_full"] == "Investigate" for r in timeline)
        assert store.node_prompt("s", child) == "Actual child instructions"
        assert store.node_prompt("s", nested) == "Nested instructions"
        assert store.node_prompt("s", empty) is None
        (row,) = store.node_timeline("s", nested)
        assert row["depth"] == 0 and row["tokens_total"] == 330
        assert row["prompt_full"] == "Nested instructions"
        assert store.node_timeline("s", empty) == []
        assert store.node_timeline("other", nested) is None
        assert store.node_turn_content("s", child, row["content_key"]) == {}
        assert list(store.node_turn_content("s", nested)) == [row["content_key"]]
        assert store.node_timeline("s", "explore") is None  # names are not identities


def test_copilot_trace_reused_tool_ids_and_foreign_or_ambiguous_responses_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(
            otel,
            [
                _otel_chat("s", "gpt-5.6-terra", 100, 10, resp="r1"),
                _otel_chat("s", "gpt-5.6-terra", 200, 20, resp="r2", trace="t2", span="s2"),
            ],
        )
        events = [
            _event(
                "assistant.message",
                {
                    "messageId": "a1",
                    "apiCallId": "r1",
                    "model": "gpt-5.6-terra",
                    "content": "First",
                    "toolRequests": [
                        {"toolCallId": "reuse", "name": "bash", "arguments": {"command": "first"}}
                    ],
                },
                "a1",
            ),
            _event(
                "assistant.message",
                {
                    "messageId": "a2",
                    "apiCallId": "r2",
                    "model": "gpt-5.6-terra",
                    "content": "Second",
                    "toolRequests": [
                        {"toolCallId": "reuse", "name": "bash", "arguments": {"command": "second"}}
                    ],
                },
                "a2",
            ),
            _event(
                "tool.execution_complete",
                {
                    "toolCallId": "reuse",
                    "success": True,
                    "result": {"content": "belongs to second"},
                },
                "result",
            ),
        ]
        path = _write_events(otel, "s", events)
        store = ot.CopilotStore(otel, _copilot_args(otel))
        turns = store.message_timeline("s")
        first = store.turn_content("s", turns[0]["content_key"])[turns[0]["content_key"]]
        assert first[-1]["output"] == ""
        second = store.turn_content("s", turns[1]["content_key"])[turns[1]["content_key"]]
        assert second[-1]["output"] == "belongs to second"
        # Unmatched newer calls must invalidate older pending bindings too.
        events[1]["data"]["apiCallId"] = "unmatched"
        _write_events(otel, "s", events)
        assert (
            store.turn_content("s", turns[0]["content_key"])[turns[0]["content_key"]][-1]["output"]
            == ""
        )
        # Same completion ID under a different model is ambiguous, never a match.
        events.append(
            _event(
                "assistant.message",
                {
                    "messageId": "foreign",
                    "apiCallId": "r1",
                    "model": "claude-sonnet-4",
                    "content": "FOREIGN",
                },
                "foreign",
            )
        )
        _write_events(otel, "s", events)
        assert store.turn_content("s", turns[0]["content_key"]) == {}
        # The directory name alone must not authorize reading another session.
        _write_jsonl(path, [_event("session.start", {"sessionId": "other"}, "start")] + events)
        assert store.turn_content("s") == {} and store.context_breakdown("s") == []


def test_copilot_demo_blocks_event_content_and_otel_only_stays_headerless():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(otel, [_otel_chat("s", "gpt-5.6-terra", 100, 10, resp="r1")])
        store = ot.CopilotStore(otel, _copilot_args(otel))
        assert not store.supports_turn_content("s") and not store.supports_tools("s")
        assert store.message_timeline("s")[0]["prompt_full"] == ""
        _write_events(
            otel,
            "s",
            [
                _event(
                    "assistant.message",
                    {"apiCallId": "r1", "model": "gpt-5.6-terra", "content": "SECRET"},
                    "a1",
                )
            ],
        )
        store.demo = True
        assert store.turn_content("s") == {} and store.context_breakdown("s") == []
        assert store.node_prompt("s", "child") is None
        assert store.node_timeline("s", "s") is None
        assert all(r["prompt_full"] == "" for r in store.message_timeline("s"))


def test_copilot_response_chunks_and_late_tool_results_preserve_call_boundaries():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(otel, [_otel_chat("s", "gpt-5.6-terra", 100, 10, resp="r")])
        _write_events(
            otel,
            "s",
            [
                _event("user.message", {"messageId": "u", "content": "Check"}, "u"),
                _event(
                    "assistant.message",
                    {
                        "apiCallId": "r",
                        "messageId": "chunk1",
                        "turnId": "1",
                        "reasoningBlocks": {
                            "provider": "openai",
                            "blocks": [
                                {
                                    "type": "reasoning",
                                    "summary": [{"type": "summary_text", "text": "Plan"}],
                                    "encrypted_content": "OPAQUE",
                                }
                            ],
                        },
                    },
                    "chunk1",
                ),
                _event(
                    "assistant.message",
                    {
                        "apiCallId": "r",
                        "messageId": "chunk2",
                        "turnId": "1",
                        "model": "gpt-5.6-terra",
                        "content": "Answer",
                        "toolRequests": [
                            {
                                "toolCallId": "c",
                                "name": "query",
                                "mcpServerName": "db",
                                "mcpToolName": "query",
                                "arguments": {"sql": "select 1"},
                            }
                        ],
                    },
                    "chunk2",
                ),
                _event(
                    "tool.execution_complete",
                    {
                        "toolCallId": "c",
                        "turnId": "0",
                        "success": True,
                        "result": {"content": "STALE"},
                    },
                    "stale",
                ),
                _event(
                    "tool.execution_start",
                    {
                        "toolCallId": "c",
                        "turnId": "1",
                        "toolName": "query",
                        "mcpServerName": "db",
                        "mcpToolName": "query",
                    },
                    "tool-start",
                ),
                _event(
                    "tool.execution_complete",
                    {"toolCallId": "c", "turnId": "1", "success": True, "result": {"content": "1"}},
                    "result",
                ),
            ],
        )
        store = ot.CopilotStore(otel, _copilot_args(otel))
        (turn,) = store.message_timeline("s")
        assert turn["tokens_total"] == 110 and turn["prompt_full"] == "Check"
        assert turn["tools"] == ["mcp__db__query"] and turn["has_reasoning"]
        events = store.turn_content("s", turn["content_key"])[turn["content_key"]]
        assert [e["kind"] for e in events] == ["reasoning", "text", "tool"]
        assert events[0]["text"] == "Plan" and events[-1]["output"] == "1"
        assert "OPAQUE" not in json.dumps(events) and "STALE" not in json.dumps(events)
        assert store.model_breakdown()[0]["runs"] == 1


def _conversation_error(code, call):
    from opentab.conversations.reader import ConversationError

    try:
        call()
    except ConversationError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError(f"expected {code}")


def test_copilot_conversation_preserves_zero_usage_chunks_and_exact_execution_scope():
    from opentab.conversations.reader import window
    from opentab.stores.copilot_events import node_id

    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, "otel")
        # No OTEL is needed to read retained messages through the leaf reader.
        text = "  Hello 🚀\n```python\nprint('hello')\n```\n" + "x" * 5000
        events = [
            _event("user.message", {"content": text, "transformedContent": "HIDDEN SYSTEM"}, "u"),
            _event(
                "assistant.message",
                {
                    "messageId": "shared",
                    "content": "first chunk",
                    "apiCallId": "r",
                    "reasoningText": "SECRET THINKING",
                    "encryptedContent": "SECRET ENCRYPTED",
                },
                "a1",
            ),
            _event(
                "assistant.message",
                {"messageId": "shared", "content": "second chunk", "apiCallId": "r"},
                "a2",
            ),
            _event("assistant.message", {"content": "zero-usage reply"}, "zero"),
            _event("model.response", {"content": "SECRET INTERNAL TITLE"}, "internal"),
            _event("assistant.message_delta", {"deltaContent": "SECRET STREAM"}, "delta"),
            _event("tool.execution_complete", {"result": {"content": "SECRET TOOL"}}, "tool"),
            _event(
                "subagent.started",
                {"toolCallId": "spawn", "agentName": "same-name"},
                "start-child",
                "child",
            ),
            _event("user.message", {"content": "child instructions"}, "cu", "child"),
            _event("assistant.message", {"content": "child retained reply"}, "ca", "child"),
            _event(
                "subagent.started",
                {"toolCallId": "nested-spawn", "parentId": "child", "agentName": "same-name"},
                "start-nested",
                "nested",
            ),
            _event(
                "user.message",
                {"content": "nested legacy prompt", "parentToolCallId": "nested-spawn"},
                "nu",
            ),
            _event("subagent.started", {"toolCallId": "empty-spawn"}, "empty", "empty-child"),
            _event("assistant.message", {"content": "SECRET UNOWNED"}, "foreign", "missing"),
        ]
        path = _write_events(otel, "s", events)
        store = ot.CopilotStore(otel, _copilot_args(otel))
        assert store.supports_conversation("s") and not store.supports_conversation("absent")
        source = store.conversation_source("s")
        assert [r["parts"][0]["text"] for r in source["records"]] == [
            text,
            "first chunk",
            "second chunk",
            "zero-usage reply",
        ]
        assert "SECRET" not in json.dumps(source) and "HIDDEN SYSTEM" not in json.dumps(source)
        child, nested, empty = (node_id("s", aid) for aid in ("child", "nested", "empty-child"))
        assert {e["id"] for e in source["executions"]} == {"s", child, nested, empty}
        assert [
            r["parts"][0]["text"] for r in store.conversation_source("s", child)["records"]
        ] == ["child instructions", "child retained reply"]
        assert (
            store.conversation_source("s", nested)["records"][0]["parts"][0]["text"]
            == "nested legacy prompt"
        )
        assert store.conversation_source("s", empty)["records"] == []
        _conversation_error(
            "invalid_execution", lambda: store.conversation_source("s", "same-name")
        )
        _conversation_error(
            "invalid_execution", lambda: store.conversation_source("s", node_id("other", "child"))
        )
        _conversation_error(
            "ambiguous_anchor", lambda: window(source, root_key="key", anchor="shared")
        )
        first = window(source, root_key="key", limit=1, max_chars=41)
        parts = [first["records"][0]["parts"][0]["text"]]
        page = first
        while page["records"][0]["parts"][0]["text_offset"] + len(
            page["records"][0]["parts"][0]["text"]
        ) < len(text):
            page = window(
                source, root_key="key", cursor=page["next_cursor"], limit=1, max_chars=1000
            )
            parts.append(page["records"][0]["parts"][0]["text"])
        assert "".join(parts) == text
        assert source["records"][0]["source"]["line"] == 2
        with patch.object(store, "conversation_source", side_effect=AssertionError("raw read")):
            assert store.conversation_manifest("s")
        events[3]["data"]["content"] = "changed reply"
        _write_events(otel, "s", events)
        changed = store.conversation_source("s")
        assert changed["snapshot"] != source["snapshot"]
        _conversation_error(
            "stale_cursor", lambda: window(changed, root_key="key", cursor=first["next_cursor"])
        )
        assert Path(path).is_file()


def test_copilot_conversation_rejects_foreign_headers_symlinks_cycles_and_demo():
    from opentab.stores.copilot_events import node_id

    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, "otel")
        events = [
            _event("subagent.started", {"toolCallId": "a", "parentId": "b"}, "spawn-a", "a"),
            _event("subagent.started", {"toolCallId": "b", "parentId": "a"}, "spawn-b", "b"),
            _event("user.message", {"content": "SECRET CYCLIC"}, "bad", "a"),
            dict(_event("user.message", {"content": "SECRET MALFORMED"}, "invalid"), agentId=4),
            _event(
                "subagent.started",
                {"toolCallId": "bad-parent", "parentId": []},
                "bad-parent-start",
                "bad-parent",
            ),
            _event(
                "user.message", {"content": "SECRET BAD PARENT"}, "bad-parent-user", "bad-parent"
            ),
            _event("subagent.started", {"toolCallId": "literal-question"}, "question-start", "?"),
            _event(
                "user.message",
                {"content": "SECRET BAD LEGACY", "parentToolCallId": "unknown"},
                "bad-legacy",
            ),
            _event("user.message", {"content": "root"}, "root"),
        ]
        path = _write_events(otel, "s", events)
        store = ot.CopilotStore(otel, _copilot_args(otel))
        assert "SECRET" not in json.dumps(store.conversation_source("s"))
        _conversation_error(
            "invalid_execution", lambda: store.conversation_source("s", node_id("s", "a"))
        )
        _write_events(
            otel, "s", events + [_event("session.start", {"sessionId": "foreign"}, "foreign")]
        )
        _conversation_error("invalid_execution", lambda: store.conversation_source("s"))
        foreign = Path(tmp) / "foreign.jsonl"
        Path(path).rename(foreign)
        Path(path).symlink_to(os.path.relpath(foreign, Path(path).parent))
        assert not store.supports_conversation("s")
        _conversation_error("invalid_execution", lambda: store.conversation_source("s"))
        _conversation_error("invalid_execution", lambda: store.conversation_source("../foreign"))
        store.demo = True
        with patch.object(store._events, "path", side_effect=AssertionError("demo raw path")):
            _conversation_error("conversation_unavailable", lambda: store.conversation_source("s"))
            assert store.conversation_manifest("s") is None


def test_copilot_fresh_source_limits_malformed_records_and_cancellation():
    import threading

    from opentab.conversations import reader
    from opentab.stores.copilot_changes import _counts

    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, "otel")
        path = _write_events(otel, "s", [_event("user.message", {"content": "root"}, "user")])
        store = ot.CopilotStore(otel, _copilot_args(otel))
        with open(path, "a", encoding="utf-8") as stream:
            stream.write('invalid JSON\n{"type":"user.message","data":null}\n')
        source = store.conversation_source("s")
        assert len(source["records"]) == 1
        assert "malformed_jsonl_records_skipped" in source["limitations"]
        assert "malformed_session_events_skipped" in source["limitations"]
        with patch.object(reader, "MAX_SOURCE_BYTES", 10):
            _conversation_error("conversation_too_large", lambda: store.conversation_source("s"))
        with patch.object(reader, "MAX_LINE_BYTES", 10):
            _conversation_error("conversation_too_large", lambda: store.conversation_source("s"))

        class CancelDuringRead:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls >= 3

        assert store.change_request("s")(CancelDuringRead()) is None
        cancelled = threading.Event()
        cancelled.set()
        _conversation_error(
            "read_cancelled", lambda: reader.read_jsonl([path], cancelled=cancelled)
        )
        with open(path, "a", encoding="utf-8") as stream:
            stream.write('{"type":"session.start","data":null}\n')
        _conversation_error("invalid_execution", lambda: store.conversation_source("s"))
    assert _counts("@@ -1 +1 @@\n-old\n+new\n+undeclared\n") == (None, None)
    assert _counts("@@ -2,2 +2 @@\n-a\n-b\n+c\n") == (1, 2)


def test_copilot_cost_and_goto_price_model_switches_and_resolve_child_roots():
    from opentab.cli.main import _goto_target, parse_args, status_line
    from opentab.stores.copilot_events import node_id

    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, "otel")
        _write_otel(
            otel,
            [
                _otel_chat("s", "gpt-4.1", 1000, 100, resp="one", end=(1775934000, 0)),
                _otel_chat(
                    "s",
                    "claude-sonnet-4",
                    2000,
                    200,
                    resp="two",
                    trace="t2",
                    span="s2",
                    end=(1775934100, 0),
                ),
                _otel_chat("older", "gpt-4.1", 100, 10, trace="t3", span="s3", end=(1775933000, 0)),
            ],
        )
        _write_events(
            otel,
            "s",
            [
                _event("subagent.started", {"toolCallId": "spawn"}, "spawn", "child"),
                _event(
                    "assistant.message",
                    {"apiCallId": "two", "model": "claude-sonnet-4", "content": "child"},
                    "a",
                    "child",
                ),
            ],
        )
        con = sqlite3.connect(os.path.join(tmp, "session-store.db"))
        con.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, summary TEXT)")
        con.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?)", [("s", tmp, "latest"), ("older", tmp, "older")]
        )
        con.commit()
        con.close()
        store = ot.CopilotStore(otel, _copilot_args(otel))
        with patch.object(
            store._events, "details", side_effect=AssertionError("cost read raw detail")
        ):
            expected = ot.money(
                ot.api_equivalent_cost("openai/gpt-4.1", 1000, 100, 0, 0, 0)
                + ot.api_equivalent_cost("anthropic/claude-sonnet-4", 2000, 200, 0, 0, 0)
            )
            assert status_line(store, "s") == "~" + expected
            assert status_line(store, node_id("s", "child")) == "~" + expected
            assert status_line(store, tmp) == "~" + expected
            assert store.root_of("child") == "s" and store.root_of("absent") is None
            assert [r["id"] for r in store.recent_roots()] == ["s", "older"]
        args = parse_args(
            ["--harness", "copilot", "--copilot-dir", otel, "--goto", "s", "--tab", "turns"]
        )
        assert _goto_target(args) == ("copilot", "s")
        args.goto = tmp
        assert _goto_target(args) == ("copilot", "s")
        args.goto = "absent"
        assert _goto_target(args) is None
        args = parse_args(["cost", "--harness", "copilot", "--copilot-dir", otel, "s"])
        assert args.copilot_dir == otel and args.source == "copilot"


def _native_patch(before, after, body):
    # The installed Copilot diffFormatGit emits these unquoted absolute-path headers.
    old = "dev/null" if before is None else before.lstrip("/")
    new = "dev/null" if after is None else after.lstrip("/")
    return f"\ndiff --git a/{old} b/{new}\nindex 0000000..0000000 100644\n--- a/{old}\n+++ b/{new}\n{body}\n"


def _edit_events(name, args, output, *, prefix="edit", agent="", success=True, turn="1", mcp=False):
    request = {"toolCallId": prefix, "name": name, "arguments": args}
    if mcp:
        request.update(mcpServerName="impostor", mcpToolName="edit")
    return [
        _event(
            "assistant.message",
            {"messageId": prefix + "-message", "toolRequests": [request], "turnId": turn},
            prefix + "-request",
            agent,
        ),
        _event(
            "tool.execution_start",
            {"toolCallId": prefix, "toolName": name, "arguments": args, "turnId": turn},
            prefix + "-start",
            agent,
        ),
        _event(
            "tool.execution_complete",
            {
                "toolCallId": prefix,
                "success": success,
                "result": {"content": "short", "detailedContent": output},
                "turnId": turn,
            },
            prefix + "-done",
            agent,
        ),
    ]


def test_copilot_changes_show_native_diffs_moves_child_edits_and_missing_patches():
    from opentab.stores.copilot_events import node_id

    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, "otel")
        a, new, old = (os.path.join(tmp, name) for name in ("a file.py", "new.py", "old.py"))
        patch_a = _native_patch(a, a, "@@ -1,2 +1,2 @@\n context\n-old\n+new\n")
        patch_new = _native_patch(None, new, "@@ -0,0 +1,1 @@\n+created\n")
        patch_move = _native_patch(old, new, "@@ -1,1 +1,1 @@\n-old\n+moved\n")
        events = [
            _event("session.context_changed", {"cwd": tmp}, "cwd"),
            *_edit_events(
                "edit", {"path": "a file.py", "old_str": "old", "new_str": "new"}, patch_a
            ),
            *_edit_events(
                "create", {"path": "new.py", "file_text": "created"}, patch_new, prefix="create"
            ),
            _event("subagent.started", {"toolCallId": "spawn"}, "spawn", "child"),
            *_edit_events(
                "apply_patch",
                "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n@@\n-old\n+moved\n*** Delete File: gone.py\n*** End Patch\n",
                patch_move,
                prefix="move",
                agent="child",
            ),
            *_edit_events(
                "edit", {"path": "missing.py"}, "only a concise result", prefix="missing"
            ),
            *_edit_events("edit", {"path": "failed.py"}, patch_a, prefix="failed", success=False),
            *_edit_events("edit", {"path": "impostor.py"}, patch_a, prefix="mcp", mcp=True),
            *_edit_events("bash", {"command": "touch shell.py"}, "done", prefix="shell"),
            *_edit_events(
                "str_replace_editor", {"command": "view", "path": "view.py"}, patch_a, prefix="view"
            ),
            # Live Copilot's read-only view output is also diff-shaped.
            *_edit_events("view", {"path": "a file.py"}, patch_a, prefix="native-view"),
        ]
        _write_events(otel, "s", events)
        store = ot.CopilotStore(otel, _copilot_args(otel))
        assert store.supports_changes("s")
        data = store.session_change_files("s")
        files = {f["file"]: f for f in data["files"]}
        assert set(files) == {"a file.py", "new.py", "gone.py", "missing.py"}
        assert files["a file.py"]["additions"] == files["a file.py"]["deletions"] == 1
        assert files["new.py"]["status"] == "mixed" and len(files["new.py"]["edits"]) == 2
        edit = files["new.py"]["edits"][1]
        assert edit["from_file"] == "old.py" and edit["execution_id"] == node_id("s", "child")
        assert store.session_change_diff("s", edit["key"])["patch"].strip() == patch_move.strip()
        assert (
            files["missing.py"]["additions"] is None
            and not files["missing.py"]["edits"][0]["available"]
        )
        assert store.session_change_diff("s", files["gone.py"]["edits"][0]["key"]) is None
        assert "created" not in json.dumps(data) and "context" not in json.dumps(data)
        assert store.session_change_diff("foreign", edit["key"]) is None
        assert store.session_change_diff("s", "bad-key") is None
        # Raw reads do not require opening any files in the working tree.
        assert not Path(a).exists() and not Path(new).exists()
        # A frozen worker owns its reader and remains independent of a demo switch.
        import threading

        request = store.change_request("s")
        assert request(threading.Event()) == data
        store.demo = True
        assert store.session_change_files("s")["files"] == []
        assert store.session_change_diff("s", edit["key"]) is None
        assert store.change_request("s") is None
        cancelled = threading.Event()
        cancelled.set()
        assert request(cancelled) is None


def test_copilot_changes_reused_ids_stale_keys_and_output_limits():
    from opentab.stores import copilot_changes as changes

    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, "otel")
        path = os.path.join(tmp, "owned.py")
        native = _native_patch(path, path, "@@ -1,1 +1,1 @@\n-old\n+new\n")
        old = _edit_events("edit", {"path": path}, native, prefix="reused")
        events = old[:2] + [
            _event(
                "assistant.message",
                {
                    "toolRequests": [
                        {"toolCallId": "reused", "name": "bash", "arguments": {"command": "other"}}
                    ]
                },
                "new-call",
            ),
            old[-1],
            *_edit_events("edit", {"path": path}, native, prefix="valid"),
            *_edit_events(
                "edit", {"path": os.path.join(tmp, "unmatched.py")}, native, prefix="wrong-path"
            ),
            *_edit_events("edit", {"path": path}, native, prefix="foreign", agent="unknown"),
        ]
        event_path = _write_events(otel, "s", events)
        store = ot.CopilotStore(otel, _copilot_args(otel))
        files = store.session_change_files("s")["files"]
        owned = next(f for f in files if f["file"] == path)
        assert len(owned["edits"]) == 1
        key = owned["edits"][0]["key"]
        with patch.object(changes, "DIFF_BYTES", 50):
            diff = store.session_change_diff("s", key)
            assert diff["truncated"] and len(diff["patch"].encode()) <= 50
        with patch.object(changes, "SUMMARY_LIMIT", 1):
            data = store.session_change_files("s")
            assert data["truncated"] and len(data["files"]) == 1
        with open(event_path, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(_event("user.message", {"content": "new activity"}, "later")) + "\n"
            )
        assert store.session_change_diff("s", key) is None
        assert store.session_change_files("s")["files"][0]["edits"][0]["key"] != key


def test_copilot_conversation_service_index_search_copy_and_permission_gates():
    from opentab.api.service import ServiceError
    from opentab.tui.exporting import conversation_markdown

    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, "otel")
        _write_otel(otel, [_otel_chat("s", "gpt-4.1", 10, 2, resp="r")])
        _write_events(
            otel,
            "s",
            [
                _event("user.message", {"content": "find copilot needle"}, "u"),
                _event("assistant.message", {"content": "zero usage retained answer"}, "a"),
            ],
        )
        store = ot.CopilotStore(otel, _copilot_args(otel))
        args = SimpleNamespace(source="copilot", demo=False, no_state=True)
        env = {
            f"XDG_{name}_HOME": os.path.join(tmp, name.lower())
            for name in ("CACHE", "CONFIG", "DATA", "STATE")
        }
        with patch.dict(os.environ, env):
            denied = ot.OpenTabService(store, args)
            try:
                denied.session_conversation("s")
            except ServiceError as exc:
                assert exc.code == "raw_content_disabled"
            else:
                raise AssertionError("raw content must be gated")
            service = ot.OpenTabService(store, args, allow_raw_content=True)
            assert service.get_session("s")["capabilities"]["conversation"]
            source = service.session_conversation("s")
            markdown, count = conversation_markdown(source["records"])
            assert count == 2
            assert "## User" in markdown and "zero usage retained answer" in markdown
            report = service.index_conversations(harness="copilot")
            assert report["complete"] and report["updated"] == 1
            with patch.object(
                store,
                "conversation_source",
                side_effect=AssertionError("unchanged manifest opened text"),
            ):
                assert service.index_conversations(harness="copilot")["unchanged"] == 1
            hits = service.search_conversations("needle", harness="copilot")["hits"]
            assert len(hits) == 1 and hits[0]["execution_id"] == "s"
            assert hits[0]["match_fields"] == ["text"]
