from collections import namedtuple

from opentab.presentation.formatting import pad, shorten
from opentab.presentation.heatmap import PRICE_HEAT_LEVELS
from opentab.tui.views import prices

Entry = namedtuple(
    "Entry",
    "bare canon family routes spend group share price eff approx status pinned",
    defaults=("", False),
)


def entry(name, rate, *, group="", share=0.0, pinned=False, routes=("anthropic",)):
    return Entry(
        name,
        name,
        "anthropic",
        routes,
        share,
        group,
        share,
        (rate, rate * 5, rate / 10, 0.0),
        rate,
        False,
        "",
        pinned,
    )


def layout(entries, **overrides):
    options = {
        "source": "models.dev 2026-09-13 (bundled)",
        "token_mix": ((0.5, 0.25, 0.2, 0.05), 100),
        "view": "flat",
        "sort": "eff",
        "descending": False,
        "query": "",
        "width": 100,
        "selection": 0,
        "scroll": 0,
        "visible": 10,
    }
    options.update(overrides)
    return prices.table_layout(entries, **options)


def test_price_view_is_pure_and_returns_semantic_local_geometry():
    entries = [entry("cheap", 1.0), entry("pricey", 10.0)]
    result = layout(entries)

    assert "curses" not in prices.__dict__
    assert result.header is not None
    assert "eff $/M ^" in result.header
    assert {span.key for span in result.sort_spans} == {
        "model",
        "eff",
        "use",
        "input",
        "output",
        "cache_read",
        "cache_write",
    }
    assert result.rows[0].entry_index == 0
    assert result.rows[0].selected
    heat = [span for span in result.rows[1].spans if span.role == "heat"]
    assert heat and all(span.level == PRICE_HEAT_LEVELS - 1 for span in heat)
    assert all(0 <= span.start < 100 and span.start + len(span.text) <= 100 for span in heat)


def test_grouped_viewport_keeps_selected_group_header_and_row_visible():
    entries = [
        entry("pinned", 0.5, pinned=True),
        entry("alpha-one", 1.0, group="anthropic"),
        entry("alpha-two", 2.0, group="anthropic"),
        entry("openai-one", 3.0, group="openai"),
    ]
    result = layout(entries, view="family", selection=3, visible=2, scroll=0)

    assert result.total_rows == 7
    assert result.scroll == 5
    assert [row.kind for row in result.rows] == ["header", "model"]
    assert result.rows[0].text == "▸ OpenAI"
    assert result.rows[1].entry_index == 3


def test_interactive_layout_formats_only_visible_catalog_rows():
    class HiddenEntry:
        bare = "hidden"
        canon = "hidden"
        family = "anthropic"
        routes = ("anthropic",)
        spend = 0.0
        group = ""
        share = 0.0
        price = (2.0, 10.0, 0.2, 0.0)
        eff = 2.0
        status = ""
        pinned = False

        @property
        def approx(self):
            raise AssertionError("hidden row was formatted")

    result = layout([entry("visible", 1.0), HiddenEntry()], visible=1)
    assert len(result.rows) == 1 and "visible" in result.rows[0].text


