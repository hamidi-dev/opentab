"""CombinedStore: merge several backends into one view."""
from __future__ import annotations

from opentab.models import Workflow


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
        self.records_cost = all(getattr(s, "records_cost", True) for s in stores)
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

    def workflows(self) -> list[Workflow]:
        out: list[Workflow] = []
        owner: dict[str, object] = {}
        for store in self.stores:
            for w in store.workflows():
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
        for store in self.stores:
            out.extend(store.model_breakdown())
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
        # Route to the owning backend; only OpenCode produces rows, the rest don't
        # implement it (so the Tools tab is empty for a Claude/Codex/Hermes/CSV
        # session even in the merged view).
        owner = self._owner.get(workflow_id)
        fetch = getattr(owner, "tool_breakdown", None)
        return fetch(workflow_id) if fetch else []

    def supports_tools(self, workflow_id: str) -> bool:
        # Only the owning backend decides -- so an OpenCode session offers the Tools
        # tab even in the merged view, while a Claude/Codex/Hermes/CSV session never
        # does (they don't implement tool_breakdown yet).
        return getattr(self._owner.get(workflow_id), "supports_tool_breakdown", False)

    def message_timeline(self, workflow_id: str) -> list:
        # Route to the owning backend; only OpenCode and Claude Code implement it, so
        # a Codex/Hermes/CSV session has no Turns tab even in the merged view.
        owner = self._owner.get(workflow_id)
        fetch = getattr(owner, "message_timeline", None)
        return fetch(workflow_id) if fetch else []

    def supports_turns(self, workflow_id: str) -> bool:
        check = getattr(self._owner.get(workflow_id), "supports_turns", None)
        return bool(check(workflow_id)) if check else False
