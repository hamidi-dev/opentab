"""Identity-owned Work browsing and revision-captured dialogs; no store queries."""
from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

from opentab.persistence.work_items import MAX_NEXT_STEP_CHARS, MAX_TITLE_CHARS, WorkItemError
from opentab.persistence.workflow_config import load_workflow_config
from opentab.tui import bindings

if TYPE_CHECKING:
    from opentab.tui.app import App


SECTIONS = ("open", "done", "unknown", "archived")


def session_work_title(title: str) -> str:
    # Harness titles can include terminal controls/newlines. Prefill is authored only on save.
    printable = "".join(" " if unicodedata.category(c).startswith("C") else c for c in title)
    return " ".join(printable.split())[:MAX_TITLE_CHARS] or "Untitled work"


class WorkWorkspace:
    def __init__(self, app: App):
        self.app = app
        self.section = "open"
        self.query = ""
        self.query_edit = False
        self.follow_scope = True
        self.selected_id: str | None = None
        self.item_id: str | None = None  # open detail is not an ordinal in the Open list
        self.root_key: str | None = None
        self.dialog: str | None = None
        self.dialog_index = 0
        self.draft = ""
        self.error = ""
        self.target_item: tuple[str, int] | None = None
        self.target_root: tuple[str, int, str | None] | None = None
        self.pending: tuple[str, str | None] | None = None
        self.return_dialog: str | None = None
        self.text_scroll = 0

    @property
    def work(self):
        return self.app.work_items

    def item(self):
        return self.work.snapshot.items.get(self.item_id or self.selected_id)

    def items(self):
        matching = self.app.work_matching_keys() if self.follow_scope else None
        rows = [
            item
            for item in self.work.snapshot.items.values()
            if self.work.section(item.id) == self.section
            and (
                not self.query
                or self.query.casefold() in (item.title + " " + item.next_step).casefold()
            )
            and (
                matching is None or bool(matching.intersection(self.work.snapshot.keys_of(item.id)))
            )
        ]
        rows.sort(key=lambda item: (item.title.casefold(), item.id))
        if rows and self.selected_id not in {item.id for item in rows}:
            self.selected_id = rows[0].id
        return rows

    def root(self, key: str | None = None):
        key = self.root_key if key is None else key
        matches = [w for w in self.app.work_catalog() if self.app.session_tag_key(w) == key]
        return matches[0] if len(matches) == 1 else None

    def roots(self):
        return list(self.work.snapshot.keys_of(self.item_id)) if self.item_id else []

    def root_reason(self, key: str) -> str:
        row = self.root(key)
        if row is None:
            return "missing, ignored, unloaded or ambiguous root"
        if self.app.resume_parts(row) is None:
            return "this harness/session has no resume command"
        if self.app.unreachable_machine_for(row):
            return "remote snapshot has no SSH target; copy handoff only"
        return ""

    def _editable(self) -> bool:
        if not self.app.work_items_active:
            self.error = "Work is off in demo / --no-state or configuration"
            return False
        refusal = self.work.edit_refusal()
        if refusal:
            self.error = refusal
            return False
        return True

    def _error(self, exc: WorkItemError):
        if exc.code == "stale":
            self.work.refresh(allowed=self.app.allow_notes)
        self.error = f"Not saved: {exc.message}. Cancel, reload and reopen to retry."

    def refresh(self):
        # Reload never changes an open item's identity, even if it leaves its section.
        if self.item_id and self.item_id not in self.work.snapshot.items:
            self.error = "This work item is unavailable in the current snapshot"
        if self.root_key and self.root_key not in self.roots():
            self.root_key = None

    def _dialog(self, kind: str):
        self.dialog, self.dialog_index, self.error = kind, 0, ""

    def create(self, *, from_session=False):
        if not self._editable():
            return
        self.target_item = self.target_root = None
        self.draft = ""
        if from_session:
            row = self.app.bookmark_target()
            if row is None:
                self.error = "Choose a session first"
                return
            key = self.app._tag_unique_target(row)
            if key is None:
                self.error = "Session identity is ambiguous"
                return
            member = self.work.snapshot.membership(key)
            if member.item_id is not None:
                self.error = "Session already belongs to work; use an explicit move instead"
                return
            self.target_root = (key, member.revision, None)
            self.draft = session_work_title(row.title)
        self._dialog("create")

    def edit_text(self, field: str):
        item = self.item()
        if item is None or not self._editable():
            return
        self.target_item = (item.id, item.revision)
        self.draft = getattr(item, field)
        self._dialog(field)

    def choose_state(self, *, reopen=False):
        item = self.item()
        if item is None or not self._editable():
            return
        self.target_item = (item.id, item.revision)
        self._dialog("reopen" if reopen else "state")

    def cycle_state(self):
        item = self.item()
        if item is None or not self._editable() or not self.work.states:
            return
        ids = [state.id for state in self.work.states]
        index = ids.index(item.state_id) if item.state_id in ids else -1
        try:
            self.work.edit(item.id, item.revision, state_id=ids[(index + 1) % len(ids)])
        except WorkItemError as exc:
            self._error(exc)

    def archive(self):
        item = self.item()
        if item is None or not self._editable():
            return
        self.target_item = (item.id, item.revision)
        self.pending = ("unarchive" if item.archived else "archive", item.id)
        self._dialog("confirm")

    def _capture_root(self, key: str, *, fresh: bool):
        # New attachments and moves need one exact loaded owner. Detach may repair retention.
        if fresh and (self.root(key) is None or not self.app._tag_targets_available({key: 0})):
            self.error = "Root is missing, ignored or ambiguous; cannot attach/move it"
            return False
        member = self.work.snapshot.membership(key)
        self.target_root = (key, member.revision, member.item_id)
        return True

    def assign_session(self):
        if not self._editable():
            return
        row = self.app.bookmark_target()
        if row is None:
            self.error = "Choose a session first"
            return
        if self._capture_root(self.app.session_tag_key(row), fresh=True):
            self._dialog("assign")

    def attach_root(self):
        if self.item_id and self._editable():
            self.target_item = (self.item_id, self.item().revision)
            self._dialog("roots")

    def detach_root(self):
        if not self.root_key or not self._editable():
            self.error = "Select a linked root first"
            return
        if self._capture_root(self.root_key, fresh=False):
            self.pending = ("detach", None)
            self._dialog("confirm")

    def move_root(self):
        if not self.root_key or not self._editable():
            self.error = "Select a linked root first"
            return
        if self._capture_root(self.root_key, fresh=True):
            self._dialog("assign")

    def resume(self, key=None):
        if not self.app.work_items_active or not load_workflow_config().work_items:
            self.app.refresh_work_items()
            self.error = "Work is off; no session was launched"
            return
        if key is None and self.root_key is None:
            self._dialog("resume")  # explicit picker even when recency suggests a candidate
            return
        key = self.root_key if key is None else key
        row = self.root(key)
        if row is None or self.app.resume_parts(row) is None:
            self.error = self.root_reason(key)
            return
        if not self.app._tag_targets_available({key: 0}):
            self.error = "Ambiguous root identity; cannot resume"
            return
        self.root_key, self.dialog = key, None
        self.app.launch_workflow(row)

    def options(self):
        if self.dialog in ("state", "reopen"):
            return [
                (state.id, state.label)
                for state in self.work.states
                if self.dialog != "reopen" or not state.done
            ]
        if self.dialog == "assign":
            rows = [
                (item.id, item.title)
                for item in sorted(
                    self.work.snapshot.items.values(),
                    key=lambda item: (item.title.casefold(), item.id),
                )
            ]
            return [("", "Detach from work (keep the item)")] + rows
        if self.dialog == "roots":
            # Newest first: the session just worked on is the likely attachment.
            rows = sorted(
                self.app.work_catalog(),
                key=lambda row: (row.created_at, self.app.session_tag_key(row)),
                reverse=True,
            )
            return [
                (
                    self.app.session_tag_key(row),
                    f"{row.created_at[:10]}  {row.source or 'unknown'}  {row.title or '(untitled)'}",
                )
                for row in rows
            ]
        if self.dialog == "resume":
            return [(key, key) for key in self.roots()]
        if self.dialog == "confirm":
            return [("confirm", "Confirm"), ("cancel", "Cancel")]
        if self.dialog:
            return []
        if self.item_id:
            return [(key, key) for key in self.roots()]
        return [(item.id, item.title) for item in self.items()]

    def apply(self):
        if not self.app.work_items_active:
            self.error = "Work is off"
            return
        options = self.options()
        value = options[min(self.dialog_index, len(options) - 1)][0] if options else None
        if self.dialog is None:
            if self.item_id:
                self.resume()
            elif value is not None:
                # Browse cursor is anchored by item ID, not the incidental dialog index.
                self.item_id, self.root_key = self.selected_id, None
            return
        if self.dialog == "resume":
            if value is not None:
                self.resume(value)
            return
        if not self._editable():
            return
        if self.dialog in ("roots", "assign"):
            if value is None:
                return
            if self.dialog == "roots":
                if not self._capture_root(value, fresh=True):
                    return
                dest = self.target_item[0]
            else:
                dest = value or None
            source = self.target_root[2]
            verb = "detach" if dest is None else "move" if source and source != dest else "attach"
            self.pending = (verb, dest)
            self._dialog("confirm")
            return
        if self.dialog == "confirm" and value == "cancel":
            self.dialog = None
            return
        try:
            if self.dialog == "create":
                target = self.target_root
                if target and not self._capture_available(target[0]):
                    return
                ident = self.work.create(
                    self.draft,
                    key=target[0] if target else None,
                    expected=target[1] if target else 0,
                )
                self.item_id = self.selected_id = ident
                self.root_key = target[0] if target else None
            elif self.dialog in ("title", "next_step"):
                self.work.edit(*self.target_item, **{self.dialog: self.draft})
            elif self.dialog in ("state", "reopen"):
                if value is None:
                    self.error = "No configured open states; configure one before reopening"
                    return
                fields = {"state_id": value}
                if self.dialog == "reopen":
                    fields["archived"] = False
                self.work.edit(*self.target_item, **fields)
            elif self.dialog == "confirm":
                verb, dest = self.pending
                if verb in ("archive", "unarchive"):
                    self.work.edit(*self.target_item, archived=verb == "archive")
                else:
                    key, revision, _source = self.target_root
                    if verb != "detach" and not self._capture_available(key):
                        return
                    operation = getattr(self.work, verb)
                    operation(key, revision) if verb == "detach" else operation(key, dest, revision)
            self.dialog, self.error = None, ""
            self.refresh()
        except WorkItemError as exc:
            self._error(exc)

    def _capture_available(self, key):
        if self.root(key) is None or not self.app._tag_targets_available({key: 0}):
            self.error = "Not saved: captured root is missing, ignored or ambiguous"
            return False
        return True

    def move_cursor(self, delta: int, *, edge=None):
        options = self.options()
        if not options:
            return
        if self.dialog:
            index = self.dialog_index
        else:
            current = self.root_key if self.item_id else self.selected_id
            index = next(
                (i for i, row in enumerate(options) if row[0] == current),
                -1 if delta > 0 else len(options),
            )
        index = (
            0
            if edge == "top"
            else len(options) - 1
            if edge == "bottom"
            else max(0, min(len(options) - 1, index + delta))
        )
        if self.dialog:
            self.dialog_index = index
        elif self.item_id:
            self.root_key = options[index][0]
        else:
            self.selected_id = options[index][0]

    def handle_key(self, key, stdscr=None):
        if key == 3:
            return False
        ctx = (
            "input"
            if self.query_edit or self.dialog in ("create", "title", "next_step")
            else "work"
        )
        act = self.app.keymap.action(ctx, key)
        if ctx == "input":
            text = self.query if self.query_edit else self.draft
            if act == "cancel":
                self.query_edit = False
                self.dialog = None
            elif act == "confirm":
                if self.query_edit:
                    self.query_edit = False
                else:
                    self.apply()
            else:
                if act == "erase":
                    text = text[:-1]
                elif act == "kill_line":
                    text = ""
                elif act == "kill_word":
                    text = text.rstrip().rsplit(" ", 1)[0] if " " in text.rstrip() else ""
                elif (ch := bindings.typed_char(key)) is not None:
                    limit = MAX_NEXT_STEP_CHARS if self.dialog == "next_step" else MAX_TITLE_CHARS
                    if len(text) < limit:
                        text += ch
                if self.query_edit:
                    self.query = text
                else:
                    self.draft = text
            return True
        if act == "back":
            if self.dialog:
                self.dialog, self.error = None, ""
            elif self.item_id:
                self.item_id, self.root_key, self.error = None, None, ""
            else:
                self.app.work_workspace = None
        elif act == "select":
            self.apply()
        elif act in ("down", "up", "top", "bottom", "page_down", "page_up"):
            delta = (
                5 if act == "page_down" else -5 if act == "page_up" else -1 if act == "up" else 1
            )
            if self.dialog == "step_view":
                self.text_scroll = (
                    0
                    if act == "top"
                    else 10000
                    if act == "bottom"
                    else max(0, self.text_scroll + delta)
                )
            else:
                self.move_cursor(delta, edge=act)
        elif self.dialog:
            return True  # a confirmation/picker cannot dispatch unrelated item actions
        elif act in ("section_next", "section_prev"):
            self.section = SECTIONS[
                (SECTIONS.index(self.section) + (1 if act == "section_next" else -1))
                % len(SECTIONS)
            ]
            self.item_id, self.root_key = None, None
        elif act == "create":
            self.create()
        elif act == "title":
            self.edit_text("title")
        elif act == "next_step":
            self.edit_text("next_step")
        elif act == "view_step" and self.item() is not None:
            self.text_scroll = 0
            self._dialog("step_view")
        elif act == "state":
            self.choose_state()
        elif act == "cycle_state":
            self.cycle_state()
        elif act == "archive":
            self.archive()
        elif act == "reopen":
            self.choose_state(reopen=True)
        elif act == "attach":
            self.attach_root()
        elif act == "detach":
            self.detach_root()
        elif act == "move":
            self.move_root()
        elif act == "resume":
            if self.item_id:
                self.resume()
            else:
                self.error = "Open an item, then choose its exact session to resume"
        elif act == "note":
            if self.item_id and self.root_key and self.root() is not None:
                self.app.edit_note(stdscr)
            else:
                self.error = "Select an available linked root to edit its session note"
        elif act == "filter":
            self.query_edit = True
        elif act == "clear_filter":
            self.query = ""
        elif act == "scope":
            self.follow_scope = not self.follow_scope
        elif act == "show_ignored":
            self.app.show_ignored_projects = not self.app.show_ignored_projects
            self.app._invalidate_workflow_cache()
        elif act == "reload":
            self.app.refresh_work_items()
        elif act == "api_prices":
            self.app.toggle_api_prices()
        return True
