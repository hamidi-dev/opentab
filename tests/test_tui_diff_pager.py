import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import opentab as ot
from opentab.cli import doctor
from opentab.tui import bindings, diff_pager, keymap

from tests._support import FakeStore, workflow


class ChangeStore(FakeStore):
    def __init__(self, workflows, *, demo=False):
        super().__init__(workflows)
        self.demo = demo
        self.demo_scale = 1.0
        self.demo_cats = frozenset()

    def workflow_nodes(self, workflow_id):
        return []

    def supports_turns(self, workflow_id):
        return False

    def supports_tools(self, workflow_id):
        return False

    def supports_changes(self, workflow_id):
        return True


def _app(*, demo=False, diff=None):
    store = ChangeStore([workflow("root", "2026-09-19 10:00:00")], demo=demo)
    args = SimpleNamespace(since=None, until=None, days=None)
    app = ot.App(store, args)
    app.view = "session"
    wf = app.current_session()
    app._nodes_by_session[wf.id] = []
    if not demo:
        app.tab = app.current_tabs().index("Changes")
        scope = app._change_scope()
        edit = {
            "key": "edit-key",
            "file": "src/name.py",
            "status": "modified",
            "available": True,
        }
        app._changes_cache[scope] = {
            "files": [{"file": "src/name.py", "status": "modified", "edits": [edit]}],
            "truncated": False,
        }
        app._changes_scope = scope
        app._change_drill = True
        if diff is not None:
            app._change_diff_cache[(scope, "edit-key")] = diff
    return app


def test_command_requires_explicit_env_and_parses_quoted_argv_without_shell_syntax():
    assert diff_pager.configured_argv({"PAGER": "less", "GIT_PAGER": "delta"}) is None
    assert diff_pager.configured_argv(
        {diff_pager.ENV_VAR: 'delta --features "side by side" --paging=always'}
    ) == ["delta", "--features", "side by side", "--paging=always"]
    for raw in ("", "   ", "delta '", "delta\0evil"):
        try:
            diff_pager.configured_argv({diff_pager.ENV_VAR: raw})
        except diff_pager.PagerConfigError:
            pass
        else:
            raise AssertionError(f"accepted invalid pager command {raw!r}")


def test_patch_preparation_sanitizes_controls_and_synthesizes_correct_headers():
    patch_text = "@@ -1 +1 @@\n-old\x1b]0;owned\x07\n+new\tvalue\n"
    cases = (
        ({"file": "new.py"}, {"status": "added"}, "--- /dev/null\n+++ b/new.py\n"),
        ({"file": "old.py"}, {"status": "deleted"}, "--- a/old.py\n+++ /dev/null\n"),
        (
            {"file": "new.py"},
            {"status": "modified", "from_file": "old.py"},
            "--- a/old.py\n+++ b/new.py\n",
        ),
    )
    for file, edit, headers in cases:
        got = diff_pager.prepare_patch(file, edit, {"patch": patch_text})
        assert got.startswith(headers + "@@ -1 +1 @@\n")
        assert "\x1b" not in got and "\x07" not in got
        assert "+new\tvalue\n" in got

    complete = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b"
    assert (
        diff_pager.prepare_patch({"file": "x"}, {"status": "modified"}, {"patch": complete})
        == complete
    )


def test_runner_uses_exact_stdin_inherited_output_no_shell_and_restores_every_path():
    screen = SimpleNamespace(clearok=Mock(), refresh=Mock())
    calls = ("def_prog_mode", "endwin", "reset_prog_mode", "curs_set", "mousemask")
    mocks = {name: Mock() for name in calls}
    with (
        patch.multiple(diff_pager.curses, **mocks),
        patch.object(diff_pager.subprocess, "run") as run,
    ):
        run.return_value = SimpleNamespace(returncode=0)
        assert diff_pager.run(["delta", "--paging=always"], "exact\tpatch\n", screen, 77) == (
            "ok",
            0,
        )
        run.assert_called_once_with(
            ["delta", "--paging=always"],
            input="exact\tpatch\n",
            text=True,
            shell=False,
            check=False,
        )
        assert "stdout" not in run.call_args.kwargs and "stderr" not in run.call_args.kwargs
        mocks["reset_prog_mode"].assert_called_once_with()
        mocks["mousemask"].assert_called_once_with(77)
        screen.clearok.assert_called_once_with(True)
        screen.refresh.assert_called_once_with()

    for side_effect, expected in (
        (OSError("secret path"), "spawn-failed"),
        (KeyboardInterrupt(), "interrupted"),
    ):
        screen = SimpleNamespace(clearok=Mock(), refresh=Mock())
        with (
            patch.object(diff_pager.curses, "def_prog_mode"),
            patch.object(diff_pager.curses, "endwin"),
            patch.object(diff_pager.curses, "reset_prog_mode") as reset,
            patch.object(diff_pager.curses, "curs_set"),
            patch.object(diff_pager.curses, "mousemask"),
            patch.object(diff_pager.subprocess, "run", side_effect=side_effect),
        ):
            assert diff_pager.run(["pager"], "patch", screen)[0] == expected
            reset.assert_called_once_with()
            screen.clearok.assert_called_once_with(True)
            screen.refresh.assert_called_once_with()

    screen = SimpleNamespace(clearok=Mock(), refresh=Mock())
    with (
        patch.object(diff_pager.curses, "def_prog_mode"),
        patch.object(diff_pager.curses, "endwin"),
        patch.object(diff_pager.curses, "reset_prog_mode") as reset,
        patch.object(diff_pager.curses, "curs_set"),
        patch.object(diff_pager.curses, "mousemask"),
        patch.object(diff_pager.subprocess, "run", return_value=SimpleNamespace(returncode=9)),
    ):
        assert diff_pager.run(["pager"], "patch", screen) == ("nonzero", 9)
        reset.assert_called_once_with()
        screen.refresh.assert_called_once_with()


