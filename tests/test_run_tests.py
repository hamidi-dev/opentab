import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import run_tests


def _run_suite(tmp, source, *argv):
    # The real runner over a throwaway tests/ package, so workers, crashes and exit
    # codes are the real ones. The package init stands in for tests/__init__.py.
    package = os.path.join(tmp, "tests")
    os.mkdir(package)
    shutil.copy(run_tests.__file__, tmp)
    with open(os.path.join(package, "__init__.py"), "w") as fh:
        fh.write(
            "import os, tempfile\n"
            "os.environ['XDG_STATE_HOME'] = tempfile.mkdtemp(dir=os.path.dirname(__file__))\n"
        )
    with open(os.path.join(package, "test_fake.py"), "w") as fh:
        fh.write(textwrap.dedent(source))
    return subprocess.run(
        [sys.executable, "run_tests.py", *argv],
        cwd=tmp,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_runner_reports_skips_apart_from_passes_and_still_fails_the_run():
    def passes():
        pass

    def skips():
        raise unittest.SkipTest("needs another SQLite")

    def fails():
        raise AssertionError("broken")

    cases = [("t.passes", passes), ("t.skips", skips), ("t.fails", fails)]
    out = io.StringIO()
    with patch.object(run_tests, "collect", return_value=cases), contextlib.redirect_stdout(
        out
    ), contextlib.redirect_stderr(io.StringIO()):
        code = run_tests.main(["--jobs", "1"])
    text = out.getvalue()
    assert code == 1
    assert "skip t.skips: needs another SQLite" in text and "ok   t.skips" not in text
    assert text.rstrip().endswith("1/3 passed, 1 skipped")
    out = io.StringIO()
    with patch.object(run_tests, "collect", return_value=cases[:1]), contextlib.redirect_stdout(
        out
    ):
        assert run_tests.main(["--jobs", "1"]) == 0
    assert out.getvalue().rstrip().endswith("1/1 passed")


def test_runner_workers_overlap_and_each_owns_its_xdg_roots():
    # Each test waits for the other to start, which only concurrent workers can satisfy.
    source = """
    import os, time

    HERE = os.path.dirname(__file__)

    def _meet(me, other):
        with open(os.path.join(HERE, me), "w") as fh:
            fh.write(os.environ["XDG_STATE_HOME"])
        deadline = time.monotonic() + 30
        while not os.path.exists(os.path.join(HERE, other)):
            assert time.monotonic() < deadline
            time.sleep(0.005)

    def test_left():
        _meet("left", "right")

    def test_right():
        _meet("right", "left")
    """
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_suite(tmp, source, "--jobs", "2")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.rstrip().endswith("2/2 passed")
        homes = set()
        for name in ("left", "right"):
            with open(os.path.join(tmp, "tests", name)) as fh:
                homes.add(fh.read())
        assert len(homes) == 2


def test_runner_workers_report_each_outcome_once_with_the_failing_tests_output():
    source = """
    import subprocess, sys, unittest

    def test_pass():
        print("quiet")

    def test_skip():
        raise unittest.SkipTest("needs another SQLite")

    def test_fail():
        print("from the test")
        subprocess.run([sys.executable, "-c", "print('from its child')"])
        raise AssertionError("broken")

    def test_filtered_out():
        raise AssertionError("must not run")
    """
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_suite(tmp, source, "-j", "2", "pass", "skip", "fail")
    lines = result.stdout.splitlines()
    assert result.returncode == 1
    assert sorted(ln for ln in lines if ln) == [
        "1/3 passed, 1 skipped",
        "FAIL test_fake.test_fail",
        "ok   test_fake.test_pass",
        "skip test_fake.test_skip: needs another SQLite",
    ]
    assert lines[-1] == "1/3 passed, 1 skipped"
    assert "AssertionError: broken" in result.stderr
    assert "from the test" in result.stderr and "from its child" in result.stderr
    assert "quiet" not in result.stdout + result.stderr


def test_runner_blames_a_dead_worker_on_its_test_and_finishes_the_rest():
    source = """
    import os, sys

    def test_a_exit():
        os._exit(3)

    def test_b_system_exit():
        sys.exit(0)

    def test_c_after_the_crashes():
        pass

    def test_d_after_the_crashes():
        pass
    """
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_suite(tmp, source, "--jobs", "2")
    assert result.returncode == 1
    assert "FAIL test_fake.test_a_exit" in result.stdout
    assert "FAIL test_fake.test_b_system_exit" in result.stdout
    assert "worker exited 3 during this test" in result.stderr
    assert "worker exited 0 during this test" in result.stderr
    assert "ok   test_fake.test_c_after_the_crashes" in result.stdout
    assert "ok   test_fake.test_d_after_the_crashes" in result.stdout
    assert result.stdout.rstrip().endswith("2/4 passed")


def test_runner_fails_closed_when_a_test_never_reports():
    cases = [("t.first", lambda: None), ("t.second", lambda: None)]
    out = io.StringIO()
    with patch.object(run_tests, "collect", return_value=cases), patch.object(
        run_tests, "run_parallel"
    ), contextlib.redirect_stdout(out):
        assert run_tests.main(["--jobs", "2"]) == 1
    assert out.getvalue().rstrip().endswith("0/2 passed")


def test_runner_stays_in_process_for_coverage_and_for_a_single_test():
    with patch.dict(sys.modules, {"coverage": object()}):
        assert run_tests.default_jobs() == 1
    with patch.dict(sys.modules), patch.object(os, "cpu_count", return_value=12):
        sys.modules.pop("coverage", None)
        assert run_tests.default_jobs() == 12
    # A local function cannot reach a worker, so passing proves nothing was spawned.
    ran = []
    cases = [("t.only", lambda: ran.append(os.getpid()))]
    with patch.object(run_tests, "collect", return_value=cases), contextlib.redirect_stdout(
        io.StringIO()
    ):
        assert run_tests.main(["--jobs", "8"]) == 0
    assert ran == [os.getpid()]


def test_runner_rejects_a_job_count_below_one():
    for value in ("0", "-1", "many"):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                run_tests.main(["--jobs", value])
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError("expected a usage error for " + value)
        assert "--jobs" in err.getvalue()
