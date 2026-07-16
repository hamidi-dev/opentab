"""The CSV and JSONL request-log backends (stores/csv_source.py + stores/jsonl_source.py)."""

import json
import os
import re
import tempfile

import opentab as ot

from tests._support import FakeStore, _jsonl_args, _parse, _write_csv, _write_jsonl, workflow


def _csv_args():
    return type("Args", (), {"demo": False})()


def test_csv_store_splits_cache_prefixes_providers_and_stays_unpriced():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "copilot.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "cached_tokens", "session_id"],
            [
                # input_tokens includes the cached read (OpenAI style) -> uncached 8000
                ["2026-06-18T10:00:00Z", "claude-sonnet-4", 12000, 800, 4000, "s1"],
                ["2026-06-18T10:05:00Z", "gpt-4o", 5000, 300, 0, "s1"],
                ["2026-06-17T09:00:00Z", "gemini-2.5-pro", 2000, 150, 0, "s2"],
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        assert store.records_cost is False  # no cost column -> subscription-style
        workflows = store.workflows()
        assert {w.id for w in workflows} == {"s1", "s2"}
        s1 = next(w for w in workflows if w.id == "s1")
        assert s1.source == "CSV"
        assert s1.subagents == 0  # CSV has no subagent tree
        assert s1.total_cost == 0.0  # recorded cost is $0
        assert s1.total_tokens == s1.unpriced_tokens == 12800 + 5300  # all unpriced

        rows = {r["model_name"]: r for r in store.model_breakdown() if r["root_id"] == "s1"}
        # mixed providers each get the right prefix so pricing + the Providers tab work
        assert set(rows) == {"anthropic/claude-sonnet-4", "openai/gpt-4o"}
        cl = rows["anthropic/claude-sonnet-4"]
        assert cl["input"] == 8000 and cl["cache_read"] == 4000  # cached split out of input
        assert cl["unpriced_input"] == 8000 and cl["unpriced_cache_read"] == 4000

        # the "$" what-if reprices the unpriced tokens at list price (non-zero)
        est = ot.api_equivalent_cost(
            cl["model_name"],
            cl["unpriced_input"],
            cl["unpriced_output"],
            cl["unpriced_reasoning"],
            cl["unpriced_cache_read"],
            cl["unpriced_cache_write"],
        )
        assert est > 0

        # one flat depth-0 node aggregating both of s1's models
        nodes = store.workflow_nodes("s1")
        assert len(nodes) == 1 and nodes[0]["depth"] == 0
        assert nodes[0]["tokens_input"] == 13000  # 8000 + 5000 uncached
        assert nodes[0]["tokens_total"] == 18100
        assert nodes[0]["cost"] == 0.0  # _priced_nodes reprices a $0 node under "$"


def test_csv_groups_by_day_and_project_when_no_session_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "copilot.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "project"],
            [
                ["2026-06-18T10:00:00Z", "gpt-4o", 100, 10, "alpha"],
                ["2026-06-18T11:00:00Z", "gpt-4o", 200, 20, "alpha"],  # same day+project
                ["2026-06-18T12:00:00Z", "gpt-4o", 50, 5, "beta"],  # different project
                ["2026-06-19T09:00:00Z", "gpt-4o", 70, 7, "alpha"],  # different day
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        workflows = store.workflows()
        # one synthetic session per (date, project): (18,alpha) (18,beta) (19,alpha)
        assert len(workflows) == 3
        alpha18 = next(w for w in workflows if w.directory == "alpha" and "06-18" in w.created_at)
        assert alpha18.total_tokens == 100 + 10 + 200 + 20  # both rows folded together


def test_csv_credits_column_prices_as_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "copilot.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "credits", "session_id"],
            [
                ["2026-06-18T10:00:00Z", "claude-opus-4-5", 10000, 2000, 150, "m1"],
                ["2026-06-18T10:10:00Z", "claude-opus-4-5", 3000, 500, 40, "m1"],
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        assert store.records_cost is True  # a populated cost column -> metered
        w = store.workflows()[0]
        assert w.total_cost == round((150 + 40) * 0.01, 6)  # credits x $0.01 = $1.90
        assert w.unpriced_tokens == 0  # metered rows are not re-estimated under "$"
        row = store.model_breakdown()[0]
        assert row["unpriced_input"] == 0 and row["unpriced_output"] == 0


def test_csv_keeps_cost_only_rows_as_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "copilot.csv")
        # A row logging only a cost (no token counts) is still real spend: records_cost
        # probes True from it, so dropping the row would show a $0 metered source.
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "credits", "session_id"],
            [
                ["2026-06-18T10:00:00Z", "gpt-4o", 1000, 100, "", "s1"],
                ["2026-06-18T11:00:00Z", "claude-opus-4-5", "", "", 75, "s2"],
                ["2026-06-18T12:00:00Z", "", "", "", "", "s3"],  # truly empty -> skipped
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        assert store.records_cost is True
        workflows = store.workflows()
        assert {w.id for w in workflows} == {"s1", "s2"}  # s3 stays dropped
        s2 = next(w for w in workflows if w.id == "s2")
        assert s2.total_cost == round(75 * 0.01, 6)  # credits x $0.01 = $0.75
        assert s2.total_tokens == 0 and s2.unpriced_tokens == 0


def test_csv_tolerates_header_aliases_and_epoch_timestamps():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "copilot.csv")
        # alias headers (Time / Model Name / Input / Output) and an epoch-seconds stamp
        _write_csv(
            path,
            ["Time", "Model Name", "Input", "Output", "session"],
            [["1750240800", "gpt-4o", 1000, 100, "e1"]],
        )
        store = ot.CsvStore(path, _csv_args())
        w = store.workflows()[0]
        assert w.id == "e1"
        assert w.created_at.startswith("2025-06-18")  # epoch parsed
        assert w.total_tokens == 1100
        assert store.model_breakdown()[0]["model_name"] == "openai/gpt-4o"


def test_csv_tolerates_missing_empty_and_garbage_files():
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "nope.csv")
        store = ot.CsvStore(missing, _csv_args())
        assert store.records_cost is False
        assert store.workflows() == []  # never crashes on a missing file

        empty = os.path.join(tmp, "empty.csv")
        open(empty, "w").close()
        assert ot.CsvStore(empty, _csv_args()).workflows() == []

        garbage = os.path.join(tmp, "garbage.csv")
        _write_csv(garbage, ["a", "b", "c"], [["1", "2", "3"]])  # no usable columns
        assert ot.CsvStore(garbage, _csv_args()).workflows() == []


