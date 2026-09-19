import json
import os
import sqlite3
import tempfile
import threading
from contextlib import closing, contextmanager
from unittest.mock import patch

import opentab as ot
from opentab.stores.opencode import REQUIRED_SCHEMA

from tests._support import (
    PI_SID,
    FakeStore,
    _jsonl_args,
    _pi_args,
    _pi_assistant,
    _pi_session,
    _pi_user,
    _pi_write,
    _write_jsonl,
    _write_opencode_db_with_tools,
    _write_opencode_db_with_turns,
)


@contextmanager
def _conversation_db(*, legacy=False, constrained=True, revisions=False, directory=None):
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "conversation.db")
        writer = sqlite3.connect(db)
        key = "primary key" if constrained else ""
        writer.execute(
            f"create table session (id text {key}, parent_id text"
            + (", directory text" if directory is not None else "")
            + ")"
        )
        writer.execute(
            f"create table message (id text {key}, session_id text, data text"
            + ("" if legacy else ", time_created integer")
            + ")"
        )
        writer.execute(
            f"create table part (id text {key}, message_id text, data text"
            + ("" if legacy else ", session_id text, time_created integer")
            + ")"
        )
        if revisions:
            for table in ("message", "part"):
                writer.execute(f"alter table {table} add column time_updated integer default 1")
            writer.execute("create index session_parent_idx on session(parent_id)")
            writer.execute(
                "create index message_session_idx on message(session_id, time_created, id)"
            )
            writer.execute("create index part_session_idx on part(session_id)")
        if directory is None:
            writer.execute("insert into session values ('root', null)")
        else:
            writer.execute("insert into session values ('root', null, ?)", [directory])
        writer.commit()
        store = ot.Store(db, type("Args", (), {"demo": False})())
        try:
            yield db, writer, store
        finally:
            store.conn.close()
            writer.close()


def _conversation_error(store, root="root", execution=None, code="conversation_unavailable"):
    from opentab.conversations.reader import ConversationError

    try:
        store.conversation_source(root, execution)
    except ConversationError as exc:
        assert exc.code == code, exc.code
        assert "PRIVATE" not in str(exc)
        return
    raise AssertionError("Expected an explicit conversation error")


def _change_message(writer, mid, sid, created, diffs, *, role="user"):
    writer.execute(
        "insert into message (id, session_id, data, time_created) values (?, ?, ?, ?)",
        (
            mid,
            sid,
            json.dumps(
                {
                    "role": role,
                    "time": {"created": created},
                    "summary": {"diffs": diffs},
                }
            ),
            created,
        ),
    )


def _change_tool(writer, pid, mid, sid, created, tool, state, *, part_id=None):
    writer.execute(
        "insert into message (id, session_id, data, time_created) values (?, ?, ?, ?)",
        (
            mid,
            sid,
            json.dumps(
                {
                    "role": "assistant",
                    "parentID": pid,
                    "time": {"created": created},
                }
            ),
            created,
        ),
    )
    writer.execute(
        "insert into part (id, message_id, data, session_id, time_created) values (?, ?, ?, ?, ?)",
        (
            part_id or "part-" + mid,
            mid,
            json.dumps({"type": "tool", "tool": tool, "state": state}),
            sid,
            created,
        ),
    )


def test_opencode_changes_list_native_patches_and_aggregate_repeated_paths_in_time_order():
    with _conversation_db() as (_, writer, store):
        writer.executemany(
            "insert into session values (?, ?)",
            [("child", "root"), ("outside", None)],
        )
        _change_message(
            writer,
            "later",
            "root",
            30,
            [
                {
                    "file": "src/a.py",
                    "status": "modified",
                    "additions": 3,
                    "deletions": 1,
                    "patch": "@@ -1 +1 @@\n-old\n+new\n",
                }
            ],
        )
        _change_message(
            writer,
            "child-first",
            "child",
            10,
            [
                {
                    "file": "src/a.py",
                    "status": "added",
                    "additions": 2,
                    "deletions": 0,
                    "patch": "new file patch",
                },
                {
                    "file": "src/b.py",
                    "status": "deleted",
                    "additions": "bad",
                    "deletions": -1,
                    "patch": "deleted patch",
                },
            ],
        )
        _change_message(
            writer,
            "outside",
            "outside",
            1,
            [{"file": "PRIVATE", "status": "added", "patch": "PRIVATE"}],
        )
        _change_message(
            writer,
            "assistant",
            "root",
            5,
            [{"file": "PRIVATE-assistant", "status": "added", "patch": "PRIVATE"}],
            role="assistant",
        )
        writer.commit()

        changes = store.session_change_files("root")
        assert not changes["truncated"]
        assert len(changes["limitations"]) == 3
        assert [row["file"] for row in changes["files"]] == ["src/a.py", "src/b.py"]
        a, b = changes["files"]
        assert (a["status"], a["additions"], a["deletions"]) == ("mixed", 5, 1)
        assert [edit["message_id"] for edit in a["edits"]] == ["child-first", "later"]
        assert [edit["execution_id"] for edit in a["edits"]] == ["child", "root"]
        assert all(edit["available"] for edit in a["edits"])
        assert b["additions"] is None and b["deletions"] is None
        assert changes == store.session_change_files("root")

        diff = store.session_change_diff("root", a["edits"][1]["key"])
        assert diff == {
            "file": "src/a.py",
            "patch": "@@ -1 +1 @@\n-old\n+new\n",
            "truncated": False,
            "limitation": "Native per-prompt snapshot patch; it may include concurrent or later-reverted edits.",
        }


def test_opencode_changes_support_legacy_snapshots_and_keep_unavailable_files():
    with _conversation_db() as (_, writer, store):
        _change_message(
            writer,
            "legacy",
            "root",
            1,
            [
                {
                    "file": "legacy.txt",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "before": "old\n",
                    "after": "new\n",
                },
                {"file": "binary.png", "status": "modified", "patch": ""},
                {"file": "empty.txt", "status": "modified", "before": "", "after": ""},
                None,
                "malformed-shape",
                {"file": "", "patch": "PRIVATE"},
            ],
        )
        writer.execute("insert into message values ('broken', 'root', 'not json', 2)")
        writer.commit()

        changes = store.session_change_files("root")
        assert [row["file"] for row in changes["files"]] == [
            "legacy.txt",
            "binary.png",
            "empty.txt",
        ]
        edits = {row["file"]: row["edits"][0] for row in changes["files"]}
        assert edits["legacy.txt"]["available"]
        assert not edits["binary.png"]["available"]
        assert edits["empty.txt"]["available"]
        assert store.session_change_diff("root", edits["binary.png"]["key"]) is None
        legacy = store.session_change_diff("root", edits["legacy.txt"]["key"])
        assert legacy["file"] == "legacy.txt" and not legacy["truncated"]
        assert "--- a/legacy.txt" in legacy["patch"]
        assert "-old" in legacy["patch"] and "+new" in legacy["patch"]
        assert store.session_change_diff("root", edits["empty.txt"]["key"])["patch"] == ""


def test_opencode_changes_enforce_root_ownership_and_reject_tampered_or_stale_keys():
    with _conversation_db() as (_, writer, store):
        writer.executemany(
            "insert into session values (?, ?)",
            [("child", "root"), ("outside", None)],
        )
        _change_message(
            writer,
            "m",
            "child",
            1,
            [{"file": "owned.py", "status": "modified", "patch": "patch one"}],
        )
        _change_message(
            writer,
            "private",
            "outside",
            1,
            [{"file": "PRIVATE", "status": "modified", "patch": "PRIVATE"}],
        )
        writer.commit()
        edit = store.session_change_files("root")["files"][0]["edits"][0]
        key = edit["key"]
        assert store.session_change_files("child")["files"] == []
        assert store.session_change_diff("outside", key) is None
        assert (
            store.session_change_diff("root", key[:-1] + ("0" if key[-1] != "0" else "1")) is None
        )

        writer.execute(
            "update message set data = replace(data, 'patch one', 'patch two') where id = 'm'"
        )
        writer.commit()
        assert store.session_change_diff("root", key) is None
        fresh = store.session_change_files("root")["files"][0]["edits"][0]["key"]
        assert fresh != key
        assert store.session_change_diff("root", fresh)["patch"] == "patch two"

        writer.execute("update session set parent_id = 'child' where id = 'root'")
        writer.commit()
        assert store.session_change_files("root")["files"] == []
        assert store.session_change_diff("root", fresh) is None


def test_opencode_changes_apply_summary_body_and_legacy_generation_bounds():
    with _conversation_db() as (_, writer, store):
        _change_message(
            writer,
            "m",
            "root",
            1,
            [
                {"file": "one", "status": "added", "patch": "abcdef"},
                {
                    "file": "two",
                    "status": "modified",
                    "before": "a\nb\nc\n",
                    "after": "x\ny\nz\n",
                },
            ],
        )
        writer.commit()
        with patch("opentab.stores.opencode.CHANGE_SUMMARY_LIMIT", 1):
            limited = store.session_change_files("root")
        assert limited["truncated"] and [row["file"] for row in limited["files"]] == ["one"]
        assert any("occurrence limit" in text for text in limited["limitations"])

        changes = store.session_change_files("root")
        keys = {row["file"]: row["edits"][0]["key"] for row in changes["files"]}
        with patch("opentab.stores.opencode.CHANGE_DIFF_BYTES", 4):
            native = store.session_change_diff("root", keys["one"])
        assert native["patch"] == "abcd" and native["truncated"]
        with patch("opentab.stores.opencode.CHANGE_LEGACY_LINE_LIMIT", 1):
            legacy = store.session_change_diff("root", keys["two"])
        assert legacy["truncated"] and "source or output limits" in legacy["limitation"]
        with patch("opentab.stores.opencode.CHANGE_SUMMARY_BYTES", 1):
            metadata = store.session_change_files("root")
        assert metadata["files"] == [] and metadata["truncated"]
        assert any("metadata byte limit" in text for text in metadata["limitations"])


def test_opencode_changes_capability_is_cheap_and_demo_never_reads():
    with _conversation_db() as (_, writer, store):
        _change_message(
            writer,
            "m",
            "root",
            1,
            [{"file": "secret.py", "status": "modified", "patch": "PRIVATE" * 10000}],
        )
        writer.commit()
        queries = []
        store.conn.set_trace_callback(queries.append)
        with patch(
            "opentab.stores.opencode.sqlite3.connect", side_effect=AssertionError("capability read")
        ):
            assert store.supports_changes("root")
            assert store.supports_changes("missing")
        assert queries == []

        changes = store.session_change_files("root")
        assert "PRIVATE" not in json.dumps(changes)
        edit = changes["files"][0]["edits"][0]
        assert edit["available"]
        store.demo = True
        store.conn.close()
        with patch(
            "opentab.stores.opencode.sqlite3.connect", side_effect=AssertionError("demo read")
        ):
            assert not store.supports_changes("root")
            assert store.session_change_files("root")["files"] == []
            assert store.session_change_diff("root", edit["key"]) is None


def test_opencode_change_request_constructs_and_closes_store_in_worker_thread():
    from opentab.tui.changes_worker import ChangesWorker

    with _conversation_db() as (_, writer, store):
        _change_message(writer, "m", "root", 1, [{"file": "a", "patch": "patch"}])
        writer.commit()
        request = store.change_request("root")
        main_thread = threading.get_ident()
        seen = {}

        class Connection:
            def set_progress_handler(self, callback, steps):
                seen["progress"] = (callback, steps)

            def close(self):
                seen["closed"] = threading.get_ident()

        class WorkerStore:
            def __init__(self, db, args):
                seen["constructed"] = threading.get_ident()
                seen["db"] = db
                self.conn = Connection()

            def session_change_files(self, root_id):
                seen["read"] = threading.get_ident()
                return {"files": [], "limitations": [], "truncated": False}

        worker = ChangesWorker()
        try:
            with patch("opentab.stores.opencode.Store", WorkerStore):
                assert worker.submit(("files", "root"), request)
                assert worker.wait_for_result()
                assert worker.poll()[0][2] is False
        finally:
            worker.close()
        assert seen["constructed"] != main_thread
        assert seen["constructed"] == seen["read"] == seen["closed"]
        assert seen["db"] == store.db
        assert seen["progress"][1] == 1000


