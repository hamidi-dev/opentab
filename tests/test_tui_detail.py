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
    box_cells,
    box_title,
    screen_text,
    workflow,
)

_cells = box_cells


def test_top_models_is_a_ruled_box_with_full_model_columns():
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
    app = _models_tab_app()
    month = app.selected_month_summary
    preview = app.renderer.month_models(month, 116)
    assert app.renderer._model_row_at == {}  # browse has no cursor
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    zoomed = app.renderer.month_models(month, 116)
    cursor = app.renderer._model_cursor_line
    assert [ln for i, ln in enumerate(zoomed) if i != cursor] == [
        ln for i, ln in enumerate(preview) if i != cursor
    ]
    # The one differing line differs by exactly the marker cell -- no reflow, no column
    # the zoom conjured up.
    assert zoomed[cursor].replace("> ", "  ", 1) == preview[cursor]
    assert len(zoomed[cursor]) == len(preview[cursor])
    # ...and the cursor points at a real data row of that very table.
    rows = app.renderer._model_row_at
    assert sorted(rows.values()) == [0, 1]
    assert rows[cursor] == 0 and "opus" in zoomed[cursor]


def test_the_models_filter_still_narrows_model_names_in_a_zoom():
    app = _models_tab_app()
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.query = "haiku"
    assert [m for m, _ in app.zoom_model_rows()] == ["haiku"]
    lines = app.renderer.month_models(app.selected_month_summary, 116)
    assert any("haiku" in ln for ln in lines) and not any("opus" in ln for ln in lines)
    assert len(app.renderer._model_row_at) == 1  # the cursor indexes the FILTERED rows


def test_a_shrinking_model_list_never_strands_the_cursor_off_screen():
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
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    row = _cells(app.renderer._model_table(rows, "# Top Models", 120))[1]
    assert "800.0k ($0.80)" in row
    assert "100.0k ($1.25)" in row
    assert "50.0k ($2.50)" in row


def test_model_table_split_scales_to_the_recorded_cost():
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 10.10, 1_000_000, 800_000, 100_000, 50_000)]
    row = _cells(app.renderer._model_table(rows, "# Top Models", 120))[1]
    assert "800.0k ($1.60)" in row
    assert "100.0k ($2.50)" in row
    assert "50.0k ($5.00)" in row


def test_model_table_split_cells_align_under_their_labels():
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
    app = app_with([])
    row = ("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)
    total = _cells(app.renderer._model_table([row, row], "# Top Models", 120))[-1]
    assert total.split()[0] == "TOTAL"
    assert "$10.10" in total
    assert "1.6M ($1.60)" in total
    assert "200.0k ($2.50)" in total
    assert "100.0k ($5.00)" in total


def test_model_table_total_split_dollars_keep_the_compact_label_convention():
    # money_label intentionally drops cents once this attribution crosses $10.
    app = app_with([])
    row = ("anthropic/claude-fable-5", 10, 5.05, 100_000, 0, 0, 100_000)
    _, first, second, total = _cells(app.renderer._model_table([row, row], "# Top Models", 120))
    assert "($5.05)" in first and "($5.05)" in second
    assert "($10)" in total and "$10.10" in total  # cells compact, Cost exact


def test_model_table_single_row_has_no_total():
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    lines = app.renderer._model_table(rows, "# Top Models", 120)
    assert not any("TOTAL" in ln for ln in lines)


def test_tools_table_total_row_stays_unsplit():
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
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    row = _cells(app.renderer._model_table(rows, "# Top Models", 120))[1]
    assert ")  " in row  # a "(...)" cell is followed by a two-space gutter, not one


def test_top_sessions_overview_box_caps_the_leaderboard_at_twenty():
    ws = [workflow(f"s{i}", "2026-06-01 12:00:00", cost=float(i + 1)) for i in range(25)]
    app = app_with(ws)
    assert len(app.renderer.top_sessions(ws)) == 20
    box = app.renderer._top_sessions_box(ws, sum(w.total_cost for w in ws), 120)
    data = [ln for ln in box if "$" in ln]  # the cost column marks every data row
    assert len(data) == 20
    assert "$25.00" in data[0]  # highest cost leads
    assert not any("$5.00" in ln for ln in data)  # the bottom five fall off the cap


def test_the_model_table_closes_every_overview():
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
    app = app_with([])
    ws = [workflow("a", "2026-06-01 12:00:00", cost=123456.78)]
    content = box_cells(app.renderer._top_sessions_box(ws, 200000.0, 120), lead=True)
    header, row = content[0], next(ln for ln in content if "$123,456.78" in ln)
    cw = len("$123,456.78")  # 11: the widened Cost column
    assert row[:cw] == "$123,456.78"  # full cost, not clipped to 10
    assert header[:cw] == f"{'Cost':>{cw}}"  # header padded to the same column
    assert header[cw] == " " and row[cw] == " "  # the gap after Cost -- columns aligned


def test_top_projects_box_widens_cost_column_for_six_figure_spend():
    app = app_with([])
    ws = [workflow("a", "2026-06-01 12:00:00", cost=123456.78, directory="/repo/x")]
    content = box_cells(app.renderer._top_projects_box(ws, 200000.0, 120), lead=True)
    header, row = content[0], next(ln for ln in content if "$123,456.78" in ln)
    cw = len("$123,456.78")
    assert row[:cw] == "$123,456.78"
    assert header[cw] == " " and row[cw] == " "  # columns still aligned past the wider Cost


def test_projects_merge_across_windows_slash_styles():
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


def _sessions_app(n):
    app = app_with(
        [workflow(f"s{i}", f"2026-06-{i + 1:02d} 12:00:00", title=f"t{i}") for i in range(n)]
    )
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    return app


def test_a_picker_is_a_ruled_box_and_pays_the_frame_out_of_its_row_budget():
    app = _sessions_app(40)
    painted = _paint_sessions_picker(app, 100)
    assert painted[0].startswith("┌ Sessions · 40")
    assert painted[1][:1] == "│" and "Title" in painted[1]  # header inside the gutters
    assert set(painted[2]) <= set("├┤─")  # the rule under the header
    assert painted[-1][:1] == "└"
    rows = [ln for ln in painted if re.search(r"\bt\d+\b", ln)]
    # 24-row screen: 3 chrome rows above the box, the box's own 4, one bottom border.
    assert len(rows) == 24 - 5 - ot.Renderer.PICKER_CHROME
    assert all(ln[:1] == "│" and ln[-1:] in ("│", "┃", "|", "#") for ln in rows)
    assert any(ln.endswith(("┃", "#")) for ln in rows)  # overflow adds a thumb to the border


def test_the_picker_cursor_reverses_only_the_cells_between_the_gutters():
    app = _sessions_app(3)
    app.workflow_index = 1
    screen = AttrScreen(24, 100)
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: n * 100
    ot.curses.init_pair = lambda *a: None
    try:
        app.renderer.draw_sessions_picker(screen, 0, 0, 24, 100)
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    # x=2 is the left gutter, x=4 the first content cell (the ">" marker), x=97 the right.
    y = next(i for i in range(24) if screen.cells.get((i, 4)) == ">")
    assert screen.cells[(y, 2)] == "│" and not screen.attrs[(y, 2)] & ot.curses.A_REVERSE
    assert screen.attrs[(y, 4)] & ot.curses.A_REVERSE
    assert screen.cells[(y, 97)] == "│" and not screen.attrs[(y, 97)] & ot.curses.A_REVERSE


def test_every_boxed_column_header_is_found_and_lit_the_same_way():
    app = _sessions_app(3)
    app._model_by_root = {"s0": [_model_row("opus", 9.0, 900)]}
    rnd = app.renderer
    rnd._box_headers = set()
    lines = rnd.detail_overview(app.loaded[0], 116)
    heads = rnd.box_header_lines(lines)
    assert len(heads) == 2  # the Token economics table and the model table
    assert all("Type" in lines[i] or "Model" in lines[i] for i in heads)
    # The chart caption right below the Token economics title is NOT a header.
    top = next(i for i, ln in enumerate(lines) if ln.startswith("┌ Token economics"))
    assert top + 1 not in heads and "share of tokens sent" in lines[top + 1]
    # Painted: the labels light up, the gutters stay in the frame's plain attribute.
    screen = AttrScreen(6, 120)
    orig_cp = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n * 100
    try:
        rnd._paint_box_header(screen, 0, 0, lines[min(heads)], 116)
        lit = ot.curses.color_pair(ot.Renderer.HEADER_PAIR) | ot.curses.A_BOLD
    finally:
        ot.curses.color_pair = orig_cp
    assert screen.attrs[(0, 0)] == 0 and screen.attrs[(0, 2)] == lit


def test_zoom_pickers_paint_no_enter_hint():
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
    # Both frames are the SAME ruled box: the tab name rides the top border, so the
    # picker takes over on Enter without a single row shifting.
    cells = box_cells(preview)

    app.view = "zoom"
    painted = _paint_sessions_picker(app, 100)

    # The painted picker IS a ruled box too, so both sides unwrap the same way.
    painted_cells = box_cells(painted)
    header = next(ln for ln in painted_cells if "Title" in ln)
    assert header.strip() == cells[0].strip()  # same columns, same sort arrows
    rows = [c for c in painted_cells if "first" in c or "second" in c]
    body = [c for c in cells[1:] if not c.strip().startswith("TOTAL")]
    assert rows and len(rows) == len(body)
    # The cursor marker is the only difference: strip it and the rows are identical.
    assert [r.strip().lstrip(">").strip() for r in rows] == [p.strip() for p in body]


def test_detail_tools_reprices_unpriced_under_dollar():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        app = ot.App(ot.Store(db, type("A", (), {"demo": False})()), args())
        app.toggle_api_prices()  # to the real view; the estimate is the default
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
            c
            for c in box_cells(rnd.detail_tools(wf, 92), lead=True)
            if c.startswith("serena_read_file")
        )
        assert "$1.00" in serena_line
        api_lines = "\n".join(rnd.detail_tools(wf, 92))
        assert "Tool-attributed spend · $7.00" in api_lines
        # ...and now that the estimate gives serena a width too, there are two rates to
        # rank, so the shade becomes a scale and the caption says which one.
        assert "area = visible cost · shade = $/call" in api_lines


