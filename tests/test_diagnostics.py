import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

from opentab import diagnostics as debug


def _records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def test_debug_disabled_does_not_create_files_or_read_clocks():
    @debug.timed("test.operation", activity=True)
    def operation():
        return 42

    with patch.object(
        debug.time, "perf_counter", side_effect=AssertionError("clock")
    ), patch.object(debug.paths, "state_dir", side_effect=AssertionError("path")), patch.object(
        debug, "_code_fingerprint", side_effect=AssertionError("source read")
    ), patch.object(debug, "_activity", side_effect=AssertionError("counter read")), patch.object(
        debug.time, "process_time", side_effect=AssertionError("cpu clock")
    ), patch.object(debug.time, "thread_time", side_effect=AssertionError("thread clock")):
        with debug.session():
            assert operation() == 42
            with debug.span("test.phase", activity=True):
                debug.event("test.event")
            assert debug.identity("private") is None
            debug.query_plan(None, "must not prepare", label="test.plan")


def test_debug_model_loading_separates_pricing_and_keeps_usage_private():
    from opentab.tui.app import App

    from tests._support import CopilotEstimateStore, _parse, copilot_prices

    with tempfile.TemporaryDirectory() as tmp, copilot_prices(), contextlib.redirect_stderr(
        io.StringIO()
    ):
        store = CopilotEstimateStore()
        plain = App(store, _parse(["--no-state"]))
        plain._ensure_models()
        filename = os.path.join(tmp, "pricing.jsonl")
        with debug.session(filename=filename):
            logged = App(store, _parse(["--no-state"]))
            logged._ensure_models()
        assert logged._model_by_root == plain._model_by_root
        assert logged.loaded == plain.loaded
        rows = _records(filename)
        starts = {r["seq"]: r for r in rows if r["event"].endswith(".start")}
        for event in (
            "fetch_models",
            "group_models",
            "reconcile_unpriced",
            "price_models",
            "apply_prices",
        ):
            end = next(r for r in rows if r["event"] == "app." + event + ".end")
            start = starts[end["span"]]
            assert starts[start["parent"]]["event"] == "app.load_models.start"
        summary = next(r for r in rows if r["event"] == "app.pricing_workload")
        assert summary["model_rows"] == 2 and summary["request_buckets"] == 3
        assert summary["copilot_rows"] == 1 and summary["root_split_rows"] == 2
        fingerprint = next(r["code_fingerprint"] for r in rows if r["event"] == "run.start")
        assert len(fingerprint) == 16 and all(c in "0123456789abcdef" for c in fingerprint)
        text = Path(filename).read_text()
        for model in store.models:
            assert model["model_name"] not in text
        assert "copilot_list_prices" not in text and tmp not in text


def test_debug_source_fingerprint_detects_same_version_code_changes():
    from unittest.mock import Mock

    package = Mock()
    package.joinpath.return_value.read_bytes.return_value = b"first build"
    with patch("importlib.resources.files", return_value=package):
        first = debug._code_fingerprint()
        assert debug._code_fingerprint() == first
        package.joinpath.return_value.read_bytes.return_value = b"second build"
        assert debug._code_fingerprint() != first
        package.joinpath.return_value.read_bytes.side_effect = OSError("private path")
        assert debug._code_fingerprint() is None


