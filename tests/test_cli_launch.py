import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import asdict
from unittest.mock import patch

from opentab.accounting.models import SessionRef
from opentab.cli import launch as picker
from opentab.cli import main as cli
from opentab.launch import resume_argv
from opentab.stores.cached import CACHE_VERSION, CachedStore
from opentab.util import local_machine_name

from tests._support import workflow


def _cache(args, key, rows):
    path = picker._cache_path(key, getattr(args, picker.sources._PATH_SLOT[key]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(
            {
                "version": CACHE_VERSION,
                "source": key,
                "workflows": [asdict(row) for row in rows],
                "model_breakdown": [],
            },
            fh,
        )
    return path


def _row(sid, source, directory, *, title="Same\n\t\x1b[31m", activity="2026-01-01T00:00:00"):
    row = workflow(sid, activity, title=title, directory=directory)
    row.source = source
    return row


def test_cached_launch_is_headless_unique_and_argv_safe():
    with tempfile.TemporaryDirectory() as tmp:
        args = cli.parse_args(
            [
                "launch",
                "--no-state",
                "--db",
                os.path.join(tmp, "oc.db"),
                "--claude-dir",
                os.path.join(tmp, "claude"),
            ]
        )
        sid = "same';$(touch wrong)"

        def fake_fzf(argv, **kw):
            lines = kw["input"].decode().split("\0")
            assert lines[0].startswith("0\t") and "Claude Code" in lines[0]
            assert lines[1].startswith("1\t") and "OpenCode" in lines[1]
            assert "Same   [31m" in lines[0] and "\x1b[31m" not in lines[0]
            assert "--tiebreak=index" in argv
            return subprocess.CompletedProcess(argv, 0, lines[1].encode() + b"\0", b"")

        with patch("opentab.stores.cached.cache_dir", return_value=os.path.join(tmp, "cache")):
            _cache(args, "opencode", [_row(sid, "OpenCode", tmp)])
            _cache(args, "claude", [_row(sid, "Claude Code", tmp, activity="2026-02-01T00:00:00")])
            with patch.object(
                picker.sources, "available_sources", side_effect=AssertionError("scanned")
            ), patch.object(
                picker.sources, "make_store", side_effect=AssertionError("built store")
            ), patch.object(cli, "App", side_effect=AssertionError("TUI")), patch.object(
                picker.shutil, "which", side_effect=lambda name: "/bin/" + name
            ), patch.object(picker.subprocess, "run", side_effect=fake_fzf), patch.object(
                picker.os, "execv"
            ) as execute, patch.object(picker.os, "chdir") as chdir, patch.object(
                picker.sys, "platform", "linux"
            ):
                assert cli._run(args) == 0
                execute.assert_called_once_with(
                    "/bin/opencode", ["/bin/opencode", "--session", sid]
                )
                chdir.assert_called_once_with(tmp)


def test_launch_missing_cache_cancel_and_refresh():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "opentab.stores.cached.cache_dir", return_value=tmp
    ):
        args = cli.parse_args(["launch", "--no-state", "--harness", "codex", "--codex-dir", tmp])
        with contextlib.redirect_stderr(io.StringIO()) as error:
            assert cli._run(args) == 1
        assert "--refresh" in error.getvalue()
        _cache(args, "codex", [_row("abc", "Codex", tmp)])
        with patch.object(picker.shutil, "which", return_value="fzf"), patch.object(
            picker.subprocess, "run", return_value=subprocess.CompletedProcess([], 130, b"", b"")
        ), patch.object(picker.os, "execv", side_effect=AssertionError("launched")):
            assert cli._run(args) == 0
        args.refresh = True

        class Store:
            def workflows(self):
                events.append("workflows")

            def model_breakdown(self):
                events.append("models")

        events = []
        with patch.object(picker.sources, "make_store", return_value=(Store(), "")), patch.object(
            picker.shutil, "which", return_value="fzf"
        ), patch.object(
            picker.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")
        ):
            assert cli._run(args) == 0
        assert events == ["workflows", "models"]


def test_launch_cache_root_version_and_remote_rejection():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "opentab.stores.cached.cache_dir", return_value=tmp
    ):
        args = cli.parse_args(
            ["launch", "--no-state", "--harness", "opencode", "--db", "custom.db"]
        )
        path = _cache(args, "opencode", [_row("one", "OpenCode", tmp)])
        assert [r.id for r in picker._cached_sessions(args)] == ["one"]
        args.db = "other.db"
        assert list(picker._cached_sessions(args)) == []
        args.db = "custom.db"
        with open(path, "w") as fh:
            json.dump({"version": CACHE_VERSION - 1, "source": "opencode", "workflows": []}, fh)
        assert list(picker._cached_sessions(args)) == []
        _cache(args, "opencode", [_row("one", "OpenCode", tmp)])
        with open(path) as fh:
            payload = json.load(fh)
        payload["workflows"][0]["machine"] = "remote"
        with open(path, "w") as fh:
            json.dump(payload, fh)
        assert list(picker._cached_sessions(args)) == []


