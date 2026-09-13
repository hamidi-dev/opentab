"""Pure geometry and content placement for centered terminal modals."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from opentab.presentation.formatting import display_width, pad, shorten

Style = TypeVar("Style")


@dataclass(frozen=True)
class StyledLine(Generic[Style]):
    text: str
    style: Style


@dataclass(frozen=True)
class PlacedLine(Generic[Style]):
    y: int
    x: int
    text: str
    style: Style


@dataclass(frozen=True)
class ModalLayout(Generic[Style]):
    y: int
    x: int
    height: int
    width: int
    title: str
    title_x: int
    field_width: int
    rows: tuple[PlacedLine[Style], ...]


def modal_layout(
    screen_height: int,
    screen_width: int,
    title: str,
    lines: Sequence[StyledLine[Style]],
    *,
    center_rows: bool = False,
) -> ModalLayout[Style]:
    """Size a centered modal and place its visible content rows."""
    content = tuple(StyledLine(str(line.text), line.style) for line in lines)
    inner_width = max(
        [display_width(title) + 2, 16] + [display_width(line.text) for line in content]
    )
    width = min(inner_width + 4, max(24, screen_width - 4))
    height = min(len(content) + 4, max(6, screen_height - 4))
    y = max(1, (screen_height - height) // 2)
    x = max(1, (screen_width - width) // 2)
    field_width = width - 4
    placed = []
    for offset, line in enumerate(content[: height - 4]):
        drawn = shorten(line.text, field_width)
        row_x = x + 2 + max(0, (field_width - display_width(drawn)) // 2) if center_rows else x + 2
        placed.append(
            PlacedLine(
                y + 2 + offset,
                row_x,
                drawn if center_rows else pad(drawn, field_width),
                line.style,
            )
        )
    title_text = f" {shorten(title, width - 6)} "
    title_x = x + max(2, (width - display_width(title_text)) // 2)
    return ModalLayout(
        y,
        x,
        height,
        width,
        title_text,
        title_x,
        field_width,
        tuple(placed),
    )