def test_debug_nested_spans_workers_errors_and_private_values():
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
        filename = os.path.join(tmp, "debug.jsonl")
        with debug.session(filename=filename):
            private_id = debug.identity("secret-session")
            assert private_id == debug.identity("secret-session")
            with debug.span("test.parent"):
                with debug.span("test.child"):
                    debug.event("test.identity", session=private_id)
                # Errors must propagate unchanged, without their potentially raw message.
                error = ValueError("private source text")
                try:
                    with debug.span("test.failure"):
                        raise error
                except ValueError as exc:
                    assert exc is error
                threads = [
                    threading.Thread(target=lambda: debug.event("test.worker")) for _ in range(4)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                conn = sqlite3.connect(":memory:")
                try:
                    assert list(
                        debug.query_rows(
                            conn, "select ?", ["secret-query-value"], label="test.query"
                        )
                    ) == [("secret-query-value",)]
                finally:
                    conn.close()
            # Starts/events are visible before the logging session is closed.
            assert any(r["event"] == "test.query.end" for r in _records(filename))
        assert not debug.enabled()
        text = Path(filename).read_text()
        for secret in ("private source text", "secret-session", "secret-query-value", tmp):
            assert secret not in text
        rows = _records(filename)
        starts = {r["seq"]: r for r in rows if r["event"].endswith(".start")}
        for row in rows:
            if row["event"].endswith(".end"):
                assert row["span"] in starts
                assert row["duration_ms"] >= 0
                assert row["process_cpu_ms"] >= 0
                assert row["thread_cpu_ms"] >= 0
                assert row["wall_minus_thread_cpu_ms"] >= 0
        child = next(r for r in rows if r["event"] == "test.child.start")
        assert starts[child["parent"]]["event"] == "test.parent.start"
        failure = next(r for r in rows if r["event"] == "test.failure.end")
        assert failure["status"] == "error" and failure["error_type"] == "ValueError"
        assert sum(r["event"] == "test.worker" for r in rows) == 4
        assert len({r["seq"] for r in rows}) == len(rows)


def test_debug_progress_stays_in_log_unless_cli_explicitly_enables_stderr():
    with tempfile.TemporaryDirectory() as tmp:
        for visible in (False, True):
            output = io.StringIO()
            filename = os.path.join(tmp, f"progress-{visible}.jsonl")
            with contextlib.redirect_stderr(output), debug.session(
                filename=filename, stderr_progress=visible
            ):
                output.seek(0)
                output.truncate()
                with patch.object(output, "flush", wraps=output.flush) as flush:
                    debug.progress("test.progress", roots=3)
                    assert flush.called == visible
                assert bool(output.getvalue()) == visible
                assert any(row["event"] == "test.progress" for row in _records(filename))


def test_debug_default_xdg_unique_files_and_explicit_no_clobber():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"XDG_STATE_HOME": tmp}
    ), contextlib.redirect_stderr(io.StringIO()):
        with debug.session(True) as first:
            first_id = debug.identity("same-input")
        with debug.session(True) as second:
            assert debug.identity("same-input") != first_id
        assert first != second
        assert Path(first).parent == Path(tmp) / "opentab" / "debug"
        if os.name != "nt":
            assert os.stat(first).st_mode & 0o777 == 0o600
        before = Path(first).read_bytes()
        try:
            with debug.session(filename=first):
                raise AssertionError("must not overwrite")
        except SystemExit:
            pass
        assert Path(first).read_bytes() == before
        assert not debug.enabled()


def test_debug_log_limit_and_write_failure_do_not_break_operations():
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
        filename = os.path.join(tmp, "limited.jsonl")
        with patch.object(debug, "MAX_BYTES", 1500), debug.session(filename=filename):
            for _ in range(100):
                debug.event("test.record", rows=123)
        rows = _records(filename)
        assert rows[-1]["event"] == "debug.limit"
        assert sum(r["event"] == "debug.limit" for r in rows) == 1
        assert os.path.getsize(filename) < 1700
        with debug.session(filename=os.path.join(tmp, "broken.jsonl")):
            with patch.object(debug._sink.file, "write", side_effect=OSError("disk full")):
                with debug.span("test.still_works"):
                    assert 2 + 2 == 4


