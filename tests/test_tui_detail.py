"""The detail tabs: sessions/projects tables, models, subagents, turns, tools, context (tui/renderer.py)."""

import os
import re
import sqlite3
import tempfile

import opentab as ot

from tests._support import (
    AttrScreen,
    FakeScreen,
    FakeStore,
    _claude_msg,
    _model_row,
    _usage,
    _write_jsonl,
    _write_opencode_db_with_tools,
    _write_opencode_db_with_turns,
    app_with,
    screen_text,
    workflow,
)


def _cells(lines):
    # The content rows of a _ruled_box model/tool table (header, data rows, and the
    # TOTAL row), with the "| ... |" frame gutters stripped -- so a test can read the
    # columns regardless of the box's Unicode/ASCII glyphs or where the rules land.
    return [ln[2:-2] for ln in lines if ln[:1] in ("│", "|")]


def test_top_models_is_a_ruled_box_with_full_model_columns():
    # The "Top Models" overview section reuses the Models-tab table, now drawn as a
    # ruled box: the title rides the top border and the row carries the cache/output
    # columns too (name, runs, cost, tokens, cacheR, cacheW, output).
    app = app_with([])
    rows = [("m", 3648, 1.0, 205_600_000, 1_000_000, 2_000_000, 5_000_000)]
    lines = app.renderer._model_table(rows, "# Top Models", 120)
    assert "Top Models" in lines[0] and lines[0][:1] in ("┌", "+")  # title on the top border
    assert lines[-1][:1] in ("└", "+")  # closed by a bottom border
    header, first = _cells(lines)
    assert header.split() == [
        "Model",
        "Msgs",
        "Cost",
        "Share",
        "Tokens",
        "CacheR",
        "CacheW",
        "Output",
    ]
    assert "$1.00" in first
    assert "205.6M" in first
    assert "3648" in first


def _models_tab_app():
    app = app_with(
        [
            workflow("b", "2026-05-02 10:00:00", cost=9.0),
            workflow("c", "2026-05-03 10:00:00", cost=1.0),
        ]
    )
    app._model_by_root = {
        "b": [_model_row("opus", 9.0, 900)],
        "c": [_model_row("opus", 0.6, 60), _model_row("haiku", 0.4, 40)],
    }
    app.focus = "months"
    return app


def test_the_zoomed_models_tab_is_the_browse_table_plus_a_cursor():
    # The tab renders ONE table, twice. Zooming used to swap the eight-column ruled box
    # for a four-column ranked picker, which is exactly the drift the Sessions/Projects
    # tables were unified to end -- so Enter may add a cursor and nothing else: same
    # rows, same columns, same TOTAL row, byte for byte.
    app = _models_tab_app()
    month = app.selected_month_summary
    preview = app.renderer.month_models(month, 116)
    assert app.renderer._model_row_at == {}  # browse has no cursor
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    zoomed = app.renderer.month_models(month, 116)
    assert zoomed == preview
    # ...and the cursor points at a real data row of that very table.
    rows = app.renderer._model_row_at
    assert sorted(rows.values()) == [0, 1]
    cursor = app.renderer._model_cursor_line
    assert rows[cursor] == 0 and "opus" in zoomed[cursor]


def test_the_models_filter_still_narrows_model_names_in_a_zoom():
    # `f` on this tab has always matched MODEL NAMES. The old picker ran the query
    # against session titles/paths instead, so focusing the tab silently re-pointed the
    # filter and a query that had narrowed the list stopped matching anything.
    app = _models_tab_app()
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.query = "haiku"
    assert [m for m, _ in app.zoom_model_rows()] == ["haiku"]
    lines = app.renderer.month_models(app.selected_month_summary, 116)
    assert any("haiku" in ln for ln in lines) and not any("opus" in ln for ln in lines)
    assert len(app.renderer._model_row_at) == 1  # the cursor indexes the FILTERED rows


def test_a_shrinking_model_list_never_strands_the_cursor_off_screen():
    # Typing an `f` query shrinks the list without moving the cursor (the j/k handlers
    # clamp, a keystroke in the filter does not). The paint must clamp the SAME way
    # zoom_selected_model does, or Enter drills the clamped last row while the pane
    # highlights nothing -- a selection you cannot see acting on a row you did not pick.
    app = _models_tab_app()
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.model_pick_index = 1  # sitting on the second row...
    app.query = "haiku"  # ...of a list the filter cuts to one
    lines = app.renderer.month_models(app.selected_month_summary, 116)
    cursor = app.renderer._model_cursor_line
    assert app.renderer._model_row_at[cursor] == 0
    assert "haiku" in lines[cursor] and app.zoom_selected_model() == "haiku"


def test_model_table_splits_cost_across_token_categories_in_wide_panes():
    # The cacheR/cacheW/Output cells carry their attributed share of the Cost
    # column: fable lists at $10/M in, $50/M out, $1/M cacheR, $12.50/M cacheW,
    # so 100k cache-write tokens cost more than 800k cache reads -- the skew the
    # plain token counts hide.
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    row = _cells(app.renderer._model_table(rows, "# Top Models", 120))[1]
    assert "800.0k ($0.80)" in row
    assert "100.0k ($1.25)" in row
    assert "50.0k ($2.50)" in row


def test_model_table_split_scales_to_the_recorded_cost():
    # A recorded cost that differs from today's list-price total is attributed
    # proportionally, so the split (with the implicit input remainder) always
    # sums to the Cost column.
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 10.10, 1_000_000, 800_000, 100_000, 50_000)]
    row = _cells(app.renderer._model_table(rows, "# Top Models", 120))[1]
    assert "800.0k ($1.60)" in row
    assert "100.0k ($2.50)" in row
    assert "50.0k ($5.00)" in row


def test_model_table_split_cells_align_under_their_labels():
    # Fixed sub-columns: the token count right-aligns under the header label and
    # the "($13)" groups end flush at the same column on every row, the parens
    # hugging the amount (no inner gap), whatever the magnitudes.
    app = app_with([])
    rows = [
        ("anthropic/claude-fable-5", 92, 20.60, 13_400_000, 13_100_000, 194_700, 99_200),
        ("anthropic/claude-opus-4-8", 1, 0.05, 23_500, 15_000, 1_900, 57),
    ]
    header, first, second = _cells(app.renderer._model_table(rows, "# Model Mix", 120))[:3]
    for label in ("CacheR", "CacheW", "Output"):
        i = header.index(label)
        assert first[i + 5] != " " and second[i + 5] != " "  # tokens end under the label
        assert first[i + 13] == ")" and second[i + 13] == ")"
    assert "( " not in first and "( " not in second  # parens hug the amount


def test_model_table_split_needs_width_dollars_and_models():
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    # Narrow pane: plain token counts, exactly the classic layout (still fits the box).
    narrow = app.renderer._model_table(rows, "# Top Models", 90)
    assert not any("(" in ln for ln in narrow)
    assert "800.0k" in _cells(narrow)[1]
    # Unpriced rows ($0.00): nothing to attribute even in a wide pane.
    unpriced = app.renderer._model_table(
        [("anthropic/claude-fable-5", 10, 0.0, 1_000_000, 800_000, 100_000, 50_000)],
        "# Top Models",
        120,
    )
    assert not any("(" in ln for ln in unpriced)
    # The Tools tab reuse: tool names aren't models, so no split there either.
    tools = app.renderer._model_table(
        rows, "# Tools — this session", 120, "Tool", "Calls", price_split=False
    )
    assert not any("(" in ln for ln in tools)


