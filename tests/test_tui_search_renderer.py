from unittest.mock import patch

import opentab as ot
from opentab.tui import bindings
from opentab.tui.search_workspace import SearchWorkspace

from tests._support import AttrScreen, app_with, screen_text


def _app():
    app = app_with([])
    ws = SearchWorkspace(app.args, "all")
    app.conversation_search = ws
    ws.consent = ""
    ws.query = "needle"
    ws.editing = False
    ws.notice = ""
    ws.hits = [
        {
            "session_key": "qualified",
            "title": "Cache investigation",
            "harness": "opencode",
            "project": "/work/opentab",
            "timestamp": "2026-09-12T10:00:00Z",
            "excerpt": "The needle is in this matching passage.",
            "anchor": "match",
        }
    ]
    ws.preview = {
        "records": [
            {
                "id": "match",
                "role": "assistant",
                "timestamp": "2026-09-12T10:00:00Z",
                "parts": [{"text": "This needle explains the actual fix."}],
            }
        ],
    }
    ws.preview_anchor = "match"
    ws.status = {"exists": True, "roots": 10}
    return app, ws


def _paint(app, height=32, width=140):
    screen = AttrScreen(height, width)
    app.renderer.oy = app.renderer.ox = 1
    app.renderer.regions.clear()
    with patch.object(ot.curses, "color_pair", lambda n: n << 8):
        app.renderer.draw_conversation_search(screen, height - 2, width - 2)
    assert all(0 <= y < height - 1 and 0 <= x < width - 1 for y, x in screen.cells)
    return screen


def test_search_wheel_reuses_hit_regions_for_the_pane_under_the_pointer():
    for height, width in ((32, 140), (20, 80)):
        app, ws = _app()
        ws.hits *= 8
        ws.preview["records"][0]["parts"][0]["text"] += "\nline" * 100
        app.help = True  # a covered overlay must not receive search's mouse events
        app._wheel_down = (
            getattr(ot.curses, "BUTTON5_PRESSED", 0) or ot.curses.REPORT_MOUSE_POSITION
        )
        _paint(app, height, width)

        def mouse(kind, state, app=app, height=height, width=width):
            y, x = next(
                (y, x)
                for y in range(height - 2)
                for x in range(width - 2)
                if (app.renderer.hit(y, x) or (None,))[0] == kind
            )
            with patch.object(ot.curses, "getmouse", return_value=(0, x + 1, y + 1, 0, state)):
                assert app.handle_key(None, ot.curses.KEY_MOUSE)

        before = ws.preview_scroll
        mouse("search-preview", app._wheel_down)
        assert ws.preview_scroll == before + 3 and ws.selected == 0 and ws.focus == "results"
        mouse("search-preview", ot.curses.BUTTON4_PRESSED)
        assert ws.preview_scroll == before
        ws.focus = "preview"
        with patch.object(ws, "_request_preview"):
            mouse("search-results", app._wheel_down)
        assert ws.selected == 1 and ws.focus == "preview"
        assert app.help and app.help_scroll == 0
        mouse("search-query", app._wheel_down)
        assert ws.selected == 1 and ws.focus == "preview"
        ws.help = True
        mouse("search-preview", app._wheel_down)
        assert ws.help_scroll == 3 and ws.preview_scroll == 0
        ws.help = False
        ws.consent = "index"
        mouse("search-preview", app._wheel_down)
        assert ws.preview_scroll == 0 and ws.selected == 1
        ws.consent = ""
        ws.reader = True
        _paint(app, height, width)
        mouse("search-preview", app._wheel_down)
        assert ws.preview_scroll == 3 and ws.selected == 1
        mouse("search-query", ot.curses.BUTTON1_CLICKED)
        assert ws.editing and not ws.reader


def test_search_click_selects_cards_and_double_click_opens_reader():
    app, ws = _app()
    ws.hits *= 4
    _paint(app)
    target = next(r for r in app.renderer.regions if r[0] == "search-result" and r[4] == 1)
    with patch.object(ws, "_request_preview"), patch.object(
        ot.curses,
        "getmouse",
        return_value=(0, target[2] + 1, target[1] + 1, 0, ot.curses.BUTTON1_CLICKED),
    ):
        app.handle_key(None, ot.curses.KEY_MOUSE)
    assert ws.selected == 1 and not ws.reader and ws.focus == "results"
    ws.preview = {"records": []}
    with patch.object(
        ot.curses,
        "getmouse",
        return_value=(0, target[2] + 1, target[1] + 1, 0, ot.curses.BUTTON1_DOUBLE_CLICKED),
    ):
        app.handle_key(None, ot.curses.KEY_MOUSE)
    assert ws.reader and ws.selected == 1


