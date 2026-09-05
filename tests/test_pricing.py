import json
import os
import sqlite3
import tempfile

import opentab as ot

from tests._support import _model_row, app_with, workflow


def test_canonical_model_folds_alias_spellings():
    assert ot.canonical_model("anthropic/claude-sonnet-4.5") == "claude-sonnet-4-5"
    assert ot.canonical_model("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5"
    assert ot.canonical_model("gpt-4o-2024-08-06") == "gpt-4o"
    assert ot.canonical_model("gpt-5.2-xhigh") == ot.canonical_model("gpt-5.2")
    assert ot.canonical_model("gpt-5.1-codex-max-medium") == "gpt-5-1-codex-max"
    # Genuinely different models stay distinct.
    assert ot.canonical_model("gpt-5.2-codex") != ot.canonical_model("gpt-5.2")
    assert ot.canonical_model("gpt-5.2-pro") != ot.canonical_model("gpt-5.2")
    assert ot.canonical_model("claude-opus-4-6") != ot.canonical_model("claude-opus-4-5")
    # display_model keeps the id's own separator style, drops only the pins.
    assert ot.display_model("claude-sonnet-4.5") == "claude-sonnet-4.5"
    assert ot.display_model("claude-opus-4-5-20251101") == "claude-opus-4-5"
    assert ot.display_model("gpt-5.1-codex-max-xhigh") == "gpt-5.1-codex-max"


def test_effective_price_blends_mix_and_flags_missing_cache_read():
    eff, approx = ot.effective_price((2.0, 10.0, 0.2, 0.4), (0.5, 0.5, 0.0, 0.0))
    assert eff == 6.0 and not approx
    # A cache-heavy mix is dominated by the cache-read rate.
    eff, approx = ot.effective_price((2.0, 10.0, 0.2, 0.4), (0.1, 0.0, 0.9, 0.0))
    assert abs(eff - 0.38) < 1e-9 and not approx
    # No cache-read rate on record: reads bill at the input rate, flagged approximate.
    eff, approx = ot.effective_price((2.0, 10.0, 0.0, 0.0), (0.1, 0.0, 0.9, 0.0))
    assert approx and abs(eff - 2.0) < 1e-9
    # An all-zero (genuinely free) rate is not approximate -- there is no gap to fill.
    eff, approx = ot.effective_price((0.0, 0.0, 0.0, 0.0), (0.5, 0.5, 0.0, 0.0))
    assert eff == 0.0 and not approx


def test_price_token_mix_folds_reasoning_into_output():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            {
                "model_name": "anthropic/claude-opus-4-8",
                "cost": 0.0,
                "tokens_total": 100,
                "input": 10,
                "reasoning": 5,
                "cache_read": 80,
                "cache_write": 0,
                "output": 5,
            },
            _model_row("ollama/llama3.1", 0.0, 1000),  # local usage never skews the mix
        ]
    }
    mix = app.price_token_mix()
    assert mix is not None
    shares, total = mix
    assert total == 100
    assert shares == (0.10, 0.10, 0.80, 0.0)  # reasoning bills as output
    # The intro block states the mix the eff column prices at.
    assert any("80.0% cacheR" in ln for ln in app.renderer.price_intro_lines())
    # No usage at all -> no mix to price.
    app._model_by_root = {}
    assert app.price_token_mix() is None


def test_model_matches_is_the_one_rule_behind_every_model_filter():
    m = ot.model_matches
    # Model id: word-anchored fuzzy (util.anchored_fuzzy_match), and dots == dashes
    # in both directions.
    assert m("opus48", "claude-opus-4-8")
    assert m("opus4.5", "claude-opus-4-5") and m("opus4-5", "claude-opus-4.5")
    assert not m("opus", "claude-sonnet-4-5")
    # A bare subsequence over the id was a false-positive machine too: over the 5k-row
    # catalog "opus" matched every id carrying o-p-u-s scattered mid-word, and the
    # no-re-ranking rule (below) sorted that junk to the top instead of out of sight.
    assert not m("opus", "qwen3-coder-plus")
    assert not m("opus", "gemini-3.1-pro-preview-customtools")
    assert m("opus", "gemma-4-31b-claude-4.6-opus-reasoning-distilled")  # a real opus row
    # Route and vendor label: substring, so typing them in full works...
    assert m("copilot", "claude-sonnet-4.5", ("github-copilot",))
    assert m("anthropic", "claude-opus-4-5", ("github-copilot",), "Anthropic")
    # ...but a SUBSEQUENCE over them does not, which is the regression this rule exists
    # for: "gpt" walks g-ithub-co-p-ilo-t and used to drag every Claude model sold
    # through Copilot into a search for GPT.
    assert not m("gpt", "claude-sonnet-4.5", ("github-copilot",), "Anthropic")
    assert m("gpt", "gpt-5.2-xhigh", ("openai",), "OpenAI")
    # A query can never straddle two fields.
    assert not m("copilotclaude", "claude-opus-4-5", ("github-copilot",))
    assert m("", "anything")  # empty query matches everything


