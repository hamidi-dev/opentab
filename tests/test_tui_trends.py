import os

import opentab as ot

from tests._support import (
    AttrScreen,
    FakeScreen,
    _model_row,
    app_with,
    box_cells,
    box_title,
    screen_text,
    workflow,
)


def open_calendar(app):
    app.handle_key(None, ord("T"))
    while app.trend_tabs[app.trend_tab] != "Calendar":
        app.handle_key(None, ord("l"))
    return app


def focus_calendar(app):
    open_calendar(app)
    app.handle_key(None, 10)
    return app


def test_bar_lane_keeps_the_bar_out_of_the_text_region():
    cells, text_w = ot.Renderer.bar_lane(57)
    assert cells == ot.BAR_CELLS
    assert text_w == 57 - 2 - (ot.BAR_CELLS + 2)
    # A narrow panel drops the bar and uses the full inner width for text.
    cells, text_w = ot.Renderer.bar_lane(40)
    assert cells == 0
    assert text_w == 38


def test_trends_survive_an_undated_workflow():
    app = app_with(
        [
            workflow("dated", "2026-06-03 12:00:00", cost=5),
            workflow("undated", "", cost=0, tokens=0),  # the metadata-only sidecar
        ]
    )
    # Weekly was the reported crash (week_key strptime on ""); Monthly/Daily/Calendar
    # are the latent siblings (month_range/month_bounds/int(year) on "").
    assert "2026-06-01" in app.renderer.trend_weekly(80, 16)[0]
    assert any("2026-06" in ln for ln in app.renderer.trend_monthly(80, 16))
    assert app.renderer.trend_daily(80, 16)[0].startswith("# Daily spend · 2026-06")
    assert app.calendar_years() == ["2026"]  # the undated row contributes no year bucket


def test_bar_chart_labels_bars_and_summarizes():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    lines = app.renderer._bar_chart([("d1", 0.0), ("d2", 1.0), ("d3", 2.0)], 80, 12)
    assert any("█" in ln for ln in lines)  # the peak bucket reaches full height
    assert "$2.00" in lines[0]  # the peak's spend rides on top of its bar, not a y-axis
    assert any("d1" in ln and "d3" in ln for ln in lines)  # x-axis tick labels
    assert any("peak" in ln and "total" in ln and "avg" in ln for ln in lines)


def test_bar_chart_compacts_crowded_edge_value_labels():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    lines = app.renderer._bar_chart(
        [(str(i), v) for i, v in enumerate((4.0, 17.0, 25.0, 7.0, 30.0, 26.0, 5.0), 1)],
        36,
        14,
    )
    assert any("$4" in ln for ln in lines)
    assert any("$5" in ln for ln in lines)


def test_bar_chart_floats_blocked_labels_up_so_no_bar_loses_its_price():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    lines = app.renderer._bar_chart([("1", 50.0), ("2", 20.0), ("3", 20.0)], 12, 16)
    assert "$50" in lines[0]  # peak still labelled on top
    assert sum(ln.count("$20") for ln in lines) == 2  # both shorter bars, not one


def test_bar_chart_fills_width_when_bars_are_dense():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    pairs = [(str(d), float(d)) for d in range(1, 31)]
    lines = app.renderer._bar_chart(pairs, 80, 18)
    baseline = next(ln for ln in lines if set(ln.strip()) == {"─"})
    assert len(baseline) >= 76  # fills ~width 80; the old fixed col_w stopped at ~61


def test_bar_chart_all_zero_window_reads_as_no_spend():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    lines = app.renderer._bar_chart([("Mon", 0.0), ("Tue", 0.0), ("Wed", 0.0)], 80, 12)
    assert any("no spend in view" in ln for ln in lines)
    assert not any("$1.00" in ln for ln in lines)
    assert not any("peak" in ln for ln in lines)


def test_trend_daily_shows_one_navigable_month():
    app = app_with(
        [
            workflow("jun", "2026-06-03 12:00:00", cost=5),
            workflow("may", "2026-05-10 12:00:00", cost=2),
        ]
    )
    app.trend_month_index = 0
    lines = app.renderer.trend_daily(80, 16)
    assert lines[0].startswith("# Daily spend · 2026-06")
    assert any("█" in ln for ln in lines) and any("peak" in ln for ln in lines)
    app.trend_month_index = 1
    assert app.renderer.trend_daily(80, 16)[0].startswith("# Daily spend · 2026-05")


def test_trend_weekly_shows_one_navigable_week():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=5),  # Mon, week of 2026-06-01
            workflow("b", "2026-06-03 12:00:00", cost=3),  # Wed, same week
            workflow("c", "2026-05-25 12:00:00", cost=2),  # Mon, the prior week
        ]
    )
    app.trend_week_index = 0
    lines = app.renderer.trend_weekly(80, 16)
    assert lines[0].startswith("# Weekly spend · 2026-06-01 – 2026-06-07")
    assert any("Mon" in ln for ln in lines) and any("Wed" in ln for ln in lines)
    assert any("█" in ln for ln in lines)
    assert any("peak" in ln and "$5.00" in ln and "Mon" in ln for ln in lines)
    app.trend_week_index = 1
    assert app.renderer.trend_weekly(80, 16)[0].startswith("# Weekly spend · 2026-05-25")


def test_trends_weekly_week_navigation_keys():
    app = app_with(
        [
            workflow("w1", "2026-06-01 12:00:00"),  # week of 2026-06-01 (newest)
            workflow("w2", "2026-05-25 12:00:00"),  # week of 2026-05-25
            workflow("w3", "2026-05-18 12:00:00"),  # week of 2026-05-18 (oldest)
        ]
    )
    app.handle_key(None, ord("T"))
    app.handle_key(None, ord("l"))
    assert app.trends and app.trend_tabs[app.trend_tab] == "Weekly"
    assert app.trend_week_index == 0
    app.handle_key(None, ord("j"))
    assert app.trend_week_index == 1
    app.handle_key(None, ord("j"))
    assert app.trend_week_index == 2
    app.handle_key(None, ord("j"))
    assert app.trend_week_index == 2
    app.handle_key(None, ord("k"))
    assert app.trend_week_index == 1


