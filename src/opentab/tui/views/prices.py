"""Pure presentation for the model-price overlay."""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from opentab.accounting.pricing import family_label
from opentab.presentation.formatting import cost_bar, human_tokens, money, pad, shorten
from opentab.presentation.heatmap import PRICE_HEAT_LEVELS

PRICE_SORT_COLUMNS = (
    ("model", "model"),
    ("eff", "eff $/M"),
    ("use", "use"),
    ("input", "input"),
    ("output", "output"),
    ("cache_read", "cacheR"),
    ("cache_write", "cacheW"),
)
PRICE_COL_W = 8
PRICE_EFF_W = 9
PRICE_USE_BAR = 5
PRICE_USE_W = PRICE_USE_BAR + 4
PRICE_BLOCK_W = PRICE_EFF_W + 2 + PRICE_USE_W + 2 + PRICE_COL_W * 4 + 3
ROUTE_ABBR = {"github-copilot": "copilot"}


class PriceSpan(NamedTuple):
    """Text painted with a semantic style at a local cell position."""

    start: int
    text: str
    role: str
    level: int | None = None


class SortSpan(NamedTuple):
    key: str
    start: int
    width: int


@dataclass(frozen=True)
class PriceRow:
    text: str
    kind: str
    entry_index: int | None = None
    spans: tuple[PriceSpan, ...] = ()
    selected: bool = False


@dataclass(frozen=True)
class PriceTableLayout:
    intro: tuple[str, ...]
    header: str | None
    sort_spans: tuple[SortSpan, ...]
    rows: tuple[PriceRow, ...]
    total_rows: int
    scroll: int
    selected_index: int | None
    empty_message: str | None = None


@dataclass(frozen=True)
class PriceSessionEntry:
    created_at: str
    cost: float
    tokens: int
    title: str


@dataclass(frozen=True)
class PriceSessionLayout:
    summary: str | None
    header: str | None
    rows: tuple[str, ...]
    total_rows: int
    scroll: int
    empty_message: str | None = None


def intro_lines(
    source: str,
    token_mix: tuple[tuple[float, float, float, float], int] | None,
) -> tuple[str, ...]:
    parts = [source]
    if token_mix:
        (inp, out, cache_read, cache_write), _total = token_mix
        parts.append(
            "eff $/M = list rates at your mix: "
            f"{inp:.1%} in · {out:.1%} out · {cache_read:.1%} cacheR · "
            f"{cache_write:.1%} cacheW"
        )
        parts.append("~ = no cacheR rate")
    return (" · ".join(parts), "")


def route_tag(routes: Iterable[str]) -> str:
    seen: list[str] = []
    for route in routes:
        tag = ROUTE_ABBR.get(route, route.split("/", 1)[0])
        if tag not in seen:
            seen.append(tag)
    return "·".join(seen)


def eff_cell(entry) -> str:
    return f"~{entry.eff:.2f}" if entry.approx else f"{entry.eff:.2f}"


def use_cell(entry, peak: float) -> str:
    if entry.share <= 0:
        return " " * PRICE_USE_W
    return f"{cost_bar(entry.share, peak, PRICE_USE_BAR)}{entry.share:>4.0%}"


def raw_cells(entry) -> list[str]:
    input_rate, output_rate, cache_read, cache_write = entry.price
    cache_read_text = "—" if cache_read <= 0 < input_rate else f"{cache_read:.2f}"
    return [
        f"{input_rate:.2f}",
        f"{output_rate:.2f}",
        cache_read_text,
        f"{cache_write:.2f}",
    ]


def core_text(entry, name_width: int, peak: float) -> str:
    name = f"★ {entry.bare}" if getattr(entry, "pinned", False) else entry.bare
    cells = " ".join(f"{cell:>{PRICE_COL_W}}" for cell in raw_cells(entry))
    return (
        f"{pad(shorten(name, name_width), name_width)}  "
        f"{eff_cell(entry):>{PRICE_EFF_W}}  "
        f"{use_cell(entry, peak):<{PRICE_USE_W}}  {cells}"
    )


def _column_head(key: str, label: str, width: int, sort: str, descending: bool, left=False):
    if sort == key:
        label = f"{label} {'v' if descending else '^'}"
    return f"{label:<{width}}" if left else f"{label:>{width}}"


