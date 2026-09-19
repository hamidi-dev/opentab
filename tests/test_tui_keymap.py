from unittest.mock import patch

import opentab as ot

from tests._support import AttrScreen, FakeScreen, _model_row, app_with, screen_text, workflow


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
    assert app.help  # an unbound key is swallowed -- you read this WHILE choosing a key
    app.handle_key(None, 27)
    assert not app.help


def test_expand_label_is_consistent_and_only_highlights_visible_maximized_zoom():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    entry = ot.keymap.BY_ID["max"]
    for view in ("browse", "zoom", "session"):
        app.view = view
        for maximized in (False, True):
            app.zoom_maximized = maximized
            assert entry.chip_segments(app) == [("+ expand", view == "zoom" and maximized)]


def test_mouse_wheel_scrolls_the_help_overlay():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.handle_key(None, ord("?"))
    assert app.help and app.help_scroll == 0
    app._wheel_down = getattr(ot.curses, "BUTTON5_PRESSED", 0) or ot.curses.REPORT_MOUSE_POSITION
    orig = ot.curses.getmouse
    try:
        ot.curses.getmouse = lambda: (0, 0, 0, 0, app._wheel_down)
        app.handle_mouse()
        assert app.help and app.help_scroll == 3
        ot.curses.getmouse = lambda: (0, 0, 0, 0, ot.curses.BUTTON4_PRESSED)
        app.handle_mouse()
        assert app.help and app.help_scroll == 0
        app.handle_mouse()
        assert app.help and app.help_scroll == 0
        ot.curses.getmouse = lambda: (0, 0, 0, 0, ot.curses.BUTTON1_CLICKED)
        app.handle_mouse()
        assert not app.help
    finally:
        ot.curses.getmouse = orig


def test_whats_new_opens_from_main_and_help_then_returns_to_its_caller():
    app = _keymap_app()
    underlying = (app.view, app.focus, app.tab, app.scroll)
    app.handle_key(None, ord("W"))
    assert app.whats_new and (app.view, app.focus, app.tab, app.scroll) == underlying
    app.handle_key(None, ord("j"))
    assert app.whats_new_scroll == 1
    app.handle_key(None, ord("W"))
    assert not app.whats_new and not app.help

    app.handle_key(None, ord("?"))
    app.handle_key(None, ord("W"))
    assert app.whats_new and app.help
    app.handle_key(None, 27)
    assert not app.whats_new and app.help


def test_whats_new_history_stops_at_bounds_resets_scroll_and_reopens_current():
    app = _keymap_app()
    app.open_whats_new()
    assert app.whats_new_index == 0
    app.whats_new_scroll = 9
    app.handle_key(None, ord("h"))
    assert app.whats_new_index == 0 and app.whats_new_scroll == 9
    app.handle_key(None, ord("l"))
    assert app.whats_new_index == 1 and app.whats_new_scroll == 0
    for _ in range(20):
        app.handle_key(None, ot.curses.KEY_RIGHT)
    assert app.whats_new_index == len(app.whats_new_history) - 1
    app.whats_new_scroll = 4
    app.handle_key(None, ord("l"))
    assert app.whats_new_index == len(app.whats_new_history) - 1
    assert app.whats_new_scroll == 4
    app.handle_key(None, ot.curses.KEY_LEFT)
    assert app.whats_new_index == len(app.whats_new_history) - 2
    app.handle_key(None, ord("h"))
    assert app.whats_new_index == len(app.whats_new_history) - 3
    app.handle_key(None, 27)
    app.open_whats_new()
    assert app.whats_new_index == 0 and app.whats_new_scroll == 0