def test_trends_daily_month_navigation_keys():
    app = app_with(
        [
            workflow("jun", "2026-06-01 12:00:00"),
            workflow("may", "2026-05-01 12:00:00"),
            workflow("apr", "2026-04-01 12:00:00"),
        ]
    )
    app.handle_key(None, ord("T"))
    assert app.trends and app.trend_tab == 0 and app.trend_month_index == 0
    app.handle_key(None, ord("j"))
    assert app.trend_month_index == 1
    app.handle_key(None, ord("j"))
    assert app.trend_month_index == 2
    app.handle_key(None, ord("j"))
    assert app.trend_month_index == 2
    app.handle_key(None, ord("k"))
    assert app.trend_month_index == 1
    app.handle_key(None, ord("l"))
    assert app.trends and app.trend_tab == 1


def test_log_scale_spreads_a_skewed_spend_distribution():
    peak = 127.0
    bulk = (0.5, 1, 2, 4, 8)  # ordinary days, all well under the peak
    shades = {ot.heat_level(v, peak, 11) for v in bulk}
    assert len(shades) >= 4  # not one flat shade


def test_trends_calendar_year_navigation_keys():
    app = app_with(
        [
            workflow("y26", "2026-03-01 12:00:00"),  # newest year
            workflow("y25", "2025-03-01 12:00:00"),
            workflow("y24", "2024-03-01 12:00:00"),  # oldest year
        ]
    )
    app.handle_key(None, ord("T"))
    for _ in range(3):
        app.handle_key(None, ord("l"))
    assert app.trends and app.trend_tabs[app.trend_tab] == "Calendar"
    assert app.trend_year_index == 0
    app.handle_key(None, ord("j"))
    assert app.trend_year_index == 1
    app.handle_key(None, ord("j"))
    assert app.trend_year_index == 2
    app.handle_key(None, ord("j"))
    assert app.trend_year_index == 2
    app.handle_key(None, ord("k"))
    assert app.trend_year_index == 1


def test_draw_calendar_paints_heat_grid():
    app = app_with(
        [
            workflow("big", "2026-06-15 12:00:00", cost=50),  # the busiest day
            workflow("small", "2026-02-03 12:00:00", cost=1),
        ]
    )
    app.trend_year_index = 0
    # Wide enough that all 53 weeks (so every month) fit without truncation.
    screen = FakeScreen(24, 130)
    # color_pair()/init_pair() need a live initscr(); stub them so it runs headless.
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: 0
    ot.curses.init_pair = lambda *a: None
    try:
        app.renderer.draw_calendar(screen, 0, 0, 24, 130)
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    text = screen_text(screen)
    assert "Spend calendar · 2026" in text  # heading names the navigated year
    assert "Mon" in text and "Sun" in text  # every weekday row is labeled
    assert "Jan" in text and "Dec" in text  # all twelve months are labeled
    assert "per day" in text and "≤$50" in text  # legend's hottest band is the peak day
    assert "total" in text and "$50.00" in text  # peak day priced into the summary
    assert "█" in text  # the busiest day paints the hottest shade
    assert any(g in text for g in "·░▒▓")  # cooler tiers (empty + light days) render too


def test_calendar_heat_grid_dims_until_focused():
    from datetime import datetime

    app = app_with([workflow("big", "2026-06-15 12:00:00", cost=50)])
    app.trend_year_index = 0
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: 0
    ot.curses.init_pair = lambda *a: None
    try:
        unfocused = AttrScreen(24, 130)
        app.trend_focus = False
        app.renderer.draw_calendar(unfocused, 0, 0, 24, 130)
        gy0, row_pitch, gx, pitch, start_col, shown, year, grid_start = app._cal_geom
        cd = datetime.strptime("2026-06-15", "%Y-%m-%d")
        col = (cd - grid_start).days // 7 - start_col
        cy, cx = gy0 + cd.weekday() * row_pitch, gx + col * pitch
        dim_attr = unfocused.attrs[(cy, cx)]
        focused = AttrScreen(24, 130)
        app.trend_focus = True
        app.renderer.draw_calendar(focused, 0, 0, 24, 130)
        bright_attr = focused.attrs[(cy, cx)]
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    assert dim_attr & ot.curses.A_DIM and not dim_attr & ot.curses.A_BOLD  # asleep
    assert bright_attr & ot.curses.A_BOLD and not bright_attr & ot.curses.A_DIM  # awake


def test_calendar_shows_orange_enter_prompt_until_focused():
    app = app_with([workflow("big", "2026-06-15 12:00:00", cost=50)])
    app.trend_year_index = 0
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: n  # identity so we can read the pair off the attr
    ot.curses.init_pair = lambda *a: None
    try:
        unfocused = AttrScreen(24, 130)
        app.trend_focus = False
        app.renderer.draw_calendar(unfocused, 0, 0, 24, 130)
        focused = AttrScreen(24, 130)
        app.trend_focus = True
        app.renderer.draw_calendar(focused, 0, 0, 24, 130)
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    prompt = "Press Enter to navigate the calendar"
    assert prompt in screen_text(unfocused)  # shown while asleep
    assert prompt not in screen_text(focused)  # gone once the grid is live
    # Its first glyph is painted in the orange accent (pair 6) + bold.
    loc = next(
        (y, x)
        for (y, x), ch in unfocused.cells.items()
        if ch == "P" and unfocused.cells.get((y, x + 1)) == "r"
    )
    assert unfocused.attrs[loc] == 6 | ot.curses.A_BOLD


def test_calendar_cursor_defaults_to_the_busiest_day():
    app = app_with(
        [
            workflow("hot", "2026-07-09 12:00:00", cost=40),
            workflow("cool", "2026-03-02 12:00:00", cost=5),
        ]
    )
    open_calendar(app)
    assert app.cal_cursor is None
    assert app.calendar_cursor() == "2026-07-09"


