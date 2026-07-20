"""Money / token / path string formatting and the rich-paint regexes."""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone

# Real token figures from human_tokens are always decimal + space-delimited
# ("35.0B", "1.0M"); model param tags are integer + hyphen-delimited ("-35B-A3B").
# Requiring the decimal and excluding hyphen boundaries keeps name segments from
# being mistaken for token counts (e.g. the "35B" in Qwen3.6-35B-A3B). Also exclude a
# leading "$": the digits inside a compact money label ("$1.2k") look exactly like a
# token count, and since write_rich paints tokens AFTER money, an unguarded match here
# would overpaint "$1.2k"'s digits token-grey, leaving only the "$" green.
TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_.\-$])\d+\.\d+[kMB](?![A-Za-z0-9_\-])")
# The trailing "k?" catches money_label's compact form ("$1.2k", "$12k") so the whole
# amount paints as one green span; guarded against a following letter so it never eats
# into an alphanumeric run.
MONEY_PATTERN = re.compile(r"\$\d+(?:,\d{3})*(?:\.\d+)?(?:k(?![A-Za-z]))?")
# Block glyphs (cost_bar / the ranked spend bars) fill their cell with the
# *foreground* colour, so under a selected row's A_REVERSE they invert to the
# theme background — a hole punched in the highlight band. Selected-row writers
# overdraw runs of them (matched here) non-reversed to keep the bar visible.
BAR_GLYPH_PATTERN = re.compile(r"[█▏▎▍▌▋▊▉]+")


def money(value: float) -> str:
    # A positive sub-cent cost rounds to "$0.00" and reads as free, which is
    # indistinguishable from genuinely unpriced rows. Show it as nonzero-but-tiny.
    if 0 < value < 0.005:
        return "<$0.01"
    return f"${value:,.2f}"


def money_label(value: float) -> str:
    # Compact spend for a label that sits on top of a (possibly narrow) bar, so
    # it fits where the full "$1,234.56" form would not. Empty for zero so blank
    # buckets stay unlabelled.
    if value <= 0:
        return ""
    if value < 0.005:
        return "<$0.01"
    if value < 10:
        return f"${value:.2f}"  # $2.34
    if value < 1000:
        return f"${value:.0f}"  # $234
    if value < 10000:
        return f"${value / 1000:.1f}k"  # $1.2k
    return f"${value / 1000:.0f}k"  # $12k


def pct(part: float, whole: float) -> str:
    if whole <= 0:
        return "-"
    share = 100.0 * part / whole
    if 0 < share < 1:
        return "<1%"
    return f"{round(share)}%"


BAR_CELLS = 8  # width of the inline spend bar lane in the Months/Days lists
BAR_EIGHTHS = " ▏▎▍▌▋▊▉"  # 0..7 eighths of a cell; a full cell is "█"


def cost_bar(value: float, peak: float, cells: int = 8) -> str:
    # Fixed-width unicode bar so spend magnitude is legible at a glance in the
    # Months/Days lists. Scaled to the largest value in the same list; any
    # positive value shows at least a sliver so cheap-but-nonzero rows are visible.
    if peak <= 0 or value <= 0:
        return " " * cells
    eighths = max(1, min(round((value / peak) * cells * 8), cells * 8))
    full, rem = divmod(eighths, 8)
    if full >= cells:
        return "█" * cells
    return ("█" * full + BAR_EIGHTHS[rem]).ljust(cells)


def iso_to_local(ts: str) -> str:
    # Claude Code timestamps are ISO-8601 UTC ("2026-06-10T18:46:00.000Z"); render
    # them as local "YYYY-MM-DD HH:MM:SS" to match Store's created_at (datetime(...,
    # 'localtime')). Python 3.9's fromisoformat rejects the "Z"/millisecond form, so
    # fall back to parsing the leading seconds as UTC.
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


def relative_age(ts: str, now: datetime | None = None) -> str:
    # "2h ago" / "3d ago" / "just now" for a machine summary's export time. `now` is
    # injectable so the Machines-mode freshness line is testable without pinning the
    # clock. Empty (blank) for the live box or an unparseable stamp.
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
        return "just now"  # a clock-skewed future stamp reads better than "-2h ago"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def tokens(value: int) -> str:
    return f"{value:,}"


def human_tokens(value: int) -> str:
    # Switch unit just BEFORE the boundary, not at it: rounding to one decimal first
    # turned 999,950 into "1000.0k" and 999,950,000 into "1000.0M" -- seven characters,
    # which overflow the fixed six-wide token cells (Renderer._split_cell) and shift that
    # row's remaining columns one place right of their headers. Nothing here ever exceeds
    # six characters now.
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
    # Compact wall-clock span for the Context graph's "how the session evolved"
    # line: seconds → minutes → "Hh Mm" → "Dd Hh". The coarser unit drops its
    # zero remainder ("2h" not "2h 0m") so the common cases stay short.
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
    # Terminal cells, not codepoints: east-asian Wide/Fullwidth glyphs take two
    # cells, combining marks none. An approximation (emoji ZWJ sequences and flags
    # are beyond east_asian_width), but it keeps CJK titles/paths in their columns.
    if value.isascii():
        return len(value)
    return sum(_char_cells(ch) for ch in value)


def clip(value: str, width: int) -> str:
    # Longest prefix within `width` display cells; a wide char that would straddle
    # the boundary is dropped, so the result never exceeds the cell budget.
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
    # clip()'s mirror: the longest *suffix* within `width` cells. What a scrolling input
    # field needs — you look at the end you're typing, not the start.
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


def wrap_cells(value: str, width: int) -> list[str]:
    # textwrap.wrap counts codepoints, so a CJK/emoji line it "wrapped" to 60 can still
    # be 100 cells wide -- and the pane then clips half of every line away. This wraps on
    # what the terminal actually spends: cells.
    if width <= 0:
        return []
    lines: list[str] = []
    current = ""
    for word in value.split():
        while display_width(word) > width:  # one word wider than the whole line: split it
            if current:
                lines.append(current)
                current = ""
            # A single glyph wider than the whole line (界 in a 1-cell pane) can't be
            # clipped to fit -- emit it whole and overflow by a cell rather than emit an
            # empty line and stall. Never drop it: this is someone's text.
            head = clip(word, width) or word[0]
            lines.append(head)
            word = word[len(head) :]
        if not word:
            continue
        candidate = f"{current} {word}" if current else word
        if display_width(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def pad(value: str, width: int) -> str:
    # ljust by display cells, so a padded wide-char row still fills exactly `width`.
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
    # A user prompt collapsed to a one-line turn-group title: fold whitespace and
    # cap it (the Turns renderer shortens further to the panel width). Empty in,
    # empty out, so callers can treat "" as "no prompt".
    if not text:
        return ""
    return " ".join(str(text).split())[:limit]


def _clip_tail(value: str, width: int) -> str:
    # Longest suffix within `width` display cells (the tail-keeping twin of clip).
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
