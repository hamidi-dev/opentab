from unittest.mock import patch

from opentab.tui.components.boxes import TABLE_GLYPHS
from opentab.tui.views.turns import (
    build_turn_drill,
    build_turn_trace,
    build_turns,
    turn_group_rows,
)


def _rows():
    return [
        {
            "time": "2026-09-13 10:00:00",
            "agent": "-",
            "depth": 0,
            "model_name": "anthropic/claude-sonnet-5",
            "cost": 1.0,
            "input": 100,
            "output": 20,
            "reasoning": 5,
            "cache_read": 80,
            "cache_write": 10,
            "tokens_total": 215,
            "prompt_id": "p1",
            "prompt_title": "inspect the reader",
            "prompt_full": "Inspect the reader without touching application state.",
            "has_text": True,
            "tools": ["Read"],
        },
        {
            "time": "2026-09-13 10:00:01",
            "agent": "explore",
            "depth": 1,
            "model_name": "anthropic/claude-haiku-4-5",
            "cost": 0.5,
            "input": 20,
            "output": 10,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "tokens_total": 30,
            "prompt_id": "p1",
            "prompt_title": "inspect the reader",
            "has_reasoning": True,
            "tools": [],
        },
        {
            "time": "2026-09-13 10:01:00",
            "agent": "-",
            "depth": 0,
            "model_name": "anthropic/claude-sonnet-5",
            "cost": 2.0,
            "input": 50,
            "output": 25,
            "reasoning": 0,
            "cache_read": 25,
            "cache_write": 0,
            "tokens_total": 100,
            "prompt_id": "p2",
            "prompt_title": "extract it",
            "prompt_full": "Extract the complete presentation.",
            "has_text": True,
            "tools": [],
        },
    ]


def test_turn_overview_returns_lines_headers_and_prompt_geometry_without_state():
    rows = _rows()
    with patch(
        "opentab.pricing.api_equivalent_cost", side_effect=AssertionError("view repriced rows")
    ), patch("opentab.pricing.cache_misses", side_effect=AssertionError("view scanned rows")):
        layout = build_turns(
            rows=rows,
            costs=[1.0, 0.5, 2.0],
            width=110,
            compactions={},
            cache_events=(),
            scoped=False,
            glyphs=TABLE_GLYPHS,
        )

    assert "2 prompts · 3 turns · $3.50" in "\n".join(layout.lines)
    assert len(layout.row_map) == 2
    assert layout.cursor_lines == {ordinal: line for line, ordinal in layout.row_map.items()}
    assert layout.box_headers <= set(layout.lines)
    assert "inspect the reader" in layout.lines[layout.cursor_lines[0]]
    assert "extract it" in layout.lines[layout.cursor_lines[1]]


def test_prompt_drill_returns_token_spans_and_turn_row_geometry():
    rows = _rows()
    costs = [1.0, 0.5, 2.0]
    groups = turn_group_rows(rows, costs)
    layout = build_turn_drill(
        rows=rows,
        costs=costs,
        groups=groups,
        drill=0,
        width=120,
        context_curve=True,
        traceable=True,
        scoped=False,
        glyphs=TABLE_GLYPHS,
        colored=True,
    )

    text = "\n".join(layout.lines)
    assert "Inspect the reader without touching application state." in text
    assert "Prompt token breakdown" in text and "peak turn $1.00" in text
    assert layout.token_runs
    assert layout.row_map and set(layout.row_map.values()) == {0, 1}
    assert layout.box_headers


def test_trace_shell_returns_event_geometry_separately_from_lines():
    rows = _rows()
    layout = build_turn_trace(
        rows=rows,
        index=0,
        siblings=[0, 1],
        events=[{"kind": "tool", "name": "Read", "args": "README", "output": "ok"}],
        width=90,
        cost=1.0,
        glyphs=TABLE_GLYPHS,
        colored=True,
        scoped=False,
        remote_machine=None,
        loading=False,
        expanded=False,
        open_outputs=frozenset(),
        full_events=None,
        select_key="Enter",
        supports_trace=True,
        unavailable_reason=None,
        remote_error=None,
        records_reasoning=True,
    )

    assert layout.lines[0] == "Turn 1 of 2 · inspect the reader"
    assert "Output · preview · Enter expand" in "\n".join(layout.lines)
    assert layout.tool_lines and layout.output_ends == sorted(layout.output_ends)
    assert layout.token_runs
