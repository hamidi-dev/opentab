import csv
import io
import json
from unittest.mock import patch

from opentab.accounting.models import SessionRef
from opentab.tui import app as app_module
from opentab.tui import bindings, exporting, keymap
from opentab.tui.views.tags import tag_drill_summary, tag_suggestion_lines

from tests._support import _ids, _tag_app, _tags_on, _type, set_tags_pref

TAB, ENTER, ESC, DOWN = 9, 10, 27, 258


def _fixture():
    app = _tag_app()
    app.view = "zoom"
    app.tab = app.current_tabs().index("Sessions")
    rows = {w.id: w for w in app.current_sessions()}
    a, b = app.session_tag_key(rows["a"]), app.session_tag_key(rows["b"])
    app.tags.set_tags(a, (), 0, create=["X", "Y"])
    x, y = app.tags.tag_ids(a)
    app.tags.set_tags(b, (y,), 0)
    rows["a"].worked_seconds = 3600
    rows["b"].worked_seconds = None
    return app, rows, a, b, x, y


def _highlight(app, key):
    app.workflow_index = next(
        i for i, row in enumerate(app.current_sessions()) if app.session_tag_key(row) == key
    )


def _edit(app, key):
    _highlight(app, key)
    app.open_tag_editor()
    assert app.tag_menu == "edit"


def _tag_index(app, tag_id):
    app.tag_menu_index = next(
        i for i, entry in enumerate(app.tag_menu_entries()) if entry[1] == tag_id
    )


def test_hash_tokens_filter_all_any_with_root_summary_and_unchanged_parents():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        before = app.months[0].cost
        aggregates = {row.tag_id: row for row in app.tag_browse_rows()}
        assert aggregates[x].cost == 5 and aggregates[y].cost == 8
        assert aggregates[y].sessions == 2 and aggregates[y].time_measured == 1
        assert aggregates[y].agent_seconds == 3600
        assert aggregates[None].session_keys == (app.session_tag_key(rows["c"]),)
        y_line, x_line = tag_suggestion_lines([("#Y", aggregates[y]), ("#X", aggregates[x])], 64)
        assert "(1/2)" in y_line and "(" not in x_line and len(y_line) == len(x_line)

        app.query = "#x #y"
        assert _ids(app) == ["a"]
        assert sum(w.total_cost for w in app.current_sessions()) == 5
        app.query = "#x|y"
        assert sorted(_ids(app)) == ["a", "b"]
        assert sum(w.total_cost for w in app.current_sessions()) == 8
        # Tags narrow session lists only: parents and fuzzy text keep their meaning.
        assert app.months[0].cost == before
        app.query = "#y Beta"
        assert _ids(app) == ["b"] and app.filter_text == "Beta"
        app.query = "#x|y"
        (summary,) = tag_drill_summary(app.current_sessions(), "recorded")
        assert "2 sessions" in summary and "$8.00" in summary and "(1/2)" in summary
        (narrow,) = tag_drill_summary(app.current_sessions(), "recorded", 30)
        assert "$8.00" in narrow and len(narrow) <= 30
        app.loaded.append(rows["a"])
        app._invalidate_workflow_cache()
        assert len(app.current_sessions()) == 2
        assert sum(w.total_cost for w in app.current_sessions()) == 8
        app.query = "#nope"
        assert _ids(app) == [] and "#nope" in app.renderer.no_sessions_text()
        app.query = "#untagged"
        assert _ids(app) == ["c"]


def test_filter_line_completes_tags_with_scoped_totals():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        app.tags.rename(y, "client-y")
        app.handle_key(None, ord("/"))
        assert app.filter_active and app.tag_suggestions() == []
        _type(app, "#")
        # Most-used in scope first, Untagged last.
        assert [row.label for row in app.tag_suggestions()] == ["client-y", "X", "Untagged"]
        app.handle_key(None, 21)
        _type(app, "fix #")  # nothing matches "fix": zero-use tags stay alphabetical
        assert [row.label for row in app.tag_suggestions()] == ["client-y", "X", "Untagged"]
        _type(app, "c")
        assert [row.label for row in app.tag_suggestions()] == ["client-y"]
        app.handle_key(None, TAB)
        assert app.query == "fix #client-y " and app.tag_suggestions() == []
        _type(app, "#")
        app.handle_key(None, DOWN)  # Down walks the suggestions, not the list
        assert app.tag_suggest_index == 1 and app.workflow_index == 0
        app.handle_key(None, TAB)
        assert app.query == "fix #client-y #X "
        app.handle_key(None, ENTER)
        assert not app.filter_active and app.query.startswith("fix")
        # With tags off, # is ordinary fuzzy text and nothing completes.
        app.notes_enabled = False
        app.handle_key(None, ord("/"))
        _type(app, "#")
        assert app.tag_suggestions() == [] and app.filter_text.endswith("#")