def test_calendar_arrow_keys_walk_the_day_cursor():
    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    focus_calendar(app)
    app.handle_key(None, ot.curses.KEY_UP)
    assert app.cal_cursor == "2026-07-08"
    app.handle_key(None, ot.curses.KEY_LEFT)
    assert app.cal_cursor == "2026-07-01"
    app.handle_key(None, ot.curses.KEY_DOWN)
    assert app.cal_cursor == "2026-07-02"
    app.handle_key(None, ot.curses.KEY_RIGHT)
    assert app.cal_cursor == "2026-07-09"
    # Movement is clamped to the shown year: stepping before Jan 1 is a no-op.
    app.cal_cursor = "2026-01-01"
    app.handle_key(None, ot.curses.KEY_LEFT)  # would land in 2025 -> ignored
    assert app.cal_cursor == "2026-01-01"


def test_calendar_plus_minus_tunes_granularity():
    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    open_calendar(app)
    assert app.cal_levels == ot.HEAT_DEFAULT_LEVELS
    app.handle_key(None, ord("+"))
    assert app.cal_levels == ot.HEAT_DEFAULT_LEVELS + 1
    for _ in range(10):
        app.handle_key(None, ord("="))
    assert app.cal_levels == ot.HEAT_MAX_LEVELS
    for _ in range(20):
        app.handle_key(None, ord("-"))
    assert app.cal_levels == ot.HEAT_MIN_LEVELS
    assert app.cal_cursor in (None, "2026-07-09")


def test_calendar_enter_drills_into_the_day():
    app = app_with(
        [
            workflow("a", "2026-07-09 09:00:00", cost=40),
            workflow("b", "2026-07-09 18:00:00", cost=10),  # second session, same day
            workflow("c", "2026-02-01 12:00:00", cost=5),
        ]
    )
    focus_calendar(app)
    app.cal_cursor = "2026-07-09"
    app.handle_key(None, 10)
    assert not app.trends
    assert app.view == "zoom" and app.focus == "days"
    assert app.active_day == "2026-07-09"
    assert len(app.workflows) == 2


def test_calendar_enter_on_empty_day_nudges_and_stays():
    app = app_with([workflow("a", "2026-07-09 09:00:00", cost=40)])
    focus_calendar(app)
    app.cal_cursor = "2026-07-10"
    app.handle_key(None, 10)
    assert app.trends and app.view == "browse"
    assert "no sessions" in app.notice


def test_calendar_year_paging_reanchors_the_cursor():
    app = app_with(
        [
            workflow("y26", "2026-07-09 12:00:00", cost=40),
            workflow("y25", "2025-05-01 12:00:00", cost=8),
        ]
    )
    open_calendar(app)
    app.cal_cursor = "2026-07-09"
    app.handle_key(None, ord("j"))
    assert app.calendar_years()[app.trend_year_index] == "2025"
    assert app.cal_cursor is None
    assert app.calendar_cursor() == "2025-05-01"


def test_calendar_mouse_click_resolves_and_double_click_drills():
    from datetime import datetime

    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    focus_calendar(app)
    screen = FakeScreen(24, 130)
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: 0
    ot.curses.init_pair = lambda *a: None
    try:
        app.renderer.draw_calendar(screen, 0, 0, 24, 130)
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    gy0, row_pitch, gx, pitch, start_col, shown, year, grid_start = app._cal_geom
    cd = datetime.strptime("2026-07-09", "%Y-%m-%d")
    col = (cd - grid_start).days // 7 - start_col
    my, mx = gy0 + cd.weekday() * row_pitch, gx + col * pitch
    assert app._calendar_date_at(my, mx) == "2026-07-09"
    assert app._calendar_date_at(gy0 - 1, mx) is None  # above the grid -> no cell
    app._mouse_trends(my, mx, up=False, down=False, click=True, double=False)
    assert app.cal_cursor == "2026-07-09" and app.trends
    app._mouse_trends(my, mx, up=False, down=False, click=False, double=True)
    assert not app.trends and app.view == "zoom" and app.active_day == "2026-07-09"


def test_calendar_mouse_is_gated_until_focused():
    from datetime import datetime

    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    open_calendar(app)
    screen = FakeScreen(24, 130)
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: 0
    ot.curses.init_pair = lambda *a: None
    try:
        app.renderer.draw_calendar(screen, 0, 0, 24, 130)
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    gy0, row_pitch, gx, pitch, start_col, shown, year, grid_start = app._cal_geom
    cd = datetime.strptime("2026-07-09", "%Y-%m-%d")
    col = (cd - grid_start).days // 7 - start_col
    my, mx = gy0 + cd.weekday() * row_pitch, gx + col * pitch
    app._mouse_trends(my, mx, up=False, down=False, click=False, double=True)
    assert app.trend_focus and app.trends and app.view == "browse"
    assert app.cal_cursor is None


def test_calendar_escape_returns_to_the_heat_map():
    app = app_with(
        [
            workflow("a", "2026-07-09 12:00:00", cost=40),
            workflow("b", "2025-05-01 12:00:00", cost=8),
        ]
    )
    focus_calendar(app)
    app.cal_cursor = "2026-07-09"
    app.handle_key(None, 10)
    assert not app.trends and app.view == "zoom"
    app.handle_key(None, 27)
    assert app.trends and app.trend_tabs[app.trend_tab] == "Calendar"
    assert app.view == "browse"
    assert app.trend_year_index == app.calendar_years().index("2026")
    assert app.cal_cursor == "2026-07-09"
    assert app.trend_focus


def test_calendar_unfocused_arrows_switch_tabs():
    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    open_calendar(app)
    assert not app.trend_focus and app.cal_cursor is None
    app.handle_key(None, ot.curses.KEY_RIGHT)
    assert app.trend_tabs[app.trend_tab] != "Calendar"
    assert app.cal_cursor is None  # the day cursor never moved
    app.handle_key(None, ot.curses.KEY_LEFT)
    assert app.trend_tabs[app.trend_tab] == "Calendar" and not app.trend_focus


