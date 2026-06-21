"""Unit tests for opentab's pure helpers and demo-mode logic.

Runs under pytest *or* standalone (`python test_opentab.py`) so CI needs no
third-party test runner. opentab is a src-layout package; we add src/ to
sys.path so the suite imports it without needing an editable install.
"""

import json
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))
import opentab as ot  # noqa: E402  (must follow the sys.path shim above)

# Isolate the whole suite from the developer's real ~/.config: point XDG at an
# empty temp dir so model_price() reads the *embedded* price table (not a local
# models.dev cache a `r`/--refresh-models run may have written) and no test reads
# or writes the real prefs/cache. Without this, the price assertions below pass on
# CI (no cache) but fail on a machine that has refreshed prices. The dir lives for
# the process; the held TemporaryDirectory cleans it up at exit.
_ISOLATED_CONFIG = tempfile.TemporaryDirectory(prefix="opentab-test-config-")
os.environ["XDG_CONFIG_HOME"] = _ISOLATED_CONFIG.name
ot.invalidate_price_cache()


def workflow(id, created_at, title=None, cost=1.0, tokens=100, directory="/tmp/project"):
    return ot.Workflow(
        id=id,
        title=title or id,
        directory=directory,
        created_at=created_at,
        root_cost=cost,
        total_cost=cost,
        subagents=0,
        model_count=1,
        total_tokens=tokens,
        unpriced_tokens=0,
    )


class FakeStore:
    demo = False
    records_cost = True
    source_name = "OpenCode"

    def __init__(self, workflows):
        self._workflows = workflows

    def workflows(self):
        return list(self._workflows)

    def model_breakdown(self):
        return []

    def summary(self, workflows):
        return {
            "workflows": len(workflows),
            "cost": sum(w.total_cost for w in workflows),
            "tokens": sum(w.total_tokens for w in workflows),
            "subagents": sum(w.subagents for w in workflows),
            "unpriced_tokens": sum(w.unpriced_tokens for w in workflows),
        }


def app_with(workflows, since=None, until=None, days=None):
    args = type("Args", (), {"since": since, "until": until, "days": days})()
    return ot.App(FakeStore(workflows), args)


class FakeScreen:
    # Just enough curses surface for the self-painting draw_* methods (which only
    # addstr onto a sized grid). Records every glyph by (y, x) so a test can read
    # back what was painted; ignores attributes (color is irrelevant to text checks).
    def __init__(self, height=24, width=80):
        self.height, self.width = height, width
        self.cells = {}

    def getmaxyx(self):
        return (self.height, self.width)

    def addstr(self, y, x, text, attr=0):
        for i, ch in enumerate(text):
            self.cells[(y, x + i)] = ch


def screen_text(screen):
    # Flatten the painted cells back into newline-joined rows (gaps become spaces).
    rows = {}
    for (y, x), ch in screen.cells.items():
        rows.setdefault(y, {})[x] = ch
    lines = []
    for y in sorted(rows):
        cols = rows[y]
        lines.append("".join(cols.get(x, " ") for x in range(min(cols), max(cols) + 1)))
    return "\n".join(lines)


def open_calendar(app):
    # Open the Trends overlay and tab across to the Calendar heat map.
    app.handle_key(None, ord("T"))
    while app.trend_tabs[app.trend_tab] != "Calendar":
        app.handle_key(None, ord("l"))
    return app


def test_human_tokens():
    assert ot.human_tokens(999) == "999"
    assert ot.human_tokens(1_500) == "1.5k"
    assert ot.human_tokens(2_000_000) == "2.0M"
    assert ot.human_tokens(3_000_000_000) == "3.0B"


def test_money_is_two_decimals():
    assert ot.money(195.6915) == "$195.69"
    assert ot.money(0) == "$0.00"
    assert ot.money(1_234_567.5) == "$1,234,567.50"


def test_money_marks_sub_cent_costs():
    # A nonzero cost under a cent must not look identical to a truly-zero row.
    assert ot.money(0.004) == "<$0.01"
    assert ot.money(0.0001) == "<$0.01"
    assert ot.money(0) == "$0.00"
    assert ot.money(0.02) == "$0.02"


def test_pct():
    assert ot.pct(50, 200) == "25%"
    assert ot.pct(1, 3) == "33%"
    assert ot.pct(1, 1000) == "<1%"  # 0.1% rounds visibly, not to "0%"
    assert ot.pct(0, 0) == "-"
    assert ot.pct(0, 10) == "0%"


def test_cost_bar():
    assert ot.cost_bar(0, 10) == " " * 8
    assert ot.cost_bar(10, 0) == " " * 8  # no peak -> blank, never divides by zero
    assert ot.cost_bar(10, 10) == "█" * 8
    assert all(len(ot.cost_bar(v, 10)) == 8 for v in (0, 1, 3, 5, 7, 10))
    assert ot.cost_bar(5, 10).startswith("████") and not ot.cost_bar(5, 10).startswith("█████")
    assert ot.cost_bar(1, 1000).startswith("▏")  # tiny-but-nonzero shows a sliver


def test_bar_lane_keeps_the_bar_out_of_the_text_region():
    # A wide panel gets a dedicated bar lane (so a row highlight never inverts it)
    # plus a text region for everything else.
    cells, text_w = ot.Renderer.bar_lane(57)
    assert cells == ot.BAR_CELLS
    assert text_w == 57 - 2 - (ot.BAR_CELLS + 2)
    # A narrow panel drops the bar and uses the full inner width for text.
    cells, text_w = ot.Renderer.bar_lane(40)
    assert cells == 0
    assert text_w == 38


def test_month_range():
    assert ot.month_range("2025-12", "2026-02") == ["2025-12", "2026-01", "2026-02"]
    assert ot.month_range("2026-05", "2026-05") == ["2026-05"]


def test_week_key_buckets_monday_to_sunday():
    # Mon..Sun of one ISO week all fold to that week's Monday; the label sorts as a
    # plain string, so the year boundary lands in the right (prior) week.
    assert ot.week_key("2026-06-01 09:00:00") == "2026-06-01"  # Monday
    assert ot.week_key("2026-06-03 12:00:00") == "2026-06-01"  # Wednesday, same week
    assert ot.week_key("2026-06-07 23:59:59") == "2026-06-01"  # Sunday, still same week
    assert ot.week_key("2026-06-08 00:00:00") == "2026-06-08"  # next Monday, next week
    assert ot.week_key("2026-01-01 12:00:00") == "2025-12-29"  # folds into prior year's week


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
    # Regression: two equal-height bars below the peak sit close enough that their
    # value labels would land on the same row and collide. The blocked one must
    # float up to the next free row, not get dropped -- every bar keeps its price.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    lines = app.renderer._bar_chart([("1", 50.0), ("2", 20.0), ("3", 20.0)], 12, 16)
    assert "$50" in lines[0]  # peak still labelled on top
    assert sum(ln.count("$20") for ln in lines) == 2  # both shorter bars, not one


def test_bar_chart_fills_width_when_bars_are_dense():
    # A full month of bars must spread across (nearly) the whole plot width, not
    # stop a third short because an integer column width didn't divide the width
    # evenly -- that empty right margin is what crammed the wide "$x.xx" labels
    # together. The baseline rule should run almost the full width.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    pairs = [(str(d), float(d)) for d in range(1, 31)]
    lines = app.renderer._bar_chart(pairs, 80, 18)
    baseline = next(ln for ln in lines if set(ln.strip()) == {"─"})
    assert len(baseline) >= 76  # fills ~width 80; the old fixed col_w stopped at ~61


def test_bar_chart_all_zero_window_reads_as_no_spend():
    # An all-empty window (e.g. browsing to a quiet week) must not borrow the
    # divide-by-zero guard (1.0) as a fake "$1.00 peak".
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    lines = app.renderer._bar_chart([("Mon", 0.0), ("Tue", 0.0), ("Wed", 0.0)], 80, 12)
    assert any("no spend in view" in ln for ln in lines)
    assert not any("$1.00" in ln for ln in lines)
    assert not any("peak" in ln for ln in lines)


def test_top_models_has_full_model_columns():
    # The "Top Models" overview section reuses the Models-tab table, so it carries
    # the cache/output columns too (name, runs, cost, tokens, cacheR, cacheW, output).
    app = app_with([])
    rows = [("m", 3648, 1.0, 205_600_000, 1_000_000, 2_000_000, 5_000_000)]
    lines = app.renderer._model_table(rows, "# Top Models", 120)
    assert lines[0] == "# Top Models"
    assert lines[1].split() == [
        "Model",
        "Msgs",
        "Cost",
        "Share",
        "Tokens",
        "CacheR",
        "CacheW",
        "Output",
    ]
    assert "$1.00" in lines[2]
    assert "205.6M" in lines[2]
    assert "3648" in lines[2]


def test_trend_daily_shows_one_navigable_month():
    app = app_with(
        [
            workflow("jun", "2026-06-03 12:00:00", cost=5),
            workflow("may", "2026-05-10 12:00:00", cost=2),
        ]
    )
    # Default: most recent month (June)
    app.trend_month_index = 0
    lines = app.renderer.trend_daily(80, 16)
    assert lines[0].startswith("# Daily spend · 2026-06")
    assert any("█" in ln for ln in lines) and any("peak" in ln for ln in lines)
    # Navigating older shows the previous month (May)
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
    # Default: most recent week, x-axis Mon..Sun of that week.
    app.trend_week_index = 0
    lines = app.renderer.trend_weekly(80, 16)
    assert lines[0].startswith("# Weekly spend · 2026-06-01 – 2026-06-07")
    assert any("Mon" in ln for ln in lines) and any("Wed" in ln for ln in lines)
    assert any("█" in ln for ln in lines)
    # Monday ($5) outspends Wednesday ($3) within this week, so it is the peak.
    assert any("peak" in ln and "$5.00" in ln and "Mon" in ln for ln in lines)
    # Navigating older shows the previous week (2026-05-25).
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
    app.handle_key(None, ord("T"))  # opens on Daily at the newest bucket
    app.handle_key(None, ord("l"))  # -> Weekly tab
    assert app.trends and app.trend_tabs[app.trend_tab] == "Weekly"
    assert app.trend_week_index == 0
    app.handle_key(None, ord("j"))  # older
    assert app.trend_week_index == 1
    app.handle_key(None, ord("j"))
    assert app.trend_week_index == 2
    app.handle_key(None, ord("j"))  # clamped at the oldest of 3 weeks
    assert app.trend_week_index == 2
    app.handle_key(None, ord("k"))  # newer
    assert app.trend_week_index == 1


def test_trends_daily_month_navigation_keys():
    app = app_with(
        [
            workflow("jun", "2026-06-01 12:00:00"),
            workflow("may", "2026-05-01 12:00:00"),
            workflow("apr", "2026-04-01 12:00:00"),
        ]
    )
    app.handle_key(None, ord("T"))  # opens at newest month
    assert app.trends and app.trend_tab == 0 and app.trend_month_index == 0
    app.handle_key(None, ord("j"))  # older
    assert app.trend_month_index == 1
    app.handle_key(None, ord("j"))
    assert app.trend_month_index == 2
    app.handle_key(None, ord("j"))  # clamped at the oldest of 3 months
    assert app.trend_month_index == 2
    app.handle_key(None, ord("k"))  # newer
    assert app.trend_month_index == 1
    app.handle_key(None, ord("l"))  # switch to the next tab (Weekly); still open
    assert app.trends and app.trend_tab == 1


def test_heat_level_buckets():
    assert ot.heat_level(0, 10, 6) == 0  # no spend -> no shade
    assert ot.heat_level(5, 0, 6) == 0  # no peak -> no shade, never divides by zero
    assert ot.heat_level(10, 10, 6) == 6  # the busiest day is the hottest tier
    assert ot.heat_level(0.001, 1000, 6) == 1  # any nonzero day shows at least the faintest tier
    # Log scale: levels climb monotonically with spend, and a mid-magnitude day lands
    # mid-ramp instead of being shoved into the faintest tier like a linear ramp would.
    climbing = [ot.heat_level(v, 1000, 6) for v in (1, 10, 100, 1000)]
    assert climbing == sorted(climbing) and climbing[-1] == 6  # non-decreasing, peak on top
    assert 1 < ot.heat_level(1000**0.5, 1000, 6) < 6  # the geometric midpoint is mid-ramp


def test_log_scale_spreads_a_skewed_spend_distribution():
    # The user's complaint: a few heavy days set a high peak, the bulk are small, and a
    # linear ramp dumped nearly all of them into tier 1 so every cell looked the same.
    # The log scale must light the common low-spend days up across several distinct tiers.
    peak = 127.0
    bulk = (0.5, 1, 2, 4, 8)  # ordinary days, all well under the peak
    shades = {ot.heat_level(v, peak, 11) for v in bulk}
    assert len(shades) >= 4  # not one flat shade


def test_heat_palette_grows_greener_to_red():
    # Every level is a genuinely distinct shade on a 256-color terminal, all the way up
    # to the finest granularity, and the ramp runs from green to red.
    for n in range(ot.HEAT_MIN_LEVELS, ot.HEAT_MAX_LEVELS + 1):
        pal = ot.heat_palette(n, has256=True)
        assert len(pal) == n and len(set(pal)) == n  # no two levels share a color
    full = ot.heat_palette(ot.HEAT_MAX_LEVELS, has256=True)
    cool, hot = full[0], full[-1]  # cube index = 16 + 36*r + 6*g + b (r,g,b in 0..5)
    assert (cool - 16) // 6 % 6 > (cool - 16) // 36  # cool end: green channel > red
    assert (hot - 16) // 36 > (hot - 16) // 6 % 6  # hot end: red channel > green
    # The 8-color fallback collapses the hues into the three ANSI heat colors.
    eight = ot.heat_palette(6, has256=False)
    assert eight[0] == ot.curses.COLOR_GREEN and eight[-1] == ot.curses.COLOR_RED


def test_heat_levels_are_visually_distinct():
    # The user's complaint: two adjacent legend swatches looked identical. Guard it —
    # on 256-color every level is a distinct color; on 8-color (where only three colors
    # exist) every level is a distinct (color, glyph) pair, so none ever look the same.
    for n in range(ot.HEAT_MIN_LEVELS, ot.HEAT_MAX_LEVELS + 1):
        colors = ot.heat_palette(n, has256=False)
        glyphs = [ot.heat_glyph(lvl, n, has256=False) for lvl in range(1, n + 1)]
        assert len(set(zip(colors, glyphs))) == n  # no repeated (color, glyph) combo


def test_heat_glyph_spans_the_density_ramp():
    assert ot.heat_glyph(0, 6) == "·"  # an in-range day with no spend
    for levels in (3, 6, 9):
        assert ot.heat_glyph(1, levels) in "░▒"  # the faintest tier is light
        assert ot.heat_glyph(levels, levels) == "█"  # the hottest tier is a solid block


def test_calendar_cells_layout():
    by_date = {"2026-01-01": 3.0, "2026-12-31": 5.0}
    grid, months, ncols = ot.calendar_cells("2026", by_date)
    assert len(grid) == 7 and ncols == 53  # 7 weekday rows by 53 week columns
    # 2026-01-01 is a Thursday (weekday 3) and lands in the first column.
    assert grid[3][0] == 3.0
    # The Mon/Wed before it are padding days outside the year, not zero-spend days.
    assert grid[0][0] is None and grid[2][0] is None
    # An in-year day with no spend is 0.0 (a real cell), not None.
    assert grid[4][0] == 0.0  # Fri 2026-01-02
    # The last day of the year is a Thursday in the final column.
    assert grid[3][52] == 5.0  # Thu 2026-12-31
    # Every month is anchored once; January sits at column 0.
    assert (0, "Jan") in months and len(months) == 12


def test_trends_calendar_year_navigation_keys():
    app = app_with(
        [
            workflow("y26", "2026-03-01 12:00:00"),  # newest year
            workflow("y25", "2025-03-01 12:00:00"),
            workflow("y24", "2024-03-01 12:00:00"),  # oldest year
        ]
    )
    app.handle_key(None, ord("T"))  # opens on Daily
    for _ in range(3):  # Daily -> Weekly -> Monthly -> Calendar
        app.handle_key(None, ord("l"))
    assert app.trends and app.trend_tabs[app.trend_tab] == "Calendar"
    assert app.trend_year_index == 0
    app.handle_key(None, ord("j"))  # older
    assert app.trend_year_index == 1
    app.handle_key(None, ord("j"))
    assert app.trend_year_index == 2
    app.handle_key(None, ord("j"))  # clamped at the oldest of 3 years
    assert app.trend_year_index == 2
    app.handle_key(None, ord("k"))  # newer
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


def test_calendar_cursor_defaults_to_the_busiest_day():
    app = app_with(
        [
            workflow("hot", "2026-07-09 12:00:00", cost=40),
            workflow("cool", "2026-03-02 12:00:00", cost=5),
        ]
    )
    open_calendar(app)
    assert app.cal_cursor is None  # nothing pinned yet
    assert app.calendar_cursor() == "2026-07-09"  # defaults to the peak-spend day


def test_calendar_arrow_keys_walk_the_day_cursor():
    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    open_calendar(app)
    app.handle_key(None, ot.curses.KEY_UP)  # -1 day
    assert app.cal_cursor == "2026-07-08"
    app.handle_key(None, ot.curses.KEY_LEFT)  # -7 days
    assert app.cal_cursor == "2026-07-01"
    app.handle_key(None, ot.curses.KEY_DOWN)  # +1 day
    assert app.cal_cursor == "2026-07-02"
    app.handle_key(None, ot.curses.KEY_RIGHT)  # +7 days
    assert app.cal_cursor == "2026-07-09"
    # Movement is clamped to the shown year: stepping before Jan 1 is a no-op.
    app.cal_cursor = "2026-01-01"
    app.handle_key(None, ot.curses.KEY_LEFT)  # would land in 2025 -> ignored
    assert app.cal_cursor == "2026-01-01"


def test_calendar_plus_minus_tunes_granularity():
    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    open_calendar(app)
    assert app.cal_levels == ot.HEAT_DEFAULT_LEVELS
    app.handle_key(None, ord("+"))  # one more shade
    assert app.cal_levels == ot.HEAT_DEFAULT_LEVELS + 1
    for _ in range(10):  # spam + past the ceiling
        app.handle_key(None, ord("="))  # '=' is '+' without shift
    assert app.cal_levels == ot.HEAT_MAX_LEVELS  # clamped at the finest ramp
    for _ in range(20):  # spam - past the floor
        app.handle_key(None, ord("-"))
    assert app.cal_levels == ot.HEAT_MIN_LEVELS  # clamped at the coarsest ramp
    # The cursor is untouched by granularity changes.
    assert app.cal_cursor in (None, "2026-07-09")


def test_calendar_enter_drills_into_the_day():
    app = app_with(
        [
            workflow("a", "2026-07-09 09:00:00", cost=40),
            workflow("b", "2026-07-09 18:00:00", cost=10),  # second session, same day
            workflow("c", "2026-02-01 12:00:00", cost=5),
        ]
    )
    open_calendar(app)
    app.cal_cursor = "2026-07-09"
    app.handle_key(None, 10)  # Enter
    assert not app.trends  # overlay closed
    assert app.view == "zoom" and app.focus == "days"
    assert app.active_day == "2026-07-09"
    assert len(app.workflows) == 2  # both of that day's sessions


def test_calendar_enter_on_empty_day_nudges_and_stays():
    app = app_with([workflow("a", "2026-07-09 09:00:00", cost=40)])
    open_calendar(app)
    app.cal_cursor = "2026-07-10"  # an in-year day with no sessions
    app.handle_key(None, 10)  # Enter
    assert app.trends and app.view == "browse"  # stayed in the calendar, no drill
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
    app.handle_key(None, ord("j"))  # page to the older year (2025)
    assert app.calendar_years()[app.trend_year_index] == "2025"
    assert app.cal_cursor is None  # re-anchored off the stale 2026 day
    assert app.calendar_cursor() == "2025-05-01"  # that year's peak day


def test_calendar_mouse_click_resolves_and_double_click_drills():
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
    assert app._calendar_date_at(my, mx) == "2026-07-09"
    assert app._calendar_date_at(gy0 - 1, mx) is None  # above the grid -> no cell
    # A single click moves the cursor; a double-click drills in.
    app._mouse_trends(my, mx, up=False, down=False, click=True, double=False)
    assert app.cal_cursor == "2026-07-09" and app.trends
    app._mouse_trends(my, mx, up=False, down=False, click=False, double=True)
    assert not app.trends and app.view == "zoom" and app.active_day == "2026-07-09"


