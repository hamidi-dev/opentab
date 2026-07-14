"""Per-session notes — ~/.config/opentab/notes.json.

This is the one file opentab writes that it cannot rebuild. Everything else it
persists is *derived* — the rollup cache re-parses, prices.json re-fetches,
state.json is just prefs and is rewritten wholesale on every quit — but a note is
authored, and nothing can recover it. That single difference sets the rules here:

- Notes live in their **own file**, never inside state.json (which any
  --no-state run or a botched pref would happily clobber).
- The file is written **on each edit**, not at exit, and atomically (temp +
  os.replace), so a crash or a full disk can't truncate the file that holds
  every other note.
- A note whose session id is no longer in view is **kept, never pruned**. Ids
  vanish for boring reasons — a transcript rotated away, a source you didn't
  merge in this run — and none of them are a reason to delete what you wrote.

Keyed by session id, which is globally unique across backends (the same property
CombinedStore's routing relies on), so one flat map serves every source.
"""
from __future__ import annotations

import contextlib
import json
import os

try:
    import fcntl  # POSIX advisory locks; native Windows has none
except ImportError:
    fcntl = None

NOTES_VERSION = 1


def notes_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "opentab", "notes.json")


@contextlib.contextmanager
def _locked():
    """Hold an exclusive lock across one read-modify-write.

    Merging on write is what stops a *slow* collision (two opentabs, minutes apart); it
    does nothing for a fast one — both read `{}`, both save, and the second replaces the
    first. The window is narrow (a human types the note) but the stake is an authored
    note, so take a real lock. On a separate lockfile, because the notes file is replaced
    (new inode) on every save, which a lock held on it would not survive.

    Best effort by design: no lock (native Windows) or an unlockable config dir must not
    stop you writing a note — it only leaves the millisecond race the merge already
    narrows.
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
            pass  # a filesystem with no advisory locking (some NFS/9p): write anyway
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


def _read_raw() -> tuple[dict, bool]:
    """(the notes mapping exactly as stored, readable). Values are NOT validated here.

    `readable` is False only when a file IS there and we could not make sense of it —
    unreadable (permissions), truncated, or not our shape. That distinction is the
    difference between "you have no notes yet" and "your notes are right there and I
    can't see them", and only one of those may be overwritten: an absent file is
    readable-and-empty, a broken one must stop the next save cold, or a single `n` would
    replace a file full of notes with a file holding exactly one.

    The mapping comes back raw so that a save can write back entries this version
    doesn't understand (a hand-edit, a newer opentab) instead of quietly dropping them.
    """
    path = notes_path()
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
    # What the UI can actually show: string keys, non-empty string values.
    return {
        key: value
        for key, value in notes.items()
        if isinstance(key, str) and isinstance(value, str) and key and value
    }


def read_notes() -> tuple[dict[str, str], bool]:
    """(notes the UI can show, readable). See _read_raw for what `readable` means."""
    notes, readable = _read_raw()
    return _valid(notes), readable


def load_notes() -> dict[str, str]:
    """The saved {session id: note} map ({} when there's nothing readable to load)."""
    return read_notes()[0]


def update_note(session_id: str, text: str) -> tuple[dict[str, str], str]:
    """Set (or, with empty text, remove) one note. Returns (the map now on disk, error).

    Read-modify-write under a lock, deliberately: the file is re-read on every edit and
    the change merged into what's actually there. Two opentabs are a normal thing to have
    open, and the obvious "write my in-memory map" would let each one's save silently
    delete every note the other made since it started. Errors: "unreadable" (a broken
    file we refuse to clobber) or "unwritable" — in both cases nothing was written.
    """
    with _locked():
        notes, readable = _read_raw()
        if not readable:
            return {}, "unreadable"
        if text:
            notes[session_id] = text
        else:
            notes.pop(session_id, None)
        if not save_notes(notes):
            return _valid(notes), "unwritable"
        return _valid(notes), ""


def save_notes(notes: dict) -> bool:
    """Write the whole map, atomically. False on any OS error (the caller says so).

    Entries are written back as given — including any this version wouldn't display (a
    hand-edit, a newer opentab's shape). Dropping what we don't understand from a file of
    authored data is still deleting someone's writing.
    """
    path = notes_path()
    payload = {
        "version": NOTES_VERSION,
        # Sorted + indented: this file is small, user-authored, and the kind of
        # thing you end up reading (or diffing in a dotfiles repo) by hand.
        # `!= ""` and not `if notes[key]`: an empty string is a note we cleared, but
        # every other falsy value (null, 0, false, {}) is a foreign entry we merely
        # don't understand -- and truthiness is not a licence to delete it.
        "notes": {key: notes[key] for key in sorted(notes, key=str) if notes[key] != ""},
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        # ValueError covers UnicodeEncodeError: JSON will happily *load* an escaped lone
        # surrogate that a UTF-8 stream then can't write. Fail the save (the caller says
        # so) and take the temp file with us -- never a half-written note left behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True
