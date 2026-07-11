"""Reading/writing ~/.config/opentab/state.json prefs."""
from __future__ import annotations

import argparse
import json
import os
from typing import TYPE_CHECKING

from opentab import themes
from opentab.heatmap import HEAT_MAX_LEVELS, HEAT_MIN_LEVELS

if TYPE_CHECKING:
    from opentab.tui.app import App


def state_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "opentab", "state.json")


def load_state() -> dict:
    try:
        with open(state_path()) as fh:
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
        "prices_view": app.prices_view,
        "sort_reverse": app.sort_reverse,
        "project_sort_reverse": app.project_sort_reverse,
        "subagent_sort_reverse": app.subagent_sort_reverse,
        "prices_sort_reverse": app.prices_sort_reverse,
        "browse_mode": app.browse_mode,
        "zoom_maximized": app.zoom_maximized,  # + in a zoomed detail: full-screen vs split
        "ignored_projects": sorted(app.ignored_projects),
        "ignored_sessions": sorted(app.ignored_sessions),
        "bookmarks": sorted(app.bookmarks),  # sessions starred with `b`
        "pinned_models": sorted(app.pinned_models),  # P-overlay pins (canonical ids, space)
        "show_api_prices": app.show_api_prices,
        "source": app.source_key,  # restore the last source (opencode/claude/all) next run
        "theme": app.theme_id,  # the colour theme (shared with the web browser)
        "cal_levels": app.cal_levels,  # the Calendar heat-map granularity (+/-)
        "prices_prompt_dismissed": app.prices_prompt_dismissed,  # "don't ask again" for prices
    }
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def apply_state(app: App, args: argparse.Namespace, state: dict) -> None:
    # CLI range flags win; otherwise restore the last range used. Sort and browse
    # mode are pure UI prefs and are always restored.
    if not (args.since or args.until or args.days is not None):
        saved_range = state.get("range")
        if saved_range:
            try:
                app.set_range_from_text(saved_range)
                # The restored range can change which years/months have data, so
                # re-pick the default (All years, current month) against the new slice.
                app._anchor_default_selection()
            except ValueError:
                pass
    saved_sort = state.get("sort_by")
    if saved_sort in app.sort_options:
        app.sort_by = saved_sort
    elif saved_sort in app.subagent_sort_options:
        # A pre-split state.json could stash a subagent-only key ("depth", "model",
        # "agent") in sort_by; route it home instead of silently dropping it.
        app.subagent_sort_by = saved_sort
    if state.get("project_sort_by") in app.project_sort_options:
        app.project_sort_by = state["project_sort_by"]
    if state.get("subagent_sort_by") in app.subagent_sort_options:
        app.subagent_sort_by = state["subagent_sort_by"]
    if state.get("prices_sort") in app.prices_sort_options:
        app.prices_sort = state["prices_sort"]
    if state.get("prices_view") in {k for k, _label in app.prices_views}:
        app.prices_view = state["prices_view"]
    # Restore a direction flip only when its column key was restored too -- a
    # direction without its column would flip whatever default the key fell back
    # to (e.g. a pre-split "depth" + reverse must not start sessions cheapest-first).
    if isinstance(state.get("sort_reverse"), bool) and saved_sort in app.sort_options:
        app.sort_reverse = state["sort_reverse"]
    if isinstance(state.get("project_sort_reverse"), bool):
        app.project_sort_reverse = state["project_sort_reverse"]
    if isinstance(state.get("subagent_sort_reverse"), bool):
        app.subagent_sort_reverse = state["subagent_sort_reverse"]
    if isinstance(state.get("prices_sort_reverse"), bool):
        app.prices_sort_reverse = state["prices_sort_reverse"]
    if state.get("browse_mode") in ("time", "projects"):
        app.browse_mode = state["browse_mode"]
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
    # Bookmarked session ids survive restarts; ids of sessions that have since
    # vanished (or live in another source) are kept — harmless, and they light up
    # again if that source returns. The B view flag itself always starts off.
    marks = state.get("bookmarks")
    if isinstance(marks, list):
        app.bookmarks = {m for m in marks if isinstance(m, str) and m}
    # Restore the what-if ($) view. Only the flag is set here; the actual reprice
    # rides on the deferred model scan in run() (via _load_model_cache ->
    # _apply_price_mode), so the first paint still comes up off the fast rollup.
    # An explicit saved value (True or False) overrides the records_cost-based
    # default from App.__init__; absent (first run), that default stands.
    saved_api = state.get("show_api_prices")
    if saved_api is not None and not app.store.demo:
        app.show_api_prices = bool(saved_api)
    # Restore the last theme, unless an explicit non-default --theme was passed
    # (which App.__init__ already applied and should win, like the range flags).
    if getattr(args, "theme", themes.DEFAULT_THEME) in (None, themes.DEFAULT_THEME):
        saved_theme = state.get("theme")
        if saved_theme in themes.THEMES:
            app.theme_id = saved_theme
            app.theme = themes.resolve_theme(saved_theme)
    saved_levels = state.get("cal_levels")
    if isinstance(saved_levels, int):
        app.cal_levels = max(HEAT_MIN_LEVELS, min(HEAT_MAX_LEVELS, saved_levels))
    app.prices_prompt_dismissed = bool(state.get("prices_prompt_dismissed", False))
    app.notice = ""
