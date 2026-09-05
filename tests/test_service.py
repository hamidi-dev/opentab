import json
import os
import sqlite3
import tempfile
from unittest.mock import patch

import opentab as ot
import opentab.notes as notes_module
import opentab.state as state_module

from tests._support import FakeStore, _claude_msg, _usage, _write_jsonl, workflow


class DetailStore(FakeStore):
    def __init__(self, workflows, source="OpenCode", model="anthropic/claude-opus-4-5"):
        super().__init__(workflows)
        self.source_name = source
        self.model = model

    def model_breakdown(self):
        return [
            {
                "root_id": item.id,
                "model_name": self.model,
                "runs": 1,
                "cost": item.total_cost,
                "tokens_total": item.total_tokens,
                "input": item.total_tokens,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "unpriced_input": item.total_tokens if item.total_cost == 0 else 0,
                "unpriced_output": 0,
                "unpriced_reasoning": 0,
                "unpriced_cache_read": 0,
                "unpriced_cache_write": 0,
            }
            for item in self._workflows
        ]

    def workflow_nodes(self, wid):
        return [
            {
                "depth": 0,
                "agent": "-",
                "title": wid,
                "created_at": "2026-09-01 12:00:00",
                "model_name": self.model,
                "cost": 0,
                "tokens_total": 100,
                "tokens_input": 100,
                "tokens_output": 0,
                "tokens_reasoning": 0,
                "tokens_cache_read": 0,
                "tokens_cache_write": 0,
            }
        ]

    def supports_turns(self, wid):
        return True

    def message_timeline(self, wid):
        return [
            {
                "time": "2026-09-01 12:00:00",
                "agent": "-",
                "depth": 0,
                "model_name": self.model,
                "cost": 0,
                "tokens_total": 100,
                "input": 100,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "tools": ["bash"],
                "prompt_id": "p1",
                "prompt_title": "secret preview",
                "prompt_full": "the whole secret prompt",
                "content_key": "turn-1",
            }
        ]

    def supports_tools(self, wid):
        return True

    def tool_breakdown(self, wid):
        return [
            {
                "tool": "mcp__github__search",
                "model_name": self.model,
                "calls": 2,
                "cost": 0,
                "tokens_total": 100,
                "input": 100,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
            }
        ]

    def supports_context(self, wid):
        return True

    def supports_context_curve(self, wid):
        return True

    def context_breakdown(self, wid):
        return [{"category": "tools", "kind": "definitions", "count": 2, "est_tokens": 40}]

    def supports_turn_content(self, wid):
        return True

    def turn_content(self, wid, content_key=None):
        return {content_key or "turn-1": [{"type": "text", "text": "raw secret"}]}


def _args():
    return type("Args", (), {"source": "auto", "demo": False})()


def test_session_ref_round_trips_arbitrary_identity_fields():
    ref = ot.SessionRef("laptop:one", "claude/code", "id with ünicode")
    encoded = ref.encode()
    assert encoded.startswith("ot1_")
    assert ot.SessionRef.decode(encoded) == ref
    for bad in ("plain", "ot1_bad", "ot1_W10"):
        try:
            ot.SessionRef.decode(bad)
            raise AssertionError("expected invalid reference")
        except ValueError:
            pass


def test_service_returns_dual_costs_filters_and_groups_without_ui_state():
    first = workflow("a", "2026-09-01 12:00:00", cost=0, tokens=1_000, directory="/repo/a")
    second = workflow("b", "2026-08-01 12:00:00", cost=2, tokens=20, directory="/repo/b")
    service = ot.OpenTabService(DetailStore([first, second]), _args(), "opencode")

    result = service.list_sessions(ot.SessionQuery(range="2026-09", limit=10))
    assert result["total"] == 1
    row = result["sessions"][0]
    assert row["native_id"] == "a"
    assert row["recorded_cost_usd"] == 0
    assert row["api_equivalent_cost_usd"] == ot.api_equivalent_cost(
        "anthropic/claude-opus-4-5", 1_000, 0, 0, 0, 0
    )
    grouped = service.summary(ot.SessionQuery(limit=10), group_by="project")
    assert grouped["totals"]["sessions"] == 2
    assert {group["key"] for group in grouped["groups"]} == {"/repo/a", "/repo/b"}


