"""Serial background worker for bounded session change reads."""
from __future__ import annotations

import threading
from collections import deque

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
                return False
            if len(self._pending) >= _MAX_PENDING:
                return False
            self._keys.add(key)
            self._pending.append((self._generation, key, request))
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

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                generation, key, request = self._pending.popleft()
                cancelled = threading.Event()
                self._active = cancelled

            value = None
            failed = False
            try:
                value = request(cancelled)
            except Exception:  # noqa: BLE001 -- source details must not cross this boundary
                failed = True

            with self._condition:
                self._active = None
                current = generation == self._generation
                if current:
                    self._keys.discard(key)
                if not self._closed and current and not cancelled.is_set():
                    self._results.append((key, value, failed))
                    self._result_ready.set()
