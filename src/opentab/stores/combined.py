"""CombinedStore: merge several backends into one view."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import cached_property

from opentab.models import Workflow


def _gather(calls: list) -> list:
    # Run each 0-arg callable in its own thread and return the results IN ORDER. The
    # backends hold disjoint state (each its own files / sqlite connection), so their
    # workflows()/model_breakdown() -- the bulk of startup -- run independently; only the
    # merge that consumes these results touches shared state, back on the caller's
    # thread. Overlapping them collapses --source all from the sum of the backends toward
    # the slowest single one. sqlite/read() release the GIL, so even the DB scan overlaps
    # the file parses. Exceptions propagate on iteration, matching the old serial loop.
    calls = list(calls)
    if len(calls) <= 1:
        return [c() for c in calls]
    with ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="opentab-store") as ex:
        return list(ex.map(lambda c: c(), calls))


class CombinedStore:
    """Merge several backends (OpenCode Store + Claude Code ClaudeStore + Codex
    CodexStore + Hermes + CSV + Copilot CLI + pi) into one view by delegating the same four
    methods to each and concatenating the results.

    Workflow ids are globally unique across sources (OpenCode "ses_..." vs Claude/Codex
    UUIDs), so model_breakdown root_ids never clash and workflow_nodes routing stays
    unambiguous; an _owner map (built in workflows()) records which backend produced
    each workflow so workflow_nodes goes straight to it. Projects group by directory
    across both tools, so the same repo worked in OpenCode and Claude Code rolls up
    into one row.

    Cost is mixed -- OpenCode's recorded dollars plus Claude Code's $0 (until "$"
    reprices its tokens). The normal "$" what-if reprices every unpriced row across
    both backends, so it just works. records_cost is False when any backend doesn't
    record cost (i.e. Claude is in the mix), driving the header hint; combined=True
    turns on the per-session source tags in the sessions list.
    """

    combined = True
    source_name = "all"  # the merged view; per-session origin lives on Workflow.source

    def __init__(self, stores: list):
        self.stores = stores
        # Combined demo: each backend would otherwise draw its own random hidden scale,
        # which would distort the cross-source ratio (the Sources view lies about the
        # OpenCode-vs-Claude proportion). Share ONE scale across all backends so the
        # proportions stay truthful -- still private (a single hidden factor can't be
        # inverted to real dollars).
        self.demo = any(getattr(s, "demo", False) for s in stores)
        if self.demo:
            scale = next((s.demo_scale for s in stores if getattr(s, "demo", False)), 1.0)
            for s in stores:
                s.demo_scale = scale
            self.demo_scale = scale
        else:
            self.demo_scale = 1.0
        # Tool breakdown is OpenCode-only today; offer the Tools tab if any backend
        # in the mix supports it. tool_breakdown() routes per session to its owner.
        self.supports_tool_breakdown = any(
            getattr(s, "supports_tool_breakdown", False) for s in stores
        )
        self._owner: dict[str, object] = {}

    @cached_property
    def records_cost(self) -> bool:
        # AND of the backends (False when any doesn't record cost), evaluated lazily so
        # building the merged view never forces a backend's full-corpus cost probe --
        # after workflows() the warm-start cache answers this for free.
        return all(getattr(s, "records_cost", True) for s in self.stores)

    def workflows(self) -> list[Workflow]:
        # Roll up every backend in parallel, then build the id->owner map and merge on
        # this thread (deterministic, order-preserving) -- see _gather.
        out: list[Workflow] = []
        owner: dict[str, object] = {}
        for store, workflows in zip(self.stores, _gather([s.workflows for s in self.stores])):
            for w in workflows:
                owner[w.id] = store
                out.append(w)
        self._owner = owner
        out.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return out

    def summary(self, workflows: list[Workflow]) -> dict[str, int | float]:
        # summary() is pure over the passed workflows, so any backend computes it.
        return self.stores[0].summary(workflows)

    def model_breakdown(self) -> list:
        out: list = []
        for rows in _gather([s.model_breakdown for s in self.stores]):
            out.extend(rows)
        return out

    def workflow_nodes(self, workflow_id: str) -> list:
        owner = self._owner.get(workflow_id)
        if owner is not None:
            return owner.workflow_nodes(workflow_id)
        for store in self.stores:  # fallback before workflows() has populated _owner
            nodes = store.workflow_nodes(workflow_id)
            if nodes:
                return nodes
        return []

    def tool_breakdown(self, workflow_id: str) -> list:
        # Route to the owning backend; a backend without the Tools opt-in
        # (Hermes, Copilot, VS Code, OpenClaw) contributes no rows.
        owner = self._owner.get(workflow_id)
        fetch = getattr(owner, "tool_breakdown", None)
        return fetch(workflow_id) if fetch else []

    def supports_tools(self, workflow_id: str) -> bool:
        # The owning backend decides per session (the supports_turns pattern) -- so
        # an OpenCode/Claude/Codex/pi/CSV session offers the Tools tab even in the
        # merged view, while a backend without the opt-in never does.
        check = getattr(self._owner.get(workflow_id), "supports_tools", None)
        return bool(check(workflow_id)) if check else False

    def message_timeline(self, workflow_id: str) -> list:
        # Route to the owning backend; only OpenCode and Claude Code implement it, so
        # a Codex/Hermes/CSV session has no Turns tab even in the merged view.
        owner = self._owner.get(workflow_id)
        fetch = getattr(owner, "message_timeline", None)
        return fetch(workflow_id) if fetch else []

    def supports_turns(self, workflow_id: str) -> bool:
        check = getattr(self._owner.get(workflow_id), "supports_turns", None)
        return bool(check(workflow_id)) if check else False
