"""CombinedStore: merge several backends into one view."""
from __future__ import annotations

from functools import cached_property

from opentab.demo import DEMO_ALL
from opentab.models import Workflow


def _gather(calls: list) -> list:
    # Backends own disjoint state; overlap their I/O, then merge in input order.
    calls = list(calls)
    if len(calls) <= 1:
        return [c() for c in calls]
    # Keep the ~6 ms import off commands that never merge stores.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="opentab-store") as ex:
        return list(ex.map(lambda c: c(), calls))


class CombinedStore:
    """Merge backend rollups and route session detail to each workflow's owner."""

    combined = True
    source_name = "all"  # the merged view; per-session origin lives on Workflow.source

    def __init__(self, stores: list):
        self.stores = stores
        # One hidden demo scale preserves cross-source proportions.
        self.demo = any(getattr(s, "demo", False) for s in stores)
        self.demo_cats = next(
            (getattr(s, "demo_cats", DEMO_ALL) for s in stores if getattr(s, "demo", False)),
            DEMO_ALL,
        )
        if self.demo:
            scale = next((s.demo_scale for s in stores if getattr(s, "demo", False)), 1.0)
            for s in stores:
                s.demo_scale = scale
            self.demo_scale = scale
        else:
            self.demo_scale = 1.0
        self.supports_tool_breakdown = any(
            getattr(s, "supports_tool_breakdown", False) for s in stores
        )
        self._owner: dict[str, object] = {}
        self._owner_by_workflow: dict[int, object] = {}

    @cached_property
    def records_cost(self) -> bool:
        # Evaluate lazily so construction cannot trigger a corpus cost probe.
        return all(getattr(s, "records_cost", True) for s in self.stores)

    @property
    def machine_meta(self) -> dict[str, dict]:
        # Local metadata wins when a machine is also present in pulled summaries.
        out: dict[str, dict] = {}
        for store in self.stores:
            for name, meta in getattr(store, "machine_meta", {}).items():
                if name not in out or meta.get("live"):
                    out[name] = meta
        return out

    def workflows(self) -> list[Workflow]:
        out: list[Workflow] = []
        owner: dict[str, object] = {}
        owner_by_workflow: dict[int, object] = {}
        for store, workflows in zip(self.stores, _gather([s.workflows for s in self.stores])):
            for w in workflows:
                owner[w.id] = store
                owner_by_workflow[id(w)] = store
                out.append(w)
        self._owner = owner
        self._owner_by_workflow = owner_by_workflow
        out.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return out

    def owner_of(self, workflow: Workflow):
        """Return the exact owner even when two harnesses reuse a native id."""
        return self._owner_by_workflow.get(id(workflow), self._owner.get(workflow.id))

    def summary(self, workflows: list[Workflow]) -> dict[str, int | float]:
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
        owner = self._owner.get(workflow_id)
        fetch = getattr(owner, "tool_breakdown", None)
        return fetch(workflow_id) if fetch else []

    def supports_tools(self, workflow_id: str) -> bool:
        check = getattr(self._owner.get(workflow_id), "supports_tools", None)
        return bool(check(workflow_id)) if check else False

    def message_timeline(self, workflow_id: str) -> list:
        owner = self._owner.get(workflow_id)
        fetch = getattr(owner, "message_timeline", None)
        return fetch(workflow_id) if fetch else []

    def supports_turns(self, workflow_id: str) -> bool:
        check = getattr(self._owner.get(workflow_id), "supports_turns", None)
        return bool(check(workflow_id)) if check else False

    def turn_content(self, workflow_id: str, content_key: str | None = None) -> dict:
        owner = self._owner.get(workflow_id)
        fetch = getattr(owner, "turn_content", None)
        if not fetch:
            return {}
        return (
            fetch(workflow_id, content_key=content_key)
            if content_key is not None
            else fetch(workflow_id)
        )

    def supports_turn_content(self, workflow_id: str) -> bool:
        check = getattr(self._owner.get(workflow_id), "supports_turn_content", None)
        return bool(check(workflow_id)) if check else False

    def records_reasoning(self, workflow_id: str) -> bool:
        # Per SESSION, not per merged view: the same tab can show a Claude turn with no
        # reasoning beside an OpenCode one that has it, and one blanket answer would be
        # wrong for whichever backend it did not describe.
        return bool(getattr(self._owner.get(workflow_id), "records_reasoning", False))

    def message_timeline_all(self) -> dict:
        # Export merges available batch paths; its caller handles per-session fallbacks.
        out: dict = {}
        for store in self.stores:
            fn = getattr(store, "message_timeline_all", None)
            if fn:
                out.update(fn())
        return out

    def context_breakdown(self, workflow_id: str) -> list:
        owner = self._owner.get(workflow_id)
        fetch = getattr(owner, "context_breakdown", None)
        return fetch(workflow_id) if fetch else []

    def supports_context(self, workflow_id: str) -> bool:
        check = getattr(self._owner.get(workflow_id), "supports_context", None)
        return bool(check(workflow_id)) if check else False

    def supports_context_curve(self, workflow_id: str) -> bool:
        # Absent an explicit curve gate, Turns support is the default.
        owner = self._owner.get(workflow_id)
        check = getattr(owner, "supports_context_curve", None)
        if check is not None:
            return bool(check(workflow_id))
        turns = getattr(owner, "supports_turns", None)
        return bool(turns(workflow_id)) if turns else False
