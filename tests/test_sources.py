"""Source resolution and the path-argument routing (sources.py)."""

import os
import tempfile

import opentab as ot

from tests._support import FakeStore, _parse, _write_csv, workflow


def test_next_source_name_names_the_destination():
    with tempfile.TemporaryDirectory() as tmp:
        # both sources present -> the cycle is opencode / claude / all
        db = os.path.join(tmp, "opencode.db")
        open(db, "w").close()
        cdir = os.path.join(tmp, "projects", "slug")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "s.jsonl"), "w") as fh:
            fh.write("{}\n")
        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": db,
                "claude_dir": os.path.join(tmp, "projects"),
                "demo": False,
            },
        )()
        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "opencode"
        assert app.next_source_name() == "Claude Code"
        app.source_key = "claude"
        assert app.next_source_name() == "all"
        app.source_key = "all"
        assert app.next_source_name() == "OpenCode"


def test_path_and_csv_flag_both_select_the_csv_source():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "requests.csv")
        _write_csv(
            csv_path,
            ["timestamp", "model", "input_tokens", "output_tokens"],
            [["2026-06-18T10:00:00Z", "gpt-4o", 100, 10]],
        )
        # All three forms point at the same CSV and open it on its own -- no saying
        # "csv" twice. (The bare positional, the --csv flag, and --source csv + path.)
        for argv in ([csv_path], ["--csv", csv_path], ["--source", "csv", csv_path]):
            a = _parse(argv)
            assert a.source == "csv", argv
            assert a.csv == csv_path, argv
        # Bare `opentab` is unchanged: auto-merge, CSV auto-discovered at the default path.
        bare = _parse([])
        assert bare.source == "auto"
        assert bare.csv == ot.DEFAULT_CSV_PATH


def test_path_arg_infers_source_routes_under_all_and_rejects_bad_paths():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        open(db, "w").close()
        # A .db positional selects opencode and fills --db.
        a = _parse([db])
        assert a.source == "opencode" and a.db == db

        csv_path = os.path.join(tmp, "requests.csv")
        _write_csv(
            csv_path,
            ["timestamp", "model", "input_tokens", "output_tokens"],
            [["2026-06-18T10:00:00Z", "gpt-4o", 100, 10]],
        )
        # --source all keeps the merged view but still routes the path into the csv slot.
        a = _parse(["--source", "all", csv_path])
        assert a.source == "all" and a.csv == csv_path

        # A missing file and an ambiguous directory both exit with an error.
        for bad in ([os.path.join(tmp, "nope.csv")], [tmp]):
            try:
                _parse(bad)
                raise AssertionError(f"expected an error for {bad}")
            except SystemExit:
                pass
