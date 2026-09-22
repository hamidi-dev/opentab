import threading
from unittest.mock import patch

import opentab as ot
from opentab.tui import bindings

from tests._support import AttrScreen, FakeScreen, FakeStore, screen_text, workflow


def _data():
    return {
        "files": [
            {
                "file": "src/one.py",
                "status": "modified",
                "additions": 5,
                "deletions": 2,
                "edits": [
                    {
                        "key": "one",
                        "message_id": "msg-1",
                        "execution_id": "root",
                        "file": "src/one.py",
                        "status": "modified",
                        "additions": 3,
                        "deletions": 1,
                        "available": True,
                    },
                    {
                        "key": "two",
                        "message_id": "msg-2",
                        "execution_id": "child",
                        "file": "src/one.py",
                        "status": "modified",
                        "additions": 2,
                        "deletions": 1,
                        "available": True,
                    },
                ],
            },
            {
                "file": "docs/\x1b[31munsafe\nname.md",
                "status": "added",
                "additions": None,
                "deletions": 0,
                "edits": [],
            },
        ],
        "limitations": ["Missing summaries are not proof of no edits."],
        "truncated": False,
    }


class ChangeStore(FakeStore):
    def __init__(self, workflows, *, demo=False, data=None):
        super().__init__(workflows)
        self.demo = demo
        self.demo_scale = 1.0
        self.demo_cats = frozenset({"spend"})
        self.data = data if data is not None else _data()
        self.support_calls = 0
        self.file_calls = 0
        self.diff_calls = []

    def workflow_nodes(self, workflow_id):
        return []

    def supports_turns(self, workflow_id):
        return False

    def supports_tools(self, workflow_id):
        return False

    def supports_changes(self, workflow_id):
        self.support_calls += 1
        return True

    def session_change_files(self, workflow_id):
        self.file_calls += 1
        return self.data

    def session_change_diff(self, workflow_id, key):
        self.diff_calls.append((workflow_id, key))
        return {
            "file": "src/one.py",
            "patch": "@@ -1 +1 @@\n-old $12\n+new 1.0M\n context",
            "truncated": False,
            "limitation": "",
        }

    def change_request(self, workflow_id, key=None):
        def read(cancelled):
            if cancelled.is_set():
                return None
            if key is None:
                return self.session_change_files(workflow_id)
            return self.session_change_diff(workflow_id, key)

        return read


def _app(store):
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(store, args)
    app.view = "session"
    wf = app.current_session()
    app._nodes_by_session[wf.id] = []
    app.tab = app.current_tabs().index("Changes")
    return app


def _finish_read(app):
    worker = app._changes_worker
    assert worker is not None and worker.wait_for_result()
    app.poll_changes()


def _load_files(app):
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)


def _load_diff(app):
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)


def test_changes_is_lazy_in_two_painted_stages_and_keeps_only_one_patch():
    store = ChangeStore([workflow("root", "2026-09-19 10:00:00")])
    app = _app(store)
    assert store.file_calls == 0 and store.diff_calls == []

    screen = FakeScreen(24, 100)
    with patch.object(ot.curses, "color_pair", side_effect=lambda n: n):
        app.renderer.draw_detail(screen, 0, 0, 22, 100)
    assert "Loading changes" in screen_text(screen)
    assert store.file_calls == 0 and app._changes_loading is not None

    _finish_read(app)
    assert store.file_calls == 1 and store.diff_calls == []
    app.open_change_file()
    screen = FakeScreen(24, 100)
    with patch.object(ot.curses, "color_pair", side_effect=lambda n: n):
        app.renderer.draw_detail(screen, 0, 0, 22, 100)
    assert "Loading selected recorded patch" in screen_text(screen)
    assert store.diff_calls == [] and app._change_diff_loading is not None

    _finish_read(app)
    assert store.diff_calls == [("root", "one")]
    first = app.renderer.detail_changes(app.current_session())
    assert any("-old $12" in line for line in first)
    app.step_change_edit(1)
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    assert store.diff_calls == [("root", "one"), ("root", "two")]
    app.renderer.detail_changes(app.current_session())
    assert app.renderer._change_layout_cache[0][1] == "two"
    app.step_change_edit(-1)
    app.renderer.detail_changes(app.current_session())
    assert app.change_diff_ready()
    assert store.diff_calls == [("root", "one"), ("root", "two")]


