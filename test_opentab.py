"""Unit tests for opentab's pure helpers and demo-mode logic.

Runs under pytest *or* standalone (`python test_opentab.py`) so CI needs no
third-party test runner -- in keeping with opentab's stdlib-only spirit.
The module under test has no .py extension, so we load it by path.
"""

import os
import sqlite3
import tempfile
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
ot = SourceFileLoader("opentab", os.path.join(HERE, "opentab")).load_module()


def test_human_tokens():
    assert ot.human_tokens(999) == "999"
    assert ot.human_tokens(1_500) == "1.5k"
    assert ot.human_tokens(2_000_000) == "2.0M"
    assert ot.human_tokens(3_000_000_000) == "3.0B"


def test_money_is_two_decimals():
    assert ot.money(195.6915) == "$195.69"
    assert ot.money(0) == "$0.00"
    assert ot.money(1_234_567.5) == "$1,234,567.50"


def test_demo_cost_zero_and_deterministic():
    assert ot.demo_cost(0, "seed") == 0.0
    a = ot.demo_cost(1_000_000, "seed")
    b = ot.demo_cost(1_000_000, "seed")
    assert a == b and a > 0
    # different seeds jitter differently (almost always)
    assert ot.demo_cost(1_000_000, "seed") != ot.demo_cost(1_000_000, "other")


def test_demo_model_remaps_local_only():
    assert ot.demo_model("ollama/llama3.1:70b") in ot.DEMO_MODEL_POOL
    assert ot.demo_model("lmstudio/whatever") in ot.DEMO_MODEL_POOL
    # stable per source name
    assert ot.demo_model("ollama/llama3.1:70b") == ot.demo_model("ollama/llama3.1:70b")
    # cloud models pass through untouched
    assert ot.demo_model("anthropic/claude-opus-4.6") == "anthropic/claude-opus-4.6"
    assert ot.demo_model("github-copilot/claude-sonnet-4.5") == "github-copilot/claude-sonnet-4.5"


def test_demo_title_and_dir_are_deterministic():
    assert ot.demo_title("ses_1") == ot.demo_title("ses_1")
    assert " " in ot.demo_title("ses_1")  # "<verb> <noun>"
    assert ot.demo_dir("ses_1") in ot.DEMO_REPOS


def test_demo_rename_merges_colliding_models():
    rows = [
        {
            "model_name": "ollama/x",
            "runs": 2,
            "cost": 0,
            "tokens_total": 10,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
        },
        {
            "model_name": "ollama/x",
            "runs": 3,
            "cost": 0,
            "tokens_total": 5,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
        },
    ]
    out = ot.App._demo_rename_models(rows)
    assert len(out) == 1
    assert out[0]["runs"] == 5 and out[0]["tokens_total"] == 15
    assert out[0]["model_name"] in ot.DEMO_MODEL_POOL


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


if __name__ == "__main__":
    import sys

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
