import json
import os
import sqlite3
import tempfile

import opentab as ot

from tests._support import _write_jsonl

COPILOT_SID = "c623bce1-5906-429f-a517-d4fb2cee7cf7"


def _copilot_args(copilot_dir):
    return type("Args", (), {"demo": False, "copilot_dir": copilot_dir})()


def _otel_chat(
    session,
    model,
    inp,
    out,
    cache_read=0,
    cache_create=0,
    reasoning=0,
    trace="t1",
    span="sp1",
    resp=None,
    end=(1775934264, 0),
):
    # A GenAI `chat` span -- the highest-fidelity per-call OTEL record.
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.conversation.id": session,
        "gen_ai.usage.input_tokens": inp,  # includes cache reads and writes
        "gen_ai.usage.output_tokens": out,
    }
    if cache_read:
        attrs["gen_ai.usage.cache_read.input_tokens"] = cache_read
    if cache_create:
        attrs["gen_ai.usage.cache_creation.input_tokens"] = cache_create
    if reasoning:
        attrs["gen_ai.usage.reasoning.output_tokens"] = reasoning
    if resp:
        attrs["gen_ai.response.id"] = resp
    return {
        "type": "span",
        "traceId": trace,
        "spanId": span,
        "name": f"chat {model}",
        "endTime": list(end),
        "attributes": attrs,
    }


def _write_otel(dirpath, rows, name="otel.jsonl"):
    os.makedirs(dirpath, exist_ok=True)
    _write_jsonl(os.path.join(dirpath, name), rows)


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


def _event(kind, data, eid, agent=""):
    event = {"type": kind, "data": data, "id": eid, "timestamp": "2026-10-02T06:43:22Z"}
    if agent:
        event["agentId"] = agent
    return event


def _write_events(otel, session, events):
    root = os.path.join(os.path.dirname(otel), "session-state", session)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "events.jsonl")
    _write_jsonl(path, [_event("session.start", {"sessionId": session}, "start")] + events)
    return path


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
