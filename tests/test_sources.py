import contextlib
import io
import json
import os
import sqlite3
import tempfile

import opentab as ot
from opentab.stores.opencode import REQUIRED_SCHEMA

from tests._support import FakeStore, _empty_opencode_db, _parse, _write_csv, workflow


def test_next_source_name_names_the_destination():
    with tempfile.TemporaryDirectory() as tmp:
        # both sources present -> the cycle is opencode / claude / all
        db = os.path.join(tmp, "opencode.db")
        _empty_opencode_db(db)
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
        _empty_opencode_db(db)
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


def test_a_db_that_is_not_opencodes_exits_cleanly_instead_of_a_sqlite_traceback():
    # `--db` (and the documented `opentab path/to/opencode.db` shortcut) names an
    # arbitrary file, so picking the wrong .db is an ordinary mistake. Existence alone
    # used to be the whole check, and the reply was a raw sqlite traceback out of the
    # first query ("no such table: session", or "file is not a database") rather than
    # the clean SystemExit every neighbouring backend produces -- HermesStore, the other
    # DB backend, probes its schema and degrades.
    with tempfile.TemporaryDirectory() as tmp:
        other = os.path.join(tmp, "other.db")
        conn = sqlite3.connect(other)
        conn.execute("create table unrelated (id integer)")
        conn.commit()
        conn.close()
        # Names are not the signal, twice over: `session` is what any web app's session
        # store calls its own (that one died on "no such table: message" one frame later,
        # at the deferred model_breakdown scan), and a foreign PAIR of session+message
        # tables passes a name check and then dies on "no such column: child.id". The
        # check is against opencode.REQUIRED_SCHEMA -- the columns every query path uses
        # unconditionally, declared beside the SQL so the two cannot drift.
        webapp = os.path.join(tmp, "webapp.db")
        conn = sqlite3.connect(webapp)
        conn.executescript(
            "create table session (sid text primary key, data blob);"
            "create table message (mid text primary key, data blob);"
        )
        conn.commit()
        conn.close()
        assert ot.sources.opencode_db_verdict(webapp)[0] == "foreign"
        assert "session.id" in ot.sources.opencode_db_verdict(webapp)[1]
        assert "opencode" not in ot.sources.available_sources(_parse(["--db", webapp]))
        # The requirement is exactly the columns every query path uses unconditionally
        # -- and NOTHING else, or a real old database that works today would be
        # rejected: cost/tokens_*/time_updated/title/directory are all probed with
        # fallbacks. A db with only the required set must both pass and run.
        old_schema = os.path.join(tmp, "old.db")
        conn = sqlite3.connect(old_schema)
        conn.executescript(
            "".join(
                f"create table {table} ({', '.join(cols)});"
                for table, cols in REQUIRED_SCHEMA.items()
            )
        )
        conn.commit()
        conn.close()
        assert ot.sources.opencode_db_verdict(old_schema) == ("", "")
        store = ot.sources.make_store(
            _parse(["--db", old_schema, "--source", "opencode"]), "opencode"
        )[0]
        assert store.workflows() == [] and store.model_breakdown() == []
        prose = os.path.join(tmp, "notes.db")
        with open(prose, "w") as fh:
            fh.write("this is not a database at all\n")

        for path in (other, prose, webapp):
            args = _parse(["--db", path, "--source", "opencode"])
            try:
                ot.sources.make_store(args, "opencode")
            except SystemExit as exc:
                assert "OpenCode database" in str(exc) and path in str(exc)
            else:
                raise AssertionError("expected a SystemExit for " + path)

        # ...and it must not be merged into `all` either, where one unusable file would
        # otherwise take down every other backend in the run.
        assert "opencode" not in ot.sources.available_sources(_parse(["--db", other]))
        # ...and the KIND separates "wrong file" from "can't read this one", which are
        # opposite next steps. A locked db is not evidence about its schema.
        assert ot.sources.opencode_db_verdict(other)[0] == "foreign"
        assert ot.sources.opencode_db_verdict(prose)[0] == "foreign"
        locked = os.path.join(tmp, "locked.db")
        w = sqlite3.connect(locked)
        w.execute("PRAGMA journal_mode=DELETE")  # rollback journal: a writer blocks readers
        w.executescript(
            "create table session (id text primary key, parent_id text, time_created integer);"
            "create table message (id text primary key, session_id text, data text);"
        )
        w.commit()
        w.execute("begin exclusive")
        w.execute("insert into session values ('s1', null, 1780000000000)")
        try:
            kind, msg = ot.sources.opencode_db_verdict(locked)
            assert kind == "unreadable" and "could not be read" in msg
        finally:
            w.rollback()
            w.close()
        assert ot.sources.opencode_db_verdict(locked)[0] == ""  # readable again
        # A missing file keeps its own (different) message.
        missing = os.path.join(tmp, "nope.db")
        try:
            ot.sources.make_store(_parse(["--db", missing, "--source", "opencode"]), "opencode")
        except SystemExit as exc:
            assert "not found" in str(exc)


