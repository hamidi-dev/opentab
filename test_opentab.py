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


class AttrScreen(FakeScreen):
    # FakeScreen that also remembers the attribute each glyph was painted with, so a
    # test can assert on color/bold/dim rather than just the text.
    def __init__(self, height=24, width=80):
        super().__init__(height, width)
        self.attrs = {}

    def addstr(self, y, x, text, attr=0):
        super().addstr(y, x, text, attr)
        for i in range(len(text)):
            self.attrs[(y, x + i)] = attr


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
    # Open the Trends overlay and tab across to the Calendar heat map (unfocused).
    app.handle_key(None, ord("T"))
    while app.trend_tabs[app.trend_tab] != "Calendar":
        app.handle_key(None, ord("l"))
    return app


def focus_calendar(app):
    # Open the Calendar tab and focus its day grid (Enter) so arrows walk days.
    open_calendar(app)
    app.handle_key(None, 10)  # Enter focuses the grid
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


def test_money_label_marks_sub_cent_costs_like_money():
    # The compact bar label spells sub-cent the same way money() does.
    assert ot.money_label(0.004) == "<$0.01"
    assert ot.money_label(0) == ""


def test_display_width_counts_terminal_cells():
    assert ot.display_width("abc") == 3
    assert ot.display_width("") == 0
    assert ot.display_width("日本語") == 6  # CJK glyphs take two cells each
    assert ot.display_width("日本語 ok") == 9
    assert ot.display_width("e\u0301") == 1  # combining accent adds no cell


def test_shorten_truncates_by_display_cells():
    # A CJK title must never render wider than its column budget.
    title = "日本語のセッションタイトル"
    for width in (6, 7, 10, 13):
        cut = ot.shorten(title, width)
        assert ot.display_width(cut) <= width
        assert cut.endswith("...")
    assert ot.shorten(title, 100) == title
    assert ot.shorten("hello world", 8) == "hello..."
    # A wide char straddling the boundary is dropped, not half-drawn.
    assert ot.shorten("日日日", 5) == "日..."


def test_pad_fills_to_exact_display_width():
    assert ot.pad("abc", 6) == "abc   "
    padded = ot.pad("日本", 8)
    assert padded == "日本    "
    assert ot.display_width(padded) == 8
    assert ot.pad("toolong", 3) == "toolong"  # never truncates, only pads


def test_clip_never_exceeds_the_cell_budget():
    assert ot.clip("hello", 3) == "hel"
    assert ot.clip("日本語", 4) == "日本"
    assert ot.clip("日本語", 5) == "日本"  # the straddling wide char is dropped
    assert ot.clip("日本語", 0) == ""


def test_short_path_keeps_wide_tails_within_budget():
    path = "/home/user/プロジェクト/深いディレクトリ"
    cut = ot.short_path(path, 12)
    assert ot.display_width(cut) <= 12
    assert cut.startswith("...")


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


def test_week_key_tolerates_missing_or_garbage_date():
    # Some backends emit a workflow with no usable timestamp (e.g. a Claude metadata-only
    # session: just an ai-title, no messages, no timestamps). week_key returns "" instead
    # of raising so such a row is treated as off-timeline, never crashing a trend view.
    assert ot.week_key("") == ""
    assert ot.week_key("not-a-date") == ""
    assert ot.week_key("2026-13-99 12:00:00") == ""  # parseable shape, impossible date


def test_trends_survive_an_undated_workflow():
    # Regression: an undated ($0, dateless) workflow mixed in must not crash any
    # time-bucketed trend view -- it is simply excluded from each timeline.
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


def test_model_table_splits_cost_across_token_categories_in_wide_panes():
    # The cacheR/cacheW/Output cells carry their attributed share of the Cost
    # column: fable lists at $10/M in, $50/M out, $1/M cacheR, $12.50/M cacheW,
    # so 100k cache-write tokens cost more than 800k cache reads -- the skew the
    # plain token counts hide.
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    lines = app.renderer._model_table(rows, "# Top Models", 120)
    assert "800.0k ($0.80)" in lines[2]
    assert "100.0k ($1.25)" in lines[2]
    assert "50.0k ($2.50)" in lines[2]


def test_model_table_split_scales_to_the_recorded_cost():
    # A recorded cost that differs from today's list-price total is attributed
    # proportionally, so the split (with the implicit input remainder) always
    # sums to the Cost column.
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 10.10, 1_000_000, 800_000, 100_000, 50_000)]
    lines = app.renderer._model_table(rows, "# Top Models", 120)
    assert "800.0k ($1.60)" in lines[2]
    assert "100.0k ($2.50)" in lines[2]
    assert "50.0k ($5.00)" in lines[2]


def test_model_table_split_cells_align_under_their_labels():
    # Fixed sub-columns: the token count right-aligns under the header label and
    # the "($13)" groups end flush at the same column on every row, the parens
    # hugging the amount (no inner gap), whatever the magnitudes.
    app = app_with([])
    rows = [
        ("anthropic/claude-fable-5", 92, 20.60, 13_400_000, 13_100_000, 194_700, 99_200),
        ("anthropic/claude-opus-4-8", 1, 0.05, 23_500, 15_000, 1_900, 57),
    ]
    lines = app.renderer._model_table(rows, "# Model Mix", 120)
    header, first, second = lines[1], lines[2], lines[3]
    for label in ("CacheR", "CacheW", "Output"):
        i = header.index(label)
        assert first[i + 5] != " " and second[i + 5] != " "  # tokens end under the label
        assert first[i + 13] == ")" and second[i + 13] == ")"
    assert "( " not in first and "( " not in second  # parens hug the amount


def test_model_table_split_needs_width_dollars_and_models():
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    # Narrow pane: plain token counts, exactly the classic layout.
    narrow = app.renderer._model_table(rows, "# Top Models", 80)
    assert not any("(" in ln for ln in narrow[1:])
    assert "800.0k" in narrow[2]
    # Unpriced rows ($0.00): nothing to attribute even in a wide pane.
    unpriced = app.renderer._model_table(
        [("anthropic/claude-fable-5", 10, 0.0, 1_000_000, 800_000, 100_000, 50_000)],
        "# Top Models",
        120,
    )
    assert not any("(" in ln for ln in unpriced[1:])
    # The Tools tab reuse: tool names aren't models, so no split there either.
    tools = app.renderer._model_table(
        rows, "# Tools — this session", 120, "Tool", "Calls", price_split=False
    )
    assert not any("(" in ln for ln in tools[1:])


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


def test_calendar_heat_grid_dims_until_focused():
    # The visual affordance for the modal tab: the grid is dimmed (and unbolded) while
    # unfocused, so arriving on Calendar reads as "asleep -- press Enter"; focusing lights
    # it back up. We probe the busiest day's own cell, away from the bright cursor marker.
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
    # The call-to-action below the grid: an orange (accent pair 6) bold line only while
    # the grid is asleep; once focused it gives way to the live per-day detail.
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
    assert app.cal_cursor is None  # nothing pinned yet
    assert app.calendar_cursor() == "2026-07-09"  # defaults to the peak-spend day


def test_calendar_arrow_keys_walk_the_day_cursor():
    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    focus_calendar(app)  # arrows only walk days once the grid is focused
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
    focus_calendar(app)
    app.cal_cursor = "2026-07-09"
    app.handle_key(None, 10)  # Enter -> drills the focused day
    assert not app.trends  # overlay closed
    assert app.view == "zoom" and app.focus == "days"
    assert app.active_day == "2026-07-09"
    assert len(app.workflows) == 2  # both of that day's sessions


def test_calendar_enter_on_empty_day_nudges_and_stays():
    app = app_with([workflow("a", "2026-07-09 09:00:00", cost=40)])
    focus_calendar(app)
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
    focus_calendar(app)  # picking days with the mouse only works once focused
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


def test_calendar_mouse_is_gated_until_focused():
    # The grid is modal for the mouse too: while unfocused a click only wakes it (it
    # can't pick or drill a day), and a double-click likewise just focuses. Once focused,
    # clicks resolve to days as usual (covered by the test above).
    from datetime import datetime

    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    open_calendar(app)  # unfocused
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
    # A double-click on the sleeping grid focuses it but does not drill into the day.
    app._mouse_trends(my, mx, up=False, down=False, click=False, double=True)
    assert app.trend_focus and app.trends and app.view == "browse"
    assert app.cal_cursor is None  # no day was picked


def test_calendar_escape_returns_to_the_heat_map():
    app = app_with(
        [
            workflow("a", "2026-07-09 12:00:00", cost=40),
            workflow("b", "2025-05-01 12:00:00", cost=8),
        ]
    )
    focus_calendar(app)
    app.cal_cursor = "2026-07-09"
    app.handle_key(None, 10)  # Enter -> drill into that day
    assert not app.trends and app.view == "zoom"
    app.handle_key(None, 27)  # Esc -> back to the heat map, not just to browse
    assert app.trends and app.trend_tabs[app.trend_tab] == "Calendar"
    assert app.view == "browse"
    assert app.trend_year_index == app.calendar_years().index("2026")
    assert app.cal_cursor == "2026-07-09"  # cursor restored to the day we came from
    assert app.trend_focus  # and the grid is focused again, so arrows resume


def test_calendar_unfocused_arrows_switch_tabs():
    # The reported bug: on the Calendar tab the arrows used to be trapped by the day
    # cursor, with no way to reach the other tabs. Until the grid is focused, ←/→ must
    # move between tabs like everywhere else and leave the day cursor alone.
    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    open_calendar(app)
    assert not app.trend_focus and app.cal_cursor is None
    app.handle_key(None, ot.curses.KEY_RIGHT)  # -> next tab, not a day move
    assert app.trend_tabs[app.trend_tab] != "Calendar"
    assert app.cal_cursor is None  # the day cursor never moved
    app.handle_key(None, ot.curses.KEY_LEFT)  # <- back onto Calendar
    assert app.trend_tabs[app.trend_tab] == "Calendar" and not app.trend_focus


def test_calendar_enter_focuses_and_escape_steps_back_out():
    # Enter focuses the grid (arrows then pick days); Esc leaves focus without closing
    # the overlay, and a second Esc closes it -- the mode the issue asked for.
    app = app_with([workflow("hot", "2026-07-09 12:00:00", cost=40)])
    open_calendar(app)
    assert not app.trend_focus
    app.handle_key(None, 10)  # Enter -> focus the grid
    assert app.trend_focus and app.trends
    app.handle_key(None, ot.curses.KEY_UP)  # now arrows walk days
    assert app.cal_cursor == "2026-07-08"
    app.handle_key(None, 27)  # Esc -> leave focus, overlay stays open
    assert app.trends and not app.trend_focus
    assert app.trend_tabs[app.trend_tab] == "Calendar"
    app.handle_key(None, 27)  # Esc again -> close the overlay
    assert not app.trends


def test_normal_day_drill_does_not_bounce_back_to_the_calendar():
    # Only a heat-map drill arms the Esc-return; an ordinary panel drill must clear it.
    app = app_with([workflow("a", "2026-07-09 12:00:00", cost=40)])
    focus_calendar(app)
    app.cal_cursor = "2026-07-09"
    app.handle_key(None, 10)  # heat-map drill arms the return
    assert app._trend_return == ("Calendar", "2026-07-09")
    app.view = "browse"  # back out to the panels and drill a day the ordinary way
    app.focus = "days"
    app.drill_in()
    assert app._trend_return is None  # the fresh drill disarmed it
    app.drill_out()
    assert not app.trends  # so Esc stays in browse, no calendar bounce


def test_trend_daily_bars_focus_walk_and_drill():
    # The Daily chart follows the Calendar's modal pattern: Enter focuses it, arrows
    # walk the bar cursor (↑/↓ hop a week), Enter drills into the highlighted day,
    # and Esc out of that day returns to the focused chart with the cursor kept.
    app = app_with(
        [
            workflow("a", "2026-06-10 12:00:00", cost=40),
            workflow("b", "2026-06-12 12:00:00", cost=8),
        ]
    )
    app.handle_key(None, ord("T"))  # opens on Daily
    assert app.trend_tabs[app.trend_tab] == "Daily" and not app.trend_focus
    app.handle_key(None, 10)  # Enter -> focus the chart
    assert app.trend_focus
    assert app.trend_bar_cursor() == "2026-06-10"  # defaults to the peak day
    app.handle_key(None, ot.curses.KEY_RIGHT)
    assert app.trend_bar_cursor() == "2026-06-11"
    app.handle_key(None, ot.curses.KEY_DOWN)  # a week forward on Daily
    assert app.trend_bar_cursor() == "2026-06-18"
    app.handle_key(None, 27)  # Esc -> unfocus, overlay stays open
    assert app.trends and not app.trend_focus
    app.handle_key(None, 10)  # refocus
    app.trend_cursor = "2026-06-12"
    app.handle_key(None, 10)  # Enter -> drill into that day
    assert not app.trends and app.view == "zoom" and app.focus == "days"
    assert app.panel_days[app.day_index].day == "2026-06-12"
    app.handle_key(None, 27)  # Esc -> back to the Daily chart, focused, cursor kept
    assert app.trends and app.trend_tabs[app.trend_tab] == "Daily"
    assert app.trend_focus and app.trend_cursor == "2026-06-12"
    app.trend_cursor = "2026-06-01"  # an empty day
    app.handle_key(None, 10)  # Enter nudges instead of drilling
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
    app.handle_key(None, 10)  # Enter -> focus the chart
    assert app.trend_bar_cursor() == "2026-04"  # the peak month
    app.handle_key(None, ot.curses.KEY_RIGHT)  # -> 2026-05 (empty, still walkable)
    assert app.trend_bar_cursor() == "2026-05"
    app.handle_key(None, 10)  # Enter on an empty month nudges, overlay stays
    assert app.trends and "no spend in 2026-05" in app.notice
    app.handle_key(None, ot.curses.KEY_RIGHT)
    app.handle_key(None, 10)  # Enter on June -> zoom that month
    assert not app.trends and app.view == "zoom" and app.focus == "months"
    assert app.months[app.month_index].month == "2026-06"
    app.handle_key(None, 27)  # Esc -> back to the Monthly chart
    assert app.trends and app.trend_tabs[app.trend_tab] == "Monthly" and app.trend_focus
    assert app.trend_cursor == "2026-06"


def test_trend_daily_marks_the_selected_bar_when_focused():
    app = app_with([workflow("a", "2026-06-10 12:00:00", cost=40)])
    app.handle_key(None, ord("T"))
    app.handle_key(None, 10)  # focus the Daily chart
    lines = app.renderer.trend_daily(100, 20)
    marked = next((ln for ln in lines if "▲" in ln), None)
    assert marked is not None and "2026-06-10" in marked and "$40.00" in marked
    app.trend_focus = False  # unfocused (or drawn outside the overlay): no cursor
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


def test_trend_models_rows_drill_into_sessions_and_a_session():
    # The ranked tabs are navigable: j/k pick a row, Enter lists its sessions
    # (range-scoped), Enter again jumps into the selected session itself, and the
    # Esc chain walks all the way back to the drill list.
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
    assert lines[0] == "# Sessions · openai/gpt-5"
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


def test_trend_drill_list_h_l_switch_tabs_instead_of_closing():
    # The reported trap: drill a model's sessions, jump into a session, Esc back to
    # the drill list, hit l -- the overlay used to close to the main view ("any
    # other key closes"). h/l must switch Trends tabs from inside a drill too.
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


def test_jk_navigates_the_prices_overlay():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("claude-opus-4-8", 5.0, 10),
            _model_row("gpt-5-codex", 2.0, 10),
            _model_row("claude-haiku-4-5", 1.0, 10),
        ]
    }
    app.handle_key(None, ord("P"))
    assert app.show_prices and app.prices_index == 0
    app.handle_key(None, ord("j"))
    assert app.prices_index == 1 and app.show_prices  # moves the cursor, stays open
    app.handle_key(None, ord("k"))
    assert app.prices_index == 0
    app.handle_key(None, ord("k"))  # floored at the top
    assert app.prices_index == 0
    app.handle_key(None, ord("G"))
    assert app.prices_index == 2  # last model (3 rows)
    app.handle_key(None, ord("j"))  # clamped at the last row
    assert app.prices_index == 2
    app.handle_key(None, ord("g"))
    assert app.prices_index == 0
    app.handle_key(None, ord("x"))  # unbound keys are swallowed, the table stays
    assert app.show_prices
    app.handle_key(None, ord("q"))  # closing is explicit (Esc/q/P)
    assert not app.show_prices


def _price_sort_app():
    # Spend order (gpt-5-mini > haiku > opus) is deliberately the reverse of the
    # list-price order (opus > haiku > gpt-5-mini) so a column sort visibly reorders.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("anthropic/claude-opus-4-8", 1.0, 10),  # priciest, least spend
            _model_row("openai/gpt-5-mini", 9.0, 10),  # cheapest, most spend
            _model_row("anthropic/claude-haiku-4-5", 5.0, 10),
        ]
    }
    return app


def test_prices_default_is_flat_cheapest_mix_first():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    assert app.prices_sort == "eff" and app.prices_view == "flat"
    # One ungrouped list, cheapest-for-your-mix first (the mix here is all input,
    # so eff equals the input rate): mini 0.25 < haiku 1.00 < opus 5.00.
    assert app.priced_model_names() == [
        "gpt-5-mini",
        "claude-haiku-4-5",
        "claude-opus-4-8",
    ]
    effs = [e.eff for e in app.priced_model_entries()]
    assert effs == sorted(effs)


