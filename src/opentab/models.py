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
    # Which machine this session ran on. Empty for local data; stamped by RemoteStore
    # with the exporting machine's label so the consolidated view can tag/group by box.
    # A second, orthogonal dimension to `source` (a session has both a tool and a host).
    machine: str = ""
    # When the session's LAST recorded activity happened, same local
    # "YYYY-MM-DD HH:MM:SS" string as created_at. Each backend fills it from data it
    # already reads (a time_updated column, the max event timestamp of the parse it
    # runs anyway) -- never a new scan. Empty = the backend can't know (an old
    # --export, a schema without the column). Purely for the "(until 16:42)" hint on
    # the detail line; the headline duration is worked_seconds, not this span.
    ended_at: str = ""
    # How long the agent ACTUALLY worked, in seconds -- the sum of its working bursts
    # with the idle gaps (you reading/composing the next prompt) removed, computed at
    # parse time by formatting.worked_seconds from the timestamps a backend already
    # walks. NOT the wall-clock span (created_at..ended_at), which includes those
    # waits. None when the backend can't tell work from waiting (a source with no
    # human-turn markers -- Copilot OTEL, VS Code -- or an old --export); the UI then
    # shows blank rather than a misleading elapsed-time or a fake 0s.
    worked_seconds: float | None = None


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


@dataclass
class MachineSummary:
    # One box in the fleet view (--source remote / --pull). `name` is the machine label
    # (w.machine); the three trailing fields are the "niceties" the plain rollup can't
    # give -- whether this is the LIVE local machine (full drill-in) or a pulled snapshot,
    # and, for a snapshot, when it was exported and by which opentab. Populated by
    # App.machines from the grouped workflows plus the store's machine_meta.
    name: str
    workflows: int
    cost: float
    tokens: int
    subagents: int
    unpriced_tokens: int
    last_active: str = ""  # created_at of the machine's most recent session
    live: bool = False  # this machine's own live data (not a pulled summary)
    exported_at: str = ""  # ISO time the summary was exported (blank for the live box)
    opentab_version: str = ""  # opentab that wrote the summary (blank for the live box)
