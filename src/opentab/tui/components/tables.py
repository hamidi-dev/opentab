"""Pure table text and scrolling-picker geometry."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from opentab.formatting import display_width, pad, short_path, shorten
from opentab.tui.components.boxes import BOX_CHROME, box_row, box_rule, box_top

PICKER_CHROME = 4
SESSION_TITLE_MIN = 24
SESSION_PROJECT_MAX = 20


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