def test_prices_family_view_groups_most_spend_first_eff_within():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    app.prices_view = "family"
    # Grouped by vendor family, families most-spend-first (openai's $9 beats
    # anthropic's $6), the default eff sort applying *within* each group.
    assert app.priced_model_names() == [
        "gpt-5-mini",
        "claude-haiku-4-5",
        "claude-opus-4-8",
    ]
    assert [e.group for e in app.priced_model_entries()] == ["openai", "anthropic", "anthropic"]


def test_prices_sort_picker_reorders_by_a_column():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    app.prices_view = "flat"  # so the column sort orders globally
    app.handle_key(None, ord("s"))  # opens the sort picker over the overlay
    assert app.sort_menu and app.sort_menu_options() == app.prices_sort_options
    app.sort_menu_index = app.prices_sort_options.index("input")
    app.handle_key(None, 10)  # Enter applies
    assert not app.sort_menu and app.prices_sort == "input"
    names = app.priced_model_names()
    # Priciest input first (the default numeric direction), spend no longer decides.
    assert names[0] == "claude-opus-4-8"
    assert [ot.model_price(n)[0] for n in names] == sorted(
        (ot.model_price(n)[0] for n in names), reverse=True
    )


def test_prices_header_click_sorts_then_flips_direction():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    app.prices_view = "flat"  # so the column sort orders globally
    app.apply_header_sort("output", "prices")  # first click sorts by output, high->low
    assert app.prices_sort == "output" and not app.prices_sort_reverse
    desc = [ot.model_price(n)[1] for n in app.priced_model_names()]
    assert desc == sorted(desc, reverse=True)
    app.apply_header_sort("output", "prices")  # re-click flips to low->high
    assert app.prices_sort == "output" and app.prices_sort_reverse
    asc = [ot.model_price(n)[1] for n in app.priced_model_names()]
    assert asc == sorted(asc)


def test_prices_header_marks_the_active_sort_column():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    # A price column sorts high->low by default, so its arrow points down.
    app.prices_sort, app.prices_sort_reverse = "output", False
    assert "output v" in app.renderer._price_header(28)
    app.prices_sort_reverse = True
    assert "output ^" in app.renderer._price_header(28)
    # model sorts a->z by default, so its arrow points up.
    app.prices_sort, app.prices_sort_reverse = "model", False
    assert "model ^" in app.renderer._price_header(28)


def test_prices_sort_is_persisted_in_state():
    app = _price_sort_app()
    app.prices_sort, app.prices_sort_reverse = "cache_write", True
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = _price_sort_app()
            assert restored.prices_sort == "eff"  # fresh app starts on the eff default
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg
    assert restored.prices_sort == "cache_write" and restored.prices_sort_reverse


def test_prices_heat_scales_each_column_cheap_to_expensive():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    r = app.renderer
    entries = app.priced_model_entries()
    ranges = r._price_column_ranges(entries)
    # ranges[0] is the eff column; the raw columns follow. Each normalizes over its
    # own positive rates: input (ranges[1]) spans mini..opus. The all-input mix here
    # makes eff equal the input rate, so its span matches.
    assert ranges[0] == (0.25, 5.0) and ranges[1] == (0.25, 5.0)
    levels = {e.bare: r._price_heat_level(e.price[0], ranges[1]) for e in entries}
    # Cheapest input -> coolest bucket (0), priciest -> hottest (top level).
    assert levels["gpt-5-mini"] == 0
    assert levels["claude-opus-4-8"] == ot.PRICE_HEAT_LEVELS - 1
    assert levels["gpt-5-mini"] < levels["claude-haiku-4-5"] < levels["claude-opus-4-8"]


def test_prices_heat_is_neutral_when_a_column_has_no_spread():
    # One model (or all-equal rates) -> no range to shade, so cells stay neutral.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {"a": [_model_row("anthropic/claude-opus-4-8", 5.0, 10)]}
    app.handle_key(None, ord("P"))
    r = app.renderer
    ranges = r._price_column_ranges(app.priced_model_entries())
    assert ranges == [None, None, None, None, None]
    assert r._price_heat_level(5.0, ranges[1]) is None


def test_canonical_model_folds_alias_spellings():
    # Route prefix ignored, dots == dashes, date pins and effort suffixes stripped.
    assert ot.canonical_model("anthropic/claude-sonnet-4.5") == "claude-sonnet-4-5"
    assert ot.canonical_model("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5"
    assert ot.canonical_model("gpt-4o-2024-08-06") == "gpt-4o"
    assert ot.canonical_model("gpt-5.2-xhigh") == ot.canonical_model("gpt-5.2")
    assert ot.canonical_model("gpt-5.1-codex-max-medium") == "gpt-5-1-codex-max"
    # Genuinely different models stay distinct.
    assert ot.canonical_model("gpt-5.2-codex") != ot.canonical_model("gpt-5.2")
    assert ot.canonical_model("gpt-5.2-pro") != ot.canonical_model("gpt-5.2")
    assert ot.canonical_model("claude-opus-4-6") != ot.canonical_model("claude-opus-4-5")
    # display_model keeps the id's own separator style, drops only the pins.
    assert ot.display_model("claude-sonnet-4.5") == "claude-sonnet-4.5"
    assert ot.display_model("claude-opus-4-5-20251101") == "claude-opus-4-5"
    assert ot.display_model("gpt-5.1-codex-max-xhigh") == "gpt-5.1-codex-max"


def test_effective_price_blends_mix_and_flags_missing_cache_read():
    # eff = the mix's shares priced at the model's rates, per 1M tokens.
    eff, approx = ot.effective_price((2.0, 10.0, 0.2, 0.4), (0.5, 0.5, 0.0, 0.0))
    assert eff == 6.0 and not approx
    # A cache-heavy mix is dominated by the cache-read rate.
    eff, approx = ot.effective_price((2.0, 10.0, 0.2, 0.4), (0.1, 0.0, 0.9, 0.0))
    assert abs(eff - 0.38) < 1e-9 and not approx
    # No cache-read rate on record: reads bill at the input rate, flagged approximate.
    eff, approx = ot.effective_price((2.0, 10.0, 0.0, 0.0), (0.1, 0.0, 0.9, 0.0))
    assert approx and abs(eff - 2.0) < 1e-9
    # An all-zero (genuinely free) rate is not approximate -- there is no gap to fill.
    eff, approx = ot.effective_price((0.0, 0.0, 0.0, 0.0), (0.5, 0.5, 0.0, 0.0))
    assert eff == 0.0 and not approx


def test_prices_dedupe_folds_alias_spellings_across_routes():
    # The same model reached as a dated id on one route and a dotted alias on
    # another is one row: routes and spend merge, the display name is the
    # most-used spelling with its date pin stripped.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("anthropic/claude-sonnet-4-5-20250929", 2.0, 100),
            _model_row("github-copilot/claude-sonnet-4.5", 1.0, 50),
        ]
    }
    entries = app.priced_model_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e.canon == "claude-sonnet-4-5" and e.bare == "claude-sonnet-4-5"
    assert set(e.routes) == {"anthropic", "github-copilot"}
    assert e.spend == 3.0 and e.share == 1.0
    assert e.price == ot.model_price("claude-sonnet-4-5")


def test_price_token_mix_folds_reasoning_into_output():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            {
                "model_name": "anthropic/claude-opus-4-8",
                "cost": 0.0,
                "tokens_total": 100,
                "input": 10,
                "reasoning": 5,
                "cache_read": 80,
                "cache_write": 0,
                "output": 5,
            },
            _model_row("ollama/llama3.1", 0.0, 1000),  # local usage never skews the mix
        ]
    }
    mix = app.price_token_mix()
    assert mix is not None
    shares, total = mix
    assert total == 100
    assert shares == (0.10, 0.10, 0.80, 0.0)  # reasoning bills as output
    # The intro block states the mix the eff column prices at.
    assert any("80.0% cacheR" in ln for ln in app.renderer.price_intro_lines())
    # No usage at all -> no mix to price.
    app._model_by_root = {}
    assert app.price_token_mix() is None


def test_prices_eff_and_missing_cache_cells_render():
    # Pure-text cell rendering over a stub entry: ~ marks the missing-cache-read
    # upper bound, the raw cache-read cell shows "—" (never a free-looking 0.00),
    # and the use column is a share bar + percent.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    entry = type(
        "E",
        (),
        {
            "bare": "x-model",
            "eff": 1.234,
            "approx": True,
            "share": 0.5,
            "price": (2.0, 10.0, 0.0, 0.0),
        },
    )()
    cells = app.renderer._price_raw_cells(entry)
    assert cells == ["2.00", "10.00", "—", "0.00"]
    assert app.renderer._price_eff_cell(entry) == "~1.23"
    core = app.renderer._price_core_text(entry, 12, 0.5)
    assert "~1.23" in core and "—" in core and "50%" in core
    entry.approx, entry.price = False, (2.0, 10.0, 0.2, 0.0)
    assert app.renderer._price_eff_cell(entry) == "1.23"
    assert app.renderer._price_raw_cells(entry)[2] == "0.20"


def test_prices_use_column_sorts_by_usage_share():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    app.apply_header_sort("use", "prices")
    assert app.prices_sort == "use" and not app.prices_sort_reverse
    # Equal token shares here, so the spend-descending tiebreak decides.
    assert app.priced_model_names() == ["gpt-5-mini", "claude-haiku-4-5", "claude-opus-4-8"]
    shares = [e.share for e in app.priced_model_entries()]
    assert shares == sorted(shares, reverse=True)


def test_price_model_sessions_aggregates_alias_spellings():
    # The Enter drill-in matches canonically, so a session that used the dotted
    # copilot spelling lists beside one that used the dashed direct id.
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", title="alpha", directory="/x"),
            workflow("b", "2026-06-02 09:00:00", title="beta", directory="/y"),
        ]
    )
    app._model_by_root = {
        "a": [_model_row("anthropic/claude-sonnet-4-5", 3.0, 50)],
        "b": [_model_row("github-copilot/claude-sonnet-4.5", 1.0, 20)],
    }
    rows = app.price_model_sessions("claude-sonnet-4-5")
    assert [w.id for w, _c, _t in rows] == ["a", "b"]  # both spellings, most spend first


def test_prices_group_by_family_dedupes_routes_and_tags_them():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("anthropic/claude-opus-4-8", 5.0, 100),
            _model_row("github-copilot/claude-haiku-4.5", 0.0, 100),  # copilot-routed Claude
            _model_row("anthropic/claude-haiku-4.5", 2.0, 100),  # the same model, direct
            _model_row("openai/gpt-5-mini", 1.0, 100),
        ]
    }
    app.handle_key(None, ord("P"))
    app.prices_view = "family"  # the grouped-by-vendor layout (default is flat)
    entries = app.priced_model_entries()
    # The two routes to claude-haiku-4.5 collapse to one deduped entry...
    assert [e.bare for e in entries].count("claude-haiku-4.5") == 1
    haiku = next(e for e in entries if e.bare == "claude-haiku-4.5")
    assert haiku.family == "anthropic"  # inferred from the name, not the route
    assert set(haiku.routes) == {"anthropic", "github-copilot"}
    # ...and a copilot-routed Claude groups under Anthropic, not its own route.
    assert {e.family for e in entries} == {"anthropic", "openai"}
    lines = app.renderer.price_table_lines(120)
    assert any(ln.startswith("▸ Anthropic") for ln in lines)
    assert any("copilot" in ln for ln in lines)  # github-copilot abbreviated in the tag


def test_prices_p_cycles_view_modes():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    assert app.prices_view == "flat"  # opens flat (no headers)
    assert not any(ln.startswith("▸ ") for ln in app.renderer.price_table_lines(120))
    app.handle_key(None, ord("p"))  # -> by vendor (grouped)
    assert app.prices_view == "family" and app.prices_index == 0
    assert any(ln.startswith("▸ ") for ln in app.renderer.price_table_lines(120))
    app.handle_key(None, ord("p"))  # -> by provider (still grouped, by route)
    assert app.prices_view == "provider"
    assert any(ln.startswith("▸ ") for ln in app.renderer.price_table_lines(120))
    app.handle_key(None, ord("p"))  # wraps back to flat
    assert app.prices_view == "flat"


def test_prices_provider_view_groups_by_route_and_tags_vendor():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("anthropic/claude-opus-4-8", 5.0, 100),
            _model_row("github-copilot/claude-haiku-4.5", 3.0, 100),  # Claude via a gateway
            _model_row("github-copilot/gpt-5-mini", 1.0, 100),  # GPT via the same gateway
        ]
    }
    app.handle_key(None, ord("P"))
    app.prices_view = "provider"
    lines = app.renderer.price_table_lines(120)
    # One header per access route; github-copilot carries models from >1 vendor.
    assert any(ln.startswith("▸ github-copilot") for ln in lines)
    assert any(ln.startswith("▸ anthropic") for ln in lines)
    # A gateway-routed GPT stays under github-copilot here (route, not vendor),
    # tagged with its vendor family instead of the route.
    copilot_rows = [e for e in app.priced_model_entries() if e.group == "github-copilot"]
    assert {e.bare for e in copilot_rows} == {"claude-haiku-4.5", "gpt-5-mini"}
    assert any("OpenAI" in ln or "Anthropic" in ln for ln in lines)  # vendor shown as the tag


def test_prices_filter_is_substring_not_fuzzy():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("claude-sonnet-4-5", 1.0, 10),
            _model_row("gpt-5-codex", 2.0, 10),
        ]
    }
    # A scattered-letter query ("gtex") subsequence-matches "gpt-5-codex" but is not a
    # substring, so the P filter (unlike the fuzzy session filter) must reject it.
    app.query = "gtex"
    assert app.priced_model_names() == []
    # A literal substring still matches.
    app.query = "codex"
    assert app.priced_model_names() == ["gpt-5-codex"]


def test_prices_enter_lists_sessions_that_used_the_model():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", title="alpha", directory="/x"),
            workflow("b", "2026-06-02 09:00:00", title="beta", directory="/y"),
            workflow("c", "2026-06-03 09:00:00", title="gamma", directory="/z"),
        ]
    )
    app._model_by_root = {
        "a": [_model_row("claude-opus-4-8", 5.0, 100)],
        "b": [_model_row("claude-opus-4-8", 3.0, 60), _model_row("gpt-5-codex", 1.0, 20)],
        "c": [_model_row("gpt-5-codex", 2.0, 40)],
    }
    app.handle_key(None, ord("P"))
    # Cheapest mix first: codex (fallback gpt-5 rate, 1.25 in) ahead of opus (5.00).
    assert app.priced_model_names() == ["gpt-5-codex", "claude-opus-4-8"]
    app.handle_key(None, ord("j"))  # select opus
    app.handle_key(None, 10)  # Enter drills into that model's sessions
    assert app.prices_model == "claude-opus-4-8"
    sessions = app.price_model_sessions("claude-opus-4-8")
    assert [w.id for w, _c, _t in sessions] == ["a", "b"]  # both used opus, most spend first
    assert "c" not in [w.id for w, _c, _t in sessions]  # c only used codex
    # The drill-in body lists those session titles (a's $5 ahead of b's $3), not gamma.
    body = app.renderer.price_session_lines("claude-opus-4-8", 80)
    assert "2 session(s)" in body[0] and "$8.00" in body[0]
    assert any("alpha" in ln for ln in body) and any("beta" in ln for ln in body)
    assert not any("gamma" in ln for ln in body)
    # Esc backs out to the model list; unbound keys are swallowed; q closes.
    app.handle_key(None, 27)
    assert app.prices_model is None and app.show_prices
    app.handle_key(None, ord("x"))
    assert app.show_prices
    app.handle_key(None, ord("q"))
    assert not app.show_prices


def test_terminal_resize_does_not_close_overlays():
    # A SIGWINCH (font/terminal resize) arrives as a KEY_RESIZE keystroke; it must not
    # be read as the "any other key closes" key that shuts an open overlay.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {"a": [_model_row("claude-opus-4-8", 5.0, 100)]}
    app.handle_key(None, ord("P"))
    app.handle_key(None, ot.curses.KEY_RESIZE)
    assert app.show_prices  # model list survives the resize
    app.handle_key(None, 10)  # drill into the model's sessions
    assert app.prices_model == "claude-opus-4-8"
    app.handle_key(None, ot.curses.KEY_RESIZE)
    assert app.show_prices and app.prices_model == "claude-opus-4-8"  # drill-in survives too
    # The help overlay (same close contract) is likewise immune.
    app.handle_key(None, 27)
    app.handle_key(None, ord("x"))
    app.handle_key(None, ord("?"))
    app.handle_key(None, ot.curses.KEY_RESIZE)
    assert app.help


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


def test_mouse_wheel_scrolls_the_help_overlay():
    # The wheel over the open help pages it (like the P overlay) instead of
    # closing it; a plain click still closes.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.handle_key(None, ord("?"))
    assert app.help and app.help_scroll == 0
    app._wheel_down = getattr(ot.curses, "BUTTON5_PRESSED", 0) or ot.curses.REPORT_MOUSE_POSITION
    orig = ot.curses.getmouse
    try:
        ot.curses.getmouse = lambda: (0, 0, 0, 0, app._wheel_down)  # wheel down
        app.handle_mouse()
        assert app.help and app.help_scroll == 3
        ot.curses.getmouse = lambda: (0, 0, 0, 0, ot.curses.BUTTON4_PRESSED)  # wheel up
        app.handle_mouse()
        assert app.help and app.help_scroll == 0
        app.handle_mouse()  # floored at the top, still open
        assert app.help and app.help_scroll == 0
        ot.curses.getmouse = lambda: (0, 0, 0, 0, ot.curses.BUTTON1_CLICKED)  # click closes
        app.handle_mouse()
        assert not app.help
    finally:
        ot.curses.getmouse = orig