def test_calendar_escape_returns_to_the_heat_map():
    app = app_with(
        [
            workflow("a", "2026-07-09 12:00:00", cost=40),
            workflow("b", "2025-05-01 12:00:00", cost=8),
        ]
    )
    open_calendar(app)
    app.cal_cursor = "2026-07-09"
    app.handle_key(None, 10)  # Enter -> drill into that day
    assert not app.trends and app.view == "zoom"
    app.handle_key(None, 27)  # Esc -> back to the heat map, not just to browse
    assert app.trends and app.trend_tabs[app.trend_tab] == "Calendar"
    assert app.view == "browse"
    assert app.trend_year_index == app.calendar_years().index("2026")
    assert app.cal_cursor == "2026-07-09"  # cursor restored to the day we came from


def test_normal_day_drill_does_not_bounce_back_to_the_calendar():
    # Only a heat-map drill arms the Esc-return; an ordinary panel drill must clear it.
    app = app_with([workflow("a", "2026-07-09 12:00:00", cost=40)])
    open_calendar(app)
    app.cal_cursor = "2026-07-09"
    app.handle_key(None, 10)  # heat-map drill arms the return
    assert app._cal_return == "2026-07-09"
    app.view = "browse"  # back out to the panels and drill a day the ordinary way
    app.focus = "days"
    app.drill_in()
    assert app._cal_return is None  # the fresh drill disarmed it
    app.drill_out()
    assert not app.trends  # so Esc stays in browse, no calendar bounce


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
    assert lines[0].startswith("# Model spend")
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


def _model_row(model_name, cost, tokens):
    return {
        "model_name": model_name,
        "runs": 1,
        "cost": cost,
        "tokens_total": tokens,
        "cache_read": 0,
        "cache_write": 0,
        "output": 0,
    }


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
    assert lines[0].startswith("# Spend by provider")
    # The two Anthropic models collapse into one "anthropic" row at $6.00 (5 + 1).
    anthropic = next(ln for ln in lines if ln.startswith("anthropic"))
    assert "$6.00" in anthropic and "█" in anthropic
    openai = next(ln for ln in lines if ln.startswith("openai"))
    assert "$2.00" in openai
    # Anthropic outspends OpenAI, so it ranks first.
    assert lines.index(anthropic) < lines.index(openai)


def test_trend_providers_lists_unpriced_provider_and_hints_at_dollar():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {"a": [_model_row("github-copilot/gpt-5", 0.0, 5_000)]}
    lines = app.renderer.trend_providers(80, 12)
    # A subscription/credit provider records $0 but still shows its token volume...
    row = next(ln for ln in lines if ln.startswith("github-copilot"))
    assert "$0.00" in row and "5.0k" in row
    # ...and the view nudges toward "$" to price it.
    assert any("$ prices subscription" in ln for ln in lines)


def test_capital_p_opens_model_prices_overlay():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            {
                "model_name": "claude-opus-4-8",
                "runs": 1,
                "cost": 5.0,
                "tokens_total": 10,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
            }
        ]
    }
    assert not app.show_prices
    app.handle_key(None, ord("P"))
    assert app.show_prices  # opens the reference overlay
    lines = app.renderer.price_table_lines(80)
    # opus-4-8 lists at $5 in / $25 out per 1M, from the embedded models.dev table
    assert any("claude-opus-4-8" in ln and "5.00" in ln and "25.00" in ln for ln in lines)
    assert any("models.dev" in ln for ln in lines)
    app.handle_key(None, 27)  # esc (a non-nav key) closes it
    assert not app.show_prices


def test_jk_scrolls_the_prices_overlay():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app.handle_key(None, ord("P"))
    assert app.show_prices and app.prices_scroll == 0
    app.handle_key(None, ord("j"))
    assert app.prices_scroll == 1 and app.show_prices  # scrolls, stays open
    app.handle_key(None, ord("k"))
    assert app.prices_scroll == 0
    app.handle_key(None, ord("k"))  # floored at the top
    assert app.prices_scroll == 0
    app.handle_key(None, ord("G"))
    assert app.prices_scroll > 0  # jumps toward the bottom (clamped on draw)
    app.handle_key(None, ord("g"))
    assert app.prices_scroll == 0
    app.handle_key(None, ord("x"))  # any other key closes
    assert not app.show_prices


def test_f_filters_the_prices_overlay_by_model_name():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("claude-sonnet-4-5", 1.0, 10),
            _model_row("gpt-5-codex", 2.0, 10),
        ]
    }
    app.handle_key(None, ord("P"))
    assert app.show_prices
    app.handle_key(None, ord("f"))
    assert app.filter_active and app.show_prices
    for ch in "sonnet":
        app.handle_key(None, ord(ch))
    lines = app.renderer.price_table_lines(80)
    assert any("claude-sonnet-4-5" in ln for ln in lines)
    assert not any("gpt-5-codex" in ln for ln in lines)
    app.handle_key(None, 10)
    assert not app.filter_active and app.show_prices and app.query == "sonnet"


def test_jk_scrolls_the_help_overlay():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app.handle_key(None, ord("?"))
    assert app.help and app.help_scroll == 0
    app.handle_key(None, ord("j"))
    assert app.help_scroll == 1 and app.help
    app.handle_key(None, ord("k"))
    assert app.help_scroll == 0
    app.handle_key(None, ord("G"))
    assert app.help_scroll > 0
    app.handle_key(None, ord("g"))
    assert app.help_scroll == 0
    app.handle_key(None, ord("x"))
    assert not app.help


def test_trends_overlay_toggles_and_switches_tabs():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert not app.trends
    app.handle_key(None, ord("T"))
    assert app.trends and app.trend_tab == 0
    app.handle_key(None, ord("l"))
    assert app.trend_tab == 1
    app.handle_key(None, ord("h"))
    assert app.trend_tab == 0
    app.handle_key(None, 27)  # Esc (a non-nav key) closes the overlay
    assert not app.trends


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


def test_mouse_hit_resolves_clicks_against_regions():
    app = app_with(
        [
            workflow("jun", "2026-06-01 12:00:00"),
            workflow("may", "2026-05-01 12:00:00"),
        ]
    )
    app.focus = "months"
    # The regions a draw() would register: a months list at rows y=5..6 and a
    # detail tab label on row y=3.
    app.renderer.regions = [
        ("rows", "month", 5, 6, 0, 30, 0),
        ("tab", 3, 10, 19, 2),
    ]
    assert app.renderer.hit(5, 4) == ("month", 0)
    assert app.renderer.hit(6, 4) == ("month", 1)
    assert app.renderer.hit(6, 99) is None  # outside the x range
    assert app.renderer.hit(7, 4) is None  # below the rows
    assert app.renderer.hit(3, 12) == ("tab", 2)


def test_mouse_click_selects_and_double_click_drills():
    app = app_with(
        [
            workflow("jun", "2026-06-01 12:00:00"),
            workflow("may", "2026-05-01 12:00:00"),
        ]
    )
    app.focus = "months"
    app._apply_click(("month", 1), drill=False)
    assert app.month_index == 1 and app.view == "browse"  # single click only selects
    app._apply_click(("month", 1), drill=True)
    assert app.view == "zoom"  # double-click drills in
    app._apply_click(("tab", 2), drill=False)
    assert app.tab == 2  # clicking a tab switches detail tab


def test_mouse_click_on_day_row_switches_focus():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.focus = "months"
    app._apply_click(("day", 0), drill=False)
    assert app.focus == "days"  # clicking the days panel focuses it


def test_handle_mouse_wheel_scrolls_the_list():
    app = app_with(
        [
            workflow("a", "2026-06-10 12:00:00"),
            workflow("b", "2026-05-10 12:00:00"),
            workflow("c", "2026-04-10 12:00:00"),
        ]
    )
    app.focus = "months"
    # Mirror run(): on builds without BUTTON5_PRESSED the wheel-down bit is the one
    # otherwise labelled REPORT_MOUSE_POSITION.
    app._wheel_down = getattr(ot.curses, "BUTTON5_PRESSED", 0) or ot.curses.REPORT_MOUSE_POSITION
    orig = ot.curses.getmouse
    try:
        ot.curses.getmouse = lambda: (0, 0, 0, 0, app._wheel_down)  # wheel down
        app.handle_mouse()
        assert app.month_index == 2  # scrolled down by 3, clamped to last month
        ot.curses.getmouse = lambda: (0, 0, 0, 0, ot.curses.BUTTON4_PRESSED)  # wheel up
        app.handle_mouse()
        assert app.month_index == 0  # scrolled back up
    finally:
        ot.curses.getmouse = orig


def test_resolve_project_root_folds_worktree():
    with tempfile.TemporaryDirectory() as tmp:
        main = os.path.join(tmp, "app")
        os.makedirs(os.path.join(main, ".git", "worktrees", "feat"))
        wt = os.path.join(tmp, "app-feat")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write(f"gitdir: {main}/.git/worktrees/feat\n")
        assert ot.resolve_project_root(wt) == main
        # a real repo (.git is a directory) and unknown paths resolve to themselves
        assert ot.resolve_project_root(main) == main
        assert ot.resolve_project_root(os.path.join(tmp, "nope")) == os.path.join(tmp, "nope")


def test_resolve_project_root_path_fallback_for_removed_worktree():
    # The worktree directory no longer exists (only its sessions remain in the DB),
    # so we cannot read its .git file — fold by the path convention instead.
    assert (
        ot.resolve_project_root("/Users/x/SoftwareProjects/mpvv/.worktrees/refactor")
        == "/Users/x/SoftwareProjects/mpvv"
    )
    assert ot.resolve_project_root("/repo/.git/worktrees/feat") == "/repo"
    assert ot.resolve_project_root("/Users/x/code/plain-repo") == "/Users/x/code/plain-repo"


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


def test_ignored_projects_are_filtered_but_can_be_shown_and_unignored():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, directory="/repo/a"),
            workflow("b", "2026-06-02 12:00:00", cost=5, directory="/repo/b"),
        ]
    )
    app.browse_mode = "projects"
    app.project_index = 0

    assert [p.directory for p in app.projects] == ["/repo/b", "/repo/a"]
    assert app.handle_key(None, ord("i"))

    assert app.ignored_projects == {"/repo/b"}
    assert [w.id for w in app.all_workflows] == ["a"]
    assert app.months[0].cost == 1
    assert [p.directory for p in app.projects] == ["/repo/a"]
    assert app.current_sessions()[0].id == "a"

    assert app.handle_key(None, ord("I"))
    shown = {p.directory: p for p in app.projects}
    assert set(shown) == {"/repo/a", "/repo/b"}
    assert shown["/repo/b"].ignored

    app.project_index = next(i for i, p in enumerate(app.projects) if p.directory == "/repo/b")
    assert app.handle_key(None, ord("i"))
    assert app.ignored_projects == set()
    assert sum(w.total_cost for w in app.all_workflows) == 6


def test_ignored_project_detail_still_uses_its_workflows_when_shown():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, directory="/repo/a"),
            workflow("b", "2026-06-02 12:00:00", cost=5, directory="/repo/b"),
        ]
    )
    app.ignored_projects = {"/repo/b"}
    app.show_ignored_projects = True
    app.browse_mode = "projects"
    app.project_index = next(i for i, p in enumerate(app.projects) if p.directory == "/repo/b")
    project = app.selected_project_summary

    assert project and project.ignored
    assert {w.id for w in app.workflows_for_project(project.directory)} == set()
    assert {w.id for w in app.workflows_for_project(project.directory, include_ignored=True)} == {
        "b"
    }
    assert any("b" in line for line in app.renderer.project_workflows(project, 100))


def test_ignored_zoom_project_opens_sessions_when_shown():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, directory="/repo/a"),
            workflow("b", "2026-06-02 12:00:00", cost=5, directory="/repo/b"),
        ]
    )
    app.ignored_projects = {"/repo/b"}
    app.show_ignored_projects = True
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Projects")
    app.project_index = next(
        i for i, p in enumerate(app.zoom_projects()) if p.directory == "/repo/b"
    )

    app.drill_in()

    assert app.zoom_project == "/repo/b"
    assert app.on_sessions_tab
    assert [w.id for w in app.current_sessions()] == ["b"]


def test_project_ignore_only_targets_navigable_project_lists():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, directory="/repo/a"),
            workflow("b", "2026-06-02 12:00:00", cost=5, directory="/repo/b"),
        ]
    )
    app.focus = "months"
    app.view = "browse"
    app.tab = app.month_tabs.index("Projects")  # right-side text table, no cursor

    assert app.handle_key(None, ord("i"))

    assert app.ignored_projects == set()
    assert "select a project" in app.notice


def test_hiding_ignored_projects_clears_ignored_zoom_target():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, directory="/repo/a"),
            workflow("b", "2026-06-02 12:00:00", cost=5, directory="/repo/b"),
        ]
    )
    app.ignored_projects = {"/repo/b"}
    app.show_ignored_projects = True
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Projects")
    app.project_index = next(
        i for i, p in enumerate(app.zoom_projects()) if p.directory == "/repo/b"
    )
    app.drill_in()
    assert app.current_sessions()[0].id == "b"

    app.handle_key(None, ord("I"))

    assert not app.show_ignored_projects
    assert app.zoom_project is None
    assert app.on_projects_tab


def test_ignored_projects_are_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/repo/a")])
    app.ignored_projects = {"/repo/a", "/repo/b"}
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00", directory="/repo/a")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    assert restored.ignored_projects == {"/repo/a", "/repo/b"}
    assert restored.all_workflows == []


def test_what_if_price_view_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.show_api_prices = True
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert not restored.show_api_prices
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    assert restored.show_api_prices


def test_calendar_granularity_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.cal_levels = ot.HEAT_MAX_LEVELS
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert restored.cal_levels == ot.HEAT_DEFAULT_LEVELS  # the default until restored
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    assert restored.cal_levels == ot.HEAT_MAX_LEVELS


def test_source_is_persisted_and_restored():
    with tempfile.TemporaryDirectory() as tmp:
        # make both sources "present" so the cycle is opencode / claude / all
        db = os.path.join(tmp, "opencode.db")
        open(db, "w").close()
        cdir = os.path.join(tmp, "projects", "slug")
        os.makedirs(cdir)
        _write_jsonl(
            os.path.join(cdir, "s.jsonl"),
            [_claude_msg("s", "claude-opus-4-8", _usage(1, 1, 0, 0), uuid="u", cwd=tmp)],
        )
        args = type(
            "Args",
            (),
            {
                "source": "auto",
                "db": db,
                "claude_dir": os.path.join(tmp, "projects"),
                "demo": False,
            },
        )()
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            app = app_with([workflow("a", "2026-06-01 12:00:00")])
            app.source_key = "all"
            ot.save_state(app)
            state = ot.load_state()
            assert state["source"] == "all"
            # auto restores the saved source when it's still available
            assert ot.resolve_source(args, state) == "all"
            # an explicit --source overrides the saved one
            args.source = "claude"
            assert ot.resolve_source(args, state) == "claude"
            # a saved source that's no longer available falls back to the default, which
            # merges every present source so you never need --source to see them together
            args.source = "auto"
            assert ot.resolve_source(args, {"source": "bogus"}) == "all"
            # demo merges too, and `c` can reach the merged view in demo
            args.demo = True
            assert "all" in ot.sources.source_cycle(args)
            assert ot.resolve_source(args, {}) == "all"
            args.demo = False
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


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


def test_reconcile_makes_models_sum_to_session_total():
    app = ot.App.__new__(ot.App)

    class _Store:
        demo = True

    app.store = _Store()
    app.loaded = [
        ot.Workflow(
            id="r",
            title="t",
            directory="d",
            created_at="2026-01-01",
            root_cost=0.0,
            total_cost=100.0,
            subagents=0,
            model_count=1,
            total_tokens=1000,
            unpriced_tokens=0,
        )
    ]
    app._model_by_root = {
        "r": [
            {
                "model_name": "m1",
                "runs": 1,
                "cost": 0.0,
                "tokens_total": 0,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
            },
        ]
    }
    app._reconcile_demo_models()
    rows = app._model_by_root["r"]
    assert round(sum(r["cost"] for r in rows), 2) == 100.0
    assert sum(r["tokens_total"] for r in rows) == 1000


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


def _write_opencode_db_with_tools(db):
    # Minimal OpenCode-shaped DB exercising the `part` table the Tools tab reads.
    # One subscription ($0) step calls TWO tools in parallel; one priced ($6) step
    # calls one tool. Token totals are chosen so even-split attribution is visible.
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table session (
          id text primary key, parent_id text, title text, directory text,
          time_created integer, cost real default 0 not null,
          tokens_input integer default 0 not null, tokens_output integer default 0 not null,
          tokens_reasoning integer default 0 not null, tokens_cache_read integer default 0 not null,
          tokens_cache_write integer default 0 not null
        );
        create table message (id text primary key, session_id text, data text);
        create table part (id text primary key, message_id text, session_id text, data text);
        """
    )
    conn.execute(
        "insert into session values (?,?,?,?,?,?,?,?,?,?,?)",
        ("s1", None, "Root", "/work/repo", 1760000000000, 6.0, 0, 0, 0, 0, 0),
    )
    conn.executemany(
        "insert into message values (?,?,?)",
        [
            (
                "m1",
                "s1",
                '{"role":"assistant","providerID":"anthropic","modelID":"claude-haiku-4.5",'
                '"cost":0,"tokens":{"input":2000000,"output":0}}',
            ),
            (
                "m2",
                "s1",
                '{"role":"assistant","providerID":"anthropic","modelID":"claude-haiku-4.5",'
                '"cost":6.0,"tokens":{"input":6000000,"output":0}}',
            ),
        ],
    )
    conn.executemany(
        "insert into part values (?,?,?,?)",
        [
            ("p1", "m1", "s1", '{"type":"step-start"}'),  # non-tool parts are ignored
            ("p2", "m1", "s1", '{"type":"tool","tool":"bash"}'),
            ("p3", "m1", "s1", '{"type":"tool","tool":"serena_read_file"}'),
            ("p4", "m2", "s1", '{"type":"tool","tool":"bash"}'),
        ],
    )
    conn.commit()
    conn.close()


def test_tool_namespace_classification():
    # Built-ins (even ones with underscores) fold to "(built-in)"; MCP/plugin tools
    # ("server_tool") roll up to their server prefix; anything else stands alone.
    assert ot.tool_namespace("bash") == "(built-in)"
    assert ot.tool_namespace("apply_patch") == "(built-in)"
    assert ot.tool_namespace("plan_exit") == "(built-in)"
    assert ot.tool_namespace("serena_find_symbol") == "serena"
    assert ot.tool_namespace("playwright_browser_navigate") == "playwright"
    assert ot.tool_namespace("standalone") == "standalone"


def test_tool_breakdown_even_splits_parallel_tool_calls():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert store.supports_tool_breakdown
        rows = {r["tool"]: r for r in store.tool_breakdown("s1")}
        # m1's 2M tokens split across its two tools -> 1M each; bash also gets m2's 6M.
        assert round(rows["bash"]["tokens_total"]) == 7_000_000
        assert round(rows["serena_read_file"]["tokens_total"]) == 1_000_000
        assert rows["bash"]["calls"] == 2
        assert rows["serena_read_file"]["calls"] == 1
        # Only the priced step carries real cost; it lands on bash, serena stays $0.
        assert rows["bash"]["cost"] == 6.0
        assert rows["serena_read_file"]["cost"] == 0
        # Attributed tokens reconcile to the tool-calling steps' totals (2M + 6M).
        assert round(sum(r["tokens_total"] for r in rows.values())) == 8_000_000


def test_tools_tab_offered_only_with_part_table():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        app = ot.App(ot.Store(db, type("A", (), {"demo": False})()), args())
        app.view = "session"
        # An OpenCode session offers both per-session tabs (Turns then Tools).
        assert app.current_tabs() == ("Overview", "Models", "Subagents", "Turns", "Tools")
    # A backend without the part table / support flag never shows the tab.
    bare = ot.App(FakeStore([]), args())
    bare.view = "session"
    assert "Tools" not in bare.current_tabs()
    assert "Turns" not in bare.current_tabs()


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
        assert "# Tools" in joined
        assert "# By server / namespace" in joined
        assert "(built-in)" in joined  # the server rollup labels built-in vs MCP
        # The subscription session records $0; under "$" the wholly-unpriced serena
        # row picks up its list-price estimate (1M Haiku input @ $1/M = $1.00).
        app.show_api_prices = True
        app._ensure_models()
        serena_line = next(
            line for line in rnd.detail_tools(wf, 92) if line.startswith("serena_read_file")
        )
        assert "$1.00" in serena_line


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


def _write_opencode_db_with_turns(db):
    # OpenCode-shaped DB for the Turns tab: a root session s1 with two assistant
    # messages and a subagent child s2 with one. Messages are inserted out of time
    # order (and carry $.time.created) so the timeline must sort them chronologically;
    # one priced ($3) step plus two $0 (subscription) steps exercise the "$" reprice.
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table session (
          id text primary key, parent_id text, title text, directory text, agent text,
          time_created integer
        );
        create table message (id text primary key, session_id text, data text);
        create table part (id text primary key, message_id text, session_id text, data text);
        """
    )
    conn.executemany(
        "insert into session values (?,?,?,?,?,?)",
        [
            ("s1", None, "Root", "/work/repo", None, 1760000000000),
            ("s2", "s1", "Explore", "/work/repo", "explore", 1760000000000),
        ],
    )

    def msg(model, cost, created, inp):
        return (
            f'{{"role":"assistant","providerID":"anthropic","modelID":"{model}",'
            f'"cost":{cost},"time":{{"created":{created}}},"tokens":{{"input":{inp},"output":0}}}}'
        )

    def user(created, title):
        return f'{{"role":"user","time":{{"created":{created}}},"summary":{{"title":"{title}"}}}}'

    conn.executemany(
        "insert into message values (?,?,?)",
        [
            # inserted last-first to prove the query orders by time, not rowid
            ("m2", "s1", msg("claude-sonnet-4-5", 3.0, 2000, 500000)),  # priced, t=2000
            ("m1", "s1", msg("claude-haiku-4.5", 0, 1000, 1000000)),  # $0, t=1000
            ("m3", "s2", msg("claude-haiku-4.5", 0, 1500, 2000000)),  # subagent $0, t=1500
            # two user prompts: u1 owns m1+m3 (t<=1500), u2 owns m2 (t=2000)
            ("u1", "s1", user(500, "Add feature X")),
            ("u2", "s1", user(1800, "Fix the bug")),
        ],
    )
    conn.commit()
    conn.close()