def test_csv_joins_the_source_cycle_and_has_no_resume_command():
    with tempfile.TemporaryDirectory() as tmp:
        oc_db = os.path.join(tmp, "opencode.db")
        open(oc_db, "w").close()
        csv_path = os.path.join(tmp, "requests.csv")
        _write_csv(
            csv_path,
            ["timestamp", "model", "input_tokens", "output_tokens"],
            [["2026-06-18T10:00:00Z", "gpt-4o", 100, 10]],
        )
        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": oc_db,
                "claude_dir": os.path.join(tmp, "no-claude"),
                "codex_dir": os.path.join(tmp, "no-codex"),
                "hermes_db": os.path.join(tmp, "no-hermes.db"),
                "csv": csv_path,
                "demo": False,
            },
        )()
        assert ot.available_sources(args) == ["opencode", "csv"]
        assert ot.sources.source_cycle(args) == ["opencode", "csv", "all"]
        # no saved pref and >=2 sources present -> auto merges them (no --source needed)
        assert ot.resolve_source(args, {}) == "all"
        store, _ = ot.sources.make_store(args, "csv")
        assert isinstance(getattr(store, "_store", store), ot.CsvStore)  # unwrap CachedStore

        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "opencode"
        assert app.next_source_name() == "CSV"

        # A CSV/Copilot source has no CLI resume, so L produces no command (never crashes)
        wf = workflow("s1", "2026-06-01 12:00:00", title="t", directory="/tmp/proj")
        wf.source = "CSV"
        assert app.resume_command(wf) is None