def test_model_table_total_row_sums_every_column():
    # A multi-row table closes with a rule + TOTAL row -- runs, cost, and every token
    # column summed -- so "what did cache writes cost me this year" is one
    # glance. Share stays blank (definitionally 100%).
    app = app_with([])
    rows = [
        ("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000),
        ("anthropic/claude-opus-4-8", 5, 2.00, 500_000, 400_000, 50_000, 25_000),
    ]
    lines = app.renderer._model_table(rows, "# Top Models", 90)  # narrow: plain counts
    total = _cells(lines)[-1]
    assert total.split()[0] == "TOTAL"
    assert "15" in total.split() and "$7.05" in total
    assert "1.5M" in total  # tokens
    assert "1.2M" in total and "150.0k" in total and "75.0k" in total
    # No coloured sum bar any more: the boxed TOTAL row is bold ink, and the title
    # rides the accented top border. (line_attr is glyph-keyed, so mock color_pair.)
    total_row = next(ln for ln in lines if ln.lstrip("│| ").startswith("TOTAL"))
    orig_cp = ot.curses.color_pair
    ot.curses.color_pair = lambda n: 0  # headless: no initscr behind line_attr
    try:
        assert app.renderer.line_attr(total_row) & ot.curses.A_BOLD
        assert not (app.renderer.line_attr(total_row) & ot.curses.A_REVERSE)
        assert app.renderer.line_attr(lines[0]) & ot.curses.A_BOLD  # accented top border
    finally:
        ot.curses.color_pair = orig_cp


def test_model_table_total_row_sums_attributed_dollars_at_each_rows_rates():
    # In split mode the TOTAL's (dollar) parts are the per-row attributions
    # summed -- each row priced at its own model's rates, never the summed
    # tokens at one model's rates. Two fable rows keep the math checkable:
    # each attributes cacheR $0.80, cacheW $1.25, output $2.50 (the split test
    # above), so TOTAL carries exactly double.
    app = app_with([])
    row = ("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)
    total = _cells(app.renderer._model_table([row, row], "# Top Models", 120))[-1]
    assert total.split()[0] == "TOTAL"
    assert "$10.10" in total
    assert "1.6M ($1.60)" in total
    assert "200.0k ($2.50)" in total
    assert "100.0k ($5.00)" in total


def test_model_table_total_split_dollars_keep_the_compact_label_convention():
    # The parenthetical attributions are approximations by construction (scaled
    # shares), rendered through money_label -- which drops cents at >=$10. So a
    # TOTAL crossing the threshold shows "($10)" while its <$10 rows keep cents;
    # pinned deliberately: the exact figure lives in the Cost column (full
    # money()), and widening the 14-char cells would break the fixed grid.
    app = app_with([])
    row = ("anthropic/claude-fable-5", 10, 5.05, 100_000, 0, 0, 100_000)
    _, first, second, total = _cells(app.renderer._model_table([row, row], "# Top Models", 120))
    assert "($5.05)" in first and "($5.05)" in second
    assert "($10)" in total and "$10.10" in total  # cells compact, Cost exact


def test_model_table_single_row_has_no_total():
    # A one-row table IS its own total; a TOTAL row would just repeat it.
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    lines = app.renderer._model_table(rows, "# Top Models", 120)
    assert not any("TOTAL" in ln for ln in lines)


def test_tools_table_total_row_stays_unsplit():
    # The Tools-tab reuse gets the TOTAL row too (rows partition the session's
    # tool-using turns, so the sum is real), but never the (dollar) split.
    app = app_with([])
    rows = [
        ("Bash", 10, 1.00, 1_000_000, 800_000, 100_000, 50_000),
        ("Read", 5, 0.50, 500_000, 400_000, 50_000, 25_000),
    ]
    lines = app.renderer._model_table(
        rows, "# Tools — this session", 120, "Tool", "Calls", price_split=False
    )
    total = _cells(lines)[-1]
    assert total.split()[0] == "TOTAL"
    assert "$1.50" in total and "(" not in total


def test_model_table_split_gives_columns_a_two_space_gutter():
    # The wide split layout separates columns with two spaces (was one) so a
    # "($0.80)" attribution cell stops butting against the next column.
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    row = _cells(app.renderer._model_table(rows, "# Top Models", 120))[1]
    assert ")  " in row  # a "(...)" cell is followed by a two-space gutter, not one


def test_top_sessions_overview_box_caps_the_leaderboard_at_twenty():
    # A busy month spills hundreds of sessions; the Overview's Top Sessions box is a
    # leaderboard (the full, navigable list is the Sessions tab), so it caps at 20 and
    # ranks by cost -- the priciest first.
    ws = [workflow(f"s{i}", "2026-06-01 12:00:00", cost=float(i + 1)) for i in range(25)]
    app = app_with(ws)
    assert len(app.renderer.top_sessions(ws)) == 20
    box = app.renderer._top_sessions_box(ws, sum(w.total_cost for w in ws), 120)
    data = [ln for ln in box if "$" in ln]  # the cost column marks every data row
    assert len(data) == 20
    assert "$25.00" in data[0]  # highest cost leads
    assert not any("$5.00" in ln for ln in data)  # the bottom five fall off the cap


def test_the_model_table_closes_every_overview():
    # The model table is the widest block on any Overview and the least likely answer to
    # "where did the money go", so it goes LAST everywhere -- under the stats, Token
    # economics and the Top projects/sessions boxes, which each fit in a glance. A new
    # section goes above it, never below.
    from tests._support import _model_row, fleet_app

    ws = [workflow("a", "2026-06-01 12:00:00", directory="/x", cost=2.0, tokens=1000)]
    app = app_with(ws)
    app._model_by_root = {"a": [_model_row("anthropic/claude-opus-4.5", 2.0, 1000)]}
    app._compute_api_costs()
    r = app.renderer
    fleet = fleet_app({"laptop": [workflow("a", "2026-05-01 10:00:00", cost=3.0)]})
    fleet.set_browse_mode("machines")
    overviews = [
        r.year_overview(app.years[0], 120),
        r.month_overview(app.months[0], 120),
        r.day_overview(app.days[0], 120),
        r.project_overview(app.projects[0], 120),
        r.detail_overview(ws[0], 120),
        fleet.renderer.machine_overview(fleet.machines[0], 120),
    ]
    for lines in overviews:
        # The last non-blank line closes the model box -- nothing renders after it.
        body = [ln for ln in lines if ln.strip()]
        title = next(i for i, ln in enumerate(body) if "Top Models" in ln or "Model Mix" in ln)
        assert body[title][:1] in ("┌", "+")  # it is the ruled box's titled top border
        assert body[-1][:1] in ("└", "+")  # and its bottom border ends the pane


def test_top_projects_box_ranks_projects_by_cost_in_the_overview():
    # The Overview grows a "# Top Projects" ruled box beside Top Models/Top Sessions,
    # aggregating the scope's sessions by project directory, cost-ranked.
    ws = [
        workflow("a", "2026-06-01 12:00:00", cost=10.0, directory="/repo/big"),
        workflow("b", "2026-06-02 12:00:00", cost=1.0, directory="/repo/small"),
        workflow("c", "2026-06-03 12:00:00", cost=5.0, directory="/repo/big"),
    ]
    app = app_with(ws)
    over = app.renderer.month_overview(app.months[0], 120)
    assert any("Top Projects" in ln for ln in over)
    box = app.renderer._top_projects_box(ws, sum(w.total_cost for w in ws), 120)
    data = [ln for ln in box if "$" in ln]
    assert "big" in data[0] and "$15.00" in data[0]  # two sessions summed, leads
    assert "small" in data[1] and "$1.00" in data[1]


def test_top_sessions_box_widens_cost_column_for_six_figure_spend():
    # "$123,456.78" is 11 cells, not 10 -- the Cost column must widen so Share/Tokens/Subs
    # stay under their headers instead of every row shoving one cell right.
    app = app_with([])
    ws = [workflow("a", "2026-06-01 12:00:00", cost=123456.78)]
    content = _cells(app.renderer._top_sessions_box(ws, 200000.0, 120))
    header, row = content[0], next(ln for ln in content if "$123,456.78" in ln)
    cw = len("$123,456.78")  # 11: the widened Cost column
    assert row[:cw] == "$123,456.78"  # full cost, not clipped to 10
    assert header[:cw] == f"{'Cost':>{cw}}"  # header padded to the same column
    assert header[cw] == " " and row[cw] == " "  # the gap after Cost -- columns aligned


def test_top_projects_box_widens_cost_column_for_six_figure_spend():
    app = app_with([])
    ws = [workflow("a", "2026-06-01 12:00:00", cost=123456.78, directory="/repo/x")]
    content = _cells(app.renderer._top_projects_box(ws, 200000.0, 120))
    header, row = content[0], next(ln for ln in content if "$123,456.78" in ln)
    cw = len("$123,456.78")
    assert row[:cw] == "$123,456.78"
    assert header[cw] == " " and row[cw] == " "  # columns still aligned past the wider Cost


def test_projects_merge_across_windows_slash_styles():
    # Pi records the cwd with backslashes; OpenCode records the same directory with
    # forward slashes. They must group as ONE project, not two (issue #4).
    app = app_with(
        [
            workflow("pi", "2026-06-01 12:00:00", cost=2, directory=r"C:\DEV\examples\okf"),
            workflow("oc", "2026-06-02 12:00:00", cost=3, directory="C:/DEV/examples/okf"),
        ]
    )
    projects = app.projects
    assert [p.directory for p in projects] == [r"C:\DEV\examples\okf"]
    assert projects[0].workflows == 2 and projects[0].cost == 5
    assert {w.id for w in app.workflows_for_project(r"C:\DEV\examples\okf")} == {"pi", "oc"}


def test_projects_group_worktrees_under_root():
    app = app_with(
        [
            workflow("m", "2026-06-01 12:00:00", cost=1, directory="/repo/app"),
            workflow("w", "2026-06-02 12:00:00", cost=2, directory="/repo/app-feat"),
        ]
    )
    app._root_by_dir = {"/repo/app-feat": "/repo/app"}  # feat is a worktree of app
    assert [p.directory for p in app.projects] == ["/repo/app"]
    assert app.projects[0].workflows == 2 and app.projects[0].cost == 3
    assert {w.id for w in app.workflows_for_project("/repo/app")} == {"m", "w"}


def _paint_sessions_picker(app, width=100):
    screen = FakeScreen(24, width)
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: 0
    ot.curses.init_pair = lambda *a: None
    try:
        app.renderer.draw_sessions_picker(screen, 0, 0, 24, width)
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    return screen_text(screen).splitlines()


def test_zoom_pickers_paint_no_enter_hint():
    # The pickers used to paint an "Enter: open session(s)" hint on the tab-strip row.
    # It duplicated the footer's "Enter in" and, in the fleet view's six-tab strip, it
    # overran the last tab. It's gone now -- no picker draws that hint.
    app = app_with(
        [
            workflow("s1", "2026-06-01 12:00:00", title="first", directory="/tmp/alpha"),
            workflow("s2", "2026-06-02 12:00:00", title="second", directory="/tmp/beta"),
        ]
    )
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    assert not any("Enter: open" in ln for ln in _paint_sessions_picker(app))


def test_sessions_picker_shows_a_project_column_in_time_mode():
    app = app_with(
        [
            workflow("s1", "2026-06-01 12:00:00", title="first", directory="/tmp/alpha"),
            workflow("s2", "2026-06-02 12:00:00", title="second", directory="/tmp/beta"),
        ]
    )
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    lines = _paint_sessions_picker(app)
    header = next(ln for ln in lines if "Title" in ln)
    assert "Project" in header  # the column header sits between Subagents and Title
    assert header.index("Subagents") < header.index("Project") < header.index("Title")
    assert any("alpha" in ln and "first" in ln for ln in lines)  # each row names its project
    assert any("beta" in ln and "second" in ln for ln in lines)


def test_sessions_picker_shows_the_date_beyond_day_scope():
    # A year (or "All years") scope spans months, so a bare clock time is useless --
    # the picker must show the date there, like the month scope already does. Only a
    # zoomed day (every row shares that day) keeps the time-only column.
    app = app_with(
        [
            workflow("s1", "2026-06-01 12:15:00", title="june"),
            workflow("s2", "2025-11-02 08:34:00", title="november"),
        ]
    )
    app.view = "zoom"
    app.focus = "years"  # defaults to the "All years" row -> both years listed
    app.tab = app.year_tabs.index("Sessions")
    lines = _paint_sessions_picker(app)
    header = next(ln for ln in lines if "Title" in ln)
    assert "Started" in header and "Time" not in header
    assert any("2026-06-01" in ln and "june" in ln for ln in lines)
    assert any("2025-11-02" in ln and "november" in ln for ln in lines)
    app.focus = "months"  # scoped to one month, but it still spans days -> date column
    app.tab = app.month_tabs.index("Sessions")
    lines = _paint_sessions_picker(app)
    header = next(ln for ln in lines if "Title" in ln)
    assert "Started" in header and "Time" not in header
    assert any("2026-06-01" in ln and "june" in ln for ln in lines)
    app.focus = "days"
    app.tab = app.day_tabs.index("Sessions")
    lines = _paint_sessions_picker(app)
    header = next(ln for ln in lines if "Title" in ln)
    assert "Time" in header and "Started" not in header
    assert any("12:15" in ln and "june" in ln for ln in lines)  # clock, not the date
    assert not any("2026-06-01" in ln for ln in lines)


def test_sessions_picker_hides_the_project_column_when_project_scoped():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", directory="/tmp/alpha"),
            workflow("b", "2026-06-02 12:00:00", directory="/tmp/alpha"),
        ]
    )
    # A zoomed project in projects mode: every session is that project's already.
    app.browse_mode = "projects"
    app.view = "zoom"
    app.tab = app.project_tabs.index("Sessions")
    lines = _paint_sessions_picker(app)
    header = next(ln for ln in lines if "Title" in ln)
    assert "Project" not in header
    # Same for a Projects-tab drill-in on a zoomed month (time mode + zoom_project).
    app2 = app_with([workflow("a", "2026-06-01 12:00:00", directory="/tmp/alpha")])
    app2.focus = "months"
    app2.view = "zoom"
    app2.tab = app2.month_tabs.index("Sessions")
    app2.zoom_project = "/tmp/alpha"
    lines2 = _paint_sessions_picker(app2)
    header2 = next(ln for ln in lines2 if "Title" in ln)
    assert "Project" not in header2


def test_browse_preview_and_zoom_picker_are_the_same_session_table():
    # Enter (browse -> zoom) must light up a row, never re-shape the table. The
    # preview and the picker were two hand-written tables and had drifted: the
    # preview had Models + Src columns and a "# Monthly Sessions" heading, the
    # picker had a Project column, an inline [oc] tag and a 2-column indent. They
    # build from one set of helpers now, so the frames can't diverge again.
    app = app_with(
        [
            workflow("s1", "2026-06-01 12:00:00", title="first", directory="/tmp/alpha"),
            workflow("s2", "2026-06-02 12:00:00", title="second", directory="/tmp/beta"),
        ]
    )
    app.focus = "months"
    app.tab = app.month_tabs.index("Sessions")
    # The picker draws into a 100-wide pane, i.e. a content width of w - 4.
    preview = app.renderer.month_workflows(app.selected_month_summary, 96)
    assert not preview[0].startswith("#")  # no heading line to shift the rows down

    app.view = "zoom"
    painted = _paint_sessions_picker(app, 100)

    header = next(ln for ln in painted if "Title" in ln)
    assert header.strip() == preview[0].strip()  # same columns, same sort arrows
    rows = [ln.strip() for ln in painted if "first" in ln or "second" in ln]
    assert rows and len(rows) == len(preview) - 1
    # The cursor is the only difference: strip it and the rows are identical.
    assert [r.lstrip(">").strip() for r in rows] == [p.strip() for p in preview[1:]]