def test_reader_footer_has_no_preview_or_focus_action_and_scopes_are_explained():
    app, ws = _app()
    ws.reader = True
    footer = ot.keymap.footer_parts(app)
    labels = [label for segments in footer for label, active in segments]
    assert not any("preview" in label or "focus" in label for label in labels)
    assert any(app.keymap.label("search", "edit") in label for label in labels)
    assert not any(active for segments in footer for _label, active in segments)
    ws.reader = False
    ws.help = True
    descriptions = {
        entry.id: entry.text(app)
        for _title, entries in ot.keymap.sections(app)
        for entry in entries
    }
    assert "selected result's session" in descriptions["search-session"]
    assert "keep other filters" in descriptions["search-all"]
    assert "clear all filters" in descriptions["search-reset"]
    assert "OpenCode / Claude / Codex / all" in descriptions["search-harness"]


def test_search_split_and_stacked_layouts_show_evidence_and_highlights():
    for height, width in ((32, 140), (24, 100), (20, 80)):
        app, ws = _app()
        screen = _paint(app, height, width)
        text = screen_text(screen)
        assert "MATCHES" in text and "MESSAGE PREVIEW" in text
        assert "Cache investigation" in text
        assert "needle" in text
        assert any(attr & ot.curses.A_UNDERLINE for attr in screen.attrs.values())
        assert ws.preview_height > 0 and ws.page_size >= 1


def test_reader_title_anchor_scroll_and_layout_cache():
    app, ws = _app()
    ws.reader = True
    ws.preview["records"].insert(
        0, {"id": "earlier", "role": "user", "parts": [{"text": "before\n" * 30}]}
    )
    ws.preview["records"][1]["parts"][0]["text"] += "\nafter" * 30
    text = screen_text(_paint(app))
    assert "Conversation · Cache investigation" in text
    assert "This needle explains the actual fix." in text
    assert ws.preview_scroll > 0 and ws.preview_anchor is None
    with patch("opentab.tui.renderer.conversation_layout", side_effect=AssertionError("reflow")):
        _paint(app)
    ws.preview_scroll = 99999
    _paint(app)
    assert ws.preview_scroll == ws.preview_lines - ws.preview_height


def test_index_confirmation_is_compact_for_building_and_updating():
    for height, width in ((20, 80), (36, 160)):
        for exists, action in ((False, "Build"), (True, "Update")):
            app, ws = _app()
            ws.consent = "index"
            ws.status = {"exists": exists}
            with patch.object(app.renderer, "draw_modal", wraps=app.renderer.draw_modal) as modal:
                with patch.object(
                    ws, "_make_worker", side_effect=AssertionError("read during paint")
                ):
                    screen = _paint(app, height, width)
            assert modal.call_count == 1 and modal.call_args.kwargs["center"] is True
            text = screen_text(screen)
            assert "Catalog:" not in text and "SEARCH CONVERSATIONS" not in text
            assert f"{action} search index?" in text
            assert f"{action} index" in text and "Cancel" in text
            assert "Cache investigation" not in text and "This needle" not in text
            assert "plaintext" in text and "dates do NOT" in text
            assert any(attr & ot.curses.A_REVERSE for attr in screen.attrs.values())
            xs = [x for _y, x in screen.cells]
            assert min(xs) > 4 and max(xs) < width - 4


def test_search_diagnostics_are_not_an_unqualified_no_results_claim():
    app, ws = _app()
    ws.hits = []
    ws.preview = None
    ws.response = {
        "limited": True,
        "match_mode": "any_term",
        "unindexed_roots": 4,
        "stale_executions_skipped": 2,
    }
    text = screen_text(_paint(app, width=220))
    assert "Matching any search word" in text
    assert "Results limited: add words or filters" in text
    assert "4 sessions not searchable yet" in text
    assert "Changed or unavailable text skipped" in text
    assert "No matches in searchable text" in text
    assert "I: update the search index" in text
    assert not any(term in text for term in ("roots", "withheld", "Bounded", "Catalog:"))


def test_search_coverage_warning_keeps_update_action_visible_at_minimum_size():
    app, ws = _app()
    app.keymap = bindings.Keymap({("search", "index"): ["U"]})
    for editing in (False, True):
        ws.editing = editing
        for response, warning in (
            ({"unindexed_roots": 1}, "1 session not searchable yet"),
            ({"stale_metadata_roots_skipped": 2}, "Changed or unavailable text skipped"),
            ({"stale_executions_skipped": 2}, "Changed or unavailable text skipped"),
        ):
            ws.response = response
            text = screen_text(_paint(app, 20, 80))
            assert warning in text
            assert "U: update the search index" in text
            assert ("Tab, then" in text) == editing
    ws.busy = "index"
    ws.notice = "Updating the search index on disk."
    text = screen_text(_paint(app, 20, 80))
    assert "Updating index" in text and "U: update" not in text
    ws.busy = ""
    ws.reader = True
    ws.notice = "No later saved messages."
    text = screen_text(_paint(app, 20, 80))
    assert ws.notice in text and "U: update" not in text