def test_jsonl_store_splits_cache_prefixes_providers_and_supports_turns():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                # s1: two requests on one prompt, then a third on a new prompt
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "s1",
                    "request_id": "r1",
                    "model": "claude-sonnet-4",
                    "prompt": "refactor auth",
                    "input_tokens": 12000,
                    "cached_tokens": 4000,
                    "output_tokens": 800,
                },
                {
                    "timestamp": "2026-06-18T10:00:30Z",
                    "session_id": "s1",
                    "request_id": "r2",
                    "model": "claude-sonnet-4",
                    "prompt": "refactor auth",
                    "input_tokens": 9000,
                    "cached_tokens": 8000,
                    "output_tokens": 600,
                },
                {
                    "timestamp": "2026-06-18T10:05:00Z",
                    "session_id": "s1",
                    "request_id": "r3",
                    "model": "gpt-4o",
                    "prompt": "add tests",
                    "input_tokens": 5000,
                    "output_tokens": 300,
                },
                {
                    "timestamp": "2026-06-17T09:00:00Z",
                    "session_id": "s2",
                    "request_id": "r4",
                    "model": "gemini-2.5-pro",
                    "input_tokens": 2000,
                    "output_tokens": 150,
                },
            ],
        )
        store = ot.JsonlStore(path, _jsonl_args())
        assert store.records_cost is False  # no cost -> subscription-style
        workflows = store.workflows()
        assert {w.id for w in workflows} == {"s1", "s2"}
        s1 = next(w for w in workflows if w.id == "s1")
        assert s1.source == "JSONL"
        assert s1.subagents == 0  # no subagent tree
        assert s1.total_cost == 0.0
        # uncached+cached+output per request: 12800 + 9600 + 5300
        assert s1.total_tokens == s1.unpriced_tokens == 27700
        assert s1.title == "refactor auth"  # title seeds from the first prompt

        rows = {r["model_name"]: r for r in store.model_breakdown() if r["root_id"] == "s1"}
        assert set(rows) == {"anthropic/claude-sonnet-4", "openai/gpt-4o"}
        cl = rows["anthropic/claude-sonnet-4"]
        assert cl["input"] == 9000 and cl["cache_read"] == 12000  # cached split out of input

        # Turns: chronological, grouped by the owning prompt (consecutive same text)
        assert store.supports_turns("s1") is True
        turns = store.message_timeline("s1")
        assert [t["tokens_total"] for t in turns] == [12800, 9600, 5300]
        assert [t["prompt_title"] for t in turns] == ["refactor auth", "refactor auth", "add tests"]
        assert turns[0]["prompt_id"] == turns[1]["prompt_id"] != turns[2]["prompt_id"]
        assert all(t["depth"] == 0 and t["agent"] == "-" for t in turns)
        # time is the canonical local "YYYY-MM-DD HH:MM:SS" the renderer slices
        assert re.match(r"2026-06-18 \d\d:\d\d:\d\d$", turns[0]["time"])


def test_jsonl_cost_and_credits_price_as_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "m1",
                    "model": "claude-opus-4-5",
                    "input_tokens": 10000,
                    "output_tokens": 2000,
                    "credits": 150,
                },
                {
                    "timestamp": "2026-06-18T10:10:00Z",
                    "session_id": "m1",
                    "model": "claude-opus-4-5",
                    "input_tokens": 3000,
                    "output_tokens": 500,
                    "cost_usd": 0.40,
                },
            ],
        )
        store = ot.JsonlStore(path, _jsonl_args())
        assert store.records_cost is True
        w = store.workflows()[0]
        assert w.total_cost == round(150 * 0.01 + 0.40, 6)  # credits x $0.01 + USD = $1.90
        assert w.unpriced_tokens == 0  # metered rows aren't re-estimated under "$"
        row = store.model_breakdown()[0]
        assert row["unpriced_input"] == 0 and row["unpriced_output"] == 0


def test_jsonl_keeps_cost_only_lines_as_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        # A line logging only a cost (no token counts) is still real spend: records_cost
        # probes True from it, so dropping the line would show a $0 metered source.
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "s1",
                    "model": "gpt-4o",
                    "input_tokens": 100,
                    "output_tokens": 10,
                },
                {
                    "timestamp": "2026-06-18T11:00:00Z",
                    "session_id": "s2",
                    "model": "claude-opus-4-5",
                    "cost_usd": 0.5,
                },
                {"timestamp": "2026-06-18T12:00:00Z", "session_id": "s3"},  # empty -> skipped
            ],
        )
        store = ot.JsonlStore(path, _jsonl_args())
        assert store.records_cost is True
        workflows = store.workflows()
        assert {w.id for w in workflows} == {"s1", "s2"}  # s3 stays dropped
        s2 = next(w for w in workflows if w.id == "s2")
        assert s2.total_cost == 0.5
        assert s2.total_tokens == 0 and s2.unpriced_tokens == 0


