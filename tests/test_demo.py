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

    app.store.demo = False
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
    demo_store = FakeStore([workflow("a", "2026-06-01 12:00:00")])
    demo_store.demo = True
    demo_store.demo_cats = ot.demo.DEMO_ALL
    app._store_cache = {("opencode", ot.demo.DEMO_ALL): demo_store}  # demo-all, pre-cached
    app.query = "Acme acquisition"

    app.toggle_demo()  # real -> demo-everything

    assert app.store.demo
    assert app.query == ""
    assert "demo mode" in app.notice


def test_demo_categories_gate_titles_turns_and_spend():
    from opentab.demo import demo_config, scramble_workflow
    from opentab.models import Workflow

    def wf():
        return Workflow(
            id="s1",
            title="real title",
            directory="/home/me/repo",
            created_at="2026-07-01 10:00:00",
            root_cost=5.0,
            total_cost=5.0,
            subagents=0,
            model_count=1,
            total_tokens=1000,
            unpriced_tokens=0,
        )

    # spend pins the scale (identity unless spend is scrambled), so cost only moves
    # when spend is on; titles only rename when titles is on.
    def scrambled(spec):
        _en, scale, cats = demo_config(type("A", (), {"demo": spec})())
        w = scramble_workflow(wf(), scale, cats)
        return w.title != "real title", w.total_cost != 5.0

    assert demo_config(type("A", (), {"demo": "titles"})())[1] == 1.0  # no spend -> real $
    assert scrambled("titles") == (True, False)  # fake name, real cost
    assert scrambled("spend") == (False, True)  # real name, scaled cost
    assert scrambled("turns") == (False, False)  # neither touches a workflow row
    assert scrambled("all")[0] and scrambled("all")[1]  # everything moves
    # an unknown category falls back to all, never a silent no-op demo
    _en, _sc, cats = demo_config(type("A", (), {"demo": "bogus"})())
    assert cats == ot.demo.DEMO_ALL


def test_demo_scale_env_override_pins_the_hidden_factor():
    # $OPENTAB_DEMO_SCALE pins the otherwise-random magnitude factor so a chaptered
    # capture (or a set of screenshots) shows ONE consistent scale across launches --
    # including --goto launches, whose throwaway probe stores would otherwise perturb
    # the RNG and shift the factor. A malformed/non-positive value falls back to the
    # random draw (never to a real-magnitude 1.0).
    from opentab.demo import demo_config

    def scale_for(spec):
        return demo_config(type("A", (), {"demo": spec})())[1]

    saved = os.environ.get("OPENTAB_DEMO_SCALE")
    try:
        os.environ["OPENTAB_DEMO_SCALE"] = "2.5"
        assert scale_for("all") == 2.5  # pinned exactly
        assert scale_for("spend") == 2.5
        assert scale_for("titles") == 1.0  # override only bites when spend is scrambled
        # empty / non-numeric / non-positive / non-finite all fall back to the random draw
        # (inf/nan would overflow tokens*scale and crash), never to a real-magnitude 1.0.
        for bad in ("", "not-a-number", "-1", "0", "inf", "-inf", "nan", "1e309"):
            os.environ["OPENTAB_DEMO_SCALE"] = bad
            s = scale_for("all")
            assert (3.0**-1.0) - 1e-9 <= s <= (3.0**1.0) + 1e-9  # random draw, not the env value
    finally:
        if saved is None:
            os.environ.pop("OPENTAB_DEMO_SCALE", None)
        else:
            os.environ["OPENTAB_DEMO_SCALE"] = saved


def test_demo_picker_toggles_categories_and_applies_the_subset():
    # Drive the D picker: uncheck spend, apply, and confirm the store is built for
    # the {titles, turns} subset (the make_store args carry that spec).
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.source_key = "opencode"
    specs = []
    real_make_store = ot.sources.make_store
    try:
        built = FakeStore([workflow("a", "2026-06-01 12:00:00")])
        built.demo = True
        built.demo_cats = frozenset({"titles", "turns"})
        ot.sources.make_store = lambda a, key: specs.append(a.demo) or (built, "")

        app.open_demo_menu()
        assert app.demo_menu and app.demo_menu_sel == set(ot.demo.DEMO_ALL)  # seeded all
        app.handle_key(None, ord("j"))  # titles -> turns
        app.handle_key(None, ord("j"))  # turns -> spend
        app.handle_key(None, ord(" "))  # uncheck spend
        assert app.demo_menu_sel == {"titles", "turns"}
        app.handle_key(None, 10)
        assert not app.demo_menu and app.store is built
        assert specs == ["titles,turns"]  # the store was built for exactly that subset
        assert app.notice == "demo: titles, turns"
    finally:
        ot.sources.make_store = real_make_store


