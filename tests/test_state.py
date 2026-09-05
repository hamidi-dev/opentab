import json
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

import opentab as ot
import opentab.state as state_module

from tests._support import (
    _claude_msg,
    _empty_opencode_db,
    _price_sort_app,
    _usage,
    _write_jsonl,
    app_with,
    workflow,
)


def test_read_state_distinguishes_missing_valid_and_malformed_files():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        assert state_module.read_state(path) == ({}, True)

        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"range": "30d", "future": {"shape": 2}}, fh)
        assert state_module.read_state(path) == (
            {"range": "30d", "future": {"shape": 2}},
            True,
        )

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{truncated")
        assert state_module.read_state(path) == ({}, False)
        assert ot.load_state(path) == {}


def test_save_state_is_atomic_and_refuses_to_overwrite_malformed_state():
    app = app_with([])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"future": {"shape": 2}}, fh)
        original_state_path = state_module.state_path
        state_module.state_path = lambda migrate=True: path
        try:
            ot.save_state(app)
        finally:
            state_module.state_path = original_state_path
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)["future"] == {"shape": 2}

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{precious but malformed")
        state_module.state_path = lambda migrate=True: path
        try:
            ot.save_state(app)
        finally:
            state_module.state_path = original_state_path

        with open(path, encoding="utf-8") as fh:
            assert fh.read() == "{precious but malformed"
        assert not [name for name in os.listdir(tmp) if name.endswith(".tmp")]


def test_first_tui_save_preserves_external_set_additions_and_removals():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        initial: dict = {key: ["keep", "removed"] for key in state_module.MUTABLE_SET_KEYS}
        initial["future"] = {"unknown": [1, 2]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(initial, fh)
        app = app_with([])
        ot.apply_state(app, app.args, ot.load_state(path))
        for key in state_module.MUTABLE_SET_KEYS:
            assert state_module.update_state("set-add", key, "external", path)[1] == ""
            assert state_module.update_state("set-remove", key, "removed", path)[1] == ""
        with patch.object(state_module, "state_path", return_value=path):
            ot.save_state(app)
        saved = ot.load_state(path)
        assert saved["future"] == initial["future"]
        for key in state_module.MUTABLE_SET_KEYS:
            assert saved[key] == ["external", "keep"]
            assert getattr(app, key) == {"keep", "removed"}


def test_tui_set_deltas_merge_with_external_edits_and_are_not_replayed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        initial = {
            key: ["keep", "local-remove", "external-remove"]
            for key in state_module.MUTABLE_SET_KEYS
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(initial, fh)
        app = app_with([])
        ot.apply_state(app, app.args, ot.load_state(path))
        local_sets = {key: getattr(app, key) for key in state_module.MUTABLE_SET_KEYS}
        for key, local in local_sets.items():
            local.remove("local-remove")
            local.add("local-add")
            assert state_module.update_state("set-add", key, "external-add", path)[1] == ""
            assert state_module.update_state("set-remove", key, "external-remove", path)[1] == ""
        with patch.object(state_module, "state_path", return_value=path):
            ot.save_state(app)
            for key in local_sets:
                assert ot.load_state(path)[key] == ["external-add", "keep", "local-add"]
                # Reverse the saved local changes externally; a later TUI save must
                # neither replay those changes nor undo the earlier external edits.
                assert state_module.update_state("set-add", key, "local-remove", path)[1] == ""
                assert state_module.update_state("set-remove", key, "local-add", path)[1] == ""
            for _ in range(2):
                ot.save_state(app)
                for key, local in local_sets.items():
                    assert ot.load_state(path)[key] == ["external-add", "keep", "local-remove"]
                    assert getattr(app, key) is local
                    assert local == {"keep", "external-remove", "local-add"}
            # New in-place local changes are measured against the successful save.
            for local in local_sets.values():
                local.remove("keep")
                local.add("next-add")
            ot.save_state(app)
            for key in local_sets:
                assert ot.load_state(path)[key] == ["external-add", "local-remove", "next-add"]


def test_tui_set_merge_starts_empty_with_missing_state_or_without_apply_state():
    for restore in (True, False):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            app = app_with([])
            if restore:
                ot.apply_state(app, app.args, ot.load_state(path))
            for key in state_module.MUTABLE_SET_KEYS:
                getattr(app, key).add("local")
                assert state_module.update_state("set-add", key, "external", path)[1] == ""
            with patch.object(state_module, "state_path", return_value=path):
                ot.save_state(app)
                for key in state_module.MUTABLE_SET_KEYS:
                    assert ot.load_state(path)[key] == ["external", "local"]
                    assert state_module.update_state("set-remove", key, "local", path)[1] == ""
                ot.save_state(app)
            for key in state_module.MUTABLE_SET_KEYS:
                assert ot.load_state(path)[key] == ["external"]


def test_failed_tui_save_retains_pending_set_deltas_for_retry():
    for restore in (True, False):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            app = app_with([])
            if restore:
                initial = {key: ["remove"] for key in state_module.MUTABLE_SET_KEYS}
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(initial, fh)
                ot.apply_state(app, app.args, ot.load_state(path))
            for key in state_module.MUTABLE_SET_KEYS:
                getattr(app, key).discard("remove")
                getattr(app, key).add("local")
            before = state_module.read_state(path)
            with patch.object(state_module, "state_path", return_value=path):
                with patch.object(state_module.os, "replace", side_effect=OSError("read-only")):
                    ot.save_state(app)
                assert state_module.read_state(path) == before
                assert not [name for name in os.listdir(tmp) if name.startswith(".state.json.")]
                for key in state_module.MUTABLE_SET_KEYS:
                    assert state_module.update_state("set-add", key, "external", path)[1] == ""
                ot.save_state(app)
            for key in state_module.MUTABLE_SET_KEYS:
                assert ot.load_state(path)[key] == ["external", "local"]


def test_tui_save_preserves_malformed_authored_sets_and_retries_after_repair():
    for key in state_module.MUTABLE_SET_KEYS:
        for malformed in (None, "oops", {}, ["keep", ""], ["keep", 1], ["keep", {}]):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "state.json")
                original = json.dumps({key: malformed, "future": {"unknown": True}})
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(original)
                app = app_with([])
                ot.apply_state(app, app.args, ot.load_state(path))
                getattr(app, key).add("local")
                with patch.object(state_module, "state_path", return_value=path):
                    ot.save_state(app)
                    with open(path, encoding="utf-8") as fh:
                        assert fh.read() == original
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump({key: ["external"], "future": {"unknown": True}}, fh)
                    ot.save_state(app)
                saved = ot.load_state(path)
                assert saved[key] == ["external", "local"]
                assert saved["future"] == {"unknown": True}


def test_update_state_adds_and_removes_values_without_losing_unknown_keys():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        original = {
            "range": "30d",
            "future": {"unknown": [1, 2]},
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(original, fh)

        for key in ("bookmarks", "ignored_projects", "ignored_sessions", "pinned_models"):
            updated, error = state_module.update_state("set-add", key, "new", path)
            assert error == "" and updated[key] == ["new"]
            updated, error = state_module.update_state("set-add", key, "old", path)
            assert error == "" and updated[key] == ["new", "old"]
            updated, error = state_module.update_state("set-remove", key, "old", path)
            assert error == "" and updated[key] == ["new"]
            assert updated["future"] == original["future"]
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh) == updated


def test_update_state_reports_invalid_unreadable_and_unwritable_mutations():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"bookmarks": ["keep"], "future": True}, fh)

        for operation, key, value in (
            ("replace", "bookmarks", "new"),
            ("set-add", "range", "new"),
            ("set-add", "bookmarks", ""),
            ("set-add", "bookmarks", 1),
        ):
            assert state_module.update_state(operation, key, value, path) == (
                {},
                "invalid operation",
            )
        assert state_module.read_state(path)[0] == {"bookmarks": ["keep"], "future": True}
        for alias in ("", 1, [], {}):
            assert state_module.update_state(
                "set-add", "bookmarks", "new", path, qualified_value=alias
            ) == ({}, "invalid operation")
        assert state_module.read_state(path)[0] == {"bookmarks": ["keep"], "future": True}

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{truncated")
        assert state_module.update_state("set-add", "bookmarks", "new", path) == (
            {},
            "unreadable",
        )

        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"bookmarks": ["keep"]}, fh)
        original_replace = state_module.os.replace
        state_module.os.replace = lambda source, target: (_ for _ in ()).throw(
            OSError("read-only filesystem")
        )
        try:
            updated, error = state_module.update_state("set-add", "bookmarks", "new", path)
        finally:
            state_module.os.replace = original_replace
        assert error == "unwritable"
        assert updated == {"bookmarks": ["keep", "new"]}
        assert not [name for name in os.listdir(tmp) if name.endswith(".tmp")]
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh) == {"bookmarks": ["keep"]}