def test_whats_new_history_uses_remapped_navigation_and_opens_the_viewed_release():
    app = _keymap_app()
    app.keymap = ot.tui.bindings.Keymap(
        {
            ("whats-new", "older"): ["n"],
            ("whats-new", "newer"): ["p"],
        }
    )
    app.open_whats_new()
    app.handle_key(None, ord("h"))
    assert app.whats_new_index == 0
    app.handle_key(None, ord("n"))
    viewed = app.whats_new_notes
    assert viewed is not None and app.whats_new_index == 1
    screen = FakeScreen(24, 80)
    original = ot.curses.color_pair
    try:
        ot.curses.color_pair = lambda n: n
        app.renderer.draw_whats_new(screen, 2, 23, 80)
    finally:
        ot.curses.color_pair = original
    footer = screen_text(screen)
    assert "n older" in footer and "p newer" in footer
    assert f"2/{len(app.whats_new_history)}" in footer
    with patch("opentab.tui.app.open_path", return_value=True) as opened:
        app.handle_key(None, ord("o"))
    opened.assert_called_once_with(viewed["release_url"])
    app.handle_key(None, ord("p"))
    assert app.whats_new_index == 0


def test_whats_new_uses_remapped_keys_and_swallows_mouse_clicks():
    app = _keymap_app()
    app.keymap = ot.tui.bindings.Keymap(
        {
            ("main", "whats_new"): ["v"],
            ("whats-new", "close"): ["x"],
        }
    )
    assert app.handle_key(None, ord("W"))  # stolen/unbound default does nothing
    assert not app.whats_new
    app._announce_whats_new()
    assert app.toasts[-1].text == "Press v to see what's new"
    app.handle_key(None, ord("v"))
    assert app.whats_new
    app.renderer.regions = [("rows", "session", 5, 20, 2, 100, 0)]
    original = ot.curses.getmouse
    try:
        ot.curses.getmouse = lambda: (0, 60, 12, 0, ot.curses.BUTTON1_DOUBLE_CLICKED)
        app.handle_mouse()
    finally:
        ot.curses.getmouse = original
    assert app.whats_new and app.view == "browse"
    app.handle_key(None, ord("x"))
    assert not app.whats_new


def test_whats_new_renders_compact_release_sections_at_80x24():
    app = _keymap_app()
    app.open_whats_new()
    screen = FakeScreen(24, 80)
    original = ot.curses.color_pair
    try:
        ot.curses.color_pair = lambda n: n
        app.renderer.draw_whats_new(screen, 2, 23, 80)
    finally:
        ot.curses.color_pair = original
    text = screen_text(screen)
    assert "What's New" in text
    assert f"v{ot.__version__}" in text
    assert app.whats_new_notes is not None
    assert app.whats_new_notes["sections"][0]["title"] in text
    assert app.whats_new_scroll == 0


def test_whats_new_fix_only_panel_omits_empty_sections_and_keeps_wrapped_text():
    app = _keymap_app()
    message = "Corrected a cache-write estimate. " * 5
    app.whats_new_history = [
        {"version": ot.__version__, "sections": [{"title": "Fixed", "items": [{"text": message}]}]}
    ]
    original = ot.curses.color_pair
    try:
        ot.curses.color_pair = lambda n: n
        lines = app.renderer.whats_new_lines(68)
    finally:
        ot.curses.color_pair = original
    assert lines[0][0][1] == "Fixed"
    assert not any(text in ("New", "Improved") for line in lines for _, text, _ in line)
    assert all(dx + ot.display_width(text) <= 68 for line in lines for dx, text, _ in line)
    body = " ".join(text for line in lines[1:] for dx, text, _ in line if dx == 2)
    assert body == message.strip()


def test_whats_new_uses_full_body_height_and_centers_both_border_labels():
    for height, width in ((24, 80), (48, 128)):
        app = _keymap_app()
        app.whats_new_history = [
            {
                "version": ot.__version__,
                "sections": [{"title": "Fixed", "items": [{"text": "A fix."}]}],
            }
        ]
        screen = FakeScreen(height, width)
        frames, writes = [], []
        app.renderer.draw_frame = lambda *args, frames=frames: frames.append(args[1:])
        app.renderer.write = lambda *args, writes=writes: writes.append(args[1:])
        original = ot.curses.color_pair
        try:
            ot.curses.color_pair = lambda n: n
            app.renderer.draw_whats_new(screen, 3, height - 2, width)
        finally:
            ot.curses.color_pair = original
        y, x, h, w, _ = frames[0]
        assert y == 3 and h == height - 5
        for row, phrase in ((y, "What's New"), (y + h - 1, "full release")):
            label = next(write for write in writes if write[0] == row and phrase in write[2])
            _, label_x, text, _ = label
            assert label_x == x + (w - ot.display_width(text)) // 2


