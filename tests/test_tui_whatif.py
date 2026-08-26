import tempfile

import opentab as ot

from tests._support import (
    FakeScreen,
    _model_row,
    _whatif_app,
    _whatif_baseline,
    _whatif_db,
    _whatif_msg,
    app_with,
    box_cells,
    screen_text,
    workflow,
)


def _table(lines):
    start = next(
        i
        for i, ln in enumerate(lines)
        if ln[:1] in ("┌", "+") and ("Session Tree" in ln or "Subagent Ex" in ln)
    )
    return lines[start:]


def _table_cells(lines):
    return box_cells(_table(lines))


def _wi_row(lines, label):
    return next((ln for ln in lines if label in ln), "")


def test_local_usage_stays_zero_in_what_if_view():
    app = app_with([workflow("a", "2026-06-01 12:00:00", cost=0, directory="/x")])
    app._model_by_root = {
        "a": [
            {**_model_row("ollama/llama3.1", 0.0, 1_000_000), "input": 900_000, "output": 100_000}
        ]
    }
    app._compute_api_costs()
    app.show_api_prices = True
    app._apply_price_mode()
    assert app.loaded[0].api_total_cost == 0.0  # no invented spend
    ollama = next(
        c
        for c in box_cells(app.renderer.trend_providers(80, 12), lead=True)
        if c.startswith("ollama")
    )
    assert "$0.00" in ollama


def test_whatif_overview_answers_for_a_solo_session_that_has_no_subagents():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp, solo=True)
        wf = app.loaded[0]
        assert wf.subagents == 0
        opus_in = ot.model_price("anthropic/claude-opus-4.5")[0]
        haiku_in = ot.model_price("anthropic/claude-haiku-4.5")[0]

        assert not any("What-if" in ln for ln in app.renderer.detail_overview(wf, 100))

        app.select_whatif_model("anthropic/claude-haiku-4.5")
        lines = app.renderer.detail_overview(wf, 100)
        assert any("Money card · what-if anthropic/claude-haiku-4.5" in ln for ln in lines)
        # BOTH sides at list rates -- the baseline is its 1M Opus tokens at Opus rates,
        # NOT the $1.50 that happened to be recorded (that would compare a list-price
        # counterfactual against a metered bill and call the difference a saving).
        assert ot.money(opus_in) in _wi_row(lines, "★ Your models (list)")
        assert ot.money(haiku_in) in _wi_row(lines, "★ All at anthropic/claude-haiku-4.5")
        # Cheaper than the models that ran it, so the change is negative -- and never
        # phrased as "routing": nothing was routed.
        assert f"-{ot.money(opus_in - haiku_in)}" in _wi_row(lines, "★ Change")
        assert not any("routing" in ln for ln in lines)
        # The Subagents tab still, correctly, has nothing to show.
        assert "No subagents used" in app.renderer.detail_subagents(wf, 100)[1]


def test_whatif_overview_and_subagents_tab_never_quote_different_totals():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        wf = app.loaded[0]
        app.select_whatif_model("anthropic/claude-opus-4.5")
        actual, whatif = app.whatif_session_totals(wf)
        assert abs(actual - _whatif_baseline(app, "root")) < 1e-9

        overview = app.renderer.detail_overview(wf, 100)
        assert ot.money(actual) in _wi_row(overview, "★ Your models (list)")
        assert ot.money(whatif) in _wi_row(overview, "★ All at anthropic/claude-opus-4.5")
        total = next(ln for ln in app.renderer.detail_subagents(wf, 200) if ln.startswith("TOTAL"))
        assert f"your models {ot.money(actual)} → " in total
        assert f"all at anthropic/claude-opus-4.5 {ot.money(whatif)}" in total