def test_tool_treemap_shades_by_per_call_rate_not_by_its_own_area():
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
    app = app_with([])
    app.show_api_prices = False  # the real view; the estimate is the default
    rnd = app.renderer
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
        app.toggle_api_prices()  # to the real view; the estimate is the default
        rnd = ot.Renderer(app)
        wf = app.loaded[0]
        # One row per prompt, columns always drawn -- the numbers ARE the tab, so they
        # never hide inside an expansion the reader has to discover.
        table = rnd.detail_turns(wf, 96)
        tj = "\n".join(table)
        # The tab's summary rides the box's top border now, not a heading line above it.
        assert box_title(table).startswith("Turns — 2 prompts · 3 turns · $3.00")
        cells = box_cells(table)
        assert "Add feature X" in tj and "Fix the bug" in tj
        assert "Turns" in cells[0] and "Cached" in cells[0] and "Cumulative" in cells[0]
        assert "$3.00 · 100%" in tj  # the last prompt's cumulative cell
        assert any(line.strip().startswith("cost│") for line in table)
        assert any(line.strip().startswith("context│") for line in table)
        cost_strip = next(line for line in table if line.strip().startswith("cost│"))
        context_strip = next(line for line in table if line.strip().startswith("context│"))
        assert len(cost_strip.split("│", 1)[1].split("peak", 1)[0].rstrip()) == len(
            context_strip.split("│", 1)[1].split("peak", 1)[0].rstrip()
        )
        # A prompt row is a moment (MM-DD HH:MM); the seconds belong to its turns, which
        # live in the popup, so no per-turn clock stamp reaches the table.
        assert not any(re.search(r"\d\d-\d\d \d\d:\d\d:\d\d", ln) for ln in table)
        assert "Cached is context reused" in tj
        # The turns themselves, with their seconds, are one Enter away.
        app.open_turn_drill(0)
        drilled = rnd.detail_turn_drill(wf, 90)
        assert any(re.search(r"\d\d-\d\d \d\d:\d\d:\d\d", ln) for ln in drilled)
        app.close_turn_drill()  # step back out: detail_turns is the table again
        # Under "$" the two $0 haiku turns estimate at list price (1M+2M @ $1/M),
        # so the total grows to $1 + $2 + $3 = $6.00 -- in the table and the drill alike.
        app.show_api_prices = True
        priced = rnd.detail_turns(wf, 96)
        assert box_title(priced).startswith("Turns — 2 prompts · 3 turns · $6.00")
        pjoined = "\n".join(priced)
        assert "$6.00 · 100%" in pjoined and "Add feature X" in pjoined
        app.open_turn_drill(0)
        assert "$1.00" in "\n".join(rnd.detail_turn_drill(wf, 90))


def test_turns_marks_compactions_even_while_folded():
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
    # The markers ride INSIDE the box, between the rows they happened between.
    cells = box_cells(lines)
    marker = next(c for c in cells if c.startswith("▼ "))
    assert "before turn 3" in marker  # the turn that ran on the cleared window
    assert "900.0k → 300.0k" in marker and "600.0k freed" in marker
    # Exactly one: a subagent's own small context neither triggers a marker (it runs in
    # its own window) nor breaks the main thread's chain (300k → 340k is growth).
    assert sum(1 for c in cells if c.startswith("▼ ")) == 1
    assert "▼ 1 compaction, ~600.0k freed" in box_title(lines)
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
        c.startswith("▼ ")
        for c in box_cells(timeless.renderer.detail_turns(timeless.current_session(), 100))
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

        app.toggle_api_prices()  # to the real view; the estimate is the default
        # Real mode: the unpriced subagent reads as $0.00.
        real = app._priced_nodes([r for r in store.workflow_nodes("root") if r["depth"] > 0])
        assert real[0]["cost"] == 0.0
        assert "$0.00" in box_cells(app.renderer.detail_subagents(app.loaded[0], 200))[-1]

        # API mode: it is repriced to the Opus API-equivalent. _priced_nodes feeds
        # both the rendered tab and the CSV export, so asserting it covers both.
        app.toggle_api_prices()
        priced = app._priced_nodes([r for r in store.workflow_nodes("root") if r["depth"] > 0])
        assert round(priced[0]["cost"], 6) == round(expected, 6)
        sub_line = box_cells(app.renderer.detail_subagents(app.loaded[0], 200))[-1]
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
    app.subagent_sort_by = "date"
    assert [r["title"] for r in app.sorted_subagent_rows(_subagent_rows())] == ["b", "a"]
    app.subagent_sort_reverse = True  # flipped: chronological
    assert [r["title"] for r in app.sorted_subagent_rows(_subagent_rows())] == ["a", "b"]


