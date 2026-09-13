"""Interaction state for the full-screen conversation-search workspace."""

from __future__ import annotations

import copy
import re
import time
from datetime import date
from typing import Callable

from opentab.tui import bindings


class SearchWorkspace:
    """Own search and reader state without depending on App or the renderer."""

    DEBOUNCE_SECONDS = 0.2
    READER_LIMIT = 12
    READER_MAX_CHARS = 40_000
    SEARCH_MAX_CHARS = 24_000
    QUERY_MAX_CHARS = 1000
    FILTER_MAX_CHARS = 8192
    READER_HISTORY_MAX = 20

    def __init__(
        self,
        args,
        source_key: str,
        selected_session_key: str | None = None,
        selected_session_title: str = "",
        project: str | None = None,
        *,
        worker_factory: Callable | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.args = args
        self.source_key = source_key
        self.active = True
        self.query = ""
        self.editing = True
        self.focus = "results"
        self.reader = False
        self.consent = ""
        self.hits: list[dict] = []
        self.selected = 0
        self.result_scroll = 0
        self.preview: dict | None = None
        self.preview_scroll = 0
        self.preview_anchor: str | None = None
        self.response: dict = {}
        self.status: dict = {}
        self.index_report: dict = {}
        self.busy = ""
        self.error = ""
        self.notice = "Search conversations. Index updates are explicit."
        self.scope: dict = {}
        if selected_session_key:
            self.scope["session"] = selected_session_key
        self._selected_session_title = selected_session_title
        self._suggested_project = project
        self.page_size = 10
        self.preview_height = 10
        self.preview_lines = 0
        self.filter_field = ""
        self.filter_text = ""
        self.filter_menu = ""
        self.filter_menu_index = 0
        self.help = False
        self.help_scroll = 0
        self.help_page_size = 10

        self._worker_factory = worker_factory
        self._worker = None
        self._clock = clock
        self._generation = 0
        self._deadline: float | None = None
        self._pending: dict[str, tuple[int, int, dict]] = {}
        self._scope_stack: list[dict] = []
        self._reader_history: list[tuple[dict, int, str | None]] = []
        self._reader_return: tuple[dict | None, int, str | None] | None = None
        self._reader_saved: tuple[
            tuple[str, str, str], dict | None, int, str | None, list[tuple[dict, int, str | None]]
        ] | None = None
        self._reader_return_pending = False
        self._started = False
        self._index_offered = False

    @property
    def selected_hit(self) -> dict | None:
        if not self.hits:
            return None
        self.selected = max(0, min(self.selected, len(self.hits) - 1))
        return self.hits[self.selected]

    @property
    def conversation_available(self) -> bool:
        return self.selected_hit is not None

    @property
    def scope_label(self) -> str:
        parts = []
        if session := self.scope.get("session"):
            parts.append(
                f"session {self._selected_session_title}"
                if self._selected_session_title
                else f"session {str(session)[:18]}"
            )
        if project := self.scope.get("project"):
            parts.append(f"project {project}")
        if harness := self.scope.get("harness"):
            parts.append(f"harness {harness}")
        since, until = self.scope.get("since"), self.scope.get("until")
        if since or until:
            parts.append(f"messages {since or '...'}..{until or '...'}")
        return " / ".join(parts) or "global catalog"

    def _make_worker(self):
        if self._worker is None:
            factory = self._worker_factory
            if factory is None:
                from opentab.tui.search_worker import SearchWorker

                factory = SearchWorker
            self._worker = factory(self.args, self.source_key)
        return self._worker

    def _submit(self, operation: str, *, _meta: dict | None = None, **params) -> int:
        worker = self._make_worker()
        job_id = worker.submit(operation, **params)
        self._pending[operation] = (job_id, self._generation, dict(_meta or {}))
        self.busy = operation
        self.error = ""
        return job_id

    def _schedule_search(self, *, immediate: bool = False) -> None:
        self._reset_reader()
        self._generation += 1
        self.error = ""
        if self._worker is not None:
            self._worker.discard_pending()
        self._pending.pop("search", None)
        self._pending.pop("conversation", None)
        self.busy = "index" if "index" in self._pending else ""
        self.hits = []
        self.selected = 0
        self.result_scroll = 0
        self.response = {}
        self.preview = None
        self.preview_scroll = 0
        self.preview_anchor = None
        if not self.query.strip() or self.consent:
            self.hits = []
            self.response = {}
            self._deadline = None
            return
        self._deadline = self._clock() if immediate else self._clock() + self.DEBOUNCE_SECONDS

    def _start_due_search(self) -> None:
        if (
            self._deadline is None
            or self._clock() < self._deadline
            or self.consent
            or "status" in self._pending
        ):
            return
        self._deadline = None
        params = {"query": self.query.strip(), "limit": 100, "max_chars": self.SEARCH_MAX_CHARS}
        params.update(self.scope)
        self._submit("search", **params)

    @staticmethod
    def _error_text(error) -> str:
        if isinstance(error, dict):
            code = str(error.get("code") or "operation_failed")
            message = str(error.get("message") or code)
            return f"{code}: {message}" if message != code else code
        return str(error or "operation failed")

    def poll(self) -> None:
        if not self.active:
            return
        if not self._started:
            self._started = True
            self._submit("status")
        self._start_due_search()
        if self._worker is None:
            return
        for job_id, operation, result, error in self._worker.poll():
            pending = self._pending.get(operation)
            stale_read = (
                operation in {"search", "conversation"}
                and pending is not None
                and pending[1] != self._generation
            )
            if pending is None or pending[0] != job_id or stale_read:
                continue
            del self._pending[operation]
            self.busy = next(iter(self._pending), "")
            if error:
                self.error = self._error_text(error)
                if operation == "index":
                    self.index_report = {
                        "complete": False,
                        "updated": 0,
                        "unchanged": 0,
                        "unsupported": 0,
                        "errors": [error],
                    }
                    self.notice = (
                        "Index refresh partial: 0 updated, 0 unchanged, " "0 unsupported, 1 error."
                    )
                continue
            if operation == "status":
                self.status = result or {}
                if (
                    self.status.get("exists") is False
                    and not self._index_offered
                    and "index" not in self._pending
                ):
                    self._index_offered = True
                    self.consent = "index"
                    self.notice = "Build a local search index to search conversation text."
            elif operation == "index":
                self.index_report = result or {}
                self.status = self.index_report.get("index", self.status)
                errors = len(self.index_report.get("errors") or [])
                complete = bool(self.index_report.get("complete"))
                self.notice = (
                    f"Index refresh {'complete' if complete else 'partial'}: "
                    f"{int(self.index_report.get('updated') or 0)} updated, "
                    f"{int(self.index_report.get('unchanged') or 0)} unchanged, "
                    f"{int(self.index_report.get('unsupported') or 0)} unsupported, "
                    f"{errors} error{'s' if errors != 1 else ''}. Date filters were not used."
                )
                self._schedule_search()
            elif operation == "search":
                self.response = result or {}
                self.hits = list(self.response.get("hits") or [])
                self.selected = min(self.selected, max(0, len(self.hits) - 1))
                self.result_scroll = min(self.result_scroll, self.selected)
                if self.hits:
                    self._request_preview(self.selected_hit)
            elif operation == "conversation":
                meta = pending[2]
                history_entry = meta.get("history_entry")
                if history_entry is not None:
                    self._reader_history.append(history_entry)
                    if len(self._reader_history) > self.READER_HISTORY_MAX:
                        del self._reader_history[: -self.READER_HISTORY_MAX]
                self.preview = result or {}
                self.preview_scroll = 0
                if meta.get("earlier"):
                    records = self.preview.get("records") or []
                    first = records[0] if records else {}
                    self.preview_anchor = first.get("id") or first.get("record_id")
                else:
                    self.preview_anchor = meta.get("display_anchor")
                if meta.get("reader"):
                    if self._reader_return_pending:
                        self._reader_return = (
                            copy.deepcopy(self.preview),
                            self.preview_scroll,
                            self.preview_anchor,
                        )
                        self._reader_return_pending = False

    def _request_preview(
        self,
        hit: dict | None,
        *,
        reader: bool = False,
        cursor: str | None = None,
        anchor: str | None = None,
        before: int = 0,
        display_anchor: str | None = None,
        earlier: bool = False,
        history_entry: tuple[dict, int, str | None] | None = None,
    ) -> None:
        if hit is None or self.consent:
            return
        params = {
            "session": hit.get("session_key"),
            "execution_id": hit.get("execution_id"),
            "limit": self.READER_LIMIT,
            "max_chars": self.READER_MAX_CHARS,
        }
        if cursor:
            params["cursor"] = cursor
        else:
            params["anchor"] = anchor or hit.get("anchor")
            params["before"] = before
        self._submit(
            "conversation",
            _meta={
                "reader": reader,
                "hit": self._hit_identity(hit),
                "initial": not cursor and not earlier and before == 0,
                "display_anchor": display_anchor or (None if cursor else hit.get("anchor")),
                "earlier": earlier,
                "history_entry": copy.deepcopy(history_entry),
            },
            **params,
        )

    def select(self, index: int) -> None:
        if not self.hits:
            self.selected = self.result_scroll = 0
            self.preview = None
            self.preview_scroll = 0
            self.preview_anchor = None
            self._reset_reader()
            return
        index = max(0, min(index, len(self.hits) - 1))
        if index == self.selected and self.preview is not None:
            return
        self.selected = index
        self._reset_reader()
        self.preview = None
        self.preview_scroll = 0
        self.preview_anchor = None
        self._generation += 1
        self._pending.pop("conversation", None)
        if self._worker is not None:
            self._worker.discard_pending()
        self._request_preview(self.selected_hit)

    def _state(self) -> dict:
        state = {
            name: copy.deepcopy(getattr(self, name))
            for name in (
                "query",
                "editing",
                "focus",
                "reader",
                "hits",
                "selected",
                "result_scroll",
                "preview",
                "preview_scroll",
                "preview_anchor",
                "response",
                "scope",
                "error",
                "notice",
            )
        }
        state["_selected_session_title"] = self._selected_session_title
        state["_reader_history"] = copy.deepcopy(self._reader_history)
        state["_reader_return"] = copy.deepcopy(self._reader_return)
        state["_reader_saved"] = copy.deepcopy(self._reader_saved)
        return state

    def _restore_state(self, state: dict) -> None:
        self._generation += 1
        if self._worker is not None:
            self._worker.discard_pending()
        self._pending = {
            key: value for key, value in self._pending.items() if key in {"index", "status"}
        }
        self._deadline = None
        self.busy = next(iter(self._pending), "")
        self._reset_reader()
        for name, value in state.items():
            setattr(self, name, value)

    def _session_scope(self) -> None:
        hit = self.selected_hit
        session = hit.get("session_key") if hit is not None else self.scope.get("session")
        if not session or self.scope.get("session") == session:
            return
        if self.reader:
            self.switch_view("results")
        self._scope_stack.append(self._state())
        if len(self._scope_stack) > self.READER_HISTORY_MAX:
            del self._scope_stack[: -self.READER_HISTORY_MAX]
        self.scope["session"] = session
        self._selected_session_title = str((hit or {}).get("title") or "")
        self.selected = self.result_scroll = 0
        self.hits = []
        self.response = {}
        self.preview = None
        self.reader = False
        self._schedule_search()

    def _all_scope(self) -> None:
        self._scope_stack.clear()
        if "session" not in self.scope:
            return
        self.scope.pop("session", None)
        self._selected_session_title = ""
        self.selected = self.result_scroll = 0
        self._schedule_search()

    def filter_options(self) -> list[tuple[str, str, bool]]:
        if self.filter_menu == "scope":
            session = self.selected_hit is not None or bool(self.scope.get("session"))
            return [
                ("all", "All sessions", True),
                ("session", "This session", session),
            ]
        if self.filter_menu == "harness":
            return [
                ("all", "All harnesses", True),
                ("opencode", "OpenCode", True),
                ("claude", "Claude Code", True),
                ("codex", "Codex", True),
            ]
        return []

    def open_filter(self, name: str) -> None:
        if name == "project":
            self.filter_menu = ""
            self._project_prompt()
            return
        if name == "date":
            self.filter_menu = ""
            self._date_prompt()
            return
        if name == "reset":
            self.filter_menu = ""
            self.filter_field = self.filter_text = ""
            self._scope_stack.clear()
            if self.scope:
                self.scope = {}
                self._selected_session_title = ""
                self.selected = self.result_scroll = 0
                self._schedule_search()
            return
        if name not in {"scope", "harness"}:
            raise ValueError(f"unknown search filter: {name}")
        self.filter_field = self.filter_text = ""
        self.filter_menu = name
        current = self.scope.get("session") if name == "scope" else self.scope.get("harness")
        value = "session" if name == "scope" and current else current or "all"
        options = self.filter_options()
        self.filter_menu_index = next(
            (index for index, option in enumerate(options) if option[0] == value), 0
        )

    def choose_filter(self, index: int) -> None:
        options = self.filter_options()
        if not options:
            return
        index = max(0, min(index, len(options) - 1))
        self.filter_menu_index = index
        value, _label, enabled = options[index]
        if not enabled:
            return
        menu = self.filter_menu
        self.filter_menu = ""
        if menu == "scope":
            if value == "session":
                self._session_scope()
            else:
                self._all_scope()
            return
        current = self.scope.get("harness") or "all"
        if current == value:
            return
        if value == "all":
            self.scope.pop("harness", None)
        else:
            self.scope["harness"] = value
        self.selected = self.result_scroll = 0
        self._schedule_search()

    def _project_prompt(self) -> None:
        self.filter_menu = ""
        hit = self.selected_hit or {}
        self.filter_field = "project"
        self.filter_text = str(
            self.scope.get("project") or hit.get("project") or self._suggested_project or ""
        )[: self.FILTER_MAX_CHARS]

    def _date_prompt(self) -> None:
        self.filter_menu = ""
        self.filter_field = "date"
        self.filter_text = f"{self.scope.get('since') or ''}..{self.scope.get('until') or ''}"

    @staticmethod
    def _valid_date(value: str) -> bool:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            return False
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True

    def _commit_filter(self) -> None:
        raw = self.filter_text.strip()
        if self.filter_field == "project":
            if raw:
                self.scope["project"] = raw
            else:
                self.scope.pop("project", None)
        elif self.filter_field == "date":
            if ".." not in raw:
                self.error = "date must be YYYY-MM-DD..YYYY-MM-DD"
                return
            since, until = (part.strip() for part in raw.split("..", 1))
            if (since and not self._valid_date(since)) or (until and not self._valid_date(until)):
                self.error = "date must be YYYY-MM-DD..YYYY-MM-DD"
                return
            if since and until and since > until:
                self.error = "date start must not be after date end"
                return
            for name, value in (("since", since), ("until", until)):
                if value:
                    self.scope[name] = value
                else:
                    self.scope.pop(name, None)
        self.filter_field = self.filter_text = ""
        self.selected = self.result_scroll = 0
        self._schedule_search()

    def _confirm_index(self) -> None:
        self._index_offered = True
        if "index" in self._pending:
            self.consent = ""
            self.notice = "Index refresh is already running."
            return
        self.consent = ""
        params = {
            key: value
            for key, value in self.scope.items()
            if key in {"project", "harness", "session", "machine"}
        }
        self.notice = (
            "Refreshing the persistent plaintext index; date bounds are deliberately ignored."
        )
        self._submit("index", **params)

    @staticmethod
    def _hit_identity(hit: dict) -> tuple[str, str, str]:
        return (
            str(hit.get("session_key") or ""),
            str(hit.get("execution_id") or ""),
            str(hit.get("anchor") or ""),
        )

    def switch_view(self, view: str) -> None:
        if view not in {"results", "conversation"}:
            raise ValueError(f"unknown search view: {view}")
        if view == "results":
            self.editing = False
            if not self.reader:
                return
            hit = self.selected_hit
            if hit is not None and self.preview is not None:
                self._reader_saved = (
                    self._hit_identity(hit),
                    copy.deepcopy(self.preview),
                    self.preview_scroll,
                    self.preview_anchor,
                    copy.deepcopy(self._reader_history),
                )
            pending = self._pending.get("conversation")
            preserve_initial = bool(
                hit is not None
                and pending is not None
                and pending[2].get("initial")
                and pending[2].get("hit") == self._hit_identity(hit)
            )
            if preserve_initial and pending is not None:
                pending[2]["reader"] = False
            else:
                self._generation += 1
                self._pending.pop("conversation", None)
                if self._worker is not None:
                    self._worker.discard_pending()
                self.busy = "index" if "index" in self._pending else ""
            if self._reader_return is not None:
                self.preview, self.preview_scroll, self.preview_anchor = self._reader_return
            self.reader = False
            self._reader_history = []
            self._reader_return = None
            self._reader_return_pending = False
            return

        hit = self.selected_hit
        if hit is None or self.reader:
            return
        self.editing = False
        self._reader_return = (
            copy.deepcopy(self.preview),
            self.preview_scroll,
            self.preview_anchor,
        )
        self._reader_return_pending = self.preview is None
        pending = self._pending.get("conversation")
        reuse_initial = bool(
            pending is not None
            and pending[2].get("initial")
            and pending[2].get("hit") == self._hit_identity(hit)
        )
        saved = self._reader_saved
        self.reader = True
        if saved is not None and saved[0] == self._hit_identity(hit):
            _, self.preview, self.preview_scroll, self.preview_anchor, history = copy.deepcopy(
                saved
            )
            self._reader_history = history
            self._reader_return_pending = False
        elif self.preview is not None:
            self._reader_history = []
            self.preview_anchor = hit.get("anchor")
        elif reuse_initial and pending is not None:
            pending[2]["reader"] = True
        else:
            self._reader_history = []
            self._request_preview(hit, reader=True)

    def _open_reader(self) -> None:
        self.switch_view("conversation")

    def _reset_reader(self) -> None:
        self.reader = False
        self._reader_history.clear()
        self._reader_return = None
        self._reader_saved = None
        self._reader_return_pending = False

    def _close_reader(self) -> None:
        self.switch_view("results")

    def _reader_next(self) -> None:
        if "conversation" in self._pending:
            return
        if not self.preview or not self.preview.get("next_cursor"):
            self.notice = "No later retained records."
            return
        self._request_preview(
            self.selected_hit,
            reader=True,
            cursor=self.preview["next_cursor"],
            history_entry=(copy.deepcopy(self.preview), self.preview_scroll, self.preview_anchor),
        )

    def _reader_previous(self) -> None:
        if self._reader_history:
            self._generation += 1
            self._pending.pop("conversation", None)
            if self._worker is not None:
                self._worker.discard_pending()
            self.busy = "index" if "index" in self._pending else ""
            self.preview, self.preview_scroll, self.preview_anchor = self._reader_history.pop()
            return
        preview = self.preview or {}
        if "conversation" in self._pending:
            return
        records = preview.get("records") or []
        first_parts = records[0].get("parts") if records else []
        starts_mid_part = any(int(part.get("text_offset") or 0) > 0 for part in first_parts or [])
        if not records or (not preview.get("has_earlier") and starts_mid_part):
            self.notice = "Earlier content is outside this bounded continuation; reopen the match."
            return
        if not preview.get("has_earlier"):
            self.notice = "No earlier retained records."
            return
        anchor = records[0].get("id") or records[0].get("record_id")
        self._request_preview(
            self.selected_hit,
            reader=True,
            anchor=anchor,
            before=self.READER_LIMIT,
            earlier=True,
        )

    def edit_query(self) -> None:
        if self.reader:
            self.switch_view("results")
        if "conversation" in self._pending:
            self._pending["conversation"][2]["reader"] = False
            self._reader_return_pending = False
        self.help = False
        self.filter_menu = ""
        self.filter_field = self.filter_text = ""
        self.editing = True
        self.focus = "results"

    def handle_key(self, key: int | str, keymap: bindings.Keymap) -> bool:
        if key == 3:
            self.close()
            return False
        if self.consent:
            if key in (10, 13, ord("y"), ord("Y")):
                self._confirm_index()
                return True
            if key in (27, ord("n"), ord("N")):
                self.consent = ""
                self._index_offered = True
                self.notice = "Index update cancelled; nothing was written."
            return True
        context = "search.edit" if self.editing or self.filter_field else "search"
        if self.filter_menu:
            action = keymap.action("menu", key)
            options = self.filter_options()
            if action == "down" and options:
                self.filter_menu_index = (self.filter_menu_index + 1) % len(options)
            elif action == "up" and options:
                self.filter_menu_index = (self.filter_menu_index - 1) % len(options)
            elif action == "first" and options:
                self.filter_menu_index = 0
            elif action == "last" and options:
                self.filter_menu_index = len(options) - 1
            elif action == "select":
                self.choose_filter(self.filter_menu_index)
            elif action == "cancel":
                self.filter_menu = ""
            elif "edit" in {
                keymap.action("search", key),
                keymap.action("search.edit", key),
            }:
                self.edit_query()
            return True
        if keymap.action(context, key) == "edit":
            self.edit_query()
            return True
        if self.help:
            action = keymap.action("help", key)
            if action == "close" or keymap.action("search", key) == "help":
                self.help = False
            elif action in ("down", "page_down"):
                self.help_scroll += self.help_page_size if action == "page_down" else 1
            elif action in ("up", "page_up"):
                self.help_scroll = max(
                    0, self.help_scroll - (self.help_page_size if action == "page_up" else 1)
                )
            elif action == "top":
                self.help_scroll = 0
            elif action == "bottom":
                self.help_scroll = 10_000
            return True
        if self.filter_field:
            act = keymap.action("search.edit", key)
            if act == "open":
                self._commit_filter()
            elif act == "back":
                self.filter_field = self.filter_text = ""
            elif act == "erase":
                self.filter_text = self.filter_text[:-1]
            elif act == "clear":
                self.filter_text = ""
            elif (ch := bindings.typed_char(key)) is not None:
                if len(self.filter_text) < self.FILTER_MAX_CHARS:
                    self.filter_text += ch
                else:
                    self.notice = f"Scope input is limited to {self.FILTER_MAX_CHARS} characters."
            return True

        context = "search.edit" if self.editing else "search"
        act = keymap.action(context, key)
        if act == "back":
            if self.reader:
                self.switch_view("results")
                return True
            if self.editing:
                self.editing = False
                self.focus = "results"
                return True
            if self._scope_stack:
                self._restore_state(self._scope_stack.pop())
                return True
            self.close()
            return False
        if act == "help":
            self.help = True
            self.help_scroll = 0
        elif act == "focus_next":
            if self.editing:
                self.editing = False
                self.focus = "results"
            else:
                self.focus = "preview" if self.focus == "results" else "results"
        elif act == "focus_previous":
            if self.editing:
                self.editing = False
                self.focus = "preview"
            else:
                self.focus = "results" if self.focus == "preview" else "preview"
        elif act == "open":
            if self.editing:
                self.editing = False
                self.focus = "results"
                if self._deadline is not None:
                    self._deadline = self._clock()
            else:
                self.switch_view("conversation")
        elif act in ("prev_tab", "next_tab"):
            self.switch_view("results" if self.reader else "conversation")
        elif act == "preview_down":
            if not self.reader:
                self.preview_scroll += 1
        elif act == "preview_up":
            if not self.reader:
                self.preview_scroll = max(0, self.preview_scroll - 1)
        elif act == "down":
            if self.focus == "results" and not self.reader:
                self.select(self.selected + 1)
            else:
                self.preview_scroll += 1
        elif act == "up":
            if self.focus == "results" and not self.reader:
                self.select(self.selected - 1)
            else:
                self.preview_scroll = max(0, self.preview_scroll - 1)
        elif act in ("page_down", "page_up"):
            step = max(
                1,
                self.page_size
                if self.focus == "results" and not self.reader
                else self.preview_height,
            )
            direction = 1 if act == "page_down" else -1
            if self.focus == "results" and not self.reader:
                self.select(self.selected + direction * step)
            else:
                self.preview_scroll = max(0, self.preview_scroll + direction * step)
        elif act == "scope_session":
            self._session_scope()
        elif act == "scope_all":
            self._all_scope()
        elif act == "scope_menu":
            self.open_filter("scope")
        elif act == "reset_filters":
            self.open_filter("reset")
        elif act == "scope_project":
            self.open_filter("project")
        elif act == "scope_harness":
            self.open_filter("harness")
        elif act == "scope_date":
            self.open_filter("date")
        elif act == "index":
            if "index" in self._pending:
                self.notice = "Index refresh is already running."
            else:
                self.consent = "index"
                self.notice = (
                    "Indexing persists sensitive plaintext locally. Date bounds do not limit what is stored; "
                    "saved project/session ignores still apply."
                )
        elif act == "previous":
            if self.reader:
                self._reader_previous()
        elif act == "next":
            if self.reader:
                self._reader_next()
        elif self.editing and act == "erase":
            self.query = self.query[:-1]
            self._schedule_search()
        elif self.editing and act == "clear":
            self.query = ""
            self._schedule_search()
        elif self.editing and (ch := bindings.typed_char(key)) is not None:
            if len(self.query) < self.QUERY_MAX_CHARS:
                self.query += ch
                self.selected = self.result_scroll = 0
                self._schedule_search()
            else:
                self.notice = f"Query is limited to {self.QUERY_MAX_CHARS} characters."
        return True

    def close(self) -> None:
        self.active = False
        self._deadline = None
        self._pending.clear()
        if self._worker is not None:
            self._worker.close()
            self._worker = None
