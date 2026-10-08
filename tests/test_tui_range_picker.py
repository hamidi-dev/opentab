from datetime import datetime
from unittest.mock import patch

from opentab.tui import range_picker as picker_module
from opentab.tui.range_picker import (
    FOCUSES,
    PRESETS,
    RangePicker,
    preset_value,
    range_preview,
    resolved_bounds,
)


def test_presets_have_exact_inclusive_bounds_across_calendar_transitions():
    cases = (
        (
            datetime(2026, 10, 8),
            (
                (None, None),
                ("2026-10-08", "2026-10-08"),
                ("2026-10-07", "2026-10-07"),
                ("2026-10-02", "2026-10-08"),
                ("2026-09-09", "2026-10-08"),
                ("2026-10-01", "2026-10-31"),
                ("2026-09-01", "2026-09-30"),
                ("2026-01-01", "2026-12-31"),
            ),
        ),
        (
            datetime(2026, 1, 1),
            (
                (None, None),
                ("2026-01-01", "2026-01-01"),
                ("2025-12-31", "2025-12-31"),
                ("2025-12-26", "2026-01-01"),
                ("2025-12-03", "2026-01-01"),
                ("2026-01-01", "2026-01-31"),
                ("2025-12-01", "2025-12-31"),
                ("2026-01-01", "2026-12-31"),
            ),
        ),
        (
            datetime(2024, 3, 1),
            (
                (None, None),
                ("2024-03-01", "2024-03-01"),
                ("2024-02-29", "2024-02-29"),
                ("2024-02-24", "2024-03-01"),
                ("2024-02-01", "2024-03-01"),
                ("2024-03-01", "2024-03-31"),
                ("2024-02-01", "2024-02-29"),
                ("2024-01-01", "2024-12-31"),
            ),
        ),
        (
            datetime(2025, 3, 1),
            (
                (None, None),
                ("2025-03-01", "2025-03-01"),
                ("2025-02-28", "2025-02-28"),
                ("2025-02-23", "2025-03-01"),
                ("2025-01-31", "2025-03-01"),
                ("2025-03-01", "2025-03-31"),
                ("2025-02-01", "2025-02-28"),
                ("2025-01-01", "2025-12-31"),
            ),
        ),
        (
            datetime(2024, 2, 29),
            (
                (None, None),
                ("2024-02-29", "2024-02-29"),
                ("2024-02-28", "2024-02-28"),
                ("2024-02-23", "2024-02-29"),
                ("2024-01-31", "2024-02-29"),
                ("2024-02-01", "2024-02-29"),
                ("2024-01-01", "2024-01-31"),
                ("2024-01-01", "2024-12-31"),
            ),
        ),
        (
            datetime(2026, 12, 31),
            (
                (None, None),
                ("2026-12-31", "2026-12-31"),
                ("2026-12-30", "2026-12-30"),
                ("2026-12-25", "2026-12-31"),
                ("2026-12-02", "2026-12-31"),
                ("2026-12-01", "2026-12-31"),
                ("2026-11-01", "2026-11-30"),
                ("2026-01-01", "2026-12-31"),
            ),
        ),
    )
    assert PRESETS == (
        "All time",
        "Today",
        "Yesterday",
        "Last 7 days",
        "Last 30 days",
        "This month",
        "Last month",
        "This year",
    )
    for today, bounds in cases:
        for index, expected in enumerate(bounds):
            raw = preset_value(index, today)
            assert resolved_bounds(raw, today) == expected, (today, PRESETS[index])
            preview = "All time" if index == 0 else f"{expected[0]} → {expected[1]} (inclusive)"
            assert range_preview(raw, today) == preview, (today, PRESETS[index])


def test_current_marker_matches_committed_semantics_not_pending_selection():
    today = datetime(2024, 3, 1)
    for index in range(len(PRESETS)):
        picker = RangePicker(preset_value(index, today), today=today)
        picker.index = (index + 1) % len(PRESETS)
        assert [i for i in range(len(PRESETS)) if picker.is_current(i)] == [index]
    for raw in ("7d", "1m", "1y", "2024-02-15..2024-02-29", "..2024-03-01"):
        picker = RangePicker(raw, today=today)
        assert not any(picker.is_current(i) for i in range(len(PRESETS))), raw
    # Equivalent explicit calendar bounds still mark the calendar preset.
    assert RangePicker("2024-02-01..2024-02-29", today=today).is_current(6)


def test_open_focuses_seeded_expression_and_initializes_preset_and_dates():
    with patch.object(picker_module, "preset_value", wraps=preset_value) as preset:
        picker = RangePicker.open("2024-02-15..")
        assert picker.focus == "expression" and picker.fresh
        assert picker.expression == "2024-02-15.." and picker.value() == "2024-02-15.."
        assert picker.index == 0 and picker.since == "2024-02-15" and picker.until == ""
        assert all(call.args[1] is picker.today for call in preset.call_args_list)
    picker = RangePicker.open("7d")
    assert (picker.since, picker.until) == tuple(
        value or "" for value in resolved_bounds("7d", picker.today)
    )
    picker = RangePicker.open("all")
    assert picker.expression == "" and not picker.fresh and picker.is_current(picker.index)
    assert picker.value() == "" and picker.preview() == "All time"


def test_focus_cycles_in_tab_order_and_picks_the_applied_value():
    picker = RangePicker("all", index=7, expression="2m", since="2024-01-01", until="")
    picker.today, picker.error = datetime(2024, 3, 1), "stale"
    seen = []
    for _ in FOCUSES:
        seen.append((picker.focus, picker.value()))
        picker.cycle(1)
    assert seen == [
        ("expression", "2m"),
        ("presets", "2024"),
        ("since", "2024-01-01.."),
        ("until", "2024-01-01.."),
    ]
    assert picker.focus == "expression" and not picker.error
    picker.cycle(-1)
    assert picker.focus == "until"


def test_quick_previews_preserve_legacy_cutoffs_and_open_endpoint_labels():
    today = datetime(2026, 1, 3)
    assert range_preview("7d", today) == "2025-12-27 → No end (inclusive)"
    assert range_preview("2026-01-01", today) == "2026-01-01 → No end (inclusive)"
    assert range_preview("..2026-01-03", today) == "Any start → 2026-01-03 (inclusive)"
    assert range_preview("2m", today) == "2025-12-01 → No end (inclusive)"


def test_invalid_picker_validation_retains_fields_and_recovers():
    picker = RangePicker("all", focus="since", since="2025-02-29", until="2025-03-01")
    assert not picker.valid() and picker.error
    assert picker.since == "2025-02-29" and picker.until == "2025-03-01"
    assert picker.preview().startswith("Invalid:")
    picker.since = "2024-02-29"
    assert picker.valid() and not picker.error
    picker.focus, picker.expression = "expression", "banana"
    assert not picker.valid() and picker.expression == "banana"
    picker.expression = "..2024-02-29"
    assert picker.valid() and not picker.error


def test_pending_preset_preview_does_not_roll_forward_if_clock_crosses_midnight():
    picker = RangePicker("all", index=2, focus="presets", today=datetime(2024, 3, 1, 23, 59))
    with patch.object(picker_module, "datetime") as clock:
        clock.now.return_value = datetime(2024, 3, 2)
        assert picker.value() == "2024-02-29..2024-02-29"
        assert picker.preview() == "2024-02-29 → 2024-02-29 (inclusive)"
        assert picker.valid()
        clock.now.assert_not_called()
