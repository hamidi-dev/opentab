import contextlib
import os
from types import SimpleNamespace

import opentab as ot

from tests._support import (
    AttrScreen,
    FakeScreen,
    FakeStore,
    _app_on_session,
    _model_row,
    app_with,
    box_title,
    fleet_app,
    screen_text,
    workflow,
)


def test_terminal_resize_does_not_close_overlays():
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


def test_startup_warning_is_prominent_dismissible_and_prioritized_over_price_prompt():
    app = app_with([])
    warning = {
        "id": "retention-v1",
        "title": "History expires",
        "headline": "Source data is temporary.",
        "lines": ["OpenTab cannot rebuild deleted records."],
    }
    app.offer_startup_warning(warning)
    app.price_prompt = True

    # Unbound keys are swallowed; the modal cannot disappear by typo or operate the one below.
    assert app.handle_key(None, ord("x")) is True
    assert app.startup_warning is not None and app.price_prompt
    screen = FakeScreen(24, 90)
    real_pair = ot.curses.color_pair
    ot.curses.color_pair = lambda _n: 0
    try:
        app.renderer.draw_startup_warning(screen, 24, 90)
    finally:
        ot.curses.color_pair = real_pair
    text = screen_text(screen)
    assert "History expires" in text and "DATA LOSS RISK" in text
    for centered in (
        "DATA LOSS RISK",
        "Source data is temporary.",
        "OpenTab cannot rebuild deleted records.",
        "continue for now",
        "don't warn again",
    ):
        row = next(line for line in text.splitlines() if centered in line)
        inner = row[1:-1]
        left = len(inner) - len(inner.lstrip())
        right = len(inner) - len(inner.rstrip())
        assert abs(left - right) <= 1
    title_row = next(line for line in text.splitlines() if "History expires" in line)
    left, right = title_row.split("History expires")
    assert abs(len(left) - len(right)) <= 1

    # Enter continues for this run; the warning is offered again next launch.
    app.handle_key(None, 10)
    assert app.startup_warning is None and not app.dismissed_startup_warnings
    assert app.price_prompt  # the lower prompt was never touched

    app.offer_startup_warning(warning)
    app.handle_key(None, ord("d"))
    assert app.startup_warning is None
    assert app.dismissed_startup_warnings == {"retention-v1"}
    app.offer_startup_warning(warning)
    assert app.startup_warning is None

    no_state = app_with([])
    no_state.offer_startup_warning(warning, can_persist=False)
    no_state.handle_key(None, ord("d"))
    assert not no_state.dismissed_startup_warnings and "--no-state" in no_state.notice


def test_startup_warnings_queue_so_a_second_harness_is_never_hidden():
    app = app_with([])
    first = {"id": "claude-retention-v1", "title": "Claude", "headline": "a", "lines": []}
    second = {"id": "gemini-retention-v1", "title": "Gemini", "headline": "b", "lines": []}
    app.offer_startup_warning(first)
    app.offer_startup_warning(second)
    app.offer_startup_warning(dict(second))  # already queued -- never twice
    assert app.startup_warnings() == [first, second]

    screen = FakeScreen(24, 90)
    real_pair = ot.curses.color_pair
    ot.curses.color_pair = lambda _n: 0
    try:
        app.renderer.draw_startup_warning(screen, 24, 90)
    finally:
        ot.curses.color_pair = real_pair
    assert "(1 more warning)" in screen_text(screen)

    # "continue" advances rather than clearing the queue: it dismisses nothing.
    app.handle_key(None, 10)
    assert app.startup_warning == second and not app.dismissed_startup_warnings
    app.handle_key(None, ord("d"))
    assert app.startup_warning is None
    assert app.dismissed_startup_warnings == {"gemini-retention-v1"}

    # A dismissed id stays out of the queue while the other still shows.
    later = app_with([])
    later.dismissed_startup_warnings = {"gemini-retention-v1"}
    later.offer_startup_warning(first)
    later.offer_startup_warning(second)
    assert later.startup_warnings() == [first]


def test_frame_draws_the_heavy_box_without_hline():
    renderer = app_with([workflow("a", "2026-06-01 12:00:00")]).renderer
    screen = FakeScreen(height=10, width=20)
    renderer.frame(screen, 0, 0, 4, 12, 0, *renderer._HEAVY_FRAME)
    assert screen_text(screen).splitlines() == [
        "┏━━━━━━━━━━┓",
        "┃          ┃",  # the pane's own rows are painted by the caller
        "┃          ┃",
        "┗━━━━━━━━━━┛",
    ]


@contextlib.contextmanager
def _acs_constants():
    # curses defines the ACS_* line constants only after initscr(), so a headless test of
    # the fallback frame has to supply them. Yields {name: value} to assert against, and
    # resets the tri-state _heavy_frame so one test's verdict can't leak into the next.
    names = ("ULCORNER", "URCORNER", "LLCORNER", "LRCORNER", "HLINE", "VLINE")
    saved_curses = {n: getattr(ot.curses, f"ACS_{n}", None) for n in names}
    saved_heavy = ot.Renderer._heavy_frame
    for i, name in enumerate(names):
        setattr(ot.curses, f"ACS_{name}", i)
    try:
        yield {name: i for i, name in enumerate(names)}
    finally:
        ot.Renderer._heavy_frame = saved_heavy
        for name, value in saved_curses.items():
            if value is None:
                delattr(ot.curses, f"ACS_{name}")
            else:
                setattr(ot.curses, f"ACS_{name}", value)


def test_frame_falls_back_to_acs_on_a_non_unicode_screen():
    renderer = app_with([workflow("a", "2026-06-01 12:00:00")]).renderer
    screen = FakeScreen(height=10, width=20)
    with _acs_constants() as acs:
        ot.Renderer._heavy_frame = False
        renderer.draw_frame(screen, 0, 0, 4, 12, 0)
        # ACS ints, not glyphs -- and via hline/vline, which a single byte fits.
        assert screen.cells[(0, 0)] == acs["ULCORNER"]
        assert screen.cells[(3, 11)] == acs["LRCORNER"]
        assert screen.cells[(0, 5)] == acs["HLINE"]
        assert screen.cells[(1, 0)] == acs["VLINE"]


def test_frame_falls_back_when_a_narrow_curses_build_rejects_the_glyphs():
    renderer = app_with([workflow("a", "2026-06-01 12:00:00")]).renderer

    class NarrowScreen(FakeScreen):
        def addch(self, y, x, ch, attr=0):
            if isinstance(ch, str) and len(ch.encode()) > 1:
                raise OverflowError("byte doesn't fit in chtype")
            super().addch(y, x, ch, attr)

    screen = NarrowScreen(height=10, width=20)
    with _acs_constants() as acs:
        ot.Renderer._heavy_frame = True  # what a UTF-8 locale resolves to
        renderer.draw_frame(screen, 0, 0, 4, 12, 0)
        assert ot.Renderer._heavy_frame is False  # and stays down for the whole run
        assert screen.cells[(0, 0)] == acs["ULCORNER"]
        assert screen.cells[(1, 0)] == acs["VLINE"]


def test_page_keys_stride_lists_by_half_a_screen():
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
    # with a real screen the stride is half the pager height (the window minus
    # Renderer.CHROME_ROWS: app frame + header + footer + the detail box's own border)
    assert app._page_step(FakeScreen(31, 80)) == 10
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


def test_theme_pairs_respect_a_pair_starved_terminal():
    renderer = app_with([workflow("a", "2026-06-01 12:00:00")]).renderer
    made = []
    saved = {k: getattr(ot.curses, k, None) for k in ("COLORS", "COLOR_PAIRS", "init_pair")}
    try:
        ot.curses.COLORS = 8
        ot.curses.COLOR_PAIRS = 8
        ot.curses.init_pair = lambda pair, fg, bg: made.append(pair)
        renderer.app.colors_ok = True
        renderer.app.has256 = False
        renderer.init_theme_colors()  # the exact call that crashed on minitel1
        assert made and max(made) <= 7  # roles landed, nothing past the terminal's 8
        assert not renderer._tool_heat_ok  # treemap switches to its glyph fallback
    finally:
        for key, value in saved.items():
            if value is None:
                delattr(ot.curses, key)
            else:
                setattr(ot.curses, key, value)


def test_color_index_never_exceeds_an_8_color_palette():
    renderer = app_with([workflow("a", "2026-06-01 12:00:00")]).renderer
    renderer._theme_color_cache = {}
    renderer._can_change = False  # what init_theme_colors resolves on a dumb palette
    saved = getattr(ot.curses, "COLORS", None)
    try:
        ot.curses.COLORS = 8
        for hexval in renderer.app.theme["roles"].values():
            assert 0 <= renderer._color_index(hexval) <= 7
        renderer._theme_color_cache = {}
        ot.curses.COLORS = 256  # a 256-color terminal keeps the finer mapping
        assert renderer._color_index("#c0caf5") > 7
    finally:
        if saved is None:
            delattr(ot.curses, "COLORS")
        else:
            ot.curses.COLORS = saved


@contextlib.contextmanager
def _truecolor_curses(recorded_colors, recorded_pairs):
    # A 256-colour terminal that accepts init_color, with both calls recorded.
    keys = ("COLORS", "COLOR_PAIRS", "init_color", "init_pair", "can_change_color")
    saved = {k: getattr(ot.curses, k, None) for k in keys}
    try:
        ot.curses.COLORS = 256
        ot.curses.COLOR_PAIRS = 256
        ot.curses.can_change_color = lambda: True
        ot.curses.init_color = lambda idx, r, g, b: recorded_colors.__setitem__(idx, (r, g, b))
        ot.curses.init_pair = lambda pair, fg, bg: recorded_pairs.__setitem__(pair, (fg, bg))
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                delattr(ot.curses, key)
            else:
                setattr(ot.curses, key, value)


def test_every_custom_colour_is_written_to_its_bold_twin():
    colors, pairs = {}, {}
    renderer = app_with([workflow("a", "2026-06-01 12:00:00")]).renderer
    renderer.app.colors_ok = True
    renderer.app.has256 = True
    with _truecolor_curses(colors, pairs):
        renderer.init_theme_colors()
    assert colors, "a truecolor terminal must still get exact colours"
    primaries = [idx for idx in colors if idx & 8 == 0]
    assert primaries, "primary slots must sit in the bit-3-clear half-blocks"
    for idx in primaries:
        assert colors.get(idx + 8) == colors[idx], f"slot {idx} has no matching bold twin"
    # The exact regression: bold secondary text must stay secondary text.
    ink2 = renderer._color_index(renderer.app.theme["roles"]["ink2"])
    assert pairs[1][0] == ink2
    assert colors[ink2 | 8] == colors[ink2]
    assert colors[ink2 | 8] != ot.themes.hex_rgb1000("#101014")


def test_no_init_color_keeps_themes_distinguishable_on_a_lying_terminal():
    colors, pairs = {}, {}
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.colors_ok = True
    app.has256 = True
    app.allow_init_color = False
    seen = []
    with _truecolor_curses(colors, pairs):
        for theme_id in ("tokyo-night", "gruvbox", "tokyo-night-day"):
            app.select_theme(theme_id, announce=False)
            seen.append(tuple(pairs[n][0] for n in range(1, 8)))
    assert not colors, "--no-init-color must not redefine a single palette slot"
    assert len(set(seen)) == 3, "each theme must land on its own standard-palette colours"
    assert all(0 <= fg <= 255 for row in seen for fg in row)
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
    app = app_with([workflow("a", "2026-06-10 12:00:00")])
    app.handle_key(None, ord("?"))
    assert app.help
    app.handle_key(None, ord("C"))
    assert app.theme_menu and app.help
    app.handle_key(None, 27)
    assert not app.theme_menu and app.help


def test_source_and_demo_toggles_route_from_inside_overlays():
    app = app_with([workflow("a", "2026-06-10 12:00:00", cost=5)])
    app._models_loaded = True
    calls = []
    app.open_source_menu = lambda: calls.append("source")  # bare Args has no flags
    app.toggle_demo = lambda: calls.append("demo")
    app.handle_key(None, ord("T"))
    app.handle_key(None, ord("H"))
    app.handle_key(None, ord("D"))
    assert calls == ["source", "demo"] and app.trends
    app.handle_key(None, ord("q"))
    app.handle_key(None, ord("P"))
    app.handle_key(None, ord("H"))
    app.handle_key(None, ord("D"))
    assert calls == ["source", "demo", "source", "demo"] and app.show_prices


def test_data_swap_reanchors_overlay_cursors():
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
    assert app.month_index == 1 and app.view == "browse"
    app._apply_click(("month", 1), drill=True)
    assert app.view == "zoom"
    app._apply_click(("tab", 2), drill=False)
    assert app.tab == 2


def test_tab_click_in_browse_preview_zooms_into_the_detail():
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
    app.handle_key(None, ord("j"))
    assert app.workflow_index == 1
    app.handle_key(None, 27)
    assert app.view == "browse"


def test_plus_drills_from_browse_and_toggles_maximize_in_zoom():
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
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    r = app.renderer
    r.regions = [("tab", 4, 30, 40, 1), ("rows", "detail", 3, 20, 28, 100, 0)]
    assert r.hit(4, 35) == ("tab", 1)  # first match wins: the tab, not the pane
    assert r.hit(10, 50) == ("detail", 7)
    app._apply_click(("detail", 7), drill=False)
    assert app.view == "zoom"  # a click on empty preview space focuses the pane
    app._apply_click(("detail", 7), drill=False)
    assert app.view == "zoom"  # already focused: inert


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


def test_mouse_wheel_scrolls_the_panel_under_the_cursor():
    app = app_with(
        [workflow(f"d{i}", f"2026-06-{i + 1:02d} 12:00:00") for i in range(6)]
        + [workflow("m", "2026-05-10 12:00:00")]
    )
    app.focus = "months"  # Months is the active panel
    app._wheel_down = getattr(ot.curses, "BUTTON5_PRESSED", 0) or ot.curses.REPORT_MOUSE_POSITION
    # A "day" rows region at content y 10..20, as draw() would register for the Days panel.
    app.renderer.oy = app.renderer.ox = 0
    app.renderer.regions = [("rows", "day", 10, 20, 0, 30, 0)]
    orig = ot.curses.getmouse
    try:
        mo_before = app.month_index
        ot.curses.getmouse = lambda: (0, 5, 12, 0, app._wheel_down)  # wheel down over Days
        app.handle_mouse()
        assert app.month_index == mo_before  # the active Months panel did NOT move
        assert app.day_index == 3  # the hovered Days panel scrolled by +3
    finally:
        ot.curses.getmouse = orig


def test_clicks_are_translated_out_of_the_app_frame():
    app = app_with(
        [
            workflow("jun", "2026-06-01 12:00:00"),
            workflow("may", "2026-05-01 12:00:00"),
        ]
    )
    app.focus = "months"
    app.renderer.oy = app.renderer.ox = 1  # what draw() sets once the frame is up
    app.renderer.regions = [("rows", "month", 5, 6, 0, 30, 0)]  # content rows 5..6
    orig = ot.curses.getmouse
    try:
        # A click on screen row 7 is content row 6 -- the second month, not the first.
        ot.curses.getmouse = lambda: (0, 5, 7, 0, ot.curses.BUTTON1_CLICKED)
        app.handle_mouse()
        assert app.month_index == 1
        ot.curses.getmouse = lambda: (0, 5, 6, 0, ot.curses.BUTTON1_CLICKED)
        app.handle_mouse()
        assert app.month_index == 0
    finally:
        ot.curses.getmouse = orig


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


def test_source_and_demo_switches_do_not_bury_the_notes_warning():
    assert ot.save_notes({"a": "keep me"})
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00")], "a")
    app.refresh_notes()
    with open(ot.notes_path(), "w") as fh:
        fh.write("{ truncated")

    app._reload_for_source()
    assert not app._notes_ok
    assert "unreadable" in app.notice  # the warning is what survives, not "real data"
    os.unlink(ot.notes_path())