def test_a_real_opencode_db_is_still_detected_and_opened():
    # The guard above must not cost the ordinary case: a real OpenCode schema (even with
    # no sessions yet) stays present in the cycle and builds a Store.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _empty_opencode_db(db)
        assert "opencode" in ot.sources.available_sources(_parse(["--db", db]))
        store = ot.sources.make_store(_parse(["--db", db, "--source", "opencode"]), "opencode")[0]
        assert store.workflows() == []


class _FakeRemote:
    # Only the two fields _fleet_warning reads; building a real fleet would scan the
    # developer's own harnesses (make_store's remote branch calls available_sources).
    def __init__(self, machines=(), unreadable=()):
        self.machines = list(machines)
        self.unreadable = list(unreadable)


def test_a_fleet_that_dropped_what_you_named_says_so():
    warn = ot.sources._fleet_warning
    # Nothing wrong: silence. A warning here would fire on every ordinary launch.
    assert warn(_FakeRemote(["box"]), "/remotes", "laptop") == ""
    # An unreadable summary is the most actionable of the three, so it wins.
    both = _FakeRemote([], ["/tmp/box.json"])
    assert "box.json" in warn(both, "/remotes", "laptop")
    assert "unreadable" in warn(both, "/remotes", "laptop")
    # Nothing loaded at all names the path it looked in.
    assert "no machine summaries" in warn(_FakeRemote(), "/remotes", "laptop")
    # A pulled label equal to this host's name is folded into the live machine by
    # CombinedStore.machine_meta -- the quiet case, since its sessions still show.
    collision = warn(_FakeRemote(["laptop"]), "/remotes", "laptop")
    assert "laptop" in collision and "--label" in collision


def test_the_unreadable_warning_stays_one_line_for_many_files():
    bad = [f"/tmp/box{n}.json" for n in range(9)]
    line = ot.sources._fleet_warning(_FakeRemote(["box"], bad), "/remotes", "laptop")
    assert "+6" in line and "box8.json" not in line and "\n" not in line


def test_a_summary_passed_as_a_bare_path_is_named_rather_than_called_ambiguous():
    # `opentab box.json` is what you type after `opentab export - > box.json`. It cannot
    # route (a summary is every harness on ANOTHER box, not a harness), but "can't tell
    # which source" sends you to --source, which has no slot for it.
    with tempfile.TemporaryDirectory() as d:
        summary = os.path.join(d, "box.json")
        with open(summary, "w", encoding="utf-8") as fh:
            json.dump({"opentab_export": 2, "label": "box", "workflows": []}, fh)
        plain = os.path.join(d, "plain.json")
        with open(plain, "w", encoding="utf-8") as fh:
            json.dump({"something": "else"}, fh)
        assert ot.sources._is_machine_summary(summary)
        assert not ot.sources._is_machine_summary(plain)
        assert not ot.sources._is_machine_summary(os.path.join(d, "gone.json"))

        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                _parse([summary])
        except SystemExit:
            pass
        assert "opentab remote" in err.getvalue()


def test_the_empty_fleet_line_stands_down_right_after_a_pull():
    # `opentab pull box` that fails has ALREADY printed the ssh reason per machine.
    # Repeating "run `opentab pull HOST`" under it is advice to redo what just failed,
    # and it would displace that reason. The other two states still apply: a pull can
    # land a truncated summary, and it can fetch a box named like this one.
    warn = ot.sources._fleet_warning
    assert warn(_FakeRemote(), "/remotes", "laptop", pulled=True) == ""
    assert "no machine summaries" in warn(_FakeRemote(), "/remotes", "laptop", pulled=False)
    assert "unreadable" in warn(_FakeRemote([], ["/r/b.json"]), "/r", "laptop", pulled=True)
    assert "--label" in warn(_FakeRemote(["laptop"]), "/r", "laptop", pulled=True)


def test_the_collision_line_needs_a_live_machine_to_merge_into():
    # With no local harness RemoteStore is the only store, its metadata stays non-live,
    # and CombinedStore.machine_meta has nothing to fold the same-named summary into --
    # so the line would describe a merge that did not happen.
    warn = ot.sources._fleet_warning
    assert warn(_FakeRemote(["laptop"]), "/r", "laptop", live=True) != ""
    assert warn(_FakeRemote(["laptop"]), "/r", "laptop", live=False) == ""
