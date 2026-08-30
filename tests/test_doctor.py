import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile

import opentab as ot
from opentab import doctor

from tests._support import _parse

# Every harness path flag, so a test namespace can point all of them somewhere empty --
# a report that reads the developer's real ~/.claude would assert differently on every
# machine. --csv/--jsonl are deliberately absent: passing them explicitly pins
# args.source (see sources._route_path_arg), which would rewrite the selection verdict;
# their defaults already resolve into the suite's isolated XDG data dir.
_FLAGS = (
    "--db", "--claude-dir", "--codex-dir", "--hermes-db", "--copilot-dir",
    "--vscode-dir", "--pi-dir", "--omp-dir", "--openclaw-dir", "--zaly-dir",
    "--gemini-dir", "--antigravity-dir",
)  # fmt: skip

# Anything in the ambient environment that would change a verdict.
_ENV_KEYS = (
    "COPILOT_OTEL_FILE_EXPORTER_PATH",
    "CLAUDE_CONFIG_DIR",
    "GEMINI_CLI_HOME",
    "OPENTAB_NO_INIT_COLOR",
    "HERDR_ENV",
    "WSL_DISTRO_NAME",
    "TMUX",
    "STY",
    "ZELLIJ",
    "DVTM",
    "BYOBU_BACKEND",
    "TERM",
    "SHELL",
    "POWERSHELL_DISTRIBUTION_CHANNEL",
    "PSModulePath",
)


@contextlib.contextmanager
def _clean_env(**over):
    saved = {k: os.environ.get(k) for k in set(_ENV_KEYS) | set(over)}
    try:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        # Doctor correctly treats an absent TERM as a terminal failure. Give ordinary
        # report tests the same usable terminal GitHub Actions does not provide.
        os.environ["TERM"] = "xterm-256color"
        for key, value in over.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


def _args(tmp, *extra, **paths):
    # A doctor namespace with every harness pointed at a subdir of `tmp` -- absent
    # unless the test created it. `paths` overrides one by dest name (claude_dir=...).
    argv = ["doctor"]
    for flag in _FLAGS:
        dest = flag[2:].replace("-", "_")
        argv += [flag, paths.get(dest) or os.path.join(tmp, "absent-" + dest)]
    return _parse([*argv, *extra])


def _ns():
    # A doctor namespace with no harness anywhere -- for the checks that only care about
    # the terminal/files sections and shouldn't depend on the developer's real home.
    with tempfile.TemporaryDirectory() as tmp:
        return _args(tmp)


def _touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _rows(sections, title):
    return dict(sections)[title]


def _by_label(sections, title, label):
    return next(r for r in _rows(sections, title) if r.label == label)


def _tilde(path):
    return doctor._tilde(path)


# --- this opentab ------------------------------------------------------------------------


def test_the_install_method_is_named_not_left_to_be_inferred_from_the_path():
    # It decides the answer to the next two questions anyone asks: how do I upgrade, and
    # am I running the copy I just changed.
    cases = {
        "/Users/x/.local/pipx/venvs/opentab-ai/lib/python3.13/site-packages/opentab": "pipx",
        "/Users/x/.local/share/uv/tools/opentab-ai/lib/python3.13/site-packages/opentab": "uv tool",
        "/opt/homebrew/lib/python3.13/site-packages/opentab": "Homebrew",
        "/usr/lib/python3/dist-packages/opentab": "system package manager",
    }
    for path, expected in cases.items():
        assert doctor._install_method(path) == expected
    # A src-layout checkout is recognised by its pyproject, not by a path fragment.
    assert doctor._install_method(doctor._pkg_dir()) == "source checkout"


def test_a_different_opentab_on_path_is_reported():
    # The failure this exists for, and the one Mo has actually hit: a shim on PATH
    # pointing into a different environment than the interpreter running. Everything you
    # change is real and none of it is what runs, and no other row would say so -- they
    # all faithfully describe the copy that IS executing.
    saved = shutil.which
    try:
        shutil.which = lambda name: "/somewhere/else/bin/opentab"
        row = doctor._path_row("/opt/homebrew/lib/python3.13/site-packages/opentab", False)
        assert row.status == doctor.WARN and "DIFFERENT install" in row.detail
        assert "which -a" in row.hint
        # From a source checkout the divergence is the point, not a mistake: still
        # stated, not warned about.
        assert doctor._path_row(doctor._pkg_dir(), False).status == doctor.INFO
        # A shim in any directory this interpreter would install a console script into
        # is simply correct -- asked of sysconfig, because `pip install --user` puts the
        # script in ~/.local/bin while sys.prefix stays /usr, and the old "under
        # sys.prefix" test called that one ordinary install two fighting ones.
        #
        # Through a temp dir rather than a real script dir: _path_row resolves the shim
        # with realpath (a pipx shim IS a symlink into its venv), so on a machine that
        # already has an opentab in one of those directories the probe would follow that
        # symlink somewhere else and the test would assert on the host, not the code.
        # Python 3.9 put the user scheme at ~/.local/bin, where exactly that lives.
        with tempfile.TemporaryDirectory() as bindir:
            saved_dirs = doctor._script_dirs
            try:
                doctor._script_dirs = lambda: {os.path.realpath(bindir)}
                shutil.which = lambda name: os.path.join(bindir, "opentab")
                assert doctor._path_row(doctor._pkg_dir(), False).status == doctor.OK
            finally:
                doctor._script_dirs = saved_dirs
        assert len(doctor._script_dirs()) >= 2  # the default scheme and the user one
        # No console script at all (python -m opentab) is neutral, never a finding.
        shutil.which = lambda name: None
        assert doctor._path_row(doctor._pkg_dir(), False).status == doctor.INFO
    finally:
        shutil.which = saved


def test_hints_that_set_a_variable_are_written_in_the_readers_own_shell():
    # A PowerShell user handed `export FOO=bar` gets a command that does not run, which
    # is worse than no hint at all.
    assert doctor._export("TERM", "xterm-256color", "posix") == "export TERM=xterm-256color"
    assert doctor._export("TERM", "xterm-256color", "fish") == "set -gx TERM xterm-256color"
    assert doctor._export("TERM", "xterm-256color", "powershell") == '$env:TERM = "xterm-256color"'

    # The TERM hint only exists on the terminfo-failure row, which has to be forced by
    # monkeypatch rather than by unsetting TERM -- ncurses caches the setup (see
    # _NoTerminfo), so a flip passes alone and fails inside the suite.
    saved = doctor.curses
    try:
        doctor.curses = _NoTerminfo
        with _clean_env(POWERSHELL_DISTRIBUTION_CHANNEL="MSI:Windows 10 Pro"):
            assert doctor._shell() == ("PowerShell", "powershell")
            os.environ.pop("TERM", None)
            row = next(r for r in doctor.terminal_rows() if r.label == "TERM")
            assert '$env:TERM = "xterm-256color"' in row.hint and "export" not in row.hint
        with _clean_env(SHELL="/bin/bash"):
            os.environ.pop("TERM", None)
            assert "export TERM=xterm-256color" in (
                next(r for r in doctor.terminal_rows() if r.label == "TERM").hint
            )
    finally:
        doctor.curses = saved
    with _clean_env(SHELL="/opt/homebrew/bin/fish"):
        assert doctor._shell() == ("fish", "fish")
    with _clean_env(SHELL="/bin/zsh"):
        assert doctor._shell() == ("zsh", "posix")