def test_qualified_refs_route_same_native_id_to_the_exact_combined_owner():
    left = workflow("same", "2026-09-01 12:00:00", cost=1)
    left.source = "OpenCode"
    right = workflow("same", "2026-09-02 12:00:00", cost=2)
    right.source = "Claude Code"
    combined = ot.CombinedStore(
        [
            DetailStore([left], source="OpenCode", model="openai/gpt-5"),
            DetailStore([right], source="Claude Code", model="anthropic/claude-opus-4-5"),
        ]
    )
    service = ot.OpenTabService(combined, _args(), "all")
    rows = service.list_sessions(ot.SessionQuery(limit=10))["sessions"]
    assert len({row["session_key"] for row in rows}) == 2
    assert {service.get_session(row["session_key"])["model_usage"][0]["model"] for row in rows} == {
        "openai/gpt-5",
        "anthropic/claude-opus-4-5",
    }
    try:
        service.get_session("same")
        raise AssertionError("expected ambiguous native id")
    except ot.ServiceError as exc:
        assert exc.code == "ambiguous_session" and len(exc.details["matches"]) == 2


def test_service_keeps_prompts_and_raw_trace_behind_separate_gates():
    item = workflow("a", "2026-09-01 12:00:00", cost=0)
    hidden = ot.OpenTabService(DetailStore([item]), _args())
    turns = hidden.session_turns("a")["turns"]
    assert "prompt" not in turns[0] and "content_key" not in turns[0]
    try:
        hidden.session_turns("a", include_prompts=True)
        raise AssertionError("expected prompt privacy gate")
    except ot.ServiceError as exc:
        assert exc.code == "raw_content_disabled"
    try:
        hidden.session_content("a", "turn-1")
        raise AssertionError("expected privacy gate")
    except ot.ServiceError as exc:
        assert exc.code == "raw_content_disabled"

    allowed = ot.OpenTabService(DetailStore([item]), _args(), allow_raw_content=True)
    turns = allowed.session_turns("a", include_prompts=True, include_content_keys=True)["turns"]
    assert turns[0]["prompt"] == "the whole secret prompt"
    assert turns[0]["content_key"] == "turn-1"
    assert allowed.session_content("a", "turn-1")["content"][0]["text"] == "raw secret"


