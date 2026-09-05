"""Shared key metadata for the footer and help overlay.

Availability predicates keep both renderings synchronized. Labels come from the live
binding map: bare action tokens show the primary key and ``action*`` shows every key;
literal overrides are reserved for non-bindings such as mouse and panel labels. Keep
summaries short; detailed behavior belongs in the key documentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, NamedTuple, Union

if TYPE_CHECKING:  # Avoid a runtime App import cycle.
    from opentab.tui.app import App

Text = Union[str, Callable[["App"], str]]
Ctx = Union[str, Callable[["App"], str]]


def _keys_text(app: App, ctx: str, tokens: tuple[str, ...], between: str, within: str) -> str:
    # Unbound actions vanish rather than advertising unusable keys.
    parts = []
    for token in tokens:
        every = token.endswith("*")
        labels = app.keymap.labels(ctx, token.rstrip("*"))
        if not labels:
            continue
        parts.append(within.join(labels) if every else labels[0])
    return between.join(parts)


class Key(NamedTuple):
    id: str
    section: str = "global"
    ctx: Ctx = "main"
    actions: tuple[str, ...] = ()
    keys: Text | None = None
    summary: Text = ""
    when: Callable[[App], bool] | None = None
    chip: Text | None = None
    chip_actions: tuple[str, ...] | None = None
    active: Callable[[App], bool] | None = None
    segments: Callable[[App], list] | None = None

    def context(self, app: App) -> str:
        return self.ctx(app) if callable(self.ctx) else self.ctx

    def shown(self, app: App) -> bool:
        return self.when is None or bool(self.when(app))

    def text(self, app: App) -> str:
        return self.summary(app) if callable(self.summary) else self.summary

    def label(self, app: App) -> str:
        if self.keys is not None:
            return self.keys(app) if callable(self.keys) else self.keys
        return _keys_text(app, self.context(app), self.actions, between="  ", within="  ")

    def chip_keys(self, app: App) -> str:
        return _keys_text(
            app, self.context(app), self.chip_actions or self.actions, between="/", within=","
        )

    def chip_segments(self, app: App) -> list[tuple[str, bool]]:
        if self.segments is not None:
            segs = self.segments(app)
            if segs:
                return segs
        if self.chip is None:
            return []
        word = self.chip(app) if callable(self.chip) else self.chip
        keys = self.chip_keys(app)
        if not keys and (self.chip_actions or self.actions):
            # Never offer a footer action the user explicitly unbound.
            return []
        label = f"{keys} {word}" if keys else str(word)
        return [(label, bool(self.active(app)) if self.active else False)]


# Context follows handle_key precedence, not visible stacked flags. Prices can open over
# Trends and then own the keyboard; advertising the covered overlay's keys would be wrong.
def in_prices(app: App) -> bool:
    return bool(app.show_prices)


def in_price_list(app: App) -> bool:
    return in_prices(app) and app.prices_model is None


def in_price_drill(app: App) -> bool:
    return in_prices(app) and app.prices_model is not None


def in_trends(app: App) -> bool:
    return bool(app.trends) and not in_prices(app)


def in_main(app: App) -> bool:
    return not app.trends and not app.show_prices


def in_zoom(app: App) -> bool:
    return in_main(app) and app.view == "zoom"


def in_session(app: App) -> bool:
    return in_main(app) and app.view == "session"


def _sort_ctx(app: App) -> str:
    return "prices" if in_prices(app) else "main"


def context_label(app: App) -> str:
    if in_price_drill(app):
        return "Prices · sessions"
    if app.show_prices:
        return "Prices"
    if app.trends:
        return f"Trends · {trend_tab(app)}"
    tab = app.active_tab_name()
    if app.view == "session":
        return f"session · {tab}"
    if app.view == "zoom":
        return f"zoom · {tab}"
    if app.flat_browse_mode:
        # Flat modes have no focused sidebar panel; use the mode table's label.
        return f"browse · {app.browse_mode_spec.label}"
    return f"browse · {app.focus.capitalize()}"


def trend_tab(app: App) -> str:
    return app.trend_tabs[app.trend_tab % len(app.trend_tabs)]


def _ranked_trend(app: App) -> bool:
    # Sortable-column vocabulary is the single source for which tabs are ranked rows.
    return bool(app.trend_sort_options())


def _trend_pager_alias(app: App) -> str:
    return _keys_text(app, "trends", ("older", "newer"), between=" ", within=" ")


def _trend_jk(app: App) -> str:
    if app.trend_drill is not None:
        return "move in the list"
    if _ranked_trend(app):
        return "move the row cursor"
    alias = _trend_pager_alias(app)
    suffix = f" ({alias} too)" if alias else ""
    return {
        "Daily": f"page the month{suffix}",
        "Weekly": f"page the week{suffix}",
        "Calendar": f"page the year{suffix}",
    }.get(trend_tab(app), "")


def _chart_arrows(app: App) -> str:
    return _keys_text(
        app,
        "trends.chart",
        ("cursor_left", "cursor_up", "cursor_down", "cursor_right"),
        between=" ",
        within=" ",
    )


def _trend_enter(app: App) -> str:
    if app.trend_drill is not None:
        return "open the session"
    if _ranked_trend(app):
        return "the sessions behind this row"
    if app.trend_focus:
        return "drill into the picked bar / day"
    return f"focus the chart — then {_chart_arrows(app)} pick"


def _trend_close_summary(app: App) -> str:
    if app.trend_focus or app.trend_drill is not None:
        back = app.keymap.label("trends", "back")
        close = _keys_text(app, "trends", ("close*",), between="", within=" / ")
        return f"leave the focused chart / the drill ({back}), or close ({close})"
    return "close"


def _on_turns(app: App) -> bool:
    return in_session(app) and app.active_tab_name() == "Turns"


def _on_trace(app: App) -> bool:
    return in_main(app) and _on_turns(app) and app.active_trace_drill is not None


def _enter_opens_something(app: App) -> bool:
    if not in_main(app):
        return False
    if app.view == "browse":
        return True
    if app.view == "zoom":
        # Every pickerized tab, not just the three that shipped first: Models and
        # Machines drill too, and a key the footer hides is a key nobody finds.
        return app.active_tab_name() in ("Sessions", "Projects", "Harnesses", "Models", "Machines")
    if _on_turns(app):
        if app.active_trace_drill is not None:
            return not app.trace_expanded and app.renderer.trace_output_target() is not None
        wf = app.current_session()
        return app.active_turn_drill is None or (
            wf is not None and app.session_supports_trace(wf.id)
        )
    return False


def _enter_summary(app: App) -> str:
    if app.view == "browse":
        what = "project" if app.browse_mode == "projects" else app.focus.rstrip("s")
        return f"drill into the selected {what}"
    tab = app.active_tab_name()
    if tab == "Sessions":
        return "open the selected session"
    if tab == "Turns":
        if app.active_trace_drill is not None:
            return "expand / collapse the output at the top of the viewport (or the next below)"
        return (
            "open the selected turn"
            if app.active_turn_drill is not None
            else "open the selected prompt"
        )
    if tab == "Models":
        return "this model's economics and sessions, within this scope"
    return "its sessions, within this scope"


def _aliases_summary(app: App, ctx: str, actions: tuple[str, ...], base: str) -> str:
    extra = " ".join(lab for a in actions for lab in app.keymap.labels(ctx, a)[1:])
    return f"{base} ({extra} too)" if extra else base


def _panel_keys_label(app: App) -> str:
    digits = _keys_text(app, "main", ("panel_1",), between=" ", within=" ")
    if app.browse_mode != "projects":
        digits = _keys_text(app, "main", ("panel_1", "panel_2", "panel_3"), between=" ", within=" ")
    detail = app.keymap.label("main", "panel_detail")
    return f"{digits}  {detail}" if detail else digits


def _panel_summary(app: App) -> str:
    p1 = app.keymap.label("main", "panel_1")
    p0 = app.keymap.label("main", "panel_detail")
    if app.browse_mode == "projects":
        return f"{p1} the Projects list · {p0} the detail pane"
    return "jump to a panel — its number is in its title"


def _mode_keys(app: App) -> str:
    tokens = tuple(mode.action for mode in app.BROWSE_MODES)
    return _keys_text(app, "main", tokens, between="  ", within="  ")


def _mode_segments(app: App) -> list:
    # Derive from BROWSE_MODES so newly registered modes reach the footer automatically.
    segs: list = []
    for i, mode in enumerate(app.BROWSE_MODES):
        if i:
            segs.append(("/", False))
        segs.append((app.keymap.label("main", mode.action), app.browse_mode == mode.key))
    segs.append((" mode", False))
    return segs


def _tab_focus_segments(app: App) -> list:
    if app.view != "browse" or app.browse_mode != "time":
        return []
    tab = app.keymap.label("main", "cycle_panel")
    return [
        (f"{tab} ", False),
        ("yr", app.focus == "years"),
        ("/", False),
        ("mo", app.focus == "months"),
        ("/", False),
        ("day", app.focus == "days"),
    ]


# Help and footer use different orderings over the same entries.

KEYS: tuple[Key, ...] = (
    Key(
        id="trace-scroll",
        ctx="main",
        actions=("down", "up"),
        summary="scroll this turn; the ▸ marker follows the next output section",
        section="here",
        when=_on_trace,
        chip="scroll",
    ),
    Key(
        id="trace-siblings",
        ctx="main",
        actions=("trace_prev", "trace_next"),
        summary="previous / next turn in this prompt",
        section="here",
        when=_on_trace,
        chip="turn",
    ),
    Key(
        id="trace-expand",
        ctx="main",
        actions=("trace_expand",),
        summary="expand the full recorded content / return to preview",
        section="here",
        when=_on_trace,
        chip=lambda app: "collapse" if app.trace_expanded else "expand",
    ),
    Key(
        id="enter",
        ctx="main",
        actions=("select",),
        summary=_enter_summary,
        section="here",
        when=_enter_opens_something,
        chip=lambda app: "output" if _on_trace(app) else "in",
    ),
    Key(
        id="max",
        ctx="main",
        actions=("maximize",),
        summary="maximize / restore the detail pane",
        section="here",
        when=in_zoom,
        chip="max",
        active=lambda app: app.zoom_maximized,
    ),
    Key(
        id="ignore",
        ctx="main",
        actions=("ignore",),
        summary="ignore / unignore the selection",
        section="here",
        when=lambda app: in_main(app) and app.can_toggle_ignore(),
        chip="ignore",
    ),
    Key(
        id="ignored",
        ctx="main",
        actions=("show_ignored",),
        summary="show ignored rows (to unignore them)",
        section="here",
        when=lambda app: in_main(app) and bool(app.ignored_projects or app.ignored_sessions),
        chip="ignored",
        active=lambda app: app.show_ignored_projects,
    ),
    Key(
        id="bookmark",
        ctx="main",
        actions=("bookmark",),
        summary="bookmark ★ this session",
        section="here",
        when=lambda app: in_main(app) and app.bookmark_target() is not None,
        chip="mark",
        active=lambda app: (t := app.bookmark_target()) is not None and t.id in app.bookmarks,
    ),
    Key(
        id="bookmarks",
        ctx="main",
        actions=("show_bookmarks",),
        summary="show only bookmarked sessions",
        section="here",
        when=lambda app: in_main(app) and bool(app.bookmarks or app.show_bookmarks_only),
        chip="marked",
        active=lambda app: app.show_bookmarks_only,
    ),
    Key(
        id="note",
        ctx="main",
        actions=("note",),
        summary="note ✎ this session — why it cost that",
        section="here",
        when=lambda app: in_main(app) and app.allow_notes and app.bookmark_target() is not None,
        chip="note",
        active=lambda app: (t := app.bookmark_target()) is not None and bool(app.note_for(t.id)),
    ),
    Key(
        id="sort",
        ctx=_sort_ctx,
        actions=("sort",),
        summary=lambda app: "sort the price table" if in_price_list(app) else "sort this list",
        section="here",
        when=lambda app: in_price_list(app) or (in_main(app) and app.can_sort_current_view()),
        chip="sort",
        active=lambda app: app.sort_menu,
    ),
    Key(
        id="filter",
        ctx=_sort_ctx,
        actions=("filter*",),
        summary=lambda app: "filter the model list"
        if in_price_list(app)
        else "filter — fuzzy over titles, projects, notes",
        section="here",
        when=lambda app: in_price_list(app) or (in_main(app) and app.can_filter_current_view()),
        chip="filter",
        active=lambda app: bool(app.query),
    ),
    Key(
        id="clear-filter",
        ctx="main",
        actions=("clear_filter",),
        summary="clear the filter",
        section="here",
        when=lambda app: in_main(app) and bool(app.query),
    ),
    Key(
        id="launch",
        ctx="main",
        actions=("launch",),
        summary="resume this session in its own tool",
        section="here",
        when=lambda app: in_main(app) and app.can_launch_current(),
        chip="launch",
        active=lambda app: app.launch_menu is not None,
    ),
    Key(
        id="open",
        ctx="main",
        actions=("open_dir",),
        summary="open its directory",
        section="here",
        when=in_main,
    ),
    Key(
        id="export",
        ctx=_sort_ctx,
        actions=("export",),
        summary=lambda app: "export the price table to CSV"
        if in_prices(app)
        else "export this list to CSV",
        section="here",
        when=lambda app: in_main(app) or in_price_list(app),
    ),
    Key(
        id="trends-tabs",
        ctx="trends",
        actions=("tab_prev", "tab_next"),
        summary="switch tab",
        section="here",
        when=in_trends,
        chip="tabs",
    ),
    Key(
        id="trends-enter",
        ctx="trends",
        actions=("select",),
        summary=_trend_enter,
        section="here",
        when=in_trends,
        chip=lambda app: "drill"
        if (app.trend_focus or app.trend_drill is not None or _ranked_trend(app))
        else "focus",
    ),
    Key(
        id="trends-page",
        ctx="trends",
        actions=("down", "up"),
        summary=_trend_jk,
        section="here",
        # Monthly has no paging dimension.
        when=lambda app: in_trends(app) and bool(_trend_jk(app)),
        chip=lambda app: "rows" if _ranked_trend(app) or app.trend_drill else "page",
    ),
    Key(
        id="trends-sort",
        ctx="trends",
        actions=("sort",),
        summary="order the ranking — cost, name, tokens, count",
        section="here",
        when=lambda app: in_trends(app) and app.in_trend_sort_context(),
        chip="sort",
        active=lambda app: app.sort_menu,
    ),
    Key(
        id="trends-shades",
        ctx="trends",
        actions=("shades_more", "shades_less"),
        summary="more / fewer heat shades",
        section="here",
        when=lambda app: in_trends(app) and trend_tab(app) == "Calendar",
        chip="shades",
    ),
    Key(
        # The focused chart's own movement keys. Only the keybar advertises these now
        # that the tab row carries no hint, so they need a chip of their own -- the
        # arrows read as one unit, hence segments rather than a "/"-joined chip.
        id="trends-chart-cursor",
        ctx="trends.chart",
        actions=("cursor_left", "cursor_up", "cursor_down", "cursor_right"),
        summary="walk the bar / day cursor",
        section="here",
        when=lambda app: in_trends(app) and app.trend_focus,
        segments=lambda app: (
            [(f"{_chart_arrows(app)} move", False)] if _chart_arrows(app) else []
        ),
    ),
    Key(
        id="trends-close",
        ctx="trends",
        actions=("back", "close*"),
        summary=_trend_close_summary,
        section="here",
        when=in_trends,
        chip=lambda app: "back" if (app.trend_focus or app.trend_drill is not None) else "close",
        chip_actions=("back",),
    ),
    Key(
        id="prices-view",
        ctx="prices",
        actions=("cycle_view", "tab_prev", "tab_next"),
        summary="view: flat · vendor · provider · models.dev",
        section="here",
        when=in_price_list,
        chip="view",
        # h/l and the clickable tabs switch views too; the removed tab-row hint used to
        # be where that was said, so the chip carries all three now.
        chip_actions=("cycle_view", "tab_prev", "tab_next"),
    ),
    Key(
        id="prices-pin",
        ctx="prices",
        actions=("pin",),
        summary="pin this model ★ (floats first, in every view)",
        section="here",
        when=in_price_list,
        chip="pin",
    ),
    Key(
        id="prices-enter",
        ctx="prices",
        actions=("select",),
        summary="the sessions that used this model",
        section="here",
        when=in_price_list,
        chip="sessions",
    ),
    Key(
        id="prices-refresh",
        ctx="prices",
        actions=("refresh",),
        summary="refresh the rates from models.dev",
        section="here",
        when=in_price_list,
        chip="refresh",
    ),
    Key(
        id="prices-close",
        ctx="prices",
        actions=("back", "close*"),
        summary="close",
        section="here",
        when=in_price_list,
        chip="close",
        chip_actions=("back",),
    ),
    Key(
        id="price-drill-back",
        ctx="prices.sessions",
        actions=("back",),
        summary="back to the model list",
        section="here",
        when=in_price_drill,
        chip="back",
    ),
    Key(
        id="price-drill-close",
        ctx="prices.sessions",
        actions=("close*",),
        summary="close",
        section="here",
        when=in_price_drill,
        chip="close",
        chip_actions=("close",),
    ),
    Key(
        id="tab-focus",
        ctx="main",
        actions=("cycle_panel",),
        summary=lambda app: "cycle the sidebar panels "
        f"({app.keymap.label('main', 'cycle_panel_back')} back)",
        section="nav",
        when=lambda app: in_main(app) and app.view != "session" and app.browse_mode == "time",
        segments=_tab_focus_segments,
        chip="focus",
    ),
    Key(
        id="panels",
        ctx="main",
        keys=_panel_keys_label,
        summary=_panel_summary,
        section="nav",
        when=in_main,
    ),
    Key(
        id="mode",
        ctx="main",
        keys=_mode_keys,
        summary="Time / Projects / Machines browse mode",
        section="nav",
        # Mode switching snapshots drilled session state and works from a session.
        when=in_main,
        segments=_mode_segments,
        chip="mode",
    ),
    Key(
        id="tabs",
        ctx="main",
        actions=("tab_prev", "tab_next"),
        summary="switch detail tabs",
        section="nav",
        when=in_main,
    ),
    Key(
        id="esc",
        ctx="main",
        actions=("back", "cycle_panel_back"),
        summary=lambda app: "back to this prompt's turns"
        if _on_trace(app)
        else "back to the prompts"
        if _on_turns(app) and app.active_turn_drill is not None
        else "step back out — session → zoom → browse",
        section="nav",
        when=lambda app: in_main(app) and app.view != "browse",
        chip="out",
        chip_actions=("back",),
    ),
    Key(
        id="move",
        ctx="main",
        actions=("down", "up"),
        summary=lambda app: _aliases_summary(
            app,
            "main",
            ("down", "up"),
            "scroll this turn"
            if _on_trace(app)
            else "pick a turn"
            if _on_turns(app) and app.active_turn_drill is not None
            else "pick a prompt"
            if _on_turns(app)
            else "move / scroll",
        ),
        section="nav",
        when=lambda app: not in_trends(app),
    ),
    Key(
        id="page",
        ctx="main",
        actions=("page_down", "page_up"),
        summary=lambda app: _aliases_summary(app, "main", ("page_down", "page_up"), "half a page"),
        section="nav",
        when=lambda app: not in_trends(app) or app.trend_drill is not None,
    ),
    Key(
        id="ends",
        ctx="main",
        actions=("top", "bottom"),
        summary=lambda app: "first / last prompt"
        if _on_turns(app) and app.active_turn_drill is None
        else "top / bottom",
        section="nav",
        when=lambda app: not in_trends(app) or app.trend_drill is not None,
    ),
    Key(
        id="mouse",
        keys="mouse",
        summary="click selects · double-click drills · header sorts",
        section="nav",
    ),
    # Global modal pickers float above overlays; context-specific pickers remain in Here.
    Key(
        id="source",
        ctx="main",
        actions=("harness",),
        summary=lambda app: "filter harness (fleet)" if app.machines_present else "switch harness",
        section="pickers",
        # Keep an armed fleet filter reachable to clear, but hide true no-op menus.
        when=lambda app: app.can_switch_source() or app.can_harness_filter(),
        chip=lambda app: app.harness_filter if app.harness_filter else "harness",
        active=lambda app: app.source_menu or app.harness_menu or bool(app.harness_filter),
    ),
    Key(
        id="machine-filter",
        ctx="main",
        actions=("machine",),
        summary="filter every view to one machine",
        section="pickers",
        when=lambda app: app.machines_present,
        chip=lambda app: app.machine_filter if app.machine_filter else "machine",
        active=lambda app: bool(app.machine_filter) or app.machine_menu,
    ),
    Key(
        id="whatif",
        ctx="main",
        actions=("whatif",),
        summary="what-if — reprice a session at one model",
        section="pickers",
        # Session-scoped, but remain visible elsewhere while armed so it can be cleared.
        when=lambda app: in_session(app) or (in_main(app) and bool(app.whatif_model)),
        chip="model",
        active=lambda app: bool(app.whatif_model),
    ),
    Key(
        id="theme",
        ctx="main",
        actions=("theme",),
        summary="colour theme",
        section="pickers",
        chip=None,
    ),
    Key(
        id="demo",
        ctx="main",
        actions=("demo",),
        summary=lambda app: (
            "back to real data"
            if getattr(app.store, "demo", False)
            else "anonymize for a screenshot — pick titles / turns / spend"
        ),
        section="pickers",
        when=lambda app: bool(app.source_key),
        chip=lambda app: "demo·on" if app.store.demo else "demo",
        active=lambda app: app.demo_menu or bool(getattr(app.store, "demo", False)),
    ),
    Key(
        id="range",
        ctx="main",
        actions=("range",),
        summary="set the range — 30d · 2m · 2026-05 · a..b",
        section="global",
        when=in_main,
        chip="range",
        active=lambda app: app.range_label() != "all time",
    ),
    Key(
        id="all-time",
        ctx="main",
        actions=("all_time",),
        summary="all time",
        section="global",
        when=in_main,
    ),
    Key(
        id="trends",
        ctx="main",
        actions=("trends",),
        summary="trends — charts, calendar heatmap, rankings",
        section="global",
        when=lambda app: in_main(app) or in_trends(app),
        chip="trends",
        active=lambda app: app.trends,
    ),
    Key(
        id="prices",
        ctx="main",
        actions=("prices",),
        summary="model prices — cheapest for your token mix",
        section="global",
        chip="prices",
        active=lambda app: app.show_prices,
    ),
    Key(
        id="dollar",
        ctx="main",
        actions=("api_prices",),
        summary="price subscription usage at API list rates",
        section="global",
        when=lambda app: not app.store.demo,
        chip="what-if",
        active=lambda app: app.show_api_prices,
    ),
    Key(
        id="refresh-machines",
        ctx="main",
        actions=("refresh_machines",),
        summary="re-pull machine summaries over ssh",
        section="global",
        when=lambda app: in_main(app) and app.machines_present,
        chip="refresh",
    ),
    Key(
        id="reload",
        ctx="main",
        actions=("reload",),
        summary="reload",
        section="global",
        when=in_main,
    ),
    Key(
        id="notices",
        ctx="main",
        actions=("notices",),
        summary="notifications — reread the toasts that faded",
        section="global",
        when=in_main,
        active=lambda app: app.toast_history,
    ),
    Key(
        id="keymap",
        ctx="main",
        actions=("edit_keymap",),
        summary="remap any of these — keymap.conf in $EDITOR, live reload",
        section="global",
        when=lambda app: in_main(app) or bool(app.help),
    ),
    Key(
        id="help",
        ctx="main",
        actions=("help",),
        summary="these keys",
        section="global",
        chip="help",
        active=lambda app: app.help,
    ),
    Key(
        id="quit",
        ctx="main",
        actions=("quit",),
        summary="quit",
        section="global",
        when=in_main,
        chip="quit",
    ),
)

BY_ID = {k.id: k for k in KEYS}

# Footer ordering is hand-tuned; help ordering follows sections.
FOOTER_ORDER = (
    "tab-focus",
    "trends-close",
    "prices-close",
    "price-drill-back",
    "price-drill-close",
    "trends-tabs",
    "trends-page",
    "trends-chart-cursor",
    "trends-enter",
    "trends-sort",
    "trends-shades",
    "prices-view",
    "prices-pin",
    "prices-enter",
    "trace-siblings",
    "trace-scroll",
    "trace-expand",
    "enter",
    "esc",
    "max",
    "mode",
    "machine-filter",
    "refresh-machines",
    "ignore",
    "ignored",
    "bookmark",
    "bookmarks",
    "note",
    "source",
    "range",
    "filter",
    "sort",
    "prices-refresh",
    "trends",
    "prices",
    "launch",
    "demo",
    "dollar",
    "whatif",
    "help",
    "quit",
)

SECTIONS = ("here", "nav", "pickers", "global")


def sections(app: App) -> list[tuple[str, list[Key]]]:
    titles = {
        "here": f"Here — {context_label(app)}",
        "nav": "Navigation",
        "pickers": "Pickers",
        "global": "Global",
    }
    out = []
    for name in SECTIONS:
        rows = [k for k in KEYS if k.section == name and k.shown(app) and k.label(app)]
        if rows:
            out.append((titles[name], rows))
    return out


def footer_parts(app: App) -> list:
    parts: list = []
    for key_id in FOOTER_ORDER:
        if _on_trace(app) and key_id not in (
            "trace-siblings",
            "trace-scroll",
            "trace-expand",
            "enter",
            "esc",
            "help",
        ):
            continue
        entry = BY_ID[key_id]
        if entry.chip is None and entry.segments is None:
            continue
        if not entry.shown(app):
            continue
        segs = entry.chip_segments(app)
        if segs:
            parts.append(segs)
    return parts
