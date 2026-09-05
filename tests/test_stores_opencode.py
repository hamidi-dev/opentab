import json
import os
import sqlite3
import tempfile

import opentab as ot
from opentab.stores.opencode import REQUIRED_SCHEMA

from tests._support import (
    PI_SID,
    FakeStore,
    _jsonl_args,
    _pi_args,
    _pi_assistant,
    _pi_session,
    _pi_user,
    _pi_write,
    _write_jsonl,
    _write_opencode_db_with_tools,
    _write_opencode_db_with_turns,
)


def test_reconcile_makes_models_sum_to_session_total():
    app = ot.App.__new__(ot.App)

    class _Store:
        demo = True

    app.store = _Store()
    app.loaded = [
        ot.Workflow(
            id="r",
            title="t",
            directory="d",
            created_at="2026-01-01",
            root_cost=0.0,
            total_cost=100.0,
            subagents=0,
            model_count=1,
            total_tokens=1000,
            unpriced_tokens=0,
        )
    ]
    app._model_by_root = {
        "r": [
            {
                "model_name": "m1",
                "runs": 1,
                "cost": 0.0,
                "tokens_total": 0,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
            },
        ]
    }
    app._reconcile_demo_models()
    rows = app._model_by_root["r"]
    assert round(sum(r["cost"] for r in rows), 2) == 100.0
    assert sum(r["tokens_total"] for r in rows) == 1000


def test_tool_breakdown_even_splits_parallel_tool_calls():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert store.supports_tool_breakdown
        rows = {r["tool"]: r for r in store.tool_breakdown("s1")}
        # m1's 2M tokens split across its two tools -> 1M each; bash also gets m2's 6M.
        assert round(rows["bash"]["tokens_total"]) == 7_000_000
        assert round(rows["serena_read_file"]["tokens_total"]) == 1_000_000
        assert rows["bash"]["calls"] == 2
        assert rows["serena_read_file"]["calls"] == 1
        # Only the priced step carries real cost; it lands on bash, serena stays $0.
        assert rows["bash"]["cost"] == 6.0
        assert rows["serena_read_file"]["cost"] == 0
        # Attributed tokens reconcile to the tool-calling steps' totals (2M + 6M).
        assert round(sum(r["tokens_total"] for r in rows.values())) == 8_000_000


def test_tools_tab_offered_only_with_part_table():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        app = ot.App(ot.Store(db, type("A", (), {"demo": False})()), args())
        app.view = "session"
        assert app.current_tabs() == (
            "Overview",
            "Subagents",
            "Turns",
            "Tools",
            "Context",
        )
    # A backend without the part table / support flag never shows the tabs.
    bare = ot.App(FakeStore([]), args())
    bare.view = "session"
    assert "Tools" not in bare.current_tabs()
    assert "Turns" not in bare.current_tabs()
    assert "Context" not in bare.current_tabs()  # the curve needs turn rows


def test_message_timeline_orders_by_time_and_marks_subagent_turns():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert store.supports_turns("s1")
        rows = store.message_timeline("s1")
        # chronological (t=1000, 1500, 2000), NOT insertion order (m2,m1,m3)
        assert [r["tokens_total"] for r in rows] == [1_000_000, 2_000_000, 500_000]
        assert [r["cost"] for r in rows] == [0, 0, 3.0]
        # the middle turn is the subagent (depth 1, its session's agent label)
        assert [r["depth"] for r in rows] == [0, 1, 0]
        assert rows[1]["agent"] == "explore"
        assert rows[0]["agent"] == "-" and rows[2]["agent"] == "-"
        assert rows[1]["model_name"] == "anthropic/claude-haiku-4.5"
        # each turn is tagged with the user prompt that owns it (most recent in time):
        # u1 (summary.title) owns m1 + the subagent m3; u2 owns the later m2.
        assert [r["prompt_title"] for r in rows] == [
            "Add feature X",
            "Add feature X",
            "Fix the bug",
        ]
        assert rows[0]["prompt_id"] == "u1" and rows[2]["prompt_id"] == "u2"