def test_opencode_changes_message_revision_survives_unrelated_live_activity():
    with _conversation_db(revisions=True) as (_, writer, store):
        _change_message(writer, "m", "root", 1, [{"file": "a", "patch": "old"}])
        writer.execute("insert into session values ('other', null)")
        writer.commit()
        key = store.session_change_files("root")["files"][0]["edits"][0]["key"]
        _change_message(writer, "unrelated", "other", 2, [{"file": "PRIVATE", "patch": "PRIVATE"}])
        writer.commit()
        assert store.session_change_diff("root", key)["patch"] == "old"
        writer.execute(
            "update message set data = replace(data, 'old', 'new'), time_updated = 2 where id = 'm'"
        )
        writer.commit()
        assert store.session_change_diff("root", key) is None
        fresh = store.session_change_files("root")["files"][0]["edits"][0]["key"]
        assert store.session_change_diff("root", fresh)["patch"] == "new"


def test_opencode_changes_unknown_counts_stay_unknown_and_missing_newlines_remain_distinct():
    with _conversation_db() as (_, writer, store):
        _change_message(
            writer,
            "one",
            "root",
            1,
            [{"file": "a", "additions": 1, "deletions": 1, "before": "old", "after": "new"}],
        )
        _change_message(writer, "two", "root", 2, [{"file": "a", "additions": None, "patch": "p"}])
        _change_message(
            writer,
            "three",
            "root",
            3,
            [{"file": "a", "additions": 2, "deletions": 2, "patch": "p"}],
        )
        writer.commit()
        file = store.session_change_files("root")["files"][0]
        assert file["additions"] is None and file["deletions"] is None
        diff = store.session_change_diff("root", file["edits"][0]["key"])
        assert "-old\n\\ No newline at end of file\n+new\n" in diff["patch"]


def test_opencode_changes_legacy_schema_skips_malformed_messages_and_ambiguous_roots():
    with _conversation_db(legacy=True) as (_, writer, store):
        writer.execute("insert into message values ('bad', 'root', 'not json')")
        writer.execute(
            "insert into message values ('good', 'root', ?)",
            [
                json.dumps(
                    {"role": "user", "summary": {"diffs": [{"file": "good", "patch": "patch"}]}}
                )
            ],
        )
        writer.commit()
        assert [f["file"] for f in store.session_change_files("root")["files"]] == ["good"]
    with _conversation_db(constrained=False) as (_, writer, store):
        _change_message(writer, "good", "root", 1, [{"file": "PRIVATE", "patch": "PRIVATE"}])
        writer.execute("insert into session values ('root', null)")
        writer.commit()
        assert store.session_change_files("root")["files"] == []


def test_opencode_changes_read_completed_apply_patch_parts_across_the_execution_tree():
    with _conversation_db(revisions=True, directory="/repo") as (_, writer, store):
        writer.execute("insert into session values ('child', 'root', '/repo')")
        _change_message(
            writer,
            "root-prompt",
            "root",
            1,
            [
                {
                    "file": "old.py",
                    "status": "deleted",
                    "patch": "snapshot old",
                    "additions": 0,
                    "deletions": 1,
                },
                {
                    "file": "new.py",
                    "status": "added",
                    "patch": "snapshot new",
                    "additions": 1,
                    "deletions": 0,
                },
            ],
        )
        _change_message(writer, "child-prompt", "child", 2, [])
        _change_tool(
            writer,
            "root-prompt",
            "root-tool",
            "root",
            3,
            "apply_patch",
            {
                "status": "completed",
                "metadata": {
                    "diff": "PRIVATE aggregate must not be selected",
                    "files": [
                        {
                            "filePath": "/repo/src/a.py",
                            "relativePath": "src/a.py",
                            "type": "update",
                            "patch": "root patch",
                            "additions": 2,
                            "deletions": 1,
                        },
                        {
                            "filePath": "/repo/old.py",
                            "relativePath": "new.py",
                            "movePath": "/repo/new.py",
                            "type": "move",
                            "patch": "move content patch",
                            "additions": 1,
                            "deletions": 1,
                        },
                    ],
                },
            },
        )
        _change_tool(
            writer,
            "child-prompt",
            "child-tool",
            "child",
            4,
            "apply_patch",
            {
                "status": "completed",
                "metadata": {
                    "files": [
                        {
                            "filePath": "/repo/src/a.py",
                            "relativePath": "src/a.py",
                            "type": "update",
                            "patch": "child patch",
                            "additions": 3,
                            "deletions": 0,
                        }
                    ]
                },
            },
        )
        writer.commit()

        changes = store.session_change_files("root")
        assert [item["file"] for item in changes["files"]] == ["old.py", "new.py", "src/a.py"]
        old, moved, repeated = changes["files"]
        assert (repeated["additions"], repeated["deletions"]) == (5, 1)
        assert [edit["execution_id"] for edit in repeated["edits"]] == ["root", "child"]
        assert [edit["source"] for edit in repeated["edits"]] == [
            "apply_patch",
            "apply_patch",
        ]
        assert moved["edits"][1]["from_file"] == "old.py"
        assert moved["status"] == "mixed"
        for item in (old, moved):
            assert item["counts_overlap"]
            assert item["additions"] is None and item["deletions"] is None
            assert store.session_change_diff("root", item["edits"][0]["key"])["patch"].startswith(
                "snapshot"
            )
        assert store.session_change_diff("root", repeated["edits"][0]["key"])["patch"] == (
            "root patch"
        )
        assert store.session_change_diff("root", moved["edits"][1]["key"])["patch"] == (
            "move content patch"
        )


def test_opencode_changes_keep_snapshot_shell_edits_alongside_same_prompt_tool_patch():
    with _conversation_db(revisions=True, directory="/repo") as (_, writer, store):
        _change_message(
            writer,
            "p1",
            "root",
            1,
            [
                {
                    "file": "same.py",
                    "status": "modified",
                    "patch": "@@ -1 +1,2 @@\n-old\n+tool change\n+shell change\n",
                    "additions": 2,
                    "deletions": 1,
                },
                {
                    "file": "snapshot-only.py",
                    "status": "added",
                    "patch": "fallback",
                    "additions": 1,
                    "deletions": 0,
                },
            ],
        )
        _change_message(
            writer,
            "p2",
            "root",
            2,
            [
                {
                    "file": "same.py",
                    "status": "modified",
                    "patch": "other prompt",
                    "additions": 1,
                    "deletions": 1,
                }
            ],
        )
        _change_tool(
            writer,
            "p1",
            "patch",
            "root",
            3,
            "apply_patch",
            {
                "status": "completed",
                "metadata": {
                    "files": [
                        {
                            "filePath": "/repo/same.py",
                            "relativePath": "same.py",
                            "type": "update",
                            "patch": "@@ -1 +1 @@\n-old\n+tool change\n",
                            "additions": 1,
                            "deletions": 1,
                        }
                    ]
                },
            },
        )
        _change_tool(
            writer,
            "p1",
            "edit",
            "root",
            4,
            "edit",
            {
                "status": "completed",
                "metadata": {
                    "filediff": {
                        "file": "/repo/edit.py",
                        "patch": "edit patch",
                        "additions": 4,
                        "deletions": 2,
                    }
                },
            },
        )
        _change_tool(
            writer,
            "p1",
            "write",
            "root",
            5,
            "write",
            {
                "status": "completed",
                "metadata": {"filepath": "/repo/write.py", "exists": False},
                "input": {"content": "PRIVATE must not become an assumed diff"},
            },
        )
        writer.commit()

        changes = store.session_change_files("root")
        files = {item["file"]: item for item in changes["files"]}
        assert [edit["source"] for edit in files["same.py"]["edits"]] == [
            "snapshot",
            "snapshot",
            "apply_patch",
        ]
        same = files["same.py"]
        assert [edit["message_id"] for edit in same["edits"]] == ["p1", "p2", "p1"]
        assert same["counts_overlap"] and same["additions"] is None and same["deletions"] is None
        assert [(edit["additions"], edit["deletions"]) for edit in same["edits"]] == [
            (2, 1),
            (1, 1),
            (1, 1),
        ]
        snapshot = store.session_change_diff("root", same["edits"][0]["key"])
        tool = store.session_change_diff("root", same["edits"][2]["key"])
        assert "+shell change" in snapshot["patch"] and "+shell change" not in tool["patch"]
        assert "+tool change" in snapshot["patch"] and "+tool change" in tool["patch"]
        assert any("Overlapping" in text for text in changes["limitations"])
        assert files["snapshot-only.py"]["edits"][0]["source"] == "snapshot"
        assert (files["snapshot-only.py"]["additions"], files["snapshot-only.py"]["deletions"]) == (
            1,
            0,
        )
        assert not files["snapshot-only.py"].get("counts_overlap")
        assert (files["edit.py"]["additions"], files["edit.py"]["deletions"]) == (4, 2)
        edit = files["edit.py"]["edits"][0]
        assert (edit["source"], edit["status"], edit["additions"], edit["deletions"]) == (
            "edit",
            "modified",
            4,
            2,
        )
        assert store.session_change_diff("root", edit["key"])["patch"] == "edit patch"
        write = files["write.py"]["edits"][0]
        assert (write["source"], write["status"], write["available"]) == (
            "write",
            "added",
            False,
        )
        assert store.session_change_diff("root", write["key"]) is None


def test_opencode_changes_overlap_requires_the_same_execution_prompt_and_path():
    with _conversation_db(revisions=True, directory="/repo") as (_, writer, store):
        writer.execute("insert into session values ('child', 'root', '/repo')")
        _change_message(
            writer,
            "p1",
            "root",
            1,
            [{"file": "a.py", "patch": "snapshot", "additions": 2, "deletions": 1}],
        )
        for pid, sid, path in (
            ("p1", "root", "b.py"),
            ("p2", "root", "a.py"),
            ("p3", "child", "a.py"),
        ):
            if pid != "p1":
                _change_message(writer, pid, sid, 2, [])
            _change_tool(
                writer,
                pid,
                "tool-" + pid,
                sid,
                3,
                "edit",
                {
                    "status": "completed",
                    "metadata": {
                        "filediff": {
                            "file": "/repo/" + path,
                            "patch": "tool",
                            "additions": 3,
                            "deletions": 1,
                        }
                    },
                },
            )
        writer.commit()
        changes = store.session_change_files("root")
        a, b = changes["files"]
        assert (a["file"], a["additions"], a["deletions"]) == ("a.py", 8, 3)
        assert (b["file"], b["additions"], b["deletions"]) == ("b.py", 3, 1)
        assert not any(item.get("counts_overlap") for item in changes["files"])
        assert not any("Overlapping" in text for text in changes["limitations"])


def test_opencode_changes_move_source_snapshot_and_path_only_write_stay_visible():
    for tool, metadata, snapshot_path, tool_path in (
        (
            "apply_patch",
            {
                "files": [
                    {
                        "type": "move",
                        "filePath": "/repo/old.py",
                        "movePath": "/repo/new.py",
                        "patch": "move",
                        "additions": 1,
                        "deletions": 1,
                    }
                ]
            },
            "old.py",
            "new.py",
        ),
        ("write", {"filepath": "/repo/a.py", "exists": True}, "a.py", "a.py"),
    ):
        with _conversation_db(revisions=True, directory="/repo") as (_, writer, store):
            _change_message(
                writer,
                "prompt",
                "root",
                1,
                [
                    {
                        "file": snapshot_path,
                        "before": "old\n",
                        "after": "script result\n",
                        "additions": 1,
                        "deletions": 1,
                    }
                ],
            )
            _change_tool(
                writer,
                "prompt",
                "tool",
                "root",
                2,
                tool,
                {"status": "completed", "metadata": metadata},
            )
            writer.commit()
            files = {item["file"]: item for item in store.session_change_files("root")["files"]}
            snapshot = files[snapshot_path]["edits"][0]
            assert snapshot["source"] == "snapshot"
            assert "+script result" in store.session_change_diff("root", snapshot["key"])["patch"]
            assert files[tool_path]["edits"][-1]["source"] == tool
            for item in files.values():
                assert item["counts_overlap"]
                assert item["additions"] is None and item["deletions"] is None


