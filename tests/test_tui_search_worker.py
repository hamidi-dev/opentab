import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from unittest.mock import patch

from opentab.service import ServiceError
from opentab.tui.search_worker import SearchWorker

from tests._support import _write_opencode_db_with_turns


def _args(**values):
    defaults = dict(demo=None, no_cache=False, no_state=False, pull="host")
    defaults.update(values)
    return argparse.Namespace(**defaults)


def _wait(worker, count=1, timeout=1.0):
    deadline = time.monotonic() + timeout
    results = []
    while len(results) < count and time.monotonic() < deadline:
        results.extend(worker.poll())
        if len(results) < count:
            time.sleep(0.005)
    assert len(results) == count
    return results


class _Service:
    def __init__(self, gate=None):
        self.created_thread = threading.get_ident()
        self.calls = []
        self.gate = gate
        self.closed_thread = None
        self.args = None

    def search_conversations(self, **params):
        self.calls.append(("search", params, threading.get_ident()))
        if self.gate is not None:
            self.gate.wait(1)
        return {"query": params["query"]}

    def session_conversation(self, **params):
        self.calls.append(("conversation", params, threading.get_ident()))
        return {"session": params["value"]}

    def index_conversations(self, **params):
        self.calls.append(("index", params, threading.get_ident()))
        return {"updated": 1}

    def close(self):
        self.closed_thread = threading.get_ident()


def test_status_is_async_and_does_not_construct_or_discover_service():
    called = threading.Event()

    def status():
        called.set()
        return {"exists": False}

    def forbidden_factory(_args, _source):
        raise AssertionError("status constructed a store")

    with patch("opentab.tui.search_worker.index_status", status):
        worker = SearchWorker(_args(), "opencode", service_factory=forbidden_factory)
        request = worker.submit("status")
        assert called.wait(1)
        assert _wait(worker) == [(request, "status", {"exists": False}, None)]
        worker.close()


def test_service_is_lazy_thread_owned_and_receives_safe_args_snapshot():
    services = []
    original = _args(no_state=True)

    def factory(args, source):
        assert source == "opencode"
        assert args.no_cache is True
        assert args.no_state is True
        assert args.pull is None
        service = _Service()
        service.args = args
        services.append(service)
        return service

    worker = SearchWorker(original, "opencode", service_factory=factory)
    original.no_state = False
    assert services == []
    request = worker.submit("conversation", session="session-1")
    assert _wait(worker) == [(request, "conversation", {"session": "session-1"}, None)]
    service = services[0]
    assert service.created_thread != threading.get_ident()
    assert service.calls[0][2] == service.created_thread
    worker.close()
    deadline = time.monotonic() + 1
    while service.closed_thread is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert service.closed_thread == service.created_thread


def test_pending_searches_coalesce_while_a_read_is_running():
    gate = threading.Event()
    service = _Service(gate)
    worker = SearchWorker(_args(), "opencode", service_factory=lambda _a, _s: service)
    first = worker.submit("search", query="first")
    deadline = time.monotonic() + 1
    while not service.calls and time.monotonic() < deadline:
        time.sleep(0.005)
    assert service.calls
    worker.submit("conversation", session="stale-preview")
    worker.submit("search", query="stale")
    latest = worker.submit("search", query="latest")
    gate.set()
    results = _wait(worker, 2)
    assert [item[0] for item in results] == [first, latest]
    assert [call[1]["query"] for call in service.calls] == ["first", "latest"]
    worker.close()


def test_discard_pending_preserves_status_and_confirmed_index_only():
    gate = threading.Event()
    service = _Service(gate)
    worker = SearchWorker(_args(), "opencode", service_factory=lambda _a, _s: service)
    running = worker.submit("search", query="running")
    deadline = time.monotonic() + 1
    while not service.calls and time.monotonic() < deadline:
        time.sleep(0.005)
    worker.submit("conversation", session="discard-me")
    status = worker.submit("status")
    index = worker.submit("index", rebuild=True)
    with patch("opentab.tui.search_worker.index_status", return_value={"exists": True}):
        worker.discard_pending()
        gate.set()
        results = _wait(worker, 3)
    assert [item[0] for item in results] == [running, status, index]
    assert [call[0] for call in service.calls] == ["search", "index"]
    worker.close()


