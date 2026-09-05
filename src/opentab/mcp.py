"""Dependency-free Model Context Protocol server over stdio."""
from __future__ import annotations

import json
import sys

from opentab import __version__
from opentab.service import OpenTabService, ServiceError, SessionQuery

SERVER_NAME = "opentab"
MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")

_QUERY_PROPERTIES = {
    "range": {"type": "string", "description": "all, 30d, 2m, YYYY, YYYY-MM, or START..END"},
    "project": {"type": "string"},
    "harness": {"type": "string"},
    "machine": {"type": "string"},
    "model": {"type": "string"},
    "search": {"type": "string"},
    "bookmarked": {"type": "boolean"},
    "include_ignored": {"type": "boolean"},
    "sort": {
        "type": "string",
        "enum": ["cost", "tokens", "date", "last_activity", "title", "project"],
    },
    "reverse": {"type": "boolean"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
    "offset": {"type": "integer", "minimum": 0},
}


def _schema(properties=None, required=None) -> dict:
    out = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


def _session_schema(extra=None, required=None) -> dict:
    props = {"session": {"type": "string", "description": "session_key or unique native id"}}
    props.update(extra or {})
    return _schema(props, ["session", *(required or [])])


TOOLS = (
    {
        "name": "opentab_usage_summary",
        "description": "Summarize local AI coding usage and optionally group it.",
        "inputSchema": _schema(
            {
                **_QUERY_PROPERTIES,
                "group_by": {
                    "type": "string",
                    "enum": [
                        "none",
                        "day",
                        "month",
                        "year",
                        "project",
                        "harness",
                        "machine",
                        "model",
                        "provider",
                    ],
                },
            }
        ),
    },
    {
        "name": "opentab_list_sessions",
        "description": "List and filter sessions. Returns qualified session_key values for detail calls.",
        "inputSchema": _schema(_QUERY_PROPERTIES),
    },
    {
        "name": "opentab_get_session",
        "description": "Get one session's totals, capabilities, note, and model usage.",
        "inputSchema": _session_schema(),
    },
    {
        "name": "opentab_get_session_nodes",
        "description": "Get one session's recursive root and subagent accounting tree.",
        "inputSchema": _session_schema(),
    },
    {
        "name": "opentab_get_session_turns",
        "description": "Get per-turn accounting. Full prompts and content keys require a server started with --allow-raw-content and an explicit request.",
        "inputSchema": _session_schema(
            {
                "include_prompts": {"type": "boolean"},
                "include_content_keys": {"type": "boolean"},
            }
        ),
    },
    {
        "name": "opentab_get_session_tools",
        "description": "Get tool and MCP-server call, token, and cost attribution for one session.",
        "inputSchema": _session_schema(),
    },
    {
        "name": "opentab_get_session_context",
        "description": "Get measured context-window growth and estimated composition for one session.",
        "inputSchema": _session_schema(),
    },
    {
        "name": "opentab_get_session_content",
        "description": "Read raw local prompt, reasoning, command, and tool output. Disabled unless the server was started with --allow-raw-content.",
        "inputSchema": _session_schema(
            {"content_key": {"type": "string"}, "confirm_raw": {"type": "boolean"}},
            ["content_key", "confirm_raw"],
        ),
    },
    {
        "name": "opentab_list_models",
        "description": "List models used in matching sessions or browse the bundled price catalog.",
        "inputSchema": _schema(
            {
                **_QUERY_PROPERTIES,
                "catalog": {"type": "boolean"},
                "catalog_search": {"type": "string"},
            }
        ),
    },
    {
        "name": "opentab_compare_model",
        "description": "Compare one session's actual model mix with running all its tokens at another model's list rates.",
        "inputSchema": _session_schema({"target_model": {"type": "string"}}, ["target_model"]),
    },
    {
        "name": "opentab_get_note",
        "description": "Read the authored note for one session.",
        "inputSchema": _session_schema(),
    },
    {
        "name": "opentab_set_note",
        "description": "Set an authored session note. An empty text deletes it.",
        "inputSchema": _session_schema({"text": {"type": "string", "maxLength": 500}}, ["text"]),
    },
    {
        "name": "opentab_list_preferences",
        "description": "List bookmarks, ignored sessions/projects, and pinned models.",
        "inputSchema": _schema(),
    },
    {
        "name": "opentab_update_preference",
        "description": "Add or remove a bookmark, ignored session/project, or pinned model.",
        "inputSchema": _schema(
            {
                "resource": {
                    "type": "string",
                    "enum": ["bookmark", "ignored-session", "ignored-project", "pinned-model"],
                },
                "operation": {"type": "string", "enum": ["add", "remove"]},
                "value": {"type": "string"},
            },
            ["resource", "operation", "value"],
        ),
    },
    {
        "name": "opentab_list_sources",
        "description": "List harnesses and whether their local records are present.",
        "inputSchema": _schema(),
    },
    {
        "name": "opentab_reload",
        "description": "Reload changed local harness records in this long-lived MCP process.",
        "inputSchema": _schema(),
    },
)

_TOOL_NAMES = {tool["name"] for tool in TOOLS}
_TOOL_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}