def test_debug_queries_report_plan_execution_and_failures_without_sql_or_false_errors():
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
        filename = os.path.join(tmp, "queries.jsonl")
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("create table private_table(private_column text)")
            conn.executemany("insert into private_table values(?)", [("private payload",)] * 2)
            with debug.session(filename=filename):
                assert debug.query_one(
                    conn, "select private_column from private_table", label="test.one"
                ) == ("private payload",)
                assert (
                    len(
                        list(
                            debug.query_rows(
                                conn,
                                "select * from private_table",
                                label="test.all",
                                explain=True,
                            )
                        )
                    )
                    == 2
                )
                try:
                    debug.query_one(conn, "select private_missing", label="test.failure")
                except sqlite3.OperationalError:
                    pass
                else:
                    raise AssertionError("SQL failure swallowed")
                debug.query_plan(conn, "select private_missing", label="test.bad_plan")
        finally:
            conn.close()
        text = Path(filename).read_text()
        assert "private_" not in text and "private payload" not in text
        rows = _records(filename)
        one = next(r for r in rows if r["event"] == "test.one.end")
        assert one["rows"] == 1 and "error_type" not in one
        assert one["execute_ms"] >= 0 and one["fetch_ms"] >= 0
        failure = next(r for r in rows if r["event"] == "test.failure.end")
        assert failure["status"] == "error" and failure["error_type"] == "OperationalError"
        assert failure["rows"] == 0
        plan = next(r for r in rows if r["event"] == "sql.plan")
        assert plan["query"] == "test.all" and plan["scans"] == 1
        assert plan["prepare_ms"] >= 0
        assert any(r["event"] == "sql.plan_unavailable" for r in rows)


def test_debug_memory_distinguishes_current_residency_from_high_water():
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
        filename = os.path.join(tmp, "memory.jsonl")
        with debug.session(filename=filename):
            with patch.object(
                debug,
                "_memory",
                side_effect=[
                    {"rss_mib": 200.0, "peak_rss_mib": 500.0},
                    {"rss_mib": 100.0, "peak_rss_mib": 500.0},
                ],
            ):
                with debug.span("test.release"):
                    pass
        end = next(r for r in _records(filename) if r["event"] == "test.release.end")
        assert end["rss_change_mib"] == -100.0
        assert end["rss_mib"] == 100.0 and end["peak_rss_mib"] == 500.0


def test_debug_activity_emits_process_deltas_only_for_opted_in_span():
    @debug.timed("test.activity", activity=True)
    def operation():
        with debug.span("test.nested"):
            return [1, 2]

    before = {
        "process_io_read_bytes": 4096,
        "process_io_rchar": 10000,
        "process_io_syscr": 50,
        "process_io_write_bytes": 0,
        "process_minor_faults": 10,
        "process_major_faults": 1,
        "process_block_in": 8,
        "process_block_out": 0,
        "process_voluntary_context_switches": 20,
        "process_involuntary_context_switches": 5,
    }
    after = {key: value + index for index, (key, value) in enumerate(before.items())}
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
        filename = os.path.join(tmp, "activity.jsonl")
        with debug.session(filename=filename), patch.object(
            debug, "_activity", side_effect=[before, after]
        ) as sample:
            assert operation() == [1, 2]
            assert sample.call_count == 2
        rows = _records(filename)
        end = next(r for r in rows if r["event"] == "test.activity.end")
        assert end["rows"] == 2
        for index, key in enumerate(before):
            assert end[key + "_delta"] == index
        for row in rows:
            if row is not end:
                assert not any(key.endswith("_delta") for key in row)


def test_debug_activity_ordinary_spans_bypass_sampling():
    @debug.timed("test.ordinary")
    def operation():
        return 42

    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
        with debug.session(filename=os.path.join(tmp, "ordinary.jsonl")), patch.object(
            debug, "_activity", side_effect=AssertionError("counter read")
        ):
            with debug.span("test.ordinary_span"):
                assert operation() == 42


def test_debug_activity_missing_and_reset_counters_are_omitted():
    snapshots = [
        {"process_io_rchar": 100, "process_io_read_bytes": 4096},
        {"process_io_rchar": 50, "process_major_faults": 0},
        {},
        {"process_io_read_bytes": 0},
    ]
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
        filename = os.path.join(tmp, "missing.jsonl")
        with debug.session(filename=filename), patch.object(
            debug, "_activity", side_effect=snapshots
        ):
            with debug.span("test.reset", activity=True):
                pass
            with debug.span("test.missing", activity=True):
                pass
        for row in _records(filename):
            assert not any(key.endswith("_delta") for key in row)