def test_tag_browse_respects_partition_date_ignore_scope_and_cost_mode_without_detail_reads():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        # Scoped metadata is already loaded. No node/timeline/conversation queries.
        with patch.object(
            app.store, "workflow_nodes", side_effect=AssertionError("detail read"), create=True
        ):
            app.zoom_source = "Codex"
            aggregates = {row.tag_id: row for row in app.tag_browse_rows()}
            assert aggregates[y].cost == 3 and aggregates[y].session_keys == (b,)
            assert aggregates[x].sessions == 0
            app.zoom_source = None
            app.ignored_sessions.add("a")
            app._invalidate_workflow_cache()
            assert {row.tag_id: row for row in app.tag_browse_rows()}[y].cost == 3
            app.ignored_sessions.clear()
            app._invalidate_workflow_cache()
            app.set_range_from_text("2026-06-02..2026-06-03")
            assert all(row.sessions == 0 for row in app.tag_browse_rows())


def test_bulk_captures_exact_roots_and_preserves_unrelated_memberships_after_sort_filter():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        _highlight(app, b)
        app.handle_key(None, 331)  # Insert selects explicitly
        captured = dict(app.tag_selected)
        app.query = "Alpha"  # selected B is hidden, not silently replaced by A
        app.sort_reverse = True
        app.handle_key(None, 2)  # Ctrl-B add tags
        assert app._tag_bulk_targets == captured
        _tag_index(app, x)
        app.handle_key(None, TAB)
        app.handle_key(None, ENTER)
        assert set(app.tags.tag_ids(b)) == {x, y}
        assert set(app.tags.tag_ids(a)) == {x, y}
        assert not app.tag_selected
        app.query = ""
        _highlight(app, b)
        app.toggle_tag_selection()
        app.open_bulk_tags(remove=True)
        _tag_index(app, x)
        app.handle_key(None, TAB)
        app.handle_key(None, ENTER)
        assert app.tags.tag_ids(b) == (y,) and set(app.tags.tag_ids(a)) == {x, y}


def test_bulk_stale_batch_rejects_all_and_keeps_draft_and_captured_revisions():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        for key in (a, b):
            _highlight(app, key)
            app.toggle_tag_selection()
        captured = dict(app.tag_selected)
        app.open_bulk_tags(remove=True)
        _tag_index(app, y)
        app.handle_key(None, TAB)
        # A second frontend changes B after selection, without touching A.
        other = _tag_app("b")
        other.tags.set_tags(b, (x, y), other.tags.revision(b))
        app.handle_key(None, ENTER)
        assert app.tag_menu == "bulk_remove" and "Not saved" in app.tag_menu_error
        assert app._tag_bulk_targets == captured and app.tag_menu_checked == [y]
        app.tags.refresh()
        assert app.tags.tag_ids(a) == (x, y) and set(app.tags.tag_ids(b)) == {x, y}
        app.handle_key(None, ENTER)  # no implicit retry with upgraded revisions
        assert app.tag_menu == "bulk_remove"


def test_bulk_missing_or_ambiguous_selected_identity_refuses_without_retargeting():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        _highlight(app, a)
        app.toggle_tag_selection()
        app.open_bulk_tags(remove=True)
        app.tag_menu_checked = [x]
        app.loaded.append(rows["a"])
        app.handle_key(None, ENTER)
        assert "ambiguous" in app.tag_menu_error and app.tags.tag_ids(a) == (x, y)