def header_text(name_width: int, sort: str, descending: bool) -> str:
    model = f"model {'v' if descending else '^'}" if sort == "model" else "model"
    eff = _column_head("eff", "eff $/M", PRICE_EFF_W, sort, descending)
    use = _column_head("use", "use", PRICE_USE_W, sort, descending, left=True)
    cells = " ".join(
        _column_head(key, label, PRICE_COL_W, sort, descending)
        for key, label in PRICE_SORT_COLUMNS[3:]
    )
    return f"{model:{name_width}}  {eff}  {use}  {cells}"


def sort_spans(header: str, width: int) -> tuple[SortSpan, ...]:
    drawn = shorten(header, width)
    spans = []
    pos = 0
    for key, label in PRICE_SORT_COLUMNS:
        start = drawn.find(label, pos)
        if start >= 0:
            spans.append(SortSpan(key, start, len(label)))
            pos = start + len(label)
    return tuple(spans)


def name_width(entries: Sequence, width: int) -> int:
    widest = max(
        len(entry.bare) + (2 if getattr(entry, "pinned", False) else 0) for entry in entries
    )
    return min(widest, max(12, width - PRICE_BLOCK_W - 3))


def use_peak(entries: Sequence) -> float:
    return max((entry.share for entry in entries), default=0.0)


def column_ranges(entries: Sequence) -> list[tuple[float, float] | None]:
    columns: list[list[float]] = [[entry.eff for entry in entries if entry.eff > 0], [], [], [], []]
    for entry in entries:
        for index, value in enumerate(entry.price):
            if value > 0:
                columns[index + 1].append(value)
    ranges = []
    for values in columns:
        low, high = (min(values), max(values)) if values else (0.0, 0.0)
        ranges.append((low, high) if high > low else None)
    return ranges


def heat_level(value: float, value_range: tuple[float, float] | None) -> int | None:
    if value_range is None:
        return None
    low, high = value_range
    if value <= low:
        return 0
    fraction = (math.log(value) - math.log(low)) / (math.log(high) - math.log(low))
    return max(0, min(PRICE_HEAT_LEVELS - 1, round(fraction * (PRICE_HEAT_LEVELS - 1))))


def group_label(group: str, view: str) -> str:
    return family_label(group) if view == "family" else group or "(direct)"


def entry_tag(entry, view: str) -> str:
    if view == "provider":
        return family_label(entry.family)
    tag = route_tag(entry.routes)
    status = getattr(entry, "status", "")
    if status and view == "all":
        tag = f"{tag}·{status}" if tag else status
    return tag


def empty_message(query: str, view: str) -> str:
    if query:
        return f"No model prices match the filter: {query}"
    if view == "all":
        return "No models.dev catalog on record — fetch one with r."
    return "No model usage on record yet."


def _render_rows(entries: Sequence, view: str) -> list[tuple]:
    rows = []
    grouped = view in ("family", "provider")
    previous = None
    for index, entry in enumerate(entries):
        pinned = getattr(entry, "pinned", False)
        if pinned and previous is None:
            rows.append(("header", "★ pinned"))
        elif (
            not pinned
            and grouped
            and (
                previous is None
                or getattr(previous, "pinned", False)
                or entry.group != previous.group
            )
        ):
            rows.append(("header", group_label(entry.group, view)))
        rows.append(("model", index, entry))
        previous = entry
    return rows


def _model_row(entry, index: int, selected: int, width: int, namew: int, peak: float, ranges, view):
    core = core_text(entry, namew, peak)
    tag = entry_tag(entry, view)
    tag_x = namew + 2 + PRICE_BLOCK_W + 2
    spans = []
    is_selected = index == selected
    if tag and tag_x < width:
        spans.append(
            PriceSpan(tag_x, shorten(tag, width - tag_x), "selected" if is_selected else "muted")
        )
    if not is_selected:
        x_eff = namew + 2
        if x_eff + PRICE_EFF_W <= width:
            level = heat_level(entry.eff, ranges[0])
            spans.append(
                PriceSpan(
                    x_eff,
                    f"{eff_cell(entry):>{PRICE_EFF_W}}",
                    "heat" if level is not None else "normal",
                    level,
                )
            )
        x_raw = x_eff + PRICE_EFF_W + 2 + PRICE_USE_W + 2
        for offset, (cell, value) in enumerate(zip(raw_cells(entry), entry.price)):
            cell_x = x_raw + offset * (PRICE_COL_W + 1)
            if cell_x + PRICE_COL_W > width:
                break
            level = heat_level(value, ranges[offset + 1])
            spans.append(
                PriceSpan(
                    cell_x,
                    f"{cell:>{PRICE_COL_W}}",
                    "heat" if level is not None else "normal",
                    level,
                )
            )
    return PriceRow(pad(shorten(core, width), width), "model", index, tuple(spans), is_selected)


