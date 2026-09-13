"""Pure framed-box primitives and line layout metadata."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from opentab.presentation.formatting import display_width, pad, shorten

BOX_CHROME = 4

TABLE_GLYPHS = {
    "tl": "┌",
    "tr": "┐",
    "bl": "└",
    "br": "┘",
    "lt": "├",
    "rt": "┤",
    "h": "─",
    "v": "│",
}
TABLE_GLYPHS_ASCII = {
    "tl": "+",
    "tr": "+",
    "bl": "+",
    "br": "+",
    "lt": "+",
    "rt": "+",
    "h": "-",
    "v": "|",
}


@dataclass(frozen=True)
class BoxLayout:
    lines: tuple[str, ...]
    header_line: int | None = None
    body_start: int | None = None


def box_top(title: str, width: int, glyphs: Mapping[str, str]) -> str:
    width = max(5, width)
    heading = shorten(title[2:] if title.startswith("# ") else title, max(1, width - 6))
    prefix = f"{glyphs['tl']} {heading} "
    return prefix + glyphs["h"] * max(0, width - display_width(prefix) - 1) + glyphs["tr"]


def box_rule(
    width: int,
    glyphs: Mapping[str, str],
    left: str = "lt",
    right: str = "rt",
) -> str:
    return glyphs[left] + glyphs["h"] * max(0, max(5, width) - 2) + glyphs[right]


def box_row(text: str, width: int, glyphs: Mapping[str, str]) -> str:
    inner = max(5, width) - BOX_CHROME
    return f"{glyphs['v']} {pad(shorten(text, inner), inner)} {glyphs['v']}"


def ruled_box(
    title: str,
    header: str,
    body: Sequence[str],
    total: str | None,
    notes: Sequence[str],
    width: int,
    glyphs: Mapping[str, str],
) -> BoxLayout:
    lines = [box_top(title, width, glyphs), box_row(header, width, glyphs)]
    body_start = None
    if body:
        lines.append(box_rule(width, glyphs))
        body_start = len(lines)
        lines.extend(box_row(row, width, glyphs) for row in body)
        if total is not None:
            lines.append(box_rule(width, glyphs))
            lines.append(box_row(total, width, glyphs))
    lines.append(box_rule(width, glyphs, "bl", "br"))
    lines.extend(notes)
    return BoxLayout(tuple(lines), header_line=1, body_start=body_start)


def sectioned_box(
    title: str,
    groups: Sequence[Sequence[str]],
    width: int,
    notes: Sequence[str],
    glyphs: Mapping[str, str],
) -> BoxLayout:
    lines = [box_top(title, width, glyphs)]
    body_start = None
    for index, group in enumerate(group for group in groups if group):
        if index:
            lines.append(box_rule(width, glyphs))
        if body_start is None:
            body_start = len(lines)
        lines.extend(box_row(row, width, glyphs) for row in group)
    lines.append(box_rule(width, glyphs, "bl", "br"))
    lines.extend(notes)
    return BoxLayout(tuple(lines), body_start=body_start)