def test_calendar_enter_focuses_and_escape_steps_back_out():
    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    open_calendar(app)
    assert not app.trend_focus
    app.handle_key(None, 10)
    assert app.trend_focus and app.trends
    app.handle_key(None, ot.curses.KEY_UP)
    assert app.cal_cursor == "2026-07-08"
    app.handle_key(None, 27)
    assert app.trends and not app.trend_focus
    assert app.trend_tabs[app.trend_tab] == "Calendar"
    app.handle_key(None, 27)
    assert not app.trends


def test_normal_day_drill_does_not_bounce_back_to_the_calendar():
    app = app_with([workflow("a", "2026-07-09 12:00:00", cost=40)])
    focus_calendar(app)
    app.cal_cursor = "2026-07-09"
    app.handle_key(None, 10)
    assert app._trend_return == ("Calendar", "2026-07-09")
    app.view = "browse"
    app.focus = "days"
    app.drill_in()
    assert app._trend_return is None
    app.drill_out()
    assert not app.trends


def test_trend_daily_bars_focus_walk_and_drill():
    app = app_with(
        [
            workflow("a", "2026-06-10 12:00:00", cost=40),
            workflow("b", "2026-06-12 12:00:00", cost=8),
        ]
    )
    app.handle_key(None, ord("T"))
    assert app.trend_tabs[app.trend_tab] == "Daily" and not app.trend_focus
    app.handle_key(None, 10)
    assert app.trend_focus
    assert app.trend_bar_cursor() == "2026-06-10"
    app.handle_key(None, ot.curses.KEY_RIGHT)
    assert app.trend_bar_cursor() == "2026-06-11"
    app.handle_key(None, ot.curses.KEY_DOWN)
    assert app.trend_bar_cursor() == "2026-06-18"
    app.handle_key(None, 27)
    assert app.trends and not app.trend_focus
    app.handle_key(None, 10)
    app.trend_cursor = "2026-06-12"
    app.handle_key(None, 10)
    assert not app.trends and app.view == "zoom" and app.focus == "days"
    assert app.panel_days[app.day_index].day == "2026-06-12"
    app.handle_key(None, 27)
    assert app.trends and app.trend_tabs[app.trend_tab] == "Daily"
    assert app.trend_focus and app.trend_cursor == "2026-06-12"
    app.trend_cursor = "2026-06-01"
    app.handle_key(None, 10)
    assert app.trends and "no sessions on 2026-06-01" in app.notice


def test_trend_monthly_bar_drills_into_month():
    app = app_with(
        [
            workflow("jun", "2026-06-01 12:00:00", cost=5),
            workflow("apr", "2026-04-01 12:00:00", cost=9),
        ]
    )
    app.handle_key(None, ord("T"))
    while app.trend_tabs[app.trend_tab] != "Monthly":
        app.handle_key(None, ord("l"))
    app.handle_key(None, 10)
    assert app.trend_bar_cursor() == "2026-04"
    app.handle_key(None, ot.curses.KEY_RIGHT)
    assert app.trend_bar_cursor() == "2026-05"
    app.handle_key(None, 10)
    assert app.trends and "no spend in 2026-05" in app.notice
    app.handle_key(None, ot.curses.KEY_RIGHT)
    app.handle_key(None, 10)
    assert not app.trends and app.view == "zoom" and app.focus == "months"
    assert app.months[app.month_index].month == "2026-06"
    app.handle_key(None, 27)
    assert app.trends and app.trend_tabs[app.trend_tab] == "Monthly" and app.trend_focus
    assert app.trend_cursor == "2026-06"


def test_trend_daily_marks_the_selected_bar_when_focused():
    app = app_with([workflow("a", "2026-06-10 12:00:00", cost=40)])
    app.handle_key(None, ord("T"))
    app.handle_key(None, 10)
    lines = app.renderer.trend_daily(100, 20)
    marked = next((ln for ln in lines if "▲" in ln), None)
    assert marked is not None and "2026-06-10" in marked and "$40.00" in marked
    app.trend_focus = False
    assert not any("▲" in ln for ln in app.renderer.trend_daily(100, 20))


def test_trend_models_ranks_priced_models():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            {
                "model_name": "anthropic/m",
                "runs": 1,
                "cost": 5.0,
                "tokens_total": 10,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
            }
        ]
    }
    lines = app.renderer.trend_models(80, 12)
    assert box_title(lines).startswith("Model spend")
    assert any("anthropic/m" in ln and "$5.00" in ln and "█" in ln for ln in lines)


def test_trend_models_shows_long_names_in_full():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    long_name = "anthropic/claude-opus-4-5-20251101"  # 34 chars, would have truncated at 30
    app._model_by_root = {
        "a": [
            {
                "model_name": long_name,
                "runs": 1,
                "cost": 5.0,
                "tokens_total": 10,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
            }
        ]
    }
    lines = app.renderer.trend_models(80, 12)
    assert any(long_name in ln for ln in lines)  # full id, not cut off


def test_trend_providers_rolls_models_up_to_provider():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("anthropic/claude-opus-4-8", 5.0, 10),
            _model_row("anthropic/claude-haiku-4-5", 1.0, 4),
            _model_row("openai/gpt-5-mini", 2.0, 7),
        ]
    }
    lines = app.renderer.trend_providers(80, 12)
    assert box_title(lines).startswith("Spend by provider")
    cells = box_cells(lines, lead=True)
    # The two Anthropic models collapse into one "anthropic" row at $6.00 (5 + 1).
    anthropic = next(c for c in cells if c.startswith("anthropic"))
    assert "$6.00" in anthropic and "█" in anthropic
    openai = next(c for c in cells if c.startswith("openai"))
    assert "$2.00" in openai
    # Anthropic outspends OpenAI, so it ranks first.
    assert cells.index(anthropic) < cells.index(openai)


