"""Machine command tree and explicit fleet management (no harness discovery)."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone

from opentab.presentation.cli_help import arrange_help

ALIASES = {"pull": "pull", "remote": "open", "export": "export", "forget": "remove"}


def add_parsers(subs, add_globals, source_paths, local_choices):
    root = subs.add_parser(
        "machines",
        description="Manage saved machines and portable usage summaries.",
        help="list, add, check and browse your machines",
        epilog="Examples:\n  opentab machines list\n  opentab machines add user@host\n  opentab machines pull\n\nNext: opentab machines COMMAND --help\nFleet setup: docs/machines.md",
    )
    verbs = root.add_subparsers(dest="machines_action", required=True, metavar="COMMAND")
    arrange_help(root)
    fleet_globals = {
        "source",
        *source_paths,
        "days",
        "since",
        "until",
        "demo",
        "no_state",
        "no_worktrees",
        "remotes",
        "theme",
        "no_cache",
    }
    descriptions = {
        "pull": "Fetch summaries over SSH or HTTP and open the combined fleet view.\n"
        "Hosts are remembered. With no host, refresh all saved machines.",
        "open": "Browse saved summaries offline, alongside local sessions.\n"
        "Pass files to open copied exports instead of the saved fleet.",
        "export": "Write this machine's usage as portable JSON: totals and models, no traces.\n"
        "Summaries can contain full user prompts. Review before sharing.",
        "remove": "Remove saved hosts and their local summaries. Remote data stays intact.",
    }

    def fleet_parser(parent, name, action, deprecated=False):
        description = descriptions[action]
        if deprecated:
            description = f"Deprecated: use opentab machines {action}.\n\n" + description
        kwargs = {} if deprecated else {"help": description.splitlines()[0]}
        parser = parent.add_parser(name, description=description, **kwargs)
        parser.epilog = (
            f"Examples:\n  opentab machines {action}"
            + (" old-laptop" if action == "remove" else "")
            + "\n\nFleet setup: docs/machines.md"
        )
        if action in ("pull", "open"):
            add_globals(
                parser,
                fleet_globals,
                source_choices=("auto", "remote"),
                source_help="fleet summaries plus every present local harness",
            )
            parser.add_argument(
                "hosts" if action == "pull" else "files",
                nargs="*",
                metavar="HOST" if action == "pull" else "FILE",
                help="SSH targets or hosted export URLs"
                if action == "pull"
                else "exported JSON files or directories (default: saved fleet)",
            )
            common = {"days", "demo", "remotes"}
        elif action == "export":
            add_globals(
                parser,
                {"source", *source_paths, "demo", "label", "no_cache"},
                source_choices=local_choices,
                source_help="local harness to export (default: all present harnesses)",
                help_overrides={
                    "demo": "anonymize selected export fields; partial categories leave other data real"
                },
            )
            parser.add_argument(
                "file",
                nargs="?",
                default="-",
                metavar="FILE",
                help="output file (default: - for stdout)",
            )
            common = {"label", "demo", "source"}
        else:
            add_globals(parser, {"remotes"})
            parser.add_argument("names", nargs="+", metavar="NAME", help="saved machine names")
            common = {"remotes"}
        arrange_help(
            parser,
            source_paths=source_paths,
            summary=description,
            common=common,
            brief={
                "days": "Show sessions started within this many days (default: all time).",
                "demo": "Anonymize all by default; partial categories leave other data real.",
                "source": "Local harness to export (default: all present).",
                "remotes": "Summary directory (default: OpenTab's saved fleet).",
            },
        )

    for action in descriptions:
        fleet_parser(verbs, action, action)
    for old, action in ALIASES.items():
        fleet_parser(subs, old, action, deprecated=True)
    for action, description in (
        ("list", "List saved machines, targets and cached summary metadata (offline)."),
        ("add", "Probe and save an SSH target or hosted export URL, without pulling."),
        ("status", "Check saved machines in parallel without updating their summaries."),
        (
            "rename",
            "Rename a saved connection and its cached summary; export labels stay unchanged.",
        ),
    ):
        parser = verbs.add_parser(action, help=description, description=description)
        parser.epilog = (
            f"Examples:\n  opentab machines {action}"
            + {"list": "", "add": " user@host", "status": "", "rename": " old new"}[action]
            + "\n\nFleet setup: docs/machines.md"
        )
        add_globals(parser, {"remotes"} if action != "add" else set())
        if action in ("list", "status"):
            parser.add_argument(
                "--json", action="store_true", help="emit a versioned JSON document"
            )
        if action == "status":
            parser.add_argument(
                "names", nargs="*", metavar="NAME", help="saved machines (default: all)"
            )
        elif action == "add":
            parser.add_argument("target", metavar="[NAME=]TARGET", help="SSH target or export URL")
            parser.add_argument(
                "--no-check", action="store_true", help="save without connecting (offline machine)"
            )
        elif action == "rename":
            parser.add_argument("old", metavar="OLD", help="current connection name")
            parser.add_argument("new", metavar="NEW", help="new connection name")
        arrange_help(parser)


@contextlib.contextmanager
def locked(path):
    """Stable sidecar lock; fail closed if an available locking API fails."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _editable_document(backend):
    document, readable = backend._read_remotes_document()
    if not readable:
        raise ValueError("could not read remotes.json; refusing to overwrite saved machines")
    version = document.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("unsupported remotes.json version; refusing to overwrite saved machines")
    document.setdefault("version", 1)
    document.setdefault("machines", {})
    return document


