"""Argument routing and JSON output for OpenTab's programmatic commands."""
from __future__ import annotations

import argparse
import copy
import json
import sys

from opentab import sources
from opentab.conversation import ConversationError
from opentab.models import API_SCHEMA_VERSION
from opentab.state import load_state

SCHEMA_VERSION = API_SCHEMA_VERSION

_SOURCE_PATH_ARGS = frozenset(
    {
        "source",
        "db",
        "claude_dir",
        "codex_dir",
        "hermes_db",
        "copilot_dir",
        "vscode_dir",
        "pi_dir",
        "omp_dir",
        "openclaw_dir",
        "zaly_dir",
        "gemini_dir",
        "antigravity_dir",
        "csv",
        "jsonl",
    }
)
_STORE_ARGS = _SOURCE_PATH_ARGS | {"remotes", "no_state", "no_cache"}
_READ_STORE_ARGS = _STORE_ARGS - {"no_state"}
_STATE_ARGS = frozenset({"no_state"})
_GLOBAL_HELP = {
    "source": "select the harness used for session discovery; --source is a deprecated alias",
    "no_state": "ignore saved notes, bookmarks, ignores, and pinned models; reject mutations",
    "no_cache": "bypass the accounting rollup cache while loading sessions",
}


def _add_output(parser) -> None:
    parser.add_argument("--pretty", action="store_true", help="indent the JSON response")


def _add_query(
    parser, *, paging: bool = True, model_search: bool = False, sorting: bool = True
) -> None:
    parser.add_argument(
        "--range",
        default="all",
        metavar="RANGE",
        help="all, 30d, 2m, 1y, YYYY, YYYY-MM, YYYY-MM-DD, or START..END; "
        "overridden by --days, then by --since/--until",
    )
    parser.add_argument("--days", type=int, help="number of days; overrides --range")
    parser.add_argument("--since", help="inclusive start date; overrides --days and --range")
    parser.add_argument("--until", help="inclusive end date; overrides --days and --range")
    parser.add_argument("--project", help="only sessions in this git project or directory")
    parser.add_argument("--from-harness", dest="query_harness", help="filter a merged source")
    parser.add_argument("--machine", help="only sessions from this machine")
    parser.add_argument("--model", help="only sessions that used this model")
    parser.add_argument(
        "--search",
        dest="model_search" if model_search else "search",
        help=(
            "filter model names by case-insensitive substring (used models or catalog)"
            if model_search
            else "fuzzy-search title, project, id, and notes"
        ),
    )
    parser.add_argument("--bookmarked", action="store_true", help="only bookmarked sessions")
    parser.add_argument("--include-ignored", action="store_true")
    if sorting:
        parser.add_argument(
            "--sort",
            choices=("cost", "tokens", "date", "last_activity", "title", "project"),
            default="cost",
        )
        parser.add_argument(
            "--reverse", action="store_true", help="reverse the default sort direction"
        )
    if paging:
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--offset", type=int, default=0)


def _add_global_bundle(parser, add_globals, allowed, help_overrides=None) -> None:
    probe = argparse.ArgumentParser(add_help=False)
    add_globals(probe)
    help_text = {**_GLOBAL_HELP, **(help_overrides or {})}
    defaults = {}
    for action in probe._actions:
        if action.dest not in allowed:
            continue
        # Each leaf gets its own Action so argparse cannot leak mutations between parsers.
        copied = copy.copy(action)
        copied.help = help_text.get(action.dest, action.help)
        parser._add_action(copied)
        if action.default is not argparse.SUPPRESS:
            defaults[action.dest] = action.default
    parser.set_defaults(_programmatic_global_defaults=defaults)


def _leaf(subs, name, help_text, add_globals, globals=(), help_overrides=None):
    parser = subs.add_parser(name, help=help_text)
    _add_global_bundle(parser, add_globals, globals, help_overrides)
    _add_output(parser)
    return parser


