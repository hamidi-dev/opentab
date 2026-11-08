import os
from unittest.mock import patch

import opentab as ot
from opentab.tui import bindings, keymap

from tests._support import (
    FakeScreen,
    _select_session,
    app_with,
    isolated_annotations,
    screen_text,
    workflow,
)

BOTH = {"version": 1, "tags": {"enabled": True}, "work_items": {"enabled": True}}


def _app():
    app = _select_session(
        app_with([workflow("a", "2026-06-01 12:00:00", title="Investigate")]), "a"
    )
    app.can_switch_source = lambda: False
    app.refresh_tags()
    return app


def _help(app):
    return {
        entry.id: entry.label(app) for _title, entries in keymap.sections(app) for entry in entries
    }


def test_org_toggle_preserves_selection_and_is_transient():
    with isolated_annotations(BOTH):
        app = _app()
        app.scroll = 7
        before = (app.view, app.tab, app.scroll, app.current_session().id)
        assert not app.org_mode and "org-toggle" in _help(app)
        # Tags are main-keymap commands; Org mode only re-labels them.
        assert "work" not in _help(app) and _help(app)["tags"] == "^T"
        app.handle_key(None, ord("O"))
        assert app.org_active and app.work_workspace is None
        assert (app.view, app.tab, app.scroll, app.current_session().id) == before
        assert _help(app)["work"] == "w" and _help(app)["tags"] == "t"
        assert "ORG" in keymap.context_label(app)
        assert "sort" not in _help(app) or _help(app)["sort"] == "S"
        assert keymap.BY_ID["whatif"].label(app) == ""
        assert keymap.BY_ID["org-toggle"] in keymap.footer_entries(app)
        assert not _app().org_mode
        app.handle_key(None, ord("O"))
        assert not app.org_active and keymap.BY_ID["whatif"].label(app) == "w"
        assert (app.view, app.tab, app.scroll, app.current_session().id) == before


def test_org_work_create_state_attach_and_work_exit_dispatch():
    with isolated_annotations(BOTH):
        app = _app()
        app.handle_key(None, ord("O"))
        app.handle_key(None, ord("c"))
        ws = app.work_workspace
        assert ws.dialog == "create" and ws.draft == "Investigate"
        app.handle_key(None, ord("O"))
        assert app.org_active and ws.draft == "InvestigateO"  # title owns input
        app.handle_key(None, 10)
        item_id = ws.item_id
        app.handle_key(None, ord("?"))
        assert app.help and "org-toggle" in _help(app)
        app.handle_key(None, 27)
        app.handle_key(None, ord("O"))
        assert not app.org_active and app.work_workspace is ws
        app.handle_key(None, 27)  # back to list
        app.handle_key(None, 27)  # back to selected session
        assert app.work_workspace is None
        app.handle_key(None, ord("O"))
        app.handle_key(None, ord("s"))
        ws = app.work_workspace
        assert ws.item_id == item_id and ws.dialog == "state" and not app.sort_menu
        app.handle_key(None, 27)
        app.handle_key(None, 27)
        app.handle_key(None, 27)
        app.handle_key(None, ord("a"))
        assert app.work_workspace.dialog == "assign"


def test_org_feature_gates_and_unshadowed_normal_commands():
    # Org mode exists only for work items; tags alone never expose it.
    for config, tags, work in (
        ({"version": 1, "tags": {"enabled": False}}, False, False),
        ({"version": 1}, True, False),
        ({"version": 1, "tags": {"enabled": False}, "work_items": {"enabled": True}}, False, True),
        (BOTH, True, True),
    ):
        with isolated_annotations(config):
            app = _app()
            app.handle_key(None, ord("O"))
            assert app.org_active == work
            assert ("org-toggle" in _help(app)) == work
            assert ("tags" in _help(app)) == tags
            assert ("work" in _help(app)) == work
            with patch.object(app, "set_all_time") as all_time, patch.object(
                app, "open_work"
            ) as open_work:
                app.handle_key(None, ord("a"))
                assert open_work.called == work and all_time.called == (not work)
            with patch.object(app, "open_tag_editor") as edit, patch.object(
                app, "set_browse_mode"
            ) as mode:
                app.handle_key(None, ord("t"))
                # Bare t is only Org mode's alias; outside it t stays the time view.
                assert edit.called == (tags and work) and mode.called == (not (tags and work))
            app.handle_key(None, ord("?"))
            assert app.help
            app.handle_key(None, ord("O"))
            assert app.org_active == work  # Help swallows browsing keys