def _keymap_app(workflows=None):
    app = app_with(workflows or [workflow("a", "2026-06-01 12:00:00")])
    app.can_switch_source = lambda: False  # the bare test Args carries no source flags
    return app


def test_help_lists_the_keys_that_work_where_you_are():
    app = _keymap_app()
    app.focus = "months"
    titles = [t for t, _ in app.renderer.help_sections()]
    assert titles == ["Here — browse · Months", "Navigation", "Pickers", "Global"]

    def here(a):
        return {
            e.id for t, rows in a.renderer.help_sections() if t.startswith("Here") for e in rows
        }

    assert "enter" in here(app) and "bookmark" not in here(app)

    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    ids = here(app)
    assert {"enter", "bookmark", "note", "sort", "filter", "max"} <= ids
    assert [t for t, _ in app.renderer.help_sections()][0] == "Here — zoom · Sessions"

    # Overlays own the keyboard, so they are their own context: their keys, not the
    # ones the view underneath would offer (which they swallow).
    app.trends = True
    ids = {e.id for _t, rows in app.renderer.help_sections() for e in rows}
    assert {"trends-tabs", "trends-page", "trends-enter", "trends-close"} <= here(app)
    assert [t for t, _ in app.renderer.help_sections()][0] == "Here — Trends · Daily"
    # Trends binds none of these, so neither the help nor the footer may offer them.
    assert not {"bookmark", "note", "max", "sort", "filter", "range", "whatif", "quit"} & ids
    # j/k is the one Trends key whose job changes per tab -- say which one it is doing.
    jk = next(e for e in ot.keymap.KEYS if e.id == "trends-page")
    assert "month" in jk.text(app)
    app.trend_tab = app.trend_tabs.index("Models")
    assert "row" in jk.text(app) and jk.shown(app)
    app.trend_tab = app.trend_tabs.index("Monthly")
    assert not jk.shown(app)
    shades = next(e for e in ot.keymap.KEYS if e.id == "trends-shades")
    assert not shades.shown(app)
    app.trend_tab = app.trend_tabs.index("Calendar")
    assert shades.shown(app)
    app.trends = False

    app.show_prices = True
    ids = {e.id for _t, rows in app.renderer.help_sections() for e in rows}
    assert {"prices-view", "prices-pin", "prices-enter", "prices-refresh"} <= here(app)
    # R refreshes the catalog inside the P overlay -- it is not the range prompt there.
    assert not {"bookmark", "range", "whatif", "reload"} & ids
    # f/s work on the price table itself, whatever the view hidden behind it is doing.
    assert {"sort", "filter"} <= ids

    # A model's session drill inside P is its own context: it only scrolls and backs out.
    app.prices_model = "anthropic/claude-opus-4-8"
    ids = here(app)
    assert [t for t, _ in app.renderer.help_sections()][0] == "Here — Prices · sessions"
    assert {"price-drill-back", "price-drill-close"} <= ids
    assert (
        not {
            "prices-view",
            "prices-pin",
            "prices-enter",
            "prices-refresh",
            "sort",
            "filter",
            "export",
        }
        & ids
    )
    app.prices_model = None

    # The two overlays cover each other: whichever was opened LAST owns the keyboard,
    # so that is the context, and the covered one's keys must not be advertised.
    app.trend_tab = 0  # the earlier block left the cursor on Calendar
    app.trends = True  # T over the already-open P
    assert [t for t, _ in app.renderer.help_sections()][0] == "Here — Trends · Daily"
    ids = here(app)
    assert "trends-page" in ids and "prices-view" not in ids
    app.show_prices = True  # P raised back over T
    assert [t for t, _ in app.renderer.help_sections()][0] == "Here — Prices"
    ids = here(app)
    assert "trends-page" not in ids and "prices-view" in ids
    # Closing the top one falls back to the one it covered, not to the browse view.
    app.show_prices = False
    assert app.trends and app.overlay_top == "trends"
    assert [t for t, _ in app.renderer.help_sections()][0] == "Here — Trends · Daily"
    app.trends = False


