"""Authored session tags in ``$XDG_DATA_HOME/opentab/session-tags.json``.

Tags are unrebuildable user data, like notes, but every mutation is a locked,
revision-checked read-modify-write. Definitions have stable opaque IDs so renames do
not rewrite memberships; memberships are keyed by canonical ``SessionRef`` strings
and are never pruned when a session disappears. Clearing a session's last tag keeps
its record as a revision tombstone so a stale editor cannot resurrect old tags.

Unknown fields at every level survive a write. An unreadable, malformed or newer
document is reported and never overwritten. Native Windows has no stdlib advisory
lock here, so editing is refused there rather than run unlocked.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from opentab.accounting.models import SessionRef
from opentab.persistence import paths

try:
    import fcntl  # POSIX advisory locks; native Windows has none
except ImportError:
    fcntl = None

# Whitespace that Python and browser JavaScript agree on (their \s classes differ).
WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
_TOKEN_SEPARATORS = re.compile(f"[{WHITESPACE}_]+")
_EDGE_SPACES = re.compile(f"^[{WHITESPACE}]+|[{WHITESPACE}]+$")

TAGS_VERSION = 1
MAX_LABEL_CHARS = 40
MAX_TAGS_PER_SESSION = 32
MAX_DEFINITIONS = 1000
MAX_CREATE_PER_EDIT = 16
MAX_BULK_SESSIONS = 500
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


class TagError(Exception):
    """A refused tag operation; ``code`` is stable, ``current`` carries stale truth."""

    def __init__(self, code: str, message: str, current: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.current = current


def tags_path() -> str:
    return os.path.join(paths.data_dir(), "session-tags.json")


def editing_supported() -> bool:
    return fcntl is not None


def normalize_label(text: str) -> str:
    """Return the stored display spelling or raise ``TagError("invalid_label")``.

    A leading ``#`` is presentation syntax. A label is one word, because ``#label``
    tokens in the ``/`` filter end at whitespace; labels saved with spaces by older
    builds still load and match with ``-`` in place of each space. Control and format
    characters are refused, and the 40-character bound applies after NFC.
    """
    if not isinstance(text, str):
        raise TagError("invalid_label", "tag names must be text")
    label = unicodedata.normalize("NFC", text.strip().lstrip("#").strip())
    if not label:
        raise TagError("invalid_label", "tag names cannot be empty")
    if any(unicodedata.category(ch).startswith("C") for ch in label):
        raise TagError("invalid_label", "tag names cannot contain control characters")
    if any(ch.isspace() or ch in "#|" for ch in label):
        raise TagError("invalid_label", "tag names are one word: use - instead of spaces")
    if label_key(label) == "untagged":
        raise TagError("invalid_label", "#untagged is reserved for sessions without tags")
    if len(label) > MAX_LABEL_CHARS:
        raise TagError("invalid_label", f"tag names are limited to {MAX_LABEL_CHARS} characters")
    return label


def label_key(label: str) -> str:
    """Case-insensitive identity, which is also how ``#label`` is typed in a filter.

    Spaces and ``_`` read as ``-``, so a legacy ``client x`` is ``#client-x`` and a new
    ``client_x`` is a duplicate of ``client-x``. ``lower()`` rather than ``casefold()``
    keeps the browser (``toLowerCase``) matching exactly the same names.
    """
    trimmed = _EDGE_SPACES.sub("", unicodedata.normalize("NFC", label))
    folded = unicodedata.normalize("NFC", trimmed.lower())
    return _TOKEN_SEPARATORS.sub("-", folded)


def _nonnegative_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class TagSnapshot:
    """An immutable, display-safe view of one read of the document."""

    revision: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    tag_revisions: dict[str, int] = field(default_factory=dict)
    # Canonical session key -> (tag IDs in display order, record revision).
    assignments: dict[str, tuple[tuple[str, ...], int]] = field(default_factory=dict)

    def tags_of(self, key: str) -> tuple[str, ...]:
        entry = self.assignments.get(key)
        return entry[0] if entry else ()

    def revision_of(self, key: str) -> int:
        entry = self.assignments.get(key)
        return entry[1] if entry else 0

    def label(self, tag_id: str) -> str | None:
        return self.labels.get(tag_id)


EMPTY = TagSnapshot()


def _empty_document() -> dict:
    return {"version": TAGS_VERSION, "revision": 0, "tags": {}, "assignments": {}}


def _read_document(path: str | None = None) -> dict:
    """Read the envelope; absence is empty, anything doubtful raises."""
    path = path or tags_path()
    try:
        with open(path, encoding="utf-8") as fh:
            if os.fstat(fh.fileno()).st_size > MAX_DOCUMENT_BYTES:
                raise TagError("too_large", "session-tags.json exceeds 8 MiB")
            data = json.load(fh)
    except FileNotFoundError:
        return _empty_document()
    except (OSError, ValueError) as exc:
        raise TagError("unreadable", f"cannot read session-tags.json: {exc}") from exc
    if not isinstance(data, dict):
        raise TagError("malformed", "session-tags.json must contain an object")
    version = data.get("version")
    if not _nonnegative_int(version) or version < 1:
        raise TagError("malformed", "session-tags.json has no valid version")
    if version > TAGS_VERSION:
        raise TagError("newer_version", "session tags use a newer format — upgrade OpenTab")
    if (
        not isinstance(data.get("tags", {}), dict)
        or not isinstance(data.get("assignments", {}), dict)
        or not _nonnegative_int(data.get("revision", 0))
    ):
        raise TagError("malformed", "session-tags.json is malformed")
    data.setdefault("revision", 0)
    data.setdefault("tags", {})
    data.setdefault("assignments", {})
    return data


def _valid_definition(entry) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("label"), str)
        and bool(entry["label"])
        and _nonnegative_int(entry.get("revision", 0))
        and entry.get("deleted", False) is False
    )


def _valid_assignment(entry) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("tag_ids"), list)
        and all(isinstance(tag_id, str) and tag_id for tag_id in entry["tag_ids"])
        and _nonnegative_int(entry.get("revision"))
    )


def _snapshot(document: dict) -> TagSnapshot:
    # Invalid records are hidden from display but stay on disk untouched.
    labels, revisions = {}, {}
    for tag_id, entry in document["tags"].items():
        if (
            isinstance(tag_id, str)
            and isinstance(entry, dict)
            and entry.get("deleted") is True
            and _nonnegative_int(entry.get("revision"))
        ):
            revisions[tag_id] = entry["revision"]
        if isinstance(tag_id, str) and tag_id and _valid_definition(entry):
            # Hand-edited labels may predate normalization; never display controls.
            labels[tag_id] = "".join(
                "�" if unicodedata.category(ch).startswith("C") else ch for ch in entry["label"]
            )
            revisions[tag_id] = entry.get("revision", 0)
    assignments = {}
    for key, entry in document["assignments"].items():
        if isinstance(key, str) and _valid_assignment(entry):
            ids = tuple(dict.fromkeys(entry["tag_ids"]))
            assignments[key] = (ids, entry["revision"])
    return TagSnapshot(document["revision"], labels, revisions, assignments)


def load_snapshot(path: str | None = None) -> TagSnapshot:
    """Read once for display; raises ``TagError`` rather than inventing emptiness."""
    return _snapshot(_read_document(path))


@contextlib.contextmanager
def _locked():
    if fcntl is None:
        raise TagError("unsupported", "tag editing is unavailable on native Windows; use WSL")
    path = tags_path() + ".lock"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "a+b")
    except OSError as exc:
        raise TagError("unwritable", f"cannot lock session tags: {exc}") from exc
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            # Never claim concurrent-edit safety while running unlocked.
            raise TagError("unwritable", f"cannot lock session tags: {exc}") from exc
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _write_document(document: dict) -> None:
    path = tags_path()
    try:
        text = json.dumps(document, ensure_ascii=False, indent=1, sort_keys=False)
    except (TypeError, ValueError) as exc:
        raise TagError("unwritable", f"cannot serialize session tags: {exc}") from exc
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise TagError("too_large", "session tags would exceed 8 MiB")
    name = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(path),
            prefix=".session-tags-",
            suffix=".tmp",
            delete=False,
        ) as fh:
            name = fh.name
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
        name = None
    except (OSError, ValueError) as exc:
        raise TagError("unwritable", f"cannot write session tags: {exc}") from exc
    finally:
        if name is not None:
            with contextlib.suppress(OSError):
                os.unlink(name)


def _canonical_key(key: str) -> str:
    try:
        if SessionRef.decode(key).encode() != key:
            raise ValueError("noncanonical")
    except ValueError as exc:
        raise TagError("invalid_session", "invalid session reference") from exc
    return key


def _new_tag_id(existing) -> str:
    while True:
        tag_id = "t" + secrets.token_hex(6)
        if tag_id not in existing:
            return tag_id


def _label_index(document: dict) -> dict[str, str]:
    """Key -> tag ID. Older builds could store two labels that now share a key
    (``client_x`` and ``client-x``); there an exact spelling wins, see ``_owner``."""
    return {
        label_key(entry["label"]): tag_id
        for tag_id, entry in document["tags"].items()
        if tag_id and _valid_definition(entry)
    }


def _owner(document: dict, index: dict[str, str], label: str) -> str | None:
    for tag_id, entry in document["tags"].items():
        # An empty ID (hand-edited file) would write a record that no longer validates.
        if tag_id and _valid_definition(entry) and entry["label"] == label:
            return tag_id
    return index.get(label_key(label))


def _define(document: dict, label: str) -> str:
    if len(document["tags"]) >= MAX_DEFINITIONS:
        raise TagError("limit", f"at most {MAX_DEFINITIONS} tags can be defined")
    tag_id = _new_tag_id(document["tags"])
    document["tags"][tag_id] = {"label": label, "revision": 1}
    return tag_id


def _commit(document: dict) -> TagSnapshot:
    document["revision"] = document["revision"] + 1
    _write_document(document)
    return _snapshot(document)


def create_tag(label: str) -> tuple[str, TagSnapshot]:
    """Define one tag; a case-insensitive duplicate is refused, not merged."""
    label = normalize_label(label)
    with _locked():
        document = _read_document()
        if label_key(label) in _label_index(document):
            raise TagError("duplicate", f"a tag named #{label} already exists")
        tag_id = _define(document, label)
        return tag_id, _commit(document)


def rename_tag(tag_id: str, label: str, expected: int) -> TagSnapshot:
    """Rename by stable ID; memberships are untouched."""
    label = normalize_label(label)
    if not _nonnegative_int(expected):
        raise TagError("invalid", "invalid expected revision")
    with _locked():
        document = _read_document()
        entry = document["tags"].get(tag_id)
        if not _valid_definition(entry):
            raise TagError("unknown_tag", "that tag no longer exists")
        if entry.get("revision", 0) != expected:
            raise TagError(
                "stale",
                "the tag changed elsewhere",
                {"label": entry["label"], "revision": entry.get("revision", 0)},
            )
        owner = _owner(document, _label_index(document), label)
        if owner is not None and owner != tag_id:
            raise TagError("duplicate", f"a tag named #{label} already exists")
        if entry["label"] == label:
            return _snapshot(document)
        entry["label"] = label
        entry["revision"] = expected + 1
        return _commit(document)


def bulk_update_session_tags(
    targets: dict[str, int], *, add: Iterable[str] = (), remove: Iterable[str] = ()
) -> TagSnapshot:
    """Patch exact roots atomically; all stale records are reported in ``conflicts``.

    Independent roots can merge despite a changed document revision. No target is
    written unless every target and its resulting membership pass validation.
    """
    if not isinstance(targets, dict):
        raise TagError("invalid", "targets must map session keys to revisions")
    targets = dict(targets)
    if len(targets) > MAX_BULK_SESSIONS:
        raise TagError("limit", f"at most {MAX_BULK_SESSIONS} sessions per bulk edit")
    for key, expected in targets.items():
        if not isinstance(key, str):
            raise TagError("invalid_session", "invalid session reference")
        _canonical_key(key)
        if not _nonnegative_int(expected):
            raise TagError("invalid", "invalid expected revision")
    add, remove = list(add), list(remove)
    if any(not isinstance(t, str) or not t for t in [*add, *remove]):
        raise TagError("invalid", "tag IDs must be nonempty text")
    add, remove = list(dict.fromkeys(add)), set(remove)
    with _locked():
        document = _read_document()
        assignments = document["assignments"]
        conflicts = {}
        malformed = {}
        for key, expected in targets.items():
            record = assignments.get(key)
            if record is not None and not _valid_assignment(record):
                malformed[key] = {"code": "malformed_record"}
                continue
            revision = record["revision"] if record else 0
            if revision != expected:
                conflicts[key] = {
                    "tag_ids": list(dict.fromkeys(record["tag_ids"])) if record else [],
                    "revision": revision,
                }
        if conflicts or malformed:
            raise TagError(
                "stale" if conflicts else "malformed_record",
                "bulk edit refused: target records changed or are malformed",
                {"conflicts": {**conflicts, **malformed}},
            )
        known = {t for t, entry in document["tags"].items() if _valid_definition(entry)}
        changes = {}
        for key in targets:
            record = assignments.get(key)
            current = list(dict.fromkeys(record["tag_ids"])) if record else []
            if any(t not in known and t not in current for t in add):
                raise TagError("unknown_tag", "a selected tag no longer exists")
            wanted = [t for t in dict.fromkeys([*current, *add]) if t not in remove]
            if len(wanted) > MAX_TAGS_PER_SESSION:
                raise TagError("limit", f"at most {MAX_TAGS_PER_SESSION} tags per session")
            if wanted != current:
                entry = dict(record) if record else {}
                entry.update(tag_ids=wanted, revision=targets[key] + 1)
                changes[key] = entry
        if not changes:
            return _snapshot(document)
        assignments.update(changes)
        return _commit(document)


def delete_tag(tag_id: str, expected: int, expected_document: int) -> TagSnapshot:
    """Globally remove memberships, including missing roots, after confirmation.

    The document CAS guards the affected-count preview. Definition tombstones keep
    unknown fields and revision history but are excluded from active definitions.
    """
    if not _nonnegative_int(expected) or not _nonnegative_int(expected_document):
        raise TagError("invalid", "invalid expected revision")
    with _locked():
        document = _read_document()
        if document["revision"] != expected_document:
            raise TagError(
                "stale",
                "tags changed since deletion was confirmed",
                {"revision": document["revision"]},
            )
        entry = document["tags"].get(tag_id)
        if not _valid_definition(entry):
            raise TagError("unknown_tag", "that tag no longer exists")
        if entry.get("revision", 0) != expected:
            raise TagError(
                "stale",
                "the tag changed elsewhere",
                {"label": entry["label"], "revision": entry.get("revision", 0)},
            )
        # A malformed record containing the ID cannot safely have its revision
        # advanced. Refuse rather than partially delete or discard authored fields.
        for record in document["assignments"].values():
            if (
                isinstance(record, dict)
                and isinstance(record.get("tag_ids"), list)
                and tag_id in record["tag_ids"]
            ):
                if not _valid_assignment(record):
                    raise TagError("malformed_record", "an affected tag record is malformed")
                record["tag_ids"] = [t for t in record["tag_ids"] if t != tag_id]
                record["revision"] += 1
        entry["deleted"] = True
        entry["revision"] = expected + 1
        return _commit(document)


def update_session_tags(
    key: str,
    expected: int,
    *,
    replace: Iterable[str] | None = None,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    create: Iterable[str] = (),
) -> TagSnapshot:
    """Change one session's membership in one locked transaction.

    ``expected`` is that session's record revision (0 when it has none), so edits to
    other sessions never conflict. ``create`` defines new labels and assigns them in
    the same write; a label that already exists (case-insensitively) reuses its tag.
    ``replace`` may keep IDs already on the session even when their definition is
    gone, but may only add known IDs.
    """
    key = _canonical_key(key)
    if not _nonnegative_int(expected):
        raise TagError("invalid", "invalid expected revision")
    labels = [normalize_label(text) for text in create]
    if len(labels) > MAX_CREATE_PER_EDIT:
        raise TagError("limit", f"at most {MAX_CREATE_PER_EDIT} new tags per edit")
    replace = None if replace is None else list(replace)
    add, remove = list(add), set(remove)
    with _locked():
        document = _read_document()
        assignments = document["assignments"]
        record = assignments.get(key)
        if record is not None and not _valid_assignment(record):
            raise TagError("malformed_record", "this session's tag record is malformed")
        current = list(dict.fromkeys(record["tag_ids"])) if record else []
        revision = record["revision"] if record else 0
        if revision != expected:
            raise TagError(
                "stale", "tags changed elsewhere", {"tag_ids": current, "revision": revision}
            )
        known = {tag_id for tag_id, entry in document["tags"].items() if _valid_definition(entry)}
        wanted = current if replace is None else list(dict.fromkeys(replace))
        for tag_id in [*wanted, *add]:
            if tag_id not in known and tag_id not in current:
                raise TagError("unknown_tag", "a selected tag no longer exists")
        wanted = [tag_id for tag_id in dict.fromkeys([*wanted, *add]) if tag_id not in remove]
        index = _label_index(document)
        defined = False
        for label in labels:
            tag_id = _owner(document, index, label)
            if tag_id is None:
                tag_id = index[label_key(label)] = _define(document, label)
                defined = True
            if tag_id not in wanted:
                wanted.append(tag_id)
        if len(wanted) > MAX_TAGS_PER_SESSION:
            raise TagError("limit", f"at most {MAX_TAGS_PER_SESSION} tags per session")
        if wanted == current and not defined:
            return _snapshot(document)
        # Patch known fields only; unknown fields on the record survive.
        entry = dict(record) if record else {}
        entry["tag_ids"] = wanted
        entry["revision"] = revision + 1
        assignments[key] = entry
        return _commit(document)
