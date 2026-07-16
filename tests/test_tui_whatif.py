"""The `w` what-if model: a session-scoped comparison at one model's list rates."""

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
    screen_text,
    workflow,
)


def test_local_usage_stays_zero_in_what_if_view():
    # A local-only session: $0 recorded, real tokens. The "$" what-if must not turn
    # those tokens into cloud dollars.
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
    ollama = next(ln for ln in app.renderer.trend_providers(80, 12) if ln.startswith("ollama"))
    assert "$0.00" in ollama


def test_whatif_overview_answers_for_a_solo_session_that_has_no_subagents():
    # A session that delegated nothing has no tree for the Subagents tab to table, so the
    # Overview is where its what-if lives -- otherwise `w` would silently do nothing on
    # every solo session. Neutral wording on purpose: with no delegation there is no
    # routing decision to credit, so it reports the change, not a "routing saved".
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp, solo=True)
        wf = app.loaded[0]
        assert wf.subagents == 0
        opus_in = ot.model_price("anthropic/claude-opus-4.5")[0]
        haiku_in = ot.model_price("anthropic/claude-haiku-4.5")[0]

        assert not any("What-if" in ln for ln in app.renderer.detail_overview(wf, 100))

        app.select_whatif_model("anthropic/claude-haiku-4.5")
        lines = app.renderer.detail_overview(wf, 100)
        assert "# What-if · anthropic/claude-haiku-4.5" in lines
        # BOTH sides at list rates -- the baseline is its 1M Opus tokens at Opus rates,
        # NOT the $1.50 that happened to be recorded (that would compare a list-price
        # counterfactual against a metered bill and call the difference a saving).
        assert f"Your models:  {ot.money(opus_in)}   (list rates, each model its own)" in lines
        assert f"All at anthropic/claude-haiku-4.5:  {ot.money(haiku_in)}" in lines
        # Cheaper than the models that ran it, so the change is negative -- and never
        # phrased as "routing": nothing was routed.
        change = next(ln for ln in lines if ln.startswith("Change:"))
        assert change.startswith(f"Change:       -{ot.money(opus_in - haiku_in)}")
        assert not any("routing" in ln for ln in lines)
        # The Subagents tab still, correctly, has nothing to show.
        assert "No subagents used" in app.renderer.detail_subagents(wf, 100)[1]


def test_whatif_overview_and_subagents_tab_never_quote_different_totals():
    # Both read whatif_session_totals, so a session's two what-if views cannot drift.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        wf = app.loaded[0]
        app.select_whatif_model("anthropic/claude-opus-4.5")
        actual, whatif = app.whatif_session_totals(wf)
        assert abs(actual - _whatif_baseline(app, "root")) < 1e-9

        overview = app.renderer.detail_overview(wf, 100)
        assert f"Your models:  {ot.money(actual)}   (list rates, each model its own)" in overview
        assert f"All at anthropic/claude-opus-4.5:  {ot.money(whatif)}" in overview
        total = next(ln for ln in app.renderer.detail_subagents(wf, 200) if ln.startswith("TOTAL"))
        assert f"your models {ot.money(actual)} → " in total
        assert f"all at anthropic/claude-opus-4.5 {ot.money(whatif)}" in total


def test_whatif_target_a_single_model_session_already_used_is_a_zero_change():
    # THE invariant, and the one that catches every baseline bug at once: repricing a
    # session that ran on ONE model at that same model's rates is a substitution of a
    # rate card for itself -- exactly $0 change, whatever was (or wasn't) recorded.
    # The old node-row baseline failed this: a session with $1.50 recorded and $5.00 of
    # list-rate value reported a fake +$3.50 "what-if".
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
        assert f"Change:       +{ot.money(0)} (+0% vs your models)" in lines
        # The old baseline was the node's recorded cost, which kept the one billed turn's
        # $1.50 as the whole of it and reported a +$13.50 "saving" that never existed.
        assert round(app.session_node_rows("root")[0]["cost"], 6) == 1.5


def test_whatif_baseline_prices_a_partially_recorded_session_in_full():
    # A session whose turns are MOSTLY subscription ($0) with a little metered usage: the
    # baseline must price every token at list rates, not pass the recorded cents off as
    # the whole comparison. (Measured on real data: 20 of 48 metered nodes are shaped
    # like this, and the node-cost baseline understated one by $21.40.)
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
        root_node = next(n for n in app.session_node_rows("root") if n["depth"] == 0)
        assert round(root_node["cost"], 6) == 0.01
        note = next(ln for ln in app.renderer.detail_subagents(wf, 200) if ln.startswith("!"))
        assert "does not add up" in note