def test_api_price_helpers():
    # Assert catalog resolution rather than release-specific dollar values.
    for name in (
        "anthropic/claude-fable-5",
        "anthropic/claude-sonnet-4-5",
        "openai/gpt-4o-2024-05-13",
        "google/gemini-2.5-pro",
    ):
        ir, orr, _cr, _cw = ot.model_price(name)
        assert ir > 0 and orr > 0, name
        assert ot.model_price(name) != ot.FALLBACK_PRICE, name
    # a date pin resolves like the plain id, and a gateway route like the vendor's
    assert ot.model_price("anthropic/claude-fable-5-20260613") == ot.model_price(
        "anthropic/claude-fable-5"
    )
    assert ot.model_price("github-copilot/claude-haiku-4.5")[0] > 0
    # a reasoning-effort variant suffix falls back to its family price
    assert ot.model_price("openai/gpt-5.2-xhigh")[:2] == (1.75, 14.0)
    # A future Codex spelling must retain GPT-5.6's separately billed write rate rather
    # than falling through to generic GPT-5, where the fourth component is zero.
    assert ot.model_price("openai/gpt-5.6-codex") == (4.0, 20.0, 0.4, 5.0)
    assert ot.model_price("openai/gpt-6-astra-codex") == (10.0, 50.0, 1.0, 12.5)
    assert ot.model_price("openai/gpt-5-6-codex") == ot.model_price("openai/gpt-5.6-codex")
    assert ot.model_price("openai/gpt-5-6-sol-fast") == ot.model_price("openai/gpt-5.6-sol-fast")
    assert ot.has_known_price("openai/gpt-5-6-sol-fast")
    assert ot.model_price("openai/gpt-5-2025-08-07") == ot.model_price("openai/gpt-5")
    assert ot.model_price("openai/gpt-5-20250807") == ot.model_price("openai/gpt-5")
    assert ot.model_price("openai/gpt-5.6-sol")[3] == ot.model_price("openai/gpt-5.6-sol")[0] * 1.25
    assert ot.model_price("openai/gpt-6-astra")[3] == ot.model_price("openai/gpt-6-astra")[0] * 1.25
    assert ot.model_price("unknown/future-model") == ot.FALLBACK_PRICE
    # 1M input + 1M output-equivalent: reasoning tokens bill as output.
    ir, orr, _cr, _cw = ot.model_price("x/claude-haiku-4.5")
    assert round(ot.api_equivalent_cost("x/claude-haiku-4.5", 1e6, 5e5, 5e5, 0, 0), 6) == round(
        ir + orr, 6
    )


def test_local_providers_are_not_priced():
    for name in ("ollama/llama3.1:70b", "mlx/qwen2.5", "lmstudio/whatever", "local/foo"):
        assert ot.model_price(name) == (0.0, 0.0, 0.0, 0.0)
        assert ot.api_equivalent_cost(name, 5e6, 1e6, 0, 0, 0) == 0.0
    # the same model id behind a cloud provider is still priced
    assert ot.api_equivalent_cost("anthropic/claude-haiku-4.5", 1e6, 0, 0, 0, 0) > 0


def _priced_workflow(wid, root_cost=10.0, total_cost=10.0, subagents=0, model_count=2):
    # The one row the $-arithmetic tests below price. Its costs are the RECORDED ones;
    # each test then supplies the per-model split _compute_api_costs estimates from.
    return ot.Workflow(
        id=wid,
        title="t",
        directory="d",
        created_at="2026-01-01",
        root_cost=root_cost,
        total_cost=total_cost,
        subagents=subagents,
        model_count=model_count,
        total_tokens=0,
        unpriced_tokens=0,
    )


def _priced_app(**kw):
    # A REAL App parked in the recorded-cost view. Deliberately not an App.__new__ stub:
    # "$" re-anchors every cost-ranked cursor by value, so the action reads the view
    # state (years, the sessions list, the sidebar) that an uninitialised App lacks.
    app = app_with([_priced_workflow("r", **kw)])
    app.show_api_prices = False
    app._apply_price_mode()
    app._models_loaded = True  # skip the deferred scan in toggle_api_prices
    return app


def test_api_price_toggle_prices_unpriced_usage():
    app = _priced_app()

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

    # a real $10 row + a $0 subscription row that used 1M Haiku input tokens (=$1)
    app._model_by_root = {
        "r": [
            row("anthropic/claude-opus-4-6", 10.0, 0),
            row("github-copilot/claude-haiku-4.5", 0.0, 1_000_000),
        ]
    }
    app._compute_api_costs()

    assert app.loaded[0].total_cost == 10.0  # default view is actual cost
    app.toggle_api_prices()
    assert app.show_api_prices
    assert round(app.loaded[0].total_cost, 2) == 11.0  # real $10 + would-have-paid $1
    costs = {m["model_name"]: m["cost"] for m in app.model_mix("r")}
    assert costs["github-copilot/claude-haiku-4.5"] == 1.0  # priced from tokens
    assert costs["anthropic/claude-opus-4-6"] == 10.0  # real spend untouched
    app.toggle_api_prices()  # reversible
    assert not app.show_api_prices
    assert app.loaded[0].total_cost == 10.0
    assert (
        app.model_mix("r")
        and {m["model_name"]: m["cost"] for m in app.model_mix("r")}[
            "github-copilot/claude-haiku-4.5"
        ]
        == 0.0
    )


