"""CachedStore: a warm-start rollup cache around any backend.

Parsing every transcript (or scanning the whole message table) on each launch is the
dominant startup cost. But between two launches the data usually hasn't changed, so the
result is recomputable from a cached rollup. CachedStore wraps a backend and, when the
backend's input files are byte-for-byte the same as last time (a (path, size, mtime)
fingerprint), returns the cached workflows()/model_breakdown() output WITHOUT parsing --
the 0.8s -> ~50ms warm start. Any change (a file added, edited, or removed) misses and
falls through to a normal parse, then rewrites the cache, so a stale rollup is never
shown; mtime is nanosecond-grained, so an in-place edit reliably invalidates.

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

from opentab.models import Workflow

CACHE_VERSION = 3  # bump when the cached payload shape changes (invalidates old files)


def cache_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "opentab", "cache")


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
        self.served_from_cache: bool | None = None  # set by workflows(); read by --timings

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
        return data

    def _write(self, fingerprint: list, workflows: list, model_breakdown: list) -> None:
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

    # --- intercepted methods ------------------------------------------------------
    def workflows(self) -> list:
        # Re-fingerprint every call so reload (r) after an edit re-parses; an unchanged
        # fingerprint (the common warm start / no-op reload) serves the cache untouched.
        self._live_fp = self._fingerprint()
        if self._disk is not None and self._disk.get("fingerprint") == self._live_fp:
            try:
                rows = [Workflow(**row) for row in self._disk["workflows"]]
            except TypeError:
                self._disk = None  # cached fields drifted from the dataclass: reparse
            else:
                self._fresh_wf = None  # a hit: nothing new to write
                self.served_from_cache = True
                return rows
        workflows = self._store.workflows()  # miss: real parse
        self._fresh_wf = [asdict(w) for w in workflows]
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
        rows = [dict(row) for row in self._store.model_breakdown()]
        # Write only when the workflows AND the breakdown were both parsed fresh under
        # this same fingerprint -- a complete, self-consistent cache.
        if self._fresh_wf is not None:
            self._write(fp, self._fresh_wf, rows)
        return rows