def test_trend_providers_lists_unpriced_provider_and_hints_at_dollar():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app.show_api_prices = False  # the real view; the estimate is the default
    app._model_by_root = {"a": [_model_row("github-copilot/gpt-5", 0.0, 5_000)]}
    lines = app.renderer.trend_providers(80, 12)
    # A subscription/credit provider records $0 but still shows its token volume...
    row = next(c for c in box_cells(lines, lead=True) if c.startswith("github-copilot"))
    assert "$0.00" in row and "5.0k" in row
    # ...and the view nudges toward "$" to price it.
    assert any("$ prices subscription" in ln for ln in lines)


def test_trend_models_rows_drill_into_sessions_and_a_session():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=5.0, directory="/x"),
            workflow("b", "2026-06-02 12:00:00", cost=2.0, directory="/x"),
        ]
    )
    app._model_by_root = {
        "a": [_model_row("anthropic/opus", 5.0, 10)],
        "b": [_model_row("openai/gpt-5", 2.0, 7)],
    }
    app.handle_key(None, ord("T"))
    while app.trend_tabs[app.trend_tab] != "Models":
        app.handle_key(None, ord("l"))
    assert app.trend_ranked_keys() == ["anthropic/opus", "openai/gpt-5"]
    app.handle_key(None, ord("j"))  # row cursor moves; the tab stays put
    assert app.trend_row_index == 1 and app.trend_tabs[app.trend_tab] == "Models"
    app.handle_key(None, 10)  # Enter -> the row's sessions list
    assert app.trend_drill == ("model", "openai/gpt-5")
    rows = app.trend_drill_sessions()
    assert [w.id for w, _c, _t in rows] == ["b"] and rows[0][1] == 2.0
    lines = app.renderer.trend_drill_lines(80, 12)
    assert box_title(lines).startswith("Sessions · openai/gpt-5")
    assert any("2026-06-02" in ln and "$2.00" in ln for ln in lines)
    app.handle_key(None, 10)  # Enter again -> straight into that session
    assert not app.trends and app.view == "session"
    assert app.current_session().id == "b"
    app.handle_key(None, 27)  # Esc -> back out to the day zoom
    app.handle_key(None, 27)  # Esc -> back to the Trends drill list
    assert app.trends and app.trend_drill == ("model", "openai/gpt-5")
    assert app.trend_tabs[app.trend_tab] == "Models"
    app.handle_key(None, 27)  # Esc -> back to the ranked rows
    assert app.trends and app.trend_drill is None


# --- sorting the ranked tabs ---------------------------------------------------------


def _ranked_app():
    # Spend order (anthropic > openai) is deliberately the reverse of both the token
    # order (openai's 900 > anthropic's 10) and the alphabetical one, so every column
    # sort visibly reorders the ranking instead of agreeing with the default.
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=5.0, directory="/x"),
            workflow("b", "2026-06-02 12:00:00", cost=2.0, directory="/y"),
        ]
    )
    app._model_by_root = {
        "a": [_model_row("anthropic/opus", 5.0, 10, runs=7)],
        "b": [_model_row("openai/gpt-5", 2.0, 900, runs=2)],
    }
    return app


def _open_trend_tab(app, name):
    if not app.trends:  # `T` on an open overlay closes it -- don't toggle it shut
        app.handle_key(None, ord("T"))
    while app.trend_tabs[app.trend_tab] != name:
        app.handle_key(None, ord("l"))
    return app


def test_trend_rankings_sort_by_any_of_their_columns():
    app = _open_trend_tab(_ranked_app(), "Providers")
    assert app.trend_ranked_keys() == ["anthropic", "openai"]  # cost, biggest first
    app.handle_key(None, ord("s"))
    assert app.sort_menu and app.sort_menu_options() == ("cost", "name", "tokens", "count")
    # The picker names the COLUMN, not the internal key -- "name"/"count" are shared
    # across four tables that call them different things.
    labels = [app.renderer.sort_label(k) for k in app.sort_menu_options()]
    assert labels == ["Cost", "Provider", "Tokens", "Msgs"]
    app.sort_menu_index = app.sort_menu_options().index("tokens")
    app.handle_key(None, 10)
    assert not app.sort_menu and app.trend_sort == "tokens"
    assert app.trend_ranked_keys() == ["openai", "anthropic"]
    # ...and the count column is Msgs here, sessions on the harness tabs.
    app.handle_key(None, ord("s"))
    app.sort_menu_index = app.sort_menu_options().index("count")
    app.handle_key(None, 10)
    assert app.trend_ranked_keys() == ["anthropic", "openai"]  # 7 msgs vs 2
    # The active column carries the arrow, and only it.
    header = box_cells(app.renderer.trend_providers(90, 12))[0]
    assert "Msgs v" in header and "Cost v" not in header and "Tokens v" not in header


def test_trend_sort_keeps_the_cursor_on_the_row_it_was_on():
    app = _open_trend_tab(_ranked_app(), "Providers")
    app.handle_key(None, ord("j"))  # onto "openai", the cheaper row
    assert app.selected_trend_key() == "openai" and app.trend_row_index == 1
    app.apply_header_sort("tokens", "trend")  # which is the token-heavy one -> row 0
    assert app.selected_trend_key() == "openai" and app.trend_row_index == 0


def test_trend_ranking_header_click_sorts_and_a_re_click_flips():
    app = _open_trend_tab(_ranked_app(), "Harnesses")
    rnd = app.renderer
    rnd._line_sort_headers = {}
    lines = rnd.trend_sources(100, 14)
    columns, target = rnd._line_sort_headers[rnd.BOX_HEADER_LINE]
    assert target == "trend"  # not "session"/"project" -- this is the Trends ranking
    assert ("name", "Harness") in columns and ("count", "Sess") in columns
    # The zones sit where the box actually painted the labels, gutters included.
    rnd.sort_regions = []
    rnd._register_line_sort_header(5, 2, rnd.BOX_HEADER_LINE, lines[rnd.BOX_HEADER_LINE], 96)
    tokens_zone = next(z for z in rnd.sort_regions if z[3] == "tokens")
    assert rnd.sort_hit(5, tokens_zone[1]) == ("tokens", "trend")
    app._mouse_trends(5, tokens_zone[1], False, False, True, False)
    assert app.trend_sort == "tokens" and not app.trend_sort_reverse
    app._mouse_trends(5, tokens_zone[1], False, False, True, False)
    assert app.trend_sort == "tokens" and app.trend_sort_reverse  # re-click flips


