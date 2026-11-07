"""Fleet CLI management, compatibility and authored-data safety."""
import contextlib
import io
import json
import os
import shlex
import subprocess
import tempfile
from unittest.mock import patch

from opentab import RemoteStore
from opentab.cli import machines
from opentab.cli import main as cli

from tests._support import _parse
from tests.test_cli import _fake_summary_text


@contextlib.contextmanager
def fleet(document=None):
    with tempfile.TemporaryDirectory() as directory:
        config = os.path.join(directory, "remotes.json")
        if document is not None:
            with open(config, "w", encoding="utf-8") as stream:
                json.dump(document, stream)
        with patch.object(cli, "remotes_config_path", return_value=config), patch.object(
            cli, "default_remotes_dir", return_value=directory
        ):
            yield directory, config


def run(*argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli._run(cli.parse_args(["machines", *argv]))
    return code, stdout.getvalue(), stderr.getvalue()


def read(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def test_machine_alias_namespaces_match_without_second_dispatch_path():
    for old, new, operands in (
        ("pull", "pull", ["a", "name=user@b"]),
        ("remote", "open", []),
        ("export", "export", ["-"]),
        ("forget", "remove", ["a", "b"]),
    ):
        legacy = vars(cli.parse_args([old, *operands]))
        modern = vars(cli.parse_args(["machines", new, *operands]))
        for key in ("command", "machines_action"):
            legacy.pop(key, None)
            modern.pop(key, None)
        assert legacy == modern, (old, legacy, modern)
    assert cli.parse_args(["machines", "pull"]).pull == []


def test_machine_help_hides_aliases_but_keeps_their_own_deprecation_guide():
    parser = cli._build_parser()
    for full in (False, True):
        text = parser.format_help(full=full)
        for old in machines.ALIASES:
            assert not any(line.strip().startswith(old + " ") for line in text.splitlines())
    for old, new in machines.ALIASES.items():
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                cli.parse_args([old, "--help"])
            except SystemExit as exc:
                assert exc.code == 0
        assert f"Deprecated: use opentab machines {new}" in output.getvalue()
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        try:
            parser.parse_args(["invalid-command"])
        except SystemExit as exc:
            assert exc.code == 2
    for old in machines.ALIASES:
        assert repr(old) not in stderr.getvalue()


def test_machine_list_is_offline_versioned_and_reads_cached_metadata():
    document = {
        "version": 1,
        "machines": {
            "a": {"ssh": "user@box", "cmd": "custom"},
            "b": {"url": "https://example.test/x"},
            "future": None,
        },
    }
    with fleet(document) as (directory, config), patch.object(
        machines, "probe", side_effect=AssertionError("network")
    ), patch.object(cli.sources, "make_store", side_effect=AssertionError("sources")):
        with open(os.path.join(directory, "a.json"), "w") as stream:
            json.dump({"opentab_version": "1.34.0", "exported_at": "2026-11-07T12:00:00Z"}, stream)
        before = read(config)
        code, text, errors = run("list", "--json")
        assert code == 0 and not errors
        payload = json.loads(text)
        assert payload["schema_version"] == 1
        a, b = payload["machines"]
        assert a["name"] == "a" and a["custom_cmd"] is True
        assert a["last_pull"] and a["opentab_version"] == "1.34.0"
        assert b["kind"] == "url" and b["last_pull"] is None
        assert read(config) == before
    with fleet():
        assert "machines add" in run("list")[1]


def test_machine_add_no_check_collision_and_local_file_rejection():
    with fleet({"version": 1, "future": True, "machines": {"unknown": None}}) as (
        directory,
        config,
    ), patch.object(machines, "probe", side_effect=AssertionError("network")):
        assert run("add", "box=user@host", "--no-check")[0] == 0
        assert read(config) == {
            "version": 1,
            "future": True,
            "machines": {"unknown": None, "box": {"ssh": "user@host"}},
        }
        assert "NAME=TARGET" in run("add", "box=other", "--no-check")[2]
        for target in ("./summary.json", "name=/tmp/summary.json", "bad=", "x=-oProxyCommand=bad"):
            assert run("add", target, "--no-check")[0] == 1
        assert not os.path.exists(os.path.join(directory, "box.json"))


def test_machine_add_rejects_a_taken_name_before_probing():
    with fleet({"version": 1, "machines": {"box": {"ssh": "old"}}}) as (_, config), patch.object(
        machines, "probe", side_effect=AssertionError("network")
    ):
        before = read(config)
        code, _, errors = run("add", "box=user@new")
        assert code == 1 and "already exists" in errors
        assert read(config) == before


def test_machine_add_keeps_argv_trace_cmd_when_the_shell_text_would_change_it():
    from opentab import remote_content

    for path, keeps in (
        ("/opt/bin/opentab", False),
        ("/opt/team's/bin/opentab", True),
        ("/opt/$x/bin/opentab", True),
        ("/opt/my tools/opentab", True),
    ):
        result = {
            "status": "reachable",
            "error": None,
            "opentab_version": "1.34.0",
            "cmd": shlex.quote(path) + " --export -",
            "trace_cmd": [path],
        }
        with fleet() as (directory, config), patch.object(machines, "probe", return_value=result):
            assert run("add", "box")[0] == 0
            entry = read(config)["machines"]["box"]
            assert ("trace_cmd" in entry) is keeps, (path, entry)
            with patch.object(remote_content.paths, "config_dir", return_value=directory):
                assert remote_content.saved_connections()["box"].command == shlex.quote(path)


def test_machine_add_reloads_after_probe_and_keeps_concurrent_edits():
    with fleet() as (_, config):

        def probe(*_args):
            with open(config, "w") as stream:
                json.dump(
                    {"version": 1, "future": 7, "machines": {"other": {"ssh": "other"}}}, stream
                )
            return {"status": "reachable", "cmd": "/opt/bin/opentab --export -"}

        with patch.object(machines, "probe", side_effect=probe):
            assert run("add", "box")[0] == 0
        saved = read(config)
        assert saved["future"] == 7 and set(saved["machines"]) == {"box", "other"}
        assert saved["machines"]["box"]["cmd"] == "/opt/bin/opentab --export -"


def test_machine_failed_probe_never_saves_and_corrupt_or_newer_config_is_preserved():
    with fleet() as (_, config), patch.object(
        machines, "probe", return_value={"status": "opentab missing", "error": "install opentab-ai"}
    ):
        assert run("add", "box")[0] == 1
        assert not os.path.exists(config)
        for data in ("{broken", "[]", '{"machines": []}', '{"version": 2, "machines": {}}'):
            with open(config, "w") as stream:
                stream.write(data)
            assert run("add", "box", "--no-check")[0] == 1
            with open(config) as stream:
                assert stream.read() == data


def test_machine_rename_moves_summary_preserves_fields_and_rolls_back_failed_publish():
    document = {
        "version": 1,
        "future": True,
        "machines": {"a": {"ssh": "box", "future": [1]}, "unknown": None},
    }
    with fleet(document) as (directory, config):
        old = os.path.join(directory, "a.json")
        with open(old, "w") as stream:
            json.dump({"label": "original", "workflows": []}, stream)
        with patch.object(cli, "_atomic_text", side_effect=OSError("disk full")):
            assert run("rename", "a", "b")[0] == 1
        assert os.path.exists(old) and read(config) == document
        assert run("rename", "a", "b")[0] == 0
        assert not os.path.exists(old)
        assert read(os.path.join(directory, "b.json"))["label"] == "original"
        assert read(config)["machines"] == {"b": {"ssh": "box", "future": [1]}, "unknown": None}
        assert run("rename", "b", "unknown")[0] == 1
        assert run("rename", "missing", "new")[0] == 1
        assert run("remove", "b")[0] == 0
        assert run("remove", "b")[0] == 1
        assert read(config)["machines"] == {"unknown": None}


def test_machine_status_reports_failures_versions_and_cache_age_without_writing():
    document = {"version": 1, "machines": {"a": {"ssh": "a"}, "b": {"ssh": "b"}}}

    def probe(_backend, name, _entry):
        return {
            "status": "reachable" if name == "a" else "auth required",
            "opentab_version": "1.21.0" if name == "a" else None,
            "error": None if name == "a" else "authentication required",
        }

    with fleet(document) as (directory, config), patch.object(machines, "probe", side_effect=probe):
        with open(os.path.join(directory, "a.json"), "w") as stream:
            json.dump({"opentab_version": "1.21.0"}, stream)
        os.utime(os.path.join(directory, "a.json"), (0, os.path.getmtime(config) - 120))
        code, text, _ = run("status", "--json")
        a, b = json.loads(text)["machines"]
        assert code == 1 and a["version_skew"] is True
        assert isinstance(a["cache_age_seconds"], int) and a["cache_age_seconds"] >= 120
        assert b["status"] == "auth required" and b["cache_age_seconds"] is None
        assert read(config) == document
        assert run("status", "a")[0] == 0
        assert run("status", "missing")[0] == 1


def test_ssh_probe_batch_mode_discovers_absolute_command_and_classifies_safe_errors():
    for stdout, cmd in (
        ("/opt/opentab\nopentab 1.34.0\n", "/opt/opentab --export -"),
        ("/usr/bin/opentab\nopentab 1.34.0\ndefault\n", None),
    ):
        with patch.object(
            machines.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout, "")
        ) as call:
            result = machines._probe_ssh({"ssh": "box"})
            assert result["status"] == "reachable" and result.get("cmd") == cmd
            argv = call.call_args.args[0]
            assert "BatchMode=yes" in argv and "ConnectTimeout=8" in argv
            # A non-POSIX login shell (fish) only has to run `/bin/sh -c '...'`.
            assert argv[-1] == "/bin/sh -c " + shlex.quote(machines._SSH_PROBE)
            assert call.call_count == 1
    with patch.object(
        machines.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], 0, "opentab 1.34.0\n", ""),
    ) as call:
        result = machines._probe_ssh({"ssh": "box", "cmd": "/opt/bin/opentab --export -"})
        assert call.call_args.args[0][-1] == "/opt/bin/opentab --version"
        assert result == {"status": "reachable", "error": None, "opentab_version": "1.34.0"}
    # Login-shell noise before the record never decides the saved path or version.
    noisy = "/banner/opentab\nopentab banner\n/real/opentab\nopentab 1.34.0\n"
    with patch.object(
        machines.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, noisy, "")
    ):
        result = machines._probe_ssh({"ssh": "box"})
        assert result["opentab_version"] == "1.34.0"
        assert result["cmd"] == "/real/opentab --export -"
    noisy = "/usr/bin/opentab\nopentab 1.34.0\ndefault\nbye from .bashrc\n"
    with patch.object(
        machines.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, noisy, "")
    ):
        assert "cmd" not in machines._probe_ssh({"ssh": "box"})
    for code, stderr, status in (
        (255, "Permission denied secret", "auth required"),
        (127, "secret", "opentab missing"),
        (255, "unreachable secret", "error"),
    ):
        with patch.object(
            machines.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], code, "", stderr),
        ):
            result = machines._probe_ssh({"ssh": "box"})
            assert result["status"] == status and "secret" not in str(result)
    with patch.object(machines.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 30)):
        assert machines._probe_ssh({"ssh": "box"})["status"] == "error"


