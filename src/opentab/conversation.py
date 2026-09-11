"""Bounded, source-attributed retained conversation reads, independent of accounting."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
# Bump for discovery, ownership, retained-text extraction, or index chunk projection changes.
CONVERSATION_READER_VERSION = 1


class ConversationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def source_key(path: Path) -> str:
    return hashlib.sha256(os.fsencode(os.path.abspath(path))).hexdigest()[:24]


def source_manifest(paths) -> list | None:
    """Cheap strong stamps for an exact source set, without reading file bodies."""
    rows = []
    try:
        for raw_path in sorted({os.path.abspath(path) for path in paths}):
            info = os.stat(raw_path)
            rows.append(
                [
                    source_key(Path(raw_path)),
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ]
            )
    except OSError:
        return None
    return rows


def read_jsonl(paths) -> tuple[list, str, list[str]]:
    """Read selected sources freshly, retaining physical line provenance, never logging bodies."""
    records, limitations = [], []
    digest = hashlib.sha256()
    total = 0
    stamps = {}

    def stamp(stat):
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    try:
        for raw_path in paths:
            path = Path(raw_path).absolute()
            digest.update(source_key(path).encode("ascii"))
            with path.open("rb") as stream:
                initial = stamp(os.fstat(stream.fileno()))
                stamps[path] = initial
                if total + initial[2] > MAX_SOURCE_BYTES:
                    raise ConversationError(
                        "conversation_too_large", "Selected sources exceed the read budget."
                    )
                number = 0
                while True:
                    line = stream.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                    number += 1
                    total += len(line)
                    if len(line) > MAX_LINE_BYTES or total > MAX_SOURCE_BYTES:
                        raise ConversationError(
                            "conversation_too_large",
                            "Selected source records exceed the read budget.",
                        )
                    digest.update(line)
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("not an object")
                    except (ValueError, UnicodeError, RecursionError):
                        limitations.append("malformed_jsonl_records_skipped")
                        continue
                    records.append((path, number, record))
                if stamp(os.fstat(stream.fileno())) != initial:
                    raise ConversationError(
                        "source_changed", "Conversation source changed during the read; retry."
                    )
        if any(stamp(path.stat()) != initial for path, initial in stamps.items()):
            raise ConversationError(
                "source_changed", "Conversation source changed during the read; retry."
            )
    except OSError:
        raise ConversationError(
            "conversation_unavailable", "A selected conversation source is missing or unreadable."
        ) from None
    return records, digest.hexdigest(), list(dict.fromkeys(limitations))


def validate_window(*, anchor=None, cursor=None, limit=20, max_chars=20000, before=0, tail=False):
    for name, value, low, high in (
        ("limit", limit, 1, 100),
        ("max_chars", max_chars, 1, 120000),
        ("before", before, 0, 99),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ConversationError(
                "invalid_arguments", f"{name} must be an integer between {low} and {high}."
            )
    for name, value, maximum in (("anchor", anchor, 1024), ("cursor", cursor, 8192)):
        if value is not None and (not isinstance(value, str) or not value or len(value) > maximum):
            raise ConversationError(
                "invalid_arguments", f"{name} must be a nonempty bounded string."
            )
    if not isinstance(tail, bool):
        raise ConversationError("invalid_arguments", "tail must be a boolean.")
    if sum((anchor is not None, cursor is not None, tail)) > 1 or (before and anchor is None):
        raise ConversationError(
            "invalid_arguments", "Use only one of anchor, cursor or tail; before requires anchor."
        )


def window(
    source, *, root_key, anchor=None, cursor=None, limit=20, max_chars=20000, before=0, tail=False
):
    """Page source records and oversized text parts without losing or repeating characters."""
    validate_window(
        anchor=anchor, cursor=cursor, limit=limit, max_chars=max_chars, before=before, tail=tail
    )
    records = source["records"]
    binding = hashlib.sha256(
        json.dumps(
            [root_key, source["execution_id"], source["snapshot"]], separators=(",", ":")
        ).encode()
    ).hexdigest()
    index, part_index, char_offset = 0, 0, 0
    if cursor is not None:
        try:
            decoded = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            token = json.loads(decoded)
            if (
                not isinstance(token, list)
                or len(token) != 5
                or not isinstance(token[0], int)
                or isinstance(token[0], bool)
                or token[0] != 1
            ):
                raise ValueError
            if not isinstance(token[1], str):
                raise ValueError
            index, part_index, char_offset = token[2:]
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in token[2:]
            ):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise ConversationError("invalid_cursor", "Conversation cursor is malformed.") from None
        if token[1] != binding:
            raise ConversationError(
                "stale_cursor", "Conversation scope or source changed; start a new window."
            )
        if index >= len(records) or part_index >= len(records[index]["parts"]):
            # Record-boundary cursors can also point at a message with no text parts.
            if index >= len(records) or (part_index, char_offset) != (0, 0):
                raise ConversationError(
                    "invalid_cursor", "Conversation cursor position is invalid."
                )
        elif char_offset > len(records[index]["parts"][part_index]["text"]):
            raise ConversationError("invalid_cursor", "Conversation cursor offset is invalid.")
    elif anchor is not None:
        matches = [i for i, record in enumerate(records) if record["id"] == anchor]
        if not matches:
            matches = [
                i
                for i, record in enumerate(records)
                if anchor in (record.get("message_id"), record.get("record_id"))
            ]
        if not matches:
            raise ConversationError(
                "anchor_not_found", "Anchor is absent from the selected retained text scope."
            )
        if len(matches) != 1:
            raise ConversationError(
                "ambiguous_anchor",
                "Native anchor has multiple occurrences; use a returned record id.",
            )
        index = max(0, matches[0] - before)
    elif tail:
        index = max(0, len(records) - limit)
    has_earlier = any((index, part_index, char_offset))
    result = []
    remaining = max_chars
    while index < len(records) and len(result) < limit:
        record = records[index]
        parts = []
        initial_part, initial_offset = part_index, char_offset
        while part_index < len(record["parts"]):
            part = record["parts"][part_index]
            text = part["text"]
            end = min(len(text), char_offset + remaining)
            parts.append(
                {
                    **part,
                    "text": text[char_offset:end],
                    "text_offset": char_offset,
                    "text_total_chars": len(text),
                    "truncated": char_offset > 0 or end < len(text),
                }
            )
            remaining -= end - char_offset
            if end < len(text):
                char_offset = end
                break
            part_index += 1
            char_offset = 0
            if remaining == 0:
                break
        finished = part_index == len(record["parts"])
        result.append(
            {
                **record,
                "parts": parts,
                "record_complete": finished and initial_part == 0 and initial_offset == 0,
            }
        )
        if finished:
            index += 1
            part_index, char_offset = 0, 0
        if remaining == 0:
            break
    has_more = index < len(records)
    next_cursor = None
    if has_more:
        token = [1, binding, index, part_index, char_offset]
        next_cursor = (
            base64.urlsafe_b64encode(json.dumps(token, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )
    return {
        "session_key": root_key,
        "execution_id": source["execution_id"],
        "executions": source["executions"],
        "snapshot": source["snapshot"],
        "ordering": source["ordering"],
        "text_scope": "retained user/assistant text; no tools, reasoning or attachments",
        "history_completeness": "unknown",
        "limitations": source["limitations"],
        "records": result,
        "has_earlier": has_earlier,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "returned_chars": max_chars - remaining,
        "limit": limit,
        "max_chars": max_chars,
    }
