"""Calendar + spend heat-map glyphs and date bucketing helpers."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

BLOCKS_UP = " ▁▂▃▄▅▆▇"  # 0..7 eighths, bottom-aligned — the top cell of a rising bar


def month_range(first: str, last: str) -> list[str]:
    # Inclusive list of "YYYY-MM" so quiet months show as gaps, not skips.
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def week_key(date_str: str) -> str:
    # The Monday ("YYYY-MM-DD") of the ISO week a date falls in. Sorts chronologically
    # as a plain string (so year boundaries are handled) and reads as "week of <date>".
    # Returns "" for a missing or unparseable date: some backends emit a workflow with
    # no usable timestamp (e.g. a metadata-only session), and such a row simply can't
    # sit on a timeline -- so callers treat a "" key as off-timeline rather than crash.
    try:
        d = datetime.strptime((date_str or "")[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


# The Calendar heat map's granularity is live-adjustable with +/- between these
# bounds; level 0 is always an in-range day with no spend. More levels = more shades,
# capped at the number of maximally-distinct colors the ramp can hold (so no two
# adjacent levels ever look the same).
HEAT_MIN_LEVELS = 3
HEAT_MAX_LEVELS = 11
HEAT_DEFAULT_LEVELS = 6
HEAT_EMPTY_GLYPH = "·"  # a tracked-but-empty day (level 0)
HEAT_RAMP = "░▒▓█"  # density glyphs; on 256-color the *color* carries the fine steps

# 256-color: the high-contrast green→red edge of the xterm color cube — eleven
# maximally-distinct steps (pure green, lime, yellow, orange, pure red). We sample N
# of these so each level is a clearly different hue even at the finest granularity;
# the four density glyphs necessarily repeat, but the color never does.
HEAT_CUBE_RAMP = (46, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196)


def heat_level(value: float, peak: float, levels: int) -> int:
    # Bucket a day's spend into one of `levels` shades relative to the busiest day of
    # the year. Spend spans orders of magnitude — a few heavy days dwarf the rest — so a
    # *linear* ramp dumps almost every ordinary day into the faintest tier and the map
    # reads as one flat shade. A log scale spreads the common low-spend days across the
    # whole ramp instead, while the peak day still tops out and any nonzero day shows at
    # least the faintest tier (mirroring cost_bar's "a sliver is never nothing").
    if value <= 0 or peak <= 0:
        return 0
    if value >= peak:
        return levels
    frac = math.log1p(value) / math.log1p(peak)
    return max(1, min(levels, math.ceil(frac * levels)))


def heat_band_label(v: float) -> str:
    # Compact dollar label for a legend bound: whole dollars past $10 (no gratuitous
    # decimals), but a decimal below it so the log scale's bunched low-end bounds stay
    # distinct instead of all rounding to the same "$1".
    if v >= 10:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:.1f}".rstrip("0").rstrip(".")
    return f"${v:.2f}".rstrip("0").rstrip(".")


def _heat_ansi_ramp() -> tuple[tuple[int, str], ...]:
    # 8-color fallback: only green/yellow/red exist, so each level is a distinct
    # (color, glyph) *pair* climbing in heat, keeping adjacent levels apart where the
    # 256-color ramp would collapse. Eleven pairs, matching HEAT_MAX_LEVELS.
    g, y, r = curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED
    return (
        (g, "░"), (g, "▒"), (g, "▓"),
        (y, "░"), (y, "▒"), (y, "▓"), (y, "█"),
        (r, "░"), (r, "▒"), (r, "▓"), (r, "█"),
    )  # fmt: skip


def heat_sample(n: int, ramp: tuple) -> list:
    # Evenly pick n entries from ramp; distinct whenever n <= len(ramp) (our level cap).
    if n <= 1:
        return [ramp[-1]]
    return [ramp[round(i * (len(ramp) - 1) / (n - 1))] for i in range(n)]


def heat_glyph(level: int, levels: int, has256: bool = True) -> str:
    # The glyph for a level. On 256-color it's a coarse ░▒▓█ density (the color carries
    # the real distinction); on 8-color it's the paired glyph that keeps each level
    # apart from its neighbours within the same ANSI color.
    if level <= 0:
        return HEAT_EMPTY_GLYPH
    if not has256:
        return heat_sample(levels, _heat_ansi_ramp())[level - 1][1]
    idx = level * len(HEAT_RAMP) // max(1, levels)
    return HEAT_RAMP[min(len(HEAT_RAMP) - 1, idx)]


def heat_palette(n: int, has256: bool) -> list[int]:
    # `n` colors from green (coolest) to red (hottest), sampled from the high-contrast
    # ramp so every level is a genuinely different shade. 256-color walks the cube's
    # green→red edge; 8-color collapses to the three ANSI heat colors (paired with the
    # glyph by heat_glyph to stay distinct).
    if has256:
        return list(heat_sample(n, HEAT_CUBE_RAMP))
    return [color for color, _glyph in heat_sample(n, _heat_ansi_ramp())]


MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def calendar_cells(
    year: str, by_date: dict[str, float]
) -> tuple[list[list[float | None]], list[tuple[int, str]], int]:
    # GitHub-style contribution grid for one calendar year: 7 rows (Mon..Sun) by up
    # to 53 week-columns. Each cell is that day's spend, or None for the padding days
    # of the leading/trailing partial weeks that spill outside the year. Also returns
    # the (column, "Jan".."Dec") anchors for the month labels, and the column count.
    y = int(year)
    jan1 = datetime(y, 1, 1)
    dec31 = datetime(y, 12, 31)
    grid_start = jan1 - timedelta(days=jan1.weekday())  # the Monday on/before Jan 1
    ncols = (dec31 - grid_start).days // 7 + 1
    grid: list[list[float | None]] = [[None] * ncols for _ in range(7)]
    months: list[tuple[int, str]] = []
    day = jan1
    while day <= dec31:
        col = (day - grid_start).days // 7
        grid[day.weekday()][col] = by_date.get(day.strftime("%Y-%m-%d"), 0.0)
        if day.day == 1:
            months.append((col, MONTH_ABBR[day.month - 1]))  # English, never locale-folded
        day += timedelta(days=1)
    return grid, months, ncols
