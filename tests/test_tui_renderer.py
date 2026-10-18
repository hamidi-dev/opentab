"""Whole-frame and pager contracts across the aggregate browse surfaces."""

from unittest.mock import patch

import opentab as ot

from tests._support import FakeScreen, _model_row, fleet_app, screen_text, workflow


class _FrameScreen(FakeScreen):
    def erase(self):
        self.cells.clear()

    def refresh(self):
        pass


def _frame_app():
    rows = [
        workflow("alpha", "2026-09-12 12:00:00", title="alpha session", cost=7, directory="/alpha"),
        workflow("beta", "2026-09-11 12:00:00", title="beta session", cost=3, directory="/beta"),
        workflow("old", "2025-08-10 12:00:00", title="old session", cost=2, directory="/alpha"),
    ]
    rows[1].source = "Claude Code"
    app = fleet_app({"laptop": rows[:2], "server": rows[2:]})
    app.args = ot.parse_args(["--no-state"])
    app._wheel_down = getattr(ot.curses, "BUTTON5_PRESSED", 0) or (1 << 21)
    app.show_api_prices = False
    app._model_by_root = {
        row.id: [
            {
                **_model_row("anthropic/claude-sonnet-4-5", row.total_cost, row.total_tokens),
                "root_id": row.id,
                "input": row.total_tokens,
            }
        ]
        for row in rows
    }
    return app


def _scope(app, name):
    app.set_browse_mode(name if name in ("projects", "machines", "harnesses") else "time")
    if name == "machines" and app.loaded:
        app.machine_index = next(
            i for i, machine in enumerate(app.machines) if machine.name == "laptop"
        )
    if name in ("years", "months", "days"):
        app.focus = name
        if name == "years" and app.loaded:
            app.year_index = next(i for i, year in enumerate(app.years) if year.year == "2026")


def _mouse(app, state, y=2, x=2):
    # Public mouse events include the frame offset; hit regions never do.
    event = (0, x + app.renderer.ox, y + app.renderer.oy, 0, state)
    with patch("curses.getmouse", return_value=event):
        assert app.handle_mouse()


def test_mouse_modal_wheels_and_cancels_do_not_navigate_the_view_behind_them():
    for attr, cursor in (
        ("demo_menu", "demo_menu_index"),
        ("sort_menu", "sort_menu_index"),
        ("source_menu", "source_menu_index"),
        ("harness_menu", "harness_menu_index"),
        ("machine_menu", "machine_menu_index"),
    ):
        app = _frame_app()
        setattr(app, attr, True)
        setattr(app, cursor, 0)
        app.focus, app.view = "years", "zoom"
        app.tab = app.current_tabs().index("Sessions")
        with patch("opentab.sources.source_cycle", return_value=["all", "opencode", "claude"]):
            _mouse(app, app._wheel_down)
            assert getattr(app, cursor) == 1, (attr, getattr(app, cursor))
            _mouse(app, ot.curses.BUTTON4_PRESSED)
            assert getattr(app, cursor) == 0
            _mouse(app, ot.curses.BUTTON1_DOUBLE_CLICKED)
        assert not getattr(app, attr)
        assert app.view == "zoom" and app.workflow_index == 0
    for attr, cursor in (
        ("help", "help_scroll"),
        ("toast_history", "toast_history_scroll"),
        ("whats_new", "whats_new_scroll"),
    ):
        app = _frame_app()
        setattr(app, attr, True)
        _mouse(app, app._wheel_down)
        assert getattr(app, cursor) == 3
        _mouse(app, ot.curses.BUTTON4_PRESSED)
        _mouse(app, ot.curses.BUTTON4_PRESSED)
        assert getattr(app, cursor) == 0
        if attr != "whats_new":
            _mouse(app, ot.curses.BUTTON1_CLICKED)
            assert not getattr(app, attr)
        assert app.view == "browse"


