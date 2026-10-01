"""Bounded, lazy recorded Copilot file changes; never inspect the working tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re

from opentab.conversations.copilot import event_agent, load_events
from opentab.conversations.reader import ConversationError
from opentab.stores.copilot_events import node_id

SUMMARY_LIMIT = 2000
SUMMARY_BYTES = 1024 * 1024
DIFF_BYTES = 1024 * 1024
_HUNK = re.compile(r"^@@ -(\d{1,9})(?:,(\d{1,9}))? \+(\d{1,9})(?:,(\d{1,9}))? @@")
_LIMITATIONS = (
    "Successful built-in file tools only; shell, formatter, MCP and failed/partial edits may be missing.",
    "Recorded patches are historical evidence, not today's Git diff or net changes.",
    "Files without retained patches have unknown line counts; paths are resolved lexically.",
)


class ChangeRequest:
    """Freeze source identity; the worker owns its file reads and metadata connection."""

    def __init__(self, root_dir: str, root_id: str, key: str | None):
        self.root_dir, self.root_id, self.key = root_dir, root_id, key

    def __call__(self, cancelled):
        from opentab.stores.copilot import CopilotStore

        if cancelled.is_set():
            return None
        store = CopilotStore(self.root_dir, argparse.Namespace(demo=False))
        try:
            return read_changes(store, self.root_id, self.key, cancelled=cancelled)
        except ConversationError as exc:
            if self.key is not None or exc.code == "read_cancelled":
                return None
            raise ValueError("Could not read recorded change summaries.") from None


def _string(value):
    return value if isinstance(value, str) else ""


def _path(value, cwd):
    # Files are labels, never opened. No realpath: historical worktrees or moved
    # files need not exist today, and symlinks must not merge distinct records.
    if not isinstance(value, str) or not value or any(c in value for c in "\0\r\n\t"):
        return None
    return os.path.normpath(os.path.join(cwd, value)) if cwd else os.path.normpath(value)


def _display(path, root):
    return os.path.relpath(path, root) if root and os.path.isabs(path) else path


def _planned(name, args, cwd):
    """Paths actually requested by a recognized builtin, independent of its output prose."""
    if name == "apply_patch":
        patch = (
            args if isinstance(args, str) else args.get("patch") if isinstance(args, dict) else None
        )
        if not isinstance(patch, str):
            return []
        rows = []
        for line in patch.splitlines():
            match = re.fullmatch(r"\*\*\* (Add|Update|Delete) File: (.+)", line)
            if match:
                path = _path(match[2], cwd)
                if path:
                    rows.append(
                        {
                            "path": path,
                            "status": {"Add": "added", "Update": "modified", "Delete": "deleted"}[
                                match[1]
                            ],
                        }
                    )
            elif line.startswith("*** Move to: ") and rows and rows[-1]["status"] == "modified":
                path = _path(line[len("*** Move to: ") :], cwd)
                if path:
                    rows[-1].update(path=path, from_path=rows[-1]["path"], status="moved")
        return rows
    if not isinstance(args, dict):
        return []
    command = args.get("command") if name == "str_replace_editor" else name
    if command not in ("edit", "str_replace", "insert", "create"):
        return []
    path = _path(args.get("path"), cwd)
    return (
        [{"path": path, "status": "added" if command == "create" else "modified"}] if path else []
    )


def _patches(text):
    """Copilot diffFormatGit output has unquoted --- a/path and +++ b/path headers."""
    starts = [m.start() for m in re.finditer(r"(?m)^diff --git ", text)]
    result = []
    for i, start in enumerate(starts):
        block = text[start : starts[i + 1] if i + 1 < len(starts) else len(text)]
        before = re.search(r"(?m)^--- (?:a/)?([^\r\n]+)$", block)
        after = re.search(r"(?m)^\+\+\+ (?:b/)?([^\r\n]+)$", block)
        if before is not None and after is not None:
            result.append((before[1], after[1], block))
    return result


def _header(path):
    # Copilot's formatter strips the leading slash from absolute paths.
    return path.replace(os.sep, "/").lstrip("/")


def _matching_patch(row, patches):
    old = "dev/null" if row["status"] == "added" else _header(row.get("from_path", row["path"]))
    new = "dev/null" if row["status"] == "deleted" else _header(row["path"])
    matches = [
        patch
        for before, after, patch in patches
        if before.lstrip("/") == old and after.lstrip("/") == new
    ]
    return matches[0] if len(matches) == 1 else None


def _counts(patch):
    if patch is None:
        return None, None
    added = removed = left = right = 0
    seen = False
    for line in patch.splitlines():
        hunk = _HUNK.match(line)
        if hunk:
            if left or right:
                return None, None
            left, right = int(hunk[2] or 1), int(hunk[4] or 1)
            seen = True
        elif left or right:
            if line.startswith("\\ No newline"):
                continue
            if line[:1] not in (" ", "+", "-"):
                return None, None
            if line[0] != "+":
                left -= 1
            if line[0] != "-":
                right -= 1
            if min(left, right) < 0:
                return None, None
            added += line[0] == "+"
            removed += line[0] == "-"
        elif seen and line and not line.startswith("\\ No newline"):
            # Do not silently ignore extra hunk content after its declared counts.
            return None, None
    return (added, removed) if seen and not (left or right) else (None, None)


def _occurrences(store, root_id, *, cancelled=None):
    located, snapshot, limitations, agents, valid = load_events(
        store._events, root_id, cancelled=cancelled
    )
    header = next(e["data"] for _, _, e in located if e.get("type") == "session.start")
    context = header.get("context")
    cwd = _string(context.get("cwd")) if isinstance(context, dict) else ""
    if not cwd:
        cwd = store._load_meta().get(root_id, ("", ""))[0]
    root_directory = cwd
    directories = {"": cwd}
    pending = {}
    seen = set()
    for _path_value, line, event in located:
        if cancelled is not None and cancelled.is_set():
            return
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        eid = _string(event.get("id"))
        # Repeated persisted events do not create additional tool completions.
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        kind = event.get("type")
        aid = event_agent(event, agents)
        owned = aid is not None and (not aid or aid in valid)
        if kind == "session.context_changed" and owned:
            context = data.get("context", data)
            value = _string(context.get("cwd")) if isinstance(context, dict) else ""
            if value:
                directories[aid] = value
                if not aid and not root_directory:
                    root_directory = value
        elif kind == "subagent.started" and aid in valid:
            directories[aid] = directories.get(valid[aid]["parent"], cwd)
        elif kind == "assistant.message":
            requests = data.get("toolRequests")
            for request in requests if isinstance(requests, list) else []:
                if not isinstance(request, dict):
                    continue
                tcid = _string(request.get("toolCallId"))
                pending.pop((aid, tcid), None)
                if tcid and owned:
                    pending[aid, tcid] = {
                        "name": _string(request.get("name")),
                        "args": request.get("arguments"),
                        "mcp": bool(request.get("mcpServerName") or request.get("mcpToolName")),
                        "message_id": _string(data.get("messageId")) or eid or str(line),
                        "turn": _string(data.get("turnId")),
                        "cwd": directories.get(aid, cwd),
                    }
        elif kind in ("tool.user_requested", "tool.execution_start"):
            tcid = _string(data.get("toolCallId"))
            previous = pending.pop((aid, tcid), None)
            if kind == "tool.execution_start" and tcid and owned:
                # Startup owns the executed arguments; it can also survive without
                # an assistant event (e.g. a user-requested tool).
                same = (
                    previous is not None
                    and previous["name"] == data.get("toolName")
                    and (
                        not previous["turn"]
                        or not data.get("turnId")
                        or previous["turn"] == data["turnId"]
                    )
                    and not previous.get("started")
                )
                pending[aid, tcid] = {
                    "name": _string(data.get("toolName")),
                    "args": data.get("arguments", previous["args"] if same else None),
                    "mcp": bool(data.get("mcpServerName") or data.get("mcpToolName"))
                    or (same and previous["mcp"]),
                    "message_id": previous["message_id"] if same else eid or str(line),
                    "turn": _string(data.get("turnId")),
                    "cwd": directories.get(aid, cwd),
                    "started": True,
                }
        elif kind == "tool.execution_complete":
            tcid = _string(data.get("toolCallId"))
            call = pending.get((aid, tcid))
            if call is None:
                continue
            if call["turn"] and data.get("turnId") and call["turn"] != data["turnId"]:
                continue
            pending.pop((aid, tcid), None)
            if (
                data.get("success") is not True
                or call["mcp"]
                or call["name"] not in ("edit", "create", "str_replace_editor", "apply_patch")
            ):
                continue
            result = data.get("result")
            result = result if isinstance(result, dict) else {}
            text = _string(result.get("detailedContent")) or _string(result.get("content"))
            patches = _patches(text)
            for index, row in enumerate(_planned(call["name"], call["args"], call["cwd"])):
                patch = _matching_patch(row, patches)
                additions, deletions = _counts(patch)
                binding = [root_id, snapshot, line, index, aid, call["message_id"], row]
                digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
                edit = {
                    "key": f"cpchg1:{line}:{index}:{digest}",
                    "message_id": call["message_id"],
                    "execution_id": node_id(root_id, aid),
                    "file": _display(row["path"], root_directory),
                    "status": row["status"],
                    "additions": additions,
                    "deletions": deletions,
                    "available": patch is not None,
                    "source": call["name"],
                }
                if row.get("from_path"):
                    edit["from_file"] = _display(row["from_path"], root_directory)
                yield edit, patch, limitations


def read_changes(store, root_id, key=None, *, cancelled=None):
    result = {"files": [], "limitations": list(_LIMITATIONS), "truncated": False}
    if store.demo:
        return None if key is not None else result
    if key is not None and (
        not isinstance(key, str)
        or re.fullmatch(r"cpchg1:\d{1,19}:\d{1,19}:[0-9a-f]{64}", key) is None
    ):
        return None
    files = {}
    used = count = 0
    for edit, patch, limitations in _occurrences(store, root_id, cancelled=cancelled):
        if key is not None:
            if edit["key"] != key:
                continue
            if patch is None:
                return None
            raw = patch.encode("utf-8")
            clipped = len(raw) > DIFF_BYTES
            return {
                "file": edit["file"],
                "patch": raw[:DIFF_BYTES].decode("utf-8", "ignore"),
                "truncated": clipped,
                "limitation": "Recorded Copilot tool patch; output was truncated."
                if clipped
                else "Recorded Copilot tool patch.",
            }
        size = len(json.dumps(edit, ensure_ascii=False).encode("utf-8"))
        if count >= SUMMARY_LIMIT or used + size > SUMMARY_BYTES:
            result["truncated"] = True
            result["limitations"].append(
                "The retained change occurrence/metadata limit was reached."
            )
            break
        count += 1
        used += size
        result["limitations"].extend(x for x in limitations if x not in result["limitations"])
        item = files.setdefault(
            edit["file"],
            {
                "file": edit["file"],
                "status": edit["status"],
                "additions": 0,
                "deletions": 0,
                "edits": [],
            },
        )
        if item["status"] != edit["status"]:
            item["status"] = "mixed"
        for field in ("additions", "deletions"):
            item[field] = (
                item[field] + edit[field]
                if item[field] is not None and edit[field] is not None
                else None
            )
        item["edits"].append(edit)
    if key is not None:
        return None
    result["files"] = list(files.values())
    return result