def test_opencode_changes_exclude_unfinished_malformed_and_ambiguously_owned_tools():
    with _conversation_db(constrained=False, revisions=True) as (_, writer, store):
        _change_message(writer, "prompt", "root", 1, [])
        for index, status in enumerate(("running", "error"), 2):
            _change_tool(
                writer,
                "prompt",
                f"unfinished-{index}",
                "root",
                index,
                "write",
                {"status": status, "metadata": {"filepath": "PRIVATE", "exists": False}},
            )
        _change_tool(
            writer,
            "prompt",
            "malformed",
            "root",
            4,
            "apply_patch",
            {"status": "completed", "metadata": {"files": "not-an-array"}},
        )
        _change_tool(
            writer,
            "missing-prompt",
            "orphan",
            "root",
            5,
            "write",
            {"status": "completed", "metadata": {"filepath": "PRIVATE", "exists": False}},
        )
        _change_tool(
            writer,
            "prompt",
            "duplicate-message",
            "root",
            6,
            "write",
            {"status": "completed", "metadata": {"filepath": "PRIVATE", "exists": False}},
        )
        writer.execute(
            "insert into message (id, session_id, data, time_created) values (?, ?, ?, ?)",
            (
                "duplicate-message",
                "root",
                json.dumps({"role": "assistant", "parentID": "prompt"}),
                6,
            ),
        )
        _change_tool(
            writer,
            "prompt",
            "duplicate-part-message",
            "root",
            7,
            "write",
            {"status": "completed", "metadata": {"filepath": "PRIVATE", "exists": False}},
            part_id="duplicate-part",
        )
        writer.execute("insert into part select * from part where id = 'duplicate-part'")
        writer.execute("insert into session values ('outside', null)")
        writer.execute(
            "insert into part (id, message_id, data, session_id, time_created) values (?, ?, ?, ?, ?)",
            (
                "mismatch",
                "duplicate-message",
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "write",
                        "state": {
                            "status": "completed",
                            "metadata": {"filepath": "PRIVATE", "exists": False},
                        },
                    }
                ),
                "outside",
                7,
            ),
        )
        writer.commit()
        assert store.session_change_files("root")["files"] == []


def test_opencode_changes_native_keys_track_selected_part_not_unrelated_modern_activity():
    with _conversation_db(revisions=True, directory="/repo") as (_, writer, store):
        writer.execute("insert into session values ('other', null, '/other')")
        _change_message(writer, "prompt", "root", 1, [])
        _change_tool(
            writer,
            "prompt",
            "tool",
            "root",
            2,
            "edit",
            {
                "status": "completed",
                "metadata": {
                    "filediff": {
                        "file": "/repo/a.py",
                        "patch": "before",
                        "additions": 1,
                        "deletions": 1,
                    }
                },
            },
        )
        writer.commit()
        key = store.session_change_files("root")["files"][0]["edits"][0]["key"]

        _change_message(writer, "other-prompt", "other", 3, [])
        _change_tool(
            writer,
            "other-prompt",
            "other-tool",
            "other",
            4,
            "write",
            {"status": "completed", "metadata": {"filepath": "/other/x", "exists": False}},
        )
        writer.commit()
        assert store.session_change_diff("root", key)["patch"] == "before"
        writer.execute(
            "update message set data = json_set(data, '$.summary.title', 'later title'), "
            "time_updated = 2 where id = 'prompt'"
        )
        writer.commit()
        assert store.session_change_diff("root", key)["patch"] == "before"

        writer.execute(
            "update part set data = replace(data, 'before', 'after'), time_updated = 2 where id = 'part-tool'"
        )
        writer.commit()
        assert store.session_change_diff("root", key) is None
        fresh = store.session_change_files("root")["files"][0]["edits"][0]["key"]
        assert store.session_change_diff("root", fresh)["patch"] == "after"


def test_opencode_changes_modern_revisions_do_not_require_a_quiet_database_fingerprint():
    with _conversation_db(revisions=True) as (_, writer, store):
        _change_message(writer, "prompt", "root", 1, [{"file": "a", "patch": "content"}])
        writer.commit()
        with patch.object(store, "_conversation_database_manifest", return_value=None):
            result = store.session_change_files("root")
            assert len(result["files"]) == 1
            key = result["files"][0]["edits"][0]["key"]
            assert store.session_change_diff("root", key)["patch"] == "content"
    with _conversation_db() as (_, writer, store):
        _change_message(writer, "prompt", "root", 1, [{"file": "a", "patch": "content"}])
        writer.commit()
        with patch.object(store, "_conversation_database_manifest", return_value=None):
            assert store.session_change_files("root")["files"] == []


def test_opencode_changes_apply_native_occurrence_metadata_and_output_bounds():
    with _conversation_db(revisions=True, directory="/repo") as (_, writer, store):
        _change_message(writer, "prompt", "root", 1, [])
        _change_tool(
            writer,
            "prompt",
            "tool",
            "root",
            2,
            "apply_patch",
            {
                "status": "completed",
                "metadata": {
                    "files": [
                        {
                            "filePath": "/repo/one",
                            "relativePath": "one",
                            "type": "update",
                            "patch": "abcdef",
                            "additions": "malformed",
                            "deletions": -1,
                        },
                        {
                            "filePath": "/repo/two",
                            "relativePath": "two",
                            "type": "update",
                            "patch": "second",
                            "additions": 1,
                            "deletions": 0,
                        },
                        {
                            "filePath": "/repo/" + "x" * 5000,
                            "relativePath": "x" * 5000,
                            "type": "add",
                            "patch": "oversized path",
                            "additions": 1,
                            "deletions": 0,
                        },
                    ]
                },
            },
        )
        writer.commit()

        changes = store.session_change_files("root")
        assert changes["truncated"]
        assert [item["file"] for item in changes["files"]] == ["one", "two"]
        assert changes["files"][0]["additions"] is None
        assert changes["files"][0]["deletions"] is None
        key = changes["files"][0]["edits"][0]["key"]
        with patch("opentab.stores.opencode.CHANGE_DIFF_BYTES", 4):
            diff = store.session_change_diff("root", key)
        assert diff["patch"] == "abcd" and diff["truncated"]
        with patch("opentab.stores.opencode.CHANGE_SUMMARY_LIMIT", 1):
            limited = store.session_change_files("root")
        assert limited["truncated"] and [item["file"] for item in limited["files"]] == ["one"]
        with patch("opentab.stores.opencode.CHANGE_SUMMARY_BYTES", 1):
            assert store.session_change_files("root")["files"] == []


def test_opencode_changes_keep_snapshot_support_without_an_ownable_part_schema():
    with _conversation_db(legacy=True) as (db, writer, store):
        writer.execute(
            "insert into message values (?, ?, ?)",
            (
                "prompt",
                "root",
                json.dumps(
                    {
                        "role": "user",
                        "time": {"created": 1},
                        "summary": {"diffs": [{"file": "legacy", "patch": "patch"}]},
                    }
                ),
            ),
        )
        writer.commit()
        assert store.session_change_files("root")["files"][0]["edits"][0]["source"] == "snapshot"
        store.conn.close()
        writer.execute("drop table part")
        writer.commit()
        no_parts = ot.Store(db, type("Args", (), {"demo": False})())
        try:
            assert no_parts.session_change_files("root")["files"][0]["file"] == "legacy"
        finally:
            no_parts.conn.close()


def test_opencode_changes_keep_child_worktrees_distinct_and_use_absolute_tool_paths():
    with _conversation_db(revisions=True, directory="/repo") as (_, writer, store):
        for child in ("left", "right"):
            base = f"/repo/.worktrees/{child}"
            writer.execute("insert into session values (?, 'root', ?)", [child, base])
            _change_message(
                writer,
                f"p-{child}",
                child,
                1,
                [{"file": "src/a.py", "patch": "snapshot duplicate"}],
            )
            _change_tool(
                writer,
                f"p-{child}",
                f"tool-{child}",
                child,
                2,
                "apply_patch",
                {
                    "status": "completed",
                    "metadata": {
                        "files": [
                            {
                                "type": "update",
                                "filePath": base + "/src/a.py",
                                "relativePath": "src/a.py",
                                "patch": child,
                            }
                        ]
                    },
                },
            )
        writer.commit()
        files = store.session_change_files("root")["files"]
        assert [f["file"] for f in files] == [
            ".worktrees/left/src/a.py",
            ".worktrees/right/src/a.py",
        ]
        for file, child in zip(files, ("left", "right")):
            assert [edit["source"] for edit in file["edits"]] == ["snapshot", "apply_patch"]
            assert file["counts_overlap"]
            snapshot = store.session_change_diff("root", file["edits"][0]["key"])
            assert snapshot["file"] == file["file"] and snapshot["patch"] == "snapshot duplicate"
            diff = store.session_change_diff("root", file["edits"][1]["key"])
            assert diff["file"] == file["file"] and diff["patch"] == child


def test_opencode_changes_skip_malformed_files_without_losing_valid_neighbors():
    with _conversation_db(revisions=True) as (_, writer, store):
        _change_message(writer, "p", "root", 1, [])
        files = [
            None,
            "not json",
            123,
            {"type": "update", "filePath": 12},
            {"type": "update", "filePath": "good", "patch": "good patch"},
        ]
        _change_tool(
            writer,
            "p",
            "tool",
            "root",
            2,
            "apply_patch",
            {"status": "completed", "metadata": {"files": files}},
        )
        _change_tool(
            writer,
            "p",
            "bad-array",
            "root",
            3,
            "apply_patch",
            {"status": "completed", "metadata": {"files": {"0": files[-1]}}},
        )
        writer.execute(
            "insert into part (id, message_id, session_id, data, time_created) "
            "values ('broken', 'tool', 'root', 'not json', 3)"
        )
        writer.commit()
        result = store.session_change_files("root")
        assert [f["file"] for f in result["files"]] == ["good"]
        assert len(result["files"][0]["edits"]) == 1
        key = result["files"][0]["edits"][0]["key"]
        assert store.session_change_diff("root", key)["patch"] == "good patch"


