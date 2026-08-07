"""Clipboard/launchers, git-root folding, fuzzy match, range parsing, tool namespaces (util.py)."""

import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import opentab as ot

from tests._support import app_with, workflow


def test_resolve_project_root_folds_worktree():
    with tempfile.TemporaryDirectory() as tmp:
        main = os.path.join(tmp, "app")
        os.makedirs(os.path.join(main, ".git", "worktrees", "feat"))
        wt = os.path.join(tmp, "app-feat")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write(f"gitdir: {main}/.git/worktrees/feat\n")
        assert ot.resolve_project_root(wt) == main
        # a real repo (.git is a directory) and unknown paths resolve to themselves
        assert ot.resolve_project_root(main) == main
        assert ot.resolve_project_root(os.path.join(tmp, "nope")) == os.path.join(tmp, "nope")


def test_resolve_project_root_path_fallback_for_removed_worktree():
    # The worktree directory no longer exists (only its sessions remain in the DB),
    # so we cannot read its .git file — fold by the path convention instead.
    assert (
        ot.resolve_project_root("/Users/x/SoftwareProjects/mpvv/.worktrees/refactor")
        == "/Users/x/SoftwareProjects/mpvv"
    )
    assert ot.resolve_project_root("/repo/.git/worktrees/feat") == "/repo"
    assert ot.resolve_project_root("/Users/x/code/plain-repo") == "/Users/x/code/plain-repo"


def test_normalize_project_path_canonicalizes_windows_drive_paths():
    n = ot.normalize_project_path
    # OpenCode's forward-slash spelling and a native backslash spelling of the SAME
    # directory must collapse to one canonical form (issue #4).
    assert n("C:/DEV/Agentic-Coding/examples/okf") == r"C:\DEV\Agentic-Coding\examples\okf"
    assert n(r"C:\DEV\Agentic-Coding\examples\okf") == r"C:\DEV\Agentic-Coding\examples\okf"
    assert n("C:/DEV/app") == n(r"C:\DEV\app")
    # drive letter is case-insensitive; trailing and doubled separators collapse
    assert n("c:/dev/app") == r"C:\dev\app"
    assert n("C:/DEV//okf/") == r"C:\DEV\okf"
    assert n("C:/") == "C:\\" and n("C:\\") == "C:\\"
    # POSIX paths (incl. a literal backslash in a name), tilde, agent names, and the
    # "(unknown)" sentinel are NOT drive paths -- returned untouched.
    for p in ("/home/mo/proj", "~/code/opentab", "/weird/na\\me", "finance-os", "(unknown)"):
        assert n(p) == p
    # idempotent
    assert n(n("C:/DEV/app")) == n("C:/DEV/app")


def test_tool_namespace_classification():
    # Built-ins (even ones with underscores) fold to "(built-in)"; MCP/plugin tools
    # ("server_tool") roll up to their server prefix; anything else stands alone.
    assert ot.tool_namespace("bash") == "(built-in)"
    assert ot.tool_namespace("apply_patch") == "(built-in)"
    assert ot.tool_namespace("plan_exit") == "(built-in)"
    assert ot.tool_namespace("serena_find_symbol") == "serena"
    assert ot.tool_namespace("playwright_browser_navigate") == "playwright"
    assert ot.tool_namespace("standalone") == "standalone"


def test_tool_call_and_mix_labels():
    # A turn's cell keeps CALL order and folds repeats, because "Bash ×8" and "Bash"
    # are different stories about the same turn -- which is what the Turns tab is read
    # for. Unlike tool_namespace this never splits on a single "_".
    assert ot.tool_call_label(["Bash"]) == "Bash"
    assert ot.tool_call_label(["Bash", "Bash"]) == "Bash ×2"
    assert ot.tool_call_label(["Read", "Bash", "Read"]) == "Read ×2, Bash"
    assert ot.tool_call_label(["update_plan"]) == "update_plan"  # not "update"
    assert ot.tool_call_label([]) == "" and ot.tool_call_label(None) == ""

    # MCP tools shed the wrapper but keep the SERVER: two servers can expose the same
    # tool name, which is the whole reason the Tools tab rolls up by server.
    assert ot.short_tool_name("mcp__chrome-devtools__take_screenshot") == (
        "chrome-devtools/take_screenshot"
    )
    assert ot.short_tool_name("mcp__srv__a__b") == "srv/a__b"  # only the first two split
    assert ot.short_tool_name("mcp__") == "mcp__"  # malformed: left verbatim
    assert ot.short_tool_name("Bash") == "Bash"

    # A prompt's mix is ordered BUSIEST-FIRST, not by first use: a 40-turn prompt is
    # read for where its time went, and the tail is what a narrow column drops.
    turns = [
        {"tools": ["Read", "Bash"]},
        {"tools": ["Bash", "Bash"]},
        {"tools": []},
        {},  # a turn with no tools key at all
    ]
    assert ot.tool_mix_label(turns) == "Bash ×3, Read"
    assert ot.tool_mix_label([]) == ""