def test_turn_rows_carry_the_reasoning_variant_opencode_records():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        rows = store.message_timeline("s1")
        # Per MESSAGE, so a mid-session switch shows up as one: m1 ran high, m2 medium.
        # The variant-less subagent turn reports "" and is NOT back-filled from the
        # session's current model setting -- that would invent a level for 200 messages
        # on a real corpus (131 of them Claude rows, which have no variant at all), and
        # an invented level feeds the cache-miss verdict, printing a ⚙ marker that
        # blames a decision nobody made.
        assert [r["effort"] for r in rows] == ["high", "", "medium"]
        # The whole-corpus batch shares _timeline_columns, so an export cannot ship a
        # different set of columns than the TUI reads.
        assert store.message_timeline_all()["s1"] == rows


def test_turn_rows_carry_the_tools_each_step_called():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        rows = store.message_timeline("s1")
        assert [r["tools"] for r in rows] == [["bash", "serena_read_file"], ["bash"]]

        # The whole-corpus batch resolves them from ONE scan of `part`, and must agree
        # with the per-session path exactly -- they are the same table, and an export
        # that disagreed with the TUI about which tools a turn called would be worse
        # than one that shipped none.
        assert store.message_timeline_all()["s1"] == rows


def test_the_tool_join_is_a_separate_scan_not_a_per_row_subquery():
    # Tool names must come from one grouped part-table scan, never one query per message.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        seen = []
        store.conn.set_trace_callback(lambda sql: seen.append(" ".join(str(sql).split())))
        store.message_timeline_all()
        store.conn.set_trace_callback(None)
        # Exactly one statement reads the tools, and it is a standalone grouped scan.
        tool_scans = [s for s in seen if "'$.tool'" in s]
        assert len(tool_scans) == 1, tool_scans
        assert tool_scans[0].startswith("select message_id"), tool_scans[0]
        assert "m.id" not in tool_scans[0]  # nothing correlated to the message row

        # ...and the timeline query itself never grew a per-row tools cell. It keeps its
        # ONE pre-existing `part` subquery, for the raw prompt text -- which is gated on
        # `role = 'user'`, so it is evaluated for the prompts, not for every turn.
        (timeline,) = (s for s in seen if "tokens_total" in s)
        assert "'$.tool'" not in timeline
        assert timeline.count("from part") == 1


def test_turn_rows_carry_no_tools_when_the_schema_has_no_part_table():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            create table session (
              id text primary key, parent_id text, title text, directory text, agent text,
              time_created integer
            );
            create table message (id text primary key, session_id text, data text);
            """
        )
        conn.execute(
            "insert into session values (?,?,?,?,?,?)",
            ("s1", None, "Root", "/work/repo", None, 1760000000000),
        )
        conn.execute(
            "insert into message values (?,?,?)",
            (
                "m1",
                "s1",
                '{"role":"assistant","providerID":"anthropic","modelID":"claude-haiku-4.5",'
                '"cost":1.0,"time":{"created":1000},"tokens":{"input":10,"output":1}}',
            ),
        )
        conn.commit()
        conn.close()
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert store.supports_tool_breakdown is False
        assert [r["tools"] for r in store.message_timeline("s1")] == [[]]


def test_message_timeline_all_matches_the_per_session_path():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        batch = store.message_timeline_all()
        assert set(batch) == {"s1"}  # s2 is a subagent -> folded under root s1, not keyed
        assert batch["s1"] == store.message_timeline("s1")  # exact, incl. the s2 depth-1 turn
        assert [r["depth"] for r in batch["s1"]] == [0, 1, 0]


def test_a_subagents_task_message_does_not_open_a_prompt_of_its_own():
    # A subagent user message is an agent-authored task, not a human prompt boundary.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "insert into message values (?,?,?)",
            (
                "u3",
                "s2",  # the SUBAGENT session: agent-authored, and it precedes m3
                '{"role":"user","time":{"created":1200},'
                '"summary":{"title":"Search the auth files"}}',
            ),
        )
        conn.commit()
        conn.close()
        store = ot.Store(db, type("A", (), {"demo": False})())
        rows = store.message_timeline("s1")
        # The subagent turn still belongs to the human prompt that spawned it, and the
        # later main-thread turn still belongs to the prompt that follows -- the task
        # text never becomes a group of its own.
        assert [r["prompt_title"] for r in rows] == [
            "Add feature X",
            "Add feature X",
            "Fix the bug",
        ]
        assert "Search the auth files" not in [r["prompt_title"] for r in rows]
        assert [r["prompt_id"] for r in rows] == ["u1", "u1", "u2"]
        # and the batch export path, which shares _process_timeline, agrees exactly
        assert store.message_timeline_all()["s1"] == rows


def _write_opencode_db_with_long_prompt(path, long_prompt):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table session (
          id text primary key, parent_id text, title text, directory text, agent text,
          time_created integer
        );
        create table message (id text primary key, session_id text, data text);
        create table part (id text primary key, message_id text, session_id text, data text);
        """
    )
    conn.execute(
        "insert into session values (?,?,?,?,?,?)",
        ("s1", None, "Root", "/work/repo", None, 1760000000000),
    )
    user = {"role": "user", "time": {"created": 500}}
    part = {"type": "text", "text": long_prompt}
    turn = {
        "role": "assistant",
        "providerID": "anthropic",
        "modelID": "claude-opus-4-8",
        "cost": 2.0,
        "time": {"created": 1000},
        "tokens": {"input": 100, "output": 10},
    }
    conn.executemany(
        "insert into message values (?,?,?)",
        [("u1", "s1", json.dumps(user)), ("m1", "s1", json.dumps(turn))],
    )
    conn.execute("insert into part values (?,?,?,?)", ("p1", "u1", "s1", json.dumps(part)))
    conn.commit()
    conn.close()