def test_api_price_toggle_prices_unpriced_part_of_mixed_model_row():
    app = _priced_app(model_count=1)
    app._model_by_root = {
        "r": [
            {
                "model_name": "github-copilot/claude-haiku-4.5",
                "runs": 2,
                "cost": 10.0,  # one message was billed, one was subscription/credit
                "tokens_total": 2_000_000,
                "input": 2_000_000,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "unpriced_input": 1_000_000,
                "unpriced_output": 0,
                "unpriced_reasoning": 0,
                "unpriced_cache_read": 0,
                "unpriced_cache_write": 0,
            }
        ]
    }

    app._compute_api_costs()
    app.toggle_api_prices()

    assert round(app.loaded[0].total_cost, 2) == 11.0
    assert app.model_mix("r")[0]["cost"] == 11.0


def test_api_price_toggle_splits_root_and_subagent_unpriced_usage():
    # real spend happened only in a child session
    app = _priced_app(root_cost=0.0, total_cost=0.5, subagents=1)
    app._model_by_root = {
        "r": [
            {
                "model_name": "github-copilot/claude-haiku-4.5",
                "runs": 1,
                "cost": 0.0,
                "root_cost": 0.0,
                "tokens_total": 1_000_000,
                "input": 1_000_000,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "unpriced_input": 1_000_000,
                "unpriced_output": 0,
                "unpriced_reasoning": 0,
                "unpriced_cache_read": 0,
                "unpriced_cache_write": 0,
                "root_unpriced_input": 1_000_000,
                "root_unpriced_output": 0,
                "root_unpriced_reasoning": 0,
                "root_unpriced_cache_read": 0,
                "root_unpriced_cache_write": 0,
            },
            {
                "model_name": "openai/gpt-5-mini",
                "runs": 1,
                "cost": 0.5,
                "root_cost": 0.0,
                "tokens_total": 0,
                "input": 0,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "unpriced_input": 0,
                "unpriced_output": 0,
                "unpriced_reasoning": 0,
                "unpriced_cache_read": 0,
                "unpriced_cache_write": 0,
                "root_unpriced_input": 0,
                "root_unpriced_output": 0,
                "root_unpriced_reasoning": 0,
                "root_unpriced_cache_read": 0,
                "root_unpriced_cache_write": 0,
            },
        ]
    }

    app._compute_api_costs()
    app.toggle_api_prices()

    assert round(app.loaded[0].total_cost, 2) == 1.5
    assert round(app.loaded[0].root_cost, 2) == 1.0
    assert round(app.loaded[0].total_cost - app.loaded[0].root_cost, 2) == 0.5


