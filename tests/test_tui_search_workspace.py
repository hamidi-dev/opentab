from types import SimpleNamespace

import opentab as ot
from opentab.models import SessionRef
from opentab.tui import bindings
from opentab.tui.search_workspace import SearchWorkspace

from tests._support import app_with, workflow


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeWorker:
    def __init__(self, _args, source_key):
        self.source_key = source_key
        self.submitted = []
        self.results = []
        self.next_id = 1
        self.discards = 0
        self.closed = False

    def submit(self, operation, **params):
        request_id = self.next_id
        self.next_id += 1
        self.submitted.append((request_id, operation, params))
        return request_id

    def poll(self):
        results, self.results = self.results, []
        return results

    def discard_pending(self):
        self.discards += 1

    def close(self):
        self.closed = True


def workspace(**kwargs):
    clock = kwargs.pop("clock", Clock())
    made = []

    def factory(args, source_key):
        worker = FakeWorker(args, source_key)
        made.append(worker)
        return worker

    ws = SearchWorkspace(SimpleNamespace(), "all", worker_factory=factory, clock=clock, **kwargs)
    return ws, clock, made


def start(ws, made, status=None):
    ws.poll()
    worker = made[0]
    assert worker.submitted == [(1, "status", {})]
    worker.results.append((1, "status", status or {"exists": True}, None))
    ws.poll()
    assert ws.consent == ""
    return worker


def search_result(hit=None):
    return {
        "hits": [
            hit
            or {
                "session_key": "qualified-one",
                "execution_id": "exec-one",
                "anchor": "record-one",
                "title": "First",
                "project": "/work/one",
            }
        ],
        "match_mode": "all_terms",
    }


def test_direct_entry_has_no_read_prompt_and_does_not_build_an_index():
    ws, _clock, made = workspace()
    assert ws.consent == "" and made == []
    ws.handle_key(ord("x"), bindings.DEFAULT)
    worker = start(ws, made)
    assert ws.query == "x" and ws.active
    assert [operation for _id, operation, _params in worker.submitted] == ["status"]


def test_capital_jk_scroll_preview_without_focus_or_selection_changes():
    ws, _clock, _made = workspace()
    ws.editing = False
    ws.query = "needle"
    ws.hits = search_result()["hits"]
    ws.preview = {"records": [{"id": "match"}]}
    for focus in ("results", "preview"):
        ws.focus = focus
        ws.handle_key(ord("J"), bindings.DEFAULT)
        assert ws.preview_scroll == 1 and ws.focus == focus and ws.selected == 0
        ws.handle_key(ord("K"), bindings.DEFAULT)
        ws.handle_key(ord("K"), bindings.DEFAULT)
        assert ws.preview_scroll == 0 and ws.query == "needle"
    ws.editing = True
    ws.handle_key(ord("J"), bindings.DEFAULT)
    assert ws.query == "needleJ"  # query typing must still accept uppercase text


def test_preview_scrolling_and_help_obey_remapped_bindings():
    ws, _clock, _made = workspace()
    ws.editing = False
    keymap = bindings.Keymap(
        {("search", "preview_down"): ["X"], ("help", "close"): ["z"], ("help", "down"): ["v"]}
    )
    ws.handle_key(ord("X"), keymap)
    assert ws.preview_scroll == 1 and ws.focus == "results"
    ws.handle_key(ord("?"), keymap)
    ws.handle_key(ord("v"), keymap)
    assert ws.help and ws.help_scroll == 1 and ws.preview_scroll == 1
    ws.handle_key(ord("z"), keymap)
    assert not ws.help


def test_enter_from_input_only_focuses_results_even_after_async_completion():
    ws, clock, made = workspace()
    worker = start(ws, made)
    ws.handle_key(ord("x"), bindings.DEFAULT)
    ws.handle_key(10, bindings.DEFAULT)
    assert not ws.editing and ws.focus == "results" and not ws.reader
    ws.poll()  # Enter flushes the debounce, but never queues a reader.
    search_id = worker.submitted[-1][0]
    worker.results.append((search_id, "search", search_result(), None))
    ws.poll()
    preview_id = worker.submitted[-1][0]
    worker.results.append((preview_id, "conversation", {"records": []}, None))
    ws.poll()
    assert not ws.reader
    assert ws.query == "x" and clock.now == 0