def test_every_registry_action_is_discoverable():
    from opentab.tui import bindings

    surfaced = set()
    for entry in ot.keymap.KEYS:
        for token in entry.actions:
            surfaced.add(token.rstrip("*"))
    # Named in a summary/hint (computed off the live keymap there), not as an entry
    # of their own: the pager-bracket aliases, the panel digits and browse-mode letters
    # (their entries print literal labels derived from the same bindings), and the
    # overlays' floating D toggle. The focused-chart cursor keys used to live here too,
    # back when the Trends tab row carried a hint; they are a keybar entry now.
    prose = {
        "older",
        "newer",
        "panel_1",
        "panel_2",
        "panel_3",
        "panel_detail",
        "mode_time",
        "mode_projects",
        "mode_harnesses",
        "mode_machines",
        "demo_toggle",
    }
    # Contexts whose keys are taught by their own chrome (modal titles, box hints,
    # input-line hints -- all rendered from the live keymap), not by the ? overlay.
    self_documenting = {
        "help",
        "whats-new",
        "notices",
        "menu",
        "menu.search-project",
        "menu.source",
        "menu.harness",
        "menu.machine",
        "menu.sort",
        "menu.theme",
        "menu.demo",
        "menu.launch",
        "menu.whatif",
        "menu.whatif.filter",
        "filter",
        "input",
        "prompt.warning",
        "prompt.prices",
    }
    missing = []
    for ctx in bindings.REGISTRY:
        if ctx.name in self_documenting:
            continue
        for action in ctx.actions:
            if action.name not in surfaced and action.name not in prose:
                missing.append(f"{ctx.name}.{action.name}")
    assert not missing, f"registry actions nothing surfaces: {missing}"


def test_footer_and_help_cannot_disagree():
    app = _keymap_app()
    for setup in (
        lambda: None,
        lambda: (
            setattr(app, "view", "zoom"),
            setattr(app, "tab", app.month_tabs.index("Sessions")),
        ),
        lambda: setattr(app, "trends", True),
        lambda: (setattr(app, "trends", False), setattr(app, "show_prices", True)),
    ):
        setup()
        chips = {e.id for e in ot.keymap.KEYS if e.id in ot.keymap.FOOTER_ORDER and e.shown(app)}
        listed = {e.id for _t, rows in app.renderer.help_sections() for e in rows}
        assert chips <= listed, f"footer offers what help doesn't explain: {chips - listed}"


def test_the_price_drill_and_trends_drill_offer_their_own_keys():
    app = _keymap_app()
    app.show_prices = True
    app.prices_model = "anthropic/claude-opus-4-8"
    chips = {
        e.id
        for e in ot.keymap.KEYS
        if e.id in ot.keymap.FOOTER_ORDER and e.shown(app) and e.chip_segments(app)
    }
    assert {"price-drill-back", "price-drill-close"} <= chips
    assert not {"prices-view", "prices-pin", "prices-enter", "prices-refresh"} & chips

    # PgDn/PgUp do work inside a Trends ranked-row drill, so they must be listed there.
    app.show_prices = False
    app.prices_model = None
    app.trends = True
    page = next(e for e in ot.keymap.KEYS if e.id == "page")
    assert not page.shown(app)
    app.trend_drill = ("model", "anthropic/claude-opus-4-8")
    assert page.shown(app)


def test_trends_chips_say_what_the_key_will_actually_do():
    app = _keymap_app()
    app.trends = True
    enter = next(e for e in ot.keymap.KEYS if e.id == "trends-enter")
    close = next(e for e in ot.keymap.KEYS if e.id == "trends-close")
    assert enter.chip_segments(app) == [("Enter focus", False)]
    assert close.chip_segments(app) == [("Esc close", False)]
    app.trend_focus = True
    assert enter.chip_segments(app) == [("Enter drill", False)]
    assert close.chip_segments(app) == [("Esc back", False)]
    app.trend_focus = False
    app.trend_tab = app.trend_tabs.index("Models")
    assert enter.chip_segments(app) == [("Enter drill", False)]


