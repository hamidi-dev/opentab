"""XDG Base Directory resolution for opentab's own files.

    config   $XDG_CONFIG_HOME   ~/.config        keymap.conf, launcher, remotes.json
    state    $XDG_STATE_HOME    ~/.local/state   state.json  (regenerable UI prefs)
    data     $XDG_DATA_HOME     ~/.local/share   notes.json  (authored, never pruned)
    cache    $XDG_CACHE_HOME    ~/.cache         warm-start cache, prices.json, remotes/

Legacy files under ``$XDG_CONFIG_HOME/opentab`` migrate on first read or startup.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile

_APP = "opentab"


def _base(env: str, default: str) -> str:
    # XDG requires absolute overrides; relative values must not resolve against the CWD.
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
    # Publish cross-filesystem copies atomically without clobbering a concurrent winner.
    # Partial copies remain temporary and are always removed.
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


_LEGACY_CACHE_NAMES = ("cache", "prices.json", "remotes")


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            os.remove(path)


def migrate_legacy_caches() -> None:
    """Relocate regenerable legacy caches at startup, never from path getters.

    Existing destinations win races. Failures leave the regenerable legacy copy intact.
    Authored ``notes.json`` is deliberately excluded.
    """
    cfg, cache = config_dir(), cache_dir()
    if os.path.abspath(cfg) == os.path.abspath(cache):
        return
    for name in _LEGACY_CACHE_NAMES:
        legacy, new = os.path.join(cfg, name), os.path.join(cache, name)
        if not os.path.exists(legacy):
            continue
        try:
            if os.path.exists(new):
                _remove(legacy)
            else:
                os.makedirs(cache, exist_ok=True)
                shutil.move(legacy, new)
        except OSError:
            pass
    _remove(os.path.join(cfg, "notes.json.lock"))
    # Never sweep legacy notes: a transition run may have authored data only there.
    if os.path.exists(os.path.join(state_dir(), "state.json")):
        _remove(os.path.join(cfg, "state.json"))


def resolved(new_path: str) -> str:
    """Resolve a pre-XDG-split file without moving it.

    Doctor uses this path because reporting must not mutate state or authored notes.
    """
    if os.path.exists(new_path):
        return new_path
    legacy = os.path.join(config_dir(), os.path.basename(new_path))
    return legacy if legacy != new_path and os.path.exists(legacy) else new_path


def migrated(new_path: str) -> str:
    """Atomically migrate a legacy file, preferring whichever intact copy remains.

    Call only at read/write time: this path lookup may touch the disk.
    """
    if os.path.exists(new_path):
        return new_path
    legacy = os.path.join(config_dir(), os.path.basename(new_path))
    if legacy == new_path or not os.path.exists(legacy):
        return new_path
    try:
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.replace(legacy, new_path)
    except OSError:
        # Cross-device copy or a concurrent migration; re-resolve from disk afterwards.
        with contextlib.suppress(OSError):
            _atomic_copy(legacy, new_path)
    if os.path.exists(new_path):
        return new_path
    return legacy if os.path.exists(legacy) else new_path
