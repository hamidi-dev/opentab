"""Pre-TUI startup progress: stores report work; the CLI decides whether to draw it.

Stores call ``task``/``advance`` unconditionally; with no board started both are
no-ops, so drill-in reads after the TUI opens never touch the terminal. Progress is
per thread because CombinedStore loads each backend on its own worker, and a
backend's file reader yields on that worker even when it reads on a pool.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import threading
import time

_board: Board | None = None
_local = threading.local()

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_BAR = 16
# Below this a backend is "instant" and folds into one summary line once done.
_QUICK_S = 0.05


class _Task:
    __slots__ = ("label", "start", "end", "done", "total", "note")

    def __init__(self, label: str):
        self.label = label
        self.start = time.monotonic()
        self.end: float | None = None
        self.done = 0
        self.total: int | None = None
        self.note = ""


class Board:
    """Repaint one line per backend on stderr until ``close``, then erase them."""

    def __init__(self, stream, delay: float = 0.2, interval: float = 0.08):
        self._stream = stream
        self._delay = delay
        self._interval = interval
        self._lock = threading.Lock()
        self._tasks: list[_Task] = []
        self._start = time.monotonic()
        self._painted = 0  # lines currently on screen
        self._frame = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="opentab-progress", daemon=True)
        self._thread.start()

    def add(self, label: str) -> _Task:
        task = _Task(label)
        with self._lock:
            self._tasks.append(task)
        return task

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        with self._lock:
            self._erase()
            self._stream.flush()

    def _loop(self) -> None:
        # A fast warm start finishes inside the delay and never paints at all.
        if self._stop.wait(self._delay):
            return
        while True:
            with self._lock:
                # Checked under the lock: close() may have erased while this waited.
                if self._stop.is_set():
                    return
                self._paint()
            self._stop.wait(self._interval)

    def _erase(self) -> None:
        if self._painted:
            self._stream.write(
                f"\r\x1b[{self._painted - 1}A\x1b[J" if self._painted > 1 else "\r\x1b[J"
            )
            self._painted = 0

    def _paint(self) -> None:
        self._frame += 1
        width = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
        now = time.monotonic()
        lines = [self._header(now)]
        quick = 0
        for t in self._tasks:
            if t.end is not None and t.end - t.start < _QUICK_S:
                quick += 1
                continue
            lines.append(self._row(t, now))
        if quick:
            lines.append(f"  ✓ {quick} more ready")
        self._erase()
        self._stream.write("\n".join(line[:width] for line in lines))
        self._painted = len(lines)
        self._stream.flush()

    def _header(self, now: float) -> str:
        running = sum(1 for t in self._tasks if t.end is None)
        if not self._tasks:
            return f"OpenTab  reading caches…  {_elapsed(now - self._start)}"
        noun = "harness" if len(self._tasks) == 1 else "harnesses"
        state = f"{running} still loading" if running else "opening"
        return f"OpenTab  {len(self._tasks)} {noun} · {state}  {_elapsed(now - self._start)}"

    def _row(self, t: _Task, now: float) -> str:
        name = f"  {t.label[:12]:<12} "
        if t.end is not None:
            note = f"  {t.note}" if t.note else ""
            return f"{name}✓ {_elapsed(t.end - t.start)}{note}"
        spin = _SPIN[self._frame % len(_SPIN)]
        if t.total:
            frac = min(1.0, t.done / t.total)
            fill = round(frac * _BAR)
            bar = "█" * fill + "░" * (_BAR - fill)
            return f"{name}{bar} {frac * 100:3.0f}%  {_elapsed(now - t.start)}"
        return f"{name}{spin} {_elapsed(now - t.start)}"


def _elapsed(seconds: float) -> str:
    return f"{seconds * 1000:.0f}ms" if seconds < 1 else f"{seconds:.1f}s"


def wanted(stream=None) -> bool:
    """Draw only on an interactive, ANSI-capable stderr."""
    stream = stream or sys.stderr
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        return False
    # Windows consoles need VT mode enabled first; keep the plain hint there.
    return tty and os.name != "nt" and os.environ.get("TERM", "") != "dumb"


def start(stream=None) -> Board:
    global _board
    _board = Board(stream or sys.stderr)
    return _board


def stop() -> None:
    global _board
    board, _board = _board, None
    if board is not None:
        board.close()


@contextlib.contextmanager
def task(label: str):
    """Track one backend's load on this thread; yields the task or ``None``."""
    board = _board
    if board is None:
        yield None
        return
    previous = getattr(_local, "task", None)
    current = board.add(label)
    _local.task = current
    try:
        yield current
    finally:
        current.end = time.monotonic()
        _local.task = previous


def active() -> bool:
    """True on a tracked thread, so a backend can skip work that only feeds a bar."""
    return getattr(_local, "task", None) is not None


def advance(done: int, total: int | None = None) -> None:
    """Report this thread's backend progress; a no-op outside a tracked task."""
    current = getattr(_local, "task", None)
    if current is not None:
        current.done = done
        if total is not None:
            current.total = total


def note(store) -> str:
    """Cache outcome for a finished row, from CachedStore's per-call flags."""
    if getattr(store, "served_from_cache", None):
        return "cached"
    if getattr(store, "served_incrementally", False):
        return "incremental"
    return ""
