"""Pure layouts for Trends charts, rankings, drills, and spend calendar."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Union

from opentab.presentation.formatting import human_tokens, money, pad, pct, short_path, shorten
from opentab.presentation.heatmap import calendar_cells, heat_band_label, heat_level
from opentab.tui.components.boxes import BOX_CHROME, ruled_box, sectioned_box
from opentab.tui.components.charts import bar_chart
from opentab.tui.components.tables import (
    group_row_budget,
    group_table_layout,
    group_unpriced_notes,
    group_window,
)
from opentab.tui.components.token_cards import TokenCard

RankItem = Mapping[str, Union[float, int]]
RankRow = tuple[str, RankItem]
SortColumns = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RowMap:
    line: int
    count: int
    start: int


@dataclass(frozen=True)
class HeaderMap:
    line: int
    columns: SortColumns
    target: str = "trend"


@dataclass(frozen=True)
class SemanticSpan:
    line: int
    column: int
    length: int
    role: str
    value: int = 0


@dataclass(frozen=True)
class CalendarGeometry:
    grid_y: int
    row_pitch: int
    grid_x: int
    column_pitch: int
    start_column: int
    shown_columns: int
    year: str
    grid_start: datetime


@dataclass(frozen=True)
class TrendsLayout:
    lines: tuple[str, ...]
    headers: tuple[HeaderMap, ...] = ()
    rows: RowMap | None = None
    bar_slots: tuple[tuple[int, int, str], ...] | None = None
    bar_click_rows: int = 0
    spans: tuple[SemanticSpan, ...] = ()
    calendar: CalendarGeometry | None = None


def _chart(title: str, pairs, width: int, height: int, *, keys=None, selected=None) -> TrendsLayout:
    chart = bar_chart(pairs, width, height - 2, keys=keys, selected=selected)
    return TrendsLayout(
        (title, "", *chart.lines),
        bar_slots=chart.slots,
        bar_click_rows=chart.click_rows,
    )


def daily_layout(
    month: str | None,
    data: Sequence[tuple[str, float]],
    months: Sequence[str],
    width: int,
    height: int,
    *,
    navigation_keys: str,
    selected: str | None = None,
) -> TrendsLayout:
    if month is None:
        return TrendsLayout(("# Daily spend", "", "No spend in the active range."))
    pairs = [(str(int(day[8:10])), value) for day, value in data]
    title = f"# Daily spend · {month}"
    if len(months) > 1:
        title += (
            f"   ({months.index(month) + 1}/{len(months)} — {navigation_keys} older/newer month)"
        )
    return _chart(
        title, pairs, width, height, keys=[day for day, _value in data], selected=selected
    )


def weekly_layout(
    monday: str | None,
    data: Sequence[tuple[str, float]],
    weeks: Sequence[str],
    width: int,
    height: int,
    *,
    navigation_keys: str,
    selected: str | None = None,
) -> TrendsLayout:
    if monday is None:
        return TrendsLayout(("# Weekly spend", "", "No spend in the active range."))
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    pairs = [(names[index], value) for index, (_day, value) in enumerate(data)]
    title = f"# Weekly spend · {monday} – {data[-1][0]}"
    if len(weeks) > 1:
        title += f"   ({weeks.index(monday) + 1}/{len(weeks)} — {navigation_keys} older/newer week)"
    return _chart(
        title, pairs, width, height, keys=[day for day, _value in data], selected=selected
    )


def monthly_layout(
    data: Sequence[tuple[str, float]], width: int, height: int, *, selected: str | None = None
) -> TrendsLayout:
    if not data:
        return TrendsLayout(("# Monthly spend", "", "No spend in the active range."))
    return _chart("# Monthly spend (cost per month)", data, width, height, selected=selected)


def ranked_row_budget(height: int, count: int, notes: int = 0) -> int:
    return group_row_budget(height, count, notes)


def cursor_window(count: int, cursor: int, fit: int) -> tuple[int, int, int]:
    return group_window(count, cursor, fit)


def unpriced_note(
    rows: Sequence[RankRow], show_api_prices: bool, price_key: str
) -> tuple[str, ...]:
    return group_unpriced_notes(rows, show_api_prices, price_key)


def model_ranking_layout(
    rows: Sequence[tuple[str, float]],
    width: int,
    height: int,
    cursor: int,
    headings: Mapping[str, str],
    glyphs: Mapping[str, str],
) -> TrendsLayout:
    if not rows:
        return TrendsLayout(("# Model spend", "", "No priced model spend in the active range."))
    total = sum(cost for _name, cost in rows)
    peak = max(cost for _name, cost in rows) or 1.0
    _index, start, shown = cursor_window(len(rows), cursor, ranked_row_budget(height, len(rows)))
    visible = rows[start : start + shown]
    inner = max(1, width - BOX_CHROME)
    name_width = min(
        max([len(name) for name, _cost in visible] + [len(headings["name"])]),
        max(12, inner - 26),
    )
    bar_width = max(3, min(24, inner - name_width - 22))
    body = tuple(
        f"  {pad(shorten(name, name_width), name_width)}  "
        f"{'█' * max(0, round(cost / peak * bar_width)):<{bar_width}} "
        f"{money(cost):>11} {pct(cost, total):>5}"
        for name, cost in visible
    )
    total_row = (
        f"  {pad('TOTAL', name_width)}  {'':{bar_width}} {money(total):>11} {'':>5}"
        if len(rows) > 1
        else None
    )
    box = ruled_box(
        "# Model spend (priced, in range)",
        f"  {headings['name']:{name_width}}  {'':{bar_width}} {headings['cost']:>11} {'Share':>5}",
        body,
        total_row,
        (),
        width,
        glyphs,
    )
    return TrendsLayout(
        box.lines,
        (HeaderMap(box.header_line or 0, (("name", "Model"), ("cost", "Cost"))),),
        RowMap(box.body_start or 0, len(visible), start),
    )


def provider_ranking_layout(
    rows: Sequence[RankRow],
    width: int,
    height: int,
    cursor: int,
    headings: Mapping[str, str],
    glyphs: Mapping[str, str],
    *,
    show_api_prices: bool,
    price_key: str,
) -> TrendsLayout:
    if not rows:
        return TrendsLayout(("# Spend by provider", "", "No model usage in the active range."))
    total_cost = sum(float(item["cost"]) for _name, item in rows)
    peak = max((float(item["cost"]) for _name, item in rows), default=0.0) or 1.0
    notes = unpriced_note(rows, show_api_prices, price_key)
    _index, start, shown = cursor_window(
        len(rows), cursor, ranked_row_budget(height, len(rows), len(notes))
    )
    visible = rows[start : start + shown]
    inner = max(1, width - BOX_CHROME)
    name_width = min(
        max([len(name) for name, _item in visible] + [len(headings["name"])]),
        max(10, inner - 44),
    )
    bar_width = max(3, min(20, inner - name_width - 40))
    header = (
        f"  {headings['name']:{name_width}}  {'':{bar_width}} {headings['cost']:>11} "
        f"{'Share':>5} {headings['tokens']:>9} {headings['count']:>7}"
    )
    body = tuple(
        f"  {pad(shorten(name, name_width), name_width)}  "
        f"{'█' * max(0, round(float(item['cost']) / peak * bar_width)):<{bar_width}} "
        f"{money(float(item['cost'])):>11} {pct(float(item['cost']), total_cost):>5} "
        f"{human_tokens(int(item['tokens'])):>9} {int(item['runs']):>7}"
        for name, item in visible
    )
    total_row = None
    if len(rows) > 1:
        total_row = (
            f"  {pad('TOTAL', name_width)}  {'':{bar_width}} {money(total_cost):>11} {'':>5} "
            f"{human_tokens(sum(int(item['tokens']) for _name, item in rows)):>9} "
            f"{sum(int(item['runs']) for _name, item in rows):>7}"
        )
    box = ruled_box("# Spend by provider", header, body, total_row, notes, width, glyphs)
    columns = (("name", "Provider"), ("cost", "Cost"), ("tokens", "Tokens"), ("count", "Msgs"))
    return TrendsLayout(
        box.lines,
        (HeaderMap(box.header_line or 0, columns),),
        RowMap(box.body_start or 0, len(visible), start),
    )


def group_ranking_layout(
    rows: Sequence[RankRow],
    width: int,
    noun: str,
    column: str,
    glyphs: Mapping[str, str],
    *,
    limit: int | None = None,
    cursor: int = 0,
    selectable: bool = False,
    height: int | None = None,
    headings: Mapping[str, str] | None = None,
    show_api_prices: bool,
    price_key: str,
    display: Callable[[str, int], str] = shorten,
) -> TrendsLayout:
    table = group_table_layout(
        rows,
        width,
        noun,
        column,
        glyphs,
        limit=limit,
        cursor=cursor,
        selectable=selectable,
        height=height,
        headings=headings,
        show_api_prices=show_api_prices,
        price_key=price_key,
        display=display,
    )
    columns = (("name", column), ("cost", "Cost"), ("tokens", "Tokens"), ("count", "Sess"))
    headers = (HeaderMap(table.header_line, columns if headings is not None else ()),)
    row_map = (
        RowMap(table.body_start or 0, table.window_count, table.window_start)
        if selectable and table.body_start is not None
        else None
    )
    return TrendsLayout(table.lines, headers, row_map)


@dataclass(frozen=True)
class DrillSession:
    started: str
    cost: float
    tokens: int
    source: str
    title: str


def drill_layout(
    kind: str,
    key: str,
    rows: Sequence[DrillSession],
    width: int,
    total_count: int,
    total_cost: float,
    total_tokens: int,
    start: int,
    source_heading: str,
    glyphs: Mapping[str, str],
) -> TrendsLayout:
    label = short_path(key, 44) if kind == "project" else key
    title = f"# Sessions · {label}"
    if not rows:
        return TrendsLayout((title, "", f"No sessions used {label} in the active range."))
    inner = max(1, width - BOX_CHROME)
    header = f"  {'Started':<10} {'Cost':>9} {'Tokens':>8}  {source_heading}Title"
    body = tuple(
        f"  {row.started:<10} {money(row.cost):>9} {human_tokens(row.tokens):>8}  "
        f"{row.source}{shorten(row.title, max(8, inner - 34))}"
        for row in rows
    )
    total_row = None
    if total_count > 1:
        total_row = (
            f"  {pad('TOTAL', 10)} {money(total_cost):>9} " f"{human_tokens(total_tokens):>8}  "
        )
    box = ruled_box(
        f"{title} · {total_count} session(s), most spend first",
        header,
        body,
        total_row,
        (),
        width,
        glyphs,
    )
    return TrendsLayout(box.lines, rows=RowMap(box.body_start or 0, len(rows), start))


def token_card_layout(card: TokenCard, width: int, glyphs: Mapping[str, str]) -> TrendsLayout:
    """Frame an already-priced economics card and preserve its semantic color spans."""
    groups = [[line.text for line in group] for group in card.groups]
    box = sectioned_box(card.title, groups, width, card.notes, glyphs)
    spans = []
    line_index = 1
    nonempty = [group for group in card.groups if group]
    for group_index, group in enumerate(nonempty):
        if group_index:
            line_index += 1
        for styled in group:
            for span in styled.spans:
                spans.append(
                    SemanticSpan(line_index, span.column + 2, span.length, "token", span.slot)
                )
            line_index += 1
    headers = ()
    if card.header:
        header_line = next((i for i, line in enumerate(box.lines) if card.header in line), None)
        if header_line is not None:
            headers = (HeaderMap(header_line, ()),)
    return TrendsLayout(box.lines, headers=headers, spans=tuple(spans))


def model_economics_layout(
    model: str,
    sessions: int,
    messages: int,
    tokens: int,
    list_cost: str,
    card: TokenCard | None,
    width: int,
    glyphs: Mapping[str, str],
) -> TrendsLayout:
    """Compose model scope facts with an already-priced token-economics card."""
    card_width = min(width, 76)
    scope = sectioned_box(
        "# Model scope",
        (
            (
                f"Model:      {shorten(model, max(20, card_width - 16))}",
                f"Sessions:   {sessions}",
                f"Messages:   {messages}",
                f"Tokens:     {human_tokens(tokens)}",
                f"List cost:  {list_cost}",
            ),
        ),
        card_width,
        (),
        glyphs,
    )
    economics = (
        token_card_layout(card, width, glyphs)
        if card is not None
        else TrendsLayout(
            ruled_box(
                "# Token economics",
                "no priceable usage here",
                (),
                None,
                (),
                width,
                glyphs,
            ).lines
        )
    )
    offset = len(scope.lines) + 1
    return TrendsLayout(
        (*scope.lines, "", *economics.lines),
        headers=tuple(
            HeaderMap(header.line + offset, header.columns, header.target)
            for header in economics.headers
        ),
        spans=tuple(
            SemanticSpan(
                span.line + offset,
                span.column,
                span.length,
                span.role,
                span.value,
            )
            for span in economics.spans
        ),
    )


def calendar_layout(
    year: str | None,
    year_index: int,
    year_count: int,
    by_date: Mapping[str, float],
    sessions_by_date: Mapping[str, int],
    levels: int,
    focused: bool,
    cursor: str | None,
    height: int,
    width: int,
    *,
    navigation_keys: str,
    select_key: str,
    arrows: str,
    price_key: str,
    show_api_prices: bool,
    heat_glyphs: Sequence[str],
) -> TrendsLayout:
    if year is None:
        text = "No spend in the active range."
        return TrendsLayout((text,), spans=(SemanticSpan(0, 0, len(text), "muted"),))
    if height < 13 or width < 24:
        text = "Not enough room for the calendar."
        return TrendsLayout((text,), spans=(SemanticSpan(0, 0, len(text), "muted"),))
    grid, months, column_count = calendar_cells(year, dict(by_date))
    peak = max(by_date.values(), default=0.0)
    total = sum(by_date.values())
    active = sum(1 for value in by_date.values() if value > 0)
    gutter, column_pitch = 4, 2
    max_columns = max(1, (width - gutter) // column_pitch)
    start_column = max(0, column_count - max_columns)
    shown_columns = column_count - start_column
    grid_width = shown_columns * column_pitch
    x_offset = max(0, (width - (gutter + grid_width)) // 2)
    grid_x = x_offset + gutter
    row_pitch = 2 if height >= 20 else 1
    grid_y = 3
    jan1 = datetime(int(year), 1, 1)
    grid_start = jan1 - timedelta(days=jan1.weekday())
    lines = [[" "] * width for _ in range(height)]
    spans: list[SemanticSpan] = []

    def put(line: int, column: int, text: str, role: str = "normal", value: int = 0) -> None:
        if column + len(text) > len(lines[line]):
            lines[line].extend(" " for _ in range(column + len(text) - len(lines[line])))
        for offset, char in enumerate(text):
            lines[line][column + offset] = char
        if text and role != "normal":
            spans.append(SemanticSpan(line, column, len(text), role, value))

    title = f"Spend calendar · {year}"
    if year_count > 1:
        title += f"   ({year_index + 1}/{year_count} — {navigation_keys} older/newer year)"
    put(0, max(0, (width - len(title)) // 2), title, "title")
    next_free_x = grid_x
    for column, abbreviation in months:
        shown = column - start_column
        month_x = grid_x + shown * column_pitch
        if (
            shown >= 0
            and month_x >= next_free_x
            and month_x + len(abbreviation) <= grid_x + grid_width
        ):
            put(2, month_x, abbreviation, "muted")
            next_free_x = month_x + len(abbreviation) + 1
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    for row in range(7):
        line = grid_y + row * row_pitch
        put(line, x_offset, weekdays[row], "muted")
        for column in range(shown_columns):
            cell = grid[row][start_column + column]
            if cell is None:
                continue
            level = heat_level(cell, peak, levels)
            put(line, grid_x + column * column_pitch, heat_glyphs[level], "heat", level)
    if cursor and cursor[:4] == year:
        date = datetime.strptime(cursor, "%Y-%m-%d")
        cursor_column = (date - grid_start).days // 7 - start_column
        if 0 <= cursor_column < shown_columns:
            line = grid_y + date.weekday() * row_pitch
            column = grid_x + cursor_column * column_pitch
            put(line, column - 1, "[", "cursor")
            put(line, column + 1, "]", "cursor")
    legend_y = grid_y + 6 * row_pitch + 2
    separator = "  " if levels <= 6 else " "
    legend: list[tuple[str, str, int]] = []
    if peak > 0:
        legend.append(("per day  ", "muted", 0))
        bounds = [math.expm1(math.log1p(peak) * index / levels) for index in range(levels + 1)]
        for level in range(levels + 1):
            legend.append((heat_glyphs[level], "heat", level))
            label = (
                f" $0{separator}"
                if level == 0
                else f" ≤{heat_band_label(bounds[level])}{separator}"
            )
            legend.append((label, "muted", 0))
    else:
        legend.append(("Less ", "muted", 0))
        legend.extend((heat_glyphs[level], "heat", level) for level in range(levels + 1))
        legend.append((" More", "muted", 0))
    legend_x = max(0, (width - sum(len(text) for text, _role, _value in legend)) // 2)
    for text, role, value in legend:
        put(legend_y, legend_x, text, role, value)
        legend_x += len(text)
    sessions = sum(sessions_by_date.values())
    if total > 0:
        peak_date = max(by_date, key=by_date.__getitem__)
        info = [
            (
                f"total {money(total)}   peak {money(peak)} on {peak_date}   {active} active days",
                "normal",
            )
        ]
    elif sessions:
        info = [(f"{sessions} sessions, no recorded spend this year", "normal")]
    else:
        info = [("no spend this year", "normal")]
    if focused and cursor:
        date = datetime.strptime(cursor, "%Y-%m-%d")
        day_sessions = sessions_by_date.get(cursor, 0)
        label = f"▸ {weekdays[date.weekday()]} {cursor}   "
        if day_sessions:
            noun = "session" if day_sessions == 1 else "sessions"
            info.append(
                (
                    f"{label}{money(by_date.get(cursor, 0.0))}   {day_sessions} {noun}   {select_key} opens",
                    "normal",
                )
            )
        else:
            info.append((f"{label}no sessions   move with {arrows}", "normal"))
    elif not focused:
        info.extend(
            (
                ("", "normal"),
                ("", "normal"),
                (f"Press {select_key} to navigate the calendar", "accent"),
            )
        )
    if total == 0 and sessions and not show_api_prices:
        info.append((f"{price_key} prices subscription/credit usage at API list rates", "normal"))
    for offset, (text, role) in enumerate(info):
        line = legend_y + 1 + offset
        if line >= height:
            break
        text = shorten(text, width)
        put(line, max(0, (width - len(text)) // 2), text, role)
    compact_lines = ["".join(line).rstrip() for line in lines]
    while compact_lines and not compact_lines[-1]:
        compact_lines.pop()
    text_lines = tuple(compact_lines)
    geometry = CalendarGeometry(
        grid_y, row_pitch, grid_x, column_pitch, start_column, shown_columns, year, grid_start
    )
    return TrendsLayout(text_lines, spans=tuple(spans), calendar=geometry)
