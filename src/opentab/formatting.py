"""Money / token / path string formatting and the rich-paint regexes."""
from __future__ import annotations

import math
import os
import re
import unicodedata
from datetime import datetime, timezone

# Require human_tokens' decimal form and reject model-tag/money boundaries; token paint
# runs after money paint and must not overpaint compact dollar labels.
TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_.\-$])\d+\.\d+[kMB](?![A-Za-z0-9_\-])")
# Include compact ``k`` amounts without consuming a following identifier.
MONEY_PATTERN = re.compile(r"\$\d+(?:,\d{3})*(?:\.\d+)?(?:k(?![A-Za-z]))?")
# Selected rows redraw these foreground bars without A_REVERSE to avoid highlight holes.
BAR_GLYPH_PATTERN = re.compile(r"[█▏▎▍▌▋▊▉]+")


def money(value: float) -> str:
    # Preserve the distinction between positive sub-cent spend and unpriced $0.00.
    if 0 < value < 0.005:
        return "<$0.01"
    return f"${value:,.2f}"


def money_label(value: float) -> str:
    if value <= 0:
        return ""
    if value < 0.005:
        return "<$0.01"
    if value < 10:
        return f"${value:.2f}"
    if value < 1000:
        return f"${value:.0f}"
    if value < 10000:
        return f"${value / 1000:.1f}k"
    return f"${value / 1000:.0f}k"


def pct(part: float, whole: float) -> str:
    if whole <= 0:
        return "-"
    share = 100.0 * part / whole
    if 0 < share < 1:
        return "<1%"
    return f"{round(share)}%"


BAR_CELLS = 8
BAR_EIGHTHS = " ▏▎▍▌▋▊▉"


def cost_bar(value: float, peak: float, cells: int = 8) -> str:
    # Any positive value gets at least one eighth-cell, preserving nonzero spend.
    if peak <= 0 or value <= 0:
        return " " * cells
    eighths = max(1, min(round((value / peak) * cells * 8), cells * 8))
    full, rem = divmod(eighths, 8)
    if full >= cells:
        return "█" * cells
    return ("█" * full + BAR_EIGHTHS[rem]).ljust(cells)


def iso_to_local(ts: str) -> str:
    # Normalize ISO UTC timestamps to Store's local created_at format.
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return ts[:19].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def iso_to_epoch(ts: str) -> float | None:
    # Read naive stamps as UTC so duration arithmetic remains timezone-absolute.
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


WORKED_BURST_GAP_SECONDS = 30 * 60


def worked_seconds(event_epochs, prompt_epochs) -> float | None:
    # Exclude gaps ending at human prompts and long unmarked idle gaps. Return None when
    # there is insufficient activity; discard non-finite input before duration rendering.
    times = sorted(e for e in event_epochs if e is not None and math.isfinite(e))
    if len(times) < 2:
        return None
    prompts = {p for p in prompt_epochs if p is not None and math.isfinite(p)}
    total = 0.0
    for a, b in zip(times, times[1:]):
        if b in prompts or b - a > WORKED_BURST_GAP_SECONDS:
            continue
        total += b - a
    return total


def relative_age(ts: str, now: datetime | None = None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    secs = (now - dt.astimezone(timezone.utc)).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def human_bytes(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:,.1f} GB"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:,.1f} MB"
    if n >= 1024:
        return f"{n / 1024:,.0f} KB"
    return f"{n:,} B"


def tokens(value: int) -> str:
    return f"{value:,}"


def human_tokens(value: int) -> str:
    # Switch before rounded unit boundaries so fixed six-cell columns never get ``1000.0k``.
    if value >= 999_950_000_000:
        return f"{value / 1_000_000_000_000:.1f}T"
    if value >= 999_950_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 999_950:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _char_cells(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(value: str) -> int:
    # Terminal-cell approximation: wide glyphs use two cells and combining marks none.
    if value.isascii():
        return len(value)
    return sum(_char_cells(ch) for ch in value)


def clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if value.isascii():
        return value[:width]
    if display_width(value) <= width:
        return value
    out = []
    used = 0
    for ch in value:
        cells = _char_cells(ch)
        if used + cells > width:
            break
        out.append(ch)
        used += cells
    return "".join(out)


def clip_tail(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if value.isascii():
        return value[-width:] if len(value) > width else value
    if display_width(value) <= width:
        return value
    out = []
    used = 0
    for ch in reversed(value):
        cells = _char_cells(ch)
        if used + cells > width:
            break
        out.append(ch)
        used += cells
    return "".join(reversed(out))


def wrap_cells(value: str, width: int, indent: str = "") -> list[str]:
    # ``textwrap`` counts codepoints rather than terminal cells.
    # ``indent`` prefixes every continuation line and is charged against the width, so a
    # caller that wants hanging indentation does not have to re-wrap what it just wrapped.
    if width <= 0:
        return []
    lead = display_width(indent)
    if lead >= width:
        # An indent that leaves no room to write in is worse than no indent: it would
        # push every continuation past the width the caller asked to fit inside.
        indent, lead = "", 0
    lines: list[str] = []
    current = ""

    def room() -> int:
        return width if not lines else max(1, width - lead)

    def emit(text: str) -> None:
        lines.append((indent if lines else "") + text)

    pending = value.split()
    while pending:
        word = pending.pop(0)
        free = room() - (display_width(current) + 1 if current else 0)
        if display_width(word) > free:
            if current:
                # Flush and RETRY rather than starting the next line with this word:
                # measured against the first line's room it may not fit the continuation,
                # whose width the indent has shrunk -- which is how an indented wrap came
                # back wider than the caller asked for.
                emit(current)
                current = ""
                pending.insert(0, word)
                continue
            head = clip(word, room())
            if not head and lines and display_width(word[0]) <= width:
                # The continuation room is narrower than one glyph but the full width
                # would hold it: spend the indent rather than overflow the pane.
                lines.append(clip(word, width))
                word = word[len(lines[-1]) :]
                if word:
                    pending.insert(0, word)
                continue
            # Preserve a single glyph wider than the pane rather than stalling or dropping it.
            head = head or word[0]
            if len(head) < len(word):
                pending.insert(0, word[len(head) :])
            emit(head)
            continue
        current = f"{current} {word}" if current else word
    if current:
        emit(current)
    return lines


def pad(value: str, width: int) -> str:
    return value + " " * max(0, width - display_width(value))


def shorten(value: str, width: int) -> str:
    if width <= 0:
        return ""
    value = value.replace("\n", " ").replace("\t", " ")
    if value.isascii():
        if len(value) <= width:
            return value
        if width <= 3:
            return value[:width]
        return value[: width - 3] + "..."
    if display_width(value) <= width:
        return value
    if width <= 3:
        return clip(value, width)
    return clip(value, width - 3) + "..."


def _clean_prompt(text, limit: int = 160) -> str:
    if not text:
        return ""
    return " ".join(str(text).split())[:limit]


def _clip_tail(value: str, width: int) -> str:
    if value.isascii():
        return value[len(value) - width :] if width > 0 else ""
    out = []
    used = 0
    for ch in reversed(value):
        cells = _char_cells(ch)
        if used + cells > width:
            break
        out.append(ch)
        used += cells
    out.reverse()
    return "".join(out)


def short_path(path: str, width: int) -> str:
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home) :]
    if display_width(path) <= width:
        return path
    if width <= 4:
        return _clip_tail(path, width)
    return "..." + _clip_tail(path, width - 3)
