"""CombinedStore: the merged view, its source tags and per-session routing (stores/combined.py)."""

import os
import sqlite3
import tempfile

import opentab as ot

from tests._support import (
    FakeStore,
    _claude_msg,
    _usage,
    _write_jsonl,
    _write_opencode_db_with_tools,
    _write_opencode_db_with_turns,
    app_with,
    workflow,
)


def test_trend_sources_row_drills_into_that_sources_sessions():
    a = workflow("a", "2026-06-01 12:00:00", cost=5.0)
    b = workflow("b", "2026-06-02 12:00:00", cost=2.0)
    a.source, b.source = "opencode", "claude"
    app = app_with([a, b])
    app.handle_key(None, ord("T"))
    while app.trend_tabs[app.trend_tab] != "Sources":
        app.handle_key(None, ord("l"))
    assert app.trend_ranked_keys() == ["opencode", "claude"]
    app.handle_key(None, ord("j"))
    app.handle_key(None, 10)
    assert app.trend_drill == ("source", "claude")
    assert [w.id for w, _c, _t in app.trend_drill_sessions()] == ["b"]


def test_zoom_sources_tab_navigates_and_drills():
    # The merged view's per-scope Sources tab works like the Trends Sources tab:
    # j/k pick a tool, Enter narrows Sessions to it (scoped), Esc pops back.
    a = workflow("a", "2026-06-01 12:00:00", cost=5)
    b = workflow("b", "2026-06-01 13:00:00", cost=1)
    a.source, b.source = "OpenCode", "Claude Code"
    app = app_with([a, b])
    app.store.combined = True  # the merged view injects the Sources tab
    app.handle_key(None, 10)  # zoom the selected day
    tabs = app.current_tabs()
    app.tab = tabs.index("Sources")
    assert [s for s, _ in app.zoom_source_rows()] == ["OpenCode", "Claude Code"]
    app.handle_key(None, ord("j"))  # j/k drive the source cursor
    assert app.source_index == 1
    app.handle_key(None, 10)  # Enter -> that source's sessions in this scope
    assert app.zoom_source == "Claude Code"
    assert app.current_tabs()[app.tab] == "Sessions"
    assert [w.id for w in app.current_sessions()] == ["b"]
    app.handle_key(None, 27)  # Esc pops the source drill, back to the Sources tab
    assert app.view == "zoom" and app.zoom_source is None
    assert app.current_tabs()[app.tab] == "Sources"
    app._apply_click(("zoomsource", 0), drill=True)  # double-click a source row
    assert app.zoom_source == "OpenCode"
    assert [w.id for w in app.current_sessions()] == ["a"]
    app.handle_key(None, 27)  # pop the drill...
    app.handle_key(None, 27)  # ...then leave the zoom
    assert app.view == "browse" and app.zoom_source is None


def test_sources_tab_counts_the_sessions_it_opens():
    # A Sources row must count exactly what Enter on it opens: it took neither the
    # `i` widening nor a Projects-tab drill, so a row read "1 session · $3" and then
    # produced two sessions and $5.
    class MergedStore(FakeStore):
        combined = True

    a = workflow("a", "2026-06-01 12:00:00", cost=3)
    a.source = "OpenCode"
    b = workflow("b", "2026-06-02 12:00:00", cost=2)
    b.source = "OpenCode"
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(MergedStore([a, b]), args)
    app.focus = "months"
    app.view = "zoom"
    app.ignored_sessions = {"b"}
    app.show_ignored_projects = True
    app._invalidate_workflow_cache()

    rows = app.zoom_source_rows()
    assert [w.id for w in app.current_sessions()] == ["a", "b"]  # what Enter opens
    assert rows[0][0] == "OpenCode"
    assert int(rows[0][1]["sessions"]) == 2 and float(rows[0][1]["cost"]) == 5.0
    # ...and the browse preview of the same tab agrees.
    lines = app.renderer.month_sources(app.selected_month_summary, 96)
    assert any("$5.00" in ln for ln in lines)


def test_source_rows_follow_a_project_drill():
    class MergedStore(FakeStore):
        combined = True

    a = workflow("a", "2026-06-01 12:00:00", cost=3, directory="/tmp/alpha")
    a.source = "OpenCode"
    b = workflow("b", "2026-06-02 12:00:00", cost=2, directory="/tmp/beta")
    b.source = "Claude Code"
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(MergedStore([a, b]), args)
    app.focus = "months"
    app.view = "zoom"
    app.zoom_project = "/tmp/alpha"  # drilled in from the Projects tab

    assert [s for s, _it in app.zoom_source_rows()] == ["OpenCode"]  # not Claude's
    assert [w.id for w in app.current_sessions()] == ["a"]


