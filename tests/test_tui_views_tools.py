"""Pure layout tests for the Tools explorer view."""

from opentab.tui.views.tools import ToolsOptions, build_tools_layout, tool_treemap_layout


def _usage(calls, cost, tokens):
    return {
        "calls": calls,
        "cost": cost,
        "tokens_total": tokens,
        "input": tokens * 0.4,
        "output": tokens * 0.2,
        "reasoning": tokens * 0.1,
        "cache_read": tokens * 0.2,
        "cache_write": tokens * 0.1,
        "cache_write_1h": tokens * 0.05,
    }


def _projection():
    read = {"kind": "tool", "name": "Read", **_usage(2, 2.0, 200)}
    bash = {"kind": "tool", "name": "Bash", **_usage(1, 3.0, 300)}
    namespace = {"kind": "namespace", "name": "(built-in)", **_usage(3, 5.0, 500)}
    calls = [
        {
            "tool": "Read",
            "namespace": "(built-in)",
            "turn_index": index,
            "time": f"2026-06-01 12:00:0{index}",
            "agent": "build",
            "depth": 0,
            "model_name": "anthropic/example",
            **_usage(1, 1.0, 100),
        }
        for index in range(2)
    ]
    return {
        "key": ("snapshot",),
        "rankings": [bash, read, namespace],
        "calls": calls,
        "models": {
            ("tool", "Read"): {"anthropic/example": _usage(2, 2.0, 200)},
            ("tool", "Bash"): {"anthropic/example": _usage(1, 3.0, 300)},
            ("namespace", "(built-in)"): {"anthropic/example": _usage(3, 5.0, 500)},
        },
    }


def test_tools_layout_builds_text_headers_rows_and_heat_from_explicit_projection():
    projection = _projection()
    layout = build_tools_layout(
        projection,
        100,
        ToolsOptions(select_label="v", api_price_label="p", tool_heat_colored=True),
    )

    text = "\n".join(layout.lines)
    assert "Tool-attributed spend · $5.00" in text
    assert "v / double-click inspects" in text
    assert set(layout.row_map.values()) == {0, 1, 2}
    assert layout.box_headers
    assert all(header in layout.lines for header in layout.box_headers)
    assert layout.heat_spans
    assert layout.call_map == {}


def test_tools_detail_returns_calls_and_token_spans_without_cursor_input():
    projection = _projection()
    options = ToolsOptions(
        drill=("tool", "Read"),
        select_label="open",
        back_label="leave",
        supports_turns=True,
    )
    layout = build_tools_layout(projection, 120, options)

    text = "\n".join(layout.lines)
    assert "leave: back to rankings" in text
    assert "calls complete: 2/2" in text
    assert "open / double-click opens" in text
    assert set(layout.call_map.values()) == {0, 1}
    assert layout.row_map == {}
    assert layout.token_spans
    assert layout.heat_spans == {}


def test_tools_capabilities_short_circuit_without_a_projection():
    unsupported = build_tools_layout(None, 80, ToolsOptions(supports_tools=False))
    empty = build_tools_layout(None, 80, ToolsOptions(has_tool_rows=False))

    assert unsupported.lines == (
        "# Tools",
        "This session's tool doesn't record per-tool attribution.",
    )
    assert empty.lines == ("# Tools", "No tool calls recorded for this session.")


def test_tool_treemap_fallback_is_pure_and_reports_local_heat_geometry():
    layout = tool_treemap_layout(
        {
            "Read": {"cost": 0.0, "tokens": 600, "calls": 6},
            "Bash": {"cost": 0.0, "tokens": 400, "calls": 1},
        },
        80,
        ToolsOptions(api_price_label="P", unicode=False, tool_heat_colored=False),
    )

    text = "\n".join(layout.lines)
    assert "area = tokens (no recorded cost) · shade = tokens/call" in text
    assert "press P for list-price spend" in text
    assert any(character in text for character in ".:*#")
    assert layout.heat_spans
