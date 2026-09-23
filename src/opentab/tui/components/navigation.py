"""Pure layouts for tabs, footer hints, and proportional scrollbars."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from opentab.presentation.formatting import display_width, shorten


@dataclass(frozen=True)
class TextSpan:
    x: int
    text: str
    style: str


@dataclass(frozen=True)
class TabHit:
    x0: int
    x1: int
    index: int


@dataclass(frozen=True)
class TabStripLayout:
    spans: tuple[TextSpan, ...]
    hits: tuple[TabHit, ...]
    rules: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class KeybarLayout:
    spans: tuple[TextSpan, ...]
    width: int


@dataclass(frozen=True)
class ViewportGeometry:
    total: int
    visible: int
    offset: int
    max_offset: int


@dataclass(frozen=True)
class PagerLayout:
    y: int
    x: int
    height: int
    width: int
    viewport: ViewportGeometry


@dataclass(frozen=True)
class ScrollbarRun:
    y: int
    length: int
    style: str


@dataclass(frozen=True)
class ScrollbarLayout:
    viewport: ViewportGeometry
    thumb_y: int
    thumb_height: int
    runs: tuple[ScrollbarRun, ...]


def tab_strip_layout(
    labels: Sequence[str],
    active_index: int,
    width: int,
    *,
    center: bool = False,
    rule: bool = False,
    disabled: Iterable[int] = (),
) -> TabStripLayout:
    """Lay out tab chips in local coordinates, including click and rule geometry."""
    if width <= 0 or not labels:
        return TabStripLayout((), (), ())

    active_index %= len(labels)
    disabled = frozenset(disabled)
    rendered = [
        f"[{label}]" if i == active_index else f" {label} " for i, label in enumerate(labels)
    ]
    separator = " " if rule else "  "
    total = sum(display_width(label) for label in rendered)
    total += display_width(separator) * (len(rendered) - 1)
    x = max(0, (width - total) // 2) if (center or rule) and total <= width else 0
    rules = []
    if rule and x >= 2:
        rules.append((0, x - 1))

    spans = []
    hits = []
    remaining = max(0, width - x)
    for index, label in enumerate(rendered):
        if index:
            text = shorten(separator, remaining)
            spans.append(TextSpan(x, text, "separator"))
            cells = display_width(text)
            x += cells
            remaining -= cells
        if remaining <= 0:
            break
        text = shorten(label, remaining)
        style = (
            "disabled" if index in disabled else "active" if index == active_index else "inactive"
        )
        spans.append(TextSpan(x, text, style))
        cells = display_width(text)
        if index not in disabled:
            hits.append(TabHit(x, x + cells - 1, index))
        x += cells
        remaining -= cells

    if rule and x + 2 <= width:
        rules.append((x + 1, width - x - 1))
    return TabStripLayout(tuple(spans), tuple(hits), tuple(rules))


def keybar_layout(parts, width: int, *, trailing=()) -> KeybarLayout:
    """Left-align whole hints, with a reserved right-hand action and quiet dividers.

    Parts contain semantic (text, style) segments. All fitting uses terminal cells;
    neither a shortcut nor the trailing action is ever partially rendered.
    """
    spans = []
    right = width - 1
    tail_width = sum(display_width(text) for text, _style in trailing)
    if trailing and tail_width <= width - 2:
        x = right - tail_width
        for text, style in trailing:
            spans.append(TextSpan(x, text, style))
            x += display_width(text)
        right -= tail_width + 2

    x = 1
    for segments in parts:
        cells = sum(display_width(text) for text, _style in segments)
        gap = 3 if x > 1 else 0
        if not cells:
            continue
        if x + gap + cells > right:
            break
        if gap:
            spans.append(TextSpan(x, " │ ", "separator"))
            x += gap
        for text, style in segments:
            spans.append(TextSpan(x, text, style))
            x += display_width(text)
    return KeybarLayout(
        tuple(sorted(spans, key=lambda span: span.x)),
        sum(display_width(span.text) for span in spans),
    )


def viewport_geometry(total: int, visible: int, offset: int) -> ViewportGeometry:
    """Clamp a document offset and expose the reusable viewport bounds."""
    total = max(0, total)
    visible = max(0, visible)
    max_offset = max(0, total - visible)
    return ViewportGeometry(total, visible, max(0, min(offset, max_offset)), max_offset)


def pager_layout(
    *,
    top: int,
    bottom: int,
    width: int,
    inner_width: int,
    horizontal_chrome: int,
    total_rows: int,
    scroll: int,
    content_sized: bool,
    min_height: int = 0,
    center_vertical: bool = True,
    vertical_chrome: int = 3,
) -> PagerLayout:
    """Place a horizontally centered pager and clamp its body viewport."""
    available_height = bottom - top
    box_width = inner_width + horizontal_chrome
    box_height = (
        min(available_height, max(min_height, total_rows + vertical_chrome))
        if content_sized
        else available_height
    )
    box_y = top + max(0, (available_height - box_height) // 2) if center_vertical else top
    visible = max(1, box_height - vertical_chrome)
    return PagerLayout(
        y=box_y,
        x=max(0, (width - box_width) // 2),
        height=box_height,
        width=box_width,
        viewport=viewport_geometry(total_rows, visible, scroll),
    )


def scrollbar_layout(total: int, visible: int, offset: int) -> ScrollbarLayout | None:
    """Return a proportional thumb and at most three constant-time paint runs."""
    viewport = viewport_geometry(total, visible, offset)
    if viewport.total <= viewport.visible or viewport.visible <= 0:
        return None

    max_thumb = max(1, viewport.visible - 1)
    proportional = (viewport.visible * viewport.visible + viewport.total - 1) // viewport.total
    thumb_height = min(max_thumb, max(min(2, viewport.visible), proportional))
    travel = viewport.visible - thumb_height
    thumb_y = (travel * viewport.offset + viewport.max_offset // 2) // viewport.max_offset
    runs = []
    if thumb_y:
        runs.append(ScrollbarRun(0, thumb_y, "track"))
    runs.append(ScrollbarRun(thumb_y, thumb_height, "thumb"))
    below = viewport.visible - thumb_y - thumb_height
    if below:
        runs.append(ScrollbarRun(thumb_y + thumb_height, below, "track"))
    return ScrollbarLayout(viewport, thumb_y, thumb_height, tuple(runs))


def scrollbar_thumb(total: int, visible: int, offset: int) -> tuple[int, int] | None:
    """Return thumb geometry for callers that do not need paint runs."""
    layout = scrollbar_layout(total, visible, offset)
    return None if layout is None else (layout.thumb_y, layout.thumb_height)
