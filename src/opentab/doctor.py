"""`opentab doctor` — the environment report you paste into a bug report.

Almost nothing opentab gets asked is "this number is wrong". The questions are
*environment* questions — why is my harness missing, why is the text invisible, why
are the frames garbage, why does it only show one tool — and every one of them used to
cost a round trip of "what does `echo $TERM` say / does that directory exist / is the
export enabled". This command answers all of them at once, in one block, from the
machine that has the problem.

Three rules shape it:

* **It reports; it never repairs.** No file is created, no cache warmed, no price
  fetched. A doctor that changes the thing it is measuring is worse than no doctor,
  and the first thing a report has to be is a faithful description of the state that
  produced the bug. (It is also why the keymap row says "not installed yet" rather
  than installing the default the way a TUI launch does.)
* **It never parses a transcript.** Every check is a stat, a glob, or a small config
  read, so it stays fast and — more importantly — it *cannot* surface a prompt, a
  session title, or a project name that only lives inside a transcript. What it does
  print is folded to `~` and machine names are counted rather than named; `--full`
  opts out for local eyes. A report meant for a public issue has to be safe to paste
  without reading it first.
* **It borrows every verdict it can rather than re-deriving one.** `available_sources`
  decides which harnesses are present, `util.init_color_allowed` decides the colour
  path, `notes.read_notes` decides whether the notes file is readable. Doctor adds the
  *reason* and the *fix* on top. A second opinion computed here is a second opinion
  that can drift from the one the app actually acts on — which would make the report
  worse than useless exactly when someone is trusting it.

Layered beside :mod:`opentab.web`: it reads sources/state/notes/pricing and is imported
only by :mod:`opentab.cli` (lazily — a one-shot verb should not be on `status`'s
import floor).
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

# --- the report model ----------------------------------------------------------------
# A row is a verdict, a label, one line of detail, and (only when there is something to
# do about it) a hint. Sections are (title, rows). build_report returns that structure
# and render() turns it into text, so the suite asserts on verdicts rather than on
# spacing -- the same split the TUI drawers use.

OK = "ok"  # working
WARN = "warn"  # working, but degraded or hiding something
BAD = "bad"  # broken: doctor exits non-zero
INFO = "info"  # simply absent / nothing to say

_MARKS = {
    True: {OK: "✓", WARN: "⚠", BAD: "✗", INFO: "·"},
    False: {OK: "+", WARN: "!", BAD: "x", INFO: "-"},
}


class Row(NamedTuple):
    status: str
    label: str
    detail: str
    hint: str = ""


def _tilde(text: str, full: bool = False) -> str:
    """Fold $HOME to ~ — EVERY occurrence, not just a leading one.

    The username is the identifying part of everything this report prints, and folding
    it is what makes the output safe to paste. A prefix-only fold is not enough because
    several of the values here are *lists*: $PI_AGENT_DIR and $OPENCLAW_DIR are
    comma-separated (both stores read the first entry), so `~/.pi/a,$HOME/.pi/b` folded
    only its head and printed the home path of every element after it.

    The lookahead is what keeps `/Users/moextra` from becoming `~extra`: a match only
    counts when the next character cannot continue the path segment — a separator, a
    list comma, or end of string.
    """
    if not text or full:
        return text or ""
    home = os.path.expanduser("~")
    if not home or home in (os.sep, "/"):
        return text  # a $HOME of "/" would fold every absolute path to "~"
    # Separator- and case-insensitive on Windows, where the SAME directory is spelled
    # `C:\\Users\\Alice`, `C:/Users/Alice` and `c:\\users\\alice` -- an exact match leaks
    # the username the fold exists to hide, in the report written to be pasted publicly.
    pattern = r"[/\\]".join(re.escape(part) for part in re.split(r"[/\\]", home))
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.sub(pattern + r"(?![\w.-])", "~", text, flags=flags)


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0  # vanished between the glob and the stat; it simply doesn't age the set


def _age(ts: float, now: datetime | None = None) -> str:
    # relative_age speaks ISO; give it an explicitly-UTC stamp rather than a naive
    # local one, which it would then *read* as UTC and report off by the offset.
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
    """How many machine summaries the fleet view would actually load.

    RemoteStore's own acceptance rule (`stores/remote.py`: a dict with a `workflows`
    list), because counting bare `*.json` made a truncated, empty or placeholder file
    read as a working fleet — doctor green and exiting 0 while `opentab remote` on the
    same directory exits "No machine summaries found". Parsing them costs ~15ms for a
    real 4-machine / 3.5MB fleet, which is the right trade for a verdict that is
    otherwise confidently wrong.
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
    # os.path.exists() answers False both for "absent" and for "its directory won't let
    # me look", and the report must not say "none yet" about a notes file it simply
    # cannot see -- that one is unrebuildable, and the whole point of its row is to
    # notice when it is in trouble.
    parent = os.path.dirname(path)
    return bool(parent) and os.path.isdir(parent) and not os.access(parent, os.X_OK | os.R_OK)