def test_api_price_split_uses_store_root_unpriced_columns_for_same_model():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            create table session (
              id text primary key,
              parent_id text,
              title text,
              directory text,
              time_created integer,
              cost real default 0 not null,
              tokens_input integer default 0 not null,
              tokens_output integer default 0 not null,
              tokens_reasoning integer default 0 not null,
              tokens_cache_read integer default 0 not null,
              tokens_cache_write integer default 0 not null
            );
            create table message (session_id text, data text);
            """
        )
        conn.executemany(
            "insert into session values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("root", None, "Root", "/tmp/project", 1760000000000, 0.0, 1_000_000, 0, 0, 0, 0),
                ("child", "root", "Child", "/tmp/project", 1760000001000, 0.5, 0, 1, 0, 0, 0),
            ],
        )
        conn.executemany(
            "insert into message values (?, ?)",
            [
                (
                    "root",
                    '{"role":"assistant","providerID":"github-copilot","modelID":"claude-haiku-4.5","cost":0,"tokens":{"input":1000000,"output":0}}',
                ),
                (
                    "child",
                    '{"role":"assistant","providerID":"github-copilot","modelID":"claude-haiku-4.5","cost":0.5,"tokens":{"input":0,"output":1}}',
                ),
            ],
        )
        conn.commit()
        conn.close()

        store = ot.Store(db, type("Args", (), {"demo": False})())
        app = ot.App(store, type("Args", (), {"since": None, "until": None, "days": None})())
        app._ensure_models()  # the estimate view is the default

        assert round(app.loaded[0].total_cost, 2) == 1.5
        assert round(app.loaded[0].root_cost, 2) == 1.0
        assert round(app.loaded[0].total_cost - app.loaded[0].root_cost, 2) == 0.5


def test_has_known_price_asks_where_the_price_came_from_not_what_it_equals():
    # A catalogued card may legitimately equal FALLBACK_PRICE.
    assert ot.model_price("openai/gpt-image-1-mini") == ot.pricing.FALLBACK_PRICE
    assert ot.has_known_price("openai/gpt-image-1-mini")  # catalogued -> real, priced model
    assert ot.has_known_price("anthropic/claude-opus-4.5")  # named outright
    assert ot.has_known_price("anthropic/claude-opus-9-99")  # unknown id, known family
    assert not ot.has_known_price("unknown/not recorded")  # nothing knows it
    assert not ot.has_known_price("ollama/llama3.3")  # local: no API rate exists at all


def test_refresh_model_prices_writes_cache_and_overlays_table():
    models_dev = {
        "anthropic": {
            "models": {"claude-opus-4-8": {"cost": {"input": 99.0, "output": 88.0}}}
        },  # overrides the embedded snapshot for this model
        "openrouter": {
            "models": {
                "moonshotai/kimi-k2.6": {"cost": {"input": 0.6, "output": 2.5, "cache_read": 0.1}}
            }
        },
        # A valid but incomplete GPT-6 card must not make separately recorded writes free.
        "openai": {
            "models": {"gpt-6-astra": {"cost": {"input": 10.0, "output": 50.0, "cache_read": 1.0}}}
        },
        "junk": "not a dict",  # tolerated, skipped
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = tmp  # so price_cache_path() lands in the temp dir
        src = os.path.join(tmp, "api.json")
        with open(src, "w") as fh:
            json.dump(models_dev, fh)
        try:
            ot.invalidate_price_cache()
            count, path = ot.refresh_model_prices(url="file://" + src)
            assert count == 3
            assert path == ot.price_cache_path()
            # a refreshed price overlays the embedded table
            assert ot.model_price("anthropic/claude-opus-4-8") == (99.0, 88.0, 0.0, 0.0)
            # a resold open model (vendor/model id) now prices off the cache, by bare id
            assert ot.model_price("moonshotai/kimi-k2.6") == (0.6, 2.5, 0.1, 0.0)
            assert ot.model_price("openai/gpt-6-astra") == (10.0, 50.0, 1.0, 12.5)
            catalog = {(pid, mid): price for pid, mid, price, _status in ot.catalog_models()}
            assert catalog[("openai", "gpt-6-astra")] == (10.0, 50.0, 1.0, 12.5)
            meta = ot.price_cache_meta()
            assert meta and meta["count"] == 3 and meta["fetched_at"]
        finally:
            ot.invalidate_price_cache()
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_refresh_model_prices_rejects_empty_response():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "api.json")
        with open(src, "w") as fh:
            json.dump({"anthropic": {"models": {}}}, fh)  # no priced models
        raised = False
        try:
            ot.refresh_model_prices(url="file://" + src, dest=os.path.join(tmp, "p.json"))
        except ValueError:
            raised = True
        assert raised
        assert not os.path.exists(os.path.join(tmp, "p.json"))  # nothing written on failure


def test_model_price_uses_embedded_table_without_cache():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = tmp  # empty dir -> no prices.json
        try:
            ot.invalidate_price_cache()
            assert ot.price_cache_meta() is None
            ir, orr, _cr, _cw = ot.model_price("anthropic/claude-opus-4-8")
            assert ir > 0 and orr > 0  # from the bundled snapshot, not the (absent) cache
        finally:
            ot.invalidate_price_cache()
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_bundled_catalog_is_the_offline_price_source():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = tmp
        try:
            ot.invalidate_price_cache()
            meta = ot.price_source_meta()
            assert meta and meta["kind"] == "bundled" and meta["count"] > 500
            assert meta["fetched_at"]
            rows = ot.catalog_models()
            assert len(rows) > 1000
            pid, mid, price, status = rows[0]
            assert pid and mid and len(price) == 4 and isinstance(status, str)
            assert ot.model_price("openrouter/deepseek/deepseek-chat") != ot.FALLBACK_PRICE
        finally:
            ot.invalidate_price_cache()
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_price_layers_newest_fetch_wins():
    # fetched_at, not layer type, determines precedence.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = tmp
        cache_dir = os.path.join(tmp, "opentab")
        os.makedirs(cache_dir)

        def write_cache(fetched_at, models):
            with open(os.path.join(cache_dir, "prices.json"), "w") as fh:
                json.dump(
                    {
                        "fetched_at": fetched_at,
                        "providers": {"anthropic": {"name": "Anthropic", "models": models}},
                    },
                    fh,
                )
            ot.invalidate_price_cache()

        try:
            ot.invalidate_price_cache()
            bundled = ot.model_price("anthropic/claude-fable-5")
            stale = {"claude-fable-5": {"cost": [111.0, 222.0, 0.0, 0.0]}}
            write_cache("2000-01-01T00:00:00Z", stale)  # ancient cache: bundled wins
            assert ot.model_price("anthropic/claude-fable-5") == bundled
            assert ot.price_source_meta()["kind"] == "bundled"
            write_cache("9999-01-01T00:00:00Z", stale)  # fresher cache: it wins
            assert ot.model_price("anthropic/claude-fable-5") == (111.0, 222.0, 0.0, 0.0)
            assert ot.price_source_meta()["kind"] == "cache"
        finally:
            ot.invalidate_price_cache()
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_legacy_flat_price_cache_still_read():
    # The legacy flat cache has rates but no provider tree.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = tmp
        cache_dir = os.path.join(tmp, "opentab")
        os.makedirs(cache_dir)
        with open(os.path.join(cache_dir, "prices.json"), "w") as fh:
            json.dump(
                {
                    "fetched_at": "9999-01-01T00:00:00Z",
                    "models": {"claude-fable-5": [111.0, 222.0, 0.0, 0.0]},
                },
                fh,
            )
        try:
            ot.invalidate_price_cache()
            assert ot.model_price("anthropic/claude-fable-5") == (111.0, 222.0, 0.0, 0.0)
            assert ot.price_source_meta()["kind"] == "cache"
            assert ot.catalog_models()  # bundled tree still backs the catalog view
        finally:
            ot.invalidate_price_cache()
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


# --- the Context tab (window growth + composition) ---------------------------


def test_model_context_window_reads_catalog_and_falls_back_by_family():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = tmp
        cache_dir = os.path.join(tmp, "opentab")
        os.makedirs(cache_dir)
        try:
            with open(os.path.join(cache_dir, "prices.json"), "w") as fh:
                json.dump(
                    {
                        "fetched_at": "9999-01-01T00:00:00Z",
                        "providers": {
                            "acme": {
                                "name": "Acme",
                                "models": {
                                    "acme-large": {"cost": [1.0, 2.0, 0.0, 0.0], "limit": 123456}
                                },
                            }
                        },
                    },
                    fh,
                )
            ot.invalidate_price_cache()
            # catalog limit wins; the route prefix is stripped like model_price
            assert ot.model_context_window("acme/acme-large") == 123456
            # unknown ids fall back by hand-kept family, then the default
            assert ot.model_context_window("x/claude-nonexistent") == 200_000
            assert ot.model_context_window("x/gpt-5-hyper-9999") == 400_000
            assert ot.model_context_window("x/total-mystery") == ot.DEFAULT_CONTEXT_WINDOW
        finally:
            ot.invalidate_price_cache()
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_model_price_folds_a_suffixed_id_only_off_a_resale_card():
    """Harnesses log the id they were handed ("claude-3-5-haiku-20241022"), and gateways
    list exactly those dated/effort-suffixed spellings -- usually with no cache-read rate.
    An exact hit on that junk beat both the plain spelling's complete card and the
    hand-kept family fallbacks, so a cache-heavy session was priced ~82% under. Fold to
    the plain spelling when only resale routes carry the suffixed one -- but never when
    the suffixed id is the vendor's OWN card, because a dated card is real pricing:
    openai sells gpt-4o-2024-05-13 dearer than plain gpt-4o."""
    haiku = ot.pricing.model_price("anthropic/claude-3-5-haiku-20241022")
    assert haiku == ot.pricing.model_price("anthropic/claude-3-5-haiku")
    assert haiku[2] > 0 and haiku[3] > 0  # the cache rates the resale card omitted

    # An authoritative dated vendor card keeps its own, dearer rates.
    assert ot.pricing.model_price("openai/gpt-4o-2024-05-13") == (5.0, 15.0, 0.0, 0.0)
    assert ot.pricing.model_price("openai/gpt-4o") == (2.5, 10.0, 1.25, 0.0)

    # The load-bearing what-if invariant: arming the canonical twin of the only model a
    # session used must be exactly a $0 change. The two spellings priced apart broke it.
    tok = (500_000, 200_000, 0, 30_000_000, 3_000_000)
    for used, armed in (
        ("openai/gpt-5.3-codex-xhigh", "openai/gpt-5.3-codex"),
        ("anthropic/claude-3-5-haiku-20241022", "anthropic/claude-3-5-haiku"),
    ):
        assert ot.pricing.api_equivalent_cost(used, *tok) == ot.pricing.api_equivalent_cost(
            armed, *tok
        )
    assert ot.pricing.has_known_price("openai/gpt-5.3-codex-xhigh")


def test_vendor_route_beats_a_gateway_markup_for_every_family():
    """ "The model's own vendor route wins over gateway resale rates" compared a models.dev
    provider id against the family inferred from the model NAME. They differ for Qwen
    (alibaba), Kimi (moonshotai) and GLM (zhipuai), so the test silently found no vendor
    at all and a gateway won on completeness plus file order -- llmgateway's
    qwen3-coder-plus card is 6x Alibaba's input rate, pricing a session 19x over."""
    assert ot.pricing.model_price("alibaba/qwen3-coder-plus") == (1.0, 5.0, 0.0, 0.0)
    assert ot.pricing.is_vendor_route("alibaba", "qwen3-coder-plus")
    assert ot.pricing.is_vendor_route("moonshotai", "kimi-k2")
    # Identity still holds where the two spellings already agreed...
    assert ot.pricing.is_vendor_route("anthropic", "claude-sonnet-4-5")
    assert ot.pricing.is_vendor_route("xai", "grok-4")
    # ...and a gateway is never the vendor. alibaba-coding-plan is deliberately absent:
    # it prices at $0 (a plan, not a rate card) and would beat the real one outright.
    assert not ot.pricing.is_vendor_route("openrouter", "claude-sonnet-4-5")
    assert not ot.pricing.is_vendor_route("alibaba-coding-plan", "qwen3-coder-plus")
    assert not ot.pricing.is_vendor_route("azure", "gpt-4o")
    # Zhipu is deliberately unmapped: it sells GLM through zhipuai AND zai at different
    # regional rates, and keying by bare model id cannot tell which one was bought, so
    # naming both would only hand the choice to catalog file order.
    assert not ot.pricing.is_vendor_route("zhipuai", "glm-4.6")


def test_a_zero_rate_card_never_wins_on_being_the_vendor():
    """A vendor lists an all-zero card for models it only sells inside a plan. That is not
    a rate card, so it must not shadow a route that publishes real rates -- zhipuai prices
    glm-4.7-flash at (0,0,0,0) while half a dozen routes quote a real one. A $0 resolution
    would also read as free in `$` and make the model armable in `w` for "all at $0"."""
    price = ot.pricing.model_price("zhipuai/glm-4.7-flash")
    assert price != (0.0, 0.0, 0.0, 0.0) and price[0] > 0

    # And a suffixed id whose vendor _MODEL_FAMILIES doesn't name must NOT fold: every
    # route looks like resale there, so folding would be a guess -- sakana's own dated
    # card would pick up a cache-write rate it never charges.
    assert ot.pricing.model_family("fugu-ultra-20260615") == ""
    assert ot.pricing.model_price("sakana/fugu-ultra-20260615") == (5.0, 30.0, 0.5, 0.0)


def test_pricing_bills_long_ttl_cache_writes_at_the_long_ttl_rate():
    # models.dev carries Anthropic's 5m write rate; the 1h rate is 2x input.
    m = "anthropic/claude-opus-4-5"
    inp, _out, _cr, cw = ot.model_price(m)
    assert cw == round(inp * 1.25, 6)  # the catalog rate IS the 5m tier
    assert ot.cache_write_1h_price(m) == inp * 2.0

    # REPLACEMENT, not addition: a long write leaves the 5m bucket. Billing it as extra
    # volume would double-charge every one of those tokens.
    short = ot.api_equivalent_cost(m, 0, 0, 0, 0, 1_000_000)
    long = ot.api_equivalent_cost(m, 0, 0, 0, 0, 1_000_000, 1_000_000)
    assert short == cw and long == inp * 2.0
    assert long != short + inp * 2.0  # would be the double-billing bug
    half = ot.api_equivalent_cost(m, 0, 0, 0, 0, 1_000_000, 500_000)
    assert abs(half - (cw + inp * 2.0) / 2) < 1e-9

    # Omitting the argument must reproduce the old arithmetic exactly -- every backend but
    # Claude Code can't see a TTL tier and must keep pricing as it always did.
    assert ot.api_equivalent_cost(m, 10, 20, 5, 100, 200) == ot.api_equivalent_cost(
        m, 10, 20, 5, 100, 200, 0
    )

    # A malformed split can't distort the total: clamped into [0, cache_write].
    assert ot.api_equivalent_cost(m, 0, 0, 0, 0, 1000, 10**9) == ot.api_equivalent_cost(
        m, 0, 0, 0, 0, 1000, 1000
    )
    assert ot.api_equivalent_cost(m, 0, 0, 0, 0, 1000, -5) == ot.api_equivalent_cost(
        m, 0, 0, 0, 0, 1000, 0
    )

    # Gated on the VENDOR, not on "a count was passed": TTL-tiered writes are an Anthropic
    # rule, so no other vendor's writes can be inflated by handing this a number.
    for other in ("openai/gpt-5.6", "google/gemini-3-pro"):
        assert ot.cache_write_1h_price(other) == ot.model_price(other)[3]
        assert ot.api_equivalent_cost(other, 0, 0, 0, 0, 1_000, 1_000) == ot.api_equivalent_cost(
            other, 0, 0, 0, 0, 1_000, 0
        )
    # A gateway reselling Claude is still Anthropic (model_family reads the bare name),
    # and the markup rides along because it marks up input too.
    resold = "github-copilot/claude-opus-4-5"
    assert ot.cache_write_1h_price(resold) == ot.model_price(resold)[0] * 2.0


