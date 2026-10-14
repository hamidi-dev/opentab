"""Start OpenTab's real TUI inside Pyodide.

Pyodide cannot start threads. OpenTab uses them for parallel reads (bounded pools
that only need their results) and background workers whose results the TUI polls.
Pools run inline; a started Thread is deferred and run during the next idle input
wait, so the UI still paints "working…" first and picks the result up on the
following poll, as it would natively. Long-lived workers park between requests (see
below). The cosmetic startup spinner is skipped.
"""

import concurrent.futures
import concurrent.futures.thread
import os
import runpy
import sys
import threading
import time

import otterm

# ---------------------------------------------------------------- time
time.sleep = lambda seconds: otterm.sleep(int(max(0, seconds) * 1000))
if not hasattr(time, "thread_time"):  # Pyodide lacks it; diagnostics spans call it
    time.thread_time = time.process_time
    time.thread_time_ns = time.process_time_ns

# ---------------------------------------------------------------- pools run inline


class InlineExecutor(concurrent.futures.Executor):
    def __init__(self, *args, **kwargs):
        pass

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - a Future carries any failure
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


concurrent.futures.ThreadPoolExecutor = InlineExecutor
concurrent.futures.thread.ThreadPoolExecutor = InlineExecutor

# ---------------------------------------------------------------- deferred threads
# A started Thread runs later, during an idle input wait. Long-lived workers (session
# changes, conversation search) loop on Condition.wait() for requests; with no other
# thread to notify them that wait would block the interpreter for good. Instead the
# wait unwinds the loop ("parks" it), notify() marks it runnable, and the next idle
# wait enters the loop again, which then finds the request it was notified about.
_pending = []  # threads to run, oldest first
_parked = {}  # thread -> the Condition it parked on
_notified = set()  # Conditions notified since a thread parked on them
_SKIP = {"opentab-progress"}
_inside = []  # the deferred thread now running, if any


class _Parked(BaseException):
    """Unwind a deferred thread that would wait for work that cannot arrive yet."""


_condition_wait = threading.Condition.wait
_condition_notify = threading.Condition.notify


def _wait(self, timeout=None):
    if _inside:
        raise _Parked(self)
    return _condition_wait(self, 0)  # the main thread never blocks: nothing could wake it


def _notify(self, n=1):
    _notified.add(self)
    return _condition_notify(self, n)


threading.Condition.wait = _wait
threading.Condition.notify = _notify  # notify_all calls notify


def _start(self):
    if self.name in _SKIP:
        self._ot_state = "done"
        return
    # Thread.run() deletes its target afterwards, and a parked loop is entered again.
    self._ot_call = (self._target, self._args, self._kwargs) if self._target else (self.run, (), {})
    self._ot_state = "pending"
    _pending.append(self)


def _run(thread):
    if getattr(thread, "_ot_state", None) != "pending":
        return
    thread._ot_state = "running"
    _inside.append(thread)
    target, args, kwargs = thread._ot_call
    try:
        target(*args, **kwargs)
        thread._ot_state = "done"
    except _Parked as parked:
        thread._ot_state = "parked"
        _parked[thread] = parked.args[0]
    except BaseException:  # noqa: BLE001 - mirror a thread dying on its own
        import traceback

        traceback.print_exc()
        thread._ot_state = "done"
    finally:
        _inside.pop()


def _wake(thread):
    _notified.discard(_parked.pop(thread))
    thread._ot_state = "pending"


def _join(self, timeout=None):
    if getattr(self, "_ot_state", None) == "parked" and _parked.get(self) in _notified:
        _wake(self)
    _run(self)


def _is_alive(self):
    return getattr(self, "_ot_state", None) in ("pending", "running", "parked")


threading.Thread.start = _start
threading.Thread.join = _join
threading.Thread.is_alive = _is_alive


def run_one_pending():
    """Run one deferred thread or notified parked loop; True if anything ran."""
    for thread, condition in list(_parked.items()):
        if condition in _notified:
            _wake(thread)
            _pending.append(thread)
    while _pending:
        thread = _pending.pop(0)
        if getattr(thread, "_ot_state", None) == "pending":
            _run(thread)
            return True
    return False


import curses  # noqa: E402 - the browser curses, after the patches it relies on

curses._idle_hook = run_one_pending


def _browser_clipboard(text):
    # The page writes it with navigator.clipboard during the key press's user activation.
    otterm.clipboard(text)
    return True


def rebind_unpacked_sources(cache_dir):
    """Point warm caches at this browser's copies of the SQLite sources.

    OpenTab keys a SQLite source by [mtime, device, inode] so that a replaced
    database is never mistaken for the old one. Unpacking the history gives every
    file a new device and inode while its bytes and mtime stay the build's, so a
    fingerprint row whose path, size and mtime still match is moved onto the new
    identity; anything else is left for OpenTab to reject as usual.
    """
    import json

    try:
        names = os.listdir(cache_dir)
    except FileNotFoundError:
        return
    for name in names:
        path = os.path.join(cache_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            continue
        changed = False
        for row in payload.get("fingerprint") or [] if isinstance(payload, dict) else []:
            if not (isinstance(row, list) and len(row) == 3 and isinstance(row[2], list) and len(row[2]) == 3):
                continue
            try:
                info = os.stat(row[0])
            except (OSError, TypeError):
                continue
            if info.st_size == row[1] and info.st_mtime_ns == row[2][0]:
                if row[2][1:] != [info.st_dev, info.st_ino]:
                    row[2][1:] = [info.st_dev, info.st_ino]
                    changed = True
        if changed:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))


class _StdlibRest:
    """Imports the standard-library modules /try/ leaves out of its first download.

    tools/build-try.sh splits Pyodide's stdlib into the modules OpenTab was seen to import
    (core.zip, loaded at startup) and everything else (rest.zip, with its module names in
    _otstdlib_rest). The first import of a left-out module fetches rest.zip once; after that
    the module and its submodules import from it like any other zip on sys.path.
    """

    def __init__(self, names, url, path):
        self.names, self.url, self.path = names, url, path

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.names:
            return None
        import zipimport

        if not os.path.exists(self.path):
            print(f"stdlib: fetching the rest for {fullname}")
            otterm.fetch_to(self.url, self.path)
        # A package split across both zips (encodings) finds its left-out submodules here.
        parent = fullname.rpartition(".")[0]
        where = os.path.join(self.path, *parent.split(".")) if parent else self.path
        return zipimport.zipimporter(where).find_spec(fullname)


def install_stdlib_fallback(url):
    try:
        from _otstdlib_rest import NAMES
    except ImportError:  # the whole stdlib shipped (no split in this build)
        return
    sys.meta_path.append(_StdlibRest(NAMES, url, "/lib/python-rest.zip"))


def main(argv):
    import opentab.util

    opentab.util.copy_to_clipboard = _browser_clipboard
    rows, cols = otterm.size()
    os.environ["LINES"], os.environ["COLUMNS"] = str(rows), str(cols)
    sys.argv = ["opentab", *argv]
    try:
        runpy.run_module("opentab", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    finally:
        _pending.clear()
        _parked.clear()
        _notified.clear()
    return 0
