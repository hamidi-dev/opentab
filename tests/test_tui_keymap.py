"""The one keymap table behind the help overlay and the footer (tui/keymap.py)."""

import os

import opentab as ot

from tests._support import AttrScreen, app_with, workflow


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
    app.handle_key(None, 27)  # closing is explicit: Esc / q / ?
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


def _keymap_app(workflows=None):
    app = app_with(workflows or [workflow("a", "2026-06-01 12:00:00")])
    app.can_switch_source = lambda: False  # the bare test Args carries no source flags
    return app


def test_help_lists_the_keys_that_work_where_you_are():
    # The `?` overlay is context-first, lazygit-style: what works HERE, then how to
    # move, then the globals. "Here" is computed from the view/tab/overlay you are in.
    app = _keymap_app()
    app.focus = "months"
    titles = [t for t, _ in app.renderer.help_sections()]
    assert titles == ["Here — browse · Months", "Navigation", "Global"]

    def here(a):
        return {
            e.id for t, rows in a.renderer.help_sections() if t.startswith("Here") for e in rows
        }

    # Browse: nothing is selected in a session sense, so no bookmark/note/launch keys.
    assert "enter" in here(app) and "bookmark" not in here(app)

    # Zoom on the Sessions tab: the session actions arrive, because a session is selected.
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
    assert "month" in jk.text(app)  # Daily
    app.trend_tab = app.trend_tabs.index("Models")
    assert "row" in jk.text(app) and jk.shown(app)  # a ranked tab: rows, not bars
    app.trend_tab = app.trend_tabs.index("Monthly")
    assert not jk.shown(app)  # one chart, nothing to page -- j/k does nothing here
    shades = next(e for e in ot.keymap.KEYS if e.id == "trends-shades")
    assert not shades.shown(app)  # +/- only shade the Calendar
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

    # P opens from INSIDE Trends and owns the keyboard (handle_key checks it first), so
    # the context is Prices even though both flags are set.
    app.trends = True
    assert [t for t, _ in app.renderer.help_sections()][0] == "Here — Prices"
    ids = here(app)
    assert "trends-page" not in ids and "prices-view" in ids


def test_every_binding_the_app_handles_is_documented():
    # The table is the source of truth for BOTH the footer and the help, so a binding
    # missing from it is a key nobody can discover. Hold it against what the App
    # actually binds: every ord("x") literal in its key handlers.
    import ast

    source = os.path.join(os.path.dirname(ot.__file__), "tui", "app.py")
    with open(source) as fh:
        tree = ast.parse(fh.read())
    bound = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ord"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            bound.add(node.args[0].value)
    documented = {ch for entry in ot.keymap.KEYS for ch in entry.binds}
    # The keys spelled out in an entry's own label ("h / l", "j / k", "1 / 2 / 3 / 0")
    # count as documented too -- binds only carries what the label doesn't say.
    app = _keymap_app()
    for entry in ot.keymap.KEYS:
        documented |= {ch for ch in entry.label(app) if ch.isalnum() or ch in "$?/+-="}
    missing = sorted(
        bound - documented - {"Y", "y", "d", "D"}
    )  # y/n/d: the price prompt's own modal
    assert not missing, f"undocumented bindings: {missing}"


def test_footer_and_help_cannot_disagree():
    # Both render the same table, so every chip the footer offers is a key the help
    # explains in the very same context -- the invariant the two hand-kept lists broke
    # (the footer used to offer `b mark` and `s sort` inside Trends, which swallows them).
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
    # Sub-contexts inside an overlay are contexts too: a model's session list inside P
    # swallows p/space/Enter/r/s/f, and only backs out or closes.
    app = _keymap_app()
    app.show_prices = True
    app.prices_model = "anthropic/claude-opus-4-8"
    chips = {
        e.id
        for e in ot.keymap.KEYS
        if e.id in ot.keymap.FOOTER_ORDER and e.shown(app) and e.chip_segments(app)
    }
    assert {"price-drill-back", "price-drill-close"} <= chips  # and they reach the footer
    assert not {"prices-view", "prices-pin", "prices-enter", "prices-refresh"} & chips

    # PgDn/PgUp do work inside a Trends ranked-row drill, so they must be listed there.
    app.show_prices = False
    app.prices_model = None
    app.trends = True
    page = next(e for e in ot.keymap.KEYS if e.id == "page")
    assert not page.shown(app)  # ...but not on a Trends chart, which never pages
    app.trend_drill = ("model", "anthropic/claude-opus-4-8")
    assert page.shown(app)


def test_trends_chips_say_what_the_key_will_actually_do():
    # The first Enter on a chart FOCUSES it; only the second drills. Esc backs out of a
    # focused chart (or a drill) before it closes anything. The footer has to say so --
    # the help summary already did, and a chip that disagrees with it is the old bug.
    app = _keymap_app()
    app.trends = True
    enter = next(e for e in ot.keymap.KEYS if e.id == "trends-enter")
    close = next(e for e in ot.keymap.KEYS if e.id == "trends-close")
    assert enter.chip_segments(app) == [("Enter focus", False)]  # Daily, unfocused
    assert close.chip_segments(app) == [("Esc close", False)]
    app.trend_focus = True
    assert enter.chip_segments(app) == [("Enter drill", False)]
    assert close.chip_segments(app) == [("Esc back", False)]  # ...unfocuses, not closes
    app.trend_focus = False
    app.trend_tab = app.trend_tabs.index("Models")  # a ranked tab: Enter opens a row
    assert enter.chip_segments(app) == [("Enter drill", False)]


def test_a_composite_chip_falls_back_to_its_plain_label():
    # Tab still cycles the focus in a zoom, where the yr/mo/day segments don't apply --
    # an empty segment list must not silently drop the key from the footer.
    app = _keymap_app()
    tab = next(e for e in ot.keymap.KEYS if e.id == "tab-focus")
    app.focus = "months"
    assert [t for t, _on in tab.chip_segments(app)][:1] == ["Tab "]  # browse: the panels light
    app.view = "zoom"
    assert tab.shown(app) and tab.chip_segments(app) == [("Tab focus", False)]


def test_help_swallows_a_mistyped_key_and_closes_explicitly():
    # It lists the keys that work here, so it is read WHILE deciding what to press: a
    # mistyped key must not tear it down (the Trends / P convention).
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
