"""App: state and the keyboard/mouse state machine."""
from __future__ import annotations

import argparse
import copy
import csv
import os
import shlex
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

from opentab import sources, util
from opentab.demo import demo_cost, demo_model, demo_title
from opentab.formatting import short_path, shorten
from opentab.heatmap import (
    HEAT_DEFAULT_LEVELS,
    HEAT_MAX_LEVELS,
    HEAT_MIN_LEVELS,
    PRICE_HEAT_BASE_PAIR,
    PRICE_HEAT_LEVELS,
    heat_palette,
    week_key,
)
from opentab.models import ALL_YEARS, DaySummary, MonthSummary, ProjectSummary, YearSummary
from opentab.pricing import (
    FALLBACK_PRICE,
    api_equivalent_cost,
    canonical_model,
    display_model,
    effective_price,
    family_label,
    invalidate_price_cache,
    is_local_provider,
    model_family,
    model_price,
    price_cache_meta,
    refresh_model_prices,
)
from opentab.sources import RESUME_COMMANDS, SOURCE_LABELS
from opentab.tui.renderer import Renderer
from opentab.util import (
    fuzzy_score,
    in_tmux,
    launcher_hook,
    month_window_start,
    open_path,
    parse_range_text,
    resolve_project_root,
    workflow_fuzzy_score,
)


class Toast:
    """One transient notification: text, a kind (info/success/warn/error that
    picks its colour), and a monotonic birth time + time-to-live so the run loop
    can fade it out on its own. Kept deliberately tiny -- it's pure UI state the
    Renderer reads by duck typing (no import back into the renderer)."""

    __slots__ = ("text", "kind", "born", "ttl")

    def __init__(self, text: str, kind: str, born: float, ttl: float):
        self.text = text
        self.kind = kind
        self.born = born
        self.ttl = ttl

    def remaining(self, now: float) -> float:
        return self.ttl - (now - self.born)


class PriceEntry(NamedTuple):
    """One row of the P overlay's price table: a model, its vendor `family`, the
    `routes` you reach it through (e.g. {"anthropic", "github-copilot"}), its `spend`,
    and the `group` key for the active view. In the vendor and flat views a row is a
    distinct model deduped to its *canonical* id (alias spellings, date pins, and
    effort suffixes fold together -- the list price is route- and spelling-
    independent), so `routes` may hold several; in the provider view a row is one
    (route, model) pair, so `routes` is a single route and the row can repeat a
    model across gateways. The Renderer reads it by duck typing."""

    bare: str  # display spelling: the row's most-used alias, date/effort suffix stripped
    canon: str  # canonical_model() key -- what the row deduped/groups/drills in by
    family: str  # vendor family key from model_family(), "" == Other
    routes: tuple[str, ...]  # access routes, sorted; () when the id had no prefix
    spend: float  # summed cost across the aliases/routes this row covers
    group: str  # grouping key for the active view ("" == no group / flat)
    share: float  # this row's share of all priced (non-local) tokens
    price: tuple  # (input, output, cacheR, cacheW) from the most completely-priced alias
    eff: float  # $/M for the app-wide token mix at `price` (the "eff $/M" column)
    approx: bool  # eff had no cache-read rate; reads were billed at the input rate


