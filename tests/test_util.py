"""Clipboard/launchers, git-root folding, fuzzy match, range parsing, tool namespaces (util.py)."""

import os
import sys
import tempfile

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
        finally:
            if old_env is None:
                os.environ.pop("OPENTAB_LAUNCHER", None)
            else:
                os.environ["OPENTAB_LAUNCHER"] = old_env


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
