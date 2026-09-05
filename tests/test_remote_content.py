import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from opentab import remote_content as rc
from opentab.models import API_SCHEMA_VERSION, SessionRef
from opentab.service import OpenTabService, ServiceError
from opentab.stores.remote import RemoteStore, _clean_turn, build_export

from tests._support import _parse, workflow
from tests.test_stores_remote import _FakeExtrasStore, _summary, _turn, _write


def _session(sid="s1", source="OpenCode"):
    row = workflow(sid, "2026-07-15")
    row.source = source
    return row


@contextlib.contextmanager
def _managed(entry=None, rows=None):
    with tempfile.TemporaryDirectory() as root:
        cache, config = os.path.join(root, "cache"), os.path.join(root, "config")
        directory = os.path.join(cache, "remotes")
        os.makedirs(directory)
        os.makedirs(config)
        _write(
            config,
            "remotes.json",
            {
                "version": 1,
                "machines": {"saved/box": {"ssh": "user@actual-host"} if entry is None else entry},
            },
        )
        payload = _summary("display-not-ssh", [_session()])
        payload["turns"] = {"s1": [_turn()] if rows is None else rows}
        filename = _write(directory, "saved%2Fbox.json", payload)
        with patch("opentab.paths.cache_dir", return_value=cache), patch(
            "opentab.paths.config_dir", return_value=config
        ):
            yield RemoteStore(directory), directory, config, filename


def _envelope(data):
    return {"schema_version": API_SCHEMA_VERSION, "ok": True, "data": data}


def _service_turn(row=None, key="native-turn-key"):
    raw = dict(_turn() if row is None else row, content_key=key)
    store = _FakeExtrasStore([_session()], [], turns={"s1": [raw]})
    service = OpenTabService(store, SimpleNamespace(no_state=True), allow_raw_content=True)
    return service.session_turns("s1", include_content_keys=True)["turns"][0]


def _replies(rows=None, events=None):
    session = SessionRef("real-host-not-export-label", "opencode", "s1").encode()
    return [
        _envelope({"session_key": session, "turns": [_service_turn()] if rows is None else rows}),
        _envelope(
            {
                "session_key": session,
                "content_key": "native-turn-key",
                "content": (
                    [{"kind": "text", "text": "private narration", "dropped": 0}]
                    if events is None
                    else events
                ),
            }
        ),
    ]


@contextlib.contextmanager
def _error(text=None, kind: type[Exception] = rc.RemoteTraceError):
    try:
        yield
    except kind as exc:
        assert "SECRET" not in str(exc)
        if text:
            assert text in str(exc), str(exc)
    else:
        raise AssertionError("expected error")


def test_remote_capabilities_keys_and_exports_are_network_free():
    with _managed() as (store, *_), patch.object(rc, "_ssh_json") as transport:
        assert store.supports_turn_content("s1")
        assert not store.supports_turn_content("missing")
        rows = store.message_timeline("s1")
        key = rows[0]["content_key"]
        assert key.startswith("remote:")
        assert "has_text" not in rows[0] and "has_reasoning" not in rows[0]
        assert store.turn_content("s1") == {}
        request = store.remote_trace_request("s1", key)
        assert callable(request)
        exported = build_export(store, "another-export")
        assert exported["opentab_export"] == 2
        turn = exported["turns"]["s1"][0]
        assert not {"content_key", "has_text", "has_reasoning"} & turn.keys()
        assert "remote:" not in json.dumps(exported)
        rows[0]["time"] = "modified by UI"
        rows[0]["tools"].append("another tool")
        assert store.message_timeline("s1")[0]["content_key"] == key
        assert "another tool" not in store.message_timeline("s1")[0]["tools"]
        transport.assert_not_called()


