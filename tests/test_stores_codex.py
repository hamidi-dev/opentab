import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import opentab as ot
from opentab.formatting import iso_to_local
from opentab.util import TRACE_OUTPUT_CAP

from tests._support import (
    FakeStore,
    _claude_msg,
    _codex_meta,
    _codex_tokens,
    _codex_turn,
    _empty_opencode_db,
    _usage,
    _write_jsonl,
    workflow,
)

# --- Codex CLI rollout helpers (~/.codex/sessions/**/rollout-*.jsonl) ---------
CODEX_SID = "0199aa8e-1b9e-7912-bcd4-9b00c8733ea6"


def _codex_user(text, ts="2025-10-03T14:51:05.000Z"):
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text, "kind": "plain"},
    }


def _codex_call(name, ts="2025-10-03T14:51:15.000Z", kind="function_call"):
    # A tool-call response_item; it belongs to the turn whose token_count follows.
    payload = {"type": kind, "call_id": "c1"}
    if name is not None:
        payload["name"] = name
    return {"timestamp": ts, "type": "response_item", "payload": payload}


def _codex_item(kind, ts="2025-10-03T14:51:15.000Z", **fields):
    return {"timestamp": ts, "type": "response_item", "payload": {"type": kind, **fields}}


def _codex_rollout(root, sid, rows):
    # Codex files are named rollout-<ts>-<uuid>.jsonl; the uuid is the session id.
    _write_jsonl(os.path.join(root, f"rollout-2025-10-03T16-51-03-{sid}.jsonl"), rows)


def _conversation_rollout(root, sid, rows=(), parent=None, name=None):
    source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "cli"
    path = Path(root) / (name or f"rollout-2025-10-03T16-51-03-{sid}.jsonl")
    _write_jsonl(str(path), [_codex_meta(sid, str(root), source=source), *rows])
    return path


def _conversation_error(store, root, selected=None, code=None):
    from opentab.conversation import ConversationError

    try:
        store.conversation_source(root, selected)
    except ConversationError as exc:
        if code:
            assert exc.code == code, exc.code
        assert "PRIVATE" not in str(exc)
    else:
        raise AssertionError("Expected a safe conversation error")


def test_codex_conversation_reads_final_messages_without_usage_flush_or_accounting():
    with tempfile.TemporaryDirectory() as tmp:
        answer = "  Verbatim answer\n" + "x" * (TRACE_OUTPUT_CAP + 20) + "\n"
        rows = [
            _codex_user("question"),
            _codex_turn("gpt-5-codex", tmp),
            _codex_tokens(10, 5, 0, 15),
        ]
        rows.extend(
            _codex_item(
                "message",
                role="assistant",
                id="same-native-id",
                content=[
                    {"type": "output_text", "text": answer if i == 120 else "repeat"},
                    {"type": "image", "text": "PRIVATE attachment"},
                    {"type": "output_text", "text": "\nsecond block  "},
                    {"type": "output_text", "text": ""},
                ],
            )
            for i in range(121)
        )
        rows[-1].update(id="native-record", parent_id="native-parent", timestamp=None)
        _conversation_rollout(tmp, CODEX_SID, rows)
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        workflows = store.workflows()
        cache = store._sessions
        with patch.object(
            store, "_parse", side_effect=AssertionError("accounting parse")
        ), patch.object(
            store, "_parse_file", side_effect=AssertionError("accounting file parse")
        ), patch.object(store, "_head_meta", side_effect=AssertionError("cached metadata")):
            result = store.conversation_source(CODEX_SID)
        assert store._sessions is cache
        assert store.workflows() == workflows
        records = result["records"]
        assert len(records) == 122 and len({r["id"] for r in records}) == 122
        assert [p["text"] for p in records[-1]["parts"]] == [answer, "\nsecond block  ", ""]
        assert [p["id"] for p in records[-1]["parts"]] == ["0", "2", "3"]
        assert [p["source"] for p in records[-1]["parts"]] == [
            {"block_index": 0},
            {"block_index": 2},
            {"block_index": 3},
        ]
        assert records[-1]["message_id"] == "same-native-id"
        assert records[-1]["record_id"] == "native-record"
        assert records[-1]["parent_id"] == "native-parent"
        assert records[-1]["timestamp"] is None
        assert result["execution_id"] == CODEX_SID
        assert result["executions"] == [{"id": CODEX_SID, "parent_id": None}]
        assert "PRIVATE" not in json.dumps(result)
        assert result == store.conversation_source(CODEX_SID)


def test_codex_refresh_manifest_reuses_heads_and_tracks_content_ownership_and_winners():
    child = "0299aa8e-1b9e-7912-bcd4-9b00c8733ea6"
    other = "0399aa8e-1b9e-7912-bcd4-9b00c8733ea6"
    with tempfile.TemporaryDirectory() as tmp:
        sessions = Path(tmp) / "sessions"
        sessions.mkdir()
        root_path = _conversation_rollout(sessions, CODEX_SID, [_codex_user("root")])
        _conversation_rollout(sessions, child, [_codex_user("child")], parent=CODEX_SID)
        other_path = _conversation_rollout(sessions, other, [_codex_user("unrelated")])
        store = ot.CodexStore(str(sessions), type("Args", (), {"demo": False})())
        with patch.object(
            store,
            "_read_conversation_head",
            wraps=store._read_conversation_head,
        ) as heads:
            store.prepare_conversation_refresh()
            initial_calls = heads.call_count
            initial = store.conversation_manifest(CODEX_SID)
            assert initial and store.conversation_source(CODEX_SID)
            assert store.conversation_source(CODEX_SID, child)
            assert heads.call_count == initial_calls == 3
            store.finish_conversation_refresh()

        with other_path.open("a") as stream:
            stream.write(json.dumps(_codex_user("unrelated change")) + "\n")
        assert store.conversation_manifest(CODEX_SID) == initial

        with root_path.open("a") as stream:
            stream.write(json.dumps(_codex_user("changed")) + "\n")
        changed = store.conversation_manifest(CODEX_SID)
        assert changed != initial

        resumed = _conversation_rollout(
            sessions,
            CODEX_SID,
            [_codex_user("resumed")],
            name=f"rollout-2025-10-04T16-51-03-{CODEX_SID}.jsonl",
        )
        with_resume = store.conversation_manifest(CODEX_SID)
        assert with_resume != changed
        resumed.unlink()
        assert store.conversation_manifest(CODEX_SID) != with_resume

        before_replace = store.conversation_manifest(CODEX_SID)
        root_path.unlink()
        root_path = _conversation_rollout(sessions, CODEX_SID, [_codex_user("replacement")])
        assert store.conversation_manifest(CODEX_SID) != before_replace

        before_reparent = store.conversation_manifest(CODEX_SID)
        _conversation_rollout(sessions, child, [_codex_user("moved")], parent=other)
        assert store.conversation_manifest(CODEX_SID) != before_reparent

        archived = Path(tmp) / "archived_sessions"
        archived.mkdir()
        before_archive = store.conversation_manifest(CODEX_SID)
        root_path.rename(archived / root_path.name)
        assert store.conversation_manifest(CODEX_SID) != before_archive


def test_codex_refresh_catalog_rejects_a_head_mutated_during_discovery():
    with tempfile.TemporaryDirectory() as tmp:
        path = _conversation_rollout(tmp, CODEX_SID, [_codex_user("before")])
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        read_head = store._read_conversation_head
        mutated = False

        def mutate_after_head(name):
            nonlocal mutated
            result = read_head(name)
            if not mutated:
                mutated = True
                _write_jsonl(
                    str(path),
                    [
                        _codex_meta(
                            CODEX_SID,
                            tmp,
                            source={"subagent": {"thread_spawn": {"parent_thread_id": "foreign"}}},
                        ),
                        _codex_user("after"),
                    ],
                )
            return result

        with patch.object(store, "_read_conversation_head", side_effect=mutate_after_head):
            store.prepare_conversation_refresh()
        assert path.exists()
        assert store.conversation_manifest(CODEX_SID) is None
        _conversation_error(store, CODEX_SID)


def test_codex_manifest_tracks_an_unidentified_missing_ancestor_candidate():
    with tempfile.TemporaryDirectory() as tmp:
        _conversation_rollout(tmp, "root", [_codex_user("root")], parent="missing")
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        initial_manifest = store.conversation_manifest("root")
        initial_snapshot = store.conversation_source("root")["snapshot"]

        candidate = Path(tmp) / "rollout-2025-10-04T16-51-03-missing.jsonl"
        _write_jsonl(str(candidate), [_codex_user("metadata unavailable")])
        assert store.conversation_manifest("root") != initial_manifest
        assert store.conversation_source("root")["snapshot"] != initial_snapshot

        candidate.unlink()
        assert store.conversation_manifest("root") == initial_manifest
        assert store.conversation_source("root")["snapshot"] == initial_snapshot


