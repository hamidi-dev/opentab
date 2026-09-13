"""Pure, width-aware layouts for retained conversation search text."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from opentab.presentation.formatting import clip, iso_to_local, shorten, wrap_cells


@dataclass
class SearchLine:
    text: str
    role: str = "text"
    highlights: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class ConversationLayout:
    lines: list[SearchLine]
    anchors: dict[str, int]


_ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _safe_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = _ANSI.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        char if char in "\n\t" or unicodedata.category(char) != "Cc" else " " for char in value
    )


def _tokens(value: str):
    start = None
    for index, char in enumerate(value):
        category = unicodedata.category(char)
        token_char = (
            category[0] in ("L", "N")
            or category == "Co"
            or (category[0] == "M" and start is not None)
        )
        if token_char and start is None:
            start = index
        elif not token_char and start is not None:
            yield start, index, value[start:index]
            start = None
    if start is not None:
        yield start, len(value), value[start:]


def _fold(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", value.lower())
        if unicodedata.category(char) != "Mn"
    )


def highlight_spans(text: str, query: str) -> list[tuple[int, int]]:
    """Return whole-token lexical matches using line-local character offsets."""
    terms = {_fold(token) for _start, _end, token in _tokens(_safe_text(query))}
    if not terms:
        return []
    return [(start, end) for start, end, token in _tokens(text) if _fold(token) in terms]


def _line(text: str, role: str, query: str) -> SearchLine:
    return SearchLine(text, role, highlight_spans(text, query))


def _hard_wrap(text: str, width: int) -> list[str]:
    """Wrap without changing whitespace, using shared terminal-cell measurement."""
    if not text:
        return [""]
    out = []
    rest = text
    while rest:
        head = clip(rest, width)
        if not head:
            # A double-cell glyph cannot be painted in a one-cell pane.
            head = "?"
            rest = rest[1:]
        else:
            rest = rest[len(head) :]
        out.append(head)
    return out


def _text_lines(text: str, width: int, query: str) -> list[SearchLine]:
    out: list[SearchLine] = []
    fenced = ""
    for raw in text.split("\n"):
        fence = _FENCE.match(raw)
        indented = raw.startswith(("    ", "\t"))
        code = bool(fenced or fence or indented)
        if code:
            expanded = raw.expandtabs(4)
            out.extend(_line(part, "code", query) for part in _hard_wrap(expanded, width))
            if fence:
                marker = fence[1]
                if not fenced:
                    fenced = marker
                elif marker[0] == fenced[0] and len(marker) >= len(fenced) and not fence[2].strip():
                    fenced = ""
        elif raw.strip():
            out.extend(
                _line(piece, "text", query)
                for part in wrap_cells(raw, width)
                for piece in _hard_wrap(part, width)
            )
        else:
            out.append(_line("", "text", query))
    return out


def _meta(lines: list[SearchLine], text: str, width: int) -> None:
    wrapped = wrap_cells(_safe_text(text), width) or [""]
    for part in wrapped:
        lines.extend(SearchLine(piece, "meta") for piece in _hard_wrap(part, width))


def _timestamp(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return ""
    if isinstance(value, str):
        return iso_to_local(value)
    return ""


def snippet_lines(text: str, width: int, query: str = "", max_lines: int = 3) -> list[SearchLine]:
    """Lay out a compact, sanitized prose excerpt with an honest clipping mark."""
    if width <= 0 or max_lines <= 0:
        return []
    clean = " ".join(_safe_text(text).split())
    wrapped = (
        [piece for line in wrap_cells(clean, width) for piece in _hard_wrap(line, width)]
        if clean
        else [""]
    )
    clipped = len(wrapped) > max_lines
    shown = wrapped[:max_lines]
    if clipped:
        shown[-1] = shorten(shown[-1] + "...", width)
        if not shown[-1].endswith("..."):
            shown[-1] = clip(shown[-1], max(0, width - 1)) + "…"
    return [_line(part, "text", query) for part in shown]


def conversation_layout(response: dict | None, width: int, query: str = "") -> ConversationLayout:
    """Lay out one bounded ``session_conversation`` response without fetching more."""
    if not response or width <= 0:
        return ConversationLayout([], {})
    records = response.get("records")
    if not isinstance(records, list):
        return ConversationLayout([], {})

    lines: list[SearchLine] = []
    anchors: dict[str, int] = {}
    selected = response.get("execution_id")
    executions = response.get("executions") or []
    child = next(
        (
            item
            for item in executions
            if isinstance(item, dict) and item.get("id") == selected and item.get("parent_id")
        ),
        None,
    )
    if child:
        _meta(lines, f"Child execution: {selected} (parent: {child['parent_id']})", width)
    _meta(lines, "Retained user/assistant text only; history completeness is unknown.", width)
    for limitation in response.get("limitations") or []:
        if isinstance(limitation, str) and limitation.strip():
            _meta(lines, "Limitation: " + limitation.replace("_", " "), width)
    if response.get("has_earlier"):
        _meta(lines, "Earlier retained records are not shown.", width)
    if lines and records:
        lines.append(SearchLine("", "meta"))

    for record in records:
        if not isinstance(record, dict):
            continue
        role: str = (
            str(record.get("role")) if record.get("role") in ("user", "assistant") else "text"
        )
        label = role.capitalize() if role != "text" else "Message"
        stamp = _timestamp(record.get("timestamp"))
        header = label + (f"  {stamp}" if stamp else "")
        record_id = record.get("id")
        if isinstance(record_id, str):
            anchors[record_id] = len(lines)
        lines.append(_line(shorten(_safe_text(header), width), role, query))

        partial_labeled = False
        parts_value = record.get("parts")
        parts: list = parts_value if isinstance(parts_value, list) else []
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            offset = part.get("text_offset")
            offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
            total = part.get("text_total_chars")
            total = total if isinstance(total, int) and not isinstance(total, bool) else None
            raw_text = part.get("text")
            text = _safe_text(raw_text)
            if offset > 0:
                _meta(lines, f"[Part continues from character {offset + 1}.]", width)
                partial_labeled = True
            lines.extend(_text_lines(text, width, query))
            end = offset + len(raw_text if isinstance(raw_text, str) else "")
            if part.get("truncated") and total is not None and end < total:
                _meta(lines, f"[Part continues after character {end} of {total}.]", width)
                partial_labeled = True
            elif part.get("truncated") and total is None:
                _meta(lines, "[Part is truncated in this bounded response.]", width)
                partial_labeled = True
            if index + 1 < len(parts):
                lines.append(SearchLine("", "text"))
        if record.get("record_complete") is False and not partial_labeled:
            _meta(lines, "[Partial record from the bounded response.]", width)
        lines.append(SearchLine("", "text"))

    if lines and lines[-1].text == "":
        lines.pop()
    if response.get("has_more"):
        if lines:
            lines.append(SearchLine("", "meta"))
        _meta(lines, "More retained records or part text are available.", width)
    return ConversationLayout(lines, anchors)
