"""Fast local session picker: opens from cached rows, then refreshes in the background."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict
from datetime import datetime

from opentab import sources
from opentab.accounting.models import SessionRef, Workflow
from opentab.launch import resume_argv
from opentab.persistence import paths
from opentab.persistence.state import load_state, state_path
from opentab.presentation.formatting import display_width, pad, shorten
from opentab.stores.cached import CACHE_VERSION, MODEL_ROW_KEYS, CachedStore, cache_path
from opentab.util import local_machine_name, resolve_project_root


def _cache_path(key: str, root: str) -> str:
    return cache_path(f"{key}|{root}")


# Picker rows only, so an ordinary launch never parses the full rollup caches.
INDEX_VERSION = 1


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
        and bool(resume_argv(row))
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
            or not isinstance(payload.get("model_breakdown"), list)
            or any(
                not isinstance(model, dict)
                or not MODEL_ROW_KEYS <= model.keys()
                or not isinstance(model["root_id"], str)
                for model in payload["model_breakdown"]
            )
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


def _index_path(args, files) -> str:
    identity = json.dumps([args.source, [path for _, path in files]])
    name = hashlib.sha1(identity.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(paths.cache_dir(), "launch", f"{name}.json")


def _save_index(path: str, stamps: dict, rows: list[Workflow]) -> None:
    payload = {
        "version": INDEX_VERSION,
        "cache_version": CACHE_VERSION,
        "caches": stamps,
        "rows": [asdict(row) for row in rows],
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        CachedStore._write_json(path, payload)
    except OSError:
        pass  # Best effort: the next launch reads the rollup caches instead.


def _load(args) -> list[Workflow]:
    """Cached picker rows, from the index while every rollup cache it saw is unchanged."""
    files = _cache_files(args)
    # Stamp before reading, so a cache replaced mid-read invalidates the new index.
    stamps = {path: _stamp(path) for _, path in files}
    index = _index_path(args, files)
    try:
        with open(index, encoding="utf-8") as fh:
            payload = json.load(fh)
        if (
            payload["version"] == INDEX_VERSION
            and payload["cache_version"] == CACHE_VERSION
            and payload["caches"] == stamps
        ):
            labels = {sources.SOURCE_LABELS[key] for key, _ in files}
            rows = [Workflow(**row) for row in payload["rows"]]
            if all(row.source in labels and _launchable(row, row.source) for row in rows):
                return rows
    except (OSError, ValueError, TypeError, KeyError):
        pass
    rows = list(_cached_sessions(args, files))
    _save_index(index, stamps, rows)
    return rows


def _clean(value: str) -> str:
    # fzf input has one record per NUL and one column per TAB. Never let authored
    # titles/paths alter that framing or draw terminal control sequences.
    return "".join(
        " " if ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF else c
        for c in value
    ).strip()


def _activity(row: Workflow) -> str:
    return row.ended_at or row.created_at


def _display_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return _clean(value)


def _project_label(directory: str) -> str:
    path = _clean(directory).replace("\\", "/").rstrip("/")
    for marker in ("/.worktrees/", "/.worktree/"):
        if marker in path:
            root, branch = path.split(marker, 1)
            return f"{root.rsplit('/', 1)[-1]} / {branch}"
    return path.rsplit("/", 1)[-1] or path


def _layout(rows: list[Workflow], columns: int) -> tuple[int, int, bool, int]:
    """Column widths, fixed for the picker's lifetime so a reloaded list keeps the header."""
    projects = [_project_label(row.directory) for row in rows]
    project_width = min(max(8, columns // 6), 26, max(7, *(display_width(p) for p in projects)))
    tool_width = max(4, *(display_width(row.source) for row in rows))
    # Give the title priority in small terminals. Wide panes retain the full date.
    show_tool = columns >= 60
    time_width = 16 if columns >= 120 else 11 if columns >= 80 else 0
    return project_width, tool_width, show_tool, time_width


def _picker_table(rows: list[Workflow], layout, prefix: str = "") -> tuple[str, str]:
    """Aligned metadata, with the full searchable title in the flexible last column."""
    project_width, tool_width, show_tool, time_width = layout
    reset, muted, accent = "\x1b[0m", "\x1b[90m", "\x1b[36m"
    separator = f"{muted} │ {reset}"

    def cells(tool: str, project: str, updated: str, title: str, header=False) -> str:
        fields = []
        if show_tool:
            fields.append(
                f"{muted if header else accent}{pad(shorten(tool, tool_width), tool_width)}{reset}"
            )
        fields.append(
            f"{muted if header else ''}{pad(shorten(project, project_width), project_width)}{reset}"
        )
        if time_width:
            fields.append(f"{muted}{pad(shorten(updated, time_width), time_width)}{reset}")
        fields.append(f"{muted if header else ''}{title}{reset}")
        return separator.join(fields)

    header = cells("TOOL", "PROJECT", "UPDATED", "SESSION", header=True)
    records = []
    for index, row in enumerate(rows):
        updated = _display_time(_activity(row))
        if time_width == 11 and len(updated) == 16:
            updated = updated[5:]  # MM-DD HH:MM
        # Keep the title whole: fzf clips the visual tail, but still searches it.
        line = cells(
            row.source, _project_label(row.directory), updated, _clean(row.title) or "(untitled)"
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


def _reloads(fzf: str) -> bool:
    """fzf 0.36 added the load event and reload-sync that swap in the refreshed list."""
    if sys.platform == "win32" or not hasattr(os, "mkfifo"):
        return False
    try:
        version = subprocess.run(
            [fzf, "--version"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
        major, minor = (int(part) for part in version.split()[0].split(".")[:2])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return False
    return (major, minor) >= (0, 36)


class _BackgroundRefresh:
    """Refresh the caches while fzf shows the cached list, then hand it the new one.

    fzf reads the replacement from a FIFO through reload-sync, which keeps the cached
    list on screen until the write completes. Replacement rows use ``r``-prefixed keys
    so a selection always resolves against the list it was made from.
    """

    def __init__(self, args, cached: list[Workflow], rows: list[Workflow], layout) -> None:
        self.rows: list[Workflow] = []
        self._directory = tempfile.mkdtemp(prefix="opentab-launch-")
        self.fifo = os.path.join(self._directory, "sessions")
        os.mkfifo(self.fifo, 0o600)
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(args, cached, rows, layout),
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

    def _run(self, args, cached: list[Workflow], rows: list[Workflow], layout) -> None:
        menu = None
        try:
            latest = _visible(args, _refreshed(args, cached))
            if latest and latest != rows:
                menu = _picker_table(latest, layout, prefix="r")[0]
                self.rows = latest
        except Exception:
            # Never disturb the picker from a background thread; the cached list stays.
            menu = None
        if menu is None:
            menu = _picker_table(rows, layout)[0]
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


def _refresh(args) -> dict[str, list[Workflow]]:
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
            store.model_breakdown()
        finally:
            connection = getattr(store, "conn", None)
            if connection is not None:
                connection.close()
        refreshed[label] = [row for row in rows if _launchable(row, label)]
    return refreshed


def _refreshed(args, cached: list[Workflow]) -> list[Workflow]:
    """Refresh available harnesses; keep cached rows for the ones that are not."""
    refreshed = _refresh(args)
    # Stamped after the writes. A concurrent writer slipping in between leaves the
    # index marginally behind its cache until the next launch refreshes again.
    files = _cache_files(args)
    rows = [row for row in cached if row.source not in refreshed]
    rows.extend(row for found in refreshed.values() for row in found)
    _save_index(_index_path(args, files), {path: _stamp(path) for _, path in files}, rows)
    return rows


def launch_command(args) -> int:
    cached = _load(args)
    if args.refresh:
        print("launch: refreshing local caches...", file=sys.stderr)
        try:
            cached = _refreshed(args, cached)
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
    fzf = shutil.which("fzf")
    if not fzf:
        print("launch: fzf not found on PATH (install fzf to use the picker)", file=sys.stderr)
        return 1
    # Opaque ordinal selection avoids collisions across titles, harnesses and IDs.
    layout = _layout(rows, shutil.get_terminal_size((100, 24)).columns)
    menu, heading = _picker_table(rows, layout)
    background = None
    if not args.refresh and not args.no_refresh and _reloads(fzf):
        try:
            background = _BackgroundRefresh(args, cached, rows, layout)
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
                "--delimiter=\\t",
                "--with-nth=2..",
                # --nth addresses the fields AFTER --with-nth has hidden the key.
                "--nth=..",
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
                *(["--bind", bind] if bind else []),
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