def test_codex_conversation_zero_usage_keeps_prompt_representations_and_omits_nontext():
    with tempfile.TemporaryDirectory() as tmp:
        prompt = "  repeat\nverbatim  "
        _conversation_rollout(
            tmp,
            CODEX_SID,
            [
                _codex_user(prompt),
                _codex_user(prompt),
                _codex_item(
                    "message", role="user", content=[{"type": "input_text", "text": prompt}]
                ),
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "UserMessage",
                            "id": "user-item",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image", "text": "PRIVATE image"},
                                {"type": "text", "text": "second"},
                            ],
                        },
                    },
                },
                _codex_item(
                    "message", role="assistant", content=[{"type": "output_text", "text": "answer"}]
                ),
                _codex_item(
                    "message", role="system", content=[{"type": "text", "text": "PRIVATE system"}]
                ),
                _codex_item(
                    "reasoning", summary=[{"type": "summary_text", "text": "PRIVATE reasoning"}]
                ),
                _codex_item("function_call", arguments="PRIVATE args"),
                _codex_item("function_call_output", output="PRIVATE output"),
            ],
        )
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        assert store.workflows() == []
        result = store.conversation_source(CODEX_SID)
        assert len(result["records"]) == 5
        assert [r["origin"] for r in result["records"]] == [
            "prompt-event",
            "prompt-event",
            "model-input",
            "prompt-event",
            "recorded-message",
        ]
        assert [r["parts"][0]["text"] for r in result["records"][:4]] == [prompt] * 4
        assert result["records"][3]["message_id"] == "user-item"
        assert len(result["records"][3]["parts"]) == 2
        assert "PRIVATE" not in json.dumps(result)
        assert any("tools, reasoning, and attachments" in note for note in result["limitations"])
        assert store.workflows() == []


def test_codex_conversation_exact_execution_ownership_and_metadata_only_discovery():
    from opentab.conversation import read_jsonl

    with tempfile.TemporaryDirectory() as tmp:
        for sid, parent in (
            ("root", None),
            ("child", "root"),
            ("sibling", "root"),
            ("grandchild", "child"),
            ("foreign", None),
        ):
            _conversation_rollout(tmp, sid, [_codex_user(sid)], parent=parent)
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        with patch("opentab.conversation.read_jsonl", wraps=read_jsonl) as reader:
            result = store.conversation_source("root", "child")
        assert len(reader.call_args.args[0]) == 1
        assert reader.call_args.args[0][0].name.endswith("-child.jsonl")
        assert [r["parts"][0]["text"] for r in result["records"]] == ["child"]
        assert result["executions"] == [
            {"id": "root", "parent_id": None},
            {"id": "child", "parent_id": "root"},
            {"id": "grandchild", "parent_id": "child"},
            {"id": "sibling", "parent_id": "root"},
        ]
        assert [r["parts"][0]["text"] for r in store.conversation_source("root")["records"]] == [
            "root"
        ]
        assert store.conversation_source("root", "grandchild")["execution_id"] == "grandchild"
        for root, selected in (
            ("root", "foreign"),
            ("child", "sibling"),
            ("root", "missing"),
            ("missing", "child"),
            ("root", ""),
            ("", "child"),
        ):
            _conversation_error(store, root, selected)


def test_codex_conversation_rejects_resumed_conflicting_ownership_and_cycles_freshly():
    with tempfile.TemporaryDirectory() as tmp:
        _conversation_rollout(tmp, "root")
        _conversation_rollout(tmp, "other")
        child = _conversation_rollout(tmp, "child", [_codex_user("PRIVATE child")], parent="root")
        _conversation_rollout(tmp, "nested", parent="child")
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        assert store.conversation_source("root", "child")["records"]
        for parent in ("other", None, "child", "nested"):
            resumed = _conversation_rollout(tmp, "child", parent=parent, name="resumed-child.jsonl")
            _conversation_error(store, "root", "child", "invalid_execution")
            _conversation_error(store, "root", "nested", "invalid_execution")
            assert {e["id"] for e in store.conversation_source("root")["executions"]} == {"root"}
            resumed.unlink()
        child.unlink()
        _conversation_rollout(tmp, "child", parent="nested")
        _conversation_error(store, "root", "nested", "invalid_execution")
        _conversation_error(store, "child", code="invalid_execution")
        _conversation_rollout(tmp, "root", parent="root")
        _conversation_error(store, "root", code="invalid_execution")


def test_codex_conversation_requires_metadata_not_filename_and_discovers_nonstandard_names():
    with tempfile.TemporaryDirectory() as tmp:
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        _conversation_error(store, CODEX_SID, code="source_unavailable")
        path = Path(tmp) / f"rollout-{CODEX_SID}.jsonl"
        _write_jsonl(str(path), [_codex_user("PRIVATE unidentified")])
        _conversation_error(store, CODEX_SID, code="source_unavailable")
        _write_jsonl(str(path), [_codex_meta("foreign", tmp), _codex_user("PRIVATE foreign")])
        _conversation_error(store, CODEX_SID, code="source_unavailable")
        path.unlink()
        _conversation_rollout(tmp, CODEX_SID, [_codex_user("renamed")], name="nonstandard.jsonl")
        result = store.conversation_source(CODEX_SID)
        assert result["records"][0]["source"]["file"] == "nonstandard.jsonl"
        assert any("64 KiB" in note for note in result["limitations"])
        _write_jsonl(
            str(Path(tmp) / "bare.jsonl"),
            [{"id": "legacy", "git": {}}, _codex_user("legacy prompt")],
        )
        assert (
            store.conversation_source("legacy")["records"][0]["parts"][0]["text"] == "legacy prompt"
        )


def test_codex_conversation_live_archive_and_resumed_copy_policy_preserves_occurrences():
    from opentab.conversation import source_key

    with tempfile.TemporaryDirectory() as tmp:
        live, archive = Path(tmp) / "sessions", Path(tmp) / "archived_sessions"
        live.mkdir()
        archive.mkdir()
        first = _conversation_rollout(live, CODEX_SID, [_codex_user("repeat")])
        _conversation_rollout(archive, CODEX_SID, [_codex_user("PRIVATE ignored backup")])
        resumed = _conversation_rollout(
            archive,
            CODEX_SID,
            [_codex_user("repeat")],
            name=f"rollout-2025-10-04-{CODEX_SID}.jsonl",
        )
        _conversation_rollout(archive, "archived-only", [_codex_user("archived")])
        store = ot.CodexStore(str(live), type("Args", (), {"demo": False})())
        result = store.conversation_source(CODEX_SID)
        assert [r["parts"][0]["text"] for r in result["records"]] == ["repeat", "repeat"]
        assert [r["id"] for r in result["records"]] == [
            f"cx:{source_key(p)}:2" for p in (first, resumed)
        ]
        assert all(r["message_id"] == "line:2" for r in result["records"])
        assert all(r["record_id"] is None and r["parent_id"] is None for r in result["records"])
        assert "PRIVATE" not in json.dumps(result)
        assert any("live tree first" in note for note in result["limitations"])
        assert any("resumed" in note for note in result["limitations"])
        assert (
            store.conversation_source("archived-only")["records"][0]["parts"][0]["text"]
            == "archived"
        )
        first.unlink()
        fresh = store.conversation_source(CODEX_SID)
        assert fresh["snapshot"] != result["snapshot"]
        assert fresh["records"][0]["id"] != result["records"][0]["id"]


def test_codex_conversation_strict_physical_lines_warn_and_mutations_change_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        path = _conversation_rollout(tmp, CODEX_SID, [_codex_user("before")])
        with path.open("ab") as fh:
            fh.write(b'\n{"PRIVATE":"bad\xff"}\n{broken\n[]\n')
            fh.write(json.dumps(_codex_user("after")).encode("utf-8") + b"\n")
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        before = store.conversation_source(CODEX_SID)
        assert [r["source"]["line"] for r in before["records"]] == [2, 7]
        assert [r["parts"][0]["text"] for r in before["records"]] == ["before", "after"]
        assert any(
            "malformed" in note.lower() or "partial" in note.lower()
            for note in before["limitations"]
        )
        assert "PRIVATE" not in json.dumps(before)
        _conversation_rollout(tmp, CODEX_SID, [_codex_user("edited")])
        after = store.conversation_source(CODEX_SID)
        assert after["snapshot"] != before["snapshot"]
        assert after["records"][0]["id"] == before["records"][0]["id"]
        path.unlink()
        _conversation_error(store, CODEX_SID, code="source_unavailable")