def test_remote_request_captures_snapshot_and_connection_and_uses_native_key():
    with _managed() as (store, directory, config, filename):
        key = store.message_timeline("s1")[0]["content_key"]
        request = store.remote_trace_request("s1", key)
        # A reload, config edit, and UI mutation cannot redirect an already-built worker.
        _write(config, "remotes.json", {"machines": {"saved/box": {"ssh": "other-host"}}})
        changed = _summary("changed", [_session(source="Codex")])
        changed["turns"] = {"s1": [_turn(pid="changed")]}
        _write(directory, os.path.basename(filename), changed)
        store._load()
        replies = _replies(rows=[_service_turn(_turn(pid="new"), "another"), _service_turn()])
        with patch.object(rc, "_ssh_json", side_effect=replies) as transport:
            assert request(threading.Event())[key][0]["text"] == "private narration"
        first, second = transport.call_args_list
        assert first.args[0] == rc.TraceConnection("user@actual-host", "opentab")
        assert first.args[1][-1] == "s1"
        assert "--include-content-keys" in first.args[1]
        assert "--include-prompts" not in first.args[1]
        assert second.args[1][-1] == "native-turn-key"
        assert second.args[1][-2] == replies[0]["data"]["session_key"]
        assert first.args[3] == second.args[3]  # one timeout budget for both commands
        for call in (first, second):
            assert "--no-state" in call.args[1] and "--allow-raw-content" in call.args[1]
            assert call.args[1][call.args[1].index("--source") + 1] == "opencode"
            parsed = _parse(list(call.args[1]))
            assert parsed.source == "opencode" and parsed.allow_raw_content and parsed.no_state
        assert _parse(list(second.args[1])).content_key == "native-turn-key"


def test_remote_winning_file_provenance_survives_duplicate_machine_labels():
    with _managed() as (store, directory, config, filename):
        duplicate = _summary(
            "display-not-ssh",
            [
                _session(source="Codex"),
                _session("s2"),
            ],
        )
        duplicate["turns"] = {"s1": [_turn(pid="loser")], "s2": [_turn()]}
        _write(directory, "zz.json", duplicate)
        _write(
            config,
            "remotes.json",
            {
                "machines": {
                    "saved/box": {"ssh": "winner"},
                    "zz": {"ssh": "second"},
                }
            },
        )
        store = RemoteStore(directory)
        assert store.machine_meta["display-not-ssh"]["key"] == "zz"
        assert store._provenance["s1"] == (filename, "saved/box", "OpenCode")
        for sid, target in (("s1", "winner"), ("s2", "second")):
            key = store.message_timeline(sid)[0]["content_key"]
            with patch.object(
                rc, "_ssh_json", side_effect=rc.RemoteTraceError("stop")
            ) as transport:
                with _error("stop"):
                    store.remote_trace_request(sid, key)(threading.Event())
                assert transport.call_args.args[0].target == target
        excluded = RemoteStore(directory, exclude_ids={"s1"})
        assert "s1" not in excluded._provenance
        assert not excluded.supports_turn_content("s1")


def test_remote_config_and_arbitrary_files_fail_closed():
    denied = [
        {},
        {"url": "https://example.test/summary"},
        {"ssh": "host", "url": "https://example.test/summary"},
        {"ssh": "host", "cmd": "custom export -"},
        {"ssh": "host", "trace_cmd": "opentab"},
        {"ssh": "host", "trace_cmd": []},
        {"ssh": "host", "trace_cmd": ["opentab", 7]},
        {"ssh": "host", "trace_cmd": None},
        {"ssh": "-oProxyCommand=bad"},
        {"ssh": "host\nsecret"},
        {"ssh": "https://example.test"},
    ]
    for entry in denied:
        with _managed(entry) as (store, *_), patch.object(rc, "_ssh_json") as transport:
            assert not store.supports_turn_content("s1"), entry
            assert "content_key" not in store.message_timeline("s1")[0]
            with _error("unavailable"):
                store.remote_trace_request("s1", "anything")
            transport.assert_not_called()
    with _managed() as (store, directory, _, filename):
        for source in (filename, [filename], [directory], os.path.dirname(directory)):
            offline = RemoteStore(source)
            assert not offline.supports_turn_content("s1")
        store.demo = True
        assert not store.supports_turn_content("s1")
        assert "content_key" not in store.message_timeline("s1")[0]


