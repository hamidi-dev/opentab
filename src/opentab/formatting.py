"""Money / token / path string formatting and the rich-paint regexes."""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone

# Real token figures from human_tokens are always decimal + space-delimited
# ("35.0B", "1.0M"); model param tags are integer + hyphen-delimited ("-35B-A3B").
# Requiring the decimal and excluding hyphen boundaries keeps name segments from
# being mistaken for token counts (e.g. the "35B" in Qwen3.6-35B-A3B).
TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_.\-])\d+\.\d+[kMB](?![A-Za-z0-9_\-])")
MONEY_PATTERN = re.compile(r"\$\d+(?:,\d{3})*(?:\.\d+)?")
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


def tokens(value: int) -> str:
    return f"{value:,}"


def human_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


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