def test_codex_conversation_snapshot_rejects_metadata_mutation_during_selected_read():
    from opentab.conversation import read_jsonl

    with tempfile.TemporaryDirectory() as tmp:
        _conversation_rollout(tmp, "root")
        _conversation_rollout(tmp, "child", [_codex_user("child")], parent="root")
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())

        def mutate(paths):
            result = read_jsonl(paths)
            _conversation_rollout(tmp, "root", parent="foreign")
            return result

        with patch("opentab.conversation.read_jsonl", side_effect=mutate):
            _conversation_error(store, "root", "child", "source_changed")


def test_codex_conversation_unrelated_activity_during_read_preserves_snapshot():
    from opentab.conversation import read_jsonl

    for selected in (None, "child"):
        with tempfile.TemporaryDirectory() as tmp:
            _conversation_rollout(tmp, "root", [_codex_user("root")])
            _conversation_rollout(tmp, "child", [_codex_user("child")], parent="root")
            unrelated = _conversation_rollout(tmp, "unrelated", [_codex_user("old")])
            store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
            before = store.conversation_source("root", selected)
            for action in ("modify", "append", "add-root", "unrelated-metadata", "other-execution"):

                def mutate(paths, action=action, unrelated=unrelated, selected=selected):
                    result = read_jsonl(paths)
                    if action == "modify":
                        _conversation_rollout(tmp, "unrelated", [_codex_user("modified")])
                    elif action == "append":
                        with unrelated.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps(_codex_user("appended")) + "\n")
                    elif action == "add-root":
                        _conversation_rollout(tmp, "new-unrelated", [_codex_user("new")])
                    elif action == "unrelated-metadata":
                        _write_jsonl(str(unrelated), [_codex_meta("unrelated", "changed-cwd")])
                    elif selected:
                        _conversation_rollout(tmp, "root", [_codex_user("ancestor text changed")])
                    else:
                        _conversation_rollout(
                            tmp, "child", [_codex_user("child text changed")], parent="root"
                        )
                    return result

                store.prepare_conversation_refresh()
                try:
                    with patch("opentab.conversation.read_jsonl", side_effect=mutate):
                        during = store.conversation_source("root", selected)
                finally:
                    store.finish_conversation_refresh()
                assert during == before, action
                assert store.conversation_source("root", selected) == before, action


def test_codex_conversation_relevant_activity_during_read_invalidates_snapshot():
    from opentab.conversation import read_jsonl

    for action in (
        "selected-text",
        "selected-parent",
        "ancestor-parent",
        "selected-resume",
        "ancestor-resume",
        "declared-descendant",
        "new-descendant",
        "adopt-unrelated",
    ):
        with tempfile.TemporaryDirectory() as tmp:
            _conversation_rollout(tmp, "root")
            _conversation_rollout(tmp, "middle", parent="root")
            _conversation_rollout(tmp, "child", [_codex_user("child")], parent="middle")
            _conversation_rollout(tmp, "sibling", parent="root")
            _conversation_rollout(tmp, "unrelated")
            store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())

            def mutate(paths, action=action):
                result = read_jsonl(paths)
                if action == "selected-text":
                    _conversation_rollout(tmp, "child", [_codex_user("changed")], parent="middle")
                elif action == "selected-parent":
                    _conversation_rollout(tmp, "child", parent="unrelated")
                elif action == "ancestor-parent":
                    _conversation_rollout(tmp, "middle", parent="unrelated")
                elif action == "selected-resume":
                    _conversation_rollout(
                        tmp, "child", parent="unrelated", name="resumed-child.jsonl"
                    )
                elif action == "ancestor-resume":
                    _conversation_rollout(
                        tmp, "middle", parent="unrelated", name="resumed-middle.jsonl"
                    )
                elif action == "declared-descendant":
                    _conversation_rollout(tmp, "sibling", parent="unrelated")
                elif action == "new-descendant":
                    _conversation_rollout(tmp, "new-descendant", parent="sibling")
                else:
                    _conversation_rollout(tmp, "unrelated", parent="root")
                return result

            with patch("opentab.conversation.read_jsonl", side_effect=mutate):
                _conversation_error(store, "root", "child", "source_changed")


def test_codex_conversation_capability_is_static_and_demo_never_reads():
    with tempfile.TemporaryDirectory() as tmp:
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        with patch.object(
            store, "_files", side_effect=AssertionError("unexpected read")
        ), patch.object(store, "_parse", side_effect=AssertionError("unexpected parse")):
            assert store.supports_conversation("not-present") is True
            store.demo = True
            assert store.supports_conversation(CODEX_SID) is False
            _conversation_error(store, CODEX_SID, code="unsupported")


def test_codex_conversation_snapshot_includes_root_metadata_for_child_reads():
    with tempfile.TemporaryDirectory() as tmp:
        root_path = _conversation_rollout(tmp, "root")
        _conversation_rollout(tmp, "child", [_codex_user("child")], parent="root")
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        before = store.conversation_source("root", "child")
        _write_jsonl(str(root_path), [_codex_meta("root", "different-metadata-cwd")])
        after = store.conversation_source("root", "child")
        assert before["records"] == after["records"]
        assert before["snapshot"] != after["snapshot"]
        _conversation_rollout(tmp, "sibling", parent="root")
        assert after["snapshot"] != store.conversation_source("root", "child")["snapshot"]


def test_codex_conversation_revalidates_all_selected_metadata_and_missing_ancestor_copies():
    with tempfile.TemporaryDirectory() as tmp:
        _conversation_rollout(tmp, "root")
        _conversation_rollout(tmp, "child", parent="root")
        path = _conversation_rollout(tmp, "nested", [_codex_user("PRIVATE nested")], parent="child")
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        assert store.conversation_source("root", "nested")["records"]
        missing_meta = Path(tmp) / "resumed-child.jsonl"
        _write_jsonl(str(missing_meta), [_codex_user("PRIVATE unknown ownership")])
        _conversation_error(store, "root", "nested", "invalid_execution")
        missing_meta.unlink()
        for meta in (_codex_meta("foreign", tmp), {"type": "session_meta", "payload": {}}):
            _conversation_rollout(
                tmp, "nested", [_codex_user("PRIVATE nested"), meta], parent="child"
            )
            _conversation_error(store, "root", "nested")
        path.unlink()
        _conversation_error(store, "root", "nested", "source_unavailable")


def test_codex_conversation_same_rollout_ancestor_conflicts_cannot_authorize_grandchildren():
    for intervening in ([], [_codex_user("PRIVATE ancestor text " + "x" * 70000)]):
        with tempfile.TemporaryDirectory() as tmp:
            _conversation_rollout(tmp, "root")
            _conversation_rollout(tmp, "foreign")
            conflict = _codex_meta(
                "middle",
                tmp,
                source={"subagent": {"thread_spawn": {"parent_thread_id": "foreign"}}},
            )
            _conversation_rollout(tmp, "middle", [*intervening, conflict], parent="root")
            _conversation_rollout(
                tmp, "grandchild", [_codex_user("PRIVATE grandchild")], parent="middle"
            )
            _conversation_rollout(tmp, "sibling", [_codex_user("allowed")], parent="root")
            store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
            _conversation_error(store, "root", "middle", "invalid_execution")
            _conversation_error(store, "root", "grandchild", "invalid_execution")
            # The corrupt middle is not an ancestor of the sibling. Its body
            # must not be scanned just because its head was discovered.
            result = store.conversation_source("root", "sibling")
            assert result["records"][0]["parts"][0]["text"] == "allowed"


def test_codex_conversation_ancestor_tail_claims_are_fresh_but_text_is_not_snapshotted():
    from opentab.conversation import read_jsonl

    with tempfile.TemporaryDirectory() as tmp:
        _conversation_rollout(tmp, "root")
        middle = _conversation_rollout(tmp, "middle", parent="root")
        _conversation_rollout(tmp, "grandchild", [_codex_user("grandchild")], parent="middle")
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        before = store.conversation_source("root", "grandchild")
        for kind in ("text", "same-claim", "conflicting-claim"):

            def mutate(paths, kind=kind):
                result = read_jsonl(paths)
                parent = "foreign" if kind == "conflicting-claim" else "root"
                row = (
                    _codex_user("PRIVATE more ancestor text")
                    if kind == "text"
                    else _codex_meta(
                        "middle",
                        tmp,
                        source={"subagent": {"thread_spawn": {"parent_thread_id": parent}}},
                    )
                )
                with middle.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
                return result

            with patch("opentab.conversation.read_jsonl", side_effect=mutate):
                if kind == "conflicting-claim":
                    _conversation_error(store, "root", "grandchild", "source_changed")
                else:
                    assert store.conversation_source("root", "grandchild") == before
            if kind != "conflicting-claim":
                assert store.conversation_source("root", "grandchild") == before