def test_remote_unknown_harness_symlink_and_malformed_config_denied():
    with _managed() as (_, directory, config, filename):
        payload = _summary("display", [_session(source="Future Harness")])
        payload["turns"] = {"s1": [_turn()]}
        _write(directory, os.path.basename(filename), payload)
        assert not RemoteStore(directory).supports_turn_content("s1")
        payload["workflows"][0]["source"] = "OpenCode"
        outside = _write(config, "outside.json", payload)
        os.remove(filename)
        os.symlink(outside, filename)
        assert not RemoteStore(directory).supports_turn_content("s1")
        for data in ([], {"version": 2, "machines": {}}, {"machines": []}):
            _write(config, "remotes.json", data)
            assert rc.saved_connections() == {}
        with open(os.path.join(config, "remotes.json"), "w") as stream:
            stream.write("not JSON secret")
        assert rc.saved_connections() == {}


def test_remote_trace_matching_rejects_stale_and_ambiguous_rows_and_keys():
    variants = [
        [],
        [_service_turn(_turn(pid="different"))],
        [_service_turn(), _service_turn(key="duplicate")],
        [_service_turn(), _service_turn(_turn(pid="different"))],
        [dict(_service_turn(), input_tokens=501)],
        [dict(_service_turn(), content_key="")],
        [dict(_service_turn(), tokens=True)],
        [dict(_service_turn(), tools="Bash")],
        [dict(_service_turn(), recorded_cost_usd=float("nan"))],
        [None],
    ]
    with _managed() as (store, *_):
        key = store.message_timeline("s1")[0]["content_key"]
        for rows in variants:
            with patch.object(rc, "_ssh_json", side_effect=_replies(rows=rows)) as transport:
                with _error():
                    store.turn_content("s1", key)
                assert transport.call_count == 1
        for bad in ("ordinal:0", "remote:old:0", key + "0", key.rsplit(":", 1)[0] + ":-1"):
            with _error("key is stale"):
                store.remote_trace_request("s1", bad)
    with _managed(rows=[_turn(), _turn()]) as (store, *_):
        with _error("ambiguous"):
            store.remote_trace_request("s1", store.message_timeline("s1")[0]["content_key"])


def test_remote_response_envelope_identity_and_event_validation():
    bad_first = [
        None,
        [],
        {},
        {"ok": True, "data": {}},
        {"schema_version": "future", "ok": True, "data": {}},
        {"schema_version": "1", "ok": False, "error": {"message": "secret"}},
    ]
    for ref in (
        "garbage",
        SessionRef("host", "codex", "s1").encode(),
        SessionRef("host", "opencode", "wrong-id").encode(),
    ):
        bad_first.append(_envelope({"session_key": ref, "turns": [_service_turn()]}))
    with _managed() as (store, *_):
        key = store.message_timeline("s1")[0]["content_key"]
        for reply in bad_first:
            with patch.object(rc, "_ssh_json", return_value=reply):
                with _error():
                    store.turn_content("s1", key)
        for events in (
            {},
            [None],
            [{"kind": "surprise"}],
            [{"kind": "text", "text": []}],
            [{"kind": "text"}],
            [{"kind": "text", "text": "hi", "dropped": True}],
            [{"kind": "tool", "name": "bash", "params": [["command", {}]]}],
            [{"kind": "text", "text": "hi", "extra": "secret"}],
        ):
            with patch.object(rc, "_ssh_json", side_effect=_replies(events=events)):
                with _error():
                    store.turn_content("s1", key)
        for field, value in (
            ("content_key", "wrong"),
            ("session_key", SessionRef("changed-host", "opencode", "s1").encode()),
        ):
            replies = _replies()
            replies[1]["data"][field] = value
            with patch.object(rc, "_ssh_json", side_effect=replies):
                with _error("identity"):
                    store.turn_content("s1", key)
        with patch.object(rc, "MAX_EVENTS", 0), patch.object(
            rc, "_ssh_json", side_effect=_replies()
        ):
            with _error("invalid content"):
                store.turn_content("s1", key)


