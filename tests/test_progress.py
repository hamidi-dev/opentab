import io
import os
import tempfile
import threading

from opentab import progress, util
from opentab.stores.combined import CombinedStore

from tests._support import workflow


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_reports_are_noops_without_a_board():
    with progress.task("Codex") as row:
        assert row is None
        assert not progress.active()
        progress.advance(3, 10)


def test_file_reader_reports_done_and_total_on_the_tracked_thread():
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i in range(5):
            path = os.path.join(tmp, f"{i}.jsonl")
            with open(path, "w") as fh:
                fh.write("{}\n")
            paths.append(path)
        progress.start(_Tty())
        try:
            with progress.task("Claude Code") as row:
                assert progress.active()
                assert len(list(util.read_files_parallel(paths))) == 5
                assert (row.done, row.total) == (5, 5)
            # Another thread is not the tracked backend.
            seen = []
            t = threading.Thread(target=lambda: seen.append(progress.active()))
            t.start()
            t.join()
            assert seen == [False]
        finally:
            progress.stop()


def test_combined_store_gives_each_backend_a_row_with_its_cache_outcome():
    class Leaf:
        def __init__(self, name, cached):
            self.source_name = name
            self.served_from_cache = cached

        def workflows(self):
            return [workflow(self.source_name, "2026-01-01T00:00:00")]

    board = progress.start(_Tty())
    try:
        CombinedStore([Leaf("Codex", True), Leaf("Pi", False)]).workflows()
        rows = {t.label: t for t in board._tasks}
    finally:
        progress.stop()
    assert set(rows) == {"Codex", "Pi"}
    assert rows["Codex"].note == "cached" and rows["Pi"].note == ""
    assert all(t.end is not None for t in rows.values())


def test_board_paints_rows_and_erases_them_on_close():
    out = _Tty()
    board = progress.Board(out, delay=60)  # never paints by itself in this test
    slow = board.add("OpenCode")
    slow.done, slow.total = 1, 4
    quick = board.add("Pi")
    quick.end = quick.start  # instant: folded into the summary line
    board._paint()
    frame = out.getvalue()
    assert "OpenCode" in frame and " 25%" in frame
    assert "Pi" not in frame and "✓ 1 more ready" in frame
    board.close()
    # Three painted lines: header, OpenCode, summary -> up two, clear below.
    assert out.getvalue().endswith("\r\x1b[2A\x1b[J")


def test_a_fast_start_never_paints():
    out = _Tty()
    board = progress.Board(out, delay=60)
    board.add("Codex").end = 0.0
    board.close()
    assert out.getvalue() == ""


def test_only_interactive_ansi_terminals_get_bars():
    assert not progress.wanted(io.StringIO())
    old = os.environ.get("TERM")
    os.environ["TERM"] = "dumb"
    try:
        assert not progress.wanted(_Tty())
    finally:
        if old is None:
            del os.environ["TERM"]
        else:
            os.environ["TERM"] = old