def test_url_probe_validates_export_before_saving():
    with fleet() as (_, config):
        for payload, code in (
            ({"opentab_export": 2, "workflows": []}, 0),
            ({"workflows": []}, 1),
            ([], 1),
        ):
            with patch.object(cli, "_fetch_summary", return_value=json.dumps(payload)):
                result = run("add", f"url{code}=https://example.test/summary")
                assert result[0] == code
        assert set(read(config)["machines"]) == {"url0"}


def test_machine_open_validation_and_range_errors_precede_io():
    with fleet(), patch.object(cli.sources, "make_store", side_effect=AssertionError("source")):
        for argv in (
            ("open", "missing.json"),
            ("pull", "--days", "-4"),
            ("open", "--since", "nonsense"),
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    cli.parse_args(["machines", *argv])
                except SystemExit as exc:
                    assert exc.code == 2
                else:
                    raise AssertionError(argv)


def test_machine_rename_preserves_resume_and_turn_connection_provenance():
    from opentab import remote_content
    from opentab.persistence import paths

    with fleet(
        {
            "version": 1,
            "machines": {"old": {"ssh": "user@box", "cmd": "/opt/bin/opentab machines export -"}},
        }
    ) as (directory, _):
        with open(os.path.join(directory, "old.json"), "w") as stream:
            stream.write(_fake_summary_text("box", ["s1"]))
        with patch.object(paths, "config_dir", return_value=directory):
            before = remote_content.saved_connections()
            assert "old" in before
            assert run("rename", "old", "new")[0] == 0
            after = remote_content.saved_connections()
            assert "new" in after and "old" not in after
            assert cli._make_ssh_targets_fn()() == {"new": "user@box"}
            # A pulled summary routes by its filename, so the moved file joins "new".
            store = RemoteStore(directory, _parse([]))
            assert store.machine_meta["box"]["key"] == "new"


def test_machine_concurrent_writers_merge_under_one_lock():
    from concurrent.futures import ThreadPoolExecutor

    with fleet() as (_, config):

        def add(index):
            def edit(saved):
                saved[str(index)] = {"ssh": f"box{index}"}

            machines.update_remotes(cli, edit)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add, range(24)))
        assert len(read(config)["machines"]) == 24


