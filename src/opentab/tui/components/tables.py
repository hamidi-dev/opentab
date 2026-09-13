"""Pure table text and scrolling-picker geometry."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from opentab.formatting import display_width, human_tokens, money, pad, pct, short_path, shorten
from opentab.tui.components.boxes import BOX_CHROME, box_row, box_rule, box_top, ruled_box

PICKER_CHROME = 4
SESSION_TITLE_MIN = 24
SESSION_PROJECT_MAX = 20
GROUP_FIXED = 40
SORT_ARROW_WIDTH = 2


@dataclass(frozen=True)
class SessionHeadings:
    date: str
    duration: str
    cost: str
    tokens: str
    subagents: str
    project: str
    title: str
    cost_width: int
    token_width: int
    source_column: str = ""
    machine_column: str = ""


@dataclass(frozen=True)
class SessionRow:
    date: str
    duration: str
    cost: str
    tokens: str
    subagents: int
    model_count: int
    source_column: str
    machine_column: str
    project: str
    marks: str
    ignored: str
    title: str


def session_header_text(
    headings: SessionHeadings,
    models: bool,
    project_width: int,
    duration: bool = True,
) -> str:
    header = f"  {headings.date:<10} "
    if duration:
        header += f"{headings.duration:>8} "
    header += (
        f"{headings.cost:>{headings.cost_width}} "
        f"{headings.tokens:>{headings.token_width}} "
        f"{headings.subagents:>11} "
    )
    if models:
        header += f"{'Models':>6}  "
    header += headings.source_column
    header += headings.machine_column
    if project_width:
        header += f"{headings.project:<{project_width}}  "
    return header + headings.title


def session_columns(
    projects: Sequence[str],
    width: int,
    span_projects: bool,
    model_scope: bool,
    headings: SessionHeadings,
) -> tuple[bool, int, bool]:
    project_width = 0
    if span_projects:
        longest = max((display_width(project) for project in projects), default=0)
        project_width = max(len(headings.project), min(SESSION_PROJECT_MAX, longest))
    for models, candidate_width, duration in (
        (True, project_width, True),
        (False, project_width, True),
        (False, 0, True),
        (False, 0, False),
    ):
        if model_scope:
            models = False
        prefix = len(session_header_text(headings, models, candidate_width, duration)) - len(
            headings.title
        )
        if width - prefix >= SESSION_TITLE_MIN:
            return models, candidate_width, duration
    return False, 0, False


def session_row_text(
    row: SessionRow,
    marker: str,
    models: bool,
    project_width: int,
    cost_width: int,
    token_width: int,
    duration: bool = True,
) -> str:
    text = f"{marker} {row.date:<10} "
    if duration:
        text += f"{row.duration:>8} "
    text += f"{row.cost:>{cost_width}} " f"{row.tokens:>{token_width}} " f"{row.subagents:>11} "
    if models:
        text += f"{row.model_count:>6}  "
    text += row.source_column
    text += row.machine_column
    if project_width:
        text += f"{pad(shorten(row.project, project_width), project_width)}  "
    return f"{text}{row.marks}{row.ignored}{row.title}"


@dataclass(frozen=True)
class ProjectHeadings:
    project: str
    cost: str
    tokens: str
    sessions: str
    subagents: str


@dataclass(frozen=True)
class ProjectRow:
    name: str
    cost: str
    tokens: str
    sessions: int
    subagents: int
    ignored: bool = False


@dataclass(frozen=True)
class ProjectTableText:
    header: str
    body: tuple[str, ...]
    total: str | None


@dataclass(frozen=True)
class GroupTableLayout:
    lines: tuple[str, ...]
    header_line: int
    body_start: int | None
    window_start: int
    window_count: int
    cursor: int


def group_widths(
    rows: Sequence[tuple[str, object]],
    column: str,
    width: int,
    display=shorten,
) -> tuple[int, int]:
    """Size the shared grouped-spend name and bar columns."""
    cap = max(10, width - GROUP_FIXED - 3)
    name_width = min(
        max(
            [display_width(display(name, cap)) for name, _item in rows]
            + [len(column) + SORT_ARROW_WIDTH]
        ),
        cap,
    )
    return name_width, max(3, min(20, width - name_width - GROUP_FIXED))


def group_header(
    column: str,
    name_width: int,
    bar_width: int,
    headings: Mapping[str, str] | None = None,
) -> str:
    labels = {
        "name": column,
        "cost": "Cost",
        "tokens": "Tokens",
        "count": "Sess",
    }
    labels.update(headings or {})
    return (
        f"  {labels['name']:<{name_width}}  {'':{bar_width}} {labels['cost']:>11} "
        f"{'Share':>5} {labels['tokens']:>9} {labels['count']:>7}"
    )


def group_row(
    name: str,
    item: Mapping[str, float | int],
    marker: str,
    name_width: int,
    bar_width: int,
    peak: float,
    total: float,
    display=shorten,
) -> str:
    bar = "█" * max(0, round((float(item["cost"]) / peak) * bar_width))
    return (
        f"{marker} {display(name, name_width):{name_width}}  {bar:<{bar_width}} "
        f"{money(float(item['cost'])):>11} {pct(float(item['cost']), total):>5} "
        f"{human_tokens(int(item['tokens'])):>9} {int(item['sessions']):>7}"
    )


def group_row_budget(height: int, count: int, notes: int = 0) -> int:
    return max(1, height - BOX_CHROME - (2 if count > 1 else 0) - notes)


def group_window(count: int, cursor: int, fit: int) -> tuple[int, int, int]:
    index = max(0, min(cursor, count - 1))
    fit = max(1, fit)
    start = max(0, min(index - fit // 2, count - fit))
    return index, start, min(fit, count - start)


def group_unpriced_notes(
    rows: Sequence[tuple[str, Mapping[str, float | int]]],
    show_api_prices: bool,
    price_key: str,
) -> tuple[str, ...]:
    if show_api_prices or not any(
        float(item["cost"]) == 0 and int(item["tokens"]) for _name, item in rows
    ):
        return ()
    return ("", f"{price_key} prices subscription/credit usage at API list rates")


def group_table_layout(
    rows: Sequence[tuple[str, Mapping[str, float | int]]],
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
) -> GroupTableLayout:
    """Build the shared grouped-spend table from already ordered rows."""
    title = f"# Spend by {noun}"
    if not rows:
        name_width, bar_width = group_widths((), column, max(1, width - BOX_CHROME), display)
        box = ruled_box(
            title,
            group_header(column, name_width, bar_width, headings),
            (),
            None,
            (),
            width,
            glyphs,
        )
        return GroupTableLayout(
            (*box.lines, "", "No sessions in the active range."),
            box.header_line or 0,
            box.body_start,
            0,
            0,
            0,
        )

    notes = group_unpriced_notes(rows, show_api_prices, price_key)
    if height is not None:
        limit = group_row_budget(height, len(rows), len(notes))
    if selectable and limit is not None:
        selected, start, count = group_window(len(rows), cursor, limit)
        visible = rows[start : start + count]
    else:
        selected = max(0, min(cursor, len(rows) - 1))
        start = 0
        visible = rows if limit is None else rows[:limit]
    scope = rows if selectable and limit is not None else visible
    total_cost = sum(float(item["cost"]) for _name, item in scope)
    peak = max((float(item["cost"]) for _name, item in scope), default=0.0) or 1.0
    inner = max(1, width - BOX_CHROME)
    name_width, bar_width = group_widths(visible, column, inner, display)
    body = tuple(
        group_row(name, item, " ", name_width, bar_width, peak, total_cost, display)
        for name, item in visible
    )
    total_row = None
    if len(scope) > 1:
        total_row = (
            f"  {pad('TOTAL', name_width)}  {'':{bar_width}} {money(total_cost):>11} {'':>5} "
            f"{human_tokens(sum(int(item['tokens']) for _name, item in scope)):>9} "
            f"{sum(int(item['sessions']) for _name, item in scope):>7}"
        )
    box = ruled_box(
        title,
        group_header(column, name_width, bar_width, headings),
        body,
        total_row,
        notes,
        width,
        glyphs,
    )
    return GroupTableLayout(
        box.lines,
        box.header_line or 0,
        box.body_start,
        start,
        len(visible),
        selected,
    )


def project_name_width(width: int) -> int:
    return max(8, width - 38)


def project_header_text(headings: ProjectHeadings, width: int) -> str:
    name_width = project_name_width(width)
    return (
        f"  {headings.project:{name_width}} "
        f"{headings.cost:>7} {headings.tokens:>6} "
        f"{headings.sessions:>7} {headings.subagents:>11}"
    )


def project_row_text(row: ProjectRow, marker: str, width: int) -> str:
    name_width = project_name_width(width)
    name = short_path(row.name, max(1, name_width - (2 if row.ignored else 0)))
    if row.ignored:
        name = f"× {name}"
    return (
        f"{marker} {pad(name, name_width)} "
        f"{row.cost:>7} {row.tokens:>6} "
        f"{row.sessions:>3} ses {row.subagents:>6} subs"
    )


def project_total_text(row: ProjectRow, width: int) -> str:
    name_width = project_name_width(width)
    return (
        f"  {pad('TOTAL', name_width)} "
        f"{row.cost:>7} {row.tokens:>6} "
        f"{row.sessions:>3} ses {row.subagents:>6} subs"
    )


def project_table_text(
    rows: Sequence[ProjectRow],
    headings: ProjectHeadings,
    width: int,
    total: ProjectRow | None,
) -> ProjectTableText:
    return ProjectTableText(
        header=project_header_text(headings, width),
        body=tuple(project_row_text(row, " ", width) for row in rows) or ("No projects.",),
        total=project_total_text(total, width) if total is not None else None,
    )


@dataclass(frozen=True)
class PickerFrame:
    frame_x: int
    content_x: int
    outer_width: int
    inner_width: int
    top_y: int
    header_y: int
    rule_y: int
    body_y: int
    bottom_y: int
    top: str
    header: str
    rule: str
    bottom: str


@dataclass(frozen=True)
class PickerRow:
    y: int
    left_x: int
    content_x: int
    right_x: int
    left: str
    content: str
    right: str
    selected: bool


def picker_box_width(width: int) -> int:
    return max(1, width - 4 - BOX_CHROME)


def picker_window(total: int, selected: int, height: int) -> tuple[int, int, int]:
    visible = max(1, height - 5 - PICKER_CHROME)
    index = max(0, min(selected, total - 1))
    start = max(0, min(index - visible // 2, max(0, total - visible)))
    return index, start, min(visible, total)


def picker_frame(
    cy: int,
    x: int,
    width: int,
    title: str,
    header: str,
    row_count: int,
    glyphs: Mapping[str, str],
) -> PickerFrame:
    outer = max(5, width - 4)
    inner = outer - BOX_CHROME
    frame_x = x + 2
    content_x = frame_x + 2
    return PickerFrame(
        frame_x=frame_x,
        content_x=content_x,
        outer_width=outer,
        inner_width=inner,
        top_y=cy,
        header_y=cy + 1,
        rule_y=cy + 2,
        body_y=cy + 3,
        bottom_y=cy + 3 + row_count,
        top=box_top(title, outer, glyphs),
        header=box_row(header, outer, glyphs),
        rule=box_rule(outer, glyphs),
        bottom=box_rule(outer, glyphs, "bl", "br"),
    )


def picker_row(
    y: int,
    x: int,
    content_x: int,
    inner_width: int,
    text: str,
    selected: bool,
    glyphs: Mapping[str, str],
) -> PickerRow:
    return PickerRow(
        y=y,
        left_x=x + 2,
        content_x=content_x,
        right_x=content_x + inner_width,
        left=f"{glyphs['v']} ",
        content=pad(shorten(text, inner_width), inner_width),
        right=f" {glyphs['v']}",
        selected=selected,
    )