def test_detail_tools_reprices_unpriced_under_dollar():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        app = ot.App(ot.Store(db, type("A", (), {"demo": False})()), args())
        rnd = ot.Renderer(app)
        wf = app.loaded[0]
        normal = rnd.detail_tools(wf, 92)
        joined = "\n".join(normal)
        assert "Tool-attributed spend" in joined
        assert "Tool-attributed spend · $6.00" in joined
        # Only `bash` has spend here, so there is exactly one tile and no per-call
        # SCALE to shade against -- the caption says so instead of implying one. The
        # rate itself still rides on the tile: it is useful without a comparison.
        assert "area + shade = visible cost" in joined
        assert "$3.00/call · 2 calls" in joined
        assert "Tools — this session" in joined  # the title rides the box's top border
        assert "By server / namespace" in joined
        assert "(built-in)" in joined  # the server rollup labels built-in vs MCP
        # The subscription session records $0; under "$" the wholly-unpriced serena
        # row picks up its list-price estimate (1M Haiku input @ $1/M = $1.00).
        app.show_api_prices = True
        app._ensure_models()
        serena_line = next(
            c for c in _cells(rnd.detail_tools(wf, 92)) if c.startswith("serena_read_file")
        )
        assert "$1.00" in serena_line
        api_lines = "\n".join(rnd.detail_tools(wf, 92))
        assert "Tool-attributed spend · $7.00" in api_lines
        # ...and now that the estimate gives serena a width too, there are two rates to
        # rank, so the shade becomes a scale and the caption says which one.
        assert "area = visible cost · shade = $/call" in api_lines


def test_tool_treemap_shades_by_per_call_rate_not_by_its_own_area():
    # The whole point of the second channel: area is the total, shade is the RATE, so a
    # tool that is big because it ran often is cool and one that is small because it ran
    # three times at $2 each is hot. Encoding the same number twice says nothing.
    rnd = app_with([]).renderer
    bucket = {
        "Bash": {"cost": 6.0, "tokens": 6000, "calls": 600},  # biggest tile, cheapest call
        "WebFetch": {"cost": 3.0, "tokens": 3000, "calls": 3},  # small tile, priciest call
        "Read": {"cost": 1.0, "tokens": 1000, "calls": 50},
    }
    joined = "\n".join(rnd._tool_treemap_box(bucket, 120))
    assert "area = visible cost · shade = $/call" in joined
    # Area ranks Bash > WebFetch > Read. The shade must NOT agree with it: the biggest
    # tile is the CHEAPEST per call and has to come out coolest, the small WebFetch
    # hottest. Levels are stashed by (line, column) and stay plain ints until paint.
    levels = {}
    for runs in rnd._tool_tree_runs.values():
        for col, _length, level in runs:
            levels.setdefault(col, level)
    ordered = [levels[col] for col in sorted(levels)]  # left to right = Bash, WebFetch, Read
    assert ordered[0] == min(ordered) and ordered[1] == max(ordered)
    rects = {
        name: w * h
        for name, _v, _x, _y, w, h in rnd._treemap_rects(
            [("Bash", 6.0), ("WebFetch", 3.0), ("Read", 1.0)], 60, 5
        )
    }
    assert rects["Bash"] > rects["WebFetch"] > rects["Read"]
    assert "$1.00/call · 3 calls" in joined  # WebFetch: $3 over 3 calls
    assert "$0.01/call · 600 calls" in joined  # Bash: $6 over 600 calls
    assert "$0.02/call" in joined  # Read: $1 over 50 calls

    # A narrow pane OMITS a figure it cannot fit, never clips it: "$0.02/call" cut to
    # "$0.0" is silently a different number, which no amount of context repairs.
    narrow = "\n".join(rnd._tool_treemap_box(bucket, 72))
    assert "$0.02/call" not in narrow  # Read's tile has no room at this width...
    assert "$0.0 " not in narrow and not narrow.count("$0.0\n")  # ...so it says nothing
    assert "$1.00/call" in narrow  # the tiles that do have room still speak

    # Sub-cent rates keep four decimals -- "<$0.01" for both would erase exactly the
    # distinction the shade is drawing.
    cheap = "\n".join(
        rnd._tool_treemap_box(
            {
                "Read": {"cost": 0.006, "tokens": 900, "calls": 1},
                "Grep": {"cost": 0.004, "tokens": 400, "calls": 10},
            },
            120,
        )
    )
    assert "$0.006/call" in cheap and "$0.0004/call" in cheap


def test_tool_treemap_uses_token_fallback_geometry_and_theme_fill_pairs():
    rnd = app_with([]).renderer
    bucket = {
        "Bash": {"cost": 0.0, "tokens": 6000},
        "Edit": {"cost": 0.0, "tokens": 3000},
        "Read": {"cost": 0.0, "tokens": 1000},
    }
    lines = rnd._tool_treemap_box(bucket, 72)
    joined = "\n".join(lines)
    assert "Tool-attributed spend · 10.0k tokens" in joined
    # No call counts in this bucket at all, so a rate is not computable -- the shade
    # falls back to the area's own measure rather than inventing a scale, and says so.
    assert "area + shade = tokens (no recorded cost)" in joined
    assert "area is TOKENS" in joined
    assert "Bash" in joined and "Edit" in joined
    assert rnd._tool_treemap_box(bucket, 72, max_height=2) == []
    three_rows = rnd._tool_treemap_box(bucket, 72, max_height=3)
    assert "Tool-attributed spend" in "\n".join(three_rows)
    # chart box + trailing blank, then the table through its first exact data row.
    assert len(three_rows) + 4 == 14

    rects = rnd._treemap_rects([("Bash", 6), ("Edit", 3), ("Read", 1)], 60, 10)
    areas = {name: w * h for name, _value, _x, _y, w, h in rects}
    assert sum(areas.values()) == 600
    assert areas["Bash"] > areas["Edit"] > areas["Read"]

    # Heat levels remain plain data until draw_detail resolves the dedicated fill pairs.
    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8
    try:
        screen = AttrScreen(height=30, width=80)
        painted = []
        for index, runs in rnd._tool_tree_runs.items():
            rnd._paint_tool_tree_runs(screen, index, 0, index, lines[index], 72)
            painted.extend(screen.attrs[(index, col)] for col, _length, _level in runs)
    finally:
        ot.curses.color_pair = orig
    attrs = set(painted)
    expected = {
        ((ot.TOOL_HEAT_BASE_PAIR + level) << 8) | ot.curses.A_BOLD
        for level in range(ot.TOOL_HEAT_LEVELS)
    }
    assert len(attrs) >= 2
    assert attrs <= expected

    # Pair-starved + non-UTF screens keep visible ASCII density instead of sending
    # block glyphs through curses' unsafe narrow-character path.
    import opentab.tui.renderer as renderer_module

    original_unicode = renderer_module.unicode_screen
    try:
        renderer_module.unicode_screen = lambda: False
        rnd._tool_heat_ok = False
        fallback = "\n".join(rnd._tool_treemap_box(bucket, 48))
    finally:
        renderer_module.unicode_screen = original_unicode
        rnd._tool_heat_ok = True
    assert any(ch in fallback for ch in ".:*#")
    assert not any(ch in fallback for ch in "░▒▓█")

    # Degenerate canvases collapse an overfull tail instead of losing its weight.
    tiny = rnd._treemap_rects([("a", 8), ("b", 1), ("c", 1)], 1, 1)
    assert tiny == [("Other", 10.0, 0, 0, 1, 1)]


def test_detail_turns_cumulative_and_reprices_under_dollar():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        app = ot.App(ot.Store(db, type("A", (), {"demo": False})()), args())
        rnd = ot.Renderer(app)
        wf = app.loaded[0]
        # One row per prompt, columns always drawn -- the numbers ARE the tab, so they
        # never hide inside an expansion the reader has to discover.
        table = rnd.detail_turns(wf, 96)
        tj = "\n".join(table)
        assert table[0].startswith("# Turns — 2 prompts · 3 turns · $3.00")
        assert "Add feature X" in tj and "Fix the bug" in tj
        assert "Turns" in table[1] and "Cached" in table[1] and "Cumulative" in table[1]
        assert "$3.00 · 100%" in tj  # the last prompt's cumulative cell
        # A prompt row is a moment (MM-DD HH:MM); the seconds belong to its turns, which
        # live in the popup, so no per-turn clock stamp reaches the table.
        assert not any(re.search(r"\d\d-\d\d \d\d:\d\d:\d\d", ln) for ln in table)
        assert "One row per prompt" in tj and "opens it with its turns" in tj
        # The turns themselves, with their seconds, are one Enter away.
        app.open_turn_drill(0)
        drilled = rnd.detail_turn_drill(wf, 90)
        assert any(re.search(r"\d\d-\d\d \d\d:\d\d:\d\d", ln) for ln in drilled)
        app.close_turn_drill()  # step back out: detail_turns is the table again
        # Under "$" the two $0 haiku turns estimate at list price (1M+2M @ $1/M),
        # so the total grows to $1 + $2 + $3 = $6.00 -- in the table and the drill alike.
        app.show_api_prices = True
        priced = rnd.detail_turns(wf, 96)
        assert priced[0].startswith("# Turns — 2 prompts · 3 turns · $6.00")
        pjoined = "\n".join(priced)
        assert "$6.00 · 100%" in pjoined and "Add feature X" in pjoined
        app.open_turn_drill(0)
        assert "$1.00" in "\n".join(rnd.detail_turn_drill(wf, 90))


