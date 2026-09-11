import io
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import Mock, patch

import opentab as ot
from opentab.conversation import ConversationError
from opentab.mcp import LEGACY_VERSIONS, MODERN_VERSION, McpServer, run_server

from tests._support import _write_jsonl


class FakeService:
    def summary(self, query, group_by="none"):
        return {"range": query.range, "group_by": group_by}

    def get_session(self, value):
        if value == "gone":
            raise ot.ServiceError("session_not_found", "gone")
        return {"session_key": value}


def _args():
    return type("Args", (), {"allow_raw_content": False})()


def _request(method, params=None, request_id=1):
    out = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        out["params"] = params
    return out


def test_mcp_legacy_initialize_lists_tools_and_calls_the_shared_service():
    server = McpServer(_args(), FakeService())
    initialized = server.handle(
        _request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    )
    assert initialized["result"]["protocolVersion"] == "2025-06-18"
    fallback = server.handle(_request("initialize", {"protocolVersion": "old"}))
    assert fallback["result"]["protocolVersion"] == LEGACY_VERSIONS[0]

    listed = server.handle(_request("tools/list"))
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "opentab_usage_summary" in names and "opentab_get_session_content" in names
    summary = next(
        tool for tool in listed["result"]["tools"] if tool["name"] == "opentab_usage_summary"
    )
    assert "session start dates, not individual call dates" in summary["description"]
    props = summary["inputSchema"]["properties"]
    assert "since that date" in props["range"]["description"]
    assert "other models' usage" in props["model"]["description"]
    assert "Ignored for summaries" in props["limit"]["description"]

    called = server.handle(
        _request(
            "tools/call",
            {"name": "opentab_usage_summary", "arguments": {"range": "30d", "group_by": "day"}},
        )
    )
    result = called["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["data"] == {"range": "30d", "group_by": "day"}


def test_mcp_domain_errors_are_tool_errors_and_bad_methods_are_protocol_errors():
    server = McpServer(_args(), FakeService())
    failed = server.handle(
        _request("tools/call", {"name": "opentab_get_session", "arguments": {"session": "gone"}})
    )
    assert failed["result"]["isError"] is True
    assert failed["result"]["structuredContent"]["error"]["code"] == "session_not_found"
    invalid = server.handle(
        _request("tools/call", {"name": "opentab_list_sessions", "arguments": {"limit": True}})
    )
    assert invalid["result"]["isError"] is True
    assert invalid["result"]["structuredContent"]["error"]["code"] == "invalid_arguments"
    too_long = server.handle(
        _request(
            "tools/call",
            {"name": "opentab_set_note", "arguments": {"session": "a", "text": "x" * 501}},
        )
    )
    assert too_long["result"]["isError"] is True
    assert too_long["result"]["structuredContent"]["error"]["code"] == "invalid_arguments"
    assert server.handle(_request("no/such/method"))["error"]["code"] == -32601
    assert server.handle(_request("tools/call", {"name": "nope"}))["error"]["code"] == -32602
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_modern_discovery_and_tools_add_completion_metadata():
    server = McpServer(_args(), FakeService())
    meta = {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    }
    discovered = server.handle(_request("server/discover", meta))
    assert discovered["result"]["resultType"] == "complete"
    assert discovered["result"]["supportedVersions"] == [MODERN_VERSION]
    listed = server.handle(_request("tools/list", meta))
    assert listed["result"]["resultType"] == "complete"
    wrong = {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2099-01-01",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    }
    assert server.handle(_request("tools/list", wrong))["error"]["code"] == -32022


def test_mcp_stdio_is_newline_json_and_survives_parse_errors():
    lines = [
        "not json",
        json.dumps(_request("ping", request_id="p")),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ]
    inp, out = io.StringIO("\n".join(lines) + "\n"), io.StringIO()
    assert run_server(_args(), inp, out) == 0
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [response.get("id") for response in responses] == [None, "p"]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["result"] == {}


def test_mcp_turns_nonfinite_source_numbers_into_a_valid_tool_error():
    class NonFinite(FakeService):
        def summary(self, query, group_by="none"):
            return {"cost": float("nan")}

    response = McpServer(_args(), NonFinite()).handle(
        _request("tools/call", {"name": "opentab_usage_summary", "arguments": {}})
    )
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["error"]["code"] == "operation_failed"
    assert "NaN" not in json.dumps(response, allow_nan=False)


def test_mcp_stdio_queries_a_real_store_and_persists_authored_mutations():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-09-01T12:00:00Z",
                    "session_id": "s1",
                    "title": "fix login form",
                    "model": "anthropic/claude-opus-4-5",
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                }
            ],
        )
        calls = [
            ("opentab_usage_summary", {}),
            ("opentab_list_models", {"catalog_search": "opus"}),
            (
                "opentab_update_preference",
                {"resource": "bookmark", "operation": "add", "value": "s1"},
            ),
            ("opentab_set_note", {"session": "s1", "text": "investigate cache churn"}),
            ("opentab_get_session", {"session": "s1"}),
        ]
        requests = [
            _request("initialize", {"protocolVersion": "2025-06-18"}, request_id=0),
            *[
                _request("tools/call", {"name": name, "arguments": arguments}, request_id=i)
                for i, (name, arguments) in enumerate(calls, 1)
            ],
        ]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "opentab",
                "mcp",
                "--harness",
                "jsonl",
                "--jsonl",
                path,
                "--no-cache",
            ],
            input="".join(json.dumps(request) + "\n" for request in requests),
            text=True,
            capture_output=True,
            cwd=tmp,
            timeout=30,
            env={
                **os.environ,
                "PYTHONPATH": os.path.dirname(os.path.dirname(ot.__file__)),
                "XDG_STATE_HOME": tmp,
                "XDG_DATA_HOME": tmp,
            },
        )
        assert result.returncode == 0, result.stderr
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        assert [response["id"] for response in responses] == list(range(len(requests)))
        results = [response["result"] for response in responses[1:]]
        assert all(not response["isError"] for response in results), results
        data = [response["structuredContent"]["data"] for response in results]
        assert data[0]["totals"]["unpriced_tokens"] == 1_000_000
        assert data[0]["totals"]["api_equivalent_cost_usd"] == 5
        assert data[0]["totals"]["input_tokens"] == 1_000_000
        assert data[0]["totals"]["token_breakdown_complete"] is True
        assert data[0]["date_scope"]["basis"] == "root_session_created_at_date"
        assert data[0]["scope"]["harnesses"] == ["jsonl"]
        assert data[0]["scope"]["saved_ignores_applied"] is True
        assert "not a verified invoice" in data[0]["accounting"]["recorded_cost_usd"]
        assert json.loads(results[0]["content"][0]["text"])["data"] == data[0]
        assert data[1]["total"] == 1
        assert data[2]["values"] == ["s1"]
        assert data[4]["bookmarked"] is True
        assert data[4]["note"] == "investigate cache churn"
        assert ot.load_state(os.path.join(tmp, "opentab", "state.json"))["bookmarks"] == ["s1"]
        notes, readable = ot.read_notes(os.path.join(tmp, "opentab", "notes.json"))
        assert readable and notes == {"s1": "investigate cache churn"}