def test_ctrl_f_refocuses_input_from_reader_help_scope_and_pending_open():
    ws, _clock, made = workspace()
    worker = start(ws, made)
    ws.query = "needle"
    ws.hits = search_result()["hits"]
    ws.editing = False
    ws.handle_key(10, bindings.DEFAULT)
    preview_id = worker.submitted[-1][0]
    ws.handle_key(6, bindings.DEFAULT)
    worker.results.append((preview_id, "conversation", {"records": []}, None))
    ws.poll()
    assert ws.editing and not ws.reader and ws.query == "needle"
    for mode in ("reader", "help", "scope"):
        ws.editing = False
        ws.focus = "preview"
        ws.reader = mode == "reader"
        ws.help = mode == "help"
        ws.filter_field = "project" if mode == "scope" else ""
        ws.handle_key(6, bindings.DEFAULT)
        assert ws.editing and ws.focus == "results"
        assert not ws.reader and not ws.help and not ws.filter_field
        assert ws.query == "needle"
    ws.handle_key(6, bindings.DEFAULT)
    assert ws.query == "needle"  # refocusing the input is idempotent


def test_reader_uses_normal_scrolling_not_preview_shortcuts():
    ws, _clock, _made = workspace()
    ws.editing = False
    ws.reader = True
    ws.handle_key(ord("J"), bindings.DEFAULT)
    assert ws.preview_scroll == 0
    ws.handle_key(ord("j"), bindings.DEFAULT)
    assert ws.preview_scroll == 1
    ws.handle_key(ord("K"), bindings.DEFAULT)
    assert ws.preview_scroll == 1


def test_query_refocus_obeys_remapping_and_does_not_confirm_index():
    ws, _clock, _made = workspace()
    keys = bindings.Keymap({("search", "edit"): ["X"], ("search.edit", "edit"): ["ctrl-e"]})
    ws.editing = False
    ws.handle_key(ord("X"), keys)
    assert ws.editing
    ws.filter_field = "project"
    ws.handle_key(5, keys)
    assert not ws.filter_field
    ws.consent = "index"
    ws.handle_key(5, keys)
    assert ws.consent == "index" and ws._worker is None


def test_missing_index_offer_can_be_cancelled_once_without_writes():
    ws, _clock, made = workspace()
    ws.poll()
    worker = made[0]
    worker.results.append((1, "status", {"exists": False}, None))
    ws.poll()
    assert ws.consent == "index" and "Build" in ws.notice

    assert ws.handle_key(27, bindings.DEFAULT)
    assert ws.active and ws.consent == ""
    assert "nothing was written" in ws.notice
    ws.poll()
    assert ws.consent == ""
    assert [operation for _id, operation, _params in worker.submitted] == ["status"]


def test_missing_index_build_is_explicit_and_searches_after_completion():
    ws, clock, made = workspace()
    ws.handle_key(ord("x"), bindings.DEFAULT)
    ws.poll()
    worker = made[0]
    worker.results.append((1, "status", {"exists": False}, None))
    ws.poll()
    assert ws.consent == "index"

    ws.handle_key(10, bindings.DEFAULT)
    assert worker.submitted[-1] == (2, "index", {})
    report = {"complete": True, "updated": 1, "index": {"exists": True, "roots": 1}}
    worker.results.append((2, "index", report, None))
    ws.poll()
    assert [operation for _id, operation, _params in worker.submitted] == ["status", "index"]
    clock.now += ws.DEBOUNCE_SECONDS
    ws.poll()
    assert worker.submitted[-1] == (
        3,
        "search",
        {"query": "x", "limit": 100, "max_chars": ws.SEARCH_MAX_CHARS},
    )


def test_typing_before_status_reply_keeps_the_startup_check():
    ws, clock, made = workspace()
    ws.handle_key(ord("x"), bindings.DEFAULT)
    clock.now = 1
    ws.poll()
    worker = made[0]
    assert worker.submitted == [(1, "status", {})]

    worker.results.append((1, "status", {"exists": True, "roots": 4}, None))
    ws.poll()
    assert ws.status == {"exists": True, "roots": 4}
    ws.poll()
    assert worker.submitted[-1][1:] == (
        "search",
        {"query": "x", "limit": 100, "max_chars": ws.SEARCH_MAX_CHARS},
    )


