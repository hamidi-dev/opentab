"""Argument routing and JSON output for OpenTab's programmatic commands."""
from __future__ import annotations

import json
import sys

from opentab import sources
from opentab.models import API_SCHEMA_VERSION
from opentab.state import load_state

SCHEMA_VERSION = API_SCHEMA_VERSION


def _add_output(parser) -> None:
    parser.add_argument("--pretty", action="store_true", help="indent the JSON response")


def _add_query(parser, *, paging: bool = True, model_search: bool = False) -> None:
    parser.add_argument(
        "--range",
        default="all",
        metavar="RANGE",
        help="all, 30d, 2m, 1y, YYYY, YYYY-MM, YYYY-MM-DD, or START..END",
    )
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
    parser.add_argument(
        "--sort",
        choices=("cost", "tokens", "date", "last_activity", "title", "project"),
        default="cost",
    )
    parser.add_argument("--reverse", action="store_true", help="reverse the default sort direction")
    if paging:
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--offset", type=int, default=0)


def _leaf(subs, name, help_text, add_globals):
    parser = subs.add_parser(name, help=help_text)
    add_globals(parser)
    _add_output(parser)
    return parser


def add_parsers(subs, add_globals) -> None:
    usage = subs.add_parser("usage", help="query usage summaries and grouped rollups")
    usage_subs = usage.add_subparsers(dest="action", required=True)
    summary = _leaf(usage_subs, "summary", "summarize matching usage", add_globals)
    _add_query(summary, paging=False)
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
    listing = _leaf(session_subs, "list", "list and filter sessions", add_globals)
    _add_query(listing)
    for action, text in (
        ("get", "show one session and its model usage"),
        ("nodes", "show a session's subagent tree"),
        ("turns", "show a session's accounting timeline"),
        ("tools", "show a session's tool and MCP attribution"),
        ("context", "show a session's context curve and composition"),
        ("content", "read one raw recorded turn trace"),
    ):
        parser = _leaf(session_subs, action, text, add_globals)
        parser.add_argument("session", metavar="SESSION_KEY|ID")
        if action == "turns":
            parser.add_argument("--include-prompts", action="store_true")
            parser.add_argument("--include-content-keys", action="store_true")
            parser.add_argument(
                "--allow-raw-content",
                action="store_true",
                help="permit full prompts and local trace keys",
            )
        if action == "content":
            parser.add_argument("content_key")
            parser.add_argument(
                "--allow-raw-content",
                action="store_true",
                required=True,
                help="explicitly permit prompts, reasoning, tool arguments, and results",
            )

    models = subs.add_parser("models", help="query used models, prices, and comparisons")
    model_subs = models.add_subparsers(dest="action", required=True)
    listing = _leaf(model_subs, "list", "list used models or the price catalog", add_globals)
    _add_query(listing, paging=False, model_search=True)
    listing.add_argument("--catalog", action="store_true")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--offset", type=int, default=0)
    compare = _leaf(model_subs, "compare", "reprice one session at a target model", add_globals)
    compare.add_argument("session", metavar="SESSION_KEY|ID")
    compare.add_argument("target_model", metavar="MODEL")
    for action in ("pin", "unpin"):
        parser = _leaf(model_subs, action, f"{action} a model", add_globals)
        parser.add_argument("model", metavar="MODEL")

    source = subs.add_parser("sources", help="inspect harness discovery")
    source_subs = source.add_subparsers(dest="action", required=True)
    _leaf(source_subs, "list", "list configured and detected harnesses", add_globals)

    notes = subs.add_parser("notes", help="get, set, or delete authored session notes")
    note_subs = notes.add_subparsers(dest="action", required=True)
    for action in ("get", "delete"):
        parser = _leaf(note_subs, action, f"{action} a session note", add_globals)
        parser.add_argument("session", metavar="SESSION_KEY|ID")
    parser = _leaf(note_subs, "set", "set a session note", add_globals)
    parser.add_argument("session", metavar="SESSION_KEY|ID")
    parser.add_argument("text")

    bookmarks = subs.add_parser("bookmarks", help="list or mutate bookmarked sessions")
    bookmark_subs = bookmarks.add_subparsers(dest="action", required=True)
    _leaf(bookmark_subs, "list", "list bookmark ids", add_globals)
    for action in ("add", "remove"):
        parser = _leaf(bookmark_subs, action, f"{action} a bookmark", add_globals)
        parser.add_argument("session", metavar="SESSION_KEY|ID")

    ignore = subs.add_parser("ignore", help="list or mutate ignored sessions and projects")
    ignore_subs = ignore.add_subparsers(dest="kind", required=True)
    _leaf(ignore_subs, "list", "list ignored sessions and projects", add_globals)
    for kind in ("session", "project"):
        kind_parser = ignore_subs.add_parser(kind, help=f"mutate an ignored {kind}")
        kind_subs = kind_parser.add_subparsers(dest="action", required=True)
        for action in ("add", "remove"):
            parser = _leaf(kind_subs, action, f"{action} an ignored {kind}", add_globals)
            parser.add_argument("value", metavar="SESSION_KEY|ID" if kind == "session" else "PATH")

    mcp = subs.add_parser("mcp", help="serve OpenTab tools over MCP on stdio")
    add_globals(mcp)
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
    if getattr(args, "demo", False) or getattr(args, "no_state", False):
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


def command(args) -> int:
    if args.command == "mcp":
        from opentab.mcp import run_server

        return run_server(args)
    from opentab.service import OpenTabService, ServiceError

    try:
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
            if args.command == "usage":
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
                else:
                    data = service.session_content(args.session, args.content_key)
            elif args.command == "models":
                if args.action == "list":
                    data = service.list_models(
                        query_from_args(args),
                        catalog=args.catalog,
                        search=args.model_search,
                        limit=args.limit,
                        offset=args.offset,
                    )
                elif args.action == "compare":
                    data = service.compare_model(args.session, args.target_model)
                else:
                    data = service.mutate_set(
                        "pinned-model", "add" if args.action == "pin" else "remove", args.model
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
                data = service.mutate_set(
                    "ignored-session" if args.kind == "session" else "ignored-project",
                    args.action,
                    args.value,
                )
            else:
                raise ServiceError("unknown_command", f"unsupported command: {args.command}")
        _write(envelope(data), getattr(args, "pretty", False))
        return 0
    except ServiceError as exc:
        _write(error_envelope(exc.code, exc.message, exc.details), getattr(args, "pretty", False))
        return 1
    except SystemExit as exc:
        _write(error_envelope("source_error", str(exc)), getattr(args, "pretty", False))
        return 1
    except (OSError, ValueError) as exc:
        _write(error_envelope("operation_failed", str(exc)), getattr(args, "pretty", False))
        return 1
