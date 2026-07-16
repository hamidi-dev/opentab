"""The P prices overlay and the startup price-fetch prompt."""

import os
import tempfile

import opentab as ot

from tests._support import FakeStore, _model_row, _price_sort_app, app_with, workflow


def test_capital_p_opens_model_prices_overlay():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            {
                "model_name": "claude-opus-4-8",
                "runs": 1,
                "cost": 5.0,
                "tokens_total": 10,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
            }
        ]
    }
    assert not app.show_prices
    app.handle_key(None, ord("P"))
    assert app.show_prices  # opens the reference overlay
    lines = app.renderer.price_table_lines(80)
    # opus-4-8 lists at $5 in / $25 out per 1M, from the embedded models.dev table
    assert any("claude-opus-4-8" in ln and "5.00" in ln and "25.00" in ln for ln in lines)
    assert any("models.dev" in ln for ln in lines)
    app.handle_key(None, 27)  # esc (a non-nav key) closes it
    assert not app.show_prices


def test_jk_navigates_the_prices_overlay():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("claude-opus-4-8", 5.0, 10),
            _model_row("gpt-5-codex", 2.0, 10),
            _model_row("claude-haiku-4-5", 1.0, 10),
        ]
    }
    app.handle_key(None, ord("P"))
    assert app.show_prices and app.prices_index == 0
    app.handle_key(None, ord("j"))
    assert app.prices_index == 1 and app.show_prices  # moves the cursor, stays open
    app.handle_key(None, ord("k"))
    assert app.prices_index == 0
    app.handle_key(None, ord("k"))  # floored at the top
    assert app.prices_index == 0
    app.handle_key(None, ord("G"))
    assert app.prices_index == 2  # last model (3 rows)
    app.handle_key(None, ord("j"))  # clamped at the last row
    assert app.prices_index == 2
    app.handle_key(None, ord("g"))
    assert app.prices_index == 0
    app.handle_key(None, ord("x"))  # unbound keys are swallowed, the table stays
    assert app.show_prices
    app.handle_key(None, ord("q"))  # closing is explicit (Esc/q/P)
    assert not app.show_prices


def test_prices_default_is_flat_cheapest_mix_first():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    assert app.prices_sort == "eff" and app.prices_view == "flat"
    # One ungrouped list, cheapest-for-your-mix first (the mix here is all input,
    # so eff equals the input rate): mini 0.25 < haiku 1.00 < opus 5.00.
    assert app.priced_model_names() == [
        "gpt-5-mini",
        "claude-haiku-4-5",
        "claude-opus-4-8",
    ]
    effs = [e.eff for e in app.priced_model_entries()]
    assert effs == sorted(effs)


def test_prices_family_view_groups_most_spend_first_eff_within():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    app.prices_view = "family"
    # Grouped by vendor family, families most-spend-first (openai's $9 beats
    # anthropic's $6), the default eff sort applying *within* each group.
    assert app.priced_model_names() == [
        "gpt-5-mini",
        "claude-haiku-4-5",
        "claude-opus-4-8",
    ]
    assert [e.group for e in app.priced_model_entries()] == ["openai", "anthropic", "anthropic"]


def test_prices_sort_picker_reorders_by_a_column():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    app.prices_view = "flat"  # so the column sort orders globally
    app.handle_key(None, ord("s"))  # opens the sort picker over the overlay
    assert app.sort_menu and app.sort_menu_options() == app.prices_sort_options
    app.sort_menu_index = app.prices_sort_options.index("input")
    app.handle_key(None, 10)  # Enter applies
    assert not app.sort_menu and app.prices_sort == "input"
    names = app.priced_model_names()
    # Priciest input first (the default numeric direction), spend no longer decides.
    assert names[0] == "claude-opus-4-8"
    assert [ot.model_price(n)[0] for n in names] == sorted(
        (ot.model_price(n)[0] for n in names), reverse=True
    )


def test_prices_header_click_sorts_then_flips_direction():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    app.prices_view = "flat"  # so the column sort orders globally
    app.apply_header_sort("output", "prices")  # first click sorts by output, high->low
    assert app.prices_sort == "output" and not app.prices_sort_reverse
    desc = [ot.model_price(n)[1] for n in app.priced_model_names()]
    assert desc == sorted(desc, reverse=True)
    app.apply_header_sort("output", "prices")  # re-click flips to low->high
    assert app.prices_sort == "output" and app.prices_sort_reverse
    asc = [ot.model_price(n)[1] for n in app.priced_model_names()]
    assert asc == sorted(asc)