def test_codex_conversation_ancestry_validation_has_bounded_safe_reads():
    with tempfile.TemporaryDirectory() as tmp:
        _conversation_rollout(tmp, "root", [_codex_user("PRIVATE " + "x" * 2048)])
        _conversation_rollout(tmp, "child", [_codex_user("child")], parent="root")
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        for constant, limit in (("MAX_LINE_BYTES", 1024), ("MAX_SOURCE_BYTES", 1024)):
            with patch("opentab.conversation." + constant, limit):
                _conversation_error(store, "root", "child", "conversation_too_large")


def test_codex_conversation_metadata_head_budget_is_bounded_and_explicit():
    with tempfile.TemporaryDirectory() as tmp:
        # A valid but huge first line must not make metadata-only discovery read
        # the whole transcript or silently identify a later session_meta record.
        path = Path(tmp) / f"rollout-{CODEX_SID}.jsonl"
        _write_jsonl(str(path), [_codex_user("x" * 65536), _codex_meta(CODEX_SID, tmp)])
        _conversation_rollout(tmp, "root")
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        _conversation_error(store, CODEX_SID, code="source_unavailable")
        result = store.conversation_source("root")
        assert result["records"] == []
        assert any("64 KiB" in note for note in result["limitations"])


def test_codex_conversation_propagates_shared_reader_limits_and_safe_errors():
    from opentab.conversation import ConversationError

    with tempfile.TemporaryDirectory() as tmp:
        _conversation_rollout(tmp, CODEX_SID, [_codex_user("PRIVATE selected")])
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        with patch(
            "opentab.conversation.read_jsonl",
            side_effect=ConversationError(
                "source_too_large", "Conversation source exceeds the read limit."
            ),
        ):
            _conversation_error(store, CODEX_SID, code="source_too_large")


def test_codex_conversation_relative_root_has_the_same_source_identity():
    with tempfile.TemporaryDirectory() as tmp:
        _conversation_rollout(tmp, CODEX_SID, [_codex_user("relative source")])
        args = type("Args", (), {"demo": False})()
        absolute = ot.CodexStore(tmp, args).conversation_source(CODEX_SID)
        relative = ot.CodexStore(os.path.relpath(tmp), args).conversation_source(CODEX_SID)
        assert relative == absolute


def test_codex_node_prompt_reads_exact_child_user_events_without_usage():
    ids = [str(i) * 8 + "-1111-1111-1111-111111111111" for i in range(1, 8)]
    root, child, sibling, grandchild, outside, empty, injected = ids
    prompt = (
        "  Review the implementation, not this misleading task label.\n"
        + ("  Keep indentation and the complete detailed instruction.\n" * 12)
        + "Finish with a concrete test plan.\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        for sid, parent, events in (
            (root, None, [_codex_user("Root prompt must never leak")]),
            (child, root, [_codex_user(" \n\t"), _codex_user(prompt), _codex_user("Later prompt")]),
            (
                sibling,
                root,
                [
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "UserMessage",
                                "content": [{"type": "text", "text": "Sibling\nreceived prompt"}],
                            },
                        },
                    }
                ],
            ),
            (grandchild, child, [_codex_user("Nested instruction")]),
            (outside, None, [_codex_user("Outside workflow prompt")]),
            (empty, root, []),
            (injected, root, []),
        ):
            source = (
                {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent,
                            "agent_nickname": "Misleading task label",
                        }
                    }
                }
                if parent
                else "cli"
            )
            rows = [_codex_meta(sid, tmp, source=source)]
            if sid in (child, injected):
                rows.append(
                    _codex_item(
                        "message",
                        role="user",
                        content=[
                            {"type": "input_text", "text": "Injected instructions, not a prompt"}
                        ],
                    )
                )
            _codex_rollout(tmp, sid, rows + events)
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        assert store.workflows() == []  # No usage/assistant rows needed for content.
        assert store.node_prompt(root, child) == prompt
        assert store.node_prompt(root, sibling) == "Sibling\nreceived prompt"
        assert store.node_prompt(root, grandchild) == "Nested instruction"
        assert store.node_timeline(root, empty) == []
        assert store.node_turn_content(root, empty) == {}
        for workflow_id, node_id in (
            (root, root),
            (root, outside),
            (root, empty),
            (root, injected),
            (root, child[:8]),
            (root, "missing"),
            ("missing", child),
            (child, sibling),
        ):
            assert store.node_prompt(workflow_id, node_id) is None
        assert store.workflows() == []  # Content reads do not widen accounting.
        store.demo = True
        with patch.object(store, "_parse", side_effect=AssertionError("demo read content")):
            assert store.node_prompt(root, child) is None
            assert store.node_timeline(root, child) is None
            assert store.node_turn_content(root, child) == {}