def test_prompt_layout_degenerate_widths():
    head, hint = " note: ", "Enter saves"
    # A pane too narrow for even one cell of field must not crash or paint garbage.
    shown, hx, max_len = ot.App.prompt_layout("abc", 4, head, hint)
    assert max_len == 1 and shown == "" and hx == ot.display_width(head)
    assert ot.App.prompt_layout("", 80, head, hint)[0] == ""  # empty value, empty field


def test_prompt_step_edit_keys():
    step = ot.App.filter_prompt_step
    assert step("ab", "c", 10) == ("abc", False, False)  # a typed character (get_wch)
    assert step("ab", "ü", 10) == ("abü", False, False)  # ... including a wide one
    assert step("ab", "c", 2) == ("ab", False, False)  # at max_chars: no more input
    assert step("ab", ot.curses.KEY_BACKSPACE, 10) == ("a", False, False)
    assert step("one two", 23, 10) == ("one ", False, False)  # Ctrl-W: back a word
    assert step("one", 23, 10) == ("", False, False)  # ... and the last word too
    assert step("one two", 21, 10) == ("", False, False)  # Ctrl-U: kill the line
    assert step("x", "\x1b", 10) == ("x", False, True)  # Esc cancels (str form)
    assert step("x", "\n", 10) == ("x", True, False)  # Enter commits (str form)
    assert step("x", 10, 10) == ("x", True, False)  # ... and as an int


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


def test_showing_ignored_rows_agrees_between_preview_and_picker():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", title="kept"),
            workflow("b", "2026-06-02 12:00:00", title="ignored-one"),
        ]
    )
    app.focus = "months"
    app.ignored_sessions = {"b"}
    app.show_ignored_projects = True
    app._invalidate_workflow_cache()
    preview = app.renderer.month_workflows(app.selected_month_summary, 96)
    assert [w.id for w in app.current_sessions()] == ["a", "b"]  # the picker's rows
    assert any("ignored-one" in ln for ln in preview)  # ...and the preview's

    proj = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", directory="/tmp/alpha"),
            workflow("b", "2026-06-02 12:00:00", directory="/tmp/beta"),
        ]
    )
    proj.focus = "months"
    proj.ignored_projects = {"/tmp/beta"}
    proj.show_ignored_projects = True
    proj._invalidate_workflow_cache()
    lines = proj.renderer.month_projects(proj.selected_month_summary, 96)
    assert [p.directory for p in proj.zoom_projects()] == ["/tmp/alpha", "/tmp/beta"]
    assert any("/tmp/beta" in ln for ln in lines)  # the ignored project, "×"-marked


def test_digit_keys_jump_to_a_panel_lazygit_style():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", directory="/tmp/alpha"),
            workflow("b", "2026-06-02 12:00:00", directory="/tmp/beta"),
        ]
    )
    assert app.focus == "days"  # the default panel
    app.handle_key(None, ord("1"))
    assert app.focus == "years" and app.view == "browse"
    app.handle_key(None, ord("2"))
    assert app.focus == "months"
    app.handle_key(None, ord("3"))
    assert app.focus == "days"

    # 0 is the pane on the right: it makes the detail active, exactly like Enter.
    app.handle_key(None, ord("0"))
    assert app.view == "zoom"
    app.handle_key(None, ord("0"))
    assert app.view == "zoom"  # already there; not a toggle

    # A digit jumps from anywhere: it steps out of the zoom to reach the panel...
    app.handle_key(None, ord("2"))
    assert app.view == "browse" and app.focus == "months"
    # ...and out of an open session, dropping the drill state with it.
    app.tab = app.month_tabs.index("Sessions")
    app.handle_key(None, 10)  # zoom
    app.zoom_project = "/tmp/alpha"
    app.handle_key(None, 10)  # session
    assert app.view == "session"
    app.handle_key(None, ord("3"))
    assert app.view == "browse" and app.focus == "days" and app.zoom_project is None

    # The detail tab is carried across the jump, like Tab does (Models stays Models).
    app.handle_key(None, ord("2"))
    app.tab = app.month_tabs.index("Models")
    app.handle_key(None, ord("1"))
    assert app.current_tabs()[app.tab] == "Models"


def test_the_sidebar_column_headers_wear_the_shared_table_header_look():
    app = fleet_app({"alpha": [workflow("a", "2026-06-01 12:00:00", directory="/repo/x")]})
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: n * 100
    ot.curses.init_pair = lambda *a: None
    try:
        lit = ot.curses.color_pair(ot.Renderer.HEADER_PAIR) | ot.curses.A_BOLD
        grey = ot.curses.color_pair(4) | ot.curses.A_BOLD  # what it used to be
        for draw, label in (
            (app.renderer.draw_project_list, "Project"),
            (app.renderer.draw_machine_list, "Machine"),
        ):
            screen = AttrScreen(27, 60)
            draw(screen, 0, 0, 27, 40)
            # Row 1 is the header, right under the panel's top border.
            row = "".join(screen.cells.get((1, x), " ") for x in range(40))
            assert label in row and "Cost" in row
            assert screen.attrs[(1, 1)] == lit and screen.attrs[(1, 1)] != grey
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip


def _panel_titles(app):
    # The titles the panels hand to box() (box itself draws ACS glyphs, which need a
    # real curses screen -- the titles are what this is about).
    screen = FakeScreen(30, 120)
    titles: list[str] = []
    real_box = app.renderer.box
    app.renderer.box = lambda s, y, x, h, w, title, active=False: titles.append(title)
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: 0
    ot.curses.init_pair = lambda *a: None
    try:
        if app.browse_mode == "projects":
            app.renderer.draw_project_list(screen, 0, 0, 27, 40)
            app.renderer.draw_project_detail(screen, 0, 40, 27, 80, active=False)
        else:
            app.renderer.draw_time_panels(screen, 0, 27, 40, focus=app.focus)
            app.renderer.draw_month_detail(screen, 0, 40, 27, 80, active=False)
    finally:
        app.renderer.box = real_box
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    return titles


def test_a_panel_jump_never_carries_a_tab_index_across_scopes():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.focus = "months"
    app.tab = app.month_tabs.index("Sessions")
    app.handle_key(None, 10)  # zoom
    app.handle_key(None, 10)  # open the session
    app.tab = app.current_tabs().index("Subagents")

    app.handle_key(None, ord("1"))  # jump to Years, which has no Subagents tab
    assert app.view == "browse" and app.focus == "years"
    assert app.current_tabs()[app.tab] == "Overview"

    # The same, jumping back to the panel we came from.
    app.tab = app.month_tabs.index("Sessions")
    app.handle_key(None, 10)
    app.handle_key(None, 10)
    app.tab = app.current_tabs().index("Subagents")
    app.handle_key(None, ord("2"))  # months: the panel the session belongs to
    assert app.view == "browse" and app.focus == "months"
    assert app.current_tabs()[app.tab] == "Overview"

    # A tab the target scope *does* have is still carried, like Tab does.
    app.tab = app.month_tabs.index("Models")
    app.handle_key(None, ord("1"))
    assert app.current_tabs()[app.tab] == "Models"


def test_each_panel_wears_its_jump_key_in_its_title():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/tmp/alpha")])
    app.focus = "months"
    titles = _panel_titles(app)
    assert titles[0] == "[1] Years"
    assert titles[1] == "[2] Months ▸"  # the focused panel keeps its ▸ marker
    assert titles[2].startswith("[3] Days")
    assert titles[3] == "[0] Month 2026-06"

    app.set_browse_mode("projects")  # one left panel here, so it is 1
    titles = _panel_titles(app)
    assert titles[0] == "[1] Projects ▸"
    assert titles[1].startswith("[0] Project ")


def test_digit_keys_in_projects_mode_name_the_one_left_panel():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/tmp/alpha")])
    app.set_browse_mode("projects")
    app.handle_key(None, 10)  # zoom into the project
    assert app.view == "zoom"
    app.handle_key(None, ord("1"))  # 1 is the Projects list
    assert app.view == "browse" and app.browse_mode == "projects"
    app.handle_key(None, 10)
    assert app.view == "zoom"
    app.handle_key(None, ord("2"))  # no second panel here: nothing happens
    assert app.view == "zoom" and app.browse_mode == "projects"


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
    app.sort_reverse = True  # a header re-click flips the *groups* to z->a...
    rows = app.sorted_workflows(app.loaded)
    # ...but each project still leads with its costliest session.
    assert [w.id for w in rows] == ["b-costly", "b-cheap", "a"]


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
    assert rnd.sort_hit(2, 1 + header.index("Subagents")) == ("subagents", "project")
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
    app.apply_header_sort("tokens", "session")
    assert app.sort_by == "tokens" and app.sort_reverse is False
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["a", "b"]
    app.apply_header_sort("tokens", "session")
    assert app.sort_reverse is True
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["b", "a"]
    app.apply_header_sort("title", "session")
    assert app.sort_by == "title" and app.sort_reverse is False


def test_last_activity_sort_orders_by_activity_and_falls_back_to_created_at():
    app = app_with(
        [
            # Started first but stayed active longest -- last_activity ranks it above
            # "b"/"c", even though "date" (created_at) ranks it near the bottom.
            workflow("a", "2026-06-01 12:00:00", ended_at="2026-06-05 09:00:00"),
            workflow("b", "2026-06-03 12:00:00"),  # no ended_at -> falls back to created_at
            workflow("c", "2026-06-02 12:00:00"),  # no ended_at -> falls back to created_at
            # No ended_at, but its OWN created_at is later than "a"'s ended_at -- this
            # only ranks first if the fallback key is really (ended_at or created_at),
            # not bare ended_at (which would leave it with an empty-string key and
            # stuck behind every session that has any ended_at at all).
            workflow("d", "2026-06-07 12:00:00"),
        ]
    )
    app.focus = "months"  # last_activity is unreachable while the Days pane is focused
    app.sort_by = "last_activity"
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["d", "a", "b", "c"]

    app.sort_by = "date"
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["d", "b", "c", "a"]


def test_last_activity_sort_is_unavailable_while_the_days_pane_is_focused():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", ended_at="2026-06-05 09:00:00"),
            workflow("b", "2026-06-02 12:00:00"),
        ]
    )
    app.focus = "months"
    app.tab = len(app.current_tabs()) - 1  # the Sessions tab (always last, day/month/year)
    assert "last_activity" in app.current_sort_options()
    assert "last_activity" in app.sort_menu_options()

    app.focus = "years"
    app.tab = len(app.current_tabs()) - 1  # re-set: day/month/year tabs aren't all the same length
    assert "last_activity" in app.current_sort_options()

    app.focus = "days"
    app.tab = len(app.current_tabs()) - 1  # day_tabs is a different tuple/length
    options = app.current_sort_options()
    # "cost" staying present is what makes the negative checks below meaningful --
    # without it they'd pass just as well if current_sort_options() returned ()
    # entirely (e.g. on_sessions_tab wrongly False), which is a different bug.
    assert "cost" in options and "last_activity" not in options
    assert "last_activity" not in app.sort_menu_options()
    # A leftover self.focus from a previous Time-mode session must not leak the
    # restriction into Projects mode, where "days" means nothing.
    app.set_browse_mode("projects")
    app.tab = len(app.current_tabs()) - 1
    assert "last_activity" in app.current_sort_options()


def test_last_activity_sort_falls_back_and_resumes_across_a_day_focus_round_trip():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, ended_at="2026-06-05 09:00:00"),
            workflow("b", "2026-06-02 12:00:00", cost=5),
        ]
    )
    app.sort_by = "last_activity"
    app.focus = "months"
    assert app.session_sort_key() == "last_activity"
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["a", "b"]

    app.focus = "days"
    # Falls back to "date" (SORT_FALLBACKS), NOT to sort_options[0]: withdrawing a
    # time sort must not silently answer with money. "b" is both the newer start and
    # the pricier row, so the assertion below can't tell the two apart -- the check
    # that can is session_sort_key(), plus the dedicated cost/date test underneath.
    assert app.session_sort_key() == "date"
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["b", "a"]
    assert app.sort_by == "last_activity"  # the stored preference itself is untouched

    app.focus = "months"
    assert app.session_sort_key() == "last_activity"  # resumes, nothing was lost


def test_withdrawn_last_activity_falls_back_to_date_not_cost_on_the_opening_screen():
    app = app_with(
        [
            workflow("pricey", "2026-06-01 12:00:00", cost=99),
            workflow("cheap-but-recent", "2026-06-09 12:00:00", cost=1),
        ]
    )
    app.sort_by = "last_activity"
    app.focus = "days"
    assert app.session_sort_key() == "date"
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["cheap-but-recent", "pricey"]
    # ...and the arrow follows it onto the Date column, so the visible order is
    # explained by the header rather than by an unmarked column.
    rnd = app.renderer
    assert rnd.session_date_column()[0] == "date"
    assert rnd.sort_heading("date", "Time").endswith(" v")
    assert rnd.sort_heading("cost", "Cost") == "Cost"  # no arrow: not the active key


def test_an_unknown_saved_sort_key_still_falls_back_to_the_head_of_the_vocabulary():
    app = app_with([workflow("a", "2026-06-01 12:00:00"), workflow("b", "2026-06-02 12:00:00")])
    app.sort_by = "no-such-column"
    for focus in ("days", "months"):
        app.focus = focus
        assert app.session_sort_key() == app.sort_options[0] == "cost", focus


def test_apply_header_sort_rejects_last_activity_while_the_days_pane_is_focused():
    app = app_with([workflow("a", "2026-06-01 12:00:00", ended_at="2026-06-05 09:00:00")])
    app.focus = "days"
    app.apply_header_sort("last_activity", "session")
    # Nothing mutated at all -- an early return, not a silent substitution.
    assert app.sort_by == "cost" and app.sort_reverse is False


def test_apply_header_sort_still_accepts_last_activity_in_projects_mode_with_stale_days_focus():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, ended_at="2026-06-05 09:00:00"),
            workflow("b", "2026-06-02 12:00:00", cost=5),
        ]
    )
    app.set_browse_mode("projects")
    assert app.focus == "days"  # confirms this exercises the leak-guard, not a no-op
    app.apply_header_sort("last_activity", "session")
    assert app.sort_by == "last_activity" and app.sort_reverse is False


def test_clicking_the_last_activity_column_sets_it_and_re_click_flips_direction():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", ended_at="2026-06-05 09:00:00"),
            workflow("b", "2026-06-02 12:00:00"),
        ]
    )
    app.focus = "months"  # last_activity is unreachable while the Days pane is focused
    app.apply_header_sort("last_activity", "session")
    assert app.sort_by == "last_activity" and app.sort_reverse is False
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["a", "b"]
    app.apply_header_sort("last_activity", "session")  # re-click flips direction
    assert app.sort_reverse is True
    assert [w.id for w in app.sorted_workflows(app.all_workflows)] == ["b", "a"]


def test_session_date_column_follows_the_active_sort():
    app = app_with([workflow("a", "2026-06-01 12:00:00", ended_at="2026-06-05 09:00:00")])
    app.focus = "months"  # a scope spanning more than one day, so the date form is "Started"
    rnd = app.renderer
    assert rnd.session_date_column() == ("date", "Started")
    assert "Started" in rnd.session_header_text(False, 0)
    assert rnd.session_sort_columns(0)[0] == ("date", "Started")  # first column, not just present

    app.sort_by = "last_activity"
    assert rnd.session_date_column() == ("last_activity", "Last act")
    assert "Last act" in rnd.session_header_text(False, 0)
    assert rnd.session_sort_columns(0)[0] == ("last_activity", "Last act")

    w = app.all_workflows[0]
    assert rnd.session_date_cell(w) == "2026-06-05"  # the activity date, not the start
    assert "2026-06-05" in rnd.session_row_text(w, " ", False, 0)