def test_page_keys_stride_lists_by_half_a_screen():
    # PgDn/PgUp and Ctrl-D/Ctrl-U move by half the visible pager height; headless
    # (no screen to measure) the stride is a fixed 10 rows.
    app = app_with([workflow(f"s{i:02d}", "2026-06-01 12:00:00") for i in range(25)])
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    app.handle_key(None, ot.curses.KEY_NPAGE)
    assert app.workflow_index == 10
    app.handle_key(None, 4)  # Ctrl-D
    assert app.workflow_index == 20
    app.handle_key(None, ot.curses.KEY_NPAGE)  # clamped at the last row
    assert app.workflow_index == 24
    app.handle_key(None, ot.curses.KEY_PPAGE)
    assert app.workflow_index == 14
    app.handle_key(None, 21)  # Ctrl-U
    assert app.workflow_index == 4
    app.handle_key(None, 21)  # floored at the top
    assert app.workflow_index == 0
    # with a real screen the stride is half the pager height (height - 9)
    assert app._page_step(FakeScreen(29, 80)) == 10
    assert app._page_step(FakeScreen(5, 80)) == 1  # never 0 on a tiny window


def test_page_keys_scroll_the_detail_help_and_prices_pagers():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app.view = "session"
    app.handle_key(None, ot.curses.KEY_NPAGE)  # detail pager, via move()
    assert app.scroll == 10
    app.handle_key(None, 21)  # Ctrl-U back up
    assert app.scroll == 0
    app.handle_key(None, ord("?"))  # the help pager
    app.handle_key(None, 4)  # Ctrl-D
    assert app.help and app.help_scroll == 10
    app.handle_key(None, ot.curses.KEY_PPAGE)
    assert app.help and app.help_scroll == 0
    app.handle_key(None, ord("q"))  # close help (any other key)
    app.view = "browse"
    app._model_by_root = {
        "a": [
            _model_row("claude-opus-4-8", 5.0, 10),
            _model_row("gpt-5-codex", 2.0, 10),
            _model_row("claude-haiku-4-5", 1.0, 10),
        ]
    }
    app.handle_key(None, ord("P"))  # the P overlay's model cursor
    app.handle_key(None, ot.curses.KEY_NPAGE)
    assert app.show_prices and app.prices_index == 2  # clamped to the last of 3 rows
    app.handle_key(None, 21)
    assert app.show_prices and app.prices_index == 0


def test_help_sections_group_and_cover_the_keymap():
    # The help overlay is grouped; help_sections() is the content source of truth
    # (draw_help only wraps/colours it). Lock the sections and that the load-bearing
    # bindings are documented.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    sections = ot.Renderer(app).help_sections()
    assert [t for t, _ in sections] == [
        "Move around",
        "Scope & filter",
        "Sessions & projects",
        "Views & overlays",
        "Reload & quit",
    ]
    keys = set()
    for _title, rows in sections:
        for row in rows:
            assert len(row) >= 2 and row[0] and row[1]  # (key, summary, *notes)
            assert all(isinstance(note, str) and note for note in row[2:])
            keys.add(row[0])
    for binding in (
        "p / t",
        "Enter / +",
        "PgDn/PgUp",
        "R",
        "f or /",
        "b / B",
        "L",
        "T",
        "P",
        "$",
        "c",
        "C",
        "q",
    ):
        assert binding in keys, f"missing help entry for {binding}"


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
    # Trends is interactive, so a mistyped key must never tear it down: closing is
    # explicit (Esc, q, or T again); ? and P float their overlay above it instead,
    # every other unbound key is swallowed, and Ctrl-C still quits the app.
    # (C/c/D act from inside too -- covered by their own tests below.)
    app = app_with([workflow("a", "2026-06-10 12:00:00", cost=5)])
    app._models_loaded = True  # keep ? / P cheap (skip the deferred scan)
    app.handle_key(None, ord("T"))
    for key in (ord("e"), ord("R"), ord("x"), ord("o"), ord("b")):
        app.handle_key(None, key)
        assert app.trends, f"key {chr(key)!r} closed the overlay"
    app.handle_key(None, ord("?"))  # help floats above Trends...
    assert app.help and app.trends
    app.handle_key(None, ord("x"))  # ...and closing it lands back on Trends
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


def test_prices_overlay_close_only_on_esc_q_or_P():
    # The P overlay gets the same explicit-close policy as Trends: unbound keys are
    # swallowed, ? floats help above it, $ re-prices in place, Esc/q/P close it,
    # and inside the per-model drill q closes while Esc only backs out.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {"a": [_model_row("claude-opus-4-8", 5.0, 10)]}
    app._models_loaded = True  # keep $'s deferred scan from wiping the fixture rows
    app.handle_key(None, ord("P"))
    for key in (ord("x"), ord("T"), ord("b")):
        app.handle_key(None, key)
        assert app.show_prices, f"key {chr(key)!r} closed the P overlay"
    app.handle_key(None, ord("?"))  # help floats above...
    assert app.help and app.show_prices
    app.handle_key(None, ord("x"))  # ...and closing it lands back on the table
    assert not app.help and app.show_prices
    app.handle_key(None, ord("$"))  # $ re-prices in place, the table stays
    assert app.show_api_prices and app.show_prices
    app.handle_key(None, ord("P"))  # P again closes (a toggle)
    assert not app.show_prices
    app.handle_key(None, ord("P"))
    assert app.handle_key(None, 3) is False  # Ctrl-C still quits from inside
    app.handle_key(None, 10)  # Enter -> the model's sessions (overlay still open)
    assert app.prices_model == "claude-opus-4-8"
    app.handle_key(None, ord("x"))  # swallowed, the list stays
    assert app.show_prices and app.prices_model is not None
    app.handle_key(None, ord("q"))  # q closes the whole overlay from the drill
    assert not app.show_prices and app.prices_model is None


def test_theme_picker_opens_from_inside_overlays():
    # C works from anywhere: inside Trends and P it floats the Colours picker above
    # the overlay (which stays open as the live-preview swatch), the picker owns the
    # keys while it's up, and Esc reverts + lands back on the overlay.
    app = app_with([workflow("a", "2026-06-10 12:00:00", cost=5)])
    app._models_loaded = True
    app.handle_key(None, ord("T"))
    app.handle_key(None, ord("C"))
    assert app.theme_menu and app.trends
    before = app.theme_id
    app.handle_key(None, ord("j"))  # the picker sees the keys, not the Trends tabs
    assert app.theme_id != before and app.trend_tab == 0
    app.handle_key(None, 27)  # Esc reverts the preview and closes just the picker
    assert not app.theme_menu and app.theme_id == before and app.trends
    app.handle_key(None, ord("q"))
    assert not app.trends
    app.handle_key(None, ord("P"))  # same from inside the P overlay
    app.handle_key(None, ord("C"))
    assert app.theme_menu and app.show_prices
    app.handle_key(None, 10)  # Enter keeps the highlighted theme, back to the table
    assert not app.theme_menu and app.show_prices


def test_theme_picker_floats_above_help():
    # Help closes on any unbound key, but C is the exception: the picker floats
    # above it (help is the swatch background) and Esc closes only the picker.
    app = app_with([workflow("a", "2026-06-10 12:00:00")])
    app.handle_key(None, ord("?"))
    assert app.help
    app.handle_key(None, ord("C"))
    assert app.theme_menu and app.help
    app.handle_key(None, 27)
    assert not app.theme_menu and app.help


def test_source_and_demo_toggles_route_from_inside_overlays():
    # c and D are overlay-wide too: from inside Trends or P they open the source
    # picker / swap demo data instead of being swallowed, and the overlay stays up.
    app = app_with([workflow("a", "2026-06-10 12:00:00", cost=5)])
    app._models_loaded = True
    calls = []
    app.open_source_menu = lambda: calls.append("source")  # bare Args has no flags
    app.toggle_demo = lambda: calls.append("demo")
    app.handle_key(None, ord("T"))
    app.handle_key(None, ord("c"))
    app.handle_key(None, ord("D"))
    assert calls == ["source", "demo"] and app.trends
    app.handle_key(None, ord("q"))
    app.handle_key(None, ord("P"))
    app.handle_key(None, ord("c"))
    app.handle_key(None, ord("D"))
    assert calls == ["source", "demo", "source", "demo"] and app.show_prices


def test_data_swap_reanchors_overlay_cursors():
    # A source switch / demo toggle replaces the dataset, so every overlay cursor
    # that pointed into the old one (a drilled model, a bar cursor, the P drill)
    # re-anchors instead of dangling; the overlays themselves stay open.
    app = app_with([workflow("a", "2026-06-10 12:00:00", cost=5)])
    app.trends = True
    app.trend_drill = ("model", "anthropic/gone")
    app.trend_drill_index = 3
    app.trend_row_index = 2
    app.trend_cursor = "2026-06-10"
    app.cal_cursor = "2026-06-10"
    app.trend_month_index = 1
    app.prices_model = "anthropic/gone"
    app.prices_index = 4
    app._reload_for_source()
    assert app.trends  # the overlay survives the swap
    assert app.trend_drill is None and app.trend_drill_index == 0
    assert app.trend_row_index == 0 and app.trend_cursor is None
    assert app.cal_cursor is None and app.trend_month_index == 0
    assert app.prices_model is None and app.prices_index == 0


def test_footer_highlights_the_focused_time_panel():
    # The "Tab yr/mo/day" footer hint doubles as a position indicator: the token of
    # the focused sidebar panel lights up in the accent as Tab moves between them.
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00"),
            workflow("b", "2025-06-01 12:00:00"),  # two years, so Years is a panel
        ]
    )
    app.can_switch_source = lambda: False  # the bare test Args has no source flags
    app.renderer.hline = lambda *a: None  # ACS_HLINE needs initscr; skip the separator
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: n  # identity so we can read the pair off the attr
    ot.curses.init_pair = lambda *a: None
    try:

        def token_attrs(focus):
            app.focus = focus
            scr = AttrScreen(24, 120)
            app.renderer.draw_footer(scr, 24, 120)
            row = 23
            line = "".join(scr.cells.get((row, x), " ") for x in range(120))
            i = line.index("Tab yr/mo/day")
            return {
                "yr": scr.attrs[(row, i + 4)],
                "mo": scr.attrs[(row, i + 7)],
                "day": scr.attrs[(row, i + 10)],
            }

        accent = 6 | ot.curses.A_BOLD
        a = token_attrs("months")
        assert a["mo"] == accent and a["yr"] == 4 and a["day"] == 4
        a = token_attrs("days")
        assert a["day"] == accent and a["mo"] == 4
        a = token_attrs("years")
        assert a["yr"] == accent and a["day"] == 4

        # The p/t hint mirrors the idea for the browse mode; and the footer stays
        # lean -- sort/export/open live in the help overlay, not down here.
        def footer_line():
            scr = AttrScreen(24, 120)
            app.renderer.draw_footer(scr, 24, 120)
            return scr, "".join(scr.cells.get((23, x), " ") for x in range(120))

        scr, line = footer_line()
        for gone in ("s sort", "e export", "o open"):
            assert gone not in line
        i = line.index("p/t mode")
        assert scr.attrs[(23, i + 2)] == accent and scr.attrs[(23, i)] == 4  # time mode: t lit
        app.browse_mode = "projects"
        scr, line = footer_line()
        i = line.index("p/t mode")
        assert scr.attrs[(23, i)] == accent and scr.attrs[(23, i + 2)] == 4  # projects: p lit
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip


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


def test_tab_click_in_browse_preview_zooms_into_the_detail():
    # Clicking a tab in the right preview pane moves the focus there: the browse
    # view zooms into the selected scope and lands on that tab, so j/k drive the
    # detail the user clicked instead of the still-active left list.
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00"),
            workflow("b", "2026-06-01 13:00:00"),
        ]
    )
    assert app.view == "browse" and app.focus == "days"
    sessions = app.current_tabs().index("Sessions")
    app._apply_click(("tab", sessions), drill=False)
    assert app.view == "zoom" and app.tab == sessions
    app.handle_key(None, ord("j"))  # keys now drive the zoomed detail...
    assert app.workflow_index == 1
    app.handle_key(None, 27)  # ...and Esc steps back out to browse
    assert app.view == "browse"


def test_plus_drills_from_browse_and_toggles_maximize_in_zoom():
    # + keeps its browse meaning (an Enter alias), and once the detail is the
    # active pane it becomes lazygit's screen-mode key: split <-> full-screen.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert not app.zoom_maximized  # the split is the default
    app.handle_key(None, ord("+"))
    assert app.view == "zoom" and not app.zoom_maximized
    app.handle_key(None, ord("+"))
    assert app.zoom_maximized and "maximized" in app.notice
    app.handle_key(None, ord("+"))
    assert not app.zoom_maximized
    app.zoom_maximized = True
    app.handle_key(None, 27)  # Esc out; the pref survives the next drill-in
    app.handle_key(None, 10)
    assert app.view == "zoom" and app.zoom_maximized


def test_sidebar_click_rescopes_the_zoomed_detail():
    # The split keeps the sidebar clickable while the detail is the active pane:
    # a row click re-scopes the zoom in place, keeping the tab across sibling
    # scopes (the web's sidebar rule), and a double-click must not fall through
    # to "open the selected session" on a Sessions tab.
    app = app_with(
        [
            workflow("jun", "2026-06-01 12:00:00"),
            workflow("may", "2026-05-01 12:00:00"),
        ]
    )
    app.focus = "months"
    app.handle_key(None, 10)  # zoom into the selected month
    assert app.view == "zoom"
    sessions = app.current_tabs().index("Sessions")
    app.tab = sessions
    app._apply_click(("month", 1), drill=True)  # double-click the other month
    assert app.view == "zoom" and app.month_index == 1
    assert app.tab == sessions and app.workflow_index == 0
    app._apply_click(("day", 0), drill=False)  # a day row switches the level too
    assert app.view == "zoom" and app.focus == "days" and app.tab == 0


def test_click_anywhere_in_the_preview_pane_focuses_it():
    # The browse preview registers a catch-all region after its real ones, so a
    # click on empty pane space focuses (zooms) it while tab clicks still win.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    r = app.renderer
    r.regions = [("tab", 4, 30, 40, 1), ("rows", "detail", 3, 20, 28, 100, 0)]
    assert r.hit(4, 35) == ("tab", 1)  # first match wins: the tab, not the pane
    assert r.hit(10, 50) == ("detail", 7)
    app._apply_click(("detail", 7), drill=False)
    assert app.view == "zoom"  # a click on empty preview space focuses the pane
    app._apply_click(("detail", 7), drill=False)
    assert app.view == "zoom"  # already focused: inert


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


def test_zoom_maximized_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.zoom_maximized = True
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert not restored.zoom_maximized  # the split is the fresh default
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg
    assert restored.zoom_maximized


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


def test_normalize_project_path_canonicalizes_windows_drive_paths():
    n = ot.normalize_project_path
    # OpenCode's forward-slash spelling and a native backslash spelling of the SAME
    # directory must collapse to one canonical form (issue #4).
    assert n("C:/DEV/Agentic-Coding/examples/okf") == r"C:\DEV\Agentic-Coding\examples\okf"
    assert n(r"C:\DEV\Agentic-Coding\examples\okf") == r"C:\DEV\Agentic-Coding\examples\okf"
    assert n("C:/DEV/app") == n(r"C:\DEV\app")
    # drive letter is case-insensitive; trailing and doubled separators collapse
    assert n("c:/dev/app") == r"C:\dev\app"
    assert n("C:/DEV//okf/") == r"C:\DEV\okf"
    assert n("C:/") == "C:\\" and n("C:\\") == "C:\\"
    # POSIX paths (incl. a literal backslash in a name), tilde, agent names, and the
    # "(unknown)" sentinel are NOT drive paths -- returned untouched.
    for p in ("/home/mo/proj", "~/code/opentab", "/weird/na\\me", "finance-os", "(unknown)"):
        assert n(p) == p
    # idempotent
    assert n(n("C:/DEV/app")) == n("C:/DEV/app")


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


def test_ignored_sessions_are_filtered_but_can_be_shown_and_unignored():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1),
            workflow("b", "2026-06-01 13:00:00", cost=5),
        ]
    )
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    app.workflow_index = next(i for i, w in enumerate(app.current_sessions()) if w.id == "b")

    assert app.handle_key(None, ord("i"))

    assert app.ignored_sessions == {"b"}
    assert [w.id for w in app.all_workflows] == ["a"]
    assert app.months[0].cost == 1
    assert [w.id for w in app.current_sessions()] == ["a"]

    assert app.handle_key(None, ord("I"))
    assert {w.id for w in app.current_sessions()} == {"a", "b"}

    app.workflow_index = next(i for i, w in enumerate(app.current_sessions()) if w.id == "b")
    assert app.handle_key(None, ord("i"))
    assert app.ignored_sessions == set()
    assert sum(w.total_cost for w in app.all_workflows) == 6


