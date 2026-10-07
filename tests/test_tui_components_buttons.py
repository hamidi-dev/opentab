from opentab.tui.components.buttons import Button, button_bar_layout, button_bar_width


def _texts(layout):
    return [(span.x, span.text, span.style) for span in layout.spans if span.style != "gap"]


def test_buttons_then_status_center_as_one_block_and_hits_cover_whole_buttons():
    layout = button_bar_layout(
        40,
        buttons=[Button("h", "Newer", "newer", "ctx"), Button("q", "Close", "close", "ctx")],
        status=["2/9"],
    )

    assert _texts(layout) == [
        (8, " h", "key"),
        (10, " Newer ", "label"),
        (18, " q", "key"),
        (20, " Close ", "label"),
        (28, "2/9", "text"),
    ]
    assert [(hit.x0, hit.x1, hit.context, hit.action) for hit in layout.hits] == [
        (8, 16, "ctx", "newer"),
        (18, 26, "ctx", "close"),
    ]
    assert layout.width == 25


def test_disabled_buttons_keep_their_place_but_are_not_clickable():
    layout = button_bar_layout(
        30, buttons=[Button("h", "Newer", "newer", enabled=False), Button("l", "Older", "older")]
    )

    assert _texts(layout)[:2] == [(5, " h", "off"), (7, " Newer ", "off")]
    assert [hit.action for hit in layout.hits] == ["older"]


def test_overflow_sheds_leading_status_then_leading_buttons_keeping_close_and_position():
    buttons = [
        Button("h", "Newer", "newer"),
        Button("o", "Open", "open"),
        Button("q", "Close", "close"),
    ]
    status = ["j/k scroll", "1/3"]
    full = button_bar_width(buttons=buttons, status=status)

    assert len(button_bar_layout(full, buttons=buttons, status=status).hits) == 3
    narrow = button_bar_layout(full - 1, buttons=buttons, status=status)
    assert [span.text for span in narrow.spans if span.style == "text"] == ["1/3"]
    assert len(narrow.hits) == 3
    tight = button_bar_layout(15, buttons=buttons, status=status)
    assert [hit.action for hit in tight.hits] == ["close"]
    assert [span.text for span in tight.spans if span.style == "text"] == ["1/3"]
    assert button_bar_layout(5, buttons=buttons).spans == ()


def test_unbound_buttons_vanish_and_wide_labels_measure_cells():
    assert button_bar_layout(30, buttons=[Button("", "Close", "close")], status=[""]).spans == ()
    layout = button_bar_layout(13, buttons=[Button("界", "閉じる", "close")])

    assert (layout.hits[0].x0, layout.hits[0].x1) == (1, 11)
