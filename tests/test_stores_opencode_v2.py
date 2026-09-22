import argparse
import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

from opentab.stores.opencode import Store
from opentab.stores.opencode_v2 import REQUIRED_SCHEMA_V2, install_views, scoped_detail_sql


def _session(sid, parent=None, *, title=None, tokens=(0, 0, 0, 0, 0), cost: float = 0, updated=20):
    return (
        sid,
        parent,
        title or sid,
        "/repo",
        "agent" if parent else None,
        json.dumps({"id": "model", "providerID": "provider", "variant": "high"}),
        cost,
        *tokens,
        10,
        updated,
    )


def _message(mid, sid, kind, seq, data, *, created=None, updated=None):
    return (
        mid,
        sid,
        kind,
        seq,
        seq if created is None else created,
        seq if updated is None else updated,
        json.dumps(data),
    )


@contextmanager
def _v2_db(*, legacy=False, legacy_part=True, message_pk=True):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "opencode.db")
        writer = sqlite3.connect(path)
        writer.executescript(
            f"""
            create table session_v2 (
              id text primary key, parent_id text, title text, directory text, agent text,
              model text, cost real, tokens_input integer, tokens_output integer,
              tokens_reasoning integer, tokens_cache_read integer, tokens_cache_write integer,
              time_created integer, time_updated integer
            );
            create index session_v2_parent_idx on session_v2(parent_id);
            create table session_message (
              id text {"primary key" if message_pk else ""}, session_id text, type text, seq integer,
              time_created integer, time_updated integer, data text
            );
            create unique index session_message_session_seq_idx on session_message(session_id, seq);
            create index session_message_session_time_idx on session_message(session_id, time_created, id);
            create table event_sequence (aggregate_id text primary key, seq integer);
            """
        )
        if legacy:
            writer.executescript(
                """
                create table session (
                  id text primary key, parent_id text, title text, directory text,
                  agent text, time_created integer, time_updated integer
                );
                create table message (id text primary key, session_id text, data text);
                """
            )
            if legacy_part:
                writer.execute(
                    "create table part (id text primary key, message_id text, session_id text, data text)"
                )
        writer.commit()
        store = Store(path, argparse.Namespace(demo=False))
        try:
            yield writer, store
        finally:
            store.conn.close()
            writer.close()