def test_org_demo_no_state_and_disabled_do_not_claim_keys():
    with isolated_annotations(BOTH):
        for blocked in ("demo", "no-state"):
            app = _app()
            if blocked == "demo":
                app.store.demo = True
            else:
                app.notes_enabled = False
            app.handle_key(None, ord("O"))
            assert not app.org_active and "org-toggle" not in _help(app)
            with patch.object(app, "set_all_time") as action:
                app.handle_key(None, ord("a"))
                assert action.called


def test_org_remapping_precedence_labels_and_unbinding():
    with isolated_annotations(BOTH) as tmp:
        app = _app()
        path = os.path.join(tmp, "config", "opentab", "keymap.conf")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[org]\ntoggle = ö\nwork = j\nwork_state =\n")
        app.keymap = bindings.load_user_keymap(path)
        assert not app.keymap.warnings
        app.handle_key(None, ord("O"))
        assert not app.org_active
        app.handle_key(None, "ö")
        assert app.org_active and _help(app)["org-toggle"] == "ö"
        assert _help(app)["work"] == "j" and "work-state" not in _help(app)
        with patch.object(app, "open_work") as open_work:
            app.handle_key(None, ord("j"))
            assert open_work.called
        app.handle_key(None, "ö")
        assert not app.org_active


def test_org_filters_tag_editor_trace_and_trace_picker_precedence():
    with isolated_annotations(BOTH):
        app = _app()
        app.handle_key(None, ord("O"))
        app.handle_key(None, ord("t"))
        assert app.tag_menu
        app.handle_key(None, ord("O"))
        assert app.org_active and app.tag_menu_query == "O"
        app.handle_key(None, 27)
        app.filter_active, app._filter_before = True, ""
        app.handle_key(None, ord("O"))
        assert app.query == "O" and app.org_active
        app.handle_key(None, 27)
        with patch.object(app, "_on_turns_tab", return_value=True), patch.object(
            type(app), "active_trace_drill", new_callable=lambda: property(lambda _self: 0)
        ), patch.object(app, "copy_trace_call") as copy:
            app.handle_key(None, ord("O"))
            assert app.org_active and copy.call_args.kwargs["part"] == "output"
        app._trace_call_menu = []
        with patch.object(app, "handle_trace_call_menu_key", return_value=True) as picker:
            app.handle_key(None, ord("O"))
            assert picker.called and app.org_active


def test_org_prices_and_trends_do_not_target_covered_session():
    with isolated_annotations(BOTH):
        app = _app()
        app.open_prices()
        app.handle_key(None, ord("O"))
        assert app.org_active and app.overlay_top == "prices" and app.bookmark_target() is None
        assert "work-from-session" not in _help(app)
        app.handle_key(None, ord("O"))
        assert not app.org_active and app.overlay_top == "prices"
        app.show_prices = False
        app.open_trends()
        app.handle_key(None, ord("O"))
        assert app.org_active and app.bookmark_target() is None
        app.handle_key(None, ord("w"))
        assert app.work_workspace is not None and app.trends
        app.handle_key(None, ord("O"))
        assert not app.org_active and app.work_workspace is not None


def test_org_indicator_survives_narrow_header_and_work_dialog():
    with isolated_annotations(BOTH):
        app = _app()
        app.handle_key(None, ord("O"))
        with patch.object(ot.curses, "color_pair", return_value=0):
            for width in (30, 80):
                screen = FakeScreen(24, width)
                app.renderer.draw_header(screen, width)
                assert "ORG" in screen_text(screen)
            app.handle_key(None, ord("c"))
            screen = FakeScreen(24, 80)
            app.renderer.draw_work(screen, 24, 80)
            assert "ORG" in screen_text(screen)


def test_org_session_detail_and_normal_compatibility_dispatch():
    with isolated_annotations(BOTH):
        app = _app()
        app.drill_in()
        assert app.view == "session"
        before = (app.view, app.tab, app.workflow_index, app.scroll)
        with patch.object(app, "open_sort_menu") as sort, patch.object(
            app, "toggle_whatif"
        ) as whatif:
            app.handle_key(None, ord("s"))
            assert sort.called
            app.handle_key(None, ord("w"))
            assert whatif.called
        app.handle_key(None, ord("O"))
        assert app.org_active and (app.view, app.tab, app.workflow_index, app.scroll) == before
        app.handle_key(None, ord("O"))
        app.handle_key(None, 14)
        assert not app.org_active and app.work_workspace.dialog == "create"


def test_org_trends_session_drill_targets_its_cursor_and_prices_keeps_sort():
    with isolated_annotations(BOTH):
        app = _app()
        app.open_trends()
        app.trend_drill = ("project", "/tmp/project")
        target = workflow("b", "2026-06-01 13:00:00", title="Trends target")
        with patch.object(app, "trend_drill_sessions", return_value=[(target, 1, 100)]):
            app.handle_key(None, ord("O"))
            assert app.bookmark_target() is target
            assert _help(app)["tags"] == "t"
            with patch.object(app, "open_tag_editor") as edit:
                app.handle_key(None, ord("t"))
                assert edit.called
            assert app._tag_filter_base() == [target]
        app.trends = False
        app.open_prices()
        with patch.object(app, "open_sort_menu") as sort:
            app.handle_key(None, ord("s"))
            assert sort.called and app.work_workspace is None


def test_work_help_owns_mouse_and_nested_release_notes_paint_above_it():
    with isolated_annotations(BOTH):
        app = _app()
        app.handle_key(None, ord("O"))
        app.handle_key(None, ord("w"))
        ws = app.work_workspace
        app.handle_key(None, ord("?"))
        app._wheel_down = (
            getattr(ot.curses, "BUTTON5_PRESSED", 0) or ot.curses.REPORT_MOUSE_POSITION
        )
        with patch.object(ws, "move_cursor") as move, patch.object(
            ws, "apply"
        ) as apply, patch.object(ot.curses, "getmouse", return_value=(0, 0, 0, 0, app._wheel_down)):
            app.handle_mouse()
            assert app.help_scroll == 3 and not move.called
            with patch.object(
                ot.curses, "getmouse", return_value=(0, 0, 0, 0, ot.curses.BUTTON1_DOUBLE_CLICKED)
            ):
                app.handle_mouse()
                assert not app.help and not apply.called and app.work_workspace is ws
        app.handle_key(None, ord("?"))
        app.handle_key(None, ord("W"))
        assert app.whats_new and app.help
        painted = []
        screen = FakeScreen(30, 100)
        screen.erase = screen.cells.clear
        screen.refresh = lambda: None
        with patch.object(ot.curses, "color_pair", return_value=0), patch.object(
            app.renderer, "draw_work", side_effect=lambda *args: painted.append("work")
        ), patch.object(
            app.renderer, "draw_help", side_effect=lambda *args: painted.append("help")
        ), patch.object(
            app.renderer, "draw_whats_new", side_effect=lambda *args: painted.append("notes")
        ):
            app.renderer.draw(screen)
        assert painted == ["work", "help", "notes"]
        app.handle_key(None, 27)
        assert not app.whats_new and app.help
        app.handle_key(None, 27)
        assert not app.help and app.work_workspace is ws