def test_mouse_price_overlay_switches_tabs_sorts_and_returns_from_session_drill():
    app = _frame_app()
    app.show_prices = True
    with patch("curses.color_pair", return_value=0):
        app.renderer.draw_prices(_FrameScreen(40, 160), 1, 38, 158)
    tab = next(
        region for region in app.renderer.regions if region[0] == "pricetab" and region[-1] == 1
    )
    _mouse(app, ot.curses.BUTTON1_CLICKED, tab[1], tab[2])
    assert app.prices_view == "family"
    _mouse(app, app._wheel_down)
    assert app.prices_index == 1
    _mouse(app, ot.curses.BUTTON4_PRESSED)
    assert app.prices_index == 0
    with patch("curses.color_pair", return_value=0):
        app.renderer.draw_prices(_FrameScreen(40, 160), 1, 38, 158)
    # Header clicks remain modal and never sort the underlying Sessions list.
    y, x0, _x1, key, target = app.renderer.sort_regions[0]
    _mouse(app, ot.curses.BUTTON1_CLICKED, y, x0)
    assert app.prices_sort == key and target == "prices" and app.show_prices
    app.prices_model = "anthropic/claude-sonnet-4-5"
    _mouse(app, app._wheel_down)
    assert app.prices_scroll == 3
    _mouse(app, ot.curses.BUTTON4_PRESSED)
    assert app.prices_scroll == 0
    _mouse(app, ot.curses.BUTTON1_CLICKED)
    assert app.prices_model is None and app.show_prices
    app.renderer.regions.clear()
    app.renderer.sort_regions.clear()
    _mouse(app, ot.curses.BUTTON1_CLICKED)
    assert not app.show_prices and app.view == "browse"


def test_mouse_trend_charts_wake_then_pick_and_ranked_rows_drill_from_real_regions():
    with patch("curses.color_pair", return_value=0):
        for tab in ("Daily", "Weekly", "Monthly", "Calendar", "Models"):
            app = _frame_app()
            app.trends = True
            app.trend_tab = app.trend_tabs.index(tab)
            app.renderer.draw_trends(_FrameScreen(42, 180), 1, 40, 178)
            if tab == "Models":
                row = next(
                    region for region in app.renderer.regions if region[:2] == ("rows", "trendrow")
                )
                _mouse(app, ot.curses.BUTTON1_DOUBLE_CLICKED, row[2], row[4])
                assert app.trend_drill is not None
                _mouse(app, app._wheel_down)
                assert app.trend_drill_scroll == 3  # Economics is the first model drill tab.
                _mouse(app, ot.curses.BUTTON4_PRESSED)
                assert app.trend_drill_scroll == 0
            elif tab == "Calendar":
                # Every real activity cell is a valid drill destination.
                date = "2026-09-12"
                cells = [
                    (y, x)
                    for y in range(1, 40)
                    for x in range(178)
                    if app._calendar_date_at(y, x) == date
                ]
                assert cells
                y, x = cells[0]
                _mouse(app, ot.curses.BUTTON1_CLICKED, y, x)
                assert app.trend_focus and app.view == "browse"
                _mouse(app, ot.curses.BUTTON1_CLICKED, y, x)
                assert app.cal_cursor == date
                _mouse(app, ot.curses.BUTTON1_DOUBLE_CLICKED, y, x)
                assert not app.trends and app.selected_day_summary.day == date
            else:
                y0, _y1, slots = app._trend_bar_geom
                active = {key for key, value in app.trend_bar_data() if value > 0}
                x0, _x1, key = next(slot for slot in slots if slot[2] in active)
                assert app._trend_bar_at(y0 - 1, x0) is None
                _mouse(app, ot.curses.BUTTON1_CLICKED, y0, x0)
                assert app.trend_focus and app.trend_cursor is None
                _mouse(app, ot.curses.BUTTON1_CLICKED, y0, x0)
                assert app.trend_cursor == key
                _mouse(app, ot.curses.BUTTON1_DOUBLE_CLICKED, y0, x0)
                assert app.view == "zoom" and not app.trends


def test_aggregate_pager_matches_the_active_tab_for_every_scope():
    # max_scroll must measure the same body the visible panel paints, not a
    # default Sessions table (especially when optional tabs change ordinals).
    builders = {
        "Overview": "overview",
        "Models": "models",
        "Projects": "projects",
        "Harnesses": "sources",
        "Machines": "machines",
        "Sessions": "workflows",
    }
    for scope in ("years", "months", "days", "projects", "harnesses", "machines"):
        app = _frame_app()
        _scope(app, scope)
        app.view = "zoom"
        prefix = {
            "years": "year",
            "months": "month",
            "days": "day",
            "projects": "project",
            "harnesses": "harness",
            "machines": "machine",
        }[scope]
        selected = getattr(app, f"selected_{prefix}_summary")
        assert selected is not None
        for index, tab in enumerate(app.current_tabs()):
            app.tab = index
            suffix = builders[tab]
            expected = getattr(app.renderer, f"{prefix}_{suffix}")(selected, 126)
            actual = app.renderer.current_pager_lines(130)
            assert actual == expected, (scope, tab)
            assert actual, (scope, tab)