def test_session_date_column_header_never_overflows_its_field():
    app = app_with([workflow("a", "2026-06-01 12:00:00", ended_at="2026-06-05 09:00:00")])
    app.focus = "months"  # last_activity is unreachable while the Days pane is focused
    app.sort_by = "last_activity"
    rnd = app.renderer
    heading = rnd.sort_heading(*rnd.session_date_column())  # includes the " v"/" ^" arrow
    assert len(heading) <= 10
    # Every subsequent column starts at the same offset as under any other sort key.
    cost_offset_here = rnd.session_header_text(False, 0).index("Cost")
    app.sort_by = "date"
    cost_offset_under_date = rnd.session_header_text(False, 0).index("Cost")
    assert cost_offset_here == cost_offset_under_date


def test_header_arrow_reflects_sort_direction():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/tmp/a")])
    app.set_browse_mode("projects")
    rnd = app.renderer
    assert "Cost v" in rnd.project_header_text(80)  # default cost sort, descending
    app.apply_header_sort("cost", "project")  # active column -> flip to ascending
    assert "Cost ^" in rnd.project_header_text(80)


def test_sort_arrows_do_not_cross_lists_in_projects_mode():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/tmp/a")])
    app.set_browse_mode("projects")
    app.project_sort_by = "tokens"
    app.sort_by = "cost"
    rnd = app.renderer
    assert "Tokens v" in rnd.project_header_text(80)
    # The sessions preview arrows the session sort (cost), not the project sort.
    assert rnd.sort_heading("cost", "Cost") == "Cost v"
    assert rnd.sort_heading("tokens", "Tokens") == "Tokens"
    # The subagent heading reads its own pair, leaving both others untouched.
    app.subagent_sort_by = "depth"
    assert rnd.subagent_sort_heading("depth", "D") == "D ^"
    assert rnd.sort_heading("cost", "Cost") == "Cost v"


def test_preview_session_lists_register_clickable_sort_headers():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.focus = "months"
    rnd = app.renderer
    rnd._line_sort_headers = {}
    lines = rnd.month_workflows(app.selected_month_summary, 100)
    head = rnd.BOX_HEADER_LINE  # line 0 is the box's titled top border
    cols, target = rnd._line_sort_headers[head]
    assert target == "session"
    assert ("date", "Started") in cols and ("subagents", "Subagents") in cols
    # The zones are located in the text as DRAWN, so the box's gutter shifts them right
    # by exactly the cells the paint shifts the line by.
    rnd.sort_regions = []
    rnd._register_line_sort_header(5, 2, head, lines[head], 96)
    keys = {(k, t) for _y, _x0, _x1, k, t in rnd.sort_regions}
    assert ("date", "session") in keys and ("subagents", "session") in keys


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

    app.drill_in()
    assert app.view == "zoom"
    app.tab = app.month_tabs.index("Projects")

    # projects in scope are this month's only (no /tmp from May's "old")
    assert {p.directory for p in app.zoom_projects()} == {"/tmp/a", "/tmp/b"}

    app.project_index = [p.directory for p in app.zoom_projects()].index("/tmp/a")
    app.drill_in()

    assert app.zoom_project == "/tmp/a"
    assert app.on_sessions_tab
    assert {w.id for w in app.current_sessions()} == {"a1", "a2"}  # June /tmp/a only

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


def test_clear_filter_reports_when_nothing_to_clear():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert app.handle_key(None, ord("x"))
    assert app.notice == "no active filter"


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
    assert box_title(lines) == "Yearly Insight"
    assert any("Year:" in ln and "2026" in ln for ln in lines)
    # The Sessions tab is scoped to the focused year (2026 sessions only).
    app.tab = app.current_tabs().index("Sessions")
    assert {w.id for w in app.current_sessions()} == {"a", "b"}


def test_opening_a_session_steps_out_of_a_leftover_turn_drill():
    app = app_with([workflow("a", "2026-06-01 12:00:00"), workflow("b", "2026-06-02 12:00:00")])
    app.turn_drill = "p1"
    assert app.goto_session("b")  # -> drill_into_session -> drill_in -> session view
    assert app.view == "session" and app.current_session().id == "b"
    assert app.turn_drill is None
    # reload closes it too (it indexes into the turn cache that reload rebuilds)
    app.turn_drill = "q1"
    app.reload()
    assert app.turn_drill is None


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


# --- the N notices scrollback -------------------------------------------------


def test_notices_log_keeps_faded_toasts_beyond_the_live_cap():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    clock = [0.0]
    app._toast_clock = lambda: clock[0]
    # More distinct notices than the live list holds: the cards cap, the log keeps all.
    for i in range(app.TOAST_MAX + 3):
        app._mark_toasts_shown()
        app.notify(f"m{i}")
    assert len(app.toasts) == app.TOAST_MAX
    assert [t.text for t in app.toast_log] == [f"m{i}" for i in range(app.TOAST_MAX + 3)]
    # Expiry empties the live cards but NEVER the scrollback -- that's the whole point.
    clock[0] += app.TOAST_TTL + 1
    assert app.active_toasts() == []
    assert len(app.toast_log) == app.TOAST_MAX + 3
    # Clearing the current message (notice = "") leaves the history intact.
    app.notice = ""
    assert app.toasts == [] and len(app.toast_log) == app.TOAST_MAX + 3


def test_notices_log_mirrors_the_within_frame_coalesce_and_caps():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app._toast_clock = lambda: 0.0
    # Two notices in one frame collapse onto one, in BOTH the live list and the log,
    # so the log records what was shown, not the discarded midpoint.
    app.notify("fetching…")
    app.notify("done")
    assert [t.text for t in app.toasts] == ["done"]
    assert [t.text for t in app.toast_log] == ["done"]
    app._mark_toasts_shown()
    app.notify("next action")  # a distinct frame stacks
    assert [t.text for t in app.toast_log] == ["done", "next action"]
    # The scrollback is bounded: oldest fall off past TOAST_LOG_MAX.
    for i in range(app.TOAST_LOG_MAX + 5):
        app._mark_toasts_shown()
        app.notify(f"n{i}")
    assert len(app.toast_log) == app.TOAST_LOG_MAX
    assert app.toast_log[-1].text == f"n{app.TOAST_LOG_MAX + 4}"


def test_notices_overlay_opens_scrolls_and_closes():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app._toast_clock = lambda: 0.0
    for i in range(5):
        app._mark_toasts_shown()
        app.notify(f"n{i}")
    assert app.handle_key(None, ord("N")) is True
    assert app.toast_history and app.toast_history_scroll == 0
    app.handle_key(None, ord("j"))
    app.handle_key(None, ord("j"))
    assert app.toast_history_scroll == 2
    app.handle_key(None, ord("k"))
    assert app.toast_history_scroll == 1
    app.handle_key(None, ord("g"))
    assert app.toast_history_scroll == 0
    app.handle_key(None, ord("z"))  # a mistyped key is swallowed, not a close
    assert app.toast_history
    app.handle_key(None, 27)  # Esc closes
    assert not app.toast_history
    app.handle_key(None, ord("N"))  # N reopens...
    assert app.toast_history
    app.handle_key(None, ord("N"))  # ...and N again closes (its own toggle)
    assert not app.toast_history


def test_toast_history_lines_are_newest_first_with_age_and_kind():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    clock = [0.0]
    app._toast_clock = lambda: clock[0]
    # Empty log -> a single info-tinted placeholder row.
    empty = app.renderer.toast_history_lines(60)
    assert len(empty) == 1 and empty[0][1] == "info" and "No notifications yet" in empty[0][0]
    app.notify("older note")
    app._mark_toasts_shown()
    clock[0] = 65.0  # 65s later
    app.notify("disk on fire", kind="error")
    rows = app.renderer.toast_history_lines(60)
    assert rows[0][1] == "error" and "disk on fire" in rows[0][0]  # newest first, kind kept
    assert rows[1][1] == "info" and "older note" in rows[1][0]
    assert "now" in rows[0][0]  # the just-raised one
    assert "1m" in rows[1][0]  # 65s -> "1m"


def test_draw_toast_history_paints_the_scrollback_newest_first():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app._toast_clock = lambda: 0.0
    app.notify("copied ses_42", kind="success")
    app._mark_toasts_shown()
    app.notify("boom", kind="error")
    app._mark_toasts_shown()
    app.toast_history = True
    screen = FakeScreen(24, 80)
    orig_cp = ot.curses.color_pair
    ot.curses.color_pair = lambda n: 0
    try:
        app.renderer.draw_toast_history(screen, 3, 22, 80)
    finally:
        ot.curses.color_pair = orig_cp
    text = screen_text(screen)
    assert "Notifications (2)" in text  # the count is in the title
    assert "boom" in text and "copied ses_42" in text
    assert text.index("boom") < text.index("copied ses_42")  # newest painted first


def test_launch_menu_opens_in_tmux_and_copy_only_outside():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/a")
    a.source = "Claude Code"
    app = app_with([a])
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    old_tmux = os.environ.get("TMUX")
    real_backend, real_launch, real_copy = (
        ot.util.launch_backend,
        ot.util.launch_command,
        ot.util.copy_to_clipboard,
    )
    launches, copies = [], []
    try:
        ot.util.launch_backend = lambda: "tmux" if os.environ.get("TMUX") else None
        ot.util.launch_command = lambda kind, d, c, backend=None: (
            launches.append((kind, d, c, backend)) or None
        )
        ot.util.copy_to_clipboard = lambda v: copies.append(v) or True
        os.environ["TMUX"] = "/tmp/tmux-1/default,1,0"
        app.handle_key(None, ord("L"))
        assert app.launch_menu is not None and not launches  # menu open, nothing run
        app.handle_key(None, ord("w"))
        assert app.launch_menu is None
        assert app.launch_menu_backend is None
        assert launches[0][:3] == ("window", "/repo/a", "claude --resume ses_1")
        app.handle_key(None, ord("L"))
        app.handle_key(None, 27)
        assert len(launches) == 1 and "cancelled" in app.notice and app.launch_menu_backend is None
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
        ot.util.launch_backend = real_backend
        ot.util.launch_command = real_launch
        ot.util.copy_to_clipboard = real_copy
        if old_tmux is None:
            os.environ.pop("TMUX", None)
        else:
            os.environ["TMUX"] = old_tmux


def test_tmux_launch_menu_snapshot_ignores_a_hook_added_after_opening():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/a")
    a.source = "Claude Code"
    app = app_with([a])
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    real_backend = ot.util.launch_backend
    real_hook = ot.util.launcher_hook
    real_run = ot.util.subprocess.run
    calls = []
    try:
        ot.util.launch_backend = lambda: "tmux"
        ot.util.launcher_hook = lambda: None
        app.handle_key(None, ord("L"))
        assert app.launch_menu_backend == "tmux"

        ot.util.launcher_hook = lambda: "/tmp/launcher"
        ot.util.subprocess.run = lambda argv, **kwargs: (
            calls.append(argv) or SimpleNamespace(returncode=0, stderr="")
        )
        app.handle_key(None, ord("w"))
        assert calls == [["tmux", "new-window", "-c", "/repo/a", "claude --resume ses_1"]]
    finally:
        ot.util.launch_backend = real_backend
        ot.util.launcher_hook = real_hook
        ot.util.subprocess.run = real_run


def test_launch_menu_is_navigable_with_jk_and_enter():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/a")
    a.source = "Claude Code"
    app = app_with([a])
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    old_tmux = os.environ.get("TMUX")
    real_backend, real_launch = ot.util.launch_backend, ot.util.launch_command
    launches = []
    try:
        ot.util.launch_backend = lambda: "tmux"
        ot.util.launch_command = lambda kind, d, c, backend=None: (
            launches.append((kind, d, c, backend)) or None
        )
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
        assert app.launch_menu_backend is None
        assert launches[0][:3] == ("hsplit", "/repo/a", "claude --resume ses_1")
    finally:
        ot.util.launch_backend = real_backend
        ot.util.launch_command = real_launch
        if old_tmux is None:
            os.environ.pop("TMUX", None)
        else:
            os.environ["TMUX"] = old_tmux


def test_herdr_launch_menu_uses_tabs_splits_copy_and_one_backend_snapshot():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/a")
    a.source = "Claude Code"
    app = app_with([a])
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    real_backend = ot.util.launch_backend
    real_launch = ot.util.launch_command
    real_copy = ot.util.copy_to_clipboard
    old_pane = os.environ.get("HERDR_PANE_ID")
    launches, copies = [], []
    try:
        os.environ["HERDR_PANE_ID"] = "pane-42"
        ot.util.launch_backend = lambda: "herdr"
        ot.util.launch_command = lambda kind, d, c, backend=None: (
            launches.append((kind, d, c, backend)) or None
        )
        ot.util.copy_to_clipboard = lambda command: copies.append(command) or True
        app.handle_key(None, ord("L"))
        assert app.launch_menu_backend == "herdr"
        targets = app.launch_targets()
        assert [kind for _key, kind, _label in targets] == ["window", "hsplit", "vsplit", "copy"]
        assert targets[0][2] == "new tab"

        screen = FakeScreen(30, 100)
        real_pair = ot.curses.color_pair
        ot.curses.color_pair = lambda n: 0
        try:
            app.renderer.draw_launch_menu(screen, 30, 100)
        finally:
            ot.curses.color_pair = real_pair
        text = screen_text(screen)
        assert "open in herdr:" in text and "new tab" in text and "popup" not in text

        # Changing detection while the menu is open must not change its rows or dispatch.
        ot.util.launch_backend = lambda: "tmux"
        app.handle_key(None, ord("w"))
        assert app.launch_menu_backend is None
        ot.util.launch_backend = lambda: "herdr"
        _launch(app, "ses_1", ord("s"))
        _launch(app, "ses_1", ord("v"))
        _launch(app, "ses_1", ord("y"))
        assert [row[0] for row in launches] == ["window", "hsplit", "vsplit"]
        assert all(row[-1] == "herdr" for row in launches)
        assert copies == ["cd /repo/a && claude --resume ses_1"]
    finally:
        ot.util.launch_backend = real_backend
        ot.util.launch_command = real_launch
        ot.util.copy_to_clipboard = real_copy
        if old_pane is None:
            os.environ.pop("HERDR_PANE_ID", None)
        else:
            os.environ["HERDR_PANE_ID"] = old_pane


def test_herdr_launch_menu_without_pane_id_offers_only_tab_and_copy():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/a")
    a.source = "Claude Code"
    app = app_with([a])
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    real_backend = ot.util.launch_backend
    old_pane = os.environ.get("HERDR_PANE_ID")
    try:
        ot.util.launch_backend = lambda: "herdr"
        for pane_id in (None, "", "  "):
            if pane_id is None:
                os.environ.pop("HERDR_PANE_ID", None)
            else:
                os.environ["HERDR_PANE_ID"] = pane_id
            app.handle_key(None, ord("L"))
            assert [kind for _key, kind, _label in app.launch_targets()] == ["window", "copy"]
            app.handle_key(None, 27)
    finally:
        ot.util.launch_backend = real_backend
        if old_pane is None:
            os.environ.pop("HERDR_PANE_ID", None)
        else:
            os.environ["HERDR_PANE_ID"] = old_pane


def test_launcher_hook_in_herdr_restores_popup_and_launch_errors_close_menu():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/a")
    a.source = "Claude Code"
    app = app_with([a])
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    real_backend = ot.util.launch_backend
    real_launch = ot.util.launch_command
    try:
        ot.util.launch_backend = lambda: "hook"
        ot.util.launch_command = lambda kind, d, c, backend=None: "hook rejected popup"
        app.handle_key(None, ord("L"))
        assert [kind for _key, kind, _label in app.launch_targets()] == [
            "window",
            "hsplit",
            "vsplit",
            "popup",
            "copy",
        ]
        screen = FakeScreen(30, 100)
        real_pair = ot.curses.color_pair
        ot.curses.color_pair = lambda n: 0
        try:
            app.renderer.draw_launch_menu(screen, 30, 100)
        finally:
            ot.curses.color_pair = real_pair
        assert "open in launcher hook:" in screen_text(screen)
        app.handle_key(None, ord("p"))
        assert (
            app.launch_menu is None
            and app.launch_menu_backend is None
            and "launch failed: hook rejected popup" in app.notice
        )
    finally:
        ot.util.launch_backend = real_backend
        ot.util.launch_command = real_launch