def test_opencode_turns_carry_the_full_prompt_uncapped():
    long_prompt = ("rework the cache invalidation and explain the tradeoffs " * 5).strip()
    long_prompt += "\nthen run the whole suite"
    assert len(long_prompt) > 200
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_long_prompt(db, long_prompt)
        store = ot.Store(db, type("A", (), {"demo": False})())
        rows = store.message_timeline("s1")
        assert rows[0]["prompt_full"] == long_prompt
        assert rows[0]["prompt_title"] == " ".join(long_prompt.split())[:160]

        # The table shows the prompt CAPPED to its cell; the whole text lives in the
        # popup Enter opens, so a pasted essay can never push the table off the pane.
        app = ot.App(store, args())
        rnd = app.renderer  # the instance _apply_click resolves rows against
        wf = app.loaded[0]
        table = rnd.detail_turns(wf, 96)
        assert not any(ln.startswith("  │") for ln in table)
        assert not any(" ".join(long_prompt.split()) in ln for ln in table)  # capped
        app.open_turn_drill(0)
        body = " ".join(rnd.detail_turn_drill(wf, 90))
        assert "then run the whole suite" in body  # the tail survived
        assert " ".join(long_prompt.split()) in " ".join(body.split())  # nothing lost
        app.turn_drill = None
        rnd.detail_turns(wf, 96)  # a paint pass records the row line indices
        idx, pid = next(iter(rnd._turn_header_at.items()))
        app._apply_click(("turnline", idx), drill=False)
        assert app.turn_drill == pid
        assert "then run the whole suite" in " ".join(rnd.detail_turn_drill(wf, 90))
        assert rnd.detail_turns(wf, 96) == rnd.detail_turns(wf, 96)


def test_store_reads_db_without_session_token_columns():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            create table session (
              id text primary key,
              parent_id text,
              title text,
              directory text,
              time_created integer
            );
            create table message (session_id text, data text);
            """
        )
        conn.executemany(
            "insert into session values (?, ?, ?, ?, ?)",
            [
                ("root", None, "Root", "/tmp/project", 1760000000000),
                ("child", "root", "Child", "/tmp/project", 1760000001000),
            ],
        )
        conn.executemany(
            "insert into message values (?, ?)",
            [
                (
                    "root",
                    '{"role":"assistant","providerID":"openai","modelID":"gpt-5-mini","cost":1.25,"tokens":{"total":10,"input":4,"output":6}}',
                ),
                (
                    "child",
                    '{"role":"assistant","providerID":"anthropic","modelID":"claude-sonnet-4.5","cost":0,"tokens":{"total":5,"input":2,"output":3}}',
                ),
            ],
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {"demo": False})()
        store = ot.Store(db, args)
        workflows = store.workflows()
        nodes = store.workflow_nodes("root")

        assert len(workflows) == 1
        assert workflows[0].total_cost == 1.25
        assert workflows[0].root_cost == 1.25
        assert workflows[0].total_tokens == 15
        assert workflows[0].unpriced_tokens == 5
        assert workflows[0].subagents == 1
        assert nodes[1]["tokens_total"] == 5
        assert nodes[1]["agent"] == "-"


def test_workflows_ended_at_is_the_latest_update_in_the_subtree():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            create table session (
              id text primary key,
              parent_id text,
              title text,
              directory text,
              time_created integer,
              time_updated integer
            );
            create table message (session_id text, data text);
            """
        )
        conn.executemany(
            "insert into session values (?, ?, ?, ?, ?, ?)",
            [
                # root's own time_updated is earlier than its subagent child's --
                # ended_at must reflect the child bumping the whole subtree.
                ("root", None, "Root", "/tmp/project", 1760000000000, 1760000001000),
                ("child", "root", "Child", "/tmp/project", 1760000000500, 1760000005000),
            ],
        )
        conn.executemany(
            "insert into message values (?, ?)",
            [
                (
                    "root",
                    '{"role":"assistant","providerID":"openai","modelID":"gpt-5-mini","cost":1.0,"tokens":{"input":1,"output":1}}',
                ),
                (
                    "child",
                    '{"role":"assistant","providerID":"openai","modelID":"gpt-5-mini","cost":0,"tokens":{"input":1,"output":1}}',
                ),
            ],
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {"demo": False})()
        store = ot.Store(db, args)
        workflow = store.workflows()[0]

        # localtime rendering is TZ-dependent, so compare against sqlite's own
        # conversion of each raw epoch-ms rather than a hardcoded wall-clock string.
        conn = sqlite3.connect(db)
        expected_created = conn.execute(
            "select datetime(1760000000000 / 1000, 'unixepoch', 'localtime')"
        ).fetchone()[0]
        expected_ended_at = conn.execute(
            "select datetime(1760000005000 / 1000, 'unixepoch', 'localtime')"
        ).fetchone()[0]
        conn.close()

        assert workflow.created_at == expected_created
        assert workflow.ended_at == expected_ended_at
        assert workflow.ended_at > workflow.created_at


