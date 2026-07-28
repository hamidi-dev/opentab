"""OpenTab — a lazygit-style browser for your AI coding spend.

Reads OpenCode, Claude Code, Codex, Hermes, the Copilot CLI, pi-agent, omp, OpenClaw,
Zaly, and logged-request CSVs read-only and shows cost by month / day / project /
session / model, including the recursive subagent tree.

This package is the modular form of what used to be a single ``opentab`` script.
The top level re-exports the public API (and a few stdlib modules) so callers and
tests can reach everything through ``opentab.<name>`` as before.
"""

from __future__ import annotations

# Single source of truth for the version (also read by hatchling at build time,
# and imported by opentab.pricing / opentab.cli). Must be set before the
# re-exports below import those modules.
__version__ = "1.15.0"

# Stdlib modules re-exposed as attributes of the package. Modules are singletons,
# so patching e.g. ``opentab.os.startfile`` is visible to every submodule that
# imported ``os`` — which keeps the test suite's monkeypatching working.
import csv
import os
import sys
from datetime import datetime

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

from opentab.cli import (
    MIN_PYTHON,
    enable_unicode_locale,
    export_command,
    forget_command,
    main,
    parse_args,
    pull_command,
    refresh_models_command,
    status_command,
    status_line,
    web_command,
)
from opentab.demo import (
    DEMO_MACHINES,
    DEMO_MODEL_POOL,
    DEMO_NOUNS,
    DEMO_RATE,
    DEMO_REPOS,
    DEMO_VERBS,
    demo_cost,
    demo_dir,
    demo_machine,
    demo_model,
    demo_title,
)
from opentab.formatting import (
    BAR_CELLS,
    BAR_EIGHTHS,
    MONEY_PATTERN,
    TOKEN_PATTERN,
    clip,
    clip_tail,
    cost_bar,
    display_width,
    human_duration,
    human_tokens,
    iso_to_local,
    money,
    money_label,
    pad,
    pct,
    relative_age,
    short_path,
    shorten,
    tokens,
    wrap_cells,
)
from opentab.heatmap import (
    BLOCKS_UP,
    HEAT_CUBE_RAMP,
    HEAT_DEFAULT_LEVELS,
    HEAT_EMPTY_GLYPH,
    HEAT_MAX_LEVELS,
    HEAT_MIN_LEVELS,
    HEAT_RAMP,
    MONTH_ABBR,
    PRICE_HEAT_BASE_PAIR,
    PRICE_HEAT_LEVELS,
    TOOL_HEAT_BASE_PAIR,
    TOOL_HEAT_LEVELS,
    calendar_cells,
    heat_band_label,
    heat_glyph,
    heat_level,
    heat_palette,
    heat_sample,
    month_range,
    week_key,
)
from opentab.models import (
    ALL_YEARS,
    DaySummary,
    MachineSummary,
    MonthSummary,
    ProjectSummary,
    Workflow,
    YearSummary,
    year_label,
)
from opentab.notes import (
    NOTES_VERSION,
    load_notes,
    notes_path,
    read_notes,
    save_notes,
    update_note,
)
from opentab.pricing import (
    DEFAULT_CONTEXT_WINDOW,
    FALLBACK_PRICE,
    LOCAL_PROVIDERS,
    MODEL_CONTEXT_FALLBACKS,
    MODEL_PRICE_FALLBACKS,
    MODELS_DEV_URL,
    api_equivalent_cost,
    cache_write_1h_price,
    canonical_model,
    catalog_models,
    display_model,
    effective_price,
    family_label,
    has_known_price,
    invalidate_price_cache,
    is_local_provider,
    model_context_window,
    model_family,
    model_matches,
    model_price,
    price_cache_meta,
    price_cache_path,
    price_source_meta,
    prune_models_dev,
    refresh_model_prices,
)
from opentab.sources import (
    DEFAULT_CSV_PATH,
    DEFAULT_JSONL_PATH,
    RESUME_COMMANDS,
    SOURCE_LABELS,
    available_sources,
    default_remotes_dir,
    make_store,
    resolve_source,
    source_cycle,
)
from opentab.state import apply_state, load_state, save_state, state_path
from opentab.stores.cached import CachedStore
from opentab.stores.claude import ClaudeStore
from opentab.stores.codex import CodexStore
from opentab.stores.combined import CombinedStore
from opentab.stores.copilot import CopilotStore
from opentab.stores.csv_source import CsvStore
from opentab.stores.hermes import HermesStore
from opentab.stores.jsonl_source import JsonlStore
from opentab.stores.omp import OmpStore
from opentab.stores.openclaw import OpenClawStore
from opentab.stores.opencode import MODEL_EXPR, MSG_MODEL_EXPR, MSG_TOKEN_TOTAL_EXPR, Store
from opentab.stores.pi import PiStore
from opentab.stores.remote import RemoteStore, build_export
from opentab.stores.vscode import VscodeStore
from opentab.stores.zaly import ZalyStore
from opentab.themes import (
    DEFAULT_THEME,
    THEME_IDS,
    THEMES,
    ink_on,
    nearest_8,
    nearest_256,
    ramp,
    resolve_theme,
    web_payload,
)
from opentab.tui import keymap
from opentab.tui.app import App
from opentab.tui.renderer import Renderer
from opentab.util import (
    ATTACHMENT_EST_TOKENS,
    DATE_PATTERN,
    EST_CHARS_PER_TOKEN,
    MONTH_PATTERN,
    OPENCODE_BUILTIN_TOOLS,
    YEAR_PATTERN,
    anchored_fuzzy_match,
    context_add,
    context_rows,
    copy_to_clipboard,
    est_tokens,
    fuzzy_score,
    git_root,
    in_tmux,
    launcher_hook,
    model_row_1h_write,
    model_row_split,
    month_bounds,
    month_window_start,
    node_1h_write,
    normalize_project_path,
    open_path,
    parse_range_text,
    read_files_parallel,
    resolve_project_root,
    ssh_command,
    tmux_launch,
    tmux_launch_argv,
    tool_namespace,
    unicode_screen,
    validate_date,
    workflow_fuzzy_score,
)