class _Process:
    def __init__(self, stdout=b"{}", stderr=b"", *, running=False, stubborn=False, code=0):
        self.stdout, self.stderr = io.BytesIO(stdout), io.BytesIO(stderr)
        self.returncode = None if running else code
        self.stubborn = stubborn
        self.terminated = self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.stubborn:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("secret command", timeout or 0)
        return self.returncode


def test_ssh_transport_quotes_argv_and_bounds_both_pipes():
    argv = ["/path with spaces/opentab", "--db", "/data/db"]
    connection = rc.TraceConnection("ssh://user@host:2222", rc._argv_prefix(argv))
    args = ("sessions", "content", "--", "id;bad", "$(bad)")
    with patch.object(rc.subprocess, "Popen", return_value=_Process()) as popen:
        assert rc._ssh_json(connection, args, threading.Event(), time.monotonic() + 1) == {}
        command = popen.call_args.args[0]
        assert command[-2] == connection.target and command[-3] == "--"
        assert shlex.split(command[-1]) == [*argv, *args]
        assert popen.call_args.kwargs["stdin"] == subprocess.DEVNULL
    for process in (
        _Process(stdout=b"x" * 101, running=True),
        _Process(stderr=b"secret" * 20, running=True),
    ):
        with patch.object(rc.subprocess, "Popen", return_value=process), patch.object(
            rc, "MAX_STDOUT", 100
        ), patch.object(rc, "MAX_STDERR", 100):
            with _error("size limit"):
                rc._ssh_json(connection, args, threading.Event(), time.monotonic() + 1)
        assert process.terminated and process.stdout.closed and process.stderr.closed


def test_ssh_transport_cancellation_timeout_and_kill_escalation():
    connection = rc.TraceConnection("host", ("opentab",))
    cancelled = threading.Event()
    cancelled.set()
    with patch.object(rc.subprocess, "Popen") as popen:
        with _error("cancelled"):
            rc._ssh_json(connection, (), cancelled, time.monotonic() + 1)
        popen.assert_not_called()
    for cancel in (True, False):
        event = threading.Event()
        process = _Process(running=True, stubborn=True)
        timer = threading.Timer(0.04, event.set) if cancel else None
        if timer:
            timer.start()
        try:
            with patch.object(rc.subprocess, "Popen", return_value=process):
                with _error("cancelled" if cancel else "timed out"):
                    rc._ssh_json(connection, (), event, time.monotonic() + (1 if cancel else 0.04))
        finally:
            if timer:
                timer.join()
        assert process.terminated and process.killed
        assert process.stdout.closed and process.stderr.closed


def test_ssh_transport_sanitizes_failures_and_rejects_malformed_json():
    connection = rc.TraceConnection("host", ("opentab",))
    for raw in (
        b"secret not json",
        b"\xff",
        b'{"ok":true,"ok":false}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"{}{}",
        b"[" * 2000,
    ):
        with patch.object(rc.subprocess, "Popen", return_value=_Process(stdout=raw)):
            with _error("invalid JSON"):
                rc._ssh_json(connection, (), threading.Event(), time.monotonic() + 1)
    with patch.object(
        rc.subprocess, "Popen", return_value=_Process(stderr=b"SECRET\x1b[31m", code=1)
    ):
        with _error("command failed"):
            rc._ssh_json(connection, (), threading.Event(), time.monotonic() + 1)
    with patch.object(rc.subprocess, "Popen", side_effect=OSError("SECRET")):
        with _error("Could not start SSH"):
            rc._ssh_json(connection, (), threading.Event(), time.monotonic() + 1)


def test_remote_service_and_mcp_raw_content_gates_precede_transport():
    from opentab.mcp import McpServer

    with _managed() as (store, *_), patch.object(
        rc, "_ssh_json", side_effect=_replies()
    ) as transport:
        service = OpenTabService(store, SimpleNamespace(no_state=True))
        turns = service.session_turns("s1")["turns"]
        assert "content_key" not in turns[0]
        key = store.message_timeline("s1")[0]["content_key"]
        with _error("--allow-raw-content", ServiceError):
            service.session_content("s1", key)
        with _error("--allow-raw-content", ServiceError):
            service.session_turns("s1", include_content_keys=True)
        service.allow_raw_content = True
        server = McpServer(service)
        with _error("confirm_raw", ServiceError):
            server.call_tool(
                "opentab_get_session_content",
                {"session": "s1", "content_key": key, "confirm_raw": False},
            )
        transport.assert_not_called()
        response = service.session_content("s1", key)
        assert response["content_key"] == key
        assert response["content"][0]["text"] == "private narration"
        assert transport.call_count == 2