def test_changes_list_navigation_mouse_back_and_tab_reset():
    store = ChangeStore([workflow("root", "2026-09-19 10:00:00")])
    app = _app(store)
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    app.renderer.detail_changes(app.current_session())
    second_line = next(line for line, row in app.renderer._change_row_at.items() if row == 1)
    app._apply_click(("changeline", second_line), drill=False)
    assert app._change_file_cursor == 1
    app.scroll = 4
    app._apply_click(("changeline", second_line), drill=True)
    assert app._change_drill
    app.scroll = 7
    assert app.close_change_file()
    assert app.scroll == 4 and app._change_file_cursor == 1

    app.tab = app.current_tabs().index("Changes")
    app.handle_key(None, ord("h"))
    assert app._changes_cache and app._change_diff_cache == {}


def test_changes_keyboard_pager_and_patch_cache_follow_existing_navigation():
    store = ChangeStore([workflow("root", "2026-09-19 10:00:00")])
    app = _app(store)
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    app.handle_key(None, ord("j"))
    assert app._change_file_cursor == 1
    app.handle_key(None, ord("g"))
    assert app._change_file_cursor == 0
    app.handle_key(None, ord("G"))
    assert app._change_file_cursor == 1
    app.handle_key(None, ord("g"))
    app.handle_key(None, 10)
    siblings = ot.keymap.BY_ID["trace-siblings"]
    assert siblings.shown(app) and "recorded edit" in siblings.text(app)
    assert ot.keymap.BY_ID["esc"].text(app) == "back to the file list"
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    first = app.renderer.detail_changes(app.current_session())
    cache = app.renderer._change_layout_cache
    app.scroll = 2
    second = app.renderer.detail_changes(app.current_session())
    assert first is second and app.renderer._change_layout_cache is cache
    narrow = app.renderer.detail_changes(app.current_session(), 30)
    assert narrow is not first
    assert all(ot.display_width(line) <= 30 for line in narrow)
    assert store.diff_calls == [("root", "one")]


def test_space_scrolls_the_loaded_change_diff_by_half_a_page():
    store = ChangeStore([workflow("root", "2026-09-19 10:00:00")])
    app = _app(store)
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    app.open_change_file()
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)

    app.handle_key(None, ord(" "))

    assert app._change_drill and app.scroll == 10
    app.handle_key(None, bindings.SHIFT_SPACE)
    assert app._change_drill and app.scroll == 0


def test_changes_errors_retry_on_reopen_and_stale_scope_never_reads():
    class FlakyStore(ChangeStore):
        def session_change_files(self, workflow_id):
            self.file_calls += 1
            if self.file_calls == 1:
                raise RuntimeError("bad\x1b[31m metadata")
            return self.data

    first = workflow("first", "2026-09-19 10:00:00")
    second = workflow("second", "2026-09-19 11:00:00")
    store = FlakyStore([first, second])
    app = _app(store)
    app.renderer.detail_changes(app.current_session())
    pending = app._changes_loading
    app.workflow_index = 1 - app.workflow_index
    _finish_read(app)
    assert pending is not None and store.file_calls == 1

    app.workflow_index = next(i for i, row in enumerate(app.current_sessions()) if row is first)
    app.tab = app.current_tabs().index("Changes")
    app.renderer.detail_changes(app.current_session())
    assert store.file_calls == 1 and "Could not read" in app.changes_error()
    error_lines = app.renderer.detail_changes(app.current_session())
    assert not any("\x1b" in line or "bad" in line for line in error_lines)
    app.handle_key(None, ord("h"))
    app.handle_key(None, ord("l"))
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    assert store.file_calls == 2 and app.changes_data() is not None


def test_changes_demo_gate_hides_tab_before_any_backend_call():
    class HostileDemo(ChangeStore):
        def supports_changes(self, workflow_id):
            raise AssertionError("demo queried change capability")

    store = HostileDemo([workflow("root", "2026-09-19 10:00:00")], demo=True)
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    demo = ot.App(store, args)
    demo.view = "session"
    assert "Changes" not in demo.current_tabs()
    assert store.file_calls == 0 and store.diff_calls == []