def _add_conversation_catalog(parser) -> None:
    parser.set_defaults(conversation_sources_only=True)
    parser.add_argument(
        "--harness",
        "--source",
        dest="source",
        choices=("opencode", "claude", "codex", "all"),
        default="all",
        help="conversation harness to load (default: all present supported harnesses; "
        "--source is a deprecated alias)",
    )
    parser.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="OpenCode database path (for --harness opencode/all)",
    )
    parser.add_argument(
        "--claude-dir",
        default=argparse.SUPPRESS,
        help="Claude Code projects directory (for --harness claude/all)",
    )
    parser.add_argument(
        "--codex-dir",
        default=argparse.SUPPRESS,
        help="Codex sessions directory (for --harness codex/all)",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        default=argparse.SUPPRESS,
        help="do not read saved project/session ignores",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        default=argparse.SUPPRESS,
        help="bypass the accounting rollup cache used to build the session catalog",
    )
    parser.add_argument("--project", help="only this project; saved ignores still apply")
    parser.add_argument(
        "--from-harness",
        dest="query_harness",
        choices=("opencode", "claude", "codex"),
        help="filter the loaded conversation catalog",
    )
    parser.add_argument("--machine", help="filter loaded machines; never fetch remote text")
    parser.add_argument("--session", help="session_key or unique native id")


