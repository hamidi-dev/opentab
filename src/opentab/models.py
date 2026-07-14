"""Plain data records (sessions, day/month/year/project rollups)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Workflow:
    id: str
    title: str
    directory: str
    created_at: str
    root_cost: float
    total_cost: float
    subagents: int
    model_count: int
    total_tokens: int
    unpriced_tokens: int
    # total_cost/root_cost hold the *active* figures; the App keeps a real snapshot
    # and an API-equivalent (real spend + what unpriced usage would cost at list
    # prices) so the "$" toggle can swap which one every panel reads. Not loaded
    # from SQL -- populated by App._snapshot_real_costs / _compute_api_costs.
    real_total_cost: float = 0.0
    real_root_cost: float = 0.0
    api_total_cost: float = 0.0
    api_root_cost: float = 0.0
    # (There is no third, what-if snapshot: the `w` what-if target is SESSION-SCOPED --
    # it reprices only the session-tree table on the Subagents tab, straight off that
    # session's workflow_nodes rows, and never touches these app-wide figures.)
    # Which backend produced this workflow ("OpenCode" / "Claude Code"); shown in the
    # sessions list (combined view) and the session detail. Empty for in-memory rows.
    source: str = ""


@dataclass
class DaySummary:
    day: str
    workflows: int
    cost: float
    tokens: int
    subagents: int
    unpriced_tokens: int


@dataclass
class MonthSummary:
    month: str
    workflows: int
    cost: float
    tokens: int
    subagents: int
    unpriced_tokens: int


# Sentinel YearSummary.year for the synthetic "All years" row, which unscopes the
# Months panel to the whole history. Picked so it never collides with a real "YYYY".
ALL_YEARS = "all"


def year_label(value: str) -> str:
    return "All years" if value == ALL_YEARS else value


@dataclass
class YearSummary:
    year: str
    workflows: int
    cost: float
    tokens: int
    subagents: int
    unpriced_tokens: int


@dataclass
class ProjectSummary:
    directory: str
    workflows: int
    cost: float
    tokens: int
    subagents: int
    unpriced_tokens: int
    last_active: str = ""  # created_at of the project's most recent session
    ignored: bool = False