def test_turns_marks_compactions_even_while_folded():
    # A compaction is the one event on the Turns tab that is not a turn: the window was
    # cleared between two of them. It has to survive the tab's DEFAULT folded state --
    # a marker hidden inside a collapsed group is a marker nobody sees -- and it must
    # read off the same rule the Context tab draws its ▼ with (util.context_compactions).
    class CompactStore(FakeStore):
        # main-thread context: 20k, 900k, 300k (the clear), then a subagent turn whose
        # own window is unrelated, then 340k -- growth again, no second marker.
        ROWS = ((0, 20_000), (0, 900_000), (0, 300_000), (1, 5_000), (0, 340_000))

        def supports_turns(self, wid):
            return True

        def message_timeline(self, wid):
            rows = []
            for i, (depth, size) in enumerate(self.ROWS):
                rows.append(
                    {
                        "time": f"2026-06-01 12:0{i}:30",
                        "agent": "explore" if depth else "-",
                        "depth": depth,
                        "model_name": "anthropic/claude-sonnet-5",
                        "cost": 1.0,
                        "input": 1000,
                        "output": 50,
                        "reasoning": 0,
                        "cache_read": size - 1000,
                        "cache_write": 0,
                        "tokens_total": size + 50,
                        "prompt_id": f"p{i}",
                        "prompt_title": f"prompt {i}",
                    }
                )
            return rows

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(CompactStore([workflow("ses_1", "2026-06-01 12:00:00", cost=5.0)]), args)
    app.view = "session"
    lines = app.renderer.detail_turns(app.current_session(), 100)
    joined = "\n".join(lines)
    # Folded is the default -- no turn rows drawn, and the marker there regardless.
    assert app.turn_drill is None
    assert not any(re.search(r"\d\d-\d\d \d\d:\d\d:\d\d", ln) for ln in lines)
    marker = next(ln for ln in lines if ln.startswith("▼ "))
    assert "before turn 3" in marker  # the turn that ran on the cleared window
    assert "900.0k → 300.0k" in marker and "600.0k freed" in marker
    # Exactly one: a subagent's own small context neither triggers a marker (it runs in
    # its own window) nor breaks the main thread's chain (300k → 340k is growth).
    assert sum(1 for ln in lines if ln.startswith("▼ ")) == 1
    assert "▼ 1 compaction, ~600.0k freed" in lines[0]
    # Amber like the Context tab's ▼ rows, so one event reads as one thing on both tabs.
    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n
    try:
        assert app.renderer.line_attr(marker) == (2 | ot.curses.A_BOLD)
    finally:
        ot.curses.color_pair = orig
    # And the Context tab agrees about the count -- one rule, read from util by both.
    assert "compacted 1×" in "\n".join(app.renderer.detail_context(app.current_session(), 100))
    assert joined.count("context compacted") == 1

    # A backend whose rows are deltas of a cumulative total (Codex) or synthetic
    # multi-conversation rows (CSV/JSONL) opts out of the curve: its input+cache is not
    # a prompt size, so the same drop must NOT be marked here either. Ungated, a real
    # Codex session drew "▼ 240.4k → 15.8k" beside a Context tab that had correctly
    # hidden its curve -- the two tabs contradicting each other on one screen.
    class NoCurve(CompactStore):
        def supports_context_curve(self, wid):
            return False

    flat = ot.App(NoCurve([workflow("ses_1", "2026-06-01 12:00:00", cost=5.0)]), args)
    flat.view = "session"
    flat_lines = flat.renderer.detail_turns(flat.current_session(), 100)
    assert not any(ln.startswith("▼ ") for ln in flat_lines)
    assert "compaction" not in flat_lines[0]

    # A row with no "time" at all still renders. The marker draws in the tab's DEFAULT
    # folded state, so an r["time"] there would take the whole tab down on a row the
    # expanded view merely prints as "--" (a truncated export, a future backend).
    class NoTime(CompactStore):
        def message_timeline(self, wid):
            return [
                {k: v for k, v in r.items() if k != "time"} for r in super().message_timeline(wid)
            ]

    timeless = ot.App(NoTime([workflow("ses_1", "2026-06-01 12:00:00", cost=5.0)]), args)
    timeless.view = "session"
    assert any(
        ln.startswith("▼ ")
        for ln in timeless.renderer.detail_turns(timeless.current_session(), 100)
    )
    # ...and the popup behind Enter survives a row with no timestamp too.
    timeless.open_turn_drill(0)
    assert timeless.renderer.detail_turn_drill(timeless.current_session(), 90)


def test_subagents_tab_reprices_unpriced_node_in_api_mode():
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
                ("root", None, "Root", "/tmp/project", 1760000000000, 0.2, 0, 1000, 0, 0, 0),
                (
                    "child",
                    "root",
                    "Child",
                    "/tmp/project",
                    1760000001000,
                    0.0,
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
                    '{"role":"assistant","providerID":"anthropic","modelID":"claude-opus-4.5","cost":0.2,"tokens":{"input":0,"output":1000}}',
                ),
                # Unpriced Copilot/Opus subagent: $0 in OpenCode, real token usage.
                (
                    "child",
                    '{"role":"assistant","providerID":"github-copilot","modelID":"claude-opus-4.5","cost":0,"tokens":{"input":1000000,"output":0}}',
                ),
            ],
        )
        conn.commit()
        conn.close()

        store = ot.Store(db, type("Args", (), {"demo": False})())
        app = ot.App(store, type("Args", (), {"since": None, "until": None, "days": None})())

        expected = ot.api_equivalent_cost("github-copilot/claude-opus-4.5", 1_000_000, 0, 0, 0, 0)
        assert expected > 0  # guard: model must resolve to a real list price

        # Real mode: the unpriced subagent reads as $0.00.
        real = app._priced_nodes([r for r in store.workflow_nodes("root") if r["depth"] > 0])
        assert real[0]["cost"] == 0.0
        assert "$0.00" in app.renderer.detail_subagents(app.loaded[0], 200)[-1]

        # API mode: it is repriced to the Opus API-equivalent. _priced_nodes feeds
        # both the rendered tab and the CSV export, so asserting it covers both.
        app.toggle_api_prices()
        priced = app._priced_nodes([r for r in store.workflow_nodes("root") if r["depth"] > 0])
        assert round(priced[0]["cost"], 6) == round(expected, 6)
        sub_line = app.renderer.detail_subagents(app.loaded[0], 200)[-1]
        assert ot.money(expected) in sub_line
        assert "$0.00" not in sub_line


def _subagent_rows():
    return [
        {
            "depth": 1,
            "agent": "b",
            "model_name": "m",
            "cost": 2.0,
            "tokens_total": 10,
            "title": "b",
            "created_at": "2026-06-01 13:00:00",
        },
        {
            "depth": 1,
            "agent": "a",
            "model_name": "m",
            "cost": 1.0,
            "tokens_total": 20,
            "title": "a",
            "created_at": "2026-06-01 12:00:00",
        },
    ]


def test_subagents_tab_is_sortable_by_tokens():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.view = "session"
    app.tab = app.workflow_tabs.index("Subagents")
    app.subagent_sort_by = "tokens"

    assert app.current_sort_options() == app.subagent_sort_options
    assert app.sorted_subagent_rows(_subagent_rows())[0]["title"] == "a"


def test_subagents_tab_is_sortable_by_date():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.view = "session"
    app.tab = app.workflow_tabs.index("Subagents")
    app.subagent_sort_by = "date"  # newest first by default
    assert [r["title"] for r in app.sorted_subagent_rows(_subagent_rows())] == ["b", "a"]
    app.subagent_sort_reverse = True  # flipped: chronological
    assert [r["title"] for r in app.sorted_subagent_rows(_subagent_rows())] == ["a", "b"]


def test_subagent_sort_is_independent_of_session_sort():
    # Sorting the Subagents tab must not clobber the sessions-list preference
    # (they used to share sort_by, so picking "depth" here reset sessions to cost).
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.sort_by = "date"
    app.view = "session"
    app.tab = app.workflow_tabs.index("Subagents")

    assert app.handle_key(None, ord("s"))  # opens the subagent sort picker
    assert app.sort_menu and app.sort_menu_options() == app.subagent_sort_options
    app.handle_key(None, ord("G"))  # jump to the last option (depth)
    app.handle_key(None, 10)  # Enter applies
    assert app.subagent_sort_by == "depth"
    assert app.sort_by == "date"  # the sessions sort survived

    # A header click on the Subagents tab targets the subagent pair only.
    app.apply_header_sort("model", "subagent")
    assert app.subagent_sort_by == "model" and app.sort_by == "date"
    app.apply_header_sort("model", "subagent")  # re-click flips direction
    assert app.subagent_sort_reverse is True


def test_projects_are_grouped_and_sorted_by_cost():
    app = app_with(
        [
            workflow("cheap", "2026-06-01 12:00:00", cost=1, directory="/tmp/a"),
            workflow("expensive", "2026-06-02 12:00:00", cost=5, directory="/tmp/b"),
            workflow("more", "2026-06-03 12:00:00", cost=2, directory="/tmp/a"),
        ]
    )

    assert [p.directory for p in app.projects] == ["/tmp/b", "/tmp/a"]
    assert app.projects[1].workflows == 2
    assert app.projects[1].cost == 3


def test_projects_sort_by_tokens_and_name():
    app = app_with(
        [
            workflow("costly", "2026-06-01 12:00:00", cost=10, tokens=1, directory="/tmp/b"),
            workflow("tokeny", "2026-06-02 12:00:00", cost=1, tokens=100, directory="/tmp/a"),
        ]
    )

    app.project_sort_by = "tokens"
    assert [p.directory for p in app.projects] == ["/tmp/a", "/tmp/b"]

    app.project_sort_by = "project"
    assert [p.directory for p in app.projects] == ["/tmp/a", "/tmp/b"]


def test_projects_sort_by_recency():
    app = app_with(
        [
            # /tmp/old's newest session predates /tmp/new's, despite costing more
            workflow("o1", "2026-06-01 09:00:00", cost=99, directory="/tmp/old"),
            workflow("n1", "2026-06-10 09:00:00", cost=1, directory="/tmp/new"),
            workflow("o2", "2026-06-05 09:00:00", cost=50, directory="/tmp/old"),
        ]
    )
    app.project_sort_by = "recency"
    assert [p.directory for p in app.projects] == ["/tmp/new", "/tmp/old"]
    # last_active reflects each project's most recent session
    by_dir = {p.directory: p for p in app.projects}
    assert by_dir["/tmp/old"].last_active == "2026-06-05 09:00:00"
    assert by_dir["/tmp/new"].last_active == "2026-06-10 09:00:00"


def test_projects_sort_by_last_activity_differs_from_recency():
    # /tmp/new's session started later than /tmp/old's, so recency (newest session
    # START) ranks it first -- but /tmp/old's session ran on well past that, so
    # last_activity (newest ended_at-or-created_at) must rank /tmp/old first instead.
    app = app_with(
        [
            workflow(
                "o1",
                "2026-06-01 09:00:00",
                cost=99,
                directory="/tmp/old",
                ended_at="2026-06-12 09:00:00",
            ),
            workflow("n1", "2026-06-10 09:00:00", cost=1, directory="/tmp/new"),
        ]
    )
    app.project_sort_by = "recency"
    assert [p.directory for p in app.projects] == ["/tmp/new", "/tmp/old"]

    app.project_sort_by = "last_activity"
    assert [p.directory for p in app.projects] == ["/tmp/old", "/tmp/new"]
    by_dir = {p.directory: p for p in app.projects}
    assert by_dir["/tmp/old"].last_activity == "2026-06-12 09:00:00"
    assert by_dir["/tmp/new"].last_activity == "2026-06-10 09:00:00"  # falls back to created_at


def test_project_header_aligns_with_project_rows():
    app = app_with(
        [workflow("a", "2026-06-01 12:00:00", cost=12.34, tokens=1500, directory="/tmp/project")]
    )
    app.set_browse_mode("projects")
    project = app.projects[0]
    header = app.renderer.project_header_text(80)
    row = app.renderer.project_row_text(project, ">", 80)

    assert header.index("Cost") + len("Cost v") == row.index("$12.34") + len("$12.34")
    assert header.index("Tokens") + len("Tokens") == row.index("1.5k") + len("1.5k")
    assert header.index("Ses") + len("Ses") == row.index("  1 ses") + len("  1 ses")
    assert header.index("Subagents") + len("Subagents") == row.index("     0 subs") + len(
        "     0 subs"
    )
    assert len(header) <= 80
    assert len(row) <= 80


