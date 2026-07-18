"""Argument parsing and the main entry point."""
from __future__ import annotations

import argparse
import json
import locale
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

from opentab import __version__, sources, themes
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
from opentab.tui.app import App
from opentab.util import git_root, resolve_project_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="opentab", description="OpenTab — OpenCode spend TUI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
        help="which harness's spend to browse: opencode (SQLite), claude (Claude Code "
        "transcripts), codex (Codex CLI rollouts), hermes (Hermes Agent DB), csv (a CSV of "
        "logged API requests, e.g. GitHub Copilot), jsonl (an NDJSON of logged API "
        "requests), copilot (GitHub Copilot CLI via its OTEL export), vscode (Copilot Chat "
        "sessions in VS Code), pi (pi-agent sessions), openclaw (OpenClaw gateway "
        "sessions), zaly (Zaly sessions), all (merged), or remote (other machines' "
        "exported summaries, gathered by --pull/--export). auto merges every present "
        "local harness (default: auto). Or just pass a file path -- e.g. `opentab "
        "requests.csv` (--source is a deprecated alias for --harness)",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        metavar="PATH",
        help="a CSV file, an OpenCode .db, etc. to view -- its harness is picked "
        "automatically (e.g. `opentab requests.csv`). Same as passing the matching "
        "--csv/--db flag; with --harness it fills that harness's path.",
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

    # --remote/--pull select the fleet, but their wiring in main() runs AFTER this command
    # returns -- so honour them here too, else `opentab --remote --timings` would profile
    # the local "all" merge and never touch the RemoteStore the user asked to measure.
    if getattr(args, "remote", False) or getattr(args, "pull", None) is not None:
        args.source = "remote"
        args.remotes = getattr(args, "remotes", None) or default_remotes_dir()

    t_start = time.perf_counter()
    present, detect_ms = timed(lambda: sources.available_sources(args))
    source_key = resolve_source(args, {})  # no saved state -> measure a clean start
    (store, _loading), build_ms = timed(lambda: sources.make_store(args, source_key))

    # One row per backend: its whole parse+scan cost and whether it came from the cache.
    # We keep the rolled-up workflows too (row[5]) -- they're already in memory, and the
    # fleet breakdown below re-aggregates them per machine and per harness rather than
    # re-parsing anything.
    backends: list[list] = []  # [label, files, ms, cached, sub, workflows]
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
        _mb, mb_ms = timed(sub.model_breakdown)
        cached = getattr(sub, "served_from_cache", None)
        backends.append([label, files, wf_ms + mb_ms, cached, sub, wf])
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
    for label, files, ms, cached, _sub, _wf in backends:
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


def _col_table(
    headers: list[str],
    rows: list[list[str]],
    aligns: str,
    indent: str = "  ",
    rule_before_last: bool = False,
) -> list[str]:
    # A plain fixed-width table for the --timings fleet breakdown: pad every column to the
    # widest cell, "l"/"r"-align per the aligns string, 2-space gutters. rule_before_last
    # draws a horizontal rule above the final row (the TOTAL). Returns lines; the caller
    # prints them. E501 is off here (fixed-width columns), same as the TUI f-strings.
    cols = len(headers)
    widths = [len(headers[i]) for i in range(cols)]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(r[i]))

    def fmt(cells: list[str]) -> str:
        parts = [
            cells[i].rjust(widths[i]) if aligns[i] == "r" else cells[i].ljust(widths[i])
            for i in range(cols)
        ]
        return (indent + "  ".join(parts)).rstrip()

    body_w = sum(widths) + 2 * (cols - 1)
    lines = [fmt(headers)]
    for idx, r in enumerate(rows):
        if rule_before_last and idx == len(rows) - 1:
            lines.append(indent + "─" * body_w)
        lines.append(fmt(r))
    return lines