def test_prices_header_marks_the_active_sort_column():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    # A price column sorts high->low by default, so its arrow points down.
    app.prices_sort, app.prices_sort_reverse = "output", False
    assert "output v" in app.renderer._price_header(28)
    app.prices_sort_reverse = True
    assert "output ^" in app.renderer._price_header(28)
    # model sorts a->z by default, so its arrow points up.
    app.prices_sort, app.prices_sort_reverse = "model", False
    assert "model ^" in app.renderer._price_header(28)


def test_prices_pinning_floats_a_shortlist_and_persists():
    # Space pins the selected ROW ("route/canon" keys): it floats to the top of the
    # view under a "★ pinned" header (the shortlist stays in sight above the ~5k-row
    # catalog), the cursor follows it, and the set persists like the other prefs.
    # Row-scoped on purpose: pinning must never light up the 20 other resellers of
    # the same model name.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("anthropic/claude-opus-4-8", 5.0, 100),
            _model_row("openai/gpt-5-mini", 1.0, 100),
        ]
    }
    app.handle_key(None, ord("P"))
    app.prices_index = app.priced_model_names().index("gpt-5-mini")
    app.handle_key(None, ord(" "))
    assert app.pinned_models == {"openai/gpt-5-mini"}  # the row's route, not the bare name
    entries = app.priced_model_entries()
    assert entries[0].bare == "gpt-5-mini" and entries[0].pinned  # floats to the top
    assert app.prices_index == 0  # the cursor follows the row into the pinned block
    lines = app.renderer.price_table_lines(120)
    assert any(ln.startswith("▸ ★ pinned") for ln in lines)
    assert any(ln.startswith("★ gpt-5-mini") for ln in lines)
    # Above the group headers in the vendor view too...
    app.prices_view = "family"
    assert app.priced_model_entries()[0].bare == "gpt-5-mini"
    # ...and in the catalog view ONLY the pinned route's row floats -- the other
    # gateways reselling gpt-5-mini stay where the sort puts them.
    app.prices_view = "all"
    entries = app.priced_model_entries()
    top = [e for e in entries if e.pinned]
    assert [(e.canon, e.routes) for e in top] == [("gpt-5-mini", ("openai",))]
    assert entries[0] == top[0]  # first
    assert sum(1 for e in entries if e.canon == "gpt-5-mini") > 1  # resellers exist, unpinned
    # Pinning a catalog row pins exactly that (route, model) too.
    other = next(
        i
        for i, e in enumerate(entries)
        if e.canon == "claude-opus-4-8" and e.routes != ("anthropic",)
    )
    app.prices_index = other
    route = entries[other].routes[0]
    app.handle_key(None, ord(" "))
    assert f"{route}/claude-opus-4-8" in app.pinned_models
    assert sum(1 for e in app.priced_model_entries() if e.pinned) == 2
    app.handle_key(None, ord(" "))  # cursor followed the row: space again unpins it
    assert app.pinned_models == {"openai/gpt-5-mini"}
    # Space on the pinned row unpins; the set round-trips through state.json.
    app.prices_view = "flat"
    app.prices_index = 0
    app.handle_key(None, ord(" "))
    assert app.pinned_models == set()
    app.pinned_models = {"openai/gpt-5-mini", "anthropic/claude-opus-4-8"}
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert restored.pinned_models == set()
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg
    assert restored.pinned_models == {"openai/gpt-5-mini", "anthropic/claude-opus-4-8"}


def test_prices_heat_scales_each_column_cheap_to_expensive():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    r = app.renderer
    entries = app.priced_model_entries()
    ranges = r._price_column_ranges(entries)
    # ranges[0] is the eff column; the raw columns follow. Each normalizes over its
    # own positive rates: input (ranges[1]) spans mini..opus. The all-input mix here
    # makes eff equal the input rate, so its span matches.
    assert ranges[0] == (0.25, 5.0) and ranges[1] == (0.25, 5.0)
    levels = {e.bare: r._price_heat_level(e.price[0], ranges[1]) for e in entries}
    # Cheapest input -> coolest bucket (0), priciest -> hottest (top level).
    assert levels["gpt-5-mini"] == 0
    assert levels["claude-opus-4-8"] == ot.PRICE_HEAT_LEVELS - 1
    assert levels["gpt-5-mini"] < levels["claude-haiku-4-5"] < levels["claude-opus-4-8"]


