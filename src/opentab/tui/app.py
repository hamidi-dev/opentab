from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import os
import re
import shlex
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import NamedTuple

from opentab.models import Workflow
from opentab.stores.opencode import Store

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

from opentab import sources, themes, util
from opentab.demo import (
    DEMO_ALL,
    DEMO_CATEGORIES,
    demo_cost,
    demo_machine,
    demo_model,
    demo_title,
    demo_turn_content,
)
from opentab.formatting import clip, clip_tail, display_width, short_path, shorten
from opentab.heatmap import (
    HEAT_DEFAULT_LEVELS,
    HEAT_MAX_LEVELS,
    HEAT_MIN_LEVELS,
    month_range,
    week_key,
)
from opentab.models import (
    ALL_MACHINES,
    ALL_YEARS,
    DaySummary,
    MachineSummary,
    MonthSummary,
    ProjectSummary,
    YearSummary,
)
from opentab.notes import notes_path, read_notes, update_note
from opentab.pricing import (
    LOCAL_PROVIDERS,
    TOKEN_TYPES,
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
    is_vendor_route,
    model_family,
    model_matches,
    model_price,
    price_cache_meta,
    refresh_model_prices,
)
from opentab.sources import RESUME_COMMANDS, SOURCE_LABELS
from opentab.tui import bindings
from opentab.tui.renderer import Renderer
from opentab.util import (
    DULL_AGENT_NAMES,
    fuzzy_score,
    model_row_1h_write,
    model_row_split,
    month_bounds,
    month_window_start,
    node_1h_write,
    open_path,
    parse_range_text,
    resolve_project_root,
    workflow_fuzzy_score,
)


class Toast:
    __slots__ = ("text", "kind", "born", "ttl")

    def __init__(self, text: str, kind: str, born: float, ttl: float):
        self.text = text
        self.kind = kind
        self.born = born
        self.ttl = ttl

    def remaining(self, now: float) -> float:
        return self.ttl - (now - self.born)


class TokenEconomics(NamedTuple):
    """Token volume and list-rate spend split by TOKEN_TYPES.

    Cost always uses list rates because backends cannot attribute recorded spend by
    token type. Local-model tokens are excluded from both distributions.
    """

    tokens: tuple[float, ...]
    cost: tuple[float, ...]
    estimated: bool
    missing_cache_rate: bool
    local_tokens: int

    @property
    def total_tokens(self) -> float:
        return sum(self.tokens)

    @property
    def total_cost(self) -> float:
        return sum(self.cost)


# Reserve a unique color for root work; children may cycle without obscuring that split.
FLAME_SELF_SLOT = 0
FLAME_CHILD_SLOTS = (1, 2, 3, 4)

_FLAME_DULL_AGENTS = DULL_AGENT_NAMES

# OpenCode sometimes stores the missing agent only as an "(@name)" title suffix.
_FLAME_AGENT_TAG = re.compile(r"\(@([\w.-]+)")


def flame_label(row: dict) -> str:
    # Parent links are unavailable, so mark nested nodes that must be drawn as siblings.
    agent = str(row.get("agent") or "").strip()
    name = agent if agent.lower() not in _FLAME_DULL_AGENTS else ""
    if not name:
        tag = _FLAME_AGENT_TAG.search(str(row.get("title") or ""))
        name = tag.group(1) if tag else "subagent"
    return ("↳ " + name) if int(row.get("depth") or 0) > 1 else name


def flame_model(row: dict) -> str:
    return display_model(str(row.get("model_name") or "").rsplit("/", 1)[-1])


class FlameSegment(NamedTuple):
    """One flamegraph band; depth 2+ is folded beside direct subagents."""

    label: str
    agent: str
    model: str
    value: float
    share: float
    slot: int
    depth: int


class SessionFlame(NamedTuple):
    """Root and subagent proportions using the table's effective node costs.

    Token width is used when recorded cost is entirely zero. Stores expose depth but
    not parent links, so nested nodes remain marked siblings instead of fake nesting.
    """

    segments: tuple[FlameSegment, ...]
    total: float
    unit: str
    estimated: bool
    deep: int
    silent: int

    @property
    def self_share(self) -> float:
        return sum(s.share for s in self.segments if s.depth == 0)

    @property
    def children(self) -> tuple[FlameSegment, ...]:
        return tuple(s for s in self.segments if s.depth > 0)

    @property
    def one_model(self) -> str:
        models = {s.model for s in self.segments if s.model}
        return models.pop() if len(models) == 1 else ""


class PriceEntry(NamedTuple):
    """Canonical price-table row, aggregated by model or split by access route."""

    bare: str
    canon: str
    family: str
    routes: tuple[str, ...]
    spend: float
    group: str
    share: float
    price: tuple
    eff: float
    approx: bool
    status: str = ""
    pinned: bool = False


class BrowseMode(NamedTuple):
    """Canonical top-level mode metadata shared by UI, keymap, state, and tests."""

    key: str
    label: str
    action: str
    hierarchical: bool


class SelectionAnchor(NamedTuple):
    """Value-based selection that survives row reordering and removal."""

    year: str | None
    month: str | None
    day: str | None
    project: str | None
    machine: str | None  # "" identifies the synthetic fleet row
    session: str | None


