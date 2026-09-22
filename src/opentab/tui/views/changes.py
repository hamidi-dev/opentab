"""Pure layouts for recorded file edits and snapshots."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import zip_longest
from pathlib import PurePosixPath

from opentab.presentation.formatting import clip_tail, display_width, pad, shorten, wrap_cells
from opentab.tui.components.boxes import TABLE_GLYPHS_ASCII, box_row, box_rule, box_top
from opentab.tui.trace import format_block


@dataclass(frozen=True)
class ChangeSpan:
    x: int
    text: str
    foreground: str = "ink"
    background: str = "code"


class ChangeLine(str):
    """A line whose role prevents generic rich-text interpretation."""

    role: str
    gutter: int

    def __new__(cls, text: str, role: str = "plain", gutter: int = 0, *, spans=(), anchor=None):
        value = super().__new__(cls, text)
        value.role = role
        value.gutter = gutter
        value.spans = tuple(spans)
        value.anchor = anchor
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


DIFF_MIN_SPLIT_WIDTH = 80
DIFF_FOREGROUNDS = ("ink", "keyword", "string", "number", "comment", "function", "gutter", "marker")
DIFF_BACKGROUNDS = ("code", "add", "delete", "add-emphasis", "delete-emphasis")
_KEYWORDS = frozenset(
    "and as assert async await begin break case catch class const continue def default del do "
    "elif else elseif end enum except export extends false False finally fn for from function "
    "global go if impl import in interface is lambda let local match module mut new nil None "
    "not null of or package pass private pub public raise require return self static struct "
    "super switch then this throw true True try type typeof use var void while with yield".split()
)
_TOKEN = re.compile(
    r"""(?P<string>"(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?|`(?:\\.|[^`\\])*`?)"""
    r"|(?P<comment>//|\#|--|/\*)"
    r"|(?P<number>\b(?:0[xX][\da-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)"
    r"|(?P<word>\b[A-Za-z_]\w*\b)"
)


def _syntax(text: str, path: str) -> list[tuple[int, int, str]]:
    """Small, line-local lexer: snippets need not be complete parseable programs."""
    if len(text) > 16000:
        return []
    suffix = PurePosixPath(path).suffix.lower()
    comments = {"/*", "//"}
    if suffix in {".py", ".sh", ".bash", ".zsh", ".rb", ".yaml", ".yml", ".toml", ".conf"}:
        comments = {"#"}
    elif suffix in {".lua", ".sql", ".hs"}:
        comments = {"--"}
    elif suffix in {".json", ".md", ".txt"}:
        comments = set()
    spans = []
    for match in _TOKEN.finditer(text):
        kind = match.lastgroup
        token = match[0]
        if kind == "comment":
            if token in comments:
                spans.append((match.start(), len(text), "comment"))
                break
            continue
        if kind == "word":
            kind = (
                "keyword"
                if token in _KEYWORDS
                else "function"
                if text[match.end() :].lstrip().startswith("(")
                else "ink"
            )
        if kind != "ink":
            spans.append((match.start(), match.end(), kind))
    return spans


@dataclass
class _DiffRow:
    text: str
    role: str
    old: int | None
    new: int | None
    anchor: int
    emphasis: tuple[tuple[int, int], ...] = ()


def _patch_rows(patch: str) -> list[_DiffRow]:
    rows = []
    old = new = left_old = left_new = 0
    raw_lines = patch.split("\n")
    if raw_lines[-1] == "":
        raw_lines.pop()
    for anchor, raw in enumerate(raw_lines):
        text = safe_text(raw, tabs=True)
        hunk = _HUNK.match(text)
        if hunk:
            old, new = int(hunk[1]), int(hunk[3])
            left_old, left_new = int(hunk[2] or 1), int(hunk[4] or 1)
            rows.append(_DiffRow(text, "hunk", None, None, anchor))
            continue
        inside = left_old > 0 or left_new > 0
        if not inside and text.startswith(("Index: ", "===", "diff ", "index ", "--- ", "+++ ")):
            continue
        role = "add" if text.startswith("+") else "delete" if text.startswith("-") else "code"
        if text.startswith("\\ No newline"):
            role = "meta"
        a = b = None
        if inside and text[:1] in ("+", "-", " "):
            if role != "add":
                a = old
                old, left_old = old + 1, max(0, left_old - 1)
            if role != "delete":
                b = new
                new, left_new = new + 1, max(0, left_new - 1)
        rows.append(_DiffRow(text, role, a, b, anchor))
    return rows


def _pairs(rows: list[_DiffRow]):
    """Pair replacement runs by order, keeping context and hunk boundaries intact."""
    deleted, added = [], []
    for row in rows:
        if row.role in ("delete", "add"):
            (deleted if row.role == "delete" else added).append(row)
            continue
        yield from zip_longest(deleted, added)
        deleted, added = [], []
        yield row, row
    yield from zip_longest(deleted, added)


def _emphasize(left: _DiffRow | None, right: _DiffRow | None) -> None:
    if left is None or right is None or left is right:
        return
    # Bound quadratic matching for minified/generated lines. The line tint still
    # identifies every edit; only the optional word-level refinement is skipped.
    if max(len(left.text), len(right.text)) > 2000:
        return
    before, after = [], []
    for tag, a, b, c, d in SequenceMatcher(None, left.text[1:], right.text[1:]).get_opcodes():
        if tag != "equal":
            if a != b:
                before.append((a + 1, b + 1))
            if c != d:
                after.append((c + 1, d + 1))
    left.emphasis, right.emphasis = tuple(before), tuple(after)


