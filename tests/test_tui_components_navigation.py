from opentab.tui.components.navigation import (
    keybar_layout,
    pager_layout,
    scrollbar_layout,
    scrollbar_thumb,
    tab_strip_layout,
    viewport_geometry,
)


def test_tab_strip_centers_selection_and_returns_local_hit_geometry():
    layout = tab_strip_layout(("One", "Two", "Three"), 1, 31, center=True)

    assert [(span.x, span.text, span.style) for span in layout.spans] == [
        (5, " One ", "inactive"),
        (10, "  ", "separator"),
        (12, "[Two]", "active"),
        (17, "  ", "separator"),
        (19, " Three ", "inactive"),
    ]
    assert [(hit.x0, hit.x1, hit.index) for hit in layout.hits] == [
        (5, 9, 0),
        (12, 16, 1),
        (19, 25, 2),
    ]


def test_ruled_tabs_clip_without_partial_extra_hits_and_disable_clicks():
    ruled = tab_strip_layout(("One", "Two", "Three"), 0, 31, rule=True, disabled={1})
    clipped = tab_strip_layout(("Long", "Next"), 0, 8)

    assert ruled.rules == ((0, 5), (26, 5))
    assert [hit.index for hit in ruled.hits] == [0, 2]
    assert next(span.style for span in ruled.spans if "Two" in span.text) == "disabled"
    assert [hit.index for hit in clipped.hits] == [0]
    assert max(span.x + len(span.text) for span in clipped.spans) <= 8


def test_navigation_spans_measure_terminal_cells_for_wide_labels():
    tabs = tab_strip_layout(("界", "X"), 0, 11, center=True)
    keys = keybar_layout([[("界", "active"), (" Go", "label")]], 20)

    assert [(hit.x0, hit.x1) for hit in tabs.hits] == [(1, 4), (7, 9)]
    assert [(span.x, span.text) for span in keys.spans] == [(1, "界"), (3, " Go")]


def test_keybar_uses_passed_labels_styles_and_drops_whole_hints_at_limit():
    parts = [
        [("z", "active"), (" Move", "label")],
        [("q", "key"), (" Quit", "label")],
    ]
    full = keybar_layout(parts, 30)
    limited = keybar_layout(parts, 16, trailing=[("?", "key"), (" Help", "label")])

    assert [(span.text, span.style) for span in full.spans] == [
        ("z", "active"),
        (" Move", "label"),
        (" │ ", "separator"),
        ("q", "key"),
        (" Quit", "label"),
    ]
    assert full.width == 15 and full.spans[0].x == 1
    assert [span.text for span in limited.spans] == ["z", " Move", "?", " Help"]
    assert limited.spans[-1].x + len(limited.spans[-1].text) == 15

    # No dangling divider, clipped action, or off-screen span at tiny widths.
    from opentab.presentation.formatting import display_width

    for width in range(32):
        layout = keybar_layout(parts, width, trailing=[("界 Help", "label")])
        assert all(1 <= span.x < span.x + display_width(span.text) < width for span in layout.spans)
        assert not layout.spans or layout.spans[-1].text != " │ "
        if width >= 9:
            assert layout.spans[-1].text == "界 Help"


def test_scrollbar_layout_clamps_viewport_and_uses_at_most_three_runs():
    viewport = viewport_geometry(20, 6, 99)
    layout = scrollbar_layout(20, 6, 7)

    assert (viewport.offset, viewport.max_offset) == (14, 14)
    assert layout is not None
    assert (layout.thumb_y, layout.thumb_height) == (2, 2)
    assert [(run.y, run.length, run.style) for run in layout.runs] == [
        (0, 2, "track"),
        (2, 2, "thumb"),
        (4, 2, "track"),
    ]
    assert len(layout.runs) <= 3


def test_pager_layout_preserves_content_full_height_and_minimum_height_profiles():
    help_layout = pager_layout(
        top=2,
        bottom=23,
        width=80,
        inner_width=52,
        horizontal_chrome=4,
        total_rows=10,
        scroll=99,
        content_sized=True,
    )
    whats_new = pager_layout(
        top=2,
        bottom=23,
        width=80,
        inner_width=72,
        horizontal_chrome=6,
        total_rows=40,
        scroll=99,
        content_sized=False,
        center_vertical=False,
    )
    history = pager_layout(
        top=2,
        bottom=12,
        width=20,
        inner_width=12,
        horizontal_chrome=4,
        total_rows=1,
        scroll=99,
        content_sized=True,
        min_height=6,
    )

    assert (help_layout.y, help_layout.x, help_layout.height, help_layout.width) == (6, 12, 13, 56)
    assert (help_layout.viewport.visible, help_layout.viewport.offset) == (10, 0)
    assert (whats_new.y, whats_new.x, whats_new.height, whats_new.width) == (2, 1, 21, 78)
    assert (whats_new.viewport.visible, whats_new.viewport.offset) == (18, 22)
    assert (history.y, history.x, history.height, history.width) == (4, 2, 6, 16)
    assert (history.viewport.visible, history.viewport.offset) == (3, 0)


def test_scrollbar_thumb_preserves_proportions_and_overflow_boundaries():
    assert scrollbar_thumb(10, 10, 0) is None
    assert scrollbar_thumb(11, 10, 0) == (0, 9)
    assert scrollbar_thumb(11, 10, 1) == (1, 9)
    assert scrollbar_thumb(20, 10, 5) == (3, 5)
    assert scrollbar_thumb(1_000, 10, 990) == (8, 2)
