"""Shared projections for per-tool-call accounting."""

from collections.abc import Iterable

from opentab.util import tool_names, tool_namespace

_ARITHMETIC_FIELDS = (
    "cost",
    "tokens_total",
    "input",
    "output",
    "reasoning",
    "cache_read",
    "cache_write",
    "cache_write_1h",
)


def tool_calls_from_turns(turns: Iterable[dict]) -> list[dict]:
    """Flatten turns into chronological calls with evenly attributed usage."""
    calls = []
    for turn_index, turn in enumerate(turns):
        tools = tool_names(turn.get("tools"))
        if not tools:
            continue
        share_count = len(tools)
        for call_index, tool in enumerate(tools):
            call = {
                "index": len(calls),
                "turn_index": turn_index,
                "call_index": call_index,
                "tool": tool,
                "namespace": tool_namespace(tool),
                "time": turn.get("time"),
                "agent": turn.get("agent"),
                "depth": turn.get("depth"),
                "model_name": turn.get("model_name"),
                "effort": turn.get("effort"),
                "prompt_id": turn.get("prompt_id"),
                "prompt_title": turn.get("prompt_title"),
            }
            for field in _ARITHMETIC_FIELDS:
                call[field] = (turn.get(field) or 0) / share_count
            calls.append(call)
    return calls
