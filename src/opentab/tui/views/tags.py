"""Coverage-aware tag accounting text from prepared root aggregates only."""
from __future__ import annotations

from opentab.presentation.formatting import clip, human_duration, money, pad


def _coverage(value: str, measured: int, total: int) -> str:
    return f"{value} ({measured}/{total})" if 0 < measured < total else value


def tag_suggestion_lines(entries: list, width: int) -> list[str]:
    """Quiet completion rows from ``(label, aggregate)`` pairs, columns sized to fit.

    Coverage shows only when some sessions lack data; time drops first when narrow.
    """
    cells = []
    for label, row in entries:
        time = human_duration(row.agent_seconds) if row.time_measured else "—"
        cost = money(row.cost) if row.cost_measured else "—"
        cells.append(
            (
                label,
                f"{row.sessions} session" + ("" if row.sessions == 1 else "s"),
                _coverage(time, row.time_measured, row.sessions),
                _coverage(cost, row.cost_measured, row.sessions),
            )
        )
    widths = [max(len(cell[i]) for cell in cells) for i in (1, 2, 3)]
    columns = (1, 2, 3)
    label_width = width - sum(widths) - 2 * len(columns)
    if label_width < 12:
        columns = (1, 3)
        label_width = width - widths[0] - widths[2] - 4
    lines = []
    for cell in cells:
        numeric = "  ".join(cell[i].rjust(widths[i - 1]) for i in columns)
        lines.append(f"{pad(clip(cell[0], max(4, label_width)), max(4, label_width))}  {numeric}")
    return lines


def tag_drill_summary(rows: list, cost_label: str, width: int = 0) -> list[str]:
    """One line over the unique matching roots (never a sum of overlapping tag rows)."""
    measured = [w.worked_seconds for w in rows if w.worked_seconds is not None]
    time = human_duration(sum(measured)) if measured else "—"
    if 0 < len(measured) < len(rows):
        time += f" ({len(measured)}/{len(rows)})"
    priced = [w for w in rows if w.total_cost or not w.unpriced_tokens]
    cost = money(sum(w.total_cost for w in priced)) if priced else "—"
    if 0 < len(priced) < len(rows):
        cost += f" ({len(priced)}/{len(rows)})"
    unpriced = " · some usage unpriced" if any(w.unpriced_tokens for w in rows) else ""
    count = f"{len(rows)} session" + ("" if len(rows) == 1 else "s")
    line = f"{count} · {cost_label} {cost} · agent time {time}{unpriced}"
    if width and len(line) > width:
        line = f"{count} · {cost} · {time}" + (" !" if unpriced else "")
    return [line]
