"""build_payload/session_extras and the served browser (web.py + webpage.py)."""

import json
import os
import re
import tempfile

import opentab as ot

from tests._support import (
    FakeStore,
    _whatif_app,
    _whatif_baseline,
    _whatif_db,
    _whatif_msg,
    app_with,
    workflow,
)

# --- The web browser (--html / --serve) -------------------------------------


class NodesFakeStore(FakeStore):
    # FakeStore + a two-node subagent tree, to exercise the payload's nodes embed.
    def workflow_nodes(self, workflow_id):
        return [
            {
                "id": workflow_id,
                "depth": 0,
                "agent": "-",
                "title": "root",
                "created_at": "2026-05-01 10:00:00",
                "cost": 2.0,
                "tokens_input": 1000,
                "tokens_output": 500,
                "tokens_reasoning": 0,
                "tokens_cache_read": 0,
                "tokens_cache_write": 0,
                "tokens_total": 1500,
                "model_name": "anthropic/claude-fable-5",
            },
            {
                "id": "ses_sub",
                "depth": 1,
                "agent": "explore",
                "title": "scout the codebase",
                "created_at": "2026-05-01 10:01:00",
                "cost": 0.0,  # a subscription node: $0 recorded, tokens present
                "tokens_input": 1_000_000,
                "tokens_output": 100_000,
                "tokens_reasoning": 0,
                "tokens_cache_read": 0,
                "tokens_cache_write": 0,
                "tokens_total": 1_100_000,
                "model_name": "anthropic/claude-fable-5",
            },
        ]


class TurnsFakeStore(FakeStore):
    # FakeStore + a message timeline, to exercise the --serve session extras.
    def supports_turns(self, workflow_id):
        return True

    def message_timeline(self, workflow_id):
        base = {
            "depth": 0,
            "agent": "-",
            "model_name": "anthropic/claude-fable-5",
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "prompt_id": "p1",
            "prompt_title": "do the thing",
            "prompt_full": "do the thing\nand do it properly, with tests",
        }
        return [
            dict(
                base,
                time="2026-05-01 10:00:05",
                cost=0.0,
                input=1000,
                output=200,
                tokens_total=1200,
            ),
            dict(
                base, time="2026-05-01 10:00:31", cost=0.5, input=400, output=100, tokens_total=500
            ),
        ]


def test_web_payload_carries_both_cost_snapshots():
    app = app_with(
        [
            workflow("w1", "2026-05-01 10:00:00", cost=3.0, tokens=1000, directory="/tmp/alpha"),
            workflow("w2", "2026-05-02 11:00:00", cost=1.0, tokens=500, directory="/tmp/beta"),
        ]
    )
    payload = ot.build_payload(app)
    meta = payload["meta"]
    assert meta["version"] == ot.__version__
    assert meta["recordsCost"] is True
    assert meta["range"] == "all time"
    assert meta["startApi"] is False
    assert meta["serve"] is False
    by_id = {w["id"]: w for w in payload["workflows"]}
    assert set(by_id) == {"w1", "w2"}
    w1 = by_id["w1"]
    # Fully priced usage: the real and the API-equivalent snapshot agree, and both
    # travel in the payload so the page's $ toggle is a client-side field swap.
    assert w1["real"] == 3.0 and w1["api"] == 3.0
    assert w1["project"] == "/tmp/alpha"
    assert w1["date"].startswith("2026-05-01")
    assert payload["nodes"] == {}  # no subagents -> no per-session tree queries


def test_web_payload_embeds_nodes_and_reprices_unpriced_ones():
    w = workflow("w1", "2026-05-01 10:00:00", cost=2.0, directory="/tmp/alpha")
    w.subagents = 1
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(NodesFakeStore([w]), args)
    payload = ot.build_payload(app)
    nodes = payload["nodes"]["w1"]
    assert [n["depth"] for n in nodes] == [0, 1]
    root, sub = nodes
    assert root["real"] == 2.0 and root["api"] == 2.0  # priced node: api == real
    assert sub["real"] == 0.0 and sub["api"] > 0  # $0 node repriced at list rates
    assert sub["agent"] == "explore" and sub["tokens"] == 1_100_000


def test_web_session_extras_reports_turns_with_both_costs():
    w = workflow("w1", "2026-05-01 10:00:00", cost=0.5)
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(TurnsFakeStore([w]), args)
    extras = ot.session_extras(app, "w1")
    assert extras["tools"] == []  # no supports_tools -> hidden, never shown empty
    first, second = extras["turns"]
    assert first["real"] == 0.0 and first["api"] > 0  # $0 turn gets a list-price figure
    assert second["real"] == 0.5 and second["api"] == 0.5  # priced turn stays as recorded
    assert first["promptTitle"] == "do the thing"
    # The whole prompt travels too, so the page's ▸ header can unfold/hover it.
    assert first["promptFull"] == "do the thing\nand do it properly, with tests"
    # The Context tab's data rides along: measured per-turn prompt sizes (input +
    # cache) + the live model's window; no composition opt-in -> comp stays empty.
    ctx = extras["context"]
    assert [p["v"] for p in ctx["points"]] == [1000, 400]
    assert ctx["window"] == ot.model_context_window("anthropic/claude-fable-5")
    assert ctx["mixedWindows"] is False and ctx["comp"] == []


def test_web_turns_ship_the_tools_each_step_called():
    # The page names what each step DID in its drilled view, and counts the calls on the
    # prompt row -- so the raw names travel per turn. Raw, not pre-labelled: folding and
    # shortening are width questions, and the page answers them at its own widths.
    class Working(TurnsFakeStore):
        def message_timeline(self, workflow_id):
            rows = super().message_timeline(workflow_id)
            rows[0]["tools"] = ["Read", "Bash", "Bash"]  # repeats kept: two calls, two
            return rows

    w = workflow("w1", "2026-05-01 10:00:00", cost=0.5)
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(Working([w]), args)
    first, second = ot.session_extras(app, "w1")["turns"]
    assert first["tools"] == ["Read", "Bash", "Bash"]
    # A backend that records none ships [], so both frontends drop the column rather
    # than draw it empty -- never a missing key the page would have to guard.
    assert second["tools"] == []


def test_web_turn_tools_are_sanitized_at_the_boundary_not_by_the_page():
    # The page's own gate can only reject what still LOOKS wrong on arrival, and a bare
    # list() launders the bad shapes into plausible ones: "Bash" becomes four one-letter
    # tools that pass every client-side check, a dict yields its KEYS, and a
    # non-iterable raises inside the /api/session handler. So the payload goes through
    # the same util.tool_names gate the TUI uses.
    def shipped(value):
        class S(TurnsFakeStore):
            def message_timeline(self, workflow_id):
                rows = super().message_timeline(workflow_id)
                rows[0]["tools"] = value
                return rows

        w = workflow("w1", "2026-05-01 10:00:00", cost=0.5)
        args = type("Args", (), {"since": None, "until": None, "days": None})()
        return ot.session_extras(ot.App(S([w]), args), "w1")["turns"][0]["tools"]

    assert shipped("Bash") == []  # NOT ["B", "a", "s", "h"]
    assert shipped({"Bash": 1}) == []  # NOT ["Bash"] off the keys
    assert shipped(3) == []  # NOT a TypeError out of the endpoint
    assert shipped([["x"], 3, None, "", "Bash"]) == ["Bash"]
    assert shipped(("Read", "Bash")) == ["Read", "Bash"]


def test_web_turns_carry_the_context_size_and_mark_compactions():
    # The page marks compactions in its Turns table like the TUI's detail_turns, so the
    # per-turn context size has to travel on the turn row (the Context tab's `points` are
    # main-thread-only and minute-stamped -- not something a turn can be matched back to).
    class Compacting(TurnsFakeStore):
        def message_timeline(self, workflow_id):
            rows = super().message_timeline(workflow_id)
            rows[0]["input"] = 900_000
            rows[0]["tokens_total"] = 900_200
            # a subagent turn between them: its own window, so ctx must ship as 0 and it
            # must not break the main thread's chain.
            rows.insert(1, dict(rows[0], depth=1, agent="explore", input=5_000, cost=0.0))
            return rows

    w = workflow("w1", "2026-05-01 10:00:00", cost=0.5)
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    turns = ot.session_extras(ot.App(Compacting([w]), args), "w1")["turns"]
    assert [t["ctx"] for t in turns] == [900_000, 0, 400]

    # ...but only where a turn's input+cache IS that request's prompt: a cumulative-delta
    # backend (Codex) opts out of the context curve, and ships zeros rather than numbers
    # the page would mark a compaction on while its own Context tab shows nothing.
    class Delta(Compacting):
        def supports_context_curve(self, workflow_id):
            return False

    flat = ot.session_extras(ot.App(Delta([w]), args), "w1")
    assert [t["ctx"] for t in flat["turns"]] == [0, 0, 0] and flat["context"] is None

    js = _js_source()
    # One rule, both views -- the JS twin of util.CONTEXT_COMPACT_*: the Context curve's
    # ▼ and the Turns markers call the same predicate, so they cannot disagree.
    assert js.count("const isCompaction = (before, after) => before > 50000") == 1
    assert "if (isCompaction(vs[i - 1], vs[i])) comps.push(i)" in js
    table = js.split("function turnsTable(", 1)[1].split("\nfunction ", 1)[0]
    assert "turnCompactions(turns)" in table
    # NOT pushed into `body` (the per-group fold list): this table is folded to prompts by
    # default, so a marker inside a collapsed group would be a marker nobody sees.
    marker = next(ln for ln in table.splitlines() if "compact-row" in ln)
    assert "rows.push(" in marker and "body.push" not in marker


