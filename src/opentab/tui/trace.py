"""Pure formatting for recorded turn events."""
from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass

from opentab.formatting import display_width, shorten, wrap_cells
from opentab.util import short_tool_name

# The store already clips recorded text. These second caps keep a turn scannable.
TRACE_OUTPUT_LINES = 6
TRACE_PARAM_LINES = 6
TRACE_VALUE_LINES = 10
TRACE_PROSE_LINES = 40


class TraceLine(str):
    """Transcript styling belongs to its event, never to text found inside it."""

    def __new__(cls, text: str, role: str, event: int | None = None):
        line = super().__new__(cls, text)
        line.role = role
        line.event = event
        return line


@dataclass
class TraceLayout:
    lines: list[str]
    tool_lines: dict[int, int]
    output_ends: list[tuple[int, int]]


def output_target(output_ends: list[tuple[int, int]], scroll: int) -> int | None:
    """Return the output at the viewport top, or the next one below it."""
    pos = bisect_left(output_ends, (scroll, -1))
    return output_ends[pos][1] if pos < len(output_ends) else None


def wrapped(prefix: str, text: str, cont: str, wrap: int) -> list[str]:
    """Wrap one prose line while charging prefixes against the pane width."""
    room = max(4, wrap - display_width(cont if len(cont) > len(prefix) else prefix))
    parts = wrap_cells(text, room) or [""]
    return [prefix + parts[0]] + [cont + part for part in parts[1:]]


def _cell_chunks(text: str, width: int) -> list[str]:
    """Hard-wrap at cell boundaries without normalizing whitespace."""
    width = max(1, width)
    if text.isascii():
        return [text[i : i + width] for i in range(0, len(text), width)] or [""]
    out: list[str] = []
    start = used = 0
    for i, char in enumerate(text):
        cells = display_width(char)
        if used + cells > width and i > start:
            out.append(text[start:i])
            start, used = i, 0
        used += cells
    return out + [text[start:]]


def format_block(text: str, indent: str, wrap: int, limit: int) -> list[str]:
    """Keep raw line layout, expanding tabs to four spaces and hard-wrapping cells."""
    out: list[str] = []
    lines = text.splitlines() or [""]
    room = max(4, wrap - display_width(indent))
    for raw in lines[:limit]:
        raw = raw.replace("\t", "    ")
        if not raw:
            out.append(indent.rstrip())
            continue
        out += [indent + chunk for chunk in _cell_chunks(raw, room)]
    hidden = len(lines) - limit
    if hidden > 0:
        out.append(f"{indent}… {hidden:,} more line{'' if hidden == 1 else 's'}")
    return out


def format_prose(
    event: dict, wrap: int, *, expanded: bool = False, indent: str = "  "
) -> list[str]:
    raw_lines = (event.get("text") or "").splitlines()
    limit = len(raw_lines) if expanded else TRACE_PROSE_LINES
    out: list[str] = []
    fenced = ""
    role = "reasoning" if event.get("kind") == "reasoning" else "text"
    for raw in raw_lines[:limit]:
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", raw)
        code = fenced or fence or raw.startswith(("    ", "\t"))
        if fence:
            if not fenced:
                fenced = fence[1]
                out += [
                    TraceLine(line, "meta")
                    for line in wrapped(indent, fence[2].strip() or "Code", indent, wrap)
                ]
            elif fence[1][0] == fenced[0] and len(fence[1]) >= len(fenced) and not fence[2].strip():
                fenced = ""
            else:
                out += [TraceLine(line, role) for line in format_block(raw, indent, wrap, 1)]
            continue
        if code:
            out += [TraceLine(line, role) for line in format_block(raw, indent, wrap, 1)]
        elif raw.strip():
            heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?$", raw)
            strong = re.fullmatch(r"\s*\*\*([^*]+)\*\*\s*", raw)
            line_role = "heading" if heading or strong else role
            raw = heading[1] if heading else strong[1] if strong else raw
            # Keep inline code literal while removing the deliberately small bold surface.
            parts = re.split(r"(`+[^`]+`+)", raw)
            raw = "".join(
                part if part.startswith("`") else re.sub(r"\*\*([^*]+)\*\*", r"\1", part)
                for part in parts
            )
            out += [
                TraceLine(indent + part, line_role)
                for part in wrap_cells(raw, max(4, wrap - display_width(indent)))
            ]
        else:
            out.append("")
    if len(raw_lines) > limit:
        out.append(TraceLine(f"{indent}… {len(raw_lines) - limit:,} more lines", "meta"))
    dropped = event.get("dropped") or 0
    if dropped:
        out.append(TraceLine(f"{indent}… {dropped:,} more characters", "meta"))
    return out


