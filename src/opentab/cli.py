"""Argument parsing and the main entry point."""
from __future__ import annotations

import argparse
import locale
import os
import sqlite3
import sys

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

from opentab import __version__, sources
from opentab.formatting import money
from opentab.pricing import (
    MODELS_DEV_URL,
    api_equivalent_cost,
    price_cache_path,
    refresh_model_prices,
)
from opentab.sources import (
    DEFAULT_CSV_PATH,
    DEFAULT_JSONL_PATH,
    _default_openclaw_dir,
    _default_pi_dir,
    _route_path_arg,
    resolve_source,
)
from opentab.state import apply_state, load_state, save_state
from opentab.stores.opencode import Store
from opentab.tui.app import App
from opentab.util import git_root, resolve_project_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="opentab", description="OpenTab — OpenCode spend TUI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--source",
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
            "all",
        ),
        default="auto",
        help="data source: opencode (SQLite), claude (Claude Code transcripts), codex "
        "(Codex CLI rollouts), hermes (Hermes Agent DB), csv (a CSV of logged API "
        "requests, e.g. GitHub Copilot), jsonl (an NDJSON of logged API requests), "
        "copilot (GitHub Copilot CLI via its OTEL export), vscode (Copilot Chat sessions "
        "in VS Code), pi (pi-agent sessions), openclaw (OpenClaw gateway sessions), or "
        "all (merged); auto merges every present "
        "source (default: auto). Or just pass a file path -- e.g. `opentab requests.csv`",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        metavar="PATH",
        help="a CSV file, an OpenCode .db, etc. to view -- its source is picked "
        "automatically (e.g. `opentab requests.csv`). Same as passing the matching "
        "--csv/--db flag; with --source it fills that source's path.",
    )
    parser.add_argument("--db", default=os.path.expanduser("~/.local/share/opencode/opencode.db"))
    parser.add_argument(
        "--claude-dir",
        default=os.path.expanduser("~/.claude/projects"),
        help="Claude Code projects directory (for --source claude)",
    )
    parser.add_argument(
        "--codex-dir",
        default=os.path.expanduser("~/.codex/sessions"),
        help="Codex CLI sessions directory (for --source codex)",
    )
    parser.add_argument(
        "--hermes-db",
        default=os.path.expanduser("~/.hermes/state.db"),
        help="Hermes Agent database path (for --source hermes)",
    )
    parser.add_argument(
        "--copilot-dir",
        default=os.path.expanduser("~/.copilot/otel"),
        help="GitHub Copilot CLI OpenTelemetry export directory (for --source copilot); "
        "the file named by $COPILOT_OTEL_FILE_EXPORTER_PATH is also read",
    )
    parser.add_argument(
        "--vscode-dir",
        default=None,
        help="a VS Code User directory (or chatSessions directory) holding Copilot Chat "
        "sessions (for --source vscode); by default every installed variant (Code, "
        "Code - Insiders, VSCodium) is scanned. From WSL, point it at the Windows-side "
        "store (not scanned by default -- reading through /mnt/c slows startup), e.g. "
        "alias opentab='opentab --vscode-dir \"/mnt/c/Users/<you>/AppData/Roaming/Code/User\"'",
    )
    parser.add_argument(
        "--pi-dir",
        default=_default_pi_dir(),
        help="pi-agent sessions directory (for --source pi); honors $PI_AGENT_DIR, "
        "default ~/.pi/agent/sessions",
    )
    parser.add_argument(
        "--openclaw-dir",
        default=_default_openclaw_dir(),
        help="OpenClaw gateway home holding agents/ and openclaw.json (for --source "
        "openclaw); honors $OPENCLAW_DIR, default ~/.openclaw",
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
        action="store_true",
        help="anonymize titles/paths and backfill synthetic prices "
        "(for live demos and screenshots; never writes to the DB)",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="do not read or write the saved range/sort state (~/.config/opentab)",
    )
    parser.add_argument(
        "--no-worktrees",
        action="store_true",
        help="do not fold git worktrees into their main repo (keep each path separate)",
    )
    parser.add_argument(
        "--status",
        nargs="?",
        const="",
        default=None,
        metavar="DIR|SESSION",
        help="print the cost of the most recently active OpenCode session (subagent "
        "subtree included) and exit; with DIR only sessions of that project count, "
        "with a session id (ses_...) exactly that session is priced. Made for a tmux "
        "status line: set -g status-right "
        "'#(opentab --status \"#{pane_current_path}\")'. A leading ~ marks a "
        "list-price estimate for usage recorded at $0 (subscription models)",
    )
    parser.add_argument(
        "--refresh-models",
        action="store_true",
        help="fetch the latest model list prices from models.dev into a local cache "
        f"({price_cache_path()}) and exit; the cache overlays the embedded table for the "
        "$ what-if estimate (also available with 'r' in the P prices overlay)",
    )
    args = parser.parse_args()
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