def test_trend_models_offers_only_the_columns_it_draws():
    app = _open_trend_tab(_ranked_app(), "Providers")
    app.apply_header_sort("tokens", "trend")
    app.handle_key(None, ord("h"))  # -> Models, which has no Tokens column
    assert app.trend_tabs[app.trend_tab] == "Models"
    assert app.trend_sort_options() == ("cost", "name")
    assert app.trend_sort_key() == "cost"  # withdrawn -> the column every tab shares
    assert app.trend_sort == "tokens"  # ...but the preference itself is kept
    assert "Cost v" in box_cells(app.renderer.trend_models(90, 12))[0]
    app.handle_key(None, ord("l"))  # back on Providers it is live again
    assert app.trend_sort_key() == "tokens"


def test_a_withdrawn_column_does_not_carry_its_direction_flip_onto_cost():
    app = _open_trend_tab(_ranked_app(), "Providers")
    app.apply_header_sort("tokens", "trend")
    app.apply_header_sort("tokens", "trend")  # re-click -> ascending tokens
    assert app.trend_sort_reverse
    app.handle_key(None, ord("h"))  # -> Models
    assert app.trend_sort_key() == "cost" and not app.trend_sort_reverse_for()
    assert app.trend_ranked_keys() == ["anthropic/opus", "openai/gpt-5"]  # dearest first
    assert "Cost v" in box_cells(app.renderer.trend_models(90, 12))[0]
    # Clicking the shown column flips what is ON SCREEN, not the withdrawn preference.
    app.apply_header_sort("cost", "trend")
    assert app.trend_sort == "cost" and app.trend_ranked_keys() == [
        "openai/gpt-5",
        "anthropic/opus",
    ]


def test_trend_sort_leaves_the_per_scope_harness_tables_alone():
    app = _open_trend_tab(_ranked_app(), "Harnesses")
    app.apply_header_sort("name", "trend")
    scoped = app.renderer.source_table(app.all_workflows, 90)
    assert "Harness ^" not in box_cells(scoped)[0] and "Harness v" not in box_cells(scoped)[0]
    assert app.source_rows(app.all_workflows) == sorted(
        app.source_rows(app.all_workflows),
        key=lambda kv: (float(kv[1]["cost"]), int(kv[1]["tokens"])),
        reverse=True,
    )


def test_trend_sort_is_inert_on_the_chart_tabs():
    app = _ranked_app()
    app.handle_key(None, ord("T"))
    assert app.trend_tabs[app.trend_tab] == "Daily"
    app.handle_key(None, ord("s"))
    assert not app.sort_menu and app.trends
    # ...and neither does a drilled row's session list, which is its own ranking.
    _open_trend_tab(app, "Models")
    app.handle_key(None, 10)
    assert app.trend_drill is not None
    app.handle_key(None, ord("s"))
    assert not app.sort_menu and app.trends and app.trend_drill is not None


def test_trend_drill_list_h_l_switch_tabs_instead_of_closing():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=5.0, directory="/x"),
            workflow("b", "2026-06-02 12:00:00", cost=2.0, directory="/x"),
        ]
    )
    app._model_by_root = {
        "a": [_model_row("anthropic/opus", 5.0, 10)],
        "b": [_model_row("openai/gpt-5", 2.0, 7)],
    }
    app.handle_key(None, ord("T"))
    while app.trend_tabs[app.trend_tab] != "Models":
        app.handle_key(None, ord("l"))
    app.handle_key(None, ord("j"))
    app.handle_key(None, 10)  # the model's sessions
    app.handle_key(None, 10)  # into a session
    app.handle_key(None, 27)  # Esc -> day zoom
    app.handle_key(None, 27)  # Esc -> back to the drill list
    assert app.trends and app.trend_drill == ("model", "openai/gpt-5")
    app.handle_key(None, ord("l"))  # -> Providers, drill left behind, overlay open
    assert app.trends and app.trend_drill is None
    assert app.trend_tabs[app.trend_tab] == "Providers" and app.trend_row_index == 0
    app.handle_key(None, ord("h"))  # and back onto Models
    assert app.trends and app.trend_tabs[app.trend_tab] == "Models"


def test_trends_overlay_toggles_and_switches_tabs():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert not app.trends
    app.handle_key(None, ord("T"))
    assert app.trends and app.trend_tab == 0
    app.handle_key(None, ord("l"))
    assert app.trend_tab == 1
    app.handle_key(None, ord("h"))
    assert app.trend_tab == 0
    app.handle_key(None, 27)  # Esc closes the overlay
    assert not app.trends


def test_trends_close_only_on_esc_q_or_T():
    app = app_with([workflow("a", "2026-06-10 12:00:00", cost=5)])
    app._models_loaded = True  # keep ? / P cheap (skip the deferred scan)
    app.handle_key(None, ord("T"))
    for key in (ord("e"), ord("R"), ord("x"), ord("o"), ord("b")):
        app.handle_key(None, key)
        assert app.trends, f"key {chr(key)!r} closed the overlay"
    app.handle_key(None, ord("?"))  # help floats above Trends...
    assert app.help and app.trends
    app.handle_key(None, ord("?"))  # ...and closing it lands back on Trends
    assert not app.help and app.trends
    app.handle_key(None, ord("P"))  # same for the prices overlay
    assert app.show_prices and app.trends
    app.handle_key(None, ord("q"))  # (P swallows unbound keys too; q closes it)
    assert not app.show_prices and app.trends
    app.handle_key(None, ord("q"))  # q closes the overlay (not the app)
    assert not app.trends
    app.handle_key(None, ord("T"))  # T toggles it open...
    app.handle_key(None, ord("T"))  # ...and closed again
    assert not app.trends
    app.handle_key(None, ord("T"))
    assert app.handle_key(None, 3) is False  # Ctrl-C still quits from inside
    # Inside a ranked row's drill list the same policy holds.
    a = app_with([workflow("s1", "2026-06-01 12:00:00", cost=5.0)])
    a._model_by_root = {"s1": [_model_row("anthropic/opus", 5.0, 10)]}
    a.handle_key(None, ord("T"))
    while a.trend_tabs[a.trend_tab] != "Models":
        a.handle_key(None, ord("l"))
    a.handle_key(None, 10)  # open the model's sessions
    assert a.trend_drill is not None
    a.handle_key(None, ord("x"))  # swallowed, list stays
    assert a.trends and a.trend_drill is not None
    a.handle_key(None, ord("q"))  # q closes the whole overlay from the drill too
    assert not a.trends and a.trend_drill is None


def test_dollar_key_toggles_prices_without_closing_trends():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app._models_loaded = True  # skip the deferred scan in toggle_api_prices
    app.show_api_prices = False
    app.handle_key(None, ord("T"))
    app.handle_key(None, ord("$"))
    assert app.show_api_prices  # repriced in place
    assert app.trends  # and the overlay stayed open
    app.handle_key(None, ord("$"))
    assert not app.show_api_prices and app.trends


def _fleet_app():
    w1 = workflow("a", "2026-05-01 10:00:00", cost=2.0)
    w1.machine = "laptop"
    w2 = workflow("b", "2026-05-02 10:00:00", cost=9.0)
    w2.machine = "server"
    return app_with([w1, w2])


def test_machines_trend_tab_only_in_the_fleet_view():
    assert "Machines" in _fleet_app().trend_tabs
    assert "Machines" not in app_with([workflow("a", "2026-05-01 10:00:00")]).trend_tabs


def test_trends_machines_drill_filters_sessions_by_machine():
    app = _fleet_app()
    app.trend_tab = app.trend_tabs.index("Machines")
    assert app.trend_ranked_keys() == ["server", "laptop"]  # cost-sorted
    app.trend_row_index = 0
    app._open_trend_drill()
    assert app.trend_drill == ("machine", "server")
    assert [s[0].id for s in app.trend_drill_sessions()] == ["b"]


def _tall_ranked_app(n=12):
    # A ranking taller than any viewport this test uses, across all four ranked tabs.
    import dataclasses

    ws = []
    for i in range(n):
        w = workflow(
            f"s{i}", "2026-06-%02d 12:00:00" % (i % 28 + 1), cost=1.0 + i, directory=f"/p{i}"
        )
        ws.append(
            dataclasses.replace(w, source=f"harness{i}", machine=f"box{i}")
            if hasattr(w, "source")
            else w
        )
    app = app_with(ws)
    app._model_by_root = {
        f"s{i}": [_model_row(f"vendor{i}/model{i}", 1.0 + i, 100 * (i + 1))] for i in range(n)
    }
    return app


def test_every_ranked_trends_table_fits_the_height_it_was_given():
    app = _tall_ranked_app()
    rnd = app.renderer
    for height in (8, 10, 14, 20):
        for name in (
            "trend_models",
            "trend_providers",
            "trend_projects",
            "trend_sources",
            "trend_machines",
        ):
            lines = getattr(rnd, name)(100, height)
            assert len(lines) <= height, (name, height, len(lines))
            # ...and what survives is a whole box: the TOTAL row and its bottom border.
            assert lines[-1].startswith(rnd.box_glyphs()["bl"]), (name, height, lines[-1])
            assert any("TOTAL" in ln for ln in lines), (name, height)


def test_a_windowed_harness_ranking_totals_the_whole_ranking():
    app = _tall_ranked_app()
    rnd = app.renderer
    full = sum(w.total_cost for w in app.all_workflows)
    for name in ("trend_sources", "trend_machines", "trend_projects"):
        lines = getattr(rnd, name)(100, 12)
        total_line = next(ln for ln in lines if "TOTAL" in ln)
        assert ot.money(full) in total_line, (name, total_line)
        # The window really is shorter than the ranking, or this proves nothing.
        assert sum(1 for ln in lines if "box" in ln or "harness" in ln or "/p" in ln) < 12


def _projects_app():
    # Three projects, one of them reached through a worktree (the sidebar folds a
    # worktree onto its git root, and the ranking has to group the same way).
    ws = [
        workflow("a", "2026-06-01 10:00:00", cost=5.0, tokens=500, directory="/repos/alpha"),
        workflow("b", "2026-06-02 10:00:00", cost=3.0, tokens=300, directory="/repos/beta"),
        workflow("c", "2026-06-03 10:00:00", cost=2.0, tokens=200, directory="/repos/alpha-wt"),
        workflow("d", "2026-06-04 10:00:00", cost=1.0, tokens=100, directory="/repos/gamma"),
    ]
    app = app_with(ws)
    app._root_by_dir = {"/repos/alpha-wt": "/repos/alpha"}
    app._invalidate_workflow_cache()
    return app


def test_trends_projects_ranks_by_git_root_and_drills_to_its_sessions():
    app = _projects_app()
    assert "Projects" in app.trend_tabs
    app.trend_tab = app.trend_tabs.index("Projects")
    # Worktree folded into its root, so alpha leads with 5 + 2 rather than trailing beta.
    assert app.trend_ranked_keys() == ["/repos/alpha", "/repos/beta", "/repos/gamma"]
    rows = dict(app.trend_ranked_rows())
    assert rows["/repos/alpha"] == {"cost": 7.0, "tokens": 700, "sessions": 2}
    app.trend_row_index = 0
    app._open_trend_drill()
    assert app.trend_drill == ("project", "/repos/alpha")
    # The drill must match by the SAME rule the ranking grouped by, or the worktree
    # session silently drops out of the list its own row counted.
    assert sorted(s[0].id for s in app.trend_drill_sessions()) == ["a", "c"]


