import contextlib
import io
import json
from unittest.mock import patch

import opentab as ot
import opentab.programmatic as programmatic
import opentab.service as service_module

from tests._support import FakeStore, workflow


class ModelStore(FakeStore):
    def model_breakdown(self):
        return [
            {
                "root_id": item.id,
                "model_name": "openai/gpt-5" if item.id == "b" else "anthropic/claude-opus-4-5",
                "cost": item.total_cost,
                "tokens_total": item.total_tokens,
                "input": item.total_tokens,
            }
            for item in self._workflows
        ]


def _model_search_command(argv, state=None):
    items = [
        workflow("a", "2026-09-01 12:00:00", title="fix login form"),
        workflow("b", "2026-09-02 12:00:00", title="investigate opus"),
        workflow("c", "2026-08-01 12:00:00", title="older login fix"),
    ]
    for item in items:
        item.machine = "laptop"
    args = ot.parse_args(argv)
    service = ot.OpenTabService(ModelStore(items), args, "opencode")
    out = io.StringIO()
    with (
        patch.object(service_module.OpenTabService, "open", return_value=service),
        patch.object(service_module, "load_state", return_value=state or {}),
        patch.object(service_module, "read_notes", return_value=({}, None)),
        contextlib.redirect_stdout(out),
    ):
        assert programmatic.command(args) == 0
    payload = json.loads(out.getvalue())
    assert payload["ok"] is True
    return payload["data"]


def test_programmatic_model_search_matches_names_not_session_text_and_keeps_filters():
    argv = ["models", "list", "--search", "opus", "--range", "2026-09"]
    result = _model_search_command(argv)
    assert result["total"] == 1
    assert result["models"][0]["model"] == "anthropic/claude-opus-4-5"
    assert result["models"][0]["recorded_cost_usd"] == 1
    assert (
        _model_search_command(["models", "list", "--search", "opus"])["models"][0][
            "recorded_cost_usd"
        ]
        == 2
    )
    assert _model_search_command(argv + ["--range", "2026-07"])["total"] == 0
    for flag, matching, missing in (
        ("--project", "/tmp/project", "/tmp/other-project"),
        ("--machine", "laptop", "desktop"),
        ("--from-harness", "opencode", "claude"),
        ("--model", "anthropic/claude-opus-4-5", "openai/gpt-5"),
    ):
        assert _model_search_command(argv + [flag, matching])["total"] == 1
        assert _model_search_command(argv + [flag, missing])["total"] == 0
    assert _model_search_command(argv + ["--bookmarked"], {"bookmarks": ["a"]})["total"] == 1
    assert _model_search_command(argv + ["--bookmarked"], {"bookmarks": ["b"]})["total"] == 0
    ignored = {"ignored_sessions": ["a"]}
    assert _model_search_command(argv, ignored)["total"] == 0
    assert _model_search_command(argv + ["--include-ignored"], ignored)["total"] == 1


def test_programmatic_session_search_still_matches_session_text():
    result = _model_search_command(["sessions", "list", "--search", "opus"])
    assert [row["native_id"] for row in result["sessions"]] == ["b"]
    result = _model_search_command(["sessions", "list", "--search", "login", "--range", "2026-09"])
    assert [row["native_id"] for row in result["sessions"]] == ["a"]


def test_programmatic_catalog_search_matches_model_names_without_matching_sessions():
    with patch.object(
        service_module,
        "catalog_models",
        return_value=[
            ("anthropic", "claude-opus-4-5", (5, 25, 0.5, 6.25), "active"),
            ("openai", "gpt-5", (1.25, 10, 0.125, 0), "active"),
        ],
    ):
        result = _model_search_command(
            ["models", "list", "--catalog", "--search", "OpUs", "--range", "2026-07"]
        )
    assert result["total"] == 1
    assert result["models"][0]["model"] == "anthropic/claude-opus-4-5"


class FakeService:
    def summary(self, query, group_by="none"):
        return {"range": query.range, "group_by": group_by}


def test_programmatic_command_tree_parses_resource_actions_and_privacy_flags():
    args = ot.parse_args(
        ["sessions", "list", "--range", "30d", "--from-harness", "claude", "--limit", "7"]
    )
    assert (args.command, args.action, args.range, args.query_harness, args.limit) == (
        "sessions",
        "list",
        "30d",
        "claude",
        7,
    )
    raw = ot.parse_args(["sessions", "content", "ot1_ref", "turn", "--allow-raw-content"])
    assert raw.action == "content" and raw.allow_raw_content
    assert ot.parse_args(["models", "pin", "gpt-5"]).model == "gpt-5"
    assert ot.parse_args(["models", "unpin", "gpt-5"]).action == "unpin"
    mcp = ot.parse_args(["mcp", "--harness", "claude", "--allow-raw-content"])
    assert mcp.command == "mcp" and mcp.source == "claude" and mcp.allow_raw_content


def test_programmatic_stdout_is_one_versioned_json_document():
    args = ot.parse_args(["usage", "summary", "--range", "2026-09", "--group-by", "day"])
    original = service_module.OpenTabService.open
    service_module.OpenTabService.open = lambda args, allow_raw_content=False: FakeService()
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            assert programmatic.command(args) == 0
    finally:
        service_module.OpenTabService.open = original
    payload = json.loads(out.getvalue())
    assert payload == {
        "schema_version": "1",
        "ok": True,
        "data": {"range": "2026-09", "group_by": "day"},
    }
    assert out.getvalue().count("\n") == 1


def test_programmatic_domain_failures_are_json_and_nonzero():
    class Broken:
        def get_session(self, value):
            raise ot.ServiceError("session_not_found", "gone", {"session": value})

    args = ot.parse_args(["sessions", "get", "missing"])
    original = service_module.OpenTabService.open
    service_module.OpenTabService.open = lambda args, allow_raw_content=False: Broken()
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            assert programmatic.command(args) == 1
    finally:
        service_module.OpenTabService.open = original
    payload = json.loads(out.getvalue())
    assert payload["ok"] is False
    assert payload["error"] == {
        "code": "session_not_found",
        "message": "gone",
        "details": {"session": "missing"},
    }