def _turn(
    time,
    model="anthropic/claude-opus-4-8",
    read=0,
    write=0,
    write_1h=0,
    inp=0,
    prompt="p",
    depth=0,
    effort="",
):
    return {
        "time": time,
        "model_name": model,
        "cache_read": read,
        "cache_write": write,
        "cache_write_1h": write_1h,
        "input": inp,
        "prompt_id": prompt,
        "depth": depth,
        "effort": effort,
    }


def test_cache_ttl_is_read_off_the_turn_not_off_a_provider_table():
    # Cache TTL is per request, not per provider.
    assert ot.cache_ttl_seconds("anthropic/claude-opus-4-8", 900, 1000) == ot.CACHE_TTL_LONG
    assert ot.cache_ttl_seconds("anthropic/claude-opus-4-8", 0, 1000) == ot.CACHE_TTL_SHORT
    # Claude sold through a gateway keeps Anthropic's contract -- the FAMILY decides,
    # never the route (github-copilot also resells OpenAI, on different terms).
    assert ot.cache_ttl_seconds("github-copilot/claude-opus-4.5", 0, 1000) == ot.CACHE_TTL_SHORT
    # OpenAI gives GPT-5.6+ a 30-minute MINIMUM lifetime, not an exact expiry. None keeps
    # the analysis from claiming "it lived 30m" when OpenAI may retain the entry longer.
    assert ot.cache_ttl_seconds("openai/gpt-5.5", 0, 1000) is None
    assert ot.cache_ttl_seconds("openai/gpt-5.6-sol", 0, 1000) is None
    assert ot.cache_ttl_seconds("openai/gpt-6-astra", 0, 1000) is None


