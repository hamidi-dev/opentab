"""Serial background worker for bounded session change reads."""
from __future__ import annotations

import threading
import time
from collections import deque

from opentab import diagnostics as debug

_JOIN_TIMEOUT = 0.25
_MAX_PENDING = 128


class ChangesWorker:
    """Execute source-owning requests without borrowing the TUI store connection."""

    def __init__(self):
        self._condition = threading.Condition()
        self._pending = deque()
        self._results = deque()
        self._keys = set()
        self._active = None
        self._result_ready = threading.Event()
        self._generation = 0
        self._sequence = 0
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="opentab-session-changes",
            daemon=True,
        )
        self._thread.start()

    def submit(self, key: tuple, request) -> bool:
        """Queue one unique cache key, returning false when already queued or running."""
        with self._condition:
            if self._closed or key in self._keys:
                debug.event("changes.queue_rejected", reason="closed_or_duplicate")
                return False
            if len(self._pending) >= _MAX_PENDING:
                debug.event("changes.queue_rejected", reason="queue_limit")
                return False
            self._keys.add(key)
            self._sequence += 1
            queued = time.perf_counter() if debug.enabled() else None
            self._pending.append((self._generation, key, request, self._sequence, queued))
            debug.event(
                "changes.queued",
                worker=id(self),
                request=self._sequence,
                generation=self._generation,
                pending=len(self._pending),
            )
            self._condition.notify()
            return True

    def poll(self) -> list[tuple[tuple, object, bool]]:
        """Return completed ``(key, value, failed)`` rows without waiting."""
        with self._condition:
            rows = list(self._results)
            self._results.clear()
            if not self._results:
                self._result_ready.clear()
            return rows

    def wait_for_result(self, timeout: float = 2.0) -> bool:
        """Test seam: wait for a completion signal without polling or sleeping."""
        return self._result_ready.wait(timeout)

    def cancel_all(self) -> None:
        """Cancel active work and discard every queued or completed generation."""
        with self._condition:
            debug.event(
                "changes.cancel",
                worker=id(self),
                generation=self._generation,
                pending=len(self._pending),
                completed=len(self._results),
                active=self._active is not None,
            )
            self._generation += 1
            self._pending.clear()
            self._results.clear()
            self._result_ready.clear()
            self._keys.clear()
            if self._active is not None:
                self._active.set()
            self._condition.notify()

    def close(self) -> None:
        """Request cooperative shutdown and wait for only a bounded interval."""
        with self._condition:
            if not self._closed:
                self._closed = True
                self._generation += 1
                self._pending.clear()
                self._results.clear()
                self._result_ready.clear()
                self._keys.clear()
                if self._active is not None:
                    self._active.set()
                self._condition.notify()
        if self._thread is not threading.current_thread():
            self._thread.join(_JOIN_TIMEOUT)
        debug.event("changes.closed", worker=id(self), thread_alive=self._thread.is_alive())

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                generation, key, request, sequence, queued = self._pending.popleft()
                cancelled = threading.Event()
                self._active = cancelled

            value = None
            failed = False
            try:
                with debug.span(
                    "changes.execute",
                    worker=id(self),
                    request=sequence,
                    generation=generation,
                    queue_ms=round((time.perf_counter() - queued) * 1000, 3)
                    if queued is not None
                    else None,
                ):
                    value = request(cancelled)
            except Exception:  # noqa: BLE001 -- source details must not cross this boundary
                failed = True

            with self._condition:
                self._active = None
                current = generation == self._generation
                debug.event(
                    "changes.completed",
                    worker=id(self),
                    request=sequence,
                    generation=generation,
                    failed=failed,
                    cancelled=cancelled.is_set(),
                    discarded=self._closed or not current,
                )
                if current:
                    self._keys.discard(key)
                if not self._closed and current and not cancelled.is_set():
                    self._results.append((key, value, failed))
                    self._result_ready.set()
