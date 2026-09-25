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
import sqlite3
import tempfile
from dataclasses import asdict

from opentab import diagnostics as debug
from opentab.accounting.models import Workflow
from opentab.persistence import paths, usage_cache

CACHE_VERSION = 13  # bump when the cached payload shape or meaning changes


# Required because cache readers index these fields directly.
MODEL_ROW_KEYS = frozenset(
    {"root_id", "model_name", "runs", "cost", "tokens_total", "cache_read", "cache_write", "output"}
)


def cache_dir() -> str:
    return os.path.join(paths.cache_dir(), "cache")


def cache_path(cache_id: str) -> str:
    source = cache_id.split("|", 1)[0]
    name = hashlib.sha1(cache_id.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(cache_dir(), f"{source}-{name}.json")


class CachedStore:
    def __init__(self, store, cache_id: str, args: argparse.Namespace):
        self._store = store  # set FIRST so __getattr__ never recurses on a missing attr
        self._args = args
        self._source = cache_id.split("|", 1)[0]
        self._path = cache_path(cache_id)
        self._disk = self._read()  # the on-disk cache, or None
        loader = getattr(store, "set_accounting_cache_loader", None)
        if loader is not None:
            loader(self._accounting_payload)
        self._live_fp: list | None = None  # fingerprint of the current workflows() call
        self._fresh_wf: list | None = None  # asdict rows from the last parse (for the write)
        self._fresh_prov: dict | None = None  # provenance to write beside them
        # A splice must not delegate model_breakdown(), which would parse the full corpus.
        self._fresh_models: list | None = None
        self.served_from_cache: bool | None = None  # set by workflows(); read by --timings
        self.served_incrementally: bool = False  # file splice or numeric-row reuse (--timings)

    def __getattr__(self, name):
        return getattr(self._store, name)

    def _debug(self, event: str, **fields) -> None:
        if debug.enabled():
            debug.event(event, source=self._source, cache=debug.identity(self._path), **fields)

    def _reject(self, layer: str, reason: str):
        self._debug("cache.reject", layer=layer, reason=reason)
        return None

    def _debug_changes(self) -> None:
        if not debug.enabled() or self._disk is None:
            return
        try:
            old = self._fp_map(self._disk.get("fingerprint"))
            new = self._fp_map(self._live_fp)
        except (TypeError, ValueError):
            self._debug("cache.input_changes", reason="invalid_fingerprint")
            return
        changed = sorted(p for p in old.keys() | new.keys() if old.get(p) != new.get(p))
        for path in changed[:20]:
            before = old.get(path, (None, None))[1]
            after = new.get(path, (None, None))[1]
            # SQLite uses a strong [mtime, device, inode] revision; other
            # stores retain their ordinary scalar mtime fingerprint.
            strong = (
                isinstance(before, list)
                and len(before) == 3
                and isinstance(after, list)
                and len(after) == 3
            )
            self._debug(
                "cache.input_changed",
                input=debug.identity(path),
                kind="wal"
                if path.endswith("-wal")
                else "shm"
                if path.endswith("-shm")
                else "db"
                if path.endswith(".db")
                else "file",
                change="added" if path not in old else "removed" if path not in new else "modified",
                size_changed=old.get(path, (None, None))[0] != new.get(path, (None, None))[0],
                mtime_changed=(before[0] != after[0]) if strong else before != after,
                identity_changed=(before[1:] != after[1:]) if strong else None,
            )
        self._debug("cache.input_changes", count=len(changed), omitted=max(0, len(changed) - 20))

    @property
    def records_cost(self) -> bool:
        # Avoid full-corpus probes on fingerprint hits.
        if self._disk is not None and "records_cost" in self._disk:
            fp = self._live_fp if self._live_fp is not None else self._fingerprint()
            if self._disk.get("fingerprint") == fp:
                return bool(self._disk["records_cost"])
        return getattr(self._store, "records_cost", True)

    @debug.timed("cache.fingerprint")
    def _fingerprint(self) -> list:
        fingerprint = getattr(self._store, "cache_fingerprint", None)
        if fingerprint is not None:
            return fingerprint()
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

    @debug.timed("cache.read")
    def _read(self) -> dict | None:
        try:
            with debug.span("cache.disk_read") as info:
                with open(self._path, encoding="utf-8") as fh:
                    text = fh.read()
                info["characters"] = len(text)
            with debug.span("cache.decode"):
                data = json.loads(text)
            del text
        except FileNotFoundError:
            return self._reject("disk", "missing")
        except OSError:
            return self._reject("disk", "unreadable")
        except ValueError:
            return self._reject("disk", "invalid_json")
        return self._validate(data)

    @debug.timed("cache.validate")
    def _validate(self, data) -> dict | None:
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return self._reject("disk", "version_or_shape")
        if not isinstance(data.get("workflows"), list) or not isinstance(
            data.get("model_breakdown"), list
        ):
            return self._reject("disk", "rollup_shape")
        # Reject the entire cache on shape drift; skipping a row would lose usage silently.
        if not all(isinstance(row, dict) for row in data["workflows"]):
            return self._reject("disk", "workflow_shape")
        # Validate direct-index fields before the hit path loses its reparse fallback.
        if not all(
            isinstance(row, dict) and MODEL_ROW_KEYS <= row.keys()
            for row in data["model_breakdown"]
        ):
            return self._reject("disk", "model_shape")
        # root_id is used as a mapping key.
        if not all(isinstance(row["root_id"], str) for row in data["model_breakdown"]):
            return self._reject("disk", "model_identity_shape")
        if not isinstance(data.get("provenance"), dict):
            data["provenance"] = {}  # readable, just not spliceable: full parse on a miss
        self._debug(
            "cache.loaded", workflows=len(data["workflows"]), models=len(data["model_breakdown"])
        )
        return data

    @debug.timed("cache.write")
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
        getter = getattr(self._store, "accounting_cache", None)
        try:
            os.makedirs(cache_dir(), exist_ok=True)
            if getter is not None:
                accounting = getter()
                if accounting is not None:
                    # Independent revision-checked scalar snapshot. Rollup hits need
                    # not load it; an older/newer sidecar is safe because every use
                    # verifies source identity, row revisions and the build cutoff.
                    path = self._path + ".usage.sqlite3"
                    if getattr(self._store, "accounting_cache_changed", True) or not os.path.isfile(
                        path
                    ):
                        with debug.span("cache.accounting_write", source=self._source):
                            usage_cache.write(path, accounting)
                    else:
                        self._debug(
                            "cache.accounting_write_skipped", reason="all_native_rows_reused"
                        )
                    payload["accounting_external"] = "sqlite-v1"
            self._write_json(self._path, payload)
            self._disk = payload
            self._fresh_wf = None
            self._fresh_models = None
            self._fresh_prov = None
            self._debug("cache.written", workflows=len(workflows), models=len(model_breakdown))
        except (OSError, sqlite3.Error) as exc:
            self._debug("cache.write_failed", error_type=type(exc).__name__)

    @staticmethod
    def _write_json(path: str, payload) -> None:
        # dumps uses the C encoder. dump's Python generator/write per scalar made
        # an unchanged 55k-row accounting snapshot cost over a second on WSL.
        with debug.span("cache.encode") as info:
            text = json.dumps(payload, separators=(",", ":"))
            info["characters"] = len(text)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                with debug.span("cache.disk_write"):
                    fh.write(text)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    @debug.timed("cache.accounting_read")
    def _accounting_payload(self, scope=None):
        disk = self._disk or {}
        self._debug(
            "cache.accounting_source",
            storage="sqlite"
            if disk.get("accounting_external") == "sqlite-v1"
            else "json"
            if disk.get("accounting_external")
            else "inline_or_absent",
            scope="all" if scope is None else "subtree",
        )
        if not disk.get("accounting_external"):
            return disk.get("accounting")  # migrate existing inline scalar caches lazily
        try:
            if disk.get("accounting_external") == "sqlite-v1":
                return usage_cache.read(self._path + ".usage.sqlite3", scope)
            with open(self._path + ".usage.json", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self._debug(
                "cache.reject",
                layer="accounting",
                reason="missing_or_invalid_sidecar",
                error_type=type(exc).__name__,
            )
            return None

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
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            self._debug(
                "cache.reject",
                layer="splice",
                reason="invalid_payload",
                error_type=type(exc).__name__,
            )
            return None

    @debug.timed("cache.splice")
    def _splice(self, live_fp: list):
        """Return a proven-safe incremental rollup, or ``None`` for a full parse."""
        disk = self._disk
        if disk is None:
            return self._reject("splice", "no_disk_cache")
        prov = disk.get("provenance") or {}  # session id -> files that produced its rows
        subset = getattr(self._store, "parse_subset", None)
        sorter = getattr(self._store, "sort_workflows", None)
        if not prov or subset is None or sorter is None:
            return self._reject("splice", "no_provenance_or_hooks")

        old_raw = disk.get("fingerprint") or []
        old_fp, new_fp = self._fp_map(old_raw), self._fp_map(live_fp)
        if len(old_fp) != len(old_raw):
            # An unreadable fingerprint may hide a removed file and stale session rows.
            return self._reject("splice", "invalid_fingerprint")
        if set(old_fp) - set(new_fp):
            # Removal can transfer dedup ownership to a session outside the slice.
            return self._reject("splice", "source_removed")
        changed = {path for path, stamp in new_fp.items() if old_fp.get(path) != stamp}
        if not changed:
            return self._reject("splice", "no_identified_changes")
        # Shrinkage can transfer dedup ownership outside the slice. A same-size-or-larger
        # rewrite remains undetectable without caching dedup keys, which would roughly
        # double cache size and burden every hit; transcripts are append-only in practice.
        if any(
            path in old_fp and (new_fp[path][0] or 0) < (old_fp[path][0] or 0) for path in changed
        ):
            return self._reject("splice", "source_shrank")

        # Files and sessions are many-to-many through sidecars and resumed replays; reparse
        # the complete connected component, never only the changed file.
        if not all(
            isinstance(files, list) and all(isinstance(f, str) for f in files)
            for files in prov.values()
        ):
            # An incomplete provenance graph could preserve stale rows indefinitely.
            return self._reject("splice", "invalid_provenance")
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

        self._debug(
            "cache.splice_scope",
            changed_files=len(changed),
            affected_sessions=len(affected),
            read_files=len(seen_files & set(new_fp)),
        )
        with debug.span("cache.parse_subset", source=self._source):
            sliced = subset(sorted(seen_files & set(new_fp)))
        if sliced is None:
            return self._reject("splice", "backend_refused")
        fresh_wf, fresh_models, fresh_prov = sliced

        # A new file feeding an existing session makes the provenance closure incomplete.
        cached_ids = {row.get("id") for row in disk["workflows"]}  # rows shape-checked by _read
        if ({w.id for w in fresh_wf} - affected) & cached_ids:
            return self._reject("splice", "ownership_overlap")

        drop = affected | {w.id for w in fresh_wf}
        try:
            kept = [Workflow(**row) for row in disk["workflows"] if row.get("id") not in drop]
        except TypeError:
            return self._reject("splice", "workflow_fields_changed")
        kept_models = [
            dict(row) for row in disk["model_breakdown"] if row.get("root_id") not in drop
        ]
        rows = sorter(kept + list(fresh_wf))
        provenance = {sid: paths for sid, paths in prov.items() if sid not in drop}
        provenance.update(fresh_prov)
        return rows, [asdict(w) for w in rows], kept_models + list(fresh_models), provenance

    @debug.timed("cache.workflows")
    def workflows(self) -> list:
        # Reload must observe changes, so fingerprint every call.
        live_fp = self._fingerprint()
        if self._fresh_wf is not None and self._live_fp == live_fp:
            self.served_from_cache = False
            self.served_incrementally = False
            self._debug("cache.decision", result="memory_hit", workflows=len(self._fresh_wf))
            return [Workflow(**row) for row in self._fresh_wf]
        self._live_fp = live_fp
        # Timing state describes this call, not the wrapper's history.
        self.served_incrementally = False
        if self._disk is not None and self._disk.get("fingerprint") == self._live_fp:
            try:
                rows = [Workflow(**row) for row in self._disk["workflows"]]
            except TypeError:
                self._reject("disk", "workflow_fields_changed")
                self._disk = None  # cached fields drifted from the dataclass: reparse
            else:
                # These stashes are one answer and must clear together on a hit.
                self._fresh_wf = None
                self._fresh_models = None
                self._fresh_prov = None
                self.served_from_cache = True
                self._debug(
                    "cache.decision",
                    result="hit",
                    workflows=len(rows),
                    accounting_restore="deferred_until_needed",
                )
                return rows
        # A miss. Before re-reading everything, try to re-read only what changed.
        self._debug(
            "cache.decision",
            result="miss",
            reason="fingerprint_changed" if self._disk else "no_usable_cache",
        )
        self._debug_changes()
        spliced = self._incremental(self._live_fp)
        if spliced is not None:
            rows, self._fresh_wf, self._fresh_models, self._fresh_prov = spliced
            self.served_from_cache = False
            self.served_incrementally = True
            self._debug("cache.decision", result="incremental_splice", workflows=len(rows))
            return rows
        restore = getattr(self._store, "restore_accounting_cache", None)
        if restore is not None:
            with debug.span("cache.restore_accounting", source=self._source):
                restore(self._accounting_payload())
        with debug.span("cache.backend_workflows", source=self._source):
            workflows = self._store.workflows()  # miss: real parse
        self.served_incrementally = bool(getattr(self._store, "accounting_cache_reused", False))
        self._fresh_wf = [asdict(w) for w in workflows]
        self._fresh_models = None
        self._fresh_prov = None
        self.served_from_cache = False
        self._debug(
            "cache.decision",
            result="incremental_accounting" if self.served_incrementally else "parsed",
            workflows=len(workflows),
        )
        return workflows

    @debug.timed("cache.models")
    def model_breakdown(self) -> list:
        fp = self._live_fp if self._live_fp is not None else self._fingerprint()
        if (
            self._fresh_wf is None
            and self._disk is not None
            and self._disk.get("fingerprint") == fp
        ):
            self._debug("cache.models_decision", result="hit")
            return [dict(row) for row in self._disk["model_breakdown"]]
        if self._fresh_models is not None:
            # An incremental splice already built these from the files it re-read.
            # Calling through would parse the whole corpus -- what the splice avoided.
            rows = [dict(row) for row in self._fresh_models]
            self._debug("cache.models_decision", result="splice_memo")
        else:
            self._debug("cache.models_decision", result="backend")
            with debug.span("cache.backend_models", source=self._source):
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

    def persist_accounting(self) -> None:
        """Complete an explicitly requested headless catalog refresh.

        This may perform the deferred model scan for a cold OpenCode store; the
        normal TUI path never calls it before the first paint. Refuse to write a
        stale fingerprint.
        """
        if self._fresh_wf is None or self._live_fp != self._fingerprint():
            return
        self.model_breakdown()
