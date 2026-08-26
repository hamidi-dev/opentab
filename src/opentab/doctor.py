"""Environment diagnostics safe to paste into a bug report.

Three constraints are load-bearing:

* Report only: create, migrate, warm, fetch, or repair nothing.
* Never parse transcripts: inspect metadata and small config files only, redact paths,
  and name pulled machines only under ``--full``.
* Borrow application verdicts for source presence, colour mode, and note readability so
  the report cannot disagree with the behavior it diagnoses.

CLI imports this module lazily to keep it off one-shot cost commands' import path.
"""
from __future__ import annotations

import argparse
import glob
import json
import locale
import os
import platform
import re
import shutil
import sys
import sysconfig
from datetime import datetime, timezone
from typing import NamedTuple

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None

from opentab import __version__, notes, paths, pricing, sources, state
from opentab.formatting import human_bytes, relative_age
from opentab.stores import cached
from opentab.tui import bindings
from opentab.util import (
    env_flag,
    init_color_allowed,
    palette_writes_ignored,
    terminal_multiplexers,
    unicode_screen,
)

# Keep report construction separate from rendering so tests assert verdicts, not spacing.

OK = "ok"
WARN = "warn"
BAD = "bad"
INFO = "info"

_MARKS = {
    True: {OK: "✓", WARN: "⚠", BAD: "✗", INFO: "·"},
    False: {OK: "+", WARN: "!", BAD: "x", INFO: "-"},
}


class Row(NamedTuple):
    status: str
    label: str
    detail: str
    hint: str = ""


# Redact personal Windows profiles from native and WSL paths, but retain shared profiles.
_WIN_USER_RE = re.compile(r"((?:/mnt/[a-z]|[a-z]:)[/\\][Uu]sers[/\\])([^/\\]+)", re.IGNORECASE)
_WIN_SHARED_PROFILES = {"public", "default", "default user", "all users"}


def _win_user(match: re.Match) -> str:
    name = match.group(2)
    return match.group(0) if name.lower() in _WIN_SHARED_PROFILES else match.group(1) + "<user>"


def _tilde(text: str, full: bool = False) -> str:
    """Redact every home/profile occurrence unless ``full`` is requested.

    Values may contain comma-separated paths, and WSL's Windows profile is outside its
    ``$HOME``. Match path boundaries to avoid folding a sibling such as ``moextra``.
    """
    if not text or full:
        return text or ""
    home = os.path.expanduser("~")
    if home and home not in (os.sep, "/"):
        # Windows paths vary by separator and case. Never fold a root home such as `/`.
        pattern = r"[/\\]".join(re.escape(part) for part in re.split(r"[/\\]", home))
        flags = re.IGNORECASE if os.name == "nt" else 0
        text = re.sub(pattern + r"(?![\w.-])", "~", text, flags=flags)
    return _WIN_USER_RE.sub(_win_user, text)


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _age(ts: float, now: datetime | None = None) -> str:
    # relative_age interprets naive stamps as UTC, so provide an explicit UTC value.
    if not ts:
        return ""
    return relative_age(datetime.fromtimestamp(ts, timezone.utc).isoformat(), now)