def test_web_session_extras_context_gated_by_curve_support():
    # A backend whose turn rows are cumulative deltas (Codex) opts out of the
    # curve; the payload ships context: None so the page never draws a wrong one.
    class DeltaTurns(TurnsFakeStore):
        def supports_context_curve(self, workflow_id):
            return False

    w = workflow("w1", "2026-05-01 10:00:00", cost=0.5)
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(DeltaTurns([w]), args)
    assert ot.session_extras(app, "w1")["context"] is None


def test_web_render_html_defuses_embedded_script_tags():
    w = workflow("w1", "2026-05-01 10:00:00")
    w.title = "evil</script><script>alert(1)</script>"
    page = ot.render_html(ot.build_payload(app_with([w])))
    # Exactly the shell's two script blocks survive; the title's closing tags are
    # escaped inside the JSON blob so they can't break out of the data block.
    assert page.count("</script>") == 2
    assert "<\\/script>" in page
    assert 'id="opentab-data"' in page


def test_web_html_command_writes_the_report_file():
    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "report.html")
        args = type("Args", (), {"html": path})()
        assert ot.html_command(app, args) == 0
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    assert 'id="opentab-data"' in text
    assert "OpenTab — AI spend browser" in text
    # The page mirrors the TUI: sidebar host + keymap-driven detail pane + the
    # Trends overlay. Lock in the load-bearing hooks so a refactor can't silently
    # drop the TUI feel.
    assert 'id="side"' in text and 'id="tabbar"' in text
    assert 'id="trends"' in text  # the T Trends overlay host
    assert "TREND_TABS" in text and "Providers" in text  # the 7-tab Trends
    # The ranked tabs drill: a row opens its in-overlay sessions list, whose rows
    # deep-link into the session (mirrors the TUI's Trends drill).
    assert "trendDrillRows" in text and "Sessions · " in text
    # Every scope Overview carries the TUI's Top sessions section, and the day
    # Overview the full model mix (day has no Models tab).
    assert "topSessionsTable" in text and "'Top sessions'" in text and "'Model mix'" in text
    # Turns ▸ headers unfold/hover the whole prompt (serve-only data, baked JS).
    assert "promptFull" in text and "prompt-full" in text
    assert 'id="prices"' in text and 'id="rangepick"' in text  # P prices + R range overlays
    assert 'id="themepick"' in text and "const THEMES" in text  # the theme picker + palettes
    assert "catppuccin-mocha" in text and "tokyo-night" in text  # bundled themes
    assert "keydown" in text  # the j/k/Tab/h/l/Esc/$/T/P/R handler


def test_web_daily_trend_charts_only_active_days():
    page = ot.render_html(ot.build_payload(app_with([workflow("w1", "2026-07-01 10:00:00")])))
    # The Daily tab charts only up to the last day with spend, not the full calendar
    # month, so an in-progress month keeps bars as wide as Weekly/Monthly (each with its
    # own on-top label) instead of squeezing 31 slots and colliding the labels.
    assert "> 0) last = d" in page
    assert "for (let d = 1; d <= last; d++)" in page


def test_web_trend_rankings_are_sortable_by_column():
    page = ot.render_html(ot.build_payload(app_with([workflow("w1", "2026-07-01 10:00:00")])))
    # The TUI's ranked-tab sort, mirrored: one shared column pair for all four tables
    # (so a sort survives a tab flip), header clicks choose and flip it.
    assert "sort: 'cost', desc: true" in page
    assert "TREND_SORT_VAL" in page and "TREND_SORT_ASC" in page
    # The count column is Msgs on the model-derived tabs and Sess on the others -- one
    # key, read off whichever field the row carries (App._trend_sort_value's rule).
    assert "count: r => (r.runs != null ? r.runs : r.sessions)" in page
    assert "{ key: 'count', label: 'Msgs'" in page and "{ key: 'count', label: 'Sess'" in page
    # Rows arrive cost-ranked, so the stable sort keeps spend as the tiebreak.
    assert "function trendSorted(rows)" in page and "rows.slice().sort" in page
    # Only the ranking's own columns are clickable: the bar and Share are not (Share is
    # Cost as a percentage), so they must not offer a pointer or a sort.
    assert ".rank th.st{cursor:pointer}" in page and ".rank th.sorted" in page
    assert "h('th', { class: 'l' }, '')" in page and "h('th', null, 'Share')" in page


def test_web_meta_carries_the_baked_theme():
    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    app.args.theme = "gruvbox"  # --theme sets the browser's initial theme
    meta = ot.build_payload(app)["meta"]
    assert meta["theme"] == "gruvbox"
    # Absent (older args) falls back to the default, never crashes.
    del app.args.theme
    assert ot.build_payload(app)["meta"]["theme"] == ot.DEFAULT_THEME


def test_web_payload_reshapes_roles_to_css_vars():
    wp = ot.web_payload()
    assert set(wp) == set(ot.THEMES)  # one entry per theme
    entry = wp["catppuccin-mocha"]
    assert set(entry) == {"name", "dark", "css", "heat", "priceHeat"}
    # underscores become CSS-var hyphens, values preserved
    assert entry["css"]["bg-glow"] == ot.THEMES["catppuccin-mocha"]["roles"]["bg_glow"]
    assert "accent-bright" in entry["css"] and "accent_bright" not in entry["css"]


def test_web_payload_embeds_the_price_reference():
    # The P overlay's data: priced models you've used, with the eff $/M blend. The
    # FakeStore has no model_breakdown, so a store with model rows is needed -- reuse
    # NodesFakeStore, which returns a fable-5 node but no model_breakdown either;
    # so assert the structural shape (present, both row sets, mix optional).
    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    prices = ot.build_payload(app)["prices"]
    assert set(prices) >= {"byModel", "byRoute", "catalog"}
    assert isinstance(prices["byModel"], list) and isinstance(prices["byRoute"], list)
    # The models.dev catalog rides in every payload, so it travels slim: m/r/p per
    # row (u/s only when meaningful); eff and the ~ flag are recomputed client-side.
    assert len(prices["catalog"]) > 1000
    row = prices["catalog"][0]
    assert set(row) >= {"m", "r", "p"} and len(row["p"]) == 4
    assert "eff" not in row and "approx" not in row


def _js_source():
    # The page's script, read back from a rendered page: the JS can't be executed here,
    # so the invariants that live in it are asserted against its source.
    page = ot.render_html(ot.build_payload(app_with([workflow("w1", "2026-05-01 10:00:00")])))
    return page.rsplit("<script>", 1)[1].split("</script>", 1)[0]


def _js_whatif_cost(tok, rates):
    # The page's whatifCost(), transcribed: the client mirrors pricing.api_equivalent_cost
    # over a [input, output, reasoning, cacheRead, cacheWrite, cacheWrite1h] split, priced
    # with (input, output, cacheRead, cacheWrite, cacheWrite1h). Written out here so the
    # serialized numbers can be repriced exactly the way the page reprices them -- and so a
    # drift between the two formulas fails a test.
    #
    # The trailing token is the 1h-TTL SUBSET of cacheWrite, billed by REPLACEMENT (those
    # tokens leave the 5m bucket), never by addition -- adding would double-bill them.
    inp, out, reason, cr, cw = tok[:5]
    cw1h = tok[5] if len(tok) > 5 else 0
    ir, orr, crr, cwr = rates[:4]
    cwr1h = rates[4] if len(rates) > 4 else cwr
    long = min(max(cw1h, 0), cw)
    write = (cw - long) * cwr + long * cwr1h
    return (inp * ir + (out + reason) * orr + cr * crr + write) / 1e6


def _js_whatif_totals(payload, session_id, target):
    # The page's whatifTotals(), transcribed: both sides off the session's PER-MODEL rows,
    # both at list rates -- your models (each at its own) vs all of it at the target.
    rows = payload["models"].get(session_id) or []
    if not rows:
        return None
    rates = payload["whatif"]["rates"]
    # six slots: zip() would otherwise truncate the 1h subset off every row
    actual, tot = 0.0, [0, 0, 0, 0, 0, 0]
    for r in rows:
        actual += _js_whatif_cost(r["tok"], rates[r["model"]])
        tot = [a + b for a, b in zip(tot, r["tok"])]
    return actual, _js_whatif_cost(tot, rates[target])


def test_web_payload_ships_the_whatif_ingredients():
    # The `w` what-if can't travel precomputed (the target model is picked at view time),
    # so the payload ships the ingredients: the per-MODEL token splits the baseline needs,
    # the nodes' splits the tree's What-if column needs, and the list rates of every model
    # you've used. That is enough to price anything client-side.
    with tempfile.TemporaryDirectory() as tmp:
        payload = ot.build_payload(_whatif_db(tmp))
    root, kid = payload["nodes"]["root"]
    assert root["tok"] == [1_000_000, 0, 0, 0, 0, 0]  # [in,out,reasoning,cacheR,cacheW,cacheW1h]
    assert kid["tok"] == [2_000_000, 0, 0, 0, 0, 0]
    # The session's model rows carry the FULL split too -- without input/reasoning the
    # page could not compute the exact per-model baseline at all.
    by_model = {m["model"]: m["tok"] for m in payload["models"]["root"]}
    assert by_model["anthropic/claude-opus-4.5"] == [1_000_000, 0, 0, 0, 0, 0]
    assert by_model["anthropic/claude-haiku-4.5"] == [2_000_000, 0, 0, 0, 0, 0]
    models = payload["whatif"]["models"]
    # The picker's rows: models actually used, most-used first (the haiku subagent burned
    # 2M tokens, the opus root 1M), each with its four list rates in $/M.
    assert [m["model"] for m in models] == [
        "anthropic/claude-haiku-4.5",
        "anthropic/claude-opus-4.5",
    ]
    assert [m["tokens"] for m in models] == [2_000_000, 1_000_000]
    for m in models:
        # Five rates, not four: the catalog's (input, output, cacheRead, cacheWrite) plus
        # the DERIVED 1h-TTL write rate, which models.dev does not carry. whatifCost needs
        # it to bill the `tok` split's long-write subset at the long-TTL price.
        assert m["price"] == [round(float(v), 6) for v in ot.model_price(m["model"])] + [
            round(ot.cache_write_1h_price(m["model"]), 6)
        ]
        assert len(m["price"]) == 5 and m["price"][0] > 0
        assert m["price"][4] == round(2.0 * m["price"][0], 6)  # 1h write == 2.00x input
    # ...and a rate card for every model used, armable or not: the baseline prices each
    # model's tokens at its own rates, so a model you cannot arm still has to be counted.
    assert set(payload["whatif"]["rates"]) == set(by_model)


def test_web_payload_ships_the_whatif_catalog_tier():
    # The picker's Tab tier travels as the TUI's own whatif_catalog_candidates rows --
    # same names, same model_price rates, same cheapest-for-your-mix order -- because
    # deriving it client-side from prices.catalog would re-implement the dedupe and
    # could drift. Slim {m, p} rows: eff and its ~ flag are pure functions of p + the
    # shipped mix, recomputed client-side exactly like the P overlay's catalog rows.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        payload = ot.build_payload(app)
        catalog = payload["whatif"]["catalog"]
        expected = app.whatif_catalog_candidates()
    assert [c["m"] for c in catalog] == [name for name, _eff, _approx in expected]
    assert len(catalog) > 100  # the whole catalog, not just the two used models
    for c in catalog[:50]:
        assert c["p"] == [round(float(v), 6) for v in ot.model_price(c["m"])] + [
            round(ot.cache_write_1h_price(c["m"]), 6)
        ]  # the derived 1h-TTL write rate rides last, so arming a catalog row prices
        # long writes exactly the way arming a used model does
        assert set(c) == {"m", "p"}  # slim on purpose: ~1.5k rows ride in every payload
    # A catalog target the data never used still reprices client-side: whatifTotals
    # reads one rate map, into which the catalog rates pre-merge on load (mirrored
    # here), so _js_whatif_totals works with an unused target armed.
    rates = dict(payload["whatif"]["rates"])
    for c in catalog:
        rates.setdefault(c["m"], c["p"])
    unused = next(c["m"] for c in catalog if c["m"] not in payload["whatif"]["rates"])
    merged = dict(payload, whatif=dict(payload["whatif"], rates=rates))
    js_actual, js_whatif = _js_whatif_totals(merged, "root", unused)
    assert js_actual > 0 and js_whatif >= 0


def test_web_whatif_reprices_the_serialized_tokens_to_the_tui_figure():
    # The page's arithmetic, run over the page's own numbers: its per-model baseline and
    # its counterfactual must land on the exact figures the TUI's whatif_session_totals
    # quotes -- one formula, two frontends.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        payload = ot.build_payload(app)
        target = "anthropic/claude-opus-4.5"
        js_actual, js_whatif = _js_whatif_totals(payload, "root", target)

        app.select_whatif_model(target)
        wf = next(w for w in app.loaded if w.id == "root")
        tui_actual, tui_whatif = app.whatif_session_totals(wf)
        assert abs(js_actual - tui_actual) < 1e-9
        assert abs(js_whatif - tui_whatif) < 1e-9
        # The per-node What-if column is the node's own tokens at the target's rates, and
        # those sum to the session's counterfactual.
        rates = payload["whatif"]["rates"][target]
        nodes = [_js_whatif_cost(n["tok"], rates) for n in payload["nodes"]["root"]]
        assert abs(nodes[0] - ot.api_equivalent_cost(target, 1_000_000, 0, 0, 0, 0)) < 1e-9
        assert abs(nodes[1] - ot.api_equivalent_cost(target, 2_000_000, 0, 0, 0, 0)) < 1e-9
        assert abs(sum(nodes) - tui_whatif) < 1e-9


def test_web_whatif_baseline_is_the_exact_per_model_list_price():
    # CRITICAL: the baseline prices EVERY token at its OWN model's list rates, off the
    # per-model rows -- never a node's recorded/`api` cost. A node keeps a partially
    # metered session's few cents as its whole cost, and carries one dominant model label
    # for a session that may have switched models; either error inflates the "saving".
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp, costs=(1.5, 0.44))
        payload = ot.build_payload(app)
        target = "anthropic/claude-opus-4.5"
        js_actual, _js_whatif = _js_whatif_totals(payload, "root", target)
        nodes = payload["nodes"]["root"]
        assert sum(n["real"] for n in nodes) == 1.94  # what was recorded...
        assert abs(js_actual - 1.94) > 1  # ...and NOT what the comparison uses
        app.select_whatif_model(target)
        assert abs(js_actual - _whatif_baseline(app, "root")) < 1e-9
    js = _js_source()
    assert "const rows = DATA.models[id];" in js  # per-model rows, not DATA.nodes
    assert "wiBase" not in js  # the node-cost baseline is gone
    # The drift guard on the JS formula. Cache write is now split by TTL -- the 1h subset
    # by REPLACEMENT, so those tokens leave the 5m bucket instead of being billed twice.
    assert "const write = (cw - long) * cwr + long * (cwr1h || cwr);" in js
    assert "return (inp * ir + (out + reason) * orr + cr * crr + write) / 1e6;" in js
    assert "const long = Math.min(Math.max(cw1h || 0, 0), cw || 0);" in js
    # One shared reducer behind both panes (tree TOTAL + Overview summary), so they cannot
    # drift, and no per-node Δ column any more (a node's baseline isn't computable).
    assert js.count("function whatifTotals(") == 1
    assert "label: 'Δ'" not in js


def test_web_whatif_answers_for_a_solo_session_with_no_nodes():
    # A session that delegated nothing has no tree for the Subagents pane to table, so its
    # what-if lives on the Overview -- and it needs no node row at all: both panes reduce
    # over the per-model rows, which ship for every session with usage. So nodes go back to
    # riding along only for sessions that actually have a subagent tree.
    with tempfile.TemporaryDirectory() as tmp:
        # A solo (subscription) session on Opus, plus an unrelated Haiku one -- so Haiku
        # is an armable target even though this session never used it.
        sessions = [
            ("root", None, "Solo", "/tmp/p", 1760000000000, 0, 1_000_000),
            ("other", None, "Other", "/tmp/p", 1760000001000, 0, 1_000_000),
        ]
        messages = [
            _whatif_msg("root", "anthropic", "claude-opus-4.5", 0, 1_000_000),
            _whatif_msg("other", "anthropic", "claude-haiku-4.5", 0, 1_000_000),
        ]
        payload = ot.build_payload(_whatif_app(tmp, sessions, messages))
    assert payload["nodes"] == {}  # no tree, no nodes shipped -- for either session
    assert payload["models"]["root"][0]["tok"] == [1_000_000, 0, 0, 0, 0, 0]
    assert [m["model"] for m in payload["whatif"]["models"]] == [  # ...and both targets
        "anthropic/claude-haiku-4.5",
        "anthropic/claude-opus-4.5",
    ]
    actual, whatif = _js_whatif_totals(payload, "root", "anthropic/claude-haiku-4.5")
    assert actual > 0 and whatif > 0  # $0 recorded, but both sides priced at list rates
    assert whatif < actual  # haiku is cheaper than the opus that ran it