def test_service_note_and_preference_mutations_use_authored_xdg_files():
    item = workflow("a", "2026-09-01 12:00:00")
    service = ot.OpenTabService(DetailStore([item]), _args())
    with tempfile.TemporaryDirectory() as tmp:
        old_state = os.environ.get("XDG_STATE_HOME")
        old_data = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_STATE_HOME"] = os.environ["XDG_DATA_HOME"] = tmp
        try:
            assert service.set_note("a", "because")["note"] == "because"
            assert service.mutate_set("bookmark", "add", "a")["values"] == ["a"]
            assert service.list_preferences()["bookmarks"] == ["a"]
            assert service.mutate_set("bookmark", "remove", "a")["values"] == []
            assert service.set_note("a", "")["note"] == ""
        finally:
            for key, value in (("XDG_STATE_HOME", old_state), ("XDG_DATA_HOME", old_data)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_qualified_mutations_do_not_cross_colliding_native_session_ids():
    left = workflow("same", "2026-09-01 12:00:00")
    right = workflow("same", "2026-09-01 13:00:00")
    left.machine = "one"
    right.machine = "two"
    service = ot.OpenTabService(
        ot.CombinedStore([DetailStore([left]), DetailStore([right])]), _args(), "all"
    )
    rows = service.list_sessions(ot.SessionQuery(limit=10))["sessions"]
    by_machine = {row["machine"]: row["session_key"] for row in rows}
    with tempfile.TemporaryDirectory() as tmp:
        old_state = os.environ.get("XDG_STATE_HOME")
        old_data = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_STATE_HOME"] = os.environ["XDG_DATA_HOME"] = tmp
        try:
            os.makedirs(os.path.dirname(ot.state_path()), exist_ok=True)
            with open(ot.state_path(), "w", encoding="utf-8") as fh:
                fh.write('{"bookmarks":["same"]}')
            with open(ot.notes_path(), "w", encoding="utf-8") as fh:
                fh.write('{"version":1,"notes":{"same":"legacy note"}}')
            assert (
                service.list_sessions(ot.SessionQuery(bookmarked=True, limit=10))["sessions"] == []
            )
            assert service.get_note(by_machine["one"])["note"] == ""
            assert service.get_note(by_machine["two"])["note"] == ""

            service.mutate_set("bookmark", "add", by_machine["one"])
            bookmarked = service.list_sessions(ot.SessionQuery(bookmarked=True, limit=10))[
                "sessions"
            ]
            assert [row["machine"] for row in bookmarked] == ["one"]
            service.set_note(by_machine["one"], "left note")
            service.set_note(by_machine["two"], "right note")
            assert service.get_note(by_machine["one"])["note"] == "left note"
            assert service.get_note(by_machine["two"])["note"] == "right note"
        finally:
            for key, value in (("XDG_STATE_HOME", old_state), ("XDG_DATA_HOME", old_data)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_demo_is_rejected_and_no_state_hides_authored_data():
    item = workflow("a", "2026-09-01 12:00:00")
    demo = DetailStore([item])
    demo.demo = True
    try:
        ot.OpenTabService(demo, _args())
        raise AssertionError("expected demo rejection")
    except ot.ServiceError as exc:
        assert exc.code == "demo_unsupported"

    args = _args()
    args.no_state = True
    service = ot.OpenTabService(DetailStore([item]), args)
    assert service.get_note("a")["note"] == ""
    assert service.list_preferences() == {
        "bookmarks": [],
        "ignored_projects": [],
        "ignored_sessions": [],
        "pinned_models": [],
    }
    for operation in (
        lambda: service.set_note("a", "hidden"),
        lambda: service.mutate_set("bookmark", "add", "a"),
    ):
        try:
            operation()
            raise AssertionError("expected state-disabled mutation")
        except ot.ServiceError as exc:
            assert exc.code == "state_disabled"


def test_service_enforces_the_tui_note_limit():
    service = ot.OpenTabService(DetailStore([workflow("a", "2026-09-01 12:00:00")]), _args())
    try:
        service.set_note("a", "x" * 501)
        raise AssertionError("expected note limit")
    except ot.ServiceError as exc:
        assert exc.code == "note_too_long" and exc.details == {"limit": 500}


def test_mixed_detail_costs_use_splits_or_report_that_the_estimate_is_incomplete():
    class MixedStore(DetailStore):
        def model_breakdown(self):
            row = super().model_breakdown()[0]
            row["unpriced_input"] = 50
            return [row]

        def workflow_nodes(self, wid):
            row = super().workflow_nodes(wid)[0]
            row.update(cost=2, unpriced_input=50)
            return [row]

        def tool_breakdown(self, wid):
            row = super().tool_breakdown(wid)[0]
            row["cost"] = 2
            return [row]

        def message_timeline(self, wid):
            row = super().message_timeline(wid)[0]
            row["cost"] = 2
            return [row]

    item = workflow("mixed", "2026-09-01 12:00:00", cost=2, tokens=100)
    assert item.unpriced_tokens == 0  # the model split, not this fast rollup, reveals mixing
    service = ot.OpenTabService(MixedStore([item]), _args())

    node = service.session_nodes("mixed")["nodes"][0]
    assert node["api_equivalent_cost_usd"] > 2
    assert node["api_equivalent_cost_complete"] is True
    tool = service.session_tools("mixed")["tools"][0]
    assert tool["api_equivalent_cost_usd"] is None
    assert tool["api_equivalent_cost_complete"] is False
    turn = service.session_turns("mixed")["turns"][0]
    assert turn["api_equivalent_cost_usd"] == 2
    assert turn["api_equivalent_cost_complete"] is True


def test_qualified_authored_entries_survive_source_scope_edits_and_removals():
    left = workflow("same", "2026-09-01 12:00:00")
    right = workflow("same", "2026-09-01 13:00:00")
    left.source, right.source = "OpenCode", "Claude Code"
    left_store, right_store = DetailStore([left]), DetailStore([right], source="Claude Code")
    merged = ot.OpenTabService(ot.CombinedStore([left_store, right_store]), _args(), "all")
    solo = ot.OpenTabService(left_store, _args(), "opencode")
    other = ot.OpenTabService(right_store, _args(), "claude")
    key = solo.get_session("same")["session_key"]
    other_key = other.get_session("same")["session_key"]
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"XDG_STATE_HOME": tmp, "XDG_DATA_HOME": tmp}
    ):
        assert ot.save_notes({"vanished": "keep me", "future": {"unknown": True}})
        with open(ot.state_path(), "w", encoding="utf-8") as fh:
            json.dump({"future": {"unknown": True}}, fh)
        merged.set_note(other_key, "other owner")
        merged.set_note(key, "old")
        assert solo.set_note(key, "quartz")["note"] == "quartz"
        for service in (solo, merged):
            assert service.get_note(key)["note"] == "quartz"
            assert service.get_session(key)["note"] == "quartz"
            matches = service.list_sessions(ot.SessionQuery(search="quartz"))["sessions"]
            assert [row["session_key"] for row in matches] == [key]
        solo.set_note("same", "")
        for service in (solo, merged):
            assert service.get_note(key)["note"] == ""
            assert service.list_sessions(ot.SessionQuery(search="quartz"))["total"] == 0
        assert other.get_note("same")["note"] == "other owner"

        for resource, field in (("bookmark", "bookmarks"), ("ignored-session", "ignored_sessions")):
            merged.mutate_set(resource, "add", other_key)
            merged.mutate_set(resource, "add", key)
            solo.mutate_set(resource, "add", "same")
            assert set(solo.list_preferences()[field]) == {key, other_key}
            solo.mutate_set(resource, "remove", key)
            assert solo.list_preferences()[field] == [other_key]
            assert merged.list_preferences()[field] == [other_key]
        assert not solo.get_session(key)["bookmarked"]
        assert not solo.get_session(key)["ignored"]
        assert solo.list_sessions(ot.SessionQuery(bookmarked=True))["total"] == 0
        assert [row["session_key"] for row in merged.list_sessions()["sessions"]] == [key]
        bookmarked = merged.list_sessions(ot.SessionQuery(bookmarked=True, include_ignored=True))
        assert [row["session_key"] for row in bookmarked["sessions"]] == [other_key]
        with open(ot.notes_path(), encoding="utf-8") as fh:
            saved_notes = json.load(fh)["notes"]
        assert saved_notes["vanished"] == "keep me"
        assert saved_notes["future"] == {"unknown": True}
        assert ot.load_state()["future"] == {"unknown": True}