def test_debug_activity_snapshot_validates_allowlisted_numeric_sources():
    usage = SimpleNamespace(
        ru_minflt=10, ru_majflt=2, ru_inblock=8, ru_oublock=3, ru_nvcsw=7, ru_nivcsw=4
    )
    resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=Mock(return_value=usage))
    text = (
        "read_bytes: 4096\nrchar: 10000\nsyscr: 50\nwrite_bytes: 8192\n"
        "private_source_path: /home/private/database\nprivate_payload\n"
    )
    with patch.dict("sys.modules", resource=resource), patch.object(
        debug.sys, "platform", "linux"
    ), patch("builtins.open", mock_open(read_data=text)) as opened:
        counters = debug._activity()
        opened.assert_called_once_with("/proc/self/io", encoding="ascii")
        resource.getrusage.assert_called_once_with(resource.RUSAGE_SELF)
    assert counters == {
        "process_io_read_bytes": 4096,
        "process_io_rchar": 10000,
        "process_io_syscr": 50,
        "process_io_write_bytes": 8192,
        "process_minor_faults": 10,
        "process_major_faults": 2,
        "process_block_in": 8,
        "process_block_out": 3,
        "process_voluntary_context_switches": 7,
        "process_involuntary_context_switches": 4,
    }
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
        filename = os.path.join(tmp, "private.jsonl")
        with debug.session(filename=filename), patch.object(
            debug, "_activity", return_value=counters
        ):
            with debug.span("test.private", activity=True):
                pass
        logged = Path(filename).read_text()
        for secret in ("private_source_path", "/home/private", "private_payload", "/proc/self/io"):
            assert secret not in logged


def test_debug_activity_snapshot_errors_malformed_data_and_unsupported_platform():
    resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=Mock(side_effect=OSError("private error")))
    with patch.dict("sys.modules", resource=resource), patch.object(debug.sys, "platform", "linux"):
        for failure in (OSError("private path"), UnicodeError("private content")):
            with patch("builtins.open", side_effect=failure):
                assert debug._activity() == {}
        for text in (
            "read_bytes: -1\nrchar: NaN\nsyscr: 1.5\nwrite_bytes: private content\n",
            "read_bytes: 5\nread_bytes: 6\nread_bytes: 7\n",
            "rchar: " + "1" * 16384,
        ):
            with patch("builtins.open", mock_open(read_data=text)):
                assert debug._activity() == {}
        with patch("builtins.open", mock_open(read_data="read_bytes: invalid\nrchar: 9\n")):
            assert debug._activity() == {"process_io_rchar": 9}
    with patch.dict("sys.modules", resource=None), patch.object(
        debug.sys, "platform", "win32"
    ), patch("builtins.open", side_effect=AssertionError("proc read")):
        assert debug._activity() == {}


def test_debug_activity_partial_resource_support_preserves_available_counters():
    resource = SimpleNamespace(
        RUSAGE_SELF=0,
        getrusage=Mock(
            return_value=SimpleNamespace(ru_minflt=12, ru_majflt=-1, ru_inblock="unknown")
        ),
    )
    with patch.dict("sys.modules", resource=resource), patch.object(
        debug.sys, "platform", "darwin"
    ), patch("builtins.open", side_effect=AssertionError("proc read")):
        assert debug._activity() == {"process_minor_faults": 12}


def test_debug_activity_read_failures_do_not_break_the_accounted_operation():
    resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=Mock(side_effect=OSError("private error")))
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
        filename = os.path.join(tmp, "unavailable.jsonl")
        with debug.session(filename=filename), patch.dict(
            "sys.modules", resource=resource
        ), patch.object(debug, "_memory", return_value={}), patch.object(
            debug.sys, "platform", "linux"
        ), patch.object(debug, "open", side_effect=OSError("private source path"), create=True):
            with debug.span("test.unavailable", activity=True) as info:
                info["rows"] = 3
        end = next(r for r in _records(filename) if r["event"] == "test.unavailable.end")
        assert end["rows"] == 3 and "error_type" not in end
        assert not any(key.endswith("_delta") for key in end)
        assert "private" not in Path(filename).read_text()