def test_redaction_survives_windows_spellings_of_the_same_home():
    # On Windows the same directory is written `C:\Users\Alice`, `C:/Users/Alice` and
    # `c:\users\alice`. An exact match folds only the first and leaks the username out
    # of the other two -- in the default report, the one meant to be pasted publicly.
    saved = os.name
    home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = os.environ["USERPROFILE"] = r"C:\Users\Alice"
        doctor.os.name = "nt"
        for spelling in (
            r"C:\Users\Alice\.codex",
            "C:/Users/Alice/.codex",
            r"c:\users\alice\.codex",
        ):
            assert "Alice" not in doctor._tilde(spelling)
            assert "alice" not in doctor._tilde(spelling).lower()
        # A sibling that merely shares the prefix is not folded to "~" -- it is a
        # different directory. It is another account, so it is redacted rather than
        # printed (see test_redaction_hides_a_windows_username_that_home_cannot_reach).
        assert doctor._tilde(r"C:\Users\Alicia\x") == r"C:\Users\<user>\x"
    finally:
        doctor.os.name = saved
        os.environ.pop("USERPROFILE", None)
        os.environ.pop("HOME", None)
        if home is not None:
            os.environ["HOME"] = home


def test_powershell_is_detected_off_windows_and_cmd_gets_its_own_syntax():
    # PowerShell exports PSModulePath on every platform, and it identifies the RUNNING
    # shell where $SHELL is only the login one -- inside pwsh on Linux, $SHELL still
    # says bash, and the report handed that user `export FOO=bar`.
    with _clean_env(PSModulePath="/usr/local/share/powershell/Modules", SHELL="/bin/bash"):
        assert doctor._shell() == ("PowerShell", "powershell")
    with _clean_env(SHELL="/usr/bin/pwsh"):
        assert doctor._shell() == ("PowerShell", "powershell")
    # On Windows PSModulePath can be machine-wide, so it is a hint and says so.
    saved = doctor.os.name
    try:
        doctor.os.name = "nt"
        with _clean_env(PSModulePath=r"C:\Program Files\PowerShell\Modules"):
            assert doctor._shell() == ("PowerShell (assumed)", "powershell")
        # ...and with no marker at all, PowerShell would have set one: that is cmd,
        # whose syntax is different again, so it no longer gets PowerShell commands.
        with _clean_env():
            assert doctor._shell() == ("cmd", "cmd")
    finally:
        doctor.os.name = saved
    assert doctor._export("FOO", "bar", "cmd") == "set FOO=bar"


def test_a_home_relative_value_is_expanded_by_the_shell_that_gets_it():
    # `$env:X = "~/..."` assigns a LITERAL tilde -- PowerShell expands ~ only when it
    # starts a path token, not inside an assigned string -- so Copilot would be pointed
    # at a directory named "~". cmd never expands it at all.
    value = "~/.copilot/otel/usage.jsonl"
    assert doctor._export("X", value, "posix") == f"export X={value}"
    assert doctor._export("X", value, "fish") == f"set -gx X {value}"
    assert doctor._export("X", value, "powershell") == '$env:X = "$HOME/.copilot/otel/usage.jsonl"'
    assert doctor._export("X", value, "cmd") == "set X=%USERPROFILE%/.copilot/otel/usage.jsonl"


def test_byobu_over_tmux_still_gets_the_tmux_colour_fix():
    # The detected name is "byobu (tmux)", which does not END with "tmux" -- so the
    # multiplexer row went OK with no hint while the TERM row had already suppressed
    # its own, leaving the one actionable line nowhere.
    saved = doctor._terminfo
    try:
        doctor._terminfo = lambda: (8, 0, True)
        with _clean_env(TMUX="/tmp/t,1,0", BYOBU_BACKEND="tmux"):
            row = next(r for r in doctor.terminal_rows() if r.label == "multiplexer")
            assert row.status == doctor.WARN and "default-terminal" in row.hint
    finally:
        doctor._terminfo = saved


def test_a_file_its_directory_hides_is_never_reported_as_absent():
    # os.path.exists() is False both for "absent" and for "the directory won't let me
    # look". Saying "none yet" about notes that may well exist is the most damaging
    # thing this report could get wrong -- notes are the one file opentab can't rebuild.
    if getattr(os, "getuid", lambda: 1)() == 0 or os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as tmp, _clean_env(), _xdg_roots(tmp):
        data = os.path.join(tmp, "dat", "opentab")
        os.makedirs(data)
        _touch(os.path.join(data, "notes.json"), json.dumps({"notes": {"s": "x"}}))
        os.chmod(data, 0o000)
        try:
            row = _by_label([("files", doctor.file_rows(_args(tmp)))], "files", "notes")
            assert row.status == doctor.BAD and "denies access" in row.detail
        finally:
            os.chmod(data, 0o755)


def test_a_keymap_that_is_not_utf8_never_takes_the_tui_down():
    # load_user_keymap promises it never raises -- but UnicodeDecodeError is a
    # ValueError, not an OSError, so a conf with one stray byte escaped it and crashed
    # cli.main() at launch, not just doctor.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keymap.conf")
        with open(path, "wb") as fh:
            fh.write(b"[browse]\nquit = \xff\xfe\n")
        keymap = ot.tui.bindings.load_user_keymap(path)
        assert keymap.warnings and "UTF-8" in keymap.warnings[0]


# --- the borrowed verdict --------------------------------------------------------------