def test_aggregate_frames_paint_all_tabs_in_browse_split_and_maximized_zoom():
    with patch("curses.color_pair", return_value=0), patch("curses.curs_set"), patch(
        "opentab.sources.source_cycle", return_value=["all", "opencode", "claude"]
    ):
        for scope in ("years", "months", "days", "projects", "harnesses", "machines"):
            for view, maximized in (("browse", False), ("zoom", False), ("zoom", True)):
                app = _frame_app()
                _scope(app, scope)
                app.view, app.zoom_maximized = view, maximized
                for index, tab in enumerate(app.current_tabs()):
                    app.tab = index
                    screen = _FrameScreen(42, 180)
                    app.renderer.draw(screen)
                    text = screen_text(screen)
                    assert "OpenTab" in text and tab in text, (scope, view, tab)
                    assert "Terminal too small" not in text
                    # A frame may clip text, never paint outside the physical terminal.
                    assert all(0 <= y < 42 and 0 <= x < 180 for y, x in screen.cells)
                    if view == "browse":
                        assert any(
                            region[:2] == ("rows", "detail") for region in app.renderer.regions
                        )


def test_empty_aggregate_pagers_and_frames_do_not_retain_previous_rows():
    with patch("curses.color_pair", return_value=0), patch("curses.curs_set"), patch(
        "opentab.sources.source_cycle", return_value=["all", "opencode", "claude"]
    ):
        for scope in ("years", "months", "days", "projects", "harnesses", "machines"):
            app = fleet_app({})
            _scope(app, scope)
            for view in ("browse", "zoom", "session"):
                app.view = view
                lines = app.renderer.current_pager_lines(100)
                if view in ("browse", "session"):
                    assert lines == []
                assert "alpha session" not in "\n".join(lines)
                screen = _FrameScreen(24, 100)
                app.renderer.draw(screen)
                assert "OpenTab" in screen_text(screen)
                assert "alpha session" not in screen_text(screen)


def test_frame_size_boundary_and_scroll_limit_follow_the_actual_content():
    app = _frame_app()
    app.view, app.focus = "zoom", "years"
    with patch("curses.color_pair", return_value=0), patch("curses.curs_set"), patch(
        "opentab.sources.source_cycle", return_value=["all", "opencode", "claude"]
    ):
        for height, width in ((19, 100), (24, 79)):
            screen = _FrameScreen(height, width)
            app.renderer.draw(screen)
            assert "Terminal too small" in screen_text(screen)
            assert app.renderer.regions == []
        screen = _FrameScreen(20, 80)
        app.renderer.draw(screen)
        assert "Terminal too small" not in screen_text(screen)
        for tab in range(len(app.current_tabs())):
            app.tab = tab
            lines = app.renderer.current_pager_lines(80)
            assert app.renderer.max_scroll(screen) == max(
                0, len(lines) - app.renderer.pager_height(screen)
            )


def test_session_breadcrumbs_preserve_scope_and_selected_session_identity():
    for scope, marker in (
        ("years", "2026"),
        ("months", "2026-09"),
        ("days", "2026-09-12"),
        ("projects", "/alpha"),
        ("harnesses", "harnesses"),
        ("machines", "laptop"),
    ):
        app = _frame_app()
        _scope(app, scope)
        app.view = "zoom"
        crumb = app.renderer.breadcrumb()
        if scope == "harnesses":
            assert "harnesses" in crumb
        else:
            assert marker in crumb, (scope, crumb)
        app.view = "session"
        crumb = app.renderer.breadcrumb()
        assert app.current_session().title in crumb and "Overview" in crumb