def test_codex_node_turns_and_content_exclude_siblings_and_grandchildren():
    ids = [str(i) * 8 + "-1111-1111-1111-111111111111" for i in range(1, 6)]
    root, child, sibling, nested, outside = ids
    with tempfile.TemporaryDirectory() as tmp:
        for sid, parent in (
            (root, None),
            (child, root),
            (sibling, root),
            (nested, child),
            (outside, None),
        ):
            source = (
                {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "cli"
            )
            _codex_rollout(
                tmp,
                sid,
                [
                    _codex_meta(sid, tmp, source=source),
                    _codex_user(sid),
                    _codex_turn("gpt-5-codex", tmp),
                    _codex_item(
                        "function_call",
                        name="shell",
                        call_id="reused",
                        arguments='{"command":"ls"}',
                    ),
                    _codex_item("function_call_output", call_id="reused", output=sid),
                    _codex_tokens(100, 10, 0, 110),
                    _codex_user("follow-up " + sid, ts="2025-10-03T14:52:00.000Z"),
                    _codex_item(
                        "message",
                        role="assistant",
                        content=[{"type": "output_text", "text": sid}],
                        ts="2025-10-03T14:52:01.000Z",
                    ),
                    _codex_tokens(200, 20, 0, 220, ts="2025-10-03T14:52:02.000Z"),
                ],
            )
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        workflows = store.workflows()
        before = store.message_timeline(root)
        for sid in (root, child, sibling, nested):
            rows = store.node_timeline(root, sid)
            assert len(rows) == 2
            assert [r["prompt_full"] for r in rows] == [sid, "follow-up " + sid]
            assert all(r["depth"] == 0 for r in rows)
            assert all(r["content_key"] in {r["content_key"] for r in before} for r in rows)
            trace = store.node_turn_content(root, sid)
            key = rows[0]["content_key"]
            assert set(trace) == {r["content_key"] for r in rows}
            assert trace[key][0]["output"] == sid
            assert store.node_turn_content(root, sid, key) == {key: trace[key]}
        for sid in (root, sibling, nested, outside):
            key = store.node_timeline(sid, sid)[0]["content_key"]
            assert store.node_turn_content(root, child, key) == {}
        for owner, node in (
            (root, outside),
            (child, sibling),
            (root, "missing"),
            ("missing", child),
        ):
            assert store.node_timeline(owner, node) is None
            assert store.node_turn_content(owner, node) == {}
        assert store.node_turn_content(root, child, "") == {}
        assert store.message_timeline(root) == before
        assert store.workflows() == workflows


def test_codex_store_dedupes_echo_attributes_models_and_rolls_up_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions", "2025", "10", "03")
        os.makedirs(root)
        # cwd is <repo>/sub but the repo root (.git) is <repo> -> must roll up.
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        # Two turns on gpt-5-codex, then one on gpt-5.5, each as a *cumulative* total.
        # Codex echoes the prior turn's count after each turn_context (equal total ->
        # must be skipped) and writes an info=null count first (no usage -> skipped).
        rows = [
            _codex_meta(CODEX_SID, sub),
            _codex_user("optimize the date formatter"),
            _codex_turn("gpt-5-codex", sub),
            {
                "timestamp": "t",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": None},
            },
            _codex_tokens(1000, 100, 800, 1100),  # turn 1 (delta = itself)
            _codex_turn("gpt-5-codex", sub),
            _codex_tokens(1000, 100, 800, 1100),  # echo of turn 1 -> skipped
            _codex_tokens(2200, 160, 1700, 2360),  # turn 2 (delta vs turn 1)
            _codex_turn("gpt-5.5", sub),
            _codex_tokens(2200, 160, 1700, 2360),  # echo of turn 2 -> skipped
            _codex_tokens(2700, 200, 1900, 2900),  # turn 3 on gpt-5.5
        ]
        _codex_rollout(root, CODEX_SID, rows)

        args = type("Args", (), {"demo": False})()
        store = ot.CodexStore(os.path.join(tmp, "sessions"), args)
        workflows = store.workflows()

        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == CODEX_SID
        assert w.title == "optimize the date formatter"  # first plain user message
        assert w.directory == repo  # folded to the git root, not the bare "sub"
        assert w.source == "Codex"
        assert w.subagents == 0  # Codex has no subagent tree
        assert w.total_cost == 0.0 and w.root_cost == 0.0  # recorded cost; $ reprices
        # the accepted deltas sum back to the final cumulative total (2900)
        assert w.total_tokens == 2900 and w.unpriced_tokens == 2900

        rows_out = {r["model_name"]: r for r in store.model_breakdown()}
        assert set(rows_out) == {"openai/gpt-5-codex", "openai/gpt-5.5"}  # provider-prefixed
        codex = rows_out["openai/gpt-5-codex"]
        assert codex["runs"] == 2  # the echo + null count did not inflate the count
        # OpenAI's input_tokens includes the cached read; we split it into uncached +
        # cache_read. turn1 (1000/800) + turn2 delta (1200/900): uncached 200+300=500.
        assert codex["unpriced_input"] == 500
        assert codex["unpriced_cache_read"] == 800 + 900
        assert codex["unpriced_output"] == 100 + 60
        # no subagents, so the root split equals the total split
        assert codex["root_unpriced_input"] == codex["unpriced_input"]
        five_five = rows_out["openai/gpt-5.5"]
        assert five_five["runs"] == 1
        assert (five_five["unpriced_input"], five_five["unpriced_cache_read"]) == (300, 200)

        # the (all-unpriced) usage reprices to a positive list-price estimate under $
        est = ot.api_equivalent_cost("openai/gpt-5-codex", 500, 160, 0, 1700, 0)
        assert est > 0

        # one flat depth-0 node; its model is the most-used one (gpt-5-codex, 2 runs)
        nodes = store.workflow_nodes(CODEX_SID)
        assert len(nodes) == 1
        assert nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["model_name"] == "openai/gpt-5-codex"
        assert nodes[0]["tokens_total"] == 2900  # root aggregates both models
        assert nodes[0]["cost"] == 0.0


def test_codex_splits_cumulative_cache_writes_without_inflating_token_totals():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_turn("gpt-5.6-sol", cwd),
            _codex_call("old_schema"),
            # Codex <0.145 omitted cache_write_input_tokens. Its existing accounting
            # remains unchanged: input splits only into fresh + cache read.
            _codex_tokens(100, 10, 40, 110),
            _codex_call("new_schema"),
            # Cumulative growth: input +200, read +60, write +60, output +20.
            _codex_tokens(300, 30, 100, 330, cache_write=60),
            _codex_tokens(300, 30, 100, 330, cache_write=60),  # duplicate echo
            _codex_call("after_compaction"),
            # A shrinking total is a reset, so this whole block is fresh usage.
            _codex_tokens(100, 10, 20, 110, cache_write=30),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())

        (w,) = store.workflows()
        assert w.total_tokens == 110 + 220 + 110
        (model,) = store.model_breakdown()
        assert model["model_name"] == "openai/gpt-5.6-sol"
        assert model["runs"] == 3  # the explicit-zero/old row counts; the echo does not
        assert model["unpriced_input"] == 60 + 80 + 50
        assert model["unpriced_cache_read"] == 40 + 60 + 20
        assert model["unpriced_cache_write"] == 0 + 60 + 30
        assert model["unpriced_output"] == 10 + 20 + 10
        # The four disjoint token categories still close on the authoritative total.
        assert (
            sum(
                model[k]
                for k in (
                    "unpriced_input",
                    "unpriced_cache_read",
                    "unpriced_cache_write",
                    "unpriced_output",
                )
            )
            == w.total_tokens
        )

        turns = store.message_timeline(CODEX_SID)
        assert [t["tokens_total"] for t in turns] == [110, 220, 110]
        assert [t["input"] for t in turns] == [60, 80, 50]
        assert [t["cache_write"] for t in turns] == [0, 60, 30]
        node = store.workflow_nodes(CODEX_SID)[0]
        assert node["tokens_cache_write"] == 90

        tools = {r["tool"]: r for r in store.tool_breakdown(CODEX_SID)}
        assert tools["old_schema"]["cache_write"] == 0
        assert tools["new_schema"]["cache_write"] == 60
        assert tools["after_compaction"]["cache_write"] == 30

        # GPT-5.6 bills writes at their own rate; moving them out of fresh input changes
        # dollars but never tokens. The bundled OpenAI rate is $4/$20/$0.40/$5 per M.
        estimated = ot.api_equivalent_cost("openai/gpt-5.6-sol", 190, 40, 0, 120, 90)
        assert abs(estimated - 0.002058) < 1e-12


def test_codex_title_takes_any_user_message_kind_and_collapses_newlines():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # Older rollouts omit "kind" on user_message; the title must still be picked up,
        # and a multi-line prompt (@file mentions) collapses to a single-line title.
        um = {
            "timestamp": "2025-10-03T14:51:05.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "fix\n@a.py:1\nthe bug"},
        }
        rows = [
            _codex_meta(CODEX_SID, cwd),
            um,
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(10, 5, 0, 15),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        assert store.workflows()[0].title == "fix @a.py:1 the bug"


def test_codex_completed_user_items_supply_titles_and_prompt_groups():
    with tempfile.TemporaryDirectory() as tmp:

        def completed(item, ts="2025-10-03T14:51:05.000Z"):
            return {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": item},
            }

        prompt = "fix\n@a.py:1\nthe bug"
        rows = [
            _codex_meta(CODEX_SID, tmp),
            _codex_item(
                "message",
                role="user",
                content=[
                    {
                        "type": "input_text",
                        "text": "<environment_context>injected</environment_context>",
                    }
                ],
            ),
            completed(None),
            completed({"type": "UserMessage", "content": None}),
            completed({"type": "UserMessage", "content": [None, {"type": "text", "text": 42}]}),
            completed({"type": "UserMessage", "content": [{"type": "text", "text": "  "}]}),
            completed(
                {"type": "AgentMessage", "content": [{"type": "text", "text": "not a prompt"}]}
            ),
            # The Responses API echo must not create a second prompt group.
            _codex_item("message", role="user", content=[{"type": "input_text", "text": prompt}]),
            completed(
                {
                    "type": "UserMessage",
                    "content": [
                        {"type": "text", "text": "fix\n@a.py:1"},
                        {"type": "image", "text": "not prompt text"},
                        {"type": "text", "text": "the bug"},
                    ],
                }
            ),
            _codex_turn("gpt-5-codex", tmp),
            _codex_tokens(10, 5, 0, 15),
            completed(
                {"type": "UserMessage", "content": [{"type": "text", "text": "then test it"}]},
                ts="2025-10-03T14:51:25.000Z",
            ),
            _codex_tokens(20, 10, 0, 30, ts="2025-10-03T14:51:30.000Z"),
        ]
        _codex_rollout(tmp, CODEX_SID, rows)
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        assert store.workflows()[0].title == "fix @a.py:1 the bug"
        assert [p["title"] for p in store._parse()[CODEX_SID]["prompts"]] == [
            prompt,
            "then test it",
        ]
        turns = store.message_timeline(CODEX_SID)
        assert [t["prompt_full"] for t in turns] == [prompt, "then test it"]
        assert turns[0]["prompt_id"] != turns[1]["prompt_id"]
        assert [t["tokens_total"] for t in turns] == [15, 15]


def test_codex_store_treats_a_shrinking_total_as_a_compaction_reset():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # The running total grows, then *shrinks* (context compaction): the smaller
        # total is fresh post-reset usage, not a duplicate -- so it is counted, added
        # on top of the pre-reset peak.
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(1000, 100, 800, 1100),  # peak
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(400, 30, 100, 430),  # reset: fresh usage of (400,30)
        ]
        _codex_rollout(root, CODEX_SID, rows)

        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        w = store.workflows()[0]
        # pre-reset 1100 + post-reset 430 (the reset block counts in full)
        assert w.total_tokens == 1100 + 430
        r = store.model_breakdown()[0]
        assert r["runs"] == 2
        assert r["unpriced_input"] == (1000 - 800) + (400 - 100)  # uncached, both blocks
        assert r["unpriced_cache_read"] == 800 + 100


def test_codex_joins_the_source_cycle_and_builds_a_resume_command():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _empty_opencode_db(db)
        cdir = os.path.join(tmp, "claude", "slug")
        os.makedirs(cdir)
        _write_jsonl(
            os.path.join(cdir, "s.jsonl"),
            [_claude_msg("s", "claude-opus-4-8", _usage(1, 1, 0, 0), uuid="u", cwd=tmp)],
        )
        xdir = os.path.join(tmp, "codex", "2025")
        os.makedirs(xdir)
        _codex_rollout(
            xdir,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, tmp),
                _codex_turn("gpt-5-codex", tmp),
                _codex_tokens(10, 5, 0, 15),
            ],
        )
        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": db,
                "claude_dir": os.path.join(tmp, "claude"),
                "codex_dir": os.path.join(tmp, "codex"),
                "demo": False,
            },
        )()
        # all three present -> the cycle is opencode / claude / codex / all
        assert ot.available_sources(args) == ["opencode", "claude", "codex"]
        assert ot.sources.source_cycle(args) == ["opencode", "claude", "codex", "all"]
        # the c key walks through Codex on the way to the merged view
        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "claude"
        assert app.next_source_name() == "Codex"
        app.source_key = "codex"
        assert app.next_source_name() == "all"

        # L copies a `codex resume <id>` command for a Codex session
        wf = workflow("0199-id", "2026-06-01 12:00:00", title="t", directory="/tmp/proj")
        wf.source = "Codex"
        assert app.resume_command(wf) == "cd /tmp/proj && codex resume 0199-id"