def test_subagents_tab_header_is_click_sortable_and_shows_started():
    class NodeStore(FakeStore):
        def workflow_nodes(self, wid):
            return [
                {
                    "depth": d,
                    "agent": "task",
                    "model_name": "anthropic/x",
                    "cost": 1.0,
                    "tokens_total": 10,
                    "title": t,
                    "created_at": ts,
                    "tokens_input": 5,
                    "tokens_output": 5,
                    "tokens_reasoning": 0,
                    "tokens_cache_read": 0,
                    "tokens_cache_write": 0,
                }
                for d, t, ts in (
                    (0, "root", "2026-06-01 12:00:00"),
                    (1, "sub", "2026-06-01 12:30:00"),
                )
            ]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(NodeStore([workflow("s1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    rnd = app.renderer
    rnd._line_sort_headers = {}
    lines = rnd.detail_subagents(app.loaded[0], 120)
    # The tab opens with the flamegraph box, so the table's header is wherever that box
    # ended -- which is exactly why the sort registration is keyed off the built list's
    # own length rather than a literal index.
    head = next(i for i, ln in enumerate(lines) if ln.startswith("Started"))
    assert lines[head - 1] == "# Subagent Executions"
    assert "2026-06-01 12:30" in lines[head + 1]  # the subagent row carries its start time
    cols, target = rnd._line_sort_headers[head]
    assert target == "subagent" and cols == rnd.SUBAGENT_SORT_COLUMNS


def test_month_and_day_views_have_projects_tab():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])

    app.focus = "months"
    assert "Projects" in app.current_tabs()

    app.focus = "days"
    assert "Projects" in app.current_tabs()


def test_month_projects_are_scoped_and_sortable():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, tokens=100, directory="/tmp/a"),
            workflow("b", "2026-06-02 12:00:00", cost=2, tokens=10, directory="/tmp/b"),
            workflow("old", "2026-05-01 12:00:00", cost=99, tokens=999, directory="/tmp/old"),
        ]
    )
    app.focus = "months"
    app.tab = app.month_tabs.index("Projects")
    app.project_sort_by = "tokens"

    lines = app.renderer.month_projects(app.selected_month_summary, 100)

    assert "/tmp/a" in lines[1]  # lines[0] is the column header (no heading above it)
    assert "/tmp/b" in lines[2]
    assert all("/tmp/old" not in line for line in lines)
    assert app.handle_key(None, ord("s"))  # opens the project-sort picker
    assert app.sort_menu and app.sort_menu_index == 1  # current is tokens
    app.handle_key(None, ord("j"))  # -> sessions
    app.handle_key(None, 10)  # Enter applies
    assert app.project_sort_by == "sessions"
    assert app.sort_by == "cost"


def test_day_projects_are_scoped():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", directory="/tmp/a"),
            workflow("b", "2026-06-02 12:00:00", directory="/tmp/b"),
        ]
    )
    app.focus = "days"
    app.tab = app.day_tabs.index("Projects")

    lines = app.renderer.day_projects(app.selected_day_summary, 100)

    assert any("/tmp/b" in line for line in lines)
    assert all("/tmp/a" not in line for line in lines)


def test_projects_panel_width_is_content_aware_and_bounded():
    longpath = "/Users/x/deeply/nested/repo/with/a/very/long/path/indeed/and/more/sub"
    wide = app_with([workflow("a", "2026-06-01 12:00:00", directory=longpath)])
    wide.set_browse_mode("projects")
    narrow = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x/y")])
    narrow.set_browse_mode("projects")

    # A long path widens the panel, but never past half the screen.
    w = wide.renderer.projects_left_width(160)
    assert w <= 160 // 2
    assert w < 160 - 44  # not maxed to the screen
    # A short-path list sizes down to its own (smaller) needs.
    assert narrow.renderer.projects_left_width(160) < w


def test_pager_lines_dispatch_session_tabs_by_name():
    # current_pager_lines feeds G / max_scroll / page scrolling; it must dispatch
    # the session tabs by NAME like draw_detail does -- current_tabs() appends
    # Turns/Tools per session, so a fixed index would clamp e.g. the Turns tab
    # against the Subagents line count.
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
    wf = app.current_session()
    assert app.current_tabs() == ("Overview", "Subagents", "Turns", "Tools", "Context")
    for name, table in (
        ("Overview", app.renderer.detail_overview),
        ("Subagents", app.renderer.detail_subagents),
        ("Turns", app.renderer.detail_turns),
        ("Tools", app.renderer.detail_tools),
        ("Context", app.renderer.detail_context),
    ):
        app.tab = app.current_tabs().index(name)
        assert app.renderer.current_pager_lines(100) == table(wf, 96)  # content = width - 4


def test_subagent_nodes_memoized_per_session():
    def node(workflow_id, depth, agent, title):
        return {
            "id": f"{workflow_id}:{depth}",
            "depth": depth,
            "agent": agent,
            "title": title,
            "created_at": "",
            "cost": 1.0,
            "model_name": "anthropic/x",
            "tokens_input": 1,
            "tokens_output": 1,
            "tokens_reasoning": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            "tokens_total": 2,
        }

    class NodeStore(FakeStore):
        node_calls = 0

        def workflow_nodes(self, workflow_id):
            self.node_calls += 1
            return [node(workflow_id, 0, "-", "root"), node(workflow_id, 1, "task", "sub")]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(NodeStore([workflow("s1", "2026-06-01 12:00:00")]), args)
    rows1 = app.session_node_rows("s1")
    rows2 = app.session_node_rows("s1")
    assert app.store.node_calls == 1  # every repaint after the first is memo-served
    assert rows1 is rows2 and [r["depth"] for r in rows1] == [0, 1]
    # The Subagents export dataset reads through the same memo (no new store call).
    kind, header, rows = app._subagents_dataset(app.loaded[0])
    assert kind == "subagents" and app.store.node_calls == 1
    assert [r[1] for r in rows] == [1]  # depth-0 root filtered out, subagent kept
    # Reload drops the memo -- the underlying data may have changed.
    app.reload()
    app.session_node_rows("s1")
    assert app.store.node_calls == 2


def test_session_data_ready_flips_after_prefetch():
    # The TUI's drill-in loading frame: a session whose lazy fetches aren't
    # memoized isn't "ready" (draw_detail paints the loading placeholder instead
    # of blocking mid-draw), and one prefetch_session_data satisfies every gate
    # so the next frame renders real data -- the prefetch must never leave
    # ready() False (that would be a loading-frame loop).
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        rows_in = [
            _claude_msg(
                "s1", "claude-opus-4-8", _usage(100, 50), uuid="a1", cwd=cwd, tools=["Bash"]
            ),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows_in)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        app = ot.App(store, type("Args", (), {"since": None, "until": None, "days": None})())
        assert not app.session_data_ready("s1")
        app.prefetch_session_data("s1")
        assert app.session_data_ready("s1")
        assert app.session_tool_rows("s1")[0]["tool"] == "Bash"


class _ContextStore(FakeStore):
    # Turn rows whose recorded prompts grow, get compacted once, then regrow; the
    # oversized subagent turn must never bend the main-thread curve.
    SIZES = (40_000, 80_000, 120_000, 160_000, 60_000, 90_000)

    def supports_turns(self, wid):
        return True

    def message_timeline(self, wid):
        rows = []
        for i, v in enumerate(self.SIZES):
            rows.append(
                {
                    "time": f"2026-06-01 12:00:{i:02d}",
                    "agent": "-",
                    "depth": 0,
                    "model_name": "anthropic/claude-testmodel",
                    "cost": 0.0,
                    "input": 1000,
                    "output": 50,
                    "reasoning": 0,
                    "cache_read": v - 1000,
                    "cache_write": 0,
                    "tokens_total": v + 50,
                    "prompt_id": "p1",
                    "prompt_title": "hi",
                }
            )
        rows.append(
            {
                "time": "2026-06-01 12:00:99",
                "agent": "subagent",
                "depth": 1,
                "model_name": "anthropic/claude-testmodel",
                "cost": 0.0,
                "input": 900_000,
                "output": 10,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "tokens_total": 900_010,
                "prompt_id": "p1",
                "prompt_title": "hi",
            }
        )
        return rows


