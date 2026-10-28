"""Read-only browser for AI coding spend, with a compatibility re-export surface."""

from __future__ import annotations

# Hatchling and importing modules read this before the re-exports below.
__version__ = "1.32.0"

# Compatibility exports also keep package-level monkeypatches visible to submodules.
import csv
import os
import sys
from datetime import datetime

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

# Every compatibility re-export resolves on first access (PEP 562): `import opentab`,
# and so every `opentab.<module>` import and the `cost` status-bar poll, no longer
# loads the TUI, all stores and the CLI. Later entries override earlier ones, as the
# eager imports did.
_EXPORTS = (
    (
        "opentab.accounting",
        (
            "models",
            "pricing",
            "tools",
        ),
    ),
    (
        "opentab.accounting.models",
        (
            "ALL_HARNESSES",
            "ALL_MACHINES",
            "ALL_YEARS",
            "API_SCHEMA_VERSION",
            "DaySummary",
            "HarnessSummary",
            "MachineSummary",
            "MonthSummary",
            "ProjectSummary",
            "SessionRef",
            "Workflow",
            "YearSummary",
            "year_label",
        ),
    ),
    (
        "opentab.accounting.pricing",
        (
            "CACHE_TTL_LONG",
            "CACHE_TTL_OPENAI_MIN",
            "CACHE_TTL_SHORT",
            "DEFAULT_CONTEXT_WINDOW",
            "FALLBACK_PRICE",
            "LOCAL_PROVIDERS",
            "MODEL_CONTEXT_FALLBACKS",
            "MODEL_PRICE_FALLBACKS",
            "MODELS_DEV_URL",
            "CacheMiss",
            "api_equivalent_cost",
            "cache_misses",
            "cache_ttl_is_minimum",
            "cache_ttl_seconds",
            "cache_write_1h_price",
            "canonical_model",
            "catalog_card",
            "catalog_models",
            "display_model",
            "effective_price",
            "family_label",
            "has_known_price",
            "invalidate_price_cache",
            "is_local_provider",
            "model_context_window",
            "model_family",
            "model_card_info",
            "model_matches",
            "model_price",
            "price_cache_meta",
            "price_cache_path",
            "price_source_meta",
            "prune_models_dev",
            "reasoning_price",
            "refresh_model_prices",
        ),
    ),
    (
        "opentab.cli.main",
        (
            "MIN_PYTHON",
            "enable_unicode_locale",
            "export_command",
            "forget_command",
            "main",
            "parse_args",
            "pull_command",
            "refresh_models_command",
            "status_command",
            "status_line",
            "web_command",
        ),
    ),
    (
        "opentab.demo",
        (
            "DEMO_MACHINES",
            "DEMO_MODEL_POOL",
            "DEMO_NOUNS",
            "DEMO_RATE",
            "DEMO_REPOS",
            "DEMO_VERBS",
            "demo_cost",
            "demo_dir",
            "demo_machine",
            "demo_model",
            "demo_title",
            "demo_turn_content",
        ),
    ),
    (
        "opentab.persistence",
        (
            "notes",
            "paths",
            "state",
        ),
    ),
    (
        "opentab.persistence.notes",
        (
            "NOTES_VERSION",
            "load_notes",
            "notes_path",
            "read_notes",
            "save_notes",
            "update_note",
        ),
    ),
    (
        "opentab.persistence.state",
        (
            "apply_state",
            "load_state",
            "read_state",
            "save_state",
            "state_path",
            "update_state",
        ),
    ),
    (
        "opentab.presentation",
        (
            "formatting",
            "heatmap",
            "themes",
            "whats_new",
        ),
    ),
    (
        "opentab.presentation.formatting",
        (
            "BAR_CELLS",
            "BAR_EIGHTHS",
            "MONEY_PATTERN",
            "TOKEN_PATTERN",
            "clip",
            "clip_tail",
            "cost_bar",
            "display_width",
            "human_duration",
            "human_tokens",
            "iso_to_local",
            "money",
            "money_label",
            "pad",
            "pct",
            "relative_age",
            "short_path",
            "shorten",
            "tokens",
            "wrap_cells",
        ),
    ),
    (
        "opentab.presentation.heatmap",
        (
            "BLOCKS_UP",
            "HEAT_CUBE_RAMP",
            "HEAT_DEFAULT_LEVELS",
            "HEAT_EMPTY_GLYPH",
            "HEAT_MAX_LEVELS",
            "HEAT_MIN_LEVELS",
            "HEAT_RAMP",
            "MONTH_ABBR",
            "PRICE_HEAT_BASE_PAIR",
            "PRICE_HEAT_LEVELS",
            "TOOL_HEAT_BASE_PAIR",
            "TOOL_HEAT_LEVELS",
            "calendar_cells",
            "heat_band_label",
            "heat_glyph",
            "heat_level",
            "heat_palette",
            "heat_sample",
            "month_range",
            "week_key",
        ),
    ),
    (
        "opentab.presentation.themes",
        (
            "DEFAULT_THEME",
            "THEME_IDS",
            "THEMES",
            "ink_on",
            "nearest_8",
            "nearest_256",
            "ramp",
            "resolve_theme",
            "web_payload",
        ),
    ),
    (
        "opentab.sources",
        (
            "DEFAULT_CSV_PATH",
            "DEFAULT_JSONL_PATH",
            "RESUME_COMMANDS",
            "SOURCE_LABELS",
            "available_sources",
            "default_remotes_dir",
            "make_store",
            "resolve_source",
            "source_cycle",
        ),
    ),
    ("opentab.stores.antigravity", ("AntigravityStore",)),
    ("opentab.stores.cached", ("CachedStore",)),
    (
        "opentab.stores.claude",
        (
            "CLAUDE_RETENTION_DEFAULT_DAYS",
            "CLAUDE_RETENTION_RECOMMENDED_DAYS",
            "CLAUDE_RETENTION_WARNING_ID",
            "ClaudeRetention",
            "ClaudeStore",
            "claude_config_dir",
            "claude_projects_dir",
            "claude_retention",
        ),
    ),
    ("opentab.stores.codex", ("CodexStore",)),
    ("opentab.stores.combined", ("CombinedStore",)),
    ("opentab.stores.copilot", ("CopilotStore",)),
    ("opentab.stores.csv_source", ("CsvStore",)),
    (
        "opentab.stores.gemini",
        (
            "GEMINI_RETENTION_DEFAULT_DAYS",
            "GEMINI_RETENTION_RECOMMENDED_DAYS",
            "GEMINI_RETENTION_WARNING_ID",
            "GeminiRetention",
            "GeminiStore",
            "default_gemini_dir",
            "gemini_max_count_label",
            "gemini_retention",
            "gemini_settings_path",
            "gemini_system_defaults_path",
            "gemini_system_settings_path",
        ),
    ),
    ("opentab.stores.hermes", ("HermesStore",)),
    ("opentab.stores.jsonl_source", ("JsonlStore",)),
    ("opentab.stores.omp", ("OmpStore",)),
    ("opentab.stores.openclaw", ("OpenClawStore",)),
    (
        "opentab.stores.opencode",
        (
            "MODEL_EXPR",
            "MSG_MODEL_EXPR",
            "MSG_TOKEN_TOTAL_EXPR",
            "Store",
        ),
    ),
    ("opentab.stores.pi", ("PiStore",)),
    (
        "opentab.stores.remote",
        (
            "RemoteStore",
            "build_export",
        ),
    ),
    ("opentab.stores.vscode", ("VscodeStore",)),
    ("opentab.stores.zaly", ("ZalyStore",)),
    ("opentab.tui", ("keymap",)),
    ("opentab.tui.app", ("App",)),
    ("opentab.tui.renderer", ("Renderer",)),
    (
        "opentab.util",
        (
            "ATTACHMENT_EST_TOKENS",
            "DATE_PATTERN",
            "EST_CHARS_PER_TOKEN",
            "MONTH_PATTERN",
            "OPENCODE_BUILTIN_TOOLS",
            "TRACE_ARG_CAP",
            "TRACE_ARGS_CAP",
            "YEAR_PATTERN",
            "agent_mix_label",
            "anchored_fuzzy_match",
            "cached_share",
            "clip_text",
            "context_add",
            "context_rows",
            "copy_to_clipboard",
            "est_tokens",
            "fuzzy_score",
            "git_root",
            "herdr_create_argv",
            "herdr_launch",
            "herdr_pane_id",
            "in_herdr",
            "in_tmux",
            "launch_backend",
            "launch_command",
            "launcher_hook",
            "model_row_1h_write",
            "model_row_split",
            "month_bounds",
            "month_window_start",
            "node_1h_write",
            "normalize_project_path",
            "open_path",
            "parse_range_text",
            "read_files_parallel",
            "resolve_project_root",
            "short_tool_name",
            "ssh_command",
            "tmux_launch",
            "tmux_launch_argv",
            "tool_call_detail",
            "tool_call_label",
            "tool_mix_label",
            "tool_names",
            "tool_namespace",
            "unicode_screen",
            "validate_date",
            "workflow_fuzzy_score",
        ),
    ),
)