def test_codex_turns_timeline_from_cumulative_deltas():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_user("write the parser", ts="2025-10-03T14:51:05.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:51:10.000Z"),
            _codex_tokens(1000, 200, 100, 1200, ts="2025-10-03T14:51:20.000Z"),
            _codex_user("now add tests", ts="2025-10-03T14:52:00.000Z"),
            _codex_tokens(2500, 500, 600, 3000, ts="2025-10-03T14:52:30.000Z"),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        store.workflows()
        assert store.supports_turns(CODEX_SID)
        t = store.message_timeline(CODEX_SID)
        assert len(t) == 2  # one row per accepted cumulative delta
        assert [r["prompt_title"] for r in t] == ["write the parser", "now add tests"]
        assert t[0]["input"] == 900 and t[0]["cache_read"] == 100 and t[0]["output"] == 200
        assert t[1]["input"] == 1000 and t[1]["cache_read"] == 500 and t[1]["output"] == 300
        assert all(r["cost"] == 0.0 for r in t)  # Codex records none; "$" estimates
        assert t[0]["model_name"] == "openai/gpt-5-codex"


def test_codex_turns_carry_the_reasoning_effort_in_force_at_each_turn():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_user("write the parser", ts="2025-10-03T14:51:05.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:51:10.000Z", effort="high"),
            _codex_tokens(1000, 200, 100, 1200, ts="2025-10-03T14:51:20.000Z"),
            _codex_user("now add tests", ts="2025-10-03T14:52:00.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:52:10.000Z", effort="low"),
            _codex_tokens(2500, 500, 600, 3000, ts="2025-10-03T14:52:30.000Z"),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        store.workflows()
        assert [r["effort"] for r in store.message_timeline(CODEX_SID)] == ["high", "low"]

    # A rollout whose turn_context records no effort (an older Codex) ships "", which is
    # what drops the column rather than drawing a stripe of dashes.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, cwd),
                _codex_turn("gpt-5-codex", cwd),
                _codex_tokens(1000, 200, 100, 1200),
            ],
        )
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        store.workflows()
        assert [r["effort"] for r in store.message_timeline(CODEX_SID)] == [""]


def test_codex_tool_breakdown_attributes_turn_deltas_to_pending_calls():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_turn("gpt-5-codex", cwd),
            # Turn 1 calls two tools; its 1200-token delta splits 600/600.
            _codex_call("shell_command", ts="2025-10-03T14:51:12.000Z"),
            _codex_call("apply_patch", ts="2025-10-03T14:51:15.000Z", kind="custom_tool_call"),
            _codex_tokens(1000, 200, 100, 1200, ts="2025-10-03T14:51:20.000Z"),
            # The duplicate echo must not consume the next turn's pending calls.
            _codex_tokens(1000, 200, 100, 1200, ts="2025-10-03T14:51:21.000Z"),
            _codex_call("update_plan", ts="2025-10-03T14:52:10.000Z"),
            _codex_tokens(2500, 500, 600, 3000, ts="2025-10-03T14:52:30.000Z"),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        store.workflows()
        assert store.supports_tools(CODEX_SID)
        rows = {r["tool"]: r for r in store.tool_breakdown(CODEX_SID)}
        assert set(rows) == {"shell_command", "apply_patch", "update_plan"}
        assert rows["shell_command"]["tokens_total"] == 600
        assert rows["apply_patch"]["tokens_total"] == 600
        assert rows["update_plan"]["tokens_total"] == 1800  # turn 2's whole delta
        assert rows["update_plan"]["model_name"] == "openai/gpt-5-codex"


def test_codex_turn_content_reads_narration_reasoning_calls_and_their_own_outputs():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        huge = "x" * (TRACE_OUTPUT_CAP + 17)
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_turn("gpt-5.6-sol", cwd),
            _codex_item(
                "message",
                role="assistant",
                phase="commentary",
                content=[{"type": "output_text", "text": "I'll inspect it first."}],
            ),
            _codex_item(
                "reasoning",
                summary=[{"type": "summary_text", "text": "Check the parser."}],
                encrypted_content="opaque",
            ),
            _codex_item(
                "function_call",
                call_id="c1",
                name="shell_command",
                arguments=json.dumps({"command": "git diff --stat", "description": "the diff"}),
            ),
            _codex_item(
                "custom_tool_call",
                call_id="c2",
                name="apply_patch",
                input=json.dumps({"command": "apply it", "patch": "*** Begin Patch"}),
            ),
            _codex_item(
                "tool_search_call",
                call_id="c3",
                arguments={"query": "browser tools"},
            ),
            # Results can finish in either order; call_id, never position, owns them.
            _codex_item(
                "custom_tool_call_output",
                call_id="c2",
                output=[
                    {"type": "input_text", "text": "patch applied"},
                    {"type": "input_image", "image_url": "not retained"},
                ],
            ),
            _codex_item(
                "tool_search_output",
                call_id="c3",
                tools=[{"name": "browser.search"}],
            ),
            _codex_item("function_call_output", call_id="c1", output=huge),
            _codex_tokens(1000, 200, 100, 1200),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())

        store.workflows()  # ordinary corpus parse: keys and flags, never raw content
        (turn,) = store.message_timeline(CODEX_SID)
        assert turn["content_key"]
        assert turn["has_text"] is True and turn["has_reasoning"] is True
        assert store._sessions is not None
        assert "content" not in store._sessions[CODEX_SID]
        assert store.supports_turn_content(CODEX_SID) is True
        assert store.records_reasoning is True

        events = store.turn_content(CODEX_SID)[turn["content_key"]]
        assert [e["kind"] for e in events] == ["text", "reasoning", "tool", "tool", "tool"]
        assert events[0]["text"] == "I'll inspect it first."
        assert events[1]["text"] == "Check the parser."
        assert (events[2]["name"], events[2]["args"]) == ("shell_command", "git diff --stat")
        assert events[2]["params"] == [("description", "the diff")]
        assert len(events[2]["output"]) == TRACE_OUTPUT_CAP
        assert events[2]["output_dropped"] == 17
        assert (events[3]["name"], events[3]["args"]) == ("apply_patch", "apply it")
        assert events[3]["output"] == "patch applied\n(image)"
        assert (events[4]["name"], events[4]["args"]) == ("tool_search", "browser tools")
        assert events[4]["output"] == '{"name": "browser.search"}'
        full = store.turn_content(CODEX_SID, content_key=turn["content_key"])
        assert list(full) == [turn["content_key"]]
        assert full[turn["content_key"]][2]["output"] == huge
        assert full[turn["content_key"]][2]["output_dropped"] == 0


def test_codex_an_echo_consumes_neither_trace_content_nor_its_call_output():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_turn("gpt-5.6-sol", cwd),
            _codex_tokens(100, 20, 0, 120),
            _codex_item(
                "message",
                role="assistant",
                content=[{"type": "output_text", "text": "Still working."}],
            ),
            _codex_item(
                "function_call",
                call_id="c1",
                name="shell_command",
                arguments='{"command":"ls"}',
            ),
            _codex_tokens(100, 20, 0, 120),  # equal-total echo: consumes nothing
            _codex_item("function_call_output", call_id="c1", output="file.txt"),
            _codex_tokens(250, 50, 0, 300),
            _codex_item(
                "reasoning",
                summary=[{"type": "summary_text", "text": "Compacted."}],
            ),
            _codex_tokens(50, 10, 0, 60),  # shrinking total: a fresh accepted turn
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())

        turns = store.message_timeline(CODEX_SID)
        assert len(turns) == 3
        assert len({t["content_key"] for t in turns}) == 3
        assert [(t["has_text"], t["has_reasoning"]) for t in turns] == [
            (False, False),
            (True, False),
            (False, True),
        ]
        trace = store.turn_content(CODEX_SID)
        assert trace[turns[1]["content_key"]][-1]["output"] == "file.txt"
        assert trace[turns[2]["content_key"]][0]["text"] == "Compacted."
        key = turns[1]["content_key"]
        assert store.turn_content(CODEX_SID, content_key=key) == {key: trace[key]}