class App:
    BROWSE_MODES = (
        BrowseMode("time", "Time", "mode_time", True),
        BrowseMode("projects", "Projects", "mode_projects", False),
        BrowseMode("machines", "Machines", "mode_machines", False),
    )
    BROWSE_MODE_KEYS = tuple(m.key for m in BROWSE_MODES)
    workflow_tabs = ("Overview", "Subagents")
    day_tabs = ("Overview", "Projects", "Sessions")
    month_tabs = ("Overview", "Models", "Projects", "Sessions")
    year_tabs = ("Overview", "Models", "Projects", "Sessions")
    project_tabs = ("Overview", "Models", "Sessions")
    machine_tabs = ("Overview", "Sessions", "Models", "Projects")
    sort_options = (
        "cost",
        "tokens",
        "date",
        "last_activity",
        "duration",
        "subagents",
        "project",
        "title",
    )
    project_sort_options = (
        "cost",
        "tokens",
        "sessions",
        "subagents",
        "project",
        "recency",
        "last_activity",
    )
    subagent_sort_options = ("cost", "tokens", "date", "title", "model", "agent", "depth")
    prices_sort_options = ("model", "eff", "use", "input", "output", "cache_read", "cache_write")
    # A ranked tab offers only visible columns; cost is the common fallback.
    _TREND_SORT_COLUMNS = {
        "Models": ("cost", "name"),
        "Providers": ("cost", "name", "tokens", "count"),
        "Projects": ("cost", "name", "tokens", "count"),
        "Harnesses": ("cost", "name", "tokens", "count"),
        "Machines": ("cost", "name", "tokens", "count"),
    }
    _TREND_SORT_LABELS = {
        "Models": {"name": "Model"},
        "Providers": {"name": "Provider", "count": "Msgs"},
        "Projects": {"name": "Project", "count": "Sessions"},
        "Harnesses": {"name": "Harness", "count": "Sessions"},
        "Machines": {"name": "Machine", "count": "Sessions"},
    }
    TREND_SORT_KEYS = frozenset(k for opts in _TREND_SORT_COLUMNS.values() for k in opts)
    prices_views = (
        ("flat", "flat list"),
        ("family", "by vendor"),
        ("provider", "by provider"),
        ("all", "models.dev"),
    )
    ascending_sort_keys = frozenset({"title", "project", "model", "agent", "depth", "eff", "name"})
    _TREND_TABS_BASE = (
        "Daily",
        "Weekly",
        "Monthly",
        "Calendar",
        "Models",
        "Providers",
        "Projects",
        "Harnesses",
    )
    LAUNCH_TARGETS = (
        ("w", "window", "new window"),
        ("s", "hsplit", "split pane │"),
        ("v", "vsplit", "split pane ─"),
        ("p", "popup", "popup"),
        ("y", "copy", "copy resume command"),
    )
    TOAST_TTL = 4.0
    TOAST_FADE = 0.9
    TOAST_MAX = 3
    TOAST_POLL_MS = 200
    # History is in-memory and expiry-independent; unlike notes, notices are not authored data.
    TOAST_LOG_MAX = 200
    # Defaults also support test instances built with __new__.
    _toast_clock = staticmethod(time.monotonic)
    _toast_shown = True
    whatif_model: str | None = None
    whatif_menu = False
    whatif_menu_index = 0
    whatif_query = ""
    whatif_filter_active = False
    whatif_catalog = False
    _whatif_catalog_rows: list[tuple[str, float, bool]] | None = None

    def __init__(
        self,
        store: Store,
        args: argparse.Namespace,
        source_key: str = "",
        keymap: bindings.Keymap | None = None,
    ):
        self.store = store
        self.args = args
        self.keymap = keymap or bindings.DEFAULT
        self.source_key = source_key
        # Demo category sets need separate stores because scrambling happens during parse.
        self._store_cache: dict[tuple[str, frozenset | None], object] = (
            {(source_key, self._store_state_key(store)): store} if source_key else {}
        )
        self.loaded = store.workflows()
        # Subscription-heavy installs default to useful API estimates; state may override.
        self.show_api_prices = not store.demo
        # What-if is session-scoped and transient; it must never alter `$` or persisted state.
        self.whatif_model: str | None = None
        self.whatif_menu = False
        self.whatif_menu_index = 0
        self.whatif_query = ""
        self.whatif_filter_active = False
        self.whatif_catalog = False
        self._whatif_catalog_rows: list[tuple[str, float, bool]] | None = None
        self._snapshot_real_costs()
        self._resolve_project_roots()
        # Defer the corpus-wide model scan until after the fast first frame.
        self._model_by_root: dict[str, list[dict]] = defaultdict(list)
        self._models_loaded = False
        self._tool_by_session: dict[str, list[dict]] = {}
        self._turns_by_session: dict[str, list[dict]] = {}
        self._turn_runs_cache: tuple | None = None
        # The trace is fetched only when a turn is opened, never by the drill-in
        # prefetch: it is the one extra that re-reads a session's whole content stream,
        # and most drill-ins never ask for it.
        self._trace_by_session: dict[str, dict] = {}
        self._context_by_session: dict[str, list[dict]] = {}
        self._nodes_by_session: dict[str, list[dict]] = {}
        # Session extras load after a placeholder frame, never during drawing.
        self._session_loading: str | None = None
        self.custom_since = args.since
        self.custom_until = args.until
        self.range_days = None if (args.since or args.until or args.days is None) else args.days
        self.range_months: int | None = None
        self.query = ""
        self.filter_active = False
        self._filter_before = ""
        self.launch_menu: Workflow | None = None
        self.launch_menu_index = 0
        self.launch_menu_backend: str | None = None
        self.price_prompt = False
        self._price_prompt_done = False
        self.prices_prompt_dismissed = False
        self.allow_price_prompt = True
        self.startup_warning: dict | None = None
        # Warnings queue rather than overwrite: two harnesses can expire history at
        # once, and a single slot would hide the second until the first was dismissed
        # for good -- which plain "continue" never does.
        self._pending_warnings: list[dict] = []
        self.startup_warning_can_persist = True
        self.dismissed_startup_warnings: set[str] = set()
        self.unknown_models: list[str] = []
        self.source_menu = False
        self.source_menu_index = 0
        self.machine_menu = False
        self.machine_menu_index = 0
        self.harness_menu = False
        self.harness_menu_index = 0
        self.sort_menu = False
        self.sort_menu_index = 0
        self.demo_menu = False
        self.demo_menu_index = 0
        self.demo_menu_sel: set = set()
        self.demo_last_sel: frozenset | None = None
        self.theme_id = getattr(args, "theme", None) or themes.DEFAULT_THEME
        if self.theme_id not in themes.THEMES:
            self.theme_id = themes.DEFAULT_THEME
        self.theme = themes.resolve_theme(self.theme_id)
        self.theme_menu = False
        self.theme_menu_index = 0
        self._theme_before = self.theme_id
        self.day_index = 0
        self.month_index = 0
        self.year_index = 0
        self.project_index = 0
        self.machine_index = 0
        self._local_machine_fake = ""
        self.workflow_index = 0
        self.focus = "days"
        self.browse_mode = "time"
        # Mode positions are value-anchored so reordered rows cannot select a neighbor.
        self._mode_memory: dict[str, dict] = {}
        # Defer blocking fleet refreshes until their progress toast has painted.
        self._refresh_request: list[str] | None = None
        self._refresh_backend = None
        # Callable so remotes.json changes become visible without restarting.
        self._ssh_targets = None
        self.view = "browse"
        self.zoom_maximized = False
        self.tab = 0
        self.scroll = 0
        self.help = False
        self.help_scroll = 0
        self.toast_history = False
        self.toast_history_scroll = 0
        self.trends = False
        self.trend_tab = 0
        self.trend_month_index = 0
        self.trend_week_index = 0
        self.trend_year_index = 0
        self.cal_cursor: str | None = None
        self.trend_focus = False
        self.trend_cursor: str | None = None
        self.trend_row_index = 0
        self.trend_sort = "cost"
        self.trend_sort_reverse = False
        self.trend_drill: tuple[str, str] | None = None
        self.trend_drill_index = 0
        self.turn_drill: int | None = None
        # A prompt ordinal is valid only for its owning session; validate at point of use.
        self._turn_drill_session: str | None = None
        self._turn_cursor = 0
        self._turn_follow = False
        # The third level: one turn's trace, addressed by its ABSOLUTE row index so it
        # survives the group's own ordering. Its cursor is a position within the drilled
        # prompt, cleared with the drill it belongs to.
        self.trace_drill: int | None = None
        self._trace_cursor = 0
        self._trace_list_scroll = 0
        self.trace_expanded = False
        self._trace_open_outputs: set[int] = set()
        self._trace_full: tuple[str, str, list[dict]] | None = None
        self._trace_loading: tuple[str, str] | None = None
        self._remote_trace_job = None
        self._remote_trace_content = None  # one turn: (session, key, full, preview)
        self._remote_trace_error = ""
        self.cal_levels = HEAT_DEFAULT_LEVELS
        self.has256 = False
        self.colors_ok = True
        # Some multiplexers accept palette writes but discard them; CLI resolves this gate.
        self.allow_init_color = True
        self._cal_geom: tuple | None = None
        self._trend_bar_geom: tuple | None = None
        self._trend_return: tuple | None = None
        self.show_prices = False
        self.prices_scroll = 0
        self.prices_index = 0
        # Pins are route-scoped so one gateway never pins every reseller of a model.
        self.pinned_models: set[str] = set()
        self.prices_model: str | None = None
        self.prices_sort = "eff"
        self.prices_sort_reverse = False
        self.prices_view = "flat"
        self.sort_by = "cost"
        self.project_sort_by = "cost"
        self.subagent_sort_by = "cost"
        self.sort_reverse = False
        self.project_sort_reverse = False
        self.subagent_sort_reverse = False
        self.ignored_projects: set[str] = set()
        self.ignored_sessions: set[str] = set()
        self.show_ignored_projects = False
        self.bookmarks: set[str] = set()
        self.show_bookmarks_only = False
        # Notes are authored data: separate from preferences and written on every edit.
        self.notes: dict[str, str] = {}
        self.notes_enabled = True
        self._notes_ok = True
        self.zoom_project: str | None = None
        self.zoom_source: str | None = None
        self.source_index = 0
        self.zoom_model: str | None = None
        self.model_pick_index = 0
        self.zoom_machine: str | None = None
        self.machine_pick_index = 0
        self.machine_filter: str | None = None
        # Fleet machine and harness filters are orthogonal and compose globally.
        self.harness_filter: str | None = None
        self.renderer = Renderer(self)
        self._anchor_default_selection()

    def _anchor_default_selection(self) -> None:
        # Set year before reading its scoped months; prefer all history and the current month.
        years = self.years
        self.year_index = next((i for i, y in enumerate(years) if y.year == ALL_YEARS), 0)
        months = self.months
        now = datetime.now().strftime("%Y-%m")
        self.month_index = next((i for i, m in enumerate(months) if m.month == now), 0)

    def _invalidate_workflow_cache(self) -> None:
        self._rw_key = self._rw_cache = self._aw_key = self._aw_cache = None

    @property
    def ranged_workflows(self) -> list[Workflow]:
        # Ignored projects remain available here so the UI can unignore them.
        key = (
            id(self.loaded),
            self.custom_since,
            self.custom_until,
            self.range_days,
            self.range_months,
            tuple(sorted(self.bookmarks)) if self.show_bookmarks_only else None,
            self.machine_filter,
            self.harness_filter,
        )
        if getattr(self, "_rw_key", None) == key:
            return self._rw_cache
        rows = self.loaded
        if self.show_bookmarks_only:
            rows = [w for w in rows if w.id in self.bookmarks]
        if self.machine_filter is not None:
            rows = [w for w in rows if self.machine_of(w) == self.machine_filter]
        if self.harness_filter is not None:
            rows = [w for w in rows if (w.source or "unknown") == self.harness_filter]
        if self.custom_since or self.custom_until:
            if self.custom_since:
                rows = [w for w in rows if w.created_at[:10] >= self.custom_since]
            if self.custom_until:
                rows = [w for w in rows if w.created_at[:10] <= self.custom_until]
        elif self.range_days is not None:
            cutoff = (datetime.now() - timedelta(days=self.range_days)).strftime("%Y-%m-%d")
            rows = [w for w in rows if w.created_at[:10] >= cutoff]
        elif self.range_months is not None:
            cutoff = month_window_start(self.range_months)
            rows = [w for w in rows if w.created_at[:10] >= cutoff]
        self._rw_key = key
        self._rw_cache = list(rows)
        return self._rw_cache

    @property
    def all_workflows(self) -> list[Workflow]:
        key = (
            id(self.loaded),
            self.custom_since,
            self.custom_until,
            self.range_days,
            self.range_months,
            tuple(sorted(self.ignored_projects)),
            tuple(sorted(self.ignored_sessions)),
            tuple(sorted(self.bookmarks)) if self.show_bookmarks_only else None,
            self.machine_filter,
            self.harness_filter,
        )
        if getattr(self, "_aw_key", None) == key:
            return self._aw_cache
        rows = [
            w
            for w in self.ranged_workflows
            if self.project_root(w.directory) not in self.ignored_projects
            and w.id not in self.ignored_sessions
        ]
        self._aw_key = key
        self._aw_cache = list(rows)
        return self._aw_cache

    def range_cost_total(self) -> float:
        return sum(w.total_cost for w in self.all_workflows)

    def _clear_zoom_drills(self) -> None:
        # A drill value and its ordinal cursor are one selection; always clear both.
        self._clear_project_drill()
        self._clear_source_drill()
        self._clear_machine_drill()
        self._clear_model_drill()

    def set_all_time(self) -> None:
        # Capture first because clearing drills widens the list containing the selection.
        anchor = self.selection_anchor()
        self._clear_zoom_drills()
        self.custom_since = None
        self.custom_until = None
        self.range_days = None
        self.range_months = None
        self.restore_selection(anchor)
        self.notice = "range: all time"

    def range_input_value(self) -> str:
        if self.custom_since or self.custom_until:
            return f"{self.custom_since or ''}..{self.custom_until or ''}"
        if self.range_days is not None:
            return f"{self.range_days}d"
        if self.range_months is not None:
            return f"{self.range_months}m"
        return "all"

    def set_range_from_text(self, raw: str) -> None:
        # Capture first because clearing drills widens the list containing the selection.
        anchor = self.selection_anchor()
        self._clear_zoom_drills()
        days, months, since, until = parse_range_text(raw)
        self.range_days = days
        self.range_months = months
        self.custom_since = since
        self.custom_until = until
        self.restore_selection(anchor)
        self.notice = f"range: {self.range_label()}"

    def _reset_indices(self) -> None:
        self.month_index = 0
        self.day_index = 0
        self.project_index = 0
        self.workflow_index = 0
        self.scroll = 0

    @property
    def active_day(self) -> str | None:
        rows = self.panel_days
        if not rows:
            return None
        self.day_index = max(0, min(self.day_index, len(rows) - 1))
        return rows[self.day_index].day

    @property
    def workflows(self) -> list[Workflow]:
        day = self.active_day
        rows = self.workflows_for_day(day) if day else []
        return self.filtered_sessions(rows)

    def filtered_sessions(self, rows: list[Workflow]) -> list[Workflow]:
        # Stable fuzzy ranking preserves the active sort for equal scores.
        rows = self.sorted_workflows(rows)
        if not self.query:
            return rows
        scored = [(workflow_fuzzy_score(self.query, w, self.note_for(w.id)), w) for w in rows]
        ranked = [(s, w) for s, w in scored if s is not None]
        ranked.sort(key=lambda pair: -pair[0])
        return [w for _, w in ranked]

    def _day_summaries(self, workflows: list[Workflow]) -> list[DaySummary]:
        grouped: dict[str, list[Workflow]] = defaultdict(list)
        for workflow in workflows:
            grouped[workflow.created_at[:10]].append(workflow)
        return [
            DaySummary(
                day=day,
                workflows=len(ws),
                cost=sum(w.total_cost for w in ws),
                tokens=sum(w.total_tokens for w in ws),
                subagents=sum(w.subagents for w in ws),
                unpriced_tokens=sum(w.unpriced_tokens for w in ws),
            )
            for day, ws in grouped.items()
        ]

    @property
    def days(self) -> list[DaySummary]:
        return sorted(self._day_summaries(self.all_workflows), key=lambda d: d.day, reverse=True)

    @property
    def panel_days(self) -> list[DaySummary]:
        month = self.focused_month
        source = self.workflows_for_month(month) if month else self.all_workflows
        return sorted(self._day_summaries(source), key=lambda d: d.day, reverse=True)

    @property
    def years(self) -> list[YearSummary]:
        grouped: dict[str, list[Workflow]] = defaultdict(list)
        for workflow in self.all_workflows:
            grouped[workflow.created_at[:4]].append(workflow)
        years = [
            YearSummary(
                year=year,
                workflows=len(workflows),
                cost=sum(w.total_cost for w in workflows),
                tokens=sum(w.total_tokens for w in workflows),
                subagents=sum(w.subagents for w in workflows),
                unpriced_tokens=sum(w.unpriced_tokens for w in workflows),
            )
            for year, workflows in grouped.items()
        ]
        years.sort(key=lambda y: y.year, reverse=True)
        if len(years) > 1:
            allw = self.all_workflows
            years.insert(
                0,
                YearSummary(
                    year=ALL_YEARS,
                    workflows=len(allw),
                    cost=sum(w.total_cost for w in allw),
                    tokens=sum(w.total_tokens for w in allw),
                    subagents=sum(w.subagents for w in allw),
                    unpriced_tokens=sum(w.unpriced_tokens for w in allw),
                ),
            )
        return years

    @property
    def months(self) -> list[MonthSummary]:
        year = self.focused_year
        grouped: dict[str, list[Workflow]] = defaultdict(list)
        for workflow in self.all_workflows:
            if year is None or workflow.created_at[:4] == year:
                grouped[workflow.created_at[:7]].append(workflow)
        months = [
            MonthSummary(
                month=month,
                workflows=len(workflows),
                cost=sum(w.total_cost for w in workflows),
                tokens=sum(w.total_tokens for w in workflows),
                subagents=sum(w.subagents for w in workflows),
                unpriced_tokens=sum(w.unpriced_tokens for w in workflows),
            )
            for month, workflows in grouped.items()
        ]
        return sorted(months, key=lambda m: m.month, reverse=True)

    def _resolve_project_roots(self) -> None:
        # Resolve each directory once so worktrees group under their main repository.
        self._root_by_dir: dict[str, str] = {}
        if self.store.demo or getattr(self.args, "no_worktrees", False):
            return
        for directory in {w.directory for w in self.loaded}:
            root = resolve_project_root(directory)
            if root != directory:
                self._root_by_dir[directory] = root

    def project_root(self, directory: str) -> str:
        return self._root_by_dir.get(directory, directory)

    @property
    def projects(self) -> list[ProjectSummary]:
        source = self.ranged_workflows if self.show_ignored_projects else self.all_workflows
        return self.projects_for_workflows(source, include_ignored=self.show_ignored_projects)

    def projects_for_workflows(
        self, workflows: list[Workflow], include_ignored: bool = False
    ) -> list[ProjectSummary]:
        grouped: dict[str, list[Workflow]] = defaultdict(list)
        for workflow in workflows:
            directory = self.project_root(workflow.directory)
            if include_ignored or directory not in self.ignored_projects:
                grouped[directory].append(workflow)
        projects = [
            ProjectSummary(
                directory=directory,
                workflows=len(workflows),
                cost=sum(w.total_cost for w in workflows),
                tokens=sum(w.total_tokens for w in workflows),
                subagents=sum(w.subagents for w in workflows),
                unpriced_tokens=sum(w.unpriced_tokens for w in workflows),
                last_active=max(w.created_at for w in workflows),
                last_activity=max((w.ended_at or w.created_at) for w in workflows),
                ignored=directory in self.ignored_projects,
            )
            for directory, workflows in grouped.items()
        ]
        projects = self.sorted_projects(projects)
        if self.query:
            scored = [(fuzzy_score(self.query, p.directory), p) for p in projects]
            ranked = [(s, p) for s, p in scored if s is not None]
            ranked.sort(key=lambda pair: -pair[0])  # stable: ties keep the sort order
            projects = [p for _, p in ranked]
        return projects

    def include_ignored_for_project(self, project: ProjectSummary) -> bool:
        return project.ignored or self.show_ignored_projects

    def sorted_projects(self, rows: list[ProjectSummary]) -> list[ProjectSummary]:
        sort_by = self.project_sort_key()
        desc = self.sort_descending(sort_by, self.project_sort_reverse)
        if sort_by == "tokens":
            return sorted(rows, key=lambda p: (p.tokens, p.cost), reverse=desc)
        if sort_by == "sessions":
            return sorted(rows, key=lambda p: (p.workflows, p.cost), reverse=desc)
        if sort_by == "subagents":
            return sorted(rows, key=lambda p: (p.subagents, p.cost), reverse=desc)
        if sort_by == "project":
            return sorted(rows, key=lambda p: p.directory.lower(), reverse=desc)
        if sort_by == "recency":
            return sorted(rows, key=lambda p: p.last_active, reverse=desc)
        if sort_by == "last_activity":
            return sorted(rows, key=lambda p: p.last_activity, reverse=desc)
        return sorted(rows, key=lambda p: (p.cost, p.tokens), reverse=desc)

    def machine_meta(self) -> dict[str, dict]:
        return getattr(self.store, "machine_meta", {}) or {}

    @property
    def local_machine_name(self) -> str:
        # Untagged sessions belong to this host; hostnames are identity and must be anonymized.
        name = util.local_machine_name()
        if not (self.store.demo and "titles" in self._demo_cats):
            return name
        if not self._local_machine_fake:
            self._local_machine_fake = demo_machine(name)
        return self._local_machine_fake

    def machine_of(self, workflow: Workflow) -> str:
        return workflow.machine or self.local_machine_name

    @property
    def machines(self) -> list[MachineSummary]:
        meta = self.machine_meta()
        local = self.local_machine_name
        grouped: dict[str, list[Workflow]] = defaultdict(list)
        for w in self.all_workflows:
            grouped[w.machine or local].append(w)
        rows = [
            MachineSummary(
                name=name,
                workflows=len(wfs),
                cost=sum(w.total_cost for w in wfs),
                tokens=sum(w.total_tokens for w in wfs),
                subagents=sum(w.subagents for w in wfs),
                unpriced_tokens=sum(w.unpriced_tokens for w in wfs),
                last_active=max(w.created_at for w in wfs),
                # A non-fleet store's only machine is necessarily the live local one.
                live=bool((meta.get(name) or {}).get("live")) or (not meta and name == local),
                exported_at=str((meta.get(name) or {}).get("exported_at") or ""),
                opentab_version=str((meta.get(name) or {}).get("opentab_version") or ""),
            )
            for name, wfs in grouped.items()
        ]
        rows.sort(key=lambda m: (m.live, m.cost, m.tokens), reverse=True)
        if len(rows) > 1:
            # Preserve workflow addition order so this total rounds like range_cost_total().
            allw = self.all_workflows
            rows.insert(
                0,
                MachineSummary(
                    name=ALL_MACHINES,
                    fleet=True,
                    workflows=len(allw),
                    cost=sum(w.total_cost for w in allw),
                    tokens=sum(w.total_tokens for w in allw),
                    subagents=sum(w.subagents for w in allw),
                    unpriced_tokens=sum(w.unpriced_tokens for w in allw),
                    last_active=max((w.created_at for w in allw), default=""),
                ),
            )
        # The query filters sessions within a machine, not machine names.
        return rows

    @property
    def selected_machine_summary(self) -> MachineSummary | None:
        rows = self.machines
        if not rows:
            return None
        self.machine_index = max(0, min(self.machine_index, len(rows) - 1))
        return rows[self.machine_index]

    def workflows_for_machine(self, name: str) -> list[Workflow]:
        return [w for w in self.all_workflows if self.machine_of(w) == name]

    def machine_scope(self, machine: MachineSummary) -> list[Workflow]:
        # Identify the synthetic fleet by its flag; machine names are unrestricted text.
        if machine.fleet:
            return list(self.all_workflows)
        return self.workflows_for_machine(machine.name)

    def machine_filter_options(self) -> list[tuple[str, str, bool]]:
        # Build from all loaded data so an active filter cannot hide its alternatives.
        meta = self.machine_meta()
        grouped: dict[str, float] = defaultdict(float)
        for w in self.loaded:
            grouped[self.machine_of(w)] += w.total_cost
        names = sorted(
            grouped,
            key=lambda n: (bool((meta.get(n) or {}).get("live")), grouped[n]),
            reverse=True,
        )
        out: list[tuple[str, str, bool]] = [("", "All machines", self.machine_filter is None)]
        for name in names:
            out.append((name, name, self.machine_filter == name))
        return out

    def open_machine_menu(self) -> None:
        if not self.machines_present:
            self.notify("machine filter needs a fleet — see --pull", "error")
            return
        options = self.machine_filter_options()
        cur = next(
            (i for i, (value, _l, _a) in enumerate(options) if value == self.machine_filter), 0
        )
        self.machine_menu_index = cur
        self.machine_menu = True

    def select_machine_filter(self, name: str | None) -> None:
        name = name or None
        if name == self.machine_filter:
            return
        anchor = self.selection_anchor()
        self.machine_filter = name
        self._invalidate_workflow_cache()
        self.restore_selection(anchor)
        self.notify(f"machine: {name}" if name else "machine filter cleared", "success")

    def _revalidate_machine_filter(self) -> None:
        # A stale global filter must clear rather than silently empty every view.
        if self.machine_filter is None:
            return
        names = {self.machine_of(w) for w in self.loaded}
        if self.machine_filter not in names:
            self.machine_filter = None

    def handle_machine_menu_key(self, key: int | str) -> bool:
        options = self.machine_filter_options()
        if not options:
            self.machine_menu = False
            return True
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("menu.machine", key)
        if act in ("down", "advance"):
            self.machine_menu_index = (self.machine_menu_index + 1) % len(options)
        elif act == "up":
            self.machine_menu_index = (self.machine_menu_index - 1) % len(options)
        elif act == "first":
            self.machine_menu_index = 0
        elif act == "last":
            self.machine_menu_index = len(options) - 1
        elif act == "select":
            self.machine_menu = False
            self.select_machine_filter(options[self.machine_menu_index % len(options)][0])
        elif act == "cancel":
            self.machine_menu = False
        return True

    def harness_filter_options(self) -> list[tuple[str, str, bool]]:
        grouped: dict[str, float] = defaultdict(float)
        for w in self.loaded:
            grouped[w.source or "unknown"] += w.total_cost
        names = sorted(grouped, key=lambda n: grouped[n], reverse=True)
        out: list[tuple[str, str, bool]] = [("", "All harnesses", self.harness_filter is None)]
        for name in names:
            out.append((name, name, self.harness_filter == name))
        return out

    def can_harness_filter(self) -> bool:
        # An armed filter must remain reachable even after only its harness remains.
        if not self.machines_present:
            return False
        if self.harness_filter is not None:
            return True
        return len({w.source or "unknown" for w in self.loaded}) >= 2

    def open_harness_menu(self) -> None:
        if not self.can_harness_filter():
            self.notify("only one harness in the fleet", "error")
            return
        options = self.harness_filter_options()
        cur = next((i for i, (v, _l, _a) in enumerate(options) if v == self.harness_filter), 0)
        self.harness_menu_index = cur
        self.harness_menu = True

    def select_harness_filter(self, name: str | None) -> None:
        name = name or None
        if name == self.harness_filter:
            return
        anchor = self.selection_anchor()
        self.harness_filter = name
        self._invalidate_workflow_cache()
        self.restore_selection(anchor)
        self.notify(f"harness: {name}" if name else "harness filter cleared", "success")

    def _revalidate_harness_filter(self) -> None:
        if self.harness_filter is None:
            return
        present = {w.source or "unknown" for w in self.loaded}
        if not self.machines_present or self.harness_filter not in present:
            self.harness_filter = None

    def handle_harness_menu_key(self, key: int | str) -> bool:
        options = self.harness_filter_options()
        if not options:
            self.harness_menu = False
            return True
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("menu.harness", key)
        if act in ("down", "advance"):
            self.harness_menu_index = (self.harness_menu_index + 1) % len(options)
        elif act == "up":
            self.harness_menu_index = (self.harness_menu_index - 1) % len(options)
        elif act == "first":
            self.harness_menu_index = 0
        elif act == "last":
            self.harness_menu_index = len(options) - 1
        elif act == "select":
            self.harness_menu = False
            self.select_harness_filter(options[self.harness_menu_index % len(options)][0])
        elif act == "cancel":
            self.harness_menu = False
        return True

    def open_harness_picker(self) -> None:
        # Swapping stores would discard pulled machines, so fleets filter instead.
        if self.machines_present:
            self.open_harness_menu()
        else:
            self.open_source_menu()

    @property
    def browse_mode_spec(self) -> BrowseMode:
        # Invalid persisted state must not prevent the first frame.
        for mode in self.BROWSE_MODES:
            if mode.key == self.browse_mode:
                return mode
        return self.BROWSE_MODES[0]

    @property
    def flat_browse_mode(self) -> bool:
        return not self.browse_mode_spec.hierarchical

    def mode_tab_list(self) -> list[tuple[str, str]]:
        return [(mode.label, mode.key) for mode in self.BROWSE_MODES]

    def switch_browse_mode(self, mode: str) -> None:
        self.set_browse_mode(mode)

    def refreshable_machines(self) -> list[str]:
        meta = self.machine_meta()
        return [n for n, m in meta.items() if (m or {}).get("key")]

    @property
    def focused_year(self) -> str | None:
        rows = self.years
        if not rows:
            return None
        self.year_index = max(0, min(self.year_index, len(rows) - 1))
        year = rows[self.year_index].year
        return None if year == ALL_YEARS else year

    @property
    def focused_month(self) -> str | None:
        rows = self.months
        if not rows:
            return None
        self.month_index = max(0, min(self.month_index, len(rows) - 1))
        return rows[self.month_index].month

    @property
    def selected(self) -> Workflow | None:
        rows = self.workflows
        if not rows:
            return None
        self.workflow_index = max(0, min(self.workflow_index, len(rows) - 1))
        return rows[self.workflow_index]

    @property
    def selected_day_summary(self) -> DaySummary | None:
        rows = self.panel_days
        if not rows:
            return None
        self.day_index = max(0, min(self.day_index, len(rows) - 1))
        return rows[self.day_index]

    @property
    def selected_month_summary(self) -> MonthSummary | None:
        rows = self.months
        if not rows:
            return None
        self.month_index = max(0, min(self.month_index, len(rows) - 1))
        return rows[self.month_index]

    @property
    def selected_year_summary(self) -> YearSummary | None:
        rows = self.years
        if not rows:
            return None
        self.year_index = max(0, min(self.year_index, len(rows) - 1))
        return rows[self.year_index]

    @property
    def selected_project_summary(self) -> ProjectSummary | None:
        rows = self.projects
        if not rows:
            return None
        self.project_index = max(0, min(self.project_index, len(rows) - 1))
        return rows[self.project_index]

    def active_project_for_toggle(self) -> ProjectSummary | None:
        if self.browse_mode == "projects":
            return self.selected_project_summary
        if self.view == "zoom" and self.on_projects_tab and self.browse_mode != "projects":
            return self.zoom_selected_project()
        return None

    def can_toggle_project_ignore(self) -> bool:
        return self.active_project_for_toggle() is not None

    def can_toggle_ignore(self) -> bool:
        return self.can_toggle_project_ignore() or self.session_ignore_target() is not None

    def toggle_ignored_projects_view(self) -> None:
        if not (self.ignored_projects or self.ignored_sessions):
            self.notify("no ignored items", "error")
            return
        project = self.active_project_for_toggle()
        project_dir = project.directory if project else None
        session = self.session_ignore_target()
        session_id = session.id if session else None
        self.show_ignored_projects = not self.show_ignored_projects
        if not self.show_ignored_projects and self.zoom_project in self.ignored_projects:
            self.zoom_project = None
            if (
                self.view == "zoom"
                and self.browse_mode != "projects"
                and "Projects" in self.current_tabs()
            ):
                self.tab = self.current_tabs().index("Projects")
        if not self.show_ignored_projects and session_id in self.ignored_sessions:
            if self.view == "session":
                self.drill_out()
        self.restore_project_selection(project_dir)
        self.notice = (
            "showing ignored items" if self.show_ignored_projects else "hiding ignored items"
        )

    def toggle_ignore(self) -> None:
        if self.active_project_for_toggle() is not None:
            self.toggle_project_ignore()
            return
        if self.session_ignore_target() is not None:
            self.toggle_session_ignore()
            return
        self.notify("ignore: select a project or session first", "error")

    def toggle_project_ignore(self) -> None:
        project = self.active_project_for_toggle()
        if project is None:
            self.notify("ignore: select a project first", "error")
            return
        directory = project.directory
        if directory in self.ignored_projects:
            self.ignored_projects.remove(directory)
            self.notice = f"unignored {short_path(directory, 40)}"
        else:
            self.ignored_projects.add(directory)
            self.notice = f"ignored {short_path(directory, 40)}"
        self._invalidate_workflow_cache()
        if self.zoom_project in self.ignored_projects and not self.show_ignored_projects:
            self.zoom_project = None
        self.restore_project_selection(directory)

    def session_ignore_target(self) -> Workflow | None:
        if self.view == "session" or (self.view == "zoom" and self.on_sessions_tab):
            return self.current_session()
        return None

    def toggle_session_ignore(self) -> None:
        session = self.session_ignore_target()
        if session is None:
            self.notify("ignore: select a session first", "error")
            return
        if session.id in self.ignored_sessions:
            self.ignored_sessions.remove(session.id)
            self.notice = f"unignored {shorten(session.title, 40)}"
        else:
            self.ignored_sessions.add(session.id)
            self.notice = f"ignored {shorten(session.title, 40)}"
        self._invalidate_workflow_cache()
        if (
            session.id in self.ignored_sessions
            and not self.show_ignored_projects
            and self.view == "session"
        ):
            self.drill_out()

    def restore_project_selection(self, directory: str | None) -> None:
        rows = (
            self.zoom_projects()
            if self.view == "zoom" and self.browse_mode != "projects" and self.on_projects_tab
            else self.projects
        )
        if directory and rows:
            self.project_index = next(
                (i for i, row in enumerate(rows) if row.directory == directory),
                min(self.project_index, len(rows) - 1),
            )
        else:
            self.project_index = min(self.project_index, max(0, len(rows) - 1))

    def bookmark_target(self) -> Workflow | None:
        # Do not memoize: stale sort/filter inputs could make actions target the wrong session.
        if self.view == "session" or (self.view == "zoom" and self.on_sessions_tab):
            return self.current_session()
        return None

    NOTE_MAX_CHARS = 500

    @property
    def allow_notes(self) -> bool:
        # Compute live: real notes would deanonymize demo screens and remain editable.
        return self.notes_enabled and not bool(getattr(self.store, "demo", False))

    def refresh_notes(self) -> bool:
        # Preserve the warning via _notes_ok because same-handler toasts collapse.
        self._notes_ok = True
        if not self.allow_notes:
            self.notes = {}
            return True
        notes, readable = read_notes()
        if not readable:
            # Broken is not empty; keep memory intact rather than implying note deletion.
            self.notify(f"notes: {short_path(notes_path(), 60)} is unreadable", "error")
            self._notes_ok = False
            return False
        self.notes = notes
        return True

    def note_for(self, workflow_id: str) -> str:
        return self.notes.get(workflow_id, "")

    def edit_note(self, stdscr: curses.window) -> None:
        session = self.bookmark_target()
        if session is None:
            self.notify("note: select a session first", "error")
            return
        if not self.allow_notes:
            self.notify("notes are off in demo / --no-state", "error")
            return
        km = self.keymap
        value = self.prompt_text(
            stdscr,
            "note: ",
            f"{km.label('input', 'confirm')} saves · {km.label('input', 'kill_line')} clears · "
            f"{km.label('input', 'cancel')} cancels",
            self.note_for(session.id),
            max_chars=self.NOTE_MAX_CHARS,
        )
        if value is None:
            return
        self.set_note(session, value)

    def set_note(self, session: Workflow, text: str) -> None:
        # Adopt update_note's locked merge; failed writes must not alter in-memory truth.
        text = text.strip()
        previous = self.note_for(session.id)
        notes, error = update_note(session.id, text)
        if error == "unreadable":
            self.notify(
                f"note not saved: {short_path(notes_path(), 60)} is unreadable — "
                "move it aside and it will be rebuilt",
                "error",
            )
            return
        if error:
            self.notify(f"note not saved: cannot write {short_path(notes_path(), 60)}", "error")
            return
        self.notes = notes
        if not text:
            self.notice = "note cleared" if previous else "no note to clear"
        else:
            self.notice = "note saved" if not previous else "note updated"

    def toggle_bookmark(self) -> None:
        session = self.bookmark_target()
        if session is None:
            self.notify("bookmark: select a session first", "error")
            return
        if session.id in self.bookmarks:
            self.bookmarks.discard(session.id)
            self.notice = f"unbookmarked {shorten(session.title, 40)}"
        else:
            self.bookmarks.add(session.id)
            self.notice = f"bookmarked {shorten(session.title, 40)}"
        if self.show_bookmarks_only and session.id not in self.bookmarks:
            if not self.bookmarks:
                self.show_bookmarks_only = False
                self.notice = "last bookmark removed — showing all sessions"
                rows = self.current_sessions()
                self.workflow_index = next(
                    (i for i, w in enumerate(rows) if w.id == session.id),
                    min(self.workflow_index, max(0, len(rows) - 1)),
                )
            elif self.view == "session" and self.current_session() is not session:
                self.drill_out()

    def toggle_bookmarks_view(self) -> None:
        if not self.show_bookmarks_only and not self.bookmarks:
            self.notify(
                f"no bookmarks — press {self.keymap.label('main', 'bookmark')} on a session",
                "error",
            )
            return
        anchor = self.selection_anchor()
        self.show_bookmarks_only = not self.show_bookmarks_only
        self.restore_selection(anchor)
        if self.view == "session" and self.current_session() is None:
            self.drill_out()
        self.notice = (
            "showing bookmarked sessions only"
            if self.show_bookmarks_only
            else "showing all sessions"
        )

    def selection_anchor(self) -> SelectionAnchor:
        # Preserve the synthetic all-years value, which focused_year intentionally maps to None.
        sel_year = self.selected_year_summary
        year = sel_year.year if sel_year else None
        month = self.focused_month
        day = self.active_day if month else None
        project = self.selected_project_summary
        # Empty string safely identifies the fleet row because real machines always have names.
        machine = self.selected_machine_summary if self.browse_mode == "machines" else None
        session = self.current_session()
        return SelectionAnchor(
            year=year,
            month=month,
            day=day,
            project=project.directory if project else None,
            machine=None if machine is None else ("" if machine.fleet else machine.name),
            session=session.id if session else None,
        )

    def restore_selection(self, anchor: SelectionAnchor) -> None:
        year, month, day = anchor.year, anchor.month, anchor.day
        project_dir, machine_name, session_id = anchor.project, anchor.machine, anchor.session

        # Restore parents before children because month/day lists are scoped.
        year_rows = self.years
        if year and year_rows:
            self.year_index = next(
                (i for i, row in enumerate(year_rows) if row.year == year),
                min(self.year_index, len(year_rows) - 1),
            )
        else:
            self.year_index = min(self.year_index, max(0, len(year_rows) - 1))

        month_rows = self.months
        if month and month_rows:
            self.month_index = next(
                (i for i, row in enumerate(month_rows) if row.month == month),
                min(self.month_index, len(month_rows) - 1),
            )
        else:
            self.month_index = min(self.month_index, max(0, len(month_rows) - 1))

        day_rows = self.panel_days
        if day and day_rows:
            self.day_index = next(
                (i for i, row in enumerate(day_rows) if row.day == day),
                min(self.day_index, len(day_rows) - 1),
            )
        else:
            self.day_index = min(self.day_index, max(0, len(day_rows) - 1))

        project_rows = self.projects
        if project_dir and project_rows:
            self.project_index = next(
                (i for i, row in enumerate(project_rows) if row.directory == project_dir),
                min(self.project_index, len(project_rows) - 1),
            )
        else:
            self.project_index = min(self.project_index, max(0, len(project_rows) - 1))

        # Restore the machine before resolving its scoped session list.
        machine_rows = self.machines
        if machine_name is not None and machine_rows:
            found = next(
                (
                    i
                    for i, row in enumerate(machine_rows)
                    if (
                        row.fleet
                        if machine_name == ""
                        else (not row.fleet and row.name == machine_name)
                    )
                ),
                None,
            )
            self.machine_index = (
                found if found is not None else min(self.machine_index, len(machine_rows) - 1)
            )
        else:
            self.machine_index = min(self.machine_index, max(0, len(machine_rows) - 1))

        session_rows = self.current_sessions()
        if session_id and session_rows:
            self.workflow_index = next(
                (i for i, row in enumerate(session_rows) if row.id == session_id),
                min(self.workflow_index, len(session_rows) - 1),
            )
        else:
            self.workflow_index = min(self.workflow_index, max(0, len(session_rows) - 1))
        self.scroll = 0

    def _load_model_cache(self) -> None:
        self._model_by_root: dict[str, list[dict]] = defaultdict(list)
        for row in self.store.model_breakdown():
            self._model_by_root[row["root_id"]].append(dict(row))
        # Reuse the heavy breakdown scan for model_count; compute before demo renaming.
        for w in self.loaded:
            w.model_count = len(self._model_by_root.get(w.id, ()))
        if self.store.demo:
            rename = "titles" in self._demo_cats
            for root_id, models in self._model_by_root.items():
                rows = self._demo_rename_models(models) if rename else models
                self._model_by_root[root_id] = self._scale_demo_models(rows)
            self._reconcile_demo_models()
        else:
            self._reconcile_unpriced_tokens()
            self._compute_api_costs()
        self._models_loaded = True
        self._whatif_catalog_rows = None
        self._apply_price_mode()
        self._revalidate_whatif()

    def _reconcile_unpriced_tokens(self) -> None:
        """Replace coarse rollup counts with the deferred message-level truth.

        OpenCode's fast node rollup can mark a mixed-cost node entirely priced; the model
        scan has the required per-message split without slowing the first frame.
        """
        parts = (
            "unpriced_input",
            "unpriced_output",
            "unpriced_reasoning",
            "unpriced_cache_read",
            "unpriced_cache_write",
        )
        for w in self.loaded:
            rows = self._model_by_root.get(w.id)
            if rows:  # No rows means the scan cannot improve the store's answer.
                w.unpriced_tokens = sum(int(r.get(p) or 0) for r in rows for p in parts)

    def _revalidate_whatif(self) -> None:
        # Usage may disappear after reload, but only loss of a real list rate invalidates a target.
        if not self.whatif_model or has_known_price(self.whatif_model):
            return
        stale, self.whatif_model = self.whatif_model, None
        self.notify(f"what-if cleared — no list rate for {stale}", "warn")

    def _ensure_models(self) -> None:
        if not self._models_loaded:
            anchor = self.selection_anchor()
            self._load_model_cache()
            # API estimates can reorder every cost-ranked session list. Keep the
            # selected session under the cursor instead of silently switching rows.
            self.restore_selection(anchor)

    @property
    def _demo_cats(self) -> frozenset:
        return getattr(self.store, "demo_cats", DEMO_ALL)

    @staticmethod
    def _demo_rename_models(models: list[dict]) -> list[dict]:
        # Merge collisions introduced by demo renaming.
        merged: dict[str, dict] = {}
        for m in models:
            m = dict(m)
            m["model_name"] = demo_model(m["model_name"])
            key = m["model_name"]
            if key in merged:
                acc = merged[key]
                fields = (
                    "runs",
                    "cost",
                    "tokens_total",
                    "input",
                    "reasoning",
                    "cache_read",
                    "cache_write",
                    "output",
                    "unpriced_input",
                    "unpriced_reasoning",
                    "unpriced_cache_read",
                    "unpriced_cache_write",
                    "unpriced_output",
                    "root_cost",
                    "root_unpriced_input",
                    "root_unpriced_reasoning",
                    "root_unpriced_cache_read",
                    "root_unpriced_cache_write",
                    "root_unpriced_output",
                )
                for f in fields:
                    acc[f] = acc.get(f, 0) + m.get(f, 0)
            else:
                merged[key] = m
        return list(merged.values())

    _DEMO_MONEY_FIELDS = ("cost", "root_cost")
    _DEMO_TOKEN_FIELDS = (
        "tokens_total",
        "input",
        "reasoning",
        "cache_read",
        "cache_write",
        "output",
        "unpriced_input",
        "unpriced_reasoning",
        "unpriced_cache_read",
        "unpriced_cache_write",
        "unpriced_output",
        "root_unpriced_input",
        "root_unpriced_reasoning",
        "root_unpriced_cache_read",
        "root_unpriced_cache_write",
        "root_unpriced_output",
    )

    def _scale_demo_models(self, models: list[dict]) -> list[dict]:
        # Scale every magnitude so model rows cannot reveal real totals.
        k = self.store.demo_scale
        for m in models:
            for f in self._DEMO_MONEY_FIELDS:
                if f in m:
                    m[f] = round(m[f] * k, 4)
            for f in self._DEMO_TOKEN_FIELDS:
                if f in m:
                    m[f] = int(round(m[f] * k))
        return models

    def _reconcile_demo_models(self) -> None:
        # Distribute synthetic subscription spend so demo model rows match workflow totals.
        by_id = {w.id: w for w in self.loaded}
        for root_id, models in self._model_by_root.items():
            wf = by_id.get(root_id)
            if not wf:
                continue
            zero_rows = [m for m in models if m["cost"] == 0]
            if not zero_rows:
                continue
            synth_cost = max(0.0, wf.total_cost - sum(m["cost"] for m in models))
            synth_tokens = max(0, wf.total_tokens - sum(m["tokens_total"] for m in models))
            weights = [max(1, m["runs"]) for m in zero_rows]
            total_w = sum(weights)
            for m, w in zip(zero_rows, weights):
                share = w / total_w
                m["cost"] = round(m["cost"] + synth_cost * share, 4)
                m["tokens_total"] = int(m["tokens_total"] + synth_tokens * share)

    def model_mix(self, workflow_id: str) -> list[dict]:
        rows = self._model_by_root.get(workflow_id, [])
        return sorted(rows, key=lambda r: (r["cost"], r["tokens_total"]), reverse=True)

    def model_session_usage(self, workflow_id: str, model: str) -> dict:
        """Return one model's exact contribution to a session at list rates."""
        runs = tokens = 0
        list_cost = 0.0
        estimated = False
        for row in self._model_by_root.get(workflow_id, []):
            if row.get("model_name") != model:
                continue
            split = model_row_split(row)
            row_tokens = int(row.get("tokens_total") or sum(split))
            runs += int(row.get("runs") or 0)
            tokens += row_tokens
            if not is_local_provider(model):
                list_cost += api_equivalent_cost(model, *split, model_row_1h_write(row))
                estimated = estimated or (row_tokens > 0 and not has_known_price(model))
        return {
            "runs": runs,
            "tokens": tokens,
            "list_cost": list_cost,
            "estimated": estimated,
        }

    def model_scope_usage(self, workflows: list[Workflow], model: str) -> dict:
        """Aggregate model_session_usage over the sessions in the active scope."""
        total = {"runs": 0, "tokens": 0, "list_cost": 0.0, "estimated": False}
        for workflow in workflows:
            usage = self.model_session_usage(workflow.id, model)
            total["runs"] += usage["runs"]
            total["tokens"] += usage["tokens"]
            total["list_cost"] += usage["list_cost"]
            total["estimated"] = total["estimated"] or usage["estimated"]
        return total

    def session_supports_tools(self, workflow_id: str) -> bool:
        # Capability is per session in a merged store; unsupported tabs stay absent.
        check = getattr(self.store, "supports_tools", None)
        return bool(check(workflow_id)) if check else False

    def session_tool_rows(self, workflow_id: str) -> list[dict]:
        cached = self._tool_by_session.get(workflow_id)
        if cached is not None:
            return cached
        fetch = getattr(self.store, "tool_breakdown", None)
        rows = [dict(r) for r in fetch(workflow_id)] if fetch else []
        if self.store.demo:
            rows = self._scale_demo_tools(workflow_id, rows)
        self._tool_by_session[workflow_id] = rows
        return rows

    def _scale_demo_tools(self, workflow_id: str, rows: list[dict]) -> list[dict]:
        # Synthetic subscription prices keep demo useful; scaling hides all real magnitudes.
        k = self.store.demo_scale
        synth = "spend" in self._demo_cats
        for r in rows:
            if synth and r.get("cost", 0) == 0 and r.get("tokens_total", 0) > 0:
                r["cost"] = demo_cost(
                    r["tokens_total"], f"{workflow_id}:{r['tool']}:{r['model_name']}"
                )
            for f in ("tokens_total", "input", "output", "reasoning", "cache_read", "cache_write", "cache_write_1h"):  # fmt: skip
                r[f] = int(round(r.get(f, 0) * k))
            r["cost"] = round(r.get("cost", 0) * k, 4)
        return rows

    def session_supports_turns(self, workflow_id: str) -> bool:
        check = getattr(self.store, "supports_turns", None)
        return bool(check(workflow_id)) if check else False

    def session_turn_rows(self, workflow_id: str) -> list[dict]:
        cached = self._turns_by_session.get(workflow_id)
        if cached is not None:
            return cached
        fetch = getattr(self.trace_owner(workflow_id), "message_timeline", None)
        rows = [dict(r) for r in fetch(workflow_id)] if fetch else []
        if self.store.demo:
            rows = self._scale_demo_turns(workflow_id, rows)
        self._turns_by_session[workflow_id] = rows
        return rows

    # --- The turn trace: the third level under Turns ---------------------------
    #
    # Turns answers WHEN the money went (prompts), the drill answers which CALLS made up
    # one prompt, and this answers WHAT one of those calls actually did. Fetched only
    # when a turn is opened -- deliberately NOT in prefetch_session_data, which every
    # drill-in runs: the trace re-reads a session's whole content stream, and most
    # drill-ins never open one.

    def session_supports_trace(self, workflow_id: str) -> bool:
        # Demo traces are static fixtures created above the store boundary. Keep them
        # off unless prompt content is being hidden, and never invent the feature for a
        # backend that cannot open real traces outside demo mode.
        if self.store.demo and "turns" not in self._demo_cats:
            return False
        check = getattr(self.trace_owner(workflow_id), "supports_turn_content", None)
        return bool(check(workflow_id)) if check else False

    def trace_owner(self, workflow_id: str):
        owner = self.store
        wf = self.current_session()
        if wf is None or wf.id != workflow_id:
            matches = [w for w in self.loaded if w.id == workflow_id]
            wf = matches[0] if len(matches) == 1 else None
        while wf is not None and callable(getattr(owner, "owner_of", None)):
            child = owner.owner_of(wf)
            if child is None or child is owner:
                break
            owner = child
        return owner

    def remote_trace_reader(self, workflow_id: str):
        return getattr(self.trace_owner(workflow_id), "remote_trace_request", None)

    def trace_unavailable_reason(self, workflow_id: str) -> str:
        """A configuration cause worth reporting, so Enter is never a silent no-op."""
        reason = getattr(self.trace_owner(workflow_id), "trace_unavailable", None)
        return reason(workflow_id) if reason and not self.store.demo else ""

    def _queue_remote_trace(self) -> None:
        wf = self.current_session()
        idx = self.active_trace_drill
        if wf is None or idx is None or not self.remote_trace_reader(wf.id):
            return
        if self.store.demo or not self.session_supports_trace(wf.id):
            return
        rows = self.session_turn_rows(wf.id)
        if 0 <= idx < len(rows) and (key := rows[idx].get("content_key")):
            self._trace_loading = (wf.id, key)

    def poll_remote_trace(self) -> None:
        pending = self._remote_trace_job
        if pending is None:
            return
        store, wid, key, job = pending
        wf = self.current_session()
        idx = self.active_trace_drill
        rows = self.session_turn_rows(wid) if wf is not None and wf.id == wid else []
        if (
            self.store is not store
            or self.store.demo
            or not self._on_turns_tab()
            or idx is None
            or not 0 <= idx < len(rows)
            or rows[idx].get("content_key") != key
        ):
            self._clear_trace_expansion()
            return
        if not job.done.is_set():
            return
        self._remote_trace_job = None
        self._trace_loading = None
        self._remote_trace_error = job.error
        if job.error:
            self.notify(job.error, "error")
            return
        from opentab.remote_content import trace_preview

        events = list(job.content.get(key) or [])
        self._remote_trace_content = (wid, key, events, trace_preview(events))

    # How many sessions' traces stay in memory. Unlike the other extras -- rows of
    # numbers -- a trace is a session's content: ~1 MB for a 1,500-turn session, and
    # browsing a project's worth of them would accumulate the corpus. It is read one
    # session at a time, so a small ring costs nothing and bounds the total.
    TRACE_MEMO_SESSIONS = 4

    def session_trace(self, workflow_id: str) -> dict:
        if self.remote_trace_reader(workflow_id):
            return {}  # Remote content is keyed, explicit, and never read from rendering.
        if self.store.demo:
            content = {}
            if self.session_supports_trace(workflow_id):
                reasoning = self.session_records_reasoning(workflow_id)
                for row in self.session_turn_rows(workflow_id):
                    if key := row.get("content_key"):
                        content.update(demo_turn_content(key, records_reasoning=reasoning))
        else:
            cached = self._trace_by_session.get(workflow_id)
            if cached is not None:
                return cached
            fetch = getattr(self.trace_owner(workflow_id), "turn_content", None)
            ok = fetch is not None and self.session_supports_trace(workflow_id)
            content = dict(fetch(workflow_id)) if ok else {}
        self._trace_by_session[workflow_id] = content
        while len(self._trace_by_session) > self.TRACE_MEMO_SESSIONS:
            # dicts keep insertion order, so the first key is the oldest fetch.
            self._trace_by_session.pop(next(iter(self._trace_by_session)))
        return content

    def session_records_reasoning(self, workflow_id: str) -> bool:
        # Whether this session's harness writes reasoning TEXT at all. Claude Code
        # writes its thinking blocks empty (the signed blob is all that survives), so
        # the trace says so rather than leaving a hole where the thinking should be.
        check = getattr(self.store, "records_reasoning", None)
        if callable(check):
            return bool(check(workflow_id))
        return bool(check)

    def turn_trace_events(self, workflow_id: str, row: dict) -> list[dict]:
        key = row.get("content_key") or ""
        if not self.session_supports_trace(workflow_id):
            return []
        if self.remote_trace_reader(workflow_id):
            loaded = self._remote_trace_content
            if loaded is not None and loaded[:2] == (workflow_id, key):
                return loaded[2] if self.trace_expanded else loaded[3]
            return []
        if self.trace_expanded and self._trace_full is not None:
            wid, loaded_key, events = self._trace_full
            if (wid, loaded_key) == (workflow_id, key):
                return events
        return list(self.session_trace(workflow_id).get(key) or []) if key else []

    def _clear_trace_expansion(self, *, keep_remote: bool = False) -> None:
        if self._remote_trace_job is not None:
            self._remote_trace_job[3].cancel()
            self._remote_trace_job = None
        if not keep_remote:
            self._remote_trace_content = None
            self._remote_trace_error = ""
        self.trace_expanded = False
        self._trace_open_outputs.clear()
        self._trace_full = None
        self._trace_loading = None

    def toggle_trace_output(self, event_index: int | None = None) -> bool:
        wf = self.current_session()
        idx = self.active_trace_drill
        if not self._on_turns_tab() or wf is None or idx is None or self.trace_expanded:
            return False
        if not self.session_supports_trace(wf.id) or self._trace_loading is not None:
            return False
        rows = self.session_turn_rows(wf.id)
        if not 0 <= idx < len(rows):
            return False
        if event_index is None:
            event_index = self.renderer.trace_output_target()
        events = self.turn_trace_events(wf.id, rows[idx])
        if event_index is None or not 0 <= event_index < len(events):
            return False
        event = events[event_index]
        if event.get("kind") != "tool" or not (event.get("output") or event.get("output_dropped")):
            return False
        if event_index in self._trace_open_outputs:
            self._trace_open_outputs.remove(event_index)
        else:
            key = rows[idx].get("content_key")
            if not key:
                return False
            self._trace_open_outputs.add(event_index)
            if self._trace_full is None:
                if self._remote_trace_content is not None:
                    self._trace_full = self._remote_trace_content[:3]
                else:
                    self._trace_loading = (wf.id, key)
        # Anchor the section when its height changes, including collapse midway through it.
        anchors = [
            line
            for line, index in getattr(self.renderer, "_trace_tool_at", {}).items()
            if index == event_index
        ]
        if anchors:
            self.scroll = min(anchors)
        return True

    def toggle_trace_expansion(self) -> bool:
        wf = self.current_session()
        idx = self.active_trace_drill
        if not self._on_turns_tab() or wf is None or idx is None:
            return False
        if self._remote_trace_job is not None or (
            self.remote_trace_reader(wf.id) and self._remote_trace_content is None
        ):
            return False
        if self.trace_expanded:
            self._clear_trace_expansion(keep_remote=True)
        elif self.session_supports_trace(wf.id):
            rows = self.session_turn_rows(wf.id)
            key = rows[idx].get("content_key") if 0 <= idx < len(rows) else None
            if not key:
                return False
            self.trace_expanded = True
            if self._remote_trace_content is not None:
                self._trace_full = self._remote_trace_content[:3]
            else:
                self._trace_loading = (wf.id, key)
        self.scroll = 0
        return True

    def load_trace_expansion(self) -> None:
        """Resolve the queued read after painting: an empty key requests the preview."""
        if self._remote_trace_job is not None:
            return
        request, self._trace_loading = self._trace_loading, None
        if request is None:
            return
        wid, key = request
        if not self.session_supports_trace(wid):
            self._clear_trace_expansion()
            return
        try:
            reader = self.remote_trace_reader(wid)
            if reader is not None:
                if not self.store.demo and key:
                    from opentab.remote_content import TraceJob

                    job = TraceJob(reader(wid, key))
                    self._remote_trace_job = (self.store, wid, key, job)
                    self._trace_loading = request
                return
            if not key:
                self.session_trace(wid)
                return
            if self.store.demo:
                content = demo_turn_content(
                    key, records_reasoning=self.session_records_reasoning(wid), full=True
                )
            else:
                content = self.trace_owner(wid).turn_content(wid, content_key=key)
            self._trace_full = (wid, key, list(content.get(key) or []))
        except (OSError, ValueError, sqlite3.Error) as exc:
            if not key:
                self._trace_by_session[wid] = {}
            self._clear_trace_expansion()
            if self.remote_trace_reader(wid):
                self._remote_trace_error = str(exc)
            self.notify(f"Could not read this turn: {exc}", "error")

    def step_trace(self, delta: int) -> bool:
        if not self._on_turns_tab() or self.active_trace_drill is None:
            return False
        rows = self.drilled_turn_indices()
        if self.active_trace_drill not in rows:
            return False
        pos = rows.index(self.active_trace_drill)
        target = max(0, min(pos + delta, len(rows) - 1))
        if target != pos:
            self._trace_cursor = target
            self.trace_drill = rows[target]
            self._clear_trace_expansion()
            self.scroll = 0
            self._queue_remote_trace()
        return True

    def drilled_turn_indices(self) -> list[int]:
        """Absolute row indices of the turns the open prompt drill lists."""
        wf = self.current_session()
        i = self.active_turn_drill
        if wf is None or i is None:
            return []
        runs = self.turn_runs(wf.id)
        return list(runs[i]) if 0 <= i < len(runs) else []

    @property
    def active_trace_drill(self) -> int | None:
        # A trace lives inside a prompt drill; when that closes, so does this. Gating on
        # the drill rather than on a second session id keeps one ownership check.
        if self.trace_drill is None or self.active_turn_drill is None:
            return None
        return self.trace_drill

    def _move_trace_cursor(self, delta: int) -> bool:
        rows = self.drilled_turn_indices()
        if not rows:
            return False
        moved = max(0, min(self._trace_cursor + delta, len(rows) - 1))
        if moved == self._trace_cursor:
            return False  # let the pane scroll at the bounds, like the prompt cursor
        self._trace_cursor = moved
        # Ask the renderer to scroll it into view, exactly as the prompt cursor does.
        # Without this a prompt with more turns than fit leaves the viewport at the top
        # while the selection walks off the bottom -- j moves a row nobody can see, and
        # Enter then opens a turn the reader never selected.
        self._turn_follow = True
        return True

    def open_trace_drill(self) -> bool:
        rows = self.drilled_turn_indices()
        if not rows:
            return False
        self._trace_cursor = max(0, min(self._trace_cursor, len(rows) - 1))
        if self.active_trace_drill is None:
            self._trace_list_scroll = self.scroll
        self._clear_trace_expansion()
        self.trace_drill = rows[self._trace_cursor]
        self.scroll = 0
        self._queue_remote_trace()
        return True

    def close_trace_drill(self) -> bool:
        if self.active_trace_drill is None:
            return False
        self.trace_drill = None
        self._clear_trace_expansion()
        self.scroll = self._trace_list_scroll
        self._turn_follow = True
        return True

    def _scale_demo_turns(self, workflow_id: str, rows: list[dict]) -> list[dict]:
        k = self.store.demo_scale
        cats = self._demo_cats
        titles, turns, spend = "titles" in cats, "turns" in cats, "spend" in cats
        synthetic_trace = turns and self.session_supports_trace(workflow_id)
        for n, r in enumerate(rows):
            if titles:
                r["model_name"] = demo_model(r["model_name"])
                # Keep aliases stable per prompt so grouped turns remain grouped.
                if "prompt_title" in r:
                    r["prompt_title"] = demo_title(r.get("prompt_id") or "noprompt")
            if turns and "prompt_full" in r:
                r["prompt_full"] = demo_title(r.get("prompt_id") or "noprompt")
            if synthetic_trace:
                r["content_key"] = f"demo:{n}"
                r["has_text"] = True
                r["has_reasoning"] = self.session_records_reasoning(workflow_id)
            if spend and r.get("cost", 0) == 0 and r.get("tokens_total", 0) > 0:
                r["cost"] = demo_cost(r["tokens_total"], f"{workflow_id}:{n}")
            for f in ("tokens_total", "input", "output", "reasoning", "cache_read", "cache_write", "cache_write_1h"):  # fmt: skip
                r[f] = int(round(r.get(f, 0) * k))
            r["cost"] = round(r.get("cost", 0) * k, 4)
        return rows

    def turn_runs(self, workflow_id: str) -> list[list[int]]:
        """Consecutive same-prompt runs, as absolute indices into the turn rows.

        The renderer's grouping rule, in one place: a prompt ordinal indexes THIS
        sequence everywhere -- the cursor, the drill, and the trace level under it --
        so a second copy of the run rule would eventually open a different prompt's
        turns than the header above them names.
        """
        rows = self.session_turn_rows(workflow_id)
        cached = self._turn_runs_cache
        if cached is not None and cached[0] is rows and cached[1] == len(rows):
            return cached[2]
        runs: list[list[int]] = []
        last: object = object()
        for n, r in enumerate(rows):
            pid = r.get("prompt_id", "")
            if pid != last:
                runs.append([])
                last = pid
            runs[-1].append(n)
        self._turn_runs_cache = (rows, len(rows), runs)
        return runs

    def turn_groups(self, workflow_id: str) -> list[str]:
        rows = self.session_turn_rows(workflow_id)
        return [rows[run[0]].get("prompt_id", "") for run in self.turn_runs(workflow_id)]

    def _on_turns_tab(self) -> bool:
        return self.view == "session" and self.active_tab_name() == "Turns"

    def _move_turn_cursor(self, delta: int) -> bool:
        wf = self.current_session()
        groups = self.turn_runs(wf.id) if wf else []
        if not groups:
            return False
        moved = max(0, min(self._turn_cursor + delta, len(groups) - 1))
        if moved == self._turn_cursor:
            # Let the pane scroll at cursor bounds so footnotes remain reachable.
            return False
        self._turn_cursor = moved
        self._turn_follow = True
        return True

    def _toggle_turn_cursor(self) -> bool:
        wf = self.current_session()
        if wf is None:
            return False
        # Inside a prompt Enter opens the selected turn; inside the reader it toggles
        # the output section at the viewport top (or the next below it).
        if self.active_turn_drill is not None:
            if self.active_trace_drill is not None:
                self.toggle_trace_output()
                return True
            if self.session_supports_trace(wf.id):
                return self.open_trace_drill()
            if reason := self.trace_unavailable_reason(wf.id):
                self.notify(reason, "warn")
            return False
        groups = self.turn_runs(wf.id)
        if not groups:
            return False
        self._turn_cursor = max(0, min(self._turn_cursor, len(groups) - 1))
        self.open_turn_drill(self._turn_cursor)
        return True

    def turn_cursor_ordinal(self) -> str:
        wf = self.current_session()
        groups = self.turn_runs(wf.id) if wf else []
        return f"{min(self._turn_cursor + 1, len(groups))} of {len(groups)}" if groups else "-"

    @property
    def active_turn_drill(self) -> int | None:
        wf = self.current_session()
        if self.turn_drill is None or wf is None or self._turn_drill_session != wf.id:
            return None
        return self.turn_drill

    def open_turn_drill(self, ordinal: int) -> None:
        # Prompt ids may repeat, so the drill identifies a consecutive run by ordinal.
        wf = self.current_session()
        self._turn_drill_session = wf.id if wf else None
        self.turn_drill = ordinal
        self._clear_trace_expansion()
        self.trace_drill = None  # a fresh drill opens on its turn list, never in a trace
        self._trace_cursor = 0
        self.scroll = 0
        self._turn_follow = False

    def close_turn_drill(self) -> bool:
        # Consume Esc only for this session's visible drill. Inactive drills belong to
        # another mode's remembered session and must remain untouched.
        if self.active_turn_drill is None:
            return False
        self.turn_drill = None
        self._turn_drill_session = None
        self._clear_trace_expansion()
        self.trace_drill = None
        self._trace_cursor = 0
        self.scroll = 0
        self._turn_follow = True
        return True

    def session_supports_context(self, workflow_id: str) -> bool:
        check = getattr(self.store, "supports_context", None)
        return bool(check(workflow_id)) if check else False

    def session_supports_context_curve(self, workflow_id: str) -> bool:
        # Context curves require per-request prompt sizes; cumulative-delta backends opt out.
        if not self.session_supports_turns(workflow_id):
            return False
        check = getattr(self.store, "supports_context_curve", None)
        return bool(check(workflow_id)) if check else True

    def session_context_rows(self, workflow_id: str) -> list[dict]:
        cached = self._context_by_session.get(workflow_id)
        if cached is not None:
            return cached
        fetch = getattr(self.store, "context_breakdown", None)
        rows = [dict(r) for r in fetch(workflow_id)] if fetch else []
        if self.store.demo:
            k = self.store.demo_scale
            for r in rows:
                r["est_tokens"] = int(round(r["est_tokens"] * k))
        self._context_by_session[workflow_id] = rows
        return rows

    def session_data_ready(self, workflow_id: str) -> bool:
        # This predicate must remain store-fetch-free; drawing uses it before the loading frame.
        if workflow_id not in self._nodes_by_session:
            return False
        if self.session_supports_turns(workflow_id) and workflow_id not in self._turns_by_session:
            return False
        if self.session_supports_tools(workflow_id) and workflow_id not in self._tool_by_session:
            return False
        if (
            self.session_supports_context(workflow_id)
            and workflow_id not in self._context_by_session
        ):
            return False
        return True

    def prefetch_session_data(self, workflow_id: str) -> None:
        # Mirror session_data_ready's gates so one prefetch always ends the loading loop.
        self.session_node_rows(workflow_id)
        if self.session_supports_turns(workflow_id):
            self.session_turn_rows(workflow_id)
        if self.session_supports_tools(workflow_id):
            self.session_tool_rows(workflow_id)
        if self.session_supports_context(workflow_id):
            self.session_context_rows(workflow_id)

    def session_node_rows(self, workflow_id: str) -> list[dict]:
        # Memoize the recursive query/backend parse; repricing copies these rows.
        cached = self._nodes_by_session.get(workflow_id)
        if cached is not None:
            return cached
        rows = [dict(r) for r in self.store.workflow_nodes(workflow_id)]
        self._nodes_by_session[workflow_id] = rows
        return rows

    def _snapshot_real_costs(self) -> None:
        # Seed both snapshots before the deferred model scan can compute estimates.
        for w in self.loaded:
            w.real_total_cost = w.api_total_cost = w.total_cost
            w.real_root_cost = w.api_root_cost = w.root_cost

    def _compute_api_costs(self) -> None:
        # Model rows may mix metered and subscription calls. Add list prices only for
        # unpriced tokens, always from real snapshots to avoid compounding refreshes.
        by_id = {w.id: w for w in self.loaded}
        for root_id, rows in self._model_by_root.items():
            has_root_split = any("root_unpriced_input" in m for m in rows)
            root_delta = 0.0
            for m in rows:
                real = m["real_cost"] = m.get("real_cost", m["cost"])
                # Legacy in-memory rows may expose only aggregate tokens.
                all_unpriced = real == 0 and "unpriced_input" not in m
                m["api_cost"] = real + api_equivalent_cost(
                    m["model_name"],
                    m.get("input", 0) if all_unpriced else m.get("unpriced_input", 0),
                    m.get("output", 0) if all_unpriced else m.get("unpriced_output", 0),
                    m.get("reasoning", 0) if all_unpriced else m.get("unpriced_reasoning", 0),
                    m.get("cache_read", 0) if all_unpriced else m.get("unpriced_cache_read", 0),
                    m.get("cache_write", 0) if all_unpriced else m.get("unpriced_cache_write", 0),
                    m.get("cache_write_1h", 0)
                    if all_unpriced
                    else m.get("unpriced_cache_write_1h", 0),
                )
                if has_root_split:
                    root_delta += api_equivalent_cost(
                        m["model_name"],
                        m.get("root_unpriced_input", 0),
                        m.get("root_unpriced_output", 0),
                        m.get("root_unpriced_reasoning", 0),
                        m.get("root_unpriced_cache_read", 0),
                        m.get("root_unpriced_cache_write", 0),
                        m.get("root_unpriced_cache_write_1h", 0),
                    )
            wf = by_id.get(root_id)
            if not wf:
                continue
            delta = sum(m["api_cost"] - m["real_cost"] for m in rows)
            wf.api_total_cost = wf.real_total_cost + delta
            if has_root_split:
                wf.api_root_cost = wf.real_root_cost + root_delta
            else:
                # Legacy rows lack exact root-vs-subagent token splits.
                frac = wf.real_root_cost / wf.real_total_cost if wf.real_total_cost else 1.0
                wf.api_root_cost = wf.real_root_cost + delta * frac

    def _apply_price_mode(self) -> None:
        # What-if must not enter this app-wide `$` path; it is session-scoped only.
        api = self.show_api_prices and not self.store.demo
        for w in self.loaded:
            w.total_cost = w.api_total_cost if api else w.real_total_cost
            w.root_cost = w.api_root_cost if api else w.real_root_cost
        for rows in self._model_by_root.values():
            for m in rows:
                m["cost"] = m.get("api_cost", m["cost"]) if api else m.get("real_cost", m["cost"])

    def whatif_candidates(self) -> list[tuple[str, int]]:
        # Never offer local or fallback-priced targets as real list-rate comparisons.
        totals: dict[str, int] = defaultdict(int)
        for rows in self._model_by_root.values():
            for m in rows:
                name = str(m.get("model_name") or "")
                if not name or not has_known_price(name):
                    continue
                totals[name] += int(m.get("tokens_total") or 0)
        # Zero-token rows name aborted turns, not actual model usage.
        return sorted(
            ((name, tok) for name, tok in totals.items() if tok > 0),
            key=lambda kv: (-kv[1], kv[0]),
        )

    def whatif_catalog_candidates(self) -> list[tuple[str, float, bool]]:
        # Fold aliases and gateway rows because model_price() arms one vendor-preferred
        # rate card per canonical model. Rank by the app-wide mix, never machine filters.
        if self._whatif_catalog_rows is not None:
            return self._whatif_catalog_rows
        mix = self._token_mix(self._model_by_root)
        shares = mix[0] if mix else (1.0, 0.0, 0.0, 0.0)
        best: dict[str, tuple[tuple, str]] = {}
        for pid, mid, price, _status in catalog_models():
            if pid.lower() in LOCAL_PROVIDERS or (price[0] <= 0 and price[1] <= 0):
                continue
            bare = mid.rsplit("/", 1)[-1].lower()
            rank = (
                is_vendor_route(pid, bare),
                sum(1 for v in model_price(mid) if v > 0),
                -len(mid),
            )
            canon = canonical_model(bare)
            cur = best.get(canon)
            if cur is None or rank > cur[0]:
                best[canon] = (rank, f"{pid}/{mid}")
        rows = []
        for _rank, name in best.values():
            eff, approx = effective_price(model_price(name), shares)
            rows.append((name, eff, approx))
        rows.sort(key=lambda r: (r[1], r[0]))
        self._whatif_catalog_rows = rows
        return rows

    def whatif_session_totals(self, workflow: Workflow) -> tuple[float, float] | None:
        """Return (actual-model list cost, all-at-target list cost) for one session.

        Both sides must use per-model rows and list rates. Node rows expose only a
        dominant model and misprice model switches (observed worst case: 47%); recorded
        subscription cost would make the baseline falsely zero. This basis also ensures
        targeting a single-model session's own model produces exactly zero change.
        """
        target = self.whatif_model
        if not target:
            return None
        rows = self._model_by_root.get(workflow.id) or []
        if not rows:
            return None
        baseline = 0.0
        tokens = [0.0, 0.0, 0.0, 0.0, 0.0]
        long_write = 0.0
        for m in rows:
            split = model_row_split(m)
            long_1h = model_row_1h_write(m)
            baseline += api_equivalent_cost(str(m.get("model_name") or ""), *split, long_1h)
            tokens = [a + b for a, b in zip(tokens, split)]
            long_write += long_1h
        # Preserve the 1h-write subset on both sides of the exact comparison.
        return baseline, api_equivalent_cost(target, *tokens, long_write)

    def token_economics(
        self, workflows: list[Workflow], model: str | None = None
    ) -> TokenEconomics | None:
        """Split priceable tokens and their list-rate cost by token type.

        Per-model rows preserve the producing rate card across model switches. Local
        tokens are excluded from both distributions, and missing cache-read rates follow
        api_equivalent_cost's zero-rate arithmetic so the pieces match the displayed total.
        """
        tokens = [0.0] * len(TOKEN_TYPES)
        cost = [0.0] * len(TOKEN_TYPES)
        estimated = missing_cache_rate = False
        local_tokens = 0
        for workflow in workflows:
            for row in self._model_by_root.get(workflow.id) or []:
                name = str(row.get("model_name") or "")
                if model is not None and name != model:
                    continue
                split = model_row_split(row)
                if is_local_provider(name):
                    local_tokens += int(sum(split))
                    continue
                inp, out, reasoning, cache_read, cache_write = split
                ir, orr, crr, cwr = model_price(name)
                for i, n in enumerate(split):
                    tokens[i] += n
                cost[0] += inp * ir / 1e6
                cost[1] += out * orr / 1e6
                cost[2] += reasoning * orr / 1e6
                cost[3] += cache_read * crr / 1e6
                long_write = min(max(model_row_1h_write(row), 0.0), cache_write)
                cost[4] += (
                    (cache_write - long_write) * cwr + long_write * cache_write_1h_price(name)
                ) / 1e6
                if crr <= 0 and cache_read > 0 and ir > 0:
                    missing_cache_rate = True
                if sum(split) > 0 and not has_known_price(name):
                    estimated = True
        if sum(tokens) <= 0:
            return None
        return TokenEconomics(
            tuple(tokens), tuple(cost), estimated, missing_cache_rate, local_tokens
        )

    @staticmethod
    def _flame_labels(rows: list[dict]) -> list[str]:
        # Disambiguate repeated generic agent names with visible start times, then rank.
        labels = [flame_label(row) for row in rows]
        repeated = {lab for lab in labels if labels.count(lab) > 1}
        if not repeated:
            return labels
        for end in (16, 19):
            stamped = [
                f"{lab} {str(row.get('created_at') or '')[11:end]}".strip()
                if lab in repeated
                else lab
                for lab, row in zip(labels, rows)
            ]
            if len(set(stamped)) == len(stamped):
                return stamped
        # Existing "#N" labels can collide with rank suffixes, so enforce uniqueness.
        seen: set[str] = set()
        out = []
        for i, lab in enumerate(labels):
            name = f"{lab} #{i + 1}" if lab in repeated else lab
            while name in seen:
                name += " ·"
            seen.add(name)
            out.append(name)
        return out

    def session_flame(self, workflow: Workflow) -> SessionFlame | None:
        """Build proportions from the table's effective node costs.

        Zero-value nodes have no drawable width but remain counted as silent executions.
        """
        nodes = self.session_node_rows(workflow.id)
        if not nodes:
            return None
        priced = self._priced_nodes(nodes)
        cost = sum(float(row["cost"] or 0) for row in priced)
        # Token proportions keep subscription sessions useful when `$` estimates are off.
        unit = "cost" if cost > 0 else "tokens"

        def value(row: dict) -> float:
            return float(row["cost"] or 0) if unit == "cost" else float(row["tokens_total"] or 0)

        total = cost if unit == "cost" else float(sum(value(r) for r in priced))
        if total <= 0:
            return None
        segments: list[FlameSegment] = []
        roots = [r for r in priced if int(r["depth"]) == 0]
        own = sum(value(r) for r in roots)
        if own > 0:
            segments.append(
                FlameSegment(
                    "root (self)",
                    "root (self)",
                    flame_model(roots[0]),
                    own,
                    own / total,
                    FLAME_SELF_SLOT,
                    0,
                )
            )
        kids = [r for r in priced if int(r["depth"]) > 0]
        # Stable ordering prevents colors from shuffling between paints.
        kids.sort(
            key=lambda r: (value(r), int(r["tokens_total"] or 0), str(r["title"])), reverse=True
        )
        drawn = [r for r in kids if value(r) > 0]
        labels = self._flame_labels(drawn)
        for i, row in enumerate(drawn):
            v = value(row)
            label = labels[i]
            segments.append(
                FlameSegment(
                    label,
                    flame_label(row),
                    flame_model(row),
                    v,
                    v / total,
                    FLAME_CHILD_SLOTS[i % len(FLAME_CHILD_SLOTS)],
                    int(row["depth"]),
                )
            )
        # Only drawn estimated widths count; aborted zero-width children must not mark the chart.
        api = self.show_api_prices and not self.store.demo
        estimated = (
            unit == "cost"
            and api
            and any(not raw["cost"] and value(row) > 0 for raw, row in zip(nodes, priced))
        )
        return SessionFlame(
            tuple(segments),
            total,
            unit,
            estimated,
            sum(1 for r in drawn if int(r["depth"]) > 1),
            len(kids) - len(drawn),
        )

    def whatif_baseline_is_estimated(self, workflow: Workflow) -> bool:
        # Ignore aborted zero-token rows; only contributing fallback rates make `~` truthful.
        return any(
            int(m.get("tokens_total") or 0) > 0
            and not has_known_price(str(m.get("model_name") or ""))
            and not is_local_provider(str(m.get("model_name") or ""))
            for m in self._model_by_root.get(workflow.id) or []
        )

    def whatif_node_price(self, row: dict, target: str) -> float:
        # A target cost is exact; a mixed-model node baseline is not available.
        return api_equivalent_cost(
            target,
            row["tokens_input"],
            row["tokens_output"],
            row["tokens_reasoning"],
            row["tokens_cache_read"],
            row["tokens_cache_write"],
            # Cache TTL belongs to the prompt, not the model answering it.
            node_1h_write(row),
        )

    def toggle_whatif(self) -> None:
        # Demo scaling hides absolute what-if spend while preserving its ratio.
        if self.whatif_model:
            self.clear_whatif_model()
            return
        self._ensure_models()
        self.whatif_catalog = False
        if not self.whatif_candidates():
            if not self.whatif_catalog_candidates():
                self.notify("no models to arm — the price catalog is unavailable", "error")
                return
            self.whatif_catalog = True
        self.whatif_menu_index = 0
        self.whatif_query = ""
        self.whatif_filter_active = False
        self.whatif_menu = True

    def whatif_rows(self) -> list[tuple]:
        # Share model_matches with Prices and preserve each tier's ranking after filtering.
        rows = self.whatif_catalog_candidates() if self.whatif_catalog else self.whatif_candidates()
        if not self.whatif_query:
            return list(rows)
        out = []
        for row in rows:
            route, _, bare = row[0].rpartition("/")
            if model_matches(self.whatif_query, bare, (route,) if route else ()):
                out.append(row)
        return out

    def whatif_toggle_catalog(self) -> None:
        # Preserve the query, but never switch into an intrinsically empty tier.
        other = (
            self.whatif_candidates() if self.whatif_catalog else self.whatif_catalog_candidates()
        )
        if not other:
            return
        self.whatif_catalog = not self.whatif_catalog
        self.whatif_menu_index = 0

    def select_whatif_model(self, name: str) -> None:
        # Never reprice app-wide here: that would make `$` inert while still toggling its state.
        self.whatif_model = name
        self.notice = f"what-if {name}: see a session's Subagents tab"

    def clear_whatif_model(self) -> None:
        self.whatif_model = None
        self.notice = "what-if off"

    def _whatif_pick(self, rows: list[tuple[str, int]]) -> None:
        if not rows:
            return
        self.whatif_menu = False
        self.select_whatif_model(rows[self.whatif_menu_index % len(rows)][0])

    def handle_whatif_menu_key(self, key: int | str) -> bool:
        if key == 3:  # Ctrl-C still quits
            return False
        if not self.whatif_candidates() and not self.whatif_catalog_candidates():
            self.whatif_menu = False
            return True
        rows = self.whatif_rows()
        if self.whatif_filter_active:
            # While typing, only non-text catalog keys may switch tiers.
            if (
                self.keymap.action("menu.whatif.filter", key) is None
                and bindings.typed_char(key) is None
                and self.keymap.is_action("menu.whatif", key, "catalog")
            ):
                self.whatif_toggle_catalog()
                return True
            return self._handle_whatif_filter_key(key, rows)
        act = self.keymap.action("menu.whatif", key)
        if act == "filter":
            self.whatif_filter_active = True
        elif act == "catalog":
            self.whatif_toggle_catalog()
        elif act in ("down", "advance") and rows:
            self.whatif_menu_index = (self.whatif_menu_index + 1) % len(rows)
        elif act == "up" and rows:
            self.whatif_menu_index = (self.whatif_menu_index - 1) % len(rows)
        elif act == "first":
            self.whatif_menu_index = 0
        elif act == "last" and rows:
            self.whatif_menu_index = len(rows) - 1
        elif act == "select":
            self._whatif_pick(rows)
        elif act == "cancel":
            self.whatif_menu = False
        return True

    def _handle_whatif_filter_key(self, key: int | str, rows: list[tuple[str, int]]) -> bool:
        act = self.keymap.action("menu.whatif.filter", key)
        if act == "cancel":
            self.whatif_query = ""
            self.whatif_filter_active = False
            self.whatif_menu_index = 0
        elif act == "select":
            self._whatif_pick(rows)
        elif act == "erase":
            self.whatif_query = self.whatif_query[:-1]
            self.whatif_menu_index = 0
        elif act == "clear":
            self.whatif_query = ""
            self.whatif_menu_index = 0
        elif act == "down" and rows:
            self.whatif_menu_index = (self.whatif_menu_index + 1) % len(rows)
        elif act == "up" and rows:
            self.whatif_menu_index = (self.whatif_menu_index - 1) % len(rows)
        elif (ch := bindings.typed_char(key)) is not None:
            self.whatif_query += ch
            self.whatif_menu_index = 0
        return True

    def _reprice_in_place(self) -> None:
        # Repricing is a resort: on a measured corpus `$` moved 106/117 project rows.
        # Re-anchor every cursor by value or Enter can open an unselected neighbor.
        anchor = self.selection_anchor()
        trend_key = self.selected_trend_key()
        drill_id = self.selected_trend_drill_id()
        picked = [
            (self.zoom_selected_source(), "source_index", self.zoom_source_rows),
            (self.zoom_selected_model(), "model_pick_index", self.zoom_model_rows),
            (self.zoom_selected_machine(), "machine_pick_index", self.zoom_machine_rows),
        ]
        zoom_project = None if self.browse_mode == "projects" else self.zoom_selected_project()
        scroll = self.scroll
        self._apply_price_mode()
        self.restore_selection(anchor)
        self.scroll = scroll
        if trend_key is not None:
            keys = self.trend_ranked_keys()
            self.trend_row_index = keys.index(trend_key) if trend_key in keys else 0
        if drill_id is not None:
            ids = [w.id for w, _cost, _tokens in self.trend_drill_sessions()]
            self.trend_drill_index = ids.index(drill_id) if drill_id in ids else 0
        for value, attr, rows_of in picked:
            if value is None:
                continue
            names = [name for name, _row in rows_of()]
            setattr(self, attr, names.index(value) if value in names else 0)
        if zoom_project is not None:
            dirs = [p.directory for p in self.zoom_projects()]
            self.project_index = (
                dirs.index(zoom_project.directory) if zoom_project.directory in dirs else 0
            )

    def selected_trend_drill_id(self) -> str | None:
        rows = self.trend_drill_sessions()
        if not rows:
            return None
        return rows[max(0, min(self.trend_drill_index, len(rows) - 1))][0].id

    def toggle_api_prices(self) -> None:
        if self.store.demo:
            self.notify("API-price view is for real data, not the demo", "error")
            return
        self._ensure_models()
        self.show_api_prices = not self.show_api_prices
        self._reprice_in_place()
        self.notice = (
            "what-if prices (what unpriced usage would cost at API list prices)"
            if self.show_api_prices
            else "actual cost"
        )

    def refresh_prices_action(self) -> None:
        self.notice = "fetching prices from models.dev…"
        try:
            count, _ = refresh_model_prices()
        except (OSError, ValueError) as exc:
            self.notify(f"price refresh failed: {exc}", "error")
            return
        invalidate_price_cache()
        self.renderer._turn_layout_cache = None
        self._whatif_catalog_rows = None
        self._ensure_models()
        self._compute_api_costs()
        self._reprice_in_place()
        # _ensure_models is already satisfied, so explicitly reject a now-unpriced target.
        self._revalidate_whatif()
        self.prices_scroll = 0
        self.notify(f"refreshed {count} model prices from models.dev", "success")

    def unknown_priced_models(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for rows in self._model_by_root.values():
            for m in rows:
                name = m.get("model_name")
                if not name or name in seen:
                    continue
                seen.add(name)
                if not is_local_provider(name) and not has_known_price(name):
                    out.append(name)
        return sorted(out)

    def maybe_prompt_prices(self) -> None:
        # Once per run, after the model scan: if usage includes models we have no real
        # price for (and there are unpriced tokens to estimate), offer to fetch from
        # models.dev. Skipped in demo, under --no-state, once "don't ask again" is set,
        # or once a cache has already been fetched (re-fetching wouldn't add them).
        if self._price_prompt_done:
            return
        self._price_prompt_done = True
        if not self.allow_price_prompt or self.store.demo or self.prices_prompt_dismissed:
            return
        if price_cache_meta() is not None:
            return
        if not self.store.summary(self.all_workflows).get("unpriced_tokens"):
            return
        unknown = self.unknown_priced_models()
        if not unknown:
            return
        self.unknown_models = unknown
        self.price_prompt = True

    def startup_warnings(self) -> list[dict]:
        """The warning on screen plus the ones queued behind it, in order."""
        shown = [self.startup_warning] if self.startup_warning is not None else []
        return shown + list(self._pending_warnings)

    def offer_startup_warning(self, warning: dict, can_persist: bool = True) -> None:
        warning_id = warning.get("id")
        if not isinstance(warning_id, str) or not warning_id:
            return
        if warning_id in self.dismissed_startup_warnings:
            return
        if any(queued.get("id") == warning_id for queued in self.startup_warnings()):
            return
        # Every offer in a launch shares one persistence rule (the state file is either
        # writable or it isn't), so the flag is per-app rather than per-warning.
        self.startup_warning_can_persist = can_persist
        if self.startup_warning is None:
            self.startup_warning = dict(warning)
        else:
            self._pending_warnings.append(dict(warning))

    def _next_startup_warning(self) -> None:
        self.startup_warning = self._pending_warnings.pop(0) if self._pending_warnings else None

    def handle_startup_warning_key(self, key: int | str) -> bool:
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("prompt.warning", key)
        if act == "continue":
            self._next_startup_warning()
        elif act == "never":
            warning = self.startup_warning or {}
            warning_id = warning.get("id")
            self._next_startup_warning()
            if self.startup_warning_can_persist and isinstance(warning_id, str):
                self.dismissed_startup_warnings.add(warning_id)
                self.notice = "warning dismissed — doctor will still report the retention"
            else:
                self.notice = "warning closed for this run — --no-state cannot remember dismissal"
        return True

    def handle_price_prompt_key(self, key: int | str) -> bool:
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("prompt.prices", key)
        if act == "accept":
            self.price_prompt = False
            self.refresh_prices_action()
        elif act == "never":
            self.price_prompt = False
            self.prices_prompt_dismissed = True
            self.notice = f"won't ask again — {self.price_fetch_hint()}"
        else:
            self.price_prompt = False
            self.notice = f"skipped — {self.price_fetch_hint()}"
        return True

    def price_fetch_hint(self) -> str:
        return (
            "fetch anytime with --refresh-models or "
            f"{self.keymap.label('prices', 'refresh')} in the "
            f"{self.keymap.label('main', 'prices')} prices view"
        )

    def reload(self) -> None:
        self._clear_trace_expansion()
        self.loaded = self.store.workflows()
        self._snapshot_real_costs()
        self._resolve_project_roots()
        notes_ok = self.refresh_notes()
        self._tool_by_session.clear()
        self._turns_by_session.clear()
        self._turn_runs_cache = None
        self.renderer._turn_layout_cache = None
        self._trace_by_session.clear()
        self._clear_trace_expansion()
        self.turn_drill = None
        self.trace_drill = None
        self._turn_cursor = 0
        self._trace_cursor = 0
        self._context_by_session.clear()
        self._nodes_by_session.clear()
        self._load_model_cache()
        self._clear_zoom_drills()
        # Dormant mode snapshots must not restore drills removed by changed data.
        self._disarm_mode_memory_drills()
        self._revalidate_machine_filter()
        self._revalidate_harness_filter()
        self.workflow_index = min(self.workflow_index, max(0, len(self.workflows) - 1))
        self.day_index = min(self.day_index, max(0, len(self.days) - 1))
        self.month_index = min(self.month_index, max(0, len(self.months) - 1))
        self.project_index = min(self.project_index, max(0, len(self.projects) - 1))
        self.machine_index = min(self.machine_index, max(0, len(self.machines) - 1))
        if notes_ok:
            self.notify("reloaded", "success")

    def can_refresh_machines(self) -> bool:
        return self.machines_present and not self.store.demo

    def refresh_target(self) -> str | None:
        if self.browse_mode == "machines":
            machine = self.selected_machine_summary
            if machine and not machine.fleet:
                return machine.name
        return None

    def _refresh_keys(self, names: list[str] | None) -> list[str]:
        meta = self.machine_meta()
        if names is None:
            return [str(m["key"]) for m in meta.values() if (m or {}).get("key")]
        return [str(k) for n in names if (k := (meta.get(n) or {}).get("key"))]

    def request_machine_refresh(self, name: str | None = None) -> None:
        # Defer SSH until the progress toast has painted.
        if self.store.demo:
            self.notify("refresh disabled in demo", "error")
            return
        meta = self.machine_meta()
        if name and (meta.get(name) or {}).get("live"):
            self.reload()
            return
        if self._refresh_backend is None:
            self.notify("refresh needs a pulled fleet (opentab pull)", "error")
            return
        keys = self._refresh_keys([name] if name else None)
        if not keys:
            self.notify("nothing to re-pull (this is your live machine)", "error")
            return
        self._refresh_request = keys
        self.notify(f"refreshing {name or 'all machines'} — ssh…")

    def _rebuild_fleet_store(self) -> None:
        # RemoteStore caches parsed summaries, so refresh requires reconstruction under
        # the current full demo-category state and replacement of the matching cache slot.
        state = self._store_state_key(self.store)
        self.store = sources.make_store(self._args_with_demo(state), self.source_key)[0]
        self._store_cache[(self.source_key, state)] = self.store

    def refresh_machines_now(self, name: str | None = None) -> list:
        # Demo must never trigger the web endpoint's network side effects.
        if self.store.demo or self._refresh_backend is None:
            return []
        keys = self._refresh_keys([name] if name else None)
        if not keys:
            return []
        results = self._refresh_backend(keys) or []
        self._rebuild_fleet_store()
        self.reload()
        return results

    def _do_refresh(self, keys: list[str]) -> None:
        self._clear_trace_expansion()
        try:
            results = self._refresh_backend(keys) or []
        except Exception as exc:  # noqa: BLE001 -- a refresh must never crash the TUI
            self.notify(f"refresh failed: {exc}", "error")
            return
        snapshot = self.ui_snapshot()
        try:
            self._rebuild_fleet_store()
        except SystemExit as exc:
            self.notify(str(exc), "error")
            return
        self._reload_for_source(snapshot)
        errs = [f"{key}: {err}" for key, _count, err in results if err]
        ok = [(key, count) for key, count, err in results if not err]
        if errs:
            self.notify("refresh — " + "; ".join(errs), "error")
        elif ok:
            total = sum(count for _key, count in ok)
            self.notify(f"refreshed {len(ok)} machine(s) · {total} sessions", "success")
        else:
            self.notify("nothing refreshed", "error")

    def can_switch_source(self) -> bool:
        return len(sources.source_cycle(self.args)) > 1

    @staticmethod
    def _store_state_key(store) -> frozenset | None:
        return store.demo_cats if getattr(store, "demo", False) else None

    def _args_with_demo(self, state) -> argparse.Namespace:
        args = copy.copy(self.args)
        args.demo = ",".join(sorted(state)) if state else None
        return args

    def next_source_name(self) -> str:
        order = sources.source_cycle(self.args)
        cur = self.source_key if self.source_key in order else order[0]
        nxt = order[(order.index(cur) + 1) % len(order)]
        return SOURCE_LABELS.get(nxt, nxt)

    def source_menu_entries(self) -> list[tuple[str, str, bool]]:
        out = []
        for skey in sources.source_cycle(self.args):
            label = "All sources (merged)" if skey == "all" else SOURCE_LABELS.get(skey, skey)
            out.append((skey, label, skey == self.source_key))
        return out

    def open_source_menu(self) -> None:
        order = sources.source_cycle(self.args)
        if len(order) < 2:
            self.notify("only one harness available", "error")
            return
        cur = self.source_key if self.source_key in order else order[0]
        self.source_menu_index = order.index(cur)
        self.source_menu = True

    def theme_menu_entries(self) -> list[tuple[str, str, bool]]:
        return [(tid, t["name"], tid == self.theme_id) for tid, t in themes.THEMES.items()]

    def open_theme_menu(self) -> None:
        ids = list(themes.THEMES)
        self.theme_menu_index = ids.index(self.theme_id) if self.theme_id in ids else 0
        self._theme_before = self.theme_id
        self.theme_menu = True

    def select_theme(self, theme_id: str, announce: bool = True) -> None:
        if theme_id not in themes.THEMES:
            return
        self.theme_id = theme_id
        self.theme = themes.resolve_theme(theme_id)
        try:
            self.renderer.init_theme_colors()
        except Exception:  # noqa: BLE001 -- a hostile terminal must never crash a switch
            pass
        if announce:
            self.notice = f"theme: {self.theme['name']}"

    def _preview_theme_at(self, index: int) -> None:
        ids = list(themes.THEMES)
        self.theme_menu_index = index % len(ids)
        self.select_theme(ids[self.theme_menu_index], announce=False)

    def handle_theme_menu_key(self, key: int | str) -> bool:
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("menu.theme", key)
        if act == "cancel":
            self.select_theme(self._theme_before, announce=False)
            self.theme_menu = False
        elif act in ("down", "advance"):
            self._preview_theme_at(self.theme_menu_index + 1)
        elif act == "up":
            self._preview_theme_at(self.theme_menu_index - 1)
        elif act == "first":
            self._preview_theme_at(0)
        elif act == "last":
            self._preview_theme_at(len(themes.THEMES) - 1)
        elif act == "select":
            self.select_theme(list(themes.THEMES)[self.theme_menu_index])
            self.theme_menu = False
        return True

    def cycle_source(self, step: int = 1) -> None:
        order = sources.source_cycle(self.args)
        if len(order) < 2:
            self.notify("only one harness available", "error")
            return
        cur = self.source_key if self.source_key in order else order[0]
        self.select_source(order[(order.index(cur) + step) % len(order)])

    def select_source(self, key: str) -> None:
        order = sources.source_cycle(self.args)
        if key not in order:
            return
        if key == self.source_key:
            self.notice = f"already on {SOURCE_LABELS.get(key, key)}"
            return
        cache_key = (key, self._store_state_key(self.store))
        if cache_key not in self._store_cache:
            try:
                self._store_cache[cache_key] = sources.make_store(
                    self._args_with_demo(cache_key[1]), key
                )[0]
            except SystemExit as exc:
                self.notify(str(exc), "error")
                return
        self.source_key = key
        self.store = self._store_cache[cache_key]
        self._reload_for_source()
        if self._notes_ok:
            self.notice = f"source: {SOURCE_LABELS.get(key, key)}"

    def toggle_demo(self) -> None:
        state = None if getattr(self.store, "demo", False) else DEMO_ALL
        self._apply_demo_state(state)

    def _apply_demo_state(self, state) -> None:
        if not self.source_key:
            self.notify("demo toggle unavailable", "error")
            return
        snapshot = self.ui_snapshot()
        cache_key = (self.source_key, state)
        if cache_key not in self._store_cache:
            try:
                self._store_cache[cache_key] = sources.make_store(
                    self._args_with_demo(state), self.source_key
                )[0]
            except SystemExit as exc:
                self.notify(str(exc), "error")
                return
        self.store = self._store_cache[cache_key]
        self._reload_for_source(snapshot)
        if state is not None and self.query:
            # User-entered filters can contain private titles, paths, or notes.
            self.query = ""
            self._filter_edited()
        if self._notes_ok:
            self.notice = self._demo_notice(state)

    @staticmethod
    def _demo_notice(state) -> str:
        if not state:
            return "real data"
        if state == DEMO_ALL:
            return "demo mode"
        return "demo: " + ", ".join(sorted(state))

    _DEMO_CAT_LABELS = {
        "titles": "Titles  — session / prompt / model / machine names",
        "paths": "Paths   — project directories",
        "turns": "Turns   — the expandable full prompt text",
        "spend": "Spend   — dollars and token magnitudes",
    }

    def demo_action(self) -> None:
        # A lit toggle turns off in one press; remember its categories for the next enable.
        if getattr(self.store, "demo", False):
            self.demo_last_sel = self._store_state_key(self.store) or DEMO_ALL
            self._apply_demo_state(None)
            return
        self.open_demo_menu()

    def open_demo_menu(self) -> None:
        self.demo_menu_sel = set(
            self._store_state_key(self.store) or self.demo_last_sel or DEMO_ALL
        )
        self.demo_menu_index = 0
        self.demo_menu = True

    def demo_menu_entries(self) -> list[tuple[str, str, bool]]:
        return [
            (cat, self._DEMO_CAT_LABELS[cat], cat in self.demo_menu_sel) for cat in DEMO_CATEGORIES
        ]

    def handle_demo_menu_key(self, key: int | str) -> bool:
        cats = DEMO_CATEGORIES
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("menu.demo", key)
        if act == "cancel":
            self.demo_menu = False
        elif act == "down":
            self.demo_menu_index = (self.demo_menu_index + 1) % len(cats)
        elif act == "up":
            self.demo_menu_index = (self.demo_menu_index - 1) % len(cats)
        elif act == "first":
            self.demo_menu_index = 0
        elif act == "last":
            self.demo_menu_index = len(cats) - 1
        elif act == "toggle":
            self.demo_menu_sel.symmetric_difference_update({cats[self.demo_menu_index]})
        elif act == "check_all":
            self.demo_menu_sel = set() if len(self.demo_menu_sel) == len(cats) else set(cats)
        elif act == "select":
            self.demo_menu = False
            self._apply_demo_state(frozenset(self.demo_menu_sel) or None)
        return True

    def ui_snapshot(self) -> dict:
        tabs = self.current_tabs()
        return {
            "view": self.view,
            "browse_mode": self.browse_mode,
            "focus": self.focus,
            "tab_name": tabs[self.tab % len(tabs)] if tabs else None,
            "tab": self.tab,
            "scroll": self.scroll,
            "query": self.query,
            "zoom_project": self.zoom_project,
            "anchor": self.selection_anchor(),
        }

    def _reload_for_source(self, restore: dict | None = None) -> None:
        self._clear_trace_expansion()
        self.loaded = self.store.workflows()
        self._snapshot_real_costs()
        self._resolve_project_roots()
        self.refresh_notes()
        self._models_loaded = False
        self._tool_by_session.clear()
        self._turns_by_session.clear()
        self._turn_runs_cache = None
        self.renderer._turn_layout_cache = None
        self._trace_by_session.clear()
        self._clear_trace_expansion()
        self.turn_drill = None
        self.trace_drill = None
        self._turn_cursor = 0
        self._trace_cursor = 0
        self._context_by_session.clear()
        self._nodes_by_session.clear()
        self._load_model_cache()
        self._invalidate_workflow_cache()
        # Keep overlays open, but reset cursors and drills that index the replaced dataset.
        self.trend_drill = None
        self.trend_drill_index = 0
        self.trend_row_index = 0
        self.trend_cursor = None
        self._trend_return = None
        self.cal_cursor = None
        self.trend_month_index = 0
        self.trend_week_index = 0
        self.trend_year_index = 0
        self.prices_model = None
        self.prices_index = 0
        self.prices_scroll = 0
        self.zoom_source = None
        self.source_index = 0
        self.zoom_model = None
        self.model_pick_index = 0
        self.zoom_machine = None
        self.machine_pick_index = 0
        self._disarm_mode_memory_drills(keep_project=restore is not None)
        self._revalidate_machine_filter()
        self._revalidate_harness_filter()
        if restore:
            self.browse_mode = restore["browse_mode"]
            self.focus = restore["focus"]
            self.view = restore["view"]
            zoom_project = restore["zoom_project"]
            self.zoom_project = (
                zoom_project
                # A Machines project drill is box-local; global existence cannot validate it.
                if zoom_project
                and self.browse_mode != "machines"
                and any(self.project_root(w.directory) == zoom_project for w in self.loaded)
                else None
            )
            self.query = restore["query"]
            self.restore_selection(restore["anchor"])
            tabs = self.current_tabs()
            tab_name = restore["tab_name"]
            self.tab = (
                tabs.index(tab_name)
                if tab_name in tabs
                else min(int(restore["tab"]), max(0, len(tabs) - 1))
            )
            if self.view == "session":
                # Clamping can return a neighbor after removal; validate session identity.
                current = self.current_session()
                saved_session_id = restore["anchor"].session
                if current is None or (saved_session_id and current.id != saved_session_id):
                    self.view = "zoom"
            self.scroll = max(0, int(restore["scroll"]))
            return
        self.zoom_project = None
        self.query = ""
        self.view = "browse"
        self.focus = "days"
        self.tab = self.scroll = 0
        self.workflow_index = self.month_index = self.day_index = self.project_index = 0
        self._anchor_default_selection()
        if self._notes_ok:
            self.notice = f"source: {self.store.source_name}"

    def _sessions_dataset(self, sessions: list[Workflow]) -> tuple[str, list[str], list[list]]:
        header = [
            "id",
            "created_at",
            "title",
            "directory",
            "total_cost",
            "root_cost",
            "subagent_cost",
            "subagents",
            "models",
            "total_tokens",
            "unpriced_tokens",
            "note",
        ]
        rows = [
            [
                w.id,
                w.created_at,
                w.title,
                w.directory,
                w.total_cost,
                w.root_cost,
                round(w.total_cost - w.root_cost, 6),
                w.subagents,
                w.model_count,
                w.total_tokens,
                w.unpriced_tokens,
                self.note_for(w.id),
            ]
            for w in sessions
        ]
        return "sessions", header, rows

    def _model_sessions_dataset(
        self, sessions: list[Workflow], model: str
    ) -> tuple[str, list[str], list[list]]:
        header = [
            "id",
            "created_at",
            "title",
            "directory",
            "model",
            "model_list_cost",
            "model_cost_estimated",
            "model_tokens",
            "model_messages",
            "session_cost",
            "session_tokens",
        ]
        rows = []
        for workflow in sessions:
            usage = self.model_session_usage(workflow.id, model)
            rows.append(
                [
                    workflow.id,
                    workflow.created_at,
                    workflow.title,
                    workflow.directory,
                    model,
                    usage["list_cost"],
                    usage["estimated"],
                    usage["tokens"],
                    usage["runs"],
                    workflow.total_cost,
                    workflow.total_tokens,
                ]
            )
        return "model-sessions", header, rows

    @staticmethod
    def _projects_dataset(projects: list[ProjectSummary]) -> tuple[str, list[str], list[list]]:
        header = ["directory", "cost", "tokens", "sessions", "subagents", "unpriced_tokens"]
        rows = [
            [p.directory, p.cost, p.tokens, p.workflows, p.subagents, p.unpriced_tokens]
            for p in projects
        ]
        return "projects", header, rows

    @staticmethod
    def _machines_dataset(machines: list[MachineSummary]) -> tuple[str, list[str], list[list]]:
        # Export the fleet flag because free-text names cannot identify the synthetic total.
        header = [
            "machine",
            "live",
            "cost",
            "tokens",
            "sessions",
            "subagents",
            "exported_at",
            "fleet",
        ]
        rows = [
            [m.name, m.live, m.cost, m.tokens, m.workflows, m.subagents, m.exported_at, m.fleet]
            for m in machines
        ]
        return "machines", header, rows

    def _active_tab(self) -> str:
        tabs = self.current_tabs()
        return tabs[self.tab % len(tabs)] if tabs else ""

    def _export_dataset(self) -> tuple[str, list[str], list[list]]:
        # Export the active panel at full precision under the active price mode.
        if self.show_prices:
            return self._prices_dataset()
        if self.view == "session":
            return self._session_tab_dataset()
        if self.view == "zoom":
            return self._zoom_tab_dataset()
        if self.browse_mode == "machines":
            return self._machines_dataset(self.machines)
        if self.browse_mode == "projects":
            return self._projects_dataset(self.projects)
        if self.focus == "years":
            return self._periods_dataset("years", "year", self.years)
        if self.focus == "months":
            return self._periods_dataset("months", "month", self.months)
        return self._periods_dataset("days", "day", self.panel_days)

    @staticmethod
    def _periods_dataset(scope: str, label: str, items: list) -> tuple[str, list[str], list[list]]:
        header = [label, "cost", "tokens", "sessions", "subagents", "unpriced_tokens"]
        rows = [
            [getattr(it, label), it.cost, it.tokens, it.workflows, it.subagents, it.unpriced_tokens]
            for it in items
        ]
        return scope, header, rows

    _PRICE_COLUMN_INDEX = {"input": 0, "output": 1, "cache_read": 2, "cache_write": 3}

    def _priced_model_roots(self) -> dict[str, list[dict]]:
        # Prices ignore time ranges but honor global machine/harness identity filters.
        if self.machine_filter is None and self.harness_filter is None:
            return self._model_by_root
        visible = {
            w.id
            for w in self.loaded
            if (self.machine_filter is None or self.machine_of(w) == self.machine_filter)
            and (self.harness_filter is None or (w.source or "unknown") == self.harness_filter)
        }
        return {rid: rows for rid, rows in self._model_by_root.items() if rid in visible}

    @staticmethod
    def _token_mix(
        roots: dict[str, list[dict]],
    ) -> tuple[tuple[float, float, float, float], int] | None:
        # The caller's root map deliberately distinguishes scoped Prices from app-wide what-if.
        sums = [0.0, 0.0, 0.0, 0.0]
        for rows in roots.values():
            for m in rows:
                name = m.get("model_name")
                if not name or is_local_provider(name):
                    continue
                inp, out, reasoning, cr, cw = model_row_split(m)
                sums[0] += inp
                sums[1] += out + reasoning
                sums[2] += cr
                sums[3] += cw
        total = sums[0] + sums[1] + sums[2] + sums[3]
        if total <= 0:
            return None
        return (sums[0] / total, sums[1] / total, sums[2] / total, sums[3] / total), int(total)

    def price_token_mix(self) -> tuple[tuple[float, float, float, float], int] | None:
        return self._token_mix(self._priced_model_roots())

    @staticmethod
    def _best_alias_price(aliases: dict[str, float]) -> tuple[float, float, float, float]:
        # Prefer the most complete alias rate card; break ties by observed usage.
        best, best_key = (0.0, 0.0, 0.0, 0.0), (-1, -1.0)
        for alias, tok in aliases.items():
            for candidate in {alias, display_model(alias)}:
                p = model_price(candidate)
                key = (sum(1 for v in p if v > 0), tok)
                if key > best_key:
                    best, best_key = tuple(p), key
        return best

    def priced_model_entries(self) -> list[PriceEntry]:
        # Flat/family views canonicalize aliases; provider view retains gateway pricing.
        # Local models have no API rate and remain outside this list-price reference.
        mix = self.price_token_mix()
        shares = mix[0] if mix else (1.0, 0.0, 0.0, 0.0)
        if self.prices_view == "all":
            entries = self._catalog_price_entries(shares)
            return self._order_price_entries(self._filter_price_entries(entries))
        by_route = self.prices_view == "provider"
        raw: dict[tuple[str, str], dict] = {}
        grand = 0.0
        for rows in self._priced_model_roots().values():
            for m in rows:
                name = str(m["model_name"])
                if is_local_provider(name):
                    continue
                bare = name.rsplit("/", 1)[-1]
                route = name.rsplit("/", 1)[0] if "/" in name else ""
                tok = float(m.get("tokens_total") or 0)
                grand += tok
                d = raw.setdefault(
                    (route if by_route else "", canonical_model(bare)),
                    {"spend": 0.0, "tokens": 0.0, "routes": set(), "aliases": defaultdict(float)},
                )
                d["spend"] += float(m.get("cost", 0) or 0)
                d["tokens"] += tok
                if route:
                    d["routes"].add(route)
                d["aliases"][bare] += tok
        entries = []
        for (route, canon), d in raw.items():
            price = self._best_alias_price(d["aliases"])
            eff, approx = effective_price(price, shares)
            routes_t = tuple(sorted(d["routes"]))
            entries.append(
                PriceEntry(
                    bare=display_model(max(d["aliases"], key=d["aliases"].get)),
                    canon=canon,
                    family=model_family(canon),
                    routes=routes_t,
                    spend=d["spend"],
                    group=(
                        route
                        if by_route
                        else (model_family(canon) if self.prices_view == "family" else "")
                    ),
                    share=(d["tokens"] / grand if grand > 0 else 0.0),
                    price=price,
                    eff=eff,
                    approx=approx,
                    pinned=self._is_pinned(canon, routes_t),
                )
            )
        return self._order_price_entries(self._filter_price_entries(entries))

    def _filter_price_entries(self, entries: list[PriceEntry]) -> list[PriceEntry]:
        # Keep the active column order; filtering must agree with the what-if picker.
        if not self.query:
            return entries
        return [
            e
            for e in entries
            if model_matches(self.query, e.bare, e.routes, family_label(e.family))
        ]

    def _catalog_price_entries(self, shares: tuple) -> list[PriceEntry]:
        # Keep one canonical row per provider: gateway resale differences are meaningful.
        # Exclude free/local rows so they cannot dominate ranking and heat scales.
        usage: dict[str, list[float]] = {}
        grand = 0.0
        for rows in self._priced_model_roots().values():
            for m in rows:
                name = str(m["model_name"])
                if is_local_provider(name):
                    continue
                tok = float(m.get("tokens_total") or 0)
                grand += tok
                u = usage.setdefault(canonical_model(name), [0.0, 0.0])
                u[0] += float(m.get("cost", 0) or 0)
                u[1] += tok
        best: dict[tuple[str, str], tuple] = {}
        for pid, mid, price, status in catalog_models():
            if pid.lower() in LOCAL_PROVIDERS or (price[0] <= 0 and price[1] <= 0):
                continue
            bare = display_model(mid.rsplit("/", 1)[-1])
            canon = canonical_model(bare)
            key = (sum(1 for v in price if v > 0), -len(mid))
            cur = best.get((pid, canon))
            if cur is None or key > cur[0]:
                best[(pid, canon)] = (key, bare, canon, pid, price, status)
        entries = []
        for _key, bare, canon, pid, price, status in best.values():
            u = usage.get(canon)
            eff, approx = effective_price(price, shares)
            entries.append(
                PriceEntry(
                    bare=bare,
                    canon=canon,
                    family=model_family(canon),
                    routes=(pid,),
                    spend=u[0] if u else 0.0,
                    group="",
                    share=(u[1] / grand if u and grand > 0 else 0.0),
                    price=price,
                    eff=eff,
                    approx=approx,
                    status=status,
                    pinned=self._is_pinned(canon, (pid,)),
                )
            )
        return entries

    def _order_price_entries(self, entries: list[PriceEntry]) -> list[PriceEntry]:
        # Pins always float; grouped views rank groups by spend with Other last.
        pinned = self._sort_price_entries([e for e in entries if e.pinned])
        rest = [e for e in entries if not e.pinned]
        if self.prices_view in ("flat", "all"):
            return pinned + self._sort_price_entries(rest)
        group_spend: dict[str, float] = defaultdict(float)
        for e in rest:
            group_spend[e.group] += e.spend
        groups = sorted(
            {e.group for e in rest},
            key=lambda g: (g == "", -group_spend[g]),
        )
        out: list[PriceEntry] = pinned
        for g in groups:
            out.extend(self._sort_price_entries([e for e in rest if e.group == g]))
        return out

    def _sort_price_entries(self, entries: list[PriceEntry]) -> list[PriceEntry]:
        # Spend is the stable tiebreak for equal column values.
        key = self.prices_sort if self.prices_sort in self.prices_sort_options else "eff"
        by_spend = sorted(entries, key=lambda e: e.spend, reverse=True)
        desc = self.sort_descending(key, self.prices_sort_reverse)
        if key == "model":
            return sorted(by_spend, key=lambda e: e.bare.lower(), reverse=desc)
        if key == "eff":
            return sorted(by_spend, key=lambda e: e.eff, reverse=desc)
        if key == "use":
            return sorted(by_spend, key=lambda e: e.share, reverse=desc)
        col = self._PRICE_COLUMN_INDEX[key]
        return sorted(by_spend, key=lambda e: e.price[col], reverse=desc)

    def priced_model_names(self) -> list[str]:
        return [e.bare for e in self.priced_model_entries()]

    def price_model_sessions(self, bare_model: str) -> list[tuple[Workflow, float, int]]:
        # Canonical matching aggregates aliases and access routes into one session row.
        target = canonical_model(bare_model)
        by_id = {w.id: w for w in self.loaded}
        per_root: dict[str, list] = {}
        for root_id, models in self._priced_model_roots().items():
            w = by_id.get(root_id)
            if w is None:
                continue
            for m in models:
                if canonical_model(str(m.get("model_name"))) != target:
                    continue
                acc = per_root.setdefault(root_id, [w, 0.0, 0])
                acc[1] += float(m.get("cost", 0) or 0)
                acc[2] += int(m.get("tokens_total", 0) or 0)
        out = [(w, cost, tok) for w, cost, tok in per_root.values()]
        out.sort(key=lambda r: (r[1], r[2]), reverse=True)
        return out

    def _prices_dataset(self) -> tuple[str, list[str], list[list]]:
        header = [
            "model",
            "family",
            "routes",
            "pinned",
            "share",
            "eff_usd_per_mtok",
            "eff_approx",
            "input",
            "output",
            "cache_read",
            "cache_write",
        ]
        rows = [
            [
                e.bare,
                family_label(e.family),
                " ".join(e.routes),
                e.pinned,
                round(e.share, 4),
                round(e.eff, 4),
                e.approx,
                *e.price,
            ]
            for e in self.priced_model_entries()
        ]
        return "prices", header, rows

    def _zoom_tab_dataset(self) -> tuple[str, list[str], list[list]]:
        tab = self._active_tab()
        if self.zoom_model:
            return self._model_sessions_dataset(self.current_sessions(), self.zoom_model)
        if tab == "Projects":
            return self._projects_dataset(self.zoom_projects())
        if tab == "Models":
            return self._models_dataset(self.aggregate_models(self._active_scope_workflows()))
        if tab == "Harnesses":
            return self._sources_dataset(self._active_scope_workflows())
        if tab == "Machines":
            return self._machine_agg_dataset(self._active_scope_workflows())
        return self._sessions_dataset(self.current_sessions())

    def _active_scope_workflows(self) -> list[Workflow]:
        if self.browse_mode == "machines":
            machine = self.selected_machine_summary
            return self.machine_scope(machine) if machine else []
        if self.browse_mode == "projects":
            project = self.selected_project_summary
            return (
                self.workflows_for_project(
                    project.directory,
                    include_ignored=self.include_ignored_for_project(project),
                )
                if project
                else []
            )
        return self.zoom_scope_workflows()

    @staticmethod
    def _sources_dataset(workflows: list[Workflow]) -> tuple[str, list[str], list[list]]:
        by_source: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"cost": 0.0, "tokens": 0, "sessions": 0}
        )
        for w in workflows:
            item = by_source[w.source or "unknown"]
            item["cost"] = float(item["cost"]) + w.total_cost
            item["tokens"] = int(item["tokens"]) + w.total_tokens
            item["sessions"] = int(item["sessions"]) + 1
        rows = sorted(
            by_source.items(),
            key=lambda kv: (float(kv[1]["cost"]), int(kv[1]["tokens"])),
            reverse=True,
        )
        header = ["source", "cost", "tokens", "sessions"]
        return "sources", header, [[s, it["cost"], it["tokens"], it["sessions"]] for s, it in rows]

    def _machine_agg_dataset(self, workflows: list[Workflow]) -> tuple[str, list[str], list[list]]:
        rows = self.machine_rows(workflows)
        header = ["machine", "cost", "tokens", "sessions"]
        return "machines", header, [[m, it["cost"], it["tokens"], it["sessions"]] for m, it in rows]

    def _session_tab_dataset(self) -> tuple[str, list[str], list[list]]:
        session = self.current_session()
        if session is None:
            return "subagents", ["date", "depth", "agent", "model", "cost", "tokens", "title"], []
        tab = self._active_tab()
        if tab == "Subagents":
            return self._subagents_dataset(session)
        if tab == "Turns":
            return self._turns_dataset(session)
        if tab == "Tools":
            return self._tools_dataset(session)
        return self._models_dataset([(r["model_name"], r) for r in self.model_mix(session.id)])

    @staticmethod
    def _models_dataset(rows: list) -> tuple[str, list[str], list[list]]:
        header = ["model", "runs", "cost", "tokens", "cache_read", "cache_write", "output"]
        out = []
        for name, it in rows:
            tokens_total = it["tokens"] if "tokens" in it else it["tokens_total"]
            out.append(
                [
                    name,
                    it["runs"],
                    it["cost"],
                    tokens_total,
                    it["cache_read"],
                    it["cache_write"],
                    it["output"],
                ]
            )
        return "models", header, out

    def _subagents_dataset(self, session: Workflow) -> tuple[str, list[str], list[list]]:
        nodes = self._priced_nodes(
            [r for r in self.session_node_rows(session.id) if r["depth"] > 0]
        )
        header = ["date", "depth", "agent", "model", "cost", "tokens", "title"]
        rows = [
            [
                r.get("created_at", ""),
                r["depth"],
                r["agent"],
                r["model_name"],
                r["cost"],
                r["tokens_total"],
                r["title"],
            ]
            for r in self.sorted_subagent_rows(nodes)
        ]
        return "subagents", header, rows

    def _turns_dataset(self, session: Workflow) -> tuple[str, list[str], list[list]]:
        api = self.show_api_prices and not self.store.demo
        header = [
            "time",
            "agent",
            "depth",
            "model",
            "cost",
            "tokens",
            "input",
            "output",
            "cache_read",
            "cache_write",
            "prompt",
        ]
        rows = []
        for r in self.session_turn_rows(session.id):
            cost = r["cost"]
            if api and not cost:  # reprice a wholly-$0 turn at list price, like the tab
                cost = api_equivalent_cost(
                    r["model_name"],
                    r["input"],
                    r["output"],
                    r["reasoning"],
                    r["cache_read"],
                    r["cache_write"],
                    r.get("cache_write_1h", 0),
                )
            rows.append(
                [
                    r["time"],
                    r["agent"] if r["depth"] else "-",
                    r["depth"],
                    r["model_name"],
                    cost,
                    r["tokens_total"],
                    r["input"],
                    r["output"],
                    r["cache_read"],
                    r["cache_write"],
                    (r.get("prompt_title") or "").strip(),
                ]
            )
        return "turns", header, rows

    def _tools_dataset(self, session: Workflow) -> tuple[str, list[str], list[list]]:
        api = self.show_api_prices and not self.store.demo
        header = [
            "tool",
            "model",
            "calls",
            "cost",
            "tokens",
            "input",
            "output",
            "cache_read",
            "cache_write",
        ]
        rows = []
        for r in self.session_tool_rows(session.id):
            cost = r["cost"]
            if api and not cost:
                cost = api_equivalent_cost(
                    r["model_name"],
                    r["input"],
                    r["output"],
                    r["reasoning"],
                    r["cache_read"],
                    r["cache_write"],
                    r.get("cache_write_1h", 0),
                )
            rows.append(
                [
                    r["tool"],
                    r["model_name"],
                    r["calls"],
                    cost,
                    r["tokens_total"],
                    r["input"],
                    r["output"],
                    r["cache_read"],
                    r["cache_write"],
                ]
            )
        return "tools", header, rows

    @staticmethod
    def _csv_safe(value):
        # Neutralize spreadsheet formula injection: a cell starting with =, +, -,
        # @, tab, or CR is executed as a formula by Excel/LibreOffice/Sheets, and
        # titles/dirs/models are attacker-influenced text. Only strings need the
        # guard, and a string that is itself a plain number (a negative cost)
        # passes through -- only would-be formulas get the leading apostrophe.
        if not isinstance(value, str) or not value or value[0] not in "=+-@\t\r":
            return value
        try:
            float(value)
            return value
        except ValueError:
            return "'" + value

    def export_current(self) -> None:
        if self.store.demo:
            self.notify("export disabled in demo mode", "error")
            return
        scope, header, rows = self._export_dataset()
        if not rows:
            self.notify("nothing to export here", "error")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.abspath(f"opentab-{scope}-{stamp}.csv")
        try:
            with open(path, "w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(header)
                writer.writerows([[self._csv_safe(cell) for cell in row] for row in rows])
        except OSError as exc:
            self.notify(f"export failed: {exc}", "error")
            return
        # Show the full path home-abbreviated but NOT truncated -- the toast wraps long
        # text now, so the directory and filename both stay readable (short_path with a
        # generous width only does the ~ swap here, no clipping).
        self.notify(f"exported {len(rows)} rows → {short_path(path, 999)}", "success")

    def _current_directory(self) -> str | None:
        if self.browse_mode == "projects":
            project = self.selected_project_summary
            return project.directory if project else None
        session = self.current_session()
        return session.directory if session else None

    def open_current(self) -> None:
        if self.store.demo:
            self.notify("open disabled in demo mode", "error")
            return
        directory = self._current_directory()
        if not directory or directory in ("(unknown)", ""):
            self.notify("no directory to open", "error")
            return
        if open_path(directory):
            self.notify(f"opened {short_path(directory, 40)}", "success")
        else:
            opener = "explorer" if sys.platform == "win32" else "open/xdg-open"
            self.notify(f"no opener found ({opener})", "error")

    def resume_parts(self, workflow: Workflow) -> tuple[str, str] | None:
        # (project directory, bare resume command) for the selected session —
        # launch backends receive the directory separately from the bare resume command.
        cli = RESUME_COMMANDS.get(workflow.source)
        directory = workflow.directory
        if not cli or not directory or directory == "(unknown)":
            return None
        return directory, f"{cli} {shlex.quote(workflow.id)}"

    def machine_ssh_target(self, workflow: Workflow) -> str | None:
        # The ssh target for the box a PULLED session ran on -- its machine's
        # remotes.json key, resolved through the map main() injects. None for your own
        # live box (there is nothing to ssh into), for a machine reached by `url` rather
        # than ssh, and whenever no fleet is in view -- each case meaning "this session
        # is launchable right here", which is the pre-fleet behaviour.
        name = getattr(workflow, "machine", "") or ""
        if not name or self._ssh_targets is None:
            return None
        meta = self.machine_meta().get(name) or {}
        key = meta.get("key")
        if meta.get("live") or not key:
            return None
        try:
            targets = self._ssh_targets() or {}
        except Exception:  # noqa: BLE001 -- a broken remotes.json must not eat the `L` key
            return None
        return targets.get(str(key)) or None

    def launch_parts(self, workflow: Workflow) -> tuple[str, str] | None:
        # The command stays in its copyable shell form for every launch backend.
        parts = self.resume_parts(workflow)
        if not parts:
            return None
        directory, command = parts
        target = self.machine_ssh_target(workflow)
        if not target:
            return directory, command
        return os.path.expanduser("~"), util.ssh_command(target, directory, command)

    def resume_command(self, workflow: Workflow) -> str | None:
        # The ready-to-paste shell form: cd to the project, then resume -- or the whole
        # thing over ssh when the session ran on another machine, so what you yank is a
        # line that works from here rather than a path that only exists over there.
        parts = self.resume_parts(workflow)
        if not parts:
            return None
        directory, command = parts
        target = self.machine_ssh_target(workflow)
        if target:
            return util.ssh_command(target, directory, command)
        return f"cd {shlex.quote(directory)} && {command}"

    def launch_available(self) -> bool:
        # launch_backend picks the hook or innermost supported multiplexer when the
        # picker opens; retain that backend snapshot for its rows and dispatch.
        return self.launch_menu_backend is not None

    def unreachable_machine(self) -> str | None:
        # The machine name when the session in the `L` picker was pulled from a box we
        # cannot ssh into (a `url` entry, or one no longer in remotes.json). Spawning
        # locally would be wrong, not merely unhelpful -- it would run `claude --resume`
        # against another box's session id, in a directory that is not this one's -- so
        # those rows come off the menu and only the yank is left.
        session = self.launch_menu
        if session is None or self._ssh_targets is None:
            return None
        name = getattr(session, "machine", "") or ""
        meta = self.machine_meta().get(name) or {}
        if not name or meta.get("live"):
            return None
        return None if self.machine_ssh_target(session) else name

    def launch_targets(self) -> tuple[tuple[str, str, str], ...]:
        # Herdr can create focused tabs and splits, but its external popups are not controllable.
        if self.launch_available() and not self.unreachable_machine():
            targets = self.LAUNCH_TARGETS
            if self.launch_menu_backend == "herdr":
                targets = tuple(target for target in targets if target[1] != "popup")
                if util.herdr_pane_id() is None:
                    targets = tuple(
                        target for target in targets if target[1] not in ("hsplit", "vsplit")
                    )
                targets = tuple(
                    (key, kind, "new tab" if kind == "window" else label)
                    for key, kind, label in targets
                )
            return targets
        return tuple(t for t in self.LAUNCH_TARGETS if t[1] == "copy")

    def launch_current(self) -> None:
        # `L`: open the launch menu (window/split/popup/copy — handled by
        # handle_launch_key on the next keystroke). Without a supported launch backend
        # (tmux, Herdr, or a hook), the menu narrows to copying (launch_targets).
        if self.store.demo:
            self.notify("launch disabled in demo mode", "error")
            return
        session = self.launch_session()
        if not session:
            self.notify("launch works on sessions only", "error")
            return
        if self.resume_parts(session) is None:
            self.notify("no launch command for this session", "error")
            return
        self.launch_menu = session
        self.launch_menu_index = 0
        self.launch_menu_backend = util.launch_backend()

    def launch_session(self) -> Workflow | None:
        if self.view == "session" or (self.view == "zoom" and self.on_sessions_tab):
            return self.current_session()
        return None

    def copy_resume_command(self, session: Workflow) -> None:
        command = self.resume_command(session)
        if command and util.copy_to_clipboard(command):
            self.notify(f"copied: {shorten(command, 60)}", "success")
        else:
            self.notify(f"clipboard tool not found ({util.clipboard_tools_label()})", "error")

    def handle_launch_key(self, key: int | str) -> bool:
        # The `L` launch picker: down/up move, select runs the highlighted target, the
        # first-letter shortcuts jump straight to one, cancel closes. Mirrors
        # handle_source_menu_key.
        if key == 3:  # Ctrl-C still quits
            return False
        targets = self.launch_targets()
        n = len(targets)
        act = self.keymap.action("menu.launch", key)
        if act == "down":
            self.launch_menu_index = (self.launch_menu_index + 1) % n
            return True
        if act == "up":
            self.launch_menu_index = (self.launch_menu_index - 1) % n
            return True
        if act == "first":
            self.launch_menu_index = 0
            return True
        if act == "last":
            self.launch_menu_index = n - 1
            return True
        if act == "cancel":
            self.launch_menu = None
            self.launch_menu_backend = None
            self.notice = "launch cancelled"
            return True
        # The per-target letters follow the target names (w/s/v/p/y today), so they
        # are dynamic, not remappable -- and a remapped action above wins over them.
        shortcuts = {ord(t[0]): i for i, t in enumerate(targets)}
        if key in shortcuts:
            index = shortcuts[key]
        elif act == "select":
            index = self.launch_menu_index % n
        else:
            return True  # ignore unknown keys, keep the modal open
        session, backend = self.launch_menu, self.launch_menu_backend
        self.launch_menu = None
        self.launch_menu_backend = None
        self._do_launch(session, targets[index][1], backend)
        return True

    def _do_launch(self, session: Workflow, kind: str, backend: str | None) -> None:
        if kind == "copy":
            self.copy_resume_command(session)
            return
        parts = self.launch_parts(session)
        if not parts:
            self.notice = "launch cancelled"
            return
        directory, command = parts
        error = util.launch_command(kind, directory, command, backend)
        if error:
            self.notify(f"launch failed: {error}", "error")
        else:
            where = self.machine_ssh_target(session)
            self.notice = f"{kind}: {shorten(command, 50)}" if not where else f"{kind} on {where}"

    def handle_source_menu_key(self, key: int | str) -> bool:
        # The `H` data-source picker: down/up move, select switches, cancel closes.
        # advance (`H` again) walks the highlight so repeated taps still move.
        order = sources.source_cycle(self.args)
        if not order:
            self.source_menu = False
            return True
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("menu.source", key)
        if act in ("down", "advance"):
            self.source_menu_index = (self.source_menu_index + 1) % len(order)
        elif act == "up":
            self.source_menu_index = (self.source_menu_index - 1) % len(order)
        elif act == "first":
            self.source_menu_index = 0
        elif act == "last":
            self.source_menu_index = len(order) - 1
        elif act == "select":
            self.source_menu = False
            self.select_source(order[self.source_menu_index % len(order)])
        elif act == "cancel":
            self.source_menu = False  # cancel, source unchanged
        # any other key: ignore and keep the menu open
        return True

    def sorted_workflows(self, rows: list[Workflow]) -> list[Workflow]:
        sort_by = self.session_sort_key()
        desc = self.sort_descending(sort_by, self.session_sort_reverse())
        model = self.zoom_model

        def model_values(item: Workflow) -> tuple[float, int]:
            if not model:
                return item.total_cost, item.total_tokens
            usage = self.model_session_usage(item.id, model)
            return float(usage["list_cost"]), int(usage["tokens"])

        if sort_by == "cost":
            return sorted(rows, key=model_values, reverse=desc)
        if sort_by == "tokens":
            return sorted(rows, key=lambda item: tuple(reversed(model_values(item))), reverse=desc)
        if sort_by == "subagents":
            return sorted(rows, key=lambda item: (item.subagents, item.total_tokens), reverse=desc)
        if sort_by == "duration":
            # Hardest-worked first by default. A session whose backend can't tell work
            # from waiting (worked None) sorts as 0s, keeping it with the shortest.
            return sorted(
                rows,
                key=lambda item: (item.worked_seconds or 0.0, item.total_cost),
                reverse=desc,
            )
        if sort_by == "title":
            return sorted(rows, key=lambda item: item.title.lower(), reverse=desc)
        if sort_by == "project":
            # Groups a mixed session list by project -- the sessions-tab way to eyeball
            # one project's sessions together. Costliest session stays first within
            # each group whichever way the project names run (the direction flip
            # reorders the groups, not their insides), hence the two stable passes.
            by_cost = sorted(rows, key=model_values, reverse=True)
            return sorted(
                by_cost, key=lambda item: self.project_root(item.directory).lower(), reverse=desc
            )
        if sort_by == "last_activity":
            return sorted(
                rows,
                key=lambda item: (item.ended_at or item.created_at, item.created_at),
                reverse=desc,
            )
        return sorted(rows, key=lambda item: item.created_at, reverse=desc)

    def zoom_scope_workflows(self, include_ignored: bool = False) -> list[Workflow]:
        # The sessions in the currently zoomed year, month, or day (time mode).
        source = self.ranged_workflows if include_ignored else self.all_workflows
        if self.focus == "years":
            item = self.selected_year_summary
            return self.workflows_for_year(item.year, source) if item else []
        if self.focus == "months":
            item = self.selected_month_summary
            return self.workflows_for_month(item.month, source) if item else []
        item = self.selected_day_summary
        return self.workflows_for_day(item.day, source) if item else []

    def zoom_projects(self) -> list[ProjectSummary]:
        # Projects active within the zoomed scope — the navigable Projects tab. In Machines
        # mode the scope is the selected box (not a month/day); everywhere else it's the
        # zoomed year/month/day.
        if self.browse_mode == "machines":
            item = self.selected_machine_summary
            base = self.machine_scope(item) if item else []
        else:
            base = self.zoom_scope_workflows(include_ignored=self.show_ignored_projects)
        return self.projects_for_workflows(base, include_ignored=self.show_ignored_projects)

    def zoom_selected_project(self) -> ProjectSummary | None:
        rows = self.zoom_projects()
        if not rows:
            return None
        self.project_index = max(0, min(self.project_index, len(rows) - 1))
        return rows[self.project_index]

    def mode_scope_workflows(self) -> list[Workflow]:
        """The sessions the SIDEBAR's current selection covers, in whatever mode we're in.

        The one answer to "what is the scope" -- `current_sessions` and
        `_zoom_picker_scope` both re-derived it, in the same three branches, with the
        same three `if item else []` guards. That duplication is what a mode added later
        has to be remembered at twice, and the two are exactly the pair that must never
        disagree: the picker ranks a scope, Enter opens it, and a row reading "1 session
        · $3" that opens two sessions and $5 is the whole reason `_zoom_picker_scope`
        exists.

        The drills stay with their callers, because they are NOT shared: a box's are
        mutually exclusive and applied in their own order, everything else composes.
        And `zoom_projects` deliberately still does its own thing -- it widens by
        `show_ignored_projects` (the `I` toggle for the projects LIST) rather than
        `_showing_ignored_workflows()`, which is a different question about sessions.
        """
        if self.browse_mode == "machines":
            item = self.selected_machine_summary
            return self.machine_scope(item) if item else []
        if self.browse_mode == "projects":
            item = self.selected_project_summary
            if item is None:
                return []
            return self.workflows_for_project(
                item.directory, include_ignored=self.include_ignored_for_project(item)
            )
        # Time: the focused panel (year / month / day) is the scope. Passing the widened
        # source through zoom_scope_workflows is the same query current_sessions used to
        # spell inline -- workflows_for_* default to all_workflows when handed None.
        return self.zoom_scope_workflows(include_ignored=self._showing_ignored_workflows())

    def _zoom_picker_scope(self, exclude: str) -> list[Workflow]:
        # The sessions a zoom's Harnesses/Machines picker ranks -- exactly the ones Enter
        # then opens (current_sessions), so it takes the same widenings: `i` (ignored rows
        # in view), a Projects-tab drill (zoom_project), and the committed `f` query.
        # Counting a scope you can't open is how a row reads "1 session · $3" and produces
        # two sessions and $5. Crucially it ALSO applies the OTHER dimension's armed drill
        # (h/l can leave a machine/source narrowed while you move to the sibling picker) --
        # everything except the dimension being picked (`exclude`), which the pick SETS. So
        # the Harnesses picker shows sources within an armed box, and vice-versa.
        # The sidebar's scope (a zoomed BOX scopes the Harnesses picker by the sidebar
        # selection, not by zoom_machine, which stays None there).
        rows = self.mode_scope_workflows()
        if self.browse_mode not in ("machines", "projects") and self.zoom_project:
            # Time mode only: in Machines the drills are mutually exclusive so there is
            # nothing to compose, and in Projects the sidebar IS the project -- there is
            # no second one to narrow to.
            rows = [w for w in rows if self.project_root(w.directory) == self.zoom_project]
        if self.browse_mode != "machines":
            # The fleet's per-scope pickers COMPOSE (h/l can leave a box/source armed while
            # you pick the sibling). Machines mode does not: its Harnesses/Projects/Models
            # drills are mutually exclusive (drilling one clears the others), so each picker
            # ranks the whole box and there's nothing to compose in.
            if exclude != "source" and self.zoom_source:
                rows = [w for w in rows if (w.source or "unknown") == self.zoom_source]
            if exclude != "machine" and self.zoom_machine:
                rows = [w for w in rows if self.machine_of(w) == self.zoom_machine]
        return self.filtered_sessions(rows)

    def zoom_source_rows(self) -> list[tuple[str, dict[str, float | int]]]:
        # The navigable Sources tab of a zoomed scope (merged view), grouped by harness.
        return self.source_rows(self._zoom_picker_scope("source"))

    def models_tab_workflows(self) -> list[Workflow]:
        # The sessions the Models tab covers -- the SAME scope machine_models /
        # project_models / month_models / year_models build their table from, so the
        # cursor's ordinal indexes exactly the rows on screen. (Day has no Models tab:
        # its models stay folded into the Overview.)
        #
        # Deliberately NOT _zoom_picker_scope, which the Harnesses/Machines pickers use:
        # that applies the `f` query to SESSIONS, while the Models tab has always applied
        # it to MODEL NAMES. One tab must mean one thing by one key -- pressing Enter
        # used to swap which, so a query narrowing the list stopped matching the moment
        # you focused it.
        if self.browse_mode == "machines":
            item = self.selected_machine_summary
            return self.machine_scope(item) if item else []
        if self.browse_mode == "projects":
            item = self.selected_project_summary
            return (
                self.workflows_for_project(
                    item.directory,
                    include_ignored=self.include_ignored_for_project(item),
                )
                if item
                else []
            )
        return self.zoom_scope_workflows()

    def compose_zoom_drills(self, rows: list[Workflow]) -> list[Workflow]:
        # Narrow a scope by the PARTITION drills already armed in it (harness, project,
        # machine) -- everything except the model drill, which is what this scope is being
        # ranked for. The Harnesses/Machines pickers do the same through
        # _zoom_picker_scope, and for the same reason: a picker must only ever offer rows
        # its Enter can actually open. Ranking the whole zoom instead let the Models tab
        # list a model the armed project never ran; picking it armed a drill that matched
        # nothing, which the _drilled net then immediately disarmed -- so the pick
        # silently did nothing and Esc popped the project instead.
        #
        # Called from BOTH App.models_tab_workflows and the renderer's four *_models
        # methods (Renderer.__getattr__ delegates here), so the rows the cursor indexes
        # and the rows on screen cannot drift.
        #
        # Machines mode composes NOTHING, the same exception _zoom_picker_scope makes
        # for the Harnesses/Machines pickers and for the same reason: a box's drills are
        # mutually exclusive, so picking a model CLEARS the harness/project drill this
        # would otherwise have ranked through. Composing what the pick discards breaks
        # the one rule these scopes exist to keep -- a picker must only ever offer rows
        # its Enter can open -- in the direction that is hardest to spot: the row read
        # "1 session · $3" and opened two sessions and $5, because the ranking was
        # narrower than the list it produced.
        if self.browse_mode == "machines":
            return rows
        if self.zoom_project and self.browse_mode != "projects":
            rows = [w for w in rows if self.project_root(w.directory) == self.zoom_project]
        if self.zoom_source:
            rows = [w for w in rows if (w.source or "unknown") == self.zoom_source]
        if self.zoom_machine:
            rows = [w for w in rows if self.machine_of(w) == self.zoom_machine]
        return rows

    def zoom_model_rows(self) -> list[tuple[str, dict[str, float | int]]]:
        # The Models tab's rows in display order -- cost-ranked by aggregate_models and
        # narrowed by `f` on model names, exactly as Renderer._models_tab renders them.
        # model_pick_index is a plain ordinal into this list (the _turn_groups pattern:
        # App owns the list, the renderer only maps a clicked LINE back to an ordinal).
        rows = self.aggregate_models(self.compose_zoom_drills(self.models_tab_workflows()))
        if self.query:
            rows = [r for r in rows if fuzzy_score(self.query, str(r[0])) is not None]
        return rows

    def zoom_selected_model(self) -> str | None:
        rows = self.zoom_model_rows()
        if not rows:
            return None
        self.model_pick_index = max(0, min(self.model_pick_index, len(rows) - 1))
        return rows[self.model_pick_index][0]

    def zoom_selected_source(self) -> str | None:
        rows = self.zoom_source_rows()
        if not rows:
            return None
        self.source_index = max(0, min(self.source_index, len(rows) - 1))
        return rows[self.source_index][0]

    def zoom_machine_rows(self) -> list[tuple[str, dict[str, float | int]]]:
        # The navigable Machines tab of a zoomed scope (fleet view), grouped by box --
        # the harness picker's twin, over the same scope.
        return self.machine_rows(self._zoom_picker_scope("machine"))

    def zoom_selected_machine(self) -> str | None:
        rows = self.zoom_machine_rows()
        if not rows:
            return None
        self.machine_pick_index = max(0, min(self.machine_pick_index, len(rows) - 1))
        return rows[self.machine_pick_index][0]

    def current_sessions(self) -> list[Workflow]:
        rows = self.mode_scope_workflows()
        if self.browse_mode == "machines":
            if self.zoom_source:  # a Harnesses-tab drill narrows this box to one harness
                rows = self._drilled(rows, self._match_source, self._clear_source_drill)
            if self.zoom_project:  # a Projects-tab drill narrows this box to one project
                rows = self._drilled(rows, self._match_project, self._clear_project_drill)
            if self.zoom_model:  # a Models-tab drill narrows to sessions that used it
                rows = self._drilled(rows, self._match_model, self._clear_model_drill)
            return self.filtered_sessions(rows)
        if self.zoom_project and self.browse_mode != "projects":
            rows = self._drilled(rows, self._match_project, self._clear_project_drill)
        if self.zoom_source:
            rows = self._drilled(rows, self._match_source, self._clear_source_drill)
        if self.zoom_machine:  # a per-scope Machines-tab drill (fleet view)
            rows = self._drilled(rows, self._match_machine, self._clear_machine_drill)
        if self.zoom_model:  # a Models-tab drill: sessions that USED the model
            rows = self._drilled(rows, self._match_model, self._clear_model_drill)
        return self.filtered_sessions(rows)

    def settle_drills(self) -> None:
        # Run the drill net once, up front, so a frame is internally consistent. The net
        # lives inside current_sessions (see _drilled), and the breadcrumb is painted
        # before the sessions pane asks for that list -- so the frame in which a stale
        # drill heals would otherwise show a crumb for a drill its own list has dropped.
        # Cheap: this is the same call the sessions pane makes moments later.
        if self.view != "browse" and any(
            (self.zoom_project, self.zoom_source, self.zoom_machine, self.zoom_model)
        ):
            self.current_sessions()

    def _drilled(self, rows: list[Workflow], keep, disarm) -> list[Workflow]:
        # Apply one armed in-zoom drill -- and DISARM it when this scope holds nothing it
        # matches, rather than serving an empty list.
        #
        # This is the safety net, and it lives at the point of APPLICATION rather than at
        # the mutation sites on purpose. Every drill is a value plus a cursor into a list
        # that a dozen unrelated things rebuild: the range, `i`, `M`, `H`, `B`, a reload,
        # a source or demo swap, every sidebar move by key, click or wheel. Clearing them
        # at each of those is how it gets missed -- three review rounds over the Models
        # drill each found more such paths than the one before, and any path added later
        # would be a fresh instance of the same bug. A filter that can only ever produce
        # an empty list is not filtering, it is stuck, and it is invisible except as "why
        # is this list empty" -- so it drops itself, once, here.
        #
        # `not rows` is the deliberate exception: a scope already emptied by something
        # else (bookmarks-only with nothing bookmarked, an ignored project) is not this
        # drill's doing, and blaming it would silently discard a selection that is still
        # valid the moment that other filter comes off.
        #
        # The eager clears on a deliberate re-scope stay on top of this: disarming as you
        # move reads better than a list that empties and then heals.
        kept = [w for w in rows if keep(w)]
        if kept or not rows:
            return kept
        disarm()
        return rows

    # The four drill predicates, named so current_sessions reads as a list of drills
    # rather than a wall of lambdas (and so _drilled's two call sites can't drift).
    def _match_project(self, w: Workflow) -> bool:
        return self.project_root(w.directory) == self.zoom_project

    def _match_source(self, w: Workflow) -> bool:
        return (w.source or "unknown") == self.zoom_source

    def _match_machine(self, w: Workflow) -> bool:
        return self.machine_of(w) == self.zoom_machine

    def _match_model(self, w: Workflow) -> bool:
        return self._session_used_model(w.id, self.zoom_model)

    # Each drill's armed value and the cursor indexing the list it was picked from are
    # ONE selection, always cleared together -- a surviving cursor is an ordinal into a
    # ranking that has since been rebuilt, and the first j/k then only re-clamps it.
    def _clear_project_drill(self) -> None:
        self.zoom_project = None
        # project_index wears two hats: the zoom Projects-tab picker cursor in time and
        # machines mode, but in PROJECTS mode the sidebar selection itself -- the project
        # you are looking at, which no drill owns and none may reset. Zeroing it there
        # walked the sidebar back to the first project on any range change. Guarded here
        # rather than at each caller, so a new one cannot reintroduce it.
        if self.browse_mode != "projects":
            self.project_index = 0

    def _clear_source_drill(self) -> None:
        self.zoom_source = None
        self.source_index = 0

    def _clear_machine_drill(self) -> None:
        self.zoom_machine = None
        self.machine_pick_index = 0

    def _zooming_ignored_project(self) -> bool:
        return bool(
            self.show_ignored_projects
            and self.zoom_project
            and self.zoom_project in self.ignored_projects
            and self.browse_mode != "projects"
        )

    def _showing_ignored_workflows(self) -> bool:
        return self.show_ignored_projects and bool(
            self.ignored_sessions or self._zooming_ignored_project()
        )

    def current_session(self) -> Workflow | None:
        rows = self.current_sessions()
        if not rows:
            return None
        self.workflow_index = max(0, min(self.workflow_index, len(rows) - 1))
        return rows[self.workflow_index]

    @property
    def on_sessions_tab(self) -> bool:
        tabs = self.current_tabs()
        return tabs[self.tab % len(tabs)] == "Sessions"

    @property
    def on_models_tab(self) -> bool:
        tabs = self.current_tabs()
        return tabs[self.tab % len(tabs)] == "Models"

    @property
    def on_subagents_tab(self) -> bool:
        tabs = self.current_tabs()
        return tabs[self.tab % len(tabs)] == "Subagents"

    @property
    def on_projects_tab(self) -> bool:
        tabs = self.current_tabs()
        return tabs[self.tab % len(tabs)] == "Projects"

    @property
    def on_sources_tab(self) -> bool:
        tabs = self.current_tabs()
        return tabs[self.tab % len(tabs)] == "Harnesses"

    @property
    def on_machines_tab(self) -> bool:
        # The per-scope Machines picker tab (never present in Machines MODE, which scopes
        # to one box already -- so this is only ever the time/projects-zoom picker).
        tabs = self.current_tabs()
        return tabs[self.tab % len(tabs)] == "Machines"

    def in_project_sort_context(self) -> bool:
        return (self.view == "browse" and self.browse_mode == "projects") or (
            self.view != "session" and self.on_projects_tab
        )

    def in_prices_sort_context(self) -> bool:
        # The P overlay's model list (not its per-model session drill-in) is sortable
        # by column, so it gets its own sort state (prices_sort/prices_sort_reverse).
        return self.show_prices and self.prices_model is None

    def in_subagent_sort_context(self) -> bool:
        # The session view's Subagents tab; its own sort pair, like project lists.
        return self.view == "session" and self.on_subagents_tab

    def in_trend_sort_context(self) -> bool:
        # A Trends ranked tab (Models/Providers/Harnesses/Machines), rows showing --
        # not a drilled row's session list, which is its own ranking, and not the
        # charts, whose x axis is time. Under the P overlay this is dead: prices float
        # above Trends and own the keyboard (in_prices_sort_context is checked first).
        return self.trends and self.trend_drill is None and bool(self.trend_sort_options())

    def active_session_sort_options(self) -> tuple[str, ...]:
        # "last_activity" is a Months/Years feature, per spec, deliberately not Days:
        # a single Day's Sessions list is read by start time, and an activity can run
        # into a LATER day than the one the row is filed under -- ranking the list by
        # a timestamp that can point outside its own scope would be more confusing
        # than useful there, even though the values themselves are perfectly valid.
        if self.browse_mode == "time" and self.focus == "days":
            return tuple(k for k in self.sort_options if k != "last_activity")
        return self.sort_options

    # Where a key the CONTEXT withdrew lands, as opposed to one that was never a
    # session sort key at all. The two fallbacks are different questions and want
    # different answers: sort_options[0] ("cost") is the escape hatch for an
    # unreadable/pre-split state.json, where any stable column will do. A withdrawn
    # key has a stored preference behind it, so it falls back inside its own column
    # family -- "last_activity" to "date", the other timestamp on the same column,
    # rather than to money. This is a first-frame path, not a corruption path:
    # `focus` starts on "days", so a saved "last_activity" is withdrawn on the
    # opening screen of every launch that doesn't restore a different panel.
    SORT_FALLBACKS = {"last_activity": "date"}

    # The three lists' active sort keys, each validated against its own vocabulary.
    # Headers and sorters read these directly (never each other's, and never the
    # context-dependent effective_sort_by) so that when a project list and a session
    # list share the screen neither borrows the other's sort arrow.
    def session_sort_key(self) -> str:
        options = self.active_session_sort_options()
        if self.sort_by in options:
            return self.sort_by
        fallback = self.SORT_FALLBACKS.get(self.sort_by)
        return fallback if fallback in options else options[0]

    def session_sort_reverse(self) -> bool:
        # The direction belongs to the stored column; falling back to a different
        # EFFECTIVE key (active_session_sort_options() dropped "last_activity" while
        # the Days pane is focused) must not carry that column's own reversed flag
        # onto the fallback column's natural order -- a direction flip saved for
        # "last_activity" must not silently sort the Days pane oldest-first.
        return self.sort_reverse if self.sort_by in self.active_session_sort_options() else False

    def project_sort_key(self) -> str:
        return (
            self.project_sort_by
            if self.project_sort_by in self.project_sort_options
            else self.project_sort_options[0]
        )

    def subagent_sort_key(self) -> str:
        return (
            self.subagent_sort_by
            if self.subagent_sort_by in self.subagent_sort_options
            else self.subagent_sort_options[0]
        )

    def can_sort_current_view(self) -> bool:
        return (
            self.in_prices_sort_context()
            or self.in_trend_sort_context()
            or self.in_project_sort_context()
            or (self.view != "session" and self.on_sessions_tab)
            or self.in_subagent_sort_context()
        )

    def can_filter_current_view(self) -> bool:
        # The "f" query fuzzy-filters the session list (a Sessions tab), the project
        # list (projects mode / a Projects tab), and the Models tab (by model name).
        # Months/Days, Overview, and Subagents are not query-filtered, so "f" is a
        # no-op there -- don't offer it. (Range, by contrast, narrows all_workflows
        # everywhere, so it always is.)
        return (
            self.in_project_sort_context()
            or self.on_models_tab
            or (self.view != "session" and self.on_sessions_tab)
        )

    def can_launch_current(self) -> bool:
        # On a session context; gates the footer's `L` hint so it never shows where
        # pressing it would no-op. No tmux requirement: the copy target works anywhere.
        return self.view == "session" or (self.view == "zoom" and self.on_sessions_tab)

    def effective_sort_by(self) -> str | None:
        # The sort key of the list the user is acting on -- feeds the header status
        # line and the `s` picker's "(current)" marker. Column-arrow rendering does
        # NOT go through this: each list's *_sort_heading reads its own key.
        if self.in_prices_sort_context():
            return self.prices_sort  # always a column ("eff" by default), so it arrows
        if self.in_trend_sort_context():
            return self.trend_sort_key()  # validated for the tab that is drawing
        if self.in_project_sort_context():
            return self.project_sort_key()
        if self.in_subagent_sort_context():
            return self.subagent_sort_key()
        if not self.current_sort_options():
            return None
        return self.session_sort_key()

    def sort_descending(self, key: str, reverse: bool) -> bool:
        # The on-screen order for a column: its natural direction (numbers and dates
        # high->low, text and depth a->z), flipped when the user has toggled this
        # column by clicking its header again. Drives both the sort and the ^/v arrow.
        return (key not in self.ascending_sort_keys) != reverse

    def sort_menu_options(self) -> tuple[str, ...]:
        # The sort keys valid for the view the `s` picker was opened over: the P
        # overlay uses prices_sort_options, project lists project_sort_options,
        # session/subagent lists current_sort_options.
        if self.in_prices_sort_context():
            return self.prices_sort_options
        if self.in_trend_sort_context():
            return self.trend_sort_options()
        if self.in_project_sort_context():
            return self.project_sort_options
        return self.current_sort_options()

    def open_sort_menu(self) -> None:
        # `s` no longer cycles blindly; it opens a small picker the user can j/k
        # through and Enter to apply (Esc cancels), mirroring the `H` source menu.
        if not self.can_sort_current_view():
            self.notify("sort: only session, project, subagent, or Trends ranking lists", "error")
            return
        options = self.sort_menu_options()
        if not options:
            self.notify("sort: only session, project, subagent, or Trends ranking lists", "error")
            return
        current = self.effective_sort_by()
        self.sort_menu_index = options.index(current) if current in options else 0
        self.sort_menu = True

    def apply_sort_choice(self, value: str) -> None:
        # The `s` picker always lands on a column's natural order; header re-clicks
        # are where direction gets flipped.
        if self.in_prices_sort_context():
            self.prices_sort = value
            self.prices_sort_reverse = False
            self.prices_index = 0
            self.prices_scroll = 0
            return
        if self.in_trend_sort_context():
            self._resort_trends(value, reverse=False)
            return
        if self.in_subagent_sort_context():
            self.subagent_sort_by = value
            self.subagent_sort_reverse = False
        elif self.in_project_sort_context():
            self.project_sort_by = value
            self.project_sort_reverse = False
            self.project_index = 0
        else:
            self.sort_by = value
            self.sort_reverse = False
            self.workflow_index = 0
        self.scroll = 0

    def _resort_trends(self, key: str, reverse: bool) -> None:
        # Re-order a Trends ranking, keeping the cursor on the ROW it was on rather
        # than on its ordinal -- re-sorting is exactly the moment an ordinal stops
        # meaning what it meant (drill_out's _reanchor rule). The row you were reading
        # is the row you get, wherever the new order put it.
        selected = self.selected_trend_key()
        self.trend_sort = key
        self.trend_sort_reverse = reverse
        keys = self.trend_ranked_keys()
        self.trend_row_index = keys.index(selected) if selected in keys else 0

    def selected_trend_key(self) -> str | None:
        # The ranked row under the cursor, by value; None off the ranked tabs.
        keys = self.trend_ranked_keys()
        return keys[max(0, min(self.trend_row_index, len(keys) - 1))] if keys else None

    def apply_header_sort(self, key: str, target: str) -> None:
        # A click on a column header sorts that list by the column; clicking the
        # already-active column again flips its direction (asc <-> desc). The click's
        # target ("prices"/"project"/"session") says which list was clicked, so it
        # works even when a project list and a session list show sortable headers on
        # screen at once. The choice persists on exit via save_state, like the `s` picker.
        if target == "prices":
            if key not in self.prices_sort_options:
                return
            if self.prices_sort == key:
                self.prices_sort_reverse = not self.prices_sort_reverse
            else:
                self.prices_sort = key
                self.prices_sort_reverse = False
            self.prices_index = 0
            self.prices_scroll = 0
            return
        if target == "trend":
            if key not in self.trend_sort_options():
                return
            # Compared against the EFFECTIVE key and direction, not the stored ones: a
            # tab that withdrew the stored column shows cost's arrow, so clicking Cost
            # there must flip what is ON SCREEN rather than re-arm the withdrawn
            # preference or invert against a direction the tab isn't honouring.
            flip = key == self.trend_sort_key()
            self._resort_trends(key, reverse=not self.trend_sort_reverse_for() if flip else False)
            return
        if target == "subagent":
            if key not in self.subagent_sort_options:
                return
            if self.subagent_sort_by == key:
                self.subagent_sort_reverse = not self.subagent_sort_reverse
            else:
                self.subagent_sort_by = key
                self.subagent_sort_reverse = False
            self.scroll = 0
            return
        if target == "project":
            if key not in self.project_sort_options:
                return
            if self.project_sort_by == key:
                self.project_sort_reverse = not self.project_sort_reverse
            else:
                self.project_sort_by = key
                self.project_sort_reverse = False
            self.project_index = 0
        else:
            if key not in self.active_session_sort_options():
                return
            if self.sort_by == key:
                self.sort_reverse = not self.sort_reverse
            else:
                self.sort_by = key
                self.sort_reverse = False
            self.workflow_index = 0
        self.scroll = 0

    def handle_sort_menu_key(self, key: int | str) -> bool:
        # The `s` sort picker: down/up move, select applies, cancel closes. advance
        # (`s` again) walks the highlight so repeated taps still move. Mirrors
        # handle_source_menu_key.
        options = self.sort_menu_options()
        if not options:
            self.sort_menu = False
            return True
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("menu.sort", key)
        if act in ("down", "advance"):
            self.sort_menu_index = (self.sort_menu_index + 1) % len(options)
        elif act == "up":
            self.sort_menu_index = (self.sort_menu_index - 1) % len(options)
        elif act == "first":
            self.sort_menu_index = 0
        elif act == "last":
            self.sort_menu_index = len(options) - 1
        elif act == "select":
            self.sort_menu = False
            self.apply_sort_choice(options[self.sort_menu_index % len(options)])
        elif act == "cancel":
            self.sort_menu = False  # cancel, order unchanged
        # any other key: ignore and keep the menu open
        return True

    FOCUS_CYCLE = ("years", "months", "days")

    def active_tab_name(self) -> str:
        # The selected tab as a NAME. Every carry goes through this, never through the
        # raw index: the tab sets differ per scope, so an index means nothing outside
        # the scope that produced it (a session's tab 2 is Subagents; a month's is
        # Projects).
        tabs = self.current_tabs()
        return tabs[self.tab % len(tabs)] if tabs else "Overview"

    def _carry_tab(self, name: str) -> None:
        # Land on the same tab where the new scope has one (Models stays on Models),
        # else on its Overview -- e.g. Days has no Models tab, and no browse scope has
        # a session's Subagents/Turns.
        tabs = self.current_tabs()
        self.tab = tabs.index(name) if name in tabs else 0

    def set_focus(self, name: str) -> None:
        active_tab = self.active_tab_name()
        self.focus = name
        self._carry_tab(active_tab)
        self.scroll = 0
        self._clear_zoom_drills()

    def cycle_focus(self, step: int = 1) -> None:
        # Tab walks the three stacked time panels (Years -> Months -> Days); Shift-Tab
        # walks back. No-op in session view and projects/machines mode (one left list,
        # nothing to cycle focus across).
        if self.view == "session" or self.flat_browse_mode:
            return
        i = self.FOCUS_CYCLE.index(self.focus) if self.focus in self.FOCUS_CYCLE else 1
        self.set_focus(self.FOCUS_CYCLE[(i + step) % len(self.FOCUS_CYCLE)])

    def toggle_focus(self) -> None:
        self.cycle_focus(1)

    # lazygit's numbered panels: a digit is a jump, not a walk. 1/2/3 are the stacked
    # sidebar panels top to bottom (Years/Months/Days -- in projects mode there is only
    # one left panel, so 1 is the Projects list and 2/3 have nothing to focus), and 0 is
    # the pane on the right, the way lazygit numbers its main view 0. The digits are
    # position-based, not scope-based: what you press is where you look.

    def focus_panel(self, name: str) -> bool:
        # Jump to a sidebar panel. A digit is a jump from *anywhere* (lazygit's rule),
        # so it steps out of a zoom or an open session first -- it always lands you in
        # that panel rather than beside an active detail pane.
        #
        # Read the tab name BEFORE leaving the view, and re-map it after: current_tabs()
        # answers for the view we are in, so reading it afterwards would reinterpret a
        # session's tab index against the browse tabs (Subagents -> Projects). The
        # re-map runs even when the target panel is the one already focused -- that is
        # exactly the jump-out-of-a-session case, where the stale index is wrong.
        active_tab = self.active_tab_name()
        if self.flat_browse_mode:
            # One left panel here (Projects / Machines); only 1 names it. 2/3 have
            # nothing to focus -- leave the view alone rather than half-obeying.
            if name != "years":
                return False
            self._return_to_browse()
            self._carry_tab(active_tab)
            return True
        self._return_to_browse()
        self.focus = name
        self._carry_tab(active_tab)
        self.scroll = 0
        self._clear_zoom_drills()
        return True

    def focus_detail(self) -> bool:
        # 0: the pane on the right. In browse that means making the detail the active
        # pane -- which is exactly what zoom is (drill_in), same as Enter. In a zoom
        # (or a session, itself full-screen) it is already the pane you are in.
        if self.view != "browse":
            return False
        self.drill_in()
        return True

    def _return_to_browse(self) -> None:
        # Back to the split with the sidebar active, dropping a Projects/Sources drill
        # with it (those scope the detail pane we are leaving).
        if self.view == "browse":
            return
        self.view = "browse"
        self._clear_zoom_drills()
        self.scroll = 0

    def toggle_zoom_maximized(self) -> None:
        # `+` in a zoomed detail: full-screen vs the lazygit split. A pref, not a
        # mode -- it persists (state.json) so "always maximize" is one keypress.
        self.zoom_maximized = not self.zoom_maximized
        self.notice = "detail maximized" if self.zoom_maximized else "split view"

    def _capture_mode_memory(self) -> dict:
        # The view position within a browse mode, anchored BY VALUE (session id, project
        # dir, month/day, machine/harness/model names -- not raw indices) so it survives a
        # sort reorder or a range/filter change between leaving the mode and returning: the
        # spot is re-found, not re-indexed. The zoom picker cursors are the one raw part,
        # and they're cosmetic (the drill they index is armed by value; the picker isn't
        # even on screen once it has moved you to Sessions).
        tabs = self.current_tabs()
        return {
            "view": self.view,
            "focus": self.focus,
            "tab_name": tabs[self.tab % len(tabs)] if tabs else None,
            "scroll": self.scroll,
            "anchor": self.selection_anchor(),
            "zoom_project": self.zoom_project,
            "zoom_source": self.zoom_source,
            "zoom_model": self.zoom_model,
            "zoom_machine": self.zoom_machine,
            "source_index": self.source_index,
            "model_pick_index": self.model_pick_index,
            "machine_pick_index": self.machine_pick_index,
        }

    def _disarm_mode_memory_drills(self, keep_project: bool = False) -> None:
        # Every reload resets the drills of the mode you're STANDING IN (they name a
        # harness / project / model / box the new data may not have). The other modes'
        # snapshots name the same vanished things and were restored unchecked, so coming
        # back to one re-armed a phantom filter and showed an EMPTY session list beside a
        # perfectly full dataset (`H` to one backend, then `p`/`m`: still scoped to the
        # harness you'd drilled before the switch). The rest of a snapshot is anchored by
        # value and re-found by restore_selection, so only the drills need disarming.
        #
        # `keep_project` mirrors what the CALLER does to the active mode, so one reload
        # can't treat the mode you happen to be in differently from the others: the
        # restore path keeps a project drill that still exists (and never in Machines
        # mode, where the drill is per-box), `r` and a fresh swap drop it outright.
        for mode, saved in self._mode_memory.items():
            keep = (
                keep_project
                and mode != "machines"
                and saved["zoom_project"]
                and any(
                    self.project_root(w.directory) == saved["zoom_project"] for w in self.loaded
                )
            )
            saved["zoom_project"] = saved["zoom_project"] if keep else None
            saved["zoom_source"] = saved["zoom_model"] = saved["zoom_machine"] = None

    def _remember_mode_position(self) -> None:
        # Snapshot the current mode's spot into _mode_memory so a later return restores the
        # session/tab/drill. set_browse_mode calls this on every switch; the Trends date/month
        # drills, which jump straight to time browse, call it too (else drilling through Trends
        # from a Projects/Machines session would lose that mode's remembered position).
        self._mode_memory[self.browse_mode] = self._capture_mode_memory()

    def set_browse_mode(self, mode: str) -> None:
        if mode == self.browse_mode:
            return
        # Remember where we were in the mode we're leaving (session, tab, drills and all),
        # then restore the target mode's remembered spot if we've been there -- otherwise
        # open it fresh at the top. The snapshot is value-anchored, so it self-heals against
        # data changes (see _capture_mode_memory) and needs no cache-invalidation hook.
        self._remember_mode_position()
        self.browse_mode = mode
        saved = self._mode_memory.get(mode)
        if saved is not None:
            self._restore_mode_memory(saved)
            return
        self.view = "browse"
        self.tab = 0
        self.scroll = 0
        self.workflow_index = 0
        if mode == "machines":
            self.machine_index = 0
        self._clear_zoom_drills()

    def _restore_mode_memory(self, saved: dict) -> None:
        self.focus = saved["focus"]
        # Value-based drills first: restore_selection scopes current_sessions() by them
        # (a Machines-mode box is filtered by its armed harness/model), so they must be set
        # before the session lookup below reads that list.
        self.zoom_project = saved["zoom_project"]
        self.zoom_source = saved["zoom_source"]
        self.zoom_model = saved["zoom_model"]
        self.zoom_machine = saved["zoom_machine"]
        self.source_index = saved["source_index"]
        self.model_pick_index = saved["model_pick_index"]
        self.machine_pick_index = saved["machine_pick_index"]
        # zoom_maximized is deliberately NOT restored: it's a single global full-screen
        # preference (persisted in state.json), so it must NOT roll back to a per-mode value
        # when you return -- toggling it off in one mode stays off in the others.
        # Re-find the scope/session BY VALUE (id/dir/name), so a reorder or a dropped row
        # lands on the SAME thing or clamps, never a wrong-but-valid neighbour by index.
        self.restore_selection(saved["anchor"])
        self.view = saved["view"]
        # A session view is only honoured if THAT session is still present: a range/filter
        # change may have removed it, and restore_selection would then clamp onto a
        # neighbour -- silently opening the wrong session. Demote to the zoom scope instead.
        saved_session_id = saved["anchor"].session
        if self.view == "session":
            current = self.current_session()
            if current is None or current.id != saved_session_id:
                self.view = "zoom"
        tabs = self.current_tabs()
        tab_name = saved["tab_name"]
        # Resolve the tab by NAME (a demoted view or a different session has different
        # tabs); fall back to the first tab rather than a stale index into another tab set.
        self.tab = tabs.index(tab_name) if tab_name in tabs else 0
        self.scroll = max(0, int(saved["scroll"]))

    def drill_in(self) -> None:
        if self.view == "browse":
            item = (
                self.selected_machine_summary
                if self.browse_mode == "machines"
                else self.selected_project_summary
                if self.browse_mode == "projects"
                else self.selected_year_summary
                if self.focus == "years"
                else self.selected_month_summary
                if self.focus == "months"
                else self.selected_day_summary
            )
            if item is not None:
                self.view = "zoom"
                self.scroll = 0
                self.workflow_index = 0
                self.zoom_project = None
                self.zoom_source = None
                self.source_index = 0
                self.zoom_model = None
                self.model_pick_index = 0
                self.zoom_machine = None
                self.machine_pick_index = 0
                self._trend_return = (
                    None  # a fresh drill; the Trends overlay re-arms it if it began one
                )
                if self.browse_mode != "projects":
                    # In time mode, project_index is only the zoom Projects-tab
                    # picker; reset it. In projects mode it is the selected project
                    # we are drilling into, so it must be left alone.
                    self.project_index = 0
        elif self.view == "zoom" and self.on_projects_tab and self.browse_mode != "projects":
            # Pick a project -> its sessions in this scope. In time mode that's within the
            # zoomed month/day; in Machines mode within the selected box (projects mode has
            # no Projects tab -- its sidebar IS the project -- so the guard just excludes it).
            project = self.zoom_selected_project()
            if project is not None:
                self.zoom_project = project.directory
                if self.browse_mode == "machines":  # the box's drills are mutually exclusive
                    self.zoom_source = self.zoom_model = None
                tabs = self.current_tabs()
                if "Sessions" in tabs:
                    self.tab = tabs.index("Sessions")
                self.workflow_index = 0
                self.scroll = 0
        elif self.view == "zoom" and self.on_sources_tab:
            # Pick a source in a zoom -> its sessions in this scope (the Trends Sources
            # drill, scoped to the zoomed year/month/day/project -- or, in Machines mode,
            # to the selected box, so "Claude Code on hermes" opens with one Enter).
            source = self.zoom_selected_source()
            if source is not None:
                self.zoom_source = source
                if self.browse_mode == "machines":
                    self.zoom_project = self.zoom_model = None
                tabs = self.current_tabs()
                if "Sessions" in tabs:
                    self.tab = tabs.index("Sessions")
                self.workflow_index = 0
                self.scroll = 0
        elif self.view == "zoom" and self.on_models_tab:
            # Pick a model -> this scope's sessions that used it. A membership filter, not
            # a partition like source/project/machine: a session can use several models, so
            # it layers ON TOP of whatever else is armed rather than replacing it (in a box
            # the drills stay mutually exclusive, below). Available in every zoom with a
            # Models tab, not just a box -- "which sessions this month ran Opus" had no
            # other path: the Trends and `P` model drills are both app-wide.
            model = self.zoom_selected_model()
            if model is not None:
                self.zoom_model = model
                self.query = ""  # the Models-name query must not become a session query
                if self.browse_mode == "machines":  # a box's drills are mutually exclusive
                    self.zoom_source = self.zoom_project = None
                self.tab = 0  # Economics is the model scope's landing page.
                self.workflow_index = 0
                self.scroll = 0
        elif self.view == "zoom" and self.on_machines_tab:
            # Pick a box in a month/day/project zoom -> its sessions in this scope (the
            # fleet's per-scope Machines picker). Only reachable off Machines MODE, so no
            # extra mode guard is needed (the tab isn't injected there).
            machine = self.zoom_selected_machine()
            if machine is not None:
                self.zoom_machine = machine
                tabs = self.current_tabs()
                if "Sessions" in tabs:
                    self.tab = tabs.index("Sessions")
                self.workflow_index = 0
                self.scroll = 0
        elif self.view == "zoom" and self.on_sessions_tab and self.current_session():
            self.view = "session"
            self.tab = 0
            self.scroll = 0
            # Individually expanded Turns prompts are this session's; a leftover set from
            # the last one would spuriously light the turn-column header (any_open) and,
            # on a prompt-id collision, auto-expand a group that was never opened here.
            self.turn_drill = None
            self._turn_cursor = 0  # the Turns cursor starts on the first prompt

    def _reanchor(self, cursor: str, value, keys: list) -> None:
        # Put a picker's cursor back on the row it was drilled FROM, found by value in the
        # ranking as it stands NOW. Esc's contract for every drill branch is "back to the
        # row you came from", and the stored ordinal cannot keep that promise: `$` re-ranks
        # these tables by a different cost, so a drill armed at row 1 and popped after a
        # `$` toggle used to land on whatever had since taken row 1. A value that is gone
        # from the ranking leaves the cursor alone -- clamped by the paint, never moved to
        # a row that means something else.
        if value in keys:
            setattr(self, cursor, keys.index(value))

    def drill_out(self) -> None:
        if self.view == "session":
            self._clear_trace_expansion()
            self.view = "zoom"
            tabs = self.current_tabs()  # land back on the Sessions tab we came from
            self.tab = tabs.index("Sessions") if "Sessions" in tabs else 0
        elif self.view == "zoom":
            if self.zoom_model:
                # Popped FIRST: a model drill is a membership filter layered on top of
                # whatever partition was already armed (it clears nothing outside a box),
                # so it is always the innermost scope -- Esc has to undo it before the
                # project/harness drill it was stacked on.
                self._reanchor(
                    "model_pick_index", self.zoom_model, [m for m, _ in self.zoom_model_rows()]
                )
                self.zoom_model = None
                tabs = self.current_tabs()
                self.tab = tabs.index("Models") if "Models" in tabs else 0
            elif self.zoom_source:
                # Leave a source's sessions, back to the Sources list of this zoom
                # (popped before a project drill: it was layered on top of one).
                self._reanchor(
                    "source_index", self.zoom_source, [s for s, _ in self.zoom_source_rows()]
                )
                self.zoom_source = None
                tabs = self.current_tabs()
                self.tab = tabs.index("Harnesses") if "Harnesses" in tabs else 0
            elif self.zoom_machine:
                # Leave a box's sessions, back to the Machines list of this zoom.
                self._reanchor(
                    "machine_pick_index",
                    self.zoom_machine,
                    [m for m, _ in self.zoom_machine_rows()],
                )
                self.zoom_machine = None
                tabs = self.current_tabs()
                self.tab = tabs.index("Machines") if "Machines" in tabs else 0
            elif self.zoom_project and self.browse_mode != "projects":
                # Leave a project's sessions, back to the Projects list of this zoom.
                self._reanchor(
                    "project_index", self.zoom_project, [p.directory for p in self.zoom_projects()]
                )
                self.zoom_project = None
                tabs = self.current_tabs()
                self.tab = tabs.index("Projects") if "Projects" in tabs else 0
            else:
                self.view = "browse"
                self._clear_zoom_drills()
                if self._trend_return is not None:
                    self._reopen_trends(self._trend_return)
        self.scroll = 0

    def _reopen_trends(self, ret: tuple) -> None:
        # Stepping out of a scope we drilled into from the Trends overlay returns to
        # it: reopen the tab we came from with the cursor back where it was. Bails
        # (stays in browse) when the bucket is gone (range/source changed).
        self._trend_return = None
        tab = ret[0]
        if tab == "drill":
            _tag, kind, key, row = ret
            ranked = {
                "model": "Models",
                "provider": "Providers",
                "project": "Projects",
                "source": "Harnesses",
                "machine": "Machines",
            }[kind]
            if ranked not in self.trend_tabs:
                return  # the tab itself is gone -- Machines after the fleet went away
            self.trends = True
            self.trend_tab = self.trend_tabs.index(ranked)
            keys = self.trend_ranked_keys()
            if key not in keys:
                self.trend_drill = None
                return
            self.trend_row_index = keys.index(key)
            self.trend_drill = (kind, key)
            self.trend_drill_index = row
            return
        key = ret[1]
        if tab == "Calendar":
            years = self.calendar_years()
            yi = next((i for i, y in enumerate(years) if y == key[:4]), None)
            if yi is None:
                return
            self.trend_year_index = yi
            self.cal_cursor = key
        elif tab == "Daily":
            months = self.trend_months()
            if key[:7] not in months:
                return
            self.trend_month_index = months.index(key[:7])
            self.trend_cursor = key
        elif tab == "Weekly":
            weeks = self.trend_weeks()
            wk = week_key(key)
            if wk not in weeks:
                return
            self.trend_week_index = weeks.index(wk)
            self.trend_cursor = key
        elif tab == "Monthly":
            if not self.trend_months():
                return
            self.trend_cursor = key
        self.trends = True
        self.trend_tab = self.trend_tabs.index(tab)
        self.trend_focus = True  # we drilled in from a focused canvas; resume there

    def move(self, delta: int) -> None:
        if self.view == "session":
            if self._on_turns_tab():
                if self.active_turn_drill is None:
                    if self._move_turn_cursor(delta):
                        return
                elif self.active_trace_drill is None and self._move_trace_cursor(delta):
                    return
            self.scroll = max(0, self.scroll + delta)
        elif self.view == "zoom":
            if self.on_sessions_tab:
                n = len(self.current_sessions())
                if n:
                    self.workflow_index = max(0, min(self.workflow_index + delta, n - 1))
            elif self.on_projects_tab and self.browse_mode != "projects":
                n = len(self.zoom_projects())
                if n:
                    self.project_index = max(0, min(self.project_index + delta, n - 1))
            elif self.on_sources_tab:
                n = len(self.zoom_source_rows())
                if n:
                    self.source_index = max(0, min(self.source_index + delta, n - 1))
            elif self.on_models_tab:
                n = len(self.zoom_model_rows())
                if n:
                    # Clamp BEFORE stepping. The ranking can shrink or re-order under the
                    # cursor without any keypress (`x` clearing the filter, `$` re-ranking
                    # by a different cost, a restored mode snapshot), and the paint already
                    # clamps what it highlights -- so stepping from the raw index spends
                    # the first press re-clamping to the row the highlight is on and reads
                    # as a dead key. Same reason the renderer clamps rather than bails.
                    cur = max(0, min(self.model_pick_index, n - 1))
                    self.model_pick_index = max(0, min(cur + delta, n - 1))
            elif self.on_machines_tab:
                n = len(self.zoom_machine_rows())
                if n:
                    self.machine_pick_index = max(0, min(self.machine_pick_index + delta, n - 1))
            else:
                self.scroll = max(0, self.scroll + delta)
        elif self.browse_mode == "machines":
            n = len(self.machines)
            if n:
                self.machine_index = max(0, min(self.machine_index + delta, n - 1))
        elif self.browse_mode == "projects":
            n = len(self.projects)
            if n:
                self.project_index = max(0, min(self.project_index + delta, n - 1))
        elif self.focus == "years":
            n = len(self.years)
            if n:
                self.year_index = max(0, min(self.year_index + delta, n - 1))
            # Changing the year rebuilds the months list, so re-anchor both panels.
            self.month_index = 0
            self.day_index = 0
        elif self.focus == "months":
            n = len(self.months)
            if n:
                self.month_index = max(0, min(self.month_index + delta, n - 1))
            self.day_index = 0  # re-anchor the day panel when the month changes
        else:  # days
            n = len(self.panel_days)
            if n:
                self.day_index = max(0, min(self.day_index + delta, n - 1))

    def _clear_box_drills(self) -> None:
        # Machines mode re-scoped to another box: its Harnesses/Projects/Models drills
        # (zoom_source/zoom_project/zoom_model) and the cursors that index their pickers
        # belonged to the OLD box. Drop the drills so the new box's Sessions aren't
        # silently filtered by the previous scope, and zero the cursors so a smaller box's
        # picker opens at the top instead of a now-out-of-range row (a dead first j/k).
        self.zoom_source = None
        self.zoom_project = None
        self.zoom_model = None
        self.source_index = 0
        self.project_index = 0
        self.model_pick_index = 0

    def _clear_model_drill(self) -> None:
        # Drop the model drill AND its cursor together -- they are one selection. The
        # cursor is an ordinal into a row set that any scope, mode or reload change
        # rebuilds, so clearing only the drill leaves j/k counting from a row that no
        # longer exists: the first press clamps back onto the row already highlighted and
        # reads as a dead key. (drill_out's own pop deliberately KEEPS the cursor -- it
        # returns to the same table, on the row you drilled from.)
        self.zoom_model = None
        self.model_pick_index = 0

    # How deep each time panel sits. The zoomed detail is scoped by the FOCUSED panel, so
    # a wheel over a deeper one (Months while Years has focus) moves a list without
    # re-scoping anything -- see _wheel_rescoped.
    _PANEL_DEPTH = {"year": 0, "month": 1, "day": 2}
    _FOCUS_DEPTH = {"years": 0, "months": 1, "days": 2}

    def _wheel_rescoped(self, panel: str, changed: bool) -> None:
        # Wheeling the sidebar onto a DIFFERENT scope disarms every drill, exactly as a
        # sidebar click does (_apply_click) and as the Machines branch does through
        # _clear_box_drills -- and, like that branch, only when the row actually changed,
        # so wheeling against the end of a list keeps the drill. They have to go because
        # they HIDE: the new month/project may never have had that harness or model, and
        # a survivor silently empties the Sessions list.
        #
        # But only when the change actually re-scopes the DETAIL. The wheel deliberately
        # scrolls whatever panel the pointer is over without moving focus, and the detail
        # follows the FOCUSED panel -- so spinning Months while Years has focus (or Days
        # under either) re-anchors a list the detail does not read, and disarming there
        # would throw away a drill for a scope that never changed.
        if self.view != "zoom":
            return
        if self.browse_mode == "time" and panel in self._PANEL_DEPTH:
            depth = self._PANEL_DEPTH[panel]
            focus = self._FOCUS_DEPTH.get(self.focus, 2)
            if depth > focus:
                return  # a panel below the focused one: the detail never reads it
            if not changed:
                # The panel itself could not move (wheeled against its end), but spinning
                # it still re-anchors the panels BELOW to their first row -- which
                # re-scopes only when the focus is actually down there.
                changed = (depth < 1 <= focus and bool(self.month_index)) or (
                    depth < 2 <= focus and bool(self.day_index)
                )
        if not changed:
            return
        self._clear_source_drill()
        self._clear_machine_drill()
        self._clear_model_drill()
        self._clear_project_drill()  # itself a no-op on the projects-mode sidebar

    def _wheel(self, my: int, mx: int, delta: int) -> None:
        # Scroll the panel the cursor is over -- active or not. The wheel used to always
        # move the active pane, so hovering a non-focused panel scrolled the wrong one.
        # Route by the region kind under the cursor (a sidebar list, a zoom picker, or the
        # detail content); a gap or the tab strip falls back to the active pane.
        hit = self.renderer.hit(my, mx)
        kind = hit[0] if hit else None
        if kind == "year" and self.years:
            new = max(0, min(self.year_index + delta, len(self.years) - 1))
            self._wheel_rescoped("year", new != self.year_index)
            self.year_index = new
            self.month_index = self.day_index = 0  # the months list rebuilds under it
        elif kind == "month" and self.months:
            new = max(0, min(self.month_index + delta, len(self.months) - 1))
            self._wheel_rescoped("month", new != self.month_index)
            self.month_index = new
            self.day_index = 0  # re-anchor the day panel when the month changes
        elif kind == "day" and self.panel_days:
            new = max(0, min(self.day_index + delta, len(self.panel_days) - 1))
            self._wheel_rescoped("day", new != self.day_index)
            self.day_index = new
        elif kind == "project" and self.projects:
            new = max(0, min(self.project_index + delta, len(self.projects) - 1))
            self._wheel_rescoped("project", new != self.project_index)
            self.project_index = new
        elif kind == "machine" and self.machines:
            new = max(0, min(self.machine_index + delta, len(self.machines) - 1))
            if new != self.machine_index and self.view == "zoom":
                self._clear_box_drills()  # a NEW box; wheeling in place must keep the drill
            self.machine_index = new
        elif kind == "session":
            n = len(self.current_sessions())
            if n:
                self.workflow_index = max(0, min(self.workflow_index + delta, n - 1))
        elif kind == "zoomproject":
            n = len(self.zoom_projects())
            if n:
                self.project_index = max(0, min(self.project_index + delta, n - 1))
        elif kind == "zoomsource":
            n = len(self.zoom_source_rows())
            if n:
                self.source_index = max(0, min(self.source_index + delta, n - 1))
        elif kind == "zoommodel":
            n = len(self.zoom_model_rows())
            if n:
                self.model_pick_index = max(0, min(self.model_pick_index + delta, n - 1))
        elif kind == "zoommachine":
            n = len(self.zoom_machine_rows())
            if n:
                self.machine_pick_index = max(0, min(self.machine_pick_index + delta, n - 1))
        elif kind in ("detail", "turnline"):
            self.scroll = max(0, self.scroll + delta)  # scroll the detail content
        else:
            self.move(delta)  # a gap or the tab strip: the active pane, as before

    def _page_step(self, stdscr: curses.window | None) -> int:
        # Half the visible pager height (the window minus chrome, measured by the
        # renderer so it can't drift from max_scroll) -- the PgDn/PgUp, Ctrl-D/Ctrl-U
        # stride.
        if stdscr is None:
            return 10
        return max(1, self.renderer.pager_height(stdscr) // 2)

    def jump(self, to_end: bool, stdscr: curses.window | None = None) -> None:
        if self.view == "browse":
            if self.browse_mode == "machines":
                rows = self.machines
                if rows:
                    self.machine_index = len(rows) - 1 if to_end else 0
            elif self.browse_mode == "projects":
                rows = self.projects
                if rows:
                    self.project_index = len(rows) - 1 if to_end else 0
            elif self.focus == "years":
                rows = self.years
                if rows:
                    self.year_index = len(rows) - 1 if to_end else 0
                    self.month_index = 0
                    self.day_index = 0
            elif self.focus == "months":
                rows = self.months
                if rows:
                    self.month_index = len(rows) - 1 if to_end else 0
                    self.day_index = 0
            else:
                rows = self.panel_days
                if rows:
                    self.day_index = len(rows) - 1 if to_end else 0
            return

        if self.view == "zoom" and self.on_sessions_tab:
            rows = self.current_sessions()
            if rows:
                self.workflow_index = len(rows) - 1 if to_end else 0
            return

        if self.view == "zoom" and self.on_projects_tab and self.browse_mode != "projects":
            rows = self.zoom_projects()
            if rows:
                self.project_index = len(rows) - 1 if to_end else 0
            return

        if self.view == "zoom" and self.on_sources_tab:
            rows = self.zoom_source_rows()
            if rows:
                self.source_index = len(rows) - 1 if to_end else 0
            return

        if self.view == "zoom" and self.on_models_tab:
            rows = self.zoom_model_rows()
            if rows:
                self.model_pick_index = len(rows) - 1 if to_end else 0
            return

        if self.view == "zoom" and self.on_machines_tab:
            rows = self.zoom_machine_rows()
            if rows:
                self.machine_pick_index = len(rows) - 1 if to_end else 0
            return

        if self._on_turns_tab() and self.active_turn_drill is None:
            wf = self.current_session()
            groups = self.turn_runs(wf.id) if wf else []
            if groups:
                self._turn_cursor = len(groups) - 1 if to_end else 0
                self._turn_follow = True
                return
        if not to_end:
            self.scroll = 0
            return
        if stdscr is None:
            self.scroll = 10_000
            return
        self.scroll = self.renderer.max_scroll(stdscr)

    # --- Toast notifications --------------------------------------------------
    # `self.notice = "..."` stays the one-liner the whole codebase already uses; it
    # now routes through notify() and surfaces as a floating, auto-dismissing toast
    # instead of a header segment. Reading `self.notice` returns the latest message
    # (kept for tests and any caller that peeks at it). Assignment means neutral
    # info BY DEFINITION; a coloured toast passes notify(text, kind) at the call
    # site. The kind is never inferred from the message text -- wording changes
    # must not change colour, and user data interpolated into a message (session
    # titles, paths, commands) must never leak into the classification.
    @property
    def toasts(self) -> list[Toast]:
        toasts = self.__dict__.get("_toasts")
        if toasts is None:
            toasts = self.__dict__["_toasts"] = []
        return toasts

    @property
    def toast_log(self) -> list[Toast]:
        # The `N` overlay's scrollback: every toast notify() ever raised (oldest first),
        # capped at TOAST_LOG_MAX and NEVER pruned by expiry -- the whole point is to
        # read a message after its live card has faded. Lazily materialised like `toasts`
        # so a __new__-built App (tests) has one on demand.
        log = self.__dict__.get("_toast_log")
        if log is None:
            log = self.__dict__["_toast_log"] = []
        return log

    @property
    def notice(self) -> str:
        toasts = self.toasts
        return toasts[-1].text if toasts else ""

    @notice.setter
    def notice(self, value: str) -> None:
        if value:
            self.notify(value)
        else:
            self.toasts.clear()  # `self.notice = ""` means "no message" (the log persists)

    def toast_now(self) -> float:
        return self._toast_clock()

    def notify(self, text: str, kind: str = "info") -> None:
        toasts = self.toasts
        if not text:
            toasts.clear()  # clears the live card only; the N scrollback is history
            return
        toast = Toast(text, kind, self.toast_now(), self.TOAST_TTL)
        # Several notices set within one input handler (e.g. "fetching…" → "refreshed")
        # never get a frame between them, so collapse onto one toast; distinct user
        # actions (a paint happened in between) stack instead. The scrollback mirrors
        # that collapse so it records what was actually shown, not the discarded midpoint.
        collapsing = bool(toasts) and not self._toast_shown
        if collapsing:
            toasts[-1] = toast
        else:
            toasts.append(toast)
            del toasts[: max(0, len(toasts) - self.TOAST_MAX)]
        log = self.toast_log
        if collapsing and log:
            log[-1] = toast
        else:
            log.append(toast)
            del log[: max(0, len(log) - self.TOAST_LOG_MAX)]
        self._toast_shown = False

    def open_notices(self) -> None:
        # `N`: open the notices scrollback, landing at the top (newest first).
        self.toast_history = True
        self.toast_history_scroll = 0

    def edit_keymap(self, stdscr: curses.window | None) -> None:
        # `K`: suspend curses, open keymap.conf in $EDITOR, and reload the bindings
        # the moment the editor returns -- edit, save, quit, and the new keys are
        # live, with every problem in the file toasted rather than fatal. The file
        # is (re)installed first so K works even before any conf exists.
        path = bindings.ensure_user_keymap()
        if stdscr is None or not hasattr(stdscr, "refresh"):
            # Headless (tests / screen doubles): no terminal to hand an editor.
            self.notify(f"keymap: edit {short_path(path, 60)}", "info")
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            cmd = shlex.split(editor) + [path]  # $EDITOR may carry flags ("code -w")
        except ValueError:
            cmd = [editor, path]
        import subprocess  # deferred: only this path pays for it

        curses.def_prog_mode()
        curses.endwin()  # hand the real terminal to the editor
        error = ""
        try:
            subprocess.call(cmd)
        except OSError as exc:
            error = str(exc)
        finally:
            curses.reset_prog_mode()
            with contextlib.suppress(curses.error):
                curses.curs_set(0)
            # Re-arm the mouse: endwin resets the terminal's tracking mode, and a
            # reload must not silently cost the wheel and clicks run() enabled.
            with contextlib.suppress(curses.error, AttributeError):
                curses.mousemask(
                    curses.BUTTON1_CLICKED
                    | curses.BUTTON1_DOUBLE_CLICKED
                    | curses.BUTTON4_PRESSED
                    | getattr(self, "_wheel_down", 0)
                )
            stdscr.clear()
            stdscr.refresh()
        if error:
            self.notify(f"editor failed ({editor}): {error}", "error")
            return
        self.reload_keymap()

    def reload_keymap(self) -> None:
        # Re-read keymap.conf into the live resolver. Warnings land one per toast in
        # the N scrollback (marked shown in between so they stack instead of
        # collapsing onto each other), with a summary card on top.
        self.keymap = bindings.load_user_keymap()
        if self.announce_keymap_warnings():
            return
        self.notify("keymap reloaded", "success")

    def announce_keymap_warnings(self) -> bool:
        # Toast every problem the last keymap load found (startup and K both come
        # through here). True when there was anything to say.
        warns = self.keymap.warnings
        if not warns:
            return False
        for w in warns:
            self.notify(f"keymap: {w}", "error")
            self._mark_toasts_shown()  # stack: each problem is its own line in N
        plural = "s" if len(warns) != 1 else ""
        notices = self.keymap.label("main", "notices") or "the notices list"
        self.notify(f"keymap: {len(warns)} problem{plural} — press {notices} for the list", "error")
        return True

    def active_toasts(self) -> list[Toast]:
        # Drop expired toasts (in place) and return what's still on screen.
        now = self.toast_now()
        self.toasts[:] = [t for t in self.toasts if t.remaining(now) > 0]
        return self.toasts

    def _mark_toasts_shown(self) -> None:
        self._toast_shown = True

    def _input_timeout_ms(self) -> int:
        # Poll for worker completion and fading toasts without requiring a keystroke.
        return self.TOAST_POLL_MS if self.toasts or self._remote_trace_job is not None else -1

    def run(self, stdscr: curses.window) -> None:
        try:
            self._run(stdscr)
        finally:
            pending = self._remote_trace_job
            self._clear_trace_expansion()
            if pending is not None:
                pending[3].thread.join(timeout=2.0)

    def _run(self, stdscr: curses.window) -> None:
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # vt220 and kin can't hide the cursor; a visible one beats a crash
        # Colours come from the active theme (opentab/themes.py, shared with the web
        # browser). The renderer maps its role hexes onto the fixed color-pair layout --
        # exact via init_color on true-colour terminals, nearest-256 otherwise, and the
        # generated ANSI ramp on 8-colour. Foreground-only: a TUI paints over the
        # terminal's own background. Live theme switches re-run init_theme_colors().
        # On a monochrome terminal (vt220) start_color *succeeds* but reports
        # curses.COLORS == 0 (measured; has_colors() is False and COLOR_PAIRS is 0,
        # so even init_pair(1, ...) raises). colors_ok False skips every init_pair
        # and use_default_colors -- the pairs then all mean "terminal default",
        # which is monochrome rendered honestly: bold/reverse still work.
        self.colors_ok = getattr(curses, "COLORS", 0) > 0
        if self.colors_ok:
            curses.use_default_colors()
        self.has256 = getattr(curses, "COLORS", 0) >= 256  # draw_calendar's ramp granularity
        self.renderer.init_theme_colors()
        stdscr.keypad(True)
        # Wheel-down is BUTTON5, but some curses builds (notably macOS system
        # ncurses) don't expose BUTTON5_PRESSED; on them the wheel-down bit is the
        # one otherwise labelled REPORT_MOUSE_POSITION, and enabling it does NOT
        # switch on motion reporting. Fall back to that so scroll-down works too.
        self._wheel_down = getattr(curses, "BUTTON5_PRESSED", 0) or getattr(
            curses, "REPORT_MOUSE_POSITION", 0
        )
        try:
            # Wheel + left click / double-click; clicks select, double-clicks drill.
            # The wheel-down bit must be in the mask or ncurses filters the event.
            curses.mousemask(
                curses.BUTTON1_CLICKED
                | curses.BUTTON1_DOUBLE_CLICKED
                | curses.BUTTON4_PRESSED
                | self._wheel_down
            )
            curses.mouseinterval(200)  # needed for click / double-click synthesis
        except (curses.error, AttributeError):
            pass  # a terminal without mouse support just keeps the keyboard

        first = True
        while True:
            self.poll_remote_trace()
            self.active_toasts()  # expire faded toasts before painting
            self.renderer.draw(stdscr)
            self._mark_toasts_shown()
            if first and self.startup_warning is None:
                # First frame is up off the fast session rollup; now do the one
                # heavy message scan, then repaint so model_count / Models tabs are
                # populated before the user's first ordinary keystroke is handled.
                # A startup warning gets input first; otherwise its visible modal would
                # look frozen while this scan blocks on a large corpus.
                first = False
                self._ensure_models()
                self.maybe_prompt_prices()  # offer a models.dev fetch if prices are missing
                self.renderer.draw(stdscr)
                self._mark_toasts_shown()
            if self.startup_warning is None and self._session_loading is not None:
                # draw_detail just painted its "loading" frame (a drilled-in session
                # whose subagents/Turns/Tools aren't memoized yet -- on a warm start
                # this is the whole backend parse). Same trick as the first frame:
                # do the blocking fetch now that the placeholder is visible, then
                # repaint immediately instead of waiting for a key.
                wf_id, self._session_loading = self._session_loading, None
                stdscr.refresh()  # make sure the loading frame actually hits the screen
                self.prefetch_session_data(wf_id)
                continue
            if self._refresh_request is not None:
                # The "refreshing…" toast painted above; now do the blocking ssh re-pull
                # and store rebuild, then repaint with the result (the _session_loading
                # trick, for a network fetch instead of a session parse).
                keys, self._refresh_request = self._refresh_request, None
                stdscr.refresh()
                self._do_refresh(keys)
                continue
            if (
                self.startup_warning is None
                and self._trace_loading is not None
                and self._remote_trace_job is None
            ):
                stdscr.refresh()
                self.load_trace_expansion()
                continue
            stdscr.timeout(self._input_timeout_ms())
            key = self._read_key(stdscr)
            if key == -1:
                continue  # idle wake while a toast fades: just re-expire and repaint
            if not self.handle_key(stdscr, key):
                break

    @staticmethod
    def _read_key(stdscr: curses.window) -> int | str:
        # get_wch reads a *character* where getch hands back raw bytes -- so a note (or a
        # title, or a project path) holding ä or 界 can be typed into the filter that
        # searches it. ASCII comes back as an int, keeping every `key == ord("x")`
        # binding downstream exactly as it was; only a non-ASCII character stays a str,
        # which nothing but the text fields accept. Special keys are ints either way --
        # and that is the whole reason to read wide: getch's byte stream cannot be told
        # apart from the KEY_* constants once it climbs past 255.
        read = getattr(stdscr, "get_wch", None)
        if read is None:  # a screen double without wide reads
            return stdscr.getch()
        try:
            key = read()
        except curses.error:
            return -1  # idle timeout: get_wch raises where getch returns -1
        if isinstance(key, str) and len(key) == 1 and key.isascii():
            return ord(key)
        return key

    @staticmethod
    def _step_trend_index(index: int, count: int, older: bool) -> int:
        # 0 == newest; higher == further back. Clamp to the available buckets.
        if older:
            return min(index + 1, max(0, count - 1))
        return max(index - 1, 0)

    def calendar_years(self) -> list[str]:
        # Years with spend in the active range, newest first -- the Calendar tab's
        # navigable buckets (index 0 == newest), shared by the key/mouse handlers.
        # week_key gates out undated rows so a "" year never reaches int(year).
        return sorted(
            {w.created_at[:4] for w in self.all_workflows if week_key(w.created_at)}, reverse=True
        )

    def _calendar_by_date(self, year: str) -> dict[str, float]:
        by_date: dict[str, float] = defaultdict(float)
        for w in self.all_workflows:
            if w.created_at[:4] == year:
                by_date[w.created_at[:10]] += w.total_cost
        return by_date

    def _effective_cursor(self, year: str, by_date: dict[str, float]) -> str | None:
        # The highlighted day: the remembered cursor while it's still in the shown
        # year, else default to the busiest day (the hottest cell draws the eye).
        if self.cal_cursor and self.cal_cursor[:4] == year:
            return self.cal_cursor
        return max(by_date, key=by_date.__getitem__) if by_date else None

    def calendar_cursor(self) -> str | None:
        years = self.calendar_years()
        if not years:
            return None
        year = years[max(0, min(self.trend_year_index, len(years) - 1))]
        return self._effective_cursor(year, self._calendar_by_date(year))

    # --- Trends bar/row data (shared by the renderer and the key/mouse handlers) ---
    def trend_months(self) -> list[str]:
        # Months with spend in the active range, newest first -- the Daily tab's
        # pager buckets (index 0 == newest).
        return sorted(
            {w.created_at[:7] for w in self.all_workflows if week_key(w.created_at)}, reverse=True
        )

    def trend_weeks(self) -> list[str]:
        # ISO-week Mondays with spend, newest first -- the Weekly tab's pager buckets.
        return sorted(
            {k for w in self.all_workflows if (k := week_key(w.created_at))}, reverse=True
        )

    def trend_daily_data(self) -> tuple[str | None, list[tuple[str, float]]]:
        # The Daily tab's chart: (shown month, [(date, cost) for each of its days]).
        months = self.trend_months()
        if not months:
            return None, []
        month = months[max(0, min(self.trend_month_index, len(months) - 1))]
        by_date: dict[str, float] = defaultdict(float)
        for w in self.all_workflows:
            if w.created_at[:7] == month:
                by_date[w.created_at[:10]] += w.total_cost
        ndays = int(month_bounds(month)[1][8:10])
        days = [f"{month}-{d:02d}" for d in range(1, ndays + 1)]
        return month, [(d, by_date.get(d, 0.0)) for d in days]

    def trend_weekly_data(self) -> tuple[str | None, list[tuple[str, float]]]:
        # The Weekly tab's chart: (shown week's Monday, [(date, cost) Mon..Sun]).
        weeks = self.trend_weeks()
        if not weeks:
            return None, []
        monday = weeks[max(0, min(self.trend_week_index, len(weeks) - 1))]
        start = datetime.strptime(monday, "%Y-%m-%d")
        by_date: dict[str, float] = defaultdict(float)
        for w in self.all_workflows:
            if week_key(w.created_at) == monday:
                by_date[w.created_at[:10]] += w.total_cost
        days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        return monday, [(d, by_date.get(d, 0.0)) for d in days]

    def trend_monthly_data(self) -> list[tuple[str, float]]:
        # The Monthly tab's chart: every month between the first and last with spend.
        by_month: dict[str, float] = defaultdict(float)
        for w in self.all_workflows:
            if week_key(w.created_at):  # skip undated rows so month_range never sees ""
                by_month[w.created_at[:7]] += w.total_cost
        if not by_month:
            return []
        keys = sorted(by_month)
        return [(m, by_month.get(m, 0.0)) for m in month_range(keys[0], keys[-1])]

    def trend_bar_data(self) -> list[tuple[str, float]]:
        # The active bar tab's (bucket key, cost) pairs; empty off the bar tabs.
        current = self.trend_tabs[self.trend_tab % len(self.trend_tabs)]
        if current == "Daily":
            return self.trend_daily_data()[1]
        if current == "Weekly":
            return self.trend_weekly_data()[1]
        if current == "Monthly":
            return self.trend_monthly_data()
        return []

    def _effective_bar_cursor(self, pairs: list[tuple[str, float]]) -> str | None:
        # The highlighted bar: the remembered cursor while it's still charted, else
        # the peak bucket (mirrors the Calendar's _effective_cursor).
        if not pairs:
            return None
        if self.trend_cursor in {k for k, _ in pairs}:
            return self.trend_cursor
        return max(pairs, key=lambda kv: kv[1])[0]

    def trend_bar_cursor(self) -> str | None:
        return self._effective_bar_cursor(self.trend_bar_data())

    def trend_model_rows(self) -> list[tuple[str, float]]:
        # The Models tab's ranked rows: (model, cost) with any priced spend, biggest
        # first (aggregate_models is already cost-sorted).
        agg = self.aggregate_models(self.all_workflows)
        return [(name, float(it["cost"])) for name, it in agg if float(it["cost"]) > 0]

    def trend_provider_rows(self) -> list[tuple[str, dict[str, float | int]]]:
        # Per-model spend rolled up to its provider (the "openai" in "openai/gpt-5"),
        # cost-sorted -- the Providers tab's rows.
        by_provider: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"cost": 0.0, "tokens": 0, "runs": 0}
        )
        for name, it in self.aggregate_models(self.all_workflows):
            item = by_provider[str(name).split("/", 1)[0] or "unknown"]
            item["cost"] = float(item["cost"]) + float(it["cost"])
            item["tokens"] = int(item["tokens"]) + int(it["tokens"])
            item["runs"] = int(item["runs"]) + int(it["runs"])
        return sorted(
            by_provider.items(),
            key=lambda kv: (float(kv[1]["cost"]), int(kv[1]["tokens"])),
            reverse=True,
        )

    @property
    def trend_tabs(self) -> tuple[str, ...]:
        # "Machines" rides on the base tabs only when the data spans machines (the fleet
        # view); otherwise it'd be a single all-"unknown" bar. Instance-only access, so a
        # property is safe (nothing reads App.trend_tabs at class level).
        base = self._TREND_TABS_BASE
        return base + ("Machines",) if self.machines_present else base

    @property
    def machines_present(self) -> bool:
        # Whether the loaded data spans two-or-more machines -- gates the Machine column
        # and the Machines tab. Deliberately NOT `combined`: the ordinary --source all
        # merge is combined but single-machine (every w.machine == ""), and a lone
        # machine's column/tab would be a 100% no-op (the src_col reasoning). Stops at
        # the second distinct machine, so it's cheap in the fleet view.
        seen: set[str] = set()
        for w in self.loaded:
            if w.machine:
                seen.add(w.machine)
                if len(seen) >= 2:
                    return True
        return False

    def machine_rows(self, workflows: list[Workflow]) -> list[tuple[str, dict[str, float | int]]]:
        # Spend grouped by the machine it ran on, cost-sorted -- the Machines tab's rows
        # and the per-scope Machines detail tables (the source_rows twin for the fleet).
        by_machine: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"cost": 0.0, "tokens": 0, "sessions": 0}
        )
        for w in workflows:
            item = by_machine[self.machine_of(w)]
            item["cost"] = float(item["cost"]) + w.total_cost
            item["tokens"] = int(item["tokens"]) + w.total_tokens
            item["sessions"] = int(item["sessions"]) + 1
        return sorted(
            by_machine.items(),
            key=lambda kv: (float(kv[1]["cost"]), int(kv[1]["tokens"])),
            reverse=True,
        )

    def _session_used_model(self, session_id: str, model: str) -> bool:
        # Whether a session's per-model breakdown includes this model -- the membership
        # test the Models-tab drill filters by (a session can use several models, so this
        # is a filter, not a partition like source/project/machine).
        return any(row["model_name"] == model for row in self.model_mix(session_id))

    def project_rows(self, workflows: list[Workflow]) -> list[tuple[str, dict[str, float | int]]]:
        # Spend grouped by the project it ran in -- the Trends "Projects" ranking, the
        # source_rows/machine_rows twin. Deliberately NOT built off projects_for_workflows,
        # which returns ProjectSummary: the ranked tabs all share one row shape
        # ((name, {cost, tokens, sessions})) so _group_table draws them, sort_trend_rows
        # orders them and trend_ranked_keys indexes them with no per-tab special case.
        # Worktrees fold to their git root (project_root), exactly as the sidebar groups
        # them, so one project is one row in both places; ignored projects are already
        # out of all_workflows, which is what every ranked tab is fed.
        by_project: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"cost": 0.0, "tokens": 0, "sessions": 0}
        )
        for w in workflows:
            item = by_project[self.project_root(w.directory) or "unknown"]
            item["cost"] = float(item["cost"]) + w.total_cost
            item["tokens"] = int(item["tokens"]) + w.total_tokens
            item["sessions"] = int(item["sessions"]) + 1
        return sorted(
            by_project.items(),
            key=lambda kv: (float(kv[1]["cost"]), int(kv[1]["tokens"])),
            reverse=True,
        )

    def source_rows(self, workflows: list[Workflow]) -> list[tuple[str, dict[str, float | int]]]:
        # Spend grouped by the tool it came from, cost-sorted -- the Sources tab's
        # rows and the per-scope Sources detail tables.
        by_source: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"cost": 0.0, "tokens": 0, "sessions": 0}
        )
        for w in workflows:
            item = by_source[w.source or "unknown"]
            item["cost"] = float(item["cost"]) + w.total_cost
            item["tokens"] = int(item["tokens"]) + w.total_tokens
            item["sessions"] = int(item["sessions"]) + 1
        return sorted(
            by_source.items(),
            key=lambda kv: (float(kv[1]["cost"]), int(kv[1]["tokens"])),
            reverse=True,
        )

    def active_trend_tab(self) -> str:
        return self.trend_tabs[self.trend_tab % len(self.trend_tabs)]

    def trend_ranked_rows(self, tab: str | None = None) -> list[tuple]:
        # One ranked tab's rows, in DISPLAY order -- the single source both the
        # renderer and trend_ranked_keys read, so the cursor's ordinal can never index
        # a different ranking than the one on screen (the models-picker rule). `tab`
        # names the table for callers that draw one without the overlay being open
        # (the suite does); None means the active tab.
        tab = tab or self.active_trend_tab()
        if tab == "Models":
            rows: list[tuple] = self.trend_model_rows()
        elif tab == "Providers":
            rows = self.trend_provider_rows()
        elif tab == "Projects":
            rows = self.project_rows(self.all_workflows)
        elif tab == "Harnesses":
            rows = self.source_rows(self.all_workflows)
        elif tab == "Machines":
            rows = self.machine_rows(self.all_workflows)
        else:
            return []
        return self.sort_trend_rows(rows, tab)

    def trend_ranked_keys(self) -> list[str]:
        # The active ranked tab's row keys, in display order; empty off those tabs.
        return [name for name, _row in self.trend_ranked_rows()]

    def trend_sort_options(self, tab: str | None = None) -> tuple[str, ...]:
        # The columns this ranked tab can be ordered by; empty off the ranked tabs.
        return self._TREND_SORT_COLUMNS.get(tab or self.active_trend_tab(), ())

    def trend_sort_labels(self, tab: str | None = None) -> dict[str, str]:
        # What this tab calls the two shared keys ("name"/"count"), for the `s` picker.
        return self._TREND_SORT_LABELS.get(tab or self.active_trend_tab(), {})

    def trend_sort_key(self, tab: str | None = None) -> str:
        # The stored column, validated against the tab that is about to draw: a key the
        # tab doesn't offer (Models has no Tokens column) falls back to cost, the one
        # column every ranked tab shares. The preference itself is kept, so tabbing
        # back to a table that HAS the column restores it.
        options = self.trend_sort_options(tab)
        return self.trend_sort if self.trend_sort in options else "cost"

    def trend_sort_reverse_for(self, tab: str | None = None) -> bool:
        # The direction belongs to the STORED column, so a tab that withdrew that column
        # must not inherit its flip: flipping Tokens on Providers and tabbing to Models
        # (which has no Tokens column, so it falls back to cost) would otherwise rank the
        # money cheapest-first -- an inversion the user asked for on a different column,
        # on a tab they never touched. The session lists' session_sort_reverse() rule.
        return self.trend_sort_reverse if self.trend_sort in self.trend_sort_options(tab) else False

    @staticmethod
    def _trend_sort_value(key: str, name: str, item) -> float | str:
        # One ranked row's value for a sort column. The Models tab's rows are
        # (model, cost) pairs and the other three (name, {cost, tokens, runs|sessions})
        # dicts, so this is where the two shapes meet -- the count column is "runs" on
        # the model-derived tabs and "sessions" on the session-derived ones.
        if key == "name":
            return str(name).lower()
        if not isinstance(item, dict):  # the Models tab's bare cost
            return float(item) if key == "cost" else 0.0
        if key == "tokens":
            return float(item.get("tokens", 0))
        if key == "count":
            return float(item.get("runs", item.get("sessions", 0)))
        return float(item.get("cost", 0.0))

    def sort_trend_rows(self, rows: list[tuple], tab: str | None = None) -> list[tuple]:
        # Order a ranked tab's rows by the active column. Cost stays the tiebreaker
        # under every other column (a two-pass stable sort, so it does NOT invert with
        # the primary): rows tied on tokens or session count keep the spend ranking the
        # tab is otherwise read by, instead of falling into dict order.
        key = self.trend_sort_key(tab)
        desc = self.sort_descending(key, self.trend_sort_reverse_for(tab))
        if key == "name":  # keys are unique, so there is nothing to tie-break
            return sorted(rows, key=lambda kv: self._trend_sort_value("name", *kv), reverse=desc)
        ranked = sorted(rows, key=lambda kv: self._trend_sort_value("cost", *kv), reverse=True)
        return sorted(ranked, key=lambda kv: self._trend_sort_value(key, *kv), reverse=desc)

    def trend_drill_sessions(self) -> list[tuple[Workflow, float, int]]:
        # Sessions behind a Trends ranked row: every root session in the active
        # range that used the model / provider / source, with its cost/tokens within
        # it, most spend first. Range-scoped, unlike the app-wide P overlay drill;
        # models match the row's exact spelling (the ranked rows aren't
        # canonically deduped, so each alias row owns its own sessions).
        if self.trend_drill is None:
            return []
        kind, key = self.trend_drill
        if kind in ("source", "machine", "project"):
            # Each of these rows names a whole-session property, so the drill is one
            # equality test -- but it must be the SAME rule the ranking grouped by, or
            # the drill silently opens an empty list for a row the tab just showed. The
            # machine key comes from machine_rows, which labels an untagged session with
            # THIS box; the project key is the git root, not the raw cwd of a worktree.
            field = {
                "machine": self.machine_of,
                "project": lambda w: self.project_root(w.directory) or "unknown",
                "source": lambda w: w.source or "unknown",
            }[kind]
            rows = [
                (w, w.total_cost, w.total_tokens) for w in self.all_workflows if field(w) == key
            ]
            rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
            return rows
        out = []
        for w in self.all_workflows:
            cost, tokens = 0.0, 0
            for m in self.model_mix(w.id):
                name = str(m.get("model_name"))
                if (kind == "model" and name == key) or (
                    kind == "provider" and name.split("/", 1)[0] == key
                ):
                    cost += float(m.get("cost", 0) or 0)
                    tokens += int(m.get("tokens_total", 0) or 0)
            if cost or tokens:
                out.append((w, cost, tokens))
        out.sort(key=lambda r: (r[1], r[2]), reverse=True)
        return out

    def drill_into_date(self, date: str) -> bool:
        # Jump from the Calendar heat map straight into a day's detail: point the
        # time-browse panels at <date> and zoom in. Returns False (no jump) when that
        # day has no sessions (an empty cell), so the caller can nudge instead.
        if not any(w.created_at[:10] == date for w in self.all_workflows):
            return False
        years = self.years
        yi = next((i for i, y in enumerate(years) if y.year == date[:4]), None)
        if yi is None:
            return False
        if self.browse_mode != "time":  # leaving Projects/Machines -- remember its spot
            self._remember_mode_position()
        self.view = "browse"  # the overlay may sit over a zoom; land back in browse first
        self.browse_mode = "time"
        self.focus = "days"
        self.year_index = yi
        self.tab = 0
        self.scroll = 0
        self.zoom_project = None
        # Resolve the month/day indices against the now-scoped panels, then drill.
        self.month_index = next((i for i, m in enumerate(self.months) if m.month == date[:7]), 0)
        self.day_index = next((i for i, d in enumerate(self.panel_days) if d.day == date), 0)
        self.drill_in()
        return True

    def drill_into_month(self, month: str) -> bool:
        # Jump from the Monthly bar chart straight into a month's detail: point the
        # time-browse panels at <month> and zoom in. Returns False (no jump) when the
        # month has no sessions (an empty bar), so the caller can nudge instead.
        if not any(w.created_at[:7] == month for w in self.all_workflows):
            return False
        yi = next((i for i, y in enumerate(self.years) if y.year == month[:4]), None)
        if yi is None:
            return False
        if self.browse_mode != "time":  # leaving Projects/Machines -- remember its spot
            self._remember_mode_position()
        self.view = "browse"  # the overlay may sit over a zoom; land back in browse first
        self.browse_mode = "time"
        self.focus = "months"
        self.year_index = yi
        self.tab = 0
        self.scroll = 0
        self.zoom_project = None
        self.month_index = next((i for i, m in enumerate(self.months) if m.month == month), 0)
        self.day_index = 0
        self.drill_in()
        return True

    def drill_into_session(self, workflow_id: str, tab: str | None = None) -> bool:
        # Jump from a Trends sessions list straight into that session's detail: zoom
        # its day, land on the Sessions tab with it selected, and drill in. An
        # optional tab name lands on that session sub-tab instead of Overview.
        w = next((x for x in self.all_workflows if x.id == workflow_id), None)
        if w is None or not self.drill_into_date(w.created_at[:10]):
            return False
        tabs = self.current_tabs()
        if "Sessions" in tabs:
            self.tab = tabs.index("Sessions")
        rows = self.current_sessions()
        i = next((j for j, x in enumerate(rows) if x.id == workflow_id), None)
        if i is not None:
            self.workflow_index = i
            self.drill_in()  # the day's Sessions tab -> the session view
            if tab and self.view == "session":
                self.select_session_tab(tab)
        return True

    def select_session_tab(self, name: str) -> bool:
        # Land on a named session sub-tab (case-insensitive) -- the --goto/--tab
        # target. The tab set is backend-dependent (Codex has no Context curve, a
        # non-OpenCode session no Tools), so a miss keeps Overview and says why:
        # the tmux popup this was made for must never error out over a bad name.
        if self.view != "session":
            return False
        tabs = self.current_tabs()
        want = name.strip().lower()
        match = next((i for i, t in enumerate(tabs) if t.lower() == want), None)
        if match is None:
            named = ", ".join(t.lower() for t in tabs)
            self.notice = f"no '{name}' tab here -- this session has: {named}"
            return False
        self.tab, self.scroll = match, 0
        return True

    def goto_session(self, workflow_id: str, tab: str | None = None) -> bool:
        # The --goto startup jump: land straight in a session's detail view (on the
        # named tab when --tab asked for one). State-only (no curses), so cli.main
        # can call it before curses.wrapper. A restored range can hide the target;
        # when it does, clear the range and retry so goto always lands. An ignored
        # project stays ignored -- that's an explicit user choice, so just say why
        # the jump didn't happen.
        if self.drill_into_session(workflow_id, tab):
            return True
        if any(w.id == workflow_id for w in self.loaded):
            self.range_days = self.range_months = None
            self.custom_since = self.custom_until = None
            self._invalidate_workflow_cache()
            if self.drill_into_session(workflow_id, tab):
                # Keep select_session_tab's "no 'context' tab here" explanation if the
                # requested tab was missing -- only announce the range clear otherwise.
                tabs = self.current_tabs()
                want = (tab or "").strip().lower()  # match select_session_tab's normalization
                on_tab = bool(tabs) and tabs[self.tab % len(tabs)].lower() == want
                if not tab or on_tab:
                    self.notice = "range cleared to reach the session"
                return True
            self.notice = "session is in an ignored project"
            return False
        self.notice = "session not found in this source"
        return False

    def _calendar_key(self, act: str) -> bool:
        # The focused Calendar grid: the cursor actions walk the day cursor (←/→ =
        # ∓1 week, ↑/↓ = ∓1 day, clamped to the shown year); select drills into the
        # highlighted day. `act` is a trends.chart action, resolved by the caller.
        cursor = self.calendar_cursor()
        if cursor is None:
            return True
        if act == "select":
            if self.drill_into_date(cursor):
                self._trend_return = ("Calendar", cursor)  # Esc out of the day returns here
                self.trends = False
            else:
                self.notify(f"no sessions on {cursor}", "error")
            return True
        delta = {"cursor_left": -7, "cursor_right": 7, "cursor_up": -1, "cursor_down": 1}
        nxt = datetime.strptime(cursor, "%Y-%m-%d") + timedelta(days=delta[act])
        if nxt.strftime("%Y") == cursor[:4]:  # stay inside the shown calendar year
            self.cal_cursor = nxt.strftime("%Y-%m-%d")
        return True

    def _calendar_date_at(self, my: int, mx: int) -> str | None:
        # Resolve a mouse (y, x) to the calendar day under it, or None (a gap, padding,
        # or outside the grid). Reads the geometry the last draw_calendar() stashed.
        geom = self._cal_geom
        if geom is None:
            return None
        gy0, row_pitch, gx, pitch, start_col, shown, year, grid_start = geom
        if my < gy0 or mx < gx:
            return None
        row, col = (my - gy0) // row_pitch, (mx - gx) // pitch
        if row > 6 or col >= shown:
            return None
        date = (grid_start + timedelta(days=(start_col + col) * 7 + row)).strftime("%Y-%m-%d")
        return date if date[:4] == year else None

    def _trend_bar_key(self, act: str, current: str) -> bool:
        # The cursor actions walk the bar cursor on a focused Daily/Weekly/Monthly
        # chart (↑/↓ hop a week on Daily, one bar elsewhere); select drills into the
        # highlighted day / month, clamped to the charted buckets.
        pairs = self.trend_bar_data()
        cursor = self._effective_bar_cursor(pairs)
        if cursor is None:
            return True
        if act == "select":
            self._trend_bar_open(current, cursor)
            return True
        vstep = 7 if current == "Daily" else 1
        delta = {
            "cursor_left": -1,
            "cursor_right": 1,
            "cursor_up": -vstep,
            "cursor_down": vstep,
        }[act]
        keys = [k for k, _ in pairs]
        i = keys.index(cursor)
        self.trend_cursor = keys[max(0, min(i + delta, len(keys) - 1))]
        return True

    def _trend_bar_open(self, current: str, cursor: str) -> None:
        # Drill from a bar into its scope; an empty bucket nudges instead (like the
        # Calendar). On success, Esc out of the scope returns to this chart.
        if current == "Monthly":
            opened, noun = self.drill_into_month(cursor), "spend in"
        else:
            opened, noun = self.drill_into_date(cursor), "sessions on"
        if opened:
            self._trend_return = (current, cursor)
            self.trends = False
        else:
            self.notify(f"no {noun} {cursor}", "error")

    def _open_trend_drill(self) -> None:
        # Enter on a ranked row (Models/Providers/Sources): open its sessions list.
        current = self.trend_tabs[self.trend_tab % len(self.trend_tabs)]
        keys = self.trend_ranked_keys()
        if not keys:
            return
        kind = {
            "Models": "model",
            "Providers": "provider",
            "Projects": "project",
            "Harnesses": "source",
            "Machines": "machine",
        }[current]
        self.trend_drill = (kind, keys[max(0, min(self.trend_row_index, len(keys) - 1))])
        self.trend_drill_index = 0

    def _trend_drill_key(self, key: int | str, stdscr: curses.window | None = None) -> bool:
        # A ranked row's sessions list: the scroll keys move the cursor, select opens
        # the selected session itself, back steps out to the ranked tab; any other
        # key is swallowed.
        n = len(self.trend_drill_sessions())
        act = self.keymap.action("trends.drill", key)
        if act == "down":
            self.trend_drill_index = min(self.trend_drill_index + 1, max(0, n - 1))
        elif act == "up":
            self.trend_drill_index = max(0, self.trend_drill_index - 1)
        elif act == "page_down":
            self.trend_drill_index = min(
                self.trend_drill_index + self._page_step(stdscr), max(0, n - 1)
            )
        elif act == "page_up":
            self.trend_drill_index = max(0, self.trend_drill_index - self._page_step(stdscr))
        elif act == "top":
            self.trend_drill_index = 0
        elif act == "bottom":
            self.trend_drill_index = max(0, n - 1)
        elif act == "select":
            self._open_trend_drill_session()
        elif act in ("tab_prev", "tab_next"):
            # Tab switching works even from inside a drill list -- leave the drill
            # and move, instead of falling into "any other key closes the overlay"
            # (which threw you back to the main view). back stays "back to the rows".
            self.trend_drill = None
            self.trend_focus = False
            self.trend_row_index = 0
            step = -1 if act == "tab_prev" else 1
            self.trend_tab = (self.trend_tab + step) % len(self.trend_tabs)
        elif act == "back":
            self.trend_drill = None  # back to the ranked rows
        elif act == "api_prices":
            self.toggle_api_prices()  # re-prices the list in place, stays open
        else:
            handled = self._trend_common_key(key, "trends.drill")
            if handled is not None:
                return handled
            # Any other key is swallowed -- it must not tear down the list.
        return True

    def _trend_common_key(self, key: int | str, context: str = "trends") -> bool | None:
        # Overlay-wide actions that work anywhere inside Trends (tabs, focused charts
        # excepted -- they see the cursor keys first -- and drill lists): close shuts
        # the overlay, help and prices float their overlay above it (closing that one
        # lands back on Trends), theme floats its picker over the charts, demo_toggle
        # swaps real/demo data under them in place, Ctrl-C still quits. None = not one
        # of these. `context` is the sub-state that asked, so its own overrides win.
        act = self.keymap.action(context, key)
        if act == "close":
            self.trends = False
            self.trend_drill = None
            return True
        if act == "help":
            self.help = True
            self.help_scroll = 0
            return True
        if act == "prices":
            self.show_prices = True
            self.prices_scroll = 0
            self.prices_index = 0
            self.prices_model = None
            return True
        if act == "theme":
            self.open_theme_menu()  # live-previews with the charts as the swatch
            return True
        if act == "harness":
            self.open_harness_picker()  # narrowing/switching re-scopes the charts in place
            return True
        if act == "machine" and self.machines_present:
            self.open_machine_menu()  # narrowing to one box re-scopes the charts in place
            return True
        if act == "demo_toggle":
            self.toggle_demo()  # anonymize before a screenshot without leaving
            return True
        if key == 3:  # Ctrl-C
            return False
        return None

    def _open_trend_drill_session(self) -> None:
        # Enter on a session in a Trends drill list: jump into that session's
        # detail. Esc-ing back out of it returns to this list, cursor kept.
        rows = self.trend_drill_sessions()
        if not rows or self.trend_drill is None:
            return
        kind, key = self.trend_drill
        idx = max(0, min(self.trend_drill_index, len(rows) - 1))
        if self.drill_into_session(rows[idx][0].id):
            self._trend_return = ("drill", kind, key, idx)
            self.trends = False
            self.trend_drill = None

    def handle_key(self, stdscr: curses.window, key: int | str) -> bool:
        if key == curses.KEY_MOUSE:
            return self.handle_mouse()
        if key == curses.KEY_RESIZE:
            # A SIGWINCH (terminal/font resize) surfaces as a keystroke; it is not one.
            # The next paint reads getmaxyx() fresh, so just swallow it -- otherwise it
            # falls through to an overlay's "any other key closes" path and shuts it.
            return True
        # A non-ASCII character arrives as a str (_read_key). It flows through the same
        # routing as any key: the keymap can bind one ("ö = quit" works), the text
        # fields type it, and every handler resolves through the keymap rather than
        # arithmetic on the code, so a str never hits an int comparison.
        if self.startup_warning is not None:
            return self.handle_startup_warning_key(key)
        if self.price_prompt:
            return self.handle_price_prompt_key(key)
        # The C (Colours), H (source) and M (machine filter) pickers float above
        # everything -- they can be opened from inside Trends / P / help now, so they must
        # see keys before the overlays do (draw() already paints these small modals on top).
        if self.theme_menu:
            return self.handle_theme_menu_key(key)
        if self.demo_menu:
            return self.handle_demo_menu_key(key)
        if self.source_menu:
            return self.handle_source_menu_key(key)
        if self.machine_menu:
            return self.handle_machine_menu_key(key)
        if self.harness_menu:
            return self.handle_harness_menu_key(key)
        if self.whatif_menu:  # the `w` target picker floats above everything too
            return self.handle_whatif_menu_key(key)
        if self.help:
            # A pager like the price overlay: scroll keys page it. Closing is explicit
            # and every other key is swallowed -- it lists the keys that work here, so
            # it is read *while deciding what to press*, and a mistyped one must not
            # tear it down (the Trends/P convention).
            if key == 3:  # Ctrl-C still quits
                return False
            act = self.keymap.action("help", key)
            if act == "down":
                self.help_scroll += 1
            elif act == "up":
                self.help_scroll = max(0, self.help_scroll - 1)
            elif act == "page_down":
                self.help_scroll += self._page_step(stdscr)  # clamped on draw
            elif act == "page_up":
                self.help_scroll = max(0, self.help_scroll - self._page_step(stdscr))
            elif act == "top":
                self.help_scroll = 0
            elif act == "bottom":
                self.help_scroll = 10_000  # clamped to the last page on draw
            elif act == "theme":
                self.open_theme_menu()  # the Colours picker floats above help too
            elif act == "harness" and (self.can_switch_source() or self.machines_present):
                self.open_harness_picker()  # ...and so does the harness key: the key list
                # names both, and a list that names a key it then eats is the very thing
                # this table exists to prevent.
            elif act == "machine" and self.machines_present:
                self.open_machine_menu()  # the machine filter floats above help too
            elif act == "edit_keymap":
                self.edit_keymap(stdscr)  # change the very keys the list is showing
            elif act == "close":
                self.help = False
            return True
        if self.toast_history:
            # A pager like help/prices: the scroll keys page the notices scrollback;
            # closing is explicit, Ctrl-C still quits, and every other key is swallowed
            # so a mistype can't tear it down.
            if key == 3:  # Ctrl-C still quits
                return False
            act = self.keymap.action("notices", key)
            if act == "down":
                self.toast_history_scroll += 1
            elif act == "up":
                self.toast_history_scroll = max(0, self.toast_history_scroll - 1)
            elif act == "page_down":
                self.toast_history_scroll += self._page_step(stdscr)  # clamped on draw
            elif act == "page_up":
                self.toast_history_scroll = max(
                    0, self.toast_history_scroll - self._page_step(stdscr)
                )
            elif act == "top":
                self.toast_history_scroll = 0
            elif act == "bottom":
                self.toast_history_scroll = 10_000  # clamped to the last page on draw
            elif act == "close":
                self.toast_history = False
            return True
        if self.show_prices:
            if self.sort_menu:  # the `s` picker floats over the price table
                return self.handle_sort_menu_key(key)
            if self.filter_active:
                return self.handle_filter_key(key)
            if self.prices_model is not None:
                return self._handle_price_sessions_key(key, stdscr)
            return self._handle_price_models_key(key, stdscr)
        if self.trends:
            if self.sort_menu:  # the `s` picker floats over the ranked tables
                return self.handle_sort_menu_key(key)
            current = self.trend_tabs[self.trend_tab % len(self.trend_tabs)]
            if self.trend_drill is not None:
                return self._trend_drill_key(key, stdscr)
            chart_tab = current in ("Daily", "Weekly", "Monthly", "Calendar")
            focused = chart_tab and self.trend_focus
            # The charts are modal so the arrows aren't trapped: until you focus one
            # (select), arrows move between tabs like everywhere else. Focused, the
            # trends.chart context takes over: cursor keys walk the bars / days,
            # select drills, back unfocuses -- and anything it doesn't bind falls
            # through to the plain trends keys below (j/k still page a focused chart).
            act = self.keymap.action("trends.chart" if focused else "trends", key)
            if focused:
                if act == "back":  # leave focus, back to tab navigation
                    self.trend_focus = False
                    return True
                if act in ("cursor_left", "cursor_right", "cursor_up", "cursor_down", "select"):
                    if current == "Calendar":
                        return self._calendar_key(act)
                    return self._trend_bar_key(act, current)
            elif chart_tab and act == "select":
                self.trend_focus = True  # focus the chart; the cursor keys now pick
                return True
            if current == "Calendar" and act in ("shades_more", "shades_less"):
                self.cal_levels = (
                    min(HEAT_MAX_LEVELS, self.cal_levels + 1)
                    if act == "shades_more"
                    else max(HEAT_MIN_LEVELS, self.cal_levels - 1)
                )
                return True
            if not chart_tab:
                # Ranked tabs (Models/Providers/Sources): down/up move the row cursor
                # directly (no pager competes for them), select opens its sessions.
                if act == "select":
                    self._open_trend_drill()
                    return True
                if act == "sort":
                    # Only here: on a chart tab `s` falls through and is swallowed --
                    # a time axis has no column to order by, and the sort keys the
                    # picker would offer belong to a table that isn't on screen.
                    self.open_sort_menu()
                    return True
                if act in ("down", "up"):
                    n = len(self.trend_ranked_keys())
                    step = 1 if act == "down" else -1
                    self.trend_row_index = max(0, min(self.trend_row_index + step, n - 1))
                    return True
            if act == "tab_prev":
                self.trend_tab = (self.trend_tab - 1) % len(self.trend_tabs)
                self.trend_focus = False  # switching tabs leaves any focused canvas
                self.trend_row_index = 0
            elif act == "tab_next":
                self.trend_tab = (self.trend_tab + 1) % len(self.trend_tabs)
                self.trend_focus = False
                self.trend_row_index = 0
            elif act in ("down", "older", "up", "newer"):
                # Page the Daily tab's month / the Weekly tab's week / the Calendar's year.
                older = act in ("down", "older")
                if current == "Daily":
                    n = len(self.trend_months())
                    self.trend_month_index = self._step_trend_index(
                        self.trend_month_index, n, older
                    )
                    self.trend_cursor = None  # re-anchor the cursor on the new month's peak
                elif current == "Weekly":
                    n = len(self.trend_weeks())
                    self.trend_week_index = self._step_trend_index(self.trend_week_index, n, older)
                    self.trend_cursor = None
                elif current == "Calendar":
                    n = len(self.calendar_years())
                    self.trend_year_index = self._step_trend_index(self.trend_year_index, n, older)
                    self.cal_cursor = None  # re-anchor the cursor on the new year's peak
            elif act == "api_prices":
                self.toggle_api_prices()  # re-prices the charts in place, stays open
            elif act == "back":
                self.trends = False  # back (with nothing focused) closes the overlay
            else:
                handled = self._trend_common_key(key)
                if handled is not None:
                    return handled
                # Any other key is swallowed: Trends is interactive, so a mistyped
                # key must not tear the overlay down. Closing is explicit.
            return True

        if self.sort_menu:
            return self.handle_sort_menu_key(key)
        if self.filter_active:
            return self.handle_filter_key(key)
        if self.launch_menu is not None:
            return self.handle_launch_key(key)
        act = self.keymap.action("main", key)
        if key == 3 or act == "quit":
            return False
        if act == "help":
            self.help = True
            self.help_scroll = 0
            return True
        if act == "notices":
            self.open_notices()  # the notices scrollback: read a toast that faded
            return True
        if act == "trends":
            self.trends = True
            self.trend_month_index = 0  # start at the most recent month
            self.trend_week_index = 0  # and the most recent week
            self.trend_year_index = 0  # and the most recent year
            self.cal_cursor = None  # Calendar cursor defaults to that year's peak day
            self.trend_cursor = None  # bar cursors default to each chart's peak
            self.trend_row_index = 0
            self.trend_drill = None
            self.trend_focus = False  # land unfocused (arrows pick tabs)
            return True
        if act == "prices":
            self.show_prices = True
            self.prices_scroll = 0
            self.prices_index = 0
            self.prices_model = None
            return True
        if act == "reload":
            self.reload()
            return True
        if act == "harness":
            self.open_harness_picker()  # fleet: filter harness (keep boxes); else swap store
            return True
        if act == "machine":
            self.open_machine_menu()  # narrow every view to one box (fleet); no-op off one
            return True
        if act == "theme":
            self.open_theme_menu()
            return True
        if act == "demo":
            self.demo_action()  # off in one press when lit; else pick what to anonymize
            return True
        if act == "edit_keymap":
            self.edit_keymap(stdscr)  # $EDITOR on keymap.conf, reloaded on return
            return True
        if act == "maximize":
            # In browse, + drills in like Enter (its old alias); once the detail is
            # the active pane it becomes lazygit's screen-mode key: toggle between
            # the split and a full-screen detail.
            if self.view == "zoom":
                self.toggle_zoom_maximized()
            else:
                self.drill_in()
            return True
        if act == "all_time":
            self.set_all_time()
            return True
        if act == "range":
            self.prompt_range(stdscr)
            return True
        if act == "mode_projects":
            self.set_browse_mode("projects")
            return True
        if act == "mode_time":
            self.set_browse_mode("time")
            return True
        if act == "mode_machines":
            # Always available: with no fleet it shows the one box you're on (see
            # mode_tab_list). The fleet-only extras (the M filter, the Machine column,
            # F re-pull) stay gated on machines_present -- they need a second box to
            # compare against, this view doesn't.
            self.set_browse_mode("machines")
            return True
        if act == "refresh_machines":
            # Fetch: re-pull the selected box (Machines mode) or every pulled box.
            if self.can_refresh_machines():
                self.request_machine_refresh(self.refresh_target())
            elif self.machines_present:
                self.notify("refresh disabled in demo", "error")
            else:
                self.notify("refresh needs a fleet (--pull / --remote)", "error")
            return True
        if act == "sort":
            self.open_sort_menu()
            return True
        if act == "ignore":
            self.toggle_ignore()
            return True
        if act == "show_ignored":
            self.toggle_ignored_projects_view()
            return True
        if act == "bookmark":
            self.toggle_bookmark()
            return True
        if act == "show_bookmarks":
            self.toggle_bookmarks_view()
            return True
        if act == "note":
            self.edit_note(stdscr)
            return True
        if act == "filter":
            if not self.can_filter_current_view():
                self.notify(
                    "nothing to filter here — open a sessions, projects, or models list", "error"
                )
                return True
            self.filter_active = True
            self._filter_before = self.query
            return True
        if act == "clear_filter":
            if self.query:
                self.query = ""
                self.workflow_index = 0
                self.project_index = 0
                self.notice = "filter cleared"
            else:
                self.notify("no active filter", "error")
            return True
        if act == "export":
            self.export_current()
            return True
        if act == "open_dir":
            self.open_current()
            return True
        if act == "launch":
            self.launch_current()
            return True
        if act == "api_prices":
            self.toggle_api_prices()
            return True
        if act == "whatif":
            self.toggle_whatif()  # pick a target model, or clear the active one
            return True
        if act == "cycle_panel":
            self.cycle_focus(1)
            return True
        if act in ("panel_1", "panel_2", "panel_3"):
            # Jump to a sidebar panel — its number is in its title.
            self.focus_panel(self.FOCUS_CYCLE[int(act[-1]) - 1])
            return True
        if act == "panel_detail":
            self.focus_detail()  # the pane on the right
            return True
        if act == "select":
            # On the Turns tab select folds/unfolds the selected ▸ group; everywhere
            # else it drills in (and it still does here when there's no group to toggle).
            if self._on_turns_tab() and self._toggle_turn_cursor():
                return True
            self.drill_in()
            return True
        if act in ("trace_prev", "trace_next"):
            self.step_trace(-1 if act == "trace_prev" else 1)
            return True
        if act == "trace_expand":
            self.toggle_trace_expansion()
            return True
        if act == "back":
            # A drilled prompt is the innermost scope on the Turns tab, so Esc leaves it
            # before it starts popping the view stack -- but ONLY while that tab is the
            # one on screen. Left ungated, Esc on Tools or Context silently tore down an
            # invisible drill and was swallowed, so the key appeared to do nothing.
            if self._on_turns_tab() and (self.close_trace_drill() or self.close_turn_drill()):
                return True
            self.drill_out()  # session -> zoom -> browse; no-op when browsing
            return True
        if act == "cycle_panel_back":
            if self.view == "browse":
                self.cycle_focus(-1)
            else:
                self.drill_out()
            return True
        if act == "tab_prev":
            self._clear_trace_expansion()
            self.tab = (self.tab - 1) % len(self.current_tabs())
            self.scroll = 0
            return True
        if act == "tab_next":
            self._clear_trace_expansion()
            self.tab = (self.tab + 1) % len(self.current_tabs())
            self.scroll = 0
            return True
        if act == "top":
            self.jump(to_end=False, stdscr=stdscr)
            return True
        if act == "bottom":
            self.jump(to_end=True, stdscr=stdscr)
            return True
        if act == "down":
            self.move(1)
            return True
        if act == "up":
            self.move(-1)
            return True
        if act == "page_down":
            self.move(self._page_step(stdscr))
            return True
        if act == "page_up":
            self.move(-self._page_step(stdscr))
            return True
        return True

    def prices_view_label(self, view: str | None = None) -> str:
        # The human label for a P-overlay view mode (defaults to the active one).
        view = view or self.prices_view
        return dict(self.prices_views).get(view, view)

    def set_prices_view(self, view: str) -> None:
        # Switch the P overlay's view (a tab click, h/l, or the p cycle). No toast:
        # the tab bar itself shows the active view.
        if view == self.prices_view:
            return
        self.prices_view = view
        self.prices_index = 0  # the row order (and count) changed under the cursor
        self.prices_scroll = 0

    def cycle_prices_view(self, step: int = 1) -> None:
        # `p` (and h/l) walk the P overlay's views (flat -> by vendor -> by provider
        # -> models.dev).
        keys = [k for k, _label in self.prices_views]
        i = keys.index(self.prices_view) if self.prices_view in keys else 0
        self.set_prices_view(keys[(i + step) % len(keys)])

    @staticmethod
    def _pin_keys(canon: str, routes: tuple) -> set[str]:
        # The pin identities one row covers: "route/canon" per route the row spans
        # (a route-less row pins the bare canon). Canon keeps pins spelling- and
        # date-pin-independent; the route scope keeps them row-scoped.
        return {f"{r}/{canon}" for r in routes} if routes else {canon}

    def _is_pinned(self, canon: str, routes: tuple) -> bool:
        return any(k in self.pinned_models for k in self._pin_keys(canon, routes))

    def toggle_price_pin(self) -> None:
        # Space pins/unpins the selected ROW: exactly that (route, model) in the
        # route-scoped views, or the routes an aggregated flat/vendor row covers --
        # never every reseller of the same model name. The cursor follows the row
        # to its new position (into or out of the pinned block).
        entries = self.priced_model_entries()
        if not entries:
            return
        entry = entries[max(0, min(self.prices_index, len(entries) - 1))]
        keys = self._pin_keys(entry.canon, entry.routes)
        if keys <= self.pinned_models:
            self.pinned_models -= keys
        else:
            self.pinned_models |= keys
        for i, e in enumerate(self.priced_model_entries()):
            if e.canon == entry.canon and e.routes == entry.routes:
                self.prices_index = i
                break

    def _handle_price_models_key(self, key: int | str, stdscr: curses.window | None = None) -> bool:
        # The P overlay's model list: the scroll keys move a cursor, select drills
        # into the selected model's sessions, sort sorts by a column, cycle_view walks
        # the layout (by vendor / by provider / flat), filter filters, refresh
        # refreshes, export exports the table. Closing is explicit, like the Trends
        # overlay -- a mistyped key must not tear the table down.
        n = len(self.priced_model_names())
        act = self.keymap.action("prices", key)
        if act == "sort":
            self.open_sort_menu()
            return True
        if act in ("cycle_view", "tab_next"):
            self.cycle_prices_view()  # the tabs read left-to-right, like Trends
            return True
        if act == "tab_prev":
            self.cycle_prices_view(-1)
            return True
        if act == "pin":
            self.toggle_price_pin()
            return True
        if act == "down":
            self.prices_index = min(self.prices_index + 1, max(0, n - 1))
        elif act == "up":
            self.prices_index = max(0, self.prices_index - 1)
        elif act == "page_down":
            self.prices_index = min(self.prices_index + self._page_step(stdscr), max(0, n - 1))
        elif act == "page_up":
            self.prices_index = max(0, self.prices_index - self._page_step(stdscr))
        elif act == "top":
            self.prices_index = 0
        elif act == "bottom":
            self.prices_index = max(0, n - 1)
        elif act == "select":
            names = self.priced_model_names()
            if names:
                self.prices_model = names[max(0, min(self.prices_index, len(names) - 1))]
                self.prices_scroll = 0
        elif act == "refresh":
            self.refresh_prices_action()  # keeps the overlay open
        elif act == "filter":
            self.filter_active = True
            self._filter_before = self.query
            self.prices_scroll = 0
        elif act == "export":
            self.export_current()  # _export_dataset sees show_prices; overlay stays open
        elif act == "back":
            self.show_prices = False  # closes the overlay from the model list
        else:
            handled = self._prices_common_key(key)
            if handled is not None:
                return handled
            # Any other key is swallowed -- closing is explicit.
        return True

    def _prices_common_key(self, key: int | str, context: str = "prices") -> bool | None:
        # Overlay-wide actions that work anywhere inside the P overlay (model list and
        # the per-model drill): back stays contextual at the call sites; close shuts
        # the overlay, help floats above it (closing that lands back here), theme its
        # picker over the table, api_prices re-prices the app behind it in place,
        # demo_toggle swaps real/demo data under it, Ctrl-C still quits.
        act = self.keymap.action(context, key)
        if act == "close":
            self.show_prices = False
            self.prices_model = None
            return True
        if act == "help":
            self.help = True
            self.help_scroll = 0
            return True
        if act == "theme":
            self.open_theme_menu()
            return True
        if act == "harness":
            self.open_harness_picker()
            return True
        if act == "machine" and self.machines_present:
            self.open_machine_menu()
            return True
        if act == "demo_toggle":
            self.toggle_demo()
            return True
        if act == "api_prices":
            self.toggle_api_prices()
            return True
        if key == 3:  # Ctrl-C
            return False
        return None

    def _handle_price_sessions_key(
        self, key: int | str, stdscr: curses.window | None = None
    ) -> bool:
        # The P overlay's per-model drill-in: the scroll keys page the session list;
        # back steps out to the model list; close shuts the overlay; any other key is
        # swallowed (closing is explicit, like Trends).
        act = self.keymap.action("prices.sessions", key)
        if act == "down":
            self.prices_scroll += 1
        elif act == "up":
            self.prices_scroll = max(0, self.prices_scroll - 1)
        elif act == "page_down":
            self.prices_scroll += self._page_step(stdscr)  # clamped on draw
        elif act == "page_up":
            self.prices_scroll = max(0, self.prices_scroll - self._page_step(stdscr))
        elif act == "top":
            self.prices_scroll = 0
        elif act == "bottom":
            self.prices_scroll = 10_000  # clamped to the last page on draw
        elif act == "back":
            self.prices_model = None  # back to the model list
            self.prices_scroll = 0
        else:
            handled = self._prices_common_key(key, "prices.sessions")
            if handled is not None:
                return handled
            # Any other key is swallowed -- it must not tear down the list.
        return True

    def handle_filter_key(self, key: int | str) -> bool:
        # Live fuzzy filter mode (`/`): printable keys edit the query and every
        # list re-ranks on the very next paint. down/up still move the selection,
        # so you can land on a match without leaving the mode.
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("filter", key)
        if act == "cancel":  # restore the query from before `/`
            self.query = self._filter_before
            self.filter_active = False
            self._filter_edited()
            self.notice = "filter cancelled"
        elif act == "confirm":
            self.filter_active = False
            # The committed query already shows as a persistent orange "filter:" chip in
            # the header, so a "filter: x" notice would just duplicate it -- only the
            # cleared case (no chip) needs a word.
            self.notice = "" if self.query else "filter cleared"
        elif act == "erase":
            self.query = self.query[:-1]
            self._filter_edited()
        elif act == "clear":
            self.query = ""
            self._filter_edited()
        elif act in ("down", "up"):
            if self.show_prices:
                # The move keys walk the P model cursor so you can land on a match.
                self.prices_index += 1 if act == "down" else -1
                self.prices_index = max(0, self.prices_index)
            else:
                self.move(1 if act == "down" else -1)
        elif (ch := bindings.typed_char(key)) is not None:
            self.query += ch
            self._filter_edited()
        return True

    def _filter_edited(self) -> None:
        # Selection snaps to the best-ranked match whenever the query changes.
        self.workflow_index = 0
        self.project_index = 0
        self.prices_scroll = 0
        self.prices_index = 0
        # ...and every zoom picker's cursor, for the same reason the two above reset: the
        # query narrows these lists too (by model NAME on the Models tab, by the sessions
        # behind each row on Harnesses/Machines), so a keystroke can shrink one under its
        # cursor. The renderer clamps what it PAINTS, but a stale index here is still the
        # number j/k step from, so the first press spends itself re-clamping to the row
        # already highlighted and reads as a dead key.
        self.model_pick_index = 0
        self.source_index = 0
        self.machine_pick_index = 0

    def handle_mouse(self) -> bool:
        # The screen's clickable regions were registered by the last draw(), so a
        # click resolves against exactly what the user sees. Wheel scrolls the
        # current context; a click selects; a double-click selects then drills in.
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except curses.error:
            return True
        # getmouse reports screen cells; everything the renderer registered (regions,
        # sort headers, the trend bar geometry) is in content cells, inside the app
        # frame. Take the frame off the click once, here, and the rest of this file
        # never has to know the border exists.
        my -= self.renderer.oy
        mx -= self.renderer.ox
        up = bool(bstate & curses.BUTTON4_PRESSED)
        down = bool(bstate & getattr(self, "_wheel_down", 0))
        click = bool(bstate & curses.BUTTON1_CLICKED)
        double = bool(bstate & curses.BUTTON1_DOUBLE_CLICKED)

        if self.startup_warning is not None:
            return True  # a click cannot accidentally dismiss a data-loss warning
        if self.price_prompt:
            if click or double:
                self.price_prompt = False  # click = not now
                self.notice = f"skipped — {self.price_fetch_hint()}"
            return True
        if self.theme_menu:
            if up:
                self._preview_theme_at(self.theme_menu_index - 1)  # wheel live-previews
            elif down:
                self._preview_theme_at(self.theme_menu_index + 1)
            elif click or double:
                self.select_theme(self._theme_before, announce=False)
                self.theme_menu = False  # click cancels, theme reverted (like Esc)
            return True
        if self.demo_menu:
            n = len(DEMO_CATEGORIES)
            if up:
                self.demo_menu_index = (self.demo_menu_index - 1) % n
            elif down:
                self.demo_menu_index = (self.demo_menu_index + 1) % n
            elif click or double:
                self.demo_menu = False  # click cancels, demo state unchanged
            return True
        if self.source_menu:
            order = sources.source_cycle(self.args)
            if order and up:
                self.source_menu_index = (self.source_menu_index - 1) % len(order)
            elif order and down:
                self.source_menu_index = (self.source_menu_index + 1) % len(order)
            elif click or double:
                self.source_menu = False  # click cancels, source unchanged
            return True
        if self.machine_menu:
            options = self.machine_filter_options()
            if options and up:
                self.machine_menu_index = (self.machine_menu_index - 1) % len(options)
            elif options and down:
                self.machine_menu_index = (self.machine_menu_index + 1) % len(options)
            elif click or double:
                self.machine_menu = False  # click cancels, filter unchanged
            return True
        if self.harness_menu:
            options = self.harness_filter_options()
            if options and up:
                self.harness_menu_index = (self.harness_menu_index - 1) % len(options)
            elif options and down:
                self.harness_menu_index = (self.harness_menu_index + 1) % len(options)
            elif click or double:
                self.harness_menu = False  # click cancels, filter unchanged
            return True
        if self.whatif_menu:
            rows = self.whatif_rows()  # the wheel walks what's on screen, filter included
            if rows and up:
                self.whatif_menu_index = (self.whatif_menu_index - 1) % len(rows)
            elif rows and down:
                self.whatif_menu_index = (self.whatif_menu_index + 1) % len(rows)
            elif click or double:
                # The tier tabs are clickable like the P overlay's view tabs. Matched
                # by kind, never through the generic hit(): the modal floats OVER the
                # body, whose earlier-registered regions cover the same cells and
                # would win a first-match scan.
                for region in self.renderer.regions:
                    if len(region) == 5 and region[0] == "whatiftab":
                        _kind, ry, x0, x1, tier = region
                        if ry == my and x0 <= mx <= x1:
                            if bool(tier) != self.whatif_catalog:
                                self.whatif_toggle_catalog()
                            return True
                self.whatif_menu = False  # any other click cancels, pricing unchanged
            return True
        if self.sort_menu:
            options = self.sort_menu_options()
            if options and up:
                self.sort_menu_index = (self.sort_menu_index - 1) % len(options)
            elif options and down:
                self.sort_menu_index = (self.sort_menu_index + 1) % len(options)
            elif click or double:
                self.sort_menu = False  # click cancels, order unchanged
            return True
        if self.launch_menu is not None:
            if click or double:
                self.launch_menu = None  # click cancels the launch picker
                self.launch_menu_backend = None
            return True
        if self.toast_history:
            # The notices scrollback is drawn over the whole body, but it had no mouse
            # branch, so the body's click regions stayed live underneath it: a
            # double-click where the session list had been drilled into a session behind
            # the overlay, and the wheel scrolled that list instead of the scrollback.
            # Same shape as the help overlay below -- wheel pages it, a click closes it.
            if up:
                self.toast_history_scroll = max(0, self.toast_history_scroll - 3)
            elif down:
                self.toast_history_scroll += 3  # clamped to the last page on draw
            elif click or double:
                self.toast_history = False
            return True
        if self.help:
            if up:
                self.help_scroll = max(0, self.help_scroll - 3)
            elif down:
                self.help_scroll += 3  # clamped to the last page on draw
            elif click or double:
                self.help = False
            return True
        if self.show_prices:
            if self.prices_model is None:
                if up:
                    self.prices_index = max(0, self.prices_index - 1)
                elif down:
                    self.prices_index += 1  # clamped on draw
                elif click or double:
                    # A view tab switches the view, a column header sorts by it
                    # (re-click flips); any other click closes, as before.
                    target = self.renderer.hit(my, mx)
                    if target and target[0] == "pricetab":
                        self.set_prices_view(self.prices_views[target[1]][0])
                        return True
                    sort = self.renderer.sort_hit(my, mx)
                    if sort is not None:
                        self.apply_header_sort(*sort)
                    else:
                        self.show_prices = False
            else:
                if up:
                    self.prices_scroll = max(0, self.prices_scroll - 3)
                elif down:
                    self.prices_scroll += 3
                elif click or double:
                    self.prices_model = None  # back to the model list
                    self.prices_scroll = 0
            return True
        if self.trends:
            return self._mouse_trends(my, mx, up, down, click, double)
        if up or down:
            self._wheel(my, mx, -3 if up else 3)
            return True
        if not (click or double):
            return True
        sort = self.renderer.sort_hit(my, mx)
        if sort is not None:
            self.apply_header_sort(*sort)
            return True
        target = self.renderer.hit(my, mx)
        if target:
            self._apply_click(target, drill=double)
        return True

    def _mouse_trends(
        self, my: int, mx: int, up: bool, down: bool, click: bool, double: bool
    ) -> bool:
        current = self.trend_tabs[self.trend_tab % len(self.trend_tabs)]
        if up or down:
            older = down  # wheel down pages to older buckets, mirroring j/k
            if self.trend_drill is not None:
                n = len(self.trend_drill_sessions())
                step = 1 if down else -1
                self.trend_drill_index = max(0, min(self.trend_drill_index + step, n - 1))
            elif current == "Daily":
                n = len(self.trend_months())
                self.trend_month_index = self._step_trend_index(self.trend_month_index, n, older)
                self.trend_cursor = None  # re-anchor the cursor on the new month's peak
            elif current == "Weekly":
                n = len(self.trend_weeks())
                self.trend_week_index = self._step_trend_index(self.trend_week_index, n, older)
                self.trend_cursor = None
            elif current == "Calendar":
                n = len(self.calendar_years())
                self.trend_year_index = self._step_trend_index(self.trend_year_index, n, older)
                self.cal_cursor = None  # re-anchor the cursor on the new year's peak
            else:  # ranked tabs: the wheel moves the row cursor
                n = len(self.trend_ranked_keys())
                step = 1 if down else -1
                self.trend_row_index = max(0, min(self.trend_row_index + step, n - 1))
            return True
        if not (click or double):
            return True
        if current == "Calendar":
            date = self._calendar_date_at(my, mx)
            if date:
                if not self.trend_focus:
                    # The grid is modal: a click on the sleeping calendar only wakes it
                    # (like Enter). You can't pick or open a day until it's focused, so a
                    # stray click never jumps into a day -- the next click does that.
                    self.trend_focus = True
                    return True
                # Focused: a click moves the day cursor onto that cell, a double-click drills.
                self.cal_cursor = date
                if double:
                    if self.drill_into_date(date):
                        self._trend_return = ("Calendar", date)  # Esc returns to the heat map
                        self.trends = False
                    else:
                        self.notify(f"no sessions on {date}", "error")
                return True
        elif current in ("Daily", "Weekly", "Monthly"):
            key = self._trend_bar_at(my, mx)
            if key:
                if not self.trend_focus:
                    self.trend_focus = True  # a click on the sleeping chart only wakes it
                    return True
                # Focused: a click moves the bar cursor, a double-click drills in.
                self.trend_cursor = key
                if double:
                    self._trend_bar_open(current, key)
                return True
        sort = self.renderer.sort_hit(my, mx)
        if sort is not None and sort[1] == "trend":
            # A click on a ranked table's column header sorts by it; a re-click flips.
            # Guarded on the target so a zone left over from the view behind the
            # overlay could never re-sort a list the click didn't land on.
            self.apply_header_sort(*sort)
            return True
        target = self.renderer.hit(my, mx)
        if target and target[0] == "trendrow":
            # A ranked row (Models/Providers/Sources): click selects, double drills.
            self.trend_row_index = target[1]
            if double:
                self._open_trend_drill()
            return True
        if target and target[0] == "trendses":
            # A session row in a drill list: click selects, double opens the session.
            self.trend_drill_index = target[1]
            if double:
                self._open_trend_drill_session()
            return True
        if target and target[0] == "trend":
            if self.trend_tab != target[1]:
                self.trend_focus = False  # switching tabs leaves any focused canvas
                self.trend_row_index = 0
                self.trend_drill = None
            self.trend_tab = target[1]
        return True

    def _trend_bar_at(self, my: int, mx: int) -> str | None:
        # Resolve a mouse (y, x) to the bar-chart bucket under it, or None. Reads the
        # geometry the last bar-chart draw stashed (the whole column is clickable,
        # not just the filled cells, so short bars are easy to hit).
        geom = self._trend_bar_geom
        if geom is None:
            return None
        y0, y1, slots = geom
        if not (y0 <= my <= y1):
            return None
        return next((key for x0, x1, key in slots if x0 <= mx <= x1), None)

    def _apply_click(self, target: tuple[str, int], drill: bool) -> None:
        kind, value = target
        if kind == "trace-output":
            self.toggle_trace_output(value)
            return
        if kind == "modetab":
            # The top-level Time/Projects/Machines strip: switch mode (works from a
            # drilled session too, via switch_browse_mode).
            tabs = self.mode_tab_list()
            if 0 <= value < len(tabs):
                self.switch_browse_mode(tabs[value][1])
            return
        if kind == "tab":
            if self.view == "browse":
                # Clicking a tab in the right preview pane moves the focus there,
                # lazygit-style: zoom into the selected scope so the keys drive the
                # detail the user just clicked -- otherwise the left list stays
                # active and j/k keeps moving it instead.
                self.drill_in()
            if self.tab != value:
                self.tab = value
                self.scroll = 0
            return
        if kind == "detail":
            # The browse preview's catch-all (registered after its real regions, so
            # tabs/rows win): a click anywhere in the right pane focuses it.
            if self.view == "browse":
                self.drill_in()
            return
        if kind == "turnline":
            # A click on a Turns-tab prompt row drills into it (its full text + its
            # turns); clicks on the ▼/❄ marker lines between rows are inert.
            headers = getattr(self.renderer, "_turn_header_at", {})
            ordinal = headers.get(value)
            if ordinal is None:
                return
            # Move the keyboard cursor onto the clicked row first, so j/k pick up from
            # here. Inside a drilled prompt the SAME region carries that prompt's turns,
            # so a click there opens the turn's trace; the map is emptied while a trace
            # is open, so a click on its prose lands nowhere rather than on a stale row.
            if self.active_turn_drill is not None:
                self._trace_cursor = ordinal
                self.open_trace_drill()
                return
            self._turn_cursor = ordinal
            self.open_turn_drill(ordinal)
            return
        if kind in ("year", "month", "day"):
            # The time sidebar: live in browse, and still live behind a zoomed
            # detail (the split keeps it clickable -- a row click re-scopes the
            # detail in place, like the web's sidebar).
            if self.view not in ("browse", "zoom") or self.browse_mode != "time":
                return
            focus = {"year": "years", "month": "months", "day": "days"}[kind]
            if self.focus != focus:
                self.focus = focus
                self.tab = 0
                self.scroll = 0
                self.zoom_project = None
            if kind == "year":
                self.year_index = value
                self.month_index = 0
                self.day_index = 0
            elif kind == "month":
                self.month_index = value
                self.day_index = 0
            else:
                self.day_index = value
            if self.view == "zoom":
                # Re-scoped under the same tab: drop the old scope's cursors, and
                # swallow the drill -- double-clicking a sidebar row must not fall
                # through to "open the selected session" on a Sessions tab.
                # Every drill goes, and the model one is the clearest case: clicking a
                # month whose models differ leaves its Sessions list filtered to a model
                # it never ran (i.e. empty), and clicking a Day -- which has no Models tab
                # to show it on -- leaves that filter with nothing on screen naming it,
                # silently eating the next Esc.
                self._clear_zoom_drills()
                self.workflow_index = 0
                self.scroll = 0
                return
        elif kind == "project":
            self.project_index = value
            if self.view == "zoom":
                self._clear_zoom_drills()  # a drill this project may not satisfy
                self.workflow_index = 0
                self.scroll = 0
                return
        elif kind == "machine":
            # The Machines sidebar: a row click re-scopes the detail (in a zoom too);
            # a double-click drills into that box's sessions.
            changed = value != self.machine_index
            self.machine_index = value
            if self.view == "zoom":
                if changed:  # a NEW box -- its Harnesses/Projects/Models drill doesn't carry over
                    self._clear_box_drills()
                self.workflow_index = 0
                self.scroll = 0
                return
        elif kind == "session":
            self.workflow_index = value
        elif kind == "zoomproject":
            self.project_index = value
        elif kind == "zoomsource":
            self.source_index = value
        elif kind == "zoommodel":
            # The Models tab is a lines-rendered table, not a picker, so its region
            # carries a LINE index; the renderer's map resolves it to the row ordinal
            # (the turnline rule). Clicks on the frame/header/TOTAL rows land nowhere.
            ordinal = getattr(self.renderer, "_model_row_at", {}).get(value)
            if ordinal is None:
                return
            self.model_pick_index = ordinal
        elif kind == "zoommachine":
            self.machine_pick_index = value
        else:
            return
        if drill:
            self.drill_in()

    def prompt_range(self, stdscr: curses.window) -> None:
        initial = "" if self.range_input_value() == "all" else self.range_input_value()
        value = self.prompt_text(
            stdscr,
            "range: ",
            "all · 30d · 2m · 2026 · 2026-05 · start..end · "
            f"{self.keymap.label('input', 'cancel')} cancel",
            initial,
        )
        if value is None:
            return
        try:
            self.set_range_from_text(value)
        except ValueError as exc:
            self.notify(f"range error: {exc}", "error")

    def prompt_text(
        self,
        stdscr: curses.window,
        label: str,
        hint: str = "",
        initial: str = "",
        max_chars: int | None = None,
    ) -> str | None:
        # Modal bottom command line, laid out exactly like the `/` filter so input
        # never drifts: a short "<label>: " + the value you type is the input field
        # (orange) at the far LEFT, and the format hint sits to its right in plain
        # slate -- never a whole-orange line. The real cursor sits at the value's end.
        #
        # How much you may TYPE (max_chars) and how much FITS on the line (max_len)
        # are two different limits. A range or filter query is short by nature, so
        # they coincide by default -- but a note is prose, and capping it at the
        # visible field would silently truncate it at ~26 chars on an 80-column
        # terminal. With max_chars set, the value scrolls instead: the field shows
        # the tail (where the cursor is), "…"-marked at the left when there's more.
        head = " " + label
        field = curses.color_pair(6) | curses.A_BOLD
        value = initial
        with contextlib.suppress(curses.error):
            curses.curs_set(1)  # a terminal with no cursor styling still takes input
        try:
            while True:
                # Re-measure every pass and guard the writes (like Renderer.write):
                # shrinking the terminal mid-prompt must repaint, never raise.
                height, width = stdscr.getmaxyx()
                # This is the one place App itself paints. Go through the renderer's
                # primitives anyway, so the app frame's origin (and its clipping) applies
                # here too: the command line takes the footer's row *inside* the border,
                # never the border's own bottom row. Coordinates below are content cells.
                oy, ox = self.renderer.oy, self.renderer.ox
                width -= 2 * ox
                row = height - 2 * oy - 1
                shown, hx, max_len = self.prompt_layout(value, width, head, hint)
                limit = max_chars if max_chars is not None else max_len
                self.renderer.write(stdscr, row, 0, " " * width)
                self.renderer.write(stdscr, row, 0, clip(head + shown, width - 1), field)
                if hint and hx < width - 1:  # format hint in plain slate, to the right
                    self.renderer.write(
                        stdscr, row, hx, clip("   " + hint, width - hx - 1), curses.color_pair(4)
                    )
                try:
                    stdscr.move(row + oy, ox + max(0, min(width - 2, hx)))
                except curses.error:
                    pass  # a resize can invalidate any coordinate; next pass re-measures
                stdscr.refresh()

                # get_wch reads a *character*, so a multi-byte key (ä, é, —) arrives as
                # one str instead of the raw bytes getch would hand back. A note is
                # prose -- dropping every non-ASCII character out of it would be a bug
                # you only notice after you typed it. Special keys still come back as
                # ints, and a screen without get_wch (a test double) falls back.
                read = getattr(stdscr, "get_wch", None) or stdscr.getch
                try:
                    key = read()
                except curses.error:
                    continue  # interrupted read (resize/signal): repaint and wait again
                if key == curses.KEY_RESIZE:
                    continue  # repaint against the new size
                value, done, cancelled = self.filter_prompt_step(value, key, limit, self.keymap)
                if cancelled:
                    return None
                if done:
                    return value
        finally:
            with contextlib.suppress(curses.error):
                curses.curs_set(0)

    @staticmethod
    def prompt_layout(value: str, width: int, head: str, hint: str) -> tuple[str, int, int]:
        # The input line's geometry, in terminal CELLS rather than codepoints -- a note
        # can hold 界 or an emoji, each of which eats two of them, and sizing the field
        # with len() would run the text under the hint and leave the cursor mid-glyph.
        # Split out of prompt_text (like filter_prompt_step) so it's testable headless.
        # Returns (the field text to paint, the x the hint/cursor sits at, the field's
        # cell budget): a value that doesn't fit scrolls, showing its cursor end with
        # the hidden head marked "…".
        max_len = max(1, width - display_width(head) - display_width(hint) - 6)
        if display_width(value) <= max_len:
            shown = value
        elif max_len > 1:
            shown = "…" + clip_tail(value, max_len - 1)
        else:
            shown = ""
        return shown, display_width(head + shown), max_len

    @staticmethod
    def filter_prompt_step(
        value: str, key: int | str, max_len: int, keymap: bindings.Keymap | None = None
    ) -> tuple[str, bool, bool]:
        # One edit step of the modal input line. `key` is an int (getch / special keys)
        # or a str (get_wch: any character the user actually typed, ASCII or not).
        # Returns the new value + (done, cancelled). Resolved against the [input]
        # context (prompt_text passes the live keymap; headless callers get defaults).
        km = keymap or bindings.DEFAULT
        if isinstance(key, str):
            if not key:
                return value, False, False
            code = ord(key) if len(key) == 1 else -1
            if 0 <= code < 32 or code == 127:
                key = code  # a control character: resolve it like the int it is
        act = km.action("input", key)
        if act == "cancel":  # cancels without changing the current value
            return value, False, True
        if act == "confirm":
            return value, True, False
        if act == "erase":
            return value[:-1], False, False
        # A note is long enough that erasing it one key at a time is a chore, so the
        # readline reflexes work: kill_line (Ctrl-U) and kill_word (Ctrl-W). (Ctrl-U
        # is what the live `/` filter already binds -- same muscle memory.)
        if act == "kill_line":
            return "", False, False
        if act == "kill_word":  # back to the previous word boundary (bash keeps the space)
            stripped = value.rstrip()
            cut = stripped.rfind(" ")
            return (stripped[: cut + 1] if cut >= 0 else ""), False, False
        ch = bindings.typed_char(key)
        if ch is not None and len(value) < max_len:
            return value + ch, False, False
        return value, False, False

    def current_tabs(self) -> tuple[str, ...]:
        if self.view == "session":
            # The Tools tab (per-tool token attribution) rides on the part table,
            # which only OpenCode has -- gate it on the SELECTED session's backend so
            # in the merged view a Claude/Codex/Hermes/CSV session never shows an
            # unsupported (empty) tab, only OpenCode sessions do.
            wf = self.current_session()
            tabs = self.workflow_tabs
            if wf is not None and self.session_supports_turns(wf.id):
                tabs += ("Turns",)
            if wf is not None and self.session_supports_tools(wf.id):
                tabs += ("Tools",)
            if wf is not None and self.session_supports_context_curve(wf.id):
                # Context rides on the turn rows (the measured growth curve; Codex
                # opts out -- its rows are cumulative deltas, not prompt sizes);
                # the estimated composition section inside it is its own separate
                # per-backend opt-in (session_supports_context), absent not empty.
                tabs += ("Context",)
            return tabs
        if self.zoom_model:
            # A model is a contribution scope, not merely a session membership filter.
            # Keep its economics and attributed sessions together, with Esc returning to
            # the model ranking that opened it.
            return ("Economics", "Sessions")
        if self.browse_mode == "machines":
            base = self.machine_tabs
        elif self.browse_mode == "projects":
            base = self.project_tabs
        elif self.focus == "years":
            base = self.year_tabs
        else:
            base = self.month_tabs if self.focus == "months" else self.day_tabs
        # In the merged view a per-source cut is meaningful, so expose it right
        # after Overview. With one backend every row is the same source (a 100%
        # bar), so the tab would be noise -- omit it unless sources are combined.
        if getattr(self.store, "combined", False):
            base = base[:1] + ("Harnesses",) + base[1:]
        # The fleet's per-scope Machines picker (this month/day/project, cut by box):
        # right after Harnesses. Not in Machines MODE (already scoped to one box) and
        # not with a lone machine (a 100% bar, like the Harnesses gate).
        if self.machines_present and self.browse_mode != "machines":
            cut = base.index("Harnesses") + 1 if "Harnesses" in base else 1
            base = base[:cut] + ("Machines",) + base[cut:]
        return base

    def current_sort_options(self) -> tuple[str, ...]:
        # Left-hand months/days and non-session detail panes are fixed-order;
        # sort only reorders visible session or subagent lists.
        if self.view == "session" and self.on_subagents_tab:
            return self.subagent_sort_options
        if self.view != "session" and self.on_sessions_tab:
            return self.active_session_sort_options()
        return ()

    def workflows_for_day(self, day: str, source: list[Workflow] | None = None) -> list[Workflow]:
        rows = self.all_workflows if source is None else source
        return [workflow for workflow in rows if workflow.created_at.startswith(day)]

    def workflows_for_month(
        self, month: str, source: list[Workflow] | None = None
    ) -> list[Workflow]:
        rows = self.all_workflows if source is None else source
        return [workflow for workflow in rows if workflow.created_at.startswith(month)]

    def workflows_for_year(self, year: str, source: list[Workflow] | None = None) -> list[Workflow]:
        # A year is just a coarser date prefix than a month (created_at is
        # "YYYY-MM-DD ..."), so the same startswith match selects the whole year.
        rows = self.all_workflows if source is None else source
        if year == ALL_YEARS:  # the synthetic "All years" row spans every session
            return list(rows)
        return [workflow for workflow in rows if workflow.created_at.startswith(year)]

    def workflows_for_project(
        self, directory: str, include_ignored: bool = False
    ) -> list[Workflow]:
        rows = self.ranged_workflows if include_ignored else self.all_workflows
        return [w for w in rows if self.project_root(w.directory) == directory]

    def aggregate_models(
        self, workflows: list[Workflow]
    ) -> list[tuple[str, dict[str, float | int]]]:
        aggregate: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {
                "runs": 0,
                "cost": 0.0,
                "tokens": 0,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
            }
        )
        for workflow in workflows:
            for row in self.model_mix(workflow.id):
                item = aggregate[row["model_name"]]
                item["runs"] = int(item["runs"]) + int(row["runs"])
                item["cost"] = float(item["cost"]) + float(row["cost"] or 0)
                item["tokens"] = int(item["tokens"]) + int(row["tokens_total"] or 0)
                item["cache_read"] = int(item["cache_read"]) + int(row["cache_read"] or 0)
                item["cache_write"] = int(item["cache_write"]) + int(row["cache_write"] or 0)
                item["output"] = int(item["output"]) + int(row["output"] or 0)
        return sorted(
            aggregate.items(),
            key=lambda kv: (float(kv[1]["cost"]), int(kv[1]["tokens"])),
            reverse=True,
        )

    def _priced_nodes(self, rows: list) -> list[dict]:
        # In API mode, reprice each unpriced ($0) subagent node at API list prices
        # so the per-execution list matches the Overview/Models "$" figures. A $0
        # node is wholly unpriced, so its full token columns are the unpriced part.
        # Returns plain dicts (sqlite Rows are read-only) so sort/render/CSV all see
        # one effective cost.
        # This is exactly what the Cost column means everywhere, including under an
        # armed `w` target: what was recorded, estimated only where nothing was. It is
        # NOT the what-if's comparison baseline -- that has to price every token at its
        # own model's rates and is computed per MODEL, not per node
        # (App.whatif_session_totals). A node whose few metered cents sit beside a
        # subscription's unrecorded $0 would otherwise pass its cents off as the whole
        # baseline, and 20 of 48 metered nodes in real data are shaped exactly that way.
        api = self.show_api_prices and not self.store.demo
        out = []
        for row in rows:
            d = dict(row)
            if api and not d["cost"]:
                d["cost"] = api_equivalent_cost(
                    d["model_name"],
                    d["tokens_input"],
                    d["tokens_output"],
                    d["tokens_reasoning"],
                    d["tokens_cache_read"],
                    d["tokens_cache_write"],
                    node_1h_write(d),
                )
            out.append(d)
        return out

    def sorted_subagent_rows(self, rows: list) -> list:
        sort_by = self.subagent_sort_key()
        desc = self.sort_descending(sort_by, self.subagent_sort_reverse)
        if sort_by == "tokens":
            return sorted(rows, key=lambda row: (row["tokens_total"], row["cost"]), reverse=desc)
        if sort_by == "date":
            return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=desc)
        if sort_by == "title":
            return sorted(rows, key=lambda row: str(row["title"]).lower(), reverse=desc)
        if sort_by == "model":
            return sorted(rows, key=lambda row: str(row["model_name"]).lower(), reverse=desc)
        if sort_by == "agent":
            return sorted(rows, key=lambda row: str(row["agent"]).lower(), reverse=desc)
        if sort_by == "depth":
            # Biggest execution first within a depth whichever way the depths run
            # (the flip reorders depths, not their insides) -- two stable passes.
            by_tokens = sorted(
                rows, key=lambda row: (row["tokens_total"], row["cost"]), reverse=True
            )
            return sorted(by_tokens, key=lambda row: row["depth"], reverse=desc)
        return sorted(rows, key=lambda row: (row["cost"], row["tokens_total"]), reverse=desc)

    def range_label(self) -> str:
        if self.custom_since or self.custom_until:
            label = f"since {self.custom_since}" if self.custom_since else "from start"
            if self.custom_until:
                label += f" until {self.custom_until}"
            return label
        if self.range_days is not None:
            return f"last {self.range_days} days"
        if self.range_months is not None:
            if self.range_months % 12 == 0:
                years = self.range_months // 12
                return f"last {years} year{'s' if years != 1 else ''}"
            return f"last {self.range_months} month{'s' if self.range_months != 1 else ''}"
        return "all time"