def test_state_alias_mutations_preserve_unrelated_entries_and_tui_delta_baseline():
    for key in ("bookmarks", "ignored_sessions"):
        for aliases in ([], ["native"], ["qualified"], ["native", "qualified"]):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "state.json")
                initial = {key: sorted(["other-owner", *aliases]), "future": {"unknown": True}}
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(initial, fh)
                app = app_with([])
                ot.apply_state(app, app.args, initial)
                updated, error = state_module.update_state(
                    "set-add", key, "native", path, qualified_value="qualified"
                )
                target = "qualified" if "qualified" in aliases else "native"
                assert error == "" and updated[key] == sorted({"other-owner", *aliases, target})
                removed, error = state_module.update_state(
                    "set-remove", key, "native", path, qualified_value="qualified"
                )
                assert error == "" and removed[key] == ["other-owner"]
                assert removed["future"] == initial["future"]
                # An unchanged TUI save must not resurrect either external deletion.
                with patch.object(state_module, "state_path", return_value=path):
                    ot.save_state(app)
                assert ot.load_state(path)[key] == ["other-owner"]


def test_state_alias_selection_and_removal_read_the_file_inside_the_lock():
    for key in ("bookmarks", "ignored_sessions"):
        for operation in ("set-add", "set-remove"):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "state.json")
                original_lock, original_read = state_module._locked, state_module.read_state
                locked = [False]

                @contextmanager
                def alias_appears_before_lock(
                    lock_path, path=path, key=key, lock=original_lock, status=locked
                ):
                    assert lock_path == path
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump({key: ["qualified", "other-owner"], "future": {"shape": 2}}, fh)
                    with lock(lock_path):
                        status[0] = True
                        try:
                            yield
                        finally:
                            status[0] = False

                def read_inside_lock(read_path=None, read=original_read, status=locked):
                    assert status[0]
                    return read(read_path)

                with patch.object(state_module, "_locked", alias_appears_before_lock), patch.object(
                    state_module, "read_state", read_inside_lock
                ):
                    updated, error = state_module.update_state(
                        operation, key, "native", path, qualified_value="qualified"
                    )
                expected = (
                    ["other-owner", "qualified"] if operation == "set-add" else ["other-owner"]
                )
                assert error == "" and updated == {key: expected, "future": {"shape": 2}}
                assert ot.load_state(path) == updated


