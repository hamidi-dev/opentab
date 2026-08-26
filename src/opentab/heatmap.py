"""Calendar + spend heat-map glyphs and date bucketing helpers."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

BLOCKS_UP = " ▁▂▃▄▅▆▇"


def month_range(first: str, last: str) -> list[str]:
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
    # Monday sorts chronologically as text; invalid dates stay off the timeline.
    try:
        d = datetime.strptime((date_str or "")[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


# Level 0 is an in-range day with no spend; the cap keeps adjacent shades distinct.
HEAT_MIN_LEVELS = 3
HEAT_MAX_LEVELS = 11
HEAT_DEFAULT_LEVELS = 6
HEAT_EMPTY_GLYPH = "·"
HEAT_RAMP = "░▒▓█"

# Dedicated fixed pair ranges prevent dynamic calendar levels from shifting other ramps.
PRICE_HEAT_LEVELS = 5
PRICE_HEAT_BASE_PAIR = 20

TOOL_HEAT_LEVELS = 6
TOOL_HEAT_BASE_PAIR = 33

# This categorical ramp means different token types, not increasing heat. Slot order is
# validated for dark/light contrast and colour-vision separation; do not reorder casually.
TOKEN_SERIES_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181")
TOKEN_SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4")
TOKEN_SERIES_BASE_PAIR = 26

# Glyphs preserve category identity when a terminal lacks enough colour pairs.
TOKEN_SERIES_GLYPHS = ("█", "▓", "▒", "░", "▚")


def token_series(dark: bool) -> tuple[str, ...]:
    return TOKEN_SERIES_DARK if dark else TOKEN_SERIES_LIGHT


def token_series_ansi() -> tuple[int, ...]:
    return (
        curses.COLOR_BLUE,
        curses.COLOR_RED,
        curses.COLOR_GREEN,
        curses.COLOR_YELLOW,
        curses.COLOR_MAGENTA,
    )


# High-contrast green-to-red edge of the xterm cube, sampled to distinct levels.
HEAT_CUBE_RAMP = (46, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196)


def heat_level(value: float, peak: float, levels: int) -> int:
    # Log scaling keeps ordinary days distinguishable from a few dominant peaks.
    if value <= 0 or peak <= 0:
        return 0
    if value >= peak:
        return levels
    frac = math.log1p(value) / math.log1p(peak)
    return max(1, min(levels, math.ceil(frac * levels)))


def heat_band_label(v: float) -> str:
    if v >= 10:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:.1f}".rstrip("0").rstrip(".")
    return f"${v:.2f}".rstrip("0").rstrip(".")


def _heat_ansi_ramp() -> tuple[tuple[int, str], ...]:
    # Pair ANSI colours with density glyphs so adjacent fallback levels remain distinct.
    g, y, r = curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED
    return (
        (g, "░"), (g, "▒"), (g, "▓"),
        (y, "░"), (y, "▒"), (y, "▓"), (y, "█"),
        (r, "░"), (r, "▒"), (r, "▓"), (r, "█"),
    )  # fmt: skip


def heat_sample(n: int, ramp: tuple) -> list:
    if n <= 1:
        return [ramp[-1]]
    return [ramp[round(i * (len(ramp) - 1) / (n - 1))] for i in range(n)]


def heat_glyph(level: int, levels: int, has256: bool = True) -> str:
    if level <= 0:
        return HEAT_EMPTY_GLYPH
    if not has256:
        return heat_sample(levels, _heat_ansi_ramp())[level - 1][1]
    idx = level * len(HEAT_RAMP) // max(1, levels)
    return HEAT_RAMP[min(len(HEAT_RAMP) - 1, idx)]


def heat_palette(n: int, has256: bool) -> list[int]:
    if has256:
        return list(heat_sample(n, HEAT_CUBE_RAMP))
    return [color for color, _glyph in heat_sample(n, _heat_ansi_ramp())]


MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def calendar_cells(
    year: str, by_date: dict[str, float]
) -> tuple[list[list[float | None]], list[tuple[int, str]], int]:
    y = int(year)
    jan1 = datetime(y, 1, 1)
    dec31 = datetime(y, 12, 31)
    grid_start = jan1 - timedelta(days=jan1.weekday())
    ncols = (dec31 - grid_start).days // 7 + 1
    grid: list[list[float | None]] = [[None] * ncols for _ in range(7)]
    months: list[tuple[int, str]] = []
    day = jan1
    while day <= dec31:
        col = (day - grid_start).days // 7
        grid[day.weekday()][col] = by_date.get(day.strftime("%Y-%m-%d"), 0.0)
        if day.day == 1:
            months.append((col, MONTH_ABBR[day.month - 1]))
        day += timedelta(days=1)
    return grid, months, ncols
