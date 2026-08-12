"""App: state and the keyboard/mouse state machine."""

from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import os
import re
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

from opentab import sources, themes, util
from opentab.demo import (
    DEMO_ALL,
    DEMO_CATEGORIES,
    demo_cost,
    demo_machine,
    demo_model,
    demo_title,
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


class TokenEconomics(NamedTuple):
    """Where a scope's tokens went and where its money went -- the same five token
    types measured twice. The gap between the two is the whole point: a token type's
    share of VOLUME and its share of SPEND differ by up to two orders of magnitude
    (an output token costs 50x a cache-read token at Anthropic's rates), so a plain
    token count says nothing about the bill and vice versa.

    `cost` is always at LIST rates, whatever the "$" toggle says, because no backend
    attributes recorded spend per token type -- there is nothing else to decompose.
    Same basis as the `w` what-if's baseline (whatif_session_totals), and computed
    with api_equivalent_cost's own arithmetic so the five pieces sum to the API-
    equivalent figure the rest of the UI already shows.
    """

    tokens: tuple[float, ...]  # per TOKEN_TYPES
    cost: tuple[float, ...]  # per TOKEN_TYPES, at list rates
    estimated: bool  # a contributing model has no real list rate (FALLBACK_PRICE) -> mark "~"
    missing_cache_rate: bool  # a contributing model has no cache-read rate -> its reads read $0
    local_tokens: int  # tokens from local models, excluded from both rows (no API rate)

    @property
    def total_tokens(self) -> float:
        return sum(self.tokens)

    @property
    def total_cost(self) -> float:
        return sum(self.cost)


# The flamegraph's colour assignment. The root's own work always owns slot 0 of the
# categorical ramp and the subagents cycle the rest, so no child can ever wear the
# root's colour -- the one distinction the chart is actually making. Four slots is a
# long enough cycle that adjacent segments always differ (the band is cost-ordered,
# so a repeat is far from its twin); the legend and the table below carry identity
# past that.
FLAME_SELF_SLOT = 0
FLAME_CHILD_SLOTS = (1, 2, 3, 4)

# Agent names worth putting on a segment. A segment answers "which AGENT, on which
# model" -- not "which session", whose title is a sentence that never fits and is one
# column away in the table below.
_FLAME_DULL_AGENTS = frozenset({"", "-", "subagent", "unknown", "(untitled)"})

# OpenCode records the agent in the `agent` column for only some sessions; for the rest
# it writes "-" and puts the name in the TITLE, as "Review browse mode (@code-reviewer)"
# or "… (@general subagent)". Mining it back out is not a guess about a title's wording,
# it is reading a field the backend stored in the wrong place: on real data it lifts the
# share of subagent nodes that can name their agent from 15% to 85%, and the names it
# recovers (explore, code-reviewer, general, homelab, org, debugger) are exactly the ones
# the `agent` column holds when it is populated.
_FLAME_AGENT_TAG = re.compile(r"\(@([\w.-]+)")


def flame_label(row: dict) -> str:
    # A segment's name: the agent that ran it. The recorded column first, then the
    # "(@name)" tag OpenCode leaves in the title, then the honest "subagent" -- Claude
    # Code names none of its Tasks, and its titles are a uniform "subagent run", so
    # falling back to the title there would put a session name on the chart to say
    # nothing. Deeper-than-direct nodes carry a marker: they are drawn as siblings of the
    # direct children (see SessionFlame.deep), and a nested execution sitting silently in
    # that row would read as a direct delegation.
    agent = str(row.get("agent") or "").strip()
    name = agent if agent.lower() not in _FLAME_DULL_AGENTS else ""
    if not name:
        tag = _FLAME_AGENT_TAG.search(str(row.get("title") or ""))
        name = tag.group(1) if tag else "subagent"
    return ("↳ " + name) if int(row.get("depth") or 0) > 1 else name


def flame_model(row: dict) -> str:
    # The segment's model, in its short display spelling: the route prefix dropped and
    # the release-date/effort suffix stripped (anthropic/claude-haiku-4-5-20251001 ->
    # claude-haiku-4-5), because a segment has tens of cells, not eighty.
    return display_model(str(row.get("model_name") or "").rsplit("/", 1)[-1])


class FlameSegment(NamedTuple):
    """One band of the session flamegraph: a slice of the session's spend wide enough
    to be worth a colour. `depth` 0 is the root's own work (the flamegraph's "self"
    frame), 1 a direct subagent, 2+ a nested one folded in as a sibling."""

    label: str  # `agent`, made unique across the session -- the key's handle
    agent: str  # the AGENT that ran it ("explore"), bare and possibly repeated
    model: str  # short display spelling of the node's dominant model
    value: float  # dollars, or tokens when the session recorded no cost at all
    share: float  # of SessionFlame.total, 0..1
    slot: int  # index into the categorical ramp
    depth: int

    # Two names for one execution, because the two places they appear need different
    # things. UNDER THE BAND position already says which slice is which, so the bare
    # `agent` reads best there -- five slices each labelled "code-reviewer" is the truth,
    # and "code-reviewer 15:00" would be five clock times nobody asked about. IN THE KEY
    # there is no position to lean on, so `label` carries whatever App._flame_labels had
    # to add to tell them apart.


class SessionFlame(NamedTuple):
    """A session's spend as a hierarchy: the whole session on top, partitioned below
    into the root's own work and each subagent execution. Width is money -- which is
    the point, and what a tree TABLE cannot say: a table sorted by cost tells you the
    ranking, an icicle tells you the *proportion*, and "the root kept 42% and five
    subagents split the rest almost evenly" is one glance rather than six subtractions.

    Widths come from `App._priced_nodes`, i.e. the Cost column's own meaning (recorded
    spend, list-price-estimated only where nothing was recorded), so the chart and the
    table under it can never disagree about a node. When a session recorded no cost at
    all AND "$" is off -- a subscription backend with the estimate turned off -- there
    are no dollars to divide, so the unit falls back to `tokens` and says so rather
    than drawing an empty frame.

    Depth is a band in principle and one band in practice: `workflow_nodes` gives each
    node a depth but no PARENT, so a depth-2 node cannot be placed under the depth-1
    node it actually ran below. Rather than draw a nesting the stores don't record,
    those nodes join the same band as siblings (`deep` counts them, the label marks
    them, and the note names it). Measured on 1,117 real sessions this costs nothing:
    exactly one session nests deeper than one level, and it spent $0. If a backend
    ever exposes parent links, this becomes a real N-level icicle without the chart
    changing shape.
    """

    segments: tuple[FlameSegment, ...]  # self first, then subagents, cost-descending
    total: float  # the session's whole spend -- the denominator every share is of
    unit: str  # "cost" | "tokens"
    estimated: bool  # a width is a list-price estimate, not recorded spend
    deep: int  # nodes at depth >= 2, drawn as siblings because parents aren't recorded
    silent: int  # subagent nodes with no value at all -- no width to draw, so not shown

    @property
    def self_share(self) -> float:
        return sum(s.share for s in self.segments if s.depth == 0)

    @property
    def children(self) -> tuple[FlameSegment, ...]:
        return tuple(s for s in self.segments if s.depth > 0)

    @property
    def one_model(self) -> str:
        # The model every drawn segment ran on, or "" when they differ. 85 of the 135
        # delegating sessions in real data are single-model end to end, and there the
        # model belongs in the caption once instead of repeated under every segment --
        # which is also what buys the other 50 the room to name theirs per segment.
        models = {s.model for s in self.segments if s.model}
        return models.pop() if len(models) == 1 else ""


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
    status: str = ""  # models.dev lifecycle flag (alpha/beta/deprecated); catalog view only
    pinned: bool = False  # row is pinned (App.pinned_models, "route/canon" keys); floats first


class App:
    workflow_tabs = ("Overview", "Subagents")  # a session's model mix lives in the Overview
    day_tabs = ("Overview", "Projects", "Sessions")  # day models stay folded into Overview
    month_tabs = ("Overview", "Models", "Projects", "Sessions")
    year_tabs = ("Overview", "Models", "Projects", "Sessions")
    project_tabs = ("Overview", "Models", "Sessions")
    # Machines mode (the fleet view): one box's Overview (the live/pulled niceties),
    # its sessions, its model mix, and which projects ran on it. "Harnesses" is injected
    # after Overview by current_tabs like every other scope (the fleet is always combined).
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
    # The P overlay's price table sorts by model name, the blended eff column, your
    # usage share, or any of the four list-price columns. "eff" is the default and
    # sorts cheapest-first (it's in ascending_sort_keys); model sorts a->z; the raw
    # price columns and "use" sort high->low, so the priciest/most-used surface first.
    prices_sort_options = ("model", "eff", "use", "input", "output", "cache_read", "cache_write")
    # The Trends overlay's four RANKED tabs sort by their own columns -- the ranking
    # is the tab, so "which harness did I use most sessions on" and "which models do I
    # have alphabetically" had no answer while every one of them was hard-wired to
    # cost. Each tab offers exactly the columns it DRAWS: the Models table trades its
    # Tokens/Msgs cells for name width (long model ids show in full), so it offers only
    # the two it shows -- sorting by a column that isn't on screen is a ranking the user
    # cannot check. "cost" is in every tab's set, which is what makes it the fallback
    # when a tab withdraws the stored key (the SORT_FALLBACKS rule: fall back inside the
    # column family every ranked tab shares, not to an arbitrary first option).
    _TREND_SORT_COLUMNS = {
        "Models": ("cost", "name"),
        "Providers": ("cost", "name", "tokens", "count"),
        "Harnesses": ("cost", "name", "tokens", "count"),
        "Machines": ("cost", "name", "tokens", "count"),
    }
    # ...and what those two shared keys are CALLED per tab, since the picker names a
    # column the user is looking at ("Harness"/"Sessions", not "name"/"count"). The keys
    # stay shared so a sort survives a tab flip: ranking Harnesses by sessions and
    # tabbing to Providers keeps you on the count column rather than snapping to cost.
    _TREND_SORT_LABELS = {
        "Models": {"name": "Model"},
        "Providers": {"name": "Provider", "count": "Msgs"},
        "Harnesses": {"name": "Harness", "count": "Sessions"},
        "Machines": {"name": "Machine", "count": "Sessions"},
    }
    # Every key any ranked tab accepts. What state.json validates a restored column
    # against -- the stored key is per-OVERLAY and re-validated per tab at draw time, so
    # a saved "tokens" has to survive a launch that opens on Models, which withdraws it.
    TREND_SORT_KEYS = frozenset(k for opts in _TREND_SORT_COLUMNS.values() for k in opts)
    # The P overlay's layout modes, cycled by `p`: "flat" (the default) is one
    # ungrouped list -- cheapest-for-your-mix is a cross-vendor question -- while
    # "family" groups deduped models under their vendor (Anthropic/OpenAI/…) and
    # "provider" groups one row per access route (anthropic/github-copilot/…, a
    # model can repeat across gateways). "all" swaps the row *set*, not just the
    # layout: the whole models.dev catalog priced at your mix (used or not), flat.
    # (key, label) -- shown in header + toast.
    prices_views = (
        ("flat", "flat list"),
        ("family", "by vendor"),
        ("provider", "by provider"),
        ("all", "models.dev"),
    )
    # Columns whose natural order is ascending (a->z / shallow-first / cheap-first);
    # every other column sorts high->low by default. A header re-click flips it.
    ascending_sort_keys = frozenset({"title", "project", "model", "agent", "depth", "eff", "name"})
    _TREND_TABS_BASE = (
        "Daily",
        "Weekly",
        "Monthly",
        "Calendar",
        "Models",
        "Providers",
        "Harnesses",
    )
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
    # Toasts fade after a few seconds, so a message you glanced away from is gone. The
    # `N` overlay keeps a scrollback of the last TOAST_LOG_MAX of them (never pruned by
    # time, unlike the live cards) so you can read what flew by. It is in-memory only --
    # notices are transient status, never authored data like notes, so nothing persists.
    TOAST_LOG_MAX = 200
    # Class-level defaults so App instances built via __new__ in tests (skipping
    # __init__) still accept a notice. _toast_clock is injectable per instance for
    # deterministic expiry tests; the live `toasts` list is lazily materialised below.
    _toast_clock = staticmethod(time.monotonic)
    _toast_shown = True  # has the newest toast been painted at least once?
    # Same reason: the footer chip and the Subagents tab read the what-if target on
    # every frame, so a __new__-built App must have one. Off is the only sane default
    # -- the target is transient and never restored from state (see __init__).
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
        # The composed key bindings (defaults + the user's keymap.conf overrides).
        # The CLI passes the loaded file; tests and the web path get pure defaults.
        # Every handler resolves keys through this — never against a literal.
        self.keymap = keymap or bindings.DEFAULT
        # Live source switching (the `H` key). source_key is the active backend's key;
        # built stores are cached so cycling back is instant. Empty when the App was
        # constructed without a key (tests / single fixed store).
        self.source_key = source_key
        # Keyed by (source, demo state) where state is None (real) or the frozenset of
        # scrambled categories -- so real, demo-all, and demo-titles-only are distinct
        # cached stores the D picker can flip between without a re-parse.
        self._store_cache: dict[tuple[str, frozenset | None], object] = (
            {(source_key, self._store_state_key(store)): store} if source_key else {}
        )
        self.loaded = store.workflows()  # every root session, all time
        # "$" toggles real cost <-> API-equivalent. When no active backend records
        # dollars (Claude Code alone, or "all" with Claude in the mix) the real view
        # is a wall of $0.00, so start in the estimate view; an explicit saved pref
        # (apply_state) or the $ key takes over from there.
        self.show_api_prices = not getattr(store, "records_cost", True) and not store.demo
        # The `w` what-if target: one model, armed to answer a SESSION-scoped question
        # ("I ran the main agent on the expensive model and delegated the grunt work --
        # what if that model had done all of it?"). Its only effect is the session tree
        # table on a session's Subagents tab; every other panel, and "$" itself, carry
        # on showing real/estimated spend. Deliberately NOT persisted to state.json:
        # it's a transient analysis mode, and a remembered one would silently falsify
        # every future launch's Subagents tab.
        self.whatif_model: str | None = None
        self.whatif_menu = False  # the `w` target-model picker overlay
        self.whatif_menu_index = 0  # highlighted row in that picker
        self.whatif_query = ""  # its live `f` filter (word-anchored, like the P overlay's)
        self.whatif_filter_active = False  # keys are editing that query
        self.whatif_catalog = False  # Tab in the picker: your models <-> the whole catalog
        self._whatif_catalog_rows: list[tuple[str, float, bool]] | None = None  # lazy, memoized
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
        # Estimated context composition (Claude + Zaly), same lazy/cached deal --
        # the Context tab's curve reads the turn rows, this is only its tree.
        self._context_by_session: dict[str, list[dict]] = {}
        # Subagent tree per session (workflow_nodes: a recursive CTE / backend parse),
        # same lazy/cached-per-session deal -- the Subagents tab repaints every frame.
        self._nodes_by_session: dict[str, list[dict]] = {}
        # Session id whose lazy fetches the next run() tick should prefetch: set by
        # draw_detail when it paints the "loading" frame instead of blocking mid-draw.
        self._session_loading: str | None = None
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
        self.launch_menu_backend: str | None = None
        self.price_prompt = False  # the "unpriced models found" startup prompt
        self._price_prompt_done = False  # offered at most once per run
        self.prices_prompt_dismissed = False  # "don't ask again" pref (persisted in state)
        self.allow_price_prompt = True  # off under --no-state/--demo (set in main)
        self.unknown_models: list[str] = []  # used models with no built-in price
        self.source_menu = False  # the `H` data-source picker overlay
        self.source_menu_index = 0  # highlighted row in that picker
        self.machine_menu = False  # the `M` machine-filter picker overlay (fleet view)
        self.machine_menu_index = 0  # highlighted row in that picker
        self.harness_menu = False  # the fleet `H` harness-filter picker overlay
        self.harness_menu_index = 0  # highlighted row in that picker
        self.sort_menu = False  # the `s` sort-order picker overlay
        self.sort_menu_index = 0  # highlighted row in that picker
        self.demo_menu = False  # the `D` demo-category multi-check picker overlay
        self.demo_menu_index = 0  # highlighted category row in that picker
        self.demo_menu_sel: set = set()  # categories checked while the picker is open
        # The categories the last demo was armed with, so switching demo off and back
        # on re-offers them rather than resetting to "everything" (see demo_action).
        self.demo_last_sel: frozenset | None = None
        # Active colour theme (shared source with the web browser). Seeded from
        # --theme; a valid saved theme (apply_state) or the `Y` picker takes over.
        self.theme_id = getattr(args, "theme", None) or themes.DEFAULT_THEME
        if self.theme_id not in themes.THEMES:
            self.theme_id = themes.DEFAULT_THEME
        self.theme = themes.resolve_theme(self.theme_id)
        self.theme_menu = False  # the `C` (Colours) theme picker overlay
        self.theme_menu_index = 0  # highlighted row in that picker
        self._theme_before = self.theme_id  # theme active when the picker opened (Esc reverts)
        self.day_index = 0
        self.month_index = 0
        self.year_index = 0
        self.project_index = 0
        self.machine_index = 0  # selected box in the Machines-mode sidebar
        self._local_machine_fake = ""  # memoized demo alias for this box (local_machine_name)
        self.workflow_index = 0  # selected session in a zoomed Sessions tab
        # Tab cycles focus across the three stacked left panels. Enter drills:
        # browse -> zoom (year/month/day detail) -> session (one session's detail).
        self.focus = "days"  # "years" | "months" | "days"
        self.browse_mode = "time"  # "time" | "projects" | "machines"
        # Where you were in each browse mode, so switching modes and back lands you on the
        # same session/tab/drill (a session's Context graph, say) instead of a fresh browse.
        # Keyed by the mode you left; value-anchored (session id / project dir / month·day /
        # names, not raw indices), so it self-heals against a range/sort/filter change rather
        # than needing invalidation -- see _capture_mode_memory / _restore_mode_memory.
        self._mode_memory: dict[str, dict] = {}
        # In-TUI machine refresh (R): a re-pull request handed to the run() loop so a
        # "refreshing…" toast paints before the blocking ssh fetch (the _session_loading
        # trick). _refresh_backend is injected by main()/web_command when a fleet is in
        # view -- it fetches given remotes keys and returns [(key, count, error)].
        self._refresh_request: list[str] | None = None
        self._refresh_backend = None
        # {remotes.json key -> ssh target}, injected by main() alongside _refresh_backend
        # so `L` can reopen a session on the box it actually ran on. A callable, not a
        # dict: remotes.json is re-read per keystroke, so a machine added mid-run lands
        # without a restart. None outside the fleet view -> `L` stays purely local.
        self._ssh_targets = None
        self.view = "browse"  # "browse" | "zoom" | "session"
        # lazygit-style zoom: the detail becomes the active pane in the split; `+`
        # maximizes it full-screen on demand (a saved pref, so it sticks between runs).
        self.zoom_maximized = False
        self.tab = 0
        self.scroll = 0
        self.help = False
        self.help_scroll = 0  # pager offset within the help overlay
        self.toast_history = False  # the `N` notices scrollback overlay
        self.toast_history_scroll = 0  # pager offset within it
        self.trends = False  # the Trends overlay (T); trend_tab selects its tab
        self.trend_tab = 0
        self.trend_month_index = 0  # which month the Daily tab shows (0 = newest)
        self.trend_week_index = 0  # which week the Weekly tab shows (0 = newest)
        self.trend_year_index = 0  # which year the Calendar tab shows (0 = newest)
        self.cal_cursor: str | None = None  # highlighted day on the Calendar tab (None = peak)
        # One focus flag for every trend canvas (Calendar grid + the bar charts):
        # Enter focuses, arrows then pick a day/bar, Esc steps back to tab navigation.
        self.trend_focus = False
        # Highlighted bucket on the bar tabs: a date (Daily/Weekly) or "YYYY-MM"
        # (Monthly); None = that chart's peak bucket (mirrors cal_cursor).
        self.trend_cursor: str | None = None
        self.trend_row_index = 0  # cursor on the ranked tabs (Models/Providers/Sources)
        # The ranked tabs' column sort, biggest-spend-first by default (what a trends
        # ranking is read for). One pair for all four tabs, validated per tab against
        # _TREND_SORT_COLUMNS -- pick a column with the `s` picker or a header click,
        # a re-click flips direction. Persisted like the other lists' sorts.
        self.trend_sort = "cost"
        self.trend_sort_reverse = False
        # Drilled into a ranked row's sessions: ("model"|"provider"|"source", key).
        self.trend_drill: tuple[str, str] | None = None
        self.trend_drill_index = 0  # cursor within that sessions list
        # Turns tab: the prompt DRILLED INTO (Enter / a click), or None for the prompt
        # table. Drilling rather than a modal, because that is what this app does
        # everywhere -- months, days, projects, sessions, trend rows and price rows all
        # open in place and step back with Esc -- and a popup would make this one tab the
        # exception. The table itself never folds: every row carries its own numbers, and
        # a 200-turn prompt is one row here and its own view in there.
        self.turn_drill: int | None = None
        # ...and the session it belongs to. An ordinal is only meaningful inside ONE
        # session's prompt list, and unlike the prompt_id it replaced it is valid in
        # almost any session, so a drill left armed while another session comes on screen
        # silently opens that session's Nth prompt. Rather than clear it on every path
        # that can swap the session (_restore_mode_memory did not, and enumerating them
        # is exactly the bet that failed), the ordinal is checked against this id at the
        # point of use -- see active_turn_drill.
        self._turn_drill_session: str | None = None
        self._turn_cursor = 0  # Turns tab: selected ▸ prompt group (a run-ordinal); j/k move it
        self._turn_follow = False  # scroll to reveal the Turns cursor on the next draw
        self.cal_levels = HEAT_DEFAULT_LEVELS  # heat-map granularity, live-adjustable with +/-
        self.has256 = False  # set in run() once curses knows the terminal's color depth
        self.colors_ok = True  # run() clears it on a monochrome terminal (no start_color)
        # May the renderer REDEFINE palette slots (init_color) to hit the theme's exact
        # hexes? cli._resolve_init_color clears it for terminals that accept the call
        # and ignore it -- detected, or forced with $OPENTAB_NO_INIT_COLOR. See
        # init_theme_colors.
        self.allow_init_color = True
        self._cal_geom: tuple | None = None  # last calendar grid geometry, for mouse hit-testing
        self._trend_bar_geom: tuple | None = None  # last bar-chart geometry, for mouse hit-testing
        # Scope drilled into from the Trends overlay; Esc out of it returns there.
        # ("Calendar"|"Daily"|"Weekly", date) · ("Monthly", month) · ("drill", kind, key, row).
        self._trend_return: tuple | None = None
        self.show_prices = False  # the "P" model-prices reference overlay
        self.prices_scroll = 0  # pager offset within that overlay
        self.prices_index = 0  # selected model row in the P overlay's list
        # Pinned models (space toggles): a hand-picked shortlist that floats above
        # every P view -- the point is keeping your candidate models in sight above
        # the ~5k-row models.dev catalog. Pins are ROW-scoped, stored as
        # "route/canon" ("canon" for a route-less row): pinning one gateway's row in
        # the catalog must not light up the 20 other resellers of the same model,
        # while pinning an aggregated flat/vendor row pins the routes it covers
        # (the ones you actually use). Persisted in state.json.
        self.pinned_models: set[str] = set()
        self.prices_model: str | None = None  # drilled into this model's sessions (P overlay)
        # The P overlay's column sort: cheapest-for-your-mix first by default (the
        # point of the overlay); pick another column via the `s` picker or a header
        # click, re-clicking a header flips direction (prices_sort_reverse).
        self.prices_sort = "eff"
        self.prices_sort_reverse = False
        self.prices_view = "flat"  # P overlay layout: one of prices_views (p cycles)
        self.sort_by = "cost"
        self.project_sort_by = "cost"
        self.subagent_sort_by = "cost"
        # Per-context "flipped off the natural order" flags, toggled by re-clicking a
        # column header. Session, project, and subagent lists each keep their own
        # sort pair so reordering one list never clobbers another's preference.
        self.sort_reverse = False
        self.project_sort_reverse = False
        self.subagent_sort_reverse = False
        self.ignored_projects: set[str] = set()
        self.ignored_sessions: set[str] = set()
        self.show_ignored_projects = False
        # Sessions starred with `b` (ids, persisted in state.json). `B` flips
        # show_bookmarks_only, the session-level cousin of the ignored projects' I:
        # every view narrows to just the starred sessions.
        self.bookmarks: set[str] = set()
        self.show_bookmarks_only = False
        # `n` annotates the selected session: {session id: note}, loaded from and
        # saved to its own notes.json (opentab.notes) rather than state.json --
        # this is authored data, not a pref. Written on every edit, not at exit.
        self.notes: dict[str, str] = {}
        self.notes_enabled = True  # --no-state turns notes off entirely (set in main)
        self._notes_ok = True  # False once a reload found notes.json there but unreadable
        # When set (in a month/day zoom), the Sessions list is narrowed to this
        # project's sessions within the zoomed scope. Drilled into from the
        # Projects tab; cleared on step-out or any scope change.
        self.zoom_project: str | None = None
        # Same drill for the merged view's Sources tab: j/k pick a tool
        # (source_index), Enter narrows the Sessions list to that source.
        self.zoom_source: str | None = None
        self.source_index = 0  # selected row on a zoomed Sources tab
        # And the Machines-mode detail's Models tab: j/k pick a model (model_pick_index),
        # Enter narrows the box's Sessions to the ones that used it. Machines-mode only,
        # and mutually exclusive with the box's zoom_source/zoom_project drills.
        self.zoom_model: str | None = None
        self.model_pick_index = 0
        # And the fleet's per-scope Machines tab (a month/day/project cut by box): j/k
        # pick a machine (machine_pick_index), Enter narrows Sessions to it. Distinct from
        # machine_index (the top-level Machines-MODE sidebar) -- a scope can carry both.
        self.zoom_machine: str | None = None
        self.machine_pick_index = 0
        # The `M` GLOBAL machine filter (fleet view): a name narrows *every* view to that
        # box. None = all machines. Distinct from zoom_machine (a per-scope drill); keyed
        # into the workflow caches below, and revalidated on reload/source swap.
        self.machine_filter: str | None = None
        # The GLOBAL harness filter, its orthogonal twin (w.source). In a FLEET, `H` arms
        # this -- narrowing to one tool across every machine -- instead of swapping the
        # store (which would drop the pulled boxes); outside a fleet `H` still swaps. So
        # machine ⊥ harness: "pi, on server" is M+H composed. Fleet-only (revalidation clears
        # it on leaving the fleet); keyed into the workflow caches like machine_filter.
        self.harness_filter: str | None = None
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
            # The `M` machine and `H` harness global filters narrow here too, so every
            # downstream view (all_workflows and the shown-ignored path both read this)
            # agrees on the one box / one tool. They compose (machine ⊥ harness).
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
        # Every visible workflow in the active range. Ignored projects are removed
        # here, so summaries, trends, sessions, and exports all agree.
        key = (
            id(self.loaded),
            self.custom_since,
            self.custom_until,
            self.range_days,
            self.range_months,
            tuple(sorted(self.ignored_projects)),
            tuple(sorted(self.ignored_sessions)),
            # ranged_workflows narrows to bookmarks under B and by the `M`/`H` global
            # filters; mirror all three in this fingerprint so the cache follows along
            # (ranged_workflows already applied them -- this key just has to change with them).
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
        # Every in-zoom drill, VALUE AND CURSOR, dropped together. Called wherever the
        # scope or the data under it changes: a range change (which used to drop only the
        # project drill, leaving a harness or model one armed against a window that may
        # contain neither), a reload, a mode or focus change, a sidebar click or wheel.
        #
        # Clearing a value without its cursor is its own bug: the cursor is an ordinal
        # into a ranking that has just been rebuilt, so the paint clamps the highlight
        # while j/k still counts from the old number, and the first press only re-clamps
        # to where the highlight already is -- a key that visibly does nothing.
        self._clear_project_drill()
        self._clear_source_drill()
        self._clear_machine_drill()
        self._clear_model_drill()

    def set_all_time(self) -> None:
        anchor = self.selection_anchor()  # BEFORE the clears: it names the SELECTED
        self._clear_zoom_drills()  # session, and the clears widen the list under it
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
        anchor = self.selection_anchor()  # BEFORE the clears: it names the SELECTED
        self._clear_zoom_drills()  # session, and the clears widen the list under it
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
        # The note is a search field too — annotating a session is half of how you
        # find it again months later ("that one where the migration went sideways").
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

    # --- Machines mode (one row per box; a fleet adds the pulled ones) -------
    def machine_meta(self) -> dict[str, dict]:
        # {machine name -> {live, exported_at, opentab_version, key}} from the store,
        # empty for a non-fleet store (whose one box is the live local one -- see
        # `machines`, which marks it live without any store help).
        return getattr(self.store, "machine_meta", {}) or {}

    @property
    def local_machine_name(self) -> str:
        # The box an UNTAGGED session ran on: this one. Only the fleet build stamps
        # w.machine, so off a fleet every session is untagged and this is the single row
        # the Machines mode shows -- "just this machine", rather than a nameless "unknown"
        # box. In a fleet the live local store stamps this exact string (sources.py calls
        # the same helper), so a mixed batch can never split the local box into two rows.
        # Scrambled under demo like a pulled label: a hostname is identity, as a title or
        # a path is.
        name = util.local_machine_name()
        if not (self.store.demo and "titles" in self._demo_cats):
            return name
        # Memoized: every machine grouping and filter asks once per workflow, and the
        # scramble hashes. One slot is enough -- the hostname can't change under a
        # running TUI, so the fake is the same on every `D` flip back.
        if not self._local_machine_fake:
            self._local_machine_fake = demo_machine(name)
        return self._local_machine_fake

    def machine_of(self, workflow: Workflow) -> str:
        # Which box a session belongs to -- its tag, or this machine when it has none.
        return workflow.machine or self.local_machine_name

    @property
    def machines(self) -> list[MachineSummary]:
        # One row per box in view, built from the grouped workflows plus the store's
        # per-machine niceties (live vs pulled, export time/version). The live local box
        # floats first -- it is "you are here", and the only one with full drill-in -- then
        # by spend; the `f`/`/` query fuzzy-ranks by name like the projects list.
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
                # Off a fleet the store has no machine_meta at all, and the one row is by
                # definition this live box (full drill-in) -- so it must not render as a
                # `○ pulled summary` just because nothing stamped it.
                live=bool((meta.get(name) or {}).get("live")) or (not meta and name == local),
                exported_at=str((meta.get(name) or {}).get("exported_at") or ""),
                opentab_version=str((meta.get(name) or {}).get("opentab_version") or ""),
            )
            for name, wfs in grouped.items()
        ]
        rows.sort(key=lambda m: (m.live, m.cost, m.tokens), reverse=True)
        # Deliberately NOT filtered by the `f`/`/` query: a fleet has a handful of boxes
        # (no list to narrow), and a hostname isn't one of workflow_fuzzy_score's fields,
        # so filtering the LIST by name would then empty the selected box's Sessions (the
        # query, applied to that box's sessions, matches none of their titles/paths). The
        # query stays a session-content filter, scoping the sessions WITHIN the box.
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

    # --- The `M` global machine filter (fleet view) --------------------------
    def machine_filter_options(self) -> list[tuple[str, str, bool]]:
        # (value, label, is-active) for the `M` picker; "" (rendered "All machines")
        # clears the filter. Built over ALL loaded data -- unfiltered by the armed filter
        # or the range -- so every box stays selectable even when it is currently hidden.
        # Live-first then by spend, matching the machines-mode list's order.
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
        # `M` opens the small machine-filter picker (j/k, Enter arms, Esc cancels). Only
        # meaningful in a fleet; off one there is nothing to narrow.
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
        # Arm (or clear, name="" / None) the global machine filter and re-anchor the
        # selection so the narrowed views land on a still-visible row.
        name = name or None
        if name == self.machine_filter:
            return
        anchor = self.selection_anchor()
        self.machine_filter = name
        self._invalidate_workflow_cache()
        self.restore_selection(anchor)
        self.notify(f"machine: {name}" if name else "machine filter cleared", "success")

    def _revalidate_machine_filter(self) -> None:
        # After the loaded data changes under an armed filter (reload, `H` source switch,
        # `D` demo rename, `F` machine re-pull), keep the filter only if its box still
        # exists -- a source swap that drops the box, or demo's rename, clears it rather
        # than silently emptying every view.
        if self.machine_filter is None:
            return
        names = {self.machine_of(w) for w in self.loaded}
        if self.machine_filter not in names:
            self.machine_filter = None

    def handle_machine_menu_key(self, key: int | str) -> bool:
        # The `M` machine-filter picker: down/up move, select arms/clears, cancel
        # closes; advance (`M` again) walks the highlight, like the `H` menu.
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
            self.machine_menu = False  # cancel, filter unchanged
        # any other key: ignore and keep the menu open
        return True

    # --- The fleet `H` global harness filter (machine_filter's orthogonal twin) ---
    def harness_filter_options(self) -> list[tuple[str, str, bool]]:
        # (value, label, is-active) for the fleet `H` picker; "" ("All harnesses") clears
        # it. Distinct w.source over ALL loaded data, most-used first -- the harness twin
        # of machine_filter_options.
        grouped: dict[str, float] = defaultdict(float)
        for w in self.loaded:
            grouped[w.source or "unknown"] += w.total_cost
        names = sorted(grouped, key=lambda n: grouped[n], reverse=True)
        out: list[tuple[str, str, bool]] = [("", "All harnesses", self.harness_filter is None)]
        for name in names:
            out.append((name, name, self.harness_filter == name))
        return out

    def can_harness_filter(self) -> bool:
        # Whether the fleet `H` filter has anything to do: a fleet with >=2 harnesses to
        # pick between, OR a filter already armed (which must ALWAYS be reachable to clear,
        # even after the other harness's sessions vanish and only its own remains).
        if not self.machines_present:
            return False
        if self.harness_filter is not None:
            return True
        return len({w.source or "unknown" for w in self.loaded}) >= 2

    def open_harness_menu(self) -> None:
        # In a fleet, `H` opens this filter (narrow to one tool across every machine) rather
        # than swapping the store -- a store swap would drop the pulled boxes. Off a fleet
        # `H` still swaps (open_source_menu); the caller routes on machines_present.
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
        # Harness filtering is a fleet-only concept (outside a fleet `H` swaps stores). Drop
        # it when the fleet is gone, or when the active data no longer has that harness.
        if self.harness_filter is None:
            return
        present = {w.source or "unknown" for w in self.loaded}
        if not self.machines_present or self.harness_filter not in present:
            self.harness_filter = None

    def handle_harness_menu_key(self, key: int | str) -> bool:
        # down/up move, select arms/clears, cancel closes; advance (`H` again) walks
        # the highlight (mirrors the machine-filter picker).
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
            self.harness_menu = False  # cancel, filter unchanged
        return True

    def open_harness_picker(self) -> None:
        # The `H` key's single entry point: in a fleet it's the harness FILTER (keep every
        # machine), else the store-swap source picker -- the fork the user chose.
        if self.machines_present:
            self.open_harness_menu()
        else:
            self.open_source_menu()

    def mode_tab_list(self) -> list[tuple[str, str]]:
        # (label, mode) for the top-level browse-mode tab strip. All three, always: off a
        # fleet the Machines mode is a one-row view of the box you're sitting at (its own
        # spend, model mix and top projects), which is a real answer -- and the only place
        # the consolidated view announces itself to someone who has never run `--pull`.
        return [("Time", "time"), ("Projects", "projects"), ("Machines", "machines")]

    def switch_browse_mode(self, mode: str) -> None:
        # The mouse/tab entry point. set_browse_mode now works from a drilled-in session
        # itself (snapshotting it into per-mode memory), so this is a thin alias -- the
        # p/t/m keys and a mode-tab click go through exactly the same path.
        self.set_browse_mode(mode)

    def refreshable_machines(self) -> list[str]:
        # Names of pulled boxes (they carry a remotes key); the live local box refreshes
        # by a plain reload, not a re-pull. Empty when nothing was pulled.
        meta = self.machine_meta()
        return [n for n, m in meta.items() if (m or {}).get("key")]

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
        # Time- and Machines-mode zooms both have a navigable Projects picker (projects
        # mode's sidebar IS the project, so it's excluded).
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
        # `b` (and `n`) work wherever one session is selected: a zoom's Sessions tab
        # or the drilled-in session detail — the same contexts as `L` (launch_session).
        #
        # Deliberately NOT memoized. The keymap asks a few times per paint (b and n each
        # ask "is there a target?" and "is it already marked?"), and a cache keyed on the
        # state it reads has to enumerate every input current_sessions() touches --
        # bookmarks (B narrows the list *by* them), ignored_sessions, the notes (the
        # fuzzy filter searches them), show_api_prices (it moves the costs a cost sort
        # orders by)... Miss one and `b` acts on the session the cursor ISN'T on, which
        # is far worse than re-sorting a list the draw already re-sorts anyway (the
        # sessions picker calls current_sessions() every frame regardless).
        if self.view == "session" or (self.view == "zoom" and self.on_sessions_tab):
            return self.current_session()
        return None

    # A note is a sentence about a session, not an essay: long enough to say why the
    # money was spent (and to wrap over a few lines in the Overview), short enough to
    # stay one field in a CSV row. The prompt scrolls to reach it (prompt_text).
    NOTE_MAX_CHARS = 500

    @property
    def allow_notes(self) -> bool:
        # Computed, never captured: `D` toggles demo *live*. Demo fakes every title and
        # path but the session ids stay real, so a note loaded from disk would be the one
        # true thing on an anonymised screen -- and worse, editable, writing real
        # annotations while you thought you were in the safe mode.
        return self.notes_enabled and not bool(getattr(self.store, "demo", False))

    def refresh_notes(self) -> bool:
        # Re-read on every store swap (source switch, `D`, reload): it re-applies the
        # gate above, and picks up anything another opentab has written meanwhile.
        # Returns False (having said so) when the file is there but unreadable, and
        # parks that in _notes_ok: toasts set within one handler collapse onto the last,
        # so every caller must skip its own cheerier message rather than bury this one.
        self._notes_ok = True
        if not self.allow_notes:
            self.notes = {}
            return True
        notes, readable = read_notes()
        if not readable:
            # The file is broken, not empty. Keep what's loaded: blanking the ✎ marks
            # would look exactly like the notes had been deleted -- which is the thing
            # we refuse to do to them.
            self.notify(f"notes: {short_path(notes_path(), 60)} is unreadable", "error")
            self._notes_ok = False
            return False
        self.notes = notes
        return True

    def note_for(self, workflow_id: str) -> str:
        return self.notes.get(workflow_id, "")

    def edit_note(self, stdscr: curses.window) -> None:
        # `n` annotates the selected session — the answer to "why was this one
        # expensive / worth it", which no amount of token accounting records. The
        # curses half only: prompt_text seeded with the existing note (so `n` edits
        # rather than overwrites), then set_note does the work.
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
            return  # Esc: the existing note stands
        self.set_note(session, value)

    def set_note(self, session: Workflow, text: str) -> None:
        # update_note re-reads the file and merges, so the map we adopt afterwards is
        # the truth on disk (including notes another opentab wrote while this one was
        # open) -- never our own stale copy replayed over theirs. A refused write leaves
        # both the file and this map untouched: an unsaved note never sits in memory
        # pretending it was saved.
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
            self.notify(
                f"no bookmarks — press {self.keymap.label('main', 'bookmark')} on a session",
                "error",
            )
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
    ) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
        # Capture the selected row's value (not focused_year, which is None for the
        # "All years" row) so an "All years" selection survives a reload/source switch.
        sel_year = self.selected_year_summary
        year = sel_year.year if sel_year else None
        month = self.focused_month
        day = self.active_day if month else None
        project = self.selected_project_summary
        # The machine by NAME, so a refresh that reorders the boxes (a re-pull changed
        # their spend) re-selects the same box rather than whatever now sits at its index.
        machine = self.selected_machine_summary if self.browse_mode == "machines" else None
        session = self.current_session()
        return (
            year,
            month,
            day,
            project.directory if project else None,
            machine.name if machine else None,
            session.id if session else None,
        )

    def restore_selection(
        self,
        anchor: tuple[str | None, str | None, str | None, str | None, str | None, str | None],
    ) -> None:
        year, month, day, project_dir, machine_name, session_id = anchor

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

        # Before current_sessions(): in machines mode that list is scoped by the selected
        # box, so the machine index must be re-anchored first.
        machine_rows = self.machines
        if machine_name and machine_rows:
            self.machine_index = next(
                (i for i, row in enumerate(machine_rows) if row.name == machine_name),
                min(self.machine_index, len(machine_rows) - 1),
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
            rename = "titles" in self._demo_cats  # local->cloud model names ride with titles
            for root_id, models in self._model_by_root.items():
                rows = self._demo_rename_models(models) if rename else models
                self._model_by_root[root_id] = self._scale_demo_models(rows)
            # Reconcile after scaling: the model rows and the workflow totals are now
            # both multiplied by the same factor, so the synthetic fill stays consistent.
            self._reconcile_demo_models()
        else:
            self._reconcile_unpriced_tokens()
            self._compute_api_costs()
        self._models_loaded = True
        self._whatif_catalog_rows = None  # the token mix behind its eff column changed
        self._apply_price_mode()  # re-assert the active ($/API) view onto fresh rows
        self._revalidate_whatif()  # the target may have lost its list rate

    def _reconcile_unpriced_tokens(self) -> None:
        """Restate each session's unpriced-token count from the per-model rows.

        `workflows()` is the fast first-frame query, so a backend can only afford to
        answer this at whatever granularity it already aggregates: OpenCode asks it per
        session NODE (`case when node_cost = 0`), which counts a node with one priced
        message as entirely priced and zeroes the rest of its tokens. `model_breakdown`
        -- the deferred scan, which is where the "$" estimate itself comes from -- splits
        it per MESSAGE, and is right. Measured on a real 692-session DB, 23 sessions and
        330M tokens were labelled priced that are not; the worst prints "Unpriced tokens:
        0" against $37 of estimable spend and suppresses the hint that would tell you to
        press `$`.

        Restating it here (where model_count is already taken from the same rows) keeps
        the fast path fast and fixes every backend at once, rather than teaching each
        one's rollup query a granularity it cannot cheaply reach. Demo is excluded by the
        caller: scramble_workflow spends this figure on a synthetic cost and then zeroes
        it deliberately.
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
            if rows:  # no rows == the scan knows nothing about it; keep what the store said
                w.unpriced_tokens = sum(int(r.get(p) or 0) for r in rows for p in parts)

    def _revalidate_whatif(self) -> None:
        # The model rows just changed under an armed target (reload, `H` source switch,
        # `D` demo toggle -- all land here). A target needs no usage in the new dataset
        # to stay meaningful -- the picker's catalog tier arms any model the price
        # catalog knows, and the comparison is "this data's tokens at that model's
        # list rates" either way. Only a target we can no longer price for real is
        # dropped (one a fresh App would refuse to arm): its "rates" would be the
        # generic FALLBACK_PRICE, a guess no price list contains.
        if not self.whatif_model or has_known_price(self.whatif_model):
            return
        stale, self.whatif_model = self.whatif_model, None
        self.notify(f"what-if cleared — no list rate for {stale}", "warn")

    def _ensure_models(self) -> None:
        # Run the deferred model-breakdown load once, on demand. Idempotent so the
        # run() loop, reload(), or any first model access all converge to one scan.
        if not self._models_loaded:
            self._load_model_cache()

    @property
    def _demo_cats(self) -> frozenset:
        # Which demo categories the active store scrambles (titles / turns / spend).
        # Default all -- a store without the attribute is the all-or-nothing legacy path.
        return getattr(self.store, "demo_cats", DEMO_ALL)

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
        # Keeps the Models tab consistent with the Money card at every zoom level.
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
        # Whether the Tools tab applies to this session -- backends without the
        # opt-in (Hermes, Copilot, VS Code, OpenClaw) have no supports_tools, so
        # the tab is hidden rather than shown empty.
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
        synth = "spend" in self._demo_cats  # fake a price for $0 rows only when hiding spend
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
        cats = self._demo_cats
        titles, turns, spend = "titles" in cats, "turns" in cats, "spend" in cats
        for n, r in enumerate(rows):
            if titles:
                r["model_name"] = demo_model(r["model_name"])
                # Anonymize the prompt title (a real prompt would leak); keep it stable
                # per prompt_id so a group's turns stay under one fake header.
                if "prompt_title" in r:
                    r["prompt_title"] = demo_title(r.get("prompt_id") or "noprompt")
            # The expandable full text is the `turns` category -- replace it with a
            # stable fake (never the real prompt body) when turns is scrambled.
            if turns and "prompt_full" in r:
                r["prompt_full"] = demo_title(r.get("prompt_id") or "noprompt")
            if spend and r.get("cost", 0) == 0 and r.get("tokens_total", 0) > 0:
                r["cost"] = demo_cost(r["tokens_total"], f"{workflow_id}:{n}")
            for f in ("tokens_total", "input", "output", "reasoning", "cache_read", "cache_write", "cache_write_1h"):  # fmt: skip
                r[f] = int(round(r.get(f, 0) * k))
            r["cost"] = round(r.get("cost", 0) * k, 4)
        return rows

    def turn_groups(self, workflow_id: str) -> list[str]:
        # The ▸ prompt groups on the Turns tab in render order: one entry per
        # consecutive run of a prompt_id -- exactly how detail_turns splits headers,
        # so the Turns cursor (_turn_cursor) is a plain index into this list, and
        # Enter/j/k agree with what's drawn without depending on a prior paint.
        pids: list[str] = []
        last: object = object()
        for r in self.session_turn_rows(workflow_id):
            pid = r.get("prompt_id", "")
            if pid != last:
                pids.append(pid)
                last = pid
        return pids

    def _on_turns_tab(self) -> bool:
        return self.view == "session" and self.active_tab_name() == "Turns"

    def _move_turn_cursor(self, delta: int) -> bool:
        # j/k/PgDn on the Turns tab walk the ▸ prompt cursor instead of raw-scrolling
        # (delta groups, clamped), and ask the next draw to scroll it into view.
        # Returns False (nothing to select) so movement falls back to plain scroll.
        wf = self.current_session()
        groups = self.turn_groups(wf.id) if wf else []
        if not groups:
            return False
        moved = max(0, min(self._turn_cursor + delta, len(groups) - 1))
        if moved == self._turn_cursor:
            # Already at an end: hand the key back so the PANE scrolls instead. Without
            # this the table's own footnotes are unreachable -- j is swallowed by a cursor
            # that cannot move, and everything below the last row stays off-screen.
            return False
        self._turn_cursor = moved
        self._turn_follow = True
        return True

    def _toggle_turn_cursor(self) -> bool:
        # Enter on the Turns tab opens the selected prompt -- its full text and the turns
        # it took -- exactly what a click on the row does. Returns False when there is
        # nothing to open, so Enter falls back to its usual drill-in.
        wf = self.current_session()
        groups = self.turn_groups(wf.id) if wf else []
        if not groups:
            return False
        self._turn_cursor = max(0, min(self._turn_cursor, len(groups) - 1))
        self.open_turn_drill(self._turn_cursor)
        return True

    def turn_cursor_ordinal(self) -> str:
        # "11 of 59" for the popup title -- which prompt of the session you have open.
        wf = self.current_session()
        groups = self.turn_groups(wf.id) if wf else []
        return f"{min(self._turn_cursor + 1, len(groups))} of {len(groups)}" if groups else "-"

    @property
    def active_turn_drill(self) -> int | None:
        """The drilled prompt's ordinal -- but only for the session actually on screen."""
        wf = self.current_session()
        if self.turn_drill is None or wf is None or self._turn_drill_session != wf.id:
            return None
        return self.turn_drill

    def open_turn_drill(self, ordinal: int) -> None:
        # The ORDINAL of the prompt run, not its id: a prompt_id is not unique (a backend
        # that groups by prompt TEXT repeats one), so an id cannot name which run.
        wf = self.current_session()
        self._turn_drill_session = wf.id if wf else None
        self.turn_drill = ordinal
        self.scroll = 0
        self._turn_follow = False

    def close_turn_drill(self) -> bool:
        # Whether it stepped back out of a VISIBLE drilled prompt, so Esc can consume the
        # key here before it starts popping the view stack (Esc out of a session is the
        # usual meaning) -- the trend_drill / zoom_source rule.
        #
        # Gated on active_turn_drill, not on the raw ordinal: a drill armed in another
        # session is inert but still set, and consuming Esc for it would make the key do
        # nothing on the table the reader is actually looking at -- the same complaint
        # that Esc-from-another-tab produced.
        #
        # An inactive drill is left ALONE rather than tidied away. It is remembered state
        # for the session that owns it, exactly like the scroll offset mode memory keeps
        # beside it: clearing it here meant an Esc pressed in one browse mode destroyed a
        # drill belonging to another, and returning there restored that session, its tab
        # and its drilled SCROLL while rendering the prompt table at that offset.
        if self.active_turn_drill is None:
            return False
        self.turn_drill = None
        self._turn_drill_session = None
        self.scroll = 0
        self._turn_follow = True  # put the table back under the row you came from
        return True

    def session_supports_context(self, workflow_id: str) -> bool:
        # Whether the Context tab's estimated composition section applies (only
        # backends whose logs carry full message content implement the opt-in).
        # The tab itself rides on session_supports_context_curve below.
        check = getattr(self.store, "supports_context", None)
        return bool(check(workflow_id)) if check else False

    def session_supports_context_curve(self, workflow_id: str) -> bool:
        # Whether the Context tab applies at all: the measured growth curve needs
        # turn rows whose input+cacheRead are *per-API-request* prompt sizes.
        # That's every Turns backend by default; a backend whose rows are deltas
        # of a cumulative total (Codex -- one row can sum many requests' prompts)
        # opts out via supports_context_curve, hiding the tab rather than
        # charting per-turn consumption as if it were context size.
        if not self.session_supports_turns(workflow_id):
            return False
        check = getattr(self.store, "supports_context_curve", None)
        return bool(check(workflow_id)) if check else True

    def session_context_rows(self, workflow_id: str) -> list[dict]:
        # Estimated composition rows for one session, fetched once and cached (the
        # session_turn_rows deal); the renderer aggregates on top each frame.
        cached = self._context_by_session.get(workflow_id)
        if cached is not None:
            return cached
        fetch = getattr(self.store, "context_breakdown", None)
        rows = [dict(r) for r in fetch(workflow_id)] if fetch else []
        if self.store.demo:
            # Scale the estimates by the hidden factor so they stay proportionate
            # to the scaled turn curve; categories/tool names aren't sensitive.
            k = self.store.demo_scale
            for r in rows:
                r["est_tokens"] = int(round(r["est_tokens"] * k))
        self._context_by_session[workflow_id] = rows
        return rows

    def session_data_ready(self, workflow_id: str) -> bool:
        # Whether every lazy per-session fetch (subagent tree, Turns, Tools) is
        # already memoized. When it isn't, draw_detail paints one "loading" frame
        # and sets _session_loading; run()'s prefetch tick then does the blocking
        # store work (a whole-backend parse on a warm start) and repaints. Cheap:
        # dict lookups + the supports_* gates, no store fetch.
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
        # The blocking fetches behind the loading frame -- same getters the tabs
        # use, so everything lands in the per-session memos and the next paint
        # renders instantly. Gates mirror session_data_ready, so one prefetch
        # always satisfies it (no loading-frame loop).
        self.session_node_rows(workflow_id)
        if self.session_supports_turns(workflow_id):
            self.session_turn_rows(workflow_id)
        if self.session_supports_tools(workflow_id):
            self.session_tool_rows(workflow_id)
        if self.session_supports_context(workflow_id):
            self.session_context_rows(workflow_id)

    def session_node_rows(self, workflow_id: str) -> list[dict]:
        # Subagent tree for one session, fetched once and cached. The store call is the
        # heavy bit (a recursive CTE / backend parse) and detail_subagents runs on every
        # paint, so memoize like session_tool_rows; the store already demo-scales nodes,
        # and _priced_nodes copies rows before repricing, so the memo stays pristine.
        cached = self._nodes_by_session.get(workflow_id)
        if cached is not None:
            return cached
        rows = [dict(r) for r in self.store.workflow_nodes(workflow_id)]
        self._nodes_by_session[workflow_id] = rows
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
                    # The 1h-TTL subset of that cache_write, when the backend could see
                    # one (Claude Code only). Absent => 0 => the old 5m-rate arithmetic.
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
        # The `w` what-if target has no say here -- it is session-scoped (it reprices
        # the Subagents tab's tree table and nothing else), so "$" keeps owning every
        # app-wide figure whether or not a target is armed.
        api = self.show_api_prices and not self.store.demo
        for w in self.loaded:
            w.total_cost = w.api_total_cost if api else w.real_total_cost
            w.root_cost = w.api_root_cost if api else w.real_root_cost
        for rows in self._model_by_root.values():
            for m in rows:
                m["cost"] = m.get("api_cost", m["cost"]) if api else m.get("real_cost", m["cost"])

    # --- What-if model, the `w` key (session-scoped) ---------------------------
    def whatif_candidates(self) -> list[tuple[str, int]]:
        # The `w` picker's rows: every model you have actually used, most-used first,
        # with the tokens it burned. Same source as priced_model_entries (the loaded
        # model rows), minus the ones with no list price to substitute in
        # (pricing.has_known_price): a local model has no API rate at all (it would
        # price a whole tree at $0 and call it a saving), and an unpriced one -- an id
        # too new for the catalog, or the literal "unknown (not recorded)" some backends
        # log -- resolves only to the generic FALLBACK_PRICE, so arming it would quote
        # "$2.00 at unknown (not recorded) list rates", a rate that exists nowhere.
        # A target you can choose must be a target we can actually price.
        totals: dict[str, int] = defaultdict(int)
        for rows in self._model_by_root.values():
            for m in rows:
                name = str(m.get("model_name") or "")
                if not name or not has_known_price(name):
                    continue
                totals[name] += int(m.get("tokens_total") or 0)
        # A model row can carry zero tokens -- OpenCode emits one for an assistant record
        # whose usage never landed (an aborted turn). It names a model but is not usage,
        # so it must not float a model into the picker, and above all must not keep a
        # stale target alive through _revalidate_whatif on a dataset that never really
        # used it.
        return sorted(
            ((name, tok) for name, tok in totals.items() if tok > 0),
            key=lambda kv: (-kv[1], kv[0]),
        )

    def whatif_catalog_candidates(self) -> list[tuple[str, float, bool]]:
        # The picker's second tier (Tab): every model in the models.dev catalog, not
        # just the ones you've used -- a user who lives on one subscription model
        # still deserves targets to compare against. One row per canonical model as
        # (name, eff $/M, approx): date-pinned aliases fold onto one spelling (the P
        # overlay's rule -- same billed model, same list price), the same model's
        # gateway resale rows fold too, because arming prices through model_price(),
        # where the vendor's own rate wins -- so per-route rows would all arm the
        # SAME rate card and only pad the list. The kept spelling is the one whose
        # resolved price is most complete (a date pin can reach a rate card its
        # plain alias misses), vendor route first, so the row's eff is computed from
        # exactly the rates arming it would use. Local providers and $0-rate models
        # are excluded like everywhere else (no API rate to substitute in); rows are
        # cheapest-for-your-mix first, the P models.dev leaderboard order. Memoized:
        # the list is asked per keystroke while the picker is open, and only the
        # token mix (model scan) or a price refresh can change it.
        if self._whatif_catalog_rows is not None:
            return self._whatif_catalog_rows
        # App-wide mix, NOT price_token_mix (which is `M`-machine-scoped): the what-if
        # machinery is deliberately app-wide and never narrows to the machine filter, so
        # arming `M` must not re-rank this tier -- and _whatif_catalog_rows, cached here,
        # is invalidated only by the model scan / a price refresh, never by an `M` change.
        mix = self._token_mix(self._model_by_root)
        shares = mix[0] if mix else (1.0, 0.0, 0.0, 0.0)
        best: dict[str, tuple[tuple, str]] = {}
        for pid, mid, price, _status in catalog_models():
            if pid.lower() in LOCAL_PROVIDERS or (price[0] <= 0 and price[1] <= 0):
                continue
            bare = mid.rsplit("/", 1)[-1].lower()
            rank = (
                is_vendor_route(pid, bare),  # same vendor-wins rule the catalog resolves by
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
        """The armed target's two figures for ONE session: (your models, all at target),
        BOTH at list prices. None when no target is armed, or the session has no
        per-model rows to price (nothing to compare).

        Both sides are computed from that session's per-model breakdown rows
        (`_model_by_root`), the one place its tokens are split PER MODEL:

        * your models = sum over those rows of each model's own tokens at its own list
          rates -- every token, exactly, whatever mix of models produced it;
        * all at target = the session's summed token split at the target's list rates.

        Two things follow, and they are the whole point. **Both bases are list rates**,
        so the comparison is apples-to-apples: a subscription backend records $0, and
        measuring a real counterfactual against that unrecorded $0 would report a 100%
        saving that never happened; a *partially* metered session (some turns billed,
        most on a subscription) is the same bug in miniature and is common in real data.
        And **arming a model a single-model session already used lands on exactly $0
        change**, because both sides then price the same tokens at the same rates.

        The session's *node* rows can't do this: `workflow_nodes` labels each node with
        its one dominant model, so pricing a node's whole token split at that label is
        wrong for any node that switched model mid-flight (measured against real data:
        73 of 147 multi-model sessions, worst case 47% off), and a node's recorded cost
        keeps a partially-billed node's few cents as its entire baseline. A per-node
        baseline is not computable from what the stores expose -- so the Subagents tab
        shows no per-node baseline and no per-node delta, only this exact session total.

        (The Subagents tab's per-node What-if column IS exact per node and normally sums
        to the counterfactual here, since both count the same tokens. In the rare session
        whose node rollup disagrees with its message-level aggregate -- 2 of 1006 on real
        data, an OpenCode session-column vs message-table drift that predates this
        feature and already splits its Models and Subagents tabs -- the column adds up to
        slightly less than the TOTAL. The per-model split is the one that prices tokens
        correctly, so the total is taken from it and the column is left alone.)
        """
        target = self.whatif_model
        if not target:
            return None
        rows = self._model_by_root.get(workflow.id) or []
        if not rows:
            return None
        baseline = 0.0
        tokens = [0.0, 0.0, 0.0, 0.0, 0.0]  # input, output, reasoning, cache_read, cache_write
        long_write = 0.0  # the 1h-TTL subset of that cache_write, billed at the long rate
        for m in rows:
            split = model_row_split(m)
            long_1h = model_row_1h_write(m)
            baseline += api_equivalent_cost(str(m.get("model_name") or ""), *split, long_1h)
            tokens = [a + b for a, b in zip(tokens, split)]
            long_write += long_1h
        # Both sides carry the subset, which is what keeps the invariant exact: arming a
        # model a single-model session already used is still a $0 change, because the two
        # sides then price the same tokens -- long-TTL writes included -- at the same rates.
        return baseline, api_equivalent_cost(target, *tokens, long_write)

    def token_economics(self, workflows: list[Workflow]) -> TokenEconomics | None:
        """Split a scope's tokens AND its list-rate cost across the five token types.
        None when nothing in the scope has priceable usage.

        Built from the per-model breakdown rows (`_model_by_root`) for the same reason
        the what-if baseline is: they are the one place a session's tokens are split per
        model, and pricing a token type needs the rate card of the model that produced
        it. A node row carries one dominant model label and would misprice any session
        that switched models mid-flight.

        Local models are excluded from BOTH rows, not just the cost one (the rule
        `_token_mix` and the P overlay already follow). They have no API rate, so their
        tokens can only be priced at a generic guess -- and leaving them in the volume
        row while dropping them from the cost row would invent a token type that looks
        free. Excluded tokens are reported separately (`local_tokens`) rather than
        silently dropped.

        The arithmetic is api_equivalent_cost's, kept in pieces instead of summed:
        input at the input rate, output AND reasoning at the output rate, cache reads
        and writes at their own. That is what makes the five parts add up to the "$"
        figure shown everywhere else -- including its known soft spot, a model whose
        cache-read rate is missing from the catalog, whose reads then price at $0
        (flagged as `missing_cache_rate`; effective_price bills that case at the input
        rate instead, but this is a decomposition of a total the app already prints, so
        it has to use the same arithmetic as the total).
        """
        tokens = [0.0] * len(TOKEN_TYPES)
        cost = [0.0] * len(TOKEN_TYPES)
        estimated = missing_cache_rate = False
        local_tokens = 0
        for workflow in workflows:
            for row in self._model_by_root.get(workflow.id) or []:
                name = str(row.get("model_name") or "")
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
                cost[2] += reasoning * orr / 1e6  # reasoning bills at the output rate
                cost[3] += cache_read * crr / 1e6
                cost[4] += cache_write * cwr / 1e6
                # A missing (zero) cache-read rate is not free reads: those tokens price
                # at $0 here, so the Cache read row understates, and the table says so.
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
        # Segment names, made unique. Most backends don't name their subagents -- Claude
        # Code writes "subagent" for every Task -- so a whole session can collapse to one
        # repeated label, and a legend of six identical entries identifies nothing.
        #
        # A repeated label takes its START TIME, because that is the one distinguishing
        # field that is also FINDABLE: it is the table's Started column, whichever way
        # that table happens to be sorted. Minute precision first (short, and enough
        # when the executions are spread out), seconds when a batch launched inside one
        # minute -- five parallel Tasks do -- and a plain cost rank if even that ties,
        # so the labels are unique no matter what the timestamps look like.
        labels = [flame_label(row) for row in rows]
        repeated = {lab for lab in labels if labels.count(lab) > 1}
        if not repeated:
            return labels
        for end in (16, 19):  # "HH:MM", then "HH:MM:SS"
            stamped = [
                f"{lab} {str(row.get('created_at') or '')[11:end]}".strip()
                if lab in repeated
                else lab
                for lab, row in zip(labels, rows)
            ]
            if len(set(stamped)) == len(stamped):
                return stamped
        # Last rung: the cost rank, which is the table's default ordering. Ranking alone
        # is still not a guarantee -- a node genuinely titled "foo #1" beside two titled
        # "foo" collides with the rank given to one of them -- so whatever is left tied
        # is separated here. The contract this function's name makes is uniqueness; a
        # ladder that ALMOST gets there just relocates the indistinguishable pair.
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
        """This session's spend as a hierarchy -- see SessionFlame for what the shape
        means and why depth is one band. None when there is nothing to divide.

        Built from the memoized node rows through `_priced_nodes`, so a segment's width
        IS its row's Cost cell in the table below: same "$" gating, same estimate rule,
        no second opinion about what a node cost. Zero-value nodes are left out (a
        segment with no width is not a segment) and counted in `silent`, because a
        subagent that ran and recorded nothing is a fact about the data, not an absence.
        """
        nodes = self.session_node_rows(workflow.id)
        if not nodes:
            return None
        priced = self._priced_nodes(nodes)  # same order as `nodes`, costs "$"-repriced
        cost = sum(float(row["cost"] or 0) for row in priced)
        # Dollars unless there are none: a subscription backend with "$" off records $0
        # everywhere, and a hierarchy of zeros is a blank frame. Tokens still answer
        # "where did the work go", which is the same question one price list away.
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
        # Cost-descending, with tokens then the title breaking ties -- a stable order,
        # so two paints of the same session never shuffle the colours.
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
        # Estimated exactly when a WIDTH ON SCREEN is an estimate: "$" on, not demo, and
        # some node that actually got drawn recorded nothing of its own. Asking it of
        # every node instead would let an aborted $0/0-token child -- which contributes
        # no segment at all -- put a "~" on a chart whose every width was recorded (one
        # real session in the corpus is shaped exactly like that).
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
        # Does the baseline lean on a model we have no real rate for? Every token is
        # priced, so an unpriceable model in the mix doesn't skew the count -- it gets
        # FALLBACK_PRICE, a mid-range guess, and the "your models" figure quietly stops
        # being a list price. Cheap to say so (a `~`, the same marker the P overlay's
        # eff column uses for an approximated rate) and dishonest not to.
        # Zero-token rows are skipped: a model named by an aborted turn contributes nothing
        # to the baseline, so it cannot make it an estimate -- flagging one would put a `~`
        # on a figure that is exact.
        return any(
            int(m.get("tokens_total") or 0) > 0
            and not has_known_price(str(m.get("model_name") or ""))
            and not is_local_provider(str(m.get("model_name") or ""))
            for m in self._model_by_root.get(workflow.id) or []
        )

    def whatif_node_price(self, row: dict, target: str) -> float:
        # One node's tokens at the target's list rates -- exact (one model, one rate
        # card, the node's own token split), and the per-node What-if column on the
        # Subagents tab. Nothing else about a node is repriced: its Cost column keeps
        # the ordinary "$"-gated meaning (_priced_nodes), and no per-node baseline or
        # delta is shown, because a node that mixed models has none we can compute.
        return api_equivalent_cost(
            target,
            row["tokens_input"],
            row["tokens_output"],
            row["tokens_reasoning"],
            row["tokens_cache_read"],
            row["tokens_cache_write"],
            # Long-TTL writes stay long-TTL writes on the target model too: the tier is a
            # property of how the prompt was cached, not of which model answered.
            node_1h_write(row),
        )

    def toggle_whatif(self) -> None:
        # `w`: with a target armed, disarm it; otherwise open the picker. Unlike "$",
        # what-if is allowed in demo mode -- demo already scales every token by a hidden
        # per-process factor, so pricing scaled tokens at list rates can't be multiplied
        # back into real dollars, while the ratio the feature exists to show (cheap
        # subagents vs one expensive model) is a ratio of scaled numbers and stays real.
        if self.whatif_model:
            self.clear_whatif_model()
            return
        self._ensure_models()  # needs the per-model token breakdown
        self.whatif_catalog = False  # each open starts on your own models...
        if not self.whatif_candidates():
            # ...unless there are none (a single-model subscription, a dataset with
            # nothing priceable): open straight on the catalog tier instead of
            # refusing -- having used few models is exactly when you need more to
            # compare against.
            if not self.whatif_catalog_candidates():
                self.notify("no models to arm — the price catalog is unavailable", "error")
                return
            self.whatif_catalog = True
        self.whatif_menu_index = 0
        self.whatif_query = ""  # each open starts from the full list
        self.whatif_filter_active = False
        self.whatif_menu = True

    def whatif_rows(self) -> list[tuple]:
        # The picker's visible rows -- the active tier (your models, or the whole
        # models.dev catalog after Tab) narrowed by the live `f` query, through the
        # one shared rule (pricing.model_matches -- id by word-anchored fuzzy match,
        # route by substring, dots==dashes). The P overlay's filter is the same call:
        # two model lists asking the same question must not answer it differently.
        #
        # Rows keep their tier's order (most-used-first, or cheapest-for-your-mix
        # first on the catalog): a filtered list should still answer its tier's
        # question, never re-rank by match quality. Each row leads with the model
        # name; the tail differs per tier (tokens used vs eff $/M), which only
        # draw_whatif_menu reads.
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
        # Tab in the picker: flip between your models and the whole catalog. The
        # query survives (typing "deepseek" over your all-Anthropic history and THEN
        # widening is the expected motion); the highlight re-anchors. A tier with
        # nothing in it isn't offered -- flipping to an empty list would strand the
        # picker on "no model matches" with nothing a backspace could widen.
        other = (
            self.whatif_candidates() if self.whatif_catalog else self.whatif_catalog_candidates()
        )
        if not other:
            return
        self.whatif_catalog = not self.whatif_catalog
        self.whatif_menu_index = 0

    def select_whatif_model(self, name: str) -> None:
        # Arming a target changes NO app-wide number: not a session's cost, not a day,
        # month or project rollup, not Trends, not the session header -- and "$" keeps
        # toggling exactly as it does with no target armed. The target's only effect is
        # the session tree table on the Subagents tab (Renderer._subagents_whatif),
        # which reprices that ONE session's nodes off its own workflow_nodes rows. An
        # app-wide reprice would leave "$" nothing to move (every token already priced
        # at one model's rates) and silently invert the saved preference behind it.
        self.whatif_model = name
        self.notice = f"what-if {name}: see a session's Subagents tab"

    def clear_whatif_model(self) -> None:
        self.whatif_model = None
        self.notice = "what-if off"

    def _whatif_pick(self, rows: list[tuple[str, int]]) -> None:
        # Commit the highlighted row. A query that matches nothing selects nothing --
        # the menu just stays open so the next keystroke can widen it again.
        if not rows:
            return
        self.whatif_menu = False
        self.select_whatif_model(rows[self.whatif_menu_index % len(rows)][0])

    def handle_whatif_menu_key(self, key: int | str) -> bool:
        # The `w` model picker: down/up move, select picks, cancel closes, filter
        # starts the live filter -- the same word-anchored narrowing, on the same
        # keys, as the P overlay's model list, because it is the same question asked
        # of the same rows -- and catalog (Tab / h / l) flips between your models and
        # the whole models.dev catalog. Mirrors handle_source_menu_key otherwise,
        # advance (`w` again) walking the highlight like `H` does.
        if key == 3:  # Ctrl-C still quits
            return False
        if not self.whatif_candidates() and not self.whatif_catalog_candidates():
            self.whatif_menu = False
            return True
        rows = self.whatif_rows()
        if self.whatif_filter_active:
            # The filter is typing: its own keys first, then a catalog-bound key that
            # is NOT a typable character (Tab, arrows) still flips the tier -- h/l stay
            # characters here, and a query must never eat the picker.
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
            self.whatif_menu = False  # cancel, pricing unchanged
        # any other key: ignore and keep the menu open
        return True

    def _handle_whatif_filter_key(self, key: int | str, rows: list[tuple[str, int]]) -> bool:
        # Filter-edit mode inside the picker: printable keys narrow the list live,
        # down/up still move the highlight so you can land on a match without leaving
        # the mode, and select picks it outright -- type, arrow, done. cancel drops
        # the query and hands the keys back to the list rather than closing the
        # picker: losing a mistyped query should not cost you the menu.
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
        self._whatif_catalog_rows = None  # the `w` picker's catalog tier reads those rates
        self._ensure_models()
        self._compute_api_costs()
        self._apply_price_mode()
        # A refresh can drop a model from the catalog (a rename, a removed provider), so an
        # armed target may have just lost its list rate. _ensure_models is a no-op here
        # (models already loaded), so its usual revalidation never runs -- do it by hand,
        # or the target stays armed and silently reprices at the generic FALLBACK_PRICE.
        self._revalidate_whatif()
        self.prices_scroll = 0
        self.notify(f"refreshed {count} model prices from models.dev", "success")

    def unknown_priced_models(self) -> list[str]:
        # Used, non-local models with no built-in price (they resolve to nothing better
        # than the generic FALLBACK_PRICE) -- the ones whose $ estimate is a guess until
        # --refresh-models. One rule, pricing.has_known_price, shared with the `w`
        # picker, which refuses to offer these as a target for the same reason.
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

    def handle_price_prompt_key(self, key: int | str) -> bool:
        # accept fetches now; never stops asking (persisted); anything else = not now.
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("prompt.prices", key)
        if act == "accept":
            self.price_prompt = False
            self.refresh_prices_action()  # fetch + reprice in place
        elif act == "never":
            self.price_prompt = False
            self.prices_prompt_dismissed = True  # save_state persists it on exit
            self.notice = f"won't ask again — {self.price_fetch_hint()}"
        else:  # decline, or any other key: not now, ask again next run
            self.price_prompt = False
            self.notice = f"skipped — {self.price_fetch_hint()}"
        return True

    def price_fetch_hint(self) -> str:
        # How to fetch model prices later, with the keys as actually bound -- this
        # trails every way of dismissing the startup prompt.
        return (
            "fetch anytime with --refresh-models or "
            f"{self.keymap.label('prices', 'refresh')} in the "
            f"{self.keymap.label('main', 'prices')} prices view"
        )

    def reload(self) -> None:
        self.loaded = self.store.workflows()
        self._snapshot_real_costs()
        self._resolve_project_roots()
        notes_ok = self.refresh_notes()  # `r` picks up notes another opentab wrote too
        self._tool_by_session.clear()
        self._turns_by_session.clear()
        self.turn_drill = None  # stepped back out with the turn cache it reads from
        self._turn_cursor = 0  # and its cursor, so a fresh session opens on the first prompt
        self._context_by_session.clear()
        self._nodes_by_session.clear()
        self._load_model_cache()
        self._clear_zoom_drills()
        # `r` drops the active mode's drills outright, so the dormant modes' drop too --
        # a reload exists to pick up data that CHANGED, and a snapshot restored unchecked
        # scopes its Sessions list by a harness/project the reload may have just removed.
        self._disarm_mode_memory_drills()
        self._revalidate_machine_filter()  # keep the `M` filter iff its box still exists
        self._revalidate_harness_filter()  # keep the `H` filter iff still a fleet w/ that tool
        self.workflow_index = min(self.workflow_index, max(0, len(self.workflows) - 1))
        self.day_index = min(self.day_index, max(0, len(self.days) - 1))
        self.month_index = min(self.month_index, max(0, len(self.months) - 1))
        self.project_index = min(self.project_index, max(0, len(self.projects) - 1))
        self.machine_index = min(self.machine_index, max(0, len(self.machines) - 1))
        if notes_ok:
            # Toasts set within one handler collapse onto the last one, so a cheery
            # "reloaded" here would swallow refresh_notes' warning. The warning wins:
            # you pressed `r`, you know it reloaded.
            self.notify("reloaded", "success")

    # --- In-TUI machine refresh (the `R` key, fleet view) --------------------
    def can_refresh_machines(self) -> bool:
        # R is offered whenever a fleet is in view: the live box re-scans (a reload), a
        # pulled box re-pulls over ssh (needs the injected backend). Off under demo.
        return self.machines_present and not self.store.demo

    def refresh_target(self) -> str | None:
        # Which box `R` acts on: the selected one in Machines mode, else every pulled box.
        if self.browse_mode == "machines":
            machine = self.selected_machine_summary
            return machine.name if machine else None
        return None  # anywhere else in the fleet view: refresh all pulled boxes

    def _refresh_keys(self, names: list[str] | None) -> list[str]:
        # remotes.json keys for the requested boxes (None = every pulled box). The live
        # local box carries no key -- it is refreshed by a plain reload, not a re-pull.
        meta = self.machine_meta()
        if names is None:
            return [str(m["key"]) for m in meta.values() if (m or {}).get("key")]
        return [str(k) for n in names if (k := (meta.get(n) or {}).get("key"))]

    def request_machine_refresh(self, name: str | None = None) -> None:
        # Hand a re-pull to the run() loop (so a "refreshing…" toast paints before the
        # blocking ssh fetch); refreshing your own live box is just a reload.
        if self.store.demo:
            self.notify("refresh disabled in demo", "error")
            return
        meta = self.machine_meta()
        if name and (meta.get(name) or {}).get("live"):
            self.reload()  # the live box re-scans its own transcripts
            return
        if self._refresh_backend is None:
            self.notify("refresh needs --pull / --remote mode", "error")
            return
        keys = self._refresh_keys([name] if name else None)
        if not keys:
            self.notify("nothing to re-pull (this is your live machine)", "error")
            return
        self._refresh_request = keys
        self.notify(f"refreshing {name or 'all machines'} — ssh…")

    def _rebuild_fleet_store(self) -> None:
        # Re-build the fleet store from scratch so RemoteStore re-reads the summaries a
        # refresh just wrote (workflows() caches _wf from construction, so a plain reload
        # wouldn't pick them up). Rebuild at the CURRENTLY ACTIVE demo state, not the launch
        # args' -- `D` toggles demo live, and rebuilding from self.args.demo would silently
        # flip the refreshed store back. Busts the cached build too, so a later c/D
        # swap-back doesn't restore stale data. Key on the demo *state* (None / the
        # scrambled-category frozenset), not a bool, so it lands in the same cache slot
        # select_source and the D picker use -- a bool key would strand the fresh store
        # and (with a partial-demo state) crash _args_with_demo's sorted(state).
        state = self._store_state_key(self.store)
        self.store = sources.make_store(self._args_with_demo(state), self.source_key)[0]
        self._store_cache[(self.source_key, state)] = self.store

    def refresh_machines_now(self, name: str | None = None) -> list:
        # Synchronous refresh for the web endpoint (no run loop to defer through): fetch,
        # rebuild, reload; returns [(name, count, error)]. Empty when nothing is re-pullable.
        # Gated OFF under demo like the TUI's F: demo must make no network side effects,
        # so a re-pull button clicked on a demo page is a no-op, not a live ssh fetch.
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
        # The blocking half, run from the loop after the toast is on screen: fetch the
        # summaries, then rebuild the fleet store so RemoteStore re-reads them.
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

    # --- Live source switching (the `H` key) ---------------------------------
    def can_switch_source(self) -> bool:
        return len(sources.source_cycle(self.args)) > 1

    @staticmethod
    def _store_state_key(store) -> frozenset | None:
        # A store's _store_cache identity: None when it's real data, else the frozenset
        # of categories it scrambles. demo-all and demo-titles-only are different stores.
        return store.demo_cats if getattr(store, "demo", False) else None

    def _args_with_demo(self, state) -> argparse.Namespace:
        # state: None (real data) or a frozenset of demo categories. Encoded onto
        # args.demo as the comma spec demo_config parses back into (enabled, scale, cats).
        args = copy.copy(self.args)
        args.demo = ",".join(sorted(state)) if state else None
        return args

    def next_source_name(self) -> str:
        # Display name of the source `H` would switch to (for the footer).
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
        # `H` opens a small picker the user can j/k through and Enter to switch (Esc
        # cancels). With a single source there's nothing to pick.
        order = sources.source_cycle(self.args)
        if len(order) < 2:
            self.notify("only one harness available", "error")
            return
        cur = self.source_key if self.source_key in order else order[0]
        self.source_menu_index = order.index(cur)
        self.source_menu = True

    # --- Colour theme (the `C` "Colours" picker; palettes shared with the web) ---
    def theme_menu_entries(self) -> list[tuple[str, str, bool]]:
        # (id, display name, is-active) per theme, in definition order.
        return [(tid, t["name"], tid == self.theme_id) for tid, t in themes.THEMES.items()]

    def open_theme_menu(self) -> None:
        ids = list(themes.THEMES)
        self.theme_menu_index = ids.index(self.theme_id) if self.theme_id in ids else 0
        self._theme_before = self.theme_id  # restored if the picker is cancelled (Esc)
        self.theme_menu = True

    def select_theme(self, theme_id: str, announce: bool = True) -> None:
        # Switch the active theme and re-map the curses colour pairs in place. announce
        # is off for live-preview steps (j/k) so the toast doesn't flood while browsing.
        if theme_id not in themes.THEMES:
            return
        self.theme_id = theme_id
        self.theme = themes.resolve_theme(theme_id)
        # Re-map the colour pairs in place (only reached interactively, so curses is up).
        try:
            self.renderer.init_theme_colors()
        except Exception:  # noqa: BLE001 -- a hostile terminal must never crash a switch
            pass
        if announce:
            self.notice = f"theme: {self.theme['name']}"

    def _preview_theme_at(self, index: int) -> None:
        # Live-apply the highlighted theme as you move (no toast), so the whole UI is
        # the swatch. Enter keeps it; Esc reverts to what was active on open.
        ids = list(themes.THEMES)
        self.theme_menu_index = index % len(ids)
        self.select_theme(ids[self.theme_menu_index], announce=False)

    def handle_theme_menu_key(self, key: int | str) -> bool:
        # down/up live-preview the highlighted theme, select keeps it + closes, cancel
        # reverts to the theme active when the picker opened, advance (`C` again)
        # walks the highlight like every picker's own key.
        if key == 3:  # Ctrl-C still quits
            return False
        act = self.keymap.action("menu.theme", key)
        if act == "cancel":
            self.select_theme(self._theme_before, announce=False)  # cancel -> revert
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
        # Relative hop (kept for completeness); the menu uses select_source directly.
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
        cache_key = (key, self._store_state_key(self.store))  # keep the demo state on switch
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
        if self._notes_ok:  # else keep _reload_for_source's warning (toasts collapse)
            self.notice = f"source: {SOURCE_LABELS.get(key, key)}"

    def toggle_demo(self) -> None:
        # Flip the whole thing on/off (the pre-screenshot path and a plain toggle): to
        # real when in demo, to demo-everything when real. The D picker refines which
        # categories via _apply_demo_state directly.
        state = None if getattr(self.store, "demo", False) else DEMO_ALL
        self._apply_demo_state(state)

    def _apply_demo_state(self, state) -> None:
        # Swap to the store for this demo state -- None (real) or a frozenset of
        # categories -- building and caching it on first use, then reload the view.
        # Shared by toggle_demo and the D category picker.
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
            # The query is text YOU typed -- out of a real title, path, or note -- and the
            # header paints it. Demo exists so the screen can be shared, and the snapshot
            # would restore "filter: Acme acquisition" right onto the anonymised view. It
            # also filters against fake titles now, so it isn't even doing anything.
            self.query = ""
            self._filter_edited()
        if self._notes_ok:  # else keep _reload_for_source's warning (toasts collapse)
            self.notice = self._demo_notice(state)

    @staticmethod
    def _demo_notice(state) -> str:
        # The toast for a demo swap: "real data", "demo mode" (everything), or the
        # partial "demo: titles, spend" so the screen says exactly what's anonymized.
        if not state:
            return "real data"
        if state == DEMO_ALL:
            return "demo mode"
        return "demo: " + ", ".join(sorted(state))

    # --- Demo category picker (the `D` multi-check overlay) --------------------
    _DEMO_CAT_LABELS = {
        "titles": "Titles  — session / prompt / project / model / machine names",
        "turns": "Turns   — the expandable full prompt text",
        "spend": "Spend   — dollars and token magnitudes",
    }

    def demo_action(self) -> None:
        # What `D` does, and it is deliberately asymmetric: ON is a choice (which parts
        # do I want scrambled for this screenshot), OFF never is. Going through the
        # picker to leave demo meant D, uncheck three rows, Enter -- and the app's own
        # idiom everywhere else ($ T P w) is that a LIT footer key turns its thing off
        # when pressed again, so a lit `demo·on` that instead popped a form was the odd
        # one out. Off is now that one press; the categories are remembered so coming
        # back re-offers them (with Enter re-arming exactly what you had).
        if getattr(self.store, "demo", False):
            self.demo_last_sel = self._store_state_key(self.store) or DEMO_ALL
            self._apply_demo_state(None)
            return
        self.open_demo_menu()

    def open_demo_menu(self) -> None:
        # The multi-check picker of what to anonymize. Seeded from the current state --
        # the live categories when already in demo, else the last ones armed this session
        # (all, the ready-to-apply full demo, on the first open), so D then Enter is the
        # quick "anonymize it all" and D-D-Enter restores the mix you were just using.
        self.demo_menu_sel = set(
            self._store_state_key(self.store) or self.demo_last_sel or DEMO_ALL
        )
        self.demo_menu_index = 0
        self.demo_menu = True

    def demo_menu_entries(self) -> list[tuple[str, str, bool]]:
        # (category, label, is-checked) per row, in the canonical titles/turns/spend order.
        return [
            (cat, self._DEMO_CAT_LABELS[cat], cat in self.demo_menu_sel) for cat in DEMO_CATEGORIES
        ]

    def handle_demo_menu_key(self, key: int | str) -> bool:
        # down/up move · toggle checks a category · check_all checks/clears all ·
        # select applies (no category checked = back to real data) · cancel closes.
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
        # Re-seed every per-store cache from the newly active backend and reset the
        # view to the top -- the months/projects/sessions are a different dataset now.
        self.loaded = self.store.workflows()
        self._snapshot_real_costs()
        self._resolve_project_roots()
        self.refresh_notes()  # the new store may be a demo one: re-apply the notes gate
        self._models_loaded = False
        self._tool_by_session.clear()
        self._turns_by_session.clear()
        self.turn_drill = None  # stepped back out with the turn cache it reads from
        self._turn_cursor = 0  # and its cursor, so a fresh session opens on the first prompt
        self._context_by_session.clear()
        self._nodes_by_session.clear()
        self._load_model_cache()
        self._invalidate_workflow_cache()
        # Overlay cursors point into the old dataset (a drilled model / provider may
        # not exist anymore) -- close the drills and re-anchor every chart cursor on
        # the new data's peaks. The overlays themselves stay open: c and D can now be
        # pressed from inside Trends / P, and the swap happens under them in place.
        self.trend_drill = None
        self.trend_drill_index = 0
        self.trend_row_index = 0
        self.trend_cursor = None
        # Where Esc would return to, armed before the swap: it names a tab and a bucket
        # from the old data, and Machines can be gone outright once the fleet is.
        self._trend_return = None
        self.cal_cursor = None
        self.trend_month_index = 0
        self.trend_week_index = 0
        self.trend_year_index = 0
        self.prices_model = None
        self.prices_index = 0
        self.prices_scroll = 0
        self.zoom_source = None  # names a source that may not exist in the new data
        self.source_index = 0
        self.zoom_model = None  # same: a model this data may no longer carry
        self.model_pick_index = 0
        self.zoom_machine = None  # same: a box that may not be in the new data
        self.machine_pick_index = 0
        # ...and the same for the modes we're NOT standing in (a restore keeps a project
        # drill that survived the swap, exactly as the restore branch below does).
        self._disarm_mode_memory_drills(keep_project=restore is not None)
        self._revalidate_machine_filter()  # drop the `M` filter if this source lacks the box
        self._revalidate_harness_filter()  # ...and the `H` harness filter if the fleet is gone
        if restore:
            self.browse_mode = restore["browse_mode"]
            self.focus = restore["focus"]
            self.view = restore["view"]
            # A source/demo swap can drop the fleet (switch to one non-remote backend);
            # Machines mode survives it -- the pulled boxes go, the box you're on stays,
            # so the restored view is one live row rather than an empty list.
            zoom_project = restore["zoom_project"]
            self.zoom_project = (
                zoom_project
                # In Machines mode a project drill is per-box; a refresh could remove it
                # from the selected box while another box keeps it, so a global "still
                # exists" check would leave the Sessions list wrongly filtered (empty).
                # Drop it there, like zoom_source/zoom_model already are (reset above).
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
                # current_session() CLAMPS workflow_index, so a session that vanished in
                # the reload hands back its neighbour -- truthy, so the old "is there a
                # session at all" guard never fired and the detail pane silently became
                # someone else's numbers. Compare identity, as _restore_mode_memory does.
                current = self.current_session()
                saved_session_id = restore["anchor"][5]
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
        if self._notes_ok:  # a broken notes.json outranks "which source am I on"
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
            # Your own annotation rides along — the export is what you take to a
            # spreadsheet (or an invoice), and "why did this session cost that"
            # is exactly the column a spreadsheet can't reconstruct.
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
        header = ["machine", "live", "cost", "tokens", "sessions", "subagents", "exported_at"]
        rows = [
            [m.name, m.live, m.cost, m.tokens, m.workflows, m.subagents, m.exported_at]
            for m in machines
        ]
        return "machines", header, rows

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
        if self.browse_mode == "machines":
            return self._machines_dataset(self.machines)
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

    def _priced_model_roots(self) -> dict[str, list[dict]]:
        # _model_by_root scoped to the active `M` machine and `H` harness filters, so the P
        # overlay -- its mix, rows, per-model drill, and `e` export -- reflects the one box /
        # one tool. No filter armed returns the whole map, so P stays the *all-time* price
        # reference it is for the range: the scope is by MACHINE/HARNESS identity over the
        # full loaded set (never all_workflows, which is also range-scoped) -- an identity
        # narrowing, not a time window. Both compose, like everywhere the filters do.
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
        # (input, output, cache-read, cache-write) shares over every non-local model row
        # in `roots`, plus the tokens they cover. Reasoning bills as output, so it folds
        # in there; a row without an input split (older stores, tests) puts the total's
        # remainder on input. None until there is usage to measure. The caller chooses the
        # scope by which root map it passes -- that is what keeps P (machine-scoped) and the
        # `w` catalog (app-wide) from having to agree.
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
        # The P overlay's token mix -- what its eff column prices at each model's list
        # rates (a cache-heavy mix makes the cache-read rate dominate, which four raw
        # price columns can't show). Machine-scoped via _priced_model_roots, so under an
        # armed `M` filter P blends the one box's mix -- the `H`-harness-picker story.
        return self._token_mix(self._priced_model_roots())

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
        # route, so a model can appear under more than one gateway; the "all" view
        # swaps the row set for the whole models.dev catalog (_catalog_price_entries).
        # Each row carries its usage share and the eff $/M blend of the app-wide mix.
        # Narrowed by the active filter (a plain case-insensitive substring over the
        # model, family, or route), then ordered by _order_price_entries. Shared with
        # the `e` export.
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
        # The active `f` filter, through the one shared rule (pricing.model_matches):
        # the model id by word-anchored fuzzy match, the route and vendor label by
        # substring. The `w` picker's filter asks the same question of the same rows
        # and goes through the same call -- they must never answer it differently.
        # Rows keep the active column sort (a filtered catalog should stay
        # cheapest-first, not re-rank by match quality -- the columns are the point).
        if not self.query:
            return entries
        return [
            e
            for e in entries
            if model_matches(self.query, e.bare, e.routes, family_label(e.family))
        ]

    def _catalog_price_entries(self, shares: tuple) -> list[PriceEntry]:
        # The models.dev view's rows: every model in the price catalog (the bundled
        # snapshot, or the refreshed cache when that's newer), one row per
        # (provider, canonical model), each priced at YOUR token mix -- a
        # cheapest-for-your-mix leaderboard over the whole catalog, used or not. The
        # same model deliberately repeats across providers: gateways resell at their
        # own rates, and that spread is the information. Rows join against your
        # usage by canonical id, so a model you've used keeps its spend/use bar (and
        # a meaningful Enter drill); the rest show a 0 share. Free/$0 models are
        # excluded like local ones (a $0 row would own the cheap end of every sort
        # and pin the heat ramp); a provider's date-pinned aliases fold onto their
        # plain spelling, most completely-priced first.
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
        # Order the entries for the active view, with pinned models first in *every*
        # view (their own sorted block -- the shortlist stays in sight above the
        # ~5k-row catalog). Below that, "flat" (and the catalog view, which is a
        # flat leaderboard) is one globally-sorted list; the grouped views order
        # groups most-spend-first (the empty group -- Other, or a route-less id --
        # always last) and apply the active column sort *within* each.
        pinned = self._sort_price_entries([e for e in entries if e.pinned])
        rest = [e for e in entries if not e.pinned]
        if self.prices_view in ("flat", "all"):
            return pinned + self._sort_price_entries(rest)
        group_spend: dict[str, float] = defaultdict(float)
        for e in rest:
            group_spend[e.group] += e.spend
        groups = sorted(
            {e.group for e in rest},
            key=lambda g: (g == "", -group_spend[g]),  # empty group last, else most spend
        )
        out: list[PriceEntry] = pinned
        for g in groups:
            out.extend(self._sort_price_entries([e for e in rest if e.group == g]))
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
        # _priced_model_roots scopes to the `M` machine filter, so the drill opens only the
        # armed box's sessions (by_id stays over all loaded -- just a root->workflow lookup).
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
        # The P overlay's model price table (per 1M tokens), filter included. One row
        # per distinct model (deduped to the canonical id), with its vendor family,
        # access routes, usage share, and the eff $/M blend of your token mix
        # (eff_approx flags a missing cache-read rate billed at the input rate);
        # every row has a real API rate (local models are excluded).
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
        if tab == "Projects":
            return self._projects_dataset(self.zoom_projects())
        if tab == "Models":
            return self._models_dataset(self.aggregate_models(self._active_scope_workflows()))
        if tab == "Harnesses":
            return self._sources_dataset(self._active_scope_workflows())
        if tab == "Machines":
            return self._machine_agg_dataset(self._active_scope_workflows())
        # Overview / Sessions both sit over the same scoped session list.
        return self._sessions_dataset(self.current_sessions())

    def _active_scope_workflows(self) -> list[Workflow]:
        # The sessions the active zoom detail summarises (for a Models/Sources export).
        if self.browse_mode == "machines":
            machine = self.selected_machine_summary
            return self.workflows_for_machine(machine.name) if machine else []
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

    def _machine_agg_dataset(self, workflows: list[Workflow]) -> tuple[str, list[str], list[list]]:
        # Spend grouped by machine, mirroring the per-scope Machines tab's rollup (the
        # _sources_dataset twin) -- so `e` on that tab exports the box aggregates it shows,
        # not the individual sessions.
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
        if sort_by == "cost":
            return sorted(rows, key=lambda item: (item.total_cost, item.total_tokens), reverse=desc)
        if sort_by == "tokens":
            return sorted(rows, key=lambda item: (item.total_tokens, item.total_cost), reverse=desc)
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
            by_cost = sorted(
                rows, key=lambda item: (item.total_cost, item.total_tokens), reverse=True
            )
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
            base = self.workflows_for_machine(item.name) if item else []
        else:
            base = self.zoom_scope_workflows(include_ignored=self.show_ignored_projects)
        return self.projects_for_workflows(base, include_ignored=self.show_ignored_projects)

    def zoom_selected_project(self) -> ProjectSummary | None:
        rows = self.zoom_projects()
        if not rows:
            return None
        self.project_index = max(0, min(self.project_index, len(rows) - 1))
        return rows[self.project_index]

    def _zoom_picker_scope(self, exclude: str) -> list[Workflow]:
        # The sessions a zoom's Harnesses/Machines picker ranks -- exactly the ones Enter
        # then opens (current_sessions), so it takes the same widenings: `i` (ignored rows
        # in view), a Projects-tab drill (zoom_project), and the committed `f` query.
        # Counting a scope you can't open is how a row reads "1 session · $3" and produces
        # two sessions and $5. Crucially it ALSO applies the OTHER dimension's armed drill
        # (h/l can leave a machine/source narrowed while you move to the sibling picker) --
        # everything except the dimension being picked (`exclude`), which the pick SETS. So
        # the Harnesses picker shows sources within an armed box, and vice-versa.
        if self.browse_mode == "machines":
            # The Harnesses picker of a zoomed BOX: rank the harnesses within this machine
            # (the sidebar selection scopes it, not zoom_machine, which stays None here).
            item = self.selected_machine_summary
            rows = self.workflows_for_machine(item.name) if item else []
        elif self.browse_mode == "projects":
            item = self.selected_project_summary
            rows = (
                self.workflows_for_project(
                    item.directory,
                    include_ignored=self.include_ignored_for_project(item),
                )
                if item
                else []
            )
        else:
            rows = self.zoom_scope_workflows(include_ignored=self._showing_ignored_workflows())
            if self.zoom_project:
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
            return self.workflows_for_machine(item.name) if item else []
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
        if self.browse_mode == "machines":
            item = self.selected_machine_summary
            rows = self.workflows_for_machine(item.name) if item else []
            if self.zoom_source:  # a Harnesses-tab drill narrows this box to one harness
                rows = self._drilled(rows, self._match_source, self._clear_source_drill)
            if self.zoom_project:  # a Projects-tab drill narrows this box to one project
                rows = self._drilled(rows, self._match_project, self._clear_project_drill)
            if self.zoom_model:  # a Models-tab drill narrows to sessions that used it
                rows = self._drilled(rows, self._match_model, self._clear_model_drill)
            return self.filtered_sessions(rows)
        if self.browse_mode == "projects":
            item = self.selected_project_summary
            rows = (
                self.workflows_for_project(
                    item.directory,
                    include_ignored=self.include_ignored_for_project(item),
                )
                if item
                else []
            )
        elif self.focus == "years":
            item = self.selected_year_summary
            source = self.ranged_workflows if self._showing_ignored_workflows() else None
            rows = self.workflows_for_year(item.year, source) if item else []
        elif self.focus == "months":
            item = self.selected_month_summary
            source = self.ranged_workflows if self._showing_ignored_workflows() else None
            rows = self.workflows_for_month(item.month, source) if item else []
        else:
            item = self.selected_day_summary
            source = self.ranged_workflows if self._showing_ignored_workflows() else None
            rows = self.workflows_for_day(item.day, source) if item else []
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
        if self.view == "session" or self.browse_mode != "time":
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
        if self.browse_mode in ("projects", "machines"):
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
        saved_session_id = saved["anchor"][5]
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
                if self.browse_mode == "machines":  # a box's drills are mutually exclusive
                    self.zoom_source = self.zoom_project = None
                tabs = self.current_tabs()
                if "Sessions" in tabs:
                    self.tab = tabs.index("Sessions")
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
            if (
                self.active_turn_drill is None
                and self._on_turns_tab()
                and self._move_turn_cursor(delta)
            ):
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
            groups = self.turn_groups(wf.id) if wf else []
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
        # Block on input when nothing is showing; poll while a toast is fading so it
        # can expire on time without a keystroke.
        return self.TOAST_POLL_MS if self.toasts else -1

    def run(self, stdscr: curses.window) -> None:
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
            if self._session_loading is not None:
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
        if kind in ("source", "machine"):
            # The machine key comes from machine_rows, which labels an untagged session
            # with THIS box -- so match by the same rule, or the drill silently opens on
            # an empty list for a row the tab just showed.
            field = self.machine_of if kind == "machine" else (lambda w: w.source or "unknown")
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
        if act == "back":
            # A drilled prompt is the innermost scope on the Turns tab, so Esc leaves it
            # before it starts popping the view stack -- but ONLY while that tab is the
            # one on screen. Left ungated, Esc on Tools or Context silently tore down an
            # invisible drill and was swallowed, so the key appeared to do nothing.
            if self._on_turns_tab() and self.close_turn_drill():
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
            self.tab = (self.tab - 1) % len(self.current_tabs())
            self.scroll = 0
            return True
        if act == "tab_next":
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
            if ordinal is not None:
                # Move the keyboard cursor onto the clicked row first, so j/k pick up
                # from here. The map is empty while a prompt is drilled, so a click on
                # drilled text lands nowhere instead of re-drilling a stale row.
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