def test_whatif_target_a_single_model_session_already_used_is_a_zero_change():
    with tempfile.TemporaryDirectory() as tmp:
        sessions = [("root", None, "Solo", "/tmp/p", 1760000000000, 1.5, 3_000_000)]
        messages = [  # one model, two turns, only one of them billed
            _whatif_msg("root", "anthropic", "claude-opus-4.5", 1.5, 1_000_000),
            _whatif_msg("root", "anthropic", "claude-opus-4.5", 0, 2_000_000),
        ]
        app = _whatif_app(tmp, sessions, messages)
        wf = app.loaded[0]
        opus_in = ot.model_price("anthropic/claude-opus-4.5")[0]

        app.select_whatif_model("anthropic/claude-opus-4.5")
        actual, whatif = app.whatif_session_totals(wf)
        assert round(actual, 9) == round(whatif, 9) == round(3 * opus_in, 9)

        lines = app.renderer.detail_overview(wf, 100)
        assert f"+{ot.money(0)} (+0%)" in _wi_row(lines, "★ Change")
        # The old baseline was the node's recorded cost, which kept the one billed turn's
        # $1.50 as the whole of it and reported a +$13.50 "saving" that never existed.
        assert round(app.session_node_rows("root")[0]["cost"], 6) == 1.5


def test_whatif_baseline_prices_a_partially_recorded_session_in_full():
    with tempfile.TemporaryDirectory() as tmp:
        sessions = [
            ("root", None, "Mixed", "/tmp/p", 1760000000000, 0.01, 3_000_000),
            ("kid", "root", "Docs", "/tmp/p", 1760000001000, 0, 1_000_000),
        ]
        messages = [
            _whatif_msg("root", "anthropic", "claude-opus-4.5", 0.01, 1_000_000),  # metered
            _whatif_msg("root", "anthropic", "claude-opus-4.5", 0, 2_000_000),  # subscription
            _whatif_msg("kid", "anthropic", "claude-haiku-4.5", 0, 1_000_000),
        ]
        app = _whatif_app(tmp, sessions, messages)
        wf = app.loaded[0]
        opus_in = ot.model_price("anthropic/claude-opus-4.5")[0]
        haiku_in = ot.model_price("anthropic/claude-haiku-4.5")[0]

        app.select_whatif_model("anthropic/claude-haiku-4.5")
        actual, _whatif = app.whatif_session_totals(wf)
        # All 4M tokens at list rates -- not the recorded cent, and not the metered turn
        # alone: the subscription turns in the SAME node are priced too.
        assert round(actual, 9) == round(3 * opus_in + haiku_in, 9)
        assert actual > 0.01
        # The root node records that one cent, and it stays its Cost -- which is exactly
        # why the TOTAL line says the Cost column does not add up to the comparison.
        app.toggle_api_prices()  # to the real view; the estimate is the default
        root_node = next(n for n in app.session_node_rows("root") if n["depth"] == 0)
        assert round(root_node["cost"], 6) == 0.01
        note = next(ln for ln in app.renderer.detail_subagents(wf, 200) if ln.startswith("!"))
        assert "does not add up" in note


def test_whatif_baseline_prices_each_model_not_the_dominant_one():
    with tempfile.TemporaryDirectory() as tmp:
        sessions = [("root", None, "Mixed", "/tmp/p", 1760000000000, 0, 3_000_000)]
        messages = [  # dominant by tokens: Opus. But a third of the tokens are Haiku's.
            _whatif_msg("root", "anthropic", "claude-opus-4.5", 0, 2_000_000),
            _whatif_msg("root", "anthropic", "claude-haiku-4.5", 0, 1_000_000),
        ]
        app = _whatif_app(tmp, sessions, messages)
        wf = app.loaded[0]
        opus_in = ot.model_price("anthropic/claude-opus-4.5")[0]
        haiku_in = ot.model_price("anthropic/claude-haiku-4.5")[0]

        app.select_whatif_model("anthropic/claude-opus-4.5")
        actual, whatif = app.whatif_session_totals(wf)
        assert round(actual, 9) == round(2 * opus_in + haiku_in, 9)  # each model its own
        assert round(actual, 9) != round(3 * opus_in, 9)  # NOT all of it at the dominant one
        # ...and the counterfactual is the whole session at the target's rates.
        assert round(whatif, 9) == round(3 * opus_in, 9)


