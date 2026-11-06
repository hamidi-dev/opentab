from __future__ import annotations

from unittest.mock import patch

from opentab.annotations import MATCH_ANY, SessionTags, TagFilter, aggregate_tags, session_key
from opentab.persistence.session_tags import TagError, TagSnapshot


def test_suppressed_annotations_do_not_read_custom_workflow_configuration():
    tags = SessionTags()
    tags.enabled = True
    tags.snapshot = TagSnapshot(labels={"private": "authored label"})
    with patch("opentab.annotations.load_workflow_config", side_effect=AssertionError("read")):
        tags.refresh(allowed=False)
        try:
            tags.create("blocked")
        except TagError as exc:
            assert exc.code == "disabled"
        else:
            raise AssertionError("suppressed mutation succeeded")
    assert not tags.enabled and not tags.snapshot.labels and not tags.error


def test_overlapping_tag_totals_and_union_intersection_count_roots_once():
    a, b = session_key("box", "codex", "a"), session_key("box", "codex", "b")
    snap = TagSnapshot(
        labels={"x": "X", "y": "Y", "empty": "Empty"},
        assignments={a: (("x", "y"), 1), b: (("y",), 1)},
    )
    rows = [(a, 5.0, 60.0), (b, 3.0, None), (a, 999.0, 999.0)]
    totals = {row.tag_id: row for row in aggregate_tags(rows, snap)}
    assert totals["x"].cost == 5 and totals["x"].sessions == 1
    assert totals["y"].cost == 8 and totals["y"].sessions == 2
    assert totals["y"].session_keys == (a, b)
    assert totals["y"].agent_seconds == 60 and totals["y"].time_measured == 1
    assert totals["y"].cost_measured == 2
    assert totals["empty"].sessions == totals[None].sessions == 0
    for mode, expected in ((MATCH_ANY, 8), ("all", 5)):
        predicate = TagFilter(mode, frozenset({"x", "y"}))
        selected = [row for row in rows if predicate.matches(snap.tags_of(row[0]))]
        union = {key: cost for key, cost, _ in reversed(selected)}
        assert sum(union.values()) == expected


def test_projection_preserves_qualified_distinctness_and_measurement_coverage():
    keys = [
        session_key("box", "codex", "same"),
        session_key("box", "claude-code", "same"),
        session_key("other-box", "codex", "same"),
    ]
    snap = TagSnapshot(
        labels={"x": "X"},
        assignments={
            keys[0]: (("x", "gone"), 1),
            keys[1]: (("x",), 1),
        },
    )
    totals = {
        r.tag_id: r
        for r in aggregate_tags(
            [(keys[0], None, 100.0), (keys[1], 0.0, 100.0), (keys[2], 4.0, 0.0)], snap
        )
    }
    assert totals["x"].sessions == 2 and totals["x"].cost_measured == 1
    assert totals["x"].cost == 0 and totals["x"].agent_seconds == 200
    assert totals["x"].time_measured == 2  # parallel agent time is additive
    assert totals["gone"].label == "?gone" and totals["gone"].session_keys == (keys[0],)
    assert totals[None].label == "Untagged" and totals[None].session_keys == (keys[2],)
    assert totals[None].time_measured == 1  # measured zero differs from missing


def test_projection_uses_supplied_cost_mode_and_scoped_rows_only():
    a, absent = session_key("box", "codex", "a"), session_key("box", "codex", "absent")
    snap = TagSnapshot(
        labels={"x": "X"},
        assignments={
            a: (("x",), 1),
            absent: (("x",), 1),
        },
    )
    recorded = aggregate_tags([(a, None, None)], snap)[0]
    estimated = aggregate_tags([(a, 7.5, None)], snap)[0]
    assert recorded.cost == 0 and recorded.cost_measured == 0
    assert estimated.cost == 7.5 and estimated.cost_measured == 1
    assert estimated.session_keys == (a,) and estimated.sessions == 1
    assert estimated.time_measured == 0
