"""CachedStore: a warm-start rollup cache around any backend.

Parsing every transcript (or scanning the whole message table) on each launch is the
dominant startup cost. But between two launches the data usually hasn't changed, so the
result is recomputable from a cached rollup. CachedStore wraps a backend and, when the
backend's input files are byte-for-byte the same as last time (a (path, size, mtime)
fingerprint), returns the cached workflows()/model_breakdown() output WITHOUT parsing --
the 0.8s -> ~50ms warm start. Any change (a file added, edited, or removed) misses and
falls through to a normal parse, then rewrites the cache, so a stale rollup is never
shown; mtime is nanosecond-grained, so an in-place edit reliably invalidates.

A whole-corpus fingerprint is all-or-nothing, though, and that is exactly wrong for the
way opentab is usually launched: from inside a live agent session, whose transcript has
just grown by a few KB. Measured on a 257-file / 531MB Claude corpus, ONE changed file
(the session `--goto` was pointed at) discarded the rollup for all 531MB and cost 1823ms
on every launch. So a miss first tries an INCREMENTAL splice: the cache remembers which
files produced each session (`provenance`), so it can re-parse only the sessions a
changed file touches and keep the cached rows for the rest -- ~1823ms -> ~100ms. It needs
two opt-ins from the backend (cache_provenance/parse_subset); a backend without them, or
any case the splice cannot prove safe, falls back to the full parse below. Every row served
is byte-identical to what a full parse would produce, with ONE acknowledged exception,
spelled out at the shrink guard: a rewrite that drops a record while keeping the file the
same size or larger can move a dedup claim invisibly. Everything else -- a removed file, a
shorter rewrite, a replay-capable transcript, a new file feeding a cached session, a file
that vanished mid-read -- pays the full parse.

Only workflows(), model_breakdown() and records_cost are intercepted -- they feed the
first frame (records_cost because some backends can only answer it by reading their
whole corpus). Everything else (workflow_nodes, tool_breakdown, message_timeline,
supports_*, summary, demo, demo_scale, ...) delegates straight to the wrapped store,
which parses lazily the first time you actually drill into a session. So a warm start
paints instantly and only pays the parse if and when you open a session's detail.

The cache is disabled under --demo (demo never persists, and its per-process scale must
not be baked in) and --no-cache; sources.make_store applies the wrapper.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict

from opentab import paths
from opentab.models import Workflow

CACHE_VERSION = 9  # bump when the cached payload shape or meaning changes
# 9: the payload carries `provenance` (session id -> the files that produced its rows),
#    which the incremental splice needs. A v8 file has no way to say which sessions a
#    changed file touches, so it would have to full-parse anyway.
# 8: Codex model rows split GPT-5.6 cache-write tokens out of inclusive input. An
#    unchanged rollout corpus must discard the old all-input/zero-write rollups.
# 7: worked_seconds starts a new burst after 30 minutes of silence, and Claude control
#    records mark additional idle boundaries. Unchanged corpora must discard old spans.
# 6: ClaudeStore credits a replayed API call to the session that made it, not to the
#    background session that replayed it (ordering in ClaudeStore._parse). Same shape,
#    different attribution -- so without a bump an unchanged corpus would keep serving
#    the old rollup, and the session list would disagree with the session detail.
# 5: model rows carry the 1h-TTL cache-write subset. Without this bump an unchanged
#    corpus keeps serving rows that lack the field, so the long-TTL pricing fix would
#    silently not apply to exactly the machines that had been running opentab longest.


# The keys a cached model row is INDEXED by rather than .get()-ed, so a payload missing
# one raises KeyError out of a launch instead of degrading. Kept here beside the guard
# that enforces it: App._load_model_cache indexes root_id, App._compute_api_costs cost
# and model_name (note `m.get("real_cost", m["cost"])` evaluates m["cost"] eagerly), and
# Renderer._mix_rows the rest. A backend that stops emitting one of these has changed the
# payload's shape, which is what CACHE_VERSION is for.
MODEL_ROW_KEYS = frozenset(
    {"root_id", "model_name", "runs", "cost", "tokens_total", "cache_read", "cache_write", "output"}
)


def cache_dir() -> str:
    # The warm-start rollups get their own subdir of the XDG cache dir, kept apart from
    # the sibling prices.json / remotes/ so a CACHE_VERSION wipe touches only these.
    return os.path.join(paths.cache_dir(), "cache")


class CachedStore:
    def __init__(self, store, cache_id: str, args: argparse.Namespace):
        self._store = store  # set FIRST so __getattr__ never recurses on a missing attr
        self._args = args
        self._source = cache_id.split("|", 1)[0]
        name = hashlib.sha1(cache_id.encode("utf-8", "replace")).hexdigest()[:16]
        self._path = os.path.join(cache_dir(), f"{self._source}-{name}.json")
        self._disk = self._read()  # the on-disk cache, or None
        self._live_fp: list | None = None  # fingerprint of the current workflows() call
        self._fresh_wf: list | None = None  # asdict rows from the last parse (for the write)
        self._fresh_prov: dict | None = None  # provenance to write beside them
        # Model rows the incremental splice already built. model_breakdown() MUST read
        # them instead of calling through: the wrapped store parsed a handful of files,
        # so asking it for the breakdown would parse the whole corpus -- the exact cost
        # the splice just avoided.
        self._fresh_models: list | None = None
        self.served_from_cache: bool | None = None  # set by workflows(); read by --timings
        self.served_incrementally: bool = False  # a splice, not a full parse (--timings)

    # Anything not intercepted below is the wrapped store's -- workflow_nodes, the Turns/
    # Tools extras, supports_*, demo, source_name, summary, and so on.
    def __getattr__(self, name):
        return getattr(self._store, name)

    @property
    def records_cost(self) -> bool:
        # Served from the cache on a fingerprint hit: some backends (pi/OpenClaw/CSV/
        # JSONL) can only answer this by reading their whole corpus, which would defeat
        # the warm start. A miss (or a pre-v3 cache) delegates like __getattr__ does.
        if self._disk is not None and "records_cost" in self._disk:
            fp = self._live_fp if self._live_fp is not None else self._fingerprint()
            if self._disk.get("fingerprint") == fp:
                return bool(self._disk["records_cost"])
        return getattr(self._store, "records_cost", True)

    # --- fingerprint / cache file -------------------------------------------------
    def _fingerprint(self) -> list:
        # Sorted [path, size, mtime_ns] over the backend's inputs. Lists (not tuples) so
        # it compares equal to the JSON-decoded fingerprint from disk. stat() reads only
        # metadata (no open()), so it stays cheap even where opening files is taxed.
        out = []
        for path in self._store.cache_inputs():
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append([path, st.st_size, st.st_mtime_ns])
        out.sort()
        return out

    def _read(self) -> dict | None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return None
        if not isinstance(data.get("workflows"), list) or not isinstance(
            data.get("model_breakdown"), list
        ):
            return None
        # Row shapes are checked HERE, not at each use: the rows are read on the hit path
        # (Workflow(**row), dict(row)) as well as by the splice, and a check at only one
        # of them leaves the other raising out of a launch on a hand-edited file. One
        # bad row rejects the whole payload -- skipping it would silently drop a
        # session's tokens, which is worse than reparsing.
        if not all(isinstance(row, dict) for row in data["workflows"]):
            return None
        # A model row must also CARRY every key its readers index rather than .get()
        # (MODEL_ROW_KEYS): a row that is a dict but is missing one passes "is it a dict"
        # and then raises KeyError out of the launch -- on the HIT path, where the
        # fingerprint matched exactly and nothing will ever re-parse to recover. The
        # splice is safe either way (it reads row.get), so this is the hit path's guard,
        # which is precisely the one with nowhere to fall back to.
        if not all(
            isinstance(row, dict) and MODEL_ROW_KEYS <= row.keys()
            for row in data["model_breakdown"]
        ):
            return None
        # ...and root_id must be a STRING for the same reason RemoteStore insists on one:
        # it is used as a dict key, so an unhashable value (a crafted `[]`) raises on the
        # access rather than on the membership test.
        if not all(isinstance(row["root_id"], str) for row in data["model_breakdown"]):
            return None
        if not isinstance(data.get("provenance"), dict):
            data["provenance"] = {}  # readable, just not spliceable: full parse on a miss
        return data

    def _write(
        self,
        fingerprint: list,
        workflows: list,
        model_breakdown: list,
        provenance: dict | None = None,
    ) -> None:
        # Best-effort and atomic (temp + replace): a cache we cannot write must never
        # break a launch, and a half-written file must never be read back as valid.
        payload = {
            "version": CACHE_VERSION,
            "source": self._source,
            "fingerprint": fingerprint,
            # Cheap here: the backend just parsed, so a lazy records_cost derives from
            # that parse instead of running its full-corpus probe.
            "records_cost": bool(getattr(self._store, "records_cost", True)),
            "workflows": workflows,
            "model_breakdown": model_breakdown,
            # session id -> the files that produced its rows. Empty for a backend with
            # no cache_provenance(), which simply means the next miss is a full parse.
            "provenance": provenance or {},
        }
        try:
            os.makedirs(cache_dir(), exist_ok=True)
            tmp = f"{self._path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self._path)
            self._disk = payload
        except OSError:
            pass

    # --- incremental splice ---------------------------------------------------------
    @staticmethod
    def _fp_map(fingerprint) -> dict:
        # Shape-checked per row: _read only validates that "fingerprint" is a list, and
        # this now runs on a MISS, where the old code path just reparsed. A cache file
        # someone hand-edited to [1] would otherwise raise TypeError out of a launch.
        out = {}
        for row in fingerprint or []:
            if isinstance(row, (list, tuple)) and len(row) == 3 and isinstance(row[0], str):
                out[row[0]] = (row[1], row[2])
        return out

    def _incremental(self, live_fp: list):
        # Any surprise in the cached payload means the splice cannot be trusted, and the
        # answer to that is always the same: parse everything. Cheaper as one guard than
        # as a shape check per access, and it can't be forgotten when a field is added.
        try:
            return self._splice(live_fp)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return None

    def _splice(self, live_fp: list):
        """Re-parse only what a changed file touches; keep the cached rows for the rest.

        Returns (workflows, asdict rows, model rows, provenance), or None to mean "this
        needs a full parse" -- which every uncertain case answers, because a wrong splice
        would show a plausible number that no parse would ever produce.
        """
        disk = self._disk
        if disk is None:
            return None
        prov = disk.get("provenance") or {}  # session id -> files that produced its rows
        subset = getattr(self._store, "parse_subset", None)
        sorter = getattr(self._store, "sort_workflows", None)
        if not prov or subset is None or sorter is None:
            return None

        old_raw = disk.get("fingerprint") or []
        old_fp, new_fp = self._fp_map(old_raw), self._fp_map(live_fp)
        if len(old_fp) != len(old_raw):
            # Rows this couldn't read are rows whose files it cannot compare -- and an
            # unreadable entry is indistinguishable from a file that was REMOVED, which
            # would leave a deleted session's rows in the splice forever.
            return None
        if set(old_fp) - set(new_fp):
            # A file DISAPPEARED. Its records may have been the first claim on keys that
            # a resumed/forked transcript also holds, so removing it can hand work to a
            # session that had none -- a change no per-session slice can see. Rare
            # (a rotated or deleted transcript), so pay the full parse.
            return None
        changed = {path for path, stamp in new_fp.items() if old_fp.get(path) != stamp}
        if not changed:
            return None  # fingerprints differ but no file does: nothing to splice from
        # A transcript that SHRANK was rewritten, not appended to -- and a record that
        # disappeared may have been the first claim on a key some other session also
        # holds, which would hand that call to the other session. Cached rows for
        # sessions this splice does not re-read cannot see that. Transcripts are
        # append-only in practice, so this costs nothing.
        #
        # It does NOT close the case completely: a rewrite that drops a record while
        # keeping the file the same size or larger still moves the claim invisibly.
        # Closing that needs per-session dedup-key sets in the cache, which would roughly
        # double the cache file and tax the HIT path -- deliberately not paid, since it
        # takes a hand-edited transcript to get there and the whole point of this path is
        # that the file grew by a few KB.
        if any(
            path in old_fp and (new_fp[path][0] or 0) < (old_fp[path][0] or 0) for path in changed
        ):
            return None

        # One file can hold several sessions (a subagent sidecar carries its PARENT's
        # id; a resumed transcript replays records under their original ids), so
        # re-parsing "the changed file" alone would rebuild a co-tenant session from
        # half its files. Close over the file <-> session graph and re-read whole
        # components. In practice a component is one transcript plus its sidecars.
        if not all(
            isinstance(files, list) and all(isinstance(f, str) for f in files)
            for files in prov.values()
        ):
            # Skipping the unreadable entry instead would drop that session out of the
            # graph, so nothing could ever mark it affected and its stale rows would be
            # kept forever -- the one outcome this whole path exists to prevent.
            return None
        files_of = {sid: set(files) for sid, files in prov.items()}
        sessions_of: dict[str, set] = {}
        for sid, files in files_of.items():
            for path in files:
                sessions_of.setdefault(path, set()).add(sid)
        affected: set = set()
        frontier, seen_files = set(changed), set()
        while frontier:
            path = frontier.pop()
            if path in seen_files:
                continue
            seen_files.add(path)
            for sid in sessions_of.get(path, ()):
                if sid in affected:
                    continue
                affected.add(sid)
                frontier |= files_of[sid] - seen_files

        sliced = subset(sorted(seen_files & set(new_fp)))
        if sliced is None:
            return None  # the backend refused (for Claude: a replay-capable transcript)
        fresh_wf, fresh_models, fresh_prov = sliced

        # A file that is NEW carries no provenance, so the closure above could not know
        # which sessions it feeds. If it turns out to feed a session the cache already
        # holds rows for, those rows were built from files this slice did not read --
        # so the slice is incomplete and the whole corpus has to be parsed. (A session
        # that is simply new to the cache is exactly what a slice is for.)
        cached_ids = {row.get("id") for row in disk["workflows"]}  # rows shape-checked by _read
        if ({w.id for w in fresh_wf} - affected) & cached_ids:
            return None

        drop = affected | {w.id for w in fresh_wf}
        try:
            kept = [Workflow(**row) for row in disk["workflows"] if row.get("id") not in drop]
        except TypeError:
            return None  # cached fields drifted from the dataclass: reparse
        kept_models = [
            dict(row) for row in disk["model_breakdown"] if row.get("root_id") not in drop
        ]
        rows = sorter(kept + list(fresh_wf))
        provenance = {sid: paths for sid, paths in prov.items() if sid not in drop}
        provenance.update(fresh_prov)
        return rows, [asdict(w) for w in rows], kept_models + list(fresh_models), provenance

    # --- intercepted methods ------------------------------------------------------
    def workflows(self) -> list:
        # Re-fingerprint every call so reload (r) after an edit re-parses; an unchanged
        # fingerprint (the common warm start / no-op reload) serves the cache untouched.
        self._live_fp = self._fingerprint()
        # Reset per call, not sticky: one wrapper can answer workflows() more than once
        # (reload, and --remote builds the local store then profiles it), and a splice
        # followed by a full-parse fallback would otherwise still report "incremental".
        self.served_incrementally = False
        if self._disk is not None and self._disk.get("fingerprint") == self._live_fp:
            try:
                rows = [Workflow(**row) for row in self._disk["workflows"]]
            except TypeError:
                self._disk = None  # cached fields drifted from the dataclass: reparse
            else:
                # A hit: nothing new to write. All three stashes clear together -- they
                # are one answer split across three fields, and a survivor could only
                # ever be written beside rows it did not come from.
                self._fresh_wf = None
                self._fresh_models = None
                self._fresh_prov = None
                self.served_from_cache = True
                return rows
        # A miss. Before re-reading everything, try to re-read only what changed.
        spliced = self._incremental(self._live_fp)
        if spliced is not None:
            rows, self._fresh_wf, self._fresh_models, self._fresh_prov = spliced
            self.served_from_cache = False
            self.served_incrementally = True
            return rows
        workflows = self._store.workflows()  # miss: real parse
        self._fresh_wf = [asdict(w) for w in workflows]
        self._fresh_models = None
        self._fresh_prov = None
        self.served_from_cache = False
        return workflows

    def model_breakdown(self) -> list:
        fp = self._live_fp if self._live_fp is not None else self._fingerprint()
        if (
            self._fresh_wf is None
            and self._disk is not None
            and self._disk.get("fingerprint") == fp
        ):
            return [dict(row) for row in self._disk["model_breakdown"]]
        if self._fresh_models is not None:
            # An incremental splice already built these from the files it re-read.
            # Calling through would parse the whole corpus -- what the splice avoided.
            rows = [dict(row) for row in self._fresh_models]
        else:
            rows = [dict(row) for row in self._store.model_breakdown()]
        # Write only when the workflows AND the breakdown were both parsed fresh under
        # this same fingerprint -- a complete, self-consistent cache.
        if self._fresh_wf is not None:
            prov = self._fresh_prov
            if prov is None:
                getter = getattr(self._store, "cache_provenance", None)
                prov = getter() if getter is not None else None
            self._write(fp, self._fresh_wf, rows, prov)
        return rows