def test_whatif_baseline_prices_each_model_not_the_dominant_one():
    # A node carries ONE model label (its dominant one), so pricing its whole token split
    # at that label is wrong for every session that switched model mid-flight. The
    # baseline goes through the per-model rows instead. (Measured: 73 of 147 multi-model
    # sessions were off by >5%, worst case 47%.)
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
    # OpenCode emits a model row with no tokens for an assistant record whose usage never
    # landed (an aborted turn). It names a model but is not usage, so it must neither
    # float that model into the picker (where it could keep a stale target alive through
    # _revalidate_whatif) nor, if it is unpriceable, mark an otherwise exact baseline "~".
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp, solo=True)  # 1M Opus tokens, priced
        wf = app.loaded[0]
        app._model_by_root[wf.id].append(_model_row("unknown/not recorded", 0.0, 0))
        app._model_by_root[wf.id].append(_model_row("anthropic/claude-haiku-4.5", 0.0, 0))

        # The aborted rows name models, but neither is usage...
        assert [name for name, _tok in app.whatif_candidates()] == ["anthropic/claude-opus-4.5"]
        # ...so an armed Haiku target is NOT kept alive by the zero-token Haiku row,
        app.whatif_model = "anthropic/claude-haiku-4.5"
        app._revalidate_whatif()
        assert app.whatif_model is None
        # ...and the unpriceable zero-token row does not turn an exact baseline into "~".
        app.select_whatif_model("anthropic/claude-opus-4.5")
        assert not app.whatif_baseline_is_estimated(wf)


def test_whatif_baseline_is_marked_estimated_when_a_model_has_no_list_rate():
    # Every token is priced (dropping the unpriceable ones would understate the baseline),
    # but a generic guess is not a list price -- so the figure wears a `~` instead of
    # quoting an invented rate as fact.
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
        assert any(ln.startswith("Your models:  ~$") for ln in lines)
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
        assert any(
            ln.startswith("Your models:  $") for ln in clean.renderer.detail_overview(w2, 100)
        )


def test_whatif_target_is_dropped_when_the_data_no_longer_uses_it():
    # Arm a target, then reload into a dataset that never used it (a `c` source switch or
    # `D` demo toggle does exactly this). A fresh App would refuse to arm it, so leaving
    # it armed would quietly answer a question about a model this data has never seen.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.select_whatif_model("anthropic/claude-haiku-4.5")
        assert app.whatif_model == "anthropic/claude-haiku-4.5"

        # The new dataset uses only Opus; the armed Haiku target must not survive it.
        app._model_by_root = {"root": [_model_row("anthropic/claude-opus-4.5", 1.0, 10)]}
        app._revalidate_whatif()
        assert app.whatif_model is None
        assert app.toasts and "not used in this data" in app.toasts[-1].text


def test_whatif_candidates_skip_models_with_no_known_price():
    # An unpriceable model must not be armable: model_price() falls back to a generic
    # mid-range guess for an id it doesn't know, so arming "unknown (not recorded)" would
    # quote "$2.00/M at unknown (not recorded) list rates" -- a rate that exists nowhere.
    # Same spirit as the local-model exclusion (no API rate at all).
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
    # pct() answers "-" for a zero denominator (an undefined share), and gluing a sign to
    # that printed "(+- vs actual)". A session whose models are all local prices to $0 at
    # list rates, which is exactly that denominator.
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
        change = next(
            ln for ln in app.renderer.detail_overview(wf, 100) if ln.startswith("Change:")
        )
        assert "+-" not in change
        assert change.endswith("(- vs your models)")
        assert ot.Renderer.signed_pct(1.0, 0, "+") == "-"  # ...the rule itself
        assert ot.Renderer.signed_pct(1.0, 4.0, "+") == "+25%"


def test_whatif_subagents_tab_prices_the_session_tree_and_the_savings_footer():
    # The feature's ONE visible effect: on a session's Subagents tab, the whole tree
    # (root row included), each node's Cost beside an EXACT per-node What-if -- and no
    # per-node Δ, because a node that mixed models has no baseline we can compute. The
    # exact comparison is the TOTAL line: both sides at list rates.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        wf = app.loaded[0]
        opus_in = ot.model_price("anthropic/claude-opus-4.5")[0]
        haiku_in = ot.model_price("anthropic/claude-haiku-4.5")[0]
        baseline = opus_in + 2 * haiku_in  # 1M Opus + 2M Haiku, each at its own rates

        app.select_whatif_model("anthropic/claude-opus-4.5")
        lines = app.renderer.detail_subagents(wf, 200)
        assert "anthropic/claude-opus-4.5" in lines[0]  # header names the target
        assert "What-if" in lines[1] and "Δ" not in lines[1]
        # The root joins the table: both nodes of the tree are listed.
        depths = [ln.split()[2] for ln in lines[2:4]]
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
    # Strict requirement: no target, no change -- the old columns, no root row, no footer.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        lines = app.renderer.detail_subagents(app.loaded[0], 200)
        assert lines[0] == "# Subagent Executions"
        assert "What-if" not in lines[1] and "Δ" not in lines[1]
        assert not any(ln.startswith("TOTAL") for ln in lines)
        rows = [ln for ln in lines[2:] if ln.strip()]
        assert len(rows) == 1 and "Docs" in rows[0]  # the subagent only, never the root