def test_subagent_sort_is_independent_of_session_sort():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.sort_by = "date"
    app.view = "session"
    app.tab = app.workflow_tabs.index("Subagents")

    assert app.handle_key(None, ord("s"))
    assert app.sort_menu and app.sort_menu_options() == app.subagent_sort_options
    app.handle_key(None, ord("G"))  # jump to the last option (depth)
    app.handle_key(None, 10)
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
    # Start time and last activity deliberately rank these projects oppositely.
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
    head = next(i for i, ln in enumerate(lines) if "Started" in ln and ln[:1] in ("│", "|"))
    assert box_title(lines[head - 1 :]) == "Subagent Executions"
    assert "2026-06-01 12:30" in lines[head + 2]  # past the rule, the subagent's row
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

    rows = box_cells(lines)
    assert "/tmp/a" in rows[1]  # rows[0] is the column header (the title rides the border)
    assert "/tmp/b" in rows[2]
    assert all("/tmp/old" not in line for line in lines)
    assert app.handle_key(None, ord("s"))
    assert app.sort_menu and app.sort_menu_index == 1  # current is tokens
    app.handle_key(None, ord("j"))  # -> sessions
    app.handle_key(None, 10)
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


class _TurnToolStore(_TurnNavStore):
    # The same three prompt groups, but each turn names the tools it called -- p1 works
    # (Read then Bash ×2), p2 answers in prose (no calls at all), p3 drives an MCP
    # server. Enough to exercise the cell, the "-" for a call-free prompt, and the MCP
    # shortening in one fixture.
    _TOOLS = {
        ("p1", 0): ["Read"],
        ("p1", 1): ["Bash", "Bash"],
        ("p3", 0): ["mcp__chrome-devtools__take_screenshot"],
        ("p3", 1): ["Read"],
    }

    def message_timeline(self, wid):
        rows = super().message_timeline(wid)
        for i, r in enumerate(rows):  # two turns per group, in order
            r["tools"] = list(self._TOOLS.get((r["prompt_id"], i % 2), []))
        return rows


class _TurnAgentStore(_TurnNavStore):
    # p1 delegates to a NAMED agent, p2 runs on the main thread alone, p3 delegates to
    # two executions the backend never named (Claude Code's every Task) -- the three
    # cells the Agents column has to draw.
    _AGENTS = {("p1", 1): "docs", ("p3", 0): "subagent", ("p3", 1): "-"}

    def message_timeline(self, wid):
        rows = super().message_timeline(wid)
        for i, r in enumerate(rows):
            agent = self._AGENTS.get((r["prompt_id"], i % 2))
            if agent is not None:
                r["agent"], r["depth"] = agent, 1
        return rows


def _counting_turn_store(n_prompts, calls_per_turn=3):
    # A store with an arbitrary number of prompt groups, so a test can drive idx_w (the
    # "#" column, sized from the group count) independently of the tool cells.
    class _Counting(_TurnNavStore):
        def message_timeline(self, wid):
            return [
                {
                    "time": f"2026-06-01 12:{i // 60 % 60:02}:{i % 60:02}",
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
                    "prompt_id": f"p{i}",
                    "prompt_title": f"prompt {i}",
                    "prompt_full": f"full {i}",
                    "tools": ["Bash"] * calls_per_turn,
                }
                for i in range(n_prompts)
            ]

    return _Counting