def _legacy_names() -> list[str]:
    # Pre-XDG-split artefacts still sitting in the config dir. The next TUI launch moves
    # them (paths.migrate_legacy_caches + migrated), so this is a "your files are about
    # to move" note, not a fault -- but leaving it out would let the rows above report an
    # empty warm cache on a machine that has a perfectly good one, one directory over.
    cfg = paths.config_dir()
    names = ("state.json", "notes.json", *paths._LEGACY_CACHE_NAMES)
    return [n for n in names if os.path.exists(os.path.join(cfg, n))]


def _dir_stats(root: str, pattern: str, keep=None) -> tuple[int, float, int]:
    # (files, newest mtime, total bytes) for one glob. Stats only -- never opens a file.
    # `keep` filters by name where a glob alone can't say what the backend accepts.
    hits = glob.glob(os.path.join(root, pattern), recursive=True)
    if keep is not None:
        hits = [p for p in hits if keep(os.path.basename(p))]
    return len(hits), max((_mtime(p) for p in hits), default=0.0), sum(_size(p) for p in hits)


def _name_filter(key: str):
    """The backend's own rule for "is this file one of its sessions", where a glob
    can't express it. OpenClaw keeps `<id>.jsonl` plus `.jsonl.reset.<ts>` and
    `.jsonl.deleted.<ts>` archives but skips locks, so the `*.jsonl*` glob that catches
    the archives also counts a live `.jsonl.lock` — inflating the count and letting the
    lock's mtime pass for the session's last activity."""
    if key == "openclaw":
        from opentab.stores.openclaw import OpenClawStore

        return OpenClawStore._is_session_file
    return None


# --- the shell -----------------------------------------------------------------------------


def _shell() -> tuple[str, str]:
    """(what to call this shell, which syntax its `export` takes).

    Detection is genuinely unreliable and the row says so: `$SHELL` is the *login*
    shell, and the version variables that would identify the running one ($ZSH_VERSION,
    $BASH_VERSION, $FISH_VERSION) are shell variables that are never exported to a
    child, so a Python process cannot see them. PowerShell is the exception and the one
    that matters — it exports its own markers, and it is the one shell where every
    `export FOO=bar` in this report would be wrong rather than merely unidiomatic.
    """
    # PowerShell first, and NOT gated on Windows: it exports these on every platform,
    # and they identify the RUNNING shell where $SHELL is only the login one -- inside
    # pwsh on Linux, $SHELL still says bash.
    if os.environ.get("POWERSHELL_DISTRIBUTION_CHANNEL"):
        return "PowerShell", "powershell"
    if os.environ.get("PSModulePath"):
        # On Windows this can also be a machine-wide variable a cmd session inherits, so
        # there it is a strong hint rather than proof; off Windows only PowerShell sets it.
        return ("PowerShell (assumed)" if os.name == "nt" else "PowerShell"), "powershell"
    name = os.path.basename(os.environ.get("SHELL") or "")
    if name:
        if name.lower() in ("pwsh", "powershell", "pwsh.exe", "powershell.exe"):
            return "PowerShell", "powershell"
        return name, "fish" if name == "fish" else "posix"
    # Windows with no PowerShell marker at all: PowerShell always sets PSModulePath, so
    # its absence points at cmd, whose `set` syntax is different again.
    return ("cmd", "cmd") if os.name == "nt" else ("unknown", "posix")


