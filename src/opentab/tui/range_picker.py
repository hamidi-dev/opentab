"""Pending date-range choices; parsing and preview never mutate the active scope."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from opentab.util import month_window_start, parse_range_text

PRESETS = (
    "All time",
    "Today",
    "Yesterday",
    "Last 7 days",
    "Last 30 days",
    "This month",
    "Last month",
    "This year",
)

# Tab order; the quick expression field is focused on open.
FOCUSES = ("expression", "presets", "since", "until")


def preset_value(index: int, today: datetime | None = None) -> str:
    now = today or datetime.now()
    date = now.strftime("%Y-%m-%d")
    if index == 0:
        return "all"
    if index == 1:
        return f"{date}..{date}"
    if index == 2:
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        return f"{yesterday}..{yesterday}"
    if index in (3, 4):
        start = (now - timedelta(days=6 if index == 3 else 29)).strftime("%Y-%m-%d")
        return f"{start}..{date}"
    if index == 5:
        return now.strftime("%Y-%m")
    if index == 6:
        return (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if index == 7:
        return str(now.year)
    raise IndexError(index)


def resolved_bounds(raw: str, today: datetime | None = None) -> tuple[str | None, str | None]:
    days, months, since, until = parse_range_text(raw)
    now = today or datetime.now()
    if days is not None:
        since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    elif months is not None:
        since = month_window_start(months, now)
    return since, until


def range_preview(raw: str, today: datetime | None = None) -> str:
    since, until = resolved_bounds(raw, today)
    if since is None and until is None:
        return "All time"
    return f"{since or 'Any start'} → {until or 'No end'} (inclusive)"


@dataclass
class RangePicker:
    current: str
    index: int = 0
    focus: str = "expression"
    expression: str = ""
    # The seeded expression is replaced by the first typed character, like a
    # preselected input; erasing edits it instead.
    fresh: bool = False
    since: str = ""
    until: str = ""
    error: str = ""
    today: datetime = field(default_factory=datetime.now)

    @classmethod
    def open(cls, current: str) -> RangePicker:
        picker = cls(current)
        parsed = parse_range_text(current)
        picker.expression = current if parsed != (None, None, None, None) else ""
        picker.fresh = bool(picker.expression)
        picker.index = next((i for i in range(len(PRESETS)) if picker.is_current(i)), 0)
        try:
            since, until = resolved_bounds(current, picker.today)
        except (ValueError, OverflowError):
            _days, _months, since, until = parsed
        picker.since, picker.until = since or "", until or ""
        return picker

    def cycle(self, step: int) -> None:
        self.focus = FOCUSES[(FOCUSES.index(self.focus) + step) % len(FOCUSES)]
        self.error = ""

    def value(self) -> str:
        if self.focus == "expression":
            return self.expression
        if self.focus == "presets":
            return preset_value(self.index, self.today)
        return f"{self.since.strip()}..{self.until.strip()}"

    def preview(self) -> str:
        try:
            return range_preview(self.value(), self.today)
        except (ValueError, OverflowError) as exc:
            return f"Invalid: {exc}"

    def valid(self) -> bool:
        try:
            resolved_bounds(self.value(), self.today)
        except (ValueError, OverflowError) as exc:
            self.error = str(exc)
            return False
        self.error = ""
        return True

    def is_current(self, index: int) -> bool:
        return parse_range_text(self.current) == parse_range_text(preset_value(index, self.today))
