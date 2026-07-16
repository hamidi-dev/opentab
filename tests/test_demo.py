"""Demo mode: deterministic anonymisation and the hidden per-process scale (demo.py)."""

import os
import sqlite3
import tempfile

import opentab as ot

from tests._support import FakeStore, _app_on_session, _select_session, app_with, workflow


def test_demo_toggle_hides_and_locks_notes_live():
    # `D` swaps the store under a running app. Demo fakes every title and path but the
    # session IDS STAY REAL, so a note left loaded would be the one true thing on an
    # anonymised screen — and editable, writing real annotations from "safe" mode. The
    # gate is therefore computed per store, not captured at startup.
    assert ot.save_notes({"a": "real money, real client"})
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00")], "a")
    app.notes_enabled = True
    app.refresh_notes()
    assert app.allow_notes and app.note_for("a") == "real money, real client"
    assert app.renderer.note_tag(app.current_session()) == "✎ "

    app.store.demo = True  # what toggle_demo does, minus the store rebuild
    app._reload_for_source()
    assert not app.allow_notes
    assert app.notes == {}  # nothing to leak: no ✎, no note in the Overview
    assert app.renderer.note_tag(app.workflows[0]) == ""
    _select_session(app, "a")  # (the reload resets the view to the top)
    assert app.handle_key(None, ord("n"))  # and `n` is inert while demo is on
    assert "demo" in app.notice
    assert ot.load_notes() == {"a": "real money, real client"}  # the file is untouched

    app.store.demo = False  # back to real data: the notes come back, same run
    app._reload_for_source()
    assert app.allow_notes and app.note_for("a") == "real money, real client"
    ot.save_notes({})


def test_demo_drops_a_filter_query_you_typed():
    # The query is text YOU typed — out of a real title, path, or note — and the header
    # paints it. Demo's whole job is that the screen can be shared, so "filter: Acme
    # acquisition" must not survive onto the anonymised view (where it matches nothing
    # anyway, the titles being fakes).
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.source_key = "opencode"
    app._store_cache = {("opencode", True): FakeStore([workflow("a", "2026-06-01 12:00:00")])}
    app._store_cache[("opencode", True)].demo = True
    app.query = "Acme acquisition"

    app.toggle_demo()

    assert app.store.demo
    assert app.query == ""
    assert "demo mode" in app.notice


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


