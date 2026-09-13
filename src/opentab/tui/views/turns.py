"""Pure layouts for the Turns prompt, drill, and trace readers."""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from opentab.presentation.formatting import (
    cost_bar,
    display_width,
    human_duration,
    human_tokens,
    money,
    pad,
    pct,
    shorten,
)
from opentab.presentation.heatmap import BLOCKS_UP
from opentab.tui.components.boxes import BOX_CHROME, ruled_box, sectioned_box
from opentab.tui.components.token_cards import token_breakdown_card
from opentab.tui.trace import TraceLine, build_event_body, wrapped
from opentab.util import (
    TRACE_EVENTS_CAP,
    agent_mix_label,
    cached_share,
    context_size,
    tool_call_label,
    tool_mix_label,
    tool_names,
)

TokenRuns = dict[str, list[tuple[int, int, int]]]


@dataclass(frozen=True)
class TurnLayout:
    lines: list[str]
    box_headers: set[str]
    row_map: dict[int, int]
    cursor_lines: dict[int, int]
    token_runs: TokenRuns


@dataclass(frozen=True)
class TraceLayout:
    lines: list[str]
    tool_lines: dict[int, int]
    output_ends: list[tuple[int, int]]
    token_runs: TokenRuns


def turn_read_mark(row: Mapping) -> str:
    kinds = []
    if row.get("has_reasoning"):
        kinds.append("Thinking")
    if row.get("has_text"):
        kinds.append("Text")
    calls = tool_call_label(row.get("tools"))
    if calls:
        kinds.append(calls)
    return " · ".join(kinds)


def turn_agent(row: Mapping) -> str:
    label = (row.get("agent") or "-").strip() or "-"
    return f"↳ {label}" if row.get("depth") else label


def turn_group_rows(rows: Sequence[Mapping], costs: Sequence[float]) -> list[dict]:
    """Aggregate consecutive prompt-id runs in chronological order."""
    groups: list[dict] = []
    last = object()
    for index, (row, cost) in enumerate(zip(rows, costs)):
        prompt_id = row.get("prompt_id", "")
        if prompt_id != last:
            last = prompt_id
            groups.append(
                {
                    "id": prompt_id,
                    "title": (row.get("prompt_title") or "").strip(),
                    "full": (row.get("prompt_full") or row.get("prompt_title") or "").strip(),
                    "time": row.get("time") or "",
                    "turns": 0,
                    "tokens": 0,
                    "cost": 0.0,
                    "indices": [],
                    "calls": 0,
                    "subturns": 0,
                    "_rows": [],
                    "_first": None,
                }
            )
        group = groups[-1]
        group["turns"] += 1
        group["tokens"] += int(row.get("tokens_total") or 0)
        group["cost"] += cost
        group["indices"].append(index)
        group["calls"] += len(tool_names(row.get("tools")))
        group["subturns"] += 1 if row.get("depth") else 0
        group["_rows"].append(row)
        if not row.get("depth") and group["_first"] is None:
            group["_first"] = row
    for group in groups:
        first = group.pop("_first")
        group["cached"] = cached_share(first) if first is not None else None
        group["tools"] = tool_mix_label(group["_rows"])
        group["agents"] = agent_mix_label(group.pop("_rows"))
    return groups