def _turns_app(store_cls=_TurnNavStore):
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(store_cls([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    app.tab = app.current_tabs().index("Turns")
    return app


def test_turns_name_the_tools_each_step_called():
    app = _turns_app(_TurnToolStore)
    wf = app.current_session()
    app.open_turn_drill(0)  # p1: Read, then Bash x2
    drilled = app.renderer.detail_turn_drill(wf, 120)
    assert "Tools" in box_cells(drilled)[0]
    body = [ln for ln in drilled if "sonnet" in ln]
    assert "Read" in body[0] and "Bash ×2" in body[1]
    # The TOTAL row carries the prompt's whole mix, busiest first.
    assert "Bash ×2, Read" in "\n".join(drilled)

    # An MCP tool sheds the "mcp__" wrapper but keeps its server.
    app.open_turn_drill(2)
    assert "chrome-devtools/take_screenshot" in "\n".join(app.renderer.detail_turn_drill(wf, 120))

    # A prompt that called nothing gets no column at all -- a stripe of dashes is width
    # the model name should have had.
    app.open_turn_drill(1)
    assert "Tools" not in box_cells(app.renderer.detail_turn_drill(wf, 120))[0]


def test_turns_table_counts_the_calls_and_drops_the_column_without_them():
    app = _turns_app(_TurnToolStore)
    lines = app.renderer.detail_turns(app.current_session(), 130)
    header = box_cells(lines)[0]
    assert "Calls" in header
    rows = [ln for ln in lines if "prompt p" in ln]
    assert [ln.split()[6] for ln in rows] == ["2", "2", "2"]  # Turns
    # p1 made 3 calls, p2 none ("-", never "0"), p3 two.
    assert [ln.split()[7] for ln in rows] == ["3", "-", "2"]
    # The TOTAL row sums them: 6 turns, 3 + 0 + 2 calls.
    total = next(ln for ln in lines if "TOTAL" in ln).split()
    assert total[2] == "6" and total[3] == "5"

    # A backend that records no per-step tool calls shows no column and no note. Its
    # OWN renderer, deliberately: both fixtures name the session "ses_1", so handing
    # this app's session to the tool-store renderer would just re-read the tool rows.
    bare = _turns_app()
    plain = bare.renderer.detail_turns(bare.current_session(), 130)
    assert "Calls" not in box_cells(plain)[0]
    assert "· Calls:" not in "\n".join(plain)


def test_turns_optional_columns_never_get_clipped_by_the_frame():
    # Sweep both optional-column fixtures at the documented prompt/model width floors.
    for store in (_TurnToolStore, _TurnAgentStore):
        app = _turns_app(store)
        wf = app.current_session()
        for w in range(77, 220):
            app.turn_drill = None
            lines = app.renderer.detail_turns(wf, w)
            assert "..." not in box_cells(lines)[0], (store.__name__, w)
            assert all(len(ln) <= w for ln in lines), (store.__name__, w)
        for w in range(75, 220):
            app.open_turn_drill(0)
            assert "..." not in box_cells(app.renderer.detail_turn_drill(wf, w))[0], w

    # ...and across idx_w, the other input to the budget: a session with 1,000 prompts
    # spends two more cells on the "#" column before any optional cell is weighed, so a
    # budget that is exact at idx_w=2 can still clip at idx_w=4.
    for count in (99, 999):
        many = _turns_app(_counting_turn_store(count))
        wide = many.current_session()
        for w in range(77, 220):
            many.turn_drill = None
            lines = many.renderer.detail_turns(wide, w)
            assert "..." not in box_cells(lines)[0], (count, w)
            assert all(len(ln) <= w for ln in lines), (count, w)


def test_turns_prompt_rows_show_which_subagents_ran_them():
    app = _turns_app(_TurnAgentStore)
    lines = app.renderer.detail_turns(app.current_session(), 140)
    assert "Agents" in box_cells(lines)[0]
    body = [ln for ln in lines if "prompt p" in ln]
    assert "↳ docs" in body[0]
    assert "-" in body[1].split("prompt p2")[1] and "↳" not in body[1]  # ran it itself
    assert "↳ subagent ×2" in body[2]  # two unnamed executions, one cell
    # The TOTAL carries the session's whole mix.
    total = next(ln for ln in lines if "TOTAL" in ln)
    assert "↳" in total

    # A session that delegated nothing shows no column at all -- gated on the ROWS, like
    # Calls, never on a backend flag (a stripe of dashes is worse than no column).
    plain = _turns_app(_TurnNavStore)
    assert "Agents" not in box_cells(plain.renderer.detail_turns(plain.current_session(), 140))[0]


def test_turns_calls_column_is_sized_from_the_data_not_the_header():
    class Busy(_TurnToolStore):
        def message_timeline(self, wid):
            rows = super().message_timeline(wid)
            for r in rows:
                r["tools"] = ["Bash"] * 40_000  # 6-digit total across six turns
            return rows

    app = _turns_app(Busy)
    lines = app.renderer.detail_turns(app.current_session(), 130)
    assert "..." not in box_cells(lines)[0]
    total = next(ln for ln in lines if "TOTAL" in ln)
    assert total.split()[3] == "240000"  # printed whole, not clipped to five cells
    assert all(len(ln) <= 130 for ln in lines)


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
    app.move(1)
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
    app = _turns_app()
    app._turn_cursor = 1
    app._toggle_turn_cursor()
    assert app.turn_drill == 1 and app.scroll == 0
    app.move(1)
    assert app._trace_cursor == 1  # the drill's OWN row cursor, one level in
    assert app._turn_cursor == 1  # the prompt cursor stayed put, ready for the step back
    assert app.scroll == 0
    app._trace_cursor = len(app.drilled_turn_indices()) - 1
    app.move(1)
    assert app.scroll == 1  # at the last turn the key goes back to the pane
    assert app.handle_key(None, 27)  # Esc
    assert app.turn_drill is None and app.view == "session"  # back to the table, not out
    assert app.handle_key(None, 27) and app.view != "session"  # a second Esc leaves


def test_turns_cursor_hands_the_key_back_at_either_end_so_the_pane_scrolls():
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
    # laptop (live) is the first BOX, one row under the synthetic fleet row: full
    # drill-in, no export stamp.
    live = "\n".join(app.renderer.machine_overview(app.machines[1], 100))
    assert "● live" in live and "Summary only" not in live
    # server (pulled): the pulled-summary niceties + the re-pull hint.
    pulled = "\n".join(app.renderer.machine_overview(app.machines[2], 100))
    assert "○ pulled" in pulled
    assert "opentab:      1.6.0" in pulled
    assert "Pulled:" in pulled
    assert "Summary only" in pulled and "F to re-pull" in pulled


def test_the_fleet_row_overview_ranks_the_boxes_instead_of_a_live_pulled_status():
    # The synthetic "all machines" row is neither live nor pulled and re-pulls every box,
    # so its Overview drops both niceties; in their place comes the cut a single box can't
    # show -- spend BY box, the headline breakdown you drill into next.
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00", cost=3.0)],
            "server": [workflow("b", "2026-05-02 10:00:00", cost=9.0)],
        }
    )
    app.set_browse_mode("machines")
    lines = app.renderer.machine_overview(app.machines[0], 100)
    fleet = "\n".join(lines)
    assert box_title(lines) == "Fleet" and "Machines:     2" in fleet
    assert "live" not in fleet and "Summary only" not in fleet
    assert "Spend by machine" in fleet and "laptop" in fleet and "server" in fleet
    assert "$12.00" in fleet  # the fleet total, not one box's
    # Its Models tab spans every box, so it isn't titled "Machine" either.
    models = app.renderer.machine_models(app.machines[0], 100)
    assert box_title(models) == "Fleet Model Spend"


def test_the_fleet_row_renders_with_a_sum_badge_and_its_own_panel_title():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00", cost=3.0)],
            "server": [workflow("b", "2026-05-02 10:00:00", cost=9.0)],
        }
    )
    app.set_browse_mode("machines")
    r = app.renderer
    assert r.machine_row_text(app.machines[0], ">", 60).startswith("> ∑ all machines")
    assert r.machine_row_text(app.machines[1], " ", 60).startswith("  ● laptop")
    # The detail pane titles itself "All machines", not "Machine all machines".
    titles: list[str] = []
    r.box = lambda s, y, x, h, w, title, active=False: titles.append(title)
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair, ot.curses.init_pair = (lambda n: 0), (lambda *a: None)
    try:
        r.draw_machine_detail(FakeScreen(20, 100), 0, 0, 20, 100)
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    assert any("All machines" in t for t in titles)


def test_machine_detail_dispatches_its_tabs():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00")],
            "server": [workflow("b", "2026-05-02 10:00:00")],
        }
    )
    app.set_browse_mode("machines")
    app.machine_index = 2  # server
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
    assert "❄ 1 cache expiry, $" in box_title(lines)
    cells = box_cells(lines)
    mark = next(i for i, c in enumerate(cells) if c.startswith("❄ "))
    # ABOVE the row it belongs to -- the wait happened before that prompt -- and flush
    # left, outside the table's own columns, because it is an event, not a prompt.
    assert "the late follow-up" in cells[mark + 1]
    assert not cells[mark + 1].startswith("❄")
    assert "2h idle" in cells[mark] and "300.0k bought again" in cells[mark]
    assert "it lived 1h" in cells[mark]  # the deadline that was missed, not just the gap
    # The first prompt is untouched: nothing expired before it.
    assert not cells[mark - 1].startswith("❄ ") if mark else True
    assert joined.count("❄ cache expired") == 1
    # Painted off its leading glyph, like every other prefix-styled line in the panes
    # (line_attr gives "❄ " its own branch -- red, where "! " caveats are amber). Inside
    # the box the glyph has to be the FIRST content cell for line_attr to reach it past
    # the gutter, so the marker keeps its colour instead of flattening into a table row.
    boxed = next(ln for ln in lines if "cache expired" in ln)
    assert boxed[2:].lstrip().startswith("❄ ")