# Keep http.server and doctor dependencies off the frequently polled cost import path.
# PEP 562 preserves the package-level compatibility surface on first access.
_LAZY_ATTRS = {
    "OpenTabService": "opentab.api.service",
    "ServiceError": "opentab.api.service",
    "SessionQuery": "opentab.api.service",
    "build_payload": "opentab.web.report",
    "html_command": "opentab.web.report",
    "serve_command": "opentab.web.report",
    "session_extras": "opentab.web.report",
    "render_html": "opentab.web.page",
    "build_report": "opentab.cli.doctor",
    "doctor_command": "opentab.cli.doctor",
}
# Preserve module attributes independently of which lazy name is accessed first.
_LAZY_MODULES = {
    "web": "opentab.web",
    "webpage": "opentab.web.page",
    "doctor": "opentab.cli.doctor",
}


# Submodules the eager imports bound as package attributes (and so in __all__).
_SUBMODULES = (
    "accounting",
    "cli",
    "conversations",
    "demo",
    "diagnostics",
    "persistence",
    "presentation",
    "progress",
    "remote_content",
    "sources",
    "stores",
    "tui",
    "util",
)
_REEXPORTS: dict[str, tuple[str, str]] = {}
for _module, _names in _EXPORTS:
    for _entry in _names:
        _name, _, _attr = _entry.partition("=")
        _REEXPORTS[_name] = (_module, _attr or _name)


def __getattr__(name: str):
    import importlib

    if name in _LAZY_MODULES:
        value = importlib.import_module(_LAZY_MODULES[name])
    elif name in _REEXPORTS:
        module, attr = _REEXPORTS[name]
        owner = importlib.import_module(module)
        try:
            value = getattr(owner, attr)
        except AttributeError:
            # `from package import submodule` re-exports a module not yet imported.
            value = importlib.import_module(f"{module}.{attr}")
    elif name in _LAZY_ATTRS:
        value = getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    else:
        # Subpackages (`opentab.stores`, `opentab.tui`) the eager imports used to load.
        import importlib.util

        if importlib.util.find_spec(f"{__name__}.{name}") is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = importlib.import_module(f"{__name__}.{name}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_SUBMODULES, *_REEXPORTS, *_LAZY_ATTRS, *_LAZY_MODULES})


# Include unmaterialized lazy names in the compatibility star-import surface.
__all__ = sorted(
    {n for n in globals() if not n.startswith("_")}
    | set(_SUBMODULES)
    | set(_REEXPORTS)
    | set(_LAZY_ATTRS)
    | set(_LAZY_MODULES)
)