def test_tool_names_is_the_one_gate_on_an_untrusted_field():
    # `tools` crosses a trust boundary -- it is whatever a harness wrote into its
    # transcript, or another machine's export. Both label helpers and both frontends'
    # column gates go through here, so nothing can reach short_tool_name that would
    # raise, and the two frontends can never gate a column on different rules.
    assert ot.tool_names(["Bash", "Read"]) == ["Bash", "Read"]
    assert ot.tool_names(("Bash",)) == ["Bash"]
    assert ot.tool_names(None) == [] and ot.tool_names({}) == []
    # A bare string would iterate into CHARACTERS: "Bash" -> four one-letter tools.
    assert ot.tool_names("Bash") == []
    # An empty name made the TUI (gating on the rendered label) and the page (gating on
    # raw length) disagree: no column vs a column of "-".
    assert ot.tool_names([""]) == []
    # A non-string entry used to reach short_tool_name and raise AttributeError -- a
    # crash in the middle of a paint, on real-but-malformed transcript data.
    assert ot.tool_names([["x"], 3, None, "Bash"]) == ["Bash"]
    assert ot.tool_call_label([["x"]]) == ""  # no raise
    assert ot.tool_mix_label([{"tools": [["x"], "Bash"]}]) == "Bash"

    # The Tools tab is the OTHER reader of the field, and it was the one still exposed:
    # a list-valued name goes into a dict KEY there, so opening Tools on that session
    # raised "unhashable type: 'list'" -- a crash, where Turns merely mislabelled.
    from opentab.util import tool_rows_from_turns

    base = dict(
        model_name="m",
        tokens_total=10,
        input=1,
        output=1,
        reasoning=0,
        cache_read=0,
        cache_write=0,
        cache_write_1h=0,
        cost=0.0,
    )
    assert tool_rows_from_turns([dict(base, tools=[["x"]])]) == []  # no raise
    # ...and an empty name used to become a real row taking an even SHARE of the turn,
    # so one bad entry beside "Bash" moved half that turn's tokens to a nameless tool.
    (row,) = tool_rows_from_turns([dict(base, tools=["", "Bash"])])
    assert row["tool"] == "Bash" and row["tokens_total"] == 10


def test_parse_range_text():
    # (days, months, since, until)
    assert ot.parse_range_text("all") == (None, None, None, None)
    assert ot.parse_range_text("30d") == (30, None, None, None)
    # months and years are calendar windows, not day approximations
    assert ot.parse_range_text("2m") == (None, 2, None, None)
    assert ot.parse_range_text("1y") == (None, 12, None, None)
    assert ot.parse_range_text("last 14 days") == (14, None, None, None)
    assert ot.parse_range_text("last 2 months") == (None, 2, None, None)
    assert ot.parse_range_text("2026") == (None, None, "2026-01-01", "2026-12-31")
    assert ot.parse_range_text("2026-05") == (None, None, "2026-05-01", "2026-05-31")
    assert ot.parse_range_text("2024-02") == (None, None, "2024-02-01", "2024-02-29")
    assert ot.parse_range_text("2026-05-01") == (None, None, "2026-05-01", None)
    assert ot.parse_range_text("2026-05-01..2026-05-31") == (
        None,
        None,
        "2026-05-01",
        "2026-05-31",
    )
    assert ot.parse_range_text("..2026-05-31") == (None, None, None, "2026-05-31")
    # a bare number is "N days"; a 4-digit value stays a calendar year
    assert ot.parse_range_text("30") == (30, None, None, None)
    assert ot.parse_range_text("7") == (7, None, None, None)
    assert ot.parse_range_text("2026") == (None, None, "2026-01-01", "2026-12-31")


def test_relative_month_range_round_trips():
    app = app_with([workflow("a", "2026-06-07 12:00:00")])
    app.set_range_from_text("2m")
    assert app.range_months == 2
    assert app.range_days is None
    assert app.range_input_value() == "2m"  # persisted form re-parses to the same window
    assert app.range_label() == "last 2 months"

    app.set_range_from_text("1y")  # a year is twelve calendar months
    assert app.range_months == 12
    assert app.range_input_value() == "12m"
    assert app.range_label() == "last 1 year"

    app.set_all_time()
    assert app.range_months is None


