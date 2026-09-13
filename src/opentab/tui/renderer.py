"""Renderer: all drawing."""
from __future__ import annotations

import textwrap
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING

from opentab import __version__
from opentab import pricing as pricing_ops
from opentab import util as util_ops
from opentab.models import (
    DaySummary,
    HarnessSummary,
    MachineSummary,
    MonthSummary,
    ProjectSummary,
    Workflow,
    YearSummary,
)
from opentab.themes import hex_rgb1000, ink_on, nearest_8, nearest_256, ramp
from opentab.tui import bindings, keymap
from opentab.tui.components import menus
from opentab.tui.components.bars import (
    legend_lines,
    segment_glyph,
    stack_line,
    stack_widths,
)
from opentab.tui.components.boxes import (
    BOX_CHROME as COMPONENT_BOX_CHROME,
)
from opentab.tui.components.boxes import (
    TABLE_GLYPHS,
    TABLE_GLYPHS_ASCII,
    ruled_box,
    sectioned_box,
)
from opentab.tui.components.boxes import (
    box_row as component_box_row,
)
from opentab.tui.components.boxes import (
    box_rule as component_box_rule,
)
from opentab.tui.components.boxes import (
    box_top as component_box_top,
)
from opentab.tui.components.charts import bar_chart, treemap_rects
from opentab.tui.components.modal import StyledLine, modal_layout
from opentab.tui.components.navigation import (
    keybar_layout,
    pager_layout,
    scrollbar_layout,
    scrollbar_thumb,
    tab_strip_layout,
)
from opentab.tui.components.notifications import (
    NOTIFICATION_STYLES,
    Notification,
    history_rows,
    toast_age,
    toast_cards,
    toast_history_viewport,
    wrap_notice,
)
from opentab.tui.components.tables import (
    PICKER_CHROME as COMPONENT_PICKER_CHROME,
)
from opentab.tui.components.tables import (
    SESSION_PROJECT_MAX as COMPONENT_SESSION_PROJECT_MAX,
)
from opentab.tui.components.tables import (
    SESSION_TITLE_MIN as COMPONENT_SESSION_TITLE_MIN,
)
from opentab.tui.components.tables import (
    ProjectHeadings,
    ProjectRow,
    SessionHeadings,
    SessionRow,
    picker_box_width,
    picker_frame,
    picker_row,
    picker_window,
    project_table_text,
)
from opentab.tui.components.tables import (
    group_header as table_group_header,
)
from opentab.tui.components.tables import (
    group_row as table_group_row,
)
from opentab.tui.components.tables import (
    group_table_layout as table_group_table_layout,
)
from opentab.tui.components.tables import (
    group_widths as table_group_widths,
)
from opentab.tui.components.tables import (
    project_header_text as table_project_header_text,
)
from opentab.tui.components.tables import (
    project_name_width as table_project_name_width,
)
from opentab.tui.components.tables import (
    project_row_text as table_project_row_text,
)
from opentab.tui.components.tables import (
    project_total_text as table_project_total_text,
)
from opentab.tui.components.tables import (
    session_columns as table_session_columns,
)
from opentab.tui.components.tables import (
    session_header_text as table_session_header_text,
)
from opentab.tui.components.tables import (
    session_row_text as table_session_row_text,
)
from opentab.tui.components.token_cards import (
    EconomicsCategory,
    token_breakdown_card,
    token_economics_card,
)
from opentab.tui.search_layout import conversation_layout, snippet_lines
from opentab.tui.trace import TraceLine, output_target
from opentab.tui.views import prices as price_view
from opentab.tui.views import subagents as subagents_view
from opentab.tui.views import tools as tools_view
from opentab.tui.views import trends as trend_views
from opentab.tui.views import turns as turns_view

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
    clip_tail,
    cost_bar,
    display_width,
    human_duration,
    human_tokens,
    iso_to_local,
    money,
    money_label,
    money_whole,
    pad,
    pct,
    relative_age,
    short_path,
    shorten,
    tokens,
    wrap_cells,
    wrap_lines,
)
from opentab.heatmap import (
    BLOCKS_UP,
    HEAT_EMPTY_GLYPH,
    PRICE_HEAT_BASE_PAIR,
    PRICE_HEAT_LEVELS,
    TOKEN_SERIES_BASE_PAIR,
    TOOL_HEAT_BASE_PAIR,
    TOOL_HEAT_LEVELS,
    heat_glyph,
    heat_palette,
    token_series,
    token_series_ansi,
)
from opentab.models import ALL_YEARS, year_label
from opentab.pricing import (
    TOKEN_TYPES,
    api_equivalent_cost,
    is_local_provider,
    model_context_window,
    model_price,
    price_source_meta,
)
from opentab.util import (
    CONTEXT_COMPACT_FLOOR,
    CONTEXT_COMPACT_RATIO,
    context_size,
    fuzzy_score,
    unicode_screen,
)
from opentab.whats_new import RELEASES_URL


def _turn_read_mark(row) -> str:
    return turns_view.turn_read_mark(row)


def _turn_agent(row) -> str:
    return turns_view.turn_agent(row)


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
    HARNESS_SORT_COLUMNS = (
        ("harness", "Harness"),
        ("cost", "Cost"),
        ("tokens", "Tokens"),
        ("sessions", "Ses"),
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
        self._turn_header_at: dict[int, int] = {}
        self._turn_layout_cache: tuple | None = None
        self._tool_layout_cache: tuple | None = None
        self._trace_layout_cache: tuple | None = None
        self._trace_tool_at: dict[int, int] = {}
        self._trace_output_ends: list[tuple[int, int]] = []
        # Selected prompt header line, recomputed each paint for scroll/highlight.
        self._turn_cursor_line: int | None = None
        self._tool_header_at: dict[int, int] = {}
        self._tool_call_at: dict[int, int] = {}
        self._tool_cursor_line: int | None = None
        self._subagent_header_at: dict[int, int] = {}
        self._subagent_cursor_line: int | None = None
        # Logical header lines become screen-coordinate sort regions during paint.
        self._line_sort_headers: dict[int, tuple[tuple, str]] = {}
        # Models remain plain strings; line-to-row mapping adds selection at paint time.
        self._model_row_at: dict[int, int] = {}
        self._model_cursor_line: int | None = None
        # Derive body offsets while building: empty bodies change box prologue length.
        self._ruled_body_start: int | None = None
        # Header identity is textual because independently built boxes are later stacked.
        self._box_headers: set[str] = set()
        # Years/Months/Days column widths, measured once per frame.
        self._period_cols: tuple[int, int, int] | None = None

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
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        h: int,
        w: int,
        lines: list[str],
        active: bool,
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
        self._paint_scrollbar(
            stdscr, y + 3, x + w - 1, len(lines), visible, self.scroll, active=active
        )

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

    # The three panels stack in one column, so they share a grid: the label field holds a
    # full date, and each numeric column is measured, not reserved, so no row carries
    # padding for a digit none of the data has.
    PERIOD_LABEL_W = len("2026-06-01")

    def period_columns(self) -> tuple[int, int, int]:
        if self._period_cols is None:
            rows = [*self.years, *self.months, *self.panel_days]
            self._period_cols = (
                max((len(money_whole(r.cost)) for r in rows), default=1),
                max((len(human_tokens(r.tokens)) for r in rows), default=1),
                max((len(str(r.workflows)) for r in rows), default=1),
            )
        return self._period_cols

    def period_row_width(self) -> int:
        # marker, label, the three measured columns, and the " ses" unit.
        cost_w, token_w, count_w = self.period_columns()
        return 2 + self.PERIOD_LABEL_W + 1 + cost_w + 1 + token_w + 1 + count_w + 4

    def _period_row_text(
        self, label: str, marker: str, cost: float, token_count: int, sessions: int
    ) -> str:
        cost_w, token_w, count_w = self.period_columns()
        return (
            f"{marker} {label:<{self.PERIOD_LABEL_W}} {money_whole(cost):>{cost_w}} "
            f"{human_tokens(token_count):>{token_w}} {sessions:>{count_w}} ses"
        )

    def year_row_text(self, year: YearSummary, marker: str) -> str:
        return self._period_row_text(
            year_label(year.year), marker, year.cost, year.tokens, year.workflows
        )

    def month_row_text(self, month: MonthSummary, marker: str) -> str:
        return self._period_row_text(month.month, marker, month.cost, month.tokens, month.workflows)

    def day_row_text(self, day: DaySummary, marker: str) -> str:
        return self._period_row_text(day.day, marker, day.cost, day.tokens, day.workflows)

    @staticmethod
    def project_name_width(width: int) -> int:
        return table_project_name_width(width)

    def _project_headings(self) -> ProjectHeadings:
        return ProjectHeadings(
            project=self.project_sort_heading("project", "Project"),
            cost=self.project_sort_heading("cost", "Cost"),
            tokens=self.project_sort_heading("tokens", "Tokens"),
            sessions=self.project_sort_heading("sessions", "Ses"),
            subagents=self.project_sort_heading("subagents", "Subagents"),
        )

    @staticmethod
    def _project_row(project: ProjectSummary) -> ProjectRow:
        return ProjectRow(
            name=project.directory,
            cost=money_whole(project.cost),
            tokens=human_tokens(project.tokens),
            sessions=project.workflows,
            subagents=project.subagents,
            ignored=project.ignored,
        )

    @staticmethod
    def _project_total_row(rows: list[ProjectSummary]) -> ProjectRow:
        return ProjectRow(
            name="TOTAL",
            cost=money_whole(sum(project.cost for project in rows)),
            tokens=human_tokens(sum(project.tokens for project in rows)),
            sessions=sum(project.workflows for project in rows),
            subagents=sum(project.subagents for project in rows),
        )

    def project_row_text(self, project: ProjectSummary, marker: str, width: int) -> str:
        return table_project_row_text(self._project_row(project), marker, width)

    def project_total_text(self, rows: list[ProjectSummary], width: int) -> str:
        # Pickers omit totals because a fixed footer would appear to sum only the window.
        return table_project_total_text(self._project_total_row(rows), width)

    def project_header_text(self, width: int) -> str:
        return table_project_header_text(self._project_headings(), width)

    def list_width(self, content: int, width: int) -> int:
        # Content plus the two box borders, never past the detail pane's 44-column floor.
        return max(24, min(content + 2, max(24, width - 44)))

    def projects_left_width(self, width: int) -> int:
        # Leave at least half the screen, and 44 columns, for detail.
        longest = max(
            (display_width(short_path(p.directory, 999)) for p in self.projects), default=8
        )
        natural = max(longest, len("Project")) + 39  # marker + Cost/Tokens/Ses/Subagents
        return max(24, min(natural, width // 2, max(24, width - 44)))

    @staticmethod
    def machine_name_width(width: int) -> int:
        return max(8, width - 27)

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
            f"{money_whole(machine.cost):>7} {human_tokens(machine.tokens):>6} "
            f"{machine.workflows:>3} ses"
        )

    def machine_header_text(self, width: int) -> str:
        name_width = self.machine_name_width(width)
        return f"  {'Machine':{name_width}} " f"{'Cost':>7} {'Tokens':>6} {'Ses':>7}"

    def machines_left_width(self, width: int) -> int:
        longest = max((display_width(m.name) for m in self.machines), default=8)
        # Include the badge and its space inside the name field budget.
        natural = max(longest, len("Machine")) + 31
        return max(24, min(natural, width // 2, max(24, width - 44)))

    @staticmethod
    def harness_name_width(width: int) -> int:
        return max(8, width - (25 if width >= 40 else 18))

    def harness_row_text(self, harness: HarnessSummary, marker: str, width: int) -> str:
        name_width = self.harness_name_width(width)
        badge = "∑ " if harness.aggregate else ""
        name = shorten(badge + harness.name, name_width)
        token_cell = f" {human_tokens(harness.tokens):>6}" if width >= 40 else ""
        return (
            f"{marker} {pad(name, name_width)} "
            f"{money_whole(harness.cost):>7}{token_cell} "
            f"{harness.workflows:>7}"
        )

    def harness_header_text(self, width: int) -> str:
        name_width = self.harness_name_width(width)
        token_cell = f" {self.harness_sort_heading('tokens', 'Tokens'):>6}" if width >= 40 else ""
        return (
            f"  {self.harness_sort_heading('harness', 'Harness'):{name_width}} "
            f"{self.harness_sort_heading('cost', 'Cost'):>7}{token_cell} "
            f"{self.harness_sort_heading('sessions', 'Ses'):>7}"
        )

    def harnesses_left_width(self, width: int) -> int:
        longest = max((display_width(h.name) for h in self.harnesses), default=8)
        natural = max(longest + 2, len("Harness")) + 31
        return max(24, min(natural, width // 2, max(24, width - 44)))

    def browse_left_width(self, width: int) -> int:
        if self.browse_mode == "machines":
            return self.machines_left_width(width)
        if self.browse_mode == "harnesses":
            return self.harnesses_left_width(width)
        if self.browse_mode == "projects":
            return self.projects_left_width(width)
        # Reserve the spend-bar lane without starving the detail pane below 44 columns.
        base = self.list_width(self.period_row_width(), width)
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

    def bar_lane(self, w: int) -> tuple[int, int]:
        # Keep bars outside row highlights, and drop the lane rather than clip a row: on a
        # cramped screen the numbers matter more than their bars.
        if (w - 2) - (BAR_CELLS + 2) < self.period_row_width():
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

    @staticmethod
    def _scrollbar_thumb(total: int, visible: int, offset: int) -> tuple[int, int] | None:
        """Return the thumb's row and height within a viewport-sized track."""
        return scrollbar_thumb(total, visible, offset)

    def _paint_scrollbar(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        total: int,
        visible: int,
        offset: int,
        active: bool = True,
    ) -> None:
        """Turn a pane's right border into a scrollbar without costing content width."""
        layout = scrollbar_layout(total, visible, offset)
        if layout is None:
            return
        track_glyph = getattr(curses, "ACS_VLINE", "|")
        thumb_glyph = getattr(curses, "ACS_BLOCK", getattr(curses, "ACS_CKBOARD", "#"))
        track_attr = curses.color_pair(4)
        thumb_attr = curses.color_pair(6 if active else 1) | curses.A_BOLD
        sy, sx = y + self.oy, x + self.ox
        try:
            # Keep this to three native runs; per-cell addch lets key-repeat outrun paint.
            for run in layout.runs:
                glyph = thumb_glyph if run.style == "thumb" else track_glyph
                attr = thumb_attr if run.style == "thumb" else track_attr
                stdscr.vline(sy + run.y, sx, glyph, run.length, attr)
        except curses.error:
            pass

    def current_pager_lines(self, width: int) -> list[str]:
        content_width = max(1, width - 4)
        if self.view == "session":
            workflow = self.current_session()
            if workflow is None:
                return []
            # Optional session tabs make name-based dispatch mandatory.
            tabs = self.current_tabs()
            current = tabs[self.tab % len(tabs)]
            if current == "Subagents" or self.app._on_turns_tab():
                lines = (
                    self.detail_subagents(workflow, content_width)
                    if current == "Subagents"
                    else self.detail_turns(workflow, content_width)
                )
                return (
                    lines[2:]
                    if self.app._on_turns_tab() and self.app.active_trace_drill is not None
                    else lines
                )
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
            if self.browse_mode == "harnesses":
                harness = self.selected_harness_summary
                if harness is None:
                    return []
                current = self.current_tabs()[self.tab % len(self.current_tabs())]
                if current == "Overview":
                    return self.harness_overview(harness, content_width)
                if current == "Models":
                    return self.harness_models(harness, content_width)
                if current == "Projects":
                    return self.harness_projects(harness, content_width)
                if current == "Machines":
                    return self.harness_machines(harness, content_width)
                return self.harness_workflows(harness, content_width)
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
        with self.app.session_selection():
            self._draw(stdscr)

    def _draw(self, stdscr: curses.window) -> None:
        if not self.app._on_turns_tab() or self.app.active_trace_drill is None:
            self.app._clear_trace_expansion()
        if not self.app._on_subagents_tab():
            self.app._clear_subagent_prompt()
        self.apply_background(stdscr)  # theme bg fills the screen (before erase reads it)
        stdscr.erase()
        self.regions = []  # rebuilt for this frame's clicks
        self.sort_regions = []
        self._line_sort_headers = {}
        self._box_headers = set()
        self._period_cols = None
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

        if keymap.in_conversation_search(self.app):
            self.draw_conversation_search(stdscr, height, width)
            if self.launch_menu is not None:
                self.draw_launch_menu(stdscr, height, width)
            self.draw_toasts(stdscr, height, width)
            stdscr.refresh()
            return
        self._search_layout_cache = None
        self.draw_header(stdscr, width)

        top = 3
        bottom = height - 2
        avail = bottom - top
        # Help floats over the body so its key context remains visible.
        # Whichever overlay was opened last paints; the other stays open underneath.
        if self.app.overlay_top == "prices":
            self.draw_prices(stdscr, top, bottom, width)
        elif self.app.overlay_top == "trends":
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
                elif self.browse_mode == "harnesses":
                    self.draw_harness_list(stdscr, top, 0, avail, left, active=False)
                elif self.browse_mode == "projects":
                    self.draw_project_list(stdscr, top, 0, avail, left, active=False)
                else:
                    self.draw_time_panels(stdscr, top, avail, left, focus=None)
                zx, zw = left, width - left
            if self.browse_mode == "machines":
                self.draw_machine_detail(stdscr, top, zx, avail, zw)
            elif self.browse_mode == "harnesses":
                self.draw_harness_detail(stdscr, top, zx, avail, zw)
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
        elif self.browse_mode == "harnesses":
            left = self.browse_left_width(width)
            self.draw_harness_list(stdscr, top, 0, avail, left)
            self.draw_harness_detail(stdscr, top, left, avail, width - left, active=False)
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

        # Reader actions depend on the output sections just laid out in the body.
        self.draw_footer(stdscr, height, width)
        if self.help:
            self.draw_help(stdscr, top, bottom, width)
        if self.toast_history:
            self.draw_toast_history(stdscr, top, bottom, width)
        if self.whats_new:
            self.draw_whats_new(stdscr, top, bottom, width)

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

        # Toasts float above modals, except their own history reader.
        self.draw_toasts(stdscr, height, width)

        stdscr.refresh()

    def draw_conversation_search(self, stdscr, height: int, width: int) -> None:
        ws = self.app.conversation_search
        accent = curses.color_pair(6) | curses.A_BOLD
        muted = curses.color_pair(4)

        if ws.consent:
            building = ws.status.get("exists") is False
            scope = f"Scope: {ws.source_key or 'current harness'} / {ws.scope_label}"
            lines = [
                ("Store sensitive messages as local plaintext.", curses.A_NORMAL),
                (snippet_lines(scope, 62, max_lines=1)[0].text, muted),
                ("Project / harness / session apply; dates do NOT.", muted),
                (
                    "Saved ignores are disabled (--no-state)."
                    if getattr(ws.args, "no_state", False)
                    else "Saved project and session ignores apply.",
                    muted,
                ),
                ("Source records stay read-only.", muted),
                ("Running updates may finish after search closes.", muted),
                ("", 0),
                (
                    " Enter / y   " + ("Build index " if building else "Update index "),
                    accent | curses.A_REVERSE,
                ),
                ("Esc / n   Cancel", muted),
            ]
            self.draw_modal(
                stdscr,
                height,
                width,
                "Build search index?" if building else "Update search index?",
                lines,
                center=True,
            )
            return

        def clean(value, cells):
            line = snippet_lines(str(value), max(1, cells), max_lines=1)
            return line[0].text if line else ""

        def text(y, value, attr=0):
            line = snippet_lines(str(value), width - 4, max_lines=1)
            if line:
                self.write(stdscr, y, 2, line[0].text, attr)

        selected_title = clean((ws.selected_hit or {}).get("title", ""), max(1, width - 30))
        conversation_available = ws.conversation_available
        conversation_tab = "Conversation"
        if selected_title and conversation_available:
            conversation_tab += f" · {selected_title}"
        self.draw_tabs(
            stdscr,
            0,
            1,
            width - 2,
            ("Results", conversation_tab),
            int(bool(ws.reader)),
            kind="searchtab",
            disabled={1} if not conversation_available else None,
        )

        value = f"{ws.filter_field}: {ws.filter_text}" if ws.filter_field else ws.query
        prefix = "Search: > " if ws.editing or ws.filter_field else "Search:   "
        query = snippet_lines(value, max(1, display_width(value)), max_lines=1)[0].text
        if ws.editing or ws.filter_field:
            query += "_"
        text(1, prefix + clip_tail(query, width - display_width(prefix) - 4), accent)
        self.regions.append(("search-query", 1, 0, width - 1, 0))

        scope = ws.scope
        values = [
            "Session" if scope.get("session") else "All",
            scope.get("harness") or "All",
            scope.get("project") or "Any",
            f"{scope.get('since') or '...'}..{scope.get('until') or '...'}"
            if scope.get("since") or scope.get("until")
            else "Any",
        ]
        prefixes = ("Scope: ", "Harness: ", "Project: ", "Dates: ")
        safe_values = [clean(value, max(1, width)) for value in values]
        gaps = 4
        value_budget = max(
            4, width - 4 - sum(map(display_width, prefixes)) - len("Reset") - gaps - 8
        )
        budgets = [1, 1, 1, 1]
        caps = (12, 16, 32, 24)
        while sum(budgets) < value_budget:
            grew = False
            for index, value in enumerate(safe_values):
                if budgets[index] < min(display_width(value), caps[index]):
                    budgets[index] += 1
                    grew = True
                    if sum(budgets) >= value_budget:
                        break
            if not grew:
                break
        chips = [
            prefix + shorten(value, budget) + " ▾"
            for prefix, value, budget in zip(prefixes, safe_values, budgets)
        ]
        chips.append("Reset")
        cx = 2
        for index, chip in enumerate(chips):
            if index:
                self.write(stdscr, 2, cx, " ", muted)
                cx += 1
            drawn = shorten(chip, max(0, width - 2 - cx))
            if not drawn:
                break
            chip_width = display_width(drawn)
            attr = muted | curses.A_UNDERLINE | (curses.A_BOLD if scope and index == 4 else 0)
            self.write(stdscr, 2, cx, drawn, attr)
            self.regions.append(("searchfilter", 2, cx, cx + chip_width - 1, index))
            cx += chip_width

        self.hline(stdscr, 4, 0, width)

        diagnostics = []
        response = ws.response
        if ws.busy:
            diagnostics.append(
                {
                    "search": "Searching",
                    "conversation": "Loading message",
                    "index": "Updating index",
                    "status": "Checking index",
                }.get(ws.busy, ws.busy)
                + "..."
            )
        if response.get("match_mode") == "any_term":
            diagnostics.append("Any-word fallback")
        if response.get("limited"):
            diagnostics.append("Bounded results: narrow scope for more")
        gaps = response.get("unindexed_roots", 0)
        stale = response.get("stale_executions_skipped", 0) + response.get(
            "stale_metadata_roots_skipped", 0
        )
        if gaps:
            diagnostics.append(f"{gaps} unindexed roots")
        if stale:
            diagnostics.append(f"{stale} stale sources withheld")
        if not diagnostics:
            index = response.get("index") or ws.status
            diagnostics.append(
                f"{index.get('roots', 0)} indexed roots"
                if index.get("exists")
                else (
                    "No index yet: leave typing with Tab, then use "
                    + (self._key("search", "index") or "the index action")
                    if index
                    else "Index coverage is checked when searching; no automatic refresh"
                )
            )
        text(
            3,
            " | ".join(diagnostics) + f"  /  Catalog: {ws.source_key or 'current harness'}",
            muted,
        )
        top, bottom = 5, height - 3
        if ws.reader:
            self._draw_search_preview(stdscr, top, 0, bottom - top, width)
        elif width >= 108:
            left = min(58, width * 2 // 5)
            self._draw_search_results(stdscr, top, 0, bottom - top, left)
            self._draw_search_preview(stdscr, top, left, bottom - top, width - left)
        else:
            # One compact result above the preview at the app's 80x20 minimum.
            results_h = max(4, (bottom - top) // 2)
            self._draw_search_results(stdscr, top, 0, results_h, width, compact=True)
            self._draw_search_preview(stdscr, top + results_h, 0, bottom - top - results_h, width)
        if ws.error:
            text(height - 3, ws.error, curses.color_pair(1) | curses.A_BOLD)
        elif ws.filter_field:
            text(
                height - 3,
                "Enter: apply / Esc: cancel. Empty project clears; dates: YYYY-MM-DD..YYYY-MM-DD",
                muted,
            )
        else:
            text(height - 3, ws.notice, muted)

        self.draw_footer(stdscr, height, width)
        filter_menu = ws.filter_menu
        if filter_menu:
            self.regions.clear()
            self._draw_search_filter_menu(stdscr, height, width, filter_menu)
        elif ws.help:
            self.regions.clear()
            self.draw_help(stdscr, 3, height - 2, width)

    def _draw_search_filter_menu(self, stdscr, height: int, width: int, menu: str) -> None:
        ws = self.app.conversation_search
        options = list(ws.filter_options())
        index = max(0, min(ws.filter_menu_index, max(0, len(options) - 1)))
        visible_count = max(1, height - 10)
        start = max(0, min(index - visible_count // 2, max(0, len(options) - visible_count)))
        visible = options[start : start + visible_count]
        muted = curses.color_pair(4)
        lines = [(f"Choose {menu}:", muted), ("", 0)]
        current = (
            "session"
            if menu == "scope" and ws.scope.get("session")
            else ws.scope.get(menu) or "all"
        )
        for offset, (value, label, enabled) in enumerate(visible, start=start):
            marker = ">" if offset == index else " "
            suffix = "  (current)" if value == current else ""
            if not enabled:
                suffix += "  (unavailable)"
            attr = curses.A_REVERSE | curses.A_BOLD if offset == index else curses.A_NORMAL
            if not enabled:
                attr |= muted | curses.A_DIM
            safe_label = snippet_lines(str(label), max(1, width - 16), max_lines=1)[0].text
            lines.append((f" {marker}  {safe_label}{suffix}", attr))
        title = self._menu_title(f"Filter {menu}", "menu")
        y, x, _h, w = self.draw_modal(stdscr, height, width, title, lines)
        for row, option_index in enumerate(range(start, start + len(visible)), start=y + 4):
            if options[option_index][2]:
                self.regions.append(("searchfilter-option", row, x, x + w - 1, option_index))

    def _draw_search_results(self, stdscr, y, x, height, width, compact=False):
        ws = self.app.conversation_search
        if height < 3:
            return
        title = f"MATCHES  {ws.selected + 1 if ws.hits else 0}/{len(ws.hits)}"
        self.box(
            stdscr, y, x, height, width, title, active=ws.focus == "results" and not ws.editing
        )
        inner = width - 6
        card = 3 if compact else 5
        ws.page_size = max(1, (height - 2) // card)
        ws.result_scroll = max(0, min(ws.result_scroll, ws.selected))
        if ws.selected >= ws.result_scroll + ws.page_size:
            ws.result_scroll = ws.selected - ws.page_size + 1
        # Register cards before the pane's catch-all, using the shared hit tester.
        for row_offset in range(1, min(height - 1, 1 + ws.page_size * card)):
            index = ws.result_scroll + (row_offset - 1) // card
            if index < len(ws.hits):
                self.regions.append(("search-result", y + row_offset, x, x + width - 1, index))
        self._add_rows_region("search-results", y, x, x + width - 1, 0, height)
        if not ws.hits:
            if ws.busy == "search" or ws._deadline is not None:
                message = "Looking for matching passages..."
            elif not ws.query.strip():
                message = "Type a few distinctive words to search."
            else:
                message = "No verified matches. Broaden scope or update the index."
            for offset, line in enumerate(snippet_lines(message, inner, max_lines=height - 2)):
                self._write_search_line(stdscr, y + 1 + offset, x + 3, line)
            return
        row = y + 1
        for index in range(ws.result_scroll, min(len(ws.hits), ws.result_scroll + ws.page_size)):
            hit = ws.hits[index]
            selected = index == ws.selected
            attr = curses.color_pair(6) | curses.A_BOLD if selected else curses.A_BOLD
            title = snippet_lines(hit.get("title") or "Untitled session", inner, ws.query, 1)[0]
            self.write(stdscr, row, x + 1, ">" if selected else " ", attr)
            self._write_search_line(stdscr, row, x + 3, title, attr)
            if row + 1 < y + height - 1:
                stamp = hit.get("timestamp")
                if isinstance(stamp, (int, float)):
                    try:
                        stamp = datetime.fromtimestamp(stamp / 1000).strftime("%Y-%m-%d")
                    except (OSError, ValueError, OverflowError):
                        stamp = ""
                meta = f"{hit.get('harness', '')} / {str(stamp or '')[:10]} / {short_path(hit.get('project') or '', 24)}"
                if hit.get("match_fields") == ["title"]:
                    meta += " / title match"
                self._write_search_line(
                    stdscr,
                    row + 1,
                    x + 3,
                    snippet_lines(meta, inner, max_lines=1)[0],
                    curses.color_pair(4),
                )
            for offset, line in enumerate(
                snippet_lines(hit.get("excerpt") or "", inner, ws.query, 1 if compact else 2)
            ):
                if row + 2 + offset < y + height - 1:
                    self._write_search_line(stdscr, row + 2 + offset, x + 3, line)
            row += card

    def _draw_search_preview(self, stdscr, y, x, height, width):
        ws = self.app.conversation_search
        if height < 3:
            return
        self._add_rows_region("search-preview", y, x, x + width - 1, 0, height)
        title = "CONVERSATION" if ws.reader else "MESSAGE PREVIEW"
        if ws.reader and ws.selected_hit:
            session_title = snippet_lines(
                str(ws.selected_hit.get("title") or ""), max(1, width - 24), max_lines=1
            )[0].text
            if session_title:
                title += f" · {session_title}"
        self.box(
            stdscr,
            y,
            x,
            height,
            width,
            title,
            active=ws.reader or (ws.focus == "preview" and not ws.editing),
        )
        inner = width - 6
        ws.preview_height = max(1, height - 2)
        cache_key = (id(ws.preview), inner, ws.query)
        cache = getattr(self, "_search_layout_cache", None)
        if cache is None or cache[0] != cache_key:
            layout = conversation_layout(ws.preview, inner, ws.query)
            anchor = ws.preview_anchor or (cache[3] if cache and cache[2] is ws.preview else None)
            self._search_layout_cache = (cache_key, layout, ws.preview, anchor)
        else:
            layout = cache[1]
            anchor = ws.preview_anchor or cache[3]
            self._search_layout_cache = (cache_key, layout, ws.preview, anchor)
        ws.preview_lines = len(layout.lines)
        if ws.preview_anchor:
            ws.preview_scroll = layout.anchors.get(ws.preview_anchor, 0)
            ws.preview_anchor = None
        # Keep the requested message at the top even when the page ends below it.
        # Ordinary bottom clamping would pull unrelated retention notices into view.
        max_scroll = max(
            0,
            len(layout.lines) - ws.preview_height,
            layout.anchors.get(anchor or (ws.selected_hit or {}).get("anchor"), 0),
        )
        ws.preview_scroll = max(0, min(ws.preview_scroll, max_scroll))
        if not layout.lines:
            message = (
                "Loading matched message..."
                if ws.busy == "conversation"
                else "Select a passage to read its conversation."
            )
            lines = snippet_lines(message, inner, max_lines=ws.preview_height)
        else:
            lines = layout.lines[ws.preview_scroll : ws.preview_scroll + ws.preview_height]
        for offset, line in enumerate(lines):
            self._write_search_line(stdscr, y + 1 + offset, x + 3, line)
        if layout.lines:
            position = f" {ws.preview_scroll + 1}-{min(len(layout.lines), ws.preview_scroll + ws.preview_height)}/{len(layout.lines)} "
            self.write(
                stdscr,
                y + height - 1,
                x + max(2, width - len(position) - 2),
                position,
                curses.color_pair(4),
            )

    def _write_search_line(self, stdscr, y, x, line, attr=None):
        if attr is None:
            attr = {
                "user": curses.color_pair(6) | curses.A_BOLD,
                "assistant": curses.color_pair(2) | curses.A_BOLD,
                "meta": curses.color_pair(4),
                "code": curses.color_pair(3),
            }.get(line.role, 0)
        self.write(stdscr, y, x, line.text, attr)
        for start, end in line.highlights:
            self.write(
                stdscr,
                y,
                x + display_width(line.text[:start]),
                line.text[start:end],
                attr | curses.A_BOLD | curses.A_UNDERLINE,
            )

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
        self.draw_tabs(
            stdscr,
            y,
            0,
            width,
            tuple(lbl for lbl, _m in tabs),
            active_index,
            kind="modetab",
            rule=True,
        )

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
        if self.app.active_subagent_turns:
            # Keep execution identity ahead of inherited scope so narrow readers retain it.
            segs += ["Subagents", shorten(self.app.subagent_turns_title(), 28), "Turns"]
            if self.app.active_turn_drill is not None:
                segs.append(f"Prompt {self.app.active_turn_drill + 1}")
            return sep.join(segs)
        if self.view == "session" and tab_name == "Tools" and self.app.active_tool_drill:
            kind, name = self.app.active_tool_drill
            segs += ["Tools", f"{kind}: {shorten(name, 28)}"]
            return sep.join(segs)
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
        if self.browse_mode == "harnesses" and self.view != "session":
            harness = self.selected_harness_summary
            segs.append("harnesses")
            if harness:
                segs.append(f"∑ {harness.name}" if harness.aggregate else harness.name)
            segs.extend(self._drill_crumbs(self.on_sessions_tab or bool(self.zoom_model)))
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
            elif self.browse_mode == "harnesses":
                harness = self.selected_harness_summary
                if harness:
                    segs.append(f"∑ {harness.name}" if harness.aggregate else harness.name)
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
            if tab_name == "Turns" and self.app.active_trace_drill is not None:
                segs.append(f"Prompt {self.app.active_turn_drill + 1}")
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
        if self.filter_active and not keymap.in_conversation_search(self.app):
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
        # Reserve the version slot before drawing the keybar, then paint it last. The bar
        # centres on the whole row, not on what the version leaves, so it lines up with
        # the centred mode tabs above it.
        ver = f" v{__version__} "
        if len(ver) + 4 < width:
            self.draw_keybar(stdscr, height - 1, width, parts, limit=width - len(ver))
            self.write(stdscr, height - 1, width - len(ver), ver, curses.color_pair(1))
        else:
            self.draw_keybar(stdscr, height - 1, width, parts)

    def draw_keybar(self, stdscr: curses.window, y: int, width: int, parts, limit: int = 0) -> None:
        # Entries may contain contiguous sub-segments so only the active token is accented.
        base = curses.color_pair(4)
        active = curses.color_pair(6) | curses.A_BOLD
        layout = keybar_layout(parts, width, limit=limit or None)
        for span in layout.spans:
            attr = active if span.style == "active" else base
            self.write(stdscr, y, span.x, span.text, attr)

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

    def harness_sort_heading(self, key: str, label: str) -> str:
        if self.harness_sort_key() != key:
            return label
        desc = self.sort_descending(key, self.harness_sort_reverse)
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
        return self.browse_mode in ("projects", "harnesses", "machines") or self.focus != "days"

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
    SESSION_TITLE_MIN = COMPONENT_SESSION_TITLE_MIN
    SESSION_PROJECT_MAX = COMPONENT_SESSION_PROJECT_MAX

    def session_columns(self, sessions: list[Workflow], width: int) -> tuple[bool, int, bool]:
        span_projects = self.sessions_span_projects()
        return table_session_columns(
            [self.session_project(workflow) for workflow in sessions] if span_projects else (),
            width,
            span_projects,
            self.view == "zoom" and bool(self.zoom_model),
            self._session_headings(),
        )

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

    def _session_headings(self) -> SessionHeadings:
        cost, token, cost_w, token_w = self.session_metric_headings()
        return SessionHeadings(
            date=self.sort_heading(*self.session_date_column()),
            duration=self.sort_heading("duration", "Worked"),
            cost=cost,
            tokens=token,
            subagents=self.sort_heading("subagents", "Subagents"),
            project=self.sort_heading("project", "Project"),
            title=self.sort_heading("title", "Title"),
            cost_width=cost_w,
            token_width=token_w,
            source_column=self.src_col(),
            machine_column=self.mach_col(),
        )

    def session_header_text(self, models: bool, proj_w: int, dur: bool = True) -> str:
        return table_session_header_text(self._session_headings(), models, proj_w, dur)

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
        _cost, _token, cost_w, token_w = self.session_metric_headings()
        return table_session_row_text(
            SessionRow(
                date=self.session_date_cell(workflow),
                duration=self.session_duration(workflow) if dur else "",
                cost=cost_text,
                tokens=human_tokens(token_count),
                subagents=workflow.subagents,
                model_count=workflow.model_count,
                source_column=self.src_col(workflow),
                machine_column=self.mach_col(workflow),
                project=self.session_project(workflow) if proj_w else "",
                marks=self.session_marks(workflow),
                ignored=self.ignored_session_tag(workflow),
                title=workflow.title,
            ),
            marker,
            models,
            proj_w,
            cost_w,
            token_w,
            dur,
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
        if isinstance(line, TraceLine):
            if line.role == "error":
                return curses.color_pair(4) | curses.A_BOLD
            if line.role == "tool":
                return curses.color_pair(6) | curses.A_BOLD
            if line.role == "heading":
                return curses.A_BOLD
            if line.role in ("reasoning", "output", "meta"):
                return curses.color_pair(1)
            return curses.A_NORMAL
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
        # $0.00/$0 mean zero/unpriced; <$0.01 is real spend and must remain emphasized.
        if cost_text in ("$0.00", "$0"):
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
    PICKER_CHROME = COMPONENT_PICKER_CHROME

    def picker_box_width(self, w: int) -> int:
        return picker_box_width(w)

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
        frame = picker_frame(cy, x, w, title, header, nrows, self.box_glyphs())
        self.write(
            stdscr,
            frame.top_y,
            frame.frame_x,
            frame.top,
            curses.color_pair(2) | curses.A_BOLD,
        )
        self._paint_box_header(
            stdscr, frame.header_y, frame.frame_x, frame.header, frame.outer_width
        )
        if sort_columns:
            self._register_sort_header(
                frame.header_y,
                frame.content_x,
                header,
                sort_columns,
                sort_target,
                frame.inner_width,
            )
        self.write(stdscr, frame.rule_y, frame.frame_x, frame.rule, curses.A_NORMAL)
        self.write(stdscr, frame.bottom_y, frame.frame_x, frame.bottom, curses.A_NORMAL)
        return frame.body_y, frame.content_x, frame.inner_width

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
        row = picker_row(ry, x, cx, inner, text, selected, self.box_glyphs())
        self.write(stdscr, row.y, row.left_x, row.left, curses.A_NORMAL)
        self.write(stdscr, row.y, row.right_x, row.right, curses.A_NORMAL)
        if row.selected:
            self.paint_cursor_row(stdscr, row.y, row.content_x, row.content, inner, bars=bars)
        elif cost or token_text:
            self.write_colored_summary_row(
                stdscr, row.y, row.content_x, row.content, cost, token_text, inner
            )
        else:
            self.write_rich(stdscr, row.y, row.content_x, row.content)

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
        idx, start, count = picker_window(len(sessions), self.workflow_index, h)
        shown = sessions[start : start + count]
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
        self._paint_scrollbar(stdscr, body_y, x + w - 3, len(sessions), len(shown), start)

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
        idx, start, count = picker_window(len(projects), self.project_index, h)
        shown = projects[start : start + count]
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
        self._paint_scrollbar(stdscr, body_y, x + w - 3, len(projects), len(shown), start)

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
        idx, start, count = picker_window(len(rows), sel_index, h)
        shown = rows[start : start + count]
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
        self._paint_scrollbar(stdscr, body_y, x + w - 3, len(rows), len(shown), start)
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
        rule: bool = False,
        disabled: set[int] | None = None,
    ) -> None:
        # Brackets preserve active-tab state in monochrome or pair-starved terminals.
        # `rule` is the browse-mode bar's shape -- chips centered in a horizontal rule,
        # which fills the row instead of leaving it to a key hint. It implies centering
        # and the tighter one-cell gap, so the chips read as one band.
        layout = tab_strip_layout(
            tabs,
            active_index,
            width,
            center=center,
            rule=rule,
            disabled=disabled or (),
        )
        if not layout.spans and not layout.rules:
            return
        attrs = {
            "separator": curses.A_NORMAL,
            "disabled": curses.color_pair(4) | curses.A_DIM,
            "active": curses.color_pair(7) | curses.A_BOLD,
            "inactive": curses.color_pair(self._TAB_PAIR),
        }
        for rule_x, length in layout.rules:
            self.hline(stdscr, y, x + rule_x, length)
        for span in layout.spans:
            self.write(stdscr, y, x + span.x, span.text, attrs[span.style])
        for hit in layout.hits:
            self.regions.append((kind, y, x + hit.x0, x + hit.x1, hit.index))

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
            cost = money_whole(year.cost)
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
            cost = money_whole(month.cost)
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
            cost = money_whole(project.cost)
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

        self.draw_tabs(stdscr, y + 1, x + 1, w - 2, self.current_tabs(), self.tab, rule=True)

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

        self._paint_detail_lines(stdscr, y, x, h, w, lines, active)

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
                    money_whole(machine.cost),
                    human_tokens(machine.tokens),
                    w - 2,
                )

    def draw_harness_list(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        self.box(stdscr, y, x, h, w, self.panel_title(1, "Harnesses", active), active=active)
        rows = self.harnesses
        header = self.harness_header_text(w - 2)
        self._paint_box_header(stdscr, y + 1, x + 1, header, w - 2)
        self._register_sort_header(
            y + 1, x + 1, header, self.HARNESS_SORT_COLUMNS, "harness", w - 2
        )
        visible = h - 4
        start = max(0, min(self.harness_index - visible // 2, max(0, len(rows) - visible)))
        self._add_rows_region(
            "harness", y + 3, x, x + w - 1, start, len(rows[start : start + visible])
        )
        for row_y, harness in enumerate(rows[start : start + visible], y + 3):
            selected = start + row_y - (y + 3) == self.harness_index
            text = self.harness_row_text(harness, ">" if selected else " ", w - 2)
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
                    money_whole(harness.cost),
                    human_tokens(harness.tokens),
                    w - 2,
                )

    def draw_harness_detail(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        harness = self.selected_harness_summary
        title = (
            "Harness"
            if harness is None
            else "All harnesses"
            if harness.aggregate
            else f"Harness {shorten(harness.name, max(8, w - 12))}"
        )
        self.box(stdscr, y, x, h, w, self.panel_title(0, title), active=active)
        if harness is None:
            self.write(stdscr, y + 2, x + 2, "No harness selected.", curses.color_pair(1))
            return
        self.draw_tabs(stdscr, y + 1, x + 1, w - 2, self.current_tabs(), self.tab, rule=True)
        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Projects" and self.view == "zoom":
            self.draw_projects_picker(stdscr, y, x, h, w)
            return
        if current == "Machines" and self.view == "zoom":
            self.draw_machines_picker(stdscr, y, x, h, w)
            return
        if current == "Economics":
            lines = self.model_scope_overview(w - 4)
        elif current == "Overview":
            lines = self.harness_overview(harness, w - 4)
        elif current == "Models":
            lines = self.harness_models(harness, w - 4)
        elif current == "Projects":
            lines = self.harness_projects(harness, w - 4)
        elif current == "Machines":
            lines = self.harness_machines(harness, w - 4)
        else:
            lines = self.harness_workflows(harness, w - 4)
        self._paint_detail_lines(stdscr, y, x, h, w, lines, active)

    def harness_overview(self, harness: HarnessSummary, width: int) -> list[str]:
        workflows = self.harness_scope(harness, include_ignored=self.show_ignored_projects)
        if harness.aggregate:
            lines = self._stat_card(
                "# Harnesses",
                [
                    f"Harnesses:     {max(0, len(self.harnesses) - 1)}",
                    f"Cost:          {money(harness.cost)}",
                    f"Tokens:        {tokens(harness.tokens)}",
                    f"Sessions:      {harness.workflows}",
                    f"Subagents:     {harness.subagents}",
                    f"Last active:   {harness.last_active[:16]}",
                ],
                width,
            )
            lines += ["", *self.source_table(workflows, width)]
        else:
            share_total = (
                sum(w.total_cost for w in self.ranged_workflows)
                if self.show_ignored_projects
                else self.range_cost_total()
            )
            lines = self._stat_card(
                "# Harness",
                [
                    f"Harness:       {harness.name}",
                    f"Cost:          {money(harness.cost)}",
                    f"Share:         {pct(harness.cost, share_total)}",
                    f"Tokens:        {tokens(harness.tokens)}",
                    f"Sessions:      {harness.workflows}",
                    f"Subagents:     {harness.subagents}",
                    f"Last active:   {harness.last_active[:16]}",
                ],
                width,
                [self.unpriced_hint()] if harness.unpriced_tokens else [],
            )
        if not getattr(self.store, "combined", False):
            key = self._key("main", "harness")
            lines += [
                "",
                *textwrap.wrap(
                    f"Only this harness is loaded. Use {key} to combine detected tools when available.",
                    max(12, width),
                ),
            ]
        lines += ["", *self._token_economics_box(workflows, width)]
        if self.projects_for_workflows(workflows):
            lines += ["", *self._top_projects_box(workflows, harness.cost, width)]
        lines += [
            "",
            *self._model_table(
                self._agg_rows(self.aggregate_models(workflows)), "# Top Models", width
            ),
        ]
        return lines

    def harness_models(self, harness: HarnessSummary, width: int) -> list[str]:
        rows = self.compose_zoom_drills(
            self.harness_scope(harness, include_ignored=self.show_ignored_projects)
        )
        return self._models_tab(
            self._agg_rows(self.aggregate_models(rows)), "# Harness Model Spend", width
        )

    def harness_projects(self, harness: HarnessSummary, width: int) -> list[str]:
        rows = self.harness_scope(harness, include_ignored=self.show_ignored_projects)
        return self.project_table(
            self.projects_for_workflows(rows, include_ignored=self.show_ignored_projects), width
        )

    def harness_machines(self, harness: HarnessSummary, width: int) -> list[str]:
        return self.machine_table(
            self.scoped_sessions(
                self.harness_scope(harness, include_ignored=self.show_ignored_projects)
            ),
            width,
        )

    def harness_workflows(self, harness: HarnessSummary, width: int) -> list[str]:
        return self.session_table(
            self.harness_scope(harness, include_ignored=self.show_ignored_projects), width
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

        self.draw_tabs(stdscr, y + 1, x + 1, w - 2, self.current_tabs(), self.tab, rule=True)

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

        self._paint_detail_lines(stdscr, y, x, h, w, lines, active)

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

        self.draw_tabs(stdscr, y + 1, x + 1, w - 2, self.current_tabs(), self.tab, rule=True)

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

        self._paint_detail_lines(stdscr, y, x, h, w, lines, active)

    def draw_month_detail(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        month = self.selected_month_summary
        title = "Month" if month is None else f"Month {month.month}"
        self.box(stdscr, y, x, h, w, self.panel_title(0, title), active=active)
        if month is None:
            self.write(stdscr, y + 2, x + 2, "No month selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 1, w - 2, self.current_tabs(), self.tab, rule=True)

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

        self._paint_detail_lines(stdscr, y, x, h, w, lines, active)

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
            cost = money_whole(day.cost)
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

        self.draw_tabs(stdscr, y + 1, x + 1, w - 2, self.current_tabs(), self.tab, rule=True)

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

        self._paint_detail_lines(stdscr, y, x, h, w, lines, active)

    def draw_detail(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        workflow = self.current_session()
        title = (
            "Detail"
            if workflow is None
            else shorten(self.session_marks(workflow) + workflow.title, max(10, w - 12))
        )
        reading = self.app._on_turns_tab() and self.app.active_trace_drill is not None
        if reading:
            self.draw_frame(stdscr, y, x, h, w, curses.color_pair(1))
            self.write(stdscr, y, x + 2, f" {title} ", curses.color_pair(1))
        else:
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
        self.draw_tabs(stdscr, y + 1, x + 1, w - 2, tabs, self.tab, rule=True)

        current = tabs[self.tab % len(tabs)]
        visible = h - 4
        if (
            self.app._on_turns_tab()
            and self.app.active_trace_drill is not None
            and not self.app.trace_data_ready(workflow.id)
            and not self.app.trace_expanded
            and self.app.session_supports_trace(workflow.id)
            and not self.app.remote_trace_reader(workflow.id)
        ):
            self.app._trace_loading = (workflow.id, "")
        if current == "Subagents":
            lines = self.detail_subagents(workflow, w - 4)
        elif self.app._on_turns_tab():
            lines = self.detail_turns(workflow, w - 4)
        elif current == "Tools":
            # Budget the treemap's largest chrome form so the first exact row stays visible.
            lines = self.detail_tools(workflow, w - 4, max(0, visible - 12))
        elif current == "Context":
            lines = self.detail_context(workflow, w - 4)
        else:
            lines = self.detail_overview(workflow, w - 4)

        turns = self.app._on_turns_tab()
        tracing = turns and self.app.active_trace_drill is not None
        body_start = 0
        if tracing and lines:
            # The turn's identity stays above the scrolling transcript, below the tabs.
            self.write(stdscr, y + 2, x + 2, shorten(lines[0], w - 4), curses.A_BOLD)
            body_start = 2

        if turns and self.app._turn_follow:
            # Follow is one-shot and must run before the scroll clamp.
            self._scroll_turn_cursor_into_view(visible)
            self.app._turn_follow = False
        if current == "Tools" and self.app._tool_follow:
            self._scroll_line_into_view(self._tool_cursor_line, visible)
            self.app._tool_follow = False
        if current == "Subagents" and not turns and self.app._subagent_follow:
            self._scroll_line_into_view(self._subagent_cursor_line, visible)
            self.app._subagent_follow = False
        loading_content = (tracing and self.app._trace_loading is not None) or (
            current == "Subagents" and not turns and self.app._subagent_prompt_loading is not None
        )
        # A temporary prompt placeholder must not clamp the restored detail scroll.
        if not loading_content:
            self.app.scroll = max(
                0, min(self.app.scroll, max(0, len(lines) - body_start - visible))
            )
        paint_scroll = 0 if loading_content else self.scroll
        drawn = lines[body_start + paint_scroll : body_start + paint_scroll + visible]
        target = self.trace_output_target() if tracing else None
        for offset, line in enumerate(drawn):
            attr = self.line_attr(line)
            if (
                (turns and self.scroll + offset == self._turn_cursor_line)
                or (
                    current == "Subagents"
                    and not turns
                    and self.scroll + offset == self._subagent_cursor_line
                )
                or (current == "Tools" and self.scroll + offset == self._tool_cursor_line)
            ):
                # Select by line index, not a display glyph. paint_cursor_row preserves
                # gutters and prevents rich number colors from shredding the highlight.
                self.paint_cursor_row(stdscr, y + 3 + offset, x + 2, line, w - 4)
                continue
            if not tracing and (
                line in self._box_headers or self.scroll + offset in self._line_sort_headers
            ):
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
            if tracing:
                if isinstance(line, TraceLine) and line.event is not None:
                    if line.role in ("tool", "error"):
                        line = ("▸" if line.event == target else "·") + line[1:]
                    elif (
                        line.role == "meta"
                        and line.event != target
                        and line.startswith("│  Output ·")
                    ):
                        line = " · ".join(line.split(" · ")[:2])
                # $1 in a shell script is not money, and 1.0M in output is not a token count.
                self.write(stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), attr)
                # Trace prose deliberately bypasses write_rich, but numeric token bands
                # still need their semantic colors overpainted explicitly.
                self._paint_token_runs(stdscr, y + 3 + offset, x + 2, line, w - 4)
                event = getattr(drawn[offset], "event", None)
                if event is not None:
                    self._add_rows_region(
                        "trace-output", y + 3 + offset, x + 2, x + w - 3, event, 1
                    )
                continue
            self.write_rich(stdscr, y + 3 + offset, x + 2, shorten(line, w - 4), attr)
            self._paint_token_runs(stdscr, y + 3 + offset, x + 2, line, w - 4)
            if current == "Tools":
                self._paint_tool_tree_runs(
                    stdscr, y + 3 + offset, x + 2, self.scroll + offset, line, w - 4
                )
            self._register_line_sort_header(
                y + 3 + offset, x + 2, self.scroll + offset, line, w - 4
            )
        if turns:
            self._add_rows_region("turnline", y + 3, x + 2, x + w - 3, self.scroll, len(drawn))
        if current == "Subagents" and not turns:
            self._add_rows_region("subagentline", y + 3, x + 2, x + w - 3, self.scroll, len(drawn))
        if current == "Tools":
            kind = "toolcallline" if self.app.active_tool_drill is not None else "toolline"
            self._add_rows_region(kind, y + 3, x + 2, x + w - 3, self.scroll, len(drawn))
        if not loading_content:
            self._paint_scrollbar(
                stdscr, y + 3, x + w - 1, len(lines) - body_start, visible, self.scroll
            )

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
    _TABLE_GLYPHS = TABLE_GLYPHS
    _TABLE_GLYPHS_ASCII = TABLE_GLYPHS_ASCII

    # Static tables and scrolling pickers share these pieces despite building at different
    # times. BOX_CHROME is the width callers must reserve for the frame.

    BOX_CHROME = COMPONENT_BOX_CHROME

    @classmethod
    def box_glyphs(cls) -> dict:
        return cls._TABLE_GLYPHS if unicode_screen() else cls._TABLE_GLYPHS_ASCII

    @classmethod
    def box_top(cls, title: str, width: int) -> str:
        return component_box_top(title, width, cls.box_glyphs())

    @classmethod
    def box_rule(cls, width: int, left: str = "lt", right: str = "rt") -> str:
        return component_box_rule(width, cls.box_glyphs(), left, right)

    @classmethod
    def box_row(cls, text: str, width: int) -> str:
        return component_box_row(text, width, cls.box_glyphs())

    def _ruled_box(
        self,
        title: str,
        header: str,
        body: list[str],
        total: str | None,
        notes: list[str],
        width: int,
    ) -> list[str]:
        layout = ruled_box(title, header, body, total, notes, width, self.box_glyphs())
        self._ruled_body_start = layout.body_start
        if layout.header_line is not None:
            self._box_headers.add(layout.lines[layout.header_line])
        return list(layout.lines)

    def _sectioned_box(
        self, title: str, groups: list[list[str]], width: int, notes: list[str]
    ) -> list[str]:
        return list(sectioned_box(title, groups, width, notes, self.box_glyphs()).lines)

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
        return segment_glyph(slot, colored=self._token_series_ok)

    @staticmethod
    def _stack_widths(rows, total: float, cells: int) -> list[int]:
        return stack_widths(rows, total, cells)

    def _token_stack_line(self, rows, total: float, cells: int, labels=None, share_fmt=None) -> str:
        line = stack_line(
            rows,
            total,
            cells,
            colored=self._token_series_ok,
            labels=labels,
            share_formatter=share_fmt,
        )
        self._token_runs[line.text] = [(span.column, span.length, span.slot) for span in line.spans]
        return line.text

    def _token_legend_lines(self, rows, inner: int) -> list[str]:
        lines = legend_lines(
            [(label, slot) for label, _toks, _cost, slot in rows],
            inner,
            colored=self._token_series_ok,
        )
        for line in lines:
            self._token_runs[line.text] = [
                (span.column, span.length, span.slot) for span in line.spans
            ]
        return [line.text for line in lines]

    def _adopt_token_card(self, card) -> list[list[str]]:
        groups = []
        for group in card.groups:
            lines = []
            for line in group:
                if line.spans:
                    self._token_runs[line.text] = [
                        (span.column, span.length, span.slot) for span in line.spans
                    ]
                lines.append(line.text)
            groups.append(lines)
        return groups

    def _token_breakdown_box(
        self,
        usage: dict,
        title: str,
        width: int,
        *,
        attributed: bool = False,
        calls: int = 0,
        notes: tuple[str, ...] = (),
    ) -> list[str]:
        inner = max(1, width - self.BOX_CHROME)
        card = token_breakdown_card(
            title=title,
            inner_width=inner,
            note_width=width,
            input_tokens=usage.get("input") or 0,
            output_tokens=usage.get("output") or 0,
            reasoning_tokens=usage.get("reasoning") or 0,
            cache_read_tokens=usage.get("cache_read") or 0,
            cache_write_tokens=usage.get("cache_write") or 0,
            cache_write_1h=usage.get("cache_write_1h") or 0,
            recorded_total=usage.get("tokens_total") or 0,
            attributed=attributed,
            calls=calls,
            notes=notes,
            colored=self._token_series_ok,
        )
        return self._sectioned_box(
            card.title, self._adopt_token_card(card), width, list(card.notes)
        )

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
        inner = max(1, width - 4)
        categories = [
            EconomicsCategory(label, econ.tokens[i], econ.cost[i], i)
            for i, label in enumerate(TOKEN_TYPES)
            if econ.tokens[i] > 0 or econ.cost[i] > 0
        ]
        card = token_economics_card(
            categories=categories,
            total_tokens=econ.total_tokens,
            total_cost=econ.total_cost,
            inner_width=inner,
            estimated=econ.estimated,
            missing_cache_rate=econ.missing_cache_rate,
            local_tokens=econ.local_tokens,
            colored=self._token_series_ok,
        )
        if card.header:
            self._mark_box_header(card.header, width)
        return self._sectioned_box(
            card.title, self._adopt_token_card(card), width, list(card.notes)
        )

    def model_scope_overview(self, width: int) -> list[str]:
        """Show one selected model's contribution inside the current zoom scope."""
        model = self.zoom_model
        if not model:
            return ["No model selected."]
        return self.model_economics(self.current_sessions(), model, width)

    def model_economics(self, workflows: list[Workflow], model: str, width: int) -> list[str]:
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
        table = project_table_text(
            [self._project_row(project) for project in rows],
            self._project_headings(),
            inner,
            self._project_total_row(rows) if len(rows) > 1 else None,
        )
        title = self.projects_box_title(rows)
        lines = self._ruled_box(title, table.header, list(table.body), table.total, [], width)
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
        return subagents_view.flame_pct(frac)

    @staticmethod
    def _legend_names(segments, with_model: bool = False) -> list[str]:
        return subagents_view.legend_names(segments, with_model)

    def _flame_label_line(self, segments, widths, text_of) -> tuple[str, list[int]]:
        text, placed, spans = subagents_view.flame_label_line(segments, widths, text_of)
        if text:
            self._token_runs[text] = [(span.column, span.length, span.slot) for span in spans]
        return text, placed

    def _adopt_subagent_layout(self, layout) -> list[str]:
        self._box_headers.update(layout.headers)
        self._subagent_header_at = dict(layout.row_map)
        self._subagent_cursor_line = layout.cursor_line
        for line, spans in layout.token_spans:
            self._token_runs[line] = [(span.column, span.length, span.slot) for span in spans]
        for line, columns, target in layout.sort_headers:
            self._line_sort_headers[line] = (columns, target)
        if layout.selected_node_index is not None:
            self.app._subagent_selected = layout.selected_node_index
        return list(layout.lines)

    def _flamegraph_box(self, workflow: Workflow, width: int) -> list[str]:
        layout = subagents_view.flamegraph_layout(
            self.app.session_flame(workflow),
            width,
            glyphs=self.box_glyphs(),
            colored=self._token_series_ok,
            api_prices_key=self._key("main", "api_prices"),
        )
        return self._adopt_subagent_layout(layout)

    def detail_subagents(self, workflow: Workflow, width: int) -> list[str]:
        self._subagent_header_at = {}
        self._subagent_cursor_line = None
        nodes = self.session_node_rows(workflow.id)
        rows = self.app.subagent_rows(workflow)
        if self.app.active_subagent_turns:
            return self.detail_turns(workflow, width)
        if not any(row["depth"] > 0 for row in nodes):
            return ["# Subagents", "No subagents used in this workflow."]
        selected = next(
            (row for row in rows if row["_node_index"] == self.app.active_subagent_drill), None
        )
        if selected is not None:
            return self._subagent_detail(selected, nodes, width)
        priced = self._priced_nodes(nodes)
        totals = self.whatif_session_totals(workflow)
        target = self.whatif_model if self.whatif_model and totals else ""
        whatif_prices = (
            {row["_node_index"]: self.whatif_node_price(row, target) for row in rows}
            if target
            else {}
        )
        headings = {
            key: self.subagent_sort_heading(key, label) for key, label in self.SUBAGENT_SORT_COLUMNS
        }
        layout = subagents_view.subagents_overview_layout(
            priced_nodes=priced,
            rows=rows,
            flame=self.app.session_flame(workflow),
            width=width,
            glyphs=self.box_glyphs(),
            colored=self._token_series_ok,
            api_prices_key=self._key("main", "api_prices"),
            select_key=self.keymap.label("main", "select"),
            sort_headings=headings,
            sort_columns=self.SUBAGENT_SORT_COLUMNS,
            selected_node_index=self.app._subagent_selected,
            target=target,
            whatif_totals=totals if target else None,
            whatif_prices=whatif_prices,
            baseline_estimated=self.whatif_baseline_is_estimated(workflow) if target else False,
        )
        return self._adopt_subagent_layout(layout)

    @staticmethod
    def _subagent_wrap(lines: list[str], width: int) -> list[str]:
        return wrap_lines(lines, width)

    def _subagent_table(
        self, rows: list[dict], width: int, offset: int, tree_cost: float, target: str = ""
    ) -> list[str]:
        layout = subagents_view.execution_table_layout(
            rows,
            width,
            offset=offset,
            tree_cost=tree_cost,
            glyphs=self.box_glyphs(),
            sort_headings={
                key: self.subagent_sort_heading(key, label)
                for key, label in self.SUBAGENT_SORT_COLUMNS
            },
            sort_columns=self.SUBAGENT_SORT_COLUMNS,
            selected_node_index=self.app._subagent_selected,
            target=target,
            whatif_prices={row["_node_index"]: self.whatif_node_price(row, target) for row in rows}
            if target
            else {},
        )
        return self._adopt_subagent_layout(layout)

    def _subagent_detail(self, row: dict, nodes: list[dict], width: int) -> list[str]:
        priced = self._priced_nodes(nodes)
        unavailable = self.app.subagent_turns_unavailable()
        prompt = self.app.subagent_prompt_text()
        target = self.whatif_model or ""
        layout = subagents_view.subagent_detail_layout(
            row,
            priced,
            width,
            glyphs=self.box_glyphs(),
            back_key=self.keymap.label("main", "back"),
            select_key=self.keymap.label("main", "select"),
            turns_unavailable=unavailable,
            prompt_text=prompt,
            cost_label="API-equivalent"
            if self.show_api_prices and not self.store.demo
            else "Recorded",
            colored=self._token_series_ok,
            target=target,
            target_cost=self.whatif_node_price(row, target) if target else 0.0,
        )
        return self._adopt_subagent_layout(layout)

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
        prefix = list(head or [])
        prices = {row["_node_index"]: self.whatif_node_price(row, target) for row in rows}
        table = subagents_view.execution_table_layout(
            rows,
            width,
            offset=len(prefix),
            tree_cost=sum(row["cost"] for row in rows),
            glyphs=self.box_glyphs(),
            sort_headings={
                key: self.subagent_sort_heading(key, label)
                for key, label in self.SUBAGENT_SORT_COLUMNS
            },
            sort_columns=self.SUBAGENT_SORT_COLUMNS,
            selected_node_index=self.app._subagent_selected,
            target=target,
            whatif_prices=prices,
        )
        layout = subagents_view.whatif_footer_layout(
            table,
            rows=rows,
            target=target,
            totals=totals,
            whatif_prices=prices,
            baseline_estimated=self.whatif_baseline_is_estimated(workflow),
        )
        return prefix + self._adopt_subagent_layout(layout)

    @staticmethod
    def _treemap_rects(
        items: list[tuple[str, float]], width: int, height: int
    ) -> list[tuple[str, float, int, int, int, int]]:
        return treemap_rects(items, width, height)

    @staticmethod
    def _heat_position(value: float, lo: float, hi: float, levels: int) -> int:
        return tools_view.heat_position(value, lo, hi, levels)

    def _tool_treemap_box(
        self, bucket: dict[str, dict], width: int, max_height: int | None = None
    ) -> list[str]:
        layout = tools_view.tool_treemap_layout(
            bucket,
            width,
            tools_view.ToolsOptions(
                treemap_height=max_height,
                api_prices=self.show_api_prices,
                demo=self.store.demo,
                api_price_label=self._key("main", "api_prices"),
                unicode=unicode_screen(),
                tool_heat_colored=self._tool_heat_ok,
            ),
        )
        self._tool_tree_runs = {line: list(spans) for line, spans in layout.heat_spans.items()}
        return list(layout.lines)

    def detail_tools(
        self, workflow: Workflow, width: int, treemap_height: int | None = None
    ) -> list[str]:
        self._tool_header_at = {}
        self._tool_call_at = {}
        self._tool_cursor_line = None
        self._tool_tree_runs = {}
        supports_tools = self.session_supports_tools(workflow.id)
        if not supports_tools:
            return list(
                tools_view.build_tools_layout(
                    None,
                    width,
                    tools_view.ToolsOptions(supports_tools=False),
                ).lines
            )
        tool_rows = self.session_tool_rows(workflow.id)
        if not tool_rows:
            return list(
                tools_view.build_tools_layout(
                    None,
                    width,
                    tools_view.ToolsOptions(has_tool_rows=False),
                ).lines
            )
        projection = self.app.tool_projection(workflow.id)
        drill = self.app.active_tool_drill
        api_price_label = self._key("main", "api_prices")
        layout_key = (
            projection["key"],
            drill,
            width,
            treemap_height,
            self._key("main", "select"),
            self._key("main", "back"),
            api_price_label,
            unicode_screen(),
            self._tool_heat_ok,
            self._token_series_ok,
        )
        cached = self._tool_layout_cache
        if cached is None or cached[0] != layout_key:
            options = tools_view.ToolsOptions(
                drill=drill,
                treemap_height=treemap_height,
                supports_turns=self.session_supports_turns(workflow.id),
                select_label=self._key("main", "select"),
                back_label=self._key("main", "back"),
                api_price_label=api_price_label,
                api_prices=self.show_api_prices,
                demo=self.store.demo,
                unicode=unicode_screen(),
                tool_heat_colored=self._tool_heat_ok,
                token_series_colored=self._token_series_ok,
            )
            layout = tools_view.build_tools_layout(projection, width, options)
            lines = list(layout.lines)
            cached = (
                layout_key,
                lines,
                dict(layout.row_map),
                dict(layout.call_map),
                {line: list(spans) for line, spans in layout.heat_spans.items()},
                set(layout.box_headers),
                {
                    line: [(span.column, span.length, span.slot) for span in spans]
                    for line, spans in layout.token_spans.items()
                },
            )
            self._tool_layout_cache = cached
        self._tool_header_at, self._tool_call_at, self._tool_tree_runs = cached[2:5]
        self._box_headers.update(cached[5])
        self._token_runs.update(cached[6])
        mapping = (
            self._tool_call_at if self.app.active_tool_drill is not None else self._tool_header_at
        )
        cursor = (
            self.app._tool_call_cursor
            if self.app.active_tool_drill is not None
            else self.app._tool_cursor
        )
        self._tool_cursor_line = next(
            (line for line, ordinal in mapping.items() if ordinal == cursor), None
        )
        return cached[1]

    def _build_detail_tools(
        self, workflow: Workflow, width: int, treemap_height: int | None, projection: dict
    ) -> list[str]:
        options = tools_view.ToolsOptions(
            drill=self.app.active_tool_drill,
            treemap_height=treemap_height,
            supports_turns=self.session_supports_turns(workflow.id),
            select_label=self._key("main", "select"),
            back_label=self._key("main", "back"),
            api_price_label=self._key("main", "api_prices"),
            api_prices=self.show_api_prices,
            demo=self.store.demo,
            unicode=unicode_screen(),
            tool_heat_colored=self._tool_heat_ok,
            token_series_colored=self._token_series_ok,
        )
        layout = tools_view.build_tools_layout(projection, width, options)
        self._tool_header_at = dict(layout.row_map)
        self._tool_call_at = dict(layout.call_map)
        self._tool_tree_runs = {line: list(spans) for line, spans in layout.heat_spans.items()}
        self._box_headers.update(layout.box_headers)
        for line, spans in layout.token_spans.items():
            self._token_runs[line] = [(span.column, span.length, span.slot) for span in spans]
        return list(layout.lines)

    def _tool_ranking_box(
        self, rows: list[dict], title: str, width: int, ordinal: int, offset: int
    ) -> list[str]:
        layout = tools_view.tool_ranking_layout(
            rows,
            title,
            width,
            ordinal,
            tools_view.ToolsOptions(unicode=unicode_screen()),
        )
        self._box_headers.update(layout.box_headers)
        self._tool_header_at.update(
            {offset + line: value for line, value in layout.row_map.items()}
        )
        return list(layout.lines)

    def _tool_detail(self, workflow: Workflow, width: int, projection: dict) -> list[str]:
        return self._build_detail_tools(workflow, width, None, projection)

    def turn_costs(self, rows) -> list[float]:
        # `$` estimates wholly unpriced turns, including long-TTL cache writes.
        api = self.show_api_prices and not self.store.demo
        out = []
        for row in rows:
            cost = row["cost"]
            if api and not cost:
                cost = api_equivalent_cost(
                    row["model_name"],
                    row["input"],
                    row["output"],
                    row["reasoning"],
                    row["cache_read"],
                    row["cache_write"],
                    row.get("cache_write_1h", 0),
                )
            out.append(cost)
        return out

    def detail_turn_trace(self, workflow: Workflow, width: int) -> list[str]:
        rows = self.reader_turn_rows(workflow.id)
        idx = self.app.active_trace_drill
        if not rows or idx is None or not 0 <= idx < len(rows):
            return []
        if self.app._trace_loading is not None or self.app._remote_trace_error:
            return self._build_turn_trace(workflow, width, rows, idx, [])
        events = self.app.turn_trace_events(workflow.id, rows[idx])
        full = self.app._trace_full
        key = (
            workflow.id,
            id(rows),
            len(rows),
            idx,
            width,
            id(events),
            id(full),
            self.show_api_prices,
            self.store.demo,
            self.app.trace_expanded,
            frozenset(self.app._trace_open_outputs),
            self.session_records_reasoning(workflow.id),
            self.app.session_supports_trace(workflow.id),
            self._key("main", "select"),
            workflow.machine,
            self.app.active_subagent_turns,
        )
        cached = self._trace_layout_cache
        if cached is None or cached[0] != key:
            lines = self._build_turn_trace(workflow, width, rows, idx, events)
            # Retain source references with one layout, never raw text in the table cache.
            cached = (
                key,
                rows,
                events,
                full,
                lines,
                self._trace_tool_at,
                self._trace_output_ends,
                dict(self._token_runs),
            )
            self._trace_layout_cache = cached
        self._trace_tool_at, self._trace_output_ends = cached[5:7]
        self._token_runs.update(cached[7])
        return cached[4]

    def _build_turn_trace(
        self, workflow: Workflow, width: int, rows, idx: int, events
    ) -> list[str]:
        self._trace_tool_at = {}
        self._trace_output_ends = []
        siblings = self.app.drilled_turn_indices()
        if idx not in siblings:
            return []
        row = rows[idx]
        cost = self.turn_costs([row])[0]
        remote = self.app.remote_trace_reader(workflow.id) is not None
        loading = self.app._trace_loading is not None
        supports_trace = True if loading else self.app.session_supports_trace(workflow.id)
        full_events = self.app._trace_full[2] if self.app._trace_full is not None else None
        layout = turns_view.build_turn_trace(
            rows=rows,
            index=idx,
            siblings=siblings,
            events=events,
            width=width,
            cost=cost,
            glyphs=self.box_glyphs(),
            colored=self._token_series_ok,
            scoped=self.app.active_subagent_turns,
            remote_machine=workflow.machine if remote else None,
            loading=loading,
            expanded=self.app.trace_expanded,
            open_outputs=frozenset(self.app._trace_open_outputs),
            full_events=full_events,
            select_key=self._key("main", "select"),
            supports_trace=supports_trace,
            unavailable_reason=(
                self.app.trace_unavailable_reason(workflow.id)
                if not loading and not supports_trace
                else None
            ),
            remote_error=self.app._remote_trace_error,
            records_reasoning=(
                True if loading else self.app.session_records_reasoning(workflow.id)
            ),
        )
        self._trace_tool_at = layout.tool_lines
        self._trace_output_ends = layout.output_ends
        self._token_runs.update(layout.token_runs)
        return layout.lines

    def trace_output_target(self) -> int | None:
        """The output section at the viewport top, or the next one below it."""
        return output_target(self._trace_output_ends, self.app.scroll)

    def detail_turn_drill(self, workflow: Workflow, width: int) -> list[str]:
        """Render one prompt's full text, totals, and turns."""
        rows = self.reader_turn_rows(workflow.id)
        if not rows:
            return []
        i = self.app.active_turn_drill
        if not isinstance(i, int) or not 0 <= i < len(self.app.turn_runs(workflow.id)):
            return []
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
        costs = self.turn_costs(rows)
        groups = self.turn_group_rows(rows, costs)
        self._turn_header_at = {}
        self._turn_cursor_line = None
        layout = turns_view.build_turn_drill(
            rows=rows,
            costs=costs,
            groups=groups,
            drill=i,
            width=width,
            context_curve=self.session_supports_context_curve(workflow.id),
            traceable=self.app.session_supports_trace(workflow.id),
            scoped=self.app.active_subagent_turns,
            glyphs=self.box_glyphs(),
            colored=self._token_series_ok,
        )
        self._box_headers.update(layout.box_headers)
        self._turn_header_at = layout.row_map
        self._token_runs.update(layout.token_runs)
        self._turn_cursor_line = layout.cursor_lines.get(self.app._trace_cursor)
        return layout.lines

    @staticmethod
    def turn_group_rows(rows, costs):
        return turns_view.turn_group_rows(rows, costs)

    @staticmethod
    def _turn_metric_strips(
        rows, costs, width: int, context_curve: bool, *, unit: str = "turn", first_index: int = 1
    ) -> list[str]:
        return turns_view.turn_metric_strips(
            rows, costs, width, context_curve, unit=unit, first_index=first_index
        )

    def detail_turns(self, workflow: Workflow, width: int) -> list[str]:
        # Only the current table layout is retained. Turn rows are immutable snapshots
        # until reload; keeping their reference also prevents id reuse after replacement.
        # Traces have scroll-dependent output markers and their own content lifetime.
        scoped = self.app.active_subagent_turns
        if (
            (scoped and (self.app._subagent_turn_rows is None or self.app._subagent_turns_error))
            or (not scoped and not self.session_supports_turns(workflow.id))
            or self.app.active_trace_drill is not None
        ):
            self._turn_header_at = {}
            self._turn_cursor_line = None
            return self._build_turns(workflow, width)
        rows = self.reader_turn_rows(workflow.id)
        drill = self.app.active_turn_drill
        key = (
            workflow.id,
            id(rows),
            len(rows),
            width,
            drill,
            self.show_api_prices,
            self.store.demo,
            self.session_supports_context_curve(workflow.id),
            self.session_supports_trace(workflow.id),
            unicode_screen(),
            scoped,
        )
        cached = self._turn_layout_cache
        if cached is None or cached[0] != key:
            self._turn_header_at = {}
            self._turn_cursor_line = None
            lines = self._build_turns(workflow, width)
            headers = {line for line in lines if line in self._box_headers}
            cursor_lines = {ordinal: line for line, ordinal in self._turn_header_at.items()}
            cached = (
                key,
                rows,
                lines,
                headers,
                self._turn_header_at,
                cursor_lines,
                dict(self._token_runs),
            )
            self._turn_layout_cache = cached
        _, _, lines, headers, self._turn_header_at, cursor_lines, token_runs = cached
        # draw() clears paint metadata each frame; selection is deliberately not cached.
        self._box_headers.update(headers)
        self._token_runs.update(token_runs)
        cursor = (
            self.app._turn_cursor if self.app.active_turn_drill is None else self.app._trace_cursor
        )
        self._turn_cursor_line = cursor_lines.get(cursor)
        return lines

    def _build_turns(self, workflow: Workflow, width: int) -> list[str]:
        scoped = self.app.active_subagent_turns
        label = "Execution turns" if scoped else "Turns"
        if scoped:
            if self.app._subagent_turns_error:
                return self._subagent_wrap([f"# {label}", self.app._subagent_turns_error], width)
            if self.app._subagent_turn_rows is None:
                return [f"# {label}", "Loading execution turns..."]
        elif not self.session_supports_turns(workflow.id):
            return [
                "# Turns",
                "This session's source records no per-turn usage.",
            ]
        rows = self.reader_turn_rows(workflow.id)
        if not rows:
            scope = "execution" if scoped else "session"
            return [f"# {label}", f"No turns recorded for this {scope}."]
        if self.app.active_turn_drill is not None:
            drilled = self.detail_turn_drill(workflow, width)
            if drilled:
                return drilled
            self.app.turn_drill = None
        costs = self.turn_costs(rows)
        curve = self.session_supports_context_curve(workflow.id)
        layout = turns_view.build_turns(
            rows=rows,
            costs=costs,
            width=width,
            compactions=util_ops.context_compactions(rows) if curve else {},
            cache_events=pricing_ops.cache_misses(rows) if curve else (),
            scoped=scoped,
            glyphs=self.box_glyphs(),
        )
        self._box_headers.update(layout.box_headers)
        self._turn_header_at = layout.row_map
        self._token_runs.update(layout.token_runs)
        self._turn_cursor_line = layout.cursor_lines.get(self.app._turn_cursor)
        return layout.lines

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
        key_w = max(
            (display_width(e.label(self.app)) for _t, rows in sections for e in rows), default=9
        )
        desc_x = key_w + 2

        head = curses.color_pair(6) | curses.A_BOLD
        rule = curses.color_pair(4)
        key_attr = curses.color_pair(2) | curses.A_BOLD

        lines: list[list[tuple[int, str, int]]] = []
        for title, rows in sections:
            # Centered section title with a rule filling both sides.
            label = f" {title} "
            label_w = display_width(label)
            left = max(1, (inner_w - label_w) // 2)
            lines.append(
                [
                    (0, "─" * left, rule),
                    (left, label, head),
                    (left + label_w, "─" * max(0, inner_w - left - label_w), rule),
                ]
            )
            for entry in rows:
                keys = entry.label(self.app)
                lines.append(
                    [
                        (key_w - display_width(keys), keys, key_attr),
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
        key_w = max(
            (display_width(e.label(self.app)) for _t, rows in sections for e in rows), default=9
        )
        desc = max(
            (display_width(e.text(self.app)) for _t, rows in sections for e in rows), default=20
        )
        titles = max((display_width(t) + 4 for t, _rows in sections), default=12)
        return max(key_w + 2 + desc, titles, 52)

    def draw_help(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        # A panel, not a view: it floats centered over whatever is behind it (draw()
        # paints the body first), sized to its own content -- a full-screen box holding
        # six lines is what a manual looks like, not a cheat sheet.
        inner_w = max(20, min(self.help_width(), width - 8))
        lines = self.help_lines(inner_w)
        workspace = (
            self.app.conversation_search if keymap.in_conversation_search(self.app) else None
        )
        pager = workspace or self.app
        layout = pager_layout(
            top=y,
            bottom=bottom,
            width=width,
            inner_width=inner_w,
            horizontal_chrome=4,
            total_rows=len(lines),
            scroll=pager.help_scroll,
            content_sized=True,
        )
        box_y, box_x, box_h, box_w = layout.y, layout.x, layout.height, layout.width

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

        visible = layout.viewport.visible
        if workspace is not None:
            workspace.help_page_size = visible
        scroll = layout.viewport.offset
        pager.help_scroll = scroll
        for offset, segments in enumerate(lines[scroll : scroll + visible]):
            row_y = box_y + 1 + offset
            for dx, text, attr in segments:
                self.write(stdscr, row_y, box_x + 2 + dx, text, attr)
        self._paint_scrollbar(stdscr, box_y + 1, box_x + box_w - 1, len(lines), visible, scroll)
        if len(lines) > visible:  # only then is there anything to scroll
            hint = f" {self._keys('help', 'down', 'up')} scroll "
            self.write(
                stdscr,
                box_y + box_h - 1,
                box_x + max(2, box_w - len(hint) - 2),
                hint,
                curses.color_pair(1),
            )

    def whats_new_lines(self, inner_w: int) -> list[list[tuple[int, str, int]]]:
        notes = self.app.whats_new_notes
        accent = curses.color_pair(6)
        muted = curses.color_pair(4)
        bold = curses.A_BOLD
        lines: list[list[tuple[int, str, int]]] = []

        def text_rows(text: str, attr: int = 0, indent: int = 0) -> None:
            room = max(8, inner_w - indent)
            for part in wrap_cells(text, room) or [""]:
                lines.append([(indent, part, attr)] if part else [])

        if not notes:
            text_rows("Release highlights are unavailable in this installation.", bold)
            text_rows("Open the official releases page for the published notes.")
            lines.append([])
            text_rows(RELEASES_URL, accent)
            return lines

        for section in notes["sections"]:
            if lines:
                lines.append([])
            title = section["title"]
            rule_x = len(title) + 2
            lines.append(
                [
                    (0, title, accent | bold),
                    (rule_x, ("─" if unicode_screen() else "-") * (inner_w - rule_x), muted),
                ]
            )
            for item in section["items"]:
                availability = item.get("availability", "both")
                suffix = f" ({availability.upper()})" if availability != "both" else ""
                parts = wrap_cells(item["text"] + suffix, inner_w - 2)
                for index, part in enumerate(parts):
                    lines.append([(0, "·" if index == 0 else " ", muted), (2, part, 0)])
                hint = item.get("hint")
                if hint:
                    binding = hint.get("binding")
                    key = (
                        self.app.keymap.label(binding["context"], binding["action"])
                        if binding
                        else ""
                    )
                    text_rows(f"{key}  {hint['text']}" if key else hint["text"], muted, 4)
        return lines

    def draw_whats_new(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        inner_w = max(20, min(72, width - 8))
        lines = self.whats_new_lines(inner_w)
        layout = pager_layout(
            top=y,
            bottom=bottom,
            width=width,
            inner_width=inner_w,
            horizontal_chrome=6,
            total_rows=len(lines),
            scroll=self.app.whats_new_scroll,
            content_sized=False,
            center_vertical=False,
        )
        box_y, box_x, box_h, box_w = layout.y, layout.x, layout.height, layout.width
        for row in range(box_y, box_y + box_h):
            self.write(stdscr, row, box_x, " " * box_w)
        close = self._key("whats-new", "close")
        release = self._key("whats-new", "open_release")
        viewed_version = (self.app.whats_new_notes or {}).get("version", self.app.whats_new_version)
        title = f"What's New · v{viewed_version}"
        border = curses.color_pair(6) | curses.A_BOLD
        self.draw_frame(stdscr, box_y, box_x, box_h, box_w, border)
        label = f" {shorten(title, box_w - 6)} "
        self.write(stdscr, box_y, box_x + (box_w - display_width(label)) // 2, label, border)
        visible = layout.viewport.visible
        scroll = layout.viewport.offset
        self.app.whats_new_scroll = scroll
        for offset, segments in enumerate(lines[scroll : scroll + visible]):
            row_y = box_y + 1 + offset
            for dx, text, attr in segments:
                self.write(stdscr, row_y, box_x + 3 + dx, text, attr)
        self._paint_scrollbar(stdscr, box_y + 1, box_x + box_w - 1, len(lines), visible, scroll)
        scroll_keys = self._keys("whats-new", "down", "up")
        older = self._key("whats-new", "older")
        newer = self._key("whats-new", "newer")
        position = (
            f"{self.app.whats_new_index + 1}/{len(self.app.whats_new_history)}"
            if self.app.whats_new_history
            else ""
        )
        hints = [
            f"{newer} newer" if newer and self.app.whats_new_index > 0 else "",
            position,
            f"{older} older"
            if older and self.app.whats_new_index + 1 < len(self.app.whats_new_history)
            else "",
            f"{close} close" if close else "",
            f"{release} full release" if release else "",
            f"{scroll_keys} scroll" if len(lines) > visible and scroll_keys else "",
        ]
        hint = " · ".join(part for part in hints if part)
        if hint:
            label = f" {shorten(hint, box_w - 6)} "
            self.write(
                stdscr,
                box_y + box_h - 1,
                box_x + (box_w - display_width(label)) // 2,
                label,
                curses.color_pair(1),
            )

    def _price_source_description(self) -> str:
        # Catalog metadata remains a pricing lookup; the view only receives display text.
        meta = price_source_meta()
        if meta:
            kind = "refreshed" if meta.get("kind") == "cache" else "bundled"
            return f"models.dev {(meta.get('fetched_at') or '?')[:10]} ({kind})"
        return "no models.dev catalog — fallback rates"

    def price_intro_lines(self) -> list[str]:
        return list(
            price_view.intro_lines(self._price_source_description(), self.app.price_token_mix())
        )

    def _price_eff_cell(self, entry) -> str:
        return price_view.eff_cell(entry)

    def _price_use_cell(self, entry, peak: float) -> str:
        return price_view.use_cell(entry, peak)

    def _price_raw_cells(self, entry) -> list[str]:
        return price_view.raw_cells(entry)

    def _price_core_text(self, entry, namew: int, peak: float) -> str:
        return price_view.core_text(entry, namew, peak)

    def _price_header(self, namew: int) -> str:
        return price_view.header_text(
            namew,
            self.app.prices_sort,
            self.sort_descending(self.app.prices_sort, self.app.prices_sort_reverse),
        )

    def _price_column_ranges(self, entries) -> list[tuple[float, float] | None]:
        return price_view.column_ranges(entries)

    def _price_heat_level(self, value: float, rng: tuple[float, float] | None) -> int | None:
        return price_view.heat_level(value, rng)

    def price_table_lines(self, width: int) -> list[str]:
        entries = self.priced_model_entries()
        return price_view.table_lines(
            entries,
            source=self._price_source_description(),
            token_mix=self.app.price_token_mix(),
            view=self.app.prices_view,
            sort=self.app.prices_sort,
            descending=self.sort_descending(self.app.prices_sort, self.app.prices_sort_reverse),
            query=self.query,
            width=width,
        )

    def draw_prices(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        # Reference overlay (toggled with P) so the rates behind the "$" what-if
        # number are visible. Laid out by the active view (p cycles by vendor / by
        # provider / flat); j/k moves a cursor over models, Enter drills into sessions.
        if self.app.prices_model is not None:
            self.draw_price_sessions(stdscr, y, bottom, width)
            return
        # Trends-style chrome: a plain box title, the view modes as clickable tabs
        # (h/l or a click switches, p still cycles) centered in a rule, and one dim
        # context line -- everything else is table. The keys live in the keybar.
        self.box(stdscr, y, 0, bottom - y, width, "Model prices", active=True)
        labels = tuple(label for _key, label in self.app.prices_views)
        keys = [key for key, _label in self.app.prices_views]
        active = keys.index(self.app.prices_view) if self.app.prices_view in keys else 0
        self.draw_tabs(stdscr, y + 1, 1, width - 2, labels, active, kind="pricetab", rule=True)
        inner_w = width - 4
        entries = self.priced_model_entries()
        source = self._price_source_description()
        token_mix = self.app.price_token_mix()
        top = y + 3
        head_y = top + 2
        list_top = head_y + 1
        visible = max(1, bottom - list_top - 1)
        layout = price_view.table_layout(
            entries,
            source=source,
            token_mix=token_mix,
            view=self.app.prices_view,
            sort=self.app.prices_sort,
            descending=self.sort_descending(self.app.prices_sort, self.app.prices_sort_reverse),
            query=self.query,
            width=inner_w,
            selection=self.app.prices_index,
            scroll=self.app.prices_scroll,
            visible=visible,
        )
        for offset, line in enumerate(layout.intro):
            self.write(
                stdscr, top + offset, 2, shorten(line, inner_w), curses.color_pair(1) | curses.A_DIM
            )
        if layout.empty_message:
            self.write(stdscr, head_y, 2, shorten(layout.empty_message, inner_w))
            return
        self.app.prices_index = layout.selected_index
        self.app.prices_scroll = layout.scroll
        self._paint_box_header(stdscr, head_y, 2, layout.header, inner_w)
        for span in layout.sort_spans:
            self.sort_regions.append(
                (head_y, 2 + span.start, 2 + span.start + span.width - 1, span.key, "prices")
            )
        for offset, row in enumerate(layout.rows):
            row_y = list_top + offset
            if row.kind == "header":
                self.write(stdscr, row_y, 2, row.text, curses.color_pair(6) | curses.A_BOLD)
                continue
            selected = row.selected
            attr = curses.A_REVERSE | curses.A_BOLD if selected else curses.A_NORMAL
            self.write(stdscr, row_y, 2, row.text, attr)
            if selected:
                self.write_selected_bars(stdscr, row_y, 2, row.text)
            for span in row.spans:
                if span.role == "muted":
                    style = curses.color_pair(1) | curses.A_DIM
                elif span.role == "heat":
                    style = curses.color_pair(PRICE_HEAT_BASE_PAIR + span.level) | curses.A_BOLD
                elif span.role == "normal":
                    style = curses.A_NORMAL
                elif span.role == "selected":
                    style = attr
                else:
                    continue
                self.write(stdscr, row_y, 2 + span.start, span.text, style)
        self._paint_scrollbar(
            stdscr, list_top, width - 1, layout.total_rows, visible, layout.scroll
        )

    def price_session_lines(self, model: str, width: int) -> list[str]:
        rows = [
            price_view.PriceSessionEntry(
                workflow.created_at,
                cost,
                tokens,
                f"{self.src_col(workflow)}{self.session_marks(workflow)}{workflow.title}",
            )
            for workflow, cost, tokens in self.price_model_sessions(model)
        ]
        return price_view.session_lines(rows, model, self.src_col())

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
        top = y + 2
        rows = [
            price_view.PriceSessionEntry(
                workflow.created_at,
                cost,
                tokens,
                f"{self.src_col(workflow)}{self.session_marks(workflow)}{workflow.title}",
            )
            for workflow, cost, tokens in self.price_model_sessions(model)
        ]
        list_top = top + 2
        visible = max(1, bottom - list_top - 1)
        layout = price_view.session_layout(
            rows,
            model=model,
            source_header=self.src_col(),
            scroll=self.app.prices_scroll,
            visible=visible,
        )
        if layout.empty_message:
            self.write(stdscr, top, 2, shorten(layout.empty_message, inner_w))
            return
        self.app.prices_scroll = layout.scroll
        self.write(stdscr, top, 2, shorten(layout.summary, inner_w), curses.color_pair(4))
        self._paint_box_header(stdscr, top + 1, 2, layout.header, inner_w)
        for offset, line in enumerate(layout.rows):
            self.write_rich(stdscr, list_top + offset, 2, shorten(line, inner_w))
        self._paint_scrollbar(
            stdscr, list_top, width - 1, layout.total_rows, visible, layout.scroll
        )

    # Per-kind toast styling: (colour pair, sigil, header word). Reuses the one
    # restrained palette -- slate info, green success, amber warn, red error -- so a
    # toast reads the same as the cost/alert colours everywhere else; the sigil + word
    # give a non-colour cue too.
    TOAST_STYLE = {
        kind: (pair, NOTIFICATION_STYLES[kind].sigil, NOTIFICATION_STYLES[kind].label)
        for kind, pair in (
            ("info", 4),
            ("success", 3),
            ("warn", 2),
            ("error", 5),
            ("release", 6),
        )
    }
    TOAST_WIDTH = 46  # card width the message wraps within
    TOAST_MAX_LINES = 4  # cap wrapped message lines so a card can't fill the screen

    @staticmethod
    def _wrap_notice(text: str, width: int) -> list[str]:
        return wrap_notice(text, width)

    def draw_toasts(self, stdscr: curses.window, height: int, width: int) -> None:
        # Severity belongs to the frame/title, not the message background. Release
        # announcements use the same card with extra vertical breathing room.
        if self.toast_history:
            return
        toasts = self.active_toasts()
        if not toasts:
            return
        key = self._key("main", "whats_new") or self._key("help", "whats_new")
        cards = toast_cards(
            [Notification(toast.text, toast.kind, toast.born) for toast in toasts],
            height=height,
            width=width,
            release_version=self.app.whats_new_version,
            release_key=key,
            fallback_sigil=None if unicode_screen() else "*",
            card_width=self.TOAST_WIDTH,
            max_lines=self.TOAST_MAX_LINES,
        )
        for card in cards:
            pair = self.TOAST_STYLE.get(card.kind, self.TOAST_STYLE["info"])[0]
            accent = curses.color_pair(pair) | curses.A_BOLD
            for dy in range(card.height):
                self.write(stdscr, card.y + dy, card.x, " " * card.width)
            self.draw_frame(stdscr, card.y, card.x, card.height, card.width, accent)
            title_attr = accent | curses.A_REVERSE if card.kind == "release" else accent
            self.write(stdscr, card.y, card.x + 2, clip(card.title, card.width - 4), title_attr)
            for dy, line in enumerate(card.lines):
                self.write(stdscr, card.y + card.padding + dy, card.x + 3, line)
            if card.shortcut:
                dy, offset, shortcut = card.shortcut
                self.write(
                    stdscr,
                    card.y + card.padding + dy,
                    card.x + 3 + offset,
                    shortcut,
                    accent | curses.A_REVERSE,
                )

    @staticmethod
    def _toast_age(seconds: float) -> str:
        return toast_age(seconds)

    def toast_history_lines(self, width: int) -> list[tuple[str, str]]:
        # Newest first, with hanging indents so complete messages remain readable
        # even when a path or error was too long for its live card.
        rows = history_rows(
            [Notification(toast.text, toast.kind, toast.born) for toast in self.app.toast_log],
            current_time=self.toast_now(),
            width=width,
            fallback_sigil=None if unicode_screen() else "*",
        )
        return [(row.text, row.kind) for row in rows]

    def draw_toast_history(self, stdscr: curses.window, y: int, bottom: int, width: int) -> None:
        # The `N` overlay: a pager over the notices scrollback (App.toast_log), floating
        # centered over the view like help -- but sized tall, since the log runs long.
        # Newest first; only the age/sigil gutter carries severity colour.
        # j/k/g/G/page scroll (handle_key); Esc/q/N close.
        layout = toast_history_viewport(
            [Notification(toast.text, toast.kind, toast.born) for toast in self.app.toast_log],
            current_time=self.toast_now(),
            y=y,
            bottom=bottom,
            width=width,
            scroll=self.app.toast_history_scroll,
            close_key=self._key("notices", "close"),
            scroll_keys=self._keys("notices", "down", "up"),
            fallback_sigil=None if unicode_screen() else "*",
        )
        if layout is None:
            return
        for row in range(layout.y, layout.y + layout.height):
            self.write(stdscr, row, layout.x, " " * layout.width)
        self.box(stdscr, layout.y, layout.x, layout.height, layout.width, layout.title, active=True)
        self.app.toast_history_scroll = layout.scroll
        count = len(self.app.toast_log)
        for offset, row in enumerate(layout.rows):
            pair = self.TOAST_STYLE.get(row.kind, self.TOAST_STYLE["info"])[0]
            self.write(stdscr, layout.y + 1 + offset, layout.x + 2, row.text)
            if count:
                self.write(
                    stdscr,
                    layout.y + 1 + offset,
                    layout.x + 2,
                    row.gutter,
                    curses.color_pair(pair),
                )
        self._paint_scrollbar(
            stdscr,
            layout.y + 1,
            layout.x + layout.width - 1,
            layout.total_rows,
            layout.visible_rows,
            layout.scroll,
        )
        if layout.scroll_hint:
            self.write(
                stdscr,
                layout.y + layout.height - 1,
                layout.scroll_hint_x,
                layout.scroll_hint,
                curses.color_pair(1),
            )

    @staticmethod
    def _menu_attr(style: str) -> int:
        attrs = {
            menus.NORMAL: curses.A_NORMAL,
            menus.MUTED: curses.color_pair(4),
            menus.DIM: curses.A_DIM,
            menus.SELECTED: curses.A_REVERSE | curses.A_BOLD,
            menus.NOTICE: curses.color_pair(2),
            menus.SUBTLE: curses.color_pair(1),
        }
        return attrs[style]

    def _menu_lines(self, layout: menus.MenuLayout) -> list[tuple[str, int]]:
        return [(line.text, self._menu_attr(line.style)) for line in layout.lines]

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
        content = [
            line if isinstance(line, StyledLine) else StyledLine(str(line[0]), line[1])
            for line in lines
        ]
        layout = modal_layout(scr_h, scr_w, title, content, center_rows=center)
        for row in range(layout.y, layout.y + layout.height):  # clear the footprint first
            self.write(stdscr, row, layout.x, " " * layout.width)
        if alert:
            border = curses.color_pair(5) | curses.A_BOLD
            self.draw_frame(stdscr, layout.y, layout.x, layout.height, layout.width, border)
            self.write(stdscr, layout.y, layout.title_x, layout.title, border)
        else:
            self.box(
                stdscr,
                layout.y,
                layout.x,
                layout.height,
                layout.width,
                title,
                active=True,
            )
        for row in layout.rows:
            self.write(stdscr, row.y, row.x, row.text, row.style)
        return layout.y, layout.x, layout.height, layout.width

    def draw_source_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `H` picker: a small modal list of every present source. j/k moves the
        # highlight, Enter switches, Esc cancels (handled in handle_source_menu_key).
        entries = self.source_menu_entries()
        layout = menus.radio_menu(
            "Browse spend recorded by which harness:",
            [(label, is_current) for _key, label, is_current in entries],
            self.source_menu_index,
        )
        self.draw_modal(
            stdscr,
            scr_h,
            scr_w,
            self._menu_title("Switch harness", "menu.source"),
            self._menu_lines(layout),
        )

    def draw_demo_menu(self, stdscr: curses.window, scr_h: int, scr_w: int) -> None:
        # The `D` picker: a multi-check list of what --demo scrambles. Space toggles a
        # row's [x], `a` all/none, Enter applies (nothing checked = back to real data),
        # Esc cancels. A checkbox list where draw_source_menu is a radio one.
        entries = self.demo_menu_entries()
        intro = (
            "Anonymize which parts (for a shareable screen):"
            if self.demo_menu_sel
            else f"Nothing checked — {self._key('menu.demo', 'select')} shows real data again."
        )
        layout = menus.check_menu(
            intro,
            [(label, checked) for _cat, label, checked in entries],
            self.demo_menu_index,
        )
        title = (
            f"Demo · {self._key('menu.demo', 'toggle')} · "
            f"{self._key('menu.demo', 'check_all')} all · "
            f"{self._key('menu.demo', 'select')} · {self._key('menu.demo', 'cancel')}"
        )
        self.draw_modal(stdscr, scr_h, scr_w, title, self._menu_lines(layout))

    def _draw_filter_menu(self, stdscr, scr_h, scr_w, title, intro, options, index) -> None:
        # Shared body for the `M` / `H` global-filter pickers: an intro line then a radio
        # list (● current, ○ others), the selected row reversed. Mirrors draw_source_menu.
        layout = menus.radio_menu(
            intro,
            [(label, is_current) for _value, label, is_current in options],
            index,
        )
        self.draw_modal(stdscr, scr_h, scr_w, title, self._menu_lines(layout))

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
        idx = menus.selected_index(self.whatif_menu_index, len(entries))
        # Reserve rows for every non-entry line the modal carries, so draw_modal never
        # clips the SELECTED entry off the bottom -- even at the 80x20 minimum, where this
        # is handed scr_h=18. draw_modal paints at most scr_h-8 content rows; the worst-case
        # non-entry lines before the list are 7 (two intro + the two-line tier strip + the
        # filter and its blank + the "↑ more" marker), so entries must stay <= scr_h-15.
        # The floor is 1, not 4: at scr_h=18 only three rows fit, and a floor of 4 would put
        # the selected last row past the paint budget (Enter then arms an off-screen model).
        max_rows = menus.option_budget(scr_h, 15)
        # The model-id column widens to fit the longest row, capped so the box still fits
        # the terminal (and the eff/tokens cell isn't clipped off the right edge): 36% of
        # catalog ids overflow a fixed 34 ("github-copilot/claude-sonnet-4.5", the whole
        # "names don't fit" complaint). Sized off `entries` (the filtered set), not the
        # scroll window, so the width tracks the filter and never jumps as j/k scrolls.
        longest = max((len(str(r[0])) for r in entries), default=24)
        name_cap = max(24, scr_w - 4) - 19  # modal width cap − "| |" gutters(4) − prefix+cell(15)
        namew = max(24, min(longest, name_cap))
        intro = [
            StyledLine("Compare a session's tree against one model's list rates:", menus.MUTED),
            StyledLine("(the Subagents tab; every other view keeps its actual cost)", menus.DIM),
            StyledLine("", menus.NORMAL),  # post-painted tier tab strip
            StyledLine("", menus.NORMAL),
        ]
        tier_line = 2
        if self.whatif_query or self.whatif_filter_active:
            # A block cursor while the query is live, so it reads as an input, not a label.
            cursor = "█" if self.whatif_filter_active else ""
            intro.append(StyledLine(f" filter: {self.whatif_query}{cursor}", menus.MUTED))
            intro.append(StyledLine("", menus.NORMAL))

        def format_entry(row):
            name = row[0]
            if self.whatif_catalog:
                _name, eff, approx = row
                cell = f"{'~' if approx else ''}${eff:,.2f}/M"
            else:
                cell = human_tokens(row[1])
            return f"{pad(shorten(name, namew), namew)} {cell:>10}"

        menu_entries = [(row, row[0] == self.whatif_model) for row in entries]
        hint = (
            f"{self._key('menu.whatif.filter', 'select')} selects · "
            f"{self._key('menu.whatif.filter', 'cancel')} drops the filter"
            if self.whatif_filter_active
            else f"{self._key('menu.whatif', 'filter')} filter · "
            f"{self._key('menu.whatif', 'advance')} next · "
            f"{self._key('menu.whatif', 'cancel')} cancels"
        )
        erase = self._key("menu.whatif.filter", "erase")
        layout = menus.windowed_radio_menu(
            intro,
            menu_entries,
            idx,
            max_rows,
            empty=StyledLine(f"    no model matches — {erase} to widen", menus.NOTICE),
            footer=[StyledLine("", menus.NORMAL), StyledLine(hint, menus.SUBTLE)],
            label_formatter=format_entry,
            current_suffix="",
        )
        catalog_specs = self.app.keymap.specs("menu.whatif", "catalog")
        if self.whatif_filter_active:
            catalog_specs = tuple(
                spec
                for spec in catalog_specs
                if any(
                    bindings.typed_char(code) is None
                    and self.app.keymap.action("menu.whatif.filter", code) is None
                    for code in bindings.parse_key(spec)
                )
            )
        catalog = "/".join(bindings.pretty_key(spec) for spec in catalog_specs[:3])
        ctx = "menu.whatif.filter" if self.whatif_filter_active else "menu.whatif"
        title = (
            f"What-if model · {self._keys(ctx, 'down', 'up')} · {catalog} · "
            f"{self._key(ctx, 'select')} · {self._key(ctx, 'cancel')}"
        )
        my, mx, mh, mw = self.draw_modal(stdscr, scr_h, scr_w, title, self._menu_lines(layout))
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
        # The list outgrew small terminals: scroll a window around the selection so
        # j/k live-preview never walks the highlight off the modal's visible rows
        # (draw_modal itself just truncates; ↑/↓ counts show what's clipped).
        layout = menus.windowed_radio_menu(
            [
                StyledLine("Colour theme (also the web browser's):", menus.MUTED),
                StyledLine("", menus.NORMAL),
            ],
            [(name, is_current) for _tid, name, is_current in entries],
            self.theme_menu_index,
            max(4, scr_h - 12),
        )
        title = (
            f"Theme · {self._keys('menu.theme', 'down', 'up')} preview · "
            f"{self._key('menu.theme', 'select')} keep · {self._key('menu.theme', 'cancel')} revert"
        )
        self.draw_modal(stdscr, scr_h, scr_w, title, self._menu_lines(layout))

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
        "harness": "Harness",
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
        current = self.effective_sort_by()
        layout = menus.radio_menu(
            "Order this list by:",
            [(self.sort_label(key), key == current) for key in options],
            self.sort_menu_index,
        )
        self.draw_modal(
            stdscr,
            scr_h,
            scr_w,
            self._menu_title("Sort by", "menu.sort"),
            self._menu_lines(layout),
        )

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
        heading = [
            StyledLine(shorten(session.title or "(untitled)", 52), menus.MUTED),
            StyledLine(headline, menus.NORMAL),
            StyledLine("", menus.NORMAL),
        ]
        rows = []
        for kc, kind, label in targets:
            # The yank is the one row whose CONTENT changes with the machine: for a
            # pulled session it copies the ssh line, not a cd into a path that isn't here.
            if kind == "copy" and remote:
                label = "copy ssh command"
            if self.app.keymap.action("menu.launch", ord(kc)) is not None:
                kc = " "  # A configured menu action takes precedence over target letters.
            rows.append((kc, label))
        layout = menus.select_menu(
            heading,
            rows,
            self.launch_menu_index,
            footer=[
                StyledLine("", menus.NORMAL),
                StyledLine(f" {self._key('menu.launch', 'cancel')}  cancel", menus.NORMAL),
            ],
        )
        self.draw_modal(
            stdscr,
            scr_h,
            scr_w,
            self._menu_title("Launch session", "menu.launch"),
            self._menu_lines(layout),
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
        # The tabs get the whole row, in the browse-mode bar's shape. There is no key
        # hint beside them: the keybar two rows below is painted from the same keymap
        # table and already says what h/l, j/k, Enter and Esc do here, per tab and per
        # focus/drill state. Reserving ~49 cells for a second copy of that clipped the
        # right-hand tabs off every terminal under ~143 columns.
        if self.trend_model_drill:
            self.draw_tabs(
                stdscr,
                y + 1,
                1,
                width - 2,
                ("Economics", "Sessions"),
                self.trend_drill_tab,
                kind="trendmodel",
                rule=True,
            )
        else:
            self.draw_tabs(
                stdscr, y + 1, 1, width - 2, tabs, self.trend_tab, kind="trend", rule=True
            )
        inner_w = width - 4
        content_h = h - 4
        if self.trend_economics:
            workflows = [w for w, _cost, _tokens in self.trend_drill_sessions()]
            model = self.trend_drill[1]
            usage = self.model_scope_usage(workflows, model)
            if is_local_provider(model):
                list_cost = "- (local model)"
            else:
                list_cost = ("~" if usage["estimated"] else "") + money(float(usage["list_cost"]))
            economics = self.app.token_economics(workflows, model)
            token_card = None
            if economics is not None:
                categories = [
                    EconomicsCategory(label, economics.tokens[i], economics.cost[i], i)
                    for i, label in enumerate(TOKEN_TYPES)
                    if economics.tokens[i] > 0 or economics.cost[i] > 0
                ]
                token_card = token_economics_card(
                    categories=categories,
                    total_tokens=economics.total_tokens,
                    total_cost=economics.total_cost,
                    inner_width=max(1, inner_w - self.BOX_CHROME),
                    estimated=economics.estimated,
                    missing_cache_rate=economics.missing_cache_rate,
                    local_tokens=economics.local_tokens,
                    colored=self._token_series_ok,
                )
            lines = self._adopt_trend_layout(
                trend_views.model_economics_layout(
                    model,
                    len(workflows),
                    int(usage["runs"]),
                    int(usage["tokens"]),
                    list_cost,
                    token_card,
                    inner_w,
                    self.box_glyphs(),
                )
            )
        elif self.trend_drill is not None:
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
        scroll = 0
        if self.trend_economics:
            scroll = max(0, min(self.trend_drill_scroll, len(lines) - content_h))
            self.app.trend_drill_scroll = scroll
        content = lines[scroll : scroll + content_h]
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
            self._paint_token_runs(stdscr, y + 3 + i, 2 + x, line, inner_w - x)
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
        if self.trend_economics:
            self._paint_scrollbar(stdscr, y + 3, width - 1, len(lines), content_h, scroll)

    def _bar_chart(
        self,
        pairs: list[tuple[str, float]],
        width: int,
        height: int,
        keys: list[str] | None = None,
        selected: str | None = None,
    ) -> list[str]:
        layout = bar_chart(pairs, width, height, keys=keys, selected=selected)
        self._bar_slots = list(layout.slots) if layout.slots is not None else None
        self._bar_click_rows = layout.click_rows
        return list(layout.lines)

    def _adopt_trend_layout(self, layout: trend_views.TrendsLayout) -> list[str]:
        self._bar_slots = list(layout.bar_slots) if layout.bar_slots is not None else None
        self._bar_click_rows = layout.bar_click_rows
        self._trend_rows_at = (
            (layout.rows.line, layout.rows.count, layout.rows.start) if layout.rows else None
        )
        for header in layout.headers:
            if 0 <= header.line < len(layout.lines):
                self._box_headers.add(layout.lines[header.line])
                if header.columns:
                    self._line_sort_headers[header.line] = (header.columns, header.target)
        token_runs: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
        for span in layout.spans:
            if span.role == "token" and 0 <= span.line < len(layout.lines):
                token_runs[layout.lines[span.line]].append((span.column, span.length, span.value))
        self._token_runs.update(token_runs)
        return list(layout.lines)

    def _bar_selection(self, tab: str, data: list[tuple[str, float]]) -> str | None:
        # The bucket to mark with the ▲ cursor: only when this chart is the focused
        # Trends tab (a direct trend_* call from a detail context never selects).
        active = self.trend_tabs[self.trend_tab % len(self.trend_tabs)]
        if not (self.trends and self.trend_focus and active == tab):
            return None
        return self._effective_bar_cursor(data)

    def trend_daily(self, width: int, height: int) -> list[str]:
        month, data = self.trend_daily_data()
        return self._adopt_trend_layout(
            trend_views.daily_layout(
                month,
                data,
                self.trend_months(),
                width,
                height,
                navigation_keys=self._keys("trends", "down", "up"),
                selected=self._bar_selection("Daily", data),
            )
        )

    def trend_weekly(self, width: int, height: int) -> list[str]:
        monday, data = self.trend_weekly_data()
        return self._adopt_trend_layout(
            trend_views.weekly_layout(
                monday,
                data,
                self.trend_weeks(),
                width,
                height,
                navigation_keys=self._keys("trends", "down", "up"),
                selected=self._bar_selection("Weekly", data),
            )
        )

    def trend_monthly(self, width: int, height: int) -> list[str]:
        data = self.trend_monthly_data()
        return self._adopt_trend_layout(
            trend_views.monthly_layout(
                data,
                width,
                height,
                selected=self._bar_selection("Monthly", data),
            )
        )

    def draw_calendar(
        self, stdscr: curses.window, top: int, left: int, height: int, width: int
    ) -> None:
        self.app._cal_geom = None
        years = self.calendar_years()
        index = max(0, min(self.trend_year_index, len(years) - 1)) if years else 0
        year = years[index] if years else None
        levels = self.cal_levels
        renderable = year is not None and height >= 13 and width >= 24
        if renderable:
            by_date = self._calendar_by_date(year)
            sessions_by_date: dict[str, int] = defaultdict(int)
            for workflow in self.all_workflows:
                if workflow.created_at[:4] == year:
                    sessions_by_date[workflow.created_at[:10]] += 1
            self._sync_heat_palette()
            cursor = self._effective_cursor(year, by_date)
            heat_glyphs = [self._heat_cell(level, levels)[0] for level in range(levels + 1)]
        else:
            by_date = {}
            sessions_by_date = {}
            cursor = None
            heat_glyphs = ()
        layout = trend_views.calendar_layout(
            year,
            index,
            len(years),
            by_date,
            sessions_by_date,
            levels,
            self.trend_focus,
            cursor,
            height,
            width,
            navigation_keys=self._keys("trends", "down", "up"),
            select_key=self._key("trends.chart" if self.trend_focus else "trends", "select"),
            arrows=" ".join(
                self.app.keymap.label("trends.chart", action)
                for action in ("cursor_left", "cursor_up", "cursor_down", "cursor_right")
            ),
            price_key=self._key("trends", "api_prices"),
            show_api_prices=self.show_api_prices,
            heat_glyphs=heat_glyphs,
        )
        if layout.calendar:
            geometry = layout.calendar
            self.app._cal_geom = (
                top + geometry.grid_y,
                geometry.row_pitch,
                left + geometry.grid_x,
                geometry.column_pitch,
                geometry.start_column,
                geometry.shown_columns,
                geometry.year,
                geometry.grid_start,
            )
        for line_index, line in enumerate(layout.lines):
            self.write_rich(stdscr, top + line_index, left, line, curses.A_NORMAL)
        for span in layout.spans:
            text = layout.lines[span.line][span.column : span.column + span.length]
            if span.role == "heat":
                _glyph, attr = self._heat_cell(span.value, levels)
                if not self.trend_focus and span.line != (
                    layout.calendar.grid_y + 6 * layout.calendar.row_pitch + 2
                    if layout.calendar
                    else -1
                ):
                    attr = (attr & ~curses.A_BOLD) | curses.A_DIM
            elif span.role in ("cursor", "accent"):
                attr = curses.color_pair(6) | curses.A_BOLD
            elif span.role == "title":
                attr = curses.color_pair(4) | curses.A_BOLD
            else:
                attr = curses.color_pair(1)
            self.write(stdscr, top + span.line, left + span.column, text, attr)

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
        return trend_views.ranked_row_budget(height, n, notes)

    def _unpriced_note(self, all_rows: list) -> list[str]:
        return list(
            trend_views.unpriced_note(
                all_rows, self.show_api_prices, self._key("trends", "api_prices")
            )
        )

    def _trend_cursor_window(self, n: int, fit: int) -> tuple[int, int, int]:
        idx, start, shown = trend_views.cursor_window(n, self.app.trend_row_index, fit)
        self.app.trend_row_index = idx
        return idx, start, shown

    # The Models ranking's sortable columns, in the order their labels appear in the
    # header -- Share is deliberately absent: it is Cost expressed as a percentage, so
    # a second zone ordering by it would be the same ranking under another name.
    _TREND_MODEL_SORT_COLUMNS = (("name", "Model"), ("cost", "Cost"))

    def trend_models(self, width: int, height: int) -> list[str]:
        all_rows = self.trend_ranked_rows("Models")
        self.app.trend_row_index = max(0, min(self.app.trend_row_index, len(all_rows) - 1))
        return self._adopt_trend_layout(
            trend_views.model_ranking_layout(
                all_rows,
                width,
                height,
                self.app.trend_row_index,
                {
                    "name": self.trend_sort_heading("name", "Model", "Models"),
                    "cost": self.trend_sort_heading("cost", "Cost", "Models"),
                },
                self.box_glyphs(),
            )
        )

    def _mark_trend_sort_header(self, columns: tuple) -> None:
        # Make a Trends ranking's column header clickable. Registered against
        # BOX_HEADER_LINE like every other boxed table's zones; draw_trends turns it
        # into screen coordinates at the y the header actually lands on.
        self._line_sort_headers[self.BOX_HEADER_LINE] = (columns, "trend")

    def trend_providers(self, width: int, height: int) -> list[str]:
        all_rows = self.trend_ranked_rows("Providers")
        columns = (("name", "Provider"), ("cost", "Cost"), ("tokens", "Tokens"), ("count", "Msgs"))
        self.app.trend_row_index = max(0, min(self.app.trend_row_index, len(all_rows) - 1))
        return self._adopt_trend_layout(
            trend_views.provider_ranking_layout(
                all_rows,
                width,
                height,
                self.app.trend_row_index,
                {key: self.trend_sort_heading(key, label, "Providers") for key, label in columns},
                self.box_glyphs(),
                show_api_prices=self.show_api_prices,
                price_key=self._key("trends", "api_prices"),
            )
        )

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
        return table_group_widths(rows, col, width, display)

    # The shared ranked table's sortable columns, in drawn order. Share is absent for
    # the same reason it is on the Models ranking: it is Cost as a percentage.
    _GROUP_SORT_COLUMNS = (("cost", "Cost"), ("tokens", "Tokens"), ("count", "Sess"))

    def _group_header(self, col: str, namew: int, barw: int, sort_tab: str | None = None) -> str:
        headings = (
            {
                key: self.trend_sort_heading(key, label, sort_tab)
                for key, label in (("name", col), *self._GROUP_SORT_COLUMNS)
            }
            if sort_tab
            else None
        )
        return table_group_header(col, namew, barw, headings)

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
        return table_group_row(name, it, marker, namew, barw, peak, total, display)

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
        if selectable:
            self.app.trend_row_index = max(0, min(self.app.trend_row_index, len(all_rows) - 1))
        headings = (
            {
                key: self.trend_sort_heading(key, label, sort_tab)
                for key, label in (("name", col), *self._GROUP_SORT_COLUMNS)
            }
            if sort_tab
            else None
        )
        if sort_tab:
            return self._adopt_trend_layout(
                trend_views.group_ranking_layout(
                    all_rows,
                    width,
                    noun,
                    col,
                    self.box_glyphs(),
                    limit=limit,
                    cursor=self.app.trend_row_index,
                    selectable=selectable,
                    height=height,
                    headings=headings,
                    show_api_prices=self.show_api_prices,
                    price_key=self._key("trends", "api_prices"),
                    display=display,
                )
            )
        layout = table_group_table_layout(
            all_rows,
            width,
            noun,
            col,
            self.box_glyphs(),
            limit=limit,
            cursor=self.app.trend_row_index,
            selectable=selectable,
            height=height,
            headings=None,
            show_api_prices=self.show_api_prices,
            price_key=self._key("trends", "api_prices"),
            display=display,
        )
        self._box_headers.add(layout.lines[layout.header_line])
        if selectable:
            self.app.trend_row_index = layout.cursor
            self._trend_rows_at = (
                (layout.body_start or 0, layout.window_count, layout.window_start)
                if layout.body_start is not None
                else None
            )
        return list(layout.lines)

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
        kind, key = self.trend_drill
        rows = self.trend_drill_sessions()
        self.app.trend_drill_index = max(0, min(self.app.trend_drill_index, len(rows) - 1))
        _index, start, shown = trend_views.cursor_window(
            len(rows),
            self.app.trend_drill_index,
            trend_views.ranked_row_budget(height, len(rows)),
        )
        projected = [
            trend_views.DrillSession(
                workflow.created_at[:10],
                cost,
                tok,
                self.src_col(workflow),
                self.session_marks(workflow) + workflow.title,
            )
            for workflow, cost, tok in rows[start : start + shown]
        ]
        return self._adopt_trend_layout(
            trend_views.drill_layout(
                kind,
                key,
                projected,
                width,
                len(rows),
                sum(cost for _workflow, cost, _tokens in rows),
                sum(tokens for _workflow, _cost, tokens in rows),
                start,
                self.src_col(),
                self.box_glyphs(),
            )
        )

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
        # ACS_* exist only after initscr(), and a narrow build may not carry them at
        # all -- fall back to ASCII rather than raising mid-paint (as _paint_scrollbar does).
        glyph = getattr(curses, "ACS_HLINE", "-")
        stdscr.hline(y + self.oy, x + self.ox, glyph, max(0, w))

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