def _remote_launch_app(targets):
    # A two-box fleet: "laptop" is the live local machine, "giant" a pulled one whose
    # remotes.json key is its own name. `targets` is what main() injects for `L`.
    from tests._support import fleet_app

    here = workflow("ses_here", "2026-06-01 12:00:00", directory="/repo/a")
    there = workflow("ses_there", "2026-06-02 12:00:00", directory="/srv/app")
    here.source = there.source = "Claude Code"
    app = fleet_app({"laptop": [here], "giant": [there]})
    app._ssh_targets = lambda: targets
    app.set_browse_mode("machines")
    app.view = "session"
    return app


def _launch(app, session_id, key):
    app.session_stack = []
    app.drill_into_session(session_id)
    app.handle_key(None, ord("L"))
    app.handle_key(None, key)


def test_launch_menu_uses_the_innermost_nested_multiplexer():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/a")
    a.source = "Claude Code"
    app = app_with([a])
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    old_env = {
        key: os.environ.get(key) for key in ("TMUX", "TMUX_PANE", "HERDR_ENV", "OPENTAB_LAUNCHER")
    }
    real_current = ot.util._current_tty
    real_tmux = ot.util._tmux_pane_tty
    real_hook = ot.util.launcher_hook
    try:
        os.environ.pop("OPENTAB_LAUNCHER", None)
        ot.util.launcher_hook = lambda: None
        os.environ["TMUX"] = "tmux"
        os.environ["TMUX_PANE"] = "%1"
        os.environ["HERDR_ENV"] = "1"
        ot.util._current_tty = lambda: "/dev/pts/7"
        ot.util._tmux_pane_tty = lambda: "/dev/pts/7"
        app.handle_key(None, ord("L"))
        assert app.launch_menu_backend == "tmux"
        app.handle_key(None, 27)

        ot.util._tmux_pane_tty = lambda: "/dev/pts/3"
        app.handle_key(None, ord("L"))
        assert app.launch_menu_backend == "herdr"
        assert "popup" not in [kind for _key, kind, _label in app.launch_targets()]
        app.handle_key(None, 27)
    finally:
        ot.util._current_tty = real_current
        ot.util._tmux_pane_tty = real_tmux
        ot.util.launcher_hook = real_hook
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_remote_herdr_launch_heading_names_host_and_backend():
    app = _remote_launch_app({"giant": "root@giant"})
    real_backend = ot.util.launch_backend
    try:
        ot.util.launch_backend = lambda: "herdr"
        app.session_stack = []
        app.drill_into_session("ses_there")
        app.handle_key(None, ord("L"))
        screen = FakeScreen(30, 100)
        real_pair = ot.curses.color_pair
        ot.curses.color_pair = lambda n: 0
        try:
            app.renderer.draw_launch_menu(screen, 30, 100)
        finally:
            ot.curses.color_pair = real_pair
        assert "open on root@giant (ssh) in herdr:" in screen_text(screen)
    finally:
        ot.util.launch_backend = real_backend


def test_launch_reopens_a_pulled_session_on_its_own_machine_over_ssh():
    app = _remote_launch_app({"giant": "root@giant"})
    old_tmux = os.environ.get("TMUX")
    real_backend, real_launch, real_copy = (
        ot.util.launch_backend,
        ot.util.launch_command,
        ot.util.copy_to_clipboard,
    )
    launches, copies = [], []
    try:
        ot.util.launch_backend = lambda: "tmux"
        ot.util.launch_command = lambda kind, d, c, backend=None: (
            launches.append((kind, d, c, backend)) or None
        )
        ot.util.copy_to_clipboard = lambda v: copies.append(v) or True
        os.environ["TMUX"] = "/tmp/tmux-1/default,1,0"
        _launch(app, "ses_there", ord("w"))
        kind, directory, command, _backend = launches[0]
        # -t (the agent CLIs are interactive) and ONE quoted remote argument, so the cd
        # and the resume happen in the same remote shell.
        assert kind == "window"
        assert command == "ssh -t root@giant 'cd /srv/app && claude --resume ses_there'"
        # started from HOME: tmux -c would refuse /srv/app, which is not a path here
        assert directory == os.path.expanduser("~")
        _launch(app, "ses_there", ord("y"))
        assert copies == [command]
        # The picker says where it is about to land, and the yank row says what it yanks.
        app.session_stack = []
        app.drill_into_session("ses_there")
        app.handle_key(None, ord("L"))
        screen = FakeScreen(30, 100)
        real_pair = ot.curses.color_pair
        ot.curses.color_pair = lambda n: 0  # headless: no initscr behind the modal
        try:
            app.renderer.draw_launch_menu(screen, 30, 100)
        finally:
            ot.curses.color_pair = real_pair
        text = screen_text(screen)
        assert "open on root@giant (ssh)" in text and "copy ssh command" in text
        app.handle_key(None, 27)
        # The local box is untouched: same session list, same plain local launch.
        _launch(app, "ses_here", ord("w"))
        assert launches[-1][:3] == ("window", "/repo/a", "claude --resume ses_here")
        _launch(app, "ses_here", ord("y"))
        assert copies[-1] == "cd /repo/a && claude --resume ses_here"
    finally:
        ot.util.launch_backend = real_backend
        ot.util.launch_command, ot.util.copy_to_clipboard = real_launch, real_copy
        if old_tmux is None:
            os.environ.pop("TMUX", None)
        else:
            os.environ["TMUX"] = old_tmux


def test_launch_on_a_machine_with_no_ssh_target_offers_only_the_yank():
    app = _remote_launch_app({})
    old_tmux = os.environ.get("TMUX")
    real_backend, real_launch = ot.util.launch_backend, ot.util.launch_command
    launches = []
    try:
        ot.util.launch_backend = lambda: "tmux"
        ot.util.launch_command = lambda kind, d, c, backend=None: (
            launches.append((kind, d, c, backend)) or None
        )
        os.environ["TMUX"] = "/tmp/tmux-1/default,1,0"
        app.session_stack = []
        app.drill_into_session("ses_there")
        app.handle_key(None, ord("L"))
        assert [kind for _k, kind, _l in app.launch_targets()] == ["copy"]
        assert app.unreachable_machine() == "giant"
        screen = FakeScreen(30, 100)
        real_pair = ot.curses.color_pair
        ot.curses.color_pair = lambda n: 0  # headless: no initscr behind the modal
        try:
            app.renderer.draw_launch_menu(screen, 30, 100)
        finally:
            ot.curses.color_pair = real_pair
        assert "no ssh target" in screen_text(screen)
        app.handle_key(None, ord("w"))  # not offered -> ignored, menu stays open
        assert app.launch_menu is not None and not launches
    finally:
        ot.util.launch_backend = real_backend
        ot.util.launch_command = real_launch
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


def test_live_filter_ranks_best_fuzzy_match_first():
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
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    assert app.can_filter_current_view()
    assert app.handle_key(None, ord("f")) and app.filter_active
    for ch in "bet":
        app.handle_key(None, ord(ch))
    assert app.query == "bet"
    assert [w.title for w in app.current_sessions()] == ["beta"]
    app.handle_key(None, 127)
    assert app.query == "be"
    app.handle_key(None, 10)
    assert not app.filter_active and app.query == "be"
    app.handle_key(None, ord("f"))
    app.handle_key(None, ord("x"))
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
    month = app.months[0]

    # The Models tab (Month/Year/Project scope) is a filterable view; a session has no
    # Models tab -- its model mix lives in the Overview, which stays unfiltered.
    # No query -> both models; a query keeps only the fuzzy matches.
    app.query = ""
    assert any("opus" in ln for ln in r.month_models(month, 120))
    app.query = "opus"
    lines = r.month_models(month, 120)
    assert any("opus" in ln for ln in lines) and not any("gpt-5.3" in ln for ln in lines)

    # A query that matches nothing gives a friendly empty message, not a bare header.
    app.query = "zzz"
    assert any("No models match" in ln for ln in r.month_models(month, 120))

    # A session's Overview Top Models is a different view and is never filtered.
    app.query = "opus"
    assert any("gpt-5.3" in ln for ln in r.detail_overview(wf, 120))


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
        app.handle_key(None, ord("H"))  # opens the picker
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
        app.handle_source_menu_key(ord("H"))  # H walks the list too
        assert app.source_menu_index == 2
        app.handle_source_menu_key(ord("H"))
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
        assert app.notice == "only one harness available"
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


# --- the machine dimension (fleet view: --pull/--remote) ----------------------


def _machine_wf(id, machine, cost=1.0, when="2026-05-01 10:00:00"):
    w = workflow(id, when, cost=cost)
    w.machine = machine
    return w


def test_machines_present_requires_two_distinct_machines():
    assert app_with([workflow("a", "2026-05-01 10:00:00")]).machines_present is False
    one = app_with(
        [_machine_wf("a", "laptop"), _machine_wf("b", "laptop", when="2026-05-02 10:00:00")]
    )
    assert one.machines_present is False  # same single machine
    two = app_with(
        [_machine_wf("a", "laptop"), _machine_wf("b", "server", when="2026-05-02 10:00:00")]
    )
    assert two.machines_present is True


def test_machine_rows_group_by_machine_cost_sorted():
    app = app_with(
        [
            _machine_wf("a", "laptop", cost=2.0),
            _machine_wf("b", "server", cost=9.0, when="2026-05-02 10:00:00"),
            _machine_wf("c", "laptop", cost=1.0, when="2026-05-03 10:00:00"),
        ]
    )
    rows = app.machine_rows(app.loaded)
    assert [m for m, _ in rows] == ["server", "laptop"]  # server $9 outranks laptop $3
    assert dict(rows)["laptop"]["sessions"] == 2


def test_machine_column_shows_only_in_the_fleet_view():
    fleet = app_with(
        [_machine_wf("a", "laptop"), _machine_wf("b", "server", when="2026-05-02 10:00:00")]
    )
    rnd = ot.Renderer(fleet)
    assert "Machine" in rnd.session_header_text(False, 0)
    assert "laptop" in rnd.session_row_text(fleet.loaded[0], ">", False, 0)
    # A single-machine (or plain) view carries no Machine column.
    plain = ot.Renderer(app_with([workflow("a", "2026-05-01 10:00:00")]))
    assert "Machine" not in plain.session_header_text(False, 0)


# --- Machines browse mode (the t/p/m strip) -----------------------------------


def _fleet():
    from tests._support import fleet_app

    return fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00", cost=3.0)],
            "server": [
                workflow("b", "2026-05-02 10:00:00", cost=9.0),
                workflow("c", "2026-05-03 10:00:00", cost=1.0),
            ],
        }
    )


def test_machines_property_floats_the_live_box_first():
    app = _fleet()
    rows = app.machines
    assert rows[0].name == ot.ALL_MACHINES and rows[0].live is False
    assert rows[1].name == "laptop" and rows[1].live is True
    assert rows[2].name == "server" and rows[2].live is False
    assert rows[2].exported_at == "2026-07-18T09:00:00+00:00" and rows[2].opentab_version == "1.6.0"
    assert rows[2].workflows == 2 and rows[2].cost == 10.0


def test_the_fleet_row_sums_every_box_and_stays_off_a_lone_machine():
    app = _fleet()
    fleet = app.machines[0]
    assert fleet.cost == 13.0 and fleet.workflows == 3
    # Bit-identical to the app-wide total: summed over all_workflows in its own order,
    # never re-added off the per-box subtotals (float addition isn't associative, so the
    # reassociated sum can print a cent away from the same figure elsewhere).
    assert fleet.cost == app.range_cost_total()
    assert fleet.exported_at == "" and fleet.opentab_version == ""
    assert {w.id for w in app.machine_scope(fleet)} == {"a", "b", "c"}
    lone = app_with([workflow("a", "2026-05-01 10:00:00")])
    assert [m.name for m in lone.machines] == [lone.local_machine_name]


def test_a_box_actually_LABELLED_all_machines_stays_its_own_box():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "all machines": [workflow("a", "2026-05-01 10:00:00", cost=3.0)],
            "server": [workflow("b", "2026-05-02 10:00:00", cost=9.0)],
        }
    )
    app.set_browse_mode("machines")
    fleet, impostor = app.machines[0], next(m for m in app.machines[1:] if m.name == "all machines")
    assert fleet.fleet is True and impostor.fleet is False
    assert {w.id for w in app.machine_scope(impostor)} == {"a"}  # its own sessions only
    assert {w.id for w in app.machine_scope(fleet)} == {"a", "b"}
    assert app.renderer.machine_badge(impostor) != "∑"
    # The breadcrumb is the ONLY locator in a full-screen session, so it must tell the
    # two apart there too (round-3 Codex finding).
    assert app.renderer.machine_crumb(impostor) == "all machines"
    assert app.renderer.machine_crumb(fleet) == "∑ all machines"
    app.machine_index = app.machines.index(impostor)
    assert app.refresh_target() == "all machines"  # re-pulls that box, not every box
    assert "Status:" in "\n".join(app.renderer.machine_overview(impostor, 100))


def test_the_fleet_row_is_what_the_machines_sidebar_opens_on():
    app = _fleet()
    app.set_browse_mode("machines")
    assert app.selected_machine_summary.name == ot.ALL_MACHINES
    assert {w.id for w in app.current_sessions()} == {"a", "b", "c"}


def test_refresh_on_the_fleet_row_repulls_every_box():
    app = _fleet()
    app.set_browse_mode("machines")
    assert app.refresh_target() is None
    app.machine_index = 2
    assert app.refresh_target() == "server"


def test_machines_mode_scopes_sessions_to_the_selected_box():
    app = _fleet()
    app.set_browse_mode("machines")
    assert app.browse_mode == "machines"
    # The fleet row is selected (index 0); one row down is laptop, with its sessions only.
    app.machine_index = 1  # laptop
    assert [w.id for w in app.current_sessions()] == ["a"]
    app.machine_index = 2  # server
    assert {w.id for w in app.current_sessions()} == {"b", "c"}
    # Its detail tabs: Harnesses injected after Overview (fleet is combined), Sessions drills.
    assert app.current_tabs() == ("Overview", "Harnesses", "Sessions", "Models", "Projects")


def test_machines_mode_harness_tab_drills_into_a_harness_on_the_box():
    from tests._support import fleet_app

    a = workflow("a", "2026-05-01 10:00:00", cost=3.0)
    a.source = "Claude Code"
    b = workflow("b", "2026-05-02 10:00:00", cost=9.0)
    b.source = "Claude Code"
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0)
    c.source = "OpenCode"
    app = fleet_app({"laptop": [a], "server": [b, c]})
    app.set_browse_mode("machines")
    app.machine_index = 2  # server: b (Claude Code) + c (OpenCode)
    app.drill_in()  # into the box
    assert app.view == "zoom"
    app.tab = app.current_tabs().index("Harnesses")
    keys = [s for s, _ in app.zoom_source_rows()]  # the box's two harnesses, ranked
    assert set(keys) == {"Claude Code", "OpenCode"}
    app.source_index = keys.index("Claude Code")
    app.drill_in()  # drill the harness -> Sessions scoped to (server, Claude Code)
    assert app.zoom_source == "Claude Code" and app.on_sessions_tab
    assert {w.id for w in app.current_sessions()} == {"b"}  # server's Claude session only
    app.drill_out()  # Esc pops back to the Harnesses picker, clearing the drill
    assert app.zoom_source is None and app.on_sources_tab


def test_machines_mode_harness_row_double_click_drills():
    from tests._support import fleet_app

    b = workflow("b", "2026-05-02 10:00:00", cost=9.0)
    b.source = "Claude Code"
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0)
    c.source = "OpenCode"
    app = fleet_app({"laptop": [workflow("a", "2026-05-01 10:00:00")], "server": [b, c]})
    app.set_browse_mode("machines")
    app.machine_index = 2  # server
    app.drill_in()
    app.tab = app.current_tabs().index("Harnesses")
    idx = [s for s, _ in app.zoom_source_rows()].index("OpenCode")
    app._apply_click(("zoomsource", idx), drill=True)
    assert app.zoom_source == "OpenCode" and app.on_sessions_tab
    assert {w.id for w in app.current_sessions()} == {"c"}


