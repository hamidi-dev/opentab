from __future__ import annotations

import argparse
import contextlib
import json
import locale
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

from opentab import __version__, paths, sources, themes
from opentab.demo import DEMO_CATEGORIES, demo_config, demo_machine
from opentab.formatting import (
    cost_bar,
    human_bytes,
    human_tokens,
    money,
    relative_age,
    short_path,
)
from opentab.pricing import (
    MODELS_DEV_URL,
    api_equivalent_cost,
    price_cache_path,
    refresh_model_prices,
)
from opentab.sources import (
    DEFAULT_CSV_PATH,
    DEFAULT_JSONL_PATH,
    SOURCE_LABELS,
    _default_antigravity_dir,
    _default_gemini_dir,
    _default_bahulam_dir,
    _default_omp_dir,
    _default_openclaw_dir,
    _default_pi_dir,
    _default_zaly_dir,
    _route_path_arg,
    default_remotes_dir,
    resolve_source,
)
from opentab.state import apply_state, load_state, save_state
from opentab.stores.claude import (
    CLAUDE_RETENTION_RECOMMENDED_DAYS,
    CLAUDE_RETENTION_WARNING_ID,
    claude_projects_dir,
    claude_retention,
)
from opentab.stores.gemini import (
    GEMINI_RETENTION_DEFAULT_DAYS,
    GEMINI_RETENTION_WARNING_ID,
    gemini_max_count_label,
    gemini_retention,
)
from opentab.tui import bindings
from opentab.tui.app import App
from opentab.util import (
    git_root,
    init_color_allowed,
    node_1h_write,
    resolve_project_root,
    unicode_screen,
)

# Global options are copied onto each subcommand. Legacy verb flags remain on the
# implicit `tui` command; new verbs own their options.


def _add_global_args(parser: argparse.ArgumentParser) -> None:
    # Keep --version order-independent after the implicit `tui` prepend, while
    # preserving the old "opentab X.Y.Z" output.
    parser.add_argument("--version", action="version", version=f"opentab {__version__}")
    parser.add_argument(
        "--harness",
        "--source",  # deprecated alias, kept working; dest stays `source` internally
        dest="source",
        metavar="HARNESS",
        choices=(
            "auto",
            "opencode",
            "claude",
            "codex",
            "hermes",
            "csv",
            "jsonl",
            "copilot",
            "vscode",
            "pi",
            "omp",
            "openclaw",
            "zaly",
            "gemini",
            "antigravity",
            "bahulam",
            "all",
            "remote",
        ),
        default="auto",
        help="which harness's spend to browse: opencode · claude · codex · hermes · csv · "
        "jsonl · copilot · vscode · pi · omp · openclaw · zaly · gemini · antigravity · "
        "all (merged) · "
        "remote "
        "(other machines, via pull/export). Default auto merges every present local "
        "harness. Or just pass a file path -- e.g. `opentab requests.csv`. (--source is a "
        "deprecated alias for --harness)",
    )
    parser.add_argument("--db", default=os.path.expanduser("~/.local/share/opencode/opencode.db"))
    parser.add_argument(
        "--claude-dir",
        default=claude_projects_dir(),
        help="Claude Code projects directory (for --harness claude)",
    )
    parser.add_argument(
        "--codex-dir",
        default=os.path.expanduser("~/.codex/sessions"),
        help="Codex CLI sessions directory (for --harness codex)",
    )
    parser.add_argument(
        "--hermes-db",
        default=os.path.expanduser("~/.hermes/state.db"),
        help="Hermes Agent database path (for --harness hermes)",
    )
    parser.add_argument(
        "--copilot-dir",
        default=os.path.expanduser("~/.copilot/otel"),
        help="GitHub Copilot CLI OpenTelemetry export directory (for --harness copilot); "
        "the file named by $COPILOT_OTEL_FILE_EXPORTER_PATH is also read",
    )
    parser.add_argument(
        "--vscode-dir",
        default=None,
        help="a VS Code User directory (or chatSessions directory) holding Copilot Chat "
        "sessions (for --harness vscode); by default every installed variant (Code, "
        "Code - Insiders, VSCodium) is scanned. From WSL, point it at the Windows-side "
        "store (not scanned by default -- reading through /mnt/c slows startup), e.g. "
        "alias opentab='opentab --vscode-dir \"/mnt/c/Users/<you>/AppData/Roaming/Code/User\"'",
    )
    parser.add_argument(
        "--pi-dir",
        default=_default_pi_dir(),
        help="pi-agent sessions directory (for --harness pi); honors $PI_AGENT_DIR, "
        "default ~/.pi/agent/sessions",
    )
    parser.add_argument(
        "--omp-dir",
        default=_default_omp_dir(),
        help="omp sessions directory (for --harness omp; omp is a pi-agent fork and "
        "reads the same records); honors $OMP_AGENT_DIR (opentab's own override -- omp "
        "itself has no session-dir env var), default ~/.omp/agent/sessions",
    )
    parser.add_argument(
        "--openclaw-dir",
        default=_default_openclaw_dir(),
        help="OpenClaw gateway home holding agents/ and openclaw.json (for --harness "
        "openclaw); honors $OPENCLAW_DIR, default ~/.openclaw",
    )
    parser.add_argument(
        "--zaly-dir",
        default=_default_zaly_dir(),
        help="Zaly data directory holding sessions/ (for --harness zaly); honors "
        "$ZALY_DATA and $ZALY_ROOT, default ~/.local/share/zaly",
    )
    parser.add_argument(
        "--gemini-dir",
        default=_default_gemini_dir(),
        help="Gemini CLI home directory holding tmp/*/chats/ (for --harness gemini); "
        "honors $GEMINI_CLI_HOME, default ~/.gemini",
    )
    parser.add_argument(
        "--antigravity-dir",
        default=_default_antigravity_dir(),
        help="Gemini home directory holding antigravity/conversations/*.db (for "
        "--harness antigravity); honors $GEMINI_CLI_HOME, default ~/.gemini",
    )
    parser.add_argument(
        "--bahulam-dir",
        default=_default_bahulam_dir(),
        help="Bahulam Code projects directory (for --harness bahulam); "
        "honors $BAHULAM_PROJECTS_DIR, default ~/.bahulam/projects",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="path to a CSV of logged API requests, e.g. GitHub Copilot; selects the "
        f"csv source. Auto-discovered at {DEFAULT_CSV_PATH} if present",
    )
    parser.add_argument(
        "--jsonl",
        default=None,
        help="path to an NDJSON file of logged API requests (one JSON object per line); "
        f"selects the jsonl source. Auto-discovered at {DEFAULT_JSONL_PATH} if present",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="initial range in days (default: all time; change live with R)",
    )
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument(
        "--demo",
        nargs="?",
        const="all",
        default=None,
        metavar="CATS",
        help="anonymize for live demos and screenshots (never writes to the DB). Bare "
        "--demo scrambles everything; a comma list limits it to some of titles "
        "(session/prompt/model/machine names), paths (project directories), turns (the "
        "expandable full prompt text), spend (dollars + token magnitudes) -- e.g. "
        "--demo titles,spend shows real project paths and prompt bodies but fake "
        "session names and hidden costs. Toggle live in the TUI with D",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="do not read or write the saved range/sort state ($XDG_STATE_HOME/opentab)",
    )
    parser.add_argument(
        "--no-worktrees",
        action="store_true",
        help="do not fold git worktrees into their main repo (keep each path separate)",
    )
    parser.add_argument(
        "--remotes",
        default=None,
        metavar="DIR",
        help="directory of exported machine summaries for --source remote "
        f"(default: {default_remotes_dir()}); one *.json per machine, from --export/--pull",
    )
    parser.add_argument(
        "--label",
        default=None,
        metavar="NAME",
        help="machine name recorded in --export (default: this host's name); how the "
        "session shows up under --source remote when several machines are merged",
    )
    parser.add_argument(
        "--theme",
        choices=themes.THEME_IDS,
        default=themes.DEFAULT_THEME,
        metavar="THEME",  # Hide the 30-name choices wall; argparse still validates it.
        help="colour theme for the TUI and the web browser (opentab, "
        "catppuccin-mocha/latte, tokyo-night/-day, gruvbox, nord, dracula, rose-pine); "
        "switch live in the TUI with C or the browser's theme button, and your choice is "
        f"remembered. Default: {themes.DEFAULT_THEME}",
    )
    parser.add_argument(
        "--port", type=int, default=8321, help="port for `opentab web` (default: 8321)"
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="address for `opentab web` (default: 127.0.0.1). The browser exposes prompt "
        "titles, project paths, and spend -- bind beyond localhost only on a "
        "trusted/VPN (e.g. Tailscale) interface, never a public one",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="skip the warm-start rollup cache and always re-parse from scratch. The "
        "cache (under $XDG_CACHE_HOME/opentab) reuses the previous parse when a backend's "
        "files are unchanged; use this to force a cold read or to measure it",
    )