def test_tag_rename_delete_from_the_editor_show_retained_count_and_preserve_detail():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        missing = SessionRef("retained-host", "codex", "missing").encode()
        app.tags.set_tags(missing, (y,), 0)
        _edit(app, a)
        _tag_index(app, y)
        app.handle_key(None, 5)  # Ctrl-E rename
        assert app.tag_menu == "rename" and app._tag_maintenance[3] == 3
        app.handle_key(None, 21)
        _type(app, "Y-renamed")
        app.handle_key(None, "界")
        app.handle_key(None, ENTER)
        assert app.tags.label(y) == "Y-renamed界" and app.tag_menu == "edit"
        assert app.tags.tag_ids(missing) == (y,)
        _tag_index(app, y)
        app.handle_key(None, 4)  # Ctrl-D global delete confirmation
        assert app.tag_menu == "delete" and app._tag_maintenance[3] == 3
        app.handle_key(None, ESC)  # back to the editor, nothing deleted
        assert app.tag_menu == "edit" and app.tags.label(y) is not None
        app.handle_key(None, ESC)
        app.query = "#y-renamed界"
        _highlight(app, b)
        app.view = "session"
        app.open_tag_editor()
        _tag_index(app, y)
        app.handle_key(None, 4)
        app.handle_key(None, ENTER)
        assert app.tags.label(y) is None and app.tags.tag_ids(missing) == ()
        assert app.tag_menu == "edit" and y not in app.tag_menu_checked
        assert app.current_session() is rows["b"]
        app.handle_key(None, ENTER)  # the editor's own save is not stale after the delete
        assert app.tag_menu is None and "Not saved" not in app.tag_menu_error
        app.drill_out()
        assert _ids(app) == [] and app.tag_filter is not None


def test_tag_delete_stale_confirmation_never_silently_updates_count():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        _edit(app, a)
        _tag_index(app, x)
        app.handle_key(None, 4)
        target = app._tag_maintenance
        other = _tag_app("b")
        other.tags.set_tags(b, (x, y), other.tags.revision(b))
        app.handle_key(None, ENTER)
        assert app.tag_menu == "delete" and "Not saved" in app.tag_menu_error
        assert app._tag_maintenance == target
        assert app.tags.label(x) == "X"


def test_annotated_csv_uses_exact_displayed_order_safe_json_and_unknown_time_blank():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        app.tags.rename(y, "=label,with-commas界")
        app.query = "#=label,with-commas界"
        app.notes = {"a": "=note", "b": "ordinary note"}
        ordinary = app._export_dataset()
        annotated = app._annotated_export_dataset()
        assert "tags" not in ordinary[1] and "agent_seconds" not in ordinary[1]
        assert annotated[2] == [
            [*original, *extra[len(ordinary[1]) :]]
            for original, extra in zip(ordinary[2], annotated[2])
        ]
        assert [r[0] for r in annotated[2]] == _ids(app)
        for row, workflow in zip(annotated[2], app.current_sessions()):
            assert row[-4] == app.session_tag_key(workflow)
            tags = json.loads(row[-3])
            assert [t["id"] for t in tags] == sorted(app.session_tag_ids(workflow))
            assert row[-2] == (3600 if workflow.id == "a" else "")
        encoded = io.StringIO(newline="")
        csv.writer(encoded).writerows(
            [[exporting.csv_safe(cell) for cell in row] for row in annotated[2]]
        )
        parsed = list(csv.reader(io.StringIO(encoded.getvalue())))
        assert json.loads(parsed[0][-3]) == json.loads(annotated[2][0][-3])
        assert next(r for r in parsed if r[0] == "a")[ordinary[1].index("note")] == "'=note"
        app.notes_enabled = False
        assert not app.annotated_csv_available()
        with patch.object(exporting, "write_csv") as write:
            app.export_annotated()
            write.assert_not_called()


def test_tag_suggestions_narrow_unicode_layout_and_remapped_actions():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        app.tags.rename(y, "界" * 40)
        app.keymap = bindings.Keymap({("filter", "complete"): ["ctrl-o"]})
        app.filter_active = True
        app.query = "#"
        written = []
        with patch.object(app.renderer, "box"), patch.object(
            app_module.curses, "color_pair", return_value=0
        ), patch.object(
            app.renderer, "write", side_effect=lambda _s, _y, _x, text, *_a: written.append(text)
        ):
            assert app.renderer.draw_tag_suggestions(None, 24, 50)
        rows_text = [t for t in written if t.startswith("#")]
        assert len(rows_text) == 3 and all(len(t) <= 46 for t in rows_text)
        assert "#untagged" in rows_text[-1]
        assert any("$" in t and "session" in t for t in rows_text)
        assert not any("TOTAL" in t or "measured" in t for t in written)
        app.handle_key(None, 15)  # remapped completion takes the most-used tag
        assert app.query == "#" + "界" * 40 + " "
        app.filter_active = False
        app.notes_enabled = False
        for name in ("tag-select", "tag-bulk-add", "export-annotated"):
            assert not keymap.BY_ID[name].shown(app)


