"""The GitHub Copilot CLI OpenTelemetry backend (stores/copilot.py)."""

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
        "gen_ai.usage.input_tokens": inp,  # OpenAI-style: includes the cached read
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
        # input_tokens (19452) includes the 123-token cached read -> uncached 19329;
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
        # tokens_total = uncached(19329) + cache_read(123) + cache_write(25) + output(281+128)
        assert w.total_tokens == w.unpriced_tokens == 19329 + 123 + 25 + 409

        row = next(r for r in store.model_breakdown() if r["root_id"] == COPILOT_SID)
        assert row["model_name"] == "anthropic/claude-sonnet-4"  # mixed-provider prefix
        assert row["unpriced_input"] == 19329
        assert row["unpriced_cache_read"] == 123
        assert row["unpriced_cache_write"] == 25
        assert row["unpriced_output"] == 409  # reasoning folded in, priced once
        assert row["reasoning"] == 0  # folded, never double-counted

        # the (all-unpriced) usage reprices to a positive list-price estimate under "$"
        est = ot.api_equivalent_cost("anthropic/claude-sonnet-4", 19329, 409, 0, 123, 25)
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
    """A response id names ONE call; a trace id groups a whole turn, and an agentic turn
    makes several calls on one trace. Coverage was (same trace) OR (same response), so a
    single chat span suppressed every other call logged under its trace -- real tokens
    gone. Only a matching response counts now, with the trace still standing in when the
    covering record named no response at all (OTel makes it recommended, not required)."""
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
    """_SESSION_ATTRS ranks gen_ai.conversation.id above the per-call gen_ai.response.id,
    and _parse keeps the best id per trace -- but _to_candidate threw the priority away
    and consulted the trace only when the record had no session attribute at all. A
    record whose only one was a response id therefore became its own session per LLM
    call, each missing the session-store.db title and cwd keyed by session id."""
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
    """_finalize reads each session's title and cwd from the sibling session-store.db, but
    cache_inputs listed only the OTEL files -- so a rollup parsed before the CLI wrote that
    DB was served from cache forever (reload re-fingerprints, still matches, still hits),
    freezing the session at "(untitled)" / "(unknown)" and out of its project."""
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(otel, [_otel_chat(COPILOT_SID, "gpt-5.4-mini", 60, 10)])
        store = ot.CopilotStore(otel, _copilot_args(otel))
        db = os.path.join(tmp, ".copilot", "session-store.db")
        assert db not in store.cache_inputs()  # absent: nothing to fingerprint yet

        sqlite3.connect(db).close()
        assert db in ot.CopilotStore(otel, _copilot_args(otel)).cache_inputs()