def test_changes_sanitize_paths_and_use_native_diff_roles_without_rich_numbers():
    store = ChangeStore([workflow("root", "2026-09-19 10:00:00")])
    app = _app(store)
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    lines = app.renderer.detail_changes(app.current_session())
    assert not any("\x1b" in line or "\n" in line for line in lines)
    assert any("unsafe name.md" in line for line in lines)
    narrow = app.renderer.detail_changes(app.current_session(), 36)
    row = next(line for line in narrow if "one.py" in line)
    assert row.index("one.py") < 36

    app.open_change_file()
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    lines = app.renderer.detail_changes(app.current_session())
    roles = {str(line)[getattr(line, "gutter", 0) :]: getattr(line, "role", "") for line in lines}
    assert any("@@ -1 +1 @@" in line and line.role == "hunk" for line in lines)
    assert roles["-old $12"] == "delete"
    assert roles["+new 1.0M"] == "add"
    screen = AttrScreen(8, 40)
    with patch.object(ot.curses, "color_pair", side_effect=lambda n: n):
        added = next(line for line in lines if line.endswith("+new 1.0M"))
        app.renderer.write(screen, 0, 0, added, app.renderer.line_attr(added))
    assert screen.attrs[(0, 0)] == 3


def test_combined_changes_resolve_the_exact_duplicate_id_owner():
    from opentab.stores.combined import CombinedStore

    left_wf = workflow("same", "2026-09-19 10:00:00")
    left_wf.source = "OpenCode left"
    right_wf = workflow("same", "2026-09-19 11:00:00")
    right_wf.source = "OpenCode right"
    left = ChangeStore([left_wf], data={**_data(), "limitations": ["left"]})
    right = ChangeStore([right_wf], data={**_data(), "limitations": ["right"]})
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(CombinedStore([left, right]), args)
    app.view = "session"
    app.workflow_index = next(i for i, row in enumerate(app.current_sessions()) if row is left_wf)
    app._nodes_by_session["same"] = []
    assert "Changes" in app.current_tabs()
    app.tab = app.current_tabs().index("Changes")
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    assert left.file_calls == 1 and right.file_calls == 0


def test_changes_navigation_drops_active_drill_but_keeps_lifetime_cache():
    for change in ("session", "tab", "mode", "leave"):
        store = ChangeStore(
            [workflow("root", "2026-09-19 10:00:00"), workflow("other", "2026-09-19 11:00:00")]
        )
        app = _app(store)
        app.renderer.detail_changes(app.current_session())
        _finish_read(app)
        app.open_change_file()
        app.renderer.detail_changes(app.current_session())
        _finish_read(app)
        app.renderer.detail_changes(app.current_session())
        assert app.change_diff_ready() and app.renderer._change_layout_cache is not None
        if change == "session":
            app.workflow_index = 1 - app.workflow_index
        elif change == "tab":
            app.tab = 0
        elif change == "mode":
            app.set_browse_mode("projects")
        else:
            app.drill_out()
        app.settle_changes()
        assert app._changes_cache and app._change_diff_cache, change
        assert app._changes_loading is None and app._change_diff_loading is None, change
        assert app.renderer._change_layout_cache is None and not app._change_drill, change
        assert app._changes_error == app._change_diff_error == "", change
        assert app.renderer._change_row_at == {}, change


def test_changes_reload_and_demo_invalidate_lifetime_cache():
    for change in ("reload", "demo"):
        store = ChangeStore([workflow("root", "2026-09-19 10:00:00")])
        app = _app(store)
        _load_files(app)
        app.open_change_file()
        _load_diff(app)
        if change == "reload":
            app.reload()
        else:
            store.demo = True
            app._invalidate_changes()
        assert app._changes_cache == {}
        assert app._change_diff_cache == {}
        assert not app._change_pending


