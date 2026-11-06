"""Pure bounded Work layouts, reusing modal/menu geometry and cell formatting."""
from __future__ import annotations

from opentab.presentation.formatting import clip, human_duration, money, wrap_cells
from opentab.tui.components import menus
from opentab.tui.components.modal import StyledLine


def totals_lines(totals, cost_label: str) -> tuple[str, str]:
    cost = money(totals.cost) if totals.cost is not None else "—"
    time = human_duration(totals.worked_seconds) if totals.worked_seconds is not None else "—"
    return (
        f"{totals.linked} linked · {totals.available} available · {totals.unavailable} unavailable",
        f"{cost_label} {cost} ({totals.cost_measured}/{totals.available}) · Agent time {time} ({totals.time_measured}/{totals.available} measured)",
    )


def next_step_preview(text: str, width: int, *, limit=2) -> list[str]:
    rows = wrap_cells("Next step: " + (text or "—"), max(1, width))
    return rows[:limit] + (
        ["… read the full next step with View step"] if len(rows) > limit else []
    )


def work_layout(
    intro: list[str],
    options: list[tuple[str, ...]],
    selection: int | None,
    footer: list[str],
    error: str,
    width: int,
    height: int,
    *,
    empty="No work items in this view.",
):
    """Multiline rows retain one identity and hit target; narrow layouts never drop coverage."""
    lines = [StyledLine(clip(line, width), menus.MUTED) for line in intro]
    if error:
        lines.extend(StyledLine(line, menus.NOTICE) for line in wrap_cells(error, width)[:2])
    lines.append(StyledLine("", menus.NORMAL))
    # Expand before budgeting: numeric/coverage lines wrap rather than disappearing at 80.
    expanded = [
        [part for line in row for part in (wrap_cells(line, max(1, width - 4)) or [""])]
        for row in options
    ]
    row_height = max((len(row) for row in expanded), default=1)
    budget = max(1, (height - len(lines) - len(footer) - 2) // row_height)
    index = selection if selection is not None else 0
    start, end = menus.selection_window(len(options), index, budget)
    hits = []
    if start:
        lines.append(StyledLine(f"↑ {start} more", menus.DIM))
    if not expanded:
        lines.append(StyledLine(empty, menus.DIM))
    for offset in range(start, end):
        for line_index, line in enumerate(expanded[offset]):
            marker = "> " if selection == offset and line_index == 0 else "  "
            style = menus.SELECTED if selection == offset else menus.NORMAL
            lines.append(StyledLine(marker + line, style))
            hits.append((len(lines) - 1, offset))
    if end < len(options):
        lines.append(StyledLine(f"↓ {len(options) - end} more", menus.DIM))
    lines.extend(StyledLine(clip(line, width), menus.SUBTLE) for line in footer)
    return menus.MenuLayout(tuple(lines), tuple(hits), start)
