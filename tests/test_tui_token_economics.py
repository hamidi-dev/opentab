import opentab.tui.app as app_mod
from opentab.heatmap import TOKEN_SERIES_BASE_PAIR
from opentab.pricing import TOKEN_TYPES, api_equivalent_cost, model_price

from tests._support import AttrScreen, _model_row, app_with, workflow


def _row(model, **tok):
    # model_row_split derives missing input from this exact total.
    row = _model_row(model, 0.0, 0)
    row.update(tok)
    row["tokens_total"] = sum(
        int(tok.get(k) or 0) for k in ("input", "output", "reasoning", "cache_read", "cache_write")
    )
    return row


def _unbox(lines):
    return [ln[2:].rstrip(" │|") for ln in lines if ln[:1] in ("│", "|")]


def _bars(lines):
    return [ln for ln in _unbox(lines) if ln and set(ln) <= set("█▓▒░▚%0123456789")]


def _app(rows_by_session, workflows=None):
    app = app_with(workflows or [workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = rows_by_session
    return app


def test_the_five_parts_sum_to_the_api_equivalent_total():
    rows = [
        _row(
            "anthropic/claude-opus-4.5",
            input=120_000,
            output=40_000,
            reasoning=5_000,
            cache_read=900_000,
            cache_write=70_000,
        ),
        _row("openai/gpt-5.5", input=8_000, output=3_000, cache_read=250_000),
    ]
    app = _app({"a": rows})
    econ = app.token_economics(app.loaded)
    reference = sum(
        api_equivalent_cost(
            r["model_name"],
            r["input"],
            r["output"],
            r.get("reasoning") or 0,
            r["cache_read"],
            r["cache_write"],
        )
        for r in rows
    )
    # Equal to the last representable bit, not bit-identical: the pieces accumulate per
    # token type across rows while the reference accumulates per row, so the two sum the
    # same products in a different order.
    assert abs(econ.total_cost - reference) < 1e-9
    assert econ.total_cost == sum(econ.cost)


def test_volume_and_spend_shares_disagree_which_is_the_point():
    rows = [_row("anthropic/claude-opus-4.5", output=50_000, cache_read=5_000_000)]
    econ = _app({"a": rows}).token_economics([workflow("a", "2026-06-01 12:00:00")])
    read_i, out_i = TOKEN_TYPES.index("Cache read"), TOKEN_TYPES.index("Output")
    vol = lambda i: econ.tokens[i] / econ.total_tokens  # noqa: E731
    spend = lambda i: econ.cost[i] / econ.total_cost  # noqa: E731
    assert econ.tokens[read_i] > econ.tokens[out_i] * 50  # reads are the volume
    assert vol(read_i) > spend(read_i)
    assert spend(out_i) > vol(out_i) * 20
    _ir, orr, crr, _cw = model_price("anthropic/claude-opus-4.5")
    assert econ.cost[out_i] == 50_000 * orr / 1e6
    assert econ.cost[read_i] == 5_000_000 * crr / 1e6


def test_reasoning_bills_at_the_output_rate_but_keeps_its_own_row():
    rows = [_row("openai/gpt-5.5", output=10_000, reasoning=10_000)]
    econ = _app({"a": rows}).token_economics([workflow("a", "2026-06-01 12:00:00")])
    out_i, rea_i = TOKEN_TYPES.index("Output"), TOKEN_TYPES.index("Reasoning")
    assert econ.cost[rea_i] == econ.cost[out_i] > 0
    assert econ.tokens[rea_i] == 10_000


def test_local_models_leave_both_rows_not_just_the_cost_one():
    rows = [
        _row("anthropic/claude-opus-4.5", input=1_000, output=1_000),
        _row("ollama/llama3.1", input=4_000_000, output=1_000_000),
    ]
    econ = _app({"a": rows}).token_economics([workflow("a", "2026-06-01 12:00:00")])
    assert econ.local_tokens == 5_000_000
    assert econ.total_tokens == 2_000  # only the priced model's tokens
    assert econ.total_cost > 0


def test_an_unpriced_model_marks_the_figure_as_an_estimate():
    rows = [_row("somevendor/brand-new-model-9", input=1_000_000, output=100_000)]
    econ = _app({"a": rows}).token_economics([workflow("a", "2026-06-01 12:00:00")])
    assert econ.estimated
    assert econ.total_cost > 0
    priced = _app({"a": [_row("anthropic/claude-opus-4.5", input=1_000)]}).token_economics(
        [workflow("a", "2026-06-01 12:00:00")]
    )
    assert not priced.estimated


def test_a_missing_cache_read_rate_is_flagged_rather_than_read_as_free():
    # Shipped paid cards have cache-read rates, so stage the missing-rate case.
    app = _app({"a": [_row("weird/no-cache-rate", input=1_000, cache_read=9_000_000)]})
    original = app_mod.model_price
    try:
        app_mod.model_price = lambda name: (3.0, 15.0, 0.0, 0.0)
        econ = app.token_economics([workflow("a", "2026-06-01 12:00:00")])
    finally:
        app_mod.model_price = original
    assert econ.missing_cache_rate
    assert econ.cost[TOKEN_TYPES.index("Cache read")] == 0.0


def test_no_priceable_usage_returns_nothing_rather_than_an_empty_chart():
    assert _app({}).token_economics([workflow("a", "2026-06-01 12:00:00")]) is None
    local_only = _app({"a": [_row("ollama/llama3.1", input=5_000)]})
    assert local_only.token_economics([workflow("a", "2026-06-01 12:00:00")]) is None


def test_the_box_shows_both_shares_and_says_it_is_list_rates():
    rows = [_row("anthropic/claude-opus-4.5", output=50_000, cache_read=5_000_000)]
    app = _app({"a": rows})
    lines = app.renderer._token_economics_box(app.loaded, 96)
    text = "\n".join(lines)
    assert "Token economics" in text and "at list rates" in text
    assert "Volume" in text and "Spend" in text
    assert "Cache read" in text and "Output" in text
    # Sub-percent rows keep two decimals: formatting.pct floors them all to "<1%", and
    # "0.98% of the tokens, 34% of the bill" is the entire reading.
    assert "<1%" not in text
    assert any("%" in ln and "0." in ln for ln in lines)


def test_the_box_is_two_stacked_bars_that_sum_to_the_pane_width():
    rows = [
        _row(
            "anthropic/claude-opus-4.5",
            input=200_000,
            output=50_000,
            reasoning=10_000,
            cache_read=5_000_000,
            cache_write=100_000,
        )
    ]
    app = _app({"a": rows})
    lines = app.renderer._token_economics_box(app.loaded, 100)
    assert "share of tokens sent" in "\n".join(lines)
    assert "share of dollars billed" in "\n".join(lines)
    bars = _bars(lines)
    assert len(bars) == 2
    assert all(len(b) == 100 - 4 for b in bars)  # exactly the inner width, no drift
    # Every type that has a positive value keeps at least one cell, so a type that cost
    # real money is never invisible -- five segments in each bar, none of them empty.
    for bar in bars:
        runs = app.renderer._token_runs[bar]
        assert len(runs) == 5
        assert all(w >= 1 for _c, w, _s in runs)
        assert sum(w for _c, w, _s in runs) == len(bar)


def test_a_type_owns_one_colour_across_both_bars_and_the_legend():
    rows = [_row("anthropic/claude-opus-4.5", output=50_000, cache_read=5_000_000)]
    app = _app({"a": rows})
    lines = app.renderer._token_economics_box(app.loaded, 100)
    bars = _bars(lines)
    slots = [[s for _c, _w, s in app.renderer._token_runs[b]] for b in bars]
    assert slots[0] == slots[1]  # same types, same order, same colours in both bars
    legend = next(ln for ln in _unbox(lines) if ln.startswith("█ "))
    assert [s for _c, _w, s in app.renderer._token_runs[legend]] == slots[0]
    assert TOKEN_TYPES[slots[0][0]] == "Cache read"  # the priciest type leads


def test_a_narrow_pane_drops_the_bars_and_keeps_every_number():
    rows = [_row("anthropic/claude-opus-4.5", output=50_000, cache_read=5_000_000)]
    app = _app({"a": rows})
    wide = "\n".join(app.renderer._token_economics_box(app.loaded, 100))
    narrow_lines = app.renderer._token_economics_box(app.loaded, 34)
    narrow = "\n".join(narrow_lines)
    assert "█" in wide and "█" not in narrow  # five segments need room to mean anything
    assert "Cache read" in narrow and "$" in narrow  # the table still answers in full
    assert all(len(ln) <= 34 for ln in narrow_lines if ln.startswith(("│", "┌", "├", "└")))


def test_every_scope_overview_carries_the_box():
    ws = [workflow("a", "2026-06-01 12:00:00", directory="/x", cost=2.0, tokens=1000)]
    app = app_with(ws)
    app._model_by_root = {
        "a": [_row("anthropic/claude-opus-4.5", input=10_000, output=5_000, cache_read=900_000)]
    }
    app._compute_api_costs()
    r = app.renderer
    for lines in (
        r.year_overview(app.years[0], 100),
        r.month_overview(app.months[0], 100),
        r.day_overview(app.days[0], 100),
        r.project_overview(app.projects[0], 100),
        r.detail_overview(ws[0], 100),
    ):
        assert any("Token economics" in ln for ln in lines)


def test_the_painted_bar_carries_five_distinct_colour_pairs():
    import opentab as ot

    rows = [
        _row(
            "anthropic/claude-opus-4.5",
            input=200_000,
            output=50_000,
            reasoning=10_000,
            cache_read=5_000_000,
            cache_write=100_000,
        )
    ]
    app = _app({"a": rows})
    r = app.renderer
    lines = r._token_economics_box(app.loaded, 80)
    bar = _bars(lines)[0]
    # Keep pair attributes distinguishable without a live screen.
    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8
    try:
        screen = AttrScreen(height=30, width=100)
        r._paint_token_runs(screen, 5, 2, bar, 80)
    finally:
        ot.curses.color_pair = orig
    painted = [screen.attrs[(5, 2 + col)] for col, _w, _s in r._token_runs[bar]]
    assert len(set(painted)) == 5  # five types, five different attributes
    assert painted == [
        ((TOKEN_SERIES_BASE_PAIR + slot) << 8) | ot.curses.A_BOLD
        for _c, _w, slot in r._token_runs[bar]
    ]


def test_a_pair_starved_terminal_keeps_the_types_apart_with_glyphs():
    rows = [
        _row(
            "anthropic/claude-opus-4.5",
            input=200_000,
            output=50_000,
            reasoning=10_000,
            cache_read=5_000_000,
            cache_write=100_000,
        )
    ]
    app = _app({"a": rows})
    r = app.renderer
    try:
        r._token_series_ok = False
        bar = _bars(r._token_economics_box(app.loaded, 80))[0]
    finally:
        r._token_series_ok = True
    # One glyph per type, all five different -- and no percentage punched through the
    # fill, because with glyphs the fill IS the identity.
    seen = [bar[col] for col, _w, _s in r._token_runs[bar]]
    assert len(set(seen)) == 5
    assert not any(ch.isdigit() for ch in bar)


def test_the_frame_wraps_the_chart_and_the_paint_sees_through_its_gutter():
    import opentab as ot

    rows = [
        _row(
            "anthropic/claude-opus-4.5",
            input=200_000,
            output=50_000,
            reasoning=10_000,
            cache_read=5_000_000,
            cache_write=100_000,
        ),
        # Local usage adds a note outside the frame without changing the bars.
        _row("ollama/llama3.1", input=7_000),
    ]
    app = _app({"a": rows})
    r = app.renderer
    lines = r._token_economics_box(app.loaded, 80)
    assert lines[0].startswith("┌")  # framed, with the "!" notes riding outside below
    assert lines[-2].startswith("└") and lines[-1].startswith("! ")
    boxed = next(ln for ln in lines if _bars([ln]) == [] and "█" in ln and ln.startswith("│"))
    bare = boxed[2:].rstrip(" │")
    runs = r._token_runs[bare]
    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8
    try:
        screen = AttrScreen(height=30, width=120)
        r._paint_token_runs(screen, 4, 0, boxed, len(boxed))
    finally:
        ot.curses.color_pair = orig
    assert (4, 0) not in screen.attrs
    for col, _w, slot in runs:
        assert (
            screen.attrs[(4, col + 2)] == ((TOKEN_SERIES_BASE_PAIR + slot) << 8) | ot.curses.A_BOLD
        )


def test_a_legend_too_wide_for_the_pane_wraps_instead_of_being_clipped():
    rows = [
        _row(
            "anthropic/claude-opus-4.5",
            input=200_000,
            output=50_000,
            reasoning=10_000,
            cache_read=5_000_000,
            cache_write=100_000,
        )
    ]
    app = _app({"a": rows})
    legend = _unbox(app.renderer._token_economics_box(app.loaded, 58))
    legend = [ln for ln in legend if ln.startswith("█ ")]
    assert len(legend) > 1  # wrapped, not truncated
    joined = " ".join(legend)
    for label in ("Cache read", "Output", "Uncached input", "Cache write", "Reasoning"):
        assert label in joined
    assert all(len(ln) <= 58 - 4 for ln in legend)
