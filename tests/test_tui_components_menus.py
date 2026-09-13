from opentab.formatting import display_width
from opentab.tui.components.menus import (
    SELECTED,
    check_menu,
    option_budget,
    radio_menu,
    select_menu,
    selection_window,
    windowed_radio_menu,
)
from opentab.tui.components.modal import StyledLine, modal_layout


def test_modal_layout_sizes_places_and_clips_rows_without_curses():
    lines = [StyledLine("界 wide", "a"), StyledLine("x" * 100, "b")]
    layout = modal_layout(12, 30, "Title", lines)

    assert (layout.y, layout.x, layout.height, layout.width) == (3, 2, 6, 26)
    assert layout.field_width == 22
    assert [(row.y, row.x, row.style) for row in layout.rows] == [(5, 4, "a"), (6, 4, "b")]
    assert all(display_width(row.text) == layout.field_width for row in layout.rows)


def test_centered_modal_rows_and_title_use_terminal_cell_widths():
    layout = modal_layout(20, 40, "危険", [StyledLine("界", "danger")], center_rows=True)

    row = layout.rows[0]
    assert row.x == layout.x + 2 + (layout.field_width - 2) // 2
    assert display_width(layout.title) == 6
    assert layout.title_x == layout.x + max(2, (layout.width - 6) // 2)


def test_radio_and_check_menus_share_selection_and_marker_layout():
    radio = radio_menu("Choose:", [("All", True), ("Claude", False)], 3)
    checks = check_menu("Scramble:", [("Titles", True), ("Turns", False)], 0)

    assert [line.text for line in radio.lines] == [
        "Choose:",
        "",
        " ●  All  (current)",
        " ○  Claude",
    ]
    assert radio.lines[3].style == SELECTED
    assert checks.lines[2].text == " [x]  Titles" and checks.lines[2].style == SELECTED
    assert checks.lines[3].text == " [ ]  Turns"


def test_select_menu_returns_local_option_rows_for_renderer_hit_geometry():
    menu = select_menu(
        [StyledLine("Session", "muted"), StyledLine("", "normal")],
        [("w", "new window"), ("y", "copy command")],
        1,
        footer=[StyledLine("Esc  cancel", "normal")],
    )

    assert menu.option_rows == ((2, 0), (3, 1))
    assert menu.lines[2].text == " w  new window"
    assert menu.lines[3].text == " y  copy command" and menu.lines[3].style == SELECTED
    assert menu.lines[4].text == "Esc  cancel"


def test_selection_window_centers_and_keeps_edge_selections_visible():
    assert selection_window(20, 0, 5) == (0, 5)
    assert selection_window(20, 10, 5) == (8, 13)
    assert selection_window(20, 19, 5) == (15, 20)
    assert selection_window(3, 8, 5) == (0, 3)


def test_minimum_height_whatif_budget_keeps_selected_row_in_modal_paint_budget():
    # Renderer passes draw_whatif_menu scr_h=18 at the 80x20 terminal minimum.
    visible = option_budget(18, 15)
    entries = [(f"model-{i}", i == 19) for i in range(20)]
    menu = windowed_radio_menu(
        [StyledLine("intro", "muted"), StyledLine("tabs", "normal")],
        entries,
        19,
        visible,
        footer=[StyledLine("hint", "muted")],
    )

    assert visible == 3 and menu.start == 17
    selected_line = next(i for i, line in enumerate(menu.lines) if line.style == SELECTED)
    assert "model-19" in menu.lines[selected_line].text
    assert selected_line < 10  # draw_modal's content-row budget at scr_h=18


def test_windowed_whatif_formats_only_visible_rows_and_omits_current_suffix():
    formatted = []

    def format_model(model):
        formatted.append(model)
        return f"model-{model}"

    menu = windowed_radio_menu(
        [],
        [(model, model == 5) for model in range(10)],
        selection=5,
        visible_count=3,
        label_formatter=format_model,
        current_suffix="",
    )

    assert formatted == [4, 5, 6]
    current = next(line.text for line in menu.lines if "model-5" in line.text)
    assert current == " ●  model-5" and "(current)" not in current