def test_opencode_changes_use_indexed_tree_reads_and_rowid_patch_lookups():
    with _conversation_db(revisions=True) as (db, writer, store):
        writer.executemany(
            "insert into session values (?, ?)",
            [("child", "root"), ("outside", None)],
        )
        _change_message(writer, "snapshot", "root", 1, [{"file": "snapshot", "patch": "s"}])
        _change_message(writer, "prompt", "child", 2, [])
        _change_tool(
            writer,
            "prompt",
            "tool",
            "child",
            3,
            "apply_patch",
            {
                "status": "completed",
                "metadata": {"files": [{"type": "update", "filePath": "native", "patch": "n"}]},
            },
        )
        _change_message(writer, "outside", "outside", 4, [{"file": "PRIVATE", "patch": "x"}])
        writer.commit()

        connect = sqlite3.connect
        queries = []

        def traced_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(queries.append)
            return conn

        with patch("opentab.stores.opencode.sqlite3.connect", traced_connect):
            files = {row["file"]: row for row in store.session_change_files("root")["files"]}
        list_queries = list(queries)
        snapshot_sql = next(sql for sql in list_queries if "cross join message m" in sql)
        native_sql = next(sql for sql in list_queries if "cross join part p" in sql)
        snapshot_plan = [row[3] for row in writer.execute("explain query plan " + snapshot_sql)]
        native_plan = [row[3] for row in writer.execute("explain query plan " + native_sql)]
        assert any(
            "SEARCH m USING INDEX message_session_idx" in step for step in snapshot_plan
        ), snapshot_plan
        assert not any("SCAN m" in step for step in snapshot_plan), snapshot_plan
        assert any(
            "SEARCH p USING INDEX part_session_idx" in step for step in native_plan
        ), native_plan
        assert not any(
            step == "SCAN p" or step.startswith("SCAN p ") for step in native_plan
        ), native_plan

        for name, table in (("native", "part"), ("snapshot", "message")):
            queries.clear()
            with patch("opentab.stores.opencode.sqlite3.connect", traced_connect):
                assert store.session_change_diff("root", files[name]["edits"][0]["key"])["patch"]
            if name == "native":
                assert not any("cross join message m" in sql for sql in queries)
            else:
                assert not any("cross join part p" in sql for sql in queries)
            validation_sql = next(sql for sql in queries if f"cross join {table} " in sql)
            validation_plan = [
                row[3] for row in writer.execute("explain query plan " + validation_sql)
            ]
            assert any(
                f"SEARCH {table[0]} USING INTEGER PRIMARY KEY" in step for step in validation_plan
            ), validation_plan
            body_sql = next(
                sql
                for sql in queries
                if f"from {table} " in sql and f"where {table[0]}.rowid =" in sql
            )
            plan = [row[3] for row in writer.execute("explain query plan " + body_sql)]
            assert any(
                f"SEARCH {table[0]} USING INTEGER PRIMARY KEY" in step for step in plan
            ), plan


def test_opencode_changes_locators_are_untrusted_and_invalid_numbers_skip_patch_bodies():
    with _conversation_db(revisions=True, constrained=False) as (db, writer, store):
        writer.execute("insert into session values ('outside', null)")
        for sid in ("root", "outside"):
            _change_message(writer, sid + "-prompt", sid, 1, [])
            _change_tool(
                writer,
                sid + "-prompt",
                sid + "-tool",
                sid,
                2,
                "apply_patch",
                {
                    "status": "completed",
                    "metadata": {"files": [{"type": "update", "filePath": sid, "patch": sid}]},
                },
            )
        writer.commit()
        key = store.session_change_files("root")["files"][0]["edits"][0]["key"]
        outside_row = writer.execute(
            "select rowid from part where session_id = 'outside'"
        ).fetchone()[0]
        _, _, rowid, index, digest = key.split(":")
        bad_keys = [
            f"occhg1:apply_patch:{outside_row}:{index}:{digest}",
            f"occhg1:apply_patch:{rowid}:1:{digest}",
            f"occhg1:edit:{rowid}:{index}:{digest}",
            f"occhg1:apply_patch:not-a-row:{index}:{digest}",
            f"occhg1:apply_patch:9223372036854775808:{index}:{digest}",
            f"occhg1:apply_patch:{rowid}:9223372036854775808:{digest}",
        ]
        store.conn.close()
        fresh = ot.Store(db, type("Args", (), {"demo": False})())
        queries = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(queries.append)
            return conn

        try:
            with patch("opentab.stores.opencode.sqlite3.connect", traced_connect):
                assert fresh.session_change_diff("root", key)["patch"] == "root"
                queries.clear()
                assert all(fresh.session_change_diff("root", bad) is None for bad in bad_keys)
            assert not any(
                "from part p" in sql and "where p.rowid =" in sql and "cross join" not in sql
                for sql in queries
            )
            assert not any("from message m join json_each" in sql for sql in queries)
        finally:
            fresh.conn.close()


def test_opencode_changes_direct_lookup_rechecks_tree_membership_and_unique_native_rows():
    with _conversation_db(revisions=True, constrained=False) as (db, writer, store):
        writer.execute("insert into session values ('child', 'root')")
        _change_message(writer, "prompt", "child", 1, [])
        _change_tool(
            writer,
            "prompt",
            "tool",
            "child",
            2,
            "apply_patch",
            {
                "status": "completed",
                "metadata": {"files": [{"type": "update", "filePath": "a", "patch": "p"}]},
            },
        )
        writer.commit()
        key = store.session_change_files("root")["files"][0]["edits"][0]["key"]
        store.conn.close()
        fresh = ot.Store(db, type("Args", (), {"demo": False})())
        try:
            writer.execute("update session set parent_id = null where id = 'child'")
            writer.commit()
            assert fresh.session_change_diff("root", key) is None
            writer.execute("update session set parent_id = 'root' where id = 'child'")
            writer.execute("insert into part select * from part where id = 'part-tool'")
            writer.commit()
            assert fresh.session_change_diff("root", key) is None
            writer.execute("delete from part where rowid = (select max(rowid) from part)")
            writer.execute("insert into message select * from message where id = 'tool'")
            writer.commit()
            assert fresh.session_change_diff("root", key) is None
        finally:
            fresh.conn.close()


def test_opencode_changes_snapshot_key_survives_later_native_edit_for_same_path():
    with _conversation_db(revisions=True) as (db, writer, store):
        _change_message(
            writer,
            "prompt",
            "root",
            1,
            [
                {"file": "same", "patch": "snapshot"},
                {"file": "fallback", "patch": "fallback"},
            ],
        )
        writer.commit()
        keys = {
            row["file"]: row["edits"][0]["key"]
            for row in store.session_change_files("root")["files"]
        }
        _change_tool(
            writer,
            "prompt",
            "tool",
            "root",
            2,
            "apply_patch",
            {
                "status": "completed",
                "metadata": {"files": [{"type": "update", "filePath": "same", "patch": "native"}]},
            },
        )
        writer.commit()
        store.conn.close()
        fresh = ot.Store(db, type("Args", (), {"demo": False})())
        try:
            assert fresh.session_change_diff("root", keys["same"])["patch"] == "snapshot"
            assert fresh.session_change_diff("root", keys["fallback"])["patch"] == "fallback"
            same = fresh.session_change_files("root")["files"][0]
            assert [edit["source"] for edit in same["edits"]] == ["snapshot", "apply_patch"]
            assert same["edits"][0]["key"] == keys["same"]
        finally:
            fresh.conn.close()


def test_opencode_conversation_manifest_tracks_wal_commits_but_not_shm_reader_churn():
    with _conversation_db() as (db, writer, store):
        writer.execute("pragma journal_mode=wal")
        store.conn.execute("select count(*) from session").fetchone()
        initial = store.conversation_manifest("root")
        store.conn.execute("select count(*) from session").fetchone()
        assert store.conversation_manifest("root") == initial

        with patch("opentab.conversations.reader.source_manifest", return_value=[["fixed"]]):
            before_commit = store.conversation_manifest("root")
            writer.execute("insert into session values ('other', null)")
            writer.commit()
            assert store.conversation_manifest("root") != before_commit

        initial = store.conversation_manifest("root")
        with open(db + "-shm", "r+b") as stream:
            stream.seek(120)
            byte = stream.read(1)
            stream.seek(120)
            stream.write(bytes([(byte[0] if byte else 0) ^ 1]))
        assert store.conversation_manifest("root") == initial

        writer.execute("insert into session values ('another', null)")
        writer.commit()
        assert store.conversation_manifest("root") != initial


def test_opencode_root_manifest_tracks_every_row_revision_and_import_without_global_churn():
    with _conversation_db(revisions=True) as (_, writer, store):
        writer.execute("pragma journal_mode=wal")
        writer.execute("insert into session values ('other', null)")
        for sid in ("root", "other"):
            writer.executemany(
                "insert into message values (?, ?, ?, ?, ?)",
                [(sid + str(n), sid, '{"role":"user"}', n, n) for n in (1, 100)],
            )
            writer.execute(
                "insert into part values (?, ?, ?, ?, 1, 1)",
                (sid, sid + "1", '{"type":"text","text":"PRIVATE"}', sid),
            )
        writer.commit()
        initial = store.conversation_manifest("root")
        assert initial[0] == "opencode-root-v1"
        assert json.loads(json.dumps(initial)) == initial
        assert "PRIVATE" not in json.dumps(initial)
        writer.execute("update part set time_updated = 2 where session_id = 'other'")
        writer.commit()
        assert store.conversation_manifest("root") == initial
        writer.execute("pragma wal_checkpoint(truncate)")
        assert store.conversation_manifest("root") == initial

        # An old row's edit can leave count and max(time_updated) unchanged.
        writer.execute("update message set time_updated = 2 where id = 'root1'")
        writer.commit()
        changed = store.conversation_manifest("root")
        assert changed != initial
        for sql in (
            'update part set data = \'{"type":"text","text":"edited"}\', time_updated = 3 where id = \'root\'',
            "update part set message_id = 'root100' where id = 'root'",
            "update part set time_created = 5 where id = 'root'",
            "update message set time_created = 6 where id = 'root1'",
            "update part set id = 'replacement' where id = 'root'",
            "update message set id = 'replacement' where id = 'root1'",
            "insert into message values ('import', 'root', '{\"role\":\"user\"}', 1, 1)",
            "insert into part values ('import', 'import', '{\"type\":\"text\",\"text\":\"imported\"}', 'root', 1, 1)",
            "delete from part where id = 'import'",
            "delete from message where id = 'import'",
        ):
            writer.execute(sql)
            writer.commit()
            next_manifest = store.conversation_manifest("root")
            assert next_manifest and next_manifest != changed, sql
            changed = next_manifest


def test_opencode_root_manifest_tracks_subtree_membership_and_both_sides_of_reparenting():
    with _conversation_db(revisions=True) as (_, writer, store):
        writer.execute("insert into session values ('other', null)")
        writer.commit()
        original = store.conversation_manifest("root")
        other = store.conversation_manifest("other")
        writer.execute("insert into session values ('child', 'root')")
        writer.execute("insert into session values ('nested', 'child')")
        writer.execute("insert into message values ('m', 'nested', '{\"role\":\"user\"}', 1, 1)")
        writer.execute("insert into part values ('p', 'm', '{}', 'nested', 1, 1)")
        writer.commit()
        child_added = store.conversation_manifest("root")
        assert child_added != original
        assert store.conversation_manifest("other") == other
        writer.execute("update part set time_updated = 2 where id = 'p'")
        writer.commit()
        assert store.conversation_manifest("root") != child_added
        writer.execute("update session set parent_id = 'other' where id = 'child'")
        writer.commit()
        assert store.conversation_manifest("root") == original
        reparented = store.conversation_manifest("other")
        assert reparented != other
        writer.execute("update part set session_id = 'root' where id = 'p'")
        writer.commit()
        assert store.conversation_manifest("root") != original
        assert store.conversation_manifest("other") != reparented
        writer.execute("delete from session where id in ('child', 'nested')")
        writer.commit()
        assert store.conversation_manifest("other") == other


def test_opencode_root_manifest_includes_event_sequences_and_explicit_message_parent():
    with _conversation_db(revisions=True) as (_, writer, store):
        writer.execute("alter table message add column parent_id text")
        writer.execute("create table event_sequence (aggregate_id text primary key, seq integer)")
        writer.execute("insert into event_sequence values ('root', 1)")
        writer.execute("insert into message values ('m', 'root', '{}', 1, 1, null)")
        writer.commit()
        before = store.conversation_manifest("root")
        writer.execute("insert into event_sequence values ('unrelated', 100)")
        writer.commit()
        assert store.conversation_manifest("root") == before
        # A durable update is visible even when millisecond row stamps collide.
        writer.execute("update message set data = '{\"role\":\"user\"}' where id = 'm'")
        writer.execute("update event_sequence set seq = 2 where aggregate_id = 'root'")
        writer.commit()
        event_changed = store.conversation_manifest("root")
        assert event_changed != before
        writer.execute("update message set parent_id = 'parent' where id = 'm'")
        writer.commit()
        assert store.conversation_manifest("root") != event_changed


