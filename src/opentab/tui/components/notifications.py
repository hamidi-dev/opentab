"""Pure notification card and history layouts."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

from opentab.presentation.formatting import clip, display_width, wrap_cells
from opentab.tui.components.navigation import pager_layout


@dataclass(frozen=True)
class Notification:
    text: str
    kind: str = "info"
    born: float = 0.0


@dataclass(frozen=True)
class NotificationStyle:
    sigil: str
    label: str


NOTIFICATION_STYLES: Mapping[str, NotificationStyle] = {
    "info": NotificationStyle("·", "Note"),
    "success": NotificationStyle("✓", "Done"),
    "warn": NotificationStyle("▲", "Heads up"),
    "error": NotificationStyle("✕", "Error"),
    "release": NotificationStyle("✦", "What's new"),
}


@dataclass(frozen=True)
class ToastCard:
    y: int
    x: int
    height: int
    width: int
    kind: str
    title: str
    lines: tuple[str, ...]
    padding: int
    shortcut: Optional[tuple[int, int, str]]


@dataclass(frozen=True)
class HistoryRow:
    text: str
    gutter: str
    kind: str


@dataclass(frozen=True)
class HistoryViewport:
    y: int
    x: int
    height: int
    width: int
    inner_width: int
    title: str
    rows: tuple[HistoryRow, ...]
    total_rows: int
    visible_rows: int
    scroll: int
    scroll_hint: str
    scroll_hint_x: int


def wrap_notice(text: str, width: int) -> list[str]:
    """Cell-wrap text without consuming whitespace at line boundaries."""
    width = max(1, width)
    rows = []
    for line in text.expandtabs(4).splitlines():
        while display_width(line) > width:
            part = clip(line, width)
            space = part.rfind(" ")
            if space > 0:
                part = part[: space + 1]
            rows.append(part)
            line = line[len(part) :]
        rows.append(line)
    return rows or [""]


def toast_cards(
    toasts: Sequence[Notification],
    *,
    height: int,
    width: int,
    release_version: str,
    release_key: str,
    fallback_sigil: Optional[str] = None,
    card_width: int = 46,
    max_lines: int = 4,
) -> tuple[ToastCard, ...]:
    """Lay out newest-first live cards in the top-right screen space."""
    max_width = min(card_width, width - 4)
    if max_width < 10:
        return ()

    max_lines = max(1, max_lines)
    cards = []
    y = 3
    text_width = max_width - 6
    for toast in reversed(toasts):
        style = NOTIFICATION_STYLES.get(toast.kind, NOTIFICATION_STYLES["info"])
        lines = wrap_notice(toast.text, text_width)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = clip(lines[-1], text_width - 1) + "…"

        is_release = toast.kind == "release"
        padding = 2 if is_release else 1
        card_height = len(lines) + padding * 2
        if y + card_height > height - 2:
            break

        sigil = fallback_sigil if fallback_sigil is not None else style.sigil
        label = f"NEW IN v{release_version}" if is_release else style.label
        shortcut = None
        if is_release and release_key:
            marker = f"Press {release_key}"
            for row, line in enumerate(lines):
                if line.startswith(marker):
                    shortcut = (row, display_width("Press "), release_key)
                    break
        cards.append(
            ToastCard(
                y=y,
                x=width - max_width - 2,
                height=card_height,
                width=max_width,
                kind=toast.kind,
                title=f" {sigil} {label} ",
                lines=tuple(lines),
                padding=padding,
                shortcut=shortcut,
            )
        )
        y += card_height + 1
    return tuple(cards)


def toast_age(seconds: float) -> str:
    """Format a monotonic elapsed duration for the notification history."""
    if seconds < 1:
        return "now"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def history_rows(
    toasts: Sequence[Notification],
    *,
    current_time: float,
    width: int,
    fallback_sigil: Optional[str] = None,
) -> tuple[HistoryRow, ...]:
    """Format newest-first history rows with a stable hanging indent."""
    width = max(2, width)
    if not toasts:
        return tuple(
            HistoryRow(line, "", "info")
            for line in wrap_cells(
                "No notifications yet — status messages will collect here.", width
            )
        )

    rows = []
    for toast in reversed(toasts):
        style = NOTIFICATION_STYLES.get(toast.kind, NOTIFICATION_STYLES["info"])
        sigil = fallback_sigil if fallback_sigil is not None else style.sigil
        age = toast_age(max(0.0, current_time - toast.born))
        prefix = f"{age:>4}  {sigil} "
        indent = " " * display_width(prefix)
        lines = wrap_notice(toast.text, max(2, width - display_width(prefix)))
        for index, line in enumerate(lines):
            gutter = prefix if index == 0 else indent
            rows.append(HistoryRow(gutter + line, gutter, toast.kind))
    return tuple(rows)


def toast_history_viewport(
    toasts: Sequence[Notification],
    *,
    current_time: float,
    y: int,
    bottom: int,
    width: int,
    scroll: int,
    close_key: str,
    scroll_keys: str,
    fallback_sigil: Optional[str] = None,
) -> Optional[HistoryViewport]:
    """Lay out a centered, bounded pager for notification history."""
    available_height = bottom - y
    if width < 18 or available_height < 4:
        return None

    inner_width = max(2, min(76, width - 8))
    rows = history_rows(
        toasts,
        current_time=current_time,
        width=inner_width,
        fallback_sigil=fallback_sigil,
    )
    pager = pager_layout(
        top=y,
        bottom=bottom,
        width=width,
        inner_width=inner_width,
        horizontal_chrome=4,
        total_rows=len(rows),
        scroll=scroll,
        content_sized=True,
        min_height=6,
    )
    visible = pager.viewport.visible
    scroll = pager.viewport.offset
    count = len(toasts)
    title = (
        f"Notifications ({count}) · {close_key} close"
        if count
        else f"Notifications · {close_key} close"
    )
    hint = f" {scroll_keys} scroll " if len(rows) > visible else ""
    hint_x = pager.x + max(2, pager.width - len(hint) - 2) if hint else pager.x
    return HistoryViewport(
        y=pager.y,
        x=pager.x,
        height=pager.height,
        width=pager.width,
        inner_width=inner_width,
        title=title,
        rows=rows[scroll : scroll + visible],
        total_rows=len(rows),
        visible_rows=visible,
        scroll=scroll,
        scroll_hint=hint,
        scroll_hint_x=hint_x,
    )
