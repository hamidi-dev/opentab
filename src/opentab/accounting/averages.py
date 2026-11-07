"""Range-wide averages: per calendar day, per active day and per-session spread.

Sessions count on their start date, like every calendar view. A session that runs
for days still lands on one day, so per-day figures are noisier than per-session
ones; the Summary tab shows both and says which is which.

Session costs are heavy-tailed: a handful of long sessions with subagents carry
most of the spend. The mean alone describes almost no real session, so every
spread reports the median and p90 beside it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Spread:
    median: float
    mean: float
    p90: float
    max: float
    count: int


@dataclass(frozen=True)
class SessionPoint:
    day: str  # YYYY-MM-DD start date
    cost: float
    tokens: int
    worked: float | None  # seconds; None when the harness can't tell work from waiting


@dataclass(frozen=True)
class RangeAverages:
    start: str
    end: str
    calendar_days: int
    active_days: int
    sessions: int
    cost: float
    tokens: int
    cost_spread: Spread
    token_spread: Spread
    worked_spread: Spread | None

    def per_day(self, total: float) -> float:
        return total / self.calendar_days if self.calendar_days else 0.0

    def per_active_day(self, total: float) -> float:
        return total / self.active_days if self.active_days else 0.0


def nearest_rank(ordered: Sequence[float], fraction: float) -> float:
    """The nearest-rank percentile of an ascending sequence (p90 = fraction 0.9)."""
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(fraction * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def spread(values: Iterable[float]) -> Spread | None:
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    if not n:
        return None
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return Spread(median, sum(ordered) / n, nearest_rank(ordered, 0.9), ordered[-1], n)


def _parse(day: str) -> date | None:
    try:
        return date.fromisoformat(day[:10])
    except ValueError:
        return None


def range_averages(
    points: Sequence[SessionPoint],
    since: str | None,
    until: str | None,
    history_start: str | None,
    today: str,
) -> RangeAverages | None:
    """Average the ranged sessions over the days the range actually covers.

    The span runs from the later of the range's start and the first recorded
    session anywhere (`history_start`), so a one-year window over three months of
    history doesn't divide by 365. It ends at the range's end, capped at today,
    and stretches to cover any session dated past it (clock skew, time zones).
    """
    dated = [p for p in points if _parse(p.day) is not None]
    if not dated:
        return None
    days = sorted({p.day[:10] for p in dated})
    floor = max(filter(None, (since, history_start)), default=days[0])
    start = min(floor, days[0])
    end = max(min(until, today) if until else today, days[-1])
    first, last = _parse(start), _parse(end)
    calendar_days = (last - first).days + 1 if first and last else len(days)
    cost_spread = spread(p.cost for p in dated)
    token_spread = spread(p.tokens for p in dated)
    assert cost_spread is not None and token_spread is not None
    return RangeAverages(
        start=start,
        end=end,
        calendar_days=max(calendar_days, len(days)),
        active_days=len(days),
        sessions=len(dated),
        cost=sum(p.cost for p in dated),
        tokens=sum(p.tokens for p in dated),
        cost_spread=cost_spread,
        token_spread=token_spread,
        worked_spread=spread(p.worked for p in dated if p.worked is not None),
    )