def test_opencode_root_manifest_refuses_unknown_recent_or_future_revisions_and_weak_schema():
    with _conversation_db(revisions=True) as (_, writer, store):
        writer.execute("insert into message values ('m', 'root', '{}', 1, 1)")
        writer.commit()
        with patch("opentab.stores.opencode.time.time_ns", return_value=10_000_000_000):
            for stamp in (None, "unknown", 9999, 10000, 12000):
                writer.execute("update message set time_updated = ?", [stamp])
                writer.commit()
                assert store.conversation_manifest("root") is None
            writer.execute("update message set time_updated = 9998")
            writer.commit()
            assert store.conversation_manifest("root")[0] == "opencode-root-v1"
        assert store.conversation_manifest("missing") is None
        store.demo = True
        with patch("opentab.stores.opencode.sqlite3.connect", side_effect=AssertionError("demo")):
            assert store.conversation_manifest("root") is None
    with _conversation_db(revisions=True, constrained=False) as (_, _, store):
        assert store.conversation_manifest("root")[0] != "opencode-root-v1"


def test_opencode_root_manifest_reads_only_indexed_metadata_in_one_fresh_snapshot():
    with _conversation_db(revisions=True) as (_, writer, store):
        writer.execute("pragma journal_mode=wal")
        writer.execute("insert into message values ('m', 'root', 'PRIVATE malformed JSON', 1, 1)")
        writer.execute("insert into part values ('p', 'm', 'PRIVATE malformed JSON', 'root', 1, 1)")
        writer.commit()
        before = store.conversation_manifest("root")
        connect = sqlite3.connect
        queries = []

        def trace(sql):
            queries.append(sql)
            if "from part r" in sql:
                writer.execute("update part set time_updated = 2 where id = 'p'")
                writer.execute("insert into session values ('child', 'root')")
                writer.commit()

        def traced_connect(*args, **kwargs):
            assert args[0].endswith("?mode=ro")
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(trace)
            return conn

        with patch("opentab.stores.opencode.sqlite3.connect", traced_connect):
            assert store.conversation_manifest("root") == before
        assert queries[0] == "begin"
        assert all(".data" not in sql and "json_extract" not in sql for sql in queries)
        for sql in queries:
            if "from message r" in sql or "from part r" in sql:
                plan = [r[3] for r in writer.execute("explain query plan " + sql)]
                assert any("SEARCH r USING INDEX" in step for step in plan), plan
                assert not any("SCAN r" in step for step in plan), plan
        assert store.conversation_manifest("root") != before


def test_opencode_root_manifest_rejects_deleted_db_and_invalidates_replacement_identity():
    with _conversation_db(revisions=True) as (db, writer, store):
        before = store.conversation_manifest("root")
        with closing(sqlite3.connect(db + ".replacement")) as replacement:
            writer.backup(replacement)
        writer.close()
        if os.name == "nt":
            store.conn.close()
        os.replace(db + ".replacement", db)
        assert store.conversation_manifest("root") != before
        os.unlink(db)
        assert store.conversation_manifest("root") is None
        assert not os.path.exists(db)