def test_remote_custom_trace_command_prefix_is_explicit_and_immutable():
    with _managed(
        {"ssh": "host", "cmd": "custom export", "trace_cmd": ["uv", "run", "opentab"]}
    ) as (store, *_):
        assert store.supports_turn_content("s1")
        assert store._trace_connections["saved/box"].command == "uv run opentab"


def test_remote_trace_prefix_derives_from_the_pull_command():
    # The shapes `opentab pull` saves and users hand-write must not need a second copy.
    for command, prefix in (
        ("opentab --export -", "opentab"),
        ("$HOME/.local/bin/opentab --export -", "$HOME/.local/bin/opentab"),
        ("/usr/bin/opentab export --label box -", "/usr/bin/opentab"),
        ("uv run opentab --export - --days 30", "uv run opentab"),
        ("NAME=value opentab --export=-", "NAME=value opentab"),
        ("opentab-ai --export -", "opentab-ai"),
        ("  opentab   --export   -  ", "opentab"),
    ):
        assert rc._derived_prefix(command) == prefix, command
    # A shell string OpenTab cannot take apart keeps needing an explicit trace_cmd,
    # and nothing may be spliced out of the middle of a compound command.
    for command in (
        "opentab --export - | gzip",
        "cd /srv && opentab --export -",
        "opentab --export -; rm -rf /tmp/x",
        "opentab --db /data/db --export -",  # an unrecognized flag is not dropped
        "custom export -",
        "bash -lc 'opentab --export -'",
        "$(which opentab) --export -",
        "'/opt/my apps/opentab' --export -",
        "opentab --export - > /tmp/out",
        "--export -",
        "opentab --export - &",
        "",
        None,
        ["opentab"],
        "x" * (rc.MAX_KEY + 1) + " opentab --export -",
    ):
        assert rc._derived_prefix(command) == "", command


def test_remote_derived_prefix_runs_and_keeps_arguments_quoted():
    with _managed({"ssh": "host", "cmd": "$HOME/.local/bin/opentab --export -"}) as (store, *_):
        assert store.supports_turn_content("s1")
        connection = store._trace_connections["saved/box"]
        assert connection.command == "$HOME/.local/bin/opentab"
        args = ("sessions", "turns", "--", "id;bad")
        with patch.object(rc.subprocess, "Popen", return_value=_Process()) as popen:
            rc._ssh_json(connection, args, threading.Event(), time.monotonic() + 1)
        # The prefix reaches the remote shell verbatim so $HOME still expands there,
        # while every argument OpenTab appends stays quoted.
        remote = popen.call_args.args[0][-1]
        assert remote.startswith("$HOME/.local/bin/opentab ")
        assert shlex.split(remote)[1:] == list(args)


def test_remote_unconfigured_ssh_machine_reports_a_cause_instead_of_nothing():
    # A cmd OpenTab will not take apart is the one denial the user can fix, so it
    # must name itself rather than leaving Enter as a silent no-op.
    with _managed({"ssh": "host", "cmd": "bash -lc 'opentab --export -'"}) as (store, *_):
        assert not store.supports_turn_content("s1")
        assert "trace_cmd" in store.trace_unavailable("s1")
    # Everything unavailable by design stays quiet: there is nothing to configure.
    for entry, sid in (
        ({"url": "https://example.test/summary"}, "s1"),
        ({"ssh": "host"}, "s1"),
        ({"ssh": "host", "cmd": "bash -lc 'opentab --export -'"}, "missing"),
    ):
        with _managed(entry) as (store, *_):
            assert store.trace_unavailable(sid) == "", entry
    with _managed({"ssh": "host", "cmd": "weird"}) as (store, *_):
        path, key, _ = store._provenance["s1"]
        store._provenance["s1"] = (path, key, "Copilot")  # a harness with no trace reader
        assert store.trace_unavailable("s1") == ""