def test_message_timeline_orders_by_time_and_marks_subagent_turns():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert store.supports_turns("s1")
        rows = store.message_timeline("s1")
        # chronological (t=1000, 1500, 2000), NOT insertion order (m2,m1,m3)
        assert [r["tokens_total"] for r in rows] == [1_000_000, 2_000_000, 500_000]
        assert [r["cost"] for r in rows] == [0, 0, 3.0]
        # the middle turn is the subagent (depth 1, its session's agent label)
        assert [r["depth"] for r in rows] == [0, 1, 0]
        assert rows[1]["agent"] == "explore"
        assert rows[0]["agent"] == "-" and rows[2]["agent"] == "-"
        assert rows[1]["model_name"] == "anthropic/claude-haiku-4.5"
        # each turn is tagged with the user prompt that owns it (most recent in time):
        # u1 (summary.title) owns m1 + the subagent m3; u2 owns the later m2.
        assert [r["prompt_title"] for r in rows] == [
            "Add feature X",
            "Add feature X",
            "Fix the bug",
        ]
        assert rows[0]["prompt_id"] == "u1" and rows[2]["prompt_id"] == "u2"


def test_detail_turns_cumulative_and_reprices_under_dollar():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        app = ot.App(ot.Store(db, type("A", (), {"demo": False})()), args())
        rnd = ot.Renderer(app)
        wf = app.loaded[0]
        # Normal mode: only the priced step counts -> $3.00 total, ends at 100%.
        normal = rnd.detail_turns(wf, 96)
        joined = "\n".join(normal)
        assert normal[0] == "# Turns — 3 turns, $3.00 total"
        assert "! Grouped by the user prompt" in joined
        assert "$3.00 · 100%" in joined  # last turn's cumulative cell
        # turns are grouped under their owning user prompt (▸ header), m2 under u2
        assert "▸ Add feature X" in joined and "▸ Fix the bug" in joined
        # each row shows the date + clock ("MM-DD HH:MM:SS"), not just the time
        assert any(re.search(r"\d\d-\d\d \d\d:\d\d:\d\d", ln) for ln in normal)
        # Under "$" the two $0 haiku turns estimate at list price (1M+2M @ $1/M),
        # so the total grows to $1 + $2 + $3 = $6.00 and each shows its estimate.
        app.show_api_prices = True
        priced = rnd.detail_turns(wf, 96)
        assert priced[0] == "# Turns — 3 turns, $6.00 total"
        pjoined = "\n".join(priced)
        assert "$1.00" in pjoined and "$2.00" in pjoined and "$6.00 · 100%" in pjoined
        # the per-prompt subtotal sits on the group header (u1 = $1+$2 estimate = $3.00)
        assert "▸ Add feature X" in pjoined


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


def test_claude_message_timeline_orders_by_time_and_marks_sidechain():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        # main thread at :02, a sidechain (subagent) turn at :01 -> the sidechain must
        # sort first by time even though it's logged second, and be marked depth 1.
        main = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(100, 50, 0, 0),
            uuid="u0",
            cwd=cwd,
            ts="2026-06-10T18:46:02.000Z",
        )
        side = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(40, 10, 0, 0),
            uuid="u1",
            cwd=cwd,
            parent="u0",
            side=True,
            ts="2026-06-10T18:46:01.000Z",
        )
        _write_jsonl(os.path.join(root, "s1.jsonl"), [main, side])

        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        store.workflows()  # parse
        rows = store.message_timeline("s1")
        assert store.supports_turns("s1") is True
        assert [r["depth"] for r in rows] == [1, 0]  # sidechain (earlier) first
        assert rows[0]["agent"] == "subagent" and rows[1]["agent"] == "-"
        assert rows[0]["tokens_total"] == 50 and rows[1]["tokens_total"] == 150
        assert rows[0]["cost"] == 0.0 and rows[1]["cost"] == 0.0  # recorded; $ reprices
        assert rows[0]["time"] < rows[1]["time"]  # "HH:MM:SS" display, in order


def test_claude_message_timeline_groups_turns_by_owning_user_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")

        def user(text, ts, uuid):
            return {
                "type": "user",
                "sessionId": "s1",
                "cwd": cwd,
                "timestamp": ts,
                "uuid": uuid,
                "message": {"role": "user", "content": text},
            }

        # two prompts; each assistant turn belongs to the most recent earlier prompt
        rows_in = [
            user("first question", "2026-06-10T18:46:00.000Z", "ua"),
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(100, 50),
                uuid="a1",
                cwd=cwd,
                ts="2026-06-10T18:46:05.000Z",
            ),
            user("second question", "2026-06-10T18:47:00.000Z", "ub"),
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(20, 5),
                uuid="a2",
                cwd=cwd,
                ts="2026-06-10T18:47:05.000Z",
            ),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows_in)

        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        store.workflows()
        rows = store.message_timeline("s1")
        assert [r["prompt_title"] for r in rows] == ["first question", "second question"]
        assert rows[0]["prompt_id"] == "ua" and rows[1]["prompt_id"] == "ub"


def test_api_price_helpers():
    # input/output/cache priced per 1M, reasoning billed as output.
    assert "gpt-4o-2024-05-13" in ot.MODEL_PRICE_TABLE
    assert "claude-fable-5" in ot.MODEL_PRICE_TABLE
    assert "claude-sonnet-4-5" in ot.MODEL_PRICE_TABLE
    assert "gemini-2.5-pro" in ot.MODEL_PRICE_TABLE
    assert ot.model_price("openai/gpt-4o-2024-05-13") == (5.0, 15.0, 0.0, 0.0)  # exact table hit
    assert ot.model_price("anthropic/claude-fable-5") == (10.0, 50.0, 1.0, 12.5)
    assert ot.model_price("anthropic/claude-fable-5-20260613") == (10.0, 50.0, 1.0, 12.5)
    assert ot.model_price("github-copilot/claude-haiku-4.5") == (1.0, 5.0, 0.1, 1.25)
    assert ot.model_price("openai/o1-mini") == (1.1, 4.4, 0.55, 0.0)
    assert ot.model_price("openai/o1-preview") == (15.0, 60.0, 7.5, 0.0)
    assert ot.model_price("openai/gpt-5.2-xhigh")[:2] == (1.75, 14.0)  # variant suffix tolerated
    assert ot.model_price("unknown/future-model") == ot.FALLBACK_PRICE
    # 1M input + 1M output(+reasoning) of Haiku = $1 + $5 = $6.
    assert round(ot.api_equivalent_cost("x/claude-haiku-4.5", 1e6, 5e5, 5e5, 0, 0), 2) == 6.0


def test_local_providers_are_not_priced():
    # Local models run on your own hardware: there is no per-token API bill, so the
    # "$" what-if must leave them at $0 rather than inventing cloud list prices.
    for name in ("ollama/llama3.1:70b", "mlx/qwen2.5", "lmstudio/whatever", "local/foo"):
        assert ot.model_price(name) == (0.0, 0.0, 0.0, 0.0)
        assert ot.api_equivalent_cost(name, 5e6, 1e6, 0, 0, 0) == 0.0
    # the same model id behind a cloud provider is still priced
    assert ot.api_equivalent_cost("anthropic/claude-haiku-4.5", 1e6, 0, 0, 0, 0) > 0


def test_local_usage_stays_zero_in_what_if_view():
    # A local-only session: $0 recorded, real tokens. The "$" what-if must not turn
    # those tokens into cloud dollars.
    app = app_with([workflow("a", "2026-06-01 12:00:00", cost=0, directory="/x")])
    app._model_by_root = {
        "a": [
            {**_model_row("ollama/llama3.1", 0.0, 1_000_000), "input": 900_000, "output": 100_000}
        ]
    }
    app._compute_api_costs()
    app.show_api_prices = True
    app._apply_price_mode()
    assert app.loaded[0].api_total_cost == 0.0  # no invented spend
    ollama = next(ln for ln in app.renderer.trend_providers(80, 12) if ln.startswith("ollama"))
    assert "$0.00" in ollama


def test_price_table_marks_local_models():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {"a": [_model_row("ollama/llama3.1", 0.0, 1000)]}
    row = next(ln for ln in app.renderer.price_table_lines(80) if ln.startswith("ollama/llama3.1"))
    assert "local" in row and "0.00" not in row  # labelled local, no fake price


def test_api_price_toggle_prices_unpriced_usage():
    app = ot.App.__new__(ot.App)

    class _Store:
        demo = False

    app.store = _Store()
    app.show_api_prices = False
    app._models_loaded = True  # skip the deferred scan in toggle_api_prices
    app.loaded = [
        ot.Workflow(
            id="r",
            title="t",
            directory="d",
            created_at="2026-01-01",
            root_cost=10.0,
            total_cost=10.0,  # one model really billed $10
            subagents=0,
            model_count=2,
            total_tokens=0,
            unpriced_tokens=0,
        )
    ]
    app._snapshot_real_costs()

    def row(name, cost, inp):
        return {
            "model_name": name,
            "runs": 1,
            "cost": cost,
            "tokens_total": inp,
            "input": inp,
            "output": 0,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
        }

    # a real $10 row + a $0 subscription row that used 1M Haiku input tokens (=$1)
    app._model_by_root = {
        "r": [
            row("anthropic/claude-opus-4-6", 10.0, 0),
            row("github-copilot/claude-haiku-4.5", 0.0, 1_000_000),
        ]
    }
    app._compute_api_costs()

    assert app.loaded[0].total_cost == 10.0  # default view is actual cost
    app.toggle_api_prices()
    assert app.show_api_prices
    assert round(app.loaded[0].total_cost, 2) == 11.0  # real $10 + would-have-paid $1
    costs = {m["model_name"]: m["cost"] for m in app.model_mix("r")}
    assert costs["github-copilot/claude-haiku-4.5"] == 1.0  # priced from tokens
    assert costs["anthropic/claude-opus-4-6"] == 10.0  # real spend untouched
    app.toggle_api_prices()  # reversible
    assert not app.show_api_prices
    assert app.loaded[0].total_cost == 10.0
    assert (
        app.model_mix("r")
        and {m["model_name"]: m["cost"] for m in app.model_mix("r")}[
            "github-copilot/claude-haiku-4.5"
        ]
        == 0.0
    )


def test_api_price_toggle_prices_unpriced_part_of_mixed_model_row():
    app = ot.App.__new__(ot.App)

    class _Store:
        demo = False

    app.store = _Store()
    app.show_api_prices = False
    app._models_loaded = True
    app.loaded = [
        ot.Workflow(
            id="r",
            title="t",
            directory="d",
            created_at="2026-01-01",
            root_cost=10.0,
            total_cost=10.0,
            subagents=0,
            model_count=1,
            total_tokens=0,
            unpriced_tokens=0,
        )
    ]
    app._snapshot_real_costs()
    app._model_by_root = {
        "r": [
            {
                "model_name": "github-copilot/claude-haiku-4.5",
                "runs": 2,
                "cost": 10.0,  # one message was billed, one was subscription/credit
                "tokens_total": 2_000_000,
                "input": 2_000_000,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "unpriced_input": 1_000_000,
                "unpriced_output": 0,
                "unpriced_reasoning": 0,
                "unpriced_cache_read": 0,
                "unpriced_cache_write": 0,
            }
        ]
    }

    app._compute_api_costs()
    app.toggle_api_prices()

    assert round(app.loaded[0].total_cost, 2) == 11.0
    assert app.model_mix("r")[0]["cost"] == 11.0


def test_api_price_toggle_splits_root_and_subagent_unpriced_usage():
    app = ot.App.__new__(ot.App)

    class _Store:
        demo = False

    app.store = _Store()
    app.show_api_prices = False
    app._models_loaded = True
    app.loaded = [
        ot.Workflow(
            id="r",
            title="t",
            directory="d",
            created_at="2026-01-01",
            root_cost=0.0,
            total_cost=0.5,  # real spend happened only in a child session
            subagents=1,
            model_count=2,
            total_tokens=0,
            unpriced_tokens=0,
        )
    ]
    app._snapshot_real_costs()
    app._model_by_root = {
        "r": [
            {
                "model_name": "github-copilot/claude-haiku-4.5",
                "runs": 1,
                "cost": 0.0,
                "root_cost": 0.0,
                "tokens_total": 1_000_000,
                "input": 1_000_000,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "unpriced_input": 1_000_000,
                "unpriced_output": 0,
                "unpriced_reasoning": 0,
                "unpriced_cache_read": 0,
                "unpriced_cache_write": 0,
                "root_unpriced_input": 1_000_000,
                "root_unpriced_output": 0,
                "root_unpriced_reasoning": 0,
                "root_unpriced_cache_read": 0,
                "root_unpriced_cache_write": 0,
            },
            {
                "model_name": "openai/gpt-5-mini",
                "runs": 1,
                "cost": 0.5,
                "root_cost": 0.0,
                "tokens_total": 0,
                "input": 0,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "unpriced_input": 0,
                "unpriced_output": 0,
                "unpriced_reasoning": 0,
                "unpriced_cache_read": 0,
                "unpriced_cache_write": 0,
                "root_unpriced_input": 0,
                "root_unpriced_output": 0,
                "root_unpriced_reasoning": 0,
                "root_unpriced_cache_read": 0,
                "root_unpriced_cache_write": 0,
            },
        ]
    }

    app._compute_api_costs()
    app.toggle_api_prices()

    assert round(app.loaded[0].total_cost, 2) == 1.5
    assert round(app.loaded[0].root_cost, 2) == 1.0
    assert round(app.loaded[0].total_cost - app.loaded[0].root_cost, 2) == 0.5


def test_api_price_split_uses_store_root_unpriced_columns_for_same_model():
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
                ("root", None, "Root", "/tmp/project", 1760000000000, 0.0, 1_000_000, 0, 0, 0, 0),
                ("child", "root", "Child", "/tmp/project", 1760000001000, 0.5, 0, 1, 0, 0, 0),
            ],
        )
        conn.executemany(
            "insert into message values (?, ?)",
            [
                (
                    "root",
                    '{"role":"assistant","providerID":"github-copilot","modelID":"claude-haiku-4.5","cost":0,"tokens":{"input":1000000,"output":0}}',
                ),
                (
                    "child",
                    '{"role":"assistant","providerID":"github-copilot","modelID":"claude-haiku-4.5","cost":0.5,"tokens":{"input":0,"output":1}}',
                ),
            ],
        )
        conn.commit()
        conn.close()

        store = ot.Store(db, type("Args", (), {"demo": False})())
        app = ot.App(store, type("Args", (), {"since": None, "until": None, "days": None})())
        app._ensure_models()
        app.toggle_api_prices()

        assert round(app.loaded[0].total_cost, 2) == 1.5
        assert round(app.loaded[0].root_cost, 2) == 1.0
        assert round(app.loaded[0].total_cost - app.loaded[0].root_cost, 2) == 0.5


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


def test_drill_in_preserves_visible_sessions_tab():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.focus = "months"
    app.view = "browse"
    app.tab = app.month_tabs.index("Sessions")

    app.drill_in()

    assert app.view == "zoom"
    assert app.on_sessions_tab


def test_sort_only_changes_on_sessions_tab():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.focus = "months"
    app.view = "browse"
    app.tab = app.month_tabs.index("Models")
    app.sort_by = "cost"

    assert app.handle_key(None, ord("s"))
    assert app.sort_by == "cost"

    app.tab = app.month_tabs.index("Sessions")
    assert app.handle_key(None, ord("s"))
    assert app.sort_by == "tokens"


def test_shift_s_cycles_sort_backward():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.focus = "months"
    app.view = "browse"
    app.tab = app.month_tabs.index("Sessions")
    app.sort_by = "tokens"

    assert app.handle_key(None, ord("S"))
    assert app.sort_by == "cost"


def test_subagents_tab_is_sortable_by_tokens():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.view = "session"
    app.tab = app.workflow_tabs.index("Subagents")
    app.sort_by = "tokens"
    rows = [
        {
            "depth": 1,
            "agent": "b",
            "model_name": "m",
            "cost": 1.0,
            "tokens_total": 10,
            "title": "b",
        },
        {
            "depth": 1,
            "agent": "a",
            "model_name": "m",
            "cost": 1.0,
            "tokens_total": 20,
            "title": "a",
        },
    ]

    assert app.current_sort_options() == app.subagent_sort_options
    assert app.sorted_subagent_rows(rows)[0]["title"] == "a"


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


def test_filter_applies_to_projects():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", directory="/tmp/auth-service"),
            workflow("b", "2026-06-02 12:00:00", directory="/tmp/billing"),
            workflow("c", "2026-06-03 12:00:00", directory="/tmp/auth-ui"),
        ]
    )
    assert {p.directory for p in app.projects} == {
        "/tmp/auth-service",
        "/tmp/billing",
        "/tmp/auth-ui",
    }
    app.query = "auth"
    assert {p.directory for p in app.projects} == {"/tmp/auth-service", "/tmp/auth-ui"}
    # zoom-scoped project lists honor the filter too
    app.focus = "months"
    assert {p.directory for p in app.zoom_projects()} == {"/tmp/auth-service", "/tmp/auth-ui"}


def test_project_list_s_cycles_project_sort():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.set_browse_mode("projects")

    assert app.handle_key(None, ord("s"))
    assert app.project_sort_by == "tokens"
    assert app.sort_by == "cost"
    assert app.handle_key(None, ord("S"))
    assert app.project_sort_by == "cost"


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
    assert header.index("Subs") + len("Subs") == row.index("  0 subs") + len("  0 subs")
    assert len(header) <= 80
    assert len(row) <= 80


def test_project_mode_sessions_use_selected_project():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, directory="/tmp/a"),
            workflow("b", "2026-06-02 12:00:00", cost=5, directory="/tmp/b"),
        ]
    )
    app.set_browse_mode("projects")
    app.tab = app.project_tabs.index("Sessions")

    assert app.browse_mode == "projects"
    assert app.current_tabs() == app.project_tabs
    assert [w.id for w in app.current_sessions()] == ["b"]


def test_project_sessions_s_keeps_session_sort():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, directory="/tmp/a"),
            workflow("b", "2026-06-02 12:00:00", cost=5, directory="/tmp/a"),
        ]
    )
    app.set_browse_mode("projects")
    app.tab = app.project_tabs.index("Sessions")
    app.drill_in()

    assert app.handle_key(None, ord("s"))
    assert app.sort_by == "tokens"
    assert app.project_sort_by == "cost"


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

    assert "/tmp/a" in lines[2]
    assert "/tmp/b" in lines[3]
    assert all("/tmp/old" not in line for line in lines)
    assert app.handle_key(None, ord("s"))
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