def _populate_v2(writer):
    writer.executemany(
        "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            _session("root", tokens=(15, 5, 2, 3, 0), cost=1.5, updated=100),
            _session("child", "root", tokens=(4, 1, 0, 0, 0), cost=0, updated=90),
            _session("sparse", tokens=(2, 2, 0, 0, 0), updated=80),
        ],
    )
    patch = {
        "type": "tool",
        "id": "call-patch",
        "name": "patch",
        "state": {
            "status": "completed",
            "input": {"patchText": "patch input"},
            "content": [{"type": "text", "text": "patch result"}],
            "metadata": {
                "files": [
                    {
                        "file": "a.py",
                        "patch": "@@ -1 +1 @@\n-old\n+new\n",
                        "additions": 1,
                        "deletions": 1,
                        "status": "modified",
                    }
                ]
            },
        },
        "time": {"created": 30, "completed": 31},
    }
    edit = {
        "type": "tool",
        "id": "call-edit",
        "name": "edit",
        "state": {
            "status": "completed",
            "input": {"path": "b.py"},
            "content": [{"type": "text", "text": "edit result"}],
            "metadata": {
                "files": [
                    {
                        "file": "b.py",
                        "patch": "edit patch",
                        "additions": 2,
                        "deletions": 0,
                        "status": "modified",
                    }
                ]
            },
        },
        "time": {"created": 32, "completed": 33},
    }
    write = {
        "type": "tool",
        "id": "call-write",
        "name": "write",
        "state": {
            "status": "completed",
            "input": {"path": "missing.py", "content": "do not infer"},
            "content": [{"type": "text", "text": "written"}],
            "metadata": {},
        },
        "time": {"created": 34, "completed": 35},
    }
    migrated = {
        "type": "tool",
        "id": "call-old",
        "name": "apply_patch",
        "state": {
            "status": "completed",
            "input": {},
            "content": [{"type": "text", "text": "old result"}],
            "metadata": {
                "files": [
                    {
                        "filePath": "/repo/old.py",
                        "relativePath": "old.py",
                        "type": "update",
                        "patch": "old patch",
                        "additions": 3,
                        "deletions": 1,
                    }
                ]
            },
        },
        "time": {"created": 36, "completed": 37},
    }
    migrated_edit = {
        "type": "tool",
        "id": "call-old-edit",
        "name": "edit",
        "state": {
            "status": "completed",
            "input": {},
            "content": [{"type": "text", "text": "old edit result"}],
            "metadata": {
                "filediff": {
                    "file": "/repo/old-edit.py",
                    "patch": "old edit patch",
                    "additions": 1,
                    "deletions": 1,
                }
            },
        },
        "time": {"created": 38, "completed": 39},
    }
    writer.executemany(
        "insert into session_message values (?,?,?,?,?,?,?)",
        [
            _message(
                "u-root",
                "root",
                "user",
                5,
                {"time": {"created": 20}, "text": "actual root prompt", "files": []},
                created=20,
            ),
            _message(
                "a-root",
                "root",
                "assistant",
                20,
                {
                    "time": {"created": 30},
                    "agent": "build",
                    "model": {"id": "model", "providerID": "provider", "variant": "high"},
                    "cost": 1,
                    "tokens": {
                        "input": 10,
                        "output": 2,
                        "reasoning": 2,
                        "cache": {"read": 3, "write": 0},
                    },
                    "content": [
                        {"type": "reasoning", "text": "reasoning prose"},
                        patch,
                        edit,
                        write,
                        migrated,
                        migrated_edit,
                        {"type": "text", "text": "root answer"},
                    ],
                },
                created=30,
                updated=40,
            ),
            _message(
                "u-child",
                "child",
                "user",
                4,
                {"time": {"created": 25}, "text": "child instructions"},
                created=25,
            ),
            _message(
                "a-child",
                "child",
                "assistant",
                11,
                {
                    "time": {"created": 35},
                    "agent": "general",
                    "model": {"id": "model", "providerID": "provider"},
                    "cost": 0,
                    "tokens": {
                        "input": 4,
                        "output": 1,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                    "content": [{"type": "text", "text": "child answer"}],
                },
                created=35,
                updated=41,
            ),
            _message(
                "s-u",
                "sparse",
                "user",
                10,
                {"time": {"created": 500}, "text": "first by seq"},
                created=500,
            ),
            _message(
                "s-a1",
                "sparse",
                "assistant",
                30,
                {
                    "time": {"created": 900},
                    "model": {"id": "model", "providerID": "provider"},
                    "tokens": {"input": 1, "output": 1},
                    "content": [{"type": "text", "text": "one"}],
                },
                created=900,
            ),
            _message(
                "s-a2",
                "sparse",
                "assistant",
                70,
                {
                    "time": {"created": 100},
                    "model": {"id": "model", "providerID": "provider"},
                    "tokens": {"input": 1, "output": 1},
                    "content": [{"type": "text", "text": "two"}],
                },
                created=100,
            ),
        ],
    )
    writer.executemany(
        "insert into event_sequence values (?,?)", [("root", 20), ("child", 11), ("sparse", 70)]
    )
    writer.commit()


def test_opencode_v2_schema_and_full_store_surface():
    assert REQUIRED_SCHEMA_V2["session_message"][3] == "seq"
    assert {
        "cost",
        "tokens_input",
        "tokens_output",
        "tokens_reasoning",
        "tokens_cache_read",
        "tokens_cache_write",
    } <= set(REQUIRED_SCHEMA_V2["session_v2"])
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        workflows = {row.id: row for row in store.workflows()}
        assert set(workflows) == {"root", "sparse"}
        assert (workflows["root"].total_tokens, workflows["root"].subagents) == (30, 1)
        assert [row["id"] for row in store.recent_roots()] == ["root", "sparse"]
        assert store.root_of("child") == "root"
        assert [(row["id"], row["tokens_total"]) for row in store.workflow_nodes("root")] == [
            ("root", 25),
            ("child", 5),
        ]

        models = store.model_breakdown()
        residual = next(row for row in models if row["model_name"] == "unknown (session aggregate)")
        assert residual["root_id"] == "root" and residual["runs"] == 0
        assert (residual["tokens_total"], residual["cost"]) == (8, 0.5)
        assert sum(row["tokens_total"] for row in models if row["root_id"] == "root") == 30

        timeline = store.message_timeline("root")
        assert [row["content_key"] for row in timeline] == ["a-root", "a-child"]
        assert timeline[0]["prompt_full"] == "actual root prompt"
        assert timeline[0]["effort"] == "high"
        assert timeline[0]["tools"] == ["patch", "edit", "write", "apply_patch", "edit"]
        batch = store.message_timeline_all()["root"]
        assert [row["content_key"] for row in batch] == [row["content_key"] for row in timeline]
        assert all(not row["has_text"] and not row["has_reasoning"] for row in batch)
        child_timeline = store.node_timeline("root", "child")
        assert child_timeline is not None and child_timeline[0]["content_key"] == "a-child"
        assert store.node_prompt("root", "child") == "child instructions"

        tools = {row["tool"]: row for row in store.tool_breakdown("root")}
        assert set(tools) == {"patch", "edit", "write", "apply_patch"}
        assert tools["edit"]["calls"] == 2
        traces = store.turn_content("root", "a-root")["a-root"]
        assert traces[0] == {"kind": "reasoning", "text": "reasoning prose", "dropped": 0}
        assert (
            next(item for item in traces if item.get("name") == "patch")["output"] == "patch result"
        )

        source = store.conversation_source("root")
        assert source["records"][0]["parts"][0]["text"] == "actual root prompt"
        assert source["records"][1]["parts"][0]["text"] == "root answer"
        assert source["executions"] == [
            {"id": "child", "parent_id": "root"},
            {"id": "root", "parent_id": None},
        ]
        assert (
            store.conversation_source("root", "child")["records"][0]["parts"][0]["text"]
            == "child instructions"
        )
        manifest = store.conversation_manifest("root")
        assert manifest is not None and manifest[0] == "opencode-root-v2"

        try:
            store.conn.execute("delete from main.session_v2")
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower() or "read-only" in str(exc).lower()
        else:
            raise AssertionError("OpenCode source connection accepted a write")


def test_opencode_v2_seq_is_authoritative_and_sparse_for_conversation_and_turns():
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        source = store.conversation_source("sparse")
        assert [row["message_id"] for row in source["records"]] == ["s-u", "s-a1", "s-a2"]
        assert [row["content_key"] for row in store.message_timeline("sparse")] == ["s-a1", "s-a2"]
        assert [row["content_key"] for row in store.message_timeline_all()["sparse"]] == [
            "s-a1",
            "s-a2",
        ]


def test_opencode_v2_mixed_prefers_v2_identity_and_retains_legacy_only_sessions():
    with _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        writer.executemany(
            "insert into session values (?,?,?,?,?,?,?)",
            [
                ("root", None, "legacy duplicate", "/legacy", None, 1, 9999),
                ("legacy", None, "legacy only", "/legacy", None, 2, 50),
            ],
        )
        writer.executemany(
            "insert into message values (?,?,?)",
            [
                (
                    "legacy-duplicate",
                    "root",
                    json.dumps(
                        {
                            "role": "assistant",
                            "cost": 99,
                            "tokens": {"input": 999},
                            "time": {"created": 9999},
                        }
                    ),
                ),
                ("legacy-user", "legacy", json.dumps({"role": "user", "time": {"created": 1}})),
                (
                    "legacy-answer",
                    "legacy",
                    json.dumps(
                        {
                            "role": "assistant",
                            "cost": 2,
                            "tokens": {"input": 7, "output": 3},
                            "time": {"created": 2},
                        }
                    ),
                ),
            ],
        )
        writer.execute(
            "insert into part values ('legacy-text','legacy-user','legacy',?)",
            [json.dumps({"type": "text", "text": "legacy prompt"})],
        )
        writer.commit()
        workflows = {row.id: row for row in store.workflows()}
        assert workflows["root"].title == "root" and workflows["root"].total_tokens == 30
        assert workflows["legacy"].total_tokens == 10 and workflows["legacy"].total_cost == 2
        assert "legacy-duplicate" not in {
            row["content_key"] for row in store.message_timeline("root")
        }
        assert store.message_timeline("legacy")[0]["prompt_full"] == "legacy prompt"
        assert store.message_timeline_all()["legacy"][0]["prompt_full"] == "legacy prompt"
        assert (
            store.conversation_source("legacy")["records"][0]["parts"][0]["text"] == "legacy prompt"
        )


def test_opencode_v2_works_without_legacy_part_table():
    with _v2_db(legacy=True, legacy_part=False) as (writer, store):
        _populate_v2(writer)
        assert store.supports_tools("root") and store.supports_conversation("root")
        assert store.node_prompt("root", "child") == "child instructions"
        assert store.turn_content("root", "a-root")["a-root"]


def test_opencode_v2_empty_migrated_session_never_falls_back_to_legacy_messages():
    with _v2_db(legacy=True) as (writer, store):
        writer.execute(
            "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _session("empty")
        )
        writer.execute("insert into session values ('empty', null, 'old', '/repo', null, 1, 9999)")
        writer.execute(
            "insert into message values ('old', 'empty', ?)",
            [
                json.dumps(
                    {
                        "role": "assistant",
                        "tokens": {"input": 999},
                        "cost": 99,
                    }
                )
            ],
        )
        writer.execute(
            "insert into part values ('old-text', 'old', 'empty', ?)",
            [
                json.dumps(
                    {
                        "type": "text",
                        "text": "old content",
                    }
                )
            ],
        )
        writer.commit()
        assert store.workflows()[0].total_tokens == 0
        assert store.model_breakdown() == []
        assert store.message_timeline("empty") == []
        assert store.conversation_source("empty")["records"] == []


def test_opencode_v2_changes_use_recorded_native_and_migrated_patches_only():
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        result = store.session_change_files("root")
        files = {row["file"]: row for row in result["files"]}
        assert set(files) == {"a.py", "b.py", "missing.py", "old.py", "old-edit.py"}
        assert files["a.py"]["edits"][0]["source"] == "patch"
        assert files["b.py"]["edits"][0]["source"] == "edit"
        assert files["old.py"]["edits"][0]["source"] == "apply_patch"
        assert files["old-edit.py"]["edits"][0]["source"] == "edit"
        assert files["missing.py"]["status"] == "unknown"
        assert files["missing.py"]["edits"][0]["available"] is False
        assert store.session_change_diff("root", files["missing.py"]["edits"][0]["key"]) is None
        for path, patch_text in (
            ("a.py", "+new"),
            ("b.py", "edit patch"),
            ("old.py", "old patch"),
            ("old-edit.py", "old edit patch"),
        ):
            edit = files[path]["edits"][0]
            diff = store.session_change_diff("root", edit["key"])
            assert diff is not None and patch_text in diff["patch"]
        stale = files["a.py"]["edits"][0]["key"]
        writer.execute(
            "update session_message set data=replace(data, '+new', '+newer'), time_updated=99 where id='a-root'"
        )
        writer.commit()
        assert store.session_change_diff("root", stale) is None


def test_opencode_v2_manifest_is_metadata_only_and_tracks_tree_revisions():
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        statements = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch("opentab.stores.opencode.sqlite3.connect", traced_connect):
            before = store.conversation_manifest("root")
        assert before is not None and before[0] == "opencode-root-v2"
        assert all(".data" not in sql and "json_extract" not in sql for sql in statements)
        writer.execute("update session_message set time_updated=88 where id='a-child'")
        writer.execute("update event_sequence set seq=12 where aggregate_id='child'")
        writer.commit()
        assert store.conversation_manifest("root") != before


def test_opencode_v2_manifest_requires_stable_unique_metadata():
    with _v2_db(message_pk=False) as (writer, store):
        _populate_v2(writer)
        manifest = store.conversation_manifest("root")
        assert manifest is not None and manifest[0] != "opencode-root-v2"

    with _v2_db() as (writer, store):
        _populate_v2(writer)
        before = store.conversation_manifest("root")
        writer.execute("update session_message set type='compaction' where id='a-child'")
        writer.commit()
        assert store.conversation_manifest("root") != before
        writer.execute(
            "update session_message set time_updated=? where id='a-child'",
            [int(time.time() * 1000) + 1000],
        )
        writer.commit()
        assert store.conversation_manifest("root") is None


def test_opencode_v2_malformed_json_fails_closed():
    with _v2_db() as (writer, store):
        writer.execute(
            "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _session("bad"),
        )
        writer.executemany(
            "insert into session_message values (?,?,?,?,?,?,?)",
            [
                ("bad-user", "bad", "user", 0, 1, 1, "{"),
                ("bad-assistant", "bad", "assistant", 1, 2, 2, "not-json"),
                ("array-assistant", "bad", "assistant", 2, 3, 3, "[]"),
                ("scalar-assistant", "bad", "assistant", 3, 4, 4, "12"),
            ],
        )
        writer.commit()
        assert {row.id for row in store.workflows()} == {"bad"}
        assert store.model_breakdown() == []
        assert store.message_timeline("bad") == []
        assert store.turn_content("bad") == {}
        assert store.conversation_source("bad")["records"] == []


def test_opencode_v2_compaction_usage_is_billed_but_text_is_not_conversation_content():
    with _v2_db() as (writer, store):
        writer.execute(
            "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _session("compact", tokens=(2, 1, 0, 0, 0), cost=0.2),
        )
        writer.execute(
            "insert into session_message values (?,?,?,?,?,?,?)",
            _message(
                "compact-1",
                "compact",
                "compaction",
                0,
                {
                    "status": "completed",
                    "summary": "synthetic summary",
                    "recent": "synthetic recent context",
                    "model": {"id": "compact-model", "providerID": "provider"},
                    "cost": 0.2,
                    "tokens": {"input": 2, "output": 1},
                },
                created=10,
                updated=11,
            ),
        )
        writer.commit()
        models = store.model_breakdown()
        assert [(row["model_name"], row["tokens_total"], row["cost"]) for row in models] == [
            ("provider/compact-model", 3, 0.2)
        ]
        assert [row["content_key"] for row in store.message_timeline("compact")] == ["compact-1"]
        assert store.conversation_source("compact")["records"] == []


def test_opencode_v2_part_locators_are_disjoint_and_keep_content_order():
    with _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        native_row = writer.execute(
            "select rowid from session_message where id='a-root'"
        ).fetchone()[0]
        writer.execute(
            "insert into session values ('legacy', null, 'legacy', '/repo', null, 1, 50)"
        )
        writer.executemany(
            "insert into message values (?,?,?)",
            [
                ("lu", "legacy", json.dumps({"role": "user", "time": {"created": 1}})),
                (
                    "la",
                    "legacy",
                    json.dumps({"role": "assistant", "parentID": "lu", "time": {"created": 2}}),
                ),
            ],
        )
        writer.execute(
            "insert into part(rowid,id,message_id,session_id,data) values (?,'lp','la','legacy',?)",
            [
                native_row,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "edit",
                        "state": {
                            "status": "completed",
                            "metadata": {
                                "filediff": {
                                    "file": "legacy.py",
                                    "patch": "legacy-only patch",
                                    "additions": 1,
                                    "deletions": 1,
                                }
                            },
                        },
                    }
                ),
            ],
        )
        writer.commit()
        rows = list(
            store.conn.execute(
                "select rowid, message_id, part_index from part where message_id='a-root' order by part_index"
            )
        )
        assert len({row[0] for row in rows}) == 1
        assert rows[0][0] < 0
        assert [row[2] for row in rows] == list(range(len(rows)))
        key = store.session_change_files("legacy")["files"][0]["edits"][0]["key"]
        assert store.session_change_diff("legacy", key)["patch"] == "legacy-only patch"
        assert store.session_change_diff("root", key) is None
        native = store.session_change_files("root")["files"][0]["edits"][0]["key"]
        assert store.session_change_diff("root", native) is not None
        assert store.session_change_diff("legacy", native) is None