def test_search_index_status_and_reader_help_use_user_facing_terms():
    app, ws = _app()
    text = screen_text(_paint(app, 20, 80))
    assert "10 sessions indexed" in text and "Catalog:" not in text
    ws.reader = True
    descriptions = {
        entry.id: entry.text(app)
        for _title, entries in ot.keymap.sections(app)
        for entry in entries
    }
    assert descriptions["search-window"] == "read earlier / later messages or remaining text"
    assert "plaintext" in descriptions["search-index"]


def test_search_rendering_sanitizes_untrusted_metadata_and_query():
    app, ws = _app()
    ws.query = "needle\x1b[31m"
    ws.scope["project"] = "/x\x1b[2J"
    ws.hits[0]["title"] = "Hostile\x1b[2Jtitle"
    ws.error = "bad\x07error"
    text = screen_text(_paint(app))
    assert "\x1b" not in text and "\x07" not in text
    assert "Hostiletitle" in text


def test_search_tabs_and_labeled_filters_keep_geometry_at_supported_widths():
    for height, width in ((20, 80), (32, 140)):
        app, ws = _app()
        ws.hits = []
        screen = _paint(app, height, width)
        text = screen_text(screen)
        assert "[Results]" in text and "Conversation" in text
        assert "Cache investigation" not in text.splitlines()[0]
        assert not any(r[0] == "searchtab" and r[4] == 1 for r in app.renderer.regions)
        filters = [r for r in app.renderer.regions if r[0] == "searchfilter"]
        assert [r[4] for r in filters] == list(range(5))
        assert all(app.renderer.hit(r[1], r[2]) == ("searchfilter", r[4]) for r in filters)
        for label in ("Scope:", "Harness:", "Project:", "Dates:", "Reset"):
            assert label in text
        assert ws.page_size >= 1 and ws.preview_height >= 2

        app, ws = _app()
        ws.reader = True
        screen = _paint(app, height, width)
        text = screen_text(screen)
        assert "[Conversation · Cache investigation]" in text
        tab = next(r for r in app.renderer.regions if r[0] == "searchtab" and r[4] == 1)
        assert app.renderer.hit(tab[1], tab[2]) == ("searchtab", 1)
        assert any(
            screen.attrs.get((tab[1] + 1, x + 1), 0) & ot.curses.A_BOLD
            for x in range(tab[2], tab[3] + 1)
        )


def test_search_filter_picker_reuses_modal_and_exposes_only_enabled_option_rows():
    app, ws = _app()
    ws.filter_menu = "harness"
    ws.filter_menu_index = 1
    ws.filter_options = lambda: [
        ("all", "All harnesses", True),
        ("opencode", "Open\x07Code", True),
        ("remote", "Remote only", False),
    ]
    with patch.object(app.renderer, "draw_modal", wraps=app.renderer.draw_modal) as modal:
        screen = _paint(app, 20, 80)
    assert modal.call_count == 1
    text = screen_text(screen)
    assert "Filter harness" in text and "All harnesses" in text and "Open Code" in text
    assert "Remote only  (unavailable)" in text and "\x07" not in text
    regions = [r for r in app.renderer.regions if r[0] == "searchfilter-option"]
    assert [r[4] for r in regions] == [0, 1]
    assert all(app.renderer.hit(r[1], r[2]) == ("searchfilter-option", r[4]) for r in regions)
    assert not any(
        r[0] in {"searchtab", "searchfilter", "search-result"} for r in app.renderer.regions
    )