def _code_lines(
    row: _DiffRow, width: int, digits: int, path: str, *, split=False, old_side=False, bar="|"
):
    numbered = row.old is not None or row.new is not None
    if split:
        number = row.old if old_side else row.new
        lead = f"{str(number) if number is not None else '':>{digits}} {bar} "
    elif numbered:
        lead = f"{str(row.old) if row.old is not None else '':>{digits}} {str(row.new) if row.new is not None else '':>{digits}}  {bar} "
    else:
        lead = ""
    gutter = len(lead)
    if gutter >= width - 2:
        lead, gutter = "", 0
    body_width = max(1, width - gutter)
    syntax = _syntax(row.text[1:] if row.text[:1] in ("+", "-", " ") else row.text, path)
    shift = int(row.text[:1] in ("+", "-", " "))
    syntax = [(a + shift, b + shift, role) for a, b, role in syntax]
    background = row.role if row.role in ("add", "delete") else "code"
    # Merge ordered intervals once. Rescanning every token for every wrapped row
    # makes a long generated line quadratic even when intraline matching is off.
    boundaries = {0, len(row.text)}
    if row.role in ("add", "delete"):
        boundaries.add(min(1, len(row.text)))
    for a, b, *_ in [*syntax, *row.emphasis]:
        boundaries.update((a, b))
    boundaries = sorted(boundaries)
    runs = []
    token = emphasis = 0
    for a, b in zip(boundaries, boundaries[1:]):
        while token < len(syntax) and syntax[token][1] <= a:
            token += 1
        while emphasis < len(row.emphasis) and row.emphasis[emphasis][1] <= a:
            emphasis += 1
        fg = syntax[token][2] if token < len(syntax) and syntax[token][0] <= a else "ink"
        if a == 0 and row.role in ("add", "delete"):
            fg = "marker"
        bg = background
        if emphasis < len(row.emphasis) and row.emphasis[emphasis][0] <= a:
            bg += "-emphasis"
        runs.append((a, b, fg, bg))
    parts = format_block(row.text, "", body_width, 1)
    offset = run = 0
    for index, part in enumerate(parts):
        prefix = lead if index == 0 else (" " * (gutter - 2) + bar + " " if gutter else "")
        spans = [ChangeSpan(gutter, " " * body_width, background=background)]
        if gutter:
            spans.append(
                ChangeSpan(0, prefix, "marker" if background != "code" else "gutter", background)
            )
        end = offset + len(part)
        x = gutter
        while run < len(runs) and runs[run][0] < end:
            a, b, fg, bg = runs[run]
            text = part[max(0, a - offset) : min(end, b) - offset]
            spans.append(ChangeSpan(x, text, fg, bg))
            x += display_width(text)
            if b > end:
                break
            run += 1
        yield ChangeLine(prefix + part, row.role, gutter, spans=spans, anchor=row.anchor)
        offset = end


def _render_patch(patch: str, path: str, width: int, split: bool, glyphs: Mapping) -> list[str]:
    rows = _patch_rows(patch)
    pairs = list(_pairs(rows))
    for left, right in pairs:
        _emphasize(left, right)
    digits = max(3, len(str(max((max(row.old or 0, row.new or 0) for row in rows), default=0))))
    lines = []
    bar = glyphs["v"]
    left_width = (width - 3) // 2
    right_width = width - left_width - 3
    if split:
        lines.append(ChangeLine(pad("Before", left_width) + f" {bar} " + "After", "meta"))
    for left, right in pairs if split else ((row, row) for row in rows):
        row = left or right
        if row.role in ("hunk", "meta"):
            if row.role == "hunk":
                heading = format_block(row.text, "", max(4, width - 6), 1)
                lines.append(
                    ChangeLine(box_top(heading[0], width, glyphs), "hunk", anchor=row.anchor)
                )
                lines.extend(
                    ChangeLine(box_row(part, width, glyphs), "hunk", anchor=row.anchor)
                    for part in heading[1:]
                )
            else:
                lines.extend(
                    ChangeLine(part, "meta", anchor=row.anchor)
                    for part in wrap_cells(row.text, width)
                )
            continue
        if not split:
            lines.extend(_code_lines(row, width, digits, path, bar=bar))
            continue
        a = (
            list(_code_lines(left, left_width, digits, path, split=True, old_side=True, bar=bar))
            if left
            else []
        )
        b = (
            list(_code_lines(right, right_width, digits, path, split=True, bar=bar))
            if right
            else []
        )
        for before, after in zip_longest(a, b):
            before = before if before is not None else ChangeLine("")
            after = after if after is not None else ChangeLine("")
            text = pad(before, left_width) + f" {bar} " + after
            spans = list(before.spans)
            spans.append(ChangeSpan(left_width, f" {bar} ", "gutter"))
            spans.extend(
                ChangeSpan(s.x + left_width + 3, s.text, s.foreground, s.background)
                for s in after.spans
            )
            lines.append(ChangeLine(text, "split", spans=spans, anchor=row.anchor))
    return lines


def diff_layout(
    file: Mapping,
    edit_index: int,
    diff: Mapping | None,
    width: int = 80,
    *,
    side_by_side: bool = False,
    glyphs: Mapping | None = None,
) -> list[str]:
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
        split = side_by_side and width >= DIFF_MIN_SPLIT_WIDTH
        mode = "Side-by-side" if split else "Unified (narrow window)" if side_by_side else "Unified"
        lines.append(ChangeLine(mode, "meta"))
        lines.extend(_render_patch(patch, path, width, split, glyphs or TABLE_GLYPHS_ASCII))
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