def test_opencode_v2_ignores_orphan_legacy_part_schema():
    with _v2_db(legacy=True) as (writer, store):
        writer.execute("drop table message")
        writer.execute("create table message (id text primary key, data text)")
        writer.commit()
        connection = sqlite3.connect(store.db)
        try:
            assert install_views(connection)
            assert list(connection.execute("select * from part")) == []
        finally:
            connection.close()


def test_opencode_v2_rejects_schema_without_authoritative_aggregates():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            create table session_v2 (id text primary key, parent_id text, time_created integer);
            create table session_message (
              id text primary key, session_id text, type text, seq integer,
              time_created integer, time_updated integer, data text
            );
            """
        )
        assert install_views(connection) is False
    finally:
        connection.close()


def test_opencode_v2_rollups_do_not_resolve_prompt_parents_per_message():
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        statements = []
        store.conn.set_trace_callback(statements.append)
        store.workflows()
        store.model_breakdown()
        store.conn.set_trace_callback(None)
        for sql in statements:
            if not sql.lstrip().lower().startswith(("select", "with")):
                continue
            plan = list(store.conn.execute("explain query plan " + sql))
            assert not any("CORRELATED SCALAR SUBQUERY" in row[3] for row in plan), plan


def test_opencode_v2_tool_outputs_skip_malformed_items_without_losing_text():
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        row = json.loads(
            writer.execute("select data from session_message where id='a-root'").fetchone()[0]
        )
        row["content"][1]["state"]["content"] = [
            "invalid",
            12,
            None,
            {"type": "text", "text": "valid"},
        ]
        writer.execute("update session_message set data=? where id='a-root'", [json.dumps(row)])
        writer.commit()
        events = store.turn_content("root", "a-root")["a-root"]
        assert next(event for event in events if event.get("name") == "patch")["output"] == "valid"


def test_opencode_v2_session_detail_work_does_not_scale_with_unrelated_messages():
    # A migrated database retains both generations. On this UNION shape, joins
    # alone can materialize the entire corpus despite tiny selected sessions.
    # Count VM work rather than asserting wall-clock timings or planner wording.
    with _v2_db(legacy=True) as (writer, store):
        writer.executescript(
            """
            create index message_session_idx on message(session_id);
            create index part_session_idx on part(session_id);
            create index part_message_idx on part(message_id);
            create index session_parent_idx on session(parent_id);
            """
        )
        _populate_v2(writer)
        writer.execute(
            "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _session("unrelated")
        )
        writer.execute(
            "insert into session values ('unrelated', null, 'migrated', '/repo', null, 1, 2)"
        )
        writer.commit()

        def measure(method):
            ticks = 0

            def progress():
                nonlocal ticks
                ticks += 1
                return False

            store.conn.set_progress_handler(progress, 100)
            try:
                result = getattr(store, method)("root")
                if isinstance(result, list):
                    result = [dict(row) for row in result]
                return result, ticks
            finally:
                store.conn.set_progress_handler(None, 0)

        methods = ("workflow_nodes", "message_timeline", "tool_breakdown", "turn_content")
        baseline = {method: measure(method) for method in methods}
        for i in range(400):
            data = {
                "role": "assistant",
                "model": {"providerID": "provider", "id": "model"},
                "tokens": {"input": 1},
                "content": [
                    {
                        "type": "tool",
                        "name": "read",
                        "state": {"content": [{"type": "text", "text": "unrelated output" * 100}]},
                    },
                ],
            }
            writer.execute(
                "insert into session_message values (?,?,?,?,?,?,?)",
                _message(f"unrelated-{i}", "unrelated", "assistant", i, data),
            )
            writer.execute(
                "insert into message values (?,?,?)",
                (f"old-{i}", "unrelated", json.dumps(data)),
            )
            writer.execute(
                "insert into part values (?,?,?,?)",
                (f"part-{i}", f"old-{i}", "unrelated", json.dumps({"type": "text", "text": "old"})),
            )
        writer.commit()
        for method in methods:
            result, ticks = measure(method)
            expected, before = baseline[method]
            assert result == expected, method
            assert ticks <= before + 50, (method, before, ticks)


def test_opencode_v2_rollups_never_serialize_inline_content_and_scan_usage_once():
    with _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        expected_workflows = store.workflows()
        expected_models = [dict(row) for row in store.model_breakdown()]
        statements = []

        def authorize(action, first, second, database, source):
            if action == sqlite3.SQLITE_FUNCTION and second in {
                "json_set",
                "json_object",
                "json_group_array",
                "json_group_object",
            }:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        # Changing the authorizer invalidates prepared statements, so even the
        # repeated queries must pass the no-serialization check.
        store.conn.set_authorizer(authorize)
        store.conn.set_trace_callback(statements.append)
        try:
            assert store.workflows() == expected_workflows
            assert [dict(row) for row in store.model_breakdown()] == expected_models
        finally:
            store.conn.set_authorizer(None)
            store.conn.set_trace_callback(None)
        # The materialized numeric rows are refreshed from metadata; unchanged
        # native JSON must not be read again for either rollup or residuals.
        assert not any("select data from main.session_message" in sql for sql in statements)
        assert sum("from main.session_message" in sql for sql in statements) == 1


def test_opencode_v2_changes_bound_fresh_list_and_diff_reads_to_the_session():
    with _v2_db(legacy=True) as (writer, store):
        writer.executescript(
            """
            create index message_session_idx on message(session_id);
            create index part_session_idx on part(session_id);
            create index part_message_idx on part(message_id);
            create index session_parent_idx on session(parent_id);
            """
        )
        _populate_v2(writer)
        # Include snapshots as well as native tools, so both queries must stay
        # scoped and both occurrence kinds still pass their live key checks.
        writer.execute(
            "update session_message set data=json_set(data, '$.summary', json(?)) where id='u-root'",
            [json.dumps({"diffs": [{"file": "snapshot.py", "patch": "snapshot patch"}]})],
        )
        writer.execute(
            "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _session("unrelated")
        )
        writer.execute(
            "insert into session values ('unrelated', null, 'migrated', '/repo', null, 1, 2)"
        )
        writer.commit()

        def measure(key=None):
            ticks = 0
            connect = store._change_connection

            def progress():
                nonlocal ticks
                ticks += 1
                return False

            def instrument(uri):
                conn = connect(uri)
                conn.set_progress_handler(progress, 100)
                return conn

            # Changes uses a fresh connection for every request, not store.conn.
            with patch.object(store, "_change_connection", instrument):
                if key is None:
                    result = store.session_change_files("root")
                else:
                    result = store.session_change_diff("root", key)
            return result, ticks

        summary, list_ticks = measure()
        keys = [
            edit["key"] for file in summary["files"] for edit in file["edits"] if edit["available"]
        ]
        assert len(keys) >= 5
        patches = {key: measure(key) for key in keys}
        assert all(result is not None for result, _ in patches.values())
        for i in range(250):
            tool = {
                "type": "tool",
                "id": "call-patch",
                "name": "patch",
                "state": {
                    "status": "completed",
                    "content": [{"type": "text", "text": "unrelated output" * 100}],
                    "metadata": {"files": [{"file": "elsewhere.py", "patch": "unrelated patch"}]},
                },
            }
            writer.execute(
                "insert into session_message values (?,?,?,?,?,?,?)",
                _message(f"outside-{i}", "unrelated", "assistant", i, {"content": [tool]}),
            )
            writer.execute(
                "insert into message values (?,?,?)",
                (f"old-{i}", "unrelated", json.dumps({"role": "assistant"})),
            )
            writer.execute(
                "insert into part values (?,?,?,?)",
                (f"part-{i}", f"old-{i}", "unrelated", json.dumps(tool)),
            )
        writer.commit()
        after, ticks = measure()
        assert after == summary
        assert ticks <= list_ticks + 100, (list_ticks, ticks)
        for key, (expected, before) in patches.items():
            result, ticks = measure(key)
            assert result == expected
            assert ticks <= before + 100, (before, ticks)

        # A selected patch must also avoid normalizing unrelated messages within
        # its own execution. Later seq values cannot change its owning prompt.
        writer.executemany(
            "insert into session_message values (?,?,?,?,?,?,?)",
            [
                _message(
                    f"later-{i}",
                    "root",
                    "assistant",
                    1000 + i,
                    {
                        "content": [
                            {
                                "type": "tool",
                                "name": "read",
                                "state": {
                                    "status": "completed",
                                    "content": [
                                        {"type": "text", "text": "large unrelated output" * 300}
                                    ],
                                },
                            }
                        ]
                    },
                )
                for i in range(200)
            ],
        )
        writer.commit()
        for key, (expected, before) in patches.items():
            result, ticks = measure(key)
            assert result == expected
            # Allow cheap rowid/ownership metadata work, not full normalization
            # of the 200 unrelated inline tool-output messages.
            assert ticks <= before + 200, (before, ticks)


def test_opencode_v2_changes_scoping_preserves_duplicate_ownership_checks():
    with _v2_db(legacy=True, message_pk=False) as (writer, store):
        _populate_v2(writer)
        key = next(
            edit["key"]
            for file in store.session_change_files("root")["files"]
            for edit in file["edits"]
            if edit["source"] == "patch"
        )
        original = writer.execute("select data from session_message where id='a-root'").fetchone()[
            0
        ]
        writer.execute(
            "insert into session_message values ('a-root', 'root', 'assistant', 21, 30, 40, ?)",
            [original],
        )
        writer.commit()
        assert store.session_change_diff("root", key) is None
        writer.execute("delete from session_message where session_id='root' and seq=21")
        writer.commit()
        assert store.session_change_diff("root", key) is not None
        data = json.loads(original)
        data["content"].append(data["content"][1])  # duplicate native tool ID in the same message
        writer.execute("update session_message set data=? where id='a-root'", [json.dumps(data)])
        writer.commit()
        assert store.session_change_diff("root", key) is None
        assert not any(
            edit["source"] == "patch"
            for file in store.session_change_files("root")["files"]
            for edit in file["edits"]
        )


def test_opencode_v2_changes_skip_normalizing_non_edit_tool_output():
    # Same execution and assistant message as the patch: session/key scoping alone
    # cannot avoid this output. Count VM work, not machine-dependent wall time.
    import threading

    with _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        original = json.loads(
            writer.execute("select data from session_message where id='a-root'").fetchone()[0]
        )

        def measure(key=None):
            ticks = 0
            connect = Store._change_connection

            def progress():
                nonlocal ticks
                ticks += 1
                return False

            def instrument(reader, uri):
                conn = connect(reader, uri)
                conn.set_progress_handler(progress, 100)
                return conn

            with patch.object(Store, "_change_connection", instrument):
                result = store.change_request("root", key)(threading.Event())
            return result, ticks

        measurements = []
        for fragments in (1, 1000):
            data = dict(original)
            data["content"] = original["content"] + [
                {
                    "type": "tool",
                    "id": "unrelated-read",
                    "name": "read",
                    "state": {
                        "status": "completed",
                        "content": [{"type": "text", "text": "not a patch"}] * fragments,
                    },
                }
            ]
            writer.execute(
                "update session_message set data=? where id='a-root'", [json.dumps(data)]
            )
            writer.commit()
            listing, ticks = measure()
            key = next(
                edit["key"]
                for file in listing["files"]
                for edit in file["edits"]
                if edit["source"] == "patch"
            )
            diff, diff_ticks = measure(key)
            assert diff is not None
            measurements.append((listing, ticks, diff, diff_ticks))
        before, after = measurements
        assert before[0] == after[0]
        assert before[2] == after[2]
        assert after[1] <= before[1] + 20, (before[1], after[1])
        assert after[3] <= before[3] + 20, (before[3], after[3])


def test_opencode_v2_changes_normalize_candidates_once_without_copying_messages():
    def check(legacy):
        with _v2_db(legacy=legacy) as (writer, store):
            _populate_v2(writer)
            expected = store.session_change_files("root")
            connect = store._change_connection
            counts = {"messages": 0, "patches": 0}
            native_query = False
            oracle = sqlite3.connect(":memory:")

            def normalize(*args):
                if native_query:
                    data = json.loads(args[0])
                    if "content" in data:
                        counts["messages"] += 1
                    if data.get("id") == "call-patch":
                        counts["patches"] += 1
                # Retain SQLite's exact JSON semantics while observing work.
                return oracle.execute(
                    "select json_set(" + ",".join("?" for _ in args) + ")", args
                ).fetchone()[0]

            def trace(sql):
                nonlocal native_query
                native_query = "native as" in sql

            def instrument(uri):
                conn = connect(uri)
                conn.create_function("json_set", -1, normalize)
                conn.set_trace_callback(trace)
                return conn

            try:
                with patch.object(store, "_change_connection", instrument):
                    assert store.session_change_files("root") == expected
            finally:
                oracle.close()
            assert counts["messages"] == 0, counts
            if sqlite3.sqlite_version_info >= (3, 35, 0):
                assert counts["patches"] == 1, counts

    check(False)
    check(True)


def test_opencode_v2_changes_debug_explains_worker_queries_and_preserves_payloads():
    from opentab import diagnostics as debug

    with tempfile.TemporaryDirectory() as tmp, _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        expected = store.change_request("root")(threading.Event())
        key = next(e["key"] for f in expected["files"] for e in f["edits"] if e["available"])
        expected_diff = store.change_request("root", key)(threading.Event())
        log = os.path.join(tmp, "debug.jsonl")
        with debug.session(filename=log):
            assert store.change_request("root")(threading.Event()) == expected
            assert store.change_request("root", key)(threading.Event()) == expected_diff
            assert store.session_change_diff("root", "private-invalid-key") is None
            cancelled = threading.Event()
            cancelled.set()
            assert store.change_request("root")(cancelled) is None
        with open(log, encoding="utf-8") as handle:
            text = handle.read()
        for value in (key, "private-invalid-key", "patch result", "patch input", "a.py", store.db):
            assert value not in text
        records = [json.loads(line) for line in text.splitlines()]
        plan = next(
            r
            for r in records
            if r["event"] == "sql.plan" and r["query"] == "opencode.changes_native"
        )
        assert isinstance(plan["native_materialized"], bool)
        native = next(r for r in records if r["event"] == "opencode.changes_native.end")
        assert native["rows"] > 0 and native["execute_ms"] >= 0
        ready = next(r for r in records if r["event"] == "opencode.changes_files_ready")
        assert ready["files"] == len(expected["files"])
        assert any(
            r["event"] == "opencode.changes_outcome" and r["result"] == "cancelled_before_open"
            for r in records
        )
        assert not any(r.get("status") == "error" for r in records)


def test_opencode_v2_change_message_metadata_matches_full_projection():
    with _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        samples = [
            "{}",
            "null",
            "[]",
            '"text"',
            "123",
            "false",
            '{"broken":',
            '{"role":"wrong","role":"other","parentID":"u-root"}',
            '{"parentID":"u-root","parentID":"other","time":null}',
            '{"parentID":{"nested":1}}',
            '{"parentID":[1,2]}',
            '{"parentID":false}',
            '{"parentID":"é\\n"}',
        ]
        for index, data in enumerate(samples):
            writer.execute(
                "insert into session_message values (?,?,?,?,?,?,?)",
                (
                    f"metadata-{index}",
                    "root",
                    "compaction" if index % 2 else "assistant",
                    100 + index,
                    1,
                    2,
                    data,
                ),
            )
        writer.commit()
        sql = """with recursive tree(id) as (select 'root')
          select m.id, m.parent_id, m.time_created, m.time_updated,
                 json_extract(m.data, '$.role'), json_extract(m.data, '$.parentID')
          from message m order by m.seq"""
        full = store.conn.execute(scoped_detail_sql(store.conn, sql)).fetchall()
        metadata = store.conn.execute(
            scoped_detail_sql(store.conn, sql, change_candidates=True)
        ).fetchall()
        assert [tuple(row) for row in metadata] == [tuple(row) for row in full]


def test_opencode_v2_changes_reject_non_edit_duplicate_part_identity():
    with _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        listing = store.session_change_files("root")
        key = next(
            edit["key"]
            for file in listing["files"]
            for edit in file["edits"]
            if edit["source"] == "patch"
        )
        data = json.loads(
            writer.execute("select data from session_message where id='a-root'").fetchone()[0]
        )
        # Candidate filtering skips both, but uniqueness must still see them.
        for name, status in (("read", "completed"), ("patch", "running")):
            duplicate = dict(data)
            duplicate["content"] = data["content"] + [
                {"type": "tool", "id": "call-patch", "name": name, "state": {"status": status}}
            ]
            writer.execute(
                "update session_message set data=? where id='a-root'", [json.dumps(duplicate)]
            )
            writer.commit()
            assert store.session_change_diff("root", key) is None
            assert not any(
                edit["source"] == "patch"
                for file in store.session_change_files("root")["files"]
                for edit in file["edits"]
            )


def test_opencode_v2_scoped_changes_keep_legacy_parents_and_duplicate_parts():
    with _v2_db(legacy=True) as (writer, store):
        writer.executescript(
            "drop table part; create table part (id text, message_id text, session_id text, data text);"
        )
        _populate_v2(writer)
        writer.execute("insert into session values ('legacy', null, 'legacy', '/repo', null, 1, 2)")
        writer.executemany(
            "insert into message values (?,?,?)",
            [
                ("lu", "legacy", json.dumps({"role": "user"})),
                ("la", "legacy", json.dumps({"role": "assistant", "parentID": "lu"})),
            ],
        )
        blob = json.dumps(
            {
                "type": "tool",
                "tool": "patch",
                "state": {
                    "status": "completed",
                    "metadata": {"files": [{"file": "legacy.py", "patch": "legacy patch"}]},
                },
            }
        )
        writer.execute("insert into part values ('lp','la','legacy',?)", [blob])
        writer.commit()
        files = store.session_change_files("legacy")["files"]
        key = files[0]["edits"][0]["key"]
        assert store.session_change_diff("legacy", key)["patch"] == "legacy patch"
        # A keyed source filter must retain other occurrences of the same part ID,
        # rather than letting its physical row locator bypass uniqueness checks.
        writer.execute("insert into part values ('lp','la','legacy',?)", [blob])
        writer.commit()
        assert store.session_change_diff("legacy", key) is None
