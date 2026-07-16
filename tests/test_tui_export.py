"""The `e` CSV export: what it writes for the view you are in."""

import os

import opentab as ot

from tests._support import FakeStore, app_with, workflow


def test_export_dataset_follows_the_visible_view():
    app = app_with(
        [
            workflow("june", "2026-06-01 12:00:00", cost=2, directory="/tmp/a"),
            workflow("may", "2026-05-01 12:00:00", cost=3, directory="/tmp/b"),
        ]
    )

    app.focus = "months"
    app.view = "browse"
    scope, header, rows = app._export_dataset()
    assert scope == "months"
    assert header[0] == "month"
    assert [r[0] for r in rows] == ["2026-06", "2026-05"]  # newest-first
    assert rows[0][1] == 2  # cost column

    app.set_browse_mode("projects")
    scope, header, rows = app._export_dataset()
    assert scope == "projects"
    assert {r[0] for r in rows} == {"/tmp/a", "/tmp/b"}

    app.set_browse_mode("time")
    app.view = "zoom"
    app.focus = "months"
    app.tab = app.month_tabs.index("Projects")
    scope, header, rows = app._export_dataset()
    assert scope == "projects"
    assert header[0] == "directory"
    assert {r[0] for r in rows} == {"/tmp/a"}


def test_export_follows_the_active_panel():
    app = app_with(
        [
            workflow("june", "2026-06-01 12:00:00", cost=2, directory="/tmp/a"),
            workflow("may", "2026-05-01 12:00:00", cost=3, directory="/tmp/b"),
        ]
    )

    # Browse, Years focused -> the years list (previously fell through to days).
    app.view = "browse"
    app.focus = "years"
    scope, header, rows = app._export_dataset()
    assert scope == "years" and header[0] == "year"
    assert [r[0] for r in rows] == ["2026"]

    # Zoom: the active tab decides, not a fixed "sessions".
    app.view = "zoom"
    app.focus = "months"
    app.tab = app.month_tabs.index("Sessions")
    assert app._export_dataset()[0] == "sessions"
    app.tab = app.month_tabs.index("Models")
    scope, header, _ = app._export_dataset()
    assert scope == "models" and header[0] == "model"
    app.tab = app.month_tabs.index("Overview")  # Overview falls back to the session list
    assert app._export_dataset()[0] == "sessions"

    # Session view: the active detail tab decides.
    app.view = "session"
    app.tab = app.workflow_tabs.index("Models")
    scope, header, _ = app._export_dataset()
    assert scope == "models" and header[0] == "model"


def test_export_prices_overlay_exports_the_price_table():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    # Seed two used models so the P table has rows (it lists models you've used).
    app._model_by_root = {
        "a": [
            {"model_name": "anthropic/claude-opus-4-8", "cost": 5.0},
            {"model_name": "openai/gpt-5.3", "cost": 1.0},
        ]
    }
    app.show_prices = True  # the P overlay is open; `e` exports its table

    scope, header, rows = app._export_dataset()
    assert scope == "prices"
    # Rows are deduped to the canonical id, tagged with vendor family + access
    # route(s), and carry the usage share + eff blend beside the four raw rates.
    assert header == [
        "model",
        "family",
        "routes",
        "pinned",
        "share",
        "eff_usd_per_mtok",
        "eff_approx",
        "input",
        "output",
        "cache_read",
        "cache_write",
    ]
    names = [r[0] for r in rows]
    assert "claude-opus-4-8" in names and "gpt-5.3" in names
    assert names[0] == "gpt-5.3"  # cheapest for the mix first (the eff default sort)
    opus = next(r for r in rows if r[0] == "claude-opus-4-8")
    assert opus[1] == "Anthropic" and opus[2] == "anthropic"  # family + route columns
    # every priced row carries pinned/share/eff/approx and four numeric rates
    assert all(len(r) == 11 and all(isinstance(v, (int, float)) for v in r[7:]) for r in rows)
    assert all(isinstance(r[3], bool) and isinstance(r[6], bool) for r in rows)

    # the active P filter narrows the export too (shared priced_model_entries)
    app.query = "gpt"
    assert [r[0] for r in app._export_dataset()[2]] == ["gpt-5.3"]

    # `e` while the overlay is open routes through export_current (overlay stays open)
    import os
    import tempfile

    cwd = os.getcwd()
    os.chdir(tempfile.mkdtemp(prefix="ot-prices-"))
    try:
        app.handle_key(None, ord("e"))
        assert app.show_prices  # still open
        assert "exported" in app.notice
        assert [f for f in os.listdir(".") if f.startswith("opentab-prices-")]
    finally:
        os.chdir(cwd)