def _add_legacy_command_flags(parser: argparse.ArgumentParser) -> None:
    # Deprecated aliases stay functional so existing scripts and tmux bindings survive
    # the move to subcommands.
    parser.add_argument(
        "--status",
        nargs="?",
        const="",
        default=None,
        metavar="DIR|SESSION",
        help="print the cost of the most recently active agent session (subagent "
        "subtree included) and exit, consulting every present harness backend "
        "(OpenCode, Claude Code, Codex, Hermes, pi, omp, OpenClaw, Zaly, Gemini); with DIR "
        "only "
        "(OpenCode, Claude Code, Codex, Hermes, pi, omp, OpenClaw, Zaly, Bahulam Code); with DIR only "
        "sessions of that project count, with a session id (ses_... or a UUID -- the "
        "id is matched to its own backend) exactly that session is priced, and "
        "--harness pins one backend. Made for a tmux status line: set -g "
        "status-right '#(opentab cost \"#{pane_current_path}\")'. A leading ~ "
        "marks a list-price estimate for usage recorded at $0 (subscription models). "
        "Deprecated alias for `opentab cost`",
    )
    parser.add_argument(
        "--goto",
        nargs="?",
        const="",
        default=None,
        metavar="DIR|SESSION",
        help="open the TUI drilled straight into a session: a session id opens "
        "exactly that session (a subagent id resolves to its root), a DIR (default: "
        "the current directory) opens the project's most recently active session -- "
        "resolved across every present harness backend like `cost`. Made for a "
        "tmux binding: bind t run 'tmux popup -E \"opentab --goto "
        "#{pane_current_path}\"'",
    )
    parser.add_argument(
        "--tab",
        metavar="NAME",
        default=None,
        help="with --goto, land on this session tab instead of Overview: overview, "
        "subagents, turns, tools, or context (case-insensitive; a tab the session's "
        "backend doesn't have keeps Overview and says so). Implies --goto of the "
        "current directory when --goto is absent -- so `opentab --tab context` jumps "
        "straight to the context curve of the cwd's live session. Made for a tmux "
        "binding: bind t run 'tmux popup -E \"opentab --goto #{pane_current_path} "
        "--tab context\"'",
    )
    parser.add_argument(
        "--export",
        nargs="?",
        const="-",
        default=None,
        metavar="FILE",
        help="write this machine's spend summary (every present harness, merged) as a "
        "portable JSON file and exit -- totals + per-model breakdown, no transcripts. "
        "Default FILE is stdout, so `ssh box opentab --export - > box.json` works. "
        "Gather these from several machines and browse them merged with `opentab "
        "--source remote` (see --remotes); pairs with --demo for a shareable summary",
    )
    parser.add_argument(
        "--pull",
        nargs="*",
        default=None,
        metavar="HOST",
        help="fetch other machines' spend summaries over SSH (all in parallel) and open "
        "them merged (--source remote). HOST is an ssh target -- `box`, `user@host`, "
        "`name=user@host`, or `http://host:port` for an `opentab --serve` box; each is "
        "remembered in remotes.json, so a later bare `opentab --pull` refreshes them all. "
        "Needs opentab on the remote (it runs `opentab --export -` there via SSH -- "
        "nothing has to be listening); set a machine's `cmd` in remotes.json if opentab "
        "isn't on its non-interactive PATH",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="open the already-pulled machine summaries (--source remote) without "
        "re-fetching -- the offline twin of --pull",
    )
    parser.add_argument(
        "--forget",
        nargs="+",
        default=None,
        metavar="NAME",
        help="remove machines from remotes.json (and delete their cached summaries under "
        "--remotes), then exit",
    )
    parser.add_argument(
        "--html",
        nargs="?",
        const="opentab-report.html",
        default=None,
        metavar="FILE",
        help="write a self-contained HTML browser and exit (deprecated alias for "
        "`opentab web --html`): drill-in by month/day/project/session, calendar heat "
        "map, sortable tables, the $ what-if toggle -- all client-side in one file "
        "(default FILE: opentab-report.html). Pairs with --demo for a shareable page",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="serve the HTML browser from a local web server (deprecated alias for "
        "`opentab web --headless`); adds the per-session Turns/Tools drill-in as live "
        "endpoints and a data-refresh button (Ctrl-C stops it)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="like --serve, but also open it in your default web browser (deprecated "
        "alias for `opentab web`; cross-platform via the stdlib webbrowser: `open` on "
        "macOS, `xdg-open` on Linux, the shell association on Windows); honors "
        "--port/--bind",
    )
    parser.add_argument(
        "--refresh-models",
        action="store_true",
        help="fetch the latest model list prices from models.dev into a local cache "
        f"({price_cache_path()}) and exit; the cache overlays the embedded table for the "
        "$ what-if estimate (also available with 'r' in the P prices overlay)",
    )
    parser.add_argument(
        "--timings",
        action="store_true",
        help="profile startup: print how long source detection, store build, and each "
        "backend's parse/scan take, then exit (no curses -- works on native Windows). "
        "Handy for measuring the file-heavy backends on a slow filesystem",
    )
    parser.add_argument(
        "--keymap",
        action="store_true",
        help="print the path of the keymap config (installing the commented default "
        "on first use) and exit; every key the TUI answers to is remappable there, "
        "and K inside opentab opens it in $EDITOR with a live reload",
    )


# Everything not named here is routed through the implicit `tui` command.
_SUBCOMMANDS = ("tui", "web", "cost", "doctor", "pull", "remote", "export", "forget")


def _focus_help(subparser: argparse.ArgumentParser, common_dests: set, keep: set) -> None:
    # Every verb accepts globals for a complete namespace, but advertises only relevant
    # ones. `opentab tui -h` remains the full reference.
    for action in subparser._actions:
        if action.dest in common_dests and action.dest not in keep:
            action.help = argparse.SUPPRESS


def _build_parser() -> argparse.ArgumentParser:
    # A shared argparse parent would reuse Action objects, so hiding help on one verb
    # would affect the others. This probe only identifies global destinations.
    probe = argparse.ArgumentParser(add_help=False)
    _add_global_args(probe)
    gdests = {a.dest for a in probe._actions if a.dest not in ("help", "version")}
    parser = argparse.ArgumentParser(
        prog="opentab", description="OpenTab — browse your AI-coding spend"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # Keep one complete namespace for legacy dispatch, App/state, and path routing.
    parser.set_defaults(
        path=None,
        status=None,
        status_targets=[],
        status_batch=False,
        goto=None,
        tab=None,
        export=None,
        pull=None,
        remote=False,
        forget=None,
        html=None,
        serve=False,
        web=False,
        refresh_models=False,
        timings=False,
        keymap=False,
    )
    subs = parser.add_subparsers(dest="command", metavar="COMMAND")
    tui = subs.add_parser(
        "tui",
        help="browse spend in the terminal (the default when no command is named)",
        description="Browse AI-coding spend in a terminal UI. This is the default "
        "command, so a bare `opentab` and `opentab <file>` run it without naming it, "
        "and the old top-level flags (--web, --status, --pull, ...) still work here.",
    )
    _add_global_args(tui)
    tui.add_argument(
        "path",
        nargs="?",
        default=None,
        metavar="PATH",
        help="a CSV file, an OpenCode .db, etc. to view -- its harness is picked "
        "automatically (e.g. `opentab requests.csv`). Same as passing the matching "
        "--csv/--db flag; with --harness it fills that harness's path.",
    )
    _add_legacy_command_flags(tui)
    web = subs.add_parser(
        "web",
        help="open the spend browser in your web browser (serve + open)",
        description="Serve the self-contained HTML spend browser and open it in your "
        "default browser. Add --headless to serve without opening one, or --html FILE "
        "to write the static page and exit instead of serving.",
    )
    _add_global_args(web)
    web.add_argument(
        "--html",
        nargs="?",
        const="opentab-report.html",
        default=None,
        metavar="FILE",
        help="write a self-contained HTML file and exit, instead of serving it live "
        "(default FILE: opentab-report.html). Pairs with --demo for a shareable page",
    )
    web.add_argument(
        "--headless",
        action="store_true",
        help="serve but do NOT open a browser (Ctrl-C stops it); the bare `opentab web` "
        "opens one. Either way the live per-session Turns/Tools endpoints and the "
        "data-refresh button are served",
    )
    _focus_help(web, gdests, {"source", "demo", "theme", "port", "bind"})
    # The verb is `cost` to avoid colliding with agent working/waiting status;
    # the established `--status` flag remains compatible.
    status = subs.add_parser(
        "cost",
        help="print one line of cost for a session or project (for a status bar)",
        description="Print the cost of an agent session (subagent subtree included) and "
        "exit -- the one-shot made for a tmux status line. With no TARGET the current "
        "directory's most recently active session is priced; every present harness "
        "backend is consulted unless --harness pins one. Several targets (or --batch) "
        "print a `<target>\\t<price>` table instead of one bare line, priced in a single "
        "process -- which is the point: the interpreter start dwarfs the pricing, so a "
        "shell loop calling this once per pane pays it once per pane.",
    )
    _add_global_args(status)
    status.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help="a directory (price that project's latest session) or a session id "
        "(price exactly that one -- a subagent id resolves to its root). None given = "
        "the current directory. Several = the keyed table; `-` reads the list from "
        "stdin, one target per line",
    )
    status.add_argument(
        "--batch",
        action="store_true",
        help="print the `<target>\\t<price>` table even for a single target, so a "
        "script's parsing doesn't change with the number of targets it happened to "
        "collect. Targets that can't be priced are omitted, so an empty table means "
        "nothing matched",
    )
    _focus_help(status, gdests, {"source", "demo"})
    doctor = subs.add_parser(
        "doctor",
        help="report the environment, the harnesses found, and what's misconfigured",
        description="Print a health report -- this opentab and Python, every harness "
        "backend and why one isn't showing up, the terminal's colour/glyph capabilities, "
        "the price catalog, and opentab's own files -- then exit. Made to be pasted into "
        "a bug report: paths are folded to ~ and machine names are counted rather than "
        "named, no transcript is ever read, and nothing is created or repaired. Exits 1 "
        "if something is actually broken (a warning alone doesn't).",
    )
    _add_global_args(doctor)
    doctor.add_argument(
        "--full",
        action="store_true",
        help="don't redact: print absolute paths and the names of pulled machines. For "
        "reading yourself, not for pasting into a public issue",
    )
    # Doctor must advertise every path override it can diagnose.
    _focus_help(
        doctor,
        gdests,
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
            "bahulam_dir",
            "csv",
            "jsonl",
            "remotes",
        },
    )
    # Fleet subcommands map to legacy fields so both interfaces share one dispatch path.
    pull = subs.add_parser(
        "pull",
        help="fetch other machines' spend over SSH and open them merged",
        description="Fetch other machines' spend summaries over SSH (all in parallel) and "
        "open them merged (the fleet view). Each HOST is remembered, so a later bare "
        "`opentab pull` refreshes every saved machine. The remote just needs opentab on "
        "its PATH -- it runs `opentab export -` there; nothing has to be listening.",
    )
    _add_global_args(pull)
    pull.add_argument(
        "hosts",
        nargs="*",
        metavar="HOST",
        help="an ssh target -- `box`, `user@host`, `name=user@host`, or "
        "`http://host:port` for an `opentab web` box. None given = refresh every machine "
        "already in remotes.json. Set a machine's `cmd` in remotes.json if opentab isn't "
        "on its non-interactive PATH",
    )
    _focus_help(pull, gdests, {"remotes", "demo"})
    remote = subs.add_parser(
        "remote",
        help="open the already-pulled machines without re-fetching (offline)",
        description="Open the machine summaries already gathered by `opentab pull`, "
        "merged into the fleet view, without re-fetching over SSH -- the offline twin of "
        "`opentab pull`.",
    )
    _add_global_args(remote)
    _focus_help(remote, gdests, {"remotes", "demo"})
    export = subs.add_parser(
        "export",
        help="write this machine's spend summary as portable JSON",
        description="Write this machine's spend summary (every present harness, merged) "
        "as a portable JSON file -- totals + per-model breakdown, no transcripts -- for "
        "another box to `opentab pull`. Pairs with --demo for a shareable summary.",
    )
    _add_global_args(export)
    export.add_argument(
        "file",
        nargs="?",
        default="-",
        metavar="FILE",
        help="where to write it (default: stdout, so `ssh box opentab export > box.json` " "works)",
    )
    _focus_help(export, gdests, {"demo", "label"})
    forget = subs.add_parser(
        "forget",
        help="drop machines from the saved fleet",
        description="Remove machines from remotes.json and delete their cached summaries "
        "(under --remotes), then exit.",
    )
    _add_global_args(forget)
    forget.add_argument(
        "names",
        nargs="+",
        metavar="NAME",
        help="the machine name(s) to forget",
    )
    _focus_help(forget, gdests, {"remotes"})
    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    # Preserve bare invocation, positional paths, and legacy flags by inserting `tui`.
    # A file named like a subcommand must be opened as `opentab tui <name>`.
    if argv and (argv[0] in _SUBCOMMANDS or argv[0] in ("-h", "--help", "--version")):
        return argv
    return ["tui", *argv]