def test_conversation_mcp_lists_bounded_schema_without_opening_service():
    with patch.object(ot.OpenTabService, "open") as opened:
        tools = McpServer(_args()).handle(_request("tools/list"))["result"]["tools"]
        opened.assert_not_called()
    tool = next(tool for tool in tools if tool["name"] == "opentab_get_session_conversation")
    schema = tool["inputSchema"]
    assert schema["required"] == ["session", "confirm_raw"]
    props = schema["properties"]
    assert props["confirm_raw"]["enum"] == [True]
    for key, low, high, default in (
        ("limit", 1, 100, 20),
        ("max_chars", 1, 120000, 20000),
        ("before", 0, 99, 0),
    ):
        assert props[key] == {
            "type": "integer",
            "minimum": low,
            "maximum": high,
            "default": default,
        }
    assert props["anchor"]["maxLength"] == 1024 and props["cursor"]["maxLength"] == 8192
    assert "response.executions" in tool["description"] and "root only" in tool["description"]


def test_conversation_mcp_confirmation_precedes_lazy_service_and_argument_validation():
    server = McpServer(_args())
    for confirmation in (
        {},
        {"confirm_raw": False},
        {"confirm_raw": 1},
        {"confirm_raw": "true"},
        {"confirm_raw": None},
    ):
        with patch.object(
            ot.OpenTabService, "open", side_effect=AssertionError("lazy open")
        ) as opened:
            result = server.handle(
                _request(
                    "tools/call",
                    {
                        "name": "opentab_get_session_conversation",
                        "arguments": {"session": "missing", "limit": False, **confirmation},
                    },
                )
            )["result"]
            assert result["isError"]
            assert (
                result["structuredContent"]["error"]["code"] == "raw_content_confirmation_required"
            )
            opened.assert_not_called()