def test_whatif_target_never_moves_an_app_wide_cost():
    # The what-if target is SESSION-SCOPED. Arming one must not touch a single app-wide
    # figure -- not the session's own cost, not a day/month rollup, not the model mix.
    # (It used to reprice all of them, which left "$" nothing to move: a dead key that
    # still flipped -- and persisted -- show_api_prices behind an unchanged screen.)
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
    # Subscription-shaped data ($0 recorded, real tokens): "$" is the only thing that
    # can move those rows, and an armed what-if target must neither move them itself nor
    # take the toggle away. The two features are independent and never fight.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp, costs=(0, 0))
        wf = app.loaded[0]
        opus_in = ot.model_price("anthropic/claude-opus-4.5")[0]
        haiku_in = ot.model_price("anthropic/claude-haiku-4.5")[0]
        estimate = opus_in + 2 * haiku_in  # each row at its OWN model's list rates
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
    # Regression: on a subscription backend every recorded cost is $0, so a what-if
    # table that took its baseline from the "$" toggle compared a real counterfactual
    # against nothing and reported "routing saved 100%". The baseline prices every token
    # at its own model's list rates, exactly as the counterfactual does -- and does not
    # move with "$".
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
    # The header counts real/estimated spend, which an armed target does not change --
    # tagging it "WHAT-IF" would call recorded money counterfactual. The Subagents tab
    # titles and caveats itself; the footer chip is the honest "a target is armed".
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)

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
        assert app.handle_key(None, ord("j"))  # move to the Opus row
        assert app.handle_key(None, 10)  # Enter arms it
        assert not app.whatif_menu
        assert app.whatif_model == "anthropic/claude-opus-4.5"
        # Armed, not applied: the sessions list still reads the actual $1.94, and only
        # the Subagents tab grows the what-if columns.
        assert round(app.loaded[0].total_cost, 2) == 1.94
        assert "What-if" in app.renderer.detail_subagents(app.loaded[0], 200)[1]

        # `w` with a target set clears it (no picker), and the tab goes back to normal.
        assert app.handle_key(None, ord("w"))
        assert not app.whatif_menu
        assert app.whatif_model is None
        assert round(app.loaded[0].total_cost, 2) == 1.94
        assert "What-if" not in app.renderer.detail_subagents(app.loaded[0], 200)[1]

        # Esc cancels the picker without arming anything.
        assert app.handle_key(None, ord("w"))
        assert app.whatif_menu
        assert app.handle_key(None, 27)
        assert not app.whatif_menu and app.whatif_model is None


def test_whatif_picker_filters_the_model_list():
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        app.toggle_whatif()  # opens the picker on the full list
        assert app.whatif_menu and [n for n, _t in app.whatif_rows()] == [
            "anthropic/claude-haiku-4.5",  # most tokens first, never re-ranked by match
            "anthropic/claude-opus-4.5",
        ]

        app.handle_whatif_menu_key(ord("f"))  # `f` starts the live filter
        assert app.whatif_filter_active
        for ch in "opus4-5":  # a subsequence over the fully-qualified name
            app.handle_whatif_menu_key(ord(ch))
        assert [n for n, _t in app.whatif_rows()] == ["anthropic/claude-opus-4.5"]

        # Enter selects the highlighted match outright -- type, Enter, done.
        app.handle_whatif_menu_key(10)
        assert app.whatif_model == "anthropic/claude-opus-4.5"
        assert not app.whatif_menu

        # Dots == dashes both ways: "4.5" typed against an id spelled "4-5" and back.
        app.toggle_whatif()  # a target is set, so this clears it
        app.toggle_whatif()  # ...and this reopens the picker, on a fresh full list
        assert app.whatif_query == "" and len(app.whatif_rows()) == 2
        app.handle_whatif_menu_key(ord("f"))
        for ch in "haiku4.5":
            app.handle_whatif_menu_key(ord(ch))
        assert [n for n, _t in app.whatif_rows()] == ["anthropic/claude-haiku-4.5"]

        # A query matching nothing selects nothing -- and Esc widens instead of closing.
        app.handle_whatif_menu_key(ord("z"))
        assert app.whatif_rows() == []
        app.handle_whatif_menu_key(10)
        assert app.whatif_menu and app.whatif_model is None  # still open, still unset
        app.handle_whatif_menu_key(27)
        assert app.whatif_menu and not app.whatif_filter_active and app.whatif_query == ""
        assert len(app.whatif_rows()) == 2


def test_whatif_is_allowed_in_demo_mode_unlike_the_dollar_toggle():
    # "$" refuses in demo (it would price real tokens); what-if does not. Demo already
    # scales every token by a hidden factor, so list rates on scaled tokens recover no
    # real dollars -- and the routing RATIO, the point of the feature, stays real.
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
        lines = app.renderer.detail_subagents(wf, 200)
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