def test_web_page_matches_models_through_one_shared_rule():
    # One JS helper (modelMatches, the mirror of pricing.model_matches) behind BOTH model
    # filters: the P overlay's and the `w` picker's. Model ids match by word-anchored
    # fuzzy (the mirror of util.anchored_fuzzy_match -- a bare subsequence filled a
    # filter for "opus" with qwen3-cOder-PlUS junk), routes and vendor labels by plain
    # substring -- subsequencing the route is what made "gpt" walk "github-copilot" and
    # drag every Claude model sold through it into a GPT search.
    js = _js_source()
    assert js.count("function modelMatches(") == 1  # exactly one matcher, not two
    assert "rows = rows.filter(r => modelMatches(PRICES.q, r.model, r.routes, r.familyLabel))" in js
    assert "return modelMatches(WHATIF.q, bare, route ? [route] : [], '');" in js
    assert "fz(q, rt)" not in js  # the route-subsequence false-positive machine is gone
    assert "const subseq" not in js  # ...and so is the id one
    assert "anchoredFuzzy(qq, dashDots(model || ''))" in js  # ids: word-anchored fuzzy
    assert "fields.some(f => f.includes(qq))" in js  # routes/labels: substring


def test_web_whatif_target_is_transient_and_app_wide_costs_never_move():
    # The target is deliberately NOT persisted -- not to localStorage (unlike the theme and
    # the price pins), not to the hash: a remembered what-if would silently falsify every
    # later look. And it is session-scoped, so no app-wide figure moves while it's armed.
    js = _js_source()
    keys = set(re.findall(r"localStorage\.setItem\('([^']+)'", js))
    assert keys == {"opentab-theme", "opentab-pins"}  # nothing what-if shaped
    assert "let WHATIF = { model: null, open: false, q: '', i: 0, cat: false };" in js
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        before = sum(w.total_cost for w in app.loaded)
        app.select_whatif_model("anthropic/claude-opus-4.5")
        payload = ot.build_payload(app)
        assert sum(w.total_cost for w in app.loaded) == before
        # ...and the serialized rollups are the same numbers with a target armed.
        assert [w["real"] for w in payload["workflows"]] == [1.94]


