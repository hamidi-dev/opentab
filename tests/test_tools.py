from opentab.accounting.tools import tool_calls_from_turns


def _turn(**changes):
    turn = {
        "time": "2026-09-12T12:00:00Z",
        "agent": "build",
        "depth": 1,
        "model_name": "claude-sonnet-4-5",
        "effort": "high",
        "prompt_id": "prompt-1",
        "prompt_title": "Implement it",
        "tools": ["Bash"],
        "cost": 1.0,
        "tokens_total": 100,
        "input": 40,
        "output": 20,
        "reasoning": 10,
        "cache_read": 20,
        "cache_write": 10,
        "cache_write_1h": 4,
    }
    turn.update(changes)
    return turn


def test_tool_calls_preserve_duplicate_calls_and_chronological_indices():
    calls = tool_calls_from_turns(
        [
            _turn(tools=["Bash", "Bash", "mcp__github__search"]),
            _turn(tools=[]),
            _turn(tools=["Read"], prompt_id="prompt-2"),
        ]
    )

    assert [call["tool"] for call in calls] == [
        "Bash",
        "Bash",
        "mcp__github__search",
        "Read",
    ]
    assert [call["index"] for call in calls] == [0, 1, 2, 3]
    assert [call["turn_index"] for call in calls] == [0, 0, 0, 2]
    assert [call["call_index"] for call in calls] == [0, 1, 2, 0]
    assert [call["namespace"] for call in calls] == [
        "(built-in)",
        "(built-in)",
        "github",
        "(built-in)",
    ]


def test_tool_calls_validate_shape_before_calculating_attribution():
    assert tool_calls_from_turns([_turn(tools="Bash")]) == []
    assert tool_calls_from_turns([_turn(tools={"tool": "Bash"})]) == []

    calls = tool_calls_from_turns([_turn(tools=[["bad"], "", None, "Bash", 3])])
    assert len(calls) == 1
    assert calls[0]["tool"] == "Bash"
    assert calls[0]["call_index"] == 0
    assert calls[0]["tokens_total"] == 100
    assert calls[0]["cost"] == 1.0


def test_tool_calls_split_every_arithmetic_field_across_valid_calls():
    calls = tool_calls_from_turns(
        [
            _turn(
                tools=["Bash", None, "Read", "Bash"],
                cost=1.0,
                tokens_total=99,
                input=42,
                output=21,
                reasoning=9,
                cache_read=15,
                cache_write=12,
                cache_write_1h=3,
            )
        ]
    )

    expected = {
        "cost": 1 / 3,
        "tokens_total": 33,
        "input": 14,
        "output": 7,
        "reasoning": 3,
        "cache_read": 5,
        "cache_write": 4,
        "cache_write_1h": 1,
    }
    for call in calls:
        for field, value in expected.items():
            assert call[field] == value
        assert call["cache_write_1h"] <= call["cache_write"]
    for field, value in expected.items():
        assert sum(call[field] for call in calls) == value * 3


def test_tool_calls_copy_only_allowed_metadata_and_numeric_fields():
    turn = _turn(
        tools=["serena_find_symbol"],
        content_key="secret-key",
        prompt_full="full private prompt",
        arguments={"path": "/private"},
        result="private output",
        status="completed",
        duration_ms=123,
    )

    (call,) = tool_calls_from_turns([turn])
    assert call == {
        "index": 0,
        "turn_index": 0,
        "call_index": 0,
        "tool": "serena_find_symbol",
        "namespace": "serena",
        "time": "2026-09-12T12:00:00Z",
        "agent": "build",
        "depth": 1,
        "model_name": "claude-sonnet-4-5",
        "effort": "high",
        "prompt_id": "prompt-1",
        "prompt_title": "Implement it",
        "cost": 1.0,
        "tokens_total": 100.0,
        "input": 40.0,
        "output": 20.0,
        "reasoning": 10.0,
        "cache_read": 20.0,
        "cache_write": 10.0,
        "cache_write_1h": 4.0,
    }