def test_codex_spawned_threads_fold_into_a_subagent_tree():
    parent_sid = "11111111-1111-1111-1111-111111111111"
    child_sid = "22222222-2222-2222-2222-222222222222"
    spawn = {
        "subagent": {
            "thread_spawn": {
                "parent_thread_id": parent_sid,
                "depth": 1,
                "agent_nickname": "researcher",
            }
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            parent_sid,
            [
                _codex_meta(parent_sid, cwd),
                _codex_user("plan the feature", ts="2025-10-03T14:51:05.000Z"),
                _codex_turn("gpt-5-codex", cwd),
                _codex_call("update_plan", ts="2025-10-03T14:51:12.000Z"),
                _codex_tokens(1000, 200, 0, 1200, ts="2025-10-03T14:51:20.000Z"),
            ],
        )
        _codex_rollout(
            root,
            child_sid,
            [
                _codex_meta(child_sid, cwd, source=spawn),
                _codex_user("  Received child task\n\n  Keep this indentation.\n"),
                _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:52:00.000Z"),
                _codex_call("shell_command", ts="2025-10-03T14:52:05.000Z"),
                _codex_tokens(400, 100, 0, 500, ts="2025-10-03T14:52:10.000Z"),
            ],
        )
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        rows = store.workflows()
        assert len(rows) == 1  # the spawned thread folded away
        w = rows[0]
        assert w.id == parent_sid and w.subagents == 1
        assert w.total_tokens == 1200 + 500  # subtree total
        nodes = store.workflow_nodes(parent_sid)
        assert [(n["depth"], n["agent"]) for n in nodes] == [(0, "-"), (1, "researcher")]
        assert nodes[1]["id"] == child_sid and nodes[1]["tokens_total"] == 500
        assert all("prompt" not in n and "prompt_full" not in n for n in nodes)
        with patch.object(store, "_parse", side_effect=AssertionError("unnecessary reparse")):
            assert store.node_prompt(parent_sid, child_sid) == (
                "  Received child task\n\n  Keep this indentation.\n"
            )
        # model rows: total covers the subtree, root_* only the parent's own share
        mrow = [r for r in store.model_breakdown() if r["root_id"] == parent_sid]
        assert len(mrow) == 1 and mrow[0]["tokens_total"] == 1700
        assert mrow[0]["unpriced_output"] == 300 and mrow[0]["root_unpriced_output"] == 200
        # Turns interleave the child's turn (agent-tagged); Tools cover the subtree.
        t = store.message_timeline(parent_sid)
        assert [(r["agent"], r["tokens_total"]) for r in t] == [("-", 1200), ("researcher", 500)]
        # Trace keys include the rollout filename, so the same call_id in parent and
        # child cannot collide; the root fetch covers every row in the subtree.
        trace = store.turn_content(parent_sid)
        assert t[0]["content_key"] != t[1]["content_key"]
        assert [trace[r["content_key"]][0]["name"] for r in t] == [
            "update_plan",
            "shell_command",
        ]
        tools = {r["tool"]: r["tokens_total"] for r in store.tool_breakdown(parent_sid)}
        assert tools == {"update_plan": 1200, "shell_command": 500}


def test_codex_ended_at_reflects_the_latest_activity_in_a_spawned_thread():
    parent_sid = "11111111-1111-1111-1111-111111111111"
    child_sid = "22222222-2222-2222-2222-222222222222"
    spawn = {
        "subagent": {
            "thread_spawn": {
                "parent_thread_id": parent_sid,
                "agent_nickname": "researcher",
            }
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            parent_sid,
            [
                _codex_meta(parent_sid, cwd, ts="2026-06-10T18:46:00.000Z"),
                _codex_turn("gpt-5-codex", cwd, ts="2026-06-10T18:46:05.000Z"),
                _codex_tokens(1000, 200, 0, 1200, ts="2026-06-10T18:46:10.000Z"),
            ],
        )
        _codex_rollout(
            root,
            child_sid,
            [
                _codex_meta(child_sid, cwd, ts="2026-06-10T18:47:00.000Z", source=spawn),
                _codex_turn("gpt-5-codex", cwd, ts="2026-06-10T19:10:00.000Z"),
                _codex_tokens(400, 100, 0, 500, ts="2026-06-10T19:10:05.000Z"),
            ],
        )
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        w = store.workflows()[0]

        # iso_to_local renders in the system's local TZ, so compare against its own
        # conversion of each raw UTC timestamp rather than a hardcoded wall-clock string.
        assert w.id == parent_sid
        assert w.created_at == iso_to_local("2026-06-10T18:46:00.000Z")
        assert w.ended_at == iso_to_local("2026-06-10T19:10:05.000Z")
        assert w.ended_at > w.created_at


def test_codex_ended_at_falls_back_to_created_at_when_nothing_later():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd, ts="2026-06-10T18:46:00.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2026-06-10T18:46:00.000Z"),
            _codex_tokens(10, 5, 0, 15, ts="2026-06-10T18:46:00.000Z"),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        w = store.workflows()[0]
        assert w.ended_at == w.created_at


def test_codex_survives_a_valid_json_line_that_is_not_an_object():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions", "2025", "10", "03")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        path = os.path.join(root, f"rollout-2025-10-03T16-51-03-{CODEX_SID}.jsonl")
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(100, 50, 0, 150),
        ]
        with open(path, "w") as fh:
            fh.write("[]\n" + '"hello"\n' + "0\n")
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        w = ot.CodexStore(
            os.path.join(tmp, "sessions"), type("Args", (), {"demo": False})()
        ).workflows()
        assert len(w) == 1 and w[0].total_tokens == 150


def test_codex_survives_a_token_count_json_parses_as_infinity():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions", "2025", "10", "03")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        path = os.path.join(root, f"rollout-2025-10-03T16-51-03-{CODEX_SID}.jsonl")
        with open(path, "w") as fh:
            for row in (_codex_meta(CODEX_SID, cwd), _codex_turn("gpt-5-codex", cwd)):
                fh.write(json.dumps(row) + "\n")
            fh.write(
                '{"timestamp": "2025-10-03T14:51:20.000Z", "type": "event_msg", "payload": '
                '{"type": "token_count", "info": {"total_token_usage": {"input_tokens": '
                '1e400, "output_tokens": 50, "cached_input_tokens": 0, "total_tokens": '
                "150}}}}\n"
            )
        store = ot.CodexStore(os.path.join(tmp, "sessions"), type("Args", (), {"demo": False})())
        w = store.workflows()
        # The backend survives, which is the point. The RECORD is skipped whole rather
        # than counted off its surviving components: every figure in it is cumulative,
        # so a half-trusted one becomes the baseline every later turn is measured
        # against (see test_codex_a_malformed_total_does_not_inflate_the_next_turn).
        # Nothing is really lost by skipping -- the next valid record's delta spans the
        # gap -- except when the bad record is the session's ONLY one, as here, which
        # then has no recorded usage at all and drops like any other usage-less session.
        assert w == [] and store.model_breakdown() == []


def test_codex_a_malformed_total_does_not_inflate_the_next_turn():
    # The malformed cumulative total must not become the next delta's baseline.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions", "2025", "10", "03")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, cwd),
                _codex_turn("gpt-5-codex", cwd),
                _codex_tokens(80, 20, 0, 100, ts="2025-10-03T14:51:20.000Z"),
                # A component JSON allows and no coercion can honour, mid-stream.
                _codex_tokens("nonsense", 100, 0, 250, ts="2025-10-03T14:51:30.000Z"),
                _codex_tokens(300, 100, 0, 400, ts="2025-10-03T14:51:40.000Z"),
            ],
        )
        store = ot.CodexStore(os.path.join(tmp, "sessions"), type("Args", (), {"demo": False})())
        (w,) = store.workflows()
        assert w.total_tokens == 400  # the deltas still close on the final total
        turns = store.message_timeline(w.id)
        assert [t["tokens_total"] for t in turns] == [100, 300]


def test_codex_a_malformed_cache_write_does_not_become_the_next_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, cwd),
                _codex_turn("gpt-5.6-sol", cwd),
                _codex_tokens(100, 20, 30, 120, cache_write=10),
                _codex_tokens(200, 40, 60, 240, cache_write="nonsense"),
                _codex_tokens(300, 60, 90, 360, cache_write=30),
            ],
        )
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        (w,) = store.workflows()
        turns = store.message_timeline(w.id)
        # The bad middle row is skipped whole; the final delta spans it from the last
        # trustworthy baseline rather than treating the bad write as a zero/reset.
        assert [t["tokens_total"] for t in turns] == [120, 240]
        assert [t["cache_write"] for t in turns] == [10, 20]
        assert w.total_tokens == 360


