import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from opentab import diagnostics as debug


def _records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def test_debug_disabled_does_not_create_files_or_read_clocks():
    @debug.timed("test.operation")
    def operation():
        return 42

    with patch.object(
        debug.time, "perf_counter", side_effect=AssertionError("clock")
    ), patch.object(debug.paths, "state_dir", side_effect=AssertionError("path")):
        with debug.session():
            assert operation() == 42
            with debug.span("test.phase"):
                debug.event("test.event")
            assert debug.identity("private") is None
            debug.query_plan(None, "must not prepare", label="test.plan")


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