def test_workflows_ended_at_is_blank_without_time_updated():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            create table session (
              id text primary key, parent_id text, title text, directory text,
              time_created integer
            );
            create table message (session_id text, data text);
            """
        )
        conn.execute(
            "insert into session values (?, ?, ?, ?, ?)",
            ("root", None, "Root", "/tmp/project", 1760000000000),
        )
        conn.execute(
            "insert into message values (?, ?)",
            (
                "root",
                '{"role":"assistant","providerID":"openai","modelID":"gpt-5-mini","cost":1.0,"tokens":{"input":1,"output":1}}',
            ),
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {"demo": False})()
        store = ot.Store(db, args)
        workflow = store.workflows()[0]

        # Legacy schema: the only signal is the creation time itself, so ended_at is
        # blanked rather than reported as a same-as-start value (the "last_activity"
        # sort's own empty-string check is what falls it back to created_at, not this
        # store).
        assert workflow.ended_at == ""


def test_records_cost_probe_runs_lazily_not_at_construction():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "s1",
                    "model": "gpt-4o",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cost_usd": 0.05,
                }
            ],
        )
        calls = []
        orig = ot.JsonlStore._probe_records_cost
        ot.JsonlStore._probe_records_cost = lambda self: (calls.append(1), orig(self))[1]
        try:
            store = ot.JsonlStore(path, _jsonl_args())
            assert calls == []  # constructing must not read the file
            assert store.records_cost is True  # first read probes...
            assert store.records_cost is True and calls == [1]  # ...and the answer sticks

            # Parsed first (the cold-start order): the answer derives from the parse's
            # accumulated per-model costs and the probe never runs at all.
            calls.clear()
            store2 = ot.JsonlStore(path, _jsonl_args())
            store2.workflows()
            assert store2.records_cost is True and calls == []
        finally:
            ot.JsonlStore._probe_records_cost = orig

        # pi's parse-derived answer honors the metered/subscription split like the probe:
        # a codex-plan cost is a list-price estimate, not spend -> records_cost False.
        root = os.path.join(tmp, "pi-sessions")
        _pi_write(
            root,
            "--proj--",
            PI_SID,
            [
                _pi_session(PI_SID, tmp),
                _pi_user("hi"),
                _pi_assistant("openai/gpt-5", 10, 5, cost=0.01, provider="openai-codex"),
            ],
        )
        sub = ot.PiStore(root, _pi_args())
        sub.workflows()  # parse first: no probe needed
        assert sub.records_cost is False


def _minimal_db(path, session_cols, message_cols, rows=True):
    conn = sqlite3.connect(path)
    conn.execute("create table session (%s)" % ", ".join(session_cols))
    conn.execute("create table message (%s)" % ", ".join(message_cols))
    if rows:
        data = json.dumps(
            {
                "role": "assistant",
                "providerID": "anthropic",
                "modelID": "claude-opus-4-6",
                "tokens": {"input": 10, "output": 5, "cache": {"read": 0, "write": 0}},
                "time": {"created": 1780000000000},
            }
        )
        conn.execute(
            "insert into session (id, parent_id, time_created) values ('s1', null, 1780000000000)"
        )
        conn.execute("insert into message (id, session_id, data) values ('m1', 's1', ?)", (data,))
    conn.commit()
    conn.close()


def test_required_schema_is_exactly_what_every_query_path_needs():
    args = type("A", (), {"demo": False})()
    session_cols = list(REQUIRED_SCHEMA["session"])
    message_cols = list(REQUIRED_SCHEMA["message"])

    with tempfile.TemporaryDirectory() as tmp:
        # NOT TOO SMALL: a database with only these columns answers every method,
        # including the Turns/Tools opt-ins its supports_* gates say it offers.
        db = os.path.join(tmp, "minimum.db")
        _minimal_db(db, session_cols, message_cols)
        store = ot.Store(db, args)
        workflows = store.workflows()
        assert [w.id for w in workflows] == ["s1"] and workflows[0].total_tokens == 15
        assert store.summary(workflows)["tokens"] == 15
        assert len(store.model_breakdown()) == 1
        assert len(store.workflow_nodes("s1")) == 1
        assert store.root_of("s1") == "s1" and len(store.recent_roots()) == 1
        assert store.supports_turns("s1") and len(store.message_timeline("s1")) == 1
        assert store.supports_tools("s1") is False  # no `part` table: probed, not required
        assert store.tool_breakdown("s1") == []

        # NOT TOO LARGE: drop any one of them and a query path really does break, so
        # none of them is there "just in case".
        for table, col in [("session", c) for c in session_cols] + [
            ("message", c) for c in message_cols
        ]:
            path = os.path.join(tmp, f"without-{table}-{col}.db")
            _minimal_db(
                path,
                [c for c in session_cols if not (table == "session" and c == col)] or ["dummy"],
                [c for c in message_cols if not (table == "message" and c == col)] or ["dummy"],
                rows=False,
            )
            assert ot.sources.opencode_db_verdict(path)[0] == "foreign", f"{table}.{col}"
            try:
                broken = ot.Store(path, args)
                broken.workflows()
                broken.model_breakdown()
                broken.workflow_nodes("x")
                broken.message_timeline("x")
                broken.recent_roots()
            except sqlite3.Error:
                pass  # what the verdict is standing in front of
            else:
                raise AssertionError(f"{table}.{col} is required by nothing -- drop it")


def _write_opencode_trace_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table session (
          id text primary key, parent_id text, title text, directory text, agent text,
          time_created integer
        );
        create table message (id text primary key, session_id text, data text);
        create table part (id text primary key, message_id text, session_id text, data text);
        """
    )
    conn.execute(
        "insert into session values (?,?,?,?,?,?)",
        ("s1", None, "Root", "/work/repo", None, 1760000000000),
    )
    user = {"role": "user", "time": {"created": 500}}
    turn = {
        "role": "assistant",
        "providerID": "anthropic",
        "modelID": "claude-opus-4-8",
        "cost": 2.0,
        "time": {"created": 1000},
        "tokens": {"input": 100, "output": 10},
    }
    conn.executemany(
        "insert into message values (?,?,?)",
        [("u1", "s1", json.dumps(user)), ("m1", "s1", json.dumps(turn))],
    )
    parts = [
        ("p0", "u1", {"type": "text", "text": "fix the cache"}),
        # OpenCode is the one backend of the three that keeps reasoning PROSE.
        ("p1", "m1", {"type": "reasoning", "text": "**Planning** I should look first."}),
        ("p2", "m1", {"type": "text", "text": "Checking the diff."}),
        (
            "p3",
            "m1",
            {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "git diff --stat", "description": "the diff"},
                    "output": "3 files changed",
                },
            },
        ),
        # step-start/step-finish parts carry no content and must not become events.
        ("p4", "m1", {"type": "step-finish", "reason": "tool-calls"}),
    ]
    conn.executemany(
        "insert into part values (?,?,?,?)",
        [(pid, mid, "s1", json.dumps(data)) for pid, mid, data in parts],
    )
    conn.commit()
    conn.close()