def test_prices_heat_is_neutral_when_a_column_has_no_spread():
    # One model (or all-equal rates) -> no range to shade, so cells stay neutral.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {"a": [_model_row("anthropic/claude-opus-4-8", 5.0, 10)]}
    app.handle_key(None, ord("P"))
    r = app.renderer
    ranges = r._price_column_ranges(app.priced_model_entries())
    assert ranges == [None, None, None, None, None]
    assert r._price_heat_level(5.0, ranges[1]) is None


def test_prices_dedupe_folds_alias_spellings_across_routes():
    # The same model reached as a dated id on one route and a dotted alias on
    # another is one row: routes and spend merge, the display name is the
    # most-used spelling with its date pin stripped.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("anthropic/claude-sonnet-4-5-20250929", 2.0, 100),
            _model_row("github-copilot/claude-sonnet-4.5", 1.0, 50),
        ]
    }
    entries = app.priced_model_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e.canon == "claude-sonnet-4-5" and e.bare == "claude-sonnet-4-5"
    assert set(e.routes) == {"anthropic", "github-copilot"}
    assert e.spend == 3.0 and e.share == 1.0
    assert e.price == ot.model_price("claude-sonnet-4-5")


def test_prices_eff_and_missing_cache_cells_render():
    # Pure-text cell rendering over a stub entry: ~ marks the missing-cache-read
    # upper bound, the raw cache-read cell shows "—" (never a free-looking 0.00),
    # and the use column is a share bar + percent.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    entry = type(
        "E",
        (),
        {
            "bare": "x-model",
            "eff": 1.234,
            "approx": True,
            "share": 0.5,
            "price": (2.0, 10.0, 0.0, 0.0),
        },
    )()
    cells = app.renderer._price_raw_cells(entry)
    assert cells == ["2.00", "10.00", "—", "0.00"]
    assert app.renderer._price_eff_cell(entry) == "~1.23"
    core = app.renderer._price_core_text(entry, 12, 0.5)
    assert "~1.23" in core and "—" in core and "50%" in core
    entry.approx, entry.price = False, (2.0, 10.0, 0.2, 0.0)
    assert app.renderer._price_eff_cell(entry) == "1.23"
    assert app.renderer._price_raw_cells(entry)[2] == "0.20"


def test_prices_use_column_sorts_by_usage_share():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    app.apply_header_sort("use", "prices")
    assert app.prices_sort == "use" and not app.prices_sort_reverse
    # Equal token shares here, so the spend-descending tiebreak decides.
    assert app.priced_model_names() == ["gpt-5-mini", "claude-haiku-4-5", "claude-opus-4-8"]
    shares = [e.share for e in app.priced_model_entries()]
    assert shares == sorted(shares, reverse=True)


def test_price_model_sessions_aggregates_alias_spellings():
    # The Enter drill-in matches canonically, so a session that used the dotted
    # copilot spelling lists beside one that used the dashed direct id.
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", title="alpha", directory="/x"),
            workflow("b", "2026-06-02 09:00:00", title="beta", directory="/y"),
        ]
    )
    app._model_by_root = {
        "a": [_model_row("anthropic/claude-sonnet-4-5", 3.0, 50)],
        "b": [_model_row("github-copilot/claude-sonnet-4.5", 1.0, 20)],
    }
    rows = app.price_model_sessions("claude-sonnet-4-5")
    assert [w.id for w, _c, _t in rows] == ["a", "b"]  # both spellings, most spend first


