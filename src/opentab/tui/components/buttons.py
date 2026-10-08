"""Pure layout for a row of clickable key buttons, such as an overlay's bottom border.

A bar is one centered block: buttons with the dismissing action (Close, Cancel)
last, then passive status text (a scroll hint, a position).

An enabled button is raised by a thin shadow edge: a quarter-cell line down its right
side (`▎`) and an eighth-cell one along its bottom (`▔`, on the row below). Pressed,
or disabled, it lies flat with neither.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional, Union

from opentab.presentation.formatting import display_width
from opentab.tui.components.navigation import TextSpan


@dataclass(frozen=True)
class Button:
    key: str  # live keymap label; empty when the user unbound the action
    label: str
    action: str  # the keymap action a click presses
    context: str = ""  # the keymap context that owns the action
    enabled: bool = True  # a disabled button keeps its place, greyed and unclickable
    primary: bool = False  # the default choice, filled in the accent


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
    shadow: tuple[TextSpan, ...] = ()  # the row below the bar, same local cells


Pressed = Optional[tuple[str, str]]  # (context, action) of the button held down


def _drawable(items: Sequence[ButtonItem]) -> list[ButtonItem]:
    # Never offer a key the user removed; empty text is simply absent.
    return [item for item in items if (item.key if isinstance(item, Button) else item)]


def _place(items: Sequence[ButtonItem], x: int = 0, pressed: Pressed = None) -> ButtonBarLayout:
    # One gap cell around every item; padding inside a button belongs to its face, so
    # the button reads as one block. A button also owns the cell after its face, where
    # the shadow's right edge goes, so its width never depends on its state.
    spans: list[TextSpan] = []
    shadow: list[TextSpan] = []
    hits: list[ButtonHit] = []
    origin = x
    for item in items:
        spans.append(TextSpan(x, " ", "gap"))
        x += 1
        if not isinstance(item, Button):
            spans.append(TextSpan(x, item, "text"))
            x += display_width(item)
            continue
        on = item.enabled
        tone = "primary-" if item.primary else ""
        parts = [
            (f" {item.key}", f"{tone}key" if on else "off"),
            (f" {item.label} ", f"{tone}label" if on else "off"),
        ]
        face = sum(display_width(text) for text, _style in parts)
        cursor = x
        for text, style in parts:
            spans.append(TextSpan(cursor, text, style))
            cursor += display_width(text)
        if on and pressed != (item.context, item.action):
            spans.append(TextSpan(x + face, "▎", "cap"))
            shadow.append(TextSpan(x, "▔" * face, "shadow"))
        else:
            spans.append(TextSpan(x + face, " ", "gap"))
        if on:
            hits.append(ButtonHit(x, x + face - 1, item.context, item.action))
        x += face + 1
    if spans:
        spans.append(TextSpan(x, " ", "gap"))
        x += 1
    return ButtonBarLayout(tuple(spans), tuple(hits), x - origin, tuple(shadow))


def button_bar_layout(
    width: int,
    *,
    buttons: Sequence[Button] = (),
    status: Sequence[str] = (),
    pressed: Pressed = None,
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
    return _place(left + right, (width - _place(left + right).width) // 2, pressed)


def button_bar_width(*, buttons: Sequence[Button] = (), status: Sequence[str] = ()) -> int:
    """Cells the whole bar needs, for sizing the box that carries it."""
    return _place(_drawable(buttons) + _drawable(status)).width