def test_fleet_rebuild_uses_the_demo_state_key_not_a_bool():
    # Regression: _rebuild_fleet_store must key the rebuilt store on the demo *state*
    # (None / the category frozenset), like select_source and the picker. A bool key
    # both stranded the fresh store (a later D swap-back showed stale data) and, in a
    # partial demo, crashed _args_with_demo's sorted(state).
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.source_key = "remote"
    demo_store = FakeStore([workflow("a", "2026-06-01 12:00:00")])
    demo_store.demo = True
    demo_store.demo_cats = ot.demo.DEMO_ALL
    app.store = demo_store
    seen = []
    real_make = ot.sources.make_store
    try:
        fresh = FakeStore([workflow("a", "2026-06-01 12:00:00")])
        fresh.demo = True
        fresh.demo_cats = ot.demo.DEMO_ALL
        ot.sources.make_store = lambda a, key: seen.append(a.demo) or (fresh, "")
        app._rebuild_fleet_store()  # must not raise (a bool state would hit sorted(True))
        assert app.store is fresh
        assert ("remote", ot.demo.DEMO_ALL) in app._store_cache  # state key, not (…, True)
        assert seen == ["spend,titles,turns"]  # a spec string reached make_store, never True
    finally:
        ot.sources.make_store = real_make


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
    assert ot.demo_model("ollama/llama3.1:70b") == ot.demo_model("ollama/llama3.1:70b")
    assert ot.demo_model("anthropic/claude-opus-4.6") == "anthropic/claude-opus-4.6"
    assert ot.demo_model("github-copilot/claude-sonnet-4.5") == "github-copilot/claude-sonnet-4.5"


def test_demo_title_and_dir_are_deterministic():
    assert ot.demo_title("ses_1") == ot.demo_title("ses_1")
    assert " " in ot.demo_title("ses_1")  # "<verb> <noun>"
    assert ot.demo_dir("ses_1") in ot.DEMO_REPOS


def test_demo_machine_is_stable_and_collision_free():
    # Unlike titles/dirs, machine names must NOT collide: two boxes folding onto one fake
    # would merge their spend and could hide the whole Machines view. Deterministic per
    # name, "" stays "", and distinct names stay distinct (a pool word + hash suffix).
    assert ot.demo_machine("laptop") == ot.demo_machine("laptop")
    assert ot.demo_machine("") == ""
    assert ot.demo_machine("laptop") != "laptop"  # never leaks the real name
    base, _, suffix = ot.demo_machine("laptop").rpartition("-")  # "<pool word>-<crc32 hex>"
    assert base in ot.DEMO_MACHINES and len(suffix) == 8 and int(suffix, 16) >= 0
    # The suffix is the FULL crc32, not a truncation: a truncated `% 100000` collapsed the
    # space (the pool index is derived from it) and collided host-89 with host-111.
    assert ot.demo_machine("host-89") != ot.demo_machine("host-111")
    assert ot.demo_machine("alpha") != ot.demo_machine("epsilon")
    names = [
        "alpha",
        "epsilon",
        "laptop",
        "server",
        "desktop",
        "vps",
        "host-89",
        "host-111",
        "nas-01",
    ]
    assert len({ot.demo_machine(n) for n in names}) == len(names)


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


def test_lit_demo_key_leaves_in_one_press_and_remembers_the_categories():
    # Leaving demo used to mean D, uncheck every row, Enter -- three keys to undo a
    # thing nobody picks parts of. Lit, D is now the off switch the other lit keys
    # ($ T P) are, and the categories survive it so coming back is D-Enter.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.source_key = "opencode"
    real = app.store
    partial = FakeStore([workflow("a", "2026-06-01 12:00:00")])
    partial.demo = True
    partial.demo_cats = frozenset({"titles"})
    real_make_store = ot.sources.make_store
    try:
        ot.sources.make_store = lambda a, key: ((partial, "") if a.demo else (real, ""))
        app.open_demo_menu()
        app.handle_key(None, ord("j"))  # titles -> turns
        app.handle_key(None, ord(" "))  # uncheck turns
        app.handle_key(None, ord("j"))  # turns -> spend
        app.handle_key(None, ord(" "))  # uncheck spend
        app.handle_key(None, 10)  # Enter: demo on, titles only
        assert app.store is partial and app.notice == "demo: titles"

        app.handle_key(None, ord("D"))  # one press out
        assert not app.demo_menu and not getattr(app.store, "demo", False)
        assert app.notice == "real data"

        app.handle_key(None, ord("D"))  # back in: the picker re-offers what you had
        assert app.demo_menu and app.demo_menu_sel == {"titles"}
    finally:
        ot.sources.make_store = real_make_store


def test_capital_d_opens_the_picker_that_toggles_real_and_demo():
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
    demo.demo_cats = ot.demo.DEMO_ALL
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(real, args, source_key="opencode")
    app.view = "zoom"
    app.focus = "months"
    app.tab = app.current_tabs().index("Models")
    real_make_store = ot.sources.make_store
    calls = []
    try:
        ot.sources.make_store = lambda a, key: calls.append((bool(a.demo), key)) or (
            demo if a.demo else real,
            "",
        )

        app.handle_key(None, ord("D"))
        assert app.demo_menu and app.store is real
        app.handle_key(None, 10)
        assert not app.demo_menu and app.store is demo
        assert app.view == "zoom" and app.current_tabs()[app.tab] == "Models"
        assert {w.title for w in app.loaded} == {"demo one", "demo two"}
        assert app.notice == "demo mode"

        app.tab = app.current_tabs().index("Sessions")
        app.workflow_index = 1
        assert app.current_session().id == "ses_1"

        # D again, with demo lit, is a plain off switch -- one press, no picker.
        app.handle_key(None, ord("D"))
        assert not app.demo_menu and app.store is real
        assert app.view == "zoom" and app.current_tabs()[app.tab] == "Sessions"
        assert app.current_session().id == "ses_1"
        assert {w.title for w in app.loaded} == {"real one", "real two"}
        assert app.notice == "real data"
        assert calls == [(True, "opencode")]  # only demo was built; real was already cached
    finally:
        ot.sources.make_store = real_make_store