def test_changes_prefetch_and_exports_never_include_files_or_source():
    from opentab.stores.cached import CachedStore

    store = ChangeStore([workflow("root", "2026-09-19 10:00:00")])
    args = type("Args", (), {"no_cache": True})()
    store.cache_inputs = lambda: []
    app = _app(CachedStore(store, "opencode|changes-fixture", args))
    app.prefetch_session_data(app.current_session().id)
    assert store.file_calls == 0 and store.diff_calls == []
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    assert store.file_calls == 1
    # A Changes tab must not accidentally export its raw rows via the normal CSV path.
    dataset = app._export_dataset()
    assert "src/one.py" not in str(dataset)


def test_changes_enter_in_diff_does_not_reset_occurrence_or_saved_list_scroll():
    app = _app(ChangeStore([workflow("root", "2026-09-19 10:00:00")]))
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    app.scroll = 3
    app.open_change_file()
    app.handle_key(None, ord("]"))
    assert app._change_edit_cursor == 1
    app.scroll = 5
    app.handle_key(None, 10)
    assert app._change_edit_cursor == 1 and app.scroll == 5
    app.handle_key(None, 27)
    assert not app._change_drill and app.scroll == 3


def test_changes_diff_wraps_without_losing_whitespace_or_interpreting_controls():
    from opentab.tui.views.changes import diff_layout

    file = _data()["files"][0]
    text = "+    " + "long_identifier_" * 30 + "\tend"
    lines = diff_layout(file, 0, {"patch": text}, 40)
    added = [line for line in lines if getattr(line, "role", "") == "add"]
    assert "".join(added) == text.replace("\t", "    ")
    assert all(ot.display_width(line) <= 40 for line in lines)
    lines = diff_layout(file, 0, {"patch": "+\x1b[31msecret\x00"}, 40)
    assert not any("\x1b" in line or "\x00" in line for line in lines)


def test_changes_diff_failure_does_not_leak_exception_and_unavailable_does_not_read():
    store = ChangeStore([workflow("root", "2026-09-19 10:00:00")])
    app = _app(store)
    app.renderer.detail_changes(app.current_session())
    _finish_read(app)
    app.open_change_file()
    app.renderer.detail_changes(app.current_session())
    with patch.object(store, "session_change_diff", side_effect=ValueError("PRIVATE SOURCE")):
        _finish_read(app)
    lines = app.renderer.detail_changes(app.current_session())
    assert "PRIVATE" not in str(lines) + str(app._change_diff_error)
    app.close_change_file()
    store.data["files"][0]["edits"][0]["available"] = False
    app.open_change_file()
    lines = app.renderer.detail_changes(app.current_session())
    assert "No text patch was retained" in " ".join(lines)
    assert app._change_diff_loading is None and store.diff_calls == []


def test_changes_show_tool_evidence_move_source_and_write_without_fabricated_diff():
    from opentab.tui.views.changes import diff_layout

    file = _data()["files"][0]
    file["edits"][0].update(source="apply_patch", status="moved", from_file="old.py")
    lines = diff_layout(file, 0, {"patch": "-old\n+new"}, 80)
    assert "Evidence: Completed apply_patch tool" in lines
    assert "Moved from: old.py" in lines
    file["edits"][0].update(source="write", available=False)
    lines = diff_layout(file, 0, None, 80)
    assert "The completed write records a path, but no before/after diff." in lines


def test_changes_render_overlapping_snapshot_and_tool_records_without_combined_counts():
    from opentab.tui.views.changes import diff_layout, files_layout

    data = _data()
    file = data["files"][0]
    file.update(additions=None, deletions=None, counts_overlap=True)
    patches = ["@@ -1 +1,2 @@\n-old\n+tool\n+script\n", "@@ -1 +1 @@\n-old\n+tool\n"]
    file["edits"][0].update(source="snapshot", additions=2, deletions=1)
    file["edits"][1].update(source="edit", additions=1, deletions=1)
    for width in (36, 100):
        layout = files_layout(data, 0, width)
        assert any("Records" in line for line in layout.lines)
        row = layout.lines[layout.cursor_line]
        assert "2" in row
        if width >= 70:
            assert row.count("?") == 2
        for index, edit in enumerate(file["edits"]):
            lines = diff_layout(file, index, {"patch": patches[index]}, width)
            text = " ".join(" ".join(lines).split())
            evidence = "Per-prompt snapshot" if index == 0 else "Completed edit tool"
            assert evidence in text
            assert "file totals are unknown" in text
            assert any(line.endswith("+script") and line.role == "add" for line in lines) == (
                index == 0
            )
            assert f"+{edit['additions']} -{edit['deletions']}" in "".join(lines)
            assert all(ot.display_width(line) <= width for line in lines)


