"""Argument parsing and the main entry point."""
from __future__ import annotations

import argparse
import locale
import os
import sqlite3
import sys
import time

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

from opentab import __version__, sources, themes
from opentab.formatting import cost_bar, money
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
    _default_zaly_dir,
    _route_path_arg,
    resolve_source,
)
from opentab.state import apply_state, load_state, save_state
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
            "zaly",
            "all",
        ),
        default="auto",
        help="data source: opencode (SQLite), claude (Claude Code transcripts), codex "
        "(Codex CLI rollouts), hermes (Hermes Agent DB), csv (a CSV of logged API "
        "requests, e.g. GitHub Copilot), jsonl (an NDJSON of logged API requests), "
        "copilot (GitHub Copilot CLI via its OTEL export), vscode (Copilot Chat sessions "
        "in VS Code), pi (pi-agent sessions), openclaw (OpenClaw gateway sessions), "
        "zaly (Zaly sessions), or all (merged); auto merges every present "
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
        "--zaly-dir",
        default=_default_zaly_dir(),
        help="Zaly data directory holding sessions/ (for --source zaly); honors "
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
        help="print the cost of the most recently active agent session (subagent "
        "subtree included) and exit, consulting every present harness backend "
        "(OpenCode, Claude Code, Codex, Hermes, pi, OpenClaw, Zaly); with DIR only "
        "sessions of that project count, with a session id (ses_... or a UUID -- the "
        "id is matched to its own backend) exactly that session is priced, and "
        "--source pins one backend. Made for a tmux status line: set -g "
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
        "--html",
        nargs="?",
        const="opentab-report.html",
        default=None,
        metavar="FILE",
        help="write a self-contained HTML browser and exit: drill-in by month/day/"
        "project/session, calendar heat map, sortable tables, the $ what-if toggle "
        "-- all client-side in one file (default FILE: opentab-report.html). "
        "Pairs with --demo for a shareable page",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="serve the HTML browser from a local web server; adds the per-session "
        "Turns/Tools drill-in as live endpoints and a data-refresh button "
        "(Ctrl-C stops it)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="like --serve, but also open it in your default web browser "
        "(cross-platform via the stdlib webbrowser: `open` on macOS, `xdg-open` on "
        "Linux, the shell association on Windows); honors --port/--bind",
    )
    parser.add_argument(
        "--theme",
        choices=themes.THEME_IDS,
        default=themes.DEFAULT_THEME,
        help="colour theme for the TUI and the --html/--serve browser (opentab, "
        "catppuccin-mocha/latte, tokyo-night/-day, gruvbox, nord, dracula, rose-pine); "
        "switch live in the TUI with C or the browser's theme button, and your choice is "
        f"remembered. Default: {themes.DEFAULT_THEME}",
    )
    parser.add_argument(
        "--port", type=int, default=8321, help="port for --serve/--web (default: 8321)"
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="address for --serve/--web (default: 127.0.0.1). The browser exposes prompt "
        "titles, project paths, and spend -- bind beyond localhost only on a "
        "trusted/VPN (e.g. Tailscale) interface, never a public one",
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
        "--no-cache",
        action="store_true",
        help="skip the warm-start rollup cache and always re-parse from scratch. The "
        "cache (under ~/.config/opentab/cache) reuses the previous parse when a backend's "
        "files are unchanged; use this to force a cold read or to measure it",
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

    t_start = time.perf_counter()
    present, detect_ms = timed(lambda: sources.available_sources(args))
    source_key = resolve_source(args, {})  # no saved state -> measure a clean start
    (store, _loading), build_ms = timed(lambda: sources.make_store(args, source_key))

    # One row per backend: its whole parse+scan cost and whether it came from the cache.
    backends: list[list] = []  # [label, files, ms, cached]
    for sub in getattr(store, "stores", None) or [store]:
        label = getattr(sub, "source_name", type(sub).__name__)
        files = None
        files_fn = getattr(sub, "_files", None)  # only the file-based backends have it
        if callable(files_fn):
            try:
                files = len(files_fn())
            except OSError:
                files = None
        _wf, wf_ms = timed(sub.workflows)
        _mb, mb_ms = timed(sub.model_breakdown)
        backends.append([label, files, wf_ms + mb_ms, getattr(sub, "served_from_cache", None)])
    total_ms = (time.perf_counter() - t_start) * 1000.0
    backends.sort(key=lambda b: b[2], reverse=True)  # slowest backend first

    flags = [c for _, _, _, c in backends if c is not None]
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
    for label, files, ms, cached in backends:
        fcell = str(files) if files is not None else "—"
        status = {True: "cached", False: "parsed"}.get(cached, "")
        bar = cost_bar(ms, peak, 12)
        print(f"  {label.ljust(lbl)}  {fcell:>5}  {fmt_ms(ms)}  {bar} {status}".rstrip())
    print()
    print(f"  {'total'.ljust(lbl)}  {'':>5}  {fmt_ms(total_ms)}")
    return 0


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


def _status_line_all(args: argparse.Namespace, target: str | None) -> str:
    stores = _status_stores(args)
    if target and _is_session_target(target):
        # The id itself names its backend: every store's root_of answers from a
        # cheap filename/dir/SQL lookup (never a parse), so probe each and let
        # the first claimant price it -- ids are UUIDs or ses_-prefixed, so a
        # cross-backend collision is not a realistic concern.
        for store in stores:
            line = status_line(store, target)
            if line:
                return line
        return ""
    # Directory (or nothing): the most recently active root across the backends
    # wins, so whichever tool you drove last is the one priced.
    project = _project_key(target) if target else None
    best_store, best = None, None
    for store in stores:
        candidate = _status_candidate(store, project)
        if candidate and (best is None or candidate[1] > best[1]):
            best_store, best = store, candidate
    if best is None:
        return ""
    return _price_root(best_store, best[0])


def _goto_target(args: argparse.Namespace) -> tuple[str, str] | None:
    # Resolve --goto's target to (source key, root session id) with the --status
    # machinery: a session id is probed via each backend's root_of (a subagent id
    # walks up to its root), a directory takes the project's most recently active
    # root across the backends. Returns None when nothing matches.
    target = args.goto or os.getcwd()
    keys = [k for k in sources.available_sources(args) if k in _STATUS_SOURCES]
    source = getattr(args, "source", "auto")
    if source not in ("auto", "all"):  # an explicit --source pins one backend
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


def status_command(args: argparse.Namespace) -> int:
    # One-shot, curses-free sibling of --refresh-models, polled from a tmux status
    # line -- so every failure mode prints nothing (an empty segment) instead of
    # erroring the whole status bar.
    try:
        line = _status_line_all(args, args.status or None)
    except (sqlite3.Error, OSError, ValueError):
        return 0
    if line:
        print(line)
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
    if getattr(args, "refresh_models", False):
        return refresh_models_command()  # fetch prices and exit; no curses needed
    if getattr(args, "status", None) is not None:
        return status_command(args)  # one-shot for the tmux status line; no curses
    if getattr(args, "timings", False):
        return timings_command(args)  # startup profiler; no curses
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
    if getattr(args, "goto", None) is not None:
        # Resolve before the store is built: the target's backend must be in view,
        # so a saved single-source preference can't hide the session it names.
        goto = _goto_target(args)
        if goto is None:
            raise SystemExit(f"--goto: no session found for {args.goto or os.getcwd()!r}")
        if source_key not in ("all", goto[0]):
            source_key = goto[0]
    store, loading = sources.make_store(args, source_key)
    # The first load runs the recursive roll-up over the whole DB / parses every
    # transcript, which can take a beat at scale. Show a hint, then clear it before
    # curses starts.
    sys.stderr.write(loading)
    sys.stderr.flush()
    app = App(store, args, source_key=source_key)
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
    app.refresh_notes()
    if goto is not None:
        # After apply_state (a restored range could hide the target; goto_session
        # clears it when needed), before curses -- the jump is state-only.
        app.goto_session(goto[1])
    curses.wrapper(app.run)
    if use_state and not app.store.demo:
        save_state(app)
    return 0