def test_conversation_mcp_rejects_invalid_types_and_bounds_before_lazy_service():
    server = McpServer(_args())
    for key, values in (
        ("limit", (True, "20", 0, 101, 1.5)),
        ("max_chars", (True, "10", 0, 120001)),
        ("before", (True, "1", -1, 100)),
        ("tail", (1, "true", None)),
        ("anchor", (False, "", "a" * 1025)),
        ("cursor", (False, "", "c" * 8193)),
        ("execution_id", (False, "", 1, [], None)),
        ("session", (False, 1)),
        ("unexpected", ("field",)),
    ):
        for value in values:
            with patch.object(
                ot.OpenTabService, "open", side_effect=AssertionError("lazy open")
            ) as opened:
                result = server.handle(
                    _request(
                        "tools/call",
                        {
                            "name": "opentab_get_session_conversation",
                            "arguments": {"session": "root", "confirm_raw": True, key: value},
                        },
                    )
                )["result"]
                assert result["isError"], (key, value)
                assert result["structuredContent"]["error"]["code"] == "invalid_arguments"
                opened.assert_not_called()


def test_conversation_mcp_forwards_defaults_and_all_selectors_explicitly():
    defaults = dict(
        execution_id=None, anchor=None, cursor=None, limit=20, max_chars=20000, before=0, tail=False
    )
    for options in (
        {},
        {"execution_id": "child", "anchor": "a", "before": 2, "limit": 7, "max_chars": 99},
        {"cursor": "opaque"},
        {"tail": True},
    ):
        service = Mock()
        service.session_conversation.return_value = {"records": [], "executions": [{"id": "child"}]}
        response = McpServer(_args(), service).handle(
            _request(
                "tools/call",
                {
                    "name": "opentab_get_session_conversation",
                    "arguments": {"session": "qualified-root", "confirm_raw": True, **options},
                },
            )
        )
        result = response["result"]
        assert not result["isError"]
        assert result["structuredContent"]["data"] == service.session_conversation.return_value
        assert (
            json.loads(result["content"][0]["text"])["data"]
            == service.session_conversation.return_value
        )
        service.session_conversation.assert_called_once_with(
            "qualified-root", **{**defaults, **options}
        )
        service.session_content.assert_not_called()


def test_conversation_mcp_still_requires_process_permission_and_translates_window_errors():
    from tests._support import workflow
    from tests.test_service import ConversationStore

    store = ConversationStore([workflow("root", "2026-09-01 12:00:00")])
    service = ot.OpenTabService(store, _args())
    server = McpServer(_args(), service)
    arguments = {"session": "root", "confirm_raw": True}
    result = server.handle(
        _request("tools/call", {"name": "opentab_get_session_conversation", "arguments": arguments})
    )["result"]
    assert result["structuredContent"]["error"]["code"] == "raw_content_disabled"
    service.allow_raw_content = True
    for options in (
        {"anchor": "a", "tail": True},
        {"cursor": "c", "tail": True},
        {"anchor": "a", "cursor": "c"},
        {"before": 1},
    ):
        result = server.handle(
            _request(
                "tools/call",
                {"name": "opentab_get_session_conversation", "arguments": {**arguments, **options}},
            )
        )["result"]
        assert result["isError"] and result["structuredContent"]["error"]["code"].startswith(
            "invalid"
        )
    assert store.reads == store.probes == []


