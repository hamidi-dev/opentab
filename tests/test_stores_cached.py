"""CachedStore: the warm-start rollup cache (stores/cached.py)."""

import json
import os
import sqlite3
import tempfile

import opentab as ot

from tests._support import workflow


def test_cached_store_warm_start_and_invalidation():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
        data = os.path.join(tmp, "data.jsonl")
        with open(data, "w") as fh:
            fh.write("one\n")

        class Backend:
            combined = False
            records_cost = False
            demo = False
            source_name = "Fake"

            def __init__(self):
                self.workflow_calls = 0
                self.breakdown_calls = 0

            def cache_inputs(self):
                return [data]

            def workflows(self):
                self.workflow_calls += 1
                return [workflow("s1", "2026-06-01 12:00:00", cost=0.0, tokens=100)]

            def model_breakdown(self):
                self.breakdown_calls += 1
                return [
                    {"root_id": "s1", "model_name": "anthropic/x", "runs": 1, "tokens_total": 100}
                ]

        args = type("Args", (), {"demo": False, "no_cache": False})()
        cid = "fake|" + data
        try:
            # Cold: the first wrapper parses (once each) and writes the cache.
            b1 = Backend()
            c1 = ot.CachedStore(b1, cid, args)
            wf1 = c1.workflows()
            mb1 = c1.model_breakdown()
            assert b1.workflow_calls == 1 and b1.breakdown_calls == 1
            assert [w.id for w in wf1] == ["s1"] and mb1[0]["root_id"] == "s1"

            # Warm: a fresh wrapper over the UNCHANGED file serves the cached rollup and
            # never touches the backend -- the whole point of the warm start.
            b2 = Backend()
            c2 = ot.CachedStore(b2, cid, args)
            wf2 = c2.workflows()
            mb2 = c2.model_breakdown()
            assert b2.workflow_calls == 0 and b2.breakdown_calls == 0
            assert [w.id for w in wf2] == ["s1"] and mb2 == mb1  # identical, round-tripped

            # Invalidate: editing the file changes size+mtime -> miss -> real re-parse.
            with open(data, "a") as fh:
                fh.write("two\n")
            b3 = Backend()
            c3 = ot.CachedStore(b3, cid, args)
            c3.workflows()
            c3.model_breakdown()
            assert b3.workflow_calls == 1 and b3.breakdown_calls == 1

            # --no-cache passes the raw backend straight through (no wrapper).
            raw = ot.sources._wrap_cache(Backend(), "fake", type("A", (), {"no_cache": True})())
            assert isinstance(raw, Backend)
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_cached_store_serves_records_cost_and_survives_field_drift():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
        data = os.path.join(tmp, "data.jsonl")
        with open(data, "w") as fh:
            fh.write("one\n")

        class Backend:
            combined = False
            demo = False
            source_name = "Fake"

            def __init__(self):
                self.workflow_calls = 0
                self.probe_calls = 0

            @property
            def records_cost(self):
                self.probe_calls += 1  # stands in for the full-corpus cost probe
                return True

            def cache_inputs(self):
                return [data]

            def workflows(self):
                self.workflow_calls += 1
                return [workflow("s1", "2026-06-01 12:00:00", cost=2.0, tokens=100)]

            def model_breakdown(self):
                return [
                    {"root_id": "s1", "model_name": "anthropic/x", "runs": 1, "tokens_total": 100}
                ]

        args = type("Args", (), {"demo": False, "no_cache": False})()
        cid = "fake|" + data
        try:
            # Cold: a real parse; the write reads records_cost off the backend (once).
            b1 = Backend()
            c1 = ot.CachedStore(b1, cid, args)
            c1.workflows()
            c1.model_breakdown()
            assert b1.probe_calls == 1

            # Warm: records_cost round-trips from the cache -- the backend's probe is
            # never touched, whether it's read after workflows() or straight away.
            b2 = Backend()
            c2 = ot.CachedStore(b2, cid, args)
            c2.workflows()
            assert c2.records_cost is True
            assert b2.probe_calls == 0 and b2.workflow_calls == 0
            b3 = Backend()
            assert ot.CachedStore(b3, cid, args).records_cost is True  # fingerprints itself
            assert b3.probe_calls == 0

            # A cached row that no longer matches the Workflow dataclass (field drift
            # without a version bump) falls back to a real parse instead of crashing.
            with open(c1._path) as fh:
                payload = json.load(fh)
            payload["workflows"][0]["bogus_field"] = 1
            with open(c1._path, "w") as fh:
                json.dump(payload, fh)
            b4 = Backend()
            c4 = ot.CachedStore(b4, cid, args)
            wf4 = c4.workflows()
            assert b4.workflow_calls == 1 and [w.id for w in wf4] == ["s1"]
            assert c4.served_from_cache is False
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_cache_invalidates_on_wal_write_so_reload_sees_new_opencode_sessions():
    # OpenCode runs SQLite in WAL mode, so a new session lands in <db>-wal while the
    # main .db's size/mtime don't move until a checkpoint. cache_inputs() must
    # fingerprint the WAL sidecars, or CachedStore keeps serving the stale rollup and a
    # reload (r) / the browser's refresh never shows sessions written since -- the
    # reported "--web refresh doesn't get new sessions" bug.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
        db = os.path.join(tmp, "opencode.db")
        # Writer stays open the whole test with autocheckpoint off, so every commit
        # stays in the -wal file and the main .db is never checkpointed/rewritten.
        w = sqlite3.connect(db)
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.executescript(
            """
            create table session (
              id text primary key, parent_id text, title text, directory text,
              time_created integer, cost real default 0 not null,
              tokens_input integer default 0 not null, tokens_output integer default 0 not null,
              tokens_reasoning integer default 0 not null, tokens_cache_read integer default 0 not null,
              tokens_cache_write integer default 0 not null
            );
            create table message (id text primary key, session_id text, data text);
            """
        )
        w.execute(
            "insert into session values ('s1',null,'One','/work/repo',1760000000000,1.0,0,0,0,0,0)"
        )
        w.commit()
        try:
            store = ot.Store(db, type("A", (), {"demo": False})())
            ci = store.cache_inputs()
            assert db in ci and db + "-wal" in ci and db + "-shm" in ci  # sidecars fingerprinted

            cid = "opencode|" + db
            cargs = type("A", (), {"demo": False, "no_cache": False})()

            # Cold: parse s1 and write the cache (workflows + breakdown both fresh).
            c1 = ot.CachedStore(store, cid, cargs)
            assert [x.id for x in c1.workflows()] == ["s1"]
            c1.model_breakdown()
            assert c1.served_from_cache is False

            # Warm: a fresh wrapper over the unchanged DB serves the cache untouched.
            c2 = ot.CachedStore(store, cid, cargs)
            c2.workflows()
            assert c2.served_from_cache is True

            # OpenCode adds a new session -> it lands in the WAL, main .db mtime unchanged.
            mtime_before = os.stat(db).st_mtime_ns
            w.execute(
                "insert into session values ('s2',null,'Two','/work/repo',1760000100000,2.0,0,0,0,0,0)"
            )
            w.commit()
            assert os.stat(db).st_mtime_ns == mtime_before  # the WAL grew, not the .db

            # A reload now MISSES the cache (the -wal fingerprint moved) and re-parses,
            # so the new session is visible -- the fix.
            c3 = ot.CachedStore(store, cid, cargs)
            wf3 = c3.workflows()
            assert c3.served_from_cache is False
            assert sorted(x.id for x in wf3) == ["s1", "s2"]
        finally:
            w.close()
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg
