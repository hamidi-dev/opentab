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
    model_context_window,
    model_price,
    price_source_meta,
)
from opentab.util import (
    CONTEXT_COMPACT_FLOOR,
    CONTEXT_COMPACT_RATIO,
    cached_share,
    context_compactions,
    context_size,
    fuzzy_score,
    tool_namespace,
    unicode_screen,
)


def _turn_agent(row) -> str:
    # The Agent cell for one turn row: a subagent is marked with "↳", a main-thread turn
    # shows its own label (or "-" when the backend gives none). Mirrors the page's
    # `t.depth ? '↳ ' + t.agent : t.agent`.
    label = (row.get("agent") or "-").strip() or "-"
    return f"↳ {label}" if row.get("depth") else label


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
    # The Subagents tab's clickable headers, in column order (detail_subagents).
    SUBAGENT_SORT_COLUMNS = (
        ("date", "Started"),
        ("depth", "D"),
        ("agent", "Agent"),
        ("model", "Model"),
        ("cost", "Cost"),
        ("tokens", "Tokens"),
        ("title", "Title"),
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

    def _key(self, ctx: str, action: str) -> str:
        # The primary key bound to (ctx, action), for a painted hint ("j", "Enter").
        # Every hint below asks the live keymap instead of quoting a character, so a
        # remapped key renames itself in every modal title and footer line.
        return self.app.keymap.label(ctx, action)

    def _keys(self, ctx: str, *actions: str) -> str:
        # Slash-joined primaries for a hint ("j/k", "h/l"); unbound actions vanish.
        return "/".join(filter(None, (self.app.keymap.label(ctx, a) for a in actions)))

    def _menu_title(self, title: str, ctx: str) -> str:
        # The standard picker title: "Sort by · j/k · Enter · Esc", off the live keymap.
        parts = [
            title,
            self._keys(ctx, "down", "up"),
            self._key(ctx, "select"),
            self._key(ctx, "cancel"),
        ]
        return " · ".join(p for p in parts if p)

    def __init__(self, app: App) -> None:
        self.app = app
        # The app frame (lazygit's outer border) is a *viewport*, not a layout change:
        # draw() paints the frame in screen coordinates, then sets this origin and hands
        # every drawer the inner (height - 2, width - 2) box. The three primitives that
        # actually touch curses -- write/hline/frame -- add it, so every drawer, region
        # and modal below keeps addressing cell (0, 0) as the top-left of the *content*
        # and no geometry had to learn about the border. It stays (0, 0) until draw()
        # frames a real screen, so a drawer called on its own (the suite does this a
        # lot) paints exactly where it's told.
        self.oy = 0
        self.ox = 0
        # Per-segment colour runs for the Token economics bars, keyed by line text
        # (_token_stack_line). draw() clears it per frame; seeded here so a line-builder
        # called on its own -- which the suite does a lot -- has somewhere to record.
        self._token_runs: dict[str, list[tuple[int, int, int]]] = {}
        # Palette bookkeeping, re-seeded by init_theme_colors; here too so _color_index
        # works on a renderer the suite drives without a curses screen.
        self._theme_color_cache: dict[str, int] = {}
        self._fallback_used: set[int] = set()
        # Tools treemap fills are keyed by detail-line index: most chart rows are
        # spaces, so unlike Token economics their line text is not a unique key.
        self._tool_tree_runs: dict[int, list[tuple[int, int, int]]] = {}
        # Clickable hit regions, rebuilt every draw() so they always match what is
        # on screen. Each is ("rows", kind, y0, y_last, x0, x1, start) for a list
        # (click row y selects index start + (y - y0)) or (kind, y, x0, x1, index)
        # for a tab label where kind is "tab"/"trend". hit() resolves a click. They are
        # content coordinates: App.handle_mouse subtracts the origin once, at getmouse().
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
        # The line index of the selected ▸ group's header (App._turn_cursor resolved
        # against the drawn headers), so draw_detail can highlight it and scroll it
        # into view. None when the Turns tab has no groups. Recomputed each paint.
        self._turn_cursor_line: int | None = None
        # Line-based panes (browse previews, the Subagents tab): which line indices
        # are sortable column headers, as line_index -> (columns, target). The paint
        # loops turn a visible one into sort_regions at its on-screen y, so header
        # clicks sort these lists exactly like the zoom pickers' headers.
        self._line_sort_headers: dict[int, tuple[tuple, str]] = {}
        # Models tab: which detail-line indices are model rows, as line_index -> row
        # ordinal, plus the line the cursor sits on (App.model_pick_index resolved
        # against the drawn rows). The turnline pattern -- the table stays a list of
        # plain strings, and the selection resolves to a colour pair only at paint.
        self._model_row_at: dict[int, int] = {}
        self._model_cursor_line: int | None = None
        # Where the last _ruled_box put its first body row. DERIVED as the box is
        # built, never counted off its prologue (the treemap-offset rule): a box grows
        # a rule between header and body only when the body is non-empty.
        self._ruled_body_start: int | None = None
        # The framed column-header strings the builders have emitted, so the paint can
        # give each one the header band (_mark_box_header explains why it's keyed by
        # text). Cleared each frame by draw().
        self._box_headers: set[str] = set()

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

    def _mark_box_header(self, text: str, width: int) -> None:
        # Record a box's COLUMN HEADER by the exact framed string it will be drawn as,
        # so the paint can find it again. Keyed by TEXT, deliberately not by line index:
        # a pane stacks several boxes (an Overview has four) and each builder only knows
        # offsets within its own block, so an index recorded here is one every caller has
        # to keep re-basing -- which is exactly how the Subagents sort zone drifted off
        # its header once already. Position can't answer it either: _sectioned_box opens
        # the Token economics card with a CHART, so "the row after the top border" would
        # band a bar caption and miss the real header three sections down.
        self._box_headers.add(self.box_row(text, width))

    def box_header_lines(self, lines: list[str]) -> set[int]:
        # Which lines of a built pane are a column header. A stale entry from an earlier
        # pane can only match a line that is byte-identical to some box's header, which
        # makes it one too -- so the lookup stays correct between the per-frame clears.
        return {i for i, line in enumerate(lines) if line in self._box_headers}

    # The column header: accent_bright, bold -- the focus colour, the brightest ink the
    # theme has. Text only, deliberately NOT a filled band: a pane stacks up to five boxes
    # (an Overview has four before the model table), and five solid bars down one screen
    # read as chrome competing with the data rather than labelling it. What it replaced
    # was the structural grey (pair 4, the keybar's colour), which sat a shade BELOW the
    # numbers it labelled and vanished under the box's own title. The row cursor is the
    # same accent REVERSED (paint_cursor_row), so a header and a selection are the same
    # family and still unmistakable for each other -- one is ink, one is a bar.
    HEADER_PAIR = 6

    def _paint_box_header(
        self, stdscr: curses.window, y: int, x: int, line: str, width: int
    ) -> None:
        # A boxed column header: the gutters stay in the frame's plain attribute so only
        # the labels light up (draw_picker_frame paints its own through here too, so a
        # table reads identically in both of its frames).
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
        # If this detail line is a sortable column header (_line_sort_headers), give
        # it click zones at the y it was actually painted on.
        meta = self._line_sort_headers.get(line_index)
        if meta:
            self._register_sort_header(sy, sx, line, meta[0], meta[1], max_w)

    def _paint_detail_lines(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, lines: list[str]
    ) -> None:
        # The shared body painter for the line-based detail panes (browse previews,
        # zoom tabs without a picker): clamp the scroll, paint the visible window,
        # and make any sortable header line clickable.
        visible = h - 4
        # The zoomed Models tab is one of those line-based panes WITH a cursor. Gate on
        # the tab rather than on the map being non-empty: _model_row_at is only rewritten
        # by _model_table, so another tab's paint would otherwise consult a stale one
        # (the same reason draw_detail gates its "turnline" region on current == "Turns").
        models = self.view == "zoom" and self.on_models_tab
        if models:
            # Scrolling this tab IS moving the cursor (the wheel and j/k both drive
            # model_pick_index), so there is no manual scroll to fight -- keep the
            # selected row on screen unconditionally.
            self._scroll_line_into_view(self._model_cursor_line, visible)
        self.app.scroll = max(0, min(self.app.scroll, max(0, len(lines) - visible)))
        drawn = lines[self.scroll : self.scroll + visible]
        # A column header reads as one (accent, bold) wherever it is drawn -- the pickers
        # paint theirs that way, and these are the same headers.
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
            # Make the model rows clickable: the region maps a row back to its line
            # index; _apply_click resolves that to an ordinal via _model_row_at.
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
        # The selected row of ANY navigable table, boxed or not. Painted with write(),
        # deliberately NOT write_rich(): the rich pass overpaints every money/token span
        # with its own colour, and a table row is almost entirely money and tokens -- the
        # highlight would come out in shreds around the one cell that isn't a number.
        # `bars` overdraws block-glyph runs so a cost bar doesn't read as a hole in the
        # highlight band (see write_selected_bars).
        attr = curses.color_pair(6) | curses.A_BOLD | curses.A_REVERSE if attr is None else attr
        row = shorten(line, width)
        if len(row) > self.BOX_CHROME and row[:1] in ("│", "|") and row[-1:] in ("│", "|"):
            # Reverse only the CELLS between the ruled box's gutters, so the selection
            # bar sits inside the table instead of punching a hole through its vertical
            # rules. A pane too narrow to keep the closing gutter falls through below.
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
        # The boxed TOTAL row, in project_row_text's own columns so the sums land under
        # the numbers they sum. Only the line-based frame draws one -- the picker's rows
        # scroll, and a total pinned to the bottom border would sum a window, not a list.
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
        natural = max(longest, len("Project")) + 42  # marker + Cost/Tokens/Ses/Subagents
        return max(24, min(natural, width // 2, max(24, width - 44)))

    # --- Machines mode sidebar (the fleet view) ------------------------------
    @staticmethod
    def machine_name_width(width: int) -> int:
        return max(8, width - 30)

    @staticmethod
    def machine_badge(machine: MachineSummary) -> str:
        # ● the live box you're on (full drill-in), ○ a pulled snapshot. Same decorative
        # Unicode register as the ★/✎ session marks, so it rides the same capable terminal.
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
        natural = max(longest, len("Machine")) + 32  # badge + marker + Cost/Tokens/Ses
        return max(24, min(natural, width // 2, max(24, width - 44)))

    def browse_left_width(self, width: int) -> int:
        if self.browse_mode == "machines":
            return self.machines_left_width(width)
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

    # What the chrome eats out of the window before a pager's first line: the app frame
    # (2 rows / 2 columns), the header strip, the footer strip, and the detail box's own
    # border and tabs. One constant, because App._page_step strides by the same height.
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
            # Dispatch by tab NAME like draw_detail: current_tabs() appends Turns and
            # Tools per session, so a fixed index would page the wrong line count.
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
        # Settle any drill whose data moved BEFORE a single cell is painted. The disarm
        # lives inside current_sessions (App._drilled), which the breadcrumb is drawn
        # ahead of -- so without this the frame that heals shows a crumb naming a drill
        # its own session list has already dropped.
        self.app.settle_drills()
        self.apply_background(stdscr)  # theme bg fills the screen (before erase reads it)
        stdscr.erase()
        self.regions = []  # rebuilt below as panels draw, for this frame's clicks
        self.sort_regions = []  # column-header sort zones, same lifecycle as regions
        self._line_sort_headers = {}  # refilled by the line-based drawers below
        self._box_headers = set()  # ditto: the framed header strings, for the header band
        # Per-segment colour runs for the Token economics bars, keyed by the line's own
        # text (see _token_stack_line). Cleared per frame like the other paint
        # side-channels, so a stale bar can never colour a line that outlived it.
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

        # The app frame: one border around the whole UI (lazygit's outer box), painted
        # in screen coordinates. Everything after this draws inside it -- the origin
        # shifts by one cell and the drawers are handed the inner box, so they lay
        # themselves out exactly as they did full-screen, two rows and two columns
        # smaller. Active, so the frame reads as chrome the panels can highlight against.
        self.frame_app(stdscr, height, width)
        self.oy = self.ox = 1
        height -= 2
        width -= 2

        self.draw_header(stdscr, width)
        self.draw_footer(stdscr, height, width)

        top = 3
        bottom = height - 2
        avail = bottom - top
        # The key list is NOT a view: it floats over whatever you were looking at (drawn
        # after the body, below the pickers), because it answers "what can I press *from
        # here*" and the answer is only legible with `here` still on screen.
        if self.show_prices:
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

        if self.help:
            self.draw_help(stdscr, top, bottom, width)
        if self.toast_history:
            self.draw_toast_history(stdscr, top, bottom, width)

        # Small centered modals float on top of the current view (so context stays
        # visible behind them), unlike the full-body prices/trends overlays.
        if self.price_prompt:
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
            f"subagents {summary['subagents']} "
        )
        self.write(stdscr, 0, 0, title, curses.color_pair(2) | curses.A_BOLD)
        # Source chip, always visible (and live-switchable with `H`): which backend
        # this data comes from — OpenCode / Claude Code / both.
        chip = f" {self.store.source_name} "
        self.write(stdscr, 0, len(title), chip, curses.color_pair(7) | curses.A_BOLD)
        # No what-if tag here on purpose: an armed `w` target changes nothing the header
        # counts (it reprices one session's tree on its Subagents tab, which titles and
        # caveats itself), so tagging the header would call recorded spend counterfactual.
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
            # Named as the COLUMN, lowercased -- the Trends rankings share two keys
            # ("name"/"count") across four tables that call them different things, and
            # "sort: count" names nothing the screen shows.
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
        # Transient status lives in floating toasts now (draw_toasts), not the header.
        for text, attr in segs:
            x = self.write_seg(stdscr, 1, x, text, attr, width)
        self.draw_mode_tabs(stdscr, 2, width)

    def draw_mode_tabs(self, stdscr: curses.window, y: int, width: int) -> None:
        # The top-level browse-mode strip on the header's rule row: Time · Projects ·
        # [Machines], the active one accented. It makes the whole navigation self-
        # describing (the t/p/m keys were invisible) and is clickable (modetab regions);
        # the rest of the row stays the header/body separator rule.
        tabs = self.mode_tab_list()
        modes = [m for _lbl, m in tabs]
        active_index = modes.index(self.browse_mode) if self.browse_mode in modes else 0
        labels = [
            f"[{lbl}]" if i == active_index else f" {lbl} " for i, (lbl, _m) in enumerate(tabs)
        ]
        gap = 1
        total = sum(len(lbl) for lbl in labels) + gap * (len(labels) - 1)
        # Center the strip on the rule row: ──── [Time] Projects Machines ────. The chips
        # carry their own panel2/accent background, so they sit on the rule as real tabs.
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
        # (Machines mode returns below without any drill crumb -- a box's drills are
        # mutually exclusive and its breadcrumb is already machines : <box> : <tab>.)
        if self.browse_mode == "machines" and self.view != "session":
            machine = self.selected_machine_summary
            segs.append("machines")
            if machine:
                segs.append(machine.name)
            segs.append(tab_name)
            return sep.join(s for s in segs if s)
        if self.browse_mode == "projects" and self.view != "session":
            project = self.selected_project_summary
            segs.append("projects")
            if project:
                segs.append(short_path(project.directory, 34))
            segs.extend(self._drill_crumbs(self.on_sessions_tab))
            segs.append(tab_name)
            return sep.join(s for s in segs if s)
        if self.view == "session":
            if self.browse_mode == "machines":
                machine = self.selected_machine_summary
                if machine:
                    segs.append(machine.name)
            elif self.browse_mode == "projects":
                project = self.selected_project_summary
                if project:
                    segs.append(short_path(project.directory, 34))
            elif self.focus == "years":
                if self.focused_year:  # month label already carries the year, so only
                    segs.append(self.focused_year)  # show a bare year when that's the scope
            elif self.focused_month:
                segs.append(self.focused_month)
            # The day/zoom_project crumbs are time-mode locators only; machines mode has
            # neither, so it must not borrow the sidebar's inherited focused month/day.
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
            if self.zoom_project and self.on_sessions_tab:
                segs.append(short_path(self.zoom_project, 24))
            segs.extend(self._drill_crumbs(self.on_sessions_tab))
            segs.append(tab_name)
        elif self.focus == "months":
            if self.focused_month:
                segs.append(self.focused_month)
            if self.zoom_project and self.on_sessions_tab:
                segs.append(short_path(self.zoom_project, 24))
            segs.extend(self._drill_crumbs(self.on_sessions_tab))
            segs.append(tab_name)
        else:
            if self.focused_month:
                segs.append(self.focused_month)
            if self.active_day:
                segs.append(self.active_day)
            if self.zoom_project and self.on_sessions_tab:
                segs.append(short_path(self.zoom_project, 24))
            segs.extend(self._drill_crumbs(self.on_sessions_tab))
            segs.append(tab_name)
        return sep.join(s for s in segs if s)

    def _drill_crumbs(self, armed: bool = True) -> list[str]:
        # The drills armed inside a zoom, innermost last. A model drill goes after the
        # partitions because it LAYERS on them (a membership filter), so it reads as the
        # deepest scope and matches the order Esc pops them in. Without a crumb an armed
        # model is invisible the moment you leave the Models tab you set it from -- the
        # Sessions list just reads short, which is how a filter gets mistaken for a bug.
        if not armed:
            return []
        return [c for c in (self.zoom_source, self.zoom_machine, self.zoom_model) if c]

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
                f"   {self._keys('filter', 'up', 'down')} select"
                f"  {self._key('filter', 'confirm')} keep"
                f"  {self._key('filter', 'cancel')} cancel"
                f"  {self._key('filter', 'clear')} clear",
                curses.color_pair(4),
                width,
            )
            return
        # The chips come from the keymap table (keymap.KEYS), the same one the `?`
        # overlay lists -- so a key can never be offered down here and go unexplained up
        # there, nor be offered in a context that swallows it. Each entry is one or more
        # (label, active) segments; an active toggle -- its overlay or mode is ON, its
        # session bookmarked -- renders in the orange accent, so the strip reflects state
        # at a glance. e export / o open stay help-only: the footer keeps navigation,
        # toggles with visible state, and the overlay openers.
        parts: list = keymap.footer_parts(self.app)
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

    # Each list's column headings read that list's own sort pair -- never the
    # context-dependent effective_sort_by -- so when a project list and a session
    # list share the screen (projects mode) neither borrows the other's arrow.
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
        # A Trends ranking's column heading. Takes the TAB explicitly rather than
        # reading the active one: the four tables are also drawn straight (the suite,
        # and a frame built before the overlay's tab index moves), and a heading that
        # asked "which tab is selected" would arrow the wrong column there.
        if self.app.trend_sort_key(tab) != key:
            return label
        desc = self.sort_descending(key, self.app.trend_sort_reverse_for(tab))
        return f"{label} {'v' if desc else '^'}"

    def _scope_spans_days(self) -> bool:
        # True whenever the session list spans more than the one scoped day: projects and
        # machines modes (a box's whole history), a month, a year, or "All years". Only a
        # focused single day in time mode shares one date across every row.
        return self.browse_mode in ("projects", "machines") or self.focus != "days"

    def session_started(self, workflow: Workflow) -> str:
        # Date when the scope spans more than a day; a bare clock time only when every
        # row shares the scoped day.
        return workflow.created_at[:10] if self._scope_spans_days() else workflow.created_at[11:16]

    def session_date_label(self) -> str:
        return "Started" if self._scope_spans_days() else "Time"

    def session_date_column(self) -> tuple[str, str]:
        # The Date column's (sort key, header label) pair -- swaps to the activity
        # timestamp under a "last_activity" sort so the visible order is legible: the
        # column you're sorted by is the one shown, not always the session's start.
        # "Last act" (not "Last act.") is deliberate: the header field is `:<10` and
        # sort_heading() always appends a " v"/" ^" arrow, so anything over 8 chars
        # overflows the column and shifts every header after it -- "Started"/"Time"
        # both clear it too, just with room to spare.
        if self.session_sort_key() == "last_activity":
            return ("last_activity", "Last act")
        return ("date", self.session_date_label())

    def session_date_cell(self, workflow: Workflow) -> str:
        if self.session_sort_key() != "last_activity":
            return self.session_started(workflow)
        # Always a date, never a bare clock time: App.active_session_sort_options()
        # makes "last_activity" unreachable in a single-day scope (browse_mode=="time"
        # and focus=="days" -- the one case _scope_spans_days() is False), so a session
        # sorted by activity is always shown across a scope wide enough that a clock
        # time alone would be ambiguous anyway.
        return (workflow.ended_at or workflow.created_at)[:10]

    # The line a ruled box's column header sits on: index 0 is the titled top border.
    # Boxed line-based tables register their sort zones against this.
    BOX_HEADER_LINE = 1

    TOP_SESSIONS_LIMIT = 20  # the Overview previews are leaderboards, not the full list

    def top_sessions(self, rows: list[Workflow]) -> list[Workflow]:
        ranked = sorted(rows, key=lambda item: (item.total_cost, item.total_tokens), reverse=True)
        # A top-N slice: the Sessions tab is the full, navigable list -- the Overview
        # box (month/year/day/project) only wants the headline few, or a busy month
        # spills hundreds of rows past the pane.
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

    def note_tag(self, workflow: Workflow) -> str:
        # "✎ " for a session carrying a note (`n`) — the note itself is too long
        # for a list row, but *that there is one* is exactly what a list must say.
        return "✎ " if self.note_for(workflow.id) else ""

    def session_marks(self, workflow: Workflow) -> str:
        # The user-authored marks that ride in front of a session title, wherever
        # one is listed: starred, annotated.
        return self.bookmark_tag(workflow) + self.note_tag(workflow)

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
        # The "Hns" (harness) column in the session tables (None = the header cell), only
        # when harnesses are merged — the one view where a row's origin isn't implied.
        if not getattr(self.store, "combined", False):
            return ""
        if workflow is None:
            return "Hns "
        return f"{self._source_abbrev(workflow):<3} "

    MACHINE_COL_W = 8

    def mach_col(self, workflow: Workflow | None = None) -> str:
        # The "Machine" column in the session tables (None = the header cell), only in
        # the fleet view (--pull/--remote) where sessions span machines. Full-ish name
        # (hostnames don't abbreviate to two letters the way harness names do).
        if not self.machines_present:
            return ""
        w = self.MACHINE_COL_W
        if workflow is None:
            return f"{'Machine':<{w}} "
        return f"{pad(shorten(workflow.machine or '?', w), w)} "

    # --- The session table: one builder, two frames -------------------------------
    # The browse preview (lines) and the zoom picker (navigable) render the SAME
    # table through session_columns/session_header_text/session_row_text, so Enter
    # only lights up a row -- it never re-shapes the columns under you. They were two
    # hand-written tables once, and drifted: different columns (Models/Src vs
    # Project), a stray heading line, a 2-column indent shift, a different header
    # colour. Add a column here and both frames get it. (The project list already
    # worked this way: project_header_text/project_row_text.)
    SESSION_TITLE_MIN = 24  # room the title keeps before an optional column earns its cells
    SESSION_PROJECT_MAX = 20

    def session_columns(self, sessions: list[Workflow], width: int) -> tuple[bool, int, bool]:
        # The optional cells this pane can afford: (Models, Project width, Duration).
        # Both frames measure the same pane, so a column can't appear on Enter and
        # vanish on Esc. A squeezed pane drops Models first (a bare count), the
        # Project column second, Duration third, and never the title -- a session
        # list is read by its titles.
        proj_w = 0
        if self.sessions_span_projects():
            # Only where the list can mix projects; sized to the longest name on show
            # (the _model_table pattern), capped so one deep path can't own the row.
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
            prefix = len(self.session_header_text(models, proj, dur)) - len(title)
            if width - prefix >= self.SESSION_TITLE_MIN:
                return models, proj, dur
        return False, 0, False

    def session_header_text(self, models: bool, proj_w: int, dur: bool = True) -> str:
        header = f"  {self.sort_heading(*self.session_date_column()):<10} "
        if dur:
            header += f"{self.sort_heading('duration', 'Worked'):>8} "
        header += (
            f"{self.sort_heading('cost', 'Cost'):>9} "
            f"{self.sort_heading('tokens', 'Tokens'):>8} "
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
        # "· worked 2h 13m (until 16:42)" appended to a session's Started line: the
        # agent's active working time (idle waits removed), and, if the backend also
        # recorded a last-activity stamp, when that work last happened. Same-day ends
        # show a bare clock time; a session that ran into another day keeps the date.
        seconds = workflow.worked_seconds
        if seconds is None:
            return ""
        ended = workflow.ended_at
        if not ended:
            return f"   · worked {human_duration(seconds)}"
        until = ended[11:16] if ended[:10] == workflow.created_at[:10] else ended[:16]
        return f"   · worked {human_duration(seconds)} (until {until})"

    def session_duration(self, workflow: Workflow) -> str:
        # How long the agent actually worked on this session, idle waits excluded.
        # Blank when the backend can't tell work from waiting (a source with no
        # human-turn markers, an old --export) -- never a fake 0s.
        seconds = workflow.worked_seconds
        return human_duration(seconds) if seconds is not None else ""

    def session_row_text(
        self, workflow: Workflow, marker: str, models: bool, proj_w: int, dur: bool = True
    ) -> str:
        text = f"{marker} {self.session_date_cell(workflow):<10} "
        if dur:
            text += f"{self.session_duration(workflow):>8} "
        text += (
            f"{money(workflow.total_cost):>9} "
            f"{human_tokens(workflow.total_tokens):>8} "
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
        # The boxed TOTAL row, laid out by session_row_text's own fields so the sums sit
        # under the columns they sum. Everything past Subagents is identity, not quantity
        # (a model count, a harness, a title), so those cells stay blank rather than
        # inventing a sum. Worked time sums only the sessions whose backend recorded it --
        # a blank stays blank, never a fake 0s.
        worked = [wf.worked_seconds for wf in sessions if wf.worked_seconds is not None]
        text = f"  {pad('TOTAL', 10)} "
        if dur:
            text += f"{human_duration(sum(worked)) if worked else '':>8} "
        text += (
            f"{money(sum(wf.total_cost for wf in sessions)):>9} "
            f"{human_tokens(sum(wf.total_tokens for wf in sessions)):>8} "
            f"{sum(wf.subagents for wf in sessions):>11} "
        )
        if models:
            text += f"{'':>6}  "
        text += " " * display_width(self.src_col() + self.mach_col())
        if proj_w:
            text += f"{'':<{proj_w}}  "
        return text

    def session_sort_columns(self, proj_w: int, dur: bool = True) -> tuple:
        # (sort_key, label) in drawn order, for the clickable headers of both frames.
        columns = [self.session_date_column(), *self.SESSION_SORT_COLUMNS]
        if dur:
            columns.insert(1, ("duration", "Worked"))  # right after the date cell
        if proj_w:
            columns.insert(-1, ("project", "Project"))  # between Subagents and Title
        return tuple(columns)

    def preview_session_source(self) -> list[Workflow] | None:
        # Same rows the picker will show, not just the same columns: with `i` on and
        # ignored sessions about, App.current_sessions widens from all_workflows to
        # ranged_workflows, and a preview that didn't would drop a row that Enter then
        # conjures back. None = the default (all_workflows).
        return self.ranged_workflows if self._showing_ignored_workflows() else None

    def preview_project_source(self) -> list[Workflow] | None:
        # The same, for the project lists: App.zoom_projects widens under `i` (and
        # keeps the ignored rows, which it marks "×") -- so the preview must too.
        return self.ranged_workflows if self.show_ignored_projects else None

    def scoped_sessions(self, rows: list[Workflow]) -> list[Workflow]:
        # The sessions a scope actually shows, for the tables that COUNT them (the
        # Sources tab) rather than list them: the committed `f` query narrows the
        # sessions list, so a Sources row aggregating past it would advertise spend
        # that Enter on it then refuses to open.
        return self.filtered_sessions(rows)

    @staticmethod
    def sessions_box_title(sessions: list) -> str:
        return f"Sessions · {len(sessions)}" if sessions else "Sessions"

    @staticmethod
    def projects_box_title(projects: list) -> str:
        return f"Projects · {len(projects)}" if projects else "Projects"

    def session_table(self, rows: list[Workflow], width: int) -> list[str]:
        # The browse preview of a Sessions tab: the picker's table minus the cursor, in the
        # same ruled box. The tab's name rides the BORDER rather than a heading line above
        # the table -- a heading would shift every row when the picker takes over on Enter,
        # which is why this table had none at all before it was boxed.
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
        # The header is line 1 of the box (below the titled top border), and the click
        # zones are located in the text actually drawn -- so the gutters shift them right
        # by exactly the two cells the paint also shifts the line by.
        self._line_sort_headers[self.BOX_HEADER_LINE] = (
            self.session_sort_columns(proj_w, dur),
            "session",
        )
        return lines

    def unpriced_hint(self) -> str:
        # Trails any block whose totals include $0.00 subscription tokens. Worded
        # per price mode so it never says "not billed" beside estimated dollars.
        if self.show_api_prices:
            return "! estimates — subscription tokens at API list prices"
        return (
            "! $0.00 = subscription tokens — press "
            f"{self._key('main', 'api_prices')} to estimate"
        )

    def line_attr(self, line: str) -> int:
        # Shared prefix styling for the text panes: "# " section titles (accent -- they
        # were structural grey and read as chrome, not headings), "! " caveats (amber --
        # attention without alarm; red is for errors and the error toast only), "· "
        # explainer captions (dim).
        if line.startswith("# "):
            return curses.color_pair(2) | curses.A_BOLD
        if line.startswith("! "):
            return curses.color_pair(2)
        if line.startswith("▼ "):
            # A Turns-tab compaction marker: the same amber the Context tab's ▼ rows get
            # from _CTX_MARK, so one event reads as one thing on both tabs.
            return curses.color_pair(2) | curses.A_BOLD
        if line.startswith("❄ "):
            # A Turns-tab cache expiry. Red rather than the ▼ amber, and this is the one
            # place in the panes that earns it: every other number opentab shows is money
            # already spent on work you got, while this is money spent buying a context
            # you had already bought -- the only line in the app that reports waste.
            return curses.color_pair(4) | curses.A_BOLD
        if line.startswith("· "):
            return curses.color_pair(1)
        if line.startswith("TOTAL"):
            # A totals line that is NOT inside a ruled table -- the w tab's
            # "TOTAL (list rates)" footer. Bold ink, no background bar (the model
            # tables carry their own boxed TOTAL row now, and the bar read as mud).
            return curses.A_BOLD
        # The ruled model/tool tables (_ruled_box): the title rides the top border in
        # the accent (matching its "# " sibling headings), the header/total rules and
        # bottom border stay plain, and the boxed TOTAL row is bold. Keyed on the leading
        # glyph -- ASCII "+" is a top border only when it carries a title (has letters),
        # else a plain rule/bottom. Model/tool cells never contain box glyphs, so a data
        # row never trips these.
        first = line[:1]
        if first == "┌" or (first == "+" and line.strip("+- ") != ""):
            # A ruled-box top border carrying a title -- the accent, matching its "# "
            # sibling headings above.
            return curses.color_pair(2) | curses.A_BOLD
        if first in ("├", "└", "+"):
            return curses.A_NORMAL
        if first in ("│", "|"):
            content = line[2:].lstrip()
            # A Turns-tab marker row inside the box: the ▼/❄ events sit between the prompt
            # rows they happened between, so the glyph tests above have to reach past the
            # gutter to keep them amber/red rather than flattening them into table rows.
            if content[:1] in ("▼", "❄"):
                return self.line_attr(content)
            # The Money card's armed what-if rows are marked with a leading ★ and painted
            # in the orange accent (pair 6 -- the same state-emphasis colour as the Turns
            # ▸ headers and the narrowed-view chip), so the counterfactual pops off the
            # white recorded-cost rows above it (money spans keep their own colour on top).
            if content.startswith("★"):
                return curses.color_pair(6) | curses.A_BOLD
            # The boxed TOTAL row -- "TOTAL" as a whole word (it is always followed by the
            # count column's separator), so a model/tool named "TOTALizer" isn't mistaken
            # for it.
            return curses.A_BOLD if content.startswith("TOTAL ") else curses.A_NORMAL
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

    # What the picker frame costs a scrolling list: the top border, the header row and
    # the rule under it, plus the bottom border. Subtracted from the rows a pane can
    # show, and added back by the callers that size a body against `h`.
    PICKER_CHROME = 4

    def picker_box_width(self, w: int) -> int:
        # The content width a boxed picker's rows get from a pane `w` wide. The browse
        # PREVIEW of the same table must measure with this too -- both frames size their
        # optional columns off it, and a column that appeared on Enter and vanished on
        # Esc is exactly the drift the shared builders exist to prevent.
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
        # The ruled box a NAVIGABLE list sits in -- the static tables' frame (_ruled_box)
        # made scrollable. The static ones can assemble their whole box as strings up
        # front; a picker only knows how many rows it will draw after it has clamped its
        # window against the pane, so the frame is painted around it in pieces instead.
        # Same glyphs, same titled top border, same rule under the header, so the two
        # cannot drift apart.
        #
        # Returns (first body y, content x, inner width). The caller paints `nrows` rows
        # at that x and MUST NOT draw more than it declared -- the bottom border is
        # painted here, below them.
        outer = max(5, w - 4)
        inner = outer - self.BOX_CHROME
        cx = x + 2 + 2
        self.write(
            stdscr, cy, x + 2, self.box_top(title, outer), curses.color_pair(2) | curses.A_BOLD
        )
        # The framed header, painted by the same helper the line-based tables use, so the
        # two frames of one table cannot end up wearing different headers.
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
        # One row inside a draw_picker_frame box: the gutters, then the cells. A selected
        # row reverses only the cells (paint_cursor_row), so the highlight sits inside the
        # table rather than punching a hole through its vertical rules.
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
        # Navigable session list on the Sessions tab of a zoomed month/day/project:
        # the browse preview's table (session_table), made selectable.
        sessions = self.current_sessions()
        cy = y + 3
        # Measured against the BOXED width, exactly as session_table's preview is, so a
        # column can't appear on Enter and vanish on Esc.
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
            self.paint_picker_row(
                stdscr,
                body_y + off,
                x,
                cx,
                inner,
                self.session_row_text(wf, marker, models, proj_w, dur),
                start + off == idx,
                money(wf.total_cost),
                human_tokens(wf.total_tokens),
            )

    def draw_projects_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        # Navigable project list on the Projects tab of a zoomed month/day.
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
        # Navigable source list on the Sources tab of a zoomed scope (merged view):
        # j/k pick a tool, Enter its sessions within this scope.
        self._draw_dimension_picker(
            stdscr, y, x, h, w, self.zoom_source_rows(), self.source_index, "Harness", "zoomsource"
        )

    def draw_machines_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        # The Sources picker's twin on the fleet's per-scope Machines tab: j/k pick a box,
        # Enter its sessions within this scope. Shared body, so the two can't drift.
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
        # The shared navigable ranked-spend picker behind the Sources and Machines tabs of
        # a zoomed scope: a name column, a cost bar, Cost/Share/Tokens/Sess, and Enter to
        # narrow the Sessions list to the selected row. `col` is the name-column header,
        # `region_kind` the click region (zoomsource / zoommachine).
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
        # Every tab is a chip: the active one filled with the accent (pair 7) and wearing
        # [brackets] (the monochrome/pair-starved fallback for "which is active"), the
        # inactive ones a raised panel2 chip (_TAB_PAIR) so they read as tabs, not grey
        # text. `center` offsets the whole strip within `width` (the detail tab bars center
        # over their pane); the modals leave it left-aligned.
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
        # lazygit's numbered panels: the key that jumps here is written into the box
        # title, so the keymap is on screen instead of in the footer. The sidebar is
        # numbered top to bottom (1/2/3) and the detail pane on the right is 0.
        return f"[{number}] {title}" + (" ▸" if active else "")

    def draw_year_list(
        self, stdscr: curses.window, y: int, x: int, h: int, w: int, active: bool = True
    ) -> None:
        self.box(stdscr, y, x, h, w, self.panel_title(1, "Years", active), active=active)
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
        # Projects mode has a single left panel, so it is panel 1 here.
        self.box(stdscr, y, x, h, w, self.panel_title(1, "Projects", active), active=active)
        rows = self.projects
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No projects in range.", curses.color_pair(1))
            return

        # The same header every table in the app wears (_paint_box_header). This one
        # carries no box -- the panel frame around the sidebar already is one, and a
        # second frame inside a ~40-column list would spend four of them on chrome --
        # so it takes the LOOK without the gutters.
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
        if current == "Overview":
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
        # The Machines-mode sidebar: one row per box, the live one first (● / ○), a spend
        # bar-free stat line. Panel 1 (single left list, like Projects mode).
        self.box(stdscr, y, x, h, w, self.panel_title(1, "Machines", active), active=active)
        rows = self.machines
        if not rows:
            self.write(stdscr, y + 2, x + 2, "No machines in range.", curses.color_pair(1))
            return
        header = self.machine_header_text(w - 2)  # the shared header look, see draw_project_list
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
        title = "Machine" if machine is None else f"Machine {shorten(machine.name, max(8, w - 12))}"
        self.box(stdscr, y, x, h, w, self.panel_title(0, title), active=active)
        if machine is None:
            self.write(stdscr, y + 2, x + 2, "No machine selected.", curses.color_pair(1))
            return

        self.draw_tabs(stdscr, y + 1, x + 2, w - 4, self.current_tabs(), self.tab, center=True)

        current = self.current_tabs()[self.tab % len(self.current_tabs())]
        # Sessions drills into a session; Harnesses/Projects/Models each drill into the
        # box's sessions along that dimension (the navigable pickers the Projects-mode
        # detail uses, plus a Models one). Overview stays a read-only breakdown.
        if current == "Sessions" and self.view == "zoom":
            self.draw_sessions_picker(stdscr, y, x, h, w)
            return
        if current == "Harnesses" and self.view == "zoom":
            self.draw_sources_picker(stdscr, y, x, h, w)
            return
        if current == "Projects" and self.view == "zoom":
            self.draw_projects_picker(stdscr, y, x, h, w)
            return
        if current == "Overview":
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
        # The Machines-mode main view -- the niceties the plain rollup can't give: live vs
        # pulled, when it was last pulled and by which opentab, plus this box's model mix
        # and its top projects (the "different machines had different stories" cut).
        workflows = self.workflows_for_machine(machine.name)
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
            # The one-box case: this view is complete, but it is also where you find out
            # the axis exists at all -- so say how a second box joins it. One SHORT line:
            # detail lines are clipped, not wrapped, and this pane can be narrow.
            lines += ["", "Only this machine. `opentab --pull HOST` adds another."]
        if machine.live:
            # A pulled box carries only rollups -- no per-model rows travel in a summary,
            # so there is nothing to decompose and the box would render "no priceable
            # usage" beside a real cost. Live boxes have the rows.
            lines.append("")
            lines.extend(self._token_economics_box(workflows, width))
        if self.projects_for_workflows(workflows):
            # The shared Top Projects box, not this view's own spelling of it -- same
            # leaderboard, same columns, wherever it appears.
            lines.append("")
            lines.extend(self._top_projects_box(workflows, machine.cost, width))
        lines.append("")
        agg = self.aggregate_models(workflows)
        lines.extend(self._model_table(self._agg_rows(agg), "# Top Models", width))
        return lines

    def machine_models(self, machine: MachineSummary, width: int) -> list[str]:
        agg = self.aggregate_models(
            self.compose_zoom_drills(self.workflows_for_machine(machine.name))
        )
        return self._models_tab(self._agg_rows(agg), "# Machine Model Spend", width)

    def machine_sources(self, machine: MachineSummary, width: int) -> list[str]:
        return self.source_table(
            self.scoped_sessions(self.workflows_for_machine(machine.name)), width
        )

    def machine_projects(self, machine: MachineSummary, width: int) -> list[str]:
        return self.project_table(
            self.projects_for_workflows(self.workflows_for_machine(machine.name)), width
        )

    def machine_workflows(self, machine: MachineSummary, width: int) -> list[str]:
        return self.session_table(self.workflows_for_machine(machine.name), width)

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
        if current == "Overview":
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
        if current == "Overview":
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
        if current == "Overview":
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
            # First frame after a drill-in: the lazy fetches (subagent tree, Turns,
            # Tools) can mean parsing the whole backend on a warm start, so show
            # this placeholder instead of freezing mid-draw; run()'s prefetch tick
            # does the blocking work right after and repaints. Tabs are skipped
            # too -- current_tabs' supports_* gates could trigger the same parse.
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
            # Treemap overhead around the chart itself: title, headline, rule, caption,
            # bottom and the trailing blank, plus a note in the $0 token-fallback mode
            # and a second headline row when the pane is too narrow for one. The table's
            # first data row is its fourth. Budget for the LARGEST form, so that exact
            # row stays visible in either cost mode and at any width.
            lines = self.detail_tools(workflow, w - 4, max(0, visible - 12))
        elif current == "Context":
            lines = self.detail_context(workflow, w - 4)
        else:
            lines = self.detail_overview(workflow, w - 4)

        if current == "Turns" and self.app._turn_follow:
            # j/k/Enter moved or toggled the ▸ cursor -- bring its header into view
            # before the scroll clamp, then consume the request (one-shot).
            self._scroll_turn_cursor_into_view(visible)
            self.app._turn_follow = False
        self.app.scroll = max(0, min(self.app.scroll, max(0, len(lines) - visible)))
        drawn = lines[self.scroll : self.scroll + visible]
        headers = self.box_header_lines(lines) | set(self._line_sort_headers)
        for offset, line in enumerate(drawn):
            attr = self.line_attr(line)
            if current == "Turns" and self.scroll + offset == self._turn_cursor_line:
                # The selected prompt row, in the pickers' own reverse video. Keyed on
                # the LINE INDEX, never on a leading glyph: the rows are ordinary table
                # rows now, and a glyph test silently highlighted nothing at all once the
                # ▸ headers became columns -- j/k moved a cursor the eye could not find.
                # Painted like every other table cursor: reversed BETWEEN the box gutters,
                # and with write() rather than write_rich(), whose money/token overpaint
                # would shred a highlight on a row that is almost all numbers.
                self.paint_cursor_row(stdscr, y + 3 + offset, x + 2, line, w - 4)
                continue
            if self.scroll + offset in headers:
                self._paint_box_header(stdscr, y + 3 + offset, x + 2, line, w - 4)
                self._register_line_sort_header(
                    y + 3 + offset, x + 2, self.scroll + offset, line, w - 4
                )
                continue
            if current == "Context":
                # The context chart's rows are heat-shaded by how full the window is
                # at that height; detail_context stashed each line's level by index
                # (plain data -- the color pair resolves only here, at paint time).
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
            # Make the ▸/▾ headers clickable: the region maps a row back to its line
            # index; _apply_click resolves headers via _turn_header_at.
            self._add_rows_region("turnline", y + 3, x + 2, x + w - 3, self.scroll, len(drawn))

    def _scroll_turn_cursor_into_view(self, visible: int) -> None:
        # The Turns tab's selected ▸ header. Its expanded body flows below it.
        self._scroll_line_into_view(self._turn_cursor_line, visible)

    def _scroll_line_into_view(self, line: int | None, visible: int) -> None:
        # Nudge the scroll so `line` sits within the visible window -- only when it's
        # off-screen, so following a cursor never yanks a row that's already
        # comfortably in view. Shared by the Turns and Models cursors.
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
        #
        # This box CLOSES every Overview -- every scope's, and the session's -- and that
        # placement is deliberate: it is the widest thing on the pane (up to eight columns,
        # one row per model) and it is the least likely answer to "where did the money go".
        # The blocks above it (the stats, Token economics, Top projects/sessions) each fit
        # in a few lines and each name a different axis of the same spend, so they read as
        # a summary; the model table is the detail you scroll to, and it has its own tab in
        # every scope that can afford one. Adding a section? It goes ABOVE this one.
        #
        # Built as a ruled box
        # (_ruled_box): the title rides the top border, the columns sit inside,
        # and a multi-row table closes with a rule + TOTAL row -- no coloured sum bar. The
        # name column fits the longest entry (so the numbers sit right after it), capped by
        # the box-reduced width so long names aren't cut when there's room. name_label/
        # count_label let the Tools tab reuse this as Tool/Calls (which also turns
        # price_split off -- tool names don't resolve to model rates). `selectable`
        # adds a CURSOR and nothing else: the zoomed Models tab is this same table with
        # one row picked out, never a slimmer picker beside it (see _models_tab).
        # The count column fits its widest value -- the TOTAL row's sum, which is >= any
        # single row -- so a 5-digit Msgs count (10,484) doesn't overflow a 4-wide field
        # and cascade a 1-char shove that clips the rightmost column.
        cw_ = max(4, len(count_label), len(str(sum(int(r[1]) for r in rows))) if rows else 0)
        longest = max([len(str(r[0])) for r in rows] + [len(name_label)])
        # Every boxed table opens with the same 2-cell marker gutter (blank here -- this
        # table marks its selection with the reverse bar, not a ">"), so a stack of boxes
        # in one pane has ONE left inset and the columns of adjacent tables line up.
        # Without it the Models/Tools tables started flush at the gutter while the session,
        # project and harness tables above them were indented, and the pane read as two
        # different grids.
        lead = "  "
        inner = max(1, width - self.BOX_CHROME - len(lead))
        # In wide panes the CacheR/CacheW/Output cells carry the tokens' attributed
        # share of the Cost column too -- "811.6k ($10)" -- because counts alone hide
        # how skewed the money is (cache writes bill at 12.5x the cache-read rate on
        # current Anthropic models). Costs 16 more columns than the plain layout, so
        # it only kicks in when the name column still gets its 20-char floor; with
        # no dollars anywhere ($0.00 unpriced rows) there is nothing to attribute.
        split = price_split and any(float(r[2]) > 0 for r in rows) and inner - 80 - cw_ >= 20
        # The split layout gets a two-space gutter between columns: single-spacing packed
        # the "($1.1k)" cells so tight they butted against the next column while the wide
        # pane stranded empty space on the right (the "crowded" look). The plain fallback
        # is the narrow-pane layout, where a one-space gutter keeps the rightmost column
        # from clipping -- so `sep` threads header, body, and TOTAL identically per mode,
        # and the split cells stay aligned under their labels.
        sep = "  " if split else " "
        block = 80 if split else 57
        mw = min(longest, max(20, inner - block - cw_))
        total_cost = sum(float(r[2]) for r in rows)
        if split:
            # Each split cell is two fixed sub-columns -- tokens right-aligned in 6,
            # dollars right-aligned in 8 inside the parens -- so the numbers line up
            # row to row and the label sits exactly over the token half.
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
            # A rule + TOTAL row closes every multi-row table: the count/token columns
            # summed, and in split mode the attributed dollars summed per row at
            # each row's OWN rates -- so "what did cache writes cost me this
            # year" is one glance, not per-model mental math. A single-row table
            # is its own total; Share is definitionally 100%, so it stays blank.
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
        # CLAMP, exactly as App.zoom_selected_model does -- never bail on an out-of-range
        # index. Anything that shrinks the list without moving the cursor (typing an `f`
        # query is the easy one: the move handlers clamp, a keystroke in the filter does
        # not) would otherwise leave Enter drilling the clamped last row while the pane
        # highlighted nothing at all.
        picked = min(max(0, self.app.model_pick_index), len(body) - 1) if body else 0
        if selectable and body:
            # The ">" every navigable table marks its selection with, in the gutter the
            # lead reserves. The reverse bar says the same thing in colour; this says it
            # in ink, which is what a screenshot, a monochrome terminal and a colour-blind
            # reader are left with -- and what made this table the odd one out.
            body[picked] = ">" + body[picked][1:]
        lines = self._ruled_box(title, header, body, total, notes, width)
        self._model_row_at = {}
        self._model_cursor_line = None
        if selectable and self._ruled_body_start is not None:
            start = self._ruled_body_start
            self._model_row_at = {start + i: i for i in range(len(body))}
            self._model_cursor_line = start + picked
        return lines

    # The ruled-box glyphs. Unicode on a UTF-8 screen; the locale-independent ASCII set
    # where a multibyte glyph would land as a garbage byte (util.unicode_screen: a
    # question asked, never caught -- see draw_frame). These are content strings, not the
    # ACS line set frame() draws its panels with, so they take the ASCII fallback instead.
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

    # -- the ruled box, in pieces ------------------------------------------------
    #
    # Every table in the app is drawn in this one frame, whether it is a static block
    # of text (the Models/Tools tables), a line-based pane with a cursor (Turns), or a
    # self-painting scrolling picker (Sessions/Projects/Harnesses). The pieces are split
    # out because those three build their box differently -- the first two assemble a
    # list of strings up front, the third paints borders around a window it only sizes
    # at draw time -- and a second hand-rolled frame is exactly how the two frames drift
    # apart (they did, once: the Sessions preview and its picker grew different columns).
    # BOX_CHROME is what a box costs a caller sizing content against a pane.

    BOX_CHROME = 4  # "| " + " |": the cells a box takes off the content width

    @classmethod
    def box_glyphs(cls) -> dict:
        # Unicode on a UTF-8 screen, the locale-independent ASCII set otherwise. Asked,
        # never caught -- see the _TABLE_GLYPHS note.
        return cls._TABLE_GLYPHS if unicode_screen() else cls._TABLE_GLYPHS_ASCII

    @classmethod
    def box_top(cls, title: str, width: int) -> str:
        # The titled top border. A titled box needs at least "| x |" (5 cells); below
        # that the frame can't be square, so `width` is clamped and the paint clips the
        # clamped box to the actual pane.
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
        # One content line inside the gutters, padded/clipped to the inner width so the
        # frame stays square even on a narrow pane -- at the cost of the rightmost column
        # there (the pre-box tables just overflowed the pane and were clipped at paint).
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
        # Wrap a column-aligned table in a ruled box: the title on the top border, a rule
        # under the header, the data rows, an optional rule + TOTAL row, then the bottom
        # border. width is the OUTER width. line_attr colours the pieces by their leading
        # glyph: the titled top in the accent, the TOTAL row bold. Any "! ..." caveat
        # rides OUTSIDE the box, below it, as its own amber line.
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
        # A ruled box like _ruled_box, but its body is several row GROUPS separated by
        # rules -- the Overview's Money card stacks the cost breakdown and, below a rule,
        # the armed what-if rows. Same glyphs, same titled top border (line_attr paints it
        # in the accent), same "! ..." notes riding outside the box below it. Empty groups
        # are dropped so a rule never opens onto nothing.
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
        # The (cacheR, cacheW, output) dollar attribution behind the split cells:
        # tokens weighed at the same list rates api_equivalent_cost bills them at,
        # then scaled so the three shares plus the implicit input remainder sum to
        # the row's Cost -- exact for $-estimated rows (same math), honest
        # attribution for recorded costs that predate today's rates. A row with no
        # dollars, or a model with no rates at all, attributes nothing.
        ir, orr, crr, cwr = model_price(name)
        inp = max(0, tok - cr - cw - out)
        raw = (inp * ir, cr * crr, cw * cwr, out * orr)
        total = sum(raw)
        scale = cost / total if cost > 0 and total > 0 else 0.0
        return (raw[1] * scale, raw[2] * scale, raw[3] * scale)

    @staticmethod
    def _split_cell(tokens_n: int, dollars: float) -> str:
        # One "tokens(dollars)" cell, 14 wide with fixed sub-columns -- tokens
        # right-aligned in 6, then the whole "($13)" group right-aligned in 8 so
        # the parens hug the amount (no inner gap) while the amounts stay flush
        # right row to row.
        label = f"({money_label(dollars)})" if dollars > 0 else ""
        return f"{human_tokens(tokens_n):>6}{label:>8}"

    @staticmethod
    def _price_split_cells(
        name: str, cost: float, tok: int, cr: int, cw: int, out: int
    ) -> tuple[str, str, str]:
        # "tokens(dollars)" cells: each category's attributed share of the row's
        # Cost (_price_split_dollars), rendered in the fixed _split_cell shape.
        d = Renderer._price_split_dollars(name, cost, tok, cr, cw, out)
        return (
            Renderer._split_cell(cr, d[0]),
            Renderer._split_cell(cw, d[1]),
            Renderer._split_cell(out, d[2]),
        )

    def _models_tab(self, rows: list[tuple], title: str, width: int) -> list[str]:
        # The Models tab body, with the live `f` filter applied to model names.
        # Unlike sessions we keep the cost ranking rather than re-ranking by fuzzy
        # score: model lists are short and the cost order is the useful one.
        #
        # In a zoom the SAME table becomes navigable -- a cursor plus Enter, drilling to
        # the scope's sessions that used the model. Deliberately not a separate picker
        # beside it: this tab used to render one table in browse (eight columns, price
        # splits, a TOTAL row) and a four-column ranked list on Enter, which is exactly
        # the drift the Sessions/Projects tables were unified to end -- and it silently
        # re-pointed `f` from model names at session titles on the way. So `selectable`
        # adds a cursor and nothing else: same rows, same columns, same filter.
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
        # Repaint a Token economics bar (or its legend) segment by segment, after
        # write_rich has laid the line down in one attribute. Same two-pass shape
        # write_rich itself uses for money/token runs: draw once, then overpaint the
        # spans that own a colour. A line with no recorded runs -- every other line in
        # the app -- costs one dict miss.
        runs, shift = self._token_runs.get(line), 0
        if runs is None and len(line) > 4 and line[0] in "│|" and line[-1] in "│|":
            # The bar lives inside a ruled box, so the painted line is the recorded one
            # wrapped in "│ … │" gutters. Look up the content and shift the runs by the
            # gutter rather than making the builder predict its own wrapping -- it is
            # spliced into six different Overviews and cannot know.
            #
            # Strip exactly the frame ("│ " + content padded to width + " │") and then
            # only the PADDING, never trailing content characters: the flamegraph's
            # labels are session titles, and rstrip("│|") ate the last character of any
            # label that happened to end in a pipe, so its swatch lost its colour.
            runs, shift = self._token_runs.get(line[2:-2].rstrip(" ")), 2
        if not runs:
            return
        for col, length, slot in runs:
            col += shift
            if col >= width:
                break  # the pane clipped this segment away entirely
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
        # Repaint complete tile spans after write_rich, which would otherwise turn
        # embedded dollar labels green. Each pair owns contrast-safe ink + heat fill.
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
        # A solid cell when the five colour pairs took, a per-type density glyph when
        # they didn't (a pair-starved terminal). Colour and glyph carry the SAME
        # distinction, so exactly one of them is ever in play.
        return "█" if self._token_series_ok else TOKEN_SERIES_GLYPHS[slot]

    @staticmethod
    def _stack_widths(rows, total: float, cells: int) -> list[int]:
        # The 100%-stacked bar's per-segment widths, summing to exactly `cells`.
        # Split out from _token_stack_line because the flamegraph's label row has to sit
        # under the segments it names, and geometry computed twice is geometry that
        # drifts by a cell and puts every name half a segment to the left.
        #
        # Cumulative rounding, so there is no drift opening a gap at the right edge, and
        # every positive value keeps at least one cell -- a type that cost real money is
        # never invisible. `bump` is that floor, paid per positive row and reserved out
        # of the width first; both halves of it drop together once the positive segments
        # outnumber the cells, because paying an unpayable floor builds a line LONGER
        # than the pane, which the box then clips -- silently dropping whatever sits at
        # the right edge. (Chart 1 has five segments and cannot reach that; the
        # flamegraph's count is the session's subagent count.)
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
        short = cells - sum(widths)  # rounding can leave a cell unclaimed
        if short > 0 and widths:
            widths[widths.index(max(widths))] += short
        return widths

    def _token_stack_line(self, rows, total: float, cells: int, labels=None, share_fmt=None) -> str:
        # One 100%-stacked bar as a single string, plus the per-segment colour runs it
        # needs, stashed in _token_runs keyed by the line TEXT rather than its index --
        # the box is spliced into a bigger line list by six different Overviews, so it
        # cannot know its final offsets, and two identical bar strings want identical
        # colouring anyway.
        #
        # Widths come from cumulative rounding so they sum to exactly `cells` with no
        # drift opening a gap at the right edge, and every positive value keeps at least
        # one cell -- a type that cost real money is never invisible. That floor is
        # reserved out of the width first, so honouring it can never overflow the bar.
        widths = self._stack_widths(rows, total, cells)
        runs: list[tuple[int, int, int]] = []
        text, col = "", 0
        for i, ((_label, value, slot), w) in enumerate(zip(rows, widths)):
            if w <= 0:
                continue
            glyph = self._token_glyph(slot)
            # `share_fmt` lets a caller impose its own rounding rule (the flamegraph
            # guards both ends, so a sliver never reads "0%" and a near-total never
            # reads a flat "100%" beside the segments it is standing next to). Default
            # is chart 1's plain round, so its five bars are untouched.
            share = ""
            if total > 0:
                share = (
                    share_fmt(value / total) if share_fmt else f"{round(100.0 * value / total)}%"
                )
            # The percentage rides INSIDE its segment when it fits with a cell of air on
            # each side; otherwise the legend and the table carry it, rather than
            # smearing two digits across a three-cell sliver. Only when colour is doing
            # the separating -- with glyphs in play the fill itself is the identity and
            # punching a label through it would break the one distinction left.
            # `labels` (the flamegraph) offers a NAME to ride in front of the share when
            # the segment is roomy enough for both -- a wide frame that says what it is
            # beats a wide frame you have to look up in the legend. It degrades to the
            # bare share, then to plain fill, so the fallback is chart 1's exact rule.
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
        # "<swatch> Cache read  <swatch> Output …" -- every type keyed to its fill, so a
        # segment too narrow for its own label is still identifiable without dropping to
        # the table. Wraps onto more rows rather than letting the box clip it: a legend
        # missing its last entry is worse than a legend two rows tall, because the
        # clipped type is exactly the small one whose segment you couldn't read either.
        # Built and measured in one pass so each row's runs land on its own swatches.
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

    def _token_economics_box(self, workflows: list[Workflow], width: int) -> list[str]:
        # "Where the money actually goes": the same five token types measured twice --
        # what you SENT, then what you PAID -- as two 100%-stacked bars, one above the
        # other. The reading is the gap between them (a type that is most of the first
        # bar and a sliver of the second), which is why they share a scale and a colour
        # per type. One ruled box holds the bars, the legend and the numbers as three
        # sections, so the chart reads as one object rather than loose lines above a
        # table -- and on a pane too narrow for five legible segments the bar section
        # drops out and the numbers alone still answer.
        #
        # Always list rates (App.token_economics explains why nothing else is
        # decomposable), so the title says so rather than following "$".
        econ = self.app.token_economics(workflows)
        if econ is None:
            return self._ruled_box(
                "# Token economics", "no priceable usage here", [], None, [], width
            )
        approx = "~" if econ.estimated else ""
        inner = max(1, width - 4)
        # `slot` is the token TYPE's index, carried with the row so a type owns one
        # colour in both bars, in the legend and in the table however the cost ordering
        # moves it around.
        rows = [
            (label, econ.tokens[i], econ.cost[i], i)
            for i, label in enumerate(TOKEN_TYPES)
            if econ.tokens[i] > 0 or econ.cost[i] > 0
        ]
        rows.sort(key=lambda r: (r[2], r[1]), reverse=True)

        def share_text(value: float, total: float) -> str:
            # NOT formatting.pct: it floors everything under 1% to "<1%", and here the
            # sub-percent rows are the punchline -- output is half a percent of the
            # tokens and a sixth of the bill, which "<1%" can't say.
            share = 100.0 * value / total if total > 0 else 0.0
            if share >= 10 or share == 0:
                return f"{share:.0f}%"
            if share >= 1:
                return f"{share:.1f}%"
            if share >= 0.005:
                return f"{share:.2f}%"
            return "<0.01%"  # present but tiny -- never round a real row down to 0.00%

        # --- section 1: the two bars + the colour key. Below ~34 usable cells five
        # segments stop being legible (several are slivers), so the section drops out
        # rather than drawing five ambiguous cells.
        chart: list[str] = []
        if inner >= 34:
            for caption, index, total, fmt in (
                ("share of tokens sent", 1, econ.total_tokens, lambda v: human_tokens(int(v))),
                ("share of dollars billed", 2, econ.total_cost, money),
            ):
                if chart:
                    chart.append("")  # a blank row between the bars, so they read as two
                figure = fmt(total)
                chart.append(caption + " " * max(1, inner - len(caption) - len(figure)) + figure)
                chart.append(
                    self._token_stack_line([(r[0], r[index], r[3]) for r in rows], total, inner)
                )
            chart.append("")
            chart.extend(self._token_legend_lines(rows, inner))

        # --- section 2: the numbers behind the bars
        type_w = max(max((len(label) for label, *_ in rows), default=4), len("TOTAL"))
        cost_w = max(8, *(len(money(c)) for _, _, c, _ in rows), len(money(econ.total_cost)) + 1)
        # The 2-cell marker gutter every boxed table opens with (see _model_table): the
        # bars above are a chart and start flush, but the numbers below them are a table
        # and line up with every other one on the pane.
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

    def _top_sessions_box(
        self, workflows: list[Workflow], scope_cost: float, width: int
    ) -> list[str]:
        # The Overview's "Top Sessions" preview as a ruled box, matching the Top Models
        # table above it: Cost · Share · Tokens · Subs · Title. A top-N slice, so no
        # TOTAL row (it would sum only the shown few, not the scope).
        rows = self.top_sessions(workflows)
        if not rows:
            return self._ruled_box("# Top Sessions", "no sessions in range", [], None, [], width)
        inner = max(1, width - self.BOX_CHROME - 2)  # -2: the shared marker gutter
        # Size Cost/Subs to their widest value so a 6-figure cost ($100,000.00 is 11
        # cells, not 10) or a 5-digit subagent count doesn't overflow a fixed field and
        # shove every following column one cell right of its header.
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
        # The Overview's "Top Projects" as a ruled box, matching Top Sessions/Top Models:
        # Cost · Share · Tokens · Sess · Project. Cost-ranked (never the Projects-tab
        # sort/filter -- this is an at-a-glance leaderboard), a top-N slice so no TOTAL row.
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
        # Fold each project to its (directory, cost, tokens, sessions) once, so the column
        # widths and the rows read the same sums.
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
        # Size Cost/Sess to their widest value (see _top_sessions_box) so a 6-figure
        # project cost or a 4-digit session count doesn't overflow and shift the columns.
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
        # Top Months is the year's headline breakdown -- the level you drill into next.
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
        # A day touches few models, so the full model table lives here in the
        # Overview rather than in its own (near-empty) tab.
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
        # The same Top Sessions box every other scope's Overview closes with, rather than
        # this scope's own hand-rolled spelling of it -- one leaderboard, one layout.
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
        # The note sits in the Session block, wrapped to the pane and hanging-indented
        # under its label like every other field there. It's above the Money card on purpose:
        # it's the one line on this screen that says what the money was *for*.
        note = self.note_for(workflow.id)
        if not note:
            return []
        # Wrapped by display cells, not codepoints: a note with CJK or an emoji wrapped
        # by len() overflows the pane, and the pane then clips the overflow away -- the
        # note would lose text on screen that it still has on disk.
        wrapped = wrap_cells(note, max(20, width - 12)) or [note]
        return [f"Note:     {wrapped[0]}"] + [f"          {line}" for line in wrapped[1:]]

    def _money_overview(self, workflow: Workflow, width: int) -> list[str]:
        # The Overview's Money card (a _sectioned_box): one box that carries the cost
        # split, the shape stats folded in (so there is no separate "# Shape" block to
        # crowd the pane), a root-vs-subagents proportion bar -- the TUI's pie stand-in --
        # and, when a `w` target is armed, the what-if comparison as accent-highlighted
        # rows below a rule. Both sides of the what-if are list rates (whatif_session_totals),
        # so the recorded-cost rows above and the comparison rows below never quote the same
        # number for different things by accident.
        root, total = workflow.root_cost, workflow.total_cost
        sub = total - root
        # A summary card, not a full-width table: capped so a wide pane doesn't strand the
        # values a hundred columns from their labels (the model table below fills the pane
        # with real columns; this one is label/value and reads best compact).
        width = min(width, 76)
        inner = max(10, width - 4)

        def kv(label: str, value: str) -> str:
            # A left label and a right-aligned value, filling the box's inner width.
            return f"{label}{value:>{max(1, inner - display_width(label))}}"

        money_rows = []
        # The root-vs-subagents "pie" -- a two-glyph proportion bar (filled = root,
        # light = subagents), one color but readable by glyph. Only when the split is
        # real: a solo session (no subagents) or a $0 subscription session in normal mode
        # has nothing to divide, so the bar would be a solid or undefined block.
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
        # An armed `w` target answers for THIS session right here -- including a solo one,
        # which has no subagent tree for the Subagents tab to show. The ★ marks the rows
        # line_attr paints in the accent (the "highlight").
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

    # A summary card is capped rather than filled: a label/value block stretched across a
    # wide pane strands its values a hundred columns from their labels. The Money card
    # already read this way; the stat blocks above it did not, which is what made them
    # look like loose text above a row of designed boxes.
    CARD_WIDTH = 76

    def _stat_card(
        self, title: str, rows: list[str], width: int, notes: list[str] = ()
    ) -> list[str]:
        # The identity/stat blocks that open every Overview -- "# Session", "# Monthly
        # Insight", "# Project Spend" -- as the same ruled card the Money box below them
        # wears. They are label/value pairs, not columns, so they carry NO header row and
        # no marker gutter: _sectioned_box gives a titled box whose whole body is data.
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
        # A share of one session's spend, with BOTH ends guarded -- which formatting.pct
        # only does at the bottom. An icicle prints the parts beside the whole, so
        # "root kept 100%" above five visible subagent segments contradicts itself on
        # its own screen: a near-total reads ">99%" and only an exact whole reads 100%.
        # And a sliver is common (65 segments in the corpus are under half a percent, and
        # they are exactly the ones a "biggest …" line might name), so the bottom floors
        # at "<1%" rather than printing a segment that exists as "0%".
        #
        # Half-up on purpose: Python rounds .5 to even and JS's Math.round rounds it up,
        # so an exact 12.5% would read "12%" in the TUI and "13%" on the page.
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
        # Key labels, clipped to the key's column budget -- and re-separated after the
        # clip. App._flame_labels guarantees the FULL names are unique; shortening can
        # undo that (two long agent names sharing their first 21 characters), and two
        # identical key LINES collide in _token_runs, which is keyed by line text: the
        # second silently overwrites the first's colour runs and both swatches paint in
        # the second segment's colour.
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
        # One row of labels under the band: each segment's text written at that segment's
        # own starting column, in that segment's own colour, so the reader maps label to
        # slice by POSITION rather than by matching a swatch in a key. A label wider than
        # its segment is dropped rather than shifted -- a name sitting over the wrong
        # slice is worse than no name, and the key picks those up.
        #
        # One cell of air is reserved before the next segment so two labels never run
        # together into a single unreadable word; the last segment can use its full
        # width, there being nothing after it to collide with.
        #
        # Returns the line and the INDEXES it managed to label, so the caller can give a
        # key to exactly the segments position could not speak for.
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
        # "Where the money went": the session as a spend hierarchy -- one bar for the
        # whole, the band beneath it split into the root's own work and every subagent,
        # width = dollars. The tree TABLE below ranks the nodes; this says what share
        # each one took, which is the question a delegation setup is actually judged on
        # ("did routing move the work off the expensive model, or did the root keep it?").
        #
        # Widths are App.session_flame's, i.e. the table's own Cost column, so the chart
        # and the table can never quote different figures for the same node.
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

        # --- the headline: the chart's finding as a sentence. It survives a pane too
        # narrow for the bands, because a share you can read beats a bar you can't.
        parts = [f"root kept {self._flame_pct(flame.self_share)} ({fmt(own)})"]
        if kids:
            parts.append(
                f"{len(kids)} subagent{'s' if len(kids) != 1 else ''} "
                f"split {fmt(sum(s.value for s in kids))}"
            )
            if len(kids) > 1:
                # The bare agent here too: the sentence points at one segment, so the
                # handle that tells five "code-reviewer" runs apart is noise in it.
                parts.append(
                    f"biggest {shorten(kids[0].agent, 22)} {self._flame_pct(kids[0].share)}"
                )
        else:
            parts = [f"root kept all {approx}{fmt(flame.total)} — no subagent recorded a share"]
        head = [" · ".join(parts)]

        # --- the band, and the names UNDER it. Writing a name into the fill made the
        # chart harder to read, not easier: the text fought the colour it was punched
        # through, and it could only ever appear on the segments wide enough not to need
        # it. Below the band each name sits at its own segment's column, in its own
        # segment's colour, so position does the pointing and the fill stays a fill.
        # The pane has to hold a cell per segment as well as the legibility floor: a
        # session that delegated more times than the band has columns cannot be
        # partitioned at all, and the sentence above says more than a row of identical
        # single cells would.
        chart: list[str] = []
        named: list[int] = []
        if inner >= max(self._FLAME_MIN_INNER, len(flame.segments)):
            caption = "session · width = " + ("dollars" if dollars else "tokens")
            # One model for the whole tree (85 of 135 real sessions) says itself once
            # here rather than repeating under every segment -- which is exactly what
            # leaves the other 50 the room to name theirs per segment.
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
            # A second positioned row for the models, and only when the segments disagree
            # about them -- a uniform tree said it once in the caption, and repeating one
            # model under every segment would spend a row to say nothing. Each row
            # degrades on its own, so a segment too thin for its model still gets a name.
            if not flame.one_model:
                models, _ = self._flame_label_line(flame.segments, widths, lambda s: s.model)
                if models:
                    chart.append(models)
            # The key only carries what position could not: a segment too thin to hold
            # its own name. When every segment is named below, there is nothing left for
            # a key to say and it disappears.
            rest = [s for i, s in enumerate(flame.segments) if i not in set(named)]
            if rest:
                chart.append("")
                legend = rest[: self._FLAME_LEGEND_MAX]
                # The key has a whole line to wrap in, so it carries the model that the
                # thin segments below the band had no room for -- otherwise the models
                # row would answer only for the wide slices.
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
            # The stores record a node's depth but not its parent, so a nested execution
            # cannot be drawn under the one it ran below. It joins the band as a sibling
            # (marked "↳") rather than inventing a nesting -- see SessionFlame.
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
        # The chart rides ABOVE the tree table on both variants: it answers "what share"
        # where the table answers "which node, how much", and it reads recorded/estimated
        # spend either way, so an armed `w` target leaves it alone. It is passed IN rather
        # than prepended after, because the sort-header registration below is keyed by
        # absolute line index -- a prefix bolted on afterwards would silently move the
        # clickable header off the header row.
        head = self._flamegraph_box(workflow, width)
        totals = self.whatif_session_totals(workflow)
        if self.whatif_model and totals:
            # With a what-if target the root (depth 0) joins the table. Normally it's
            # excluded because it isn't an execution *under* the session -- but the
            # question here ("what if the expensive model had done the delegated work
            # too?") is about the whole tree, and the root is the model the delegation
            # was made from. Without a target the table is exactly what it always was.
            # A session with no per-model rows has no computable baseline (totals is
            # None), so it keeps the ordinary table rather than quoting half a
            # comparison.
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
        # The header is line 1 of the box, offset by however many lines the flamegraph
        # above it took -- the sort zones are keyed by ABSOLUTE line index, so this must
        # be derived from the prefix, never written as a constant.
        self._line_sort_headers[len(head) + self.BOX_HEADER_LINE] = (
            self.SUBAGENT_SORT_COLUMNS,
            "subagent",
        )
        return head + box

    @staticmethod
    def signed_pct(part: float, whole: float, sign: str) -> str:
        # A share with its direction glued on -- except when there is no share to sign:
        # pct() answers "-" for a zero denominator (undefined), and "+-" is not a
        # percentage. An undefined change stays bare.
        share = pct(abs(part), whole)
        return share if share == "-" else f"{sign}{share}"

    def detail_whatif_summary(self, workflow: Workflow) -> list[str]:
        # The armed target's effect on THIS session, in three figures. It lives on the
        # Overview because the Subagents tab can't answer for a session that delegated
        # nothing: a solo session has no tree to table, and "routing saved $X" would be
        # crediting a routing decision that was never made. So the summary stays neutral
        # -- the two list-price totals and the change between them -- and the Subagents
        # tab keeps the routing narrative for the sessions that actually routed.
        # Both figures come from App.whatif_session_totals (per-MODEL rows, both sides at
        # list rates), so the two views cannot drift into quoting different numbers.
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
        # The what-if payoff table -- the feature's ONE visible effect, scoped to this
        # session: the whole tree (root + every subagent), each node's cost beside what
        # that node's tokens would have cost had `target` produced them. Nothing outside
        # this table and the Overview summary moves; "$" keeps owning every app-wide
        # figure (an app-wide reprice would leave "$" nothing to toggle).
        #
        # Two columns, not three. The per-node What-if is exact (one model, one rate
        # card, that node's own tokens). A per-node *baseline* is not: workflow_nodes
        # labels a node with its single dominant model, so pricing its whole token split
        # at that one label is wrong for every node that switched model mid-flight -- so
        # there is no per-node Δ, because there is no honest per-node figure to subtract
        # from. The exact comparison exists only at session level, where the per-model
        # rows split the tokens properly (App.whatif_session_totals), and that is what
        # the TOTAL line reports -- both sides at list rates, the only apples-to-apples
        # basis. The Cost column stays what it is everywhere else: recorded spend,
        # "$"-estimated where nothing was recorded -- which is why it does NOT add up to
        # the TOTAL, and says so.
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
        # No boxed TOTAL row here, unlike every other multi-row table: this tab's total is
        # the session-level footer below, and its two figures deliberately do NOT equal
        # the columns above them (the Cost column is recorded spend, $0 wherever a
        # subscription recorded none). A column sum ruled into the box would sit two lines
        # above a different TOTAL and read as an arithmetic bug.
        lines = prefix + self._ruled_box(
            f"# Session Tree · what-if {target}", header, body, None, [], width
        )
        self._line_sort_headers[len(prefix) + self.BOX_HEADER_LINE] = (
            self.SUBAGENT_SORT_COLUMNS,
            "subagent",
        )
        actual, total = totals
        # Signed from the TARGET's point of view: what running all of it on the target
        # would have saved (or cost extra) against the models that actually ran it.
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
        # Normally the What-if column sums to the counterfactual (same tokens, same rate).
        # It won't when a session's node rollup disagrees with its message-level totals --
        # rare, and not this feature's doing, but an unexplained mismatch on screen reads
        # as a bug, so name it only on the sessions where it is actually true.
        column = sum(wi for _row, wi in priced)
        if abs(column - total) > 0.01:
            # Which way it drifts is not fixed -- a node rollup can overshoot the message
            # totals as easily as undershoot them -- so say the direction, don't assume it.
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
        # The web twin uses the same balanced-binary rule: split weights nearest
        # half along the current long edge, then recurse. Integer cuts make every
        # returned rectangle paintable in terminal cells.
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

    # The narrowest column that can still carry a tool name plus its inset -- below it a
    # tile is a stripe, and the exact table is the honest place for it.
    _TOOL_TILE_MIN = 12

    @staticmethod
    def _heat_position(value: float, lo: float, hi: float, levels: int) -> int:
        # A 0..levels-1 bucket for `value`'s LOGARITHMIC position in [lo, hi] -- the same
        # reasoning as _price_heat_level, for the same reason: per-call rates span orders
        # of magnitude (a sub-cent Read beside a $0.60 WebFetch), and a linear ramp
        # collapses everything but the most expensive tool into one indistinguishable
        # band. A degenerate range reads coolest rather than falsely hot.
        if not (hi > lo > 0) or value <= lo:
            return 0
        frac = (math.log(value) - math.log(lo)) / (math.log(hi) - math.log(lo))
        return max(0, min(levels - 1, round(frac * (levels - 1))))

    def _tool_treemap_box(
        self, bucket: dict[str, dict], width: int, max_height: int | None = None
    ) -> list[str]:
        # Area follows the table's live Cost column. A wholly-$0 subscription session
        # has no spend geometry, so it falls back to attributed tokens until `$` is on.
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
        # The graph is a summary, not a table blocker: at the supported 80x20
        # minimum the first exact tool row must still fit in the initial viewport.
        height = max(3, min(5, inner // 14))
        if max_height is not None:
            height = min(height, max_height)
        if height < 3:
            self._tool_tree_runs = {}
            return []

        # How many tiles the pane can actually SPEAK for. A box this wide and this short
        # partitions into full-height columns, so a tile's width is its share of `inner`
        # -- and a 3-cell column carries no name, no figure and no story, just a stripe.
        # A row of those at the right edge is most of what made the old chart read as big
        # and empty. So the tail folds into "Other" until every remaining tile can hold at
        # least its own label; the long tail is read in the exact table below, which is
        # where a long tail belongs.
        def fold(keep: int) -> list[tuple[str, float]]:
            head, tail = ranked[:keep], ranked[keep:]
            if not tail:
                return list(head)
            calls["Other"] = sum(calls.get(name, 0) for name, _ in tail)
            out = head + [("Other", sum(value for _, value in tail))]
            out.sort(key=lambda row: (-row[1], row[0].lower()))
            return out

        # Only the TAIL folds -- the tiles that individually cannot hold a label. Asking
        # instead that every tile in the folded set clear the floor makes one small tool
        # drag away everything ranked below it: on real data (18 tools, an 884-cell pane)
        # that rule left three tiles, which is a bar chart with extra steps.
        grand = sum(value for _, value in ranked)
        keep = 0
        while keep < min(8, len(ranked)):
            if ranked[keep][1] / grand * inner < self._TOOL_TILE_MIN:
                break
            keep += 1
        ranked_all, ranked = ranked, fold(max(1, keep))

        # Shade is the PER-CALL rate, deliberately not the area's own measure: area
        # already says what a tool cost in total, so colouring by the same number spends
        # the second channel saying it twice. $/call is the one thing the table below
        # cannot be read off at a glance, and it splits the two findings that look
        # identical in a Cost column -- "expensive because it ran 200 times" (a big, cool
        # tile: a workflow question) from "expensive every single time" (a small, hot
        # one: a context question). It needs a call count for EVERY drawn tile to be a
        # scale at all, so a bucket without them falls back to the area's measure, where
        # the fill is at worst redundant rather than arbitrary.
        # The SCALE comes off the full ranking, not the drawn tiles: whether per-call
        # rates exist and vary is a property of the data, and pinning the range to it
        # keeps a tool the same colour when a resize folds a neighbour away. A folded
        # "Other" carries the blended rate of what it swallowed, which sits inside that
        # range by construction.
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
            # money() floors at the cent, but a per-call rate usually lives below one and
            # the whole point of the figure is telling $0.0004 from $0.006 -- rendering
            # both as "<$0.01" would erase exactly the distinction the shade is drawing.
            return "<$0.0001/call" if rate < 0.0001 else f"${rate:.4f}".rstrip("0") + "/call"

        for name, value, x, y, w, h in rects:
            # A one-cell panel gutter separates adjacent heat fills -- but only where
            # there is actually a neighbour to separate from. A tile on the chart's own
            # right or bottom edge keeps its last cell, because the gutter there abuts
            # the frame, not another fill: at five rows a blanked bottom edge was a
            # quarter of the chart spent on nothing. Runt rectangles keep their single
            # cell; the exact table below always carries the label.
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

            # Name, then the area's own figure, then the shade's -- each on its own row
            # and each dropping independently, so a tile too short for the rate still
            # names itself. No vertical centring any more: the box is five rows at most
            # now, and the row it used to spend on air is the row the rate reads from.
            inset = 1 if tw >= 4 else 0
            room = tw - inset * 2
            if room >= 4 and th >= 2:
                put(y, x + inset, shorten(name, room), room)
                metric = money(value) if dollars else human_tokens(int(value))
                stat = f"{metric} · {pct(value, total)}"
                if len(stat) <= room:
                    put(y + 1, x + inset, stat, room)
                # Every figure here is gated on FITTING, never clipped: `shorten` marks a
                # cut name with an ellipsis, but a clipped money value is silently a
                # different number ("$0.02/call" losing its tail reads as "$0.0"), so a
                # rate with no room is omitted and the table below answers instead.
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

        # The finding, as a sentence -- the flamegraph's headline, for the same reason:
        # it is the part that survives a pane too narrow to read tiles off, and it is
        # what a passive chart otherwise makes you derive yourself. It reads the FULL
        # ranking, not the drawn tiles, so the tool the fold swallowed into "Other" can
        # still be named -- which matters most exactly when it is the hot one, since a
        # tool that is pricey per call is usually small by total and folds first.
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
        # One line when it fits, its clauses stacked when it doesn't -- never clipped.
        # _sectioned_box shortens what overflows, and a headline ending in "…" loses the
        # figure it exists to deliver; a second row is the cheaper price.
        joined = " · ".join(headline)
        boxed = self._sectioned_box(
            f"# Tool-attributed spend · {total_label}",
            [[joined] if len(joined) <= inner else headline, [caption, *chart]],
            width,
            notes,
        )
        # Where the chart landed inside the box, DERIVED rather than counted off the
        # prologue: the box is title + groups + rules + bottom + notes, and a literal
        # index silently shifts every tile's colour a row the moment a group grows a
        # line -- which the headline just did. Content rows carry the frame's two-cell
        # gutter, so the columns shift with them.
        chart_at = len(boxed) - len(notes) - 1 - len(chart)
        self._tool_tree_runs = {
            chart_at + row: [(col + 2, length, level) for col, length, level in runs]
            for row, runs in row_runs.items()
        }
        return boxed + [""]

    def detail_tools(
        self, workflow: Workflow, width: int, treemap_height: int | None = None
    ) -> list[str]:
        # Which tools (and MCP servers) the LLM calls cost the most. Each row is the
        # tokens/cost of the assistant steps that invoked a tool, split evenly when a
        # step called several -- so this is "tokens spent in turns that used this
        # tool", not the tool's own output size. The "$" view reprices $0
        # (subscription) rows at list price, like every other panel.
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
        # Each turn's cost as the tab shows it: recorded spend, or -- under "$" -- a
        # wholly-unpriced ($0) turn's tokens at list rates, long-TTL writes included.
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

    def detail_turn_drill(self, workflow: Workflow, width: int) -> list[str]:
        """One prompt, drilled into: its full text, its totals, and the turns it took.

        Where the per-turn detail went when the tab became a table -- a prompt that ran
        217 turns is one row out there and its own view in here, rather than 217 lines
        shoved between two rows of the table you were reading. Esc steps back out, like
        every other drill in the app.
        """
        rows = self.session_turn_rows(workflow.id)
        if not rows:
            return []
        costs = self.turn_costs(rows)
        groups = self.turn_group_rows(rows, costs)
        i = self.app.active_turn_drill
        if not isinstance(i, int) or not 0 <= i < len(groups):
            return []
        g = groups[i]
        # The table's click map and selected line belong to the TABLE. Left standing they
        # keep answering inside the drill, where draw_detail still lays a "turnline"
        # region over every line: a click on drilled text re-drilled whatever prompt used
        # to occupy that row, and the stale cursor line put the selection highlight on an
        # unrelated line of prompt text.
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
        # "↳ " marks a subagent turn, as the page does; a main-thread turn keeps whatever
        # label its backend gave it. Forcing "-" there threw a real one away -- OpenCode
        # names its main agent ("build" on 1,574 turns of a real corpus), and the page was
        # already showing it, so the two frontends disagreed about a visible cell.
        agent_w = min(
            12,
            max(5, max((len(_turn_agent(rows[i])) for i in g["indices"]), default=5)),
        )
        inner = max(1, width - self.BOX_CHROME - 2)
        mw = max(12, min(30, inner - (idx_w + agent_w + 14 + 6 + 9 + 9 + 6)))
        header = (
            f"  {'#':>{idx_w}} {'Time':<14} {pad('Model', mw)} {pad('Agent', agent_w)} "
            f"{'Cached':>6} {'Tokens':>9} {'Cost':>9}"
        )
        body = []
        for i in g["indices"]:
            r = rows[i]
            sh = cached_share(r)
            body.append(
                f"  {i + 1:>{idx_w}} {(r.get('time') or '--')[5:19]:<14} "
                f"{pad(shorten(r['model_name'], mw), mw)} "
                f"{pad(shorten(_turn_agent(r), agent_w), agent_w)} "
                f"{('-' if sh is None else f'{sh * 100:.0f}%'):>6} "
                f"{human_tokens(r['tokens_total']):>9} {money(costs[i]):>9}"
            )
        totals_row = None
        if len(body) > 1:
            totals_row = (
                f"  {'':>{idx_w}} {pad('TOTAL', 14)} {pad('', mw)} {pad('', agent_w)} "
                f"{'':>6} {human_tokens(g['tokens']):>9} {money(g['cost']):>9}"
            )
        lines += self._ruled_box(f"# Turns of prompt {n}", header, body, totals_row, [], width)
        lines += ["", f"· {self._key('main', 'back')} back to the prompts."]
        return lines

    @staticmethod
    def turn_group_rows(rows, costs):
        """The Turns table's rows: one per RUN of consecutive turns sharing a prompt_id.

        Returned as a LIST, and identified downstream by ORDINAL, because a prompt_id is
        not unique: a backend without explicit ids groups by the prompt TEXT (CsvStore
        does), so asking "fix the bug" twice in one session yields A, B, A. Keyed by id,
        the two A runs collapsed into one row worth both their costs while
        App.turn_groups still counted three -- the cursor's last ordinal addressed a row
        that was never drawn, and Enter opened the merged first run.

        Each aggregate carries what the row prints plus the turn indices behind it, which
        the markers (▼ compaction, ❄ expiry) and the drilled view both need.

        `cached` is the share of context the group's FIRST main-thread turn served from
        cache -- not an average over its turns. Every later turn in a prompt is warm by
        construction (the one before it just wrote the cache), so averaging drags every
        row toward 100% and buries the only moment that could have missed: measured on a
        real session, the prompt that followed an 8h44m expiry read 5% of its context
        back on the turn that mattered and averaged to 76%, directly under a ❄ marker
        saying it had re-bought the lot. A prompt with 114 turns hid the same thing at
        97%. The question this column answers -- did this prompt have to buy its context
        again -- is decided when the prompt starts.
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
                        "_first": None,  # the group's first main-thread turn
                    }
                )
            g = groups[-1]
            g["turns"] += 1
            g["tokens"] += int(r.get("tokens_total") or 0)
            g["cost"] += cost
            g["indices"].append(i)
            # Subagents run in their OWN context windows, so they neither answer for the
            # main thread's cache nor stand in for it: a prompt whose turns were all
            # subagent work (an interrupt lands like this) reports no share at all rather
            # than the subagent's.
            if not r.get("depth") and g["_first"] is None:
                g["_first"] = r
        for g in groups:
            g["cached"] = cached_share(g["_first"]) if g["_first"] is not None else None
        return groups

    def detail_turns(self, workflow: Workflow, width: int) -> list[str]:
        # How this session's cost accrued, in the order you spent it: one row per PROMPT,
        # each with the turns it took, how much of its context came from cache, its
        # tokens, its cost and the running total. Chronological, never cost-sorted -- the
        # point of the tab is WHEN the money went. The individual turns behind a row live
        # in the popup (Enter / a click), which is what lets every row carry real columns:
        # they used to be hidden inside a per-prompt expansion, so the only view that
        # listed them was one keystroke away from the view you were reading, and opening a
        # 40-turn prompt buried the table. Wholly-unpriced ($0) turns reprice at list
        # price under "$", like every other panel.
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
            self.app.turn_drill = None  # the prompt went away under us (reload)
        costs = self.turn_costs(rows)
        total = sum(costs)
        # One entry per prompt, in the order the prompts ran. Consecutive turns sharing a
        # prompt_id are one group -- the same split App.turn_groups makes, so the cursor
        # ordinal lines up with these rows.
        groups = self.turn_group_rows(rows, costs)

        # Compactions and cache expiries are the two things on this tab that are NOT
        # prompts: the window was cleared, or the cache died, between two turns. Both are
        # gated by the SAME opt-in the Context tab is (supports_context_curve) -- reading
        # a row's cache split as one request's prompt is exactly what a cumulative-delta
        # backend (Codex) and the synthetic CSV/JSONL sessions cannot support, and two
        # tabs disagreeing about one session is what that shared gate exists to prevent.
        #
        # Of the expiry causes only "waited" is drawn: it is the one the reader can do
        # something about, while "invalidated" (a changed tool set, an added image) is
        # both the most common and the least actionable, and a marker on every one of
        # those would be noise that teaches you to skip the marker you wanted.
        curve = self.session_supports_context_curve(workflow.id)
        comps = context_compactions(rows) if curve else {}
        late = {m.index: m for m in cache_misses(rows) if m.cause == "waited"} if curve else {}
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
        # ONE ROW PER PROMPT -- the thing you actually sent. Every row carries its own
        # numbers and the header is always drawn, because the columns are the point of
        # the tab; the per-turn rows live in the popup (Enter / a click), where a prompt
        # that ran 40 turns costs a keystroke instead of 40 lines of the pane. This tab
        # used to fold to prompt headers with the columns hidden INSIDE an expansion,
        # which put every number one keystroke away from the only view that listed them.
        idx_w = max(2, len(str(len(groups))))
        time_w = 11  # "MM-DD HH:MM" -- a prompt is a moment, its turns carry the seconds
        turns_w, cached_w, tok_w, cost_w = 5, 6, 8, 9
        # Optional cells, budgeted against the pane like session_columns does rather than
        # left to overflow and be clipped at paint -- a column the frame eats is worse
        # than one deliberately dropped, because it takes the prompt text with it. The
        # bar goes first (it restates the Cost cell), Cumulative second; the prompt keeps
        # at least PROMPT_MIN, since a prompt list is read by its prompts.
        PROMPT_MIN = 20
        inner = max(1, width - self.BOX_CHROME)  # the ruled box's gutters
        base = idx_w + time_w + turns_w + cached_w + tok_w + cost_w + 8
        cum_w = 14 if inner - base - 14 >= PROMPT_MIN else 0
        bar_w = 8 if inner - base - cum_w - 8 >= PROMPT_MIN + 12 else 0
        fixed = base + cum_w + bar_w + (1 if cum_w else 0) + (1 if bar_w else 0)
        peak = max((g["cost"] for g in groups), default=0.0)
        pw = max(PROMPT_MIN, inner - fixed)  # the prompt text takes whatever is left

        header = (
            f"  {'#':>{idx_w}} {'Time':<{time_w}} {'Prompt':<{pw}} {'Turns':>{turns_w}} "
            f"{'Cached':>{cached_w}} {'Tokens':>{tok_w}} {'Cost':>{cost_w}}"
            + (f" {'':<{bar_w}}" if bar_w else "")
            + (f" {'Cumulative':>{cum_w}}" if cum_w else "")
        )
        cum = 0.0
        body: list[str] = []
        cursor_rows: list[int] = []  # body-relative index of each prompt row
        for n, g in enumerate(groups, start=1):
            cum += g["cost"]
            # Marker rows first: they describe what happened BEFORE this prompt ran. They
            # ride INSIDE the box, between the rows they sit between -- they are events in
            # the same chronology, and line_attr reaches past the gutter to keep their
            # amber/red.
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
            share = g["cached"]
            cached = "-" if share is None else f"{share * 100:.0f}%"
            title = " ".join((g["title"] or "").split()) or "(no preceding prompt)"
            cumlabel = f"{money(cum)} · {pct(cum, total)}"
            cursor_rows.append(len(body))
            body.append(
                f"  {n:>{idx_w}} {g['time'][5:16]:<{time_w}} {pad(shorten(title, pw), pw)} "
                f"{g['turns']:>{turns_w}} {cached:>{cached_w}} "
                f"{human_tokens(g['tokens']):>{tok_w}} {money(g['cost']):>{cost_w}}"
                + (f" {cost_bar(g['cost'], peak, bar_w)}" if bar_w else "")
                + (f" {cumlabel:>{cum_w}}" if cum_w else "")
            )
        totals_row = None
        if len(groups) > 1:
            # Turns and tokens summed; Cost too, which the last Cumulative cell already
            # equals -- the row is here because every multi-row table has one, and reading
            # the sum off the bottom beats reading it off the last row of a running total.
            # Cached is a per-prompt ratio, not a quantity, so it stays blank.
            totals_row = (
                f"  {'':>{idx_w}} {pad('TOTAL', time_w)} {'':<{pw}} "
                f"{sum(g['turns'] for g in groups):>{turns_w}} {'':>{cached_w}} "
                f"{human_tokens(sum(g['tokens'] for g in groups)):>{tok_w}} "
                f"{money(total):>{cost_w}}"
            )
        lines = self._ruled_box(head, header, body, totals_row, [], width)
        # The click/cursor maps are ABSOLUTE line indices, so they rebase onto wherever
        # the box actually put its first body row -- derived from the box, never counted
        # off its prologue.
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
        if comps:
            notes.append(
                "· ▼ the context window was cleared before that turn — the Context tab charts it."
            )
        if late:
            notes.append(
                "· ❄ the prompt cache expired while the session sat idle, so that prompt paid "
                "again for context it already had — a faster follow-up would have cost less."
            )
        # WRAPPED, unlike every earlier version of this tab: these run past 140 characters
        # and the paint clips rather than wraps, so on a real pane they ended mid-sentence
        # ("...the context it was miss"). Continuations indent under the "· ".
        lines.append("")
        for note in notes:
            # Wrapped to width MINUS the continuation indent, not to width: adding the
            # two spaces afterwards pushed continuations two cells past the pane, so at
            # an 80-column terminal the ❄ note was still clipped by the very fix meant
            # to stop it being clipped.
            wrapped = textwrap.wrap(note, max(20, width - 2)) or [note]
            lines.append(wrapped[0])
            lines += ["  " + piece for piece in wrapped[1:]]
        return lines

    # The Context tab's chart geometry: enough rows for the curve's shape without
    # eating the pane, and a right-aligned y-axis gutter ("681.7k┤").
    _CTX_CHART_ROWS = 9
    _CTX_GUTTER = 8

    # Marker value in _ctx_line_heat for the amber ▼ compaction rows (heat levels
    # are >= 0); draw_detail maps levels to color pairs at paint time, keeping
    # detail_context plain-text like every other drawing method.
    _CTX_MARK = -1

    @staticmethod
    def _ctx_heat_level(value: float, window: int) -> int:
        # The green→red price-heat ramp, repurposed: how full the window is at this
        # height. Fixed pairs (PRICE_HEAT_BASE_PAIR..), so no per-frame inits.
        frac = value / window if window > 0 else 0.0
        return max(0, min(PRICE_HEAT_LEVELS - 1, int(frac * PRICE_HEAT_LEVELS)))

    @staticmethod
    def _turn_dt(row: dict) -> datetime | None:
        # A turn row's localtime string ("YYYY-MM-DD HH:MM:SS") as a naive datetime,
        # for the Context graph's wall-clock span. None when a backend's row carries no
        # (or a malformed) time -- the caller then drops the time enrichments.
        # Caveat: the string is local and tz-naive, so a span across a DST change is off
        # by the offset (e.g. an hour short over a fall-back). It's display-only (never a
        # money total) and human_duration clamps a negative to 0s, so the worst case is a
        # cosmetically wrong duration on the rare session that straddles the switch --
        # accepted rather than thread a UTC timestamp through every backend's timeline.
        try:
            return datetime.strptime((row.get("time") or "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @staticmethod
    def _ctx_clock(row: dict, multiday: bool) -> str:
        # "HH:MM" for a same-day session, "MM-DD HH:MM" once it spans days so the
        # left/right axis clocks and the ▼ times stay unambiguous.
        t = row.get("time") or ""
        return t[5:16] if multiday else t[11:16]

    def detail_context(self, workflow: Workflow, width: int) -> list[str]:
        # What the session's context window did over time -- the *measured* side is
        # exact (every main-thread turn's recorded prompt = input + cacheRead +
        # cacheWrite IS the live context size at that step), drawn as a heat-shaded
        # area chart with ▼ compaction markers; the *estimated* side (what filled
        # it) is the per-category composition tree the content-carrying backends
        # opt into, chars/4 like zaly's own /context. The system prompt and tool
        # schemas exist only in the live request -- no harness logs them -- so they
        # can only ever appear here as the measured turn-1 baseline.
        self._ctx_line_heat: dict[int, int] = {}
        rows = self.session_turn_rows(workflow.id)
        main = [r for r in rows if not r.get("depth")]
        pts = [(r, context_size(r)) for r in main]
        pts = [(r, v) for r, v in pts if v > 0]
        if not pts:
            return ["# Context", "No per-turn context usage recorded for this session."]
        vals = [v for _r, v in pts]
        n = len(vals)
        model = pts[-1][0]["model_name"]  # the chart scales to the live (last) window
        window = model_context_window(model)
        final, peakv, start = vals[-1], max(vals), vals[0]
        peak_i = vals.index(peakv)
        peak_at = peak_i + 1
        # A session can switch models mid-way: the peak % is honest to the window
        # the peak turn actually ran in, and mixed windows get a "!" caveat --
        # the heat rows can't re-scale per column (one attr per line), so the
        # header declares which window the chart uses.
        peak_window = model_context_window(pts[peak_i][0]["model_name"])
        windows = {model_context_window(r["model_name"]) for r, _v in pts}
        # The same rule the Turns tab marks with (util.context_compactions), applied to
        # this tab's own main-thread series so the marker positions stay indices into
        # `pts`/`vals` -- what the chart's columns and the ▼ lines below it are numbered by.
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
        # The peak line describes the value the chart's top row draws -- give it
        # that height's heat color (against its own turn's window) so the stats
        # and the chart read as one scale, like the end line above.
        self._ctx_line_heat[len(lines) - 1] = self._ctx_heat_level(peakv, peak_window)
        # Money + wall-clock: what the session cost and how it evolved in real time,
        # so the context curve isn't read in a vacuum -- spend and pace live nowhere
        # else on this tab. Both come straight off the turn rows / the session total.
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
            self._ctx_line_heat[len(lines) - 1] = self._CTX_MARK  # ▼ amber, one grammar
        if len(windows) > 1:
            lines.append(
                "! this session switched between models with different windows — the "
                f"chart and heat scale to the last one ({human_tokens(window)})"
            )
        lines.append("")

        # --- the growth curve: one column per turn (bucketed max when the session
        # outgrows the pane), eighth-block resolution, rows heat-shaded by window
        # fullness so the chart itself says how close to the limit it ran.
        gut, chart_h = self._CTX_GUTTER, self._CTX_CHART_ROWS
        plot_w = max(10, width - gut - 1)
        rep = max(1, min(4, plot_w // n))  # widen the columns when turns are few
        cols = min(plot_w, n * rep)
        ymax = float(peakv)

        def bucket(c: int) -> int:  # peak-preserving: a bucket shows its max turn
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
        # The x-axis carries turn indices *and* the start/end clock, so the chart's
        # left and right edges are pinned to real time -- when there's room for both.
        sc, ec = self._ctx_clock(pts[0][0], multiday), self._ctx_clock(pts[-1][0], multiday)
        xl = f"turn 1 · {sc}" if sc else "turn 1"
        xr = f"{ec} · turn {n}" if ec else str(n)
        if len(xl) + len(xr) + 1 > cols:  # clocks don't fit this narrow -> bare indices
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
            self._ctx_line_heat[len(lines) - 1] = self._CTX_MARK  # same amber as the ▼ row
        if len(comps) > 4:
            lines.append(f"  ▼ … and {len(comps) - 4} more")
            self._ctx_line_heat[len(lines) - 1] = self._CTX_MARK

        # --- what filled it (estimated), for backends whose logs carry content.
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
            # Kind names flex (MCP tools are long); the category column tracks them
            # (+3: its shallower indent) so the count/token columns stay aligned.
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

        # Two footnote lines, no more -- the rows explain themselves (the fixed-
        # overhead line carries its own label, a ▼ line shows the before → after).
        # The full story lives in docs/keys.md.
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
        self, stdscr: curses.window, scr_h: int, scr_w: int, title: str, lines: list
    ) -> tuple[int, int, int, int]:
        # A small centered popup box floating over the current view (cleared interior so
        # the view doesn't bleed through). `lines` is a list of (text, attr); the caller
        # styles each row (header tint, A_REVERSE for a selected entry). Sized to content.
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
        self.box(stdscr, y, x, h, w, title, active=True)
        field = w - 4
        for offset, (text, attr) in enumerate(content[: h - 4]):
            self.write(stdscr, y + 2 + offset, x + 2, pad(shorten(text, field), field), attr)
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
        _idx, start, shown = self._trend_cursor_window(len(all_rows), height - 3)
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
        _idx, start, shown = self._trend_cursor_window(len(all_rows), height - 4)
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
        notes = []
        if not self.show_api_prices and any(
            float(it["cost"]) == 0 and int(it["tokens"]) for _, it in rows
        ):
            notes = [
                "",
                f"{self._key('trends', 'api_prices')} prices subscription/credit "
                "usage at API list rates",
            ]
        lines = self._ruled_box("# Spend by provider", header, body, totals_row, notes, width)
        self._mark_trend_sort_header(columns)
        self._trend_rows_at = (self._ruled_body_start or 0, len(rows), start)
        return lines

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
            limit=max(1, height - 4),
            selectable=True,
            sort_tab="Harnesses",
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
    def _group_widths(rows: list, col: str, width: int) -> tuple[int, int]:
        # Size the name column to its HEADER too, not just the data: with every name
        # shorter than the label ("Harness", "Machine"), the header's own field overflowed
        # and shifted Cost/Share/Tokens/Sess right of the numbers they label. Short
        # hostnames make that the default in a fleet view. _model_table guards the same way.
        namew = min(
            max([len(s) for s, _ in rows] + [len(col) + Renderer._SORT_ARROW_W]),
            max(10, width - Renderer._GROUP_FIXED - 3),
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
        name: str, it, marker: str, namew: int, barw: int, peak: float, total: float
    ) -> str:
        bar = "█" * max(0, round((float(it["cost"]) / peak) * barw))
        return (
            f"{marker} {shorten(name, namew):{namew}}  {bar:<{barw}} "
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
    ) -> list[str]:
        # The shared ranked-spend table behind source_table/machine_table: a name column,
        # a cost bar, then Cost/Share/Tokens/Sess, in the same ruled box every other table
        # wears. `noun` is the box title's word, `col` the name-column header. Selectable
        # rows carry the Trends cursor + Enter drill. The rows themselves come from the
        # builders above, which the zoom picker paints too. `sort_tab` (the Trends frame
        # only) arrows the active column and makes the header clickable.
        title = f"# Spend by {noun}"
        if not all_rows:
            namew, barw = self._group_widths([], col, max(1, width - self.BOX_CHROME))
            return self._ruled_box(
                title, self._group_header(col, namew, barw, sort_tab), [], None, [], width
            ) + ["", "No sessions in the active range."]
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
        inner = max(1, width - self.BOX_CHROME)
        namew, barw = self._group_widths(rows, col, inner)
        body = [self._group_row(name, it, " ", namew, barw, peak, total_cost) for name, it in rows]
        total = None
        if len(rows) > 1:
            # The TOTAL row every multi-row table closes with. Counts and tokens summed;
            # Share stays blank (it is definitionally 100%) and so does the bar, which
            # measures rows against the peak, not against the sum.
            tcost = sum(float(it["cost"]) for _, it in rows)
            ttok = sum(int(it["tokens"]) for _, it in rows)
            tses = sum(int(it["sessions"]) for _, it in rows)
            total = (
                f"  {pad('TOTAL', namew)}  {'':{barw}} {money(tcost):>11} {'':>5} "
                f"{human_tokens(ttok):>9} {tses:>7}"
            )
        notes = []
        if not self.show_api_prices and any(
            float(it["cost"]) == 0 and int(it["tokens"]) for _, it in rows
        ):
            notes = [
                "",
                f"{self._key('trends', 'api_prices')} prices subscription/credit "
                "usage at API list rates",
            ]
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
            limit=max(1, height - 4),
            selectable=True,
            sort_tab="Machines",
        )

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
        inner = max(1, width - self.BOX_CHROME)
        header = f"  {'Started':<10} {'Cost':>9} {'Tokens':>8}  {self.src_col()}{'Title'}"
        idx = max(0, min(self.app.trend_drill_index, len(rows) - 1))
        self.app.trend_drill_index = idx
        fit = max(1, height - 4 - self.PICKER_CHROME)  # the box's own four lines
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
