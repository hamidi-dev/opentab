"""Authored per-session notes in ``$XDG_DATA_HOME/opentab/notes.json``.

Notes are unrebuildable: keep them separate from regenerable state, write atomically
on every edit, preserve unknown entries, and never prune vanished session ids.
"""
from __future__ import annotations

import contextlib
import json
import os

from opentab import paths

try:
    import fcntl  # POSIX advisory locks; native Windows has none
except ImportError:
    fcntl = None

NOTES_VERSION = 1


def notes_path(migrate: bool = True) -> str:
    # migrate=False lets doctor inspect authored data without moving it.
    target = os.path.join(paths.data_dir(), "notes.json")
    return paths.migrated(target) if migrate else paths.resolved(target)


@contextlib.contextmanager
def _locked():
    """Lock a read-modify-write using a stable sidecar inode.

    Locking is best effort where ``fcntl`` or advisory filesystem locks are unavailable.
    """
    if fcntl is None:
        yield
        return
    path = notes_path() + ".lock"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "w")
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


def _read_raw(path: str | None = None) -> tuple[dict, bool]:
    """Read raw entries without dropping shapes this version does not understand.

    An absent file is readable and empty; an existing unreadable or malformed file is
    not, so the writer can refuse to overwrite authored data. ``path`` lets doctor use
    the same verdict without triggering migration.
    """
    path = path or notes_path()
    if not os.path.exists(path):
        return {}, True
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}, False
    notes = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(notes, dict):
        return {}, False
    return notes, True


def _valid(notes: dict) -> dict[str, str]:
    return {
        key: value
        for key, value in notes.items()
        if isinstance(key, str) and isinstance(value, str) and key and value
    }


def read_notes(path: str | None = None) -> tuple[dict[str, str], bool]:
    """Return displayable notes and whether the underlying file is safe to update."""
    notes, readable = _read_raw(path)
    return _valid(notes), readable


def load_notes() -> dict[str, str]:
    return read_notes()[0]


def update_note(
    session_id: str, text: str, *, qualified_id: str | None = None
) -> tuple[dict[str, str], str]:
    """Merge one edit under lock so concurrent opentabs cannot erase each other's notes.

    Supply qualified_id only when both IDs safely identify the same session. Prefer
    its displayable note, matching read_notes; clearing also removes text at the
    other alias, but preserves unknown shapes there. Without it, edit only session_id.
    """
    with _locked():
        notes, readable = _read_raw()
        if not readable:
            return {}, "unreadable"
        target = session_id
        if qualified_id is not None:
            qualified = notes.get(qualified_id)
            if isinstance(qualified, str) and qualified:
                target = qualified_id
        if text:
            notes[target] = text
        else:
            notes.pop(target, None)
            if qualified_id is not None:
                other = session_id if target == qualified_id else qualified_id
                if isinstance(notes.get(other), str):
                    notes.pop(other)
        if not save_notes(notes):
            return _valid(notes), "unwritable"
        return _valid(notes), ""


def save_notes(notes: dict) -> bool:
    """Atomically write every entry, including shapes this version cannot display."""
    path = notes_path()
    payload = {
        "version": NOTES_VERSION,
        # Only an empty string means deletion; preserve all unknown falsy values.
        "notes": {key: notes[key] for key in sorted(notes, key=str) if notes[key] != ""},
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        # UnicodeEncodeError is a ValueError; remove any partial temporary file.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True
