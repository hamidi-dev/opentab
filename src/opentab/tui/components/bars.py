"""Pure stacked-bar and legend layouts with semantic color spans."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Callable

from opentab.presentation.heatmap import TOKEN_SERIES_GLYPHS


@dataclass(frozen=True)
class StyleSpan:
    column: int
    length: int
    slot: int


@dataclass(frozen=True)
class StyledLine:
    text: str
    spans: tuple[StyleSpan, ...] = ()


Segment = tuple[str, float, int]
LegendEntry = tuple[str, int]


def segment_glyph(slot: int, *, colored: bool) -> str:
    return "█" if colored else TOKEN_SERIES_GLYPHS[slot]


def stack_widths(rows: Iterable[Segment], total: float, cells: int) -> list[int]:
    """Allocate an exact-width stack while keeping positive segments visible."""
    rows = list(rows)
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


def stack_line(
    rows: Sequence[Segment],
    total: float,
    cells: int,
    *,
    colored: bool,
    labels: Sequence[str] | None = None,
    share_formatter: Callable[[float], str] | None = None,
) -> StyledLine:
    """Build one stacked band and return its color spans separately."""
    widths = stack_widths(rows, total, cells)
    spans = []
    text, column = "", 0
    for index, ((_label, value, slot), width) in enumerate(zip(rows, widths)):
        if width <= 0:
            continue
        glyph = segment_glyph(slot, colored=colored)
        share = ""
        if total > 0:
            share = (
                share_formatter(value / total)
                if share_formatter
                else f"{round(100.0 * value / total)}%"
            )
        body = glyph * width
        if colored:
            named = f"{labels[index]} {share}".strip() if labels else ""
            for candidate in (named, share):
                if candidate and len(candidate) + 2 <= width:
                    body = candidate.center(width, glyph)
                    break
        spans.append(StyleSpan(column, width, slot))
        text += body
        column += width
    return StyledLine(text, tuple(spans))


def legend_lines(
    rows: Sequence[LegendEntry], inner_width: int, *, colored: bool
) -> list[StyledLine]:
    """Wrap a color key without clipping entries or losing swatch spans."""
    lines = []
    spans = []
    text = ""
    for label, slot in rows:
        glyph = segment_glyph(slot, colored=colored)
        entry = f"{glyph} {label}"
        gap = "  " if text else ""
        if text and len(text) + len(gap) + len(entry) > inner_width:
            lines.append(StyledLine(text, tuple(spans)))
            text, spans, gap = "", [], ""
        spans.append(StyleSpan(len(text) + len(gap), 1, slot))
        text += gap + entry
    if text:
        lines.append(StyledLine(text, tuple(spans)))
    return lines


def positioned_label_line(
    labels: Sequence[tuple[str, int]], widths: Sequence[int]
) -> tuple[StyledLine, list[int]]:
    """Place labels under their own segments, dropping labels that do not fit."""
    text, spans, placed = "", [], []
    for index, ((label, slot), width) in enumerate(zip(labels, widths)):
        if width <= 0:
            continue
        room = width - 1 if index < len(widths) - 1 else width
        if not label or len(label) > room:
            continue
        column = sum(widths[:index])
        text += " " * (column - len(text)) + label
        spans.append(StyleSpan(column, len(label), slot))
        placed.append(index)
    return StyledLine(text, tuple(spans)), placed
