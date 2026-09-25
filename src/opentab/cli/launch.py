"""Fast cached local session picker, without backend discovery on ordinary launch."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime

from opentab import sources
from opentab.accounting.models import SessionRef, Workflow
from opentab.launch import resume_argv
from opentab.persistence.state import load_state, state_path
from opentab.presentation.formatting import display_width, pad, shorten
from opentab.stores.cached import CACHE_VERSION, MODEL_ROW_KEYS, cache_path
from opentab.util import local_machine_name, resolve_project_root


def _cache_path(key: str, root: str) -> str:
    return cache_path(f"{key}|{root}")


def _cached_sessions(args):
    # Resolve exactly the same configured roots as sources._wrap_cache; globbing the
    # directory would include stale caches for roots no longer configured here.
    for key, label in sources.SOURCE_LABELS.items():
        if label not in sources.RESUME_COMMANDS or key == "all":
            continue
        if args.source not in ("auto", "all", key):
            continue
        root = getattr(args, sources._PATH_SLOT[key], "") or ""
        path = _cache_path(key, root)
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
        for row in rows:
            if (
                row.source == label
                and not row.machine
                and all(
                    isinstance(field, str)
                    for field in (row.id, row.title, row.directory, row.created_at, row.ended_at)
                )
                and resume_argv(row)
            ):
                yield row


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


def _picker_table(rows: list[Workflow], columns: int) -> tuple[str, str]:
    """Aligned metadata, with the full searchable title in the flexible last column."""
    projects = [_project_label(row.directory) for row in rows]
    project_width = min(max(8, columns // 6), 26, max(7, *(display_width(p) for p in projects)))
    tool_width = max(4, *(display_width(row.source) for row in rows))
    # Give the title priority in small terminals. Wide panes retain the full date.
    show_tool = columns >= 60
    time_width = 16 if columns >= 120 else 11 if columns >= 80 else 0
    reset, muted, accent = "\x1b[0m", "\x1b[90m", "\x1b[36m"
    separator = f"{muted} │ {reset}"

    def cells(tool: str, project: str, updated: str, title: str, header=False) -> str:
        fields = []
        if show_tool:
            fields.append(f"{muted if header else accent}{pad(tool, tool_width)}{reset}")
        fields.append(
            f"{muted if header else ''}{pad(shorten(project, project_width), project_width)}{reset}"
        )
        if time_width:
            fields.append(f"{muted}{pad(shorten(updated, time_width), time_width)}{reset}")
        fields.append(f"{muted if header else ''}{title}{reset}")
        return separator.join(fields)

    header = cells("TOOL", "PROJECT", "UPDATED", "SESSION", header=True)
    records = []
    for index, (row, project) in enumerate(zip(rows, projects)):
        updated = _display_time(_activity(row))
        if time_width == 11 and len(updated) == 16:
            updated = updated[5:]  # MM-DD HH:MM
        # Keep the title whole: fzf clips the visual tail, but still searches it.
        line = cells(row.source, project, updated, _clean(row.title) or "(untitled)")
        records.append(f"{index}\t{line}\0")
    return "".join(records), header


def _refresh(args) -> None:
    # Explicitly requested refresh is the normal local cache path. model_breakdown
    # completes CachedStore's atomic write; workflows alone do not persist it.
    keys = [args.source] if args.source not in ("auto", "all") else sources.available_sources(args)
    for key in keys:
        if sources.SOURCE_LABELS.get(key) not in sources.RESUME_COMMANDS:
            continue
        store, _ = sources.make_store(args, key)
        try:
            store.workflows()
            store.model_breakdown()
        finally:
            connection = getattr(store, "conn", None)
            if connection is not None:
                connection.close()


def launch_command(args) -> int:
    if args.refresh:
        print("launch: refreshing local caches...", file=sys.stderr)
        try:
            _refresh(args)
        except (OSError, sqlite3.Error, ValueError) as exc:
            print(f"launch: cache refresh failed: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    rows = list(_cached_sessions(args))
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
    rows.sort(key=lambda row: _activity(row), reverse=True)
    # Opaque ordinal selection avoids collisions across titles, harnesses and IDs.
    menu, heading = _picker_table(rows, shutil.get_terminal_size((100, 24)).columns)
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
        index = int(selected.split("\t", 1)[0])
        if not 0 <= index < len(rows) or "\0" in selected:
            raise ValueError("invalid selection")
        row = rows[index]
        if not selected.startswith(f"{index}\t"):
            raise ValueError("invalid selection")
    except (UnicodeError, ValueError, IndexError):
        print("launch: fzf returned an invalid selection", file=sys.stderr)
        return 1
    directory, argv = resume_argv(row)
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