def test_search_mouse_tabs_filters_and_reset_use_workspace_actions():
    for height, width in ((20, 80), (32, 140)):
        app, ws = _app()
        app._wheel_down = (
            getattr(ot.curses, "BUTTON5_PRESSED", 0) or ot.curses.REPORT_MOUSE_POSITION
        )

        def click(kind, index, app=app, height=height, width=width):
            _paint(app, height, width)
            region = next(r for r in app.renderer.regions if r[0] == kind and r[4] == index)
            with patch.object(
                ot.curses,
                "getmouse",
                return_value=(0, region[2] + 1, region[1] + 1, 0, ot.curses.BUTTON1_CLICKED),
            ):
                app.handle_key(None, ot.curses.KEY_MOUSE)

        ws.editing = True
        click("searchtab", 1)
        assert ws.reader and not ws.editing
        ws.preview_scroll = 2
        click("searchtab", 0)
        assert not ws.reader
        click("searchtab", 1)
        assert ws.reader and ws.preview_scroll == 2
        click("searchtab", 0)
        ws.scope["project"] = "/unchanged"
        click("searchfilter", 1)
        assert ws.filter_menu == "harness"
        with patch.object(app, "launch_current") as launch:
            app.handle_key(None, ord("L"))
        launch.assert_not_called()
        click("searchfilter-option", 2)
        assert ws.scope == {"project": "/unchanged", "harness": "claude"}
        assert not ws.filter_menu and ws.query == "needle"
        click("searchfilter", 4)
        assert ws.scope == {} and ws.query == "needle"
        click("searchfilter", 0)
        assert ws.filter_options()[1][2] is False  # reset cleared stale hits
        with patch.object(ot.curses, "getmouse", return_value=(0, 2, 3, 0, app._wheel_down)):
            app.handle_key(None, ot.curses.KEY_MOUSE)
        assert ws.filter_menu_index == 1 and ws.selected == 0
        app.handle_key(None, 10)
        assert ws.filter_menu == "scope" and ws.scope == {}
        app.handle_key(None, 27)
        click("searchfilter", 2)
        assert ws.filter_field == "project"
        app.handle_key(None, 27)
        click("searchfilter", 3)
        assert ws.filter_field == "date"


def test_search_filter_menu_and_footer_use_shared_remapped_menu_keys():
    app, ws = _app()
    app.keymap = bindings.Keymap({("menu", "select"): ["v"], ("menu", "cancel"): ["z"]})
    ws.open_filter("scope")
    text = screen_text(_paint(app, 20, 80))
    assert "v" in text and "z" in text
    assert ot.keymap.footer_parts(app) == [[("v apply", False)], [("z cancel", False)]]
    app.handle_key(None, ord("z"))
    assert ws.filter_menu == ""


def test_search_help_clears_underlying_mouse_regions():
    app, ws = _app()
    ws.help = True
    _paint(app)
    assert app.renderer.regions == []


def test_short_preview_keeps_exact_anchor_at_top_across_idle_paints():
    app, ws = _app()
    ws.preview["limitations"] = ["First limitation", "Second limitation", "Third limitation"]
    first = _paint(app)
    start = ws.preview_scroll
    assert start > 0
    second = _paint(app)
    assert ws.preview_scroll == start
    assert "Note:" not in screen_text(first)
    assert "This needle explains" in screen_text(second)


def test_short_preview_keeps_its_anchor_position_after_round_trip_through_reader():
    app, ws = _app()
    _paint(app, 36, 160)
    before = ws.preview_scroll
    assert before > 0
    ws.switch_view("conversation")
    _paint(app, 36, 160)
    ws.switch_view("results")
    _paint(app, 36, 160)
    assert ws.preview_scroll == before


def test_search_help_scrolls_at_minimum_size_and_close_releases_raw_layout():
    app, ws = _app()
    _paint(app)
    assert app.renderer._search_layout_cache is not None
    ws.handle_key(ord("?"), bindings.DEFAULT)
    _paint(app, 20, 80)
    for _ in range(15):
        ws.handle_key(ot.curses.KEY_DOWN, bindings.DEFAULT)
    text = screen_text(_paint(app, 20, 80))
    assert "Keys" in text and "Navigation" in text
    assert ws.help_scroll > 0
    app._close_conversation_search()
    assert app.renderer._search_layout_cache is None


def test_search_reuses_centered_footer_and_styled_help_without_underlying_actions():
    app, ws = _app()
    app.help = True
    app.help_scroll = 71
    app.filter_active = True
    app.query = "underlying-filter"
    with patch.object(app.renderer, "draw_keybar", wraps=app.renderer.draw_keybar) as keybar:
        screen = _paint(app, 36, 160)
    assert keybar.call_count == 1
    footer = "".join(screen.cells.get((34, x), " ") for x in range(160))
    assert "J/K preview" in footer and "? help" in footer
    assert footer.index("Enter read") > 3
    assert "underlying-filter" not in screen_text(screen)
    assert "demo" not in footer and "range" not in footer
    ws.handle_key(ord("?"), bindings.DEFAULT)
    with patch.object(app.renderer, "draw_help", wraps=app.renderer.draw_help) as help_popup:
        screen = _paint(app, 36, 160)
    assert help_popup.call_count == 1
    text = screen_text(screen)
    assert "Keys" in text and "Conversation search" in text
    assert "scroll preview without moving" in text
    assert "Search keys" not in text and "Operators are not query syntax" not in text
    assert app.help and app.help_scroll == 71
    assert any(attr == ((2 << 8) | ot.curses.A_BOLD) for attr in screen.attrs.values())
