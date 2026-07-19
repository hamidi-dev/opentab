"""The keymap: one table, two renderings.

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

`binds` lists the characters an entry documents, including aliases (`S` for `s`, `=`
for `+`), so a test can hold the table against every `ord(...)` the App actually binds
and fail on an undocumented key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, NamedTuple, Union

if TYPE_CHECKING:  # annotation-only: the keymap is a leaf, it must not import the App
    from opentab.tui.app import App

Text = Union[str, Callable[["App"], str]]


class Key(NamedTuple):
    id: str
    keys: Text  # how the binding is written in the help list (callable = context-dependent)
    summary: Text  # ONE short line; a callable when it depends on where you are
    section: str = "global"  # "here" (context) | "nav" | "global"
    when: Callable[[App], bool] | None = None  # None = always available
    chip: Text | None = None  # footer label ("s sort"); None = help-only
    active: Callable[[App], bool] | None = None  # footer chip lights up
    segments: Callable[[App], list] | None = None  # composite chip (Tab yr/mo/day)
    binds: tuple[str, ...] = ()  # the characters this entry documents

    def shown(self, app: App) -> bool:
        return self.when is None or bool(self.when(app))

    def text(self, app: App) -> str:
        return self.summary(app) if callable(self.summary) else self.summary

    def label(self, app: App) -> str:
        # The key column. Contextual where the keys themselves are: Projects mode has one
        # sidebar panel, so printing "1 2 3" there would offer two keys that do nothing.
        return self.keys(app) if callable(self.keys) else self.keys

    def chip_segments(self, app: App) -> list[tuple[str, bool]]:
        # What draw_footer paints: one segment, or several so a single token inside a
        # hint can light up on its own ("Tab yr/mo/day").
        if self.segments is not None:
            segs = self.segments(app)
            if segs:
                return segs
        if self.chip is None:
            return []
        label = self.chip(app) if callable(self.chip) else self.chip
        return [(str(label), bool(self.active(app)) if self.active else False)]


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
    # The model table itself -- p/space/Enter/r/s/f/e live here...
    return in_prices(app) and app.prices_model is None


def in_price_drill(app: App) -> bool:
    # ...and none of them do in a model's session list, which only scrolls (j/k, g/G,
    # page) and steps back out.
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


def _trend_jk(app: App) -> str:
    # j/k is the one Trends key whose job changes per tab -- say which one it is doing.
    if app.trend_drill is not None:
        return "move in the list"
    if _ranked_trend(app):
        return "move the row cursor"
    return {
        "Daily": "page the month ([ ] too)",
        "Weekly": "page the week ([ ] too)",
        "Calendar": "page the year ([ ] too)",
    }.get(trend_tab(app), "")


def _trend_enter(app: App) -> str:
    if app.trend_drill is not None:
        return "open the session"
    if _ranked_trend(app):
        return "the sessions behind this row"
    if app.trend_focus:
        return "drill into the picked bar / day"
    return "focus the chart — then ← ↑ ↓ → pick"


def _on_turns(app: App) -> bool:
    return in_session(app) and app.active_tab_name() == "Turns"


def _enter_opens_something(app: App) -> bool:
    # Enter is the one key whose meaning IS the context: it drills from browse, opens a
    # row on the pickerized tabs, and does nothing on Overview/Models or in a session.
    if not in_main(app):
        return False
    if app.view == "browse":
        return True
    if app.view == "zoom":
        return app.active_tab_name() in ("Sessions", "Projects", "Harnesses")
    if _on_turns(app):  # Enter folds/unfolds the selected ▸ prompt group
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
    if tab == "Projects":
        return "its sessions, within this scope"
    return "its sessions, within this scope"


# --- the table ----------------------------------------------------------------------
# Help renders it in section order (Here · Navigation · Global); the footer renders
# FOOTER_ORDER. Two orderings, one set of facts.

KEYS: tuple[Key, ...] = (
    # ---- Here: the main views (browse -> zoom -> session) --------------------------
    Key(
        id="enter",
        keys="Enter",
        summary=_enter_summary,
        section="here",
        when=_enter_opens_something,
        chip="Enter in",
    ),
    Key(
        id="max",
        keys="+",
        summary="maximize / restore the detail pane",
        section="here",
        when=in_zoom,
        chip="+ max",
        active=lambda app: app.zoom_maximized,
        binds=("+",),
    ),
    Key(
        id="ignore",
        keys="i",
        summary="ignore / unignore the selection",
        section="here",
        when=lambda app: in_main(app) and app.can_toggle_ignore(),
        chip="i ignore",
        binds=("i",),
    ),
    Key(
        id="ignored",
        keys="I",
        summary="show ignored rows (to unignore them)",
        section="here",
        when=lambda app: in_main(app) and bool(app.ignored_projects or app.ignored_sessions),
        chip="I ignored",
        active=lambda app: app.show_ignored_projects,
        binds=("I",),
    ),
    Key(
        id="bookmark",
        keys="b",
        summary="bookmark ★ this session",
        section="here",
        when=lambda app: in_main(app) and app.bookmark_target() is not None,
        chip="b mark",
        active=lambda app: (t := app.bookmark_target()) is not None and t.id in app.bookmarks,
        binds=("b",),
    ),
    Key(
        id="bookmarks",
        keys="B",
        summary="show only bookmarked sessions",
        section="here",
        when=lambda app: in_main(app) and bool(app.bookmarks or app.show_bookmarks_only),
        chip="B marked",
        active=lambda app: app.show_bookmarks_only,
        binds=("B",),
    ),
    Key(
        id="note",
        keys="n",
        summary="note ✎ this session — why it cost that",
        section="here",
        when=lambda app: in_main(app) and app.allow_notes and app.bookmark_target() is not None,
        chip="n note",
        active=lambda app: (t := app.bookmark_target()) is not None and bool(app.note_for(t.id)),
        binds=("n",),
    ),
    Key(
        id="sort",
        keys="s",
        summary=lambda app: "sort the price table" if in_price_list(app) else "sort this list",
        section="here",
        when=lambda app: in_price_list(app) or (in_main(app) and app.can_sort_current_view()),
        chip="s sort",
        active=lambda app: app.sort_menu,
        binds=("s", "S"),
    ),
    Key(
        id="filter",
        keys="f  /",
        summary=lambda app: "filter the model list"
        if in_price_list(app)
        else "filter — fuzzy over titles, projects, notes",
        section="here",
        when=lambda app: in_price_list(app) or (in_main(app) and app.can_filter_current_view()),
        chip="f,/ filter",
        active=lambda app: bool(app.query),
        binds=("f", "/"),
    ),
    Key(
        id="clear-filter",
        keys="x",
        summary="clear the filter",
        section="here",
        when=lambda app: in_main(app) and bool(app.query),
        binds=("x",),
    ),
    Key(
        id="unfold",
        keys="z",
        summary="unfold every ▸ prompt to its full text",
        section="here",
        when=_on_turns,
        binds=("z",),
    ),
    Key(
        id="launch",
        keys="L",
        summary="resume this session in its own tool",
        section="here",
        when=lambda app: in_main(app) and app.can_launch_current(),
        chip="L launch",
        active=lambda app: app.launch_menu is not None,
        binds=("L",),
    ),
    Key(
        id="open",
        keys="o",
        summary="open its directory",
        section="here",
        when=in_main,
        binds=("o",),
    ),
    Key(
        id="export",
        keys="e",
        summary=lambda app: "export the price table to CSV"
        if in_prices(app)
        else "export this list to CSV",
        section="here",
        when=lambda app: in_main(app) or in_price_list(app),
        binds=("e",),
    ),
    # ---- Here: the Trends overlay ---------------------------------------------------
    Key(
        id="trends-tabs",
        keys="h  l",
        summary="switch tab",
        section="here",
        when=in_trends,
        chip="h/l tabs",
    ),
    Key(
        id="trends-enter",
        keys="Enter",
        summary=_trend_enter,
        section="here",
        when=in_trends,
        chip=lambda app: "Enter drill"
        if (app.trend_focus or app.trend_drill is not None or _ranked_trend(app))
        else "Enter focus",
    ),
    Key(
        id="trends-page",
        keys="j  k",
        summary=_trend_jk,
        section="here",
        # Monthly has one chart and nothing to page: j/k does nothing there.
        when=lambda app: in_trends(app) and bool(_trend_jk(app)),
        chip=lambda app: "j/k rows" if _ranked_trend(app) or app.trend_drill else "j/k page",
        binds=("[", "]"),
    ),
    Key(
        id="trends-shades",
        keys="+  -",
        summary="more / fewer heat shades",
        section="here",
        when=lambda app: in_trends(app) and trend_tab(app) == "Calendar",
        binds=("=", "_", "-"),
    ),
    Key(
        id="trends-close",
        keys="Esc  q  T",
        summary=lambda app: "leave the focused chart / the drill (Esc), or close (q / T)"
        if (app.trend_focus or app.trend_drill is not None)
        else "close",
        section="here",
        when=in_trends,
        chip=lambda app: "Esc back"
        if (app.trend_focus or app.trend_drill is not None)
        else "Esc close",
    ),
    # ---- Here: the Prices overlay ---------------------------------------------------
    Key(
        id="prices-view",
        keys="p  h  l",
        summary="view: flat · vendor · provider · models.dev",
        section="here",
        when=in_price_list,
        chip="p view",
        binds=("p",),
    ),
    Key(
        id="prices-pin",
        keys="space",
        summary="pin this model ★ (floats first, in every view)",
        section="here",
        when=in_price_list,
        chip="space pin",
        binds=(" ",),
    ),
    Key(
        id="prices-enter",
        keys="Enter",
        summary="the sessions that used this model",
        section="here",
        when=in_price_list,
        chip="Enter sessions",
    ),
    Key(
        id="prices-refresh",
        keys="r",
        summary="refresh the rates from models.dev",
        section="here",
        when=in_price_list,
        chip="r refresh",
        binds=("r", "R"),
    ),
    Key(
        id="prices-close",
        keys="Esc  q  P",
        summary="close",
        section="here",
        when=in_price_list,
        chip="Esc close",
    ),
    Key(
        id="price-drill-back",
        keys="Esc",
        summary="back to the model list",
        section="here",
        when=in_price_drill,
        chip="Esc back",
    ),
    Key(
        id="price-drill-close",
        keys="q  P",
        summary="close",
        section="here",
        when=in_price_drill,
        chip="q close",
    ),
    # ---- Navigation -----------------------------------------------------------------
    Key(
        id="tab-focus",
        keys="Tab",
        summary="cycle the sidebar panels (Shift-Tab back)",
        section="nav",
        when=lambda app: in_main(app) and app.view != "session" and app.browse_mode == "time",
        segments=lambda app: [
            ("Tab ", False),
            ("yr", app.focus == "years"),
            ("/", False),
            ("mo", app.focus == "months"),
            ("/", False),
            ("day", app.focus == "days"),
        ]
        if app.view == "browse" and app.browse_mode == "time"
        else [],
        chip="Tab focus",
        binds=("\t",),
    ),
    Key(
        id="panels",
        keys=lambda app: "1  0" if app.browse_mode == "projects" else "1 2 3  0",
        summary=lambda app: "1 the Projects list · 0 the detail pane"
        if app.browse_mode == "projects"
        else "jump to a panel — its number is in its title",
        section="nav",
        when=in_main,
    ),
    Key(
        id="mode",
        keys=lambda app: "t  p  m" if app.machines_present else "t  p",
        summary=lambda app: "Time / Projects / Machines browse mode"
        if app.machines_present
        else "Time / Projects browse mode",
        section="nav",
        # Works from a drilled-in session too (set_browse_mode snapshots it), so advertise
        # it there -- returning to the mode lands back on that session.
        when=in_main,
        segments=lambda app: [
            ("t", app.browse_mode == "time"),
            ("/", False),
            ("p", app.browse_mode == "projects"),
        ]
        + ([("/", False), ("m", app.browse_mode == "machines")] if app.machines_present else [])
        + [(" mode", False)],
        chip=lambda app: "t/p/m mode" if app.machines_present else "t/p mode",
        binds=("p", "t", "m"),
    ),
    Key(
        id="tabs",
        keys="h  l",
        summary="switch detail tabs",
        section="nav",
        when=in_main,
        binds=("h", "l"),
    ),
    Key(
        id="esc",
        keys="Esc  S-Tab",
        summary="step back out — session → zoom → browse",
        section="nav",
        when=lambda app: in_main(app) and app.view != "browse",
        chip="Esc out",
    ),
    Key(
        id="move",
        keys="j  k",
        summary=lambda app: "pick a ▸ prompt (↑ ↓ too)"
        if _on_turns(app)
        else "move / scroll (↑ ↓ too)",
        section="nav",
        when=lambda app: not in_trends(app),  # Trends binds j/k to paging -- its own entry
        binds=("j", "k"),
    ),
    Key(
        id="page",
        keys="PgDn PgUp",
        summary="half a page (Ctrl-D / Ctrl-U too)",
        section="nav",
        when=lambda app: not in_trends(app) or app.trend_drill is not None,
    ),
    Key(
        id="ends",
        keys="g  G",
        summary=lambda app: "first / last prompt" if _on_turns(app) else "top / bottom",
        section="nav",
        when=lambda app: not in_trends(app) or app.trend_drill is not None,
        binds=("g", "G"),
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
        keys="H",
        # In a fleet `H` FILTERS by harness (keeps every machine); elsewhere it swaps the
        # backend store. Available whenever either applies -- a fleet with a single local
        # source still filters, so machines_present widens can_switch_source's gate.
        summary=lambda app: "filter harness (fleet)" if app.machines_present else "switch harness",
        section="pickers",
        # Shown when there's actually something to do: a backend swap available, or a fleet
        # harness filter with >=2 harnesses / one armed to clear (never a bare single-harness
        # fleet no-op, and never an armed filter you can't reach to clear).
        when=lambda app: app.can_switch_source() or app.can_harness_filter(),
        chip=lambda app: f"H {app.harness_filter}" if app.harness_filter else "H harness",
        active=lambda app: app.source_menu or app.harness_menu or bool(app.harness_filter),
        binds=("H",),
    ),
    Key(
        id="machine-filter",
        keys="M",
        # Twin of `H`: not in_main-gated, floats above Trends/Prices/help (handled there in
        # the overlay-common paths), so machines_present is the whole gate.
        summary="filter every view to one machine",
        section="pickers",
        when=lambda app: app.machines_present,
        chip=lambda app: f"M {app.machine_filter}" if app.machine_filter else "M machine",
        active=lambda app: bool(app.machine_filter) or app.machine_menu,
        binds=("M",),
    ),
    Key(
        id="whatif",
        keys="w",
        summary="what-if — reprice a session at one model",
        section="pickers",
        when=in_main,
        chip="w model",
        active=lambda app: bool(app.whatif_model),
        binds=("w",),
    ),
    Key(
        id="theme",
        keys="C",
        summary="colour theme",
        section="pickers",
        chip=None,
        binds=("C",),
    ),
    Key(
        id="demo",
        keys="D",
        summary="anonymize for a screenshot — pick titles / turns / spend",
        section="pickers",
        when=lambda app: bool(app.source_key),
        chip=lambda app: "D demo·on" if app.store.demo else "D demo",
        active=lambda app: app.demo_menu or bool(getattr(app.store, "demo", False)),
        binds=("D",),
    ),
    # ---- Global ---------------------------------------------------------------------
    Key(
        id="range",
        keys="R",
        summary="set the range — 30d · 2m · 2026-05 · a..b",
        section="global",
        when=in_main,
        chip="R range",
        active=lambda app: app.range_label() != "all time",
        binds=("R",),
    ),
    Key(
        id="all-time",
        keys="a",
        summary="all time",
        section="global",
        when=in_main,
        binds=("a",),
    ),
    Key(
        id="trends",
        keys="T",
        summary="trends — charts, calendar heatmap, rankings",
        section="global",
        when=lambda app: in_main(app) or in_trends(app),
        chip="T trends",
        active=lambda app: app.trends,
        binds=("T",),
    ),
    Key(
        id="prices",
        keys="P",
        summary="model prices — cheapest for your token mix",
        section="global",
        chip="P prices",
        active=lambda app: app.show_prices,
        binds=("P",),
    ),
    Key(
        id="dollar",
        keys="$",
        summary="price subscription usage at API list rates",
        section="global",
        when=lambda app: not app.store.demo,
        chip="$ what-if",
        active=lambda app: app.show_api_prices,
        binds=("$",),
    ),
    Key(
        id="refresh-machines",
        keys="F",
        # A fleet ACTION (an ssh re-fetch), not movement -- it belongs in Global with the
        # other things that always work when a fleet is present, not under Navigation.
        summary="re-pull machine summaries over ssh",
        section="global",
        when=lambda app: in_main(app) and app.machines_present,
        chip="F refresh",
        binds=("F",),
    ),
    Key(
        id="reload",
        keys="r",
        summary="reload",
        section="global",
        when=in_main,
        binds=("r",),
    ),
    Key(
        id="help",
        keys="?",
        summary="these keys",
        section="global",
        chip="? help",
        active=lambda app: app.help,
        binds=("?",),
    ),
    Key(
        id="quit",
        keys="q",
        summary="quit",
        section="global",
        when=in_main,
        chip="q quit",
        binds=("q",),
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
        rows = [k for k in KEYS if k.section == name and k.shown(app)]
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