def test_conversations_mcp_lists_bounded_tools_without_service_or_index_creation():
    helper = Mock()
    with (
        patch.object(ot, "conversation_search", helper, create=True),
        patch.object(ot.OpenTabService, "open") as opened,
    ):
        tools = McpServer(_args()).handle(_request("tools/list"))["result"]["tools"]
    opened.assert_not_called()
    assert helper.mock_calls == []
    tools = {tool["name"]: tool for tool in tools}
    assert not any("clear" in name for name in tools)
    search = tools["opentab_search_conversations"]
    index = tools["opentab_index_conversations"]
    status = tools["opentab_conversation_index_status"]
    assert index["annotations"]["readOnlyHint"] is False
    assert search["annotations"]["readOnlyHint"] is status["annotations"]["readOnlyHint"] is True
    assert "persistent local plaintext" in index["description"]
    assert "No network" in index["description"] and "embeddings" in index["description"]
    assert index["inputSchema"]["required"] == ["confirm_index"]
    assert search["inputSchema"]["required"] == ["query", "confirm_raw"]
    assert status["inputSchema"]["properties"] == {}
    for tool in (search, index, status):
        assert tool["inputSchema"]["additionalProperties"] is False
    props = search["inputSchema"]["properties"]
    assert "search" not in props and "range" not in props and "include_ignored" not in props
    for key in ("project", "harness", "machine", "session", "exclude_session"):
        assert props[key]["minLength"] == 1 and props[key]["maxLength"] == 4096
    assert props["query"] == {"type": "string", "minLength": 1, "maxLength": 1000}
    for key in ("since", "until"):
        assert props[key]["format"] == "date"
        assert "message date" in props[key]["description"]
    for key, high, default in (("limit", 100, 10), ("max_chars", 120000, 6000)):
        assert props[key] == {"type": "integer", "minimum": 1, "maximum": high, "default": default}


def test_conversations_mcp_confirmation_precedes_lazy_service_and_validation():
    for name, confirmation, code in (
        ("opentab_search_conversations", "confirm_raw", "raw_content_confirmation_required"),
        ("opentab_index_conversations", "confirm_index", "index_confirmation_required"),
    ):
        for value in (
            {},
            {confirmation: False},
            {confirmation: "true"},
            {confirmation: 1},
            {confirmation: None},
        ):
            with patch.object(ot.OpenTabService, "open") as opened:
                result = McpServer(_args()).handle(
                    _request(
                        "tools/call",
                        {
                            "name": name,
                            "arguments": {"unexpected": True, **value},
                        },
                    )
                )["result"]
            opened.assert_not_called()
            assert result["isError"]
            assert result["structuredContent"]["error"]["code"] == code


def test_conversations_mcp_rejects_bad_arguments_before_lazy_service():
    search = {"query": "synthetic evidence", "confirm_raw": True}
    index = {"confirm_index": True}
    invalid = [
        ("opentab_search_conversations", {"confirm_raw": True}),
        ("opentab_conversation_index_status", {"query": "synthetic"}),
    ]
    for key, values in (
        ("query", (False, "", "q" * 1001, None)),
        ("limit", (True, "10", 0, 101, 1.5)),
        ("max_chars", (True, "6000", 0, 120001)),
        ("since", (True, "", "2026-9-1", "2026-09-01T00:00:00")),
        ("until", (None, 20260911)),
        ("exclude_session", (False, "", "s" * 4097)),
        ("search", ("title",)),
        ("range", ("30d",)),
    ):
        invalid += [("opentab_search_conversations", {**search, key: value}) for value in values]
    for name, base in (
        ("opentab_search_conversations", search),
        ("opentab_index_conversations", index),
    ):
        for key in ("project", "harness", "machine", "session"):
            for value in (False, "", "s" * 4097, None):
                invalid.append((name, {**base, key: value}))
        invalid.append((name, {**base, "include_ignored": True}))
        invalid.append((name, {**base, "unexpected": "field"}))
    for value in ("true", 1, None):
        invalid.append(("opentab_index_conversations", {**index, "rebuild": value}))
    for name, arguments in invalid:
        with patch.object(ot.OpenTabService, "open") as opened:
            result = McpServer(_args()).handle(
                _request(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": arguments,
                    },
                )
            )["result"]
        opened.assert_not_called()
        assert result["isError"], (name, arguments)
        assert result["structuredContent"]["error"]["code"] == "invalid_arguments"