def test_ignored_sessions_stay_hidden_in_project_mode_until_shown():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, directory="/repo/app"),
            workflow("b", "2026-06-01 13:00:00", cost=5, directory="/repo/app"),
        ]
    )
    app.ignored_sessions = {"b"}
    app._invalidate_workflow_cache()
    app.browse_mode = "projects"

    assert [w.id for w in app.current_sessions()] == ["a"]

    app.handle_key(None, ord("I"))

    assert {w.id for w in app.current_sessions()} == {"a", "b"}


def test_ignored_session_detail_drills_out_when_hidden():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1),
            workflow("b", "2026-06-01 13:00:00", cost=5),
        ]
    )
    app.focus = "months"
    app.view = "session"
    app.workflow_index = next(i for i, w in enumerate(app.current_sessions()) if w.id == "b")

    assert app.handle_key(None, ord("i"))

    assert app.ignored_sessions == {"b"}
    assert app.view == "zoom"
    assert [w.id for w in app.current_sessions()] == ["a"]


def test_ignored_sessions_are_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.ignored_sessions = {"a", "missing"}
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    assert restored.ignored_sessions == {"a", "missing"}
    assert restored.all_workflows == []


def test_bookmark_toggles_on_selected_session():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1),
            workflow("b", "2026-06-01 13:00:00", cost=5),
        ]
    )
    # No session is selected while browsing the time panels, so `b` explains itself.
    assert app.handle_key(None, ord("b"))
    assert app.bookmarks == set()
    assert "select a session" in app.notice

    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    app.workflow_index = next(i for i, w in enumerate(app.current_sessions()) if w.id == "b")
    assert app.handle_key(None, ord("b"))
    assert app.bookmarks == {"b"}
    assert app.handle_key(None, ord("b"))  # same key unstars
    assert app.bookmarks == set()


def test_bookmark_toast_ignores_error_words_in_the_title():
    # The toast kind must never be inferred from user data: a session titled
    # "… backup failure analysis" used to paint the bookmark confirmation as
    # a red "✕ Error" card because the title matched the "fail" marker.
    app = app_with(
        [workflow("a", "2026-06-01 12:00:00", title="Vzdump snapshot backup failure analysis")]
    )
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    app.workflow_index = 0
    assert app.handle_key(None, ord("b"))
    assert app.notice.startswith("bookmarked ")
    assert app.toasts[-1].kind == "info"
    app._mark_toasts_shown()
    assert app.handle_key(None, ord("b"))
    assert app.notice.startswith("unbookmarked ")
    assert app.toasts[-1].kind == "info"


def test_bookmarks_view_narrows_every_list_to_starred_sessions():
    app = app_with(
        [
            workflow("a", "2026-05-01 12:00:00", cost=1, directory="/repo/a"),
            workflow("b", "2026-06-02 12:00:00", cost=5, directory="/repo/b"),
        ]
    )
    assert app.handle_key(None, ord("B"))  # nothing starred yet: a no-op with a hint
    assert not app.show_bookmarks_only
    assert "no bookmarks" in app.notice

    app.bookmarks = {"b"}
    assert app.handle_key(None, ord("B"))
    assert app.show_bookmarks_only
    assert [w.id for w in app.all_workflows] == ["b"]
    assert [m.month for m in app.months] == ["2026-06"]
    assert [p.directory for p in app.projects] == ["/repo/b"]

    assert app.handle_key(None, ord("B"))  # back to everything
    assert not app.show_bookmarks_only
    assert {w.id for w in app.all_workflows} == {"a", "b"}


def test_removing_last_bookmark_exits_the_bookmarks_view():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1),
            workflow("b", "2026-06-01 13:00:00", cost=5),
        ]
    )
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    app.bookmarks = {"b"}
    app.show_bookmarks_only = True
    assert [w.id for w in app.current_sessions()] == ["b"]

    assert app.handle_key(None, ord("b"))  # unstar the only bookmark

    assert app.bookmarks == set()
    assert not app.show_bookmarks_only
    assert "showing all sessions" in app.notice
    assert {w.id for w in app.current_sessions()} == {"a", "b"}


def test_unstarring_last_bookmark_keeps_the_open_session_selected():
    # Dropping the B filter widens the list back out; the cursor (and an open
    # session detail) must stay on the just-unstarred session, not jump to
    # whatever now sorts first.
    app = app_with(
        [
            workflow("expensive", "2026-06-01 12:00:00", cost=50),
            workflow("cheap", "2026-06-01 13:00:00", cost=1),
        ]
    )
    app.focus = "months"
    app.view = "session"  # drilled into the only (starred) session
    app.bookmarks = {"cheap"}
    app.show_bookmarks_only = True
    assert app.current_session().id == "cheap"

    assert app.handle_key(None, ord("b"))  # unstar the last bookmark

    assert not app.show_bookmarks_only
    assert app.current_session().id == "cheap"


def test_bookmarks_are_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.bookmarks = {"a", "gone-session"}  # a stale id survives too (source may return)
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    assert restored.bookmarks == {"a", "gone-session"}
    assert not restored.show_bookmarks_only  # the B view itself always starts off


def test_bookmarked_rows_wear_a_star_in_the_sessions_picker():
    app = app_with(
        [
            workflow("plain", "2026-06-01 12:00:00", cost=5),
            workflow("starred", "2026-06-01 13:00:00", cost=1),
        ]
    )
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    app.bookmarks = {"starred"}
    screen = FakeScreen(24, 100)
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: 0
    ot.curses.init_pair = lambda *a: None
    try:
        app.renderer.draw_sessions_picker(screen, 0, 0, 24, 100)
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    lines = screen_text(screen).splitlines()
    assert any("★ starred" in ln for ln in lines)  # the starred row wears the marker
    assert not any("★" in ln and "plain" in ln for ln in lines)  # the other doesn't


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
    assert "Project" in header  # the column header sits between Subs and Title
    assert header.index("Subs") < header.index("Project") < header.index("Title")
    assert any("alpha" in ln and "first" in ln for ln in lines)  # each row names its project
    assert any("beta" in ln and "second" in ln for ln in lines)


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


def test_sessions_sort_by_project_groups_sessions_by_root():
    app = app_with(
        [
            workflow("b-cheap", "2026-06-01 12:00:00", cost=1, directory="/tmp/beta"),
            workflow("a", "2026-06-02 12:00:00", cost=2, directory="/tmp/alpha"),
            workflow("b-costly", "2026-06-03 12:00:00", cost=9, directory="/tmp/beta"),
        ]
    )
    app.sort_by = "project"
    rows = app.sorted_workflows(app.loaded)
    # a->z by project, costliest session first within each project.
    assert [w.id for w in rows] == ["a", "b-costly", "b-cheap"]
    app.sort_reverse = True  # a header re-click flips to z->a
    rows = app.sorted_workflows(app.loaded)
    assert [w.id for w in rows] == ["b-cheap", "b-costly", "a"]


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
        assert "· Grouped by the user prompt" in joined
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


def test_claude_turns_carry_the_full_prompt_uncapped():
    # The Turns tab can unfold a prompt, so the timeline keeps its whole text: the
    # one-line group title stays capped, prompt_full is the raw prompt (line breaks
    # kept), and the session-title fallback stays short.
    long_prompt = ("please refactor the frobnicator carefully " * 6).strip() + "\nkeep tests green"
    assert len(long_prompt) > 200
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        rows_in = [
            {
                "type": "user",
                "sessionId": "s1",
                "cwd": cwd,
                "timestamp": "2026-06-10T18:46:00.000Z",
                "uuid": "ua",
                "message": {"role": "user", "content": long_prompt},
            },
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(100, 50),
                uuid="a1",
                cwd=cwd,
                ts="2026-06-10T18:46:05.000Z",
            ),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows_in)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        w = store.workflows()[0]
        assert w.title == long_prompt[:80]  # the session-title fallback stays short
        rows = store.message_timeline("s1")
        assert rows[0]["prompt_full"] == long_prompt  # uncapped, newline kept
        assert rows[0]["prompt_title"] == " ".join(long_prompt.split())[:160]


def _write_opencode_db_with_long_prompt(path, long_prompt):
    conn = sqlite3.connect(path)
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
    conn.execute(
        "insert into session values (?,?,?,?,?,?)",
        ("s1", None, "Root", "/work/repo", None, 1760000000000),
    )
    user = {"role": "user", "time": {"created": 500}}
    part = {"type": "text", "text": long_prompt}
    turn = {
        "role": "assistant",
        "providerID": "anthropic",
        "modelID": "claude-opus-4-8",
        "cost": 2.0,
        "time": {"created": 1000},
        "tokens": {"input": 100, "output": 10},
    }
    conn.executemany(
        "insert into message values (?,?,?)",
        [("u1", "s1", json.dumps(user)), ("m1", "s1", json.dumps(turn))],
    )
    conn.execute("insert into part values (?,?,?,?)", ("p1", "u1", "s1", json.dumps(part)))
    conn.commit()
    conn.close()


def test_opencode_turns_carry_the_full_prompt_uncapped():
    # No summary.title on the user message: the one-line group title is the capped
    # raw prompt, prompt_full the whole thing with its line breaks kept.
    long_prompt = ("rework the cache invalidation and explain the tradeoffs " * 5).strip()
    long_prompt += "\nthen run the whole suite"
    assert len(long_prompt) > 200
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_long_prompt(db, long_prompt)
        store = ot.Store(db, type("A", (), {"demo": False})())
        rows = store.message_timeline("s1")
        assert rows[0]["prompt_full"] == long_prompt
        assert rows[0]["prompt_title"] == " ".join(long_prompt.split())[:160]

        # The TUI unfolds it: z flips every ▸ header to ▾ + the wrapped whole text,
        # and a click on one header (the "turnline" region) toggles just that group.
        app = ot.App(store, args())
        rnd = app.renderer  # the instance _apply_click resolves headers against
        wf = app.loaded[0]
        folded = rnd.detail_turns(wf, 96)
        assert any(ln.startswith("▸ ") for ln in folded)
        assert not any(ln.startswith("  │") for ln in folded)
        app.turns_full = True
        unfolded = rnd.detail_turns(wf, 96)
        assert any(ln.startswith("▾ ") for ln in unfolded)
        body = " ".join(ln[4:] for ln in unfolded if ln.startswith("  │"))
        assert "then run the whole suite" in body  # the tail survived the unfold
        assert " ".join(long_prompt.split()) == " ".join(body.split())  # nothing lost
        # Click-toggle one group while the global fold is off.
        app.turns_full = False
        rnd.detail_turns(wf, 96)  # a paint pass records the header line indices
        idx, pid = next(iter(rnd._turn_header_at.items()))
        app._apply_click(("turnline", idx), drill=False)
        assert pid in app._turns_expanded
        assert any(ln.startswith("▾ ") for ln in rnd.detail_turns(wf, 96))
        app._apply_click(("turnline", idx), drill=False)  # toggles back off
        assert pid not in app._turns_expanded


def test_demo_turns_anonymize_the_full_prompt_too():
    # Demo must never leak a real prompt through the expandable full text: both the
    # title and prompt_full become the same stable fake.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.store.demo_scale = 0.5
    rows = app._scale_demo_turns(
        "a",
        [
            {
                "model_name": "anthropic/claude-opus-4-8",
                "prompt_id": "p1",
                "prompt_title": "company secret plan",
                "prompt_full": "company secret plan\nwith all the details",
                "cost": 1.0,
                "tokens_total": 10,
                "input": 10,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
            }
        ],
    )
    assert "secret" not in rows[0]["prompt_title"] and "secret" not in rows[0]["prompt_full"]
    assert rows[0]["prompt_full"] == rows[0]["prompt_title"]  # the fake, twice


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


def test_price_table_omits_local_models():
    # The P overlay is a reference for API list prices behind the "$" what-if. Local
    # models (ollama/mlx/…) have no rate, so they're dropped here -- their usage still
    # shows in the Models/Trends views. A mixed set keeps only the priced models.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("ollama/llama3.1", 0.0, 1000),
            _model_row("anthropic/claude-opus-4-8", 5.0, 1000),
        ]
    }
    assert app.priced_model_names() == ["claude-opus-4-8"]  # deduped to the bare id
    lines = app.renderer.price_table_lines(80)
    assert any("claude-opus-4-8" in ln for ln in lines)
    assert not any("ollama" in ln for ln in lines)
    # Local-only usage leaves nothing to price.
    app._model_by_root = {"a": [_model_row("ollama/llama3.1", 0.0, 1000)]}
    assert app.priced_model_names() == []
    assert any("No model usage" in ln for ln in app.renderer.price_table_lines(80))


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


def test_price_refresh_while_api_view_applied_does_not_compound_estimate():
    # Repro for the P->r doubling: refresh_prices_action recomputes the API costs
    # while _apply_price_mode has already swapped the estimate into the live cost
    # fields ($ starts on for cost-less backends). The recompute must build from
    # the real snapshots, not add a fresh estimate on top of the previous one.
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

    app._model_by_root = {
        "r": [
            row("anthropic/claude-opus-4-6", 10.0, 0),
            row("github-copilot/claude-haiku-4.5", 0.0, 1_000_000),
        ]
    }
    app._compute_api_costs()
    app.toggle_api_prices()
    assert round(app.loaded[0].total_cost, 2) == 11.0  # $10 real + $1 estimate

    # What refresh_prices_action does after the fetch, $ view still applied.
    for _ in range(3):
        app._compute_api_costs()
        app._apply_price_mode()

    assert round(app.loaded[0].total_cost, 2) == 11.0  # unchanged, not 12/13/14
    assert round(app.loaded[0].root_cost, 2) == 11.0
    costs = {m["model_name"]: m["cost"] for m in app.model_mix("r")}
    assert costs["github-copilot/claude-haiku-4.5"] == 1.0
    assert costs["anthropic/claude-opus-4-6"] == 10.0
    app.toggle_api_prices()  # $ off still restores the true real cost
    assert app.loaded[0].total_cost == 10.0
    assert {m["model_name"]: m["cost"] for m in app.model_mix("r")}[
        "github-copilot/claude-haiku-4.5"
    ] == 0.0


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

    # On a non-sortable tab the picker won't open and the sort is untouched.
    assert app.handle_key(None, ord("s"))
    assert not app.sort_menu
    assert app.sort_by == "cost"

    # On the Sessions tab `s` opens the picker; navigate + Enter applies the choice.
    app.tab = app.month_tabs.index("Sessions")
    assert app.handle_key(None, ord("s"))
    assert app.sort_menu and app.sort_menu_index == 0  # starts on the current sort (cost)
    app.handle_key(None, ord("j"))  # -> tokens
    app.handle_key(None, 10)  # Enter applies
    assert not app.sort_menu
    assert app.sort_by == "tokens"


def test_sort_menu_is_navigable_with_jk_s_and_enter():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.focus = "months"
    app.view = "browse"
    app.tab = app.month_tabs.index("Sessions")
    app.sort_by = "cost"
    n = len(app.sort_options)

    app.handle_key(None, ord("s"))
    assert app.sort_menu and app.sort_menu_index == 0
    app.handle_key(None, ord("s"))  # `s` again advances the highlight
    assert app.sort_menu_index == 1
    app.handle_key(None, ord("k"))  # back to 0
    app.handle_key(None, ord("k"))  # wraps up to the last option
    assert app.sort_menu_index == n - 1
    app.handle_key(None, ord("g"))  # jump to top
    assert app.sort_menu_index == 0
    app.handle_key(None, ord("G"))  # jump to bottom
    assert app.sort_menu_index == n - 1
    app.handle_key(None, 10)  # Enter applies the highlighted option
    assert not app.sort_menu and app.sort_by == app.sort_options[-1]


def test_shift_s_opens_the_sort_picker_too():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.focus = "months"
    app.view = "browse"
    app.tab = app.month_tabs.index("Sessions")
    app.sort_by = "tokens"

    assert app.handle_key(None, ord("S"))
    assert app.sort_menu
    app.handle_key(None, 27)  # Esc cancels, sort unchanged
    assert not app.sort_menu and app.sort_by == "tokens"


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


def test_project_list_s_opens_project_sort_picker():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.set_browse_mode("projects")

    assert app.handle_key(None, ord("s"))
    assert app.sort_menu
    assert app.sort_menu_options() == app.project_sort_options
    assert app.sort_menu_index == 0  # current project sort is cost
    app.handle_key(None, ord("j"))  # -> tokens
    app.handle_key(None, 10)  # Enter applies
    assert not app.sort_menu
    assert app.project_sort_by == "tokens"
    assert app.sort_by == "cost"  # session sort untouched


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


def test_clicking_a_column_header_sorts_by_that_column():
    app = app_with(
        [workflow("a", "2026-06-01 12:00:00", cost=12.34, tokens=1500, directory="/tmp/project")]
    )
    app.set_browse_mode("projects")
    rnd = app.renderer
    header = rnd.project_header_text(80)
    rnd.sort_regions = []
    rnd._register_sort_header(2, 1, header, rnd.PROJECT_SORT_COLUMNS, "project", 80)
    # Each label word resolves to its sort key (x_base=1, so screen x = 1 + offset).
    assert rnd.sort_hit(2, 1 + header.index("Tokens")) == ("tokens", "project")
    assert rnd.sort_hit(2, 1 + header.index("Cost")) == ("cost", "project")
    assert rnd.sort_hit(2, 1 + header.index("Subs")) == ("subagents", "project")
    # A different row, or the leading marker gutter, is not a column label.
    assert rnd.sort_hit(3, 1 + header.index("Cost")) is None
    assert rnd.sort_hit(2, 1) is None


