import contextlib
import io
import unittest
from unittest.mock import patch

import run_tests


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
        code = run_tests.main([])
    text = out.getvalue()
    assert code == 1
    assert "skip t.skips: needs another SQLite" in text and "ok   t.skips" not in text
    assert text.rstrip().endswith("1/3 passed, 1 skipped")
    out = io.StringIO()
    with patch.object(run_tests, "collect", return_value=cases[:1]), contextlib.redirect_stdout(
        out
    ):
        assert run_tests.main([]) == 0
    assert out.getvalue().rstrip().endswith("1/1 passed")
