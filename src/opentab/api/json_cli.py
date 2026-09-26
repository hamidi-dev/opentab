"""Argument routing and JSON output for OpenTab's programmatic commands."""
from __future__ import annotations

import argparse
import copy
import json
import sys

from opentab import diagnostics, sources
from opentab.accounting.models import API_SCHEMA_VERSION
from opentab.conversations.reader import ConversationError
from opentab.persistence.state import load_state
from opentab.presentation.cli_help import arrange_help

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
    "source": "choose sources to load (default: auto merges present local harnesses); "
    "--source is a deprecated alias; choices and setup: docs/sources.md",
    "no_state": "ignore saved notes, bookmarks, ignores, and pinned models; reject edits to saved data",
    "no_cache": "bypass the accounting rollup cache while loading sessions",
}
_ACCOUNTING_DATES = (
    "Dates select sessions by root start date and include their whole recorded usage, "
    "including descendants and later activity."
)
_SESSION_HELP = "session_key from `opentab sessions list`, or a unique native root ID"
_DOCS = "JSON contract and session keys: docs/programmatic.md\nSource setup: docs/sources.md"
_CONVERSATION_HELP = "OpenCode, Claude Code, Codex, Hermes, Pi, or Omp"


def _parent(subs, name, text, *, example):
    parser = subs.add_parser(
        name,
        help=text[0].lower() + text[1:],
        description=text + ". Subcommands return JSON.",
        epilog=f"Examples:\n  {example}\n\nMore help: %(prog)s COMMAND --help\n{_DOCS}",
    )
    return parser


def _add_output(parser) -> None:
    parser.add_argument(
        "--pretty", action="store_true", help="indent the JSON response; results are unchanged"
    )


def _add_query(
    parser, *, paging: bool = True, model_search: bool = False, sorting: bool = True
) -> None:
    parser.add_argument(
        "--range",
        default="all",
        metavar="RANGE",
        help="all (default), 30d, 2m, 1y, YYYY, YYYY-MM, YYYY-MM-DD (since that date), "
        "or START..END (bounded interval); "
        "overridden by --days, then by --since/--until",
    )
    parser.add_argument("--days", type=int, help="number of days; overrides --range")
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="inclusive root start-date lower bound; overrides --days and --range",
    )
    parser.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="inclusive root start-date upper bound; overrides --days and --range",
    )
    parser.add_argument("--project", help="only sessions in this git project or directory")
    parser.add_argument(
        "--from-harness",
        dest="query_harness",
        metavar="HARNESS",
        help="filter the loaded catalog; never load additional sources (use --harness for that)",
    )
    parser.add_argument("--machine", help="only sessions from this machine")
    parser.add_argument(
        "--model", help="select sessions that used this model, retaining their other models' usage"
    )
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
    parser.add_argument(
        "--include-ignored",
        action="store_true",
        help="include sessions hidden by saved session/project ignores",
    )
    if sorting:
        parser.add_argument(
            "--sort",
            choices=("cost", "tokens", "date", "last_activity", "title", "project"),
            default="cost",
            metavar="KEY",
            help="cost (default: API-equivalent cost), tokens, date (root start), "
            "last_activity: descending; title or project: alphabetical ascending",
        )
        parser.add_argument(
            "--reverse", action="store_true", help="reverse the default sort direction"
        )
    if paging:
        parser.add_argument(
            "--limit", type=int, default=100, help="maximum sessions to return (default: 100)"
        )
        parser.add_argument(
            "--offset", type=int, default=0, help="skip this many sorted sessions (default: 0)"
        )


def _add_global_bundle(parser, add_globals, allowed, help_overrides=None) -> None:
    probe = argparse.ArgumentParser(add_help=False)
    add_globals(probe)
    help_text = {**_GLOBAL_HELP, **(help_overrides or {})}
    defaults = {}
    for action in probe._actions:
        if action.dest not in {*allowed, "debug", "debug_log"}:
            continue
        # Each leaf gets its own Action so argparse cannot leak mutations between parsers.
        copied = copy.copy(action)
        copied.help = help_text.get(action.dest, action.help)
        parser._add_action(copied)
        if action.default is not argparse.SUPPRESS:
            defaults[action.dest] = action.default
    parser.set_defaults(_programmatic_global_defaults=defaults)