def test_machines_mode_switching_box_clears_an_armed_harness():
    from tests._support import fleet_app

    b = workflow("b", "2026-05-02 10:00:00", cost=9.0)
    b.source = "Claude Code"
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0)
    c.source = "OpenCode"
    app = fleet_app({"laptop": [workflow("a", "2026-05-01 10:00:00")], "server": [b, c]})
    app.set_browse_mode("machines")
    app.machine_index = 2  # server
    app.drill_in()
    app.tab = app.current_tabs().index("Harnesses")
    app.source_index = [s for s, _ in app.zoom_source_rows()].index("Claude Code")
    app.drill_in()  # harness armed on server
    assert app.zoom_source == "Claude Code"
    app._apply_click(("machine", 1), drill=False)  # click laptop in the sidebar
    assert app.zoom_source is None  # the old box's harness drill is dropped
    assert {w.id for w in app.current_sessions()} == {"a"}  # laptop's sessions, unfiltered


def test_machines_mode_wheel_over_the_sidebar_also_clears_an_armed_harness():
    from tests._support import fleet_app

    b = workflow("b", "2026-05-02 10:00:00", cost=9.0)
    b.source = "Claude Code"
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0)
    c.source = "OpenCode"
    app = fleet_app({"laptop": [workflow("a", "2026-05-01 10:00:00")], "server": [b, c]})
    app.set_browse_mode("machines")
    app.machine_index = 2  # server
    app.drill_in()
    app.tab = app.current_tabs().index("Harnesses")
    app.source_index = [s for s, _ in app.zoom_source_rows()].index("Claude Code")
    app.drill_in()
    assert app.zoom_source == "Claude Code"
    app.renderer.hit = lambda _y, _x: ("machine", 0)  # cursor over the machine sidebar
    app._wheel(0, 0, -1)  # wheel up to laptop
    assert app.machine_index == 1 and app.zoom_source is None
    assert {w.id for w in app.current_sessions()} == {"a"}


def test_machines_mode_projects_tab_drills_into_a_project_on_the_box():
    from tests._support import fleet_app

    b = workflow("b", "2026-05-02 10:00:00", cost=9.0, directory="/work/alpha")
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0, directory="/work/beta")
    app = fleet_app({"laptop": [workflow("a", "2026-05-01 10:00:00")], "server": [b, c]})
    app.set_browse_mode("machines")
    app.machine_index = 2  # server: b (/work/alpha), c (/work/beta)
    app.drill_in()
    app.tab = app.current_tabs().index("Projects")
    projects = app.zoom_projects()  # the box's two projects, cost-sorted (alpha $9 first)
    assert len(projects) == 2 and projects[0].directory == app.project_root("/work/alpha")
    app.project_index = 0
    app.drill_in()
    assert app.zoom_project == app.project_root("/work/alpha") and app.on_sessions_tab
    assert {w.id for w in app.current_sessions()} == {"b"}
    app.drill_out()
    assert app.zoom_project is None and app.on_projects_tab


def test_machines_mode_models_tab_drills_into_sessions_using_a_model():
    from tests._support import fleet_app

    b = workflow("b", "2026-05-02 10:00:00", cost=9.0)
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0)
    app = fleet_app({"laptop": [workflow("a", "2026-05-01 10:00:00")], "server": [b, c]})
    app._model_by_root = {  # seed the per-model breakdown the table/drill read
        "b": [_model_row("opus", 9.0, 900)],
        "c": [_model_row("opus", 0.6, 60), _model_row("haiku", 0.4, 40)],
    }
    app.set_browse_mode("machines")
    app.machine_index = 2  # server: b (opus), c (opus + haiku)
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    keys = [m for m, _ in app.zoom_model_rows()]
    assert set(keys) == {"opus", "haiku"}
    app.model_pick_index = keys.index("opus")
    app.drill_in()  # opus was used by both sessions
    assert app.zoom_model == "opus" and app.current_tabs() == ("Economics", "Sessions")
    assert app.current_tabs()[app.tab] == "Economics"
    assert {w.id for w in app.current_sessions()} == {"b", "c"}
    app.drill_out()
    assert app.zoom_model is None and app.on_models_tab
    keys = [m for m, _ in app.zoom_model_rows()]
    app.model_pick_index = keys.index("haiku")
    app.drill_in()  # haiku only by c
    assert {w.id for w in app.current_sessions()} == {"c"}


def _month_app_with_models():
    # Two sessions in one month: b ran opus, c ran opus + haiku.
    b = workflow("b", "2026-05-02 10:00:00", cost=9.0, directory="/work/alpha")
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0, directory="/work/beta")
    app = app_with([b, c])
    app._model_by_root = {
        "b": [_model_row("opus", 9.0, 900)],
        "c": [_model_row("opus", 0.6, 60), _model_row("haiku", 0.4, 40)],
    }
    return app


def test_month_models_tab_drills_into_sessions_using_a_model():
    app = _month_app_with_models()
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    keys = [m for m, _ in app.zoom_model_rows()]
    assert keys == ["opus", "haiku"]  # cost-ranked, like the table
    app.model_pick_index = keys.index("haiku")
    app.drill_in()
    assert app.zoom_model == "haiku" and app.current_tabs()[app.tab] == "Economics"
    assert {w.id for w in app.current_sessions()} == {"c"}
    app.tab = app.current_tabs().index("Sessions")
    app.drill_in()
    assert app.view == "session" and app.current_session().id == "c"
    app.drill_out()
    assert app.view == "zoom" and app.on_sessions_tab and app.zoom_model == "haiku"
    app.drill_out()
    assert app.zoom_model is None and app.on_models_tab
    assert {w.id for w in app.current_sessions()} == {"b", "c"}


def test_the_renamed_metric_columns_keep_their_click_to_sort_zones():
    # The header labels are the hit-test keys: a model scope renames Cost/Tokens, and a
    # hard-coded label would leave the two columns it exists to rank by unclickable.
    app = _month_app_with_models()
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.model_pick_index = [m for m, _ in app.zoom_model_rows()].index("opus")
    app.drill_in()
    app.tab = app.current_tabs().index("Sessions")
    rnd = app.renderer

    _models, proj_w, dur = rnd.session_columns(app.current_sessions(), 96)
    header = rnd.session_header_text(_models, proj_w, dur)
    rnd.sort_regions = []
    rnd._register_sort_header(2, 1, header, rnd.session_sort_columns(proj_w, dur), "session", 96)

    assert "Model list" in header and "Model tok" in header
    assert rnd.sort_hit(2, 1 + header.index("Model list")) == ("cost", "session")
    assert rnd.sort_hit(2, 1 + header.index("Model tok")) == ("tokens", "session")
    assert rnd.sort_hit(2, 1 + header.index("Subagents")) == ("subagents", "session")


def test_a_models_name_filter_does_not_become_a_session_filter_after_drilling():
    app = _month_app_with_models()
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.query = "hai"

    assert [m for m, _ in app.zoom_model_rows()] == ["haiku"]
    app.drill_in()

    assert app.zoom_model == "haiku" and app.query == ""
    assert [w.id for w in app.current_sessions()] == ["c"]


def test_a_model_drill_layers_on_another_drill_and_pops_first():
    app = _month_app_with_models()
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Projects")
    app.project_index = [p.directory for p in app.zoom_projects()].index(
        app.project_root("/work/beta")
    )
    app.drill_in()
    assert app.zoom_project == app.project_root("/work/beta")
    app.tab = app.current_tabs().index("Models")
    app.model_pick_index = [m for m, _ in app.zoom_model_rows()].index("opus")
    app.drill_in()
    assert app.zoom_model == "opus" and app.zoom_project  # composed, not replaced
    assert {w.id for w in app.current_sessions()} == {"c"}  # beta AND opus
    app.drill_out()  # the model pops first...
    assert app.zoom_model is None and app.zoom_project and app.on_models_tab
    app.drill_out()  # ...then the project
    assert app.zoom_project is None and app.on_projects_tab


def test_re_scoping_from_the_sidebar_disarms_the_model_drill():
    may = workflow("m", "2026-05-02 10:00:00", cost=9.0)
    jun = workflow("j", "2026-06-02 10:00:00", cost=5.0)
    app = app_with([may, jun])
    app._model_by_root = {
        "m": [_model_row("haiku", 9.0, 900)],
        "j": [_model_row("opus", 5.0, 500)],  # June never ran haiku
    }
    app.focus = "months"
    app.month_index = [s.month for s in app.months].index("2026-05")
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.drill_in()
    assert app.zoom_model == "haiku" and [w.id for w in app.current_sessions()] == ["m"]
    app._apply_click(("month", [s.month for s in app.months].index("2026-06")), drill=False)
    assert app.zoom_model is None and app.model_pick_index == 0
    assert [w.id for w in app.current_sessions()] == ["j"]  # not hidden by May's model


def test_wheeling_the_sidebar_onto_a_new_scope_disarms_the_model_drill():
    may = workflow("m", "2026-05-02 10:00:00", cost=9.0)
    jun = workflow("j", "2026-06-02 10:00:00", cost=5.0)
    app = app_with([may, jun])
    app._model_by_root = {
        "m": [_model_row("haiku", 9.0, 900)],
        "j": [_model_row("opus", 5.0, 500)],
    }
    app.focus = "months"
    months = [s.month for s in app.months]
    app.month_index = months.index("2026-05")
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.drill_in()
    assert app.zoom_model == "haiku"
    app.renderer.hit = lambda my, mx: ("month", 0)  # put the wheel over the Months list
    toward_end = 1 if months.index("2026-05") == len(months) - 1 else -1
    app._wheel(0, 0, toward_end * 5)  # already at that end: the row cannot change
    assert app.zoom_model == "haiku"
    app._wheel(0, 0, -toward_end)  # a real move, onto June
    assert app.zoom_model is None and app.model_pick_index == 0
    assert [w.id for w in app.current_sessions()] == ["j"]


def test_a_model_drill_disarms_itself_when_its_data_moves_away():
    may = workflow("m", "2026-05-02 10:00:00", cost=9.0, directory="/work/alpha")
    jun = workflow("j", "2026-06-02 10:00:00", cost=5.0, directory="/work/beta")

    def armed():
        a = app_with([may, jun])
        a._model_by_root = {
            "m": [_model_row("haiku", 9.0, 900)],
            "j": [_model_row("opus", 5.0, 500)],  # June never ran haiku
        }
        a.focus = "months"
        a.month_index = [s.month for s in a.months].index("2026-05")
        a.drill_in()
        a.tab = a.current_tabs().index("Models")
        a.drill_in()
        assert a.zoom_model == "haiku"
        return a

    app = armed()
    app.set_range_from_text("2026-06")  # a range that excludes every haiku session
    assert [w.id for w in app.current_sessions()] == ["j"]
    assert app.zoom_model is None and app.model_pick_index == 0

    app = armed()
    app.tab = app.current_tabs().index("Sessions")
    app.toggle_ignore()  # ignoring the project drops the drill's sessions from view
    assert [w.id for w in app.current_sessions()] == ["j"]
    assert app.zoom_model is None


def _drill_app():
    # One month/harness/project per session, so any drill armed in May excludes June.
    may = workflow("m", "2026-05-02 10:00:00", cost=9.0, directory="/work/alpha")
    jun = workflow("j", "2026-06-02 10:00:00", cost=5.0, directory="/work/beta")
    may.source, jun.source = "OpenCode", "Claude Code"
    app = app_with([may, jun])
    app.store.combined = True  # the merged view is what grows a Harnesses tab
    app._model_by_root = {
        "m": [_model_row("haiku", 9.0, 900)],
        "j": [_model_row("opus", 5.0, 500)],
    }
    app.focus = "months"
    app.month_index = [s.month for s in app.months].index("2026-05")
    return app


def test_a_range_change_disarms_every_drill_not_just_the_project_one():
    for tab, attr, armed in (
        ("Harnesses", "zoom_source", "OpenCode"),
        ("Models", "zoom_model", "haiku"),
    ):
        app = _drill_app()
        app.drill_in()
        app.tab = app.current_tabs().index(tab)
        app.drill_in()
        assert getattr(app, attr) == armed
        app.set_range_from_text("2026-06")
        assert getattr(app, attr) is None
        assert [w.id for w in app.current_sessions()] == ["j"]


def test_the_models_tab_only_offers_models_the_armed_drill_can_open():
    alpha1 = workflow("a", "2026-05-02 10:00:00", cost=9.0, directory="/work/alpha")
    alpha2 = workflow("a2", "2026-05-02 11:00:00", cost=3.0, directory="/work/alpha")
    beta = workflow("b", "2026-05-03 10:00:00", cost=5.0, directory="/work/beta")
    app = app_with([alpha1, alpha2, beta])
    app._model_by_root = {
        "a": [_model_row("opus", 9.0, 900)],
        "a2": [_model_row("sonnet", 3.0, 300)],
        "b": [_model_row("haiku", 5.0, 500)],  # beta's model, alpha never ran it
    }
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Projects")
    alpha = app.project_root("/work/alpha")
    app.project_index = [p.directory for p in app.zoom_projects()].index(alpha)
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    assert [m for m, _ in app.zoom_model_rows()] == ["opus", "sonnet"]  # no haiku
    # ...and the DRAWN table agrees, so the cursor indexes what is on screen
    lines = app.renderer.month_models(app.selected_month_summary, 116)
    drawn = [
        lines[i][2:-2].lstrip("> ").split("  ")[0].strip()
        for i, _ in sorted(app.renderer._model_row_at.items(), key=lambda kv: kv[1])
    ]
    assert drawn == ["opus", "sonnet"]
    app.model_pick_index = 1
    app.drill_in()
    assert app.zoom_model == "sonnet"
    assert [w.id for w in app.current_sessions()] == ["a2"]  # the pick actually took
    app.drill_out()
    assert app.zoom_model is None and app.zoom_project == alpha  # model popped, not project


def test_wheeling_a_panel_below_the_focused_one_keeps_the_drills():
    ws = []
    for i in range(4):
        w = workflow(f"w{i}", f"2026-0{5 + i // 2}-0{i % 2 + 1} 10:00:00", cost=float(9 - i))
        w.source = "OpenCode" if i % 2 == 0 else "Claude Code"
        ws.append(w)

    def armed(focus):
        app = app_with(ws)
        app.store.combined = True
        app._model_by_root = {w.id: [_model_row("opus", w.total_cost, 100)] for w in ws}
        app.focus = focus
        app.drill_in()
        app.tab = app.current_tabs().index("Harnesses")
        app.source_index = 0
        app.drill_in()
        return app

    for focus, panel in (("years", "month"), ("years", "day"), ("months", "day")):
        app = armed(focus)
        app.renderer.hit = lambda my, mx, p=panel: (p, 0)
        app._wheel(0, 0, 1)
        assert app.zoom_source is not None, f"{panel} under {focus} must not disarm"
    # ...but the focused panel itself still re-scopes and still disarms
    app = armed("months")
    app.renderer.hit = lambda my, mx: ("month", 0)
    app._wheel(0, 0, 1)
    assert app.zoom_source is None


def test_a_frame_never_paints_a_crumb_for_a_drill_its_own_list_dropped():
    may = workflow("m", "2026-05-02 10:00:00", cost=9.0)
    jun = workflow("j", "2026-06-02 10:00:00", cost=5.0)
    app = app_with([may, jun])
    app._model_by_root = {
        "m": [_model_row("haiku", 9.0, 900)],
        "j": [_model_row("opus", 5.0, 500)],
    }
    app.focus = "months"
    app.month_index = [s.month for s in app.months].index("2026-05")
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.drill_in()
    assert app.zoom_model == "haiku"
    app.month_index = [s.month for s in app.months].index("2026-06")  # re-scope behind it
    assert "haiku" in app.renderer.breadcrumb()  # stale until something settles it
    app.settle_drills()
    assert app.zoom_model is None and "haiku" not in app.renderer.breadcrumb()