def test_opencode_conversation_preserves_all_messages_and_verbatim_multipart_text():
    with _conversation_db() as (_, writer, store):
        # Insertion and JSON timestamps deliberately disagree with table timestamps.
        for n in reversed(range(125)):
            mid = f"m{n:03}"
            data = {"role": "user" if n == 0 else "assistant", "time": {"created": 500 - n}}
            if n:
                data["parentID"] = "m000"
            writer.execute(
                "insert into message values (?, 'root', ?, ?)", (mid, json.dumps(data), n // 2)
            )
            for pid, ts, text in (
                ("c", 2, ""),
                ("b", 1, "\nsecond\t"),
                ("a", 1, "  first\n\u00e9\x00"),
            ):
                writer.execute(
                    "insert into part values (?, ?, ?, 'root', ?)",
                    (
                        mid + pid,
                        mid,
                        json.dumps({"type": "text", "text": text, "synthetic": False}),
                        ts,
                    ),
                )
        for mid, role in (
            ("empty-user", "user"),
            ("empty-assistant", "assistant"),
            ("system", "system"),
        ):
            writer.execute(
                "insert into message values (?, 'root', ?, 200)", (mid, json.dumps({"role": role}))
            )
            for n, part in enumerate(
                (
                    {"type": "text", "synthetic": True, "text": "PRIVATE synthetic"},
                    {"type": "reasoning", "text": "PRIVATE reasoning"},
                    {"type": "tool", "state": {"output": "PRIVATE tool" * 100000}},
                    {"type": "file", "url": "PRIVATE attachment"},
                )
            ):
                writer.execute(
                    "insert into part values (?, ?, ?, 'root', ?)",
                    (f"{mid}{n}", mid, json.dumps(part), n),
                )
        writer.commit()
        source = store.conversation_source("root")
        records = source["records"]
        assert len(records) == 127  # No 100-message preview cap or usage filter.
        assert [r["message_id"] for r in records[:125]] == [f"m{n:03}" for n in range(125)]
        assert records[0] == {
            "id": "oc:m000",
            "message_id": "m000",
            "record_id": "m000",
            "parent_id": None,
            "execution_id": "root",
            "role": "user",
            "timestamp": 500,
            "origin": "recorded-message",
            "source": {"message_id": "m000"},
            "parts": [
                {"id": "m000a", "text": "  first\n\u00e9\x00", "source": {"part_id": "m000a"}},
                {"id": "m000b", "text": "\nsecond\t", "source": {"part_id": "m000b"}},
                {"id": "m000c", "text": "", "source": {"part_id": "m000c"}},
            ],
        }
        assert records[124]["parent_id"] == "m000"
        assert all(r["parts"] == [] and r["timestamp"] == 200 for r in records[125:])
        assert "PRIVATE" not in json.dumps(source)
        assert source["execution_id"] == "root"
        assert "synthetic_text_excluded" in source["limitations"]
        assert "non_text_parts_excluded" in source["limitations"]
        assert source == store.conversation_source("root")


def test_opencode_conversation_proves_exact_execution_and_part_ownership():
    with _conversation_db() as (_, writer, store):
        writer.executemany(
            "insert into session values (?, ?)",
            [
                ("child", "root"),
                ("sibling", "root"),
                ("nested", "child"),
                ("outside", None),
            ],
        )
        for sid in ("root", "child", "sibling", "nested", "outside"):
            writer.execute("insert into message values (?, ?, ?, 1)", (sid, sid, '{"role":"user"}'))
            writer.execute(
                "insert into part values (?, ?, ?, ?, 1)",
                (sid, sid, json.dumps({"type": "text", "text": sid}), sid),
            )
        for pid, mid, sid in (
            ("foreign-part", "child", "sibling"),
            ("false-message", "sibling", "child"),
        ):
            writer.execute(
                "insert into part values (?, ?, ?, ?, 1)",
                (pid, mid, '{"type":"text","text":"PRIVATE"}', sid),
            )
        writer.commit()
        root = store.conversation_source("root")
        assert [r["message_id"] for r in root["records"]] == ["root"]
        assert root["executions"] == [
            {"id": "child", "parent_id": "root"},
            {"id": "nested", "parent_id": "child"},
            {"id": "root", "parent_id": None},
            {"id": "sibling", "parent_id": "root"},
        ]
        for sid in ("child", "sibling", "nested"):
            source = store.conversation_source("root", sid)
            assert source["executions"] == root["executions"]
            assert [r["message_id"] for r in source["records"]] == [sid]
            assert source["records"][0]["parts"] == [
                {"id": sid, "text": sid, "source": {"part_id": sid}}
            ]
        for root_id, sid in (
            ("child", "sibling"),
            ("root", "outside"),
            ("root", "missing"),
            ("roo", "child"),
            ("root", ""),
        ):
            _conversation_error(store, root_id, sid)
        # Starting at a child is exact, not automatically expanded to its ancestor.
        assert store.conversation_source("child")["executions"] == [
            {"id": "child", "parent_id": "root"},
            {"id": "nested", "parent_id": "child"},
        ]
        assert (
            store.conversation_source("child")["snapshot"]
            != store.conversation_source("root", "child")["snapshot"]
        )
        writer.execute("update session set parent_id = 'nested' where id = 'root'")
        writer.commit()
        _conversation_error(store)


def test_opencode_conversation_legacy_schema_and_raw_timestamp_fallback():
    with _conversation_db(legacy=True) as (_, writer, store):
        for mid, data in (
            ("z", {"role": "assistant", "time": {"created": 4}}),
            ("b", {"role": "user"}),
            ("a", {"role": "user"}),
        ):
            writer.execute("insert into message values (?, 'root', ?)", (mid, json.dumps(data)))
        writer.executemany(
            "insert into part values (?, 'z', ?)",
            [(pid, json.dumps({"type": "text", "text": pid})) for pid in ("z", "a")],
        )
        writer.commit()
        source = store.conversation_source("root")
        assert [r["message_id"] for r in source["records"]] == ["a", "b", "z"]
        assert [r["timestamp"] for r in source["records"]] == [None, None, 4]
        assert [p["id"] for p in source["records"][2]["parts"]] == ["a", "z"]
        assert (
            source["ordering"]
            == "messages: json_extract(m.data, '$.time.created'), m.id; parts: p.id"
        )


def test_opencode_conversation_capability_is_schema_only_and_demo_never_reads():
    with _conversation_db() as (_, writer, store):
        writer.execute("insert into message values ('broken', 'root', 'PRIVATE malformed JSON', 1)")
        writer.commit()
        queries = []
        store.conn.set_trace_callback(queries.append)
        with patch(
            "opentab.stores.opencode.sqlite3.connect", side_effect=AssertionError("No new read")
        ):
            assert store.supports_conversation("root")
            assert store.supports_conversation("nonexistent")  # Not a retention promise.
        assert queries and all(sql.startswith("pragma table_info(") for sql in queries)
        _conversation_error(store)
        store.demo = True
        store.conn.close()
        with patch(
            "opentab.stores.opencode.sqlite3.connect", side_effect=AssertionError("Demo read")
        ):
            assert not store.supports_conversation("root")
            _conversation_error(store)


def test_opencode_conversation_empty_missing_deleted_and_unsupported_are_distinct():
    with _conversation_db() as (db, writer, store):
        assert store.supports_conversation("root")
        assert store.conversation_source("root")["records"] == []
        _conversation_error(store, "missing")
        writer.execute("delete from session")
        writer.commit()
        _conversation_error(store)
        writer.execute("insert into session values ('root', null)")
        writer.execute("drop table part")
        writer.commit()
        assert not store.supports_conversation("root")
        _conversation_error(store)
        writer.close()
        store.conn.close()
        os.unlink(db)
        _conversation_error(store)
        assert not os.path.exists(db)


def test_opencode_conversation_rejects_ambiguous_native_ids():
    for table in ("session", "message", "part"):
        with _conversation_db(legacy=True, constrained=False) as (_, writer, store):
            writer.execute("insert into message values ('m', 'root', '{\"role\":\"user\"}')")
            writer.execute(
                'insert into part values (\'p\', \'m\', \'{"type":"text","text":"hello"}\')'
            )
            if table == "session":
                writer.execute("insert into session values ('root', 'outside')")
            elif table == "message":
                writer.execute("insert into message values ('m', 'outside', '{\"role\":\"user\"}')")
            else:
                writer.execute("insert into part select * from part")
            writer.commit()
            _conversation_error(store)


def test_opencode_conversation_does_not_read_a_deleted_database_from_the_old_connection():
    with _conversation_db(legacy=True) as (db, writer, store):
        writer.execute("insert into message values ('m', 'root', '{\"role\":\"assistant\"}')")
        writer.execute(
            'insert into part values (\'p\', \'m\', \'{"type":"text","text":"retained"}\')'
        )
        writer.commit()
        assert store.conversation_source("root")["records"][0]["parts"][0]["text"] == "retained"
        writer.close()
        if os.name == "nt":
            store.conn.close()  # Windows cannot unlink the open database.
        os.unlink(db)
        # POSIX leaves the original reader's file descriptor usable after unlink.
        if os.name != "nt":
            assert store.conn.execute("select count(*) from message").fetchone()[0] == 1
        _conversation_error(store)
        assert not os.path.exists(db)


def test_opencode_conversation_snapshot_is_fresh_and_hashes_text_and_execution_metadata():
    with _conversation_db(legacy=True) as (_, writer, store):
        empty = store.conversation_source("root")["snapshot"]
        assert len(empty) == 64 and int(empty, 16) >= 0
        writer.execute("insert into message values ('m', 'root', '{\"role\":\"assistant\"}')")
        writer.execute(
            'insert into part values (\'p\', \'m\', \'{"type":"text","text":"before"}\')'
        )
        writer.commit()
        initial = store.conversation_source("root")
        assert initial["snapshot"] != empty
        initial["records"][0]["parts"][0]["text"] = "caller mutation"
        assert store.conversation_source("root")["snapshot"] == initial["snapshot"]
        writer.execute('update part set data = \'{"type":"text","text":"after"}\'')
        writer.commit()
        edited = store.conversation_source("root")
        assert edited["snapshot"] != initial["snapshot"]
        assert edited["records"][0]["parts"][0]["text"] == "after"
        writer.execute("insert into session values ('child', 'root')")
        writer.commit()
        assert store.conversation_source("root")["snapshot"] != edited["snapshot"]
        writer.execute("delete from part")
        writer.commit()
        assert store.conversation_source("root")["records"][0]["parts"] == []


def test_opencode_conversation_text_budget_is_explicit_and_counts_utf8_bytes():
    with _conversation_db(legacy=True) as (_, writer, store):
        writer.execute("insert into message values ('m', 'root', '{\"role\":\"user\"}')")
        writer.executemany(
            "insert into part values (?, 'm', ?)",
            [
                ("p", json.dumps({"type": "text", "text": "\u00e9" * 4})),
                ("tool", json.dumps({"type": "tool", "text": "PRIVATE" * 10000})),
                (
                    "synthetic",
                    json.dumps({"type": "text", "synthetic": True, "text": "PRIVATE" * 10000}),
                ),
            ],
        )
        writer.commit()
        with patch("opentab.stores.opencode.CONVERSATION_TEXT_BUDGET", 8):
            assert (
                store.conversation_source("root")["records"][0]["parts"][0]["text"] == "\u00e9" * 4
            )
        with patch("opentab.stores.opencode.CONVERSATION_TEXT_BUDGET", 7):
            _conversation_error(store, code="conversation_too_large")


def test_opencode_conversation_transaction_is_coherent_and_sql_never_projects_raw_blobs():
    with _conversation_db(legacy=True) as (db, writer, store):
        writer.execute("pragma journal_mode = wal")
        writer.execute("insert into message values ('m', 'root', '{\"role\":\"assistant\"}')")
        writer.execute(
            'insert into part values (\'p\', \'m\', \'{"type":"text","text":"before"}\')'
        )
        writer.commit()
        before = store.conversation_source("root")
        connect = sqlite3.connect
        queries = []

        def trace(sql):
            queries.append(sql)
            if sql.startswith("select coalesce(sum("):
                writer.execute('update part set data = \'{"type":"text","text":"after"}\'')
                writer.execute("insert into session values ('child', 'root')")
                writer.commit()

        def traced_connect(*args, **kwargs):
            assert args[0].endswith("?mode=ro") and db in args[0]
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(trace)
            return conn

        with patch("opentab.stores.opencode.sqlite3.connect", traced_connect):
            assert store.conversation_source("root") == before
        assert queries[0] == "begin"
        assert all(
            "p.data" not in sql.replace("json_extract(p.data,", "").replace("json_type(p.data,", "")
            for sql in queries
        )
        assert all("m.data" not in sql.replace("json_extract(m.data,", "") for sql in queries)
        assert not any("$.state" in sql or "$.output" in sql or "$.input" in sql for sql in queries)
        assert store.conversation_source("root")["records"][0]["parts"][0]["text"] == "after"


def test_opencode_node_prompt_reads_all_text_parts_from_the_exact_child():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        conn = sqlite3.connect(db)
        conn.executemany(
            "insert into session values (?,?,?,?,?,?)",
            [
                ("s3", "s1", "Explore", "/work/repo", "explore", 1760000000000),
                ("s4", None, "Unrelated", "/elsewhere", "explore", 1760000000000),
                ("s5", "s2", "Nested", "/work/repo", "explore", 1760000000000),
            ],
        )
        prompts = [
            ("later", "s2", 1900, ["A later follow-up, not the initial task"]),
            (
                "child",
                "s2",
                800,
                [
                    "Inspect the parser.\n\n  Preserve this indentation.",
                    "Second block " + "full instructions " * 50 + "END",
                ],
            ),
            ("sibling", "s3", 800, ["The sibling's distinct instructions"]),
            ("outside", "s4", 800, ["Unrelated private prompt"]),
            ("nested", "s5", 800, ["Nested instructions"]),
        ]
        for mid, sid, ts, texts in prompts:
            conn.execute(
                "insert into message values (?,?,?)",
                (
                    mid,
                    sid,
                    json.dumps(
                        {
                            "role": "user",
                            "time": {"created": ts},
                            "summary": {"title": "Misleading short title"},
                        }
                    ),
                ),
            )
            for i, text in enumerate(texts):
                conn.execute(
                    "insert into part values (?,?,?,?)",
                    (f"{mid}-{i}", mid, sid, json.dumps({"type": "text", "text": text})),
                )
        conn.commit()
        conn.close()
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert store.node_prompt("s1", "s2") == "\n\n".join(prompts[1][3])
        assert store.node_prompt("s1", "s3") == prompts[2][3][0]  # no assistant or usage required
        assert store.node_prompt("s1", "s5") == "Nested instructions"
        for root, child in (
            ("s1", "s4"),
            ("s2", "s3"),
            ("missing", "s2"),
            ("s1", "s1"),
            ("s1", "missing"),
        ):
            assert store.node_prompt(root, child) is None
        store.demo = True
        store.conn.close()  # demo guard must run before any SQL/content read
        assert store.node_prompt("s1", "s2") is None


def test_opencode_node_prompt_never_falls_back_to_summary_or_title():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert store.node_prompt("s1", "s2") is None
        assert len(store.node_timeline("s1", "s2")) == 1  # no user prompt required
        store.supports_tool_breakdown = False
        store.conn.close()
        assert store.node_prompt("s1", "s2") is None


def test_opencode_node_turns_and_content_are_exact_execution_only():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        conn = sqlite3.connect(db)
        conn.executemany(
            "insert into session values (?,?,?,?,?,?)",
            [
                (sid, parent, sid, "/repo", "same-agent", 1)
                for sid, parent in (
                    ("sibling", "s1"),
                    ("nested", "s2"),
                    ("outside", None),
                    ("empty", "s1"),
                )
            ],
        )
        for sid in ("s1", "s2", "sibling", "nested", "outside"):
            for role, ts in (("user", 900), ("assistant", 1600)):
                mid = sid + role
                conn.execute(
                    "insert into message values (?,?,?)",
                    (
                        mid,
                        sid,
                        json.dumps(
                            {"role": role, "time": {"created": ts}, "tokens": {"input": 10}}
                        ),
                    ),
                )
                conn.execute(
                    "insert into part values (?,?,?,?)",
                    (
                        mid,
                        mid,
                        sid,
                        json.dumps({"type": "text", "text": mid}),
                    ),
                )
        # A corrupt part claims the child but names a sibling's assistant message.
        conn.execute(
            "insert into part values (?,?,?,?)",
            (
                "wrong-owner",
                "siblingassistant",
                "s2",
                json.dumps({"type": "text", "text": "must not leak"}),
            ),
        )
        conn.commit()
        conn.close()
        store = ot.Store(db, type("Args", (), {"demo": False})())
        before = store.message_timeline("s1")
        rows = store.node_timeline("s1", "s2")
        assert [r["content_key"] for r in rows] == ["m3", "s2assistant"]
        assert all(r["depth"] == 0 and r["prompt_full"] == "s2user" for r in rows)
        assert len(store.node_timeline("s1", "s1")) == 3
        assert store.node_timeline("s1", "empty") == []
        assert store.node_timeline("s1", "nested")[0]["content_key"] == "nestedassistant"
        content = store.node_turn_content("s1", "s2")
        assert set(content) == {"s2assistant"}
        assert content["s2assistant"][0]["text"] == "s2assistant"
        assert store.node_turn_content("s1", "s2", "s2assistant") == content
        for key in ("m1", "siblingassistant", "nestedassistant", "s2user", "missing", ""):
            assert store.node_turn_content("s1", "s2", key) == {}
        for root, child in (
            ("s1", "outside"),
            ("s2", "sibling"),
            ("missing", "s2"),
            ("s1", "missing"),
            ("missing", "missing"),
        ):
            assert store.node_timeline(root, child) is None
            assert store.node_turn_content(root, child) == {}
        assert store.message_timeline("s1") == before
        store.demo = True
        store.conn.close()
        assert store.node_timeline("s1", "s2") is None
        assert store.node_turn_content("s1", "s2") == {}


def test_reconcile_makes_models_sum_to_session_total():
    app = ot.App.__new__(ot.App)

    class _Store:
        demo = True

    app.store = _Store()
    app.loaded = [
        ot.Workflow(
            id="r",
            title="t",
            directory="d",
            created_at="2026-01-01",
            root_cost=0.0,
            total_cost=100.0,
            subagents=0,
            model_count=1,
            total_tokens=1000,
            unpriced_tokens=0,
        )
    ]
    app._model_by_root = {
        "r": [
            {
                "model_name": "m1",
                "runs": 1,
                "cost": 0.0,
                "tokens_total": 0,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
            },
        ]
    }
    app._reconcile_demo_models()
    rows = app._model_by_root["r"]
    assert round(sum(r["cost"] for r in rows), 2) == 100.0
    assert sum(r["tokens_total"] for r in rows) == 1000


def test_tool_breakdown_even_splits_parallel_tool_calls():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert store.supports_tool_breakdown
        rows = {r["tool"]: r for r in store.tool_breakdown("s1")}
        # m1's 2M tokens split across its two tools -> 1M each; bash also gets m2's 6M.
        assert round(rows["bash"]["tokens_total"]) == 7_000_000
        assert round(rows["serena_read_file"]["tokens_total"]) == 1_000_000
        assert rows["bash"]["calls"] == 2
        assert rows["serena_read_file"]["calls"] == 1
        # Only the priced step carries real cost; it lands on bash, serena stays $0.
        assert rows["bash"]["cost"] == 6.0
        assert rows["serena_read_file"]["cost"] == 0
        # Attributed tokens reconcile to the tool-calling steps' totals (2M + 6M).
        assert round(sum(r["tokens_total"] for r in rows.values())) == 8_000_000


def test_tools_tab_offered_only_with_part_table():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        app = ot.App(ot.Store(db, type("A", (), {"demo": False})()), args())
        app.view = "session"
        assert app.current_tabs() == (
            "Overview",
            "Subagents",
            "Turns",
            "Tools",
            "Changes",
            "Context",
        )
    # A backend without the part table / support flag never shows the tabs.
    bare = ot.App(FakeStore([]), args())
    bare.view = "session"
    assert "Tools" not in bare.current_tabs()
    assert "Turns" not in bare.current_tabs()
    assert "Context" not in bare.current_tabs()  # the curve needs turn rows


def test_message_timeline_orders_by_time_and_marks_subagent_turns():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert store.supports_turns("s1")
        rows = store.message_timeline("s1")
        # chronological (t=1000, 1500, 2000), NOT insertion order (m2,m1,m3)
        assert [r["tokens_total"] for r in rows] == [1_000_000, 2_000_000, 500_000]
        assert [r["cost"] for r in rows] == [0, 0, 3.0]
        # the middle turn is the subagent (depth 1, its session's agent label)
        assert [r["depth"] for r in rows] == [0, 1, 0]
        assert rows[1]["agent"] == "explore"
        assert rows[0]["agent"] == "-" and rows[2]["agent"] == "-"
        assert rows[1]["model_name"] == "anthropic/claude-haiku-4.5"
        # each turn is tagged with the user prompt that owns it (most recent in time):
        # u1 (summary.title) owns m1 + the subagent m3; u2 owns the later m2.
        assert [r["prompt_title"] for r in rows] == [
            "Add feature X",
            "Add feature X",
            "Fix the bug",
        ]
        assert rows[0]["prompt_id"] == "u1" and rows[2]["prompt_id"] == "u2"


def test_turn_rows_carry_the_reasoning_variant_opencode_records():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        rows = store.message_timeline("s1")
        # Per MESSAGE, so a mid-session switch shows up as one: m1 ran high, m2 medium.
        # The variant-less subagent turn reports "" and is NOT back-filled from the
        # session's current model setting -- that would invent a level for 200 messages
        # on a real corpus (131 of them Claude rows, which have no variant at all), and
        # an invented level feeds the cache-miss verdict, printing a ⚙ marker that
        # blames a decision nobody made.
        assert [r["effort"] for r in rows] == ["high", "", "medium"]
        # The whole-corpus batch shares _timeline_columns, so an export cannot ship a
        # different set of columns than the TUI reads.
        assert store.message_timeline_all()["s1"] == rows


def test_turn_rows_carry_the_tools_each_step_called():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        rows = store.message_timeline("s1")
        assert [r["tools"] for r in rows] == [["bash", "serena_read_file"], ["bash"]]

        # The whole-corpus batch resolves them from ONE scan of `part`, and must agree
        # with the per-session path exactly -- they are the same table, and an export
        # that disagreed with the TUI about which tools a turn called would be worse
        # than one that shipped none.
        assert store.message_timeline_all()["s1"] == rows


def test_zero_usage_tool_calls_match_the_ranking_and_per_call_timeline():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "insert into message values (?,?,?)",
                (
                    "m0",
                    "s1",
                    '{"role":"assistant","providerID":"anthropic",'
                    '"modelID":"claude-haiku-4.5","cost":0,'
                    '"time":{"created":1},"tokens":{}}',
                ),
            )
            conn.executemany(
                "insert into part values (?,?,?,?)",
                (
                    ("p0a", "m0", "s1", '{"type":"tool","tool":"task"}'),
                    ("p0b", "m0", "s1", '{"type":"tool","tool":"task"}'),
                ),
            )
        store = ot.Store(db, type("A", (), {"demo": False})())
        ranking_calls = sum(row["calls"] for row in store.tool_breakdown("s1"))
        timeline_calls = sum(len(row["tools"]) for row in store.message_timeline("s1"))
        assert ranking_calls == timeline_calls == 5
        zero = next(row for row in store.message_timeline("s1") if row["content_key"] == "m0")
        assert zero["tools"] == ["task", "task"] and zero["tokens_total"] == zero["cost"] == 0