def test_apply_header_sort_targets_the_clicked_list():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, directory="/tmp/a"),
            workflow("b", "2026-06-02 12:00:00", cost=5, directory="/tmp/a"),
        ]
    )
    app.workflow_index = 1
    app.apply_header_sort("tokens", "session")  # a session-header click
    assert app.sort_by == "tokens" and app.workflow_index == 0

    app.project_index = 2
    app.apply_header_sort("project", "project")  # a project-header click
    assert app.project_sort_by == "project" and app.project_index == 0

    # An unknown key for the target is ignored rather than corrupting the sort.
    app.apply_header_sort("bogus", "session")
    assert app.sort_by == "tokens"


def test_mouse_click_on_column_header_applies_the_sort():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    # Stand in for what a draw() registered: a "Tokens" header zone above the rows.
    app.renderer.sort_regions = [(4, 10, 15, "tokens", "session")]
    app.renderer.regions = [("rows", "session", 5, 9, 0, 30, 0)]
    orig = ot.curses.getmouse
    try:
        ot.curses.getmouse = lambda: (0, 12, 4, 0, ot.curses.BUTTON1_CLICKED)
        assert app.handle_mouse()
        assert app.sort_by == "tokens"  # the header click sorted, didn't select a row
        assert app.workflow_index == 0
    finally:
        ot.curses.getmouse = orig


def test_clicking_active_column_header_toggles_direction():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1.0, tokens=50),
            workflow("b", "2026-06-02 12:00:00", cost=5.0, tokens=10),
        ]
    )
    # Click a column that isn't the current sort -> its natural order (tokens high->low).
    app.apply_header_sort("tokens", "session")
    assert app.sort_by == "tokens" and app.sort_reverse is False
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["a", "b"]
    # Re-clicking the active column flips it to ascending.
    app.apply_header_sort("tokens", "session")
    assert app.sort_reverse is True
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["b", "a"]
    # Clicking a different column resets to that column's natural order.
    app.apply_header_sort("title", "session")
    assert app.sort_by == "title" and app.sort_reverse is False


def test_header_arrow_reflects_sort_direction():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/tmp/a")])
    app.set_browse_mode("projects")
    rnd = app.renderer
    assert "Cost v" in rnd.project_header_text(80)  # default cost sort, descending
    app.apply_header_sort("cost", "project")  # active column -> flip to ascending
    assert "Cost ^" in rnd.project_header_text(80)


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

    assert app.handle_key(None, ord("s"))  # opens the session-sort picker
    assert app.sort_menu and app.sort_menu_options() == app.sort_options
    app.handle_key(None, ord("j"))  # cost -> tokens
    app.handle_key(None, 10)  # Enter applies
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
    # every priced row carries share/eff/approx and four numeric rates
    assert all(len(r) == 10 and all(isinstance(v, (int, float)) for v in r[6:]) for r in rows)
    assert all(isinstance(r[5], bool) for r in rows)

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
    assert scope == "subagents" and header[0] == "depth" and rows[0][1] == "build"

    app.tab = app.current_tabs().index("Turns")
    scope, header, rows = app._export_dataset()
    assert scope == "turns" and "prompt" in header and rows[0][-1] == "first prompt"

    app.tab = app.current_tabs().index("Tools")
    scope, header, rows = app._export_dataset()
    assert scope == "tools" and header[0] == "tool" and rows[0][0] == "bash"


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
    assert app.current_tabs() == ("Overview", "Models", "Subagents", "Turns", "Tools")
    for name, table in (
        ("Overview", app.renderer.detail_overview),
        ("Models", app.renderer.detail_models),
        ("Subagents", app.renderer.detail_subagents),
        ("Turns", app.renderer.detail_turns),
        ("Tools", app.renderer.detail_tools),
    ):
        app.tab = app.current_tabs().index(name)
        assert app.renderer.current_pager_lines(100) == table(wf, 96)  # content = width - 4


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


def test_default_opens_on_all_years_with_the_days_panel_focused():
    from datetime import datetime

    now = datetime.now()
    cm = now.strftime("%Y-%m")
    # Multiple years -> open on "All years" (focused_year None) with the Days panel
    # focused, while the Months selection is still anchored to the current month (so the
    # Days panel lists this month's days).
    app = app_with(
        [
            workflow("a", f"{cm}-01 12:00:00"),  # this month
            workflow("b", f"{now.year - 1}-03-01 12:00:00"),  # a prior year
        ]
    )
    assert app.focus == "days"
    assert app.focused_year is None  # "All years"
    assert app.months[app.month_index].month == cm  # current month anchors the Days panel


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


def test_copy_to_clipboard_backends_per_platform():
    real_which = ot.util.shutil.which
    real_run = ot.util.subprocess.run
    real_platform = sys.platform
    calls = []

    class _Proc:
        returncode = 0

    def fake_run(cmd, input=None, check=False, **kw):
        calls.append((cmd, input))
        return _Proc()

    try:
        ot.util.subprocess.run = fake_run

        # Windows: clip.exe is preferred (utf-8 bytes), label names clip/powershell.
        sys.platform = "win32"
        assert ot.util.clipboard_tools_label() == "clip/powershell"
        ot.util.shutil.which = lambda name: f"C:\\{name}.exe" if name == "clip" else None
        calls.clear()
        assert ot.util.copy_to_clipboard("ses_42") is True
        assert calls == [(["clip"], b"ses_42")]

        # clip missing -> PowerShell Set-Clipboard fallback.
        ot.util.shutil.which = lambda name: "pwsh" if name == "powershell" else None
        calls.clear()
        assert ot.util.copy_to_clipboard("hi") is True
        assert calls[0][0][0] == "powershell" and calls[0][1] == b"hi"

        # No Windows clipboard tool at all -> False, nothing run.
        ot.util.shutil.which = lambda name: None
        calls.clear()
        assert ot.util.copy_to_clipboard("x") is False
        assert calls == []

        # POSIX still uses pbcopy/xclip/... and reports them in the label.
        sys.platform = "darwin"
        assert ot.util.clipboard_tools_label() == "pbcopy/wl-copy/xclip/xsel"
        ot.util.shutil.which = lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None
        calls.clear()
        assert ot.util.copy_to_clipboard("ok") is True
        assert calls == [(["pbcopy"], b"ok")]
    finally:
        sys.platform = real_platform
        ot.util.shutil.which = real_which
        ot.util.subprocess.run = real_run


def test_notice_is_info_and_colours_are_explicit():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    clock = [100.0]
    app._toast_clock = lambda: clock[0]

    # `self.notice = "..."` stays the one-liner and is neutral info BY DEFINITION:
    # the kind is never inferred from the text, so error-sounding words (a session
    # title saying "failed", a reworded message) can never change the colour.
    app.notice = "price refresh failed: boom"
    assert app.notice == "price refresh failed: boom"  # readable back (tests/callers)
    assert app.toasts[-1].kind == "info"
    app._mark_toasts_shown()

    # A coloured toast names its kind at the call site.
    app.notify("export failed: disk full", "error")
    assert app.toasts[-1].kind == "error"
    app._mark_toasts_shown()

    app.notify("copied: ses_42", "success")
    assert app.toasts[-1].kind == "success"
    assert len(app.toasts) == 3  # three distinct frames -> three stacked toasts

    app._mark_toasts_shown()
    app.notify("heads up", kind="warn")
    assert app.toasts[-1].kind == "warn"


def test_toasts_coalesce_within_a_frame_cap_and_expire():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    clock = [0.0]
    app._toast_clock = lambda: clock[0]

    # Two notices set without a paint between them collapse onto one toast
    # ("fetching…" -> "refreshed"); the last one wins.
    app.notify("fetching prices…")
    app.notify("refreshed 10 model prices")
    assert len(app.toasts) == 1
    assert app.notice == "refreshed 10 model prices"

    # Distinct frames stack, but only TOAST_MAX survive (oldest drops).
    for i in range(app.TOAST_MAX + 2):
        app._mark_toasts_shown()
        app.notify(f"message {i}")
    assert len(app.toasts) == app.TOAST_MAX
    assert [t.text for t in app.toasts] == [f"message {i}" for i in range(2, app.TOAST_MAX + 2)]

    # Time, not a keystroke, dismisses them: past the TTL they're gone.
    clock[0] += app.TOAST_TTL + 0.01
    assert app.active_toasts() == []
    assert app.notice == ""

    # `self.notice = ""` clears immediately.
    app.notify("lingering")
    app.notice = ""
    assert app.toasts == []


def test_draw_toasts_paints_stacked_top_right_cards():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.notify("copied: ses_42", kind="success")
    app._mark_toasts_shown()
    app.notify("disk on fire", kind="error")
    app._mark_toasts_shown()
    screen = FakeScreen(24, 80)
    orig_cp = ot.curses.color_pair
    ot.curses.color_pair = lambda n: 0
    try:
        app.renderer.draw_toasts(screen, 24, 80)
    finally:
        ot.curses.color_pair = orig_cp
    text = screen_text(screen)
    assert "copied: ses_42" in text and "Done" in text  # success card: header + message
    assert "disk on fire" in text and "Error" in text  # error card: header + message
    assert "✓" in text and "✕" in text  # per-kind sigils
    # two-line cards in the top-right (newest on top), below the header hline (row 2)
    # and clear of the footer; a 1-row gap separates them.
    rows = {y for (y, _x) in screen.cells}
    assert rows == {3, 4, 6, 7}  # newest (error) at rows 3-4, older (success) at 6-7
    # right-aligned: every painted cell sits in the right half of an 80-wide screen
    assert min(x for (_y, x) in screen.cells) > 40


def test_draw_toasts_wraps_a_long_message_instead_of_truncating():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    # A full export path -- longer than one toast line; it must wrap, not get clipped.
    msg = "exported 9 rows → ~/SoftwareProjects/opentab/opentab-months-20260621-175102.csv"
    app.notify(msg, kind="success")
    app._mark_toasts_shown()
    screen = FakeScreen(24, 80)
    orig_cp = ot.curses.color_pair
    ot.curses.color_pair = lambda n: 0
    try:
        app.renderer.draw_toasts(screen, 24, 80)
    finally:
        ot.curses.color_pair = orig_cp
    text = screen_text(screen)
    rows = sorted({y for (y, _x) in screen.cells})
    assert len(rows) >= 3  # header + at least two wrapped message lines
    assert "exported" in text  # head of the message...
    assert ".csv" in text  # ...and its tail both survive (nothing truncated away)


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


def test_launch_menu_opens_in_tmux_and_copy_only_outside():
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
        # outside tmux (and no launcher hook), the menu still opens but narrows to
        # the copy target: spawn shortcuts are ignored, Enter picks the only row.
        os.environ.pop("TMUX")
        assert app.can_launch_current()  # footer keeps L: copy needs no tmux
        app.handle_key(None, ord("L"))
        assert app.launch_menu is not None
        assert [kind for _k, kind, _l in app.launch_targets()] == ["copy"]
        app.handle_key(None, ord("w"))  # not offered -> ignored, menu stays open
        assert app.launch_menu is not None and len(launches) == 1
        app.handle_key(None, 10)  # Enter runs the only target: copy
        assert app.launch_menu is None
        assert copies[-1] == "cd /repo/a && claude --resume ses_1"
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
    app.focus = "months"  # one scope holds all three (they sit on different days)
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


def test_slash_is_an_alias_for_the_filter_key():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", title="alpha"),
            workflow("b", "2026-06-02 12:00:00", title="beta"),
        ]
    )
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    assert app.handle_key(None, ord("/")) and app.filter_active
    for ch in "bet":
        app.handle_key(None, ord(ch))
    assert [w.title for w in app.current_sessions()] == ["beta"]
    app.handle_key(None, 10)  # Enter keeps the filter and leaves the mode
    assert not app.filter_active and app.query == "bet"
    # `/` also opens the P overlay's filter, like `f`
    app.handle_key(None, ord("x"))  # clear the committed filter first
    app.handle_key(None, ord("P"))
    assert app.show_prices and not app.filter_active
    assert app.handle_key(None, ord("/")) and app.filter_active and app.show_prices


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


def test_csv_keeps_cost_only_rows_as_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "copilot.csv")
        # A row logging only a cost (no token counts) is still real spend: records_cost
        # probes True from it, so dropping the row would show a $0 metered source.
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "credits", "session_id"],
            [
                ["2026-06-18T10:00:00Z", "gpt-4o", 1000, 100, "", "s1"],
                ["2026-06-18T11:00:00Z", "claude-opus-4-5", "", "", 75, "s2"],
                ["2026-06-18T12:00:00Z", "", "", "", "", "s3"],  # truly empty -> skipped
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        assert store.records_cost is True
        workflows = store.workflows()
        assert {w.id for w in workflows} == {"s1", "s2"}  # s3 stays dropped
        s2 = next(w for w in workflows if w.id == "s2")
        assert s2.total_cost == round(75 * 0.01, 6)  # credits x $0.01 = $0.75
        assert s2.total_tokens == 0 and s2.unpriced_tokens == 0


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
        assert isinstance(getattr(store, "_store", store), ot.CsvStore)  # unwrap CachedStore

        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "opencode"
        assert app.next_source_name() == "CSV"

        # A CSV/Copilot source has no CLI resume, so L produces no command (never crashes)
        wf = workflow("s1", "2026-06-01 12:00:00", title="t", directory="/tmp/proj")
        wf.source = "CSV"
        assert app.resume_command(wf) is None


def _jsonl_args():
    return type("Args", (), {"demo": False})()


def test_jsonl_store_splits_cache_prefixes_providers_and_supports_turns():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                # s1: two requests on one prompt, then a third on a new prompt
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "s1",
                    "request_id": "r1",
                    "model": "claude-sonnet-4",
                    "prompt": "refactor auth",
                    "input_tokens": 12000,
                    "cached_tokens": 4000,
                    "output_tokens": 800,
                },
                {
                    "timestamp": "2026-06-18T10:00:30Z",
                    "session_id": "s1",
                    "request_id": "r2",
                    "model": "claude-sonnet-4",
                    "prompt": "refactor auth",
                    "input_tokens": 9000,
                    "cached_tokens": 8000,
                    "output_tokens": 600,
                },
                {
                    "timestamp": "2026-06-18T10:05:00Z",
                    "session_id": "s1",
                    "request_id": "r3",
                    "model": "gpt-4o",
                    "prompt": "add tests",
                    "input_tokens": 5000,
                    "output_tokens": 300,
                },
                {
                    "timestamp": "2026-06-17T09:00:00Z",
                    "session_id": "s2",
                    "request_id": "r4",
                    "model": "gemini-2.5-pro",
                    "input_tokens": 2000,
                    "output_tokens": 150,
                },
            ],
        )
        store = ot.JsonlStore(path, _jsonl_args())
        assert store.records_cost is False  # no cost -> subscription-style
        workflows = store.workflows()
        assert {w.id for w in workflows} == {"s1", "s2"}
        s1 = next(w for w in workflows if w.id == "s1")
        assert s1.source == "JSONL"
        assert s1.subagents == 0  # no subagent tree
        assert s1.total_cost == 0.0
        # uncached+cached+output per request: 12800 + 9600 + 5300
        assert s1.total_tokens == s1.unpriced_tokens == 27700
        assert s1.title == "refactor auth"  # title seeds from the first prompt

        rows = {r["model_name"]: r for r in store.model_breakdown() if r["root_id"] == "s1"}
        assert set(rows) == {"anthropic/claude-sonnet-4", "openai/gpt-4o"}
        cl = rows["anthropic/claude-sonnet-4"]
        assert cl["input"] == 9000 and cl["cache_read"] == 12000  # cached split out of input

        # Turns: chronological, grouped by the owning prompt (consecutive same text)
        assert store.supports_turns("s1") is True
        turns = store.message_timeline("s1")
        assert [t["tokens_total"] for t in turns] == [12800, 9600, 5300]
        assert [t["prompt_title"] for t in turns] == ["refactor auth", "refactor auth", "add tests"]
        assert turns[0]["prompt_id"] == turns[1]["prompt_id"] != turns[2]["prompt_id"]
        assert all(t["depth"] == 0 and t["agent"] == "-" for t in turns)
        # time is the canonical local "YYYY-MM-DD HH:MM:SS" the renderer slices
        assert re.match(r"2026-06-18 \d\d:\d\d:\d\d$", turns[0]["time"])


def test_jsonl_cost_and_credits_price_as_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "m1",
                    "model": "claude-opus-4-5",
                    "input_tokens": 10000,
                    "output_tokens": 2000,
                    "credits": 150,
                },
                {
                    "timestamp": "2026-06-18T10:10:00Z",
                    "session_id": "m1",
                    "model": "claude-opus-4-5",
                    "input_tokens": 3000,
                    "output_tokens": 500,
                    "cost_usd": 0.40,
                },
            ],
        )
        store = ot.JsonlStore(path, _jsonl_args())
        assert store.records_cost is True
        w = store.workflows()[0]
        assert w.total_cost == round(150 * 0.01 + 0.40, 6)  # credits x $0.01 + USD = $1.90
        assert w.unpriced_tokens == 0  # metered rows aren't re-estimated under "$"
        row = store.model_breakdown()[0]
        assert row["unpriced_input"] == 0 and row["unpriced_output"] == 0


