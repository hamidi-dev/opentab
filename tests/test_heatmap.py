"""Heat levels, calendar cells and the week/month bucketing (heatmap.py)."""

import opentab as ot


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


def test_week_key_tolerates_missing_or_garbage_date():
    # Some backends emit a workflow with no usable timestamp (e.g. a Claude metadata-only
    # session: just an ai-title, no messages, no timestamps). week_key returns "" instead
    # of raising so such a row is treated as off-timeline, never crashing a trend view.
    assert ot.week_key("") == ""
    assert ot.week_key("not-a-date") == ""
    assert ot.week_key("2026-13-99 12:00:00") == ""  # parseable shape, impossible date


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


def test_month_window_start_is_bucket_aligned():
    base = ot.datetime(2026, 6, 8)
    # "2m" = this month + last => starts at the first of last month (two buckets)
    assert ot.month_window_start(2, base) == "2026-05-01"
    assert ot.month_window_start(1, base) == "2026-06-01"  # just this month
    assert ot.month_window_start(12, base) == "2025-07-01"  # trailing twelve months
    # wraps across the year boundary
    assert ot.month_window_start(2, ot.datetime(2026, 1, 15)) == "2025-12-01"