def test_prices_group_by_family_dedupes_routes_and_tags_them():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("anthropic/claude-opus-4-8", 5.0, 100),
            _model_row("github-copilot/claude-haiku-4.5", 0.0, 100),  # copilot-routed Claude
            _model_row("anthropic/claude-haiku-4.5", 2.0, 100),  # the same model, direct
            _model_row("openai/gpt-5-mini", 1.0, 100),
        ]
    }
    app.handle_key(None, ord("P"))
    app.prices_view = "family"  # the grouped-by-vendor layout (default is flat)
    entries = app.priced_model_entries()
    # The two routes to claude-haiku-4.5 collapse to one deduped entry...
    assert [e.bare for e in entries].count("claude-haiku-4.5") == 1
    haiku = next(e for e in entries if e.bare == "claude-haiku-4.5")
    assert haiku.family == "anthropic"  # inferred from the name, not the route
    assert set(haiku.routes) == {"anthropic", "github-copilot"}
    # ...and a copilot-routed Claude groups under Anthropic, not its own route.
    assert {e.family for e in entries} == {"anthropic", "openai"}
    lines = app.renderer.price_table_lines(120)
    assert any(ln.startswith("▸ Anthropic") for ln in lines)
    assert any("copilot" in ln for ln in lines)  # github-copilot abbreviated in the tag


def test_prices_p_cycles_view_modes():
    app = _price_sort_app()
    app.handle_key(None, ord("P"))
    assert app.prices_view == "flat"  # opens flat (no headers)
    assert not any(ln.startswith("▸ ") for ln in app.renderer.price_table_lines(120))
    app.handle_key(None, ord("p"))  # -> by vendor (grouped)
    assert app.prices_view == "family" and app.prices_index == 0
    assert any(ln.startswith("▸ ") for ln in app.renderer.price_table_lines(120))
    app.handle_key(None, ord("p"))  # -> by provider (still grouped, by route)
    assert app.prices_view == "provider"
    assert any(ln.startswith("▸ ") for ln in app.renderer.price_table_lines(120))
    app.handle_key(None, ord("p"))  # -> the whole models.dev catalog (flat, ungrouped)
    assert app.prices_view == "all"
    assert not any(ln.startswith("▸ ") for ln in app.renderer.price_table_lines(160))
    app.handle_key(None, ord("p"))  # wraps back to flat
    assert app.prices_view == "flat"
    app.handle_key(None, ord("l"))  # h/l walk the view tabs too, like Trends
    assert app.prices_view == "family"
    app.handle_key(None, ord("h"))
    assert app.prices_view == "flat"


def test_prices_models_dev_view_lists_the_whole_catalog_at_your_mix():
    # The "all" view swaps the row *set* for the bundled models.dev catalog: every
    # priced model on every route (the same model repeating across gateways is the
    # price-spread information), eff-blended at YOUR mix, joined against your usage
    # by canonical id so a used model keeps its share everywhere it's sold -- every
    # other row shows 0 and draws a blank use cell.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {"a": [_model_row("anthropic/claude-opus-4-8", 5.0, 1000)]}
    app.handle_key(None, ord("P"))
    app.prices_view = "all"
    entries = app.priced_model_entries()
    assert len(entries) > 1000  # the whole catalog, not just what you've used
    assert all(e.price[0] > 0 or e.price[1] > 0 for e in entries)  # $0 rows excluded
    used = [e for e in entries if e.share > 0]
    assert used and all(e.canon == "claude-opus-4-8" for e in used)
    assert len({e.routes[0] for e in used}) > 1  # your model, compared across routes
    unused = next(e for e in entries if e.share == 0)
    assert app.renderer._price_use_cell(unused, 1.0).strip() == ""
    assert entries[0].eff <= entries[-1].eff  # default sort: cheapest-for-your-mix first
    app.query = "claude-opus-4.8"  # the f filter tames the ~5k rows
    hits = app.priced_model_entries()
    assert 0 < len(hits) < 200 and all("opus" in e.canon for e in hits)
    # dots==dashes in the filter too: the dotted query still finds providers that
    # spell the id with dashes (anthropic itself says "claude-opus-4-8")
    exact = [e for e in hits if e.canon == "claude-opus-4-8"]
    assert any(e.routes[0] == "anthropic" for e in exact)
    app.query = "opus8"  # and it's fuzzy: a scattered subsequence narrows too
    assert any(e.canon == "claude-opus-4-8" for e in app.priced_model_entries())
    app.query = ""


