"""Fresh Copilot retained events, independent of OTEL usage and trace clipping."""
from __future__ import annotations

from pathlib import Path

from opentab.conversations.reader import (
    ConversationError,
    execution_tree,
    finish_source,
    read_jsonl,
    source_key,
    source_manifest,
)
from opentab.stores.copilot_events import CopilotEvents, node_id


def load_events(events: CopilotEvents, root_id: str, *, cancelled=None):
    """Authorize one exact session file and bind all ownership to the same fresh read."""
    path = events.path(root_id)
    if path is None:
        raise ConversationError("invalid_execution", "An exact local session ID is required.")
    located, snapshot, limitations = read_jsonl([path], cancelled=cancelled)
    first = True
    agents = {}
    for _path, _line, event in located:
        if cancelled is not None and cancelled.is_set():
            raise ConversationError("read_cancelled", "The source read was cancelled.")
        data = event.get("data")
        if not isinstance(data, dict):
            if event.get("type") == "session.start":
                raise ConversationError("invalid_execution", "The session header is malformed.")
            limitations.append("malformed_session_events_skipped")
            continue
        if first or event.get("type") == "session.start":
            if event.get("type") != "session.start" or data.get("sessionId") != root_id:
                raise ConversationError(
                    "invalid_execution", "The source has conflicting session metadata."
                )
            first = False
        if event.get("type") == "subagent.started":
            events.add_agent(agents, event)
    if first:
        raise ConversationError("invalid_execution", "The source has no verified session header.")
    # Recheck the path too: replacing it with a symlink during the read cannot
    # authorize an arbitrary file, even if that file has a matching header.
    if events.path(root_id) != path:
        raise ConversationError("source_changed", "The session source changed; retry.")
    return located, snapshot, limitations, agents, events.valid_agents(root_id, agents)


def event_agent(event: dict, agents: dict) -> str | None:
    # A malformed execution marker must not silently become a root message.
    if event.get("agentId") is not None and not isinstance(event["agentId"], str):
        return None
    if event["data"].get("parentToolCallId") is not None and not isinstance(
        event["data"]["parentToolCallId"], str
    ):
        return None
    aid = event.get("agentId") or ""
    legacy = event["data"].get("parentToolCallId")
    if not aid and legacy:
        matches = [key for key, agent in agents.items() if agent.get("tool") == legacy]
        return matches[0] if len(matches) == 1 else None
    return aid


def manifest(store, root_id: str):
    if store.demo:
        return None
    path = store._events.path(root_id)
    return source_manifest([path]) if path is not None else None


def read_source(store, root_id: str, execution_id: str | None = None) -> dict:
    if store.demo:
        raise ConversationError("conversation_unavailable", "Conversation is disabled in demo.")
    selected = root_id if execution_id is None else execution_id
    if any(not isinstance(sid, str) or not sid for sid in (root_id, selected)):
        raise ConversationError("invalid_execution", "An exact session ID is required.")
    events = store._events
    located, snapshot, limitations, agents, valid = load_events(events, root_id)
    parents = {root_id: None}
    parents.update({a["id"]: node_id(root_id, a["parent"]) for a in valid.values()})
    executions = execution_tree(parents, root_id, selected)
    records = []
    for path, line, event in located:
        kind, data = event.get("type"), event.get("data")
        if kind not in ("user.message", "assistant.message") or not isinstance(data, dict):
            continue
        aid = event_agent(event, agents)
        if aid is None or (aid and aid not in valid):
            limitations.append("unowned_execution_messages_excluded")
            continue
        if node_id(root_id, aid) != selected:
            continue
        if event.get("ephemeral") is True:
            limitations.append("ephemeral_events_excluded")
            continue
        text = data.get("content")
        if not isinstance(text, str):
            limitations.append("non_text_content_excluded")
            continue
        origin = {"source_key": source_key(Path(path)), "line": line}
        record_id = f"copilot:{origin['source_key']}:{line}"
        mid, eid = data.get("messageId"), event.get("id")
        records.append(
            {
                "id": record_id,
                "message_id": mid if isinstance(mid, str) else None,
                "record_id": eid if isinstance(eid, str) else None,
                "parent_id": event.get("parentId")
                if isinstance(event.get("parentId"), str)
                else None,
                "execution_id": selected,
                "role": "user" if kind == "user.message" else "assistant",
                "timestamp": event.get("timestamp")
                if isinstance(event.get("timestamp"), str)
                else None,
                "origin": "recorded-message",
                "source": origin,
                "parts": [{"id": record_id + ":0", "text": text}] if text else [],
            }
        )
    limitations.extend(
        [
            "retained_messages_only",
            "non_text_parts_excluded",
            "Internal model events and streaming deltas are not conversation messages.",
            "Recorded occurrences are retained; rewinds and active branches are not reconstructed.",
            "User-role text may include harness or delegated instructions.",
        ]
    )
    return finish_source(
        records,
        selected,
        executions,
        limitations,
        "physical session event JSONL line order",
        binding=[snapshot, store.source_name],
    )
