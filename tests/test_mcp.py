import io
import json
import os
import subprocess
import sys
import tempfile

import opentab as ot
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
        assert data[1]["total"] == 1
        assert data[2]["values"] == ["s1"]
        assert data[4]["bookmarked"] is True
        assert data[4]["note"] == "investigate cache churn"
        assert ot.load_state(os.path.join(tmp, "opentab", "state.json"))["bookmarks"] == ["s1"]
        notes, readable = ot.read_notes(os.path.join(tmp, "opentab", "notes.json"))
        assert readable and notes == {"s1": "investigate cache churn"}
