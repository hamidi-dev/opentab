"""Opt-in, bounded JSONL performance diagnostics. Never record source content.

Call sites supply static labels, counts and durations only. Use ``identity`` for
source/session correlation; arguments, results, SQL and exception messages must
never be logged. Disabled decorators bypass all clocks and formatting.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import json
import os
import sys
import threading
import time

from opentab.persistence import paths

_sink = None
_parent = contextvars.ContextVar("opentab_debug_span", default=None)
MAX_BYTES = 20 * 1024 * 1024


def enabled() -> bool:
    return _sink is not None


def identity(value: str) -> str | None:
    """A run-local identifier, without retaining paths or native session IDs."""
    sink = _sink
    if sink is None:
        return None
    return hashlib.sha256(sink.salt + value.encode("utf-8", "replace")).hexdigest()[:12]


class _Sink:
    def __init__(self, filename: str | None):
        import tempfile

        self.salt = os.urandom(16)
        if filename is None:
            directory = os.path.join(paths.state_dir(), "debug")
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd, filename = tempfile.mkstemp(
                prefix=time.strftime("%Y%m%d-%H%M%S-") + str(os.getpid()) + "-",
                suffix=".jsonl",
                dir=directory,
            )
        else:
            filename = os.path.abspath(os.path.expanduser(filename))
            # Explicit destinations are new files: never overwrite a source or old log.
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.path = filename
        self.file = os.fdopen(fd, "w", encoding="utf-8")
        self.lock = threading.Lock()
        self.started = time.perf_counter()
        self.sequence = 0
        self.size = 0
        self.stopped = False
        self.stderr_progress = False

    def emit(self, event: str, fields: dict) -> int:
        with self.lock:
            self.sequence += 1
            seq = self.sequence
            if self.stopped:
                return seq
            record = {
                "time": time.time(),
                "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 3),
                "pid": os.getpid(),
                "thread": threading.get_ident(),
                "seq": seq,
                "parent": _parent.get(),
                "event": event,
                **fields,
            }
            line = json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n"
            if self.size + len(line) > MAX_BYTES:
                line = json.dumps({"event": "debug.limit", "max_bytes": MAX_BYTES}) + "\n"
                self.stopped = True
            try:
                self.file.write(line)
                self.file.flush()  # An unfinished start event identifies a stalled phase.
                self.size += len(line)
            except OSError:
                self.stopped = True  # Diagnostics must not break a running TUI/worker.
            return seq


def event(name: str, **fields) -> None:
    sink = _sink
    if sink is not None:
        sink.emit(name, fields)


def progress(name: str, **fields) -> None:
    """Flush an opt-in CLI milestone to both the log and stderr.

    Callers must supply only static labels, counts and run-local hashed identities.
    Unlike span events this remains visible without a second tail process.
    """
    sink = _sink
    if sink is None:
        return
    sink.emit(name, fields)
    if not sink.stderr_progress:
        return
    sys.stderr.write(
        "OpenTab debug: "
        + json.dumps(
            {
                "stage": name,
                "elapsed_ms": round((time.perf_counter() - sink.started) * 1000, 3),
                **fields,
            }
        )
        + "\n"
    )
    sys.stderr.flush()


def _memory() -> dict:
    # Peak process RSS, NOT total WSL memory or filesystem cache.
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result = {
            "peak_rss_mib": round(value / (1024 * 1024 if sys.platform == "darwin" else 1024), 3),
        }
    except ImportError:
        result = {}
    if sys.platform == "linux":
        # Process-local residency distinguishes a retained allocation from an old
        # high-water mark. Never label Linux page cache or VmmemWSL as process RSS.
        try:
            with open("/proc/self/status", encoding="ascii") as handle:
                fields = dict(line.split(":", 1) for line in handle if ":" in line)
            for name, label in (
                ("VmRSS", "rss_mib"),
                ("RssAnon", "rss_anon_mib"),
                ("RssFile", "rss_file_mib"),
                ("VmSwap", "swap_mib"),
            ):
                if name in fields:
                    result[label] = round(int(fields[name].split()[0]) / 1024, 3)
        except (OSError, ValueError, IndexError):
            pass
    return result


@contextlib.contextmanager
def span(name: str, **fields):
    sink = _sink
    result = {}
    if sink is None:
        yield result
        return
    started = time.perf_counter()
    cpu_started = time.process_time()
    thread_started = time.thread_time()
    before = _memory()
    seq = sink.emit(name + ".start", {**fields, **before})
    token = _parent.set(seq)
    try:
        yield result
    except GeneratorExit:
        # Closing a bounded row iterator after fetchone is successful consumption.
        raise
    except BaseException as exc:
        result.update(status="error", error_type=type(exc).__name__)
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        process_cpu_ms = (time.process_time() - cpu_started) * 1000
        thread_cpu_ms = (time.thread_time() - thread_started) * 1000
        _parent.reset(token)
        after = _memory()
        if "rss_mib" in before and "rss_mib" in after:
            after["rss_change_mib"] = round(after["rss_mib"] - before["rss_mib"], 3)
        sink.emit(
            name + ".end",
            {
                **fields,
                **result,
                "span": seq,
                "duration_ms": round(duration_ms, 3),
                "process_cpu_ms": round(process_cpu_ms, 3),
                "thread_cpu_ms": round(thread_cpu_ms, 3),
                "wall_minus_thread_cpu_ms": round(max(0, duration_ms - thread_cpu_ms), 3),
                **after,
            },
        )


def timed(name: str, *, count_rows: bool = True):
    """Time a call without inspecting its arguments or logging its return value."""

    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            if not enabled():
                return fn(*args, **kwargs)
            with span(name) as info:
                result = fn(*args, **kwargs)
                if count_rows and isinstance(result, (list, tuple)):
                    info["rows"] = len(result)
                return result

        return wrapped

    return decorate


def query_rows(conn, sql: str, params=(), *, label: str, explain: bool = False):
    """Time SQLite execute/fetch separately from the caller's per-row processing.

    SQL and parameters are deliberately never emitted. Only use on iterators
    consumed to exhaustion (or explicitly closed) so the end event is reliable.
    """
    if not enabled():
        yield from conn.execute(sql, params)
        return
    with span(label) as info:
        if explain:
            query_plan(conn, sql, params, label=label)
        started = time.perf_counter()
        cursor = None
        execute_seconds = fetch_seconds = 0.0
        count = 0
        try:
            cursor = conn.execute(sql, params)
            execute_seconds = time.perf_counter() - started
            while True:
                started = time.perf_counter()
                try:
                    row = cursor.fetchone()
                finally:
                    fetch_seconds += time.perf_counter() - started
                if row is None:
                    break
                count += 1
                yield row
        finally:
            if cursor is None:
                execute_seconds = time.perf_counter() - started
            else:
                cursor.close()
            info.update(
                rows=count,
                sql_ms=round((execute_seconds + fetch_seconds) * 1000, 3),
                execute_ms=round(execute_seconds * 1000, 3),
                fetch_ms=round(fetch_seconds * 1000, 3),
            )


def query_one(conn, sql: str, params=(), *, label: str):
    """A bounded one-row read with the same timing/error contract as query_rows."""
    rows = query_rows(conn, sql, params, label=label)
    try:
        return next(rows, None)
    finally:
        rows.close()


def query_plan(conn, sql: str, params=(), *, label: str) -> None:
    """Summarize selected internal query plans, never SQL or arbitrary plan text.

    MATERIALIZED is a planner request, not evidence that a particular runtime
    honored it. Only counts and two fixed internal CTE flags cross the log boundary.
    EXPLAIN prepares but does not execute the source read.
    """
    if not enabled():
        return
    import sqlite3

    started = time.perf_counter()
    try:
        cursor = conn.execute("explain query plan " + sql, params)
        try:
            rows = cursor.fetchmany(257)
        finally:
            cursor.close()
        details = [str(row[3]).upper() for row in rows[:256]]
        event(
            "sql.plan",
            query=label,
            prepare_ms=round((time.perf_counter() - started) * 1000, 3),
            truncated=len(rows) > 256,
            steps=len(details),
            scans=sum(d.startswith("SCAN ") for d in details),
            searches=sum(d.startswith("SEARCH ") for d in details),
            materializations=sum(d.startswith("MATERIALIZE ") for d in details),
            temp_btrees=sum("TEMP B-TREE" in d for d in details),
            correlated=sum("CORRELATED" in d for d in details),
            automatic_indexes=sum("AUTOMATIC" in d for d in details),
            candidate_parts_materialized="MATERIALIZE CANDIDATE_PARTS" in details,
            native_materialized="MATERIALIZE NATIVE" in details,
        )
    except sqlite3.Error as exc:
        event("sql.plan_unavailable", query=label, error_type=type(exc).__name__)


@contextlib.contextmanager
def session(active: bool = False, filename: str | None = None, *, stderr_progress: bool = False):
    """Own the CLI log's lifetime; stdout remains usable for JSON/MCP/exports."""
    global _sink
    if not active and filename is None:
        yield
        return
    import sqlite3

    from opentab import __version__

    previous = _sink
    try:
        sink = _Sink(filename)
    except OSError as exc:
        raise SystemExit(
            f"opentab: cannot create debug log ({type(exc).__name__}); use a new writable --debug-log path"
        ) from None
    _sink = sink
    sink.stderr_progress = stderr_progress
    sys.stderr.write(f"OpenTab debug log: {sink.path}\n")
    sys.stderr.flush()
    try:
        with span(
            "run",
            version=__version__,
            python=sys.version.split()[0],
            sqlite=sqlite3.sqlite_version,
            platform=sys.platform,
            cpu_count=os.cpu_count(),
        ):
            yield sink.path
    finally:
        _sink = previous
        with sink.lock:
            sink.stopped = True
            with contextlib.suppress(OSError):
                sink.file.close()
