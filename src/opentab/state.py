"""Reading/writing the saved-prefs state.json (in $XDG_STATE_HOME/opentab)."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import tempfile
from typing import TYPE_CHECKING

from opentab import paths, themes
from opentab.heatmap import HEAT_MAX_LEVELS, HEAT_MIN_LEVELS

if TYPE_CHECKING:
    from opentab.tui.app import App

try:
    import fcntl  # POSIX advisory locks; native Windows has none
except ImportError:
    fcntl = None


MUTABLE_SET_KEYS = frozenset({"bookmarks", "ignored_projects", "ignored_sessions", "pinned_models"})
SET_OPERATIONS = frozenset({"set-add", "set-remove"})


def state_path(migrate: bool = True) -> str:
    # migrate=False lets doctor inspect preferences without moving them.
    target = os.path.join(paths.state_dir(), "state.json")
    return paths.migrated(target) if migrate else paths.resolved(target)


@contextlib.contextmanager
def _locked(path: str):
    """Lock a read-modify-write using a stable sidecar inode when available."""
    if fcntl is None:
        yield
        return
    lock_path = path + ".lock"
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        handle = open(lock_path, "w")
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


def read_state(path: str | None = None) -> tuple[dict, bool]:
    """Return raw state and whether the file is safe to update.

    A missing file is readable and empty. An existing unreadable, malformed, or
    non-object file is not safe to replace.
    """
    path = path or state_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError):
        return {}, False
    return (data, True) if isinstance(data, dict) else ({}, False)


def load_state(path: str | None = None) -> dict:
    return read_state(path)[0]


def _write_state(data: dict, path: str) -> bool:
    tmp = ""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(path),
            prefix=f".{os.path.basename(path)}.",
            delete=False,
        ) as fh:
            tmp = fh.name
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def save_state(app: App) -> None:
    data = {
        "range": app.range_input_value(),
        "sort_by": app.sort_by,
        "project_sort_by": app.project_sort_by,
        "subagent_sort_by": app.subagent_sort_by,
        "prices_sort": app.prices_sort,
        "trend_sort": app.trend_sort,
        "prices_view": app.prices_view,
        "sort_reverse": app.sort_reverse,
        "project_sort_reverse": app.project_sort_reverse,
        "subagent_sort_reverse": app.subagent_sort_reverse,
        "prices_sort_reverse": app.prices_sort_reverse,
        "trend_sort_reverse": app.trend_sort_reverse,
        "browse_mode": app.browse_mode,
        "focus": app.focus,
        "zoom_maximized": app.zoom_maximized,
        "show_api_prices": app.show_api_prices,
        "source": app.source_key,
        "theme": app.theme_id,
        "cal_levels": app.cal_levels,
        "prices_prompt_dismissed": app.prices_prompt_dismissed,
        "dismissed_startup_warnings": sorted(app.dismissed_startup_warnings),
    }
    local_sets = {key: set(getattr(app, key)) for key in MUTABLE_SET_KEYS}
    # Apps saved without a restore start from empty sets, just like a missing file.
    baseline = getattr(app, "_state_set_baseline", {})
    path = state_path()
    with _locked(path):
        current, readable = read_state(path)
        if not readable:
            return
        for key, local in local_sets.items():
            saved = current.get(key, [])
            if not isinstance(saved, list) or any(
                not isinstance(item, str) or not item for item in saved
            ):
                return  # Do not normalize away malformed authored data.
            original = baseline.get(key, set())
            data[key] = sorted((set(saved) - (original - local)) | (local - original))
        current.update(data)
        if _write_state(current, path):
            # Track what this App saved, not the merged disk sets: importing external
            # edits into the baseline alone would undo them on the next unchanged save.
            app._state_set_baseline = local_sets


def update_state(
    operation: str,
    key: str,
    value: str,
    path: str | None = None,
    *,
    qualified_value: str | None = None,
) -> tuple[dict, str]:
    """Apply one semantic set mutation without replacing unrelated state.

    Supply qualified_value only when both values safely identify the same entry.
    Prefer the qualified alias if present; removal clears both aliases under lock.
    Errors are returned as ``unreadable``, ``unwritable``, or ``invalid operation``.
    """
    if (
        not isinstance(operation, str)
        or operation not in SET_OPERATIONS
        or not isinstance(key, str)
        or key not in MUTABLE_SET_KEYS
        or not isinstance(value, str)
        or not value
        or (
            qualified_value is not None
            and (not isinstance(qualified_value, str) or not qualified_value)
        )
    ):
        return {}, "invalid operation"
    path = path or state_path()
    with _locked(path):
        data, readable = read_state(path)
        if not readable:
            return {}, "unreadable"
        current = data.get(key, [])
        if not isinstance(current, list) or any(
            not isinstance(item, str) or not item for item in current
        ):
            return data, "invalid operation"
        values = set(current)
        if operation == "set-add":
            values.add(qualified_value if qualified_value in values else value)
        else:
            values.discard(value)
            if qualified_value is not None:
                values.discard(qualified_value)
        data[key] = sorted(values)
        if not _write_state(data, path):
            return data, "unwritable"
        return data, ""


def apply_state(app: App, args: argparse.Namespace, state: dict) -> None:
    # Explicit CLI range flags override the saved range.
    if not (args.since or args.until or args.days is not None):
        saved_range = state.get("range")
        if saved_range:
            try:
                app.set_range_from_text(saved_range)
                app._anchor_default_selection()
            except ValueError:
                pass
    saved_sort = state.get("sort_by")
    if saved_sort in app.sort_options:
        app.sort_by = saved_sort
    elif saved_sort in app.subagent_sort_options:
        # Migrate legacy subagent-only values formerly stored in sort_by.
        app.subagent_sort_by = saved_sort
    if state.get("project_sort_by") in app.project_sort_options:
        app.project_sort_by = state["project_sort_by"]
    if state.get("subagent_sort_by") in app.subagent_sort_options:
        app.subagent_sort_by = state["subagent_sort_by"]
    if state.get("prices_sort") in app.prices_sort_options:
        app.prices_sort = state["prices_sort"]
    # Validate against the overlay-wide vocabulary. Guard the set lookup because a
    # hand-edited unhashable value would otherwise abort startup.
    saved_trend_sort = state.get("trend_sort")
    trend_sort_ok = isinstance(saved_trend_sort, str) and saved_trend_sort in app.TREND_SORT_KEYS
    if trend_sort_ok:
        app.trend_sort = saved_trend_sort
    if state.get("prices_view") in {k for k, _label in app.prices_views}:
        app.prices_view = state["prices_view"]
    # A direction without its validated column must not flip the fallback column.
    if isinstance(state.get("sort_reverse"), bool) and saved_sort in app.sort_options:
        app.sort_reverse = state["sort_reverse"]
    if isinstance(state.get("project_sort_reverse"), bool):
        app.project_sort_reverse = state["project_sort_reverse"]
    if isinstance(state.get("subagent_sort_reverse"), bool):
        app.subagent_sort_reverse = state["subagent_sort_reverse"]
    if isinstance(state.get("prices_sort_reverse"), bool):
        app.prices_sort_reverse = state["prices_sort_reverse"]
    if isinstance(state.get("trend_sort_reverse"), bool) and trend_sort_ok:
        app.trend_sort_reverse = state["trend_sort_reverse"]
    mode = state.get("browse_mode")
    if mode in app.BROWSE_MODE_KEYS:
        app.browse_mode = mode
    # Assign directly: set_focus() has live-navigation side effects not wanted at restore.
    # Keep Time focus even while another browse mode is active for a later switch back.
    if state.get("focus") in app.FOCUS_CYCLE:
        app.focus = state["focus"]
    if isinstance(state.get("zoom_maximized"), bool):
        app.zoom_maximized = state["zoom_maximized"]
    pinned = state.get("pinned_models")
    if isinstance(pinned, list):
        app.pinned_models = {m for m in pinned if isinstance(m, str) and m}
    ignored = state.get("ignored_projects")
    if isinstance(ignored, list):
        app.ignored_projects = {p for p in ignored if isinstance(p, str) and p}
        app._invalidate_workflow_cache()
    ignored_sessions = state.get("ignored_sessions")
    if isinstance(ignored_sessions, list):
        app.ignored_sessions = {s for s in ignored_sessions if isinstance(s, str) and s}
        app._invalidate_workflow_cache()
    # Keep vanished bookmark ids; their source may return later.
    marks = state.get("bookmarks")
    if isinstance(marks, list):
        app.bookmarks = {m for m in marks if isinstance(m, str) and m}
    app._state_set_baseline = {key: set(getattr(app, key)) for key in MUTABLE_SET_KEYS}
    # Repricing remains deferred to the model scan; restore only an explicit saved flag.
    saved_api = state.get("show_api_prices")
    if saved_api is not None and not app.store.demo:
        app.show_api_prices = bool(saved_api)
    # An explicit non-default CLI theme wins over saved state.
    if getattr(args, "theme", themes.DEFAULT_THEME) in (None, themes.DEFAULT_THEME):
        saved_theme = state.get("theme")
        if saved_theme in themes.THEMES:
            app.theme_id = saved_theme
            app.theme = themes.resolve_theme(saved_theme)
    saved_levels = state.get("cal_levels")
    if isinstance(saved_levels, int):
        app.cal_levels = max(HEAT_MIN_LEVELS, min(HEAT_MAX_LEVELS, saved_levels))
    app.prices_prompt_dismissed = bool(state.get("prices_prompt_dismissed", False))
    dismissed_warnings = state.get("dismissed_startup_warnings")
    if isinstance(dismissed_warnings, list):
        app.dismissed_startup_warnings = {
            item for item in dismissed_warnings if isinstance(item, str) and item
        }
    app.notice = ""
