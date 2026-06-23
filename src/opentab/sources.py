"""Backend discovery, selection, and store construction (make_store)."""
from __future__ import annotations

import argparse
import glob
import os

from opentab.stores.claude import ClaudeStore
from opentab.stores.codex import CodexStore
from opentab.stores.combined import CombinedStore
from opentab.stores.copilot import CopilotStore
from opentab.stores.csv_source import CsvStore
from opentab.stores.hermes import HermesStore
from opentab.stores.jsonl_source import JsonlStore
from opentab.stores.openclaw import OpenClawStore
from opentab.stores.opencode import Store
from opentab.stores.pi import PiStore

DEFAULT_CSV_PATH = os.path.expanduser("~/.config/opentab/requests.csv")
DEFAULT_JSONL_PATH = os.path.expanduser("~/.config/opentab/requests.jsonl")


def _default_pi_dir() -> str:
    # pi-agent honors $PI_AGENT_DIR (a comma-separated list; we take the first dir);
    # otherwise its sessions live under ~/.pi/agent/sessions.
    env = (os.environ.get("PI_AGENT_DIR") or "").split(",")[0].strip()
    return env or os.path.expanduser("~/.pi/agent/sessions")


def _default_openclaw_dir() -> str:
    # OpenClaw honors $OPENCLAW_DIR (a comma-separated list; we take the first
    # dir); otherwise its gateway home (holding agents/ and openclaw.json) is ~/.openclaw.
    env = (os.environ.get("OPENCLAW_DIR") or "").split(",")[0].strip()
    return env or os.path.expanduser("~/.openclaw")


# Which --flag each concrete source reads its path from (used to route a bare
# positional path into the right slot).
_PATH_SLOT = {
    "csv": "csv",
    "jsonl": "jsonl",
    "opencode": "db",
    "claude": "claude_dir",
    "codex": "codex_dir",
    "hermes": "hermes_db",
    "copilot": "copilot_dir",
    "pi": "pi_dir",
    "openclaw": "openclaw_dir",
}


def _infer_source_from_path(path: str) -> str | None:
    # Guess which backend a bare positional path belongs to, by shape. A directory is
    # ambiguous (Claude Code vs Codex both use dirs), so it returns None and the caller
    # asks for an explicit --source.
    low = path.lower()
    if low.endswith(".csv"):
        return "csv"
    if low.endswith((".jsonl", ".ndjson")):
        return "jsonl"  # a single .jsonl FILE is the logged-request source (the dir-
        # based JSONL backends -- claude/codex/pi/openclaw/copilot -- want --source)
    if low.endswith((".db", ".sqlite", ".sqlite3")):
        return "opencode"
    return None