def test_close_is_bounded_and_drops_queued_work():
    gate = threading.Event()
    service = _Service(gate)
    worker = SearchWorker(_args(), "opencode", service_factory=lambda _a, _s: service)
    worker.submit("search", query="running")
    deadline = time.monotonic() + 1
    while not service.calls and time.monotonic() < deadline:
        time.sleep(0.005)
    worker.submit("index")
    started = time.monotonic()
    worker.close()
    assert time.monotonic() - started < 0.25
    gate.set()
    deadline = time.monotonic() + 1
    while service.closed_thread is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert [call[0] for call in service.calls] == ["search"]


def test_errors_are_structured_bounded_and_do_not_leak_unexpected_content():
    class Broken(_Service):
        def search_conversations(self, **params):
            if params["query"] == "domain":
                raise ServiceError("invalid_query", "x" * 1000)
            raise RuntimeError("secret conversation text")

    worker = SearchWorker(_args(), "opencode", service_factory=lambda _a, _s: Broken())
    domain = worker.submit("search", query="domain")
    unexpected = worker.submit("conversation", session="unused")
    # Conversation succeeds; issue the unexpected search after it has started processing.
    results = _wait(worker, 2)
    assert results[0][0] == domain
    assert results[0][3] == {"code": "invalid_query", "message": "x" * 500}
    assert results[1][0] == unexpected
    leak = worker.submit("search", query="leak")
    assert _wait(worker) == [
        (
            leak,
            "search",
            None,
            {
                "code": "operation_failed",
                "message": "Conversation search operation failed.",
            },
        )
    ]
    worker.close()


def test_demo_and_remote_are_rejected_without_service_creation_or_indexing():
    calls = []

    def factory(_args, _source):
        calls.append("constructed")
        return _Service()

    demo = SearchWorker(_args(demo="all"), "opencode", service_factory=factory)
    demo_id = demo.submit("index")
    assert _wait(demo)[0][3]["code"] == "demo_unsupported"
    demo.close()
    remote = SearchWorker(_args(), "remote", service_factory=factory)
    remote_id = remote.submit("search", query="text")
    result = _wait(remote)[0]
    assert result[0] == remote_id
    assert result[3]["code"] == "remote_unsupported"
    remote.close()
    assert demo_id > 0
    assert calls == []


def test_search_never_auto_indexes():
    service = _Service()
    worker = SearchWorker(_args(), "opencode", service_factory=lambda _a, _s: service)
    request = worker.submit("search", query="needle")
    assert _wait(worker) == [(request, "search", {"query": "needle"}, None)]
    assert [call[0] for call in service.calls] == ["search"]
    worker.close()


def test_real_worker_indexes_searches_and_reads_a_synthetic_subagent_without_source_writes():
    with tempfile.TemporaryDirectory() as directory:
        db = os.path.join(directory, "source.db")
        _write_opencode_db_with_turns(db)
        with sqlite3.connect(db) as writer:
            writer.execute(
                "insert into part values (?, ?, ?, ?)",
                (
                    "text-child",
                    "m3",
                    "s2",
                    json.dumps({"type": "text", "text": "quasar needle child answer"}),
                ),
            )
        writer.close()
        with open(db, "rb") as source:
            before = hashlib.sha256(source.read()).hexdigest()
        env = {
            f"XDG_{name}_HOME": os.path.join(directory, name.lower())
            for name in ("CACHE", "CONFIG", "DATA", "STATE")
        }
        with patch.dict(os.environ, env):
            worker = SearchWorker(_args(db=db), "opencode")
            try:
                worker.submit("status")
                assert _wait(worker)[0][2]["exists"] is False
                worker.submit("index")
                indexed = _wait(worker, timeout=5)[0]
                assert indexed[3] is None, indexed
                assert indexed[2]["complete"] and indexed[2]["updated"] == 1
                worker.submit("search", query="quasar needle")
                searched = _wait(worker, timeout=5)[0]
                assert searched[3] is None, searched
                hit = searched[2]["hits"][0]
                assert hit["execution_id"] == "s2"
                worker.submit(
                    "conversation",
                    session=hit["session_key"],
                    execution_id=hit["execution_id"],
                    anchor=hit["anchor"],
                )
                read = _wait(worker, timeout=5)[0]
                assert read[3] is None, read
                assert read[2]["records"][0]["parts"][0]["text"] == "quasar needle child answer"
            finally:
                worker.close()
        with open(db, "rb") as source:
            assert hashlib.sha256(source.read()).hexdigest() == before
