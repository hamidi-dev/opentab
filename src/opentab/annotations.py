"""Session tags shared by the TUI and the live browser.

One ``SessionTags`` per frontend holds the switch state and a snapshot read on load,
reload or after a successful edit -- never per painted row. Edits go through the
persistence layer's locked, revision-checked operations and adopt its merged truth.
Stores never see tags: accounting and caches are unaffected by any annotation.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from opentab.accounting.models import SessionRef
from opentab.persistence import session_tags
from opentab.persistence.session_tags import EMPTY, TagError, TagSnapshot
from opentab.persistence.state import tags_enabled

# A group member meaning "has no tags"; real tag IDs are never empty.
UNTAGGED = ""
_SPACES = re.compile(f"[{session_tags.WHITESPACE}]+")
_TOKEN_SEPARATORS = re.compile(f"[{session_tags.WHITESPACE}_]+")


@dataclass(frozen=True)
class TagFilter:
    """A transient session-list predicate; never persisted with preferences.

    Every group must be satisfied by one of its members (``#a #b`` is two groups,
    ``#a|b`` one). An empty group, from a token naming no tag, matches nothing.
    """

    groups: tuple[frozenset[str], ...] = ()

    def matches(self, tag_ids: Iterable[str]) -> bool:
        present = set(tag_ids)
        return all(present & group or (UNTAGGED in group and not present) for group in self.groups)


def tag_token_key(label: str) -> str:
    """How a label is written after ``#`` in a filter: one key shared with duplicates."""
    return session_tags.label_key(label)


def tag_token(label: str) -> str:
    """The completion spelling of a label, keeping its case."""
    return "#" + _TOKEN_SEPARATORS.sub("-", session_tags._EDGE_SPACES.sub("", label))


@dataclass(frozen=True)
class TagQuery:
    text: str  # the fuzzy part, with tag tokens removed
    filter: TagFilter | None  # None when the query names no tags
    unknown: tuple[str, ...] = ()  # tokens naming no tag; they match nothing
    partial: str | None = None  # the trailing ``#word`` being typed, without ``#``


def parse_tag_query(query: str, labels: dict[str, str]) -> TagQuery:
    """Split ``/`` filter text into fuzzy words and ``#tag`` tokens.

    A token names a tag exactly, else every tag starting with it; ``#untagged`` always
    means no tags. A bare ``#`` is still being typed.
    """
    by_key: dict[str, list[str]] = {}
    for tag_id, label in labels.items():
        by_key.setdefault(tag_token_key(label), []).append(tag_id)
    words, groups, unknown = [], [], []
    tokens = [token for token in _SPACES.split(query) if token]
    for token in tokens:
        if not token.startswith("#"):
            words.append(token)
            continue
        alternatives = [part.lstrip("#") for part in token[1:].split("|")]
        alternatives = [part for part in alternatives if part]
        if not alternatives:
            continue
        group: set[str] = set()
        for part in alternatives:
            key = tag_token_key(part)
            if key == "untagged":  # always synthetic, even over a legacy tag of that name
                group.add(UNTAGGED)
            elif key in by_key:
                group.update(by_key[key])
            else:
                prefixed = [i for k, ids in by_key.items() if k.startswith(key) for i in ids]
                group.update(prefixed)
                if not prefixed:
                    unknown.append(f"#{part}")
        groups.append(frozenset(group))
    partial = None
    if tokens and not _SPACES.match(query[-1]) and tokens[-1].startswith("#"):
        partial = tokens[-1].rsplit("|", 1)[-1].lstrip("#")
    return TagQuery(
        " ".join(words), TagFilter(tuple(groups)) if groups else None, tuple(unknown), partial
    )


def complete_tag_query(query: str, label: str) -> str:
    """Replace the trailing partial token's last alternative with ``label``."""
    gaps = list(_SPACES.finditer(query))
    cut = gaps[-1].end() if gaps else 0
    head, tail = query[:cut], query[cut:]
    token = tag_token(label)
    if "|" in tail:
        token = tail.rsplit("|", 1)[0] + "|" + token[1:]
    return f"{head}{token} "


def rename_tag_in_query(query: str, old_label: str, new_label: str) -> str:
    """Point every ``#old`` alternative at ``new_label``, keeping the rest as typed."""
    old_key, new = tag_token_key(old_label), tag_token(new_label)[1:]
    pieces = re.split(f"([{session_tags.WHITESPACE}]+)", query)
    for i, word in enumerate(pieces):
        if i % 2 == 0 and word.startswith("#"):
            parts = word[1:].split("|")
            pieces[i] = "#" + "|".join(
                new if tag_token_key(part.lstrip("#")) == old_key else part for part in parts
            )
    return "".join(pieces)


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