def _route_path_arg(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # Make "point opentab at a file" intuitive so you never have to name the source
    # twice. All of these show that CSV:
    #     opentab requests.csv
    #     opentab --csv requests.csv
    #     opentab --source csv requests.csv
    # With an explicit --source the positional fills THAT source's path; with no
    # --source the source is inferred from the path (.csv -> csv, .db -> opencode) and
    # opentab opens it on its own. A bare `opentab` is unchanged (auto-merge).
    csv_explicit = args.csv is not None
    jsonl_explicit = args.jsonl is not None
    path = args.path
    if path is not None:
        if not os.path.exists(path):
            parser.error(f"no such file or directory: {path}")
        if args.source in ("auto", "all"):
            target = _infer_source_from_path(path)
            if target is None:
                parser.error(f"can't tell which source {path!r} is -- pass --source explicitly")
            if args.source == "auto":
                args.source = target  # view that file's source on its own
        else:
            target = args.source
        slot = _PATH_SLOT.get(target)
        if slot is None:  # e.g. a path given with --source all but no usable extension
            parser.error(f"--source {target} does not take a path argument")
        setattr(args, slot, path)
        if target == "csv":
            csv_explicit = True
        elif target == "jsonl":
            jsonl_explicit = True
    # An explicit --csv/--jsonl (with no --source) also means "just show that file".
    if csv_explicit and args.source == "auto":
        args.source = "csv"
    if jsonl_explicit and args.source == "auto":
        args.source = "jsonl"
    if args.csv is None:
        args.csv = DEFAULT_CSV_PATH
    if args.jsonl is None:
        args.jsonl = DEFAULT_JSONL_PATH


def _jsonl_dir_available(directory: str) -> bool:
    return os.path.isdir(directory) and bool(
        glob.glob(os.path.join(directory, "**", "*.jsonl"), recursive=True)
    )


def _openclaw_available(root_dir: str) -> bool:
    # OpenClaw sessions live at <root>/agents/<agent>/sessions/<id>.jsonl (plus archives);
    # check that precise shape so an unrelated ~/.openclaw/**/*.jsonl never trips detection.
    if not root_dir or not os.path.isdir(root_dir):
        return False
    return bool(glob.glob(os.path.join(root_dir, "agents", "*", "sessions", "*.jsonl")))


def _copilot_otel_available(args: argparse.Namespace) -> bool:
    # Copilot CLI usage lives only in its (opt-in) OTEL export: the export directory
    # holding *.jsonl, or the single file named by $COPILOT_OTEL_FILE_EXPORTER_PATH.
    if _jsonl_dir_available(getattr(args, "copilot_dir", "")):
        return True
    extra = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH") or ""
    return bool(extra) and os.path.isfile(extra)


# Display names for the source keys, matching each backend's source_name.
SOURCE_LABELS = {
    "opencode": "OpenCode",
    "claude": "Claude Code",
    "codex": "Codex",
    "hermes": "Hermes",
    "csv": "CSV",
    "jsonl": "JSONL",
    "copilot": "Copilot",
    "pi": "Pi",
    "openclaw": "OpenClaw",
    "all": "all",
}

# How each tool reopens one of its sessions (the `L` key copies this command,
# keyed by Workflow.source).
RESUME_COMMANDS = {
    "OpenCode": "opencode --session",
    "Claude Code": "claude --resume",
    "Codex": "codex resume",
    "Hermes": "hermes --resume",
    "Copilot": "copilot --resume",
    "Pi": "pi --session",
}


def available_sources(args: argparse.Namespace) -> list[str]:
    # The single-source backends actually present on this machine, in priority order.
    keys = []
    if os.path.exists(args.db):
        keys.append("opencode")
    if _jsonl_dir_available(args.claude_dir):
        keys.append("claude")
    if _jsonl_dir_available(getattr(args, "codex_dir", "")):
        keys.append("codex")
    if os.path.exists(getattr(args, "hermes_db", "")):
        keys.append("hermes")
    if os.path.exists(getattr(args, "csv", "")):
        keys.append("csv")
    if os.path.exists(getattr(args, "jsonl", "")):
        keys.append("jsonl")
    if _copilot_otel_available(args):
        keys.append("copilot")
    if _jsonl_dir_available(getattr(args, "pi_dir", "")):
        keys.append("pi")
    if _openclaw_available(getattr(args, "openclaw_dir", "")):
        keys.append("openclaw")
    return keys


def source_cycle(args: argparse.Namespace) -> list[str]:
    # The order the `c` key cycles through: each present source, plus "all" (merged)
    # when at least two exist. Demo merges too -- CombinedStore shares one hidden scale.
    keys = available_sources(args)
    if len(keys) >= 2:
        keys.append("all")
    return keys


def resolve_source(args: argparse.Namespace, state: dict | None = None) -> str:
    # The concrete starting source: an explicit --source wins; otherwise restore the
    # last-used source from saved state (when it's still available). With no saved
    # preference, auto merges every present source ("all") so you never need --source to
    # see them together; `c` narrows to a single source and that choice is remembered.
    if args.source != "auto":
        return args.source
    saved = (state or {}).get("source")
    if saved in source_cycle(args):  # valid + available (incl. "all" only when >=2)
        return saved
    if "all" in source_cycle(args):  # >=2 sources present -> show them merged
        return "all"
    present = available_sources(args)
    return present[0] if present else "opencode"


def make_store(args: argparse.Namespace, key: str) -> tuple[object, str]:
    # Build the backend for a concrete source key. Returns (store, loading-hint);
    # exits with a clear message when the chosen source isn't present.
    if key == "all":
        subs = [make_store(args, k)[0] for k in available_sources(args)]
        if not subs:
            raise SystemExit("no data sources found (no OpenCode DB, no Claude Code transcripts)")
        if len(subs) == 1:
            return subs[0], "OpenTab: loading…\r"
        return CombinedStore(subs), "OpenTab: loading all sources…\r"
    if key == "claude":
        if not os.path.isdir(args.claude_dir):
            raise SystemExit(f"Claude Code projects directory not found: {args.claude_dir}")
        return ClaudeStore(args.claude_dir, args), "OpenTab: loading Claude Code sessions…\r"
    if key == "codex":
        if not os.path.isdir(args.codex_dir):
            raise SystemExit(f"Codex sessions directory not found: {args.codex_dir}")
        return CodexStore(args.codex_dir, args), "OpenTab: loading Codex sessions…\r"
    if key == "hermes":
        db = getattr(args, "hermes_db", "")
        if not os.path.exists(db):
            raise SystemExit(f"Hermes database not found: {db}")
        return HermesStore(db, args), "OpenTab: loading Hermes sessions…\r"
    if key == "csv":
        path = getattr(args, "csv", "")
        if not os.path.exists(path):
            raise SystemExit(f"CSV file not found: {path}")
        return CsvStore(path, args), "OpenTab: loading API-request CSV…\r"
    if key == "jsonl":
        path = getattr(args, "jsonl", "")
        if not os.path.exists(path):
            raise SystemExit(f"JSONL file not found: {path}")
        return JsonlStore(path, args), "OpenTab: loading API-request JSONL…\r"
    if key == "copilot":
        if not _copilot_otel_available(args):
            raise SystemExit(
                "No GitHub Copilot CLI usage found. Enable its OpenTelemetry file export "
                "(set COPILOT_OTEL_FILE_EXPORTER_PATH before launching Copilot, or point "
                "--copilot-dir at the export) -- see the README."
            )
        return CopilotStore(args.copilot_dir, args), "OpenTab: loading Copilot CLI sessions…\r"
    if key == "pi":
        if not os.path.isdir(args.pi_dir):
            raise SystemExit(f"pi-agent sessions directory not found: {args.pi_dir}")
        return PiStore(args.pi_dir, args), "OpenTab: loading pi-agent sessions…\r"
    if key == "openclaw":
        if not _openclaw_available(getattr(args, "openclaw_dir", "")):
            raise SystemExit(
                "No OpenClaw sessions found. Point --openclaw-dir (or $OPENCLAW_DIR) at an "
                f"OpenClaw home holding agents/*/sessions/*.jsonl (looked in {args.openclaw_dir})."
            )
        return OpenClawStore(args.openclaw_dir, args), "OpenTab: loading OpenClaw sessions…\r"
    if not os.path.exists(args.db):
        raise SystemExit(f"OpenCode database not found: {args.db}")
    return Store(args.db, args), "OpenTab: loading OpenCode database…\r"