def _fleet_aggregate(workflows: list) -> tuple[dict, dict, dict]:
    # Roll the fleet's sessions up two ways and cross-tabbed, from the already-parsed
    # Workflow rows (each carries .machine, .source, .total_cost, .total_tokens): per
    # machine, per harness, and machine -> harness -> [sessions, tokens, cost]. Pure over
    # the rows so it's testable without a store.
    by_machine: dict[str, list] = {}
    by_harness: dict[str, list] = {}
    cell: dict[str, dict[str, list]] = {}
    for w in workflows:
        m = w.machine or "(this machine)"
        h = w.source or "?"
        for table, key in ((by_machine, m), (by_harness, h)):
            e = table.setdefault(key, [0, 0, 0.0])
            e[0] += 1
            e[1] += w.total_tokens
            e[2] += w.total_cost
        c = cell.setdefault(m, {}).setdefault(h, [0, 0, 0.0])
        c[0] += 1
        c[1] += w.total_tokens
        c[2] += w.total_cost
    return by_machine, by_harness, cell


def _fleet_timing_tables(store, backends: list) -> list[str]:
    # Build the fleet breakdown printed after the per-backend load table: By machine, By
    # harness, and the machine x harness session grid. Returns the lines to print (empty
    # when there's nothing worth breaking down -- one box AND one tool).
    all_wf = [w for row in backends for w in (row[5] or [])]
    by_machine, by_harness, cell = _fleet_aggregate(all_wf)
    multi_machine = len(by_machine) >= 2
    multi_harness = len(by_harness) >= 2
    if not (multi_machine or multi_harness):
        return []

    meta = getattr(store, "machine_meta", {}) or {}
    live_label = next((n for n, m in meta.items() if m.get("live")), None)

    # Where the load time went: the RemoteStore row is the pulled read (per box via its
    # byte share); every other backend row is a harness parsed on THIS machine.
    byte_by_machine: dict[str, int] = {}
    remote_ms = 0.0
    live_load_ms = 0.0
    harness_time: dict[str, float] = {}
    for _label, _files, ms, _cached, sub, wf in backends:
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
        ]

    if multi_machine:
        # live box first, then heaviest spend, then most sessions
        order = sorted(
            by_machine,
            key=lambda n: (n != live_label, -by_machine[n][2], -by_machine[n][0]),
        )
        rows = []
        for name in order:
            sess, toks, cost = by_machine[name]
            live = name == live_label
            mark = "● " if live else "○ "
            size = "—" if live else _human_bytes(byte_by_machine.get(name, 0))
            age = "live" if live else relative_age(meta.get(name, {}).get("exported_at", ""))
            rows.append(
                [
                    mark + name,
                    f"{sess:,}",
                    human_tokens(toks),
                    money(cost),
                    size,
                    _fmt_ms(machine_load(name)),
                    age,
                ]
            )
        tsess, ttoks, tcost = _totals(by_machine)
        rows.append(
            [
                "fleet",
                f"{tsess:,}",
                human_tokens(ttoks),
                money(tcost),
                _human_bytes(total_bytes),
                _fmt_ms(live_load_ms + remote_ms),
                "",
            ]
        )
        out.append("")
        out.append("  By machine")
        out.extend(
            _col_table(
                ["machine", "sess", "tokens", "cost", "size", "load", "age"],
                rows,
                aligns="lrrrrrl",
                rule_before_last=True,
            )
        )

    if multi_harness:
        order = sorted(by_harness, key=lambda h: (-by_harness[h][0], h))
        rows = []
        for key in order:
            sess, toks, cost = by_harness[key]
            t = harness_time.get(key)
            row = [SOURCE_LABELS.get(key, key), f"{sess:,}", human_tokens(toks), money(cost)]
            if multi_machine:  # a "boxes" count is only informative once there's a fleet
                row.append(str(sum(1 for m in cell if key in cell[m])))
            row.append(_fmt_ms(t) if t is not None else "—")
            rows.append(row)
        tsess, ttoks, tcost = _totals(by_harness)
        total_row = ["all", f"{tsess:,}", human_tokens(ttoks), money(tcost)]
        if multi_machine:
            total_row.append(str(len(cell)))
        total_row.append(_fmt_ms(live_load_ms))
        headers = (
            ["harness", "sess", "tokens", "cost"] + (["boxes"] if multi_machine else []) + ["load"]
        )
        out.append("")
        note = (
            "   (load = this machine's parse; pulled boxes arrive pre-rolled)"
            if multi_machine
            else ""
        )
        out.append("  By harness" + note)
        out.extend(
            _col_table(
                headers,
                [*rows, total_row],
                aligns="lrrr" + ("r" if multi_machine else "") + "r",
                rule_before_last=True,
            )
        )

    if multi_machine and multi_harness:
        m_order = sorted(by_machine, key=lambda n: (n != live_label, -by_machine[n][0]))
        h_order = sorted(by_harness, key=lambda h: (-by_harness[h][0], h))
        headers = ["machine"] + [SOURCE_LABELS.get(h, h) for h in h_order] + ["Σ"]
        grid = []
        for name in m_order:
            cells = [("● " if name == live_label else "○ ") + name]
            for h in h_order:
                n = cell.get(name, {}).get(h, [0])[0]
                cells.append(f"{n:,}" if n else "·")
            cells.append(f"{by_machine[name][0]:,}")
            grid.append(cells)
        foot = ["Σ"]
        for h in h_order:
            foot.append(f"{by_harness[h][0]:,}")
        foot.append(f"{sum(v[0] for v in by_machine.values()):,}")
        grid.append(foot)
        aligns = "l" + "r" * (len(h_order) + 1)
        table = _col_table(headers, grid, aligns=aligns, rule_before_last=True)
        width = max((len(line) for line in table), default=0)
        out.append("")
        if width <= 118:  # a grid too wide to read is worse than none -- the flat tables have it
            out.append("  Sessions by machine × harness")
            out.extend(table)
        else:
            out.append(
                f"  (machine × harness grid omitted -- {len(h_order)} harnesses too wide for one row)"
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


def _goto_hint(target: str) -> str:
    # The toast shown when --goto resolves to nothing -- typically a project whose
    # agent was just launched and hasn't recorded a turn yet. The flag was made
    # for a tmux popup binding, where exiting would flash the error and close, so
    # main opens the ordinary TUI and this says why it didn't jump.
    if _is_session_target(target):
        return f"--goto: session {target} not found in any source"
    return f"--goto: no session yet in {short_path(target, 40)}"


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
    # The learned machine list for --pull/--remote (an ssh target or url per machine),
    # beside the remotes/ directory of summaries it fetches into.
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "opentab", "remotes.json")


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
    if getattr(args, "refresh_models", False):
        return refresh_models_command()  # fetch prices and exit; no curses needed
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
    if getattr(args, "goto", None) is not None:
        # Resolve before the store is built: the target's backend must be in view,
        # so a saved single-source preference can't hide the session it names.
        goto = _goto_target(args)
        if goto is None:
            # Nothing to land in. Don't exit -- that just flash-closes the tmux
            # popup this flag was made for; open the plain TUI with a hint.
            goto_hint = _goto_hint(args.goto or os.getcwd())
        elif source_key not in ("all", goto[0]):
            source_key = goto[0]
    store, loading = sources.make_store(args, source_key)
    # The first load runs the recursive roll-up over the whole DB / parses every
    # transcript, which can take a beat at scale. Show a hint, then clear it before
    # curses starts.
    sys.stderr.write(loading)
    sys.stderr.flush()
    app = App(store, args, source_key=source_key)
    if source_key == "remote":
        # Let `F` in the TUI re-pull a machine over ssh (fleet view only).
        app._refresh_backend = _make_refresh_fn(args)
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
    if goto is not None:
        # After apply_state (a restored range could hide the target; goto_session
        # clears it when needed), before curses -- the jump is state-only.
        app.goto_session(goto[1])
    elif goto_hint and notes_ok:
        # a broken notes.json outranks the miss hint: no frame paints between the
        # two notify calls, so this one would collapse onto and bury the warning
        app.notify(goto_hint, "error")
    curses.wrapper(app.run)
    if use_state and not app.store.demo:
        save_state(app)
    return 0