def _n(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _pulled_machines(remotes: str) -> tuple[int, int]:
    """Count summaries RemoteStore would accept, not merely ``*.json`` files.

    Parsing a measured 4-machine/3.5MB fleet costs about 15ms and avoids declaring
    truncated or placeholder JSON usable.
    """
    found = sorted(glob.glob(os.path.join(remotes, "*.json")))
    kept = 0
    for path in found:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        kept += isinstance(data, dict) and isinstance(data.get("workflows"), list)
    return kept, len(found)


def _blocked(path: str) -> bool:
    # exists() also returns false when a parent denies lookup. Never report an authored
    # notes file as absent when it may only be inaccessible.
    parent = os.path.dirname(path)
    return bool(parent) and os.path.isdir(parent) and not os.access(parent, os.X_OK | os.R_OK)


def _legacy_names() -> list[str]:
    # Doctor reports pre-XDG artifacts without triggering their next-launch migration.
    cfg = paths.config_dir()
    names = ("state.json", "notes.json", *paths._LEGACY_CACHE_NAMES)
    return [n for n in names if os.path.exists(os.path.join(cfg, n))]


def _dir_stats(root: str, pattern: str, keep=None) -> tuple[int, float, int]:
    # Metadata only; `keep` applies backend acceptance rules a glob cannot express.
    hits = glob.glob(os.path.join(root, pattern), recursive=True)
    if keep is not None:
        hits = [p for p in hits if keep(os.path.basename(p))]
    return len(hits), max((_mtime(p) for p in hits), default=0.0), sum(_size(p) for p in hits)


def _name_filter(key: str):
    """Return a backend file filter where the diagnostic glob is intentionally broad.

    OpenClaw includes reset/deleted archives but excludes locks, all matched by
    ``*.jsonl*``.
    """
    if key == "openclaw":
        from opentab.stores.openclaw import OpenClawStore

        return OpenClawStore._is_session_file
    return None


def _shell() -> tuple[str, str]:
    """Return the likely shell name and assignment syntax.

    ``$SHELL`` names the login shell, not necessarily the running one. PowerShell exports
    cross-platform markers and matters most because POSIX syntax is invalid there.
    """
    # PowerShell markers identify the running shell even on Linux.
    if os.environ.get("POWERSHELL_DISTRIBUTION_CHANNEL"):
        return "PowerShell", "powershell"
    if os.environ.get("PSModulePath"):
        # On Windows cmd may inherit this machine-wide variable, so label it assumed.
        return ("PowerShell (assumed)" if os.name == "nt" else "PowerShell"), "powershell"
    name = os.path.basename(os.environ.get("SHELL") or "")
    if name:
        if name.lower() in ("pwsh", "powershell", "pwsh.exe", "powershell.exe"):
            return "PowerShell", "powershell"
        return name, "fish" if name == "fish" else "posix"
    # PowerShell always sets PSModulePath; without a marker, native Windows implies cmd.
    return ("cmd", "cmd") if os.name == "nt" else ("unknown", "posix")


def _export(var: str, value: str, kind: str) -> str:
    # PowerShell and cmd do not expand `~` inside assignments.
    if kind == "powershell":
        return f'$env:{var} = "{value.replace("~/", "$HOME/", 1)}"'
    if kind == "cmd":
        return f"set {var}={value.replace('~/', '%USERPROFILE%/', 1)}"
    if kind == "fish":
        return f"set -gx {var} {value}"
    return f"export {var}={value}"


# How an install is recognised, by a path fragment each installer owns. Checked in this
# order because pipx/Homebrew paths also match less-specific layouts.
_INSTALL_MARKERS = (
    (os.path.join("pipx", "venvs"), "pipx"),
    (os.path.join("uv", "tools"), "uv tool"),
    (os.path.join("Cellar", "opentab"), "Homebrew"),
    ("/opt/homebrew/", "Homebrew"),
    ("/home/linuxbrew/", "Homebrew"),
    ("dist-packages", "system package manager"),
)


def _pkg_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _install_method(pkg_dir: str) -> str:
    """Identify the installer so upgrade advice and PATH mismatches are actionable."""
    lower = pkg_dir.replace("\\", "/").lower()
    for fragment, label in _INSTALL_MARKERS:
        if fragment.replace("\\", "/").lower() in lower:
            return label
    parent = os.path.dirname(pkg_dir)
    if os.path.basename(parent) == "src" and os.path.exists(
        os.path.join(os.path.dirname(parent), "pyproject.toml")
    ):
        return "source checkout"
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return "virtualenv"
    if "site-packages" in lower:
        return "pip"
    return "unknown layout"


def _script_dirs() -> set[str]:
    """Return default and user console-script directories from sysconfig.

    ``pip install --user`` scripts are outside ``sys.prefix``; prefix comparison would
    falsely diagnose that ordinary install as two environments.
    """
    dirs = set()
    for scheme in (None, "posix_user", "nt_user"):
        try:
            path = (
                sysconfig.get_path("scripts")
                if scheme is None
                else sysconfig.get_path("scripts", scheme)
            )
        except (KeyError, ValueError):
            continue
        if path:
            dirs.add(os.path.realpath(path))
    return dirs


def _path_row(pkg_dir: str, full: bool) -> Row:
    """Report whether PATH resolves into this interpreter's script environment.

    Console scripts are wrappers, so compare their environment directories rather than
    the wrapper file with the imported package.
    """
    found = shutil.which("opentab")
    if not found:
        return Row(
            INFO,
            "on PATH",
            "no `opentab` on PATH (this run came from `python -m opentab` or a full path)",
        )
    resolved = os.path.realpath(found)
    if os.path.dirname(resolved) in _script_dirs():
        return Row(OK, "on PATH", _tilde(found, full))
    detail = f"{_tilde(found, full)} — a DIFFERENT install from the one running ({_tilde(pkg_dir, full)})"
    if _install_method(pkg_dir) == "source checkout":
        # PATH divergence is expected when intentionally running a source checkout.
        return Row(INFO, "on PATH", detail)
    return Row(
        WARN,
        "on PATH",
        detail,
        "changes to one won't show up in the other; `which -a opentab` lists every copy",
    )


# `pattern` is accepted data; `loose` detects a present tool at the wrong directory level.
_FILE, _TREE, _COPILOT, _VSCODE = "file", "tree", "copilot", "vscode"

_HARNESSES = (
    ("opencode", "db", _FILE, "", "", "OpenCode keeps its database at ~/.local/share/opencode/opencode.db; pass --db if yours is elsewhere"),
    ("claude", "claude_dir", _TREE, "**/*.jsonl", "", "Claude Code writes transcripts under ~/.claude/projects; pass --claude-dir if yours is elsewhere"),
    ("codex", "codex_dir", _TREE, "**/*.jsonl", "", "Codex writes rollouts under ~/.codex/sessions; pass --codex-dir if yours is elsewhere"),
    ("hermes", "hermes_db", _FILE, "", "", "pass --hermes-db to point at a Hermes state.db"),
    ("csv", "csv", _FILE, "", "", "point --csv at a CSV of logged API requests"),
    ("jsonl", "jsonl", _FILE, "", "", "point --jsonl at an NDJSON of logged API requests"),
    ("copilot", "copilot_dir", _COPILOT, "**/*.jsonl", "", "the OTEL export is opt-in and the CLI records tokens nowhere else"),
    ("vscode", "vscode_dir", _VSCODE, "", "", "point --vscode-dir at a VS Code User directory holding Copilot Chat sessions"),
    ("pi", "pi_dir", _TREE, "**/*.jsonl", "", "pi-agent writes sessions under ~/.pi/agent/sessions; it honors $PI_AGENT_DIR, or pass --pi-dir"),
    ("omp", "omp_dir", _TREE, "**/*.jsonl", "", "omp writes sessions under ~/.omp/agent/sessions; set $OMP_AGENT_DIR, or pass --omp-dir"),
    ("openclaw", "openclaw_dir", _TREE, "agents/*/sessions/*.jsonl*", "**/*.jsonl", "--openclaw-dir/$OPENCLAW_DIR want the gateway HOME holding agents/, not a sessions directory"),
    ("zaly", "zaly_dir", _TREE, "sessions/*/*/session.jsonl", "**/session.jsonl", "--zaly-dir/$ZALY_DATA want the DATA directory holding sessions/, not sessions/ itself"),
)  # fmt: skip


def _vscode_session_files(user_dir: str) -> list[str]:
    """List only chat-session JSON, not unrelated ``*.json*`` files.

    Root-level files count only when the supplied directory is itself ``chatSessions``;
    otherwise settings and backup files inflate the diagnostic count.
    """
    patterns = [
        os.path.join(user_dir, "workspaceStorage", "*", "chatSessions", "*.json*"),
        os.path.join(user_dir, "globalStorage", "emptyWindowChatSessions", "*.json*"),
    ]
    if os.path.basename(user_dir.rstrip("/\\")).lower() == "chatsessions":
        patterns.append(os.path.join(user_dir, "*.json*"))
    return [
        path
        for pattern in patterns
        for path in glob.glob(pattern)
        if path.endswith((".json", ".jsonl"))
    ]


def _vscode_detail(args: argparse.Namespace, present: bool, full: bool) -> tuple[str, str, str]:
    # VS Code may be absent, unused, or have tokenless sessions; distinguish all three.
    dirs = [d for d in sources._vscode_dirs(args) if os.path.isdir(d)]
    if not dirs:
        return INFO, "no VS Code / Insiders / VSCodium user directory found", ""
    files = sum(len(_vscode_session_files(d)) for d in dirs)
    where = ", ".join(_tilde(d, full) for d in dirs)
    if present:
        return OK, f"{where} · {_n(files, 'chat session file')}", ""
    if not files:
        return INFO, f"{where} · no chat sessions", ""
    hint = "opening the chat panel alone writes empty session files; only a chat that ran records tokens"
    if os.environ.get("WSL_DISTRO_NAME"):
        # WSL intentionally avoids the slow Windows-mounted store unless explicitly set.
        hint = "from WSL the Windows-side store is not scanned by default: --vscode-dir '/mnt/c/Users/<you>/AppData/Roaming/Code/User'"
    return WARN, f"{where} · {_n(files, 'chat session')}, none with recorded tokens", hint


def _copilot_detail(
    args: argparse.Namespace, spec: tuple, present: bool, full: bool
) -> tuple[str, str, str]:
    root = getattr(args, "copilot_dir", "") or ""
    env = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH") or ""
    files, newest, total = _dir_stats(root, spec[3]) if os.path.isdir(root) else (0, 0.0, 0)
    if present:
        parts = (
            [f"{_n(files, 'export file')} · {human_bytes(total)} · newest {_age(newest)}"]
            if files
            else []
        )
        if env and os.path.isfile(env):
            parts.append(f"$COPILOT_OTEL_FILE_EXPORTER_PATH → {_tilde(env, full)}")
        return OK, f"{_tilde(root, full)} · " + " · ".join(parts), ""
    if env:
        # Export configuration affects only Copilot sessions started after it was set.
        return (
            WARN,
            f"$COPILOT_OTEL_FILE_EXPORTER_PATH → {_tilde(env, full)} · nothing written there yet",
            "only a copilot session started AFTER the variable was set writes it — check the shell that launches copilot exports it",
        )
    # Detect installation from Copilot's home, not an arbitrary --copilot-dir parent.
    if os.path.isdir(os.path.expanduser("~/.copilot")):
        target = _export(
            "COPILOT_OTEL_FILE_EXPORTER_PATH", "~/.copilot/otel/usage.jsonl", _shell()[1]
        )
        return (
            WARN,
            f"{_tilde(root, full)} · Copilot CLI is installed, its OTEL export is off",
            f"{spec[5]}: `{target}` before launching copilot",
        )
    return INFO, f"{_tilde(root, full)} · not found", ""


def _harness_row(args: argparse.Namespace, spec: tuple, present: bool, full: bool) -> Row:
    key, attr, kind, pattern, loose, hint = spec
    label = sources.SOURCE_LABELS.get(key, key)
    if kind == _VSCODE:
        status, detail, extra = _vscode_detail(args, present, full)
        return Row(status, label, detail, extra or (hint if status == WARN else ""))
    if kind == _COPILOT:
        status, detail, extra = _copilot_detail(args, spec, present, full)
        return Row(status, label, detail, extra)
    path = getattr(args, attr, "") or ""
    shown = _tilde(path, full)
    if kind == _FILE:
        if present:
            return Row(OK, label, f"{shown} · {human_bytes(_size(path))} · {_age(_mtime(path))}")
        if key == "opencode" and os.path.exists(path):
            # Borrow the source verdict to distinguish unreadable from wrong-schema DBs;
            # both are absent to the app but require opposite fixes.
            kind = sources.opencode_db_verdict(path)[0]
            if kind == "unreadable":
                return Row(
                    BAD,
                    label,
                    f"{shown} · exists, but cannot be read — locked or permission denied",
                    "check the permissions on that file (opentab opens it read-only)",
                )
            return Row(WARN, label, f"{shown} · exists, but is not an OpenCode database", hint)
        return Row(INFO, label, f"{shown} · not found", "")
    keep = _name_filter(key)
    if present:
        files, newest, total = _dir_stats(path, pattern, keep)
        return Row(
            OK,
            label,
            f"{shown} · {_n(files, 'file')} · {human_bytes(total)} · newest {_age(newest)}",
        )
    if not os.path.isdir(path):
        return Row(INFO, label, f"{shown} · not found", "")
    stray = _dir_stats(path, loose)[0] if loose else 0
    if stray:
        return Row(WARN, label, f"{shown} · {_n(stray, 'file')}, none matching {pattern}", hint)
    return Row(INFO, label, f"{shown} · no sessions yet", "")


def _selection_row(args: argparse.Namespace, present: list[str], saved: dict) -> Row:
    # Surface saved or explicit selection that silently hides present harnesses.
    labels = [sources.SOURCE_LABELS.get(k, k) for k in present]
    pick = getattr(args, "source", "auto")
    if (
        pick == "remote"
        or getattr(args, "remote", False)
        or getattr(args, "pull", None) is not None
    ):
        # A remote fleet can work without any local harness.
        remotes = getattr(args, "remotes", None) or sources.default_remotes_dir()
        pulled, files = _pulled_machines(remotes)
        if pulled or present:
            local = f", plus this machine ({', '.join(labels)})" if present else ""
            broken = files - pulled
            return Row(
                WARN if broken else OK,
                "selection",
                f"--harness remote → {_n(pulled, 'pulled machine')}{local}"
                + (f" · {_n(broken, 'summary')} in that directory unreadable" if broken else ""),
                "re-run `opentab pull HOST` for those machines; the fleet view skips them silently"
                if broken
                else "",
            )
        return Row(
            BAD,
            "selection",
            "--harness remote, but nothing has been pulled and this machine has no harness either",
            "`opentab pull HOST` fetches a machine's summary over SSH",
        )
    if pick != "auto" and pick != "all":
        # A pinned absent harness is BAD because the equivalent TUI invocation exits.
        label = sources.SOURCE_LABELS.get(pick, pick)
        if pick in present:
            return Row(INFO, "selection", f"--harness {pick} pins {label}")
        return Row(
            BAD,
            "selection",
            f"--harness {pick} pins {label}, which isn't present — the TUI would exit here",
            f"drop the flag to browse what is present ({', '.join(labels)})"
            if present
            else "and no other harness is present either",
        )
    if not present:
        return Row(
            BAD,
            "selection",
            "no harness found — there is nothing to browse",
            "install a supported tool, or point opentab at a request log: `opentab requests.csv`",
        )
    resolved = sources.resolve_source(args, saved)
    if resolved == "all":
        return Row(OK, "selection", f"auto → all {len(present)} merged ({', '.join(labels)})")
    pinned = sources.SOURCE_LABELS.get(resolved, resolved)
    if len(present) > 1:
        return Row(
            WARN,
            "selection",
            f"state.json remembers {pinned} — the other {len(present) - 1} present harness(es) are hidden",
            "press H in the TUI to switch, or run `opentab --harness all`",
        )
    return Row(OK, "selection", f"auto → {pinned} (the only harness present)")


def harness_rows(args: argparse.Namespace, full: bool = False) -> list[Row]:
    """Explain ``available_sources`` verbatim, ordered actionable-first."""
    present = sources.available_sources(args)
    rows = [_harness_row(args, spec, spec[0] in present, full) for spec in _HARNESSES]
    rank = {OK: 0, BAD: 1, WARN: 2, INFO: 3}
    rows.sort(key=lambda r: rank.get(r.status, 9))
    # Reading selection state must not migrate a pre-XDG state file.
    saved = (
        {}
        if getattr(args, "no_state", False)
        else state.load_state(state.state_path(migrate=False))
    )
    return rows + [_selection_row(args, present, saved)]


def _terminfo() -> tuple[int | None, int | None, bool | None]:
    """Return terminfo's colour claims and whether lookup succeeded.

    Missing curses and unusable terminfo are distinct: the latter also prevents initscr.
    Call once because ncurses caches setupterm; tests must monkeypatch failures rather
    than change TERM after an earlier successful call.
    """
    if curses is None:
        return None, None, None
    try:
        # Captured stdout may lack fileno; that must not masquerade as broken terminfo.
        try:
            fd = sys.stdout.fileno()
        except (AttributeError, OSError, ValueError):
            fd = 1
        curses.setupterm(fd=fd)
        return curses.tigetnum("colors"), curses.tigetflag("ccc"), True
    except (curses.error, OSError, ValueError, AttributeError):
        return None, None, False


def _multiplexer_row(colors: int | None, muxes: list[str]) -> Row:
    """Report terminal intermediaries without leaking marker values.

    Multiplexers re-emit cells and can lose palette/glyph information. Their environment
    values can contain uid, socket, or hostname data, so only names are public-safe.
    """
    found = muxes
    if not found:
        return Row(INFO, "multiplexer", "none detected")
    if len(found) > 1:
        # Environment markers do not reveal nesting order.
        return Row(
            WARN,
            "multiplexer",
            f"{' and '.join(found)} — {len(found)} layers, each re-emitting the cells below it",
            "colour and glyph faults usually come from the outermost layer; running opentab outside them narrows it down",
        )
    name = found[0]
    if name == "herdr":
        # Leave the actionable warning to the colours row.
        return Row(
            INFO, "multiplexer", "herdr — known to discard palette writes (see colours, below)"
        )
    if "tmux" in name and colors is not None and 0 < colors < 256:
        # Low colour inside tmux is fixed in tmux, not by overriding TERM in the pane.
        return Row(
            WARN,
            "multiplexer",
            f"{name}, and TERM inside it claims only {colors} colours",
            'set -g default-terminal "tmux-256color" in ~/.tmux.conf, then start a fresh server',
        )
    return Row(OK, "multiplexer", name)


def terminal_rows() -> list[Row]:
    rows: list[Row] = []
    term = os.environ.get("TERM") or "(unset)"
    colorterm = os.environ.get("COLORTERM") or ""
    colors, ccc, usable = _terminfo()
    muxes = terminal_multiplexers()
    shell = _shell()[1]
    bits = [term]
    if colorterm:
        bits.append(f"COLORTERM={colorterm}")
    if colors is not None and colors > 0:
        bits.append(f"{colors} colours")
    if ccc is not None:
        bits.append(f"ccc={'yes' if ccc == 1 else 'no'}")
    if usable is False:
        rows.append(
            Row(
                BAD,
                "TERM",
                f"{' · '.join(bits)} · no terminfo entry — curses cannot start, so the TUI won't either",
                "set TERM to an entry this system has (TERM=xterm-256color), or install its terminfo database"
                if os.environ.get("TERM")
                else f"TERM is not set; {_export('TERM', 'xterm-256color', shell)} (a bare `su`, cron, or some CI shells drop it)",
            )
        )
    elif colors is not None and 0 < colors < 256:
        rows.append(
            Row(
                WARN,
                "TERM",
                " · ".join(bits),
                # Never recommend overriding TERM inside a multiplexer.
                ""
                if muxes
                else "themes fall back to the 8/16-colour ANSI ramp; TERM=xterm-256color renders them properly",
            )
        )
    else:
        rows.append(Row(OK, "TERM", " · ".join(bits)))

    rows.append(_multiplexer_row(colors, muxes))

    allowed = init_color_allowed()
    forced = env_flag("OPENTAB_NO_INIT_COLOR")
    if allowed:
        detail = "exact — the theme's hexes are written into palette slots (init_color)"
        if forced is False and palette_writes_ignored():
            detail = "exact — forced back on by $OPENTAB_NO_INIT_COLOR=0 inside a host known to drop palette writes"
        rows.append(Row(OK, "colours", detail))
    elif forced:
        rows.append(
            Row(
                INFO,
                "colours",
                "nearest-256 — $OPENTAB_NO_INIT_COLOR is set",
                "unset it to get exact theme colours back on a terminal that renders palette writes",
            )
        )
    else:
        rows.append(
            Row(
                WARN,
                "colours",
                "nearest-256 — this host stores palette writes and renders the index instead (herdr, via $HERDR_ENV)",
                f"themes still work, the hues are approximated; {_export('OPENTAB_NO_INIT_COLOR', '0', shell)} forces the exact path back on",
            )
        )
    if allowed and ccc == 0:
        rows.append(
            Row(
                WARN,
                "",
                "terminfo does not advertise ccc, so those palette writes may be refused",
                f"if `C` changes nothing while other apps re-colour, {_export('OPENTAB_NO_INIT_COLOR', '1', shell)}",
            )
        )

    try:
        codeset = locale.nl_langinfo(locale.CODESET) or "?"
    except (AttributeError, ValueError):
        codeset = "n/a"
    if unicode_screen():
        rows.append(Row(OK, "glyphs", f"{codeset} · heavy box frames and block bars"))
    elif term == "linux":
        rows.append(
            Row(
                INFO,
                "glyphs",
                "ACS frames — the Linux console font carries no heavy box drawing",
                "",
            )
        )
    else:
        rows.append(
            Row(
                WARN,
                "glyphs",
                f"ACS frames — the locale ({codeset}) cannot encode box drawing",
                "set a UTF-8 locale (LANG=C.UTF-8) for the full line set",
            )
        )

    if curses is None:
        rows.append(
            Row(
                BAD,
                "curses",
                "not available — the TUI cannot start",
                "native Windows Python ships no curses: `pip install windows-curses`, or run opentab under WSL",
            )
        )
    elif usable is False:
        # Module presence does not rescue failed terminfo lookup.
        rows.append(
            Row(
                INFO, "curses", "module present — the terminfo failure above is what blocks the TUI"
            )
        )
    else:
        rows.append(
            Row(OK, "curses", f"available ({'windows-curses' if os.name == 'nt' else 'stdlib'})")
        )
    return rows


def price_rows(full: bool = False) -> list[Row]:
    meta = pricing.price_source_meta()
    if not meta:
        return [
            Row(
                BAD,
                "catalog",
                "no model price catalog readable",
                "the bundled snapshot is missing from this install; `opentab --refresh-models` fetches one, or reinstall",
            )
        ]
    kind = "refreshed cache" if meta.get("kind") == "cache" else "bundled snapshot"
    fetched = str(meta.get("fetched_at") or "")[:10] or "?"
    rows = [
        Row(OK, "catalog", f"{kind} · fetched {fetched} · {len(pricing.catalog_models()):,} models")
    ]
    cache_meta = pricing.price_cache_meta()
    path = pricing.price_cache_path()
    if cache_meta:
        rows.append(
            Row(
                OK,
                "price cache",
                f"{_tilde(path, full)} · fetched {str(cache_meta.get('fetched_at') or '')[:10]} · {human_bytes(_size(path))}",
            )
        )
    else:
        rows.append(
            Row(
                INFO,
                "price cache",
                "none — the bundled snapshot serves every price, and opentab makes no network calls",
                "",
            )
        )
    return rows


def file_rows(args: argparse.Namespace, full: bool = False) -> list[Row]:
    rows: list[Row] = []

    keymap = bindings.keymap_path()
    if _blocked(keymap):
        rows.append(
            Row(
                WARN,
                "keymap",
                f"{_tilde(keymap, full)} · cannot be read — its directory denies access",
                "check the permissions on that directory; opentab falls back to the built-in keys",
            )
        )
    elif not os.path.exists(keymap):
        rows.append(
            Row(
                INFO,
                "keymap",
                f"{_tilde(keymap, full)} · not installed yet (written on the first TUI launch)",
            )
        )
    elif not os.access(keymap, os.R_OK):
        # The TUI safely falls back to defaults; doctor must expose the ignored config.
        rows.append(
            Row(
                WARN,
                "keymap",
                f"{_tilde(keymap, full)} · unreadable — every custom binding in it is being ignored",
                "check its permissions; until then the TUI runs on the built-in defaults",
            )
        )
    else:
        warnings = bindings.load_user_keymap(keymap).warnings
        if warnings:
            rows.append(
                Row(
                    WARN,
                    "keymap",
                    f"{_tilde(keymap, full)} · {len(warnings)} problem(s): {warnings[0]}",
                    "those bindings fall back to their defaults; the TUI reports them as notices too",
                )
            )
        else:
            rows.append(Row(OK, "keymap", f"{_tilde(keymap, full)} · {human_bytes(_size(keymap))}"))

    st_path = state.state_path(migrate=False)
    if _blocked(st_path):
        rows.append(
            Row(
                WARN,
                "state",
                f"{_tilde(st_path, full)} · cannot be read — its directory denies access",
                "prefs fall back to defaults until that directory is readable",
            )
        )
    elif not os.path.exists(st_path):
        rows.append(
            Row(INFO, "state", f"{_tilde(st_path, full)} · none yet (prefs are written on quit)")
        )
    else:
        try:
            with open(st_path, encoding="utf-8") as fh:
                data = json.load(fh)
            ok = isinstance(data, dict)
        except (OSError, ValueError):
            ok = False
        if ok:
            rows.append(
                Row(
                    OK,
                    "state",
                    f"{_tilde(st_path, full)} · {human_bytes(_size(st_path))} · {_n(len(data), 'pref')}",
                )
            )
        else:
            rows.append(
                Row(
                    WARN,
                    "state",
                    f"{_tilde(st_path, full)} · unreadable — prefs fall back to defaults",
                    "delete it to start clean; nothing in it is unrecoverable",
                )
            )

    notes_file = notes.notes_path(migrate=False)
    if _blocked(notes_file):
        # Never call the one unrebuildable file absent when its parent only denies access.
        rows.append(
            Row(
                BAD,
                "notes",
                f"{_tilde(notes_file, full)} · cannot be read — its directory denies access, so any notes in it are invisible AND unsaveable",
                "fix the permissions on that directory before writing more notes",
            )
        )
    elif not os.path.exists(notes_file):
        rows.append(Row(INFO, "notes", f"{_tilde(notes_file, full)} · none yet"))
    else:
        saved, readable = notes.read_notes(notes_file)
        if readable:
            rows.append(
                Row(
                    OK,
                    "notes",
                    f"{_tilde(notes_file, full)} · {_n(len(saved), 'note')} · {human_bytes(_size(notes_file))}",
                )
            )
        else:
            # Notes are authored and unrebuildable; the app correctly refuses to overwrite
            # malformed JSON, leaving future edits unsaved until repaired.
            rows.append(
                Row(
                    BAD,
                    "notes",
                    f"{_tilde(notes_file, full)} · unreadable — opentab refuses to overwrite it, so new notes cannot be saved",
                    'back the file up, then repair its JSON: the shape is {"notes": {"<session id>": "<text>"}}',
                )
            )

    warm = cached.cache_dir()
    count, newest, total = _dir_stats(warm, "*.json")
    if count:
        rows.append(
            Row(
                OK,
                "warm cache",
                f"{_tilde(warm, full)} · {_n(count, 'backend')} · {human_bytes(total)} · newest {_age(newest)}",
            )
        )
    else:
        rows.append(
            Row(
                INFO,
                "warm cache",
                f"{_tilde(warm, full)} · empty — the next launch is a cold parse",
                "",
            )
        )

    remotes = getattr(args, "remotes", None) or sources.default_remotes_dir()
    files = sorted(glob.glob(os.path.join(remotes, "*.json")))
    if files:
        who = (
            ", ".join(os.path.basename(f)[:-5] for f in files)
            if full
            else _n(len(files), "machine")
        )
        rows.append(
            Row(
                OK,
                "fleet",
                f"{_tilde(remotes, full)} · {who} · newest {_age(max(_mtime(f) for f in files))}",
            )
        )
    else:
        rows.append(
            Row(INFO, "fleet", "no pulled machine summaries (`opentab pull HOST` fetches one)", "")
        )

    legacy = _legacy_names()
    if legacy:
        # Explain apparently missing state/cache without migrating it.
        rows.append(
            Row(
                INFO,
                "legacy",
                f"{_tilde(paths.config_dir(), full)} still holds {', '.join(legacy)} from before the XDG split — the next TUI launch moves them",
                "",
            )
        )
    return rows


# Only variables opentab itself reads, so the block stays a list of things that can
# actually explain a behaviour rather than a dump of the user's shell.
_ENV_VARS = (
    "OPENTAB_NO_INIT_COLOR",
    "OPENTAB_MAX_WORKERS",
    "OPENTAB_DEMO_SCALE",
    "OPENTAB_LAUNCHER",
    "HERDR_ENV",
    "COPILOT_OTEL_FILE_EXPORTER_PATH",
    "PI_AGENT_DIR",
    "OMP_AGENT_DIR",
    "OPENCLAW_DIR",
    "ZALY_DATA",
    "ZALY_ROOT",
    "ZALY_STATE",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "LANG",
    "LC_ALL",
    "WSL_DISTRO_NAME",
)


def env_rows(full: bool = False) -> list[Row]:
    rows = [
        Row(INFO, name, _tilde(os.environ[name], full))
        for name in _ENV_VARS
        if os.environ.get(name)
    ]
    return rows or [Row(INFO, "(none set)", "no opentab environment variable is in effect")]


def build_report(args: argparse.Namespace, full: bool = False) -> list:
    return [
        (
            "opentab",
            [
                Row(OK, "version", __version__),
                Row(OK, "install", f"{_install_method(_pkg_dir())} · {_tilde(_pkg_dir(), full)}"),
                _path_row(_pkg_dir(), full),
                Row(OK, "python", f"{platform.python_version()} · {_tilde(sys.executable, full)}"),
                Row(OK, "platform", f"{sys.platform} · {platform.machine()} · {_shell()[0]}"),
            ],
        ),
        ("harnesses", harness_rows(args, full)),
        ("terminal", terminal_rows()),
        ("prices", price_rows(full)),
        ("files", file_rows(args, full)),
        ("environment", env_rows(full)),
    ]


# Fold the finished line so separators in details and hints cannot escape ASCII mode.
_ASCII_FOLD = ((" · ", " | "), ("—", "--"), ("→", "->"), ("·", "-"))


def render(sections: list, uni: bool = True) -> list[str]:
    """Render sections with per-section label widths and an explicit ASCII fallback."""
    marks = _MARKS[bool(uni)]

    def out(line: str) -> str:
        if uni:
            return line
        for glyph, plain in _ASCII_FOLD:
            line = line.replace(glyph, plain)
        return line

    lines = [out(f"opentab doctor · {__version__}")]
    for title, rows in sections:
        width = max((len(r.label) for r in rows), default=0)
        lines.extend(("", title))
        for row in rows:
            lines.append(
                out(f"  {marks[row.status]} {row.label.ljust(width)}  {row.detail}").rstrip()
            )
            if row.hint:
                lines.append(out(f"  {' ' * (width + 4)}→ {row.hint}"))
    return lines


def doctor_command(args: argparse.Namespace) -> int:
    """Exit nonzero only for broken rows; warnings still describe working setups."""
    sections = build_report(args, full=bool(getattr(args, "full", False)))
    for line in render(sections, unicode_screen()):
        print(line)
    broken = [r for _t, rows in sections for r in rows if r.status == BAD]
    if broken:
        print()
        print(f"{len(broken)} problem(s) above need attention.")
    return 1 if broken else 0
