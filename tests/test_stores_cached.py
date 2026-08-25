"""CachedStore: the warm-start rollup cache (stores/cached.py)."""

import json
import os
import sqlite3
import tempfile

import opentab as ot

from tests._support import workflow


def _fixture_model_row(root_id, tokens, model="anthropic/x"):
    # A model row shaped like a real backend's: CachedStore._read rejects a cached
    # payload whose rows lack any key the app INDEXES rather than .get()s
    # (cached.MODEL_ROW_KEYS -- Renderer._mix_rows reads every one of these), because
    # a launch served that row off the HIT path raises KeyError with nothing left to
    # re-parse. Every shipped backend emits all of them; a fixture must too.
    return {
        "root_id": root_id,
        "model_name": model,
        "runs": 1,
        "cost": 0.0,
        "tokens_total": tokens,
        "cache_read": 0,
        "cache_write": 0,
        "output": 0,
    }


def test_cached_store_warm_start_and_invalidation():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
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
                return [_fixture_model_row("s1", 100)]

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
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_cached_store_serves_records_cost_and_survives_field_drift():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
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
                return [_fixture_model_row("s1", 100)]

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
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_cached_store_round_trips_ended_at():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
        data = os.path.join(tmp, "data.jsonl")
        with open(data, "w") as fh:
            fh.write("one\n")

        class Backend:
            combined = False
            records_cost = False
            demo = False
            source_name = "Fake"

            def cache_inputs(self):
                return [data]

            def workflows(self):
                return [workflow("s1", "2026-06-01 12:00:00", ended_at="2026-06-01 15:45:00")]

            def model_breakdown(self):
                return []

        args = type("Args", (), {"demo": False, "no_cache": False})()
        cid = "fake|" + data
        try:
            # Cold: writes the cache with ended_at in the payload (the write itself
            # is triggered from model_breakdown(), which needs both fresh rollups).
            c1 = ot.CachedStore(Backend(), cid, args)
            assert c1.workflows()[0].ended_at == "2026-06-01 15:45:00"
            c1.model_breakdown()

            # Warm: served from the cached JSON, ended_at survives the round trip.
            c2 = ot.CachedStore(Backend(), cid, args)
            wf2 = c2.workflows()
            assert c2.served_from_cache is True
            assert wf2[0].ended_at == "2026-06-01 15:45:00"
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_cached_store_invalidates_a_stale_cache_version():
    # A cache file written by an older opentab carries an older CACHE_VERSION; that
    # mismatch alone must force a real re-parse rather than silently serving rows
    # shaped for the old version until some unrelated file edit.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
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

            def cache_inputs(self):
                return [data]

            def workflows(self):
                self.workflow_calls += 1
                return [workflow("s1", "2026-06-01 12:00:00")]

            def model_breakdown(self):
                return []

        args = type("Args", (), {"demo": False, "no_cache": False})()
        cid = "fake|" + data
        try:
            b1 = Backend()
            c1 = ot.CachedStore(b1, cid, args)
            c1.workflows()
            c1.model_breakdown()  # triggers the actual disk write
            assert b1.workflow_calls == 1

            with open(c1._path) as fh:
                payload = json.load(fh)
            payload["version"] = ot.stores.cached.CACHE_VERSION - 1
            with open(c1._path, "w") as fh:
                json.dump(payload, fh)

            b2 = Backend()
            c2 = ot.CachedStore(b2, cid, args)
            c2.workflows()
            assert b2.workflow_calls == 1  # a miss, not served from the stale-shaped cache
            assert c2.served_from_cache is False
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_cache_invalidates_on_wal_write_so_reload_sees_new_opencode_sessions():
    # OpenCode runs SQLite in WAL mode, so a new session lands in <db>-wal while the
    # main .db's size/mtime don't move until a checkpoint. cache_inputs() must
    # fingerprint the WAL sidecars, or CachedStore keeps serving the stale rollup and a
    # reload (r) / the browser's refresh never shows sessions written since -- the
    # reported "--web refresh doesn't get new sessions" bug.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cfg")  # isolate the cache dir
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
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


