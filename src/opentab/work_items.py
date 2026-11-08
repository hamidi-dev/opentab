"""Shared opt-in work capability and whole-root accounting, without store reads."""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType

from opentab.persistence import work_items as persistence
from opentab.persistence.work_items import EMPTY, WorkItemError, WorkItemSnapshot
from opentab.persistence.workflow_config import WorkState, load_workflow_config


class WorkItems:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.enabled = False
        self.readable = True
        self.error = ""
        self.states: tuple[WorkState, ...] = ()
        self.snapshot = EMPTY

    def refresh(self, *, allowed: bool | None = None) -> None:
        if allowed is not None:
            self.allowed = allowed
        self.disable()
        # Demo/no-state must not even read custom state labels.
        if not self.allowed:
            return
        config = load_workflow_config()
        # The file only configures work items, so file-level errors are theirs too.
        self.error = config.error
        self.enabled = config.work_items
        self.states = config.states if self.enabled else ()
        if self.enabled:
            try:
                self.snapshot = persistence.load_snapshot()
            except WorkItemError as exc:
                self.readable = False
                self.error = exc.message

    def disable(self) -> None:
        self.enabled, self.readable, self.error = False, True, ""
        self.states, self.snapshot = (), EMPTY

    @property
    def editable(self) -> bool:
        return self.enabled and self.readable and persistence.editing_supported()

    def edit_refusal(self) -> str:
        if not self.enabled:
            return "work items are off"
        if not self.readable:
            return "work-items.json is unreadable; correct it and reload"
        if not persistence.editing_supported():
            return "work item editing is unavailable on native Windows; use WSL"
        return ""

    def _recheck(self):
        config = load_workflow_config() if self.allowed else None
        if config is None or not config.work_items:
            self.disable()
            raise WorkItemError("disabled", "work items are off")
        self.states = config.states
        refusal = self.edit_refusal()
        if refusal:
            raise WorkItemError("unavailable", refusal)

    def _state(self, state_id):
        if not isinstance(state_id, str) or not any(state.id == state_id for state in self.states):
            raise WorkItemError("unknown_state", "choose a configured work state")

    def _save(self, operation, *args, **kwargs):
        try:
            self.snapshot = operation(*args, **kwargs)
        except WorkItemError as exc:
            if exc.code in ("unreadable", "malformed", "newer_version", "too_large"):
                self.readable, self.error = False, exc.message
            raise
        return self.snapshot

    def create(self, title, state_id=None, next_step="", *, key=None, expected=0) -> str:
        self._recheck()
        state_id = self.states[0].id if state_id is None else state_id
        self._state(state_id)

        def operation():
            ident, snapshot = persistence.create_item(
                title, state_id, next_step, key=key, expected=expected
            )
            ids.append(ident)
            return snapshot

        ids = []
        self._save(operation)
        return ids[0]

    def edit(self, item_id, expected, **fields) -> WorkItemSnapshot:
        self._recheck()
        if "state_id" in fields:
            self._state(fields["state_id"])
        return self._save(persistence.edit_item, item_id, expected, **fields)

    def attach(self, key, item_id, expected) -> WorkItemSnapshot:
        self._recheck()
        if item_id is None:
            raise WorkItemError("invalid", "choose an item to attach")
        return self._save(persistence.set_membership, key, item_id, expected)

    def detach(self, key, expected) -> WorkItemSnapshot:
        self._recheck()
        return self._save(persistence.set_membership, key, None, expected)

    def move(self, key, item_id, expected) -> WorkItemSnapshot:
        self._recheck()
        if item_id is None:
            raise WorkItemError("invalid", "choose an item to move to")
        return self._save(persistence.set_membership, key, item_id, expected, move=True)

    def state(self, state_id: str) -> WorkState | None:
        return next((state for state in self.states if state.id == state_id), None)

    def section(self, item_id: str) -> str:
        item = self.snapshot.items[item_id]
        if item.archived:
            return "archived"
        state = self.state(item.state_id)
        return "unknown" if state is None else "done" if state.done else "open"


@dataclass(frozen=True)
class WorkRoot:
    key: str
    cost: float | None = None
    worked_seconds: float | None = None


@dataclass(frozen=True)
class WorkTotals:
    linked: int = 0
    available: int = 0
    unavailable: int = 0
    cost_measured: int = 0
    time_measured: int = 0
    cost: float | None = None
    worked_seconds: float | None = None


def aggregate_work_items(roots: Iterable[WorkRoot], snapshot: WorkItemSnapshot):
    """All-date, whole-root totals; caller applies saved-ignore/source policy.

    Repeated identical roots count once. Conflicting duplicate identities are
    unavailable rather than selecting an arbitrary owner. None means unmeasured,
    including an empty item; a measured zero remains zero. Children must not be
    supplied: inputs are already normalized root rollups including their subagents.
    """
    available, ambiguous = {}, set()
    for root in roots:
        if root.key in available and available[root.key] != root:
            ambiguous.add(root.key)
        available[root.key] = root
    for key in ambiguous:
        available.pop(key, None)
    result = {}
    linked = {ident: [] for ident in snapshot.items}
    for key, membership in snapshot.memberships.items():
        if membership.item_id in linked:
            linked[membership.item_id].append(key)
    for ident in snapshot.items:
        keys = linked[ident]
        rows = [available[key] for key in keys if key in available]
        costs = [
            r.cost for r in rows if r.cost is not None and math.isfinite(r.cost) and r.cost >= 0
        ]
        times = [
            r.worked_seconds
            for r in rows
            if r.worked_seconds is not None
            and math.isfinite(r.worked_seconds)
            and r.worked_seconds >= 0
        ]
        result[ident] = WorkTotals(
            len(keys),
            len(rows),
            len(keys) - len(rows),
            len(costs),
            len(times),
            sum(costs) if costs else None,
            sum(times) if times else None,
        )
    return MappingProxyType(result)