def test_legacy_authored_keys_stay_native_until_a_collision_requires_qualification():
    left = workflow("same", "2026-09-01 12:00:00")
    right = workflow("same", "2026-09-01 13:00:00")
    left.machine, right.machine = "one", "two"
    left_store, right_store = DetailStore([left]), DetailStore([right])
    solo = ot.OpenTabService(left_store, _args())
    merged = ot.OpenTabService(ot.CombinedStore([left_store, right_store]), _args())
    key = solo.get_session("same")["session_key"]
    other_key = next(item.ref.encode() for item in merged._sessions if item.ref.machine == "two")
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"XDG_STATE_HOME": tmp, "XDG_DATA_HOME": tmp}
    ):
        solo.set_note("same", "legacy")
        solo.mutate_set("bookmark", "add", "same")
        solo.mutate_set("ignored-session", "add", "same")
        assert ot.read_notes()[0] == {"same": "legacy"}
        assert solo.get_session("same")["bookmarked"]
        assert solo.get_session("same")["ignored"]
        assert merged.get_note(key)["note"] == ""
        # Filtering to one harness/machine does not make a colliding legacy ID safe.
        assert merged.list_sessions(ot.SessionQuery(machine="one", bookmarked=True))["total"] == 0
        assert merged.list_sessions(ot.SessionQuery(search="legacy"))["total"] == 0
        merged.set_note(key, "qualified")
        merged.mutate_set("bookmark", "add", key)
        merged.mutate_set("ignored-session", "add", key)
        solo.set_note("same", "updated")
        assert solo.get_note("same")["note"] == merged.get_note(key)["note"] == "updated"
        assert ot.read_notes()[0] == {"same": "legacy", key: "updated"}
        merged.set_note(other_key, "other owner")
        for resource in ("bookmark", "ignored-session"):
            merged.mutate_set(resource, "add", other_key)
        # A merged-scope deletion touches only this owner, not the ambiguous native key.
        merged.set_note(key, "")
        for resource in ("bookmark", "ignored-session"):
            merged.mutate_set(resource, "remove", key)
        assert merged.get_note(key)["note"] == ""
        assert merged.list_sessions(ot.SessionQuery(bookmarked=True))["total"] == 0
        assert ot.read_notes()[0] == {"same": "legacy", other_key: "other owner"}
        for field in ("bookmarks", "ignored_sessions"):
            assert set(solo.list_preferences()[field]) == {"same", other_key}

        # Recreate both aliases, then delete in the unique scope: neither can resurrect.
        merged.set_note(key, "qualified")
        for resource in ("bookmark", "ignored-session"):
            merged.mutate_set(resource, "add", key)
        assert solo.set_note("same", "")["note"] == ""
        for resource in ("bookmark", "ignored-session"):
            assert solo.mutate_set(resource, "remove", "same")["values"] == [other_key]
        for service in (solo, merged):
            assert service.get_note(key)["note"] == ""
            assert service.get_session(key)["note"] == ""
            assert not service.get_session(key)["bookmarked"]
            assert not service.get_session(key)["ignored"]
            assert service.list_sessions(ot.SessionQuery(search="legacy"))["total"] == 0
            assert service.list_sessions(ot.SessionQuery(search="qualified"))["total"] == 0
            assert service.list_sessions(ot.SessionQuery(bookmarked=True))["total"] == 0
        assert ot.read_notes()[0] == {other_key: "other owner"}
        assert merged.get_session(other_key)["bookmarked"]
        assert merged.get_session(other_key)["ignored"]


def test_service_mutations_defer_alias_selection_to_the_storage_lock():
    service = ot.OpenTabService(DetailStore([workflow("native", "2026-09-01 12:00:00")]), _args())
    key = service.get_session("native")["session_key"]
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"XDG_STATE_HOME": tmp, "XDG_DATA_HOME": tmp}
    ):
        original_note_lock, original_state_lock = notes_module._locked, state_module._locked

        def note_alias_appears():
            assert ot.save_notes({key: "concurrent", "other": "keep"})
            return original_note_lock()

        def state_alias_appears(path):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"bookmarks": [key], "ignored_sessions": [key]}, fh)
            return original_state_lock(path)

        for text in ("new", ""):
            assert ot.save_notes({"native": "old"})
            with patch.object(notes_module, "_locked", note_alias_appears):
                assert service.set_note("native", text)["note"] == text
            assert ot.read_notes()[0] == (
                {key: "new", "other": "keep"} if text else {"other": "keep"}
            )
            assert service.get_note("native")["note"] == text
        for resource in ("bookmark", "ignored-session"):
            for operation in ("add", "remove"):
                with open(ot.state_path(), "w", encoding="utf-8") as fh:
                    json.dump({}, fh)
                with patch.object(state_module, "_locked", state_alias_appears):
                    result = service.mutate_set(resource, operation, "native")
                assert result["values"] == ([key] if operation == "add" else [])


def test_service_note_mutations_match_displayable_alias_selection():
    service = ot.OpenTabService(DetailStore([workflow("native", "2026-09-01 12:00:00")]), _args())
    key = service.get_session("native")["session_key"]
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
        for target, other in (("native", key), (key, "native")):
            assert ot.save_notes({target: "old", other: {"future": "keep"}})
            assert service.get_note("native")["note"] == "old"
            assert service.set_note("native", "new")["note"] == "new"
            assert service.get_note("native")["note"] == "new"
            assert service.set_note("native", "")["note"] == ""
            assert service.get_note("native")["note"] == ""
            assert notes_module._read_raw() == ({other: {"future": "keep"}}, True)


def test_service_counts_long_ttl_cache_writes_once_and_keeps_their_price():
    with tempfile.TemporaryDirectory() as tmp:
        _write_jsonl(
            os.path.join(tmp, "s1.jsonl"),
            [
                _claude_msg(
                    "s1", "claude-opus-4-5", _usage(cw=1_000_000, cw1h=750_000), uuid="u0", cwd=tmp
                )
            ],
        )
        store = ot.ClaudeStore(tmp, _args())
        model = store.model_breakdown()[0]
        assert model["unpriced_cache_write"] == 1_000_000
        assert model["unpriced_cache_write_1h"] == 750_000
        service = ot.OpenTabService(store, _args())
        session = service.get_session("s1")
        totals = service.summary()["totals"]
        expected = ot.api_equivalent_cost(
            "anthropic/claude-opus-4-5", 0, 0, 0, 0, 1_000_000, 750_000
        )
        for row in (session, totals):
            assert row["tokens"] == row["unpriced_tokens"] == 1_000_000
            assert row["api_equivalent_cost_usd"] == expected
        assert session["api_equivalent_root_cost_usd"] == expected
        assert service.session_nodes("s1")["nodes"][0]["api_equivalent_cost_usd"] == expected


