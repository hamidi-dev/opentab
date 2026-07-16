"""The models.dev catalog, canonical ids, effective price and the `$` API-equivalent costing (pricing.py)."""

import json
import os
import sqlite3
import tempfile

import opentab as ot

from tests._support import _model_row, app_with, workflow


def test_canonical_model_folds_alias_spellings():
    # Route prefix ignored, dots == dashes, date pins and effort suffixes stripped.
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
    # eff = the mix's shares priced at the model's rates, per 1M tokens.
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
    # The P overlay's `f` and the `w` picker both go through pricing.model_matches, so
    # the rules are asserted once, here.
    m = ot.model_matches
    # Model id: fzf-style subsequence, and dots == dashes in both directions.
    assert m("opus48", "claude-opus-4-8")
    assert m("opus4.5", "claude-opus-4-5") and m("opus4-5", "claude-opus-4.5")
    assert not m("opus", "claude-sonnet-4-5")
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
    # input/output/cache priced per 1M, reasoning billed as output. Rates come from
    # the bundled models.dev snapshot, which is regenerated every release -- so
    # assert *resolution* (known models price, aliases fold, fallbacks catch), never
    # a pinned dollar figure that would break on the next scripts/update_prices.py.
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
    assert ot.model_price("unknown/future-model") == ot.FALLBACK_PRICE
    # 1M input + 1M output-equivalent: reasoning tokens bill as output.
    ir, orr, _cr, _cw = ot.model_price("x/claude-haiku-4.5")
    assert round(ot.api_equivalent_cost("x/claude-haiku-4.5", 1e6, 5e5, 5e5, 0, 0), 6) == round(
        ir + orr, 6
    )


def test_local_providers_are_not_priced():
    # Local models run on your own hardware: there is no per-token API bill, so the
    # "$" what-if must leave them at $0 rather than inventing cloud list prices.
    for name in ("ollama/llama3.1:70b", "mlx/qwen2.5", "lmstudio/whatever", "local/foo"):
        assert ot.model_price(name) == (0.0, 0.0, 0.0, 0.0)
        assert ot.api_equivalent_cost(name, 5e6, 1e6, 0, 0, 0) == 0.0
    # the same model id behind a cloud provider is still priced
    assert ot.api_equivalent_cost("anthropic/claude-haiku-4.5", 1e6, 0, 0, 0, 0) > 0


def test_api_price_toggle_prices_unpriced_usage():
    app = ot.App.__new__(ot.App)

    class _Store:
        demo = False

    app.store = _Store()
    app.show_api_prices = False
    app._models_loaded = True  # skip the deferred scan in toggle_api_prices
    app.loaded = [
        ot.Workflow(
            id="r",
            title="t",
            directory="d",
            created_at="2026-01-01",
            root_cost=10.0,
            total_cost=10.0,  # one model really billed $10
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
            model_count=1,
            total_tokens=0,
            unpriced_tokens=0,
        )
    ]
    app._snapshot_real_costs()
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
            root_cost=0.0,
            total_cost=0.5,  # real spend happened only in a child session
            subagents=1,
            model_count=2,
            total_tokens=0,
            unpriced_tokens=0,
        )
    ]
    app._snapshot_real_costs()
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
        app._ensure_models()
        app.toggle_api_prices()

        assert round(app.loaded[0].total_cost, 2) == 1.5
        assert round(app.loaded[0].root_cost, 2) == 1.0
        assert round(app.loaded[0].total_cost - app.loaded[0].root_cost, 2) == 0.5


def test_has_known_price_asks_where_the_price_came_from_not_what_it_equals():
    # Regression: has_known_price used to compare the resolved rates against
    # FALLBACK_PRICE, which brands any genuinely-catalogued model whose real rate card
    # happens to equal it as "unpriced" -- dropping it from the `w` picker and offering
    # it to a models.dev refresh that already knows it. The bundled catalog prices
    # openai/gpt-image-1-mini at exactly FALLBACK_PRICE, so this is not hypothetical.
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
        "junk": "not a dict",  # tolerated, skipped
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp  # so price_cache_path() lands in the temp dir
        src = os.path.join(tmp, "api.json")
        with open(src, "w") as fh:
            json.dump(models_dev, fh)
        try:
            ot.invalidate_price_cache()
            count, path = ot.refresh_model_prices(url="file://" + src)
            assert count == 2
            assert path == ot.price_cache_path()
            # a refreshed price overlays the embedded table
            assert ot.model_price("anthropic/claude-opus-4-8") == (99.0, 88.0, 0.0, 0.0)
            # a resold open model (vendor/model id) now prices off the cache, by bare id
            assert ot.model_price("moonshotai/kimi-k2.6") == (0.6, 2.5, 0.1, 0.0)
            meta = ot.price_cache_meta()
            assert meta and meta["count"] == 2 and meta["fetched_at"]
        finally:
            ot.invalidate_price_cache()
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


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
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp  # empty dir -> no prices.json
        try:
            ot.invalidate_price_cache()
            assert ot.price_cache_meta() is None
            ir, orr, _cr, _cw = ot.model_price("anthropic/claude-opus-4-8")
            assert ir > 0 and orr > 0  # from the bundled snapshot, not the (absent) cache
        finally:
            ot.invalidate_price_cache()
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_bundled_catalog_is_the_offline_price_source():
    # With no user cache, everything prices off the release-bundled models.dev
    # snapshot: every provider, so open models on paid routes resolve offline (the
    # old embedded table covered only the big three and left them to FALLBACK_PRICE).
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
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
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_price_layers_newest_fetch_wins():
    # The cache and the bundled snapshot are ordered by fetched_at: a year-old cache
    # must not shadow a fresher release snapshot, and a fresh refresh beats any
    # release. (Pre-catalog behavior was "cache always wins", which inverts once the
    # bundled layer refreshes every release.)
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
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
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_legacy_flat_price_cache_still_read():
    # A pre-catalog cache ({"models": {bare_id: [4 rates]}}) still prices lookups;
    # the models.dev view falls back to the bundled provider tree the flat shape
    # can't provide.
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
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
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


# --- the Context tab (window growth + composition) ---------------------------


def test_model_context_window_reads_catalog_and_falls_back_by_family():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
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
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg
