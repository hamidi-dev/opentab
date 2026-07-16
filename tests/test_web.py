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
    # over a [input, output, reasoning, cacheRead, cacheWrite] split. Written out here so
    # the serialized numbers can be repriced exactly the way the page reprices them -- and
    # so a drift between the two formulas fails a test.
    inp, out, reason, cr, cw = tok
    ir, orr, crr, cwr = rates
    return (inp * ir + (out + reason) * orr + cr * crr + cw * cwr) / 1e6


def _js_whatif_totals(payload, session_id, target):
    # The page's whatifTotals(), transcribed: both sides off the session's PER-MODEL rows,
    # both at list rates -- your models (each at its own) vs all of it at the target.
    rows = payload["models"].get(session_id) or []
    if not rows:
        return None
    rates = payload["whatif"]["rates"]
    actual, tot = 0.0, [0, 0, 0, 0, 0]
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
    assert root["tok"] == [1_000_000, 0, 0, 0, 0]  # [in, out, reasoning, cacheR, cacheW]
    assert kid["tok"] == [2_000_000, 0, 0, 0, 0]
    # The session's model rows carry the FULL split too -- without input/reasoning the
    # page could not compute the exact per-model baseline at all.
    by_model = {m["model"]: m["tok"] for m in payload["models"]["root"]}
    assert by_model["anthropic/claude-opus-4.5"] == [1_000_000, 0, 0, 0, 0]
    assert by_model["anthropic/claude-haiku-4.5"] == [2_000_000, 0, 0, 0, 0]
    models = payload["whatif"]["models"]
    # The picker's rows: models actually used, most-used first (the haiku subagent burned
    # 2M tokens, the opus root 1M), each with its four list rates in $/M.
    assert [m["model"] for m in models] == [
        "anthropic/claude-haiku-4.5",
        "anthropic/claude-opus-4.5",
    ]
    assert [m["tokens"] for m in models] == [2_000_000, 1_000_000]
    for m in models:
        assert m["price"] == [round(float(v), 6) for v in ot.model_price(m["model"])]
        assert len(m["price"]) == 4 and m["price"][0] > 0
    # ...and a rate card for every model used, armable or not: the baseline prices each
    # model's tokens at its own rates, so a model you cannot arm still has to be counted.
    assert set(payload["whatif"]["rates"]) == set(by_model)


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
    assert "(inp * ir + (out + reason) * orr + cr * crr + cw * cwr) / 1e6" in js
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
    assert payload["models"]["root"][0]["tok"] == [1_000_000, 0, 0, 0, 0]
    assert [m["model"] for m in payload["whatif"]["models"]] == [  # ...and both targets
        "anthropic/claude-haiku-4.5",
        "anthropic/claude-opus-4.5",
    ]
    actual, whatif = _js_whatif_totals(payload, "root", "anthropic/claude-haiku-4.5")
    assert actual > 0 and whatif > 0  # $0 recorded, but both sides priced at list rates
    assert whatif < actual  # haiku is cheaper than the opus that ran it


def test_web_page_matches_models_through_one_shared_rule():
    # One JS helper (modelMatches, the mirror of pricing.model_matches) behind BOTH model
    # filters: the P overlay's and the `w` picker's. Model ids match by subsequence, routes
    # and vendor labels by plain substring -- subsequencing the route is what made "gpt"
    # walk "github-copilot" and drag every Claude model sold through it into a GPT search.
    js = _js_source()
    assert js.count("function modelMatches(") == 1  # exactly one matcher, not two
    assert "rows = rows.filter(r => modelMatches(PRICES.q, r.model, r.routes, r.familyLabel))" in js
    assert "return modelMatches(WHATIF.q, bare, route ? [route] : [], '');" in js
    assert "fz(q, rt)" not in js  # the route-subsequence false-positive machine is gone
    assert "fields.some(f => f.includes(qq))" in js  # routes/labels: substring


def test_web_whatif_target_is_transient_and_app_wide_costs_never_move():
    # The target is deliberately NOT persisted -- not to localStorage (unlike the theme and
    # the price pins), not to the hash: a remembered what-if would silently falsify every
    # later look. And it is session-scoped, so no app-wide figure moves while it's armed.
    js = _js_source()
    keys = set(re.findall(r"localStorage\.setItem\('([^']+)'", js))
    assert keys == {"opentab-theme", "opentab-pins"}  # nothing what-if shaped
    assert "let WHATIF = { model: null, open: false, q: '', i: 0 };" in js
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
        assert extras == {"turns": [], "tools": [], "context": None}
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
