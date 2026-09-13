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