def test_changes_real_store_renders_completed_patch_without_snapshot_summaries():
    import json
    import os
    import sqlite3
    import tempfile

    from tests._support import _write_opencode_db_with_tools

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "changes.db")
        _write_opencode_db_with_tools(db)
        with sqlite3.connect(db) as writer:
            writer.execute(
                "insert into message values ('prompt', 's1', ?)",
                [json.dumps({"role": "user", "summary": {"diffs": []}})],
            )
            writer.execute(
                "update message set data = json_set(data, '$.parentID', 'prompt') where id = 'm1'"
            )
            writer.execute(
                "insert into part values ('edit', 'm1', 's1', ?)",
                [
                    json.dumps(
                        {
                            "type": "tool",
                            "tool": "apply_patch",
                            "state": {
                                "status": "completed",
                                "metadata": {
                                    "files": [
                                        {
                                            "filePath": "/work/repo/.worktrees/feature/changed.py",
                                            "type": "update",
                                            "patch": "@@ -1 +1 @@\n-old\n+new",
                                            "additions": 1,
                                            "deletions": 1,
                                        }
                                    ]
                                },
                            },
                        }
                    )
                ],
            )
        store = ot.Store(db, type("Args", (), {"demo": False})())
        try:
            app = _app(store)
            app.renderer.detail_changes(app.current_session())
            _finish_read(app)
            assert len(app.changes_data()["files"]) == 1
            assert app.changes_data()["files"][0]["file"] == ".worktrees/feature/changed.py"
            app.open_change_file()
            app.renderer.detail_changes(app.current_session())
            _finish_read(app)
            lines = app.renderer.detail_changes(app.current_session())
            assert any(line.endswith("+new") for line in lines)
            assert any(line.endswith("-old") for line in lines)
            assert "Evidence: Completed apply_patch tool" in lines
        finally:
            store.conn.close()


def test_changes_diff_numbers_hunks_preserves_code_and_removes_redundant_headers():
    from opentab.tui.views.changes import diff_layout

    file = _data()["files"][0]
    patch_text = (
        "Index: /src/one.py\n=======\n--- a/src/one.py\n+++ b/src/one.py\n"
        "@@ -10,2 +20,3 @@ function\n-old\n+++value\n+next\n context\n"
    )
    lines = diff_layout(file, 0, {"patch": patch_text}, 60)
    assert not any(line.startswith(("Index:", "====", "--- a/", "+++ b/")) for line in lines)
    assert " 10      | -old" in lines
    assert "     20  | +++value" in lines
    assert "     21  | +next" in lines
    assert " 11  22  |  context" in lines
    assert next(line for line in lines if line.endswith("+++value")).role == "add"
    long = "+" + "identifier_" * 50
    lines = diff_layout(file, 0, {"patch": "@@ -0,0 +1 @@\n" + long}, 40)
    code = [line[line.gutter :] for line in lines if getattr(line, "role", "") == "add"]
    assert "".join(code) == long
    assert all(ot.display_width(line) <= 40 for line in lines)


