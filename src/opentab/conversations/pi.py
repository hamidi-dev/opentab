"""Fresh retained-text reads for pi and omp, separate from their usage projections."""
from __future__ import annotations

import json
from pathlib import Path

from opentab.conversations.reader import (
    ConversationError,
    execution_tree,
    finish_source,
    read_jsonl,
    source_key,
    source_manifest,
)

_HEAD_BYTES = 65536


def _head(path):
    """Bounded discovery only; the selected sources' entire metadata is checked later."""
    try:
        with path.open("rb") as stream:
            remaining = _HEAD_BYTES
            while remaining:
                line = stream.readline(remaining + 1)
                if not line or len(line) > remaining:
                    break
                remaining -= len(line)
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if isinstance(obj, dict) and obj.get("type") == "session":
                    sid = obj.get("id")
                    return sid if isinstance(sid, str) and sid else None
    except OSError:
        pass
    return None


def _catalog(store, previous=None):
    paths = sorted({Path(name).absolute() for name in store._files()})
    manifest = source_manifest(paths)
    if manifest is None:
        raise ConversationError("conversation_unavailable", "Conversation sources are unreadable.")
    by_key = {stamp[0]: stamp for stamp in manifest}
    stamps = {path: by_key[source_key(path)] for path in paths}
    heads = {}
    for path in paths:
        if previous is not None and previous["stamps"].get(path) == stamps[path]:
            heads[path] = previous["heads"][path]
        else:
            heads[path] = _head(path)
    return {"heads": heads, "stamps": stamps}


def _scope(store, catalog, root_id, selected, nested):
    heads = catalog["heads"]
    sessions, candidates, invalid = {}, {}, set()
    for path, sid in heads.items():
        filename_id = store._id_from_name(str(path))
        if filename_id:
            candidates.setdefault(filename_id, []).append(path)
        if sid is None:
            continue
        if filename_id is not None and filename_id != sid:
            invalid.add(path)
        # pi accounts only UUID-named transcripts; omp also owns nickname-named children.
        if not nested and filename_id is None:
            continue
        parent_path = Path(str(path.parent) + ".jsonl") if nested else None
        if parent_path in heads and heads[parent_path] is None:
            invalid.add(path)
        parent = heads.get(parent_path)
        session = sessions.setdefault(sid, {"paths": [], "parents": set()})
        session["paths"].append(path)
        session["parents"].add(parent)

    # Include every potential child before rejecting ambiguous parentage. Never select
    # whichever root happened to claim an omp nickname/UUID first in accounting.
    relevant, queue = {root_id}, [root_id]
    children = {}
    for sid, session in sessions.items():
        for parent in session["parents"]:
            children.setdefault(parent, []).append(sid)
    while queue:
        for sid in children.get(queue.pop(), ()):
            if sid not in relevant:
                relevant.add(sid)
                queue.append(sid)
    current, ancestors = root_id, set()
    while current in sessions and current not in ancestors:
        ancestors.add(current)
        parents = sessions[current]["parents"]
        if len(parents) != 1:
            raise ConversationError("invalid_execution", "Conversation ownership is ambiguous.")
        current = next(iter(parents))
    relevant.update(ancestors)
    parents = {}
    for sid in relevant:
        session = sessions.get(sid)
        if session is None:
            raise ConversationError(
                "invalid_execution", "The exact session metadata is unavailable."
            )
        if len(session["parents"]) != 1 or any(
            path in invalid or heads[path] != sid
            for path in session["paths"] + candidates.get(sid, [])
        ):
            raise ConversationError("invalid_execution", "Conversation ownership is ambiguous.")
        parents[sid] = next(iter(session["parents"]))
    executions = execution_tree(parents, root_id, selected)

    # Read the selected execution and its authorization chain freshly. No accounting
    # cache, usage gate, prompt cleaning, trace clipping or replay deduplication here.
    required, current = {selected}, parents[selected]
    while current in parents:
        required.add(current)
        current = parents[current]
    read_paths = sorted(path for sid in required for path in sessions[sid]["paths"])
    relevant_paths = sorted(path for sid in relevant for path in sessions[sid]["paths"])
    manifest = [catalog["stamps"][path] for path in relevant_paths]
    return executions, read_paths, manifest


def read_source(store, root_id, execution_id=None, *, nested=False):
    if store.demo:
        raise ConversationError("conversation_unavailable", "Conversation is disabled in demo.")
    selected = root_id if execution_id is None else execution_id
    if any(not isinstance(sid, str) or not sid for sid in (root_id, selected)):
        raise ConversationError("invalid_execution", "An exact session ID is required.")
    catalog = _catalog(store)
    executions, read_paths, manifest = _scope(store, catalog, root_id, selected, nested)
    heads = catalog["heads"]
    located, snapshot, limitations = read_jsonl(read_paths)
    records = []
    seen_headers = set()
    for path, line, obj in located:
        sid = heads[path]
        if obj.get("type") == "session":
            if obj.get("id") != sid:
                raise ConversationError(
                    "invalid_execution", "A source has conflicting session metadata."
                )
            seen_headers.add(path)
            continue
        if obj.get("type") != "message":
            continue
        if path not in seen_headers:
            raise ConversationError(
                "invalid_execution", "A message precedes verified session metadata."
            )
        if sid != selected:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            content = []
        origin = {"source_key": source_key(path), "line": line}
        record_id = f"pi:{origin['source_key']}:{line}"
        mid = obj.get("id")
        stamp = obj.get("timestamp")
        if isinstance(stamp, bool) or not isinstance(stamp, (str, int, float)):
            stamp = None
        records.append(
            {
                "id": record_id,
                "record_id": mid if isinstance(mid, str) else None,
                "message_id": mid if isinstance(mid, str) else None,
                "parent_id": obj.get("parentId") if isinstance(obj.get("parentId"), str) else None,
                "execution_id": selected,
                "role": msg["role"],
                "timestamp": stamp,
                "origin": "recorded-message",
                "source": origin,
                "parts": [
                    {"id": f"{record_id}:{i}", "text": part["text"]}
                    for i, part in enumerate(content)
                    if isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                ],
            }
        )
    if seen_headers != set(read_paths):
        raise ConversationError("invalid_execution", "A source has no verified session metadata.")
    # Recheck topology as well as bytes: a new child, resumed copy or competing UUID
    # can change ownership. Unrelated sessions may keep writing during this read.
    final_catalog = _catalog(store, catalog)
    try:
        final_scope = _scope(store, final_catalog, root_id, selected, nested)
    except ConversationError:
        raise ConversationError(
            "source_changed", "Conversation ownership changed during the read; retry."
        ) from None
    if final_scope != (executions, read_paths, manifest):
        raise ConversationError(
            "source_changed", "Conversation sources changed during the read; retry."
        )
    limitations.extend(
        [
            "retained_messages_only",
            "non_text_parts_excluded",
            "Recorded occurrences and resumed copies are retained; active branches are not reconstructed.",
            "User-role text can include harness or delegated instructions, not only human-authored prompts.",
            "Execution discovery requires session metadata within the first 64 KiB.",
        ]
    )
    if any(sid is None for sid in final_catalog["heads"].values()):
        limitations.append(
            "Some session metadata is unavailable; execution discovery may be incomplete."
        )
    return finish_source(
        records,
        selected,
        executions,
        limitations,
        "filename/path order, then physical JSONL line order",
        binding=[snapshot, str(store.source_name), manifest],
    )
