"""Argument parsing and the main entry point."""
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
    _default_openclaw_dir,
    _default_pi_dir,
    _default_zaly_dir,
    _route_path_arg,
    default_remotes_dir,
    resolve_source,
)
from opentab.state import apply_state, load_state, save_state
from opentab.tui import bindings
from opentab.tui.app import App
from opentab.util import git_root, node_1h_write, resolve_project_root, unicode_screen

# --- argument groups ----------------------------------------------------------------
# The parser is split so subcommands can share it. GLOBAL modifiers (source
# selection, per-harness paths, range, demo, theme, the serve address) attach to
# every subcommand via parents=[...]. The old VERB flags (--status/--export/--web/...)
# live only on the implicit `tui` command, kept working as deprecated aliases the same
# way --source still works after becoming --harness. Each new verb (`web`, and more as
# they land) owns its own options. --version is the top-level parser's alone.


def _add_global_args(parser: argparse.ArgumentParser) -> None:
    # --version rides on the shared parent, not just the top-level parser, so it stays
    # order-independent through the implicit `tui` prepend: `opentab --source x --version`
    # and `opentab PATH --version` must still print and exit, as the flat parser did. A
    # fixed string (not %(prog)s) keeps it "opentab X.Y.Z" and never "opentab tui X.Y.Z".
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
            "openclaw",
            "zaly",
            "all",
            "remote",
        ),
        default="auto",
        help="which harness's spend to browse: opencode · claude · codex · hermes · csv · "
        "jsonl · copilot · vscode · pi · openclaw · zaly · all (merged) · remote (other "
        "machines, via pull/export). Default auto merges every present local harness. Or "
        "just pass a file path -- e.g. `opentab requests.csv`. (--source is a deprecated "
        "alias for --harness)",
    )
    parser.add_argument("--db", default=os.path.expanduser("~/.local/share/opencode/opencode.db"))
    parser.add_argument(
        "--claude-dir",
        default=os.path.expanduser("~/.claude/projects"),
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
        "(session/prompt/project/model/machine names), turns (the expandable full "
        "prompt text), spend (dollars + token magnitudes) -- e.g. --demo titles,spend "
        "shows real prompt bodies but fake names and hidden costs. Toggle live in the "
        "TUI with D",
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
        metavar="THEME",  # collapse the 30-name choices wall in --help; still validated
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
    # The pre-subcommand verb flags. They stay on the implicit `tui` command as
    # deprecated-but-working aliases, so old invocations and tmux bindings never break --
    # the same courtesy --source got when it became --harness. New spellings are
    # subcommands: `opentab web` today (== --web/--serve/--html), and
    # status/goto/export/pull/... as they are ported.
    parser.add_argument(
        "--status",
        nargs="?",
        const="",
        default=None,
        metavar="DIR|SESSION",
        help="print the cost of the most recently active agent session (subagent "
        "subtree included) and exit, consulting every present harness backend "
        "(OpenCode, Claude Code, Codex, Hermes, pi, OpenClaw, Zaly); with DIR only "
        "sessions of that project count, with a session id (ses_... or a UUID -- the "
        "id is matched to its own backend) exactly that session is priced, and "
        "--harness pins one backend. Made for a tmux status line: set -g "
        "status-right '#(opentab --status \"#{pane_current_path}\")'. A leading ~ "
        "marks a list-price estimate for usage recorded at $0 (subscription models)",
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
        "resolved across every present harness backend like --status. Made for a "
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


# The verbs that carry their own subparser. Everything else -- a bare `opentab`, a
# `opentab requests.csv`, any legacy flag -- is the implicit `tui` command.
_SUBCOMMANDS = ("tui", "web", "status", "pull", "remote", "export", "forget")


def _focus_help(subparser: argparse.ArgumentParser, common_dests: set, keep: set) -> None:
    # A verb inherits every global (parents=[common]) so its namespace is complete and
    # `main()`/_route_path_arg never miss an attribute -- but `opentab pull -h` should not
    # then recite every backend path, the theme list and the serve address. Hide the
    # globals a verb doesn't care about from its --help (they still PARSE, just aren't
    # advertised -- `opentab tui -h` remains the full reference). Zen over completeness.
    for action in subparser._actions:
        if action.dest in common_dests and action.dest not in keep:
            action.help = argparse.SUPPRESS


def _build_parser() -> argparse.ArgumentParser:
    # `common` is a PROBE, not a shared parent: argparse's parents=[...] re-adds the SAME
    # action objects to every child, so mutating action.help in _focus_help would leak
    # across verbs. Instead each subparser gets its OWN globals via _add_global_args(sub),
    # and this throwaway just enumerates which dests count as "global" for _focus_help.
    probe = argparse.ArgumentParser(add_help=False)
    _add_global_args(probe)
    gdests = {a.dest for a in probe._actions if a.dest not in ("help", "version")}
    parser = argparse.ArgumentParser(
        prog="opentab", description="OpenTab — browse your AI-coding spend"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # Seed every legacy verb field on the namespace so a subcommand that has none of them
    # (web, pull, ...) still carries them for main()'s dispatch and App/apply_state to read
    # without an AttributeError -- the tui subparser and _apply_subcommand override what
    # they own. path is here too: _route_path_arg reads it (and applies the --csv/--jsonl
    # default paths) for every command, though only tui takes a positional.
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
    _add_global_args(tui)  # tui is the full reference: every global, not focused
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
    status = subs.add_parser(
        "status",
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
    # --- the fleet: getting other machines' spend (the --pull/--remote/--export/--forget
    # verbs as subcommands). pull/remote open the merged fleet view; export/forget are
    # one-shot. All map onto the legacy fields in _apply_subcommand, so main() dispatches
    # them through the exact same code path.
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
    # Insert the implicit `tui` command unless the first token already names a
    # subcommand or asks for top-level --help/--version -- so `opentab`,
    # `opentab requests.csv`, `opentab --demo`, and every legacy flag keep working
    # unchanged. (A file literally named like a subcommand must be opened as
    # `opentab tui web` -- the one cost of the bare-path shortcut.)
    if argv and (argv[0] in _SUBCOMMANDS or argv[0] in ("-h", "--help", "--version")):
        return argv
    return ["tui", *argv]


def _apply_subcommand(args: argparse.Namespace) -> None:
    # Map a new subcommand's own options onto the legacy args.* fields main() already
    # dispatches on, so there stays exactly ONE dispatch path. `tui` needs nothing --
    # its flags already ARE those fields.
    command = getattr(args, "command", None)
    if command == "web":
        # web: --html writes the static file, --headless serves without opening, the
        # bare command serves AND opens -- the three legacy flags, one friendly verb.
        if args.html is not None:
            args.serve = args.web = False
        elif args.headless:
            args.serve, args.web = True, False
        else:
            args.serve, args.web = False, True
    elif command == "pull":
        # `opentab pull [HOST...]` == `--pull [HOST...]`: fetch, then main() flows into the
        # merged fleet view (TUI). An empty list is bare --pull (refresh the saved machines).
        args.pull = list(args.hosts)
    elif command == "remote":
        args.remote = True  # open the already-pulled fleet, no fetch (== --remote)
    elif command == "status":
        # `opentab status [TARGET]` == `--status [TARGET]`, so main() dispatches both
        # through status_command. status is set to "" (not None) even with no target,
        # because None is what "the user didn't ask for status at all" means there.
        args.status = args.targets[0] if args.targets else ""
        args.status_targets = list(args.targets)
        # Arity decides the shape unless --batch forces the table: one target keeps the
        # bare line every existing status-bar hook already parses.
        args.status_batch = bool(args.batch) or len(args.targets) > 1
    elif command == "export":
        args.export = args.file  # "-" (stdout) by default (== --export)
    elif command == "forget":
        args.forget = list(args.names)  # >=1 by nargs="+" (== --forget)


def _validate_demo_cats(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # --demo takes an OPTIONAL value, so argparse happily eats the next positional:
    # `opentab --demo requests.csv` bound the path to --demo, and `opentab export --demo
    # out.json` sent the summary to stdout while out.json was never written. Nothing
    # complained, because parse_demo_cats deliberately drops names it doesn't know (an
    # empty result falls back to everything), which is right for a set arriving from
    # saved state but hides a typo -- or a swallowed filename -- on the command line.
    # Reject it here instead, where a value is unambiguously something the user typed.
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
    # Positional-path routing (tui only -- path is None for other subcommands) plus the
    # --csv/--jsonl default paths, which every command needs.
    _route_path_arg(parser, args)
    return args


MIN_PYTHON = (3, 9)


def enable_unicode_locale() -> None:
    # ncurses renders the Unicode bars/blocks (█ ▁▂▃, the ─ rules) only when the C
    # library is in a UTF-8 locale; otherwise it drops those multibyte bytes and only
    # the (locale-independent) ACS box frame survives. CPython applies the env locale
    # to LC_CTYPE at startup, so this already works wherever $LANG is a UTF-8 locale
    # (macOS, most servers). WSL typically ships with $LANG unset or "C" -- so apply
    # the env locale first, then, if that didn't land on UTF-8, force a UTF-8 locale
    # so the chart renders regardless of how the shell is configured. opentab does no
    # locale-aware formatting (explicit f-strings, code-point sorts), so forcing one
    # is side-effect-free. Must run before curses initscr().
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        if "utf" in locale.nl_langinfo(locale.CODESET).lower():
            return
    except (AttributeError, ValueError):
        return  # nl_langinfo/CODESET unavailable -- leave the locale as-is
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
    # Startup profiler: walk the same load path the TUI takes (detect sources, build
    # the store, roll up each backend, run the model scan) with a stopwatch on every
    # phase, print a table, and exit. Curses-free, so it also runs on native Windows
    # -- the platform where the file-heavy backends hurt most. State is skipped so the
    # numbers reflect a cold, reproducible run rather than a restored source.
    def timed(fn):
        t0 = time.perf_counter()
        result = fn()
        return result, (time.perf_counter() - t0) * 1000.0

    # --remote/--pull select the fleet, but their wiring in main() runs AFTER this command
    # returns -- so honour them here too, else `opentab --remote --timings` would profile
    # the local "all" merge and never touch the RemoteStore the user asked to measure.
    if getattr(args, "remote", False) or getattr(args, "pull", None) is not None:
        args.source = "remote"
        args.remotes = getattr(args, "remotes", None) or default_remotes_dir()
        # --pull means "refresh from the machines first". main()'s pull step runs AFTER
        # this command returns and so never fires under --timings -- without this the
        # summaries stay stale and the fleet ages read "Nh ago" despite the --pull. Fetch
        # here (progress on stderr) so the timings profile a freshly pulled fleet.
        if getattr(args, "pull", None) is not None:
            pull_command(args)

    t_start = time.perf_counter()
    present, detect_ms = timed(lambda: sources.available_sources(args))
    source_key = resolve_source(args, {})  # no saved state -> measure a clean start
    (store, _loading), build_ms = timed(lambda: sources.make_store(args, source_key))

    # One row per backend: its whole parse+scan cost and whether it came from the cache.
    # We keep the rolled-up workflows too (row[5]) -- they're already in memory, and the
    # fleet breakdown below re-aggregates them per machine and per harness rather than
    # re-parsing anything.
    backends: list[list] = []  # [label, files, ms, cached, sub, workflows, model_rows]
    for sub in getattr(store, "stores", None) or [store]:
        label = getattr(sub, "source_name", type(sub).__name__)
        files = None
        files_fn = getattr(sub, "_files", None)  # only the file-based backends have it
        if callable(files_fn):
            try:
                files = len(files_fn())
            except OSError:
                files = None
        wf, wf_ms = timed(sub.workflows)
        mb, mb_ms = timed(sub.model_breakdown)
        cached = getattr(sub, "served_from_cache", None)
        # Keep the per-model rows: the fleet's list-price ("$") estimate reprices each
        # row's unpriced tokens (below), and they're already parsed -- discarding them
        # would mean a second model_breakdown scan.
        backends.append([label, files, wf_ms + mb_ms, cached, sub, wf, mb])
    total_ms = (time.perf_counter() - t_start) * 1000.0
    backends.sort(key=lambda b: b[2], reverse=True)  # slowest backend first

    flags = [b[3] for b in backends if b[3] is not None]
    if flags and all(flags):
        warmth = "warm start · all cached"
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
    for label, files, ms, cached, _sub, _wf, _mb in backends:
        fcell = str(files) if files is not None else "—"
        status = {True: "cached", False: "parsed"}.get(cached, "")
        bar = cost_bar(ms, peak, 12)
        print(f"  {label.ljust(lbl)}  {fcell:>5}  {fmt_ms(ms)}  {bar} {status}".rstrip())
    print()
    print(f"  {'total'.ljust(lbl)}  {'':>5}  {fmt_ms(total_ms)}")

    # The fleet breakdown: the same rolled-up sessions re-aggregated per machine and per
    # harness (sessions / tokens / cost), the machine x harness grid, and where the load
    # time went. Only meaningful once there's more than one box or more than one tool in
    # the mix -- a single-source local run prints nothing extra.
    for line in _fleet_timing_tables(store, backends):
        print(line)
    return 0


def _fmt_ms(ms: float) -> str:
    return f"{ms:.1f} ms"


def _human_bytes(n: int) -> str:
    # Compact on-disk size for the --timings machine table -- the summary file is where
    # the v2 Turns/Tools/Context extras land, so its size is a real signal.
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:,.1f} MB"
    if n >= 1024:
        return f"{n / 1024:,.0f} KB"
    return f"{n:,} B"


# Light box-drawing for the --timings tables, with the ASCII set the frame falls back
# to where the locale can't encode the glyphs (util.unicode_screen -- the same gate the
# TUI's panels use). Multibyte, so these only ever go to a UTF-8 stdout.
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
    # A ruled box-drawing table for the --timings fleet breakdown, matching the TUI's
    # ruled panels: a bordered grid with the title set into the top rule, every column
    # padded to its widest value and l/r-aligned per `aligns`, and an inner rule above the
    # final (TOTAL) row. `uni` picks light box drawing vs the ASCII +-| fallback.
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
    if title:  # set the caption into the top rule, just past the corner
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
    # Per-root list-price ESTIMATE delta: what each session's $0/subscription tokens WOULD
    # cost at API rates -- the `$` view's number, mirroring App._compute_api_costs exactly
    # (same unpriced_* split, same api_equivalent_cost). Summed per root_id across every
    # backend's already-parsed model rows (row[6]); a backend without model rows (older test
    # fixtures) contributes nothing, so the estimate falls back to the real cost.
    # input, output, reasoning, cache_read, cache_write -- api_equivalent_cost's arg order.
    # Both tuples end with the 1h-TTL cache-write subset, which api_equivalent_cost takes
    # as its trailing argument -- a machine whose export predates the field contributes 0
    # and prices exactly as it did before.
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
            # A pure-$0 row without an unpriced_* split still prices from its aggregate
            # tokens (the App's all_unpriced case); otherwise only the $0 messages count.
            keys = whole if (real == 0 and "unpriced_input" not in m) else unpriced
            delta[rid] = delta.get(rid, 0.0) + api_equivalent_cost(
                m["model_name"], *(m.get(k, 0) for k in keys)
            )
    return delta


def _fleet_aggregate(workflows: list, est_by_id: dict | None = None) -> tuple[dict, dict, dict]:
    # Roll the fleet's sessions up two ways and cross-tabbed, from the already-parsed
    # Workflow rows (each carries .machine, .source, .total_cost, .total_tokens): per
    # machine, per harness, and machine -> harness -> [sessions, tokens, cost, est]. `est`
    # is the list-price figure (real spend + the session's unpriced tokens at list rates,
    # from _fleet_estimated_costs) -- equal to `cost` when there's nothing to estimate.
    # Pure over the rows so it's testable without a store.
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
    # Build the fleet breakdown printed after the per-backend load table: By machine, By
    # harness, and the machine x harness session grid, each as a ruled box-drawing table.
    # Returns the lines to print (empty when there's nothing worth breaking down -- one
    # box AND one tool). `uni` overrides the UTF-8 gate (for tests); production reads it
    # from the locale, so a non-UTF-8 terminal gets the ASCII glyph set throughout.
    if uni is None:
        uni = unicode_screen()
    live_mark, pull_mark = ("● ", "○ ") if uni else ("* ", "- ")
    dot = "·" if uni else "."  # an empty grid cell
    sig = "Σ" if uni else "sum"  # the totals row/column label
    times = "×" if uni else "x"
    dash = "—" if uni else "-"  # a not-applicable cell (live box has no summary file)

    all_wf = [w for row in backends for w in (row[5] or [])]
    est_by_id = _fleet_estimated_costs(backends)
    by_machine, by_harness, cell = _fleet_aggregate(all_wf, est_by_id)

    meta = getattr(store, "machine_meta", {}) or {}
    live_label = next((n for n, m in meta.items() if m.get("live")), None)

    # Where the load time went: the RemoteStore row is the pulled read (per box via its
    # byte share); every other backend row is a harness parsed on THIS machine.
    byte_by_machine: dict[str, int] = {}
    remote_ms = 0.0
    live_load_ms = 0.0
    harness_time: dict[str, float] = {}
    for row in backends:  # index, not unpack -- a row may or may not carry model rows (row[6])
        ms, sub, wf = row[2], row[4], row[5]
        stats_fn = getattr(sub, "machine_stats", None)
        if callable(stats_fn):  # the RemoteStore -- pulled summaries, one file per box
            remote_ms += ms
            for s in stats_fn():
                byte_by_machine[s["label"]] = s["bytes"]
        else:
            live_load_ms += ms
            if wf:
                harness_time[wf[0].source] = harness_time.get(wf[0].source, 0.0) + ms
    total_bytes = sum(byte_by_machine.values())

    # A box in the fleet with zero KEPT sessions (a valid empty export, or one whose every
    # session the live box already has) never appears in the workflow rollup -- seed it
    # from machine_meta / machine_stats so it still shows as an idle member (and doesn't
    # drop the machine count below the "it's a fleet" threshold). Labels here already agree
    # with w.machine: both machine_meta and machine_stats scramble under demo.
    for name in list(meta) + list(byte_by_machine):
        by_machine.setdefault(name, [0, 0, 0.0, 0.0])

    multi_machine = len(by_machine) >= 2
    multi_harness = len(by_harness) >= 2
    if not (multi_machine or multi_harness):
        return []

    # Show the list-price ("$") estimate column only when it says something the real cost
    # doesn't -- i.e. there are unpriced/subscription tokens. A fully metered fleet skips it.
    total_cost = sum(v[2] for v in by_machine.values())
    total_est = sum(v[3] for v in by_machine.values())
    show_est = abs(total_est - total_cost) > 0.005

    def machine_load(name: str) -> float:
        if name == live_label:
            return live_load_ms
        if total_bytes:  # pulled: byte-proportional share of the (tiny) summary read
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
        # live box first, then heaviest spend, then most sessions
        order = sorted(
            by_machine,
            key=lambda n: (n != live_label, -by_machine[n][2], -by_machine[n][0]),
        )
        rows = []
        for name in order:
            sess, toks, cost, est = by_machine[name]
            live = name == live_label
            mark = live_mark if live else pull_mark
            size = dash if live else _human_bytes(byte_by_machine.get(name, 0))
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
        total_row += [_human_bytes(total_bytes), _fmt_ms(live_load_ms + remote_ms), ""]
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
            if multi_machine:  # a "boxes" count is only informative once there's a fleet
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
        if multi_machine:  # a footnote, since load means something different per box
            out.append("  load = this machine's parse; pulled boxes arrive pre-rolled")

    if show_est:  # the est column can ride on either flat table -- footnote it once
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
        if width <= 118:  # a grid too wide to read is worse than none -- the flat tables have it
            out.extend(table)
        else:
            out.append(
                f"  (machine {times} harness grid omitted -- {len(h_order)} harnesses too wide)"
            )

    return out


def _project_key(directory: str) -> str:
    # Fold a session's recorded cwd (or the pane path tmux hands us) onto the
    # project it belongs to, the same way the TUI groups projects: up to the git
    # root, then worktrees onto their main repo.
    return os.path.normpath(resolve_project_root(git_root(os.path.expanduser(directory))))


# The backends a --status target can price: the interactive harnesses, each with
# a live session a tmux pane can point at. The request-log sources (csv/jsonl)
# have synthetic per-(date, project) sessions with no live identity, and the
# Copilot/VS Code stores record no terminal session to follow.
_STATUS_SOURCES = ("opencode", "claude", "codex", "hermes", "pi", "openclaw", "zaly")


def _is_session_target(target: str) -> bool:
    # A --status target is a directory or a session id. An id never contains a
    # path separator and doesn't exist on disk; anything else scopes by project.
    # Which backend an id belongs to is NOT decided here -- ids are probed via
    # each store's root_of (UUID shapes collide across Claude/Codex/pi/Zaly), and
    # an id nobody claims yields an empty segment rather than being reinterpreted
    # as a directory, so a stale id can never price the shell's own project.
    if os.sep in target or (os.altsep and os.altsep in target):
        return False
    return not os.path.exists(os.path.expanduser(target))


def _status_candidate(store, project: str | None) -> tuple[str, int] | None:
    # The newest root (id, last-active ms) -- scoped to `project` when given.
    for row in store.recent_roots():
        if project is None or _project_key(row["directory"]) == project:
            return row["id"], row["last_active"]
    return None


def _price_root(store, workflow_id: str) -> str:
    # Recorded cost of the workflow's whole subtree, plus a list-price estimate for
    # any $0 (subscription) node -- prefixed "~" so a real dollar amount is never
    # conflated with an estimate (the one-shot sibling of the TUI's $ view /
    # _priced_nodes). status_nodes is the backend's cheap single-session opt-in
    # (ClaudeStore parses just that transcript); workflow_nodes otherwise.
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


def status_line(store, target: str | None = None) -> str:
    # The figure for the tmux segment: recorded cost of the most recently active
    # session's whole subtree. Empty when nothing matches, so the segment simply
    # disappears.
    #
    # `target` is a directory (price that project's most recent session) or a
    # session id (price exactly that session -- the disambiguator when several
    # sessions run in one project, e.g. stamped per-pane by a tmux plugin); a
    # subagent id resolves to its root.
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
    # Every present status-capable backend, built raw (no cache wrap -- the
    # status trio answers from file names/heads or SQL, never a corpus parse).
    # An explicit --source narrows to that one backend; auto/all consult them
    # all, deliberately ignoring the TUI's saved source preference.
    keys = [k for k in sources.available_sources(args) if k in _STATUS_SOURCES]
    source = getattr(args, "source", "auto")
    if source not in ("auto", "all"):
        keys = [k for k in keys if k == source]
    return [sources._build_store(args, k)[0] for k in keys]


class _StatusPricer:
    """Prices --status targets against one shared set of backends.

    A per-target `opentab --status` fan-out repeats three costs N times: the
    interpreter+import start (~90ms, the shell's to avoid -- hence `status
    --batch`), each backend's recent_roots scan, and the node parse of every
    root it prices. This holds the latter two for the life of one process, so a
    batch of panes sharing a session -- a split, or a subagent id that walks up
    to the same root -- parses it once. Single-target callers get the same
    behaviour as before: nothing is precomputed, every memo fills on demand.
    """

    def __init__(self, stores: list):
        self._stores = stores
        self._recent: dict[int, list] = {}
        self._prices: dict[tuple[int, str], str] = {}

    def _roots(self, index: int) -> list:
        # recent_roots() is already a list in every backend, and its rows resolve
        # "directory" lazily on first read (util.LazyStatusRoot), so caching the
        # list keeps the "a project scan stops paying at the row that matches"
        # property while letting the next target reuse the head reads this one
        # paid for -- the rows memoize their own resolved fields.
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
        """The status figure for one target -- '' when nothing matches."""
        if target and _is_session_target(target):
            # The id itself names its backend: every store's root_of answers from
            # a cheap filename/dir/SQL lookup (never a parse), so probe each and
            # let the first claimant price it -- ids are UUIDs or ses_-prefixed,
            # so a cross-backend collision is not a realistic concern.
            for index, store in enumerate(self._stores):
                root = store.root_of(target)
                if root:
                    return self._price(index, root)
            return ""
        # Directory (or nothing): the most recently active root across the backends
        # wins, so whichever tool you drove last is the one priced.
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
    # Resolve --goto's target to (source key, root session id) with the --status
    # machinery: a session id is probed via each backend's root_of (a subagent id
    # walks up to its root), a directory takes the project's most recently active
    # root across the backends. Returns None when nothing matches.
    target = args.goto or os.getcwd()
    keys = [k for k in sources.available_sources(args) if k in _STATUS_SOURCES]
    source = getattr(args, "source", "auto")
    # "auto"/"all" span every backend; "remote" is the fleet view whose live box IS the
    # local backends (available_sources never yields "remote"), so it must probe them
    # too -- pinning to =="remote" would empty the key list and never find the session.
    if source not in ("auto", "all", "remote"):  # an explicit --source pins one backend
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
    # The toast shown when --goto resolves to nothing -- typically a project whose
    # agent was just launched and hasn't recorded a turn yet. The flag was made
    # for a tmux popup binding, where exiting would flash the error and close, so
    # main opens the ordinary TUI and this says why it didn't jump.
    if _is_session_target(target):
        return f"--goto: session {target} not found in any source"
    return f"--goto: no session yet in {short_path(target, 40)}"


def _read_batch_stdin() -> list[str]:
    # `-` among the targets means "the rest come from stdin, one per line" (the
    # `export -` convention). A tty would just hang waiting for EOF, which from a
    # status-bar hook looks like opentab freezing, so refuse it outright.
    if sys.stdin is None or sys.stdin.isatty():
        raise ValueError("status --batch -: stdin is a terminal (pipe the targets in)")
    return sys.stdin.read().splitlines()


def _batch_targets(targets: list[str]) -> list[str]:
    # The batch target list, cleaned but NOT canonicalized: order and cardinality
    # are the caller's, because the output is keyed by the exact string asked for
    # and a caller may legitimately ask twice (pricing it twice is free -- the
    # pricer memoizes the root). Only three things are dropped:
    #
    #  * blank lines, so a trailing newline in a piped list isn't a target;
    #  * one trailing \r, so a list produced by a Windows-side tool doesn't key
    #    rows nothing matches -- but NOT a general .strip(), because leading and
    #    trailing spaces are legal in a Unix path and stripping them would price
    #    the wrong directory;
    #  * a target containing a tab or NUL, which a TSV keyed by it cannot
    #    represent. (An id never contains one; a path could.) A target containing
    #    a newline is likewise unrepresentable, and the line protocol has already
    #    split it -- documented in the subcommand's help rather than detected.
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
    # Price many targets in ONE process: `<target>\t<price>` per line, in the
    # order asked, unpriceable targets omitted (--status's own contract -- an
    # empty segment rather than an error). Keyed output is what lets the caller
    # read the table straight into a map without tracking which reply was whose;
    # the tmux picker's sweep used numbered temp files for exactly that.
    pricer = _StatusPricer(_status_stores(args))
    failed = False
    try:
        for target in targets:
            try:
                line = pricer.line(target)
            except (sqlite3.Error, OSError, ValueError):
                failed = True  # one unreadable backend must not cost the other targets
                continue
            if line:
                print(f"{target}\t{line}")
    except BrokenPipeError:
        # The reader stopped (`| head -1`): its choice, not our failure. Point stdout
        # at the void so the interpreter's exit-time flush can't print a traceback
        # over whatever the caller is doing.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    # A read error means this table is missing rows it should have had. Legacy
    # --status deliberately swallows that (an empty status segment beats a broken
    # status bar), but a batch caller is building a table it will PUBLISH: the
    # tmux collector replaces its cached prices only on a zero exit, so saying
    # "incomplete" here is what keeps the previous complete table in place.
    return 1 if failed else 0


def status_command(args: argparse.Namespace) -> int:
    # One-shot, curses-free sibling of --refresh-models, polled from a tmux status
    # line -- so every failure mode prints nothing (an empty segment) instead of
    # erroring the whole status bar.
    if getattr(args, "status_batch", False):
        # A usage mistake here IS worth shouting about: batch is called by a
        # script the author is writing, not polled by a status bar mid-session.
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
    # --export: write this machine's spend summary (the warm-start rollup -- totals +
    # per-model breakdown, no transcripts) as portable JSON and exit. Curses-free like
    # --status/--html. Defaults to the whole machine (every present harness, merged);
    # --harness pins one. Made for `ssh box opentab --export - > box.json`, then browse
    # the gathered files with `opentab --source remote`.
    import socket

    from opentab.stores.remote import EXPORT_VERSION, build_export

    key = args.source if args.source not in ("auto", "remote") else "all"
    label = args.label or socket.gethostname() or "machine"
    # --demo pairs with --export for a shareable summary, and under it every title, path,
    # model and dollar figure in the payload is scrambled -- but the label is a real
    # hostname (a work box, a personal handle), so leaving it raw put the one piece of
    # genuine identity into the artefact you hand to someone. RemoteStore already
    # scrambles it at display time behind the same `titles` gate; do it here too, so the
    # file on disk matches what a demo shows. demo_machine is deterministic, so a
    # summary exported under demo still joins to its scrambled w.machine.
    demo_on, _scale, demo_cats = demo_config(args)
    if demo_on and "titles" in demo_cats:
        label = demo_machine(label)
    exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if key == "all" and not sources.available_sources(args):
        # A machine with no agent data yet still exports a valid, empty summary, so
        # `opentab --pull` reports "0 sessions" for it instead of failing outright.
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
    # The learned machine list for --pull/--remote (an ssh target or url per machine).
    # Real config -> the XDG config dir; the summaries it fetches are a cache elsewhere
    # (sources.default_remotes_dir, under the XDG cache dir).
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
    # Drop malformed entries (a hand-edited `"box": null`) so a fetch never trips over
    # a non-dict value -- see _fetch_summary / _pull_one.
    return {k: v for k, v in machines.items() if isinstance(k, str) and isinstance(v, dict)}


def _save_remotes(machines: dict) -> None:
    # Atomic (temp + replace) and best-effort, like the notes/cache writers: a config
    # we can't write must never break a launch.
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
    # Parse a --pull token into (machine name, entry):
    #   host / user@host        -> {"ssh": ...}, name = the host part
    #   http(s)://addr[:port]   -> {"url": ...}, name = the host
    #   name=<any of the above> -> that target, under the explicit name
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
    # The name is a label, not a path -- percent-encode it so distinct names never
    # collide (`a/b` vs `a_b`) and no separator can escape the remotes directory.
    from urllib.parse import quote

    safe = quote(name, safe="")
    if safe.startswith("."):
        # RemoteStore reads the remotes dir with glob("*.json"), which skips dotfiles --
        # so a name like ".box" must not produce a hidden summary the view can't see.
        safe = "%2E" + safe[1:]
    return safe + ".json"


def _fetch_summary(name: str, entry: dict, timeout: float = 60.0) -> str:
    # Fetch one machine's summary. SSH (the default) runs the exporter on the remote
    # over the user's existing ssh config -- nothing has to be listening there. A `url`
    # entry GETs it instead (an `opentab --serve` box on a trusted/VPN interface).
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
        ["ssh", target, cmd],
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
    # Fetch, validate, and save one machine's summary. Returns (session_count, "") on
    # success or (0, error). Never raises -- so one unreachable machine can't sink the
    # whole parallel pull; its slot just reports the failure.
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
        # Broad on purpose: this runs in a worker thread, and any escape would abort the
        # whole parallel pull at fut.result(). A malformed entry becomes this machine's
        # failure line, never everyone's.
        return 0, str(exc) or exc.__class__.__name__
    try:
        with open(os.path.join(remotes_dir, _summary_filename(name)), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        return 0, f"could not save: {exc}"
    return len(payload["workflows"]), ""


def pull_command(args: argparse.Namespace) -> None:
    # --pull: fetch machine summaries over SSH/HTTP in parallel, showing each machine's
    # progress as it lands, and learn the targets into remotes.json. Not a terminal
    # command -- main() then opens the merged view (--source remote).
    machines = _load_remotes()
    tokens = args.pull or []
    if tokens:  # explicit hosts: add/refresh (learn) them
        targets: dict = {}
        for spec in tokens:
            name, entry = _remote_entry(spec)
            saved = machines.get(name)
            if spec == name and isinstance(saved, dict) and (saved.get("ssh") or saved.get("url")):
                targets[name] = saved  # bare name of a known, usable machine: reuse it
            else:
                # Learn a new machine, or repair a saved entry that has no reachable
                # target (a hand-edited cmd-only entry) by folding in the derived ssh.
                merged = {**(saved or {}), **entry}
                # ssh and url are mutually exclusive target types -- a re-learn that
                # switches type must drop the old one (else _fetch_summary keeps using
                # the stale url); orthogonal fields like a custom `cmd` are preserved.
                if "ssh" in entry:
                    merged.pop("url", None)
                elif "url" in entry:
                    merged.pop("ssh", None)
                machines[name] = merged
                targets[name] = merged
        _save_remotes(machines)
    else:  # no hosts named: refresh every saved machine
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
    # Local, like util.read_files_parallel's: concurrent.futures costs ~10ms of
    # imports that only the SSH fan-out needs, and the one-shot commands (--status
    # polled from a status bar) paid it on every single invocation.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ok = 0
    with ThreadPoolExecutor(
        max_workers=min(len(targets), 8), thread_name_prefix="opentab-pull"
    ) as ex:
        futures = {ex.submit(_pull_one, n, e, remotes_dir): n for n, e in targets.items()}
        for fut in as_completed(futures):  # report each machine the moment it finishes
            name = futures[fut]
            count, error = fut.result()
            if error:
                sys.stderr.write(f"  ✗ {name} — {error}\n")
            else:
                ok += 1
                sys.stderr.write(f"  ✓ {name} — {count} sessions\n")
    sys.stderr.write(f"Pulled {ok}/{len(targets)} machine(s) into {remotes_dir}\n")


def _make_refresh_fn(args: argparse.Namespace):
    # A closure the TUI/web App calls to re-pull specific machines from inside opentab
    # (the `F` key / the web refresh button). Takes remotes.json keys, returns
    # [(name, session_count, error)] -- the same _pull_one workers as `--pull`, in
    # parallel. Bound to the run's --remotes dir so an in-app refresh writes where the
    # fleet reads.
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
    # A closure the TUI's `L` menu calls to turn a machine's remotes.json key into its
    # ssh target, so a session pulled from another box reopens on that box. Re-reads the
    # file per call (it is tiny, and this runs on a keystroke) so a machine learned by a
    # `--pull` in another terminal is launchable without restarting. `url` machines have
    # no ssh target and are deliberately absent: opentab fetches summaries over HTTP, but
    # it will not invent a shell on a box that only offered it a JSON endpoint.
    def targets() -> dict:
        return {
            name: str(entry["ssh"])
            for name, entry in _load_remotes().items()
            if isinstance(entry, dict) and entry.get("ssh")
        }

    return targets


def forget_command(args: argparse.Namespace) -> int:
    # --forget: drop machines from remotes.json and delete their cached summaries.
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


def web_command(args: argparse.Namespace) -> int:
    # --html / --serve: the web frontend, one-shot and curses-free. Builds the same
    # headless App the TUI drives -- rollups, worktree folding, saved prefs (ignored
    # projects, the restored range/$ view), and the real/API cost snapshots -- and
    # hands it to opentab.web. Import deferred so TUI startup doesn't pay for it.
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
        app._refresh_backend = _make_refresh_fn(args)  # the web /api/refresh endpoint
    if use_state:
        apply_state(app, args, state)
    app._ensure_models()  # the $ what-if snapshots ride on the per-model breakdown
    sys.stderr.write(" " * 40 + "\r")
    sys.stderr.flush()
    if args.serve or args.web:  # --web serves too, then pops the browser
        return web.serve_command(app, args)
    return web.html_command(app, args)


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(
            f"OpenTab requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
            f"(found {sys.version_info[0]}.{sys.version_info[1]})."
        )
    enable_unicode_locale()
    args = parse_args()  # handles --help first, so it works even without curses
    if not getattr(args, "demo", False):
        # One-time upgrade tidy: relocate the pre-split caches (cache/, prices.json,
        # remotes/) out of ~/.config into the XDG cache dir. Not in a path getter — those
        # feed --help; here it runs once and is a no-op forever after. --demo touches no
        # real files, so it's skipped there.
        paths.migrate_legacy_caches()
    if getattr(args, "refresh_models", False):
        return refresh_models_command()  # fetch prices and exit; no curses needed
    if getattr(args, "keymap", False):
        # Print (and first-run install) the keymap config path; no curses needed.
        print(bindings.ensure_user_keymap())
        return 0
    if getattr(args, "status", None) is not None:
        return status_command(args)  # one-shot for the tmux status line; no curses
    if getattr(args, "timings", False):
        return timings_command(args)  # startup profiler; no curses
    if getattr(args, "export", None) is not None:
        return export_command(args)  # portable machine summary; no curses
    if getattr(args, "forget", None):
        return forget_command(args)  # edit remotes.json and exit; no curses
    if getattr(args, "pull", None) is not None:
        pull_command(args)  # fetch machine summaries over ssh/http in parallel; no curses
    if getattr(args, "pull", None) is not None or getattr(args, "remote", False):
        # --pull/--remote view the consolidated machines; both the TUI and the
        # --html/--serve paths below read args.source, so set it before either.
        args.source = "remote"
        args.remotes = args.remotes or default_remotes_dir()
    if (
        getattr(args, "html", None) is not None
        or getattr(args, "serve", False)
        or getattr(args, "web", False)
    ):
        return web_command(args)  # HTML browser / local browser server; no curses
    if curses is None:
        raise SystemExit(
            "OpenTab needs Python's curses module, which native Windows Python doesn't bundle.\n"
            "  - Native Windows: pip install windows-curses, then rerun opentab.\n"
            "  - Or run opentab under WSL (where OpenCode's database usually lives anyway)."
        )
    # Load saved prefs first so the start source can be restored (resolve_source uses
    # it) and the store is built once for the right backend -- the model scan stays
    # deferred. Disabled by --demo / --no-state.
    use_state = not args.demo and not args.no_state
    state = load_state() if use_state else {}
    source_key = resolve_source(args, state)
    goto = None
    goto_hint = None
    if getattr(args, "tab", None) and getattr(args, "goto", None) is None:
        # --tab is meaningless without a session to open, so it stands in for a bare
        # --goto of the current directory: `opentab --tab context` == jump to the
        # cwd's live session, landing on its Context tab.
        args.goto = ""
    if getattr(args, "goto", None) is not None:
        # Resolve before the store is built: the target's backend must be in view,
        # so a saved single-source preference can't hide the session it names.
        goto = _goto_target(args)
        if goto is None:
            # Nothing to land in. Don't exit -- that just flash-closes the tmux
            # popup this flag was made for; open the plain TUI with a hint.
            goto_hint = _goto_hint(args.goto or os.getcwd())
        elif source_key not in ("all", "remote", goto[0]):
            # "all"/"remote" already compose goto[0]'s backend (remote's live box is the
            # local sources), so the target is in view -- don't collapse the merged/fleet
            # view down to a single source. Only a pinned single source needs overriding.
            source_key = goto[0]
    store, loading = sources.make_store(args, source_key)
    # The first load runs the recursive roll-up over the whole DB / parses every
    # transcript, which can take a beat at scale. Show a hint, then clear it before
    # curses starts.
    sys.stderr.write(loading)
    sys.stderr.flush()
    # The user's key bindings: install the commented default file on first run, then
    # load it (typos become toasts, never a refusal to start). Tests and the web path
    # construct App without one and get the pristine defaults.
    bindings.ensure_user_keymap()
    app = App(store, args, source_key=source_key, keymap=bindings.load_user_keymap())
    if source_key == "remote":
        # Let `F` in the TUI re-pull a machine over ssh (fleet view only), and `L`
        # reopen a pulled session on the box it actually ran on.
        app._refresh_backend = _make_refresh_fn(args)
        app._ssh_targets = _make_ssh_targets_fn()
    app.allow_price_prompt = use_state  # no startup prompt under --no-state/--demo
    # Session notes are authored data, so they live in their own file and carry their own
    # gate: --no-state turns them off for the run, while demo is re-checked live (`D`
    # toggles it) inside App.allow_notes. refresh_notes applies both.
    app.notes_enabled = not args.no_state
    sys.stderr.write(" " * 40 + "\r")
    sys.stderr.flush()
    if use_state:
        apply_state(app, args, state)
    # After apply_state, which ends by clearing the notice -- and so would wipe the
    # "your notes.json is unreadable" warning this can raise.
    notes_ok = app.refresh_notes()
    app.announce_keymap_warnings()  # a broken keymap.conf line greets, not breaks
    if goto is not None:
        # After apply_state (a restored range could hide the target; goto_session
        # clears it when needed), before curses -- the jump is state-only.
        app.goto_session(goto[1], tab=getattr(args, "tab", None))
    elif goto_hint and notes_ok:
        # a broken notes.json outranks the miss hint: no frame paints between the
        # two notify calls, so this one would collapse onto and bury the warning
        app.notify(goto_hint, "error")
    curses.wrapper(app.run)
    if use_state and not app.store.demo:
        save_state(app)
    return 0