def test_enter_or_tabs_before_a_hit_do_not_queue_a_future_reader():
    ws, clock, made = workspace()
    worker = start(ws, made)
    ws.handle_key(ord("x"), bindings.DEFAULT)
    clock.now = 1
    ws.poll()
    search_id = worker.submitted[-1][0]
    ws.handle_key(10, bindings.DEFAULT)
    assert not ws.editing and ws.focus == "results"
    ws.handle_key(10, bindings.DEFAULT)
    ws.handle_key(ord("h"), bindings.DEFAULT)
    ws.handle_key(ord("l"), bindings.DEFAULT)
    assert not ws.reader and not ws.conversation_available
    assert not any(operation == "conversation" for _id, operation, _params in worker.submitted)

    worker.results.append((search_id, "search", search_result(), None))
    ws.poll()
    preview_id = worker.submitted[-1][0]
    page = {"records": [{"id": "first"}], "next_cursor": "next"}
    worker.results.append((preview_id, "conversation", page, None))
    ws.poll()
    assert not ws.reader and ws.conversation_available
    ws.handle_key(ord("l"), bindings.DEFAULT)
    assert ws.reader
    ws.handle_key(ord("]"), bindings.DEFAULT)
    later_id = worker.submitted[-1][0]
    ws.handle_key(27, bindings.DEFAULT)
    assert not ws.reader and ws.preview == page
    worker.results.append((later_id, "conversation", {"records": [{"id": "late"}]}, None))
    ws.poll()
    assert not ws.reader and ws.preview == page


def test_switch_view_restores_results_and_reader_page_scroll_and_history():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.hits = search_result()["hits"]
    ws.selected = 0
    ws.result_scroll = 6
    result_preview = {"records": [{"id": "match"}], "next_cursor": "next"}
    ws.preview = result_preview
    ws.preview_scroll = 3
    ws.preview_anchor = "record-one"

    ws.switch_view("conversation")
    reader_page = {"records": [{"id": "later"}], "next_cursor": None}
    reader_history: list[tuple[dict, int, str | None]] = [(result_preview, 3, "record-one")]
    ws.preview = reader_page
    ws.preview_scroll = 11
    ws.preview_anchor = "later"
    ws._reader_history = reader_history
    ws.switch_view("results")

    assert not ws.reader and ws.selected == 0 and ws.result_scroll == 6
    assert ws.preview == result_preview and ws.preview_scroll == 3
    ws.switch_view("conversation")
    assert ws.reader and ws.preview == reader_page and ws.preview_scroll == 11
    assert ws.preview_anchor == "later" and ws._reader_history == reader_history


def test_switching_away_from_initial_load_retags_it_as_results_preview():
    ws, _clock, made = workspace()
    worker = start(ws, made)
    ws.hits = search_result()["hits"]
    ws.editing = True
    ws.focus = "preview"
    ws._request_preview(ws.selected_hit)
    request_id = worker.submitted[-1][0]

    ws.switch_view("conversation")
    assert ws.reader and not ws.editing
    assert worker.submitted[-1][0] == request_id
    ws.switch_view("results")
    assert not ws.reader and not ws.editing and ws.focus == "preview"
    assert ws._reader_saved is None
    assert ws._pending["conversation"][2]["reader"] is False

    loaded = {"records": [{"id": "record-one"}], "has_earlier": False}
    worker.results.append((request_id, "conversation", loaded, None))
    ws.poll()
    assert ws.preview == loaded and not ws.reader
    ws.switch_view("conversation")
    assert ws.reader and ws.preview == loaded
    assert worker.submitted[-1][0] == request_id


