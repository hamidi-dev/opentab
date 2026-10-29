#!/usr/bin/env python3
"""Run the whole unit suite: `python3 run_tests.py [-j N] [substring ...]`.

Stdlib only, like the tool itself — CI needs no third-party test runner (pytest
also discovers tests/ if you have it: `pytest tests -k NAME`).

Discovery is by glob over tests/test_*.py, so a new module needs no registration
anywhere. The two guards below are what make that safe: an import error is fatal
(never skipped), and a test module that contributes no tests is an error — either
would otherwise drop tests silently while the run stayed green.

Tests run on one worker per CPU; `-j 1` keeps them in this process, in order. Workers
are spawned, never forked: each fresh interpreter imports tests/__init__.py and so owns
its XDG roots, which import time resolves and fork would share. Each pulls its next test
from a queue because a few modules hold most of the suite's time. A worker that dies is
a failure of the test it was running, so a crash cannot shrink the run.
"""

import argparse
import collections
import importlib
import multiprocessing
import multiprocessing.connection
import os
import sys
import tempfile
import traceback
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(ROOT, "tests")


def collect():
    sys.path.insert(0, ROOT)
    cases = []
    files = sorted(f for f in os.listdir(TESTS) if f.startswith("test_") and f.endswith(".py"))
    if not files:
        raise SystemExit(f"no test modules found in {TESTS}")
    for fname in files:
        name = f"tests.{fname[:-3]}"
        mod = importlib.import_module(name)
        found = [
            (f"{fname[:-3]}.{k}", v)
            for k, v in sorted(vars(mod).items())
            if k.startswith("test_") and callable(v) and getattr(v, "__module__", None) == name
        ]
        if not found:
            raise SystemExit(f"{fname} defines no tests — it would be silently skipped")
        cases.extend(found)
    return cases


def run_case(fn):
    try:
        fn()
    except unittest.SkipTest as exc:
        # Only a test that cannot run here skips; it must never read as a pass.
        return "skip", str(exc)
    except Exception:
        return "FAIL", traceback.format_exc()
    return "ok", ""


def worker(conn):
    cases = dict(collect())
    # File descriptors, not sys.stdout: child processes write here too, and the output
    # of concurrent tests only makes sense attached to the failure that printed it.
    output = tempfile.TemporaryFile()
    os.dup2(output.fileno(), 1)
    os.dup2(output.fileno(), 2)
    while True:
        try:
            name = conn.recv()
        except EOFError:
            return
        status, detail = run_case(cases[name])
        sys.stdout.flush()
        sys.stderr.flush()
        if status == "FAIL":
            output.seek(0)
            detail += output.read().decode("utf-8", "replace")
        output.seek(0)
        output.truncate()
        conn.send((status, detail))


def run_parallel(names, jobs, report):
    context = multiprocessing.get_context("spawn")
    queue = collections.deque(names)
    running = {}  # pipe -> (worker process, the test it was last handed)

    def assign(conn, process):
        if not queue:
            conn.close()
            process.join()
            return
        name = queue.popleft()
        running[conn] = (process, name)
        try:
            conn.send(name)
        except OSError:
            pass  # Already dead; the wait below reports it against this test.

    def spawn():
        ours, theirs = context.Pipe()
        process = context.Process(target=worker, args=(theirs,))
        process.start()
        theirs.close()
        assign(ours, process)

    try:
        for _ in range(jobs):
            spawn()
        while running:
            for conn in multiprocessing.connection.wait(list(running)):
                process, name = running.pop(conn)
                try:
                    status, detail = conn.recv()
                except (EOFError, OSError):
                    conn.close()
                    process.join()
                    report(name, "FAIL", f"worker exited {process.exitcode} during this test\n")
                    if queue:
                        spawn()
                else:
                    report(name, status, detail)
                    assign(conn, process)
    finally:
        for process, _ in running.values():
            process.terminate()


def default_jobs():
    # `coverage run` traces this process only, so workers would hide their lines from it.
    return 1 if "coverage" in sys.modules else os.cpu_count() or 1


def job_count(value):
    jobs = int(value)
    if jobs < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return jobs


def main(argv):
    parser = argparse.ArgumentParser(description="Run the OpenTab unit suite.")
    parser.add_argument("substrings", nargs="*", metavar="substring", help="module/test filter")
    parser.add_argument(
        "-j", "--jobs", type=job_count, default=default_jobs(), help="workers; 1 runs in-process"
    )
    args = parser.parse_args(argv)
    cases = collect()
    if args.substrings:
        cases = [(n, f) for n, f in cases if any(a in n for a in args.substrings)]
        if not cases:
            raise SystemExit(f"no test matches {args.substrings}")
    counts = collections.Counter()

    def report(name, status, detail):
        counts[status] += 1
        if status == "ok":
            print(f"ok   {name}")
        elif status == "skip":
            print(f"skip {name}: {detail}")
        else:
            print(f"FAIL {name}", flush=True)
            print(detail, end="", file=sys.stderr, flush=True)

    jobs = min(args.jobs, len(cases))
    if jobs > 1:
        run_parallel([name for name, _ in cases], jobs, report)
    else:
        for name, fn in cases:
            report(name, *run_case(fn))
    summary = f"{counts['ok']}/{len(cases)} passed"
    print(f"\n{summary}, {counts['skip']} skipped" if counts["skip"] else f"\n{summary}")
    return 0 if counts["ok"] + counts["skip"] == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