def test_present_harnesses_are_exactly_what_available_sources_reports():
    # THE invariant. Doctor explains a verdict, it never forms a second one: the moment
    # it decides presence for itself, the report starts contradicting the app it exists
    # to describe -- and it contradicts it in the one situation where someone is
    # trusting it over what they can see.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        claude = os.path.join(tmp, "claude")
        zaly = os.path.join(tmp, "zaly")
        _touch(os.path.join(claude, "proj", "a.jsonl"), "{}\n")
        _touch(os.path.join(zaly, "sessions", "ws", "u1", "session.jsonl"), "{}\n")
        args = _args(tmp, claude_dir=claude, zaly_dir=zaly)
        present = set(ot.sources.available_sources(args))
        assert present == {"claude", "zaly"}
        ok = {r.label for r in doctor.harness_rows(args) if r.status == doctor.OK}
        assert ok == {"Claude Code", "Zaly", "selection"}


def test_claude_retention_warns_without_changing_the_presence_verdict():
    with tempfile.TemporaryDirectory() as tmp:
        claude = os.path.join(tmp, "claude")
        config = os.path.join(tmp, "claude-config")
        _touch(os.path.join(claude, "proj", "a.jsonl"), "{}\n")
        os.makedirs(config)
        args = _args(tmp, claude_dir=claude)

        with _clean_env(CLAUDE_CONFIG_DIR=config):
            rows = doctor.harness_rows(args)
            harness = next(row for row in rows if row.label == "Claude Code")
            retention = next(row for row in rows if row.label == "Claude retention")
            assert harness.status == doctor.OK
            assert retention.status == doctor.WARN
            assert "default: 30 days" in retention.detail
            assert '"cleanupPeriodDays": 3650' in retention.hint

            with open(os.path.join(config, "settings.json"), "w") as fh:
                json.dump({"cleanupPeriodDays": 3650}, fh)
            rows = doctor.harness_rows(args)
            assert not any(row.label == "Claude retention" for row in rows)

            with open(os.path.join(config, "settings.json"), "w") as fh:
                fh.write("{broken")
            retention = next(
                row for row in doctor.harness_rows(args) if row.label == "Claude retention"
            )
            assert retention.status == doctor.WARN and "invalid" in retention.detail


def test_claude_retention_still_warns_after_the_last_transcript_is_gone():
    with tempfile.TemporaryDirectory() as tmp:
        claude = os.path.join(tmp, "empty-claude-projects")
        config = os.path.join(tmp, "claude-config")
        os.makedirs(claude)
        os.makedirs(config)
        with _clean_env(CLAUDE_CONFIG_DIR=config):
            rows = doctor.harness_rows(_args(tmp, claude_dir=claude))
        harness = next(row for row in rows if row.label == "Claude Code")
        retention = next(row for row in rows if row.label == "Claude retention")
        assert harness.status == doctor.INFO and "no sessions" in harness.detail
        assert retention.status == doctor.WARN


def test_gemini_retention_warns_on_the_defaults_it_never_had_to_be_told():
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        gemini = os.path.join(home, ".gemini")
        _touch(os.path.join(gemini, "tmp", "proj", "chats", "session-a.jsonl"), "{}\n")
        args = _args(tmp, gemini_dir=gemini)

        with _clean_env(GEMINI_CLI_HOME=home):
            rows = doctor.harness_rows(args)
            harness = next(row for row in rows if row.label == "Gemini")
            retention = next(row for row in rows if row.label == "Gemini retention")
            assert harness.status == doctor.OK
            assert retention.status == doctor.WARN
            assert "default: 30 days" in retention.detail
            assert '"enabled": false' in retention.hint

            settings = os.path.join(gemini, "settings.json")
            with open(settings, "w") as fh:
                json.dump({"general": {"sessionRetention": {"enabled": False}}}, fh)
            assert not any(row.label == "Gemini retention" for row in doctor.harness_rows(args))

            with open(settings, "w") as fh:
                json.dump({"general": {"sessionRetention": {"maxCount": 50}}}, fh)
            capped = next(
                row for row in doctor.harness_rows(args) if row.label == "Gemini retention"
            )
            assert "maxCount: 50" in capped.detail

            # Gemini refuses to start on a settings file it cannot parse, so the
            # policy is unknown rather than "the 30-day default".
            with open(settings, "w") as fh:
                fh.write("{broken")
            broken = next(
                row for row in doctor.harness_rows(args) if row.label == "Gemini retention"
            )
            assert "unreadable" in broken.detail and "will not start" in broken.detail


def test_a_missing_harness_is_neutral_and_carries_no_hint():
    # 12 backends and most people run one or two: an absent tool is not a finding, and
    # 10 "here is how to install it" hints would bury the row that matters.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        claude = os.path.join(tmp, "claude")
        _touch(os.path.join(claude, "p", "a.jsonl"), "{}\n")
        rows = doctor.harness_rows(_args(tmp, claude_dir=claude))
        hermes = next(r for r in rows if r.label == "Hermes")
        assert hermes.status == doctor.INFO
        assert "not found" in hermes.detail and hermes.hint == ""


def test_rows_are_ordered_actionable_first():
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        claude = os.path.join(tmp, "claude")
        zaly = os.path.join(tmp, "zaly")
        _touch(os.path.join(claude, "p", "a.jsonl"), "{}\n")
        _touch(os.path.join(zaly, "sessions", "a.jsonl"), "{}\n")  # wrong depth -> WARN
        rows = doctor.harness_rows(_args(tmp, claude_dir=claude, zaly_dir=zaly))[:-1]
        order = [r.status for r in rows]
        assert order == sorted(order, key=[doctor.OK, doctor.BAD, doctor.WARN, doctor.INFO].index)


# --- the reasons doctor adds on top ------------------------------------------------------


def test_a_data_directory_in_the_wrong_layout_warns_and_names_the_layout():
    # The failure worth building this for: the tool IS installed and the transcripts ARE
    # there, opentab just isn't looking where the flag points. "not found" would send the
    # user to reinstall zaly; the fix is one directory level.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        deep = os.path.join(tmp, "zaly", "sessions")  # --zaly-dir wants zaly/, not sessions/
        _touch(os.path.join(deep, "ws", "u1", "session.jsonl"), "{}\n")
        row = next(r for r in doctor.harness_rows(_args(tmp, zaly_dir=deep)) if r.label == "Zaly")
        assert row.status == doctor.WARN
        assert "1 file, none matching" in row.detail
        assert "DATA directory" in row.hint


def test_openclaw_outside_its_agents_layout_warns_too():
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        root = os.path.join(tmp, "openclaw")
        _touch(os.path.join(root, "sessions", "s.jsonl"), "{}\n")  # no agents/<a>/ level
        rows = doctor.harness_rows(_args(tmp, openclaw_dir=root))
        row = next(r for r in rows if r.label == "OpenClaw")
        assert row.status == doctor.WARN and "gateway HOME" in row.hint


