"""Warm-start cache for backend rollups.

Fingerprint hits serve workflows, model rows, and recorded-cost state without parsing.
On a miss, backends with provenance support reparse a proven-safe changed component;
otherwise they fall back to a full parse. Session-detail methods always delegate.
The cache is disabled for demo mode and ``--no-cache``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict

from opentab import paths
from opentab.models import Workflow

CACHE_VERSION = 12  # bump when the cached payload shape or meaning changes


# Required because cache readers index these fields directly.
MODEL_ROW_KEYS = frozenset(
    {"root_id", "model_name", "runs", "cost", "tokens_total", "cache_read", "cache_write", "output"}
)


def cache_dir() -> str:
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
        # A splice must not delegate model_breakdown(), which would parse the full corpus.
        self._fresh_models: list | None = None
        self.served_from_cache: bool | None = None  # set by workflows(); read by --timings
        self.served_incrementally: bool = False  # a splice, not a full parse (--timings)

    def __getattr__(self, name):
        return getattr(self._store, name)

    @property
    def records_cost(self) -> bool:
        # Avoid full-corpus probes on fingerprint hits.
        if self._disk is not None and "records_cost" in self._disk:
            fp = self._live_fp if self._live_fp is not None else self._fingerprint()
            if self._disk.get("fingerprint") == fp:
                return bool(self._disk["records_cost"])
        return getattr(self._store, "records_cost", True)

    def _fingerprint(self) -> list:
        # Lists compare directly with the JSON-decoded [path, size, mtime_ns] rows.
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
        # Reject the entire cache on shape drift; skipping a row would lose usage silently.
        if not all(isinstance(row, dict) for row in data["workflows"]):
            return None
        # Validate direct-index fields before the hit path loses its reparse fallback.
        if not all(
            isinstance(row, dict) and MODEL_ROW_KEYS <= row.keys()
            for row in data["model_breakdown"]
        ):
            return None
        # root_id is used as a mapping key.
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
        # Cache writes are best-effort and atomic; failure must not block launch.
        payload = {
            "version": CACHE_VERSION,
            "source": self._source,
            "fingerprint": fingerprint,
            # The backend just parsed, so lazy cost state is already available.
            "records_cost": bool(getattr(self._store, "records_cost", True)),
            "workflows": workflows,
            "model_breakdown": model_breakdown,
            # Empty provenance disables incremental misses for this backend.
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

    @staticmethod
    def _fp_map(fingerprint) -> dict:
        # Malformed fingerprint rows invalidate the splice instead of the launch.
        out = {}
        for row in fingerprint or []:
            if isinstance(row, (list, tuple)) and len(row) == 3 and isinstance(row[0], str):
                out[row[0]] = (row[1], row[2])
        return out

    def _incremental(self, live_fp: list):
        # Any payload surprise falls back to a full parse.
        try:
            return self._splice(live_fp)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return None

    def _splice(self, live_fp: list):
        """Return a proven-safe incremental rollup, or ``None`` for a full parse."""
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
            # An unreadable fingerprint may hide a removed file and stale session rows.
            return None
        if set(old_fp) - set(new_fp):
            # Removal can transfer dedup ownership to a session outside the slice.
            return None
        changed = {path for path, stamp in new_fp.items() if old_fp.get(path) != stamp}
        if not changed:
            return None  # fingerprints differ but no file does: nothing to splice from
        # Shrinkage can transfer dedup ownership outside the slice. A same-size-or-larger
        # rewrite remains undetectable without caching dedup keys, which would roughly
        # double cache size and burden every hit; transcripts are append-only in practice.
        if any(
            path in old_fp and (new_fp[path][0] or 0) < (old_fp[path][0] or 0) for path in changed
        ):
            return None

        # Files and sessions are many-to-many through sidecars and resumed replays; reparse
        # the complete connected component, never only the changed file.
        if not all(
            isinstance(files, list) and all(isinstance(f, str) for f in files)
            for files in prov.values()
        ):
            # An incomplete provenance graph could preserve stale rows indefinitely.
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

        # A new file feeding an existing session makes the provenance closure incomplete.
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

    def workflows(self) -> list:
        # Reload must observe changes, so fingerprint every call.
        self._live_fp = self._fingerprint()
        # Timing state describes this call, not the wrapper's history.
        self.served_incrementally = False
        if self._disk is not None and self._disk.get("fingerprint") == self._live_fp:
            try:
                rows = [Workflow(**row) for row in self._disk["workflows"]]
            except TypeError:
                self._disk = None  # cached fields drifted from the dataclass: reparse
            else:
                # These stashes are one answer and must clear together on a hit.
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