def test_trends_projects_tab_is_reachable_and_paints_in_the_overlay():
    app = _projects_app()
    app.handle_key(None, ord("T"))
    while app.trend_tabs[app.trend_tab] != "Projects":
        app.handle_key(None, ord("l"))
    screen = FakeScreen(40, 120)
    # color_pair()/init_pair() need a live initscr(); stub them so it runs headless.
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: 0
    ot.curses.init_pair = lambda *a: None
    try:
        app.renderer.draw_trends(screen, 0, 39, 120)
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    text = screen_text(screen)
    assert "Spend by project" in text
    assert "/repos/alpha" in text and "Project" in text


def test_trends_projects_sorts_by_its_own_columns_and_keeps_cost_as_the_tiebreak():
    app = _projects_app()
    app.trend_tab = app.trend_tabs.index("Projects")
    assert set(app.trend_sort_options()) == {"cost", "name", "tokens", "count"}
    # The shared "count"/"name" keys are labelled for the column actually on screen.
    assert app.trend_sort_labels()["count"] == "Sessions"
    assert app.trend_sort_labels()["name"] == "Project"
    app.trend_sort = "count"
    # alpha has 2 sessions; beta/gamma have 1 each and keep the SPEND order under the
    # tie, rather than falling into dict order (sort_trend_rows' two-pass rule).
    assert app.trend_ranked_keys() == ["/repos/alpha", "/repos/beta", "/repos/gamma"]
    app.trend_sort_reverse = True  # a flip is measured from the column's natural order
    assert app.trend_ranked_keys() == ["/repos/beta", "/repos/gamma", "/repos/alpha"]


def test_trends_projects_fold_paths_instead_of_clipping_their_tails():
    home = os.path.expanduser("~")
    app = app_with(
        [
            workflow("a", "2026-06-01 10:00:00", cost=5.0, directory=f"{home}/Projects/alpha"),
            workflow("b", "2026-06-02 10:00:00", cost=1.0, directory=f"{home}/Projects/beta"),
        ]
    )
    lines = app.renderer.trend_projects(100, 14)
    body = [ln for ln in lines if "alpha" in ln or "beta" in ln]
    assert len(body) == 2
    # $HOME folds to ~ (so no row leaks the username), and the column is then narrow
    # enough that the costliest row still draws a real bar.
    assert all(home not in ln for ln in body)
    assert all("~/Projects/" in ln for ln in body)
    assert "\u2588" in body[0]
    # And where a path genuinely outgrows the column, the elision takes the MIDDLE: the
    # tail is what distinguishes two sibling projects.
    narrow = [ln for ln in app.renderer.trend_projects(50, 14) if "alpha" in ln or "beta" in ln]
    assert len(narrow) == 2
    assert all("..." in ln for ln in narrow)  # elided, and elided at the HEAD:
    assert "alpha" in narrow[0] and "beta" in narrow[1]


def test_trends_projects_drill_labels_its_path_instead_of_printing_it_raw():
    app = _projects_app()
    app.trend_tab = app.trend_tabs.index("Projects")
    app.trend_row_index = 0
    app._open_trend_drill()
    lines = app.renderer.trend_drill_lines(80, 14)
    title = next(ln for ln in lines if "Sessions" in ln)
    assert "alpha" in title and "session(s), most spend first" in title


def _reprice_app():
    # Real-cost order (alpha $5 > bravo $0) is deliberately the REVERSE of the
    # API-estimate order (bravo's unpriced tokens > alpha's $5), so "$" visibly
    # reorders every cost-ranked list instead of agreeing with the default.
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=5.0, directory="/alpha"),
            workflow("b", "2026-06-02 12:00:00", cost=0.0, directory="/bravo"),
        ]
    )
    app._model_by_root = {
        "a": [dict(_model_row("anthropic/claude-opus-4-5", 5.0, 10), input=0)],
        # $0 recorded with real tokens behind it: exactly what a subscription route
        # looks like, and what the "$" estimate exists to price.
        "b": [dict(_model_row("anthropic/claude-opus-4-5", 0.0, 20_000_000), input=20_000_000)],
    }
    app._models_loaded = True
    app._compute_api_costs()
    app._apply_price_mode()
    return app


def test_dollar_keeps_the_trends_cursor_on_the_row_it_was_on():
    app = _open_trend_tab(_reprice_app(), "Projects")
    assert app.show_api_prices  # the estimate view is the cold start
    assert [name for name, _ in app.trend_ranked_rows()] == ["/bravo", "/alpha"]
    assert app.selected_trend_key() == "/bravo" and app.trend_row_index == 0

    app.handle_key(None, ord("$"))  # to the recorded view, which reverses the ranking
    assert [name for name, _ in app.trend_ranked_rows()] == ["/alpha", "/bravo"]
    assert app.selected_trend_key() == "/bravo" and app.trend_row_index == 1


def test_dollar_keeps_the_trends_drill_on_the_session_it_was_on():
    app = _open_trend_tab(_reprice_app(), "Harnesses")
    app._open_trend_drill()
    assert app.trend_drill is not None
    ids = [w.id for w, _c, _t in app.trend_drill_sessions()]
    assert ids == ["b", "a"]  # bravo's estimate outranks alpha's recorded $5
    assert app.selected_trend_drill_id() == "b"

    app.handle_key(None, ord("$"))
    assert [w.id for w, _c, _t in app.trend_drill_sessions()] == ["a", "b"]
    assert app.selected_trend_drill_id() == "b" and app.trend_drill_index == 1


def test_dollar_is_a_reprice_in_place_not_a_navigation():
    app = _open_trend_tab(_reprice_app(), "Projects")
    app.trends = False
    app.scroll = 7
    app.handle_key(None, ord("$"))
    assert app.scroll == 7