def test_codex_a_missing_write_after_a_present_one_preserves_the_cumulative_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, cwd),
                _codex_turn("gpt-5.6-sol", cwd),
                _codex_tokens(100, 10, 20, 110, cache_write=30),
                _codex_tokens(200, 20, 40, 220),  # downgraded/keyless writer
                _codex_tokens(300, 30, 60, 330, cache_write=60),
            ],
        )
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        (w,) = store.workflows()
        (model,) = store.model_breakdown()

        # The missing middle key contributes no known writes but must not reset the
        # cumulative write baseline. When the field returns, only 60 - 30 is new.
        assert [t["cache_write"] for t in store.message_timeline(w.id)] == [30, 0, 30]
        assert model["unpriced_cache_write"] == 60
        assert (
            sum(
                model[k]
                for k in (
                    "unpriced_input",
                    "unpriced_cache_read",
                    "unpriced_cache_write",
                    "unpriced_output",
                )
            )
            == w.total_tokens
            == 330
        )


def test_codex_a_write_that_outgrows_its_own_turns_input_is_clamped_not_negative():
    # A keyless middle record hides writes, so the returning delta can exceed its input.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, cwd),
                _codex_turn("gpt-5.6-sol", cwd),
                _codex_tokens(300, 30, 100, 330, cache_write=60),
                _codex_tokens(400, 40, 150, 440),  # keyless: hides this turn's writes
                _codex_tokens(450, 50, 160, 500, cache_write=120),
            ],
        )
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        (w,) = store.workflows()
        (model,) = store.model_breakdown()
        turns = store.message_timeline(w.id)
        # turn 3 reports +50 input and +60 write; the write is clamped to what is left
        # after its +10 cache read, and uncached never goes negative.
        assert [t["cache_write"] for t in turns] == [60, 0, 40]
        assert [t["input"] for t in turns] == [140, 50, 0]
        assert all(t["input"] >= 0 for t in turns)
        for t in turns:
            assert (
                t["input"] + t["cache_read"] + t["cache_write"] + t["output"] == t["tokens_total"]
            )
        assert model["unpriced_cache_write"] == 100  # the 120 the file ends on, less 20
        assert (
            sum(
                model[k]
                for k in (
                    "unpriced_input",
                    "unpriced_cache_read",
                    "unpriced_cache_write",
                    "unpriced_output",
                )
            )
            == w.total_tokens
            == 500  # the authoritative cumulative total is still exact
        )


def test_codex_a_falsy_or_fractional_component_is_distrusted_too():
    for bad in (False, None, 1.5, -5, float("inf"), (1 << 53) + 1, "9007199254740993"):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "sessions", "2025", "10", "03")
            os.makedirs(root)
            cwd = os.path.join(tmp, "repo")
            os.makedirs(cwd)
            _codex_rollout(
                root,
                CODEX_SID,
                [
                    _codex_meta(CODEX_SID, cwd),
                    _codex_turn("gpt-5-codex", cwd),
                    _codex_tokens(80, 20, 0, 100, ts="2025-10-03T14:51:20.000Z"),
                    _codex_tokens(bad, 100, 0, 250, ts="2025-10-03T14:51:30.000Z"),
                    _codex_tokens(300, 100, 0, 400, ts="2025-10-03T14:51:40.000Z"),
                ],
            )
            store = ot.CodexStore(
                os.path.join(tmp, "sessions"), type("Args", (), {"demo": False})()
            )
            (w,) = store.workflows()
            turns = store.message_timeline(w.id)
            assert [t["tokens_total"] for t in turns] == [100, 300], bad
            assert w.total_tokens == 400, bad
    # An ABSENT component stays a valid 0 -- absence is how an older schema differs, and
    # rejecting it would drop a whole session's usage over a field that was never there.
    assert ot.CodexStore._cumulative(0) == 0 and ot.CodexStore._cumulative("12") == 12
    # The bound sits exactly where safe_int's does, on BOTH sides of it: float() is lossy
    # right at the ceiling, so measuring the converted value let 2**53+1 in as 2**53 --
    # the rounding defeating the very guard it was being fed to.
    assert ot.CodexStore._cumulative(1 << 53) == 1 << 53
    assert ot.CodexStore._cumulative((1 << 53) + 1) is None


def test_an_archived_thread_keeps_its_spend_because_codex_moved_it_not_deleted_it():
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "codex")
        live = os.path.join(home, "sessions", "2025", "10", "03")
        # `codex archive` MOVES the rollout into the sibling archived_sessions/, flat.
        archived = os.path.join(home, "archived_sessions")
        os.makedirs(live)
        os.makedirs(archived)
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        other = "0199aa8e-1b9e-7912-bcd4-9b00c8733eaa"
        _codex_rollout(
            live,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, repo),
                _codex_turn("gpt-5-codex", repo),
                _codex_tokens(1000, 100, 800, 1100),
            ],
        )
        _codex_rollout(
            archived,
            other,
            [
                _codex_meta(other, repo),
                _codex_turn("gpt-5-codex", repo),
                _codex_tokens(500, 50, 400, 550),
            ],
        )

        args = type("Args", (), {"demo": False})()
        store = ot.CodexStore(os.path.join(home, "sessions"), args)
        workflows = {w.id: w for w in store.workflows()}
        assert set(workflows) == {CODEX_SID, other}
        assert workflows[other].total_tokens == 550
        # The archive is fingerprinted too, so archiving invalidates the warm cache.
        assert any(archived in path for path in store.cache_inputs())
        # And the status trio resolves an archived id rather than pricing it at $0.
        assert store.root_of(other) == other

        # A rollout present in BOTH trees (a restored backup) is read once: per-turn
        # usage is a delta off each file's own cumulative counter, so reading it twice
        # would report 2200 tokens for 1100 of work.
        _codex_rollout(
            archived,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, repo),
                _codex_turn("gpt-5-codex", repo),
                _codex_tokens(1000, 100, 800, 1100),
            ],
        )
        doubled = ot.CodexStore(os.path.join(home, "sessions"), args)
        assert {w.id: w.total_tokens for w in doubled.workflows()}[CODEX_SID] == 1100
        assert len(doubled._session_files(CODEX_SID)) == 1

        # An archive that does not exist adds no root, and a store pointed straight at
        # one does not pair with itself.
        bare = ot.CodexStore(os.path.join(tmp, "no-such", "sessions"), args)
        assert bare._roots() == [os.path.join(tmp, "no-such", "sessions")]
        assert ot.CodexStore(archived, args)._roots() == [archived]

        # A relative --codex-dir still finds its sibling: dirname("sessions") is "".
        cwd = os.getcwd()
        os.chdir(home)
        try:
            # (getcwd, not `home`: macOS resolves /var to /private/var on chdir.)
            assert ot.CodexStore("sessions", args)._roots()[1:] == [
                os.path.join(os.getcwd(), "archived_sessions")
            ]
        finally:
            os.chdir(cwd)


def test_a_fully_archived_codex_install_is_still_discovered():
    # Archiving the last live thread empties sessions/, and availability that looked
    # only there would drop Codex out of auto/all and out of `opentab cost`.
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "codex")
        archived = os.path.join(home, "archived_sessions")
        os.makedirs(archived)
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        _codex_rollout(
            archived,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, repo),
                _codex_turn("gpt-5-codex", repo),
                _codex_tokens(1000, 100, 800, 1100),
            ],
        )
        sessions = os.path.join(home, "sessions")  # never created
        args = type("Args", (), {"demo": False, "codex_dir": sessions})()
        assert ot.sources._codex_available(sessions) is True
        # ...and selecting it explicitly must not be refused for a missing sessions/.
        store, _loading = ot.sources.make_store(args, "codex")
        assert [w.id for w in store.workflows()] == [CODEX_SID]


def test_codex_node_prompt_refuses_conflicting_resumed_parent_claims():
    first, second, child = (str(i) * 8 + "-1111-1111-1111-111111111111" for i in (1, 2, 3))
    with tempfile.TemporaryDirectory() as tmp:
        for root in (first, second):
            _codex_rollout(tmp, root, [_codex_meta(root, tmp)])
        for i, root in enumerate((first, second)):
            source = {"subagent": {"thread_spawn": {"parent_thread_id": root}}}
            _write_jsonl(
                os.path.join(tmp, f"rollout-2025-10-0{i+3}-{child}.jsonl"),
                [
                    _codex_meta(child, tmp, source=source),
                    _codex_user(f"Private prompt from root {i}", ts=f"2025-10-0{4-i}T10:00:00Z"),
                ],
            )
        store = ot.CodexStore(tmp, type("Args", (), {"demo": False})())
        assert store.node_prompt(first, child) is None
        assert store.node_prompt(second, child) is None
        for root in (first, second):
            assert store.node_timeline(root, child) is None
            assert store.node_turn_content(root, child) == {}