# The web frontend is the one part of the package NOT re-exported eagerly. Its
# http.server import costs ~13ms, which every `opentab status` poll and every TUI
# start paid just to have these five names on the package -- cli.web_command has
# always imported opentab.web lazily for the same reason, so this closes the last
# eager path to it. PEP 562: the attribute materializes on first access, so
# `opentab.build_payload` (and patching it) works exactly as before.
_LAZY_ATTRS = {
    "build_payload": "opentab.web",
    "html_command": "opentab.web",
    "serve_command": "opentab.web",
    "session_extras": "opentab.web",
    "render_html": "opentab.webpage",
}
# The two MODULES as well, not just the names above. `from opentab.web import ...`
# used to bind `opentab.web` as a side effect of importing it, and callers rely on
# that (ot.web.ReportServer, ot.webpage.render_html). Without these entries the
# attribute exists only once something else has resolved a lazy name -- which is a
# bug that hides, because it depends on what ran first.
_LAZY_MODULES = ("web", "webpage")


def __getattr__(name: str):
    import importlib

    if name in _LAZY_MODULES:
        value = importlib.import_module(f"{__name__}.{name}")
    else:
        module = _LAZY_ATTRS.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # cached, so later reads never reach __getattr__
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_ATTRS, *_LAZY_MODULES})


# Spelled out because a lazy name is invisible to `from opentab import *` until it
# has been materialized: with no __all__, star-import reads the module dict and
# would silently skip exactly the deferred names. Built from the eager surface so
# it stays identical to what star-import exported when everything was eager.
__all__ = sorted(
    {n for n in globals() if not n.startswith("_")} | set(_LAZY_ATTRS) | set(_LAZY_MODULES)
)
