"""Pure trace formatting tests: no App, store, or curses setup."""

from opentab.presentation.formatting import display_width
from opentab.tui.trace import build_event_body, format_output, format_prose, output_target


def test_trace_prose_wraps_words_but_fenced_code_keeps_spaces():
    prose = "alpha beta gamma delta " * 8
    lines = format_prose({"text": prose}, 36)
    assert " ".join(line.strip() for line in lines) == prose.strip()
    code = "```python\n    print('a  b')\n```"
    lines = format_prose({"text": code}, 36)
    assert "      print('a  b')" in lines


def test_trace_output_preview_budgets_screen_rows_and_full_output_is_faithful():
    output = "  " + "界  $1 **raw** " * 60 + "\n\n    \nlast"
    event = {"output": output, "output_dropped": 90000}
    preview = format_output(event, 40)
    assert all(display_width(line) <= 40 for line in preview)
    assert len(preview) < 10 and "90,000 more characters" in " ".join(
        line.removeprefix("│").strip() for line in preview
    )
    full = format_output({"output": " a  b\n\n    \n$1 **raw**"}, 80, expanded=True)
    assert full == ["│   a  b", "│", "│      ", "│  $1 **raw**"]


def test_trace_output_reports_dropped_characters_when_nothing_was_retained():
    assert format_output({"output": "", "output_dropped": 90000}, 80) == [
        "│  … 90,000 more characters"
    ]


def test_trace_markdown_headings_are_readable_but_code_and_output_are_raw():
    event = {
        "kind": "reasoning",
        "text": "**Inspecting the renderer**\n## Next step\n```python\n    print('**raw**')\n```\nUse **tests** to verify.",
    }
    lines = format_prose(event, 80)
    assert lines[0] == "  Inspecting the renderer" and lines[0].role == "heading"
    assert lines[1] == "  Next step" and lines[1].role == "heading"
    assert "      print('**raw**')" in lines and "  Use tests to verify." in lines
    assert "│  ## Raw **output**" in format_output({"output": "## Raw **output**"}, 80)
    # A shorter fence inside a longer one is code, not the end of the block.
    lines = format_prose({"text": "````\n```\n**literal**\n````"}, 80)
    assert "  ```" in lines and "  **literal**" in lines


def test_event_layout_has_explicit_expansion_and_absolute_output_targets():
    preview = [
        {"kind": "text", "text": "Intro"},
        {"kind": "tool", "name": "shell", "output": "short\n…"},
        {"kind": "tool", "name": "shell", "output": "preview"},
    ]
    full = [preview[0], preview[1], {**preview[2], "output": "full result"}]
    layout = build_event_body(
        preview,
        60,
        line_offset=17,
        open_outputs=frozenset({2}),
        full_events=full,
        select_key="Enter",
    )
    text = "\n".join(layout.lines)
    assert "│  short" in text and "│  full result" in text
    assert "Output · preview · Enter expand" in text
    assert "Output · full · Enter collapse" in text
    assert layout.output_ends == sorted(layout.output_ends)
    assert min(layout.tool_lines) >= 17
    first_end, first_event = layout.output_ends[0]
    second_end, second_event = layout.output_ends[1]
    assert output_target(layout.output_ends, first_end) == first_event
    assert output_target(layout.output_ends, first_end + 1) == second_event
    assert output_target(layout.output_ends, second_end + 1) is None