def _export(var: str, value: str, kind: str) -> str:
    # One env-var assignment in the reader's own shell. Every "set this variable" hint
    # in the report goes through here: a PowerShell user handed `export FOO=bar` gets a
    # command that does not run, which is a worse outcome than no hint at all.
    # A leading ~ is expanded by POSIX shells and fish, but NOT inside a PowerShell
    # assignment or by cmd -- both would hand the tool a literal "~" directory.
    if kind == "powershell":
        return f'$env:{var} = "{value.replace("~/", "$HOME/", 1)}"'
    if kind == "cmd":
        return f"set {var}={value.replace('~/', '%USERPROFILE%/', 1)}"
    if kind == "fish":
        return f"set -gx {var} {value}"
    return f"export {var}={value}"


# --- this opentab ------------------------------------------------------------------------

# How an install is recognised, by a path fragment each installer owns. Checked in this
# order (most specific first): a pipx venv also lives under a user data dir, and a
# Homebrew-managed venv also contains "site-packages".
_INSTALL_MARKERS = (
    (os.path.join("pipx", "venvs"), "pipx"),
    (os.path.join("uv", "tools"), "uv tool"),
    (os.path.join("Cellar", "opentab"), "Homebrew"),
    ("/opt/homebrew/", "Homebrew"),
    ("/home/linuxbrew/", "Homebrew"),
    ("dist-packages", "system package manager"),
)


def _pkg_dir() -> str:
    # Where this running copy of the opentab package lives.
    return os.path.dirname(os.path.abspath(__file__))


def _install_method(pkg_dir: str) -> str:
    """How this copy of opentab got here — pipx, uv, Homebrew, a venv, a source tree.

    Worth naming rather than leaving the reader to infer it from the path, because it
    decides the answer to the next two questions anyone asks: how do I upgrade, and am
    I even running the copy I just changed. A source checkout is the tell for the
    nastiest version of the second one (see `_path_row`).
    """
    lower = pkg_dir.replace("\\", "/").lower()
    for fragment, label in _INSTALL_MARKERS:
        if fragment.replace("\\", "/").lower() in lower:
            return label
    # A src-layout checkout: the package sits in `.../src/opentab` next to a pyproject.
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
    """Every directory this interpreter would install an `opentab` console script into.

    Asked of sysconfig rather than tested as "under sys.prefix", because a
    `pip install --user` puts the package in `~/.local/lib/.../site-packages` and the
    script in `~/.local/bin` while `sys.prefix` stays `/usr` — one perfectly ordinary
    installation that the prefix test called two fighting ones. The user scheme covers
    that; the default scheme covers venvs and pipx (whose shim resolves into its own
    venv's bin).
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
    """Is the `opentab` on your PATH the one that produced this report?

    The failure this exists for: a shim on PATH pointing into a *different*
    environment than the interpreter now running — a pipx install alongside a Homebrew
    one, or a pipx venv still pointing at an old source tree. Everything you change is
    real and none of it is what runs, and nothing else in the report would say so,
    because every other row faithfully describes the copy that is executing.

    Compared by environment root, not by file: the shim on PATH is a wrapper script, so
    the honest question is which prefix it would import from.
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
        # Running `python -m opentab` out of a checkout: the divergence is the point,
        # not a mistake. Still stated, because "I fixed it and nothing changed" is the
        # same confusion from the other end -- just not something to warn about.
        return Row(INFO, "on PATH", detail)
    return Row(
        WARN,
        "on PATH",
        detail,
        "changes to one won't show up in the other; `which -a opentab` lists every copy",
    )