def add_parsers(subs, add_globals) -> None:
    usage = subs.add_parser("usage", help="query usage summaries and grouped rollups")
    usage_subs = usage.add_subparsers(dest="action", required=True)
    summary = _leaf(usage_subs, "summary", "summarize matching usage", add_globals, _STORE_ARGS)
    _add_query(summary, paging=False, sorting=False)
    summary.add_argument(
        "--group-by",
        choices=(
            "none",
            "day",
            "month",
            "year",
            "project",
            "harness",
            "machine",
            "model",
            "provider",
        ),
        default="none",
    )

    sessions = subs.add_parser("sessions", help="list sessions and inspect their lazy detail")
    session_subs = sessions.add_subparsers(dest="action", required=True)
    listing = _leaf(session_subs, "list", "list and filter sessions", add_globals, _STORE_ARGS)
    _add_query(listing)
    for action, text in (
        ("get", "show one session and its model usage"),
        ("nodes", "show a session's subagent tree"),
        ("turns", "show a session's accounting timeline"),
        ("tools", "show a session's tool and MCP attribution"),
        ("context", "show a session's context curve and composition"),
        ("content", "read one raw recorded turn trace"),
        ("conversation", "read bounded text records from one local execution"),
    ):
        global_args = _STORE_ARGS if action in {"get", "turns", "content"} else _READ_STORE_ARGS
        parser = _leaf(
            session_subs,
            action,
            text,
            add_globals,
            global_args,
            help_overrides=(
                {
                    "no_state": "accepted for remote trace compatibility; "
                    "detail output is unchanged"
                }
                if action in {"turns", "content"}
                else None
            ),
        )
        parser.add_argument("session", metavar="SESSION_KEY|ID")
        if action == "turns":
            parser.add_argument("--include-prompts", action="store_true")
            parser.add_argument("--include-content-keys", action="store_true")
            parser.add_argument(
                "--allow-raw-content",
                action="store_true",
                help="permit full prompts and local trace keys",
            )
        if action in {"content", "conversation"}:
            if action == "content":
                parser.add_argument("content_key")
            parser.add_argument(
                "--allow-raw-content",
                action="store_true",
                required=True,
                help=(
                    "permit reading retained user and assistant text records"
                    if action == "conversation"
                    else "explicitly permit prompts, reasoning, tool arguments, and results"
                ),
            )
        if action == "conversation":
            parser.add_argument(
                "--execution-id",
                help="exact child id from response.executions; default is root only",
            )
            selector = parser.add_mutually_exclusive_group()
            selector.add_argument("--anchor", help="record anchor returned by a previous read")
            selector.add_argument("--cursor", help="opaque next_cursor returned by a previous read")
            selector.add_argument("--tail", action="store_true", help="read the last window")
            parser.add_argument(
                "--limit", type=int, default=20, help="maximum records (1..100; default 20)"
            )
            parser.add_argument(
                "--max-chars",
                type=int,
                default=20000,
                help="text budget (1..120000; default 20000)",
            )
            parser.add_argument(
                "--before",
                type=int,
                default=0,
                help="records before anchor (0..99; requires --anchor)",
            )

    conversations = subs.add_parser(
        "conversations", help="explicit local conversation indexing and search"
    )
    conversation_subs = conversations.add_subparsers(dest="action", required=True)
    for action, text in (
        (
            "index",
            "create or update a local plaintext conversation index; no network or embeddings",
        ),
        ("search", "search indexed conversation text; dates select messages, not root sessions"),
        ("status", "show local conversation index counts without discovering sources"),
        ("clear", "delete the local conversation index, not original harness records"),
    ):
        parser = conversation_subs.add_parser(action, help=text)
        _add_output(parser)
        if action != "status":
            permission_help = {
                "index": "permit reading source conversations and writing the local plaintext index",
                "search": "permit reading indexed text and live source conversation evidence",
                "clear": "permit deletion of the local plaintext conversation index",
            }[action]
            parser.add_argument(
                "--allow-raw-content",
                action="store_true",
                required=True,
                help=permission_help,
            )
        if action in {"index", "search"}:
            _add_conversation_catalog(parser)
        if action == "index":
            parser.add_argument("--rebuild", action="store_true", help="rebuild the selected scope")
        elif action == "search":
            parser.add_argument("query", metavar="QUERY", help="lexical conversation text query")
            parser.add_argument(
                "--exclude-session", help="exclude this session_key or unique native id"
            )
            parser.add_argument(
                "--limit", type=int, default=10, help="maximum hits (1..100; default 10)"
            )
            parser.add_argument(
                "--max-chars", type=int, default=6000, help="text budget (1..120000; default 6000)"
            )
            for option in ("--since", "--until"):
                parser.add_argument(
                    option,
                    metavar="YYYY-MM-DD",
                    help="inclusive UTC message-date bound, not root-session start date",
                )

    models = subs.add_parser("models", help="query used models, prices, and comparisons")
    model_subs = models.add_subparsers(dest="action", required=True)
    listing = _leaf(
        model_subs, "list", "list used models or the price catalog", add_globals, _STORE_ARGS
    )
    _add_query(listing, paging=False, model_search=True, sorting=False)
    listing.add_argument("--catalog", action="store_true")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--offset", type=int, default=0)
    compare = _leaf(
        model_subs,
        "compare",
        "reprice one session at a target model",
        add_globals,
        _READ_STORE_ARGS,
    )
    compare.add_argument("session", metavar="SESSION_KEY|ID")
    compare.add_argument("target_model", metavar="MODEL")
    for action in ("pin", "unpin"):
        parser = _leaf(model_subs, action, f"{action} a model", add_globals, _STATE_ARGS)
        parser.add_argument("model", metavar="MODEL")

    source = subs.add_parser("sources", help="inspect harness discovery")
    source_subs = source.add_subparsers(dest="action", required=True)
    _leaf(
        source_subs,
        "list",
        "list configured and detected harnesses",
        add_globals,
        _SOURCE_PATH_ARGS,
        help_overrides={
            **_GLOBAL_HELP,
            "source": "select the harness reported as selected alongside discovery results; "
            "--source is a deprecated alias",
        },
    )

    notes = subs.add_parser("notes", help="get, set, or delete authored session notes")
    note_subs = notes.add_subparsers(dest="action", required=True)
    for action in ("get", "delete"):
        parser = _leaf(note_subs, action, f"{action} a session note", add_globals, _STORE_ARGS)
        parser.add_argument("session", metavar="SESSION_KEY|ID")
    parser = _leaf(note_subs, "set", "set a session note", add_globals, _STORE_ARGS)
    parser.add_argument("session", metavar="SESSION_KEY|ID")
    parser.add_argument("text")

    bookmarks = subs.add_parser("bookmarks", help="list or mutate bookmarked sessions")
    bookmark_subs = bookmarks.add_subparsers(dest="action", required=True)
    _leaf(bookmark_subs, "list", "list bookmark ids", add_globals, _STATE_ARGS)
    for action in ("add", "remove"):
        parser = _leaf(bookmark_subs, action, f"{action} a bookmark", add_globals, _STORE_ARGS)
        parser.add_argument("session", metavar="SESSION_KEY|ID")

    ignore = subs.add_parser("ignore", help="list or mutate ignored sessions and projects")
    ignore_subs = ignore.add_subparsers(dest="kind", required=True)
    _leaf(ignore_subs, "list", "list ignored sessions and projects", add_globals, _STATE_ARGS)
    for kind in ("session", "project"):
        kind_parser = ignore_subs.add_parser(kind, help=f"mutate an ignored {kind}")
        kind_subs = kind_parser.add_subparsers(dest="action", required=True)
        for action in ("add", "remove"):
            parser = _leaf(
                kind_subs,
                action,
                f"{action} an ignored {kind}",
                add_globals,
                _STORE_ARGS if kind == "session" else _STATE_ARGS,
            )
            parser.add_argument("value", metavar="SESSION_KEY|ID" if kind == "session" else "PATH")

    mcp = subs.add_parser("mcp", help="serve OpenTab tools over MCP on stdio")
    _add_global_bundle(mcp, add_globals, _STORE_ARGS)
    mcp.add_argument(
        "--allow-raw-content",
        action="store_true",
        help="allow tools to expose prompts, reasoning, commands, and tool output",
    )


