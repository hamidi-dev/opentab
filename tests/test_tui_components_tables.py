from unittest.mock import Mock

from opentab.tui.components.boxes import (
    TABLE_GLYPHS,
    TABLE_GLYPHS_ASCII,
    box_row,
    ruled_box,
    sectioned_box,
)
from opentab.tui.components.tables import (
    GroupTableLayout,
    ProjectHeadings,
    ProjectRow,
    SessionHeadings,
    SessionRow,
    group_table_layout,
    picker_box_width,
    picker_frame,
    picker_row,
    picker_window,
    project_header_text,
    project_row_text,
    project_table_text,
    session_columns,
    session_header_text,
    session_row_text,
)

from tests._support import app_with, workflow


def _headings(**changes):
    values = {
        "date": "Started",
        "duration": "Worked",
        "cost": "Cost v",
        "tokens": "Tokens",
        "subagents": "Subagents",
        "project": "Project",
        "title": "Title",
        "cost_width": 9,
        "token_width": 8,
    }
    values.update(changes)
    return SessionHeadings(**values)


def test_ruled_box_returns_header_and_body_metadata_without_terminal_state():
    layout = ruled_box(
        "# Sessions",
        "Started  Cost  Title",
        ["2026-09-13  $1  refactor"],
        "TOTAL       $1",
        ["! note"],
        38,
        TABLE_GLYPHS,
    )

    assert layout.header_line == 1
    assert layout.body_start == 3
    assert layout.lines[layout.header_line].startswith("│ Started")
    assert layout.lines[layout.body_start].startswith("│ 2026-09-13")
    assert layout.lines[-2].startswith("└")
    assert layout.lines[-1] == "! note"


def test_empty_and_sectioned_boxes_report_their_actual_body_boundaries():
    empty = ruled_box("Empty", "Header", [], None, [], 12, TABLE_GLYPHS_ASCII)
    grouped = sectioned_box("Card", [[], ["first"], [], ["second"]], 14, [], TABLE_GLYPHS_ASCII)

    assert empty.body_start is None
    assert empty.lines == ("+ Empty ---+", "| Header   |", "+----------+")
    assert grouped.body_start == 1
    assert grouped.lines[2] == "+------------+"
    assert grouped.lines[-1].startswith("+")


def test_box_row_clips_inside_fixed_gutters_at_minimum_width():
    assert box_row("abcdef", 3, TABLE_GLYPHS_ASCII) == "| a |"


def test_session_header_and_row_are_formatted_from_explicit_cells():
    headings = _headings(source_column="Hns ", machine_column="Machine  ")
    header = session_header_text(headings, models=True, project_width=8)
    row = session_row_text(
        SessionRow(
            date="2026-09-13",
            duration="2m",
            cost="$1.20",
            tokens="12.3k",
            subagents=2,
            model_count=3,
            source_column="oc  ",
            machine_column="mac      ",
            project="long-project",
            marks="★ ",
            ignored="ignored: ",
            title="extract tables",
        ),
        ">",
        models=True,
        project_width=8,
        cost_width=headings.cost_width,
        token_width=headings.token_width,
    )

    assert header.startswith("  Started      Worked")
    assert "Models  Hns Machine" in header
    assert row.startswith("> 2026-09-13       2m     $1.20    12.3k")
    assert "     3  oc  mac      long-...  ★ ignored: extract tables" in row


def test_session_columns_drop_optional_fields_but_protect_title_space():
    headings = _headings()
    projects = ["short", "a-project-name-that-is-far-too-long"]

    assert session_columns(projects, 120, True, False, headings) == (True, 20, True)
    assert session_columns(projects, 80, True, True, headings)[0] is False
    assert session_columns(projects, 50, True, False, headings) == (False, 0, False)


def test_session_columns_do_not_resolve_projects_in_a_project_scoped_list():
    item = workflow("a", "2026-09-13 12:00:00")
    app = app_with([item])
    app.set_browse_mode("projects")
    resolve_project = Mock(side_effect=AssertionError("hidden project column was evaluated"))
    app.renderer.session_project = resolve_project

    assert app.renderer.session_columns([item], 100)[1] == 0
    assert resolve_project.call_count == 0