def test_parse_range_text_rejects_bad_input():
    for value in ("0d", "0m", "2026-13", "2026-02-31", "banana", "2026-06-01..2026-05-01"):
        try:
            ot.parse_range_text(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid range: {value}")


def test_resume_command_cds_to_the_project_first():
    a = workflow("ses_1", "2026-06-01 12:00:00", directory="/repo/my project")
    a.source = "OpenCode"
    app = app_with([a])
    assert app.resume_command(a) == "cd '/repo/my project' && opencode --session ses_1"
    a.source = "Claude Code"
    assert app.resume_command(a) == "cd '/repo/my project' && claude --resume ses_1"
    # no command without a source stamp or a usable directory
    a.source = ""
    assert app.resume_command(a) is None
    a.source = "Claude Code"
    a.directory = "(unknown)"
    assert app.resume_command(a) is None


def test_copy_to_clipboard_backends_per_platform():
    real_which = ot.util.shutil.which
    real_run = ot.util.subprocess.run
    real_platform = sys.platform
    calls = []

    class _Proc:
        returncode = 0

    def fake_run(cmd, input=None, check=False, **kw):
        calls.append((cmd, input))
        return _Proc()

    try:
        ot.util.subprocess.run = fake_run

        # Windows: clip.exe is preferred (utf-8 bytes), label names clip/powershell.
        sys.platform = "win32"
        assert ot.util.clipboard_tools_label() == "clip/powershell"
        ot.util.shutil.which = lambda name: f"C:\\{name}.exe" if name == "clip" else None
        calls.clear()
        assert ot.util.copy_to_clipboard("ses_42") is True
        assert calls == [(["clip"], b"ses_42")]

        # clip missing -> PowerShell Set-Clipboard fallback.
        ot.util.shutil.which = lambda name: "pwsh" if name == "powershell" else None
        calls.clear()
        assert ot.util.copy_to_clipboard("hi") is True
        assert calls[0][0][0] == "powershell" and calls[0][1] == b"hi"

        # No Windows clipboard tool at all -> False, nothing run.
        ot.util.shutil.which = lambda name: None
        calls.clear()
        assert ot.util.copy_to_clipboard("x") is False
        assert calls == []

        # POSIX still uses pbcopy/xclip/... and reports them in the label.
        sys.platform = "darwin"
        assert ot.util.clipboard_tools_label() == "pbcopy/wl-copy/xclip/xsel"
        ot.util.shutil.which = lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None
        calls.clear()
        assert ot.util.copy_to_clipboard("ok") is True
        assert calls == [(["pbcopy"], b"ok")]
    finally:
        sys.platform = real_platform
        ot.util.shutil.which = real_which
        ot.util.subprocess.run = real_run


def test_tmux_launch_argv_builds_window_split_popup():
    cmd = "claude --resume abc123"
    # directory rides on -c / -d flags
    assert ot.tmux_launch_argv("window", "/repo/a", cmd) == [
        "tmux",
        "new-window",
        "-c",
        "/repo/a",
        cmd,
    ]
    assert ot.tmux_launch_argv("hsplit", "/repo/a", cmd)[:3] == ["tmux", "split-window", "-h"]
    assert ot.tmux_launch_argv("vsplit", "/repo/a", cmd)[:3] == ["tmux", "split-window", "-v"]
    popup = ot.tmux_launch_argv("popup", "/repo/a", cmd)
    assert popup[:3] == ["tmux", "display-popup", "-E"]
    assert "/repo/a" in popup and cmd in popup


def test_ssh_command_quotes_the_remote_side_as_one_argument():
    # The whole remote half is ONE quoted argument: passed as two, ssh joins them with a
    # space and the local shell evaluates the "&&" instead -- which resumes on the remote
    # box but in the wrong directory (and, with a path holding a space, not at all).
    cmd = ot.ssh_command("root@giant", "/srv/app", "claude --resume abc123")
    assert cmd == "ssh -t root@giant 'cd /srv/app && claude --resume abc123'"
    assert cmd.startswith("ssh -t ")  # a tty: these CLIs are interactive
    spaced = ot.ssh_command("mo@box", "/srv/my app", "codex resume 'x y'")
    assert spaced == """ssh -t mo@box 'cd '"'"'/srv/my app'"'"' && codex resume '"'"'x y'"'"''"""


def test_launcher_hook_detected_via_env_then_config():
    old_env = os.environ.get("OPENTAB_LAUNCHER")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            # nothing installed: no hook
            os.environ.pop("OPENTAB_LAUNCHER", None)
            os.environ["XDG_CONFIG_HOME"] = tmp
            assert ot.launcher_hook() is None
            # the well-known config path is picked up once executable
            hook = os.path.join(tmp, "opentab", "launcher")
            os.makedirs(os.path.dirname(hook))
            with open(hook, "w") as fh:
                fh.write("#!/bin/sh\n")
            assert ot.launcher_hook() is None  # not executable yet
            os.chmod(hook, 0o755)
            assert ot.launcher_hook() == hook
            # the env override wins over the config path
            override = os.path.join(tmp, "other")
            with open(override, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(override, 0o755)
            os.environ["OPENTAB_LAUNCHER"] = override
            assert ot.launcher_hook() == override
            # a bogus override falls through to the config path
            os.environ["OPENTAB_LAUNCHER"] = os.path.join(tmp, "missing")
            assert ot.launcher_hook() == hook
        finally:
            for key, val in (("OPENTAB_LAUNCHER", old_env), ("XDG_CONFIG_HOME", old_xdg)):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val


def test_tmux_launch_runs_the_hook_and_reports_its_stderr():
    old_env = os.environ.get("OPENTAB_LAUNCHER")
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "log")
        hook = os.path.join(tmp, "launcher")
        with open(hook, "w") as fh:
            fh.write(f'#!/bin/sh\nprintf "%s|%s|%s" "$1" "$2" "$3" > {log}\n')
        os.chmod(hook, 0o755)
        try:
            os.environ["OPENTAB_LAUNCHER"] = hook
            assert ot.util.tmux_launch("window", "/repo/a", "claude --resume x1") is None
            with open(log) as fh:
                assert fh.read() == "window|/repo/a|claude --resume x1"
            # a failing hook surfaces its stderr as the launch error
            with open(hook, "w") as fh:
                fh.write('#!/bin/sh\necho "no such kind" >&2\nexit 1\n')
            assert ot.util.tmux_launch("vsplit", "/repo/a", "claude --resume x1") == "no such kind"
            with open(hook, "w") as fh:
                fh.write('#!/bin/sh\nprintf "  \\n" >&2\nexit 1\n')
            assert (
                ot.util.tmux_launch("vsplit", "/repo/a", "claude --resume x1")
                == "launcher hook failed"
            )
        finally:
            if old_env is None:
                os.environ.pop("OPENTAB_LAUNCHER", None)
            else:
                os.environ["OPENTAB_LAUNCHER"] = old_env


def test_in_herdr_only_accepts_an_enabled_official_marker():
    old = os.environ.get("HERDR_ENV")
    try:
        for value in (None, "", "0", "false", "no"):
            if value is None:
                os.environ.pop("HERDR_ENV", None)
            else:
                os.environ["HERDR_ENV"] = value
            assert not ot.in_herdr()
        os.environ["HERDR_ENV"] = "1"
        assert ot.in_herdr()
        os.environ["HERDR_ENV"] = "true"
        assert ot.in_herdr()
    finally:
        if old is None:
            os.environ.pop("HERDR_ENV", None)
        else:
            os.environ["HERDR_ENV"] = old


def test_herdr_create_argv_builds_focused_splits():
    old_pane = os.environ.get("HERDR_PANE_ID")
    try:
        os.environ["HERDR_PANE_ID"] = "  pane-42  "
        assert ot.herdr_create_argv("hsplit", "/repo/a") == [
            "herdr",
            "pane",
            "split",
            "--pane",
            "pane-42",
            "--direction",
            "right",
            "--cwd",
            "/repo/a",
            "--focus",
        ]
        assert ot.herdr_create_argv("vsplit", "/repo/a") == [
            "herdr",
            "pane",
            "split",
            "--pane",
            "pane-42",
            "--direction",
            "down",
            "--cwd",
            "/repo/a",
            "--focus",
        ]
        for kind in ("popup", "unknown"):
            try:
                ot.herdr_create_argv(kind, "/repo/a")
            except ValueError as exc:
                assert "popup" in str(exc) if kind == "popup" else "unknown" in str(exc)
            else:
                raise AssertionError(f"accepted Herdr launch kind {kind}")
    finally:
        if old_pane is None:
            os.environ.pop("HERDR_PANE_ID", None)
        else:
            os.environ["HERDR_PANE_ID"] = old_pane


def test_herdr_splits_require_the_current_pane_id():
    old_pane = os.environ.pop("HERDR_PANE_ID", None)
    try:
        for pane_id in (None, "", " \t "):
            if pane_id is None:
                os.environ.pop("HERDR_PANE_ID", None)
            else:
                os.environ["HERDR_PANE_ID"] = pane_id
            for kind in ("hsplit", "vsplit"):
                try:
                    ot.herdr_create_argv(kind, "/repo/a")
                except ValueError as exc:
                    assert str(exc) == "HERDR_PANE_ID is required for Herdr splits"
                else:
                    raise AssertionError(f"accepted {kind} with HERDR_PANE_ID={pane_id!r}")
                assert (
                    ot.herdr_launch(kind, "/repo/a", "cmd")
                    == "HERDR_PANE_ID is required for Herdr splits"
                )
    finally:
        if old_pane is not None:
            os.environ["HERDR_PANE_ID"] = old_pane


def test_herdr_create_argv_uses_configured_binary():
    old = os.environ.get("HERDR_BIN_PATH")
    with tempfile.TemporaryDirectory() as tmp:
        binary = os.path.join(tmp, "herdr-bin")
        with open(binary, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(binary, 0o755)
        try:
            os.environ["HERDR_BIN_PATH"] = binary
            assert ot.herdr_create_argv("window", "/repo/a")[0] == binary
        finally:
            if old is None:
                os.environ.pop("HERDR_BIN_PATH", None)
            else:
                os.environ["HERDR_BIN_PATH"] = old


def test_herdr_tab_create_argv_uses_workspace_when_set():
    old = os.environ.get("HERDR_WORKSPACE_ID")
    old_pane = os.environ.get("HERDR_PANE_ID")
    try:
        os.environ["HERDR_WORKSPACE_ID"] = "workspace-42"
        os.environ["HERDR_PANE_ID"] = "pane-42"
        assert ot.herdr_create_argv("window", "/repo/a") == [
            "herdr",
            "tab",
            "create",
            "--workspace",
            "workspace-42",
            "--cwd",
            "/repo/a",
            "--focus",
        ]
        assert "--workspace" not in ot.herdr_create_argv("hsplit", "/repo/a")
    finally:
        if old is None:
            os.environ.pop("HERDR_WORKSPACE_ID", None)
        else:
            os.environ["HERDR_WORKSPACE_ID"] = old
        if old_pane is None:
            os.environ.pop("HERDR_PANE_ID", None)
        else:
            os.environ["HERDR_PANE_ID"] = old_pane


def test_herdr_tab_create_argv_omits_workspace_when_unset():
    old = os.environ.get("HERDR_WORKSPACE_ID")
    try:
        os.environ.pop("HERDR_WORKSPACE_ID", None)
        assert ot.herdr_create_argv("window", "/repo/a") == [
            "herdr",
            "tab",
            "create",
            "--cwd",
            "/repo/a",
            "--focus",
        ]
    finally:
        if old is not None:
            os.environ["HERDR_WORKSPACE_ID"] = old


def test_herdr_launch_reads_both_json_paths_and_keeps_command_one_argument():
    real_run = ot.util.subprocess.run
    old_pane = os.environ.get("HERDR_PANE_ID")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if len(calls) == 1:
            key = "root_pane" if argv[1:3] == ["tab", "create"] else "pane"
            return SimpleNamespace(
                returncode=0, stdout=f'{{"result":{{"{key}":{{"pane_id":"pane-7"}}}}}}', stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    try:
        os.environ["HERDR_PANE_ID"] = "pane-current"
        ot.util.subprocess.run = fake_run
        command = "ssh -t box 'cd /repo/a && claude --resume abc'"
        assert ot.herdr_launch("window", "/repo/a", command) is None
        assert calls[1][0] == ["herdr", "pane", "run", "pane-7", command]
        calls.clear()
        assert ot.herdr_launch("hsplit", "/repo/a", command) is None
        assert calls[1][0][-1] == command and len(calls[1][0]) == 5
        assert calls[0][0][3:5] == ["--pane", "pane-current"]
    finally:
        ot.util.subprocess.run = real_run
        if old_pane is None:
            os.environ.pop("HERDR_PANE_ID", None)
        else:
            os.environ["HERDR_PANE_ID"] = old_pane


def test_herdr_launch_stops_on_create_and_json_errors():
    real_run = ot.util.subprocess.run
    old_pane = os.environ.get("HERDR_PANE_ID")
    cases = [
        (SimpleNamespace(returncode=2, stdout="", stderr="socket gone"), "create failed"),
        (SimpleNamespace(returncode=0, stdout="not json", stderr=""), "invalid JSON"),
        (SimpleNamespace(returncode=0, stdout='{"result":{}}', stderr=""), "pane ID"),
        (
            SimpleNamespace(returncode=0, stdout='{"result":{"pane":{"pane_id":4}}}', stderr=""),
            "pane ID",
        ),
    ]
    try:
        os.environ["HERDR_PANE_ID"] = "pane-current"
        for response, message in cases:
            calls = []

            def fake_run(argv, response=response, calls=calls, **kwargs):
                calls.append(argv)
                return response

            ot.util.subprocess.run = fake_run
            error = ot.herdr_launch("hsplit", "/repo/a", "claude --resume abc")
            assert message in error and len(calls) == 1
            assert not any(call[1:3] == ["pane", "current"] for call in calls)
    finally:
        ot.util.subprocess.run = real_run
        if old_pane is None:
            os.environ.pop("HERDR_PANE_ID", None)
        else:
            os.environ["HERDR_PANE_ID"] = old_pane


def test_herdr_failures_use_stderr_but_never_raw_json_output():
    real_run = ot.util.subprocess.run
    try:
        for stderr, stdout, expected in (
            (
                "permission denied\nmore detail",
                '{"error":"no"}',
                "herdr create failed: permission denied",
            ),
            ("   \n", '{"error":"no"}', "herdr create failed (exit status 2)"),
            ('{"error":"no"}', "", "herdr create failed (exit status 2)"),
        ):
            ot.util.subprocess.run = lambda argv, stderr=stderr, stdout=stdout, **kwargs: (
                SimpleNamespace(returncode=2, stderr=stderr, stdout=stdout)
            )
            error = ot.herdr_launch("window", "/repo/a", "cmd")
            assert error == expected and '{"error"' not in error
    finally:
        ot.util.subprocess.run = real_run


def test_herdr_launch_reports_os_errors_timeouts_and_run_stage():
    real_run = ot.util.subprocess.run
    try:
        for failure, message in (
            (OSError("missing"), "create failed: missing"),
            (subprocess.TimeoutExpired(["herdr"], 10), "create timed out"),
        ):

            def fail_create(argv, failure=failure, **kwargs):
                raise failure

            ot.util.subprocess.run = fail_create
            assert message in ot.herdr_launch("window", "/repo/a", "cmd")

        for failure, message in (
            (
                SimpleNamespace(returncode=3, stdout="", stderr="rejected"),
                "pane run failed: rejected",
            ),
            (OSError("closed"), "pane run failed for pane pane-9: closed"),
            (subprocess.TimeoutExpired(["herdr"], 10), "pane run timed out for pane pane-9"),
        ):
            calls = 0

            def fail_run(argv, failure=failure, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return SimpleNamespace(
                        returncode=0,
                        stdout='{"result":{"root_pane":{"pane_id":"pane-9"}}}',
                        stderr="",
                    )
                if isinstance(failure, BaseException):
                    raise failure
                return failure

            ot.util.subprocess.run = fail_run
            error = ot.herdr_launch("window", "/repo/a", "cmd")
            assert message in error and "pane-9" in error and calls == 2
    finally:
        ot.util.subprocess.run = real_run


def test_launch_backend_prefers_hook_and_selects_innermost_multiplexer():
    keys = ("OPENTAB_LAUNCHER", "TMUX", "TMUX_PANE", "HERDR_ENV", "TERM", "XDG_CONFIG_HOME")
    old_env = {key: os.environ.get(key) for key in keys}
    real_current, real_tmux = ot.util._current_tty, ot.util._tmux_pane_tty
    with tempfile.TemporaryDirectory() as tmp:
        hook = os.path.join(tmp, "launcher")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(hook, 0o755)
        try:
            for key in keys:
                os.environ.pop(key, None)
            # A real config launcher must not leak into backend selection; the explicit
            # OPENTAB_LAUNCHER below still has priority.
            os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "empty-config")
            assert ot.launch_backend() is None
            os.environ["TMUX"] = "tmux"
            assert ot.launch_backend() == "tmux"
            os.environ.pop("TMUX")
            os.environ["HERDR_ENV"] = "1"
            assert ot.launch_backend() == "herdr"
            os.environ["OPENTAB_LAUNCHER"] = hook
            assert ot.launch_backend() == "hook"
            os.environ.pop("OPENTAB_LAUNCHER")
            os.environ["TMUX"] = "tmux"
            os.environ["TMUX_PANE"] = "%1"
            ot.util._current_tty = lambda: "/dev/pts/7"
            ot.util._tmux_pane_tty = lambda: "/dev/pts/7"
            assert ot.launch_backend() == "tmux"  # tmux inside Herdr
            ot.util._tmux_pane_tty = lambda: "/dev/pts/3"
            assert ot.launch_backend() == "herdr"  # Herdr inside tmux
            ot.util._current_tty = lambda: None
            os.environ["TERM"] = "screen-256color"
            assert ot.launch_backend() == "tmux"
            os.environ["TERM"] = "xterm-256color"
            assert ot.launch_backend() == "herdr"
        finally:
            ot.util._current_tty, ot.util._tmux_pane_tty = real_current, real_tmux
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_launch_command_dispatches_to_herdr_and_hook_popup_stays_async():
    real_backend = ot.util.launch_backend
    real_herdr = ot.util.herdr_launch
    real_hook = ot.util.launcher_hook
    real_popen = ot.util.subprocess.Popen
    calls = []
    try:
        ot.util.launch_backend = lambda: "herdr"
        ot.util.herdr_launch = lambda kind, directory, command: (
            calls.append((kind, directory, command)) or None
        )
        assert ot.launch_command("window", "/repo/a", "cmd") is None
        assert calls == [("window", "/repo/a", "cmd")]

        ot.util.launch_backend = lambda: "hook"
        ot.util.launcher_hook = lambda: "/tmp/hook"
        ot.util.subprocess.Popen = lambda argv, **kwargs: calls.append(argv)
        assert ot.launch_command("popup", "/repo/a", "cmd") is None
        assert calls[-1] == ["/tmp/hook", "popup", "/repo/a", "cmd"]
    finally:
        ot.util.launch_backend = real_backend
        ot.util.herdr_launch = real_herdr
        ot.util.launcher_hook = real_hook
        ot.util.subprocess.Popen = real_popen


def test_fuzzy_score_matches_subsequences():
    assert ot.fuzzy_score("", "anything") == 0  # empty query matches everything
    assert ot.fuzzy_score("otb", "opentab") is not None  # subsequence, not substring
    assert ot.fuzzy_score("xyz", "opentab") is None
    assert ot.fuzzy_score("TREND", "Trend view") is not None  # case-insensitive
    # tight matches outrank scattered ones
    assert ot.fuzzy_score("trend", "fix trend view") > ot.fuzzy_score(
        "trend", "travel reimbursement node"
    )
    # word starts outrank mid-word hits
    assert ot.fuzzy_score("tv", "trend view") > ot.fuzzy_score("tv", "octave")


def test_anchored_fuzzy_match_only_enters_words_at_their_start():
    # The strict cousin fuzzy_score's binary consumers use (pricing.model_matches):
    # letters may scatter INSIDE a word, but a new word only joins at its first
    # character. fuzzy_score would accept all the rejections below as subsequences --
    # fzf does too, but fzf ranks them out of sight, and the model filters keep the
    # column sort, so over the ~5k-row catalog a bare subsequence floated the junk
    # to the top (filtering "opus" showed qwen3-coder-plus above claude-opus-4-8).
    m = ot.anchored_fuzzy_match
    assert m("", "anything")  # empty query matches everything
    assert m("OPUS", "claude-Opus-4-8")  # case-insensitive
    assert m("opus48", "claude-opus-4-8")  # chunks chain word starts...
    assert m("snt45", "claude-sonnet-4-5")  # ...and may scatter inside a word
    assert m("opus4-5", "claude-opus-4-5")  # a typed boundary matches a boundary
    # ...and a separator is not a word: it needs no anchoring of its own, only
    # order, so a leading/floating "-" narrows instead of matching nothing.
    assert m("-48", "claude-opus-4-8")
    assert m("a-b", "a/x-b")
    assert m("cs45", "claude-sonnet-4-5")  # initials alone work too
    assert m("net", "claude-sonnet-4-5")  # a plain substring always matches
    # Every alignment is tracked, not a greedy scan: the dead-end o-p of "openai"
    # must not eat the match.
    assert m("opus", "openai-opus-clone")
    # The junk the pure subsequence let through (real rows from the catalog).
    assert not m("opus", "qwen3-coder-plus")  # o enters "coder" mid-word
    assert not m("opus", "gemini-3.1-pro-preview-customtools")
    assert not m("opus", "phi-4-reasoning-plus")
    assert not m("opus", "anthropic.claude-sonnet-5")
    assert not m("gtex", "gpt-5-codex")  # e enters "codex" mid-word
    # A near-miss over a repeated-letter id must return, fast: a backtracking-regex
    # implementation of this rule enumerated the alignments here (exponential -- a
    # single row froze the keystroke for seconds), which is why the walk is a DP.
    assert not m("a" * 25 + "z", "aaa-" * 12 + "bz")


def test_wsl_mount_root_and_windows_path_mapping():
    with tempfile.TemporaryDirectory() as tmp:
        # wsl.conf parsing: [automount] root= wins, comments stripped, missing -> /mnt.
        conf = os.path.join(tmp, "wsl.conf")
        with open(conf, "w") as fh:
            fh.write("[boot]\nsystemd=true\n[automount]\n# comment\nroot = /win ; inline\n")
        assert ot.util.wsl_mount_root(conf) == "/win"
        assert ot.util.wsl_mount_root(os.path.join(tmp, "absent.conf")) == "/mnt"

        # Drive-path folding: C:\... and C:/... land on <mount>/c/... when it exists.
        proj = os.path.join(tmp, "c", "Users", "mo", "proj")
        os.makedirs(proj)
        assert ot.util.windows_to_wsl_path(r"C:\Users\mo\proj", mount_root=tmp) == proj
        assert ot.util.windows_to_wsl_path("c:/Users/mo/proj", mount_root=tmp) == proj
        assert ot.util.windows_to_wsl_path("C:/Users/mo/gone", mount_root=tmp) == ""  # not mounted
        assert (
            ot.util.windows_to_wsl_path("/home/mo/proj", mount_root=tmp) == ""
        )  # not a drive path


def test_open_path_uses_startfile_on_windows():
    # On Windows there is no open/xdg-open; open_path reveals the folder via os.startfile.
    called = {}
    orig_platform = ot.sys.platform
    had_startfile = hasattr(ot.os, "startfile")
    orig_startfile = getattr(ot.os, "startfile", None)
    try:
        ot.sys.platform = "win32"
        ot.os.startfile = lambda p: called.setdefault("path", p)
        assert ot.open_path("C:/repo/proj") is True
        assert called["path"] == "C:/repo/proj"
    finally:
        ot.sys.platform = orig_platform
        if had_startfile:
            ot.os.startfile = orig_startfile
        else:
            del ot.os.startfile


def test_tool_namespace_folds_builtins_case_insensitively_and_mcp_servers():
    # OpenCode/pi log "bash" where Claude Code logs "Bash"; both are built-ins.
    assert ot.tool_namespace("Bash") == "(built-in)"
    assert ot.tool_namespace("bash") == "(built-in)"
    assert ot.tool_namespace("shell_command") == "(built-in)"  # Codex; not a "shell" server
    # Claude Code MCP names group under their server, like OpenCode's prefix form.
    assert ot.tool_namespace("mcp__chrome-devtools__evaluate_script") == "chrome-devtools"
    assert ot.tool_namespace("serena_find_symbol") == "serena"


def test_unicode_screen_prefers_acs_on_the_linux_console():
    # The Linux virtual console pairs a UTF-8 locale with a font that lacks the heavy
    # glyphs (console fonts carry CP437's light lines only), and the miss is invisible
    # to curses -- the console renders the replacement blob client-side, no error comes
    # back. TERM is the only tell, so the gate must read it before the locale.
    import locale

    from opentab import util

    saved_cache = util._UNICODE_SCREEN
    saved_term = os.environ.get("TERM")
    try:
        util._UNICODE_SCREEN = None
        os.environ["TERM"] = "linux"
        assert ot.unicode_screen() is False
        util._UNICODE_SCREEN = None  # any other TERM falls through to the locale
        os.environ["TERM"] = "xterm-256color"
        try:
            expected = "utf" in locale.nl_langinfo(locale.CODESET).lower()
        except (AttributeError, ValueError):
            expected = True
        assert ot.unicode_screen() is expected
    finally:
        util._UNICODE_SCREEN = saved_cache
        if saved_term is None:
            os.environ.pop("TERM", None)
        else:
            os.environ["TERM"] = saved_term


def test_cached_share_reads_one_turn_without_needing_its_neighbours():
    # The whole cache story off a SINGLE turn -- no previous row, no clock, no TTL.
    # Measured across ~37k real turns, a normal turn leaves 0.4-0.7% of its context
    # uncached while a re-buy leaves 80-89%, so there is no threshold to tune.
    normal = {"input": 2, "cache_read": 520_000, "cache_write": 900}
    assert round(ot.cached_share(normal) * 100) == 100

    # A re-buy: nothing read back, the whole context written again.
    rebuy = {"input": 10, "cache_read": 0, "cache_write": 300_000}
    assert ot.cached_share(rebuy) == 0.0

    # No per-backend branching: where writes are not billed (Claude via GitHub Copilot
    # records write:0 even on a miss) the re-buy lands in plain `input`, and the same
    # subtraction catches it.
    copilot = {"input": 200_000, "cache_read": 0, "cache_write": 0}
    assert ot.cached_share(copilot) == 0.0
    warm_copilot = {"input": 4_000, "cache_read": 196_000, "cache_write": 0}
    assert 0.97 < ot.cached_share(warm_copilot) < 1.0

    # Too small to mean anything -> None, never a confident 0% that would read as a
    # total miss (and under every provider's minimum cacheable prompt anyway).
    assert ot.cached_share({"input": 100, "cache_read": 0, "cache_write": 0}) is None
    assert ot.cached_share({}) is None


def test_env_flag_reads_a_falsey_value_as_off():
    # bool(os.environ.get(...)) reads "0" and "false" as ON -- any non-empty string is
    # truthy -- so a user turning something off would have turned it on.
    saved = os.environ.get("OPENTAB_TEST_FLAG")
    try:
        for value, want in (
            ("1", True),
            ("true", True),
            ("YES", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("Off", False),
        ):
            os.environ["OPENTAB_TEST_FLAG"] = value
            assert ot.util.env_flag("OPENTAB_TEST_FLAG") is want, value
        os.environ["OPENTAB_TEST_FLAG"] = "   "
        assert ot.util.env_flag("OPENTAB_TEST_FLAG") is None  # blank == unset
        os.environ.pop("OPENTAB_TEST_FLAG")
        assert ot.util.env_flag("OPENTAB_TEST_FLAG") is None
    finally:
        os.environ.pop("OPENTAB_TEST_FLAG", None)
        if saved is not None:
            os.environ["OPENTAB_TEST_FLAG"] = saved


def test_palette_writes_ignored_detects_herdr_only_by_its_own_marker():
    # herdr runs each pane under libghostty-vt and re-emits the parsed cells, forwarding
    # a palette-indexed cell as an INDEX -- so the host terminal resolves it against its
    # own palette and an init_color write is structurally discarded (issue #12). It
    # cannot be probed, so it is a denylist keyed on the marker herdr sets itself.
    saved = os.environ.get("HERDR_ENV")
    try:
        os.environ.pop("HERDR_ENV", None)
        assert ot.util.palette_writes_ignored() is False
        os.environ["HERDR_ENV"] = "1"
        assert ot.util.palette_writes_ignored() is True
        os.environ["HERDR_ENV"] = "0"  # never a guess: an explicit off is off
        assert ot.util.palette_writes_ignored() is False
    finally:
        os.environ.pop("HERDR_ENV", None)
        if saved is not None:
            os.environ["HERDR_ENV"] = saved


def test_safe_int_and_safe_float_reject_every_number_a_log_can_hold_but_a_float_cannot():
    # The one coercion rule every file backend reads its usage through, because each way
    # of getting it wrong loses a whole BACKEND rather than one record. Three shapes, and
    # the third is the one that looks harmless:
    #   * a string / object / None -> TypeError or ValueError,
    #   * `1e400` -> valid JSON, parses to inf, and int(inf) raises OverflowError (an
    #     ArithmeticError, which `except (TypeError, ValueError)` does NOT catch),
    #   * a 400-digit JSON *integer* -> raises nothing at all here. Python's int has no
    #     ceiling, so it parses, sums, and then raises "int too large to convert to
    #     float" out of the first float it meets: api_equivalent_cost's multiply under
    #     "$", or the tokens() formatter's divide -- a crash a whole layer away from the
    #     parse that admitted it.
    huge = int("1" + "0" * 400)  # what json.loads gives for a 401-digit literal
    assert ot.util.safe_int(huge) == 0
    assert ot.util.safe_float(huge) == 0.0

    # ...and finite is not the same as safe to ADD UP, which is why safe_float bounds
    # the magnitude rather than only checking isfinite: 1e308 is a perfectly good float
    # and two of them sum to inf -- raising nothing, poisoning every session, day and
    # month total downstream. Bounded in the coercer, so no caller has to re-check a sum.
    assert ot.util.safe_float(10**308) == 0.0
    assert ot.util.safe_float(-(10**308)) == 0.0
    assert ot.ZalyStore._cost_total({"cost": {"input": 10**308, "output": 10**308}}) == 0.0
    assert ot.PiStore._cost_total({"cost": {"total": 10**308}}) == 0.0
    for bad in (float("inf"), float("-inf"), float("nan"), "abc", None, {}, [], "1e400"):
        assert ot.util.safe_int(bad) == 0, bad
        assert ot.util.safe_float(bad) == 0.0, bad
    assert ot.util.safe_float("abc", default=-1.0) == -1.0

    # Ordinary values are untouched, negatives clamp to 0 (a token count is a count),
    # and the ceiling is the largest integer a float holds exactly.
    assert ot.util.safe_int(0) == 0 and ot.util.safe_int(12345) == 12345
    assert ot.util.safe_int("42") == 42 and ot.util.safe_int(1.9) == 1
    assert ot.util.safe_int(-5) == 0
    assert ot.util.safe_int(1 << 53) == 1 << 53 and ot.util.safe_int((1 << 53) + 1) == 0
    assert ot.util.safe_float(1.25) == 1.25 and ot.util.safe_float("0.5") == 0.5
    assert ot.util.safe_float(-3.5) == -3.5  # the sign survives; callers clamp if they must
    assert ot.util.safe_float(float(1 << 53)) == float(1 << 53)  # the same ceiling, both sides

    # ...and every backend's coercer is that rule, not a copy of it that drifted.
    for store in (
        ot.PiStore,
        ot.OmpStore,
        ot.OpenClawStore,
        ot.ZalyStore,
        ot.ClaudeStore,
        ot.CodexStore,
    ):
        assert store._int(huge) == 0 and store._int(float("inf")) == 0, store.__name__
        assert store._int(9_000) == 9_000, store.__name__
    for store in (ot.PiStore, ot.OpenClawStore):
        assert store._cost_total({"cost": {"total": huge}}) == 0.0
        assert store._cost_total({"cost": {"total": float("inf")}}) == 0.0
        assert store._cost_total({"cost": {"total": 1.5}}) == 1.5
    # zaly sums per-component costs: a poisoned component drops, the rest still counts.
    assert ot.ZalyStore._cost_total({"cost": {"input": huge, "output": 0.25}}) == 0.25
    # CSV/JSONL clean their cell (strip, drop "," and "$") and then coerce through the
    # same rule -- a token count of "1e308" is finite and would still have flowed into
    # every total, and shown as 309 digits.
    assert ot.CsvStore._to_int("1" + "0" * 400) == 0
    assert ot.CsvStore._to_float("1" + "0" * 400) == 0.0
    assert ot.CsvStore._to_int("1e308") == 0 and ot.CsvStore._to_float("1e308") == 0.0
    assert ot.CsvStore._to_int("12,345") == 12345 and ot.CsvStore._to_int("1e3") == 1000
    assert ot.CsvStore._to_float("$1,000.50") == 1000.5