def test_opencode_turn_content_reads_narration_reasoning_and_each_calls_arguments():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_trace_db(db)
        store = ot.Store(db, type("A", (), {"demo": False})())

        assert store.supports_turn_content("s1") is True
        assert store.records_reasoning is True
        row = store.message_timeline("s1")[0]
        # The message id is what the part table joins on, so it is the turn's identity.
        assert row["content_key"] == "m1"
        assert row["has_text"] is True and row["has_reasoning"] is True
        events = store.turn_content("s1")["m1"]

        assert [e["kind"] for e in events] == ["reasoning", "text", "tool"]
        assert events[0]["text"].startswith("**Planning**")
        assert events[2]["name"] == "bash" and events[2]["args"] == "git diff --stat"
        assert events[2]["params"] == [("description", "the diff")]
        assert events[2]["output"] == "3 files changed"
        # The user's own prompt part belongs to the prompt header, not to a turn.
        assert "u1" not in store.turn_content("s1")


def test_expanding_one_opencode_turn_recovers_errors_arguments_and_all_events():
    from opentab.util import TRACE_EVENTS_CAP, TRACE_OUTPUT_CAP

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_trace_db(db)
        command = "    python <<'PY'\n" + "print('a  b')\n" * 100 + "PY\n"
        failure = "Permission denied\n" + "details\n" * 800
        params = {f"arg{i}": "x" * 600 for i in range(8)}
        with sqlite3.connect(db) as conn:
            data = {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "error",
                    "error": failure,
                    "input": dict(params, command=command),
                },
            }
            conn.execute("UPDATE part SET data=? WHERE id='p3'", (json.dumps(data),))
            conn.execute(
                "INSERT INTO message SELECT 'm2', session_id, data FROM message WHERE id='m1'"
            )
            conn.execute(
                "INSERT INTO part VALUES ('other', 'm2', 's1', ?)",
                (json.dumps({"type": "text", "text": "other turn"}),),
            )
            conn.executemany(
                "INSERT INTO part VALUES (?, 'm1', 's1', ?)",
                [
                    (f"extra{i}", json.dumps({"type": "text", "text": f"event {i}"}))
                    for i in range(TRACE_EVENTS_CAP)
                ],
            )
        store = ot.Store(db, type("A", (), {"demo": False})())
        preview = store.turn_content("s1")
        assert preview["m1"][2]["status"] == "error"
        assert preview["m1"][2]["output"].startswith("Permission denied")
        assert len(preview["m1"][2]["output"]) <= TRACE_OUTPUT_CAP
        assert len(preview["m1"]) == TRACE_EVENTS_CAP
        full = store.turn_content("s1", content_key="m1")
        assert list(full) == ["m1"] and len(full["m1"]) == TRACE_EVENTS_CAP + 3
        call = full["m1"][2]
        assert call["args"] == command and dict(call["params"]) == params
        assert call["output"] == failure and call["output_dropped"] == 0
        assert store.turn_content("s1") == preview


