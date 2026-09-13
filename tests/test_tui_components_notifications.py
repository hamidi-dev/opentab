"""Pure notification layout tests: no App, renderer, or curses."""

from opentab.formatting import display_width
from opentab.tui.components.notifications import (
    Notification,
    history_rows,
    toast_cards,
    toast_history_viewport,
    wrap_notice,
)


def test_live_cards_stack_newest_first_and_truncate_by_cells():
    cards = toast_cards(
        [Notification("copied", "success"), Notification("界" * 81, "warn")],
        height=24,
        width=80,
        release_version="1.2.3",
        release_key="W",
    )
    assert [card.kind for card in cards] == ["warn", "success"]
    assert cards[0].y == 3 and cards[1].y == cards[0].y + cards[0].height + 1
    assert len(cards[0].lines) == 4 and cards[0].lines[-1].endswith("…")
    assert all(display_width(line) <= cards[0].width - 6 for line in cards[0].lines)


def test_live_cards_preserve_whitespace_and_release_metadata():
    message = "Failed:\n  /tmp/a  b.csv\n\nTry again"
    release = toast_cards(
        [Notification(message, "release")],
        height=24,
        width=80,
        release_version="9.8.7",
        release_key="W",
        max_lines=10,
    )[0]
    assert release.title == " ✦ NEW IN v9.8.7 "
    assert release.padding == 2
    assert release.lines == ("Failed:", "  /tmp/a  b.csv", "", "Try again")
    assert "".join(wrap_notice("exported /tmp/a  b.csv", 10)) == "exported /tmp/a  b.csv"


def test_release_shortcut_and_ascii_fallback_are_explicit():
    card = toast_cards(
        [Notification("Press W to see what's new", "release")],
        height=20,
        width=50,
        release_version="1.0.0",
        release_key="W",
        fallback_sigil="*",
    )[0]
    assert card.title == " * NEW IN v1.0.0 "
    assert card.shortcut == (0, 6, "W")


def test_history_formats_ages_and_hanging_indents_without_losing_text():
    message = "Could not export " + "/long path/界" * 8
    rows = history_rows(
        [Notification("older", "info", 0), Notification(message, "error", 65)],
        current_time=65,
        width=30,
    )
    error_rows = [row for row in rows if row.kind == "error"]
    assert "now" in error_rows[0].gutter
    assert all(row.text.startswith(" " * 8) for row in error_rows[1:])
    assert "".join(row.text[8:] for row in error_rows) == message
    assert "1m" in next(row.text for row in rows if row.kind == "info")


def test_history_viewport_clamps_scroll_and_stays_inside_narrow_dimensions():
    viewport = toast_history_viewport(
        [Notification(f"message {index}", born=float(index)) for index in range(20)],
        current_time=20,
        y=2,
        bottom=12,
        width=20,
        scroll=10_000,
        close_key="N",
        scroll_keys="j/k",
        fallback_sigil="*",
    )
    assert viewport is not None
    assert viewport.x >= 0 and viewport.x + viewport.width <= 20
    assert viewport.y >= 2 and viewport.y + viewport.height <= 12
    assert viewport.scroll == viewport.total_rows - viewport.visible_rows
    assert viewport.rows[-1].text.endswith("0")
    assert viewport.scroll_hint == " j/k scroll "


def test_history_viewport_declines_unpaintable_space():
    assert (
        toast_history_viewport(
            [],
            current_time=0,
            y=3,
            bottom=6,
            width=17,
            scroll=0,
            close_key="q",
            scroll_keys="j/k",
        )
        is None
    )