def test_boundary_widths_keep_core_tag_and_heat_as_separate_paint_operations():
    entries = [
        entry(
            "pinned-model-with-a-long-name",
            1.0,
            pinned=True,
            routes=("über-provider-with-a-very-long-route",),
        ),
        entry(
            "current-model-with-a-long-name",
            10.0,
            routes=("github-copilot", "another-provider-with-a-very-long-route"),
        ),
    ]
    ranges = prices.column_ranges(entries)
    for width in range(58, 111):
        namew = prices.name_width(entries, width)
        peak = prices.use_peak(entries)
        tag_x = namew + 2 + prices.PRICE_BLOCK_W + 2
        for selected in (0, 1):
            result = layout(entries, width=width, selection=selected, visible=3)
            rows = {row.entry_index: row for row in result.rows if row.kind == "model"}
            for index, item in enumerate(entries):
                row = rows[index]
                expected_core = pad(shorten(prices.core_text(item, namew, peak), width), width)
                assert row.text == expected_core
                assert row.selected is (index == selected)

                tag_spans = [span for span in row.spans if span.role in ("muted", "selected")]
                tag = prices.entry_tag(item, "flat")
                if tag_x < width:
                    assert tag_spans == [
                        prices.PriceSpan(
                            tag_x,
                            shorten(tag, width - tag_x),
                            "selected" if index == selected else "muted",
                        )
                    ]
                else:
                    assert tag_spans == []

                heat = [span for span in row.spans if span.role == "heat"]
                if index == selected:
                    assert heat == []
                    continue
                eff_x = namew + 2
                raw_x = eff_x + prices.PRICE_EFF_W + 2 + prices.PRICE_USE_W + 2
                expected_heat = {
                    eff_x: f"{prices.eff_cell(item):>{prices.PRICE_EFF_W}}",
                    **{
                        raw_x + offset * (prices.PRICE_COL_W + 1): f"{cell:>{prices.PRICE_COL_W}}"
                        for offset, cell in enumerate(prices.raw_cells(item))
                    },
                }
                for span in heat:
                    assert span.text == expected_heat[span.start]
                    assert span.level is not None
                if eff_x + prices.PRICE_EFF_W <= width:
                    assert any(
                        span.start == eff_x
                        and span.text == f"{prices.eff_cell(item):>{prices.PRICE_EFF_W}}"
                        and span.level == prices.heat_level(item.eff, ranges[0])
                        for span in heat
                    )


def test_one_long_catalog_name_keeps_the_tag_column_on_screen():
    # A 60-character catalog name used to take the whole name column at 124 wide,
    # pushing every route/tier tag past the edge; narrow screens keep the old names.
    entries = [entry("x" * 60, 1.0), entry("claude-opus-4-8", 5.0)]
    assert prices.name_width(entries, 124) + 2 + prices.PRICE_BLOCK_W + 2 + 16 <= 124
    assert prices.name_width(entries, 80) >= len("claude-opus-4-8")


def test_neutral_price_cells_keep_explicit_paint_positions_when_values_overflow():
    entries = [entry("a", 1_000_000), entry("b", 1_000_000)]
    result = layout(entries, width=100)
    row = result.rows[1]
    fields = [span for span in row.spans if span.role == "normal"]
    assert len(fields) == 5 and all(span.level is None for span in fields)
    assert fields[0].text == f"{prices.eff_cell(entries[1]):>{prices.PRICE_EFF_W}}"
    assert [span.text for span in fields[1:]] == [
        f"{cell:>{prices.PRICE_COL_W}}" for cell in prices.raw_cells(entries[1])
    ]


def test_full_text_builder_explicitly_formats_all_rows_and_tags():
    entries = [
        entry("claude-a", 1.0, group="anthropic", routes=("anthropic", "github-copilot")),
        entry("gpt-b", 2.0, group="openai", routes=("openai",)),
    ]
    lines = prices.table_lines(
        entries,
        source="catalog",
        token_mix=None,
        view="family",
        sort="output",
        descending=True,
        query="",
        width=120,
    )

    assert lines[:2] == ["catalog", ""]
    assert "output v" in lines[2]
    assert "▸ Anthropic" in lines and "▸ OpenAI" in lines
    assert any("claude-a" in line and "anthropic·copilot" in line for line in lines)
    assert any("gpt-b" in line for line in lines)


def test_session_drill_formats_full_text_and_slices_the_viewport():
    rows = [
        prices.PriceSessionEntry("2026-09-01 10:00", 3.0, 1_500, "oc  ★ alpha"),
        prices.PriceSessionEntry("2026-09-02 10:00", 2.0, 500, "cc  beta"),
        prices.PriceSessionEntry("2026-09-03 10:00", 1.0, 250, "cx  gamma"),
    ]
    result = prices.session_layout(
        rows,
        model="claude",
        source_header="Hns ",
        scroll=99,
        visible=2,
    )

    assert result.summary == "3 session(s) · $6.00 on this model · most spend first"
    assert result.header is not None
    assert result.header.endswith("Hns Title")
    assert result.scroll == 1 and result.total_rows == 3
    assert "beta" in result.rows[0] and "gamma" in result.rows[1]
    full = prices.session_lines(rows, "claude", "Hns ")
    assert len(full) == 5 and "alpha" in full[2]
    assert prices.session_lines([], "claude") == ["No sessions used claude."]