def test_ssh_probe_script_finds_off_path_and_reports_broken_install():
    if os.name == "nt":
        import unittest

        raise unittest.SkipTest("POSIX remote shell fixture")
    with tempfile.TemporaryDirectory() as directory:
        bindir = os.path.join(directory, ".local", "bin")
        os.makedirs(bindir)
        binary = os.path.join(bindir, "opentab")
        shell = os.path.join(directory, "shell")
        with open(shell, "w") as stream:
            stream.write("#!/bin/sh\nexit 1\n")
        os.chmod(shell, 0o700)
        env = {**os.environ, "HOME": directory, "PATH": "/nonexistent", "SHELL": shell}
        for body, code in (("echo 'opentab 1.34.0'", 0), ("exit 1", 126)):
            with open(binary, "w") as stream:
                stream.write("#!/bin/sh\n" + body + "\n")
            os.chmod(binary, 0o700)
            # Through the exact quoting ssh sends to the remote login shell.
            result = subprocess.run(
                ["/bin/sh", "-c", "/bin/sh -c " + shlex.quote(machines._SSH_PROBE)],
                env=env,
                capture_output=True,
                text=True,
            )
            assert result.returncode == code, result
            if code == 0:
                assert result.stdout.splitlines() == [binary, "opentab 1.34.0"]
                assert "default" not in result.stdout
        os.unlink(binary)
        result = subprocess.run(
            ["/bin/sh", "-c", machines._SSH_PROBE], env=env, capture_output=True, text=True
        )
        assert result.returncode == 127