def test_a_whitespace_only_part_never_marks_a_turn_as_readable():
    # The Read column and the trace must agree: SQLite's one-argument trim() removes
    # ASCII spaces only, so a part holding "\t\n" or a vertical tab marked the row
    # readable and then opened onto nothing. The explicit set covers every ASCII
    # whitespace character Python's str.strip() removes.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_trace_db(db)
        conn = sqlite3.connect(db)
        turn = {
            "role": "assistant",
            "providerID": "anthropic",
            "modelID": "claude-opus-4-8",
            "cost": 1.0,
            "time": {"created": 2000},
            "tokens": {"input": 10, "output": 5},
        }
        conn.execute("insert into message values (?,?,?)", ("m2", "s1", json.dumps(turn)))
        # Every ASCII whitespace character str.strip() removes, the four control
        # SEPARATORS included -- verified against Python over the whole ASCII range.
        for n, blank in enumerate(("   ", "\t\n", "\x0b\x0c", "\x1c\x1f", "\r", "")):
            conn.execute(
                "insert into part values (?,?,?,?)",
                (f"w{n}", "m2", "s1", json.dumps({"type": "text", "text": blank})),
            )
        conn.commit()
        conn.close()
        store = ot.Store(db, type("A", (), {"demo": False})())

        row = next(r for r in store.message_timeline("s1") if r["content_key"] == "m2")
        assert row["has_text"] is False and row["has_reasoning"] is False
        # ...and the trace agrees: nothing to show behind it.
        assert store.turn_content("s1").get("m2", []) == []