def test_an_empty_but_real_directory_is_neutral_not_a_warning():
    # The tool is installed and simply hasn't been used. Nothing to fix.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        empty = os.path.join(tmp, "codex")
        os.makedirs(empty)
        row = next(
            r for r in doctor.harness_rows(_args(tmp, codex_dir=empty)) if r.label == "Codex"
        )
        assert row.status == doctor.INFO and "no sessions yet" in row.detail


def test_vscode_sessions_without_tokens_explain_themselves():
    # Opening the chat panel writes empty session files, so "files but no usage" looks
    # exactly like a parsing bug from outside. It isn't one.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        user = os.path.join(tmp, "Code", "User")
        _touch(os.path.join(user, "workspaceStorage", "h1", "chatSessions", "s.json"), "{}")
        row = next(
            r for r in doctor.harness_rows(_args(tmp, vscode_dir=user)) if r.label == "VS Code"
        )
        assert row.status == doctor.WARN
        assert "none with recorded tokens" in row.detail
        assert "chat that ran" in row.hint


def test_vscode_from_wsl_points_at_the_windows_store_instead():
    # The same state has a different cause under WSL: the Windows-side store is
    # deliberately never auto-scanned, so "chat more" would be the wrong advice.
    with tempfile.TemporaryDirectory() as tmp, _clean_env(WSL_DISTRO_NAME="Ubuntu"):
        user = os.path.join(tmp, "Code", "User")
        _touch(os.path.join(user, "workspaceStorage", "h1", "chatSessions", "s.json"), "{}")
        row = next(
            r for r in doctor.harness_rows(_args(tmp, vscode_dir=user)) if r.label == "VS Code"
        )
        assert row.status == doctor.WARN and "--vscode-dir" in row.hint and "/mnt/c" in row.hint


def test_vscode_with_recorded_tokens_is_simply_present():
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        user = os.path.join(tmp, "Code", "User")
        _touch(
            os.path.join(user, "workspaceStorage", "h1", "chatSessions", "s.json"),
            json.dumps({"requests": [{"promptTokens": 10}]}),
        )
        args = _args(tmp, vscode_dir=user)
        assert "vscode" in ot.sources.available_sources(args)
        row = next(r for r in doctor.harness_rows(args) if r.label == "VS Code")
        assert row.status == doctor.OK and row.hint == ""


def test_vscode_counts_chat_sessions_and_not_the_user_settings_next_to_them():
    # sources._vscode_available scans a bare `*.json*` at the root too -- that pattern is
    # for --vscode-dir pointed straight AT a chatSessions dir, and availability only ever
    # opens those files looking for a token marker, so a stray match costs it nothing.
    # Counting them reported settings.json and chatLanguageModels.json as chat sessions
    # ("15 chat sessions" on a real machine with 13).
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        user = os.path.join(tmp, "Code", "User")
        _touch(os.path.join(user, "settings.json"), "{}")
        _touch(os.path.join(user, "chatLanguageModels.json"), "{}")
        _touch(os.path.join(user, "settings.json.bak"), "{}")
        for i in range(2):
            _touch(os.path.join(user, "workspaceStorage", f"h{i}", "chatSessions", "s.json"), "{}")
        row = next(
            r for r in doctor.harness_rows(_args(tmp, vscode_dir=user)) if r.label == "VS Code"
        )
        assert "2 chat sessions," in row.detail

        # ...but pointed straight at a chatSessions dir, the root files ARE the sessions.
        sessions = os.path.join(user, "workspaceStorage", "h0", "chatSessions")
        row = next(
            r for r in doctor.harness_rows(_args(tmp, vscode_dir=sessions)) if r.label == "VS Code"
        )
        assert "1 chat session," in row.detail


@contextlib.contextmanager
def _fake_home(tmp):
    # Detection keys on ~/.copilot, so the test must own $HOME -- otherwise it passes
    # only on a developer machine that already has Copilot and fails in a clean CI home.
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    try:
        os.environ["HOME"] = os.environ["USERPROFILE"] = home
        yield home
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


def test_copilot_installed_but_export_off_says_so():
    # The most common Copilot report: the CLI is there, the OTEL export is opt-in, and
    # nothing anywhere tells you that.
    with tempfile.TemporaryDirectory() as tmp, _clean_env(), _fake_home(tmp) as home:
        os.makedirs(os.path.join(home, ".copilot"))  # the CLI is installed
        otel = os.path.join(home, ".copilot", "otel")
        row = next(
            r for r in doctor.harness_rows(_args(tmp, copilot_dir=otel)) if r.label == "Copilot"
        )
        assert row.status == doctor.WARN
        assert "export is off" in row.detail
        assert "COPILOT_OTEL_FILE_EXPORTER_PATH" in row.hint
        # ...and with no ~/.copilot at all it is simply absent, never a claim that the
        # CLI is installed (which the old parent-directory test made of any path).
        os.rmdir(os.path.join(home, ".copilot"))
        row = next(
            r for r in doctor.harness_rows(_args(tmp, copilot_dir=otel)) if r.label == "Copilot"
        )
        assert row.status == doctor.INFO and "not found" in row.detail


def test_copilot_export_configured_but_empty_gets_its_own_verdict():
    # The user already did the non-obvious step. Telling them to do it again is the one
    # answer guaranteed not to help.
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "usage.jsonl")  # named, never written
        with _clean_env(COPILOT_OTEL_FILE_EXPORTER_PATH=target):
            row = next(r for r in doctor.harness_rows(_args(tmp)) if r.label == "Copilot")
            assert row.status == doctor.WARN
            assert "nothing written there yet" in row.detail
            assert "AFTER the variable was set" in row.hint


# --- selection ---------------------------------------------------------------------------


def test_openclaw_counts_its_archived_sessions_like_the_store_does():
    # OpenClawStore._files reads "<id>.jsonl.reset.<ts>"/".deleted.<ts>" archives too, so
    # a count that stops at "*.jsonl" reports fewer files than opentab actually parses.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        root = os.path.join(tmp, "openclaw")
        sessions = os.path.join(root, "agents", "main", "sessions")
        _touch(os.path.join(sessions, "s1.jsonl"), "{}\n")
        _touch(os.path.join(sessions, "s2.jsonl.reset.1700000000"), "{}\n")
        _touch(os.path.join(sessions, "s3.jsonl.deleted.1700000001"), "{}\n")
        row = next(
            r for r in doctor.harness_rows(_args(tmp, openclaw_dir=root)) if r.label == "OpenClaw"
        )
        assert row.status == doctor.OK and "3 files" in row.detail