def test_the_tool_join_is_a_separate_scan_not_a_per_row_subquery():
    # Tool names must come from one grouped part-table scan, never one query per message.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        seen = []
        store.conn.set_trace_callback(lambda sql: seen.append(" ".join(str(sql).split())))
        store.message_timeline_all()
        store.conn.set_trace_callback(None)
        # Exactly one statement reads the tools, and it is a standalone grouped scan.
        tool_scans = [s for s in seen if "'$.tool'" in s]
        assert len(tool_scans) == 1, tool_scans
        assert tool_scans[0].startswith("select message_id"), tool_scans[0]
        assert "m.id" not in tool_scans[0]  # nothing correlated to the message row

        # ...and the timeline query itself never grew a per-row tools cell. It keeps its
        # ONE pre-existing `part` subquery, for the raw prompt text -- which is gated on
        # `role = 'user'`, so it is evaluated for the prompts, not for every turn.
        (timeline,) = (s for s in seen if "tokens_total" in s)
        assert "'$.tool'" not in timeline
        assert timeline.count("from part") == 1


def test_turn_rows_carry_no_tools_when_the_schema_has_no_part_table():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            create table session (
              id text primary key, parent_id text, title text, directory text, agent text,
              time_created integer
            );
            create table message (id text primary key, session_id text, data text);
            """
        )
        conn.execute(
            "insert into session values (?,?,?,?,?,?)",
            ("s1", None, "Root", "/work/repo", None, 1760000000000),
        )
        conn.execute(
            "insert into message values (?,?,?)",
            (
                "m1",
                "s1",
                '{"role":"assistant","providerID":"anthropic","modelID":"claude-haiku-4.5",'
                '"cost":1.0,"time":{"created":1000},"tokens":{"input":10,"output":1}}',
            ),
        )
        conn.commit()
        conn.close()
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert store.supports_tool_breakdown is False
        assert [r["tools"] for r in store.message_timeline("s1")] == [[]]


def test_message_timeline_all_matches_the_per_session_path():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        store = ot.Store(db, type("A", (), {"demo": False})())
        batch = store.message_timeline_all()
        assert set(batch) == {"s1"}  # s2 is a subagent -> folded under root s1, not keyed
        assert batch["s1"] == store.message_timeline("s1")  # exact, incl. the s2 depth-1 turn
        assert [r["depth"] for r in batch["s1"]] == [0, 1, 0]


def test_a_subagents_task_message_does_not_open_a_prompt_of_its_own():
    # A subagent user message is an agent-authored task, not a human prompt boundary.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "insert into message values (?,?,?)",
            (
                "u3",
                "s2",  # the SUBAGENT session: agent-authored, and it precedes m3
                '{"role":"user","time":{"created":1200},'
                '"summary":{"title":"Search the auth files"}}',
            ),
        )
        conn.commit()
        conn.close()
        store = ot.Store(db, type("A", (), {"demo": False})())
        rows = store.message_timeline("s1")
        # The subagent turn still belongs to the human prompt that spawned it, and the
        # later main-thread turn still belongs to the prompt that follows -- the task
        # text never becomes a group of its own.
        assert [r["prompt_title"] for r in rows] == [
            "Add feature X",
            "Add feature X",
            "Fix the bug",
        ]
        assert "Search the auth files" not in [r["prompt_title"] for r in rows]
        assert [r["prompt_id"] for r in rows] == ["u1", "u1", "u2"]
        # and the batch export path, which shares _process_timeline, agrees exactly
        assert store.message_timeline_all()["s1"] == rows


def _write_opencode_db_with_long_prompt(path, long_prompt):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table session (
          id text primary key, parent_id text, title text, directory text, agent text,
          time_created integer
        );
        create table message (id text primary key, session_id text, data text);
        create table part (id text primary key, message_id text, session_id text, data text);
        """
    )
    conn.execute(
        "insert into session values (?,?,?,?,?,?)",
        ("s1", None, "Root", "/work/repo", None, 1760000000000),
    )
    user = {"role": "user", "time": {"created": 500}}
    part = {"type": "text", "text": long_prompt}
    turn = {
        "role": "assistant",
        "providerID": "anthropic",
        "modelID": "claude-opus-4-8",
        "cost": 2.0,
        "time": {"created": 1000},
        "tokens": {"input": 100, "output": 10},
    }
    conn.executemany(
        "insert into message values (?,?,?)",
        [("u1", "s1", json.dumps(user)), ("m1", "s1", json.dumps(turn))],
    )
    conn.execute("insert into part values (?,?,?,?)", ("p1", "u1", "s1", json.dumps(part)))
    conn.commit()
    conn.close()


def test_opencode_turns_carry_the_full_prompt_uncapped():
    long_prompt = ("rework the cache invalidation and explain the tradeoffs " * 5).strip()
    long_prompt += "\nthen run the whole suite"
    assert len(long_prompt) > 200
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_long_prompt(db, long_prompt)
        store = ot.Store(db, type("A", (), {"demo": False})())
        rows = store.message_timeline("s1")
        assert rows[0]["prompt_full"] == long_prompt
        assert rows[0]["prompt_title"] == " ".join(long_prompt.split())[:160]

        # The table shows the prompt CAPPED to its cell; the whole text lives in the
        # popup Enter opens, so a pasted essay can never push the table off the pane.
        app = ot.App(store, args())
        rnd = app.renderer  # the instance _apply_click resolves rows against
        wf = app.loaded[0]
        table = rnd.detail_turns(wf, 96)
        assert not any(ln.startswith("  │") for ln in table)
        assert not any(" ".join(long_prompt.split()) in ln for ln in table)  # capped
        app.open_turn_drill(0)
        body = " ".join(rnd.detail_turn_drill(wf, 90))
        assert "then run the whole suite" in body  # the tail survived
        assert " ".join(long_prompt.split()) in " ".join(body.split())  # nothing lost
        app.turn_drill = None
        rnd.detail_turns(wf, 96)  # a paint pass records the row line indices
        idx, pid = next(iter(rnd._turn_header_at.items()))
        app._apply_click(("turnline", idx), drill=False)
        assert app.turn_drill == pid
        assert "then run the whole suite" in " ".join(rnd.detail_turn_drill(wf, 90))
        assert rnd.detail_turns(wf, 96) == rnd.detail_turns(wf, 96)


def test_store_reads_db_without_session_token_columns():
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
              time_created integer
            );
            create table message (session_id text, data text);
            """
        )
        conn.executemany(
            "insert into session values (?, ?, ?, ?, ?)",
            [
                ("root", None, "Root", "/tmp/project", 1760000000000),
                ("child", "root", "Child", "/tmp/project", 1760000001000),
            ],
        )
        conn.executemany(
            "insert into message values (?, ?)",
            [
                (
                    "root",
                    '{"role":"assistant","providerID":"openai","modelID":"gpt-5-mini","cost":1.25,"tokens":{"total":10,"input":4,"output":6}}',
                ),
                (
                    "child",
                    '{"role":"assistant","providerID":"anthropic","modelID":"claude-sonnet-4.5","cost":0,"tokens":{"total":5,"input":2,"output":3}}',
                ),
            ],
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {"demo": False})()
        store = ot.Store(db, args)
        workflows = store.workflows()
        nodes = store.workflow_nodes("root")

        assert len(workflows) == 1
        assert workflows[0].total_cost == 1.25
        assert workflows[0].root_cost == 1.25
        assert workflows[0].total_tokens == 15
        assert workflows[0].unpriced_tokens == 5
        assert workflows[0].subagents == 1
        assert nodes[1]["tokens_total"] == 5
        assert nodes[1]["agent"] == "-"


def test_workflows_ended_at_is_the_latest_update_in_the_subtree():
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
              time_updated integer
            );
            create table message (session_id text, data text);
            """
        )
        conn.executemany(
            "insert into session values (?, ?, ?, ?, ?, ?)",
            [
                # root's own time_updated is earlier than its subagent child's --
                # ended_at must reflect the child bumping the whole subtree.
                ("root", None, "Root", "/tmp/project", 1760000000000, 1760000001000),
                ("child", "root", "Child", "/tmp/project", 1760000000500, 1760000005000),
            ],
        )
        conn.executemany(
            "insert into message values (?, ?)",
            [
                (
                    "root",
                    '{"role":"assistant","providerID":"openai","modelID":"gpt-5-mini","cost":1.0,"tokens":{"input":1,"output":1}}',
                ),
                (
                    "child",
                    '{"role":"assistant","providerID":"openai","modelID":"gpt-5-mini","cost":0,"tokens":{"input":1,"output":1}}',
                ),
            ],
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {"demo": False})()
        store = ot.Store(db, args)
        workflow = store.workflows()[0]

        # localtime rendering is TZ-dependent, so compare against sqlite's own
        # conversion of each raw epoch-ms rather than a hardcoded wall-clock string.
        conn = sqlite3.connect(db)
        expected_created = conn.execute(
            "select datetime(1760000000000 / 1000, 'unixepoch', 'localtime')"
        ).fetchone()[0]
        expected_ended_at = conn.execute(
            "select datetime(1760000005000 / 1000, 'unixepoch', 'localtime')"
        ).fetchone()[0]
        conn.close()

        assert workflow.created_at == expected_created
        assert workflow.ended_at == expected_ended_at
        assert workflow.ended_at > workflow.created_at


