"""Fast local session picker: opens from cached rows, then refreshes in the background."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime

from opentab import __version__, sources
from opentab.accounting.models import SessionRef, Workflow
from opentab.launch import resume_argv
from opentab.persistence import paths
from opentab.persistence.state import load_state, state_path
from opentab.presentation.formatting import display_width, pad, shorten
from opentab.stores.cached import (
    CACHE_VERSION,
    MODEL_ROW_KEYS,
    CachedStore,
    cache_path,
    is_sha1_hex,
)
from opentab.util import local_machine_name, resolve_project_root


def _cache_path(key: str, root: str) -> str:
    return cache_path(f"{key}|{root}")


# Picker rows only, so an ordinary launch never parses the full rollup caches.
# v2 adds API-equivalent costs, priced by the refresh and never on the open path.
INDEX_VERSION = 2

# (source, id, created_at, ended_at, total_cost, total_tokens, unpriced_tokens): a
# cost stays valid while its session is unchanged, even after a rollup rebuild.
CostKey = tuple


class Costs(dict):
    """Session costs, tagged with the rate-table stamp they were priced under."""

    def __init__(self, items=(), pricing=None) -> None:
        super().__init__(items)
        self.pricing = pricing


def _cache_files(args) -> list[tuple[str, str]]:
    # Resolve exactly the same configured roots as sources._wrap_cache; globbing the
    # directory would include stale caches for roots no longer configured here.
    files = []
    for key, label in sources.SOURCE_LABELS.items():
        if label not in sources.RESUME_COMMANDS or key == "all":
            continue
        if args.source not in ("auto", "all", key):
            continue
        root = getattr(args, sources._PATH_SLOT[key], "") or ""
        files.append((key, _cache_path(key, root)))
    return files


def _launchable(row: Workflow, label: str) -> bool:
    return (
        row.source == label
        and not row.machine
        and all(
            isinstance(field, str)
            for field in (row.id, row.title, row.directory, row.created_at, row.ended_at)
        )
        # The cost key hashes these; damaged values must reject the row, not crash.
        and all(
            isinstance(field, (int, float)) and not isinstance(field, bool)
            for field in (row.total_cost, row.total_tokens, row.unpriced_tokens)
        )
        and bool(resume_argv(row))
    )


def _models_shaped(payload: dict) -> bool:
    # Launch lists only workflows, so a sidecar's rows are never read here; inline
    # rows (older writers, hand-made fixtures) keep CachedStore's shape check.
    if "model_breakdown" not in payload:
        external = payload.get("models_external")
        return isinstance(external, dict) and is_sha1_hex(external.get("sha1"))
    rows = payload["model_breakdown"]
    return isinstance(rows, list) and all(
        isinstance(model, dict)
        and MODEL_ROW_KEYS <= model.keys()
        and isinstance(model["root_id"], str)
        for model in rows
    )


def _cached_sessions(args, files=None):
    for key, path in files if files is not None else _cache_files(args):
        label = sources.SOURCE_LABELS[key]
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("version") != CACHE_VERSION
            or payload.get("source") != key
            or not isinstance(payload.get("workflows"), list)
            or not _models_shaped(payload)
        ):
            continue
        # A malformed payload is rejected as a unit, as for CachedStore._read().
        try:
            rows = [Workflow(**row) for row in payload["workflows"]]
        except (TypeError, ValueError):
            continue
        yield from (row for row in rows if _launchable(row, label))


def _stamp(path: str) -> list[int] | None:
    # Every cache write replaces the file, so a new inode marks it even when the
    # size and a coarse mtime happen to match.
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_ino, st.st_size, st.st_mtime_ns]


def _cost_key(row: Workflow) -> CostKey:
    return (
        row.source,
        row.id,
        row.created_at,
        row.ended_at,
        row.total_cost,
        row.total_tokens,
        row.unpriced_tokens,
    )


def _pricing_stamp() -> list:
    """Identity of the rate tables: `P` replaces prices.json, upgrades the bundled table.

    Stat only, so the open path never imports or parses the pricing module.
    """
    bundled = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "models.json")
    prices = os.path.join(paths.cache_dir(), "prices.json")  # pricing.price_cache_path()
    return [__version__, _stamp(prices), _stamp(bundled)]


def _parse_costs(entries, pricing) -> Costs:
    costs = Costs(pricing=pricing)
    for entry in entries:
        if not isinstance(entry, list) or len(entry) != 8:
            continue
        cost = entry[7]
        if not all(isinstance(part, (str, int, float)) for part in entry[:7]):
            continue
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            try:
                value = float(cost)
            except OverflowError:  # a JSON integer beyond float range
                continue
            if math.isfinite(value):
                costs[tuple(entry[:7])] = value
    return costs


def _price(rows: list[Workflow], models) -> Costs:
    """API-equivalent total per session: the TUI's `$` figure, from the same helper."""
    from opentab.accounting.tiers import price_root_models

    by_root = defaultdict(list)
    for model in models or ():
        if isinstance(model, dict) and isinstance(model.get("root_id"), str):
            by_root[model["root_id"]].append(dict(model))  # never mutate the store's rows
    costs = {}
    for row in rows:
        found = by_root.get(row.id)
        costs[_cost_key(row)] = row.total_cost + (price_root_models(found)[0] if found else 0.0)
    return costs