def test_a_saved_single_harness_preference_is_reported_as_hiding_the_others():
    # "opentab only shows Claude" -- because state.json remembers the last `H`. Nothing
    # in the UI announces that, and state.json is not somewhere anyone thinks to look.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        claude, codex = os.path.join(tmp, "claude"), os.path.join(tmp, "codex")
        _touch(os.path.join(claude, "p", "a.jsonl"), "{}\n")
        _touch(os.path.join(codex, "p", "b.jsonl"), "{}\n")
        args = _args(tmp, claude_dir=claude, codex_dir=codex)
        state_file = ot.state.state_path()
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as fh:
            json.dump({"source": "claude"}, fh)
        try:
            row = doctor.harness_rows(args)[-1]
            assert row.status == doctor.WARN and row.label == "selection"
            assert "Claude Code" in row.detail and "hidden" in row.detail
            assert "--harness all" in row.hint
        finally:
            os.remove(state_file)


def test_two_harnesses_and_no_saved_preference_merge():
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        claude, codex = os.path.join(tmp, "claude"), os.path.join(tmp, "codex")
        _touch(os.path.join(claude, "p", "a.jsonl"), "{}\n")
        _touch(os.path.join(codex, "p", "b.jsonl"), "{}\n")
        row = doctor.harness_rows(_args(tmp, claude_dir=claude, codex_dir=codex))[-1]
        assert row.status == doctor.OK and "all 2 merged" in row.detail


def test_pinning_a_harness_that_isnt_there_fails_like_the_tui_would():
    # `--harness hermes` on a box with no Hermes reported a tidy "pins Hermes"; drop the
    # `doctor` verb from that same command line and make_store raises SystemExit. Doctor
    # can see that and the TUI can only die of it, so it is BAD, not a warning: opentab
    # AS INVOKED does not work, which is what the exit code is for.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        claude = os.path.join(tmp, "claude")
        _touch(os.path.join(claude, "p", "a.jsonl"), "{}\n")
        args = _args(tmp, "--harness", "hermes", claude_dir=claude)
        row = doctor.harness_rows(args)[-1]
        assert row.status == doctor.BAD
        assert "isn't present" in row.detail and "TUI would exit" in row.detail
        assert "Claude Code" in row.hint
        with contextlib.redirect_stdout(io.StringIO()):
            assert doctor.doctor_command(args) == 1
        # ...but an explicit pin that IS present stays a plain statement of fact.
        row = doctor.harness_rows(_args(tmp, "--harness", "claude", claude_dir=claude))[-1]
        assert row.status == doctor.INFO and row.detail == "--harness claude pins Claude Code"


def test_the_fleet_view_is_judged_by_what_was_pulled_not_by_local_harnesses():
    # `--harness remote` reads the pulled summaries; it does not need a local harness at
    # all. Judging it by available_sources declared "nothing to browse" and exited 1 on
    # a machine whose fleet view works perfectly.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        remotes = os.path.join(tmp, "remotes")
        _touch(os.path.join(remotes, "build-box.json"), json.dumps({"workflows": []}))
        args = _args(tmp, "--harness", "remote", "--remotes", remotes)
        row = doctor.harness_rows(args)[-1]
        assert row.status == doctor.OK and "1 pulled machine" in row.detail
        # A file RemoteStore would skip must not count: `{}`, truncated JSON and an
        # unreadable file all make the real fleet view exit "No machine summaries
        # found", so a green row here would be the report contradicting the app.
        _touch(os.path.join(remotes, "half.json"), '{"workflows": [')
        _touch(os.path.join(remotes, "empty.json"), "{}")
        assert doctor.harness_rows(args)[-1].detail.count("1 pulled machine") == 1
        with contextlib.redirect_stdout(io.StringIO()):
            assert doctor.doctor_command(args) == 0
        # Nothing pulled AND no local harness is the one case that really is broken.
        empty = _args(tmp, "--harness", "remote", "--remotes", os.path.join(tmp, "none"))
        assert doctor.harness_rows(empty)[-1].status == doctor.BAD


def test_no_harness_at_all_is_the_one_harness_failure():
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        row = doctor.harness_rows(_args(tmp))[-1]
        assert row.status == doctor.BAD and "nothing to browse" in row.detail


# --- terminal ------------------------------------------------------------------------------


def test_the_colour_verdict_tracks_util_rather_than_re_deriving_the_rule():
    # Three environments, and in each one the report must agree with the function the
    # renderer actually calls -- the whole point of moving init_color_allowed into util.
    for env, expected in (
        ({}, True),
        ({"HERDR_ENV": "1"}, False),
        ({"HERDR_ENV": "1", "OPENTAB_NO_INIT_COLOR": "0"}, True),
        ({"OPENTAB_NO_INIT_COLOR": "1"}, False),
    ):
        with _clean_env(**env):
            row = next(r for r in doctor.terminal_rows() if r.label == "colours")
            assert ot.util.init_color_allowed() is expected
            assert ("exact" in row.detail) is expected
            assert (row.status == doctor.OK) is expected


def test_a_detected_palette_dropping_host_warns_while_an_explicit_optout_does_not():
    # Set by hand = a decision, and warning about someone's own decision is noise.
    # Detected = they don't know yet, and the colours look wrong.
    with _clean_env(HERDR_ENV="1"):
        assert next(r for r in doctor.terminal_rows() if r.label == "colours").status == doctor.WARN
    with _clean_env(OPENTAB_NO_INIT_COLOR="1"):
        assert next(r for r in doctor.terminal_rows() if r.label == "colours").status == doctor.INFO


class _NoTerminfo:
    # curses with a setupterm that fails, as it does in a fresh process when TERM is
    # unset or names an entry this system has no terminfo for. Driven by monkeypatch and
    # NOT by setting TERM, because ncurses CACHES the setup: once setupterm has
    # succeeded once in a process, a later call with a bogus TERM returns happily
    # (measured). A real `opentab doctor` calls it exactly once, at the top of its own
    # process, so the detection is sound there -- but a test that flips TERM passes
    # alone and fails inside the suite, which is worse than no test.
    error = Exception

    @staticmethod
    def setupterm(fd=None):
        raise _NoTerminfo.error("setupterm: could not find terminal")