def test_changes_tab_is_absent_for_claude_and_codex_including_combined_view():
    import os
    import tempfile

    from opentab.stores.combined import CombinedStore

    from tests._support import (
        _claude_msg,
        _codex_meta,
        _codex_tokens,
        _codex_turn,
        _usage,
        _write_jsonl,
    )

    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        claude_dir = os.path.join(tmp, "claude")
        os.makedirs(claude_dir)
        _write_jsonl(
            os.path.join(claude_dir, "s1.jsonl"),
            [_claude_msg("s1", "claude-opus-4-8", _usage(10, 5), uuid="a1", cwd=repo)],
        )
        codex_dir = os.path.join(tmp, "codex")
        os.makedirs(codex_dir)
        sid = "0199aa8e-1b9e-7912-bcd4-9b00c8733ea6"
        _write_jsonl(
            os.path.join(codex_dir, f"rollout-2025-10-03T16-51-03-{sid}.jsonl"),
            [
                _codex_meta(sid, repo),
                _codex_turn("gpt-5-codex", repo),
                _codex_tokens(10, 5, 0, 15),
            ],
        )
        args = type("Args", (), {"demo": False, "since": None, "until": None, "days": None})()
        claude = ot.ClaudeStore(claude_dir, args)
        codex = ot.CodexStore(codex_dir, args)
        for store, expected_sessions in (
            (claude, 1),
            (codex, 1),
            (CombinedStore([claude, codex]), 2),
        ):
            app = ot.App(store, args)
            app.set_browse_mode("projects")
            app.view = "session"
            assert len(app.current_sessions()) == expected_sessions
            for index in range(expected_sessions):
                app.workflow_index = index
                assert "Changes" not in app.current_tabs()
                assert "Turns" in app.current_tabs()
                assert not app.session_supports_changes(app.current_session().id)
            assert app._changes_worker is None


def test_changes_blocked_read_never_blocks_navigation_and_adopts_offscreen_result():
    started = threading.Event()
    release = threading.Event()

    class BlockingStore(ChangeStore):
        def change_request(self, workflow_id, key=None):
            request = super().change_request(workflow_id, key)

            def blocked(cancelled):
                started.set()
                release.wait()
                return request(cancelled)

            return blocked

    store = BlockingStore(
        [workflow("first", "2026-09-19 10:00:00"), workflow("second", "2026-09-19 11:00:00")]
    )
    app = _app(store)
    first_scope = app._change_scope()
    app.renderer.detail_changes(app.current_session())
    assert started.wait(2)
    app.handle_key(None, ord("h"))
    assert app.active_tab_name() != "Changes"
    app.workflow_index = 1 - app.workflow_index
    release.set()
    _finish_read(app)
    assert first_scope in app._changes_cache


def test_changes_same_tab_and_cross_session_revisits_never_reread_cached_results():
    rows = [workflow("first", "2026-09-19 10:00:00"), workflow("second", "2026-09-19 11:00:00")]
    store = ChangeStore(rows)
    app = _app(store)
    first = app.current_session()
    _load_files(app)
    app.handle_key(None, ord("h"))
    app.handle_key(None, ord("l"))
    app.renderer.detail_changes(app.current_session())
    assert store.file_calls == 1

    app.workflow_index = next(i for i, row in enumerate(app.current_sessions()) if row is not first)
    app.tab = app.current_tabs().index("Changes")
    _load_files(app)
    assert store.file_calls == 2
    app.workflow_index = next(i for i, row in enumerate(app.current_sessions()) if row is first)
    app.tab = app.current_tabs().index("Changes")
    app.renderer.detail_changes(app.current_session())
    assert store.file_calls == 2 and app.changes_data() is not None


def test_changes_late_patch_completion_does_not_replace_new_selection():
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    second_release = threading.Event()

    class BlockingDiffStore(ChangeStore):
        def change_request(self, workflow_id, key=None):
            request = super().change_request(workflow_id, key)

            def blocked(cancelled):
                if key == "one":
                    started.set()
                    release.wait()
                elif key == "two":
                    second_started.set()
                    second_release.wait()
                return request(cancelled)

            return blocked

    app = _app(BlockingDiffStore([workflow("root", "2026-09-19 10:00:00")]))
    _load_files(app)
    app.open_change_file()
    app.renderer.detail_changes(app.current_session())
    assert started.wait(2)
    app.step_change_edit(1)
    app.renderer.detail_changes(app.current_session())
    release.set()
    _finish_read(app)
    assert not app.change_diff_ready()
    assert second_started.wait(2)
    second_release.set()
    _finish_read(app)
    assert app.selected_change_edit()["key"] == "two"
    assert app.change_diff_ready()