def test_whatif_ignores_zero_token_model_rows():
    # OpenCode can emit named model rows for aborted turns with no usage.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp, solo=True)  # 1M Opus tokens, priced
        wf = app.loaded[0]
        app._model_by_root[wf.id].append(_model_row("unknown/not recorded", 0.0, 0))
        app._model_by_root[wf.id].append(_model_row("anthropic/claude-haiku-4.5", 0.0, 0))

        # The aborted rows name models, but neither is usage...
        assert [name for name, _tok in app.whatif_candidates()] == ["anthropic/claude-opus-4.5"]
        # ...and the unpriceable zero-token row does not turn an exact baseline into "~".
        app.select_whatif_model("anthropic/claude-opus-4.5")
        assert not app.whatif_baseline_is_estimated(wf)


def test_whatif_baseline_is_marked_estimated_when_a_model_has_no_list_rate():
    with tempfile.TemporaryDirectory() as tmp:
        sessions = [("root", None, "Solo", "/tmp/p", 1760000000000, 0, 2_000_000)]
        messages = [
            _whatif_msg("root", "anthropic", "claude-opus-4.5", 0, 1_000_000),
            _whatif_msg("root", "unknown", "not recorded", 0, 1_000_000),
        ]
        app = _whatif_app(tmp, sessions, messages)
        wf = app.loaded[0]
        app.select_whatif_model("anthropic/claude-opus-4.5")
        assert app.whatif_baseline_is_estimated(wf)
        lines = app.renderer.detail_overview(wf, 100)
        assert "~$" in _wi_row(lines, "★ Your models (list)")  # the estimate marker
        assert any("no known list rate" in ln for ln in lines)

    # A session priced end to end carries no marker.
    with tempfile.TemporaryDirectory() as tmp2:
        clean = _whatif_app(
            tmp2,
            [("s2", None, "Clean", "/tmp/p", 1760000000000, 0, 1_000_000)],
            [_whatif_msg("s2", "anthropic", "claude-opus-4.5", 0, 1_000_000)],
        )
        w2 = clean.loaded[0]
        clean.select_whatif_model("anthropic/claude-opus-4.5")
        assert not clean.whatif_baseline_is_estimated(w2)
        wi = _wi_row(clean.renderer.detail_overview(w2, 100), "★ Your models (list)")
        assert "$" in wi and "~$" not in wi  # priced end to end -- no estimate marker


def test_whatif_target_survives_a_dataset_that_never_used_it():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.select_whatif_model("anthropic/claude-haiku-4.5")
        assert app.whatif_model == "anthropic/claude-haiku-4.5"

        # The new dataset uses only Opus; the priceable Haiku target stays armed...
        app._model_by_root = {"root": [_model_row("anthropic/claude-opus-4.5", 1.0, 10)]}
        app._revalidate_whatif()
        assert app.whatif_model == "anthropic/claude-haiku-4.5"

        # ...while a target with no list rate at all is what gets dropped.
        app.whatif_model = "unknown (not recorded)"
        app._revalidate_whatif()
        assert app.whatif_model is None
        assert app.toasts and "no list rate" in app.toasts[-1].text


def test_whatif_price_refresh_drops_a_target_that_lost_its_list_rate():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.select_whatif_model("anthropic/claude-opus-4.5")
        app.whatif_model = "vendor/dropped-by-refresh"  # a target the new catalog won't price

        orig = ot.tui.app.refresh_model_prices
        ot.tui.app.refresh_model_prices = lambda *a, **k: (0, "/tmp/prices.json")  # no network
        try:
            app.refresh_prices_action()
        finally:
            ot.tui.app.refresh_model_prices = orig
        # The target is dropped -- not left armed to reprice at FALLBACK_PRICE. (The
        # revalidate's "no list rate" toast is collapsed onto the refresh's own success
        # toast, both being set within this one handler with no paint between, so we
        # assert the drop itself, which is the fix.)
        assert app.whatif_model is None


