"""Renderer: all drawing."""
from __future__ import annotations

import math
import textwrap
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from opentab import __version__
from opentab.models import (
    DaySummary,
    MachineSummary,
    MonthSummary,
    ProjectSummary,
    Workflow,
    YearSummary,
)
from opentab.themes import hex_rgb1000, ink_on, nearest_8, nearest_256, ramp
from opentab.tui import keymap

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
    HEAT_EMPTY_GLYPH,
    PRICE_HEAT_BASE_PAIR,
    PRICE_HEAT_LEVELS,
    TOKEN_SERIES_BASE_PAIR,
    TOKEN_SERIES_GLYPHS,
    TOOL_HEAT_BASE_PAIR,
    TOOL_HEAT_LEVELS,
    calendar_cells,
    heat_band_label,
    heat_glyph,
    heat_level,
    heat_palette,
    token_series,
    token_series_ansi,
)
from opentab.models import ALL_YEARS, year_label
from opentab.pricing import (
    TOKEN_TYPES,
    api_equivalent_cost,
    cache_misses,
    family_label,
    is_local_provider,
    model_context_window,
    model_price,
    price_source_meta,
)
from opentab.util import (
    CONTEXT_COMPACT_FLOOR,
    CONTEXT_COMPACT_RATIO,
    agent_mix_label,
    cached_share,
    context_compactions,
    context_size,
    fuzzy_score,
    short_tool_name,
    tool_call_label,
    tool_mix_label,
    tool_names,
    tool_namespace,
    unicode_screen,
)


def _turn_read_mark(row) -> str:
    # N narration, R reasoning, NR both -- letters rather than colour, because the row
    # can be selected (reverse video), the terminal can be monochrome, and colour alone
    # cannot say WHICH of the two a turn holds.
    return ("N" if row.get("has_text") else "") + ("R" if row.get("has_reasoning") else "")


def _turn_agent(row) -> str:
    # Keep the subagent marker consistent with the web frontend.
    label = (row.get("agent") or "-").strip() or "-"
    return f"↳ {label}" if row.get("depth") else label