def _apply_subcommand(args: argparse.Namespace) -> None:
    # Map subcommands onto the legacy namespace so there is one dispatch path.
    command = getattr(args, "command", None)
    if command == "web":
        if args.html is not None:
            args.serve = args.web = False
        elif args.headless:
            args.serve, args.web = True, False
        else:
            args.serve, args.web = False, True
    elif command == "pull":
        args.pull = list(args.hosts)
    elif command == "remote":
        args.remote = True
    elif command == "cost":
        # Empty string means "cost the current directory"; None means no cost request.
        args.status = args.targets[0] if args.targets else ""
        args.status_targets = list(args.targets)
        # Preserve the bare single-target output consumed by status-bar hooks.
        args.status_batch = bool(args.batch) or len(args.targets) > 1
    elif command == "export":
        args.export = args.file
    elif command == "forget":
        args.forget = list(args.names)


def _validate_demo_cats(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # Because --demo has an optional value, argparse can swallow a following filename.
    # Reject unknown CLI categories here; saved-state parsing remains permissive.
    spec = getattr(args, "demo", None)
    if not isinstance(spec, str) or spec == "all":
        return
    unknown = [n.strip() for n in spec.split(",") if n.strip().lower() not in DEMO_CATEGORIES]
    if unknown or not spec.strip():
        parser.error(
            f"--demo: unknown categor{'y' if len(unknown) == 1 else 'ies'} "
            f"{', '.join(repr(u) for u in unknown) or repr(spec)} "
            f"(choose from {', '.join(DEMO_CATEGORIES)}, or pass bare --demo for all). "
            "Note --demo takes an optional value, so a path right after it is read as "
            "one: use `--demo -- PATH` or put the path first."
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(_normalize_argv(raw))
    _validate_demo_cats(parser, args)
    _apply_subcommand(args)
    _route_path_arg(parser, args)
    return args


MIN_PYTHON = (3, 9)


def enable_unicode_locale() -> None:
    # Curses needs a UTF-8 C locale before initscr() to render multibyte bars and rules.
    # WSL often starts in C; opentab performs no locale-aware formatting, so trying a
    # UTF-8 fallback does not alter its number or sort semantics.
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        if "utf" in locale.nl_langinfo(locale.CODESET).lower():
            return
    except (AttributeError, ValueError):
        return  # No portable codeset probe; leave the locale unchanged.
    for name in ("C.UTF-8", "C.utf8", "en_US.UTF-8"):
        try:
            locale.setlocale(locale.LC_ALL, name)
            return
        except locale.Error:
            continue


def refresh_models_command() -> int:
    print(f"Fetching model list prices from {MODELS_DEV_URL} …")
    try:
        count, path = refresh_model_prices()
    except (OSError, ValueError) as exc:
        raise SystemExit(f"price refresh failed: {exc}") from exc
    print(f"Cached {count} model prices to {path}.")
    print("These overlay the embedded table for the $ what-if estimate; rerun to update.")
    return 0


def timings_command(args: argparse.Namespace) -> int:
    # Profile the TUI load path without restored state. It stays curses-free for native
    # Windows, where file-heavy backends are often slowest.
    def timed(fn):
        t0 = time.perf_counter()
        result = fn()
        return result, (time.perf_counter() - t0) * 1000.0

    # main() applies fleet selection after this early command, so mirror it here.
    if getattr(args, "remote", False) or getattr(args, "pull", None) is not None:
        args.source = "remote"
        args.remotes = getattr(args, "remotes", None) or default_remotes_dir()
        if getattr(args, "pull", None) is not None:
            pull_command(args)

    t_start = time.perf_counter()
    present, detect_ms = timed(lambda: sources.available_sources(args))
    source_key = resolve_source(args, {})
    (store, _loading), build_ms = timed(lambda: sources.make_store(args, source_key))

    # Retain parsed rows for the fleet breakdown rather than scanning again.
    # [label, files, ms, cached, sub, workflows, model_rows, incremental]
    backends: list[list] = []
    for sub in getattr(store, "stores", None) or [store]:
        label = getattr(sub, "source_name", type(sub).__name__)
        files = None
        files_fn = getattr(sub, "_files", None)
        if callable(files_fn):
            try:
                files = len(files_fn())
            except OSError:
                files = None
        wf, wf_ms = timed(sub.workflows)
        mb, mb_ms = timed(sub.model_breakdown)
        cached = getattr(sub, "served_from_cache", None)
        # Incremental splices are distinct from cache hits and full parses.
        inc = bool(getattr(sub, "served_incrementally", False))
        backends.append([label, files, wf_ms + mb_ms, cached, sub, wf, mb, inc])
    total_ms = (time.perf_counter() - t_start) * 1000.0
    backends.sort(key=lambda b: b[2], reverse=True)

    flags = [b[3] for b in backends if b[3] is not None]
    warm = [b[3] or b[7] for b in backends if b[3] is not None]
    if flags and all(flags):
        warmth = "warm start · all cached"
    elif warm and all(warm):
        warmth = "warm start · incremental"
    elif flags and any(flags):
        warmth = "partial cache"
    else:
        warmth = "cold start"
    py = f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"

    lbl = max([len(b[0]) for b in backends] + [len("backend"), len("total")])
    peak = max([b[2] for b in backends], default=0.0)

    def fmt_ms(ms: float) -> str:
        return f"{ms:7.1f} ms"

    print(f"opentab --timings · {total_ms:.0f} ms total · {warmth}")
    print(f"source={source_key} · python {py} · {sys.platform}")
    print()
    print(f"  detect sources  {fmt_ms(detect_ms)}   {', '.join(present) or '(none)'}")
    print(f"  build store     {fmt_ms(build_ms)}")
    print()
    print(f"  {'backend'.ljust(lbl)}  {'files':>5}  {'time':>10}")
    for label, files, ms, cached, _sub, _wf, _mb, inc in backends:
        fcell = str(files) if files is not None else "—"
        status = {True: "cached", False: "incremental" if inc else "parsed"}.get(cached, "")
        bar = cost_bar(ms, peak, 12)
        print(f"  {label.ljust(lbl)}  {fcell:>5}  {fmt_ms(ms)}  {bar} {status}".rstrip())
    print()
    print(f"  {'total'.ljust(lbl)}  {'':>5}  {fmt_ms(total_ms)}")

    # Re-aggregate loaded rows; a single-machine, single-harness run needs no breakdown.
    for line in _fleet_timing_tables(store, backends):
        print(line)
    return 0


def _fmt_ms(ms: float) -> str:
    return f"{ms:.1f} ms"


# Match the TUI's UTF-8 gate for timing-table box drawing.
_BOX_GLYPHS = {
    True: dict(
        tl="┌", tm="┬", tr="┐", ml="├", mm="┼", mr="┤", bl="└", bm="┴", br="┘", h="─", v="│"
    ),
    False: dict(
        tl="+", tm="+", tr="+", ml="+", mm="+", mr="+", bl="+", bm="+", br="+", h="-", v="|"
    ),
}


def _box_table(
    title: str,
    headers: list[str],
    rows: list[list[str]],
    aligns: str,
    indent: str = "  ",
    rule_before_last: bool = False,
    uni: bool = True,
) -> list[str]:
    g = _BOX_GLYPHS[uni]
    cols = len(headers)
    widths = [len(headers[i]) for i in range(cols)]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(r[i]))

    def rule(left: str, mid: str, right: str) -> str:
        return indent + left + mid.join(g["h"] * (w + 2) for w in widths) + right

    def line(cells: list[str]) -> str:
        parts = [
            f" {cells[i].rjust(widths[i]) if aligns[i] == 'r' else cells[i].ljust(widths[i])} "
            for i in range(cols)
        ]
        return indent + g["v"] + g["v"].join(parts) + g["v"]

    top = rule(g["tl"], g["tm"], g["tr"])
    if title:
        cap = f"{g['h']} {title} "
        pos = len(indent) + 1
        if pos + len(cap) < len(top) - 1:
            top = top[:pos] + cap + top[pos + len(cap) :]
    lines = [top, line(headers), rule(g["ml"], g["mm"], g["mr"])]
    for idx, r in enumerate(rows):
        if rule_before_last and idx == len(rows) - 1:
            lines.append(rule(g["ml"], g["mm"], g["mr"]))
        lines.append(line(r))
    lines.append(rule(g["bl"], g["bm"], g["br"]))
    return lines


def _fleet_estimated_costs(backends: list) -> dict[str, float]:
    # Mirror App's `$` estimate from already-parsed per-model rows. The tuples follow
    # api_equivalent_cost's argument order and end with the 1h-write subset; older exports
    # omit it and therefore retain their previous pricing.
    unpriced = ("unpriced_input", "unpriced_output", "unpriced_reasoning",
                "unpriced_cache_read", "unpriced_cache_write",
                "unpriced_cache_write_1h")  # fmt: skip
    whole = ("input", "output", "reasoning", "cache_read", "cache_write", "cache_write_1h")
    delta: dict[str, float] = {}
    for row in backends:
        for m in row[6] if len(row) > 6 and row[6] else ():
            m = dict(m)
            rid = m.get("root_id")
            if not rid:
                continue
            real = m.get("cost", 0) or 0
            # Old pure-$0 rows lack the unpriced split, so their whole token row is unpriced.
            keys = whole if (real == 0 and "unpriced_input" not in m) else unpriced
            delta[rid] = delta.get(rid, 0.0) + api_equivalent_cost(
                m["model_name"], *(m.get(k, 0) for k in keys)
            )
    return delta


def _fleet_aggregate(workflows: list, est_by_id: dict | None = None) -> tuple[dict, dict, dict]:
    # Return per-machine, per-harness, and cross-tabbed [sessions, tokens, cost, estimate].
    est_by_id = est_by_id or {}
    by_machine: dict[str, list] = {}
    by_harness: dict[str, list] = {}
    cell: dict[str, dict[str, list]] = {}
    for w in workflows:
        m = w.machine or "(this machine)"
        h = w.source or "?"
        est = w.total_cost + est_by_id.get(w.id, 0.0)
        for table, key in ((by_machine, m), (by_harness, h)):
            e = table.setdefault(key, [0, 0, 0.0, 0.0])
            e[0] += 1
            e[1] += w.total_tokens
            e[2] += w.total_cost
            e[3] += est
        c = cell.setdefault(m, {}).setdefault(h, [0, 0, 0.0, 0.0])
        c[0] += 1
        c[1] += w.total_tokens
        c[2] += w.total_cost
        c[3] += est
    return by_machine, by_harness, cell


def _fleet_timing_tables(store, backends: list, uni: bool | None = None) -> list[str]:
    # Build the fleet tables, or nothing for a single-machine, single-harness run.
    # `uni` is injectable for tests; production uses the same locale gate as the TUI.
    if uni is None:
        uni = unicode_screen()
    live_mark, pull_mark = ("● ", "○ ") if uni else ("* ", "- ")
    dot = "·" if uni else "."
    sig = "Σ" if uni else "sum"
    times = "×" if uni else "x"
    dash = "—" if uni else "-"

    all_wf = [w for row in backends for w in (row[5] or [])]
    est_by_id = _fleet_estimated_costs(backends)
    by_machine, by_harness, cell = _fleet_aggregate(all_wf, est_by_id)

    meta = getattr(store, "machine_meta", {}) or {}
    live_label = next((n for n, m in meta.items() if m.get("live")), None)

    # Apportion RemoteStore load by summary bytes; other load belongs to this machine.
    byte_by_machine: dict[str, int] = {}
    remote_ms = 0.0
    live_load_ms = 0.0
    harness_time: dict[str, float] = {}
    for row in backends:
        ms, sub, wf = row[2], row[4], row[5]
        stats_fn = getattr(sub, "machine_stats", None)
        if callable(stats_fn):
            remote_ms += ms
            for s in stats_fn():
                byte_by_machine[s["label"]] = s["bytes"]
        else:
            live_load_ms += ms
            if wf:
                harness_time[wf[0].source] = harness_time.get(wf[0].source, 0.0) + ms
    total_bytes = sum(byte_by_machine.values())

    # Seed valid empty/deduplicated exports so idle machines remain fleet members.
    for name in list(meta) + list(byte_by_machine):
        by_machine.setdefault(name, [0, 0, 0.0, 0.0])

    multi_machine = len(by_machine) >= 2
    multi_harness = len(by_harness) >= 2
    if not (multi_machine or multi_harness):
        return []

    # Fully metered fleets do not need a duplicate estimate column.
    total_cost = sum(v[2] for v in by_machine.values())
    total_est = sum(v[3] for v in by_machine.values())
    show_est = abs(total_est - total_cost) > 0.005

    def machine_load(name: str) -> float:
        if name == live_label:
            return live_load_ms
        if total_bytes:
            return remote_ms * byte_by_machine.get(name, 0) / total_bytes
        return 0.0

    out: list[str] = []

    def _totals(table: dict) -> list:
        return [
            sum(v[0] for v in table.values()),
            sum(v[1] for v in table.values()),
            sum(v[2] for v in table.values()),
            sum(v[3] for v in table.values()),
        ]

    if multi_machine:
        order = sorted(
            by_machine,
            key=lambda n: (n != live_label, -by_machine[n][2], -by_machine[n][0]),
        )
        rows = []
        for name in order:
            sess, toks, cost, est = by_machine[name]
            live = name == live_label
            mark = live_mark if live else pull_mark
            size = dash if live else human_bytes(byte_by_machine.get(name, 0))
            age = "live" if live else relative_age(meta.get(name, {}).get("exported_at", ""))
            row = [mark + name, f"{sess:,}", human_tokens(toks), money(cost)]
            if show_est:
                row.append(money(est))
            row += [size, _fmt_ms(machine_load(name)), age]
            rows.append(row)
        tsess, ttoks, tcost, test = _totals(by_machine)
        total_row = ["fleet", f"{tsess:,}", human_tokens(ttoks), money(tcost)]
        if show_est:
            total_row.append(money(test))
        total_row += [human_bytes(total_bytes), _fmt_ms(live_load_ms + remote_ms), ""]
        rows.append(total_row)
        headers = (
            ["machine", "sess", "tokens", "cost"]
            + (["est $"] if show_est else [])
            + ["size", "load", "age"]
        )
        out.append("")
        out.extend(
            _box_table(
                "By machine",
                headers,
                rows,
                aligns="lrrr" + ("r" if show_est else "") + "rrl",
                rule_before_last=True,
                uni=uni,
            )
        )

    if multi_harness:
        order = sorted(by_harness, key=lambda h: (-by_harness[h][0], h))
        rows = []
        for key in order:
            sess, toks, cost, est = by_harness[key]
            t = harness_time.get(key)
            row = [SOURCE_LABELS.get(key, key), f"{sess:,}", human_tokens(toks), money(cost)]
            if show_est:
                row.append(money(est))
            if multi_machine:
                row.append(str(sum(1 for m in cell if key in cell[m])))
            row.append(_fmt_ms(t) if t is not None else dash)
            rows.append(row)
        tsess, ttoks, tcost, test = _totals(by_harness)
        total_row = ["all", f"{tsess:,}", human_tokens(ttoks), money(tcost)]
        if show_est:
            total_row.append(money(test))
        if multi_machine:
            total_row.append(str(len(cell)))
        total_row.append(_fmt_ms(live_load_ms))
        headers = (
            ["harness", "sess", "tokens", "cost"]
            + (["est $"] if show_est else [])
            + (["boxes"] if multi_machine else [])
            + ["load"]
        )
        out.append("")
        out.extend(
            _box_table(
                "By harness",
                headers,
                [*rows, total_row],
                aligns="lrrr" + ("r" if show_est else "") + ("r" if multi_machine else "") + "r",
                rule_before_last=True,
                uni=uni,
            )
        )
        if multi_machine:
            out.append("  load = this machine's parse; pulled boxes arrive pre-rolled")

    if show_est:
        out.append("  est $ = list-price estimate for $0 / subscription tokens (the $ view)")

    if multi_machine and multi_harness:
        m_order = sorted(by_machine, key=lambda n: (n != live_label, -by_machine[n][0]))
        h_order = sorted(by_harness, key=lambda h: (-by_harness[h][0], h))
        headers = ["machine"] + [SOURCE_LABELS.get(h, h) for h in h_order] + [sig]
        grid = []
        for name in m_order:
            cells = [(live_mark if name == live_label else pull_mark) + name]
            for h in h_order:
                n = cell.get(name, {}).get(h, [0])[0]
                cells.append(f"{n:,}" if n else dot)
            cells.append(f"{by_machine[name][0]:,}")
            grid.append(cells)
        foot = [sig]
        for h in h_order:
            foot.append(f"{by_harness[h][0]:,}")
        foot.append(f"{sum(v[0] for v in by_machine.values()):,}")
        grid.append(foot)
        aligns = "l" + "r" * (len(h_order) + 1)
        table = _box_table(
            f"Sessions by machine {times} harness",
            headers,
            grid,
            aligns=aligns,
            rule_before_last=True,
            uni=uni,
        )
        width = max((len(line) for line in table), default=0)
        out.append("")
        if width <= 118:  # The flat tables remain useful when the grid is too wide.
            out.extend(table)
        else:
            out.append(
                f"  (machine {times} harness grid omitted -- {len(h_order)} harnesses too wide)"
            )

    return out


def _project_key(directory: str) -> str:
    # Match the TUI's git-root and worktree project grouping.
    return os.path.normpath(resolve_project_root(git_root(os.path.expanduser(directory))))


# Only interactive harnesses expose live session identity for cost/goto targets.
_STATUS_SOURCES = (
    "opencode",
    "claude",
    "codex",
    "hermes",
    "pi",
    "omp",
    "openclaw",
    "zaly",
    "gemini",
    "antigravity",
    "bahulam",
)


def _is_session_target(target: str) -> bool:
    # Backend cannot be inferred from ID shape because several use UUIDs. Unclaimed IDs
    # must stay IDs, never fall through to pricing the current project.
    if os.sep in target or (os.altsep and os.altsep in target):
        return False
    return not os.path.exists(os.path.expanduser(target))


def _status_candidate(store, project: str | None) -> tuple[str, int] | None:
    for row in store.recent_roots():
        if project is None or _project_key(row["directory"]) == project:
            return row["id"], row["last_active"]
    return None


def _price_root(store, workflow_id: str) -> str:
    # Prefix list-price estimates for $0 nodes with `~`; never present them as spend.
    # status_nodes is the cheap single-session path where a backend provides one.
    total = estimated = 0.0
    nodes_of = getattr(store, "status_nodes", store.workflow_nodes)
    for node in nodes_of(workflow_id):
        total += node["cost"]
        if not node["cost"] and node["tokens_total"]:
            estimated += api_equivalent_cost(
                node["model_name"],
                node["tokens_input"],
                node["tokens_output"],
                node["tokens_reasoning"],
                node["tokens_cache_read"],
                node["tokens_cache_write"],
                node_1h_write(node),
            )
    text = money(total + estimated)
    return "~" + text if estimated > 0 else text


# Compatibility alias; doctor and the renderer share util's single decision rule.
_resolve_init_color = init_color_allowed


def status_line(store, target: str | None = None) -> str:
    # Empty output deliberately makes an unmatched tmux segment disappear. A directory
    # selects its newest session; a session or subagent ID selects its exact root.
    workflow_id = None
    if target and _is_session_target(target):
        workflow_id = store.root_of(target)
    else:
        project = _project_key(target) if target else None
        candidate = _status_candidate(store, project)
        workflow_id = candidate[0] if candidate else None
    if workflow_id is None:
        return ""
    return _price_root(store, workflow_id)


def _status_stores(args: argparse.Namespace) -> list:
    # Build raw stores: status lookup uses filenames/heads or SQL, never corpus caches.
    # auto/all intentionally ignore the TUI's saved single-source preference.
    keys = [k for k in sources.available_sources(args) if k in _STATUS_SOURCES]
    source = getattr(args, "source", "auto")
    if source not in ("auto", "all"):
        keys = [k for k in keys if k == source]
    return [sources._build_store(args, k)[0] for k in keys]


class _StatusPricer:
    """Share root scans and node pricing across a batch.

    Interpreter startup dominates a single target; within one process, memoizing roots
    and prices also prevents split panes or subagent IDs from parsing one root twice.
    """

    def __init__(self, stores: list):
        self._stores = stores
        self._recent: dict[int, list] = {}
        self._prices: dict[tuple[int, str], str] = {}

    def _roots(self, index: int) -> list:
        # Lazy rows preserve early-stop scans while sharing fields already resolved by
        # previous targets.
        if index not in self._recent:
            self._recent[index] = list(self._stores[index].recent_roots())
        return self._recent[index]

    def _candidate(self, index: int, project: str | None) -> tuple[str, int] | None:
        for row in self._roots(index):
            if project is None or _project_key(row["directory"]) == project:
                return row["id"], row["last_active"]
        return None

    def _price(self, index: int, root: str) -> str:
        key = (index, root)
        if key not in self._prices:
            self._prices[key] = _price_root(self._stores[index], root)
        return self._prices[key]

    def line(self, target: str | None) -> str:
        if target and _is_session_target(target):
            # root_of is a cheap filename/directory/SQL probe, not a parse.
            for index, store in enumerate(self._stores):
                root = store.root_of(target)
                if root:
                    return self._price(index, root)
            return ""
        project = _project_key(target) if target else None
        best: tuple[int, str, int] | None = None
        for index in range(len(self._stores)):
            candidate = self._candidate(index, project)
            if candidate and (best is None or candidate[1] > best[2]):
                best = (index, candidate[0], candidate[1])
        if best is None:
            return ""
        return self._price(best[0], best[1])


def _status_line_all(args: argparse.Namespace, target: str | None) -> str:
    return _StatusPricer(_status_stores(args)).line(target)


def _goto_target(args: argparse.Namespace) -> tuple[str, str] | None:
    # Reuse cost-target semantics so IDs, subagents, and project recency cannot drift.
    target = args.goto or os.getcwd()
    keys = [k for k in sources.available_sources(args) if k in _STATUS_SOURCES]
    source = getattr(args, "source", "auto")
    # The remote fleet includes local backends, although available_sources never yields it.
    if source not in ("auto", "all", "remote"):
        keys = [k for k in keys if k == source]
    stores = [(k, sources._build_store(args, k)[0]) for k in keys]
    if _is_session_target(target):
        for key, store in stores:
            root = store.root_of(target)
            if root:
                return key, root
        return None
    project = _project_key(target)
    best: tuple[str, str, int] | None = None
    for key, store in stores:
        candidate = _status_candidate(store, project)
        if candidate and (best is None or candidate[1] > best[2]):
            best = (key, candidate[0], candidate[1])
    return (best[0], best[1]) if best else None


def _goto_hint(target: str) -> str:
    # Keep a tmux popup open when its new agent has not recorded a turn yet.
    if _is_session_target(target):
        return f"--goto: session {target} not found in any source"
    return f"--goto: no session yet in {short_path(target, 40)}"


def _read_batch_stdin() -> list[str]:
    # Refuse a tty: waiting for EOF would look like a frozen status-bar hook.
    if sys.stdin is None or sys.stdin.isatty():
        raise ValueError("status --batch -: stdin is a terminal (pipe the targets in)")
    return sys.stdin.read().splitlines()


def _batch_targets(targets: list[str]) -> list[str]:
    # Preserve exact target order, duplicates, and legal path spaces. Drop blank lines,
    # one CR from Windows input, and tab/NUL values the TSV protocol cannot represent.
    if targets.count("-") and targets != ["-"]:
        raise ValueError(
            "status --batch: `-` reads every target from stdin; don't mix it with others"
        )
    out: list[str] = []
    for raw in targets:
        for token in _read_batch_stdin() if raw == "-" else [raw]:
            target = token[:-1] if token.endswith("\r") else token
            if not target.strip() or "\t" in target or "\0" in target:
                continue
            out.append(target)
    return out


def _status_batch(args: argparse.Namespace, targets: list[str]) -> int:
    # Keyed, ordered TSV lets callers build a map; unmatched targets remain omitted.
    pricer = _StatusPricer(_status_stores(args))
    failed = False
    try:
        for target in targets:
            try:
                line = pricer.line(target)
            except (sqlite3.Error, OSError, ValueError):
                failed = True
                continue
            if line:
                print(f"{target}\t{line}")
    except BrokenPipeError:
        # Avoid an exit-time flush traceback after a downstream reader closes early.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    # Batch consumers publish or cache the table, so incomplete output must fail. The
    # legacy single status segment still swallows read errors to protect the status bar.
    return 1 if failed else 0


def status_command(args: argparse.Namespace) -> int:
    # Single-target failures become an empty segment rather than breaking a status bar.
    if getattr(args, "status_batch", False):
        try:
            targets = _batch_targets(getattr(args, "status_targets", []) or [])
        except ValueError as exc:
            print(f"opentab: {exc}", file=sys.stderr)
            return 2
        return _status_batch(args, targets)
    try:
        line = _status_line_all(args, args.status or None)
    except (sqlite3.Error, OSError, ValueError):
        return 0
    if line:
        print(line)
    return 0


def export_command(args: argparse.Namespace) -> int:
    import socket

    from opentab.stores.remote import EXPORT_VERSION, build_export

    key = args.source if args.source not in ("auto", "remote") else "all"
    label = args.label or socket.gethostname() or "machine"
    # A demo export must also anonymize its real hostname. Deterministic scrambling keeps
    # the exported label joinable to demo workflow.machine values.
    demo_on, _scale, demo_cats = demo_config(args)
    if demo_on and "titles" in demo_cats:
        label = demo_machine(label)
    exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if key == "all" and not sources.available_sources(args):
        # Empty machines remain valid fleet members.
        payload = {
            "opentab_export": EXPORT_VERSION,
            "label": label,
            "exported_at": exported_at,
            "opentab_version": __version__,
            "records_cost": True,
            "workflows": [],
            "model_breakdown": [],
        }
    else:
        store, _loading = sources.make_store(args, key)
        payload = build_export(store, label, exported_at, __version__)
    text = json.dumps(payload)
    dest = args.export
    if dest in (None, "-", ""):
        sys.stdout.write(text + "\n")
    else:
        try:
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            raise SystemExit(f"could not write {dest}: {exc}") from exc
        sys.stderr.write(
            f"Wrote {label} summary — {len(payload['workflows'])} sessions — to {dest}\n"
        )
    return 0


REMOTES_VERSION = 1


def remotes_config_path() -> str:
    # Connection config is durable; fetched summaries live in the cache directory.
    return os.path.join(paths.config_dir(), "remotes.json")


def _load_remotes() -> dict:
    try:
        with open(remotes_config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    machines = data.get("machines") if isinstance(data, dict) else None
    if not isinstance(machines, dict):
        return {}
    # remotes.json is user-edited input; malformed entries must not reach workers.
    return {k: v for k, v in machines.items() if isinstance(k, str) and isinstance(v, dict)}


def _save_remotes(machines: dict) -> None:
    # Best-effort and atomic: an unwritable config must not break a launch.
    path = remotes_config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"version": REMOTES_VERSION, "machines": machines}, fh, indent=2, sort_keys=True
            )
        os.replace(tmp, path)
    except OSError:
        pass