def test_concurrent_state_mutations_do_not_lose_updates():
    if state_module.fcntl is None or not hasattr(os, "fork"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        children = []
        for index in range(8):
            pid = os.fork()
            if pid == 0:
                _updated, error = state_module.update_state(
                    "set-add", "bookmarks", f"session-{index}", path
                )
                os._exit(bool(error))
            children.append(pid)
        statuses = [os.waitpid(pid, 0)[1] for pid in children]

        assert statuses == [0] * 8
        state, readable = state_module.read_state(path)
        assert readable
        assert state["bookmarks"] == [f"session-{index}" for index in range(8)]


def test_prices_sort_is_persisted_in_state():
    app = _price_sort_app()
    app.prices_sort, app.prices_sort_reverse = "cache_write", True
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = _price_sort_app()
            assert restored.prices_sort == "eff"  # fresh app starts on the eff default
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg
    assert restored.prices_sort == "cache_write" and restored.prices_sort_reverse


def test_dismissed_startup_warnings_are_persisted_and_shape_checked():
    app = app_with([])
    app.dismissed_startup_warnings = {"claude-retention-v1", "future-warning-v2"}
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([])
            ot.apply_state(restored, restored.args, ot.load_state())
            assert restored.dismissed_startup_warnings == app.dismissed_startup_warnings

            malformed = app_with([])
            ot.apply_state(malformed, malformed.args, {"dismissed_startup_warnings": "oops"})
            assert malformed.dismissed_startup_warnings == set()
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg


def test_trend_sort_is_persisted_in_state():
    # The Trends ranking column, like every other list's sort. Validated against the
    # UNION of the ranked tabs' vocabularies, not one tab's: the key is per-overlay and
    # re-validated per tab at draw time, so a saved "tokens" must survive a launch that
    # opens on Models -- which withdraws that column.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.trend_sort, app.trend_sort_reverse = "tokens", True
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert restored.trend_sort == "cost"  # a fresh app ranks by spend
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg
    assert restored.trend_sort == "tokens" and restored.trend_sort_reverse
    assert restored.trend_sort_key("Models") == "cost"  # withdrawn there, kept in state
    assert restored.trend_sort_key("Providers") == "tokens"


def test_a_broken_trend_sort_never_takes_the_launch_down_with_it():
    # state.json is a file people hand-edit. The trend column is validated against a
    # SET (the union of the ranked tabs' vocabularies), and `[] in a_set` RAISES where
    # the tuple-valued checks around it quietly answer False -- so an unhashable value
    # would kill the launch before the first frame ever paints.
    for bad in ([], {}, 42, None, "nonsense"):
        app = app_with([workflow("a", "2026-06-01 12:00:00")])
        ot.apply_state(app, app.args, {"trend_sort": bad, "trend_sort_reverse": True})
        assert app.trend_sort == "cost" and not app.trend_sort_reverse


def test_a_trend_direction_is_only_restored_with_its_column():
    # A direction with no column behind it flips whatever the key fell back to: a state
    # file carrying only the reverse would open EVERY ranking cheapest-first. Same rule
    # as sort_reverse, which documents the same trap.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    ot.apply_state(app, app.args, {"trend_sort_reverse": True})
    assert app.trend_sort == "cost" and not app.trend_sort_reverse
    # The valid pair still round-trips, and stays scoped to the tabs that have it.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    ot.apply_state(app, app.args, {"trend_sort": "tokens", "trend_sort_reverse": True})
    assert app.trend_sort_key("Providers") == "tokens" and app.trend_sort_reverse_for("Providers")
    assert app.trend_sort_key("Models") == "cost" and not app.trend_sort_reverse_for("Models")


def test_focused_time_panel_is_persisted_in_state():
    # Quit reading a month and you come back to that month, not to today. This is
    # also what keeps a saved "last_activity" sort alive across a restart: the sort
    # is withdrawn on the Days pane, so without the focus the preference would be
    # silently withdrawn on the first frame of every launch.
    app = app_with([workflow("a", "2026-06-01 12:00:00", ended_at="2026-06-05 09:00:00")])
    assert app.focus == "days"  # the fresh-app default this test has to move off
    app.set_focus("months")
    app.sort_by = "last_activity"
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with(
                [workflow("a", "2026-06-01 12:00:00", ended_at="2026-06-05 09:00:00")]
            )
            assert restored.focus == "days"
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg
    assert restored.focus == "months"
    assert restored.session_sort_key() == "last_activity"  # the pref survives, not just the key


def test_a_bogus_saved_focus_leaves_the_default_panel_standing():
    # A hand-edited or future-version state.json must not focus a panel that isn't
    # one of the three -- every other drawer reads self.focus by name.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    ot.apply_state(app, app.args, {"focus": "sidebar"})
    assert app.focus == "days"
    ot.apply_state(app, app.args, {"focus": None})
    assert app.focus == "days"


def test_last_activity_sort_is_persisted_in_state():
    # No dedicated save/restore code exists for this -- sort_by/project_sort_by are
    # already generic (state.py validates against app.sort_options/
    # project_sort_options), so "last_activity" persists for free once it's part of
    # those tuples. This locks that in.
    app = app_with([workflow("a", "2026-06-01 12:00:00", ended_at="2026-06-05 09:00:00")])
    app.sort_by = "last_activity"
    app.project_sort_by = "last_activity"
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with(
                [workflow("a", "2026-06-01 12:00:00", ended_at="2026-06-05 09:00:00")]
            )
            assert restored.sort_by == "cost" and restored.project_sort_by == "cost"
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg
    assert restored.sort_by == "last_activity"
    assert restored.project_sort_by == "last_activity"


def test_machines_browse_mode_is_restored_fleet_or_not():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-06-01 12:00:00")],
            "server": [workflow("b", "2026-06-02 12:00:00")],
        }
    )
    app.browse_mode = "machines"
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            state = ot.load_state()
            assert state["browse_mode"] == "machines"  # it IS persisted
            # Reopened as a fleet -> Machines mode is restored.
            fleet = fleet_app(
                {
                    "laptop": [workflow("a", "2026-06-01 12:00:00")],
                    "server": [workflow("b", "2026-06-02 12:00:00")],
                }
            )
            ot.apply_state(fleet, fleet.args, state)
            assert fleet.browse_mode == "machines"
            # Reopened as a single non-fleet source -> still Machines mode: the pulled
            # boxes are gone, the box you're sitting at isn't, so the list is one live row.
            solo = app_with([workflow("a", "2026-06-01 12:00:00")])
            ot.apply_state(solo, solo.args, state)
            assert solo.browse_mode == "machines"
            assert [m.name for m in solo.machines] == [solo.local_machine_name]
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg


def test_zoom_maximized_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.zoom_maximized = True
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert not restored.zoom_maximized  # the split is the fresh default
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg
    assert restored.zoom_maximized


def test_ignored_projects_are_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/repo/a")])
    app.ignored_projects = {"/repo/a", "/repo/b"}
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00", directory="/repo/a")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg

    assert restored.ignored_projects == {"/repo/a", "/repo/b"}
    assert restored.all_workflows == []


def test_ignored_sessions_are_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.ignored_sessions = {"a", "missing"}
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg

    assert restored.ignored_sessions == {"a", "missing"}
    assert restored.all_workflows == []


def test_bookmarks_are_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.bookmarks = {"a", "gone-session"}  # a stale id survives too (source may return)
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg

    assert restored.bookmarks == {"a", "gone-session"}
    assert not restored.show_bookmarks_only  # the B view itself always starts off


def test_what_if_price_view_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.show_api_prices = False  # the non-default: the estimate view is the cold start
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert restored.show_api_prices  # the default, until the saved pref lands
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg

    assert not restored.show_api_prices


def test_calendar_granularity_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.cal_levels = ot.HEAT_MAX_LEVELS
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert restored.cal_levels == ot.HEAT_DEFAULT_LEVELS  # the default until restored
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg

    assert restored.cal_levels == ot.HEAT_MAX_LEVELS


