#!/usr/bin/env python3
"""Run the whole unit suite: `python3 run_tests.py [substring ...]`.

Stdlib only, like the tool itself — CI needs no third-party test runner (pytest
also discovers tests/ if you have it: `pytest tests -k NAME`).

Discovery is by glob over tests/test_*.py, so a new module needs no registration
anywhere. The two guards below are what make that safe: an import error is fatal
(never skipped), and a test module that contributes no tests is an error — either
would otherwise drop tests silently while the run stayed green.
"""

import importlib
import os
import sys
import traceback

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
        mod = importlib.import_module(name)  # an ImportError here is fatal, by design
        found = [
            (f"{fname[:-3]}.{k}", v)
            for k, v in sorted(vars(mod).items())
            if k.startswith("test_") and callable(v) and getattr(v, "__module__", None) == name
        ]
        if not found:
            raise SystemExit(f"{fname} defines no tests — it would be silently skipped")
        cases.extend(found)
    return cases


def main(argv):
    cases = collect()
    if argv:
        cases = [(n, f) for n, f in cases if any(a in n for a in argv)]
        if not cases:
            raise SystemExit(f"no test matches {argv}")
    failures = 0
    for name, fn in cases:
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