def test_prices_provider_view_groups_by_route_and_tags_vendor():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("anthropic/claude-opus-4-8", 5.0, 100),
            _model_row("github-copilot/claude-haiku-4.5", 3.0, 100),  # Claude via a gateway
            _model_row("github-copilot/gpt-5-mini", 1.0, 100),  # GPT via the same gateway
        ]
    }
    app.handle_key(None, ord("P"))
    app.prices_view = "provider"
    lines = app.renderer.price_table_lines(120)
    # One header per access route; github-copilot carries models from >1 vendor.
    assert any(ln.startswith("▸ github-copilot") for ln in lines)
    assert any(ln.startswith("▸ anthropic") for ln in lines)
    # A gateway-routed GPT stays under github-copilot here (route, not vendor),
    # tagged with its vendor family instead of the route.
    copilot_rows = [e for e in app.priced_model_entries() if e.group == "github-copilot"]
    assert {e.bare for e in copilot_rows} == {"claude-haiku-4.5", "gpt-5-mini"}
    assert any("OpenAI" in ln or "Anthropic" in ln for ln in lines)  # vendor shown as the tag


def test_prices_filter_is_fuzzy():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("claude-sonnet-4-5", 1.0, 10),
            _model_row("gpt-5-codex", 2.0, 10),
        ]
    }
    # The P filter is the same fzf-style subsequence match as the session filter:
    # a scattered-letter query narrows without needing the exact substring.
    app.query = "gtex"
    assert app.priced_model_names() == ["gpt-5-codex"]
    app.query = "snt45"
    assert app.priced_model_names() == ["claude-sonnet-4-5"]
    # A literal substring still matches; garbage still yields nothing.
    app.query = "codex"
    assert app.priced_model_names() == ["gpt-5-codex"]
    app.query = "zzz"
    assert app.priced_model_names() == []


def test_prices_enter_lists_sessions_that_used_the_model():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", title="alpha", directory="/x"),
            workflow("b", "2026-06-02 09:00:00", title="beta", directory="/y"),
            workflow("c", "2026-06-03 09:00:00", title="gamma", directory="/z"),
        ]
    )
    app._model_by_root = {
        "a": [_model_row("claude-opus-4-8", 5.0, 100)],
        "b": [_model_row("claude-opus-4-8", 3.0, 60), _model_row("gpt-5-codex", 1.0, 20)],
        "c": [_model_row("gpt-5-codex", 2.0, 40)],
    }
    app.handle_key(None, ord("P"))
    # Cheapest mix first: codex (fallback gpt-5 rate, 1.25 in) ahead of opus (5.00).
    assert app.priced_model_names() == ["gpt-5-codex", "claude-opus-4-8"]
    app.handle_key(None, ord("j"))  # select opus
    app.handle_key(None, 10)  # Enter drills into that model's sessions
    assert app.prices_model == "claude-opus-4-8"
    sessions = app.price_model_sessions("claude-opus-4-8")
    assert [w.id for w, _c, _t in sessions] == ["a", "b"]  # both used opus, most spend first
    assert "c" not in [w.id for w, _c, _t in sessions]  # c only used codex
    # The drill-in body lists those session titles (a's $5 ahead of b's $3), not gamma.
    body = app.renderer.price_session_lines("claude-opus-4-8", 80)
    assert "2 session(s)" in body[0] and "$8.00" in body[0]
    assert any("alpha" in ln for ln in body) and any("beta" in ln for ln in body)
    assert not any("gamma" in ln for ln in body)
    # Esc backs out to the model list; unbound keys are swallowed; q closes.
    app.handle_key(None, 27)
    assert app.prices_model is None and app.show_prices
    app.handle_key(None, ord("x"))
    assert app.show_prices
    app.handle_key(None, ord("q"))
    assert not app.show_prices


def test_f_filters_the_prices_overlay_by_model_name():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("claude-sonnet-4-5", 1.0, 10),
            _model_row("gpt-5-codex", 2.0, 10),
        ]
    }
    app.handle_key(None, ord("P"))
    assert app.show_prices
    app.handle_key(None, ord("f"))
    assert app.filter_active and app.show_prices
    for ch in "sonnet":
        app.handle_key(None, ord(ch))
    lines = app.renderer.price_table_lines(80)
    assert any("claude-sonnet-4-5" in ln for ln in lines)
    assert not any("gpt-5-codex" in ln for ln in lines)
    app.handle_key(None, 10)
    assert not app.filter_active and app.show_prices and app.query == "sonnet"


