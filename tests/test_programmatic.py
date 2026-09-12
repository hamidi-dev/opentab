import argparse
import contextlib
import io
import json
from unittest.mock import Mock, patch

import opentab as ot
import opentab.programmatic as programmatic
import opentab.service as service_module
from opentab.conversation import ConversationError

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
        result = _model_search_command(["models", "list", "--catalog", "--search", "OpUs"])
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


def test_programmatic_valid_option_bundles_parse_and_date_precedence_is_stable():
    cases = (
        ["usage", "summary", "--days", "7", "--harness", "all", "--no-cache"],
        ["sessions", "get", "root", "--harness", "remote", "--remotes", "/tmp/remotes"],
        ["models", "list", "--catalog", "--search", "opus", "--no-state"],
        ["sources", "list", "--db", "/tmp/opentab.db"],
        ["notes", "get", "root", "--harness", "claude"],
        ["bookmarks", "list", "--no-state"],
        ["ignore", "project", "add", "/tmp/project"],
        ["mcp", "--harness", "remote", "--remotes", "/tmp/remotes", "--no-cache"],
    )
    for argv in cases:
        parsed = ot.parse_args(argv)
        assert parsed.command == argv[0]
        assert isinstance(parsed._programmatic_global_defaults, dict)

    assert (
        programmatic._range(
            ot.parse_args(
                [
                    "sessions",
                    "list",
                    "--range",
                    "1y",
                    "--days",
                    "30",
                    "--since",
                    "2026-09-01",
                    "--until",
                    "2026-09-10",
                ]
            )
        )
        == "2026-09-01..2026-09-10"
    )
    assert programmatic._range(ot.parse_args(["sessions", "list", "--days", "30"])) == "30d"