def test_tag_accounting_cost_mode_uses_existing_root_snapshots_not_session_whatif():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        rows["a"].api_total_cost = 50
        rows["b"].api_total_cost = 30
        app.show_api_prices = False
        app._apply_price_mode()
        with patch.object(app, "_ensure_models"), patch.object(
            app.store, "model_breakdown", side_effect=AssertionError("unexpected scan")
        ):
            app.handle_key(None, ord("$"))
        assert app.show_api_prices
        assert {row.tag_id: row for row in app.tag_browse_rows()}[y].cost == 80
        app.whatif_model = "arbitrary comparison"
        assert {row.tag_id: row for row in app.tag_browse_rows()}[y].cost == 80
        # Unknown cost is not a measured zero; known zero Agent time is measured.
        rows["b"].total_cost = 0
        rows["b"].unpriced_tokens = 100
        rows["b"].worked_seconds = 0
        total = {row.tag_id: row for row in app.tag_browse_rows()}[y]
        assert total.cost == 50 and total.cost_measured == 1 and total.time_measured == 2


def test_bulk_and_maintenance_recheck_disabled_capability_after_opening():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        _highlight(app, a)
        app.toggle_tag_selection()
        app.open_bulk_tags(remove=True)
        app.tag_menu_checked = [x]
        set_tags_pref(False)
        app.handle_key(None, ENTER)
        assert "Not saved" in app.tag_menu_error and app.tag_menu == "bulk_remove"
        app.refresh_tags()
        assert not app.tag_selected and app.tag_menu is None


def test_annotated_csv_prices_and_trends_drills_preserve_exact_contribution_rows():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        app._model_by_root = {
            "a": [{"model_name": "openai/gpt-5.3", "cost": 2, "tokens_total": 100}],
            "b": [{"model_name": "openai/gpt-5.3", "cost": 1, "tokens_total": 50}],
        }
        app.query = "#x"
        app.show_prices = True
        app.prices_model = "gpt-5.3"
        scope, header, data = app._annotated_export_dataset()
        assert scope == "prices-sessions-annotated"
        assert [row[0] for row in data] == ["a"]
        assert data[0][header.index("displayed_cost")] == 2
        assert data[0][header.index("total_cost")] == 5
        with patch.object(exporting, "write_csv") as write:
            app.handle_key(None, ord("E"))
            assert write.call_args.args[2] == data
        app.show_prices = False
        app.trends = True
        app.query = "#y"
        app.trend_drill = ("source", "Codex")
        scope, header, data = app._annotated_export_dataset()
        assert scope == "trends-sessions-annotated" and [row[0] for row in data] == ["b"]
        with patch.object(exporting, "write_csv") as write:
            app.handle_key(None, ord("E"))
            assert write.call_args.args[2] == data
        app.trend_drill = ("model", "openai/gpt-5.3")
        app.trend_drill_tab = 0  # Economics is not a session export choice.
        assert not app.annotated_csv_available()


def test_tag_editor_mouse_uses_modal_rows_and_never_clicks_through():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        _edit(app, a)
        index = next(i for i, entry in enumerate(app.tag_menu_entries()) if entry[1] == x)
        app.renderer.regions = [("tag-option", 5, 2, 70, index)]
        app.renderer.oy = app.renderer.ox = 1
        with patch.object(
            app_module.curses,
            "getmouse",
            return_value=(0, 3, 6, 0, app_module.curses.BUTTON1_CLICKED),
        ):
            app.handle_mouse()
        assert app.tag_menu_checked == [y] and app.tag_menu == "edit"
        with patch.object(
            app_module.curses,
            "getmouse",
            return_value=(0, 79, 22, 0, app_module.curses.BUTTON1_CLICKED),
        ):
            app.handle_mouse()
        assert app.tag_menu == "edit" and app.tags.tag_ids(a) == (x, y)