def test_app_launches_only_the_current_loaded_complete_patch_and_reports_safe_failures():
    screen = SimpleNamespace(refresh=Mock())
    complete = {"patch": "@@ -1 +1 @@\n-old\n+new\n", "truncated": False}
    app = _app(diff=complete)
    with (
        patch.dict(os.environ, {diff_pager.ENV_VAR: 'delta --features "line nums"'}),
        patch.object(diff_pager, "run", return_value=("ok", 0)) as run,
    ):
        app.handle_key(screen, ord("d"))
    argv, stdin, handed_screen, _mask = run.call_args.args
    assert argv == ["delta", "--features", "line nums"]
    assert stdin == "--- a/src/name.py\n+++ b/src/name.py\n" + complete["patch"]
    assert handed_screen is screen
    assert all("src/name.py" not in arg for arg in argv)

    blocked = (
        _app(diff=None),
        _app(diff={"patch": "patch", "truncated": True}),
        _app(diff={"patch": "", "truncated": False}),
        _app(demo=True),
    )
    unavailable = _app(diff=complete)
    unavailable.selected_change_edit()["available"] = False
    blocked += (unavailable,)
    with (
        patch.dict(os.environ, {diff_pager.ENV_VAR: "delta"}),
        patch.object(diff_pager, "run") as run,
    ):
        for candidate in blocked:
            candidate.open_change_diff_pager(screen)
        assert not run.called
    assert "truncated" in blocked[1].notice

    stale = _app(diff=complete)
    stale.selected_change_edit()["key"] = "unknown"
    with (
        patch.dict(os.environ, {diff_pager.ENV_VAR: "delta"}),
        patch.object(diff_pager, "run") as run,
    ):
        stale.open_change_diff_pager(screen)
        assert not run.called and "no loaded" in stale.notice


def test_unset_and_malformed_configuration_are_safe_and_never_expose_parser_details():
    app = _app(diff={"patch": "patch", "truncated": False})
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(diff_pager.ENV_VAR, None)
        app.open_change_diff_pager(SimpleNamespace(refresh=Mock()))
    assert diff_pager.ENV_VAR in app.notice and "set" in app.notice

    with patch.dict(os.environ, {diff_pager.ENV_VAR: "delta '"}):
        app.open_change_diff_pager(SimpleNamespace(refresh=Mock()))
    assert app.notice == "diff pager: OPENTAB_DIFF_PAGER is empty or malformed"
    assert "quotation" not in app.notice


def test_key_is_contextual_remappable_and_doctor_lists_only_the_explicit_variable():
    app = _app(diff={"patch": "patch", "truncated": False})
    entry = keymap.BY_ID["diff-pager"]
    assert entry.shown(app) and entry.label(app) == "d" and diff_pager.ENV_VAR in entry.text(app)
    app._change_drill = False
    assert not entry.shown(app)

    app._change_drill = True
    app.keymap = bindings.Keymap({("main", "diff_pager"): ["x"]})
    assert entry.label(app) == "x"
    with patch.object(app, "open_change_diff_pager") as opened:
        app.handle_key(None, ord("d"))
        assert not opened.called
        app.handle_key(None, ord("x"))
        opened.assert_called_once_with(None)

    with patch.dict(os.environ, {diff_pager.ENV_VAR: "less -R"}):
        rows = doctor.env_rows()
    assert any(row.label == diff_pager.ENV_VAR and row.detail == "less -R" for row in rows)