class McpServer:
    def __init__(self, args, service: OpenTabService | None = None):
        self.args = args
        self._service = service

    @property
    def service(self) -> OpenTabService:
        if self._service is None:
            self._service = OpenTabService.open(
                self.args,
                allow_raw_content=bool(getattr(self.args, "allow_raw_content", False)),
            )
        return self._service

    @staticmethod
    def _error(request_id, code: int, message: str, data=None) -> dict:
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        response = {"jsonrpc": "2.0", "error": error}
        if request_id is not None:
            response["id"] = request_id
        return response

    @staticmethod
    def _success(request_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _modern(request: dict) -> tuple[bool, str | None]:
        params = request.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(meta, dict):
            return False, None
        version = meta.get("io.modelcontextprotocol/protocolVersion")
        return version is not None, version if isinstance(version, str) else None

    @staticmethod
    def _modern_result(result: dict) -> dict:
        return {
            **result,
            "resultType": "complete",
            "_meta": {
                "io.modelcontextprotocol/serverInfo": {
                    "name": SERVER_NAME,
                    "version": __version__,
                }
            },
        }

    def handle(self, request) -> dict | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        method = request.get("method")
        is_notification = "id" not in request
        request_id = request.get("id")
        if not isinstance(method, str) or (
            not is_notification
            and (
                request_id is None
                or isinstance(request_id, bool)
                or not isinstance(request_id, (str, int))
            )
        ):
            return None if is_notification else self._error(None, -32600, "Invalid Request")
        if is_notification:
            return None
        modern, version = self._modern(request)
        if modern and version != MODERN_VERSION:
            return self._error(
                request_id,
                -32022,
                "Unsupported protocol version",
                {"supported": [MODERN_VERSION], "requested": version},
            )
        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params")
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                selected = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
                return self._success(
                    request_id,
                    {
                        "protocolVersion": selected,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": SERVER_NAME, "version": __version__},
                    },
                )
            if method == "server/discover" and modern:
                return self._success(
                    request_id,
                    self._modern_result(
                        {"supportedVersions": [MODERN_VERSION], "capabilities": {"tools": {}}}
                    ),
                )
            if method == "ping" and not modern:
                return self._success(request_id, {})
            if method == "tools/list":
                result = {"tools": list(TOOLS)}
                return self._success(request_id, self._modern_result(result) if modern else result)
            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments", {})
                if name not in _TOOL_NAMES or not isinstance(arguments, dict):
                    return self._error(request_id, -32602, "Unknown tool or invalid arguments")
                result = self.call_tool(name, arguments)
                return self._success(request_id, self._modern_result(result) if modern else result)
            return self._error(request_id, -32601, "Method not found")
        except ServiceError as exc:
            data = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
            if exc.details:
                data["error"]["details"] = exc.details
            return self._success(request_id, self._tool_result(data, is_error=True, modern=modern))
        except (OSError, ValueError, SystemExit) as exc:
            data = {"ok": False, "error": {"code": "operation_failed", "message": str(exc)}}
            return self._success(request_id, self._tool_result(data, is_error=True, modern=modern))
        except Exception:  # noqa: BLE001 -- never leak a traceback or kill the protocol loop
            return self._error(request_id, -32603, "Internal error")

    @staticmethod
    def _tool_result(data: dict, *, is_error: bool = False, modern: bool = False) -> dict:
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(data, ensure_ascii=False, allow_nan=False),
                }
            ],
            "structuredContent": data,
            "isError": is_error,
        }
        return McpServer._modern_result(result) if modern else result

    @staticmethod
    def _query(arguments: dict) -> SessionQuery:
        fields = set(SessionQuery.__dataclass_fields__)
        unknown = set(arguments) - fields - {"group_by", "catalog", "catalog_search"}
        if unknown:
            raise ServiceError(
                "invalid_arguments", f"unknown arguments: {', '.join(sorted(unknown))}"
            )
        values = {key: value for key, value in arguments.items() if key in fields}
        try:
            return SessionQuery(**values)
        except TypeError as exc:
            raise ServiceError("invalid_arguments", str(exc)) from exc

    @staticmethod
    def _require(arguments: dict, name: str, kind=str):
        value = arguments.get(name)
        if not isinstance(value, kind) or (kind is str and not value):
            raise ServiceError("invalid_arguments", f"{name} is required")
        return value

    @staticmethod
    def _validate_arguments(name: str, arguments: dict) -> None:
        schema = _TOOL_SCHEMAS[name]
        properties = schema["properties"]
        unknown = set(arguments) - set(properties)
        if unknown:
            raise ServiceError(
                "invalid_arguments", f"unknown arguments: {', '.join(sorted(unknown))}"
            )
        missing = [key for key in schema.get("required", ()) if key not in arguments]
        if missing:
            raise ServiceError("invalid_arguments", f"missing arguments: {', '.join(missing)}")
        kinds = {"string": str, "boolean": bool, "integer": int}
        for key, value in arguments.items():
            rule = properties[key]
            kind = kinds.get(rule.get("type"))
            if kind is not None and (
                not isinstance(value, kind) or (kind is int and isinstance(value, bool))
            ):
                raise ServiceError("invalid_arguments", f"{key} must be a {rule['type']}")
            if "enum" in rule and value not in rule["enum"]:
                raise ServiceError("invalid_arguments", f"{key} has an unsupported value")
            if isinstance(value, str) and "maxLength" in rule and len(value) > rule["maxLength"]:
                raise ServiceError("invalid_arguments", f"{key} is too long")
            if isinstance(value, int) and not isinstance(value, bool):
                if "minimum" in rule and value < rule["minimum"]:
                    raise ServiceError("invalid_arguments", f"{key} is below its minimum")
                if "maximum" in rule and value > rule["maximum"]:
                    raise ServiceError("invalid_arguments", f"{key} is above its maximum")

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name not in _TOOL_SCHEMAS:
            raise ServiceError("unknown_tool", f"unknown tool: {name}")
        self._validate_arguments(name, arguments)
        if name == "opentab_usage_summary":
            data = self.service.summary(
                self._query(arguments), group_by=str(arguments.get("group_by") or "none")
            )
        elif name == "opentab_list_sessions":
            data = self.service.list_sessions(self._query(arguments))
        elif name == "opentab_get_session":
            data = self.service.get_session(self._require(arguments, "session"))
        elif name == "opentab_get_session_nodes":
            data = self.service.session_nodes(self._require(arguments, "session"))
        elif name == "opentab_get_session_turns":
            data = self.service.session_turns(
                self._require(arguments, "session"),
                include_prompts=bool(arguments.get("include_prompts", False)),
                include_content_keys=bool(arguments.get("include_content_keys", False)),
            )
        elif name == "opentab_get_session_tools":
            data = self.service.session_tools(self._require(arguments, "session"))
        elif name == "opentab_get_session_context":
            data = self.service.session_context(self._require(arguments, "session"))
        elif name == "opentab_get_session_content":
            if arguments.get("confirm_raw") is not True:
                raise ServiceError("raw_content_confirmation_required", "confirm_raw must be true")
            data = self.service.session_content(
                self._require(arguments, "session"), self._require(arguments, "content_key")
            )
        elif name == "opentab_list_models":
            query = self._query(arguments)
            data = self.service.list_models(
                query,
                catalog=bool(arguments.get("catalog", False)),
                search=arguments.get("catalog_search"),
                limit=query.limit,
                offset=query.offset,
            )
        elif name == "opentab_compare_model":
            data = self.service.compare_model(
                self._require(arguments, "session"), self._require(arguments, "target_model")
            )
        elif name == "opentab_get_note":
            data = self.service.get_note(self._require(arguments, "session"))
        elif name == "opentab_set_note":
            text = arguments.get("text")
            if not isinstance(text, str):
                raise ServiceError("invalid_arguments", "text must be a string")
            data = self.service.set_note(self._require(arguments, "session"), text)
        elif name == "opentab_list_preferences":
            if arguments:
                raise ServiceError("invalid_arguments", "this tool takes no arguments")
            data = self.service.list_preferences()
        elif name == "opentab_update_preference":
            data = self.service.mutate_set(
                self._require(arguments, "resource"),
                self._require(arguments, "operation"),
                self._require(arguments, "value"),
            )
        elif name == "opentab_list_sources":
            if arguments:
                raise ServiceError("invalid_arguments", "this tool takes no arguments")
            data = self.service.list_sources()
        elif name == "opentab_reload":
            if arguments:
                raise ServiceError("invalid_arguments", "this tool takes no arguments")
            self.service.reload()
            data = {"reloaded": True, "sessions": len(self.service._sessions)}
        else:
            raise ServiceError("unknown_tool", f"unknown tool: {name}")
        return self._tool_result({"ok": True, "data": data})


def run_server(args, stdin=None, stdout=None) -> int:
    server = McpServer(args)
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    for line in inp:
        try:
            request = json.loads(line)
        except ValueError:
            response = server._error(None, -32700, "Parse error")
        else:
            response = server.handle(request)
        if response is not None:
            out.write(
                json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                + "\n"
            )
            out.flush()
    return 0
