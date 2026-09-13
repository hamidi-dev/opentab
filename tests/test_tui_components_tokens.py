from opentab.tui.components.bars import legend_lines, stack_line, stack_widths
from opentab.tui.components.token_cards import (
    EconomicsCategory,
    token_breakdown_card,
    token_economics_card,
)


def test_stack_geometry_is_exact_and_keeps_positive_segments_visible():
    rows = [("Input", 999.0, 0), ("Output", 1.0, 1), ("Empty", 0.0, 2)]
    widths = stack_widths(rows, 1000.0, 20)
    line = stack_line(rows, 1000.0, 20, colored=True)
    assert sum(widths) == len(line.text) == 20
    assert widths[1] >= 1 and widths[2] == 0
    assert [(span.column, span.length, span.slot) for span in line.spans] == [
        (0, widths[0], 0),
        (widths[0], widths[1], 1),
    ]


def test_pair_starved_bars_use_glyph_identity_and_legend_wraps_with_spans():
    rows = [("Uncached input", 5.0, 0), ("Model output", 5.0, 1)]
    line = stack_line(rows, 10.0, 12, colored=False)
    legend = legend_lines([(label, slot) for label, _value, slot in rows], 16, colored=False)
    assert len({line.text[span.column] for span in line.spans}) == 2
    assert len(legend) == 2
    assert all(len(item.spans) == 1 and item.spans[0].column == 0 for item in legend)


def test_token_breakdown_formats_exact_totals_mismatch_and_style_metadata():
    card = token_breakdown_card(
        title="# Tokens",
        inner_width=60,
        note_width=64,
        input_tokens=100,
        output_tokens=20,
        reasoning_tokens=5,
        cache_read_tokens=50,
        cache_write_tokens=25,
        cache_write_1h=10,
        recorded_total=230,
        notes=("normalized categories",),
        colored=True,
    )
    lines = [line for group in card.groups for line in group]
    text = "\n".join(line.text for line in lines)
    assert card.title == "# Tokens" and card.notes == ("normalized categories",)
    assert "of cache writes, 1h: 10 (subset)" in text
    assert "Category sum: 200" in text and "Recorded total: 230" in text
    assert "Mismatch: recorded total is 30 higher" in text
    assert len(lines[0].text) == 60 and [span.slot for span in lines[0].spans] == [0, 1, 2, 3, 4]


def test_token_economics_sorts_by_spend_and_returns_unboxed_sections():
    card = token_economics_card(
        categories=(
            EconomicsCategory("Uncached input", 1_000_000, 1.0, 0),
            EconomicsCategory("Model output", 10_000, 2.0, 1),
        ),
        total_tokens=1_010_000,
        total_cost=3.0,
        inner_width=56,
        estimated=True,
        missing_cache_rate=False,
        local_tokens=500,
        colored=True,
    )
    chart, table, total = card.groups
    assert not chart[0].text.startswith(("│", "|"))
    assert [span.slot for span in chart[1].spans] == [1, 0]
    assert table[1].text.strip().startswith("Model output")
    assert "~$3.00" in total[0].text
    assert len(card.notes) == 2