def test_cancelled_next_page_does_not_add_duplicate_reader_history():
    ws, _clock, made = workspace()
    worker = start(ws, made)
    ws.editing = False
    ws.focus = "preview"
    ws.hits = search_result()["hits"]
    result_preview = {"records": [{"id": "match"}]}
    older = ({"records": [{"id": "older"}]}, 2, "older")
    current = {
        "records": [{"id": "current"}],
        "has_earlier": True,
        "next_cursor": "next-page",
    }
    ws.preview = result_preview
    ws.switch_view("conversation")
    ws.preview = current
    ws.preview_scroll = 7
    ws.preview_anchor = "current"
    ws._reader_history = [older]

    ws._reader_next()
    next_id = worker.submitted[-1][0]
    assert ws._reader_history == [older]
    ws.switch_view("results")
    worker.results.append((next_id, "conversation", {"records": [{"id": "too-late"}]}, None))
    ws.poll()
    ws.switch_view("conversation")
    assert ws.preview == current and ws._reader_history == [older]
    ws._reader_previous()
    assert ws.preview == older[0] and ws.preview_scroll == 2


def test_reader_location_is_invalidated_by_selection_query_and_filter_changes():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.query = "needle"
    ws.hits = [
        search_result()["hits"][0],
        {
            "session_key": "qualified-two",
            "execution_id": "exec-two",
            "anchor": "record-two",
        },
    ]
    ws.preview = {"records": [{"id": "match"}]}
    ws.switch_view("conversation")
    ws.preview_scroll = 9
    ws.switch_view("results")
    assert ws._reader_saved is not None

    ws.select(1)
    assert ws._reader_saved is None
    ws.preview = {"records": [{"id": "second"}]}
    ws.switch_view("conversation")
    ws.switch_view("results")
    ws.query = "changed"
    ws._schedule_search()
    assert ws._reader_saved is None

    ws.scope = {"project": "/old"}
    ws.hits = search_result()["hits"]
    ws.preview = {"records": []}
    ws.switch_view("conversation")
    ws.switch_view("results")
    ws.open_filter("harness")
    ws.choose_filter(1)
    assert ws.scope == {"project": "/old", "harness": "opencode"}
    assert ws._reader_saved is None


def test_search_is_debounced_and_obsolete_results_are_ignored():
    ws, clock, made = workspace()
    worker = start(ws, made)
    assert worker.submitted == [(1, "status", {})]

    for char in "old":
        ws.handle_key(ord(char), bindings.DEFAULT)
    clock.now = 0.19
    ws.poll()
    assert [op for _id, op, _params in worker.submitted] == ["status"]
    clock.now = 0.21
    ws.poll()
    old_id = worker.submitted[-1][0]
    assert worker.submitted[-1][1:] == (
        "search",
        {"query": "old", "limit": 100, "max_chars": ws.SEARCH_MAX_CHARS},
    )

    ws.handle_key(ord("x"), bindings.DEFAULT)
    worker.results.append((old_id, "search", search_result(), None))
    ws.poll()
    assert ws.hits == []
    clock.now += 0.21
    ws.poll()
    assert worker.submitted[-1][2]["query"] == "oldx"


def test_result_preview_uses_exact_qualified_hit_and_opens_reader():
    ws, clock, made = workspace()
    worker = start(ws, made)
    ws.handle_key(ord("x"), bindings.DEFAULT)
    clock.now = 1
    ws.poll()
    search_id = worker.submitted[-1][0]
    worker.results.append((search_id, "search", search_result(), None))
    ws.poll()
    preview_id, operation, params = worker.submitted[-1]
    assert operation == "conversation"
    assert params == {
        "session": "qualified-one",
        "execution_id": "exec-one",
        "limit": 12,
        "max_chars": ws.READER_MAX_CHARS,
        "anchor": "record-one",
        "before": 0,
    }
    worker.results.append(
        (
            preview_id,
            "conversation",
            {"records": [], "has_earlier": False, "next_cursor": None},
            None,
        )
    )
    ws.poll()
    ws.handle_key(10, bindings.DEFAULT)
    assert not ws.editing and ws.focus == "results" and not ws.reader
    ws.handle_key(10, bindings.DEFAULT)
    assert ws.reader and ws.preview_anchor == "record-one"
    ws.handle_key(27, bindings.DEFAULT)
    assert ws.active and not ws.reader and ws.selected_hit["session_key"] == "qualified-one"


