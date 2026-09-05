"""Stable, presentation-neutral access to OpenTab's local accounting model."""
from __future__ import annotations

import os
import socket
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from opentab import sources
from opentab.models import API_SCHEMA_VERSION, SessionRef, Workflow
from opentab.notes import read_notes, update_note
from opentab.pricing import (
    api_equivalent_cost,
    cache_write_1h_price,
    canonical_model,
    catalog_models,
    has_known_price,
    is_local_provider,
    model_context_window,
    model_price,
)
from opentab.state import load_state, update_state
from opentab.util import (
    cached_share,
    context_size,
    git_root,
    model_row_1h_write,
    model_row_split,
    month_window_start,
    parse_range_text,
    resolve_project_root,
    tool_names,
    tool_namespace,
    workflow_fuzzy_score,
)

SCHEMA_VERSION = API_SCHEMA_VERSION
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

_SOURCE_KEYS = {label.lower(): key for key, label in sources.SOURCE_LABELS.items()}


class ServiceError(Exception):
    """A stable domain error suitable for CLI and MCP clients."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class SessionQuery:
    range: str = "all"
    project: str | None = None
    harness: str | None = None
    machine: str | None = None
    model: str | None = None
    search: str | None = None
    bookmarked: bool = False
    include_ignored: bool = False
    sort: str = "cost"
    reverse: bool = False
    limit: int = DEFAULT_LIMIT
    offset: int = 0


@dataclass
class _Session:
    workflow: Workflow
    owner: object
    ref: SessionRef
    project: str


class OpenTabService:
    """Long-lived query service shared by JSON CLI and MCP transports."""

    def __init__(
        self,
        store,
        args,
        source_key: str = "",
        *,
        allow_raw_content: bool = False,
    ):
        if getattr(store, "demo", False):
            raise ServiceError(
                "demo_unsupported",
                "programmatic access is unavailable in demo mode",
            )
        self.store = store
        self.args = args
        self.source_key = source_key or getattr(store, "source_name", "") or "data"
        self.allow_raw_content = bool(allow_raw_content)
        self.use_state = not bool(getattr(args, "no_state", False))
        self._sessions: list[_Session] = []
        self._by_key: dict[str, _Session] = {}
        self._by_native: dict[str, list[_Session]] = defaultdict(list)
        self._models_by_owner: dict[int, dict[str, list[dict]]] = {}
        self.reload()

    @classmethod
    def open(cls, args, *, allow_raw_content: bool = False) -> OpenTabService:
        if getattr(args, "demo", False):
            raise ServiceError(
                "demo_unsupported",
                "programmatic access is unavailable in demo mode",
            )
        source_key = sources.resolve_source(args, {})
        store, _loading = sources.make_store(args, source_key)
        return cls(store, args, source_key, allow_raw_content=allow_raw_content)

    def reload(self) -> None:
        reload_store = getattr(self.store, "reload", None)
        if self._sessions and callable(reload_store):
            reload_store()
        workflows = list(self.store.workflows())
        owner_of = getattr(self.store, "owner_of", None)
        local_machine = socket.gethostname() or "this machine"
        sessions = []
        for workflow in workflows:
            owner = owner_of(workflow) if callable(owner_of) else self.store
            harness = self._harness(workflow, owner)
            machine = workflow.machine or local_machine
            ref = SessionRef(machine, harness, workflow.id)
            sessions.append(
                _Session(
                    workflow=workflow,
                    owner=owner,
                    ref=ref,
                    project=resolve_project_root(git_root(workflow.directory)),
                )
            )
        self._sessions = sessions
        self._by_key = {item.ref.encode(): item for item in sessions}
        self._by_native = defaultdict(list)
        for item in sessions:
            self._by_native[item.workflow.id].append(item)
        self._models_by_owner.clear()

    @staticmethod
    def _harness(workflow: Workflow, owner) -> str:
        value = str(workflow.source or getattr(owner, "source_name", "") or "unknown")
        return _SOURCE_KEYS.get(value.lower(), value.lower().replace(" ", "-"))

    @staticmethod
    def _bounded(limit: int, offset: int) -> tuple[int, int]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_LIMIT:
            raise ServiceError("invalid_limit", f"limit must be between 1 and {MAX_LIMIT}")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ServiceError("invalid_offset", "offset must be zero or greater")
        return limit, offset

    @staticmethod
    def _date_bounds(spec: str) -> tuple[str | None, str | None]:
        try:
            days, months, since, until = parse_range_text(spec or "all")
        except ValueError as exc:
            raise ServiceError("invalid_range", str(exc)) from exc
        if days is not None:
            since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        elif months is not None:
            since = month_window_start(months)
        return since, until

    def _model_rows(self, item: _Session) -> list[dict]:
        owner_key = id(item.owner)
        grouped = self._models_by_owner.get(owner_key)
        if grouped is None:
            grouped = defaultdict(list)
            try:
                rows = item.owner.model_breakdown()
            except (OSError, ValueError) as exc:
                raise ServiceError("backend_error", str(exc)) from exc
            for row in rows:
                data = dict(row)
                root = data.get("root_id")
                if isinstance(root, str):
                    grouped[root].append(data)
            self._models_by_owner[owner_key] = grouped
        return list(grouped.get(item.workflow.id, ()))

    @staticmethod
    def _api_model_cost(row: dict) -> float:
        real = float(row.get("cost") or 0)
        whole = real == 0 and "unpriced_input" not in row
        prefix = "" if whole else "unpriced_"
        return real + api_equivalent_cost(
            str(row.get("model_name") or ""),
            row.get(prefix + "input", 0),
            row.get(prefix + "output", 0),
            row.get(prefix + "reasoning", 0),
            row.get(prefix + "cache_read", 0),
            row.get(prefix + "cache_write", 0),
            row.get("cache_write_1h", 0) if whole else row.get("unpriced_cache_write_1h", 0),
        )

    @staticmethod
    def _detail_api_cost(
        row: dict,
        fields: tuple[str, str, str, str, str, str],
        *,
        possibly_mixed: bool,
    ) -> float | None:
        recorded = float(row.get("cost") or 0)
        unpriced = []
        has_split = False
        for field in fields:
            plain = field.removeprefix("tokens_")
            candidates = (f"unpriced_{field}", f"unpriced_{plain}")
            key = next((name for name in candidates if name in row), None)
            has_split = has_split or key is not None
            unpriced.append(row.get(key, 0) if key else 0)
        if has_split:
            tokens = unpriced
        elif recorded == 0:
            tokens = [row.get(field, 0) for field in fields]
        elif possibly_mixed:
            # An aggregate can contain paid and $0 calls. Without its unpriced split,
            # returning the recorded dollars would silently understate the hybrid view.
            return None
        else:
            return recorded
        return recorded + api_equivalent_cost(str(row.get("model_name") or ""), *tokens)

    def _costs(self, item: _Session) -> tuple[float, float, float, float, int]:
        workflow = item.workflow
        models = self._model_rows(item)
        recorded = float(workflow.total_cost or 0)
        recorded_root = float(workflow.root_cost or 0)
        api = recorded + sum(
            self._api_model_cost(row) - float(row.get("cost") or 0) for row in models
        )
        has_root_split = any("root_unpriced_input" in row for row in models)
        if has_root_split:
            delta = sum(
                api_equivalent_cost(
                    str(row.get("model_name") or ""),
                    row.get("root_unpriced_input", 0),
                    row.get("root_unpriced_output", 0),
                    row.get("root_unpriced_reasoning", 0),
                    row.get("root_unpriced_cache_read", 0),
                    row.get("root_unpriced_cache_write", 0),
                    row.get("root_unpriced_cache_write_1h", 0),
                )
                for row in models
            )
            api_root = recorded_root + delta
        else:
            fraction = recorded_root / recorded if recorded else 1.0
            api_root = recorded_root + (api - recorded) * fraction
        unpriced_fields = (
            "unpriced_input",
            "unpriced_output",
            "unpriced_reasoning",
            "unpriced_cache_read",
            "unpriced_cache_write",
        )
        unpriced = (
            sum(int(row.get(field) or 0) for row in models for field in unpriced_fields)
            if models and any("unpriced_input" in row for row in models)
            else int(workflow.unpriced_tokens or 0)
        )
        return recorded, api, recorded_root, api_root, unpriced

    def _state_sets(self) -> tuple[set[str], set[str], set[str], set[str]]:
        if not self.use_state:
            return set(), set(), set(), set()
        state = load_state()
        sets = []
        for key in ("bookmarks", "ignored_projects", "ignored_sessions", "pinned_models"):
            value = state.get(key, [])
            sets.append(
                {item for item in value if isinstance(item, str)}
                if isinstance(value, list)
                else set()
            )
        return tuple(sets)  # type: ignore[return-value]

    def _session_value(self, item: _Session, values: set[str]) -> bool:
        return self._authored_session_key(item, values) in values

    def _session_note(self, item: _Session, notes: dict) -> str:
        note = notes.get(self._authored_session_key(item, notes))
        return note if isinstance(note, str) else ""

    def _authored_session_key(self, item: _Session, existing) -> str:
        # An existing qualified entry stays authoritative even in a narrower source
        # scope. Otherwise preserve native TUI keys only for unambiguous sessions.
        return (
            item.ref.encode()
            if item.ref.encode() in existing or len(self._by_native.get(item.workflow.id, ())) != 1
            else item.workflow.id
        )

    def _filtered(self, query: SessionQuery, *, paginate: bool = True) -> list[_Session]:
        if paginate:
            limit, offset = self._bounded(query.limit, query.offset)
        since, until = self._date_bounds(query.range)
        bookmarks, ignored_projects, ignored_sessions, _pins = self._state_sets()
        notes = read_notes()[0] if query.search and self.use_state else {}
        rows = list(self._sessions)
        if since:
            rows = [item for item in rows if item.workflow.created_at[:10] >= since]
        if until:
            rows = [item for item in rows if item.workflow.created_at[:10] <= until]
        if query.project:
            target = resolve_project_root(git_root(os.path.expanduser(query.project)))
            rows = [item for item in rows if item.project == target]
        if query.harness:
            target = _SOURCE_KEYS.get(query.harness.lower(), query.harness.lower())
            rows = [item for item in rows if item.ref.harness == target]
        if query.machine:
            rows = [item for item in rows if item.ref.machine == query.machine]
        if query.model:
            target = canonical_model(query.model)
            rows = [
                item
                for item in rows
                if any(
                    canonical_model(str(row.get("model_name") or "")) == target
                    for row in self._model_rows(item)
                )
            ]
        if query.bookmarked:
            rows = [item for item in rows if self._session_value(item, bookmarks)]
        if not query.include_ignored:
            rows = [
                item
                for item in rows
                if item.project not in ignored_projects
                and not self._session_value(item, ignored_sessions)
            ]
        if query.search:
            scored = [
                (
                    workflow_fuzzy_score(
                        query.search, item.workflow, self._session_note(item, notes)
                    ),
                    item,
                )
                for item in rows
            ]
            rows = [
                item
                for score, item in sorted(scored, key=lambda pair: pair[0] or -1, reverse=True)
                if score is not None
            ]
        rows = self._sort_sessions(rows, query.sort, query.reverse)
        return rows[offset : offset + limit] if paginate else rows

    def _sort_sessions(self, rows: list[_Session], key: str, reverse: bool) -> list[_Session]:
        if key not in {"cost", "tokens", "date", "last_activity", "title", "project"}:
            raise ServiceError("invalid_sort", f"unknown session sort: {key}")
        descending = key not in {"title", "project"}
        descending = not descending if reverse else descending

        def value(item: _Session):
            workflow = item.workflow
            if key == "cost":
                return self._costs(item)[1]
            if key == "tokens":
                return workflow.total_tokens
            if key == "date":
                return workflow.created_at
            if key == "last_activity":
                return workflow.ended_at or workflow.created_at
            if key == "project":
                return item.project.lower()
            return workflow.title.lower()

        return sorted(rows, key=value, reverse=descending)

    def _serialize_session(self, item: _Session, *, details: bool = False) -> dict:
        workflow = item.workflow
        recorded, api, recorded_root, api_root, unpriced = self._costs(item)
        bookmarks, ignored_projects, ignored_sessions, _pins = self._state_sets()
        models = self._model_rows(item)
        data = {
            "session_key": item.ref.encode(),
            "native_id": workflow.id,
            "harness": item.ref.harness,
            "machine": item.ref.machine,
            "title": workflow.title,
            "directory": workflow.directory,
            "project": item.project,
            "created_at": workflow.created_at,
            "last_activity_at": workflow.ended_at or workflow.created_at,
            "worked_seconds": workflow.worked_seconds,
            "recorded_cost_usd": recorded,
            "api_equivalent_cost_usd": api,
            "recorded_root_cost_usd": recorded_root,
            "api_equivalent_root_cost_usd": api_root,
            "tokens": int(workflow.total_tokens or 0),
            "unpriced_tokens": unpriced,
            "subagents": int(workflow.subagents or 0),
            "models": len(models),
            "bookmarked": self._session_value(item, bookmarks),
            "ignored": item.project in ignored_projects
            or self._session_value(item, ignored_sessions),
        }
        if details:
            note_map, readable = read_notes() if self.use_state else ({}, True)
            data["note"] = self._session_note(item, note_map)
            data["notes_readable"] = readable
            data["capabilities"] = self._capabilities(item)
        return data

    def list_sessions(self, query: SessionQuery | None = None) -> dict:
        query = query or SessionQuery()
        self._bounded(query.limit, query.offset)
        all_rows = self._filtered(query, paginate=False)
        rows = all_rows[query.offset : query.offset + query.limit]
        return {
            "sessions": [self._serialize_session(item) for item in rows],
            "total": len(all_rows),
            "limit": query.limit,
            "offset": query.offset,
        }

    def resolve_session(self, value: str) -> _Session:
        if value.startswith(SessionRef.PREFIX):
            try:
                key = SessionRef.decode(value).encode()
            except ValueError as exc:
                raise ServiceError("invalid_session_ref", str(exc)) from exc
            item = self._by_key.get(key)
            if item is None:
                raise ServiceError(
                    "session_not_found", "session is not present in the selected data"
                )
            return item
        matches = self._by_native.get(value, [])
        if not matches:
            raise ServiceError("session_not_found", f"session {value!r} was not found")
        if len(matches) > 1:
            raise ServiceError(
                "ambiguous_session",
                "native session id exists in more than one harness or machine; use session_key",
                {"matches": [item.ref.encode() for item in matches]},
            )
        return matches[0]

    def get_session(self, value: str) -> dict:
        item = self.resolve_session(value)
        data = self._serialize_session(item, details=True)
        data["model_usage"] = self._serialize_models(item)
        return data

    def _capabilities(self, item: _Session) -> dict:
        owner, sid = item.owner, item.workflow.id

        def supports(name: str, fallback: bool = False) -> bool:
            check = getattr(owner, name, None)
            if check is None:
                return fallback
            try:
                return bool(check(sid))
            except (OSError, ValueError):
                return False

        turns = supports("supports_turns")
        return {
            "nodes": bool(item.workflow.subagents),
            "turns": turns,
            "tools": supports("supports_tools"),
            "context": supports("supports_context"),
            "context_curve": supports("supports_context_curve", turns),
            "raw_content": self.allow_raw_content and supports("supports_turn_content"),
        }

    def _serialize_models(self, item: _Session) -> list[dict]:
        _bookmarks, _ignored_projects, _ignored_sessions, pinned_models = self._state_sets()
        out = []
        for row in self._model_rows(item):
            split = model_row_split(row)
            name = str(row.get("model_name") or "unknown")
            out.append(
                {
                    "model": name,
                    "runs": int(row.get("runs") or 0),
                    "recorded_cost_usd": float(row.get("cost") or 0),
                    "api_equivalent_cost_usd": self._api_model_cost(row),
                    "tokens": int(row.get("tokens_total") or sum(split)),
                    "input_tokens": int(split[0]),
                    "output_tokens": int(split[1]),
                    "reasoning_tokens": int(split[2]),
                    "cache_read_tokens": int(split[3]),
                    "cache_write_tokens": int(split[4]),
                    "cache_write_1h_tokens": int(model_row_1h_write(row)),
                    "known_price": has_known_price(name),
                    "local": is_local_provider(name),
                    "pinned": name in pinned_models or canonical_model(name) in pinned_models,
                }
            )
        out.sort(key=lambda row: (row["recorded_cost_usd"], row["tokens"]), reverse=True)
        return out

    def session_nodes(self, value: str) -> dict:
        item = self.resolve_session(value)
        costs = self._costs(item)
        possibly_mixed = bool(costs[4])
        models = self._model_rows(item)
        producing_models = {
            str(row.get("model_name") or "")
            for row in models
            if row.get("tokens_total") or any(model_row_split(row))
        }
        single_model = next(iter(producing_models)) if len(producing_models) == 1 else ""
        known_models = bool(producing_models) and all(
            has_known_price(model) or is_local_provider(model) for model in producing_models
        )
        exact_root_split = bool(models) and all("root_unpriced_input" in row for row in models)
        rows = []
        for raw in item.owner.workflow_nodes(item.workflow.id):
            row = dict(raw)
            recorded = float(row.get("cost") or 0)
            api = self._detail_api_cost(
                dict(row, model_name=single_model) if single_model else row,
                (
                    "tokens_input",
                    "tokens_output",
                    "tokens_reasoning",
                    "tokens_cache_read",
                    "tokens_cache_write",
                    "tokens_cache_write_1h",
                ),
                possibly_mixed=possibly_mixed,
            )
            if not int(row.get("depth") or 0) and exact_root_split and known_models:
                api = costs[3]
            elif (recorded == 0 or possibly_mixed) and row.get("tokens_total"):
                # A node's label is only its dominant model, not per-model usage.
                # Root model rows cannot attribute a multi-model tree to its children.
                if not single_model or not known_models:
                    api = None
            rows.append(
                {
                    "depth": int(row.get("depth") or 0),
                    "agent": str(row.get("agent") or "-"),
                    "title": str(row.get("title") or "(untitled)"),
                    "created_at": str(row.get("created_at") or ""),
                    "model": str(row.get("model_name") or "unknown"),
                    "recorded_cost_usd": recorded,
                    "api_equivalent_cost_usd": api,
                    "api_equivalent_cost_complete": api is not None,
                    "tokens": int(row.get("tokens_total") or 0),
                }
            )
        return {"session_key": item.ref.encode(), "nodes": rows}

    def session_turns(
        self,
        value: str,
        *,
        include_prompts: bool = False,
        include_content_keys: bool = False,
    ) -> dict:
        item = self.resolve_session(value)
        if (include_prompts or include_content_keys) and not self.allow_raw_content:
            raise ServiceError("raw_content_disabled", "start OpenTab with --allow-raw-content")
        fetch = getattr(item.owner, "message_timeline", None)
        rows = []
        for raw in fetch(item.workflow.id) if fetch else ():
            row = dict(raw)
            recorded = float(row.get("cost") or 0)
            api = self._detail_api_cost(
                row,
                ("input", "output", "reasoning", "cache_read", "cache_write", "cache_write_1h"),
                possibly_mixed=False,
            )
            data = {
                "time": str(row.get("time") or ""),
                "agent": str(row.get("agent") or "-"),
                "depth": int(row.get("depth") or 0),
                "model": str(row.get("model_name") or "unknown"),
                "effort": str(row.get("effort") or ""),
                "recorded_cost_usd": recorded,
                "api_equivalent_cost_usd": api,
                "api_equivalent_cost_complete": api is not None,
                "tokens": int(row.get("tokens_total") or 0),
                "input_tokens": int(row.get("input") or 0),
                "output_tokens": int(row.get("output") or 0),
                "reasoning_tokens": int(row.get("reasoning") or 0),
                "cache_read_tokens": int(row.get("cache_read") or 0),
                "cache_write_tokens": int(row.get("cache_write") or 0),
                "tools": tool_names(row.get("tools")),
                "prompt_id": str(row.get("prompt_id") or ""),
                "prompt_title": str(row.get("prompt_title") or ""),
            }
            if include_prompts:
                data["prompt"] = str(row.get("prompt_full") or row.get("prompt_title") or "")
            if include_content_keys:
                data["content_key"] = str(row.get("content_key") or "")
            rows.append(data)
        return {"session_key": item.ref.encode(), "turns": rows}

    def session_tools(self, value: str) -> dict:
        item = self.resolve_session(value)
        possibly_mixed = bool(self._costs(item)[4])
        fetch = getattr(item.owner, "tool_breakdown", None)
        rows = []
        for raw in fetch(item.workflow.id) if fetch else ():
            row = dict(raw)
            recorded = float(row.get("cost") or 0)
            api = self._detail_api_cost(
                row,
                ("input", "output", "reasoning", "cache_read", "cache_write", "cache_write_1h"),
                possibly_mixed=possibly_mixed,
            )
            tool = str(row.get("tool") or "?")
            rows.append(
                {
                    "tool": tool,
                    "namespace": tool_namespace(tool),
                    "model": str(row.get("model_name") or "unknown"),
                    "calls": int(row.get("calls") or 0),
                    "recorded_cost_usd": recorded,
                    "api_equivalent_cost_usd": api,
                    "api_equivalent_cost_complete": api is not None,
                    "tokens": int(row.get("tokens_total") or 0),
                }
            )
        return {"session_key": item.ref.encode(), "tools": rows}

    def session_context(self, value: str) -> dict:
        item = self.resolve_session(value)
        capabilities = self._capabilities(item)
        composition = []
        fetch_context = getattr(item.owner, "context_breakdown", None)
        if capabilities["context"] and fetch_context:
            composition = [
                {
                    "category": str(row.get("category") or "other"),
                    "kind": str(row.get("kind") or ""),
                    "count": int(row.get("count") or 0),
                    "estimated_tokens": int(row.get("est_tokens") or 0),
                }
                for raw in fetch_context(item.workflow.id)
                for row in [dict(raw)]
            ]
        points = []
        if capabilities["context_curve"]:
            fetch_turns = getattr(item.owner, "message_timeline", None)
            for raw in fetch_turns(item.workflow.id) if fetch_turns else ():
                row = dict(raw)
                if int(row.get("depth") or 0):
                    continue
                size = context_size(row)
                if size <= 0:
                    continue
                model = str(row.get("model_name") or "")
                points.append(
                    {
                        "time": str(row.get("time") or ""),
                        "tokens": int(size),
                        "window_tokens": model_context_window(model),
                        "cached_share": cached_share(row),
                    }
                )
        return {
            "session_key": item.ref.encode(),
            "points": points,
            "composition": composition,
        }

    def session_content(self, value: str, content_key: str) -> dict:
        if not self.allow_raw_content:
            raise ServiceError("raw_content_disabled", "start OpenTab with --allow-raw-content")
        if not content_key:
            raise ServiceError("invalid_content_key", "content_key is required")
        item = self.resolve_session(value)
        supports = getattr(item.owner, "supports_turn_content", None)
        if not supports or not supports(item.workflow.id):
            raise ServiceError("content_unavailable", "this session has no recorded raw content")
        content = item.owner.turn_content(item.workflow.id, content_key=content_key)
        return {
            "session_key": item.ref.encode(),
            "content_key": content_key,
            "content": content.get(content_key, []),
        }

    def summary(self, query: SessionQuery | None = None, *, group_by: str = "none") -> dict:
        query = query or SessionQuery(limit=MAX_LIMIT)
        rows = self._filtered(query, paginate=False)
        valid = {
            "none",
            "day",
            "month",
            "year",
            "project",
            "harness",
            "machine",
            "model",
            "provider",
        }
        if group_by not in valid:
            raise ServiceError("invalid_group", f"unknown group: {group_by}")
        if group_by in {"model", "provider"}:
            groups = self._model_groups(rows, group_by)
        elif group_by == "none":
            groups = []
        else:
            pick = {
                "day": lambda item: item.workflow.created_at[:10],
                "month": lambda item: item.workflow.created_at[:7],
                "year": lambda item: item.workflow.created_at[:4],
                "project": lambda item: item.project,
                "harness": lambda item: item.ref.harness,
                "machine": lambda item: item.ref.machine,
            }[group_by]
            buckets: dict[str, list[_Session]] = defaultdict(list)
            for item in rows:
                buckets[pick(item)].append(item)
            groups = [self._aggregate(items, key) for key, items in buckets.items()]
            groups.sort(
                key=lambda group: (group["api_equivalent_cost_usd"], group["tokens"]), reverse=True
            )
        return {
            "range": query.range,
            "totals": self._aggregate(rows),
            "group_by": group_by,
            "groups": groups,
        }

    def _aggregate(self, rows: list[_Session], key: str | None = None) -> dict:
        costs = [self._costs(item) for item in rows]
        data = {
            "sessions": len(rows),
            "recorded_cost_usd": sum(cost[0] for cost in costs),
            "api_equivalent_cost_usd": sum(cost[1] for cost in costs),
            "tokens": sum(int(item.workflow.total_tokens or 0) for item in rows),
            "unpriced_tokens": sum(cost[4] for cost in costs),
            "subagents": sum(int(item.workflow.subagents or 0) for item in rows),
            "worked_seconds": sum(float(item.workflow.worked_seconds or 0) for item in rows),
        }
        if key is not None:
            data["key"] = key
        return data

    def _model_groups(self, sessions: list[_Session], group_by: str) -> list[dict]:
        groups: dict[str, dict] = defaultdict(
            lambda: {
                "runs": 0,
                "recorded_cost_usd": 0.0,
                "api_equivalent_cost_usd": 0.0,
                "tokens": 0,
            }
        )
        for item in sessions:
            for row in self._serialize_models(item):
                key = row["model"] if group_by == "model" else row["model"].split("/", 1)[0]
                group = groups[key]
                group["runs"] += row["runs"]
                group["recorded_cost_usd"] += row["recorded_cost_usd"]
                group["api_equivalent_cost_usd"] += row["api_equivalent_cost_usd"]
                group["tokens"] += row["tokens"]
        out = [{"key": key, **value} for key, value in groups.items()]
        out.sort(key=lambda row: (row["api_equivalent_cost_usd"], row["tokens"]), reverse=True)
        return out

    def list_models(
        self,
        query: SessionQuery | None = None,
        *,
        catalog: bool = False,
        search: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict:
        limit, offset = self._bounded(limit, offset)
        _bookmarks, _ignored_projects, _ignored_sessions, pinned_models = self._state_sets()
        if catalog:
            rows = []
            for provider, model, price, status in catalog_models():
                name = f"{provider}/{model}"
                if search and search.lower() not in name.lower():
                    continue
                rows.append(
                    {
                        "model": name,
                        "input_usd_per_mtok": price[0],
                        "output_usd_per_mtok": price[1],
                        "cache_read_usd_per_mtok": price[2],
                        "cache_write_usd_per_mtok": price[3],
                        "cache_write_1h_usd_per_mtok": cache_write_1h_price(name),
                        "status": status,
                        "pinned": name in pinned_models or canonical_model(name) in pinned_models,
                    }
                )
            rows.sort(key=lambda row: row["model"])
        else:
            sessions = self._filtered(query or SessionQuery(), paginate=False)
            rows = self._model_groups(sessions, "model")
            if search:
                rows = [row for row in rows if search.lower() in row["key"].lower()]
            for row in rows:
                name = row.pop("key")
                price = model_price(name)
                row.update(
                    {
                        "model": name,
                        "known_price": has_known_price(name),
                        "local": is_local_provider(name),
                        "input_usd_per_mtok": price[0],
                        "output_usd_per_mtok": price[1],
                        "cache_read_usd_per_mtok": price[2],
                        "cache_write_usd_per_mtok": price[3],
                        "cache_write_1h_usd_per_mtok": cache_write_1h_price(name),
                        "pinned": name in pinned_models or canonical_model(name) in pinned_models,
                    }
                )
        total = len(rows)
        return {
            "models": rows[offset : offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def compare_model(self, value: str, target_model: str) -> dict:
        item = self.resolve_session(value)
        rows = self._model_rows(item)
        if not rows:
            raise ServiceError("model_data_unavailable", "this session has no per-model token data")
        target_price = model_price(target_model)
        if not has_known_price(target_model) or is_local_provider(target_model):
            raise ServiceError(
                "unknown_model_price", f"no comparable list price for {target_model}"
            )
        baseline = target = 0.0
        estimated = False
        for row in rows:
            split = model_row_split(row)
            name = str(row.get("model_name") or "")
            if not is_local_provider(name):
                baseline += api_equivalent_cost(name, *split, model_row_1h_write(row))
                estimated = estimated or not has_known_price(name)
            target += api_equivalent_cost(target_model, *split, model_row_1h_write(row))
        return {
            "session_key": item.ref.encode(),
            "target_model": target_model,
            "actual_models_list_cost_usd": baseline,
            "target_model_list_cost_usd": target,
            "change_usd": target - baseline,
            "change_fraction": (target - baseline) / baseline if baseline else None,
            "baseline_estimated": estimated,
            "target_rates": {
                "input_usd_per_mtok": target_price[0],
                "output_usd_per_mtok": target_price[1],
                "cache_read_usd_per_mtok": target_price[2],
                "cache_write_usd_per_mtok": target_price[3],
            },
        }

    def list_sources(self) -> dict:
        present = sources.available_sources(self.args)
        return {
            "selected": self.source_key,
            "sources": [
                {
                    "harness": key,
                    "label": sources.SOURCE_LABELS.get(key, key),
                    "present": key in present,
                }
                for key in sources.SOURCE_LABELS
                if key != "all"
            ],
        }

    def get_note(self, value: str) -> dict:
        item = self.resolve_session(value)
        if not self.use_state:
            return {"session_key": item.ref.encode(), "note": ""}
        notes, readable = read_notes()
        if not readable:
            raise ServiceError("notes_unreadable", "notes file is malformed or unreadable")
        return {"session_key": item.ref.encode(), "note": self._session_note(item, notes)}

    def set_note(self, value: str, text: str) -> dict:
        if not self.use_state:
            raise ServiceError("state_disabled", "notes are unavailable with --no-state")
        if not isinstance(text, str):
            raise ServiceError("invalid_note", "note text must be a string")
        if len(text) > 500:
            raise ServiceError("note_too_long", "note text exceeds 500 characters", {"limit": 500})
        item = self.resolve_session(value)
        unique = len(self._by_native.get(item.workflow.id, ())) == 1
        notes, error = update_note(
            item.workflow.id if unique else item.ref.encode(),
            text,
            qualified_id=item.ref.encode() if unique else None,
        )
        if error:
            raise ServiceError(f"notes_{error}", f"notes file is {error}")
        return {"session_key": item.ref.encode(), "note": self._session_note(item, notes)}

    def mutate_set(self, resource: str, operation: str, value: str) -> dict:
        if not self.use_state:
            raise ServiceError("state_disabled", "preferences are unavailable with --no-state")
        key_by_resource = {
            "bookmark": "bookmarks",
            "ignored-session": "ignored_sessions",
            "ignored-project": "ignored_projects",
            "pinned-model": "pinned_models",
        }
        key = key_by_resource.get(resource)
        if key is None:
            raise ServiceError("invalid_resource", f"unknown preference set: {resource}")
        if operation not in {"add", "remove"}:
            raise ServiceError("invalid_operation", "operation must be add or remove")
        if not isinstance(value, str) or not value:
            raise ServiceError("invalid_value", "value must be a nonempty string")
        qualified_value = None
        if resource in {"bookmark", "ignored-session"}:
            item = self.resolve_session(value)
            value = item.ref.encode()
            if len(self._by_native.get(item.workflow.id, ())) == 1:
                qualified_value = value
                value = item.workflow.id
        elif resource == "ignored-project":
            value = resolve_project_root(git_root(os.path.expanduser(value)))
        state, error = update_state(
            "set-add" if operation == "add" else "set-remove",
            key,
            value,
            qualified_value=qualified_value,
        )
        if error:
            raise ServiceError(f"state_{error.replace(' ', '_')}", f"state update failed: {error}")
        return {"resource": resource, "values": state.get(key, [])}

    def list_preferences(self) -> dict:
        if not self.use_state:
            return {
                "bookmarks": [],
                "ignored_projects": [],
                "ignored_sessions": [],
                "pinned_models": [],
            }
        state = load_state()
        return {
            key: state.get(key, [])
            for key in ("bookmarks", "ignored_projects", "ignored_sessions", "pinned_models")
        }