def _range(args) -> str:
    if getattr(args, "since", None) or getattr(args, "until", None):
        return f"{getattr(args, 'since', None) or ''}..{getattr(args, 'until', None) or ''}"
    if getattr(args, "days", None) is not None:
        return f"{args.days}d"
    return getattr(args, "range", "all")


def query_from_args(args):
    from opentab.service import SessionQuery

    return SessionQuery(
        range=_range(args),
        project=getattr(args, "project", None),
        harness=getattr(args, "query_harness", None),
        machine=getattr(args, "machine", None),
        model=getattr(args, "model", None),
        search=getattr(args, "search", None),
        bookmarked=bool(getattr(args, "bookmarked", False)),
        include_ignored=bool(getattr(args, "include_ignored", False)),
        sort=getattr(args, "sort", "cost"),
        reverse=bool(getattr(args, "reverse", False)),
        limit=getattr(args, "limit", 100),
        offset=getattr(args, "offset", 0),
    )


def envelope(data) -> dict:
    return {"schema_version": SCHEMA_VERSION, "ok": True, "data": data}


def error_envelope(code: str, message: str, details: dict | None = None) -> dict:
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"schema_version": SCHEMA_VERSION, "ok": False, "error": error}


def _write(payload: dict, pretty: bool = False) -> None:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )
    sys.stdout.write(text + "\n")


def _preferences(args) -> dict:
    if getattr(args, "no_state", False):
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


def _changed_global(args, dest: str) -> bool:
    defaults = getattr(args, "_programmatic_global_defaults", {})
    default = defaults.get(dest, argparse.SUPPRESS)
    if default is argparse.SUPPRESS:
        return False
    if dest == "csv" and default is None:
        default = sources.DEFAULT_CSV_PATH
    elif dest == "jsonl" and default is None:
        default = sources.DEFAULT_JSONL_PATH
    return getattr(args, dest, None) != default


def _validate_catalog_args(args, ServiceError) -> None:
    if args.command != "models" or args.action != "list" or not args.catalog:
        return
    conflicts = []
    query_defaults = {
        "range": "all",
        "project": None,
        "query_harness": None,
        "machine": None,
        "model": None,
        "bookmarked": False,
        "include_ignored": False,
        "days": None,
        "since": None,
        "until": None,
    }
    for dest, default in query_defaults.items():
        if getattr(args, dest, default) != default:
            conflicts.append(dest)
    conflicts.extend(
        dest for dest in sorted(_STORE_ARGS - _STATE_ARGS) if _changed_global(args, dest)
    )
    if conflicts:
        option_names = {"query_harness": "from-harness", "source": "harness"}
        options = ", ".join(
            "--" + option_names.get(dest, dest.replace("_", "-")) for dest in conflicts
        )
        raise ServiceError(
            "invalid_catalog_options",
            f"models list --catalog does not accept session or source options: {options}",
        )


def _mutate_without_store(args, OpenTabService) -> dict | None:
    if args.command == "models" and args.action in {"pin", "unpin"}:
        resource, operation, value = (
            "pinned-model",
            "add" if args.action == "pin" else "remove",
            args.model,
        )
    elif args.command == "ignore" and args.kind == "project":
        resource, operation, value = "ignored-project", args.action, args.value
    else:
        return None
    return OpenTabService.mutate_global_set(
        resource,
        operation,
        value,
        use_state=not bool(getattr(args, "no_state", False)),
    )


