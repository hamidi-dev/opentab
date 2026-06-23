"""Renderer: all drawing."""
from __future__ import annotations

import math
import textwrap
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from opentab.models import DaySummary, MonthSummary, ProjectSummary, Workflow, YearSummary

if TYPE_CHECKING:
    from opentab.tui.app import App

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

from opentab.formatting import (
    BAR_CELLS,
    MONEY_PATTERN,
    TOKEN_PATTERN,
    cost_bar,
    human_tokens,
    money,
    money_label,
    pct,
    short_path,
    shorten,
    tokens,
)
from opentab.heatmap import (
    BLOCKS_UP,
    HEAT_EMPTY_GLYPH,
    calendar_cells,
    heat_band_label,
    heat_glyph,
    heat_level,
    heat_palette,
    month_range,
    week_key,
)
from opentab.models import ALL_YEARS, year_label
from opentab.pricing import api_equivalent_cost, is_local_provider, model_price, price_cache_meta
from opentab.util import fuzzy_score, launcher_hook, month_bounds, tool_namespace


class Renderer:
    """All terminal drawing for OpenTab.

    Holds the App and reads its state through __getattr__, so the App stays a
    pure controller (state + input + data) with no curses/rendering code.
    """

    def __init__(self, app: App) -> None:
        self.app = app
        # Clickable hit regions, rebuilt every draw() so they always match what is
        # on screen. Each is ("rows", kind, y0, y_last, x0, x1, start) for a list
        # (click row y selects index start + (y - y0)) or (kind, y, x0, x1, index)
        # for a tab label where kind is "tab"/"trend". hit() resolves a click.
        self.regions: list[tuple] = []

    def __getattr__(self, name: str):
        # Misses are App state/logic; read them from the App. (Renderer's own
        # methods are found normally, so they win over this delegation.)
        return getattr(self.app, name)

    def _add_rows_region(
        self, kind: str, y_first: int, x0: int, x1: int, start: int, drawn: int
    ) -> None:
        if drawn > 0:
            self.regions.append(("rows", kind, y_first, y_first + drawn - 1, x0, x1, start))

    def hit(self, my: int, mx: int) -> tuple[str, int] | None:
        # Resolve a mouse (y, x) to (kind, value): a list index for "rows" regions,
        # or a tab index for "tab"/"trend" regions. First match wins.
        for region in self.regions:
            if region[0] == "rows":
                _, kind, y0, y_last, x0, x1, start = region
                if y0 <= my <= y_last and x0 <= mx <= x1:
                    return kind, start + (my - y0)
            else:
                kind, y, x0, x1, index = region  # ("tab"|"trend", y, x0, x1, index)
                if my == y and x0 <= mx <= x1:
                    return kind, index
        return None

    def year_row_text(self, year: YearSummary, marker: str) -> str:
        return (
            f"{marker} {year_label(year.year):<9} {money(year.cost):>9} "
            f"{human_tokens(year.tokens):>7} {year.workflows:>3} ses {year.subagents:>3} subs"
        )

    def month_row_text(self, month: MonthSummary, marker: str) -> str:
        return (
            f"{marker} {month.month} {money(month.cost):>9} "
            f"{human_tokens(month.tokens):>7} {month.workflows:>3} ses {month.subagents:>3} subs"
        )

    def day_row_text(self, day: DaySummary, marker: str) -> str:
        return (
            f"{marker} {day.day} {money(day.cost):>9} "
            f"{human_tokens(day.tokens):>7} {day.workflows:>3} ses {day.subagents:>3} subs"
        )

    @staticmethod
    def project_name_width(width: int) -> int:
        return max(8, width - 38)

    def project_row_text(self, project: ProjectSummary, marker: str, width: int) -> str:
        name_width = self.project_name_width(width)
        name = short_path(project.directory, max(1, name_width - (2 if project.ignored else 0)))
        if project.ignored:
            name = f"× {name}"
        return (
            f"{marker} {name:{name_width}} "
            f"{money(project.cost):>9} {human_tokens(project.tokens):>7} "
            f"{project.workflows:>3} ses {project.subagents:>3} subs"
        )

    def project_header_text(self, width: int) -> str:
        name_width = self.project_name_width(width)
        return (
            f"  {self.project_sort_heading('project', 'Project'):{name_width}} "
            f"{self.project_sort_heading('cost', 'Cost'):>9} "
            f"{self.project_sort_heading('tokens', 'Tokens'):>7} "
            f"{self.project_sort_heading('sessions', 'Ses'):>7} "
            f"{self.project_sort_heading('subagents', 'Subs'):>8}"
        )

    def list_width(self, rows_text: list[str], width: int) -> int:
        # Size a left list to its content; keep at least 44 cols for the detail pane.
        content = max((len(r) for r in rows_text), default=20)
        return max(24, min(content + 3, max(24, width - 44)))

    def projects_left_width(self, width: int) -> int:
        # Size to the longest (home-shortened) project path plus the stat columns,
        # but never wider than half the screen — so it fits the content yet leaves
        # the detail pane room. Long paths truncate inside the panel instead.
        longest = max((len(short_path(p.directory, 999)) for p in self.projects), default=8)
        natural = max(longest, len("Project")) + 39  # marker + Cost/Tokens/Ses/Subs
        return max(24, min(natural, width // 2, max(24, width - 44)))

    def browse_left_width(self, width: int) -> int:
        if self.browse_mode == "projects":
            return self.projects_left_width(width)
        rows = [self.year_row_text(yr, ">") for yr in self.years]
        rows += [self.month_row_text(m, ">") for m in self.months]
        rows += [self.day_row_text(d, ">") for d in self.panel_days]
        # Size to the text, then reserve a lane for the inline spend bar (without
        # ever starving the detail pane of its minimum 44 columns).
        base = self.list_width(rows, width)
        return max(24, min(base + BAR_CELLS + 2, max(24, width - 44)))

    @staticmethod
    def bar_lane(w: int) -> tuple[int, int]:
        # (bar_cells, text_width) for a list panel of inner width w. The bar gets
        # its own lane on the right so it is never painted under a row highlight
        # (which would invert it); 0 cells when the panel is too narrow to spare.
        if w < 46:
            return 0, w - 2
        return BAR_CELLS, (w - 2) - (BAR_CELLS + 2)

    def max_scroll(self, stdscr: curses.window) -> int:
        height, width = stdscr.getmaxyx()
        visible = max(1, height - 9)
        lines = self.current_pager_lines(width)
        return max(0, len(lines) - visible)

    def current_pager_lines(self, width: int) -> list[str]:
        content_width = max(1, width - 4)
        if self.view == "session":
            workflow = self.current_session()
            if workflow is None:
                return []
            if self.tab == 0:
                return self.detail_overview(workflow, content_width)
            if self.tab == 1:
                return self.detail_models(workflow, content_width)
            return self.detail_subagents(workflow, content_width)

        if self.view == "zoom":
            if self.browse_mode == "projects":
                project = self.selected_project_summary
                if project is None:
                    return []
                current = self.current_tabs()[self.tab % len(self.current_tabs())]
                if current == "Overview":
                    return self.project_overview(project, content_width)
                if current == "Sources":
                    return self.project_sources(project, content_width)
                if current == "Models":
                    return self.project_models(project, content_width)
                return self.project_workflows(project, content_width)

            if self.focus == "years":
                year = self.selected_year_summary
                if year is None:
                    return []
                current = self.current_tabs()[self.tab % len(self.current_tabs())]
                if current == "Overview":
                    return self.year_overview(year, content_width)
                if current == "Sources":
                    return self.year_sources(year, content_width)
                if current == "Models":
                    return self.year_models(year, content_width)
                if current == "Projects":
                    return self.year_projects(year, content_width)
                return self.year_workflows(year, content_width)

            if self.focus == "months":
                month = self.selected_month_summary
                if month is None:
                    return []
                current = self.current_tabs()[self.tab % len(self.current_tabs())]
                if current == "Overview":
                    return self.month_overview(month, content_width)
                if current == "Sources":
                    return self.month_sources(month, content_width)
                if current == "Models":
                    return self.month_models(month, content_width)
                if current == "Projects":
                    return self.month_projects(month, content_width)
                return self.month_workflows(month, content_width)

            day = self.selected_day_summary
            if day is None:
                return []
            current = self.current_tabs()[self.tab % len(self.current_tabs())]
            if current == "Overview":
                return self.day_overview(day, content_width)
            if current == "Sources":
                return self.day_sources(day, content_width)
            if current == "Projects":
                return self.day_projects(day, content_width)
            return self.day_workflows(day, content_width)

        return []

    def draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        self.regions = []  # rebuilt below as panels draw, for this frame's clicks
        height, width = stdscr.getmaxyx()
        if height < 20 or width < 80:
            self.write(
                stdscr, 0, 0, "Terminal too small. Need at least 80x20.", curses.color_pair(1)
            )
            stdscr.refresh()
            return

        self.draw_header(stdscr, width)
        self.draw_footer(stdscr, height, width)

        top = 3
        bottom = height - 2
        avail = bottom - top
        if self.help:
            self.draw_help(stdscr, top, bottom, width)
        elif self.show_prices:
            self.draw_prices(stdscr, top, bottom, width)
        elif self.trends:
            self.draw_trends(stdscr, top, bottom, width)
        elif self.view == "session":
            self.draw_detail(stdscr, top, 0, avail, width)
        elif self.view == "zoom":
            if self.browse_mode == "projects":
                self.draw_project_detail(stdscr, top, 0, avail, width)
            elif self.focus == "years":
                self.draw_year_detail(stdscr, top, 0, avail, width)
            elif self.focus == "months":
                self.draw_month_detail(stdscr, top, 0, avail, width)
            else:
                self.draw_day_detail(stdscr, top, 0, avail, width)
        elif self.browse_mode == "projects":
            left = self.browse_left_width(width)
            self.draw_project_list(stdscr, top, 0, avail, left)
            self.draw_project_detail(stdscr, top, left, avail, width - left, active=False)
        else:
            left = self.browse_left_width(width)
            # Three stacked time panels. Years is short (few rows), so size it to
            # show every year (panels render h-3 rows, hence +3), capped so a long
            # history can't starve Months/Days; those split the rest as before.
            years_h = max(4, min(len(self.years) + 3, max(4, avail // 3)))
            remaining = avail - years_h
            months_h = max(4, min(len(self.months) + 3, remaining // 2))
            days_h = remaining - months_h
            self.draw_year_list(stdscr, top, 0, years_h, left, active=self.focus == "years")
            self.draw_month_list(
                stdscr, top + years_h, 0, months_h, left, active=self.focus == "months"
            )
            self.draw_day_list(
                stdscr, top + years_h + months_h, 0, days_h, left, active=self.focus == "days"
            )
            rx, rw = left, width - left
            if self.focus == "years":
                self.draw_year_detail(stdscr, top, rx, avail, rw, active=False)
            elif self.focus == "months":
                self.draw_month_detail(stdscr, top, rx, avail, rw, active=False)
            else:
                self.draw_day_detail(stdscr, top, rx, avail, rw, active=False)

        # Small centered modals float on top of the current view (so context stays
        # visible behind them), unlike the full-body help/prices/trends overlays.
        if self.price_prompt:
            self.draw_price_prompt(stdscr, height, width)
        elif self.source_menu:
            self.draw_source_menu(stdscr, height, width)
        elif self.sort_menu:
            self.draw_sort_menu(stdscr, height, width)
        elif self.launch_menu is not None:
            self.draw_launch_menu(stdscr, height, width)

        # Toasts float over everything, including modals -- they're the topmost layer.
        self.draw_toasts(stdscr, height, width)

        stdscr.refresh()

    def draw_header(self, stdscr: curses.window, width: int) -> None:
        summary = self.store.summary(self.all_workflows)
        title = " OpenTab "
        info = (
            f" {summary['workflows']} sessions "
            f"cost {money(float(summary['cost']))} "
            f"tokens {human_tokens(int(summary['tokens']))} "
            f"subs {summary['subagents']} "
        )
        self.write(stdscr, 0, 0, title, curses.color_pair(2) | curses.A_BOLD)
        # Source chip, always visible (and live-switchable with `c`): which backend
        # this data comes from — OpenCode / Claude Code / both.
        chip = f" {self.store.source_name} "
        self.write(stdscr, 0, len(title), chip, curses.color_pair(7) | curses.A_BOLD)
        if self.store.demo:
            tag = " DEMO — synthetic "
        elif self.show_api_prices:
            if getattr(self.store, "records_cost", True):
                tag = " WHAT-IF — would-have-paid at API prices "
            else:
                # No recorded dollars exist to deviate from, so this isn't a
                # "what-if" — the estimate is the only meaningful number.
                tag = " ESTIMATED — tokens × API list prices "
        elif not getattr(self.store, "records_cost", True):
            tag = " $0 = no recorded cost · press $ to estimate "
        else:
            tag = ""
        info_x = len(title) + len(chip)
        self.write(
            stdscr,
            0,
            info_x,
            shorten(info, max(0, width - info_x - len(tag) - 1)),
            curses.color_pair(3),
        )
        if tag:
            self.write(
                stdscr,
                0,
                max(0, width - len(tag) - 1),
                tag,
                curses.color_pair(2) | curses.A_REVERSE | curses.A_BOLD,
            )
        drilled = self.view in ("zoom", "session")
        sort_by = self.effective_sort_by()
        # The header is persistent view state. A modifier that LIMITS what you see --
        # a non-default range, a committed filter, ignored projects -- is shown in the
        # orange accent so you can't forget your view is narrowed; everything else (the
        # scope path, sort order) stays quiet grey. Same single meaning for orange as
        # everywhere else: active / non-default. Transient status pops up as a toast.
        # The live filter query is NOT echoed here -- it's in the bottom command line
        # while you type -- so the filter shows only once committed.
        x = 0
        if drilled:
            chip = " ZOOM "
            self.write(stdscr, 1, 0, chip, curses.color_pair(2) | curses.A_REVERSE | curses.A_BOLD)
            x = len(chip) + 1
        base = curses.color_pair(1) | (curses.A_BOLD if drilled else 0)
        active = curses.color_pair(6) | curses.A_BOLD
        range_lbl = self.range_label()
        bc = self.breadcrumb()  # always starts with range_lbl (its root segment)
        rest_bc = bc[len(range_lbl) :] if bc.startswith(range_lbl) else bc
        segs = [(range_lbl, active if range_lbl != "all time" else base), (rest_bc, base)]
        if sort_by:
            segs.append((f"  ·  sort: {sort_by}", base))
        if self.query and not self.filter_active:
            segs.append((f"  ·  filter: {self.query}", active))
        if self.ignored_projects:
            segs.append((f"  ·  ignored: {len(self.ignored_projects)}", active))
        # Transient status lives in floating toasts now (draw_toasts), not the header.
        for text, attr in segs:
            x = self.write_seg(stdscr, 1, x, text, attr, width)
        self.hline(stdscr, 2, 0, width)

    def write_seg(
        self, stdscr: curses.window, y: int, x: int, text: str, attr: int, width: int
    ) -> int:
        # Write one clipped segment of a single-line strip and return the next x, so a
        # line can be painted piece by piece with per-segment colours (the bottom
        # command line uses it to highlight just the input field, not the whole bar).
        if not text or x >= width - 1:
            return x
        clipped = shorten(text, width - x - 1)
        self.write(stdscr, y, x, clipped, attr)
        return x + len(clipped)

    def breadcrumb(self) -> str:
        # Always-visible "you are here" path: scope › month › day › session › tab.
        # It is the only locator once a zoom hides the sidebar.
        sep = " › "
        tabs = self.current_tabs()
        tab_name = tabs[self.tab % len(tabs)]
        segs = [self.range_label()]
        if self.browse_mode == "projects" and self.view != "session":
            project = self.selected_project_summary
            segs.append("projects")
            if project:
                segs.append(short_path(project.directory, 34))
            segs.append(tab_name)
            return sep.join(s for s in segs if s)
        if self.view == "session":
            if self.browse_mode == "projects":
                project = self.selected_project_summary
                if project:
                    segs.append(short_path(project.directory, 34))
            elif self.focus == "years":
                if self.focused_year:  # month label already carries the year, so only
                    segs.append(self.focused_year)  # show a bare year when that's the scope
            elif self.focused_month:
                segs.append(self.focused_month)
            if self.browse_mode != "projects" and self.focus == "days" and self.active_day:
                segs.append(self.active_day)
            if self.browse_mode != "projects" and self.zoom_project:
                segs.append(short_path(self.zoom_project, 24))
            sess = self.current_session()
            segs.append(shorten(sess.title, 28) if sess else "session")
            segs.append(tab_name)
        elif self.focus == "years":
            if self.focused_year:
                segs.append(self.focused_year)
            if self.zoom_project and self.on_sessions_tab:
                segs.append(short_path(self.zoom_project, 24))
            segs.append(tab_name)
        elif self.focus == "months":
            if self.focused_month:
                segs.append(self.focused_month)
            if self.zoom_project and self.on_sessions_tab:
                segs.append(short_path(self.zoom_project, 24))
            segs.append(tab_name)
        else:
            if self.focused_month:
                segs.append(self.focused_month)
            if self.active_day:
                segs.append(self.active_day)
            if self.zoom_project and self.on_sessions_tab:
                segs.append(short_path(self.zoom_project, 24))
            segs.append(tab_name)
        return sep.join(s for s in segs if s)

    def draw_footer(self, stdscr: curses.window, height: int, width: int) -> None:
        # Context-sensitive: show only keys that do something in the current view, so
        # the strip stays short enough to read instead of silently truncating. Plain
        # movement (j/k/h/l, arrows) is deliberately omitted -- vim users know it and
        # everyone else reaches for the arrow keys.
        if self.filter_active:
            # Bottom command line: you type here. The whole input field -- the
            # "filter:" label, the query, and the block cursor -- is orange; only the
            # key hints stay plain, so the accent marks the field, not the whole bar.
            self.hline(stdscr, height - 2, 0, width)
            x = self.write_seg(
                stdscr,
                height - 1,
                0,
                f" filter: {self.query}▌",
                curses.color_pair(6) | curses.A_BOLD,
                width,
            )
            self.write_seg(
                stdscr,
                height - 1,
                x,
                "   ↑/↓ select  Enter keep  Esc cancel  Ctrl-U clear",
                curses.color_pair(4),
                width,
            )
            return
        # Each entry is (label, active). An active toggle -- its overlay or mode is
        # currently ON -- renders in the orange accent so the footer reflects state at
        # a glance (e.g. hitting T highlights "T trends" while the overlay is open).
        parts: list[tuple[str, bool]] = []
        if self.view == "browse" and self.browse_mode == "time":
            parts.append(("Tab yr/mo/day", False))
        parts.append(("Enter in", False))
        if self.view != "browse":
            parts.append(("Esc out", False))
        if self.view != "session":
            parts.append(("p/t mode", False))
        if self.can_sort_current_view():
            parts.append(("s sort", self.sort_menu))
        if self.can_toggle_project_ignore():
            parts.append(("i ignore", False))
        if self.ignored_projects:
            parts.append(("I ignored", self.show_ignored_projects))
        if self.can_switch_source():
            # `c` opens the source picker (lights up while it's open); the active source
            # is the header chip, so the key just advertises the menu, not a destination.
            parts.append(("c source", self.source_menu))
        # active/non-default modifiers light up too, matching the header chips: a
        # range that isn't "all time", a committed filter query. Range narrows every
        # view so it's always offered; "f" only filters session/project lists, so it
        # appears only where it does something (like "s/S sort").
        parts.append(("R range", self.range_label() != "all time"))
        if self.can_filter_current_view():
            parts.append(("f filter", bool(self.query)))
        parts += [
            ("T trends", self.trends),
            ("P prices", self.show_prices),
            ("e export", False),
            ("y copy", False),
            ("o open", False),
        ]
        if self.can_launch_current():
            parts.append(("L launch", self.launch_menu is not None))
        if self.source_key:
            parts.append(("D real" if self.store.demo else "D demo", False))
        if not self.store.demo:
            parts.append(("$ what-if", self.show_api_prices))
        parts += [("? help", self.help), ("q quit", False)]
        self.hline(stdscr, height - 2, 0, width)
        self.draw_keybar(stdscr, height - 1, width, parts)

    def draw_keybar(self, stdscr: curses.window, y: int, width: int, parts) -> None:
        # Render the footer key strip segment by segment so active toggles can stand
        # out in the orange accent (pair 6) against the slate baseline (pair 4),
        # instead of one flat-coloured joined string.
        base = curses.color_pair(4)
        active = curses.color_pair(6) | curses.A_BOLD
        x = 0
        self.write(stdscr, y, x, " ", base)
        x += 1
        for i, (text, on) in enumerate(parts):
            if x >= width - 1:
                break
            if i:
                self.write(stdscr, y, x, "  ", base)
                x += 2
            self.write(stdscr, y, x, text, active if on else base)
            x += len(text)

    def sort_heading(self, key: str, label: str) -> str:
        if self.effective_sort_by() != key:
            return label
        return f"{label} {'^' if key in ('title', 'project', 'model', 'agent') else 'v'}"

    def project_sort_heading(self, key: str, label: str) -> str:
        if self.effective_sort_by() != key:
            return label
        return f"{label} {'^' if key == 'project' else 'v'}"

    def session_started(self, workflow: Workflow) -> str:
        return (
            workflow.created_at[:10]
            if self.browse_mode == "projects" or self.focus == "months"
            else workflow.created_at[11:16]
        )

    def session_date_label(self) -> str:
        return "Started" if self.browse_mode == "projects" or self.focus == "months" else "Time"

    def top_sessions(self, rows: list[Workflow]) -> list[Workflow]:
        return sorted(rows, key=lambda item: (item.total_cost, item.total_tokens), reverse=True)

    @staticmethod
    def _source_abbrev(workflow: Workflow) -> str:
        return {
            "OpenCode": "oc",
            "Claude Code": "cc",
            "Codex": "cx",
            "Hermes": "hm",
            "CSV": "csv",
            "JSONL": "jl",
            "Copilot": "cp",
            "Pi": "pi",
            "OpenClaw": "ocl",
        }.get(workflow.source, (workflow.source or "??")[:2].lower())

    def source_tag(self, workflow: Workflow) -> str:
        # A compact origin marker ("[cc] ") prepended to titles in the sessions
        # picker and Top Sessions lists, only when sources are merged, so you can
        # tell OpenCode from Claude Code rows at a glance. Empty in single-source
        # views (the header chip already says which).
        if not getattr(self.store, "combined", False) or not workflow.source:
            return ""
        return f"[{self._source_abbrev(workflow)}] "

    def src_col(self, workflow: Workflow | None = None) -> str:
        # The "Src" column in the session tables (None = the header cell), only
        # when sources are merged — the one view where a row's origin isn't implied.
        if not getattr(self.store, "combined", False):
            return ""
        if workflow is None:
            return "Src "
        return f"{self._source_abbrev(workflow):<3} "

    def unpriced_hint(self) -> str:
        # Trails any block whose totals include $0.00 subscription tokens. Worded
        # per price mode so it never says "not billed" beside estimated dollars.
        if self.show_api_prices:
            return "! estimates — subscription tokens at API list prices"
        return "! $0.00 = subscription tokens — press $ to estimate"

    def money_attr(self, cost_text: str) -> int:
        # "$0.00" means zero or unpriced (tokens with no local price); muted grey so
        # it recedes behind real spend. "<$0.01" is a real cost and stays green.
        if cost_text == "$0.00":
            return curses.color_pair(1)
        return curses.color_pair(3) | curses.A_BOLD

    def token_attr(self, token_text: str) -> int:
        if token_text.endswith("B"):
            return curses.color_pair(5) | curses.A_BOLD
        if token_text.endswith("M"):
            return curses.color_pair(2) | curses.A_BOLD
        if token_text.endswith("k"):
            return curses.color_pair(1) | curses.A_BOLD
        return curses.color_pair(1)

    def write_colored_summary_row(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        text: str,
        cost: str,
        token_text: str,
        width: int,
    ) -> None:
        rendered = shorten(text, width).ljust(width)
        self.write(stdscr, y, x, rendered, curses.A_NORMAL)
        cost_pos = rendered.find(cost)
        if cost_pos >= 0:
            self.write(stdscr, y, x + cost_pos, cost, self.money_attr(cost))
        token_pos = rendered.find(token_text)
        if token_pos >= 0:
            self.write(stdscr, y, x + token_pos, token_text, self.token_attr(token_text))

    def draw_sessions_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        # Navigable session list on the Sessions tab of a zoomed month/day/project.
        sessions = self.current_sessions()
        cy = y + 3
        date_label = self.session_date_label()
        header = (
            f"  {self.sort_heading('date', date_label):<10} "
            f"{self.sort_heading('cost', 'Cost'):>9} "
            f"{self.sort_heading('tokens', 'Tokens'):>8} "
            f"{self.sort_heading('subagents', 'Subs'):>6}  "
            f"{self.sort_heading('title', 'Title')}"
        )
        self.write(
            stdscr,
            cy,
            x + 2,
            shorten(header, w - 4),
            curses.color_pair(4) | curses.A_BOLD,
        )
        if not sessions:
            self.write(stdscr, cy + 1, x + 2, "No sessions.", curses.color_pair(1))
            return
        visible = max(1, h - 5)
        idx = max(0, min(self.workflow_index, len(sessions) - 1))
        start = max(0, min(idx - visible // 2, max(0, len(sessions) - visible)))
        self._add_rows_region(
            "session", cy + 1, x, x + w - 1, start, len(sessions[start : start + visible])
        )
        for off, wf in enumerate(sessions[start : start + visible]):
            ry = cy + 1 + off
            marker = ">" if start + off == idx else " "
            started = self.session_started(wf)
            cost = money(wf.total_cost)
            tok = human_tokens(wf.total_tokens)
            text = (
                f"{marker} {started:<10} {cost:>9} {tok:>8} {wf.subagents:>6}  "
                f"{self.source_tag(wf)}{wf.title}"
            )
            if start + off == idx:
                self.write(
                    stdscr,
                    ry,
                    x + 2,
                    shorten(text, w - 4).ljust(w - 4),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            else:
                self.write_colored_summary_row(stdscr, ry, x + 2, text, cost, tok, w - 4)
        hint = "Enter: open session"
        self.write(stdscr, y + 1, x + w - len(hint) - 2, hint, curses.color_pair(1))

    def draw_projects_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        # Navigable project list on the Projects tab of a zoomed month/day.
        projects = self.zoom_projects()
        cy = y + 3
        header = self.project_header_text(w - 4)
        self.write(stdscr, cy, x + 2, shorten(header, w - 4), curses.color_pair(4) | curses.A_BOLD)
        if not projects:
            self.write(stdscr, cy + 1, x + 2, "No projects.", curses.color_pair(1))
            return
        visible = max(1, h - 5)
        idx = max(0, min(self.project_index, len(projects) - 1))
        start = max(0, min(idx - visible // 2, max(0, len(projects) - visible)))
        self._add_rows_region(
            "zoomproject", cy + 1, x, x + w - 1, start, len(projects[start : start + visible])
        )
        for off, project in enumerate(projects[start : start + visible]):
            ry = cy + 1 + off
            marker = ">" if start + off == idx else " "
            cost = money(project.cost)
            tok = human_tokens(project.tokens)
            text = self.project_row_text(project, marker, w - 4)
            if start + off == idx:
                self.write(
                    stdscr,
                    ry,
                    x + 2,
                    shorten(text, w - 4).ljust(w - 4),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            else:
                self.write_colored_summary_row(stdscr, ry, x + 2, text, cost, tok, w - 4)
        hint = "Enter: open sessions"
        self.write(stdscr, y + 1, x + w - len(hint) - 2, hint, curses.color_pair(1))

    def draw_tabs(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        width: int,
        tabs: tuple[str, ...],
        active_index: int,
        kind: str = "tab",
    ) -> None:
        if width <= 0 or not tabs:
            return
        cx = x
        active_index %= len(tabs)
        remaining = width
        for i, tab in enumerate(tabs):
            label = f" {tab} " if i != active_index else f"[{tab}]"
            if i > 0:
                sep = "  "
                self.write(stdscr, y, cx, shorten(sep, remaining), curses.A_NORMAL)
                cx += min(len(sep), remaining)
                remaining -= min(len(sep), remaining)
            if remaining <= 0:
                return
            attr = (
                curses.color_pair(7) | curses.A_BOLD if i == active_index else curses.color_pair(1)
            )
            text = shorten(label, remaining)
            self.write(stdscr, y, cx, text, attr)
            self.regions.append((kind, y, cx, cx + len(text) - 1, i))  # clickable tab
            cx += len(text)
            remaining -= len(text)

    def draw_year_list(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        self.box(stdscr, y, x, h, w, "Years" + (" ▸" if active else ""), active=active)
        rows = self.years
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No years in range.", curses.color_pair(1))
            return

        # Scale per-year bars among the concrete years; "All years" (the sum) would
        # otherwise dwarf them. cost_bar clamps the all-years row to a full bar.
        peak = max((yr.cost for yr in rows if yr.year != ALL_YEARS), default=0.0)
        bar_cells, text_w = self.bar_lane(w)
        visible = h - 3
        start = max(0, min(self.year_index - visible // 2, max(0, len(rows) - visible)))
        self._add_rows_region(
            "year", y + 2, x, x + w - 1, start, len(rows[start : start + visible])
        )
        for row_y, year in enumerate(rows[start : start + visible], y + 2):
            selected = start + row_y - (y + 2) == self.year_index
            marker = ">" if selected else " "
            cost = money(year.cost)
            tok = human_tokens(year.tokens)
            text = self.year_row_text(year, marker)
            if selected and active:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    shorten(text, text_w).ljust(text_w),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            elif selected:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    shorten(text, text_w).ljust(text_w),
                    curses.color_pair(1) | curses.A_BOLD,
                )
            else:
                self.write_colored_summary_row(stdscr, row_y, x + 1, text, cost, tok, text_w)
            if bar_cells:
                self.write(
                    stdscr,
                    row_y,
                    x + w - 1 - bar_cells,
                    cost_bar(year.cost, peak, bar_cells),
                    curses.color_pair(1),
                )

    def draw_month_list(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        self.box(stdscr, y, x, h, w, "Months" + (" ▸" if active else ""), active=active)
        rows = self.months
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No months in range.", curses.color_pair(1))
            return

        peak = max((m.cost for m in rows), default=0.0)
        bar_cells, text_w = self.bar_lane(w)
        visible = h - 3
        start = max(0, min(self.month_index - visible // 2, max(0, len(rows) - visible)))
        self._add_rows_region(
            "month", y + 2, x, x + w - 1, start, len(rows[start : start + visible])
        )
        for row_y, month in enumerate(rows[start : start + visible], y + 2):
            selected = start + row_y - (y + 2) == self.month_index
            marker = ">" if selected else " "
            cost = money(month.cost)
            tok = human_tokens(month.tokens)
            text = self.month_row_text(month, marker)
            if selected and active:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    shorten(text, text_w).ljust(text_w),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            elif selected:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    shorten(text, text_w).ljust(text_w),
                    curses.color_pair(1) | curses.A_BOLD,
                )
            else:
                self.write_colored_summary_row(stdscr, row_y, x + 1, text, cost, tok, text_w)
            if bar_cells:
                self.write(
                    stdscr,
                    row_y,
                    x + w - 1 - bar_cells,
                    cost_bar(month.cost, peak, bar_cells),
                    curses.color_pair(1),
                )

    def draw_project_list(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        self.box(stdscr, y, x, h, w, "Projects ▸", active=True)
        rows = self.projects
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No projects in range.", curses.color_pair(1))
            return

        header = self.project_header_text(w - 2)
        self.write(
            stdscr, y + 1, x + 1, shorten(header, w - 2), curses.color_pair(4) | curses.A_BOLD
        )

        visible = h - 4
        start = max(0, min(self.project_index - visible // 2, max(0, len(rows) - visible)))
        self._add_rows_region(
            "project", y + 3, x, x + w - 1, start, len(rows[start : start + visible])
        )
        for row_y, project in enumerate(rows[start : start + visible], y + 3):
            selected = start + row_y - (y + 3) == self.project_index
            marker = ">" if selected else " "
            cost = money(project.cost)
            tok = human_tokens(project.tokens)
            text = self.project_row_text(project, marker, w - 2)
            if selected:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    shorten(text, w - 2).ljust(w - 2),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            else:
                self.write_colored_summary_row(stdscr, row_y, x + 1, text, cost, tok, w - 2)

    def draw_project_detail(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        project = self.selected_project_summary
        title = (
            "Project"
            if project is None
            else f"Project {short_path(project.directory, max(10, w - 14))}"
        )
        self.box(stdscr, y, x, h, w, title, active=active)
        if project is None:
            self.write(stdscr, y + 2, x + 2, "No project selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, self.current_tabs(), self.tab)

        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Overview":
            lines = self.project_overview(project, w - 4)
        elif current == "Sources":
            lines = self.project_sources(project, w - 4)
        elif current == "Models":
            lines = self.project_models(project, w - 4)
        else:
            lines = self.project_workflows(project, w - 4)

        visible = h - 4
        self.app.scroll = max(0, min(self.app.scroll, max(0, len(lines) - visible)))
        for offset, line in enumerate(lines[self.scroll : self.scroll + visible]):
            attr = (
                curses.color_pair(4) | curses.A_BOLD if line.startswith("# ") else curses.A_NORMAL
            )
            if line.startswith("! "):
                attr = curses.color_pair(5) | curses.A_BOLD
            self.write_rich(stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), attr)

    def draw_year_detail(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        year = self.selected_year_summary
        title = (
            "Year"
            if year is None
            else "All years"
            if year.year == ALL_YEARS
            else f"Year {year.year}"
        )
        self.box(stdscr, y, x, h, w, title, active=active)
        if year is None:
            self.write(stdscr, y + 2, x + 2, "No year selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, self.current_tabs(), self.tab)

        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Projects" and self.view == "zoom":
            self.draw_projects_picker(stdscr, y, x, h, w)
            return
        if current == "Overview":
            lines = self.year_overview(year, w - 4)
        elif current == "Sources":
            lines = self.year_sources(year, w - 4)
        elif current == "Models":
            lines = self.year_models(year, w - 4)
        elif current == "Projects":
            lines = self.year_projects(year, w - 4)
        else:
            lines = self.year_workflows(year, w - 4)

        visible = h - 4
        self.app.scroll = max(0, min(self.app.scroll, max(0, len(lines) - visible)))
        for offset, line in enumerate(lines[self.scroll : self.scroll + visible]):
            attr = (
                curses.color_pair(4) | curses.A_BOLD if line.startswith("# ") else curses.A_NORMAL
            )
            if line.startswith("! "):
                attr = curses.color_pair(5) | curses.A_BOLD
            self.write_rich(stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), attr)

    def draw_month_detail(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        month = self.selected_month_summary
        title = "Month" if month is None else f"Month {month.month}"
        self.box(stdscr, y, x, h, w, title, active=active)
        if month is None:
            self.write(stdscr, y + 2, x + 2, "No month selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, self.current_tabs(), self.tab)

        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Projects" and self.view == "zoom":
            self.draw_projects_picker(stdscr, y, x, h, w)
            return
        if current == "Overview":
            lines = self.month_overview(month, w - 4)
        elif current == "Sources":
            lines = self.month_sources(month, w - 4)
        elif current == "Models":
            lines = self.month_models(month, w - 4)
        elif current == "Projects":
            lines = self.month_projects(month, w - 4)
        else:
            lines = self.month_workflows(month, w - 4)

        visible = h - 4
        self.app.scroll = max(0, min(self.app.scroll, max(0, len(lines) - visible)))
        for offset, line in enumerate(lines[self.scroll : self.scroll + visible]):
            attr = (
                curses.color_pair(4) | curses.A_BOLD if line.startswith("# ") else curses.A_NORMAL
            )
            if line.startswith("! "):
                attr = curses.color_pair(5) | curses.A_BOLD
            self.write_rich(stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), attr)

    def draw_day_list(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        month = self.focused_month
        self.box(
            stdscr,
            y,
            x,
            h,
            w,
            (f"Days · {month}" if month else "Days") + (" ▸" if active else ""),
            active=active,
        )
        rows = self.panel_days
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No days in month.", curses.color_pair(1))
            return

        peak = max((d.cost for d in rows), default=0.0)
        bar_cells, text_w = self.bar_lane(w)
        visible = h - 3
        start = max(0, min(self.day_index - visible // 2, max(0, len(rows) - visible)))
        self._add_rows_region("day", y + 2, x, x + w - 1, start, len(rows[start : start + visible]))
        for row_y, day in enumerate(rows[start : start + visible], y + 2):
            selected = start + row_y - (y + 2) == self.day_index
            marker = ">" if selected else " "
            cost = money(day.cost)
            tok = human_tokens(day.tokens)
            text = self.day_row_text(day, marker)
            if selected and active:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    shorten(text, text_w).ljust(text_w),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            elif selected:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    shorten(text, text_w).ljust(text_w),
                    curses.color_pair(1) | curses.A_BOLD,
                )
            else:
                self.write_colored_summary_row(stdscr, row_y, x + 1, text, cost, tok, text_w)
            if bar_cells:
                self.write(
                    stdscr,
                    row_y,
                    x + w - 1 - bar_cells,
                    cost_bar(day.cost, peak, bar_cells),
                    curses.color_pair(1),
                )

    def draw_day_detail(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        day = self.selected_day_summary
        title = "Day" if day is None else f"Day {day.day}"
        self.box(stdscr, y, x, h, w, title, active=active)
        if day is None:
            self.write(stdscr, y + 2, x + 2, "No day selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, self.current_tabs(), self.tab)

        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Projects" and self.view == "zoom":
            self.draw_projects_picker(stdscr, y, x, h, w)
            return
        if current == "Overview":
            lines = self.day_overview(day, w - 4)
        elif current == "Sources":
            lines = self.day_sources(day, w - 4)
        elif current == "Projects":
            lines = self.day_projects(day, w - 4)
        else:
            lines = self.day_workflows(day, w - 4)

        visible = h - 4
        self.app.scroll = max(0, min(self.app.scroll, max(0, len(lines) - visible)))
        for offset, line in enumerate(lines[self.scroll : self.scroll + visible]):
            attr = (
                curses.color_pair(4) | curses.A_BOLD if line.startswith("# ") else curses.A_NORMAL
            )
            if line.startswith("! "):
                attr = curses.color_pair(5) | curses.A_BOLD
            self.write_rich(stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), attr)

    def draw_workflow_list(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        day = self.active_day
        title = f"Sessions · {day}" if day else "Sessions"
        self.box(stdscr, y, x, h, w, title, active=True)
        rows = self.workflows
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No sessions match the filter.", curses.color_pair(1))
            return

        visible = h - 3
        start = max(0, min(self.workflow_index - visible // 2, max(0, len(rows) - visible)))
        for row_y, workflow in enumerate(rows[start : start + visible], y + 2):
            selected = start + row_y - (y + 2) == self.workflow_index
            marker = ">" if selected else " "
            cost = money(workflow.total_cost)
            tok = human_tokens(workflow.total_tokens)
            text = (
                f"{marker} {cost:>9} "
                f"{tok:>7} "
                f"{workflow.subagents:>3} subs "
                f"{shorten(workflow.title, max(12, w - 25))}"
            )
            if selected:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    shorten(text, w - 2).ljust(w - 2),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            else:
                self.write_colored_summary_row(stdscr, row_y, x + 1, text, cost, tok, w - 2)

    def draw_detail(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        workflow = self.current_session()
        title = "Detail" if workflow is None else shorten(workflow.title, max(10, w - 12))
        self.box(stdscr, y, x, h, w, title, active=True)
        if workflow is None:
            self.write(stdscr, y + 2, x + 2, "No session selected.", curses.color_pair(1))
            return

        tabs = self.current_tabs()
        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, tabs, self.tab)

        current = tabs[self.tab % len(tabs)]
        if current == "Models":
            lines = self.detail_models(workflow, w - 4)
        elif current == "Subagents":
            lines = self.detail_subagents(workflow, w - 4)
        elif current == "Turns":
            lines = self.detail_turns(workflow, w - 4)
        elif current == "Tools":
            lines = self.detail_tools(workflow, w - 4)
        else:
            lines = self.detail_overview(workflow, w - 4)

        visible = h - 4
        self.app.scroll = max(0, min(self.app.scroll, max(0, len(lines) - visible)))
        for offset, line in enumerate(lines[self.scroll : self.scroll + visible]):
            attr = (
                curses.color_pair(4) | curses.A_BOLD if line.startswith("# ") else curses.A_NORMAL
            )
            if line.startswith("! "):
                attr = curses.color_pair(5) | curses.A_BOLD
            elif line.startswith("▸ "):  # Turns tab: a user-prompt group header
                attr = curses.color_pair(6) | curses.A_BOLD
            self.write_rich(stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), attr)

    def _model_table(
        self,
        rows: list[tuple],
        title: str,
        width: int,
        name_label: str = "Model",
        count_label: str = "Msgs",
    ) -> list[str]:
        # rows: (name, count, cost, tokens, cache_read, cache_write, output). The
        # name column fits the longest entry (so the numbers sit right after it),
        # capped by the available width so long names aren't cut when there's room.
        # name_label/count_label let the Tools tab reuse this as Tool/Calls.
        cw_ = max(4, len(count_label))
        longest = max([len(str(r[0])) for r in rows] + [len(name_label)])
        mw = min(longest, max(20, width - 57 - cw_))
        total_cost = sum(float(r[2]) for r in rows)
        lines = [
            title,
            f"{name_label:{mw}} {count_label:>{cw_}} {'Cost':>10} {'Share':>5} {'Tokens':>9} {'CacheR':>9} {'CacheW':>9} {'Output':>8}",
        ]
        for name, runs, cost, tok, cr, cw, out in rows:
            lines.append(
                f"{shorten(name, mw):{mw}} {int(runs):>{cw_}} {money(float(cost)):>10} "
                f"{pct(float(cost), total_cost):>5} "
                f"{human_tokens(int(tok)):>9} {human_tokens(int(cr)):>9} {human_tokens(int(cw)):>9} {human_tokens(int(out)):>8}"
            )
        if any(str(name).startswith("unknown") for name, *_ in rows):
            lines.extend(
                [
                    "",
                    "! unknown (not recorded) means provider/model metadata was not stored for these rows.",
                ]
            )
        return lines

    def _models_tab(self, rows: list[tuple], title: str, width: int) -> list[str]:
        # The Models tab body, with the live `f` filter applied to model names.
        # Unlike sessions we keep the cost ranking rather than re-ranking by fuzzy
        # score: model lists are short and the cost order is the useful one.
        if self.query:
            rows = [r for r in rows if fuzzy_score(self.query, str(r[0])) is not None]
            if not rows:
                return [title, f"No models match the filter: {self.query}"]
        return self._model_table(rows, title, width)

    @staticmethod
    def _agg_rows(aggregate: list[tuple[str, dict]]) -> list[tuple]:
        return [
            (
                m,
                it["runs"],
                it["cost"],
                it["tokens"],
                it["cache_read"],
                it["cache_write"],
                it["output"],
            )
            for m, it in aggregate
        ]

    @staticmethod
    def _mix_rows(model_rows: list[dict]) -> list[tuple]:
        return [
            (
                r["model_name"],
                r["runs"],
                r["cost"],
                r["tokens_total"],
                r["cache_read"],
                r["cache_write"],
                r["output"],
            )
            for r in model_rows
        ]

    def month_overview(self, month: MonthSummary, width: int) -> list[str]:
        lines = [
            "# Monthly Insight",
            f"Month:           {month.month}",
            f"Cost:            {money(month.cost)}",
            f"Share of range:  {pct(month.cost, self.range_cost_total())}",
            f"Tokens:          {tokens(month.tokens)}",
            f"Sessions:        {month.workflows}",
            f"Subagents:       {month.subagents}",
            f"Unpriced tokens: {tokens(month.unpriced_tokens)}",
        ]
        if month.unpriced_tokens:
            lines.extend(["", self.unpriced_hint()])
        month_ws = self.workflows_for_month(month.month)
        lines.append("")
        agg = self.aggregate_models(month_ws)
        lines.extend(self._model_table(self._agg_rows(agg), "# Top Models", width))
        lines.extend(["", "# Top Sessions"])
        for workflow in self.top_sessions(month_ws):
            lines.append(
                f"{money(workflow.total_cost):>10} {pct(workflow.total_cost, month.cost):>5} "
                f"{human_tokens(workflow.total_tokens):>8} "
                f"agents {workflow.subagents:<3} "
                f"{shorten(self.source_tag(workflow) + workflow.title, max(20, width - 37))}"
            )
        return lines

    def month_models(self, month: MonthSummary, width: int) -> list[str]:
        agg = self.aggregate_models(self.workflows_for_month(month.month))
        return self._models_tab(self._agg_rows(agg), "# Monthly Model Spend", width)

    def month_sources(self, month: MonthSummary, width: int) -> list[str]:
        return self.source_table(self.workflows_for_month(month.month), width)

    def month_workflows(self, month: MonthSummary, width: int) -> list[str]:
        lines = [
            "# Monthly Sessions",
            f"{self.sort_heading('date', 'Started'):<10} "
            f"{self.sort_heading('cost', 'Cost'):>9} "
            f"{self.sort_heading('tokens', 'Tokens'):>8} "
            f"{self.sort_heading('subagents', 'Agts'):>4} Models  "
            f"{self.src_col()}{self.sort_heading('title', 'Title')}",
        ]
        for workflow in self.filtered_sessions(self.workflows_for_month(month.month)):
            lines.append(
                f"{workflow.created_at[:10]:<10} "
                f"{money(workflow.total_cost):>9} "
                f"{human_tokens(workflow.total_tokens):>8} "
                f"{workflow.subagents:>4} "
                f"{workflow.model_count:>6}  "
                f"{self.src_col(workflow)}{workflow.title}"
            )
        return lines

    def year_overview(self, year: YearSummary, width: int) -> list[str]:
        lines = [
            "# Yearly Insight",
            f"Year:            {year_label(year.year)}",
            f"Cost:            {money(year.cost)}",
            f"Share of range:  {pct(year.cost, self.range_cost_total())}",
            f"Tokens:          {tokens(year.tokens)}",
            f"Sessions:        {year.workflows}",
            f"Subagents:       {year.subagents}",
            f"Unpriced tokens: {tokens(year.unpriced_tokens)}",
        ]
        if year.unpriced_tokens:
            lines.extend(["", self.unpriced_hint()])
        year_ws = self.workflows_for_year(year.year)
        # Top Months is the year's headline breakdown -- the level you drill into next.
        by_month: dict[str, list[Workflow]] = defaultdict(list)
        for w in year_ws:
            by_month[w.created_at[:7]].append(w)
        lines.extend(["", "# Top Months"])
        for month in sorted(
            by_month, key=lambda m: sum(w.total_cost for w in by_month[m]), reverse=True
        ):
            ws = by_month[month]
            cost = sum(w.total_cost for w in ws)
            lines.append(
                f"{month:<10} {money(cost):>10} {pct(cost, year.cost):>5} "
                f"{human_tokens(sum(w.total_tokens for w in ws)):>9} "
                f"{len(ws):>4} sess"
            )
        lines.append("")
        agg = self.aggregate_models(year_ws)
        lines.extend(self._model_table(self._agg_rows(agg), "# Top Models", width))
        lines.extend(["", "# Top Sessions"])
        for workflow in self.top_sessions(year_ws):
            lines.append(
                f"{money(workflow.total_cost):>10} {pct(workflow.total_cost, year.cost):>5} "
                f"{human_tokens(workflow.total_tokens):>8} "
                f"agents {workflow.subagents:<3} "
                f"{shorten(self.source_tag(workflow) + workflow.title, max(20, width - 37))}"
            )
        return lines

    def year_models(self, year: YearSummary, width: int) -> list[str]:
        agg = self.aggregate_models(self.workflows_for_year(year.year))
        return self._models_tab(self._agg_rows(agg), "# Yearly Model Spend", width)

    def year_sources(self, year: YearSummary, width: int) -> list[str]:
        return self.source_table(self.workflows_for_year(year.year), width)

    def year_projects(self, year: YearSummary, width: int) -> list[str]:
        projects = self.projects_for_workflows(self.workflows_for_year(year.year))
        return self.project_table(projects, "# Yearly Projects", width)

    def year_workflows(self, year: YearSummary, width: int) -> list[str]:
        lines = [
            "# Yearly Sessions",
            f"{self.sort_heading('date', 'Started'):<10} "
            f"{self.sort_heading('cost', 'Cost'):>9} "
            f"{self.sort_heading('tokens', 'Tokens'):>8} "
            f"{self.sort_heading('subagents', 'Agts'):>4} Models  "
            f"{self.src_col()}{self.sort_heading('title', 'Title')}",
        ]
        for workflow in self.filtered_sessions(self.workflows_for_year(year.year)):
            lines.append(
                f"{workflow.created_at[:10]:<10} "
                f"{money(workflow.total_cost):>9} "
                f"{human_tokens(workflow.total_tokens):>8} "
                f"{workflow.subagents:>4} "
                f"{workflow.model_count:>6}  "
                f"{self.src_col(workflow)}{workflow.title}"
            )
        return lines

    def day_overview(self, day: DaySummary, width: int) -> list[str]:
        lines = [
            "# Day Burn",
            f"Day:             {day.day}",
            f"Cost:            {money(day.cost)}",
            f"Share of range:  {pct(day.cost, self.range_cost_total())}",
            f"Tokens:          {tokens(day.tokens)}",
            f"Sessions:        {day.workflows}",
            f"Subagents:       {day.subagents}",
            f"Unpriced tokens: {tokens(day.unpriced_tokens)}",
        ]
        if day.unpriced_tokens:
            lines.extend(["", self.unpriced_hint()])
        # A day touches few models, so the full model table lives here in the
        # Overview rather than in its own (near-empty) tab.
        agg = self.aggregate_models(self.workflows_for_day(day.day))
        lines.append("")
        lines.extend(self._model_table(self._agg_rows(agg), "# Model Mix", width))
        lines.extend(["", "# Top Sessions"])
        for workflow in self.top_sessions(self.workflows_for_day(day.day)):
            lines.append(
                f"{money(workflow.total_cost):>10} {pct(workflow.total_cost, day.cost):>5} "
                f"{human_tokens(workflow.total_tokens):>8} "
                f"agents {workflow.subagents:<3} "
                f"{shorten(self.source_tag(workflow) + workflow.title, max(20, width - 37))}"
            )
        return lines

    def day_sources(self, day: DaySummary, width: int) -> list[str]:
        return self.source_table(self.workflows_for_day(day.day), width)

    def day_workflows(self, day: DaySummary, width: int) -> list[str]:
        lines = [
            "# Day Sessions",
            f"{self.sort_heading('date', 'Time'):<10} "
            f"{self.sort_heading('cost', 'Cost'):>9} "
            f"{self.sort_heading('tokens', 'Tokens'):>8} "
            f"{self.sort_heading('subagents', 'Agts'):>4} Models  "
            f"{self.src_col()}{self.sort_heading('title', 'Title')}",
        ]
        for workflow in self.filtered_sessions(self.workflows_for_day(day.day)):
            lines.append(
                f"{workflow.created_at[11:16]:<10} "
                f"{money(workflow.total_cost):>9} "
                f"{human_tokens(workflow.total_tokens):>8} "
                f"{workflow.subagents:>4} "
                f"{workflow.model_count:>6}  "
                f"{self.src_col(workflow)}{workflow.title}"
            )
        return lines

    def project_overview(self, project: ProjectSummary, width: int) -> list[str]:
        workflows = self.workflows_for_project(project.directory, include_ignored=project.ignored)
        share_total = (
            sum(w.total_cost for w in self.ranged_workflows)
            if project.ignored
            else self.range_cost_total()
        )
        lines = [
            "# Project Spend",
            f"Project:         {short_path(project.directory, max(20, width - 17))}",
            f"Ignored:         {'yes' if project.ignored else 'no'}",
            f"Cost:            {money(project.cost)}",
            f"Share of range:  {pct(project.cost, share_total)}",
            f"Tokens:          {tokens(project.tokens)}",
            f"Sessions:        {project.workflows}",
            f"Subagents:       {project.subagents}",
            f"Unpriced tokens: {tokens(project.unpriced_tokens)}",
        ]
        if project.unpriced_tokens:
            lines.extend(["", self.unpriced_hint()])
        lines.append("")
        agg = self.aggregate_models(workflows)
        lines.extend(self._model_table(self._agg_rows(agg), "# Top Models", width))
        lines.extend(["", "# Top Sessions"])
        for workflow in self.top_sessions(workflows):
            lines.append(
                f"{workflow.created_at[:10]:<10} {money(workflow.total_cost):>10} "
                f"{pct(workflow.total_cost, project.cost):>5} "
                f"{human_tokens(workflow.total_tokens):>8} agents {workflow.subagents:<3} "
                f"{shorten(self.source_tag(workflow) + workflow.title, max(20, width - 50))}"
            )
        return lines

    def project_models(self, project: ProjectSummary, width: int) -> list[str]:
        agg = self.aggregate_models(
            self.workflows_for_project(project.directory, include_ignored=project.ignored)
        )
        return self._models_tab(self._agg_rows(agg), "# Project Model Spend", width)

    def project_sources(self, project: ProjectSummary, width: int) -> list[str]:
        return self.source_table(
            self.workflows_for_project(project.directory, include_ignored=project.ignored), width
        )

    def project_table(self, rows: list[ProjectSummary], title: str, width: int) -> list[str]:
        lines = [title, self.project_header_text(width)]
        if not rows:
            lines.append("No projects.")
            return lines
        lines.extend(self.project_row_text(project, " ", width) for project in rows)
        return lines

    def month_projects(self, month: MonthSummary, width: int) -> list[str]:
        projects = self.projects_for_workflows(self.workflows_for_month(month.month))
        return self.project_table(projects, "# Monthly Projects", width)

    def day_projects(self, day: DaySummary, width: int) -> list[str]:
        projects = self.projects_for_workflows(self.workflows_for_day(day.day))
        return self.project_table(projects, "# Day Projects", width)

    def project_workflows(self, project: ProjectSummary, width: int) -> list[str]:
        lines = [
            "# Project Sessions",
            f"{self.sort_heading('date', 'Started'):<10} "
            f"{self.sort_heading('cost', 'Cost'):>9} "
            f"{self.sort_heading('tokens', 'Tokens'):>8} "
            f"{self.sort_heading('subagents', 'Agts'):>4} Models  "
            f"{self.src_col()}{self.sort_heading('title', 'Title')}",
        ]
        workflows = self.workflows_for_project(project.directory, include_ignored=project.ignored)
        for workflow in self.filtered_sessions(workflows):
            lines.append(
                f"{workflow.created_at[:10]:<10} "
                f"{money(workflow.total_cost):>9} "
                f"{human_tokens(workflow.total_tokens):>8} "
                f"{workflow.subagents:>4} "
                f"{workflow.model_count:>6}  "
                f"{self.src_col(workflow)}{workflow.title}"
            )
        return lines

    def detail_overview(self, workflow: Workflow, width: int) -> list[str]:
        lines = [
            "# Session",
            f"ID:       {workflow.id}",
            f"Started:  {workflow.created_at}",
            f"Project:  {short_path(workflow.directory, max(20, width - 10))}",
            f"Title:    {workflow.title}",
        ]
        if workflow.source:
            lines.append(f"Source:   {workflow.source}")
        lines += [
            "",
            "# Money",
            f"Total:    {money(workflow.total_cost)}",
            f"Root:     {money(workflow.root_cost)}",
            f"Subagent: {money(workflow.total_cost - workflow.root_cost)}",
            f"Share:    {pct(workflow.total_cost, self.range_cost_total())} of range",
            "",
            "# Shape",
            f"Subagents:       {workflow.subagents}",
            f"Distinct models: {workflow.model_count}",
            f"Tokens:          {tokens(workflow.total_tokens)}",
            f"Unpriced tokens: {tokens(workflow.unpriced_tokens)}",
        ]
        if workflow.unpriced_tokens:
            lines.extend(["", self.unpriced_hint()])
        lines.append("")
        model_rows = self.model_mix(workflow.id)
        lines.extend(self._model_table(self._mix_rows(model_rows), "# Top Models", width))
        return lines

    def detail_models(self, workflow: Workflow, width: int) -> list[str]:
        model_rows = self.model_mix(workflow.id)
        lines = self._models_tab(self._mix_rows(model_rows), "# Model Mix", width)
        # The note only makes sense alongside an unknown row that survived the filter.
        shown = [
            row
            for row in model_rows
            if not self.query or fuzzy_score(self.query, str(row["model_name"])) is not None
        ]
        if any(row["model_name"].startswith("unknown") for row in shown):
            lines.append(
                "! $0.00 rows with tokens = no local per-token price; common on subscription/credit plans (Claude Code, Codex, Copilot)."
            )
        return lines

    def detail_subagents(self, workflow: Workflow, width: int) -> list[str]:
        rows = self._priced_nodes(
            [row for row in self.store.workflow_nodes(workflow.id) if row["depth"] > 0]
        )
        if not rows:
            return ["# Subagents", "No subagents used in this workflow."]
        rows = self.sorted_subagent_rows(rows)
        lines = [
            "# Subagent Executions",
            f"{self.sort_heading('depth', 'D'):<1} "
            f"{self.sort_heading('agent', 'Agent'):14} "
            f"{self.sort_heading('model', 'Model'):31} "
            f"{self.sort_heading('cost', 'Cost'):>8} "
            f"{self.sort_heading('tokens', 'Tokens'):>9}  "
            f"{self.sort_heading('title', 'Title')}",
        ]
        for row in rows:
            lines.append(
                f"{row['depth']:<1} "
                f"{shorten(row['agent'], 14):14} "
                f"{shorten(row['model_name'], 31):31} "
                f"{money(row['cost']):>8} "
                f"{human_tokens(row['tokens_total']):>9}  "
                f"{row['title']}"
            )
        return lines

    def detail_tools(self, workflow: Workflow, width: int) -> list[str]:
        # Which tools (and MCP servers) the LLM calls cost the most. Each row is the
        # tokens/cost of the assistant steps that invoked a tool, split evenly when a
        # step called several -- so this is "tokens spent in turns that used this
        # tool", not the tool's own output size. The "$" view reprices $0
        # (subscription) rows at list price, like every other panel.
        if not self.session_supports_tools(workflow.id):
            return [
                "# Tools",
                "Per-tool token attribution is only available for OpenCode sessions.",
            ]
        rows = self.session_tool_rows(workflow.id)
        if not rows:
            return ["# Tools", "No tool calls recorded for this session."]
        api = self.show_api_prices and not self.store.demo

        def agg() -> dict[str, dict]:
            return defaultdict(
                lambda: {
                    "calls": 0,
                    "cost": 0.0,
                    "tokens": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "output": 0,
                }
            )

        by_tool, by_server = agg(), agg()
        for r in rows:
            # A wholly-$0 (tool, model) row is unpriced -- estimate it at list price in
            # the "$" view (mirrors _priced_nodes); a priced row keeps its real cost.
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
            for bucket, key in ((by_tool, r["tool"]), (by_server, tool_namespace(r["tool"]))):
                it = bucket[key]
                it["calls"] += r["calls"]
                it["cost"] += cost
                it["tokens"] += r["tokens_total"]
                it["cache_read"] += r["cache_read"]
                it["cache_write"] += r["cache_write"]
                it["output"] += r["output"]

        def table_rows(bucket: dict[str, dict]) -> list[tuple]:
            ordered = sorted(
                bucket.items(), key=lambda kv: (kv[1]["cost"], kv[1]["tokens"]), reverse=True
            )
            return [
                (
                    name,
                    it["calls"],
                    it["cost"],
                    it["tokens"],
                    it["cache_read"],
                    it["cache_write"],
                    it["output"],
                )
                for name, it in ordered
            ]

        lines = self._model_table(
            table_rows(by_tool), "# Tools — this session", width, "Tool", "Calls"
        )
        lines.append("")
        lines.extend(
            self._model_table(
                table_rows(by_server), "# By server / namespace", width, "Server", "Calls"
            )
        )
        lines += [
            "",
            "! Tokens/cost are for the LLM turns that invoked each tool (split evenly across",
            "! a turn's tools), not the tool's own output size.",
        ]
        return lines

    def detail_turns(self, workflow: Workflow, width: int) -> list[str]:
        # How this session's cost accrued, one LLM step ("turn") at a time, in
        # chronological order -- not sorted by cost -- grouped under the user prompt
        # that triggered each run. A "▸ <prompt>" header opens each group (with that
        # prompt's subtotal); the indented turns beneath are the agent's work on it,
        # so the user-vs-llm split is the header-vs-rows. Subagent (Task) turns are
        # interleaved by time and tagged in the Agent column. The Cumulative column is
        # the point: a running total you read top-to-bottom. Wholly-unpriced ($0)
        # turns are repriced at list price under "$", like every other panel.
        if not self.session_supports_turns(workflow.id):
            return [
                "# Turns",
                "A per-turn timeline is only available for OpenCode and Claude Code sessions.",
            ]
        rows = self.session_turn_rows(workflow.id)
        if not rows:
            return ["# Turns", "No turns recorded for this session."]
        api = self.show_api_prices and not self.store.demo
        costs = []
        for r in rows:
            cost = r["cost"]
            if api and not cost:  # a recorded $0 turn is wholly unpriced -- estimate it
                cost = api_equivalent_cost(
                    r["model_name"],
                    r["input"],
                    r["output"],
                    r["reasoning"],
                    r["cache_read"],
                    r["cache_write"],
                )
            costs.append(cost)
        total = sum(costs)
        subtotal: dict[str, float] = defaultdict(float)  # per-prompt spend, for headers
        for r, cost in zip(rows, costs):
            subtotal[r.get("prompt_id", "")] += cost

        # time is the full localtime "YYYY-MM-DD HH:MM:SS"; show date + clock ("MM-DD
        # HH:MM:SS") on every row. The seconds matter (turns can be seconds apart) and
        # the date matters too (a resumed session spans days), so keep both. Rows are
        # indented two spaces to nest under their "▸" prompt header.
        time_w = 14

        def clock(t: str) -> str:
            return t[5:19] if t else "--"  # "MM-DD HH:MM:SS"

        idx_w = max(2, len(str(len(rows))))
        agent_w = min(10, max(5, max((len(r["agent"]) for r in rows), default=5)))
        mw = max(16, min(34, width - (idx_w + agent_w + time_w + 42)))  # model flexes
        lines = [
            f"# Turns — {len(rows)} turns, {money(total)} total",
            f"  {'#':>{idx_w}} {'Time':<{time_w}} {'Model':<{mw}} {'Agent':<{agent_w}} "
            f"{'Tokens':>9} {'Cost':>9} {'Cumulative':>16}",
        ]
        cum = 0.0
        last_pid = object()  # sentinel: the first row always opens a group
        for n, (r, cost) in enumerate(zip(rows, costs), start=1):
            pid = r.get("prompt_id", "")
            if pid != last_pid:
                last_pid = pid
                gc = money(subtotal[pid])
                title = (r.get("prompt_title") or "").strip() or "(no preceding prompt)"
                title = shorten(title, max(10, width - len(gc) - 5))
                head = "▸ " + title
                lines.append(head + " " * max(1, width - len(head) - len(gc)) + gc)
            cum += cost
            agent = r["agent"] if r["depth"] else "-"
            cumlabel = f"{money(cum)} · {pct(cum, total)}"
            lines.append(
                f"  {n:>{idx_w}} {clock(r['time']):<{time_w}} {shorten(r['model_name'], mw):<{mw}} "
                f"{shorten(agent, agent_w):<{agent_w}} "
                f"{human_tokens(r['tokens_total']):>9} {money(cost):>9} {cumlabel:>16}"
            )
        lines += [
            "",
            "! Grouped by the user prompt (▸) that triggered each run; indented rows are the",
            "! agent's turns. Time order, not cost; Cumulative is the running session total.",
        ]
        return lines

    def draw_help(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        self.box(stdscr, y, 0, bottom - y, width, "Help · j/k scroll · any other key closes")
        lines = [
            "OpenTab is a session browser, not a fuzzy-search toy.",
            "",
            "Navigation:",
            "  p / t            switch to Projects / Time browse mode",
            "  Tab              cycle focus Years -> Months -> Days in Time mode",
            "  Enter / +        zoom year/month/day/project detail; on the Sessions tab open the",
            "                   selected session; on a zoom's Projects tab open that",
            "                   project's sessions within the year/month/day",
            "  Esc              step back out (session -> zoom -> browse)",
            "  Shift-Tab        cycle focus backward while browsing; else step back out",
            "  h/l              switch detail tabs (years/months: Overview/Models/Projects/",
            "                   Sessions; days: Overview/Projects/Sessions; a session adds a",
            "                   Turns tab -- per-turn cost over time (OpenCode + Claude) -- and",
            "                   a Tools tab -- per-tool/MCP-server token spend, OpenCode only;",
            "                   a Sources tab joins after Overview in the merged 'all' view)",
            "  j/k or Up/Down   move in the current list (or scroll detail)",
            "  mouse            wheel scrolls; click selects; double-click drills; click a tab",
            "  g/G              top / bottom",
            "  R                set range: all, 30d (or 30), 2m, 1y, 2026, 2026-05, start..end",
            "  a                show all time, keeping the current selection when possible",
            "  s                open the sort picker for the visible list (j/k move · Enter apply · Esc cancel)",
            "  i                ignore/unignore the selected project (project lists only)",
            "  I                show/hide ignored projects so they can be unignored",
            "  f                live filter: fuzzy over sessions (title/project/id), projects, Models; substring over Prices",
            "                   while filtering: ↑/↓ select · Enter keep · Esc cancel · Ctrl-U clear",
            "  x                clear filter",
            "  e                export the current list to a CSV in the working directory",
            "  y                copy the selected session id (or project path)",
            "  o                open the selected session's / project's directory",
            "  L                launch the selected session in its tool (Sessions tab/session detail only)",
            "                   opencode --session / claude --resume / codex resume",
            "                   w window · s split · v vsplit · p popup · y copy command",
            "                   needs tmux (or a launcher hook); hidden otherwise",
            "  D                toggle real/demo data on the fly (demo anonymizes titles/paths)",
            "  c                open the data-source picker (j/k move · Enter switch · Esc cancel):",
            "                   OpenCode / Claude Code / Codex / Copilot / pi / OpenClaw / all (when present)",
            "  T                Trends overlay: Daily / Weekly / Monthly / Calendar / Models / Providers / Sources",
            "                   (h/l tabs; j/k month/week/year; $ what-if prices)",
            "                   Calendar is a spend heat map: ↑↓←→ pick a day, Enter opens it,",
            "                   +/- adjusts the color granularity",
            "  P                model prices (the models.dev API rates used for $ what-if);",
            "                   j/k select a model, Enter lists the sessions that used it;",
            "                   press f to filter, r to refresh from models.dev, or e to export them",
            "  $                toggle what-if prices (what unpriced usage would cost at API list)",
            "  r                reload database",
            "  q                quit",
            "",
            "Cost caveat:",
            "  Cost is OpenCode's local attribution. Subscription or credit plans",
            "  (Claude Code, Codex, Copilot) aren't priced per token, so their usage",
            "  shows as unpriced $0.00 -- check your provider/subscription for the total.",
            "  Press $ for the what-if view: that usage priced at API list prices",
            "  (models.dev) -- an estimate of what you'd have paid without the subscription.",
            "  Sub-cent costs show as <$0.01; a red $0.00 means unpriced (no local price).",
            "  Range, sort, and the $ what-if view are remembered between runs (--no-state off).",
            "  Git worktrees fold into their main repo (--no-worktrees to keep them split).",
            "",
            "Press any other key to close help.",
        ]
        visible = max(1, bottom - y - 3)
        scroll = max(0, min(self.app.help_scroll, max(0, len(lines) - visible)))
        self.app.help_scroll = scroll
        for offset, line in enumerate(lines[scroll : scroll + visible]):
            self.write(
                stdscr,
                y + 2 + offset,
                2,
                shorten(line, width - 4),
                curses.color_pair(4) if line.endswith(":") else curses.A_NORMAL,
            )

    def price_intro_lines(self) -> list[str]:
        # The fixed header block above the P overlay's price table: where the rates
        # came from and what they mean. Pulled out so both the flat price_table_lines
        # (export/tests) and the navigable draw_prices share one source of truth.
        meta = price_cache_meta()
        if meta:
            when = (meta.get("fetched_at") or "?")[:10]
            source = f"models.dev cache · {meta.get('count', 0)} models · fetched {when}"
        else:
            source = "embedded offline snapshot (anthropic/openai/google)"
        return [
            f"Source: {source}.  Press r to refresh from models.dev.",
            "",
            "API list prices from models.dev, per 1M tokens. These are the rates",
            "OpenTab uses to estimate the $ what-if cost of unpriced subscription or",
            "credit usage -- approximate list prices, not your invoice.",
            "",
        ]

    def _price_row_text(self, name: str, namew: int) -> str:
        # One model's formatted price row (or the "local — no API cost" note).
        if is_local_provider(name):
            return f"{shorten(name, namew):{namew}}  {'local — no API cost':>31}"
        ir, orr, crr, cwr = model_price(name)
        return f"{shorten(name, namew):{namew}}  {ir:>7.2f} {orr:>7.2f} {crr:>7.2f} {cwr:>7.2f}"

    def _price_namew(self, names: list[str], width: int) -> int:
        return min(max(len(n) for n in names), max(12, width - 34))

    def price_table_lines(self, width: int) -> list[str]:
        # The models you have used (most spend first) and the models.dev API list
        # prices OpenTab applies for the "$" what-if estimate. Pure text so it can
        # be tested without a screen; draw_prices paints the same rows with a cursor.
        # The model set (and the active filter) is shared with the `e` export via
        # priced_model_names.
        names = self.priced_model_names()
        lines = self.price_intro_lines()
        if not names:
            lines.append(
                f"No model prices match the filter: {self.query}"
                if self.query
                else "No model usage on record yet."
            )
            return lines
        namew = self._price_namew(names, width)
        lines.append(f"{'model':{namew}}  {'input':>7} {'output':>7} {'cache-r':>7} {'cache-w':>7}")
        lines.extend(self._price_row_text(name, namew) for name in names)
        return lines

    def draw_prices(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        # Reference overlay (toggled with P) so the rates behind the "$" what-if
        # number are visible. The model list is navigable (j/k moves a cursor);
        # Enter on a model drills into the sessions that used it.
        if self.app.prices_model is not None:
            self.draw_price_sessions(stdscr, y, bottom, width)
            return
        self.box(
            stdscr,
            y,
            0,
            bottom - y,
            width,
            "Model prices  ·  j/k select · Enter sessions · f filter · r refresh · e export · q closes",
            active=True,
        )
        inner_w = width - 4
        intro = self.price_intro_lines()
        top = y + 2
        for offset, line in enumerate(intro):
            attr = curses.color_pair(4) if "models.dev" in line else curses.A_NORMAL
            self.write(stdscr, top + offset, 2, shorten(line, inner_w), attr)
        names = self.priced_model_names()
        head_y = top + len(intro)
        if not names:
            msg = (
                f"No model prices match the filter: {self.query}"
                if self.query
                else "No model usage on record yet."
            )
            self.write(stdscr, head_y, 2, shorten(msg, inner_w))
            return
        namew = self._price_namew(names, inner_w)
        header = f"{'model':{namew}}  {'input':>7} {'output':>7} {'cache-r':>7} {'cache-w':>7}"
        self.write(
            stdscr, head_y, 2, shorten(header, inner_w), curses.color_pair(4) | curses.A_BOLD
        )
        list_top = head_y + 1
        visible = max(1, bottom - list_top - 1)
        idx = max(0, min(self.app.prices_index, len(names) - 1))
        self.app.prices_index = idx
        scroll = max(0, min(self.app.prices_scroll, max(0, len(names) - visible)))
        if idx < scroll:  # keep the cursor inside the window
            scroll = idx
        elif idx >= scroll + visible:
            scroll = idx - visible + 1
        self.app.prices_scroll = scroll
        for offset, name in enumerate(names[scroll : scroll + visible]):
            text = self._price_row_text(name, namew)
            selected = scroll + offset == idx
            attr = curses.A_REVERSE | curses.A_BOLD if selected else curses.A_NORMAL
            self.write(stdscr, list_top + offset, 2, f"{shorten(text, inner_w):<{inner_w}}", attr)

    def price_session_lines(self, model: str, width: int) -> list[str]:
        # Pure-text body for the P overlay's per-model drill-in: every root session
        # that used `model`, with that model's cost/tokens within the session. Line 0
        # is the subtotal, line 1 the column header, the rest are the sessions.
        rows = self.price_model_sessions(model)
        if not rows:
            return [f"No sessions used {model}."]
        subtotal = sum(cost for _w, cost, _t in rows)
        lines = [
            f"{len(rows)} session(s) · {money(subtotal)} on this model",
            f"{'Started':<10} {'Cost':>9} {'Tokens':>8}  {self.src_col()}{'Title'}",
        ]
        for w, cost, tok in rows:
            lines.append(
                f"{w.created_at[:10]:<10} {money(cost):>9} {human_tokens(tok):>8}  "
                f"{self.src_col(w)}{w.title}"
            )
        return lines

    def draw_price_sessions(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        # The P overlay's per-model drill-in (Enter on a model). The subtotal +
        # column header stay pinned; only the session rows scroll. Esc backs out to
        # the model list; a close key shuts the overlay.
        model = self.app.prices_model
        self.box(
            stdscr,
            y,
            0,
            bottom - y,
            width,
            f"Sessions using {shorten(model, max(8, width - 48))}  ·  j/k scroll · Esc back · q closes",
            active=True,
        )
        inner_w = width - 4
        lines = self.price_session_lines(model, inner_w)
        top = y + 2
        if len(lines) == 1:  # the "No sessions used …" case
            self.write(stdscr, top, 2, shorten(lines[0], inner_w))
            return
        self.write(stdscr, top, 2, shorten(lines[0], inner_w), curses.color_pair(4))
        self.write(
            stdscr, top + 1, 2, shorten(lines[1], inner_w), curses.color_pair(4) | curses.A_BOLD
        )
        body = lines[2:]
        list_top = top + 2
        visible = max(1, bottom - list_top - 1)
        scroll = max(0, min(self.app.prices_scroll, max(0, len(body) - visible)))
        self.app.prices_scroll = scroll
        for offset, line in enumerate(body[scroll : scroll + visible]):
            self.write_rich(stdscr, list_top + offset, 2, shorten(line, inner_w))

    # Per-kind toast styling: (colour pair, sigil, header word). Reuses the one
    # restrained palette -- slate info, green success, amber warn, red error -- so a
    # toast reads the same as the cost/alert colours everywhere else; the sigil + word
    # give a non-colour cue too.
    TOAST_STYLE = {
        "info": (4, "·", "Note"),
        "success": (3, "✓", "Done"),
        "warn": (2, "▲", "Heads up"),
        "error": (5, "✕", "Error"),
    }
    TOAST_WIDTH = 46  # card width the message wraps within
    TOAST_MAX_LINES = 4  # cap wrapped message lines so a card can't fill the screen

    def draw_toasts(self, stdscr: curses.window, height: int, width: int) -> None:
        # Floating cards stacked in the top-right, just under the header separator,
        # newest on top. Each is a filled (reverse) coloured block: a header line
        # (sigil + kind word) over the message, which WRAPS across as many lines as it
        # needs (up to TOAST_MAX_LINES) instead of truncating, so nothing is hidden. The
        # run loop expires cards by time; the last fraction of a second renders dim.
        toasts = self.active_toasts()
        if not toasts:
            return
        now = self.toast_now()
        maxw = min(self.TOAST_WIDTH, max(16, width - 4))
        row = 3  # first body row, below the header hline (row 2)
        for toast in reversed(toasts):
            pair, sigil, label = self.TOAST_STYLE.get(toast.kind, self.TOAST_STYLE["info"])
            head = f" {sigil} {label}"
            wrapped = textwrap.wrap(toast.text, maxw - 1) or [""]
            if len(wrapped) > self.TOAST_MAX_LINES:  # mark the overflow rather than hide it
                wrapped = wrapped[: self.TOAST_MAX_LINES]
                wrapped[-1] = shorten(wrapped[-1], maxw - 2) + "…"
            body = [f" {line}" for line in wrapped]
            if row + len(body) >= height - 2:  # the whole card must clear the footer hline
                break
            cardw = min(max([len(head)] + [len(line) for line in body]) + 1, maxw)
            x = max(0, width - cardw - 2)
            fading = toast.remaining(now) < self.TOAST_FADE
            base = curses.color_pair(pair) | curses.A_REVERSE
            self.write(
                stdscr,
                row,
                x,
                f"{head:<{cardw}}",
                base | (curses.A_DIM if fading else curses.A_BOLD),
            )
            for i, line in enumerate(body):
                self.write(
                    stdscr,
                    row + 1 + i,
                    x,
                    f"{line:<{cardw}}",
                    base | (curses.A_DIM if fading else 0),
                )
            row += len(body) + 2  # card (header + body lines) plus a 1-row gap

    def draw_modal(
        self, stdscr: curses.window, scr_h: int, scr_w: int, title: str, lines: list
    ) -> None:
        # A small centered popup box floating over the current view (cleared interior so
        # the view doesn't bleed through). `lines` is a list of (text, attr); the caller
        # styles each row (header tint, A_REVERSE for a selected entry). Sized to content.
        content = [(str(t), a) for t, a in lines]
        inner_w = max([len(title) + 2] + [len(t) for t, _ in content] + [16])
        w = min(inner_w + 4, max(24, scr_w - 4))
        h = min(len(content) + 4, max(6, scr_h - 4))
        y = max(1, (scr_h - h) // 2)
        x = max(1, (scr_w - w) // 2)
        for row in range(y, y + h):  # clear the footprint first
            self.write(stdscr, row, x, " " * w)
        self.box(stdscr, y, x, h, w, title, active=True)
        field = w - 4
        for offset, (text, attr) in enumerate(content[: h - 4]):
            self.write(stdscr, y + 2 + offset, x + 2, f"{shorten(text, field):<{field}}", attr)

    def draw_source_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `c` picker: a small modal list of every present source. j/k moves the
        # highlight, Enter switches, Esc cancels (handled in handle_source_menu_key).
        entries = self.source_menu_entries()
        idx = self.source_menu_index % len(entries) if entries else 0
        lines = [("Browse spend recorded by which tool:", curses.color_pair(4)), ("", 0)]
        for offset, (_key, label, is_current) in enumerate(entries):
            marker = "●" if is_current else "○"
            suffix = "  (current)" if is_current else ""
            attr = curses.A_REVERSE | curses.A_BOLD if offset == idx else curses.A_NORMAL
            lines.append((f" {marker}  {label}{suffix}", attr))
        self.draw_modal(stdscr, scr_h, scr_w, "Switch source · j/k · Enter · Esc", lines)

    # Friendlier one-word names for the raw sort keys shown in the `s` picker.
    SORT_LABELS = {
        "cost": "Cost",
        "tokens": "Tokens",
        "date": "Date",
        "recency": "Recency",
        "subagents": "Subagents",
        "sessions": "Sessions",
        "title": "Title",
        "project": "Project",
        "model": "Model",
        "agent": "Agent",
        "depth": "Depth",
    }

    def draw_sort_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `s` picker: a small modal list of the sort keys valid for the current
        # list. j/k moves the highlight, Enter applies, Esc cancels (handled in
        # handle_sort_menu_key).
        options = self.sort_menu_options()
        idx = self.sort_menu_index % len(options) if options else 0
        current = self.effective_sort_by()
        lines = [("Order this list by:", curses.color_pair(4)), ("", 0)]
        for offset, key in enumerate(options):
            is_current = key == current
            marker = "●" if is_current else "○"
            suffix = "  (current)" if is_current else ""
            attr = curses.A_REVERSE | curses.A_BOLD if offset == idx else curses.A_NORMAL
            lines.append((f" {marker}  {self.SORT_LABELS.get(key, key)}{suffix}", attr))
        self.draw_modal(stdscr, scr_h, scr_w, "Sort by · j/k · Enter · Esc", lines)

    def draw_launch_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `L` picker: a small modal of launch targets. One keystroke picks (handled in
        # handle_launch_key); anything else cancels.
        session = self.launch_menu
        via = "launcher hook" if launcher_hook() else "tmux"
        idx = self.launch_menu_index % len(self.LAUNCH_TARGETS)
        lines = [
            (shorten(session.title or "(untitled)", 52), curses.color_pair(4)),
            (f"open in {via}:", curses.A_NORMAL),
            ("", 0),
        ]
        for offset, (kc, _kind, label) in enumerate(self.LAUNCH_TARGETS):
            attr = curses.A_REVERSE | curses.A_BOLD if offset == idx else curses.A_NORMAL
            lines.append((f" {kc}  {label}", attr))
        lines += [("", 0), (" Esc  cancel", curses.A_NORMAL)]
        self.draw_modal(stdscr, scr_h, scr_w, "Launch session · j/k · Enter · Esc", lines)

    def draw_price_prompt(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # Startup prompt when used models have no built-in price: offer a models.dev fetch.
        names = self.unknown_models
        shown = names[:5]
        lines = [
            (f"{len(names)} model(s) here have no built-in price:", curses.color_pair(4)),
            ("", 0),
        ]
        lines += [(f"  • {n}", curses.A_NORMAL) for n in shown]
        if len(names) > len(shown):
            lines.append((f"  … and {len(names) - len(shown)} more", curses.A_NORMAL))
        accent = curses.color_pair(6) | curses.A_BOLD
        lines += [
            ("", 0),
            ("Fetch current list prices from models.dev?", curses.A_NORMAL),
            ("", 0),
            (" y   yes, fetch now", accent),
            (" n   not now (ask again next run)", accent),
            (" d   don't ask again", accent),
            ("", 0),
            ("anytime: --refresh-models, or r in the P prices view", curses.color_pair(1)),
        ]
        self.draw_modal(stdscr, scr_h, scr_w, "Unpriced models found", lines)

    # --- Trends overlay -------------------------------------------------------
    def draw_trends(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        h = bottom - y
        self.box(stdscr, y, 0, h, width, f"Trends · {self.range_label()}", active=True)
        tabs = self.trend_tabs
        current = tabs[self.trend_tab % len(tabs)]
        unit = {"Daily": "month", "Weekly": "week"}.get(current)
        if current == "Calendar":
            hint = "↑↓←→ pick · +/- shades · Enter open · esc"
        elif unit:
            hint = f"h/l tabs · j/k {unit} · esc closes"
        else:
            hint = "h/l tabs · esc closes"
        self.draw_tabs(stdscr, y + 1, 2, width - len(hint) - 4, tabs, self.trend_tab, kind="trend")
        self.write(stdscr, y + 1, width - len(hint) - 2, hint, curses.color_pair(4))
        inner_w = width - 4
        content_h = h - 4
        if current == "Calendar":
            # The heat map paints itself: its cells carry per-cell color attributes,
            # so it bypasses the generic string -> write_rich path the other tabs use.
            self.draw_calendar(stdscr, y + 3, 2, content_h, inner_w)
            return
        if current == "Daily":
            lines = self.trend_daily(inner_w, content_h)
        elif current == "Weekly":
            lines = self.trend_weekly(inner_w, content_h)
        elif current == "Monthly":
            lines = self.trend_monthly(inner_w, content_h)
        elif current == "Providers":
            lines = self.trend_providers(inner_w, content_h)
        elif current == "Sources":
            lines = self.trend_sources(inner_w, content_h)
        else:
            lines = self.trend_models(inner_w, content_h)
        content = lines[:content_h]
        # Center the chart in the panel instead of hugging the left edge: the
        # graph lines (everything but the "# title") move as one block so the
        # bars stay aligned, split the slack evenly so narrow charts (a week, a
        # handful of months) sit in the middle. Each title line is then centered
        # on the graph's center, so it sits above the middle of the chart rather
        # than left-aligned to the block's edge.
        graph_w = max((len(line) for line in content if not line.startswith("# ")), default=0)
        graph_off = max(0, (inner_w - graph_w) // 2)
        graph_center = graph_off + graph_w // 2
        for i, line in enumerate(content):
            is_title = line.startswith("# ")
            attr = curses.color_pair(4) | curses.A_BOLD if is_title else curses.A_NORMAL
            x = max(0, graph_center - len(line) // 2) if is_title else graph_off
            self.write_rich(stdscr, y + 3 + i, 2 + x, shorten(line, inner_w - x), attr)

    def _bar_chart(self, pairs: list[tuple[str, float]], width: int, height: int) -> list[str]:
        # Vertical bar chart from chronological (label, value) pairs. Shows the most
        # recent buckets that fit; eighth-blocks give sub-row resolution on top.
        # The spend for each bar rides on top of it (no y-axis) — the peak is always
        # labelled and the rest fill in where there's room; when dense (e.g. daily),
        # bars pack in and labels/x-ticks are spaced so they never overlap.
        if not pairs or height < 5:
            return ["Not enough room to chart."]
        margin = 1  # the y-axis is gone; just a sliver of left padding
        plot_w = max(4, width - margin)
        label_w = max((len(label) for label, _ in pairs), default=2)
        ideal = label_w + 2  # the slot that fits an x-tick label with a column of air
        if len(pairs) * ideal <= plot_w:
            col_w = ideal  # room for a label under every bar
        else:
            col_w = next((c for c in (4, 3, 2) if len(pairs) * c <= plot_w), 1)
        fit = max(1, plot_w // col_w)
        shown = pairs[-fit:]
        n = len(shown)
        # Spread the shown bars across the *whole* plot width with a fractional
        # step, capped at the ideal slot so a handful of bars stay clustered (not
        # stretched comically wide). When bars are dense the integer col_w would
        # leave the right side empty and cram the wide "$x.xx" value labels;
        # filling the width gives every bar a little more horizontal air.
        step = min(float(ideal), plot_w / n)
        bar_w = max(1, min(int(step) - 1, 4))

        def x0_of(i: int) -> int:  # left edge of bar i, centred in its float-width slot
            lo = round(i * step)
            hi = round((i + 1) * step)
            return margin + lo + max(0, (hi - lo - bar_w) // 2)

        peak = max((v for _, v in shown), default=0.0)
        scale = peak or 1.0  # bar-height denominator; guards an all-empty window
        rows_n = max(2, height - 4)  # value labels + bars + baseline + x-ticks + summary
        total_w = margin + round(n * step)
        # grid row 0 is the label margin above the tallest bar; 1..rows_n are bars.
        grid = [[" "] * total_w for _ in range(rows_n + 1)]
        tops: list[tuple[int, int, float]] = []  # (col, top filled row, value)
        for i, (_, v) in enumerate(shown):
            full, rem = divmod(round((v / scale) * rows_n * 8), 8)
            x0 = x0_of(i)
            for b in range(full):  # full cells from the bottom up
                for dx in range(bar_w):
                    grid[rows_n - b][x0 + dx] = "█"
            if rem:
                for dx in range(bar_w):
                    grid[rows_n - full][x0 + dx] = BLOCKS_UP[rem]
            filled = full + (1 if rem else 0)
            if filled:
                tops.append((i, rows_n - filled + 1, v))

        def place_value(i: int, top_row: int, v: float) -> None:
            labels = [money_label(v)]
            if 1 <= v < 1000:
                labels.append(f"${v:.0f}")
            labels = [label for j, label in enumerate(labels) if label and label not in labels[:j]]
            if not labels:
                return
            center = x0_of(i) + bar_w // 2
            if top_row - 1 < 0:
                return
            for text in labels:
                start = max(margin, min(center - len(text) // 2, total_w - len(text)))
                lo, hi = start - 1, start + len(text)  # keep a blank column on each side
                cols = range(max(margin, lo), min(total_w, hi + 1))
                # Sit just above the bar; if a neighbour's label already owns that
                # row, float up to the next free one so the bar still gets its price.
                for lrow in range(top_row - 1, -1, -1):
                    if all(grid[lrow][c] == " " for c in cols):
                        for k, ch in enumerate(text):
                            grid[lrow][start + k] = ch
                        return

        # Peak first so its value is never crowded out, then the rest left-to-right.
        tops.sort(key=lambda t: t[2], reverse=True)
        if tops:
            place_value(*tops[0])
        for spec in sorted(tops[1:], key=lambda t: t[0]):
            place_value(*spec)
        out = ["".join(r).rstrip() for r in grid]
        out.append(" " * margin + "─" * (total_w - margin))
        # x-axis tick labels, greedily spaced left-to-right so they never overlap,
        # with the final bucket always labelled at the right edge.
        axis = [" "] * total_w

        def place(pos: int, label: str) -> None:
            for j, ch in enumerate(label):
                if 0 <= pos + j < len(axis):
                    axis[pos + j] = ch

        # Always anchor the final (most recent) bucket at the right edge, then fill
        # earlier ticks greedily in the space before it.
        tail = len(axis) - len(shown[-1][0])
        place(tail, shown[-1][0])
        next_free = margin
        for i, (label, _) in enumerate(shown[:-1]):
            pos = x0_of(i)
            if pos >= next_free and pos + len(label) < tail:
                place(pos, label)
                next_free = pos + len(label) + 1
        out.append("".join(axis).rstrip())
        total = sum(v for _, v in shown)  # match exactly what's charted
        if total:
            peak_label = max(shown, key=lambda kv: kv[1])[0]
            out.append(
                f"{' ' * margin}peak {money(peak)} on {peak_label}    "
                f"total {money(total)}    avg {money(total / len(shown))}"
            )
        else:
            out.append(f"{' ' * margin}no spend in view")
        if len(shown) < len(pairs):
            out.append(f"{' ' * margin}(most recent {len(shown)} of {len(pairs)} — widen for more)")
        return out

    def trend_daily(self, width: int, height: int) -> list[str]:
        # One calendar month at a time (navigate with j/k); the x-axis is the day
        # of the month, so it stays readable instead of cramming the whole range.
        months = sorted({w.created_at[:7] for w in self.all_workflows}, reverse=True)
        if not months:
            return ["# Daily spend", "", "No spend in the active range."]
        idx = max(0, min(self.trend_month_index, len(months) - 1))
        month = months[idx]
        ndays = int(month_bounds(month)[1][8:10])
        by_day: dict[int, float] = defaultdict(float)
        for w in self.all_workflows:
            if w.created_at[:7] == month:
                by_day[int(w.created_at[8:10])] += w.total_cost
        pairs = [(str(d), by_day.get(d, 0.0)) for d in range(1, ndays + 1)]
        title = f"# Daily spend · {month}"
        if len(months) > 1:
            title += f"   ({idx + 1}/{len(months)} — j/k older/newer month)"
        return [title, ""] + self._bar_chart(pairs, width, height - 2)

    def trend_weekly(self, width: int, height: int) -> list[str]:
        # One ISO week at a time (navigate with j/k), x-axis is Mon..Sun of that week.
        # Like trend_daily, but a week instead of a month -- finer-grained browsing.
        weeks = sorted({week_key(w.created_at) for w in self.all_workflows}, reverse=True)
        if not weeks:
            return ["# Weekly spend", "", "No spend in the active range."]
        idx = max(0, min(self.trend_week_index, len(weeks) - 1))
        monday = weeks[idx]
        start = datetime.strptime(monday, "%Y-%m-%d")
        by_date: dict[str, float] = defaultdict(float)
        for w in self.all_workflows:
            if week_key(w.created_at) == monday:
                by_date[w.created_at[:10]] += w.total_cost
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        pairs = [
            (names[i], by_date.get((start + timedelta(days=i)).strftime("%Y-%m-%d"), 0.0))
            for i in range(7)
        ]
        sunday = (start + timedelta(days=6)).strftime("%Y-%m-%d")
        title = f"# Weekly spend · {monday} – {sunday}"
        if len(weeks) > 1:
            title += f"   ({idx + 1}/{len(weeks)} — j/k older/newer week)"
        return [title, ""] + self._bar_chart(pairs, width, height - 2)

    def trend_monthly(self, width: int, height: int) -> list[str]:
        by_month: dict[str, float] = defaultdict(float)
        for w in self.all_workflows:
            by_month[w.created_at[:7]] += w.total_cost
        if not by_month:
            return ["# Monthly spend", "", "No spend in the active range."]
        keys = sorted(by_month)
        pairs = [(m, by_month.get(m, 0.0)) for m in month_range(keys[0], keys[-1])]
        return ["# Monthly spend (cost per month)", ""] + self._bar_chart(pairs, width, height - 2)

    def draw_calendar(
        self, stdscr: curses.window, top: int, left: int, height: int, width: int
    ) -> None:
        # GitHub-style spend heat map for one navigable calendar year: weekday rows
        # (Mon..Sun) by week columns, each day shaded by how it compares to the year's
        # busiest day. Paints its own cells (unlike the string-returning trend_* tabs)
        # because the heat shades are per-cell color attributes, not regex spans.
        self.app._cal_geom = None  # cleared until a full grid is drawn (mouse hit-testing)
        years = self.calendar_years()
        if not years:
            self.write(stdscr, top, left, "No spend in the active range.", curses.color_pair(1))
            return
        if height < 13 or width < 24:
            self.write(stdscr, top, left, "Not enough room for the calendar.", curses.color_pair(1))
            return
        idx = max(0, min(self.trend_year_index, len(years) - 1))
        year = years[idx]
        by_date: dict[str, float] = defaultdict(float)
        sessions = 0
        for w in self.all_workflows:
            if w.created_at[:4] == year:
                by_date[w.created_at[:10]] += w.total_cost
                sessions += 1
        grid, months, ncols = calendar_cells(year, by_date)
        peak = max(by_date.values(), default=0.0)
        total = sum(by_date.values())
        active = sum(1 for v in by_date.values() if v > 0)
        levels = self.cal_levels  # live granularity (+/-): more levels = more shades
        self._sync_heat_palette()  # restyle the color pairs to the current granularity

        gutter = 4  # the weekday label ("Mon") plus a trailing space, then the grid
        pitch = 2  # one glyph + a one-column gap per day, so cells don't run together
        # A narrow panel can't hold all 53 weeks; show the most recent ones that fit.
        max_cols = max(1, (width - gutter) // pitch)
        start_col = max(0, ncols - max_cols)
        shown = ncols - start_col
        grid_w = shown * pitch
        xoff = max(0, (width - (gutter + grid_w)) // 2)  # center the block in the panel
        gx = left + xoff + gutter  # screen x of the first shown grid column
        # Breathe vertically when the panel is tall: a blank line between weekday rows
        # (else keep them tight so a short panel still fits in its 13-row minimum).
        row_pitch = 2 if height >= 20 else 1
        gy0 = top + 3  # screen row of the first (Mon) weekday line
        jan1 = datetime(int(year), 1, 1)
        grid_start = jan1 - timedelta(days=jan1.weekday())  # Monday of week column 0
        # Stash the geometry so a mouse click can resolve back to a date.
        self.app._cal_geom = (gy0, row_pitch, gx, pitch, start_col, shown, year, grid_start)
        cursor = self._effective_cursor(year, by_date)  # the highlighted day

        title = f"Spend calendar · {year}"
        if len(years) > 1:
            title += f"   ({idx + 1}/{len(years)} — j/k older/newer year)"
        self.write(
            stdscr,
            top,
            left + max(0, (width - len(title)) // 2),
            title,
            curses.color_pair(4) | curses.A_BOLD,
        )

        # Month labels anchored over each month's first week column; the spacing leaves
        # room for all twelve, but skip any that would collide with the previous one.
        next_free_x = gx
        for col, abbr in months:
            c = col - start_col
            mx = gx + c * pitch
            if c >= 0 and mx >= next_free_x and mx + len(abbr) <= gx + grid_w:
                self.write(stdscr, top + 2, mx, abbr, curses.color_pair(1))
                next_free_x = mx + len(abbr) + 1

        # Every weekday gets its own labeled row; the heat grid sits to the right.
        weekday_labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        for r in range(7):
            ry = top + 3 + r * row_pitch
            self.write(stdscr, ry, left + xoff, weekday_labels[r], curses.color_pair(1))
            for c in range(shown):
                cell = grid[r][start_col + c]
                if cell is None:
                    continue  # a padding day outside the year: leave it blank
                self.write(
                    stdscr,
                    ry,
                    gx + c * pitch,
                    *self._heat_cell(heat_level(cell, peak, levels), levels),
                )

        # Frame the highlighted day in the gap columns around its cell, so the brackets
        # never overwrite a neighbouring glyph.
        if cursor and cursor[:4] == year:
            cd = datetime.strptime(cursor, "%Y-%m-%d")
            ccol = (cd - grid_start).days // 7 - start_col
            if 0 <= ccol < shown:
                cy = gy0 + cd.weekday() * row_pitch
                cxx = gx + ccol * pitch
                self.write(stdscr, cy, cxx - 1, "[", curses.color_pair(6) | curses.A_BOLD)
                self.write(stdscr, cy, cxx + 1, "]", curses.color_pair(6) | curses.A_BOLD)

        # Legend: spell out the per-day dollar band each shade stands for, on the same
        # log scale as the cells, so the colors read as numbers (· is a day with no
        # spend; each shade is "up to" its bound, the hottest = the peak day). The bounds
        # bunch up toward the low end — that's the log scale spreading the common days.
        ly = top + 3 + 6 * row_pitch + 2  # one blank line below the last weekday row
        lx = left + xoff
        sep = "  " if levels <= 6 else " "  # tighten the bands so a finer ramp still fits
        if peak > 0:
            self.write(stdscr, ly, lx, "per day  ", curses.color_pair(1))
            lx += 9
            bounds = [math.expm1(math.log1p(peak) * i / levels) for i in range(levels + 1)]
            for level in range(levels + 1):
                self.write(stdscr, ly, lx, *self._heat_cell(level, levels))
                lx += 1
                label = f" $0{sep}" if level == 0 else f" ≤{heat_band_label(bounds[level])}{sep}"
                self.write(stdscr, ly, lx, label, curses.color_pair(1))
                lx += len(label)
        else:
            self.write(stdscr, ly, lx, "Less ", curses.color_pair(1))
            lx += 5
            for level in range(levels + 1):
                self.write(stdscr, ly, lx, *self._heat_cell(level, levels))
                lx += 1
            self.write(stdscr, ly, lx, " More", curses.color_pair(1))
        if total > 0:
            peak_date = max(by_date, key=by_date.__getitem__)
            summary = (
                f"total {money(total)}   peak {money(peak)} on {peak_date}   {active} active days"
            )
        elif sessions:
            summary = f"{sessions} sessions, no recorded spend this year"
        else:
            summary = "no spend this year"
        # The year summary, then the highlighted day, then (for $0 years) the $ nudge —
        # painted top-down and clipped to whatever rows are left in the panel.
        info = [summary]
        if cursor:
            cd = datetime.strptime(cursor, "%Y-%m-%d")
            day_cost = by_date.get(cursor, 0.0)
            day_sessions = sum(1 for w in self.all_workflows if w.created_at[:10] == cursor)
            label = f"▸ {weekday_labels[cd.weekday()]} {cursor}   "
            if day_sessions:
                noun = "session" if day_sessions == 1 else "sessions"
                info.append(f"{label}{money(day_cost)}   {day_sessions} {noun}   Enter opens")
            else:
                info.append(f"{label}no sessions   move with ←↑↓→")
        if total == 0 and sessions and not self.show_api_prices:
            info.append("$ prices subscription/credit usage at API list rates")
        for i, line in enumerate(info):
            row_y = ly + 1 + i
            if row_y >= top + height:
                break
            self.write_rich(stdscr, row_y, left + xoff, shorten(line, width), curses.A_NORMAL)

    def _sync_heat_palette(self) -> None:
        # Re-init the heat color pairs (8..) to the current granularity so +/- restyles
        # the map live. App stays curses-free, so this init_pair lives in the renderer.
        for i, col in enumerate(heat_palette(self.cal_levels, self.has256)):
            curses.init_pair(8 + i, col, -1)

    def _heat_cell(self, level: int, levels: int) -> tuple[str, int]:
        # (glyph, attr) for one heat level: a distinct color per level, plus a glyph that
        # keeps levels apart where the color ramp collapses (8-color / mono terminals).
        if level <= 0:
            return HEAT_EMPTY_GLYPH, curses.color_pair(1) | curses.A_DIM
        return heat_glyph(level, levels, self.has256), curses.color_pair(7 + level) | curses.A_BOLD

    def trend_models(self, width: int, height: int) -> list[str]:
        agg = self.aggregate_models(self.all_workflows)
        rows = [(name, float(it["cost"])) for name, it in agg if float(it["cost"]) > 0]
        rows = rows[: max(1, height - 3)]
        if not rows:
            return ["# Model spend", "", "No priced model spend in the active range."]
        total = sum(c for _, c in rows)
        peak = max(c for _, c in rows) or 1.0
        # Names get priority so long ids like claude-opus-4-5-20251101 show in
        # full; the bar takes only the leftover (kept modest) instead of eating
        # the width and forcing names to truncate.
        tail = 20  # spacing + money (>=11) + percent (5)
        namew = min(max(len(n) for n, _ in rows), max(12, width - tail - 4))
        barw = max(3, min(24, width - namew - tail))
        lines = ["# Model spend (priced, in range)", ""]
        for name, cost in rows:
            bar = "█" * max(0, round((cost / peak) * barw))
            lines.append(
                f"{shorten(name, namew):{namew}}  {bar:<{barw}} {money(cost):>11} {pct(cost, total):>5}"
            )
        return lines

    def trend_providers(self, width: int, height: int) -> list[str]:
        # Roll the per-model spend up to its provider (the "openai" in "openai/gpt-5"),
        # so you can compare e.g. openai vs github-copilot. Subscription/credit providers
        # record $0 per message, so their cost only shows once "$" reprices unpriced
        # usage at API list rates -- the cost column and bar react to it live. We still
        # list those providers when "$" is off (tokens are the tell) and nudge toward "$".
        by_provider: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"cost": 0.0, "tokens": 0, "runs": 0}
        )
        for name, it in self.aggregate_models(self.all_workflows):
            item = by_provider[str(name).split("/", 1)[0] or "unknown"]
            item["cost"] = float(item["cost"]) + float(it["cost"])
            item["tokens"] = int(item["tokens"]) + int(it["tokens"])
            item["runs"] = int(item["runs"]) + int(it["runs"])
        rows = sorted(
            by_provider.items(),
            key=lambda kv: (float(kv[1]["cost"]), int(kv[1]["tokens"])),
            reverse=True,
        )
        if not rows:
            return ["# Spend by provider", "", "No model usage in the active range."]
        rows = rows[: max(1, height - 4)]
        total_cost = sum(float(it["cost"]) for _, it in rows)
        peak = max((float(it["cost"]) for _, it in rows), default=0.0) or 1.0
        namew = min(max(len(p) for p, _ in rows), max(10, width - 44))
        barw = max(3, min(20, width - namew - 38))
        lines = [
            "# Spend by provider",
            "",
            f"{'Provider':{namew}}  {'':{barw}} {'Cost':>11} {'Share':>5} {'Tokens':>9} {'Msgs':>7}",
        ]
        for provider, it in rows:
            bar = "█" * max(0, round((float(it["cost"]) / peak) * barw))
            lines.append(
                f"{shorten(provider, namew):{namew}}  {bar:<{barw}} "
                f"{money(float(it['cost'])):>11} {pct(float(it['cost']), total_cost):>5} "
                f"{human_tokens(int(it['tokens'])):>9} {int(it['runs']):>7}"
            )
        if not self.show_api_prices and any(
            float(it["cost"]) == 0 and int(it["tokens"]) for _, it in rows
        ):
            lines += ["", "$ prices subscription/credit usage at API list rates"]
        return lines

    def trend_sources(self, width: int, height: int) -> list[str]:
        # The Trends overlay's headline cut: spend by tool across the whole range.
        return self.source_table(self.all_workflows, width, limit=max(1, height - 4))

    def source_table(
        self, workflows: list[Workflow], width: int, limit: int | None = None
    ) -> list[str]:
        # Spend grouped by the *tool* it came from (OpenCode / Claude Code / Codex).
        # Shared by the Trends "Sources" tab (whole range) and the per-month/day/
        # project "Sources" detail tabs (a scoped slice). Subscription rows (Claude
        # Code, Codex) cost $0 until "$" reprices their tokens, so the bar reacts live.
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
        if not rows:
            return ["# Spend by source", "", "No sessions in the active range."]
        if limit is not None:
            rows = rows[:limit]
        total_cost = sum(float(it["cost"]) for _, it in rows)
        peak = max((float(it["cost"]) for _, it in rows), default=0.0) or 1.0
        namew = min(max(len(s) for s, _ in rows), max(10, width - 44))
        barw = max(3, min(20, width - namew - 38))
        lines = [
            "# Spend by source",
            "",
            f"{'Source':{namew}}  {'':{barw}} {'Cost':>11} {'Share':>5} {'Tokens':>9} {'Sess':>7}",
        ]
        for source, it in rows:
            bar = "█" * max(0, round((float(it["cost"]) / peak) * barw))
            lines.append(
                f"{shorten(source, namew):{namew}}  {bar:<{barw}} "
                f"{money(float(it['cost'])):>11} {pct(float(it['cost']), total_cost):>5} "
                f"{human_tokens(int(it['tokens'])):>9} {int(it['sessions']):>7}"
            )
        if not self.show_api_prices and any(
            float(it["cost"]) == 0 and int(it["tokens"]) for _, it in rows
        ):
            lines += ["", "$ prices subscription/credit usage at API list rates"]
        return lines

    def box(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        h: int,
        w: int,
        title: str,
        active: bool = False,
    ) -> None:
        if h <= 1 or w <= 1:
            return
        border_attr = curses.color_pair(6) | curses.A_BOLD if active else curses.A_NORMAL
        title_attr = border_attr if active else curses.color_pair(1) | curses.A_BOLD
        stdscr.addch(y, x, curses.ACS_ULCORNER, border_attr)
        stdscr.addch(y, x + w - 1, curses.ACS_URCORNER, border_attr)
        stdscr.addch(y + h - 1, x, curses.ACS_LLCORNER, border_attr)
        stdscr.addch(y + h - 1, x + w - 1, curses.ACS_LRCORNER, border_attr)
        stdscr.hline(y, x + 1, curses.ACS_HLINE, w - 2, border_attr)
        stdscr.hline(y + h - 1, x + 1, curses.ACS_HLINE, w - 2, border_attr)
        stdscr.vline(y + 1, x, curses.ACS_VLINE, h - 2, border_attr)
        stdscr.vline(y + 1, x + w - 1, curses.ACS_VLINE, h - 2, border_attr)
        self.write(stdscr, y, x + 2, f" {shorten(title, w - 6)} ", title_attr)

    def hline(self, stdscr: curses.window, y: int, x: int, w: int) -> None:
        stdscr.hline(y, x, curses.ACS_HLINE, max(0, w - 1))

    def write(self, stdscr: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = stdscr.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        try:
            stdscr.addstr(y, x, text[: max(0, width - x - 1)], attr)
        except curses.error:
            pass

    def write_rich(self, stdscr: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
        self.write(stdscr, y, x, text, attr)
        if attr & curses.A_BOLD and text.startswith("# "):
            return
        if text.lstrip().startswith("ID:"):
            return  # session ids can contain money/token-like runs; don't recolor them
        for match in MONEY_PATTERN.finditer(text):
            self.write(
                stdscr, y, x + match.start(), match.group(0), self.money_attr(match.group(0))
            )
        for match in TOKEN_PATTERN.finditer(text):
            token_text = match.group(0)
            self.write(stdscr, y, x + match.start(), token_text, self.token_attr(token_text))
