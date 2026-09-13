"""Pure conversation-search layout tests: no App, renderer, or curses."""

from datetime import datetime

from opentab.formatting import display_width
from opentab.tui.search_layout import (
    SearchLine,
    conversation_layout,
    highlight_spans,
    snippet_lines,
)


def test_highlights_unicode61_style_whole_tokens_with_source_offsets():
    text = "CAFÉ cafe\u0301 cafeteria Straße 你好"
    assert highlight_spans(text, "cafe STRAẞE 你好") == [(0, 4), (5, 10), (21, 27), (28, 30)]
    assert highlight_spans("Straße", "STRASSE") == []
    assert highlight_spans("concatenate cat", "cat") == [(12, 15)]


def test_snippet_is_cell_bounded_sanitized_highlighted_and_clipped():
    lines = snippet_lines("\x1b[31mCafé\x1b[0m " + "界 alpha " * 8, 12, "cafe", 2)
    assert len(lines) == 2
    assert all(display_width(line.text) <= 12 for line in lines)
    assert "\x1b" not in "".join(line.text for line in lines)
    assert lines[0].highlights == [(0, 4)]
    assert lines[-1].text.endswith(("...", "…"))


def test_conversation_layout_preserves_code_blank_lines_and_anchors():
    response = {
        "execution_id": "root",
        "executions": [{"id": "root", "parent_id": None}],
        "records": [
            {
                "id": "r1",
                "role": "user",
                "timestamp": "2026-09-11T12:00:00Z",
                "record_complete": True,
                "parts": [{"text": "Intro café\n```python\n    print('a  b')\n\n```\nAfter"}],
            },
            {
                "id": "r2",
                "role": "assistant",
                "timestamp": 1789128000000,
                "record_complete": True,
                "parts": [{"text": "A very long 界 line that must wrap safely."}],
            },
        ],
        "limitations": [],
    }
    layout = conversation_layout(response, 28, "cafe")
    assert layout.anchors["r1"] < layout.anchors["r2"]
    assert layout.lines[layout.anchors["r1"]].role == "user"
    assert "2026-09-11" in layout.lines[layout.anchors["r1"]].text
    expected_epoch = datetime.fromtimestamp(1789128000).strftime("%Y-%m-%d %H:%M:%S")
    assert expected_epoch[:10] in layout.lines[layout.anchors["r2"]].text
    code = [line for line in layout.lines if line.role == "code"]
    assert any(line.text == "    print('a  b')" for line in code)
    assert any(line.text == "" for line in code)
    assert any(line.highlights for line in layout.lines)
    assert all(display_width(line.text) <= 28 for line in layout.lines)


def test_layout_labels_child_partial_pages_and_sanitizes_controls():
    response = {
        "execution_id": "child\x1b[31m",
        "executions": [
            {"id": "root", "parent_id": None},
            {"id": "child\x1b[31m", "parent_id": "root"},
        ],
        "limitations": ["retained_messages_only"],
        "has_earlier": True,
        "has_more": True,
        "records": [
            {
                "id": "record",
                "role": "assistant",
                "record_complete": False,
                "parts": [
                    {
                        "text": "middle\x00 text",
                        "text_offset": 4,
                        "text_total_chars": 30,
                        "truncated": True,
                    }
                ],
            }
        ],
    }
    layout = conversation_layout(response, 32)
    text = " ".join(line.text for line in layout.lines)
    assert "Child execution:" in text
    assert "Retained user/assistant" in text and "history completeness" in text
    assert "Earlier retained records" in text and "More retained records" in text
    assert "continues from character 5" in text
    assert "continues after character 16 of 30" in text
    assert "\x1b" not in text and "\x00" not in text


def test_empty_and_tiny_layouts_are_bounded_and_defaults_are_independent():
    assert conversation_layout(None, 80).lines == []
    assert conversation_layout({}, 80).anchors == {}
    first, second = SearchLine("a"), SearchLine("b")
    first.highlights.append((0, 1))
    assert second.highlights == []
    lines = snippet_lines("界界", 1, max_lines=3)
    assert [line.text for line in lines] == ["?", "?"]
    assert all(display_width(line.text) <= 1 for line in lines)