def test_source_is_persisted_and_restored():
    with tempfile.TemporaryDirectory() as tmp:
        # make both sources "present" so the cycle is opencode / claude / all
        db = os.path.join(tmp, "opencode.db")
        _empty_opencode_db(db)
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
        old_xdg = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = tmp
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
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg


def test_legacy_subagent_sort_state_routes_home_and_direction_stays_safe():
    # A pre-split state.json could hold a subagent-only key in sort_by (the lists
    # used to share it); it must land on subagent_sort_by, and the saved direction
    # must not flip the cost fallback (sessions would start cheapest-first).
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    ot.apply_state(app, app.args, {"sort_by": "depth", "sort_reverse": True})
    assert app.sort_by == "cost" and app.sort_reverse is False
    assert app.subagent_sort_by == "depth"


def test_subagent_sort_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.subagent_sort_by, app.subagent_sort_reverse = "agent", True
    old_xdg = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg
    assert restored.subagent_sort_by == "agent"
    assert restored.subagent_sort_reverse is True


def test_the_restored_browse_mode_whitelist_follows_the_mode_table():
    # The accepted keys are derived from App.BROWSE_MODES, so a mode added there is
    # restorable immediately -- a hand-kept whitelist is one that silently stops
    # accepting a mode, which reads as "opentab forgot my preference".
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert app.BROWSE_MODE_KEYS == tuple(m.key for m in app.BROWSE_MODES)
    for mode in app.BROWSE_MODES:
        fresh = app_with([workflow("a", "2026-06-01 12:00:00")])
        ot.apply_state(fresh, fresh.args, {"browse_mode": mode.key})
        assert fresh.browse_mode == mode.key
    # Anything else leaves __init__'s default standing rather than being adopted.
    fresh = app_with([workflow("a", "2026-06-01 12:00:00")])
    ot.apply_state(fresh, fresh.args, {"browse_mode": "harnesses"})
    assert fresh.browse_mode == "time"