def test_remote_keys_survive_service_reopen_but_not_changed_snapshots():
    with _managed() as (store, directory, _, filename):
        key = store.message_timeline("s1")[0]["content_key"]
        reopened = RemoteStore(directory)
        assert reopened.message_timeline("s1")[0]["content_key"] == key
        with patch.object(rc, "_ssh_json", side_effect=_replies()):
            assert reopened.turn_content("s1", key)[key]
        payload = _summary("display-not-ssh", [_session()])
        payload["turns"] = {"s1": [_turn(pid="changed")]}
        _write(directory, os.path.basename(filename), payload)
        reopened = RemoteStore(directory)
        with _error("stale"):
            reopened.remote_trace_request("s1", key)


def test_remote_harness_allowlist_and_sparse_snapshot_denial():
    for source, harness in (
        ("OpenCode", "opencode"),
        ("Claude Code", "claude"),
        ("Codex", "codex"),
        ("Hermes", "hermes"),
        ("Pi", "pi"),
        ("Omp", "omp"),
        ("OpenClaw", "openclaw"),
        ("Gemini", "gemini"),
        ("Zaly", "zaly"),
    ):
        snapshot = rc.trace_snapshot("/cache/box.json", "box", source, "s1", [_clean_turn(_turn())])
        assert snapshot is not None
        assert snapshot.harness == harness
    for source in ("", "all", "remote", "CSV", "future"):
        assert (
            rc.trace_snapshot("/cache/box.json", "box", source, "s1", [_clean_turn(_turn())])
            is None
        )
    snapshot = rc.trace_snapshot("/cache/box.json", "box", "OpenCode", "s1", [_clean_turn({})])
    assert snapshot is not None
    with _error("identity is missing"):
        rc.trace_request(
            snapshot, rc.TraceConnection("host", ("opentab",)), snapshot.content_key(0)
        )


def test_ssh_transport_cancels_real_blocked_pipes_without_connecting():
    # Exercise OS pipe semantics, not just BytesIO: the fake SSH is a local child.
    real_popen = subprocess.Popen
    children = []

    def spawn(_argv, **kwargs):
        child = real_popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
        children.append(child)
        return child

    started = time.monotonic()
    with patch.object(rc.subprocess, "Popen", side_effect=spawn):
        with _error("timed out"):
            rc._ssh_json(
                rc.TraceConnection("not-contacted", ("opentab",)),
                (),
                threading.Event(),
                started + 0.1,
            )
    assert time.monotonic() - started < 2
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed


def test_remote_valid_events_and_count_limits():
    with _managed() as (store, *_):
        key = store.message_timeline("s1")[0]["content_key"]
        with patch.object(rc, "MAX_TURNS", 0), patch.object(
            rc, "_ssh_json", side_effect=_replies()
        ):
            with _error("invalid timeline"):
                store.turn_content("s1", key)
        events = [
            {"kind": "reasoning", "text": "thinking", "dropped": 0},
            {
                "kind": "tool",
                "name": "bash",
                "args": "ls",
                "params": [["timeout", "10"]],
                "output": "result",
                "output_dropped": 0,
                "status": "completed",
            },
        ]
        with patch.object(rc, "_ssh_json", side_effect=_replies(events=events)):
            assert store.turn_content("s1", key) == {key: events}