def test_frame_renders_each_global_overlay_without_losing_its_body():
    cases = (
        ("help", "Help"),
        ("toast_history", "Notifications"),
        ("theme_menu", "Theme"),
        ("demo_menu", "Demo"),
        ("source_menu", "Harness"),
        ("machine_menu", "Machine"),
        ("harness_menu", "Harness"),
        ("sort_menu", "Sort"),
        ("price_prompt", "Unpriced models"),
        ("star_prompt", "Support OpenTab"),
    )
    with patch("curses.color_pair", return_value=0), patch("curses.curs_set"), patch(
        "opentab.sources.source_cycle", return_value=["all", "opencode", "claude"]
    ):
        for attr, title in cases:
            app = _frame_app()
            setattr(app, attr, True)
            screen = _FrameScreen(40, 160)
            app.renderer.draw(screen)
            text = screen_text(screen)
            assert title.casefold() in text.casefold(), (attr, text)
            assert "OpenTab" in text


def test_session_pager_uses_real_overview_and_subagents_content():
    app = _frame_app()
    app.view = "session"
    session = app.current_session()
    with patch("curses.color_pair", return_value=0):
        for tab, builder in (("Overview", "detail_overview"), ("Subagents", "detail_subagents")):
            app.tab = app.current_tabs().index(tab)
            assert app.renderer.current_pager_lines(130) == getattr(app.renderer, builder)(
                session, 126
            )


def test_price_overlay_frames_clamp_selection_render_groupings_and_clear_empty_hit_regions():
    with patch("curses.color_pair", return_value=0), patch("curses.curs_set"), patch(
        "opentab.sources.source_cycle", return_value=["all", "opencode", "claude"]
    ):
        app = _frame_app()
        app.handle_key(None, ord("P"))
        screen = _FrameScreen(32, 160)
        for view, label in app.prices_views:
            app.set_prices_view(view)
            app.prices_index = 100000
            app.renderer.draw(screen)
            text = screen_text(screen)
            assert "Model prices" in text and label in text
            assert 0 <= app.prices_index < len(app.priced_model_entries())
            assert any(region[-1] == "prices" for region in app.renderer.sort_regions)
        app.query = "no-such-model-fixture-928734"
        app.renderer.draw(screen)
        assert "no-such-model-fixture-928734" in screen_text(screen)
        assert not app.renderer.sort_regions


def test_price_session_frame_shows_used_sessions_and_explicit_empty_model():
    with patch("curses.color_pair", return_value=0), patch("curses.curs_set"), patch(
        "opentab.sources.source_cycle", return_value=["all", "opencode", "claude"]
    ):
        app = _frame_app()
        app.handle_key(None, ord("P"))
        app.prices_model = "anthropic/claude-sonnet-4-5"
        app.prices_scroll = 999
        screen = _FrameScreen(32, 160)
        app.renderer.draw(screen)
        text = screen_text(screen)
        assert "alpha session" in text and "beta session" in text and "old session" in text
        assert app.prices_scroll == 0
        app.prices_model = "unused/model"
        app.renderer.draw(screen)
        text = screen_text(screen)
        assert "unused/model" in text and "No sessions" in text
        assert "alpha session" not in text


def test_trends_whole_frames_paint_every_chart_and_ranked_drill():
    with patch("curses.color_pair", return_value=0), patch("curses.curs_set"), patch(
        "opentab.sources.source_cycle", return_value=["all", "opencode", "claude"]
    ):
        app = _frame_app()
        app.handle_key(None, ord("T"))
        screen = _FrameScreen(42, 180)
        for index, tab in enumerate(app.trend_tabs):
            app.trend_tab = index
            app.trend_focus = False
            app.renderer.draw(screen)
            assert "Trends" in screen_text(screen) and tab in screen_text(screen)
            assert all(0 <= y < 42 and 0 <= x < 180 for y, x in screen.cells)
            if tab in ("Projects", "Harnesses", "Machines", "Providers", "Models"):
                app.handle_key(None, 10)
                assert app.trend_drill is not None, tab
                app.renderer.draw(screen)
                if tab == "Models":
                    assert "Economics" in screen_text(screen)
                    app.handle_key(None, ord("l"))
                    app.renderer.draw(screen)
                assert "session" in screen_text(screen).lower()
                app.handle_key(None, 27)
                assert app.trend_drill is None