def update_remotes(backend, edit):
    """Re-read authored data under lock, preserving unknown envelope and entry fields."""
    path = backend.remotes_config_path()
    with locked(path):
        document = _editable_document(backend)
        result = edit(document["machines"])
        backend._atomic_text(path, json.dumps(document, indent=2, sort_keys=True))
        return result


def _valid_name(name):
    if not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("machine name must be nonempty and contain no control characters")


def _taken(name):
    return f"machine {name!r} already exists; choose another NAME=TARGET"


def _cache_metadata(backend, name, directory):
    try:
        filename = os.path.join(directory, backend._summary_filename(name))
        with open(filename, encoding="utf-8") as stream:
            payload = json.load(stream)
        stamp = os.path.getmtime(filename)
        return {
            "last_pull": datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds"),
            "cache_age_seconds": max(0, int(datetime.now(timezone.utc).timestamp() - stamp)),
            "exported_at": payload.get("exported_at")
            if isinstance(payload.get("exported_at"), str)
            else None,
            "opentab_version": payload.get("opentab_version")
            if isinstance(payload.get("opentab_version"), str)
            else None,
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return {
            "last_pull": None,
            "cache_age_seconds": None,
            "exported_at": None,
            "opentab_version": None,
        }


def _rows(backend, args):
    document, readable = backend._read_remotes_document()
    if not readable:
        raise ValueError("could not read remotes.json")
    directory = backend._remotes_write_dir(args)
    rows = []
    entries = {
        name: entry
        for name, entry in document.get("machines", {}).items()
        if isinstance(entry, dict)
    }
    for name, entry in sorted(entries.items()):
        kind = "url" if entry.get("url") else "ssh"
        rows.append(
            {
                "name": name,
                "kind": kind,
                "target": entry.get(kind),
                "custom_cmd": bool(entry.get("cmd")),
                **_cache_metadata(backend, name, directory),
            }
        )
    return rows, entries


# One batch-mode SSH call. Login-shell discovery is a fallback, not a second connection.
_SSH_PROBE = r"""
found=0
probe() {
    p=$1
    [ -n "$p" ] && [ -x "$p" ] || return 1
    found=1
    v=$("$p" --version 2>/dev/null) || return 1
    case "$v" in opentab\ *) printf '%s\n%s\n' "$p" "$v"; return 0;; esac
    return 1
}
p=$(command -v opentab 2>/dev/null)
probe "$p" && { printf 'default\n'; exit 0; }
p=$("${SHELL:-/bin/sh}" -lc 'command -v opentab' 2>/dev/null)
probe "$p" && exit 0
for p in "$HOME/.local/bin/opentab" "$HOME/.local/share/pipx/venvs/opentab-ai/bin/opentab" "$HOME/.local/pipx/venvs/opentab-ai/bin/opentab" "$HOME/.opentab/bin/opentab" "$HOME/.opentab/src/.venv/bin/opentab" "$HOME/.opentab/src/opentab"; do
    probe "$p" && exit 0
done
[ "$found" = 1 ] && exit 126
exit 127
"""


def _probe_ssh(entry):
    target = entry.get("ssh")
    if not isinstance(target, str) or not target or target.startswith("-"):
        return {"status": "error", "error": "invalid SSH target", "opentab_version": None}
    # ssh hands the command to the remote login shell, which may not be POSIX (fish).
    # Run the discovery script under /bin/sh; a custom cmd stays raw, exactly as pull sends it.
    discover = not (entry.get("cmd") or entry.get("trace_cmd"))
    script = "/bin/sh -c " + shlex.quote(_SSH_PROBE)
    if not discover:
        from opentab.remote_content import _argv_prefix, _derived_prefix

        prefix = (
            _argv_prefix(entry["trace_cmd"])
            if "trace_cmd" in entry
            else _derived_prefix(entry["cmd"])
        )
        if not prefix:
            return {
                "status": "error",
                "error": "custom cmd needs a usable trace_cmd to check its version",
                "opentab_version": None,
            }
        script = f"{prefix} --version"
    try:
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
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode:
            stderr = (proc.stderr or "").lower()
            status = (
                "auth required"
                if any(
                    s in stderr
                    for s in (
                        "permission denied",
                        "authentication failed",
                        "host key verification failed",
                    )
                )
                else "opentab missing"
                if proc.returncode == 127
                else "error"
            )
            return {
                "status": status,
                "error": "SSH authentication required"
                if status == "auth required"
                else "OpenTab not found; install opentab-ai on the remote"
                if status == "opentab missing"
                else "OpenTab was found but its version check failed"
                if proc.returncode == 126
                else "SSH probe failed",
                "opentab_version": None,
            }
        lines = proc.stdout.strip().splitlines()
        # Login-shell noise may precede the probe's record, so read the version, the path
        # before it and the default marker after it from the same (last) record.
        index = next(
            (i for i in reversed(range(len(lines))) if lines[i].startswith("opentab ")), None
        )
        version = lines[index].removeprefix("opentab ").strip() if index is not None else ""
        if not version:
            return {
                "status": "error",
                "error": "invalid OpenTab version response",
                "opentab_version": None,
            }
        result = {"status": "reachable", "error": None, "opentab_version": version}
        if discover:
            if index == 0 or not lines[index - 1].startswith("/"):
                return {
                    "status": "error",
                    "error": "probe did not return an absolute OpenTab path",
                    "opentab_version": version,
                }
            if lines[index + 1 : index + 2] != ["default"]:
                result["cmd"] = shlex.quote(lines[index - 1]) + " --export -"
                result["trace_cmd"] = [lines[index - 1]]
        return result
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "error", "error": "SSH unavailable or timed out", "opentab_version": None}