def test_zoom_projects_tab_drills_into_scoped_sessions():
    app = app_with(
        [
            workflow("a1", "2026-06-01 12:00:00", cost=1, directory="/tmp/a"),
            workflow("a2", "2026-06-02 12:00:00", cost=2, directory="/tmp/a"),
            workflow("b1", "2026-06-03 12:00:00", cost=5, directory="/tmp/b"),
            workflow("old", "2026-05-01 12:00:00", cost=9, directory="/tmp/a"),
        ]
    )
    app.focus = "months"
    app.view = "browse"

    app.drill_in()  # browse -> month zoom
    assert app.view == "zoom"
    app.tab = app.month_tabs.index("Projects")

    # projects in scope are this month's only (no /tmp from May's "old")
    assert {p.directory for p in app.zoom_projects()} == {"/tmp/a", "/tmp/b"}

    # select /tmp/a (cost-sorted: b=5 first, a=3 second) and drill into its sessions
    app.project_index = [p.directory for p in app.zoom_projects()].index("/tmp/a")
    app.drill_in()

    assert app.zoom_project == "/tmp/a"
    assert app.on_sessions_tab
    assert {w.id for w in app.current_sessions()} == {"a1", "a2"}  # June /tmp/a only

    # Enter opens one of those sessions
    app.drill_in()
    assert app.view == "session"
    assert app.current_session().directory == "/tmp/a"

    # stepping back unwinds session -> project's sessions -> projects list -> browse
    app.drill_out()
    assert app.view == "zoom" and app.zoom_project == "/tmp/a" and app.on_sessions_tab
    app.drill_out()
    assert app.view == "zoom" and app.zoom_project is None and app.on_projects_tab
    app.drill_out()
    assert app.view == "browse"


def test_zoom_project_scope_clears_on_scope_change():
    app = app_with([workflow("a1", "2026-06-01 12:00:00", directory="/tmp/a")])
    app.focus = "months"
    app.drill_in()
    app.tab = app.month_tabs.index("Projects")
    app.drill_in()
    assert app.zoom_project == "/tmp/a"
    app.toggle_focus()  # flipping the months/days focus drops the project scope
    assert app.zoom_project is None


def test_project_sessions_drill_into_session():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/tmp/a")])
    app.set_browse_mode("projects")
    app.tab = app.project_tabs.index("Sessions")

    app.drill_in()
    app.drill_in()

    assert app.view == "session"
    assert app.current_session().id == "a"


def test_projects_drill_keeps_the_selected_project():
    # Regression: drilling into a non-first project must zoom into THAT project,
    # not reset the selection to projects[0].
    app = app_with(
        [
            workflow("x", "2026-06-01 12:00:00", cost=9, directory="/tmp/expensive"),
            workflow("y", "2026-06-02 12:00:00", cost=1, directory="/tmp/cheap"),
        ]
    )
    app.set_browse_mode("projects")
    app.project_index = 1  # cost-sorted: 0=/tmp/expensive, 1=/tmp/cheap
    assert app.selected_project_summary.directory == "/tmp/cheap"

    app.drill_in()

    assert app.view == "zoom"
    assert app.selected_project_summary.directory == "/tmp/cheap"


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


def test_p_and_t_switch_browse_modes_directly():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])

    assert app.handle_key(None, ord("p"))
    assert app.browse_mode == "projects"
    assert app.handle_key(None, ord("p"))
    assert app.browse_mode == "projects"
    assert app.handle_key(None, ord("t"))
    assert app.browse_mode == "time"


def test_filter_prompt_escape_cancels():
    value, done, cancelled = ot.App.filter_prompt_step("old", 27, 20)

    assert value == "old"
    assert not done
    assert cancelled


def test_filter_prompt_editing():
    value, done, cancelled = ot.App.filter_prompt_step("ho", ord("m"), 20)
    assert (value, done, cancelled) == ("hom", False, False)

    value, done, cancelled = ot.App.filter_prompt_step(value, 127, 20)
    assert (value, done, cancelled) == ("ho", False, False)

    value, done, cancelled = ot.App.filter_prompt_step(value, 10, 20)
    assert (value, done, cancelled) == ("ho", True, False)


def test_parse_range_text():
    # (days, months, since, until)
    assert ot.parse_range_text("all") == (None, None, None, None)
    assert ot.parse_range_text("30d") == (30, None, None, None)
    # months and years are calendar windows, not day approximations
    assert ot.parse_range_text("2m") == (None, 2, None, None)
    assert ot.parse_range_text("1y") == (None, 12, None, None)
    assert ot.parse_range_text("last 14 days") == (14, None, None, None)
    assert ot.parse_range_text("last 2 months") == (None, 2, None, None)
    assert ot.parse_range_text("2026") == (None, None, "2026-01-01", "2026-12-31")
    assert ot.parse_range_text("2026-05") == (None, None, "2026-05-01", "2026-05-31")
    assert ot.parse_range_text("2024-02") == (None, None, "2024-02-01", "2024-02-29")
    assert ot.parse_range_text("2026-05-01") == (None, None, "2026-05-01", None)
    assert ot.parse_range_text("2026-05-01..2026-05-31") == (
        None,
        None,
        "2026-05-01",
        "2026-05-31",
    )
    assert ot.parse_range_text("..2026-05-31") == (None, None, None, "2026-05-31")
    # a bare number is "N days"; a 4-digit value stays a calendar year
    assert ot.parse_range_text("30") == (30, None, None, None)
    assert ot.parse_range_text("7") == (7, None, None, None)
    assert ot.parse_range_text("2026") == (None, None, "2026-01-01", "2026-12-31")


def test_month_window_start_is_bucket_aligned():
    base = ot.datetime(2026, 6, 8)
    # "2m" = this month + last => starts at the first of last month (two buckets)
    assert ot.month_window_start(2, base) == "2026-05-01"
    assert ot.month_window_start(1, base) == "2026-06-01"  # just this month
    assert ot.month_window_start(12, base) == "2025-07-01"  # trailing twelve months
    # wraps across the year boundary
    assert ot.month_window_start(2, ot.datetime(2026, 1, 15)) == "2025-12-01"


def test_relative_month_range_round_trips():
    app = app_with([workflow("a", "2026-06-07 12:00:00")])
    app.set_range_from_text("2m")
    assert app.range_months == 2
    assert app.range_days is None
    assert app.range_input_value() == "2m"  # persisted form re-parses to the same window
    assert app.range_label() == "last 2 months"

    app.set_range_from_text("1y")  # a year is twelve calendar months
    assert app.range_months == 12
    assert app.range_input_value() == "12m"
    assert app.range_label() == "last 1 year"

    app.set_all_time()
    assert app.range_months is None