class App:
    workflow_tabs = ("Overview", "Models", "Subagents")
    day_tabs = ("Overview", "Projects", "Sessions")  # day models stay folded into Overview
    month_tabs = ("Overview", "Models", "Projects", "Sessions")
    year_tabs = ("Overview", "Models", "Projects", "Sessions")
    project_tabs = ("Overview", "Models", "Sessions")
    sort_options = ("cost", "tokens", "date", "subagents", "title")
    project_sort_options = ("cost", "tokens", "sessions", "subagents", "project", "recency")
    subagent_sort_options = ("cost", "tokens", "title", "model", "agent", "depth")
    # The P overlay's price table sorts by model name, the blended eff column, your
    # usage share, or any of the four list-price columns. "eff" is the default and
    # sorts cheapest-first (it's in ascending_sort_keys); model sorts a->z; the raw
    # price columns and "use" sort high->low, so the priciest/most-used surface first.
    prices_sort_options = ("model", "eff", "use", "input", "output", "cache_read", "cache_write")
    # The P overlay's layout modes, cycled by `p`: "flat" (the default) is one
    # ungrouped list -- cheapest-for-your-mix is a cross-vendor question -- while
    # "family" groups deduped models under their vendor (Anthropic/OpenAI/…) and
    # "provider" groups one row per access route (anthropic/github-copilot/…, a
    # model can repeat across gateways). (key, label) -- shown in header + toast.
    prices_views = (("flat", "flat list"), ("family", "by vendor"), ("provider", "by provider"))
    # Columns whose natural order is ascending (a->z / shallow-first / cheap-first);
    # every other column sorts high->low by default. A header re-click flips it.
    ascending_sort_keys = frozenset({"title", "project", "model", "agent", "depth", "eff"})
    trend_tabs = ("Daily", "Weekly", "Monthly", "Calendar", "Models", "Providers", "Sources")
    # The `L` launch picker's targets: (shortcut key, kind, label). "copy" hands the
    # resume command to the clipboard and is always offered; the tmux window/split/
    # popup spawns need tmux or a launcher hook (launch_targets filters them out).
    LAUNCH_TARGETS = (
        ("w", "window", "new window"),
        ("s", "hsplit", "split pane │"),
        ("v", "vsplit", "split pane ─"),
        ("p", "popup", "popup"),
        ("y", "copy", "copy resume command"),
    )
    # Toast notifications: how long one lingers, when it starts fading, and how many
    # stack before the oldest is dropped. While any toast is alive the run loop polls
    # (TOAST_POLL_MS) so they expire on time without a keystroke; otherwise it blocks.
    TOAST_TTL = 4.0
    TOAST_FADE = 0.9
    TOAST_MAX = 3
    TOAST_POLL_MS = 200
    # Class-level defaults so App instances built via __new__ in tests (skipping
    # __init__) still accept a notice. _toast_clock is injectable per instance for
    # deterministic expiry tests; the live `toasts` list is lazily materialised below.
    _toast_clock = staticmethod(time.monotonic)
    _toast_shown = True  # has the newest toast been painted at least once?

    def __init__(self, store: Store, args: argparse.Namespace, source_key: str = ""):
        self.store = store
        self.args = args
        # Live source switching (the `c` key). source_key is the active backend's key;
        # built stores are cached so cycling back is instant. Empty when the App was
        # constructed without a key (tests / single fixed store).
        self.source_key = source_key
        self._store_cache: dict[tuple[str, bool], object] = (
            {(source_key, bool(getattr(store, "demo", False))): store} if source_key else {}
        )
        self.loaded = store.workflows()  # every root session, all time
        # "$" toggles real cost <-> API-equivalent. When no active backend records
        # dollars (Claude Code alone, or "all" with Claude in the mix) the real view
        # is a wall of $0.00, so start in the estimate view; an explicit saved pref
        # (apply_state) or the $ key takes over from there.
        self.show_api_prices = not getattr(store, "records_cost", True) and not store.demo
        self._snapshot_real_costs()
        self._resolve_project_roots()
        # The per-model breakdown is the one heavy scan of the (huge) message
        # table; it's deferred so the first frame paints off the fast session
        # rollup. run() loads it right after that first paint, before any key is
        # handled -- so model_count and the Models tabs are ready by the time
        # anything shows them. Empty until then; model_mix tolerates that.
        self._model_by_root: dict[str, list[dict]] = defaultdict(list)
        self._models_loaded = False
        # Per-tool attribution (OpenCode only) is fetched lazily on drill-in and
        # cached per session id -- it's a ~per-session scan of the part table, not
        # the startup-wide message scan model_breakdown does. See session_tool_rows.
        self._tool_by_session: dict[str, list[dict]] = {}
        # Per-turn timeline (OpenCode + Claude), same lazy/cached-per-session deal as
        # the tool rows above -- a cheap per-session scan, never loaded at startup.
        self._turns_by_session: dict[str, list[dict]] = {}
        # Active range: custom bounds from CLI take precedence, else a day window
        # (None = all). Default is all time so the Months panel is actually useful.
        self.custom_since = args.since
        self.custom_until = args.until
        self.range_days = None if (args.since or args.until or args.days is None) else args.days
        self.range_months: int | None = None  # set by an "Nm"/"Ny" range, calendar-based
        self.query = ""
        self.filter_active = False  # live `f` filter mode: keys edit the query
        self._filter_before = ""  # the query as it was when `/` opened the mode (Esc restores)
        self.launch_menu: Workflow | None = None  # session awaiting an `L` launch-target key
        self.launch_menu_index = 0  # highlighted row in that picker
        self.price_prompt = False  # the "unpriced models found" startup prompt
        self._price_prompt_done = False  # offered at most once per run
        self.prices_prompt_dismissed = False  # "don't ask again" pref (persisted in state)
        self.allow_price_prompt = True  # off under --no-state/--demo (set in main)
        self.unknown_models: list[str] = []  # used models with no built-in price
        self.source_menu = False  # the `c` data-source picker overlay
        self.source_menu_index = 0  # highlighted row in that picker
        self.sort_menu = False  # the `s` sort-order picker overlay
        self.sort_menu_index = 0  # highlighted row in that picker
        self.day_index = 0
        self.month_index = 0
        self.year_index = 0
        self.project_index = 0
        self.workflow_index = 0  # selected session in a zoomed Sessions tab
        # Tab cycles focus across the three stacked left panels. Enter drills:
        # browse -> zoom (year/month/day detail) -> session (one session's detail).
        self.focus = "days"  # "years" | "months" | "days"
        self.browse_mode = "time"  # "time" | "projects"
        self.view = "browse"  # "browse" | "zoom" | "session"
        self.tab = 0
        self.scroll = 0
        self.help = False
        self.help_scroll = 0  # pager offset within the help overlay
        self.trends = False  # the Trends overlay (T); trend_tab selects its tab
        self.trend_tab = 0
        self.trend_month_index = 0  # which month the Daily tab shows (0 = newest)
        self.trend_week_index = 0  # which week the Weekly tab shows (0 = newest)
        self.trend_year_index = 0  # which year the Calendar tab shows (0 = newest)
        self.cal_cursor: str | None = None  # highlighted day on the Calendar tab (None = peak)
        self.cal_focus = False  # Calendar day-grid focused (Enter focuses, Esc steps back out)
        self.cal_levels = HEAT_DEFAULT_LEVELS  # heat-map granularity, live-adjustable with +/-
        self.has256 = False  # set in run() once curses knows the terminal's color depth
        self._cal_geom: tuple | None = None  # last calendar grid geometry, for mouse hit-testing
        self._cal_return: str | None = None  # day drilled in from the heat map; Esc returns there
        self.show_prices = False  # the "P" model-prices reference overlay
        self.prices_scroll = 0  # pager offset within that overlay
        self.prices_index = 0  # selected model row in the P overlay's list
        self.prices_model: str | None = None  # drilled into this model's sessions (P overlay)
        # The P overlay's column sort: cheapest-for-your-mix first by default (the
        # point of the overlay); pick another column via the `s` picker or a header
        # click, re-clicking a header flips direction (prices_sort_reverse).
        self.prices_sort = "eff"
        self.prices_sort_reverse = False
        self.prices_view = "flat"  # P overlay layout: one of prices_views (p cycles)
        self.sort_by = "cost"
        self.project_sort_by = "cost"
        # Per-context "flipped off the natural order" flags, toggled by re-clicking a
        # column header. sort_reverse covers the session and subagent lists (which
        # share sort_by); project lists have their own.
        self.sort_reverse = False
        self.project_sort_reverse = False
        self.ignored_projects: set[str] = set()
        self.show_ignored_projects = False
        # Sessions starred with `b` (ids, persisted in state.json). `B` flips
        # show_bookmarks_only, the session-level cousin of the ignored projects' I:
        # every view narrows to just the starred sessions.
        self.bookmarks: set[str] = set()
        self.show_bookmarks_only = False
        # When set (in a month/day zoom), the Sessions list is narrowed to this
        # project's sessions within the zoomed scope. Drilled into from the
        # Projects tab; cleared on step-out or any scope change.
        self.zoom_project: str | None = None
        # All screen output lives on the Renderer; the App stays curses-free
        # (aside from the modal prompt line in prompt_text).
        self.renderer = Renderer(self)
        self._anchor_default_selection()

    def _anchor_default_selection(self) -> None:
        # Open on "All years" (so the Months panel lists the whole history) with the
        # Months selection sitting on the current calendar month -- falling back to the
        # newest month when this month has no data yet. The Days panel is the default
        # active focus (set in __init__), so this anchor decides which month's days it
        # lists. Called at startup, after restoring saved prefs, and on a source switch
        # -- any time the dataset (and so the years/months) changes under us. The year
        # must be set before reading self.months, which is scoped to the focused year.
        years = self.years
        # Prefer the synthetic "All years" row; with a single year it isn't shown,
        # so fall back to that lone year (index 0).
        self.year_index = next((i for i, y in enumerate(years) if y.year == ALL_YEARS), 0)
        months = self.months
        now = datetime.now().strftime("%Y-%m")
        self.month_index = next((i for i, m in enumerate(months) if m.month == now), 0)

    def _invalidate_workflow_cache(self) -> None:
        self._rw_key = self._rw_cache = self._aw_key = self._aw_cache = None

    @property
    def ranged_workflows(self) -> list[Workflow]:
        # Cached range-only source. Ignored-project filtering happens in
        # all_workflows so ignored projects can still be shown for unignore.
        key = (
            id(self.loaded),
            self.custom_since,
            self.custom_until,
            self.range_days,
            self.range_months,
            # Bookmarks-only (B) narrows at the source so every downstream view --
            # summaries, projects, trends, exports, even shown-ignored paths -- agrees.
            # The fingerprint keys the cache, so toggling b/B rebuilds it by itself.
            tuple(sorted(self.bookmarks)) if self.show_bookmarks_only else None,
        )
        if getattr(self, "_rw_key", None) == key:
            return self._rw_cache
        rows = self.loaded
        if self.show_bookmarks_only:
            rows = [w for w in rows if w.id in self.bookmarks]
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
        # Every visible workflow in the active range. Ignored projects are removed
        # here, so summaries, trends, sessions, and exports all agree.
        key = (
            id(self.loaded),
            self.custom_since,
            self.custom_until,
            self.range_days,
            self.range_months,
            tuple(sorted(self.ignored_projects)),
            # ranged_workflows narrows to bookmarks under B; mirror its fingerprint
            # so this cache follows along.
            tuple(sorted(self.bookmarks)) if self.show_bookmarks_only else None,
        )
        if getattr(self, "_aw_key", None) == key:
            return self._aw_cache
        rows = [
            w
            for w in self.ranged_workflows
            if self.project_root(w.directory) not in self.ignored_projects
        ]
        self._aw_key = key
        self._aw_cache = list(rows)
        return self._aw_cache

    def range_cost_total(self) -> float:
        return sum(w.total_cost for w in self.all_workflows)

    def set_all_time(self) -> None:
        self.zoom_project = None
        anchor = self.selection_anchor()
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
        self.zoom_project = None
        anchor = self.selection_anchor()
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
        # Sessions of the selected day — the lazygit "commits of this branch".
        day = self.active_day
        rows = self.workflows_for_day(day) if day else []
        return self.filtered_sessions(rows)

    def filtered_sessions(self, rows: list[Workflow]) -> list[Workflow]:
        # The active sort first, then (with a query) rank fuzzy matches by
        # score. The sort is stable input to the ranking, so equally good
        # matches keep their cost/date order.
        rows = self.sorted_workflows(rows)
        if not self.query:
            return rows
        scored = [(workflow_fuzzy_score(self.query, w), w) for w in rows]
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
        # All days in range, always newest-first — left-hand nav is date-sorted.
        return sorted(self._day_summaries(self.all_workflows), key=lambda d: d.day, reverse=True)

    @property
    def panel_days(self) -> list[DaySummary]:
        # Days belonging to the focused month, newest-first — the lower-left panel.
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
        # Always newest-first — left-hand nav is date-sorted.
        years.sort(key=lambda y: y.year, reverse=True)
        # An "All years" row at the top unscopes the Months panel to the full
        # history. Only worth showing with >1 year (otherwise it just mirrors it).
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
        # The middle panel is scoped to the focused year, so a long history reads as
        # one year at a time instead of a giant flat list (the whole point of Years).
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
        # Always newest-first — left-hand nav is date-sorted.
        return sorted(months, key=lambda m: m.month, reverse=True)

    def _resolve_project_roots(self) -> None:
        # directory -> main-repo path, so git worktrees fold into their parent
        # project. Resolved once per distinct directory at load (cheap; only the
        # worktree dirs trigger a file read). Skipped for demo / --no-worktrees.
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

    def sorted_projects(self, rows: list[ProjectSummary]) -> list[ProjectSummary]:
        sort_by = (
            self.project_sort_by
            if self.project_sort_by in self.project_sort_options
            else self.project_sort_options[0]
        )
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
        return sorted(rows, key=lambda p: (p.cost, p.tokens), reverse=desc)

    @property
    def focused_year(self) -> str | None:
        rows = self.years
        if not rows:
            return None
        self.year_index = max(0, min(self.year_index, len(rows) - 1))
        year = rows[self.year_index].year
        # "All years" scopes to the whole history -> no single-year filter.
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
        if self.view == "zoom" and self.on_projects_tab:
            return self.zoom_selected_project()
        return None

    def can_toggle_project_ignore(self) -> bool:
        return self.active_project_for_toggle() is not None

    def toggle_ignored_projects_view(self) -> None:
        if not self.ignored_projects:
            self.notify("no ignored projects", "error")
            return
        project = self.active_project_for_toggle()
        project_dir = project.directory if project else None
        self.show_ignored_projects = not self.show_ignored_projects
        if not self.show_ignored_projects and self.zoom_project in self.ignored_projects:
            self.zoom_project = None
            if (
                self.view == "zoom"
                and self.browse_mode != "projects"
                and "Projects" in self.current_tabs()
            ):
                self.tab = self.current_tabs().index("Projects")
        self.restore_project_selection(project_dir)
        self.notice = (
            "showing ignored projects" if self.show_ignored_projects else "hiding ignored projects"
        )

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
        # `b` works wherever one session is selected: a zoom's Sessions tab or the
        # drilled-in session detail — the same contexts as `L` (launch_session).
        if self.view == "session" or (self.view == "zoom" and self.on_sessions_tab):
            return self.current_session()
        return None

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
            # Unstarring under the B filter drops the row from every list.
            if not self.bookmarks:
                self.show_bookmarks_only = False
                self.notice = "last bookmark removed — showing all sessions"
                # The list just widened back out; keep the cursor (and an open
                # session detail) on the session that was unstarred.
                rows = self.current_sessions()
                self.workflow_index = next(
                    (i for i, w in enumerate(rows) if w.id == session.id),
                    min(self.workflow_index, max(0, len(rows) - 1)),
                )
            elif self.view == "session" and self.current_session() is not session:
                self.drill_out()  # the open session just left the narrowed list

    def toggle_bookmarks_view(self) -> None:
        # `B` flips the bookmarks-only view: every list narrows to the sessions
        # starred with `b` (within the active range), mirroring I for ignored
        # projects. ranged_workflows applies the filter (keyed into its cache).
        if not self.show_bookmarks_only and not self.bookmarks:
            self.notify("no bookmarks — press b on a session", "error")
            return
        anchor = self.selection_anchor()
        self.show_bookmarks_only = not self.show_bookmarks_only
        self.restore_selection(anchor)
        if self.view == "session" and self.current_session() is None:
            self.drill_out()  # the open session isn't bookmarked; back to the list
        self.notice = (
            "showing bookmarked sessions only"
            if self.show_bookmarks_only
            else "showing all sessions"
        )

    def selection_anchor(
        self,
    ) -> tuple[str | None, str | None, str | None, str | None, str | None]:
        # Capture the selected row's value (not focused_year, which is None for the
        # "All years" row) so an "All years" selection survives a reload/source switch.
        sel_year = self.selected_year_summary
        year = sel_year.year if sel_year else None
        month = self.focused_month
        day = self.active_day if month else None
        project = self.selected_project_summary
        session = self.current_session()
        return (
            year,
            month,
            day,
            project.directory if project else None,
            session.id if session else None,
        )

    def restore_selection(
        self,
        anchor: tuple[str | None, str | None, str | None, str | None, str | None],
    ) -> None:
        year, month, day, project_dir, session_id = anchor

        # Restore the year first: months/days are scoped to the focused year, so the
        # month lookup below only sees the right slice once year_index is set.
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
        # root_id -> [ {model_name, runs, cost, tokens_total, cache_read, cache_write, output}, ... ]
        self._model_by_root: dict[str, list[dict]] = defaultdict(list)
        for row in self.store.model_breakdown():
            self._model_by_root[row["root_id"]].append(dict(row))
        # model_count rides along on the breakdown (one message scan at startup
        # instead of two): distinct models per root == its number of breakdown rows.
        # MSG_MODEL_EXPR coalesces to 'unknown', so a root never gets a NULL group;
        # this equals the old count(distinct ...). Done before any demo renaming.
        for w in self.loaded:
            w.model_count = len(self._model_by_root.get(w.id, ()))
        if self.store.demo:
            for root_id, models in self._model_by_root.items():
                self._model_by_root[root_id] = self._scale_demo_models(
                    self._demo_rename_models(models)
                )
            # Reconcile after scaling: the model rows and the workflow totals are now
            # both multiplied by the same factor, so the synthetic fill stays consistent.
            self._reconcile_demo_models()
        else:
            self._compute_api_costs()
        self._models_loaded = True
        self._apply_price_mode()  # re-assert the active ($/API) view onto fresh rows

    def _ensure_models(self) -> None:
        # Run the deferred model-breakdown load once, on demand. Idempotent so the
        # run() loop, reload(), or any first model access all converge to one scan.
        if not self._models_loaded:
            self._load_model_cache()

    @staticmethod
    def _demo_rename_models(models: list[dict]) -> list[dict]:
        # Rename local models to cloud ones, merging rows that collide on the new
        # name so the Models table never shows two rows with the same label.
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

    # Per-model magnitude fields scaled by the demo factor: costs round to cents,
    # token counts to ints. runs/model_name are structural and left untouched.
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
        # Apply the hidden demo factor to every per-model cost/token so the Models tab
        # can't be multiplied back into real spend, matching the scaled workflow totals.
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
        # Make each session's per-model rows sum exactly to that session's demo
        # total cost/tokens. Subscription/credit rows (Copilot, Codex, Claude Code)
        # carry real runs/tokens but $0 cost in the message JSON, so we distribute
        # the session's synthetic shortfall across those rows by message count.
        # Keeps the Models tab consistent with the Money panel at every zoom level.
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

    def session_supports_tools(self, workflow_id: str) -> bool:
        # Whether the Tools tab applies to this session -- backends that don't
        # implement tool_breakdown (Claude/Codex/Hermes/CSV) have no supports_tools,
        # so the tab is hidden rather than shown empty.
        check = getattr(self.store, "supports_tools", None)
        return bool(check(workflow_id)) if check else False

    def session_tool_rows(self, workflow_id: str) -> list[dict]:
        # Raw per-(tool, model) attribution for one session, fetched once and cached.
        # The store call is the heavy bit (~per-session part scan), so memoize it; the
        # Tools renderer aggregates/reprices these on top each frame (cheap).
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
        # Hide real magnitudes the same way _demo_workflow does: synthesize a price for
        # $0 (subscription) rows so the tab isn't a wall of red $0.00, then scale every
        # cost/token by the hidden per-process factor. Tool/model names aren't
        # sensitive, so they pass through unchanged.
        k = self.store.demo_scale
        for r in rows:
            if r.get("cost", 0) == 0 and r.get("tokens_total", 0) > 0:
                r["cost"] = demo_cost(
                    r["tokens_total"], f"{workflow_id}:{r['tool']}:{r['model_name']}"
                )
            for f in ("tokens_total", "input", "output", "reasoning", "cache_read", "cache_write"):
                r[f] = int(round(r.get(f, 0) * k))
            r["cost"] = round(r.get("cost", 0) * k, 4)
        return rows

    def session_supports_turns(self, workflow_id: str) -> bool:
        # Whether the Turns tab applies to this session. Only OpenCode and Claude Code
        # implement message_timeline, so a Codex/Hermes/CSV (or FakeStore) session has
        # no supports_turns and the tab is hidden rather than shown empty.
        check = getattr(self.store, "supports_turns", None)
        return bool(check(workflow_id)) if check else False

    def session_turn_rows(self, workflow_id: str) -> list[dict]:
        # Chronological per-turn rows for one session, fetched once and cached. The
        # store call is the heavy bit (~per-session message scan); the Turns renderer
        # reprices/accumulates on top each frame (cheap), same as session_tool_rows.
        cached = self._turns_by_session.get(workflow_id)
        if cached is not None:
            return cached
        fetch = getattr(self.store, "message_timeline", None)
        rows = [dict(r) for r in fetch(workflow_id)] if fetch else []
        if self.store.demo:
            rows = self._scale_demo_turns(workflow_id, rows)
        self._turns_by_session[workflow_id] = rows
        return rows

    def _scale_demo_turns(self, workflow_id: str, rows: list[dict]) -> list[dict]:
        # Hide real magnitudes like _scale_demo_tools: remap local model names, give
        # $0 (subscription) turns a synthetic price so the cumulative column isn't a
        # wall of red, then scale every cost/token by the hidden per-process factor.
        k = self.store.demo_scale
        for n, r in enumerate(rows):
            r["model_name"] = demo_model(r["model_name"])
            # Anonymize the prompt title (a real prompt would leak); keep it stable per
            # prompt_id so a group's turns stay under one fake header.
            if "prompt_title" in r:
                r["prompt_title"] = demo_title(r.get("prompt_id") or "noprompt")
            if r.get("cost", 0) == 0 and r.get("tokens_total", 0) > 0:
                r["cost"] = demo_cost(r["tokens_total"], f"{workflow_id}:{n}")
            for f in ("tokens_total", "input", "output", "reasoning", "cache_read", "cache_write"):
                r[f] = int(round(r.get(f, 0) * k))
            r["cost"] = round(r.get("cost", 0) * k, 4)
        return rows

    def _snapshot_real_costs(self) -> None:
        # Freshly loaded rows carry only real cost; seed the real/api snapshots so
        # _apply_price_mode is safe even before the (deferred) model scan runs.
        for w in self.loaded:
            w.real_total_cost = w.api_total_cost = w.total_cost
            w.real_root_cost = w.api_root_cost = w.root_cost

    def _compute_api_costs(self) -> None:
        # For each model row, keep its real cost and an API-equivalent: real spend
        # plus only the messages in that row that OpenCode recorded as $0.
        # model_breakdown groups by model, so priced and unpriced messages can be
        # mixed in one row; the unpriced_* fields preserve that split.
        # Re-run on price refresh while the $ view may already be applied, so build
        # from the real_* snapshots only -- the live cost fields can hold the
        # previous estimate, and adding to them compounds it on every refresh.
        by_id = {w.id: w for w in self.loaded}
        for root_id, rows in self._model_by_root.items():
            has_root_split = any("root_unpriced_input" in m for m in rows)
            root_delta = 0.0
            for m in rows:
                real = m["real_cost"] = m.get("real_cost", m["cost"])
                # Tests and older in-memory callers may not carry unpriced_*;
                # pure-$0 rows can still price from their aggregate token fields.
                all_unpriced = real == 0 and "unpriced_input" not in m
                m["api_cost"] = real + api_equivalent_cost(
                    m["model_name"],
                    m.get("input", 0) if all_unpriced else m.get("unpriced_input", 0),
                    m.get("output", 0) if all_unpriced else m.get("unpriced_output", 0),
                    m.get("reasoning", 0) if all_unpriced else m.get("unpriced_reasoning", 0),
                    m.get("cache_read", 0) if all_unpriced else m.get("unpriced_cache_read", 0),
                    m.get("cache_write", 0) if all_unpriced else m.get("unpriced_cache_write", 0),
                )
                if has_root_split:
                    root_delta += api_equivalent_cost(
                        m["model_name"],
                        m.get("root_unpriced_input", 0),
                        m.get("root_unpriced_output", 0),
                        m.get("root_unpriced_reasoning", 0),
                        m.get("root_unpriced_cache_read", 0),
                        m.get("root_unpriced_cache_write", 0),
                    )
            wf = by_id.get(root_id)
            if not wf:
                continue
            delta = sum(m["api_cost"] - m["real_cost"] for m in rows)  # only $0 rows differ
            wf.api_total_cost = wf.real_total_cost + delta
            if has_root_split:
                wf.api_root_cost = wf.real_root_cost + root_delta
            else:
                # Older in-memory test rows lack root-vs-subagent token splits.
                # Fall back to the old approximation only when exact data is absent.
                frac = wf.real_root_cost / wf.real_total_cost if wf.real_total_cost else 1.0
                wf.api_root_cost = wf.real_root_cost + delta * frac

    def _apply_price_mode(self) -> None:
        # Point every panel's cost at either the real or the API-equivalent figure.
        api = self.show_api_prices and not self.store.demo
        for w in self.loaded:
            w.total_cost = w.api_total_cost if api else w.real_total_cost
            w.root_cost = w.api_root_cost if api else w.real_root_cost
        for rows in self._model_by_root.values():
            for m in rows:
                m["cost"] = m.get("api_cost", m["cost"]) if api else m.get("real_cost", m["cost"])

    def toggle_api_prices(self) -> None:
        if self.store.demo:
            self.notify("API-price view is for real data, not the demo", "error")
            return
        self._ensure_models()  # needs the per-model token breakdown
        self.show_api_prices = not self.show_api_prices
        self._apply_price_mode()
        self.notice = (
            "what-if prices (what unpriced usage would cost at API list prices)"
            if self.show_api_prices
            else "actual cost"
        )

    def refresh_prices_action(self) -> None:
        # Pull the latest models.dev prices into the local cache, then re-price every
        # unpriced row in place so the P overlay and the $ view reflect the new rates.
        self.notice = "fetching prices from models.dev…"
        try:
            count, _ = refresh_model_prices()
        except (OSError, ValueError) as exc:
            self.notify(f"price refresh failed: {exc}", "error")
            return
        invalidate_price_cache()  # drop the in-process overlay so the new file is read
        self._ensure_models()
        self._compute_api_costs()
        self._apply_price_mode()
        self.prices_scroll = 0
        self.notify(f"refreshed {count} model prices from models.dev", "success")

    def unknown_priced_models(self) -> list[str]:
        # Used, non-local models with no built-in price (resolve to the generic
        # fallback) -- the ones whose $ estimate is a guess until --refresh-models.
        out: list[str] = []
        seen: set[str] = set()
        for rows in self._model_by_root.values():
            for m in rows:
                name = m.get("model_name")
                if not name or name in seen:
                    continue
                seen.add(name)
                if is_local_provider(name):
                    continue
                if model_price(name) == FALLBACK_PRICE:
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

    def handle_price_prompt_key(self, key: int) -> bool:
        # y/Enter fetches now; d never asks again (persisted); n/Esc/other = not now.
        if key == 3:  # Ctrl-C still quits
            return False
        if key in (ord("y"), ord("Y"), 10, 13, curses.KEY_ENTER):
            self.price_prompt = False
            self.refresh_prices_action()  # fetch + reprice in place
        elif key in (ord("d"), ord("D")):
            self.price_prompt = False
            self.prices_prompt_dismissed = True  # save_state persists it on exit
            self.notice = "won't ask again — fetch anytime with --refresh-models or r in P"
        else:  # n, Esc, or anything else: not now, ask again next run
            self.price_prompt = False
            self.notice = "skipped — fetch anytime with --refresh-models or r in the P view"
        return True

    def reload(self) -> None:
        self.loaded = self.store.workflows()
        self._snapshot_real_costs()
        self._resolve_project_roots()
        self._tool_by_session.clear()
        self._turns_by_session.clear()
        self._load_model_cache()
        self.zoom_project = None
        self.workflow_index = min(self.workflow_index, max(0, len(self.workflows) - 1))
        self.day_index = min(self.day_index, max(0, len(self.days) - 1))
        self.month_index = min(self.month_index, max(0, len(self.months) - 1))
        self.project_index = min(self.project_index, max(0, len(self.projects) - 1))
        self.notify("reloaded", "success")

    # --- Live source switching (the `c` key) ---------------------------------
    def can_switch_source(self) -> bool:
        return len(sources.source_cycle(self.args)) > 1

    def _args_with_demo(self, demo: bool) -> argparse.Namespace:
        args = copy.copy(self.args)
        args.demo = demo
        return args

    def next_source_name(self) -> str:
        # Display name of the source `c` would switch to (for the footer).
        order = sources.source_cycle(self.args)
        cur = self.source_key if self.source_key in order else order[0]
        nxt = order[(order.index(cur) + 1) % len(order)]
        return SOURCE_LABELS.get(nxt, nxt)

    def source_menu_entries(self) -> list[tuple[str, str, bool]]:
        # (key, display label, is-active) per switchable source, in cycle order.
        out = []
        for skey in sources.source_cycle(self.args):
            label = "All sources (merged)" if skey == "all" else SOURCE_LABELS.get(skey, skey)
            out.append((skey, label, skey == self.source_key))
        return out

    def open_source_menu(self) -> None:
        # `c` no longer cycles blindly; it opens a small picker the user can j/k through
        # and Enter to switch (Esc cancels). With a single source there's nothing to pick.
        order = sources.source_cycle(self.args)
        if len(order) < 2:
            self.notify("only one data source available", "error")
            return
        cur = self.source_key if self.source_key in order else order[0]
        self.source_menu_index = order.index(cur)
        self.source_menu = True

    def cycle_source(self, step: int = 1) -> None:
        # Relative hop (kept for completeness); the menu uses select_source directly.
        order = sources.source_cycle(self.args)
        if len(order) < 2:
            self.notify("only one data source available", "error")
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
        cache_key = (key, bool(getattr(self.store, "demo", False)))
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
        self.notice = f"source: {SOURCE_LABELS.get(key, key)}"

    def toggle_demo(self) -> None:
        if not self.source_key:
            self.notify("demo toggle unavailable", "error")
            return
        snapshot = self.ui_snapshot()
        demo = not bool(getattr(self.store, "demo", False))
        cache_key = (self.source_key, demo)
        if cache_key not in self._store_cache:
            try:
                self._store_cache[cache_key] = sources.make_store(
                    self._args_with_demo(demo), self.source_key
                )[0]
            except SystemExit as exc:
                self.notify(str(exc), "error")
                return
        self.store = self._store_cache[cache_key]
        self._reload_for_source(snapshot)
        self.notice = "demo mode" if demo else "real data"

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
        # Re-seed every per-store cache from the newly active backend and reset the
        # view to the top -- the months/projects/sessions are a different dataset now.
        self.loaded = self.store.workflows()
        self._snapshot_real_costs()
        self._resolve_project_roots()
        self._models_loaded = False
        self._tool_by_session.clear()
        self._turns_by_session.clear()
        self._load_model_cache()
        self._invalidate_workflow_cache()
        if restore:
            self.browse_mode = restore["browse_mode"]
            self.focus = restore["focus"]
            self.view = restore["view"]
            zoom_project = restore["zoom_project"]
            self.zoom_project = (
                zoom_project
                if zoom_project
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
            if self.view == "session" and not self.current_session():
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
        self.notice = f"source: {self.store.source_name}"

    # --- Export / clipboard / open -------------------------------------------
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
            ]
            for w in sessions
        ]
        return "sessions", header, rows

    @staticmethod
    def _projects_dataset(projects: list[ProjectSummary]) -> tuple[str, list[str], list[list]]:
        header = ["directory", "cost", "tokens", "sessions", "subagents", "unpriced_tokens"]
        rows = [
            [p.directory, p.cost, p.tokens, p.workflows, p.subagents, p.unpriced_tokens]
            for p in projects
        ]
        return "projects", header, rows

    def _active_tab(self) -> str:
        tabs = self.current_tabs()
        return tabs[self.tab % len(tabs)] if tabs else ""

    def _export_dataset(self) -> tuple[str, list[str], list[list]]:
        # Export whatever panel is active (the orange-bordered list/tab), at full
        # precision and honouring the live $ price mode -- so `e` always saves exactly
        # what you're looking at.
        if self.show_prices:  # the P overlay sits on top of any view -- export its table
            return self._prices_dataset()
        if self.view == "session":
            return self._session_tab_dataset()
        if self.view == "zoom":
            return self._zoom_tab_dataset()
        if self.browse_mode == "projects":
            return self._projects_dataset(self.projects)
        # Time browse: the focused left list (years / months / days) is the active panel.
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

    def price_token_mix(self) -> tuple[tuple[float, float, float, float], int] | None:
        # Your app-wide token mix -- (input, output, cache-read, cache-write) shares
        # over every non-local model row, plus the tokens they cover. This is what
        # the P overlay's eff column prices at each model's list rates: with a
        # cache-heavy mix the cache-read rate dominates, which four raw price
        # columns can't show. Reasoning bills as output, so it folds in there; a
        # row without an input split (older stores, tests) puts the total's
        # remainder on input. None until the model scan has usage to measure.
        sums = [0.0, 0.0, 0.0, 0.0]
        for rows in self._model_by_root.values():
            for m in rows:
                name = m.get("model_name")
                if not name or is_local_provider(name):
                    continue
                out = float(m.get("output") or 0) + float(m.get("reasoning") or 0)
                cr = float(m.get("cache_read") or 0)
                cw = float(m.get("cache_write") or 0)
                inp = m.get("input")
                if inp is None:
                    inp = max(0.0, float(m.get("tokens_total") or 0) - out - cr - cw)
                sums[0] += float(inp)
                sums[1] += out
                sums[2] += cr
                sums[3] += cw
        total = sums[0] + sums[1] + sums[2] + sums[3]
        if total <= 0:
            return None
        return (sums[0] / total, sums[1] / total, sums[2] / total, sums[3] / total), int(total)

    @staticmethod
    def _best_alias_price(aliases: dict[str, float]) -> tuple[float, float, float, float]:
        # One list price for a canonical row: alias spellings can resolve differently
        # (a date-pinned id often reaches a cache entry with no cache rates while its
        # plain alias hits the complete embedded price), so try each alias *and* its
        # suffix-stripped spelling and take the most completely priced, ties to the
        # most-used alias.
        best, best_key = (0.0, 0.0, 0.0, 0.0), (-1, -1.0)
        for alias, tok in aliases.items():
            for candidate in {alias, display_model(alias)}:
                p = model_price(candidate)
                key = (sum(1 for v in p if v > 0), tok)
                if key > best_key:
                    best, best_key = tuple(p), key
        return best

    def priced_model_entries(self) -> list[PriceEntry]:
        # The P overlay's rows for the active view (prices_view). Every model you've
        # used, local excluded (no API rate; the P overlay is the list-price reference
        # behind "$", and local usage still shows in Models/Trends). In the "family"
        # and "flat" views a row is a distinct model deduped to its canonical id
        # (alias spellings/date pins/effort suffixes fold together -- the list price
        # is route- and spelling-independent), carrying the route(s) it was reached
        # through; in the "provider" view a row is one (route, model) pair grouped by
        # route, so a model can appear under more than one gateway. Each row carries
        # its usage share and the eff $/M blend of the app-wide mix. Narrowed by the
        # active filter (a plain case-insensitive substring over the model, family,
        # or route), then ordered by _order_price_entries. Shared with the `e` export.
        mix = self.price_token_mix()
        shares = mix[0] if mix else (1.0, 0.0, 0.0, 0.0)
        by_route = self.prices_view == "provider"
        raw: dict[tuple[str, str], dict] = {}
        grand = 0.0
        for rows in self._model_by_root.values():
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
            entries.append(
                PriceEntry(
                    bare=display_model(max(d["aliases"], key=d["aliases"].get)),
                    canon=canon,
                    family=model_family(canon),
                    routes=tuple(sorted(d["routes"])),
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
                )
            )
        if self.query:
            q = self.query.lower()
            entries = [
                e
                for e in entries
                if q in e.bare.lower()
                or q in family_label(e.family).lower()
                or any(q in r.lower() for r in e.routes)
            ]
        return self._order_price_entries(entries)

    def _order_price_entries(self, entries: list[PriceEntry]) -> list[PriceEntry]:
        # Order the entries for the active view. "flat" is one globally-sorted list;
        # the grouped views order groups most-spend-first (the empty group -- Other, or
        # a route-less id -- always last) and apply the active column sort *within* each.
        if self.prices_view == "flat":
            return self._sort_price_entries(entries)
        group_spend: dict[str, float] = defaultdict(float)
        for e in entries:
            group_spend[e.group] += e.spend
        groups = sorted(
            {e.group for e in entries},
            key=lambda g: (g == "", -group_spend[g]),  # empty group last, else most spend
        )
        out: list[PriceEntry] = []
        for g in groups:
            out.extend(self._sort_price_entries([e for e in entries if e.group == g]))
        return out

    def _sort_price_entries(self, entries: list[PriceEntry]) -> list[PriceEntry]:
        # Order price entries by the active prices_sort (cheapest eff first by
        # default); spend-descending is the stable tiebreak under every column so
        # equal values keep a sensible order (the identically-priced Opus versions
        # line up most-used first).
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
        # The bare model ids in display order -- parallel to priced_model_entries (so
        # prices_index selects the same row). Kept for the row count, the Enter
        # drill-in (which then aggregates that bare model's sessions), and the export.
        return [e.bare for e in self.priced_model_entries()]

    def price_model_sessions(self, bare_model: str) -> list[tuple[Workflow, float, int]]:
        # Root sessions that used the model `bare_model`, matched by canonical id so
        # every access route (anthropic, github-copilot, …) *and* alias spelling
        # (dots/dashes, date pins, effort suffixes) is aggregated -- one row per
        # session with that model's cost/tokens within it (cost already reflects the
        # active $ mode). Most spend first. Backs the P overlay's per-model drill-in.
        target = canonical_model(bare_model)
        by_id = {w.id: w for w in self.loaded}
        per_root: dict[str, list] = {}
        for root_id, models in self._model_by_root.items():
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
        # The P overlay's model price table (per 1M tokens), filter included. One row
        # per distinct model (deduped to the canonical id), with its vendor family,
        # access routes, usage share, and the eff $/M blend of your token mix
        # (eff_approx flags a missing cache-read rate billed at the input rate);
        # every row has a real API rate (local models are excluded).
        header = [
            "model",
            "family",
            "routes",
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
        if tab == "Projects":
            return self._projects_dataset(self.zoom_projects())
        if tab == "Models":
            return self._models_dataset(self.aggregate_models(self._active_scope_workflows()))
        if tab == "Sources":
            return self._sources_dataset(self._active_scope_workflows())
        # Overview / Sessions both sit over the same scoped session list.
        return self._sessions_dataset(self.current_sessions())

    def _active_scope_workflows(self) -> list[Workflow]:
        # The sessions the active zoom detail summarises (for a Models/Sources export).
        if self.browse_mode == "projects":
            project = self.selected_project_summary
            return (
                self.workflows_for_project(project.directory, include_ignored=True)
                if project
                else []
            )
        return self.zoom_scope_workflows()

    @staticmethod
    def _sources_dataset(workflows: list[Workflow]) -> tuple[str, list[str], list[list]]:
        # Spend grouped by the tool it came from, mirroring the Sources tab's rollup.
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

    def _session_tab_dataset(self) -> tuple[str, list[str], list[list]]:
        session = self.current_session()
        if session is None:
            return "subagents", ["depth", "agent", "model", "cost", "tokens", "title"], []
        tab = self._active_tab()
        if tab == "Subagents":
            return self._subagents_dataset(session)
        if tab == "Turns":
            return self._turns_dataset(session)
        if tab == "Tools":
            return self._tools_dataset(session)
        # Models tab, and the Overview fallback (whose main table is the model mix).
        return self._models_dataset([(r["model_name"], r) for r in self.model_mix(session.id)])

    @staticmethod
    def _models_dataset(rows: list) -> tuple[str, list[str], list[list]]:
        # rows: list of (name, item) where item carries runs/cost/tokens/cache/output --
        # the shape both aggregate_models (scope) and model_mix (one session) produce.
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
            [r for r in self.store.workflow_nodes(session.id) if r["depth"] > 0]
        )
        header = ["depth", "agent", "model", "cost", "tokens", "title"]
        rows = [
            [r["depth"], r["agent"], r["model_name"], r["cost"], r["tokens_total"], r["title"]]
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
                writer.writerows(rows)
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
        # the tmux launch paths pass the directory separately (-c/-d flags).
        cli = RESUME_COMMANDS.get(workflow.source)
        directory = workflow.directory
        if not cli or not directory or directory == "(unknown)":
            return None
        return directory, f"{cli} {shlex.quote(workflow.id)}"

    def resume_command(self, workflow: Workflow) -> str | None:
        # The ready-to-paste shell form: cd to the project, then resume.
        parts = self.resume_parts(workflow)
        if not parts:
            return None
        directory, command = parts
        return f"cd {shlex.quote(directory)} && {command}"

    def launch_available(self) -> bool:
        # The spawn targets can only land next to opentab from inside tmux (its
        # window/split/popup commands) or through a user launcher hook (which can
        # drive zellij/kitty/etc. anywhere). Outside both the `L` menu still opens,
        # but only offers copying the resume command (see launch_targets).
        return in_tmux() or launcher_hook() is not None

    def launch_targets(self) -> tuple[tuple[str, str, str], ...]:
        # The picker rows actually offered here: everything inside tmux (or with a
        # launcher hook); only the clipboard copy outside — copying needs neither.
        if self.launch_available():
            return self.LAUNCH_TARGETS
        return tuple(t for t in self.LAUNCH_TARGETS if t[1] == "copy")

    def launch_current(self) -> None:
        # `L`: open the launch menu (window/split/popup/copy — handled by
        # handle_launch_key on the next keystroke). Outside tmux/hook the menu
        # narrows to the copy target instead of disappearing (launch_targets).
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

    def handle_launch_key(self, key: int) -> bool:
        # The `L` launch picker: j/k move, Enter runs the highlighted target, the w/s/v/p/y
        # shortcuts jump straight to one, Esc/q cancels. Mirrors handle_source_menu_key.
        if key == 3:  # Ctrl-C still quits
            return False
        targets = self.launch_targets()
        n = len(targets)
        if key in (ord("j"), curses.KEY_DOWN):
            self.launch_menu_index = (self.launch_menu_index + 1) % n
            return True
        if key in (ord("k"), curses.KEY_UP):
            self.launch_menu_index = (self.launch_menu_index - 1) % n
            return True
        if key in (27, ord("q"), curses.KEY_BACKSPACE, 127):
            self.launch_menu = None
            self.notice = "launch cancelled"
            return True
        shortcuts = {ord(t[0]): i for i, t in enumerate(targets)}
        if key in shortcuts:
            index = shortcuts[key]
        elif key in (10, 13, curses.KEY_ENTER):
            index = self.launch_menu_index % n
        else:
            return True  # ignore unknown keys, keep the modal open
        session, self.launch_menu = self.launch_menu, None
        self._do_launch(session, targets[index][1])
        return True

    def _do_launch(self, session: Workflow, kind: str) -> None:
        if kind == "copy":
            self.copy_resume_command(session)
            return
        parts = self.resume_parts(session)
        if not parts:
            self.notice = "launch cancelled"
            return
        directory, command = parts
        error = util.tmux_launch(kind, directory, command)
        if error:
            self.notify(f"launch failed: {error}", "error")
        else:
            self.notice = f"{kind}: {shorten(command, 50)}"

    def handle_source_menu_key(self, key: int) -> bool:
        # The `c` data-source picker: j/k move, Enter switches, Esc/q cancels. `c` again
        # advances the highlight so repeated taps still walk the list.
        order = sources.source_cycle(self.args)
        if not order:
            self.source_menu = False
            return True
        if key == 3:  # Ctrl-C still quits
            return False
        if key in (ord("j"), curses.KEY_DOWN, ord("c")):
            self.source_menu_index = (self.source_menu_index + 1) % len(order)
        elif key in (ord("k"), curses.KEY_UP):
            self.source_menu_index = (self.source_menu_index - 1) % len(order)
        elif key == ord("g"):
            self.source_menu_index = 0
        elif key == ord("G"):
            self.source_menu_index = len(order) - 1
        elif key in (10, 13, curses.KEY_ENTER):
            self.source_menu = False
            self.select_source(order[self.source_menu_index % len(order)])
        elif key in (27, curses.KEY_BACKSPACE, 127, ord("q")):
            self.source_menu = False  # cancel, source unchanged
        # any other key: ignore and keep the menu open
        return True

    def sorted_workflows(self, rows: list[Workflow]) -> list[Workflow]:
        sort_by = self.sort_by if self.sort_by in self.sort_options else self.sort_options[0]
        desc = self.sort_descending(sort_by, self.sort_reverse)
        if sort_by == "cost":
            return sorted(rows, key=lambda item: (item.total_cost, item.total_tokens), reverse=desc)
        if sort_by == "tokens":
            return sorted(rows, key=lambda item: (item.total_tokens, item.total_cost), reverse=desc)
        if sort_by == "subagents":
            return sorted(rows, key=lambda item: (item.subagents, item.total_tokens), reverse=desc)
        if sort_by == "title":
            return sorted(rows, key=lambda item: item.title.lower(), reverse=desc)
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
        # Projects active within the zoomed month/day — the navigable Projects tab.
        return self.projects_for_workflows(
            self.zoom_scope_workflows(include_ignored=self.show_ignored_projects),
            include_ignored=self.show_ignored_projects,
        )

    def zoom_selected_project(self) -> ProjectSummary | None:
        rows = self.zoom_projects()
        if not rows:
            return None
        self.project_index = max(0, min(self.project_index, len(rows) - 1))
        return rows[self.project_index]

    def current_sessions(self) -> list[Workflow]:
        if self.browse_mode == "projects":
            item = self.selected_project_summary
            rows = self.workflows_for_project(item.directory, include_ignored=True) if item else []
        elif self.focus == "years":
            item = self.selected_year_summary
            source = self.ranged_workflows if self._zooming_ignored_project() else None
            rows = self.workflows_for_year(item.year, source) if item else []
        elif self.focus == "months":
            item = self.selected_month_summary
            source = self.ranged_workflows if self._zooming_ignored_project() else None
            rows = self.workflows_for_month(item.month, source) if item else []
        else:
            item = self.selected_day_summary
            source = self.ranged_workflows if self._zooming_ignored_project() else None
            rows = self.workflows_for_day(item.day, source) if item else []
        if self.zoom_project and self.browse_mode != "projects":
            rows = [w for w in rows if self.project_root(w.directory) == self.zoom_project]
        return self.filtered_sessions(rows)

    def _zooming_ignored_project(self) -> bool:
        return bool(
            self.show_ignored_projects
            and self.zoom_project
            and self.zoom_project in self.ignored_projects
            and self.browse_mode != "projects"
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

    def in_project_sort_context(self) -> bool:
        return (self.view == "browse" and self.browse_mode == "projects") or (
            self.view != "session" and self.on_projects_tab
        )

    def in_prices_sort_context(self) -> bool:
        # The P overlay's model list (not its per-model session drill-in) is sortable
        # by column, so it gets its own sort state (prices_sort/prices_sort_reverse).
        return self.show_prices and self.prices_model is None

    def can_sort_current_view(self) -> bool:
        return (
            self.in_prices_sort_context()
            or self.in_project_sort_context()
            or (self.view != "session" and self.on_sessions_tab)
            or (self.view == "session" and self.on_subagents_tab)
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
        if self.in_prices_sort_context():
            return self.prices_sort  # always a column ("eff" by default), so it arrows
        if self.in_project_sort_context():
            return (
                self.project_sort_by
                if self.project_sort_by in self.project_sort_options
                else self.project_sort_options[0]
            )
        options = self.current_sort_options()
        if not options:
            return None
        return self.sort_by if self.sort_by in options else options[0]

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
        if self.in_project_sort_context():
            return self.project_sort_options
        return self.current_sort_options()

    def open_sort_menu(self) -> None:
        # `s` no longer cycles blindly; it opens a small picker the user can j/k
        # through and Enter to apply (Esc cancels), mirroring the `c` source menu.
        if not self.can_sort_current_view():
            self.notify("sort: only session, project, or subagent lists", "error")
            return
        options = self.sort_menu_options()
        if not options:
            self.notify("sort: only session, project, or subagent lists", "error")
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
        if self.in_project_sort_context():
            self.project_sort_by = value
            self.project_sort_reverse = False
            self.project_index = 0
        else:
            self.sort_by = value
            self.sort_reverse = False
            self.workflow_index = 0
        self.scroll = 0

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
            if key not in self.sort_options:
                return
            if self.sort_by == key:
                self.sort_reverse = not self.sort_reverse
            else:
                self.sort_by = key
                self.sort_reverse = False
            self.workflow_index = 0
        self.scroll = 0

    def handle_sort_menu_key(self, key: int) -> bool:
        # The `s` sort picker: j/k move, Enter applies, Esc/q cancels. `s` again
        # advances the highlight so repeated taps still walk the list. Mirrors
        # handle_source_menu_key.
        options = self.sort_menu_options()
        if not options:
            self.sort_menu = False
            return True
        if key == 3:  # Ctrl-C still quits
            return False
        if key in (ord("j"), curses.KEY_DOWN, ord("s")):
            self.sort_menu_index = (self.sort_menu_index + 1) % len(options)
        elif key in (ord("k"), curses.KEY_UP):
            self.sort_menu_index = (self.sort_menu_index - 1) % len(options)
        elif key == ord("g"):
            self.sort_menu_index = 0
        elif key == ord("G"):
            self.sort_menu_index = len(options) - 1
        elif key in (10, 13, curses.KEY_ENTER):
            self.sort_menu = False
            self.apply_sort_choice(options[self.sort_menu_index % len(options)])
        elif key in (27, curses.KEY_BACKSPACE, 127, ord("q")):
            self.sort_menu = False  # cancel, order unchanged
        # any other key: ignore and keep the menu open
        return True

    FOCUS_CYCLE = ("years", "months", "days")

    def cycle_focus(self, step: int = 1) -> None:
        # Tab walks the three stacked time panels (Years -> Months -> Days); Shift-Tab
        # walks back. No-op in session view and projects mode (no left-panel focus).
        if self.view == "session" or self.browse_mode == "projects":
            return
        # Keep the same detail tab across the switch (Models stays on Models). Carry it
        # by name since the levels differ; fall back to Overview when the target level
        # lacks it (e.g. Days has no Models tab).
        tabs = self.current_tabs()
        active_tab = tabs[self.tab % len(tabs)]
        i = self.FOCUS_CYCLE.index(self.focus) if self.focus in self.FOCUS_CYCLE else 1
        self.focus = self.FOCUS_CYCLE[(i + step) % len(self.FOCUS_CYCLE)]
        new_tabs = self.current_tabs()
        self.tab = new_tabs.index(active_tab) if active_tab in new_tabs else 0
        self.scroll = 0
        self.zoom_project = None

    def toggle_focus(self) -> None:
        self.cycle_focus(1)

    def set_browse_mode(self, mode: str) -> None:
        if self.view == "session":
            return
        if mode == self.browse_mode:
            return
        self.browse_mode = mode
        self.view = "browse"
        self.tab = 0
        self.scroll = 0
        self.workflow_index = 0
        self.zoom_project = None

    def drill_in(self) -> None:
        if self.view == "browse":
            item = (
                self.selected_project_summary
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
                self._cal_return = None  # a fresh drill; the calendar re-sets this if it began one
                if self.browse_mode != "projects":
                    # In time mode, project_index is only the zoom Projects-tab
                    # picker; reset it. In projects mode it is the selected project
                    # we are drilling into, so it must be left alone.
                    self.project_index = 0
        elif self.view == "zoom" and self.on_projects_tab and self.browse_mode != "projects":
            # Pick a project in a month/day zoom -> view its sessions in this scope.
            project = self.zoom_selected_project()
            if project is not None:
                self.zoom_project = project.directory
                tabs = self.current_tabs()
                if "Sessions" in tabs:
                    self.tab = tabs.index("Sessions")
                self.workflow_index = 0
                self.scroll = 0
        elif self.view == "zoom" and self.on_sessions_tab and self.current_session():
            self.view = "session"
            self.tab = 0
            self.scroll = 0

    def drill_out(self) -> None:
        if self.view == "session":
            self.view = "zoom"
            tabs = self.current_tabs()  # land back on the Sessions tab we came from
            self.tab = tabs.index("Sessions") if "Sessions" in tabs else 0
        elif self.view == "zoom":
            if self.zoom_project and self.browse_mode != "projects":
                # Leave a project's sessions, back to the Projects list of this zoom.
                self.zoom_project = None
                tabs = self.current_tabs()
                self.tab = tabs.index("Projects") if "Projects" in tabs else 0
            else:
                self.view = "browse"
                self.zoom_project = None
                if self._cal_return is not None:
                    self._reopen_calendar(self._cal_return)
        self.scroll = 0

    def _reopen_calendar(self, date: str) -> None:
        # Stepping out of a day we drilled into from the heat map returns to it: reopen
        # the Calendar tab on that year, cursor back on the day we came from.
        self._cal_return = None
        years = self.calendar_years()
        yi = next((i for i, y in enumerate(years) if y == date[:4]), None)
        if yi is None:
            return  # the day's year is gone (range/source changed) — just stay in browse
        self.trends = True
        self.trend_tab = self.trend_tabs.index("Calendar")
        self.trend_year_index = yi
        self.cal_cursor = date
        self.cal_focus = True  # we drilled in from a focused grid; resume there

    def move(self, delta: int) -> None:
        if self.view == "session":
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
            else:
                self.scroll = max(0, self.scroll + delta)
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

    def jump(self, to_end: bool, stdscr: curses.window | None = None) -> None:
        if self.view == "browse":
            if self.browse_mode == "projects":
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
    def notice(self) -> str:
        toasts = self.toasts
        return toasts[-1].text if toasts else ""

    @notice.setter
    def notice(self, value: str) -> None:
        if value:
            self.notify(value)
        else:
            self.toasts.clear()  # `self.notice = ""` means "no message"

    def toast_now(self) -> float:
        return self._toast_clock()

    def notify(self, text: str, kind: str = "info") -> None:
        toasts = self.toasts
        if not text:
            toasts.clear()
            return
        toast = Toast(text, kind, self.toast_now(), self.TOAST_TTL)
        # Several notices set within one input handler (e.g. "fetching…" → "refreshed")
        # never get a frame between them, so collapse onto one toast; distinct user
        # actions (a paint happened in between) stack instead.
        if toasts and not self._toast_shown:
            toasts[-1] = toast
        else:
            toasts.append(toast)
            del toasts[: max(0, len(toasts) - self.TOAST_MAX)]
        self._toast_shown = False

    def active_toasts(self) -> list[Toast]:
        # Drop expired toasts (in place) and return what's still on screen.
        now = self.toast_now()
        self.toasts[:] = [t for t in self.toasts if t.remaining(now) > 0]
        return self.toasts

    def _mark_toasts_shown(self) -> None:
        self._toast_shown = True

    def _input_timeout_ms(self) -> int:
        # Block on input when nothing is showing; poll while a toast is fading so it
        # can expire on time without a keystroke.
        return self.TOAST_POLL_MS if self.toasts else -1

    def run(self, stdscr: curses.window) -> None:
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        curses.curs_set(0)
        curses.use_default_colors()
        # One restrained palette instead of a full ANSI rainbow: a grey baseline,
        # warm amber + orange accents for focus/brand, green for money, soft red
        # reserved for genuine alerts. 256-color where available, 8-color fallback.
        has256 = curses.COLORS >= 256
        self.has256 = has256  # draw_calendar regenerates its ramp against this
        grey = 245 if has256 else curses.COLOR_WHITE
        amber = 214 if has256 else curses.COLOR_YELLOW
        slate = 67 if has256 else curses.COLOR_BLUE
        green = 35 if has256 else curses.COLOR_GREEN
        soft_red = 203 if has256 else curses.COLOR_RED
        accent = 208 if has256 else curses.COLOR_YELLOW
        curses.init_pair(1, grey, -1)  # secondary: hints, breadcrumb, bars, small tokens
        curses.init_pair(2, amber, -1)  # warm accent: title, drilldown chip, M-tokens
        curses.init_pair(3, green, -1)  # money
        curses.init_pair(4, slate, -1)  # structural: headers, keybar, markdown '#'
        curses.init_pair(5, soft_red, -1)  # alerts only: errors, billions of tokens
        curses.init_pair(6, accent, -1)  # active panel border / focus
        curses.init_pair(7, curses.COLOR_BLACK, accent)  # active tab
        # Heat-map ramp (pairs 8..8+HEAT_MAX_LEVELS-1) for the Calendar tab: a green→red
        # gradient where a hotter shade means a heavier spend day. The granularity is
        # live (+/- on the tab), so draw_calendar re-inits these per frame to match
        # self.cal_levels; seed the current count here so the pairs exist beforehand.
        for i, col in enumerate(heat_palette(self.cal_levels, has256)):
            curses.init_pair(8 + i, col, -1)
        # The P overlay's price-heat ramp: same green→red palette on its own fixed
        # pair block (never rescaled by +/-), so cheap/expensive rates read at a glance.
        for i, col in enumerate(heat_palette(PRICE_HEAT_LEVELS, has256)):
            curses.init_pair(PRICE_HEAT_BASE_PAIR + i, col, -1)
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
            self.active_toasts()  # expire faded toasts before painting
            self.renderer.draw(stdscr)
            self._mark_toasts_shown()
            if first:
                # First frame is up off the fast session rollup; now do the one
                # heavy message scan, then repaint so model_count / Models tabs are
                # populated before the user's first keystroke is handled.
                first = False
                self._ensure_models()
                self.maybe_prompt_prices()  # offer a models.dev fetch if prices are missing
                self.renderer.draw(stdscr)
                self._mark_toasts_shown()
            stdscr.timeout(self._input_timeout_ms())
            key = stdscr.getch()
            if key == -1:
                continue  # idle wake while a toast fades: just re-expire and repaint
            if not self.handle_key(stdscr, key):
                break

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

    def _calendar_key(self, key: int) -> bool:
        # +/- tune the heat-map granularity live (more shades = finer spend bands);
        # arrow keys walk the day cursor (←/→ = ∓1 week, ↑/↓ = ∓1 day, clamped to the
        # shown year); Enter drills into the highlighted day.
        if key in (ord("+"), ord("=")):
            self.cal_levels = min(HEAT_MAX_LEVELS, self.cal_levels + 1)
            return True
        if key in (ord("-"), ord("_")):
            self.cal_levels = max(HEAT_MIN_LEVELS, self.cal_levels - 1)
            return True
        cursor = self.calendar_cursor()
        if cursor is None:
            return True
        if key in (10, 13, curses.KEY_ENTER):
            if self.drill_into_date(cursor):
                self._cal_return = cursor  # Esc out of the day returns to the heat map
                self.trends = False
            else:
                self.notify(f"no sessions on {cursor}", "error")
            return True
        delta = {curses.KEY_LEFT: -7, curses.KEY_RIGHT: 7, curses.KEY_UP: -1, curses.KEY_DOWN: 1}
        nxt = datetime.strptime(cursor, "%Y-%m-%d") + timedelta(days=delta[key])
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

    def handle_key(self, stdscr: curses.window, key: int) -> bool:
        if key == curses.KEY_MOUSE:
            return self.handle_mouse()
        if key == curses.KEY_RESIZE:
            # A SIGWINCH (terminal/font resize) surfaces as a keystroke; it is not one.
            # The next paint reads getmaxyx() fresh, so just swallow it -- otherwise it
            # falls through to an overlay's "any other key closes" path and shuts it.
            return True
        if self.price_prompt:
            return self.handle_price_prompt_key(key)
        if self.help:
            # A pager like the price overlay: j/k/arrows and g/G scroll;
            # any other key closes it.
            if key in (ord("j"), curses.KEY_DOWN):
                self.help_scroll += 1
            elif key in (ord("k"), curses.KEY_UP):
                self.help_scroll = max(0, self.help_scroll - 1)
            elif key == ord("g"):
                self.help_scroll = 0
            elif key == ord("G"):
                self.help_scroll = 10_000  # clamped to the last page on draw
            else:
                self.help = False
            return True
        if self.show_prices:
            if self.sort_menu:  # the `s` picker floats over the price table
                return self.handle_sort_menu_key(key)
            if self.filter_active:
                return self.handle_filter_key(key)
            if self.prices_model is not None:
                return self._handle_price_sessions_key(key)
            return self._handle_price_models_key(key)
        if self.trends:
            current = self.trend_tabs[self.trend_tab % len(self.trend_tabs)]
            if current == "Calendar":
                # The Calendar tab is modal so the arrows aren't trapped: until you
                # focus the grid, arrows move between tabs like every other tab. Enter
                # focuses it (arrows then walk the day cursor, Enter drills in), and Esc
                # steps back out to tab navigation instead of closing the whole overlay.
                if self.cal_focus:
                    if key == 27:  # Esc -> leave focus, back to tab navigation
                        self.cal_focus = False
                        return True
                    if key in (
                        curses.KEY_LEFT,
                        curses.KEY_RIGHT,
                        curses.KEY_UP,
                        curses.KEY_DOWN,
                        10,
                        13,
                        curses.KEY_ENTER,
                    ):
                        return self._calendar_key(key)
                elif key in (10, 13, curses.KEY_ENTER):
                    self.cal_focus = True  # focus the grid; arrows now pick days
                    return True
                if key in (ord("+"), ord("="), ord("-"), ord("_")):
                    return self._calendar_key(key)  # +/- tune shades in either mode
            if key in (ord("h"), curses.KEY_LEFT):
                self.trend_tab = (self.trend_tab - 1) % len(self.trend_tabs)
                self.cal_focus = False
            elif key in (ord("l"), curses.KEY_RIGHT):
                self.trend_tab = (self.trend_tab + 1) % len(self.trend_tabs)
                self.cal_focus = False
            elif key in (ord("j"), curses.KEY_DOWN, ord("["), ord("k"), curses.KEY_UP, ord("]")):
                # Page the Daily tab's month / the Weekly tab's week / the Calendar's year.
                older = key in (ord("j"), curses.KEY_DOWN, ord("["))
                if current == "Daily":
                    n = len(
                        {w.created_at[:7] for w in self.all_workflows if week_key(w.created_at)}
                    )
                    self.trend_month_index = self._step_trend_index(
                        self.trend_month_index, n, older
                    )
                elif current == "Weekly":
                    n = len({k for w in self.all_workflows if (k := week_key(w.created_at))})
                    self.trend_week_index = self._step_trend_index(self.trend_week_index, n, older)
                elif current == "Calendar":
                    n = len(self.calendar_years())
                    self.trend_year_index = self._step_trend_index(self.trend_year_index, n, older)
                    self.cal_cursor = None  # re-anchor the cursor on the new year's peak
            elif key == ord("$"):
                self.toggle_api_prices()  # re-prices the charts in place, stays open
            else:
                self.trends = False  # any other key closes the overlay
            return True

        if self.source_menu:
            return self.handle_source_menu_key(key)
        if self.sort_menu:
            return self.handle_sort_menu_key(key)
        if self.filter_active:
            return self.handle_filter_key(key)
        if self.launch_menu is not None:
            return self.handle_launch_key(key)

        if key in (ord("q"), 3):
            return False
        if key == ord("?"):
            self.help = True
            self.help_scroll = 0
            return True
        if key == ord("T"):
            self.trends = True
            self.trend_month_index = 0  # start at the most recent month
            self.trend_week_index = 0  # and the most recent week
            self.trend_year_index = 0  # and the most recent year
            self.cal_cursor = None  # Calendar cursor defaults to that year's peak day
            self.cal_focus = False  # land on the Calendar tab unfocused (arrows pick tabs)
            return True
        if key == ord("P"):
            self.show_prices = True
            self.prices_scroll = 0
            self.prices_index = 0
            self.prices_model = None
            return True
        if key == ord("r"):
            self.reload()
            return True
        if key == ord("c"):
            self.open_source_menu()
            return True
        if key == ord("D"):
            self.toggle_demo()
            return True
        if key == ord("+"):
            self.drill_in()
            return True
        if key == ord("a"):
            self.set_all_time()
            return True
        if key == ord("R"):
            self.prompt_range(stdscr)
            return True
        if key == ord("p"):
            self.set_browse_mode("projects")
            return True
        if key == ord("t"):
            self.set_browse_mode("time")
            return True
        if key in (ord("s"), ord("S")):
            self.open_sort_menu()
            return True
        if key == ord("i"):
            self.toggle_project_ignore()
            return True
        if key == ord("I"):
            self.toggle_ignored_projects_view()
            return True
        if key == ord("b"):
            self.toggle_bookmark()
            return True
        if key == ord("B"):
            self.toggle_bookmarks_view()
            return True
        if key == ord("f"):
            if not self.can_filter_current_view():
                self.notify(
                    "nothing to filter here — open a sessions, projects, or models list", "error"
                )
                return True
            self.filter_active = True
            self._filter_before = self.query
            return True
        if key == ord("x"):
            if self.query:
                self.query = ""
                self.workflow_index = 0
                self.project_index = 0
                self.notice = "filter cleared"
            else:
                self.notify("no active filter", "error")
            return True
        if key == ord("e"):
            self.export_current()
            return True
        if key == ord("o"):
            self.open_current()
            return True
        if key == ord("L"):
            self.launch_current()
            return True
        if key == ord("$"):
            self.toggle_api_prices()
            return True
        if key == ord("\t"):
            self.cycle_focus(1)
            return True
        if key in (10, 13, curses.KEY_ENTER):
            self.drill_in()
            return True
        if key in (27, curses.KEY_BACKSPACE, 127):
            self.drill_out()  # session -> zoom -> browse; no-op when browsing
            return True
        if key == curses.KEY_BTAB:
            if self.view == "browse":
                self.cycle_focus(-1)
            else:
                self.drill_out()
            return True
        if key in (ord("h"), curses.KEY_LEFT):
            self.tab = (self.tab - 1) % len(self.current_tabs())
            self.scroll = 0
            return True
        if key in (ord("l"), curses.KEY_RIGHT):
            self.tab = (self.tab + 1) % len(self.current_tabs())
            self.scroll = 0
            return True
        if key == ord("g"):
            self.jump(to_end=False, stdscr=stdscr)
            return True
        if key == ord("G"):
            self.jump(to_end=True, stdscr=stdscr)
            return True
        if key in (ord("j"), curses.KEY_DOWN):
            self.move(1)
            return True
        if key in (ord("k"), curses.KEY_UP):
            self.move(-1)
            return True
        return True

    def prices_view_label(self, view: str | None = None) -> str:
        # The human label for a P-overlay view mode (defaults to the active one).
        view = view or self.prices_view
        return dict(self.prices_views).get(view, view)

    def cycle_prices_view(self) -> None:
        # `p` walks the P overlay's layout modes (flat -> by vendor -> by provider).
        keys = [k for k, _label in self.prices_views]
        i = keys.index(self.prices_view) if self.prices_view in keys else 0
        self.prices_view = keys[(i + 1) % len(keys)]
        self.prices_index = 0  # the row order (and count) changed under the cursor
        self.prices_scroll = 0
        self.notice = f"view: {self.prices_view_label()}"

    def _handle_price_models_key(self, key: int) -> bool:
        # The P overlay's model list: j/k/arrows move a cursor, g/G jump to ends,
        # Enter drills into the selected model's sessions, s sorts by a column, p
        # cycles the layout (by vendor / by provider / flat), f filters, r refreshes,
        # e exports the table; any other key closes the overlay.
        n = len(self.priced_model_names())
        if key in (ord("s"), ord("S")):
            self.open_sort_menu()
            return True
        if key == ord("p"):
            self.cycle_prices_view()
            return True
        if key in (ord("j"), curses.KEY_DOWN):
            self.prices_index = min(self.prices_index + 1, max(0, n - 1))
        elif key in (ord("k"), curses.KEY_UP):
            self.prices_index = max(0, self.prices_index - 1)
        elif key == ord("g"):
            self.prices_index = 0
        elif key == ord("G"):
            self.prices_index = max(0, n - 1)
        elif key in (10, 13, curses.KEY_ENTER):
            names = self.priced_model_names()
            if names:
                self.prices_model = names[max(0, min(self.prices_index, len(names) - 1))]
                self.prices_scroll = 0
        elif key in (ord("r"), ord("R")):
            self.refresh_prices_action()  # keeps the overlay open
        elif key == ord("f"):
            self.filter_active = True
            self._filter_before = self.query
            self.prices_scroll = 0
        elif key == ord("e"):
            self.export_current()  # _export_dataset sees show_prices; overlay stays open
        else:
            self.show_prices = False
        return True

    def _handle_price_sessions_key(self, key: int) -> bool:
        # The P overlay's per-model drill-in: j/k/arrows and g/G scroll the session
        # list; Esc/left/backspace backs out to the model list; any other key closes.
        if key in (ord("j"), curses.KEY_DOWN):
            self.prices_scroll += 1
        elif key in (ord("k"), curses.KEY_UP):
            self.prices_scroll = max(0, self.prices_scroll - 1)
        elif key == ord("g"):
            self.prices_scroll = 0
        elif key == ord("G"):
            self.prices_scroll = 10_000  # clamped to the last page on draw
        elif key in (27, curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8):
            self.prices_model = None  # back to the model list
            self.prices_scroll = 0
        else:
            self.show_prices = False
            self.prices_model = None
        return True

    def handle_filter_key(self, key: int) -> bool:
        # Live fuzzy filter mode (`/`): printable keys edit the query and every
        # list re-ranks on the very next paint. Arrows still move the selection,
        # so you can land on a match without leaving the mode.
        if key == 3:  # Ctrl-C still quits
            return False
        if key == 27:  # Esc restores the query from before `/`
            self.query = self._filter_before
            self.filter_active = False
            self._filter_edited()
            self.notice = "filter cancelled"
        elif key in (10, 13, curses.KEY_ENTER):
            self.filter_active = False
            # The committed query already shows as a persistent orange "filter:" chip in
            # the header, so a "filter: x" notice would just duplicate it -- only the
            # cleared case (no chip) needs a word.
            self.notice = "" if self.query else "filter cleared"
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.query = self.query[:-1]
            self._filter_edited()
        elif key == 21:  # Ctrl-U clears the input
            self.query = ""
            self._filter_edited()
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            if self.show_prices:
                # Arrows move the P model cursor so you can land on a filter match.
                self.prices_index += 1 if key == curses.KEY_DOWN else -1
                self.prices_index = max(0, self.prices_index)
            else:
                self.move(1 if key == curses.KEY_DOWN else -1)
        elif 32 <= key <= 126:
            self.query += chr(key)
            self._filter_edited()
        return True

    def _filter_edited(self) -> None:
        # Selection snaps to the best-ranked match whenever the query changes.
        self.workflow_index = 0
        self.project_index = 0
        self.prices_scroll = 0
        self.prices_index = 0

    def handle_mouse(self) -> bool:
        # The screen's clickable regions were registered by the last draw(), so a
        # click resolves against exactly what the user sees. Wheel scrolls the
        # current context; a click selects; a double-click selects then drills in.
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except curses.error:
            return True
        up = bool(bstate & curses.BUTTON4_PRESSED)
        down = bool(bstate & getattr(self, "_wheel_down", 0))
        click = bool(bstate & curses.BUTTON1_CLICKED)
        double = bool(bstate & curses.BUTTON1_DOUBLE_CLICKED)

        if self.price_prompt:
            if click or double:
                self.price_prompt = False  # click = not now
                self.notice = "skipped — fetch anytime with --refresh-models or r in the P view"
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
            return True
        if self.help:
            if up or down or click or double:
                self.help = False
            return True
        if self.show_prices:
            if self.prices_model is None:
                if up:
                    self.prices_index = max(0, self.prices_index - 1)
                elif down:
                    self.prices_index += 1  # clamped on draw
                elif click or double:
                    # A click on a column header sorts by it (re-click flips); any
                    # other click closes, as before.
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
            self.move(-3 if up else 3)
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
            if current == "Daily":
                n = len({w.created_at[:7] for w in self.all_workflows if week_key(w.created_at)})
                self.trend_month_index = self._step_trend_index(self.trend_month_index, n, older)
            elif current == "Weekly":
                n = len({k for w in self.all_workflows if (k := week_key(w.created_at))})
                self.trend_week_index = self._step_trend_index(self.trend_week_index, n, older)
            elif current == "Calendar":
                n = len(self.calendar_years())
                self.trend_year_index = self._step_trend_index(self.trend_year_index, n, older)
                self.cal_cursor = None  # re-anchor the cursor on the new year's peak
            return True
        if current == "Calendar" and (click or double):
            date = self._calendar_date_at(my, mx)
            if date:
                if not self.cal_focus:
                    # The grid is modal: a click on the sleeping calendar only wakes it
                    # (like Enter). You can't pick or open a day until it's focused, so a
                    # stray click never jumps into a day -- the next click does that.
                    self.cal_focus = True
                    return True
                # Focused: a click moves the day cursor onto that cell, a double-click drills.
                self.cal_cursor = date
                if double:
                    if self.drill_into_date(date):
                        self._cal_return = date  # Esc out of the day returns to the heat map
                        self.trends = False
                    else:
                        self.notify(f"no sessions on {date}", "error")
                return True
        if click or double:
            target = self.renderer.hit(my, mx)
            if target and target[0] == "trend":
                if self.trend_tab != target[1]:
                    self.cal_focus = False  # switching tabs leaves the calendar grid
                self.trend_tab = target[1]
        return True

    def _apply_click(self, target: tuple[str, int], drill: bool) -> None:
        kind, value = target
        if kind == "tab":
            if self.tab != value:
                self.tab = value
                self.scroll = 0
            return
        if kind == "year" and self.view == "browse" and self.browse_mode == "time":
            if self.focus != "years":
                self.focus = "years"
                self.tab = 0
                self.scroll = 0
                self.zoom_project = None
            self.year_index = value
            self.month_index = 0
            self.day_index = 0
        elif kind == "month" and self.view == "browse" and self.browse_mode == "time":
            if self.focus != "months":
                self.focus = "months"
                self.tab = 0
                self.scroll = 0
                self.zoom_project = None
            self.month_index = value
            self.day_index = 0
        elif kind == "day" and self.view == "browse" and self.browse_mode == "time":
            if self.focus != "days":
                self.focus = "days"
                self.tab = 0
                self.scroll = 0
                self.zoom_project = None
            self.day_index = value
        elif kind == "project":
            self.project_index = value
        elif kind == "session":
            self.workflow_index = value
        elif kind == "zoomproject":
            self.project_index = value
        else:
            return
        if drill:
            self.drill_in()

    def prompt_range(self, stdscr: curses.window) -> None:
        initial = "" if self.range_input_value() == "all" else self.range_input_value()
        value = self.prompt_text(
            stdscr,
            "range: ",
            "all · 30d · 2m · 2026 · 2026-05 · start..end · Esc cancel",
            initial,
        )
        if value is None:
            return
        try:
            self.set_range_from_text(value)
        except ValueError as exc:
            self.notify(f"range error: {exc}", "error")

    def prompt_text(
        self, stdscr: curses.window, label: str, hint: str = "", initial: str = ""
    ) -> str | None:
        # Modal bottom command line, laid out exactly like the `/` filter so input
        # never drifts: a short "<label>: " + the value you type is the input field
        # (orange) at the far LEFT, and the format hint sits to its right in plain
        # slate -- never a whole-orange line. The real cursor sits at the value's end.
        height, width = stdscr.getmaxyx()
        head = " " + label
        max_len = max(1, width - len(head) - len(hint) - 6)
        field = curses.color_pair(6) | curses.A_BOLD
        value = initial
        curses.curs_set(1)
        try:
            while True:
                stdscr.addstr(height - 1, 0, " " * (width - 1))
                shown = shorten(value, max_len)
                left = head + shown
                stdscr.addstr(height - 1, 0, left[: width - 1], field)
                hx = len(left)
                if hint and hx < width - 1:  # format hint in plain slate, to the right
                    stdscr.addstr(
                        height - 1, hx, ("   " + hint)[: width - hx - 1], curses.color_pair(4)
                    )
                stdscr.move(height - 1, min(width - 2, len(left)))
                stdscr.refresh()

                value, done, cancelled = self.filter_prompt_step(value, stdscr.getch(), max_len)
                if cancelled:
                    return None
                if done:
                    return value
        finally:
            curses.curs_set(0)

    @staticmethod
    def filter_prompt_step(value: str, key: int, max_len: int) -> tuple[str, bool, bool]:
        if key == 27:  # Esc cancels without changing the current filter.
            return value, False, True
        if key in (10, 13, curses.KEY_ENTER):
            return value, True, False
        if key in (curses.KEY_BACKSPACE, 127, 8):
            return value[:-1], False, False
        if 32 <= key <= 126 and len(value) < max_len:
            return value + chr(key), False, False
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
            return tabs
        if self.browse_mode == "projects":
            base = self.project_tabs
        elif self.focus == "years":
            base = self.year_tabs
        else:
            base = self.month_tabs if self.focus == "months" else self.day_tabs
        # In the merged view a per-source cut is meaningful, so expose it right
        # after Overview. With one backend every row is the same source (a 100%
        # bar), so the tab would be noise -- omit it unless sources are combined.
        if getattr(self.store, "combined", False):
            return base[:1] + ("Sources",) + base[1:]
        return base

    def current_sort_options(self) -> tuple[str, ...]:
        # Left-hand months/days and non-session detail panes are fixed-order;
        # sort only reorders visible session or subagent lists.
        if self.view == "session" and self.on_subagents_tab:
            return self.subagent_sort_options
        if self.view != "session" and self.on_sessions_tab:
            return self.sort_options
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
                )
            out.append(d)
        return out

    def sorted_subagent_rows(self, rows: list) -> list:
        sort_by = (
            self.sort_by
            if self.sort_by in self.subagent_sort_options
            else self.subagent_sort_options[0]
        )
        desc = self.sort_descending(sort_by, self.sort_reverse)
        if sort_by == "tokens":
            return sorted(rows, key=lambda row: (row["tokens_total"], row["cost"]), reverse=desc)
        if sort_by == "title":
            return sorted(rows, key=lambda row: str(row["title"]).lower(), reverse=desc)
        if sort_by == "model":
            return sorted(rows, key=lambda row: str(row["model_name"]).lower(), reverse=desc)
        if sort_by == "agent":
            return sorted(rows, key=lambda row: str(row["agent"]).lower(), reverse=desc)
        if sort_by == "depth":
            return sorted(rows, key=lambda row: (row["depth"], row["tokens_total"]), reverse=desc)
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