def test_cache_miss_blames_the_wait_only_when_the_gap_was_the_users():
    rows = [
        _turn("2026-06-10 10:00:00", read=200000, write=100000, write_1h=100000, prompt="a"),
        _turn("2026-06-10 12:00:00", write=300000, write_1h=300000, prompt="b"),  # 2h later
    ]
    (miss,) = ot.cache_misses(rows)
    assert miss.cause == "waited"  # the gap ended on a NEW prompt: the follow-up was late
    assert miss.index == 1 and miss.idle == 7200 and miss.ttl == ot.CACHE_TTL_LONG
    assert miss.repaid == 300000
    # Priced as what those tokens cost ABOVE a cache hit: re-bought at the 1h write rate
    # (2x input) against the 0.1x they would have cost had the entry lived.
    ir, _o, crr, _cw = ot.model_price("anthropic/claude-opus-4-8")
    assert abs(miss.cost - 300000 * (ir * 2 - crr) / 1e6) < 1e-9

    # The same 2-hour gap, but the agent was the one filling it -- a long tool call or
    # build under the SAME prompt. Real money, but not something the reader did.
    same_prompt = [dict(rows[0]), dict(rows[1], prompt_id="a")]
    assert ot.cache_misses(same_prompt)[0].cause == "agent"

    # ... and likewise when a subagent ground through the gap. Subagent rows never
    # compare against the main thread (their own context windows), but their timestamps
    # prove the session was not idle.
    with_sub = [rows[0], _turn("2026-06-10 11:00:00", depth=1, read=50000), rows[1]]
    assert ot.cache_misses(with_sub)[0].cause == "agent"


def test_cache_miss_separates_causes_it_must_not_blame_on_waiting():
    prefix = _turn("2026-06-10 10:00:00", read=200000, write=100000, write_1h=100000, prompt="a")

    # Inside the TTL: the prefix changed under it (edited tools, an added image, a
    # different tool_choice -- Anthropic lists them), which is not the reader's timing.
    quick = ot.cache_misses([prefix, _turn("2026-06-10 10:00:30", write=300000, prompt="b")])
    assert quick[0].cause == "invalidated"

    # A different model cannot read another model's cache, whatever the gap.
    switched = ot.cache_misses(
        [
            prefix,
            _turn(
                "2026-06-10 12:00:00", model="anthropic/claude-haiku-4-5", write=300000, prompt="b"
            ),
        ]
    )
    assert switched[0].cause == "switched"

    # The window was rebuilt smaller: a compaction freed it, it did not expire.
    small = ot.cache_misses([prefix, _turn("2026-06-10 12:00:00", write=20000, prompt="b")])
    assert small[0].cause == "compacted"

    # OpenAI publishes a minimum lifetime, not the exact point when this entry disappeared.
    oa = _turn("2026-06-10 10:00:00", model="openai/gpt-5.6-sol", read=200000, write=100000)
    late = _turn("2026-06-10 20:00:00", model="openai/gpt-5.6-sol", inp=300000, prompt="b")
    assert ot.cache_misses([oa, late])[0].cause == "invalidated"

    # A prefix too small to have been cacheable at all is never reported as lost.
    tiny = _turn("2026-06-10 10:00:00", read=1000, write=500)
    assert ot.cache_misses([tiny, _turn("2026-06-10 12:00:00", write=1500, prompt="b")]) == []


def test_cache_miss_names_a_reasoning_switch_instead_of_the_catch_all():
    # The transcript exposes effort changes but not the other invalidation causes.
    hi = _turn("2026-06-10 10:00:00", read=200000, write=100000, effort="high", prompt="a")
    lo = _turn("2026-06-10 10:00:30", write=300000, effort="low", prompt="b")
    (miss,) = ot.cache_misses([hi, lo])
    assert miss.cause == "reasoning" and miss.detail == "high → low"
    assert miss.repaid == 300000 and miss.cost > 0  # priced like any other re-buy

    # Same level on both sides is not a switch -- it stays the honest catch-all.
    same = ot.cache_misses(
        [hi, _turn("2026-06-10 10:00:30", write=300000, effort="high", prompt="b")]
    )
    assert same[0].cause == "invalidated" and same[0].detail == ""

    # A level recorded on one side only is a backend that stopped writing the field
    # (or started), never a switch -- both directions.
    for pair in (
        [hi, _turn("2026-06-10 10:00:30", write=300000, prompt="b")],
        [_turn("2026-06-10 10:00:00", read=200000, write=100000, prompt="a"), lo],
    ):
        assert ot.cache_misses(pair)[0].cause == "invalidated"

    # The stronger explanations still win: a model swap and a compaction both outrank
    # it, and so does an entry that was dead by the clock anyway.
    swap = ot.cache_misses(
        [
            hi,
            _turn(
                "2026-06-10 10:00:30",
                model="anthropic/claude-haiku-4-5",
                write=300000,
                effort="low",
            ),
        ]
    )
    assert swap[0].cause == "switched"
    shrunk = ot.cache_misses(
        [hi, _turn("2026-06-10 10:00:30", write=20000, effort="low", prompt="b")]
    )
    assert shrunk[0].cause == "compacted"
    stale = ot.cache_misses(
        [hi, _turn("2026-06-10 12:00:00", write=300000, effort="low", prompt="b")]
    )
    assert stale[0].cause == "waited"  # two hours dead: the switch is not what killed it


def test_cache_miss_prices_a_provider_that_bills_no_cache_write():
    # Providers without cache-write accounting report the re-buy as uncached input.
    m = "github-copilot/claude-opus-4.5"
    rows = [
        _turn("2026-06-10 10:00:00", model=m, read=200000, prompt="a"),
        _turn("2026-06-10 10:30:00", model=m, inp=200000, prompt="b"),  # past the 5m TTL
    ]
    (miss,) = ot.cache_misses(rows)
    assert miss.cause == "waited" and miss.ttl == ot.CACHE_TTL_SHORT
    ir, _o, crr, _cw = ot.model_price(m)
    assert abs(miss.cost - 200000 * (ir - crr) / 1e6) < 1e-9


