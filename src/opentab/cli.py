"""Argument parsing and the main entry point."""
from __future__ import annotations

import argparse
import locale
import os
import sys

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

from opentab import __version__, sources
from opentab.pricing import MODELS_DEV_URL, price_cache_path, refresh_model_prices
from opentab.sources import (
    DEFAULT_CSV_PATH,
    _default_openclaw_dir,
    _default_pi_dir,
    _route_path_arg,
    resolve_source,
)
from opentab.state import apply_state, load_state, save_state
from opentab.tui.app import App


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
            "copilot",
            "pi",
            "openclaw",
            "all",
        ),
        default="auto",
        help="data source: opencode (SQLite), claude (Claude Code transcripts), codex "
        "(Codex CLI rollouts), hermes (Hermes Agent DB), csv (a CSV of logged API "
        "requests, e.g. GitHub Copilot), copilot (GitHub Copilot CLI via its OTEL "
        "export), pi (pi-agent sessions), openclaw (OpenClaw gateway sessions), or all "
        "(merged); auto merges every present source (default: auto). Or just pass a file "
        "path -- e.g. `opentab requests.csv`",
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