def test_whatif_candidates_skip_models_with_no_known_price():
    with tempfile.TemporaryDirectory() as tmp:
        sessions = [("root", None, "Solo", "/tmp/p", 1760000000000, 0, 3_000_000)]
        messages = [
            _whatif_msg("root", "anthropic", "claude-opus-4.5", 0, 1_000_000),
            _whatif_msg("root", "unknown", "not recorded", 0, 1_000_000),
            _whatif_msg("root", "ollama", "llama3.3", 0, 1_000_000),  # local: no API rate
        ]
        app = _whatif_app(tmp, sessions, messages)
        names = {m["model_name"] for m in app._model_by_root["root"]}
        assert "unknown/not recorded" in names and "ollama/llama3.3" in names  # all used...
        assert ot.model_price("unknown/not recorded") == ot.pricing.FALLBACK_PRICE
        # ...but only the one we can actually price is offered as a target.
        assert [name for name, _tok in app.whatif_candidates()] == ["anthropic/claude-opus-4.5"]
        assert not ot.has_known_price("unknown/not recorded")
        assert not ot.has_known_price("ollama/llama3.3")
        assert ot.has_known_price("anthropic/claude-opus-4.5")
    # ...including the literal label every store falls back to when a message records no
    # model at all -- the one the picker used to offer as if it had a rate card.
    assert not ot.has_known_price("unknown (not recorded)")


def test_whatif_change_of_a_zero_baseline_is_never_a_plus_dash():
    with tempfile.TemporaryDirectory() as tmp:
        sessions = [
            ("root", None, "Local", "/tmp/p", 1760000000000, 0, 1_000_000),
            ("paid", None, "Paid", "/tmp/p", 1760000001000, 1.0, 1_000_000),
        ]
        messages = [
            _whatif_msg("root", "ollama", "llama3.3", 0, 1_000_000),
            _whatif_msg("paid", "anthropic", "claude-opus-4.5", 1.0, 1_000_000),
        ]
        app = _whatif_app(tmp, sessions, messages)
        wf = next(w for w in app.loaded if w.id == "root")

        app.select_whatif_model("anthropic/claude-opus-4.5")
        actual, whatif = app.whatif_session_totals(wf)
        assert actual == 0 and whatif > 0  # local tokens have no list price to bill
        change = _wi_row(app.renderer.detail_overview(wf, 100), "★ Change")
        assert "+-" not in change
        assert "(-)" in change  # undefined % of a zero baseline prints bare, never "+-"
        assert ot.Renderer.signed_pct(1.0, 0, "+") == "-"  # ...the rule itself
        assert ot.Renderer.signed_pct(1.0, 4.0, "+") == "+25%"


def test_whatif_subagents_tab_prices_the_session_tree_and_the_savings_footer():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        wf = app.loaded[0]
        opus_in = ot.model_price("anthropic/claude-opus-4.5")[0]
        haiku_in = ot.model_price("anthropic/claude-haiku-4.5")[0]
        baseline = opus_in + 2 * haiku_in  # 1M Opus + 2M Haiku, each at its own rates

        app.select_whatif_model("anthropic/claude-opus-4.5")
        lines = _table(app.renderer.detail_subagents(wf, 200))
        cells = box_cells(lines)
        assert "anthropic/claude-opus-4.5" in lines[0]  # the box title names the target
        assert "What-if" in cells[0] and "Δ" not in cells[0]
        # The root joins the table: both nodes of the tree are listed.
        depths = [c.split()[2] for c in cells[1:3]]
        assert sorted(depths) == ["0", "1"]
        # Cost is what was recorded (the ordinary $-gated column); What-if is that node's
        # own tokens at the target's rates -- exact, one model, one rate card.
        root = next(ln for ln in lines if "Root" in ln)
        assert ot.money(1.5) in root and ot.money(opus_in) in root
        kid = next(ln for ln in lines if "Docs" in ln)
        assert ot.money(0.44) in kid and ot.money(2 * opus_in) in kid
        total = next(ln for ln in lines if ln.startswith("TOTAL"))
        assert total.startswith("TOTAL (list rates)")
        assert f"your models {ot.money(baseline)}" in total  # NOT the $1.94 recorded
        assert ot.money(1.94) not in total
        assert f"all at anthropic/claude-opus-4.5 {ot.money(3 * opus_in)}" in total
        # Opus for everything costs more than the routed mix did, at list rates.
        assert f"cost more {ot.money(3 * opus_in - baseline)}" in total