def _project_key(directory: str) -> str:
    # Fold a session's recorded cwd (or the pane path tmux hands us) onto the
    # project it belongs to, the same way the TUI groups projects: up to the git
    # root, then worktrees onto their main repo.
    return os.path.normpath(resolve_project_root(git_root(os.path.expanduser(directory))))


def status_line(store: Store, target: str | None = None) -> str:
    # The figure for the tmux segment: recorded cost of the most recently active
    # session's whole subtree, plus a list-price estimate for any $0 (subscription)
    # node -- prefixed "~" so a real dollar amount is never conflated with an
    # estimate (the one-shot sibling of the TUI's $ view / _priced_nodes). Empty
    # when nothing matches, so the segment simply disappears.
    #
    # `target` is a directory (price that project's most recent session) or an
    # OpenCode session id (price exactly that session -- the disambiguator when
    # several sessions run in one project, e.g. stamped per-pane by a tmux
    # plugin); a subagent id resolves to its root.
    workflow_id = None
    if target and target.startswith("ses_") and os.sep not in target:
        workflow_id = store.root_of(target)
    else:
        directory = target
        project = _project_key(directory) if directory else None
        for row in store.recent_roots():
            if project is None or _project_key(row["directory"]) == project:
                workflow_id = row["id"]
                break
    if workflow_id is None:
        return ""
    total = estimated = 0.0
    for node in store.workflow_nodes(workflow_id):
        total += node["cost"]
        if not node["cost"] and node["tokens_total"]:
            estimated += api_equivalent_cost(
                node["model_name"],
                node["tokens_input"],
                node["tokens_output"],
                node["tokens_reasoning"],
                node["tokens_cache_read"],
                node["tokens_cache_write"],
            )
    text = money(total + estimated)
    return "~" + text if estimated > 0 else text


def status_command(args: argparse.Namespace) -> int:
    # One-shot, curses-free sibling of --refresh-models, polled from a tmux status
    # line -- so every failure mode prints nothing (an empty segment) instead of
    # erroring the whole status bar.
    db = os.path.expanduser(args.db)
    if not os.path.exists(db):
        return 0
    try:
        line = status_line(Store(db, args), args.status or None)
    except sqlite3.Error:
        return 0
    if line:
        print(line)
    return 0


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(
            f"OpenTab requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
            f"(found {sys.version_info[0]}.{sys.version_info[1]})."
        )
    enable_unicode_locale()
    args = parse_args()  # handles --help first, so it works even without curses
    if getattr(args, "refresh_models", False):
        return refresh_models_command()  # fetch prices and exit; no curses needed
    if getattr(args, "status", None) is not None:
        return status_command(args)  # one-shot for the tmux status line; no curses
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
    store, loading = sources.make_store(args, source_key)
    # The first load runs the recursive roll-up over the whole DB / parses every
    # transcript, which can take a beat at scale. Show a hint, then clear it before
    # curses starts.
    sys.stderr.write(loading)
    sys.stderr.flush()
    app = App(store, args, source_key=source_key)
    app.allow_price_prompt = use_state  # no startup prompt under --no-state/--demo
    sys.stderr.write(" " * 40 + "\r")
    sys.stderr.flush()
    if use_state:
        apply_state(app, args, state)
    curses.wrapper(app.run)
    if use_state and not app.store.demo:
        save_state(app)
    return 0