def test_jsonl_keeps_cost_only_lines_as_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        # A line logging only a cost (no token counts) is still real spend: records_cost
        # probes True from it, so dropping the line would show a $0 metered source.
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "s1",
                    "model": "gpt-4o",
                    "input_tokens": 100,
                    "output_tokens": 10,
                },
                {
                    "timestamp": "2026-06-18T11:00:00Z",
                    "session_id": "s2",
                    "model": "claude-opus-4-5",
                    "cost_usd": 0.5,
                },
                {"timestamp": "2026-06-18T12:00:00Z", "session_id": "s3"},  # empty -> skipped
            ],
        )
        store = ot.JsonlStore(path, _jsonl_args())
        assert store.records_cost is True
        workflows = store.workflows()
        assert {w.id for w in workflows} == {"s1", "s2"}  # s3 stays dropped
        s2 = next(w for w in workflows if w.id == "s2")
        assert s2.total_cost == 0.5
        assert s2.total_tokens == 0 and s2.unpriced_tokens == 0


def test_jsonl_dedupes_request_id_and_synthesizes_sessions():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        # r1 appears twice (regenerated file) -> counted once; a junk line is skipped;
        # rows with no session_id group into one synthetic session per (date, project).
        with open(path, "w") as fh:
            fh.write(
                json.dumps(
                    {
                        "timestamp": "2026-06-18T10:00:00Z",
                        "session_id": "s1",
                        "request_id": "r1",
                        "model": "gpt-4o",
                        "input_tokens": 100,
                        "output_tokens": 10,
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "timestamp": "2026-06-18T10:00:30Z",
                        "session_id": "s1",
                        "request_id": "r1",
                        "model": "gpt-4o",
                        "input_tokens": 100,
                        "output_tokens": 10,
                    }
                )
                + "\n"
            )
            fh.write("this is not json\n")
            fh.write(
                json.dumps(
                    {
                        "timestamp": "2026-06-18T11:00:00Z",
                        "project": "alpha",
                        "model": "gpt-4o",
                        "input_tokens": 50,
                        "output_tokens": 5,
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "timestamp": "2026-06-19T09:00:00Z",
                        "project": "alpha",
                        "model": "gpt-4o",
                        "input_tokens": 70,
                        "output_tokens": 7,
                    }
                )
                + "\n"
            )
        store = ot.JsonlStore(path, _jsonl_args())
        workflows = store.workflows()
        s1 = next(w for w in workflows if w.id == "s1")
        assert s1.total_tokens == 110  # r1 counted once, not 220
        synthetic = [w for w in workflows if w.id.startswith("jsonl:")]
        assert len(synthetic) == 2  # (06-18, alpha) and (06-19, alpha)


def test_jsonl_detail_turns_groups_and_reprices_under_dollar():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "s1",
                    "request_id": "r1",
                    "model": "claude-sonnet-4",
                    "prompt": "refactor auth",
                    "input_tokens": 8000,
                    "output_tokens": 800,
                },
                {
                    "timestamp": "2026-06-18T10:05:00Z",
                    "session_id": "s1",
                    "request_id": "r2",
                    "model": "claude-sonnet-4",
                    "prompt": "add tests",
                    "input_tokens": 5000,
                    "output_tokens": 300,
                },
            ],
        )
        app = ot.App(ot.JsonlStore(path, _jsonl_args()), args())
        rnd = ot.Renderer(app)
        wf = app.loaded[0]
        # A token-only source defaults the "$" estimate ON, so the two $0 turns are
        # repriced at list price -> non-zero total, grouped under their prompts.
        assert app.show_api_prices is True
        priced = rnd.detail_turns(wf, 96)
        joined = "\n".join(priced)
        assert priced[0].startswith("# Turns — 2 turns, $") and "$0.00 total" not in priced[0]
        assert "▸ refactor auth" in joined and "▸ add tests" in joined
        # Toggle the estimate off -> only recorded cost ($0) counts.
        app.show_api_prices = False
        assert rnd.detail_turns(wf, 96)[0] == "# Turns — 2 turns, $0.00 total"


def test_jsonl_path_routing_and_source_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        jl_path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            jl_path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "model": "gpt-4o",
                    "input_tokens": 100,
                    "output_tokens": 10,
                }
            ],
        )
        # All three forms select the jsonl source and fill --jsonl.
        for argv in ([jl_path], ["--jsonl", jl_path], ["--source", "jsonl", jl_path]):
            a = _parse(argv)
            assert a.source == "jsonl", argv
            assert a.jsonl == jl_path, argv
        # Bare `opentab` is unchanged: auto, jsonl auto-discovered at the default path.
        bare = _parse([])
        assert bare.source == "auto"
        assert bare.jsonl == ot.DEFAULT_JSONL_PATH

        oc_db = os.path.join(tmp, "opencode.db")
        open(oc_db, "w").close()
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
                "csv": os.path.join(tmp, "no.csv"),
                "jsonl": jl_path,
                "demo": False,
            },
        )()
        assert "jsonl" in ot.available_sources(args)
        store, _ = ot.sources.make_store(args, "jsonl")
        assert isinstance(getattr(store, "_store", store), ot.JsonlStore)  # unwrap CachedStore


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


def test_copilot_store_dedupes_span_and_log_split_across_files():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        # OTEL exporters write spans and logs to DIFFERENT files: the same call appears
        # as a chat span in spans.jsonl and an inference log in logs.jsonl. It must
        # count once (the chat span's 60/10), never twice.
        chat = _otel_chat(
            "conv-x", "gpt-5.4", 60, 10, trace="trace-x", span="chat-1", resp="resp-x"
        )
        inference = {
            "traceId": "trace-x",
            "hrTime": [1775934263, 0],
            "_body": "GenAI inference: gpt-5.4",
            "attributes": {
                "event.name": "gen_ai.client.inference.operation.details",
                "gen_ai.response.model": "gpt-5.4",
                "gen_ai.conversation.id": "conv-x",
                "gen_ai.response.id": "resp-x",
                "gen_ai.usage.input_tokens": 80,
                "gen_ai.usage.output_tokens": 20,
            },
        }
        _write_otel(otel, [chat], name="spans.jsonl")
        _write_otel(otel, [inference], name="logs.jsonl")
        store = ot.CopilotStore(otel, _copilot_args(otel))
        rows = [r for r in store.model_breakdown() if r["root_id"] == "conv-x"]
        assert len(rows) == 1
        assert rows[0]["runs"] == 1  # the log in the other file was suppressed
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


VSCODE_SID = "a66d5e72-2c39-48c0-8514-8eecb3cdbabc"


def _vscode_args(vscode_dir):
    return type("Args", (), {"demo": False, "vscode_dir": vscode_dir})()


def _vscode_request(
    rid="request_1",
    ts=1781122800000,
    completion=490,
    md_prompt=32543,
    md_output=60,
    resolved="claude-sonnet-4-6",
    model_id="copilot/claude-sonnet-4.6",
    message=None,
):
    # The serialized shape VS Code's chatModel.ts writes: response data (tokens, result)
    # is flattened onto the request; completionTokens is the turn total across tool-call
    # rounds while result.metadata carries the extension's single-round figures.
    return {
        "requestId": rid,
        "timestamp": ts,
        "modelId": model_id,
        "message": {"text": "fix the flaky test"} if message is None else message,
        "completionTokens": completion,
        "result": {
            "metadata": {
                "promptTokens": md_prompt,
                "outputTokens": md_output,
                "resolvedModel": resolved,
            }
        },
    }


def _vscode_user_dir(tmp, journal_entries, hash_name="h1", folder_name="myrepo", name=VSCODE_SID):
    # Build <User>/workspaceStorage/<hash>/chatSessions/<sid>.jsonl plus the
    # workspace.json that names the workspace folder (as a file:// URI).
    user = os.path.join(tmp, "Code", "User")
    hash_dir = os.path.join(user, "workspaceStorage", hash_name)
    chat = os.path.join(hash_dir, "chatSessions")
    os.makedirs(chat, exist_ok=True)
    folder = os.path.join(tmp, folder_name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(hash_dir, "workspace.json"), "w") as fh:
        json.dump({"folder": "file://" + folder}, fh)
    _write_jsonl(os.path.join(chat, f"{name}.jsonl"), journal_entries)
    return user, folder


def test_vscode_store_replays_journal_and_prefers_cumulative_output():
    with tempfile.TemporaryDirectory() as tmp:
        user, folder = _vscode_user_dir(
            tmp,
            [
                {
                    "kind": 0,
                    "v": {
                        "version": 3,
                        "sessionId": VSCODE_SID,
                        "creationDate": 1781122762688,
                        "requests": [],
                    },
                },
                {"kind": 2, "k": ["requests"], "v": [_vscode_request()]},
                # A canceled request records nothing and must not count as a run.
                {
                    "kind": 2,
                    "v": [
                        {
                            "requestId": "request_2",
                            "timestamp": 1781122900000,
                            "modelId": "copilot/gpt-4.1",
                            "message": {"text": "never mind"},
                        }
                    ],
                },
                {"kind": 1, "k": ["customTitle"], "v": "Fix the flaky test"},
            ],
        )
        store = ot.VscodeStore([user], _vscode_args(user))
        assert store.records_cost is False  # subscription-style: $0 until "$" reprices
        workflows = store.workflows()
        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == VSCODE_SID
        assert w.title == "Fix the flaky test"  # customTitle wins over the first prompt
        assert w.directory == folder  # workspace.json folder URI, git-root folded
        assert w.total_cost == 0.0 and w.root_cost == 0.0
        assert w.total_tokens == 32543 + 490
        assert w.unpriced_tokens == w.total_tokens  # every token is unpriced
        rows = store.model_breakdown()
        assert len(rows) == 1
        row = rows[0]
        assert row["model_name"] == "anthropic/claude-sonnet-4-6"  # resolvedModel, prefixed
        assert row["runs"] == 1  # the canceled request did not count
        assert row["input"] == 32543  # metadata.promptTokens (the fuller figure)
        # chatModel.ts accumulates completionTokens across tool-call rounds (490);
        # metadata.outputTokens is a single round (60) and must not win.
        assert row["output"] == 490
        assert row["unpriced_input"] == 32543 and row["root_unpriced_output"] == 490
        nodes = store.workflow_nodes(VSCODE_SID)
        assert len(nodes) == 1 and nodes[0]["depth"] == 0
        assert nodes[0]["tokens_total"] == 32543 + 490


def test_vscode_store_reads_legacy_json_and_dedupes_against_journal():
    with tempfile.TemporaryDirectory() as tmp:
        user, _ = _vscode_user_dir(
            tmp,
            [
                {
                    "kind": 0,
                    "v": {
                        "version": 3,
                        "sessionId": VSCODE_SID,
                        "creationDate": 1781122762688,
                        "requests": [],
                    },
                },
                {"kind": 2, "k": ["requests"], "v": [_vscode_request()]},
            ],
        )
        chat = os.path.join(user, "workspaceStorage", "h1", "chatSessions")
        # The same session also in the pre-journal plain-JSON shape (a migrated
        # session): identical requestId -> counted once, journal first.
        with open(os.path.join(chat, f"{VSCODE_SID}.json"), "w") as fh:
            json.dump(
                {
                    "version": 3,
                    "sessionId": VSCODE_SID,
                    "creationDate": 1781122762688,
                    "requests": [_vscode_request()],
                },
                fh,
            )
        # Plus a legacy-only session: message as a plain string (the old format),
        # top-level promptTokens, bare modelId -> provider-prefixed by family.
        with open(os.path.join(chat, "22222222-2222-2222-2222-222222222222.json"), "w") as fh:
            json.dump(
                {
                    "version": 2,
                    "sessionId": "22222222-2222-2222-2222-222222222222",
                    "creationDate": 1781100000000,
                    "requests": [
                        {
                            "requestId": "request_9",
                            "timestamp": 1781100060000,
                            "modelId": "gpt-4.1",
                            "message": "explain this regex",
                            "completionTokens": 200,
                            "promptTokens": 1500,
                        }
                    ],
                },
                fh,
            )
        store = ot.VscodeStore([user], _vscode_args(user))
        workflows = {w.id: w for w in store.workflows()}
        assert len(workflows) == 2
        merged = workflows[VSCODE_SID]
        assert merged.total_tokens == 32543 + 490  # once, not twice
        legacy = workflows["22222222-2222-2222-2222-222222222222"]
        assert legacy.title == "explain this regex"  # first prompt (no customTitle)
        assert legacy.total_tokens == 1500 + 200
        legacy_rows = [r for r in store.model_breakdown() if r["root_id"] == legacy.id]
        assert legacy_rows[0]["model_name"] == "openai/gpt-4.1"


def test_vscode_store_turns_empty_window_and_source_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        user = os.path.join(tmp, "Code", "User")
        empty = os.path.join(user, "globalStorage", "emptyWindowChatSessions")
        os.makedirs(empty, exist_ok=True)
        sid = "33333333-3333-3333-3333-333333333333"
        _write_jsonl(
            os.path.join(empty, f"{sid}.jsonl"),
            [
                {
                    "kind": 0,
                    "v": {
                        "version": 3,
                        "sessionId": sid,
                        "creationDate": 1781122762688,
                        "requests": [],
                    },
                },
                {
                    "kind": 2,
                    "k": ["requests"],
                    "v": [
                        _vscode_request(
                            rid="request_a", ts=1781122800000, message={"text": "first prompt"}
                        ),
                        _vscode_request(
                            rid="request_b",
                            ts=1781126400000,
                            completion=80,
                            md_prompt=900,
                            md_output=0,
                            resolved="gpt-4.1",
                            model_id="copilot/gpt-4.1",
                            message={"text": "second prompt"},
                        ),
                    ],
                },
            ],
        )
        store = ot.VscodeStore([user], _vscode_args(user))
        w = store.workflows()[0]
        assert w.directory == "(no workspace)"  # empty-window sessions have no folder
        assert store.supports_turns(sid)
        turns = store.message_timeline(sid)
        assert [t["prompt_title"] for t in turns] == ["first prompt", "second prompt"]
        assert turns[0]["time"] < turns[1]["time"]  # chronological, never cost-sorted
        assert turns[0]["output"] == 490 and turns[1]["output"] == 80
        assert turns[0]["prompt_id"] == "request_a"  # one request == one prompt group
        assert all(t["cost"] == 0.0 for t in turns)  # nothing recorded; "$" reprices

        # Source plumbing: tokens present -> available; make_store builds the backend.
        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": os.path.join(tmp, "no.db"),
                "claude_dir": os.path.join(tmp, "no-claude"),
                "codex_dir": os.path.join(tmp, "no-codex"),
                "hermes_db": os.path.join(tmp, "no-hermes.db"),
                "csv": os.path.join(tmp, "no.csv"),
                "jsonl": os.path.join(tmp, "no.jsonl"),
                "vscode_dir": user,
                "demo": False,
            },
        )()
        assert ot.available_sources(args) == ["vscode"]
        built, _ = ot.sources.make_store(args, "vscode")
        assert isinstance(getattr(built, "_store", built), ot.VscodeStore)  # unwrap CachedStore

        # An opened-but-never-used chat panel (no tokens anywhere) must NOT surface
        # the source -- that is every VS Code install on earth.
        bare = os.path.join(tmp, "Bare", "User")
        chat = os.path.join(bare, "workspaceStorage", "h9", "chatSessions")
        os.makedirs(chat)
        _write_jsonl(
            os.path.join(chat, "9b593653-875b-4309-8cba-e8719e139426.jsonl"),
            [{"kind": 0, "v": {"version": 3, "sessionId": "9b593653", "requests": []}}],
        )
        args.vscode_dir = bare
        assert ot.available_sources(args) == []