def test_export_sources_tab_exports_the_source_breakdown():
    a = workflow("a", "2026-06-01 12:00:00", cost=2)
    b = workflow("b", "2026-06-02 12:00:00", cost=5)
    a.source, b.source = "OpenCode", "Claude Code"
    app = app_with([a, b])
    app.store.combined = True  # the Sources tab only appears in the merged view
    app.view = "zoom"
    app.focus = "months"
    app.tab = app.current_tabs().index("Sources")
    scope, header, rows = app._export_dataset()
    assert scope == "sources"
    assert header == ["source", "cost", "tokens", "sessions"]
    assert {r[0] for r in rows} == {"OpenCode", "Claude Code"}
    assert rows[0][0] == "Claude Code" and rows[0][1] == 5  # cost-sorted, priciest first


def test_export_neutralizes_formula_prefixed_cells():
    # Formula injection: a cell starting with =, +, -, @, tab, or CR is executed
    # by Excel/LibreOffice/Sheets on import. Would-be formulas get a leading
    # apostrophe; plain numbers (negative included) and non-strings pass through.
    safe = ot.App._csv_safe
    assert safe("=SUM(A1:A9)") == "'=SUM(A1:A9)"
    assert safe("+cmd|' /C calc'!A0") == "'+cmd|' /C calc'!A0"
    assert safe("@evil") == "'@evil"
    assert safe("-rm -rf notes") == "'-rm -rf notes"
    assert safe("\t=1+1") == "'\t=1+1"
    assert safe("\r=1+1") == "'\r=1+1"
    assert safe("-1.5") == "-1.5"  # a negative number string is not a formula
    assert safe("+42") == "+42"
    assert safe(-1.5) == -1.5 and safe(0) == 0  # non-strings untouched
    assert safe("session title") == "session title"
    assert safe("") == ""


def test_export_current_sanitizes_the_written_csv():
    import csv
    import tempfile

    w = workflow("w1", "2026-06-01 12:00:00", title='=HYPERLINK("http://x","y")')
    app = app_with([w])
    app.view = "zoom"
    app.focus = "months"
    app.tab = app.month_tabs.index("Sessions")
    cwd = os.getcwd()
    os.chdir(tempfile.mkdtemp(prefix="ot-export-"))
    try:
        app.export_current()
        assert "exported" in app.notice
        (name,) = (f for f in os.listdir(".") if f.startswith("opentab-sessions-"))
        with open(name, newline="") as fh:
            header, row = list(csv.reader(fh))
    finally:
        os.chdir(cwd)
    assert row[header.index("title")] == '\'=HYPERLINK("http://x","y")'
    assert row[header.index("total_cost")] == "1.0"  # numeric cells stay numbers


def test_export_session_tabs_dispatch_to_their_tables():
    # A store rich enough to back the Subagents / Turns / Tools tabs.
    class RichStore(FakeStore):
        def workflow_nodes(self, wid):
            return [
                {
                    "depth": 1,
                    "agent": "build",
                    "model_name": "anthropic/claude",
                    "cost": 0.5,
                    "tokens_total": 1234,
                    "title": "do the thing",
                    "tokens_input": 1000,
                    "tokens_output": 200,
                    "tokens_reasoning": 0,
                    "tokens_cache_read": 34,
                    "tokens_cache_write": 0,
                }
            ]

        def supports_turns(self, wid):
            return True

        def supports_tools(self, wid):
            return True

        def message_timeline(self, wid):
            return [
                {
                    "time": "2026-06-01 12:00:01",
                    "agent": "main",
                    "depth": 0,
                    "model_name": "anthropic/claude",
                    "cost": 0.25,
                    "tokens_total": 800,
                    "input": 600,
                    "output": 200,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "prompt_id": "p1",
                    "prompt_title": "first prompt",
                }
            ]

        def tool_breakdown(self, wid):
            return [
                {
                    "tool": "bash",
                    "model_name": "anthropic/claude",
                    "calls": 3,
                    "cost": 0.1,
                    "tokens_total": 500,
                    "input": 400,
                    "output": 100,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                }
            ]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(RichStore([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"

    app.tab = app.current_tabs().index("Subagents")
    scope, header, rows = app._export_dataset()
    assert scope == "subagents" and header[0] == "date" and rows[0][2] == "build"

    app.tab = app.current_tabs().index("Turns")
    scope, header, rows = app._export_dataset()
    assert scope == "turns" and "prompt" in header and rows[0][-1] == "first prompt"

    app.tab = app.current_tabs().index("Tools")
    scope, header, rows = app._export_dataset()
    assert scope == "tools" and header[0] == "tool" and rows[0][0] == "bash"


def test_export_disabled_in_demo_mode():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.store.demo = True
    app.export_current()
    assert "demo" in app.notice