def _parser_help(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            ot.parse_args([*argv, "--help"])
        except SystemExit as exc:
            assert exc.code == 0
    return out.getvalue()


def test_programmatic_help_only_exposes_options_used_by_each_command():
    summary = _parser_help(["usage", "summary"])
    assert all(flag in summary for flag in ("--range", "--days", "--since", "--until", "--db"))
    assert "YYYY-MM-DD" in summary and "START..END" in summary
    assert "spend to browse" not in summary and "saved range/sort state" not in summary
    assert "accounting rollup cache" in summary
    assert all(
        flag not in summary
        for flag in ("--sort", "--reverse", "--no-worktrees", "--demo", "--theme")
    )

    session_list = _parser_help(["sessions", "list"])
    assert "--sort" in session_list and "--no-cache" in session_list
    session_get = _parser_help(["sessions", "get"])
    assert all(flag not in session_get for flag in ("--range", "--days", "--sort", "--theme"))
    assert "--no-state" in session_get

    for action in ("nodes", "tools", "context", "conversation"):
        detail = _parser_help(["sessions", action])
        assert "--no-state" not in detail
    for action in ("turns", "content"):
        detail = _parser_help(["sessions", action])
        assert "--no-state" in detail
        assert "accepted for remote trace compatibility" in detail
    assert "--no-state" not in _parser_help(["models", "compare"])

    source_list = _parser_help(["sources", "list"])
    assert "--db" in source_list and "--claude-dir" in source_list
    assert "reported as selected alongside" in source_list
    assert all(flag not in source_list for flag in ("--no-cache", "--no-state", "--remotes"))

    bookmarks = _parser_help(["bookmarks", "list"])
    assert "--pretty" in bookmarks and "--no-state" in bookmarks
    assert "ignore saved notes, bookmarks, ignores, and pinned models" in bookmarks
    assert "--harness" not in bookmarks and "--db" not in bookmarks

    conversation = _parser_help(["sessions", "conversation"])
    assert "retained user and assistant" in conversation
    assert "reasoning" not in conversation and "tool arguments" not in conversation

    mcp = _parser_help(["mcp"])
    assert all(
        flag in mcp for flag in ("--harness", "--db", "--remotes", "--no-cache", "--no-state")
    )
    assert all(
        flag not in mcp
        for flag in (
            "--range",
            "--days",
            "--no-worktrees",
            "--demo",
            "--theme",
            "--port",
            "--bind",
        )
    )


def test_programmatic_irrelevant_options_are_rejected_by_parser_before_service_open():
    cases = (
        ["usage", "summary", "--sort", "cost"],
        ["sessions", "get", "root", "--range", "30d"],
        ["sources", "list", "--no-cache"],
        ["bookmarks", "list", "--harness", "claude"],
        ["models", "pin", "gpt-5", "--db", "/tmp/opentab.db"],
        ["sessions", "nodes", "root", "--no-state"],
        ["sessions", "tools", "root", "--no-state"],
        ["sessions", "context", "root", "--no-state"],
        ["sessions", "conversation", "root", "--allow-raw-content", "--no-state"],
        ["models", "compare", "root", "gpt-5", "--no-state"],
        ["mcp", "--no-worktrees"],
        ["mcp", "--demo"],
    )
    for argv in cases:
        with contextlib.redirect_stderr(io.StringIO()), patch.object(
            ot.OpenTabService, "open"
        ) as opened:
            try:
                ot.parse_args(argv)
                raise AssertionError(f"expected parser rejection: {argv}")
            except SystemExit as exc:
                assert exc.code == 2
            opened.assert_not_called()


def test_remote_trace_argv_keeps_no_state_compatibility_for_turns_and_content():
    for argv in (
        [
            "sessions",
            "turns",
            "--source",
            "claude",
            "--allow-raw-content",
            "--no-state",
            "--include-content-keys",
            "--",
            "native-root",
        ],
        [
            "sessions",
            "content",
            "--source",
            "claude",
            "--allow-raw-content",
            "--no-state",
            "--",
            "ot1_remote",
            "turn-key",
        ],
    ):
        args = ot.parse_args(argv)
        assert args.source == "claude"
        assert args.allow_raw_content is True
        assert args.no_state is True


def test_catalog_validation_defaults_stay_bound_to_the_parser_that_created_args():
    def parse(default):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers(dest="command", required=True)

        def add_globals(leaf):
            leaf.add_argument("--db", default=default)

        programmatic.add_parsers(subs, add_globals)
        return parser.parse_args(["models", "list", "--catalog"])

    first = parse("/parser-a.db")
    parse("/parser-b.db")
    programmatic._validate_catalog_args(first, ot.ServiceError)
    assert first._programmatic_global_defaults == {"db": "/parser-a.db"}


def test_programmatic_catalog_rejects_session_and_source_filters_before_store_open():
    for flags, reported in (
        (["--range", "30d"], "--range"),
        (["--days", "30"], "--days"),
        (["--since", "2026-09-01"], "--since"),
        (["--until", "2026-09-10"], "--until"),
        (["--project", "/tmp/project"], "--project"),
        (["--from-harness", "claude"], "--from-harness"),
        (["--machine", "laptop"], "--machine"),
        (["--model", "gpt-5"], "--model"),
        (["--bookmarked"], "--bookmarked"),
        (["--include-ignored"], "--include-ignored"),
        (["--harness", "claude"], "--harness"),
        (["--db", "/tmp/opentab.db"], "--db"),
        (["--claude-dir", "/tmp/claude"], "--claude-dir"),
        (["--codex-dir", "/tmp/codex"], "--codex-dir"),
        (["--hermes-db", "/tmp/hermes.db"], "--hermes-db"),
        (["--copilot-dir", "/tmp/copilot"], "--copilot-dir"),
        (["--vscode-dir", "/tmp/vscode"], "--vscode-dir"),
        (["--pi-dir", "/tmp/pi"], "--pi-dir"),
        (["--omp-dir", "/tmp/omp"], "--omp-dir"),
        (["--openclaw-dir", "/tmp/openclaw"], "--openclaw-dir"),
        (["--zaly-dir", "/tmp/zaly"], "--zaly-dir"),
        (["--gemini-dir", "/tmp/gemini"], "--gemini-dir"),
        (["--antigravity-dir", "/tmp/antigravity"], "--antigravity-dir"),
        (["--csv", "/tmp/requests.csv"], "--csv"),
        (["--jsonl", "/tmp/requests.jsonl"], "--jsonl"),
        (["--remotes", "/tmp/remotes"], "--remotes"),
        (["--no-cache"], "--no-cache"),
    ):
        args = ot.parse_args(["models", "list", "--catalog", *flags])
        out = io.StringIO()
        with patch.object(ot.OpenTabService, "open") as opened, contextlib.redirect_stdout(out):
            assert programmatic.command(args) == 1
        opened.assert_not_called()
        error = json.loads(out.getvalue())["error"]
        assert error["code"] == "invalid_catalog_options"
        assert reported in error["message"]


def test_programmatic_catalog_needs_no_session_store_or_source_discovery():
    args = ot.parse_args(
        ["models", "list", "--catalog", "--search", "opus", "--limit", "1", "--offset", "0"]
    )
    expected = {
        "models": [{"model": "anthropic/claude-opus", "pinned": True}],
        "total": 1,
        "limit": 1,
        "offset": 0,
    }
    out = io.StringIO()
    with (
        patch.object(ot.OpenTabService, "open") as opened,
        patch.object(ot.OpenTabService, "list_model_catalog", return_value=expected) as catalog,
        patch.object(programmatic.sources, "available_sources") as available,
        patch.object(programmatic.sources, "resolve_source") as resolved,
        patch.object(programmatic.sources, "make_store") as made,
        contextlib.redirect_stdout(out),
    ):
        assert programmatic.command(args) == 0
    opened.assert_not_called()
    available.assert_not_called()
    resolved.assert_not_called()
    made.assert_not_called()
    catalog.assert_called_once_with(search="opus", limit=1, offset=0, use_state=True)
    assert json.loads(out.getvalue())["data"] == expected


def test_programmatic_state_only_mutations_do_not_open_a_store():
    state = {
        "pinned_models": ["synthetic/model"],
        "ignored_projects": ["/tmp/synthetic-project"],
    }
    for argv, resource in (
        (["models", "pin", "synthetic/model"], "pinned-model"),
        (["ignore", "project", "add", "/tmp/synthetic-project"], "ignored-project"),
    ):
        args = ot.parse_args(argv)
        out = io.StringIO()
        with (
            patch.object(ot.OpenTabService, "open") as opened,
            patch.object(service_module, "update_state", return_value=(state, "")),
            contextlib.redirect_stdout(out),
        ):
            assert programmatic.command(args) == 0
        opened.assert_not_called()
        assert json.loads(out.getvalue())["data"]["resource"] == resource


def test_programmatic_internal_demo_gate_precedes_state_mutation():
    args = ot.parse_args(["models", "pin", "synthetic/model"])
    args.demo = "all"
    out = io.StringIO()
    with (
        patch.object(ot.OpenTabService, "open") as opened,
        patch("opentab.state.update_state") as updated,
        contextlib.redirect_stdout(out),
    ):
        assert programmatic.command(args) == 1
    opened.assert_not_called()
    updated.assert_not_called()
    assert json.loads(out.getvalue())["error"]["code"] == "demo_unsupported"


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


def test_conversation_cli_requires_opt_in_and_rejects_bad_selector_types():
    for argv in (
        ["sessions", "conversation", "root"],
        ["sessions", "conversation", "root", "--allow-raw-content", "--limit", "true"],
        ["sessions", "conversation", "root", "--allow-raw-content", "--max-chars", "1.5"],
        ["sessions", "conversation", "root", "--allow-raw-content", "--before", "no"],
        ["sessions", "conversation", "root", "--allow-raw-content", "--anchor", "a", "--tail"],
        [
            "sessions",
            "conversation",
            "root",
            "--allow-raw-content",
            "--cursor",
            "c",
            "--anchor",
            "a",
        ],
    ):
        with contextlib.redirect_stderr(io.StringIO()), patch.object(
            ot.OpenTabService, "open"
        ) as opened:
            try:
                ot.parse_args(argv)
                raise AssertionError("expected parser rejection")
            except SystemExit as exc:
                assert exc.code == 2
            opened.assert_not_called()


def test_conversation_cli_forwards_all_options_without_falling_into_content():
    defaults = dict(
        execution_id=None, anchor=None, cursor=None, limit=20, max_chars=20000, before=0, tail=False
    )
    for flags, expected in (
        ([], defaults),
        (
            [
                "--execution-id",
                "child",
                "--anchor",
                "a",
                "--before",
                "2",
                "--limit",
                "7",
                "--max-chars",
                "99",
            ],
            {
                **defaults,
                "execution_id": "child",
                "anchor": "a",
                "before": 2,
                "limit": 7,
                "max_chars": 99,
            },
        ),
        (["--cursor", "opaque"], {**defaults, "cursor": "opaque"}),
        (["--tail"], {**defaults, "tail": True}),
    ):
        args = ot.parse_args(
            ["sessions", "conversation", "qualified-root", "--allow-raw-content", *flags]
        )
        service = Mock()
        service.session_conversation.return_value = {"records": [], "execution_id": "root"}
        out = io.StringIO()
        with patch.object(
            ot.OpenTabService, "open", return_value=service
        ) as opened, contextlib.redirect_stdout(out):
            assert programmatic.command(args) == 0
        opened.assert_called_once_with(args, allow_raw_content=True)
        service.session_conversation.assert_called_once_with("qualified-root", **expected)
        service.session_content.assert_not_called()
        assert json.loads(out.getvalue()) == {
            "schema_version": "1",
            "ok": True,
            "data": service.session_conversation.return_value,
        }


def test_conversation_cli_shared_validation_errors_are_json_without_reader_calls():
    from tests.test_service import ConversationStore

    args = ot.parse_args(
        ["sessions", "conversation", "root", "--allow-raw-content", "--limit", "101"]
    )
    store = ConversationStore([workflow("root", "2026-09-01 12:00:00")])
    service = ot.OpenTabService(store, args, allow_raw_content=True)
    out = io.StringIO()
    with patch.object(ot.OpenTabService, "open", return_value=service), contextlib.redirect_stdout(
        out
    ):
        assert programmatic.command(args) == 1
    assert json.loads(out.getvalue())["error"]["code"].startswith("invalid")
    assert store.reads == store.probes == []


def test_conversations_cli_forwards_index_and_search_without_session_queries():
    scope = dict(project=None, harness=None, machine=None, session=None)
    for action in ("index", "search"):
        for scoped in (False, True):
            argv = ["conversations", action]
            if action == "search":
                argv.append("synthetic evidence")
            argv.append("--allow-raw-content")
            expected = dict(scope)
            if scoped:
                argv += [
                    "--project",
                    "/synthetic/project",
                    "--from-harness",
                    "claude",
                    "--machine",
                    "synthetic-box",
                    "--session",
                    "qualified-root",
                ]
                expected.update(
                    project="/synthetic/project",
                    harness="claude",
                    machine="synthetic-box",
                    session="qualified-root",
                )
            if action == "index":
                expected["rebuild"] = scoped
                if scoped:
                    argv.append("--rebuild")
            else:
                expected.update(
                    exclude_session=None, since=None, until=None, limit=10, max_chars=6000
                )
                if scoped:
                    argv += [
                        "--exclude-session",
                        "current",
                        "--since",
                        "2026-09-01",
                        "--until",
                        "2026-09-11",
                        "--limit",
                        "7",
                        "--max-chars",
                        "99",
                    ]
                    expected.update(
                        exclude_session="current",
                        since="2026-09-01",
                        until="2026-09-11",
                        limit=7,
                        max_chars=99,
                    )
            args = ot.parse_args(argv)
            service = Mock(spec=["index_conversations", "search_conversations"])
            method = getattr(service, f"{action}_conversations")
            method.return_value = {"synthetic": True}
            out = io.StringIO()
            with (
                patch.object(ot.OpenTabService, "open", return_value=service) as opened,
                patch.object(
                    programmatic, "query_from_args", side_effect=AssertionError("session query")
                ),
                contextlib.redirect_stdout(out),
            ):
                assert programmatic.command(args) == 0
            opened.assert_called_once_with(args, allow_raw_content=True)
            method.assert_called_once_with(
                *(["synthetic evidence"] if action == "search" else []), **expected
            )
            assert json.loads(out.getvalue()) == {
                "schema_version": "1",
                "ok": True,
                "data": {"synthetic": True},
            }


def test_conversations_cli_maintenance_is_dynamic_and_never_discovers_sources():
    for action, method in (("status", "index_status"), ("clear", "clear_index")):
        argv = ["conversations", action]
        if action == "clear":
            argv.append("--allow-raw-content")
        args = ot.parse_args(argv)
        helper = Mock(spec=["index_status", "clear_index"])
        getattr(helper, method).return_value = {"exists": False, "records": 0}
        out = io.StringIO()
        with (
            patch.object(ot, "conversation_search", helper, create=True),
            patch.object(
                ot.OpenTabService, "open", side_effect=AssertionError("service discovery")
            ) as opened,
            patch.object(
                programmatic.sources,
                "resolve_source",
                side_effect=AssertionError("source discovery"),
            ),
            patch.object(
                programmatic.sources,
                "available_sources",
                side_effect=AssertionError("source discovery"),
            ),
            patch.object(
                programmatic.sources, "make_store", side_effect=AssertionError("store creation")
            ),
            contextlib.redirect_stdout(out),
        ):
            assert programmatic.command(args) == 0
        opened.assert_not_called()
        getattr(helper, method).assert_called_once_with()
        assert len(helper.mock_calls) == 1
        assert json.loads(out.getvalue())["data"] == {"exists": False, "records": 0}


def test_conversations_cli_demo_and_missing_permission_precede_helpers_and_service():
    for action in ("index", "search", "status", "clear"):
        argv = ["conversations", action]
        if action == "search":
            argv.append("synthetic evidence")
        if action != "status":
            argv.append("--allow-raw-content")
        for denied in ("demo", "permission"):
            if action == "status" and denied == "permission":
                continue
            args = ot.parse_args(argv)
            args.demo = "all" if denied == "demo" else None
            args.allow_raw_content = denied == "demo"
            out, helper = io.StringIO(), Mock()
            with (
                patch.object(ot, "conversation_search", helper, create=True),
                patch.object(ot.OpenTabService, "open") as opened,
                contextlib.redirect_stdout(out),
            ):
                assert programmatic.command(args) == 1
            opened.assert_not_called()
            assert helper.mock_calls == []
            assert json.loads(out.getvalue())["error"]["code"] == (
                "demo_unsupported" if denied == "demo" else "raw_content_disabled"
            )


def test_conversations_cli_translates_shared_errors_including_maintenance():
    for action in ("index", "search", "status", "clear"):
        argv = ["conversations", action]
        if action == "search":
            argv += ["synthetic", "--limit", "0", "--since", "not-a-date"]
        if action != "status":
            argv.append("--allow-raw-content")
        args = ot.parse_args(argv)
        helper, service = Mock(), Mock()
        target = (
            getattr(helper, "index_status" if action == "status" else "clear_index")
            if action in {"status", "clear"}
            else getattr(service, f"{action}_conversations")
        )
        target.side_effect = ConversationError("invalid_conversation_index", "synthetic failure")
        out = io.StringIO()
        with (
            patch.object(ot, "conversation_search", helper, create=True),
            patch.object(ot.OpenTabService, "open", return_value=service),
            contextlib.redirect_stdout(out),
        ):
            assert programmatic.command(args) == 1
        assert json.loads(out.getvalue()) == {
            "schema_version": "1",
            "ok": False,
            "error": {"code": "invalid_conversation_index", "message": "synthetic failure"},
        }