def test_detail_turns_prices_an_effort_switch_that_took_the_cache_with_it():
    # Changing the reasoning level changes the request's thinking config, which changes
    # the prefix -- so the next turn re-buys the whole cached context. That used to land
    # in the silent "invalidated" bucket, i.e. a real cost with no marker and no cause;
    # it now gets its own ⚙ line, because unlike an edited tool set it is a decision the
    # reader made and can see the price of.
    app = _cache_miss_app()
    rows = app._turns_by_session["s1"]
    rows[0]["effort"], rows[1]["effort"] = "high", "low"
    rows[1]["time"] = "2026-06-10 10:00:30"  # inside the TTL: the switch is the cause
    lines = ot.Renderer(app).detail_turns(app.loaded[0], 96)
    assert "⚙ 1 effort switch, $" in box_title(lines)
    cells = box_cells(lines)
    mark = next(i for i, c in enumerate(cells) if c.startswith("⚙ "))
    assert "high → low" in cells[mark] and "300.0k bought again" in cells[mark]
    assert "the late follow-up" in cells[mark + 1]  # above the prompt it happened before
    assert not any(c.startswith("❄ ") for c in cells)  # not an expiry: nothing sat idle
    # Painted off its leading glyph like ▼/❄, and reachable past the box's gutter.
    boxed = next(ln for ln in lines if "reasoning effort" in ln)
    assert boxed[2:].lstrip().startswith("⚙ ")
    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n
    try:
        # The ❄ red, not the ▼ amber: both lines report the same thing -- money spent
        # buying a context you had already bought.
        assert ot.Renderer(app).line_attr(boxed) == (4 | ot.curses.A_BOLD)
    finally:
        ot.curses.color_pair = orig
    # ...and a footnote says WHY a level change costs anything (the notes wrap, so the
    # sentence is read off the joined text).
    assert "invalidated the cached prefix" in " ".join(ln.strip() for ln in lines)

    # Same level on both sides: no marker at all, and the catch-all stays silent.
    rows[1]["effort"] = "high"
    quiet = ot.Renderer(app).detail_turns(app.loaded[0], 96)
    assert "⚙" not in "\n".join(quiet)


def test_detail_turn_drill_shows_the_reasoning_level_each_call_ran_at():
    # Beside the model that ran it -- the two halves of "what answered this". Gated on
    # the ROWS: only four backends record a level, and the rest must show no column
    # rather than a stripe of dashes.
    app = _turns_app(_TurnNavStore)
    wf = app.current_session()
    app.open_turn_drill(0)
    assert "Eff" not in box_cells(ot.Renderer(app).detail_turn_drill(wf, 130))[0]

    rows = app.session_turn_rows(wf.id)
    for r in rows:
        r["effort"] = "xhigh"
    drilled = ot.Renderer(app).detail_turn_drill(wf, 130)
    assert "Eff" in box_cells(drilled)[0]
    assert all("xhigh" in ln for ln in drilled if "sonnet" in ln)
    # ...and the column never pushes the table through its own frame.
    for w in range(77, 220):
        assert "..." not in box_cells(ot.Renderer(app).detail_turn_drill(wf, w))[0], w


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
    assert any(ln.strip().startswith("cost│") for ln in lines)
    assert not any(ln.strip().startswith("context│") for ln in lines)


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
    assert "Cumulative" in box_cells(wide)[0] and any("█" in ln or "▏" in ln for ln in wide)

    mid = app.renderer.detail_turns(wf, 88)  # bar dropped, Cumulative kept
    assert all(len(ln) <= 88 for ln in mid)
    assert "Cumulative" in box_cells(mid)[0]
    assert not any("█" in ln or "▏" in ln for ln in box_cells(mid))

    # 76, not 72: the ruled box takes four cells off the table's own budget, and below
    # ~76 the PROMPT_MIN floor starts overflowing the frame rather than yielding.
    narrow = app.renderer.detail_turns(wf, 76)  # both dropped
    assert all(len(ln) <= 76 for ln in narrow)
    assert "Cumulative" not in box_cells(narrow)[0]
    # ...and the columns that carry the answer never go.
    for lines_ in (wide, mid, narrow):
        line = box_cells(lines_)[0]
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


def test_trace_header_keeps_the_prompt_visible_while_the_content_scrolls():
    app = _trace_app()
    wf = app.current_session()
    for width in (72, 96, 140):
        app.open_trace_drill()
        lines = app.renderer.detail_turn_drill(wf, width)
        assert lines[0].startswith("Turn 1 of 2 · do the thing")
        assert len(lines[0]) <= width
        app.close_trace_drill()


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
        assert any("❄ idle time expired" in ln for ln in lines), width
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


class _TraceStore(FakeStore):
    """Two prompts, three turns, with content behind two of them."""

    records_reasoning = False  # a Claude-shaped harness: empty thinking blocks

    _TEMPLATE = {
        "k0": [
            {"kind": "text", "text": "I'll check the diff first."},
            {
                "kind": "tool",
                "name": "Bash",
                "args": "git diff --stat src/opentab/tui/renderer.py",
                "params": [("description", "the diff")],
                "output": "3 files changed\n\n\n42 insertions(+)\n" + "noise\n" * 40,
                "output_dropped": 900,
            },
        ],
        "k1": [
            {
                "kind": "reasoning",
                "text": "**Planning** the next step.",
                "dropped": 120,
            }
        ],
    }

    def __init__(self, workflows):
        super().__init__(workflows)
        # Per instance: a test that rewrites an event must not reach the next one.
        self._CONTENT = {k: [dict(e) for e in v] for k, v in self._TEMPLATE.items()}

    def supports_turns(self, wid):
        return True

    def supports_turn_content(self, wid):
        return True

    def turn_content(self, wid, content_key=None):
        return {
            k: [dict(e) for e in v]
            for k, v in self._CONTENT.items()
            if content_key is None or k == content_key
        }

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
                "prompt_id": "p0" if i < 2 else "p1",
                "prompt_title": "do the thing",
                "prompt_full": "do the thing",
                "time": f"2026-06-01 12:0{i}:00",
                "content_key": f"k{i}" if i < 2 else "",
                "has_text": i == 0,
                "has_reasoning": i == 1,
            }
            for i in range(3)
        ]


def _trace_app():
    app = _turns_app(_TraceStore)
    app.open_turn_drill(0)  # the first prompt, whose two turns both have content
    return app


def test_turn_trace_shows_the_exact_command_its_arguments_and_its_output():
    # The whole point of the level: a Cost column cannot say WHAT was bought, and the
    # command a call ran with is the one thing no token count carries.
    app = _trace_app()
    wf = app.current_session()
    assert app.open_trace_drill() is True
    body = app.renderer.detail_turn_drill(wf, 96)
    text = "\n".join(body)

    assert body[0] == "Turn 1 of 2 · do the thing"
    assert "claude-opus-4-8" in text and "$1.00" in text
    assert "  I'll check the diff first." in body  # narration, the assistant's own voice
    assert "▸ Bash\n│  git diff --stat src/opentab/tui/renderer.py" in text
    assert "│  description: the diff" in body  # the rest of the arguments, beneath it
    assert "│  3 files changed" in body
    assert "Output · preview · Enter expand" in text
    # Both caps are reported rather than silently applied: what is on screen is a head.
    assert "… 37 more lines, 900 more characters" in text