def test_remote_trace_reads_real_cli_json_and_retained_subagent_content():
    import sqlite3

    from opentab.stores.opencode import Store

    from tests._support import _write_opencode_db_with_turns

    real_popen = subprocess.Popen
    with _managed() as (_, directory, config, filename):
        db = os.path.join(config, "records.db")
        _write_opencode_db_with_turns(db)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "insert into part values (?,?,?,?)",
                ("text1", "m3", "s2", json.dumps({"type": "text", "text": "subagent trace"})),
            )
        local = Store(db, _parse([]))
        try:
            payload = build_export(local, "display-label")
        finally:
            local.conn.close()
        _write(directory, os.path.basename(filename), payload)
        store = RemoteStore(directory)
        rows = store.message_timeline("s1")
        selected = next(row for row in rows if row["depth"] == 1)
        invocations = []

        def local_cli(argv, **kwargs):
            # Only substitute the SSH executable. The real JSON commands, parser,
            # source reader, envelopes, and pipe handling all run in child processes.
            command = shlex.split(argv[-1])
            assert command[0] == "opentab"
            invocations.append(command)
            command = [sys.executable, "-m", "opentab", *command[1:3], "--db", db, *command[3:]]
            env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(rc.__file__)))
            return real_popen(command, env=env, **kwargs)

        with patch.object(rc.subprocess, "Popen", side_effect=local_cli):
            content = store.turn_content("s1", selected["content_key"])
        assert content[selected["content_key"]][0]["text"] == "subagent trace"
        assert len(invocations) == 2
        assert invocations[1][-1] == "m3"


def test_remote_preview_keeps_arguments_literal_and_bounds_content():
    from opentab.util import TRACE_ARG_CAP, TRACE_EVENTS_CAP, TRACE_OUTPUT_CAP, TRACE_TEXT_CAP

    events = [
        {"kind": "text", "text": "x" * 5000, "dropped": 7},
        {
            "kind": "tool",
            "name": "bash",
            "args": '{"command":"literal JSON, not arguments to reinterpret"}',
            "params": [["data", '{"key":"  preserve this"}'], ["long", "v" * 10000]],
            "output": "o" * 5000,
            "output_dropped": 9,
        },
    ]
    preview = rc.trace_preview(events)
    assert len(preview[0]["text"]) == TRACE_TEXT_CAP
    assert preview[0]["dropped"] == 1007
    assert preview[1]["args"] == events[1]["args"]
    assert preview[1]["params"][0] == tuple(events[1]["params"][0])
    assert len(preview[1]["params"][1][1]) <= TRACE_ARG_CAP
    assert len(preview[1]["output"]) == TRACE_OUTPUT_CAP
    assert preview[1]["output_dropped"] == 3009
    assert len(events[0]["text"]) == 5000 and len(events[1]["output"]) == 5000
    assert len(rc.trace_preview(events * 200)) == TRACE_EVENTS_CAP


def test_remote_trace_job_cancels_worker_and_sanitizes_unexpected_errors():
    entered = threading.Event()

    def request(cancel):
        entered.set()
        assert cancel.wait(2)
        raise rc.RemoteTraceError("Remote trace cancelled.")

    job = rc.TraceJob(request)
    try:
        assert entered.wait(2)
        assert not job.done.is_set()
        job.cancel()
        assert job.done.wait(2)
        assert job.error == "Remote trace cancelled."
    finally:
        job.cancel()
        job.thread.join(2)
    assert not job.thread.is_alive()

    def broken(cancel):
        raise RuntimeError("SECRET payload")

    job = rc.TraceJob(broken)
    assert job.done.wait(2)
    job.thread.join(2)
    assert job.error == "Could not read remote trace." and not job.content


def test_remote_identity_snapshots_are_lazy_and_bounded():
    with _managed() as (store, directory, _, filename), patch.object(
        rc, "trace_snapshot", wraps=rc.trace_snapshot
    ) as build:
        payload = _summary("display", [_session(f"s{i}") for i in range(6)])
        payload["turns"] = {f"s{i}": [_turn()] for i in range(6)}
        _write(directory, os.path.basename(filename), payload)
        store = RemoteStore(directory)
        for i in range(6):
            assert store.supports_turn_content(f"s{i}")
        build.assert_not_called()
        key = store.message_timeline("s0")[0]["content_key"]
        for i in range(1, 6):
            store.message_timeline(f"s{i}")
            assert len(store._trace_snapshots) <= 4
        assert store.message_timeline("s0")[0]["content_key"] == key
