"""Revisioned authored work items; canonical membership is stored only once.

No harness access. Missing roots and unknown fields survive every transaction.
Native Windows is read-only: atomic replace alone cannot serialize writers.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from opentab.accounting.models import SessionRef
from opentab.persistence import paths

try:
    import fcntl
except ImportError:
    fcntl = None

MAX_ITEMS = 5000
MAX_MEMBERSHIPS = 100000
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_TITLE_CHARS = 200
MAX_NEXT_STEP_CHARS = 2000


class WorkItemError(Exception):
    def __init__(self, code: str, message: str, current: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.current = code, message, current


@dataclass(frozen=True)
class WorkItem:
    id: str
    title: str
    state_id: str
    next_step: str
    revision: int
    archived: bool


@dataclass(frozen=True)
class Membership:
    item_id: str | None = None
    revision: int = 0


@dataclass(frozen=True)
class WorkItemSnapshot:
    revision: int = 0
    items: Mapping[str, WorkItem] = field(default_factory=dict)
    memberships: Mapping[str, Membership] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "items", MappingProxyType(dict(self.items)))
        object.__setattr__(self, "memberships", MappingProxyType(dict(self.memberships)))

    def membership(self, key: str) -> Membership:
        return self.memberships.get(key, Membership())

    def keys_of(self, item_id: str) -> tuple[str, ...]:
        return tuple(key for key, entry in self.memberships.items() if entry.item_id == item_id)


EMPTY = WorkItemSnapshot()


def items_path() -> str:
    return os.path.join(paths.data_dir(), "work-items.json")


def editing_supported() -> bool:
    return fcntl is not None


def _revision(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkItemError("invalid", "invalid revision")
    return value


def _text(value, limit, empty=False):
    if not isinstance(value, str):
        raise WorkItemError("invalid", "work item fields must be text")
    # Check the original text: stripping first could admit leading/trailing control
    # characters from a hand-edited file, then expose them through the snapshot.
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        raise WorkItemError("invalid", "work item fields must contain printable text")
    value = unicodedata.normalize("NFC", value.strip())
    if (not empty and not value) or len(value) > limit:
        raise WorkItemError("invalid", f"text must be printable and at most {limit} characters")
    return value


def _key(key):
    try:
        if not isinstance(key, str) or SessionRef.decode(key).encode() != key:
            raise ValueError()
    except ValueError as exc:
        raise WorkItemError("invalid_session", "invalid canonical root reference") from exc
    return key


def _snapshot(data):
    items = {
        ident: WorkItem(
            ident, e["title"], e["state_id"], e["next_step"], e["revision"], e["archived"]
        )
        for ident, e in data["items"].items()
    }
    memberships = {
        key: Membership(e["item_id"], e["revision"]) for key, e in data["memberships"].items()
    }
    return WorkItemSnapshot(data["revision"], items, memberships)


def _read(path=None):
    try:
        with open(path or items_path(), encoding="utf-8") as fh:
            if os.fstat(fh.fileno()).st_size > MAX_DOCUMENT_BYTES:
                raise WorkItemError("too_large", "work-items.json exceeds 8 MiB")
            data = json.load(fh)
    except FileNotFoundError:
        return {"version": 1, "revision": 0, "items": {}, "memberships": {}}
    except (OSError, ValueError) as exc:
        raise WorkItemError("unreadable", f"cannot read work-items.json: {exc}") from exc
    try:
        if not isinstance(data, dict) or _revision(data.get("version")) < 1:
            raise ValueError()
        if data["version"] > 1:
            raise WorkItemError("newer_version", "work items use a newer format — upgrade OpenTab")
        _revision(data["revision"])
        if not isinstance(data["items"], dict) or not isinstance(data["memberships"], dict):
            raise ValueError()
        if len(data["items"]) > MAX_ITEMS or len(data["memberships"]) > MAX_MEMBERSHIPS:
            raise WorkItemError("too_large", "work item record limit exceeded")
        for ident, entry in data["items"].items():
            _text(ident, 80)
            _text(entry["title"], MAX_TITLE_CHARS)
            _text(entry["state_id"], 40)
            _text(entry["next_step"], MAX_NEXT_STEP_CHARS, True)
            _revision(entry["revision"])
            if not isinstance(entry["archived"], bool):
                raise ValueError()
        for key, entry in data["memberships"].items():
            _key(key)
            _revision(entry["revision"])
            if entry["item_id"] is not None:
                _text(entry["item_id"], 80)
    except (KeyError, TypeError, ValueError, WorkItemError) as exc:
        if isinstance(exc, WorkItemError) and exc.code in ("newer_version", "too_large"):
            raise
        raise WorkItemError("malformed", "work-items.json contains malformed records") from exc
    return data


def load_snapshot(path=None) -> WorkItemSnapshot:
    return _snapshot(_read(path))


@contextlib.contextmanager
def _locked():
    if not editing_supported():
        raise WorkItemError(
            "unsupported", "work item editing is unavailable on native Windows; use WSL"
        )
    try:
        os.makedirs(os.path.dirname(items_path()), exist_ok=True)
        with open(items_path() + ".lock", "a+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise WorkItemError("unwritable", f"cannot lock work items: {exc}") from exc


def _commit(data):
    data["revision"] += 1
    try:
        text = json.dumps(data, ensure_ascii=False, indent=1)
        size = len(text.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WorkItemError("unwritable", f"cannot serialize work items: {exc}") from exc
    if size > MAX_DOCUMENT_BYTES:
        raise WorkItemError("too_large", "work items would exceed 8 MiB")
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(items_path()),
            prefix=".work-items-",
            delete=False,
        ) as fh:
            name = fh.name
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, items_path())
        name = None
    except OSError as exc:
        raise WorkItemError("unwritable", f"cannot write work items: {exc}") from exc
    finally:
        if name:
            with contextlib.suppress(OSError):
                os.unlink(name)
    return _snapshot(data)


def _membership(data, key, item_id, expected, move):
    key = _key(key)
    _revision(expected)
    if item_id is not None:
        item_id = _text(item_id, 80)
    record = data["memberships"].get(key, {"item_id": None, "revision": 0})
    if record["revision"] != expected:
        raise WorkItemError("stale", "membership changed elsewhere", dict(record))
    if item_id is not None and item_id not in data["items"]:
        raise WorkItemError("unknown_item", "work item does not exist")
    if item_id is not None and record["item_id"] not in (None, item_id) and not move:
        raise WorkItemError(
            "move_required", "session already belongs to another item", dict(record)
        )
    if record["item_id"] == item_id:
        return False
    if key not in data["memberships"] and len(data["memberships"]) >= MAX_MEMBERSHIPS:
        raise WorkItemError("limit", "membership limit reached")
    record.update(item_id=item_id, revision=expected + 1)
    data["memberships"][key] = record
    return True


def create_item(title, state_id, next_step="", *, key=None, expected=0):
    title = _text(title, MAX_TITLE_CHARS)
    state_id = _text(state_id, 40)
    next_step = _text(next_step, MAX_NEXT_STEP_CHARS, True)
    with _locked():
        data = _read()
        if len(data["items"]) >= MAX_ITEMS:
            raise WorkItemError("limit", "work item limit reached")
        ident = "w" + secrets.token_hex(8)
        while ident in data["items"]:
            ident = "w" + secrets.token_hex(8)
        data["items"][ident] = dict(
            title=title, state_id=state_id, next_step=next_step, revision=1, archived=False
        )
        if key is not None:
            _membership(data, key, ident, expected, False)
        return ident, _commit(data)


def edit_item(item_id, expected, **fields):
    item_id = _text(item_id, 80)
    _revision(expected)
    if not fields or set(fields) - {"title", "state_id", "next_step", "archived"}:
        raise WorkItemError("invalid", "choose fixed work item fields to edit")
    for name, value in list(fields.items()):
        if name == "archived":
            if not isinstance(value, bool):
                raise WorkItemError("invalid", "archived must be true or false")
        else:
            fields[name] = _text(
                value,
                {"title": MAX_TITLE_CHARS, "state_id": 40, "next_step": MAX_NEXT_STEP_CHARS}[name],
                name == "next_step",
            )
    with _locked():
        data = _read()
        record = data["items"].get(item_id)
        if record is None:
            raise WorkItemError("unknown_item", "work item does not exist")
        if record["revision"] != expected:
            raise WorkItemError("stale", "work item changed elsewhere", dict(record))
        if all(record[k] == v for k, v in fields.items()):
            return _snapshot(data)
        record.update(fields)
        record["revision"] += 1
        return _commit(data)


def set_membership(key, item_id, expected, *, move=False):
    with _locked():
        data = _read()
        if not _membership(data, key, item_id, expected, move):
            return _snapshot(data)
        return _commit(data)