def test_session_rows_do_not_build_hidden_duration_project_or_header_cells():
    item = workflow("a", "2026-09-13 12:00:00")
    app = app_with([item])
    renderer = app.renderer
    renderer.session_duration = Mock(side_effect=AssertionError("hidden duration was evaluated"))
    renderer.session_project = Mock(side_effect=AssertionError("hidden project was evaluated"))
    renderer._session_headings = Mock(side_effect=AssertionError("all headings were rebuilt"))

    row = renderer.session_row_text(item, ">", models=False, proj_w=0, dur=False)

    assert row.endswith("a")
    assert renderer.session_duration.call_count == 0
    assert renderer.session_project.call_count == 0
    assert renderer._session_headings.call_count == 0


def test_project_preview_and_picker_text_share_pure_formatters():
    headings = ProjectHeadings("Project", "Cost v", "Tokens", "Ses", "Subagents")
    project = ProjectRow("/tmp/a-very-long-project", "$12", "1.5k", 2, 1, ignored=True)

    table = project_table_text([project], headings, 60, None)

    assert table.header == project_header_text(headings, 60)
    assert table.body == (project_row_text(project, " ", 60),)
    assert table.total is None
    assert table.body[0].startswith("  × ...very-long-project")
    assert table.body[0].endswith("$12   1.5k   2 ses      1 subs")


def test_picker_layout_owns_frame_row_and_centered_viewport_geometry():
    frame = picker_frame(3, 10, 100, "Sessions", "Started  Title", 4, TABLE_GLYPHS)
    row = picker_row(
        frame.body_y + 1, 10, frame.content_x, frame.inner_width, "row", True, TABLE_GLYPHS
    )

    assert picker_box_width(100) == 92
    assert (frame.frame_x, frame.content_x, frame.outer_width, frame.inner_width) == (
        12,
        14,
        96,
        92,
    )
    assert (frame.top_y, frame.header_y, frame.rule_y, frame.body_y, frame.bottom_y) == (
        3,
        4,
        5,
        6,
        10,
    )
    assert (row.y, row.left_x, row.content_x, row.right_x) == (7, 12, 14, 106)
    assert row.left == "│ " and row.right == " │" and len(row.content) == 92
    assert row.selected is True
    assert picker_window(40, 20, 24) == (20, 13, 15)
    assert picker_window(3, 99, 24) == (2, 0, 3)


def test_group_table_layout_owns_window_totals_notes_and_local_metadata():
    rows = tuple(
        (
            f"harness-{index}",
            {"cost": 0.0 if index == 1 else float(index), "tokens": index * 100, "sessions": index},
        )
        for index in range(1, 9)
    )
    layout = group_table_layout(
        rows,
        90,
        "harness",
        "Harness",
        TABLE_GLYPHS,
        cursor=6,
        selectable=True,
        height=10,
        headings={"cost": "Cost v"},
        show_api_prices=False,
        price_key="$",
    )

    assert isinstance(layout, GroupTableLayout)
    assert layout.body_start == 3
    assert layout.window_start > 0 and layout.window_count < len(rows)
    assert layout.window_start <= layout.cursor < layout.window_start + layout.window_count
    assert any("TOTAL" in line and "$35.00" in line for line in layout.lines)
    assert layout.lines[-1] == "$ prices subscription/credit usage at API list rates"


def test_group_table_top_n_totals_only_the_slice_and_components_do_not_import_views():
    import inspect

    from opentab.tui.components import tables

    rows = (
        ("one", {"cost": 5.0, "tokens": 500, "sessions": 1}),
        ("two", {"cost": 3.0, "tokens": 300, "sessions": 2}),
        ("three", {"cost": 2.0, "tokens": 200, "sessions": 3}),
    )
    layout = group_table_layout(
        rows,
        80,
        "machine",
        "Machine",
        TABLE_GLYPHS,
        limit=2,
        show_api_prices=True,
        price_key="$",
    )

    assert any("TOTAL" in line and "$8.00" in line and "3" in line for line in layout.lines)
    assert not any("three" in line for line in layout.lines)
    assert "opentab.tui.views" not in inspect.getsource(tables)
