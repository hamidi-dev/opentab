import opentab as ot

from tests._support import app_with, workflow


def test_relative_age():
    from datetime import datetime, timedelta, timezone

    from opentab.formatting import relative_age

    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert relative_age("", now=now) == ""
    assert relative_age("not-a-date", now=now) == ""
    assert relative_age((now - timedelta(seconds=30)).isoformat(), now=now) == "just now"
    assert relative_age((now - timedelta(minutes=5)).isoformat(), now=now) == "5m ago"
    assert relative_age((now - timedelta(hours=2)).isoformat(), now=now) == "2h ago"
    assert relative_age((now - timedelta(days=3)).isoformat(), now=now) == "3d ago"
    # A naive UTC stamp (Z form) parses; a future stamp reads "just now", never "-2h".
    assert relative_age("2026-07-18T10:00:00Z", now=now) == "2h ago"
    assert relative_age((now + timedelta(hours=1)).isoformat(), now=now) == "just now"


def test_human_tokens():
    assert ot.human_tokens(999) == "999"
    assert ot.human_tokens(1_500) == "1.5k"
    assert ot.human_tokens(2_000_000) == "2.0M"
    assert ot.human_tokens(3_000_000_000) == "3.0B"


def test_human_duration():
    assert ot.human_duration(0) == "0s"
    assert ot.human_duration(-5) == "0s"
    assert ot.human_duration(45) == "45s"
    assert ot.human_duration(60) == "1m"
    assert ot.human_duration(125) == "2m"  # seconds drop once we reach minutes
    assert ot.human_duration(3600) == "1h"  # a whole hour drops its 0m
    assert ot.human_duration(3600 + 3 * 60) == "1h 3m"
    assert ot.human_duration(26 * 3600) == "1d 2h"
    assert ot.human_duration(48 * 3600) == "2d"  # a whole day drops its 0h


def test_money_is_two_decimals():
    assert ot.money(195.6915) == "$195.69"
    assert ot.money(0) == "$0.00"
    assert ot.money(1_234_567.5) == "$1,234,567.50"


def test_money_marks_sub_cent_costs():
    assert ot.money(0.004) == "<$0.01"
    assert ot.money(0.0001) == "<$0.01"
    assert ot.money(0) == "$0.00"
    assert ot.money(0.02) == "$0.02"


def test_money_label_marks_sub_cent_costs_like_money():
    assert ot.money_label(0.004) == "<$0.01"
    assert ot.money_label(0) == ""


def test_money_pattern_covers_the_compact_k_suffix():
    from opentab.formatting import MONEY_PATTERN

    for label in ("$1.2k", "$12k", "$1,234.56", "$0.00", "<$0.01"):
        assert MONEY_PATTERN.search(label).group(0).lstrip("<") == label.lstrip("<")
    # a following letter is not part of the amount (never eat into a word)
    assert MONEY_PATTERN.search("$5kb").group(0) == "$5"


def test_token_pattern_never_clobbers_a_money_k_label():
    from opentab.formatting import TOKEN_PATTERN

    assert TOKEN_PATTERN.search("$1.2k") is None
    assert TOKEN_PATTERN.search("($12.3k)") is None
    # a real, unprefixed token figure still matches (that colouring is intended)
    assert TOKEN_PATTERN.search(" 1.2k ").group(0) == "1.2k"
    assert TOKEN_PATTERN.search("35.0B").group(0) == "35.0B"


def test_display_width_counts_terminal_cells():
    assert ot.display_width("abc") == 3
    assert ot.display_width("") == 0
    assert ot.display_width("日本語") == 6  # CJK glyphs take two cells each
    assert ot.display_width("日本語 ok") == 9
    assert ot.display_width("e\u0301") == 1  # combining accent adds no cell


def test_shorten_truncates_by_display_cells():
    title = "日本語のセッションタイトル"
    for width in (6, 7, 10, 13):
        cut = ot.shorten(title, width)
        assert ot.display_width(cut) <= width
        assert cut.endswith("...")
    assert ot.shorten(title, 100) == title
    assert ot.shorten("hello world", 8) == "hello..."
    # A wide char straddling the boundary is dropped, not half-drawn.
    assert ot.shorten("日日日", 5) == "日..."


def test_pad_fills_to_exact_display_width():
    assert ot.pad("abc", 6) == "abc   "
    padded = ot.pad("日本", 8)
    assert padded == "日本    "
    assert ot.display_width(padded) == 8
    assert ot.pad("toolong", 3) == "toolong"  # never truncates, only pads


def test_clip_never_exceeds_the_cell_budget():
    assert ot.clip("hello", 3) == "hel"
    assert ot.clip("日本語", 4) == "日本"
    assert ot.clip("日本語", 5) == "日本"  # the straddling wide char is dropped
    assert ot.clip("日本語", 0) == ""