def test_prices_overlay_close_only_on_esc_q_or_P():
    # The P overlay gets the same explicit-close policy as Trends: unbound keys are
    # swallowed, ? floats help above it, $ re-prices in place, Esc/q/P close it,
    # and inside the per-model drill q closes while Esc only backs out.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {"a": [_model_row("claude-opus-4-8", 5.0, 10)]}
    app._models_loaded = True  # keep $'s deferred scan from wiping the fixture rows
    app.handle_key(None, ord("P"))
    for key in (ord("x"), ord("T"), ord("b")):
        app.handle_key(None, key)
        assert app.show_prices, f"key {chr(key)!r} closed the P overlay"
    app.handle_key(None, ord("?"))  # help floats above...
    assert app.help and app.show_prices
    app.handle_key(None, ord("?"))  # ...and closing it lands back on the table
    assert not app.help and app.show_prices
    app.handle_key(None, ord("$"))  # $ re-prices in place, the table stays
    assert app.show_api_prices and app.show_prices
    app.handle_key(None, ord("P"))  # P again closes (a toggle)
    assert not app.show_prices
    app.handle_key(None, ord("P"))
    assert app.handle_key(None, 3) is False  # Ctrl-C still quits from inside
    app.handle_key(None, 10)  # Enter -> the model's sessions (overlay still open)
    assert app.prices_model == "claude-opus-4-8"
    app.handle_key(None, ord("x"))  # swallowed, the list stays
    assert app.show_prices and app.prices_model is not None
    app.handle_key(None, ord("q"))  # q closes the whole overlay from the drill
    assert not app.show_prices and app.prices_model is None


def test_price_table_omits_local_models():
    # The P overlay is a reference for API list prices behind the "$" what-if. Local
    # models (ollama/mlx/…) have no rate, so they're dropped here -- their usage still
    # shows in the Models/Trends views. A mixed set keeps only the priced models.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("ollama/llama3.1", 0.0, 1000),
            _model_row("anthropic/claude-opus-4-8", 5.0, 1000),
        ]
    }
    assert app.priced_model_names() == ["claude-opus-4-8"]  # deduped to the bare id
    lines = app.renderer.price_table_lines(80)
    assert any("claude-opus-4-8" in ln for ln in lines)
    assert not any("ollama" in ln for ln in lines)
    # Local-only usage leaves nothing to price.
    app._model_by_root = {"a": [_model_row("ollama/llama3.1", 0.0, 1000)]}
    assert app.priced_model_names() == []
    assert any("No model usage" in ln for ln in app.renderer.price_table_lines(80))


def test_price_refresh_while_api_view_applied_does_not_compound_estimate():
    # Repro for the P->r doubling: refresh_prices_action recomputes the API costs
    # while _apply_price_mode has already swapped the estimate into the live cost
    # fields ($ starts on for cost-less backends). The recompute must build from
    # the real snapshots, not add a fresh estimate on top of the previous one.
    app = ot.App.__new__(ot.App)

    class _Store:
        demo = False

    app.store = _Store()
    app.show_api_prices = False
    app._models_loaded = True
    app.loaded = [
        ot.Workflow(
            id="r",
            title="t",
            directory="d",
            created_at="2026-01-01",
            root_cost=10.0,
            total_cost=10.0,
            subagents=0,
            model_count=2,
            total_tokens=0,
            unpriced_tokens=0,
        )
    ]
    app._snapshot_real_costs()

    def row(name, cost, inp):
        return {
            "model_name": name,
            "runs": 1,
            "cost": cost,
            "tokens_total": inp,
            "input": inp,
            "output": 0,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
        }

    app._model_by_root = {
        "r": [
            row("anthropic/claude-opus-4-6", 10.0, 0),
            row("github-copilot/claude-haiku-4.5", 0.0, 1_000_000),
        ]
    }
    app._compute_api_costs()
    app.toggle_api_prices()
    assert round(app.loaded[0].total_cost, 2) == 11.0  # $10 real + $1 estimate

    # What refresh_prices_action does after the fetch, $ view still applied.
    for _ in range(3):
        app._compute_api_costs()
        app._apply_price_mode()

    assert round(app.loaded[0].total_cost, 2) == 11.0  # unchanged, not 12/13/14
    assert round(app.loaded[0].root_cost, 2) == 11.0
    costs = {m["model_name"]: m["cost"] for m in app.model_mix("r")}
    assert costs["github-copilot/claude-haiku-4.5"] == 1.0
    assert costs["anthropic/claude-opus-4-6"] == 10.0
    app.toggle_api_prices()  # $ off still restores the true real cost
    assert app.loaded[0].total_cost == 10.0
    assert {m["model_name"]: m["cost"] for m in app.model_mix("r")}[
        "github-copilot/claude-haiku-4.5"
    ] == 0.0


