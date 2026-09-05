"""Plain data records (sessions, day/month/year/project rollups)."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

API_SCHEMA_VERSION = "1"


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
    # Active costs swap between the real and API-equivalent snapshots populated by App.
    real_total_cost: float = 0.0
    real_root_cost: float = 0.0
    api_total_cost: float = 0.0
    api_root_cost: float = 0.0
    # No app-wide `w` snapshot: that comparison is session-scoped and uses model rows.
    source: str = ""
    # Machine is orthogonal to source; local rows are untagged.
    machine: str = ""
    # Last subtree activity, in created_at's format; empty falls back to created_at.
    # Group summaries' last_active instead means the newest session's start.
    ended_at: str = ""
    # Agent working bursts excluding idle gaps; None when the backend lacks boundaries.
    worked_seconds: float | None = None


@dataclass(frozen=True)
class SessionRef:
    """Globally address one native session without reserving delimiters in its fields."""

    machine: str
    harness: str
    native_id: str

    PREFIX = "ot1_"

    def encode(self) -> str:
        raw = json.dumps(
            [self.machine, self.harness, self.native_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return self.PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str) -> SessionRef:
        if not isinstance(value, str) or not value.startswith(cls.PREFIX):
            raise ValueError("not an OpenTab session reference")
        token = value[len(cls.PREFIX) :]
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            fields = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid OpenTab session reference") from exc
        if (
            not isinstance(fields, list)
            or len(fields) != 3
            or any(not isinstance(field, str) or not field for field in fields)
        ):
            raise ValueError("invalid OpenTab session reference")
        return cls(*fields)


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


# Non-colliding identity for the synthetic unscoped year row.
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
    last_active: str = ""
    # Unlike last_active (newest start), this includes later activity in older subtrees.
    last_activity: str = ""
    ignored: bool = False


# Display name only; ``MachineSummary.fleet`` is the identity because labels are free text.
ALL_MACHINES = "all machines"


@dataclass
class MachineSummary:
    name: str
    workflows: int
    cost: float
    tokens: int
    subagents: int
    unpriced_tokens: int
    last_active: str = ""
    live: bool = False
    exported_at: str = ""
    opentab_version: str = ""
    # A flag avoids reserving a synthetic identity in the free-text name field.
    fleet: bool = False