def test_context_tab_charts_measured_growth_and_marks_compaction():
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(_ContextStore([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    assert "Context" in app.current_tabs()
    lines = app.renderer.detail_context(app.current_session(), 90)
    joined = "\n".join(lines)
    # measured stats: the peak is the biggest main-thread turn (not the huge
    # subagent turn), the window comes from the family fallback (claude -> 200k)
    assert joined.startswith("# Context — anthropic/claude-testmodel · 200.0k window")
    assert "160.0k" in joined and "of the window" in joined
    assert "6 turns" in joined and "900.0k" not in joined
    # the 160k -> 60k drop is a compaction: counted, marked and itemized
    assert "compacted 1×" in joined and "freed ~100.0k" in joined
    assert "▼" in joined and "160.0k → 60.0k" in joined
    # chart rows carry plain heat levels (colors resolve only at paint time)
    heat = app.renderer._ctx_line_heat
    assert heat and all(isinstance(lvl, int) for lvl in heat.values())
    # one color grammar: the peak line wears the heat of the height it describes
    # (160k of the 200k window -> the hottest band), and every ▼ compaction line
    # shares the marker row's amber
    peak_idx = next(i for i, ln in enumerate(lines) if ln.startswith("  peak"))
    assert heat[peak_idx] == 4  # int(160/200 * 5)
    for i, ln in enumerate(lines):
        if ln.startswith(("  compacted", "  ▼")):
            assert heat[i] == app.renderer._CTX_MARK
    # no composition opt-in on this store -> the estimated section stays absent
    assert "What filled it" not in joined


def test_context_tab_no_usage_message():
    args = type("Args", (), {"since": None, "until": None, "days": None})()

    class NoUsage(FakeStore):
        def supports_turns(self, wid):
            return True

        def message_timeline(self, wid):
            return []

    app = ot.App(NoUsage([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    lines = app.renderer.detail_context(app.current_session(), 80)
    assert lines[0] == "# Context"
    assert "No per-turn context usage" in lines[1]


def test_context_tab_hidden_when_curve_unsupported():
    # A backend whose turn rows are cumulative deltas, not per-request prompt
    # sizes (Codex), opts out of the curve -- the whole tab disappears rather
    # than charting per-turn consumption as context. Turns/Tools stay.
    class DeltaStore(_ContextStore):
        def supports_context_curve(self, wid):
            return False

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(DeltaStore([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    tabs = app.current_tabs()
    assert "Turns" in tabs and "Context" not in tabs
    # CodexStore itself is the real opt-out
    codex = ot.CodexStore("/nonexistent", type("A", (), {"demo": False})())
    assert codex.supports_context_curve("any") is False


def test_context_tab_flags_mixed_model_windows():
    # After a mid-session model switch the chart still scales to the last model's
    # window (declared in the header), but the peak %% must use the window the
    # peak turn actually ran in, and a "!" caveat calls out the mixed windows.
    class SwitchStore(_ContextStore):
        def message_timeline(self, wid):
            rows = super().message_timeline(wid)
            rows[0]["model_name"] = "openai/gpt-5-early"  # 400k fallback window
            return rows

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(SwitchStore([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    joined = "\n".join(app.renderer.detail_context(app.current_session(), 90))
    # peak turn (160k) ran on the claude model -> 200k window -> 80%
    assert "(80%)" in joined
    assert "! this session switched between models" in joined
    assert "200.0k window" in joined  # header still names the live window


def test_context_tab_overlays_spend_wallclock_and_compaction_times():
    # The graph carries how the session evolved in real time and what it cost, on
    # top of the token curve: a spend + $/h burn line, the wall-clock span, clock
    # edges on the x-axis, and each compaction stamped with its time-into-session.
    class SpanStore(FakeStore):
        SIZES = (30_000, 70_000, 120_000, 180_000, 55_000, 95_000, 150_000, 60_000, 110_000)
        TIMES = ("09:00", "09:20", "09:55", "10:30", "10:45", "11:20", "11:58", "12:07", "12:15")

        def supports_turns(self, wid):
            return True

        def message_timeline(self, wid):
            rows = []
            for i, (v, t) in enumerate(zip(self.SIZES, self.TIMES)):
                rows.append(
                    {
                        "time": f"2026-06-01 {t}:00",
                        "agent": "-",
                        "depth": 0,
                        "model_name": "anthropic/claude-sonnet-5",
                        "cost": 0.0,
                        "input": 1000,
                        "output": 50,
                        "reasoning": 0,
                        "cache_read": v - 1000,
                        "cache_write": 0,
                        "tokens_total": v + 50,
                        "prompt_id": f"p{i}",
                        "prompt_title": "hi",
                    }
                )
            return rows

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(SpanStore([workflow("ses_1", "2026-06-01 09:00:00", cost=6.0)]), args)
    app.view = "session"
    joined = "\n".join(app.renderer.detail_context(app.current_session(), 100))
    # money overlay: session total, a per-turn figure, and a $/h burn rate (span >= 1m)
    assert "$6.00" in joined and "/turn" in joined and "/h" in joined
    # wall-clock span line: 09:00 -> 12:15 is 3h 15m
    assert "3h 15m" in joined and "09:00 → 12:15" in joined
    # x-axis edges pinned to the clock (enough turns/width for both to fit)
    assert "turn 1 · 09:00" in joined and "12:15 · turn 9" in joined
    # both compactions stamped with clock time and how far into the session they hit
    assert "▼ turn 5 · 10:45 (+1h 45m)" in joined
    assert "▼ turn 8 · 12:07 (+3h 7m)" in joined


class _TurnNavStore(FakeStore):
    # Three ▸ prompt groups (two turns each) so the Turns cursor has somewhere to go.
    def supports_turns(self, wid):
        return True

    def message_timeline(self, wid):
        rows = []
        for i, pid in enumerate(("p1", "p2", "p3")):
            for j in range(2):
                rows.append(
                    {
                        "time": f"2026-06-01 12:0{i}:0{j}",
                        "agent": "-",
                        "depth": 0,
                        "model_name": "anthropic/claude-sonnet-5",
                        "cost": 0.5,
                        "input": 1000,
                        "output": 50,
                        "reasoning": 0,
                        "cache_read": 0,
                        "cache_write": 0,
                        "tokens_total": 1050,
                        "prompt_id": pid,
                        "prompt_title": f"prompt {pid}",
                        "prompt_full": f"the full text of {pid}",
                    }
                )
        return rows


def _turns_app():
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(_TurnNavStore([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    app.tab = app.current_tabs().index("Turns")
    return app


def test_turns_cursor_walks_the_prompt_groups_with_jk_and_gG():
    app = _turns_app()
    assert app._on_turns_tab()
    assert app.turn_groups("ses_1") == ["p1", "p2", "p3"]
    assert app._turn_cursor == 0
    app.move(1)
    assert app._turn_cursor == 1  # j steps one group
    app.move(1)
    app.move(1)
    assert app._turn_cursor == 2  # clamped at the last group, not past it
    app.move(-1)
    assert app._turn_cursor == 1  # k steps back
    app.jump(to_end=True)
    assert app._turn_cursor == 2  # G -> last prompt
    app.jump(to_end=False)
    assert app._turn_cursor == 0  # g -> first prompt


def test_turns_enter_drills_into_the_selected_prompt_and_esc_steps_back():
    app = _turns_app()
    app.move(1)  # select p2
    assert app._toggle_turn_cursor()  # the Enter path
    assert app.turn_drill == 1  # the ordinal of the selected run
    wf = app.current_session()
    body = "\n".join(app.renderer.detail_turn_drill(wf, 90))
    assert "the full text of p2" in body and "the full text of p1" not in body
    # The table underneath never re-shapes: opening a prompt is not a fold, so no row
    # moves and the reader's place in the list survives the round trip.
    before = app.renderer.detail_turns(wf, 96)
    assert app.renderer.detail_turns(wf, 96) == before
    assert app.close_turn_drill() and app.turn_drill is None
    assert app.close_turn_drill() is False  # nothing left to close


def test_turns_cursor_line_tracks_the_selected_row():
    app = _turns_app()
    app._turn_cursor = 0
    lines = app.renderer.detail_turns(app.current_session(), 96)
    first = app.renderer._turn_cursor_line
    assert first is not None and "p1" in lines[first]  # the first prompt's own row
    app._turn_cursor = 2
    app.renderer.detail_turns(app.current_session(), 96)
    assert app.renderer._turn_cursor_line > first  # a later header line


def test_turns_follow_scroll_reveals_an_offscreen_cursor():
    app = _turns_app()
    app.renderer._turn_cursor_line = 20
    app.scroll = 0
    app.renderer._scroll_turn_cursor_into_view(visible=5)
    assert app.scroll == 16  # 20 - 5 + 1: scrolled down just enough to show line 20
    app.renderer._turn_cursor_line = 2
    app.renderer._scroll_turn_cursor_into_view(visible=5)
    assert app.scroll == 2  # scrolled back up to reveal an earlier header


def test_turns_click_moves_the_keyboard_cursor_onto_the_group():
    app = _turns_app()
    rnd = app.renderer
    rnd.detail_turns(app.current_session(), 96)  # a paint records the header lines
    line_of = {n: i for i, n in rnd._turn_header_at.items()}
    app._apply_click(("turnline", line_of[2]), drill=False)
    assert app._turn_cursor == 2 and app.turn_drill == 2


def test_turns_drill_scrolls_the_pane_and_esc_steps_back_out():
    # A drilled prompt is an ordinary view, not a modal: j/k scroll the PANE (there is no
    # prompt cursor in there to move), and Esc steps back to the table rather than out of
    # the session -- the trend_drill rule.
    app = _turns_app()
    app._turn_cursor = 1
    app._toggle_turn_cursor()
    assert app.turn_drill == 1 and app.scroll == 0
    app.move(1)
    assert app.scroll == 1  # the pane, not a cursor
    assert app._turn_cursor == 1  # which stayed put, ready for the step back
    assert app.handle_key(None, 27)  # Esc
    assert app.turn_drill is None and app.view == "session"  # back to the table, not out
    assert app.handle_key(None, 27) and app.view != "session"  # a second Esc leaves


def test_turns_cursor_hands_the_key_back_at_either_end_so_the_pane_scrolls():
    # The cursor swallowed j/k unconditionally, so once it reached the last prompt the
    # pane stopped: everything below the last row -- the tab's own footnotes -- was
    # unreachable. At an end the key belongs to the pane.
    app = _turns_app()
    app._turn_cursor = 0
    assert app._move_turn_cursor(-1) is False  # already at the top
    last = len(app.turn_groups(app.current_session().id)) - 1
    app._turn_cursor = last
    assert app._move_turn_cursor(1) is False  # ...and at the bottom
    assert app._move_turn_cursor(-1) is True  # but it still moves in between
    app._turn_cursor = last
    app.scroll = 0
    app.move(1)
    assert app.scroll == 1  # the pane took the key the cursor could not use


def test_machine_overview_shows_live_pulled_and_freshness_niceties():
    # The Machines-mode main view carries what the plain rollup can't: live vs pulled,
    # the pull time + version, and (for a pulled box) the summary-only caveat.
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00", cost=3.0)],
            "server": [workflow("b", "2026-05-02 10:00:00", cost=9.0)],
        }
    )
    app.set_browse_mode("machines")
    # laptop (live) is first: full drill-in, no export stamp.
    live = "\n".join(app.renderer.machine_overview(app.machines[0], 100))
    assert "● live" in live and "Summary only" not in live
    # server (pulled): the pulled-summary niceties + the re-pull hint.
    pulled = "\n".join(app.renderer.machine_overview(app.machines[1], 100))
    assert "○ pulled" in pulled
    assert "opentab:      1.6.0" in pulled
    assert "Pulled:" in pulled
    assert "Summary only" in pulled and "F to re-pull" in pulled


def test_machine_detail_dispatches_its_tabs():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00")],
            "server": [workflow("b", "2026-05-02 10:00:00")],
        }
    )
    app.set_browse_mode("machines")
    app.machine_index = 1  # server
    r = app.renderer
    machine = app.selected_machine_summary
    assert r.machine_workflows(machine, 100)  # its sessions table
    assert any("Spend by harness" in ln for ln in r.machine_sources(machine, 100))
    assert r.machine_projects(machine, 100)  # its projects table
    assert any("Model" in ln for ln in r.machine_models(machine, 100))


def test_section_headings_use_the_accent_not_structural_grey():
    # The "# ..." pane titles read as headings (the accent pair), not the muted structural
    # pair they used to share with the keybar/frame -- the "grey on black" complaint.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n  # identity: read the pair number off the attr
    try:
        assert app.renderer.line_attr("# Money card") == (2 | ot.curses.A_BOLD)  # pair 2 == accent
        assert app.renderer.line_attr("# Top Models") == (2 | ot.curses.A_BOLD)
        # a ruled-box title (top border with a title) matches its "# " siblings
        assert app.renderer.line_attr("┌ Top Models ──────┐") == (2 | ot.curses.A_BOLD)
    finally:
        ot.curses.color_pair = orig


def test_detail_tabs_center_as_accent_and_chip_pairs():
    # Every tab is a chip: the active one filled with the accent (pair 7), the inactive
    # ones a raised panel2 chip (_TAB_PAIR) so they don't vanish into the background; the
    # detail strip is centered, so the first tab starts past the left edge.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    r = app.renderer
    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n
    try:
        r.oy = r.ox = 0
        r.regions = []
        scr = AttrScreen(6, 80)
        r.draw_tabs(scr, 0, 0, 80, ("Overview", "Models", "Sessions"), 0, center=True)
        tabregs = [rg for rg in r.regions if rg[0] == "tab"]
        assert [rg[-1] for rg in tabregs] == [0, 1, 2]  # all three clickable
        assert tabregs[0][2] > 0  # centered: leading slack before the first tab
        assert scr.attrs[(0, tabregs[0][2])] == (7 | ot.curses.A_BOLD)  # active = accent fill
        assert scr.attrs[(0, tabregs[1][2])] == r._TAB_PAIR  # inactive = the chip pair
    finally:
        ot.curses.color_pair = orig


def test_ranked_group_table_header_lines_up_with_short_names():
    """The name column was sized to the DATA alone, so when every name is shorter than the
    header label ("Harness", "Machine", "Provider") the header's own field overflowed and
    pushed Cost/Share/Tokens/Sess right of the numbers they label. Short hostnames make
    that the default in a fleet view; _model_table already guards the same way."""
    app = app_with([workflow("s1", "2026-07-01 12:00:00", cost=4.0)])
    rows = [
        ("pi", {"cost": 4.0, "tokens": 4000, "sessions": 2}),
        ("Zaly", {"cost": 2.0, "tokens": 2000, "sessions": 1}),
    ]
    lines = app.renderer._group_table(rows, 90, "harness", "Harness")
    header = next(line for line in lines if "Cost" in line)
    row = next(line for line in lines if "$4.00" in line)
    # The Cost header and the cost it labels must end in the same column.
    assert header.index("Cost") + len("Cost") == row.index("$4.00") + len("$4.00")


def _cache_miss_app():
    # One session whose second prompt landed two hours after the first finished, by
    # which time the 1h-TTL cache holding 300k tokens of context had died.
    class MissStore(FakeStore):
        def supports_turns(self, wid):
            return True

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(MissStore([workflow("s1", "2026-06-10 10:00:00", directory="/repo")]), args)
    app._turns_by_session["s1"] = [
        {
            "time": "2026-06-10 10:00:00",
            "depth": 0,
            "agent": "-",
            "model_name": "anthropic/claude-opus-4-8",
            "cost": 1.0,
            "input": 10,
            "output": 100,
            "reasoning": 0,
            "cache_read": 200000,
            "cache_write": 100000,
            "cache_write_1h": 100000,
            "tokens_total": 300110,
            "prompt_id": "a",
            "prompt_title": "first question",
            "prompt_full": "first question",
        },
        {
            "time": "2026-06-10 12:00:00",
            "depth": 0,
            "agent": "-",
            "model_name": "anthropic/claude-opus-4-8",
            "cost": 2.0,
            "input": 10,
            "output": 100,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 300000,
            "cache_write_1h": 300000,
            "tokens_total": 300110,
            "prompt_id": "b",
            "prompt_title": "the late follow-up",
            "prompt_full": "the late follow-up",
        },
    ]
    return app


def test_detail_turns_marks_the_prompt_that_arrived_after_the_cache_expired():
    app = _cache_miss_app()
    lines = ot.Renderer(app).detail_turns(app.loaded[0], 96)
    joined = "\n".join(lines)
    # The tab title carries the count and the money, like the ▼ compaction summary.
    assert "❄ 1 cache expiry, $" in lines[0]
    mark = next(i for i, ln in enumerate(lines) if ln.startswith("❄ "))
    # ABOVE the row it belongs to -- the wait happened before that prompt -- and flush
    # left, outside the table's own columns, because it is an event, not a prompt.
    assert "the late follow-up" in lines[mark + 1]
    assert not lines[mark + 1].startswith("❄")
    assert "2h idle" in lines[mark] and "300.0k bought again" in lines[mark]
    assert "it lived 1h" in lines[mark]  # the deadline that was missed, not just the gap
    # The first prompt is untouched: nothing expired before it.
    assert not lines[mark - 1].startswith("❄ ") if mark else True
    assert joined.count("❄ cache expired") == 1
    # Painted off its leading glyph, like every other prefix-styled line in the panes
    # (line_attr gives "❄ " its own branch -- red, where "! " caveats are amber).
    assert lines[mark].startswith("❄ ")


def test_detail_turns_stays_silent_when_the_backend_cannot_support_the_reading():
    # Same rows, but a backend whose turn rows are cumulative deltas (Codex) or synthetic
    # (the CSV/JSONL logs): reading a row's cache split as one request's prompt is exactly
    # what it cannot support. Gated by the SAME opt-in as the ▼ compaction markers, so the
    # Turns and Context tabs can never disagree about one session.
    app = _cache_miss_app()
    app.store.supports_context_curve = lambda _id: False
    lines = ot.Renderer(app).detail_turns(app.loaded[0], 96)
    assert not any(ln.startswith("❄") for ln in lines)
    assert "❄" not in lines[0]


def test_turn_cursor_and_table_rows_split_the_prompts_the_same_way():
    # Two places split the turns into prompts: App.turn_groups (which the cursor, j/k,
    # g/G and Enter index into, computed without a paint) and Renderer.turn_group_rows
    # (which builds the drawn rows). If they ever disagree, Enter opens a different
    # prompt than the highlighted row -- silently, and only on sessions whose shape
    # happens to differ.
    app = _turns_app()
    wf = app.current_session()
    rows = app.session_turn_rows(wf.id)
    groups = app.renderer.turn_group_rows(rows, app.renderer.turn_costs(rows))
    assert [g["id"] for g in groups] == app.turn_groups(wf.id)

    # And every drawn row's line index maps back to its own ORDINAL, in order, so the
    # click ordinal is the cursor ordinal.
    app.renderer.detail_turns(wf, 96)
    drawn = [n for _line, n in sorted(app.renderer._turn_header_at.items())]
    assert drawn == list(range(len(groups)))

    # The aggregate really is the group's turns, not a sample of them.
    assert sum(g["turns"] for g in groups) == len(rows)
    assert sum(g["tokens"] for g in groups) == sum(r["tokens_total"] for r in rows)

    # A prompt_id is NOT unique -- a backend without explicit ids groups by the prompt
    # TEXT, so asking the same thing twice in one session gives A, B, A. Keyed by id the
    # two A runs merged into one row worth both costs while turn_groups counted three,
    # leaving the cursor's last ordinal addressing a row that was never drawn.
    repeated = [dict(rows[0]), dict(rows[0]), dict(rows[0])]
    for r, pid in zip(repeated, ("A", "B", "A")):
        r["prompt_id"] = pid
    runs = app.renderer.turn_group_rows(repeated, [1.0, 2.0, 3.0])
    assert [g["id"] for g in runs] == ["A", "B", "A"]
    assert [g["cost"] for g in runs] == [1.0, 2.0, 3.0]  # not one merged $4.00 A row


def test_turns_table_budgets_its_optional_columns_against_the_pane():
    # Optional cells are dropped deliberately rather than left to overflow and be clipped
    # at paint: a column the frame eats takes the prompt text with it, which is what a
    # prompt list is read by. The bar goes first (it restates the Cost cell), Cumulative
    # second, and the prompt keeps its floor.
    app = _turns_app()
    wf = app.current_session()
    wide = app.renderer.detail_turns(wf, 130)
    assert all(len(ln) <= 130 for ln in wide if ln.startswith("  "))
    assert "Cumulative" in wide[1] and any("█" in ln or "▏" in ln for ln in wide)

    mid = app.renderer.detail_turns(wf, 88)  # bar dropped, Cumulative kept
    assert all(len(ln) <= 88 for ln in mid if ln.startswith("  "))
    assert "Cumulative" in mid[1]
    assert not any("█" in ln or "▏" in ln for ln in mid if ln.startswith("  "))

    narrow = app.renderer.detail_turns(wf, 72)  # both dropped
    assert all(len(ln) <= 72 for ln in narrow if ln.startswith("  "))
    assert "Cumulative" not in narrow[1]
    # ...and the columns that carry the answer never go.
    for line in (wide[1], mid[1], narrow[1]):
        assert "Prompt" in line and "Turns" in line and "Cached" in line and "Cost" in line


def test_turns_paints_the_selected_prompt_row_and_the_highlight_follows_j_k():
    # The cursor has to be VISIBLE, and this is the test that was missing: the highlight
    # used to be keyed on a "▸ " prefix, so when the headers became ordinary table rows
    # it silently matched nothing -- j/k moved a selection the eye could not find, with
    # the whole suite green. Keyed on the line index now, and asserted at PAINT.
    app = _turns_app()
    # draw_detail paints a "Loading session" frame until the lazy per-session fetches are
    # memoized, so fill the memos this tab reads before painting.
    wid = app.current_session().id
    app.session_turn_rows(wid)
    app._nodes_by_session[wid] = []
    app._tool_by_session[wid] = []
    app._context_by_session[wid] = []
    screen = AttrScreen(30, 100)
    # Headless: color_pair() needs a real terminal, so stand it in with something that
    # keeps the attribute bits distinguishable (the token-runs test's trick).
    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8

    def paint():
        screen.attrs.clear()
        app.renderer.draw_detail(screen, 0, 0, 26, 100)

    def selected_rows():
        # The screen rows painted in reverse video -- the pickers' selection look.
        return sorted({y for (y, _x), a in screen.attrs.items() if a & ot.curses.A_REVERSE})

    try:
        paint()
        first = selected_rows()
        assert len(first) == 1, "exactly one prompt row is selected"

        app.handle_key(None, ord("j"))
        paint()
        moved = selected_rows()
        assert len(moved) == 1 and moved[0] > first[0], "j moves the highlight down a row"

        app.handle_key(None, ord("k"))
        paint()
        assert selected_rows() == first, "k brings it back"
    finally:
        ot.curses.color_pair = orig


def test_turns_cached_reports_the_prompts_first_turn_not_an_average_of_them():
    # Every turn after the first is warm by construction -- the one before it just wrote
    # the cache -- so averaging a prompt's turns drags every row toward 100% and buries
    # the only moment that could have missed. Measured on a real session, the prompt
    # after an 8h44m expiry averaged to 76% while the turn that mattered read back 5%,
    # directly under a ❄ marker saying it had re-bought the lot.
    class ColdStartStore(FakeStore):
        def supports_turns(self, wid):
            return True

        def message_timeline(self, wid):
            base = {
                "depth": 0,
                "agent": "-",
                "model_name": "anthropic/claude-opus-4-8",
                "cost": 1.0,
                "input": 10,
                "output": 100,
                "reasoning": 0,
                "cache_write_1h": 0,
                "prompt_id": "p1",
                "prompt_title": "after the wait",
                "prompt_full": "after the wait",
            }
            return [
                # cold: bought its whole context again
                dict(
                    base,
                    time="2026-06-10 12:00:00",
                    cache_read=0,
                    cache_write=300_000,
                    tokens_total=300_110,
                ),
                # ...then three warm turns, which an average would hide it behind
                *(
                    dict(
                        base,
                        time=f"2026-06-10 12:0{i}:00",
                        cache_read=300_000,
                        cache_write=500,
                        tokens_total=300_610,
                    )
                    for i in (1, 2, 3)
                ),
            ]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(ColdStartStore([workflow("s1", "2026-06-10 12:00:00", cost=4.0)]), args)
    wf = app.loaded[0]
    rows = app.session_turn_rows(wf.id)
    (g,) = app.renderer.turn_group_rows(rows, app.renderer.turn_costs(rows))
    assert g["turns"] == 4
    assert g["cached"] == 0.0  # the first turn's own share, not ~75%

    # A prompt whose turns were ALL subagent work has no main-thread context to answer
    # for -- subagents run in their own windows -- so it reports nothing, not a number
    # borrowed from the subagent.
    for r in rows:
        r["depth"] = 1
    assert app.renderer.turn_group_rows(rows, app.renderer.turn_costs(rows))[0]["cached"] is None


def _drill_app():
    class DrillStore(FakeStore):
        def supports_turns(self, wid):
            return True

        def message_timeline(self, wid):
            return [
                {
                    "depth": 0,
                    "agent": "-",
                    "model_name": "anthropic/claude-opus-4-8",
                    "cost": 1.0,
                    "input": 10,
                    "output": 10,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "cache_write_1h": 0,
                    "tokens_total": 20,
                    "prompt_id": f"p{i}",
                    "prompt_title": f"prompt {i}",
                    "prompt_full": f"prompt {i}",
                    "time": f"2026-06-01 12:0{i}:00",
                }
                for i in range(4)
            ]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(DrillStore([workflow("s1", "2026-06-01 12:00:00", cost=4.0)]), args)
    app.view = "session"
    app.tab = app.current_tabs().index("Turns")
    return app


def test_a_drilled_prompt_does_not_leave_the_tables_hit_testing_armed():
    # draw_detail lays a "turnline" region over whatever the pane is showing, so the
    # TABLE's line->ordinal map and selected line have to go when the drill takes over.
    # Left standing, a click on drilled prompt TEXT re-drilled whichever row used to
    # occupy that screen line, and the stale cursor line highlighted an unrelated line.
    app = _drill_app()
    rnd = app.renderer
    rnd.detail_turns(app.current_session(), 100)  # the table
    stale_line = min(rnd._turn_header_at)
    app.open_turn_drill(2)
    rnd.detail_turns(app.current_session(), 100)  # the drill
    assert rnd._turn_header_at == {} and rnd._turn_cursor_line is None
    app._apply_click(("turnline", stale_line), drill=False)
    assert app.turn_drill == 2  # still where the reader put it


def test_jump_keys_belong_to_the_drilled_pane_not_the_hidden_prompt_cursor():
    # g/G scroll the drilled view. They used to fall through to the table's cursor, so g
    # left the pane where it was and silently moved a selection nobody could see.
    app = _drill_app()
    app.open_turn_drill(2)
    app.scroll, app._turn_cursor = 5, 2
    app.jump(to_end=False)
    assert app.scroll == 0 and app._turn_cursor == 2  # the pane moved, the cursor did not
    app.jump(to_end=True)
    assert app._turn_cursor == 2


def test_esc_only_leaves_a_drilled_prompt_while_the_turns_tab_is_showing():
    # Ungated, Esc on Tools or Context tore down an invisible drill and was swallowed --
    # the key did nothing the reader could see, instead of stepping out of the session.
    app = _drill_app()
    app.open_turn_drill(1)
    app.tab = app.current_tabs().index("Overview")
    app.handle_key(None, 27)
    assert app.view != "session"  # Esc stepped out, as the visible tab implies
    assert app.turn_drill == 1  # ...and the drill it could not see is untouched


def test_turns_footnotes_wrap_instead_of_being_clipped_mid_sentence():
    # Everything else on the tab is width-budgeted; these were not, and ran ~50 characters
    # past a 100-column pane, where the paint clips rather than wraps.
    app = _turns_app()
    for width in (72, 100, 140):
        lines = app.renderer.detail_turns(app.current_session(), width)
        assert all(len(ln) <= width for ln in lines if ln.startswith(("· ", "  ")))


def test_a_drill_never_follows_the_reader_into_another_session():
    # An ordinal is only meaningful inside ONE session's prompt list, and unlike the
    # prompt_id it replaced it is valid in almost any session -- so a drill left armed
    # while another session comes on screen would silently open THAT session's Nth
    # prompt. Checked at the point of use rather than cleared on each path that can swap
    # the session, because _restore_mode_memory did not clear it and enumerating those
    # paths is the bet that failed.
    class TwoSessionStore(FakeStore):
        def supports_turns(self, wid):
            return True

        def message_timeline(self, wid):
            return [
                {
                    "depth": 0,
                    "agent": "-",
                    "model_name": "anthropic/claude-opus-4-8",
                    "cost": 1.0,
                    "input": 10,
                    "output": 10,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "cache_write_1h": 0,
                    "tokens_total": 20,
                    "prompt_id": f"{wid}-p{i}",
                    "prompt_title": f"{wid} prompt {i}",
                    "prompt_full": f"{wid} prompt {i}",
                    "time": f"2026-06-01 12:0{i}:00",
                }
                for i in range(4)
            ]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(
        TwoSessionStore(
            [
                # Same day and project, so both are in scope at once and the selection
                # can move between them the way the mode-memory restore moves it.
                workflow("A", "2026-06-01 12:00:00", cost=4.0, directory="/a"),
                workflow("B", "2026-06-01 13:00:00", cost=4.0, directory="/a"),
            ]
        ),
        args,
    )
    app.view = "session"
    app.workflow_index = 0
    app.tab = app.current_tabs().index("Turns")
    assert app.current_session().id == "A"
    app.open_turn_drill(2)
    assert "A prompt 2" in "\n".join(app.renderer.detail_turns(app.current_session(), 90))

    # Put session B on screen WITHOUT going through a path that clears the drill -- the
    # mode-memory restore does exactly this, rebinding view/session/tab and nothing else
    # (it moves the selection by value and never touches turn_drill).
    app.workflow_index = 1
    assert app.current_session().id == "B"
    assert app.active_turn_drill is None  # ...so B shows its table, not A's third prompt
    shown = "\n".join(app.renderer.detail_turns(app.current_session(), 90))
    assert "B prompt 0" in shown and "A prompt" not in shown


def test_turns_footnotes_fit_an_eighty_column_terminal():
    # The wrap fix indented continuations AFTER wrapping, so they ran two cells past the
    # pane and the longest footnote was still clipped at the minimum supported width.
    # Needs a session that actually HAS a cache expiry -- the plain fixture never renders
    # that note, which is why the first version of this test passed while it was broken.
    app = _cache_miss_app()
    for width in (74, 80, 100):
        lines = app.renderer.detail_turns(app.loaded[0], width)
        assert any("❄ the prompt cache expired" in ln for ln in lines), width
        assert all(len(ln) <= width for ln in lines), width


def test_esc_is_not_swallowed_by_a_drill_the_reader_cannot_see():
    # The mirror of the Esc-from-another-tab bug, reached the other way: bind the drill to
    # its session and a drill armed elsewhere is inert but still SET, so an Esc consumed
    # to tear it down does nothing to the table the reader is actually looking at.
    class TwoSessionStore(FakeStore):
        def supports_turns(self, wid):
            return True

        def message_timeline(self, wid):
            return [
                {
                    "depth": 0,
                    "agent": "-",
                    "model_name": "anthropic/claude-opus-4-8",
                    "cost": 1.0,
                    "input": 10,
                    "output": 10,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "cache_write_1h": 0,
                    "tokens_total": 20,
                    "prompt_id": f"{wid}-p{i}",
                    "prompt_title": f"{wid} p{i}",
                    "prompt_full": f"{wid} p{i}",
                    "time": f"2026-06-01 12:0{i}:00",
                }
                for i in range(4)
            ]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(
        TwoSessionStore(
            [
                workflow("A", "2026-06-01 12:00:00", cost=4.0, directory="/a"),
                workflow("B", "2026-06-01 13:00:00", cost=4.0, directory="/a"),
            ]
        ),
        args,
    )
    app.view = "session"
    app.workflow_index = 0
    app.tab = app.current_tabs().index("Turns")
    app.open_turn_drill(2)
    app.workflow_index = 1  # B on screen; A keeps the armed ordinal
    assert app.active_turn_drill is None  # the reader sees B's TABLE

    app.handle_key(None, 27)
    assert app.view != "session"  # Esc stepped out, as the visible pane implies
    # ...and A's drill is untouched: it is remembered state for the session that owns it
    # (mode memory keeps the matching scroll offset beside it), so an Esc pressed while
    # looking at B must not destroy it.
    assert app.turn_drill == 2
    app.view = "session"
    app.workflow_index = 0
    assert app.active_turn_drill == 2  # back on A, the drill is live again


def test_a_drill_survives_a_browse_mode_round_trip_with_its_scroll():
    # A drill is remembered state for the session that owns it, and mode memory keeps the
    # matching scroll offset beside it. Tidying an inactive drill away on Esc meant an Esc
    # pressed in one mode destroyed a drill belonging to another: coming back restored the
    # session, its tab and its DRILLED scroll, then rendered the prompt table at that
    # offset. Drives the real mode-memory path, which the single-session test cannot.
    class TwoProjectStore(FakeStore):
        def supports_turns(self, wid):
            return True

        def message_timeline(self, wid):
            return [
                {
                    "depth": 0,
                    "agent": "-",
                    "model_name": "anthropic/claude-opus-4-8",
                    "cost": 1.0,
                    "input": 10,
                    "output": 10,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "cache_write_1h": 0,
                    "tokens_total": 20,
                    "prompt_id": f"{wid}-p{i}",
                    "prompt_title": f"{wid} p{i}",
                    "prompt_full": f"{wid} p{i}",
                    "time": f"2026-06-01 12:0{i}:00",
                }
                for i in range(4)
            ]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(
        TwoProjectStore(
            [
                workflow("A", "2026-06-01 12:00:00", cost=4.0, directory="/a"),
                workflow("B", "2026-06-02 12:00:00", cost=4.0, directory="/b"),
            ]
        ),
        args,
    )

    def open_turns():
        app.drill_in()
        app.tab = app.current_tabs().index("Sessions")
        app.drill_in()
        app.tab = app.current_tabs().index("Turns")

    # Projects mode remembers B's Turns table...
    app.set_browse_mode("projects")
    app.project_index = next(i for i, p in enumerate(app.projects) if p.directory == "/b")
    open_turns()
    assert app.current_session().id == "B"

    # ...while Time mode drills into A.
    app.set_browse_mode("time")
    app.focus = "days"
    app.day_index = next(i for i, d in enumerate(app.panel_days) if d.day == "2026-06-01")
    open_turns()
    assert app.current_session().id == "A"
    app.open_turn_drill(2)
    app.scroll = 3

    # Esc while looking at B must leave B, and leave A's drill alone.
    app.set_browse_mode("projects")
    app.handle_key(None, 27)
    assert app.view != "session"
    assert app.turn_drill == 2

    app.set_browse_mode("time")
    assert app.current_session().id == "A"
    assert app.active_turn_drill == 2 and app.scroll == 3  # the drill, at its own offset


def test_drilled_turns_show_the_agent_label_the_backend_gave_them():
    # The page prints a main-thread turn's own agent label; the TUI forced "-" and threw
    # it away. OpenCode names its main agent, so the two frontends disagreed about a
    # visible cell on 1,574 turns of a real corpus. A subagent is marked "↳" on both.
    class AgentStore(FakeStore):
        def supports_turns(self, wid):
            return True

        def message_timeline(self, wid):
            base = {
                "model_name": "anthropic/claude-opus-4-8",
                "cost": 1.0,
                "input": 10,
                "output": 10,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "cache_write_1h": 0,
                "tokens_total": 20,
                "prompt_id": "p1",
                "prompt_title": "t",
                "prompt_full": "t",
            }
            return [
                dict(base, time="2026-06-01 12:00:00", depth=0, agent="build"),
                dict(base, time="2026-06-01 12:01:00", depth=1, agent="explore"),
                dict(base, time="2026-06-01 12:02:00", depth=0, agent="-"),
            ]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(AgentStore([workflow("s1", "2026-06-01 12:00:00", cost=3.0)]), args)
    app.view = "session"
    app.open_turn_drill(0)
    body = app.renderer.detail_turn_drill(app.current_session(), 100)
    joined = "\n".join(body)
    assert "build" in joined  # the main agent's real name, not "-"
    assert "↳ explore" in joined  # the subagent, marked
    # A backend that gives no label still shows "-", never a blank cell.
    assert any(ln.count("-") for ln in body)