def test_conversations_mcp_forwards_exact_service_arguments_and_open_permission():
    scope = dict(project=None, harness=None, machine=None, session=None)
    for action in ("index", "search"):
        for scoped in (False, True):
            expected = dict(scope)
            arguments = {"confirm_index" if action == "index" else "confirm_raw": True}
            if scoped:
                arguments.update(
                    project="/synthetic/project", harness="claude", machine="box", session="root"
                )
                expected.update(
                    project="/synthetic/project", harness="claude", machine="box", session="root"
                )
            if action == "index":
                expected["rebuild"] = scoped
                if scoped:
                    arguments["rebuild"] = True
            else:
                arguments["query"] = "synthetic evidence"
                expected.update(
                    exclude_session=None, since=None, until=None, limit=10, max_chars=6000
                )
                if scoped:
                    options = dict(
                        exclude_session="current",
                        since="2026-09-01",
                        until="2026-09-11",
                        limit=7,
                        max_chars=99,
                    )
                    arguments.update(options)
                    expected.update(options)
            args = _args()
            args.allow_raw_content = True
            service = Mock(spec=["index_conversations", "search_conversations"])
            method = getattr(service, f"{action}_conversations")
            method.return_value = {"synthetic": True}
            with (
                patch.object(ot.OpenTabService, "open", return_value=service) as opened,
                patch.object(McpServer, "_query", side_effect=AssertionError("session query")),
            ):
                result = McpServer(args).handle(
                    _request(
                        "tools/call",
                        {
                            "name": f"opentab_{action}_conversations",
                            "arguments": arguments,
                        },
                    )
                )["result"]
            opened.assert_called_once_with(args, allow_raw_content=True)
            method.assert_called_once_with(
                *(["synthetic evidence"] if action == "search" else []), **expected
            )
            assert not result["isError"]
            assert result["structuredContent"] == {"ok": True, "data": {"synthetic": True}}
            assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_conversations_mcp_service_gate_and_shared_validation_errors_are_tool_errors():
    for action in ("index", "search"):
        arguments = {"confirm_index" if action == "index" else "confirm_raw": True}
        if action == "search":
            arguments.update(query="synthetic evidence", since="2026-02-30")
        for allowed in (False, True):
            args = _args()
            args.allow_raw_content = allowed
            service = Mock(spec=["index_conversations", "search_conversations"])
            method = getattr(service, f"{action}_conversations")
            method.side_effect = (
                ConversationError("invalid_date", "synthetic validation error")
                if allowed
                else ot.ServiceError("raw_content_disabled", "process permission required")
            )
            with patch.object(ot.OpenTabService, "open", return_value=service) as opened:
                result = McpServer(args).handle(
                    _request(
                        "tools/call",
                        {
                            "name": f"opentab_{action}_conversations",
                            "arguments": arguments,
                        },
                    )
                )["result"]
            opened.assert_called_once_with(args, allow_raw_content=allowed)
            method.assert_called_once()
            assert result["isError"]
            assert result["structuredContent"]["error"]["code"] == (
                "invalid_date" if allowed else "raw_content_disabled"
            )


def test_conversations_mcp_status_is_dynamic_counts_only_without_discovery():
    server = McpServer(_args())
    for result_value in ({"exists": False, "records": 0}, {"exists": True, "records": 3}):
        helper = Mock(spec=["index_status"])
        helper.index_status.return_value = result_value
        with (
            patch.object(ot, "conversation_search", helper, create=True),
            patch.object(
                ot.OpenTabService, "open", side_effect=AssertionError("service creation")
            ) as opened,
            patch("opentab.sources.resolve_source", side_effect=AssertionError("discovery")),
            patch("opentab.sources.make_store", side_effect=AssertionError("store creation")),
        ):
            result = server.handle(
                _request("tools/call", {"name": "opentab_conversation_index_status"})
            )["result"]
        opened.assert_not_called()
        helper.index_status.assert_called_once_with()
        assert server._service is None
        assert not result["isError"]
        assert result["structuredContent"]["data"] == result_value


def test_conversations_mcp_status_translates_local_helper_errors():
    helper = Mock(spec=["index_status"])
    helper.index_status.side_effect = ConversationError("index_unavailable", "synthetic failure")
    with patch.object(ot, "conversation_search", helper, create=True):
        result = McpServer(_args()).handle(
            _request(
                "tools/call",
                {
                    "name": "opentab_conversation_index_status",
                },
            )
        )["result"]
    assert result["isError"]
    assert result["structuredContent"]["error"] == {
        "code": "index_unavailable",
        "message": "synthetic failure",
    }


def test_conversations_mcp_demo_denies_all_actions_before_service_or_helpers():
    for name, arguments in (
        ("opentab_index_conversations", {"confirm_index": True}),
        ("opentab_search_conversations", {"query": "synthetic", "confirm_raw": True}),
        ("opentab_conversation_index_status", {}),
    ):
        args = _args()
        args.demo = "all"
        helper = Mock()
        with (
            patch.object(ot, "conversation_search", helper, create=True),
            patch.object(ot.OpenTabService, "open") as opened,
        ):
            result = McpServer(args).handle(
                _request(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": arguments,
                    },
                )
            )["result"]
        opened.assert_not_called()
        assert helper.mock_calls == []
        assert result["isError"]
        assert result["structuredContent"]["error"]["code"] == "demo_unsupported"