def turn_metric_strips(
    rows: Sequence[Mapping],
    costs: Sequence[float],
    width: int,
    context_curve: bool,
    *,
    unit: str = "turn",
    first_index: int = 1,
) -> list[str]:
    """Build multi-row bars with shared index buckets and separate scales."""
    count = len(rows)
    if not count:
        return []
    contexts = [None if row.get("depth") else context_size(row) or None for row in rows]
    metrics: list[tuple[str, Sequence[float | None], str, int]] = [
        ("cost", list(costs), f"peak {unit} {money(max(costs, default=0.0))}", 3)
    ]
    if context_curve and any(value is not None for value in contexts):
        peak_context = max(value for value in contexts if value is not None)
        metrics.append(("context", contexts, f"peak {human_tokens(peak_context)}", 5))

    gutter = 9
    tail_width = max(len(tail) for _label, _values, tail, _height in metrics)
    plot_width = max(8, width - gutter - tail_width - 3)
    repeat = max(1, min(4, plot_width // count))
    left, right = f"{unit} {first_index}", str(first_index + count - 1)
    columns = min(plot_width, max(count * repeat, len(left) + len(right) + 1))

    def buckets(values) -> list[float | None]:
        output = []
        for column in range(columns):
            lo = column * count // columns
            hi = max(lo + 1, (column + 1) * count // columns)
            present = [value for value in values[lo:hi] if value is not None]
            output.append(max(present) if present else None)
        return output

    lines = []
    for label, values, tail, height in metrics:
        if lines:
            lines.append(" " * gutter + "│")
        values = buckets(values)
        peak = max((value for value in values if value is not None), default=0.0)
        levels = [
            max(1, round(value / peak * height * 8))
            if value is not None and value > 0 and peak > 0
            else 0
            for value in values
        ]
        for row_index in range(height):
            cells = []
            for level in levels:
                fill = max(0, min(8, level - (height - row_index - 1) * 8))
                cells.append("█" if fill == 8 else BLOCKS_UP[fill])
            name = label if row_index == 0 else ""
            suffix = f"  {tail:>{tail_width}}" if row_index == 0 else ""
            lines.append(f"{name:>{gutter}}│{''.join(cells)}{suffix}")
    lines.append(" " * gutter + "└" + "─" * columns)
    if len(left) + len(right) + 1 > columns:
        left = str(first_index)
    labels = left + " " * max(1, columns - len(left) - len(right)) + right
    lines.append(" " * (gutter + 1) + labels[:columns])
    return lines


def _token_box(
    usage: Mapping,
    title: str,
    width: int,
    glyphs: Mapping[str, str],
    *,
    colored: bool,
    notes: Sequence[str],
) -> tuple[list[str], TokenRuns]:
    card = token_breakdown_card(
        title=title,
        inner_width=max(1, width - BOX_CHROME),
        note_width=width,
        input_tokens=usage.get("input") or 0,
        output_tokens=usage.get("output") or 0,
        reasoning_tokens=usage.get("reasoning") or 0,
        cache_read_tokens=usage.get("cache_read") or 0,
        cache_write_tokens=usage.get("cache_write") or 0,
        cache_write_1h=usage.get("cache_write_1h") or 0,
        recorded_total=usage.get("tokens_total") or 0,
        notes=notes,
        colored=colored,
    )
    groups = []
    token_runs: TokenRuns = {}
    for group in card.groups:
        lines = []
        for line in group:
            if line.spans:
                token_runs[line.text] = [
                    (span.column, span.length, span.slot) for span in line.spans
                ]
            lines.append(line.text)
        groups.append(lines)
    layout = sectioned_box(card.title, groups, width, card.notes, glyphs)
    return list(layout.lines), token_runs


def build_turn_trace(
    *,
    rows: Sequence[Mapping],
    index: int,
    siblings: Sequence[int],
    events: list[dict],
    width: int,
    cost: float,
    glyphs: Mapping[str, str],
    colored: bool,
    scoped: bool,
    remote_machine: str | None,
    loading: bool,
    expanded: bool,
    open_outputs: frozenset[int],
    full_events: list[dict] | None,
    select_key: str,
    supports_trace: bool,
    unavailable_reason: str | None,
    remote_error: str | None,
    records_reasoning: bool,
) -> TraceLayout:
    """Present one selected turn; all source access and ownership stay outside."""
    if index not in siblings:
        return TraceLayout([], {}, [], {})
    row = rows[index]
    position = siblings.index(index) + 1
    prefix = f"Turn {position} of {len(siblings)}"
    if scoped:
        prefix = f"Execution turn {index + 1} · {position} of {len(siblings)} in prompt"
    prompt = " ".join(str(row.get("prompt_title") or "").split()) or "(no prompt)"
    room = max(12, width - display_width(prefix) - 3)
    head = f"{prefix} · {shorten(prompt, room)}"
    model = str(row.get("model_name") or "-").split("/", 1)[-1]
    meta = f"{model} · {human_tokens(row['tokens_total'])} tokens · {money(cost)} · {(row.get('time') or '--')[5:19]}"
    if row.get("depth"):
        meta += f" · {turn_agent(row)}"
    if remote_machine:
        meta += f" · SSH: {remote_machine}"
    lines: list[str] = [head, ""]
    token_lines, token_runs = _token_box(
        row,
        "# Turn token breakdown",
        width,
        glyphs,
        colored=colored,
        notes=(
            "· Uncached input is request input not served from cache.",
            "· A zero reasoning field may mean reasoning is included in model output.",
        ),
    )
    lines += token_lines
    lines.append("")
    if loading:
        if remote_machine:
            lines += [
                TraceLine(f"  Fetching turn over SSH: {remote_machine}", "meta"),
                TraceLine("  Close the trace to cancel.", "meta"),
            ]
        else:
            label = "full turn" if expanded else "output" if open_outputs else "turn"
            lines.append(TraceLine(f"  Loading {label} — reading recorded content…", "meta"))
        return TraceLayout(lines, {}, [], token_runs)
    lines += [TraceLine(line, "meta") for line in wrapped("", meta, "  ", width)] + [""]
    if not supports_trace:
        message = (
            f"Recorded trace unavailable: {unavailable_reason}"
            if unavailable_reason
            else "Recorded trace unavailable for this source; numeric usage is still available."
        )
        lines += [TraceLine(line, "meta") for line in wrapped("  ", message, "  ", width)]
        return TraceLayout(lines, {}, [], token_runs)
    if remote_machine and remote_error:
        lines += [
            TraceLine(f"  {remote_error}", "meta"),
            TraceLine("  Close and reopen the trace to retry.", "meta"),
        ]
        return TraceLayout(lines, {}, [], token_runs)
    if not events:
        lines.append("  No content recorded for this turn.")
        return TraceLayout(lines, {}, [], token_runs)
    body = build_event_body(
        events,
        width,
        line_offset=len(lines),
        expanded=expanded,
        open_outputs=open_outputs,
        full_events=full_events,
        select_key=select_key,
    )
    lines += body.lines
    while lines and not lines[-1]:
        lines.pop()
    if not expanded and len(events) >= TRACE_EVENTS_CAP:
        lines += [
            "",
            TraceLine(
                f"· Preview limited to {TRACE_EVENTS_CAP} events; expand to read all.", "meta"
            ),
        ]
    if (
        not remote_machine
        and not records_reasoning
        and not any(event.get("kind") == "reasoning" for event in events)
    ):
        lines += wrapped(
            "· ",
            "This harness records no reasoning text; its thinking blocks are empty.",
            "  ",
            width,
        )
    return TraceLayout(lines, body.tool_lines, body.output_ends, token_runs)


def build_turn_drill(
    *,
    rows: Sequence[Mapping],
    costs: Sequence[float],
    groups: Sequence[dict],
    drill: int,
    width: int,
    context_curve: bool,
    traceable: bool,
    scoped: bool,
    glyphs: Mapping[str, str],
    colored: bool,
) -> TurnLayout:
    group = groups[drill]
    number = drill + 1
    label = "Execution turns" if scoped else "Turns"
    share = group["cached"]
    lines: list[str] = [
        f"# {label} · prompt {number} of {len(groups)} — {group['turns']} turn"
        f"{'' if group['turns'] == 1 else 's'} · {human_tokens(group['tokens'])} · "
        f"{money(group['cost'])} · cached {'-' if share is None else f'{share * 100:.0f}%'}",
        "",
    ]
    for paragraph in (group["full"] or "(no preceding prompt)").splitlines() or [""]:
        lines += textwrap.wrap(paragraph, max(20, width)) or [""]
    lines.append("")
    prompt_usage = {
        field: sum(rows[index].get(field) or 0 for index in group["indices"])
        for field in (
            "input",
            "output",
            "reasoning",
            "cache_read",
            "cache_write",
            "cache_write_1h",
            "tokens_total",
        )
    }
    token_lines, token_runs = _token_box(
        prompt_usage,
        "# Prompt token breakdown",
        width,
        glyphs,
        colored=colored,
        notes=(
            "· This sums the prompt's answering turns; it is not the typed prompt's token length.",
            "· A zero reasoning field may mean reasoning is included in model output.",
        ),
    )
    lines += token_lines
    lines.append("")
    lines += turn_metric_strips(
        [rows[index] for index in group["indices"]],
        [costs[index] for index in group["indices"]],
        width,
        context_curve,
        unit="execution turn" if scoped else "turn",
        first_index=group["indices"][0] + 1,
    )
    lines.append("")

    index_width = max(2, len(str(len(rows))))
    agent_width = min(
        12, max(5, max((len(turn_agent(rows[i])) for i in group["indices"]), default=5))
    )
    inner = max(1, width - BOX_CHROME - 2)
    efforts = {i: str(rows[i].get("effort") or "") for i in group["indices"]}
    effort_width = max((len(value) for value in efforts.values()), default=0)
    effort_width = min(7, max(len("Eff"), effort_width)) if any(efforts.values()) else 0
    reads = {i: turn_read_mark(rows[i]) for i in group["indices"]} if traceable else {}
    read_width = (
        max(7, max(map(display_width, reads.values()), default=0)) if any(reads.values()) else 0
    )
    fixed = (
        index_width + agent_width + 14 + 6 + 9 + 9 + 6 + (effort_width + 1 if effort_width else 0)
    )
    model_min = 12
    if read_width:
        read_width = min(read_width, 30, inner - fixed - model_min - 1)
        if read_width < 5:
            read_width = 0
    read_heading = "Content" if read_width >= 7 else "Read"
    if 0 < read_width < 8:
        reads = {
            i: "Both"
            if rows[i].get("has_text") and rows[i].get("has_reasoning")
            else "Think"
            if rows[i].get("has_reasoning")
            else "Text"
            if rows[i].get("has_text")
            else "Tools"
            if rows[i].get("tools")
            else ""
            for i in group["indices"]
        }
    fixed += read_width + 1 if read_width else 0
    model_width = max(model_min, min(30, inner - fixed))
    labels = {i: tool_call_label(rows[i].get("tools")) for i in group["indices"]}
    tools_width = inner - fixed - model_width - 1
    tools_width = tools_width if not traceable and any(labels.values()) and tools_width >= 10 else 0
    header = (
        f"  {'#':>{index_width}} {'Time':<14} {pad('Model', model_width)} "
        + (f"{pad('Eff', effort_width)} " if effort_width else "")
        + f"{pad('Agent', agent_width)} "
        + (f"{pad(read_heading, read_width)} " if read_width else "")
        + (f"{pad('Tools', tools_width)} " if tools_width else "")
        + f"{'Cached':>6} {'Tokens':>9} {'Cost':>9}"
    )
    body = []
    for index in group["indices"]:
        row = rows[index]
        cached = cached_share(row)
        model = row["model_name"]
        if display_width(model) > model_width:
            model = model.rsplit("/", 1)[-1]
        body.append(
            f"  {index + 1:>{index_width}} {(row.get('time') or '--')[5:19]:<14} "
            f"{pad(shorten(model, model_width), model_width)} "
            + (
                f"{pad(shorten(efforts[index] or '-', effort_width), effort_width)} "
                if effort_width
                else ""
            )
            + f"{pad(shorten(turn_agent(row), agent_width), agent_width)} "
            + (
                f"{pad(shorten(reads.get(index) or '-', read_width), read_width)} "
                if read_width
                else ""
            )
            + (
                f"{pad(shorten(labels[index] or '-', tools_width), tools_width)} "
                if tools_width
                else ""
            )
            + f"{('-' if cached is None else f'{cached * 100:.0f}%'):>6} "
            f"{human_tokens(row['tokens_total']):>9} {money(costs[index]):>9}"
        )
    total = None
    if len(body) > 1:
        total = (
            f"  {'':>{index_width}} {pad('TOTAL', 14)} {pad('', model_width)} "
            + (f"{pad('', effort_width)} " if effort_width else "")
            + f"{pad('', agent_width)} "
            + (f"{pad('', read_width)} " if read_width else "")
            + (
                f"{pad(shorten(tool_mix_label([rows[i] for i in group['indices']]), tools_width), tools_width)} "
                if tools_width
                else ""
            )
            + f"{'':>6} {human_tokens(group['tokens']):>9} {money(group['cost']):>9}"
        )
    prologue = len(lines)
    box = ruled_box(f"# {label} of prompt {number}", header, body, total, [], width, glyphs)
    lines += box.lines
    start = prologue + (box.body_start or 0)
    row_map = {start + offset: offset for offset in range(len(body))}
    return TurnLayout(
        lines,
        {box.lines[box.header_line]} if box.header_line is not None else set(),
        row_map,
        {ordinal: line for line, ordinal in row_map.items()},
        token_runs,
    )


def build_turns(
    *,
    rows: Sequence[Mapping],
    costs: Sequence[float],
    width: int,
    compactions: Mapping[int, tuple[int, int]],
    cache_events: Sequence,
    scoped: bool,
    glyphs: Mapping[str, str],
) -> TurnLayout:
    """Build the chronological prompt overview and its local interaction map."""
    groups = turn_group_rows(rows, costs)
    total_cost = sum(costs)
    comps = compactions
    late = {event.index: event for event in cache_events if event.cause == "waited"}
    switched = {event.index: event for event in cache_events if event.cause == "reasoning"}
    label = "Execution turns" if scoped else "Turns"
    head = f"# {label} — {len(groups)} prompts · {len(rows)} turns · {money(total_cost)}"
    if comps:
        freed = sum(before - after for before, after in comps.values())
        head += f" · ▼ {len(comps)} compaction{'s' if len(comps) > 1 else ''}"
        head += f", ~{human_tokens(freed)} freed"
    if late:
        burnt = sum(miss.cost for miss in late.values())
        head += f" · ❄ {len(late)} cache expir{'y' if len(late) == 1 else 'ies'}, {money(burnt)}"
    if switched:
        spent = sum(miss.cost for miss in switched.values())
        head += f" · ⚙ {len(switched)} effort switch{'' if len(switched) == 1 else 'es'}"
        head += f", {money(spent)}"

    index_width = max(2, len(str(len(groups))))
    time_width, turns_width, cached_width, token_width, cost_width = 11, 5, 6, 8, 9
    prompt_min = 20
    inner = max(1, width - BOX_CHROME)
    used = index_width + time_width + turns_width + cached_width + token_width + cost_width + 8
    cumulative_width = 14 if inner - used - 15 >= prompt_min else 0
    used += cumulative_width + (1 if cumulative_width else 0)
    total_calls = sum(group["calls"] for group in groups)
    calls_width = max(len("Calls"), len(str(total_calls)))
    calls_width = calls_width if total_calls and inner - used - calls_width - 1 >= prompt_min else 0
    used += calls_width + (1 if calls_width else 0)
    agent_cells = {
        index: ("↳ " + group["agents"] if group["agents"] else "-")
        for index, group in enumerate(groups)
    }
    agents_width = 0
    if any(group["subturns"] for group in groups):
        agents_width = min(22, max(12, max(map(len, agent_cells.values()))))
        if inner - used - agents_width - 1 < prompt_min:
            agents_width = 0
    used += agents_width + (1 if agents_width else 0)
    bar_width = 8 if inner - used - 9 >= prompt_min + 12 else 0
    used += bar_width + (1 if bar_width else 0)
    peak = max((group["cost"] for group in groups), default=0.0)
    prompt_width = max(prompt_min, inner - used)
    header = (
        f"  {'#':>{index_width}} {'Time':<{time_width}} {'Prompt':<{prompt_width}} {'Turns':>{turns_width}} "
        + (f"{'Calls':>{calls_width}} " if calls_width else "")
        + (f"{pad('Agents', agents_width)} " if agents_width else "")
        + f"{'Cached':>{cached_width}} {'Tokens':>{token_width}} {'Cost':>{cost_width}}"
        + (f" {'':<{bar_width}}" if bar_width else "")
        + (f" {'Cumulative':>{cumulative_width}}" if cumulative_width else "")
    )
    cumulative = 0.0
    body: list[str] = []
    cursor_rows = []
    for number, group in enumerate(groups, start=1):
        cumulative += group["cost"]
        for index in group["indices"]:
            compaction = comps.get(index)
            if compaction:
                before, after = compaction
                when = (rows[index].get("time") or "")[5:16]
                body.append(
                    f"▼ context compacted before turn {index + 1} · {when} — "
                    f"{human_tokens(before)} → {human_tokens(after)} "
                    f"(~{human_tokens(before - after)} freed)"
                )
            miss = late.get(index)
            if miss:
                body.append(
                    f"❄ cache expired — {human_duration(miss.idle)} idle, "
                    f"{human_tokens(miss.repaid)} bought again for {money(miss.cost)} "
                    f"(it lived {human_duration(miss.ttl)})"
                )
            effort = switched.get(index)
            if effort:
                body.append(
                    f"⚙ reasoning effort {effort.detail} — the cache went with it, "
                    f"{human_tokens(effort.repaid)} bought again for {money(effort.cost)}"
                )
        share = group["cached"]
        cached = "-" if share is None else f"{share * 100:.0f}%"
        title = " ".join((group["title"] or "").split()) or "(no preceding prompt)"
        cumulative_label = f"{money(cumulative)} · {pct(cumulative, total_cost)}"
        cursor_rows.append(len(body))
        body.append(
            f"  {number:>{index_width}} {group['time'][5:16]:<{time_width}} "
            f"{pad(shorten(title, prompt_width), prompt_width)} {group['turns']:>{turns_width}} "
            + (
                f"{(str(group['calls']) if group['calls'] else '-'):>{calls_width}} "
                if calls_width
                else ""
            )
            + (
                f"{pad(shorten(agent_cells[number - 1], agents_width), agents_width)} "
                if agents_width
                else ""
            )
            + f"{cached:>{cached_width}} {human_tokens(group['tokens']):>{token_width}} "
            f"{money(group['cost']):>{cost_width}}"
            + (f" {cost_bar(group['cost'], peak, bar_width)}" if bar_width else "")
            + (f" {cumulative_label:>{cumulative_width}}" if cumulative_width else "")
        )
    total_row = None
    if len(groups) > 1:
        total_row = (
            f"  {'':>{index_width}} {pad('TOTAL', time_width)} {'':<{prompt_width}} "
            f"{sum(group['turns'] for group in groups):>{turns_width}} "
            + (f"{sum(group['calls'] for group in groups):>{calls_width}} " if calls_width else "")
            + (
                f"{pad(shorten('↳ ' + agent_mix_label(rows), agents_width), agents_width)} "
                if agents_width
                else ""
            )
            + f"{'':>{cached_width}} "
            f"{human_tokens(sum(group['tokens'] for group in groups)):>{token_width}} "
            f"{money(total_cost):>{cost_width}}"
        )
    strips = turn_metric_strips(
        groups, [group["cost"] for group in groups], width, False, unit="prompt"
    )
    box = ruled_box(head, header, body, total_row, [], width, glyphs)
    lines = strips + [""] + list(box.lines)
    start = len(strips) + 1 + (box.body_start or 0)
    row_map = {start + row: ordinal for ordinal, row in enumerate(cursor_rows)}
    notes = ["· Cached is context reused when the prompt started; low means it paid again."]
    if scoped:
        notes.insert(0, "· Prompt and turn numbers are local to this execution, not session-wide.")
    if comps:
        notes.append(
            "· ▼ context was compacted before that execution turn."
            if scoped
            else "· ▼ context was compacted before that turn; Context charts the drop."
        )
    if late:
        notes.append("· ❄ idle time expired the prompt cache, so that turn paid for context again.")
    if switched:
        notes.append(
            "· ⚙ changing reasoning effort invalidated the cached prefix before that turn."
        )
    lines.append("")
    for note in notes:
        pieces = textwrap.wrap(note, max(20, width - 2)) or [note]
        lines.append(pieces[0])
        lines += ["  " + piece for piece in pieces[1:]]
    return TurnLayout(
        lines,
        {box.lines[box.header_line]} if box.header_line is not None else set(),
        row_map,
        {ordinal: line for line, ordinal in row_map.items()},
        {},
    )