def test_session_scope_back_restores_exact_result_and_preview_state():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.query = "needle"
    ws.hits = search_result()["hits"]
    ws.selected = 0
    ws.result_scroll = 7
    ws.preview = {"records": [{"id": "record-one"}]}
    ws.preview_scroll = 19
    ws.handle_key(ord("s"), bindings.DEFAULT)
    assert ws.scope == {"session": "qualified-one"} and ws.hits == []
    ws.handle_key(27, bindings.DEFAULT)
    assert ws.query == "needle"
    assert ws.scope == {}
    assert ws.selected == 0 and ws.result_scroll == 7
    assert ws.preview == {"records": [{"id": "record-one"}]}
    assert ws.preview_scroll == 19


def test_index_has_separate_consent_and_omits_date_bounds():
    ws, _clock, made = workspace()
    worker = start(ws, made)
    ws.editing = False
    ws.scope = {
        "project": "/work/one",
        "harness": "opencode",
        "session": "qualified-one",
        "since": "2026-09-01",
        "until": "2026-09-02",
    }
    ws.handle_key(ord("I"), bindings.DEFAULT)
    assert ws.consent == "index" and "plaintext" in ws.notice and "Date" in ws.notice
    ws.handle_key(10, bindings.DEFAULT)
    assert worker.submitted[-1] == (
        2,
        "index",
        {
            "project": "/work/one",
            "harness": "opencode",
            "session": "qualified-one",
        },
    )


def test_reader_continuation_uses_cursor_and_stale_errors_remain_visible():
    ws, _clock, made = workspace()
    worker = start(ws, made)
    ws.editing = False
    ws.hits = search_result()["hits"]
    ws.preview = {
        "records": [{"id": "huge", "record_complete": False}],
        "has_earlier": False,
        "next_cursor": "inside-huge-message",
    }
    ws.reader = True
    ws.handle_key(ord("]"), bindings.DEFAULT)
    request_id, operation, params = worker.submitted[-1]
    assert operation == "conversation" and params["cursor"] == "inside-huge-message"
    assert "anchor" not in params
    worker.results.append(
        (
            request_id,
            operation,
            None,
            {"code": "stale_cursor", "message": "source changed"},
        )
    )
    ws.poll()
    assert ws.error == "stale_cursor: source changed"


def test_app_binding_demo_filter_resize_and_mouse_routing():
    app = app_with([workflow("native", "2026-09-01", title="Session")])
    app.keymap = bindings.Keymap({("main", "conversation_search"): ["X"]})
    assert app.handle_key(None, ord("X"))
    assert app.conversation_search is not None
    app.conversation_search.handle_key(27, app.keymap)
    assert app.conversation_search.active and not app.conversation_search.editing
    app._close_conversation_search()

    app.browse_mode = "projects"
    app.handle_key(None, ord("/"))
    assert app.filter_active and app.conversation_search is None
    app.handle_key(None, 27)

    app.open_conversation_search()
    assert app.handle_key(None, ot.curses.KEY_RESIZE)
    assert app.conversation_search is not None and app.conversation_search.active
    assert app.handle_key(None, ot.curses.KEY_MOUSE)
    app._close_conversation_search()

    app.store.demo = True
    app.open_conversation_search()
    assert app.conversation_search is None


def test_app_session_scope_uses_exact_normalized_session_ref():
    row = workflow("native", "2026-09-01", title="Session")
    row.source = "Claude Code"
    app = app_with([row])
    app.view = "session"
    app.open_conversation_search()
    ref = SessionRef.decode(app.conversation_search.scope["session"])
    assert ref.native_id == "native"
    assert ref.harness == "claude"
    assert ref.machine == app.local_machine_name
    app._close_conversation_search()


def test_ctrl_c_closes_search_and_exits_the_app_loop():
    app = app_with([workflow("native", "2026-09-01")])
    app.open_conversation_search()
    assert app.handle_key(None, 3) is False
    assert app.conversation_search is None


def test_select_clears_the_old_preview_before_new_content_arrives():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.hits = [
        search_result()["hits"][0],
        {
            "session_key": "qualified-two",
            "execution_id": "exec-two",
            "anchor": "record-two",
            "title": "Second",
        },
    ]
    ws.preview = {"records": [{"id": "old"}]}
    ws.preview_scroll = 8
    ws.preview_anchor = "old"
    ws.select(1)
    assert ws.preview is None and ws.preview_scroll == 0 and ws.preview_anchor is None
    assert made[0].submitted[-1][2]["session"] == "qualified-two"


