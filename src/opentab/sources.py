"""Backend discovery, selection, and store construction (make_store)."""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from urllib.parse import quote

from opentab import paths, util
from opentab.stores.antigravity import (
    CONVERSATION_DIRS as ANTIGRAVITY_DIRS,
)
from opentab.stores.antigravity import AntigravityStore, default_antigravity_dir
from opentab.stores.bahulam import BahulamStore
from opentab.stores.cached import CachedStore
from opentab.stores.claude import ClaudeStore
from opentab.stores.codex import CodexStore, codex_archive_dirs
from opentab.stores.combined import CombinedStore
from opentab.stores.copilot import CopilotStore
from opentab.stores.csv_source import CsvStore
from opentab.stores.gemini import GeminiStore, default_gemini_dir
from opentab.stores.hermes import HermesStore
from opentab.stores.jsonl_source import JsonlStore
from opentab.stores.omp import OmpStore
from opentab.stores.openclaw import OpenClawStore
from opentab.stores.opencode import REQUIRED_SCHEMA, Store
from opentab.stores.pi import PiStore
from opentab.stores.remote import RemoteStore
from opentab.stores.vscode import VscodeStore
from opentab.stores.zaly import ZalyStore, default_zaly_data_dir


def _default_requests_path(name: str) -> str:
    # Preserve auto-discovery of user data left in the pre-XDG-split config directory.
    data = os.path.join(paths.data_dir(), name)
    legacy = os.path.join(paths.config_dir(), name)
    return legacy if (not os.path.exists(data) and os.path.exists(legacy)) else data


DEFAULT_CSV_PATH = _default_requests_path("requests.csv")
DEFAULT_JSONL_PATH = _default_requests_path("requests.jsonl")


def default_remotes_dir() -> str:
    return os.path.join(paths.cache_dir(), "remotes")


def _default_pi_dir() -> str:
    env = (os.environ.get("PI_AGENT_DIR") or "").split(",")[0].strip()
    return env or os.path.expanduser("~/.pi/agent/sessions")


def _default_omp_dir() -> str:
    # OMP_AGENT_DIR is opentab's override; omp itself defines no session-dir variable.
    env = (os.environ.get("OMP_AGENT_DIR") or "").strip()
    return env or os.path.expanduser("~/.omp/agent/sessions")


def _default_vscode_user_dirs() -> list[str]:
    # Do not auto-scan Windows profiles from WSL: drvfs session reads make startup slow.
    # Users opt in with --vscode-dir; VscodeStore maps the resulting Windows URIs.
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return [
        os.path.join(base, variant, "User") for variant in ("Code", "Code - Insiders", "VSCodium")
    ]


def _vscode_dirs(args: argparse.Namespace) -> list[str]:
    # A namespace lacking this argparse field must not make test stubs scan the real home.
    explicit = getattr(args, "vscode_dir", "")
    if explicit:
        return [explicit]
    return _default_vscode_user_dirs() if explicit is None else []


def _default_openclaw_dir() -> str:
    env = (os.environ.get("OPENCLAW_DIR") or "").split(",")[0].strip()
    return env or os.path.expanduser("~/.openclaw")


def _default_antigravity_dir() -> str:
    # Antigravity keeps its conversations under the same Gemini home, in its own
    # subdirectory, so the two sources share a root but never a flag.
    return default_antigravity_dir()


def _default_gemini_dir() -> str:
    # Store-owned resolution: $GEMINI_CLI_HOME replaces the HOME directory, not the
    # .gemini directory inside it.
    return default_gemini_dir()


def _default_zaly_dir() -> str:
    # Store-owned resolution keeps Zaly's data and auth-state conventions together.
    return default_zaly_data_dir()


def _default_bahulam_dir() -> str:
    """Return the default Bahulam Code projects directory.

    Resolution order (first match wins, empty values skipped):
      1. ``$BAHULAM_PROJECTS_DIR`` — explicit override
      2. ``$BAHULAM_HOME/projects`` — Bahulam home relocated
      3. ``$KEPLER_HOME/projects`` — legacy alias from the Kepler-branded builds
      4. ``~/.bahulam/projects``   — Bahulam default
      5. ``~/.kepler/projects``    — legacy default, only if it exists on disk
         (skipping this last-resort check would silently point at a phantom
         path when neither install layout is present)
    """
    override = (os.environ.get("BAHULAM_PROJECTS_DIR") or "").strip()
    if override:
        return override
    for env_name in ("BAHULAM_HOME", "KEPLER_HOME"):
        home = (os.environ.get(env_name) or "").strip()
        if home:
            return os.path.join(os.path.expanduser(home), "projects")
    bahulam_default = os.path.expanduser("~/.bahulam/projects")
    kepler_legacy = os.path.expanduser("~/.kepler/projects")
    if not os.path.isdir(bahulam_default) and os.path.isdir(kepler_legacy):
        return kepler_legacy
    return bahulam_default


