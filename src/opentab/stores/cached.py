"""Warm-start cache for backend rollups.

Fingerprint hits serve workflows, model rows, and recorded-cost state without parsing.
On a miss, backends with provenance support reparse a proven-safe changed component;
otherwise they fall back to a full parse. Session-detail methods always delegate.
The cache is disabled for demo mode. Under ``--no-cache`` the wrapper is a plain
delegate that persists nothing; it only adopts a private handoff file a loader
process wrote for this run (see ``stores/loader.py``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict

from opentab import __version__
from opentab import diagnostics as debug
from opentab.accounting.models import Workflow
from opentab.accounting.tiers import valid_pricing
from opentab.persistence import paths, usage_cache

CACHE_VERSION = 23  # model rows moved to a lazily read sidecar
# Beside each cache: the digest this release wrote it with. A match proves the rows
# are the ones its own parse produced, so the per-row pricing check is skipped.
TRUST_SUFFIX = ".trust"
# The model rows (~97% of the decode time) live beside the cache and are decoded only
# when model_breakdown() runs, which the TUI defers until after its first paint. The
# sidecar's first line is its rows' SHA-1, which the cache pins. The read checks that
# line and keeps the file open, so a sidecar replaced later by another process can
# never hand this run rows of a different generation than the workflows it served.
MODELS_SUFFIX = ".models"


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
    def __init__(
        self,
        store,
        cache_id: str,
        args: argparse.Namespace,
        persistent: bool = True,
        path: str | None = None,
    ):
        self._store = store  # set FIRST so __getattr__ never recurses on a missing attr
        self._args = args
        self._cache_id = cache_id
        self._source = cache_id.split("|", 1)[0]
        # --no-cache: no path, so a plain delegate until load_from_disk() adopts a handoff.
        self.persistent = persistent
        self._path = path or (cache_path(cache_id) if persistent else None)
        self._disk = self._read() if self._path else None  # the on-disk cache, or None
        if self._path:
            self._install_accounting_loader()
        self._live_fp: list | None = None  # fingerprint of the current workflows() call
        self._fresh_wf: list | None = None  # asdict rows from the last parse (for the write)
        self._fresh_prov: dict | None = None  # provenance to write beside them
        # A splice must not delegate model_breakdown(), which would parse the full corpus.
        self._fresh_models: list | None = None
        self.served_from_cache: bool | None = None  # set by workflows(); read by --timings
        self.served_incrementally: bool = False  # file splice or numeric-row reuse (--timings)
        # A loader process parsed this backend and the rows came back through its cache.
        self.served_by_process: bool = False
        self._plan_memo: tuple | None = None  # (fingerprint, splice plan) from full_parse_bytes
        self.rebuild: tuple | None = None  # (builder, args, key) for a loader process

    def __getattr__(self, name):
        return getattr(self._store, name)

    @staticmethod
    def _trust_stamp(text: str) -> dict:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        return {"version": CACHE_VERSION, "opentab": __version__, "sha1": digest}

    def _trusted(self, text: str) -> bool:
        try:
            with open(self._path + TRUST_SUFFIX, encoding="utf-8") as fh:
                stamp = json.load(fh)
        except (OSError, ValueError):
            return False
        return stamp == self._trust_stamp(text)

    def _install_accounting_loader(self) -> None:
        loader = getattr(self._store, "set_accounting_cache_loader", None)
        if loader is not None:
            loader(self._accounting_payload)

    def writing_to(self, path: str) -> CachedStore:
        """A persistent wrapper over the same backend that writes ``path`` (a loader child)."""
        return CachedStore(self._store, self._cache_id, self._args, path=path)

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
            with debug.span("cache.trust"):
                trusted = self._trusted(text)
            del text
        except FileNotFoundError:
            return self._reject("disk", "missing")
        except OSError:
            return self._reject("disk", "unreadable")
        except ValueError:
            return self._reject("disk", "invalid_json")
        data = self._validate(data, trusted)
        if data is None or "model_breakdown" in data:
            return data
        pin = (data["models_external"]["sha1"] + "\n").encode("ascii", "replace")
        try:
            fh = open(self._path + MODELS_SUFFIX, "rb")
        except OSError:
            return self._reject("models", "missing")
        if fh.read(len(pin)) != pin:
            fh.close()
            return self._reject("models", "other_generation")
        data["_models_file"] = fh  # in memory only; read and closed by _disk_models()
        return data

    def _close_models(self) -> None:
        # Windows cannot replace a file held open, so release it before any write.
        fh = (self._disk or {}).pop("_models_file", None)
        if fh is not None:
            fh.close()

    @debug.timed("cache.validate")
    def _validate(self, data, trusted: bool = False) -> dict | None:
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return self._reject("disk", "version_or_shape")
        inline = "model_breakdown" in data
        external = data.get("models_external")
        if not isinstance(data.get("workflows"), list) or (
            not isinstance(data.get("model_breakdown"), list)
            if inline
            else not (isinstance(external, dict) and isinstance(external.get("sha1"), str))
        ):
            return self._reject("disk", "rollup_shape")
        # Reject the entire cache on shape drift; skipping a row would lose usage silently.
        if not all(isinstance(row, dict) for row in data["workflows"]):
            return self._reject("disk", "workflow_shape")
        if inline and self._model_problem(data["model_breakdown"], trusted):
            return self._reject("disk", self._model_problem(data["model_breakdown"], trusted))
        if not isinstance(data.get("provenance"), dict):
            data["provenance"] = {}  # readable, just not spliceable: full parse on a miss
        data["_trusted"] = trusted  # in memory only: whether the sidecar may skip pricing
        self._debug(
            "cache.loaded",
            workflows=len(data["workflows"]),
            models=len(data["model_breakdown"]) if inline else None,
            trusted=trusted,
        )
        return data

    @staticmethod
    def _model_problem(rows, trusted: bool) -> str | None:
        # Validate direct-index fields before the hit path loses its reparse fallback.
        # Pricing buckets are the expensive part; a trusted file skips only those.
        if not all(
            isinstance(row, dict)
            and MODEL_ROW_KEYS <= row.keys()
            and (trusted or valid_pricing(row))
            for row in rows
        ):
            return "model_shape"
        # root_id is used as a mapping key.
        if not all(isinstance(row["root_id"], str) for row in rows):
            return "model_identity_shape"
        return None

    @debug.timed("cache.models_read")
    def _disk_models(self) -> list | None:
        """The disk cache's model rows, read from the sidecar on first use; None if unusable."""
        disk = self._disk
        if disk is None:
            return None
        if "model_breakdown" in disk:
            return disk["model_breakdown"]
        fh = disk.pop("_models_file", None)
        if fh is None:
            return self._reject("models", "missing")
        try:
            with fh:
                raw = fh.read()  # the generation checked at read time, even if replaced since
            if hashlib.sha1(raw).hexdigest() != disk["models_external"]["sha1"]:
                return self._reject("models", "sidecar_mismatch")
            rows = json.loads(raw.decode("utf-8"))
        except OSError:
            return self._reject("models", "unreadable")
        except ValueError:
            return self._reject("models", "invalid_json")
        if not isinstance(rows, list):
            return self._reject("models", "rollup_shape")
        problem = self._model_problem(rows, bool(disk.get("_trusted")))
        if problem:
            return self._reject("models", problem)
        disk["model_breakdown"] = rows
        self._debug("cache.models_loaded", models=len(rows))
        return rows

    @debug.timed("cache.write")
    def _write(
        self,
        fingerprint: list,
        workflows: list,
        model_breakdown: list,
        provenance: dict | None = None,
        model_token: str | None = None,
    ) -> None:
        # Cache writes are best-effort and atomic; failure must not block launch.
        payload = {
            "version": CACHE_VERSION,
            "source": self._source,
            "fingerprint": fingerprint,
            # The backend just parsed, so lazy cost state is already available.
            "records_cost": bool(getattr(self._store, "records_cost", True)),
            "workflows": workflows,
            # Empty provenance disables incremental misses for this backend.
            "provenance": provenance or {},
        }
        if model_token is not None:
            payload["model_token"] = model_token
        getter = getattr(self._store, "accounting_cache", None)
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
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
            with debug.span("cache.encode_models"):
                models = json.dumps(model_breakdown, separators=(",", ":"))
            digest = hashlib.sha1(models.encode("utf-8")).hexdigest()
            self._close_models()
            # The sidecar goes first: a cache never names rows that are not on disk yet.
            self._write_text(self._path + MODELS_SUFFIX, digest + "\n" + models)
            payload["models_external"] = {"sha1": digest, "rows": len(model_breakdown)}
            del models
            text = self._write_json(self._path, payload)
            try:
                # Written second: a crash in between leaves a stale digest, which fails
                # to match and costs one full validation, never trust in other rows.
                self._write_text(self._path + TRUST_SUFFIX, json.dumps(self._trust_stamp(text)))
            except OSError:
                pass
            payload["model_breakdown"] = model_breakdown
            self._disk = payload
            self._fresh_wf = None
            self._fresh_models = None
            self._fresh_prov = None
            self._debug("cache.written", workflows=len(workflows), models=len(model_breakdown))
        except (OSError, sqlite3.Error) as exc:
            self._debug("cache.write_failed", error_type=type(exc).__name__)

    @classmethod
    def _write_json(cls, path: str, payload) -> str:
        # dumps uses the C encoder. dump's Python generator/write per scalar made
        # an unchanged 55k-row accounting snapshot cost over a second on WSL.
        with debug.span("cache.encode") as info:
            text = json.dumps(payload, separators=(",", ":"))
            info["characters"] = len(text)
        cls._write_text(path, text)
        return text

    @staticmethod
    def _write_text(path: str, text: str) -> None:
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

    def full_parse_bytes(self) -> int:
        """Input bytes the next workflows() call would parse in full, else 0.

        Cache hits, splices and accounting reuse are cheap and stay in-process;
        CombinedStore moves only full parses to a loader process. The splice plan
        is memoized against this fingerprint so workflows() does not recompute it.
        """
        live_fp = self._fingerprint()
        if self._fresh_wf is not None and self._live_fp == live_fp:
            return 0
        disk = self._disk
        if disk is not None:
            if disk.get("fingerprint") == live_fp:
                return 0
            if getattr(self._store, "restore_accounting_cache", None) is not None and (
                disk.get("accounting_external") or disk.get("accounting")
            ):
                return 0  # changed rows only, against the stored scalar snapshot
            try:
                plan = self._splice_plan(live_fp)
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                plan = None
            self._plan_memo = (live_fp, plan)
            if plan is not None:
                return 0
        total = 0
        for row in live_fp:
            if isinstance(row, (list, tuple)) and len(row) == 3 and isinstance(row[1], int):
                total += row[1]
        return total

    def load_from_disk(self, path: str | None = None) -> None:
        """Adopt the cache another process just wrote; workflows() then checks it as usual.

        A non-persistent wrapper passes the private handoff ``path``; the one it
        replaces is deleted, since nothing reads it once the new one is adopted.
        """
        if path is not None and path != self._path:
            old, self._path = self._path, path
            if old is not None and not self.persistent:
                for stale in (
                    old,
                    old + ".usage.sqlite3",
                    old + TRUST_SUFFIX,
                    old + MODELS_SUFFIX,
                ):
                    try:
                        os.unlink(stale)
                    except OSError:
                        pass
            self._install_accounting_loader()
        self._close_models()
        self._disk = self._read()
        self._plan_memo = None

    @debug.timed("cache.splice")
    def _splice(self, live_fp: list):
        """Return a proven-safe incremental rollup, or ``None`` for a full parse."""
        memo, self._plan_memo = self._plan_memo, None
        plan = memo[1] if memo is not None and memo[0] == live_fp else self._splice_plan(live_fp)
        if plan is None:
            return None
        disk = self._disk
        prov = disk.get("provenance") or {}
        subset, sorter = self._store.parse_subset, self._store.sort_workflows
        affected, read = plan
        with debug.span("cache.parse_subset", source=self._source):
            sliced = subset(read)
        return self._finish_splice(disk, prov, sorter, affected, sliced)

    def _splice_plan(self, live_fp: list):
        """Return ``(affected sessions, files to reread)``, or ``None`` for a full parse."""
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
        return affected, sorted(seen_files & set(new_fp))

    def _finish_splice(self, disk, prov, sorter, affected, sliced):
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
        disk_models = self._disk_models()
        if disk_models is None:
            return None  # rejected and logged: the caller parses everything
        kept_models = [dict(row) for row in disk_models if row.get("root_id") not in drop]
        rows = sorter(kept + list(fresh_wf))
        provenance = {sid: paths for sid, paths in prov.items() if sid not in drop}
        provenance.update(fresh_prov)
        return rows, [asdict(w) for w in rows], kept_models + list(fresh_models), provenance

    @debug.timed("cache.workflows")
    def workflows(self) -> list:
        # Reload must observe changes, so fingerprint every call.
        self.served_by_process = False  # CombinedStore sets it after a loader process
        if self._path is None:
            # --no-cache with nothing handed off: exactly the bare backend.
            self.served_from_cache = None
            self.served_incrementally = False
            return self._store.workflows()
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
                self._close_models()
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
        if self._path is None:
            return self._store.model_breakdown()
        fp = self._live_fp if self._live_fp is not None else self._fingerprint()
        if (
            self._fresh_wf is None
            and self._disk is not None
            and self._disk.get("fingerprint") == fp
        ):
            disk_rows = self._disk_models()
            if disk_rows is not None:
                self._debug("cache.models_decision", result="hit")
                return [dict(row) for row in disk_rows]
            # The workflows came from this cache but its model rows are unusable:
            # parse both now, so the write below replaces the whole cache.
            self._close_models()
            self._disk = None
            with debug.span("cache.backend_workflows", source=self._source):
                self._fresh_wf = [asdict(w) for w in self._store.workflows()]
            self._fresh_models = None
            self._fresh_prov = None
        token_of = getattr(self._store, "model_cache_token", None)
        token = token_of() if token_of is not None else None
        if self._fresh_models is not None:
            # An incremental splice already built these from the files it re-read.
            # Calling through would parse the whole corpus -- what the splice avoided.
            rows = [dict(row) for row in self._fresh_models]
            self._debug("cache.models_decision", result="splice_memo")
        elif (
            token is not None
            and self._disk is not None
            and self._disk.get("model_token") == token
            and fp == self._fingerprint()
            and self._disk_models() is not None
        ):
            rows = [dict(row) for row in self._disk_models()]
            self._debug("cache.models_decision", result="unchanged_accounting")
        else:
            self._debug("cache.models_decision", result="backend")
            with debug.span("cache.backend_models", source=self._source):
                rows = [dict(row) for row in self._store.model_breakdown()]
        # Write only when the workflows AND the breakdown were both parsed fresh under
        # this same fingerprint -- a complete, self-consistent cache.
        # A --no-cache wrapper reads its handoff but never writes.
        if self._fresh_wf is not None and self.persistent:
            prov = self._fresh_prov
            if prov is None:
                getter = getattr(self._store, "cache_provenance", None)
                prov = getter() if getter is not None else None
            # A concurrent source change must not label old model rows with a new token.
            stable_token = token if token is not None and token_of() == token else None
            self._write(fp, self._fresh_wf, rows, prov, model_token=stable_token)
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
