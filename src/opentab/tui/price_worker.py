"""One explicit price fetch, isolated from App, stores and live pricing caches."""
from __future__ import annotations

import threading
from collections.abc import Callable


class PriceRefreshJob:
    def __init__(self, fetch: Callable[[], tuple[int, str]]):
        self.done = threading.Event()
        self.count = 0
        self.error = ""
        self.thread = threading.Thread(
            target=self._run, args=(fetch,), name="opentab-prices", daemon=True
        )
        self.thread.start()

    def _run(self, fetch: Callable[[], tuple[int, str]]) -> None:
        try:
            self.count, _ = fetch()
        except Exception as exc:  # Worker failures are adopted as notices on the UI thread.
            self.error = str(exc) or type(exc).__name__
        finally:
            self.done.set()
