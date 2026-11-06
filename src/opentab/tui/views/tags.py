"""Coverage-aware tag accounting text from prepared root aggregates only."""
from __future__ import annotations

from opentab.presentation.formatting import clip, human_duration, money, pad


def tag_accounting_text(row, width: int) -> str:
    """A bounded table row; never present overlapping rows as an additive total."""
    time = human_duration(row.agent_seconds) if row.time_measured else "—"
    cost = money(row.cost) if row.cost_measured else "—"
    if row.cost_measured < row.sessions:
        cost += f" ({row.cost_measured}/{row.sessions})"
    coverage = f"{time} · {row.time_measured}/{row.sessions} measured"
    numeric = f"{row.sessions:>3}  {coverage:<24}  {cost:>8}"
    label_width = max(8, width - len(numeric) - 2)
    return f"{pad(clip(row.label, label_width), label_width)}  {numeric}"


def tag_drill_summary(rows: list, cost_label: str, width: int = 0) -> list[str]:
    """Unique matching roots (not a sum of tag rows), with missing-time coverage."""
    measured = [w.worked_seconds for w in rows if w.worked_seconds is not None]
    time = human_duration(sum(measured)) if measured else "—"
    priced = [w for w in rows if w.total_cost or not w.unpriced_tokens]
    cost = money(sum(w.total_cost for w in priced)) if priced else "—"
    if len(priced) < len(rows):
        cost += f" ({len(priced)}/{len(rows)} available)"
    unpriced = sum(w.unpriced_tokens for w in rows)
    lines = [
        f"Matching roots: {len(rows)} · {cost_label}: {cost}"
        + (" · includes unpriced usage" if unpriced else ""),
        f"Agent time: {time} · {len(measured)}/{len(rows)} sessions measured",
    ]
    if width and any(len(line) > width for line in lines):
        return [
            f"Matching roots: {len(rows)}",
            f"{cost_label.replace(' (whole roots)', '')}: {cost}" + (" !" if unpriced else ""),
            f"Agent time: {time} · {len(measured)}/{len(rows)} measured",
        ]
    return lines