def test_reader_escape_restores_result_preview_and_scroll_after_paging():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.hits = search_result()["hits"]
    original = {
        "records": [{"id": "record-one"}],
        "has_earlier": True,
        "next_cursor": "next-page",
    }
    ws.preview = original
    ws.preview_scroll = 9
    ws.preview_anchor = "record-one"
    ws.handle_key(10, bindings.DEFAULT)
    ws.handle_key(ord("]"), bindings.DEFAULT)
    request_id = made[0].submitted[-1][0]
    assert ws._reader_history == []
    later = {"records": [{"id": "later"}], "has_earlier": True, "next_cursor": None}
    made[0].results.append((request_id, "conversation", later, None))
    ws.poll()
    assert ws._reader_history == [(original, 9, "record-one")]
    ws.preview_scroll = 4
    ws.handle_key(27, bindings.DEFAULT)
    assert not ws.reader
    assert ws.preview == original and ws.preview_scroll == 9
    assert ws.preview_anchor == "record-one"


def test_pending_preview_reader_uses_loaded_match_as_its_return_page():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.hits = search_result()["hits"]
    ws.handle_key(10, bindings.DEFAULT)
    request_id = made[0].submitted[-1][0]
    loaded = {"records": [{"id": "record-one"}], "has_earlier": False}
    made[0].results.append((request_id, "conversation", loaded, None))
    ws.poll()
    assert ws.reader
    ws.preview = {"records": [{"id": "later"}]}
    ws.preview_scroll = 6
    ws.handle_key(27, bindings.DEFAULT)
    assert ws.preview == loaded and ws.preview_scroll == 0


def test_scope_label_names_global_catalog_and_composes_every_filter():
    ws, _clock, _made = workspace(
        selected_session_key="qualified-one", selected_session_title="First"
    )
    assert ws.scope_label == "session First"
    ws.scope.update(
        {
            "project": "/work/one",
            "harness": "opencode",
            "since": "2026-09-01",
            "until": "2026-09-02",
        }
    )
    assert ws.scope_label == (
        "session First / project /work/one / harness opencode / " "messages 2026-09-01..2026-09-02"
    )
    ws.scope = {}
    assert ws.scope_label == "global catalog"


def test_filter_options_cover_scope_and_harness_with_disabled_session():
    ws, _clock, _made = workspace()
    ws.open_filter("scope")
    assert ws.filter_options() == [
        ("all", "All sessions", True),
        ("session", "This session", False),
    ]

    scoped, _clock, _made = workspace(
        selected_session_key="qualified-one", selected_session_title="First"
    )
    scoped.open_filter("scope")
    assert scoped.filter_menu_index == 1
    assert scoped.filter_options()[1] == ("session", "This session", True)
    scoped.choose_filter(1)
    assert scoped.scope == {"session": "qualified-one"}

    ws.open_filter("harness")
    assert ws.filter_options() == [
        ("all", "All harnesses", True),
        ("opencode", "OpenCode", True),
        ("claude", "Claude Code", True),
        ("codex", "Codex", True),
    ]


def test_filter_picker_is_modal_and_uses_menu_navigation_without_indexing():
    ws, _clock, made = workspace()
    worker = start(ws, made)
    ws.editing = False
    ws.query = "needle"
    ws.open_filter("scope")
    ws.handle_key(ord("x"), bindings.DEFAULT)
    assert ws.query == "needle" and ws.filter_menu == "scope"
    ws.handle_key(ord("/"), bindings.DEFAULT)
    assert ws.editing and ws.filter_menu == ""
    ws.editing = False
    ws.open_filter("scope")
    ws.handle_key(ord("j"), bindings.DEFAULT)
    assert ws.filter_menu_index == 1
    ws.handle_key(10, bindings.DEFAULT)
    assert ws.filter_menu == "scope"  # Disabled rows do not apply or dismiss.
    ws.handle_key(ord("G"), bindings.DEFAULT)
    ws.handle_key(ord("g"), bindings.DEFAULT)
    assert ws.filter_menu_index == 0
    ws.handle_key(27, bindings.DEFAULT)
    assert ws.filter_menu == "" and ws.active
    assert not any(operation == "index" for _id, operation, _params in worker.submitted)

    ws.open_filter("harness")
    ws.handle_key(ord("k"), bindings.DEFAULT)
    assert ws.filter_menu_index == 3
    ws.handle_key(10, bindings.DEFAULT)
    assert ws.scope == {"harness": "codex"} and ws.filter_menu == ""
    assert not any(operation == "index" for _id, operation, _params in worker.submitted)