# --- harnesses ------------------------------------------------------------------------
# key -> where its data lives and what shape it takes. `pattern` is the glob that IS the
# data; `loose` is a wider one meaning "the tool is here, but not in the layout we
# expect" -- the difference between "not installed" (say nothing) and "you pointed the
# flag one directory too deep" (say exactly that). The two backends with a layout-
# specific probe are the two that have a `loose` glob, for precisely that reason.
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
    """The chat-session files under one VS Code user directory — and nothing else.

    `sources._vscode_available` scans three patterns, the third being a bare `*.json*`
    at the root, which is there for `--vscode-dir` pointed straight AT a chatSessions
    directory. Availability only ever opens those files looking for a token marker, so a
    stray match costs it nothing — but *counting* them made a plain VS Code install
    report its `settings.json` and `chatLanguageModels.json` as chat sessions
    ("15 chat sessions, none with recorded tokens" on a machine with 13). So the root
    pattern applies only when the directory really is a chatSessions one, and the
    extension test is the exact `.json`/`.jsonl` that `_vscode_available` applies too —
    `*.json*` alone also swallows `settings.json.bak`.
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
    # VS Code is the one backend whose absence has three different meanings, and the
    # useful one is the third: merely opening the chat panel writes session files with
    # no usage in them, so "files but no tokens" is a real state that looks like a bug.
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
        # From WSL the Windows-side store is deliberately never auto-scanned (reading
        # every session over /mnt/c would slow every startup), so this is the expected
        # state for a WSL user and the fix is the opt-in flag, not "chat more".
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
        # Configured but empty. Worth its own verdict: the user did the one non-obvious
        # step, so telling them to do it again is the wrong answer -- Copilot only writes
        # the export while it runs, and only for sessions started after the var was set.
        return (
            WARN,
            f"$COPILOT_OTEL_FILE_EXPORTER_PATH → {_tilde(env, full)} · nothing written there yet",
            "only a copilot session started AFTER the variable was set writes it — check the shell that launches copilot exports it",
        )
    # The CLI's own home means it is installed and the export was simply never turned on
    # -- the most common "opentab doesn't see my Copilot usage" report. Keyed on
    # ~/.copilot itself, NOT on the parent of --copilot-dir: that parent is whatever
    # directory the flag happened to name, so `--copilot-dir /tmp/nope` announced
    # "Copilot CLI is installed" on the strength of /tmp existing.
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
        # The interesting failure: the directory is real and full of transcripts, but
        # not where this backend looks. Say which layout it wanted.
        return Row(WARN, label, f"{shown} · {_n(stray, 'file')}, none matching {pattern}", hint)
    return Row(INFO, label, f"{shown} · no sessions yet", "")


def _selection_row(args: argparse.Namespace, present: list[str], saved: dict) -> Row:
    # Which harnesses you will actually SEE. A saved single-harness preference silently
    # hiding the others is the "opentab only shows Claude" report, and nothing else in
    # the UI announces it -- state.json is not somewhere anyone thinks to look.
    labels = [sources.SOURCE_LABELS.get(k, k) for k in present]
    pick = getattr(args, "source", "auto")
    if (
        pick == "remote"
        or getattr(args, "remote", False)
        or getattr(args, "pull", None) is not None
    ):
        # The fleet view is the one selection that does NOT need a local harness: it
        # reads the pulled summaries (plus this box, if it has any). Judging it by
        # available_sources declared "nothing to browse" and exited 1 on a machine
        # whose fleet view works perfectly.
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
        # An explicit --harness that IS present is just a statement of fact. One that
        # isn't is a misconfiguration doctor can see and the TUI can only die of --
        # make_store raises SystemExit ("Hermes database not found") the moment you drop
        # the `doctor` verb from that same command line -- so it is BAD, not a warning:
        # opentab as invoked does not work, which is exactly what the exit code is for.
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
    """One row per backend, plus the selection verdict.

    Presence is `available_sources`' answer, verbatim -- doctor explains a verdict, it
    never forms a second one, or the report would eventually contradict the app it is
    supposed to be describing. Rows are ordered actionable-first (found, then wrong-
    looking, then absent) so a 12-harness report doesn't bury its two real lines.
    """
    present = sources.available_sources(args)
    rows = [_harness_row(args, spec, spec[0] in present, full) for spec in _HARNESSES]
    rank = {OK: 0, BAD: 1, WARN: 2, INFO: 3}
    rows.sort(key=lambda r: rank.get(r.status, 9))  # stable: ties keep _HARNESSES order
    # migrate=False: doctor must not relocate a pre-XDG-split state.json as a side
    # effect of asking what source it remembers (see paths.resolved).
    saved = (
        {}
        if getattr(args, "no_state", False)
        else state.load_state(state.state_path(migrate=False))
    )
    return rows + [_selection_row(args, present, saved)]


# --- terminal -------------------------------------------------------------------------


def _terminfo() -> tuple[int | None, int | None, bool | None]:
    """(colours, ccc, terminfo usable) — what terminfo CLAIMS about this terminal.

    `setupterm` needs no screen, so this is safe outside `curses.wrapper`, and the claim
    is worth printing precisely because issue #12 was a terminal that advertises `ccc`,
    accepts every `init_color`, and renders none of them.

    The third value exists because a *failure* here is not the same as "nothing to say":
    with `TERM` unset or naming an entry the system has no terminfo for, `setupterm`
    raises — and so does `initscr()`, which means the TUI cannot start at all. Folding
    that into `(None, None)` made doctor print a green TERM row and a green curses row
    on exactly the machine that cannot run opentab. `None` (no curses to ask at all) is
    the third state, and must not be reported as a broken terminfo.

    Sound because doctor calls this once, at the top of its own process: ncurses CACHES
    the setup, so a *second* `setupterm` with a bogus TERM succeeds on the strength of
    the first (measured — which is why the suite drives the failure by monkeypatch
    rather than by flipping TERM, a test that otherwise passes alone and fails in the
    suite).
    """
    if curses is None:
        return None, None, None
    try:
        # An explicit fd, because `setupterm()` with no argument asks *sys.stdout* for
        # its fileno -- which raises when stdout is an in-process wrapper with no real
        # descriptor, and that failure is indistinguishable from a terminal with no
        # terminfo entry. Reporting "your terminal is broken" because the caller
        # captured stdout would be a false alarm of the worst kind, given this row now
        # carries a BAD. A shell redirect (`opentab doctor > report.txt`) hands over a
        # real fd and setupterm is happy with it; only the fd-less case falls back to 1.
        try:
            fd = sys.stdout.fileno()
        except (AttributeError, OSError, ValueError):
            fd = 1
        curses.setupterm(fd=fd)
        return curses.tigetnum("colors"), curses.tigetflag("ccc"), True
    except (curses.error, OSError, ValueError, AttributeError):
        return None, None, False


def _multiplexer_row(colors: int | None, muxes: list[str]) -> Row:
    """What is sitting between opentab and the actual terminal.

    Worth its own row because a multiplexer is the prime suspect whenever colours or
    frames come out wrong: it consumes opentab's escape sequences and re-emits its own,
    so it — not the emulator you can see — is where a palette write or a wide glyph
    goes missing. Issue #12 was exactly this (herdr forwarding a palette *index* rather
    than the colour behind it), and from inside the pane nothing else distinguishes it
    from a terminal that simply ignores you.

    Only the NAME is printed, never the marker's value: `$TMUX` carries a socket path
    (with your uid) and `$STY` carries the hostname, and this report is written to be
    pasted in public. They are absent from `_ENV_VARS` for the same reason.
    """
    found = muxes
    if not found:
        # Worth saying out loud rather than omitting: "nothing is wrapping this pane"
        # rules the whole layer out, which is most of the value of asking.
        return Row(INFO, "multiplexer", "none detected")
    if len(found) > 1:
        # Nesting order isn't recoverable from the environment, hence "and", not "inside".
        return Row(
            WARN,
            "multiplexer",
            f"{' and '.join(found)} — {len(found)} layers, each re-emitting the cells below it",
            "colour and glyph faults usually come from the outermost layer; running opentab outside them narrows it down",
        )
    name = found[0]
    if name == "herdr":
        # The colours row already carries the warning; two findings for one cause is
        # noise. This one states the fact and points at it.
        return Row(
            INFO, "multiplexer", "herdr — known to discard palette writes (see colours, below)"
        )
    if "tmux" in name and colors is not None and 0 < colors < 256:
        # The classic: tmux's default-terminal left at `screen`, so opentab sees 8
        # colours and drops to the ANSI ramp. The TERM row above says the colours are
        # limited; the fix belongs here, because it is tmux's setting and not the
        # terminal's.
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
    shell = _shell()[1]  # every 'set this variable' hint below is rendered in it
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
                # Inside a multiplexer this hint would be actively WRONG (setting
                # TERM=xterm-256color inside tmux is the classic anti-pattern), and the
                # multiplexer row below already carries the right fix. One cause, one fix.
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
        # The module is there; the terminfo lookup above is what stops it. Saying
        # "available" flat would contradict the ✗ two rows up.
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


# --- prices ---------------------------------------------------------------------------


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


# --- opentab's own files ---------------------------------------------------------------


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
        # load_user_keymap swallows an OSError and returns pristine defaults with no
        # warnings -- right for the TUI (a typo must not lock you out) and invisible
        # here, so a mode-000 keymap.conf reported a cheerful size row while every
        # binding in it was being ignored.
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
        # Never "none yet" for the one file opentab cannot rebuild: if there ARE notes
        # under there, saying they don't exist is the most damaging thing this report
        # could get wrong.
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
            # The one authored file opentab cannot rebuild, so this is the report's
            # only genuinely urgent line: opentab refuses to write over it, which
            # means every note you make from now on is silently going nowhere.
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
        # Not a fault -- but without it the rows above read "no state yet" and "warm
        # cache empty" on a machine that has both, one directory over, and someone
        # would go looking for a bug in the cache instead of an upgrade in progress.
        rows.append(
            Row(
                INFO,
                "legacy",
                f"{_tilde(paths.config_dir(), full)} still holds {', '.join(legacy)} from before the XDG split — the next TUI launch moves them",
                "",
            )
        )
    return rows


# --- environment ------------------------------------------------------------------------

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


# --- assembly ---------------------------------------------------------------------------


def build_report(args: argparse.Namespace, full: bool = False) -> list:
    """The whole report as (title, rows) sections — the testable shape."""
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


# Every non-ASCII glyph the report composes, and what it degrades to. Applied to the
# finished LINE rather than at each call site: the separators are typed into forty
# f-strings and the hints, and folding only the marks (the obvious half) is how a
# Linux-console report ends up with tidy `+`/`-` marks and a row of replacement blobs
# between every field.
_ASCII_FOLD = ((" · ", " | "), ("—", "--"), ("→", "->"), ("·", "-"))


def render(sections: list, uni: bool = True) -> list[str]:
    """Sections as plain lines.

    The label column is measured PER SECTION, not once for the whole report: the
    environment block's labels are variable names (`COPILOT_OTEL_FILE_EXPORTER_PATH`),
    and letting the widest of those set one global column pushes every harness detail
    twenty spaces right of its mark. Alignment inside a block is what the eye uses.

    `uni` is asked, never caught (util.unicode_screen, the frame's rule): this goes to
    stdout on the terminal that couldn't render opentab, which is exactly the terminal
    most likely to be the reason someone is running doctor at all.
    """
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
                # Set under the detail column, so a hint reads as a continuation of the
                # row above it and never as a finding of its own.
                lines.append(out(f"  {' ' * (width + 4)}→ {row.hint}"))
    return lines


def doctor_command(args: argparse.Namespace) -> int:
    """Print the report; exit 1 if anything is actually broken.

    Only a BAD row moves the exit code — a WARN is "working, but you probably didn't
    mean this", and making those fail would train everyone to ignore the code. Curses
    is never started, so this runs on the terminal that cannot render opentab.
    """
    sections = build_report(args, full=bool(getattr(args, "full", False)))
    for line in render(sections, unicode_screen()):
        print(line)
    broken = [r for _t, rows in sections for r in rows if r.status == BAD]
    if broken:
        print()
        print(f"{len(broken)} problem(s) above need attention.")
    return 1 if broken else 0
