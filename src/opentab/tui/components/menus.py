"""Pure row layouts and selection windows shared by terminal menus."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable, TypeVar

from opentab.tui.components.modal import StyledLine

NORMAL = "normal"
MUTED = "muted"
DIM = "dim"
SELECTED = "selected"
NOTICE = "notice"
SUBTLE = "subtle"

Entry = TypeVar("Entry")


@dataclass(frozen=True)
class MenuLayout:
    lines: tuple[StyledLine[str], ...]
    option_rows: tuple[tuple[int, int], ...]
    start: int = 0


def selected_index(selection: int, count: int) -> int:
    return selection % count if count else 0


def selection_window(count: int, selection: int, visible_count: int) -> tuple[int, int]:
    """Return a bounded window centered around the selected item."""
    visible_count = max(1, visible_count)
    index = selected_index(selection, count)
    start = 0
    if count > visible_count:
        start = min(max(0, index - visible_count // 2), count - visible_count)
    return start, min(count, start + visible_count)


def option_budget(available_height: int, fixed_rows: int, *, minimum: int = 1) -> int:
    """Rows left for options after a modal's fixed content is reserved."""
    return max(minimum, available_height - fixed_rows)


def radio_menu(
    intro: str,
    entries: Sequence[tuple[str, bool]],
    selection: int,
    *,
    current_suffix: str = "  (current)",
) -> MenuLayout:
    index = selected_index(selection, len(entries))
    lines = [StyledLine(intro, MUTED), StyledLine("", NORMAL)]
    option_rows = []
    for offset, (label, current) in enumerate(entries):
        marker = "●" if current else "○"
        suffix = current_suffix if current else ""
        lines.append(StyledLine(f" {marker}  {label}{suffix}", row_style(offset, index)))
        option_rows.append((len(lines) - 1, offset))
    return MenuLayout(tuple(lines), tuple(option_rows))


def check_menu(intro: str, entries: Sequence[tuple[str, bool]], selection: int) -> MenuLayout:
    index = selected_index(selection, len(entries))
    lines = [StyledLine(intro, MUTED), StyledLine("", NORMAL)]
    option_rows = []
    for offset, (label, checked) in enumerate(entries):
        box = "[x]" if checked else "[ ]"
        lines.append(StyledLine(f" {box}  {label}", row_style(offset, index)))
        option_rows.append((len(lines) - 1, offset))
    return MenuLayout(tuple(lines), tuple(option_rows))


def select_menu(
    heading: Sequence[StyledLine[str]],
    entries: Sequence[tuple[str, str]],
    selection: int,
    *,
    footer: Sequence[StyledLine[str]] = (),
) -> MenuLayout:
    index = selected_index(selection, len(entries))
    lines = list(heading)
    option_rows = []
    for offset, (prefix, label) in enumerate(entries):
        lines.append(StyledLine(f" {prefix}  {label}", row_style(offset, index)))
        option_rows.append((len(lines) - 1, offset))
    lines.extend(footer)
    return MenuLayout(tuple(lines), tuple(option_rows))


def windowed_radio_menu(
    intro: Sequence[StyledLine[str]],
    entries: Sequence[tuple[Entry, bool]],
    selection: int,
    visible_count: int,
    *,
    footer: Sequence[StyledLine[str]] = (),
    empty: StyledLine[str] | None = None,
    label_formatter: Callable[[Entry], str] = str,
    current_suffix: str = "  (current)",
) -> MenuLayout:
    index = selected_index(selection, len(entries))
    start, end = selection_window(len(entries), index, visible_count)
    lines = list(intro)
    option_rows = []
    if not entries and empty is not None:
        lines.append(empty)
    if start:
        lines.append(StyledLine(f"    ↑ {start} more", DIM))
    for offset in range(start, end):
        entry, current = entries[offset]
        label = label_formatter(entry)
        marker = "●" if current else "○"
        suffix = current_suffix if current else ""
        lines.append(StyledLine(f" {marker}  {label}{suffix}", row_style(offset, index)))
        option_rows.append((len(lines) - 1, offset))
    below = len(entries) - end
    if below:
        lines.append(StyledLine(f"    ↓ {below} more", DIM))
    lines.extend(footer)
    return MenuLayout(tuple(lines), tuple(option_rows), start)


def row_style(offset: int, selection: int) -> str:
    return SELECTED if offset == selection else NORMAL
