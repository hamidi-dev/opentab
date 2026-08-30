"""Reading/writing the saved-prefs state.json (in $XDG_STATE_HOME/opentab)."""
from __future__ import annotations

import argparse
import json
import os
from typing import TYPE_CHECKING

from opentab import paths, themes
from opentab.heatmap import HEAT_MAX_LEVELS, HEAT_MIN_LEVELS

if TYPE_CHECKING:
    from opentab.tui.app import App


def state_path(migrate: bool = True) -> str:
    # migrate=False lets doctor inspect preferences without moving them.
    target = os.path.join(paths.state_dir(), "state.json")
    return paths.migrated(target) if migrate else paths.resolved(target)


def load_state(path: str | None = None) -> dict:
    try:
        with open(path or state_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


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
        "ignored_projects": sorted(app.ignored_projects),
        "ignored_sessions": sorted(app.ignored_sessions),
        "bookmarks": sorted(app.bookmarks),
        "pinned_models": sorted(app.pinned_models),
        "show_api_prices": app.show_api_prices,
        "source": app.source_key,
        "theme": app.theme_id,
        "cal_levels": app.cal_levels,
        "prices_prompt_dismissed": app.prices_prompt_dismissed,
        "dismissed_startup_warnings": sorted(app.dismissed_startup_warnings),
    }
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh)
    except OSError:
        pass


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
