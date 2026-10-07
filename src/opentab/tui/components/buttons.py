"""Pure layout for a row of clickable key buttons, such as an overlay's bottom border.

A bar is one centered block: buttons with the dismissing action (Close, Cancel)
last, then passive status text (a scroll hint, a position).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Union

from opentab.presentation.formatting import display_width
from opentab.tui.components.navigation import TextSpan


@dataclass(frozen=True)
class Button:
    key: str  # live keymap label; empty when the user unbound the action
    label: str
    action: str  # the keymap action a click presses
    context: str = ""  # the keymap context that owns the action
    enabled: bool = True  # a disabled button keeps its place, greyed and unclickable


# A plain string is passive text: a position, a scroll hint.
ButtonItem = Union[Button, str]


@dataclass(frozen=True)
class ButtonHit:
    x0: int
    x1: int
    context: str
    action: str


@dataclass(frozen=True)
class ButtonBarLayout:
    spans: tuple[TextSpan, ...]
    hits: tuple[ButtonHit, ...]
    width: int


def _drawable(items: Sequence[ButtonItem]) -> list[ButtonItem]:
    # Never offer a key the user removed; empty text is simply absent.
    return [item for item in items if (item.key if isinstance(item, Button) else item)]


def _place(items: Sequence[ButtonItem], x: int = 0) -> ButtonBarLayout:
    # One gap cell around every item; padding inside a button belongs to its surface,
    # so the pill reads as one block.
    spans: list[TextSpan] = []
    hits: list[ButtonHit] = []
    origin = x
    for item in items:
        spans.append(TextSpan(x, " ", "gap"))
        x += 1
        if isinstance(item, Button):
            on = item.enabled
            parts = [
                (f" {item.key}", "key" if on else "off"),
                (f" {item.label} ", "label" if on else "off"),
            ]
        else:
            parts = [(item, "text")]
        start = x
        for text, style in parts:
            spans.append(TextSpan(x, text, style))
            x += display_width(text)
        if isinstance(item, Button) and item.enabled:
            hits.append(ButtonHit(start, x - 1, item.context, item.action))
    if spans:
        spans.append(TextSpan(x, " ", "gap"))
        x += 1
    return ButtonBarLayout(tuple(spans), tuple(hits), x - origin)


def button_bar_layout(
    width: int, *, buttons: Sequence[Button] = (), status: Sequence[str] = ()
) -> ButtonBarLayout:
    """Center the buttons, then the status text, as one block in local cells.

    Overflow sheds status from the front first, so the last item (a position) stays
    longest; then buttons from the front, so a trailing Close or Cancel outlives the
    rest. Nothing is ever half-drawn.
    """
    left, right = _drawable(buttons), _drawable(status)
    while _place(left + right).width > width:
        if right and (len(right) > 1 or not left):
            del right[0]
        elif left:
            del left[0]
        else:
            return ButtonBarLayout((), (), 0)
    return _place(left + right, (width - _place(left + right).width) // 2)


def button_bar_width(*, buttons: Sequence[Button] = (), status: Sequence[str] = ()) -> int:
    """Cells the whole bar needs, for sizing the box that carries it."""
    return _place(_drawable(buttons) + _drawable(status)).width
