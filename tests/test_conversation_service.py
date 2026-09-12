"""Service orchestration over synthetic stores and a real, XDG-isolated FTS index."""

import json
import os
import sqlite3
import stat
import tempfile
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import opentab as ot
from opentab import conversation_search as search
from opentab import service as service_module
from opentab import state
from opentab.conversation import ConversationError

from tests._support import (
    FakeStore,
    _codex_meta,
    _write_jsonl,
    _write_opencode_db_with_turns,
    workflow,
)


class ConversationStore(FakeStore):
    def __init__(
        self,
        ids=("root",),
        *,
        harness="OpenCode",
        machine="test-machine",
        project="/synthetic/project",
        root_dir="/synthetic/conversations",
        db=None,
    ):
        items = [workflow(sid, "2020-01-01 12:00:00", directory=project) for sid in ids]
        for item in items:
            item.machine, item.source = machine, harness
        super().__init__(items)
        self.source_name, self._machine = harness, machine
        self.root_dir, self.db = root_dir, db
        self.sources, self.reads, self.probes = {}, [], []
        self.unsupported = set()
        self.failures = {}
        self.on_read = None
        for sid in ids:
            self.put(sid, "needle synthetic retained text")

    def put(self, root, *texts, execution=None, timestamps=None, snapshot="v1"):
        selected = execution or root
        self.sources[root, selected] = {
            "snapshot": snapshot,
            "execution_id": selected,
            "executions": [{"id": root, "parent_id": None}],
            "limitations": ["synthetic retained history only"],
            "ordering": "source order",
            "records": [
                {
                    "id": f"{selected}:record:{i}",
                    "message_id": f"{selected}:message:{i}",
                    "execution_id": selected,
                    "role": "user" if i % 2 == 0 else "assistant",
                    "timestamp": timestamps[i] if timestamps else "2026-09-11T12:00:00Z",
                    "origin": "recorded-message",
                    "source": {"file": "synthetic.jsonl", "line": i + 1},
                    "parts": [{"id": f"part:{i}", "text": text}],
                }
                for i, text in enumerate(texts)
            ],
        }
        executions = [
            {"id": child, "parent_id": None if child == root else root}
            for sid, child in self.sources
            if sid == root
        ]
        for (sid, _), source in self.sources.items():
            if sid == root:
                source["executions"] = deepcopy(executions)

    def supports_conversation(self, sid):
        self.probes.append(sid)
        return sid not in self.unsupported

    def conversation_source(self, root_id, execution_id=None):
        self.reads.append((root_id, execution_id))
        selected = execution_id or root_id
        if self.on_read:
            self.on_read(root_id, selected)
        if (root_id, selected) in self.failures:
            raise self.failures[root_id, selected]
        if (root_id, selected) not in self.sources:
            raise ConversationError("invalid_execution_id", "Execution no longer belongs to root")
        return deepcopy(self.sources[root_id, selected])


