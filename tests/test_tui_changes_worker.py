import json
import tempfile
import threading
from pathlib import Path

from opentab import diagnostics as debug
from opentab.tui.changes_worker import ChangesWorker


def test_changes_worker_is_serial_and_coalesces_duplicate_keys():
    worker = ChangesWorker()
    first_started = threading.Event()
    first_release = threading.Event()
    second_started = threading.Event()

    def first(cancelled):
        first_started.set()
        first_release.wait()
        return "first"

    def second(cancelled):
        second_started.set()
        return "second"

    try:
        assert worker.submit(("files", "one"), first)
        assert first_started.wait(2)
        assert not worker.submit(("files", "one"), first)
        assert worker.submit(("files", "two"), second)
        assert not second_started.is_set()
        first_release.set()
        assert worker.wait_for_result()
        rows = worker.poll()
        if len(rows) == 1:
            assert worker.wait_for_result()
            rows += worker.poll()
        assert rows == [
            (("files", "one"), "first", False),
            (("files", "two"), "second", False),
        ]
    finally:
        first_release.set()
        worker.close()


def test_changes_worker_cancel_drops_active_queued_and_completed_generation():
    worker = ChangesWorker()
    started = threading.Event()
    cancelled_seen = threading.Event()
    queued_ran = threading.Event()

    def blocked(cancelled):
        started.set()
        cancelled.wait()
        cancelled_seen.set()
        return "stale"

    try:
        assert worker.submit(("files", "active"), blocked)
        assert started.wait(2)
        assert worker.submit(("files", "queued"), lambda event: queued_ran.set())
        worker.cancel_all()
        assert cancelled_seen.wait(2)
        assert not queued_ran.is_set()
        assert worker.submit(("files", "fresh"), lambda event: "fresh")
        assert worker.wait_for_result()
        assert worker.poll() == [(("files", "fresh"), "fresh", False)]
    finally:
        worker.close()


def test_changes_worker_close_is_bounded_for_uncooperative_request():
    worker = ChangesWorker()
    started = threading.Event()
    release = threading.Event()

    def blocked(cancelled):
        started.set()
        release.wait()

    assert worker.submit(("files", "blocked"), blocked)
    assert started.wait(2)
    worker.close()
    assert worker._thread.is_alive()
    release.set()
    worker._thread.join(2)
    assert not worker._thread.is_alive()


def test_changes_worker_debug_correlates_queue_execution_and_result_without_keys():
    with tempfile.TemporaryDirectory() as tmp:
        filename = Path(tmp) / "debug.jsonl"
        with debug.session(filename=str(filename)):
            worker = ChangesWorker()
            try:
                assert worker.submit(("private-key",), lambda cancelled: "private-patch")
                assert worker.wait_for_result()
                assert worker.poll() == [(("private-key",), "private-patch", False)]
            finally:
                worker.close()
        text = filename.read_text()
        assert "private-" not in text
        rows = [json.loads(line) for line in text.splitlines()]
        queue = next(r for r in rows if r["event"] == "changes.queued")
        execute = next(r for r in rows if r["event"] == "changes.execute.start")
        result = next(r for r in rows if r["event"] == "changes.completed")
        assert queue["request"] == execute["request"] == result["request"]
        assert queue["worker"] == execute["worker"] == result["worker"]
        assert execute["queue_ms"] >= 0 and not result["failed"]