def test_cache_miss_reads_the_ttl_off_the_entry_that_died_not_the_one_replacing_it():
    # The lifetime that ran out belongs to the turn that CREATED or last refreshed the
    # entry -- prev -- never to cur, which is re-buying the context and may write a
    # different tier. Read off cur, the verdict was wrong on 5.5% of cold turns in a real
    # corpus, and wrong in both directions.
    def turn(time, read=0, write=0, write_1h=0, inp=0, prompt="p"):
        return _turn(time, read=read, write=write, write_1h=write_1h, inp=inp, prompt=prompt)

    # prev bought an HOUR; cur re-buys on the 5-minute tier ten minutes later. The entry
    # still had 50 minutes to live, so nothing expired -- the prefix changed under it.
    alive = ot.cache_misses(
        [
            turn("2026-06-10 10:00:00", read=200000, write=100000, write_1h=100000, prompt="a"),
            turn("2026-06-10 10:10:00", write=300000, write_1h=0, prompt="b"),
        ]
    )
    assert alive[0].cause == "invalidated" and alive[0].ttl == ot.CACHE_TTL_LONG

    # The mirror image: prev bought 5 minutes, cur happens to write an hour. Ten minutes
    # is genuinely past a 5-minute entry, and reading cur's tier would have excused it.
    dead = ot.cache_misses(
        [
            turn("2026-06-10 10:00:00", read=200000, write=100000, write_1h=0, prompt="a"),
            turn("2026-06-10 10:10:00", write=300000, write_1h=300000, prompt="b"),
        ]
    )
    assert dead[0].cause == "waited" and dead[0].ttl == ot.CACHE_TTL_SHORT


def test_priority_processing_modes_become_priced_ids_of_their_own():
    """models.dev files priority processing as a MODE on the base model
    (experimental.modes.fast: a `service_tier: priority` flag plus its own 2x card), but
    every harness logs it as a model -- OpenCode writes modelID "gpt-5.6-sol-fast". The
    pruner read only `cost`, so that id matched no vendor row and fell through to the one
    gateway listing the spelling (vercel, quoting OpenAI's BASE rate): fast sessions
    priced at exactly half, with no gap to show for it."""
    catalog = {
        "openai": {
            "name": "OpenAI",
            "models": {
                "gpt-5.6-sol": {
                    "cost": {"input": 4, "output": 20, "cache_read": 0.4, "cache_write": 5},
                    "limit": {"context": 1050000},
                    "experimental": {
                        "modes": {
                            # Dearer rates AND a request flag: needs a row of its own.
                            "fast": {
                                "cost": {
                                    "input": 8,
                                    "output": 40,
                                    "cache_read": 0.8,
                                    "cache_write": 10,
                                },
                                "provider": {"body": {"service_tier": "priority"}},
                            },
                            # Only a request flag: bills at the base rate, so no row.
                            "pro": {"provider": {"body": {"reasoning": {"mode": "pro"}}}},
                        }
                    },
                },
                "gpt-legacy": {
                    "cost": {"input": 1, "output": 2},
                    "status": "deprecated",
                    "limit": {"context": 128000},
                    # A provider that already sells the spelling keeps its own card.
                    "experimental": {"modes": {"fast": {"cost": {"input": 99, "output": 99}}}},
                },
                "gpt-legacy-fast": {"cost": {"input": 3, "output": 6}},
                # A gateway can quote a fast rate for a model it lists no base rate for.
                "gpt-plan-only": {
                    "cost": {"input": None, "output": None},
                    "status": "beta",
                    "experimental": {"modes": {"fast": {"cost": {"input": 7, "output": 21}}}},
                },
            },
        }
    }
    models = ot.pricing.prune_models_dev(catalog)["openai"]["models"]

    assert models["gpt-5.6-sol-fast"]["cost"] == [8.0, 40.0, 0.8, 10.0]
    assert models["gpt-5.6-sol"]["cost"] == [4.0, 20.0, 0.4, 5.0]
    # The mode shares the base model's context window and lifecycle status.
    assert models["gpt-5.6-sol-fast"]["limit"] == 1050000
    assert models["gpt-plan-only-fast"]["status"] == "beta"
    assert "gpt-5.6-sol-pro" not in models
    assert models["gpt-legacy-fast"]["cost"] == [3.0, 6.0, 0.0, 0.0]  # the real card, not 99
    assert models["gpt-plan-only-fast"]["cost"] == [7.0, 21.0, 0.0, 0.0]
    assert "gpt-plan-only" not in models

    # End to end on the bundled snapshot: fast is exactly twice base, and the vendor row
    # now outranks the gateway that used to answer for it.
    base = ot.pricing.model_price("openai/gpt-5.6-sol")
    fast = ot.pricing.model_price("openai/gpt-5.6-sol-fast")
    assert fast == tuple(2 * x for x in base)
    assert ot.pricing.model_price("gpt-5.6-sol-fast") == fast  # bare id, as harnesses log it
    assert ot.pricing.model_price("anthropic/claude-opus-5-fast") == tuple(
        2 * x for x in ot.pricing.model_price("anthropic/claude-opus-5")
    )
