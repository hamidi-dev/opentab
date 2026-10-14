"""Parse cold backends in separate processes so they really run at the same time.

Backend parsing is Python bytecode, so CombinedStore's threads hold the GIL in turn:
a cold Claude Code parse finishes before OpenCode advances. When two or more
backends need a full parse, all but the largest run ``workflows()`` and
``model_breakdown()`` in their own process, which writes the ordinary warm-start cache. The parent
then adopts that cache like any warm start, so the parent keeps sole ownership of
drill-in state and no rows cross the pipe. Progress is forwarded for the startup
board. Any failure leaves the parent to parse in-process as before.

Under ``--no-cache`` the child writes the same payload to a private temporary file
instead, which the parent adopts and deletes at exit; the real cache is untouched.
"""
from __future__ import annotations

import atexit
import itertools
import os
import shutil
import sys
import tempfile
import threading

from opentab import diagnostics as debug
from opentab import progress

# Below this many input bytes a parse costs less than spawning an interpreter.
MIN_PARSE_BYTES = 32 * 1024 * 1024
# Progress updates the child sends at most this often (the board repaints at 80 ms).
_PROGRESS_S = 0.08

_handoff_dir: str | None = None
_handoff_lock = threading.Lock()
_handoff_ids = itertools.count()


def enabled() -> bool:
    """Loader processes are on unless OPENTAB_LOADER_PROCESSES=0."""
    return os.environ.get("OPENTAB_LOADER_PROCESSES", "").strip() not in ("0", "false", "no")


def heavy(stores: list) -> set:
    """Indexes of stores to parse in loader processes.

    Needs two cold backends. The largest stays in this process, overlapping the
    rest: it skips the cache round trip and keeps its model scan deferred.
    """
    if not enabled():
        return set()
    sizes = {}
    for i, store in enumerate(stores):
        probe = getattr(store, "full_parse_bytes", None)
        if getattr(store, "rebuild", None) is None or probe is None:
            continue
        try:
            size = probe()
        except OSError:
            continue
        if size >= MIN_PARSE_BYTES:
            sizes[i] = size
    if len(sizes) < 2:
        return set()
    sizes.pop(max(sizes, key=sizes.get))
    return set(sizes)


def warm(store, task=None) -> bool:
    """Parse ``store`` in a child process; True once its cache is adopted."""
    builder, args, key = store.rebuild
    with debug.span("loader.process", source=key) as info:
        try:
            handoff = None if getattr(store, "persistent", True) else _handoff_path(key)
        except OSError as exc:
            debug.event("loader.spawn_failed", source=key, error_type=type(exc).__name__)
            return False
        ok = _run_child(builder, args, key, task, handoff)
        info["ok"] = ok
    if not ok:
        return False
    store.load_from_disk(handoff)
    return True


def _handoff_path(key: str) -> str:
    """A fresh file in this run's private (0700) handoff directory."""
    global _handoff_dir
    with _handoff_lock:
        if _handoff_dir is None:
            _handoff_dir = tempfile.mkdtemp(prefix="opentab-load-")
            atexit.register(shutil.rmtree, _handoff_dir, True)
        return os.path.join(_handoff_dir, f"{key}-{next(_handoff_ids)}.json")


# The child imports nothing of the caller's: multiprocessing's spawn would re-run an
# unguarded __main__ (an API user's script) in every worker.
_BOOT = (
    "import pickle,sys\n"
    "sys.path[:] = pickle.load(sys.stdin.buffer)\n"
    "from opentab.stores.loader import _child_main\n"
    "_child_main()\n"
)


def _run_child(builder, args, key: str, task, handoff: str | None = None) -> bool:
    import pickle
    import subprocess

    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", _BOOT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
    except (OSError, TypeError, ValueError) as exc:  # no usable interpreter, resource limits
        debug.event("loader.spawn_failed", source=key, error_type=type(exc).__name__)
        return False
    ok = False
    try:
        try:
            pickle.dump(list(sys.path), proc.stdin)
            # Track only under a drawn row: progress.active() gates bar-only work.
            pickle.dump((builder, args, key, task is not None, handoff), proc.stdin)
            proc.stdin.close()
        except (OSError, pickle.PicklingError, AttributeError, TypeError) as exc:
            debug.event("loader.spawn_failed", source=key, error_type=type(exc).__name__)
            return False
        while True:
            try:
                kind, *rest = pickle.load(proc.stdout)
            except (EOFError, OSError, pickle.UnpicklingError, ValueError):
                break
            if kind == "progress":
                if task is not None:
                    task.done, task.total = rest
            else:
                ok = kind == "done"
                if not ok:
                    debug.event("loader.child_failed", source=key, error_type=rest[0])
                break
    finally:
        if proc.poll() is None and not ok:
            proc.kill()
        proc.stdout.close()
        proc.wait()
    return ok


def _child_main() -> None:
    import pickle

    builder, args, key, tracked, handoff = pickle.load(sys.stdin.buffer)
    # Messages own the real stdout; anything a backend prints goes to stderr instead.
    conn = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    lock = threading.Lock()
    task = progress.Task(key)
    if tracked:
        progress.track(task)  # stores report into this task, as on a tracked thread
    stop = threading.Event()

    def send(message) -> None:
        with lock:
            pickle.dump(message, conn)
            conn.flush()

    def forward() -> None:
        last = None
        while not stop.wait(_PROGRESS_S):
            now = (task.done, task.total)
            if now != last:
                last = now
                try:
                    send(("progress", *now))
                except OSError:
                    return

    pump = threading.Thread(target=forward, name="opentab-load-progress", daemon=True)
    pump.start()
    try:
        store = builder(args, key)
        if handoff is not None:
            store = store.writing_to(handoff)  # --no-cache: a private file, not the cache
        store.workflows()
        store.model_breakdown()  # completes the cache write the parent will read
        result = ("done",)
    except BaseException as exc:
        result = ("error", type(exc).__name__)
    stop.set()
    pump.join()
    try:
        send(("progress", task.done, task.total))  # the final count, however fast the parse
        send(result)
    finally:
        conn.close()
