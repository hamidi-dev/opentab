"""The OpenCode SQLite backend: schema-adaptive queries, turns and tools (stores/opencode.py)."""

import json
import os
import sqlite3
import tempfile

import opentab as ot

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
        # An OpenCode session offers every per-session tab (Turns, Tools, Context).
        assert app.current_tabs() == (
            "Overview",
            "Models",
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
    # No summary.title on the user message: the one-line group title is the capped
    # raw prompt, prompt_full the whole thing with its line breaks kept.
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

        # The TUI unfolds it: z flips every ▸ header to ▾ + the wrapped whole text,
        # and a click on one header (the "turnline" region) toggles just that group.
        app = ot.App(store, args())
        rnd = app.renderer  # the instance _apply_click resolves headers against
        wf = app.loaded[0]
        folded = rnd.detail_turns(wf, 96)
        assert any(ln.startswith("▸ ") for ln in folded)
        assert not any(ln.startswith("  │") for ln in folded)
        app.turns_full = True
        unfolded = rnd.detail_turns(wf, 96)
        assert any(ln.startswith("▾ ") for ln in unfolded)
        body = " ".join(ln[4:] for ln in unfolded if ln.startswith("  │"))
        assert "then run the whole suite" in body  # the tail survived the unfold
        assert " ".join(long_prompt.split()) == " ".join(body.split())  # nothing lost
        # Click-toggle one group while the global fold is off.
        app.turns_full = False
        rnd.detail_turns(wf, 96)  # a paint pass records the header line indices
        idx, pid = next(iter(rnd._turn_header_at.items()))
        app._apply_click(("turnline", idx), drill=False)
        assert pid in app._turns_expanded
        assert any(ln.startswith("▾ ") for ln in rnd.detail_turns(wf, 96))
        app._apply_click(("turnline", idx), drill=False)  # toggles back off
        assert pid not in app._turns_expanded


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
