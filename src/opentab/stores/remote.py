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

Only the cheap rollup travels. The subagent TREE rides along too (workflow_nodes,
for the sessions that delegated) -- a small per-session structure, so the remote
Subagents tab and its $/w what-if are real. But the transcript-scale drill-in
(Turns/Tools/Context) is deliberately NOT exported: those return nothing and the
supports_* gates hide their tabs for remote sessions, so a remote session shows its
money, model mix, and subagent tree but not its turn-by-turn detail. That is the
summaries-only contract -- exporting the full transcripts would ship megabytes per
machine and defeat the point.
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import random
from dataclasses import asdict, fields
from urllib.parse import unquote

from opentab.demo import demo_cost, demo_dir, demo_machine, demo_model, demo_title
from opentab.models import Workflow

# The on-disk summary format version. Separate from CachedStore.CACHE_VERSION on
# purpose: a summary is a portable interface between machines that may run different
# opentab versions, so it evolves on its own cadence (and RemoteStore stays
# tolerant of unknown keys -- see _load). Bump only on an incompatible shape change.
EXPORT_VERSION = 1

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
    "tokens_total",
)


def _coerce_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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

    The subagent TREE (workflow_nodes) rides along too, but ONLY for the sessions that
    delegated (``w.subagents``) -- a small per-session structure (agent/model/tokens/cost
    per node, no transcript), the same cheap fetch build_payload embeds for the web. It
    makes the remote Subagents tab real (and its $/w what-if) instead of an empty tab.
    Turns/Tools/Context stay out: those are per-message rollups an order of magnitude
    larger (and Turns leaks prompt titles), so they remain the deliberately-excluded,
    transcript-scale part of the summaries-only contract. Nodes cost one workflow_nodes()
    call per subagent session at export time (the scan the TUI defers) -- fine for a
    one-shot ``--export``; a backend with its own thread-bound connection can't parallelise
    those safely, so they run serially.
    """
    wf_objs = store.workflows()
    workflows = [asdict(w) for w in wf_objs]
    model_breakdown = [dict(row) for row in store.model_breakdown()]
    nodes: dict[str, list[dict]] = {}
    for w in wf_objs:
        if not w.subagents:
            continue
        try:
            rows = store.workflow_nodes(w.id)
        except Exception:  # noqa: BLE001 -- one bad session must not sink the whole export
            continue
        if rows:
            nodes[w.id] = [dict(r) for r in rows]
    return {
        "opentab_export": EXPORT_VERSION,
        "label": label,
        "exported_at": exported_at,
        "opentab_version": opentab_version,
        "records_cost": bool(getattr(store, "records_cost", True)),
        "workflows": workflows,
        "model_breakdown": model_breakdown,
        "nodes": nodes,
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
        self.demo = bool(getattr(args, "demo", False))
        # A hidden per-process scale like every other store's demo, so absolute
        # numbers can't be read off a shared screen while proportions stay real.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._exclude_ids = set(exclude_ids or ())
        self._paths = self._resolve_paths(source)
        self._wf: list[Workflow] = []
        self._models: list[dict] = []
        # session id -> its subagent tree (workflow_nodes rows), for the sessions that
        # exported one. Raw; demo is applied lazily in workflow_nodes (like _wf).
        self._nodes: dict[str, list[dict]] = {}
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
        machines: list[str] = []
        info: dict[str, dict] = {}
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
        self.records_cost = all(records) if records else True
        wfs.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        self._wf = wfs  # RAW, unscaled -- demo is applied lazily in workflows()
        self._models = models
        self._nodes = nodes
        self.machines = machines
        self._machine_info = info

    @property
    def machine_meta(self) -> dict[str, dict]:
        # {machine label -> {live, exported_at, opentab_version, key}} for the Machines
        # mode. Under demo the label is scrambled to match the scrambled w.machine that
        # workflows() stamps (demo_machine is deterministic, so both agree); the `key`
        # value stays the real remotes handle -- a refresh is a real re-pull, and demo
        # gates refresh off anyway.
        out: dict[str, dict] = {}
        for label, meta in self._machine_info.items():
            name = demo_machine(label) if self.demo else label
            out[name] = {"live": False, **meta}
        return out

    def _apply_demo(self, wfs: list[Workflow]) -> None:
        # Anonymize the WORKFLOW rows (ids stay real, as everywhere in demo) and scale
        # them, mirroring the leaf stores' _demo_workflow. Applied LAZILY on the copies
        # workflows() returns, not baked into _wf, because in the fleet view CombinedStore
        # shares one demo_scale across its sub-stores by setting store.demo_scale AFTER
        # construction -- scaling eagerly in __init__ would use RemoteStore's own random
        # factor and disagree with the local machine's rows. Model rows stay RAW: the
        # App's _load_model_cache scales and remaps the breakdown for every store.
        for w in wfs:
            w.title = demo_title(w.id)
            w.directory = demo_dir(w.id)
            w.machine = demo_machine(w.machine)  # scramble the box name too (D must hide it)
            if w.unpriced_tokens > 0:
                add = demo_cost(w.unpriced_tokens, w.id)
                w.total_cost += add
                w.root_cost += add
                w.unpriced_tokens = 0
            w.total_cost = round(w.total_cost * self.demo_scale, 4)
            w.root_cost = round(w.root_cost * self.demo_scale, 4)
            w.total_tokens = int(round(w.total_tokens * self.demo_scale))

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
        seed = f"{workflow_id}:{index}"
        node["title"] = demo_title(seed)
        if node.get("model_name"):
            node["model_name"] = demo_model(node["model_name"])
        cost = float(node.get("cost") or 0.0)
        if cost == 0:
            cost = demo_cost(node.get("tokens_total") or 0, seed)
        node["cost"] = round(cost * self.demo_scale, 4)
        for field in (
            "tokens_input",
            "tokens_output",
            "tokens_reasoning",
            "tokens_cache_read",
            "tokens_cache_write",
            "tokens_total",
        ):
            if field in node:
                node[field] = int(round((node.get(field) or 0) * self.demo_scale))

    def tool_breakdown(self, workflow_id: str) -> list:
        return []

    def message_timeline(self, workflow_id: str) -> list:
        return []

    def context_breakdown(self, workflow_id: str) -> list:
        return []

    def supports_turns(self, workflow_id: str) -> bool:
        return False

    def supports_tools(self, workflow_id: str) -> bool:
        return False

    def supports_context(self, workflow_id: str) -> bool:
        return False

    def supports_context_curve(self, workflow_id: str) -> bool:
        return False


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
        # The label this machine's sessions carry -- scrambled under demo so `D` hides
        # the real local hostname too, exactly as RemoteStore scrambles pulled labels.
        return demo_machine(self._machine) if getattr(self._store, "demo", False) else self._machine

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
