from opentab.tui.components.bars import positioned_label_line
from opentab.tui.components.charts import bar_chart, treemap_rects


def test_bar_chart_returns_lines_and_local_hit_geometry():
    layout = bar_chart(
        [("one", 1.0), ("two", 2.0), ("three", 3.0)],
        48,
        12,
        keys=("k1", "k2", "k3"),
        selected="k2",
    )
    assert any("█" in line for line in layout.lines)
    assert any("peak $3.00 on three" in line for line in layout.lines)
    assert any("▲" in line and "k2 · $2.00" in line for line in layout.lines)
    assert layout.slots is not None
    assert [slot[2] for slot in layout.slots] == ["k1", "k2", "k3"]
    assert layout.click_rows < len(layout.lines)


def test_bar_chart_reports_unavailable_and_zero_views_without_geometry_lies():
    unavailable = bar_chart([], 80, 12)
    assert unavailable.lines == ("Not enough room to chart.",)
    assert unavailable.slots is None and unavailable.click_rows == 0
    zero = bar_chart([("Mon", 0.0), ("Tue", 0.0)], 40, 10)
    assert any("no spend in view" in line for line in zero.lines)
    assert not any("peak" in line for line in zero.lines)


def test_treemap_geometry_partitions_the_canvas_and_collapses_a_runt_canvas():
    rects = treemap_rects([("Bash", 6), ("Edit", 3), ("Read", 1)], 60, 10)
    areas = {name: width * height for name, _value, _x, _y, width, height in rects}
    assert sum(areas.values()) == 600
    assert areas["Bash"] > areas["Edit"] > areas["Read"]
    assert treemap_rects([("a", 8), ("b", 1), ("c", 1)], 1, 1) == [("Other", 10.0, 0, 0, 1, 1)]


def test_positioned_labels_return_color_spans_and_placed_indices():
    line, placed = positioned_label_line([("root", 0), ("too-wide", 1), ("docs", 2)], [8, 3, 6])
    assert line.text == "root       docs"
    assert placed == [0, 2]
    assert [(span.column, span.length, span.slot) for span in line.spans] == [
        (0, 4, 0),
        (11, 4, 2),
    ]