def test_launch_ignored_missing_fzf_and_missing_directory():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "opentab.stores.cached.cache_dir", return_value=tmp
    ):
        args = cli.parse_args(["launch", "--harness", "claude", "--claude-dir", tmp])
        _cache(args, "claude", [_row("ignore-me", "Claude Code", tmp)])
        with patch.object(picker, "load_state", return_value={"ignored_sessions": ["ignore-me"]}):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                assert cli._run(args) == 1
            assert "no cached local sessions" in error.getvalue()
        args.no_state = True
        with patch.object(picker.shutil, "which", return_value=None):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                assert cli._run(args) == 1
            assert "fzf not found" in error.getvalue()
        _cache(args, "claude", [_row("gone", "Claude Code", os.path.join(tmp, "gone"))])
        with patch.object(picker.shutil, "which", return_value="fzf"), patch.object(
            picker.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, b"0\tchosen\0", b""),
        ), patch.object(picker.os, "execv", side_effect=AssertionError("launched")):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                assert cli._run(args) == 1
            assert "directory no longer exists" in error.getvalue()


def test_resume_args_match_tui_shell_form():
    from opentab.tui.app import App

    row = _row("a'b", "Claude Code", "/a path")
    assert resume_argv(row) == ("/a path", ["claude", "--resume", "a'b"])
    assert App.resume_parts(None, row) == ("/a path", "claude --resume 'a'\"'\"'b'")


def test_hermes_resume_without_cwd_preserves_id_validation():
    for directory in ("", "(unknown)"):
        row = _row("a'b", "Hermes", directory)
        assert resume_argv(row) == ("", ["hermes", "--resume", "a'b"])
        for source in ("OpenCode", "Claude Code", "Codex", ""):
            row.source = source
            assert resume_argv(row) is None
        row.source = "Hermes"
        for sid in ("", "bad\0id"):
            row.id = sid
            assert resume_argv(row) is None
    assert resume_argv(_row("ok", "Hermes", "/bad\0path")) is None


def test_cached_hermes_without_cwd_launches_from_local_home():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "opentab.stores.cached.cache_dir", return_value=tmp
    ):
        args = cli.parse_args(["launch", "--no-state", "--harness", "hermes", "--hermes-db", tmp])
        _cache(args, "hermes", [_row("hermes-id", "Hermes", "(unknown)")])
        with patch.object(
            picker.shutil, "which", side_effect=lambda name: "/bin/" + name
        ), patch.object(
            picker.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, b"0\tchosen\0", b""),
        ), patch.object(picker.os, "execv") as execute, patch.object(
            picker.os, "chdir"
        ) as chdir, patch.object(picker.sys, "platform", "linux"):
            assert cli._run(args) == 0
            execute.assert_called_once_with("/bin/hermes", ["/bin/hermes", "--resume", "hermes-id"])
            chdir.assert_called_once_with(os.path.expanduser("~"))


def test_explicit_refresh_persists_an_ordinary_cache():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "opentab.stores.cached.cache_dir", return_value=tmp
    ):
        args = cli.parse_args(
            ["launch", "--refresh", "--no-state", "--harness", "claude", "--claude-dir", tmp]
        )

        class Store:
            records_cost = True

            def cache_inputs(self):
                return []

            def workflows(self):
                return [_row("session", "Claude Code", tmp)]

            def model_breakdown(self):
                return []

        with patch.object(
            picker.sources,
            "make_store",
            return_value=(CachedStore(Store(), f"claude|{tmp}", args), ""),
        ), patch.object(picker.shutil, "which", return_value="fzf"), patch.object(
            picker.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")
        ):
            assert cli._run(args) == 0
        assert [row.id for row in picker._cached_sessions(args)] == ["session"]


def test_refresh_reads_native_opencode_v2_sessions_without_legacy_table():
    from tests.test_stores_opencode_v2 import _populate_v2, _v2_db

    with _v2_db() as (writer, store), tempfile.TemporaryDirectory() as tmp, patch(
        "opentab.stores.cached.cache_dir", return_value=tmp
    ):
        _populate_v2(writer)
        args = cli.parse_args(
            ["launch", "--refresh", "--no-state", "--harness", "opencode", "--db", store.db]
        )
        picker._refresh(args)
        assert {row.id for row in picker._cached_sessions(args)} >= {"root", "sparse"}