def table_layout(
    entries: Sequence,
    *,
    source: str,
    token_mix: tuple[tuple[float, float, float, float], int] | None,
    view: str,
    sort: str,
    descending: bool,
    query: str,
    width: int,
    selection: int,
    scroll: int,
    visible: int,
) -> PriceTableLayout:
    intro = intro_lines(source, token_mix)
    if not entries:
        return PriceTableLayout(intro, None, (), (), 0, 0, None, empty_message(query, view))
    namew = name_width(entries, width)
    header = header_text(namew, sort, descending)
    selected = max(0, min(selection, len(entries) - 1))
    render_rows = _render_rows(entries, view)
    selected_row = next(
        row for row, item in enumerate(render_rows) if item[0] == "model" and item[1] == selected
    )
    anchor = (
        selected_row - 1
        if selected_row > 0 and render_rows[selected_row - 1][0] == "header"
        else selected_row
    )
    visible = max(1, visible)
    offset = max(0, min(scroll, max(0, len(render_rows) - visible)))
    if anchor < offset:
        offset = anchor
    elif selected_row >= offset + visible:
        offset = selected_row - visible + 1
    ranges = column_ranges(entries)
    peak = use_peak(entries)
    rows = []
    for item in render_rows[offset : offset + visible]:
        if item[0] == "header":
            rows.append(PriceRow(shorten(f"▸ {item[1]}", width), "header"))
        else:
            rows.append(_model_row(item[2], item[1], selected, width, namew, peak, ranges, view))
    return PriceTableLayout(
        intro,
        header,
        sort_spans(header, width),
        tuple(rows),
        len(render_rows),
        offset,
        selected,
    )


def table_lines(
    entries: Sequence,
    *,
    source: str,
    token_mix: tuple[tuple[float, float, float, float], int] | None,
    view: str,
    sort: str,
    descending: bool,
    query: str,
    width: int,
) -> list[str]:
    lines = list(intro_lines(source, token_mix))
    if not entries:
        lines.append(empty_message(query, view))
        return lines
    namew = name_width(entries, width)
    peak = use_peak(entries)
    lines.append(header_text(namew, sort, descending))
    for item in _render_rows(entries, view):
        if item[0] == "header":
            lines.append(f"▸ {item[1]}")
        else:
            entry = item[2]
            core = core_text(entry, namew, peak)
            tag = entry_tag(entry, view)
            lines.append(f"{core}  {tag}" if tag else core)
    return lines


def _session_summary(rows: Sequence[PriceSessionEntry]) -> str:
    subtotal = sum(row.cost for row in rows)
    return f"{len(rows)} session(s) · {money(subtotal)} on this model · most spend first"


def _session_header(source_header: str) -> str:
    return f"{'Started':<10} {'Cost':>9} {'Tokens':>8}  {source_header}Title"


def _session_row(row: PriceSessionEntry) -> str:
    return (
        f"{row.created_at[:10]:<10} {money(row.cost):>9} {human_tokens(row.tokens):>8}  "
        f"{row.title}"
    )


def session_lines(
    rows: Sequence[PriceSessionEntry], model: str, source_header: str = ""
) -> list[str]:
    if not rows:
        return [f"No sessions used {model}."]
    return [_session_summary(rows), _session_header(source_header)] + [
        _session_row(row) for row in rows
    ]


def session_layout(
    rows: Sequence[PriceSessionEntry],
    *,
    model: str,
    source_header: str,
    scroll: int,
    visible: int,
) -> PriceSessionLayout:
    if not rows:
        return PriceSessionLayout(None, None, (), 0, 0, f"No sessions used {model}.")
    visible = max(1, visible)
    offset = max(0, min(scroll, max(0, len(rows) - visible)))
    return PriceSessionLayout(
        _session_summary(rows),
        _session_header(source_header),
        tuple(_session_row(row) for row in rows[offset : offset + visible]),
        len(rows),
        offset,
    )
