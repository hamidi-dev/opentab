"""Pure Tools explorer presentation from an already-priced numeric projection."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from opentab.presentation.formatting import (
    clip,
    human_tokens,
    money,
    pad,
    pct,
    shorten,
    wrap_lines,
)
from opentab.presentation.heatmap import TOOL_HEAT_LEVELS
from opentab.tui.components.bars import StyleSpan
from opentab.tui.components.boxes import (
    BOX_CHROME,
    TABLE_GLYPHS,
    TABLE_GLYPHS_ASCII,
    BoxLayout,
    ruled_box,
    sectioned_box,
)
from opentab.tui.components.charts import treemap_rects
from opentab.tui.components.token_cards import token_breakdown_card
from opentab.util import short_tool_name

_TOOL_TILE_MIN = 12


@dataclass(frozen=True)
class ToolsOptions:
    drill: tuple[str, str] | None = None
    treemap_height: int | None = None
    supports_tools: bool = True
    has_tool_rows: bool = True
    supports_turns: bool = True
    select_label: str = "Enter"
    back_label: str = "Esc"
    api_price_label: str = "$"
    api_prices: bool = False
    demo: bool = False
    unicode: bool = True
    tool_heat_colored: bool = True
    token_series_colored: bool = True


@dataclass(frozen=True)
class ToolsLayout:
    lines: tuple[str, ...]
    box_headers: frozenset[str] = frozenset()
    row_map: Mapping[int, int] = field(default_factory=dict)
    call_map: Mapping[int, int] = field(default_factory=dict)
    token_spans: Mapping[str, tuple[StyleSpan, ...]] = field(default_factory=dict)
    heat_spans: Mapping[int, tuple[tuple[int, int, int], ...]] = field(default_factory=dict)


@dataclass
class _Build:
    lines: list[str] = field(default_factory=list)
    headers: set[str] = field(default_factory=set)
    row_map: dict[int, int] = field(default_factory=dict)
    call_map: dict[int, int] = field(default_factory=dict)
    token_spans: dict[str, tuple[StyleSpan, ...]] = field(default_factory=dict)
    heat_spans: dict[int, tuple[tuple[int, int, int], ...]] = field(default_factory=dict)

    def finish(self) -> ToolsLayout:
        return ToolsLayout(
            tuple(self.lines),
            frozenset(self.headers),
            dict(self.row_map),
            dict(self.call_map),
            dict(self.token_spans),
            dict(self.heat_spans),
        )


def heat_position(value: float, lo: float, hi: float, levels: int) -> int:
    """Place an orders-of-magnitude rate on a bounded logarithmic heat scale."""
    if not (hi > lo > 0) or value <= lo:
        return 0
    frac = (math.log(value) - math.log(lo)) / (math.log(hi) - math.log(lo))
    return max(0, min(levels - 1, round(frac * (levels - 1))))


def _glyphs(unicode: bool) -> dict[str, str]:
    return TABLE_GLYPHS if unicode else TABLE_GLYPHS_ASCII


def _add_ruled(
    build: _Build,
    title: str,
    header: str,
    body: Sequence[str],
    total: str | None,
    notes: Sequence[str],
    width: int,
    unicode: bool,
) -> tuple[int, int | None]:
    offset = len(build.lines)
    box = ruled_box(title, header, body, total, notes, width, _glyphs(unicode))
    build.lines.extend(box.lines)
    if box.header_line is not None:
        build.headers.add(box.lines[box.header_line])
    return offset, box.body_start


def _add_sectioned(
    build: _Build,
    title: str,
    groups: Sequence[Sequence[str]],
    width: int,
    notes: Sequence[str],
    unicode: bool,
) -> BoxLayout:
    box = sectioned_box(title, groups, width, notes, _glyphs(unicode))
    build.lines.extend(box.lines)
    return box


def tool_treemap_layout(
    bucket: Mapping[str, Mapping[str, float]], width: int, options: ToolsOptions
) -> ToolsLayout:
    build = _Build()
    costs = {name: float(item["cost"]) for name, item in bucket.items()}
    dollars = sum(costs.values()) > 0
    values = costs if dollars else {name: float(item["tokens"]) for name, item in bucket.items()}
    calls = {name: int(item.get("calls") or 0) for name, item in bucket.items()}
    ranked = sorted(
        ((name, value) for name, value in values.items() if value > 0),
        key=lambda row: (-row[1], row[0].lower()),
    )
    if not ranked:
        return build.finish()

    inner = max(1, width - BOX_CHROME)
    height = max(3, min(5, inner // 14))
    if options.treemap_height is not None:
        height = min(height, options.treemap_height)
    if height < 3:
        return build.finish()

    def fold(keep: int) -> list[tuple[str, float]]:
        head, tail = ranked[:keep], ranked[keep:]
        if not tail:
            return list(head)
        calls["Other"] = sum(calls.get(name, 0) for name, _ in tail)
        out = head + [("Other", sum(value for _, value in tail))]
        out.sort(key=lambda row: (-row[1], row[0].lower()))
        return out

    grand = sum(value for _, value in ranked)
    keep = 0
    while keep < min(8, len(ranked)):
        if ranked[keep][1] / grand * inner < _TOOL_TILE_MIN:
            break
        keep += 1
    ranked_all, ranked = ranked, fold(max(1, keep))
    all_rates = {name: value / calls[name] for name, value in ranked_all if calls.get(name)}
    by_rate = len(all_rates) == len(ranked_all) and max(all_rates.values()) > min(
        all_rates.values()
    )
    rate_lo = min(all_rates.values()) if by_rate else 0.0
    rate_hi = max(all_rates.values()) if by_rate else 0.0
    rates = {name: value / calls[name] for name, value in ranked if calls.get(name)}

    rects = treemap_rects(ranked, inner, height)
    total = sum(value for _, value in ranked)
    peak = max(value for _, value in ranked)
    glyphs = "░▒▓█" if options.unicode else ".:*#"
    grid = [[" " for _ in range(inner)] for _ in range(height)]
    row_runs: dict[int, list[tuple[int, int, int]]] = defaultdict(list)

    def put(y: int, x: int, text: str, room: int) -> None:
        for index, char in enumerate(clip(text, room)):
            if x + index < inner:
                grid[y][x + index] = char

    def rate_text(rate: float | None) -> str:
        if rate is None:
            return ""
        if not dollars:
            return f"{human_tokens(int(round(rate)))}/call"
        if rate >= 0.01:
            return f"{money(rate)}/call"
        return "<$0.0001/call" if rate < 0.0001 else f"${rate:.4f}".rstrip("0") + "/call"

    for name, value, x, y, tile_width, tile_height in rects:
        drawn_width = tile_width if x + tile_width >= inner else max(1, tile_width - 1)
        drawn_height = tile_height if y + tile_height >= height else max(1, tile_height - 1)
        level = (
            heat_position(rates[name], rate_lo, rate_hi, TOOL_HEAT_LEVELS)
            if by_rate
            else max(
                0,
                min(
                    TOOL_HEAT_LEVELS - 1,
                    round(math.sqrt(value / peak) * (TOOL_HEAT_LEVELS - 1)),
                ),
            )
        )
        fill = (
            " "
            if options.tool_heat_colored
            else glyphs[min(len(glyphs) - 1, level * len(glyphs) // TOOL_HEAT_LEVELS)]
        )
        for yy in range(y, min(height, y + drawn_height)):
            for xx in range(x, min(inner, x + drawn_width)):
                grid[yy][xx] = fill
            row_runs[yy].append((x, drawn_width, level))

        inset = 1 if drawn_width >= 4 else 0
        room = drawn_width - inset * 2
        if room >= 4 and drawn_height >= 2:
            put(y, x + inset, shorten(name, room), room)
            metric = money(value) if dollars else human_tokens(int(value))
            stat = f"{metric} · {pct(value, total)}"
            if len(stat) <= room:
                put(y + 1, x + inset, stat, room)
            rate = rate_text(rates.get(name))
            count = calls.get(name) or 0
            both = f"{rate} · {count} call{'s' if count != 1 else ''}"
            if drawn_height >= 3 and rate:
                for candidate in (both, rate):
                    if len(candidate) <= room:
                        put(y + 2, x + inset, candidate, room)
                        break

    chart = ["".join(row) for row in grid]
    area_unit = "visible cost" if dollars else "tokens (no recorded cost)"
    caption = (
        f"area = {area_unit} · shade = {'$' if dollars else 'tokens'}/call"
        if by_rate
        else f"area + shade = {area_unit}"
    )
    total_label = money(total) if dollars else f"{human_tokens(int(total))} tokens"
    top_name, top_value = ranked_all[0]
    of_what = "the spend" if dollars else "the tokens"
    headline = [
        f"{shorten(top_name, 22)} is {pct(top_value, sum(value for _, value in ranked_all))} of {of_what}"
    ]
    if calls.get(top_name):
        headline[0] += f", over {calls[top_name]} calls"
    if len(all_rates) > 1:
        hot = max(all_rates, key=lambda name: all_rates[name])
        if hot != top_name and all_rates[top_name] > 0:
            headline.append(
                f"priciest per call is {shorten(hot, 22)} at {rate_text(all_rates[hot])}"
                f" — {all_rates[hot] / all_rates[top_name]:.0f}× {shorten(top_name, 22)}'s"
            )
        elif hot == top_name:
            headline.append(f"and the priciest per call, at {rate_text(all_rates[hot])}")
    notes = []
    if not dollars:
        if options.api_prices and not options.demo:
            notes.append("! no tool-attributed tokens here have a list price — area stays TOKENS")
        else:
            notes.append(
                "! nothing here recorded a cost, so area is TOKENS — press "
                f"{options.api_price_label} for list-price spend"
            )
    joined = " · ".join(headline)
    box = _add_sectioned(
        build,
        f"# Tool-attributed spend · {total_label}",
        [[joined] if len(joined) <= inner else headline, [caption, *chart]],
        width,
        notes,
        options.unicode,
    )
    chart_at = len(box.lines) - len(notes) - 1 - len(chart)
    build.heat_spans = {
        chart_at + row: tuple((column + 2, length, level) for column, length, level in runs)
        for row, runs in row_runs.items()
    }
    build.lines.append("")
    return build.finish()


def _add_ranking(
    build: _Build,
    rows: Sequence[dict],
    title: str,
    width: int,
    ordinal: int,
    options: ToolsOptions,
) -> None:
    display_rows = list(rows)
    if len(rows) > 1:
        display_rows.append(
            {
                "name": "TOTAL",
                **{key: sum(row[key] for row in rows) for key in ("calls", "cost", "tokens_total")},
            }
        )
    inner = max(1, width - BOX_CHROME)
    calls_width = max(5, len(f"{sum(row['calls'] for row in rows):,}"))
    cost_width = max(4, max((len(money(float(row["cost"]))) for row in display_rows), default=0))
    average_width = max(
        6,
        max(
            (len(money(float(row["cost"]) / row["calls"])) for row in display_rows if row["calls"]),
            default=0,
        ),
    )
    token_width = max(
        6,
        max((len(human_tokens(int(row["tokens_total"]))) for row in display_rows), default=0),
    )
    show_tokens = inner >= calls_width + cost_width + token_width + 19
    show_average = inner >= calls_width + cost_width + token_width + average_width + 20
    tail = (
        calls_width
        + cost_width
        + 2
        + (token_width + 1 if show_tokens else 0)
        + (average_width + 1 if show_average else 0)
    )
    name_width = max(4, inner - tail - 2)
    header = f"  {pad('Name', name_width)} {'Calls':>{calls_width}}"
    if show_average:
        header += f" {'$/call':>{average_width}}"
    if show_tokens:
        header += f" {'Tokens':>{token_width}}"
    header += f" {'Cost':>{cost_width}}"
    body = []
    for row in display_rows:
        average = row["cost"] / row["calls"] if row["calls"] else 0
        body.append(
            f"  {pad(shorten(str(row['name']), name_width), name_width)} {row['calls']:>{calls_width},}"
            + (f"{money(average):>{average_width + 1}}" if show_average else "")
            + (
                f"{human_tokens(int(row['tokens_total'])):>{token_width + 1}}"
                if show_tokens
                else ""
            )
            + f" {money(row['cost']):>{cost_width}}"
        )
    total = body.pop() if len(display_rows) > len(rows) else None
    offset, body_start = _add_ruled(build, title, header, body, total, [], width, options.unicode)
    if body_start is not None:
        for index in range(len(rows)):
            build.row_map[offset + body_start + index] = ordinal + index


def tool_ranking_layout(
    rows: Sequence[dict], title: str, width: int, ordinal: int, options: ToolsOptions
) -> ToolsLayout:
    """Build one selectable ranking box with local row ordinals."""
    build = _Build()
    _add_ranking(build, rows, title, width, ordinal, options)
    return build.finish()


def _add_token_breakdown(
    build: _Build, usage: dict, title: str, width: int, options: ToolsOptions
) -> None:
    inner = max(1, width - BOX_CHROME)
    card = token_breakdown_card(
        title=title,
        inner_width=inner,
        note_width=width,
        input_tokens=usage.get("input") or 0,
        output_tokens=usage.get("output") or 0,
        reasoning_tokens=usage.get("reasoning") or 0,
        cache_read_tokens=usage.get("cache_read") or 0,
        cache_write_tokens=usage.get("cache_write") or 0,
        cache_write_1h=usage.get("cache_write_1h") or 0,
        recorded_total=usage.get("tokens_total") or 0,
        attributed=True,
        calls=int(usage["calls"]),
        colored=options.token_series_colored,
    )
    groups = []
    for group in card.groups:
        text_group = []
        for line in group:
            if line.spans:
                build.token_spans[line.text] = line.spans
            text_group.append(line.text)
        groups.append(text_group)
    _add_sectioned(build, card.title, groups, width, card.notes, options.unicode)


def _build_detail(build: _Build, projection: dict, width: int, options: ToolsOptions) -> None:
    drill = options.drill
    if drill is None:
        return
    ranking = next(row for row in projection["rankings"] if (row["kind"], row["name"]) == drill)
    calls = [
        row
        for row in projection["calls"]
        if (row.get("tool") if drill[0] == "tool" else row.get("namespace")) == drill[1]
    ]
    all_tools = [row for row in projection["rankings"] if row["kind"] == "tool"]
    total_cost = sum(row["cost"] for row in all_tools)
    total_tokens = sum(row["tokens_total"] for row in all_tools)
    build.lines.extend(
        wrap_lines(
            [
                f"# {drill[0].capitalize()} · {drill[1]}   {options.back_label}: back to rankings",
                "",
            ],
            width,
        )
    )
    average_cost = ranking["cost"] / ranking["calls"] if ranking["calls"] else 0
    average_tokens = ranking["tokens_total"] / ranking["calls"] if ranking["calls"] else 0
    _add_sectioned(
        build,
        "# Contribution",
        [
            wrap_lines(
                [
                    f"{ranking['calls']:,} calls   cost {money(ranking['cost'])} ({pct(ranking['cost'], total_cost)})   tokens {human_tokens(int(ranking['tokens_total']))} ({pct(ranking['tokens_total'], total_tokens)})",
                    f"Per call: {money(average_cost)}   {human_tokens(int(average_tokens))} attributed tokens",
                ],
                width - BOX_CHROME,
            )
        ],
        width,
        [],
        options.unicode,
    )
    build.lines.append("")
    _add_token_breakdown(build, ranking, "# Attributed token categories", width, options)

    models = projection["models"].get(drill, {})
    model_rows = []
    wide = width >= 116
    inner = max(1, width - BOX_CHROME)
    model_cost_width = max(
        4, max((len(money(float(item["cost"]))) for item in models.values()), default=0)
    )
    show_total_tokens = wide or inner >= model_cost_width + 30
    name_width = max(
        4,
        inner
        - (
            70 + model_cost_width
            if wide
            else 9 + model_cost_width + (10 if show_total_tokens else 0)
        ),
    )
    header = f"  {pad('Model', name_width)} {'Calls':>5}"
    if show_total_tokens:
        header += f" {'Tokens':>9}"
    if wide:
        header += f" {'Input':>9} {'Model out':>9} {'Reason':>9} {'CacheR':>9} {'CacheW':>9}"
    header += f" {'Cost':>{model_cost_width}}"
    sorted_models = sorted(
        models.items(), key=lambda item: (item[1]["cost"], item[1]["tokens_total"]), reverse=True
    )
    for model, item in sorted_models:
        line = f"  {pad(shorten(model, name_width), name_width)} {item['calls']:>5}"
        if show_total_tokens:
            line += f" {human_tokens(int(item['tokens_total'])):>9}"
        if wide:
            line += " " + " ".join(
                f"{human_tokens(int(item[key])):>9}"
                for key in ("input", "output", "reasoning", "cache_read", "cache_write")
            )
        line += f" {money(item['cost']):>{model_cost_width}}"
        model_rows.append(line)
    build.lines.append("")
    _add_ruled(
        build,
        "# Exact attributed usage by model",
        header,
        model_rows,
        None,
        [],
        width,
        options.unicode,
    )
    if not wide:
        for model, item in sorted_models:
            split = (
                f"  {shorten(model, 28)}: input {human_tokens(int(item['input']))} · "
                f"model output {human_tokens(int(item['output']))} · "
                f"reasoning {human_tokens(int(item['reasoning']))} · "
                f"cache read {human_tokens(int(item['cache_read']))} · "
                f"cache write {human_tokens(int(item['cache_write']))}"
            )
            if item["cache_write_1h"]:
                split += f" (1h {human_tokens(int(item['cache_write_1h']))}, subset)"
            build.lines.extend(wrap_lines([split], width))

    aggregate_calls = int(ranking["calls"])
    recovered_calls = len(calls)
    recovered_tokens = sum(float(call.get("tokens_total") or 0) for call in calls)
    recovered_cost = sum(float(call.get("cost") or 0) for call in calls)
    call_state = "complete" if recovered_calls == aggregate_calls else "partial"
    token_state = (
        "complete" if abs(recovered_tokens - ranking["tokens_total"]) < 0.01 else "partial"
    )
    cost_state = "complete" if abs(recovered_cost - float(ranking["cost"])) < 0.00005 else "partial"
    ledger = (
        f"Ledger coverage — calls {call_state}: {recovered_calls}/{aggregate_calls}; "
        f"tokens {token_state}: {human_tokens(round(recovered_tokens))}/{human_tokens(round(ranking['tokens_total']))}; "
        f"cost {cost_state}: {money(recovered_cost)}/{money(float(ranking['cost']))}."
    )
    if not options.supports_turns:
        ledger = f"Call ledger unavailable: aggregate source reports {aggregate_calls} calls but has no Turns timeline."
    build.lines.extend(
        wrap_lines(["", ledger, "Timestamps below are owning-turn timestamps."], width)
    )
    if not calls:
        return

    inner = max(1, width - BOX_CHROME)
    call_cost_width = max(4, max(len(money(float(call.get("cost") or 0))) for call in calls))
    call_token_width = max(
        6, max(len(human_tokens(int(call.get("tokens_total") or 0))) for call in calls)
    )
    room = inner - (13 + call_token_width + call_cost_width)
    model_width = min(24, max(0, room)) if room >= 11 else 0
    room -= model_width + (1 if model_width else 0)
    time_width = 8 if room >= 9 else 0
    room -= time_width + (1 if time_width else 0)
    agent_width = min(12, room - 1) if room >= 9 else 0
    room -= agent_width + (1 if agent_width else 0)
    tool_width = min(24, room - 1) if drill[0] == "namespace" and room >= 9 else 0
    header = f"  {'#':>3} {'Turn':>5}"
    if time_width:
        header += f" {'Time':<{time_width}}"
    if tool_width:
        header += f" {pad('Tool', tool_width)}"
    if model_width:
        header += f" {pad('Model', model_width)}"
    if agent_width:
        header += f" {pad('Agent', agent_width)}"
    header += f" {'Tokens':>{call_token_width}} {'Cost':>{call_cost_width}}"
    body = []
    for index, call in enumerate(calls, start=1):
        raw_time = str(call.get("time") or "")
        time = (
            raw_time[:19] if time_width == 19 else raw_time[11:19] if len(raw_time) >= 19 else "-"
        )
        agent = ("↳ " if call.get("depth") else "") + str(call.get("agent") or "-")
        line = f"  {index:>3} {int(call['turn_index']) + 1:>5}"
        if time_width:
            line += f" {time:<{time_width}}"
        if tool_width:
            line += f" {pad(shorten(short_tool_name(str(call['tool'])), tool_width), tool_width)}"
        if model_width:
            line += f" {pad(shorten(str(call.get('model_name') or 'unknown'), model_width), model_width)}"
        if agent_width:
            line += f" {pad(shorten(agent, agent_width), agent_width)}"
        line += f" {human_tokens(int(call.get('tokens_total') or 0)):>{call_token_width}} {money(float(call.get('cost') or 0)):>{call_cost_width}}"
        body.append(line)
    build.lines.append("")
    offset, body_start = _add_ruled(
        build, "# Calls — chronological", header, body, None, [], width, options.unicode
    )
    if body_start is not None:
        build.call_map = {offset + body_start + index: index for index in range(len(calls))}
    build.lines.extend(
        wrap_lines(
            [
                f"{options.select_label} / double-click opens the owning prompt's turn list; opening raw trace content remains a separate explicit {options.select_label}. Model output is attributed LLM output, never tool-result bytes."
            ],
            width,
        )
    )


def build_tools_layout(projection: dict | None, width: int, options: ToolsOptions) -> ToolsLayout:
    """Build all Tools text and local paint/interaction metadata."""
    if not options.supports_tools:
        return ToolsLayout(("# Tools", "This session's tool doesn't record per-tool attribution."))
    if not options.has_tool_rows:
        return ToolsLayout(("# Tools", "No tool calls recorded for this session."))
    if projection is None:
        raise ValueError("a numeric projection is required when tool rows are available")

    build = _Build()
    if options.drill is not None:
        _build_detail(build, projection, width, options)
        return build.finish()

    rankings = projection["rankings"]
    tools = [row for row in rankings if row["kind"] == "tool"]
    namespaces = [row for row in rankings if row["kind"] == "namespace"]
    by_tool = {
        row["name"]: {
            "calls": row["calls"],
            "cost": row["cost"],
            "tokens": row["tokens_total"],
        }
        for row in tools
    }
    calls = sum(row["calls"] for row in tools)
    cost = sum(row["cost"] for row in tools)
    overview = [
        f"{calls:,} calls   {len(tools)} tools   {len(namespaces)} namespaces",
        f"Attributed cost {money(cost)}   {money(cost / calls) if calls else '-'} / call   "
        f"{human_tokens(int(sum(row['tokens_total'] for row in tools) / calls)) if calls else '-'} tokens / call",
    ]
    tree = tool_treemap_layout(by_tool, width, options)
    build.lines.extend(tree.lines)
    build.heat_spans.update(tree.heat_spans)
    _add_sectioned(
        build,
        "# Tool ledger",
        [wrap_lines(overview, width - BOX_CHROME)],
        width,
        [],
        options.unicode,
    )
    build.lines.append("")
    _add_ranking(build, tools, "# Tools — this session", width, 0, options)
    build.lines.append("")
    _add_ranking(build, namespaces, "# By server / namespace", width, len(tools), options)
    build.lines.extend(
        wrap_lines(
            [
                "",
                f"{options.select_label} / double-click inspects a tool or namespace. Tokens and cost belong to the LLM turns that invoked calls, split across every call; they are not tool-result size.",
            ],
            width,
        )
    )
    return build.finish()
