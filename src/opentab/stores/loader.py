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


# A pool for one backend's own parse: the same import-nothing boot, with a job list.
_POOL_BOOT = (
    "import pickle,sys\n"
    "sys.path[:] = pickle.load(sys.stdin.buffer)\n"
    "from opentab.stores.loader import _pool_main\n"
    "_pool_main()\n"
)


def pool_workers(jobs: int) -> int:
    """Processes for a parse pool: the read override, else one per core (at most 12)."""
    override = os.environ.get("OPENTAB_MAX_WORKERS")
    if override:
        try:
            return max(1, min(jobs, int(override)))
        except ValueError:
            pass
    return max(1, min(jobs, os.cpu_count() or 1, 12))


def run_pool(func, jobs: list, weights: list, workers: int, on_result=None) -> list | None:
    """``[func(job) for job in jobs]`` across ``workers`` child processes, or None.

    ``func`` must be a module-level function: children import it by name. Jobs are
    dealt largest first to the least loaded child, by ``weights``. ``on_result(i)``
    runs as each job completes (progress). None on any spawn, pickling or child
    failure: the caller then parses in-process, so a pool can only ever save time.
    """
    import pickle
    import subprocess

    if not enabled() or workers < 2 or len(jobs) < 2:
        return None
    loads = [[0, []] for _ in range(workers)]
    for i in sorted(range(len(jobs)), key=lambda i: -weights[i]):
        least = min(loads, key=lambda load: load[0])
        least[0] += weights[i]
        least[1].append(i)
    target = (func.__module__, func.__qualname__)
    results: list = [None] * len(jobs)
    finished = [False] * workers
    procs = []
    lock = threading.Lock()

    def collect(slot: int, proc) -> None:
        while True:
            try:
                kind, *rest = pickle.load(proc.stdout)
            except (EOFError, OSError, pickle.UnpicklingError, ValueError, AttributeError):
                return
            if kind == "result":
                results[rest[0]] = rest[1]
                if on_result is not None:
                    with lock:
                        on_result(rest[0])
            else:
                finished[slot] = kind == "done"
                if kind != "done":
                    debug.event("loader.pool_child_failed", error_type=rest[0])
                return

    with debug.span("loader.pool", jobs=len(jobs), workers=workers) as info:
        readers = []
        try:
            for slot, (_, mine) in enumerate(loads):
                if not mine:
                    finished[slot] = True
                    continue
                proc = subprocess.Popen(
                    [sys.executable, "-c", _POOL_BOOT],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                )
                procs.append(proc)
                pickle.dump(list(sys.path), proc.stdin)
                pickle.dump((target, [(i, jobs[i]) for i in mine]), proc.stdin)
                proc.stdin.close()
                reader = threading.Thread(
                    target=collect, args=(slot, proc), name="opentab-pool", daemon=True
                )
                reader.start()
                readers.append(reader)
        except (OSError, TypeError, ValueError, pickle.PicklingError, AttributeError) as exc:
            debug.event("loader.pool_spawn_failed", error_type=type(exc).__name__)
        for reader in readers:
            reader.join()
        ok = len(readers) == sum(1 for _, mine in loads if mine) and all(finished)
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()
        info["ok"] = ok
    return results if ok else None


def _pool_main() -> None:
    import importlib
    import pickle

    (module, name), jobs = pickle.load(sys.stdin.buffer)
    conn = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        func = importlib.import_module(module)
        for part in name.split("."):
            func = getattr(func, part)
        for i, job in jobs:
            pickle.dump(("result", i, func(job)), conn, protocol=pickle.HIGHEST_PROTOCOL)
        result = ("done",)
    except BaseException as exc:
        result = ("error", type(exc).__name__)
    try:
        pickle.dump(result, conn)
    finally:
        conn.close()