def test_parse_range_text_rejects_bad_input():
    for value in ("0d", "0m", "2026-13", "2026-02-31", "banana", "2026-06-01..2026-05-01"):
        try:
            ot.parse_range_text(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid range: {value}")


def test_set_range_from_text_preserves_selection():
    app = app_with(
        [
            workflow("june", "2026-06-01 12:00:00"),
            workflow("may", "2026-05-01 12:00:00"),
        ]
    )
    app.focus = "months"
    app.month_index = 1

    app.set_range_from_text("2026-05-01..2026-06-30")

    assert app.custom_since == "2026-05-01"
    assert app.custom_until == "2026-06-30"
    assert app.range_days is None
    assert app.selected_month_summary.month == "2026-05"


def test_set_all_time_preserves_current_month_selection():
    app = app_with(
        [
            workflow("june", "2026-06-01 12:00:00"),
            workflow("may", "2026-05-01 12:00:00"),
        ],
        since="2026-05-01",
    )
    app.focus = "months"
    app.month_index = 1

    app.set_all_time()

    assert app.selected_month_summary.month == "2026-05"


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


def test_export_disabled_in_demo_mode():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.store.demo = True
    app.export_current()
    assert "demo" in app.notice


def test_clear_filter_reports_when_nothing_to_clear():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert app.handle_key(None, ord("x"))
    assert app.notice == "no active filter"


def test_store_reads_db_without_session_token_columns():
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
              time_created integer
            );
            create table message (session_id text, data text);
            """
        )
        conn.executemany(
            "insert into session values (?, ?, ?, ?, ?)",
            [
                ("root", None, "Root", "/tmp/project", 1760000000000),
                ("child", "root", "Child", "/tmp/project", 1760000001000),
            ],
        )
        conn.executemany(
            "insert into message values (?, ?)",
            [
                (
                    "root",
                    '{"role":"assistant","providerID":"openai","modelID":"gpt-5-mini","cost":1.25,"tokens":{"total":10,"input":4,"output":6}}',
                ),
                (
                    "child",
                    '{"role":"assistant","providerID":"anthropic","modelID":"claude-sonnet-4.5","cost":0,"tokens":{"total":5,"input":2,"output":3}}',
                ),
            ],
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {"demo": False})()
        store = ot.Store(db, args)
        workflows = store.workflows()
        nodes = store.workflow_nodes("root")

        assert len(workflows) == 1
        assert workflows[0].total_cost == 1.25
        assert workflows[0].root_cost == 1.25
        assert workflows[0].total_tokens == 15
        assert workflows[0].unpriced_tokens == 5
        assert workflows[0].subagents == 1
        assert nodes[1]["tokens_total"] == 5
        assert nodes[1]["agent"] == "-"


def _claude_msg(
    session, model, usage, *, uuid, cwd, parent=None, side=False, mid=None, req=None, ts=None
):
    return {
        "type": "assistant",
        "sessionId": session,
        "cwd": cwd,
        "timestamp": ts or "2026-06-10T18:46:00.000Z",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": side,
        "requestId": req or (uuid + "-req"),
        "message": {
            "id": mid or (uuid + "-id"),
            "model": model,
            "role": "assistant",
            "usage": usage,
        },
    }


def _usage(inp=0, out=0, cr=0, cw=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
    }


def _write_jsonl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_claude_store_prices_tokens_dedupes_and_rolls_up_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        # cwd is <repo>/sub but the repo root (.git) is <repo> -> a session started
        # in a subdir must roll up to the repo, not the bare basename "sub".
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        m1 = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(1000, 500, 2000, 300),
            uuid="u1",
            cwd=sub,
            mid="m1",
            req="r1",
        )
        m2 = _claude_msg(
            "s1", "claude-opus-4-8", _usage(10, 20, 100, 0), uuid="u2", cwd=sub, mid="m2", req="r2"
        )
        dup = dict(m1)  # same (message.id, requestId) -> must be deduped, not double-counted
        _write_jsonl(os.path.join(root, "s1.jsonl"), [m1, dup, m2])

        args = type("Args", (), {"demo": False})()
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), args)
        workflows = store.workflows()

        assert len(workflows) == 1
        w = workflows[0]
        # tokens summed across the two distinct messages (dup ignored)
        assert w.total_tokens == (1000 + 500 + 2000 + 300) + (10 + 20 + 100)
        # recorded cost is $0 (Claude logs none); all of it is "unpriced" until $
        assert w.total_cost == 0.0 and w.root_cost == 0.0
        assert w.unpriced_tokens == w.total_tokens
        assert w.subagents == 0
        assert w.source == "Claude Code"
        assert w.directory == repo  # folded to the git root
        assert w.created_at.startswith("2026-06") and len(w.created_at) == 19

        rows = store.model_breakdown()
        assert len(rows) == 1
        r = rows[0]
        assert r["runs"] == 2  # dup deduped
        assert r["model_name"] == "anthropic/claude-opus-4-8"
        assert r["cost"] == 0.0
        # the unpriced split carries the full token counts so "$" can reprice them
        assert (r["unpriced_input"], r["unpriced_output"], r["unpriced_cache_read"]) == (
            1010,
            520,
            2100,
        )
        expected = ot.api_equivalent_cost("anthropic/claude-opus-4-8", 1010, 520, 0, 2100, 300)
        assert abs(expected - round(expected, 6)) < 1e-9 and expected > 0


def test_claude_store_groups_sidechain_subagents_into_tree():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        main = _claude_msg("s1", "claude-opus-4-8", _usage(100, 50, 0, 0), uuid="u0", cwd=cwd)
        # two sidechain messages chained off the main thread -> one subagent run
        s1 = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(40, 10, 0, 0),
            uuid="u1",
            cwd=cwd,
            parent="u0",
            side=True,
        )
        s2 = _claude_msg(
            "s1", "claude-opus-4-8", _usage(20, 5, 0, 0), uuid="u2", cwd=cwd, parent="u1", side=True
        )
        _write_jsonl(os.path.join(root, "s1.jsonl"), [main, s1, s2])

        args = type("Args", (), {"demo": False})()
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), args)
        w = store.workflows()[0]
        nodes = store.workflow_nodes("s1")

        assert w.subagents == 1  # the two sidechain msgs collapse to one run
        assert w.total_tokens == 150 + 50 + 25
        assert w.total_cost == 0.0 and w.root_cost == 0.0  # recorded cost; $ reprices

        # the root vs subagent split lives in the (un)priced token fields
        r = store.model_breakdown()[0]
        assert r["root_unpriced_input"] == 100  # main thread only
        assert r["unpriced_input"] == 100 + 40 + 20  # main + both sidechain msgs

        assert len(nodes) == 2
        assert nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[1]["depth"] == 1 and nodes[1]["agent"] == "subagent"
        assert nodes[1]["tokens_total"] == (40 + 10) + (20 + 5)
        assert nodes[0]["cost"] == 0.0 and nodes[1]["cost"] == 0.0  # recorded; $ reprices


def _claude_user(text, *, cwd, meta=False, side=False, uuid="u"):
    return {
        "type": "user",
        "sessionId": "s1",
        "cwd": cwd,
        "timestamp": "2026-06-10T18:46:00.000Z",
        "uuid": uuid,
        "isMeta": meta,
        "isSidechain": side,
        "message": {"role": "user", "content": text},
    }


def test_claude_title_skips_injected_command_and_meta_messages():
    # A session started by a slash command opens with Claude Code's injected
    # messages (meta caveat, <command-name> wrapper). With no ai-title, the title
    # must fall through to the first *real* user prompt, not the scaffolding.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        rows = [
            _claude_user("<local-command-caveat>Caveat: ...", cwd=repo, meta=True, uuid="u0"),
            _claude_user("<command-name>/clear</command-name>", cwd=repo, uuid="u1"),
            _claude_user("the real prompt about heat maps", cwd=repo, uuid="u2"),
            _claude_msg("s1", "claude-opus-4-8", _usage(10, 20, 30, 0), uuid="ua", cwd=repo),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        assert store.workflows()[0].title == "the real prompt about heat maps"


def test_claude_title_keeps_genuine_short_first_prompt():
    # When the only real user message is "ok" (a continuation/resume stub) and there
    # is no ai-title, opentab honestly shows "ok" rather than inventing a title.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        rows = [
            _claude_user("ok", cwd=repo, uuid="u0"),
            _claude_msg("s1", "claude-opus-4-8", _usage(10, 20, 30, 0), uuid="ua", cwd=repo),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        assert store.workflows()[0].title == "ok"


# --- Codex CLI rollout helpers (~/.codex/sessions/**/rollout-*.jsonl) ---------
CODEX_SID = "0199aa8e-1b9e-7912-bcd4-9b00c8733ea6"


def _codex_meta(sid, cwd, ts="2025-10-03T14:51:03.966Z"):
    return {
        "timestamp": ts,
        "type": "session_meta",
        "payload": {"id": sid, "timestamp": ts, "cwd": cwd, "git": {"branch": "main"}},
    }


def _codex_turn(model, cwd, ts="2025-10-03T14:51:10.000Z"):
    return {"timestamp": ts, "type": "turn_context", "payload": {"cwd": cwd, "model": model}}


def _codex_user(text, ts="2025-10-03T14:51:05.000Z"):
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text, "kind": "plain"},
    }


def _codex_tokens(inp, out, cached, total, ts="2025-10-03T14:51:20.000Z"):
    # A token_count event carrying the *cumulative* running total (Codex's shape).
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cached_input_tokens": cached,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total,
                }
            },
        },
    }


def _codex_rollout(root, sid, rows):
    # Codex files are named rollout-<ts>-<uuid>.jsonl; the uuid is the session id.
    _write_jsonl(os.path.join(root, f"rollout-2025-10-03T16-51-03-{sid}.jsonl"), rows)


def test_codex_store_dedupes_echo_attributes_models_and_rolls_up_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions", "2025", "10", "03")
        os.makedirs(root)
        # cwd is <repo>/sub but the repo root (.git) is <repo> -> must roll up.
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        # Two turns on gpt-5-codex, then one on gpt-5.5, each as a *cumulative* total.
        # Codex echoes the prior turn's count after each turn_context (equal total ->
        # must be skipped) and writes an info=null count first (no usage -> skipped).
        rows = [
            _codex_meta(CODEX_SID, sub),
            _codex_user("optimize the date formatter"),
            _codex_turn("gpt-5-codex", sub),
            {
                "timestamp": "t",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": None},
            },
            _codex_tokens(1000, 100, 800, 1100),  # turn 1 (delta = itself)
            _codex_turn("gpt-5-codex", sub),
            _codex_tokens(1000, 100, 800, 1100),  # echo of turn 1 -> skipped
            _codex_tokens(2200, 160, 1700, 2360),  # turn 2 (delta vs turn 1)
            _codex_turn("gpt-5.5", sub),
            _codex_tokens(2200, 160, 1700, 2360),  # echo of turn 2 -> skipped
            _codex_tokens(2700, 200, 1900, 2900),  # turn 3 on gpt-5.5
        ]
        _codex_rollout(root, CODEX_SID, rows)

        args = type("Args", (), {"demo": False})()
        store = ot.CodexStore(os.path.join(tmp, "sessions"), args)
        workflows = store.workflows()

        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == CODEX_SID
        assert w.title == "optimize the date formatter"  # first plain user message
        assert w.directory == repo  # folded to the git root, not the bare "sub"
        assert w.source == "Codex"
        assert w.subagents == 0  # Codex has no subagent tree
        assert w.total_cost == 0.0 and w.root_cost == 0.0  # recorded cost; $ reprices
        # the accepted deltas sum back to the final cumulative total (2900)
        assert w.total_tokens == 2900 and w.unpriced_tokens == 2900

        rows_out = {r["model_name"]: r for r in store.model_breakdown()}
        assert set(rows_out) == {"openai/gpt-5-codex", "openai/gpt-5.5"}  # provider-prefixed
        codex = rows_out["openai/gpt-5-codex"]
        assert codex["runs"] == 2  # the echo + null count did not inflate the count
        # OpenAI's input_tokens includes the cached read; we split it into uncached +
        # cache_read. turn1 (1000/800) + turn2 delta (1200/900): uncached 200+300=500.
        assert codex["unpriced_input"] == 500
        assert codex["unpriced_cache_read"] == 800 + 900
        assert codex["unpriced_output"] == 100 + 60
        # no subagents, so the root split equals the total split
        assert codex["root_unpriced_input"] == codex["unpriced_input"]
        five_five = rows_out["openai/gpt-5.5"]
        assert five_five["runs"] == 1
        assert (five_five["unpriced_input"], five_five["unpriced_cache_read"]) == (300, 200)

        # the (all-unpriced) usage reprices to a positive list-price estimate under $
        est = ot.api_equivalent_cost("openai/gpt-5-codex", 500, 160, 0, 1700, 0)
        assert est > 0

        # one flat depth-0 node; its model is the most-used one (gpt-5-codex, 2 runs)
        nodes = store.workflow_nodes(CODEX_SID)
        assert len(nodes) == 1
        assert nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["model_name"] == "openai/gpt-5-codex"
        assert nodes[0]["tokens_total"] == 2900  # root aggregates both models
        assert nodes[0]["cost"] == 0.0


def test_codex_title_takes_any_user_message_kind_and_collapses_newlines():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # Older rollouts omit "kind" on user_message; the title must still be picked up,
        # and a multi-line prompt (@file mentions) collapses to a single-line title.
        um = {
            "timestamp": "2025-10-03T14:51:05.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "fix\n@a.py:1\nthe bug"},
        }
        rows = [
            _codex_meta(CODEX_SID, cwd),
            um,
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(10, 5, 0, 15),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        assert store.workflows()[0].title == "fix @a.py:1 the bug"


def test_codex_store_treats_a_shrinking_total_as_a_compaction_reset():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # The running total grows, then *shrinks* (context compaction): the smaller
        # total is fresh post-reset usage, not a duplicate -- so it is counted, added
        # on top of the pre-reset peak.
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(1000, 100, 800, 1100),  # peak
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(400, 30, 100, 430),  # reset: fresh usage of (400,30)
        ]
        _codex_rollout(root, CODEX_SID, rows)

        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        w = store.workflows()[0]
        # pre-reset 1100 + post-reset 430 (the reset block counts in full)
        assert w.total_tokens == 1100 + 430
        r = store.model_breakdown()[0]
        assert r["runs"] == 2
        assert r["unpriced_input"] == (1000 - 800) + (400 - 100)  # uncached, both blocks
        assert r["unpriced_cache_read"] == 800 + 100


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
    assert any("cx  codex session" in ln for ln in lines)  # Src column abbreviation
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


def test_years_panel_groups_and_scopes_months_to_the_focused_year():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=2),
            workflow("b", "2026-05-01 12:00:00", cost=1),
            workflow("c", "2025-11-01 12:00:00", cost=4),
        ]
    )
    # An "All years" row leads, then the concrete years newest-first.
    assert [y.year for y in app.years] == [ot.ALL_YEARS, "2026", "2025"]

    # The middle panel shows only the focused year's months.
    app.year_index = next(i for i, y in enumerate(app.years) if y.year == "2026")
    assert [m.month for m in app.months] == ["2026-06", "2026-05"]
    app.year_index = next(i for i, y in enumerate(app.years) if y.year == "2025")
    assert [m.month for m in app.months] == ["2025-11"]

    # "All years" unscopes Months to every month across every year.
    app.year_index = next(i for i, y in enumerate(app.years) if y.year == ot.ALL_YEARS)
    assert app.focused_year is None
    assert [m.month for m in app.months] == ["2026-06", "2026-05", "2025-11"]


def test_all_years_row_omitted_with_a_single_year():
    # With one year an "All years" row would just mirror it, so it's not shown.
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00"),
            workflow("b", "2026-05-01 12:00:00"),
        ]
    )
    assert [y.year for y in app.years] == ["2026"]


def test_drilling_into_all_years_scopes_to_every_session():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=2),
            workflow("b", "2025-11-01 12:00:00", cost=4),
        ]
    )
    app.focus = "years"
    app.year_index = next(i for i, y in enumerate(app.years) if y.year == ot.ALL_YEARS)
    assert {w.id for w in app.zoom_scope_workflows()} == {"a", "b"}


def test_cycle_focus_keeps_the_active_tab_by_name():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=2),
            workflow("b", "2025-11-01 12:00:00", cost=4),
        ]
    )
    app.focus = "years"
    app.tab = app.current_tabs().index("Models")
    app.cycle_focus(1)  # years -> months
    assert app.focus == "months"
    assert app.current_tabs()[app.tab] == "Models"  # carried over
    app.cycle_focus(1)  # months -> days (which has no Models tab)
    assert app.focus == "days"
    assert app.current_tabs()[app.tab] == "Overview"  # graceful fallback


def test_default_opens_on_all_years_focused_on_current_month():
    from datetime import datetime

    now = datetime.now()
    cm = now.strftime("%Y-%m")
    # Multiple years -> open on "All years" (focused_year None) with the Months panel
    # focused, sitting on the current month.
    app = app_with(
        [
            workflow("a", f"{cm}-01 12:00:00"),  # this month
            workflow("b", f"{now.year - 1}-03-01 12:00:00"),  # a prior year
        ]
    )
    assert app.focus == "months"
    assert app.focused_year is None  # "All years"
    assert app.months[app.month_index].month == cm  # current month is the selection


def test_default_month_falls_back_to_newest_when_current_absent():
    from datetime import datetime

    py = datetime.now().year - 1
    app = app_with(
        [
            workflow("a", f"{py}-08-01 12:00:00"),
            workflow("b", f"{py - 1}-02-01 12:00:00"),
        ]
    )
    # Two years -> still "All years"; current month has no data, so the Months focus
    # falls back to the newest month overall.
    assert app.focused_year is None
    assert app.months[app.month_index].month == f"{py}-08"


def test_single_year_defaults_to_that_year():
    from datetime import datetime

    py = datetime.now().year - 2
    # One year -> no "All years" row; default lands on that year.
    older = app_with([workflow("a", f"{py}-03-01 12:00:00")])
    assert older.focused_year == str(py)


def test_tab_cycles_year_month_day_focus():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.focus = "years"
    app.cycle_focus(1)
    assert app.focus == "months"
    app.cycle_focus(1)
    assert app.focus == "days"
    app.cycle_focus(1)
    assert app.focus == "years"  # wraps
    app.cycle_focus(-1)
    assert app.focus == "days"  # Shift-Tab walks back


def test_moving_year_reanchors_months_and_changes_the_visible_months():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00"),
            workflow("b", "2025-11-01 12:00:00"),
        ]
    )
    app.focus = "years"
    app.year_index = next(i for i, y in enumerate(app.years) if y.year == "2026")
    app.month_index = 5  # deliberately stale
    app.move(1)  # step to the next (older) year
    assert app.focused_year == "2025"
    assert app.month_index == 0  # re-anchored when the year changed
    assert [m.month for m in app.months] == ["2025-11"]


def test_drilling_into_a_year_zooms_and_lists_its_sessions():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", title="recent"),
            workflow("b", "2026-02-01 12:00:00", title="older"),
            workflow("c", "2025-11-01 12:00:00", title="last year"),
        ]
    )
    app.focus = "years"
    app.year_index = next(i for i, y in enumerate(app.years) if y.year == "2026")
    app.drill_in()
    assert app.view == "zoom"
    lines = app.renderer.year_overview(app.selected_year_summary, 100)
    assert lines[0] == "# Yearly Insight"
    assert any("Year:" in ln and "2026" in ln for ln in lines)
    # The Sessions tab is scoped to the focused year (2026 sessions only).
    app.tab = app.current_tabs().index("Sessions")
    assert {w.id for w in app.current_sessions()} == {"a", "b"}


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


def test_codex_joins_the_source_cycle_and_builds_a_resume_command():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        open(db, "w").close()
        cdir = os.path.join(tmp, "claude", "slug")
        os.makedirs(cdir)
        _write_jsonl(
            os.path.join(cdir, "s.jsonl"),
            [_claude_msg("s", "claude-opus-4-8", _usage(1, 1, 0, 0), uuid="u", cwd=tmp)],
        )
        xdir = os.path.join(tmp, "codex", "2025")
        os.makedirs(xdir)
        _codex_rollout(
            xdir,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, tmp),
                _codex_turn("gpt-5-codex", tmp),
                _codex_tokens(10, 5, 0, 15),
            ],
        )
        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": db,
                "claude_dir": os.path.join(tmp, "claude"),
                "codex_dir": os.path.join(tmp, "codex"),
                "demo": False,
            },
        )()
        # all three present -> the cycle is opencode / claude / codex / all
        assert ot.available_sources(args) == ["opencode", "claude", "codex"]
        assert ot.sources.source_cycle(args) == ["opencode", "claude", "codex", "all"]
        # the c key walks through Codex on the way to the merged view
        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "claude"
        assert app.next_source_name() == "Codex"
        app.source_key = "codex"
        assert app.next_source_name() == "all"

        # L copies a `codex resume <id>` command for a Codex session
        wf = workflow("0199-id", "2026-06-01 12:00:00", title="t", directory="/tmp/proj")
        wf.source = "Codex"
        assert app.resume_command(wf) == "cd /tmp/proj && codex resume 0199-id"


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


def test_claude_shows_zero_in_normal_mode_and_estimate_under_dollar():
    with tempfile.TemporaryDirectory() as tmp:
        cdir = os.path.join(tmp, "projects", "slug")
        os.makedirs(cdir)
        msg = _claude_msg("s1", "claude-opus-4-8", _usage(1000, 500, 200, 50), uuid="u1", cwd=tmp)
        _write_jsonl(os.path.join(cdir, "s1.jsonl"), [msg])

        args = type(
            "Args",
            (),
            {"demo": False, "no_worktrees": True, "since": None, "until": None, "days": None},
        )()
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), args)
        app = ot.App(store, args)
        app._load_model_cache()  # the deferred per-model scan

        # Claude records no cost, so the app starts in the $ estimate view
        # (tokens repriced at list rates), not on a wall of $0.00
        assert app.show_api_prices
        expected = ot.api_equivalent_cost("anthropic/claude-opus-4-8", 1000, 500, 0, 200, 50)
        assert expected > 0
        assert abs(app.range_cost_total() - expected) < 1e-6
        # "$" flips to the recorded numbers: $0 (Claude logs none)
        app.toggle_api_prices()
        assert app.range_cost_total() == 0.0
        # and back to the estimate
        app.toggle_api_prices()
        assert abs(app.range_cost_total() - expected) < 1e-6
        # and the model mix reflects the same flip
        assert (
            app.model_mix("s1")[0]["cost"] == round(expected, 6)
            or abs(app.model_mix("s1")[0]["cost"] - expected) < 1e-6
        )


def test_no_recorded_cost_defaults_to_estimate_view():
    # A backend that records no dollars (Claude Code) would paint a wall of
    # $0.00 in normal mode, so the $ estimate view starts on by default...
    class SubscriptionStore(FakeStore):
        records_cost = False

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(SubscriptionStore([workflow("a", "2026-06-01 12:00:00")]), args)
    assert app.show_api_prices
    # ...but an explicit saved preference (user toggled $ off and quit) wins...
    ot.apply_state(app, args, {"show_api_prices": False})
    assert not app.show_api_prices
    # ...and cost-recording stores keep the real-cost default.
    assert not app_with([workflow("a", "2026-06-01 12:00:00")]).show_api_prices


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
    assert "Src" in lines[1]  # header gains the column
    assert any("oc  opencode session" in ln for ln in lines)
    assert any("cc  claude session" in ln for ln in lines)
    # Top Sessions in the overview carries the bracket tag instead
    over = app.renderer.month_overview(month, 120)
    assert any("[cc] claude session" in ln for ln in over)
    # single-source views stay untouched (origin is implied by the header chip)
    plain = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert "Src" not in plain.renderer.month_workflows(plain.months[0], 120)[1]


def test_unpriced_hint_matches_price_mode():
    # The hint teaches $ in normal mode and must not say "not billed" next to
    # nonzero estimated dollars in the $ view.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.show_api_prices = False
    assert "press $" in app.renderer.unpriced_hint()
    app.show_api_prices = True
    hint = app.renderer.unpriced_hint()
    assert "estimate" in hint and "press $" not in hint


def test_resume_command_cds_to_the_project_first():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/my project")
    a.source = "OpenCode"
    app = app_with([a])
    assert app.resume_command(a) == "cd '/repo/my project' && opencode --session ses_1"
    a.source = "Claude Code"
    assert app.resume_command(a) == "cd '/repo/my project' && claude --resume ses_1"
    # no command without a source stamp or a usable directory
    a.source = ""
    assert app.resume_command(a) is None
    a.source = "Claude Code"
    a.directory = "(unknown)"
    assert app.resume_command(a) is None


def test_tmux_launch_argv_builds_window_split_popup():
    cmd = "claude --resume abc123"
    # directory rides on -c / -d flags
    assert ot.tmux_launch_argv("window", "/repo/a", cmd) == [
        "tmux",
        "new-window",
        "-c",
        "/repo/a",
        cmd,
    ]
    assert ot.tmux_launch_argv("hsplit", "/repo/a", cmd)[:3] == ["tmux", "split-window", "-h"]
    assert ot.tmux_launch_argv("vsplit", "/repo/a", cmd)[:3] == ["tmux", "split-window", "-v"]
    popup = ot.tmux_launch_argv("popup", "/repo/a", cmd)
    assert popup[:3] == ["tmux", "display-popup", "-E"]
    assert "/repo/a" in popup and cmd in popup


def test_launcher_hook_detected_via_env_then_config():
    old_env = os.environ.get("OPENTAB_LAUNCHER")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            # nothing installed: no hook
            os.environ.pop("OPENTAB_LAUNCHER", None)
            os.environ["XDG_CONFIG_HOME"] = tmp
            assert ot.launcher_hook() is None
            # the well-known config path is picked up once executable
            hook = os.path.join(tmp, "opentab", "launcher")
            os.makedirs(os.path.dirname(hook))
            with open(hook, "w") as fh:
                fh.write("#!/bin/sh\n")
            assert ot.launcher_hook() is None  # not executable yet
            os.chmod(hook, 0o755)
            assert ot.launcher_hook() == hook
            # the env override wins over the config path
            override = os.path.join(tmp, "other")
            with open(override, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(override, 0o755)
            os.environ["OPENTAB_LAUNCHER"] = override
            assert ot.launcher_hook() == override
            # a bogus override falls through to the config path
            os.environ["OPENTAB_LAUNCHER"] = os.path.join(tmp, "missing")
            assert ot.launcher_hook() == hook
        finally:
            for key, val in (("OPENTAB_LAUNCHER", old_env), ("XDG_CONFIG_HOME", old_xdg)):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val


def test_tmux_launch_runs_the_hook_and_reports_its_stderr():
    old_env = os.environ.get("OPENTAB_LAUNCHER")
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "log")
        hook = os.path.join(tmp, "launcher")
        with open(hook, "w") as fh:
            fh.write(f'#!/bin/sh\nprintf "%s|%s|%s" "$1" "$2" "$3" > {log}\n')
        os.chmod(hook, 0o755)
        try:
            os.environ["OPENTAB_LAUNCHER"] = hook
            assert ot.util.tmux_launch("window", "/repo/a", "claude --resume x1") is None
            with open(log) as fh:
                assert fh.read() == "window|/repo/a|claude --resume x1"
            # a failing hook surfaces its stderr as the launch error
            with open(hook, "w") as fh:
                fh.write('#!/bin/sh\necho "no such kind" >&2\nexit 1\n')
            assert ot.util.tmux_launch("vsplit", "/repo/a", "claude --resume x1") == "no such kind"
        finally:
            if old_env is None:
                os.environ.pop("OPENTAB_LAUNCHER", None)
            else:
                os.environ["OPENTAB_LAUNCHER"] = old_env


def test_launch_menu_opens_in_tmux_and_copies_outside():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/a")
    a.source = "Claude Code"
    app = app_with([a])
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    old_tmux = os.environ.get("TMUX")
    real_launch, real_copy = ot.util.tmux_launch, ot.util.copy_to_clipboard
    launches, copies = [], []
    try:
        ot.util.tmux_launch = lambda kind, d, c: launches.append((kind, d, c)) or None
        ot.util.copy_to_clipboard = lambda v: copies.append(v) or True
        os.environ["TMUX"] = "/tmp/tmux-1/default,1,0"
        app.handle_key(None, ord("L"))
        assert app.launch_menu is not None and not launches  # menu open, nothing run
        app.handle_key(None, ord("w"))
        assert app.launch_menu is None
        assert launches == [("window", "/repo/a", "claude --resume ses_1")]
        # Esc cancels without launching
        app.handle_key(None, ord("L"))
        app.handle_key(None, 27)
        assert len(launches) == 1 and "cancelled" in app.notice
        # y inside the menu copies the cd-prefixed command
        app.handle_key(None, ord("L"))
        app.handle_key(None, ord("y"))
        assert copies == ["cd /repo/a && claude --resume ses_1"]
        # outside tmux, L skips the menu and copies directly
        os.environ.pop("TMUX")
        app.handle_key(None, ord("L"))
        assert app.launch_menu is None
        assert copies[-1] == "cd /repo/a && claude --resume ses_1" and len(copies) == 2
    finally:
        ot.util.tmux_launch = real_launch
        ot.util.copy_to_clipboard = real_copy
        if old_tmux is None:
            os.environ.pop("TMUX", None)
        else:
            os.environ["TMUX"] = old_tmux


def test_launch_menu_is_navigable_with_jk_and_enter():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/a")
    a.source = "Claude Code"
    app = app_with([a])
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    old_tmux = os.environ.get("TMUX")
    real_launch = ot.util.tmux_launch
    launches = []
    try:
        ot.util.tmux_launch = lambda kind, d, c: launches.append((kind, d, c)) or None
        os.environ["TMUX"] = "/tmp/tmux-1/default,1,0"
        app.handle_key(None, ord("L"))
        assert app.launch_menu is not None and app.launch_menu_index == 0  # starts at "window"
        app.handle_key(None, ord("j"))  # -> hsplit
        assert app.launch_menu_index == 1
        app.handle_key(None, ord("k"))  # back to window
        app.handle_key(None, ord("k"))  # wraps up to the last target (copy)
        assert app.launch_menu_index == 4
        app.handle_key(None, ord("j"))  # wraps back to window
        assert app.launch_menu_index == 0
        app.handle_key(None, ord("j"))  # -> hsplit
        app.handle_key(None, 10)  # Enter runs the highlighted target
        assert app.launch_menu is None
        assert launches == [("hsplit", "/repo/a", "claude --resume ses_1")]
    finally:
        ot.util.tmux_launch = real_launch
        if old_tmux is None:
            os.environ.pop("TMUX", None)
        else:
            os.environ["TMUX"] = old_tmux


def test_launch_only_works_on_session_contexts():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/a")
    a.source = "OpenCode"
    app = app_with([a])

    app.handle_key(None, ord("L"))
    assert app.launch_menu is None and app.notice == "launch works on sessions only"

    app.set_browse_mode("projects")
    app.handle_key(None, ord("L"))
    assert app.launch_menu is None and app.notice == "launch works on sessions only"


def test_next_source_name_names_the_destination():
    with tempfile.TemporaryDirectory() as tmp:
        # both sources present -> the cycle is opencode / claude / all
        db = os.path.join(tmp, "opencode.db")
        open(db, "w").close()
        cdir = os.path.join(tmp, "projects", "slug")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "s.jsonl"), "w") as fh:
            fh.write("{}\n")
        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": db,
                "claude_dir": os.path.join(tmp, "projects"),
                "demo": False,
            },
        )()
        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "opencode"
        assert app.next_source_name() == "Claude Code"
        app.source_key = "claude"
        assert app.next_source_name() == "all"
        app.source_key = "all"
        assert app.next_source_name() == "OpenCode"


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


def test_fuzzy_score_matches_subsequences():
    assert ot.fuzzy_score("", "anything") == 0  # empty query matches everything
    assert ot.fuzzy_score("otb", "opentab") is not None  # subsequence, not substring
    assert ot.fuzzy_score("xyz", "opentab") is None
    assert ot.fuzzy_score("TREND", "Trend view") is not None  # case-insensitive
    # tight matches outrank scattered ones
    assert ot.fuzzy_score("trend", "fix trend view") > ot.fuzzy_score(
        "trend", "travel reimbursement node"
    )
    # word starts outrank mid-word hits
    assert ot.fuzzy_score("tv", "trend view") > ot.fuzzy_score("tv", "octave")


def test_live_filter_ranks_best_fuzzy_match_first():
    # b would win the default cost sort; with a query the match quality decides.
    a = workflow("a", "2026-06-01 12:00:00", title="fix trends view", cost=1.0)
    b = workflow("b", "2026-06-02 12:00:00", title="travel reimbursement node", cost=50.0)
    c = workflow("c", "2026-06-03 12:00:00", title="unrelated", cost=99.0)
    app = app_with([a, b, c])
    app.query = "trend"
    rows = app.current_sessions()
    assert [w.id for w in rows] == ["a", "b"]  # both match; tight one first, c dropped
    app.query = ""
    assert [w.id for w in app.current_sessions()] == ["c", "b", "a"]  # cost sort returns


def test_f_enters_live_filter_mode():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", title="alpha"),
            workflow("b", "2026-06-02 12:00:00", title="beta"),
        ]
    )
    # "f" only filters where a session/project list is shown -- put it on a Sessions tab
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    assert app.can_filter_current_view()
    assert app.handle_key(None, ord("f")) and app.filter_active
    for ch in "bet":
        app.handle_key(None, ord(ch))
    assert app.query == "bet"  # edits apply live, no Enter needed
    assert [w.title for w in app.current_sessions()] == ["beta"]
    app.handle_key(None, 127)  # backspace
    assert app.query == "be"
    app.handle_key(None, 10)  # Enter keeps the filter and leaves the mode
    assert not app.filter_active and app.query == "be"
    # Esc restores the query from before `f`
    app.handle_key(None, ord("f"))
    app.handle_key(None, ord("x"))  # types into the query, doesn't clear the filter
    assert app.query == "bex"
    app.handle_key(None, 27)
    assert not app.filter_active and app.query == "be"
    # Ctrl-U clears the input while staying in the mode
    app.handle_key(None, ord("f"))
    app.handle_key(None, 21)
    assert app.filter_active and app.query == ""
    # q is text here, not quit; Ctrl-C still quits
    assert app.handle_key(None, ord("q")) and app.query == "q"
    assert app.handle_key(None, 3) is False


def test_f_is_a_noop_where_no_list_is_filtered():
    # The time-browse main view shows Months/Days, not a session/project list, so the
    # query would filter nothing -- "f" must not enter filter mode there, and the
    # footer must not advertise it (mirrors how "s/S sort" is gated).
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", title="alpha"),
            workflow("b", "2026-06-02 12:00:00", title="beta"),
        ]
    )
    assert app.view == "browse" and not app.can_filter_current_view()
    assert app.handle_key(None, ord("f")) and not app.filter_active  # consumed, but no-op
    assert "nothing to filter" in app.notice
    # on a Sessions tab it works again
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    assert app.can_filter_current_view()
    assert app.handle_key(None, ord("f")) and app.filter_active


def test_f_filters_the_models_tab_by_name():
    # "f" also narrows the Models tab, matching the query against the model name
    # (cost order preserved). Overview's Top Models stays unfiltered.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            {
                "model_name": "anthropic/claude-opus-4-6",
                "runs": 1,
                "cost": 5.0,
                "tokens_total": 10,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
            },
            {
                "model_name": "openai/gpt-5.3",
                "runs": 2,
                "cost": 3.0,
                "tokens_total": 20,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
            },
        ]
    }
    wf = app.all_workflows[0]
    r = app.renderer

    # The Models tab is now a filterable view.
    app.view = "session"
    app.tab = app.current_tabs().index("Models")
    assert app.on_models_tab and app.can_filter_current_view()

    # No query -> both models; a query keeps only the fuzzy matches.
    app.query = ""
    assert any("opus" in ln for ln in r.detail_models(wf, 120))
    app.query = "opus"
    lines = r.detail_models(wf, 120)
    assert any("opus" in ln for ln in lines) and not any("gpt-5.3" in ln for ln in lines)

    # A query that matches nothing gives a friendly empty message, not a bare header.
    app.query = "zzz"
    assert any("No models match" in ln for ln in r.detail_models(wf, 120))

    # Overview's Top Models is a different tab and is never filtered.
    app.query = "opus"
    assert any("gpt-5.3" in ln for ln in r.detail_overview(wf, 120))


# --- Hermes Agent database helpers (~/.hermes/state.db) ----------------------


def _hermes_db(path, rows):
    """Create a minimal Hermes state.db with only the columns HermesStore reads."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            cwd TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r["id"],
                r.get("title", r["id"]),
                r.get("model", "gpt-5"),
                r.get("cwd", ""),
                r.get("parent_id"),
                r.get("started_at", 1750000000.0),
                r.get("inp", 0),
                r.get("out", 0),
                r.get("cr", 0),
                r.get("cw", 0),
                r.get("reasoning", 0),
                r.get("archived", 0),
            )
            for r in rows
        ],
    )
    conn.commit()
    conn.close()