def test_sources_rows_honour_the_committed_filter():
    # The `f` query narrows the sessions list, so a Sources row that aggregates past
    # it would advertise spend Enter then refuses to open.
    class MergedStore(FakeStore):
        combined = True

    a = workflow("a", "2026-06-01 12:00:00", title="alpha work", cost=3)
    a.source = "OpenCode"
    b = workflow("b", "2026-06-02 12:00:00", title="beta work", cost=7)
    b.source = "OpenCode"
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(MergedStore([a, b]), args)
    app.focus = "months"
    app.view = "zoom"
    app.query = "alpha"  # committed filter

    rows = app.zoom_source_rows()
    assert [w.id for w in app.current_sessions()] == ["a"]  # what Enter opens
    assert int(rows[0][1]["sessions"]) == 1 and float(rows[0][1]["cost"]) == 3.0
    lines = app.renderer.month_sources(app.selected_month_summary, 96)
    assert any("$3.00" in ln for ln in lines) and not any("$10.00" in ln for ln in lines)


def test_combined_demo_shares_one_scale():
    # A merged demo must scale every backend by the SAME hidden factor, or the
    # cross-source ratio (the Sources view) would be distorted by two random scales.
    class Stub:
        def __init__(self, scale):
            self.demo = True
            self.demo_scale = scale
            self.records_cost = False

    a, b = Stub(0.5), Stub(2.0)
    cs = ot.CombinedStore([a, b])
    assert cs.demo is True
    assert a.demo_scale == b.demo_scale == cs.demo_scale  # one shared scale wins
    # non-demo stays unscaled
    plain = ot.CombinedStore([type("S", (), {"records_cost": True})()])
    assert plain.demo is False and plain.demo_scale == 1.0


def test_tools_tab_gated_to_opencode_sessions_in_combined_view():
    # In the merged view the Tools tab must follow the SELECTED session's backend:
    # an OpenCode session offers it, a non-OpenCode session never shows it empty.
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        oc = ot.Store(db, type("A", (), {"demo": False})())
        other = FakeStore([workflow("cc1", "2026-06-01 12:00:00")])  # no tool support
        app = ot.App(ot.CombinedStore([oc, other]), args())
        assert app.store.supports_tools("s1") is True
        assert app.store.supports_tools("cc1") is False
        assert app.session_supports_tools("s1") is True
        assert app.session_supports_tools("cc1") is False


def test_turns_tab_gated_per_session_in_combined_view():
    # Like the Tools tab, Turns follows the SELECTED session's backend: OpenCode (and
    # Claude) offer it; a backend without message_timeline never shows it.
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        oc = ot.Store(db, type("A", (), {"demo": False})())
        other = FakeStore([workflow("x1", "2026-06-01 12:00:00")])  # no timeline support
        app = ot.App(ot.CombinedStore([oc, other]), args())
        assert app.store.supports_turns("s1") is True
        assert app.store.supports_turns("x1") is False
        assert app.session_supports_turns("s1") is True
        assert app.session_supports_turns("x1") is False
        assert app.store.message_timeline("x1") == []


def test_codex_in_combined_view_carries_a_cx_source_tag():
    a = workflow("a", "2026-06-01 12:00:00", title="opencode session")
    a.source = "OpenCode"
    b = workflow("b", "2026-06-02 12:00:00", title="codex session")
    b.source = "Codex"

    class MergedStore(FakeStore):
        combined = True

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(MergedStore([a, b]), args)
    month = app.months[0]
    lines = app.renderer.month_workflows(month, 120)
    assert any("cx " in ln and "codex session" in ln for ln in lines)  # Src column abbrev
    over = app.renderer.month_overview(month, 120)
    assert any("[cx] codex session" in ln for ln in over)  # Top Sessions bracket tag


def test_sources_tab_appears_in_combined_view_and_aggregates_by_source():
    a = workflow("a", "2026-06-01 12:00:00", title="opencode session", cost=3, tokens=300)
    a.source = "OpenCode"
    b = workflow("b", "2026-06-01 09:00:00", title="codex session", cost=0, tokens=200)
    b.source = "Codex"

    class MergedStore(FakeStore):
        combined = True

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(MergedStore([a, b]), args)

    # The tab joins right after Overview in every aggregate detail view.
    app.focus = "months"
    assert app.current_tabs()[:2] == ("Overview", "Sources")
    app.focus = "days"
    assert app.current_tabs()[:2] == ("Overview", "Sources")
    app.set_browse_mode("projects")
    assert app.current_tabs()[:2] == ("Overview", "Sources")

    # It renders a per-source breakdown scoped to that slice.
    month = app.months[0]
    lines = app.renderer.month_sources(month, 120)
    assert lines[0].startswith("# Spend by source")
    assert any("OpenCode" in ln for ln in lines)
    assert any("Codex" in ln for ln in lines)