def test_a_terminal_with_no_terminfo_entry_is_a_failure_not_a_green_row():
    # TERM unset (a bare `su`, cron, some CI shells) or naming an entry this system has
    # no terminfo for: setupterm raises, and so does initscr(), so the TUI cannot start
    # at all. Folding that into "nothing to say" printed a green TERM row and a green
    # curses row on exactly the machine that cannot run opentab.
    saved = doctor.curses
    try:
        doctor.curses = _NoTerminfo
        with _clean_env(TERM="not-a-real-terminal"):
            rows = doctor.terminal_rows()
            row = next(r for r in rows if r.label == "TERM")
            assert row.status == doctor.BAD and "no terminfo entry" in row.detail
            assert "TERM=xterm-256color" in row.hint
            # ...and the curses row must not then claim to be fine right underneath.
            assert next(r for r in rows if r.label == "curses").status != doctor.OK
        with _clean_env():
            os.environ.pop("TERM", None)
            assert "not set" in next(r for r in doctor.terminal_rows() if r.label == "TERM").hint
    finally:
        doctor.curses = saved


def test_capturing_stdout_is_not_mistaken_for_a_broken_terminal():
    # setupterm() with no argument asks sys.stdout for its fileno, which raises under an
    # in-process capture -- indistinguishable from a missing terminfo entry, and now
    # carrying a BAD. Reporting "your terminal is broken" because the caller redirected
    # stdout would be the worst kind of false alarm.
    if not os.environ.get("TERM"):
        return  # nothing to assert on a host that genuinely has no TERM
    with _clean_env(), contextlib.redirect_stdout(io.StringIO()):
        assert next(r for r in doctor.terminal_rows() if r.label == "TERM").status != doctor.BAD


def test_without_curses_the_report_still_renders_and_names_the_fix():
    # Native Windows Python bundles no curses, so the TUI cannot start at all -- which
    # is exactly when someone runs doctor. It must degrade (no colour count, no ccc)
    # rather than raise, and it must name windows-curses.
    saved = doctor.curses
    try:
        doctor.curses = None
        rows = doctor.terminal_rows()
        term = next(r for r in rows if r.label == "TERM")
        assert "colours" not in term.detail and "ccc" not in term.detail
        curses_row = next(r for r in rows if r.label == "curses")
        assert curses_row.status == doctor.BAD and "windows-curses" in curses_row.hint
    finally:
        doctor.curses = saved


def test_the_terminal_section_always_answers_its_questions():
    with _clean_env():
        labels = [r.label for r in doctor.terminal_rows()]
        for expected in ("TERM", "multiplexer", "colours", "glyphs", "curses"):
            assert expected in labels


def _mux_row(**env):
    with _clean_env(**env):
        return next(r for r in doctor.terminal_rows() if r.label == "multiplexer")


def test_multiplexers_are_detected_by_their_own_markers():
    # A multiplexer is the prime suspect whenever colours or frames come out wrong: it
    # consumes opentab's escapes and re-emits its own, so it -- not the emulator you can
    # see -- is where a palette write goes missing (issue #12 was exactly that). Keyed
    # on the marker each program sets ITSELF, never guessed from TERM, which tmux and
    # screen share.
    assert _mux_row(TMUX="/tmp/tmux-501/default,7,0").detail == "tmux"
    assert _mux_row(STY="4242.pts-0.box").detail == "GNU screen"
    assert _mux_row(DVTM="0.15").detail == "dvtm"
    # $ZELLIJ is literally "0" -- presence is the signal, and env_flag would read that
    # as *off* and miss every zellij user.
    assert _mux_row(ZELLIJ="0").detail == "zellij"
    assert _mux_row().detail == "none detected"  # says the layer was ruled out


def test_the_multiplexer_row_never_prints_the_markers_value():
    # $TMUX carries a socket path (with your uid) and $STY the hostname. This report is
    # written to be pasted in public, so only the NAME goes in -- and they stay out of
    # the environment section for the same reason.
    with _clean_env(TMUX="/tmp/tmux-501/default,7,0", STY="4242.pts-0.secret-host"):
        text = "\n".join(doctor.render(doctor.build_report(_ns())))
        assert "tmux-501" not in text and "secret-host" not in text
        assert "tmux and GNU screen" in text


def test_nested_multiplexers_are_worth_a_warning():
    row = _mux_row(TMUX="/tmp/t,1,0", STY="1.pts-0.box")
    assert row.status == doctor.WARN
    assert "tmux and GNU screen" in row.detail and "2 layers" in row.detail
    assert "outermost" in row.hint


def test_byobu_is_a_qualifier_on_its_backend_not_a_second_layer():
    # byobu is a CONFIGURATION of tmux/screen, not something wrapped around one;
    # counting it would report a plain byobu session as nested.
    row = _mux_row(TMUX="/tmp/t,1,0", BYOBU_BACKEND="tmux")
    assert row.status == doctor.OK and row.detail == "byobu (tmux)"


def test_herdr_states_the_fact_and_leaves_the_warning_to_the_colours_row():
    # One cause, one warning. The colours row owns the finding; this one names the layer.
    with _clean_env(HERDR_ENV="1"):
        rows = doctor.terminal_rows()
        mux = next(r for r in rows if r.label == "multiplexer")
        assert mux.status == doctor.INFO and "discard palette writes" in mux.detail
        assert next(r for r in rows if r.label == "colours").status == doctor.WARN


def test_inside_tmux_the_colour_fix_is_tmuxs_setting_not_the_terminals():
    # TERM=screen inside tmux means 8 colours and the ANSI ramp. The TERM row's generic
    # advice would be actively WRONG here -- setting TERM=xterm-256color inside tmux is
    # the classic anti-pattern -- so the multiplexer row owns the fix and the TERM row
    # drops its hint.
    saved = doctor._terminfo
    try:
        doctor._terminfo = lambda: (8, 0, True)
        with _clean_env(TMUX="/tmp/t,1,0"):
            rows = doctor.terminal_rows()
            term = next(r for r in rows if r.label == "TERM")
            assert term.status == doctor.WARN and term.hint == ""
            mux = next(r for r in rows if r.label == "multiplexer")
            assert mux.status == doctor.WARN and "only 8 colours" in mux.detail
            assert "default-terminal" in mux.hint
        # Outside a multiplexer the TERM row keeps its own advice.
        with _clean_env():
            assert next(r for r in doctor.terminal_rows() if r.label == "TERM").hint
    finally:
        doctor._terminfo = saved


# --- opentab's own files ---------------------------------------------------------------------


def test_an_unreadable_notes_file_is_the_files_section_failure():
    # notes.json is the one artefact opentab cannot rebuild, and a broken one is silent:
    # the writer refuses to clobber it, so every note you make from then on goes nowhere.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        path = ot.notes.notes_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        try:
            row = _by_label([("files", doctor.file_rows(_args(tmp)))], "files", "notes")
            assert row.status == doctor.BAD
            assert "refuses to overwrite" in row.detail and row.hint
        finally:
            os.remove(path)