def test_turn_trace_says_when_the_harness_records_no_reasoning_text():
    # Claude Code writes its thinking blocks EMPTY. Showing nothing would read as a
    # parsing bug on the one thing people most expect a trace to hold.
    app = _trace_app()
    app.open_trace_drill()
    text = "\n".join(app.renderer.detail_turn_drill(app.current_session(), 96))
    assert "records no reasoning text" in text

    app.store.records_reasoning = True
    app._trace_cursor = 1  # the turn that does carry reasoning prose
    app.open_trace_drill()
    lines = app.renderer.detail_turn_drill(app.current_session(), 96)
    assert "✻ Thinking" in lines and "  Planning the next step." in lines
    assert "  … 120 more characters" in lines
    assert "records no reasoning text" not in "\n".join(lines)


def test_a_turn_with_no_recorded_content_says_so_rather_than_rendering_empty():
    app = _turns_app(_TraceStore)
    app.open_turn_drill(1)  # the second prompt's turn carries no key
    app.open_trace_drill()
    lines = app.renderer.detail_turn_drill(app.current_session(), 96)
    assert "  No content recorded for this turn." in lines


def test_esc_leaves_the_trace_before_the_prompt_that_holds_it():
    # Innermost first: three levels out, one Esc each, and none of them skipped.
    app = _trace_app()
    app.open_trace_drill()
    assert app.handle_key(None, 27) and app.active_trace_drill is None
    assert app.turn_drill == 0  # the prompt is still open
    assert app.handle_key(None, 27) and app.turn_drill is None
    assert app.view == "session"
    assert app.handle_key(None, 27) and app.view != "session"


def test_enter_inside_a_drilled_prompt_opens_the_selected_turn():
    app = _trace_app()
    app.move(1)  # the drill has its OWN row cursor, one level in
    assert (app._trace_cursor, app._turn_cursor) == (1, 0)
    assert app.handle_key(None, 10)  # Enter
    assert app.active_trace_drill == 1
    # A second Enter is swallowed rather than re-opening what is already open.
    assert app.handle_key(None, 10) and app.active_trace_drill == 1


def test_a_click_inside_a_drilled_prompt_opens_that_turns_trace():
    app = _trace_app()
    rnd = app.renderer
    lines = rnd.detail_turn_drill(app.current_session(), 96)  # a paint records the rows
    # Pin the cursor to the ROW, not merely to "some line": the map is rebased from the
    # box's body start, and this pane prints the prompt above the frame -- so an
    # unadded prologue lights a blank line above the table and every click is off by it.
    app._trace_cursor = 1
    lines = rnd.detail_turn_drill(app.current_session(), 96)
    assert lines[rnd._turn_cursor_line].startswith("│    2 ")
    line = next(k for k, v in rnd._turn_header_at.items() if v == 1)
    assert line == rnd._turn_cursor_line
    app._trace_cursor = 0
    app._apply_click(("turnline", line), drill=False)
    assert app.active_trace_drill == 1 and app._trace_cursor == 1
    # While a trace is open the map is empty, so a click on its prose lands nowhere.
    rnd.detail_turn_drill(app.current_session(), 96)
    assert rnd._turn_header_at == {} and rnd._turn_cursor_line is None


def test_demo_replaces_the_trace_without_reading_or_derived_anonymization():
    # Demo content is authored fixture data, not transformed backend data. If this
    # method is touched, even briefly, the test fails before anything can leak.
    app = _trace_app()
    app.store.demo = True
    app.store.demo_scale = 1.0
    app.store.demo_cats = ot.demo.DEMO_ALL
    app.store.turn_content = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("real read"))
    app._turns_by_session.clear()  # actual demo toggling reloads and clears this cache
    assert app.session_supports_trace(app.current_session().id) is True
    content = app.session_trace(app.current_session().id)
    assert list(content) == ["demo:0", "demo:1", "demo:2"]
    assert app.open_trace_drill() is True
    text = "\n".join(app.renderer.detail_turn_drill(app.current_session(), 96))
    assert "git diff --stat" not in text and "src/opentab" not in text
    assert "found the refresh path" in text and "src/cache.py" in text


def test_the_drill_marks_which_turns_have_something_to_read():
    # 60.2% of real Claude turns are pure tool calls. Without a marker, finding the ones
    # that narrate or reason means opening them one at a time -- which is the whole
    # reason the column exists. The flags ride on the turn ROW, computed by the parse
    # that already happened, so nothing pays a content fetch to draw them.
    app = _trace_app()
    lines = app.renderer.detail_turn_drill(app.current_session(), 96)
    header = next(ln for ln in lines if "Cached" in ln)
    assert "Content" in header
    body = [ln for ln in lines if "claude-opus" in ln]
    assert [ln.split()[-5] for ln in body] == ["Text", "Thinking"]

    # A backend that records neither shows NO column, not a stripe of dashes.
    for row in app.session_turn_rows(app.current_session().id):
        row["has_text"] = row["has_reasoning"] = False
    header = next(
        ln for ln in app.renderer.detail_turn_drill(app.current_session(), 96) if "Cached" in ln
    )
    assert "Read" not in header and "Content" not in header


def test_the_read_column_is_gated_on_the_trace_being_openable():
    # A partial demo that leaves real prompts visible must not expose raw trace content.
    app = _trace_app()
    app.store.demo = True
    app.store.demo_scale = 1.0
    app.store.demo_cats = frozenset({"titles", "spend"})
    header = next(
        ln for ln in app.renderer.detail_turn_drill(app.current_session(), 96) if "Cached" in ln
    )
    assert "Read" not in header


def test_moving_the_drill_cursor_scrolls_it_into_view():
    # Without the follow flag a prompt with more turns than fit leaves the viewport at
    # the top while the selection walks off the bottom: j moves a row nobody can see,
    # and Enter then opens a turn the reader never selected.
    app = _trace_app()
    app._turn_follow = False
    assert app._move_trace_cursor(1) is True
    assert app._turn_follow is True
    app._turn_follow = False
    assert app._move_trace_cursor(-5) is True  # clamped, but it still moved
    assert app._turn_follow is True
    app._turn_follow = False
    assert app._move_trace_cursor(-1) is False  # already at the top: the pane takes it
    assert app._turn_follow is False


def test_the_trace_memo_does_not_accumulate_every_session_browsed():
    # Unlike the other extras -- rows of numbers -- a trace is a session's content,
    # ~1 MB for a long one. Browsing a project's worth would hold the corpus in memory.
    app = _trace_app()
    for n in range(app.TRACE_MEMO_SESSIONS + 3):
        app.session_trace(f"other-{n}")
    assert len(app._trace_by_session) <= app.TRACE_MEMO_SESSIONS