def test_bulk_selection_distinguishes_equal_native_ids_across_harnesses_and_machines():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        rows["b"].id = rows["c"].id = "a"
        rows["c"].source = rows["a"].source
        rows["c"].machine = "other-machine"
        app._tag_keys.clear()
        app._invalidate_workflow_cache()
        other_harness = app.session_tag_key(rows["b"])
        other_machine = app.session_tag_key(rows["c"])
        assert len({a, other_harness, other_machine}) == 3
        for key in (other_harness, other_machine):
            _highlight(app, key)
            app.toggle_tag_selection()
        app.open_bulk_tags()
        app.tag_menu_checked = [x]
        app.handle_key(None, ENTER)
        assert app.tags.tag_ids(other_harness) == app.tags.tag_ids(other_machine) == (x,)
        assert app.tags.tag_ids(a) == (x, y)
        assert app.tags.tag_ids(b) == (y,)  # retained original identity is not pruned


def test_annotated_csv_rechecks_switch_without_changing_ordinary_export():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        ordinary = app._export_dataset()
        set_tags_pref(False)
        with patch.object(exporting, "write_csv") as write:
            app.export_annotated()
            write.assert_not_called()
        assert app._export_dataset() == ordinary


def test_rename_and_delete_from_the_editor_never_overwrite_another_windows_edit():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        _edit(app, b)  # B has only Y
        other = _tag_app("b")
        other.tags.set_tags(b, (x, y), other.tags.revision(b))  # another window adds X
        app.tags.refresh()
        _tag_index(app, y)
        app.handle_key(None, 5)  # rename Y from the stale editor
        app.handle_key(None, 21)
        _type(app, "renamed")
        app.handle_key(None, ENTER)
        app.handle_key(None, ENTER)  # Apply must surface the conflict, not drop X
        assert app.tag_menu == "edit" and "Changed elsewhere" in app.tag_menu_error
        assert set(app.tags.tag_ids(b)) == {x, y}
        app.handle_key(None, ESC)
        _edit(app, a)  # A has X and Y
        other.tags.refresh()
        other.tags.set_tags(a, (x,), other.tags.revision(a))  # another window drops Y
        app.tags.refresh()
        _tag_index(app, x)
        app.handle_key(None, 4)
        app.handle_key(None, ENTER)  # delete X everywhere: A now has no tags
        app.handle_key(None, ENTER)
        assert "Changed elsewhere" in app.tag_menu_error and app.tags.tag_ids(a) == ()


def test_rename_keeps_an_active_filter_on_the_renamed_tag():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        app.query = "Alpha #y|x"
        _edit(app, a)
        _tag_index(app, y)
        app.handle_key(None, 5)
        app.handle_key(None, 21)
        _type(app, "client-y")
        app.handle_key(None, ENTER)
        assert app.query == "Alpha #client-y|x"


def test_every_session_list_including_previews_applies_the_tag_filter():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        app.query = "#x"
        preview = app.renderer.session_table(list(rows.values()), 100)
        text = "\n".join(preview)
        assert "Alpha" in text and "Beta" not in text and "Gamma" not in text


def test_tag_completion_is_offered_only_where_tags_filter():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        app.filter_active = True
        app.query = "#"
        assert app.tag_filter_applies() and app.tag_suggestions()
        app.tab = app.current_tabs().index("Projects")
        assert not app.tag_filter_applies() and app.tag_suggestions() == []
        app.query = "#x"
        with patch.object(app_module.curses, "color_pair", return_value=0):
            from tests._support import FakeScreen, screen_text

            screen = FakeScreen(10, 80)
            app.renderer.draw_footer(screen, 10, 80)
        assert "#tag filters session lists" in screen_text(screen)


def test_suggestions_in_a_prices_drill_count_only_that_drills_sessions():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        app._model_by_root = {
            "a": [{"model_name": "openai/gpt-5.3", "cost": 2, "tokens_total": 100}]
        }
        app.show_prices = True
        app.prices_model = "gpt-5.3"
        app.filter_active = True
        app.query = "#"
        counts = {row.label: row.sessions for row in app.tag_suggestions()}
        assert counts["Y"] == 1 and counts["X"] == 1  # B and C are outside the drill


def test_tag_filter_narrows_before_sorting_so_excluded_rows_cost_nothing():
    with _tags_on():
        app, rows, a, b, x, y = _fixture()
        app.query = "#x"
        seen = []
        real = app.sorted_workflows
        with patch.object(
            app, "sorted_workflows", side_effect=lambda r: seen.append(len(r)) or real(r)
        ):
            assert _ids(app) == ["a"]
        assert seen and max(seen) == 1