def test_demo_scale_hides_real_magnitudes_consistently():
    # Demo mode must not leave enough real data to reconstruct actual spend: every
    # cost and token is multiplied by one hidden factor, consistently across the
    # workflow totals, the model mix, and the subagent nodes. We force the factor so
    # the assertions are deterministic.
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
              cost real default 0 not null,
              tokens_input integer default 0 not null,
              tokens_output integer default 0 not null,
              tokens_reasoning integer default 0 not null,
              tokens_cache_read integer default 0 not null,
              tokens_cache_write integer default 0 not null
            );
            create table message (session_id text, data text);
            """
        )
        conn.executemany(
            "insert into session values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "root",
                    None,
                    "Root",
                    "/work/secret-repo",
                    1760000000000,
                    10.0,
                    2_000_000,
                    0,
                    0,
                    0,
                    0,
                ),
                (
                    "child",
                    "root",
                    "Child",
                    "/work/secret-repo",
                    1760000001000,
                    4.0,
                    1_000_000,
                    0,
                    0,
                    0,
                    0,
                ),
            ],
        )
        conn.executemany(
            "insert into message values (?, ?)",
            [
                (
                    "root",
                    '{"role":"assistant","providerID":"anthropic","modelID":"claude-opus-4.5","cost":10.0,"tokens":{"input":2000000,"output":0}}',
                ),
                (
                    "child",
                    '{"role":"assistant","providerID":"anthropic","modelID":"claude-sonnet-4.5","cost":4.0,"tokens":{"input":1000000,"output":0}}',
                ),
            ],
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {"since": None, "until": None, "days": None})

        real = ot.App(ot.Store(db, type("A", (), {"demo": False})()), args())
        real._ensure_models()
        rw = real.loaded[0]

        store = ot.Store(db, type("A", (), {"demo": True})())
        store.demo_scale = 0.5  # pin the otherwise-random hidden factor
        demo = ot.App(store, args())
        demo._ensure_models()
        dw = demo.loaded[0]

        # Workflow totals are scaled, so the screen no longer shows real spend.
        assert dw.total_cost == round(rw.total_cost * 0.5, 4)
        assert dw.root_cost == round(rw.root_cost * 0.5, 4)
        assert dw.total_tokens == int(round(rw.total_tokens * 0.5))
        assert dw.total_cost != rw.total_cost  # genuinely obscured, not a no-op

        # Model mix carries the same factor (so tokens x list price can't recover it).
        real_mix = {m["model_name"]: m for m in real.model_mix("root")}
        for dm in demo.model_mix("root"):
            rm = real_mix[dm["model_name"]]  # anthropic names pass through unrenamed
            assert dm["cost"] == round(rm["cost"] * 0.5, 4)
            assert dm["tokens_total"] == int(round(rm["tokens_total"] * 0.5))

        # Subagent execution rows (the Subagents tab / CSV) are scaled too.
        real_child = next(r for r in real.store.workflow_nodes("root") if r["depth"] > 0)
        demo_child = next(r for r in store.workflow_nodes("root") if r["depth"] > 0)
        assert demo_child["cost"] == round(real_child["cost"] * 0.5, 4)
        assert demo_child["tokens_total"] == int(round(real_child["tokens_total"] * 0.5))


def test_demo_turns_anonymize_the_full_prompt_too():
    # Demo must never leak a real prompt through the expandable full text: both the
    # title and prompt_full become the same stable fake.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.store.demo_scale = 0.5
    rows = app._scale_demo_turns(
        "a",
        [
            {
                "model_name": "anthropic/claude-opus-4-8",
                "prompt_id": "p1",
                "prompt_title": "company secret plan",
                "prompt_full": "company secret plan\nwith all the details",
                "cost": 1.0,
                "tokens_total": 10,
                "input": 10,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
            }
        ],
    )
    assert "secret" not in rows[0]["prompt_title"] and "secret" not in rows[0]["prompt_full"]
    assert rows[0]["prompt_full"] == rows[0]["prompt_title"]  # the fake, twice


def test_capital_d_toggles_real_and_demo_store():
    real = FakeStore(
        [
            workflow("ses_1", "2026-06-01 12:00:00", title="real one", cost=1.0),
            workflow("ses_2", "2026-06-02 12:00:00", title="real two", cost=2.0),
        ]
    )
    demo = FakeStore(
        [
            workflow("ses_1", "2026-06-01 12:00:00", title="demo one", cost=1.0),
            workflow("ses_2", "2026-06-02 12:00:00", title="demo two", cost=2.0),
        ]
    )
    demo.demo = True
    demo.demo_scale = 2.0
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(real, args, source_key="opencode")
    app.view = "zoom"
    app.focus = "months"
    app.tab = app.current_tabs().index("Models")
    real_make_store = ot.sources.make_store
    calls = []
    try:
        ot.sources.make_store = lambda a, key: calls.append((a.demo, key)) or (
            demo if a.demo else real,
            "",
        )

        app.handle_key(None, ord("D"))
        assert app.store is demo
        assert app.view == "zoom" and app.current_tabs()[app.tab] == "Models"
        assert {w.title for w in app.loaded} == {"demo one", "demo two"}
        assert app.notice == "demo mode"

        app.tab = app.current_tabs().index("Sessions")
        app.workflow_index = 1
        assert app.current_session().id == "ses_1"

        app.handle_key(None, ord("D"))
        assert app.store is real
        assert app.view == "zoom" and app.current_tabs()[app.tab] == "Sessions"
        assert app.current_session().id == "ses_1"
        assert {w.title for w in app.loaded} == {"real one", "real two"}
        assert app.notice == "real data"
        assert calls == [(True, "opencode")]  # real store was already cached
    finally:
        ot.sources.make_store = real_make_store