def test_the_trends_sort_chip_only_shows_where_a_ranking_is_on_screen():
    app = _keymap_app()
    app.trends = True
    sort = next(e for e in ot.keymap.KEYS if e.id == "trends-sort")
    assert app.trend_tabs[app.trend_tab] == "Daily" and not sort.shown(app)
    app.trend_tab = app.trend_tabs.index("Harnesses")
    assert sort.shown(app) and sort.chip_segments(app) == [("s sort", False)]
    # ...and not inside a drilled row's session list, which is its own ranking.
    app.trend_drill = ("source", "OpenCode")
    assert not sort.shown(app)


def test_the_whatif_chip_only_shows_where_a_target_would_change_something():
    app = _keymap_app()
    whatif = next(e for e in ot.keymap.KEYS if e.id == "whatif")
    assert app.view == "browse" and not whatif.shown(app)
    app.view = "zoom"
    assert not whatif.shown(app)
    app.view = "session"
    assert whatif.shown(app) and whatif.chip_segments(app) == [("w model", False)]
    # Armed, it stays reachable wherever you wander -- the lit chip is how you clear it.
    app.view = "browse"
    app.whatif_model = "anthropic/claude-opus-4-5"
    assert whatif.shown(app) and whatif.chip_segments(app) == [("w model", True)]


def test_a_composite_chip_falls_back_to_its_plain_label():
    app = _keymap_app()
    tab = next(e for e in ot.keymap.KEYS if e.id == "tab-focus")
    app.focus = "months"
    assert [t for t, _on in tab.chip_segments(app)][:1] == ["Tab "]  # browse: the panels light
    app.view = "zoom"
    assert tab.shown(app) and tab.chip_segments(app) == [("Tab focus", False)]


def test_help_swallows_a_mistyped_key_and_closes_explicitly():
    app = _keymap_app()
    app.handle_key(None, ord("?"))
    assert app.help
    app.handle_key(None, ord("m"))  # not a binding
    assert app.help  # ...and the overlay stands
    app.handle_key(None, 27)
    assert not app.help
    app.handle_key(None, ord("?"))
    app.handle_key(None, ord("?"))  # toggles off
    assert not app.help


def test_footer_highlights_the_focused_time_panel():
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
        i = line.index("t/p/u/m mode")  # all four modes, fleet or not
        assert scr.attrs[(23, i)] == accent and scr.attrs[(23, i + 2)] == 4  # time mode: t lit
        app.browse_mode = "projects"
        scr, line = footer_line()
        i = line.index("t/p/u/m mode")
        assert scr.attrs[(23, i + 2)] == accent and scr.attrs[(23, i)] == 4  # projects: p lit
        app.browse_mode = "harnesses"
        scr, line = footer_line()
        i = line.index("t/p/u/m mode")
        assert scr.attrs[(23, i + 4)] == accent and scr.attrs[(23, i)] == 4  # harnesses: u lit
        app.browse_mode = "machines"
        scr, line = footer_line()
        i = line.index("t/p/u/m mode")
        assert scr.attrs[(23, i + 6)] == accent and scr.attrs[(23, i)] == 4  # machines: m lit
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip


def test_the_here_label_names_every_flat_mode_not_just_projects():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    for mode in app.BROWSE_MODES:
        app.set_browse_mode(mode.key)
        label = ot.tui.keymap.context_label(app)
        if mode.hierarchical:
            assert label.startswith("browse · ")  # Time names its focused PANEL
        else:
            assert label == f"browse · {mode.label}"


def test_the_footer_mode_chips_are_built_from_the_mode_table():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    segs = ot.tui.keymap._mode_segments(app)
    labels = [text for text, _lit in segs]
    for mode in app.BROWSE_MODES:
        assert app.keymap.label("main", mode.action) in labels
    # Exactly one chip is lit, and it is the active mode's.
    app.set_browse_mode("machines")
    lit = [text for text, on in ot.tui.keymap._mode_segments(app) if on]
    assert lit == [app.keymap.label("main", "mode_machines")]