def test_no_recorded_cost_defaults_to_estimate_view():
    # A backend that records no dollars (Claude Code) would paint a wall of
    # $0.00 in normal mode, so the $ estimate view starts on by default...
    class SubscriptionStore(FakeStore):
        records_cost = False

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(SubscriptionStore([workflow("a", "2026-06-01 12:00:00")]), args)
    assert app.show_api_prices
    # ...but an explicit saved preference (user toggled $ off and quit) wins...
    ot.apply_state(app, args, {"show_api_prices": False})
    assert not app.show_api_prices
    # ...and cost-recording stores keep the real-cost default.
    assert not app_with([workflow("a", "2026-06-01 12:00:00")]).show_api_prices


def test_unpriced_hint_matches_price_mode():
    # The hint teaches $ in normal mode and must not say "not billed" next to
    # nonzero estimated dollars in the $ view.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.show_api_prices = False
    assert "press $" in app.renderer.unpriced_hint()
    app.show_api_prices = True
    hint = app.renderer.unpriced_hint()
    assert "estimate" in hint and "press $" not in hint


def _price_prompt_app(model="openrouter/exotic-zzz-9", unpriced=500):
    # An app whose model scan contains one model + an unpriced-token workflow, with an
    # isolated empty config so price_cache_meta() is None. Caller restores via _restore_xdg.
    w = workflow("w1", "2026-06-01 12:00:00")
    w.unpriced_tokens = unpriced
    app = app_with([w])
    app._model_by_root = {"w1": [{"model_name": model}]}
    return app


def test_price_prompt_triggers_on_unknown_models():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.invalidate_price_cache()
            app = _price_prompt_app()
            app.maybe_prompt_prices()
            assert app.price_prompt is True
            assert app.unknown_models == ["openrouter/exotic-zzz-9"]
            # offered at most once per run
            app.price_prompt = False
            app.maybe_prompt_prices()
            assert app.price_prompt is False
        finally:
            ot.invalidate_price_cache()
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old


def test_price_prompt_skipped_when_known_dismissed_or_no_unpriced():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.invalidate_price_cache()
            # a hardcoded model -> no prompt
            known = _price_prompt_app(model="anthropic/claude-opus-4-8")
            known.maybe_prompt_prices()
            assert known.price_prompt is False
            # unknown model but no unpriced tokens -> nothing to estimate -> no prompt
            metered = _price_prompt_app(unpriced=0)
            metered.maybe_prompt_prices()
            assert metered.price_prompt is False
            # unknown + unpriced but "don't ask again" set -> no prompt
            dismissed = _price_prompt_app()
            dismissed.prices_prompt_dismissed = True
            dismissed.maybe_prompt_prices()
            assert dismissed.price_prompt is False
            # --no-state / demo path (allow_price_prompt False) -> no prompt
            blocked = _price_prompt_app()
            blocked.allow_price_prompt = False
            blocked.maybe_prompt_prices()
            assert blocked.price_prompt is False
        finally:
            ot.invalidate_price_cache()
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old


def test_price_prompt_keys_fetch_dismiss_and_skip():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.invalidate_price_cache()
            # y fetches (stubbed) and closes
            app = _price_prompt_app()
            app.price_prompt = True
            fetched = []
            app.refresh_prices_action = lambda: fetched.append(True)
            app.handle_price_prompt_key(ord("y"))
            assert app.price_prompt is False and fetched == [True]
            # n closes without dismissing
            app.price_prompt = True
            app.handle_price_prompt_key(ord("n"))
            assert app.price_prompt is False and app.prices_prompt_dismissed is False
            # d dismisses and persists through save_state -> a fresh app restores it
            app.price_prompt = True
            app.handle_price_prompt_key(ord("d"))
            assert app.price_prompt is False and app.prices_prompt_dismissed is True
            ot.save_state(app)
            restored = app_with([workflow("w1", "2026-06-01 12:00:00")])
            ot.apply_state(restored, restored.args, ot.load_state())
            assert restored.prices_prompt_dismissed is True
        finally:
            ot.invalidate_price_cache()
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old