def test_a_range_change_keeps_the_selected_session_while_disarming_drills():
    ws = []
    for i, src in enumerate(["Claude Code", "OpenCode", "OpenCode"]):
        w = workflow(f"w{i}", f"2026-05-0{i + 1} 10:00:00", cost=float(9 - i))
        w.source = src
        ws.append(w)

    def drilled():
        app = app_with(ws)
        app.store.combined = True
        app._model_by_root = {w.id: [_model_row("opus", w.total_cost, 100)] for w in ws}
        app.focus = "months"
        app.drill_in()
        app.tab = app.current_tabs().index("Harnesses")
        app.source_index = [s for s, _ in app.zoom_source_rows()].index("OpenCode")
        app.drill_in()
        app.workflow_index = 1  # the SECOND session of the drilled pair
        return app

    app = drilled()
    assert app.current_session().id == "w2"
    app.set_range_from_text("2026-05")
    assert app.current_session().id == "w2" and app.zoom_source is None

    app = drilled()
    app.set_all_time()
    assert app.current_session().id == "w2"


def test_clearing_the_project_drill_never_moves_the_projects_mode_sidebar():
    ws = [
        workflow("a", "2026-05-02 10:00:00", cost=1.0, directory="/work/alpha"),
        workflow("b", "2026-05-03 10:00:00", cost=9.0, directory="/work/beta"),
        workflow("c", "2026-05-04 10:00:00", cost=5.0, directory="/work/gamma"),
    ]
    app = app_with(ws)
    app._model_by_root = {w.id: [_model_row("opus", w.total_cost, 100)] for w in ws}
    app.set_browse_mode("projects")
    app.project_index = 2
    here = app.projects[2].directory
    app.set_range_from_text("2026-05")
    assert app.projects[app.project_index].directory == here
    app.set_all_time()
    assert app.projects[app.project_index].directory == here
    # ...and outside projects mode, where it IS the picker cursor, a range change disarms
    # the drill but restore_selection re-finds the row BY VALUE -- the codebase's rule
    # everywhere else (never a wrong-but-valid neighbour by index).
    app = app_with(ws)
    app._model_by_root = {w.id: [_model_row("opus", w.total_cost, 100)] for w in ws}
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Projects")
    gamma = app.project_root("/work/gamma")
    app.project_index = [p.directory for p in app.zoom_projects()].index(gamma)
    app.drill_in()
    assert app.zoom_project == gamma
    app.set_range_from_text("2026-05")
    assert app.zoom_project is None
    assert app.zoom_projects()[app.project_index].directory == gamma


def test_wheeling_the_sidebar_disarms_every_drill_not_just_the_model_one():
    app = _drill_app()
    app.drill_in()
    app.tab = app.current_tabs().index("Harnesses")
    app.drill_in()
    assert app.zoom_source == "OpenCode"
    months = [s.month for s in app.months]
    app.renderer.hit = lambda my, mx: ("month", 0)
    app._wheel(0, 0, -1 if months.index("2026-06") < months.index("2026-05") else 1)
    assert app.zoom_source is None
    assert [w.id for w in app.current_sessions()] == ["j"]


def test_editing_the_filter_snaps_every_zoom_cursor_back():
    app = _drill_app()
    app.drill_in()
    app.tab = app.current_tabs().index("Harnesses")
    app.source_index, app.machine_pick_index = 1, 3
    for ch in "alpha":
        app.query += ch
        app._filter_edited()
    assert app.source_index == 0 and app.machine_pick_index == 0


def test_esc_returns_the_cursor_to_the_row_it_drilled_even_after_a_rerank():
    app = _drill_app()
    app._model_by_root = {"m": [_model_row("cheap", 1.0, 100), _model_row("pricey", 9.0, 900)]}
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.model_pick_index = [m for m, _ in app.zoom_model_rows()].index("cheap")
    app.drill_in()
    assert app.zoom_model == "cheap"
    # the ranking flips under the armed drill (what a `$` toggle does)
    app._model_by_root = {"m": [_model_row("cheap", 99.0, 100), _model_row("pricey", 9.0, 900)]}
    app.drill_out()
    assert [m for m, _ in app.zoom_model_rows()][app.model_pick_index] == "cheap"


def test_a_model_drill_survives_a_scope_emptied_by_something_else():
    may = workflow("m", "2026-05-02 10:00:00", cost=9.0)
    app = app_with([may])
    app._model_by_root = {"m": [_model_row("haiku", 9.0, 900)]}
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.drill_in()
    app.show_bookmarks_only = True
    app._invalidate_workflow_cache()
    assert app.current_sessions() == [] and app.zoom_model == "haiku"  # kept, not blamed
    app.show_bookmarks_only = False
    app._invalidate_workflow_cache()
    assert [w.id for w in app.current_sessions()] == ["m"] and app.zoom_model == "haiku"


def test_the_models_cursor_moves_on_the_first_press_after_the_list_reorders():
    app = app_with([workflow("a", "2026-05-02 10:00:00", cost=9.0)])
    app._model_by_root = {"a": [_model_row(f"m{i}", float(9 - i), 100) for i in range(6)]}
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.model_pick_index = 5
    app.query = "m0"  # the list is now one row; the cursor is stale at 5
    app.move(-1)
    assert app.model_pick_index == 0


def test_changing_focus_snaps_the_models_cursor_back():
    app = app_with(
        [
            workflow("a", "2026-05-02 10:00:00", cost=9.0),
            workflow("b", "2025-05-02 10:00:00", cost=9.0),
        ]
    )
    app._model_by_root = {
        "a": [_model_row("m0", 9.0, 100), _model_row("m1", 8.0, 100)],
        "b": [_model_row(f"x{i}", float(7 - i), 100) for i in range(5)],
    }
    app.focus = "years"
    app.year_index = 0
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.model_pick_index = 4  # valid for the year's 7 models
    app.set_focus("months")
    assert app.model_pick_index == 0 and len(app.zoom_model_rows()) == 2


def test_editing_the_filter_snaps_the_models_cursor_back():
    app = app_with([workflow("a", "2026-05-02 10:00:00", cost=9.0)])
    app._model_by_root = {
        "a": [
            _model_row("opus", 9.0, 900),
            _model_row("sonnet", 7.0, 700),
            _model_row("haiku-fast", 5.0, 500),
            _model_row("haiku", 3.0, 300),
        ]
    }
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.model_pick_index = 3
    for ch in "haiku":
        app.query += ch
        app._filter_edited()
    assert [m for m, _ in app.zoom_model_rows()] == ["haiku-fast", "haiku"]
    assert app.model_pick_index == 0  # snapped, not left at 3
    app.renderer.month_models(app.selected_month_summary, 116)
    line = app.renderer._model_cursor_line
    assert app.renderer._model_row_at[line] == 0 and app.zoom_selected_model() == "haiku-fast"


def test_the_breadcrumb_names_an_armed_model_drill():
    app = _month_app_with_models()
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.model_pick_index = [m for m, _ in app.zoom_model_rows()].index("opus")
    app.drill_in()
    crumb = app.renderer.breadcrumb()
    assert crumb == "all time › 2026-05 › opus › Economics"
    app.drill_out()
    assert "opus" not in app.renderer.breadcrumb()


def test_a_models_tab_click_maps_a_line_to_its_row():
    app = _month_app_with_models()
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Models")
    app.renderer.month_models(app.selected_month_summary, 116)
    rows = app.renderer._model_row_at
    assert sorted(rows.values()) == [0, 1]  # exactly the two model rows
    haiku_line = [ln for ln, ordinal in rows.items() if ordinal == 1][0]
    app._apply_click(("zoommodel", haiku_line), drill=True)
    assert app.zoom_model == "haiku"
    app.drill_out()
    app._apply_click(("zoommodel", 0), drill=True)  # the box's top border
    assert app.zoom_model is None and app.model_pick_index == 1  # cursor unmoved


def test_machines_mode_drills_are_mutually_exclusive():
    from tests._support import fleet_app

    b = workflow("b", "2026-05-02 10:00:00", cost=9.0, directory="/work/alpha")
    b.source = "Claude Code"
    app = fleet_app({"laptop": [workflow("a", "2026-05-01 10:00:00")], "server": [b]})
    app.set_browse_mode("machines")
    app.machine_index = 2  # server
    app.drill_in()
    app.tab = app.current_tabs().index("Harnesses")
    app.source_index = 0
    app.drill_in()
    assert app.zoom_source == "Claude Code"
    app.tab = app.current_tabs().index("Projects")
    app.project_index = 0
    app.drill_in()  # arming the project clears the harness
    assert app.zoom_project == app.project_root("/work/alpha") and app.zoom_source is None


def test_machines_mode_wheeling_in_place_keeps_an_armed_drill():
    from tests._support import fleet_app

    a = workflow("a", "2026-05-01 10:00:00", cost=9.0)
    a.source = "Claude Code"
    app = fleet_app({"laptop": [a], "server": [workflow("b", "2026-05-02 10:00:00")]})
    app.set_browse_mode("machines")
    app.machine_index = 0
    app.drill_in()
    app.tab = app.current_tabs().index("Harnesses")
    app.source_index = 0
    app.drill_in()
    assert app.zoom_source == "Claude Code"
    app.renderer.hit = lambda _y, _x: ("machine", 0)  # cursor over the sidebar
    app._wheel(0, 0, -1)  # wheel up at the top edge -- machine_index stays 0
    assert app.machine_index == 0 and app.zoom_source == "Claude Code"  # drill preserved


def test_machines_mode_switching_to_a_smaller_box_resets_the_picker_cursor():
    from tests._support import fleet_app

    big = [workflow(f"s{i}", f"2026-05-0{i + 1} 10:00:00", cost=float(9 - i)) for i in range(4)]
    for i, w in enumerate(big):
        w.source = f"Src{i}"
    app = fleet_app({"laptop": [workflow("a", "2026-05-01 10:00:00")], "server": big})
    app.set_browse_mode("machines")
    app.machine_index = 2  # the big box
    app.drill_in()
    app.tab = app.current_tabs().index("Harnesses")
    app.source_index = 3  # cursor deep in the big box's picker
    app._apply_click(("machine", 1), drill=False)  # click over to the one-session laptop
    assert app.source_index == 0  # cursor re-anchored for the smaller box's picker


def test_machines_mode_refresh_drops_a_project_drill_like_source_and_model():
    from tests._support import fleet_app

    b = workflow("b", "2026-05-02 10:00:00", cost=9.0, directory="/work/alpha")
    app = fleet_app({"laptop": [workflow("a", "2026-05-01 10:00:00")], "server": [b]})
    app.set_browse_mode("machines")
    app.machine_index = 2  # server
    app.drill_in()
    app.tab = app.current_tabs().index("Projects")
    app.project_index = 0
    app.drill_in()  # project armed on server
    assert app.zoom_project == app.project_root("/work/alpha")
    app._reload_for_source(app.ui_snapshot())  # the refresh path (_do_refresh)
    assert app.zoom_project is None  # per-box drill dropped, not restored


def test_mode_tab_list_always_offers_machines():
    modes = ["time", "projects", "machines"]
    assert [
        m for _l, m in app_with([workflow("a", "2026-05-01 10:00:00")]).mode_tab_list()
    ] == modes
    assert [m for _l, m in _fleet().mode_tab_list()] == modes


def test_mode_tab_click_switches_browse_mode():
    app = _fleet()
    # ("modetab", index) -> the mode at that index in mode_tab_list.
    app._apply_click(("modetab", 2), drill=False)  # Machines
    assert app.browse_mode == "machines"
    app._apply_click(("modetab", 0), drill=False)  # Time
    assert app.browse_mode == "time"


def test_machine_row_click_selects_and_double_click_drills():
    app = _fleet()
    app.set_browse_mode("machines")
    app._apply_click(("machine", 1), drill=False)  # click server
    assert app.machine_index == 1 and app.view == "browse"
    app._apply_click(("machine", 1), drill=True)  # double-click drills
    assert app.view == "zoom"


def test_m_key_off_a_fleet_shows_this_one_machine():
    plain = app_with([workflow("a", "2026-05-01 10:00:00")])
    assert plain.handle_key(None, ord("m")) is True
    assert plain.browse_mode == "machines"
    rows = plain.machines
    assert len(rows) == 1
    assert rows[0].name == plain.local_machine_name == ot.util.local_machine_name()
    assert rows[0].live is True and rows[0].workflows == 1
    # ...and the fleet-only extras stay gated: nothing to filter or compare against.
    assert plain.machines_present is False
    assert plain.renderer.mach_col() == ""  # no Machine column
    plain.open_machine_menu()
    assert plain.machine_menu is False and "fleet" in plain.notice.lower()


def test_this_machines_label_is_scrambled_under_demo():
    app = app_with([workflow("a", "2026-05-01 10:00:00")])
    real = app.local_machine_name
    app.store.demo = True
    app.store.demo_cats = ot.demo.DEMO_ALL
    fake = app.local_machine_name
    assert fake != real and fake == ot.demo.demo_machine(real)
    assert [m.name for m in app.machines] == [fake]
    assert app.workflows_for_machine(fake) and not app.workflows_for_machine(real)
    # `titles` off (a spend-only demo) keeps names real, like every other label.
    app.store.demo_cats = frozenset({"spend"})
    assert app.local_machine_name == real


def test_switch_browse_mode_steps_out_of_a_session():
    app = _fleet()
    app.set_browse_mode("machines")
    app.drill_in()  # into the box
    tabs = app.current_tabs()
    app.tab = tabs.index("Sessions")
    app.drill_in()  # into a session
    assert app.view == "session"
    app.switch_browse_mode("time")  # a mode-tab click from a session steps out first
    assert app.view == "browse" and app.browse_mode == "time"


def test_mode_keys_switch_browse_mode_from_within_a_session():
    app = _fleet()
    app.set_browse_mode("machines")
    app.machine_index = 1
    app.drill_in()  # into the box
    app.tab = app.current_tabs().index("Sessions")
    app.drill_in()  # into a session
    assert app.view == "session"
    assert app.handle_key(None, ord("t")) is True  # keyboard, from inside the session
    assert app.browse_mode == "time"


def test_returning_to_a_browse_mode_restores_the_session_and_tab():
    app = _fleet()
    app.set_browse_mode("machines")
    app.machine_index = 2  # server
    app.drill_in()  # into the box
    app.tab = app.current_tabs().index("Sessions")
    app.drill_in()  # into a session
    assert app.view == "session"
    sid = app.current_session().id
    app.tab = len(app.current_tabs()) - 1  # a non-first detail tab
    tab_name = app.current_tabs()[app.tab]
    app.set_browse_mode("time")  # wander off to time...
    assert app.view == "browse" and app.browse_mode == "time"
    app.set_browse_mode("machines")  # ...and come back
    assert app.browse_mode == "machines" and app.view == "session"
    assert app.current_session().id == sid  # same session
    assert app.current_tabs()[app.tab] == tab_name  # same tab


def test_returning_after_a_range_change_dropped_the_session_demotes_to_zoom():
    from tests._support import fleet_app

    old = workflow("old", "2020-01-01 10:00:00", cost=5.0)
    new = workflow("new", "2026-05-02 10:00:00", cost=9.0)
    app = fleet_app({"laptop": [old, new], "server": [workflow("s", "2026-05-03 10:00:00")]})
    app.set_browse_mode("machines")
    app.machine_index = 0  # laptop: old (2020) + new (2026)
    app.drill_in()
    app.tab = app.current_tabs().index("Sessions")
    app.workflow_index = [w.id for w in app.current_sessions()].index("old")
    app.drill_in()  # into the 2020 session
    assert app.view == "session" and app.current_session().id == "old"
    app.set_browse_mode("time")  # remember the machines spot (session "old")
    app.set_range_from_text("2026-01-01..")  # a range change drops 2020 -> "old" is gone
    app.set_browse_mode("machines")  # come back
    assert app.view == "zoom"  # demoted, NOT silently opening the surviving "new"