def test_enter_is_offered_on_every_pickerized_zoom_tab():
    # A drillable tab whose Enter the footer hides is a drill nobody finds: Models and
    # Machines pick a row and open a scope exactly as Sessions/Projects/Harnesses do.
    b = workflow("b", "2026-05-02 10:00:00", cost=9.0, directory="/work/alpha")
    c = workflow("c", "2026-05-03 10:00:00", cost=1.0, directory="/work/beta")
    app = app_with([b, c])
    app._model_by_root = {"b": [_model_row("opus", 9.0, 900)], "c": [_model_row("haiku", 0.4, 40)]}
    app.focus = "months"
    app.drill_in()
    enter = next(e for e in ot.keymap.KEYS if e.id == "enter")

    for tab in ("Sessions", "Projects", "Models"):
        app.tab = app.current_tabs().index(tab)
        assert enter.shown(app), tab
    app.tab = app.current_tabs().index("Models")
    assert "economics" in enter.text(app)
    app.tab = app.current_tabs().index("Overview")
    assert not enter.shown(app)  # a static pane opens nothing


def test_the_focused_chart_and_calendar_shades_keep_their_own_keybar_chips():
    # The Trends tab row carries no key hint any more, so the keybar is the only place
    # the focused chart's arrows and the Calendar's shade keys are advertised. Both were
    # help-only (the arrows not even that, once focused) while that hint existed.
    app = _keymap_app()
    app.trends = True
    arrows = next(e for e in ot.keymap.KEYS if e.id == "trends-chart-cursor")
    shades = next(e for e in ot.keymap.KEYS if e.id == "trends-shades")
    assert not arrows.shown(app)  # nothing to walk until Enter focuses a chart
    app.trend_focus = True
    assert arrows.shown(app) and arrows.chip_segments(app) == [("← ↑ ↓ → move", False)]
    app.trend_focus = False

    app.trend_tab = app.trend_tabs.index("Calendar")
    assert shades.chip_segments(app) == [("+/- shades", False)]
    assert {"trends-chart-cursor", "trends-shades"} <= set(ot.keymap.FOOTER_ORDER)

    # Both must reach the keybar, not just the table: footer_parts is what paints it.
    app.trend_focus = True
    painted = {seg[0] for part in ot.keymap.footer_parts(app) for seg in part}
    assert {"← ↑ ↓ → move", "+/- shades"} <= painted


def test_the_keybar_is_centred_and_never_crowds_the_version():
    # The hints sit centred under the centred mode tabs; where the row is too narrow to
    # do that clear of the version tag, they re-centre in the room left of it.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.can_switch_source = lambda: False
    app.renderer.hline = lambda *a: None  # ACS_HLINE needs initscr
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair, ot.curses.init_pair = (lambda n: 0), (lambda *a: None)
    try:

        def margins(width):
            scr = AttrScreen(24, width)
            app.renderer.draw_footer(scr, 24, width)
            line = "".join(scr.cells.get((23, x), " ") for x in range(width))
            hints, ver = line[: line.index(" v")], line[line.index(" v") :]
            return len(hints) - len(hints.rstrip()), hints.index(hints.strip()[0]), ver

        right, left, ver = margins(200)  # room to centre on the whole row
        assert abs(left - (right + len(ver))) <= 1 and left > 1
        right, left, _ = margins(80)  # version in the way: centred in what is left
        assert abs(left - right) <= 1 and left > 0
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip


def test_help_uses_the_dispatch_context_in_every_overlay_subview():
    from opentab.tui.bindings import Keymap

    for ctx in ("main", "trends", "trends.chart", "trends.drill", "prices", "prices.sessions"):
        app = _keymap_app()
        app.source_key = "opencode"
        app.loaded[0].machine = "local"
        remote = workflow("remote", "2026-06-01 13:00:00")
        remote.machine = "remote"
        app.loaded.append(remote)
        app.can_harness_filter = lambda: True
        if ctx.startswith("trends"):
            app.trends = True
            app.trend_focus = ctx == "trends.chart"
            if ctx == "trends.drill":
                app.trend_drill = ("provider", "anthropic")
        elif ctx.startswith("prices"):
            app.show_prices = True
            if ctx == "prices.sessions":
                app.prices_model = "opus"
        assert ot.keymap.binding_context(app) == ctx
        ids = ["source", "machine-filter", "theme", "dollar", "help"]
        ids += ["demo" if ctx == "main" else "demo-toggle"]
        ids += ["prices"] if ctx.startswith("trends") else ["trends"]
        ids += (
            ["trends-tabs", "trends-enter", "trends-page", "trends-close"]
            if ctx.startswith("trends")
            else ["move", "page", "ends"]
        )
        for key_id in ids:
            entry = ot.keymap.BY_ID[key_id]
            action = entry.actions[0].rstrip("*")
            app.keymap = Keymap({(ctx, action): ["v"]})
            assert entry.shown(app), (ctx, key_id)
            assert entry.label(app).split()[0] == "v", (ctx, key_id, entry.label(app))
            app.help = True
            expected = "help" if key_id in ("source", "machine-filter", "theme", "demo") else ctx
            assert entry.context(app) == expected
            app.help = False