def test_workflows_ended_at_is_blank_without_time_updated():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            create table session (
              id text primary key, parent_id text, title text, directory text,
              time_created integer
            );
            create table message (session_id text, data text);
            """
        )
        conn.execute(
            "insert into session values (?, ?, ?, ?, ?)",
            ("root", None, "Root", "/tmp/project", 1760000000000),
        )
        conn.execute(
            "insert into message values (?, ?)",
            (
                "root",
                '{"role":"assistant","providerID":"openai","modelID":"gpt-5-mini","cost":1.0,"tokens":{"input":1,"output":1}}',
            ),
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {"demo": False})()
        store = ot.Store(db, args)
        workflow = store.workflows()[0]

        # Legacy schema: the only signal is the creation time itself, so ended_at is
        # blanked rather than reported as a same-as-start value (the "last_activity"
        # sort's own empty-string check is what falls it back to created_at, not this
        # store).
        assert workflow.ended_at == ""


def test_records_cost_probe_runs_lazily_not_at_construction():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-18T10:00:00Z",
                    "session_id": "s1",
                    "model": "gpt-4o",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cost_usd": 0.05,
                }
            ],
        )
        calls = []
        orig = ot.JsonlStore._probe_records_cost
        ot.JsonlStore._probe_records_cost = lambda self: (calls.append(1), orig(self))[1]
        try:
            store = ot.JsonlStore(path, _jsonl_args())
            assert calls == []  # constructing must not read the file
            assert store.records_cost is True  # first read probes...
            assert store.records_cost is True and calls == [1]  # ...and the answer sticks

            # Parsed first (the cold-start order): the answer derives from the parse's
            # accumulated per-model costs and the probe never runs at all.
            calls.clear()
            store2 = ot.JsonlStore(path, _jsonl_args())
            store2.workflows()
            assert store2.records_cost is True and calls == []
        finally:
            ot.JsonlStore._probe_records_cost = orig

        # pi's parse-derived answer honors the metered/subscription split like the probe:
        # a codex-plan cost is a list-price estimate, not spend -> records_cost False.
        root = os.path.join(tmp, "pi-sessions")
        _pi_write(
            root,
            "--proj--",
            PI_SID,
            [
                _pi_session(PI_SID, tmp),
                _pi_user("hi"),
                _pi_assistant("openai/gpt-5", 10, 5, cost=0.01, provider="openai-codex"),
            ],
        )
        sub = ot.PiStore(root, _pi_args())
        sub.workflows()  # parse first: no probe needed
        assert sub.records_cost is False


def _minimal_db(path, session_cols, message_cols, rows=True):
    conn = sqlite3.connect(path)
    conn.execute("create table session (%s)" % ", ".join(session_cols))
    conn.execute("create table message (%s)" % ", ".join(message_cols))
    if rows:
        data = json.dumps(
            {
                "role": "assistant",
                "providerID": "anthropic",
                "modelID": "claude-opus-4-6",
                "tokens": {"input": 10, "output": 5, "cache": {"read": 0, "write": 0}},
                "time": {"created": 1780000000000},
            }
        )
        conn.execute(
            "insert into session (id, parent_id, time_created) values ('s1', null, 1780000000000)"
        )
        conn.execute("insert into message (id, session_id, data) values ('m1', 's1', ?)", (data,))
    conn.commit()
    conn.close()


def test_required_schema_is_exactly_what_every_query_path_needs():
    args = type("A", (), {"demo": False})()
    session_cols = list(REQUIRED_SCHEMA["session"])
    message_cols = list(REQUIRED_SCHEMA["message"])

    with tempfile.TemporaryDirectory() as tmp:
        # NOT TOO SMALL: a database with only these columns answers every method,
        # including the Turns/Tools opt-ins its supports_* gates say it offers.
        db = os.path.join(tmp, "minimum.db")
        _minimal_db(db, session_cols, message_cols)
        store = ot.Store(db, args)
        workflows = store.workflows()
        assert [w.id for w in workflows] == ["s1"] and workflows[0].total_tokens == 15
        assert store.summary(workflows)["tokens"] == 15
        assert len(store.model_breakdown()) == 1
        assert len(store.workflow_nodes("s1")) == 1
        assert store.root_of("s1") == "s1" and len(store.recent_roots()) == 1
        assert store.supports_turns("s1") and len(store.message_timeline("s1")) == 1
        assert store.supports_tools("s1") is False  # no `part` table: probed, not required
        assert store.tool_breakdown("s1") == []

        # NOT TOO LARGE: drop any one of them and a query path really does break, so
        # none of them is there "just in case".
        for table, col in [("session", c) for c in session_cols] + [
            ("message", c) for c in message_cols
        ]:
            path = os.path.join(tmp, f"without-{table}-{col}.db")
            _minimal_db(
                path,
                [c for c in session_cols if not (table == "session" and c == col)] or ["dummy"],
                [c for c in message_cols if not (table == "message" and c == col)] or ["dummy"],
                rows=False,
            )
            assert ot.sources.opencode_db_verdict(path)[0] == "foreign", f"{table}.{col}"
            try:
                broken = ot.Store(path, args)
                broken.workflows()
                broken.model_breakdown()
                broken.workflow_nodes("x")
                broken.message_timeline("x")
                broken.recent_roots()
            except sqlite3.Error:
                pass  # what the verdict is standing in front of
            else:
                raise AssertionError(f"{table}.{col} is required by nothing -- drop it")


def _write_opencode_trace_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table session (
          id text primary key, parent_id text, title text, directory text, agent text,
          time_created integer
        );
        create table message (id text primary key, session_id text, data text);
        create table part (id text primary key, message_id text, session_id text, data text);
        """
    )
    conn.execute(
        "insert into session values (?,?,?,?,?,?)",
        ("s1", None, "Root", "/work/repo", None, 1760000000000),
    )
    user = {"role": "user", "time": {"created": 500}}
    turn = {
        "role": "assistant",
        "providerID": "anthropic",
        "modelID": "claude-opus-4-8",
        "cost": 2.0,
        "time": {"created": 1000},
        "tokens": {"input": 100, "output": 10},
    }
    conn.executemany(
        "insert into message values (?,?,?)",
        [("u1", "s1", json.dumps(user)), ("m1", "s1", json.dumps(turn))],
    )
    parts = [
        ("p0", "u1", {"type": "text", "text": "fix the cache"}),
        # OpenCode is the one backend of the three that keeps reasoning PROSE.
        ("p1", "m1", {"type": "reasoning", "text": "**Planning** I should look first."}),
        ("p2", "m1", {"type": "text", "text": "Checking the diff."}),
        (
            "p3",
            "m1",
            {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "git diff --stat", "description": "the diff"},
                    "output": "3 files changed",
                },
            },
        ),
        # step-start/step-finish parts carry no content and must not become events.
        ("p4", "m1", {"type": "step-finish", "reason": "tool-calls"}),
    ]
    conn.executemany(
        "insert into part values (?,?,?,?)",
        [(pid, mid, "s1", json.dumps(data)) for pid, mid, data in parts],
    )
    conn.commit()
    conn.close()


def test_opencode_turn_content_reads_narration_reasoning_and_each_calls_arguments():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_trace_db(db)
        store = ot.Store(db, type("A", (), {"demo": False})())

        assert store.supports_turn_content("s1") is True
        assert store.records_reasoning is True
        row = store.message_timeline("s1")[0]
        # The message id is what the part table joins on, so it is the turn's identity.
        assert row["content_key"] == "m1"
        assert row["has_text"] is True and row["has_reasoning"] is True
        events = store.turn_content("s1")["m1"]

        assert [e["kind"] for e in events] == ["reasoning", "text", "tool"]
        assert events[0]["text"].startswith("**Planning**")
        assert events[2]["name"] == "bash" and events[2]["args"] == "git diff --stat"
        assert events[2]["params"] == [("description", "the diff")]
        assert events[2]["output"] == "3 files changed"
        # The user's own prompt part belongs to the prompt header, not to a turn.
        assert "u1" not in store.turn_content("s1")


def test_expanding_one_opencode_turn_recovers_errors_arguments_and_all_events():
    from opentab.util import TRACE_EVENTS_CAP, TRACE_OUTPUT_CAP

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_trace_db(db)
        command = "    python <<'PY'\n" + "print('a  b')\n" * 100 + "PY\n"
        failure = "Permission denied\n" + "details\n" * 800
        params = {f"arg{i}": "x" * 600 for i in range(8)}
        with sqlite3.connect(db) as conn:
            data = {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "error",
                    "error": failure,
                    "input": dict(params, command=command),
                },
            }
            conn.execute("UPDATE part SET data=? WHERE id='p3'", (json.dumps(data),))
            conn.execute(
                "INSERT INTO message SELECT 'm2', session_id, data FROM message WHERE id='m1'"
            )
            conn.execute(
                "INSERT INTO part VALUES ('other', 'm2', 's1', ?)",
                (json.dumps({"type": "text", "text": "other turn"}),),
            )
            conn.executemany(
                "INSERT INTO part VALUES (?, 'm1', 's1', ?)",
                [
                    (f"extra{i}", json.dumps({"type": "text", "text": f"event {i}"}))
                    for i in range(TRACE_EVENTS_CAP)
                ],
            )
        store = ot.Store(db, type("A", (), {"demo": False})())
        preview = store.turn_content("s1")
        assert preview["m1"][2]["status"] == "error"
        assert preview["m1"][2]["output"].startswith("Permission denied")
        assert len(preview["m1"][2]["output"]) <= TRACE_OUTPUT_CAP
        assert len(preview["m1"]) == TRACE_EVENTS_CAP
        full = store.turn_content("s1", content_key="m1")
        assert list(full) == ["m1"] and len(full["m1"]) == TRACE_EVENTS_CAP + 3
        call = full["m1"][2]
        assert call["args"] == command and dict(call["params"]) == params
        assert call["output"] == failure and call["output_dropped"] == 0
        assert store.turn_content("s1") == preview


def test_a_whitespace_only_part_never_marks_a_turn_as_readable():
    # The Read column and the trace must agree: SQLite's one-argument trim() removes
    # ASCII spaces only, so a part holding "\t\n" or a vertical tab marked the row
    # readable and then opened onto nothing. The explicit set covers every ASCII
    # whitespace character Python's str.strip() removes.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_trace_db(db)
        conn = sqlite3.connect(db)
        turn = {
            "role": "assistant",
            "providerID": "anthropic",
            "modelID": "claude-opus-4-8",
            "cost": 1.0,
            "time": {"created": 2000},
            "tokens": {"input": 10, "output": 5},
        }
        conn.execute("insert into message values (?,?,?)", ("m2", "s1", json.dumps(turn)))
        # Every ASCII whitespace character str.strip() removes, the four control
        # SEPARATORS included -- verified against Python over the whole ASCII range.
        for n, blank in enumerate(("   ", "\t\n", "\x0b\x0c", "\x1c\x1f", "\r", "")):
            conn.execute(
                "insert into part values (?,?,?,?)",
                (f"w{n}", "m2", "s1", json.dumps({"type": "text", "text": blank})),
            )
        conn.commit()
        conn.close()
        store = ot.Store(db, type("A", (), {"demo": False})())

        row = next(r for r in store.message_timeline("s1") if r["content_key"] == "m2")
        assert row["has_text"] is False and row["has_reasoning"] is False
        # ...and the trace agrees: nothing to show behind it.
        assert store.turn_content("s1").get("m2", []) == []
