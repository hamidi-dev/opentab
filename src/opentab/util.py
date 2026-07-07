"""Clipboard, launchers, git roots, fuzzy match, date/range parsing, tool labels."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from opentab.models import Workflow


def _read_text(path: str) -> str | None:
    # Text mode: universal-newline translation (\r\n -> \n) matches what `for line in
    # fh` yields, so a caller that splits on "\n" gets exactly the same lines. Returns
    # None on an unreadable file, which read_files_parallel drops.
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _read_worker_count(n: int) -> int:
    override = os.environ.get("OPENTAB_MAX_WORKERS")
    if override:
        try:
            return max(1, min(n, int(override)))
        except ValueError:
            pass
    # Reading files is I/O-bound and open()/read() release the GIL, so oversubscribing
    # cores overlaps the per-file open latency that dominates on Windows/WSL (every open
    # is virus-scanned). Cap it so a huge transcript dir can't spawn a thread per file.
    return max(1, min(16, n, (os.cpu_count() or 4) * 2))


def read_files_parallel(paths, max_workers: int | None = None):
    """Yield (path, text) for each readable path, IN THE GIVEN ORDER, reading the files
    concurrently in a thread pool.

    Only the I/O is parallel: json.loads and each store's cross-file dedup/merge stay on
    the caller's thread, so parsing determinism is untouched -- this just hides the
    per-file open/read latency instead of paying it one file at a time. Unreadable files
    are skipped. Set OPENTAB_MAX_WORKERS=1 to force serial reads (an A/B baseline, or a
    filesystem that punishes concurrent opens)."""
    paths = list(paths)
    if not paths:
        return
    workers = max_workers or _read_worker_count(len(paths))
    if workers <= 1 or len(paths) == 1:
        for path in paths:
            text = _read_text(path)
            if text is not None:
                yield path, text
        return
    window = workers * 4  # bound how many files' contents sit in memory at once
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="opentab-read") as ex:
        for start in range(0, len(paths), window):
            batch = paths[start : start + window]
            for path, text in zip(batch, ex.map(_read_text, batch)):
                if text is not None:
                    yield path, text


# OpenCode's built-in tools. Everything else is an MCP server or a local plugin
# tool, which OpenCode names "{server}_{tool}" (e.g. serena_find_symbol,
# playwright_browser_navigate) -- so the server is the segment before the first
# underscore. This set is only used to LABEL the Tools tab's server rollup
# (built-in vs MCP); the token/cost attribution itself never depends on it. Built-in
# names that themselves contain an underscore (apply_patch, plan_exit, todowrite...)
# must stay listed here so they aren't mis-split into a fake "apply"/"plan" server.
OPENCODE_BUILTIN_TOOLS = frozenset(
    {
        "bash",
        "read",
        "edit",
        "write",
        "grep",
        "glob",
        "list",
        "ls",
        "webfetch",
        "task",
        "todowrite",
        "todoread",
        "patch",
        "apply_patch",
        "multiedit",
        "question",
        "skill",
        "plan_exit",
        "invalid",
    }
)


def tool_namespace(tool: str) -> str:
    # Group a tool name into its source for the Tools tab's server rollup: a built-in
    # tool folds to "(built-in)"; an MCP/plugin tool ("server_name") to its server
    # prefix; anything else stands alone as its own bucket.
    if tool in OPENCODE_BUILTIN_TOOLS:
        return "(built-in)"
    return tool.split("_", 1)[0] if "_" in tool else tool


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
YEAR_PATTERN = re.compile(r"^\d{4}$")


# The clipboard CLIs we try, per platform, each paired with how to encode the
# payload for it. Windows ships none of the POSIX tools but always has clip.exe
# (OEM/ANSI -- fine for the ASCII ids/paths/commands opentab copies) with
# PowerShell's Set-Clipboard as a Unicode-safe fallback.
_WINDOWS_CLIPBOARD = (
    (["clip"], "utf-8"),
    (
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "[Console]::InputEncoding=[Text.Encoding]::UTF8;"
            "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
        ],
        "utf-8",
    ),
)
_POSIX_CLIPBOARD = (
    (["pbcopy"], "utf-8"),
    (["wl-copy"], "utf-8"),
    (["xclip", "-selection", "clipboard"], "utf-8"),
    (["xsel", "--clipboard", "--input"], "utf-8"),
)


def clipboard_tools_label() -> str:
    # The tool names to name in a "no clipboard tool" notice, per platform.
    return "clip/powershell" if sys.platform == "win32" else "pbcopy/wl-copy/xclip/xsel"


def copy_to_clipboard(text: str) -> bool:
    commands = _WINDOWS_CLIPBOARD if sys.platform == "win32" else _POSIX_CLIPBOARD
    for cmd, encoding in commands:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(
                    cmd,
                    input=text.encode(encoding),
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def open_path(path: str) -> bool:
    target = os.path.expanduser(path)
    if sys.platform == "win32":
        # Windows has no open/xdg-open; os.startfile (Windows-only) reveals the folder in
        # Explorer, with explorer.exe as the fallback.
        startfile = getattr(os, "startfile", None)
        if startfile is not None:
            try:
                startfile(target)
                return True
            except OSError:
                pass
        if shutil.which("explorer"):
            try:
                subprocess.Popen(["explorer", target])
                return True
            except OSError:
                return False
        return False
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if not shutil.which(opener):
        return False
    try:
        subprocess.Popen([opener, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def in_tmux() -> bool:
    # Inside a tmux session (not merely "tmux installed"): only then can the
    # launch menu create windows/splits/popups next to opentab's own pane.
    return bool(os.environ.get("TMUX"))


def launcher_hook() -> str | None:
    # Optional user hook, git-hooks style: an executable that receives every
    # launch-menu action instead of the built-in tmux commands, so launches can
    # be routed through personal tooling (custom popup managers, zellij, kitty,
    # ...) without any of it living here. Called as:
    #   <hook> <kind> <directory> <command>     kind ∈ window|hsplit|vsplit|popup
    # Exit 0 = handled; nonzero = stderr is shown as the launch error.
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    candidates = (
        os.environ.get("OPENTAB_LAUNCHER", ""),
        os.path.join(base, "opentab", "launcher"),
    )
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def tmux_launch_argv(kind: str, directory: str, command: str) -> list[str]:
    # Build the tmux invocation for one launch-menu action. `command` is the
    # bare resume command ("claude --resume <id>"); the directory rides on the
    # -c/-d flags.
    if kind == "window":
        return ["tmux", "new-window", "-c", directory, command]
    if kind == "hsplit":
        return ["tmux", "split-window", "-h", "-c", directory, command]
    if kind == "vsplit":
        return ["tmux", "split-window", "-v", "-c", directory, command]
    return ["tmux", "display-popup", "-E", "-d", directory, "-w", "85%", "-h", "75%", command]


def tmux_launch(kind: str, directory: str, command: str) -> str | None:
    """Run a resume command in a new tmux window/split/popup — or hand the
    whole action to the user's launcher hook when one is installed. Returns an
    error message, or None when the launch was issued."""
    hook = launcher_hook()
    argv = [hook, kind, directory, command] if hook else tmux_launch_argv(kind, directory, command)
    try:
        if kind == "popup":
            # display-popup (and popup hooks that wrap it) can block until the
            # popup closes; fire and forget so the TUI keeps running underneath.
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return None
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if proc.returncode != 0:
        return (proc.stderr or ("launcher hook failed" if hook else "tmux failed")).strip()
    return None


def normalize_project_path(directory: str) -> str:
    # Canonicalize a Windows drive path so the SAME directory recorded by different
    # tools collapses to one project. OpenCode (a JS app) stores forward slashes
    # (C:/DEV/app) while native tools fold cwd to backslashes (C:\DEV\app), and the
    # drive letter's case is arbitrary (c: == C:) -- left alone they group AND display
    # as two separate projects (issue #4). Fold both to one spelling: uppercase drive,
    # single backslash separators, no trailing slash. POSIX paths (and UNC shares,
    # agent-name pseudo-dirs, "(unknown)") don't match the drive-path shape and are
    # returned unchanged -- a backslash in a POSIX path can be a real filename char.
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", directory)
    if not m:
        return directory
    rest = m.group(2).replace("/", "\\")
    while "\\\\" in rest:  # collapse doubled separators from mixed spellings
        rest = rest.replace("\\\\", "\\")
    rest = rest.rstrip("\\")
    root = m.group(1).upper() + ":\\"
    return root + rest if rest else root


def resolve_project_root(directory: str) -> str:
    # Fold a git worktree into its main repo so feature worktrees don't show as
    # separate projects. Two strategies, both pure local reads (no `git`):
    #
    #   1. Accurate: a linked worktree's `.git` is a FILE that reads
    #      "gitdir: <main>/.git/worktrees/<name>" — the main working dir is what
    #      comes before "/.git/worktrees/". Needs the worktree dir to still exist.
    #   2. Path convention: fold ".../<repo>/.worktrees/<name>" (and the standard
    #      ".git/worktrees/<name>") to <repo>. Works even when the worktree dir is
    #      gone (a removed throwaway worktree still has sessions in the DB).
    directory = normalize_project_path(directory)  # collapse C:/ vs C:\ spellings first
    try:
        dotgit = os.path.join(os.path.expanduser(directory), ".git")
        if os.path.isfile(dotgit):
            with open(dotgit, encoding="utf-8") as fh:
                line = fh.read(4096).strip()
            if line.startswith("gitdir:"):
                gitdir = line[len("gitdir:") :].strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(os.path.join(os.path.expanduser(directory), gitdir))
                marker = os.sep + ".git" + os.sep + "worktrees" + os.sep
                if marker in gitdir:
                    main = gitdir[: gitdir.index(marker)]
                    if main:
                        return main
    except OSError:
        pass
    for marker in (os.sep + ".worktrees" + os.sep, os.sep + ".git" + os.sep + "worktrees" + os.sep):
        idx = directory.find(marker)
        if idx > 0:
            return directory[:idx]
    return directory


def git_root(directory: str) -> str:
    # Walk up from `directory` to the nearest ancestor that contains a `.git`
    # entry, so a Claude Code session started in a subdir (frontend/, src/, ...)
    # rolls up to its repo instead of showing as its own bare-basename project.
    # Pure filesystem reads; returns `directory` unchanged when the path no longer
    # exists or no repo is found (App.resolve_project_root then folds worktrees).
    # Every return is separator-canonicalized (normalize_project_path) so a Windows
    # cwd folds to one spelling regardless of the recording tool's slash style.
    try:
        cur = os.path.abspath(os.path.expanduser(directory))
    except (OSError, ValueError):
        return normalize_project_path(directory)
    if not os.path.isdir(cur):
        return normalize_project_path(directory)  # path gone -- keep the recorded cwd
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return normalize_project_path(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return normalize_project_path(directory)  # filesystem root, no repo
        cur = parent


_IS_WSL: bool | None = None


def is_wsl() -> bool:
    # WSL masquerades as plain Linux; the interop env vars (set in every WSL session)
    # or the kernel string give it away. Memoized -- probed per store build.
    global _IS_WSL
    if _IS_WSL is None:
        _IS_WSL = False
        if sys.platform == "linux":
            if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
                _IS_WSL = True
            else:
                try:
                    with open("/proc/version", encoding="utf-8", errors="replace") as fh:
                        _IS_WSL = "microsoft" in fh.read().lower()
                except OSError:
                    _IS_WSL = False
    return _IS_WSL


def wsl_mount_root(conf_path: str = "/etc/wsl.conf") -> str:
    # Where WSL mounts the Windows drives: [automount] root= in /etc/wsl.conf,
    # default /mnt (so C: appears at /mnt/c).
    try:
        section = ""
        with open(conf_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                for comment in ("#", ";"):
                    line = line.split(comment, 1)[0]
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip().lower()
                elif section == "automount" and "=" in line:
                    key, _, value = line.partition("=")
                    value = value.strip().strip("\"'")
                    if key.strip().lower() == "root" and value:
                        return value
    except OSError:
        pass
    return "/mnt"


def windows_to_wsl_path(path: str, mount_root: str | None = None) -> str:
    # Fold a Windows drive path (C:\Users\... or C:/Users/...) onto its WSL mount
    # (/mnt/c/Users/...) so filesystem probes like the git-root walk can reach it.
    # "" when not applicable: not under WSL, not a drive path, or the drive isn't
    # mounted -- callers keep the original path as a label then.
    if mount_root is None:
        if not is_wsl():
            return ""
        mount_root = wsl_mount_root()
    m = re.match(r"^([A-Za-z]):[/\\](.*)$", path)
    if not m:
        return ""
    rest = m.group(2).replace("\\", "/")
    mapped = os.path.join(mount_root.rstrip("/") or "/", m.group(1).lower(), rest)
    return mapped if os.path.exists(mapped) else ""


def fuzzy_score(query: str, text: str) -> int | None:
    """fzf-style subsequence match (case-insensitive): every query character
    must appear in `text` in order. Returns None on no match, else a score —
    higher is better. Consecutive matches and word starts score up; the gap
    skipped to reach each character scores down, so tight matches beat
    scattered ones ("trend" ranks "trend view" above "travel node script")."""
    if not query:
        return 0
    t = text.lower()
    score = 0
    pos = 0
    prev = -2
    for ch in query.lower():
        found = t.find(ch, pos)
        if found < 0:
            return None
        if found == prev + 1:
            score += 3  # extends a consecutive run
        if found == 0 or t[found - 1] in " -_/.":
            score += 2  # word / path-segment start
        score -= found - pos
        prev = found
        pos = found + 1
    return score


def workflow_fuzzy_score(query: str, workflow: Workflow) -> int | None:
    # Best match across the fields people aim for, nudged so a title hit
    # outranks an equally good directory or id hit.
    best = None
    for bonus, text in ((2, workflow.title), (1, workflow.directory), (0, workflow.id)):
        s = fuzzy_score(query, text)
        if s is not None and (best is None or s + bonus > best):
            best = s + bonus
    return best


def parse_range_text(raw: str) -> tuple[int | None, int | None, str | None, str | None]:
    # Returns (days, months, since, until). Days and months are *relative*
    # windows re-evaluated each run; since/until are absolute bounds.
    value = raw.strip().lower()
    if value in ("", "a", "all", "all time", "all-time"):
        return None, None, None, None

    duration_match = re.fullmatch(
        r"(?:last\s+)?(\d+)\s*(d(?:ays?)?|m(?:onths?)?|y(?:ears?)?)", value
    )
    if duration_match:
        amount = int(duration_match.group(1))
        unit = duration_match.group(2)[0]
        if amount <= 0:
            raise ValueError("range amount must be greater than 0")
        # Days stay a rolling day window; months and years are calendar-accurate
        # (a year is just twelve months), so "2m" is two whole months, not 60 days.
        if unit == "d":
            return amount, None, None, None
        return None, amount * (12 if unit == "y" else 1), None, None

    # A bare number is the obvious "N days" intent (4-digit values stay years).
    if value.isdigit() and not YEAR_PATTERN.fullmatch(value):
        amount = int(value)
        if amount <= 0:
            raise ValueError("range amount must be greater than 0")
        return amount, None, None, None

    if ".." in value:
        since, until = (part.strip() or None for part in value.split("..", 1))
        if since:
            validate_date(since)
        if until:
            validate_date(until)
        if since and until and since > until:
            raise ValueError("since date must be before until date")
        return None, None, since, until

    if DATE_PATTERN.fullmatch(value):
        validate_date(value)
        return None, None, value, None

    if MONTH_PATTERN.fullmatch(value):
        return None, None, *month_bounds(value)

    if YEAR_PATTERN.fullmatch(value):
        return None, None, f"{value}-01-01", f"{value}-12-31"

    raise ValueError("use all, 30d, 2m, 1y, YYYY, YYYY-MM, YYYY-MM-DD, or start..end")


def validate_date(value: str) -> None:
    if not DATE_PATTERN.fullmatch(value):
        raise ValueError("use all, 30d, YYYY-MM-DD, or YYYY-MM-DD..YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"invalid date: {value}") from exc


def month_bounds(value: str) -> tuple[str, str]:
    try:
        start = datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise ValueError(f"invalid month: {value}") from exc
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1) - timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1) - timedelta(days=1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def month_window_start(n: int, today: datetime | None = None) -> str:
    # First day of the calendar month that opens a trailing window of n months:
    # this month plus the n-1 before it. So "2m" spans exactly two month buckets
    # (this month and last), whatever today's day-of-month is -- which is what the
    # month-oriented views expect, rather than a 60-day window straddling three.
    base = today or datetime.now()
    year, month0 = divmod(base.year * 12 + (base.month - 1) - (n - 1), 12)
    return datetime(year, month0 + 1, 1).strftime("%Y-%m-%d")