class Renderer:
    """Terminal drawing, delegating state and logic reads to the App."""

    # Must match the labels emitted by the header builders for click hit-testing.
    SESSION_SORT_COLUMNS = (
        ("cost", "Cost"),
        ("tokens", "Tokens"),
        ("subagents", "Subagents"),
        ("title", "Title"),
    )
    PROJECT_SORT_COLUMNS = (
        ("project", "Project"),
        ("cost", "Cost"),
        ("tokens", "Tokens"),
        ("sessions", "Ses"),
        ("subagents", "Subagents"),
    )
    SUBAGENT_SORT_COLUMNS = (
        ("date", "Started"),
        ("depth", "D"),
        ("agent", "Agent"),
        ("model", "Model"),
        ("cost", "Cost"),
        ("tokens", "Tokens"),
        ("title", "Title"),
    )
    # Base labels are located in the rendered header, including active-sort arrows.
    PRICE_SORT_COLUMNS = (
        ("model", "model"),
        ("eff", "eff $/M"),
        ("use", "use"),
        ("input", "input"),
        ("output", "output"),
        ("cache_read", "cacheR"),
        ("cache_write", "cacheW"),
    )

    def _key(self, ctx: str, action: str) -> str:
        # Painted hints must follow live keymap remappings.
        return self.app.keymap.label(ctx, action)

    def _keys(self, ctx: str, *actions: str) -> str:
        return "/".join(filter(None, (self.app.keymap.label(ctx, a) for a in actions)))

    def _menu_title(self, title: str, ctx: str) -> str:
        parts = [
            title,
            self._keys(ctx, "down", "up"),
            self._key(ctx, "select"),
            self._key(ctx, "cancel"),
        ]
        return " · ".join(p for p in parts if p)

    def __init__(self, app: App) -> None:
        self.app = app
        # Drawers use content coordinates. Only write/hline/frame add the app-frame
        # origin; it remains zero for headless tests that call drawers directly.
        self.oy = 0
        self.ox = 0
        # Paint side channels are initialized here for headless line-builder tests.
        self._token_runs: dict[str, list[tuple[int, int, int]]] = {}
        self._theme_color_cache: dict[str, int] = {}
        self._fallback_used: set[int] = set()
        # Treemap lines are mostly spaces, so line text is not a unique paint key.
        self._tool_tree_runs: dict[int, list[tuple[int, int, int]]] = {}
        # Rebuilt each frame in content coordinates; App removes the origin once.
        # Row regions map y to start + offset; other regions carry a direct index.
        self.regions: list[tuple] = []
        # (y, x0, x1, key, target), rebuilt with regions each frame.
        self.sort_regions: list[tuple] = []
        # Draw-time trend geometry used for hit-testing and row highlights.
        self._bar_slots: list[tuple[int, int, str]] | None = None
        self._bar_click_rows = 0
        self._trend_rows_at: tuple[int, int, int] | None = None
        self._turn_header_at: dict[int, str] = {}
        # Selected prompt header line, recomputed each paint for scroll/highlight.
        self._turn_cursor_line: int | None = None
        # Logical header lines become screen-coordinate sort regions during paint.
        self._line_sort_headers: dict[int, tuple[tuple, str]] = {}
        # Models remain plain strings; line-to-row mapping adds selection at paint time.
        self._model_row_at: dict[int, int] = {}
        self._model_cursor_line: int | None = None
        # Derive body offsets while building: empty bodies change box prologue length.
        self._ruled_body_start: int | None = None
        # Header identity is textual because independently built boxes are later stacked.
        self._box_headers: set[str] = set()

    def __getattr__(self, name: str):
        return getattr(self.app, name)

    def _add_rows_region(
        self, kind: str, y_first: int, x0: int, x1: int, start: int, drawn: int
    ) -> None:
        if drawn > 0:
            self.regions.append(("rows", kind, y_first, y_first + drawn - 1, x0, x1, start))

    def hit(self, my: int, mx: int) -> tuple[str, int] | None:
        # First match wins, allowing broad catch-all regions to be appended last.
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
        # Locate labels in the clipped, arrow-bearing text so hit zones match pixels.
        drawn = shorten(header, max_w)
        pos = 0
        for key, label in columns:
            i = drawn.find(label, pos)
            if i < 0:
                continue
            self.sort_regions.append((y, x_base + i, x_base + i + len(label) - 1, key, target))
            pos = i + len(label)

    def sort_hit(self, my: int, mx: int) -> tuple[str, str] | None:
        for y, x0, x1, key, target in self.sort_regions:
            if my == y and x0 <= mx <= x1:
                return key, target
        return None

    def _mark_box_header(self, text: str, width: int) -> None:
        # Text survives later box stacking; local line indices do not. Position is also
        # insufficient because sectioned boxes may place charts before their headers.
        self._box_headers.add(self.box_row(text, width))

    def box_header_lines(self, lines: list[str]) -> set[int]:
        # A stale textual match is harmless: byte-identical framed text is still a header.
        return {i for i, line in enumerate(lines) if line in self._box_headers}

    # Text-only accent avoids stacking multiple filled chrome bands in Overview panes.
    HEADER_PAIR = 6

    def _paint_box_header(
        self, stdscr: curses.window, y: int, x: int, line: str, width: int
    ) -> None:
        # Keep frame gutters plain; only column labels receive the header attribute.
        row = shorten(line, width)
        attr = curses.color_pair(self.HEADER_PAIR) | curses.A_BOLD
        if len(row) > self.BOX_CHROME and row[:1] in ("│", "|") and row[-1:] in ("│", "|"):
            head, cells, tail = row[:2], row[2:-2], row[-2:]
            self.write(stdscr, y, x, head, curses.A_NORMAL)
            self.write(stdscr, y, x + display_width(head), cells, attr)
            self.write(stdscr, y, x + display_width(head + cells), tail, curses.A_NORMAL)
            return
        self.write(stdscr, y, x, row, attr)

    def _register_line_sort_header(
        self, sy: int, sx: int, line_index: int, line: str, max_w: int
    ) -> None:
        meta = self._line_sort_headers.get(line_index)
        if meta:
            self._register_sort_header(sy, sx, line, meta[0], meta[1], max_w)

    def _paint_detail_lines(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, lines: list[str]
    ) -> None:
        visible = h - 4
        # Gate model cursor state on the active tab; its line map persists until the
        # model table is rebuilt and is therefore stale while another tab is visible.
        models = self.view == "zoom" and self.on_models_tab
        if models:
            # Model scrolling moves the cursor, so always keep the selection visible.
            self._scroll_line_into_view(self._model_cursor_line, visible)
        self.app.scroll = max(0, min(self.app.scroll, max(0, len(lines) - visible)))
        drawn = lines[self.scroll : self.scroll + visible]
        headers = self.box_header_lines(lines) | set(self._line_sort_headers)
        for offset, line in enumerate(drawn):
            index = self.scroll + offset
            if models and index == self._model_cursor_line:
                self._paint_model_cursor(stdscr, y + 3 + offset, x + 2, line, w - 4)
                continue
            if index in headers:
                self._paint_box_header(stdscr, y + 3 + offset, x + 2, line, w - 4)
                self._register_line_sort_header(y + 3 + offset, x + 2, index, line, w - 4)
                continue
            self.write_rich(
                stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), self.line_attr(line)
            )
            self._paint_token_runs(stdscr, y + 3 + offset, x + 2, line, w - 4)
            self._register_line_sort_header(y + 3 + offset, x + 2, index, line, w - 4)
        if models:
            self._add_rows_region("zoommodel", y + 3, x + 2, x + w - 3, self.scroll, len(drawn))

    def paint_cursor_row(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        line: str,
        width: int,
        attr: int | None = None,
        bars: bool = False,
    ) -> None:
        # Use write(), not write_rich(): rich token/money overpainting shreds reverse
        # highlights. `bars` restores block glyphs hidden by reverse video.
        attr = curses.color_pair(6) | curses.A_BOLD | curses.A_REVERSE if attr is None else attr
        row = shorten(line, width)
        if len(row) > self.BOX_CHROME and row[:1] in ("│", "|") and row[-1:] in ("│", "|"):
            # Reverse only cells between gutters so the cursor does not erase the frame.
            head, cells, tail = row[:2], row[2:-2], row[-2:]
            frame = self.line_attr(line)
            self.write(stdscr, y, x, head, frame)
            self.write(stdscr, y, x + display_width(head), cells, attr)
            if bars:
                self.write_selected_bars(stdscr, y, x + display_width(head), cells)
            self.write(stdscr, y, x + display_width(head + cells), tail, frame)
            return
        row = pad(row, width)
        self.write(stdscr, y, x, row, attr)
        if bars:
            self.write_selected_bars(stdscr, y, x, row)

    def _paint_model_cursor(
        self, stdscr: curses.window, y: int, x: int, line: str, width: int
    ) -> None:
        self.paint_cursor_row(stdscr, y, x, line, width)

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
        return max(8, width - 41)

    def project_row_text(self, project: ProjectSummary, marker: str, width: int) -> str:
        name_width = self.project_name_width(width)
        name = short_path(project.directory, max(1, name_width - (2 if project.ignored else 0)))
        if project.ignored:
            name = f"× {name}"
        return (
            f"{marker} {pad(name, name_width)} "
            f"{money(project.cost):>9} {human_tokens(project.tokens):>7} "
            f"{project.workflows:>3} ses {project.subagents:>6} subs"
        )

    def project_total_text(self, rows: list[ProjectSummary], width: int) -> str:
        # Pickers omit totals because a fixed footer would appear to sum only the window.
        name_width = self.project_name_width(width)
        return (
            f"  {pad('TOTAL', name_width)} "
            f"{money(sum(p.cost for p in rows)):>9} "
            f"{human_tokens(sum(p.tokens for p in rows)):>7} "
            f"{sum(p.workflows for p in rows):>3} ses {sum(p.subagents for p in rows):>6} subs"
        )

    def project_header_text(self, width: int) -> str:
        name_width = self.project_name_width(width)
        return (
            f"  {self.project_sort_heading('project', 'Project'):{name_width}} "
            f"{self.project_sort_heading('cost', 'Cost'):>9} "
            f"{self.project_sort_heading('tokens', 'Tokens'):>7} "
            f"{self.project_sort_heading('sessions', 'Ses'):>7} "
            f"{self.project_sort_heading('subagents', 'Subagents'):>11}"
        )

    def list_width(self, rows_text: list[str], width: int) -> int:
        content = max((len(r) for r in rows_text), default=20)
        return max(24, min(content + 3, max(24, width - 44)))

    def projects_left_width(self, width: int) -> int:
        # Leave at least half the screen, and 44 columns, for detail.
        longest = max(
            (display_width(short_path(p.directory, 999)) for p in self.projects), default=8
        )
        natural = max(longest, len("Project")) + 42  # marker + Cost/Tokens/Ses/Subagents
        return max(24, min(natural, width // 2, max(24, width - 44)))

    @staticmethod
    def machine_name_width(width: int) -> int:
        return max(8, width - 30)

    @staticmethod
    def machine_badge(machine: MachineSummary) -> str:
        # Match the web frontend: live, pulled snapshot, synthetic fleet total.
        if machine.fleet:
            return "∑"
        return "●" if machine.live else "○"

    def machine_row_text(self, machine: MachineSummary, marker: str, width: int) -> str:
        name_width = self.machine_name_width(width)
        name = shorten(f"{self.machine_badge(machine)} {machine.name}", name_width)
        return (
            f"{marker} {pad(name, name_width)} "
            f"{money(machine.cost):>9} {human_tokens(machine.tokens):>7} "
            f"{machine.workflows:>3} ses"
        )

    def machine_header_text(self, width: int) -> str:
        name_width = self.machine_name_width(width)
        return f"  {'Machine':{name_width}} " f"{'Cost':>9} {'Tokens':>7} {'Ses':>6}"

    def machines_left_width(self, width: int) -> int:
        longest = max((display_width(m.name) for m in self.machines), default=8)
        # Include the badge and its space inside the name field budget.
        natural = max(longest, len("Machine")) + 34
        return max(24, min(natural, width // 2, max(24, width - 44)))

    def browse_left_width(self, width: int) -> int:
        if self.browse_mode == "machines":
            return self.machines_left_width(width)
        if self.browse_mode == "projects":
            return self.projects_left_width(width)
        rows = [self.year_row_text(yr, ">") for yr in self.years]
        rows += [self.month_row_text(m, ">") for m in self.months]
        rows += [self.day_row_text(d, ">") for d in self.panel_days]
        # Reserve the spend-bar lane without starving the detail pane below 44 columns.
        base = self.list_width(rows, width)
        return max(24, min(base + BAR_CELLS + 2, max(24, width - 44)))

    def draw_time_panels(
        self, stdscr: curses.window, top: int, avail: int, left: int, focus: str | None
    ) -> None:
        # Panels render h-3 rows. Cap Years so a long history cannot starve Months/Days.
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
        # Keep bars outside row highlights; omit the lane when the panel is too narrow.
        if w < 46:
            return 0, w - 2
        return BAR_CELLS, (w - 2) - (BAR_CELLS + 2)

    # App._page_step must use the same frame/header/footer/detail chrome budget.
    CHROME_ROWS = 11
    CHROME_COLS = 2

    def pager_height(self, stdscr: curses.window) -> int:
        height, _width = stdscr.getmaxyx()
        return max(1, height - self.CHROME_ROWS)

    def max_scroll(self, stdscr: curses.window) -> int:
        _height, width = stdscr.getmaxyx()
        lines = self.current_pager_lines(width - self.CHROME_COLS)
        return max(0, len(lines) - self.pager_height(stdscr))

    def current_pager_lines(self, width: int) -> list[str]:
        content_width = max(1, width - 4)
        if self.view == "session":
            workflow = self.current_session()
            if workflow is None:
                return []
            # Optional session tabs make name-based dispatch mandatory.
            tabs = self.current_tabs()
            current = tabs[self.tab % len(tabs)]
            if current == "Subagents":
                return self.detail_subagents(workflow, content_width)
            if current == "Turns":
                return self.detail_turns(workflow, content_width)
            if current == "Tools":
                return self.detail_tools(workflow, content_width)
            if current == "Context":
                return self.detail_context(workflow, content_width)
            return self.detail_overview(workflow, content_width)

        if self.view == "zoom":
            if self.browse_mode == "machines":
                machine = self.selected_machine_summary
                if machine is None:
                    return []
                current = self.current_tabs()[self.tab % len(self.current_tabs())]
                if current == "Overview":
                    return self.machine_overview(machine, content_width)
                if current == "Harnesses":
                    return self.machine_sources(machine, content_width)
                if current == "Models":
                    return self.machine_models(machine, content_width)
                if current == "Projects":
                    return self.machine_projects(machine, content_width)
                return self.machine_workflows(machine, content_width)
            if self.browse_mode == "projects":
                project = self.selected_project_summary
                if project is None:
                    return []
                current = self.current_tabs()[self.tab % len(self.current_tabs())]
                if current == "Overview":
                    return self.project_overview(project, content_width)
                if current == "Harnesses":
                    return self.project_sources(project, content_width)
                if current == "Machines":
                    return self.project_machines(project, content_width)
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
                if current == "Harnesses":
                    return self.year_sources(year, content_width)
                if current == "Machines":
                    return self.year_machines(year, content_width)
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
                if current == "Harnesses":
                    return self.month_sources(month, content_width)
                if current == "Machines":
                    return self.month_machines(month, content_width)
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
            if current == "Harnesses":
                return self.day_sources(day, content_width)
            if current == "Machines":
                return self.day_machines(day, content_width)
            if current == "Projects":
                return self.day_projects(day, content_width)
            return self.day_workflows(day, content_width)

        return []

    def draw(self, stdscr: curses.window) -> None:
        # Settle before painting so breadcrumbs cannot name a drill the session list drops.
        self.app.settle_drills()
        self.apply_background(stdscr)  # theme bg fills the screen (before erase reads it)
        stdscr.erase()
        self.regions = []  # rebuilt for this frame's clicks
        self.sort_regions = []
        self._line_sort_headers = {}
        self._box_headers = set()
        # Paint side channels must not color geometry from an earlier frame.
        self._token_runs: dict[str, list[tuple[int, int, int]]] = {}
        self._tool_tree_runs = {}
        self.oy = self.ox = 0  # screen coordinates until the app frame is up
        height, width = stdscr.getmaxyx()
        if height < 20 or width < 80:
            self.write(
                stdscr, 0, 0, "Terminal too small. Need at least 80x20.", curses.color_pair(1)
            )
            stdscr.refresh()
            return

        # Paint the outer frame in screen coordinates, then switch to its inner viewport.
        self.frame_app(stdscr, height, width)
        self.oy = self.ox = 1
        height -= 2
        width -= 2

        self.draw_header(stdscr, width)
        self.draw_footer(stdscr, height, width)

        top = 3
        bottom = height - 2
        avail = bottom - top
        # Help floats over the body so its key context remains visible.
        if self.show_prices:
            self.draw_prices(stdscr, top, bottom, width)
        elif self.trends:
            self.draw_trends(stdscr, top, bottom, width)
        elif self.view == "session":
            self.draw_detail(stdscr, top, 0, avail, width)
        elif self.view == "zoom":
            # Keep the inactive sidebar clickable while zoomed; `+` maximizes detail.
            zx, zw = 0, width
            if not self.zoom_maximized:
                left = self.browse_left_width(width)
                if self.browse_mode == "machines":
                    self.draw_machine_list(stdscr, top, 0, avail, left, active=False)
                elif self.browse_mode == "projects":
                    self.draw_project_list(stdscr, top, 0, avail, left, active=False)
                else:
                    self.draw_time_panels(stdscr, top, avail, left, focus=None)
                zx, zw = left, width - left
            if self.browse_mode == "machines":
                self.draw_machine_detail(stdscr, top, zx, avail, zw)
            elif self.browse_mode == "projects":
                self.draw_project_detail(stdscr, top, zx, avail, zw)
            elif self.focus == "years":
                self.draw_year_detail(stdscr, top, zx, avail, zw)
            elif self.focus == "months":
                self.draw_month_detail(stdscr, top, zx, avail, zw)
            else:
                self.draw_day_detail(stdscr, top, zx, avail, zw)
        elif self.browse_mode == "machines":
            left = self.browse_left_width(width)
            self.draw_machine_list(stdscr, top, 0, avail, left)
            self.draw_machine_detail(stdscr, top, left, avail, width - left, active=False)
            self._add_rows_region("detail", top, left, width - 1, 0, avail)
        elif self.browse_mode == "projects":
            left = self.browse_left_width(width)
            self.draw_project_list(stdscr, top, 0, avail, left)
            self.draw_project_detail(stdscr, top, left, avail, width - left, active=False)
            # Append catch-all last because hit() uses first match.
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

        if self.help:
            self.draw_help(stdscr, top, bottom, width)
        if self.toast_history:
            self.draw_toast_history(stdscr, top, bottom, width)

        if self.startup_warning is not None:
            self.draw_startup_warning(stdscr, height, width)
        elif self.price_prompt:
            self.draw_price_prompt(stdscr, height, width)
        elif self.theme_menu:
            self.draw_theme_menu(stdscr, height, width)
        elif self.demo_menu:
            self.draw_demo_menu(stdscr, height, width)
        elif self.source_menu:
            self.draw_source_menu(stdscr, height, width)
        elif self.machine_menu:
            self.draw_machine_menu(stdscr, height, width)
        elif self.harness_menu:
            self.draw_harness_menu(stdscr, height, width)
        elif self.whatif_menu:
            self.draw_whatif_menu(stdscr, height, width)
        elif self.sort_menu:
            self.draw_sort_menu(stdscr, height, width)
        elif self.launch_menu is not None:
            self.draw_launch_menu(stdscr, height, width)

        # Toasts are the topmost layer, including above modals.
        self.draw_toasts(stdscr, height, width)

        stdscr.refresh()

    def draw_header(self, stdscr: curses.window, width: int) -> None:
        summary = self.store.summary(self.all_workflows)
        title = " OpenTab "
        info = (
            f" {summary['workflows']} sessions "
            f"cost {money(float(summary['cost']))} "
            f"tokens {human_tokens(int(summary['tokens']))} "
            f"subagents {summary['subagents']} "
        )
        self.write(stdscr, 0, 0, title, curses.color_pair(2) | curses.A_BOLD)
        chip = f" {self.store.source_name} "
        self.write(stdscr, 0, len(title), chip, curses.color_pair(7) | curses.A_BOLD)
        # Session-scoped what-if does not alter these aggregate header figures.
        if self.store.demo:
            tag = " DEMO — synthetic "
        elif self.show_api_prices:
            if getattr(self.store, "records_cost", True):
                tag = " WHAT-IF — would-have-paid at API prices "
            else:
                # With no recorded dollars, list-price spend is an estimate, not a delta.
                tag = " ESTIMATED — tokens × API list prices "
        elif not getattr(self.store, "records_cost", True):
            tag = f" $0 = no recorded cost · press {self._key('main', 'api_prices')} to estimate "
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
        # Accent only persistent narrowing modifiers; scope and sort remain neutral.
        # The live filter appears in the command line and is shown here only when committed.
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
            # Display the visible column label, not a shared internal sort key.
            segs.append((f"  ·  sort: {self.sort_label(sort_by).lower()}", base))
        if self.query and not self.filter_active:
            segs.append((f"  ·  filter: {self.query}", active))
        ignored_count = len(self.ignored_projects) + len(self.ignored_sessions)
        if ignored_count:
            segs.append((f"  ·  ignored: {ignored_count}", active))
        if self.machine_filter:  # the `M` global narrowing -- a LIMIT, so accented
            segs.append((f"  ·  machine: {self.machine_filter}", active))
        if self.harness_filter:  # the fleet `H` harness narrowing -- likewise a LIMIT
            segs.append((f"  ·  harness: {self.harness_filter}", active))
        if self.show_bookmarks_only:
            segs.append(("  ·  ★ bookmarks only", active))
        for text, attr in segs:
            x = self.write_seg(stdscr, 1, x, text, attr, width)
        self.draw_mode_tabs(stdscr, 2, width)

    def draw_mode_tabs(self, stdscr: curses.window, y: int, width: int) -> None:
        tabs = self.mode_tab_list()
        modes = [m for _lbl, m in tabs]
        active_index = modes.index(self.browse_mode) if self.browse_mode in modes else 0
        labels = [
            f"[{lbl}]" if i == active_index else f" {lbl} " for i, (lbl, _m) in enumerate(tabs)
        ]
        gap = 1
        total = sum(len(lbl) for lbl in labels) + gap * (len(labels) - 1)
        start = max(0, (width - total) // 2)
        if start >= 2:
            self.hline(stdscr, y, 0, start - 1)  # left rule, a blank cell before the chips
        cx = start
        for i, label in enumerate(labels):
            if i > 0:
                cx += gap
            if cx >= width:
                break
            attr = (
                curses.color_pair(7) | curses.A_BOLD
                if i == active_index
                else curses.color_pair(self._TAB_PAIR)
            )
            text = shorten(label, width - cx)
            self.write(stdscr, y, cx, text, attr)
            self.regions.append(("modetab", y, cx, cx + len(text) - 1, i))
            cx += len(text)
        if cx + 2 <= width:
            self.hline(stdscr, y, cx + 1, width - cx - 1)  # right rule after the chips

    def write_seg(
        self, stdscr: curses.window, y: int, x: int, text: str, attr: int, width: int
    ) -> int:
        if not text or x >= width - 1:
            return x
        clipped = shorten(text, width - x - 1)
        self.write(stdscr, y, x, clipped, attr)
        return x + display_width(clipped)

    @staticmethod
    def machine_crumb(machine: MachineSummary) -> str:
        # Preserve the fleet sigil where the breadcrumb may be the only locator.
        return f"∑ {machine.name}" if machine.fleet else machine.name

    def breadcrumb(self) -> str:
        sep = " › "
        tabs = self.current_tabs()
        tab_name = tabs[self.tab % len(tabs)]
        segs = [self.range_label()]
        # Machine drills are mutually exclusive and need no additional crumb.
        if self.browse_mode == "machines" and self.view != "session":
            machine = self.selected_machine_summary
            segs.append("machines")
            if machine:
                segs.append(self.machine_crumb(machine))
            if self.zoom_model:
                segs.append(self.zoom_model)
            segs.append(tab_name)
            return sep.join(s for s in segs if s)
        if self.browse_mode == "projects" and self.view != "session":
            project = self.selected_project_summary
            segs.append("projects")
            if project:
                segs.append(short_path(project.directory, 34))
            segs.extend(self._drill_crumbs(self.on_sessions_tab or bool(self.zoom_model)))
            segs.append(tab_name)
            return sep.join(s for s in segs if s)
        if self.view == "session":
            if self.browse_mode == "machines":
                machine = self.selected_machine_summary
                if machine:
                    segs.append(self.machine_crumb(machine))
            elif self.browse_mode == "projects":
                project = self.selected_project_summary
                if project:
                    segs.append(short_path(project.directory, 34))
            elif self.focus == "years":
                if self.focused_year:  # month label already carries the year, so only
                    segs.append(self.focused_year)  # show a bare year when that's the scope
            elif self.focused_month:
                segs.append(self.focused_month)
            # Do not leak inherited time-sidebar state into machine breadcrumbs.
            if self.browse_mode == "time" and self.focus == "days" and self.active_day:
                segs.append(self.active_day)
            if self.browse_mode == "time" and self.zoom_project:
                segs.append(short_path(self.zoom_project, 24))
            segs.extend(self._drill_crumbs())
            sess = self.current_session()
            segs.append(shorten(sess.title, 28) if sess else "session")
            segs.append(tab_name)
        elif self.focus == "years":
            if self.focused_year:
                segs.append(self.focused_year)
            if self.zoom_project and (self.on_sessions_tab or self.zoom_model):
                segs.append(short_path(self.zoom_project, 24))
            segs.extend(self._drill_crumbs(self.on_sessions_tab or bool(self.zoom_model)))
            segs.append(tab_name)
        elif self.focus == "months":
            if self.focused_month:
                segs.append(self.focused_month)
            if self.zoom_project and (self.on_sessions_tab or self.zoom_model):
                segs.append(short_path(self.zoom_project, 24))
            segs.extend(self._drill_crumbs(self.on_sessions_tab or bool(self.zoom_model)))
            segs.append(tab_name)
        else:
            if self.focused_month:
                segs.append(self.focused_month)
            if self.active_day:
                segs.append(self.active_day)
            if self.zoom_project and (self.on_sessions_tab or self.zoom_model):
                segs.append(short_path(self.zoom_project, 24))
            segs.extend(self._drill_crumbs(self.on_sessions_tab or bool(self.zoom_model)))
            segs.append(tab_name)
        return sep.join(s for s in segs if s)

    def _drill_crumbs(self, armed: bool = True) -> list[str]:
        # Model membership layers over partition drills, so it is the innermost crumb
        # and the first one Esc removes.
        if not armed:
            return []
        return [c for c in (self.zoom_source, self.zoom_machine, self.zoom_model) if c]

    def draw_footer(self, stdscr: curses.window, height: int, width: int) -> None:
        # Show only contextual actions; omit conventional movement keys to save space.
        if self.filter_active:
            # Accent the input field, not its key hints.
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
                f"   {self._keys('filter', 'up', 'down')} select"
                f"  {self._key('filter', 'confirm')} keep"
                f"  {self._key('filter', 'cancel')} cancel"
                f"  {self._key('filter', 'clear')} clear",
                curses.color_pair(4),
                width,
            )
            return
        # Footer and help share keymap.KEYS so advertised actions cannot diverge.
        # Active toggle segments use the accent; less common actions remain help-only.
        parts: list = keymap.footer_parts(self.app)
        self.hline(stdscr, height - 2, 0, width)
        # Reserve the version slot before drawing the keybar, then paint it last.
        ver = f" v{__version__} "
        if len(ver) + 4 < width:
            self.draw_keybar(stdscr, height - 1, width - len(ver), parts)
            self.write(stdscr, height - 1, width - len(ver), ver, curses.color_pair(1))
        else:
            self.draw_keybar(stdscr, height - 1, width, parts)

    def draw_keybar(self, stdscr: curses.window, y: int, width: int, parts) -> None:
        # Entries may contain contiguous sub-segments so only the active token is accented.
        base = curses.color_pair(4)
        active = curses.color_pair(6) | curses.A_BOLD
        x = 0
        self.write(stdscr, y, x, " ", base)
        x += 1
        for i, part in enumerate(parts):
            segs = part if isinstance(part, list) else [part]
            # Never draw a partial hint.
            if x + (2 if i else 0) + sum(len(t) for t, _ in segs) > width - 1:
                break
            if i:
                self.write(stdscr, y, x, "  ", base)
                x += 2
            for text, on in segs:
                self.write(stdscr, y, x, text, active if on else base)
                x += len(text)

    # Each visible list owns its sort arrow; shared screens must not use effective_sort_by.
    def sort_heading(self, key: str, label: str) -> str:
        if self.session_sort_key() != key:
            return label
        desc = self.sort_descending(key, self.session_sort_reverse())
        return f"{label} {'v' if desc else '^'}"

    def project_sort_heading(self, key: str, label: str) -> str:
        if self.project_sort_key() != key:
            return label
        desc = self.sort_descending(key, self.project_sort_reverse)
        return f"{label} {'v' if desc else '^'}"

    def subagent_sort_heading(self, key: str, label: str) -> str:
        if self.subagent_sort_key() != key:
            return label
        desc = self.sort_descending(key, self.subagent_sort_reverse)
        return f"{label} {'v' if desc else '^'}"

    def trend_sort_heading(self, key: str, label: str, tab: str) -> str:
        # Explicit tab keeps direct rendering independent of overlay selection state.
        if self.app.trend_sort_key(tab) != key:
            return label
        desc = self.sort_descending(key, self.app.trend_sort_reverse_for(tab))
        return f"{label} {'v' if desc else '^'}"

    def _scope_spans_days(self) -> bool:
        return self.browse_mode in ("projects", "machines") or self.focus != "days"

    def session_started(self, workflow: Workflow) -> str:
        return workflow.created_at[:10] if self._scope_spans_days() else workflow.created_at[11:16]

    def session_date_label(self) -> str:
        return "Started" if self._scope_spans_days() else "Time"

    def session_date_column(self) -> tuple[str, str]:
        # Show the timestamp being sorted. "Last act" leaves room for the arrow in
        # the fixed 10-cell field; a longer label shifts every following column.
        if self.session_sort_key() == "last_activity":
            return ("last_activity", "Last act")
        return ("date", self.session_date_label())

    def session_date_cell(self, workflow: Workflow) -> str:
        if self.session_sort_key() != "last_activity":
            return self.session_started(workflow)
        # Activity sort is unavailable in single-day scope, so a date is always required.
        return (workflow.ended_at or workflow.created_at)[:10]

    BOX_HEADER_LINE = 1

    TOP_SESSIONS_LIMIT = 20

    def top_sessions(self, rows: list[Workflow]) -> list[Workflow]:
        ranked = sorted(rows, key=lambda item: (item.total_cost, item.total_tokens), reverse=True)
        return ranked[: self.TOP_SESSIONS_LIMIT]

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
            "Omp": "omp",
            "OpenClaw": "ocl",
            "Zaly": "zy",
        }.get(workflow.source, (workflow.source or "??")[:2].lower())

    def source_tag(self, workflow: Workflow) -> str:
        if not getattr(self.store, "combined", False) or not workflow.source:
            return ""
        return f"[{self._source_abbrev(workflow)}] "

    def bookmark_tag(self, workflow: Workflow) -> str:
        return "★ " if workflow.id in self.bookmarks else ""

    def note_tag(self, workflow: Workflow) -> str:
        return "✎ " if self.note_for(workflow.id) else ""

    def session_marks(self, workflow: Workflow) -> str:
        return self.bookmark_tag(workflow) + self.note_tag(workflow)

    def ignored_session_tag(self, workflow: Workflow) -> str:
        return "ignored: " if workflow.id in self.ignored_sessions else ""

    def session_project(self, workflow: Workflow) -> str:
        root = self.project_root(workflow.directory)
        return root.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or root

    def sessions_span_projects(self) -> bool:
        return self.browse_mode != "projects" and not self.zoom_project

    def src_col(self, workflow: Workflow | None = None) -> str:
        if not getattr(self.store, "combined", False):
            return ""
        if workflow is None:
            return "Hns "
        return f"{self._source_abbrev(workflow):<3} "

    MACHINE_COL_W = 8

    def mach_col(self, workflow: Workflow | None = None) -> str:
        if not self.machines_present:
            return ""
        w = self.MACHINE_COL_W
        if workflow is None:
            return f"{'Machine':<{w}} "
        return f"{pad(shorten(workflow.machine or '?', w), w)} "

    # Preview and picker must use these same builders so entering zoom only adds a cursor.
    SESSION_TITLE_MIN = 24  # room the title keeps before an optional column earns its cells
    SESSION_PROJECT_MAX = 20

    def session_columns(self, sessions: list[Workflow], width: int) -> tuple[bool, int, bool]:
        # Both frames must measure the same pane. Drop Models, Project, then Duration;
        # preserve the title floor because session lists are read by title.
        proj_w = 0
        if self.sessions_span_projects():
            # Cap project width so one deep path cannot consume the title.
            head = self.sort_heading("project", "Project")
            longest = max((display_width(self.session_project(wf)) for wf in sessions), default=0)
            proj_w = max(len(head), min(self.SESSION_PROJECT_MAX, longest))
        title = self.sort_heading("title", "Title")
        for models, proj, dur in (
            (True, proj_w, True),
            (False, proj_w, True),
            (False, 0, True),
            (False, 0, False),
        ):
            if self.view == "zoom" and self.zoom_model:
                # The selected model is already the scope; a Models-count column says
                # nothing and is the first width the attributed session list should shed.
                models = False
            prefix = len(self.session_header_text(models, proj, dur)) - len(title)
            if width - prefix >= self.SESSION_TITLE_MIN:
                return models, proj, dur
        return False, 0, False

    def session_metric_labels(self) -> tuple[str, str]:
        # A model scope attributes the two metric columns to the selected model, so they
        # are renamed. One source for the printed heading AND the click hit-test label:
        # _register_sort_header finds a column by searching the DRAWN header for its
        # label, so a renamed column whose SESSION_SORT_COLUMNS entry still said "Cost"
        # silently lost its sort zone -- the header stayed clickable-looking and did
        # nothing, on the two columns this scope exists to rank by.
        if self.view == "zoom" and bool(self.zoom_model):
            return "Model list", "Model tok"
        return "Cost", "Tokens"

    def session_metric_headings(self) -> tuple[str, str, int, int]:
        model_scope = self.view == "zoom" and bool(self.zoom_model)
        cost_label, token_label = self.session_metric_labels()
        cost = self.sort_heading("cost", cost_label)
        token = self.sort_heading("tokens", token_label)
        return (
            cost,
            token,
            max(10 if model_scope else 9, len(cost)),
            max(9 if model_scope else 8, len(token)),
        )

    def session_header_text(self, models: bool, proj_w: int, dur: bool = True) -> str:
        cost, token, cost_w, token_w = self.session_metric_headings()
        header = f"  {self.sort_heading(*self.session_date_column()):<10} "
        if dur:
            header += f"{self.sort_heading('duration', 'Worked'):>8} "
        header += (
            f"{cost:>{cost_w}} "
            f"{token:>{token_w}} "
            f"{self.sort_heading('subagents', 'Subagents'):>11} "
        )
        if models:
            header += f"{'Models':>6}  "
        header += self.src_col()
        header += self.mach_col()
        if proj_w:
            header += f"{self.sort_heading('project', 'Project'):<{proj_w}}  "
        return header + self.sort_heading("title", "Title")

    def _worked_suffix(self, workflow: Workflow) -> str:
        # Worked time excludes idle waits. Keep the date when activity crosses a day.
        seconds = workflow.worked_seconds
        if seconds is None:
            return ""
        ended = workflow.ended_at
        if not ended:
            return f"   · worked {human_duration(seconds)}"
        until = ended[11:16] if ended[:10] == workflow.created_at[:10] else ended[:16]
        return f"   · worked {human_duration(seconds)} (until {until})"

    def session_duration(self, workflow: Workflow) -> str:
        # Unknown active time stays blank rather than becoming a fake 0s.
        seconds = workflow.worked_seconds
        return human_duration(seconds) if seconds is not None else ""

    def session_row_text(
        self, workflow: Workflow, marker: str, models: bool, proj_w: int, dur: bool = True
    ) -> str:
        if self.view == "zoom" and self.zoom_model:
            usage = self.model_session_usage(workflow.id, self.zoom_model)
            cost, token_count = float(usage["list_cost"]), int(usage["tokens"])
            cost_text = ("~" if usage["estimated"] else "") + money(cost)
        else:
            cost, token_count = workflow.total_cost, workflow.total_tokens
            cost_text = money(cost)
        _cost_head, _token_head, cost_w, token_w = self.session_metric_headings()
        text = f"{marker} {self.session_date_cell(workflow):<10} "
        if dur:
            text += f"{self.session_duration(workflow):>8} "
        text += (
            f"{cost_text:>{cost_w}} "
            f"{human_tokens(token_count):>{token_w}} "
            f"{workflow.subagents:>11} "
        )
        if models:
            text += f"{workflow.model_count:>6}  "
        text += self.src_col(workflow)
        text += self.mach_col(workflow)
        if proj_w:
            text += f"{pad(shorten(self.session_project(workflow), proj_w), proj_w)}  "
        return (
            f"{text}{self.session_marks(workflow)}"
            f"{self.ignored_session_tag(workflow)}{workflow.title}"
        )

    def session_total_text(
        self, sessions: list[Workflow], models: bool, proj_w: int, dur: bool = True
    ) -> str:
        # Sum only quantitative fields. Worked excludes unknown values and stays blank
        # when no backend supplied it.
        worked = [wf.worked_seconds for wf in sessions if wf.worked_seconds is not None]
        if self.view == "zoom" and self.zoom_model:
            usage = self.model_scope_usage(sessions, self.zoom_model)
            total_cost = float(usage["list_cost"])
            total_tokens = int(usage["tokens"])
            cost_text = ("~" if usage["estimated"] else "") + money(total_cost)
        else:
            total_cost = sum(wf.total_cost for wf in sessions)
            total_tokens = sum(wf.total_tokens for wf in sessions)
            cost_text = money(total_cost)
        _cost_head, _token_head, cost_w, token_w = self.session_metric_headings()
        text = f"  {pad('TOTAL', 10)} "
        if dur:
            text += f"{human_duration(sum(worked)) if worked else '':>8} "
        text += (
            f"{cost_text:>{cost_w}} "
            f"{human_tokens(total_tokens):>{token_w}} "
            f"{sum(wf.subagents for wf in sessions):>11} "
        )
        if models:
            text += f"{'':>6}  "
        text += " " * display_width(self.src_col() + self.mach_col())
        if proj_w:
            text += f"{'':<{proj_w}}  "
        return text

    def session_sort_columns(self, proj_w: int, dur: bool = True) -> tuple:
        # Take the two metric labels from the header builder rather than the constant:
        # in a model scope they read "Model list"/"Model tok" and a hard-coded "Cost"
        # is a label no drawn header contains.
        metrics = dict(zip(("cost", "tokens"), self.session_metric_labels()))
        columns = [
            self.session_date_column(),
            *((key, metrics.get(key, label)) for key, label in self.SESSION_SORT_COLUMNS),
        ]
        if dur:
            columns.insert(1, ("duration", "Worked"))  # right after the date cell
        if proj_w:
            columns.insert(-1, ("project", "Project"))  # between Subagents and Title
        return tuple(columns)

    def preview_session_source(self) -> list[Workflow] | None:
        # Preview and picker must widen identically when ignored sessions are visible.
        # None selects the default all_workflows source.
        return self.ranged_workflows if self._showing_ignored_workflows() else None

    def preview_project_source(self) -> list[Workflow] | None:
        # Project preview and picker must widen identically under `i`.
        return self.ranged_workflows if self.show_ignored_projects else None

    def scoped_sessions(self, rows: list[Workflow]) -> list[Workflow]:
        # Aggregate the filtered rows that Enter can actually open.
        return self.filtered_sessions(rows)

    @staticmethod
    def sessions_box_title(sessions: list) -> str:
        return f"Sessions · {len(sessions)}" if sessions else "Sessions"

    @staticmethod
    def projects_box_title(projects: list) -> str:
        return f"Projects · {len(projects)}" if projects else "Projects"

    def session_table(self, rows: list[Workflow], width: int) -> list[str]:
        # Put the title in the border so adding the picker cursor cannot shift rows.
        sessions = self.filtered_sessions(rows)
        inner = max(1, width - self.BOX_CHROME)
        models, proj_w, dur = self.session_columns(sessions, inner)
        header = self.session_header_text(models, proj_w, dur)
        title = self.sessions_box_title(sessions)
        if not sessions:
            lines = self._ruled_box(title, header, ["No sessions."], None, [], width)
        else:
            body = [self.session_row_text(wf, " ", models, proj_w, dur) for wf in sessions]
            total = (
                self.session_total_text(sessions, models, proj_w, dur)
                if len(sessions) > 1
                else None
            )
            lines = self._ruled_box(title, header, body, total, [], width)
        # Register against rendered framed text so gutter offsets match paint.
        self._line_sort_headers[self.BOX_HEADER_LINE] = (
            self.session_sort_columns(proj_w, dur),
            "session",
        )
        return lines

    def unpriced_hint(self) -> str:
        # Do not describe estimated dollars as unbilled.
        if self.show_api_prices:
            return "! estimates — subscription tokens at API list prices"
        return (
            "! $0.00 = subscription tokens — press "
            f"{self._key('main', 'api_prices')} to estimate"
        )

    def line_attr(self, line: str) -> int:
        # Prefixes carry semantic styling: headings, caveats, and dim explanations.
        if line.startswith("# "):
            return curses.color_pair(2) | curses.A_BOLD
        if line.startswith("! "):
            return curses.color_pair(2)
        if line.startswith("▼ "):
            # Match Context-tab compaction styling.
            return curses.color_pair(2) | curses.A_BOLD
        if line.startswith("❄ ") or line.startswith("⚙ "):
            # Cache repurchases are waste, distinct from informational compactions.
            return curses.color_pair(4) | curses.A_BOLD
        if line.startswith("▸ "):
            # A trace's tool call: it labels the block beneath it, so it reads like the
            # box headers do rather than like the prose it introduces.
            return curses.color_pair(6) | curses.A_BOLD
        if line.startswith("✻ ") or line.startswith("→ "):
            # Reasoning and tool output are secondary to the narration between them:
            # bulk you skim, not the voice you read.
            return curses.color_pair(1)
        if line.startswith("· "):
            return curses.color_pair(1)
        if line.startswith("TOTAL"):
            # Unboxed what-if total.
            return curses.A_BOLD
        # Box styling keys off frame glyphs; an ASCII '+' is a title only when text follows.
        first = line[:1]
        if first == "┌" or (first == "+" and line.strip("+- ") != ""):
            return curses.color_pair(2) | curses.A_BOLD
        if first in ("├", "└", "+"):
            return curses.A_NORMAL
        if first in ("│", "|"):
            content = line[2:].lstrip()
            # Reach past box gutters for timeline event styling.
            if content[:1] in ("▼", "❄", "⚙"):
                return self.line_attr(content)
            # Separate counterfactual rows from recorded-cost rows.
            if content.startswith("★"):
                return curses.color_pair(6) | curses.A_BOLD
            # Match TOTAL as a word so names such as "TOTALizer" stay ordinary rows.
            return curses.A_BOLD if content.startswith("TOTAL ") else curses.A_NORMAL
        return curses.A_NORMAL

    def money_attr(self, cost_text: str) -> int:
        # $0.00 means zero/unpriced; <$0.01 is real spend and must remain emphasized.
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

    # Top border, header, rule, bottom border.
    PICKER_CHROME = 4

    def picker_box_width(self, w: int) -> int:
        # Preview and picker must use this same boxed width for optional columns.
        return max(1, w - 4 - self.BOX_CHROME)

    def draw_picker_frame(
        self,
        stdscr: curses.window,
        cy: int,
        x: int,
        w: int,
        title: str,
        header: str,
        nrows: int,
        sort_columns: tuple = (),
        sort_target: str = "",
    ) -> tuple[int, int, int]:
        # Static tables assemble frames up front; scrolling pickers paint the same pieces
        # after sizing their window. Callers must paint exactly `nrows`, because the
        # bottom border is already placed beneath them.
        outer = max(5, w - 4)
        inner = outer - self.BOX_CHROME
        cx = x + 2 + 2
        self.write(
            stdscr, cy, x + 2, self.box_top(title, outer), curses.color_pair(2) | curses.A_BOLD
        )
        self._paint_box_header(stdscr, cy + 1, x + 2, self.box_row(header, outer), outer)
        if sort_columns:
            self._register_sort_header(cy + 1, cx, header, sort_columns, sort_target, inner)
        self.write(stdscr, cy + 2, x + 2, self.box_rule(outer), curses.A_NORMAL)
        self.write(stdscr, cy + 3 + nrows, x + 2, self.box_rule(outer, "bl", "br"), curses.A_NORMAL)
        return cy + 3, cx, inner

    def paint_picker_row(
        self,
        stdscr: curses.window,
        ry: int,
        x: int,
        cx: int,
        inner: int,
        text: str,
        selected: bool,
        cost: str = "",
        token_text: str = "",
        bars: bool = False,
    ) -> None:
        # Selection reverses cells only; gutters retain the table frame.
        g = self.box_glyphs()["v"]
        self.write(stdscr, ry, x + 2, f"{g} ", curses.A_NORMAL)
        self.write(stdscr, ry, cx + inner, f" {g}", curses.A_NORMAL)
        if selected:
            self.paint_cursor_row(
                stdscr, ry, cx, pad(shorten(text, inner), inner), inner, bars=bars
            )
        elif cost or token_text:
            self.write_colored_summary_row(stdscr, ry, cx, text, cost, token_text, inner)
        else:
            self.write_rich(stdscr, ry, cx, pad(shorten(text, inner), inner))

    def draw_sessions_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        sessions = self.current_sessions()
        cy = y + 3
        models, proj_w, dur = self.session_columns(sessions, self.picker_box_width(w))
        header = self.session_header_text(models, proj_w, dur)
        columns = self.session_sort_columns(proj_w, dur)
        if not sessions:
            body_y, cx, inner = self.draw_picker_frame(
                stdscr, cy, x, w, self.sessions_box_title(sessions), header, 1, columns, "session"
            )
            self.paint_picker_row(stdscr, body_y, x, cx, inner, "No sessions.", False)
            return
        visible = max(1, h - 5 - self.PICKER_CHROME)
        idx = max(0, min(self.workflow_index, len(sessions) - 1))
        start = max(0, min(idx - visible // 2, max(0, len(sessions) - visible)))
        shown = sessions[start : start + visible]
        body_y, cx, inner = self.draw_picker_frame(
            stdscr,
            cy,
            x,
            w,
            self.sessions_box_title(sessions),
            header,
            len(shown),
            columns,
            "session",
        )
        self._add_rows_region("session", body_y, x, x + w - 1, start, len(shown))
        for off, wf in enumerate(shown):
            marker = ">" if start + off == idx else " "
            if self.zoom_model:
                usage = self.model_session_usage(wf.id, self.zoom_model)
                cost_text = ("~" if usage["estimated"] else "") + money(float(usage["list_cost"]))
                token_text = human_tokens(int(usage["tokens"]))
            else:
                cost_text = money(wf.total_cost)
                token_text = human_tokens(wf.total_tokens)
            self.paint_picker_row(
                stdscr,
                body_y + off,
                x,
                cx,
                inner,
                self.session_row_text(wf, marker, models, proj_w, dur),
                start + off == idx,
                cost_text,
                token_text,
            )

    def draw_projects_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        projects = self.zoom_projects()
        cy = y + 3
        inner_w = self.picker_box_width(w)
        header = self.project_header_text(inner_w)
        title = self.projects_box_title(projects)
        if not projects:
            body_y, cx, inner = self.draw_picker_frame(
                stdscr, cy, x, w, title, header, 1, self.PROJECT_SORT_COLUMNS, "project"
            )
            self.paint_picker_row(stdscr, body_y, x, cx, inner, "No projects.", False)
            return
        visible = max(1, h - 5 - self.PICKER_CHROME)
        idx = max(0, min(self.project_index, len(projects) - 1))
        start = max(0, min(idx - visible // 2, max(0, len(projects) - visible)))
        shown = projects[start : start + visible]
        body_y, cx, inner = self.draw_picker_frame(
            stdscr, cy, x, w, title, header, len(shown), self.PROJECT_SORT_COLUMNS, "project"
        )
        self._add_rows_region("zoomproject", body_y, x, x + w - 1, start, len(shown))
        for off, project in enumerate(shown):
            marker = ">" if start + off == idx else " "
            self.paint_picker_row(
                stdscr,
                body_y + off,
                x,
                cx,
                inner,
                self.project_row_text(project, marker, inner_w),
                start + off == idx,
                money(project.cost),
                human_tokens(project.tokens),
            )

    def draw_sources_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        self._draw_dimension_picker(
            stdscr, y, x, h, w, self.zoom_source_rows(), self.source_index, "Harness", "zoomsource"
        )

    def draw_machines_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        self._draw_dimension_picker(
            stdscr,
            y,
            x,
            h,
            w,
            self.zoom_machine_rows(),
            self.machine_pick_index,
            "Machine",
            "zoommachine",
        )

    def _draw_dimension_picker(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        h: int,
        w: int,
        rows: list,
        sel_index: int,
        col: str,
        region_kind: str,
    ) -> None:
        cy = y + 3
        inner_w = self.picker_box_width(w)
        title = f"Spend by {col.lower()}"
        if not rows:
            nw, bw = self._group_widths([], col, inner_w)
            body_y, cx, inner = self.draw_picker_frame(
                stdscr, cy, x, w, title, self._group_header(col, nw, bw), 1
            )
            self.paint_picker_row(stdscr, body_y, x, cx, inner, "No sessions in this scope.", False)
            return
        total = sum(float(it["cost"]) for _, it in rows)
        peak = max((float(it["cost"]) for _, it in rows), default=0.0) or 1.0
        namew, barw = self._group_widths(rows, col, inner_w)
        header = self._group_header(col, namew, barw)
        visible = max(1, h - 5 - self.PICKER_CHROME)
        idx = max(0, min(sel_index, len(rows) - 1))
        start = max(0, min(idx - visible // 2, max(0, len(rows) - visible)))
        shown = rows[start : start + visible]
        body_y, cx, inner = self.draw_picker_frame(stdscr, cy, x, w, title, header, len(shown))
        self._add_rows_region(region_kind, body_y, x, x + w - 1, start, len(shown))
        for off, (source, it) in enumerate(shown):
            marker = ">" if start + off == idx else " "
            cost = money(float(it["cost"]))
            tok = human_tokens(int(it["tokens"]))
            self.paint_picker_row(
                stdscr,
                body_y + off,
                x,
                cx,
                inner,
                self._group_row(source, it, marker, namew, barw, peak, total),
                start + off == idx,
                cost,
                tok,
                bars=True,
            )
        if not self.show_api_prices and any(
            float(it["cost"]) == 0 and int(it["tokens"]) for _, it in rows
        ):
            caption = (
                f"· {self._key('main', 'api_prices')} prices subscription/credit "
                "usage at API list rates"
            )
            note_y = body_y + len(shown) + 2  # clear of the box's bottom border
            if note_y < y + h - 1:
                self.write(stdscr, note_y, x + 2, shorten(caption, w - 4), curses.color_pair(1))

    def draw_tabs(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        width: int,
        tabs: tuple[str, ...],
        active_index: int,
        kind: str = "tab",
        center: bool = False,
    ) -> None:
        # Brackets preserve active-tab state in monochrome or pair-starved terminals.
        if width <= 0 or not tabs:
            return
        active_index %= len(tabs)
        labels = [f"[{t}]" if i == active_index else f" {t} " for i, t in enumerate(tabs)]
        sep = "  "
        total = sum(len(lbl) for lbl in labels) + len(sep) * (len(labels) - 1)
        cx = x + max(0, (width - total) // 2) if center and total <= width else x
        remaining = max(0, width - (cx - x))
        for i, label in enumerate(labels):
            if i > 0:
                self.write(stdscr, y, cx, shorten(sep, remaining), curses.A_NORMAL)
                cx += min(len(sep), remaining)
                remaining -= min(len(sep), remaining)
            if remaining <= 0:
                return
            attr = (
                curses.color_pair(7) | curses.A_BOLD
                if i == active_index
                else curses.color_pair(self._TAB_PAIR)
            )
            text = shorten(label, remaining)
            self.write(stdscr, y, cx, text, attr)
            self.regions.append((kind, y, cx, cx + len(text) - 1, i))  # clickable tab
            cx += len(text)
            remaining -= len(text)

    @staticmethod
    def panel_title(number: int, title: str, active: bool = False) -> str:
        return f"[{number}] {title}" + (" ▸" if active else "")

    def draw_year_list(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        self.box(stdscr, y, x, h, w, self.panel_title(1, "Years", active), active=active)
        rows = self.years
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No years in range.", curses.color_pair(1))
            return

        # Exclude the synthetic sum from the bar scale.
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
        self.box(stdscr, y, x, h, w, self.panel_title(2, "Months", active), active=active)
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
        self.box(stdscr, y, x, h, w, self.panel_title(1, "Projects", active), active=active)
        rows = self.projects
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No projects in range.", curses.color_pair(1))
            return

        # Reuse header styling without nesting another frame inside the sidebar panel.
        header = self.project_header_text(w - 2)
        self._paint_box_header(stdscr, y + 1, x + 1, header, w - 2)
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
        self.box(stdscr, y, x, h, w, self.panel_title(0, title), active=active)
        if project is None:
            self.write(stdscr, y + 2, x + 2, "No project selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, self.current_tabs(), self.tab, center=True)

        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Harnesses" and self.view == "zoom":
            self.draw_sources_picker(stdscr, y, x, h, w)
            return
        if current == "Machines" and self.view == "zoom":
            self.draw_machines_picker(stdscr, y, x, h, w)
            return
        if current == "Economics":
            lines = self.model_scope_overview(w - 4)
        elif current == "Overview":
            lines = self.project_overview(project, w - 4)
        elif current == "Harnesses":
            lines = self.project_sources(project, w - 4)
        elif current == "Machines":
            lines = self.project_machines(project, w - 4)
        elif current == "Models":
            lines = self.project_models(project, w - 4)
        else:
            lines = self.project_workflows(project, w - 4)

        self._paint_detail_lines(stdscr, y, x, h, w, lines)

    def draw_machine_list(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        self.box(stdscr, y, x, h, w, self.panel_title(1, "Machines", active), active=active)
        rows = self.machines
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No machines in range.", curses.color_pair(1))
            return
        header = self.machine_header_text(w - 2)
        self._paint_box_header(stdscr, y + 1, x + 1, header, w - 2)
        visible = h - 4
        start = max(0, min(self.machine_index - visible // 2, max(0, len(rows) - visible)))
        self._add_rows_region(
            "machine", y + 3, x, x + w - 1, start, len(rows[start : start + visible])
        )
        for row_y, machine in enumerate(rows[start : start + visible], y + 3):
            selected = start + row_y - (y + 3) == self.machine_index
            text = self.machine_row_text(machine, ">" if selected else " ", w - 2)
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
                self.write_colored_summary_row(
                    stdscr,
                    row_y,
                    x + 1,
                    text,
                    money(machine.cost),
                    human_tokens(machine.tokens),
                    w - 2,
                )

    def draw_machine_detail(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        machine = self.selected_machine_summary
        title = (
            "Machine"
            if machine is None
            else "All machines"
            if machine.fleet
            else f"Machine {shorten(machine.name, max(8, w - 12))}"
        )
        self.box(stdscr, y, x, h, w, self.panel_title(0, title), active=active)
        if machine is None:
            self.write(stdscr, y + 2, x + 2, "No machine selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, self.current_tabs(), self.tab, center=True)

        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Harnesses" and self.view == "zoom":
            self.draw_sources_picker(stdscr, y, x, h, w)
            return
        if current == "Projects" and self.view == "zoom":
            self.draw_projects_picker(stdscr, y, x, h, w)
            return
        if current == "Economics":
            lines = self.model_scope_overview(w - 4)
        elif current == "Overview":
            lines = self.machine_overview(machine, w - 4)
        elif current == "Harnesses":
            lines = self.machine_sources(machine, w - 4)
        elif current == "Models":
            lines = self.machine_models(machine, w - 4)
        elif current == "Projects":
            lines = self.machine_projects(machine, w - 4)
        else:
            lines = self.machine_workflows(machine, w - 4)

        self._paint_detail_lines(stdscr, y, x, h, w, lines)

    def machine_overview(self, machine: MachineSummary, width: int) -> list[str]:
        workflows = self.machine_scope(machine)
        if machine.fleet:
            return self._fleet_overview(machine, workflows, width)
        rows = [
            f"Machine:      {machine.name}",
            f"Status:       {'● live — full drill-in' if machine.live else '○ pulled summary'}",
        ]
        if not machine.live:
            when = iso_to_local(machine.exported_at)
            age = relative_age(machine.exported_at)
            if when:
                rows.append(f"Pulled:       {when}" + (f"  ({age})" if age else ""))
            if machine.opentab_version:
                rows.append(f"opentab:      {machine.opentab_version}")
        rows += [
            f"Cost:         {money(machine.cost)}",
            f"Share:        {pct(machine.cost, self.range_cost_total())}",
            f"Tokens:       {tokens(machine.tokens)}",
            f"Sessions:     {machine.workflows}",
            f"Subagents:    {machine.subagents}",
            f"Last active:  {machine.last_active[:16]}",
        ]
        lines = self._stat_card("# Machine", rows, width)
        if not machine.live:
            lines += [
                "",
                "Summary only — Turns/Tools/Context aren't exported. "
                f"Press {self._key('main', 'refresh_machines')} to re-pull.",
            ]
        elif not self.machines_present:
            # Detail lines clip rather than wrap, so keep this discovery hint short.
            lines += ["", "Only this machine. `opentab --pull HOST` adds another."]
        if machine.live:
            # Pulled summaries contain no per-model rows to decompose.
            lines.append("")
            lines.extend(self._token_economics_box(workflows, width))
        if self.projects_for_workflows(workflows):
            lines.append("")
            lines.extend(self._top_projects_box(workflows, machine.cost, width))
        lines.append("")
        agg = self.aggregate_models(workflows)
        lines.extend(self._model_table(self._agg_rows(agg), "# Top Models", width))
        return lines

    def _fleet_overview(
        self, machine: MachineSummary, workflows: list[Workflow], width: int
    ) -> list[str]:
        # The synthetic fleet row has no per-box status; decompose it by machine instead.
        lines = self._stat_card(
            "# Fleet",
            [
                f"Machines:     {sum(1 for m in self.machines if not m.fleet)}",
                f"Cost:         {money(machine.cost)}",
                f"Tokens:       {tokens(machine.tokens)}",
                f"Sessions:     {machine.workflows}",
                f"Subagents:    {machine.subagents}",
                f"Last active:  {machine.last_active[:16]}",
            ],
            width,
        )
        lines.append("")
        lines.extend(self.machine_table(workflows, width))
        # Decompose the priceable fleet subset even when pulled boxes lack model rows.
        lines.append("")
        lines.extend(self._token_economics_box(workflows, width))
        if self.projects_for_workflows(workflows):
            lines.append("")
            lines.extend(self._top_projects_box(workflows, machine.cost, width))
        lines.append("")
        agg = self.aggregate_models(workflows)
        lines.extend(self._model_table(self._agg_rows(agg), "# Top Models", width))
        return lines

    def machine_models(self, machine: MachineSummary, width: int) -> list[str]:
        agg = self.aggregate_models(self.compose_zoom_drills(self.machine_scope(machine)))
        title = "# Fleet Model Spend" if machine.fleet else "# Machine Model Spend"
        return self._models_tab(self._agg_rows(agg), title, width)

    def machine_sources(self, machine: MachineSummary, width: int) -> list[str]:
        return self.source_table(self.scoped_sessions(self.machine_scope(machine)), width)

    def machine_projects(self, machine: MachineSummary, width: int) -> list[str]:
        return self.project_table(self.projects_for_workflows(self.machine_scope(machine)), width)

    def machine_workflows(self, machine: MachineSummary, width: int) -> list[str]:
        return self.session_table(self.machine_scope(machine), width)

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
        self.box(stdscr, y, x, h, w, self.panel_title(0, title), active=active)
        if year is None:
            self.write(stdscr, y + 2, x + 2, "No year selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, self.current_tabs(), self.tab, center=True)

        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Projects" and self.view == "zoom":
            self.draw_projects_picker(stdscr, y, x, h, w)
            return
        if current == "Harnesses" and self.view == "zoom":
            self.draw_sources_picker(stdscr, y, x, h, w)
            return
        if current == "Machines" and self.view == "zoom":
            self.draw_machines_picker(stdscr, y, x, h, w)
            return
        if current == "Economics":
            lines = self.model_scope_overview(w - 4)
        elif current == "Overview":
            lines = self.year_overview(year, w - 4)
        elif current == "Harnesses":
            lines = self.year_sources(year, w - 4)
        elif current == "Machines":
            lines = self.year_machines(year, w - 4)
        elif current == "Models":
            lines = self.year_models(year, w - 4)
        elif current == "Projects":
            lines = self.year_projects(year, w - 4)
        else:
            lines = self.year_workflows(year, w - 4)

        self._paint_detail_lines(stdscr, y, x, h, w, lines)

    def draw_month_detail(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        month = self.selected_month_summary
        title = "Month" if month is None else f"Month {month.month}"
        self.box(stdscr, y, x, h, w, self.panel_title(0, title), active=active)
        if month is None:
            self.write(stdscr, y + 2, x + 2, "No month selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, self.current_tabs(), self.tab, center=True)

        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Projects" and self.view == "zoom":
            self.draw_projects_picker(stdscr, y, x, h, w)
            return
        if current == "Harnesses" and self.view == "zoom":
            self.draw_sources_picker(stdscr, y, x, h, w)
            return
        if current == "Machines" and self.view == "zoom":
            self.draw_machines_picker(stdscr, y, x, h, w)
            return
        if current == "Economics":
            lines = self.model_scope_overview(w - 4)
        elif current == "Overview":
            lines = self.month_overview(month, w - 4)
        elif current == "Harnesses":
            lines = self.month_sources(month, w - 4)
        elif current == "Machines":
            lines = self.month_machines(month, w - 4)
        elif current == "Models":
            lines = self.month_models(month, w - 4)
        elif current == "Projects":
            lines = self.month_projects(month, w - 4)
        else:
            lines = self.month_workflows(month, w - 4)

        self._paint_detail_lines(stdscr, y, x, h, w, lines)

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
            self.panel_title(3, f"Days · {month}" if month else "Days", active),
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
        self.box(stdscr, y, x, h, w, self.panel_title(0, title), active=active)
        if day is None:
            self.write(stdscr, y + 2, x + 2, "No day selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, self.current_tabs(), self.tab, center=True)

        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Projects" and self.view == "zoom":
            self.draw_projects_picker(stdscr, y, x, h, w)
            return
        if current == "Harnesses" and self.view == "zoom":
            self.draw_sources_picker(stdscr, y, x, h, w)
            return
        if current == "Machines" and self.view == "zoom":
            self.draw_machines_picker(stdscr, y, x, h, w)
            return
        if current == "Economics":
            lines = self.model_scope_overview(w - 4)
        elif current == "Overview":
            lines = self.day_overview(day, w - 4)
        elif current == "Harnesses":
            lines = self.day_sources(day, w - 4)
        elif current == "Machines":
            lines = self.day_machines(day, w - 4)
        elif current == "Projects":
            lines = self.day_projects(day, w - 4)
        else:
            lines = self.day_workflows(day, w - 4)

        self._paint_detail_lines(stdscr, y, x, h, w, lines)

    def draw_detail(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        workflow = self.current_session()
        title = (
            "Detail"
            if workflow is None
            else shorten(self.session_marks(workflow) + workflow.title, max(10, w - 12))
        )
        self.box(stdscr, y, x, h, w, title, active=True)
        if workflow is None:
            self.write(stdscr, y + 2, x + 2, "No session selected.", curses.color_pair(1))
            return
        if not self.app.session_data_ready(workflow.id):
            # Lazy extras may parse an entire backend. Paint first, then let run() prefetch;
            # even current_tabs() is skipped because supports_* may trigger that parse.
            self.app._session_loading = workflow.id
            src = getattr(workflow, "source", "") or self.store.source_name
            self.write(
                stdscr,
                y + 2,
                x + 2,
                f"Loading session — reading {src} records…",
                curses.color_pair(6) | curses.A_BOLD,
            )
            return

        tabs = self.current_tabs()
        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, tabs, self.tab, center=True)

        current = tabs[self.tab % len(tabs)]
        visible = h - 4
        if current == "Subagents":
            lines = self.detail_subagents(workflow, w - 4)
        elif current == "Turns":
            lines = self.detail_turns(workflow, w - 4)
        elif current == "Tools":
            # Budget the treemap's largest chrome form so the first exact row stays visible.
            lines = self.detail_tools(workflow, w - 4, max(0, visible - 12))
        elif current == "Context":
            lines = self.detail_context(workflow, w - 4)
        else:
            lines = self.detail_overview(workflow, w - 4)

        if current == "Turns" and self.app._turn_follow:
            # Follow is one-shot and must run before the scroll clamp.
            self._scroll_turn_cursor_into_view(visible)
            self.app._turn_follow = False
        self.app.scroll = max(0, min(self.app.scroll, max(0, len(lines) - visible)))
        drawn = lines[self.scroll : self.scroll + visible]
        headers = self.box_header_lines(lines) | set(self._line_sort_headers)
        for offset, line in enumerate(drawn):
            attr = self.line_attr(line)
            if current == "Turns" and self.scroll + offset == self._turn_cursor_line:
                # Select by line index, not a display glyph. paint_cursor_row preserves
                # gutters and prevents rich number colors from shredding the highlight.
                self.paint_cursor_row(stdscr, y + 3 + offset, x + 2, line, w - 4)
                continue
            if self.scroll + offset in headers:
                self._paint_box_header(stdscr, y + 3 + offset, x + 2, line, w - 4)
                self._register_line_sort_header(
                    y + 3 + offset, x + 2, self.scroll + offset, line, w - 4
                )
                continue
            if current == "Context":
                # Resolve plain heat levels to curses pairs only at paint time.
                lvl = self._ctx_line_heat.get(self.scroll + offset)
                if lvl == self._CTX_MARK:
                    attr = curses.color_pair(2) | curses.A_BOLD
                elif lvl is not None:
                    attr = curses.color_pair(PRICE_HEAT_BASE_PAIR + lvl) | curses.A_BOLD
            self.write_rich(stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), attr)
            self._paint_token_runs(stdscr, y + 3 + offset, x + 2, line, w - 4)
            if current == "Tools":
                self._paint_tool_tree_runs(
                    stdscr, y + 3 + offset, x + 2, self.scroll + offset, line, w - 4
                )
            self._register_line_sort_header(
                y + 3 + offset, x + 2, self.scroll + offset, line, w - 4
            )
        if current == "Turns":
            self._add_rows_region("turnline", y + 3, x + 2, x + w - 3, self.scroll, len(drawn))

    def _scroll_turn_cursor_into_view(self, visible: int) -> None:
        self._scroll_line_into_view(self._turn_cursor_line, visible)

    def _scroll_line_into_view(self, line: int | None, visible: int) -> None:
        # Do not re-anchor an already visible row.
        if line is None or visible <= 0:
            return
        top = self.app.scroll
        if line < top:
            self.app.scroll = line
        elif line >= top + visible:
            self.app.scroll = line - visible + 1

    def _model_table(
        self,
        rows: list[tuple],
        title: str,
        width: int,
        name_label: str = "Model",
        count_label: str = "Msgs",
        price_split: bool = True,
        selectable: bool = False,
    ) -> list[str]:
        # rows: (name, count, cost, tokens, cache_read, cache_write, output).
        # `selectable` only adds a cursor; browse and zoom must retain identical columns.
        # Size the count column from its TOTAL, which is at least as wide as any row.
        cw_ = max(4, len(count_label), len(str(sum(int(r[1]) for r in rows))) if rows else 0)
        longest = max([len(str(r[0])) for r in rows] + [len(name_label)])
        # Keep the common 2-cell marker gutter so stacked table columns align.
        lead = "  "
        inner = max(1, width - self.BOX_CHROME - len(lead))
        # Wide panes attribute Cost across CacheR/CacheW/Output. Require both recorded
        # dollars and the 20-cell model-name floor; $0 rows have nothing to attribute.
        split = price_split and any(float(r[2]) > 0 for r in rows) and inner - 80 - cw_ >= 20
        # Split cells need two-space gutters; the narrow fallback needs every spare cell.
        sep = "  " if split else " "
        block = 80 if split else 57
        mw = min(longest, max(20, inner - block - cw_))
        total_cost = sum(float(r[2]) for r in rows)
        if split:
            # Split cells use fixed token and dollar sub-columns.
            tail_head = sep.join(f"{h:>6}{'':8}" for h in ("CacheR", "CacheW", "Output"))
        else:
            tail_head = sep.join((f"{'CacheR':>9}", f"{'CacheW':>9}", f"{'Output':>8}"))
        header = (
            f"{lead}{name_label:{mw}}{sep}{count_label:>{cw_}}{sep}{'Cost':>10}{sep}"
            f"{'Share':>5}{sep}{'Tokens':>9}{sep}{tail_head}"
        )
        body = []
        for name, runs, cost, tok, cr, cw, out in rows:
            if split:
                c1, c2, c3 = self._price_split_cells(
                    str(name), float(cost), int(tok), int(cr), int(cw), int(out)
                )
                tail = sep.join((c1, c2, c3))
            else:
                tail = sep.join(
                    (
                        f"{human_tokens(int(cr)):>9}",
                        f"{human_tokens(int(cw)):>9}",
                        f"{human_tokens(int(out)):>8}",
                    )
                )
            body.append(
                f"{lead}{pad(shorten(name, mw), mw)}{sep}{int(runs):>{cw_}}{sep}{money(float(cost)):>10}{sep}"
                f"{pct(float(cost), total_cost):>5}{sep}"
                f"{human_tokens(int(tok)):>9}{sep}{tail}"
            )
        total = None
        if len(rows) > 1:
            # Sum attributed dollars per row at each model's own rates. A single row is
            # already its total, and aggregate Share is definitionally blank.
            truns, ttok, tcr, tcw, tout = (sum(int(r[i]) for r in rows) for i in (1, 3, 4, 5, 6))
            if split:
                dollars = (0.0, 0.0, 0.0)
                for name, _, cost, tok, cr, cw, out in rows:
                    row_d = self._price_split_dollars(
                        str(name), float(cost), int(tok), int(cr), int(cw), int(out)
                    )
                    dollars = tuple(a + b for a, b in zip(dollars, row_d))
                tail = sep.join(self._split_cell(n, d) for n, d in zip((tcr, tcw, tout), dollars))
            else:
                tail = sep.join(
                    (
                        f"{human_tokens(tcr):>9}",
                        f"{human_tokens(tcw):>9}",
                        f"{human_tokens(tout):>8}",
                    )
                )
            total = (
                f"{lead}{pad('TOTAL', mw)}{sep}{truns:>{cw_}}{sep}{money(total_cost):>10}{sep}{'':>5}{sep}"
                f"{human_tokens(ttok):>9}{sep}{tail}"
            )
        notes = []
        if any(str(name).startswith("unknown") for name, *_ in rows):
            notes = [
                "",
                "! unknown (not recorded) means provider/model metadata was not stored for these rows.",
            ]
        # Clamp exactly like App.zoom_selected_model so filtering cannot make highlight
        # and Enter resolve to different rows.
        picked = min(max(0, self.app.model_pick_index), len(body) - 1) if body else 0
        if selectable and body:
            # Preserve a non-color selection cue for screenshots and accessibility.
            body[picked] = ">" + body[picked][1:]
        lines = self._ruled_box(title, header, body, total, notes, width)
        self._model_row_at = {}
        self._model_cursor_line = None
        if selectable and self._ruled_body_start is not None:
            start = self._ruled_body_start
            self._model_row_at = {start + i: i for i in range(len(body))}
            self._model_cursor_line = start + picked
        return lines

    # Content strings cannot use curses ACS; use ASCII when the screen is not UTF-8.
    _TABLE_GLYPHS = {
        "tl": "┌",
        "tr": "┐",
        "bl": "└",
        "br": "┘",
        "lt": "├",
        "rt": "┤",
        "h": "─",
        "v": "│",
    }
    _TABLE_GLYPHS_ASCII = {
        "tl": "+",
        "tr": "+",
        "bl": "+",
        "br": "+",
        "lt": "+",
        "rt": "+",
        "h": "-",
        "v": "|",
    }

    # Static tables and scrolling pickers share these pieces despite building at different
    # times. BOX_CHROME is the width callers must reserve for the frame.

    BOX_CHROME = 4  # "| " + " |"

    @classmethod
    def box_glyphs(cls) -> dict:
        return cls._TABLE_GLYPHS if unicode_screen() else cls._TABLE_GLYPHS_ASCII

    @classmethod
    def box_top(cls, title: str, width: int) -> str:
        # A square titled frame needs at least five cells; paint clips narrower panes.
        g = cls.box_glyphs()
        width = max(5, width)
        heading = shorten(title[2:] if title.startswith("# ") else title, max(1, width - 6))
        prefix = f"{g['tl']} {heading} "
        return prefix + g["h"] * max(0, width - display_width(prefix) - 1) + g["tr"]

    @classmethod
    def box_rule(cls, width: int, left: str = "lt", right: str = "rt") -> str:
        g = cls.box_glyphs()
        return g[left] + g["h"] * max(0, max(5, width) - 2) + g[right]

    @classmethod
    def box_row(cls, text: str, width: int) -> str:
        # Clip inside gutters so narrow panes retain a square frame.
        g = cls.box_glyphs()
        inner = max(5, width) - cls.BOX_CHROME
        return f"{g['v']} {pad(shorten(text, inner), inner)} {g['v']}"

    def _ruled_box(
        self,
        title: str,
        header: str,
        body: list[str],
        total: str | None,
        notes: list[str],
        width: int,
    ) -> list[str]:
        # `width` is outer width. Caveats remain outside the box so line_attr can style
        # them independently; the title and TOTAL are recognized by their framed text.
        lines = [self.box_top(title, width), self.box_row(header, width)]
        self._mark_box_header(header, width)
        self._ruled_body_start = None
        if body:
            lines.append(self.box_rule(width))
            self._ruled_body_start = len(lines)
            lines.extend(self.box_row(b, width) for b in body)
            if total is not None:
                lines.append(self.box_rule(width))
                lines.append(self.box_row(total, width))
        lines.append(self.box_rule(width, "bl", "br"))
        lines.extend(notes)
        return lines

    def _sectioned_box(
        self, title: str, groups: list[list[str]], width: int, notes: list[str]
    ) -> list[str]:
        # Drop empty groups so separators never open onto nothing.
        lines = [self.box_top(title, width)]
        for i, group in enumerate(g2 for g2 in groups if g2):
            if i:
                lines.append(self.box_rule(width))
            lines.extend(self.box_row(row, width) for row in group)
        lines.append(self.box_rule(width, "bl", "br"))
        lines.extend(notes)
        return lines

    @staticmethod
    def _price_split_dollars(
        name: str, cost: float, tok: int, cr: int, cw: int, out: int
    ) -> tuple[float, float, float]:
        # Weight token categories at list rates, then scale to recorded Cost. This is exact
        # for list-price estimates and proportional for historical recorded costs.
        ir, orr, crr, cwr = model_price(name)
        inp = max(0, tok - cr - cw - out)
        raw = (inp * ir, cr * crr, cw * cwr, out * orr)
        total = sum(raw)
        scale = cost / total if cost > 0 and total > 0 else 0.0
        return (raw[1] * scale, raw[2] * scale, raw[3] * scale)

    @staticmethod
    def _split_cell(tokens_n: int, dollars: float) -> str:
        label = f"({money_label(dollars)})" if dollars > 0 else ""
        return f"{human_tokens(tokens_n):>6}{label:>8}"

    @staticmethod
    def _price_split_cells(
        name: str, cost: float, tok: int, cr: int, cw: int, out: int
    ) -> tuple[str, str, str]:
        d = Renderer._price_split_dollars(name, cost, tok, cr, cw, out)
        return (
            Renderer._split_cell(cr, d[0]),
            Renderer._split_cell(cw, d[1]),
            Renderer._split_cell(out, d[2]),
        )

    def _models_tab(self, rows: list[tuple], title: str, width: int) -> list[str]:
        # Filter model names without fuzzy re-ranking; cost order remains meaningful.
        # Zoom adds only a cursor to this same table.
        if self.query:
            rows = [r for r in rows if fuzzy_score(self.query, str(r[0])) is not None]
            if not rows:
                self._model_row_at = {}
                self._model_cursor_line = None
                return [title, f"No models match the filter: {self.query}"]
        return self._model_table(rows, title, width, selectable=self.view == "zoom")

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

    def _paint_token_runs(
        self, stdscr: curses.window, y: int, x: int, line: str, width: int
    ) -> None:
        # Overpaint recorded spans after write_rich; unrelated lines cost one dict miss.
        runs, shift = self._token_runs.get(line), 0
        if runs is None and len(line) > 4 and line[0] in "│|" and line[-1] in "│|":
            # Builders cannot predict their later box offset. Strip exact gutters and
            # padding only; trailing `|` may be real label content.
            runs, shift = self._token_runs.get(line[2:-2].rstrip(" ")), 2
        if not runs:
            return
        for col, length, slot in runs:
            col += shift
            if col >= width:
                break
            self.write(
                stdscr,
                y,
                x + col,
                line[col : col + min(length, width - col)],
                curses.color_pair(TOKEN_SERIES_BASE_PAIR + slot) | curses.A_BOLD,
            )

    def _paint_tool_tree_runs(
        self, stdscr: curses.window, y: int, x: int, index: int, line: str, width: int
    ) -> None:
        # Repaint complete tiles so rich dollar coloring cannot override contrast-safe ink.
        if not self._tool_heat_ok:
            return
        for col, length, level in self._tool_tree_runs.get(index, []):
            if col >= width:
                break
            self.write(
                stdscr,
                y,
                x + col,
                line[col : col + min(length, width - col)],
                curses.color_pair(TOOL_HEAT_BASE_PAIR + level) | curses.A_BOLD,
            )

    def _token_glyph(self, slot: int) -> str:
        # Pair-starved terminals distinguish token types by glyph instead of color.
        return "█" if self._token_series_ok else TOKEN_SERIES_GLYPHS[slot]

    @staticmethod
    def _stack_widths(rows, total: float, cells: int) -> list[int]:
        # Compute geometry once for bars and labels. Cumulative rounding closes the right
        # edge; positive segments get one cell unless they outnumber available cells.
        floor = sum(1 for _, value, _ in rows if value > 0)
        bump = 1
        if floor > cells:
            floor = bump = 0
        room = max(0, cells - floor)
        widths, acc, used = [], 0.0, 0
        for _label, value, _slot in rows:
            if total > 0:
                acc += value / total
            edge = min(room, round(acc * room))
            widths.append(max(0, edge - used) + (bump if value > 0 else 0))
            used = edge
        short = cells - sum(widths)
        if short > 0 and widths:
            widths[widths.index(max(widths))] += short
        return widths

    def _token_stack_line(self, rows, total: float, cells: int, labels=None, share_fmt=None) -> str:
        # Key color runs by text because boxes splice this line at unknown offsets.
        widths = self._stack_widths(rows, total, cells)
        runs: list[tuple[int, int, int]] = []
        text, col = "", 0
        for i, ((_label, value, slot), w) in enumerate(zip(rows, widths)):
            if w <= 0:
                continue
            glyph = self._token_glyph(slot)
            # Flamegraph shares guard near-zero and near-total values; other bars use round.
            share = ""
            if total > 0:
                share = (
                    share_fmt(value / total) if share_fmt else f"{round(100.0 * value / total)}%"
                )
            # Inline labels require surrounding space and color separation. In glyph
            # fallback mode, overwriting fill would remove the only category cue.
            body = glyph * w
            if self._token_series_ok:
                named = f"{labels[i]} {share}".strip() if labels else ""
                for candidate in (named, share):
                    if candidate and len(candidate) + 2 <= w:
                        body = candidate.center(w, glyph)
                        break
            runs.append((col, w, slot))
            text += body
            col += w
        self._token_runs[text] = runs
        return text

    def _token_legend_lines(self, rows, inner: int) -> list[str]:
        # Wrap rather than clip: narrow segments rely most on the legend. Build color runs
        # with each line so wrapped swatches retain their positions.
        lines: list[str] = []
        runs: list[tuple[int, int, int]] = []
        text = ""
        for label, _toks, _cost, slot in rows:
            entry = f"{self._token_glyph(slot)} {label}"
            gap = "  " if text else ""
            if text and len(text) + len(gap) + len(entry) > inner:
                self._token_runs[text] = runs
                lines.append(text)
                text, runs, gap = "", [], ""
            runs.append((len(text) + len(gap), 1, slot))
            text += gap + entry
        if text:
            self._token_runs[text] = runs
            lines.append(text)
        return lines

    def _token_economics_box(
        self, workflows: list[Workflow], width: int, model: str | None = None
    ) -> list[str]:
        # Compare token volume with list-price spend using one color per token type.
        # Recorded cost is not decomposable, so this box always uses list rates.
        econ = self.app.token_economics(workflows, model)
        if econ is None:
            return self._ruled_box(
                "# Token economics", "no priceable usage here", [], None, [], width
            )
        approx = "~" if econ.estimated else ""
        inner = max(1, width - 4)
        # Keep each token type's color stable after cost sorting.
        rows = [
            (label, econ.tokens[i], econ.cost[i], i)
            for i, label in enumerate(TOKEN_TYPES)
            if econ.tokens[i] > 0 or econ.cost[i] > 0
        ]
        rows.sort(key=lambda r: (r[2], r[1]), reverse=True)

        def share_text(value: float, total: float) -> str:
            # Preserve informative sub-percent differences that formatting.pct floors.
            share = 100.0 * value / total if total > 0 else 0.0
            if share >= 10 or share == 0:
                return f"{share:.0f}%"
            if share >= 1:
                return f"{share:.1f}%"
            if share >= 0.005:
                return f"{share:.2f}%"
            return "<0.01%"

        # Below 34 cells, omit ambiguous bars and keep the exact table.
        chart: list[str] = []
        if inner >= 34:
            for caption, index, total, fmt in (
                ("share of tokens sent", 1, econ.total_tokens, lambda v: human_tokens(int(v))),
                ("share of dollars billed", 2, econ.total_cost, money),
            ):
                if chart:
                    chart.append("")
                figure = fmt(total)
                chart.append(caption + " " * max(1, inner - len(caption) - len(figure)) + figure)
                chart.append(
                    self._token_stack_line([(r[0], r[index], r[3]) for r in rows], total, inner)
                )
            chart.append("")
            chart.extend(self._token_legend_lines(rows, inner))

        type_w = max(max((len(label) for label, *_ in rows), default=4), len("TOTAL"))
        cost_w = max(8, *(len(money(c)) for _, _, c, _ in rows), len(money(econ.total_cost)) + 1)
        # Use the common 2-cell table gutter below the flush chart.
        table = [
            f"  {'Type':<{type_w}}  {'Tokens':>8}  {'Volume':>6}  {'Cost':>{cost_w}}  {'Spend':>6}"
        ]
        table += [
            f"  {label:<{type_w}}  {human_tokens(int(toks)):>8}  "
            f"{share_text(toks, econ.total_tokens):>6}  {money(cost):>{cost_w}}  "
            f"{share_text(cost, econ.total_cost):>6}"
            for label, toks, cost, _slot in rows
        ]
        total_row = [
            f"  {'TOTAL':<{type_w}}  {human_tokens(int(econ.total_tokens)):>8}  "
            f"{'':>6}  {approx + money(econ.total_cost):>{cost_w}}"
        ]
        notes = []
        if econ.estimated:
            notes.append(
                "! ~ a model here has no known list rate — its tokens use a generic estimate"
            )
        if econ.missing_cache_rate:
            notes.append(
                "! a model here has no cache-read rate on file — its reads price at $0, "
                "so Cache read is understated"
            )
        if econ.local_tokens:
            notes.append(
                f"! {human_tokens(econ.local_tokens)} local-model tokens excluded — "
                "no API rate to price them at"
            )
        self._mark_box_header(table[0], width)
        title = f"# Token economics · {approx}{money(econ.total_cost)} at list rates"
        return self._sectioned_box(title, [chart, table, total_row], width, notes)

    def model_scope_overview(self, width: int) -> list[str]:
        """Show one selected model's contribution inside the current zoom scope."""
        model = self.zoom_model
        if not model:
            return ["No model selected."]
        workflows = self.current_sessions()
        usage = self.model_scope_usage(workflows, model)
        if is_local_provider(model):
            list_cost = "- (local model)"
        else:
            approx = "~" if usage["estimated"] else ""
            list_cost = approx + money(float(usage["list_cost"]))
        card = min(width, self.CARD_WIDTH)
        lines = self._stat_card(
            "# Model scope",
            [
                f"Model:      {shorten(model, max(20, card - 16))}",
                f"Sessions:   {len(workflows)}",
                f"Messages:   {int(usage['runs'])}",
                f"Tokens:     {human_tokens(int(usage['tokens']))}",
                f"List cost:  {list_cost}",
            ],
            width,
        )
        lines.append("")
        lines.extend(self._token_economics_box(workflows, width, model))
        return lines

    def _top_sessions_box(
        self, workflows: list[Workflow], scope_cost: float, width: int
    ) -> list[str]:
        # A top-N slice has no TOTAL because it would not represent the full scope.
        rows = self.top_sessions(workflows)
        if not rows:
            return self._ruled_box("# Top Sessions", "no sessions in range", [], None, [], width)
        inner = max(1, width - self.BOX_CHROME - 2)  # -2: the shared marker gutter
        # Size numeric columns from data to prevent overflow shifting later columns.
        cost_w = max(10, *(len(money(w.total_cost)) for w in rows))
        subs_w = max(4, *(len(str(w.subagents)) for w in rows))
        prefix = cost_w + 2 + 5 + 2 + 8 + 2 + subs_w + 2  # Cost·Share·Tokens·Subs + gaps
        title_w = max(10, inner - prefix)
        header = f"  {'Cost':>{cost_w}}  {'Share':>5}  {'Tokens':>8}  {'Subs':>{subs_w}}  Title"
        body = [
            f"  {money(w.total_cost):>{cost_w}}  {pct(w.total_cost, scope_cost):>5}  "
            f"{human_tokens(w.total_tokens):>8}  {w.subagents:>{subs_w}}  "
            f"{shorten(self.source_tag(w) + self.session_marks(w) + w.title, title_w)}"
            for w in rows
        ]
        return self._ruled_box("# Top Sessions", header, body, None, [], width)

    def _top_projects_box(
        self, workflows: list[Workflow], scope_cost: float, width: int
    ) -> list[str]:
        # Cost-ranked top-N, independent of Projects-tab sort/filter; no partial TOTAL.
        grouped: dict[str, list[Workflow]] = defaultdict(list)
        for w in workflows:
            grouped[self.project_root(w.directory)].append(w)
        ranked = sorted(
            grouped.items(),
            key=lambda kv: (
                sum(w.total_cost for w in kv[1]),
                sum(w.total_tokens for w in kv[1]),
            ),
            reverse=True,
        )[: self.TOP_SESSIONS_LIMIT]
        if not ranked:
            return self._ruled_box("# Top Projects", "no projects in range", [], None, [], width)
        # Aggregate once so width calculation and rows use identical sums.
        agg = [
            (
                directory,
                sum(w.total_cost for w in ws),
                sum(w.total_tokens for w in ws),
                len(ws),
            )
            for directory, ws in ranked
        ]
        inner = max(1, width - self.BOX_CHROME - 2)  # -2: the shared marker gutter
        cost_w = max(10, *(len(money(cost)) for _, cost, _, _ in agg))
        sess_w = max(4, *(len(str(n)) for _, _, _, n in agg))
        prefix = cost_w + 2 + 5 + 2 + 8 + 2 + sess_w + 2  # Cost·Share·Tokens·Sess + gaps
        path_w = max(10, inner - prefix)
        header = f"  {'Cost':>{cost_w}}  {'Share':>5}  {'Tokens':>8}  {'Sess':>{sess_w}}  Project"
        body = [
            f"  {money(cost):>{cost_w}}  {pct(cost, scope_cost):>5}  "
            f"{human_tokens(toks):>8}  {n:>{sess_w}}  {short_path(directory, path_w)}"
            for directory, cost, toks, n in agg
        ]
        return self._ruled_box("# Top Projects", header, body, None, [], width)

    def month_overview(self, month: MonthSummary, width: int) -> list[str]:
        lines = self._stat_card(
            "# Monthly Insight",
            [
                f"Month:           {month.month}",
                f"Cost:            {money(month.cost)}",
                f"Share of range:  {pct(month.cost, self.range_cost_total())}",
                f"Tokens:          {tokens(month.tokens)}",
                f"Sessions:        {month.workflows}",
                f"Subagents:       {month.subagents}",
                f"Unpriced tokens: {tokens(month.unpriced_tokens)}",
            ],
            width,
            [self.unpriced_hint()] if month.unpriced_tokens else [],
        )
        month_ws = self.workflows_for_month(month.month)
        lines.append("")
        lines.extend(self._token_economics_box(month_ws, width))
        lines.append("")
        lines.extend(self._top_projects_box(month_ws, month.cost, width))
        lines.append("")
        lines.extend(self._top_sessions_box(month_ws, month.cost, width))
        lines.append("")
        agg = self.aggregate_models(month_ws)
        lines.extend(self._model_table(self._agg_rows(agg), "# Top Models", width))
        return lines

    def month_models(self, month: MonthSummary, width: int) -> list[str]:
        agg = self.aggregate_models(self.compose_zoom_drills(self.workflows_for_month(month.month)))
        return self._models_tab(self._agg_rows(agg), "# Monthly Model Spend", width)

    def month_sources(self, month: MonthSummary, width: int) -> list[str]:
        return self.source_table(
            self.scoped_sessions(
                self.workflows_for_month(month.month, self.preview_session_source())
            ),
            width,
        )

    def month_machines(self, month: MonthSummary, width: int) -> list[str]:
        return self.machine_table(
            self.scoped_sessions(
                self.workflows_for_month(month.month, self.preview_session_source())
            ),
            width,
        )

    def month_workflows(self, month: MonthSummary, width: int) -> list[str]:
        return self.session_table(
            self.workflows_for_month(month.month, self.preview_session_source()), width
        )

    def year_overview(self, year: YearSummary, width: int) -> list[str]:
        lines = self._stat_card(
            "# Yearly Insight",
            [
                f"Year:            {year_label(year.year)}",
                f"Cost:            {money(year.cost)}",
                f"Share of range:  {pct(year.cost, self.range_cost_total())}",
                f"Tokens:          {tokens(year.tokens)}",
                f"Sessions:        {year.workflows}",
                f"Subagents:       {year.subagents}",
                f"Unpriced tokens: {tokens(year.unpriced_tokens)}",
            ],
            width,
            [self.unpriced_hint()] if year.unpriced_tokens else [],
        )
        year_ws = self.workflows_for_year(year.year)
        by_month: dict[str, list[Workflow]] = defaultdict(list)
        for w in year_ws:
            by_month[w.created_at[:7]].append(w)
        ranked = sorted(
            by_month, key=lambda m: sum(w.total_cost for w in by_month[m]), reverse=True
        )
        month_rows = []
        for month in ranked:
            ws = by_month[month]
            cost = sum(w.total_cost for w in ws)
            month_rows.append(
                f"  {month:<10} {money(cost):>10} {pct(cost, year.cost):>5} "
                f"{human_tokens(sum(w.total_tokens for w in ws)):>9} "
                f"{len(ws):>4} sess"
            )
        month_total = None
        if len(ranked) > 1:
            month_total = (
                f"  {pad('TOTAL', 10)} {money(year.cost):>10} {'':>5} "
                f"{human_tokens(year.tokens):>9} {year.workflows:>4} sess"
            )
        lines.append("")
        lines.extend(
            self._ruled_box(
                "# Top Months",
                f"  {'Month':<10} {'Cost':>10} {'Share':>5} {'Tokens':>9} {'Sess':>9}",
                month_rows,
                month_total,
                [],
                width,
            )
        )
        lines.append("")
        lines.extend(self._token_economics_box(year_ws, width))
        lines.append("")
        lines.extend(self._top_projects_box(year_ws, year.cost, width))
        lines.append("")
        lines.extend(self._top_sessions_box(year_ws, year.cost, width))
        lines.append("")
        agg = self.aggregate_models(year_ws)
        lines.extend(self._model_table(self._agg_rows(agg), "# Top Models", width))
        return lines

    def year_models(self, year: YearSummary, width: int) -> list[str]:
        agg = self.aggregate_models(self.compose_zoom_drills(self.workflows_for_year(year.year)))
        return self._models_tab(self._agg_rows(agg), "# Yearly Model Spend", width)

    def year_sources(self, year: YearSummary, width: int) -> list[str]:
        return self.source_table(
            self.scoped_sessions(self.workflows_for_year(year.year, self.preview_session_source())),
            width,
        )

    def year_machines(self, year: YearSummary, width: int) -> list[str]:
        return self.machine_table(
            self.scoped_sessions(self.workflows_for_year(year.year, self.preview_session_source())),
            width,
        )

    def year_projects(self, year: YearSummary, width: int) -> list[str]:
        return self.project_table(
            self.projects_for_workflows(
                self.workflows_for_year(year.year, self.preview_project_source()),
                include_ignored=self.show_ignored_projects,
            ),
            width,
        )

    def year_workflows(self, year: YearSummary, width: int) -> list[str]:
        return self.session_table(
            self.workflows_for_year(year.year, self.preview_session_source()), width
        )

    def day_overview(self, day: DaySummary, width: int) -> list[str]:
        lines = self._stat_card(
            "# Day Burn",
            [
                f"Day:             {day.day}",
                f"Cost:            {money(day.cost)}",
                f"Share of range:  {pct(day.cost, self.range_cost_total())}",
                f"Tokens:          {tokens(day.tokens)}",
                f"Sessions:        {day.workflows}",
                f"Subagents:       {day.subagents}",
                f"Unpriced tokens: {tokens(day.unpriced_tokens)}",
            ],
            width,
            [self.unpriced_hint()] if day.unpriced_tokens else [],
        )
        day_ws = self.workflows_for_day(day.day)
        lines.append("")
        lines.extend(self._token_economics_box(day_ws, width))
        lines.append("")
        lines.extend(self._top_sessions_box(day_ws, day.cost, width))
        lines.append("")
        agg = self.aggregate_models(day_ws)
        lines.extend(self._model_table(self._agg_rows(agg), "# Model Mix", width))
        return lines

    def day_sources(self, day: DaySummary, width: int) -> list[str]:
        return self.source_table(
            self.scoped_sessions(self.workflows_for_day(day.day, self.preview_session_source())),
            width,
        )

    def day_machines(self, day: DaySummary, width: int) -> list[str]:
        return self.machine_table(
            self.scoped_sessions(self.workflows_for_day(day.day, self.preview_session_source())),
            width,
        )

    def day_workflows(self, day: DaySummary, width: int) -> list[str]:
        return self.session_table(
            self.workflows_for_day(day.day, self.preview_session_source()), width
        )

    def project_overview(self, project: ProjectSummary, width: int) -> list[str]:
        include_ignored = self.include_ignored_for_project(project)
        workflows = self.workflows_for_project(project.directory, include_ignored=include_ignored)
        share_total = (
            sum(w.total_cost for w in self.ranged_workflows)
            if include_ignored
            else self.range_cost_total()
        )
        card = min(width, self.CARD_WIDTH)
        lines = self._stat_card(
            "# Project Spend",
            [
                f"Project:         {short_path(project.directory, max(20, card - 21))}",
                f"Ignored:         {'yes' if project.ignored else 'no'}",
                f"Cost:            {money(project.cost)}",
                f"Share of range:  {pct(project.cost, share_total)}",
                f"Tokens:          {tokens(project.tokens)}",
                f"Sessions:        {project.workflows}",
                f"Subagents:       {project.subagents}",
                f"Unpriced tokens: {tokens(project.unpriced_tokens)}",
            ],
            width,
            [self.unpriced_hint()] if project.unpriced_tokens else [],
        )
        lines.append("")
        lines.extend(self._token_economics_box(workflows, width))
        lines.append("")
        lines.extend(self._top_sessions_box(workflows, project.cost, width))
        lines.append("")
        agg = self.aggregate_models(workflows)
        lines.extend(self._model_table(self._agg_rows(agg), "# Top Models", width))
        return lines

    def project_models(self, project: ProjectSummary, width: int) -> list[str]:
        agg = self.aggregate_models(
            self.compose_zoom_drills(
                self.workflows_for_project(
                    project.directory,
                    include_ignored=self.include_ignored_for_project(project),
                )
            )
        )
        return self._models_tab(self._agg_rows(agg), "# Project Model Spend", width)

    def project_sources(self, project: ProjectSummary, width: int) -> list[str]:
        return self.source_table(
            self.scoped_sessions(
                self.workflows_for_project(
                    project.directory,
                    include_ignored=self.include_ignored_for_project(project),
                )
            ),
            width,
        )

    def project_machines(self, project: ProjectSummary, width: int) -> list[str]:
        return self.machine_table(
            self.scoped_sessions(
                self.workflows_for_project(
                    project.directory,
                    include_ignored=self.include_ignored_for_project(project),
                )
            ),
            width,
        )

    def project_table(self, rows: list[ProjectSummary], width: int) -> list[str]:
        # The browse preview of a Projects tab: draw_projects_picker's table minus the
        # cursor — same builders, same ruled box, so the picker takes over in place on
        # Enter without a single row shifting.
        inner = max(1, width - self.BOX_CHROME)
        header = self.project_header_text(inner)
        title = self.projects_box_title(rows)
        body = (
            [self.project_row_text(project, " ", inner) for project in rows]
            if rows
            else ["No projects."]
        )
        total = self.project_total_text(rows, inner) if len(rows) > 1 else None
        lines = self._ruled_box(title, header, body, total, [], width)
        self._line_sort_headers[self.BOX_HEADER_LINE] = (self.PROJECT_SORT_COLUMNS, "project")
        return lines

    def month_projects(self, month: MonthSummary, width: int) -> list[str]:
        return self.project_table(
            self.projects_for_workflows(
                self.workflows_for_month(month.month, self.preview_project_source()),
                include_ignored=self.show_ignored_projects,
            ),
            width,
        )

    def day_projects(self, day: DaySummary, width: int) -> list[str]:
        return self.project_table(
            self.projects_for_workflows(
                self.workflows_for_day(day.day, self.preview_project_source()),
                include_ignored=self.show_ignored_projects,
            ),
            width,
        )

    def project_workflows(self, project: ProjectSummary, width: int) -> list[str]:
        return self.session_table(
            self.workflows_for_project(
                project.directory,
                include_ignored=self.include_ignored_for_project(project),
            ),
            width,
        )

    def note_lines(self, workflow: Workflow, width: int) -> list[str]:
        note = self.note_for(workflow.id)
        if not note:
            return []
        # Wrap by display cells so wide Unicode cannot be clipped from the on-screen note.
        wrapped = wrap_cells(note, max(20, width - 12)) or [note]
        return [f"Note:     {wrapped[0]}"] + [f"          {line}" for line in wrapped[1:]]

    def _money_overview(self, workflow: Workflow, width: int) -> list[str]:
        # Keep recorded cost and list-rate what-if in separate sections; their figures have
        # different semantics even when displayed in one card.
        root, total = workflow.root_cost, workflow.total_cost
        sub = total - root
        # Cap label/value cards so wide panes do not strand values far from labels.
        width = min(width, 76)
        inner = max(10, width - 4)

        def kv(label: str, value: str) -> str:
            return f"{label}{value:>{max(1, inner - display_width(label))}}"

        money_rows = []
        # Glyph density keeps the proportion readable without color. Omit undefined or
        # trivial splits for solo and $0 sessions.
        if workflow.subagents and total > 0:
            cells = max(8, min(28, inner - 26))
            rc = max(0, min(cells, round(cells * root / total)))
            bar = "█" * rc + "░" * (cells - rc)
            money_rows.append(kv(f"Root {bar} Sub", f"{pct(root, total)} / {pct(sub, total)}"))
        money_rows += [
            kv("Root", money(root)),
            kv("Subagents", money(sub)),
            kv("Total", money(total)),
            kv("Share of range", pct(total, self.range_cost_total())),
            kv("Tokens", tokens(workflow.total_tokens)),
            kv("Models · Subagents", f"{workflow.model_count} · {workflow.subagents}"),
        ]
        title = "# Money card"
        notes: list[str] = []
        # Session-level what-if must also cover solo sessions with no Subagents table.
        whatif_rows: list[str] = []
        totals = self.whatif_session_totals(workflow)
        if self.whatif_model and totals:
            target = self.whatif_model
            actual, whatif = totals
            delta = whatif - actual
            sign = "+" if delta >= 0 else "-"
            approx = "~" if self.whatif_baseline_is_estimated(workflow) else ""
            title = f"# Money card · what-if {target}"
            whatif_rows = [
                kv("★ Your models (list)", f"{approx}{money(actual)}"),
                kv(f"★ All at {shorten(target, max(4, inner - 22))}", money(whatif)),
                kv(
                    "★ Change",
                    f"{sign}{money(abs(delta))} ({self.signed_pct(delta, actual, sign)})",
                ),
            ]
            notes.append(
                "! What-if sides are list rates — the apples-to-apples basis; recorded "
                "spend above and everywhere else is unchanged."
            )
            if approx:
                notes.append(
                    "! ~ a model in your mix has no known list rate — its tokens use a "
                    "generic estimate, so that baseline is not a real list price."
                )
        if workflow.unpriced_tokens and not whatif_rows:
            notes.append(self.unpriced_hint())
        return self._sectioned_box(title, [money_rows, whatif_rows], width, notes)

    CARD_WIDTH = 76

    def _stat_card(
        self, title: str, rows: list[str], width: int, notes: list[str] = ()
    ) -> list[str]:
        return self._sectioned_box(title, [list(rows)], min(width, self.CARD_WIDTH), list(notes))

    def detail_overview(self, workflow: Workflow, width: int) -> list[str]:
        card = min(width, self.CARD_WIDTH)
        rows = [
            f"ID:       {workflow.id}",
            f"Started:  {workflow.created_at}{self._worked_suffix(workflow)}",
            f"Project:  {short_path(workflow.directory, max(20, card - 14))}",
            f"Title:    {workflow.title}",
        ]
        if workflow.source:
            rows.append(f"Harness:  {workflow.source}")
        if workflow.machine:
            rows.append(f"Machine:  {workflow.machine}")
        rows += self.note_lines(workflow, card - self.BOX_CHROME)
        lines = self._stat_card("# Session", rows, width)
        lines.append("")
        lines += self._money_overview(workflow, width)
        lines.append("")
        lines.extend(self._token_economics_box([workflow], width))
        lines.append("")
        model_rows = self.model_mix(workflow.id)
        lines.extend(self._model_table(self._mix_rows(model_rows), "# Top Models", width))
        return lines

    _FLAME_LEGEND_MAX = 6  # past this the legend is noise; the table below has them all
    _FLAME_MIN_INNER = 30  # below this five segments stop being distinguishable at all

    @staticmethod
    def _flame_pct(frac: float) -> str:
        # Guard both ends so visible parts never read as 0% or make a near-total read 100%.
        # Half-up matches JavaScript Math.round in the web frontend.
        if frac >= 1:
            return "100%"
        if frac <= 0:
            return "0%"
        share = 100.0 * frac
        if share >= 99.5:
            return ">99%"
        if share < 0.5:
            return "<1%"
        return f"{math.floor(share + 0.5):.0f}%"

    @staticmethod
    def _legend_names(segments, with_model: bool = False) -> list[str]:
        # Re-separate names after clipping: duplicate line text collides in _token_runs
        # and would assign both swatches the latter color.
        out: list[str] = []
        used: dict[str, int] = {}
        for seg in segments:
            name = shorten(seg.label, 24)
            if with_model and seg.model:
                name += f" {seg.model}"
            used[name] = seen = used.get(name, 0) + 1
            out.append(name if seen == 1 else f"{name}·{seen}")
        return out

    def _flame_label_line(self, segments, widths, text_of) -> tuple[str, list[int]]:
        # Position labels under their own segments; drop rather than shift oversized text.
        # Reserve one cell between labels and return labeled indices for legend fallback.
        text, runs, done = "", [], []
        for i, (seg, w) in enumerate(zip(segments, widths)):
            if w <= 0:
                continue
            label = str(text_of(seg) or "")
            room = w - 1 if i < len(widths) - 1 else w
            if not label or len(label) > room:
                continue
            col = sum(widths[:i])
            text += " " * (col - len(text)) + label
            runs.append((col, len(label), seg.slot))
            done.append(i)
        if not text:
            return "", []
        self._token_runs[text] = runs
        return text, done

    def _flamegraph_box(self, workflow: Workflow, width: int) -> list[str]:
        # Visualize root/subagent share using App.session_flame, the same values as the
        # table's Cost column. Width falls back to tokens when no costs exist.
        flame = self.app.session_flame(workflow)
        if flame is None or not flame.segments:
            return []
        inner = max(1, width - 4)
        dollars = flame.unit == "cost"

        def fmt(v: float) -> str:
            return money(v) if dollars else human_tokens(int(v))

        approx = "~" if flame.estimated else ""
        kids = flame.children
        own = flame.total - sum(s.value for s in kids)

        # Keep the finding readable when the pane is too narrow for bands.
        parts = [f"root kept {self._flame_pct(flame.self_share)} ({fmt(own)})"]
        if kids:
            parts.append(
                f"{len(kids)} subagent{'s' if len(kids) != 1 else ''} "
                f"split {fmt(sum(s.value for s in kids))}"
            )
            if len(kids) > 1:
                parts.append(
                    f"biggest {shorten(kids[0].agent, 22)} {self._flame_pct(kids[0].share)}"
                )
        else:
            parts = [f"root kept all {approx}{fmt(flame.total)} — no subagent recorded a share"]
        head = [" · ".join(parts)]

        # Place names below the fill so text does not erase color. Require at least one
        # cell per segment; otherwise the headline is more honest than indistinguishable
        # single-cell slices.
        chart: list[str] = []
        named: list[int] = []
        if inner >= max(self._FLAME_MIN_INNER, len(flame.segments)):
            caption = "session · width = " + ("dollars" if dollars else "tokens")
            # State a uniform model once; use positioned labels only for mixed trees.
            if flame.one_model:
                caption += f" · all on {flame.one_model}"
            figure = approx + fmt(flame.total)
            chart.append(caption + " " * max(1, inner - len(caption) - len(figure)) + figure)
            rows = [(s.label, s.value, s.slot) for s in flame.segments]
            widths = self._stack_widths(rows, flame.total, inner)
            chart.append(
                self._token_stack_line(rows, flame.total, inner, share_fmt=self._flame_pct)
            )
            names, named = self._flame_label_line(flame.segments, widths, lambda s: s.agent)
            if names:
                chart.append(names)
            if not flame.one_model:
                models, _ = self._flame_label_line(flame.segments, widths, lambda s: s.model)
                if models:
                    chart.append(models)
            # The legend carries only segments too narrow for positioned names.
            rest = [s for i, s in enumerate(flame.segments) if i not in set(named)]
            if rest:
                chart.append("")
                legend = rest[: self._FLAME_LEGEND_MAX]
                names_ = self._legend_names(legend, with_model=not flame.one_model)
                chart.extend(
                    self._token_legend_lines(
                        list(
                            zip(
                                names_,
                                [0] * len(legend),
                                [0] * len(legend),
                                [s.slot for s in legend],
                            )
                        ),
                        inner,
                    )
                )

        notes = []
        unnamed = len(flame.segments) - len(named)
        if chart and unnamed > self._FLAME_LEGEND_MAX:
            notes.append(
                f"· {unnamed - self._FLAME_LEGEND_MAX} thinner segment"
                f"{'s' if unnamed - self._FLAME_LEGEND_MAX != 1 else ''} left out of the key — "
                "the table below names every execution"
            )
        if not dollars:
            notes.append(
                "! nothing here recorded a cost, so width is TOKENS — press "
                f"{self._key('main', 'api_prices')} to divide list-price dollars instead"
            )
        elif flame.estimated:
            notes.append("! widths include list-price estimates for what recorded no cost")
        if flame.deep:
            # Stores expose depth but not parent identity; show deep nodes as marked
            # siblings rather than inventing tree edges.
            notes.append(
                f"! {flame.deep} execution{'s' if flame.deep != 1 else ''} ran under another "
                "subagent (↳) — shown alongside, since the tree records depth but not parents"
            )
        if flame.silent:
            notes.append(
                f"· {flame.silent} subagent{'s' if flame.silent != 1 else ''} recorded no "
                f"{'spend' if dollars else 'tokens'} — no width to draw, still in the table below"
            )
        return self._sectioned_box(
            f"# Where the money went · {approx}{fmt(flame.total)}",
            [head, chart],
            width,
            notes,
        ) + [""]

    def detail_subagents(self, workflow: Workflow, width: int) -> list[str]:
        nodes = self.session_node_rows(workflow.id)
        if not any(row["depth"] > 0 for row in nodes):
            return ["# Subagents", "No subagents used in this workflow."]
        # Build the chart prefix before registering the table's absolute sort-header line.
        # What-if does not alter the chart's recorded/estimated share.
        head = self._flamegraph_box(workflow, width)
        totals = self.whatif_session_totals(workflow)
        if self.whatif_model and totals:
            # What-if covers the whole tree, including root. Without per-model rows the
            # baseline is unknowable, so retain the ordinary table rather than quote half.
            return self._subagents_whatif(
                self.sorted_subagent_rows(self._priced_nodes(nodes)),
                self.whatif_model,
                totals,
                workflow,
                width,
                head,
            )
        rows = self.sorted_subagent_rows(
            self._priced_nodes([row for row in nodes if row["depth"] > 0])
        )
        header = (
            f"  {self.subagent_sort_heading('date', 'Started'):<16} "
            f"{self.subagent_sort_heading('depth', 'D'):<3} "
            f"{self.subagent_sort_heading('agent', 'Agent'):14} "
            f"{self.subagent_sort_heading('model', 'Model'):31} "
            f"{self.subagent_sort_heading('cost', 'Cost'):>8} "
            f"{self.subagent_sort_heading('tokens', 'Tokens'):>9}  "
            f"{self.subagent_sort_heading('title', 'Title')}"
        )
        body = [
            f"  {str(row.get('created_at') or '')[:16]:<16} "
            f"{row['depth']:<3} "
            f"{pad(shorten(row['agent'], 14), 14)} "
            f"{pad(shorten(row['model_name'], 31), 31)} "
            f"{money(row['cost']):>8} "
            f"{human_tokens(row['tokens_total']):>9}  "
            f"{row['title']}"
            for row in rows
        ]
        total = None
        if len(rows) > 1:
            total = (
                f"  {pad('TOTAL', 16)} {'':<3} {'':14} {'':31} "
                f"{money(sum(row['cost'] for row in rows)):>8} "
                f"{human_tokens(sum(row['tokens_total'] for row in rows)):>9}  "
            )
        box = self._ruled_box("# Subagent Executions", header, body, total, [], width)
        # Sort zones use absolute line indices, so derive the chart-prefix offset.
        self._line_sort_headers[len(head) + self.BOX_HEADER_LINE] = (
            self.SUBAGENT_SORT_COLUMNS,
            "subagent",
        )
        return head + box

    @staticmethod
    def signed_pct(part: float, whole: float, sign: str) -> str:
        # A zero denominator is undefined; do not turn pct()'s "-" into "+-".
        share = pct(abs(part), whole)
        return share if share == "-" else f"{sign}{share}"

    def detail_whatif_summary(self, workflow: Workflow) -> list[str]:
        # Keep the session comparison neutral so it also applies to solo sessions.
        # Both sides come from per-model rows at list rates via whatif_session_totals.
        totals = self.whatif_session_totals(workflow)
        if not totals:
            return []
        target = self.whatif_model
        actual, whatif = totals
        delta = whatif - actual
        sign = "+" if delta >= 0 else "-"
        approx = "~" if self.whatif_baseline_is_estimated(workflow) else ""
        lines = [
            "",
            f"# What-if · {target}",
            f"Your models:  {approx}{money(actual)}   (list rates, each model its own)",
            f"All at {target}:  {money(whatif)}",
            f"Change:       {sign}{money(abs(delta))} "
            f"({self.signed_pct(delta, actual, sign)} vs your models)",
            "! Both sides priced at list rates — the only apples-to-apples basis for a rate "
            "substitution. Recorded spend is unchanged, here and everywhere else.",
        ]
        if approx:
            lines.append(
                "! ~ your models include one with no known list rate — its tokens are priced at a "
                "generic estimate, so the baseline is not a real list price."
            )
        return lines

    def _subagents_whatif(
        self,
        rows: list[dict],
        target: str,
        totals: tuple[float, float],
        workflow: Workflow,
        width: int,
        head: list[str] | None = None,
    ) -> list[str]:
        # What-if is session-scoped; `$` continues to own all app-wide figures. Per-node
        # target cost is exact, but no per-node delta is shown: a node exposes only its
        # dominant model and may have switched models. The exact baseline therefore comes
        # from session per-model rows, with both TOTAL sides priced at list rates.
        priced = [(row, self.whatif_node_price(row, target)) for row in rows]
        prefix = list(head or [])
        header = (
            f"  {self.subagent_sort_heading('date', 'Started'):<16} "
            f"{self.subagent_sort_heading('depth', 'D'):<3} "
            f"{self.subagent_sort_heading('agent', 'Agent'):14} "
            f"{self.subagent_sort_heading('model', 'Model'):26} "
            f"{self.subagent_sort_heading('cost', 'Cost'):>9} "
            f"{'What-if':>9} "
            f"{self.subagent_sort_heading('tokens', 'Tokens'):>9}  "
            f"{self.subagent_sort_heading('title', 'Title')}"
        )
        body = [
            f"  {str(row.get('created_at') or '')[:16]:<16} "
            f"{row['depth']:<3} "
            f"{pad(shorten(row['agent'], 14), 14)} "
            f"{pad(shorten(row['model_name'], 26), 26)} "
            f"{money(row['cost']):>9} "
            f"{money(wi):>9} "
            f"{human_tokens(row['tokens_total']):>9}  "
            f"{row['title']}"
            for row, wi in priced
        ]
        # Do not add a column TOTAL: recorded Cost intentionally differs from the exact
        # list-rate session footer below.
        lines = prefix + self._ruled_box(
            f"# Session Tree · what-if {target}", header, body, None, [], width
        )
        self._line_sort_headers[len(prefix) + self.BOX_HEADER_LINE] = (
            self.SUBAGENT_SORT_COLUMNS,
            "subagent",
        )
        actual, total = totals
        # Sign from the target's point of view.
        saved = actual - total
        verb = "saved" if saved >= 0 else "cost more"
        approx = "~" if self.whatif_baseline_is_estimated(workflow) else ""
        lines += [
            "",
            f"TOTAL (list rates)  your models {approx}{money(actual)} → all at {target} {money(total)}   "
            f"{verb} {money(abs(saved))} ({pct(abs(saved), actual)})",
            "! Both sides priced at list rates — the only apples-to-apples basis. The Cost column is "
            "what was actually recorded ($0 where a subscription recorded none), so it does not add "
            "up to these.",
            "· No per-node Δ: a node can mix models, so its baseline isn't computable — the exact "
            "comparison exists at session level, where the tokens are split per model.",
        ]
        if approx:
            lines.append(
                "! ~ your models include one with no known list rate — its tokens are priced at a "
                "generic estimate, so the baseline is not a real list price."
            )
        # Node and message rollups can disagree; explain only observed mismatches.
        column = sum(wi for _row, wi in priced)
        if abs(column - total) > 0.01:
            # A node rollup may drift in either direction.
            direction = "more" if column > total else "less"
            lines.append(
                "! This session's node totals disagree with its message totals, so the What-if "
                f"column adds up to slightly {direction} than the TOTAL. The TOTAL is the exact one."
            )
        return lines

    @staticmethod
    def _treemap_rects(
        items: list[tuple[str, float]], width: int, height: int
    ) -> list[tuple[str, float, int, int, int, int]]:
        # Match the web's balanced-binary split; integer cuts remain paintable as cells.
        out: list[tuple[str, float, int, int, int, int]] = []

        def place(rows, x: int, y: int, w: int, h: int) -> None:
            if not rows or w <= 0 or h <= 0:
                return
            if len(rows) == 1 or w * h == 1:
                name = rows[0][0] if len(rows) == 1 else "Other"
                out.append((name, sum(value for _, value in rows), x, y, w, h))
                return
            total = sum(value for _, value in rows)
            half = total / 2
            split = min(
                range(1, len(rows)),
                key=lambda i: abs(sum(value for _, value in rows[:i]) - half),
            )
            left, right = rows[:split], rows[split:]
            share = sum(value for _, value in left) / total
            vertical = w >= h
            if vertical and w < 2:
                vertical = False
            elif not vertical and h < 2:
                vertical = True
            if vertical:
                cut = max(1, min(w - 1, round(w * share)))
                place(left, x, y, cut, h)
                place(right, x + cut, y, w - cut, h)
            else:
                cut = max(1, min(h - 1, round(h * share)))
                place(left, x, y, w, cut)
                place(right, x, y + cut, w, h - cut)

        positive = [(name, float(value)) for name, value in items if value > 0]
        place(positive, 0, 0, max(1, width), max(1, height))
        return out

    # Narrower tiles cannot carry a useful label; the exact table still lists them.
    _TOOL_TILE_MIN = 12

    @staticmethod
    def _heat_position(value: float, lo: float, hi: float, levels: int) -> int:
        # Per-call rates span orders of magnitude, so use logarithmic heat. Degenerate
        # ranges stay cool rather than falsely hot.
        if not (hi > lo > 0) or value <= lo:
            return 0
        frac = (math.log(value) - math.log(lo)) / (math.log(hi) - math.log(lo))
        return max(0, min(levels - 1, round(frac * (levels - 1))))

    def _tool_treemap_box(
        self, bucket: dict[str, dict], width: int, max_height: int | None = None
    ) -> list[str]:
        # Area follows visible Cost, falling back to attributed tokens when all costs are $0.
        costs = {name: float(it["cost"]) for name, it in bucket.items()}
        dollars = sum(costs.values()) > 0
        values = costs if dollars else {name: float(it["tokens"]) for name, it in bucket.items()}
        calls = {name: int(it.get("calls") or 0) for name, it in bucket.items()}
        ranked = sorted(
            ((name, value) for name, value in values.items() if value > 0),
            key=lambda row: (-row[1], row[0].lower()),
        )
        if not ranked:
            self._tool_tree_runs = {}
            return []

        inner = max(1, width - 4)
        # Preserve the first exact table row at the 80x20 minimum.
        height = max(3, min(5, inner // 14))
        if max_height is not None:
            height = min(height, max_height)
        if height < 3:
            self._tool_tree_runs = {}
            return []

        # Fold the unlabeled tail into Other; the exact table below retains every tool.
        def fold(keep: int) -> list[tuple[str, float]]:
            head, tail = ranked[:keep], ranked[keep:]
            if not tail:
                return list(head)
            calls["Other"] = sum(calls.get(name, 0) for name, _ in tail)
            out = head + [("Other", sum(value for _, value in tail))]
            out.sort(key=lambda row: (-row[1], row[0].lower()))
            return out

        # Fold from the first tile below the label floor, never based on aggregate tail size.
        grand = sum(value for _, value in ranked)
        keep = 0
        while keep < min(8, len(ranked)):
            if ranked[keep][1] / grand * inner < self._TOOL_TILE_MIN:
                break
            keep += 1
        ranked_all, ranked = ranked, fold(max(1, keep))

        # Area already encodes total cost, so shade encodes cost per call when every tool
        # has call counts. Use the full ranking's range so resizing/folding cannot recolor
        # unchanged tools; otherwise shade by area rather than inventing partial rates.
        all_rates = {name: value / calls[name] for name, value in ranked_all if calls.get(name)}
        by_rate = len(all_rates) == len(ranked_all) and max(all_rates.values()) > min(
            all_rates.values()
        )
        rate_lo = min(all_rates.values()) if by_rate else 0.0
        rate_hi = max(all_rates.values()) if by_rate else 0.0
        rates = {name: value / calls[name] for name, value in ranked if calls.get(name)}

        rects = self._treemap_rects(ranked, inner, height)
        total = sum(value for _, value in ranked)
        peak = max(value for _, value in ranked)
        glyphs = "░▒▓█" if unicode_screen() else ".:*#"
        grid = [[" " for _ in range(inner)] for _ in range(height)]
        row_runs: dict[int, list[tuple[int, int, int]]] = defaultdict(list)

        def put(y: int, x: int, text: str, room: int) -> None:
            for i, ch in enumerate(clip(text, room)):
                if x + i < inner:
                    grid[y][x + i] = ch

        def rate_text(rate: float | None) -> str:
            if rate is None:
                return ""
            if not dollars:
                return f"{human_tokens(int(round(rate)))}/call"
            if rate >= 0.01:
                return f"{money(rate)}/call"
            # money() intentionally collapses sub-cent values, but per-call heat needs
            # enough precision to distinguish them.
            return "<$0.0001/call" if rate < 0.0001 else f"${rate:.4f}".rstrip("0") + "/call"

        for name, value, x, y, w, h in rects:
            # Add gutters only between tiles, not against the frame; preserve runt cells.
            tw = w if x + w >= inner else max(1, w - 1)
            th = h if y + h >= height else max(1, h - 1)
            level = (
                self._heat_position(rates[name], rate_lo, rate_hi, TOOL_HEAT_LEVELS)
                if by_rate
                else max(
                    0,
                    min(
                        TOOL_HEAT_LEVELS - 1,
                        round(math.sqrt(value / peak) * (TOOL_HEAT_LEVELS - 1)),
                    ),
                )
            )
            fill = (
                " "
                if self._tool_heat_ok
                else glyphs[min(len(glyphs) - 1, level * len(glyphs) // TOOL_HEAT_LEVELS)]
            )
            for yy in range(y, min(height, y + th)):
                for xx in range(x, min(inner, x + tw)):
                    grid[yy][xx] = fill
                row_runs[yy].append((x, tw, level))

            # Drop name, area, and rate independently as height shrinks.
            inset = 1 if tw >= 4 else 0
            room = tw - inset * 2
            if room >= 4 and th >= 2:
                put(y, x + inset, shorten(name, room), room)
                metric = money(value) if dollars else human_tokens(int(value))
                stat = f"{metric} · {pct(value, total)}"
                if len(stat) <= room:
                    put(y + 1, x + inset, stat, room)
                # Never clip numeric values into different numbers; omit them if they do
                # not fit and let the exact table answer.
                rate = rate_text(rates.get(name))
                n = calls.get(name) or 0
                both = f"{rate} · {n} call{'s' if n != 1 else ''}"
                if th >= 3 and rate:
                    for candidate in (both, rate):
                        if len(candidate) <= room:
                            put(y + 2, x + inset, candidate, room)
                            break

        chart = ["".join(row) for row in grid]
        area_unit = "visible cost" if dollars else "tokens (no recorded cost)"
        caption = (
            f"area = {area_unit} · shade = {'$' if dollars else 'tokens'}/call"
            if by_rate
            else f"area + shade = {area_unit}"
        )
        total_label = money(total) if dollars else f"{human_tokens(int(total))} tokens"

        # Derive the headline from the full ranking so folded expensive-per-call tools
        # remain visible on narrow panes.
        top_name, top_value = ranked_all[0]
        of_what = "the spend" if dollars else "the tokens"
        headline = [
            f"{shorten(top_name, 22)} is "
            f"{pct(top_value, sum(v for _, v in ranked_all))} of {of_what}"
        ]
        if calls.get(top_name):
            headline[0] += f", over {calls[top_name]} calls"
        if len(all_rates) > 1:
            hot = max(all_rates, key=lambda name: all_rates[name])
            if hot != top_name and all_rates[top_name] > 0:
                headline.append(
                    f"priciest per call is {shorten(hot, 22)} at {rate_text(all_rates[hot])}"
                    f" — {all_rates[hot] / all_rates[top_name]:.0f}× {shorten(top_name, 22)}'s"
                )
            elif hot == top_name:
                headline.append(f"and the priciest per call, at {rate_text(all_rates[hot])}")
        notes = []
        if not dollars:
            if self.show_api_prices and not self.store.demo:
                notes.append(
                    "! no tool-attributed tokens here have a list price — area stays TOKENS"
                )
            else:
                notes.append(
                    "! nothing here recorded a cost, so area is TOKENS — press "
                    f"{self._key('main', 'api_prices')} for list-price spend"
                )
        # Stack headline clauses rather than clipping their figures.
        joined = " · ".join(headline)
        boxed = self._sectioned_box(
            f"# Tool-attributed spend · {total_label}",
            [[joined] if len(joined) <= inner else headline, [caption, *chart]],
            width,
            notes,
        )
        # Derive chart offset after box assembly; headline wrapping changes prologue size.
        # Shift runs by the frame's two-cell gutter.
        chart_at = len(boxed) - len(notes) - 1 - len(chart)
        self._tool_tree_runs = {
            chart_at + row: [(col + 2, length, level) for col, length, level in runs]
            for row, runs in row_runs.items()
        }
        return boxed + [""]

    def detail_tools(
        self, workflow: Workflow, width: int, treemap_height: int | None = None
    ) -> list[str]:
        # Attribute each assistant step across its invoked tools. These are turn costs,
        # not tool-output sizes; `$` reprices wholly unpriced rows at list rates.
        if not self.session_supports_tools(workflow.id):
            return [
                "# Tools",
                "This session's tool doesn't record per-tool attribution.",
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
            # Preserve recorded cost; only wholly unpriced rows receive list-price estimates.
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

        lines = self._tool_treemap_box(by_tool, width, treemap_height)
        lines += self._model_table(
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

    def turn_costs(self, rows) -> list[float]:
        # `$` estimates wholly unpriced turns, including long-TTL cache writes.
        api = self.show_api_prices and not self.store.demo
        out = []
        for r in rows:
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
            out.append(cost)
        return out

    # How much of one event the pane shows before it says how much it is holding back.
    # The store already clipped the text; this is the SECOND cap, and it exists because
    # a 2,000-character tool result is four screens of a pane whose job is to let you
    # scan a turn -- the head is what identifies the output, the rest is why there is a
    # cap at all.
    _TRACE_OUTPUT_LINES = 12
    _TRACE_PARAM_LINES = 6
    _TRACE_VALUE_LINES = 10
    _TRACE_PROSE_LINES = 40

    def detail_turn_trace(self, workflow: Workflow, width: int) -> list[str]:
        """Render one turn's own content: narration, reasoning, and the calls it made.

        The third level under Turns. Turns answers when the money went, the drill which
        calls made up one prompt, and this what one of those calls actually did -- the
        exact arguments included, which is the part no token column can carry.
        """
        rows = self.session_turn_rows(workflow.id)
        idx = self.app.active_trace_drill
        if not rows or idx is None or not 0 <= idx < len(rows):
            return []
        siblings = self.app.drilled_turn_indices()
        if idx not in siblings:
            return []
        row = rows[idx]
        costs = self.turn_costs(rows)
        pos = siblings.index(idx) + 1
        wrap = max(20, width - 2)
        head = (
            f"# Turn {pos} of {len(siblings)} · {shorten(row.get('model_name', '-'), 40)} · "
            f"{(row.get('time') or '--')[5:19]} · {human_tokens(row['tokens_total'])} · "
            f"{money(costs[idx])}"
        )
        if row.get("depth"):
            head += f" · {_turn_agent(row)}"
        lines: list[str] = [head, ""]
        events = self.app.turn_trace_events(workflow.id, row)
        if not events:
            # Distinguish "this turn recorded nothing" from an unsupported backend: the
            # tab only offers this level where the store said it could answer.
            lines.append("  No content recorded for this turn.")
            lines += ["", f"· {self._key('main', 'back')} back to this prompt's turns."]
            return lines
        for event in events:
            kind = event.get("kind")
            if kind == "text":
                lines += self._trace_prose(event, wrap - 2, indent="  ")
            elif kind == "reasoning":
                lines.append("✻ thinking")
                lines += self._trace_prose(event, wrap - 2, indent="  ")
            else:
                lines += self._trace_call(event, wrap)
            lines.append("")
        while lines and not lines[-1]:
            lines.pop()
        lines += ["", f"· {self._key('main', 'back')} back to this prompt's turns."]
        if not self.app.session_records_reasoning(workflow.id):
            # Say WHY the thinking is missing. This harness writes its thinking blocks
            # empty -- only the signed blob survives -- so silence here would read as a
            # parsing bug on the one thing people most expect to find.
            lines += self._trace_wrapped(
                "· ",
                "This harness records no reasoning text; its thinking blocks are empty.",
                "  ",
                width,
            )
        return lines

    @staticmethod
    def _trace_wrapped(prefix: str, text: str, cont: str, wrap: int) -> list[str]:
        # One logical line: the prefix rides the first row, continuations are indented,
        # and BOTH are charged against the pane. wrap_cells re-joins on single spaces, so
        # the prefix is added here rather than baked into the string handed to it -- built
        # in, an indent comes back stripped and the block loses its shape.
        room = max(4, wrap - display_width(cont if len(cont) > len(prefix) else prefix))
        parts = wrap_cells(text, room) or [""]
        return [prefix + parts[0]] + [cont + p for p in parts[1:]]

    @staticmethod
    def _cell_chunks(text: str, width: int) -> list[str]:
        # Hard-wrap at cell boundaries, preserving EVERY character. wrap_cells is a WORD
        # wrapper: it splits on whitespace and rejoins on single spaces, which is right
        # for prose and wrong for anything quoted -- `printf 'a  b'` comes back claiming
        # to print one space, and a column-aligned table loses its alignment.
        out: list[str] = []
        while text:
            head = clip(text, width) or text[0]
            out.append(head)
            text = text[len(head) :]
        return out or [""]

    def _trace_block(self, text: str, indent: str, wrap: int, limit: int) -> list[str]:
        """Raw transcript lines, verbatim: every space, tab stop and blank line kept.

        This is what makes a patch, a heredoc or an aligned table readable. Overlong
        lines break at a cell boundary rather than being re-flowed, because a shell
        command's characters are the thing the reader came for.
        """
        out: list[str] = []
        lines = text.splitlines() or [""]
        room = max(4, wrap - display_width(indent))
        for raw in lines[:limit]:
            raw = raw.replace("\t", "    ")
            if not raw.strip():
                out.append("")
                continue
            out += [indent + chunk for chunk in self._cell_chunks(raw, room)]
        hidden = len(lines) - limit
        if hidden > 0:
            out.append(f"{indent}… {hidden:,} more line{'' if hidden == 1 else 's'}")
        return out

    def _trace_prose(self, event: dict, wrap: int, indent: str = "  ") -> list[str]:
        out = self._trace_block(event.get("text") or "", indent, wrap, self._TRACE_PROSE_LINES)
        dropped = event.get("dropped") or 0
        if dropped:
            out.append(f"{indent}… {dropped:,} more characters")
        return out

    def _trace_call(self, event: dict, wrap: int) -> list[str]:
        name = short_tool_name(str(event.get("name") or "(unknown)"))
        args = str(event.get("args") or "")
        # Whitespace is NOT collapsed: 48% of real tool calls carry a multi-line argument
        # and 25% a multi-line command (heredocs, patches, a Write's body), and "the exact
        # command that ran" is this view's whole promise. Flattened, a patch becomes one
        # unreadable line and `printf 'a  b'` starts lying about what it printed.
        head = args.splitlines()
        if len(head) == 1:
            # A one-line command rides the marker row, where it reads as a command --
            # broken at a cell boundary, never re-flowed, for the reason above.
            lead = f"▸ {name}  "
            chunks = self._cell_chunks(head[0], max(4, wrap - display_width(lead)))
            out = [lead + chunks[0]] + ["    " + c for c in chunks[1:]]
        else:
            out = [f"▸ {name}"]
            if args:
                out += self._trace_block(args, "  ", wrap, self._TRACE_VALUE_LINES)
        for key, value in (event.get("params") or [])[: self._TRACE_PARAM_LINES]:
            text = str(value)
            if "\n" in text:
                out.append(f"  {key}:")
                out += self._trace_block(text, "    ", wrap, self._TRACE_VALUE_LINES)
            else:
                lead = f"  {key}: "
                chunks = self._cell_chunks(text, max(4, wrap - display_width(lead)))
                out += [lead + chunks[0]] + ["      " + c for c in chunks[1:]]
        extra = len(event.get("params") or []) - self._TRACE_PARAM_LINES
        if extra > 0:
            out.append(f"  … {extra} more argument{'' if extra == 1 else 's'}")
        out += self._trace_output(event, wrap)
        return out

    def _trace_output(self, event: dict, wrap: int) -> list[str]:
        output = event.get("output") or ""
        if not output:
            return []
        # Collapse runs of blank lines before counting: many tools separate their results
        # with them, and a budget of twelve lines spent on six blanks shows half the
        # output it could.
        shown: list[str] = []
        for para in output.splitlines():
            if para.strip() or (shown and shown[-1].strip()):
                shown.append(para)
        while shown and not shown[-1].strip():
            shown.pop()
        hidden = max(0, len(shown) - self._TRACE_OUTPUT_LINES)
        # Only the first row carries the arrow: it marks where the result starts, and
        # repeating it would fight the text it introduces.
        body = self._trace_block("\n".join(shown), "  ", wrap, self._TRACE_OUTPUT_LINES)
        out = [("→" + line[1:]) if line[:2] == "  " else line for line in body[:1]] + body[1:]
        tail = []
        if hidden:
            tail.append(f"{hidden:,} more line{'' if hidden == 1 else 's'}")
        if event.get("output_dropped"):
            tail.append(f"{event['output_dropped']:,} more characters")
        if tail:
            # _trace_block already reports the line count when IT truncated; this one is
            # the store's clip on top of that, so replace rather than print both.
            if out and out[-1].lstrip().startswith("…"):
                out.pop()
            out.append("  … " + ", ".join(tail))
        return out

    def detail_turn_drill(self, workflow: Workflow, width: int) -> list[str]:
        """Render one prompt's full text, totals, and turns."""
        rows = self.session_turn_rows(workflow.id)
        if not rows:
            return []
        costs = self.turn_costs(rows)
        groups = self.turn_group_rows(rows, costs)
        i = self.app.active_turn_drill
        if not isinstance(i, int) or not 0 <= i < len(groups):
            return []
        g = groups[i]
        if self.app.active_trace_drill is not None:
            # Clear FIRST: draw_detail lays a turnline region over whatever this returns,
            # so keeping the drill's map would make the trace's prose clickable and
            # highlight a line that is no longer a row.
            self._turn_header_at = {}
            self._turn_cursor_line = None
            traced = self.detail_turn_trace(workflow, width)
            if traced:
                return traced
            self.app.trace_drill = None
        # Rebuilt below for THIS level's rows: draw_detail lays one turnline region over
        # whatever the tab drew, and a stale map would make the prompt text clickable.
        self._turn_header_at = {}
        self._turn_cursor_line = None
        n = i + 1
        order = groups
        share = g["cached"]
        lines: list[str] = [
            f"# Turns · prompt {n} of {len(order)} — {g['turns']} turn"
            f"{'' if g['turns'] == 1 else 's'} · {human_tokens(g['tokens'])} · "
            f"{money(g['cost'])} · cached {'-' if share is None else f'{share * 100:.0f}%'}",
            "",
        ]
        for para in (g["full"] or "(no preceding prompt)").splitlines() or [""]:
            lines += textwrap.wrap(para, max(20, width)) or [""]
        lines.append("")
        idx_w = max(2, len(str(len(rows))))
        # Preserve backend-provided main-agent labels and match web subagent markers.
        agent_w = min(
            12,
            max(5, max((len(_turn_agent(rows[i])) for i in g["indices"]), default=5)),
        )
        inner = max(1, width - self.BOX_CHROME - 2)
        # Gate effort on row data; changing it can invalidate prompt cache as marked above.
        efforts = {i: str(rows[i].get("effort") or "") for i in g["indices"]}
        eff_w = max((len(e) for e in efforts.values()), default=0)
        eff_w = min(7, max(len("Eff"), eff_w)) if any(efforts.values()) else 0
        traceable = self.app.session_supports_trace(workflow.id)
        # Which turns have something to READ. Gated on the ROWS, never on a capability
        # flag, so a backend that records no narration shows no column instead of a
        # stripe of dashes -- and gated on the trace being openable, since a marker
        # pointing at a level you cannot enter is an advertisement for nothing.
        reads = {i: _turn_read_mark(rows[i]) for i in g["indices"]} if traceable else {}
        read_w = (
            max(len("Read"), max(map(len, reads.values()), default=0)) if any(reads.values()) else 0
        )
        fixed = idx_w + agent_w + 14 + 6 + 9 + 9 + 6 + (eff_w + 1 if eff_w else 0)
        # Budgeted against the model column's floor like every other optional cell: added
        # unconditionally it pushed a narrow pane past its width, and the overflow lands
        # on the RIGHT, so an 80-column terminal lost the Cost column to a marker.
        MODEL_MIN = 12
        if read_w and inner - fixed - read_w - 1 < MODEL_MIN:
            read_w = 0
        fixed += read_w + 1 if read_w else 0
        mw = max(MODEL_MIN, min(30, inner - fixed))
        # Gate Tools on row data and a width floor. Tool lists are open-ended, so they use
        # space left after the capped model column and disappear rather than steal it.
        TOOLS_MIN = 10
        labels = {i: tool_call_label(rows[i].get("tools")) for i in g["indices"]}
        tools_w = inner - fixed - mw - 1
        tools_w = tools_w if any(labels.values()) and tools_w >= TOOLS_MIN else 0
        header = (
            f"  {'#':>{idx_w}} {'Time':<14} {pad('Model', mw)} "
            + (f"{pad('Eff', eff_w)} " if eff_w else "")
            + f"{pad('Agent', agent_w)} "
            + (f"{pad('Read', read_w)} " if read_w else "")
            + (f"{pad('Tools', tools_w)} " if tools_w else "")
            + f"{'Cached':>6} {'Tokens':>9} {'Cost':>9}"
        )
        body = []
        for i in g["indices"]:
            r = rows[i]
            sh = cached_share(r)
            body.append(
                f"  {i + 1:>{idx_w}} {(r.get('time') or '--')[5:19]:<14} "
                f"{pad(shorten(r['model_name'], mw), mw)} "
                + (f"{pad(shorten(efforts[i] or '-', eff_w), eff_w)} " if eff_w else "")
                + f"{pad(shorten(_turn_agent(r), agent_w), agent_w)} "
                + (f"{pad(reads.get(i) or '-', read_w)} " if read_w else "")
                + (f"{pad(shorten(labels[i] or '-', tools_w), tools_w)} " if tools_w else "")
                + f"{('-' if sh is None else f'{sh * 100:.0f}%'):>6} "
                f"{human_tokens(r['tokens_total']):>9} {money(costs[i]):>9}"
            )
        totals_row = None
        if len(body) > 1:
            totals_row = (
                f"  {'':>{idx_w}} {pad('TOTAL', 14)} {pad('', mw)} "
                + (f"{pad('', eff_w)} " if eff_w else "")
                + f"{pad('', agent_w)} "
                + (f"{pad(str(sum(1 for v in reads.values() if v)), read_w)} " if read_w else "")
                # pad() does not truncate, so shorten the aggregate tool mix first.
                + (
                    f"{pad(shorten(tool_mix_label([rows[i] for i in g['indices']]), tools_w), tools_w)} "
                    if tools_w
                    else ""
                )
                + f"{'':>6} {human_tokens(g['tokens']):>9} {money(g['cost']):>9}"
            )
        # _ruled_body_start indexes the BOX's own lines, and this pane prints the prompt
        # text above it -- so the prologue is added, never assumed away. Left out, the
        # cursor lit a blank line above the frame and the click map was off by its height.
        prologue = len(lines)
        lines += self._ruled_box(f"# Turns of prompt {n}", header, body, totals_row, [], width)
        if traceable:
            # One body line per turn, so the offset IS the ordinal within this prompt.
            start = prologue + (self._ruled_body_start or 0)
            self._turn_header_at = {start + k: k for k in range(len(body))}
            cur = self.app._trace_cursor
            self._turn_cursor_line = start + cur if 0 <= cur < len(body) else None
        lines += ["", f"· {self._key('main', 'back')} back to the prompts."]
        if read_w:
            lines += self._trace_wrapped(
                "· Read: ",
                "N a turn that narrates, R one that reasons aloud — the turns with "
                "something to read. The rest are tool calls, and the TOTAL counts the "
                "marked ones.",
                "  ",
                width,
            )
        if traceable:
            # The footnote wrapping the tab above uses: continuation is indented, and the
            # indent is reserved inside the width rather than added past it.
            hint = textwrap.wrap(
                f"· {self._key('main', 'select')} (or a click) opens what a turn actually "
                "did — its narration, its reasoning, and each call's arguments.",
                max(20, width - 2),
            )
            lines += hint[:1] + ["  " + piece for piece in hint[1:]]
        return lines

    @staticmethod
    def turn_group_rows(rows, costs):
        """Aggregate consecutive prompt-id runs in chronological order.

        Prompt IDs may recur non-consecutively, so downstream identity is list ordinal.
        `cached` uses the first main-thread turn: later turns are warm by construction and
        averaging them would hide whether the prompt initially repurchased context.
        """
        groups: list[dict] = []
        last = object()
        for i, (r, cost) in enumerate(zip(rows, costs)):
            pid = r.get("prompt_id", "")
            if pid != last:
                last = pid
                groups.append(
                    {
                        "id": pid,
                        "title": (r.get("prompt_title") or "").strip(),
                        "full": (r.get("prompt_full") or r.get("prompt_title") or "").strip(),
                        "time": r.get("time") or "",
                        "turns": 0,
                        "tokens": 0,
                        "cost": 0.0,
                        "indices": [],
                        "calls": 0,
                        "subturns": 0,
                        "_rows": [],
                        "_first": None,
                    }
                )
            g = groups[-1]
            g["turns"] += 1
            g["tokens"] += int(r.get("tokens_total") or 0)
            g["cost"] += cost
            g["indices"].append(i)
            # Count through the same sanitizer as labels so the two cannot disagree.
            g["calls"] += len(tool_names(r.get("tools")))
            g["subturns"] += 1 if r.get("depth") else 0
            g["_rows"].append(r)
            # Subagents have separate context windows and cannot represent main-thread cache.
            if not r.get("depth") and g["_first"] is None:
                g["_first"] = r
        for g in groups:
            g["cached"] = cached_share(g["_first"]) if g["_first"] is not None else None
            g["tools"] = tool_mix_label(g["_rows"])
            g["agents"] = agent_mix_label(g.pop("_rows"))
        return groups

    def detail_turns(self, workflow: Workflow, width: int) -> list[str]:
        # Keep prompts chronological because this tab answers when cost accrued. Per-turn
        # detail is drilled separately; `$` reprices wholly unpriced turns at list rates.
        if not self.session_supports_turns(workflow.id):
            return [
                "# Turns",
                "This session's source records no per-turn usage.",
            ]
        rows = self.session_turn_rows(workflow.id)
        if not rows:
            return ["# Turns", "No turns recorded for this session."]
        if self.app.active_turn_drill is not None:
            drilled = self.detail_turn_drill(workflow, width)
            if drilled:
                return drilled
            self.app.turn_drill = None
        costs = self.turn_costs(rows)
        total = sum(costs)
        groups = self.turn_group_rows(rows, costs)

        # Share supports_context_curve with Context: cumulative-delta and synthetic rows
        # cannot interpret a row's cache split as one request. Show only actionable cache
        # misses caused by waiting or changing reasoning effort.
        curve = self.session_supports_context_curve(workflow.id)
        comps = context_compactions(rows) if curve else {}
        misses = cache_misses(rows) if curve else []
        late = {m.index: m for m in misses if m.cause == "waited"}
        switched = {m.index: m for m in misses if m.cause == "reasoning"}
        head = f"# Turns — {len(groups)} prompts · {len(rows)} turns · {money(total)}"
        if comps:
            freed = sum(before - after for before, after in comps.values())
            head += f" · ▼ {len(comps)} compaction{'s' if len(comps) > 1 else ''}"
            head += f", ~{human_tokens(freed)} freed"
        if late:
            burnt = sum(m.cost for m in late.values())
            head += (
                f" · ❄ {len(late)} cache expir{'y' if len(late) == 1 else 'ies'}, {money(burnt)}"
            )
        if switched:
            spent = sum(m.cost for m in switched.values())
            head += f" · ⚙ {len(switched)} effort switch{'' if len(switched) == 1 else 'es'}"
            head += f", {money(spent)}"
        idx_w = max(2, len(str(len(groups))))
        time_w = 11
        turns_w, cached_w, tok_w, cost_w = 5, 6, 8, 9
        # Budget optional columns incrementally so box_row never clips the right edge.
        # Preserve PROMPT_MIN; drop the redundant bar before Cumulative.
        PROMPT_MIN = 20
        inner = max(1, width - self.BOX_CHROME)
        base = idx_w + time_w + turns_w + cached_w + tok_w + cost_w + 8
        # Include each separator in `used`; independent width expressions drift by a cell
        # and cause box_row to clip a column that should have been dropped.
        used = base
        cum_w = 14 if inner - used - 15 >= PROMPT_MIN else 0
        used += cum_w + (1 if cum_w else 0)
        # Show call count only when present; names live in the drill. Size from the TOTAL
        # so unexpectedly large counts cannot shift following columns.
        total_calls = sum(g["calls"] for g in groups)
        calls_w = max(len("Calls"), len(str(total_calls)))
        calls_w = calls_w if total_calls and inner - used - calls_w - 1 >= PROMPT_MIN else 0
        used += calls_w + (1 if calls_w else 0)
        # Gate agent names on actual delegation; unnamed executions are folded upstream.
        AGENTS_MIN, AGENTS_MAX = 12, 22
        # Measure rendered cells including the delegation marker.
        agent_cells = {
            n: ("↳ " + g["agents"] if g["agents"] else "-") for n, g in enumerate(groups)
        }
        agents_w = 0
        if any(g["subturns"] for g in groups):
            agents_w = min(AGENTS_MAX, max(AGENTS_MIN, max(map(len, agent_cells.values()))))
            if inner - used - agents_w - 1 < PROMPT_MIN:
                agents_w = 0
        used += agents_w + (1 if agents_w else 0)
        bar_w = 8 if inner - used - 9 >= PROMPT_MIN + 12 else 0
        used += bar_w + (1 if bar_w else 0)
        fixed = used
        peak = max((g["cost"] for g in groups), default=0.0)
        pw = max(PROMPT_MIN, inner - fixed)

        header = (
            f"  {'#':>{idx_w}} {'Time':<{time_w}} {'Prompt':<{pw}} {'Turns':>{turns_w}} "
            + (f"{'Calls':>{calls_w}} " if calls_w else "")
            + (f"{pad('Agents', agents_w)} " if agents_w else "")
            + f"{'Cached':>{cached_w}} {'Tokens':>{tok_w}} {'Cost':>{cost_w}}"
            + (f" {'':<{bar_w}}" if bar_w else "")
            + (f" {'Cumulative':>{cum_w}}" if cum_w else "")
        )
        cum = 0.0
        body: list[str] = []
        cursor_rows: list[int] = []
        for n, g in enumerate(groups, start=1):
            cum += g["cost"]
            # Marker rows belong inside the chronology immediately before the prompt.
            for i in g["indices"]:
                comp = comps.get(i)
                if comp:
                    before, after = comp
                    when = (rows[i].get("time") or "")[5:16]
                    body.append(
                        f"▼ context compacted before turn {i + 1} · {when} — "
                        f"{human_tokens(before)} → {human_tokens(after)} "
                        f"(~{human_tokens(before - after)} freed)"
                    )
                miss = late.get(i)
                if miss:
                    body.append(
                        f"❄ cache expired — {human_duration(miss.idle)} idle, "
                        f"{human_tokens(miss.repaid)} bought again for {money(miss.cost)} "
                        f"(it lived {human_duration(miss.ttl)})"
                    )
                eff = switched.get(i)
                if eff:
                    # Changing thinking configuration invalidates the cached prefix.
                    body.append(
                        f"⚙ reasoning effort {eff.detail} — the cache went with it, "
                        f"{human_tokens(eff.repaid)} bought again for {money(eff.cost)}"
                    )
            share = g["cached"]
            cached = "-" if share is None else f"{share * 100:.0f}%"
            title = " ".join((g["title"] or "").split()) or "(no preceding prompt)"
            cumlabel = f"{money(cum)} · {pct(cum, total)}"
            cursor_rows.append(len(body))
            body.append(
                f"  {n:>{idx_w}} {g['time'][5:16]:<{time_w}} {pad(shorten(title, pw), pw)} "
                f"{g['turns']:>{turns_w}} "
                # "-" means no calls; avoid another zero beside $0.00's unpriced meaning.
                + (f"{(str(g['calls']) if g['calls'] else '-'):>{calls_w}} " if calls_w else "")
                # Keep a non-color delegation cue; main-thread-only rows show explicit absence.
                + (f"{pad(shorten(agent_cells[n - 1], agents_w), agents_w)} " if agents_w else "")
                + f"{cached:>{cached_w}} "
                f"{human_tokens(g['tokens']):>{tok_w}} {money(g['cost']):>{cost_w}}"
                + (f" {cost_bar(g['cost'], peak, bar_w)}" if bar_w else "")
                + (f" {cumlabel:>{cum_w}}" if cum_w else "")
            )
        totals_row = None
        if len(groups) > 1:
            # Cached is a per-prompt ratio, not an additive quantity.
            totals_row = (
                f"  {'':>{idx_w}} {pad('TOTAL', time_w)} {'':<{pw}} "
                f"{sum(g['turns'] for g in groups):>{turns_w}} "
                + (f"{sum(g['calls'] for g in groups):>{calls_w}} " if calls_w else "")
                # pad() does not truncate, so shorten the aggregate agent mix first.
                + (
                    f"{pad(shorten('↳ ' + agent_mix_label(rows), agents_w), agents_w)} "
                    if agents_w
                    else ""
                )
                + f"{'':>{cached_w}} "
                f"{human_tokens(sum(g['tokens'] for g in groups)):>{tok_w}} "
                f"{money(total):>{cost_w}}"
            )
        lines = self._ruled_box(head, header, body, totals_row, [], width)
        # Rebase click maps from the box's derived body start, never a counted prologue.
        start = self._ruled_body_start or 0
        self._turn_header_at = {start + row: n for n, row in enumerate(cursor_rows)}
        cur = self.app._turn_cursor
        self._turn_cursor_line = start + cursor_rows[cur] if 0 <= cur < len(cursor_rows) else None
        notes = [
            f"· One row per prompt, in time order — {self._key('main', 'select')} "
            "(or a click) opens it with its turns.",
            "· Cached: how much of the context came from the cache when that prompt STARTED "
            "— near 100% is normal, and anything low re-bought what it was missing.",
        ]
        if calls_w:
            notes.append(
                "· Calls: tool calls the prompt made — which tools, and on which turn, "
                "are in the prompt's own view."
            )
        if agents_w:
            notes.append(
                "· ↳ Agents: subagents the prompt delegated to — which turn ran under which "
                "is in the prompt's own view. A prompt the main thread ran alone reads '-'."
            )
        if comps:
            notes.append(
                "· ▼ the context window was cleared before that turn — the Context tab charts it."
            )
        if late:
            notes.append(
                "· ❄ the prompt cache expired while the session sat idle, so that prompt paid "
                "again for context it already had — a faster follow-up would have cost less."
            )
        if switched:
            notes.append(
                "· ⚙ the reasoning effort changed, which changes the request's thinking config "
                "and so drops the cached prefix with it — the next turn re-bought its whole "
                "context. Worth doing, worth doing early."
            )
        # Paint clips, so wrap long notes and indent continuations under the bullet.
        lines.append("")
        for note in notes:
            # Reserve continuation indentation inside the wrapping width.
            wrapped = textwrap.wrap(note, max(20, width - 2)) or [note]
            lines.append(wrapped[0])
            lines += ["  " + piece for piece in wrapped[1:]]
        return lines

    # Fixed chart height and right-aligned y-axis gutter.
    _CTX_CHART_ROWS = 9
    _CTX_GUTTER = 8

    # Negative sentinel keeps compaction markers distinct from nonnegative heat levels.
    _CTX_MARK = -1

    @staticmethod
    def _ctx_heat_level(value: float, window: int) -> int:
        frac = value / window if window > 0 else 0.0
        return max(0, min(PRICE_HEAT_LEVELS - 1, int(frac * PRICE_HEAT_LEVELS)))

    @staticmethod
    def _turn_dt(row: dict) -> datetime | None:
        # Local naive timestamps make DST-crossing display durations approximate. They do
        # not affect accounting; malformed or absent times simply omit enrichments.
        try:
            return datetime.strptime((row.get("time") or "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @staticmethod
    def _ctx_clock(row: dict, multiday: bool) -> str:
        t = row.get("time") or ""
        return t[5:16] if multiday else t[11:16]

    def detail_context(self, workflow: Workflow, width: int) -> list[str]:
        # Measured context is input + cacheRead + cacheWrite per main-thread turn.
        # Composition is an optional chars/4 estimate; unlogged system/tool schemas can
        # only appear as part of the measured first-turn baseline.
        self._ctx_line_heat: dict[int, int] = {}
        rows = self.session_turn_rows(workflow.id)
        main = [r for r in rows if not r.get("depth")]
        pts = [(r, context_size(r)) for r in main]
        pts = [(r, v) for r, v in pts if v > 0]
        if not pts:
            return ["# Context", "No per-turn context usage recorded for this session."]
        vals = [v for _r, v in pts]
        n = len(vals)
        model = pts[-1][0]["model_name"]
        window = model_context_window(model)
        final, peakv, start = vals[-1], max(vals), vals[0]
        peak_i = vals.index(peakv)
        peak_at = peak_i + 1
        # Peak percentage uses its turn's model window. Heat rows have one attribute per
        # line, so mixed-model charts scale to the final window and disclose that choice.
        peak_window = model_context_window(pts[peak_i][0]["model_name"])
        windows = {model_context_window(r["model_name"]) for r, _v in pts}
        # Apply the shared compaction rule to this filtered series so indices match chart columns.
        comps = [
            (j, vals[j - 1], vals[j])
            for j in range(1, n)
            if vals[j - 1] > CONTEXT_COMPACT_FLOOR and vals[j] < vals[j - 1] * CONTEXT_COMPACT_RATIO
        ]
        freed = sum(before - after for _j, before, after in comps)

        lines = [
            f"# Context — {shorten(model, max(16, width - 24))} · {human_tokens(window)} window"
        ]
        bar = cost_bar(final, window, 22)
        lines.append(f"  end  {human_tokens(final):>7}  ▕{bar}▏ {pct(final, window)} of the window")
        self._ctx_line_heat[len(lines) - 1] = self._ctx_heat_level(final, window)
        lines.append(
            f"  peak {human_tokens(peakv):>7}  ({pct(peakv, peak_window)}) at turn {peak_at} · "
            f"session start {human_tokens(start)} · {n} turns"
        )
        # Color peak against its own model window.
        self._ctx_line_heat[len(lines) - 1] = self._ctx_heat_level(peakv, peak_window)
        start_dt, end_dt = self._turn_dt(pts[0][0]), self._turn_dt(pts[-1][0])
        elapsed = (end_dt - start_dt).total_seconds() if start_dt and end_dt else 0.0
        multiday = bool(start_dt and end_dt and start_dt.date() != end_dt.date())
        spent = workflow.total_cost
        if spent > 0:
            rate = f" · ~{money(spent / (elapsed / 3600))}/h" if elapsed >= 60 else ""
            lines.append(f"  spent {money(spent):>8}  ~{money(spent / n)}/turn{rate}")
        if elapsed >= 1:
            lines.append(
                f"  over  {human_duration(elapsed):>8}  "
                f"{self._ctx_clock(pts[0][0], multiday)} → {self._ctx_clock(pts[-1][0], multiday)}"
            )
        if comps:
            lines.append(
                f"  compacted {len(comps)}× — freed ~{human_tokens(freed)} of context along the way"
            )
            self._ctx_line_heat[len(lines) - 1] = self._CTX_MARK
        if len(windows) > 1:
            lines.append(
                "! this session switched between models with different windows — the "
                f"chart and heat scale to the last one ({human_tokens(window)})"
            )
        lines.append("")

        # Bucket by max when turns outgrow the pane, preserving peaks and compactions.
        gut, chart_h = self._CTX_GUTTER, self._CTX_CHART_ROWS
        plot_w = max(10, width - gut - 1)
        rep = max(1, min(4, plot_w // n))
        cols = min(plot_w, n * rep)
        ymax = float(peakv)

        def bucket(c: int) -> int:
            lo = c * n // cols
            hi = max(lo + 1, (c + 1) * n // cols)
            return max(vals[lo:hi])

        colv = [bucket(c) for c in range(cols)]
        if comps:
            marker = [" "] * cols
            for j, _b, _a in comps:
                marker[min(cols - 1, j * cols // n)] = "▼"
            lines.append(" " * (gut + 1) + "".join(marker))
            self._ctx_line_heat[len(lines) - 1] = self._CTX_MARK
        for r in range(chart_h):
            cells = []
            for c in range(cols):
                eighths = round(colv[c] / ymax * chart_h * 8)
                filled = max(0, min(8, eighths - (chart_h - 1 - r) * 8))
                cells.append("█" if filled >= 8 else BLOCKS_UP[filled])
            if r == 0:
                ylab = human_tokens(int(ymax))
            elif r == chart_h // 2:
                ylab = human_tokens(int(ymax * (chart_h - chart_h // 2) / chart_h))
            else:
                ylab = ""
            axis = "┤" if ylab else "│"
            lines.append(f"{ylab:>{gut}}{axis}" + "".join(cells))
            band_mid = ymax * (chart_h - r - 0.5) / chart_h
            self._ctx_line_heat[len(lines) - 1] = self._ctx_heat_level(band_mid, window)
        lines.append(" " * gut + "└" + "─" * cols)
        # Add edge clocks only when they fit without obscuring turn indices.
        sc, ec = self._ctx_clock(pts[0][0], multiday), self._ctx_clock(pts[-1][0], multiday)
        xl = f"turn 1 · {sc}" if sc else "turn 1"
        xr = f"{ec} · turn {n}" if ec else str(n)
        if len(xl) + len(xr) + 1 > cols:
            xl, xr = "turn 1", str(n)
        lines.append(" " * (gut + 1) + xl + " " * max(1, cols - len(xl) - len(xr)) + xr)
        for j, before, after in comps[:4]:
            ct = self._turn_dt(pts[j][0])
            when = self._ctx_clock(pts[j][0], multiday)
            into = (
                f" (+{human_duration((ct - start_dt).total_seconds())})" if ct and start_dt else ""
            )
            lines.append(
                f"  ▼ turn {j + 1} · {when}{into} — {human_tokens(before)} → {human_tokens(after)}"
            )
            self._ctx_line_heat[len(lines) - 1] = self._CTX_MARK
        if len(comps) > 4:
            lines.append(f"  ▼ … and {len(comps) - 4} more")
            self._ctx_line_heat[len(lines) - 1] = self._CTX_MARK

        comp_rows = (
            self.session_context_rows(workflow.id)
            if self.session_supports_context(workflow.id)
            else []
        )
        if comp_rows:
            by_cat: dict[str, list[dict]] = {}
            for cr in comp_rows:
                by_cat.setdefault(cr["category"], []).append(cr)
            cats = sorted(
                by_cat.items(), key=lambda kv: sum(x["est_tokens"] for x in kv[1]), reverse=True
            )
            total_est = sum(cr["est_tokens"] for cr in comp_rows)
            top = sum(x["est_tokens"] for x in cats[0][1])
            # Keep category width three cells wider than its indented kind rows.
            kw = max(19, min(34, width - 62))
            cw = kw + 3
            lines += ["", f"# What filled it — ~{human_tokens(total_est)} of content sent"]
            for cat, crs in cats:
                ctot = sum(x["est_tokens"] for x in crs)
                ccount = sum(x["count"] for x in crs)
                cbar = cost_bar(ctot, top, 12)
                lines.append(
                    f"  {pad(cat, cw)} {ccount:>6}× {'~' + human_tokens(ctot):>8}  "
                    f"▕{cbar}▏ {pct(ctot, total_est):>4}"
                )
                kinds = sorted(
                    (x for x in crs if x["kind"]), key=lambda x: x["est_tokens"], reverse=True
                )
                for x in kinds[:6]:
                    lines.append(
                        f"    · {pad(shorten(x['kind'], kw), kw)} {x['count']:>5}× "
                        f"{'~' + human_tokens(x['est_tokens']):>8}  {pct(x['est_tokens'], total_est):>17}"
                    )
                if len(kinds) > 6:
                    rest = sum(x["est_tokens"] for x in kinds[6:])
                    lines.append(
                        f"    · {pad(f'… {len(kinds) - 6} more', kw)} {'':>5}  "
                        f"{'~' + human_tokens(rest):>8}  {pct(rest, total_est):>17}"
                    )
            lines.append(
                f"  {pad('fixed overhead', cw)} {'':>6}  {human_tokens(start):>8}  "
                "measured at turn 1 (system prompt + tools + first prompt)"
            )

        lines += [
            "",
            "· Measured per-turn prompt tokens; green → red = window fullness. Subagents excluded.",
        ]
        if comp_rows:
            lines.append(
                "· What-filled-it is a ~chars/4 estimate of everything sent, compacted or not."
            )
        return lines

    def help_sections(self) -> list[tuple[str, list]]:
        # What the `?` overlay lists, straight off the keymap table: the keys that work
        # HERE (this view, this tab, this overlay) first, then how to move, then the
        # globals. draw_footer reads the same table, so the two can't disagree about
        # what is available.
        return keymap.sections(self.app)

    # No prose block here any more. A keymap lists keys; what a $0.00 means is a fact
    # about the numbers, and it is already said where the numbers are (unpriced_hint,
    # under the tables that carry them) and in full in docs/keys.md. A paragraph nobody
    # reads is worse than no paragraph -- it makes the panel scroll.

    def help_lines(self, inner_w: int) -> list[list[tuple[int, str, int]]]:
        # The key list as paint-ready segment lists ([(dx, text, attr), …] per line, a
        # blank line being []), laid out inside a panel `inner_w` wide. One short line
        # per key, keys right-aligned in their own column (lazygit's shape) so the eye
        # runs down the keys and stops at the one it wants -- anything that needs a
        # paragraph belongs in docs/keys.md, not here.
        sections = self.help_sections()
        key_w = max((len(e.label(self.app)) for _t, rows in sections for e in rows), default=9)
        desc_x = key_w + 2

        head = curses.color_pair(6) | curses.A_BOLD
        rule = curses.color_pair(4)
        key_attr = curses.color_pair(2) | curses.A_BOLD

        lines: list[list[tuple[int, str, int]]] = []
        for title, rows in sections:
            # Centered section title with a rule filling both sides.
            label = f" {title} "
            left = max(1, (inner_w - len(label)) // 2)
            lines.append(
                [
                    (0, "─" * left, rule),
                    (left, label, head),
                    (left + len(label), "─" * max(0, inner_w - left - len(label)), rule),
                ]
            )
            for entry in rows:
                keys = entry.label(self.app)
                lines.append(
                    [
                        (key_w - len(keys), keys, key_attr),
                        (desc_x, shorten(entry.text(self.app), max(8, inner_w - desc_x)), 0),
                    ]
                )
            lines.append([])
        if lines and not lines[-1]:
            lines.pop()  # no trailing blank inside the panel
        return lines

    def help_width(self) -> int:
        # Sized to the longest line it has to print -- the panel is as big as the keys
        # need and no bigger.
        sections = self.help_sections()
        key_w = max((len(e.label(self.app)) for _t, rows in sections for e in rows), default=9)
        desc = max((len(e.text(self.app)) for _t, rows in sections for e in rows), default=20)
        titles = max((len(t) + 4 for t, _rows in sections), default=12)
        return max(key_w + 2 + desc, titles, 52)

    def draw_help(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        # A panel, not a view: it floats centered over whatever is behind it (draw()
        # paints the body first), sized to its own content -- a full-screen box holding
        # six lines is what a manual looks like, not a cheat sheet.
        inner_w = max(20, min(self.help_width(), width - 8))
        lines = self.help_lines(inner_w)
        box_w = inner_w + 4
        box_x = max(0, (width - box_w) // 2)
        avail_h = bottom - y
        box_h = min(avail_h, len(lines) + 3)
        box_y = y + max(0, (avail_h - box_h) // 2)

        # Clear the footprint first (draw_modal's rule) so the view behind doesn't bleed
        # through the gaps between segments.
        for row in range(box_y, box_y + box_h):
            self.write(stdscr, row, box_x, " " * box_w)
        self.box(
            stdscr,
            box_y,
            box_x,
            box_h,
            box_w,
            f"Keys · {self._key('help', 'close')} close",
            active=True,
        )

        visible = max(1, box_h - 3)
        scroll = max(0, min(self.app.help_scroll, max(0, len(lines) - visible)))
        self.app.help_scroll = scroll
        for offset, segments in enumerate(lines[scroll : scroll + visible]):
            row_y = box_y + 1 + offset
            for dx, text, attr in segments:
                self.write(stdscr, row_y, box_x + 2 + dx, text, attr)
        if len(lines) > visible:  # only then is there anything to scroll
            hint = f" {self._keys('help', 'down', 'up')} scroll "
            self.write(
                stdscr,
                box_y + box_h - 1,
                box_x + max(2, box_w - len(hint) - 2),
                hint,
                curses.color_pair(1),
            )

    def price_intro_lines(self) -> list[str]:
        # ONE dim context line above the P overlay's price table (plus a spacer):
        # where the rates come from and what the eff blend means. Deliberately terse
        # -- the overlay chrome (tabs + hint) carries the navigation, and the P help
        # entry documents the long form. Shared by the flat price_table_lines
        # (export/tests) and the navigable draw_prices.
        meta = price_source_meta()
        if meta:
            kind = "refreshed" if meta.get("kind") == "cache" else "bundled"
            source = f"models.dev {(meta.get('fetched_at') or '?')[:10]} ({kind})"
        else:
            source = "no models.dev catalog — fallback rates"
        parts = [source]
        mix = self.app.price_token_mix()
        if mix:
            (inp, out, cr, cw), _total = mix
            parts.append(
                f"eff $/M = list rates at your mix: {inp:.1%} in · {out:.1%} out · {cr:.1%} cacheR · {cw:.1%} cacheW"
            )
            parts.append("~ = no cacheR rate")
        return [" · ".join(parts), ""]

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
        # A share of exactly 0 (a catalog model you've never used) stays blank --
        # thousands of "0%" cells would drown the rows that carry information.
        if entry.share <= 0:
            return " " * self._PRICE_USE_W
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
        # One model's name (★-prefixed when pinned) + eff/use/raw-price cells (no
        # route tag -- that's overlaid dim, and appended by the text path). Every
        # entry carries its resolved price (from the most completely-priced alias;
        # local models are dropped upstream).
        w = self._PRICE_COL_W
        name = f"★ {entry.bare}" if getattr(entry, "pinned", False) else entry.bare
        cells = " ".join(f"{c:>{w}}" for c in self._price_raw_cells(entry))
        return (
            f"{pad(shorten(name, namew), namew)}  "
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
        widest = max(len(e.bare) + (2 if getattr(e, "pinned", False) else 0) for e in entries)
        return min(widest, max(12, width - self._PRICE_BLOCK_W - 3))

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
        # The catalog view appends the models.dev lifecycle flag (alpha/beta/deprecated).
        if self.app.prices_view == "provider":
            return family_label(entry.family)
        tag = self._route_tag(entry.routes)
        status = getattr(entry, "status", "")
        if status and self.app.prices_view == "all":
            tag = f"{tag}·{status}" if tag else status
        return tag

    def _price_render_rows(self, entries) -> list[tuple]:
        # Flatten the ordered entries into drawable rows: ("header", label) before each
        # new group (unless the view is flat), then ("model", entry_index, entry) for
        # each model. Pinned entries always come first (ordered so upstream) under one
        # "★ pinned" header, in every view. The entry_index is the position in
        # `entries`, so the cursor (prices_index) and this list stay in lock-step.
        rows: list[tuple] = []
        grouped = self.app.prices_view in ("family", "provider")
        prev = None
        for i, entry in enumerate(entries):
            pinned = getattr(entry, "pinned", False)
            if pinned and prev is None:
                rows.append(("header", "★ pinned"))
            elif (
                not pinned
                and grouped
                and (prev is None or getattr(prev, "pinned", False) or entry.group != prev.group)
            ):
                rows.append(("header", self._price_group_label(entry.group)))
            rows.append(("model", i, entry))
            prev = entry
        return rows

    def _price_empty_msg(self) -> str:
        if self.query:
            return f"No model prices match the filter: {self.query}"
        if self.app.prices_view == "all":
            return "No models.dev catalog on record — fetch one with r."
        return "No model usage on record yet."

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
        # Trends-style chrome: a plain box title, the view modes as clickable tabs
        # (h/l or a click switches, p still cycles), a short right-aligned hint, and
        # one dim context line -- everything else is table.
        self.box(stdscr, y, 0, bottom - y, width, "Model prices", active=True)
        hint = (
            f"{self._keys('prices', 'tab_prev', 'tab_next')} views · "
            f"{self._keys('prices', 'down', 'up')} · "
            f"{self._key('prices', 'pin')} pin · "
            f"{self._key('prices', 'select')} sessions · "
            f"{self._key('prices', 'filter')} filter · "
            f"{self._key('prices', 'close')} closes"
        )
        labels = tuple(label for _key, label in self.app.prices_views)
        keys = [key for key, _label in self.app.prices_views]
        active = keys.index(self.app.prices_view) if self.app.prices_view in keys else 0
        self.draw_tabs(stdscr, y + 1, 2, width - len(hint) - 4, labels, active, kind="pricetab")
        self.write(stdscr, y + 1, width - len(hint) - 2, hint, curses.color_pair(4))
        inner_w = width - 4
        intro = self.price_intro_lines()
        top = y + 3
        for offset, line in enumerate(intro):
            self.write(
                stdscr, top + offset, 2, shorten(line, inner_w), curses.color_pair(1) | curses.A_DIM
            )
        entries = self.priced_model_entries()
        head_y = top + len(intro)
        if not entries:
            self.write(stdscr, head_y, 2, shorten(self._price_empty_msg(), inner_w))
            return
        namew = self._price_namew(entries, inner_w)
        header = self._price_header(namew)
        self._paint_box_header(stdscr, head_y, 2, header, inner_w)
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
            f"{len(rows)} session(s) · {money(subtotal)} on this model · most spend first",
            f"{'Started':<10} {'Cost':>9} {'Tokens':>8}  {self.src_col()}{'Title'}",
        ]
        for w, cost, tok in rows:
            lines.append(
                f"{w.created_at[:10]:<10} {money(cost):>9} {human_tokens(tok):>8}  "
                f"{self.src_col(w)}{self.session_marks(w)}{w.title}"
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
            f"Model prices · {shorten(model, max(8, width - 30))}",
            active=True,
        )
        hint = (
            f"{self._keys('prices.sessions', 'down', 'up')} scroll · "
            f"{self._key('prices.sessions', 'back')} back · "
            f"{self._key('prices.sessions', 'close')} closes"
        )
        self.write(stdscr, y + 1, width - len(hint) - 2, hint, curses.color_pair(4))
        inner_w = width - 4
        lines = self.price_session_lines(model, inner_w)
        top = y + 2
        if len(lines) == 1:  # the "No sessions used …" case
            self.write(stdscr, top, 2, shorten(lines[0], inner_w))
            return
        self.write(stdscr, top, 2, shorten(lines[0], inner_w), curses.color_pair(4))
        self._paint_box_header(stdscr, top + 1, 2, lines[1], inner_w)
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

    @staticmethod
    def _toast_age(seconds: float) -> str:
        # A compact "how long ago" for the notices log. Toasts store a monotonic birth
        # time, so this is elapsed seconds -- no wall clock, no timezone, just an age.
        if seconds < 1:
            return "now"
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h"
        return f"{int(seconds // 86400)}d"

    def toast_history_lines(self, width: int) -> list[tuple[str, str]]:
        # One row per past notice, NEWEST FIRST -- "<age>  <sigil> <message>" -- each
        # tagged with its kind so draw_toast_history colours it. Returns (text, kind)
        # pairs, so a test can assert the content with no screen. Empty log = one hint row.
        log = self.app.toast_log
        if not log:
            return [("No notifications yet — status messages will collect here.", "info")]
        now = self.toast_now()
        rows: list[tuple[str, str]] = []
        for toast in reversed(log):
            sigil = self.TOAST_STYLE.get(toast.kind, self.TOAST_STYLE["info"])[1]
            age = self._toast_age(max(0.0, now - toast.born))
            rows.append((shorten(f"{age:>4}  {sigil} {toast.text}", width), toast.kind))
        return rows

    def draw_toast_history(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        # The `N` overlay: a pager over the notices scrollback (App.toast_log), floating
        # centered over the view like help -- but sized tall, since the log runs long.
        # Newest first; each row painted in its kind's colour (red errors stay legible in
        # the scrollback too). j/k/g/G/page scroll (handle_key); Esc/q/N close.
        inner_w = max(24, min(76, width - 8))
        rows = self.toast_history_lines(inner_w)
        box_w = inner_w + 4
        box_x = max(0, (width - box_w) // 2)
        avail_h = bottom - y
        box_h = min(avail_h, max(6, len(rows) + 3))
        box_y = y + max(0, (avail_h - box_h) // 2)
        for row in range(box_y, box_y + box_h):  # clear the footprint (draw_modal's rule)
            self.write(stdscr, row, box_x, " " * box_w)
        count = len(self.app.toast_log)
        close = self._key("notices", "close")
        title = (
            f"Notifications ({count}) · {close} close"
            if count
            else f"Notifications · {close} close"
        )
        self.box(stdscr, box_y, box_x, box_h, box_w, title, active=True)
        visible = max(1, box_h - 3)
        scroll = max(0, min(self.app.toast_history_scroll, max(0, len(rows) - visible)))
        self.app.toast_history_scroll = scroll
        for offset, (text, kind) in enumerate(rows[scroll : scroll + visible]):
            pair = self.TOAST_STYLE.get(kind, self.TOAST_STYLE["info"])[0]
            self.write(stdscr, box_y + 1 + offset, box_x + 2, text, curses.color_pair(pair))
        if len(rows) > visible:  # only then is there anything to scroll
            hint = f" {self._keys('notices', 'down', 'up')} scroll "
            self.write(
                stdscr,
                box_y + box_h - 1,
                box_x + max(2, box_w - len(hint) - 2),
                hint,
                curses.color_pair(1),
            )

    def draw_modal(
        self,
        stdscr: curses.window,
        scr_h: int,
        scr_w: int,
        title: str,
        lines: list,
        center: bool = False,
        alert: bool = False,
    ) -> tuple[int, int, int, int]:
        # A small centered popup box floating over the current view (cleared interior so
        # the view doesn't bleed through). `lines` is a list of (text, attr); the caller
        # styles each row (header tint, A_REVERSE for a selected entry). Alert modals use
        # the bad-role border; callers can center their rows instead of picker-aligning them.
        # Returns the box geometry (y, x, h, w) so a caller can post-paint richer rows --
        # the `w` picker lays its tier tab strip over a placeholder line this way.
        content = [(str(t), a) for t, a in lines]
        inner_w = max([len(title) + 2] + [display_width(t) for t, _ in content] + [16])
        w = min(inner_w + 4, max(24, scr_w - 4))
        h = min(len(content) + 4, max(6, scr_h - 4))
        y = max(1, (scr_h - h) // 2)
        x = max(1, (scr_w - w) // 2)
        for row in range(y, y + h):  # clear the footprint first
            self.write(stdscr, row, x, " " * w)
        if alert:
            border = curses.color_pair(5) | curses.A_BOLD
            self.draw_frame(stdscr, y, x, h, w, border)
            label = f" {shorten(title, w - 6)} "
            self.write(stdscr, y, x + max(2, (w - display_width(label)) // 2), label, border)
        else:
            self.box(stdscr, y, x, h, w, title, active=True)
        field = w - 4
        for offset, (text, attr) in enumerate(content[: h - 4]):
            drawn = shorten(text, field)
            tx = x + 2 + max(0, (field - display_width(drawn)) // 2) if center else x + 2
            self.write(
                stdscr,
                y + 2 + offset,
                tx,
                drawn if center else pad(drawn, field),
                attr,
            )
        return y, x, h, w

    def draw_source_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `H` picker: a small modal list of every present source. j/k moves the
        # highlight, Enter switches, Esc cancels (handled in handle_source_menu_key).
        entries = self.source_menu_entries()
        idx = self.source_menu_index % len(entries) if entries else 0
        lines = [("Browse spend recorded by which harness:", curses.color_pair(4)), ("", 0)]
        for offset, (_key, label, is_current) in enumerate(entries):
            marker = "●" if is_current else "○"
            suffix = "  (current)" if is_current else ""
            attr = curses.A_REVERSE | curses.A_BOLD if offset == idx else curses.A_NORMAL
            lines.append((f" {marker}  {label}{suffix}", attr))
        self.draw_modal(
            stdscr, scr_h, scr_w, self._menu_title("Switch harness", "menu.source"), lines
        )

    def draw_demo_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `D` picker: a multi-check list of what --demo scrambles. Space toggles a
        # row's [x], `a` all/none, Enter applies (nothing checked = back to real data),
        # Esc cancels. A checkbox list where draw_source_menu is a radio one.
        entries = self.demo_menu_entries()
        idx = self.demo_menu_index % len(entries) if entries else 0
        intro = (
            "Anonymize which parts (for a shareable screen):"
            if self.demo_menu_sel
            else f"Nothing checked — {self._key('menu.demo', 'select')} shows real data again."
        )
        lines = [(intro, curses.color_pair(4)), ("", 0)]
        for offset, (_cat, label, checked) in enumerate(entries):
            box = "[x]" if checked else "[ ]"
            attr = curses.A_REVERSE | curses.A_BOLD if offset == idx else curses.A_NORMAL
            lines.append((f" {box}  {label}", attr))
        title = (
            f"Demo · {self._key('menu.demo', 'toggle')} · "
            f"{self._key('menu.demo', 'check_all')} all · "
            f"{self._key('menu.demo', 'select')} · {self._key('menu.demo', 'cancel')}"
        )
        self.draw_modal(stdscr, scr_h, scr_w, title, lines)

    def _draw_filter_menu(self, stdscr, scr_h, scr_w, title, intro, options, index) -> None:
        # Shared body for the `M` / `H` global-filter pickers: an intro line then a radio
        # list (● current, ○ others), the selected row reversed. Mirrors draw_source_menu.
        idx = index % len(options) if options else 0
        lines = [(intro, curses.color_pair(4)), ("", 0)]
        for offset, (_value, label, is_current) in enumerate(options):
            marker = "●" if is_current else "○"
            suffix = "  (current)" if is_current else ""
            attr = curses.A_REVERSE | curses.A_BOLD if offset == idx else curses.A_NORMAL
            lines.append((f" {marker}  {label}{suffix}", attr))
        self.draw_modal(stdscr, scr_h, scr_w, title, lines)

    def draw_machine_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `M` picker: narrow every view to one box (or "All machines" to clear). j/k
        # moves the highlight, Enter arms, Esc cancels (handle_machine_menu_key).
        self._draw_filter_menu(
            stdscr,
            scr_h,
            scr_w,
            self._menu_title("Filter machine", "menu.machine"),
            "Narrow every view to which machine:",
            self.machine_filter_options(),
            self.machine_menu_index,
        )

    def draw_harness_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The fleet `H` picker: narrow every view to one tool across all machines (or "All
        # harnesses" to clear). The machine picker's orthogonal twin.
        self._draw_filter_menu(
            stdscr,
            scr_h,
            scr_w,
            self._menu_title("Filter harness", "menu.harness"),
            "Narrow every view to which harness (kept across all machines):",
            self.harness_filter_options(),
            self.harness_menu_index,
        )

    WHATIF_TIERS = ("your models", "models.dev")  # the picker's two row sets, Tab-flipped

    def draw_whatif_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `w` picker: arm ONE model as a comparison target -- "what if this model
        # had done all of a session's work?" -- and a session's Subagents tab prices
        # that session's tree at its list rates. Two tiers, Tab flips between them:
        # your own models (most-used first, with the tokens they burned) and the whole
        # models.dev catalog (cheapest-for-your-mix first, with the eff $/M blend the
        # P overlay computes -- ~ marks a missing cache-read rate billed at the input
        # rate). j/k moves the highlight, Enter arms, `f` narrows the list (word-anchored,
        # the P overlay's filter), Esc cancels (handle_whatif_menu_key); `w` again with
        # a target set clears it. Scrolled around the selection like the theme picker --
        # the catalog runs to thousands of rows, which is what the filter is for.
        entries = self.whatif_rows()
        idx = self.whatif_menu_index % len(entries) if entries else 0
        # Reserve rows for every non-entry line the modal carries, so draw_modal never
        # clips the SELECTED entry off the bottom -- even at the 80x20 minimum, where this
        # is handed scr_h=18. draw_modal paints at most scr_h-8 content rows; the worst-case
        # non-entry lines before the list are 7 (two intro + the two-line tier strip + the
        # filter and its blank + the "↑ more" marker), so entries must stay <= scr_h-15.
        # The floor is 1, not 4: at scr_h=18 only three rows fit, and a floor of 4 would put
        # the selected last row past the paint budget (Enter then arms an off-screen model).
        max_rows = max(1, scr_h - 15)
        start = 0
        if len(entries) > max_rows:
            start = min(max(0, idx - max_rows // 2), len(entries) - max_rows)
        visible = entries[start : start + max_rows]
        # The model-id column widens to fit the longest row, capped so the box still fits
        # the terminal (and the eff/tokens cell isn't clipped off the right edge): 36% of
        # catalog ids overflow a fixed 34 ("github-copilot/claude-sonnet-4.5", the whole
        # "names don't fit" complaint). Sized off `entries` (the filtered set), not the
        # scroll window, so the width tracks the filter and never jumps as j/k scrolls.
        longest = max((len(str(r[0])) for r in entries), default=24)
        name_cap = max(24, scr_w - 4) - 19  # modal width cap − "| |" gutters(4) − prefix+cell(15)
        namew = max(24, min(longest, name_cap))
        lines = [
            ("Compare a session's tree against one model's list rates:", curses.color_pair(4)),
            ("(the Subagents tab; every other view keeps its actual cost)", curses.A_DIM),
            ("", 0),  # the tier tab strip, post-painted below (draw_tabs needs mixed attrs)
            ("", 0),
        ]
        tier_line = 2
        if self.whatif_query or self.whatif_filter_active:
            # A block cursor while the query is live, so it reads as an input, not a label.
            cursor = "█" if self.whatif_filter_active else ""
            lines.append((f" filter: {self.whatif_query}{cursor}", curses.color_pair(4)))
            lines.append(("", 0))
        if start:
            lines.append((f"    ↑ {start} more", curses.A_DIM))
        for offset, row in enumerate(visible, start=start):
            name = row[0]
            if self.whatif_catalog:
                _name, eff, approx = row
                cell = f"{'~' if approx else ''}${eff:,.2f}/M"
            else:
                cell = human_tokens(row[1])
            marker = "●" if name == self.whatif_model else "○"
            attr = curses.A_REVERSE | curses.A_BOLD if offset == idx else curses.A_NORMAL
            lines.append((f" {marker}  {pad(shorten(name, namew), namew)} {cell:>10}", attr))
        if not entries:
            erase = self._key("menu.whatif.filter", "erase")
            lines.append((f"    no model matches — {erase} to widen", curses.color_pair(2)))
        below = len(entries) - (start + len(visible))
        if below:
            lines.append((f"    ↓ {below} more", curses.A_DIM))
        hint = (
            f"{self._key('menu.whatif.filter', 'select')} selects · "
            f"{self._key('menu.whatif.filter', 'cancel')} drops the filter"
            if self.whatif_filter_active
            else f"{self._key('menu.whatif', 'filter')} filter · "
            f"{self._key('main', 'whatif')} again clears it · "
            f"{self._key('menu.whatif', 'cancel')} cancels"
        )
        lines += [("", 0), (hint, curses.color_pair(1))]
        catalog = "/".join(self.app.keymap.labels("menu.whatif", "catalog")[:3])
        title = (
            f"What-if model · {self._keys('menu.whatif', 'down', 'up')} · {catalog} · "
            f"{self._key('menu.whatif', 'filter')} · {self._key('menu.whatif', 'select')} · "
            f"{self._key('menu.whatif', 'cancel')}"
        )
        my, mx, mh, mw = self.draw_modal(stdscr, scr_h, scr_w, title, lines)
        # The tier switch is a real tab strip (the P overlay's view tabs, same renderer,
        # same clickable regions -- handle_mouse routes "whatiftab" hits to the flip):
        # [your models]  models.dev, with the tier's column meaning dimmed beside it.
        if tier_line < mh - 4:
            ty, tx, field = my + 2 + tier_line, mx + 2, mw - 4
            tabs = self.WHATIF_TIERS
            self.draw_tabs(stdscr, ty, tx, field, tabs, int(self.whatif_catalog), kind="whatiftab")
            tabs_w = sum(len(t) + 2 for t in tabs) + 2 * (len(tabs) - 1)
            note = "eff $/M at your mix" if self.whatif_catalog else "tokens you ran through each"
            if tabs_w + 2 + len(note) <= field:
                self.write(stdscr, ty, tx + field - len(note), note, curses.A_DIM)

    def draw_theme_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `C` (Colours) picker: a modal list of the themes (shared with the web
        # browser). j/k live-previews each (the whole UI is the swatch), Enter keeps it,
        # Esc reverts to the theme active on open. Colours re-map via init_theme_colors.
        entries = self.theme_menu_entries()
        idx = self.theme_menu_index % len(entries) if entries else 0
        # The list outgrew small terminals: scroll a window around the selection so
        # j/k live-preview never walks the highlight off the modal's visible rows
        # (draw_modal itself just truncates; ↑/↓ counts show what's clipped).
        max_rows = max(4, scr_h - 12)
        start = 0
        if len(entries) > max_rows:
            start = min(max(0, idx - max_rows // 2), len(entries) - max_rows)
        visible = entries[start : start + max_rows]
        lines = [("Colour theme (also the web browser's):", curses.color_pair(4)), ("", 0)]
        if start:
            lines.append((f"    ↑ {start} more", curses.A_DIM))
        for offset, (_tid, name, is_current) in enumerate(visible, start=start):
            marker = "●" if is_current else "○"
            suffix = "  (current)" if is_current else ""
            attr = curses.A_REVERSE | curses.A_BOLD if offset == idx else curses.A_NORMAL
            lines.append((f" {marker}  {name}{suffix}", attr))
        below = len(entries) - (start + len(visible))
        if below:
            lines.append((f"    ↓ {below} more", curses.A_DIM))
        title = (
            f"Theme · {self._keys('menu.theme', 'down', 'up')} preview · "
            f"{self._key('menu.theme', 'select')} keep · {self._key('menu.theme', 'cancel')} revert"
        )
        self.draw_modal(stdscr, scr_h, scr_w, title, lines)

    # Friendlier one-word names for the raw sort keys shown in the `s` picker.
    SORT_LABELS = {
        "cost": "Cost",
        "tokens": "Tokens",
        "date": "Start Date",
        "last_activity": "Last Activity",
        "duration": "Worked",
        "recency": "Recency",
        "subagents": "Subagents",
        "sessions": "Sessions",
        "title": "Title",
        "project": "Project",
        "model": "Model",
        "agent": "Agent",
        "depth": "Depth",
        "eff": "eff $/M (your mix)",
        "use": "use (token share)",
        "input": "Input price",
        "output": "Output price",
        "cache_read": "Cache-read price",
        "cache_write": "Cache-write price",
        "name": "Name",
        "count": "Count",
    }

    def sort_label(self, key: str) -> str:
        # What the `s` picker calls a sort key. The Trends rankings share two keys
        # across four tables ("name"/"count"), so they name them per tab -- the picker
        # must read as the column you can see ("Harness", "Sessions"), not as the
        # internal key. Everything else takes the flat table above.
        if self.app.in_trend_sort_context():
            return self.app.trend_sort_labels().get(key, self.SORT_LABELS.get(key, key))
        return self.SORT_LABELS.get(key, key)

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
            lines.append((f" {marker}  {self.sort_label(key)}{suffix}", attr))
        self.draw_modal(stdscr, scr_h, scr_w, self._menu_title("Sort by", "menu.sort"), lines)

    def draw_launch_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `L` picker: a small modal of launch targets. One keystroke picks (handled in
        # handle_launch_key); anything else cancels.
        session = self.launch_menu
        targets = self.launch_targets()
        # A session pulled from another box reopens THERE, over ssh -- so the picker says
        # where it is about to land before you pick, rather than after.
        remote = self.machine_ssh_target(session)
        unreachable = self.unreachable_machine()
        if unreachable:
            headline = f"pulled from {unreachable} — no ssh target, copy instead:"
        elif not self.launch_available():
            headline = "no tmux / herdr / launcher hook — copy instead:"
        else:
            via = (
                "launcher hook" if self.launch_menu_backend == "hook" else self.launch_menu_backend
            )
            headline = f"open in {via}:" if not remote else f"open on {remote} (ssh) in {via}:"
        idx = self.launch_menu_index % len(targets)
        lines = [
            (shorten(session.title or "(untitled)", 52), curses.color_pair(4)),
            (headline, curses.A_NORMAL),
            ("", 0),
        ]
        for offset, (kc, kind, label) in enumerate(targets):
            attr = curses.A_REVERSE | curses.A_BOLD if offset == idx else curses.A_NORMAL
            # The yank is the one row whose CONTENT changes with the machine: for a
            # pulled session it copies the ssh line, not a cd into a path that isn't here.
            if kind == "copy" and remote:
                label = "copy ssh command"
            lines.append((f" {kc}  {label}", attr))
        lines += [("", 0), (f" {self._key('menu.launch', 'cancel')}  cancel", curses.A_NORMAL)]
        self.draw_modal(
            stdscr, scr_h, scr_w, self._menu_title("Launch session", "menu.launch"), lines
        )

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
            (f" {self._key('prompt.prices', 'accept')}   yes, fetch now", accent),
            (f" {self._key('prompt.prices', 'decline')}   not now (ask again next run)", accent),
            (f" {self._key('prompt.prices', 'never')}   don't ask again", accent),
            ("", 0),
            (
                "anytime: --refresh-models, or "
                f"{self._key('prices', 'refresh')} in the {self._key('main', 'prices')} "
                "prices view",
                curses.color_pair(1),
            ),
        ]
        self.draw_modal(stdscr, scr_h, scr_w, "Unpriced models found", lines)

    def draw_startup_warning(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        warning = self.startup_warning or {}
        accent = curses.color_pair(6) | curses.A_BOLD
        danger = curses.color_pair(5) | curses.A_BOLD
        lines = [
            (" DATA LOSS RISK ", danger | curses.A_REVERSE),
            ("", 0),
            (
                warning.get("headline", "History may expire"),
                danger,
            ),
            ("", 0),
        ]
        lines += [(text, curses.A_NORMAL) for text in warning.get("lines", [])]
        queued = len(self.startup_warnings()) - 1
        lines += [
            ("", 0),
            (
                f"{self._keys('prompt.warning', 'continue')}  continue for now"
                + (f"  ({queued} more warning{'s' if queued > 1 else ''})" if queued > 0 else ""),
                accent,
            ),
            (
                f"{self._key('prompt.warning', 'never')}  "
                + (
                    "don't warn again" if self.startup_warning_can_persist else "close for this run"
                ),
                accent,
            ),
        ]
        self.draw_modal(
            stdscr,
            scr_h,
            scr_w,
            warning.get("title", "WARNING · Data retention"),
            lines,
            center=True,
            alert=True,
        )

    # --- Trends overlay -------------------------------------------------------
    def draw_trends(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        h = bottom - y
        self.box(stdscr, y, 0, h, width, f"Trends · {self.range_label()}", active=True)
        tabs = self.trend_tabs
        current = tabs[self.trend_tab % len(tabs)]
        self.app._trend_bar_geom = None  # rebuilt below when a bar chart draws
        self._trend_rows_at = None  # rebuilt below when a selectable list draws
        arrows = "".join(
            self.app.keymap.label("trends.chart", a)
            for a in ("cursor_up", "cursor_down", "cursor_left", "cursor_right")
        )
        tabkeys = self._keys("trends", "tab_prev", "tab_next")
        jk = self._keys("trends", "down", "up")
        if self.trend_drill is not None:
            hint = (
                f"{self._keys('trends.drill', 'down', 'up')} move · "
                f"{self._key('trends.drill', 'select')} opens session · "
                f"{self._key('trends.drill', 'back')} back"
            )
        elif current == "Calendar":
            if self.trend_focus:
                hint = (
                    f"{arrows} day · "
                    f"{self._keys('trends', 'shades_more', 'shades_less')} shades · "
                    f"{self._key('trends.chart', 'select')} open · "
                    f"{self._key('trends.chart', 'back')} back"
                )
            else:
                hint = (
                    f"{tabkeys} tabs · {self._key('trends', 'select')} pick days · "
                    f"{self._key('trends', 'back')} closes"
                )
        elif current in ("Daily", "Weekly", "Monthly"):
            if self.trend_focus:
                hint = (
                    f"{arrows} bar · {self._key('trends.chart', 'select')} open · "
                    f"{self._key('trends.chart', 'back')} back"
                )
            else:
                unit = {"Daily": f"{jk} month · ", "Weekly": f"{jk} week · "}.get(current, "")
                hint = (
                    f"{tabkeys} tabs · {unit}{self._key('trends', 'select')} pick bars · "
                    f"{self._key('trends', 'back')} closes"
                )
        else:
            hint = (
                f"{tabkeys} tabs · {jk} rows · {self._key('trends', 'select')} sessions · "
                f"{self._key('trends', 'back')} closes"
            )
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
        elif current == "Projects":
            lines = self.trend_projects(inner_w, content_h)
        elif current == "Harnesses":
            lines = self.trend_sources(inner_w, content_h)
        elif current == "Machines":
            lines = self.trend_machines(inner_w, content_h)
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
        headers = self.box_header_lines(content)
        for i, line in enumerate(content):
            is_title = line.startswith("# ")
            is_marker = line.lstrip().startswith("▲")  # the bar cursor's pointer line
            if i == sel_line:
                # The same cursor every table in the app wears: reversed BETWEEN the box
                # gutters, bars overdrawn so a spend bar isn't a hole in the highlight.
                self.paint_cursor_row(
                    stdscr, y + 3 + i, 2 + graph_off, line, inner_w - graph_off, bars=True
                )
                continue
            if i in headers:
                # A ranking's header is also its sort control: the zones are placed at
                # the y and x this frame actually painted it on, centering offset and
                # all, so the click lands on the label the user aimed at.
                self._register_line_sort_header(
                    y + 3 + i, 2 + graph_off, i, line, inner_w - graph_off
                )
                self._paint_box_header(stdscr, y + 3 + i, 2 + graph_off, line, inner_w - graph_off)
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
            title += f"   ({idx + 1}/{len(months)} — {self._keys('trends', 'down', 'up')} older/newer month)"
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
            title += f"   ({idx + 1}/{len(weeks)} — {self._keys('trends', 'down', 'up')} older/newer week)"
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
            title += f"   ({idx + 1}/{len(years)} — {self._keys('trends', 'down', 'up')} older/newer year)"
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
                        (
                            f"{label}{money(day_cost)}   {day_sessions} {noun}   "
                            f"{self._key('trends.chart', 'select')} opens",
                            0,
                        )
                    )
                else:
                    arrows = " ".join(
                        self.app.keymap.label("trends.chart", a)
                        for a in ("cursor_left", "cursor_up", "cursor_down", "cursor_right")
                    )
                    info.append((f"{label}no sessions   move with {arrows}", 0))
        else:
            info.append(("", 0))  # a couple of blank lines set the call-to-action apart
            info.append(("", 0))
            info.append(
                (
                    f"Press {self._key('trends', 'select')} to navigate the calendar",
                    curses.color_pair(6) | curses.A_BOLD,
                )
            )
        if total == 0 and sessions and not self.show_api_prices:
            info.append(
                (
                    f"{self._key('trends', 'api_prices')} prices subscription/credit "
                    "usage at API list rates",
                    0,
                )
            )
        for i, (line, line_attr) in enumerate(info):
            row_y = ly + 1 + i
            if row_y >= top + height:
                break
            text = shorten(line, width)
            cx = left + max(0, (width - len(text)) // 2)  # center each line under the grid
            self.write_rich(stdscr, row_y, cx, text, line_attr)

    # Custom-color SLOT NUMBERS (only touched when the terminal can redefine colors):
    # roles allocate up from 0, the heat ramps get fixed numbers so they can be
    # re-init_color'd every frame without exhausting the palette. A slot number is not
    # a palette index -- `_slot()` maps it to one, and every colour is written TWICE.
    # See `_slot`/`_write_color` for why.
    _THEME_COLOR_BASE = 16  # palette index the first slot maps to
    _ROLE_SLOTS = 16  # slots 0..15 for the theme roles (11 used); the ramps start after
    _HEAT_COLOR_BASE = 16  # calendar heat colours (up to HEAT_MAX_LEVELS)
    _PRICE_COLOR_BASE = 32  # price-heat colours (PRICE_HEAT_LEVELS)
    _TOKEN_COLOR_BASE = 40  # the token-type categorical ramp (TOKEN_SERIES)
    _TOOL_COLOR_BASE = 48  # Tools treemap fill colours (TOOL_HEAT_LEVELS)
    _BASE_PAIR = 32  # the window background pair (ink on theme bg); clear of heat/price
    _TAB_PAIR = 25  # inactive-tab chip (ink2 on panel2); free slot after the price ramp
    _bg_index = -1  # the theme's background colour index (set in init_theme_colors)
    # Did the five token-type pairs take? False on a pair-starved terminal, where the
    # bar falls back to per-type glyphs. Class-level so a Renderer built without curses
    # (the line-builders are unit-tested headless) still answers.
    _token_series_ok = True
    _tool_heat_ok = True

    @classmethod
    def _slot(cls, number: int) -> int:
        # Slot number -> palette index, kept in the half-blocks whose BIT 3 IS CLEAR:
        # 16..23, 32..39, 48..55, ... The gaps are not waste, they are the point.
        #
        # A terminal with "bold is bright" applies the classic fg -> fg|8 bump, and some
        # apply it across the whole 256-palette rather than just the 8 base colours.
        # Every index we hand out therefore needs its |8 twin (== +8 here, bit 3 being
        # clear, so it covers a terminal that adds instead of ors) to hold the SAME
        # colour -- otherwise a bold cell silently reads whichever slot happens to sit
        # 8 higher. Measured on a real report: roles landed at 16..26, so bold ink2
        # (18) read slot 26, which `_init_tool_heat` had loaded with `ink_on`'s near
        # black -- the breadcrumb, inactive panel titles and the selected row of an
        # unfocused sidebar panel all rendered #101014 on a #1a1b26 background, i.e.
        # invisible. Bold accent/good/accent_bright (19/20/23) likewise read the
        # untouched cube colours at 27/28/31.
        return cls._THEME_COLOR_BASE + (number // 8) * 16 + (number % 8)

    def _write_color(self, number: int, hexcolor: str) -> int | None:
        # init_color one slot AND its bold twin, returning the palette index to use
        # (None when the terminal can't take it). Writing the twin is what makes a
        # bold-is-bright terminal render bold text in the theme's colour instead of an
        # unrelated slot's; a terminal that doesn't do the bump never reads it.
        idx = self._slot(number)
        if idx + 8 >= getattr(curses, "COLORS", 0):
            return None
        rgb = hex_rgb1000(hexcolor)
        try:
            curses.init_color(idx, *rgb)
        except (curses.error, ValueError):
            return None
        try:
            curses.init_color(idx + 8, *rgb)
        except (curses.error, ValueError):
            pass  # no twin: bold may shift hue, but the un-bolded colour is still right
        return idx

    def _color_index(self, hexcolor: str) -> int:
        # A curses color index for a hex: a fresh init_color slot on truecolor
        # terminals (cached per hex), else the nearest xterm-256 -- but never past
        # what the terminal has: on an 8-color screen (TERM=linux) init_pair raises
        # ValueError for any index >= COLORS, so there the nearest basic ANSI color
        # is the whole palette. Falls back to the nearest lookup if init_color is
        # refused, so a partial terminal never crashes.
        cache = self._theme_color_cache
        if hexcolor in cache:
            return cache[hexcolor]
        # Roles get slots 0.._ROLE_SLOTS-1; past that the ramps' fixed slots begin, so a
        # theme that grew more roles than the block holds falls back to nearest-256
        # rather than overwriting the calendar's colours.
        if self._can_change and self._next_color < self._ROLE_SLOTS:
            written = self._write_color(self._next_color, hexcolor)
            if written is not None:
                self._next_color += 1
                cache[hexcolor] = written
                return written
        if getattr(curses, "COLORS", 256) < 256:
            idx = nearest_8(hexcolor)  # 8 colours for ~9 roles: collisions are inevitable
        else:
            # Approximating: claim a distinct index per role, so two roles that both
            # round to the same entry stay tellable apart (a focused border must not
            # look like ordinary accent text). Allocation order gives the earlier role
            # the better match, and bg -- which fills every cell -- goes first.
            idx = nearest_256(hexcolor, frozenset(self._fallback_used))
            self._fallback_used.add(idx)
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
        self._next_color = 0  # slot NUMBER, not a palette index -- see _slot()
        self._fallback_used = set()  # indices claimed on the nearest-256 path this theme
        self._themed_bg = False
        self._can_change = False
        self._tool_heat_ok = False
        if not self.colors_ok:  # monochrome: every pair stays "terminal default"
            return
        # `can_change_color()` only reports what terminfo CLAIMS (the `ccc` capability).
        # A terminal can advertise it, accept every init_color without error, and drop
        # the palette write on the floor -- then all eleven roles paint as whatever the
        # default cube holds at 16.., i.e. one blue mush, identically under every theme
        # (issue #12). Nothing readable back from ncurses distinguishes that case
        # (color_content reports ncurses' own idea of the palette, not the terminal's),
        # so the known hosts are detected up front (util.palette_writes_ignored) and
        # $OPENTAB_NO_INIT_COLOR overrides that either way. Both land here as
        # allow_init_color=False, dropping us onto the nearest-256 path -- the standard
        # palette every terminal renders.
        self._can_change = bool(
            self.app.allow_init_color
            and self.has256
            and getattr(curses, "can_change_color", lambda: False)()
        )
        roles = self.app.theme["roles"]
        r = self._color_index
        bg = self._bg_index = r(roles["bg"])
        # The window-background pair: if the terminal can't hold pair 32, skip the fill.
        if self._set_pair(self._BASE_PAIR, r(roles["ink"]), bg):
            self._themed_bg = True
        else:
            self._bg_index = bg = -1  # no themed fill -> role pairs fall back to terminal bg
        self._set_pair(1, r(roles["ink2"]), bg)  # secondary text
        self._set_pair(2, r(roles["accent"]), bg)  # warm accent / title / M-tokens
        self._set_pair(3, r(roles["good"]), bg)  # money
        self._set_pair(4, r(roles["mut"]), bg)  # structural: headers, keybar, '#'
        self._set_pair(5, r(roles["bad"]), bg)  # alerts
        self._set_pair(6, r(roles["accent_bright"]), bg)  # focus / active border
        self._set_pair(7, bg, r(roles["accent"]))  # active tab (inverse: bg on accent)
        # An inactive tab is a raised chip (secondary ink on the panel2 surface), so a tab
        # bar reads as tabs instead of grey text on the background. Pair-starved terminals
        # skip it and fall back to plain text -- the active tab's [brackets] still show which.
        self._set_pair(self._TAB_PAIR, r(roles["ink2"]), r(roles["panel2"]))
        self._init_price_heat()
        self._init_tool_heat()
        self._init_token_series()
        self._sync_heat_palette()

    @staticmethod
    def _set_pair(pair: int, fg: int, bg: int) -> bool:
        # Every init_pair goes through here: a terminal can be color-capable and still
        # pair-starved (minitel1: COLORS=8, COLOR_PAIRS=8 -- pairs 1..7 fit, the heat
        # ramps at 8+/20+ and the bg pair at 32 don't, and init_pair raises ValueError
        # for those, not curses.error). A pair that doesn't fit is skipped: reading it
        # via color_pair() is still legal and renders as the terminal default, so the
        # UI degrades to fewer colors instead of crashing at startup.
        if pair >= getattr(curses, "COLOR_PAIRS", 0):
            return False
        try:
            curses.init_pair(pair, fg, bg)
            return True
        except (curses.error, ValueError):
            return False

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
                self._set_pair(
                    PRICE_HEAT_BASE_PAIR + i,
                    self._heat_index(self._PRICE_COLOR_BASE + i, hx),
                    self._bg_index,
                )
        else:
            for i, col in enumerate(heat_palette(PRICE_HEAT_LEVELS, False)):
                self._set_pair(PRICE_HEAT_BASE_PAIR + i, col, self._bg_index)

    def _init_tool_heat(self) -> None:
        # These pairs are filled rectangles: theme heat as background, with
        # black/white foreground selected independently for every shade.
        hexes = ramp(self.app.theme["heat"], TOOL_HEAT_LEVELS)
        ok = []
        for i, hx in enumerate(hexes):
            bg = (
                self._heat_index(self._TOOL_COLOR_BASE + i, hx)
                if self._can_change or self.has256
                else nearest_8(hx)
            )
            ok.append(
                self._set_pair(
                    TOOL_HEAT_BASE_PAIR + i,
                    self._color_index(ink_on(hx)),
                    bg,
                )
            )
        self._tool_heat_ok = all(ok)

    def _init_token_series(self) -> None:
        # The Token economics bar's five categorical fills. Unlike the two heat ramps
        # this one is not a scale, so it never re-derives from the theme's `heat` hexes:
        # it is its own validated pair, picked by whether the theme is dark or light.
        #
        # `_token_series_ok` records whether the pairs actually took. A pair-starved
        # terminal (_set_pair returns False for anything past COLOR_PAIRS) would render
        # every segment in the terminal default -- one indistinguishable blob across a
        # chart whose whole point is telling five things apart -- so the box switches to
        # per-segment glyphs there instead.
        hexes = token_series(bool(self.app.theme.get("dark", True)))
        if self._can_change or self.has256:
            ok = [
                self._set_pair(
                    TOKEN_SERIES_BASE_PAIR + i,
                    self._heat_index(self._TOKEN_COLOR_BASE + i, hx),
                    self._bg_index,
                )
                for i, hx in enumerate(hexes)
            ]
        else:
            ok = [
                self._set_pair(TOKEN_SERIES_BASE_PAIR + i, col, self._bg_index)
                for i, col in enumerate(token_series_ansi())
            ]
        self._token_series_ok = all(ok)

    def _heat_index(self, slot: int, hexcolor: str) -> int:
        # A reusable fixed-slot heat colour: re-init_color the slot on truecolor
        # terminals (so per-frame ramps don't leak indices), else nearest-256. `slot` is
        # a slot NUMBER, not a palette index -- it goes through _write_color so the heat
        # colours get their bold twin like the roles do (the heat cells are drawn
        # A_BOLD, so on a bold-is-bright terminal they are the first thing to shift).
        if self._can_change:
            written = self._write_color(slot, hexcolor)
            if written is not None:
                return written
        return nearest_256(hexcolor)

    def _sync_heat_palette(self) -> None:
        # Re-init the calendar heat pairs (8..) for the current granularity so +/-
        # restyles live. Colours come from the active theme's ramp, resampled to
        # cal_levels; 8-colour terminals keep the generated ANSI ramp + glyphs.
        # Also reached at runtime (a +/- granularity change), so it carries its own
        # monochrome guard -- there is no pair to init without start_color.
        if not self.colors_ok:
            return
        if self.has256:
            for i, hx in enumerate(ramp(self.app.theme["heat"], self.cal_levels)):
                self._set_pair(
                    8 + i, self._heat_index(self._HEAT_COLOR_BASE + i, hx), self._bg_index
                )
        else:
            for i, col in enumerate(heat_palette(self.cal_levels, False)):
                self._set_pair(8 + i, col, self._bg_index)

    def _heat_cell(self, level: int, levels: int) -> tuple[str, int]:
        # (glyph, attr) for one heat level: a distinct color per level, plus a glyph that
        # keeps levels apart where the color ramp collapses (8-color / mono terminals).
        if level <= 0:
            return HEAT_EMPTY_GLYPH, curses.color_pair(1) | curses.A_DIM
        return heat_glyph(level, levels, self.has256), curses.color_pair(7 + level) | curses.A_BOLD

    def _ranked_row_budget(self, height: int, n: int, notes: int = 0) -> int:
        """How many BODY rows a scrolling ruled table may draw inside `height` lines.

        draw_trends hands each tab a height and then CLIPS what comes back to it, so a
        builder that budgets its window without counting its own frame loses whatever
        the frame pushed past the cut -- and what a ruled box pushes out last is its
        TOTAL row and its bottom border, i.e. the table reads as though it simply ended
        (measured: a 20-line viewport produced 23 lines). The chrome is BOX_CHROME (top,
        header, rule, bottom) plus the rule+TOTAL a multi-row table closes with, plus any
        notes riding below the box.
        """
        return max(1, height - self.BOX_CHROME - (2 if n > 1 else 0) - notes)

    def _unpriced_note(self, all_rows: list) -> list[str]:
        """The "press $" nudge under a ranked table, when the ranking holds $0 rows.

        Asked of the WHOLE ranking rather than the scrolled window: the note is about
        the data the table stands for, and one that blinked in and out as you scrolled
        would also move the row budget under the cursor on every keypress.
        """
        if self.show_api_prices or not any(
            float(it["cost"]) == 0 and int(it["tokens"]) for _, it in all_rows
        ):
            return []
        return [
            "",
            f"{self._key('trends', 'api_prices')} prices subscription/credit "
            "usage at API list rates",
        ]

    def _trend_cursor_window(self, n: int, fit: int) -> tuple[int, int, int]:
        # Clamp the ranked-row cursor (writing the clamp back, so a shrunk list
        # never leaves it dangling), then a stateless window that keeps it visible:
        # (cursor, window start, rows shown).
        idx = max(0, min(self.app.trend_row_index, n - 1))
        self.app.trend_row_index = idx
        fit = max(1, fit)
        start = max(0, min(idx - fit // 2, n - fit))
        return idx, start, min(fit, n - start)

    # The Models ranking's sortable columns, in the order their labels appear in the
    # header -- Share is deliberately absent: it is Cost expressed as a percentage, so
    # a second zone ordering by it would be the same ranking under another name.
    _TREND_MODEL_SORT_COLUMNS = (("name", "Model"), ("cost", "Cost"))

    def trend_models(self, width: int, height: int) -> list[str]:
        all_rows = self.trend_ranked_rows("Models")
        if not all_rows:
            return ["# Model spend", "", "No priced model spend in the active range."]
        total = sum(c for _, c in all_rows)
        peak = max(c for _, c in all_rows) or 1.0
        _idx, start, shown = self._trend_cursor_window(
            len(all_rows), self._ranked_row_budget(height, len(all_rows))
        )
        rows = all_rows[start : start + shown]
        head_name = self.trend_sort_heading("name", "Model", "Models")
        head_cost = self.trend_sort_heading("cost", "Cost", "Models")
        # Names get priority so long ids like claude-opus-4-5-20251101 show in
        # full; the bar takes only the leftover (kept modest) instead of eating
        # the width and forcing names to truncate. The name column is sized to its own
        # HEADER too (_group_widths' rule): with every model shorter than "Model v",
        # the header's field overflowed and shifted Cost/Share right of their numbers.
        tail = 22  # marker gutter + spacing + money (>=11) + percent (5)
        inner = max(1, width - self.BOX_CHROME)
        namew = min(max([len(n) for n, _ in rows] + [len(head_name)]), max(12, inner - tail - 4))
        barw = max(3, min(24, inner - namew - tail))
        body = []
        for name, cost in rows:
            bar = "█" * max(0, round((cost / peak) * barw))
            body.append(
                f"  {pad(shorten(name, namew), namew)}  {bar:<{barw}} "
                f"{money(cost):>11} {pct(cost, total):>5}"
            )
        totals_row = None
        if len(all_rows) > 1:
            # Sums the WHOLE ranking, not the scrolled window -- the window is a viewport
            # onto it, and a total that changed as you scrolled would be a different
            # number every frame.
            totals_row = f"  {pad('TOTAL', namew)}  {'':{barw}} {money(total):>11} {'':>5}"
        lines = self._ruled_box(
            "# Model spend (priced, in range)",
            f"  {head_name:{namew}}  {'':{barw}} {head_cost:>11} {'Share':>5}",
            body,
            totals_row,
            [],
            width,
        )
        self._mark_trend_sort_header(self._TREND_MODEL_SORT_COLUMNS)
        self._trend_rows_at = (self._ruled_body_start or 0, len(rows), start)
        return lines

    def _mark_trend_sort_header(self, columns: tuple) -> None:
        # Make a Trends ranking's column header clickable. Registered against
        # BOX_HEADER_LINE like every other boxed table's zones; draw_trends turns it
        # into screen coordinates at the y the header actually lands on.
        self._line_sort_headers[self.BOX_HEADER_LINE] = (columns, "trend")

    def trend_providers(self, width: int, height: int) -> list[str]:
        # The per-model spend rolled up to its provider (the "openai" in
        # "openai/gpt-5"), so you can compare e.g. openai vs github-copilot.
        # Subscription/credit providers record $0 per message, so their cost only
        # shows once "$" reprices unpriced usage at API list rates -- the cost column
        # and bar react to it live. We still list those providers when "$" is off
        # (tokens are the tell) and nudge toward "$".
        all_rows = self.trend_ranked_rows("Providers")
        if not all_rows:
            return ["# Spend by provider", "", "No model usage in the active range."]
        total_cost = sum(float(it["cost"]) for _, it in all_rows)
        peak = max((float(it["cost"]) for _, it in all_rows), default=0.0) or 1.0
        notes = self._unpriced_note(all_rows)
        _idx, start, shown = self._trend_cursor_window(
            len(all_rows), self._ranked_row_budget(height, len(all_rows), len(notes))
        )
        rows = all_rows[start : start + shown]
        columns = (("name", "Provider"), ("cost", "Cost"), ("tokens", "Tokens"), ("count", "Msgs"))
        heads = {k: self.trend_sort_heading(k, label, "Providers") for k, label in columns}
        inner = max(1, width - self.BOX_CHROME)
        # The name column is sized to its own (arrowed) header too, or a table of short
        # provider names shifts every numeric label right of its column.
        namew = min(max([len(p) for p, _ in rows] + [len(heads["name"])]), max(10, inner - 44))
        barw = max(3, min(20, inner - namew - 40))
        header = (
            f"  {heads['name']:{namew}}  {'':{barw}} {heads['cost']:>11} {'Share':>5} "
            f"{heads['tokens']:>9} {heads['count']:>7}"
        )
        body = []
        for provider, it in rows:
            bar = "█" * max(0, round((float(it["cost"]) / peak) * barw))
            body.append(
                f"  {pad(shorten(provider, namew), namew)}  {bar:<{barw}} "
                f"{money(float(it['cost'])):>11} {pct(float(it['cost']), total_cost):>5} "
                f"{human_tokens(int(it['tokens'])):>9} {int(it['runs']):>7}"
            )
        totals_row = None
        if len(all_rows) > 1:
            # The whole ranking, not the scrolled window (see trend_models).
            totals_row = (
                f"  {pad('TOTAL', namew)}  {'':{barw}} {money(total_cost):>11} {'':>5} "
                f"{human_tokens(sum(int(it['tokens']) for _, it in all_rows)):>9} "
                f"{sum(int(it['runs']) for _, it in all_rows):>7}"
            )
        lines = self._ruled_box("# Spend by provider", header, body, totals_row, notes, width)
        self._mark_trend_sort_header(columns)
        self._trend_rows_at = (self._ruled_body_start or 0, len(rows), start)
        return lines

    def trend_projects(self, width: int, height: int) -> list[str]:
        # The Trends overlay's project ranking: spend by directory over the whole range.
        # Names go through short_path rather than shorten, because a project row IS a
        # path: clipping the tail of ~/SoftwareProjects/opentab leaves every row reading
        # the same identical prefix with the only distinguishing part cut off. Same
        # $HOME fold and interior elision the Projects sidebar uses, so one project reads
        # the same in both places.
        return self._group_table(
            self.trend_ranked_rows("Projects"),
            width,
            "project",
            "Project",
            selectable=True,
            sort_tab="Projects",
            height=height,
            display=short_path,
        )

    def trend_sources(self, width: int, height: int) -> list[str]:
        # The Trends overlay's headline cut: spend by tool across the whole range.
        # Goes straight to _group_table with rows the App already ordered, rather than
        # through source_table: that one also serves the per-scope Harnesses tabs,
        # which have no cursor and no sort of their own -- a Trends sort must not
        # silently re-rank a month's breakdown.
        return self._group_table(
            self.trend_ranked_rows("Harnesses"),
            width,
            "harness",
            "Harness",
            selectable=True,
            sort_tab="Harnesses",
            height=height,
        )

    def source_table(
        self,
        workflows: list[Workflow],
        width: int,
        limit: int | None = None,
        selectable: bool = False,
    ) -> list[str]:
        # Spend grouped by the *tool* it came from (OpenCode / Claude Code / Codex).
        # Shared by the Trends "Harnesses" tab (whole range, selectable: the rows get
        # the trend cursor + Enter drill) and the per-month/day/project "Harnesses"
        # detail tabs (a scoped slice, plain). Subscription rows (Claude Code,
        # Codex) cost $0 until "$" reprices their tokens, so the bar reacts live.
        return self._group_table(
            self.source_rows(workflows), width, "harness", "Harness", limit, selectable
        )

    def machine_table(
        self,
        workflows: list[Workflow],
        width: int,
        limit: int | None = None,
        selectable: bool = False,
    ) -> list[str]:
        # The source_table twin for the fleet view: spend grouped by the *machine* it
        # ran on. Same rendering, different grouping -- the Trends "Machines" tab and
        # (later) the per-scope Machines detail tabs.
        return self._group_table(
            self.machine_rows(workflows), width, "machine", "Machine", limit, selectable
        )

    # What a _group_row spends on everything but the name and the bar: the marker, the
    # column gutters and the Cost/Share/Tokens/Sess cells. Both frames of this table --
    # the line-based preview (_group_table) and the zoom picker (_draw_dimension_picker)
    # -- size themselves with it, so a column can't shift on Enter.
    _GROUP_FIXED = 40

    # The name column reserves this much on top of its label, so the sort arrow the
    # Trends frame appends (" v") has somewhere to go. Reserved in BOTH frames, sorted
    # or not: the picker and the preview must measure the same pane, or a column shifts
    # on Enter.
    _SORT_ARROW_W = 2

    @staticmethod
    def _group_widths(rows: list, col: str, width: int, display=shorten) -> tuple[int, int]:
        # Size the name column to its HEADER too, not just the data: with every name
        # shorter than the label ("Harness", "Machine"), the header's own field overflowed
        # and shifted Cost/Share/Tokens/Sess right of the numbers they label. Short
        # hostnames make that the default in a fleet view. _model_table guards the same way.
        #
        # Measured on the name as DISPLAYED, not as stored: the Projects ranking shortens
        # paths with short_path, which folds $HOME first -- so a 34-column row measured
        # raw claims the whole cap, starves the bar to its 3-cell floor and leaves the
        # ranking with nothing to rank by. The cap is what display is asked to fit into,
        # so this can't recurse.
        cap = max(10, width - Renderer._GROUP_FIXED - 3)
        namew = min(
            max(
                [display_width(display(s, cap)) for s, _ in rows]
                + [len(col) + Renderer._SORT_ARROW_W]
            ),
            cap,
        )
        return namew, max(3, min(20, width - namew - Renderer._GROUP_FIXED))

    # The shared ranked table's sortable columns, in drawn order. Share is absent for
    # the same reason it is on the Models ranking: it is Cost as a percentage.
    _GROUP_SORT_COLUMNS = (("cost", "Cost"), ("tokens", "Tokens"), ("count", "Sess"))

    def _group_header(self, col: str, namew: int, barw: int, sort_tab: str | None = None) -> str:
        # `sort_tab` names the Trends ranking this header belongs to, and is what puts
        # the sort arrow on the active column; the per-scope tabs pass None and get the
        # plain header they always had.
        def head(key: str, label: str) -> str:
            return self.trend_sort_heading(key, label, sort_tab) if sort_tab else label

        return (
            f"  {head('name', col):<{namew}}  {'':{barw}} {head('cost', 'Cost'):>11} "
            f"{'Share':>5} {head('tokens', 'Tokens'):>9} {head('count', 'Sess'):>7}"
        )

    @staticmethod
    def _group_row(
        name: str,
        it,
        marker: str,
        namew: int,
        barw: int,
        peak: float,
        total: float,
        display=shorten,
    ) -> str:
        bar = "█" * max(0, round((float(it["cost"]) / peak) * barw))
        return (
            f"{marker} {display(name, namew):{namew}}  {bar:<{barw}} "
            f"{money(float(it['cost'])):>11} {pct(float(it['cost']), total):>5} "
            f"{human_tokens(int(it['tokens'])):>9} {int(it['sessions']):>7}"
        )

    def _group_table(
        self,
        all_rows: list,
        width: int,
        noun: str,
        col: str,
        limit: int | None = None,
        selectable: bool = False,
        sort_tab: str | None = None,
        height: int | None = None,
        display=shorten,
    ) -> list[str]:
        # The shared ranked-spend table behind source_table/machine_table: a name column,
        # a cost bar, then Cost/Share/Tokens/Sess, in the same ruled box every other table
        # wears. `noun` is the box title's word, `col` the name-column header. Selectable
        # rows carry the Trends cursor + Enter drill. The rows themselves come from the
        # builders above, which the zoom picker paints too. `sort_tab` (the Trends frame
        # only) arrows the active column and makes the header clickable. `display` is how
        # a name is fitted into the column -- shorten for the flat names, short_path for
        # the Projects ranking, whose rows are directories.
        title = f"# Spend by {noun}"
        if not all_rows:
            namew, barw = self._group_widths([], col, max(1, width - self.BOX_CHROME), display)
            return self._ruled_box(
                title, self._group_header(col, namew, barw, sort_tab), [], None, [], width
            ) + ["", "No sessions in the active range."]
        notes = self._unpriced_note(all_rows)
        if height is not None:  # the scrolling Trends frame: budget the box's own lines
            limit = self._ranked_row_budget(height, len(all_rows), len(notes))
        if selectable and limit is not None:
            _idx, start, shown = self._trend_cursor_window(len(all_rows), limit)
            rows = all_rows[start : start + shown]
        else:
            start = 0
            rows = all_rows if limit is None else all_rows[:limit]
        # Shares, bars and the TOTAL all measure the WHOLE ranking, never the scrolled
        # window: the window is a viewport onto it (trend_models' rule), and a total that
        # changed as you scrolled would be a different number every frame -- one that
        # disagreed with the Harnesses/Machines rollups everywhere else in the app.
        scope = all_rows if (selectable and limit is not None) else rows
        total_cost = sum(float(it["cost"]) for _, it in scope)
        peak = max((float(it["cost"]) for _, it in scope), default=0.0) or 1.0
        inner = max(1, width - self.BOX_CHROME)
        namew, barw = self._group_widths(rows, col, inner, display)
        body = [
            self._group_row(name, it, " ", namew, barw, peak, total_cost, display)
            for name, it in rows
        ]
        total = None
        if len(scope) > 1:
            # The TOTAL row every multi-row table closes with. Counts and tokens summed;
            # Share stays blank (it is definitionally 100%) and so does the bar, which
            # measures rows against the peak, not against the sum.
            ttok = sum(int(it["tokens"]) for _, it in scope)
            tses = sum(int(it["sessions"]) for _, it in scope)
            total = (
                f"  {pad('TOTAL', namew)}  {'':{barw}} {money(total_cost):>11} {'':>5} "
                f"{human_tokens(ttok):>9} {tses:>7}"
            )
        lines = self._ruled_box(
            title, self._group_header(col, namew, barw, sort_tab), body, total, notes, width
        )
        if sort_tab:
            self._mark_trend_sort_header((("name", col), *self._GROUP_SORT_COLUMNS))
        if selectable and self._ruled_body_start is not None:
            self._trend_rows_at = (self._ruled_body_start, len(rows), start)
        return lines

    def trend_machines(self, width: int, height: int) -> list[str]:
        # The Trends overlay's fleet cut: spend by machine across the whole range.
        # Pre-ordered by the App, like trend_sources (machine_table also serves the
        # per-scope Machines tabs, which carry no sort).
        return self._group_table(
            self.trend_ranked_rows("Machines"),
            width,
            "machine",
            "Machine",
            selectable=True,
            sort_tab="Machines",
            height=height,
        )

    def trend_drill_lines(self, width: int, height: int) -> list[str]:
        # A ranked row's sessions list (Enter on Models/Providers/Sources): every
        # root session in the active range that used it, with its cost/tokens
        # within the session, windowed around the cursor.
        kind, key = self.trend_drill
        rows = self.trend_drill_sessions()
        # A project key is a full path (the drill matches on it), so it is folded for
        # display like every other path on screen -- raw, it pushes the session count
        # and "most spend first" off the end of the box title that carries them.
        label = short_path(key, 44) if kind == "project" else key
        title = f"# Sessions · {label}"
        if not rows:
            return [title, "", f"No sessions used {label} in the active range."]
        subtotal = sum(cost for _w, cost, _t in rows)
        inner = max(1, width - self.BOX_CHROME)
        header = f"  {'Started':<10} {'Cost':>9} {'Tokens':>8}  {self.src_col()}{'Title'}"
        idx = max(0, min(self.app.trend_drill_index, len(rows) - 1))
        self.app.trend_drill_index = idx
        fit = self._ranked_row_budget(height, len(rows))  # the box's own lines
        start = max(0, min(idx - fit // 2, len(rows) - fit))
        shown = rows[start : start + min(fit, len(rows) - start)]
        body = [
            f"  {w.created_at[:10]:<10} {money(cost):>9} {human_tokens(tok):>8}  "
            f"{self.src_col(w)}{shorten(self.session_marks(w) + w.title, max(8, inner - 34))}"
            for w, cost, tok in shown
        ]
        totals_row = None
        if len(rows) > 1:
            # The whole drilled list, not the window (see trend_models).
            totals_row = (
                f"  {pad('TOTAL', 10)} {money(subtotal):>9} "
                f"{human_tokens(sum(t for _w, _c, t in rows)):>8}  "
            )
        lines = self._ruled_box(
            f"{title} · {len(rows)} session(s), most spend first",
            header,
            body,
            totals_row,
            [],
            width,
        )
        self._trend_rows_at = (self._ruled_body_start or 0, len(shown), start)
        return lines

    # The frame every panel/overlay/modal is drawn with: heavy box-drawing glyphs.
    # They are Unicode, so they need the same UTF-8 screen the block-glyph charts do
    # (cli.enable_unicode_locale forces one). Where they don't, the locale-independent
    # ACS line set is drawn instead -- a light frame beats a frame of garbage bytes,
    # which is what curses silently paints there (see util.unicode_screen: it does not
    # raise, so this has to be *asked* rather than caught). Resolved once, on the first
    # frame: a screen does not change its encoding mid-run.
    _HEAVY_FRAME = ("┏", "┓", "┗", "┛", "━", "┃")
    _heavy_frame: bool | None = None

    def frame_app(self, stdscr: curses.window, height: int, width: int) -> None:
        # The border around the whole UI, drawn in screen coordinates before draw()
        # shifts the origin into it. Structural grey (pair 4, the keybar's colour), never
        # the focus accent: it is chrome, not a panel that can take focus -- the active
        # panel has to stay the brightest border on screen.
        self.draw_frame(stdscr, 0, 0, height, width, curses.color_pair(4))

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
        self.draw_frame(stdscr, y, x, h, w, border_attr)
        self.write(stdscr, y, x + 2, f" {shorten(title, w - 6)} ", title_attr)

    def draw_frame(self, stdscr: curses.window, y: int, x: int, h: int, w: int, attr: int) -> None:
        if Renderer._heavy_frame is None:
            Renderer._heavy_frame = unicode_screen()
        if self._heavy_frame:
            try:
                self.frame(stdscr, y, x, h, w, attr, *self._HEAVY_FRAME)
                return
            except (UnicodeEncodeError, OverflowError):
                # Only a NARROW (non-ncursesw) curses build can get here: it encodes the
                # str itself, and complains either way -- UnicodeEncodeError when the
                # window's encoding has no such character, OverflowError when it has one
                # that doesn't fit a chtype's single byte (any multibyte glyph on a UTF-8
                # window). No build we ship on is narrow (windows-curses is PDC_WIDE +
                # HAVE_NCURSESW), and a wide one never raises here -- unicode_screen()
                # already ruled. Kept because the cost of being wrong is a crash on the
                # first frame, on a platform CI only smoke-tests the import of.
                Renderer._heavy_frame = False
        self.frame(
            stdscr,
            y,
            x,
            h,
            w,
            attr,
            curses.ACS_ULCORNER,
            curses.ACS_URCORNER,
            curses.ACS_LLCORNER,
            curses.ACS_LRCORNER,
            curses.ACS_HLINE,
            curses.ACS_VLINE,
        )

    def frame(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        h: int,
        w: int,
        attr: int,
        ul: int | str,
        ur: int | str,
        ll: int | str,
        lr: int | str,
        horiz: int | str,
        vert: int | str,
    ) -> None:
        y += self.oy
        x += self.ox
        stdscr.addch(y, x, ul, attr)
        stdscr.addch(y, x + w - 1, ur, attr)
        stdscr.addch(y + h - 1, x, ll, attr)
        # The app frame's lower-right corner is the screen's very last cell: curses puts
        # the glyph there and then reports an error because it cannot advance the cursor
        # past it. The cell is drawn; only the cursor move failed, so swallow it. (insch,
        # the usual escape hatch, is no good here -- it takes a chtype, i.e. one byte.)
        try:
            stdscr.addch(y + h - 1, x + w - 1, lr, attr)
        except curses.error:
            pass
        if isinstance(horiz, str):
            # hline/vline take a chtype -- a single *byte* -- so a multibyte glyph
            # raises OverflowError there. Run them through addstr/addch instead, the
            # same wide-character path every other Unicode glyph on screen takes. The
            # horizontal run stops one cell short of the right border, so it never
            # writes the last column of the screen (which addstr can't).
            stdscr.addstr(y, x + 1, horiz * (w - 2), attr)
            stdscr.addstr(y + h - 1, x + 1, horiz * (w - 2), attr)
            for row in range(y + 1, y + h - 1):
                stdscr.addch(row, x, vert, attr)
                stdscr.addch(row, x + w - 1, vert, attr)
        else:
            stdscr.hline(y, x + 1, horiz, w - 2, attr)
            stdscr.hline(y + h - 1, x + 1, horiz, w - 2, attr)
            stdscr.vline(y + 1, x, vert, h - 2, attr)
            stdscr.vline(y + 1, x + w - 1, vert, h - 2, attr)

    def hline(self, stdscr: curses.window, y: int, x: int, w: int) -> None:
        # A light rule across w content cells (the header/footer separators). Inside the
        # app frame the last content column is an ordinary cell, so the rule runs the
        # full width and meets the border instead of stopping a column short of it.
        stdscr.hline(y + self.oy, x + self.ox, curses.ACS_HLINE, max(0, w))

    def write(self, stdscr: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = stdscr.getmaxyx()
        y += self.oy
        x += self.ox
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        try:
            # Clip by display cells, not codepoints, so wide (CJK) text never
            # overflows the row and wraps. The clip is against the *screen* edge, so
            # inside the app frame it stops one cell short of the right border --
            # which is the cell the border itself occupies.
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