def _remote_entry(spec: str) -> tuple[str, dict]:
    name, sep, rest = spec.partition("=")
    if not sep:
        rest, name = spec, ""
    if rest.startswith(("http://", "https://")):
        entry = {"url": rest}
        host = rest.split("://", 1)[1]
    else:
        entry = {"ssh": rest}
        host = rest
    if not name:
        name = host.split("@")[-1].split("/")[0].split(":")[0] or rest
    return name, entry


def _summary_filename(name: str) -> str:
    # Percent-encode untrusted labels without collisions or directory traversal.
    from urllib.parse import quote

    safe = quote(name, safe="")
    if safe.startswith("."):
        # glob("*.json") skips dotfiles, so encode a leading dot too.
        safe = "%2E" + safe[1:]
    return safe + ".json"


def _fetch_summary(name: str, entry: dict, timeout: float = 60.0) -> str:
    if not isinstance(entry, dict):
        raise RuntimeError("invalid machine entry (expected an object with 'ssh' or 'url')")
    if entry.get("url"):
        import urllib.request

        with urllib.request.urlopen(entry["url"], timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    target = entry.get("ssh")
    if not target:
        raise RuntimeError("machine has neither an 'ssh' target nor a 'url'")
    cmd = entry.get("cmd") or "opentab --export -"
    proc = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "ConnectionAttempts=1",
            target,
            cmd,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        detail = [ln for ln in (proc.stderr or "").strip().splitlines() if ln]
        raise RuntimeError(detail[-1] if detail else f"ssh exited {proc.returncode}")
    return proc.stdout


def _pull_one(name: str, entry: dict, remotes_dir: str) -> tuple[int, str]:
    # Worker failures are returned per machine; none may abort the parallel pull.
    try:
        text = _fetch_summary(name, entry)
        payload = json.loads(text)
        if not isinstance(payload, dict) or not isinstance(payload.get("workflows"), list):
            return 0, "not an opentab summary (is opentab installed on that machine?)"
    except subprocess.TimeoutExpired:
        return 0, "timed out"
    except FileNotFoundError:
        return 0, "ssh not found on this machine"
    except (OSError, ValueError, RuntimeError, AttributeError, TypeError) as exc:
        # Any worker escape would surface at fut.result() and abort every machine.
        return 0, str(exc) or exc.__class__.__name__
    try:
        with open(os.path.join(remotes_dir, _summary_filename(name)), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        return 0, f"could not save: {exc}"
    return len(payload["workflows"]), ""


def pull_command(args: argparse.Namespace) -> None:
    machines = _load_remotes()
    tokens = args.pull or []
    if tokens:
        targets: dict = {}
        for spec in tokens:
            name, entry = _remote_entry(spec)
            saved = machines.get(name)
            if spec == name and isinstance(saved, dict) and (saved.get("ssh") or saved.get("url")):
                targets[name] = saved
            else:
                # Preserve orthogonal fields while repairing or replacing the target.
                merged = {**(saved or {}), **entry}
                # SSH and URL are mutually exclusive; a stale URL otherwise wins.
                if "ssh" in entry:
                    merged.pop("url", None)
                elif "url" in entry:
                    merged.pop("ssh", None)
                machines[name] = merged
                targets[name] = merged
        _save_remotes(machines)
    else:
        targets = dict(machines)
    if not targets:
        sys.stderr.write(
            "opentab --pull: no machines configured. Add one, e.g. "
            "`opentab --pull user@host` (or name=user@host, or http://host:port).\n"
        )
        return
    remotes_dir = args.remotes or default_remotes_dir()
    try:
        os.makedirs(remotes_dir, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"opentab --pull: cannot write {remotes_dir}: {exc}\n")
        return
    sys.stderr.write(
        f"Pulling {len(targets)} machine(s) in parallel: {', '.join(sorted(targets))}\n"
    )
    # Keep concurrent.futures' measured ~10ms import cost off one-shot status calls.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ok = 0
    with ThreadPoolExecutor(
        max_workers=min(len(targets), 8), thread_name_prefix="opentab-pull"
    ) as ex:
        futures = {ex.submit(_pull_one, n, e, remotes_dir): n for n, e in targets.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            count, error = fut.result()
            if error:
                sys.stderr.write(f"  ✗ {name} — {error}\n")
            else:
                ok += 1
                sys.stderr.write(f"  ✓ {name} — {count} sessions\n")
    sys.stderr.write(f"Pulled {ok}/{len(targets)} machine(s) into {remotes_dir}\n")


def _make_refresh_fn(args: argparse.Namespace):
    # Bind in-app refreshes to this run's remotes directory and the same pull workers.
    remotes_dir = args.remotes or default_remotes_dir()

    def refresh(keys: list) -> list:
        machines = _load_remotes()
        targets = {k: machines[k] for k in keys if k in machines}
        if not targets:
            return []
        try:
            os.makedirs(remotes_dir, exist_ok=True)
        except OSError as exc:
            return [(k, 0, f"cannot write {remotes_dir}: {exc}") for k in targets]
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = []
        with ThreadPoolExecutor(
            max_workers=min(len(targets), 8), thread_name_prefix="opentab-refresh"
        ) as ex:
            futures = {ex.submit(_pull_one, n, e, remotes_dir): n for n, e in targets.items()}
            for fut in as_completed(futures):
                name = futures[fut]
                count, error = fut.result()
                results.append((name, count, error))
        return results

    return refresh


def _make_ssh_targets_fn():
    # Re-read tiny remotes.json on each launch so newly learned machines work immediately.
    # HTTP-only machines deliberately provide no invented shell target.
    def targets() -> dict:
        return {
            name: str(entry["ssh"])
            for name, entry in _load_remotes().items()
            if isinstance(entry, dict) and entry.get("ssh")
        }

    return targets


def forget_command(args: argparse.Namespace) -> int:
    machines = _load_remotes()
    remotes_dir = args.remotes or default_remotes_dir()
    for name in args.forget:
        existed = machines.pop(name, None) is not None
        try:
            os.remove(os.path.join(remotes_dir, _summary_filename(name)))
        except OSError:
            pass
        sys.stderr.write(f"{'forgot' if existed else 'no such machine:'} {name}\n")
    _save_remotes(machines)
    return 0


def _home_path(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def _offer_retention_warnings(
    app: App, args: argparse.Namespace, source_key: str, can_persist: bool
) -> None:
    """Queue one warning per present harness that expires its own history.

    Both frontends call this, so the TUI and the report page cannot end up warning
    about different harnesses. Each harness is gated on its data root existing, not on
    it having sessions today: the point is to warn while there is still history left.
    """
    if getattr(args, "demo", False):
        return
    if source_key == "claude" or os.path.isdir(args.claude_dir):
        _offer_claude_retention_warning(app, can_persist)
    if source_key == "gemini" or os.path.isdir(os.path.join(args.gemini_dir, "tmp")):
        _offer_gemini_retention_warning(app, can_persist)


def _offer_gemini_retention_warning(app: App, can_persist: bool) -> None:
    retention = gemini_retention()
    if not retention.needs_warning:
        return
    if retention.source in ("unverifiable", "unknown"):
        headline = "OpenTab cannot verify when Gemini CLI deletes chat recordings."
    elif retention.source == "workspace":
        headline = "A project's Gemini settings switch chat cleanup back on."
    elif retention.max_count is not None:
        kept = gemini_max_count_label(retention.max_count)
        headline = f"Gemini CLI keeps only the newest {kept} sessions per project."
    elif retention.source == "configured" and retention.max_age:
        headline = f"Gemini CLI deletes chat recordings after {retention.max_age}."
    else:
        headline = (
            f"Gemini CLI will delete chat recordings after {GEMINI_RETENTION_DEFAULT_DAYS} days."
        )
    app.offer_startup_warning(
        {
            "id": GEMINI_RETENTION_WARNING_ID,
            "title": "WARNING · Gemini CLI history expires",
            "headline": headline,
            "lines": [
                "Cleanup runs on every launch and takes the session's subagent",
                "transcripts with it. Its tokens and cost estimates then leave",
                "OpenTab for good. 'All time' only covers files on disk.",
                "",
                "Keep long-term history:",
                _home_path(retention.settings_path),
                '"general": {"sessionRetention": {"enabled": false}}',
            ],
        },
        can_persist=can_persist,
    )


def _offer_claude_retention_warning(app: App, can_persist: bool) -> None:
    retention = claude_retention()
    if not retention.needs_warning:
        return
    path = _home_path(retention.settings_path)
    if retention.source == "default":
        headline = "Claude Code will delete local transcripts after 30 days."
    elif retention.days is not None:
        headline = f"Claude Code deletes local transcripts after {retention.days} days."
    else:
        headline = "OpenTab cannot verify when Claude Code deletes local transcripts."
    app.offer_startup_warning(
        {
            "id": CLAUDE_RETENTION_WARNING_ID,
            "title": "WARNING · Claude Code history expires",
            "headline": headline,
            "lines": [
                "When a transcript is deleted, its session, tokens, and cost",
                "estimates disappear from OpenTab permanently.",
                "OpenTab cannot recover them. 'All time' only covers files on disk.",
                "",
                "Keep long-term history:",
                path,
                f'"cleanupPeriodDays": {CLAUDE_RETENTION_RECOMMENDED_DAYS}',
            ],
        },
        can_persist=can_persist,
    )


def web_command(args: argparse.Namespace) -> int:
    # Build the same headless App state as the TUI. Defer the web import from TUI startup.
    from opentab import web

    use_state = not args.demo and not args.no_state
    state = load_state() if use_state else {}
    source_key = resolve_source(args, state)
    store, loading = sources.make_store(args, source_key)
    sys.stderr.write(loading)
    sys.stderr.flush()
    app = App(store, args, source_key=source_key)
    app.allow_price_prompt = False
    if source_key == "remote":
        app._refresh_backend = _make_refresh_fn(args)
    if use_state:
        apply_state(app, args, state)
    # The browser can close this report's modal but cannot write OpenTab's state.
    _offer_retention_warnings(app, args, source_key, can_persist=False)
    app._ensure_models()
    sys.stderr.write(" " * 40 + "\r")
    sys.stderr.flush()
    if args.serve or args.web:
        return web.serve_command(app, args)
    return web.html_command(app, args)


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(
            f"OpenTab requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
            f"(found {sys.version_info[0]}.{sys.version_info[1]})."
        )
    enable_unicode_locale()
    args = parse_args()
    if getattr(args, "command", None) == "doctor":
        # Doctor must run before migration: it reports the state that caused the problem
        # and promises no side effects. Keep its import off the status command's hot path.
        from opentab import doctor as doctor_module

        return doctor_module.doctor_command(args)
    if not getattr(args, "demo", False):
        # Migrate here, not in path getters used by help/doctor; demo touches no files.
        paths.migrate_legacy_caches()
    if getattr(args, "refresh_models", False):
        return refresh_models_command()
    if getattr(args, "keymap", False):
        print(bindings.ensure_user_keymap())
        return 0
    if getattr(args, "status", None) is not None:
        return status_command(args)
    if getattr(args, "timings", False):
        return timings_command(args)
    if getattr(args, "export", None) is not None:
        return export_command(args)
    if getattr(args, "forget", None):
        return forget_command(args)
    if getattr(args, "pull", None) is not None:
        pull_command(args)
    if getattr(args, "pull", None) is not None or getattr(args, "remote", False):
        # Set fleet selection before either frontend reads args.source.
        args.source = "remote"
        args.remotes = args.remotes or default_remotes_dir()
    if (
        getattr(args, "html", None) is not None
        or getattr(args, "serve", False)
        or getattr(args, "web", False)
    ):
        return web_command(args)
    if curses is None:
        raise SystemExit(
            "OpenTab needs Python's curses module, which native Windows Python doesn't bundle.\n"
            "  - Native Windows: pip install windows-curses, then rerun opentab.\n"
            "  - Or run opentab under WSL (where OpenCode's database usually lives anyway)."
        )
    # Restore the source before building its store; keep the model scan deferred.
    use_state = not args.demo and not args.no_state
    state = load_state() if use_state else {}
    source_key = resolve_source(args, state)
    goto = None
    goto_hint = None
    if getattr(args, "tab", None) and getattr(args, "goto", None) is None:
        # --tab implies --goto of the current directory.
        args.goto = ""
    if getattr(args, "goto", None) is not None:
        # Resolve first so saved source state cannot hide the target's backend.
        goto = _goto_target(args)
        if goto is None:
            # Keep the tmux popup open as a plain TUI when nothing matches yet.
            goto_hint = _goto_hint(args.goto or os.getcwd())
        elif source_key not in ("all", "remote", goto[0]):
            # Merged and fleet views already contain the backend; only override a pinned one.
            source_key = goto[0]
    store, loading = sources.make_store(args, source_key)
    sys.stderr.write(loading)
    sys.stderr.flush()
    # Invalid custom bindings degrade to defaults and notices, never block startup.
    bindings.ensure_user_keymap()
    app = App(store, args, source_key=source_key, keymap=bindings.load_user_keymap())
    if source_key == "remote":
        app._refresh_backend = _make_refresh_fn(args)
        app._ssh_targets = _make_ssh_targets_fn()
    app.allow_price_prompt = use_state
    app.allow_init_color = _resolve_init_color()
    # Notes are authored state: --no-state disables them, while App rechecks live demo mode.
    app.notes_enabled = not args.no_state
    sys.stderr.write(" " * 40 + "\r")
    sys.stderr.flush()
    if use_state:
        apply_state(app, args, state)
    _offer_retention_warnings(app, args, source_key, can_persist=use_state)
    # Refresh after apply_state, which clears notices and would erase a notes warning.
    notes_ok = app.refresh_notes()
    app.announce_keymap_warnings()
    if goto is not None:
        # goto_session clears a restored range that hides the target; no screen is needed.
        app.goto_session(goto[1], tab=getattr(args, "tab", None))
    elif goto_hint and notes_ok:
        # Notes loss risk outranks a goto miss when only one notice can paint.
        app.notify(goto_hint, "error")
    curses.wrapper(app.run)
    if use_state and not app.store.demo:
        save_state(app)
    return 0
