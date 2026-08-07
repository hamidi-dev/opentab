"""RemoteStore: browse other machines' spend from exported summary files.

opentab reads only local data. To consolidate several machines, each machine
exports a compact summary (``opentab --export``) and those summaries are gathered
into one directory -- via ``opentab --pull``, a shared folder, scp, Syncthing,
whatever moves a file. RemoteStore reads that directory and presents every
machine's sessions merged, each tagged with the machine it came from.

The summary IS the warm-start cache payload (workflows() + model_breakdown() +
records_cost) plus a machine label -- so ``build_export`` here is the mirror image
of stores.cached.CachedStore._write, and RemoteStore is CachedStore read in
reverse. Because the raw per-model rows travel too, the "$" what-if and the "w"
model-target comparison recompute against list prices on remote data exactly as
they do locally, with no special-casing.

The rollup travels, and so does the drill-in: the subagent TREE (workflow_nodes, for
the sessions that delegated) and, as of export v2, the lazy per-session extras --
Turns (message_timeline), Tools (tool_breakdown) and the estimated Context
composition (context_breakdown) -- so a pulled session's tabs are as real as a local
one's. That makes the extras the heavy part of an export (transcript-scale, fetched
per session), a cost paid once at ``--export`` time; the summaries themselves grow
accordingly. A v1 summary carried none of the extras, so RemoteStore's supports_*
gates simply hide those tabs for it -- older exports still load, just without the
turn-by-turn detail.
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import os
from dataclasses import asdict, fields
from urllib.parse import unquote

from opentab.demo import DEMO_ALL, demo_config, demo_machine, scramble_node, scramble_workflow
from opentab.models import Workflow
from opentab.util import tool_names

# The on-disk summary format version. Separate from CachedStore.CACHE_VERSION on
# purpose: a summary is a portable interface between machines that may run different
# opentab versions, so it evolves on its own cadence (and RemoteStore stays
# tolerant of unknown keys -- see _load). Bump only on an incompatible shape change.
EXPORT_VERSION = 2  # v2 adds the per-session Turns/Tools/Context extras (see build_export)

_WF_FIELDS = {f.name for f in fields(Workflow)}

# The subagent-node fields the App/Renderer read (detail_subagents, _priced_nodes,
# whatif_node_price). An exported node is normalized to exactly these on load, with safe
# defaults + coerced types, so a partial/garbage node (a crafted `{}` or a string where a
# count is expected) renders/scales instead of crashing with KeyError/TypeError.
_NODE_INT_FIELDS = (
    "tokens_input",
    "tokens_output",
    "tokens_reasoning",
    "tokens_cache_read",
    "tokens_cache_write",
    "tokens_cache_write_1h",  # subset of tokens_cache_write; long-TTL pricing
    "tokens_total",
)


def _coerce_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# The v2 extras are normalized on load just like _clean_node: a summary is an untrusted
# file (corrupt, hand-edited, or from a future version), and the Turns/Tools/Context
# renderers read these fields with [] -- so a partial row like {} must render as zeros,
# never KeyError the drill-in. Each cleaner emits exactly the fields its renderer reads.
_TURN_INT_FIELDS = (
    "depth",
    "tokens_total",
    "input",
    "output",
    "reasoning",
    "cache_read",
    "cache_write",
    "cache_write_1h",
)
_TOOL_INT_FIELDS = (
    "calls",
    "tokens_total",
    "input",
    "output",
    "reasoning",
    "cache_read",
    "cache_write",
    "cache_write_1h",
)


def _clean_turn(row: dict) -> dict:
    turn = {
        "time": str(row.get("time") or ""),
        "model_name": str(row.get("model_name") or "unknown"),
        "agent": str(row.get("agent") or "-"),
        "prompt_id": str(row.get("prompt_id") or ""),
        "prompt_title": str(row.get("prompt_title") or ""),
        "prompt_full": str(row.get("prompt_full") or row.get("prompt_title") or ""),
        "cost": _coerce_float(row.get("cost")),
        # The tools this step called. This whitelist is what a pulled machine's Turns
        # tab gets, so a field missing here is a column that silently disappears on
        # every remote session while the local one still draws it -- the two-views-
        # disagreeing failure. Sanitized through the SAME gate the renderer uses, so a
        # hostile or older payload can't put anything through here that a local
        # transcript couldn't (util.tool_names documents what it rejects and why).
        "tools": tool_names(row.get("tools")),
    }
    for field in _TURN_INT_FIELDS:
        turn[field] = _coerce_int(row.get(field))
    return turn


def _clean_tool(row: dict) -> dict:
    tool = {
        "tool": str(row.get("tool") or "?"),
        "model_name": str(row.get("model_name") or "unknown"),
        "cost": _coerce_float(row.get("cost")),
    }
    for field in _TOOL_INT_FIELDS:
        tool[field] = _coerce_int(row.get(field))
    return tool


def _clean_context(row: dict) -> dict:
    return {
        "category": str(row.get("category") or "other"),
        "kind": str(row.get("kind") or ""),
        # detail_context sums this per category ("40× ~200 tokens"); a row without
        # it (an older/partial export) must default to 0, never KeyError mid-draw.
        "count": _coerce_int(row.get("count")),
        "est_tokens": _coerce_int(row.get("est_tokens")),
    }


def _clean_node(row: dict) -> dict:
    node = {
        "depth": _coerce_int(row.get("depth")),
        "agent": str(row.get("agent") or "-"),
        "title": str(row.get("title") or "(untitled)"),
        "created_at": str(row.get("created_at") or ""),
        "model_name": str(row.get("model_name") or "unknown"),
    }
    try:
        node["cost"] = float(row.get("cost") or 0.0)
    except (TypeError, ValueError):
        node["cost"] = 0.0
    for field in _NODE_INT_FIELDS:
        node[field] = _coerce_int(row.get(field))
    return node


def _export_supports(store, name: str, sid: str) -> bool:
    # Per-session opt-in gate (supports_turns/tools/context), tolerant of a backend that
    # doesn't implement it or raises on a bad session.
    fn = getattr(store, name, None)
    if not fn:
        return False
    try:
        return bool(fn(sid))
    except Exception:  # noqa: BLE001
        return False


def _export_curve_ok(store, sid: str) -> bool:
    # Whether the measured Context growth curve applies -- App.session_supports_context_curve's
    # rule, shipped from the source so the remote view doesn't have to re-derive it: any turns
    # backend supports it UNLESS it explicitly opts out (Codex's cumulative deltas, CSV/JSONL).
    fn = getattr(store, "supports_context_curve", None)
    if fn is None:
        return True
    try:
        return bool(fn(sid))
    except Exception:  # noqa: BLE001
        return True


def _export_rows(store, name: str, sid: str) -> list[dict]:
    # One session's extra rows (message_timeline/tool_breakdown/context_breakdown/workflow_nodes)
    # as plain dicts. One bad session must not sink the whole export.
    fn = getattr(store, name, None)
    if not fn:
        return []
    try:
        return [dict(r) for r in fn(sid)]
    except Exception:  # noqa: BLE001
        return []


def _collect_timeline(store, wf_objs) -> dict[str, list[dict]]:
    # {session id: Turns rows} for every session, using a backend's whole-corpus batch
    # (message_timeline_all) where it offers one and the per-session path otherwise.
    # OpenCode's per-session Turns query re-scans the message table under a recursive CTE
    # (~200ms/session; 138s over 689 sessions in a real export) -- its batch collapses
    # that to one grouped scan, ~100x. File backends (Claude/Codex/pi/Zaly) parse once and
    # slice, so their per-session path is already cheap and needs no batch.
    batch_fn = getattr(store, "message_timeline_all", None)
    batched: dict[str, list[dict]] = {}
    batch_ok = False
    if batch_fn:
        try:
            batched = batch_fn() or {}
            batch_ok = True
        except Exception:  # noqa: BLE001 -- a batch failure must fall back, not sink the export
            batch_ok = False  # so batch_covers() sends every session down the slow path
    owner = getattr(store, "_owner", None)

    def batch_covers(sid: str) -> bool:
        # True when a batch DEFINITIVELY owns this session (so an all-aborted session it
        # returned no rows for is not re-fetched by the slow per-session query). Only when
        # the batch actually SUCCEEDED -- if message_timeline_all raised, batched is empty
        # and every session must fall back, or the export would silently drop all its
        # Turns. For the merged store "owns" is "the owning backend has a batch"; for a
        # leaf export, "the store does".
        if not batch_ok:
            return False
        if owner is not None:
            return bool(getattr(owner.get(sid), "message_timeline_all", None))
        return batch_fn is not None

    out: dict[str, list[dict]] = {}
    for w in wf_objs:
        sid = w.id
        if sid in batched:
            rows = [dict(r) for r in batched[sid]]
            if rows:
                out[sid] = rows
        elif batch_covers(sid):
            continue  # covered by a batch that yielded nothing for it -- don't re-query
        elif _export_supports(store, "supports_turns", sid):
            rows = _export_rows(store, "message_timeline", sid)
            if rows:
                out[sid] = rows
    return out


def build_export(
    store,
    label: str,
    exported_at: str = "",
    opentab_version: str = "",
) -> dict:
    """Serialize a machine's whole rollup to a portable summary dict.

    ``store`` is any backend (usually the merged "all" view), so its workflows()
    already carry each session's ``source`` (backend) tag; RemoteStore adds the
    machine tag on load. model_breakdown() rows may be sqlite3.Row (OpenCode) --
    ``dict(row)`` normalizes both, matching CachedStore._write.

    The subagent TREE (workflow_nodes, for the sessions that delegated) and the lazy
    per-session extras -- Turns (``message_timeline``), Tools (``tool_breakdown``) and the
    estimated Context composition (``context_breakdown``), plus the ``curve_ok`` set naming
    the sessions whose measured Context growth curve applies -- ride along too, so a pulled
    session's tabs are as real as a local one's. The extras are the HEAVY part of an export:
    transcript-scale (an order of magnitude larger than the rollup), the scan the TUI defers
    to drill-in, paid up front here for every session. Turns go through the whole-corpus
    batch (``_collect_timeline``) -- OpenCode's per-session Turns query is a recursive-CTE
    message-table re-scan that dominates a big export (measured 138s over 689 sessions),
    which one grouped scan cuts ~100x. The other extras stay per-session: a file backend
    parses once and slices (cheap), and OpenCode's nodes/tools are already ~per-session
    scans over the smaller session/part tables. The raw rows travel; a demo view
    re-anonymises them lazily (App._scale_demo_turns et al.), exactly as it does for a local
    store -- RemoteStore never demos the extras itself, so there's no double-scaling.
    """
    wf_objs = store.workflows()
    workflows = [asdict(w) for w in wf_objs]
    model_breakdown = [dict(row) for row in store.model_breakdown()]
    turns = _collect_timeline(store, wf_objs)
    nodes: dict[str, list[dict]] = {}
    tools: dict[str, list[dict]] = {}
    context: dict[str, list[dict]] = {}
    curve_ok: list[str] = []
    for w in wf_objs:
        sid = w.id
        if w.subagents:
            rows = _export_rows(store, "workflow_nodes", sid)
            if rows:
                nodes[sid] = rows
        if sid in turns and _export_curve_ok(store, sid):
            curve_ok.append(sid)
        if _export_supports(store, "supports_tools", sid):
            rows = _export_rows(store, "tool_breakdown", sid)
            if rows:
                tools[sid] = rows
        if _export_supports(store, "supports_context", sid):
            rows = _export_rows(store, "context_breakdown", sid)
            if rows:
                context[sid] = rows
    return {
        "opentab_export": EXPORT_VERSION,
        "label": label,
        "exported_at": exported_at,
        "opentab_version": opentab_version,
        "records_cost": bool(getattr(store, "records_cost", True)),
        "workflows": workflows,
        "model_breakdown": model_breakdown,
        "nodes": nodes,
        "turns": turns,
        "tools": tools,
        "context": context,
        "curve_ok": curve_ok,
    }


class RemoteStore:
    """Merge several machines' exported summaries into one read-only view.

    combined=True turns on the per-session origin tags (the Src column / [oc]-style
    title tags) already used by the merged local view -- remote data spans both
    backends and machines, so the same machinery labels it.
    """

    combined = True
    source_name = "remote"

    def __init__(self, source, args: argparse.Namespace | None = None, exclude_ids=None):
        # source: a directory of *.json summaries, a single summary file, or an
        # explicit list of paths (tests pass a list). exclude_ids: session ids to drop
        # (the fleet passes the LIVE local ids so a pulled summary of a session we
        # already have locally can't double-count or steal its drill-in).
        self._args = args
        # Demo config (categories + hidden scale) shared with every other store; the
        # scale is 1.0 unless spend is scrambled, so absolute numbers can't be read
        # off a shared screen while proportions stay real. See demo_config.
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._exclude_ids = set(exclude_ids or ())
        self._paths = self._resolve_paths(source)
        self._wf: list[Workflow] = []
        self._models: list[dict] = []
        # session id -> its subagent tree (workflow_nodes rows), for the sessions that
        # exported one. Raw; demo is applied lazily in workflow_nodes (like _wf).
        self._nodes: dict[str, list[dict]] = {}
        # v2 per-session extras: session id -> Turns / Tools / Context-composition rows,
        # and the set of ids whose measured Context curve applies. Raw -- the App demos
        # them lazily (App._scale_demo_turns et al.), never RemoteStore, so no double-scale.
        self._turns: dict[str, list[dict]] = {}
        self._tools: dict[str, list[dict]] = {}
        self._context: dict[str, list[dict]] = {}
        self._curve_ok: set[str] = set()
        self._file_sizes: dict[str, int] = {}  # label -> summary file bytes (for --timings)
        self.machines: list[str] = []  # labels loaded, in file order
        # Per-machine niceties for the Machines mode: the label -> {exported_at,
        # opentab_version, key}. `key` is the remotes.json name (decoded from the summary
        # FILENAME, `_summary_filename`'s inverse) so an in-TUI refresh can re-pull exactly
        # this box. Raw labels here; machine_meta demo-scrambles the KEYS to match w.machine.
        self._machine_info: dict[str, dict] = {}
        self.records_cost = True
        self._load()

    @staticmethod
    def _resolve_paths(source) -> list[str]:
        if isinstance(source, (list, tuple)):
            return list(source)
        if isinstance(source, str) and os.path.isdir(source):
            return sorted(glob.glob(os.path.join(source, "*.json")))
        if isinstance(source, str) and os.path.isfile(source):
            return [source]
        return []

    def _load(self) -> None:
        wfs: list[Workflow] = []
        models: list[dict] = []
        nodes: dict[str, list[dict]] = {}
        # The v2 per-session extras (Turns/Tools/Context) and the ids whose measured
        # Context curve applies -- absent in a v1 summary, leaving those tabs hidden.
        turns: dict[str, list[dict]] = {}
        tools: dict[str, list[dict]] = {}
        context: dict[str, list[dict]] = {}
        curve_ok: set[str] = set()
        machines: list[str] = []
        info: dict[str, dict] = {}
        sizes: dict[str, int] = {}  # label -> summary file size on disk, for --timings
        records: list[bool] = []
        # Seed with the excluded (live-local) ids so a summary re-stating one is dropped,
        # then dedup ids across machines (a rotated/synced session) on top.
        seen: set[str] = set(self._exclude_ids)
        for path in self._paths:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue  # a broken file is skipped, never fatal (like notes.json)
            if not isinstance(data, dict) or not isinstance(data.get("workflows"), list):
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            label = str(data.get("label") or stem)
            machines.append(label)
            try:
                sizes[label] = os.path.getsize(path)
            except OSError:
                sizes[label] = 0
            # The remotes.json key is the summary filename decoded (unquote reverses
            # _summary_filename's percent-encoding, incl. the leading-dot %2E guard) --
            # the handle an in-TUI refresh re-pulls this box by.
            info[label] = {
                "exported_at": str(data.get("exported_at") or ""),
                "opentab_version": str(data.get("opentab_version") or ""),
                "key": unquote(stem),
            }
            records.append(bool(data.get("records_cost", True)))
            kept: set[str] = set()  # ids kept from THIS file (to filter its model rows)
            for row in data["workflows"]:
                if not isinstance(row, dict):
                    continue
                # Keep only fields this opentab knows: a newer export with extra
                # fields must load, not crash (forward compatibility).
                clean = {k: v for k, v in row.items() if k in _WF_FIELDS}
                # A session id must be a real string: it's the key for dedup, for the
                # per-file `kept` set, and for the App's model attribution. A missing or
                # non-string id (a corrupt/crafted summary) is dropped, not carried.
                if not isinstance(clean.get("id"), str) or not clean["id"]:
                    continue
                try:
                    w = Workflow(**clean)
                except TypeError:
                    continue
                if w.id in seen:
                    continue
                seen.add(w.id)
                kept.add(w.id)
                w.machine = label
                wfs.append(w)
            mb = data.get("model_breakdown")
            for row in mb if isinstance(mb, list) else []:  # a non-list is skipped, not fatal
                if not isinstance(row, dict):
                    continue
                # Keep a model row only for a session we actually kept from THIS file:
                # that dedups a session seen on two machines AND drops any row we can't
                # attribute -- one with no/foreign root_id -- which the App later indexes
                # by row["root_id"] (an absent key would crash the model scan). root_id
                # must be a string: `kept` is a set, so an unhashable value (a crafted
                # `[]`) would raise on the membership test.
                rid = row.get("root_id")
                if isinstance(rid, str) and rid in kept:
                    models.append(dict(row))
            nd = data.get("nodes")
            for sid, rows in nd.items() if isinstance(nd, dict) else ():
                # Only for a session we kept from THIS file (same dedup as model rows), and
                # only well-formed node lists -- a newer/older export without nodes just
                # leaves the Subagents tab empty, never crashes.
                if isinstance(sid, str) and sid in kept and isinstance(rows, list):
                    # Normalize each node to the fields the App reads, with defaults --
                    # so a partial/garbage node renders instead of crashing the tab.
                    kept_rows = [_clean_node(r) for r in rows if isinstance(r, dict)]
                    if kept_rows:
                        nodes[sid] = kept_rows
            # The Turns/Tools/Context extras, same per-file `kept` dedup as nodes: each is a
            # {session id -> [rows]} map, every row normalized (a hostile/partial summary must
            # not crash drill-in -- see the cleaners). A v1 summary omits them entirely, so
            # the tabs just stay hidden there.
            for src_key, target, cleaner in (
                ("turns", turns, _clean_turn),
                ("tools", tools, _clean_tool),
                ("context", context, _clean_context),
            ):
                src = data.get(src_key)
                for sid, rows in src.items() if isinstance(src, dict) else ():
                    if isinstance(sid, str) and sid in kept and isinstance(rows, list):
                        clean = [cleaner(r) for r in rows if isinstance(r, dict)]
                        if clean:
                            target[sid] = clean
            cok = data.get("curve_ok")
            for sid in cok if isinstance(cok, list) else ():
                if isinstance(sid, str) and sid in kept:
                    curve_ok.add(sid)
        self.records_cost = all(records) if records else True
        wfs.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        self._wf = wfs  # RAW, unscaled -- demo is applied lazily in workflows()
        self._models = models
        self._nodes = nodes
        self._turns = turns
        self._tools = tools
        self._context = context
        self._curve_ok = curve_ok
        self._file_sizes = sizes
        self.machines = machines
        self._machine_info = info

    @property
    def _demo_names(self) -> bool:
        # Whether to scramble machine labels: only when the `titles` category is on
        # (a box name is identity, like a title or a path). Every machine-label site
        # shares this one gate so the scrambled labels keep matching the scrambled
        # w.machine that _apply_demo stamps -- the Machines views join them by label.
        return self.demo and "titles" in self.demo_cats

    @property
    def machine_meta(self) -> dict[str, dict]:
        # {machine label -> {live, exported_at, opentab_version, key}} for the Machines
        # mode. Under demo the label is scrambled to match the scrambled w.machine that
        # workflows() stamps (demo_machine is deterministic, so both agree); the `key`
        # value stays the real remotes handle -- a refresh is a real re-pull, and demo
        # gates refresh off anyway.
        out: dict[str, dict] = {}
        for label, meta in self._machine_info.items():
            name = demo_machine(label) if self._demo_names else label
            out[name] = {"live": False, **meta}
        return out

    def _files(self) -> list[str]:
        # The summary files backing this store -- the --timings "files" count (one per box).
        return list(self._paths)

    def machine_stats(self) -> list[dict]:
        # Per-machine volume for the --timings breakdown: sessions kept (deduped, so a
        # session re-stated by two boxes counts once on the box it was kept for) and the
        # summary file's size on disk -- which is where the v2 extras show up. Read off the
        # loaded state, no re-parse. Under demo the label is scrambled (demo_machine, as in
        # machine_meta) so it MATCHES the demo-scrambled w.machine that workflows() stamps
        # -- the --timings table joins bytes to a machine by label, so a raw label here
        # would miss under demo and show every pulled box as 0 B.
        counts: dict[str, int] = {}
        for w in self._wf:  # _wf is raw -- w.machine is the real label
            counts[w.machine] = counts.get(w.machine, 0) + 1
        return [
            {
                "label": demo_machine(label) if self._demo_names else label,
                "sessions": counts.get(label, 0),
                "bytes": self._file_sizes.get(label, 0),
            }
            for label in self.machines
        ]

    def _apply_demo(self, wfs: list[Workflow]) -> None:
        # Anonymize the WORKFLOW rows (ids stay real, as everywhere in demo) and scale
        # them, mirroring the leaf stores' _demo_workflow. Applied LAZILY on the copies
        # workflows() returns, not baked into _wf, because in the fleet view CombinedStore
        # shares one demo_scale across its sub-stores by setting store.demo_scale AFTER
        # construction -- scaling eagerly in __init__ would use RemoteStore's own random
        # factor and disagree with the local machine's rows. Model rows stay RAW: the
        # App's _load_model_cache scales and remaps the breakdown for every store.
        for w in wfs:
            scramble_workflow(w, self.demo_scale, self.demo_cats)
            if self._demo_names:  # the box name is identity too -- hide it with titles
                w.machine = demo_machine(w.machine)

    # --- Store interface (four methods) -------------------------------------------
    def workflows(self) -> list[Workflow]:
        # FRESH copies every call, like the leaf stores (which re-parse) and CachedStore
        # (which rebuilds from dicts). The App mutates Workflow.total_cost in place under
        # the "$" view, and reload (r) re-snapshots total_cost as the real cost -- if we
        # handed back the same objects, that estimate would compound on every reload.
        rows = [copy.copy(w) for w in self._wf]
        if self.demo:
            self._apply_demo(rows)  # lazy, so a fleet's shared demo_scale wins -- see above
        return rows

    def summary(self, workflows: list[Workflow]) -> dict:
        return {
            "workflows": len(workflows),
            "cost": sum(w.total_cost for w in workflows),
            "tokens": sum(w.total_tokens for w in workflows),
            "subagents": sum(w.subagents for w in workflows),
            "unpriced_tokens": sum(w.unpriced_tokens for w in workflows),
            "paid_workflows": sum(1 for w in workflows if w.total_cost > 0),
        }

    def model_breakdown(self) -> list[dict]:
        return [dict(row) for row in self._models]

    # The subagent TREE IS exported (for sessions that delegated) -- so the Subagents tab
    # and the $/w what-if work on remote sessions. The other drill-in extras
    # (Turns/Tools/Context) are transcript-scale and stay out; their empty results +
    # supports_* False hide those tabs.
    def workflow_nodes(self, workflow_id: str) -> list:
        rows = [dict(r) for r in self._nodes.get(workflow_id, ())]  # fresh copies each call
        if self.demo:
            for i, node in enumerate(rows):
                self._demo_node(node, workflow_id, i)
        return rows

    def _demo_node(self, node: dict, workflow_id: str, index: int) -> None:
        # Anonymise + scale one exported subagent node, mirroring the leaf stores'
        # _demo_node. The node carries no stable id of its own, so seed the fake title/cost
        # off the (session id, position) -- deterministic across redraws, and the scale is
        # the fleet's shared demo_scale (set on this store after construction).
        scramble_node(node, self.demo_scale, self.demo_cats, seed=f"{workflow_id}:{index}")

    # The v2 per-session extras -- Turns/Tools/Context. Fresh dict copies each call (the
    # App owns and may demo-mutate the list it gets); demo scaling is the App's job, so
    # these return the raw exported rows. A v1 summary shipped none, so the maps are empty
    # and supports_* is False -- the tab hides rather than shows blank.
    def tool_breakdown(self, workflow_id: str) -> list:
        return [dict(r) for r in self._tools.get(workflow_id, ())]

    def message_timeline(self, workflow_id: str) -> list:
        return [dict(r) for r in self._turns.get(workflow_id, ())]

    def context_breakdown(self, workflow_id: str) -> list:
        return [dict(r) for r in self._context.get(workflow_id, ())]

    def supports_turns(self, workflow_id: str) -> bool:
        return workflow_id in self._turns

    def supports_tools(self, workflow_id: str) -> bool:
        return workflow_id in self._tools

    def supports_context(self, workflow_id: str) -> bool:
        return workflow_id in self._context

    def supports_context_curve(self, workflow_id: str) -> bool:
        return workflow_id in self._curve_ok


class MachineTaggedStore:
    """Wrap a live LOCAL store so its sessions carry a machine label in the fleet view.

    In `--remote`/`--pull` the pulled summaries are machine-tagged by RemoteStore, but
    THIS machine's own live data (which we merge in so the fleet view isn't missing the
    box you're sitting at) has machine="". This thin proxy stamps every workflow with
    the local machine name and otherwise delegates entirely -- so the local sessions
    keep full drill-in (Turns/Tools/Context) and the deferred model scan, unlike the
    summary-only remote ones. workflows() is the only overridden method.
    """

    def __init__(self, store, machine: str):
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_machine", machine)

    def __getattr__(self, name):
        return getattr(self._store, name)

    def __setattr__(self, name, value):
        # Forward attribute writes to the wrapped store (e.g. CombinedStore sharing one
        # demo_scale across its sub-stores), keeping the proxy transparent.
        setattr(self._store, name, value)

    def _tag(self) -> str:
        # The label this machine's sessions carry -- scrambled under demo (only when the
        # `titles` category is on, like every other machine-label site) so `D` hides the
        # real local hostname too, exactly as RemoteStore scrambles pulled labels.
        store = self._store
        # A minimal wrapped store (a test leaf) may set demo without demo_cats; default
        # to scrambling all, matching the old all-or-nothing behaviour.
        names = getattr(store, "demo", False) and "titles" in getattr(store, "demo_cats", DEMO_ALL)
        return demo_machine(self._machine) if names else self._machine

    @property
    def machine_meta(self) -> dict[str, dict]:
        # The live local box -- full drill-in, no export timestamp. A property (not
        # delegated through __getattr__) so it wins over the wrapped store's absence of it.
        return {self._tag(): {"live": True, "exported_at": "", "opentab_version": "", "key": ""}}

    def workflows(self) -> list[Workflow]:
        rows = self._store.workflows()
        tag = self._tag()
        for w in rows:
            w.machine = tag
        return rows