def test_subagents_tab_is_unchanged_without_a_whatif_target():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        lines = _table(app.renderer.detail_subagents(app.loaded[0], 200))
        assert "Subagent Executions" in lines[0]  # the title, on the box's top border
        cells = box_cells(lines)
        assert "What-if" not in cells[0] and "Δ" not in cells[0]
        assert not any(ln.startswith("TOTAL") for ln in lines)
        rows = [c for c in cells[1:] if c.strip()]
        assert len(rows) == 1 and "Docs" in rows[0]  # the subagent only, never the root


def test_whatif_target_never_moves_an_app_wide_cost():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        wf = app.loaded[0]
        before = (wf.total_cost, wf.root_cost)
        months = [(m.month, m.cost) for m in app.months]
        days = [(d.day, d.cost) for d in app.days]
        mix = {m["model_name"]: m["cost"] for m in app.model_mix("root")}
        assert round(sum(w.total_cost for w in app.loaded), 2) == 1.94  # actual spend

        app.select_whatif_model("anthropic/claude-opus-4.5")
        assert (wf.total_cost, wf.root_cost) == before  # the session row itself
        assert [(m.month, m.cost) for m in app.months] == months  # the month rollup
        assert [(d.day, d.cost) for d in app.days] == days  # the day rollup
        assert {m["model_name"]: m["cost"] for m in app.model_mix("root")} == mix
        assert round(sum(w.total_cost for w in app.loaded), 2) == 1.94
        # ...while the one place that IS scoped to the target shows the counterfactual.
        assert any(ln.startswith("TOTAL") for ln in app.renderer.detail_subagents(wf, 200))

        app.clear_whatif_model()
        assert (wf.total_cost, wf.root_cost) == before
        assert round(sum(w.total_cost for w in app.loaded), 2) == 1.94


def test_dollar_toggle_still_works_while_a_whatif_target_is_armed():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp, costs=(0, 0))
        wf = app.loaded[0]
        opus_in = ot.model_price("anthropic/claude-opus-4.5")[0]
        haiku_in = ot.model_price("anthropic/claude-haiku-4.5")[0]
        estimate = opus_in + 2 * haiku_in  # each row at its OWN model's list rates
        app.toggle_api_prices()  # to the real view; the estimate is the default
        assert round(wf.total_cost, 6) == 0.0  # nothing recorded, "$" off

        app.select_whatif_model("anthropic/claude-opus-4.5")
        assert round(wf.total_cost, 6) == 0.0  # arming reprices nothing app-wide

        app.toggle_api_prices()  # "$" on -- and it still moves the number
        assert app.show_api_prices
        assert round(wf.total_cost, 6) == round(estimate, 6)
        assert round(wf.total_cost, 6) != round(3 * opus_in, 6)  # not the what-if figure

        app.toggle_api_prices()  # ...and back off, target still armed
        assert not app.show_api_prices
        assert round(wf.total_cost, 6) == 0.0
        assert app.whatif_model == "anthropic/claude-opus-4.5"


def test_whatif_baseline_prices_unrecorded_usage_even_with_the_dollar_toggle_off():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp, costs=(0, 0))  # subscription: tokens, no recorded cost
        wf = app.loaded[0]
        opus_in = ot.model_price("anthropic/claude-opus-4.5")[0]
        haiku_in = ot.model_price("anthropic/claude-haiku-4.5")[0]

        app.select_whatif_model("anthropic/claude-opus-4.5")
        baseline = opus_in + 2 * haiku_in  # 1M Opus + 2M Haiku, both estimated
        for api_toggle in (True, False):  # the baseline must not depend on "$"
            app.show_api_prices = api_toggle
            app._apply_price_mode()
            assert round(app.whatif_session_totals(wf)[0], 9) == round(baseline, 9)
            total = next(
                ln for ln in app.renderer.detail_subagents(wf, 200) if ln.startswith("TOTAL")
            )
            assert f"your models {ot.money(baseline)}" in total
            assert ot.money(0.0) not in total  # never "$0.00 → ..." / a 100% saving
            assert f"cost more {ot.money(3 * opus_in - baseline)}" in total


