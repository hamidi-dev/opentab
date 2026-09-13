"""Pure token-breakdown and list-rate economics card formatting."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from opentab.presentation.formatting import human_tokens, money, wrap_lines
from opentab.tui.components.bars import StyledLine, legend_lines, stack_line


@dataclass(frozen=True)
class TokenCard:
    title: str
    groups: tuple[tuple[StyledLine, ...], ...]
    notes: tuple[str, ...] = ()
    header: str | None = None


@dataclass(frozen=True)
class EconomicsCategory:
    label: str
    tokens: float
    cost: float
    slot: int


def _wrapped(lines: Iterable[str], width: int) -> list[StyledLine]:
    return [StyledLine(line) for line in wrap_lines(lines, width)]


def exact_token_count(value: float) -> str:
    value = value or 0
    if isinstance(value, int):
        return f"{value:,}"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{float(value):,.6f}".rstrip("0").rstrip(".")


def token_breakdown_card(
    *,
    title: str,
    inner_width: int,
    note_width: int,
    input_tokens: float,
    output_tokens: float,
    reasoning_tokens: float,
    cache_read_tokens: float,
    cache_write_tokens: float,
    recorded_total: float,
    cache_write_1h: float = 0,
    attributed: bool = False,
    calls: int = 0,
    notes: Sequence[str] = (),
    colored: bool,
) -> TokenCard:
    """Format the normalized five-part split without redefining recorded total."""
    categories = (
        ("Uncached input", input_tokens),
        ("Model output", output_tokens),
        ("Reasoning", reasoning_tokens),
        ("Cache read", cache_read_tokens),
        ("Cache write", cache_write_tokens),
    )
    category_total = sum(value for _label, value in categories)
    composition: list[StyledLine] = []
    if category_total > 0 and inner_width >= 20:
        segments = [(label, value, slot) for slot, (label, value) in enumerate(categories)]
        composition = [stack_line(segments, category_total, inner_width, colored=colored)]
        composition.extend(
            legend_lines(
                [(label, slot) for label, value, slot in segments if value > 0],
                inner_width,
                colored=colored,
            )
        )
        composition.append(StyledLine(""))

    rows = []
    for label, value in categories:
        share = f"{100 * value / category_total:.1f}%" if category_total else "-"
        count = exact_token_count(value)
        avg = f"   avg {human_tokens(int(value / calls))}" if attributed and calls else ""
        text = (
            f"{label:<14} {count:>16}  {share:>6}{avg}"
            if inner_width >= 48
            else f"{label}: {count} ({share}){avg}"
        )
        rows.extend(_wrapped([text], inner_width))

    if cache_write_1h:
        rows.extend(
            _wrapped(
                [f"of cache writes, 1h: {exact_token_count(cache_write_1h)} (subset)"],
                inner_width,
            )
        )
    rows.append(StyledLine(f"Category sum: {exact_token_count(category_total)}"))
    rows.append(StyledLine(f"Recorded total: {exact_token_count(recorded_total)}"))
    delta = recorded_total - category_total
    if abs(delta) > 1e-9:
        direction = "higher" if delta > 0 else "lower"
        rows.extend(
            _wrapped(
                [
                    f"Mismatch: recorded total is {exact_token_count(abs(delta))} "
                    f"{direction} than the five-category sum."
                ],
                inner_width,
            )
        )
    return TokenCard(
        title,
        (tuple(composition + rows),),
        tuple(line.text for line in _wrapped(notes, note_width)),
    )


def _share_text(value: float, total: float) -> str:
    share = 100.0 * value / total if total > 0 else 0.0
    if share >= 10 or share == 0:
        return f"{share:.0f}%"
    if share >= 1:
        return f"{share:.1f}%"
    if share >= 0.005:
        return f"{share:.2f}%"
    return "<0.01%"


def token_economics_card(
    *,
    categories: Sequence[EconomicsCategory],
    total_tokens: float,
    total_cost: float,
    inner_width: int,
    estimated: bool,
    missing_cache_rate: bool,
    local_tokens: int,
    colored: bool,
) -> TokenCard:
    """Format token volume against list-rate spend from explicit economics data."""
    rows = sorted(categories, key=lambda row: (row.cost, row.tokens), reverse=True)
    chart: list[StyledLine] = []
    if inner_width >= 34:
        for caption, values, total, figure in (
            (
                "share of tokens used",
                [row.tokens for row in rows],
                total_tokens,
                human_tokens(int(total_tokens)),
            ),
            ("share of dollars billed", [row.cost for row in rows], total_cost, money(total_cost)),
        ):
            if chart:
                chart.append(StyledLine(""))
            chart.append(
                StyledLine(
                    caption + " " * max(1, inner_width - len(caption) - len(figure)) + figure
                )
            )
            segments = [(row.label, value, row.slot) for row, value in zip(rows, values)]
            chart.append(stack_line(segments, total, inner_width, colored=colored))
        chart.append(StyledLine(""))
        chart.extend(
            legend_lines([(row.label, row.slot) for row in rows], inner_width, colored=colored)
        )

    type_w = max(max((len(row.label) for row in rows), default=4), len("TOTAL"))
    cost_w = max(8, *(len(money(row.cost)) for row in rows), len(money(total_cost)) + 1)
    header = f"  {'Type':<{type_w}}  {'Tokens':>8}  {'Volume':>6}  {'Cost':>{cost_w}}  {'Spend':>6}"
    table = [StyledLine(header)]
    table.extend(
        StyledLine(
            f"  {row.label:<{type_w}}  {human_tokens(int(row.tokens)):>8}  "
            f"{_share_text(row.tokens, total_tokens):>6}  {money(row.cost):>{cost_w}}  "
            f"{_share_text(row.cost, total_cost):>6}"
        )
        for row in rows
    )
    approx = "~" if estimated else ""
    total_row = StyledLine(
        f"  {'TOTAL':<{type_w}}  {human_tokens(int(total_tokens)):>8}  "
        f"{'':>6}  {approx + money(total_cost):>{cost_w}}"
    )
    notes = []
    if estimated:
        notes.append("! ~ a model here has no known list rate — its tokens use a generic estimate")
    if missing_cache_rate:
        notes.append(
            "! a model here has no cache-read rate on file — its reads price at $0, "
            "so Cache read is understated"
        )
    if local_tokens:
        notes.append(
            f"! {human_tokens(local_tokens)} local-model tokens excluded — no API rate to price them at"
        )
    return TokenCard(
        f"# Token economics · {approx}{money(total_cost)} at list rates",
        (tuple(chart), tuple(table), (total_row,)),
        tuple(notes),
        header,
    )
