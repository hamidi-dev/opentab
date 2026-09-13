"""Pure chart layouts shared by terminal renderers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from opentab.presentation.formatting import money, money_label
from opentab.presentation.heatmap import BLOCKS_UP


@dataclass(frozen=True)
class BarChartLayout:
    lines: tuple[str, ...]
    slots: tuple[tuple[int, int, str], ...] | None = None
    click_rows: int = 0


TreemapRect = tuple[str, float, int, int, int, int]


def bar_chart(
    pairs: Sequence[tuple[str, float]],
    width: int,
    height: int,
    *,
    keys: Sequence[str] | None = None,
    selected: str | None = None,
) -> BarChartLayout:
    """Lay out a vertical spend chart and its local hit geometry."""
    if not pairs or height < 5:
        return BarChartLayout(("Not enough room to chart.",))
    margin = 1
    plot_w = max(4, width - margin)
    label_w = max((len(label) for label, _ in pairs), default=2)
    ideal = label_w + 2
    if len(pairs) * ideal <= plot_w:
        col_w = ideal
    else:
        col_w = next((cells for cells in (4, 3, 2) if len(pairs) * cells <= plot_w), 1)
    fit = max(1, plot_w // col_w)
    shown = pairs[-fit:]
    count = len(shown)
    step = min(float(ideal), plot_w / count)
    bar_w = max(1, min(int(step) - 1, 4))

    def x0_of(index: int) -> int:
        lo = round(index * step)
        hi = round((index + 1) * step)
        return margin + lo + max(0, (hi - lo - bar_w) // 2)

    shown_keys = tuple((keys or [label for label, _ in pairs])[len(pairs) - count :])
    slots = tuple(
        (margin + round(index * step), margin + round((index + 1) * step) - 1, shown_keys[index])
        for index in range(count)
    )
    peak = max((value for _, value in shown), default=0.0)
    scale = peak or 1.0
    rows_n = max(2, height - 4)
    total_w = margin + round(count * step)
    grid = [[" "] * total_w for _ in range(rows_n + 1)]
    tops: list[tuple[int, int, float]] = []
    for index, (_label, value) in enumerate(shown):
        full, remainder = divmod(round((value / scale) * rows_n * 8), 8)
        x0 = x0_of(index)
        for offset in range(full):
            for dx in range(bar_w):
                grid[rows_n - offset][x0 + dx] = "█"
        if remainder:
            for dx in range(bar_w):
                grid[rows_n - full][x0 + dx] = BLOCKS_UP[remainder]
        filled = full + (1 if remainder else 0)
        if filled:
            tops.append((index, rows_n - filled + 1, value))

    def place_value(index: int, top_row: int, value: float) -> None:
        labels = [money_label(value)]
        if 1 <= value < 1000:
            labels.append(f"${value:.0f}")
        labels = [label for pos, label in enumerate(labels) if label and label not in labels[:pos]]
        if not labels:
            return
        center = x0_of(index) + bar_w // 2
        if top_row - 1 < 0:
            return
        for text in labels:
            start = max(margin, min(center - len(text) // 2, total_w - len(text)))
            lo, hi = start - 1, start + len(text)
            columns = range(max(margin, lo), min(total_w, hi + 1))
            for label_row in range(top_row - 1, -1, -1):
                if all(grid[label_row][column] == " " for column in columns):
                    for offset, char in enumerate(text):
                        grid[label_row][start + offset] = char
                    return

    tops.sort(key=lambda item: item[2], reverse=True)
    if tops:
        place_value(*tops[0])
    for spec in sorted(tops[1:], key=lambda item: item[0]):
        place_value(*spec)
    lines = ["".join(row).rstrip() for row in grid]
    lines.append(" " * margin + "─" * (total_w - margin))
    axis = [" "] * total_w

    def place(position: int, label: str) -> None:
        for offset, char in enumerate(label):
            if 0 <= position + offset < len(axis):
                axis[position + offset] = char

    tail = len(axis) - len(shown[-1][0])
    place(tail, shown[-1][0])
    next_free = margin
    for index, (label, _value) in enumerate(shown[:-1]):
        position = x0_of(index)
        if position >= next_free and position + len(label) < tail:
            place(position, label)
            next_free = position + len(label) + 1
    lines.append("".join(axis).rstrip())
    click_rows = len(lines)
    if selected in shown_keys:
        selected_index = shown_keys.index(selected)
        marker = [" "] * total_w
        center = min(x0_of(selected_index) + bar_w // 2, total_w - 1)
        marker[center] = "▲"
        text = f" {selected} · {money(shown[selected_index][1])}"
        if center + 1 + len(text) <= total_w:
            start = center + 1
        else:
            text = f"{selected} · {money(shown[selected_index][1])} "
            start = max(0, center - len(text))
        for offset, char in enumerate(text):
            if 0 <= start + offset < total_w and start + offset != center:
                marker[start + offset] = char
        lines.append("".join(marker).rstrip())
    total = sum(value for _, value in shown)
    if total:
        peak_label = max(shown, key=lambda item: item[1])[0]
        lines.append(
            f"{' ' * margin}peak {money(peak)} on {peak_label}    "
            f"total {money(total)}    avg {money(total / len(shown))}"
        )
    else:
        lines.append(f"{' ' * margin}no spend in view")
    if len(shown) < len(pairs):
        lines.append(f"{' ' * margin}(most recent {len(shown)} of {len(pairs)} — widen for more)")
    return BarChartLayout(tuple(lines), slots, click_rows)


def treemap_rects(items: Sequence[tuple[str, float]], width: int, height: int) -> list[TreemapRect]:
    """Partition positive values using balanced binary splits."""
    out: list[TreemapRect] = []

    def place(rows: Sequence[tuple[str, float]], x: int, y: int, w: int, h: int) -> None:
        if not rows or w <= 0 or h <= 0:
            return
        if len(rows) == 1 or w * h == 1:
            name = rows[0][0] if len(rows) == 1 else "Other"
            out.append((name, sum(value for _, value in rows), x, y, w, h))
            return
        total = sum(value for _, value in rows)
        half = total / 2
        split = min(
            range(1, len(rows)),
            key=lambda index: abs(sum(value for _, value in rows[:index]) - half),
        )
        left, right = rows[:split], rows[split:]
        share = sum(value for _, value in left) / total
        vertical = w >= h
        if vertical and w < 2:
            vertical = False
        elif not vertical and h < 2:
            vertical = True
        if vertical:
            cut = max(1, min(w - 1, round(w * share)))
            place(left, x, y, cut, h)
            place(right, x + cut, y, w - cut, h)
        else:
            cut = max(1, min(h - 1, round(h * share)))
            place(left, x, y, w, cut)
            place(right, x, y + cut, w, h - cut)

    positive = [(name, float(value)) for name, value in items if value > 0]
    place(positive, 0, 0, max(1, width), max(1, height))
    return out