def test_changes_reload_source_demo_and_exit_cancel_without_stale_adoption():
    for action in ("reload", "source", "demo", "exit"):
        started = threading.Event()
        cancelled_seen = threading.Event()

        class BlockingStore(ChangeStore):
            def __init__(self, workflows, started_event, cancelled_event):
                super().__init__(workflows)
                self.started_event = started_event
                self.cancelled_event = cancelled_event

            def change_request(self, workflow_id, key=None):
                def blocked(cancelled):
                    self.started_event.set()
                    cancelled.wait()
                    self.cancelled_event.set()
                    return self.data

                return blocked

        store = BlockingStore([workflow("root", "2026-09-19 10:00:00")], started, cancelled_seen)
        app = _app(store)
        app.renderer.detail_changes(app.current_session())
        assert started.wait(2)
        if action == "reload":
            app.reload()
        elif action == "source":
            app._reload_for_source()
        elif action == "demo":
            store.demo = True
            app._reload_for_source()
        else:
            app._invalidate_changes(close=True)
        assert cancelled_seen.wait(2), action
        app.poll_changes()
        assert app._changes_cache == {} and not app._change_pending, action


def test_changes_duplicate_qualified_identity_fails_closed_without_request():
    first = workflow("same", "2026-09-19 10:00:00")
    second = workflow("same", "2026-09-19 11:00:00")
    store = ChangeStore([first, second])
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(store, args)
    app.view = "session"
    assert app._change_scope() is None
    assert not app.session_supports_changes("same")
    assert store.file_calls == 0


def test_changes_split_aligns_replacements_insertions_and_numbered_context():
    from opentab.tui.views.changes import diff_layout

    patch_text = "@@ -10,4 +20,5 @@\n first\n-old = 1\n+new = 2\n+extra = 3\n last\n-tail\n+end\n@@ -80 +90 @@\n-one\n+two"
    lines = diff_layout(_data()["files"][0], 0, {"patch": patch_text}, 101, side_by_side=True)
    rows = [line for line in lines if getattr(line, "role", "") == "split"]
    assert " 10 |  first" in rows[0] and " 20 |  first" in rows[0]
    assert " 11 | -old = 1" in rows[1] and " 21 | +new = 2" in rows[1]
    assert rows[2][:49].isspace() and " 22 | +extra = 3" in rows[2]
    assert " 12 |  last" in rows[3] and " 23 |  last" in rows[3]
    assert " 80 | -one" in rows[-1] and " 90 | +two" in rows[-1]
    assert all(ot.display_width(line) <= 101 for line in lines)
    assert all(span.x + ot.display_width(span.text) <= 101 for line in rows for span in line.spans)
    for width in (30, 60, 79):
        narrow = diff_layout(
            _data()["files"][0], 0, {"patch": patch_text}, width, side_by_side=True
        )
        assert "Unified (narrow window)" in narrow
        assert not any(getattr(line, "role", "") == "split" for line in narrow)
        assert all(ot.display_width(line) <= width for line in narrow)


def test_changes_split_wraps_wide_text_and_preserves_indentation_and_alignment():
    from opentab.tui.views.changes import diff_layout

    before = "-    label = '" + "界e\u0301" * 40 + "'"
    after = "+    label = 'short'"
    lines = diff_layout(
        _data()["files"][0],
        0,
        {"patch": "@@ -1 +1 @@\n" + before + "\n" + after},
        100,
        side_by_side=True,
    )
    rows = [line for line in lines if getattr(line, "role", "") == "split"]
    assert len(rows) > 2
    # Recover painted code spans (excluding padding and the number gutters).
    left = "".join(
        span.text
        for line in rows
        for span in line.spans
        if 6 <= span.x < 48 and not span.text.isspace()
    )
    assert left.replace(" ", "") == before.replace(" ", "")
    assert "    label" in rows[0]
    assert all(ot.display_width(line) <= 100 for line in lines)
    assert all(span.x + ot.display_width(span.text) <= 100 for line in rows for span in line.spans)
    assert all("short" not in line for line in rows[1:])