def command(args) -> int:
    if args.command == "mcp":
        from opentab.mcp import run_server

        return run_server(args)
    from opentab.service import OpenTabService, ServiceError

    try:
        if getattr(args, "demo", False):
            raise ServiceError(
                "demo_unsupported", "programmatic access is unavailable in demo mode"
            )
        _validate_catalog_args(args, ServiceError)
        if args.command == "models" and args.action == "list" and args.catalog:
            data = OpenTabService.list_model_catalog(
                search=args.model_search,
                limit=args.limit,
                offset=args.offset,
                use_state=not bool(getattr(args, "no_state", False)),
            )
            _write(envelope(data), getattr(args, "pretty", False))
            return 0
        direct_mutation = _mutate_without_store(args, OpenTabService)
        if direct_mutation is not None:
            _write(envelope(direct_mutation), getattr(args, "pretty", False))
            return 0
        if args.command == "conversations":
            if args.action != "status" and not getattr(args, "allow_raw_content", False):
                raise ServiceError("raw_content_disabled", "--allow-raw-content is required")
            if args.action in {"status", "clear"}:
                from opentab import conversation_search

                data = (
                    conversation_search.index_status()
                    if args.action == "status"
                    else conversation_search.clear_index()
                )
                _write(envelope(data), getattr(args, "pretty", False))
                return 0
        if args.command == "sources":
            present = sources.available_sources(args)
            selected = sources.resolve_source(args, {})
            data = {
                "selected": selected,
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
        elif args.command == "bookmarks" and args.action == "list":
            data = {"bookmarks": _preferences(args)["bookmarks"]}
        elif args.command == "ignore" and args.kind == "list":
            prefs = _preferences(args)
            data = {
                "ignored_sessions": prefs["ignored_sessions"],
                "ignored_projects": prefs["ignored_projects"],
            }
        else:
            service = OpenTabService.open(
                args, allow_raw_content=bool(getattr(args, "allow_raw_content", False))
            )
            if args.command == "conversations":
                scope = dict(
                    project=args.project,
                    harness=args.query_harness,
                    machine=args.machine,
                    session=args.session,
                )
                if args.action == "index":
                    data = service.index_conversations(**scope, rebuild=args.rebuild)
                else:
                    data = service.search_conversations(
                        args.query,
                        **scope,
                        exclude_session=args.exclude_session,
                        since=args.since,
                        until=args.until,
                        limit=args.limit,
                        max_chars=args.max_chars,
                    )
            elif args.command == "usage":
                data = service.summary(query_from_args(args), group_by=args.group_by)
            elif args.command == "sessions":
                if args.action == "list":
                    data = service.list_sessions(query_from_args(args))
                elif args.action == "get":
                    data = service.get_session(args.session)
                elif args.action == "nodes":
                    data = service.session_nodes(args.session)
                elif args.action == "turns":
                    data = service.session_turns(
                        args.session,
                        include_prompts=args.include_prompts,
                        include_content_keys=args.include_content_keys,
                    )
                elif args.action == "tools":
                    data = service.session_tools(args.session)
                elif args.action == "context":
                    data = service.session_context(args.session)
                elif args.action == "conversation":
                    data = service.session_conversation(
                        args.session,
                        execution_id=args.execution_id,
                        anchor=args.anchor,
                        cursor=args.cursor,
                        limit=args.limit,
                        max_chars=args.max_chars,
                        before=args.before,
                        tail=args.tail,
                    )
                elif args.action == "content":
                    data = service.session_content(args.session, args.content_key)
                else:
                    raise ServiceError(
                        "unknown_command", f"unsupported session action: {args.action}"
                    )
            elif args.command == "models":
                if args.action == "list":
                    data = service.list_models(
                        query_from_args(args),
                        search=args.model_search,
                        limit=args.limit,
                        offset=args.offset,
                    )
                elif args.action == "compare":
                    data = service.compare_model(args.session, args.target_model)
                else:
                    raise ServiceError(
                        "unknown_command", f"unsupported model action: {args.action}"
                    )
            elif args.command == "notes":
                if args.action == "get":
                    data = service.get_note(args.session)
                else:
                    data = service.set_note(
                        args.session, "" if args.action == "delete" else args.text
                    )
            elif args.command == "bookmarks":
                data = service.mutate_set("bookmark", args.action, args.session)
            elif args.command == "ignore":
                if args.kind != "session":
                    raise ServiceError("unknown_command", f"unsupported ignore kind: {args.kind}")
                data = service.mutate_set("ignored-session", args.action, args.value)
            else:
                raise ServiceError("unknown_command", f"unsupported command: {args.command}")
        _write(envelope(data), getattr(args, "pretty", False))
        return 0
    except ServiceError as exc:
        _write(error_envelope(exc.code, exc.message, exc.details), getattr(args, "pretty", False))
        return 1
    except ConversationError as exc:
        _write(error_envelope(exc.code, exc.message), getattr(args, "pretty", False))
        return 1
    except SystemExit as exc:
        _write(error_envelope("source_error", str(exc)), getattr(args, "pretty", False))
        return 1
    except (OSError, ValueError) as exc:
        _write(error_envelope("operation_failed", str(exc)), getattr(args, "pretty", False))
        return 1