@contextlib.contextmanager
def _xdg_roots(tmp):
    roots = {
        "XDG_CONFIG_HOME": os.path.join(tmp, "cfg"),
        "XDG_STATE_HOME": os.path.join(tmp, "st"),
        "XDG_DATA_HOME": os.path.join(tmp, "dat"),
        "XDG_CACHE_HOME": os.path.join(tmp, "ch"),
    }
    saved = {k: os.environ.get(k) for k in roots}
    try:
        os.environ.update(roots)
        yield roots
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


def test_the_report_creates_nothing():
    # "It reports; it never repairs." A launch installs keymap.conf and warms the cache;
    # doctor must do neither, or the report describes the state it just made rather than
    # the one that produced the bug.
    with tempfile.TemporaryDirectory() as tmp, _clean_env(), _xdg_roots(tmp) as roots:
        with contextlib.redirect_stdout(io.StringIO()):
            doctor.doctor_command(_args(tmp))
        assert not any(os.path.exists(p) for p in roots.values())


def test_the_report_never_migrates_a_pre_xdg_split_layout():
    # The case a fresh-root test cannot see, and it was real: state_path()/notes_path()
    # answer "where does this live" through paths.migrated(), which MOVES a pre-split
    # copy as a side effect of being asked -- so merely rendering the files section
    # relocated the user's state.json and notes.json out from under the launch doctor
    # was describing. Measured before the fix: both files moved.
    with tempfile.TemporaryDirectory() as tmp, _clean_env(), _xdg_roots(tmp):
        cfg = os.path.join(tmp, "cfg", "opentab")
        _touch(os.path.join(cfg, "state.json"), json.dumps({"source": "claude"}))
        _touch(os.path.join(cfg, "notes.json"), json.dumps({"notes": {"s1": "why $12"}}))
        _touch(os.path.join(cfg, "cache", "claude-abc.json"), "{}")
        before = sorted(os.listdir(cfg))
        sections = doctor.build_report(_args(tmp))
        assert sorted(os.listdir(cfg)) == before  # nothing moved, nothing created

        # ...and the report says so, rather than reading "no state yet / cache empty" on
        # a machine that has both one directory over.
        files = _rows(sections, "files")
        legacy = next(r for r in files if r.label == "legacy")
        assert "state.json" in legacy.detail and "cache" in legacy.detail
        assert "next TUI launch moves them" in legacy.detail
        # Both rows read the copy that actually exists, in the legacy location.
        assert _by_label(sections, "files", "state").detail.startswith(_tilde(cfg))
        notes_row = _by_label(sections, "files", "notes")
        assert notes_row.status == doctor.OK and "1 note" in notes_row.detail


def test_a_fully_migrated_layout_says_nothing_about_legacy_files():
    with tempfile.TemporaryDirectory() as tmp, _clean_env(), _xdg_roots(tmp):
        sections = doctor.build_report(_args(tmp))
        assert not any(r.label == "legacy" for r in _rows(sections, "files"))


# --- privacy ------------------------------------------------------------------------------------


def test_the_default_report_never_prints_the_home_path_or_a_machine_name():
    # It is written to be pasted into a public issue without reading it first.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        remotes = os.path.join(tmp, "remotes")
        _touch(os.path.join(remotes, "build-box-7.json"), "{}")
        args = _args(tmp, "--remotes", remotes)
        text = "\n".join(doctor.render(doctor.build_report(args)))
        home = os.path.expanduser("~")
        assert home not in text and "~" in text
        assert "build-box-7" not in text and "1 machine" in text


def test_redaction_folds_every_home_in_a_value_not_only_a_leading_one():
    # $PI_AGENT_DIR and $OPENCLAW_DIR are comma-separated lists, so a prefix-only fold
    # printed the home path (and therefore the username) of every element after the
    # first -- in the DEFAULT report, the one written to be pasted publicly.
    home = os.path.expanduser("~")
    with tempfile.TemporaryDirectory() as tmp:
        with _clean_env(PI_AGENT_DIR=f"{home}/.pi/a,{home}/.pi/b"):
            text = "\n".join(doctor.render(doctor.build_report(_args(tmp))))
            assert "~/.pi/a,~/.pi/b" in text and home not in text
    # ...and a sibling directory that merely starts with the same characters is not
    # mangled into "~extra".
    assert doctor._tilde(home + "extra/x") == home + "extra/x"
    assert doctor._tilde(home) == "~" and doctor._tilde(home + "/a") == "~/a"


def test_an_unreadable_keymap_is_reported_rather_than_sized():
    # load_user_keymap swallows an OSError and returns pristine defaults with NO
    # warnings -- right for the TUI (a broken conf must not lock you out) and invisible
    # here, so a mode-000 keymap.conf showed a cheerful size row while every binding in
    # it was being ignored.
    if getattr(os, "getuid", lambda: 1)() == 0 or os.name == "nt":
        return  # root reads it regardless, so there is nothing to detect
    with tempfile.TemporaryDirectory() as tmp, _clean_env(), _xdg_roots(tmp):
        keymap = _touch(os.path.join(tmp, "cfg", "opentab", "keymap.conf"), "[browse]\n")
        os.chmod(keymap, 0o000)
        try:
            row = _by_label([("files", doctor.file_rows(_args(tmp)))], "files", "keymap")
            assert row.status == doctor.WARN
            assert "unreadable" in row.detail and "built-in defaults" in row.hint
        finally:
            os.chmod(keymap, 0o644)


def test_full_opts_out_of_the_redaction():
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        remotes = os.path.join(tmp, "remotes")
        _touch(os.path.join(remotes, "build-box-7.json"), "{}")
        args = _args(tmp, "--remotes", remotes)
        text = "\n".join(doctor.render(doctor.build_report(args, full=True)))
        assert "build-box-7" in text
        assert os.path.expanduser("~") in text  # absolute install/python paths


# --- rendering -------------------------------------------------------------------------------------


def test_a_hint_is_set_under_the_detail_column_of_its_own_row():
    sections = [("s", [doctor.Row(doctor.WARN, "abc", "detail here", "do this")])]
    lines = doctor.render(sections)
    row, hint = lines[-2], lines[-1]
    assert hint.index("→") == row.index("detail here")


def test_the_label_column_is_measured_per_section():
    # One column for the whole report lets an environment variable name
    # (COPILOT_OTEL_FILE_EXPORTER_PATH) push every harness detail 20 spaces right of
    # its mark, in a block whose own widest label is "Claude Code".
    sections = [
        ("harnesses", [doctor.Row(doctor.OK, "Codex", "here")]),
        ("environment", [doctor.Row(doctor.INFO, "COPILOT_OTEL_FILE_EXPORTER_PATH", "x")]),
    ]
    lines = doctor.render(sections)
    assert lines[3] == "  ✓ Codex  here"


