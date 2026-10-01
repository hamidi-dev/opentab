"""Read-only, session-bound Copilot CLI event enrichment.

OTEL remains the usage ledger. Persisted events supply exact response/execution
identities; prompts, arguments and results are read only for session details and
never retained in accounting rollups. Internal model.* events (including the
title generator) are not assistant conversation records.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

from opentab.accounting.pricing import canonical_model
from opentab.presentation.formatting import _clean_prompt
from opentab.util import TRACE_OUTPUT_CAP, TRACE_TEXT_CAP, TraceContent


def _string(value) -> str:
    return value if isinstance(value, str) else ""


def call_key(session: str, response: str) -> str:
    return "copilot:" + hashlib.sha256(json.dumps([session, response]).encode("utf-8")).hexdigest()


def node_id(session: str, agent: str) -> str:
    return (
        session
        if not agent
        else "copilot-node:"
        + hashlib.sha256(json.dumps([session, agent]).encode("utf-8")).hexdigest()
    )


def _tools(data: dict) -> list[dict]:
    requests = data.get("toolRequests")
    if not isinstance(requests, list):
        return []
    return [r for r in requests if isinstance(r, dict) and _string(r.get("name"))]


def _tool_name(request: dict) -> str:
    server = _string(request.get("mcpServerName"))
    tool = _string(request.get("mcpToolName"))
    return f"mcp__{server}__{tool}" if server and tool else request["name"]


class CopilotEvents:
    def __init__(self, home: str):
        self.root = os.path.join(os.path.realpath(home), "session-state")

    def path(self, session: str) -> str | None:
        if not session or session in (".", "..") or any(c in session for c in "/\\\0"):
            return None
        path = os.path.join(self.root, session, "events.jsonl")
        # A symlink must not turn a session ID into an arbitrary content reader.
        if os.path.realpath(path) != os.path.abspath(path):
            return None
        return path

    def records(self, session: str, kinds: set[str] | None = None):
        path = self.path(session)
        if path is None:
            return
        try:
            source = open(path, encoding="utf-8", errors="replace")
        except OSError:
            return
        with source:
            first = True
            seen = set()
            for line in source:
                # Metadata passes need only these event kinds, not model snapshots,
                # system messages, tool results or other potentially huge bodies.
                if (
                    not first
                    and kinds is not None
                    and not any(f'"{kind}"' in line for kind in kinds)
                ):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                    continue
                if first:
                    if (
                        event.get("type") != "session.start"
                        or event["data"].get("sessionId") != session
                    ):
                        return
                    first = False
                if kinds is not None and event.get("type") not in kinds:
                    continue
                eid = _string(event.get("id"))
                if eid and eid in seen:
                    continue
                if eid:
                    seen.add(eid)
                yield event

    @staticmethod
    def agent(event: dict, agents: dict) -> str:
        aid = _string(event.get("agentId"))
        legacy = _string(event["data"].get("parentToolCallId"))
        if not aid and legacy:
            matches = [key for key, a in agents.items() if a.get("tool") == legacy]
            return matches[0] if len(matches) == 1 else "?"
        return aid

    @staticmethod
    def add_agent(agents: dict, event: dict) -> None:
        data = event["data"]
        aid = _string(event.get("agentId")) or _string(data.get("toolCallId"))
        if not aid:
            return
        row = {
            "name": _string(data.get("agentDisplayName"))
            or _string(data.get("agentName"))
            or "subagent",
            "parent": _string(data.get("parentId")),
            "tool": _string(data.get("toolCallId")),
            "created": _string(event.get("timestamp")),
        }
        # Invalid identity metadata cannot be coerced into root ownership. Keep
        # the identity invalid so its descendants fail the same ancestry check.
        if any(
            value is not None and not isinstance(value, str)
            for value in (event.get("agentId"), data.get("parentId"), data.get("toolCallId"))
        ):
            row["parent"] = aid
        if aid in agents and agents[aid] != row:
            agents[aid]["parent"] = aid  # conflicting identity fails closed
        else:
            agents[aid] = row

    @staticmethod
    def valid_agents(session: str, agents: dict) -> dict:
        valid = {}
        for aid, agent in agents.items():
            parent, chain = aid, set()
            while parent and parent in agents and parent not in chain:
                chain.add(parent)
                parent = agents[parent]["parent"]
            if not parent:
                valid[aid] = dict(agent, depth=len(chain), id=node_id(session, aid))
        return valid

    def metadata(self, session: str) -> tuple[dict, dict]:
        """Scalar ownership only; no prompt, reasoning, argument or result retention."""
        agents = {}
        messages = []
        for event in self.records(session, {"subagent.started", "assistant.message"}):
            data = event["data"]
            if event["type"] == "subagent.started":
                self.add_agent(agents, event)
            else:
                response = _string(data.get("apiCallId"))
                if response:
                    # Hold only identity fields while resolving legacy parent tool IDs.
                    messages.append(
                        (
                            call_key(session, response),
                            {
                                "agentId": _string(event.get("agentId")),
                                "data": {"parentToolCallId": data.get("parentToolCallId")},
                            },
                            canonical_model(_string(data.get("model"))),
                        )
                    )
        valid = self.valid_agents(session, agents)
        ownership = {}
        for key, event, model in messages:
            aid = self.agent(event, agents)
            identity = (aid, model)
            if aid and aid not in valid:
                identity = None
            if key not in ownership:
                ownership[key] = identity
            else:
                previous = ownership[key]
                if (
                    previous is None
                    or identity is None
                    or previous[0] != aid
                    or (previous[1] and model and previous[1] != model)
                ):
                    ownership[key] = None
                else:
                    ownership[key] = (aid, previous[1] or model)
        return valid, ownership

    @staticmethod
    def _reasoning(data: dict) -> list[str]:
        text = _string(data.get("reasoningText"))
        if text:
            return [text]
        blocks = data.get("reasoningBlocks")
        if isinstance(blocks, dict):
            blocks = blocks.get("blocks")
        if not isinstance(blocks, list):
            return []
        result = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text = _string(block.get("thinking")) or _string(block.get("text"))
            if text:
                result.append(text)
            summary = block.get("summary")
            if isinstance(summary, list):
                result.extend(
                    s["text"] for s in summary if isinstance(s, dict) and _string(s.get("text"))
                )
        return result

    def details(
        self,
        session: str,
        turns: list[dict],
        *,
        content_key: str | None = None,
        trace: bool = False,
        execution: str | None = None,
    ) -> tuple[list[dict], dict]:
        agents, ownership = self.metadata(session)
        rows = {
            t["content_key"]: dict(
                t,
                tools=[],
                has_text=False,
                has_reasoning=False,
                prompt_id="",
                prompt_title="",
                prompt_full="",
            )
            for t in turns
            if t.get("content_key")
        }
        prompts, current, inherited, effort = {}, {}, {}, {}
        calls = {}
        output = TraceContent(content_key)
        for event in self.records(
            session,
            {
                "session.model_change",
                "subagent.started",
                "user.message",
                "assistant.message",
                "tool.user_requested",
                "tool.execution_start",
                "tool.execution_complete",
            },
        ):
            data, kind = event["data"], event.get("type")
            aid = self.agent(event, agents)
            if kind == "session.model_change":
                effort[aid] = _string(data.get("reasoningEffort"))
            elif kind == "subagent.started":
                child = _string(event.get("agentId")) or _string(data.get("toolCallId"))
                parent = _string(data.get("parentId"))
                inherited[child] = inherited.get(parent) or current.get(parent) or current.get("")
            elif kind == "user.message":
                mid = _string(data.get("messageId")) or _string(event.get("id"))
                text = _string(data.get("content"))
                prompt = (mid, _clean_prompt(text), text)
                if mid and text:
                    prompts[(aid, mid)] = current[aid] = prompt
            elif kind == "assistant.message":
                key = call_key(session, _string(data.get("apiCallId")))
                requests = _tools(data)
                # A newer request claims a reused tool ID even when that call is
                # unmatched or outside the selected trace. Never borrow its result.
                for request in requests:
                    calls.pop((aid, _string(request.get("toolCallId"))), None)
                row = rows.get(key)
                owner = ownership.get(key)
                if (
                    row is None
                    or owner is None
                    or owner[0] != aid
                    or (owner[1] and canonical_model(row["model_name"]) != owner[1])
                    or node_id(session, aid) != row.get("execution_id", session)
                ):
                    continue
                if execution is not None and row.get("execution_id", session) != execution:
                    continue
                originating = _string(data.get("originatingMessageId"))
                prompt = prompts.get((aid, originating)) if originating else current.get(aid)
                if aid and execution is None:
                    prompt = inherited.get(aid)
                if prompt:
                    row["prompt_id"], row["prompt_title"], row["prompt_full"] = prompt
                if effort.get(aid):
                    row["effort"] = effort[aid]
                text, reasoning = _string(data.get("content")), self._reasoning(data)
                row["has_text"] |= bool(text)
                row["has_reasoning"] |= bool(reasoning)
                row["tools"].extend(_tool_name(r) for r in requests)
                if not trace or not output.accepts(key):
                    continue
                events = output.setdefault(key, [])
                for name, value in [("reasoning", r) for r in reasoning] + [("text", text)]:
                    if value and len(events) < output.event_limit:
                        clipped, dropped = output.clip(value, TRACE_TEXT_CAP)
                        events.append({"kind": name, "text": clipped, "dropped": dropped})
                for request in requests:
                    if len(events) >= output.event_limit:
                        break
                    head, params = output.arguments(request.get("arguments"))
                    item = {
                        "kind": "tool",
                        "name": _tool_name(request),
                        "args": head,
                        "params": params,
                        "output": "",
                        "output_dropped": 0,
                    }
                    events.append(item)
                    tcid = _string(request.get("toolCallId"))
                    if tcid:
                        calls[(aid, tcid)] = (item, False, _string(data.get("turnId")))
            elif kind in ("tool.user_requested", "tool.execution_start"):
                key = (aid, _string(data.get("toolCallId")))
                pending = calls.get(key)
                turn = _string(data.get("turnId"))
                if pending is not None and turn and pending[2] and turn != pending[2]:
                    continue
                name = _tool_name(dict(data, name=_string(data.get("toolName"))))
                if (
                    kind == "tool.user_requested"
                    or pending is None
                    or pending[1]
                    or name != pending[0]["name"]
                ):
                    calls.pop(key, None)
                else:
                    calls[key] = (pending[0], True, pending[2])
            elif kind == "tool.execution_complete":
                key = (aid, _string(data.get("toolCallId")))
                pending = calls.get(key)
                turn = _string(data.get("turnId"))
                if pending is not None and turn and pending[2] and turn != pending[2]:
                    continue
                calls.pop(key, None)
                if pending is not None:
                    result = data.get("result")
                    result = result if isinstance(result, dict) else {}
                    error = data.get("error")
                    error = error if isinstance(error, dict) else {}
                    text = (
                        _string(result.get("detailedContent"))
                        or _string(result.get("content"))
                        or _string(error.get("message"))
                    )
                    item = pending[0]
                    item["output"], item["output_dropped"] = output.clip(text, TRACE_OUTPUT_CAP)
                    item["status"] = "completed" if data.get("success") else "error"
        return [
            rows.get(
                t.get("content_key"),
                dict(
                    t,
                    prompt_id="",
                    prompt_title="",
                    prompt_full="",
                    tools=[],
                    has_text=False,
                    has_reasoning=False,
                ),
            )
            for t in turns
        ], output

    def prompt(self, session: str, execution: str) -> str | None:
        agents, _ownership = self.metadata(session)
        matched = [aid for aid, a in agents.items() if a["id"] == execution]
        if len(matched) != 1:
            return None
        aid = matched[0]
        # Prefer the child's actual received user event over the parent's task hint.
        for event in self.records(session, {"user.message"}):
            if self.agent(event, agents) == aid and _string(event["data"].get("content")):
                return event["data"]["content"]
        tool = agents[aid]["tool"]
        parent = agents[aid]["parent"]
        values = []
        for event in self.records(session, {"assistant.message"}):
            if self.agent(event, agents) != parent:
                continue
            for request in _tools(event["data"]):
                args = request.get("arguments")
                if request.get("toolCallId") == tool and isinstance(args, dict):
                    values.append(_string(args.get("prompt")))
        return values[0] if len(values) == 1 and values[0] else None

    def context(self, session: str) -> list[dict]:
        rows = []
        for event in self.records(session, {"session.shutdown"}):
            data = event["data"]
            rows = []
            for field, category, name in (
                ("systemTokens", "System", "instructions"),
                ("toolDefinitionsTokens", "Tools", "definitions"),
                ("conversationTokens", "Messages", "conversation"),
            ):
                value = data.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    rows.append(
                        {
                            "category": category,
                            "kind": name,
                            "count": 1,
                            "est_tokens": int(value),
                            "basis": "reported_snapshot",
                        }
                    )
        return sorted(rows, key=lambda r: r["est_tokens"], reverse=True)


def local_time(value: str) -> str:
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except (ValueError, OverflowError, OSError):
        return ""