def _hermes_db_full(path, rows):
    """Hermes state.db superset that also carries the billing/cost columns
    (billing_provider, billing_mode, estimated_cost_usd, actual_cost_usd) so
    metered routes can be exercised. Mirrors the real ~/.hermes/state.db."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            cwd TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            archived INTEGER NOT NULL DEFAULT 0
        )"""
    )
    cols = (
        "id",
        "title",
        "model",
        "cwd",
        "parent_session_id",
        "started_at",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "billing_provider",
        "billing_mode",
        "estimated_cost_usd",
        "actual_cost_usd",
        "archived",
    )
    conn.executemany(
        f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [
            (
                r["id"],
                r.get("title", r["id"]),
                r.get("model", "gpt-5"),
                r.get("cwd", ""),
                r.get("parent_id"),
                r.get("started_at", 1750000000.0),
                r.get("inp", 0),
                r.get("out", 0),
                r.get("cr", 0),
                r.get("cw", 0),
                r.get("reasoning", 0),
                r.get("provider"),
                r.get("billing_mode"),
                r.get("estimated_cost_usd"),
                r.get("actual_cost_usd"),
                r.get("archived", 0),
            )
            for r in rows
        ],
    )
    conn.commit()
    conn.close()


def test_hermes_store_loads_tokens_and_rolls_up_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        _hermes_db(
            db,
            [
                {
                    "id": "sess1",
                    "title": "Fix the bug",
                    "model": "gpt-5.5",
                    "cwd": sub,
                    "started_at": 1750000000.0,
                    "inp": 1000,
                    "out": 500,
                    "cr": 200,
                    "cw": 50,
                }
            ],
        )

        args = type("Args", (), {"demo": False})()
        store = ot.HermesStore(db, args)
        workflows = store.workflows()

        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == "sess1"
        assert w.title == "Fix the bug"
        assert w.directory == repo  # folded to git root, not bare "sub"
        assert w.source == "Hermes"
        assert w.subagents == 0
        assert w.total_cost == 0.0 and w.root_cost == 0.0  # subscription; $ reprices
        assert w.total_tokens == 1000 + 500 + 200 + 50
        assert w.unpriced_tokens == w.total_tokens
        assert len(w.created_at) == 19  # YYYY-MM-DD HH:MM:SS

        rows = store.model_breakdown()
        assert len(rows) == 1
        r = rows[0]
        assert r["root_id"] == "sess1"
        assert r["model_name"] == "openai/gpt-5.5"  # provider-prefixed
        assert r["cost"] == 0.0
        assert r["tokens_total"] == 1750
        assert r["unpriced_input"] == 1000
        assert r["unpriced_output"] == 500
        assert r["unpriced_cache_read"] == 200
        assert r["unpriced_cache_write"] == 50
        # no subagents -> root_unpriced_* equals the total
        assert r["root_unpriced_input"] == 1000
        assert r["root_unpriced_output"] == 500

        nodes = store.workflow_nodes("sess1")
        assert len(nodes) == 1
        assert nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["model_name"] == "openai/gpt-5.5"
        assert nodes[0]["tokens_total"] == 1750
        assert nodes[0]["cost"] == 0.0

        # tokens are unpriced -> list-price estimate under $ is positive
        est = ot.api_equivalent_cost("openai/gpt-5.5", 1000, 500, 0, 200, 50)
        assert est > 0


def test_hermes_store_rolls_child_session_into_parent_subtotal():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "project")
        os.makedirs(cwd)
        _hermes_db(
            db,
            [
                # root session
                {
                    "id": "root1",
                    "title": "Root task",
                    "model": "gpt-5.5",
                    "cwd": cwd,
                    "started_at": 1750000000.0,
                    "inp": 100,
                    "out": 50,
                    "cr": 0,
                    "cw": 0,
                },
                # child session (subagent)
                {
                    "id": "child1",
                    "title": "Subagent run",
                    "model": "gpt-5.5",
                    "cwd": cwd,
                    "parent_id": "root1",
                    "started_at": 1750001000.0,
                    "inp": 400,
                    "out": 200,
                    "cr": 100,
                    "cw": 0,
                },
            ],
        )

        args = type("Args", (), {"demo": False})()
        store = ot.HermesStore(db, args)
        workflows = store.workflows()

        # only the root surfaces as a top-level workflow
        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == "root1"
        assert w.subagents == 1
        # total = root (100+50) + child (400+200+100) = 850
        assert w.total_tokens == 850
        assert w.unpriced_tokens == 850

        # model_breakdown: root_unpriced_* is the root's own tokens only
        rows = store.model_breakdown()
        assert len(rows) == 1
        r = rows[0]
        assert r["tokens_total"] == 850
        assert r["unpriced_input"] == 100 + 400  # root + child
        assert r["root_unpriced_input"] == 100  # root only
        assert r["unpriced_output"] == 50 + 200
        assert r["root_unpriced_output"] == 50

        # workflow_nodes: depth-0 root + depth-1 child
        nodes = store.workflow_nodes("root1")
        assert len(nodes) == 2
        root_node, child_node = nodes
        assert root_node["depth"] == 0 and root_node["agent"] == "-"
        assert root_node["tokens_total"] == 150  # root's own tokens only
        assert child_node["depth"] == 1 and child_node["agent"] == "subagent"
        assert child_node["tokens_total"] == 700  # child's tokens
        assert child_node["title"] == "Subagent run"
        assert root_node["cost"] == 0.0 and child_node["cost"] == 0.0