_PATH_SLOT = {
    "csv": "csv",
    "jsonl": "jsonl",
    "opencode": "db",
    "claude": "claude_dir",
    "codex": "codex_dir",
    "hermes": "hermes_db",
    "copilot": "copilot_dir",
    "vscode": "vscode_dir",
    "pi": "pi_dir",
    "omp": "omp_dir",
    "openclaw": "openclaw_dir",
    "zaly": "zaly_dir",
    "gemini": "gemini_dir",
    "antigravity": "antigravity_dir",
    "bahulam": "bahulam_dir",
}


def _infer_source_from_path(path: str) -> str | None:
    # Directories are ambiguous across transcript backends and require an explicit source.
    low = path.lower()
    if low.endswith(".csv"):
        return "csv"
    if low.endswith((".jsonl", ".ndjson")):
        return "jsonl"
    if low.endswith((".db", ".sqlite", ".sqlite3")):
        return "opencode"
    return None


def _route_path_arg(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # An explicit source owns the positional path; otherwise infer file-backed sources.
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
                args.source = target
        else:
            target = args.source
        slot = _PATH_SLOT.get(target)
        if slot is None:
            parser.error(f"--source {target} does not take a path argument")
        setattr(args, slot, path)
        if target == "csv":
            csv_explicit = True
        elif target == "jsonl":
            jsonl_explicit = True
    if csv_explicit and args.source == "auto":
        args.source = "csv"
    if jsonl_explicit and args.source == "auto":
        args.source = "jsonl"
    if args.csv is None:
        args.csv = DEFAULT_CSV_PATH
    if args.jsonl is None:
        args.jsonl = DEFAULT_JSONL_PATH


def _jsonl_dir_available(directory: str) -> bool:
    # Stop at the first hit instead of enumerating large or remotely scanned trees.
    if not os.path.isdir(directory):
        return False
    return (
        next(glob.iglob(os.path.join(directory, "**", "*.jsonl"), recursive=True), None) is not None
    )


def _codex_available(sessions_dir: str) -> bool:
    # Archiving every live thread leaves sessions/ empty (or absent) while the rollouts
    # sit in the sibling archive the store also reads, so ask about the same roots.
    return any(
        _jsonl_dir_available(directory)
        for directory in [sessions_dir, *codex_archive_dirs(sessions_dir)]
    )


def opencode_db_verdict(db: str) -> tuple[str, str]:
    """Classify a database as valid, missing, unreadable, or foreign.

    Probe only REQUIRED_SCHEMA columns; optional schema remains adaptive. Distinguishing
    unreadable from foreign keeps diagnostics honest while ``all`` skips unusable stores.
    """
    if not db:
        return "missing", "No OpenCode database configured (--db)."
    if not os.path.exists(db):
        return "missing", f"OpenCode database not found: {db}"
    try:
        conn = sqlite3.connect("file:" + quote(os.path.abspath(db)) + "?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        return "unreadable", f"OpenCode database could not be opened: {db} ({exc})"
    except sqlite3.Error as exc:
        return "foreign", f"Not an OpenCode database: {db} ({exc})"
    missing = []
    try:
        for table, columns in REQUIRED_SCHEMA.items():
            # Empty table_info also detects a missing table; names are trusted constants.
            have = {row[1] for row in conn.execute(f"pragma table_info({table})")}
            missing += [f"{table}.{c}" for c in columns if c not in have]
    except sqlite3.OperationalError as exc:
        return "unreadable", f"OpenCode database could not be read: {db} ({exc})"
    except sqlite3.Error as exc:
        return "foreign", f"Not an OpenCode database: {db} ({exc})"
    finally:
        conn.close()
    if missing:
        lacks = ", ".join(missing[:4]) + (" …" if len(missing) > 4 else "")
        return (
            "foreign",
            f"Not an OpenCode database (no {lacks}): {db}. "
            "Point --db at OpenCode's own database, or name the right backend with "
            "--source (--hermes-db for Hermes, --csv/--jsonl for a request log).",
        )
    return "", ""


def _openclaw_available(root_dir: str) -> bool:
    if not root_dir or not os.path.isdir(root_dir):
        return False
    return (
        next(glob.iglob(os.path.join(root_dir, "agents", "*", "sessions", "*.jsonl")), None)
        is not None
    )


def _zaly_available(root_dir: str) -> bool:
    if not root_dir or not os.path.isdir(root_dir):
        return False
    return (
        next(glob.iglob(os.path.join(root_dir, "sessions", "*", "*", "session.jsonl")), None)
        is not None
    )


def _gemini_available(root_dir: str) -> bool:
    if not root_dir or not os.path.isdir(root_dir):
        return False
    # Both layouts, and the nested subagent transcripts, live under tmp/*/chats. The
    # suffix rule is the store's own (`GeminiStore._is_transcript`): a tree holding only
    # the `.unreadable-<ms>` copies a rewrite leaves behind would otherwise advertise the
    # source and then produce no sessions, and `--harness gemini` would pass validation
    # only to open an empty browser.
    for pattern in ("*.json*", os.path.join("*", "*.json*")):
        for path in glob.iglob(os.path.join(root_dir, "tmp", "*", "chats", pattern)):
            if GeminiStore._is_transcript(path):
                return True
    return False


def _antigravity_available(root_dir: str) -> bool:
    if not root_dir or not os.path.isdir(root_dir):
        return False
    # A stray or truncated *.db must not advertise the source: `--harness antigravity`
    # would pass validation and then open an empty browser. Opening is the only honest
    # test, and it stops at the first real conversation.
    for name in ANTIGRAVITY_DIRS:
        pattern = os.path.join(root_dir, name, "conversations", "*.db")
        for path in glob.iglob(pattern):
            if AntigravityStore.is_conversation(path):
                return True
    return False


def _bahulam_available(root_dir: str) -> bool:
    """Return ``True`` if *root_dir* contains at least one ``.jsonl`` file."""
    return _jsonl_dir_available(root_dir)


def _copilot_otel_available(args: argparse.Namespace) -> bool:
    if _jsonl_dir_available(getattr(args, "copilot_dir", "")):
        return True
    extra = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH") or ""
    return bool(extra) and os.path.isfile(extra)


def _vscode_available(args: argparse.Namespace) -> bool:
    # Empty chat-panel files are not usage; require token markers and stop at the first hit.
    for user_dir in _vscode_dirs(args):
        patterns = (
            os.path.join(user_dir, "workspaceStorage", "*", "chatSessions", "*.json*"),
            os.path.join(user_dir, "globalStorage", "emptyWindowChatSessions", "*.json*"),
            os.path.join(user_dir, "*.json*"),  # pointed straight at a chatSessions dir
        )
        for pattern in patterns:
            for path in glob.iglob(pattern):
                if not path.endswith((".json", ".jsonl")):
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            if '"promptTokens"' in line or '"completionTokens"' in line:
                                return True
                except OSError:
                    continue
    return False


SOURCE_LABELS = {
    "opencode": "OpenCode",
    "claude": "Claude Code",
    "codex": "Codex",
    "hermes": "Hermes",
    "csv": "CSV",
    "jsonl": "JSONL",
    "copilot": "Copilot",
    "vscode": "VS Code",
    "pi": "Pi",
    "omp": "Omp",
    "openclaw": "OpenClaw",
    "zaly": "Zaly",
    "gemini": "Gemini",
    "antigravity": "Antigravity",
    "bahulam": "Bahulam Code",
    "all": "all",
}

RESUME_COMMANDS = {
    "OpenCode": "opencode --session",
    "Claude Code": "claude --resume",
    "Codex": "codex resume",
    "Hermes": "hermes --resume",
    "Copilot": "copilot --resume",
    "Pi": "pi --session",
    "Omp": "omp --resume",
    "Zaly": "zaly --session",
    "Gemini": "gemini --resume",
    "Antigravity": "antigravity",
    "Bahulam Code": "bahulam resume",
}


def _detect_fingerprint(args: argparse.Namespace) -> tuple:
    # Fingerprint every detection input so namespace path changes invalidate the memo.
    return tuple(
        getattr(args, name, "")
        for name in (
            "db",
            "claude_dir",
            "codex_dir",
            "hermes_db",
            "csv",
            "jsonl",
            "copilot_dir",
            "vscode_dir",
            "pi_dir",
            "omp_dir",
            "openclaw_dir",
            "zaly_dir",
            "gemini_dir",
            "antigravity_dir",
            "bahulam_dir",
        )
    ) + (os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH", ""),)


def available_sources(args: argparse.Namespace) -> list[str]:
    # Memoize repeated tree probes, especially costly on Windows/WSL, by all path inputs.
    fp = _detect_fingerprint(args)
    cached = getattr(args, "_available_sources", None)
    if cached is not None and cached[0] == fp:
        return list(cached[1])
    keys = []
    # Never merge an unreadable or foreign database into ``all``.
    if not opencode_db_verdict(args.db)[0]:
        keys.append("opencode")
    if _jsonl_dir_available(args.claude_dir):
        keys.append("claude")
    if _codex_available(getattr(args, "codex_dir", "")):
        keys.append("codex")
    if os.path.exists(getattr(args, "hermes_db", "")):
        keys.append("hermes")
    if os.path.exists(getattr(args, "csv", "")):
        keys.append("csv")
    if os.path.exists(getattr(args, "jsonl", "")):
        keys.append("jsonl")
    if _copilot_otel_available(args):
        keys.append("copilot")
    if _vscode_available(args):
        keys.append("vscode")
    if _jsonl_dir_available(getattr(args, "pi_dir", "")):
        keys.append("pi")
    if _jsonl_dir_available(getattr(args, "omp_dir", "")):
        keys.append("omp")
    if _openclaw_available(getattr(args, "openclaw_dir", "")):
        keys.append("openclaw")
    if _zaly_available(getattr(args, "zaly_dir", "")):
        keys.append("zaly")
    if _gemini_available(getattr(args, "gemini_dir", "")):
        keys.append("gemini")
    if _antigravity_available(getattr(args, "antigravity_dir", "")):
        keys.append("antigravity")
    if _bahulam_available(getattr(args, "bahulam_dir", "")):
        keys.append("bahulam")
    args._available_sources = (fp, keys)
    return list(keys)


def source_cycle(args: argparse.Namespace) -> list[str]:
    keys = available_sources(args)
    if len(keys) >= 2:
        keys.append("all")
    return keys


def resolve_source(args: argparse.Namespace, state: dict | None = None) -> str:
    # Explicit selection wins, then available saved state, then an automatic merged view.
    if args.source != "auto":
        return args.source
    saved = (state or {}).get("source")
    if saved in source_cycle(args):
        return saved
    if "all" in source_cycle(args):
        return "all"
    present = available_sources(args)
    return present[0] if present else "opencode"


def _wrap_cache(store, key: str, args: argparse.Namespace):
    # Cache leaves independently; never persist per-process demo transforms.
    if key == "all" or getattr(store, "combined", False):
        return store
    if getattr(args, "demo", False) or getattr(args, "no_cache", False):
        return store
    if not callable(getattr(store, "cache_inputs", None)):
        return store
    root = getattr(args, _PATH_SLOT.get(key, ""), "") or ""
    return CachedStore(store, f"{key}|{root}", args)


def make_store(args: argparse.Namespace, key: str) -> tuple[object, str]:
    store, hint = _build_store(args, key)
    return _wrap_cache(store, key, args), hint


def _build_store(args: argparse.Namespace, key: str) -> tuple[object, str]:
    if key == "all":
        subs = [make_store(args, k)[0] for k in available_sources(args)]
        if not subs:
            raise SystemExit("no data sources found (no OpenCode DB, no Claude Code transcripts)")
        if len(subs) == 1:
            return subs[0], "OpenTab: loading…\r"
        return CombinedStore(subs), "OpenTab: loading all sources…\r"
    if key == "remote":
        from opentab.stores.remote import MachineTaggedStore

        remotes = getattr(args, "remotes", None) or default_remotes_dir()
        # Fleet view combines live local drill-in with pulled summaries under one hostname.
        hostname = util.local_machine_name()
        local_subs = [make_store(args, k)[0] for k in available_sources(args)]
        # Live rows win duplicate ids so totals and drill-in do not use stale summaries.
        local_ids = {w.id for sub in local_subs for w in sub.workflows()}
        remote = RemoteStore(remotes, args, exclude_ids=local_ids)
        subs = [MachineTaggedStore(sub, hostname) for sub in local_subs]
        if remote.machines:
            subs.append(remote)
        if not subs:
            raise SystemExit(
                f"No machine summaries found in {remotes} and no local data on this "
                "machine. On each other machine run `opentab --pull HOST` to fetch its "
                "spend over SSH (or `opentab --export -` and gather the files there)."
            )
        if len(subs) == 1:
            return subs[0], "OpenTab: loading fleet…\r"
        return CombinedStore(subs), "OpenTab: loading fleet (this machine + pulled)…\r"
    if key == "claude":
        if not os.path.isdir(args.claude_dir):
            raise SystemExit(f"Claude Code projects directory not found: {args.claude_dir}")
        return ClaudeStore(args.claude_dir, args), "OpenTab: loading Claude Code sessions…\r"
    if key == "codex":
        # An all-archived install has no sessions/ at all; the store still reads it.
        if not os.path.isdir(args.codex_dir) and not codex_archive_dirs(args.codex_dir):
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
                "--copilot-dir at the export) -- see docs/sources.md."
            )
        return CopilotStore(args.copilot_dir, args), "OpenTab: loading Copilot CLI sessions…\r"
    if key == "vscode":
        if not _vscode_available(args):
            raise SystemExit(
                "No VS Code Copilot Chat usage found. Sessions with recorded tokens live "
                "under <User>/workspaceStorage/*/chatSessions; point --vscode-dir at a VS "
                "Code User directory (or a chatSessions directory) if yours is elsewhere."
            )
        return (
            VscodeStore(_vscode_dirs(args), args),
            "OpenTab: loading VS Code Copilot Chat sessions…\r",
        )
    if key == "pi":
        if not os.path.isdir(args.pi_dir):
            raise SystemExit(f"pi-agent sessions directory not found: {args.pi_dir}")
        return PiStore(args.pi_dir, args), "OpenTab: loading pi-agent sessions…\r"
    if key == "omp":
        if not os.path.isdir(args.omp_dir):
            raise SystemExit(f"omp sessions directory not found: {args.omp_dir}")
        return OmpStore(args.omp_dir, args), "OpenTab: loading omp sessions…\r"
    if key == "openclaw":
        if not _openclaw_available(getattr(args, "openclaw_dir", "")):
            raise SystemExit(
                "No OpenClaw sessions found. Point --openclaw-dir (or $OPENCLAW_DIR) at an "
                f"OpenClaw home holding agents/*/sessions/*.jsonl (looked in {args.openclaw_dir})."
            )
        return OpenClawStore(args.openclaw_dir, args), "OpenTab: loading OpenClaw sessions…\r"
    if key == "zaly":
        if not _zaly_available(getattr(args, "zaly_dir", "")):
            raise SystemExit(
                "No Zaly sessions found. Point --zaly-dir (or $ZALY_DATA) at a Zaly data "
                "directory holding sessions/*/*/session.jsonl "
                f"(looked in {getattr(args, 'zaly_dir', '')})."
            )
        return ZalyStore(args.zaly_dir, args), "OpenTab: loading Zaly sessions…\r"
    if key == "gemini":
        if not _gemini_available(getattr(args, "gemini_dir", "")):
            raise SystemExit(
                "No Gemini CLI sessions found. Point --gemini-dir at a .gemini directory "
                "holding tmp/*/chats/ "
                f"(looked in {getattr(args, 'gemini_dir', '')})."
            )
        return GeminiStore(args.gemini_dir, args), "OpenTab: loading Gemini sessions…\r"
    if key == "antigravity":
        if not _antigravity_available(getattr(args, "antigravity_dir", "")):
            raise SystemExit(
                "No Antigravity conversations found. Point --antigravity-dir at a .gemini "
                "directory holding antigravity/conversations/*.db "
                f"(looked in {getattr(args, 'antigravity_dir', '')})."
            )
        return (
            AntigravityStore(args.antigravity_dir, args),
            "OpenTab: loading Antigravity conversations…\r",
        )
    if key == "bahulam":
        if not _bahulam_available(getattr(args, "bahulam_dir", "")):
            raise SystemExit(
                "No Bahulam Code sessions found. Point --bahulam-dir (or "
                "$BAHULAM_PROJECTS_DIR / $BAHULAM_HOME / $KEPLER_HOME) at "
                "~/.bahulam/projects (or ~/.kepler/projects if you migrated from "
                f"Kepler) (looked in {getattr(args, 'bahulam_dir', '')})."
            )
        return BahulamStore(args.bahulam_dir, args), "OpenTab: loading Bahulam Code sessions…\r"
    kind, problem = opencode_db_verdict(args.db)
    if kind:
        raise SystemExit(problem)
    return Store(args.db, args), "OpenTab: loading OpenCode database…\r"