def test_filter_actions_obey_remaps_and_ctrl_f_dismisses_a_picker():
    ws, _clock, _made = workspace()
    ws.editing = False
    ws.hits = search_result()["hits"]
    keys = bindings.Keymap(
        {
            ("search", "scope_menu"): ["X"],
            ("search", "scope_harness"): ["Y"],
            ("search", "reset_filters"): ["Z"],
            ("search", "edit"): ["ctrl-e"],
            ("search.edit", "edit"): ["ctrl-e"],
        }
    )
    ws.handle_key(ord("X"), keys)
    assert ws.filter_menu == "scope"
    ws.handle_key(5, keys)
    assert ws.editing and ws.filter_menu == ""

    ws.editing = False
    ws.handle_key(ord("Y"), keys)
    assert ws.filter_menu == "harness"
    ws.choose_filter(1)
    ws.scope.update({"project": "/work", "since": "2026-09-01"})
    ws.handle_key(ord("Z"), keys)
    assert ws.scope == {} and ws.query == ""


def test_picker_menu_actions_win_over_remapped_query_edit():
    ws, _clock, _made = workspace()
    ws.editing = False
    ws.open_filter("harness")
    keys = bindings.Keymap(
        {
            ("menu", "down"): ["X"],
            ("search", "edit"): ["X", "Y"],
            ("search.edit", "edit"): ["X", "Y"],
        }
    )
    ws.handle_key(ord("X"), keys)
    assert ws.filter_menu == "harness" and ws.filter_menu_index == 1
    ws.handle_key(ord("Y"), keys)
    assert ws.filter_menu == "" and ws.editing


def test_session_all_and_reset_filters_are_orthogonal_and_preserve_query():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.query = "needle"
    ws.scope = {
        "project": "/work/one",
        "harness": "claude",
        "since": "2026-09-01",
        "until": "2026-09-02",
    }
    ws.hits = search_result()["hits"]
    ws.handle_key(ord("s"), bindings.DEFAULT)
    assert ws.scope == {
        "session": "qualified-one",
        "project": "/work/one",
        "harness": "claude",
        "since": "2026-09-01",
        "until": "2026-09-02",
    }
    assert len(ws._scope_stack) == 1

    ws.hits = search_result()["hits"]
    ws.handle_key(ord("s"), bindings.DEFAULT)
    assert len(ws._scope_stack) == 1
    ws.handle_key(ord("a"), bindings.DEFAULT)
    assert ws.scope == {
        "project": "/work/one",
        "harness": "claude",
        "since": "2026-09-01",
        "until": "2026-09-02",
    }
    assert ws.query == "needle"

    ws.open_filter("reset")
    assert ws.scope == {} and ws.query == "needle" and ws._scope_stack == []


def test_project_and_date_filters_use_existing_text_inputs():
    ws, _clock, made = workspace(project="/suggested")
    start(ws, made)
    ws.editing = False
    ws.open_filter("project")
    assert ws.filter_field == "project" and ws.filter_text == "/suggested"
    ws.filter_text = "/chosen"
    ws.handle_key(10, bindings.DEFAULT)
    assert ws.scope == {"project": "/chosen"}

    ws.open_filter("date")
    ws.filter_text = "2026-09-01..2026-09-02"
    ws.handle_key(10, bindings.DEFAULT)
    assert ws.scope == {
        "project": "/chosen",
        "since": "2026-09-01",
        "until": "2026-09-02",
    }


def test_query_or_scope_change_exits_reader_and_drops_reader_state():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.reader = True
    ws._reader_return = ({"records": []}, 2, "return")
    ws._reader_history = [({"records": []}, 1, None)]
    ws.query = "new"
    ws._schedule_search()
    assert not ws.reader and ws._reader_return is None and ws._reader_history == []


