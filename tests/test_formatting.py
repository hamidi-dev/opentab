"""money/pct/tokens/cost_bar/short_path and the display-width helpers (formatting.py)."""

import opentab as ot

from tests._support import app_with, workflow


def test_human_tokens():
    assert ot.human_tokens(999) == "999"
    assert ot.human_tokens(1_500) == "1.5k"
    assert ot.human_tokens(2_000_000) == "2.0M"
    assert ot.human_tokens(3_000_000_000) == "3.0B"


def test_money_is_two_decimals():
    assert ot.money(195.6915) == "$195.69"
    assert ot.money(0) == "$0.00"
    assert ot.money(1_234_567.5) == "$1,234,567.50"


def test_money_marks_sub_cent_costs():
    # A nonzero cost under a cent must not look identical to a truly-zero row.
    assert ot.money(0.004) == "<$0.01"
    assert ot.money(0.0001) == "<$0.01"
    assert ot.money(0) == "$0.00"
    assert ot.money(0.02) == "$0.02"


def test_money_label_marks_sub_cent_costs_like_money():
    # The compact bar label spells sub-cent the same way money() does.
    assert ot.money_label(0.004) == "<$0.01"
    assert ot.money_label(0) == ""


def test_display_width_counts_terminal_cells():
    assert ot.display_width("abc") == 3
    assert ot.display_width("") == 0
    assert ot.display_width("日本語") == 6  # CJK glyphs take two cells each
    assert ot.display_width("日本語 ok") == 9
    assert ot.display_width("e\u0301") == 1  # combining accent adds no cell


def test_shorten_truncates_by_display_cells():
    # A CJK title must never render wider than its column budget.
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