class ManifestConversationStore(ConversationStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.manifests = {item.id: 1 for item in self._workflows}

    def conversation_manifest(self, root_id):
        return deepcopy(self.manifests.get(root_id))


@contextmanager
def _isolated():
    with tempfile.TemporaryDirectory(prefix="opentab-conversation-service-") as directory:
        base = Path(directory)
        env = {
            f"XDG_{name}_HOME": str(base / name.lower())
            for name in ("CACHE", "CONFIG", "DATA", "STATE")
        }
        # Project paths are identifiers here, never real Git repositories or source files.
        with patch.dict(os.environ, env), patch.object(
            service_module, "git_root", side_effect=lambda value: value
        ), patch.object(
            service_module, "resolve_project_root", side_effect=lambda value: value
        ), patch.object(
            service_module.sources, "make_store", side_effect=AssertionError("source discovery")
        ):
            yield base / "cache" / "opentab" / "conversations" / "index.sqlite3"


def _service(*stores, allowed=True, no_state=False):
    store = stores[0] if len(stores) == 1 else ot.CombinedStore(list(stores))
    return ot.OpenTabService(
        store,
        SimpleNamespace(source="all", demo=False, no_state=no_state),
        allow_raw_content=allowed,
    )


def _key(store, sid="root"):
    harness = {"OpenCode": "opencode", "Claude Code": "claude", "Codex": "codex"}[store.source_name]
    return ot.SessionRef(store._machine, harness, sid).encode()


@contextmanager
def _error(code):
    try:
        yield
    except ot.ServiceError as exc:
        assert exc.code == code, (exc.code, code)
        assert "SECRET" not in str(exc)
    else:
        raise AssertionError(f"expected {code}")


def _saved(**values):
    assert state._write_state(values, state.state_path())


def test_raw_and_demo_gates_precede_validation_reads_reload_and_index_creation():
    with _isolated() as path:
        store = ConversationStore()
        service = _service(store, allowed=False)
        with patch.object(
            service, "_filtered", side_effect=AssertionError("catalog access")
        ), patch.object(service, "reload", side_effect=AssertionError("reload")), patch.object(
            search, "ConversationIndex", side_effect=AssertionError("index access")
        ):
            for operation in (
                lambda: service.index_conversations(project=[], rebuild="bad"),
                lambda: service.search_conversations(None, limit=False),
                lambda: service._conversation_scope(session=[]),
            ):
                with _error("raw_content_disabled"):
                    operation()
            service.allow_raw_content = True
            for obj in (service.args, store):
                obj.demo = True
                for operation in (
                    service.index_conversations,
                    lambda: service.search_conversations(None),
                ):
                    with _error("demo_unsupported"):
                        operation()
                obj.demo = False
        assert store.reads == store.probes == []
        assert not path.parent.exists()


def test_startup_reload_missing_search_and_status_never_autoindex_or_read_sources():
    with _isolated() as path:
        store = ConversationStore()
        service = _service(store)
        service.reload()
        service.list_sessions()
        with _error("index_not_built"):
            service.search_conversations("needle")
        assert search.index_status() == {
            "exists": False,
            "schema_version": None,
            "roots": 0,
            "passages": 0,
            "updated_at": None,
        }
        assert store.reads == []
        assert not path.parent.exists()


def test_exact_qualified_owners_and_ambiguous_native_ids_fail_before_index_reads():
    with _isolated():
        stores = [
            ConversationStore(harness="OpenCode", machine="one"),
            ConversationStore(harness="Claude Code", machine="one"),
            ConversationStore(harness="OpenCode", machine="two"),
        ]
        service = _service(*stores)
        for i, store in enumerate(stores):
            store.put("root", f"needle owner{i}")
            assert service.index_conversations(session=_key(store))["updated"] == 1
            assert store.reads == [("root", None)]
            hit = service.search_conversations("needle", session=_key(store))["hits"][0]
            assert hit["session_key"] == _key(store)
            assert hit["excerpt"] == f"needle owner{i}"
            store.reads.clear()
        with patch.object(search, "ConversationIndex", side_effect=AssertionError("index access")):
            for operation in (
                lambda: service.index_conversations(session="root"),
                lambda: service.search_conversations("needle", session="root"),
                lambda: service.search_conversations("needle", exclude_session="root"),
            ):
                with _error("ambiguous_session"):
                    operation()
        assert all(not store.reads for store in stores)


def test_duplicate_fully_qualified_owners_fail_closed_without_creating_index():
    with _isolated() as path:
        stores = [ConversationStore(), ConversationStore()]
        service = _service(*stores)
        for session in (None, "root", _key(stores[0])):
            for operation in (
                lambda session=session: service.index_conversations(session=session),
                lambda session=session: service.search_conversations("needle", session=session),
            ):
                with _error("ambiguous_session"):
                    operation()
        assert all(store.reads == store.probes == [] for store in stores)
        assert not path.parent.exists()


def test_saved_ignores_and_project_harness_machine_scopes_precede_lexical_ranking():
    for scope_name in ("ignored_sessions", "ignored_projects", "project", "harness", "machine"):
        with _isolated():
            visible = ConversationStore(("visible",), project="/synthetic/visible")
            hidden = ConversationStore(
                ("hidden",), harness="Claude Code", machine="other", project="/synthetic/hidden"
            )
            visible.put("visible", "alpha")
            hidden.put("hidden", "alpha beta")
            service = _service(visible, hidden)
            service.index_conversations()
            options = {}
            if scope_name == "ignored_sessions":
                _saved(ignored_sessions=[_key(hidden, "hidden")])
            elif scope_name == "ignored_projects":
                _saved(ignored_projects=["/synthetic/hidden"])
            else:
                options[scope_name] = {
                    "project": "/synthetic/visible",
                    "harness": "OpenCode",
                    "machine": "test-machine",
                }[scope_name]
            visible.reads.clear()
            hidden.reads.clear()
            result = service.search_conversations("alpha beta", limit=1, **options)
            assert result["match_mode"] == "any_term", scope_name
            assert [hit["native_id"] for hit in result["hits"]] == ["visible"]
            assert visible.reads == [("visible", "visible")]
            assert hidden.reads == [], scope_name


def test_index_scope_and_saved_ignores_exclude_sources_before_reading():
    with _isolated():
        store = ConversationStore(("visible", "ignored", "ignored-project"))
        store._workflows[-1].directory = "/synthetic/hidden"
        _saved(ignored_sessions=[_key(store, "ignored")], ignored_projects=["/synthetic/hidden"])
        service = _service(store)
        result = service.index_conversations(
            project="/synthetic/project", harness="opencode", machine="test-machine"
        )
        assert result["updated"] == 1 and result["complete"]
        assert store.reads == [("visible", None)]
        assert not service.search_conversations("needle", session="ignored")["hits"]
        service = _service(store, no_state=True)
        assert service.index_conversations()["updated"] == 2


def test_title_and_project_metadata_changes_are_excluded_before_and_or_ranking():
    for field in ("title", "project"):
        with _isolated() as path:
            store = ConversationStore(("stale", "current"))
            stale = store._workflows[0]
            stale.title = "alpha beta" if field == "title" else "Synthetic session"
            store.put("stale", "unrelated retained text" if field == "title" else "alpha beta")
            store.put("current", "alpha")
            service = _service(store)
            service.index_conversations()
            initial = service.search_conversations("alpha beta", limit=1)
            assert initial["match_mode"] == "all_terms"
            assert initial["hits"][0]["native_id"] == "stale"
            assert initial["stale_metadata_roots_skipped"] == 0
            sources, indexed_bytes = deepcopy(store.sources), path.read_bytes()

            if field == "title":
                stale.title = "Renamed synthetic session"
            else:
                stale.directory = "/synthetic/moved-project"
            service.reload()
            store.reads.clear()
            result = service.search_conversations("alpha beta", limit=1)
            assert result["match_mode"] == "any_term", field
            assert [hit["native_id"] for hit in result["hits"]] == ["current"]
            assert result["stale_metadata_roots_skipped"] == 1
            assert result["stale_executions_skipped"] == result["unindexed_roots"] == 0
            assert store.reads == [("current", "current")]
            selected = service.search_conversations("alpha beta", session=_key(store, "stale"))
            assert selected["hits"] == [] and selected["stale_metadata_roots_skipped"] == 1
            assert store.reads == [("current", "current")]
            assert store.sources == sources
            assert path.read_bytes() == indexed_bytes

            refreshed = service.index_conversations()
            assert refreshed["updated"] == refreshed["unchanged"] == 1
            assert service.search_conversations("alpha beta")["stale_metadata_roots_skipped"] == 0
            assert store.sources == sources


def test_match_fields_distinguish_title_only_body_only_and_combined_evidence():
    with _isolated():
        store = ConversationStore(("title-only", "body-only", "both"))
        expected = {
            "title-only": ("needle in title", "unrelated retained source text", ["title"]),
            "body-only": ("Synthetic session", "needle in retained source text", ["text"]),
            "both": ("needle in title", "needle in retained source text", ["text", "title"]),
        }
        for item in store._workflows:
            item.title, text, _ = expected[item.id]
            store.put(item.id, text)
        service = _service(store)
        service.index_conversations()
        result = service.search_conversations("needle")
        assert result["match_mode"] == "all_terms"
        hits = {hit["native_id"]: hit for hit in result["hits"]}
        assert set(hits) == set(expected)
        for sid, (title, text, fields) in expected.items():
            hit = hits[sid]
            assert hit["match_fields"] == fields
            assert hit["title"] == title and hit["excerpt"] == text
            assert hit["source"] == store.sources[sid, sid]["records"][0]["source"]
            assert ("needle" in hit["excerpt"]) == ("text" in fields)


def test_excluding_current_root_excludes_all_children_before_fallback():
    with _isolated():
        store = ConversationStore(("current", "other"))
        store.put("current", "alpha beta")
        store.put("current", "alpha beta", execution="child")
        store.put("other", "alpha")
        service = _service(store)
        service.index_conversations()
        for excluded in ("current", _key(store, "current")):
            store.reads.clear()
            result = service.search_conversations("alpha beta", exclude_session=excluded)
            assert result["match_mode"] == "any_term"
            assert [hit["native_id"] for hit in result["hits"]] == ["other"]
            assert store.reads == [("other", "other")]
        assert not service.search_conversations(
            "alpha", session="current", exclude_session="current"
        )["hits"]


def test_child_evidence_groups_per_root_by_default_and_per_record_in_selected_session():
    with _isolated():
        store = ConversationStore()
        store.put("root", "unrelated root text")
        store.put("root", "needle first", "needle second", execution="child")
        service = _service(store)
        service.index_conversations()
        result = service.search_conversations("needle")
        assert result["grouping"] == "root_session" and len(result["hits"]) == 1
        assert result["hits"][0]["execution_id"] == "child"
        store.reads.clear()
        result = service.search_conversations("needle", session=_key(store))
        assert result["grouping"] == "record" and len(result["hits"]) == 2
        assert store.reads == [("root", "child")]
        assert {hit["record_id"] for hit in result["hits"]} == {"child:record:0", "child:record:1"}
        for hit in result["hits"]:
            assert hit["session_key"] == _key(store)
            assert hit["anchor"] == hit["record_id"]
            assert hit["message_id"].startswith("child:message:")
            assert hit["limitations"] == ["synthetic retained history only"]


def test_message_dates_use_inclusive_utc_not_root_creation_and_exclude_unknown_dates():
    with _isolated():
        store = ConversationStore()
        stamps = [
            "2026-09-12T00:30:00+02:00",
            "2026-09-10T23:30:00-02:00",
            int(datetime(2026, 9, 11, 12, tzinfo=timezone.utc).timestamp() * 1000),
            "2026-09-11T23:30:00-02:00",
            None,
            "",
            "2026-09-11T12:00:00",
            "not-a-date",
        ]
        store.put("root", *(f"needle record{i}" for i in range(len(stamps))), timestamps=stamps)
        service = _service(store)
        service.index_conversations()
        result = service.search_conversations(
            "needle", session="root", since="2026-09-11", until="2026-09-11"
        )
        assert {hit["record_id"] for hit in result["hits"]} == {
            f"root:record:{i}" for i in range(3)
        }
        assert "UTC message" in result["date_scope"]
        assert not service.search_conversations("needle", since="2020-01-01", until="2020-01-01")[
            "hits"
        ]
        assert len(service.search_conversations("needle", session="root")["hits"]) == len(stamps)


def test_literal_query_top_limit_and_total_excerpt_character_budget():
    with _isolated():
        store = ConversationStore(("literal", "operator", "other"))
        store.put("literal", "alpha OR omega src/widget.ts C++ " + "x" * 900)
        store.put("operator", "alpha omega")
        store.put("other", "alpha")
        service = _service(store)
        service.index_conversations()
        result = service.search_conversations('"alpha" OR omega', limit=1, max_chars=17)
        assert result["match_mode"] == "all_terms"
        assert [hit["native_id"] for hit in result["hits"]] == ["literal"]
        assert result["returned_chars"] == 17 == len(result["hits"][0]["excerpt"])
        assert result["limited"]
        assert (
            service.search_conversations("src/widget.ts C++")["hits"][0]["native_id"] == "literal"
        )
        result = service.search_conversations("alpha", limit=2, max_chars=605)
        assert len(result["hits"]) == 2
        assert result["returned_chars"] == sum(len(hit["excerpt"]) for hit in result["hits"]) <= 605
        assert result["limited"]
        for item in store._workflows:
            store.put(item.id, "needle " + "x" * 393, snapshot="v2")
        service.index_conversations()
        result = service.search_conversations("needle", limit=3, max_chars=605)
        assert [len(hit["excerpt"]) for hit in result["hits"]] == [400, 205]
        assert result["returned_chars"] == 605 and result["limited"]


def test_invalid_query_scope_dates_and_limits_do_not_read_sources_or_create_index():
    with _isolated() as path:
        store = ConversationStore()
        service = _service(store)
        for query in (None, "", "x" * 1001, "...", " ".join(f"term{i}" for i in range(65))):
            with _error("invalid_arguments"):
                service.search_conversations(query)
        for name, values in (
            ("limit", (False, 0, 101, 1.5, "10")),
            ("max_chars", (True, 0, 120001, "10")),
            ("since", ("2026-02-30", "2026-9-01", True)),
            ("until", ("not-date",)),
            ("exclude_session", ([], "", "x" * 8193)),
            ("session", ([], "", "x" * 8193)),
            ("project", ([], "")),
            ("machine", (False, "")),
            ("harness", (1, "")),
        ):
            for value in values:
                with _error("invalid_arguments"):
                    service.search_conversations("needle", **{name: value})
        with _error("invalid_arguments"):
            service.search_conversations("needle", since="2026-09-12", until="2026-09-11")
        with _error("invalid_arguments"):
            service.index_conversations(rebuild=1)
        assert store.reads == store.probes == []
        assert not path.parent.exists()


def test_stale_deleted_moved_or_unreadable_execution_evidence_is_withheld_without_rewriting():
    for change in ("snapshot", "record_deleted", "moved", "oserror", "valueerror", "unsupported"):
        with _isolated() as path:
            store = ConversationStore(("root", "destination"))
            store.put("root", "unrelated")
            store.put("destination", "unrelated")
            store.put("root", "needle SECRET old evidence", execution="child")
            service = _service(store)
            service.index_conversations()
            before = path.read_bytes()
            if change == "snapshot":
                store.sources["root", "child"]["snapshot"] = "v2"
            elif change == "record_deleted":
                store.sources["root", "child"]["records"] = []
            elif change == "moved":
                store.sources["destination", "child"] = store.sources.pop(("root", "child"))
            elif change in ("oserror", "valueerror"):
                store.failures["root", "child"] = (OSError if change == "oserror" else ValueError)(
                    "SECRET source path"
                )
            else:
                store.unsupported.add("root")
            result = service.search_conversations("needle")
            assert result["hits"] == [], change
            assert result["stale_executions_skipped"] == 1, change
            assert "SECRET" not in json.dumps(result)
            assert path.read_bytes() == before


def test_refresh_always_reads_sources_but_rewrites_only_changed_root_snapshots():
    with _isolated() as path:
        store = ConversationStore(("root", "unchanged"))
        store.put("root", "oldtoken")
        service = _service(store)
        first = service.index_conversations()
        assert first["updated"] == 2 and first["unchanged"] == 0
        if os.name == "posix":
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert path.parent.stat().st_uid == os.getuid()
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        store.reads.clear()
        result = service.index_conversations()
        assert result["updated"] == 0 and result["unchanged"] == 2
        assert set(store.reads) == {("root", None), ("unchanged", None)}
        assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
        assert result["index"] == first["index"]
        store.put("root", "newtoken", snapshot="v2")
        store.reads.clear()
        with patch.object(
            search.ConversationIndex,
            "_delete_root",
            autospec=True,
            side_effect=search.ConversationIndex._delete_root,
        ) as delete:
            result = service.index_conversations()
        assert [call.args[1] for call in delete.call_args_list] == [_key(store)]
        assert result["updated"] == result["unchanged"] == 1
        assert set(store.reads) == {("root", None), ("unchanged", None)}
        assert path.read_bytes() != before
        assert service.search_conversations("newtoken")["hits"]
        with search.ConversationIndex() as index:
            assert not index.candidates("oldtoken", [_key(store)])["hits"]
        assert service.index_conversations(rebuild=True)["updated"] == 2


def test_source_manifests_skip_full_readers_and_invalidate_changes_ownership_and_rebuild():
    with _isolated():
        store = ManifestConversationStore(("root", "other"))
        service = _service(store)
        assert service.index_conversations()["updated"] == 2
        store.reads.clear()
        assert service.index_conversations()["unchanged"] == 2
        assert store.reads == []

        for token in ("modified", "resumed-added", "child-deleted", "source-replaced"):
            store.manifests["root"] = token
            store.put("root", token, snapshot=token)
            store.reads.clear()
            result = service.index_conversations(session=_key(store))
            assert result["updated"] == 1 and store.reads == [("root", None)]

        store.put("root", "root", snapshot="owner-v1")
        store.put("root", "child", execution="child", snapshot="child-v1")
        store.manifests["root"] = "ownership-v1"
        store.reads.clear()
        assert service.index_conversations(session=_key(store))["updated"] == 1
        assert store.reads == [("root", None), ("root", "child"), ("root", None)]
        del store.sources["root", "child"]
        store.put("root", "root", snapshot="owner-v2")
        store.manifests["root"] = "ownership-v2"
        store.reads.clear()
        assert service.index_conversations(session=_key(store))["updated"] == 1
        assert store.reads == [("root", None)]

        store.reads.clear()
        assert service.index_conversations(session=_key(store), rebuild=True)["updated"] == 1
        assert store.reads == [("root", None)]


def test_codex_manifest_survives_index_json_roundtrip_and_skips_second_full_read():
    sid = "0199aa8e-1b9e-7912-bcd4-9b00c8733ea6"
    other_sid = "0999aa8e-1b9e-7912-bcd4-9b00c8733ea6"
    with _isolated(), tempfile.TemporaryDirectory() as source_dir:
        path = Path(source_dir) / f"rollout-2026-09-12T12-00-00-{sid}.jsonl"
        _write_jsonl(
            path,
            [
                _codex_meta(sid, "/synthetic/project"),
                {
                    "timestamp": "2026-09-12T12:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "needle"},
                },
            ],
        )
        other = Path(source_dir) / f"rollout-2026-09-12T13-00-00-{other_sid}.jsonl"
        _write_jsonl(other, [_codex_meta(other_sid, "/unrelated")])
        store = ot.CodexStore(source_dir, SimpleNamespace(demo=False))
        item = workflow(sid, "2026-09-12 12:00:00", directory="/synthetic/project")
        item.machine, item.source = "test-machine", "Codex"
        store.workflows = lambda: [item]
        service = _service(store)
        key = ot.SessionRef("test-machine", "codex", sid).encode()
        with patch.object(store, "conversation_source", wraps=store.conversation_source) as reader:
            assert service.index_conversations()["updated"] == 1
            assert reader.call_count == 1
            reader.reset_mock()
            assert service.index_conversations()["unchanged"] == 1
            reader.assert_not_called()
        with search.ConversationIndex() as index:
            persisted = index.manifests()[key]
        assert isinstance(persisted[1][0][1], list)

        read_head = store._read_conversation_head
        mutated = False

        def mutate_unrelated_after_head(name):
            nonlocal mutated
            result = read_head(name)
            if name == str(other) and not mutated:
                mutated = True
                with other.open("a") as stream:
                    stream.write(json.dumps({"type": "unrelated"}) + "\n")
            return result

        with patch.object(
            store, "_read_conversation_head", side_effect=mutate_unrelated_after_head
        ), patch.object(store, "conversation_source", wraps=store.conversation_source) as reader:
            result = service.index_conversations()
            assert result["complete"] and result["unchanged"] == 1
            assert reader.call_count == 1
        with search.ConversationIndex() as index:
            assert index.candidates("needle", [key])["hits"]


@contextmanager
def _opencode_revision_service():
    with _isolated() as index_path:
        db = str(index_path.parents[3] / "source.db")
        _write_opencode_db_with_turns(db)
        with closing(sqlite3.connect(db)) as writer:
            writer.execute("pragma journal_mode=wal")
            for table in ("message", "part"):
                writer.execute(f"alter table {table} add column time_created integer default 1")
                writer.execute(f"alter table {table} add column time_updated integer default 1")
            writer.execute(
                "insert into session values ('s3', null, 'Other', '/elsewhere', null, 1)"
            )
            writer.execute("insert into message values ('m4', 's3', '{\"role\":\"user\"}', 1, 1)")
            for pid, mid, sid, text in (
                ("p1", "m1", "s1", "rootneedle"),
                ("p2", "m3", "s2", "childneedle"),
                ("p3", "m4", "s3", "otherneedle"),
            ):
                writer.execute(
                    "insert into part values (?, ?, ?, ?, 1, 1)",
                    (pid, mid, sid, json.dumps({"type": "text", "text": text})),
                )
            writer.commit()
            store = ot.Store(db, SimpleNamespace(demo=False))
            try:
                yield _service(store), store, writer
            finally:
                store.conn.close()


def test_opencode_refresh_skips_unchanged_roots_and_tracks_children_and_scoped_refreshes():
    with _opencode_revision_service() as (service, store, writer), patch.object(
        store, "conversation_source", wraps=store.conversation_source
    ) as reader:
        assert service.index_conversations()["updated"] == 2
        reader.reset_mock()
        assert service.index_conversations()["unchanged"] == 2
        reader.assert_not_called()
        writer.execute(
            'update part set data = \'{"type":"text","text":"newchild"}\', '
            "time_updated = 2 where id = 'p2'"
        )
        writer.commit()
        assert service.index_conversations(session="s3")["unchanged"] == 1
        reader.assert_not_called()
        assert service.index_conversations()["updated"] == 1
        assert [call.args for call in reader.call_args_list] == [("s1",), ("s1",), ("s1",)]
        assert reader.call_args_list[1].kwargs == {"execution_id": "s2"}
        assert service.search_conversations("newchild")["hits"][0]["execution_id"] == "s2"
        assert not service.search_conversations("childneedle")["hits"]

        for pid, text in (("p1", "newroot"), ("p3", "newother")):
            writer.execute(
                "update part set data = ?, time_updated = 3 where id = ?",
                (json.dumps({"type": "text", "text": text}), pid),
            )
        writer.commit()
        assert service.index_conversations(session="s1")["updated"] == 1
        assert not service.search_conversations("newother")["hits"]
        assert service.index_conversations(session="s3")["updated"] == 1
        assert service.search_conversations("newother")["hits"]

        # Direct SQL bypassing the writer's revision contract requires an explicit
        # rebuild, but search must still withhold the old text via live verification.
        writer.execute(
            'update part set data = \'{"type":"text","text":"manualrewrite"}\' where id = \'p3\''
        )
        writer.commit()
        stale = service.search_conversations("newother")
        assert not stale["hits"] and stale["stale_executions_skipped"] == 1
        assert service.index_conversations(session="s3", rebuild=True)["updated"] == 1
        assert service.search_conversations("manualrewrite")["hits"]


def test_opencode_refresh_allows_unrelated_commits_but_rejects_child_edits_during_read():
    with _opencode_revision_service() as (service, store, writer):
        assert service.index_conversations()["updated"] == 2
        writer.execute(
            'update part set data = \'{"type":"text","text":"newroot"}\', time_updated = 2 where id = \'p1\''
        )
        writer.commit()
        read = store.conversation_source

        def unrelated_commit(root_id, execution_id=None):
            source = read(root_id, execution_id)
            writer.execute("update part set time_updated = time_updated + 1 where id = 'p3'")
            writer.commit()
            return source

        with patch.object(store, "conversation_source", side_effect=unrelated_commit):
            result = service.index_conversations(session="s1")
        assert result["complete"] and result["updated"] == 1
        writer.execute("update part set time_updated = 3 where id = 'p1'")
        writer.commit()

        def child_commit(root_id, execution_id=None):
            source = read(root_id, execution_id)
            if execution_id == "s2":
                writer.execute("update part set time_updated = 3 where id = 'p2'")
                writer.commit()
            return source

        with patch.object(store, "conversation_source", side_effect=child_commit):
            result = service.index_conversations(session="s1")
        assert not result["complete"]
        assert [error["code"] for error in result["errors"]] == ["source_changed"]
        assert not service.search_conversations("newroot")["hits"]
        assert service.index_conversations(session="s1")["updated"] == 1


def test_manifest_mutation_during_read_fails_closed_and_is_not_blessed():
    with _isolated():
        store = ManifestConversationStore()
        service = _service(store)
        service.index_conversations()
        store.manifests["root"] = "before"

        def mutate(_root, _selected):
            store.manifests["root"] = "after"
            store.on_read = None

        store.on_read = mutate
        result = service.index_conversations()
        assert not result["complete"]
        assert result["errors"] == [{"session_key": _key(store), "code": "source_changed"}]
        assert result["index"]["roots"] == 0
        store.reads.clear()
        assert service.index_conversations()["updated"] == 1
        assert store.reads == [("root", None)]


def test_global_manifest_token_is_persisted_per_root_under_scoped_refreshes():
    with _isolated():
        store = ManifestConversationStore(("one", "two"))
        store.global_token = 1
        store.conversation_manifest = lambda _root: store.global_token
        service = _service(store)
        service.index_conversations()
        store.global_token = 2
        store.put("one", "changed one", snapshot="two")
        store.put("two", "changed two", snapshot="two")
        store.reads.clear()
        assert service.index_conversations(session=_key(store, "one"))["updated"] == 1
        assert store.reads == [("one", None)]
        store.reads.clear()
        assert service.index_conversations(session=_key(store, "one"))["unchanged"] == 1
        assert store.reads == []
        assert service.index_conversations(session=_key(store, "two"))["updated"] == 1
        assert store.reads == [("two", None)]


def test_reader_version_change_invalidates_manifest_shortcut():
    from opentab import conversation

    with _isolated() as path:
        store = ManifestConversationStore()
        service = _service(store)
        service.index_conversations()
        store.put("root", "reprojected semantic token", snapshot="v1")
        store.reads.clear()
        with patch.object(
            conversation,
            "CONVERSATION_READER_VERSION",
            conversation.CONVERSATION_READER_VERSION + 1,
        ):
            assert service.index_conversations()["updated"] == 1
        assert store.reads == [("root", None)]
        with search.ConversationIndex(path) as index:
            assert index.candidates("reprojected", [_key(store)])["hits"]
            assert not index.candidates("needle", [_key(store)])["hits"]


def test_child_snapshot_change_refreshes_root_and_preserves_nochange_write_contract():
    with _isolated() as path:
        store = ConversationStore()
        store.put("root", "oldchild", execution="child")
        service = _service(store)
        service.index_conversations()
        before = path.read_bytes()
        store.reads.clear()
        assert service.index_conversations()["unchanged"] == 1
        assert store.reads == [("root", None), ("root", "child"), ("root", None)]
        assert path.read_bytes() == before
        store.put("root", "newchild", execution="child", snapshot="child-v2")
        assert service.index_conversations()["updated"] == 1
        assert service.search_conversations("newchild")["hits"][0]["execution_id"] == "child"
        with search.ConversationIndex() as index:
            assert not index.candidates("oldchild", [_key(store)])["hits"]


def test_failed_full_root_refresh_removes_old_text_and_reports_incomplete_safely():
    for failed_execution in ("root", "child"):
        for failure in (
            OSError("SECRET path"),
            ValueError("SECRET parse"),
            ConversationError("source_changed", "SECRET detail"),
        ):
            with _isolated():
                store = ConversationStore(("root", "healthy"))
                store.put("root", "oldtoken root")
                store.put("root", "oldtoken child", execution="child")
                service = _service(store)
                service.index_conversations()
                store.failures["root", failed_execution] = failure
                result = service.index_conversations()
                assert not result["complete"]
                assert result["errors"] == [
                    {
                        "session_key": _key(store),
                        "code": failure.code
                        if isinstance(failure, ConversationError)
                        else "conversation_unavailable",
                    }
                ]
                assert result["unchanged"] == 1 and result["index"]["roots"] == 1
                assert "SECRET" not in json.dumps(result)
                with search.ConversationIndex() as index:
                    assert not index.candidates("oldtoken", [_key(store)])["hits"]


def test_newly_unsupported_root_removes_old_indexed_text_and_reports_incomplete():
    with _isolated():
        store = ConversationStore()
        service = _service(store)
        service.index_conversations()
        store.unsupported.add("root")
        store.reads.clear()
        result = service.index_conversations()
        assert not result["complete"] and result["unsupported"] == 1
        assert result["index"]["roots"] == result["index"]["passages"] == 0
        assert not store.reads


def test_capability_probe_failure_removes_old_text_and_reports_incomplete_safely():
    with _isolated():
        store = ConversationStore()
        service = _service(store)
        service.index_conversations()
        with patch.object(
            store, "supports_conversation", side_effect=OSError("SECRET source path")
        ):
            result = service.index_conversations()
        assert not result["complete"]
        assert result["errors"] == [
            {"session_key": _key(store), "code": "conversation_unavailable"}
        ]
        assert result["index"]["roots"] == result["index"]["passages"] == 0
        assert "SECRET" not in json.dumps(result)


def test_pruning_respects_loaded_domains_and_project_harness_machine_scopes():
    for options in ({"project": "/synthetic/a"}, {"harness": "OpenCode"}, {"machine": "one"}):
        with _isolated():
            stores = [
                ConversationStore(("gone",), machine="one", project="/synthetic/a"),
                ConversationStore(("other-project",), machine="one", project="/synthetic/b"),
                ConversationStore(
                    ("other-harness",), harness="Claude Code", machine="one", project="/synthetic/a"
                ),
                ConversationStore(("other-machine",), machine="two", project="/synthetic/a"),
            ]
            service = _service(*stores)
            service.index_conversations()
            for store in stores:
                store._workflows.clear()
                store.reads.clear()
            result = service.index_conversations(**options)
            expected = {
                "project": {_key(stores[1], "other-project")},
                "harness": {_key(stores[2], "other-harness")},
                "machine": {_key(stores[3], "other-machine")},
            }[next(iter(options))]
            with search.ConversationIndex() as index:
                assert {row["session_key"] for row in index.roots()} == expected
            assert result["removed"] == 3
            assert all(not store.reads for store in stores)
    with _isolated():
        local, unloaded = ConversationStore(), ConversationStore(harness="Claude Code")
        _service(local, unloaded).index_conversations()
        local._workflows.clear()
        result = _service(local).index_conversations()
        assert result["removed"] == 1
        with search.ConversationIndex() as index:
            assert [row["session_key"] for row in index.roots()] == [_key(unloaded)]


def test_refresh_prunes_now_ignored_roots_but_session_scope_preserves_other_roots():
    with _isolated():
        store = ConversationStore(("selected", "ignored", "ignored-project"))
        store._workflows[-1].directory = "/synthetic/hidden"
        service = _service(store)
        service.index_conversations()
        _saved(ignored_sessions=[_key(store, "ignored")], ignored_projects=["/synthetic/hidden"])
        store.reads.clear()
        result = service.index_conversations(session=_key(store, "ignored"))
        assert result["removed"] == 1 and result["index"]["roots"] == 2
        assert not store.reads
        result = service.index_conversations()
        assert result["removed"] == 1 and result["index"]["roots"] == 1
        assert store.reads == [("selected", None)]
        with search.ConversationIndex() as index:
            assert [row["native_id"] for row in index.roots()] == ["selected"]


def test_status_is_counts_only_without_raw_source_reads_or_write_side_effects():
    with _isolated() as path:
        store = ConversationStore()
        store.put("root", "SECRET raw contents")
        _service(store).index_conversations()
        before = path.read_bytes()
        store.reads.clear()
        with patch.object(store, "conversation_source", side_effect=AssertionError("raw read")):
            status = search.index_status()
        assert set(status) == {"exists", "schema_version", "roots", "passages", "updated_at"}
        assert status["roots"] == status["passages"] == 1
        assert "SECRET" not in json.dumps(status)
        assert path.read_bytes() == before and not store.reads


def test_query_live_source_budget_has_twenty_floor_and_one_hundred_ceiling():
    with _isolated() as path:
        store = ConversationStore(tuple(f"root{i:03}" for i in range(105)))
        service = _service(store)
        service.index_conversations()
        for source in store.sources.values():
            source["snapshot"] = "stale"
        before = path.read_bytes()
        for limit, expected in ((1, 20), (10, 30), (100, 100)):
            store.reads.clear()
            result = service.search_conversations("needle", limit=limit)
            assert not result["hits"]
            assert len(store.reads) == len(set(store.reads)) == expected
            assert result["stale_executions_skipped"] == expected
            assert result["limited"]
            assert path.read_bytes() == before


def test_root_live_activity_during_child_read_never_persists_a_mixed_snapshot():
    with _isolated():
        store = ConversationStore()
        store.put("root", "oldtoken root")
        store.put("root", "oldtoken child", execution="child")
        service = _service(store)
        service.index_conversations()

        def mutate_root(root, selected):
            if selected == "child":
                store.sources[root, root]["snapshot"] = "root-live-v2"
                store.sources[root, root]["records"][0]["parts"][0]["text"] = "newtoken live root"
                store.on_read = None

        store.on_read = mutate_root
        result = service.index_conversations()
        assert not result["complete"]
        assert result["errors"] == [{"session_key": _key(store), "code": "source_changed"}]
        assert result["index"]["roots"] == result["index"]["passages"] == 0
        with search.ConversationIndex() as index:
            assert not index.candidates("oldtoken newtoken", [_key(store)])["hits"]
        result = service.index_conversations()
        assert result["complete"] and result["updated"] == 1
        assert service.search_conversations("newtoken")["hits"][0]["execution_id"] == "root"


def test_indexing_another_source_location_does_not_prune_same_harness_machine_history():
    for field in ("db", "root_dir"):
        with _isolated():
            first = ConversationStore(("root-a",), **{field: "/synthetic/source-a"})
            second = ConversationStore(("root-b",), **{field: "/synthetic/source-b"})
            first_service, second_service = _service(first), _service(second)
            first_service.index_conversations()
            first.reads.clear()
            result = second_service.index_conversations()
            assert result["removed"] == 0 and result["updated"] == 1, field
            assert result["index"]["roots"] == 2
            assert first.reads == [] and second.reads == [("root-b", None)]
            with search.ConversationIndex() as index:
                roots = index.roots()
                assert {row["session_key"] for row in roots} == {
                    _key(first, "root-a"),
                    _key(second, "root-b"),
                }
                assert len({row["source_id"] for row in roots}) == 2
                assert all(row["source_id"] for row in roots)
            assert first_service.search_conversations("needle")["hits"][0]["native_id"] == "root-a"
            assert second_service.search_conversations("needle")["hits"][0]["native_id"] == "root-b"
    with _isolated():
        unknown = ConversationStore(root_dir=None)
        service = _service(unknown)
        service.index_conversations()
        unknown._workflows.clear()
        result = service.index_conversations()
        assert result["removed"] == 0 and result["index"]["roots"] == 1
        with search.ConversationIndex() as index:
            assert index.roots()[0]["source_id"] is None


def test_native_session_refresh_prunes_only_its_resolved_qualified_identity():
    with _isolated():
        opencode = ConversationStore(root_dir="/synthetic/opencode")
        claude = ConversationStore(harness="Claude Code", root_dir="/synthetic/claude")
        service = _service(opencode, claude)
        service.index_conversations()
        claude._workflows.clear()
        service.reload()
        assert service.resolve_session("root").ref.encode() == _key(opencode)
        opencode.reads.clear()
        claude.reads.clear()
        result = service.index_conversations(session="root")
        assert result["removed"] == 0 and result["unchanged"] == 1
        assert result["index"]["roots"] == 2
        assert opencode.reads == [("root", None)] and claude.reads == []
        with search.ConversationIndex() as index:
            assert {row["session_key"] for row in index.roots()} == {_key(opencode), _key(claude)}
            assert index.candidates("needle", [_key(claude)])["hits"]


def test_session_scope_resolves_after_refreshing_the_catalog():
    with _isolated():
        store = ManifestConversationStore()
        service = _service(store)
        added = workflow("new-root", "2026-09-12 12:00:00", directory="/synthetic/project")
        added.machine, added.source = store._machine, store.source_name
        store._workflows.append(added)
        store.put("new-root", "newly discovered retained text")
        store.manifests["new-root"] = 1
        result = service.index_conversations(session="new-root")
        assert result["updated"] == 1
        assert store.reads == [("new-root", None)]


def test_concurrent_title_replacement_between_roots_and_candidates_withholds_phantom_hit():
    with _isolated():
        store = ConversationStore()
        store.put("root", "unrelated retained source text")
        service = _service(store)
        service.index_conversations()
        sources = deepcopy(store.sources)
        store.reads.clear()
        read_roots = search.ConversationIndex.roots

        def replace_after_roots(reader):
            roots = read_roots(reader)
            assert roots[0]["title"] == "root"
            metadata = {**roots[0], "title": "needle concurrent title"}
            with search.ConversationIndex(write=True) as writer:
                assert writer._db is not reader._db
                assert writer.replace_root(metadata, [sources["root", "root"]])["changed"]
            return roots

        with patch.object(
            search.ConversationIndex, "roots", autospec=True, side_effect=replace_after_roots
        ) as roots:
            result = service.search_conversations("needle")
        assert roots.call_count == 1
        assert result["hits"] == []
        assert result["stale_metadata_roots_skipped"] == 1
        assert result["stale_executions_skipped"] == 0
        assert store.reads == [] and store.sources == sources
        assert store._workflows[0].title == "root"
        with search.ConversationIndex() as index:
            candidate = index.candidates("needle", [_key(store)])["hits"][0]
            assert candidate["metadata"]["title"] == "needle concurrent title"
            assert candidate["match_fields"] == ["title"]


def test_root_grouping_before_candidate_cap_keeps_other_root_after_a_thousand_matches():
    with _isolated():
        store = ConversationStore(("root-a", "root-b"))
        store.put("root-a", *(["needle"] * 1001))
        store.put("root-b", "needle filler")
        service = _service(store)
        assert service.index_conversations()["index"]["passages"] == 1002
        with search.ConversationIndex() as index:
            ungrouped = index.candidates("needle", [_key(store, "root-a"), _key(store, "root-b")])
            assert len(ungrouped["hits"]) == 1000 and ungrouped["limited"]
            assert {hit["session_key"] for hit in ungrouped["hits"]} == {_key(store, "root-a")}
        store.reads.clear()
        result = service.search_conversations("needle", limit=2)
        assert result["grouping"] == "root_session"
        assert [hit["native_id"] for hit in result["hits"]] == ["root-a", "root-b"]
        assert store.reads == [("root-a", "root-a"), ("root-b", "root-b")]


def test_tiny_final_excerpt_budget_recenters_on_body_match():
    with _isolated():
        store = ConversationStore()
        text = "filler " * 100 + "needle " + "tail " * 100
        store.put("root", text)
        service = _service(store)
        service.index_conversations()
        result = service.search_conversations("needle", max_chars=20)
        assert len(result["hits"]) == 1
        hit = result["hits"][0]
        assert "needle" in hit["excerpt"]
        assert len(hit["excerpt"]) == result["returned_chars"] == 20
        assert hit["excerpt"] in text
        assert hit["match_fields"] == ["text"]
        assert hit["excerpt_truncated"] and result["limited"]


def test_embedded_nul_before_match_does_not_shift_excerpt_away_from_needle():
    with _isolated():
        store = ConversationStore()
        text = "padding " * 100 + "\0" + "padding " * 100 + "needle"
        store.put("root", text)
        service = _service(store)
        service.index_conversations()
        result = service.search_conversations("needle")
        assert len(result["hits"]) == 1
        hit = result["hits"][0]
        assert "needle" in hit["excerpt"]
        assert hit["excerpt"] in text
        assert hit["match_fields"] == ["text"]
        assert 0 < result["returned_chars"] == len(hit["excerpt"]) <= 600


def test_same_qualified_key_from_different_source_cannot_reuse_indexed_body():
    for field in ("db", "root_dir"):
        with _isolated() as path:
            first = ConversationStore(**{field: "/synthetic/source-a"})
            second = ConversationStore(**{field: "/synthetic/source-b"})
            first.put("root", "needle SECRET source A")
            second.put("root", "needle source B")
            assert _key(first) == _key(second)
            assert (
                first.sources["root", "root"]["snapshot"]
                == second.sources["root", "root"]["snapshot"]
            )
            first_service = _service(first)
            first_service.index_conversations()
            second_service = _service(second)
            before = path.read_bytes()
            result = second_service.search_conversations("needle")
            assert result["hits"] == [], field
            assert result["stale_metadata_roots_skipped"] == 1
            assert result["stale_executions_skipped"] == 0
            assert "SECRET" not in json.dumps(result)
            assert second.reads == [] and path.read_bytes() == before
            assert second_service.index_conversations()["updated"] == 1
            hit = second_service.search_conversations("needle")["hits"][0]
            assert hit["excerpt"] == "needle source B"
            assert hit["session_key"] == _key(second)
            assert first_service.search_conversations("needle")["hits"] == []


def test_excerpt_centers_actual_token_not_earlier_substring_with_default_and_tiny_budgets():
    with _isolated() as path:
        store = ConversationStore()
        text = "alphabetical " + "filler " * 100 + "alpha"
        store.put("root", text)
        service = _service(store)
        service.index_conversations()
        before = path.read_bytes()
        for options, expected_length in (({}, 600), ({"max_chars": 20}, 20)):
            result = service.search_conversations("alpha", **options)
            assert len(result["hits"]) == 1
            hit = result["hits"][0]
            assert hit["excerpt"].endswith(" alpha")
            assert "alphabetical" not in hit["excerpt"]
            assert hit["excerpt"] in text
            assert len(hit["excerpt"]) == result["returned_chars"] == expected_length
            assert hit["match_fields"] == ["text"]
            assert hit["excerpt_truncated"] == bool(options)
        assert path.read_bytes() == before


def test_stale_best_root_execution_falls_back_to_fresh_child_in_global_search():
    with _isolated() as path:
        store = ConversationStore()
        store.put("root", "needle")
        store.put("root", "needle filler", execution="child")
        service = _service(store)
        service.index_conversations()
        initial = service.search_conversations("needle", limit=1)
        assert initial["hits"][0]["execution_id"] == "root"
        child = deepcopy(store.sources["root", "child"])
        before = path.read_bytes()
        store.sources["root", "root"]["snapshot"] = "root-v2"
        store.sources["root", "root"]["records"][0]["parts"][0]["text"] = "unrelated new root body"
        store.reads.clear()
        result = service.search_conversations("needle", limit=1)
        assert result["grouping"] == "root_session" and len(result["hits"]) == 1
        hit = result["hits"][0]
        assert hit["session_key"] == _key(store) and hit["execution_id"] == "child"
        assert hit["excerpt"] == "needle filler"
        assert hit["record_id"] == "child:record:0" and hit["match_fields"] == ["text"]
        assert result["stale_executions_skipped"] >= 1
        assert result["stale_metadata_roots_skipped"] == 0
        assert store.reads == [("root", "root"), ("root", "child")]
        assert store.sources["root", "child"] == child
        assert path.read_bytes() == before