def test_jsonl_dedupes_request_id_and_synthesizes_sessions():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        # r1 appears twice (regenerated file) -> counted once; a junk line is skipped;
        # rows with no session_id group into one synthetic session per (date, project).
        with open(path, "w") as fh:
            fh.write(
                json.dumps(
                    {
                        "timestamp": "2026-06-18T10:00:00Z",
                        "session_id": "s1",
                        "request_id": "r1",
                        "model": "gpt-4o",
                        "input_tokens": 100,
                        "output_tokens": 10,
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "timestamp": "2026-06-18T10:00:30Z",
                        "session_id": "s1",
                        "request_id": "r1",
                        "model": "gpt-4o",
                        "input_tokens": 100,
                        "output_tokens": 10,
                    }
                )
                + "\n"
            )
            fh.write("this is not json\n")
            fh.write(
                json.dumps(
                    {
                        "timestamp": "2026-06-18T11:00:00Z",
                        "project": "alpha",
                        "model": "gpt-4o",
                        "input_tokens": 50,
                        "output_tokens": 5,
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "timestamp": "2026-06-19T09:00:00Z",
                        "project": "alpha",
                        "model": "gpt-4o",
                        "input_tokens": 70,
                        "output_tokens": 7,
                    }
                )
                + "\n"
            )
        store = ot.JsonlStore(path, _jsonl_args())
        workflows = store.workflows()
        s1 = next(w for w in workflows if w.id == "s1")
        assert s1.total_tokens == 110  # r1 counted once, not 220
        synthetic = [w for w in workflows if w.id.startswith("jsonl:")]
        assert len(synthetic) == 2  # (06-18, alpha) and (06-19, alpha)


def test_jsonl_detail_turns_groups_and_reprices_under_dollar():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "s1",
                    "request_id": "r1",
                    "model": "claude-sonnet-4",
                    "prompt": "refactor auth",
                    "input_tokens": 8000,
                    "output_tokens": 800,
                },
                {
                    "timestamp": "2026-06-18T10:05:00Z",
                    "session_id": "s1",
                    "request_id": "r2",
                    "model": "claude-sonnet-4",
                    "prompt": "add tests",
                    "input_tokens": 5000,
                    "output_tokens": 300,
                },
            ],
        )
        app = ot.App(ot.JsonlStore(path, _jsonl_args()), args())
        rnd = ot.Renderer(app)
        wf = app.loaded[0]
        # A token-only source defaults the "$" estimate ON, so the two $0 turns are
        # repriced at list price -> non-zero total, grouped under their prompts.
        assert app.show_api_prices is True
        priced = rnd.detail_turns(wf, 96)
        joined = "\n".join(priced)
        assert priced[0].startswith("# Turns — 2 turns, $") and "$0.00 total" not in priced[0]
        assert "▸ refactor auth" in joined and "▸ add tests" in joined
        # Toggle the estimate off -> only recorded cost ($0) counts.
        app.show_api_prices = False
        assert rnd.detail_turns(wf, 96)[0] == "# Turns — 2 turns, $0.00 total"


def test_jsonl_path_routing_and_source_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        jl_path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            jl_path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "model": "gpt-4o",
                    "input_tokens": 100,
                    "output_tokens": 10,
                }
            ],
        )
        # All three forms select the jsonl source and fill --jsonl.
        for argv in ([jl_path], ["--jsonl", jl_path], ["--source", "jsonl", jl_path]):
            a = _parse(argv)
            assert a.source == "jsonl", argv
            assert a.jsonl == jl_path, argv
        # Bare `opentab` is unchanged: auto, jsonl auto-discovered at the default path.
        bare = _parse([])
        assert bare.source == "auto"
        assert bare.jsonl == ot.DEFAULT_JSONL_PATH

        oc_db = os.path.join(tmp, "opencode.db")
        open(oc_db, "w").close()
        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": oc_db,
                "claude_dir": os.path.join(tmp, "no-claude"),
                "codex_dir": os.path.join(tmp, "no-codex"),
                "hermes_db": os.path.join(tmp, "no-hermes.db"),
                "csv": os.path.join(tmp, "no.csv"),
                "jsonl": jl_path,
                "demo": False,
            },
        )()
        assert "jsonl" in ot.available_sources(args)
        store, _ = ot.sources.make_store(args, "jsonl")
        assert isinstance(getattr(store, "_store", store), ot.JsonlStore)  # unwrap CachedStore