def test_changes_syntax_and_word_emphasis_are_independent_of_diff_background():
    from opentab.tui.views.changes import diff_layout

    file = _data()["files"][0]
    patch_text = (
        '@@ -1 +1 @@\n-return greet("old", 42) # comment\n+return greet("new", 42) # comment'
    )
    for split in (False, True):
        lines = diff_layout(file, 0, {"patch": patch_text}, 120, side_by_side=split)
        spans = [span for line in lines for span in getattr(line, "spans", ())]
        assert any(s.foreground == "keyword" and s.text == "return" for s in spans)
        assert any(s.foreground == "function" and s.text == "greet" for s in spans)
        assert any(s.foreground == "number" and s.text == "42" for s in spans)
        assert any(s.foreground == "comment" and s.text == "# comment" for s in spans)
        assert any(
            s.foreground == "string" and s.background == "delete-emphasis" and s.text == "old"
            for s in spans
        )
        assert any(
            s.foreground == "string" and s.background == "add-emphasis" and s.text == "new"
            for s in spans
        )
    # Marker-looking source remains content; terminal controls never reach painting.
    lines = diff_layout(
        file, 0, {"patch": "@@ -0,0 +1 @@\n+++\x1b[31m\x00"}, 100, side_by_side=True
    )
    assert "+++" in "".join(lines)
    assert all(
        "\x1b" not in span.text and "\x00" not in span.text
        for line in lines
        for span in getattr(line, "spans", ())
    )


def test_changes_layout_toggle_preserves_occurrence_anchor_and_never_rereads():
    store = ChangeStore([workflow("root", "2026-09-19 10:00:00")])
    app = _app(store)
    app.handle_key(None, ord("v"))
    assert not app.change_side_by_side  # only active inside a file
    _load_files(app)
    app.open_change_file()
    _load_diff(app)
    unified = app.renderer.detail_changes(app.current_session(), 120)
    app.scroll = next(i for i, line in enumerate(unified) if line.role == "delete")
    app.handle_key(None, ord("v"))
    assert app.change_side_by_side
    split = app.renderer.detail_changes(app.current_session(), 120)
    assert split[app.scroll].role == "split" and "-old" in split[app.scroll]
    assert split is app.renderer.detail_changes(app.current_session(), 120)
    assert app.selected_change_edit()["key"] == "one"
    app.handle_key(None, ord("v"))
    unified = app.renderer.detail_changes(app.current_session(), 120)
    assert unified[app.scroll].role == "delete"
    assert store.diff_calls == [("root", "one")]
    assert ot.keymap.BY_ID["diff-layout"].shown(app)
    app.can_switch_source = lambda: False
    footer = str(ot.keymap.footer_parts(app))
    assert "side-by-side" in footer and "v" in footer
    app.scroll = 0
    app.handle_key(None, ord("v"))
    app.renderer.detail_changes(app.current_session(), 120)
    assert app.scroll == 0

    from opentab.tui import bindings

    app.keymap = bindings.Keymap({("main", "diff_layout"): ["V"]})
    app.handle_key(None, ord("v"))
    assert app.change_side_by_side
    app.handle_key(None, ord("V"))
    assert not app.change_side_by_side
    assert ot.keymap.BY_ID["diff-layout"].label(app) == "V"


def test_changes_paints_independent_halves_and_theme_pairs_with_safe_fallback():
    from opentab.tui.views.changes import DIFF_BACKGROUNDS, DIFF_FOREGROUNDS, diff_layout

    app = _app(ChangeStore([workflow("root", "2026-09-19 10:00:00")]))
    renderer = app.renderer
    lines = diff_layout(
        _data()["files"][0],
        0,
        {"patch": '@@ -1 +1 @@\n-x = "old"\n+x = "new"'},
        100,
        side_by_side=True,
    )
    row = next(line for line in lines if getattr(line, "role", "") == "split")
    renderer._diff_pairs = {
        (bg, fg): 40 + i * 8 + j
        for i, bg in enumerate(DIFF_BACKGROUNDS)
        for j, fg in enumerate(DIFF_FOREGROUNDS)
    }
    screen = AttrScreen(4, 104)
    with patch.object(ot.curses, "color_pair", side_effect=lambda n: n):
        renderer.paint_change_line(screen, 0, 0, row, 100)
        assert screen.attrs[0, 25] == renderer._diff_pairs["delete", "ink"]
        assert screen.attrs[0, 95] == renderer._diff_pairs["add", "ink"]
        assert screen.attrs[0, 49] == renderer._diff_pairs["code", "gutter"]
        renderer._diff_pairs = {}
        renderer.paint_change_line(screen, 1, 0, row, 100)
    assert '-x = "old"' in screen_text(screen) and '+x = "new"' in screen_text(screen)