def test_a_multiline_command_keeps_its_shape_instead_of_collapsing():
    # 48% of real tool calls carry a multi-line argument and 25% a multi-line command.
    # Flattened, a heredoc becomes one unreadable line and a patch loses the indentation
    # that carries its meaning -- on the view whose whole promise is the exact command.
    app = _trace_app()
    app.store._CONTENT["k0"] = [
        {
            "kind": "tool",
            "name": "Bash",
            "args": "python3 - <<'PY'\nif x:\n    print('a  b')\nPY",
            "params": [("patch", "line one\n    line two")],
            "output": "",
            "output_dropped": 0,
        }
    ]
    app.open_trace_drill()
    lines = app.renderer.detail_turn_drill(app.current_session(), 96)
    assert "▸ Bash" in lines  # a multi-line command does NOT ride the marker row
    assert "│  python3 - <<'PY'" in lines
    assert "│      print('a  b')" in lines  # its own indentation, and its double space
    assert "│  patch:" in lines
    assert "│        line two" in lines


def test_trace_lines_never_exceed_the_pane_in_terminal_cells():
    # textwrap counts code points, so wide glyphs came back at twice the width asked for
    # and the painter clipped them: characters vanished instead of flowing on.
    from opentab.formatting import display_width

    app = _trace_app()
    app.store._CONTENT["k0"] = [
        {"kind": "text", "text": "界" * 60},
        {
            "kind": "tool",
            "name": "Bash",
            "args": "echo " + "界" * 40,
            "params": [("note", "界" * 40)],
            "output": "界" * 80,
            "output_dropped": 0,
        },
    ]
    app.open_trace_drill()
    for width in (40, 72, 96):
        lines = app.renderer.detail_turn_drill(app.current_session(), width)
        # The "# " heading is clipped by convention, like every other pane's; the
        # content and the footnotes must fit, because clipping loses characters.
        assert all(display_width(ln) <= width for ln in lines if not ln.startswith("# "))


def test_the_read_column_gives_way_before_the_money_does():
    # Every optional cell is budgeted against the model column's floor. Read was added
    # unconditionally, and this table's overflow lands on the RIGHT -- so at 80 columns
    # a marker cost the reader the Cost column, on an ordinary row carrying both an
    # effort and narration. (Below 80 the table clips with or without this column; that
    # is the pre-existing geometry of its eight fixed-width cells, not the marker.)
    from opentab.formatting import display_width

    app = _trace_app()
    for row in app.session_turn_rows(app.current_session().id):
        row["effort"] = "xhigh"
        row["has_text"] = True

    def table(width):
        lines = app.renderer.detail_turn_drill(app.current_session(), width)
        return [ln for ln in lines if ln.startswith(("┌", "│", "├", "└"))]

    at80 = table(80)
    assert all(display_width(ln) <= 80 for ln in at80)
    assert not any(ln.rstrip().endswith("...") for ln in at80)
    assert all("$1.00" in ln for ln in at80 if "claude-opus" in ln)
    # Narrower, the marker is what gives way -- never a column carrying a number.
    for width in (60, 74):
        rows = table(width)
        assert all(display_width(ln) <= width for ln in rows)
        assert "Read" not in rows[1]


def test_return_from_a_late_turn_restores_the_list_and_selected_row():
    app = _trace_app()
    wf = app.current_session()
    base = app.session_turn_rows(wf.id)[0]
    app._turns_by_session[wf.id] = [dict(base) for _ in range(40)]
    app._trace_cursor = 30
    rnd = app.renderer
    rnd.detail_turn_drill(wf, 96)
    rnd._scroll_turn_cursor_into_view(20)
    previous = app.scroll
    app._turn_follow = False
    app.open_trace_drill()
    rnd.detail_turn_drill(wf, 96)
    app.handle_key(None, 27)
    rnd.detail_turn_drill(wf, 96)
    assert app.scroll == previous and app._turn_follow
    rnd._scroll_turn_cursor_into_view(20)
    assert app.scroll <= rnd._turn_cursor_line < app.scroll + 20


def test_trace_keys_step_siblings_expand_on_demand_and_release_full_content():
    app = _trace_app()
    app.store._CONTENT["k0"] = [
        {
            "kind": "tool",
            "name": "shell",
            "args": "cat log",
            "output": "\n".join(f"line {i}" for i in range(60)),
        }
    ]
    app.open_trace_drill()
    wf = app.current_session()
    preview = app.renderer.detail_turn_drill(wf, 96)
    assert "line 59" not in "\n".join(preview)
    app.handle_key(None, ord("z"))
    assert app._trace_full is None  # first paint can show loading before I/O
    assert "Loading full turn" in "\n".join(app.renderer.detail_turn_drill(wf, 96))
    app.load_trace_expansion()
    assert "line 59" in "\n".join(app.renderer.detail_turn_drill(wf, 96))
    app.handle_key(None, ord("]"))
    assert app.active_trace_drill == 1 and app._trace_cursor == 1
    assert app._trace_full is None and not app.trace_expanded
    app.handle_key(None, ord("]"))  # stop at this prompt's boundary
    assert app.active_trace_drill == 1
    app.handle_key(None, ord("["))
    assert app.active_trace_drill == 0
    app.handle_key(None, ord("z"))
    app.load_trace_expansion()
    app.handle_key(None, ord("z"))
    assert app._trace_full is None and not app.trace_expanded
    assert "line 59" not in "\n".join(app.renderer.detail_turn_drill(wf, 96))


def test_trace_prose_wraps_words_but_fenced_code_keeps_spaces():
    app = _trace_app()
    prose = "alpha beta gamma delta " * 8
    lines = app.renderer._trace_prose({"text": prose}, 36)
    assert " ".join(ln.strip() for ln in lines) == prose.strip()
    code = "```python\n    print('a  b')\n```"
    lines = app.renderer._trace_prose({"text": code}, 36)
    assert "      print('a  b')" in lines


def test_trace_styles_cover_whole_blocks_and_stay_visible_while_scrolling():
    app = _trace_app()
    app.store._CONTENT["k0"] = [
        {"kind": "reasoning", "text": "First thought\nSecond thought"},
        {"kind": "text", "text": "Now run it."},
        {
            "kind": "tool",
            "name": "shell",
            "args": "echo $1",
            "status": "error",
            "output": "Permission denied\nSecond output line",
        },
    ]
    app.open_trace_drill()
    rnd = app.renderer
    lines = rnd.detail_turn_drill(app.current_session(), 96)
    original = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8
    try:
        for text in (
            "  First thought",
            "  Second thought",
            "│  Permission denied",
            "│  Second output line",
        ):
            assert rnd.line_attr(next(ln for ln in lines if ln == text)) == 1 << 8
        assert rnd.line_attr(next(ln for ln in lines if "shell · Error" in ln)) == (
            (4 << 8) | ot.curses.A_BOLD
        )
        assert (
            rnd.line_attr(next(ln for ln in lines if ln == "  Now run it.")) == ot.curses.A_NORMAL
        )
        app._nodes_by_session[app.current_session().id] = []
        app.scroll = 4
        screen = AttrScreen(12, 100)
        rnd.draw_detail(screen, 0, 0, 12, 100)
        assert "Turn 1 of 2" in screen_text(screen)
        # The shell parameter must keep the command's style, not become a green dollar amount.
        app.scroll = 0
        screen = AttrScreen(30, 100)
        rnd.draw_detail(screen, 0, 0, 30, 100)
        dollar = next(
            (y, x)
            for (y, x), ch in screen.cells.items()
            if ch == "$" and screen.cells.get((y, x + 2)) != "."
        )
        assert screen.attrs[dollar] == ot.curses.A_NORMAL
    finally:
        ot.curses.color_pair = original


