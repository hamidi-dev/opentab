"""Session tags shared by the TUI and the live browser.

One ``SessionTags`` per frontend holds the switch state and a snapshot read on load,
reload or after a successful edit -- never per painted row. Edits go through the
persistence layer's locked, revision-checked operations and adopt its merged truth.
Stores never see tags: accounting and caches are unaffected by any annotation.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from opentab.accounting.models import SessionRef
from opentab.persistence import session_tags
from opentab.persistence.session_tags import EMPTY, TagError, TagSnapshot
from opentab.persistence.state import tags_enabled

MATCH_ALL = "all"
MATCH_ANY = "any"
UNTAGGED = "untagged"


@dataclass(frozen=True)
class TagFilter:
    """A transient session-list predicate; never persisted with preferences."""

    mode: str = MATCH_ALL
    ids: frozenset[str] = frozenset()

    def matches(self, tag_ids: Iterable[str]) -> bool:
        present = set(tag_ids)
        if self.mode == UNTAGGED:
            return not present
        if self.mode == MATCH_ANY:
            return bool(present & self.ids)
        return self.ids <= present


def session_key(machine: str, harness: str, native_id: str) -> str:
    return SessionRef(machine, harness, native_id).encode()


@dataclass(frozen=True)
class TagAggregate:
    tag_id: str | None
    label: str
    session_keys: tuple[str, ...]
    sessions: int
    cost: float
    cost_measured: int
    agent_seconds: float
    time_measured: int


def aggregate_tags(
    rows: Iterable[tuple[str, float | None, float | None]], snapshot: TagSnapshot
) -> list[TagAggregate]:
    """Project scoped root rollups, with overlapping sets and explicit coverage.

    Callers choose the cost mode and supply whole-root usage (including children).
    First occurrence wins for duplicate qualified roots; missing measurements are
    not zero measurements. Definitions with no scoped roots remain discoverable.
    """
    groups: dict[str | None, list[tuple[str, float | None, float | None]]] = {
        tag_id: [] for tag_id in snapshot.labels
    }
    groups[None] = []
    seen = set()
    for key, cost, seconds in rows:
        if key in seen:
            continue
        seen.add(key)
        for tag_id in dict.fromkeys(snapshot.tags_of(key)) or (None,):
            groups.setdefault(tag_id, []).append((key, cost, seconds))
    result = []
    for tag_id, members in groups.items():
        label = "Untagged" if tag_id is None else snapshot.labels.get(tag_id, f"?{tag_id}")
        result.append(
            TagAggregate(
                tag_id,
                label,
                tuple(row[0] for row in members),
                len(members),
                sum(cost for _, cost, _ in members if cost is not None),
                sum(cost is not None for _, cost, _ in members),
                sum(seconds for _, _, seconds in members if seconds is not None),
                sum(seconds is not None for _, _, seconds in members),
            )
        )
    return sorted(
        result,
        key=lambda row: (
            row.tag_id is None,
            row.tag_id not in snapshot.labels,
            session_tags.label_key(row.label),
            row.tag_id or "",
        ),
    )


class SessionTags:
    """Capability, snapshot and mutations for one frontend.

    ``allowed`` is false under ``--no-state``; callers also gate on live demo mode.
    While disabled nothing is read and no data or lock file is created.
    """

    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.enabled = False
        # Why tags are off or unreadable; shown once on load/reload.
        self.error = ""
        # Broken is not empty: edits stay refused until a reload reads it cleanly.
        self.readable = True
        self.snapshot: TagSnapshot = EMPTY

    def refresh(self, *, allowed: bool | None = None) -> None:
        if allowed is not None:
            self.allowed = allowed
        self.snapshot, self.readable, self.error = EMPTY, True, ""
        if not self.allowed:
            self.enabled = False
            return
        self.enabled = tags_enabled()
        if not self.enabled:
            return
        try:
            self.snapshot = session_tags.load_snapshot()
        except TagError as exc:
            self.readable = False
            self.error = f"tags: {exc.message}"

    def disable(self) -> None:
        self.enabled = False
        self.snapshot, self.readable, self.error = EMPTY, True, ""

    @property
    def editable(self) -> bool:
        return self.enabled and self.readable and session_tags.editing_supported()

    def edit_refusal(self) -> str:
        if not self.enabled:
            return "tags are off"
        if not self.readable:
            return f"{session_tags.tags_path()} is unreadable"
        if not session_tags.editing_supported():
            return "tag editing is unavailable on native Windows; use WSL"
        return ""

    # -- lookups -------------------------------------------------------------------

    def tag_ids(self, key: str) -> tuple[str, ...]:
        return self.snapshot.tags_of(key) if self.enabled else ()

    def revision(self, key: str) -> int:
        return self.snapshot.revision_of(key)

    def label(self, tag_id: str) -> str | None:
        return self.snapshot.label(tag_id)

    def labels(self, key: str) -> list[str]:
        """Display labels, alphabetical; an unresolved ID stays visible, sorted last."""
        known = sorted(
            (label for tag_id in self.tag_ids(key) if (label := self.label(tag_id))),
            key=session_tags.label_key,
        )
        return known + [f"?{t}" for t in self.tag_ids(key) if self.label(t) is None]

    def definitions(self) -> list[tuple[str, str]]:
        return sorted(
            self.snapshot.labels.items(),
            key=lambda item: (session_tags.label_key(item[1]), item[0]),
        )

    def unresolved(self, keys: Iterable[str]) -> list[str]:
        """Assigned IDs with no definition among ``keys``, in first-seen order."""
        out: dict[str, None] = {}
        for key in keys:
            for tag_id in self.tag_ids(key):
                if self.label(tag_id) is None:
                    out[tag_id] = None
        return list(out)

    # -- mutations -----------------------------------------------------------------

    def _recheck(self) -> None:
        # A stale frontend must not keep writing after the switch was turned off.
        if not self.allowed:
            self.disable()
            raise TagError("disabled", "tags are off")
        if not tags_enabled():
            self.disable()
            raise TagError("disabled", "tags are off")
        refusal = self.edit_refusal()
        if refusal:
            raise TagError("unavailable", refusal)

    def set_tags(
        self, key: str, tag_ids: Iterable[str], expected: int, create: Iterable[str] = ()
    ) -> TagSnapshot:
        """Replace one session's tags, creating ``create`` labels in the same write."""
        self._recheck()
        try:
            self.snapshot = session_tags.update_session_tags(
                key, expected, replace=tag_ids, create=create
            )
        except TagError as exc:
            if exc.code in ("unreadable", "malformed", "newer_version", "too_large"):
                self.readable = False
            raise
        return self.snapshot

    def create(self, label: str) -> str:
        self._recheck()
        tag_id, self.snapshot = session_tags.create_tag(label)
        return tag_id

    def rename(self, tag_id: str, label: str, expected: int | None = None) -> TagSnapshot:
        self._recheck()
        self.snapshot = session_tags.rename_tag(
            tag_id,
            label,
            self.snapshot.tag_revisions.get(tag_id, 0) if expected is None else expected,
        )
        return self.snapshot

    def bulk_update(
        self, targets: dict[str, int], *, add: Iterable[str] = (), remove: Iterable[str] = ()
    ) -> TagSnapshot:
        self._recheck()
        try:
            self.snapshot = session_tags.bulk_update_session_tags(targets, add=add, remove=remove)
        except TagError as exc:
            if exc.code in ("unreadable", "malformed", "newer_version", "too_large"):
                self.readable = False
            raise
        return self.snapshot

    def affected_count(self, tag_id: str) -> int:
        return sum(tag_id in ids for ids, _ in self.snapshot.assignments.values())

    def delete(self, tag_id: str, expected: int, expected_document: int) -> TagSnapshot:
        self._recheck()
        try:
            self.snapshot = session_tags.delete_tag(tag_id, expected, expected_document)
        except TagError as exc:
            if exc.code in ("unreadable", "malformed", "newer_version", "too_large"):
                self.readable = False
            raise
        return self.snapshot