def test_session_text_builder_and_layout_leave_clipping_to_the_renderer():
    long_title = "a session title that is deliberately much wider than the viewport"
    rows = [prices.PriceSessionEntry("2026-09-01 10:00", 3.0, 1_500, long_title)]

    lines = prices.session_lines(rows, "claude")
    result = prices.session_layout(
        rows,
        model="claude",
        source_header="",
        scroll=0,
        visible=1,
    )

    assert lines[2].endswith(long_title)
    assert result.rows[0] == lines[2]
    assert "…" not in lines[2]


CardEntry = namedtuple(
    "CardEntry",
    "bare canon family routes spend group share price eff approx status pinned card",
    defaults=("", False, None),
)


def card_entry(card, *, eff=2.0, routes=("alibaba",), status=""):
    return CardEntry(
        "qwen-plus", "qwen-plus", "qwen", routes, 0.0, "", 0.0,
        (0.4, 1.2, 0.08, 0.5), eff, False, status, False, card,
    )  # fmt: skip


TIERED = {"tiers": [(256000, (1.2, 3.6, 0.24, 1.5), 12.0)], "reasoning": 4.0}


def test_tier_tag_rides_in_every_views_tag_and_type_only_in_the_catalog():
    assert prices.context_label(272000) == "272k" and prices.context_label(1_048_576) == "1.05M"
    tiered = card_entry(TIERED)
    assert prices.tier_tag(tiered) == "↑256k" and prices.tier_tag(card_entry(None)) == ""
    assert prices.entry_tag(tiered, "flat") == "alibaba·↑256k"
    assert prices.entry_tag(tiered, "provider") == "Qwen·↑256k"
    embed = card_entry({"type": "embedding"}, status="beta")
    assert prices.entry_tag(embed, "all") == "alibaba·beta·embedding"
    assert prices.entry_tag(embed, "flat") == "alibaba"


def test_card_lines_show_rates_tiers_exposure_facts_and_a_meaningfully_cheaper_route():
    card = {
        **TIERED,
        "released": "2024-01-25",
        "knowledge": "2024-04-30",
        "max_output": 32768,
        "open": True,
    }
    lines = prices.card_lines(
        card_entry(card),
        context_window=1_000_000,
        exposure=(2.0, 8.0, 1.0),
        cheapest=("deepinfra", 1.0, False, 2.0),
    )
    text = "\n".join(lines)
    assert lines[0].startswith("List rates") and "reasoning 4.00" in lines[0]
    assert lines[1].startswith("Above 256k") and "reasoning 12.00" in lines[1]
    assert "bills whole at the higher rates" in lines[2]
    assert "25% of your list cost ran at long-context rates" in text
    assert "12% lacks request size, priced at base (~)" in text
    assert "released 2024-01-25 · knowledge 2024-04 · 1M context · 32.8k max output" in text
    assert "open weights" in text
    assert "deepinfra 1.00 eff $/M, 50% below this row at your mix" in text
    unknown = prices.card_lines(card_entry(card), exposure=(0.0, 4.0, 4.0))
    assert any("request sizes unknown" in ln for ln in unknown)
    assert not any("0% of your list cost" in ln for ln in unknown)
    # No tiers: no tier lines, and exposure is meaningless without them.
    plain = prices.card_lines(card_entry({}), exposure=(0.0, 1.0, 0.0))
    assert plain == [plain[0]] and "reasoning" not in plain[0]
    # A known card without tiers says so, so a short card reads as flat, not missing.
    flat = prices.card_lines(card_entry({"context": 1_000_000}))
    assert flat[1].startswith("Long context") and "same rates at any request size" in flat[1]
    # A route within rounding noise, or the row's own route, is not news.
    for cheapest in (("deepinfra", 1.95, False, 2.0), ("alibaba", 1.0, False, 2.0)):
        assert not any(
            ln.startswith("Cheapest") for ln in prices.card_lines(card_entry({}), cheapest=cheapest)
        )
