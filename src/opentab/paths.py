"""XDG Base Directory resolution for opentab's *own* files.

Everything opentab writes for itself lands under an ``opentab/`` folder in the
matching XDG base dir — honoring the env override, else the spec default:

    config   $XDG_CONFIG_HOME   ~/.config        keymap.conf, launcher, remotes.json
    state    $XDG_STATE_HOME    ~/.local/state   state.json  (regenerable UI prefs)
    data     $XDG_DATA_HOME     ~/.local/share   notes.json  (authored, never pruned)
    cache    $XDG_CACHE_HOME    ~/.cache         warm-start cache, prices.json, remotes/

Before the split every one of these lived under ``$XDG_CONFIG_HOME/opentab`` — the
"everything in config" layout XDG exists to avoid. state.json and notes.json migrate on
first read via :func:`migrated`; the caches are relocated once at startup by
:func:`migrate_legacy_caches`, which also leaves the old config dir tidy.

Other tools' files opentab merely *reads* (claude, codex, opencode, …) follow each
tool's own convention and are resolved in their store modules, never here.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile

_APP = "opentab"


def _base(env: str, default: str) -> str:
    # An XDG base dir: the env override, else the spec default. Per the spec a value
    # that isn't an absolute path "should be considered invalid and ignored", so a
    # relative (or empty) $XDG_*_HOME falls back rather than resolving against the CWD.
    value = os.environ.get(env)
    return value if value and os.path.isabs(value) else os.path.expanduser(default)


def config_dir() -> str:
    return os.path.join(_base("XDG_CONFIG_HOME", "~/.config"), _APP)


def state_dir() -> str:
    return os.path.join(_base("XDG_STATE_HOME", "~/.local/state"), _APP)


def data_dir() -> str:
    return os.path.join(_base("XDG_DATA_HOME", "~/.local/share"), _APP)


def cache_dir() -> str:
    return os.path.join(_base("XDG_CACHE_HOME", "~/.cache"), _APP)


def _atomic_copy(src: str, dst: str) -> None:
    # Copy across filesystems so `dst` only ever appears whole *and* is never clobbered:
    # write a sibling temp file, then publish it with os.link, which creates `dst`
    # atomically and fails (FileExistsError) if another migrator already did. A racing
    # process therefore can't overwrite a `dst` the winner has since edited. A copy that
    # dies partway (ENOSPC, a kill, Ctrl-C) leaves only the temp — always removed in the
    # finally — never a truncated `dst` a later run would mistake for the migrated file.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".migrate-")
    try:
        os.close(fd)
        shutil.copy2(src, tmp)
        try:
            os.link(tmp, dst)  # atomic exclusive publish (same dir -> same filesystem)
        except FileExistsError:
            pass  # another migrator won the race; our copy is redundant, drop it
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


# Regenerable caches that moved from the old config dir to the XDG cache dir in the
# split. Unlike state/notes they migrate once at startup (see migrate_legacy_caches),
# not lazily in a path getter -- those getters render --help text and must not touch disk.
_LEGACY_CACHE_NAMES = ("cache", "prices.json", "remotes")


def _remove(path: str) -> None:
    # Delete a file or a directory tree, tolerating a missing path (best-effort cleanup).
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            os.remove(path)


def migrate_legacy_caches() -> None:
    """One-time relocation of the regenerable caches out of the old config dir.

    Moves cache/, prices.json and remotes/ from config_dir() to cache_dir() — preserving a
    warm start, fetched prices, and pulled remote summaries — when the new location is
    still empty; if the new copy already exists (a newer run already took over), the stale
    legacy one is simply removed. Also clears the orphaned notes.json.lock the pre-split
    notes writer left behind, and a superseded config/state.json an old run may have
    rewritten during the upgrade. A no-op once done. Best-effort: any failure leaves the
    legacy copy in place, which is harmless since it regenerates. Call once at startup — not
    under --demo, and never from a path getter (it touches the disk; getters render --help).
    """
    cfg, cache = config_dir(), cache_dir()
    if os.path.abspath(cfg) == os.path.abspath(cache):
        return  # the two roots collapsed onto one dir (unusual): nothing to move
    for name in _LEGACY_CACHE_NAMES:
        legacy, new = os.path.join(cfg, name), os.path.join(cache, name)
        if not os.path.exists(legacy):
            continue
        try:
            if os.path.exists(new):
                _remove(legacy)  # the new location already owns this cache; drop the orphan
            else:
                os.makedirs(cache, exist_ok=True)
                shutil.move(legacy, new)  # same-fs rename or cross-fs copy; drops the legacy
        except OSError:
            pass  # regenerable — leave the legacy copy in place on any failure
    _remove(os.path.join(cfg, "notes.json.lock"))  # stale lock for the now-migrated notes
    # A pre-split opentab run during the upgrade window can rewrite state.json back into
    # config; once the authoritative copy lives in the state dir, that config one is a
    # superseded orphan — drop it. (notes.json is deliberately NOT swept here: a transition
    # run could have authored a note only the config copy holds, and a note is the one
    # thing we never risk deleting — migrated() folds it in only when the new copy is absent.)
    if os.path.exists(os.path.join(state_dir(), "state.json")):
        _remove(os.path.join(cfg, "state.json"))


def migrated(new_path: str) -> str:
    """Path for a file that moved out of the old config dir in the XDG split.

    Returns the path opentab should read/write, relocating a pre-split file — the same
    basename under ``config_dir()`` — to ``new_path`` when only the legacy copy exists.
    The move is atomic (``os.replace``, or an atomic cross-device copy), and the result
    is always re-resolved from what is actually on disk afterwards, so a concurrent
    first-run migration, a vanished legacy, or a failed copy can never make this return a
    partial or missing file over intact data. Idempotent. Best-effort: if nothing can be
    moved, it falls back to whichever copy still exists (new preferred). Call only at
    genuine read/write time — never to render help text — since it can touch the disk.
    """
    if os.path.exists(new_path):
        return new_path
    legacy = os.path.join(config_dir(), os.path.basename(new_path))
    if legacy == new_path or not os.path.exists(legacy):
        return new_path
    try:
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.replace(legacy, new_path)  # atomic within a filesystem (the common case)
    except OSError:
        # os.replace failed: a different filesystem, or another process already claimed
        # the legacy file. Try an atomic cross-device copy; swallow any error and let the
        # re-resolve below decide from the ground truth rather than trusting a stale path.
        with contextlib.suppress(OSError):
            _atomic_copy(legacy, new_path)
    # Re-resolve: prefer the new location (a concurrent migrator may have just created
    # it), then the legacy if it survived, so we never hand back a path that a race or a
    # partial copy left inconsistent.
    if os.path.exists(new_path):
        return new_path
    return legacy if os.path.exists(legacy) else new_path