def test_cached_store_warm_start_and_invalidation():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
        data = os.path.join(tmp, "data.jsonl")
        with open(data, "w") as fh:
            fh.write("one\n")

        class Backend:
            combined = False
            records_cost = False
            demo = False
            source_name = "Fake"

            def __init__(self):
                self.workflow_calls = 0
                self.breakdown_calls = 0

            def cache_inputs(self):
                return [data]

            def workflows(self):
                self.workflow_calls += 1
                return [workflow("s1", "2026-06-01 12:00:00", cost=0.0, tokens=100)]

            def model_breakdown(self):
                self.breakdown_calls += 1
                return [
                    {"root_id": "s1", "model_name": "anthropic/x", "runs": 1, "tokens_total": 100}
                ]

        args = type("Args", (), {"demo": False, "no_cache": False})()
        cid = "fake|" + data
        try:
            # Cold: the first wrapper parses (once each) and writes the cache.
            b1 = Backend()
            c1 = ot.CachedStore(b1, cid, args)
            wf1 = c1.workflows()
            mb1 = c1.model_breakdown()
            assert b1.workflow_calls == 1 and b1.breakdown_calls == 1
            assert [w.id for w in wf1] == ["s1"] and mb1[0]["root_id"] == "s1"

            # Warm: a fresh wrapper over the UNCHANGED file serves the cached rollup and
            # never touches the backend -- the whole point of the warm start.
            b2 = Backend()
            c2 = ot.CachedStore(b2, cid, args)
            wf2 = c2.workflows()
            mb2 = c2.model_breakdown()
            assert b2.workflow_calls == 0 and b2.breakdown_calls == 0
            assert [w.id for w in wf2] == ["s1"] and mb2 == mb1  # identical, round-tripped

            # Invalidate: editing the file changes size+mtime -> miss -> real re-parse.
            with open(data, "a") as fh:
                fh.write("two\n")
            b3 = Backend()
            c3 = ot.CachedStore(b3, cid, args)
            c3.workflows()
            c3.model_breakdown()
            assert b3.workflow_calls == 1 and b3.breakdown_calls == 1

            # --no-cache passes the raw backend straight through (no wrapper).
            raw = ot.sources._wrap_cache(Backend(), "fake", type("A", (), {"no_cache": True})())
            assert isinstance(raw, Backend)
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_cached_store_serves_records_cost_and_survives_field_drift():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
        data = os.path.join(tmp, "data.jsonl")
        with open(data, "w") as fh:
            fh.write("one\n")

        class Backend:
            combined = False
            demo = False
            source_name = "Fake"

            def __init__(self):
                self.workflow_calls = 0
                self.probe_calls = 0

            @property
            def records_cost(self):
                self.probe_calls += 1  # stands in for the full-corpus cost probe
                return True

            def cache_inputs(self):
                return [data]

            def workflows(self):
                self.workflow_calls += 1
                return [workflow("s1", "2026-06-01 12:00:00", cost=2.0, tokens=100)]

            def model_breakdown(self):
                return [
                    {"root_id": "s1", "model_name": "anthropic/x", "runs": 1, "tokens_total": 100}
                ]

        args = type("Args", (), {"demo": False, "no_cache": False})()
        cid = "fake|" + data
        try:
            # Cold: a real parse; the write reads records_cost off the backend (once).
            b1 = Backend()
            c1 = ot.CachedStore(b1, cid, args)
            c1.workflows()
            c1.model_breakdown()
            assert b1.probe_calls == 1

            # Warm: records_cost round-trips from the cache -- the backend's probe is
            # never touched, whether it's read after workflows() or straight away.
            b2 = Backend()
            c2 = ot.CachedStore(b2, cid, args)
            c2.workflows()
            assert c2.records_cost is True
            assert b2.probe_calls == 0 and b2.workflow_calls == 0
            b3 = Backend()
            assert ot.CachedStore(b3, cid, args).records_cost is True  # fingerprints itself
            assert b3.probe_calls == 0

            # A cached row that no longer matches the Workflow dataclass (field drift
            # without a version bump) falls back to a real parse instead of crashing.
            with open(c1._path) as fh:
                payload = json.load(fh)
            payload["workflows"][0]["bogus_field"] = 1
            with open(c1._path, "w") as fh:
                json.dump(payload, fh)
            b4 = Backend()
            c4 = ot.CachedStore(b4, cid, args)
            wf4 = c4.workflows()
            assert b4.workflow_calls == 1 and [w.id for w in wf4] == ["s1"]
            assert c4.served_from_cache is False
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_records_cost_probe_runs_lazily_not_at_construction():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "s1",
                    "model": "gpt-4o",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cost_usd": 0.05,
                }
            ],
        )
        calls = []
        orig = ot.JsonlStore._probe_records_cost
        ot.JsonlStore._probe_records_cost = lambda self: (calls.append(1), orig(self))[1]
        try:
            store = ot.JsonlStore(path, _jsonl_args())
            assert calls == []  # constructing must not read the file
            assert store.records_cost is True  # first read probes...
            assert store.records_cost is True and calls == [1]  # ...and the answer sticks

            # Parsed first (the cold-start order): the answer derives from the parse's
            # accumulated per-model costs and the probe never runs at all.
            calls.clear()
            store2 = ot.JsonlStore(path, _jsonl_args())
            store2.workflows()
            assert store2.records_cost is True and calls == []
        finally:
            ot.JsonlStore._probe_records_cost = orig

        # pi's parse-derived answer honors the metered/subscription split like the probe:
        # a codex-plan cost is a list-price estimate, not spend -> records_cost False.
        root = os.path.join(tmp, "pi-sessions")
        _pi_write(
            root,
            "--proj--",
            PI_SID,
            [
                _pi_session(PI_SID, tmp),
                _pi_user("hi"),
                _pi_assistant("openai/gpt-5", 10, 5, cost=0.01, provider="openai-codex"),
            ],
        )
        sub = ot.PiStore(root, _pi_args())
        sub.workflows()  # parse first: no probe needed
        assert sub.records_cost is False


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
    assert [r[0] for r in rows] == [1]  # depth-0 root filtered out, subagent kept
    # Reload drops the memo -- the underlying data may have changed.
    app.reload()
    app.session_node_rows("s1")
    assert app.store.node_calls == 2


def test_cache_invalidates_on_wal_write_so_reload_sees_new_opencode_sessions():
    # OpenCode runs SQLite in WAL mode, so a new session lands in <db>-wal while the
    # main .db's size/mtime don't move until a checkpoint. cache_inputs() must
    # fingerprint the WAL sidecars, or CachedStore keeps serving the stale rollup and a
    # reload (r) / the browser's refresh never shows sessions written since -- the
    # reported "--web refresh doesn't get new sessions" bug.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
        db = os.path.join(tmp, "opencode.db")
        # Writer stays open the whole test with autocheckpoint off, so every commit
        # stays in the -wal file and the main .db is never checkpointed/rewritten.
        w = sqlite3.connect(db)
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.executescript(
            """
            create table session (
              id text primary key, parent_id text, title text, directory text,
              time_created integer, cost real default 0 not null,
              tokens_input integer default 0 not null, tokens_output integer default 0 not null,
              tokens_reasoning integer default 0 not null, tokens_cache_read integer default 0 not null,
              tokens_cache_write integer default 0 not null
            );
            create table message (id text primary key, session_id text, data text);
            """
        )
        w.execute(
            "insert into session values ('s1',null,'One','/work/repo',1760000000000,1.0,0,0,0,0,0)"
        )
        w.commit()
        try:
            store = ot.Store(db, type("A", (), {"demo": False})())
            ci = store.cache_inputs()
            assert db in ci and db + "-wal" in ci and db + "-shm" in ci  # sidecars fingerprinted

            cid = "opencode|" + db
            cargs = type("A", (), {"demo": False, "no_cache": False})()

            # Cold: parse s1 and write the cache (workflows + breakdown both fresh).
            c1 = ot.CachedStore(store, cid, cargs)
            assert [x.id for x in c1.workflows()] == ["s1"]
            c1.model_breakdown()
            assert c1.served_from_cache is False

            # Warm: a fresh wrapper over the unchanged DB serves the cache untouched.
            c2 = ot.CachedStore(store, cid, cargs)
            c2.workflows()
            assert c2.served_from_cache is True

            # OpenCode adds a new session -> it lands in the WAL, main .db mtime unchanged.
            mtime_before = os.stat(db).st_mtime_ns
            w.execute(
                "insert into session values ('s2',null,'Two','/work/repo',1760000100000,2.0,0,0,0,0,0)"
            )
            w.commit()
            assert os.stat(db).st_mtime_ns == mtime_before  # the WAL grew, not the .db

            # A reload now MISSES the cache (the -wal fingerprint moved) and re-parses,
            # so the new session is visible -- the fix.
            c3 = ot.CachedStore(store, cid, cargs)
            wf3 = c3.workflows()
            assert c3.served_from_cache is False
            assert sorted(x.id for x in wf3) == ["s1", "s2"]
        finally:
            w.close()
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_wsl_mount_root_and_windows_path_mapping():
    with tempfile.TemporaryDirectory() as tmp:
        # wsl.conf parsing: [automount] root= wins, comments stripped, missing -> /mnt.
        conf = os.path.join(tmp, "wsl.conf")
        with open(conf, "w") as fh:
            fh.write("[boot]\nsystemd=true\n[automount]\n# comment\nroot = /win ; inline\n")
        assert ot.util.wsl_mount_root(conf) == "/win"
        assert ot.util.wsl_mount_root(os.path.join(tmp, "absent.conf")) == "/mnt"

        # Drive-path folding: C:\... and C:/... land on <mount>/c/... when it exists.
        proj = os.path.join(tmp, "c", "Users", "mo", "proj")
        os.makedirs(proj)
        assert ot.util.windows_to_wsl_path(r"C:\Users\mo\proj", mount_root=tmp) == proj
        assert ot.util.windows_to_wsl_path("c:/Users/mo/proj", mount_root=tmp) == proj
        assert ot.util.windows_to_wsl_path("C:/Users/mo/gone", mount_root=tmp) == ""  # not mounted
        assert (
            ot.util.windows_to_wsl_path("/home/mo/proj", mount_root=tmp) == ""
        )  # not a drive path


def test_vscode_resolves_remote_and_windows_workspace_uris():
    # vscode-remote:// URIs (Remote-WSL / SSH / container workspaces) yield their path
    # segment; Windows file URIs keep the drive-path label when no WSL mount matches.
    to_path = ot.VscodeStore._uri_to_path
    assert to_path("vscode-remote://wsl%2BUbuntu/home/mo/proj") == "/home/mo/proj"
    assert to_path("vscode-remote://ssh-remote%2Bbox/srv/app") == "/srv/app"
    assert to_path("vscode-remote://wsl%2BUbuntu") == ""  # authority only, no path
    assert to_path("file:///c%3A/Users/nosuch-opentab/proj") == "c:/Users/nosuch-opentab/proj"
    assert to_path("untitled:Untitled-1") == ""

    # End to end: a Windows-side session store whose workspace.json points into this
    # distro via Remote-WSL resolves to the local (reachable) directory.
    with tempfile.TemporaryDirectory() as tmp:
        user = os.path.join(tmp, "Code", "User")
        hash_dir = os.path.join(user, "workspaceStorage", "hwsl")
        chat = os.path.join(hash_dir, "chatSessions")
        os.makedirs(chat)
        folder = os.path.join(tmp, "wslrepo")
        os.makedirs(folder)
        with open(os.path.join(hash_dir, "workspace.json"), "w") as fh:
            json.dump({"folder": "vscode-remote://wsl%2BUbuntu" + folder}, fh)
        _write_jsonl(
            os.path.join(chat, f"{VSCODE_SID}.jsonl"),
            [
                {"kind": 0, "v": {"version": 3, "sessionId": VSCODE_SID, "requests": []}},
                {"kind": 2, "k": ["requests"], "v": [_vscode_request()]},
            ],
        )
        store = ot.VscodeStore([user], _vscode_args(user))
        assert store.workflows()[0].directory == folder


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