def test_returning_after_a_sort_reorder_reopens_the_same_session():
    from tests._support import fleet_app

    b = workflow("b", "2026-05-02 10:00:00", cost=9.0)
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0)
    app = fleet_app({"laptop": [workflow("a", "2026-05-01 10:00:00")], "server": [b, c]})
    app.set_browse_mode("machines")
    app.machine_index = 2  # server: cost-sorted [b ($9), c ($1)]
    app.drill_in()
    app.tab = app.current_tabs().index("Sessions")
    assert [w.id for w in app.current_sessions()] == ["b", "c"]
    app.workflow_index = 1  # the SECOND row, c
    app.drill_in()
    assert app.current_session().id == "c"
    app.set_browse_mode("time")  # remember (session c, then at index 1)
    app.sort_reverse = True  # flip the order -> server is now [c, b], c at index 0
    app.set_browse_mode("machines")  # come back
    assert app.view == "session" and app.current_session().id == "c"  # c, not the row-1 b


def test_maximize_stays_global_across_a_mode_switch():
    app = _fleet()
    app.set_browse_mode("machines")
    app.drill_in()  # zoom on the box
    app.zoom_maximized = True  # maximized while in machines
    app.set_browse_mode("time")
    app.zoom_maximized = False  # ...then turned off while in time
    app.set_browse_mode("machines")  # return
    assert app.zoom_maximized is False  # stayed off, not restored to the stale True


def test_trends_date_drill_remembers_the_mode_it_left():
    app = _fleet()
    app.set_browse_mode("machines")
    app.machine_index = 2  # server
    app.drill_in()
    app.tab = app.current_tabs().index("Sessions")
    app.drill_in()  # into a session
    assert app.view == "session"
    sid = app.current_session().id
    date = app.current_session().created_at[:10]
    assert app.drill_into_date(date) is True  # Trends date drill -> time browse
    assert app.browse_mode == "time"
    app.set_browse_mode("machines")  # return to machines
    assert app.view == "session" and app.current_session().id == sid


def test_export_dataset_in_machines_mode():
    app = _fleet()
    app.set_browse_mode("machines")
    kind, header, rows = app._export_dataset()
    assert kind == "machines"
    assert header[0] == "machine" and "live" in header
    # The fleet row is exported too: `e` saves exactly the table on screen (the years
    # export ships its "all" row for the same reason) -- and a `fleet` column marks it,
    # since the NAME can't: a real box can be labelled "all machines".
    assert {r[0] for r in rows} == {ot.ALL_MACHINES, "laptop", "server"}
    fleet_col = header.index("fleet")
    assert [r[0] for r in rows if r[fleet_col]] == [ot.ALL_MACHINES]


def test_machines_mode_query_still_shows_the_selected_box_sessions():
    app = _fleet()
    app.set_browse_mode("machines")
    app.machine_index = 2  # server (its sessions are "b", "c")
    assert {w.id for w in app.current_sessions()} == {"b", "c"}
    app.query = "server"  # the hostname: matches no session title/path -> the OLD bug emptied it
    assert len(app.machines) == 3  # the machine LIST stays full (not filtered by the query)
    # The box's sessions filter by CONTENT, so "server" (absent from titles) narrows to none --
    # but selecting the box by clicking it still works; a title word would keep its sessions.
    app.query = "b"
    assert {w.id for w in app.current_sessions()} == {"b"}


def test_refresh_reanchors_the_selected_machine_by_name():
    app = _fleet()
    app.set_browse_mode("machines")
    app.machine_index = 2  # server
    anchor = app.selection_anchor()
    assert anchor[4] == "server"  # the machine name rides in the anchor
    # Simulate a rebuild that reordered machines: force index off, then restore by name.
    app.machine_index = 0
    app.restore_selection(anchor)
    assert app.selected_machine_summary.name == "server"


def test_the_anchor_tells_the_fleet_row_apart_from_a_box_of_the_same_name():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "all machines": [workflow("a", "2026-05-01 10:00:00", cost=3.0)],
            "server": [workflow("b", "2026-05-02 10:00:00", cost=9.0)],
        }
    )
    app.set_browse_mode("machines")
    impostor = next(
        i for i, m in enumerate(app.machines) if m.name == "all machines" and not m.fleet
    )
    app.machine_index = impostor
    anchor = app.selection_anchor()
    assert anchor[4] == "all machines"
    app.machine_index = 0
    app.restore_selection(anchor)
    assert app.machine_index == impostor  # the box, not the fleet row above it
    # ...and the fleet row round-trips too, on its flag rather than its name.
    app.machine_index = 0
    app.restore_selection(app.selection_anchor())
    assert app.selected_machine_summary.fleet is True


def test_request_machine_refresh_paths():
    app = _fleet()
    app.set_browse_mode("machines")
    # No backend injected -> a friendly error, no request queued.
    app.request_machine_refresh("server")
    assert app._refresh_request is None and "refresh needs" in app.notice.lower()
    # The live box (laptop) refreshes by a plain reload, never a re-pull.
    calls = []
    app._refresh_backend = lambda keys: calls.append(keys) or [(k, 5, "") for k in keys]
    app.request_machine_refresh("laptop")
    assert app._refresh_request is None and calls == []  # reload path, no backend call
    # A pulled box queues its remotes key for the run loop.
    app.request_machine_refresh("server")
    assert app._refresh_request == ["server"]  # the meta key, ready for _do_refresh


def test_per_scope_machines_tab_is_a_picker_that_narrows_sessions():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00"), workflow("d", "2026-05-04 10:00:00")],
            "server": [workflow("b", "2026-05-02 10:00:00"), workflow("c", "2026-05-03 10:00:00")],
        }
    )
    app.set_browse_mode("time")
    app.focus = "months"
    app.drill_in()
    tabs = app.current_tabs()
    assert "Machines" in tabs and "Harnesses" in tabs  # both dimensions, in the fleet
    app.tab = tabs.index("Machines")
    assert app.on_machines_tab
    names = [m for m, _it in app.zoom_machine_rows()]
    assert set(names) == {"laptop", "server"}
    app.machine_pick_index = names.index("server")
    app.drill_in()  # pick server -> its sessions in this month
    assert app.zoom_machine == "server"
    assert {w.id for w in app.current_sessions()} == {"b", "c"}
    app.drill_out()  # back to the Machines list of this zoom
    assert app.zoom_machine is None and app.current_tabs()[app.tab] == "Machines"


def test_cross_dimension_picker_counts_what_enter_opens():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00", cost=10.0)],
            "server": [workflow("b", "2026-05-02 10:00:00", cost=2.0)],
        }
    )
    for w in app.loaded:
        w.source = "OpenCode"
    app.set_browse_mode("time")
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Machines")
    names = [m for m, _it in app.zoom_machine_rows()]
    app.machine_pick_index = names.index("server")
    app.drill_in()  # zoom_machine = server
    app.tab = app.current_tabs().index("Harnesses")  # h/l over, machine still armed
    advertised = sum(int(it["sessions"]) for _s, it in app.zoom_source_rows())
    app.drill_in()  # pick the source
    assert advertised == len(app.current_sessions()) == 1  # server's one session, not both


def test_export_on_the_machines_picker_gives_machine_aggregates():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00", cost=10.0)],
            "server": [workflow("b", "2026-05-02 10:00:00", cost=2.0)],
        }
    )
    app.set_browse_mode("time")
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Machines")
    kind, header, rows = app._export_dataset()
    assert kind == "machines" and header[0] == "machine"  # aggregates, not the session list
    assert {r[0] for r in rows} == {"laptop", "server"}


def test_breadcrumb_shows_the_armed_per_scope_machine():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00", cost=3.0)],
            "server": [workflow("b", "2026-05-02 10:00:00", cost=9.0)],
        }
    )
    app.set_browse_mode("time")
    app.focus = "months"
    app.drill_in()
    app.tab = app.current_tabs().index("Machines")
    names = [m for m, _it in app.zoom_machine_rows()]
    app.machine_pick_index = names.index("server")
    app.drill_in()
    assert "server" in app.renderer.breadcrumb()  # the active machine scope is located


def test_machines_mode_has_no_per_scope_machines_picker_tab():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00")],
            "server": [workflow("b", "2026-05-02 10:00:00")],
        }
    )
    app.set_browse_mode("machines")
    app.drill_in()
    assert "Machines" not in app.current_tabs()


def test_refresh_machines_now_is_a_no_op_under_demo():
    app = _fleet()
    called = []
    app._refresh_backend = lambda keys: called.append(keys) or [(k, 1, "") for k in keys]
    app.store.demo = True
    assert app.refresh_machines_now("server") == []
    assert called == []  # the backend was never called


def test_machine_sessions_show_full_dates_not_a_bare_clock():
    app = _fleet()
    app.set_browse_mode("machines")
    r = app.renderer
    assert r.session_date_label() == "Started"
    w = app.machines[1]  # any box (0 is the fleet row); its first session
    sess = app.machine_scope(w)[0]
    assert r.session_started(sess) == sess.created_at[:10]  # the date, not created_at[11:16]


# --- The `M` global machine filter (the harness-picker twin) ------------------
def test_machine_filter_narrows_every_view_and_clears():
    app = _fleet()
    assert {w.machine for w in app.all_workflows} == {"laptop", "server"}
    app.select_machine_filter("server")
    assert app.machine_filter == "server"
    assert {w.id for w in app.all_workflows} == {"b", "c"}
    assert [m.name for m in app.machines] == ["server"]  # the list collapses to the one box
    assert app.machines_present is True  # ...but the `M`/mode gate stays on
    app.select_machine_filter(None)  # "All machines" clears it
    assert app.machine_filter is None
    assert {w.machine for w in app.all_workflows} == {"laptop", "server"}


def test_machine_filter_menu_opens_selects_and_reopens_at_current():
    app = _fleet()
    app.handle_key(None, ord("M"))
    assert app.machine_menu is True
    opts = app.machine_filter_options()
    assert opts[0] == ("", "All machines", True)  # nothing armed -> "All" is current
    assert {v for v, _l, _a in opts} == {"", "laptop", "server"}
    app.machine_menu_index = next(i for i, (v, _l, _a) in enumerate(opts) if v == "server")
    app.handle_machine_menu_key(10)  # Enter arms server + closes
    assert app.machine_menu is False and app.machine_filter == "server"
    app.handle_key(None, ord("M"))  # reopen: server is now current, "All" is not
    reopened = app.machine_filter_options()
    assert reopened[0][2] is False
    assert next(a for v, _l, a in reopened if v == "server") is True


def test_machine_filter_menu_M_advances_and_esc_cancels():
    app = _fleet()
    app.open_machine_menu()
    start = app.machine_menu_index
    app.handle_machine_menu_key(ord("M"))  # M walks the list too
    assert app.machine_menu_index == (start + 1) % len(app.machine_filter_options())
    app.handle_machine_menu_key(27)  # Esc cancels, filter unchanged
    assert app.machine_menu is False and app.machine_filter is None


def test_machine_filter_key_off_a_fleet_is_a_no_op():
    app = app_with([workflow("a", "2026-05-01 10:00:00")])
    app.handle_key(None, ord("M"))
    assert app.machine_menu is False
    assert "fleet" in app.notice


def test_machine_filter_revalidated_when_its_box_disappears():
    app = _fleet()
    app.select_machine_filter("server")
    app.loaded = [w for w in app.loaded if w.machine == "laptop"]  # server gone
    app._revalidate_machine_filter()
    assert app.machine_filter is None
    # ...and it survives a change that keeps the box.
    app2 = _fleet()
    app2.select_machine_filter("server")
    app2._revalidate_machine_filter()  # server still loaded
    assert app2.machine_filter == "server"


def test_machine_filter_shows_in_the_header_as_a_narrowing_chip():
    app = _fleet()
    app.select_machine_filter("server")
    screen = FakeScreen(24, 80)
    orig_cp = ot.curses.color_pair
    ot.curses.color_pair = lambda n: 0
    app.renderer.draw_mode_tabs = lambda *a, **k: None  # ACS glyphs, irrelevant to the chip
    try:
        app.renderer.draw_header(screen, 80)
    finally:
        ot.curses.color_pair = orig_cp
    assert "machine: server" in screen_text(screen)


def test_machine_filter_narrows_the_prices_overlay_like_the_harness_picker():
    from tests._support import _model_row, fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00", cost=3.0)],
            "server": [workflow("b", "2026-05-02 10:00:00", cost=9.0)],
        }
    )
    app._model_by_root = {
        "a": [_model_row("anthropic/claude-sonnet-4", 3.0, 1000)],
        "b": [_model_row("openai/gpt-5", 9.0, 2000)],
    }
    app._models_loaded = True
    # No filter: both machines' models show, and each drills to its own session.
    assert {e.canon for e in app.priced_model_entries()} == {"claude-sonnet-4", "gpt-5"}
    assert [w.id for w, _c, _t in app.price_model_sessions("claude-sonnet-4")] == ["a"]
    # Arm server: laptop's model vanishes from the table and its drill goes empty; server stays.
    app.select_machine_filter("server")
    assert {e.canon for e in app.priced_model_entries()} == {"gpt-5"}
    assert app.price_model_sessions("claude-sonnet-4") == []
    assert [w.id for w, _c, _t in app.price_model_sessions("gpt-5")] == ["b"]
    # ...but the filter is an IDENTITY narrowing, not a time window: it scopes by machine
    # over all loaded (P stays all-time), so clearing it restores the full app-wide table.
    app.select_machine_filter(None)
    assert {e.canon for e in app.priced_model_entries()} == {"claude-sonnet-4", "gpt-5"}


def test_machine_filter_key_is_advertised_wherever_it_is_handled():
    app = _fleet()
    orig_cycle = ot.sources.source_cycle  # a full keymap sweep evaluates `H`'s when, which
    ot.sources.source_cycle = lambda args: ["opencode", "claude"]  # probes the filesystem
    try:
        for setup in (lambda: None, lambda: setattr(app, "trends", True), None):
            if setup is None:
                app.trends = False
                app.show_prices = True
            else:
                setup()
            shown = {
                e.id for e in ot.keymap.KEYS if e.id in ot.keymap.FOOTER_ORDER and e.shown(app)
            }
            listed = {e.id for _t, rows in app.renderer.help_sections() for e in rows}
            assert "machine-filter" in shown  # a fleet, in every context M is handled
            assert ("machine-filter" in shown) == ("machine-filter" in listed)
    finally:
        ot.sources.source_cycle = orig_cycle


# --- The fleet `H` harness filter (machine_filter's orthogonal twin) ----------
def _mixed_fleet():
    # Two machines, two harnesses, so a harness filter and a machine filter can be shown
    # to compose without one implying the other: laptop runs OpenCode + Claude, server OpenCode.
    from tests._support import fleet_app

    a = workflow("a", "2026-05-01 10:00:00", cost=3.0)
    a.source = "OpenCode"
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0)
    c.source = "Claude Code"
    b = workflow("b", "2026-05-02 10:00:00", cost=9.0)
    b.source = "OpenCode"
    return fleet_app({"laptop": [a, c], "server": [b]})


def test_harness_filter_narrows_by_tool_and_keeps_every_machine():
    app = _mixed_fleet()
    assert {w.source for w in app.all_workflows} == {"OpenCode", "Claude Code"}
    app.select_harness_filter("OpenCode")
    assert {w.id for w in app.all_workflows} == {"a", "b"}
    assert {w.machine for w in app.all_workflows} == {"laptop", "server"}  # fleet intact
    assert app.machines_present is True
    app.select_machine_filter("server")  # composes
    assert {w.id for w in app.all_workflows} == {"b"}
    app.select_harness_filter(None)
    app.select_machine_filter(None)
    assert {w.id for w in app.all_workflows} == {"a", "b", "c"}


