"""Pure layouts for recorded file edits and snapshots."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from opentab.presentation.formatting import clip_tail, pad, shorten
from opentab.tui.components.boxes import TABLE_GLYPHS_ASCII, box_row, box_rule, box_top
from opentab.tui.trace import format_block


class ChangeLine(str):
    """A line whose role prevents generic rich-text interpretation."""

    role: str
    gutter: int

    def __new__(cls, text: str, role: str = "plain", gutter: int = 0):
        value = super().__new__(cls, text)
        value.role = role
        value.gutter = gutter
        return value


@dataclass(frozen=True)
class ChangeLayout:
    lines: list[str]
    row_map: dict[int, int]
    cursor_line: int | None


def safe_text(value: object, *, tabs: bool = False) -> str:
    """Keep untrusted source text inert while retaining ordinary whitespace."""
    text = str(value or "")
    out = []
    for char in text:
        if char == "\t" and tabs:
            out.append("    ")
        elif char in "\r\n\t" or unicodedata.category(char).startswith("C"):
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def _count(value: object) -> str:
    return str(value) if isinstance(value, int) and value >= 0 else "?"


def files_layout(
    data: Mapping, selected: int, width: int = 80, glyphs: Mapping | None = None
) -> ChangeLayout:
    glyphs = glyphs or TABLE_GLYPHS_ASCII
    files = data.get("files")
    files = (
        list(files) if isinstance(files, Sequence) and not isinstance(files, (str, bytes)) else []
    )
    lines: list[str] = [
        ChangeLine(box_top(f"Recorded changes - {len(files)} files", width, glyphs), "heading"),
    ]
    row_map: dict[int, int] = {}
    cursor_line = None
    if files:
        selected = max(0, min(selected, len(files) - 1))
        show_counts = width >= 70
        suffix = f"  {'Status':<8}  {'Records':>7}  {'Diff':<4}"
        if show_counts:
            suffix += f"  {'+Add':>6}  {'-Del':>6}"
        path_width = max(6, width - 6 - len(suffix))
        header = "  " + pad("File", path_width) + suffix
        lines.append(ChangeLine(box_row(header, width, glyphs), "heading"))
        lines.append(ChangeLine(box_rule(width, glyphs), "meta"))
        for index, item in enumerate(files):
            item = item if isinstance(item, Mapping) else {}
            edits = item.get("edits")
            edit_count = (
                len(edits)
                if isinstance(edits, Sequence) and not isinstance(edits, (str, bytes))
                else 0
            )
            marker = ">" if index == selected else " "
            status = safe_text(item.get("status") or "unknown")[:8]
            path = safe_text(item.get("file") or "(unknown file)")
            available = sum(
                bool(e.get("available")) for e in (edits or []) if isinstance(e, Mapping)
            )
            availability = (
                "yes" if available == edit_count and edit_count else "some" if available else "no"
            )
            text = (
                f"{marker} {pad(clip_tail(path, path_width), path_width)}  {status:<8}  "
                f"{edit_count:>7}  {availability:<4}"
            )
            if show_counts:
                text += f"  {shorten(_count(item.get('additions')), 6):>6}  {shorten(_count(item.get('deletions')), 6):>6}"
            line = ChangeLine(box_row(text, width, glyphs), "file")
            row_map[len(lines)] = index
            if index == selected:
                cursor_line = len(lines)
            lines.append(line)
        lines.append(ChangeLine(box_rule(width, glyphs, "bl", "br"), "meta"))
    else:
        lines.extend(
            [
                ChangeLine(
                    box_row("No retained change metadata was found.", width, glyphs), "empty"
                ),
                ChangeLine(
                    box_row("This does not prove the session made no edits.", width, glyphs), "meta"
                ),
            ]
        )
        lines.append(ChangeLine(box_rule(width, glyphs, "bl", "br"), "meta"))
    lines.extend(
        ChangeLine(line, "meta")
        for line in format_block(
            "Records are evidence, not unique edits. Line counts are not net changes.", "", width, 1
        )
    )
    limitations = data.get("limitations")
    if isinstance(limitations, Sequence) and not isinstance(limitations, (str, bytes)):
        clean = [safe_text(item) for item in limitations if safe_text(item)]
        if clean:
            lines.extend(["", ChangeLine("Limitations", "heading")])
            lines.extend(
                ChangeLine(part, "meta")
                for item in clean
                for part in format_block(f"- {item}", "", width, 1)
            )
    if data.get("truncated"):
        lines.append(ChangeLine("- Some retained change metadata was omitted.", "warning"))
    return ChangeLayout(lines, row_map, cursor_line)


_HUNK = re.compile(
    r"^@@ -(\d{1,9})(?:,(\d{1,9}))? \+(\d{1,9})(?:,(\d{1,9}))? @@(.*)$", re.MULTILINE
)


def diff_layout(file: Mapping, edit_index: int, diff: Mapping | None, width: int = 80) -> list[str]:
    edits = file.get("edits")
    edits = (
        list(edits) if isinstance(edits, Sequence) and not isinstance(edits, (str, bytes)) else []
    )
    if not edits:
        return [ChangeLine("No retained edit occurrences for this file.", "empty")]
    edit_index = max(0, min(edit_index, len(edits) - 1))
    edit = edits[edit_index] if isinstance(edits[edit_index], Mapping) else {}
    path = safe_text(file.get("file") or edit.get("file") or "(unknown file)")
    source = safe_text(edit.get("source") or "snapshot")
    evidence = "Per-prompt snapshot" if source == "snapshot" else f"Completed {source} tool"
    lines: list[str] = [
        ChangeLine(path, "heading"),
        ChangeLine(
            f"Occurrence {edit_index + 1}/{len(edits)} | status {safe_text(edit.get('status') or 'unknown')} "
            f"| +{_count(edit.get('additions'))} -{_count(edit.get('deletions'))}",
            "meta",
        ),
        ChangeLine(f"Evidence: {evidence}", "meta"),
    ]
    if edit.get("from_file"):
        lines.append(ChangeLine(f"Moved from: {safe_text(edit['from_file'])}", "meta"))
    if file.get("counts_overlap"):
        lines.append(ChangeLine("Snapshot/tool records overlap; file totals are unknown.", "meta"))
    lines.append("")
    if diff is None:
        text = (
            "This patch is unavailable or its record changed. Reload Changes to retry."
            if edit.get("available")
            else "The completed write records a path, but no before/after diff."
            if source == "write"
            else "No text patch was retained (for example, a binary file)."
        )
        lines.append(ChangeLine(text, "empty"))
    limitation = safe_text((diff or {}).get("limitation"))
    patch = (diff or {}).get("patch")
    lines = [
        ChangeLine(part, getattr(line, "role", "plain"))
        for line in lines
        for part in format_block(line, "", width, 1)
    ]
    if isinstance(patch, str) and patch:
        peak = max(
            (
                max(int(m[1]) + int(m[2] or 1), int(m[3]) + int(m[4] or 1))
                for m in _HUNK.finditer(patch)
            ),
            default=0,
        )
        digits = max(3, len(str(peak)))
        gutter = digits * 2 + 5
        old = new = left_old = left_new = 0
        for raw in patch.split("\n"):
            text = safe_text(raw, tabs=True)
            hunk = _HUNK.match(text)
            if hunk:
                old, new = int(hunk[1]), int(hunk[3])
                left_old, left_new = int(hunk[2] or 1), int(hunk[4] or 1)
                lines.append(ChangeLine("", "meta"))
                lines.extend(ChangeLine(part, "hunk") for part in format_block(text, "", width, 1))
                continue
            inside = left_old > 0 or left_new > 0
            if not inside and text.startswith(
                ("Index: ", "===", "diff ", "index ", "--- ", "+++ ")
            ):
                continue  # the file's title is already visible above the patch
            role = "add" if text.startswith("+") else "delete" if text.startswith("-") else "code"
            if text.startswith("\\ No newline"):
                lines.extend(ChangeLine(part, "meta") for part in format_block(text, "", width, 1))
                continue
            if inside and text[:1] in ("+", "-", " "):
                old_label = str(old) if role != "add" else ""
                new_label = str(new) if role != "delete" else ""
                lead = f"{old_label:>{digits}} {new_label:>{digits}}  | "
                if role != "add":
                    old, left_old = old + 1, left_old - 1
                if role != "delete":
                    new, left_new = new + 1, left_new - 1
                parts = format_block(text, "", max(4, width - gutter), 1)
                lines.append(ChangeLine(lead + parts[0], role, gutter))
                lines.extend(
                    ChangeLine(" " * (gutter - 2) + "| " + part, role, gutter) for part in parts[1:]
                )
            else:
                lines.extend(ChangeLine(part, role) for part in format_block(text, "", width, 1))
    elif diff is not None:
        lines.extend(
            ChangeLine(part, "empty")
            for part in format_block("The recorded change has no textual difference.", "", width, 1)
        )
    if (diff or {}).get("truncated") or limitation:
        role = "warning" if (diff or {}).get("truncated") else "meta"
        lines.append("")
        lines.extend(
            ChangeLine(part, role)
            for part in format_block(
                limitation or "The recorded patch was truncated.", "", width, 1
            )
        )
    return lines