def _write_status_db(db, sessions, messages=()):
    # Minimal OpenCode-shaped DB for the --status one-shot: session rows carry
    # (id, parent_id, directory, time_created, time_updated, cost, tokens_input),
    # messages only feed workflow_nodes' per-session model attribution.
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table session (
          id text primary key, parent_id text, title text, directory text,
          time_created integer, time_updated integer, cost real default 0 not null,
          tokens_input integer default 0 not null, tokens_output integer default 0 not null,
          tokens_reasoning integer default 0 not null, tokens_cache_read integer default 0 not null,
          tokens_cache_write integer default 0 not null
        );
        create table message (id text primary key, session_id text, data text);
        """
    )
    conn.executemany(
        "insert into session values (?,?,?,?,?,?,?,?,0,0,0,0)",
        [(id, parent, id, d, tc, tu, cost, tok) for id, parent, d, tc, tu, cost, tok in sessions],
    )
    conn.executemany("insert into message values (?,?,?)", messages)
    conn.commit()
    conn.close()


def test_status_line_follows_subagent_activity_and_sums_subtree():
    # "Current session" = the root whose *subtree* saw the latest update: a session
    # whose subagent is still streaming must beat a root created later but idle
    # since. The printed figure is the whole subtree's recorded cost.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(
            db,
            [
                # old root, but its subagent has the newest time_updated in the DB
                ("r1", None, "/work/repo", 1760000000000, None, 1.0, 10),
                ("r1c", "r1", "/work/repo", 1760000001000, 1760099999000, 0.5, 5),
                # created after r1, idle since
                ("r2", None, "/work/repo", 1760005000000, 1760005000000, 9.0, 10),
            ],
        )
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert [r["id"] for r in store.recent_roots()] == ["r1", "r2"]
        assert ot.status_line(store) == "$1.50"


def test_status_line_scopes_to_project_and_estimates_unpriced():
    # DIR narrows to that project's sessions; a $0 subscription session shows the
    # list-price estimate with the "~" marker instead of a useless $0.00; a project
    # with no sessions yields an empty segment (never an error).
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(
            db,
            [
                ("a", None, "/work/alpha", 1760000000000, 1760000900000, 2.0, 100),
                ("b", None, "/work/beta", 1760000000000, 1760000500000, 0.0, 1_000_000),
            ],
            messages=[
                (
                    "m1",
                    "b",
                    '{"role":"assistant","providerID":"anthropic","modelID":"claude-opus-4.5",'
                    '"cost":0,"tokens":{"input":1000000,"output":0}}',
                ),
            ],
        )
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert ot.status_line(store) == "$2.00"  # newest activity overall wins
        expected = ot.money(
            ot.api_equivalent_cost("anthropic/claude-opus-4.5", 1_000_000, 0, 0, 0, 0)
        )
        assert ot.status_line(store, "/work/beta") == "~" + expected
        assert ot.status_line(store, "/work/nowhere") == ""


def test_status_line_prices_an_exact_session_id():
    # Two sessions in ONE project can't be told apart by directory (a dir target
    # picks the project's most recent one) -- a session id target prices exactly
    # that session, and a subagent's id resolves up to its root so the whole
    # workflow is priced. Unknown ids yield an empty segment.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(
            db,
            [
                ("ses_old", None, "/work/repo", 1760000000000, 1760000100000, 5.0, 10),
                ("ses_oldchild", "ses_old", "/work/repo", 1760000001000, 1760000090000, 0.5, 5),
                ("ses_new", None, "/work/repo", 1760000200000, 1760000900000, 2.0, 10),
            ],
        )
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert ot.status_line(store, "/work/repo") == "$2.00"  # dir -> project's latest
        assert ot.status_line(store, "ses_new") == "$2.00"
        assert ot.status_line(store, "ses_old") == "$5.50"  # exact session, subtree included
        assert ot.status_line(store, "ses_oldchild") == "$5.50"  # subagent id -> its root
        assert ot.status_line(store, "ses_gone") == ""


# --- The web browser (--html / --serve) -------------------------------------


class NodesFakeStore(FakeStore):
    # FakeStore + a two-node subagent tree, to exercise the payload's nodes embed.
    def workflow_nodes(self, workflow_id):
        return [
            {
                "id": workflow_id,
                "depth": 0,
                "agent": "-",
                "title": "root",
                "created_at": "2026-05-01 10:00:00",
                "cost": 2.0,
                "tokens_input": 1000,
                "tokens_output": 500,
                "tokens_reasoning": 0,
                "tokens_cache_read": 0,
                "tokens_cache_write": 0,
                "tokens_total": 1500,
                "model_name": "anthropic/claude-fable-5",
            },
            {
                "id": "ses_sub",
                "depth": 1,
                "agent": "explore",
                "title": "scout the codebase",
                "created_at": "2026-05-01 10:01:00",
                "cost": 0.0,  # a subscription node: $0 recorded, tokens present
                "tokens_input": 1_000_000,
                "tokens_output": 100_000,
                "tokens_reasoning": 0,
                "tokens_cache_read": 0,
                "tokens_cache_write": 0,
                "tokens_total": 1_100_000,
                "model_name": "anthropic/claude-fable-5",
            },
        ]


class TurnsFakeStore(FakeStore):
    # FakeStore + a message timeline, to exercise the --serve session extras.
    def supports_turns(self, workflow_id):
        return True

    def message_timeline(self, workflow_id):
        base = {
            "depth": 0,
            "agent": "-",
            "model_name": "anthropic/claude-fable-5",
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "prompt_id": "p1",
            "prompt_title": "do the thing",
            "prompt_full": "do the thing\nand do it properly, with tests",
        }
        return [
            dict(
                base,
                time="2026-05-01 10:00:05",
                cost=0.0,
                input=1000,
                output=200,
                tokens_total=1200,
            ),
            dict(
                base, time="2026-05-01 10:00:31", cost=0.5, input=400, output=100, tokens_total=500
            ),
        ]


def test_web_payload_carries_both_cost_snapshots():
    app = app_with(
        [
            workflow("w1", "2026-05-01 10:00:00", cost=3.0, tokens=1000, directory="/tmp/alpha"),
            workflow("w2", "2026-05-02 11:00:00", cost=1.0, tokens=500, directory="/tmp/beta"),
        ]
    )
    payload = ot.build_payload(app)
    meta = payload["meta"]
    assert meta["version"] == ot.__version__
    assert meta["recordsCost"] is True
    assert meta["range"] == "all time"
    assert meta["startApi"] is False
    assert meta["serve"] is False
    by_id = {w["id"]: w for w in payload["workflows"]}
    assert set(by_id) == {"w1", "w2"}
    w1 = by_id["w1"]
    # Fully priced usage: the real and the API-equivalent snapshot agree, and both
    # travel in the payload so the page's $ toggle is a client-side field swap.
    assert w1["real"] == 3.0 and w1["api"] == 3.0
    assert w1["project"] == "/tmp/alpha"
    assert w1["date"].startswith("2026-05-01")
    assert payload["nodes"] == {}  # no subagents -> no per-session tree queries


def test_web_payload_embeds_nodes_and_reprices_unpriced_ones():
    w = workflow("w1", "2026-05-01 10:00:00", cost=2.0, directory="/tmp/alpha")
    w.subagents = 1
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(NodesFakeStore([w]), args)
    payload = ot.build_payload(app)
    nodes = payload["nodes"]["w1"]
    assert [n["depth"] for n in nodes] == [0, 1]
    root, sub = nodes
    assert root["real"] == 2.0 and root["api"] == 2.0  # priced node: api == real
    assert sub["real"] == 0.0 and sub["api"] > 0  # $0 node repriced at list rates
    assert sub["agent"] == "explore" and sub["tokens"] == 1_100_000


def test_web_session_extras_reports_turns_with_both_costs():
    w = workflow("w1", "2026-05-01 10:00:00", cost=0.5)
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(TurnsFakeStore([w]), args)
    extras = ot.session_extras(app, "w1")
    assert extras["tools"] == []  # no supports_tools -> hidden, never shown empty
    first, second = extras["turns"]
    assert first["real"] == 0.0 and first["api"] > 0  # $0 turn gets a list-price figure
    assert second["real"] == 0.5 and second["api"] == 0.5  # priced turn stays as recorded
    assert first["promptTitle"] == "do the thing"
    # The whole prompt travels too, so the page's ▸ header can unfold/hover it.
    assert first["promptFull"] == "do the thing\nand do it properly, with tests"


def test_web_render_html_defuses_embedded_script_tags():
    w = workflow("w1", "2026-05-01 10:00:00")
    w.title = "evil</script><script>alert(1)</script>"
    page = ot.render_html(ot.build_payload(app_with([w])))
    # Exactly the shell's two script blocks survive; the title's closing tags are
    # escaped inside the JSON blob so they can't break out of the data block.
    assert page.count("</script>") == 2
    assert "<\\/script>" in page
    assert 'id="opentab-data"' in page


def test_web_html_command_writes_the_report_file():
    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "report.html")
        args = type("Args", (), {"html": path})()
        assert ot.html_command(app, args) == 0
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    assert 'id="opentab-data"' in text
    assert "OpenTab — AI spend browser" in text
    # The page mirrors the TUI: sidebar host + keymap-driven detail pane + the
    # Trends overlay. Lock in the load-bearing hooks so a refactor can't silently
    # drop the TUI feel.
    assert 'id="side"' in text and 'id="tabbar"' in text
    assert 'id="trends"' in text  # the T Trends overlay host
    assert "TREND_TABS" in text and "Providers" in text  # the 7-tab Trends
    # The ranked tabs drill: a row opens its in-overlay sessions list, whose rows
    # deep-link into the session (mirrors the TUI's Trends drill).
    assert "trendDrillRows" in text and "Sessions · " in text
    # Every scope Overview carries the TUI's Top sessions section, and the day
    # Overview the full model mix (day has no Models tab).
    assert "topSessionsTable" in text and "'Top sessions'" in text and "'Model mix'" in text
    # Turns ▸ headers unfold/hover the whole prompt (serve-only data, baked JS).
    assert "promptFull" in text and "prompt-full" in text
    assert 'id="prices"' in text and 'id="rangepick"' in text  # P prices + R range overlays
    assert 'id="themepick"' in text and "const THEMES" in text  # the theme picker + palettes
    assert "catppuccin-mocha" in text and "tokyo-night" in text  # bundled themes
    assert "keydown" in text  # the j/k/Tab/h/l/Esc/$/T/P/R handler


def test_web_daily_trend_charts_only_active_days():
    page = ot.render_html(ot.build_payload(app_with([workflow("w1", "2026-07-01 10:00:00")])))
    # The Daily tab charts only up to the last day with spend, not the full calendar
    # month, so an in-progress month keeps bars as wide as Weekly/Monthly (each with its
    # own on-top label) instead of squeezing 31 slots and colliding the labels.
    assert "> 0) last = d" in page
    assert "for (let d = 1; d <= last; d++)" in page


def test_web_meta_carries_the_baked_theme():
    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    app.args.theme = "gruvbox"  # --theme sets the browser's initial theme
    meta = ot.build_payload(app)["meta"]
    assert meta["theme"] == "gruvbox"
    # Absent (older args) falls back to the default, never crashes.
    del app.args.theme
    assert ot.build_payload(app)["meta"]["theme"] == "opentab"


# --- Shared themes (one source for the web browser + the TUI) ----------------


def test_themes_are_complete_and_consistent():
    for tid, t in ot.THEMES.items():
        assert set(t["roles"]) == set(ot.themes.ROLE_KEYS), f"{tid} missing role slots"
        assert len(t["heat"]) >= 2 and len(t["price_heat"]) >= 2
        assert isinstance(t["dark"], bool) and t["name"]
        for hexval in list(t["roles"].values()) + t["heat"] + t["price_heat"]:
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", hexval), f"{tid}: bad hex {hexval}"
    assert ot.DEFAULT_THEME in ot.THEMES
    assert ot.themes.resolve_theme("nonsense") is ot.THEMES[ot.DEFAULT_THEME]


def test_web_payload_reshapes_roles_to_css_vars():
    wp = ot.web_payload()
    assert set(wp) == set(ot.THEMES)  # one entry per theme
    entry = wp["catppuccin-mocha"]
    assert set(entry) == {"name", "dark", "css", "heat", "priceHeat"}
    # underscores become CSS-var hyphens, values preserved
    assert entry["css"]["bg-glow"] == ot.THEMES["catppuccin-mocha"]["roles"]["bg_glow"]
    assert "accent-bright" in entry["css"] and "accent_bright" not in entry["css"]


def test_theme_color_math():
    # nearest_256 lands in the palette range; pure black/white hit the ends.
    assert 16 <= ot.nearest_256("#e0a458") <= 255
    assert ot.nearest_256("#000000") in (16, 232)  # cube origin or darkest grey
    # ramp resamples to exactly n and interpolates the midpoint.
    r = ot.ramp(["#000000", "#ffffff"], 5)
    assert len(r) == 5 and r[0] == "#000000" and r[-1] == "#ffffff"
    assert r[2] in ("#808080", "#7f7f7f")  # halfway grey
    assert ot.ramp(["#123456"], 4) == ["#123456"] * 4  # single stop repeats


def test_cli_theme_choices_match_the_theme_registry():
    # The --theme choices are sourced from themes.THEME_IDS, so they can't drift.
    args = ot.parse_args.__wrapped__ if hasattr(ot.parse_args, "__wrapped__") else None
    del args  # parse_args builds its own parser; assert the registry instead
    assert ot.THEME_IDS == tuple(ot.THEMES)
    assert "opentab" in ot.THEME_IDS and "tokyo-night" in ot.THEME_IDS


def test_web_payload_embeds_the_price_reference():
    # The P overlay's data: priced models you've used, with the eff $/M blend. The
    # FakeStore has no model_breakdown, so a store with model rows is needed -- reuse
    # NodesFakeStore, which returns a fable-5 node but no model_breakdown either;
    # so assert the structural shape (present, both row sets, mix optional).
    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    prices = ot.build_payload(app)["prices"]
    assert set(prices) >= {"byModel", "byRoute"}
    assert isinstance(prices["byModel"], list) and isinstance(prices["byRoute"], list)


def test_web_report_server_serves_page_extras_and_404():
    import threading
    import urllib.error
    import urllib.request

    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    server = ot.web.ReportServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert 'id="opentab-data"' in page
        assert '"serve":true' in page  # the served page knows the extras exist
        extras = json.loads(urllib.request.urlopen(base + "/api/session/w1").read().decode("utf-8"))
        assert extras == {"turns": [], "tools": []}  # FakeStore: no turns/tools support
        try:
            urllib.request.urlopen(base + "/nope")
            raise AssertionError("expected a 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()
    thread.join(timeout=5)


def test_web_server_is_hardened_against_csrf_and_dns_rebinding():
    import threading
    import urllib.error
    import urllib.request

    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    server = ot.web.ReportServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        # Every response carries the lockdown headers (self-contained page: inline
        # JS/CSS, data: favicon, fetch back to this server only).
        resp = urllib.request.urlopen(base + "/")
        csp = resp.headers["Content-Security-Policy"]
        assert csp.startswith("default-src 'none'")
        assert "connect-src 'self'" in csp and "img-src data:" in csp
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        # Reload mutates state, so it is POST-only: a GET (fireable cross-origin
        # by any webpage) gets a 405 and does not touch the stores.
        try:
            urllib.request.urlopen(base + "/api/reload")
            raise AssertionError("expected a 405")
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
        req = urllib.request.Request(base + "/api/reload", data=b"", method="POST")
        assert json.loads(urllib.request.urlopen(req).read().decode("utf-8")) == {"ok": True}
        # DNS rebinding: a foreign Host header is rejected on a loopback bind...
        req = urllib.request.Request(base + "/", headers={"Host": "evil.example.com"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("expected a 403")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        # ...while every local spelling passes, with or without the port.
        for host in ("localhost", f"localhost:{port}", "127.0.0.1", f"[::1]:{port}"):
            req = urllib.request.Request(base + "/", headers={"Host": host})
            assert urllib.request.urlopen(req).status == 200
    finally:
        server.shutdown()
        server.server_close()
    thread.join(timeout=5)


def test_serve_command_runs_serve_forever_off_the_main_thread():
    # Regression: on Windows a Ctrl-C never wakes serve_forever's select(), so serving
    # in the foreground was unkillable from the keyboard. serve_command must run
    # serve_forever on a background thread (leaving the main thread free to catch the
    # interrupt) and always tear the server down via shutdown() + server_close().
    import threading
    import types

    class FakeServer:
        def __init__(self, address, app):
            self.server_address = (address[0], 8765)
            self.events = []
            self.serve_on_main = None

        def page(self):
            self.events.append("page")

        def serve_forever(self):
            self.serve_on_main = threading.current_thread() is threading.main_thread()
            self.events.append("serve")  # returns at once -> the join loop then exits

        def shutdown(self):
            self.events.append("shutdown")

        def server_close(self):
            self.events.append("close")

    made = {}
    real = ot.web.ReportServer
    ot.web.ReportServer = lambda address, app: made.setdefault("s", FakeServer(address, app))
    try:
        args = types.SimpleNamespace(bind="127.0.0.1", port=0, web=False)
        rc = ot.web.serve_command(app_with([workflow("w1", "2026-05-01 10:00:00")]), args)
    finally:
        ot.web.ReportServer = real
    server = made["s"]
    assert rc == 0
    assert server.serve_on_main is False  # never the foreground / main thread
    assert "serve" in server.events
    assert server.events[-2:] == ["shutdown", "close"]  # always torn down


def test_cli_web_flag_is_recognized_and_is_distinct_from_serve():
    # --web is its own flag; web_command/main route it through the serve path.
    import sys as _sys

    argv = _sys.argv
    _sys.argv = ["opentab", "--web"]
    try:
        args = ot.parse_args()
    finally:
        _sys.argv = argv
    assert args.web is True and args.serve is False
    assert args.port == 8321 and args.bind == "127.0.0.1"  # shared with --serve


def test_web_open_report_opens_a_browser_and_survives_a_headless_box():
    # --web pops the browser open cross-platform via stdlib webbrowser; a box with no
    # browser must return False, never raise, so serving keeps running.
    import webbrowser

    calls = []
    real_open = webbrowser.open

    def fake_open(url, new=0, autoraise=True):
        calls.append((url, new))
        return True

    def boom(*a, **k):
        raise webbrowser.Error("no browser found")

    webbrowser.open = fake_open
    try:
        assert ot.web.open_report("http://localhost:8321/") is True
        assert calls == [("http://localhost:8321/", 2)]  # new=2 -> a new tab
        webbrowser.open = boom
        assert ot.web.open_report("http://localhost:8321/") is False
    finally:
        webbrowser.open = real_open


def test_pi_turns_timeline_groups_by_prompt_and_meters_cost():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _pi_session(PI_SID, cwd, ts="2026-05-15T07:32:15.949Z"),
            _pi_user("first ask", mid="u1", ts="2026-05-15T07:32:20.000Z"),
            _pi_assistant(
                "anthropic/claude-sonnet-4",
                100,
                50,
                cost=0.01,
                mid="a1",
                ts="2026-05-15T07:32:30.000Z",
            ),
            _pi_user("second ask\nwith detail", mid="u2", ts="2026-05-15T07:33:00.000Z"),
            _pi_assistant(
                "openai/gpt-5.2",
                10,
                5,
                cost=0.5,
                provider="openai-codex",  # plan route: its cost is an estimate, not spend
                mid="a2",
                ts="2026-05-15T07:33:10.000Z",
            ),
        ]
        _pi_write(root, "--proj--", PI_SID, rows)
        store = ot.PiStore(root, _pi_args())
        store.workflows()
        assert store.supports_turns(PI_SID)
        t = store.message_timeline(PI_SID)
        assert [r["prompt_title"] for r in t] == ["first ask", "second ask with detail"]
        assert t[1]["prompt_full"] == "second ask\nwith detail"  # raw, line breaks kept
        assert t[0]["cost"] == 0.01 and t[0]["model_name"] == "anthropic/claude-sonnet-4"
        assert t[1]["cost"] == 0.0  # subscription turn stays $0 (the "$" view estimates)
        assert t[0]["tokens_total"] == 150 and t[0]["time"].startswith("2026-05-15")


def test_openclaw_turns_timeline_groups_by_prompt():
    with tempfile.TemporaryDirectory() as root:
        rows = [
            _ocl_user("build the dashboard", mid="u1", ts="2026-04-27T16:00:00.000Z"),
            _ocl_msg(
                "claude-opus-4-6",
                100,
                40,
                cost=0.02,
                provider="anthropic",
                mid="a1",
                ts="2026-04-27T16:00:16.401Z",
            ),
            _ocl_msg(
                "claude-opus-4-6",
                50,
                20,
                cost=0.01,
                provider="anthropic",
                mid="a2",
                ts="2026-04-27T16:01:00.000Z",
            ),
        ]
        _ocl_write(root, "finance-os", "ses-t1", rows)
        store = ot.OpenClawStore(root, _ocl_args())
        store.workflows()
        assert store.supports_turns("ses-t1")
        t = store.message_timeline("ses-t1")
        assert len(t) == 2  # chronological, both under the one prompt
        assert [r["prompt_title"] for r in t] == ["build the dashboard"] * 2
        assert t[0]["cost"] == 0.02 and t[1]["cost"] == 0.01  # metered: real spend
        assert t[0]["model_name"] == "anthropic/claude-opus-4-6"
        assert t[0]["time"] <= t[1]["time"] and t[0]["time"].startswith("2026-04-27")


def test_codex_turns_timeline_from_cumulative_deltas():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_user("write the parser", ts="2025-10-03T14:51:05.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:51:10.000Z"),
            _codex_tokens(1000, 200, 100, 1200, ts="2025-10-03T14:51:20.000Z"),
            _codex_user("now add tests", ts="2025-10-03T14:52:00.000Z"),
            _codex_tokens(2500, 500, 600, 3000, ts="2025-10-03T14:52:30.000Z"),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        store.workflows()
        assert store.supports_turns(CODEX_SID)
        t = store.message_timeline(CODEX_SID)
        assert len(t) == 2  # one row per accepted cumulative delta
        assert [r["prompt_title"] for r in t] == ["write the parser", "now add tests"]
        assert t[0]["input"] == 900 and t[0]["cache_read"] == 100 and t[0]["output"] == 200
        assert t[1]["input"] == 1000 and t[1]["cache_read"] == 500 and t[1]["output"] == 300
        assert all(r["cost"] == 0.0 for r in t)  # Codex records none; "$" estimates
        assert t[0]["model_name"] == "openai/gpt-5-codex"


def test_csv_turns_timeline_groups_prompts_and_dedupes_requests():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.csv")
        _write_csv(
            path,
            [
                "timestamp",
                "model",
                "input_tokens",
                "output_tokens",
                "session_id",
                "prompt",
                "request_id",
            ],
            [
                ["2026-06-01T10:00:00Z", "gpt-4o", 100, 20, "s1", "fix the bug", "r1"],
                ["2026-06-01T10:05:00Z", "gpt-4o", 50, 10, "s1", "fix the bug", "r2"],
                ["2026-06-01T10:05:00Z", "gpt-4o", 50, 10, "s1", "fix the bug", "r2"],  # dupe
                ["2026-06-01T10:10:00Z", "claude-sonnet-4", 30, 5, "s1", "now the docs", "r3"],
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        w = store.workflows()[0]
        assert w.title == "fix the bug"  # no title column -> first prompt
        assert w.total_tokens == 100 + 20 + 50 + 10 + 30 + 5  # the r2 dupe dropped
        assert store.supports_turns("s1")
        t = store.message_timeline("s1")
        assert len(t) == 3
        assert [r["prompt_title"] for r in t] == ["fix the bug", "fix the bug", "now the docs"]
        assert t[0]["prompt_id"] == "fix the bug"  # no explicit id -> the text groups
        assert t[2]["model_name"] == "anthropic/claude-sonnet-4"


def test_copilot_turns_timeline_is_headerless():
    with tempfile.TemporaryDirectory() as tmp:
        otel = os.path.join(tmp, ".copilot", "otel")
        _write_otel(
            otel,
            [
                _otel_chat("sess-1", "gpt-5", 1000, 100, cache_read=200, trace="t1", span="s1"),
                _otel_chat(
                    "sess-1", "claude-sonnet-4", 500, 50, trace="t2", span="s2", end=(1775934300, 0)
                ),
            ],
        )
        store = ot.CopilotStore(otel, _copilot_args(otel))
        store.workflows()
        assert store.supports_turns("sess-1")
        t = store.message_timeline("sess-1")
        assert len(t) == 2
        # OTEL captures no prompt content by default -> headerless rows (one group).
        assert all(r["prompt_id"] == "" and r["prompt_title"] == "" for r in t)
        assert t[0]["model_name"] == "openai/gpt-5" and t[0]["input"] == 800
        assert t[0]["time"] <= t[1]["time"] and t[0]["time"].startswith("2026-")


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