def test_sources_tab_is_hidden_with_a_single_backend():
    # One backend -> every row is the same source (a 100% bar), so the tab is noise.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])  # FakeStore: not combined
    app.focus = "months"
    assert "Sources" not in app.current_tabs()
    app.focus = "days"
    assert "Sources" not in app.current_tabs()
    app.set_browse_mode("projects")
    assert "Sources" not in app.current_tabs()


def test_year_sources_tab_appears_in_combined_view():
    a = workflow("a", "2026-06-01 12:00:00", title="oc", cost=3)
    a.source = "OpenCode"
    b = workflow("b", "2026-03-01 09:00:00", title="cx", cost=0)
    b.source = "Codex"

    class MergedStore(FakeStore):
        combined = True

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(MergedStore([a, b]), args)
    app.focus = "years"
    assert app.current_tabs()[:2] == ("Overview", "Sources")
    lines = app.renderer.year_sources(app.selected_year_summary, 100)
    assert lines[0].startswith("# Spend by source")
    assert any("OpenCode" in ln for ln in lines) and any("Codex" in ln for ln in lines)


def test_combined_store_merges_sources_and_routes_workflow_nodes():
    with tempfile.TemporaryDirectory() as tmp:
        # an OpenCode SQLite source...
        db = os.path.join(tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            create table session (id text primary key, parent_id text, title text,
              directory text, time_created integer);
            create table message (session_id text, data text);
            """
        )
        conn.execute(
            "insert into session values (?, ?, ?, ?, ?)",
            ("ses_oc", None, "OC", "/tmp/project", 1760000000000),
        )
        conn.execute(
            "insert into message values (?, ?)",
            (
                "ses_oc",
                '{"role":"assistant","providerID":"openai","modelID":"gpt-5-mini","cost":1.25,'
                '"tokens":{"total":10,"input":4,"output":6}}',
            ),
        )
        conn.commit()
        conn.close()
        # ...and a Claude Code source
        cdir = os.path.join(tmp, "projects", "slug")
        os.makedirs(cdir)
        msg = _claude_msg("cc-uuid", "claude-opus-4-8", _usage(1000, 500, 0, 0), uuid="u1", cwd=tmp)
        _write_jsonl(os.path.join(cdir, "cc.jsonl"), [msg])

        args = type("Args", (), {"demo": False})()
        oc, cc = ot.Store(db, args), ot.ClaudeStore(os.path.join(tmp, "projects"), args)
        store = ot.CombinedStore([oc, cc])

        workflows = store.workflows()
        ids = {w.id for w in workflows}
        assert ids == {"ses_oc", "cc-uuid"}  # both sources merged
        assert store.combined and not store.records_cost  # Claude in the mix

        # summary sums recorded cost across both: OpenCode's $1.25 + Claude's $0
        # (Claude is unpriced until "$" reprices it; tested at App level elsewhere)
        summary = store.summary(workflows)
        assert summary["workflows"] == 2
        assert abs(summary["cost"] - 1.25) < 1e-9
        assert summary["unpriced_tokens"] == 1500  # all of Claude's tokens

        # workflow_nodes routes each id to the backend that produced it
        oc_nodes = store.workflow_nodes("ses_oc")
        cc_nodes = store.workflow_nodes("cc-uuid")
        assert oc_nodes[0]["model_name"].startswith("openai/")
        assert cc_nodes[0]["model_name"] == "anthropic/claude-opus-4-8"

        # model_breakdown concatenates rows from both, keyed by their real root ids
        roots = {r["root_id"] for r in store.model_breakdown()}
        assert roots == {"ses_oc", "cc-uuid"}


def test_combined_sessions_tables_get_a_src_column():
    class MergedStore(FakeStore):
        combined = True

    a = workflow("a", "2026-06-01 12:00:00", title="opencode session")
    a.source = "OpenCode"
    b = workflow("b", "2026-06-02 12:00:00", title="claude session")
    b.source = "Claude Code"
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(MergedStore([a, b]), args)
    month = app.months[0]
    lines = app.renderer.month_workflows(month, 120)
    assert "Src" in lines[0]  # header gains the column
    assert any("oc " in ln and "opencode session" in ln for ln in lines)
    assert any("cc " in ln and "claude session" in ln for ln in lines)
    # Top Sessions in the overview carries the bracket tag instead
    over = app.renderer.month_overview(month, 120)
    assert any("[cc] claude session" in ln for ln in over)
    # single-source views stay untouched (origin is implied by the header chip)
    plain = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert "Src" not in plain.renderer.month_workflows(plain.months[0], 120)[0]
