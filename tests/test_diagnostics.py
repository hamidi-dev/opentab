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