def format_output(event: dict, wrap: int, *, expanded: bool = False) -> list[str]:
    output = event.get("output") or ""
    if not output:
        if event.get("output_dropped"):
            return wrapped(
                "│  … ",
                f"{event['output_dropped']:,} more characters",
                "│    ",
                wrap,
            )
        return []
    if expanded:
        return [line or "│" for line in format_block(output, "│  ", wrap, len(output.splitlines()))]
    # Blank runs should not consume the whole preview before useful output appears.
    shown: list[str] = []
    for paragraph in output.splitlines():
        if paragraph.strip() or (shown and shown[-1].strip()):
            shown.append(paragraph)
    while shown and not shown[-1].strip():
        shown.pop()
    body = format_block("\n".join(shown), "│  ", wrap, len(shown))
    hidden = max(0, len(body) - TRACE_OUTPUT_LINES)
    out = [line or "│" for line in body[:TRACE_OUTPUT_LINES]]
    tail = []
    if hidden:
        tail.append(f"{hidden:,} more line{'' if hidden == 1 else 's'}")
    if event.get("output_dropped"):
        tail.append(f"{event['output_dropped']:,} more characters")
    if tail:
        out += wrapped("│  … ", ", ".join(tail), "│    ", wrap)
    return out


def _format_call(
    event: dict,
    wrap: int,
    event_index: int,
    *,
    expanded: bool,
    full_events: list[dict] | None,
    select_key: str,
    whole_turn_expanded: bool,
) -> list[str]:
    name = short_tool_name(str(event.get("name") or "(unknown)"))
    status = event.get("status")
    if status in ("error", "pending", "running"):
        name += f" · {str(status).capitalize()}"
    args = str(event.get("args") or "")
    limit = len(args.splitlines()) if whole_turn_expanded else TRACE_VALUE_LINES
    out = format_block(f"▸ {name}", "", wrap, 1)
    if args:
        out += format_block(args, "│  ", wrap, limit)
    out = [
        TraceLine(line, "error" if status == "error" else "tool")
        if i == 0
        else TraceLine(line, "text")
        for i, line in enumerate(out)
    ]
    params = event.get("params") or []
    param_limit = len(params) if whole_turn_expanded else TRACE_PARAM_LINES
    for key, value in params[:param_limit]:
        if key == "…":
            out.append(TraceLine(f"│  … {value}", "meta"))
            continue
        text = str(value)
        if "\n" in text:
            out += format_block(f"{key}:", "│  ", wrap, 1)
            out += format_block(
                text,
                "│    ",
                wrap,
                len(text.splitlines()) if whole_turn_expanded else TRACE_VALUE_LINES,
            )
        else:
            out += format_block(f"{key}: {text}", "│  ", wrap, 1)
    extra = len(params) - param_limit
    if extra > 0:
        out.append(f"│  … {extra} more argument{'' if extra == 1 else 's'}")
    out = [line if isinstance(line, TraceLine) else TraceLine(line, "text") for line in out]
    output_event = event
    if expanded and full_events is not None and event_index < len(full_events):
        output_event = full_events[event_index]
    if event.get("output") or event.get("output_dropped"):
        label = "Output · full" if expanded else "Output · preview"
        out.append(TraceLine("│", "meta"))
        if not whole_turn_expanded and select_key:
            label += f" · {select_key} {'collapse' if expanded else 'expand'}"
        out.append(TraceLine(shorten(f"│  {label}", wrap), "meta", event_index))
        out += [
            TraceLine(line, "output", event_index)
            for line in format_output(output_event, wrap, expanded=expanded)
        ]
        out[0].event = event_index
    out.append(TraceLine("╰─", "meta"))
    return out


def build_event_body(
    events: list[dict],
    width: int,
    *,
    line_offset: int = 0,
    expanded: bool = False,
    open_outputs: frozenset[int] = frozenset(),
    full_events: list[dict] | None = None,
    select_key: str = "",
) -> TraceLayout:
    """Format recorded events and their absolute click/scroll side channels."""
    lines: list[str] = []
    tool_lines: dict[int, int] = {}
    output_ends: list[tuple[int, int]] = []
    wrap = max(20, width - 2)
    for event_index, event in enumerate(events):
        kind = event.get("kind")
        if kind == "text":
            lines += format_prose(event, min(wrap, 100), expanded=expanded)
        elif kind == "reasoning":
            lines.append(TraceLine("✻ Thinking", "reasoning"))
            lines += format_prose(event, min(wrap, 100), expanded=expanded)
        else:
            start = line_offset + len(lines) - 2
            output_expanded = expanded or event_index in open_outputs
            lines += _format_call(
                event,
                wrap,
                event_index,
                expanded=output_expanded,
                full_events=full_events,
                select_key=select_key,
                whole_turn_expanded=expanded,
            )
            if event.get("output") or event.get("output_dropped"):
                end = line_offset + len(lines)
                tool_lines.update((line, event_index) for line in range(start, end - 2))
                output_ends.append((end - 3, event_index))
        lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return TraceLayout(lines, tool_lines, output_ends)