def _index_path(args, files) -> str:
    identity = json.dumps([args.source, [path for _, path in files]])
    name = hashlib.sha1(identity.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(paths.cache_dir(), "launch", f"{name}.json")


def _save_index(path: str, stamps: dict, rows: list[Workflow], costs: Costs, pricing) -> None:
    payload = {
        "version": INDEX_VERSION,
        "cache_version": CACHE_VERSION,
        "caches": stamps,
        "rows": [asdict(row) for row in rows],
        "pricing": pricing,
        "costs": [[*key, cost] for key, cost in costs.items()],
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        CachedStore._write_json(path, payload)
    except OSError:
        pass  # Best effort: the next launch reads the rollup caches instead.


def _load(args) -> tuple[list[Workflow], Costs]:
    """Cached picker rows, from the index while every rollup cache it saw is unchanged.

    Costs survive only under the rate tables they were priced with. A rollup rebuild
    keeps the cost of every session whose identity and usage did not change.
    """
    files = _cache_files(args)
    # Stamp before reading, so a cache replaced mid-read invalidates the new index.
    stamps = {path: _stamp(path) for _, path in files}
    pricing = _pricing_stamp()
    index = _index_path(args, files)
    costs = Costs(pricing=pricing)
    try:
        with open(index, encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload["version"] == INDEX_VERSION and payload["cache_version"] == CACHE_VERSION:
            if payload.get("pricing") == pricing and isinstance(payload.get("costs"), list):
                costs = _parse_costs(payload["costs"], pricing)
            if payload["caches"] == stamps:
                labels = {sources.SOURCE_LABELS[key] for key, _ in files}
                rows = [Workflow(**row) for row in payload["rows"]]
                if all(row.source in labels and _launchable(row, row.source) for row in rows):
                    return rows, costs
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    rows = list(_cached_sessions(args, files))
    keys = {_cost_key(row) for row in rows}
    costs = Costs(((key, cost) for key, cost in costs.items() if key in keys), pricing)
    _save_index(index, stamps, rows, costs, pricing)
    return rows, costs


def _clean(value: str) -> str:
    # fzf input has one record per NUL and one column per TAB. Never let authored
    # titles/paths alter that framing or draw terminal control sequences.
    return "".join(
        " " if ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF else c
        for c in value
    ).strip()


def _activity(row: Workflow) -> str:
    return row.ended_at or row.created_at


def _display_time(value: str, year: int | None = None) -> str:
    """MM-DD HH:MM this year, YYYY-MM-DD before it: one 11-cell column either way."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _clean(value)
    this_year = year if year is not None else datetime.now().year
    return moment.strftime("%m-%d %H:%M" if moment.year == this_year else "%Y-%m-%d")


def _cost_text(cost: float) -> str:
    """At most five cells, rounding included, so a repriced reload keeps the header."""
    if cost <= 0:
        return "$0"
    if cost < 0.005:
        return "<$.01"
    if cost < 9.995:
        return f"${cost:.2f}"
    if cost < 999.5:
        return f"${cost:.0f}"
    if cost < 9950:
        return f"${cost / 1000:.1f}k"
    if cost < 999500:
        return f"${cost / 1000:.0f}k"
    return f"${cost / 1e6:.1f}M" if cost < 9.95e6 else "$10M+"


def _project_label(directory: str) -> str:
    path = _clean(directory).replace("\\", "/").rstrip("/")
    for marker in ("/.worktrees/", "/.worktree/"):
        if marker in path:
            root, branch = path.split(marker, 1)
            return f"{root.rsplit('/', 1)[-1]} / {branch}"
    return path.rsplit("/", 1)[-1] or path


# Fields are split on the visible separator too, so --nth can skip the cost cell.
_SEPARATOR = " │ "
_DELIMITER = "\t|" + _SEPARATOR
_UNPRICED = "—"
_COST_WIDTH = 5  # "API $", "$9.99", "$347", "$1.2k", "<$.01"


def _layout(
    rows: list[Workflow], columns: int, costs: Costs | None = None
) -> tuple[int, int, bool, int, int]:
    """Column widths, fixed for the picker's lifetime so a reloaded list keeps the header."""
    # Fit nine in ten project names; one long worktree name must not widen every row.
    widths = sorted(display_width(_project_label(row.directory)) for row in rows)
    typical = widths[(len(widths) - 1) * 9 // 10] if widths else 0
    project_width = min(max(8, columns // 6), 26, max(7, typical))
    tool_width = max(4, *(display_width(row.source) for row in rows))
    # Give the title priority in small terminals: the cost goes first, then the date.
    show_tool = columns >= 60
    time_width = 11 if columns >= 80 else 0
    cost_width = _COST_WIDTH if columns >= 100 else 0
    return project_width, tool_width, show_tool, time_width, cost_width


def _search_fields(layout) -> str:
    """fzf --nth over the fields after --with-nth hid the key: everything but the cost."""
    _project_width, _tool_width, show_tool, time_width, cost_width = layout
    if not cost_width:
        return ".."
    cost = int(show_tool) + 1 + bool(time_width) + 1
    return f"..{cost - 1},{cost + 1}.."


def _picker_table(
    rows: list[Workflow], layout, costs: Costs | None = None, prefix: str = ""
) -> tuple[str, str]:
    """Aligned metadata, with the full searchable title in the flexible last column."""
    project_width, tool_width, show_tool, time_width, cost_width = layout
    reset, muted, accent = "\x1b[0m", "\x1b[90m", "\x1b[36m"
    separator = f"{muted}{_SEPARATOR}{reset}"
    costs = costs or {}

    def cells(tool: str, project: str, updated: str, cost: str, title: str, header=False) -> str:
        fields = []
        if show_tool:
            fields.append(
                f"{muted if header else accent}{pad(shorten(tool, tool_width), tool_width)}{reset}"
            )
        # A separator inside a cell would shift the field --nth leaves out.
        project = shorten(project.replace("│", "|"), project_width)
        fields.append(f"{muted if header else ''}{pad(project, project_width)}{reset}")
        if time_width:
            fields.append(f"{muted}{pad(shorten(updated, time_width), time_width)}{reset}")
        if cost_width:
            fields.append(f"{muted}{' ' * max(0, cost_width - display_width(cost))}{cost}{reset}")
        fields.append(f"{muted if header else ''}{title}{reset}")
        return separator.join(fields)

    header = cells("TOOL", "PROJECT", "UPDATED", "API $", "SESSION", header=True)
    records = []
    year = datetime.now().year
    for index, row in enumerate(rows):
        updated = _display_time(_activity(row), year)
        cost = costs.get(_cost_key(row))
        # Keep the title whole: fzf clips the visual tail, but still searches it.
        line = cells(
            row.source,
            _project_label(row.directory),
            updated,
            _UNPRICED if cost is None else _cost_text(cost),
            _clean(row.title) or "(untitled)",
        )
        records.append(f"{prefix}{index}\t{line}\0")
    return "".join(records), header


def _visible(args, rows: list[Workflow]) -> list[Workflow]:
    """Drop ignored sessions and projects; newest activity first."""
    if not args.no_state:
        state = load_state(state_path(migrate=False))
        projects = state.get("ignored_projects")
        sessions = state.get("ignored_sessions")
        ignored_projects = (
            {p for p in projects if isinstance(p, str) and p}
            if isinstance(projects, list)
            else set()
        )
        ignored_sessions = (
            {s for s in sessions if isinstance(s, str) and s}
            if isinstance(sessions, list)
            else set()
        )
        harnesses = {label: key for key, label in sources.SOURCE_LABELS.items()}
        rows = [
            row
            for row in rows
            if row.id not in ignored_sessions
            and SessionRef(local_machine_name(), harnesses[row.source], row.id).encode()
            not in ignored_sessions
            and (
                resolve_project_root(row.directory)
                if ignored_projects and not args.no_worktrees
                else row.directory
            )
            not in ignored_projects
        ]
    return sorted(rows, key=_activity, reverse=True)


def _fzf_version(fzf: str) -> tuple[int, int] | None:
    try:
        version = subprocess.run(
            [fzf, "--version"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
        major, minor = (int(part) for part in version.split()[0].split(".")[:2])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return major, minor


def _reloads() -> bool:
    """Whether a FIFO can carry the refreshed list (fzf side checked separately)."""
    return sys.platform != "win32" and hasattr(os, "mkfifo")


def _tmux_popup(version: tuple[int, int] | None) -> list[str]:
    """fzf 0.53 added --tmux. A popup receives keys before tmux's own bindings, so
    keys a pane would lose to tmux (Alt-J/K pane navigation) reach the picker."""
    if not os.environ.get("TMUX") or version is None or version < (0, 53):
        return []
    return ["--tmux=center,100%,100%"]


class _BackgroundRefresh:
    """Refresh the caches while fzf shows the cached list, then hand it the new one.

    fzf reads the replacement from a FIFO through reload-sync, which keeps the cached
    list on screen until the write completes. Replacement rows use ``r``-prefixed keys
    so a selection always resolves against the list it was made from.
    """

    def __init__(
        self, args, cached: list[Workflow], costs: Costs, rows: list[Workflow], layout
    ) -> None:
        self.rows: list[Workflow] = []
        self._directory = tempfile.mkdtemp(prefix="opentab-launch-")
        self.fifo = os.path.join(self._directory, "sessions")
        os.mkfifo(self.fifo, 0o600)
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(args, cached, costs, rows, layout),
            name="opentab-launch-refresh",
            daemon=True,
        )
        self._thread.start()

    def bind(self) -> str | None:
        # fzf expands {…} placeholders and parses parentheses inside actions; the
        # colon form must close the chain. Refuse any path that could be misread.
        if not re.fullmatch(r"[\w./-]+", self.fifo):
            return None
        return f"load:unbind(load)+reload-sync:cat {shlex.quote(self.fifo)}"

    def _run(
        self, args, cached: list[Workflow], costs: Costs, rows: list[Workflow], layout
    ) -> None:
        menu = None
        try:
            latest, priced = _refreshed(args, cached, costs)
            latest = _visible(args, latest)

            def shown(table, prices):
                return [prices.get(_cost_key(row)) for row in table]

            # Same sessions with newly priced or repriced costs also earn a reload.
            if latest and (latest != rows or shown(latest, priced) != shown(rows, costs)):
                menu = _picker_table(latest, layout, priced, prefix="r")[0]
                self.rows = latest
        except Exception:
            # Never disturb the picker from a background thread; the cached list stays.
            menu = None
        if menu is None:
            menu = _picker_table(rows, layout, costs)[0]
        if self._closed.is_set():
            return
        try:
            with open(self.fifo, "wb") as fh:
                fh.write(menu.encode("utf-8"))
        except OSError:
            pass

    def close(self) -> None:
        self._closed.set()
        # A writer still blocked opening the FIFO is released by a transient reader.
        try:
            os.close(os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass
        shutil.rmtree(self._directory, ignore_errors=True)


def _refresh(args) -> dict[str, tuple[list[Workflow], Costs]]:
    # Both refresh paths use the normal local cache path. model_breakdown
    # completes CachedStore's atomic write; workflows alone do not persist it.
    keys = [args.source] if args.source not in ("auto", "all") else sources.available_sources(args)
    refreshed = {}
    for key in keys:
        label = sources.SOURCE_LABELS.get(key)
        if label not in sources.RESUME_COMMANDS:
            continue
        store, _ = sources.make_store(args, key)
        try:
            rows = store.workflows()
            models = store.model_breakdown()
        finally:
            connection = getattr(store, "conn", None)
            if connection is not None:
                connection.close()
        rows = [row for row in rows if _launchable(row, label)]
        try:
            costs = _price(rows, models)
        except Exception:  # noqa: BLE001 -- a pricing fault must not cost the session list
            costs = {}
        refreshed[label] = (rows, costs)
    return refreshed


def _refreshed(
    args, cached: list[Workflow], costs: Costs | None = None
) -> tuple[list[Workflow], Costs]:
    """Refresh available harnesses; keep cached rows and costs for the ones that are not."""
    # Stamped before pricing loads, so a price refresh mid-way invalidates these costs.
    pricing = _pricing_stamp()
    refreshed = _refresh(args)
    # Stamped after the writes. A concurrent writer slipping in between leaves the
    # index marginally behind its cache until the next launch refreshes again.
    files = _cache_files(args)
    rows = [row for row in cached if row.source not in refreshed]
    # Unrefreshed harnesses keep their costs only under the rates they were priced with.
    carried = costs if getattr(costs, "pricing", None) == pricing else {}
    kept = Costs(((k, c) for k, c in carried.items() if k[0] not in refreshed), pricing)
    for found, priced in refreshed.values():
        rows.extend(found)
        kept.update(priced)
    stamps = {path: _stamp(path) for _, path in files}
    _save_index(_index_path(args, files), stamps, rows, kept, pricing)
    return rows, kept


def launch_command(args) -> int:
    # Check before --refresh so a missing picker fails fast, not after a full refresh.
    fzf = shutil.which("fzf")
    if not fzf:
        print("launch: fzf not found on PATH (install fzf to use the picker)", file=sys.stderr)
        return 1
    cached, costs = _load(args)
    if args.refresh:
        print("launch: refreshing local caches...", file=sys.stderr)
        try:
            cached, costs = _refreshed(args, cached, costs)
        except (OSError, sqlite3.Error, ValueError) as exc:
            print(f"launch: cache refresh failed: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    rows = _visible(args, cached)
    if not rows:
        print(
            "launch: no cached local sessions; run `opentab launch --refresh` first",
            file=sys.stderr,
        )
        return 1
    # Opaque ordinal selection avoids collisions across titles, harnesses and IDs.
    layout = _layout(rows, shutil.get_terminal_size((100, 24)).columns, costs)
    menu, heading = _picker_table(rows, layout, costs)
    background = None
    refreshing = not args.refresh and not args.no_refresh and _reloads()
    version = _fzf_version(fzf) if refreshing or args.tmux else None
    # fzf 0.36 added the load event and reload-sync that swap in the refreshed list.
    if refreshing and version is not None and version >= (0, 36):
        try:
            background = _BackgroundRefresh(args, cached, costs, rows, layout)
        except OSError:
            background = None
    bind = background.bind() if background else None
    if background and not bind:
        background.close()
        background = None
    # User-wide fzf bindings/flags can change stdout framing, enable multi-select,
    # or auto-accept a result. This picker owns its one-row selection protocol.
    env = {**os.environ, "FZF_DEFAULT_OPTS": "", "FZF_DEFAULT_OPTS_FILE": ""}
    try:
        result = subprocess.run(
            [
                fzf,
                "--ansi",
                "--read0",
                "--print0",
                f"--delimiter={_DELIMITER}",
                "--with-nth=2..",
                # --nth addresses the fields AFTER --with-nth has hidden the key.
                f"--nth={_search_fields(layout)}",
                "--tiebreak=index",
                "--layout=reverse",
                "--border=rounded",
                "--border-label= OpenTab · Sessions ",
                "--padding=0,1",
                "--info=inline-right",
                "--no-hscroll",
                "--ellipsis=…",
                "--color=border:8,header:8,info:8,prompt:6,pointer:6,hl:3,hl+:3",
                "--prompt=Find session › ",
                f"--header=Enter resume · Esc cancel · newest first\n\n{heading}",
                # Vim-style movement that leaves the query keys alone; the user's own
                # fzf bindings are dropped with FZF_DEFAULT_OPTS above.
                "--bind=alt-j:down,alt-k:up",
                *(["--bind", bind] if bind else []),
                *(_tmux_popup(version) if args.tmux else []),
            ],
            input=menu.encode("utf-8"),
            capture_output=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        print(f"launch: could not start fzf: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if background:
            background.close()
    if result.returncode in (1, 130):
        return 0  # Esc or Ctrl-C
    if result.returncode != 0:
        print(
            f"launch: fzf failed (exit {result.returncode}): {result.stderr.decode('utf-8', 'replace').strip()}",
            file=sys.stderr,
        )
        return 1
    try:
        selected = result.stdout.removesuffix(b"\0").decode("utf-8")
        key = selected.split("\t", 1)[0]
        prefix, table = ("r", background.rows) if background and key[:1] == "r" else ("", rows)
        index = int(key[len(prefix) :])
        if not 0 <= index < len(table) or "\0" in selected:
            raise ValueError("invalid selection")
        row = table[index]
        if not selected.startswith(f"{prefix}{index}\t"):
            raise ValueError("invalid selection")
    except (UnicodeError, ValueError, IndexError):
        print("launch: fzf returned an invalid selection", file=sys.stderr)
        return 1
    directory, argv = resume_argv(row)
    directory = directory or os.path.expanduser("~")
    if not os.path.isdir(directory):
        print(f"launch: session directory no longer exists: {directory}", file=sys.stderr)
        return 1
    executable = shutil.which(argv[0])
    if not executable:
        print(f"launch: {argv[0]} not found on PATH", file=sys.stderr)
        return 1
    try:
        # Inherit this terminal's three streams; no shell interprets session data.
        argv = [os.path.abspath(executable), *argv[1:]]
        if sys.platform != "win32":
            # Replace OpenTab so Ctrl-C reaches the harness without a Python parent
            # interpreting a cancelled agent turn as a reason to kill the process.
            os.chdir(directory)
            os.execv(argv[0], argv)
            return 0  # execv only returns in tests
        with subprocess.Popen(argv, cwd=directory) as process:
            while True:
                try:
                    return process.wait()
                except KeyboardInterrupt:
                    # The console also delivered Ctrl-C to the harness. It decides
                    # whether that means cancel the current turn or exit entirely.
                    continue
    except OSError as exc:
        print(f"launch: could not resume session: {exc}", file=sys.stderr)
        return 1
