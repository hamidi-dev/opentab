"""Pure Subagents layouts built from explicit node and flame projections."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

from opentab.formatting import human_tokens, money, pad, pct, shorten, wrap_lines
from opentab.tui.components.bars import (
    StyleSpan,
    legend_lines,
    positioned_label_line,
    stack_line,
    stack_widths,
)
from opentab.tui.components.boxes import BOX_CHROME, ruled_box, sectioned_box
from opentab.tui.trace import format_block
from opentab.util import node_1h_write

FLAME_LEGEND_MAX = 6
FLAME_MIN_INNER = 30


@dataclass(frozen=True)
class SubagentLayout:
    lines: tuple[str, ...]
    headers: tuple[str, ...] = ()
    sort_headers: tuple[tuple[int, tuple, str], ...] = ()
    row_map: tuple[tuple[int, int], ...] = ()
    cursor_line: int | None = None
    selected_node_index: int | None = None
    token_spans: tuple[tuple[str, tuple[StyleSpan, ...]], ...] = ()


def flame_pct(frac: float) -> str:
    """Round shares like the web frontend without rounding visible edges to 0/100."""
    if frac >= 1:
        return "100%"
    if frac <= 0:
        return "0%"
    share = 100.0 * frac
    if share >= 99.5:
        return ">99%"
    if share < 0.5:
        return "<1%"
    return f"{math.floor(share + 0.5):.0f}%"


def legend_names(segments: Sequence[Any], with_model: bool = False) -> list[str]:
    out: list[str] = []
    used: dict[str, int] = {}
    for segment in segments:
        name = shorten(segment.label, 24)
        if with_model and segment.model:
            name += f" {segment.model}"
        used[name] = seen = used.get(name, 0) + 1
        out.append(name if seen == 1 else f"{name}·{seen}")
    return out


def flame_label_line(
    segments: Sequence[Any], widths: Sequence[int], text_of: Callable[[Any], str]
) -> tuple[str, list[int], tuple[StyleSpan, ...]]:
    line, placed = positioned_label_line(
        [(str(text_of(segment) or ""), segment.slot) for segment in segments], widths
    )
    return line.text, placed, line.spans


def flamegraph_layout(
    flame: Any,
    width: int,
    *,
    glyphs: Mapping[str, str],
    colored: bool,
    api_prices_key: str,
) -> SubagentLayout:
    if flame is None or not flame.segments:
        return SubagentLayout(())
    inner = max(1, width - BOX_CHROME)
    dollars = flame.unit == "cost"

    def fmt(value: float) -> str:
        return money(value) if dollars else human_tokens(int(value))

    approx = "~" if flame.estimated else ""
    children = flame.children
    own = flame.total - sum(segment.value for segment in children)
    parts = [f"root kept {flame_pct(flame.self_share)} ({fmt(own)})"]
    if children:
        parts.append(
            f"{len(children)} subagent{'s' if len(children) != 1 else ''} "
            f"split {fmt(sum(segment.value for segment in children))}"
        )
        if len(children) > 1:
            parts.append(f"biggest {shorten(children[0].agent, 22)} {flame_pct(children[0].share)}")
    else:
        parts = [f"root kept all {approx}{fmt(flame.total)} — no subagent recorded a share"]
    head = [" · ".join(parts)]

    chart: list[str] = []
    named: list[int] = []
    spans: list[tuple[str, tuple[StyleSpan, ...]]] = []
    if inner >= max(FLAME_MIN_INNER, len(flame.segments)):
        caption = "session · width = " + ("dollars" if dollars else "tokens")
        if flame.one_model:
            caption += f" · all on {flame.one_model}"
        figure = approx + fmt(flame.total)
        chart.append(caption + " " * max(1, inner - len(caption) - len(figure)) + figure)
        rows = [(segment.label, segment.value, segment.slot) for segment in flame.segments]
        widths = stack_widths(rows, flame.total, inner)
        band = stack_line(
            rows,
            flame.total,
            inner,
            colored=colored,
            share_formatter=flame_pct,
        )
        chart.append(band.text)
        spans.append((band.text, band.spans))
        names, named, name_spans = flame_label_line(
            flame.segments, widths, lambda segment: segment.agent
        )
        if names:
            chart.append(names)
            spans.append((names, name_spans))
        if not flame.one_model:
            models, _placed, model_spans = flame_label_line(
                flame.segments, widths, lambda segment: segment.model
            )
            if models:
                chart.append(models)
                spans.append((models, model_spans))
        rest = [segment for index, segment in enumerate(flame.segments) if index not in set(named)]
        if rest:
            chart.append("")
            legend = rest[:FLAME_LEGEND_MAX]
            names_ = legend_names(legend, with_model=not flame.one_model)
            for line in legend_lines(
                [(name, segment.slot) for name, segment in zip(names_, legend)],
                inner,
                colored=colored,
            ):
                chart.append(line.text)
                spans.append((line.text, line.spans))

    notes = []
    unnamed = len(flame.segments) - len(named)
    if chart and unnamed > FLAME_LEGEND_MAX:
        omitted = unnamed - FLAME_LEGEND_MAX
        notes.append(
            f"· {omitted} thinner segment{'s' if omitted != 1 else ''} left out of the key — "
            "the table below names every execution"
        )
    if not dollars:
        notes.append(
            "! nothing here recorded a cost, so width is TOKENS — press "
            f"{api_prices_key} to divide list-price dollars instead"
        )
    elif flame.estimated:
        notes.append("! widths include list-price estimates for what recorded no cost")
    if flame.deep:
        notes.append(
            f"! {flame.deep} execution{'s' if flame.deep != 1 else ''} ran under another "
            "subagent (↳) — shown alongside, since the tree records depth but not parents"
        )
    if flame.silent:
        notes.append(
            f"· {flame.silent} subagent{'s' if flame.silent != 1 else ''} recorded no "
            f"{'spend' if dollars else 'tokens'} — no width to draw, still in the table below"
        )
    box = sectioned_box(
        f"# Where the money went · {approx}{fmt(flame.total)}",
        [head, chart],
        width,
        notes,
        glyphs,
    )
    return SubagentLayout(tuple(box.lines) + ("",), token_spans=tuple(spans))


def execution_table_layout(
    rows: Sequence[dict],
    width: int,
    *,
    offset: int,
    tree_cost: float,
    glyphs: Mapping[str, str],
    sort_headings: Mapping[str, str],
    sort_columns: tuple,
    selected_node_index: int | None,
    target: str = "",
    whatif_prices: Mapping[int, float] | None = None,
) -> SubagentLayout:
    whatif_prices = whatif_prices or {}
    columns = [("cost", "Cost", 9), ("tokens", "Tokens", 9)]
    if target:
        columns.insert(1, ("whatif", "What-if", 9))
    if width >= 90:
        columns = [("date", "Started", 16), ("depth", "D", 3), ("agent", "Agent", 12)] + columns
    if width >= 120:
        columns.insert(3, ("model", "Model", min(26, max(14, width - 116))))
    if width >= 145:
        columns += [("share", "Share", 6), ("cache", "Cache", 6)]
    title_width = max(1, width - BOX_CHROME - 2 - sum(size + 1 for _, _, size in columns))
    header = "  " + " ".join(
        pad(shorten(sort_headings.get(key, label), size), size) for key, label, size in columns
    )
    header += " " + sort_headings.get("title", "Title")
    body = []
    total_cost = sum(row["cost"] for row in rows)
    for row in rows:
        incoming = sum(
            row.get("tokens_" + key, 0) for key in ("input", "cache_read", "cache_write")
        )
        node_index = int(row["_node_index"])
        values = {
            "date": str(row.get("created_at") or "")[:16],
            "depth": str(row["depth"]),
            "agent": str(row.get("agent") or "unknown"),
            "model": str(row.get("model_name") or "unknown"),
            "cost": money(row["cost"]),
            "tokens": human_tokens(row["tokens_total"]),
            "whatif": money(whatif_prices.get(node_index, 0.0)) if target else "",
            "share": pct(row["cost"], tree_cost),
            "cache": pct(row.get("tokens_cache_read", 0), incoming),
        }
        body.append(
            "  "
            + " ".join(
                f"{values[key]:>{size}}"
                if key in ("cost", "tokens", "whatif", "share", "cache")
                else pad(shorten(values[key], size), size)
                for key, _label, size in columns
            )
            + " "
            + shorten(str(row.get("title") or "(untitled)"), title_width)
        )
    title = f"# Session Tree · what-if {target}" if target else "# Subagent Executions"
    total = None
    if len(rows) > 1 and not target:
        values = {
            "cost": money(total_cost),
            "tokens": human_tokens(sum(row["tokens_total"] for row in rows)),
        }
        total = "  " + " ".join(
            f"{values.get(key, ''):>{size}}"
            if key in values
            else pad("TOTAL" if i == 0 else "", size)
            for i, (key, _label, size) in enumerate(columns)
        )
        if columns[0][0] == "cost":
            total += " TOTAL"
    box = ruled_box(title, header, body, total, [], width, glyphs)
    body_start = box.body_start or 0
    start = offset + body_start
    cursor = next((i for i, row in enumerate(rows) if row["_node_index"] == selected_node_index), 0)
    selected = int(rows[cursor]["_node_index"]) if rows else None
    headers = (box.lines[box.header_line],) if box.header_line is not None else ()
    return SubagentLayout(
        box.lines,
        headers=headers,
        sort_headers=((offset + (box.header_line or 0), sort_columns, "subagent"),),
        row_map=tuple((start + i, i) for i in range(len(rows))),
        cursor_line=start + cursor if rows else None,
        selected_node_index=selected,
    )


def _group_box(
    label: str,
    field: str,
    children: Sequence[dict],
    priced_nodes: Sequence[dict],
    width: int,
    glyphs: Mapping[str, str],
) -> tuple[list[str], str]:
    groups: dict[str, list[dict]] = {}
    for row in children:
        groups.setdefault(str(row.get(field) or "unknown"), []).append(row)
    extra = width >= 64
    name_width = max(8, width - BOX_CHROME - (42 if extra else 28))
    header = (
        f"  {pad(shorten(label, name_width), name_width)} {'Runs':>4} {'Cost':>9} {'Tokens':>9}"
    )
    if extra:
        header += f" {'Share':>6} {'Cache':>6}"
    tree_cost = sum(row["cost"] for row in priced_nodes)
    body = []
    for name, group in sorted(
        groups.items(), key=lambda item: sum(row["cost"] for row in item[1]), reverse=True
    ):
        group_cost = sum(row["cost"] for row in group)
        cache = sum(row.get("tokens_cache_read", 0) for row in group)
        incoming = sum(
            row.get("tokens_input", 0)
            + row.get("tokens_cache_read", 0)
            + row.get("tokens_cache_write", 0)
            for row in group
        )
        body.append(
            f"  {pad(shorten(name, name_width), name_width)} {len(group):>4} {money(group_cost):>9} "
            f"{human_tokens(sum(row['tokens_total'] for row in group)):>9}"
            + (f" {pct(group_cost, tree_cost):>6} {pct(cache, incoming):>6}" if extra else "")
        )
    box = ruled_box(f"# By {label.lower()}", header, body, None, [], width, glyphs)
    return list(box.lines), box.lines[box.header_line or 0]


def subagents_overview_layout(
    *,
    priced_nodes: Sequence[dict],
    rows: Sequence[dict],
    flame: Any,
    width: int,
    glyphs: Mapping[str, str],
    colored: bool,
    api_prices_key: str,
    select_key: str,
    sort_headings: Mapping[str, str],
    sort_columns: tuple,
    selected_node_index: int | None,
    target: str = "",
    whatif_totals: tuple[float, float] | None = None,
    whatif_prices: Mapping[int, float] | None = None,
    baseline_estimated: bool = False,
) -> SubagentLayout:
    whatif_prices = whatif_prices or {}
    children = [row for row in priced_nodes if row["depth"] > 0]
    if not children:
        return SubagentLayout(("# Subagents", "No subagents used in this workflow."))

    flame_layout = flamegraph_layout(
        flame, width, glyphs=glyphs, colored=colored, api_prices_key=api_prices_key
    )
    lines = wrap_lines(flame_layout.lines, width)
    summary = [
        f"{len(children)} executions   {sum(row['depth'] == 1 for row in children)} direct / "
        f"{sum(row['depth'] > 1 for row in children)} nested   max depth {max(row['depth'] for row in children)}",
        f"Delegated cost {money(sum(row['cost'] for row in children))} "
        f"({pct(sum(row['cost'] for row in children), sum(row['cost'] for row in priced_nodes))} of tree)   "
        f"tokens {human_tokens(sum(row['tokens_total'] for row in children))} "
        f"({pct(sum(row['tokens_total'] for row in children), sum(row['tokens_total'] for row in priced_nodes))})",
    ]
    lines.extend(
        sectioned_box(
            "# Delegation", [wrap_lines(summary, width - BOX_CHROME)], width, [], glyphs
        ).lines
    )
    lines.append("")

    table_offset = len(lines)
    table = execution_table_layout(
        rows,
        width,
        offset=table_offset,
        tree_cost=sum(row["cost"] for row in priced_nodes),
        glyphs=glyphs,
        sort_headings=sort_headings,
        sort_columns=sort_columns,
        selected_node_index=selected_node_index,
        target=target,
        whatif_prices=whatif_prices,
    )
    if target and whatif_totals is not None:
        table = whatif_footer_layout(
            table,
            rows=rows,
            target=target,
            totals=whatif_totals,
            whatif_prices=whatif_prices,
            baseline_estimated=baseline_estimated,
        )
    lines.extend(table.lines)

    lines.extend(
        wrap_lines(
            [
                f"{select_key} / click: inspect execution. Shares use node totals, which can differ from session rollups."
            ],
            width,
        )
    )
    headers = list(table.headers)
    for field, label in (("agent", "Agent"), ("model_name", "Representative model")):
        group_lines, header = _group_box(label, field, children, priced_nodes, width, glyphs)
        lines.append("")
        lines.extend(group_lines)
        headers.append(header)
    lines.extend(
        wrap_lines(
            [
                "Model groups use each execution's representative model, not an exact model split. Cache = reads / (input + cache reads + cache writes)."
            ],
            width,
        )
    )
    return SubagentLayout(
        tuple(lines),
        headers=tuple(headers),
        sort_headers=table.sort_headers,
        row_map=table.row_map,
        cursor_line=table.cursor_line,
        selected_node_index=table.selected_node_index,
        token_spans=flame_layout.token_spans,
    )


def subagent_detail_layout(
    row: dict,
    priced_nodes: Sequence[dict],
    width: int,
    *,
    glyphs: Mapping[str, str],
    back_key: str,
    select_key: str,
    turns_unavailable: str,
    prompt_text: str,
    cost_label: str,
    colored: bool,
    target: str = "",
    target_cost: float = 0.0,
) -> SubagentLayout:
    children = [node for node in priced_nodes if node["depth"] > 0]
    inner = max(1, width - BOX_CHROME)
    title = str(row.get("title") or "(untitled)")
    meta = [
        f"Agent: {row.get('agent') or 'unknown'}   Depth: {row['depth']}",
        f"Representative model: {row.get('model_name') or 'unknown'}",
        f"Started: {row.get('created_at') or 'not recorded'}",
    ]
    lines = wrap_lines([f"# Subagent execution   {back_key}: back to executions", ""], width)
    if turns_unavailable:
        lines.extend(wrap_lines([turns_unavailable, ""], width))
    elif select_key:
        lines.extend(wrap_lines([f"{select_key}: open this execution's turns", ""], width))
    lines.extend(sectioned_box("# Title", [wrap_lines([title], inner)], width, [], glyphs).lines)
    lines.append("")
    lines.extend(
        sectioned_box(
            "# Received prompt",
            [format_block(prompt_text, "", inner, len(prompt_text.splitlines()))],
            width,
            wrap_lines(
                [
                    "First recorded child user message, not its title or the full system/context payload."
                ],
                width,
            ),
            glyphs,
        ).lines
    )
    lines.append("")
    lines.extend(sectioned_box("# Execution", [wrap_lines(meta, inner)], width, [], glyphs).lines)

    total_cost = sum(node["cost"] for node in priced_nodes)
    child_cost = sum(node["cost"] for node in children)
    spend = [
        f"{cost_label} cost: {money(row['cost'])}",
        f"Share of tree: {pct(row['cost'], total_cost)} cost / "
        f"{pct(row['tokens_total'], sum(node['tokens_total'] for node in priced_nodes))} tokens",
    ]
    if row["depth"] > 0:
        rank = 1 + sum(node["cost"] > row["cost"] for node in children)
        spend.append(
            f"Delegated spend: {pct(row['cost'], child_cost)}   Cost rank: {rank} of {len(children)}"
        )
    if target:
        spend.append(f"All tokens at {target}: {money(target_cost)}")
    lines.append("")
    lines.extend(
        sectioned_box("# Contribution", [wrap_lines(spend, inner)], width, [], glyphs).lines
    )

    categories = (
        ("Input", "input", 0),
        ("Output", "output", 1),
        ("Reasoning", "reasoning", 2),
        ("Cache read", "cache_read", 3),
        ("Cache write", "cache_write", 4),
    )
    values = [(label, int(row.get("tokens_" + key, 0)), slot) for label, key, slot in categories]
    category_total = sum(value for _label, value, _slot in values)
    chart: list[str] = []
    spans: list[tuple[str, tuple[StyleSpan, ...]]] = []
    if category_total:
        band = stack_line(values, category_total, inner, colored=colored)
        chart.append(band.text)
        spans.append((band.text, band.spans))
        for line in legend_lines(
            [(label, slot) for label, _value, slot in values], inner, colored=colored
        ):
            chart.append(line.text)
            spans.append((line.text, line.spans))
    token_rows = [
        f"{label:<12} {value:>16,}  {pct(value, category_total):>6}"
        for label, value, _slot in values
    ]
    if node_1h_write(row):
        token_rows.append(f"  of writes, 1h: {node_1h_write(row):,}")
    token_rows.append(f"Recorded total: {row['tokens_total']:,}")
    incoming = sum(row.get("tokens_" + key, 0) for key in ("input", "cache_read", "cache_write"))
    token_rows.append(
        f"Cache hit: {pct(row.get('tokens_cache_read', 0), incoming)} of incoming tokens"
    )
    lines.append("")
    lines.extend(
        sectioned_box(
            "# Token breakdown", [chart, wrap_lines(token_rows, inner)], width, [], glyphs
        ).lines
    )
    lines.extend(
        wrap_lines(
            [
                "Shares use node totals, not session rollups. Token category shares use their sum; recorded totals can differ. The 1h cache-write count is a subset, not extra tokens.",
                "The model is representative: an execution can switch models. No per-node savings baseline, duration, status, or turn ownership is inferred.",
            ],
            width,
        )
    )
    return SubagentLayout(tuple(lines), token_spans=tuple(spans))


def whatif_footer_layout(
    table: SubagentLayout,
    *,
    rows: Sequence[dict],
    target: str,
    totals: tuple[float, float],
    whatif_prices: Mapping[int, float],
    baseline_estimated: bool,
) -> SubagentLayout:
    lines = list(table.lines)
    actual, total = totals
    saved = actual - total
    verb = "saved" if saved >= 0 else "cost more"
    approx = "~" if baseline_estimated else ""
    lines.extend(
        [
            "",
            f"TOTAL (list rates)  your models {approx}{money(actual)} → all at {target} {money(total)}   "
            f"{verb} {money(abs(saved))} ({pct(abs(saved), actual)})",
            "! Both sides priced at list rates — the only apples-to-apples basis. The Cost column is "
            "what was actually recorded ($0 where a subscription recorded none), so it does not add "
            "up to these.",
            "· No per-node Δ: a node can mix models, so its baseline isn't computable — the exact "
            "comparison exists at session level, where the tokens are split per model.",
        ]
    )
    if baseline_estimated:
        lines.append(
            "! ~ your models include one with no known list rate — its tokens are priced at a "
            "generic estimate, so the baseline is not a real list price."
        )
    column = sum(whatif_prices.get(int(row["_node_index"]), 0.0) for row in rows)
    if abs(column - total) > 0.01:
        direction = "more" if column > total else "less"
        lines.append(
            "! This session's node totals disagree with its message totals, so the What-if "
            f"column adds up to slightly {direction} than the TOTAL. The TOTAL is the exact one."
        )
    return SubagentLayout(
        tuple(lines),
        headers=table.headers,
        sort_headers=table.sort_headers,
        row_map=table.row_map,
        cursor_line=table.cursor_line,
        selected_node_index=table.selected_node_index,
        token_spans=table.token_spans,
    )