def test_whatif_target_is_not_tagged_into_the_header():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.toggle_api_prices()  # to the real view, where only the target could tag it

        class _Scr(FakeScreen):
            # The header/footer also draw rules; FakeScreen only records addstr text.
            def hline(self, y, x, ch, n):
                pass

        app.select_whatif_model("anthropic/claude-opus-4.5")
        app.can_switch_source = lambda: False  # the bare test Args carries no source flags
        screen, footer = _Scr(24, 130), _Scr(24, 130)
        # color_pair()/init_pair()/ACS_* need a live initscr(); stub them for headless.
        orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
        ot.curses.color_pair = lambda n: 0
        ot.curses.init_pair = lambda *a: None
        ot.curses.ACS_HLINE = getattr(ot.curses, "ACS_HLINE", ord("-"))
        try:
            app.renderer.draw_header(screen, 130)
            app.renderer.draw_footer(footer, 24, 130)
        finally:
            ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
        header = screen_text(screen).splitlines()[0]
        assert "WHAT-IF" not in header and "opus" not in header
        assert ot.money(1.94) in header  # still the actual spend

        # ... and the footer chip does light up, so "armed" is never invisible.
        assert "w model" in screen_text(footer)


def test_whatif_key_opens_the_picker_and_clears_an_active_target():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        # Most-used model first: the subagent's 2M Haiku beats the root's 1M Opus.
        assert [name for name, _tok in app.whatif_candidates()] == [
            "anthropic/claude-haiku-4.5",
            "anthropic/claude-opus-4.5",
        ]
        assert app.handle_key(None, ord("w"))
        assert app.whatif_menu and app.whatif_menu_index == 0
        assert app.handle_key(None, ord("j"))
        assert app.handle_key(None, 10)
        assert not app.whatif_menu
        assert app.whatif_model == "anthropic/claude-opus-4.5"
        assert round(app.loaded[0].total_cost, 2) == 1.94
        assert "What-if" in _table_cells(app.renderer.detail_subagents(app.loaded[0], 200))[0]

        assert app.handle_key(None, ord("w"))
        assert not app.whatif_menu
        assert app.whatif_model is None
        assert round(app.loaded[0].total_cost, 2) == 1.94
        assert "What-if" not in app.renderer.detail_subagents(app.loaded[0], 200)[1]

        assert app.handle_key(None, ord("w"))
        assert app.whatif_menu
        assert app.handle_key(None, 27)
        assert not app.whatif_menu and app.whatif_model is None


def test_whatif_picker_filters_the_model_list():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.toggle_whatif()
        assert app.whatif_menu and [n for n, _t in app.whatif_rows()] == [
            "anthropic/claude-haiku-4.5",  # most tokens first, never re-ranked by match
            "anthropic/claude-opus-4.5",
        ]

        app.handle_whatif_menu_key(ord("f"))
        assert app.whatif_filter_active
        for ch in "opus4-5":  # a subsequence over the fully-qualified name
            app.handle_whatif_menu_key(ord(ch))
        assert [n for n, _t in app.whatif_rows()] == ["anthropic/claude-opus-4.5"]

        app.handle_whatif_menu_key(10)
        assert app.whatif_model == "anthropic/claude-opus-4.5"
        assert not app.whatif_menu

        app.toggle_whatif()
        app.toggle_whatif()
        assert app.whatif_query == "" and len(app.whatif_rows()) == 2
        app.handle_whatif_menu_key(ord("f"))
        for ch in "haiku4.5":
            app.handle_whatif_menu_key(ord(ch))
        assert [n for n, _t in app.whatif_rows()] == ["anthropic/claude-haiku-4.5"]

        app.handle_whatif_menu_key(ord("z"))
        assert app.whatif_rows() == []
        app.handle_whatif_menu_key(10)
        assert app.whatif_menu and app.whatif_model is None  # still open, still unset
        app.handle_whatif_menu_key(27)
        assert app.whatif_menu and not app.whatif_filter_active and app.whatif_query == ""
        assert len(app.whatif_rows()) == 2