def test_real_fzf_matches_titles_and_preserves_recency_ties():
    fzf = shutil.which("fzf")
    if not fzf:
        print("  (real fzf matching check skipped: fzf not installed)")
        return
    run = subprocess.run
    with tempfile.TemporaryDirectory() as tmp, patch(
        "opentab.stores.cached.cache_dir", return_value=tmp
    ):
        args = cli.parse_args(
            ["launch", "--no-state", "--harness", "opencode", "--db", "cache-only.db"]
        )
        old = _row("older", "OpenCode", tmp, title="needle session", activity="2026-01-01T00:00:00")
        new = _row("newer", "OpenCode", tmp, title="needle session", activity="2025-01-01T00:00:00")
        new.ended_at = "2026-02-01T00:00:00"
        _cache(args, "opencode", [old, new])

        def filter_in_real_fzf(argv, **kwargs):
            assert kwargs["env"]["FZF_DEFAULT_OPTS"] == ""
            assert kwargs["env"]["FZF_DEFAULT_OPTS_FILE"] == ""
            result = run([*argv, "--filter=needle"], **kwargs)
            selected = result.stdout.split(b"\0")
            assert result.returncode == 0 and len(selected) == 3, result
            assert selected[0].startswith(b"0\t") and b"needle session" in selected[0]
            # Both titles match equally, so original newest-first order wins.
            return subprocess.CompletedProcess(argv, 0, selected[0] + b"\0", b"")

        with patch.dict(
            os.environ,
            {
                "FZF_DEFAULT_OPTS": "--print-query --multi",
                "FZF_DEFAULT_OPTS_FILE": "/absent/options",
            },
        ), patch.object(
            picker.shutil,
            "which",
            side_effect=lambda name: fzf if name == "fzf" else "/bin/opencode",
        ), patch.object(picker.subprocess, "run", side_effect=filter_in_real_fzf), patch.object(
            picker.os, "execv"
        ) as execute, patch.object(picker.os, "chdir"), patch.object(
            picker.sys, "platform", "linux"
        ):
            assert cli._run(args) == 0
            assert execute.call_args[0][1] == ["/bin/opencode", "--session", "newer"]


def test_launch_honors_qualified_and_project_ignores():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "opentab.stores.cached.cache_dir", return_value=tmp
    ):
        args = cli.parse_args(["launch", "--db", tmp + "/oc.db", "--claude-dir", tmp])
        _cache(args, "opencode", [_row("same", "OpenCode", tmp, title="hidden")])
        _cache(args, "claude", [_row("same", "Claude Code", tmp, title="visible")])
        ref = SessionRef(local_machine_name(), "opencode", "same").encode()

        def cancel(argv, **kwargs):
            assert b"visible" in kwargs["input"] and b"hidden" not in kwargs["input"]
            return subprocess.CompletedProcess(argv, 130, b"", b"")

        with patch.object(
            picker, "load_state", return_value={"ignored_sessions": [ref]}
        ), patch.object(picker.shutil, "which", return_value="fzf"), patch.object(
            picker.subprocess, "run", side_effect=cancel
        ):
            assert cli._run(args) == 0
        with patch.object(
            picker, "load_state", return_value={"ignored_projects": [tmp]}
        ), patch.object(
            picker.subprocess, "run", side_effect=AssertionError("opened picker")
        ), contextlib.redirect_stderr(io.StringIO()):
            assert cli._run(args) == 1


def test_launch_errors_never_resume_a_session():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "opentab.stores.cached.cache_dir", return_value=tmp
    ):
        args = cli.parse_args(
            ["launch", "--no-state", "--harness", "opencode", "--db", tmp + "/db"]
        )
        _cache(args, "opencode", [_row("session", "OpenCode", tmp)])
        for code, output, message in [
            (2, b"", "fzf failed"),
            (0, b"-1\trow\0", "invalid selection"),
            (0, b"0\trow\0more\0", "invalid selection"),
        ]:
            with patch.object(picker.shutil, "which", return_value="fzf"), patch.object(
                picker.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], code, output, b"bad option"),
            ), patch.object(
                picker.os, "execv", side_effect=AssertionError("launched")
            ), contextlib.redirect_stderr(io.StringIO()) as error:
                assert cli._run(args) == 1
                assert message in error.getvalue()
        with patch.object(
            picker.shutil, "which", side_effect=lambda name: "fzf" if name == "fzf" else None
        ), patch.object(
            picker.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, b"0\trow\0", b""),
        ), contextlib.redirect_stderr(io.StringIO()) as error:
            assert cli._run(args) == 1
            assert "opencode not found" in error.getvalue()
        args.refresh = True
        with patch.object(
            picker.sources, "make_store", side_effect=sqlite3.DatabaseError("broken source")
        ), contextlib.redirect_stderr(io.StringIO()) as error:
            assert cli._run(args) == 1
            assert "cache refresh failed" in error.getvalue()


def test_windows_launch_leaves_interrupt_handling_to_harness():
    from unittest.mock import MagicMock

    with tempfile.TemporaryDirectory() as tmp, patch(
        "opentab.stores.cached.cache_dir", return_value=tmp
    ):
        args = cli.parse_args(
            ["launch", "--no-state", "--harness", "opencode", "--db", tmp + "/db"]
        )
        _cache(args, "opencode", [_row("session", "OpenCode", tmp)])
        process = MagicMock()
        process.__enter__.return_value = process
        process.wait.side_effect = [KeyboardInterrupt, 7]
        with patch.object(picker.sys, "platform", "win32"), patch.object(
            picker.shutil, "which", side_effect=lambda name: "/bin/" + name
        ), patch.object(
            picker.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, b"0\trow\0", b""),
        ), patch.object(picker.subprocess, "Popen", return_value=process) as popen:
            assert cli._run(args) == 7
            popen.assert_called_once_with(
                [os.path.abspath("/bin/opencode"), "--session", "session"], cwd=tmp
            )
            process.kill.assert_not_called()