def _leaf(
    subs,
    name,
    help_text,
    add_globals,
    globals=(),
    help_overrides=None,
    *,
    description=None,
    epilog=None,
):
    parser = subs.add_parser(
        name,
        help=help_text,
        description=description or (help_text[0].upper() + help_text[1:] + "; output is JSON."),
        epilog=epilog or _DOCS,
    )
    _add_global_bundle(parser, add_globals, globals, help_overrides)
    _add_output(parser)
    return parser


def _add_conversation_catalog(parser) -> None:
    parser.set_defaults(conversation_sources_only=True)
    parser.add_argument(
        "--harness",
        "--source",
        dest="source",
        choices=(*sources.CONVERSATION_LABELS, "all"),
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
    for name, label, kind in (
        ("hermes", "Hermes", "db"),
        ("pi", "pi", "dir"),
        ("omp", "omp", "dir"),
    ):
        parser.add_argument(
            f"--{name}-{kind}",
            default=argparse.SUPPRESS,
            help=f"{label} {'database' if kind == 'db' else 'sessions directory'} path (for --harness {name}/all)",
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
        choices=tuple(sources.CONVERSATION_LABELS),
        help="filter the loaded conversation catalog; never load additional sources",
    )
    parser.add_argument("--machine", help="filter loaded machines; never fetch remote text")
    parser.add_argument("--session", help=_SESSION_HELP)


def add_parsers(subs, add_globals) -> None:
    usage = _parent(
        subs,
        "usage",
        "Query usage summaries and grouped rollups",
        example="opentab usage summary --range 30d --group-by project",
    )
    usage_subs = usage.add_subparsers(dest="action", required=True)
    summary = _leaf(
        usage_subs,
        "summary",
        "summarize matching usage",
        add_globals,
        _STORE_ARGS,
        description="Summarize matching sessions as JSON totals and optional groups. "
        + _ACCOUNTING_DATES,
        epilog="Examples:\n  opentab usage summary --range 30d --group-by project\n\n" + _DOCS,
    )
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
        help="group totals by this dimension (default: none); day/month/year use root "
        "start dates. Groups sort by API-equivalent cost, then tokens, descending",
    )

    sessions = _parent(
        subs,
        "sessions",
        "List sessions and inspect accounting, raw traces, or text-only conversations",
        example="opentab sessions list --range 7d --limit 20",
    )
    session_subs = sessions.add_subparsers(dest="action", required=True)
    listing = _leaf(
        session_subs,
        "list",
        "list and filter sessions",
        add_globals,
        _STORE_ARGS,
        description="List matching sessions as JSON, with session_key identifiers for detail commands. "
        + _ACCOUNTING_DATES,
        epilog="Examples:\n  opentab sessions list --range 7d --from-harness claude\n\nNext: opentab sessions get SESSION_KEY\n"
        + _DOCS,
    )
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
        parser.add_argument("session", metavar="SESSION_KEY|ID", help=_SESSION_HELP)
        example = f"opentab sessions {action} SESSION_KEY"
        if action == "content":
            example += " CONTENT_KEY --allow-raw-content"
        parser.epilog = (
            "Examples (replace keys with returned values):\n  " + example + "\n\n" + _DOCS
        )
        if action == "turns":
            parser.add_argument(
                "--include-prompts",
                action="store_true",
                help="include full recorded prompts; requires --allow-raw-content",
            )
            parser.add_argument(
                "--include-content-keys",
                action="store_true",
                help="include opaque trace identifiers without fetching trace text; requires --allow-raw-content",
            )
            parser.add_argument(
                "--allow-raw-content",
                action="store_true",
                help="permit full prompts and local trace keys",
            )
        if action in {"content", "conversation"}:
            if action == "content":
                parser.add_argument(
                    "content_key",
                    metavar="CONTENT_KEY",
                    help="trace key from `sessions turns --include-content-keys --allow-raw-content`",
                )
                parser.description += " Reads prompts, reasoning, tool arguments and results; managed remote trace reads can connect over SSH."
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
            parser.description = (
                "Read retained user and assistant text as bounded JSON records from local "
                f"{_CONVERSATION_HELP} sessions. Default: root execution only, "
                "without descendants; use an exact child execution ID to read a child. "
                "This text-only view does not reconstruct missing history or the active branch."
            )
            parser.epilog = (
                "Examples (replace SESSION_KEY and ANCHOR with returned values):\n"
                "  opentab sessions conversation SESSION_KEY --allow-raw-content\n"
                "  opentab sessions conversation SESSION_KEY --allow-raw-content --anchor ANCHOR\n\n"
                "Use session_key from sessions list or search results; child search hits also\n"
                "need --execution-id with the returned exact ID. Continue with --cursor.\n"
                "Reader and search workflow: docs/conversation-search.md\n" + _DOCS
            )
            parser.add_argument(
                "--execution-id",
                help="exact child id from response.executions; default is root only",
            )
            selector = parser.add_mutually_exclusive_group()
            selector.add_argument(
                "--anchor",
                help="record anchor from a previous read or search hit in this execution",
            )
            selector.add_argument(
                "--cursor",
                help="opaque next_cursor from a previous read of this execution and source snapshot",
            )
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
                help="records before anchor (0..99; default: 0; nonzero requires --anchor)",
            )

    conversations = _parent(
        subs,
        "conversations",
        "Explicit local plaintext conversation indexing and search",
        example="opentab conversations status",
    )
    conversations.description += (
        " First index explicitly (writes sensitive plaintext locally), then search the "
        "existing index and read a hit with sessions conversation. Local OpenCode, "
        "Claude Code, Codex, Hermes, Pi, and Omp only; saved ignores and retained-source "
        "limits apply. "
        "Search never refreshes the index automatically."
    )
    conversations.epilog = (
        "Workflow:\n"
        "  opentab conversations index --harness all --allow-raw-content\n"
        '  opentab conversations search "cache configuration" --allow-raw-content\n'
        "  opentab sessions conversation SESSION_KEY --allow-raw-content --anchor ANCHOR\n\n"
        "SESSION_KEY and ANCHOR are placeholders from search results. Child hits also\n"
        "require --execution-id with their exact execution ID.\n"
        "More help: opentab conversations COMMAND --help\n"
        "Workflow, privacy and freshness: docs/conversation-search.md"
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
        notes = {
            "index": " Writes sensitive plaintext locally for retained OpenCode, Claude Code, Codex, "
            "Hermes, Pi, and Omp "
            "sessions. Saved ignores apply. Inspect complete, errors and unsupported: a finished "
            "refresh can be partial and never guarantees complete history.",
            "search": " Requires an existing index; never refreshes it automatically. Saved ignores "
            "apply, selected evidence is verified against local sources, and stale or missing "
            "coverage is reported. Only retained OpenCode, Claude Code, Codex, Hermes, Pi, and Omp "
            "text is supported.",
            "status": " Read-only: does not create an index, discover sources, or read conversations.",
            "clear": " Clears indexed text only, leaving source records and authored notes intact. "
            "Leaves an empty database file; this is not secure erasure. Does not discover sources.",
        }
        examples = {
            "index": "opentab conversations index --harness all --allow-raw-content",
            "search": 'opentab conversations search "cache configuration" --allow-raw-content',
            "status": "opentab conversations status",
            "clear": "opentab conversations clear --allow-raw-content",
        }
        parser = conversation_subs.add_parser(
            action,
            help=text,
            description=text[0].upper() + text[1:] + "; output is JSON." + notes[action],
            epilog="Examples:\n  " + examples[action] + "\n\n"
            "Index/search/read workflow: opentab conversations --help\n"
            "Privacy, source limits and freshness: docs/conversation-search.md",
        )
        parser.add_argument(
            "--debug",
            action="store_true",
            help="flush indexing/search stages and timings to stderr and a private JSONL log",
        )
        parser.add_argument(
            "--debug-log", metavar="FILE", help="write diagnostics to a new file (implies --debug)"
        )
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

    models = _parent(
        subs,
        "models",
        "Query used models, catalog prices, and session-only comparisons",
        example="opentab models list --catalog --search sonnet",
    )
    model_subs = models.add_subparsers(dest="action", required=True)
    listing = _leaf(
        model_subs, "list", "list used models or the price catalog", add_globals, _STORE_ARGS
    )
    _add_query(listing, paging=False, model_search=True, sorting=False)
    listing.description += (
        " Default: models used by matching sessions, ordered by API-equivalent cost, "
        "then tokens, descending. Catalog mode lists known prices alphabetically without "
        "opening sessions; use --search, --limit, --offset, --no-state and --pretty. "
        "Nondefault session/source selections are rejected in catalog mode. " + _ACCOUNTING_DATES
    )
    listing.epilog = (
        "Examples:\n  opentab models list --range 30d\n  opentab models list --catalog --search sonnet\n\n"
        + _DOCS
    )
    listing.add_argument(
        "--catalog",
        action="store_true",
        help="list the price catalog instead of models used by sessions",
    )
    listing.add_argument(
        "--limit", type=int, default=100, help="maximum models to return (default: 100)"
    )
    listing.add_argument(
        "--offset", type=int, default=0, help="skip this many ordered models (default: 0)"
    )
    compare = _leaf(
        model_subs,
        "compare",
        "calculate a hypothetical rate comparison for one session without changing recorded costs",
        add_globals,
        _READ_STORE_ARGS,
    )
    compare.add_argument("session", metavar="SESSION_KEY|ID", help=_SESSION_HELP)
    compare.add_argument(
        "target_model",
        metavar="MODEL",
        help="target model identifier from `opentab models list --catalog`",
    )
    compare.epilog = "Examples:\n  opentab models compare SESSION_KEY openai/gpt-5\n\n" + _DOCS
    for action in ("pin", "unpin"):
        text = "add a model to saved pins" if action == "pin" else "remove a model from saved pins"
        parser = _leaf(model_subs, action, text, add_globals, _STATE_ARGS)
        parser.add_argument(
            "model",
            metavar="MODEL",
            help="model identifier to pin/unpin; does not change usage or model rates",
        )
        parser.epilog = f"Examples:\n  opentab models {action} openai/gpt-5\n\n" + _DOCS

    source = _parent(subs, "sources", "Inspect harness discovery", example="opentab sources list")
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
        description="List configured harness names, labels, presence and the selected source as JSON. "
        "Does not read conversation text. Source paths and environment overrides: docs/sources.md.",
        epilog="Examples:\n  opentab sources list --pretty\n\n" + _DOCS,
    )

    notes = _parent(
        subs,
        "notes",
        "Get, replace, or delete authored session notes",
        example="opentab notes get SESSION_KEY",
    )
    note_subs = notes.add_subparsers(dest="action", required=True)
    for action in ("get", "delete"):
        text = (
            "read the authored note (empty if absent)"
            if action == "get"
            else "delete the authored note, keeping the session and harness records"
        )
        parser = _leaf(note_subs, action, text, add_globals, _STORE_ARGS)
        parser.add_argument("session", metavar="SESSION_KEY|ID", help=_SESSION_HELP)
        parser.epilog = f"Examples:\n  opentab notes {action} SESSION_KEY\n\n" + _DOCS
    parser = _leaf(
        note_subs, "set", "replace the authored note for a session", add_globals, _STORE_ARGS
    )
    parser.add_argument("session", metavar="SESSION_KEY|ID", help=_SESSION_HELP)
    parser.add_argument(
        "text",
        metavar="TEXT",
        help="complete replacement note, up to 500 characters (quote text containing spaces); empty text deletes the note",
    )
    parser.epilog = (
        'Examples:\n  opentab notes set SESSION_KEY "investigate cache churn"\n\n' + _DOCS
    )

    bookmarks = _parent(
        subs,
        "bookmarks",
        "List, add, or remove saved session bookmarks",
        example="opentab bookmarks add SESSION_KEY",
    )
    bookmark_subs = bookmarks.add_subparsers(dest="action", required=True)
    _leaf(
        bookmark_subs,
        "list",
        "list saved bookmark identifiers",
        add_globals,
        _STATE_ARGS,
        epilog="Examples:\n  opentab bookmarks list\n\n" + _DOCS,
    )
    for action in ("add", "remove"):
        parser = _leaf(bookmark_subs, action, f"{action} a bookmark", add_globals, _STORE_ARGS)
        parser.add_argument("session", metavar="SESSION_KEY|ID", help=_SESSION_HELP)
        parser.epilog = f"Examples:\n  opentab bookmarks {action} SESSION_KEY\n\n" + _DOCS

    ignore = _parent(
        subs,
        "ignore",
        "List, add, or remove saved session/project ignores without deleting source data",
        example="opentab ignore project add ./generated-client",
    )
    ignore_subs = ignore.add_subparsers(dest="kind", required=True)
    _leaf(
        ignore_subs,
        "list",
        "list saved ignored session identifiers and project paths",
        add_globals,
        _STATE_ARGS,
        epilog="Examples:\n  opentab ignore list\n\n" + _DOCS,
    )
    for kind in ("session", "project"):
        kind_parser = _parent(
            ignore_subs,
            kind,
            f"Add or remove a saved {kind} ignore; source data stays intact",
            example=f"opentab ignore {kind} add "
            + ("SESSION_KEY" if kind == "session" else "./generated-client"),
        )
        kind_subs = kind_parser.add_subparsers(dest="action", required=True)
        for action in ("add", "remove"):
            parser = _leaf(
                kind_subs,
                action,
                f"{action} an ignored {kind}",
                add_globals,
                _STORE_ARGS if kind == "session" else _STATE_ARGS,
            )
            parser.description += " Changes saved visibility, never deletes source records."
            target = "SESSION_KEY" if kind == "session" else "./generated-client"
            parser.epilog = f"Examples:\n  opentab ignore {kind} {action} {target}\n\n" + _DOCS
            parser.add_argument(
                "value",
                metavar="SESSION_KEY|ID" if kind == "session" else "PATH",
                help=_SESSION_HELP
                if kind == "session"
                else "project directory to hide/show in normal queries and views",
            )

    mcp = subs.add_parser(
        "mcp",
        help="serve OpenTab tools over MCP on stdio",
        description="Serve OpenTab tools as a client-launched stdio MCP process; no HTTP listener. "
        "Normal tools query usage, sessions, models and manage saved preferences. "
        "Raw traces, conversation reads, search and indexing require --allow-raw-content "
        "plus each tool's confirmation. Permission does not automatically index anything.",
        epilog="Examples (configure your MCP client to launch this process):\n"
        "  opentab mcp\n\nClient JSON configuration and raw-content setup: docs/programmatic.md",
    )
    _add_global_bundle(mcp, add_globals, _STORE_ARGS)
    mcp.add_argument(
        "--allow-raw-content",
        action="store_true",
        help="allow tools to expose prompts, reasoning, commands, and tool output",
    )

    # Everyday help is a task guide; the full reference retains all details above.
    query_options = {
        "range",
        "project",
        "query_harness",
        "model",
        "search",
        "model_search",
        "pretty",
    }
    common = {
        "usage summary": query_options | {"group_by"},
        "sessions list": query_options | {"limit", "sort"},
        "models list": {"catalog", "model_search", "range", "project", "limit", "pretty"},
        "sessions conversation": {"execution_id", "anchor", "cursor", "tail", "limit", "pretty"},
        "conversations index": {"source", "project", "session", "rebuild", "pretty"},
        "conversations search": {
            "project",
            "session",
            "since",
            "until",
            "limit",
            "pretty",
        },
    }
    summaries = {
        "usage summary": "Summarize usage as JSON totals, optionally grouped. Dates select whole\n"
        "sessions by root start, including descendants and later activity.",
        "sessions list": "Find sessions and their session_key identifiers. Output: JSON.\n"
        "Dates select whole sessions by root start, including descendants and later activity.",
        "models list": "List models used by matching sessions, or browse prices with --catalog.\n"
        "Output: JSON. Catalog mode supports search and pagination, not session filters.",
        "sessions conversation": f"Read retained user/assistant text as JSON: local {_CONVERSATION_HELP}.\n"
        "Root execution only by default; child hits need their exact --execution-id.\n"
        "Use SESSION_KEY and ANCHOR from search results; cursors continue the same read.",
        "conversations": "Index locally, search, then read a matching conversation.\n"
        "Indexing saves sensitive plaintext. Search never refreshes it automatically.\n"
        "Replace SESSION_KEY/ANCHOR from results; child hits also need --execution-id.",
        "conversations index": f"Build or refresh a local index of {_CONVERSATION_HELP} text.\n"
        "Writes sensitive plaintext; saved ignores apply. JSON reports partial failures.",
        "conversations search": "Search an existing local conversation index; output is JSON.\n"
        "No automatic refresh. Dates filter UTC messages; stale evidence is withheld.",
        "conversations status": "Show local index counts as JSON. Read-only; no source discovery or text reads.",
        "conversations clear": "Clear the local text index, keeping original records and notes. Output: JSON.\n"
        "Leaves an empty database; not secure erasure.",
        "sources list": "Show detected harnesses and the selected source as JSON.",
        "mcp": "Connect your MCP client to OpenTab over stdio. No HTTP listener.\n"
        "Raw-content tools require opt-in and confirmation; nothing is auto-indexed.",
    }
    brief = {
        "source": "Sources to load (default: auto).",
        "remotes": "Read saved machine summaries from this path (no fetch).",
        "pretty": "Indent JSON output.",
        "range": "30d, YYYY-MM, YYYY-MM-DD (since), START..END; default: all.",
        "project": "Only sessions in this project or directory.",
        "query_harness": "Filter loaded sessions by harness; does not load more sources.",
        "model": "Sessions using this model; keeps their other models' usage.",
        "search": "Find sessions by title, project, ID or note.",
        "model_search": "Find model names (case-insensitive substring).",
        "group_by": "none (default), day, month, year, project, harness, machine, model, provider.",
        "sort": "cost (default, API-equivalent), tokens, date, last_activity, title, project.",
        "session": "session_key from sessions list, or a unique native root ID.",
        "text": "Replacement note, up to 500 characters; empty text deletes it.",
        "content_key": "Trace key from sessions turns --include-content-keys.",
        "execution_id": "Exact child execution ID (default: root only).",
        "anchor": "Start at a returned record anchor.",
        "cursor": "Continue with the previous response's next_cursor.",
        "catalog": "Browse the price catalog instead of used models.",
    }

    # Finalize display only after all command-specific options have been registered.
    def arrange_tree(parser):
        path = parser.prog.partition(" ")[2]
        prefs = path.startswith(("notes ", "bookmarks ", "ignore ", "models pin", "models unpin"))
        overrides = dict(brief)
        if path.startswith("conversations "):
            overrides["source"] = ", ".join(sources.CONVERSATION_LABELS) + ", or all (default)."
        if path.startswith("models "):
            overrides.pop("model", None)  # positional model names are not session filters
        arrange_help(
            parser,
            source_paths=_SOURCE_PATH_ARGS - {"source"},
            common=common.get(path, {"pretty"} if prefs else None),
            summary=summaries.get(path),
            brief=overrides,
        )
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    arrange_tree(child)

    for parser in (usage, sessions, conversations, models, source, notes, bookmarks, ignore, mcp):
        arrange_tree(parser)


def _range(args) -> str:
    if getattr(args, "since", None) or getattr(args, "until", None):
        return f"{getattr(args, 'since', None) or ''}..{getattr(args, 'until', None) or ''}"
    if getattr(args, "days", None) is not None:
        return f"{args.days}d"
    return getattr(args, "range", "all")


def query_from_args(args):
    from opentab.api.service import SessionQuery

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
        from opentab.api.mcp import run_server

        return run_server(args)
    from opentab.api.service import OpenTabService, ServiceError

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
                from opentab.conversations import index

                data = index.index_status() if args.action == "status" else index.clear_index()
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
            if args.command == "conversations":
                diagnostics.progress("conversations.catalog.start", action=args.action)
            with diagnostics.span("conversations.catalog.open"):
                service = OpenTabService.open(
                    args, allow_raw_content=bool(getattr(args, "allow_raw_content", False))
                )
            if args.command == "conversations":
                diagnostics.progress("conversations.catalog.ready", action=args.action)
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
