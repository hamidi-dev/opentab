"""RemoteStore + build_export: consolidating other machines' exported summaries."""

import contextlib
import io
import json
import os
import tempfile

import opentab as ot

from tests._support import _parse, workflow


def _summary(label, workflows, model_breakdown=(), records_cost=True):
    # A summary payload as build_export would emit it (workflows are Workflow rows,
    # serialized here the way stores.remote.build_export does).
    from dataclasses import asdict

    return {
        "opentab_export": 1,
        "label": label,
        "records_cost": records_cost,
        "workflows": [asdict(w) for w in workflows],
        "model_breakdown": [dict(r) for r in model_breakdown],
    }


def _write(dir_, name, payload):
    path = os.path.join(dir_, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


class _FakeExportStore:
    records_cost = False

    def __init__(self, workflows, model_breakdown, nodes=None):
        self._w = workflows
        self._m = model_breakdown
        self._n = nodes or {}

    def workflows(self):
        return list(self._w)

    def model_breakdown(self):
        return list(self._m)

    def workflow_nodes(self, workflow_id):
        return list(self._n.get(workflow_id, ()))


def _node(depth, agent, title, model, cost, tot):
    return {
        "depth": depth,
        "agent": agent,
        "title": title,
        "created_at": "2026-07-15 10:00:00",
        "model_name": model,
        "cost": cost,
        "tokens_input": tot // 2,
        "tokens_output": tot // 4,
        "tokens_reasoning": 0,
        "tokens_cache_read": tot // 4,
        "tokens_cache_write": 0,
        "tokens_total": tot,
    }


class _FakeExtrasStore(_FakeExportStore):
    # A backend that also implements the lazy per-session extras, so build_export ships
    # them (export v2) and RemoteStore reads them back.
    def __init__(self, workflows, model_breakdown, turns=None, tools=None, context=None, curve=()):
        super().__init__(workflows, model_breakdown)
        self._turns = turns or {}
        self._tools = tools or {}
        self._ctx = context or {}
        self._curve = set(curve)

    def message_timeline(self, wid):
        return list(self._turns.get(wid, ()))

    def tool_breakdown(self, wid):
        return list(self._tools.get(wid, ()))

    def context_breakdown(self, wid):
        return list(self._ctx.get(wid, ()))

    def supports_turns(self, wid):
        return wid in self._turns

    def supports_tools(self, wid):
        return wid in self._tools

    def supports_context(self, wid):
        return wid in self._ctx

    def supports_context_curve(self, wid):
        return wid in self._curve


def _turn(prompt="do the port", pid="p1"):
    return {
        "time": "2026-07-15 10:00:00",
        "model_name": "openai/gpt-5.6",
        "prompt_title": prompt,
        "prompt_full": prompt,
        "prompt_id": pid,
        "cost": 6.5,
        "tokens_total": 1000,
        "input": 500,
        "output": 250,
        "reasoning": 0,
        "cache_read": 250,
        "cache_write": 0,
    }


def test_v2_export_round_trips_turns_tools_and_context():
    # The v2 addition: the lazy per-session extras travel too, so a pulled session's
    # Turns/Tools/Context tabs are real rather than hidden.
    wfs = [workflow("s1", "2026-07-15 10:00:00", title="port", cost=6.5, tokens=1000)]
    turns = {"s1": [_turn()]}
    tools = {
        "s1": [{"tool": "Bash", "model_name": "openai/gpt-5.6", "cost": 3.0, "tokens_total": 500}]
    }
    context = {"s1": [{"category": "tool_result", "kind": "Bash", "est_tokens": 400}]}
    store = _FakeExtrasStore(wfs, [], turns=turns, tools=tools, context=context, curve={"s1"})
    payload = ot.build_export(store, "laptop", "2026-07-18T00:00:00", "9.9")
    assert payload["opentab_export"] == 2
    assert payload["turns"]["s1"] and payload["tools"]["s1"] and payload["context"]["s1"]
    assert payload["curve_ok"] == ["s1"]

    with tempfile.TemporaryDirectory() as d:
        _write(d, "laptop.json", payload)
        rs = ot.RemoteStore(d, _parse([]))
        assert rs.supports_turns("s1") and rs.supports_tools("s1")
        assert rs.supports_context("s1") and rs.supports_context_curve("s1")
        assert rs.message_timeline("s1")[0]["prompt_title"] == "do the port"
        assert rs.tool_breakdown("s1")[0]["tool"] == "Bash"
        assert rs.context_breakdown("s1")[0]["est_tokens"] == 400
        assert not rs.supports_turns("nope")  # a session the export never carried stays hidden


class _BatchTurnsStore(_FakeExtrasStore):
    # A backend exposing the whole-corpus Turns batch (like OpenCode's
    # message_timeline_all) alongside the per-session path, so a test can prove
    # build_export prefers the batch and never runs the slow per-session query for a
    # session the batch already covers.
    def __init__(self, *a, batch=None, **k):
        super().__init__(*a, **k)
        self._batch = batch or {}
        self.per_session_calls = []

    def message_timeline(self, wid):
        self.per_session_calls.append(wid)
        return super().message_timeline(wid)

    def message_timeline_all(self):
        return {k: list(v) for k, v in self._batch.items()}


def test_build_export_prefers_the_turns_batch_and_skips_covered_sessions():
    # The export uses a backend's whole-corpus Turns batch (OpenCode's
    # message_timeline_all, ~100x cheaper than its per-session recursive-CTE scan) and
    # must NOT re-run the slow per-session query for any session the batch owns -- even an
    # all-aborted one the batch yields no rows for (else it re-pays the very cost it dodged).
    wfs = [
        workflow("s1", "2026-07-15 10:00:00", cost=1.0),
        workflow("s2", "2026-07-15 11:00:00", cost=0.0),  # aborted: not in the batch result
    ]
    store = _BatchTurnsStore(
        wfs, [], turns={"s1": [_turn()], "s2": [_turn()]}, batch={"s1": [_turn()]}
    )
    payload = ot.build_export(store, "box")
    assert set(payload["turns"]) == {"s1"}  # from the batch; s2 empty -> absent
    assert payload["turns"]["s1"][0]["prompt_title"] == "do the port"
    assert store.per_session_calls == []  # the slow per-session path never ran


class _RaisingBatchStore(_FakeExtrasStore):
    # A backend whose Turns batch RAISES (a mid-export sqlite error). build_export must
    # fall back to the per-session path, not silently drop the session's Turns.
    def message_timeline_all(self):
        raise RuntimeError("batch blew up")


def test_build_export_falls_back_to_per_session_when_the_batch_raises():
    wfs = [workflow("s1", "2026-07-15 10:00:00", cost=1.0)]
    store = _RaisingBatchStore(wfs, [], turns={"s1": [_turn()]})
    payload = ot.build_export(store, "box")
    assert payload["turns"]["s1"][0]["prompt_title"] == "do the port"  # recovered, not dropped


def test_machine_stats_scrambles_labels_under_demo():
    # --timings joins per-box bytes to a machine by LABEL; under --demo the workflow rows
    # carry demo-scrambled machine names, so machine_stats must scramble to match (else a
    # pulled box shows 0 B). The label agrees with what workflows() stamps.
    wfs = [workflow("a", "2026-07-15 10:00:00", cost=1.0)]
    with tempfile.TemporaryDirectory() as d:
        _write(d, "box.json", _summary("realbox", wfs))
        rs = ot.RemoteStore(d, _parse(["--demo"]))
        labels = {s["label"] for s in rs.machine_stats()}
        assert "realbox" not in labels and labels == {ot.demo_machine("realbox")}
        assert {w.machine for w in rs.workflows()} == labels  # joins cleanly in the table


def test_malformed_extras_rows_normalize_instead_of_crashing():
    # Codex: a hostile/partial summary -- {"turns": {"s1": [{}]}} -- makes supports_turns
    # true, so drill-in must render zeros, not KeyError. Every extras row is cleaned on load
    # (the _clean_node treatment), so the renderers' bracket-accessed fields always exist.
    wfs = [workflow("s1", "2026-07-15 10:00:00", cost=1.0)]
    payload = _summary("box", wfs)
    payload["opentab_export"] = 2
    payload["turns"] = {"s1": [{}, {"cost": "oops", "tokens_total": None, "prompt_title": 5}]}
    payload["tools"] = {"s1": [{}]}
    payload["context"] = {"s1": [{}]}
    payload["curve_ok"] = ["s1"]
    with tempfile.TemporaryDirectory() as d:
        _write(d, "box.json", payload)
        rs = ot.RemoteStore(d, _parse([]))
        assert rs.supports_turns("s1") and rs.supports_tools("s1") and rs.supports_context("s1")
        turns = rs.message_timeline("s1")
        assert len(turns) == 2
        for r in turns:  # every field the Turns renderer brackets, with a safe default
            assert isinstance(r["cost"], float) and isinstance(r["tokens_total"], int)
            for f in (
                "time",
                "model_name",
                "agent",
                "depth",
                "input",
                "output",
                "reasoning",
                "cache_read",
                "cache_write",
                "prompt_id",
                "prompt_title",
            ):
                assert f in r
        tool = rs.tool_breakdown("s1")[0]
        assert tool["calls"] == 0 and tool["tool"] == "?" and isinstance(tool["cost"], float)
        assert rs.context_breakdown("s1")[0]["est_tokens"] == 0


def test_machine_stats_reports_per_machine_sessions_and_bytes():
    # Feeds --timings' per-machine breakdown: sessions kept (deduped) + summary file size on
    # disk, read off the loaded state with no re-parse. Bytes are where the v2 extras show up.
    big = [workflow("a", "2026-07-15 10:00:00", cost=2.0), workflow("b", "2026-07-16 10:00:00")]
    with tempfile.TemporaryDirectory() as d:
        _write(d, "big.json", _summary("big", big))
        _write(d, "small.json", _summary("small", [workflow("c", "2026-07-15 10:00:00", cost=0.5)]))
        rs = ot.RemoteStore(d, _parse([]))
        stats = {s["label"]: s for s in rs.machine_stats()}
        assert stats["big"]["sessions"] == 2 and stats["small"]["sessions"] == 1
        assert stats["big"]["bytes"] > stats["small"]["bytes"] > 0
        assert set(rs._files()) == {os.path.join(d, "big.json"), os.path.join(d, "small.json")}


def test_v1_summary_still_loads_without_the_extras():
    # A v1 summary (no turns/tools/context) loads fine; those tabs just stay hidden.
    wfs = [workflow("s1", "2026-07-15 10:00:00", cost=1.0)]
    with tempfile.TemporaryDirectory() as d:
        _write(d, "old.json", _summary("old-box", wfs))  # _summary emits v1
        rs = ot.RemoteStore(d, _parse([]))
        assert [w.id for w in rs.workflows()] == ["s1"]
        assert not rs.supports_turns("s1") and not rs.supports_tools("s1")
        assert rs.message_timeline("s1") == [] and rs.context_breakdown("s1") == []


def test_remote_store_leaves_extras_raw_for_the_app_to_demo():
    # RemoteStore returns raw Turns rows even in demo mode -- the App re-anonymises them
    # lazily (App._scale_demo_turns), so demoing here too would double-scale / it's the
    # App's job. The raw prompt survives the store; the App is what hides it.
    wfs = [workflow("s1", "2026-07-15 10:00:00", cost=1.0)]
    store = _FakeExtrasStore(wfs, [], turns={"s1": [_turn(prompt="secret plan")]}, curve={"s1"})
    payload = ot.build_export(store, "laptop")
    with tempfile.TemporaryDirectory() as d:
        _write(d, "laptop.json", payload)
        rs = ot.RemoteStore(d, _parse(["--demo"]))
        assert rs.demo is True
        assert rs.message_timeline("s1")[0]["prompt_title"] == "secret plan"


def test_build_export_round_trips_through_remote_store():
    # build_export serializes a machine's rollup; RemoteStore reads it back with the
    # same sessions, model rows, and records_cost -- it is the cache payload reversed.
    wfs = [
        workflow("s1", "2026-07-15 10:00:00", title="rust port", cost=6.5, tokens=1000),
        workflow("s2", "2026-07-16 09:00:00", title="sidebar", cost=0.0, tokens=500),
    ]
    models = [{"root_id": "s1", "model_name": "openai/gpt-5.6", "cost": 6.5, "tokens_total": 1000}]
    payload = ot.build_export(_FakeExportStore(wfs, models), "laptop", "2026-07-18T00:00:00", "9.9")

    assert payload["opentab_export"] == 2
    assert payload["label"] == "laptop" and payload["opentab_version"] == "9.9"
    assert payload["records_cost"] is False

    with tempfile.TemporaryDirectory() as d:
        _write(d, "laptop.json", payload)
        store = ot.RemoteStore(d, _parse([]))
        got = store.workflows()
        assert [w.id for w in got] == ["s1", "s2"]  # sorted by cost desc
        assert store.model_breakdown() == models
        assert store.records_cost is False
        assert store.summary(got)["cost"] == 6.5


def test_remote_store_stamps_the_machine_label():
    # Every loaded session is tagged with the exporting machine, from the payload's
    # `label`; a payload with no label falls back to the file's basename.
    tagged = workflow("a", "2026-07-15 10:00:00")
    tagged.source = "Codex"  # the backend tag rides on each row, orthogonal to machine
    with tempfile.TemporaryDirectory() as d:
        _write(d, "laptop.json", _summary("laptop", [tagged]))
        unlabeled = _summary("", [workflow("b", "2026-07-15 11:00:00")])
        unlabeled.pop("label")
        _write(d, "desktop.json", unlabeled)
        by_id = {w.id: w for w in ot.RemoteStore(d, _parse([])).workflows()}
        assert by_id["a"].machine == "laptop"
        assert by_id["b"].machine == "desktop"  # basename fallback
        assert by_id["a"].source == "Codex"  # the backend tag survives untouched


def test_remote_store_merges_multiple_machines_sorted_by_cost():
    with tempfile.TemporaryDirectory() as d:
        _write(
            d,
            "laptop.json",
            _summary("laptop", [workflow("cheap", "2026-07-15 10:00:00", cost=1.0)]),
        )
        _write(
            d,
            "server.json",
            _summary("server", [workflow("dear", "2026-07-16 10:00:00", cost=9.0)]),
        )
        got = ot.RemoteStore(d, _parse([])).workflows()
        assert [w.id for w in got] == ["dear", "cheap"]
        assert {w.machine for w in got} == {"laptop", "server"}


def test_remote_store_dedups_a_session_seen_on_two_machines():
    # A synced/rotated session can appear in two summaries; keep it once and do NOT
    # double-count its model rows (they follow the workflow that was kept).
    row = {"root_id": "dup", "model_name": "m", "cost": 2.0, "tokens_total": 100}
    with tempfile.TemporaryDirectory() as d:
        _write(
            d, "a.json", _summary("a", [workflow("dup", "2026-07-15 10:00:00", cost=2.0)], [row])
        )
        _write(
            d, "b.json", _summary("b", [workflow("dup", "2026-07-15 10:00:00", cost=2.0)], [row])
        )
        store = ot.RemoteStore(d, _parse([]))
        assert [w.id for w in store.workflows()] == ["dup"]
        assert len(store.model_breakdown()) == 1  # not two


def test_remote_store_skips_broken_or_shapeless_summaries():
    # A file it can't parse (or that isn't a summary) is skipped, never fatal -- the
    # good machines still load. Same forgiveness as notes.json.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "corrupt.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        _write(d, "notasummary.json", {"hello": "world"})
        _write(d, "good.json", _summary("good", [workflow("ok", "2026-07-15 10:00:00")]))
        got = ot.RemoteStore(d, _parse([])).workflows()
        assert [w.id for w in got] == ["ok"]


def test_remote_store_is_forward_compatible_with_unknown_fields():
    # A summary written by a newer opentab (extra Workflow fields) must load, not crash.
    payload = _summary("new", [workflow("z", "2026-07-15 10:00:00")])
    payload["workflows"][0]["future_field"] = {"whatever": 1}
    with tempfile.TemporaryDirectory() as d:
        _write(d, "new.json", payload)
        got = ot.RemoteStore(d, _parse([])).workflows()
        assert [w.id for w in got] == ["z"]


def test_remote_store_records_cost_is_and_across_machines():
    # One metered machine + one subscription machine -> the merged view reports no
    # recorded cost (drives the "$"/ESTIMATED nudges), like CombinedStore.
    with tempfile.TemporaryDirectory() as d:
        _write(
            d,
            "metered.json",
            _summary("m", [workflow("a", "2026-07-15 10:00:00")], records_cost=True),
        )
        _write(
            d, "sub.json", _summary("s", [workflow("b", "2026-07-15 10:00:00")], records_cost=False)
        )
        assert ot.RemoteStore(d, _parse([])).records_cost is False


def test_remote_store_hides_the_drill_in_tabs():
    # Summaries carry no transcripts, so the per-session extras are empty and their
    # supports_* gates hide the Turns/Tools/Context tabs for remote sessions.
    with tempfile.TemporaryDirectory() as d:
        _write(d, "z.json", _summary("z", [workflow("a", "2026-07-15 10:00:00")]))
        store = ot.RemoteStore(d, _parse([]))
        assert store.workflow_nodes("a") == []
        assert store.supports_turns("a") is False
        assert store.supports_tools("a") is False
        assert store.supports_context_curve("a") is False


def test_remote_store_demo_anonymizes_titles_but_keeps_ids():
    args = _parse(["--demo"])
    with tempfile.TemporaryDirectory() as d:
        _write(
            d,
            "z.json",
            _summary("z", [workflow("realid", "2026-07-15 10:00:00", title="Acme merger")]),
        )
        w = ot.RemoteStore(d, args).workflows()[0]
        assert w.id == "realid"  # ids stay real everywhere in demo (notes target them)
        assert w.title != "Acme merger" and w.directory != "/tmp/project"


def test_remote_store_empty_directory_loads_nothing():
    with tempfile.TemporaryDirectory() as d:
        store = ot.RemoteStore(d, _parse([]))
        assert store.workflows() == [] and store.machines == []


def test_build_export_ships_the_subagent_tree_only_for_delegating_sessions():
    # The tree rides along, but ONLY for sessions that delegated (w.subagents) -- a solo
    # session exports no nodes, keeping the summary small.
    deleg = workflow("root", "2026-07-15 10:00:00", cost=12.0)
    deleg.subagents = 2
    solo = workflow("solo", "2026-07-16 10:00:00", cost=1.0)
    tree = [
        _node(0, "-", "root", "openai/gpt-5.6", 12.0, 3000),
        _node(1, "docs", "write docs", "anthropic/claude-haiku-4.5", 0.0, 800),
    ]
    store = _FakeExportStore([deleg, solo], [], nodes={"root": tree})
    payload = ot.build_export(store, "laptop")
    assert list(payload["nodes"]) == ["root"]  # solo delegated nothing -> no node rows
    assert len(payload["nodes"]["root"]) == 2


def test_remote_store_serves_the_exported_subagent_tree():
    # A remote session's Subagents tab is real, not empty: workflow_nodes returns the
    # exported tree (and a session that shipped none still returns []).
    payload = _summary("laptop", [workflow("root", "2026-07-15 10:00:00", cost=12.0)])
    payload["nodes"] = {
        "root": [
            _node(1, "docs", "write docs", "anthropic/claude-haiku-4.5", 0.0, 800),
            _node(1, "impl", "implement", "anthropic/claude-opus-4.6", 0.0, 1500),
        ]
    }
    with tempfile.TemporaryDirectory() as d:
        _write(d, "laptop.json", payload)
        store = ot.RemoteStore(d, _parse([]))
        nodes = store.workflow_nodes("root")
        assert [n["agent"] for n in nodes] == ["docs", "impl"]
        assert nodes[0]["tokens_total"] == 800
        assert store.workflow_nodes("missing") == []  # unknown session -> empty


def test_remote_store_demo_anonymizes_the_exported_nodes():
    # Under demo the node titles/models are scrambled and tokens scaled, like the leaf
    # stores' _demo_node -- so a fleet Subagents tab leaks nothing on a shared screen.
    payload = _summary("laptop", [workflow("root", "2026-07-15 10:00:00", cost=12.0)])
    payload["nodes"] = {
        "root": [_node(1, "docs", "write docs", "anthropic/claude-opus-4.6", 5.0, 800)]
    }
    with tempfile.TemporaryDirectory() as d:
        _write(d, "laptop.json", payload)
        node = ot.RemoteStore(d, _parse(["--demo"])).workflow_nodes("root")[0]
        assert node["title"] != "write docs"  # scrambled
        assert node["agent"] == "docs"  # agent role is structural, kept (like the leaf stores)
        assert node["tokens_total"] != 800  # scaled by the hidden demo factor


def test_remote_store_survives_a_malformed_nodes_block():
    # A non-dict `nodes`, or node lists with junk, must load (empty tree), never crash.
    payload = _summary("laptop", [workflow("root", "2026-07-15 10:00:00")])
    payload["nodes"] = {"root": ["not a dict", 7], "ghost": [_node(1, "x", "y", "m", 0.0, 1)]}
    with tempfile.TemporaryDirectory() as d:
        _write(d, "laptop.json", payload)
        store = ot.RemoteStore(d, _parse([]))
        assert store.workflow_nodes("root") == []  # only the junk rows were dropped
        assert store.workflow_nodes("ghost") == []  # ghost isn't a kept session


def test_remote_store_normalizes_partial_or_garbage_nodes():
    # A partial/garbage node dict (a crafted `{}`, a string where a count belongs) is
    # NORMALIZED to the fields the Subagents tab reads, with defaults + coerced types --
    # so it renders (and demo-scales) instead of crashing with KeyError/TypeError.
    payload = _summary("laptop", [workflow("root", "2026-07-15 10:00:00")])
    payload["nodes"] = {"root": [{}, {"depth": "x", "cost": "nan", "tokens_total": None}]}
    with tempfile.TemporaryDirectory() as d:
        _write(d, "laptop.json", payload)
        node = ot.RemoteStore(d, _parse([])).workflow_nodes("root")[0]
        assert node["depth"] == 0 and node["agent"] == "-" and node["title"] == "(untitled)"
        assert node["cost"] == 0.0 and node["tokens_total"] == 0
        # And the Subagents tab renders it without raising.
        store = ot.RemoteStore(d, _parse([]))
        app = ot.App(store, _parse([]), source_key="remote")
        app.view = "session"
        wf = next(w for w in app.loaded if w.id == "root")
        app.renderer.detail_subagents(wf, 200)  # no KeyError
        ot.RemoteStore(d, _parse(["--demo"])).workflow_nodes("root")  # demo path safe too


def test_remote_store_machine_meta_carries_pulled_niceties_and_the_refresh_key():
    # machine_meta feeds the Machines mode: each pulled box is live=False, keeps its
    # export time/version, and carries the remotes key (the summary FILENAME decoded)
    # so an in-TUI refresh re-pulls exactly it.
    payload = _summary("workstation", [workflow("a", "2026-07-15 10:00:00")])
    payload["exported_at"] = "2026-07-18T09:00:00+00:00"
    payload["opentab_version"] = "1.6.0"
    with tempfile.TemporaryDirectory() as d:
        _write(d, "omv.json", payload)  # filename (remotes key) != label
        meta = ot.RemoteStore(d, _parse([])).machine_meta
        info = meta["workstation"]
        assert info["live"] is False
        assert info["exported_at"] == "2026-07-18T09:00:00+00:00"
        assert info["opentab_version"] == "1.6.0"
        assert info["key"] == "omv"  # decoded from the filename, the handle a refresh uses


def test_remote_store_machine_meta_key_survives_a_percent_encoded_filename():
    # _summary_filename percent-encodes the remotes key; machine_meta must decode the
    # filename back to the exact key so a refresh finds the entry in remotes.json.
    from opentab.cli import _summary_filename

    with tempfile.TemporaryDirectory() as d:
        _write(
            d,
            _summary_filename("mo@host.local"),
            _summary("box", [workflow("a", "2026-07-15 10:00:00")]),
        )
        assert ot.RemoteStore(d, _parse([])).machine_meta["box"]["key"] == "mo@host.local"


def test_remote_store_demo_scrambles_the_machine_name_and_meta_stays_aligned():
    # D must hide the box name too. The scrambled label on the workflows and the
    # machine_meta keys go through the same deterministic demo_machine, so they agree
    # (else the Machines Overview couldn't look up a scrambled box's freshness).
    with tempfile.TemporaryDirectory() as d:
        _write(d, "omv.json", _summary("workstation", [workflow("a", "2026-07-15 10:00:00")]))
        _write(d, "gi.json", _summary("giant", [workflow("b", "2026-07-15 11:00:00")]))
        store = ot.RemoteStore(d, _parse(["--demo"]))
        wf_names = {w.machine for w in store.workflows()}
        assert "workstation" not in wf_names and "giant" not in wf_names  # scrambled
        assert set(store.machine_meta) == wf_names  # keys track the scrambled names 1:1
        assert len(wf_names) == 2  # two boxes stay two boxes (no collision)


def test_machine_tagged_store_meta_is_live_and_scrambles_under_demo():
    from opentab.demo import demo_machine
    from opentab.stores.remote import MachineTaggedStore

    class Leaf:
        demo = False
        source_name = "opencode"

        def workflows(self):
            return [workflow("x", "2026-07-15 10:00:00")]

    live = MachineTaggedStore(Leaf(), "laptop")
    assert live.machine_meta == {
        "laptop": {"live": True, "exported_at": "", "opentab_version": "", "key": ""}
    }
    assert live.workflows()[0].machine == "laptop"

    class DemoLeaf(Leaf):
        demo = True

    demo = MachineTaggedStore(DemoLeaf(), "laptop")
    assert list(demo.machine_meta) == [demo_machine("laptop")]  # local hostname hidden too
    assert demo.workflows()[0].machine == demo_machine("laptop")


def test_combined_machine_meta_merges_and_live_wins():
    from opentab.stores.combined import CombinedStore
    from opentab.stores.remote import MachineTaggedStore

    class Leaf:
        demo = False
        source_name = "opencode"
        records_cost = True

        def __init__(self, wfs):
            self._w = wfs

        def workflows(self):
            return list(self._w)

        def model_breakdown(self):
            return []

    local = MachineTaggedStore(Leaf([workflow("l", "2026-07-15 10:00:00")]), "laptop")
    with tempfile.TemporaryDirectory() as d:
        _write(d, "omv.json", _summary("omv", [workflow("a", "2026-07-15 10:00:00")]))
        remote = ot.RemoteStore(d, _parse([]))
        merged = CombinedStore([local, remote]).machine_meta
        assert merged["laptop"]["live"] is True  # the box you're on
        assert merged["omv"]["live"] is False and merged["omv"]["key"] == "omv"


# --- the --export CLI command -------------------------------------------------


def _csv_machine(dir_):
    path = os.path.join(dir_, "box.csv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "timestamp,model,input_tokens,output_tokens,session_id,project,cost_usd\n"
            "2026-07-15T10:00:00Z,openai/gpt-5.6,800,200,s1,/home/mo/rustport,4.25\n"
            "2026-07-15T11:00:00Z,anthropic/claude-fable-5,1200,400,s2,/home/mo/t3,0\n"
        )
    return path


def test_export_command_writes_a_summary_to_stdout():
    with tempfile.TemporaryDirectory() as d:
        args = _parse(["--csv", _csv_machine(d), "--export", "-", "--label", "laptop"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ot.export_command(args)
        assert rc == 0
        payload = json.loads(buf.getvalue())
        assert payload["label"] == "laptop" and payload["opentab_export"] == 2
        assert len(payload["workflows"]) == 2
        assert payload["exported_at"]  # a timestamp was stamped


def test_export_command_writes_a_file_and_defaults_the_label_to_the_host():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "machine.json")
        args = _parse(["--csv", _csv_machine(d), "--export", out])
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ot.export_command(args)
        assert rc == 0
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        assert payload["label"]  # defaulted to socket.gethostname(), never empty
        # And it reads straight back as a machine in RemoteStore.
        store = ot.RemoteStore(d, _parse([]))
        assert {w.machine for w in store.workflows()} == {payload["label"]}


def test_export_command_emits_an_empty_summary_when_no_sources_present():
    # A machine with no agent data yet must still export a valid (empty) summary, so
    # `opentab --pull` shows it as "0 sessions" rather than erroring on that host.
    orig = ot.sources.available_sources
    ot.sources.available_sources = lambda a: []
    try:
        args = _parse(["--export", "-", "--label", "fresh-box"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ot.export_command(args)
        assert rc == 0
        payload = json.loads(buf.getvalue())
        assert payload["label"] == "fresh-box" and payload["opentab_export"] == 2
        assert payload["workflows"] == [] and payload["model_breakdown"] == []
    finally:
        ot.sources.available_sources = orig


def test_remote_store_survives_a_malformed_model_breakdown():
    # A summary whose model_breakdown isn't a list (corruption/bad producer) must load
    # its workflows, not crash the whole store on a TypeError.
    payload = _summary("bad", [workflow("s1", "2026-07-15 10:00:00")])
    payload["model_breakdown"] = 1  # not a list
    with tempfile.TemporaryDirectory() as d:
        _write(d, "bad.json", payload)
        store = ot.RemoteStore(d, _parse([]))
        assert [w.id for w in store.workflows()] == ["s1"]
        assert store.model_breakdown() == []


def test_remote_store_drops_model_rows_without_a_root_id():
    # The App indexes model rows by row["root_id"]; a row missing it would crash the
    # model scan, so RemoteStore drops any unattributable row.
    payload = _summary("m", [workflow("s1", "2026-07-15 10:00:00")])
    payload["model_breakdown"] = [
        {"root_id": "s1", "model_name": "openai/m", "cost": 1.0, "tokens_total": 100},
        {"model_name": "openai/orphan", "cost": 9.0, "tokens_total": 900},  # no root_id
    ]
    with tempfile.TemporaryDirectory() as d:
        _write(d, "m.json", payload)
        rows = ot.RemoteStore(d, _parse([])).model_breakdown()
        assert [r["root_id"] for r in rows] == ["s1"]  # the orphan is gone


def test_remote_store_demo_leaves_model_rows_unscaled():
    # The App's _load_model_cache scales the per-model breakdown for every store, so
    # RemoteStore must NOT pre-scale it -- else Overview and the Models tab disagree.
    import opentab.stores.remote as remote_mod

    payload = _summary("z", [workflow("s1", "2026-07-15 10:00:00", cost=10.0)])
    payload["model_breakdown"] = [
        {"root_id": "s1", "model_name": "openai/m", "cost": 10.0, "tokens_total": 100}
    ]
    orig = remote_mod.random.uniform
    remote_mod.random.uniform = lambda a, b: 1.0  # pin demo_scale to 3.0
    try:
        with tempfile.TemporaryDirectory() as d:
            _write(d, "z.json", payload)
            store = ot.RemoteStore(d, _parse(["--demo"]))
            assert store.model_breakdown()[0]["cost"] == 10.0  # raw; App scales it once
            assert store.workflows()[0].total_cost == 30.0  # workflow IS scaled in-store
    finally:
        remote_mod.random.uniform = orig


def test_remote_store_survives_unhashable_or_missing_root_id():
    # A crafted/corrupt model row (root_id an unhashable list, or absent) must be
    # dropped, not crash RemoteStore construction on the `in kept` set test.
    payload = _summary("bad", [workflow("s1", "2026-07-15 10:00:00")])
    payload["model_breakdown"] = [
        {"root_id": [], "model_name": "unhashable", "cost": 1.0},
        {"model_name": "missing", "cost": 2.0},
        {"root_id": "s1", "model_name": "ok", "cost": 3.0},
    ]
    with tempfile.TemporaryDirectory() as d:
        _write(d, "bad.json", payload)
        rows = ot.RemoteStore(d, _parse([])).model_breakdown()
        assert [r["model_name"] for r in rows] == ["ok"]


def test_remote_store_drops_a_workflow_with_no_valid_id():
    # A session with no usable id can't be keyed/deduped/attributed -- drop it rather
    # than seed the dedup set with None (which then poisons the model-row filter).
    payload = _summary("m", [workflow("good", "2026-07-15 10:00:00")])
    bad = dict(payload["workflows"][0])
    bad["id"] = None
    payload["workflows"].append(bad)
    with tempfile.TemporaryDirectory() as d:
        _write(d, "m.json", payload)
        got = ot.RemoteStore(d, _parse([])).workflows()
        assert [w.id for w in got] == ["good"]


def test_remote_store_workflows_returns_fresh_objects_each_call():
    # reload (r) re-snapshots total_cost as the real cost, and the App mutates it in
    # place under the "$" view -- so RemoteStore must hand back fresh copies or the
    # list-price estimate compounds on every reload (prices visibly climb).
    with tempfile.TemporaryDirectory() as d:
        _write(d, "z.json", _summary("z", [workflow("s1", "2026-07-15 10:00:00", cost=5.0)]))
        store = ot.RemoteStore(d, _parse([]))
        store.workflows()[0].total_cost = 999.0  # simulate the App's in-place $ mutation
        assert store.workflows()[0].total_cost == 5.0  # the next call is pristine, not 999


def test_remote_store_excludes_given_ids():
    # The fleet passes live-local ids so a pulled summary that re-states one is dropped
    # (no double count), model rows and all -- the live local copy wins.
    payload = _summary(
        "m",
        [workflow("local1", "2026-07-15 10:00:00"), workflow("remote-only", "2026-07-16 10:00:00")],
        model_breakdown=[
            {"root_id": "local1", "model_name": "a", "cost": 1.0},
            {"root_id": "remote-only", "model_name": "b", "cost": 2.0},
        ],
    )
    with tempfile.TemporaryDirectory() as d:
        _write(d, "m.json", payload)
        store = ot.RemoteStore(d, _parse([]), exclude_ids={"local1"})
        assert [w.id for w in store.workflows()] == ["remote-only"]
        assert [r["root_id"] for r in store.model_breakdown()] == ["remote-only"]


def test_remote_store_applies_demo_with_the_current_scale():
    # Demo is applied lazily in workflows() (not baked in at construction), so a shared
    # demo_scale assigned AFTER construction -- the fleet's CombinedStore -- takes effect
    # and local/remote proportions stay truthful.
    with tempfile.TemporaryDirectory() as d:
        _write(d, "z.json", _summary("z", [workflow("s1", "2026-07-15 10:00:00", cost=10.0)]))
        store = ot.RemoteStore(d, _parse(["--demo"]))
        store.demo_scale = 2.0  # as CombinedStore sets a shared factor post-construction
        assert store.workflows()[0].total_cost == 20.0
        store.demo_scale = 5.0  # re-read each call, never frozen into _wf
        assert store.workflows()[0].total_cost == 50.0


def test_remote_store_registers_a_zero_session_machine():
    # A valid but empty export still registers its machine, so the fleet's presence
    # check (remote.machines) doesn't discard it as "no summaries".
    with tempfile.TemporaryDirectory() as d:
        _write(d, "empty.json", _summary("freshbox", []))
        store = ot.RemoteStore(d, _parse([]))
        assert store.machines == ["freshbox"]
        assert store.workflows() == []