def test_trace_footer_and_help_describe_the_current_level_and_bindings():
    app = _trace_app()
    app.can_switch_source = lambda: False
    assert ot.keymap.BY_ID["enter"].text(app) == "open the selected turn"
    app.open_trace_drill()
    assert not ot.keymap.BY_ID["enter"].shown(app)
    assert ot.keymap.BY_ID["move"].text(app).startswith("scroll this turn")
    footer = " ".join(
        text
        for segments in ot.keymap.footer_parts(app)
        for text, _active in (segments if isinstance(segments, list) else [segments])
    )
    assert "[/] turn" in footer and "z expand" in footer


def test_first_trace_read_paints_loading_before_fetching_and_respects_demo():
    app = _trace_app()
    app.open_trace_drill()
    app._nodes_by_session[app.current_session().id] = []
    fetched = []
    fetch = app.store.turn_content
    app.store.turn_content = lambda wid, **kw: (fetched.append((wid, kw)) or fetch(wid, **kw))
    original = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8
    try:
        screen = AttrScreen(24, 100)
        app.renderer.draw_detail(screen, 0, 0, 24, 100)
        assert "Loading turn" in screen_text(screen) and not fetched
        app.load_trace_expansion()
        assert len(fetched) == 1
        app.handle_key(None, ord("z"))
        # A demo toggle between request and read swaps in a fixture without fetching.
        app.store.demo = True
        app.store.demo_cats = ot.demo.DEMO_ALL
        app.load_trace_expansion()
        assert len(fetched) == 1 and app._trace_full is not None
        events = app.turn_trace_events(app.current_session().id, {"content_key": "k0"})
        assert "src/cache.py" in str(events) and "git diff --stat" not in str(events)
    finally:
        ot.curses.color_pair = original


def test_trace_outputs_expand_independently_with_keyboard_and_mouse():
    app = _trace_app()
    app.store._CONTENT["k0"] = [
        {"kind": "tool", "name": "grep", "output": "\n".join(f"first {i}" for i in range(30))},
        {"kind": "tool", "name": "grep", "output": "\n".join(f"second {i}" for i in range(30))},
    ]
    app.open_trace_drill()
    wf = app.current_session()

    def render():
        return "\n".join(app.renderer.detail_turn_drill(wf, 80))

    assert "first 29" not in render()
    app.handle_key(None, 10)
    assert app._trace_open_outputs == {0} and not app.trace_expanded
    assert "Loading output" in render()
    app.load_trace_expansion()
    text = render()
    assert "first 29" in text and "second 29" not in text
    app._apply_click(("trace-output", 1), drill=False)
    assert app._trace_loading is None  # the one-turn read is reused
    assert "second 29" in render()
    app.scroll = max(line for line, index in app.renderer._trace_tool_at.items() if index == 0)
    app.handle_key(None, 10)
    assert app._trace_open_outputs == {1}
    text = render()
    assert "first 29" not in text and "second 29" in text
    assert "│  Output · full" in text
    app.handle_key(None, ord("]"))
    assert not app._trace_open_outputs and app._trace_full is None


def test_trace_output_preview_budgets_screen_rows_and_full_output_is_faithful():
    from opentab.formatting import display_width

    app = _trace_app()
    output = "  " + "界  $1 **raw** " * 60 + "\n\n    \nlast"
    event = {"output": output, "output_dropped": 90000}
    preview = app.renderer._trace_output(event, 40)
    assert all(display_width(ln) <= 40 for ln in preview)
    assert len(preview) < 10 and "90,000 more characters" in " ".join(
        ln.removeprefix("│").strip() for ln in preview
    )
    full = app.renderer._trace_output({"output": " a  b\n\n    \n$1 **raw**"}, 80, True)
    assert full == ["│   a  b", "│", "│      ", "│  $1 **raw**"]


def test_trace_markdown_headings_are_readable_but_code_and_output_are_raw():
    app = _trace_app()
    event = {
        "kind": "reasoning",
        "text": "**Inspecting the renderer**\n## Next step\n```python\n    print('**raw**')\n```\nUse **tests** to verify.",
    }
    lines = app.renderer._trace_prose(event, 80)
    assert lines[0] == "  Inspecting the renderer" and lines[0].role == "heading"
    assert lines[1] == "  Next step" and lines[1].role == "heading"
    assert "      print('**raw**')" in lines and "  Use tests to verify." in lines
    assert "│  ## Raw **output**" in app.renderer._trace_output({"output": "## Raw **output**"}, 80)
    # A shorter fence inside a longer one is code, not the end of the block.
    lines = app.renderer._trace_prose({"text": "````\n```\n**literal**\n````"}, 80)
    assert "  ```" in lines and "  **literal**" in lines


def test_trace_output_click_regions_and_reader_chrome_are_contextual():
    app = _trace_app()
    app.open_trace_drill()
    app._nodes_by_session[app.current_session().id] = []
    app.session_trace(app.current_session().id)
    rnd = app.renderer
    original = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8
    try:
        screen = AttrScreen(40, 120)
        rnd.draw_detail(screen, 0, 0, 40, 120)
        output = next(
            (y, x)
            for (y, x), ch in screen.cells.items()
            if ch == "O" and screen.cells.get((y, x + 1)) == "u"
        )
        assert rnd.hit(*output) == ("trace-output", 1)
        app._apply_click(rnd.hit(*output), drill=False)
        assert app._trace_open_outputs == {1}
        footer = str(ot.keymap.footer_parts(app))
        assert "output" in footer and "ignore" not in footer and "note" not in footer
    finally:
        ot.curses.color_pair = original


def test_trace_loading_keeps_the_output_anchor_and_failures_restore_preview():
    app = _trace_app()
    app.store._CONTENT["k0"] = [
        {"kind": "text", "text": "Introductory paragraph.\n" * 15},
        {"kind": "tool", "name": "Bash", "output": "result\n" * 60},
    ]
    app.open_trace_drill()
    app._nodes_by_session[app.current_session().id] = []
    rnd = app.renderer
    rnd.detail_turn_drill(app.current_session(), 80)
    app.toggle_trace_output(1)
    anchor = app.scroll
    assert anchor > 10
    original = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8
    try:
        screen = AttrScreen(24, 84)
        rnd.draw_detail(screen, 0, 0, 24, 84)
        assert "Loading output" in screen_text(screen)
        assert app.scroll == anchor
        app.load_trace_expansion()
        rnd.draw_detail(screen, 0, 0, 24, 84)
        assert app.scroll == anchor
    finally:
        ot.curses.color_pair = original
    app._clear_trace_expansion()
    rnd.detail_turn_drill(app.current_session(), 80)
    app.toggle_trace_output(1)

    def unreadable(*args, **kwargs):
        raise OSError("recording was removed")

    app.store.turn_content = unreadable
    app.load_trace_expansion()
    assert not app._trace_open_outputs and app._trace_full is None
    assert "Output · preview" in "\n".join(rnd.detail_turn_drill(app.current_session(), 80))