def test_hermes_store_rolls_grandchild_session_into_subtotal():
    """Depth-2+ sessions must be included in aggregate totals and node list."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "project")
        os.makedirs(cwd)
        _hermes_db(
            db,
            [
                {"id": "root1", "model": "gpt-5", "cwd": cwd, "inp": 10, "out": 5},
                {
                    "id": "child1",
                    "parent_id": "root1",
                    "model": "gpt-5",
                    "cwd": cwd,
                    "inp": 20,
                    "out": 10,
                },
                {
                    "id": "grand1",
                    "parent_id": "child1",
                    "model": "gpt-5",
                    "cwd": cwd,
                    "inp": 40,
                    "out": 20,
                },
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        workflows = store.workflows()
        assert len(workflows) == 1
        w = workflows[0]
        assert w.total_tokens == 10 + 5 + 20 + 10 + 40 + 20  # all three sessions
        assert w.subagents == 2  # child + grandchild

        rows = store.model_breakdown()
        assert len(rows) == 1
        assert rows[0]["tokens_total"] == w.total_tokens

        nodes = store.workflow_nodes("root1")
        assert len(nodes) == 3
        assert nodes[0]["depth"] == 0
        assert nodes[1]["depth"] == 1
        assert nodes[2]["depth"] == 2


def test_hermes_store_splits_model_rows_by_child_model():
    """Tokens from a child using a different model must appear in a separate model_row."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "project")
        os.makedirs(cwd)
        _hermes_db(
            db,
            [
                {"id": "root1", "model": "gpt-5", "cwd": cwd, "inp": 100, "out": 50},
                {
                    "id": "child1",
                    "parent_id": "root1",
                    "model": "gpt-4o",
                    "cwd": cwd,
                    "inp": 200,
                    "out": 100,
                },
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        rows = store.model_breakdown()
        assert len(rows) == 2  # one row per distinct model
        by_model = {r["model_name"]: r for r in rows}
        assert "openai/gpt-5" in by_model
        assert "openai/gpt-4o" in by_model

        gpt5 = by_model["openai/gpt-5"]
        assert gpt5["unpriced_input"] == 100
        assert gpt5["root_unpriced_input"] == 100  # root session used this model

        gpt4o = by_model["openai/gpt-4o"]
        assert gpt4o["unpriced_input"] == 200
        assert gpt4o["root_unpriced_input"] == 0  # root did not use gpt-4o


def test_hermes_store_excludes_archived_sessions():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(
            db,
            [
                {"id": "live", "inp": 100, "out": 50},
                {"id": "archived", "inp": 200, "out": 100, "archived": 1},
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        ids = {w.id for w in store.workflows()}
        assert ids == {"live"}
        assert "archived" not in ids


def test_hermes_metered_session_uses_recorded_cost():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "proj")
        os.makedirs(cwd)
        _hermes_db_full(
            db,
            [
                {
                    "id": "m1",
                    "model": "claude-sonnet-4",
                    "provider": "anthropic",  # billing_provider -> display prefix
                    "billing_mode": "official_docs_snapshot",
                    "cwd": cwd,
                    "inp": 1000,
                    "out": 500,
                    "cr": 200,
                    "cw": 50,
                    "estimated_cost_usd": 0.12,
                    "actual_cost_usd": 0.34,  # reconciled actual is preferred
                }
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert store.records_cost is True  # a metered session exists

        w = store.workflows()[0]
        assert w.total_cost == 0.34 and w.root_cost == 0.34
        assert w.unpriced_tokens == 0  # priced -> "$" must not reprice it
        assert w.total_tokens == 1750

        r = store.model_breakdown()[0]
        assert r["model_name"] == "anthropic/claude-sonnet-4"  # from billing_provider
        assert r["cost"] == 0.34 and r["root_cost"] == 0.34
        assert r["input"] == 1000 and r["tokens_total"] == 1750  # tokens still in full
        assert r["unpriced_input"] == 0 and r["root_unpriced_input"] == 0

        assert store.workflow_nodes("m1")[0]["cost"] == 0.34


def test_hermes_estimated_cost_used_when_actual_absent():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "proj")
        os.makedirs(cwd)
        _hermes_db_full(
            db,
            [
                {
                    "id": "e1",
                    "model": "gpt-5.5",
                    "provider": "openrouter",
                    "cwd": cwd,
                    "inp": 1000,
                    "out": 500,
                    "estimated_cost_usd": 0.21,
                    "actual_cost_usd": None,
                }
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert store.records_cost is True
        w = store.workflows()[0]
        assert w.total_cost == 0.21  # falls back to estimated_cost_usd
        assert w.unpriced_tokens == 0


def test_hermes_subscription_session_stays_unpriced():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "proj")
        os.makedirs(cwd)
        _hermes_db_full(
            db,
            [
                {
                    "id": "s1",
                    "model": "gpt-5.5",
                    "provider": "openai-codex",
                    "billing_mode": "subscription_included",
                    "cwd": cwd,
                    "inp": 1000,
                    "out": 500,
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": None,
                }
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert store.records_cost is False  # no recorded cost anywhere

        w = store.workflows()[0]
        assert w.total_cost == 0.0
        assert w.unpriced_tokens == 1500  # all tokens are unpriced -> "$" estimates them

        r = store.model_breakdown()[0]
        assert r["model_name"] == "openai/gpt-5.5"  # openai-codex -> openai
        assert r["cost"] == 0.0 and r["unpriced_input"] == 1000


def test_hermes_mixed_subscription_and_metered():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "proj")
        os.makedirs(cwd)
        _hermes_db_full(
            db,
            [
                {
                    "id": "sub",
                    "model": "gpt-5.5",
                    "provider": "openai-codex",
                    "cwd": cwd,
                    "inp": 1000,
                    "out": 500,
                    "estimated_cost_usd": 0.0,
                },
                {
                    "id": "paid",
                    "model": "claude-opus-4",
                    "provider": "anthropic",
                    "cwd": cwd,
                    "inp": 2000,
                    "out": 800,
                    "actual_cost_usd": 1.50,
                },
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert store.records_cost is True  # at least one metered session

        by_id = {w.id: w for w in store.workflows()}
        assert by_id["sub"].total_cost == 0.0 and by_id["sub"].unpriced_tokens == 1500
        assert by_id["paid"].total_cost == 1.50 and by_id["paid"].unpriced_tokens == 0


def test_hermes_subtree_prices_each_session_independently():
    """A metered root with a $0 subscription child: only the child stays unpriced."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "proj")
        os.makedirs(cwd)
        _hermes_db_full(
            db,
            [
                {
                    "id": "root",
                    "model": "claude-opus-4",
                    "provider": "anthropic",
                    "cwd": cwd,
                    "inp": 100,
                    "out": 50,
                    "actual_cost_usd": 0.40,
                },
                {
                    "id": "child",
                    "parent_id": "root",
                    "model": "gpt-5.5",
                    "provider": "openai-codex",
                    "cwd": cwd,
                    "inp": 400,
                    "out": 200,
                    "estimated_cost_usd": 0.0,
                },
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        w = store.workflows()[0]
        assert w.id == "root"
        assert w.total_cost == 0.40  # root metered + child $0
        assert w.root_cost == 0.40
        assert w.unpriced_tokens == 600  # only the subscription child's tokens

        rows = {r["model_name"]: r for r in store.model_breakdown()}
        assert rows["anthropic/claude-opus-4"]["cost"] == 0.40
        assert rows["anthropic/claude-opus-4"]["unpriced_input"] == 0
        assert rows["openai/gpt-5.5"]["cost"] == 0.0
        assert rows["openai/gpt-5.5"]["unpriced_input"] == 400

        nodes = store.workflow_nodes("root")
        assert nodes[0]["cost"] == 0.40  # root node
        assert nodes[1]["cost"] == 0.0  # subscription child node


def test_hermes_tolerates_minimal_schema():
    """A Hermes version missing optional columns must not crash (schema-adaptive)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        conn = sqlite3.connect(db)
        # No cwd / parent / started_at / cache / billing / cost / archived columns.
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT,"
            " input_tokens INTEGER, output_tokens INTEGER)"
        )
        conn.execute("INSERT INTO sessions VALUES ('a', 'gpt-5', 100, 50)")
        conn.commit()
        conn.close()

        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert store.records_cost is False  # no cost columns -> not cost-recording

        workflows = store.workflows()
        assert len(workflows) == 1
        w = workflows[0]
        assert w.total_tokens == 150
        assert w.total_cost == 0.0
        assert w.directory == "(unknown)"  # no cwd column

        r = store.model_breakdown()[0]
        assert r["model_name"] == "openai/gpt-5"  # inferred from bare model name
        assert r["tokens_total"] == 150


def test_hermes_joins_the_source_cycle_and_builds_a_resume_command():
    with tempfile.TemporaryDirectory() as tmp:
        oc_db = os.path.join(tmp, "opencode.db")
        open(oc_db, "w").close()
        hermes_db = os.path.join(tmp, "hermes_state.db")
        cwd = os.path.join(tmp, "project")
        os.makedirs(cwd)
        _hermes_db(hermes_db, [{"id": "h1", "inp": 100, "cwd": cwd}])

        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": oc_db,
                "claude_dir": os.path.join(tmp, "no-claude"),
                "codex_dir": os.path.join(tmp, "no-codex"),
                "hermes_db": hermes_db,
                "demo": False,
            },
        )()

        assert ot.available_sources(args) == ["opencode", "hermes"]
        assert ot.sources.source_cycle(args) == ["opencode", "hermes", "all"]

        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "opencode"
        assert app.next_source_name() == "Hermes"
        app.source_key = "hermes"
        assert app.next_source_name() == "all"

        wf = workflow("h1-sess", "2026-06-01 12:00:00", title="t", directory="/tmp/proj")
        wf.source = "Hermes"
        assert app.resume_command(wf) == "cd /tmp/proj && hermes --resume h1-sess"


# --- CSV adapter (a CSV of logged API requests, e.g. GitHub Copilot) ---------


def _write_csv(path, header, rows):
    import csv as _csv

    with open(path, "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def _csv_args():
    return type("Args", (), {"demo": False})()


def test_csv_store_splits_cache_prefixes_providers_and_stays_unpriced():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "copilot.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "cached_tokens", "session_id"],
            [
                # input_tokens includes the cached read (OpenAI style) -> uncached 8000
                ["2026-06-18T10:00:00Z", "claude-sonnet-4", 12000, 800, 4000, "s1"],
                ["2026-06-18T10:05:00Z", "gpt-4o", 5000, 300, 0, "s1"],
                ["2026-06-17T09:00:00Z", "gemini-2.5-pro", 2000, 150, 0, "s2"],
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        assert store.records_cost is False  # no cost column -> subscription-style
        workflows = store.workflows()
        assert {w.id for w in workflows} == {"s1", "s2"}
        s1 = next(w for w in workflows if w.id == "s1")
        assert s1.source == "CSV"
        assert s1.subagents == 0  # CSV has no subagent tree
        assert s1.total_cost == 0.0  # recorded cost is $0
        assert s1.total_tokens == s1.unpriced_tokens == 12800 + 5300  # all unpriced

        rows = {r["model_name"]: r for r in store.model_breakdown() if r["root_id"] == "s1"}
        # mixed providers each get the right prefix so pricing + the Providers tab work
        assert set(rows) == {"anthropic/claude-sonnet-4", "openai/gpt-4o"}
        cl = rows["anthropic/claude-sonnet-4"]
        assert cl["input"] == 8000 and cl["cache_read"] == 4000  # cached split out of input
        assert cl["unpriced_input"] == 8000 and cl["unpriced_cache_read"] == 4000

        # the "$" what-if reprices the unpriced tokens at list price (non-zero)
        est = ot.api_equivalent_cost(
            cl["model_name"],
            cl["unpriced_input"],
            cl["unpriced_output"],
            cl["unpriced_reasoning"],
            cl["unpriced_cache_read"],
            cl["unpriced_cache_write"],
        )
        assert est > 0

        # one flat depth-0 node aggregating both of s1's models
        nodes = store.workflow_nodes("s1")
        assert len(nodes) == 1 and nodes[0]["depth"] == 0
        assert nodes[0]["tokens_input"] == 13000  # 8000 + 5000 uncached
        assert nodes[0]["tokens_total"] == 18100
        assert nodes[0]["cost"] == 0.0  # _priced_nodes reprices a $0 node under "$"


def test_csv_groups_by_day_and_project_when_no_session_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "copilot.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "project"],
            [
                ["2026-06-18T10:00:00Z", "gpt-4o", 100, 10, "alpha"],
                ["2026-06-18T11:00:00Z", "gpt-4o", 200, 20, "alpha"],  # same day+project
                ["2026-06-18T12:00:00Z", "gpt-4o", 50, 5, "beta"],  # different project
                ["2026-06-19T09:00:00Z", "gpt-4o", 70, 7, "alpha"],  # different day
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        workflows = store.workflows()
        # one synthetic session per (date, project): (18,alpha) (18,beta) (19,alpha)
        assert len(workflows) == 3
        alpha18 = next(w for w in workflows if w.directory == "alpha" and "06-18" in w.created_at)
        assert alpha18.total_tokens == 100 + 10 + 200 + 20  # both rows folded together


def test_csv_credits_column_prices_as_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "copilot.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "credits", "session_id"],
            [
                ["2026-06-18T10:00:00Z", "claude-opus-4-5", 10000, 2000, 150, "m1"],
                ["2026-06-18T10:10:00Z", "claude-opus-4-5", 3000, 500, 40, "m1"],
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        assert store.records_cost is True  # a populated cost column -> metered
        w = store.workflows()[0]
        assert w.total_cost == round((150 + 40) * 0.01, 6)  # credits x $0.01 = $1.90
        assert w.unpriced_tokens == 0  # metered rows are not re-estimated under "$"
        row = store.model_breakdown()[0]
        assert row["unpriced_input"] == 0 and row["unpriced_output"] == 0


def test_csv_tolerates_header_aliases_and_epoch_timestamps():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "copilot.csv")
        # alias headers (Time / Model Name / Input / Output) and an epoch-seconds stamp
        _write_csv(
            path,
            ["Time", "Model Name", "Input", "Output", "session"],
            [["1750240800", "gpt-4o", 1000, 100, "e1"]],
        )
        store = ot.CsvStore(path, _csv_args())
        w = store.workflows()[0]
        assert w.id == "e1"
        assert w.created_at.startswith("2025-06-18")  # epoch parsed
        assert w.total_tokens == 1100
        assert store.model_breakdown()[0]["model_name"] == "openai/gpt-4o"


def test_csv_tolerates_missing_empty_and_garbage_files():
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "nope.csv")
        store = ot.CsvStore(missing, _csv_args())
        assert store.records_cost is False
        assert store.workflows() == []  # never crashes on a missing file

        empty = os.path.join(tmp, "empty.csv")
        open(empty, "w").close()
        assert ot.CsvStore(empty, _csv_args()).workflows() == []

        garbage = os.path.join(tmp, "garbage.csv")
        _write_csv(garbage, ["a", "b", "c"], [["1", "2", "3"]])  # no usable columns
        assert ot.CsvStore(garbage, _csv_args()).workflows() == []


def test_csv_joins_the_source_cycle_and_has_no_resume_command():
    with tempfile.TemporaryDirectory() as tmp:
        oc_db = os.path.join(tmp, "opencode.db")
        open(oc_db, "w").close()
        csv_path = os.path.join(tmp, "requests.csv")
        _write_csv(
            csv_path,
            ["timestamp", "model", "input_tokens", "output_tokens"],
            [["2026-06-18T10:00:00Z", "gpt-4o", 100, 10]],
        )
        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": oc_db,
                "claude_dir": os.path.join(tmp, "no-claude"),
                "codex_dir": os.path.join(tmp, "no-codex"),
                "hermes_db": os.path.join(tmp, "no-hermes.db"),
                "csv": csv_path,
                "demo": False,
            },
        )()
        assert ot.available_sources(args) == ["opencode", "csv"]
        assert ot.sources.source_cycle(args) == ["opencode", "csv", "all"]
        # no saved pref and >=2 sources present -> auto merges them (no --source needed)
        assert ot.resolve_source(args, {}) == "all"
        store, _ = ot.sources.make_store(args, "csv")
        assert isinstance(store, ot.CsvStore)

        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "opencode"
        assert app.next_source_name() == "CSV"

        # A CSV/Copilot source has no CLI resume, so L produces no command (never crashes)
        wf = workflow("s1", "2026-06-01 12:00:00", title="t", directory="/tmp/proj")
        wf.source = "CSV"
        assert app.resume_command(wf) is None


def _parse(argv):
    import sys as _sys

    old = _sys.argv
    _sys.argv = ["opentab"] + list(argv)
    try:
        return ot.parse_args()
    finally:
        _sys.argv = old


def test_path_and_csv_flag_both_select_the_csv_source():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "requests.csv")
        _write_csv(
            csv_path,
            ["timestamp", "model", "input_tokens", "output_tokens"],
            [["2026-06-18T10:00:00Z", "gpt-4o", 100, 10]],
        )
        # All three forms point at the same CSV and open it on its own -- no saying
        # "csv" twice. (The bare positional, the --csv flag, and --source csv + path.)
        for argv in ([csv_path], ["--csv", csv_path], ["--source", "csv", csv_path]):
            a = _parse(argv)
            assert a.source == "csv", argv
            assert a.csv == csv_path, argv
        # Bare `opentab` is unchanged: auto-merge, CSV auto-discovered at the default path.
        bare = _parse([])
        assert bare.source == "auto"
        assert bare.csv == ot.DEFAULT_CSV_PATH


def test_path_arg_infers_source_routes_under_all_and_rejects_bad_paths():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        open(db, "w").close()
        # A .db positional selects opencode and fills --db.
        a = _parse([db])
        assert a.source == "opencode" and a.db == db

        csv_path = os.path.join(tmp, "requests.csv")
        _write_csv(
            csv_path,
            ["timestamp", "model", "input_tokens", "output_tokens"],
            [["2026-06-18T10:00:00Z", "gpt-4o", 100, 10]],
        )
        # --source all keeps the merged view but still routes the path into the csv slot.
        a = _parse(["--source", "all", csv_path])
        assert a.source == "all" and a.csv == csv_path

        # A missing file and an ambiguous directory both exit with an error.
        for bad in ([os.path.join(tmp, "nope.csv")], [tmp]):
            try:
                _parse(bad)
                raise AssertionError(f"expected an error for {bad}")
            except SystemExit:
                pass


COPILOT_SID = "c623bce1-5906-429f-a517-d4fb2cee7cf7"


def _copilot_args(copilot_dir):
    return type("Args", (), {"demo": False, "copilot_dir": copilot_dir})()


def _otel_chat(
    session,
    model,
    inp,
    out,
    cache_read=0,
    cache_create=0,
    reasoning=0,
    trace="t1",
    span="sp1",
    resp=None,
    end=(1775934264, 0),
):
    # A GenAI `chat` span -- the highest-fidelity per-call OTEL record.
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.conversation.id": session,
        "gen_ai.usage.input_tokens": inp,  # OpenAI-style: includes the cached read
        "gen_ai.usage.output_tokens": out,
    }
    if cache_read:
        attrs["gen_ai.usage.cache_read.input_tokens"] = cache_read
    if cache_create:
        attrs["gen_ai.usage.cache_creation.input_tokens"] = cache_create
    if reasoning:
        attrs["gen_ai.usage.reasoning.output_tokens"] = reasoning
    if resp:
        attrs["gen_ai.response.id"] = resp
    return {
        "type": "span",
        "traceId": trace,
        "spanId": span,
        "name": f"chat {model}",
        "endTime": list(end),
        "attributes": attrs,
    }


def _write_otel(dirpath, rows, name="otel.jsonl"):
    os.makedirs(dirpath, exist_ok=True)
    _write_jsonl(os.path.join(dirpath, name), rows)


def test_copilot_store_splits_cache_folds_reasoning_and_stays_unpriced():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        # input_tokens (19452) includes the 123-token cached read -> uncached 19329;
        # reasoning (128) folds into output for pricing; cache_creation -> cache_write.
        _write_otel(
            otel,
            [
                {"type": "metric", "name": "gen_ai.client.token.usage"},  # non-usage -> ignored
                _otel_chat(
                    COPILOT_SID,
                    "claude-sonnet-4",
                    19452,
                    281,
                    cache_read=123,
                    cache_create=25,
                    reasoning=128,
                ),
            ],
        )
        store = ot.CopilotStore(otel, _copilot_args(otel))
        assert store.records_cost is False  # subscription-style: $0 until "$" reprices
        workflows = store.workflows()
        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == COPILOT_SID
        assert w.source == "Copilot"
        assert w.subagents == 0  # no subagent tree
        assert w.total_cost == 0.0 and w.root_cost == 0.0
        # tokens_total = uncached(19329) + cache_read(123) + cache_write(25) + output(281+128)
        assert w.total_tokens == w.unpriced_tokens == 19329 + 123 + 25 + 409

        row = next(r for r in store.model_breakdown() if r["root_id"] == COPILOT_SID)
        assert row["model_name"] == "anthropic/claude-sonnet-4"  # mixed-provider prefix
        assert row["unpriced_input"] == 19329
        assert row["unpriced_cache_read"] == 123
        assert row["unpriced_cache_write"] == 25
        assert row["unpriced_output"] == 409  # reasoning folded in, priced once
        assert row["reasoning"] == 0  # folded, never double-counted

        # the (all-unpriced) usage reprices to a positive list-price estimate under "$"
        est = ot.api_equivalent_cost("anthropic/claude-sonnet-4", 19329, 409, 0, 123, 25)
        assert est > 0

        nodes = store.workflow_nodes(COPILOT_SID)
        assert len(nodes) == 1 and nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["model_name"] == "anthropic/claude-sonnet-4"
        assert nodes[0]["cost"] == 0.0


def test_copilot_store_dedupes_redundant_records_keeping_chat_span():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        # The same LLM call logged three ways for one (trace, response). Only the chat
        # span must count (60/10) -- the inference log and invoke_agent summary are
        # suppressed by matching trace id / response id.
        agent_summary = {
            "type": "span",
            "traceId": "trace-dupe",
            "spanId": "agent-1",
            "name": "invoke_agent GitHub Copilot",
            "attributes": {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.response.model": "gpt-5.4-mini",
                "gen_ai.conversation.id": "conv-dupe",
                "gen_ai.response.id": "resp-dupe",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 30,
            },
        }
        inference = {
            "hrTime": [1775934263, 0],
            "_body": "GenAI inference: gpt-5.4-mini",
            "attributes": {
                "event.name": "gen_ai.client.inference.operation.details",
                "gen_ai.response.model": "gpt-5.4-mini",
                "gen_ai.conversation.id": "conv-dupe",
                "gen_ai.response.id": "resp-dupe",
                "gen_ai.usage.input_tokens": 80,
                "gen_ai.usage.output_tokens": 20,
            },
        }
        chat = _otel_chat(
            "conv-dupe", "gpt-5.4-mini", 60, 10, trace="trace-dupe", span="chat-1", resp="resp-dupe"
        )
        _write_otel(otel, [agent_summary, inference, chat])
        store = ot.CopilotStore(otel, _copilot_args(otel))
        rows = [r for r in store.model_breakdown() if r["root_id"] == "conv-dupe"]
        assert len(rows) == 1
        assert rows[0]["model_name"] == "openai/gpt-5.4-mini"
        assert rows[0]["runs"] == 1  # only the chat span survived dedup
        assert rows[0]["unpriced_input"] == 60 and rows[0]["unpriced_output"] == 10
        assert rows[0]["tokens_total"] == 70


def test_copilot_store_enriches_cwd_and_title_from_session_store_db():
    with tempfile.TemporaryDirectory() as tmp:
        copilot = os.path.join(tmp, ".copilot")
        otel = os.path.join(copilot, "otel")
        # The session ran in <repo>/sub; OTEL carries no cwd, so it must come from the
        # sibling session-store.db and fold to the git root, with the title from summary.
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(otel)  # also creates the .copilot dir that holds session-store.db
        db = sqlite3.connect(os.path.join(copilot, "session-store.db"))
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, summary TEXT)")
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            (COPILOT_SID, sub, "Refactor the date formatter"),
        )
        db.commit()
        db.close()
        _write_otel(otel, [_otel_chat(COPILOT_SID, "gpt-5.4", 100, 50)])
        w = ot.CopilotStore(otel, _copilot_args(otel)).workflows()[0]
        assert w.directory == repo  # folded to the git root, not the bare "sub"
        assert w.title == "Refactor the date formatter"
        assert w.created_at  # derived from the OTEL endTime


def test_copilot_store_reads_exporter_env_file_and_total_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")  # empty export dir
        os.makedirs(otel)
        # A single-file export pointed to by the documented env var, living OUTSIDE the
        # export dir, must still be read. The record logs only a grand total (no
        # input/output split) -> the total back-fills as output.
        extra = os.path.join(tmp, "elsewhere", "export.jsonl")
        os.makedirs(os.path.dirname(extra))
        rec = {
            "type": "span",
            "traceId": "t9",
            "spanId": "s9",
            "name": "chat gpt-5.4",
            "endTime": [1775934264, 0],
            "attributes": {
                "gen_ai.operation.name": "chat",
                "gen_ai.response.model": "gpt-5.4",
                "gen_ai.conversation.id": "env-sess",
                "gen_ai.usage.total_tokens": 250,
            },
        }
        _write_jsonl(extra, [rec])
        prev = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH")
        os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"] = extra
        try:
            store = ot.CopilotStore(otel, _copilot_args(otel))
            rows = store.model_breakdown()
        finally:
            if prev is None:
                del os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"]
            else:
                os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"] = prev
        assert len(rows) == 1
        assert rows[0]["model_name"] == "openai/gpt-5.4"
        assert rows[0]["unpriced_output"] == 250  # total back-filled as output
        assert rows[0]["tokens_total"] == 250


def test_copilot_store_does_not_double_count_exporter_file_inside_dir():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        # The env var points at a file that ALSO lives in --copilot-dir (the default
        # setup). It must be read once, not once via glob + once via the env var.
        _write_otel(otel, [_otel_chat("s1", "gpt-5.4", 100, 50)], name="usage.jsonl")
        inside = os.path.join(otel, "usage.jsonl")
        prev = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH")
        os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"] = inside
        try:
            rows = ot.CopilotStore(otel, _copilot_args(otel)).model_breakdown()
        finally:
            if prev is None:
                del os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"]
            else:
                os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"] = prev
        assert len(rows) == 1
        assert rows[0]["runs"] == 1  # not 2 -- the file was not read twice
        assert rows[0]["tokens_total"] == 150


PI_SID = "019e2a8c-dfcc-77f3-a956-c3ee1862aca3"


def _pi_args():
    return type("Args", (), {"demo": False})()


def _pi_session(sid, cwd, ts="2026-05-15T07:32:15.949Z"):
    return {"type": "session", "version": 3, "id": sid, "timestamp": ts, "cwd": cwd}


def _pi_user(text, mid="u1", ts="2026-05-15T07:32:34.188Z"):
    return {
        "type": "message",
        "id": mid,
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _pi_assistant(
    model,
    inp,
    out,
    cache_read=0,
    cache_write=0,
    total=None,
    cost=None,
    provider=None,
    api=None,
    mid="a1",
    ts="2026-05-15T07:32:36.257Z",
):
    usage = {"input": inp, "output": out, "cacheRead": cache_read, "cacheWrite": cache_write}
    usage["totalTokens"] = total if total is not None else inp + out + cache_read + cache_write
    if cost is not None:
        usage["cost"] = {"total": cost}
    message = {"role": "assistant", "model": model, "usage": usage}
    if provider is not None:
        message["provider"] = provider
    if api is not None:
        message["api"] = api
    return {"type": "message", "id": mid, "timestamp": ts, "message": message}


def _pi_write(root, project, sid, rows, ts_prefix="2026-05-15T07-32-15-949Z"):
    d = os.path.join(root, project)
    os.makedirs(d, exist_ok=True)
    _write_jsonl(os.path.join(d, f"{ts_prefix}_{sid}.jsonl"), rows)


def test_pi_store_meters_cost_splits_cache_and_rolls_up_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        # Session ran in <repo>/sub; cwd comes from the `session` record and folds to root.
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        # pi records a real per-message cost -> metered; tokens are Anthropic-style
        # (input excludes the cached read, so input stays 339, never subtracted).
        rows = [
            _pi_session(PI_SID, sub),
            _pi_user("hi"),
            _pi_assistant("moonshotai/kimi-k2.6", 339, 33, cache_read=768, cost=0.00048495),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        assert store.records_cost is True  # a recorded cost -> metered
        wfs = store.workflows()
        assert len(wfs) == 1
        w = wfs[0]
        assert w.id == PI_SID
        assert w.source == "Pi"
        assert w.subagents == 0
        assert w.directory == repo  # folded to the git root, not bare "sub"
        assert w.title == "hi"  # first user text
        assert w.created_at.startswith("2026-05-15")
        assert w.total_cost == 0.000485  # recorded spend (rounded to 6dp), not estimated
        assert w.total_tokens == 1140  # 339 + 33 + 768 (+0)
        assert w.unpriced_tokens == 0  # priced -> nothing left for "$" to estimate

        row = next(r for r in store.model_breakdown() if r["root_id"] == PI_SID)
        assert row["model_name"] == "moonshotai/kimi-k2.6"  # used verbatim (already prefixed)
        assert row["input"] == 339 and row["cache_read"] == 768  # input not reduced
        assert row["unpriced_input"] == 0  # priced row -> unpriced split zeroed

        nodes = store.workflow_nodes(PI_SID)
        assert len(nodes) == 1 and nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["cost"] == 0.000485


def test_pi_store_dedupes_assistant_messages_by_id():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        a = _pi_assistant("anthropic/claude-sonnet-4", 100, 50, cost=0.01, mid="dupe")
        rows = [_pi_session(PI_SID, cwd), _pi_user("go"), a, dict(a)]  # same id twice
        _pi_write(root, "--proj--", PI_SID, rows)
        row = next(
            r for r in ot.PiStore(root, _pi_args()).model_breakdown() if r["root_id"] == PI_SID
        )
        assert row["runs"] == 1  # the duplicate assistant step was not double-counted
        assert row["tokens_total"] == 150
        assert abs(row["cost"] - 0.01) < 1e-9


def test_pi_store_unpriced_session_estimates_under_dollar():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # A subscription-route session: usage but no cost -> records_cost False, the tokens
        # stay unpriced so the "$" what-if estimates them at list price.
        rows = [
            _pi_session(PI_SID, cwd),
            _pi_user("estimate me"),
            _pi_assistant("anthropic/claude-sonnet-4", 1000, 500, cache_read=200),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        assert store.records_cost is False  # no recorded cost anywhere
        w = store.workflows()[0]
        assert w.total_cost == 0.0
        assert w.total_tokens == w.unpriced_tokens == 1700
        row = next(r for r in store.model_breakdown() if r["root_id"] == PI_SID)
        assert row["unpriced_input"] == 1000 and row["unpriced_cache_read"] == 200
        est = ot.api_equivalent_cost("anthropic/claude-sonnet-4", 1000, 500, 0, 200, 0)
        assert est > 0


def test_pi_store_falls_back_to_total_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # Only totalTokens recorded (no input/output split) -> back-fills as output.
        a = {
            "type": "message",
            "id": "a1",
            "timestamp": "2026-05-15T07:32:36.257Z",
            "message": {
                "role": "assistant",
                "model": "openai/gpt-5",
                "usage": {"totalTokens": 333},
            },
        }
        _pi_write(root, "--proj--", PI_SID, [_pi_session(PI_SID, cwd), a])
        row = next(
            r for r in ot.PiStore(root, _pi_args()).model_breakdown() if r["root_id"] == PI_SID
        )
        assert row["output"] == 333 and row["tokens_total"] == 333
        assert row["model_name"] == "openai/gpt-5"


def test_pi_store_subscription_route_cost_is_not_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # auth.json marks openai-codex as an OAuth (ChatGPT-plan) login -> subscription.
        # pi still writes a list-price cost, but it is NOT what the user pays, so it must be
        # dropped (tokens unpriced, estimated under "$"), not counted as real spend.
        with open(os.path.join(tmp, "auth.json"), "w") as fh:
            json.dump({"openai-codex": {"type": "oauth", "access": "x"}}, fh)
        rows = [
            _pi_session(PI_SID, cwd),
            _pi_user("whats the repo about?"),
            _pi_assistant(
                "gpt-5.5",
                8289,
                231,
                cost=0.048375,
                provider="openai-codex",
                api="openai-codex-responses",
            ),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        assert store.records_cost is False  # subscription-only setup -> nothing metered
        w = store.workflows()[0]
        assert w.total_cost == 0.0  # the $0.048 list-price cost is not real spend
        assert w.total_tokens == w.unpriced_tokens == 8520  # all of it estimable under "$"
        row = next(r for r in store.model_breakdown() if r["root_id"] == PI_SID)
        assert row["cost"] == 0.0 and row["unpriced_input"] == 8289
        est = ot.api_equivalent_cost("openai/gpt-5.5", 8289, 231, 0, 0, 0)
        assert est > 0  # the "$" view still estimates the plan usage


def test_pi_store_mixes_metered_and_subscription_in_one_session():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # One session, two routes: openrouter (metered, real cost) + a codex turn
        # (subscription, recognized by the provider marker -- no auth.json needed). Only
        # the openrouter spend is real; the codex tokens are unpriced.
        rows = [
            _pi_session(PI_SID, cwd),
            _pi_user("go"),
            _pi_assistant(
                "moonshotai/kimi-k2.6", 8000, 300, cost=0.0071, provider="openrouter", mid="m1"
            ),
            _pi_assistant("gpt-5.5", 5000, 200, cost=0.03, provider="openai-codex", mid="m2"),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        assert store.records_cost is True  # the openrouter turn is genuinely metered
        w = store.workflows()[0]
        assert w.total_cost == 0.0071  # openrouter only; the codex $0.03 is excluded
        assert w.total_tokens == 13500  # 8300 + 5200
        assert w.unpriced_tokens == 5200  # just the codex (subscription) turn
        rows_out = {r["model_name"]: r for r in store.model_breakdown() if r["root_id"] == PI_SID}
        assert rows_out["moonshotai/kimi-k2.6"]["unpriced_input"] == 0  # metered -> priced
        assert rows_out["gpt-5.5"]["cost"] == 0.0  # subscription -> no real cost
        assert rows_out["gpt-5.5"]["unpriced_input"] == 5000


OCL_SID = "01998b2c-7d41-7a90-bf03-2b6e1c9f04aa"


def _ocl_args():
    return type("Args", (), {"demo": False})()


def _ocl_user(text, mid="u1", ts="2026-04-27T16:00:00.000Z"):
    return {
        "type": "message",
        "id": mid,
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _ocl_msg(
    model,
    inp,
    out,
    cache_read=0,
    cache_write=0,
    total=None,
    cost=None,
    provider=None,
    api=None,
    mid="a1",
    ts="2026-04-27T16:00:16.401Z",
):
    usage = {"input": inp, "output": out, "cacheRead": cache_read, "cacheWrite": cache_write}
    usage["totalTokens"] = total if total is not None else inp + out + cache_read + cache_write
    if cost is not None:
        usage["cost"] = {"total": cost}  # OpenClaw records cost as an object; only .total read
    message = {"role": "assistant", "usage": usage}
    if model is not None:
        message["model"] = model
    if provider is not None:
        message["provider"] = provider
    if api is not None:
        message["api"] = api
    return {"type": "message", "id": mid, "timestamp": ts, "message": message}


def _ocl_model_snapshot(provider, model_id, mid="mc1", ts="2026-04-27T15:59:00.000Z"):
    return {
        "type": "custom",
        "customType": "model-snapshot",
        "data": {"provider": provider, "modelApi": "x", "modelId": model_id},
        "id": mid,
        "timestamp": ts,
    }


def _ocl_write(root, agent, sid, rows, suffix=".jsonl"):
    d = os.path.join(root, "agents", agent, "sessions")
    os.makedirs(d, exist_ok=True)
    _write_jsonl(os.path.join(d, f"{sid}{suffix}"), rows)


def _ocl_oauth(root, profiles):
    # profiles: {provider: mode}; written in openclaw.json's auth.profiles shape.
    data = {
        "auth": {
            "profiles": {f"{p}:default": {"mode": m, "provider": p} for p, m in profiles.items()}
        }
    }
    with open(os.path.join(root, "openclaw.json"), "w") as fh:
        json.dump(data, fh)


def test_openclaw_store_meters_cost_splits_cache_and_uses_agent_as_project():
    with tempfile.TemporaryDirectory() as root:
        # A direct-Anthropic-key turn: provider isn't OAuth/plan -> metered, real spend.
        rows = [
            _ocl_user("summarize the budget"),
            _ocl_msg(
                "claude-opus-4-6", 1660, 55, cache_read=108928, cost=0.0228375, provider="anthropic"
            ),
        ]
        _ocl_write(root, "finance-os", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is True  # a metered cost -> real spend
        wfs = store.workflows()
        assert len(wfs) == 1
        w = wfs[0]
        assert w.id == OCL_SID
        assert w.source == "OpenClaw"
        assert w.subagents == 0
        assert w.directory == "finance-os"  # the agent name, not the gateway cwd
        assert w.title == "summarize the budget"
        assert w.total_cost == 0.022838  # recorded spend (rounded to 6dp), not estimated
        assert w.total_tokens == 110643  # 1660 + 55 + 108928 (input not reduced)
        assert w.unpriced_tokens == 0  # priced -> nothing left for "$" to estimate

        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["model_name"] == "anthropic/claude-opus-4-6"  # bare id -> provider-prefixed
        assert row["input"] == 1660 and row["cache_read"] == 108928
        assert row["unpriced_input"] == 0  # priced row -> unpriced split zeroed

        nodes = store.workflow_nodes(OCL_SID)
        assert len(nodes) == 1 and nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["cost"] == 0.022838


def test_openclaw_store_dedupes_messages_across_archive_files():
    with tempfile.TemporaryDirectory() as root:
        a = _ocl_msg("claude-sonnet-4-5", 100, 50, cost=0.01, provider="anthropic", mid="dupe")
        # Same assistant step lives in the live file and a .jsonl.reset archive -> count once.
        _ocl_write(root, "main", OCL_SID, [_ocl_user("go"), a])
        _ocl_write(root, "main", OCL_SID, [a], suffix=".jsonl.reset.2026-03-20T06-34-44.520Z")
        store = ot.OpenClawStore(root, _ocl_args())
        wfs = store.workflows()
        assert len(wfs) == 1  # the two files key to one session id
        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["runs"] == 1  # the archived duplicate was not double-counted
        assert row["tokens_total"] == 150
        assert abs(row["cost"] - 0.01) < 1e-9


def test_openclaw_store_unpriced_session_estimates_under_dollar():
    with tempfile.TemporaryDirectory() as root:
        # Usage but no recorded cost -> records_cost False, tokens unpriced for the "$" view.
        rows = [
            _ocl_user("estimate me"),
            _ocl_msg("claude-sonnet-4-5", 1000, 500, cache_read=200, provider="anthropic"),
        ]
        _ocl_write(root, "homelab", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is False  # no recorded cost anywhere
        w = store.workflows()[0]
        assert w.total_cost == 0.0
        assert w.total_tokens == w.unpriced_tokens == 1700
        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["unpriced_input"] == 1000 and row["unpriced_cache_read"] == 200
        est = ot.api_equivalent_cost("anthropic/claude-sonnet-4-5", 1000, 500, 0, 200, 0)
        assert est > 0


def test_openclaw_store_falls_back_to_total_tokens():
    with tempfile.TemporaryDirectory() as root:
        # Only totalTokens recorded (no input/output split) -> back-fills as output.
        a = {
            "type": "message",
            "id": "a1",
            "timestamp": "2026-04-27T16:00:16.401Z",
            "message": {"role": "assistant", "model": "gpt-5.2", "usage": {"totalTokens": 333}},
        }
        _ocl_write(root, "main", OCL_SID, [_ocl_user("hi"), a])
        row = next(
            r
            for r in ot.OpenClawStore(root, _ocl_args()).model_breakdown()
            if r["root_id"] == OCL_SID
        )
        assert row["output"] == 333 and row["tokens_total"] == 333
        assert row["model_name"] == "openai/gpt-5.2"  # gpt -> openai/


def test_openclaw_store_oauth_route_cost_is_not_real_spend():
    with tempfile.TemporaryDirectory() as root:
        # openclaw.json marks openai-codex as an OAuth (ChatGPT-plan) login -> subscription.
        # OpenClaw still writes a list-price cost, but it is NOT what the user pays.
        _ocl_oauth(root, {"openai-codex": "oauth", "anthropic": "token"})
        rows = [
            _ocl_user("whats this repo about?"),
            _ocl_msg(
                "gpt-5.3-codex",
                12594,
                57,
                cost=0.0228375,
                provider="openai-codex",
                api="openai-codex-responses",
            ),
        ]
        _ocl_write(root, "main", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is False  # OAuth route -> nothing metered
        w = store.workflows()[0]
        assert w.total_cost == 0.0  # the list-price cost is not real spend
        assert w.total_tokens == w.unpriced_tokens == 12651  # all estimable under "$"
        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["cost"] == 0.0 and row["unpriced_input"] == 12594
        assert ot.api_equivalent_cost("openai/gpt-5.3-codex", 12594, 57, 0, 0, 0) > 0


def test_openclaw_store_copilot_marker_is_subscription_without_openclaw_json():
    with tempfile.TemporaryDirectory() as root:
        # github-copilot logs in with a static token (mode != "oauth"), so the OAuth probe
        # misses it -- the "copilot" provider marker catches it instead. No openclaw.json.
        rows = [
            _ocl_user("draft a PR"),
            _ocl_msg(
                "gpt-4o", 800, 120, cost=0.005, provider="github-copilot", api="openai-completions"
            ),
        ]
        _ocl_write(root, "github-os", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is False  # copilot is a plan route -> not real spend
        w = store.workflows()[0]
        assert w.total_cost == 0.0
        assert w.total_tokens == w.unpriced_tokens == 920
        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["cost"] == 0.0 and row["unpriced_output"] == 120


def test_openclaw_store_model_snapshot_supplies_model_and_provider():
    with tempfile.TemporaryDirectory() as root:
        # A model-snapshot sets the current model+provider; the following assistant message
        # omits both, so it inherits them -- model for the label, provider for billing.
        rows = [
            _ocl_model_snapshot("openai-codex", "gpt-5.2"),
            _ocl_user("go"),
            _ocl_msg(None, 2000, 80, cost=0.011, provider=None),  # codex marker -> subscription
        ]
        _ocl_write(root, "main", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is False  # provider inherited from the snapshot -> codex plan
        row = next(r for r in store.model_breakdown() if r["root_id"] == OCL_SID)
        assert row["model_name"] == "openai/gpt-5.2"  # model id from the snapshot
        assert row["cost"] == 0.0 and row["unpriced_input"] == 2000


def test_openclaw_store_mixes_metered_and_subscription_in_one_session():
    with tempfile.TemporaryDirectory() as root:
        # One session, two routes: anthropic (metered, real cost) + a codex turn
        # (subscription via the provider marker). Only the anthropic spend is real.
        rows = [
            _ocl_user("go"),
            _ocl_msg("claude-opus-4-6", 8000, 300, cost=0.0071, provider="anthropic", mid="m1"),
            _ocl_msg("gpt-5.3-codex", 5000, 200, cost=0.03, provider="openai-codex", mid="m2"),
        ]
        _ocl_write(root, "main", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.records_cost is True  # the anthropic turn is genuinely metered
        w = store.workflows()[0]
        assert w.total_cost == 0.0071  # anthropic only; the codex $0.03 is excluded
        assert w.total_tokens == 13500  # 8300 + 5200
        assert w.unpriced_tokens == 5200  # just the codex (subscription) turn
        rows_out = {r["model_name"]: r for r in store.model_breakdown() if r["root_id"] == OCL_SID}
        assert rows_out["anthropic/claude-opus-4-6"]["unpriced_input"] == 0  # metered -> priced
        assert rows_out["openai/gpt-5.3-codex"]["cost"] == 0.0  # subscription -> no real cost
        assert rows_out["openai/gpt-5.3-codex"]["unpriced_input"] == 5000


def test_refresh_model_prices_writes_cache_and_overlays_table():
    models_dev = {
        "anthropic": {
            "models": {"claude-opus-4-8": {"cost": {"input": 99.0, "output": 88.0}}}
        },  # overrides the embedded snapshot for this model
        "openrouter": {
            "models": {
                "moonshotai/kimi-k2.6": {"cost": {"input": 0.6, "output": 2.5, "cache_read": 0.1}}
            }
        },
        "junk": "not a dict",  # tolerated, skipped
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp  # so price_cache_path() lands in the temp dir
        src = os.path.join(tmp, "api.json")
        with open(src, "w") as fh:
            json.dump(models_dev, fh)
        try:
            ot.invalidate_price_cache()
            count, path = ot.refresh_model_prices(url="file://" + src)
            assert count == 2
            assert path == ot.price_cache_path()
            # a refreshed price overlays the embedded table
            assert ot.model_price("anthropic/claude-opus-4-8") == (99.0, 88.0, 0.0, 0.0)
            # a resold open model (vendor/model id) now prices off the cache, by bare id
            assert ot.model_price("moonshotai/kimi-k2.6") == (0.6, 2.5, 0.1, 0.0)
            meta = ot.price_cache_meta()
            assert meta and meta["count"] == 2 and meta["fetched_at"]
        finally:
            ot.invalidate_price_cache()
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_refresh_model_prices_rejects_empty_response():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "api.json")
        with open(src, "w") as fh:
            json.dump({"anthropic": {"models": {}}}, fh)  # no priced models
        raised = False
        try:
            ot.refresh_model_prices(url="file://" + src, dest=os.path.join(tmp, "p.json"))
        except ValueError:
            raised = True
        assert raised
        assert not os.path.exists(os.path.join(tmp, "p.json"))  # nothing written on failure


def test_model_price_uses_embedded_table_without_cache():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp  # empty dir -> no prices.json
        try:
            ot.invalidate_price_cache()
            assert ot.price_cache_meta() is None
            ir, orr, _cr, _cw = ot.model_price("anthropic/claude-opus-4-8")
            assert ir > 0 and orr > 0  # from the embedded table, not the (absent) cache
        finally:
            ot.invalidate_price_cache()
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def _price_prompt_app(model="openrouter/exotic-zzz-9", unpriced=500):
    # An app whose model scan contains one model + an unpriced-token workflow, with an
    # isolated empty config so price_cache_meta() is None. Caller restores via _restore_xdg.
    w = workflow("w1", "2026-06-01 12:00:00")
    w.unpriced_tokens = unpriced
    app = app_with([w])
    app._model_by_root = {"w1": [{"model_name": model}]}
    return app


def test_price_prompt_triggers_on_unknown_models():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.invalidate_price_cache()
            app = _price_prompt_app()
            app.maybe_prompt_prices()
            assert app.price_prompt is True
            assert app.unknown_models == ["openrouter/exotic-zzz-9"]
            # offered at most once per run
            app.price_prompt = False
            app.maybe_prompt_prices()
            assert app.price_prompt is False
        finally:
            ot.invalidate_price_cache()
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old


def test_price_prompt_skipped_when_known_dismissed_or_no_unpriced():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.invalidate_price_cache()
            # a hardcoded model -> no prompt
            known = _price_prompt_app(model="anthropic/claude-opus-4-8")
            known.maybe_prompt_prices()
            assert known.price_prompt is False
            # unknown model but no unpriced tokens -> nothing to estimate -> no prompt
            metered = _price_prompt_app(unpriced=0)
            metered.maybe_prompt_prices()
            assert metered.price_prompt is False
            # unknown + unpriced but "don't ask again" set -> no prompt
            dismissed = _price_prompt_app()
            dismissed.prices_prompt_dismissed = True
            dismissed.maybe_prompt_prices()
            assert dismissed.price_prompt is False
            # --no-state / demo path (allow_price_prompt False) -> no prompt
            blocked = _price_prompt_app()
            blocked.allow_price_prompt = False
            blocked.maybe_prompt_prices()
            assert blocked.price_prompt is False
        finally:
            ot.invalidate_price_cache()
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old


def test_price_prompt_keys_fetch_dismiss_and_skip():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.invalidate_price_cache()
            # y fetches (stubbed) and closes
            app = _price_prompt_app()
            app.price_prompt = True
            fetched = []
            app.refresh_prices_action = lambda: fetched.append(True)
            app.handle_price_prompt_key(ord("y"))
            assert app.price_prompt is False and fetched == [True]
            # n closes without dismissing
            app.price_prompt = True
            app.handle_price_prompt_key(ord("n"))
            assert app.price_prompt is False and app.prices_prompt_dismissed is False
            # d dismisses and persists through save_state -> a fresh app restores it
            app.price_prompt = True
            app.handle_price_prompt_key(ord("d"))
            assert app.price_prompt is False and app.prices_prompt_dismissed is True
            ot.save_state(app)
            restored = app_with([workflow("w1", "2026-06-01 12:00:00")])
            ot.apply_state(restored, restored.args, ot.load_state())
            assert restored.prices_prompt_dismissed is True
        finally:
            ot.invalidate_price_cache()
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old


def _menu_app(current="opencode", cycle=("opencode", "claude", "all")):
    # An app whose source cycle is fixed, with select_source stubbed so menu tests never
    # touch the filesystem / make_store. Returns (app, chosen) where chosen records picks.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.source_key = current
    chosen = {}
    app._orig_cycle = ot.sources.source_cycle
    ot.sources.source_cycle = lambda args, _c=list(cycle): list(_c)
    app.select_source = lambda key: chosen.setdefault("key", key)
    return app, chosen


def test_source_menu_opens_at_current_and_navigates_then_selects():
    app, chosen = _menu_app(current="opencode")
    try:
        app.handle_key(None, ord("c"))  # opens the picker
        assert app.source_menu is True
        assert app.source_menu_index == 0  # highlight starts on the active source
        app.handle_source_menu_key(ord("j"))
        assert app.source_menu_index == 1  # -> claude
        app.handle_source_menu_key(ot.curses.KEY_DOWN)
        assert app.source_menu_index == 2  # -> all
        app.handle_source_menu_key(ord("j"))
        assert app.source_menu_index == 0  # wraps
        app.handle_source_menu_key(ord("k"))
        assert app.source_menu_index == 2  # wraps back up
        app.handle_source_menu_key(ord("k"))  # -> claude
        app.handle_source_menu_key(10)  # Enter selects + closes
        assert app.source_menu is False
        assert chosen["key"] == "claude"
    finally:
        ot.sources.source_cycle = app._orig_cycle


def test_source_menu_c_advances_and_esc_cancels():
    app, chosen = _menu_app(current="claude")
    try:
        app.open_source_menu()
        assert app.source_menu_index == 1  # claude is current
        app.handle_source_menu_key(ord("c"))  # c walks the list too
        assert app.source_menu_index == 2
        app.handle_source_menu_key(ord("c"))
        assert app.source_menu_index == 0  # wraps
        app.handle_source_menu_key(27)  # Esc cancels, source unchanged
        assert app.source_menu is False
        assert "key" not in chosen
    finally:
        ot.sources.source_cycle = app._orig_cycle


def test_source_menu_not_opened_with_single_source():
    app, _ = _menu_app(current="opencode", cycle=("opencode",))
    try:
        app.open_source_menu()
        assert app.source_menu is False
        assert app.notice == "only one data source available"
    finally:
        ot.sources.source_cycle = app._orig_cycle


def test_source_menu_entries_label_all_and_mark_current():
    app, _ = _menu_app(current="all", cycle=("opencode", "openclaw", "all"))
    try:
        entries = app.source_menu_entries()
        assert [k for k, _, _ in entries] == ["opencode", "openclaw", "all"]
        labels = {k: lbl for k, lbl, _ in entries}
        assert labels["openclaw"] == "OpenClaw"
        assert labels["all"] == "All sources (merged)"  # friendlier than the bare "all"
        assert {k: cur for k, _, cur in entries} == {
            "opencode": False,
            "openclaw": False,
            "all": True,
        }
    finally:
        ot.sources.source_cycle = app._orig_cycle


def test_open_path_uses_startfile_on_windows():
    # On Windows there is no open/xdg-open; open_path reveals the folder via os.startfile.
    called = {}
    orig_platform = ot.sys.platform
    had_startfile = hasattr(ot.os, "startfile")
    orig_startfile = getattr(ot.os, "startfile", None)
    try:
        ot.sys.platform = "win32"
        ot.os.startfile = lambda p: called.setdefault("path", p)
        assert ot.open_path("C:/repo/proj") is True
        assert called["path"] == "C:/repo/proj"
    finally:
        ot.sys.platform = orig_platform
        if had_startfile:
            ot.os.startfile = orig_startfile
        else:
            del ot.os.startfile


if __name__ == "__main__":
    import sys

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
