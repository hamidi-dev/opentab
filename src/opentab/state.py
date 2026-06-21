"""Reading/writing ~/.config/opentab/state.json prefs."""
from __future__ import annotations

import argparse
import json
import os
from typing import TYPE_CHECKING

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
        "browse_mode": app.browse_mode,
        "ignored_projects": sorted(app.ignored_projects),
        "show_api_prices": app.show_api_prices,
        "source": app.source_key,  # restore the last source (opencode/claude/all) next run
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
    if state.get("sort_by") in app.sort_options:
        app.sort_by = state["sort_by"]
    if state.get("project_sort_by") in app.project_sort_options:
        app.project_sort_by = state["project_sort_by"]
    if state.get("browse_mode") in ("time", "projects"):
        app.browse_mode = state["browse_mode"]
    ignored = state.get("ignored_projects")
    if isinstance(ignored, list):
        app.ignored_projects = {p for p in ignored if isinstance(p, str) and p}
        app._invalidate_workflow_cache()
    # Restore the what-if ($) view. Only the flag is set here; the actual reprice
    # rides on the deferred model scan in run() (via _load_model_cache ->
    # _apply_price_mode), so the first paint still comes up off the fast rollup.
    # An explicit saved value (True or False) overrides the records_cost-based
    # default from App.__init__; absent (first run), that default stands.
    saved_api = state.get("show_api_prices")
    if saved_api is not None and not app.store.demo:
        app.show_api_prices = bool(saved_api)
    saved_levels = state.get("cal_levels")
    if isinstance(saved_levels, int):
        app.cal_levels = max(HEAT_MIN_LEVELS, min(HEAT_MAX_LEVELS, saved_levels))
    app.prices_prompt_dismissed = bool(state.get("prices_prompt_dismissed", False))
    app.notice = ""