def test_short_path_keeps_wide_tails_within_budget():
    path = "/home/user/プロジェクト/深いディレクトリ"
    cut = ot.short_path(path, 12)
    assert ot.display_width(cut) <= 12
    assert cut.startswith("...")


def test_pct():
    assert ot.pct(50, 200) == "25%"
    assert ot.pct(1, 3) == "33%"
    assert ot.pct(1, 1000) == "<1%"  # 0.1% rounds visibly, not to "0%"
    assert ot.pct(0, 0) == "-"
    assert ot.pct(0, 10) == "0%"


def test_cost_bar():
    assert ot.cost_bar(0, 10) == " " * 8
    assert ot.cost_bar(10, 0) == " " * 8  # no peak -> blank, never divides by zero
    assert ot.cost_bar(10, 10) == "█" * 8
    assert all(len(ot.cost_bar(v, 10)) == 8 for v in (0, 1, 3, 5, 7, 10))
    assert ot.cost_bar(5, 10).startswith("████") and not ot.cost_bar(5, 10).startswith("█████")
    assert ot.cost_bar(1, 1000).startswith("▏")  # tiny-but-nonzero shows a sliver


def test_wrap_cells_and_clip_tail():
    assert ot.wrap_cells("one two three", 9) == ["one two", "three"]
    assert ot.wrap_cells("世界世界世界", 4) == ["世界", "世界", "世界"]  # 2 cells each
    assert ot.wrap_cells("supercalifragilistic", 6) == ["superc", "alifra", "gilist", "ic"]
    assert ot.wrap_cells("", 10) == []
    assert ot.clip_tail("hello world", 5) == "world"
    assert ot.clip_tail("世界世界", 3) == "界"  # a straddling wide char is dropped, not halved
    assert ot.clip_tail("hi", 10) == "hi"


def test_notice_is_info_and_colours_are_explicit():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    clock = [100.0]
    app._toast_clock = lambda: clock[0]

    # `self.notice = "..."` stays the one-liner and is neutral info BY DEFINITION:
    # the kind is never inferred from the text, so error-sounding words (a session
    # title saying "failed", a reworded message) can never change the colour.
    app.notice = "price refresh failed: boom"
    assert app.notice == "price refresh failed: boom"  # readable back (tests/callers)
    assert app.toasts[-1].kind == "info"
    app._mark_toasts_shown()

    # A coloured toast names its kind at the call site.
    app.notify("export failed: disk full", "error")
    assert app.toasts[-1].kind == "error"
    app._mark_toasts_shown()

    app.notify("copied: ses_42", "success")
    assert app.toasts[-1].kind == "success"
    assert len(app.toasts) == 3  # three distinct frames -> three stacked toasts

    app._mark_toasts_shown()
    app.notify("heads up", kind="warn")
    assert app.toasts[-1].kind == "warn"


def test_human_tokens_never_exceeds_six_characters():
    assert ot.human_tokens(999_950) == "1.0M"
    assert ot.human_tokens(999_949) == "999.9k"
    assert ot.human_tokens(999_950_000) == "1.0B"
    assert ot.human_tokens(999_949_999) == "999.9M"
    # Nothing anywhere in the range outgrows the cell.
    for v in (0, 1, 999, 1_000, 12_345, 999_949, 999_950, 1_000_000, 123_456_789, 10**12):
        assert len(ot.human_tokens(v)) <= 6, (v, ot.human_tokens(v))


def test_wrap_cells_indent_never_overflows_the_width_it_was_given():
    # A word accepted against the FIRST line's room was then emitted on a continuation,
    # whose width the indent has shrunk -- so an indented wrap came back wider than the
    # caller asked for, and the painter clipped what it could not fit.
    from opentab.formatting import display_width, wrap_cells

    assert wrap_cells("12345 1234567", 10, "    ") == ["12345", "    123456", "    7"]
    # A continuation narrower than one glyph spends the indent rather than the width.
    assert wrap_cells("aaaa 界", 4, "   ") == ["aaaa", "界"]
    for text in ("12345 1234567", "界" * 20, "a" * 45, "aaaa 界", "the quick brown fox"):
        for width in (1, 2, 4, 7, 10, 20):
            for indent in ("", "  ", "   ", "        "):
                out = wrap_cells(text, width, indent)
                # The one documented exception is a single glyph wider than the WHOLE
                # pane, which the original preserves rather than stalling or dropping.
                over = [ln for ln in out if display_width(ln) > width]
                assert all(len(ln.strip()) == 1 for ln in over), (text, width, indent, over)
                # ...and nothing is dropped on the way (a hard-broken word rejoins).
                assert "".join(out).replace(indent, "").replace(" ", "") == text.replace(" ", "")
    # Existing callers, which pass no indent, are untouched.
    assert wrap_cells("the quick brown fox", 10) == ["the quick", "brown fox"]
    assert wrap_cells("", 10) == [] and wrap_cells("x", 0) == []
