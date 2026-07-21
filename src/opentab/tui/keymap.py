"""The keymap display table: one table, two renderings.

The footer strip and the `?` overlay answer the same question -- "what can I do from
here?" -- so they read one table, never two hand-kept lists. Each `Key` carries the
predicate that decides whether it does anything in the current context (`when`), the
one line the help prints, and, when it earns a place down there, its footer chip. A key
that stops applying disappears from both at once. (The alternative -- a footer that
computes and a help text that recites -- drifts: the footer used to offer `b mark` and
`s sort` while the Trends overlay was open and swallowing them.)

**One short line per key.** This is a cheat sheet, not a manual: you open it to find a
key, not to read about it. The long form -- what `$` estimates, what `w` compares --
lives in docs/keys.md, where there is room to say it properly. Anything here that needs
a paragraph is a key that needs a better name.

**The key labels are computed, never quoted.** An entry names its binding context and
action(s) (`ctx="main"`, `actions=("sort",)`) and the label is read off the App's live
`Keymap` -- so when keymap.conf rebinds sort to `o`, the footer chip says `o sort` and
the help lists `o`, without either being told. A token `"action*"` prints every key
bound to the action ("f  /"); a bare `"action"` prints the primary. The handful of
labels that aren't bindings at all (the mouse row, the panel digits) keep a literal
`keys` override. The same goes for summaries that mention keys: they are callables
that ask the keymap, so the help can never advertise a key that isn't bound.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, NamedTuple, Union

if TYPE_CHECKING:  # annotation-only: the keymap is a leaf, it must not import the App
    from opentab.tui.app import App

Text = Union[str, Callable[["App"], str]]
Ctx = Union[str, Callable[["App"], str]]  # a fixed context, or one the app state picks


def _keys_text(app: App, ctx: str, tokens: tuple[str, ...], between: str, within: str) -> str:
    # Render label tokens against the live bindings: "action" -> its primary key,
    # "action*" -> every key it answers to. An action the user unbound vanishes.
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
    section: str = "global"  # "here" (context) | "nav" | "pickers" | "global"
    ctx: Ctx = "main"  # the binding context read for labels
    actions: tuple[str, ...] = ()  # label tokens ("sort", "filter*"); () = literal keys
    keys: Text | None = None  # literal/callable override (mouse, panel digits)
    summary: Text = ""  # ONE short line; a callable when it depends on where you are
    when: Callable[[App], bool] | None = None  # None = always available
    chip: Text | None = None  # the footer chip's WORD ("sort"); None = help-only
    chip_actions: tuple[str, ...] | None = None  # chip key tokens; None = `actions`
    active: Callable[[App], bool] | None = None  # footer chip lights up
    segments: Callable[[App], list] | None = None  # composite chip (Tab yr/mo/day)

    def context(self, app: App) -> str:
        return self.ctx(app) if callable(self.ctx) else self.ctx

    def shown(self, app: App) -> bool:
        return self.when is None or bool(self.when(app))

    def text(self, app: App) -> str:
        return self.summary(app) if callable(self.summary) else self.summary

    def label(self, app: App) -> str:
        # The help overlay's key column, computed from the live bindings (or the
        # literal override where the "keys" aren't bindings: mouse, panel digits).
        if self.keys is not None:
            return self.keys(app) if callable(self.keys) else self.keys
        return _keys_text(app, self.context(app), self.actions, between="  ", within="  ")

    def chip_keys(self, app: App) -> str:
        # The chip's key part, compact: primaries joined with "/" ("h/l", "t/p/m"),
        # a *-token's keys with "," ("f,/").
        return _keys_text(
            app, self.context(app), self.chip_actions or self.actions, between="/", within=","
        )

    def chip_segments(self, app: App) -> list[tuple[str, bool]]:
        # What draw_footer paints: one segment, or several so a single token inside a
        # hint can light up on its own ("Tab yr/mo/day").
        if self.segments is not None:
            segs = self.segments(app)
            if segs:
                return segs
        if self.chip is None:
            return []
        word = self.chip(app) if callable(self.chip) else self.chip
        keys = self.chip_keys(app)
        if not keys and (self.chip_actions or self.actions):
            # The action was unbound: a chip with no key is an offer nobody can take.
            # The help overlay drops the entry the same way, so the two agree.
            return []
        label = f"{keys} {word}" if keys else str(word)
        return [(label, bool(self.active(app)) if self.active else False)]


# --- where are we? ------------------------------------------------------------------
# The overlays own the keyboard while they are open (their handlers swallow everything
# they don't bind), so they are contexts in their own right -- not decorations on top
# of the view underneath. `help` is not one: it is what asks the question.


# Precedence is handle_key's, not the screen's: P opens the price table from INSIDE
# Trends (both flags stay true) and the prices branch is checked first, so it owns the
# keyboard. A context that claimed otherwise would advertise Trends' keys to a table
# that swallows them.
def in_prices(app: App) -> bool:
    return bool(app.show_prices)


def in_price_list(app: App) -> bool:
    # The model table itself -- view/pin/select/refresh/sort/filter/export live here...
    return in_prices(app) and app.prices_model is None


def in_price_drill(app: App) -> bool:
    # ...and none of them do in a model's session list, which only scrolls and steps
    # back out.
    return in_prices(app) and app.prices_model is not None


def in_trends(app: App) -> bool:
    return bool(app.trends) and not in_prices(app)


def in_main(app: App) -> bool:
    # The browse -> zoom -> session stack, i.e. no overlay is eating the keys.
    return not app.trends and not app.show_prices


def in_zoom(app: App) -> bool:
    return in_main(app) and app.view == "zoom"


def in_session(app: App) -> bool:
    return in_main(app) and app.view == "session"


def _sort_ctx(app: App) -> str:
    # sort/filter/export act on the price table when it's up -- their labels must
    # then read the [prices] bindings, not [main]'s.
    return "prices" if in_prices(app) else "main"


def context_label(app: App) -> str:
    # Names the "Here" section -- the same words the breadcrumb and the tabs use, so the
    # section title reads as the place you are looking at.
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
    if app.browse_mode == "projects":
        return "browse · Projects"
    return f"browse · {app.focus.capitalize()}"


def trend_tab(app: App) -> str:
    return app.trend_tabs[app.trend_tab % len(app.trend_tabs)]


def _ranked_trend(app: App) -> bool:
    # The Models / Providers / Harnesses tabs: rows, not bars.
    return trend_tab(app) in ("Models", "Providers", "Harnesses")


def _trend_pager_alias(app: App) -> str:
    # The bracket aliases for the pager, as bound ("[ ]") -- "" when unbound.
    return _keys_text(app, "trends", ("older", "newer"), between=" ", within=" ")


def _trend_jk(app: App) -> str:
    # down/up is the one Trends pair whose job changes per tab -- say which one it is
    # doing (and name the aliases as they are actually bound).
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
    # The focused-chart cursor keys, as bound ("← ↑ ↓ →" by default).
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


def _enter_opens_something(app: App) -> bool:
    # select is the one key whose meaning IS the context: it drills from browse, opens
    # a row on the pickerized tabs, and does nothing on Overview/Models or in a session.
    if not in_main(app):
        return False
    if app.view == "browse":
        return True
    if app.view == "zoom":
        return app.active_tab_name() in ("Sessions", "Projects", "Harnesses")
    if _on_turns(app):  # select folds/unfolds the selected ▸ prompt group
        return True
    return False


def _enter_summary(app: App) -> str:
    if app.view == "browse":
        what = "project" if app.browse_mode == "projects" else app.focus.rstrip("s")
        return f"drill into the selected {what}"
    tab = app.active_tab_name()
    if tab == "Sessions":
        return "open the selected session"
    if tab == "Turns":
        return "fold / unfold the selected ▸ prompt"
    return "its sessions, within this scope"


def _aliases_summary(app: App, ctx: str, actions: tuple[str, ...], base: str) -> str:
    # "move / scroll (↑ ↓ too)" -- the parenthetical is every SECONDARY key of the
    # actions, so it follows a remap and vanishes when the aliases are unbound.
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
    tokens = (
        ("mode_time", "mode_projects", "mode_machines")
        if app.machines_present
        else (
            "mode_time",
            "mode_projects",
        )
    )
    return _keys_text(app, "main", tokens, between="  ", within="  ")


def _mode_segments(app: App) -> list:
    t = app.keymap.label("main", "mode_time")
    p = app.keymap.label("main", "mode_projects")
    m = app.keymap.label("main", "mode_machines")
    segs = [(t, app.browse_mode == "time"), ("/", False), (p, app.browse_mode == "projects")]
    if app.machines_present:
        segs += [("/", False), (m, app.browse_mode == "machines")]
    return segs + [(" mode", False)]


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


# --- the table ----------------------------------------------------------------------
# Help renders it in section order (Here · Navigation · Pickers · Global); the footer
# renders FOOTER_ORDER. Two orderings, one set of facts.

KEYS: tuple[Key, ...] = (
    # ---- Here: the main views (browse -> zoom -> session) --------------------------
    Key(
        id="enter",
        ctx="main",
        actions=("select",),
        summary=_enter_summary,
        section="here",
        when=_enter_opens_something,
        chip="in",
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
        id="unfold",
        ctx="main",
        actions=("fold_turns",),
        summary="unfold every ▸ prompt to its full text",
        section="here",
        when=_on_turns,
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
    # ---- Here: the Trends overlay ---------------------------------------------------
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
        # Monthly has one chart and nothing to page: down/up does nothing there.
        when=lambda app: in_trends(app) and bool(_trend_jk(app)),
        chip=lambda app: "rows" if _ranked_trend(app) or app.trend_drill else "page",
    ),
    Key(
        id="trends-shades",
        ctx="trends",
        actions=("shades_more", "shades_less"),
        summary="more / fewer heat shades",
        section="here",
        when=lambda app: in_trends(app) and trend_tab(app) == "Calendar",
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
    # ---- Here: the Prices overlay ---------------------------------------------------
    Key(
        id="prices-view",
        ctx="prices",
        actions=("cycle_view", "tab_prev", "tab_next"),
        summary="view: flat · vendor · provider · models.dev",
        section="here",
        when=in_price_list,
        chip="view",
        chip_actions=("cycle_view",),
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
    # ---- Navigation -----------------------------------------------------------------
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
        summary=lambda app: "Time / Projects / Machines browse mode"
        if app.machines_present
        else "Time / Projects browse mode",
        section="nav",
        # Works from a drilled-in session too (set_browse_mode snapshots it), so advertise
        # it there -- returning to the mode lands back on that session.
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
        summary="step back out — session → zoom → browse",
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
            "pick a ▸ prompt" if _on_turns(app) else "move / scroll",
        ),
        section="nav",
        when=lambda app: not in_trends(app),  # Trends binds down/up itself -- its own entry
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
        summary=lambda app: "first / last prompt" if _on_turns(app) else "top / bottom",
        section="nav",
        when=lambda app: not in_trends(app) or app.trend_drill is not None,
    ),
    Key(
        id="mouse",
        keys="mouse",
        summary="click selects · double-click drills · header sorts",
        section="nav",
    ),
    # ---- Pickers --------------------------------------------------------------------
    # The GLOBAL modal choosers, all handled in the same pre-overlay slot (they float above
    # Trends/Prices/help): pop a list, pick one (D's is a multi-check). Context-gated pickers
    # (s sort, L launch) stay in "Here" where they apply; F (an ssh action) stays in "Global".
    Key(
        id="source",
        ctx="main",
        actions=("harness",),
        # In a fleet the harness key FILTERS by harness (keeps every machine); elsewhere
        # it swaps the backend store. Available whenever either applies -- a fleet with a
        # single local source still filters, so machines_present widens the gate.
        summary=lambda app: "filter harness (fleet)" if app.machines_present else "switch harness",
        section="pickers",
        # Shown when there's actually something to do: a backend swap available, or a fleet
        # harness filter with >=2 harnesses / one armed to clear (never a bare single-harness
        # fleet no-op, and never an armed filter you can't reach to clear).
        when=lambda app: app.can_switch_source() or app.can_harness_filter(),
        chip=lambda app: app.harness_filter if app.harness_filter else "harness",
        active=lambda app: app.source_menu or app.harness_menu or bool(app.harness_filter),
    ),
    Key(
        id="machine-filter",
        ctx="main",
        actions=("machine",),
        # Twin of the harness key: not in_main-gated, floats above Trends/Prices/help
        # (handled there in the overlay-common paths), so machines_present is the whole gate.
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
        when=in_main,
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
        summary="anonymize for a screenshot — pick titles / turns / spend",
        section="pickers",
        when=lambda app: bool(app.source_key),
        chip=lambda app: "demo·on" if app.store.demo else "demo",
        active=lambda app: app.demo_menu or bool(getattr(app.store, "demo", False)),
    ),
    # ---- Global ---------------------------------------------------------------------
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
        # A fleet ACTION (an ssh re-fetch), not movement -- it belongs in Global with the
        # other things that always work when a fleet is present, not under Navigation.
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

# The footer's own order: motion, then what you can do here, then the globals. It is
# spelled out rather than derived from the table, because the help reads best grouped by
# section and the footer reads best grouped by hand.
FOOTER_ORDER = (
    "tab-focus",
    "trends-close",
    "prices-close",
    "price-drill-back",
    "price-drill-close",
    "trends-tabs",
    "trends-page",
    "trends-enter",
    "prices-view",
    "prices-pin",
    "prices-enter",
    "enter",
    "esc",
    "max",
    "mode",
    "machine-filter",  # both fleet-gated: shown only when a fleet is in view
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
    # (title, entries) for the help overlay: what works here, how to move, what always works.
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
    # The chips draw_footer paints, in FOOTER_ORDER: an entry with a chip, shown here.
    parts: list = []
    for key_id in FOOTER_ORDER:
        entry = BY_ID[key_id]
        if entry.chip is None and entry.segments is None:
            continue
        if not entry.shown(app):
            continue
        segs = entry.chip_segments(app)
        if segs:
            parts.append(segs)
    return parts