def test_focused_chart_dispatches_remapped_global_actions_from_its_context():
    app = _keymap_app()
    app.trends = app.trend_focus = True
    app.keymap = ot.tui.bindings.Keymap({("trends.chart", "help"): ["v"]})
    app.handle_key(None, ord("?"))
    assert not app.help
    app.handle_key(None, ord("v"))
    assert app.help


def test_help_picker_remaps_and_demo_dispatch():
    app = _keymap_app()
    app.help = True
    app.source_key = "opencode"
    calls = []
    app.demo_action = lambda: calls.append("demo")
    for key_id, action in (
        ("theme", "theme"),
        ("source", "harness"),
        ("machine-filter", "machine"),
        ("keymap", "edit_keymap"),
        ("demo", "demo"),
    ):
        app.keymap = ot.tui.bindings.Keymap({("help", action): ["v"]})
        assert ot.keymap.BY_ID[key_id].label(app) == "v"
    app.handle_key(None, ord("v"))
    assert calls == ["demo"] and app.help


def test_help_gates_demo_actions_and_names_flat_panels():
    app = _keymap_app()
    for mode in app.BROWSE_MODES:
        app.set_browse_mode(mode.key)
        entry = ot.keymap.BY_ID["panels"]
        assert ("2" in entry.label(app)) == mode.hierarchical
        if not mode.hierarchical:
            assert mode.label in entry.text(app)
            noun = "harness" if mode.key == "harnesses" else mode.key.rstrip("s")
            assert noun in ot.keymap.BY_ID["enter"].text(app)
    app.store.demo = True
    for key_id in ("launch", "export", "open", "refresh-machines", "dollar", "note"):
        assert not ot.keymap.BY_ID[key_id].shown(app), key_id


def test_directory_help_requires_a_visible_target_and_plus_works_in_sessions():
    app = _keymap_app([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    assert not ot.keymap.BY_ID["open"].shown(app)
    app.view = "session"
    assert ot.keymap.BY_ID["open"].shown(app)
    assert ot.keymap.BY_ID["max"].shown(app)
    before = app.zoom_maximized
    app.handle_key(None, ord("+"))
    assert app.zoom_maximized != before and app.view == "session"


def test_help_aligns_wide_character_bindings_by_terminal_cells():
    app = _keymap_app()
    app.keymap = ot.tui.bindings.Keymap({("main", "select"): ["界"]})
    original = ot.curses.color_pair
    try:
        ot.curses.color_pair = lambda n: 0
        lines = app.renderer.help_lines(100)
    finally:
        ot.curses.color_pair = original
    row = next(row for row in lines if len(row) == 2 and row[0][1] == "界")
    assert row[1][0] - (row[0][0] + ot.display_width(row[0][1])) == 2


def test_launch_hint_hides_a_target_shortcut_stolen_by_a_menu_remap():
    app = _keymap_app()
    app.launch_menu = app.loaded[0]
    app.launch_targets = lambda: [("w", "window", "new window"), ("y", "copy", "copy command")]
    app.keymap = ot.tui.bindings.Keymap({("menu.launch", "down"): ["w"]})
    captured = []
    app.renderer.draw_modal = lambda *args: captured.extend(args[-1])
    original = ot.curses.color_pair
    try:
        ot.curses.color_pair = lambda n: 0
        app.renderer.draw_launch_menu(FakeScreen(24, 80), 24, 80)
    finally:
        ot.curses.color_pair = original
    assert any(text == "    new window" for text, _ in captured)
    assert any(text == " y  copy command" for text, _ in captured)
    app.handle_launch_key(ord("w"))
    assert app.launch_menu_index == 1 and app.launch_menu is not None