def test_a_non_utf8_screen_gets_ascii_marks_arrows_and_separators():
    # The marks are the obvious half. The separators are typed into forty f-strings and
    # every hint, so folding only the marks leaves a Linux-console report with tidy
    # +/- verdicts and a replacement blob between every field.
    sections = [
        ("s", [doctor.Row(doctor.WARN, "a", "x · y — z", "h · k"), doctor.Row(doctor.OK, "b", "d")])
    ]
    text = "\n".join(doctor.render(sections, uni=False))
    assert not any(glyph in text for glyph in "✓⚠✗·→—")
    assert "! a" in text and "+ b" in text and "-> h | k" in text
    assert "x | y -- z" in text


# --- the command ---------------------------------------------------------------------------------------


def test_the_command_exits_nonzero_only_for_a_real_failure():
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.doctor_command(_args(tmp))  # no harness anywhere -> BAD
        assert code == 1 and "need attention" in out.getvalue()

        claude = os.path.join(tmp, "claude")
        _touch(os.path.join(claude, "p", "a.jsonl"), "{}\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.doctor_command(_args(tmp, claude_dir=claude))
        assert code == 0 and "need attention" not in out.getvalue()


def test_a_warning_alone_never_moves_the_exit_code():
    # Warnings are "you probably didn't mean this", and failing on them trains everyone
    # to stop reading the exit code.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        claude = os.path.join(tmp, "claude")
        _touch(os.path.join(claude, "p", "a.jsonl"), "{}\n")
        zaly = os.path.join(tmp, "zaly", "sessions")
        _touch(os.path.join(zaly, "ws", "u", "session.jsonl"), "{}\n")
        args = _args(tmp, claude_dir=claude, zaly_dir=zaly)
        sections = doctor.build_report(args)
        assert any(r.status == doctor.WARN for _t, rows in sections for r in rows)
        with contextlib.redirect_stdout(io.StringIO()):
            assert doctor.doctor_command(args) == 0


def test_doctor_is_a_subcommand_that_main_dispatches_without_curses():
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        import sys as _sys

        argv = _sys.argv
        _sys.argv = ["opentab", "doctor", "--claude-dir", os.path.join(tmp, "absent")]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = ot.cli.main()
        finally:
            _sys.argv = argv
        assert code in (0, 1)  # depends on the developer's real machine; it must not raise
        assert out.getvalue().startswith("opentab doctor · ")


def test_the_report_covers_every_section():
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        titles = [t for t, _rows in doctor.build_report(_args(tmp))]
        assert titles == ["opentab", "harnesses", "terminal", "prices", "files", "environment"]


def test_every_harness_key_has_a_row():
    # A backend added to sources without a doctor spec would silently never be reported.
    keys = {spec[0] for spec in doctor._HARNESSES}
    assert keys == set(ot.sources.SOURCE_LABELS) - {"all"}


def test_a_db_that_is_not_opencodes_is_reported_as_such_not_as_missing():
    # available_sources() drops an OpenCode db whose schema isn't OpenCode's -- else one
    # unusable file takes `all` down with a sqlite traceback -- and doctor borrows that
    # verdict as always. But the plain absent-harness wording would then say "not found"
    # about a file sitting right there at the path the row prints, which is the shape of
    # wrongness this report can least afford.
    with tempfile.TemporaryDirectory() as tmp, _clean_env():
        db = os.path.join(tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.execute("create table unrelated (id integer)")
        conn.commit()
        conn.close()
        row = _by_label(doctor.build_report(_args(tmp, db=db)), "harnesses", "OpenCode")
        assert row.status == doctor.WARN
        assert "not an OpenCode database" in row.detail and "not found" not in row.detail
        assert row.hint  # and it says where the real one lives

        # An unreadable one is separated: "not an OpenCode database" about a database
        # nobody can open sends someone hunting for the wrong file.
        os.chmod(db, 0o000)
        try:
            row = _by_label(doctor.build_report(_args(tmp, db=db)), "harnesses", "OpenCode")
        finally:
            os.chmod(db, 0o644)
        assert row.status == doctor.BAD and "cannot be read" in row.detail


def test_redaction_hides_a_windows_username_that_home_cannot_reach():
    # The whole point of --vscode-dir under WSL is to name the WINDOWS-side store, and
    # $HOME there is /home/<you>: the fold never touches /mnt/c/Users/Alice/..., so the
    # Windows username went out verbatim in the default "safe to paste" report. Redacted
    # rather than folded to ~, since it need not be the reader's own profile.
    assert doctor._tilde("/mnt/c/Users/Alice/AppData/Roaming/Code/User") == (
        "/mnt/c/Users/<user>/AppData/Roaming/Code/User"
    )
    assert doctor._tilde("/mnt/d/users/bob/x") == "/mnt/d/users/<user>/x"
    assert doctor._tilde(r"C:\Users\Alice\AppData") == r"C:\Users\<user>\AppData"
    # Shared profiles are not people; folding them would drop the informative half.
    assert doctor._tilde("/mnt/c/Users/Public/Docs") == "/mnt/c/Users/Public/Docs"
    # Not a user directory at all, and --full still opts out for local eyes.
    assert doctor._tilde("/mnt/c/UsersFoo/bar") == "/mnt/c/UsersFoo/bar"
    assert doctor._tilde("/mnt/c/Users/Alice/x", full=True) == "/mnt/c/Users/Alice/x"
    # ...and it reaches the rendered report, wherever such a path enters it.
    with tempfile.TemporaryDirectory() as tmp:
        with _clean_env(OPENCLAW_DIR="/mnt/c/Users/Alice/.openclaw"):
            text = "\n".join(doctor.render(doctor.build_report(_args(tmp))))
        assert "Alice" not in text


def test_redaction_keeps_folding_a_windows_home_to_a_tilde():
    # The Windows-user redaction runs AFTER the $HOME fold, never before: on Windows
    # itself the reader's own home is still the shorter, more useful "~".
    saved = os.name
    home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = os.environ["USERPROFILE"] = r"C:\Users\Alice"
        doctor.os.name = "nt"
        assert doctor._tilde(r"C:\Users\Alice\.config\opentab") == r"~\.config\opentab"
        # A DIFFERENT account on the same box is redacted instead -- still no username.
        assert doctor._tilde(r"C:\Users\Bob\.codex") == r"C:\Users\<user>\.codex"
    finally:
        doctor.os.name = saved
        os.environ.pop("USERPROFILE", None)
        os.environ.pop("HOME", None)
        if home is not None:
            os.environ["HOME"] = home