def probe(backend, name, entry):
    if not entry.get("url"):
        return _probe_ssh(entry)
    try:
        payload = json.loads(backend._fetch_summary(name, entry, timeout=30))
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("workflows"), list)
            or payload.get("opentab_export") not in (1, 2)
        ):
            raise ValueError("not an OpenTab export")
        return {
            "status": "reachable",
            "error": None,
            "opentab_version": payload.get("opentab_version")
            if isinstance(payload.get("opentab_version"), str)
            else None,
        }
    except (OSError, ValueError, RuntimeError, TypeError):
        return {
            "status": "url unreachable",
            "error": "URL unavailable or not an OpenTab export",
            "opentab_version": None,
        }


def _emit(args, rows):
    if args.json:
        print(json.dumps({"schema_version": 1, "machines": rows}))
    elif not rows:
        print("No saved machines. Add one: opentab machines add user@host")
    else:
        columns = ["name", "kind", "target", "custom_cmd", "last_pull", "opentab_version"]
        if args.machines_action == "status":
            columns += ["status", "cache_age_seconds", "version_skew", "error"]
        print("\t".join(columns))
        for row in rows:
            print(
                "\t".join(
                    re.sub(r"[\x00-\x1f\x7f]", "?", str(row.get(key)))
                    if row.get(key) is not None
                    else "-"
                    for key in columns
                )
            )