def test_whatif_catalog_candidates_cover_the_catalog_and_price_what_they_show():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        rows = app.whatif_catalog_candidates()
        assert len(rows) > 100  # the whole catalog, not the two models this data used

        canons = [ot.pricing.canonical_model(name) for name, _eff, _approx in rows]
        assert len(canons) == len(set(canons))  # one row per canonical model

        mix = app.price_token_mix()
        shares = mix[0] if mix else (1.0, 0.0, 0.0, 0.0)
        for name, eff, approx in rows[:50]:
            got_eff, got_approx = ot.pricing.effective_price(ot.model_price(name), shares)
            assert (round(got_eff, 9), got_approx) == (round(eff, 9), approx)

        # Cheapest-for-your-mix first (the P models.dev leaderboard order), and no
        # local or $0-rate rows -- there is no API rate to substitute in.
        assert [e for _n, e, _a in rows] == sorted(e for _n, e, _a in rows)
        assert not any(ot.pricing.is_local_provider(n) for n, _e, _a in rows)

        # The catalog tier survives a picker round-trip through the memo (the list is
        # asked per keystroke) and re-computes after the model scan re-runs.
        assert app.whatif_catalog_candidates() is rows
        app._load_model_cache()
        assert app.whatif_catalog_candidates() is not rows


def test_whatif_picker_tab_flips_to_the_catalog_and_arms_from_it():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.toggle_whatif()
        assert app.whatif_menu and not app.whatif_catalog

        app.handle_whatif_menu_key(ord("f"))
        for ch in "haiku4.5":
            app.handle_whatif_menu_key(ord(ch))
        app.handle_whatif_menu_key(ord("\t"))
        assert app.whatif_catalog and app.whatif_query == "haiku4.5"
        assert app.whatif_menu_index == 0
        rows = app.whatif_rows()
        assert rows and all("haiku" in name for name, _eff, _approx in rows)

        app.handle_whatif_menu_key(10)
        assert not app.whatif_menu
        assert app.whatif_model == rows[0][0]
        assert ot.has_known_price(app.whatif_model)
        # The armed catalog target drives the Subagents tab like any used model.
        lines = _table(app.renderer.detail_subagents(app.loaded[0], 200))
        assert app.whatif_model in lines[0]
        assert any(ln.startswith("TOTAL (list rates)") for ln in lines)


def test_whatif_picker_renders_the_tier_tab_strip():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.toggle_whatif()
        orig_cp = ot.curses.color_pair
        ot.curses.color_pair = lambda n: 0
        try:
            screen = FakeScreen(30, 90)
            app.renderer.draw_whatif_menu(screen, 30, 90)
            text = screen_text(screen)
            assert "[your models]" in text and " models.dev " in text
            tabs = [r for r in app.renderer.regions if r[0] == "whatiftab"]
            assert [t[-1] for t in tabs] == [0, 1]

            app.whatif_toggle_catalog()
            screen2 = FakeScreen(30, 90)
            app.renderer.draw_whatif_menu(screen2, 30, 90)
            assert "[models.dev]" in screen_text(screen2)
        finally:
            ot.curses.color_pair = orig_cp


def test_whatif_picker_keeps_the_selected_row_on_screen_at_the_minimum_height():
    # The app frame leaves an 18-row picker at the 80x20 minimum terminal size.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.toggle_whatif()
        app.whatif_catalog = True
        app.whatif_filter_active = True
        app.whatif_query = "gpt"  # a long filtered catalog list
        rows = app.whatif_rows()
        assert len(rows) > 6
        app.whatif_menu_index = len(rows) - 1
        orig_cp = ot.curses.color_pair
        ot.curses.color_pair = lambda n: 0
        try:
            screen = FakeScreen(18, 100)
            app.renderer.draw_whatif_menu(screen, 18, 100)
            selected = rows[app.whatif_menu_index][0]
            assert selected[:20] in screen_text(screen)
        finally:
            ot.curses.color_pair = orig_cp