def test_H_in_a_fleet_filters_harness_and_never_swaps_the_store():
    app = _mixed_fleet()
    store_before = app.store
    app.handle_key(None, ord("H"))
    assert app.harness_menu is True and app.source_menu is False  # the FILTER, not the swap
    opts = app.harness_filter_options()
    assert opts[0] == ("", "All harnesses", True)
    app.harness_menu_index = next(i for i, (v, _l, _a) in enumerate(opts) if v == "Claude Code")
    app.handle_harness_menu_key(10)  # Enter arms it
    assert app.harness_filter == "Claude Code"
    assert app.store is store_before  # store NOT swapped -- the pulled boxes are still here
    assert {w.id for w in app.all_workflows} == {"c"}


def test_H_outside_a_fleet_still_opens_the_store_swap_picker():
    app, _chosen = _menu_app(current="opencode")
    try:
        app.handle_key(None, ord("H"))  # non-fleet -> the backend swap, not a harness filter
        assert app.source_menu is True and app.harness_menu is False
    finally:
        ot.sources.source_cycle = app._orig_cycle


def test_harness_filter_is_fleet_only_and_revalidates_away_when_the_fleet_is_gone():
    app = _mixed_fleet()
    app.select_harness_filter("OpenCode")
    app.loaded = [w for w in app.loaded if w.machine == "laptop"]  # one box left -> not a fleet
    app._revalidate_harness_filter()
    assert app.harness_filter is None  # even though OpenCode still exists, the fleet doesn't


def test_open_harness_menu_needs_more_than_one_harness():
    app = _fleet()  # both boxes are OpenCode-only, nothing armed
    assert app.can_harness_filter() is False  # nothing to filter -> H doesn't advertise it
    app.open_harness_menu()
    assert app.harness_menu is False
    assert "one harness" in app.notice


def test_armed_harness_filter_is_always_clearable_even_with_one_harness_left():
    app = _mixed_fleet()
    app.select_harness_filter("OpenCode")
    app.loaded = [w for w in app.loaded if w.source == "OpenCode"]  # Claude's session gone
    app._revalidate_harness_filter()
    assert app.harness_filter == "OpenCode"  # still a fleet with that harness -> kept
    assert app.can_harness_filter() is True  # armed -> must stay reachable to clear
    app.open_harness_menu()
    assert app.harness_menu is True
    assert app.harness_filter_options()[0][:2] == ("", "All harnesses")


def test_source_swap_out_of_a_fleet_keeps_machines_mode_on_this_box():
    app = app_with([workflow("a", "2026-05-01 10:00:00")])  # a non-fleet store
    app.browse_mode = "machines"  # as if we'd been in a fleet's Machines mode
    app.view = "zoom"
    app._reload_for_source()  # the no-restore path select_source uses
    assert app.browse_mode == "machines" and app.view == "browse"
    assert [m.name for m in app.machines] == [app.local_machine_name]


def _sourced(wid, source, when):
    w = workflow(wid, when)
    w.source = source
    return w


def test_a_source_swap_disarms_the_drills_of_the_modes_you_are_not_in():
    both = [
        _sourced("a", "Claude", "2026-05-01 10:00:00"),
        _sourced("b", "OpenCode", "2026-05-02 10:00:00"),
    ]
    for mode in ("projects", "machines"):
        app = app_with(list(both))
        app.set_browse_mode(mode)
        app.drill_in()
        app.zoom_source = "Claude"  # as if drilled into the Claude row of the Harnesses tab
        app.set_browse_mode("time")  # leave the mode -- the drill is snapshotted
        # `H` -> one backend: the swapped store carries only OpenCode sessions now.
        app.store._workflows = [_sourced("c", "OpenCode", "2026-05-03 10:00:00")]
        app._reload_for_source()
        app.set_browse_mode(mode)  # ...and come back
        assert app.zoom_source is None, mode
        assert [w.id for w in app.current_sessions()] == ["c"], mode


def test_a_plain_reload_disarms_the_dormant_drills_too():
    app = app_with(
        [
            _sourced("a", "Claude", "2026-05-01 10:00:00"),
            _sourced("b", "OpenCode", "2026-05-02 10:00:00"),
        ]
    )
    app.set_browse_mode("projects")
    app.drill_in()
    app.zoom_source = "Claude"
    app.set_browse_mode("time")
    app.store._workflows = [_sourced("c", "OpenCode", "2026-05-03 10:00:00")]
    app.reload()
    app.set_browse_mode("projects")
    assert app.zoom_source is None
    assert [w.id for w in app.current_sessions()] == ["c"]


def test_a_dormant_project_drill_survives_a_restoring_reload_iff_it_still_exists():
    def drilled():
        app = app_with(
            [
                workflow("a", "2026-05-01 10:00:00", directory="/work/alpha"),
                workflow("b", "2026-05-02 10:00:00", directory="/work/beta"),
            ]
        )
        app.set_browse_mode("projects")
        app.drill_in()
        app.zoom_project = app.project_root("/work/alpha")
        app.set_browse_mode("time")  # snapshot it, then reload from the other mode
        return app

    kept = drilled()
    kept._reload_for_source(kept.ui_snapshot())  # same data -> the drill is still valid
    kept.set_browse_mode("projects")
    assert kept.zoom_project == kept.project_root("/work/alpha")

    gone = drilled()
    gone.store._workflows = [workflow("b", "2026-05-02 10:00:00", directory="/work/beta")]
    gone._reload_for_source(gone.ui_snapshot())  # the project is no longer in the data
    gone.set_browse_mode("projects")
    assert gone.zoom_project is None
    assert [w.id for w in gone.current_sessions()] == ["b"]


def test_notices_overlay_swallows_mouse_events():
    app = app_with([workflow("s1", "2026-07-01 12:00:00"), workflow("s2", "2026-07-02 12:00:00")])
    app.view, app.workflow_index = "zoom", 0
    app.renderer.regions = [("rows", "session", 5, 20, 2, 100, 0)]  # a body region beneath it
    app.toast_history, app.toast_history_scroll = True, 5
    orig = ot.curses.getmouse
    try:
        ot.curses.getmouse = lambda: (0, 60, 12, 0, ot.curses.BUTTON4_PRESSED)
        app.handle_mouse()
        assert app.toast_history_scroll == 2 and app.toast_history  # wheel pages the overlay
        ot.curses.getmouse = lambda: (0, 60, 12, 0, ot.curses.BUTTON1_DOUBLE_CLICKED)
        app.handle_mouse()
        assert app.view == "zoom" and app.workflow_index == 0  # never drilled in behind it
        assert not app.toast_history  # the click closed it, as on help
    finally:
        ot.curses.getmouse = orig


def test_reopen_trends_survives_a_tab_that_no_longer_exists():
    app = app_with([workflow("s1", "2026-07-01 12:00:00")])
    assert "Machines" not in app.trend_tabs  # a single non-remote backend has no fleet
    app._trend_return = ("drill", "machine", "boxA", 0)
    app._reopen_trends(app._trend_return)  # must degrade, not crash
    assert not app.trends


def test_whatif_picker_ignores_a_stray_non_ascii_key():
    app = app_with([workflow("s1", "2026-07-01 12:00:00")])
    app.whatif_menu, app.whatif_filter_active, app.whatif_query = True, False, ""
    app.handle_whatif_menu_key("ä")
    assert app.whatif_query == "" and app.whatif_menu
    app.whatif_filter_active = True  # ...but the filter still takes it
    app.handle_whatif_menu_key("ä")
    assert app.whatif_query == "ä"


def test_reload_demotes_a_session_that_vanished_instead_of_showing_a_neighbour():
    keeper = workflow("keeper", "2026-07-01 12:00:00", cost=1.0)
    gone = workflow("gone", "2026-07-02 12:00:00", cost=9.0)

    app = _app_on_session([keeper, gone], "gone")
    app.view = "session"  # _app_on_session selects it; open its detail
    assert app.current_session().id == "gone"
    snap = app.ui_snapshot()
    app.store._workflows = [keeper]  # the re-pull no longer carries it
    app._reload_for_source(snap)
    assert app.view == "zoom"  # demoted, rather than silently showing `keeper`

    # ...and a session that survives the reload is still restored, not demoted.
    app2 = _app_on_session([keeper, gone], "keeper")
    app2.view = "session"
    snap2 = app2.ui_snapshot()
    app2._reload_for_source(snap2)
    assert app2.view == "session" and app2.current_session().id == "keeper"


def test_unpriced_tokens_are_restated_at_message_granularity():
    # Model rows supersede node-granular workflow totals when present.

    class NodeGranularStore(FakeStore):
        def model_breakdown(self):
            # One priced message and one $0 message on the same session and model: the
            # node's summed cost is > 0, so a node-granular rollup reports 0 unpriced.
            return [
                dict(_model_row("anthropic/claude-fable-5", 0.17, 10), root_id="w1"),
                dict(
                    _model_row("anthropic/claude-fable-5", 0.0, 900),
                    root_id="w1",
                    unpriced_input=900,
                    unpriced_output=0,
                    unpriced_reasoning=0,
                    unpriced_cache_read=0,
                    unpriced_cache_write=0,
                ),
            ]

    w = workflow("w1", "2026-07-01 12:00:00", cost=0.17, tokens=910)
    w.unpriced_tokens = 0  # what the node-granular rollup claimed
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(NodeGranularStore([w]), args)
    app._load_model_cache()
    assert app.loaded[0].unpriced_tokens == 900  # the $0 message's tokens, visible again


def test_deferred_model_scan_keeps_the_selected_session_when_estimates_reorder_rows():
    class DeferredStore(FakeStore):
        def model_breakdown(self):
            return [
                dict(
                    _model_row("anthropic/claude-haiku-4-5", 0.0, 1_000_000_000),
                    root_id="subscription",
                    input=1_000_000_000,
                )
            ]

    priced = workflow("priced", "2026-07-01 12:00:00", cost=5.0)
    subscription = workflow("subscription", "2026-07-01 13:00:00", cost=0.0)
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(DeferredStore([priced, subscription]), args)
    assert app.goto_session("priced") is True
    assert app.current_session().id == "priced"

    app._ensure_models()

    assert app.current_session().id == "priced"
    assert app.current_sessions()[0].id == "subscription"  # the list really did reorder


def test_a_box_ranks_models_over_exactly_the_sessions_its_enter_opens():
    from tests._support import fleet_app

    b = workflow("b", "2026-05-02 10:00:00", cost=9.0)
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0)
    b.source, c.source = "Claude Code", "OpenCode"
    app = fleet_app({"server": [b, c]})
    app._model_by_root = {
        "b": [_model_row("opus", 9.0, 900)],
        "c": [_model_row("opus", 0.6, 60)],
    }
    app.set_browse_mode("machines")
    app.drill_in()
    # Arm the box's Harnesses drill on Claude Code (session b only), then walk to Models.
    app.tab = app.current_tabs().index("Harnesses")
    keys = [s for s, _ in app.zoom_source_rows()]
    app.source_index = keys.index("Claude Code")
    app.drill_in()
    assert app.zoom_source == "Claude Code"
    assert {w.id for w in app.current_sessions()} == {"b"}
    app.tab = app.current_tabs().index("Models")
    ranked = {m: int(it["runs"]) for m, it in app.zoom_model_rows()}
    app.model_pick_index = 0
    app.drill_in()
    # Enter cleared the harness drill, so the ranking above must have covered the whole
    # box -- both sessions, not just Claude Code's one.
    assert app.zoom_source is None and app.zoom_model == "opus"
    opened = {w.id for w in app.current_sessions()}
    assert opened == {"b", "c"}
    assert ranked["opus"] == len(opened)


def test_the_browse_mode_table_is_the_only_place_modes_are_enumerated():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    modes = app.BROWSE_MODES
    assert [m.key for m in modes] == ["time", "projects", "machines"]
    assert app.BROWSE_MODE_KEYS == tuple(m.key for m in modes)
    assert app.mode_tab_list() == [(m.label, m.key) for m in modes]
    # Each mode's keymap action really exists in the `main` context, so the footer and
    # the help overlay can label it -- a typo here would silently print an empty chip.
    main = next(c for c in ot.tui.bindings.REGISTRY if c.name == "main")
    names = {a.name for a in main.actions}
    assert {m.action for m in modes} <= names
    # Exactly one hierarchical mode (Time's stacked Years/Months/Days); the rest are one
    # flat list, which is what the Tab cycle and the numbered panel jumps key on.
    assert [m.key for m in modes if m.hierarchical] == ["time"]
    for mode in modes:
        app.set_browse_mode(mode.key)
        assert app.browse_mode_spec is mode
        assert app.flat_browse_mode is not mode.hierarchical


def test_an_unknown_saved_browse_mode_falls_back_instead_of_raising():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.browse_mode = "harnesses"  # a mode from a future version, or a typo
    assert app.browse_mode_spec is app.BROWSE_MODES[0]
    assert app.flat_browse_mode is False


def test_the_selection_anchor_is_read_by_name_and_still_indexes():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    anchor = app.selection_anchor()
    assert anchor.session == "a"
    assert anchor.project == "/x"
    assert anchor.month == "2026-06"
    # Still a tuple: everything that already unpacked or indexed it is unaffected.
    assert tuple(anchor)[5] == anchor.session
    assert len(anchor) == 6
    year, month, day, project, machine, session = anchor
    assert (month, project, session) == ("2026-06", "/x", "a")


def test_both_session_lists_scope_from_the_one_mode_scope():
    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-06-01 12:00:00", cost=5.0, directory="/x")],
            "server": [workflow("b", "2026-06-02 12:00:00", cost=3.0, directory="/y")],
        }
    )
    for mode in app.BROWSE_MODES:
        app.set_browse_mode(mode.key)
        scope = {w.id for w in app.mode_scope_workflows()}
        assert scope, mode.key  # every mode's sidebar lands on something
        assert {w.id for w in app.current_sessions()} <= scope, mode.key
        assert {w.id for w in app._zoom_picker_scope("source")} <= scope, mode.key
    # In a flat mode the scope IS the selected row, so it must not be the whole corpus.
    app.set_browse_mode("machines")
    app.machine_index = 1  # past the synthetic "all machines" row, onto a real box
    assert len(app.mode_scope_workflows()) == 1


def _zoom_reprice_app():
    # Harness/model B is $0 recorded with a big unpriced block; A is $5 recorded with
    # none. So the estimate view ranks B first and the recorded view ranks A first --
    # every cost-ranked zoom picker visibly reorders under "$".
    a = workflow("a", "2026-06-01 12:00:00", cost=5.0, directory="/alpha")
    b = workflow("b", "2026-06-01 13:00:00", cost=0.0, directory="/bravo")
    a.source, b.source = "A", "B"

    class _Merged(FakeStore):
        combined = True
        source_name = "all"

    app = ot.App(_Merged([a, b]), type("Ar", (), {"since": None, "until": None, "days": None})())
    app._model_by_root = {
        "a": [dict(_model_row("anthropic/claude-opus-4-5", 5.0, 10), input=0)],
        "b": [dict(_model_row("openai/gpt-5", 0.0, 20_000_000), input=20_000_000)],
    }
    app._models_loaded = True
    app._compute_api_costs()
    app._apply_price_mode()
    app.view = "zoom"
    app.focus = "days"
    return app


def test_dollar_keeps_every_zoom_picker_on_the_row_it_was_on():
    app = _zoom_reprice_app()
    assert app.show_api_prices
    assert [n for n, _ in app.zoom_source_rows()] == ["B", "A"]
    assert app.zoom_selected_source() == "B"
    assert [n for n, _ in app.zoom_model_rows()][0] == "openai/gpt-5"
    assert app.zoom_selected_model() == "openai/gpt-5"

    app.toggle_api_prices()  # to the recorded view, which reverses both rankings
    assert [n for n, _ in app.zoom_source_rows()] == ["A", "B"]
    assert app.zoom_selected_source() == "B" and app.source_index == 1
    assert app.zoom_selected_model() == "openai/gpt-5" and app.model_pick_index == 1


def test_dollar_leaves_the_projects_sidebar_selection_to_the_anchor():
    app = _zoom_reprice_app()
    app.set_browse_mode("projects")
    before = app.selected_project_summary
    app.toggle_api_prices()
    after = app.selected_project_summary
    assert before is not None and after is not None
    assert after.directory == before.directory