def command(args, backend):
    try:
        action = args.machines_action
        if action == "add":
            if backend._looks_like_a_summary_path(args.target):
                raise ValueError(
                    "target looks like a local summary; use opentab machines open FILE"
                )
            name, entry = backend._remote_entry(args.target)
            _valid_name(name)
            target = entry.get("url") or entry.get("ssh")
            if not isinstance(target, str) or not target or target.startswith("-"):
                raise ValueError("invalid machine target")
            # Fail fast on a taken name; the locked edit below re-checks for races.
            if name in _editable_document(backend)["machines"]:
                raise ValueError(_taken(name))
            if not args.no_check:
                result = probe(backend, name, entry)
                if result["status"] != "reachable":
                    raise ValueError(result["error"])
                if result.get("cmd"):
                    entry["cmd"] = result["cmd"]
                    from opentab.remote_content import _argv_prefix, _derived_prefix

                    # Turn reads run the derived prefix through the remote shell unquoted;
                    # keep the argv form whenever that text is not the quoted path (`'`, `$`).
                    trace = result.get("trace_cmd")
                    if trace and _derived_prefix(result["cmd"]) != _argv_prefix(trace):
                        entry["trace_cmd"] = trace

            def add(machines):
                if name in machines:
                    raise ValueError(_taken(name))
                machines[name] = entry

            update_remotes(backend, add)
            print(f"Added {name}. Next: opentab machines pull {shlex.quote(name)}")
            return 0
        if action == "rename":
            _valid_name(args.new)
            directory = backend._remotes_write_dir(args)
            old = os.path.join(directory, backend._summary_filename(args.old))
            new = os.path.join(directory, backend._summary_filename(args.new))
            moved = False

            def rename(machines):
                nonlocal moved
                if args.old not in machines:
                    raise ValueError(f"no such machine: {args.old}")
                if args.new in machines or os.path.lexists(new):
                    raise ValueError(f"machine or cached summary already exists: {args.new}")
                if os.path.lexists(old):
                    os.replace(old, new)
                    moved = True
                machines[args.new] = machines.pop(args.old)

            with locked(backend.remotes_config_path()):
                document = _editable_document(backend)
                try:
                    rename(document["machines"])
                    backend._atomic_text(
                        backend.remotes_config_path(),
                        json.dumps(document, indent=2, sort_keys=True),
                    )
                except BaseException:
                    if moved:
                        os.replace(new, old)
                    raise
            print(f"Renamed {args.old} to {args.new} (export label unchanged).")
            return 0
        rows, entries = _rows(backend, args)
        if action == "status":
            selected = args.names or list(entries)
            missing = set(selected) - set(entries)
            if missing:
                raise ValueError("no such machine: " + ", ".join(sorted(missing)))
            rows = [row for row in rows if row["name"] in selected]
            if rows:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
                    results = list(
                        pool.map(
                            lambda row: probe(backend, row["name"], entries[row["name"]]), rows
                        )
                    )
                for row, result in zip(rows, results):
                    result.pop("cmd", None)
                    result.pop("trace_cmd", None)
                    row.update(result)
                    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:$|[-+])", str(row["opentab_version"]))
                    row["version_skew"] = (
                        tuple(map(int, match.groups())) < (1, 22, 0) if match else None
                    )
        _emit(args, rows)
        return int(action == "status" and any(row["status"] != "reachable" for row in rows))
    except (OSError, ValueError) as exc:
        print(f"opentab machines: {exc}", file=sys.stderr)
        return 1
