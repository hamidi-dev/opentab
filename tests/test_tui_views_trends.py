from typing import Union, get_args, get_origin

from opentab.tui.components.boxes import TABLE_GLYPHS
from opentab.tui.components.token_cards import EconomicsCategory, token_economics_card
from opentab.tui.views.trends import (
    DrillSession,
    RankItem,
    calendar_layout,
    daily_layout,
    drill_layout,
    group_ranking_layout,
    model_economics_layout,
    model_ranking_layout,
)


def test_daily_layout_returns_chart_text_and_local_bar_geometry():
    data = (("2026-06-01", 2.0), ("2026-06-02", 5.0))
    layout = daily_layout(
        "2026-06",
        data,
        ("2026-06", "2026-05"),
        80,
        14,
        navigation_keys="j/k",
        selected="2026-06-02",
    )
    assert layout.lines[0] == "# Daily spend · 2026-06   (1/2 — j/k older/newer month)"
    assert layout.bar_slots and [slot[2] for slot in layout.bar_slots] == [
        "2026-06-01",
        "2026-06-02",
    ]
    assert any("▲" in line for line in layout.lines)


def test_runtime_rank_alias_uses_typing_union_for_python_39_imports():
    assert get_origin(get_args(RankItem)[1]) is Union


def test_ranked_layout_windows_rows_but_totals_the_full_ordered_scope():
    rows = [(f"model-{index}", float(10 - index)) for index in range(8)]
    layout = model_ranking_layout(
        rows,
        80,
        10,
        5,
        {"name": "Model", "cost": "Cost v"},
        TABLE_GLYPHS,
    )
    assert layout.rows and layout.rows.start > 0
    assert layout.rows.count < len(rows)
    assert any("$52.00" in line and "TOTAL" in line for line in layout.lines)
    assert layout.headers[0].columns == (("name", "Model"), ("cost", "Cost"))


def test_group_layout_preserves_input_order_and_reports_row_map():
    rows = (
        ("second", {"cost": 2.0, "tokens": 200, "sessions": 2}),
        ("first", {"cost": 5.0, "tokens": 500, "sessions": 1}),
    )
    layout = group_ranking_layout(
        rows,
        90,
        "harness",
        "Harness",
        TABLE_GLYPHS,
        cursor=0,
        selectable=True,
        height=12,
        headings={"name": "Harness", "cost": "Cost v", "tokens": "Tokens", "count": "Sess"},
        show_api_prices=True,
        price_key="$",
    )
    row_map = layout.rows
    assert row_map is not None
    body = layout.lines[row_map.line : row_map.line + row_map.count]
    assert "second" in body[0] and "first" in body[1]
    assert any("$7.00" in line and "TOTAL" in line for line in layout.lines)


def test_drill_layout_formats_only_visible_rows_and_keeps_full_total():
    rows = tuple(
        DrillSession(
            f"2026-06-{index + 1:02d}", float(index + 1), 100 * (index + 1), "", f"s{index}"
        )
        for index in range(9)
    )
    layout = drill_layout(
        "project",
        "/tmp/project",
        rows[4:],
        76,
        len(rows),
        sum(row.cost for row in rows),
        sum(row.tokens for row in rows),
        4,
        "",
        TABLE_GLYPHS,
    )
    assert layout.rows and layout.rows.start > 0
    assert any("$45.00" in line and "TOTAL" in line for line in layout.lines)
    assert "most spend first" in layout.lines[0]


def test_calendar_layout_returns_semantic_heat_spans_and_local_hit_geometry():
    layout = calendar_layout(
        "2026",
        0,
        2,
        {"2026-06-15": 50.0, "2026-02-03": 1.0},
        {"2026-06-15": 2, "2026-02-03": 1},
        5,
        False,
        "2026-06-15",
        24,
        130,
        navigation_keys="j/k",
        select_key="Enter",
        arrows="h k j l",
        price_key="$",
        show_api_prices=False,
        heat_glyphs=("·", "░", "▒", "▓", "▆", "█"),
    )
    assert layout.calendar and layout.calendar.year == "2026"
    assert "Spend calendar · 2026" in layout.lines[0]
    assert any(span.role == "heat" and span.value == 5 for span in layout.spans)
    assert sum(span.role == "cursor" for span in layout.spans) == 2
    assert any("Press Enter" in line for line in layout.lines)


def test_narrow_calendar_preserves_unclipped_legend_placements_for_the_painter():
    layout = calendar_layout(
        "2026",
        0,
        1,
        {"2026-06-15": 50.0},
        {"2026-06-15": 1},
        5,
        False,
        "2026-06-15",
        13,
        40,
        navigation_keys="j/k",
        select_key="Enter",
        arrows="h k j l",
        price_key="$",
        show_api_prices=False,
        heat_glyphs=(".", "1", "2", "3", "4", "5"),
    )

    legend = layout.lines[11]
    assert len(legend) > 40
    assert legend.endswith("4 ≤$22  5 ≤$50")
    assert any(span.line == 11 and span.column >= 40 for span in layout.spans)


def test_economics_layout_reuses_token_card_spans_without_pricing_inputs():
    card = token_economics_card(
        categories=(EconomicsCategory("Output", 100, 2.0, 1),),
        total_tokens=100,
        total_cost=2.0,
        inner_width=56,
        estimated=False,
        missing_cache_rate=False,
        local_tokens=0,
        colored=True,
    )
    layout = model_economics_layout("anthropic/example", 2, 3, 100, "$2.00", card, 60, TABLE_GLYPHS)
    assert any("Model scope" in line for line in layout.lines)
    assert any("Sessions:   2" in line for line in layout.lines)
    assert any("Token economics" in line for line in layout.lines)
    assert any(span.role == "token" and span.value == 1 for span in layout.spans)