def _opencode_memory_service(messages):
    store = ot.Store.__new__(ot.Store)
    store.demo = False
    store.conn = sqlite3.connect(":memory:")
    store.conn.row_factory = sqlite3.Row
    store.conn.executescript(
        """
        create table session (id text primary key, parent_id text, time_created integer);
        create table message (id text primary key, session_id text, data text);
        """
    )
    sessions = {sid for sid, _model, _tokens, _cost in messages}
    store.conn.executemany(
        "insert into session values (?, ?, ?)",
        [(sid, None if sid == "root" else "root", 1_780_000_000_000) for sid in sorted(sessions)],
    )
    for i, (sid, model, tokens, cost) in enumerate(messages):
        provider, model_id = model.split("/", 1)
        data = {
            "role": "assistant",
            "providerID": provider,
            "modelID": model_id,
            "tokens": {"input": tokens},
            "cost": cost,
            "time": {"created": 1_780_000_000_000 + i},
        }
        store.conn.execute("insert into message values (?, ?, ?)", (str(i), sid, json.dumps(data)))
    store.session_columns = store._table_columns("session")
    store.supports_tool_breakdown = False
    store.supports_message_timeline = True
    return ot.OpenTabService(store, _args())


def test_opencode_multimodel_root_node_uses_exact_model_attribution_without_detail_reads():
    service = _opencode_memory_service(
        [("root", "anthropic/claude-opus-4-5", 1_000_000, 0)]
        + [("root", "anthropic/claude-sonnet-4-5", 1_000_000, 0)] * 2
    )
    try:
        session = service.get_session("root")
        assert session["api_equivalent_cost_usd"] == session["api_equivalent_root_cost_usd"] == 11
        assert (
            sum(row["api_equivalent_cost_usd"] for row in service.session_turns("root")["turns"])
            == 11
        )
        with patch.object(
            service.store, "message_timeline", side_effect=AssertionError("turn read")
        ), patch.object(service.store, "turn_content", side_effect=AssertionError("trace read")):
            node = service.session_nodes("root")["nodes"][0]
        assert node["api_equivalent_cost_usd"] == 11
        assert node["api_equivalent_cost_complete"] is True
    finally:
        service.store.conn.close()


def test_opencode_multimodel_subagent_node_is_uncertain_but_single_model_is_complete():
    opus, sonnet = "anthropic/claude-opus-4-5", "anthropic/claude-sonnet-4-5"
    for child_models, expected in (((opus, sonnet, sonnet), None), ((opus, opus, opus), 15)):
        service = _opencode_memory_service(
            [("root", opus, 1_000_000, 0)]
            + [("child", model, 1_000_000, 0) for model in child_models]
        )
        try:
            with patch.object(
                service.store, "message_timeline", side_effect=AssertionError("turn read")
            ), patch.object(
                service.store, "turn_content", side_effect=AssertionError("trace read")
            ):
                root, child = service.session_nodes("root")["nodes"]
            assert root["api_equivalent_cost_usd"] == 5
            assert root["api_equivalent_cost_complete"] is True
            assert child["api_equivalent_cost_usd"] == expected
            assert child["api_equivalent_cost_complete"] is (expected is not None)
        finally:
            service.store.conn.close()


def test_node_pricing_does_not_use_a_zero_usage_dominant_label_or_unknown_price():
    for model, expected in (("anthropic/claude-opus-4-5", 5), ("unknown/not-recorded", None)):
        service = _opencode_memory_service(
            [("root", model, 1_000_000, 0)] + [("root", "anthropic/claude-sonnet-4-5", 0, 0)] * 2
        )
        try:
            node = service.session_nodes("root")["nodes"][0]
            assert node["api_equivalent_cost_usd"] == expected
            assert node["api_equivalent_cost_complete"] is (expected is not None)
        finally:
            service.store.conn.close()