def test_web_report_server_serves_page_extras_and_404():
    import threading
    import urllib.error
    import urllib.request

    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    server = ot.web.ReportServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert 'id="opentab-data"' in page
        assert '"serve":true' in page  # the served page knows the extras exist
        extras = json.loads(urllib.request.urlopen(base + "/api/session/w1").read().decode("utf-8"))
        # FakeStore: no turns/tools support, and no turns means no context curve
        assert extras == {"turns": [], "tools": [], "context": None, "expiries": []}
        try:
            urllib.request.urlopen(base + "/nope")
            raise AssertionError("expected a 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()
    thread.join(timeout=5)


def test_web_server_is_hardened_against_csrf_and_dns_rebinding():
    import threading
    import urllib.error
    import urllib.request

    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    server = ot.web.ReportServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        # Every response carries the lockdown headers (self-contained page: inline
        # JS/CSS, data: favicon, fetch back to this server only).
        resp = urllib.request.urlopen(base + "/")
        csp = resp.headers["Content-Security-Policy"]
        assert csp.startswith("default-src 'none'")
        assert "connect-src 'self'" in csp and "img-src data:" in csp
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        # Reload mutates state, so it is POST-only: a GET (fireable cross-origin
        # by any webpage) gets a 405 and does not touch the stores.
        try:
            urllib.request.urlopen(base + "/api/reload")
            raise AssertionError("expected a 405")
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
        req = urllib.request.Request(base + "/api/reload", data=b"", method="POST")
        assert json.loads(urllib.request.urlopen(req).read().decode("utf-8")) == {"ok": True}
        # DNS rebinding: a foreign Host header is rejected on a loopback bind...
        req = urllib.request.Request(base + "/", headers={"Host": "evil.example.com"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("expected a 403")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        # ...while every local spelling passes, with or without the port.
        for host in ("localhost", f"localhost:{port}", "127.0.0.1", f"[::1]:{port}"):
            req = urllib.request.Request(base + "/", headers={"Host": host})
            assert urllib.request.urlopen(req).status == 200
    finally:
        server.shutdown()
        server.server_close()
    thread.join(timeout=5)


def test_serve_command_runs_serve_forever_off_the_main_thread():
    # Regression: on Windows a Ctrl-C never wakes serve_forever's select(), so serving
    # in the foreground was unkillable from the keyboard. serve_command must run
    # serve_forever on a background thread (leaving the main thread free to catch the
    # interrupt) and always tear the server down via shutdown() + server_close().
    import threading
    import types

    class FakeServer:
        def __init__(self, address, app):
            self.server_address = (address[0], 8765)
            self.events = []
            self.serve_on_main = None

        def page(self):
            self.events.append("page")

        def serve_forever(self):
            self.serve_on_main = threading.current_thread() is threading.main_thread()
            self.events.append("serve")  # returns at once -> the join loop then exits

        def shutdown(self):
            self.events.append("shutdown")

        def server_close(self):
            self.events.append("close")

    made = {}
    real = ot.web.ReportServer
    ot.web.ReportServer = lambda address, app: made.setdefault("s", FakeServer(address, app))
    try:
        args = types.SimpleNamespace(bind="127.0.0.1", port=0, web=False)
        rc = ot.web.serve_command(app_with([workflow("w1", "2026-05-01 10:00:00")]), args)
    finally:
        ot.web.ReportServer = real
    server = made["s"]
    assert rc == 0
    assert server.serve_on_main is False  # never the foreground / main thread
    assert "serve" in server.events
    assert server.events[-2:] == ["shutdown", "close"]  # always torn down


def test_web_open_report_opens_a_browser_and_survives_a_headless_box():
    # --web pops the browser open cross-platform via stdlib webbrowser; a box with no
    # browser must return False, never raise, so serving keeps running.
    import webbrowser

    calls = []
    real_open = webbrowser.open

    def fake_open(url, new=0, autoraise=True):
        calls.append((url, new))
        return True

    def boom(*a, **k):
        raise webbrowser.Error("no browser found")

    webbrowser.open = fake_open
    try:
        assert ot.web.open_report("http://localhost:8321/") is True
        assert calls == [("http://localhost:8321/", 2)]  # new=2 -> a new tab
        webbrowser.open = boom
        assert ot.web.open_report("http://localhost:8321/") is False
    finally:
        webbrowser.open = real_open


def test_web_payload_carries_machine_and_the_fleet_flag():
    w1 = workflow("a", "2026-05-01 10:00:00", cost=1.0)
    w1.machine = "laptop"
    w2 = workflow("b", "2026-05-02 10:00:00", cost=2.0)
    w2.machine = "server"
    payload = ot.build_payload(app_with([w1, w2]))
    assert payload["meta"]["machines"] is True
    assert {row["machine"] for row in payload["workflows"]} == {"laptop", "server"}
    # A non-fleet view sets the flag off, so the page grows no Machine column/tab.
    plain = ot.build_payload(app_with([workflow("a", "2026-05-01 10:00:00")]))
    assert plain["meta"]["machines"] is False


def test_web_payload_carries_machine_meta_for_the_machines_mode():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00")],
            "server": [workflow("b", "2026-05-02 10:00:00")],
        }
    )
    mm = ot.build_payload(app)["machineMeta"]
    assert mm["laptop"]["live"] is True and mm["laptop"]["refreshable"] is False
    assert mm["server"]["live"] is False
    assert mm["server"]["refreshable"] is True  # a pulled box with a remotes key
    assert mm["server"]["exportedAt"].startswith("2026") and mm["server"]["version"] == "1.6.0"
    # Off the fleet view the Machines mode still renders this one box, so it needs its
    # entry -- without one the page would draw the box you're on as a pulled summary.
    solo = app_with([workflow("a", "2026-05-01 10:00:00")])
    payload = ot.build_payload(solo)
    assert payload["meta"]["machines"] is False  # ...but no fleet: no Machine column/tab
    assert payload["machineMeta"] == {
        solo.local_machine_name: {
            "live": True,
            "exportedAt": "",
            "version": "",
            "refreshable": False,
        }
    }
    # Every workflow row carries a real machine label, so the page groups them into it.
    assert {w["machine"] for w in payload["workflows"]} == {solo.local_machine_name}
    # Regression (Codex): "not a fleet" is not "no machine metadata". A remote source
    # with ONE pulled box also has machines_present False, but it does carry that box's
    # pull timestamp/version -- which must not be replaced by a local-live entry (the
    # page would then lose the ↻ re-pull button and the "pulled 2h ago" line).
    one_pulled = fleet_app({"server": [workflow("b", "2026-05-02 10:00:00")]}, meta=None)
    one_pulled.store.machine_meta = {
        "server": {
            "live": False,
            "exported_at": "2026-07-18T09:00:00+00:00",
            "opentab_version": "1.6.0",
            "key": "server",
        }
    }
    mm = ot.build_payload(one_pulled)["machineMeta"]
    assert one_pulled.machines_present is False
    assert list(mm) == ["server"] and mm["server"]["refreshable"] is True
    assert mm["server"]["version"] == "1.6.0"


def test_web_page_has_the_per_scope_machines_tab_machinery():
    from tests._support import fleet_app

    app = fleet_app(
        {
            "laptop": [workflow("a", "2026-05-01 10:00:00")],
            "server": [workflow("b", "2026-05-02 10:00:00")],
        }
    )
    html = ot.webpage.render_html(ot.build_payload(app))
    # the read-only per-scope Machines breakdown table + its tab dispatch, gated off the
    # 'M' machine scope (which is already one box)
    assert "function machinesTable(" in html
    assert "TAB === 'Machines'" in html
    assert "sc.kind !== 'M'" in html
    # a non-fleet page grows no per-scope Machines tab (machines flag off)
    plain = ot.webpage.render_html(
        ot.build_payload(app_with([workflow("a", "2026-05-01 10:00:00")]))
    )
    assert '"machines":false' in plain


def test_web_refresh_endpoint_repulls_the_named_machine():
    import threading
    import urllib.request

    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    captured = {}

    def fake_refresh(name=None):
        captured["name"] = name
        return [(name or "all", 4, "")]

    app.refresh_machines_now = fake_refresh
    server = ot.web.ReportServer(("127.0.0.1", 0), app)
    server.page()  # prime the page cache so we can prove the refresh invalidates it
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        req = urllib.request.Request(
            base + "/api/refresh",
            data=json.dumps({"machine": "server"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        body = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
        assert body["ok"] is True and body["results"] == [["server", 4, ""]]
        assert captured["name"] == "server"
        assert server._page is None  # the next GET rebuilds off the freshly pulled data
    finally:
        server.shutdown()
        server.server_close()
    thread.join(timeout=5)


def test_web_refresh_endpoint_ignores_malformed_and_unnamed_requests():
    # A malformed/empty/non-string machine must be a no-op -- never "refresh every box"
    # (an ssh storm) and never crash the handler on an unhashable value.
    import threading
    import urllib.request

    app = app_with([workflow("w1", "2026-05-01 10:00:00")])
    calls = []
    app.refresh_machines_now = lambda name=None: calls.append(name) or [(name or "ALL", 1, "")]
    server = ot.web.ReportServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(raw):
        req = urllib.request.Request(base + "/api/refresh", data=raw, method="POST")
        return json.loads(urllib.request.urlopen(req).read().decode("utf-8"))

    try:
        assert post(b"{}")["results"] == []  # no machine named -> no-op, not refresh-all
        assert post(b"{ not json")["results"] == []  # malformed body -> no-op
        assert post(json.dumps({"machine": {"x": 1}}).encode())["results"] == []  # dict -> no crash
        assert post(json.dumps({"machine": ""}).encode())["results"] == []  # blank name
        assert calls == []  # none of those reached the backend
    finally:
        server.shutdown()
        server.server_close()
    thread.join(timeout=5)


def test_web_context_points_carry_their_own_window():
    """The page derives peak/final itself, and a session can switch models mid-way, so the
    peak % must be measured against the window the PEAK TURN ran in -- the TUI's split
    (renderer.detail_context). Shipping only the live model's window made the page print an
    impossible 120% of the window for a session that peaked on a big model and ended on a
    smaller one; shipping one precomputed peakWindow could still disagree with whichever
    turn the client picked as the peak, so every point carries its own."""
    base = {
        "depth": 0,
        "agent": "-",
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost": 0.0,
        "output": 0,
        "prompt_id": "p1",
        "prompt_title": "t",
        "prompt_full": "t",
    }

    class MixedWindowTurns(TurnsFakeStore):
        def message_timeline(self, workflow_id):
            return [
                # peaks on a 400k-window model, then ends on a 200k one
                dict(base, time="2026-05-01 10:00:00", model_name="openai/gpt-5.2", input=239_957),
                dict(
                    base,
                    time="2026-05-01 10:05:00",
                    model_name="anthropic/claude-opus-4-5",
                    input=50_000,
                ),
            ]

    w = workflow("w1", "2026-05-01 10:00:00", cost=0.5)
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(MixedWindowTurns([w]), args)
    ctx = ot.session_extras(app, "w1")["context"]
    windows = [p["w"] for p in ctx["points"]]
    assert len(windows) == 2 and windows[0] != windows[1]  # each turn's own window
    assert ctx["mixedWindows"]
    # The peak turn's window is the bigger one, so the peak reads under 100% -- the
    # figure the TUI shows -- not the 120% the live model's window would have given.
    peak_i = max(range(len(ctx["points"])), key=lambda i: ctx["points"][i]["v"])
    assert 100 * ctx["points"][peak_i]["v"] / windows[peak_i] < 100


def _js_token_economics(payload, session_ids):
    # The page's tokenEconomics(), transcribed: the same per-model rows and the same
    # per-type arithmetic the TUI's App.token_economics runs, so a drift between the two
    # implementations fails a test instead of quietly showing two different bills.
    rates = payload["whatif"]["rates"]
    local = set(payload["whatif"].get("local") or [])
    tokens, cost, local_tokens = [0.0] * 5, [0.0] * 5, 0
    for sid in session_ids:
        for r in payload["models"].get(sid) or []:
            if r["model"] in local:
                local_tokens += sum(r["tok"][:5])
                continue
            ir, orr, crr, cwr = rates[r["model"]][:4]
            rate_row = rates[r["model"]]
            for i, v in enumerate(r["tok"][:5]):
                tokens[i] += v
            cost[0] += r["tok"][0] * ir / 1e6
            cost[1] += r["tok"][1] * orr / 1e6
            cost[2] += r["tok"][2] * orr / 1e6
            cost[3] += r["tok"][3] * crr / 1e6
            long1h = min(max(r["tok"][5] if len(r["tok"]) > 5 else 0, 0), r["tok"][4])
            cwr1h = rate_row[4] if len(rate_row) > 4 else cwr
            cost[4] += ((r["tok"][4] - long1h) * cwr + long1h * cwr1h) / 1e6
    return tokens, cost, local_tokens


def test_web_token_economics_matches_the_tui_split_exactly():
    # One formula, two frontends -- the same rule the what-if follows. The page computes
    # the split client-side (only it knows the drilled/filtered scope), so the payload has
    # to carry everything that needs: the per-model token splits, every used model's
    # rates, and which of those models are local.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)
        payload = ot.build_payload(app)
        tokens, cost, local_tokens = _js_token_economics(payload, [w.id for w in app.loaded])
        econ = app.token_economics(app.loaded)
        assert [round(v, 6) for v in tokens] == [round(v, 6) for v in econ.tokens]
        assert [round(v, 9) for v in cost] == [round(v, 9) for v in econ.cost]
        assert local_tokens == econ.local_tokens
    js = _js_source()
    # The pieces must stay pieces: summing them here would make the pane's TOTAL a second
    # implementation of the cost rather than a decomposition of the one on screen.
    assert "function tokenEconomics(" in js
    assert "cost[2] += tok[2] * orr / 1e6" in js  # reasoning bills at the output rate
    # A missing cache rate is flagged, never read as free reads (the TUI's rule).
    assert "if (crr <= 0 && tok[3] > 0 && ir > 0) missingCache = true;" in js
    assert "WI_LOCAL.has(r.model)" in js  # local models leave both rows


def test_web_token_economics_pane_is_two_stacked_bars_over_a_fixed_order_table():
    js = _js_source()
    # Two 100% bars over the SAME five types, one above the other -- the reading is the
    # gap between them, which needs a shared scale and one colour per type.
    assert "share of tokens sent" in js and "share of dollars billed" in js
    # The detail table must NOT go through the sortable table(): its headers install
    # click handlers unconditionally, so a click would re-rank the five rows away from
    # cost order and keep that ranking for every later scope (one shared table id).
    pane = js.split("function tokenEconomicsPane(", 1)[1].split("\nfunction ", 1)[0]
    assert "table('t-" not in pane and "SORT[" not in pane
    assert "b.cost - a.cost" in pane  # the fixed order is by cost
    # Five slots per mode, and the ORDER is the colour-vision-deficiency safety
    # mechanism (adjacent pairs were validated in that order) -- not decoration.
    for ramp in ("TOK_SERIES_DARK", "TOK_SERIES_LIGHT"):
        row = next(ln for ln in js.splitlines() if ln.startswith("const " + ramp))
        assert row.count("#") == 5, row
    # A label sitting ON a fill picks its ink from that fill, not from the theme: one
    # theme-wide choice is unreadable on the ramp's own lighter slots.
    assert "function inkOn(" in js
    assert "1.05 / (L + 0.05)" in js  # WCAG contrast against white, not 21/(L+0.05)


def test_web_models_tab_drills_in_place_in_every_scope():
    # The TUI's Models drill, mirrored: a model row arms the in-place sub-drill in EVERY
    # scope that has a Models tab, not just the Machines box -- there is no model scope to
    # navigate to instead. Harnesses/Projects stay box-gated (they DO have their own
    # scope), and the clearable chip is no longer box-gated either, or an armed model
    # drill outside a box would have no way back out.
    js = _js_source()
    body = js.split("function renderDetail(", 1)[1].split("\nfunction ", 1)[0]
    models = body.index("modelsTable('t-tab-models'")
    assert "box ?" not in body[models : body.index("TAB === 'Projects'")]
    assert "setMsub('model', r.model)" in body
    # the two that keep the gate, and the sessions list that reflects any armed drill
    assert "box ? (r => setMsub('project', r.project))" in body
    assert "box ? (r => setMsub('source', r.source))" in body
    assert "sessionsTable('t-tab-sessions', msubFilter(ws))" in body
    assert "if (MSUB) {" in js and "sc.kind === 'M' && MSUB" not in js


def test_web_overview_closes_with_the_models_table():
    # The TUI's rule (renderer._model_table), mirrored: the models table is the widest
    # block and the least likely answer to "where did the money go", so every Overview
    # ends with it and the blocks that read in a glance -- Token economics, Top projects,
    # Top sessions -- come first.
    js = _js_source()
    body = js.split("function renderOverview(", 1)[1].split("\nfunction ", 1)[0]
    econ = body.index("tokenEconomicsPane(")
    projects = body.index("'Top projects'")
    sessions = body.index("'Top sessions'")
    models = body.index("'Top models'")
    assert econ < projects < sessions < models
    # And it is the LAST thing appended -- a new pane goes above it, never below.
    assert "modelsTable('t-ov-models'" in body[body.rindex("root.appendChild(") :]


def test_web_tools_treemap_is_passive_themed_and_precedes_the_table():
    js = _js_source()
    tools = js.split("function toolsTable(", 1)[1].split("\nfunction binaryTreemap", 1)[0]
    tree = js.split("function toolTreemap(", 1)[1].split("\n}", 1)[0]
    assert tools.index("toolTreemap(rows)") < tools.index("class: 'tool-table'")
    assert "onclick" not in tools and "onclick" not in tree  # passive: no hidden interaction mode
    assert "TH.heat[level(r)]" in tree and "inkOn(fill)" in tree
    assert "dollars ? mCost(r) : r.tokens" in tree  # $0 subscription fallback
    assert "Math.min(8, all.length)" in tree and "tool: 'Other'" in tree
    assert "out.sort(sortItems)" in tree  # Other can become the new largest tile
    assert "getBoundingClientRect()" in tree  # partition the real wide/mobile canvas
    assert "r.w >= 66 && r.h >= 30" in tree  # labels are pixel-gated, not percentage-gated
    # Shade is the PER-CALL rate, not the area's own measure -- area already says what a
    # tool cost in total, so colouring by the same number spends the second channel
    # saying it twice. It needs a count on every drawn tile to be a scale at all, and
    # falls back to the area's measure when the payload has none.
    assert "new Set(all.map(r => r.value / r.calls)).size > 1" in tree
    assert (
        "if (!byRate) return Math.max(0, Math.min(n, Math.round(Math.sqrt(r.value / peak) * n)));"
        in tree
    )
    # Log position over the rate range, like Renderer._heat_position: per-call rates span
    # orders of magnitude and a linear ramp flattens every tool but the priciest.
    assert "Math.log(v) - Math.log(rLo)" in tree
    # money() floors at the cent; a per-call rate usually lives below one, so telling
    # $0.0004 from $0.006 needs the extra decimals "<$0.01" would erase for both.
    assert "'<$0.0001/call'" in tree and ".toFixed(4).replace(/0+$/, '')" in tree
    # The folded tail carries its calls too, or "Other" would have no rate at all.
    assert "calls: sum(rest, r => r.calls)" in tree
    # The tail keeps folding until every drawn tile can hold its own name -- measured
    # against the REAL container, since a percentage cannot know whether a tile is 40px
    # or 240px wide. A row of anonymous stripes is what made this chart read as empty.
    assert "const TILE_MIN = 70;" in tree
    assert "all[keep].value / total * box.width >= TILE_MIN) keep++;" in tree
    assert "const items = fold(Math.max(1, keep));" in tree
    # The scale is a property of the DATA, not of how many tiles fit: `all`, not the
    # folded set, so a tool keeps its colour when a resize folds a neighbour away.
    assert "const byRate = all.every(r => r.calls > 0)" in tree
    # The finding as a sentence, read off the FULL ranking so it can still name the tool
    # the fold swallowed -- which is exactly the pricey-per-call one, small by total and
    # therefore first to fold. (The flamegraph's headline, same reason.)
    assert "const top = all[0], ofWhat = dollars ? 'the spend' : 'the tokens';" in tree
    assert "'priciest per call is ' + hot.tool + ' at ' + rateText(hot)" in tree
    assert "class: 'flame-head' }, line.join(' · ')" in tree
    # The exact table below has to be able to state the figure the shade encodes.
    assert "{ key: 'calls', label: 'Calls', align: 'r' }," in tools
    assert "calls: sum(rows, r => r.calls)" in tools
    assert "'aria-hidden': 'true'" in tree  # exact accessible table follows immediately
    assert "new ResizeObserver(" in tree  # reflow only the chart, never global page state
    assert "render(false)" not in tree
    assert "function binaryTreemap(" in js
    page = ot.render_html(ot.build_payload(app_with([workflow("w1", "2026-05-01 10:00:00")])))
    assert ".tool-map{position:relative" in page
    assert ".tool-tile{position:absolute" in page
    # Shorter than it was: eight tiles restating one column did not earn 360px.
    assert "height:clamp(150px,18vw,220px)" in page
    assert ".tool-tile .tr{" in page  # the rate rides its own line, gated on its own


def test_web_flamegraph_divides_the_same_node_costs_as_the_tui():
    # One chart, two frontends. The target is not the issue here (the flamegraph has
    # none) -- the SCOPE is: widths follow the live $ toggle, so the page computes them
    # client-side and the payload has to carry each node's two costs, its depth, and the
    # fields the labels need. Restating sessionFlame() over the payload has to land on
    # App.session_flame's segments exactly.
    with tempfile.TemporaryDirectory() as tmp:
        app = _whatif_db(tmp)  # a subscription session: root + one Docs subagent, $0
        app.show_api_prices = True
        payload = ot.build_payload(app)
        nodes = payload["nodes"]["root"]
        # Every field sessionFlame() reads travels.
        for n in nodes:
            assert {"depth", "agent", "title", "date", "real", "api", "tokens"} <= set(n)
        total = sum(n["api"] for n in nodes)
        own = sum(n["api"] for n in nodes if not n["depth"])
        flame = app.session_flame(app.loaded[0])
        assert flame.unit == "cost"
        assert round(flame.total, 6) == round(total, 6)
        assert round(flame.self_share, 6) == round(own / total, 6)
        assert [round(s.value, 6) for s in flame.segments] == [
            round(own, 6),
            *(round(n["api"], 6) for n in nodes if n["depth"]),
        ]
    # With "$" off a subscription session has no dollars at all, and both sides fall back
    # to tokens rather than drawing a hierarchy of zeros.
    with tempfile.TemporaryDirectory() as tmp:
        sub = _whatif_db(tmp, costs=(0, 0))
        assert not sub.show_api_prices
        assert sum(n["real"] for n in ot.build_payload(sub)["nodes"]["root"]) == 0
        assert sub.session_flame(sub.loaded[0]).unit == "tokens"

    js = _js_source()
    # The client mirror, and the two rules that keep it honest: widths are the node's
    # $-gated Cost (mCost, i.e. _priced_nodes), and the fallback is tokens.
    assert "function sessionFlame(" in js and "function flamePane(" in js
    assert "const val = n => unit === 'cost' ? mCost(n) : n.tokens;" in js
    # The root's slot is reserved -- a child cycling onto it would erase the one
    # distinction the chart makes.
    assert "const FLAME_SELF_SLOT = 0, FLAME_CHILD_SLOTS = [1, 2, 3, 4];" in js
    assert "FLAME_CHILD_SLOTS[i % FLAME_CHILD_SLOTS.length]" in js
    # Two places the mirror can silently drift, both caught once already: the tie-break
    # must run DESCENDING and by CODE POINT, like the Python's reverse=True tuple sort.
    # localeCompare is not that -- it disagrees with Python on case and accents ("Z" vs
    # "a"), and a tie ordered differently gives the same two segments different colours
    # in the two frontends...
    sort = next(ln for ln in js.splitlines() if ".sort((a, b) => val(b) - val(a)" in ln)
    assert sort.endswith("|| byTitle(a, b));") and "localeCompare" not in sort
    # ...and code POINTS, not code units: a bare `<` compares UTF-16 units, which ranks
    # an astral character below a high BMP one where Python's code-point sort does not.
    assert "y[i].codePointAt(0) - x[i].codePointAt(0)" in js
    # The repeat counter is a Map, not a plain object: a subagent titled exactly
    # "constructor"/"toString"/"__proto__" would read Object.prototype instead of a
    # count, so `> 1` is false and two identically-named executions never get
    # disambiguated -- a hole Python's list.count() does not have, i.e. a divergence
    # only one of the two frontends can fall into.
    assert "const base = rows.map(flameLabel), n = new Map();" in js
    assert "const many = l => n.get(l) > 1;" in js
    # ...and "~" is gated on the UNIT (token widths were never priced) and on the node
    # actually being DRAWN (an aborted $0/0-token child must not mark a chart whose every
    # width was recorded), exactly like App.session_flame.
    assert "est: unit === 'cost' && MODE === 'api' && nodes.some(n => !n.real && val(n) > 0)" in js


def test_web_flamegraph_sits_above_the_tree_and_ignores_an_armed_target():
    js = _js_source()
    pane = js.split("else if (TAB === 'Subagents') {", 1)[1].split("} else if (TAB ===", 1)[0]
    # It is appended BEFORE either tree variant, and outside the what-if branch: the
    # chart is recorded/estimated spend, the tree's what-if is a counterfactual, and
    # restating one as the other is the mistake this ordering prevents.
    assert pane.index("flamePane(nodes)") < pane.index("whatifTree(nodes, wi)")
    assert "wi ? flamePane" not in pane and "flamePane(nodes, wi" not in pane
    # A solo session has no tree, so no chart either -- the pane already says so, and a
    # one-segment icicle would imply a hierarchy that isn't there.
    assert "if (tree) { const fl = flamePane(nodes); if (fl) root.appendChild(fl); }" in pane
    # The names sit UNDER the band, in rows that share the band's own flex ratios, so a
    # name is under its slice by construction rather than by arithmetic -- and only the
    # share still rides inside the fill.
    assert "const labelRow = textOf => h('div', { class: 'names' }" in js
    assert "s.share > NAMED ? fPct(s.share) : null" in js
    page = ot.render_html(ot.build_payload(app_with([workflow("w1", "2026-05-01 10:00:00")])))
    assert ".flame .names{display:flex;gap:1px" in page
    assert ".flame .names > div{min-width:0;overflow:hidden" in page
    # A second positioned row for the models, and only when the segments disagree about
    # them -- a single-model tree says it once in the caption instead.
    assert "f.oneModel ? null : labelRow(s => s.model)" in js
    assert "(f.oneModel ? ' · all on ' + f.oneModel : '')" in js
    # The agent is mined out of OpenCode's "(@name)" title when its own column is empty,
    # and the title is never used as a label -- it is a sentence, and it is one column
    # away in the table below.
    assert "const FLAME_AGENT_TAG = /\\(@([\\w.-]+)/;" in js
    label = js.split("function flameLabel(", 1)[1].split("\nfunction ", 1)[0]
    assert "n.title" in label and "tag ? tag[1] : 'subagent'" in label
    # A label on a fill picks its ink from that fill (chart 1's rule), not from the theme.
    flame = js.split("function flamePane(", 1)[1].split("\nfunction ", 1)[0]
    assert "inkOn(SER[s.slot])" in flame
    # fPct guards BOTH ends like Renderer._flame_pct: an icicle prints the parts beside
    # the whole, so a near-total must not read a flat "100%" above the segments standing
    # next to it, and a sub-half-percent segment that is visibly there must not read 0%.
    pct = js.split("function fPct(", 1)[1].split("\nfunction ", 1)[0]
    assert "if (share >= 99.5) return '>99%';" in pct
    assert "if (share < 0.5) return '<1%';" in pct
    # The label ladder ends on a guaranteed-unique pass, not merely on the cost rank --
    # a node genuinely titled "foo #1" collides with the rank handed to a repeated "foo".
    assert "while (seen.has(name)) name += ' ·';" in js


def test_web_payload_names_the_local_models_so_the_page_can_exclude_them():
    # Without this list the page cannot apply App.token_economics' rule and would price
    # local tokens at the generic fallback -- inventing spend that no one was billed.
    app = app_with([workflow("w1", "2026-05-01 10:00:00", cost=0.0)])
    app._model_by_root = {
        "w1": [
            {
                "model_name": "ollama/llama3.1",
                "runs": 1,
                "cost": 0.0,
                "tokens_total": 1_000,
                "input": 1_000,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
            },
            {
                "model_name": "anthropic/claude-opus-4.5",
                "runs": 1,
                "cost": 1.0,
                "tokens_total": 1_000,
                "input": 1_000,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
            },
        ]
    }
    app._models_loaded = True  # the rows above ARE the breakdown; don't rescan the store
    app._compute_api_costs()
    payload = ot.build_payload(app)
    assert "ollama/llama3.1" in payload["whatif"]["local"]
    assert "anthropic/claude-opus-4.5" not in payload["whatif"]["local"]
    # It is a subset of `rates` (which covers every used model), so the page can look a
    # model up in both without a missing-key branch.
    assert set(payload["whatif"]["local"]) <= set(payload["whatif"]["rates"])


def test_web_names_stay_reachable_from_a_cold_package_import():
    # opentab/__init__.py re-exports every module eagerly EXCEPT this one: reaching
    # opentab.web pulls http.server (~13ms) that no TUI start and no `opentab status`
    # poll should pay, so the five web names and the two modules resolve on demand
    # (PEP 562). This test lives here because the deferred set is exactly the web
    # frontend's surface -- if the registry ever generalizes, it moves.
    #
    # The trap it guards: `from opentab.web import build_payload` used to bind
    # `opentab.web` as a side effect, so ot.web.ReportServer worked. With a lazy
    # registry that only knows FUNCTION names, ot.web resolves only once something
    # else happened to trigger an import -- passing or failing depending on what ran
    # first in the process. So the modules are registered too, and a subprocess with
    # nothing else imported is the only honest way to assert it.
    import subprocess
    import sys

    probe = (
        "import opentab, sys;"
        "assert 'opentab.web' not in sys.modules, 'web imported eagerly';"
        "assert opentab.web.ReportServer, 'ot.web unreachable from a cold import';"
        "assert opentab.webpage.render_html, 'ot.webpage unreachable';"
        "assert callable(opentab.build_payload) and callable(opentab.render_html);"
        "assert 'build_payload' in dir(opentab) and 'web' in dir(opentab);"
        # star-import must still see the deferred names, which it cannot do off the
        # module dict alone -- hence the explicit __all__
        "assert {'build_payload', 'render_html', 'web', 'webpage'} <= set(opentab.__all__);"
        "print('ok')"
    )
    # src/ on the path the way tests/__init__.py puts it there, so this runs with no
    # install like the rest of the suite; the rest of the env is inherited so the
    # child sees the same temp XDG roots.
    src = os.path.dirname(os.path.dirname(os.path.abspath(ot.__file__)))
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": src},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


class ExpiryFakeStore(FakeStore):
    # Two turns holding 300k of cached context, the second arriving two hours later --
    # by which time the 1h-TTL entry it would have read back was gone.
    def supports_turns(self, wid):
        return True

    def message_timeline(self, wid):
        base = {
            "depth": 0,
            "agent": "-",
            "model_name": "anthropic/claude-opus-4-8",
            "cost": 0.0,
            "input": 10,
            "output": 100,
            "reasoning": 0,
            "tokens_total": 300110,
        }
        return [
            dict(
                base,
                time="2026-06-10 10:00:00",
                cache_read=200000,
                cache_write=100000,
                cache_write_1h=100000,
                prompt_id="a",
                prompt_title="first",
                prompt_full="first",
            ),
            dict(
                base,
                time="2026-06-10 12:00:00",
                cache_read=0,
                cache_write=300000,
                cache_write_1h=300000,
                prompt_id="b",
                prompt_title="the late one",
                prompt_full="the late one",
            ),
        ]


def test_web_ships_cache_expiries_precomputed_rather_than_mirroring_the_rule():
    # Unlike the what-if (armed at view time) and the compaction markers (three lines off
    # the `ctx` each turn already carries), this needs list rates and the TTL rules and
    # moves with nothing on the page -- so it is computed once, in Python, and the page
    # only draws it. A JS twin here could only drift away from the TUI.
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(ExpiryFakeStore([workflow("w1", "2026-06-10 10:00:00", cost=0.0)]), args)
    (exp,) = ot.session_extras(app, "w1")["expiries"]
    assert exp["i"] == 1 and exp["idle"] == 7200 and exp["ttl"] == ot.CACHE_TTL_LONG
    assert exp["repaid"] == 300000
    # The same figure the TUI's ❄ line quotes, from the same helper.
    (miss,) = (m for m in ot.cache_misses(app.session_turn_rows("w1")) if m.cause == "waited")
    assert exp["cost"] == round(miss.cost, 6) > 0

    js = _js_source()
    # Drawn above its ▸ prompt header and OUTSIDE the collapsible group, like the ▼
    # compaction row: this table folds to prompts by default.
    assert "class: 'expiry-row'" in js and "'❄ cache expired — '" in js
    # The seam between the two halves above, which nothing else covers: EXTRAS is rebuilt
    # field by field on every drill-in, so a field the fetch handler forgets is served by
    # the API and silently never drawn. Caught exactly once, in a browser, with the API
    # returning three expiries and the page rendering none.
    assert "expiries: x.expiries || []" in js
    # Waste reads red, where the ▼ compaction row is amber (the TUI makes the same split).
    assert "tr.expiry-row td{color:var(--bad)" in ot.webpage._CSS


def test_web_expiries_stay_empty_when_the_backend_cannot_support_the_reading():
    # The Context-curve opt-in gates it, exactly as it gates the TUI's marker and the
    # compaction rows -- a cumulative-delta backend cannot have its cache split read as
    # one request's prompt, and the two frontends must not disagree about that.
    args = type("Args", (), {"since": None, "until": None, "days": None})()

    class NoCurve(ExpiryFakeStore):
        def supports_context_curve(self, wid):
            return False

    app = ot.App(NoCurve([workflow("w1", "2026-06-10 10:00:00", cost=0.0)]), args)
    assert ot.session_extras(app, "w1")["expiries"] == []


def test_web_turn_costs_bill_long_ttl_writes_so_an_expiry_fits_inside_its_turn():
    # A cache expiry's cost is not a separate charge -- it is the part of the FOLLOWING
    # turn's cost that went on re-buying context, so it can never exceed that turn. The
    # page broke the invariant by pricing turns without the 1h-TTL cache-write subset
    # (2.00x input, against the 5m tier's 1.25x) while the marker priced the re-buy with
    # it: measured on a real session, a $4.81 expiry sat inside a turn the page called
    # $3.27. The TUI's detail_turns/detail_tools always passed it; the page now does too.
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(ExpiryFakeStore([workflow("w1", "2026-06-10 10:00:00", cost=0.0)]), args)
    extras = ot.session_extras(app, "w1")
    (exp,) = extras["expiries"]
    turn = extras["turns"][exp["i"]]
    assert exp["cost"] <= turn["api"]  # the miss is a slice of the turn, never more

    # It is the long-TTL rate specifically: the same tokens at the 5m rate would price
    # the turn BELOW its own expiry, which is the bug this guards.
    row = app.session_turn_rows("w1")[exp["i"]]
    short = ot.api_equivalent_cost(
        row["model_name"],
        row["input"],
        row["output"],
        row["reasoning"],
        row["cache_read"],
        row["cache_write"],
    )
    assert short < exp["cost"] <= turn["api"]


def test_web_turns_carry_the_cached_share_and_keep_the_table_rectangular():
    # The one number that answers "did this turn re-buy its context", shipped rather
    # than derived: the page has each turn's total tokens but not its cache split.
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(ExpiryFakeStore([workflow("w1", "2026-06-10 10:00:00", cost=0.0)]), args)
    warm, cold = ot.session_extras(app, "w1")["turns"]
    # 200k read against a 300k context -- it read most of its context back while still
    # writing the 100k it had just added.
    assert 0.6 < warm["cached"] < 0.7
    assert cold["cached"] == 0.0  # read none of it: bought the lot again

    # Adding a column to a table whose marker rows span it: every full-width row has to
    # widen with it, or the ▸/▼/❄ rows tear the layout in exactly the states (folded,
    # compacted, expired) that are hardest to notice.
    js = _js_source()
    assert "h('th', { class: 'r' }, 'Cached')" in js
    assert "colspan: 6" not in js  # the turn table's full-width rows all widened
    assert js.count("colspan: 7") >= 3  # prompt-full, compaction, expiry


def test_web_cached_share_is_gated_like_every_other_per_request_reading():
    # A cumulative-delta backend cannot have one row read as one request's prompt, so it
    # gets no share at all rather than a plausible wrong one -- the same opt-in behind
    # the compaction markers, the expiry markers and the Context curve.
    args = type("Args", (), {"since": None, "until": None, "days": None})()

    class NoCurve(ExpiryFakeStore):
        def supports_context_curve(self, wid):
            return False

    app = ot.App(NoCurve([workflow("w1", "2026-06-10 10:00:00", cost=0.0)]), args)
    assert all(t["cached"] is None for t in ot.session_extras(app, "w1")["turns"])


def test_web_turns_subtotal_each_run_not_each_prompt_id():
    # A prompt id is not unique -- a backend without explicit ids groups by the prompt
    # TEXT, so asking the same thing twice in one session gives A, B, A. The page drew a
    # header per RUN but looked its subtotal up in a Map keyed by ID, so both A headers
    # showed the two runs' combined cost and a $6 session rendered $4 + $2 + $4. Same bug
    # the TUI's turn_group_rows had, in the page's own copy of the grouping.
    js = _js_source()
    # turnGroupRows is the page's mirror of Renderer.turn_group_rows: a LIST of runs
    # addressed by ordinal, never a map keyed by the id.
    assert "function turnGroupRows(turns)" in js
    assert "if (!groups.length || key !== last)" in js  # a new entry per RUN
    assert "groups.get(key)" not in js  # never keyed by a repeatable id
    # Each drawn row reads its OWN group, so a repeated id cannot make two rows quote one
    # merged subtotal.
    assert "groups.forEach((g, n) =>" in js
    assert "moneyCell(g.cost)" in js


def test_web_ships_the_reasoning_level_and_names_the_switch_that_dropped_the_cache():
    # The Eff column and the ⚙ marker, mirrored. The cause travels with each expiry so
    # the page can tell an idle gap from an effort switch -- one "cache expired" line
    # covering both would tell someone who never went idle that they did.
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(ExpiryFakeStore([workflow("w1", "2026-06-10 10:00:00", cost=0.0)]), args)
    rows = app.session_turn_rows("w1")
    assert all("effort" in t for t in ot.session_extras(app, "w1")["turns"])
    rows[0]["effort"], rows[1]["effort"] = "high", "low"
    rows[1]["time"] = "2026-06-10 10:00:30"  # inside the TTL: the switch is the cause
    app._turns_by_session["w1"] = rows
    x = ot.session_extras(app, "w1")
    assert [t["effort"] for t in x["turns"]][:2] == ["high", "low"]
    (miss,) = x["expiries"]
    assert miss["cause"] == "reasoning" and miss["detail"] == "high → low"

    js = _js_source()
    drill = js.split("function turnDrillPane(", 1)[1].split("\nfunction ", 1)[0]
    assert "const hasEffort = g.indices.some(i => turns[i].effort);" in drill
    assert "h('th', null, 'Eff')" in drill
    table = js.split("function turnsTable(", 1)[1].split("\nfunction ", 1)[0]
    assert "e.cause === 'reasoning'" in table  # split out of the ❄ set
    assert "'⚙ reasoning effort '" in table


def test_web_prompt_rows_name_the_subagents_that_ran_them():
    # The page mirrors the TUI's Agents column: which subagents a prompt delegated to,
    # gated on the rows (a session that delegated nothing draws no column), with the
    # unnamed executions folded into one "subagent ×n" through the same dull-name set
    # the flamegraph labels use -- one set, so the two can't disagree about which names
    # are worth printing.
    js = _js_source()
    assert js.count("const DULL_AGENTS = new Set(") == 1  # shared with flameLabel
    assert "FLAME_DULL" not in js
    label = js.split("function agentLabel(", 1)[1].split("\nfunction ", 1)[0]
    assert "if (!t.depth) return;" in label  # main-thread turns are not delegation
    assert "'subagent ×' + unnamed" in label
    table = js.split("function turnsTable(", 1)[1].split("\nfunction ", 1)[0]
    assert "const hasAgents = groups.some(g => g.subturns);" in table
    assert "h('th', null, 'Agents')" in table
    assert "'↳ ' + g.agents" in table
    # The marker rows span the table, so their colspan follows the optional columns --
    # a fixed one leaves the ▼/❄ lines short of the right edge exactly when a column
    # appears.
    assert "const span = 8 + (hasCalls ? 1 : 0) + (hasAgents ? 1 : 0);" in table


def test_web_a_range_change_forgets_the_scope_state_itself():
    # applyRange used to leave this to the hashchange listener, via go('', ''). That
    # event only fires when the hash actually CHANGES, so applying a range from the root
    # scope -- the most ordinary way to do it -- kept an armed model sub-drill and a
    # typed filter over a dataset they were never chosen in. On screen that reads as a
    # Sessions tab that came back empty for no reason.
    js = _js_source()
    reset = "function resetScopeState() { FILTER = ''; EXPANDED.clear(); MSUB = null; }"
    assert reset in js
    body = js.split("function applyRange(", 1)[1].split("\nfunction ", 1)[0]
    assert "resetScopeState();" in body
    # ...and it happens BEFORE the render that paints the new range.
    assert body.index("resetScopeState();") < body.index("render(false)")
    # One rule, not two: navigation forgets exactly the same things.
    listener = next(ln for ln in js.splitlines() if "'hashchange'" in ln)
    assert "resetScopeState(); render();" in listener
    assert "MSUB = null" not in listener  # never a second, drifting copy
