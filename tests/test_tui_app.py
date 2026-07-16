"""The App state machine: views, keys, mouse, filter, bookmarks, ignores, menus (tui/app.py)."""

import os

import opentab as ot

from tests._support import FakeScreen, _app_on_session, _model_row, app_with, screen_text, workflow


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


def test_frame_draws_the_heavy_box_without_hline():
    # Every panel/overlay/modal is framed through this one method (box() adds only the
    # colors and the title, which need a real initscr). The frame is heavy box-drawing,
    # i.e. multibyte -- so it must go through addch/addstr, never hline/vline, whose
    # chtype is a single byte (FakeScreen raises OverflowError there, as curses does).
    renderer = app_with([workflow("a", "2026-06-01 12:00:00")]).renderer
    screen = FakeScreen(height=10, width=20)
    renderer.frame(screen, 0, 0, 4, 12, 0, *renderer._HEAVY_FRAME)
    assert screen_text(screen).splitlines() == [
        "┏━━━━━━━━━━┓",
        "┃          ┃",  # the pane's own rows are painted by the caller
        "┃          ┃",
        "┗━━━━━━━━━━┛",
    ]


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


def test_source_and_demo_switches_do_not_bury_the_notes_warning():
    # Toasts set within one handler collapse onto the last, so a "demo mode" / "source:"
    # notice would swallow the warning that notes.json is broken — and with the map
    # cleared by demo, the notes would then simply look deleted.
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
    # The preview must show the same ROWS the picker will, not just the same columns:
    # under `i` (show ignored) the pickers widen to ranged_workflows, and the previews
    # used to stay on all_workflows -- so an ignored session/project was missing until
    # Enter conjured it back.
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
    # A tab index means nothing outside the scope that produced it: a session's tab 2
    # is Subagents, a month's is Projects. Jumping out of a session used to reinterpret
    # the index against the browse tabs and land on an unrelated tab -- including when
    # the target panel was the one already focused (the carry was skipped entirely).
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
    # lazygit's affordance: the key that jumps to a panel is written in its box
    # title, so the keymap is on screen (and the footer stays about motion).
    # Sidebar top to bottom = 1/2/3, the detail pane on the right = 0.
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


def test_sort_arrows_do_not_cross_lists_in_projects_mode():
    # Projects browse mode shows the project sidebar and a sessions preview at
    # once; each header must arrow its own list's sort (they used to share the
    # context-dependent effective_sort_by, so one list borrowed the other's arrow).
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
    # Browse previews used to show sort arrows on headers that ignored clicks;
    # the drawers now mark the header line so the paint loop registers zones.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.focus = "months"
    rnd = app.renderer
    rnd._line_sort_headers = {}
    lines = rnd.month_workflows(app.selected_month_summary, 100)
    cols, target = rnd._line_sort_headers[0]  # the header is the pane's first line
    assert target == "session"
    assert ("date", "Started") in cols and ("subagents", "Subagents") in cols
    rnd.sort_regions = []
    rnd._register_line_sort_header(5, 2, 0, lines[0], 96)
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
