"""Serial background access to the local conversation search service."""
from __future__ import annotations

import copy
import sqlite3
import threading
from collections import deque

from opentab.conversation import ConversationError
from opentab.conversation_search import index_status
from opentab.service import OpenTabService, ServiceError

_OPERATIONS = frozenset({"status", "search", "conversation", "index"})
_COALESCED = frozenset({"search", "conversation"})
_JOIN_TIMEOUT = 0.05
_MAX_ERROR_CHARS = 500


def _default_service_factory(args, source_key):
    # Construction belongs here, not in SearchWorker.__init__: some stores open a
    # long-lived SQLite connection in their constructor.
    from opentab import sources

    store, _loading = sources.make_store(args, source_key)
    return OpenTabService(store, args, source_key, allow_raw_content=True)


def _close_owned(root):
    """Close worker-owned wrappers, leaves, and persistent SQLite handles."""
    seen = set()

    def visit(obj):
        if obj is None or id(obj) in seen:
            return
        seen.add(id(obj))
        values = getattr(obj, "__dict__", {})
        children = values.get("stores", ())
        if isinstance(children, (list, tuple)):
            for child in children:
                visit(child)
        for name in ("store", "_store"):
            visit(values.get(name))

        close = getattr(type(obj), "close", None)
        if callable(close):
            try:
                close(obj)
            except Exception:  # cleanup must not kill the worker
                pass
        for value in values.values():
            if isinstance(value, sqlite3.Connection):
                try:
                    value.close()
                except sqlite3.Error:
                    pass

    visit(root)


class SearchWorker:
    """Run conversation operations serially without borrowing the TUI store.

    ``service_factory`` is a keyword-only test seam. It is called in the worker
    thread as ``factory(args_snapshot, source_key)`` and must return an object with
    the three service operation methods.

    ``discard_pending()`` cancels unstarted content reads, retaining the initial
    metadata-only status check and explicitly confirmed ``index`` jobs.
    ``close()`` is stronger and drops every queued job.
    """

    def __init__(self, args, source_key, *, service_factory=None):
        self._args = copy.copy(args)
        self._args.no_cache = True
        self._args.pull = None
        self._args.conversation_sources_only = True
        self._source_key = source_key
        self._service_factory = service_factory or _default_service_factory
        self._condition = threading.Condition()
        self._pending = deque()
        self._results = deque()
        self._next_id = 1
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="opentab-conversation-search",
            daemon=True,
        )
        self._thread.start()

    def submit(self, operation: str, **params) -> int:
        """Queue an operation and return its monotonically increasing request ID."""
        with self._condition:
            request_id = self._next_id
            self._next_id += 1
            if self._closed:
                self._results.append(
                    (
                        request_id,
                        operation,
                        None,
                        {"code": "worker_closed", "message": "Conversation search is closed."},
                    )
                )
                return request_id
            if operation in _COALESCED:
                stale = _COALESCED if operation == "search" else {"conversation"}
                self._pending = deque(job for job in self._pending if job[1] not in stale)
            self._pending.append((request_id, operation, dict(params)))
            self._condition.notify()
        return request_id

    def poll(self) -> list[tuple[int, str, dict | None, dict | None]]:
        """Return and clear completed results without waiting."""
        with self._condition:
            results = list(self._results)
            self._results.clear()
        return results

    def discard_pending(self) -> None:
        """Drop unstarted content reads, preserving status and confirmed writes."""
        with self._condition:
            self._pending = deque(job for job in self._pending if job[1] in {"index", "status"})

    def close(self) -> None:
        """Drop queued work and request shutdown, waiting only for a short bound."""
        with self._condition:
            if not self._closed:
                self._closed = True
                self._pending.clear()
                self._condition.notify()
        if self._thread is not threading.current_thread():
            self._thread.join(_JOIN_TIMEOUT)

    @staticmethod
    def _error(exc):
        if isinstance(exc, (ServiceError, ConversationError)):
            code = exc.code
            message = getattr(exc, "message", str(exc))
            message = " ".join(str(message).splitlines())[:_MAX_ERROR_CHARS]
            return {"code": code, "message": message}
        return {
            "code": "operation_failed",
            "message": "Conversation search operation failed.",
        }

    def _run(self):
        service = None
        try:
            while True:
                with self._condition:
                    while not self._pending and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    request_id, operation, params = self._pending.popleft()

                result = error = None
                try:
                    if operation not in _OPERATIONS:
                        raise ServiceError("invalid_operation", "Unknown conversation operation.")
                    if getattr(self._args, "demo", False):
                        raise ServiceError(
                            "demo_unsupported",
                            "Conversation search is unavailable in demo mode.",
                        )
                    if self._source_key == "remote":
                        raise ServiceError(
                            "remote_unsupported",
                            "Conversation search requires a local source.",
                        )
                    if self._source_key not in {"all", "opencode", "claude", "codex"}:
                        raise ServiceError(
                            "unsupported_harness",
                            "Choose local OpenCode, Claude Code, Codex, or all harnesses.",
                        )
                    if operation == "status":
                        result = index_status()
                    else:
                        if service is None:
                            service = self._service_factory(self._args, self._source_key)
                        if operation == "search":
                            result = service.search_conversations(**params)
                        elif operation == "conversation":
                            if "session" in params:
                                if "value" in params:
                                    raise ServiceError(
                                        "invalid_arguments",
                                        "Use session once for a conversation request.",
                                    )
                                params["value"] = params.pop("session")
                            result = service.session_conversation(**params)
                        else:
                            result = service.index_conversations(**params)
                except (Exception, SystemExit) as exc:  # keep malformed jobs from killing the lane
                    error = self._error(exc)

                with self._condition:
                    if not self._closed:
                        self._results.append((request_id, operation, result, error))
        finally:
            _close_owned(service)
