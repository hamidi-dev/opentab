"""Renderer: all drawing."""
from __future__ import annotations

import math
import textwrap
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from opentab import __version__
from opentab.models import DaySummary, MonthSummary, ProjectSummary, Workflow, YearSummary
from opentab.themes import hex_rgb1000, nearest_256, ramp

if TYPE_CHECKING:
    from opentab.tui.app import App

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

from opentab.formatting import (
    BAR_CELLS,
    BAR_GLYPH_PATTERN,
    MONEY_PATTERN,
    TOKEN_PATTERN,
    clip,
    cost_bar,
    display_width,
    human_tokens,
    money,
    money_label,
    pad,
    pct,
    short_path,
    shorten,
    tokens,
)
from opentab.heatmap import (
    BLOCKS_UP,
    HEAT_EMPTY_GLYPH,
    PRICE_HEAT_BASE_PAIR,
    PRICE_HEAT_LEVELS,
    calendar_cells,
    heat_band_label,
    heat_glyph,
    heat_level,
    heat_palette,
)
from opentab.models import ALL_YEARS, year_label
from opentab.pricing import api_equivalent_cost, family_label, model_price, price_cache_meta
from opentab.util import fuzzy_score, launcher_hook, tool_namespace


class Renderer:
    """All terminal drawing for OpenTab.

    Holds the App and reads its state through __getattr__, so the App stays a
    pure controller (state + input + data) with no curses/rendering code.
    """

    # Ordered (sort_key, label) for the clickable headers of the sortable picker
    # lists, matching the labels the *_header builders emit. The session picker
    # prepends a varying date column (Started/Time) and, in multi-project views,
    # inserts a Project column before Title, so neither is listed here.
    SESSION_SORT_COLUMNS = (
        ("cost", "Cost"),
        ("tokens", "Tokens"),
        ("subagents", "Subs"),
        ("title", "Title"),
    )
    PROJECT_SORT_COLUMNS = (
        ("project", "Project"),
        ("cost", "Cost"),
        ("tokens", "Tokens"),
        ("sessions", "Ses"),
        ("subagents", "Subs"),
    )
    # The P overlay's clickable price-table headers: (sort_key, label) in column
    # order, matching what _price_header draws. The active column carries a v/^
    # direction arrow; _register_sort_header locates each base label in the drawn
    # text (arrow included) so the click zones line up.
    PRICE_SORT_COLUMNS = (
        ("model", "model"),
        ("eff", "eff $/M"),
        ("use", "use"),
        ("input", "input"),
        ("output", "output"),
        ("cache_read", "cacheR"),
        ("cache_write", "cacheW"),
    )

    def __init__(self, app: App) -> None:
        self.app = app
        # Clickable hit regions, rebuilt every draw() so they always match what is
        # on screen. Each is ("rows", kind, y0, y_last, x0, x1, start) for a list
        # (click row y selects index start + (y - y0)) or (kind, y, x0, x1, index)
        # for a tab label where kind is "tab"/"trend". hit() resolves a click.
        self.regions: list[tuple] = []
        # Clickable column-header zones for sortable lists: (y, x0, x1, key, target).
        # Rebuilt every draw() alongside regions; sort_hit() resolves a click.
        self.sort_regions: list[tuple] = []
        # Trends-overlay paint artifacts, stashed by the drawers for draw_trends to
        # turn into mouse geometry / row highlights: the last bar chart's per-bar
        # slots + clickable height, and where a ranked/sessions list's rows sit
        # within its returned lines as (first line, rows drawn, dataset offset).
        self._bar_slots: list[tuple[int, int, str]] | None = None
        self._bar_click_rows = 0
        self._trend_rows_at: tuple[int, int, int] | None = None
        # Turns tab: which detail-line indices are ▸ prompt headers (click unfolds).
        self._turn_header_at: dict[int, str] = {}

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

    def _register_sort_header(
        self, y: int, x_base: int, header: str, columns, target: str, max_w: int
    ) -> None:
        # Make each column label in a sortable list header clickable so a click on
        # the name sorts the list by that column. `columns` is the ordered
        # (key, label) list exactly as the labels appear in `header`; we locate each
        # in the text actually drawn (post-shorten) so every zone lines up with what
        # is on screen even when the active-sort arrow has shifted columns right.
        drawn = shorten(header, max_w)
        pos = 0
        for key, label in columns:
            i = drawn.find(label, pos)
            if i < 0:
                continue
            self.sort_regions.append((y, x_base + i, x_base + i + len(label) - 1, key, target))
            pos = i + len(label)

    def sort_hit(self, my: int, mx: int) -> tuple[str, str] | None:
        # Resolve a click over a column header to (sort_key, target), or None.
        for y, x0, x1, key, target in self.sort_regions:
            if my == y and x0 <= mx <= x1:
                return key, target
        return None

    def year_row_text(self, year: YearSummary, marker: str) -> str:
        return (
            f"{marker} {year_label(year.year):<9} {money(year.cost):>9} "
            f"{human_tokens(year.tokens):>7} {year.workflows:>3} ses"
        )

    def month_row_text(self, month: MonthSummary, marker: str) -> str:
        return (
            f"{marker} {month.month} {money(month.cost):>9} "
            f"{human_tokens(month.tokens):>7} {month.workflows:>3} ses"
        )

    def day_row_text(self, day: DaySummary, marker: str) -> str:
        return (
            f"{marker} {day.day} {money(day.cost):>9} "
            f"{human_tokens(day.tokens):>7} {day.workflows:>3} ses"
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
            f"{marker} {pad(name, name_width)} "
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
        longest = max(
            (display_width(short_path(p.directory, 999)) for p in self.projects), default=8
        )
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

    def draw_time_panels(
        self, stdscr: curses.window, top: int, avail: int, left: int, focus: str | None
    ) -> None:
        # The three stacked time panels (browse sidebar, and the inactive sidebar
        # beside a zoomed detail -- focus=None dims all three). Years is short (few
        # rows), so size it to show every year (panels render h-3 rows, hence +3),
        # capped so a long history can't starve Months/Days; those split the rest.
        years_h = max(4, min(len(self.years) + 3, max(4, avail // 3)))
        remaining = avail - years_h
        months_h = max(4, min(len(self.months) + 3, remaining // 2))
        days_h = remaining - months_h
        self.draw_year_list(stdscr, top, 0, years_h, left, active=focus == "years")
        self.draw_month_list(stdscr, top + years_h, 0, months_h, left, active=focus == "months")
        self.draw_day_list(
            stdscr, top + years_h + months_h, 0, days_h, left, active=focus == "days"
        )

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
            # Dispatch by tab NAME like draw_detail: current_tabs() appends Turns and
            # Tools per session, so a fixed index would page the wrong line count.
            tabs = self.current_tabs()
            current = tabs[self.tab % len(tabs)]
            if current == "Models":
                return self.detail_models(workflow, content_width)
            if current == "Subagents":
                return self.detail_subagents(workflow, content_width)
            if current == "Turns":
                return self.detail_turns(workflow, content_width)
            if current == "Tools":
                return self.detail_tools(workflow, content_width)
            return self.detail_overview(workflow, content_width)

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
        self.apply_background(stdscr)  # theme bg fills the screen (before erase reads it)
        stdscr.erase()
        self.regions = []  # rebuilt below as panels draw, for this frame's clicks
        self.sort_regions = []  # column-header sort zones, same lifecycle as regions
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
            # lazygit-style: the detail is the active pane of the same split, the
            # sidebar stays put (inactive, still clickable to re-scope); `+`
            # maximizes the detail full-screen on demand.
            zx, zw = 0, width
            if not self.zoom_maximized:
                left = self.browse_left_width(width)
                if self.browse_mode == "projects":
                    self.draw_project_list(stdscr, top, 0, avail, left, active=False)
                else:
                    self.draw_time_panels(stdscr, top, avail, left, focus=None)
                zx, zw = left, width - left
            if self.browse_mode == "projects":
                self.draw_project_detail(stdscr, top, zx, avail, zw)
            elif self.focus == "years":
                self.draw_year_detail(stdscr, top, zx, avail, zw)
            elif self.focus == "months":
                self.draw_month_detail(stdscr, top, zx, avail, zw)
            else:
                self.draw_day_detail(stdscr, top, zx, avail, zw)
        elif self.browse_mode == "projects":
            left = self.browse_left_width(width)
            self.draw_project_list(stdscr, top, 0, avail, left)
            self.draw_project_detail(stdscr, top, left, avail, width - left, active=False)
            # Catch-all region under the preview's own tabs/rows (first match wins,
            # so it's appended last): a click anywhere in the pane focuses it.
            self._add_rows_region("detail", top, left, width - 1, 0, avail)
        else:
            left = self.browse_left_width(width)
            self.draw_time_panels(stdscr, top, avail, left, focus=self.focus)
            rx, rw = left, width - left
            if self.focus == "years":
                self.draw_year_detail(stdscr, top, rx, avail, rw, active=False)
            elif self.focus == "months":
                self.draw_month_detail(stdscr, top, rx, avail, rw, active=False)
            else:
                self.draw_day_detail(stdscr, top, rx, avail, rw, active=False)
            self._add_rows_region("detail", top, rx, width - 1, 0, avail)

        # Small centered modals float on top of the current view (so context stays
        # visible behind them), unlike the full-body help/prices/trends overlays.
        if self.price_prompt:
            self.draw_price_prompt(stdscr, height, width)
        elif self.theme_menu:
            self.draw_theme_menu(stdscr, height, width)
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
        ignored_count = len(self.ignored_projects) + len(self.ignored_sessions)
        if ignored_count:
            segs.append((f"  ·  ignored: {ignored_count}", active))
        if self.show_bookmarks_only:
            segs.append(("  ·  ★ bookmarks only", active))
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
        return x + display_width(clipped)

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
            if self.zoom_source and self.on_sessions_tab:
                segs.append(self.zoom_source)
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
            if self.zoom_source:
                segs.append(self.zoom_source)
            sess = self.current_session()
            segs.append(shorten(sess.title, 28) if sess else "session")
            segs.append(tab_name)
        elif self.focus == "years":
            if self.focused_year:
                segs.append(self.focused_year)
            if self.zoom_project and self.on_sessions_tab:
                segs.append(short_path(self.zoom_project, 24))
            if self.zoom_source and self.on_sessions_tab:
                segs.append(self.zoom_source)
            segs.append(tab_name)
        elif self.focus == "months":
            if self.focused_month:
                segs.append(self.focused_month)
            if self.zoom_project and self.on_sessions_tab:
                segs.append(short_path(self.zoom_project, 24))
            if self.zoom_source and self.on_sessions_tab:
                segs.append(self.zoom_source)
            segs.append(tab_name)
        else:
            if self.focused_month:
                segs.append(self.focused_month)
            if self.active_day:
                segs.append(self.active_day)
            if self.zoom_project and self.on_sessions_tab:
                segs.append(short_path(self.zoom_project, 24))
            if self.zoom_source and self.on_sessions_tab:
                segs.append(self.zoom_source)
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
        # Each entry is (label, active) -- or a list of such pairs painted as one
        # contiguous segment, so a single token inside a hint can light up. An active
        # toggle -- its overlay or mode is currently ON -- renders in the orange
        # accent so the footer reflects state at a glance (e.g. hitting T highlights
        # "T trends" while the overlay is open).
        parts: list = []
        if self.view == "browse" and self.browse_mode == "time":
            # The focused panel's own token lights up, so "where am I?" is answered
            # by the same hint that says how to move (Tab).
            parts.append(
                [
                    ("Tab ", False),
                    ("yr", self.focus == "years"),
                    ("/", False),
                    ("mo", self.focus == "months"),
                    ("/", False),
                    ("day", self.focus == "days"),
                ]
            )
        parts.append(("Enter in", False))
        if self.view != "browse":
            parts.append(("Esc out", False))
        if self.view == "zoom":
            parts.append(("+ max", self.zoom_maximized))
        if self.view != "session":
            # Like yr/mo/day: the active browse mode's own letter lights up.
            parts.append(
                [
                    ("p", self.browse_mode == "projects"),
                    ("/", False),
                    ("t", self.browse_mode == "time"),
                    (" mode", False),
                ]
            )
        if self.can_toggle_ignore():
            parts.append(("i ignore", False))
        if self.ignored_projects or self.ignored_sessions:
            parts.append(("I ignored", self.show_ignored_projects))
        # `b` lights up while the selected session is starred; `B` while the
        # bookmarks-only view is on (and is offered only once something is starred).
        target = self.bookmark_target()
        if target is not None:
            parts.append(("b mark", target.id in self.bookmarks))
        if self.bookmarks or self.show_bookmarks_only:
            parts.append(("B marked", self.show_bookmarks_only))
        if self.can_switch_source():
            # `c` opens the source picker (lights up while it's open); the active source
            # is the header chip, so the key just advertises the menu, not a destination.
            parts.append(("c source", self.source_menu))
        # active/non-default modifiers light up too, matching the header chips: a
        # range that isn't "all time", a committed filter query. Range narrows every
        # view so it's always offered; "f" only filters session/project lists, so it
        # appears only where it does something.
        parts.append(("R range", self.range_label() != "all time"))
        if self.can_filter_current_view():
            parts.append(("f,/ filter", bool(self.query)))
        # s sort / e export / o open live in the help overlay -- the footer keeps
        # only navigation, toggles with visible state, and the overlay openers.
        parts += [
            ("T trends", self.trends),
            ("P prices", self.show_prices),
        ]
        if self.can_launch_current():
            parts.append(("L launch", self.launch_menu is not None))
        if self.source_key:
            parts.append(("D real" if self.store.demo else "D demo", False))
        if not self.store.demo:
            parts.append(("$ what-if", self.show_api_prices))
        parts += [("? help", self.help), ("q quit", False)]
        self.hline(stdscr, height - 2, 0, width)
        # Version in the bottom-right corner, lazygit-style: a quiet chrome label.
        # Reserve its slot so the key strip truncates before it instead of colliding;
        # paint it last so it always wins those right-edge cells.
        ver = f" v{__version__} "
        if len(ver) + 4 < width:
            self.draw_keybar(stdscr, height - 1, width - len(ver), parts)
            self.write(stdscr, height - 1, width - len(ver), ver, curses.color_pair(1))
        else:
            self.draw_keybar(stdscr, height - 1, width, parts)

    def draw_keybar(self, stdscr: curses.window, y: int, width: int, parts) -> None:
        # Render the footer key strip segment by segment so active toggles can stand
        # out in the orange accent (pair 6) against the slate baseline (pair 4),
        # instead of one flat-coloured joined string. An entry may itself be a list
        # of (text, on) sub-segments painted contiguously (no separator), so one
        # token inside a hint -- the focused panel in "Tab yr/mo/day" -- can light up.
        base = curses.color_pair(4)
        active = curses.color_pair(6) | curses.A_BOLD
        x = 0
        self.write(stdscr, y, x, " ", base)
        x += 1
        for i, part in enumerate(parts):
            segs = part if isinstance(part, list) else [part]
            # Stop before a hint (plus its leading separator) that won't fully fit -- a
            # clean gap at the right edge, and ahead of the version label, instead of a
            # clipped half-word.
            if x + (2 if i else 0) + sum(len(t) for t, _ in segs) > width - 1:
                break
            if i:
                self.write(stdscr, y, x, "  ", base)
                x += 2
            for text, on in segs:
                self.write(stdscr, y, x, text, active if on else base)
                x += len(text)

    def sort_heading(self, key: str, label: str) -> str:
        if self.effective_sort_by() != key:
            return label
        desc = self.sort_descending(key, self.sort_reverse)
        return f"{label} {'v' if desc else '^'}"

    def project_sort_heading(self, key: str, label: str) -> str:
        if self.effective_sort_by() != key:
            return label
        desc = self.sort_descending(key, self.project_sort_reverse)
        return f"{label} {'v' if desc else '^'}"

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
            "VS Code": "vs",
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

    def bookmark_tag(self, workflow: Workflow) -> str:
        # "★ " before the title of a session starred with `b`, in every list that
        # shows session titles, so bookmarks are spottable wherever they surface.
        return "★ " if workflow.id in self.bookmarks else ""

    def ignored_session_tag(self, workflow: Workflow) -> str:
        return "ignored: " if workflow.id in self.ignored_sessions else ""

    def session_project(self, workflow: Workflow) -> str:
        # A session's project as its root directory's last path segment -- compact
        # enough for a fixed column (worktrees already fold into their parent repo).
        root = self.project_root(workflow.directory)
        return root.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or root

    def sessions_span_projects(self) -> bool:
        # Whether the sessions picker can mix projects: true in time mode without a
        # Projects-tab drill-in. Gates the Project column, which would otherwise
        # repeat the one project the view is already scoped to.
        return self.browse_mode != "projects" and not self.zoom_project

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

    def line_attr(self, line: str) -> int:
        # Shared prefix styling for the text panes: "# " titles (accent), "! "
        # caveats (amber -- attention without alarm; red is for errors and the
        # error toast only), "· " explainer captions (dim).
        if line.startswith("# "):
            return curses.color_pair(4) | curses.A_BOLD
        if line.startswith("! "):
            return curses.color_pair(2)
        if line.startswith("· "):
            return curses.color_pair(1)
        return curses.A_NORMAL

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
        rendered = pad(shorten(text, width), width)
        self.write(stdscr, y, x, rendered, curses.A_NORMAL)
        cost_pos = rendered.find(cost)
        if cost_pos >= 0:
            self.write(
                stdscr, y, x + display_width(rendered[:cost_pos]), cost, self.money_attr(cost)
            )
        token_pos = rendered.find(token_text)
        if token_pos >= 0:
            self.write(
                stdscr,
                y,
                x + display_width(rendered[:token_pos]),
                token_text,
                self.token_attr(token_text),
            )

    def draw_sessions_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        # Navigable session list on the Sessions tab of a zoomed month/day/project.
        sessions = self.current_sessions()
        cy = y + 3
        date_label = self.session_date_label()
        # The Project column only where the list can mix projects; sized to the
        # longest name on show (the _model_table pattern), capped so titles keep room.
        proj_w = 0
        if self.sessions_span_projects():
            proj_head = self.sort_heading("project", "Project")
            longest = max((display_width(self.session_project(wf)) for wf in sessions), default=0)
            proj_w = max(len(proj_head), min(20, longest))
        header = (
            f"  {self.sort_heading('date', date_label):<10} "
            f"{self.sort_heading('cost', 'Cost'):>9} "
            f"{self.sort_heading('tokens', 'Tokens'):>8} "
            f"{self.sort_heading('subagents', 'Subs'):>6}  "
        )
        if proj_w:
            header += f"{self.sort_heading('project', 'Project'):<{proj_w}}  "
        header += self.sort_heading("title", "Title")
        self.write(
            stdscr,
            cy,
            x + 2,
            shorten(header, w - 4),
            curses.color_pair(4) | curses.A_BOLD,
        )
        sort_columns = [("date", date_label), *self.SESSION_SORT_COLUMNS]
        if proj_w:
            sort_columns.insert(-1, ("project", "Project"))  # between Subs and Title
        self._register_sort_header(
            cy,
            x + 2,
            header,
            sort_columns,
            "session",
            w - 4,
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
            text = f"{marker} {started:<10} {cost:>9} {tok:>8} {wf.subagents:>6}  "
            if proj_w:
                text += f"{pad(shorten(self.session_project(wf), proj_w), proj_w)}  "
            text += f"{self.source_tag(wf)}{self.bookmark_tag(wf)}{self.ignored_session_tag(wf)}{wf.title}"
            if start + off == idx:
                self.write(
                    stdscr,
                    ry,
                    x + 2,
                    pad(shorten(text, w - 4), w - 4),
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
        self._register_sort_header(cy, x + 2, header, self.PROJECT_SORT_COLUMNS, "project", w - 4)
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
                    pad(shorten(text, w - 4), w - 4),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            else:
                self.write_colored_summary_row(stdscr, ry, x + 2, text, cost, tok, w - 4)
        hint = "Enter: open sessions"
        self.write(stdscr, y + 1, x + w - len(hint) - 2, hint, curses.color_pair(1))

    def draw_sources_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        # Navigable source list on the Sources tab of a zoomed scope (merged view):
        # j/k pick a tool, Enter its sessions within this scope — the Trends
        # Sources drill, zoom-scoped.
        rows = self.zoom_source_rows()
        cy = y + 3
        if not rows:
            self.write(stdscr, cy, x + 2, "No sessions in this scope.", curses.color_pair(1))
            return
        total = sum(float(it["cost"]) for _, it in rows)
        peak = max((float(it["cost"]) for _, it in rows), default=0.0) or 1.0
        namew = min(max(len(s) for s, _ in rows), max(10, w - 48))
        barw = max(3, min(20, w - namew - 44))
        header = f"  {'Source':<{namew}}  {'':{barw}} {'Cost':>11} {'Share':>5} {'Tokens':>9} {'Sess':>7}"
        self.write(stdscr, cy, x + 2, shorten(header, w - 4), curses.color_pair(4) | curses.A_BOLD)
        visible = max(1, h - 5)
        idx = max(0, min(self.source_index, len(rows) - 1))
        start = max(0, min(idx - visible // 2, max(0, len(rows) - visible)))
        shown = rows[start : start + visible]
        self._add_rows_region("zoomsource", cy + 1, x, x + w - 1, start, len(shown))
        for off, (source, it) in enumerate(shown):
            ry = cy + 1 + off
            marker = ">" if start + off == idx else " "
            cost = money(float(it["cost"]))
            tok = human_tokens(int(it["tokens"]))
            bar = "█" * max(0, round((float(it["cost"]) / peak) * barw))
            text = (
                f"{marker} {shorten(source, namew):{namew}}  {bar:<{barw}} "
                f"{cost:>11} {pct(float(it['cost']), total):>5} {tok:>9} {int(it['sessions']):>7}"
            )
            if start + off == idx:
                row = pad(shorten(text, w - 4), w - 4)
                self.write(stdscr, ry, x + 2, row, curses.A_REVERSE | curses.A_BOLD)
                self.write_selected_bars(stdscr, ry, x + 2, row)
            else:
                self.write_colored_summary_row(stdscr, ry, x + 2, text, cost, tok, w - 4)
        if not self.show_api_prices and any(
            float(it["cost"]) == 0 and int(it["tokens"]) for _, it in rows
        ):
            caption = "· $ prices subscription/credit usage at API list rates"
            if cy + 2 + len(shown) < y + h - 1:
                self.write(
                    stdscr,
                    cy + 2 + len(shown),
                    x + 2,
                    shorten(caption, w - 4),
                    curses.color_pair(1),
                )
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
                    pad(shorten(text, text_w), text_w),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            elif selected:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    pad(shorten(text, text_w), text_w),
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
                    pad(shorten(text, text_w), text_w),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            elif selected:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    pad(shorten(text, text_w), text_w),
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

    def draw_project_list(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        self.box(stdscr, y, x, h, w, "Projects" + (" ▸" if active else ""), active=active)
        rows = self.projects
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No projects in range.", curses.color_pair(1))
            return

        header = self.project_header_text(w - 2)
        self.write(
            stdscr, y + 1, x + 1, shorten(header, w - 2), curses.color_pair(4) | curses.A_BOLD
        )
        self._register_sort_header(
            y + 1, x + 1, header, self.PROJECT_SORT_COLUMNS, "project", w - 2
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
            if selected and active:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    pad(shorten(text, w - 2), w - 2),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            elif selected:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    pad(shorten(text, w - 2), w - 2),
                    curses.color_pair(1) | curses.A_BOLD,
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
        if current == "Sources" and self.view == "zoom":
            self.draw_sources_picker(stdscr, y, x, h, w)
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
            self.write_rich(
                stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), self.line_attr(line)
            )

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
        if current == "Sources" and self.view == "zoom":
            self.draw_sources_picker(stdscr, y, x, h, w)
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
            self.write_rich(
                stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), self.line_attr(line)
            )

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
        if current == "Sources" and self.view == "zoom":
            self.draw_sources_picker(stdscr, y, x, h, w)
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
            self.write_rich(
                stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), self.line_attr(line)
            )

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
                    pad(shorten(text, text_w), text_w),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            elif selected:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    pad(shorten(text, text_w), text_w),
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
        if current == "Sources" and self.view == "zoom":
            self.draw_sources_picker(stdscr, y, x, h, w)
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
            self.write_rich(
                stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), self.line_attr(line)
            )

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
                f"{shorten(self.bookmark_tag(workflow) + workflow.title, max(12, w - 25))}"
            )
            if selected:
                self.write(
                    stdscr,
                    row_y,
                    x + 1,
                    pad(shorten(text, w - 2), w - 2),
                    curses.A_REVERSE | curses.A_BOLD,
                )
            else:
                self.write_colored_summary_row(stdscr, row_y, x + 1, text, cost, tok, w - 2)

    def draw_detail(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        workflow = self.current_session()
        title = (
            "Detail"
            if workflow is None
            else shorten(self.bookmark_tag(workflow) + workflow.title, max(10, w - 12))
        )
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
        drawn = lines[self.scroll : self.scroll + visible]
        for offset, line in enumerate(drawn):
            attr = self.line_attr(line)
            if line.startswith(("▸ ", "▾ ")):  # Turns tab: a user-prompt group header
                attr = curses.color_pair(6) | curses.A_BOLD
            elif line.startswith("  │"):  # Turns tab: an unfolded prompt's full text
                attr = curses.color_pair(1)
            self.write_rich(stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), attr)
        if current == "Turns":
            # Make the ▸/▾ headers clickable: the region maps a row back to its line
            # index; _apply_click resolves headers via _turn_header_at.
            self._add_rows_region("turnline", y + 3, x + 2, x + w - 3, self.scroll, len(drawn))

    def _model_table(
        self,
        rows: list[tuple],
        title: str,
        width: int,
        name_label: str = "Model",
        count_label: str = "Msgs",
        price_split: bool = True,
    ) -> list[str]:
        # rows: (name, count, cost, tokens, cache_read, cache_write, output). The
        # name column fits the longest entry (so the numbers sit right after it),
        # capped by the available width so long names aren't cut when there's room.
        # name_label/count_label let the Tools tab reuse this as Tool/Calls (which
        # also turns price_split off -- tool names don't resolve to model rates).
        cw_ = max(4, len(count_label))
        longest = max([len(str(r[0])) for r in rows] + [len(name_label)])
        # In wide panes the CacheR/CacheW/Output cells carry the tokens' attributed
        # share of the Cost column too -- "811.6k($10)" -- because counts alone hide
        # how skewed the money is (cache writes bill at 12.5x the cache-read rate on
        # current Anthropic models). Costs 16 more columns than the plain layout, so
        # it only kicks in when the name column still gets its 20-char floor; with
        # no dollars anywhere ($0.00 unpriced rows) there is nothing to attribute.
        split = price_split and any(float(r[2]) > 0 for r in rows) and width - 73 - cw_ >= 20
        block = 73 if split else 57
        mw = min(longest, max(20, width - block - cw_))
        total_cost = sum(float(r[2]) for r in rows)
        if split:
            # Each split cell is two fixed sub-columns -- tokens right-aligned in 6,
            # dollars right-aligned in 6 inside the parens -- so the numbers line up
            # row to row and the label sits exactly over the token half.
            tail_head = f"{'CacheR':>6}{'':8} {'CacheW':>6}{'':8} {'Output':>6}{'':8}"
        else:
            tail_head = f"{'CacheR':>9} {'CacheW':>9} {'Output':>8}"
        lines = [
            title,
            f"{name_label:{mw}} {count_label:>{cw_}} {'Cost':>10} {'Share':>5} {'Tokens':>9} {tail_head}",
        ]
        for name, runs, cost, tok, cr, cw, out in rows:
            if split:
                c1, c2, c3 = self._price_split_cells(
                    str(name), float(cost), int(tok), int(cr), int(cw), int(out)
                )
                tail = f"{c1} {c2} {c3}"
            else:
                tail = f"{human_tokens(int(cr)):>9} {human_tokens(int(cw)):>9} {human_tokens(int(out)):>8}"
            lines.append(
                f"{pad(shorten(name, mw), mw)} {int(runs):>{cw_}} {money(float(cost)):>10} "
                f"{pct(float(cost), total_cost):>5} "
                f"{human_tokens(int(tok)):>9} {tail}"
            )
        if any(str(name).startswith("unknown") for name, *_ in rows):
            lines.extend(
                [
                    "",
                    "! unknown (not recorded) means provider/model metadata was not stored for these rows.",
                ]
            )
        return lines

    @staticmethod
    def _price_split_cells(
        name: str, cost: float, tok: int, cr: int, cw: int, out: int
    ) -> tuple[str, str, str]:
        # "tokens(dollars)" cells: each category's attributed share of the row's
        # Cost. The split weighs tokens by the same list rates api_equivalent_cost
        # bills them at, then scales so the three cells plus the implicit input
        # remainder sum to the Cost column -- exact for $-estimated rows (same
        # math), honest attribution for recorded costs that predate today's rates.
        # A row with no dollars, or a model with no rates at all, stays bare.
        # Every cell is 14 wide with fixed sub-columns -- tokens right-aligned in
        # 6, then the whole "($13)" group right-aligned in 8 so the parens hug the
        # amount (no inner gap) while the amounts stay flush right row to row.
        ir, orr, crr, cwr = model_price(name)
        inp = max(0, tok - cr - cw - out)
        raw = (inp * ir, cr * crr, cw * cwr, out * orr)
        total = sum(raw)
        scale = cost / total if cost > 0 and total > 0 else 0.0
        cells = []
        for tokens_n, share in ((cr, raw[1]), (cw, raw[2]), (out, raw[3])):
            dollars = share * scale
            label = f"({money_label(dollars)})" if dollars > 0 else ""
            cells.append(f"{human_tokens(tokens_n):>6}{label:>8}")
        return (cells[0], cells[1], cells[2])

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
                f"{shorten(self.source_tag(workflow) + self.bookmark_tag(workflow) + workflow.title, max(20, width - 37))}"
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
                f"{self.src_col(workflow)}{self.bookmark_tag(workflow)}{workflow.title}"
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
                f"{shorten(self.source_tag(workflow) + self.bookmark_tag(workflow) + workflow.title, max(20, width - 37))}"
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
                f"{self.src_col(workflow)}{self.bookmark_tag(workflow)}{workflow.title}"
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
                f"{shorten(self.source_tag(workflow) + self.bookmark_tag(workflow) + workflow.title, max(20, width - 37))}"
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
                f"{self.src_col(workflow)}{self.bookmark_tag(workflow)}{workflow.title}"
            )
        return lines

    def project_overview(self, project: ProjectSummary, width: int) -> list[str]:
        include_ignored = self.include_ignored_for_project(project)
        workflows = self.workflows_for_project(project.directory, include_ignored=include_ignored)
        share_total = (
            sum(w.total_cost for w in self.ranged_workflows)
            if include_ignored
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
                f"{shorten(self.source_tag(workflow) + self.bookmark_tag(workflow) + workflow.title, max(20, width - 50))}"
            )
        return lines

    def project_models(self, project: ProjectSummary, width: int) -> list[str]:
        agg = self.aggregate_models(
            self.workflows_for_project(
                project.directory,
                include_ignored=self.include_ignored_for_project(project),
            )
        )
        return self._models_tab(self._agg_rows(agg), "# Project Model Spend", width)

    def project_sources(self, project: ProjectSummary, width: int) -> list[str]:
        return self.source_table(
            self.workflows_for_project(
                project.directory,
                include_ignored=self.include_ignored_for_project(project),
            ),
            width,
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
        workflows = self.workflows_for_project(
            project.directory,
            include_ignored=self.include_ignored_for_project(project),
        )
        for workflow in self.filtered_sessions(workflows):
            lines.append(
                f"{workflow.created_at[:10]:<10} "
                f"{money(workflow.total_cost):>9} "
                f"{human_tokens(workflow.total_tokens):>8} "
                f"{workflow.subagents:>4} "
                f"{workflow.model_count:>6}  "
                f"{self.src_col(workflow)}{self.bookmark_tag(workflow)}{workflow.title}"
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
            [row for row in self.session_node_rows(workflow.id) if row["depth"] > 0]
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
                f"{pad(shorten(row['agent'], 14), 14)} "
                f"{pad(shorten(row['model_name'], 31), 31)} "
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
            table_rows(by_tool), "# Tools — this session", width, "Tool", "Calls", price_split=False
        )
        lines.append("")
        lines.extend(
            self._model_table(
                table_rows(by_server),
                "# By server / namespace",
                width,
                "Server",
                "Calls",
                price_split=False,
            )
        )
        lines += [
            "",
            "· Tokens/cost are for the LLM turns that invoked each tool (split evenly across",
            "· a turn's tools), not the tool's own output size.",
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
                "This session's source records no per-turn usage.",
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
        self._turn_header_at = {}  # line index -> prompt_id, for the click toggle
        for n, (r, cost) in enumerate(zip(rows, costs), start=1):
            pid = r.get("prompt_id", "")
            if pid != last_pid:
                last_pid = pid
                gc = money(subtotal[pid])
                opened = self.turns_full or pid in self._turns_expanded
                title = (r.get("prompt_title") or "").strip() or "(no preceding prompt)"
                title = shorten(title, max(10, width - len(gc) - 5))
                head = ("▾ " if opened else "▸ ") + title
                self._turn_header_at[len(lines)] = pid
                lines.append(head + " " * max(1, width - display_width(head) - len(gc)) + gc)
                if opened:
                    # The whole prompt, its own line breaks kept, wrapped to the pane.
                    full = (r.get("prompt_full") or r.get("prompt_title") or "").strip()
                    for para in full.splitlines() or [""]:
                        for piece in textwrap.wrap(para, max(20, width - 4)) or [""]:
                            lines.append("  │ " + piece)
            cum += cost
            agent = r["agent"] if r["depth"] else "-"
            cumlabel = f"{money(cum)} · {pct(cum, total)}"
            lines.append(
                f"  {n:>{idx_w}} {clock(r['time']):<{time_w}} {pad(shorten(r['model_name'], mw), mw)} "
                f"{pad(shorten(agent, agent_w), agent_w)} "
                f"{human_tokens(r['tokens_total']):>9} {money(cost):>9} {cumlabel:>16}"
            )
        lines += [
            "",
            "· Grouped by the user prompt (▸) that triggered each run — time order, not cost.",
            "· z (or a click on a ▸ header) unfolds the whole prompt.",
        ]
        return lines

    # The help overlay's keymap, grouped into sections. Each row is
    # (key, one-line summary[, note, note, ...]); notes render as dim sub-lines under
    # the description. draw_help wraps and colours these; kept as data so the content
    # is one flat list to edit and is unit-testable without a screen.
    def help_sections(self) -> list[tuple[str, list[tuple]]]:
        return [
            (
                "Move around",
                [
                    ("p / t", "switch to the Projects / Time browse mode"),
                    ("Tab", "cycle focus Years → Months → Days (Time mode)"),
                    ("Shift-Tab", "cycle focus backward; at the top level, step back out"),
                    (
                        "Enter / +",
                        "drill into the selected year / month / day / project; on a "
                        "Sessions, Projects or Sources tab, open that session / "
                        "project's / source's sessions in this scope",
                        "the detail opens beside the sidebar, which stays clickable "
                        "to re-scope; + maximizes / restores it (remembered)",
                    ),
                    ("Esc", "step back out — session → zoom → browse"),
                    (
                        "h / l",
                        "switch detail tabs",
                        "years/months: Overview · Models · Projects · Sessions; days drop Models",
                        "a session adds Turns (per-turn cost over time; every source that "
                        "records per-step usage) and Tools (per-tool / MCP spend, OpenCode); "
                        "Sources joins in the merged 'all' view",
                        "on Turns, z (or clicking a ▸ header) unfolds the whole prompt text",
                    ),
                    ("j / k", "move in the list (↑/↓ too), or scroll the detail pane"),
                    ("PgDn/PgUp", "move / scroll by half a page (Ctrl-D / Ctrl-U too)"),
                    ("g / G", "jump to the top / bottom"),
                    (
                        "mouse",
                        "wheel scrolls · click selects (anywhere in the preview pane "
                        "focuses it) · double-click drills · click a tab "
                        "or a column header to sort by it (again to reverse)",
                    ),
                ],
            ),
            (
                "Scope & filter",
                [
                    (
                        "R",
                        "set the range — all · 30d (or 30) · 2m · 1y · 2026 · 2026-05 · start..end",
                    ),
                    ("a", "show all time, keeping the current selection where possible"),
                    ("s", "open the sort picker for the visible list (j/k move · Enter · Esc)"),
                    (
                        "f or /",
                        "live filter — fuzzy over sessions (title/project/id), projects and "
                        "Models; substring over Prices",
                        "while filtering: ↑/↓ select · Enter keep · Esc cancel · Ctrl-U clear",
                    ),
                    ("x", "clear the filter"),
                ],
            ),
            (
                "Sessions & projects",
                [
                    (
                        "i / I",
                        "ignore / unignore the selection; I reveals hidden rows so they can "
                        "be unignored",
                    ),
                    (
                        "b / B",
                        "bookmark ★ the selected session (remembered between runs); B shows "
                        "only bookmarks, within the active range",
                    ),
                    ("o", "open the selected session's / project's directory"),
                    (
                        "L",
                        "launch the session in its tool — opencode --session / claude "
                        "--resume / codex resume",
                        "w window · s split · v vsplit · p popup · y copy command",
                        "w/s/v/p need tmux (or a launcher hook); y copies anywhere",
                    ),
                    ("e", "export the current list to a CSV in the working directory"),
                ],
            ),
            (
                "Views & overlays",
                [
                    (
                        "T",
                        "Trends — Daily · Weekly · Monthly · Calendar · Models · Providers · Sources",
                        "h/l tabs · j/k page months / weeks / years · $ what-if prices",
                        "charts (and the Calendar heat map): Enter focuses, ↑↓←→ pick a "
                        "bar / day, Enter drills into it, Esc back; +/- calendar shades",
                        "Models/Providers/Sources: j/k pick a row · Enter its sessions "
                        "· Enter again opens one · Esc backs out",
                        "C theme · c source · D demo · ? help keep working inside",
                    ),
                    (
                        "P",
                        "model prices — eff $/M blends each model's list rates at your token "
                        "mix (cheapest first), beside your usage share and raw rates",
                        "p cycles the layout — flat / by vendor / by provider",
                        "j/k select · Enter its sessions · s sort a column (or click a header) · f/r/e as usual",
                        "C theme · c source · D demo · $ what-if · ? help keep working inside",
                    ),
                    ("$", "toggle what-if prices — what unpriced usage would cost at API list"),
                    (
                        "c",
                        "data-source picker (j/k move · Enter switch · Esc cancel) — OpenCode "
                        "/ Claude / Codex / Copilot / pi / OpenClaw / all when present",
                    ),
                    (
                        "C",
                        "colour-theme picker — j/k live-preview · Enter keep · Esc revert "
                        "(also the web browser's)",
                    ),
                    ("D", "toggle real / demo data (demo anonymizes titles and paths)"),
                ],
            ),
            (
                "Reload & quit",
                [
                    ("r", "reload the database"),
                    ("q", "quit"),
                ],
            ),
        ]

    _HELP_CAVEAT = (
        "Cost is each tool's own local attribution. Subscription or credit plans "
        "(Claude Code, Codex, Copilot) aren't priced per token, so their usage shows as "
        "unpriced $0.00 — check your provider for the real total.",
        "Press $ for the what-if view: that usage priced at models.dev API list — an "
        "estimate of what you'd have paid without the subscription.",
        "Sub-cent costs show as <$0.01; a red $0.00 means unpriced (no local price). "
        "Range, sort and the $ view persist between runs (unless --no-state). Git "
        "worktrees fold into their main repo (--no-worktrees to keep them split).",
    )

    def draw_help(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        self.box(stdscr, y, 0, bottom - y, width, "Help · j/k scroll · any other key closes")
        key_x = 2
        key_w = 11
        desc_x = key_x + key_w + 1
        desc_w = max(12, width - desc_x - 2)
        rule_end = width - 3

        head = curses.color_pair(6) | curses.A_BOLD
        rule = curses.color_pair(4)
        key_attr = curses.color_pair(2) | curses.A_BOLD
        dim = curses.color_pair(1)

        # Flatten the sections into per-line segment lists ([(x, text, attr), …]); a
        # blank line is []. Scrolling then just slices this list.
        render: list[list[tuple[int, str, int]]] = []

        def header(title: str) -> None:
            # Centered section title with a rule filling both sides.
            span = rule_end - key_x
            label = f" {title} "
            left = (span - len(label)) // 2
            if left < 1:  # a panel too narrow to center: plain left-aligned title
                render.append([(key_x, title, head)])
                return
            render.append(
                [
                    (key_x, "─" * left, rule),
                    (key_x + left, label, head),
                    (key_x + left + len(label), "─" * max(1, span - len(label) - left), rule),
                ]
            )

        def dim_wrapped(text: str) -> None:
            for piece in textwrap.wrap(text, desc_w) or [""]:
                render.append([(desc_x, piece, dim)])

        render.append(
            [(key_x, "OpenTab — browse AI-coding spend by month / day / project / session.", 0)]
        )
        render.append([])
        for title, rows in self.help_sections():
            header(title)
            for row in rows:
                key, desc, notes = row[0], row[1], row[2:]
                wrapped = textwrap.wrap(desc, desc_w) or [""]
                render.append([(key_x, key, key_attr), (desc_x, wrapped[0], 0)])
                for cont in wrapped[1:]:
                    render.append([(desc_x, cont, dim)])
                for note in notes:
                    dim_wrapped("· " + note)
            render.append([])
        header("Reading the numbers")
        for para in self._HELP_CAVEAT:
            for piece in textwrap.wrap(para, width - key_x - 3) or [""]:
                render.append([(key_x, piece, dim)])

        visible = max(1, bottom - y - 3)
        scroll = max(0, min(self.app.help_scroll, max(0, len(render) - visible)))
        self.app.help_scroll = scroll
        for offset, segments in enumerate(render[scroll : scroll + visible]):
            row_y = y + 2 + offset
            for sx, text, attr in segments:
                self.write(stdscr, row_y, sx, text, attr)

    def price_intro_lines(self) -> list[str]:
        # The fixed header block above the P overlay's price table: where the rates
        # came from and what the eff blend means. Pulled out so both the flat
        # price_table_lines (export/tests) and the navigable draw_prices share one
        # source of truth.
        meta = price_cache_meta()
        if meta:
            when = (meta.get("fetched_at") or "?")[:10]
            source = f"models.dev cache · {meta.get('count', 0)} models · fetched {when}"
        else:
            source = "embedded offline snapshot (anthropic/openai/google)"
        lines = [f"Source: {source}.  API list prices per 1M tokens; r refreshes from models.dev."]
        mix = self.app.price_token_mix()
        if mix:
            (inp, out, cr, cw), total = mix
            lines.append(
                f"eff $/M prices each model's list rates at YOUR token mix: {inp:.1%} input · {out:.1%} output · {cr:.1%} cacheR · {cw:.1%} cacheW ({human_tokens(total)} tokens)."
            )
            lines.append(
                "~ = no cache-read rate on record; those reads are billed at the input rate (an upper bound)."
            )
        lines.append("")
        return lines

    # Price columns are 8 wide (not 7) so the active-sort header can carry a " v"/
    # " ^" arrow -- "output v"/"cacheR v" need the eighth cell -- and still line up
    # with the numeric rows below. The eff column is 9 so "eff $/M ^" fits; the use
    # column is a 5-cell share bar + a 4-wide percentage.
    _PRICE_COL_W = 8
    _PRICE_EFF_W = 9
    _PRICE_USE_BAR = 5
    _PRICE_USE_W = _PRICE_USE_BAR + 4
    # name gap -> eff, gap, use, gap, four raw columns + three single-space gaps
    _PRICE_BLOCK_W = _PRICE_EFF_W + 2 + _PRICE_USE_W + 2 + _PRICE_COL_W * 4 + 3

    # A few access routes are long; abbreviate the worst offenders for the route tag.
    _ROUTE_ABBR = {"github-copilot": "copilot"}

    def _route_tag(self, routes) -> str:
        # The trailing "how you reach this model" annotation, e.g. "anthropic·copilot".
        # A slashed gateway route ("openrouter/anthropic") collapses to the gateway --
        # the vendor half is already the row's family -- deduped after collapsing.
        seen: list[str] = []
        for r in routes:
            tag = self._ROUTE_ABBR.get(r, r.split("/", 1)[0])
            if tag not in seen:
                seen.append(tag)
        return "·".join(seen)

    def _price_eff_cell(self, entry) -> str:
        # The blended eff $/M figure; ~ marks the missing-cache-read upper bound.
        return f"~{entry.eff:.2f}" if entry.approx else f"{entry.eff:.2f}"

    def _price_use_cell(self, entry, peak: float) -> str:
        # Your usage share of this model: a bar scaled to the biggest row + percent.
        bar = cost_bar(entry.share, peak, self._PRICE_USE_BAR)
        return f"{bar}{entry.share:>4.0%}"

    def _price_raw_cells(self, entry) -> list[str]:
        # The four raw list-price cells. A 0 cache-read rate is missing data, never
        # a free lunch, so it renders as "—" (the eff blend bills it at the input
        # rate); a 0 cache-write is genuine (OpenAI/Gemini don't charge writes).
        ir, orr, crr, cwr = entry.price
        cr = "—" if crr <= 0 < ir else f"{crr:.2f}"
        return [f"{ir:.2f}", f"{orr:.2f}", cr, f"{cwr:.2f}"]

    def _price_core_text(self, entry, namew: int, peak: float) -> str:
        # One model's name + eff/use/raw-price cells (no route tag -- that's overlaid
        # dim, and appended by the text path). Every entry carries its resolved price
        # (from the most completely-priced alias; local models are dropped upstream).
        w = self._PRICE_COL_W
        cells = " ".join(f"{c:>{w}}" for c in self._price_raw_cells(entry))
        return (
            f"{pad(shorten(entry.bare, namew), namew)}  "
            f"{self._price_eff_cell(entry):>{self._PRICE_EFF_W}}  "
            f"{self._price_use_cell(entry, peak):<{self._PRICE_USE_W}}  {cells}"
        )

    def _price_col_head(self, key: str, label: str, width: int, left: bool = False) -> str:
        # One `width`-wide header cell, with a v/^ arrow appended when this is the
        # active sort column (direction from prices_sort_reverse).
        if self.app.prices_sort == key:
            desc = self.sort_descending(key, self.app.prices_sort_reverse)
            label = f"{label} {'v' if desc else '^'}"
        return f"{label:<{width}}" if left else f"{label:>{width}}"

    def _price_header(self, namew: int) -> str:
        # The price table's column header, shared by the flat price_table_lines and
        # the navigable draw_prices so both show the same sort arrows. model is
        # left-aligned in the name column; every other cell aligns with the rows.
        model = "model"
        if self.app.prices_sort == "model":
            desc = self.sort_descending("model", self.app.prices_sort_reverse)
            model = f"model {'v' if desc else '^'}"
        eff = self._price_col_head("eff", "eff $/M", self._PRICE_EFF_W)
        use = self._price_col_head("use", "use", self._PRICE_USE_W, left=True)
        cells = " ".join(
            self._price_col_head(key, label, self._PRICE_COL_W)
            for key, label in self.PRICE_SORT_COLUMNS[3:]
        )
        return f"{model:{namew}}  {eff}  {use}  {cells}"

    def _price_namew(self, entries, width: int) -> int:
        return min(max(len(e.bare) for e in entries), max(12, width - self._PRICE_BLOCK_W - 3))

    @staticmethod
    def _price_use_peak(entries) -> float:
        # The biggest usage share among the rows -- what the use bars scale against.
        return max((e.share for e in entries), default=0.0)

    def _price_column_ranges(self, entries) -> list[tuple[float, float] | None]:
        # For the eff column and each of the four price columns, the (min, max) over
        # the *positive* values among `entries` -- the span the green→red heat
        # normalizes against. Zero cells are excluded, so a column of {0, 0, 5.0}
        # still spans by its paying member; a column with fewer than two distinct
        # positive rates is degenerate (None) and stays neutral.
        cols: list[list[float]] = [[e.eff for e in entries if e.eff > 0], [], [], [], []]
        for entry in entries:
            for i, value in enumerate(entry.price):
                if value > 0:
                    cols[i + 1].append(value)
        ranges: list[tuple[float, float] | None] = []
        for vals in cols:
            lo, hi = (min(vals), max(vals)) if vals else (0.0, 0.0)
            ranges.append((lo, hi) if hi > lo else None)
        return ranges

    def _price_heat_level(self, value: float, rng: tuple[float, float] | None) -> int | None:
        # The 0..PRICE_HEAT_LEVELS-1 heat bucket for one price cell, by its
        # *logarithmic* position in the column's [min, max] -- list prices span orders
        # of magnitude, so a linear ramp would flatten the low end (same reasoning as
        # heat_level). None means neutral: a degenerate column or a non-positive rate,
        # which must never read as falsely hot. Pure (no curses) so it's unit-testable.
        if rng is None:
            return None
        lo, hi = rng
        if value <= lo:
            return 0
        frac = (math.log(value) - math.log(lo)) / (math.log(hi) - math.log(lo))
        return max(0, min(PRICE_HEAT_LEVELS - 1, round(frac * (PRICE_HEAT_LEVELS - 1))))

    def _price_heat_attr(self, value: float, rng: tuple[float, float] | None) -> int:
        # The green(cheap)→red(pricy) curses attribute for one price cell.
        level = self._price_heat_level(value, rng)
        if level is None:
            return curses.A_NORMAL
        return curses.color_pair(PRICE_HEAT_BASE_PAIR + level) | curses.A_BOLD

    def _price_group_label(self, group: str) -> str:
        # The header label for a group in the active view: the vendor name in the
        # "family" view, the route (or "(direct)" for a route-less id) in "provider".
        if self.app.prices_view == "family":
            return family_label(group)
        return group or "(direct)"

    def _price_entry_tag(self, entry) -> str:
        # The trailing annotation per row: in the "provider" view the group is already
        # the route, so show the vendor family instead; otherwise show the route(s).
        if self.app.prices_view == "provider":
            return family_label(entry.family)
        return self._route_tag(entry.routes)

    def _price_render_rows(self, entries) -> list[tuple]:
        # Flatten the ordered entries into drawable rows: ("header", label) before each
        # new group (unless the view is flat), then ("model", entry_index, entry) for
        # each model. The entry_index is the position in `entries`, so the cursor
        # (prices_index) and this list stay in lock-step.
        rows: list[tuple] = []
        grouped = self.app.prices_view != "flat"
        for i, entry in enumerate(entries):
            if grouped and (i == 0 or entry.group != entries[i - 1].group):
                rows.append(("header", self._price_group_label(entry.group)))
            rows.append(("model", i, entry))
        return rows

    def _price_empty_msg(self) -> str:
        return (
            f"No model prices match the filter: {self.query}"
            if self.query
            else "No model usage on record yet."
        )

    def price_table_lines(self, width: int) -> list[str]:
        # The models you have used and the models.dev API list prices OpenTab applies
        # for the "$" what-if estimate, laid out by the active view (grouped under
        # ▸ headers unless flat). Pure text so it can be tested without a screen;
        # draw_prices paints the same rows with a cursor + heat colors. The entry set
        # (and the active filter) is shared with the `e` export via priced_model_entries.
        entries = self.priced_model_entries()
        lines = self.price_intro_lines()
        if not entries:
            lines.append(self._price_empty_msg())
            return lines
        namew = self._price_namew(entries, width)
        peak = self._price_use_peak(entries)
        lines.append(self._price_header(namew))
        for row in self._price_render_rows(entries):
            if row[0] == "header":
                lines.append(f"▸ {row[1]}")
            else:
                _, _i, entry = row
                core = self._price_core_text(entry, namew, peak)
                tag = self._price_entry_tag(entry)
                lines.append(f"{core}  {tag}" if tag else core)
        return lines

    def draw_prices(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        # Reference overlay (toggled with P) so the rates behind the "$" what-if
        # number are visible. Laid out by the active view (p cycles by vendor / by
        # provider / flat); j/k moves a cursor over models, Enter drills into sessions.
        if self.app.prices_model is not None:
            self.draw_price_sessions(stdscr, y, bottom, width)
            return
        self.box(
            stdscr,
            y,
            0,
            bottom - y,
            width,
            f"Model prices ({self.prices_view_label()})  ·  j/k select · Enter sessions · s sort · p view · f filter · r refresh · e export · q closes",
            active=True,
        )
        inner_w = width - 4
        intro = self.price_intro_lines()
        top = y + 2
        for offset, line in enumerate(intro):
            attr = curses.color_pair(4) if "models.dev" in line else curses.A_NORMAL
            self.write(stdscr, top + offset, 2, shorten(line, inner_w), attr)
        entries = self.priced_model_entries()
        head_y = top + len(intro)
        if not entries:
            self.write(stdscr, head_y, 2, shorten(self._price_empty_msg(), inner_w))
            return
        namew = self._price_namew(entries, inner_w)
        header = self._price_header(namew)
        self.write(
            stdscr, head_y, 2, shorten(header, inner_w), curses.color_pair(4) | curses.A_BOLD
        )
        # Clicking a column header sorts by it (re-click flips); zones match the
        # drawn text, arrows included, via the base labels in PRICE_SORT_COLUMNS.
        self._register_sort_header(head_y, 2, header, self.PRICE_SORT_COLUMNS, "prices", inner_w)
        list_top = head_y + 1
        visible = max(1, bottom - list_top - 1)
        idx = max(0, min(self.app.prices_index, len(entries) - 1))
        self.app.prices_index = idx
        render = self._price_render_rows(entries)
        # Scroll over the flattened rows (headers included) while keeping the selected
        # model row -- and, when it exists, the group header just above it -- in view.
        sel_row = next(r for r, item in enumerate(render) if item[0] == "model" and item[1] == idx)
        anchor = sel_row - 1 if sel_row > 0 and render[sel_row - 1][0] == "header" else sel_row
        scroll = max(0, min(self.app.prices_scroll, max(0, len(render) - visible)))
        if anchor < scroll:
            scroll = anchor
        elif sel_row >= scroll + visible:
            scroll = sel_row - visible + 1
        self.app.prices_scroll = scroll
        ranges = self._price_column_ranges(entries)
        peak = self._price_use_peak(entries)
        w = self._PRICE_COL_W
        x_eff = 2 + namew + 2
        x_raw = x_eff + self._PRICE_EFF_W + 2 + self._PRICE_USE_W + 2
        tag_x = 2 + namew + 2 + self._PRICE_BLOCK_W + 2  # after the price cells
        for offset, item in enumerate(render[scroll : scroll + visible]):
            row_y = list_top + offset
            if item[0] == "header":
                self.write(
                    stdscr,
                    row_y,
                    2,
                    shorten(f"▸ {item[1]}", inner_w),
                    curses.color_pair(6) | curses.A_BOLD,
                )
                continue
            _, i, entry = item
            core = self._price_core_text(entry, namew, peak)
            selected = i == idx
            attr = curses.A_REVERSE | curses.A_BOLD if selected else curses.A_NORMAL
            core_row = pad(shorten(core, inner_w), inner_w)
            self.write(stdscr, row_y, 2, core_row, attr)
            if selected:
                self.write_selected_bars(stdscr, row_y, 2, core_row)
            tag = self._price_entry_tag(entry)
            if tag and tag_x < 2 + inner_w and not selected:
                self.write(
                    stdscr,
                    row_y,
                    tag_x,
                    shorten(tag, 2 + inner_w - tag_x),
                    curses.color_pair(1) | curses.A_DIM,
                )
            elif tag and tag_x < 2 + inner_w:  # selected row: keep the tag in the reverse bar
                self.write(stdscr, row_y, tag_x, shorten(tag, 2 + inner_w - tag_x), attr)
            if selected:
                continue  # the reverse cursor bar reads clearer without heat
            if x_eff + self._PRICE_EFF_W <= 2 + inner_w:
                self.write(
                    stdscr,
                    row_y,
                    x_eff,
                    f"{self._price_eff_cell(entry):>{self._PRICE_EFF_W}}",
                    self._price_heat_attr(entry.eff, ranges[0]),
                )
            for j, cell in enumerate(self._price_raw_cells(entry)):
                cell_x = x_raw + j * (w + 1)
                if cell_x + w > 2 + inner_w:
                    break  # cell would spill past the shortened row; leave it plain
                self.write(
                    stdscr,
                    row_y,
                    cell_x,
                    f"{cell:>{w}}",
                    self._price_heat_attr(entry.price[j], ranges[j + 1]),
                )

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
            cardw = min(max([len(head)] + [display_width(line) for line in body]) + 1, maxw)
            x = max(0, width - cardw - 2)
            fading = toast.remaining(now) < self.TOAST_FADE
            base = curses.color_pair(pair) | curses.A_REVERSE
            self.write(
                stdscr,
                row,
                x,
                pad(head, cardw),
                base | (curses.A_DIM if fading else curses.A_BOLD),
            )
            for i, line in enumerate(body):
                self.write(
                    stdscr,
                    row + 1 + i,
                    x,
                    pad(line, cardw),
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
        inner_w = max([len(title) + 2] + [display_width(t) for t, _ in content] + [16])
        w = min(inner_w + 4, max(24, scr_w - 4))
        h = min(len(content) + 4, max(6, scr_h - 4))
        y = max(1, (scr_h - h) // 2)
        x = max(1, (scr_w - w) // 2)
        for row in range(y, y + h):  # clear the footprint first
            self.write(stdscr, row, x, " " * w)
        self.box(stdscr, y, x, h, w, title, active=True)
        field = w - 4
        for offset, (text, attr) in enumerate(content[: h - 4]):
            self.write(stdscr, y + 2 + offset, x + 2, pad(shorten(text, field), field), attr)

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

    def draw_theme_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `C` (Colours) picker: a modal list of the themes (shared with the web
        # browser). j/k live-previews each (the whole UI is the swatch), Enter keeps it,
        # Esc reverts to the theme active on open. Colours re-map via init_theme_colors.
        entries = self.theme_menu_entries()
        idx = self.theme_menu_index % len(entries) if entries else 0
        lines = [("Colour theme (also the web browser's):", curses.color_pair(4)), ("", 0)]
        for offset, (_tid, name, is_current) in enumerate(entries):
            marker = "●" if is_current else "○"
            suffix = "  (current)" if is_current else ""
            attr = curses.A_REVERSE | curses.A_BOLD if offset == idx else curses.A_NORMAL
            lines.append((f" {marker}  {name}{suffix}", attr))
        self.draw_modal(
            stdscr, scr_h, scr_w, "Theme · j/k preview · Enter keep · Esc revert", lines
        )

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
        "input": "Input price",
        "output": "Output price",
        "cache_read": "Cache-read price",
        "cache_write": "Cache-write price",
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
        targets = self.launch_targets()
        if self.launch_available():
            via = "launcher hook" if launcher_hook() else "tmux"
            headline = f"open in {via}:"
        else:
            headline = "no tmux / launcher hook — copy instead:"
        idx = self.launch_menu_index % len(targets)
        lines = [
            (shorten(session.title or "(untitled)", 52), curses.color_pair(4)),
            (headline, curses.A_NORMAL),
            ("", 0),
        ]
        for offset, (kc, _kind, label) in enumerate(targets):
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
        self.app._trend_bar_geom = None  # rebuilt below when a bar chart draws
        self._trend_rows_at = None  # rebuilt below when a selectable list draws
        if self.trend_drill is not None:
            hint = "j/k move · Enter opens session · esc back"
        elif current == "Calendar":
            if self.trend_focus:
                hint = "↑↓←→ day · +/- shades · Enter open · esc back"
            else:
                hint = "h/l tabs · Enter pick days · esc closes"
        elif current in ("Daily", "Weekly", "Monthly"):
            if self.trend_focus:
                hint = "↑↓←→ bar · Enter open · esc back"
            else:
                unit = {"Daily": "j/k month · ", "Weekly": "j/k week · "}.get(current, "")
                hint = f"h/l tabs · {unit}Enter pick bars · esc closes"
        else:
            hint = "h/l tabs · j/k rows · Enter sessions · esc closes"
        self.draw_tabs(stdscr, y + 1, 2, width - len(hint) - 4, tabs, self.trend_tab, kind="trend")
        self.write(stdscr, y + 1, width - len(hint) - 2, hint, curses.color_pair(4))
        inner_w = width - 4
        content_h = h - 4
        if self.trend_drill is not None:
            lines = self.trend_drill_lines(inner_w, content_h)
        elif current == "Calendar":
            # The heat map paints itself: its cells carry per-cell color attributes,
            # so it bypasses the generic string -> write_rich path the other tabs use.
            self.draw_calendar(stdscr, y + 3, 2, content_h, inner_w)
            return
        elif current == "Daily":
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
        # The selected row of a ranked/sessions list, as a content-line index.
        sel_line = None
        if self._trend_rows_at is not None:
            line0, drawn, start = self._trend_rows_at
            cursor = self.trend_drill_index if self.trend_drill else self.trend_row_index
            if start <= cursor < start + drawn:
                sel_line = line0 + (cursor - start)
        for i, line in enumerate(content):
            is_title = line.startswith("# ")
            is_marker = line.lstrip().startswith("▲")  # the bar cursor's pointer line
            if i == sel_line:
                row = pad(line, inner_w - graph_off)
                self.write(stdscr, y + 3 + i, 2 + graph_off, row, curses.A_REVERSE | curses.A_BOLD)
                self.write_selected_bars(stdscr, y + 3 + i, 2 + graph_off, row)
                continue
            if is_title:
                attr = curses.color_pair(4) | curses.A_BOLD
            elif is_marker:
                attr = curses.color_pair(6) | curses.A_BOLD
            else:
                attr = curses.A_NORMAL
            x = max(0, graph_center - len(line) // 2) if is_title else graph_off
            self.write_rich(stdscr, y + 3 + i, 2 + x, shorten(line, inner_w - x), attr)
        # Hand the mouse handler this frame's geometry: the bar slots (shifted by
        # the centering offset) and the selectable rows' screen band.
        if self._bar_slots and current in ("Daily", "Weekly", "Monthly"):
            xoff = 2 + graph_off
            y0 = y + 3 + 2  # the chart block starts after its title + blank line
            y1 = min(y0 + self._bar_click_rows - 1, y + 3 + len(content) - 1)
            self.app._trend_bar_geom = (
                y0,
                y1,
                [(x0 + xoff, x1 + xoff, key) for x0, x1, key in self._bar_slots],
            )
        if self._trend_rows_at is not None:
            line0, drawn, start = self._trend_rows_at
            kind = "trendses" if self.trend_drill else "trendrow"
            self._add_rows_region(kind, y + 3 + line0, 2, width - 3, start, drawn)

    def _bar_chart(
        self,
        pairs: list[tuple[str, float]],
        width: int,
        height: int,
        keys: list[str] | None = None,
        selected: str | None = None,
    ) -> list[str]:
        # Vertical bar chart from chronological (label, value) pairs. Shows the most
        # recent buckets that fit; eighth-blocks give sub-row resolution on top.
        # The spend for each bar rides on top of it (no y-axis) — the peak is always
        # labelled and the rest fill in where there's room; when dense (e.g. daily),
        # bars pack in and labels/x-ticks are spaced so they never overlap.
        # `keys` names each bar's bucket (defaults to its label) for the mouse
        # geometry stash; `selected` marks that bucket's bar with a ▲ cursor line.
        self._bar_slots = None
        self._bar_click_rows = 0
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

        # Each shown bar's clickable slot (its whole float-width column, so short
        # bars are easy to hit) tagged with its bucket key, for _trend_bar_at.
        shown_keys = (keys or [label for label, _ in pairs])[len(pairs) - n :]
        self._bar_slots = [
            (margin + round(i * step), margin + round((i + 1) * step) - 1, shown_keys[i])
            for i in range(n)
        ]

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
        self._bar_click_rows = len(out)  # grid + baseline + axis: the clickable band
        if selected in shown_keys:
            # The focused-chart cursor: a ▲ under the selected bar, its bucket and
            # value beside it (before the ▲ when the bar sits near the right edge).
            sel_i = shown_keys.index(selected)
            marker = [" "] * total_w
            center = min(x0_of(sel_i) + bar_w // 2, total_w - 1)
            marker[center] = "▲"
            text = f" {selected} · {money(shown[sel_i][1])}"
            if center + 1 + len(text) <= total_w:
                start = center + 1
            else:
                text = f"{selected} · {money(shown[sel_i][1])} "
                start = max(0, center - len(text))
            for j, ch in enumerate(text):
                if 0 <= start + j < total_w and start + j != center:
                    marker[start + j] = ch
            out.append("".join(marker).rstrip())
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

    def _bar_selection(self, tab: str, data: list[tuple[str, float]]) -> str | None:
        # The bucket to mark with the ▲ cursor: only when this chart is the focused
        # Trends tab (a direct trend_* call from a detail context never selects).
        active = self.trend_tabs[self.trend_tab % len(self.trend_tabs)]
        if not (self.trends and self.trend_focus and active == tab):
            return None
        return self._effective_bar_cursor(data)

    def trend_daily(self, width: int, height: int) -> list[str]:
        # One calendar month at a time (navigate with j/k); the x-axis is the day
        # of the month, so it stays readable instead of cramming the whole range.
        month, data = self.trend_daily_data()
        if month is None:
            return ["# Daily spend", "", "No spend in the active range."]
        months = self.trend_months()
        idx = months.index(month)
        pairs = [(str(int(d[8:10])), v) for d, v in data]
        title = f"# Daily spend · {month}"
        if len(months) > 1:
            title += f"   ({idx + 1}/{len(months)} — j/k older/newer month)"
        chart = self._bar_chart(
            pairs,
            width,
            height - 2,
            keys=[d for d, _ in data],
            selected=self._bar_selection("Daily", data),
        )
        return [title, ""] + chart

    def trend_weekly(self, width: int, height: int) -> list[str]:
        # One ISO week at a time (navigate with j/k), x-axis is Mon..Sun of that week.
        # Like trend_daily, but a week instead of a month -- finer-grained browsing.
        monday, data = self.trend_weekly_data()
        if monday is None:
            return ["# Weekly spend", "", "No spend in the active range."]
        weeks = self.trend_weeks()
        idx = weeks.index(monday)
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        pairs = [(names[i], v) for i, (_d, v) in enumerate(data)]
        sunday = data[-1][0]
        title = f"# Weekly spend · {monday} – {sunday}"
        if len(weeks) > 1:
            title += f"   ({idx + 1}/{len(weeks)} — j/k older/newer week)"
        chart = self._bar_chart(
            pairs,
            width,
            height - 2,
            keys=[d for d, _ in data],
            selected=self._bar_selection("Weekly", data),
        )
        return [title, ""] + chart

    def trend_monthly(self, width: int, height: int) -> list[str]:
        data = self.trend_monthly_data()
        if not data:
            return ["# Monthly spend", "", "No spend in the active range."]
        chart = self._bar_chart(
            data,
            width,
            height - 2,
            selected=self._bar_selection("Monthly", data),
        )
        return ["# Monthly spend (cost per month)", ""] + chart

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
        # Until the grid is focused it reads as "asleep": every cell is dimmed and only
        # the cursor marker stays lit, so the bright [ ] on the muted field invites the
        # Enter that wakes the whole map up — the affordance without spelling it out.
        weekday_labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        for r in range(7):
            ry = top + 3 + r * row_pitch
            self.write(stdscr, ry, left + xoff, weekday_labels[r], curses.color_pair(1))
            for c in range(shown):
                cell = grid[r][start_col + c]
                if cell is None:
                    continue  # a padding day outside the year: leave it blank
                glyph, attr = self._heat_cell(heat_level(cell, peak, levels), levels)
                if not self.trend_focus:
                    attr = (attr & ~curses.A_BOLD) | curses.A_DIM  # dim the sleeping grid
                self.write(stdscr, ry, gx + c * pitch, glyph, attr)

        # Frame the highlighted day in the gap columns around its cell, so the brackets
        # never overwrite a neighbouring glyph. The marker stays bright in both states:
        # against the dimmed unfocused grid it's the lone focal point ("start here"),
        # and on the lit focused grid it's the cursor the arrows walk.
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
        # Built as (text, attr) segments first so the whole strip can be centered under
        # the grid; legend cells stay bright (a reference key) even while the grid sleeps.
        ly = top + 3 + 6 * row_pitch + 2  # one blank line below the last weekday row
        sep = "  " if levels <= 6 else " "  # tighten the bands so a finer ramp still fits
        legend: list[tuple[str, int]] = []
        if peak > 0:
            legend.append(("per day  ", curses.color_pair(1)))
            bounds = [math.expm1(math.log1p(peak) * i / levels) for i in range(levels + 1)]
            for level in range(levels + 1):
                legend.append(self._heat_cell(level, levels))
                label = f" $0{sep}" if level == 0 else f" ≤{heat_band_label(bounds[level])}{sep}"
                legend.append((label, curses.color_pair(1)))
        else:
            legend.append(("Less ", curses.color_pair(1)))
            for level in range(levels + 1):
                legend.append(self._heat_cell(level, levels))
            legend.append((" More", curses.color_pair(1)))
        lx = left + max(0, (width - sum(len(t) for t, _ in legend)) // 2)  # center the strip
        for text, seg_attr in legend:
            self.write(stdscr, ly, lx, text, seg_attr)
            lx += len(text)

        if total > 0:
            peak_date = max(by_date, key=by_date.__getitem__)
            summary = (
                f"total {money(total)}   peak {money(peak)} on {peak_date}   {active} active days"
            )
        elif sessions:
            summary = f"{sessions} sessions, no recorded spend this year"
        else:
            summary = "no spend this year"
        # Below the legend, all centered: the year summary, then either the focused-day
        # detail (when the grid is live) or an orange "press Enter" call-to-action (when
        # it's asleep), then the $ nudge for $0 years. Painted top-down, clipped to fit.
        info: list[tuple[str, int]] = [(summary, curses.A_NORMAL)]
        if self.trend_focus:
            if cursor:
                cd = datetime.strptime(cursor, "%Y-%m-%d")
                day_cost = by_date.get(cursor, 0.0)
                day_sessions = sum(1 for w in self.all_workflows if w.created_at[:10] == cursor)
                label = f"▸ {weekday_labels[cd.weekday()]} {cursor}   "
                if day_sessions:
                    noun = "session" if day_sessions == 1 else "sessions"
                    info.append(
                        (f"{label}{money(day_cost)}   {day_sessions} {noun}   Enter opens", 0)
                    )
                else:
                    info.append((f"{label}no sessions   move with ←↑↓→", 0))
        else:
            info.append(("", 0))  # a couple of blank lines set the call-to-action apart
            info.append(("", 0))
            info.append(
                ("Press Enter to navigate the calendar", curses.color_pair(6) | curses.A_BOLD)
            )
        if total == 0 and sessions and not self.show_api_prices:
            info.append(("$ prices subscription/credit usage at API list rates", 0))
        for i, (line, line_attr) in enumerate(info):
            row_y = ly + 1 + i
            if row_y >= top + height:
                break
            text = shorten(line, width)
            cx = left + max(0, (width - len(text)) // 2)  # center each line under the grid
            self.write_rich(stdscr, row_y, cx, text, line_attr)

    # Custom-color index blocks (only touched when the terminal can redefine colors):
    # roles allocate up from _THEME_COLOR_BASE, the two heat ramps get fixed slots so
    # they can be re-init_color'd every frame without exhausting the palette.
    _THEME_COLOR_BASE = 16
    _HEAT_COLOR_BASE = 40  # calendar heat colours (up to HEAT_MAX_LEVELS)
    _PRICE_COLOR_BASE = 56  # price-heat colours (PRICE_HEAT_LEVELS)
    _BASE_PAIR = 32  # the window background pair (ink on theme bg); clear of heat/price
    _bg_index = -1  # the theme's background colour index (set in init_theme_colors)

    def _color_index(self, hexcolor: str) -> int:
        # A curses color index for a hex: a fresh init_color slot on truecolor
        # terminals (cached per hex), else the nearest xterm-256. Falls back to the
        # nearest-256 if init_color is refused, so a partial terminal never crashes.
        cache = self._theme_color_cache
        if hexcolor in cache:
            return cache[hexcolor]
        idx = nearest_256(hexcolor)
        if self._can_change and self._next_color < curses.COLORS:
            try:
                curses.init_color(self._next_color, *hex_rgb1000(hexcolor))
                idx = self._next_color
                self._next_color += 1
            except curses.error:
                pass
        cache[hexcolor] = idx
        return idx

    def init_theme_colors(self) -> None:
        # Map the active theme's role hexes onto the fixed color-pair layout the whole
        # renderer draws against (pairs 1..7 + the two heat ramps). Re-run on a live
        # theme switch. 8-colour terminals map roles to the nearest of the 8.
        #
        # Every pair paints an *explicit* theme background (not "-1"/terminal default),
        # and draw() sets the window background to _BASE_PAIR (ink on bg) before each
        # erase -- so the theme's bg fills every cell the way neovim's Normal group does,
        # and a light theme actually shows a light screen instead of coloured text on the
        # terminal's own dark background. (assume_default_colors only changes what "-1"
        # *means*; ncurses still erases to the terminal default, which is why it stayed
        # dark -- so we colour every cell instead.)
        self._theme_color_cache = {}
        self._next_color = self._THEME_COLOR_BASE
        self._can_change = bool(
            self.has256 and getattr(curses, "can_change_color", lambda: False)()
        )
        roles = self.app.theme["roles"]
        r = self._color_index
        bg = self._bg_index = r(roles["bg"])
        self._themed_bg = False
        try:  # the window-background pair; if the terminal is too small for it, skip the fill
            curses.init_pair(self._BASE_PAIR, r(roles["ink"]), bg)
            self._themed_bg = True
        except curses.error:
            self._bg_index = bg = -1  # no themed fill -> role pairs fall back to terminal bg
        curses.init_pair(1, r(roles["ink2"]), bg)  # secondary text
        curses.init_pair(2, r(roles["accent"]), bg)  # warm accent / title / M-tokens
        curses.init_pair(3, r(roles["good"]), bg)  # money
        curses.init_pair(4, r(roles["mut"]), bg)  # structural: headers, keybar, '#'
        curses.init_pair(5, r(roles["bad"]), bg)  # alerts
        curses.init_pair(6, r(roles["accent_bright"]), bg)  # focus / active border
        curses.init_pair(7, bg, r(roles["accent"]))  # active tab (inverse: bg on accent)
        self._init_price_heat()
        self._sync_heat_palette()

    def apply_background(self, stdscr) -> None:
        # Point the window background at the theme's base pair (ink on bg) so erase()
        # fills every cell with the theme bg and A_NORMAL text reads as theme ink. Called
        # each frame before erase, so a live theme switch repaints the whole screen.
        if not getattr(self, "_themed_bg", False):
            return
        try:
            stdscr.bkgd(" ", curses.color_pair(self._BASE_PAIR))
        except curses.error:
            pass

    def _init_price_heat(self) -> None:
        # The P overlay's cheap→pricey ramp, fixed granularity (PRICE_HEAT_LEVELS).
        hexes = self.app.theme["price_heat"]
        if self._can_change or self.has256:
            for i, hx in enumerate(hexes):
                curses.init_pair(
                    PRICE_HEAT_BASE_PAIR + i,
                    self._heat_index(self._PRICE_COLOR_BASE + i, hx),
                    self._bg_index,
                )
        else:
            for i, col in enumerate(heat_palette(PRICE_HEAT_LEVELS, False)):
                curses.init_pair(PRICE_HEAT_BASE_PAIR + i, col, self._bg_index)

    def _heat_index(self, slot: int, hexcolor: str) -> int:
        # A reusable fixed-slot heat colour: re-init_color the slot on truecolor
        # terminals (so per-frame ramps don't leak indices), else nearest-256.
        if self._can_change:
            try:
                curses.init_color(slot, *hex_rgb1000(hexcolor))
                return slot
            except curses.error:
                pass
        return nearest_256(hexcolor)

    def _sync_heat_palette(self) -> None:
        # Re-init the calendar heat pairs (8..) for the current granularity so +/-
        # restyles live. Colours come from the active theme's ramp, resampled to
        # cal_levels; 8-colour terminals keep the generated ANSI ramp + glyphs.
        if self.has256:
            for i, hx in enumerate(ramp(self.app.theme["heat"], self.cal_levels)):
                curses.init_pair(
                    8 + i, self._heat_index(self._HEAT_COLOR_BASE + i, hx), self._bg_index
                )
        else:
            for i, col in enumerate(heat_palette(self.cal_levels, False)):
                curses.init_pair(8 + i, col, self._bg_index)

    def _heat_cell(self, level: int, levels: int) -> tuple[str, int]:
        # (glyph, attr) for one heat level: a distinct color per level, plus a glyph that
        # keeps levels apart where the color ramp collapses (8-color / mono terminals).
        if level <= 0:
            return HEAT_EMPTY_GLYPH, curses.color_pair(1) | curses.A_DIM
        return heat_glyph(level, levels, self.has256), curses.color_pair(7 + level) | curses.A_BOLD

    def _trend_cursor_window(self, n: int, fit: int) -> tuple[int, int, int]:
        # Clamp the ranked-row cursor (writing the clamp back, so a shrunk list
        # never leaves it dangling), then a stateless window that keeps it visible:
        # (cursor, window start, rows shown).
        idx = max(0, min(self.app.trend_row_index, n - 1))
        self.app.trend_row_index = idx
        fit = max(1, fit)
        start = max(0, min(idx - fit // 2, n - fit))
        return idx, start, min(fit, n - start)

    def trend_models(self, width: int, height: int) -> list[str]:
        all_rows = self.trend_model_rows()
        if not all_rows:
            return ["# Model spend", "", "No priced model spend in the active range."]
        total = sum(c for _, c in all_rows)
        peak = max(c for _, c in all_rows) or 1.0
        _idx, start, shown = self._trend_cursor_window(len(all_rows), height - 3)
        rows = all_rows[start : start + shown]
        # Names get priority so long ids like claude-opus-4-5-20251101 show in
        # full; the bar takes only the leftover (kept modest) instead of eating
        # the width and forcing names to truncate.
        tail = 20  # spacing + money (>=11) + percent (5)
        namew = min(max(len(n) for n, _ in rows), max(12, width - tail - 4))
        barw = max(3, min(24, width - namew - tail))
        lines = ["# Model spend (priced, in range)", ""]
        self._trend_rows_at = (len(lines), len(rows), start)
        for name, cost in rows:
            bar = "█" * max(0, round((cost / peak) * barw))
            lines.append(
                f"{pad(shorten(name, namew), namew)}  {bar:<{barw}} {money(cost):>11} {pct(cost, total):>5}"
            )
        return lines

    def trend_providers(self, width: int, height: int) -> list[str]:
        # The per-model spend rolled up to its provider (the "openai" in
        # "openai/gpt-5"), so you can compare e.g. openai vs github-copilot.
        # Subscription/credit providers record $0 per message, so their cost only
        # shows once "$" reprices unpriced usage at API list rates -- the cost column
        # and bar react to it live. We still list those providers when "$" is off
        # (tokens are the tell) and nudge toward "$".
        all_rows = self.trend_provider_rows()
        if not all_rows:
            return ["# Spend by provider", "", "No model usage in the active range."]
        total_cost = sum(float(it["cost"]) for _, it in all_rows)
        peak = max((float(it["cost"]) for _, it in all_rows), default=0.0) or 1.0
        _idx, start, shown = self._trend_cursor_window(len(all_rows), height - 4)
        rows = all_rows[start : start + shown]
        namew = min(max(len(p) for p, _ in rows), max(10, width - 44))
        barw = max(3, min(20, width - namew - 38))
        lines = [
            "# Spend by provider",
            "",
            f"{'Provider':{namew}}  {'':{barw}} {'Cost':>11} {'Share':>5} {'Tokens':>9} {'Msgs':>7}",
        ]
        self._trend_rows_at = (len(lines), len(rows), start)
        for provider, it in rows:
            bar = "█" * max(0, round((float(it["cost"]) / peak) * barw))
            lines.append(
                f"{pad(shorten(provider, namew), namew)}  {bar:<{barw}} "
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
        return self.source_table(
            self.all_workflows, width, limit=max(1, height - 4), selectable=True
        )

    def source_table(
        self,
        workflows: list[Workflow],
        width: int,
        limit: int | None = None,
        selectable: bool = False,
    ) -> list[str]:
        # Spend grouped by the *tool* it came from (OpenCode / Claude Code / Codex).
        # Shared by the Trends "Sources" tab (whole range, selectable: the rows get
        # the trend cursor + Enter drill) and the per-month/day/project "Sources"
        # detail tabs (a scoped slice, plain). Subscription rows (Claude Code,
        # Codex) cost $0 until "$" reprices their tokens, so the bar reacts live.
        all_rows = self.source_rows(workflows)
        if not all_rows:
            return ["# Spend by source", "", "No sessions in the active range."]
        if selectable and limit is not None:
            _idx, start, shown = self._trend_cursor_window(len(all_rows), limit)
            rows = all_rows[start : start + shown]
            # Shares/bars stay anchored to the whole list so scrolling the window
            # never re-scales them under the cursor.
            total_cost = sum(float(it["cost"]) for _, it in all_rows)
            peak = max((float(it["cost"]) for _, it in all_rows), default=0.0) or 1.0
        else:
            start = 0
            rows = all_rows if limit is None else all_rows[:limit]
            total_cost = sum(float(it["cost"]) for _, it in rows)
            peak = max((float(it["cost"]) for _, it in rows), default=0.0) or 1.0
        namew = min(max(len(s) for s, _ in rows), max(10, width - 44))
        barw = max(3, min(20, width - namew - 38))
        lines = [
            "# Spend by source",
            "",
            f"{'Source':{namew}}  {'':{barw}} {'Cost':>11} {'Share':>5} {'Tokens':>9} {'Sess':>7}",
        ]
        if selectable:
            self._trend_rows_at = (len(lines), len(rows), start)
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

    def trend_drill_lines(self, width: int, height: int) -> list[str]:
        # A ranked row's sessions list (Enter on Models/Providers/Sources): every
        # root session in the active range that used it, with its cost/tokens
        # within the session, windowed around the cursor.
        kind, key = self.trend_drill
        rows = self.trend_drill_sessions()
        title = f"# Sessions · {key}"
        if not rows:
            return [title, "", f"No sessions used {key} in the active range."]
        subtotal = sum(cost for _w, cost, _t in rows)
        lines = [
            title,
            "",
            f"{len(rows)} session(s) · {money(subtotal)} on this {kind}",
            f"{'Started':<10} {'Cost':>9} {'Tokens':>8}  {self.src_col()}{'Title'}",
        ]
        idx = max(0, min(self.app.trend_drill_index, len(rows) - 1))
        self.app.trend_drill_index = idx
        fit = max(1, height - len(lines))
        start = max(0, min(idx - fit // 2, len(rows) - fit))
        shown = rows[start : start + min(fit, len(rows) - start)]
        self._trend_rows_at = (len(lines), len(shown), start)
        for w, cost, tok in shown:
            lines.append(
                f"{w.created_at[:10]:<10} {money(cost):>9} {human_tokens(tok):>8}  "
                f"{self.src_col(w)}{shorten(w.title, max(8, width - 34))}"
            )
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
            # Clip by display cells, not codepoints, so wide (CJK) text never
            # overflows the row and wraps.
            stdscr.addstr(y, x, clip(text, max(0, width - x - 1)), attr)
        except curses.error:
            pass

    def write_selected_bars(self, stdscr: curses.window, y: int, x: int, text: str) -> None:
        # Repaint the block-glyph bar runs of a row just written with A_REVERSE:
        # reverse video renders a block in the pair's background colour, so the
        # spend bar reads as a theme-bg hole in the highlight band. Overdrawing
        # the runs non-reversed in the focus accent keeps the bar legible (a full
        # block fills its cell, so the band shows no seam around it).
        for match in BAR_GLYPH_PATTERN.finditer(text):
            self.write(
                stdscr,
                y,
                x + display_width(text[: match.start()]),
                match.group(0),
                curses.color_pair(6) | curses.A_BOLD,
            )

    def write_rich(self, stdscr: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
        self.write(stdscr, y, x, text, attr)
        if attr & curses.A_BOLD and text.startswith("# "):
            return
        if text.lstrip().startswith("ID:"):
            return  # session ids can contain money/token-like runs; don't recolor them
        for match in MONEY_PATTERN.finditer(text):
            self.write(
                stdscr,
                y,
                x + display_width(text[: match.start()]),
                match.group(0),
                self.money_attr(match.group(0)),
            )
        for match in TOKEN_PATTERN.finditer(text):
            token_text = match.group(0)
            self.write(
                stdscr,
                y,
                x + display_width(text[: match.start()]),
                token_text,
                self.token_attr(token_text),
            )