def test_index_report_survives_search_reset_and_duplicate_submission_is_blocked():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.handle_key(ord("I"), bindings.DEFAULT)
    ws.handle_key(10, bindings.DEFAULT)
    index_id = made[0].submitted[-1][0]
    ws.handle_key(ord("I"), bindings.DEFAULT)
    assert ws.consent == "" and "already running" in ws.notice
    assert len([job for job in made[0].submitted if job[1] == "index"]) == 1

    ws.query = "needle"
    ws._schedule_search()
    assert ws.busy == "index" and ws._pending["index"][0] == index_id
    report = {
        "complete": False,
        "updated": 2,
        "unchanged": 3,
        "unsupported": 1,
        "errors": [{"code": "broken"}],
        "index": {"roots": 9},
    }
    made[0].results.append((index_id, "index", report, None))
    ws.poll()
    assert ws.index_report == report and ws.status == {"roots": 9}
    assert "partial" in ws.notice and "2 updated" in ws.notice and "1 error" in ws.notice
    assert ws.response == {}


def test_scope_back_does_not_forget_a_confirmed_index_job():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.hits = search_result()["hits"]
    ws.handle_key(ord("I"), bindings.DEFAULT)
    ws.handle_key(10, bindings.DEFAULT)
    index_id = made[0].submitted[-1][0]
    ws.handle_key(ord("s"), bindings.DEFAULT)
    ws.handle_key(27, bindings.DEFAULT)
    assert ws._pending["index"][0] == index_id and ws.busy == "index"


def test_index_worker_error_becomes_a_partial_report():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.handle_key(ord("I"), bindings.DEFAULT)
    ws.handle_key(10, bindings.DEFAULT)
    index_id = made[0].submitted[-1][0]
    error = {"code": "operation_failed", "message": "refresh failed"}
    made[0].results.append((index_id, "index", None, error))
    ws.poll()
    assert ws.index_report["complete"] is False
    assert ws.index_report["errors"] == [error]
    assert "partial" in ws.notice and "1 error" in ws.notice


def test_tab_and_escape_leave_query_editing_without_opening_or_closing():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.handle_key(9, bindings.DEFAULT)
    assert ws.active and not ws.editing and ws.focus == "results" and not ws.reader
    ws.handle_key(ord("/"), bindings.DEFAULT)
    assert ws.editing
    ws.handle_key(27, bindings.DEFAULT)
    assert ws.active and not ws.editing and ws.focus == "results"

    ws.editing = True
    ws.handle_key(ot.curses.KEY_BTAB, bindings.DEFAULT)
    assert not ws.editing and ws.focus == "preview"


def test_earlier_window_anchors_renderer_to_first_returned_record():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.editing = False
    ws.hits = search_result()["hits"]
    ws.preview = {
        "records": [{"id": "current"}],
        "has_earlier": True,
        "next_cursor": None,
    }
    ws._open_reader()
    ws.handle_key(ord("["), bindings.DEFAULT)
    request_id, operation, params = made[0].submitted[-1]
    assert operation == "conversation" and params["anchor"] == "current"
    assert params["before"] == ws.READER_LIMIT
    earlier = {
        "records": [{"id": "first-loaded", "parts": []}],
        "has_earlier": True,
        "has_more": True,
    }
    made[0].results.append((request_id, operation, earlier, None))
    ws.poll()
    assert ws.preview_anchor == "first-loaded"


def test_query_scope_caps_and_strict_dates():
    ws, _clock, made = workspace()
    start(ws, made)
    ws.query = "x" * ws.QUERY_MAX_CHARS
    ws.handle_key(ord("y"), bindings.DEFAULT)
    assert len(ws.query) == ws.QUERY_MAX_CHARS and "limited" in ws.notice
    assert ws._valid_date("2026-09-01")
    assert not ws._valid_date("20260901")
    assert not ws._valid_date("2026-02-30")

    ws.filter_field = "project"
    ws.filter_text = "x" * ws.FILTER_MAX_CHARS
    ws.handle_key(ord("y"), bindings.DEFAULT)
    assert len(ws.filter_text) == ws.FILTER_MAX_CHARS and "limited" in ws.notice