def test_whatif_picker_tier_tab_click_flips_the_tier():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.toggle_whatif()
        assert app.whatif_menu and not app.whatif_catalog
        app.renderer.regions = [  # what draw_whatif_menu's draw_tabs registers
            ("whatiftab", 5, 10, 22, 0),
            ("whatiftab", 5, 25, 36, 1),
        ]
        orig = ot.curses.getmouse
        try:
            ot.curses.getmouse = lambda: (0, 30, 5, 0, ot.curses.BUTTON1_CLICKED)
            app.handle_mouse()
            assert app.whatif_menu and app.whatif_catalog
            app.handle_mouse()
            assert app.whatif_menu and app.whatif_catalog
            ot.curses.getmouse = lambda: (0, 30, 15, 0, ot.curses.BUTTON1_CLICKED)
            app.handle_mouse()
            assert not app.whatif_menu
        finally:
            ot.curses.getmouse = orig


def test_whatif_picker_h_l_switch_the_tier_tabs():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.toggle_whatif()
        assert app.whatif_menu and not app.whatif_catalog
        app.handle_whatif_menu_key(ord("l"))
        assert app.whatif_catalog
        app.handle_whatif_menu_key(ord("h"))
        assert not app.whatif_catalog
        app.handle_whatif_menu_key(ot.curses.KEY_RIGHT)
        assert app.whatif_catalog

        app.handle_whatif_menu_key(ord("f"))  # the live filter owns printable keys
        app.handle_whatif_menu_key(ord("h"))
        assert app.whatif_query == "h" and app.whatif_catalog  # typed, not flipped


def test_whatif_picker_opens_on_the_catalog_when_nothing_used_is_priceable():
    with tempfile.TemporaryDirectory() as tmp:
        sessions = [("root", None, "Solo", "/tmp/p", 1760000000000, 0, 1_000_000)]
        messages = [_whatif_msg("root", "unknown", "not recorded", 0, 1_000_000)]
        app = _whatif_app(tmp, sessions, messages)
        assert app.whatif_candidates() == []

        app.handle_key(None, ord("w"))
        assert app.whatif_menu and app.whatif_catalog
        app.handle_whatif_menu_key(ord("\t"))  # nothing to flip back to: Tab is inert
        assert app.whatif_catalog
        app.handle_whatif_menu_key(10)  # Enter arms the cheapest-for-the-mix row
        assert app.whatif_model and ot.has_known_price(app.whatif_model)


def test_whatif_is_allowed_in_demo_mode_unlike_the_dollar_toggle():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp, demo=True)  # demo-scaled at load, like a real --demo run
        wf = app.loaded[0]
        demo_total = wf.total_cost  # the hidden scale is per-process; compare to itself
        demo_row = app.model_mix("root")[0]["cost"]

        app.toggle_api_prices()  # "$" refuses outright
        assert not app.show_api_prices

        app.handle_key(None, ord("w"))  # `w` opens its picker regardless
        assert app.whatif_menu
        app.handle_key(None, 10)  # Enter arms the top row (the most-used model)
        assert app.whatif_model

        # The Subagents tab prices the (scaled) tree at the target's rates, both sides at
        # list rates like everywhere else...
        lines = _table(app.renderer.detail_subagents(wf, 200))
        assert app.whatif_model in lines[0]
        total = next(ln for ln in lines if ln.startswith("TOTAL"))
        assert total.startswith("TOTAL (list rates)") and "your models" in total
        # ...and, as everywhere else, the app-wide demo figures do not budge.
        assert round(wf.total_cost, 6) == round(demo_total, 6)
        assert round(app.model_mix("root")[0]["cost"], 6) == round(demo_row, 6)

        app.handle_key(None, ord("w"))  # clears the target
        assert app.whatif_model is None
        assert round(wf.total_cost, 6) == round(demo_total, 6)
        assert round(app.model_mix("root")[0]["cost"], 6) == round(demo_row, 6)