def test_csv_turns_timeline_groups_prompts_and_dedupes_requests():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.csv")
        _write_csv(
            path,
            [
                "timestamp",
                "model",
                "input_tokens",
                "output_tokens",
                "session_id",
                "prompt",
                "request_id",
            ],
            [
                ["2026-06-01T10:00:00Z", "gpt-4o", 100, 20, "s1", "fix the bug", "r1"],
                ["2026-06-01T10:05:00Z", "gpt-4o", 50, 10, "s1", "fix the bug", "r2"],
                ["2026-06-01T10:05:00Z", "gpt-4o", 50, 10, "s1", "fix the bug", "r2"],  # dupe
                ["2026-06-01T10:10:00Z", "claude-sonnet-4", 30, 5, "s1", "now the docs", "r3"],
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        w = store.workflows()[0]
        assert w.title == "fix the bug"  # no title column -> first prompt
        assert w.total_tokens == 100 + 20 + 50 + 10 + 30 + 5  # the r2 dupe dropped
        assert store.supports_turns("s1")
        t = store.message_timeline("s1")
        assert len(t) == 3
        assert [r["prompt_title"] for r in t] == ["fix the bug", "fix the bug", "now the docs"]
        assert t[0]["prompt_id"] == "fix the bug"  # no explicit id -> the text groups
        assert t[2]["model_name"] == "anthropic/claude-sonnet-4"


def test_csv_tool_column_gates_the_tab_and_splits_rows():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "session_id", "tool"],
            [
                ["2026-06-01T10:00:00Z", "gpt-4o", 100, 20, "s1", "Bash;Read"],
                ["2026-06-01T10:05:00Z", "gpt-4o", 50, 10, "s1", ""],
                ["2026-06-01T10:10:00Z", "gpt-4o", 30, 5, "s1", "Bash"],
            ],
        )
        store = ot.CsvStore(path, _csv_args())
        store.workflows()
        assert store.supports_tools("s1")
        rows = {r["tool"]: r for r in store.tool_breakdown("s1")}
        assert rows["Bash"]["tokens_total"] == 60 + 35  # half of row 1 + all of row 3
        assert rows["Bash"]["calls"] == 2 and rows["Read"]["tokens_total"] == 60
        # A log without the tool column hides the tab instead of showing it empty.
        bare = os.path.join(tmp, "bare.csv")
        _write_csv(
            bare,
            ["timestamp", "model", "input_tokens", "output_tokens", "session_id"],
            [["2026-06-01T10:00:00Z", "gpt-4o", 100, 20, "s2"]],
        )
        store2 = ot.CsvStore(bare, _csv_args())
        store2.workflows()
        assert not store2.supports_tools("s2")


def test_jsonl_tool_field_accepts_a_list_or_delimited_string():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-01T10:00:00Z",
                    "model": "gpt-4o",
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "session_id": "s1",
                    "tools": ["Bash", "Read"],
                },
                {
                    "timestamp": "2026-06-01T10:05:00Z",
                    "model": "gpt-4o",
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "session_id": "s1",
                    "tool": "Edit",
                },
            ],
        )
        store = ot.JsonlStore(path, _csv_args())
        store.workflows()
        assert store.supports_tools("s1")
        rows = {r["tool"]: r for r in store.tool_breakdown("s1")}
        assert rows["Bash"]["tokens_total"] == 60 and rows["Read"]["tokens_total"] == 60
        assert rows["Edit"]["tokens_total"] == 60


def test_csv_context_curve_only_with_real_session_ids():
    # A synthetic (date, project) session interleaves unrelated conversations, so
    # its "curve" would be noise with fake compactions -- no Context tab. Rows
    # with a real session_id keep it.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "log.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "session_id"],
            [["2026-06-18T10:00:00Z", "gpt-4o", 5000, 300, "s1"]],
        )
        store = ot.CsvStore(path, _csv_args())
        store.workflows()
        assert store.supports_context_curve("s1") is True
        bare = os.path.join(tmp, "bare.csv")
        _write_csv(
            bare,
            ["timestamp", "model", "input_tokens", "output_tokens"],
            [["2026-06-18T10:00:00Z", "gpt-4o", 5000, 300]],
        )
        store2 = ot.CsvStore(bare, _csv_args())
        (w,) = store2.workflows()
        assert w.id.startswith("csv:")
        assert store2.supports_context_curve(w.id) is False
        assert store2.supports_turns(w.id) is True  # Turns stays