class _SpliceBackend:
    """A backend that implements the two incremental opt-ins over a toy file->session
    map, and counts what it was asked to read."""

    combined = False
    records_cost = False
    demo = False
    source_name = "Fake"

    def __init__(self, layout):
        self.layout = layout  # {path: {sid: tokens}}
        self.full_parses = 0
        self.subset_calls = []
        self.refuse = set()  # paths parse_subset must refuse (a replay transcript)

    def cache_inputs(self):
        return sorted(self.layout)

    def _sessions(self, paths):
        out = {}
        for path in paths:
            for sid, tokens in self.layout.get(path, {}).items():
                out[sid] = out.get(sid, 0) + tokens
        return out

    @staticmethod
    def sort_workflows(rows):
        rows = sorted(rows, key=lambda w: w.id)
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return rows

    def _rows(self, sessions):
        rows = [
            workflow(sid, "2026-06-01 12:00:00", cost=0.0, tokens=tokens)
            for sid, tokens in sessions.items()
        ]
        models = [_fixture_model_row(sid, tokens) for sid, tokens in sessions.items()]
        return self.sort_workflows(rows), models

    def workflows(self):
        self.full_parses += 1
        self._last = self._sessions(self.cache_inputs())
        return self._rows(self._last)[0]

    def model_breakdown(self):
        return self._rows(self._sessions(self.cache_inputs()))[1]

    def cache_provenance(self):
        prov = {}
        for path, sessions in self.layout.items():
            for sid in sessions:
                prov.setdefault(sid, []).append(path)
        return {sid: sorted(paths) for sid, paths in prov.items()}

    def parse_subset(self, paths):
        self.subset_calls.append(sorted(paths))
        if self.refuse.intersection(paths):
            return None
        sessions = self._sessions(paths)
        rows, models = self._rows(sessions)
        prov = {}
        for path in paths:
            for sid in self.layout.get(path, {}):
                prov.setdefault(sid, []).append(path)
        return rows, models, {sid: sorted(p) for sid, p in prov.items()}


def _splice_env(tmp, layout):
    os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cfg")
    paths = {}
    for name, sessions in layout.items():
        path = os.path.join(tmp, name)
        with open(path, "w") as fh:
            fh.write(name + "\n")
        paths[path] = sessions
    return paths


def _touch(path, text):
    with open(path, "a") as fh:
        fh.write(text + "\n")
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))


def test_cached_store_splices_only_the_sessions_a_changed_file_touches():
    # The whole point: a corpus-wide fingerprint made ONE appended transcript throw away
    # the rollup for every other file (measured: 4KB of growth cost a 531MB re-parse on
    # every launch from inside a live session).
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            files = _splice_env(tmp, {"a.jsonl": {"s1": 100}, "b.jsonl": {"s2": 200}})
            args = type("Args", (), {"demo": False, "no_cache": False})()
            cid = "fake|splice"
            a = next(p for p in files if p.endswith("a.jsonl"))

            back = _SpliceBackend(files)
            cold = ot.CachedStore(back, cid, args)
            truth = [(w.id, w.total_tokens) for w in cold.workflows()]
            truth_mb = cold.model_breakdown()
            assert back.full_parses == 1

            files[a] = {"s1": 150}  # the live session grew
            _touch(a, "more")
            back2 = _SpliceBackend(files)
            warm = ot.CachedStore(back2, cid, args)
            rows = warm.workflows()
            assert back2.full_parses == 0  # never re-read the corpus
            assert back2.subset_calls == [[a]]  # ... only the file that changed
            assert warm.served_incrementally is True and warm.served_from_cache is False
            assert [(w.id, w.total_tokens) for w in rows] == [("s2", 200), ("s1", 150)]
            # The breakdown rides on the same splice: asking the backend would parse
            # everything, which is exactly the cost just avoided.
            assert sorted(r["tokens_total"] for r in warm.model_breakdown()) == [150, 200]

            # ... and the splice is what gets cached, so the NEXT launch is a plain hit
            # serving rows identical to a full parse of the same corpus.
            back3 = _SpliceBackend(files)
            again = ot.CachedStore(back3, cid, args)
            assert [(w.id, w.total_tokens) for w in again.workflows()] == [
                ("s2", 200),
                ("s1", 150),
            ]
            assert again.served_from_cache is True and back3.full_parses == 0
            assert truth == [("s2", 200), ("s1", 100)] and len(truth_mb) == 2
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_cached_store_splice_reparses_a_whole_session_not_just_the_changed_file():
    # One file can hold several sessions (a subagent sidecar carries its PARENT's id), so
    # re-reading only the changed file would rebuild a co-tenant session from half its
    # records -- a plausible-looking undercount. The closure runs over the file<->session
    # graph, so a changed sidecar drags in its parent's transcript AND the other sessions
    # that transcript feeds.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            files = _splice_env(
                tmp,
                {
                    "main.jsonl": {"s1": 100, "s2": 10},  # a resumed transcript: two ids
                    "side.jsonl": {"s1": 40},  # s1's subagent sidecar
                    "other.jsonl": {"s3": 7},  # untouched, must stay cached
                },
            )
            args = type("Args", (), {"demo": False, "no_cache": False})()
            cid = "fake|closure"
            side = next(p for p in files if p.endswith("side.jsonl"))
            main = next(p for p in files if p.endswith("main.jsonl"))

            seed = ot.CachedStore(_SpliceBackend(files), cid, args)
            seed.workflows()
            seed.model_breakdown()  # same wrapper: the pair is what gets written

            _touch(side, "more")
            back = _SpliceBackend(files)
            warm = ot.CachedStore(back, cid, args)
            rows = warm.workflows()
            assert warm.served_incrementally is True
            assert back.subset_calls == [[main, side]]  # NOT [side] alone
            assert {w.id for w in rows} == {"s1", "s2", "s3"}
            assert {w.id: w.total_tokens for w in rows}["s1"] == 140
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_cached_store_falls_back_to_a_full_parse_when_the_splice_cannot_be_trusted():
    # Each of these would otherwise serve a number no parse would ever produce, so each
    # one costs the full parse it was trying to avoid. Cheap: measured on a real corpus,
    # a replay-capable transcript is 1 file of 257.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            args = type("Args", (), {"demo": False, "no_cache": False})()

            def seed(name, layout):
                files = _splice_env(os.path.join(tmp, name), layout)
                cid = "fake|" + name
                cold = ot.CachedStore(_SpliceBackend(files), cid, args)
                cold.workflows()
                cold.model_breakdown()  # same wrapper: the pair is what gets written
                return files, cid

            for name in ("refuse", "removed", "newfile", "noprov"):
                os.makedirs(os.path.join(tmp, name))

            # 1. the backend refuses the slice (for Claude: a replay-capable transcript)
            files, cid = seed("refuse", {"a.jsonl": {"s1": 1}, "b.jsonl": {"s2": 2}})
            a = next(p for p in files if p.endswith("a.jsonl"))
            _touch(a, "x")
            back = _SpliceBackend(files)
            back.refuse = {a}
            c = ot.CachedStore(back, cid, args)
            c.workflows()
            assert back.full_parses == 1 and c.served_incrementally is False

            # 2. a file disappeared: its records may have been the first claim on keys a
            #    resumed transcript also holds, which no per-session slice can see.
            files, cid = seed("removed", {"a.jsonl": {"s1": 1}, "b.jsonl": {"s2": 2}})
            gone = next(p for p in files if p.endswith("b.jsonl"))
            os.remove(gone)
            del files[gone]
            back = _SpliceBackend(files)
            ot.CachedStore(back, cid, args).workflows()
            assert back.full_parses == 1

            # 3. a NEW file turns out to feed a session the cache already has rows for.
            #    Its provenance could not be known before parsing it, so the closure
            #    missed that session's other files and the slice is incomplete.
            files, cid = seed("newfile", {"a.jsonl": {"s1": 1}, "b.jsonl": {"s2": 2}})
            extra = os.path.join(tmp, "newfile", "c.jsonl")
            with open(extra, "w") as fh:
                fh.write("c\n")
            files[extra] = {"s1": 5}
            back = _SpliceBackend(files)
            ot.CachedStore(back, cid, args).workflows()
            assert back.full_parses == 1
            assert back.subset_calls == [[extra]]  # it tried, then bailed

            # 4. a cache written before provenance existed: nothing to splice on.
            files, cid = seed("noprov", {"a.jsonl": {"s1": 1}})
            from opentab.stores.cached import cache_dir

            for fname in os.listdir(cache_dir()):
                fpath = os.path.join(cache_dir(), fname)
                blob = json.load(open(fpath))
                if blob.get("source") == "fake":
                    blob["provenance"] = {}
                    json.dump(blob, open(fpath, "w"))
            _touch(next(iter(files)), "x")
            back = _SpliceBackend(files)
            ot.CachedStore(back, cid, args).workflows()
            assert back.full_parses == 1
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_cached_store_splice_refuses_a_file_that_shrank():
    # A transcript that shrank was rewritten, not appended to, and a record that vanished
    # may have been the first claim on a dedup key another session also holds -- which
    # would hand that call to the other session. Rows this splice keeps cannot see that.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            files = _splice_env(tmp, {"a.jsonl": {"s1": 100}, "b.jsonl": {"s2": 200}})
            args = type("Args", (), {"demo": False, "no_cache": False})()
            cid = "fake|shrink"
            cold = ot.CachedStore(_SpliceBackend(files), cid, args)
            cold.workflows()
            cold.model_breakdown()

            a = next(p for p in files if p.endswith("a.jsonl"))
            with open(a, "w") as fh:  # rewrite, shorter than before
                fh.write("")
            st = os.stat(a)
            os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            back = _SpliceBackend(files)
            c = ot.CachedStore(back, cid, args)
            c.workflows()
            assert back.full_parses == 1 and c.served_incrementally is False
            assert back.subset_calls == []  # refused before the backend was asked
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_cached_store_splice_survives_a_corrupt_cache_payload():
    # These paths only run on a MISS, where the old code simply reparsed -- so a cache
    # file someone hand-edited (or a half-migrated one) must fall back to a full parse,
    # never raise out of a launch.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            args = type("Args", (), {"demo": False, "no_cache": False})()
            for i, damage in enumerate(
                (
                    {"fingerprint": [1]},
                    {"workflows": [1]},
                    {"model_breakdown": [1]},
                    {"provenance": {"s1": "not-a-list"}},
                    {"fingerprint": [["a.jsonl", "not-a-size", None]]},
                )
            ):
                sub = os.path.join(tmp, f"c{i}")
                os.makedirs(sub)
                files = _splice_env(sub, {"a.jsonl": {"s1": 1}, "b.jsonl": {"s2": 2}})
                cid = f"fake|corrupt{i}"
                cold = ot.CachedStore(_SpliceBackend(files), cid, args)
                cold.workflows()
                cold.model_breakdown()

                from opentab.stores.cached import cache_dir

                for fname in os.listdir(cache_dir()):
                    fpath = os.path.join(cache_dir(), fname)
                    blob = json.load(open(fpath))
                    if blob.get("source") == "fake" and blob.get("fingerprint"):
                        if any(r[0].startswith(sub) for r in blob["fingerprint"]):
                            blob.update(damage)
                            json.dump(blob, open(fpath, "w"))
                _touch(next(iter(files)), "x")
                back = _SpliceBackend(files)
                c = ot.CachedStore(back, cid, args)
                rows = c.workflows()  # must not raise
                assert {w.id for w in rows} == {"s1", "s2"}
                assert back.full_parses == 1, damage
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_cached_store_rejects_a_cache_whose_rows_are_the_wrong_shape():
    # Row shapes are checked at LOAD, not at each use: the rows are read on the hit path
    # too (Workflow(**row) / dict(row)), so checking only the splice left a hand-edited
    # model_breakdown raising TypeError out of a launch whose fingerprint matched exactly.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            args = type("Args", (), {"demo": False, "no_cache": False})()
            damages = (
                {"workflows": [1]},
                {"model_breakdown": [1]},
                # A model row that IS a dict but carries no root_id passed the
                # is-it-a-dict check and then raised KeyError out of the launch:
                # App._load_model_cache indexes row["root_id"] rather than .get()ing
                # it. On the HIT path, where the fingerprint matched exactly, nothing
                # would ever re-parse to recover -- so the payload has to be rejected
                # here, which is what "one bad row rejects the whole payload" means.
                {"model_breakdown": [{"model_name": "anthropic/x"}]},
                # ...and an unhashable one raises on the dict access instead
                # (RemoteStore refuses a non-string root_id for the same reason).
                {"model_breakdown": [{"root_id": [], "model_name": "anthropic/x"}]},
            )
            for i, damage in enumerate(damages):
                sub = os.path.join(tmp, f"h{i}")
                os.makedirs(sub)
                files = _splice_env(sub, {"a.jsonl": {"s1": 1}})
                cid = f"fake|hit{i}"
                cold = ot.CachedStore(_SpliceBackend(files), cid, args)
                cold.workflows()
                cold.model_breakdown()

                from opentab.stores.cached import cache_dir

                for fname in os.listdir(cache_dir()):
                    fpath = os.path.join(cache_dir(), fname)
                    blob = json.load(open(fpath))
                    if blob.get("fingerprint") and any(
                        r[0].startswith(sub) for r in blob["fingerprint"]
                    ):
                        blob.update(damage)
                        json.dump(blob, open(fpath, "w"))

                # The fingerprint still matches exactly -- this is the HIT path.
                back = _SpliceBackend(files)
                c = ot.CachedStore(back, cid, args)
                assert {w.id for w in c.workflows()} == {"s1"}
                assert [r["root_id"] for r in c.model_breakdown()] == ["s1"]  # no raise
                assert back.full_parses == 1, damage
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_cached_store_incremental_flag_is_per_call_not_sticky():
    # --timings reads this. One wrapper answers workflows() more than once (reload, and
    # --remote builds the local store then profiles it), so a splice followed by a full
    # parse must not still report "incremental" -- a profiler that is confidently wrong
    # about why a launch was slow is worse than one that says nothing.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            files = _splice_env(tmp, {"a.jsonl": {"s1": 1}, "b.jsonl": {"s2": 2}})
            args = type("Args", (), {"demo": False, "no_cache": False})()
            cid = "fake|sticky"
            cold = ot.CachedStore(_SpliceBackend(files), cid, args)
            cold.workflows()
            cold.model_breakdown()

            a = next(p for p in files if p.endswith("a.jsonl"))
            _touch(a, "x")
            back = _SpliceBackend(files)
            c = ot.CachedStore(back, cid, args)
            c.workflows()
            assert c.served_incrementally is True

            # Same wrapper, second call: now the backend refuses, so it is a full parse.
            back.refuse = {a}
            _touch(a, "y")
            c.workflows()
            assert c.served_incrementally is False and back.full_parses == 1
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg
