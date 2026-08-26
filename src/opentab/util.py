from __future__ import annotations

import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

from opentab import paths
from opentab.models import Workflow

_UNICODE_SCREEN: bool | None = None


def unicode_screen() -> bool:
    """Whether curses can render the UI's multibyte glyphs with stable cell widths.

    In non-UTF-8 locales curses can silently paint garbage rather than raise (measured:
    heavy ``┏`` became 0x0f). The Linux console also needs ACS despite UTF-8 because its
    font lacks heavy frames. Windows without nl_langinfo supports wide glyphs via curses.
    """
    global _UNICODE_SCREEN
    if _UNICODE_SCREEN is None:
        if os.environ.get("TERM") == "linux":
            _UNICODE_SCREEN = False
        else:
            try:
                _UNICODE_SCREEN = "utf" in locale.nl_langinfo(locale.CODESET).lower()
            except (AttributeError, ValueError):
                _UNICODE_SCREEN = True
    return _UNICODE_SCREEN


def env_flag(name: str) -> bool | None:
    """Read an environment switch without treating ``0`` or ``false`` as enabled."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() not in ("0", "false", "no", "off")


def palette_writes_ignored() -> bool:
    """Detect hosts known to accept palette writes but not render them.

    This cannot be probed: curses and OSC queries report stored palette state, not the
    displayed cell. Herdr 0.7.5 forwards ``CellColor::Palette(i)`` as the index, so the
    outer terminal resolves its own palette and discards the redefinition.
    """
    return in_herdr()


# Use markers set by each multiplexer, never TERM guesses. Presence matters because
# Zellij sets ``ZELLIJ=0``; Herdr alone shares env_flag semantics with colour detection.
_MULTIPLEXERS = (
    ("tmux", "TMUX"),
    ("GNU screen", "STY"),
    ("zellij", "ZELLIJ"),
    ("herdr", "HERDR_ENV"),
    ("dvtm", "DVTM"),
)


def terminal_multiplexers() -> list[str]:
    """Return detected multiplexer layers; environment markers reveal no nesting order.

    Byobu qualifies its tmux/screen backend rather than adding a false second layer.
    """

    def running(name: str, var: str) -> bool:
        if name == "herdr":
            return in_herdr()
        return bool(os.environ.get(var))

    found = [name for name, var in _MULTIPLEXERS if running(name, var)]
    backend = (os.environ.get("BYOBU_BACKEND") or "").strip().lower()
    for i, name in enumerate(found):
        if (backend == "tmux" and name == "tmux") or (backend == "screen" and name == "GNU screen"):
            found[i] = f"byobu ({name})"
    return found


def init_color_allowed() -> bool:
    """Choose exact palette writes or nearest-256 fallback.

    ``OPENTAB_NO_INIT_COLOR`` overrides detection in both directions because failures
    cannot be probed and hosts may later be fixed. It is terminal state, not persisted
    app state. Doctor and renderer share this single decision.
    """
    forced = env_flag("OPENTAB_NO_INIT_COLOR")
    if forced is not None:
        return not forced
    return not palette_writes_ignored()


# Bound untrusted numbers to the largest exact float integer. Huge Python ints fail only
# later during pricing/formatting, while individually finite floats such as 1e308 can sum
# to infinity and silently poison every aggregate.
_NUMERIC_LIMIT = 1 << 53


def safe_int(value) -> int:
    """Coerce an untrusted non-negative count, rejecting infinities and huge integers."""
    try:
        num = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return num if 0 <= num <= _NUMERIC_LIMIT else 0


def safe_float(value, default: float = 0.0) -> float:
    """Coerce an untrusted summable float, bounded to prevent infinite aggregates."""
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if -_NUMERIC_LIMIT <= num <= _NUMERIC_LIMIT else default


def _read_text(path: str) -> str | None:
    # Universal-newline text matches serial line iteration; unreadable files are skipped.
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
    # Overlap Windows/WSL open latency without spawning a thread per transcript.
    return max(1, min(16, n, (os.cpu_count() or 4) * 2))


def read_files_parallel(paths, max_workers: int | None = None):
    """Read concurrently but yield readable files in input order.

    Parsing and cross-file dedup stay serial and deterministic. Set
    ``OPENTAB_MAX_WORKERS=1`` for an A/B baseline or hostile filesystem.
    """
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
    # Keep concurrent.futures' measured ~10ms import cost off one-shot cost commands.
    from concurrent.futures import ThreadPoolExecutor

    window = workers * 4  # Bound file contents retained in memory.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="opentab-read") as ex:
        for start in range(0, len(paths), window):
            batch = paths[start : start + window]
            for path, text in zip(batch, ex.map(_read_text, batch)):
                if text is not None:
                    yield path, text


# Public historical name covering built-ins across harnesses. Underscored built-ins must
# be explicit or server rollups misclassify them; attribution never depends on this set.
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
        # Claude Code logs these in CamelCase.
        "websearch",
        "toolsearch",
        "notebookedit",
        "askuserquestion",
        "exitplanmode",
        "enterplanmode",
        "bashoutput",
        "killshell",
        "slashcommand",
        # Codex CLI
        "shell",
        "shell_command",
        "update_plan",
        "web_search",
        "view_image",
    }
)


def tool_namespace(tool: str) -> str:
    # Normalize built-ins case-insensitively and both MCP naming conventions.
    if tool.lower() in OPENCODE_BUILTIN_TOOLS:
        return "(built-in)"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return parts[1] if len(parts) > 2 and parts[1] else "mcp"
    return tool.split("_", 1)[0] if "_" in tool else tool


def short_tool_name(tool: str) -> str:
    # Remove the MCP wrapper but retain the server, which disambiguates duplicate names.
    # Non-MCP underscores remain intact.
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        if len(parts) > 2 and parts[1]:
            return parts[1] + "/" + "__".join(parts[2:])
    return tool


def tool_names(value) -> list[str]:
    """Validate the transcript/export ``tools`` trust boundary in one place.

    Reject non-string and empty names before labels, hashing, or frontend column gates.
    A bare string is invalid because treating it as an iterable creates character tools.
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [t for t in value if isinstance(t, str) and t]


def tool_call_label(tools) -> str:
    # Preserve call order while folding repeats into counts.
    counts: dict[str, int] = {}
    for tool in tool_names(tools):
        name = short_tool_name(tool)
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(n if c == 1 else f"{n} ×{c}" for n, c in counts.items())


def tool_mix_label(turns) -> str:
    # Rank prompt-wide tools busiest-first; stable ties preserve first-call order.
    counts: dict[str, int] = {}
    for turn in turns or ():
        for tool in tool_names(turn.get("tools")):
            name = short_tool_name(tool)
            counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    return ", ".join(n if c == 1 else f"{n} ×{c}" for n, c in ordered)


# Shared dull-name rule keeps flamegraph and Turns labels consistent.
DULL_AGENT_NAMES = frozenset({"", "-", "subagent", "unknown", "(untitled)"})


def agent_mix_label(turns) -> str:
    # Count only nested turns. Fold anonymous executions into one useful total.
    counts: dict[str, int] = {}
    unnamed = 0
    for turn in turns or ():
        if not turn.get("depth"):
            continue
        name = str(turn.get("agent") or "").strip()
        if name.lower() in DULL_AGENT_NAMES:
            unnamed += 1
            continue
        counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    parts = [n if c == 1 else f"{n} ×{c}" for n, c in ordered]
    if unnamed:
        parts.append("subagent" if unnamed == 1 else f"subagent ×{unnamed}")
    return ", ".join(parts)


def tool_rows_from_turns(turns: list[dict]) -> list[dict]:
    # Attribute a turn evenly across all calls, including duplicates. This measures
    # tokens in turns using a tool, not the tool output's size.
    agg: dict[tuple[str, str], dict] = {}
    # Dividing the 1h-write subset by the same share preserves its subset invariant.
    fields = (
        "tokens_total",
        "input",
        "output",
        "reasoning",
        "cache_read",
        "cache_write",
        "cache_write_1h",
        "cost",
    )
    for t in turns:
        # Validate before names become hash keys or take an attribution share.
        tools = tool_names(t.get("tools"))
        n = len(tools)
        if not n:
            continue
        for tool in tools:
            row = agg.get((tool, t["model_name"]))
            if row is None:
                row = agg[(tool, t["model_name"])] = dict.fromkeys(fields, 0.0)
                row["tool"] = tool
                row["model_name"] = t["model_name"]
                row["calls"] = 0
            row["calls"] += 1
            for f in fields:
                row[f] += (t.get(f) or 0) / n
    return sorted(agg.values(), key=lambda r: (r["cost"], r["tokens_total"]), reverse=True)


def model_row_split(row) -> tuple[float, float, float, float, float]:
    # Match api_equivalent_cost's argument order. Per-model rows, unlike dominant-model
    # nodes, provide the exact what-if baseline. Infer input for legacy rows from remainder.
    out = float(row.get("output") or 0)
    reasoning = float(row.get("reasoning") or 0)
    cache_read = float(row.get("cache_read") or 0)
    cache_write = float(row.get("cache_write") or 0)
    inp = row.get("input")
    if inp is None:
        inp = max(
            0.0, float(row.get("tokens_total") or 0) - out - reasoning - cache_read - cache_write
        )
    return float(inp), out, reasoning, cache_read, cache_write


def model_row_1h_write(row) -> float:
    """Return the 1h subset separately so token-mix totals never double-count it."""
    return float(row.get("cache_write_1h") or 0)


def node_1h_write(node) -> int:
    """Read the optional 1h subset from dicts or sqlite3.Row values."""
    try:
        return int(node["tokens_cache_write_1h"] or 0)
    except (KeyError, IndexError, TypeError):
        return 0


# No tokenizer in the stdlib, so composition sizes are chars/4 estimates -- the
# same coarse constant zaly's own /context command uses (and labels "estimated"),
# which keeps opentab's Zaly numbers comparable to zaly's. Attachments have no
# text length; these are zaly's flat per-attachment guesses.
EST_CHARS_PER_TOKEN = 4
ATTACHMENT_EST_TOKENS = {"image": 1500, "audio": 3000, "pdf": 8000, "video": 5000}


def est_tokens(text) -> int:
    if not text:
        return 0
    return max(1, (len(text) + EST_CHARS_PER_TOKEN - 1) // EST_CHARS_PER_TOKEN)


def context_add(ctx: dict, category: str, kind: str, tokens: int, count: int = 1) -> None:
    if tokens <= 0:
        return
    row = ctx.get((category, kind))
    if row is None:
        row = ctx[(category, kind)] = [0, 0]
    row[0] += count
    row[1] += tokens


def context_rows(ctx: dict) -> list[dict]:
    return sorted(
        (
            {"category": cat, "kind": kind, "count": c, "est_tokens": t}
            for (cat, kind), (c, t) in ctx.items()
        ),
        key=lambda r: r["est_tokens"],
        reverse=True,
    )


# Shared heuristic keeps four context/turn views consistent. Both bounds are strict:
# previous context must exceed the floor and the new value be below the ratio.
CONTEXT_COMPACT_FLOOR = 50_000
CONTEXT_COMPACT_RATIO = 0.6


def context_size(row) -> int:
    # Anthropic-style input plus cache reads/writes is the live prompt window.
    return int(
        (row.get("input") or 0) + (row.get("cache_read") or 0) + (row.get("cache_write") or 0)
    )


# Below provider cache minima, a cache share would imply false precision.
CACHED_SHARE_FLOOR = 5000


def cached_share(row):
    """Return one turn's cache-hit share, or None below the meaningful context floor.

    Use cache_read rather than cache_write so billed writes and providers recording a
    miss as plain input share one rule. In about 37k measured turns, ordinary misses and
    full re-buys formed separate ranges, so no extra threshold is needed.
    """
    ctx = context_size(row)
    if ctx < CACHED_SHARE_FLOOR:
        return None
    return max(0.0, min(1.0, (row.get("cache_read") or 0) / ctx))


def context_compactions(rows) -> dict:
    # Subagents have independent windows; zero-size turns neither clear nor break the chain.
    out: dict[int, tuple[int, int]] = {}
    prev = 0
    for i, row in enumerate(rows):
        if row.get("depth"):
            continue
        size = context_size(row)
        if size <= 0:
            continue
        if prev > CONTEXT_COMPACT_FLOOR and size < prev * CONTEXT_COMPACT_RATIO:
            out[i] = (prev, size)
        prev = size
    return out


class LazyStatusRoot(dict):
    """Resolve expensive recent-root fields only until a newest-first scan matches.

    Directory needs a file-head read; Codex root IDs may require walking spawned threads.
    """

    def __init__(self, fields: dict, lazy: dict):
        super().__init__(fields)
        self._lazy = lazy

    def __getitem__(self, key):
        if key not in self and key in self._lazy:
            self[key] = self._lazy[key]()
        return super().__getitem__(key)


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
YEAR_PATTERN = re.compile(r"^\d{4}$")


# Clipboard commands and input encoding by platform; PowerShell is Windows' Unicode fallback.
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
        # Windows has no open/xdg-open; prefer its shell association.
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
    # This asks whether tmux can create a sibling pane, unlike general mux detection.
    return bool(os.environ.get("TMUX"))


def in_herdr() -> bool:
    return env_flag("HERDR_ENV") is True


def herdr_pane_id() -> str | None:
    pane = os.environ.get("HERDR_PANE_ID")
    if pane is None:
        return None
    pane = pane.strip()
    return pane or None


def _current_tty() -> str | None:
    try:
        return os.ttyname(sys.stdin.fileno())
    except (AttributeError, OSError, ValueError):
        return None


def _tmux_pane_tty() -> str | None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_tty}"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    tty = proc.stdout.strip()
    return tty or None


def launch_backend() -> str | None:
    """Select the explicit hook or infer the innermost supported multiplexer."""
    if launcher_hook() is not None:
        return "hook"
    tmux = in_tmux()
    herdr = in_herdr()
    if tmux and not herdr:
        return "tmux"
    if herdr and not tmux:
        return "herdr"
    if not tmux:
        return None

    current_tty = _current_tty()
    pane_tty = _tmux_pane_tty()
    if current_tty and pane_tty:
        return "tmux" if current_tty == pane_tty else "herdr"
    term = os.environ.get("TERM", "")
    return "tmux" if term.startswith(("tmux", "screen")) else "herdr"


def herdr_create_argv(kind: str, directory: str) -> list[str]:
    configured = os.environ.get("HERDR_BIN_PATH")
    herdr = (
        configured
        if configured and os.path.isfile(configured) and os.access(configured, os.X_OK)
        else "herdr"
    )
    if kind == "window":
        argv = [herdr, "tab", "create"]
        workspace = os.environ.get("HERDR_WORKSPACE_ID")
        if workspace:
            argv.extend(["--workspace", workspace])
        return argv + ["--cwd", directory, "--focus"]
    if kind == "hsplit":
        pane = herdr_pane_id()
        if pane is None:
            raise ValueError("HERDR_PANE_ID is required for Herdr splits")
        return [
            herdr,
            "pane",
            "split",
            "--pane",
            pane,
            "--direction",
            "right",
            "--cwd",
            directory,
            "--focus",
        ]
    if kind == "vsplit":
        pane = herdr_pane_id()
        if pane is None:
            raise ValueError("HERDR_PANE_ID is required for Herdr splits")
        return [
            herdr,
            "pane",
            "split",
            "--pane",
            pane,
            "--direction",
            "down",
            "--cwd",
            directory,
            "--focus",
        ]
    if kind == "popup":
        raise ValueError("herdr does not support popups")
    raise ValueError(f"unknown Herdr launch kind: {kind}")


def _herdr_failure(stage: str, proc) -> str:
    detail = (getattr(proc, "stderr", "") or "").strip()
    # Prefer Herdr's human stderr; never dump a raw JSON failure into a toast.
    try:
        json.loads(detail)
    except (TypeError, json.JSONDecodeError):
        if detail:
            return f"herdr {stage} failed: {detail.splitlines()[0][:200]}"
    return f"herdr {stage} failed (exit status {getattr(proc, 'returncode', '?')})"


def _herdr_cli_launch(kind: str, directory: str, command: str) -> str | None:
    create_argv = herdr_create_argv(kind, directory)
    try:
        created = subprocess.run(create_argv, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "herdr create timed out"
    except OSError as exc:
        return f"herdr create failed: {exc}"
    if created.returncode != 0:
        return _herdr_failure("create", created)
    try:
        payload = json.loads(created.stdout)
    except json.JSONDecodeError:
        return "herdr create returned invalid JSON"

    key = "root_pane" if kind == "window" else "pane"
    result = payload.get("result") if isinstance(payload, dict) else None
    pane = result.get(key) if isinstance(result, dict) else None
    pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
    if not isinstance(pane_id, str) or not pane_id:
        return f"herdr create returned no valid pane ID for {key}"

    try:
        ran = subprocess.run(
            [create_argv[0], "pane", "run", pane_id, command],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return f"herdr pane run timed out for pane {pane_id}"
    except OSError as exc:
        return f"herdr pane run failed for pane {pane_id}: {exc}"
    if ran.returncode != 0:
        return f"{_herdr_failure('pane run', ran)} for pane {pane_id}"
    return None


def herdr_launch(kind: str, directory: str, command: str) -> str | None:
    try:
        return _herdr_cli_launch(kind, directory, command)
    except ValueError as exc:
        return str(exc)


def launcher_hook() -> str | None:
    # Optional executable override for custom terminal tooling. Called as:
    #   <hook> <kind> <directory> <command>     kind ∈ window|hsplit|vsplit|popup
    # Exit 0 = handled; nonzero = stderr is shown as the launch error.
    candidates = (
        os.environ.get("OPENTAB_LAUNCHER", ""),
        os.path.join(paths.config_dir(), "launcher"),
    )
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


_LOCAL_MACHINE = ""


def local_machine_name() -> str:
    # Fleet stamping and untagged App rows must share one memoized hostname. Keep socket's
    # import off one-shot cost commands.
    global _LOCAL_MACHINE
    if not _LOCAL_MACHINE:
        import socket

        _LOCAL_MACHINE = socket.gethostname() or "this-machine"
    return _LOCAL_MACHINE


def ssh_command(target: str, directory: str, command: str) -> str:
    # Agent CLIs need a tty. Quote the remote command as one argument so `&&` runs in the
    # remote shell rather than changing the local shell's directory.
    inner = f"cd {shlex.quote(directory)} && {command}"
    return f"ssh -t {shlex.quote(target)} {shlex.quote(inner)}"


def tmux_launch_argv(kind: str, directory: str, command: str) -> list[str]:
    if kind == "window":
        return ["tmux", "new-window", "-c", directory, command]
    if kind == "hsplit":
        return ["tmux", "split-window", "-h", "-c", directory, command]
    if kind == "vsplit":
        return ["tmux", "split-window", "-v", "-c", directory, command]
    return ["tmux", "display-popup", "-E", "-d", directory, "-w", "85%", "-h", "75%", command]


def _run_tmux_or_hook(kind: str, argv: list[str], label: str) -> str | None:
    try:
        if kind == "popup":
            # Popups may block until close; keep the underlying TUI responsive.
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return None
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        return detail or f"{label} failed"
    return None


def launch_command(
    kind: str,
    directory: str,
    command: str,
    backend: str | None = None,
) -> str | None:
    backend = launch_backend() if backend is None else backend
    if backend == "hook":
        hook = launcher_hook()
        if hook is None:
            return "launcher hook unavailable"
        return _run_tmux_or_hook(kind, [hook, kind, directory, command], "launcher hook")
    if backend == "tmux":
        return _run_tmux_or_hook(kind, tmux_launch_argv(kind, directory, command), "tmux")
    if backend == "herdr":
        return herdr_launch(kind, directory, command)
    return "no supported launch backend available"


def tmux_launch(kind: str, directory: str, command: str) -> str | None:
    """Legacy tmux entry point; retain the launcher-hook override."""
    hook = launcher_hook()
    argv = [hook, kind, directory, command] if hook else tmux_launch_argv(kind, directory, command)
    return _run_tmux_or_hook(kind, argv, "launcher hook" if hook else "tmux")


def normalize_project_path(directory: str) -> str:
    # Canonicalize drive case and separators so JS/native Windows cwd spellings group as
    # one project. Leave POSIX and UNC paths untouched; backslash may be a valid POSIX name.
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", directory)
    if not m:
        return directory
    rest = m.group(2).replace("/", "\\")
    while "\\\\" in rest:
        rest = rest.replace("\\\\", "\\")
    rest = rest.rstrip("\\")
    root = m.group(1).upper() + ":\\"
    return root + rest if rest else root


def resolve_project_root(directory: str) -> str:
    # Prefer a linked worktree's .git pointer; path conventions cover deleted worktrees.
    # Both are local reads and avoid invoking git.
    directory = normalize_project_path(directory)
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
    # Walk to the nearest .git ancestor using filesystem reads only. Preserve missing or
    # non-repo recorded paths, but canonicalize Windows separators on every return.
    try:
        cur = os.path.abspath(os.path.expanduser(directory))
    except (OSError, ValueError):
        return normalize_project_path(directory)
    if not os.path.isdir(cur):
        return normalize_project_path(directory)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return normalize_project_path(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return normalize_project_path(directory)
        cur = parent


_IS_WSL: bool | None = None


def is_wsl() -> bool:
    # WSL identifies itself through interop variables or the kernel string.
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
    # Return empty unless a Windows drive path maps to an existing WSL mount; callers then
    # keep the original path as a label.
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
    """Score a case-insensitive subsequence, rewarding runs and word starts."""
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
            score += 3
        if found == 0 or t[found - 1] in " -_/.":
            score += 2
        score -= found - pos
        prev = found
        pos = found + 1
    return score


_WORD_BOUNDS = " -_/."


def anchored_fuzzy_match(query: str, text: str) -> bool:
    """Match identifier subsequences that enter each new word only at its start.

    Unlike ranked fuzzy search, binary model filters preserve row order; unrestricted
    scatter would leave irrelevant matches near the top of the 5k-row catalog.
    """
    if not query:
        return True
    q = query.lower()
    t = text.lower()
    if q in t:
        return True
    # Reject most catalog rows with a cheap plain-subsequence scan first.
    pos = 0
    for ch in q:
        pos = t.find(ch, pos) + 1
        if pos == 0:
            return False
    # Track all viable prefixes in one linear pass. The former backtracking regex froze
    # for seconds on near misses; a greedy scan cannot recover from a dead-end prefix.
    #   done[i]   -- q[:i] fully matched somewhere before this point
    #   in_word[i] -- ...with its last char inside the CURRENT word, so q[i]
    #                 may scatter onto any later char of the same word; a new
    #                 word admits q[i] only as its first char (start + done).
    n = len(q)
    prefix_at: dict = {}
    for i in range(n, 0, -1):  # One text character cannot satisfy two query positions.
        prefix_at.setdefault(q[i - 1], []).append(i)
    done = [True] + [False] * n
    in_word = [False] * (n + 1)
    start = True
    for c in t:
        boundary = c in _WORD_BOUNDS
        for i in prefix_at.get(c, ()):
            # Query separators match later separators without adding word anchoring.
            if boundary:
                ok = done[i - 1]
            else:
                ok = in_word[i - 1] or (start and done[i - 1])
            if ok:
                if i == n:
                    return True
                done[i] = True
                if not boundary:
                    in_word[i] = True
        if boundary:
            in_word = [False] * (n + 1)
            start = True
        else:
            start = False
    return False


def workflow_fuzzy_score(query: str, workflow: Workflow, note: str = "") -> int | None:
    # Prefer equal-quality title matches; notes remain searchable when titles are poor.
    best = None
    for bonus, text in (
        (2, workflow.title),
        (1, workflow.directory),
        (1, note),
        (0, workflow.id),
    ):
        s = fuzzy_score(query, text)
        if s is not None and (best is None or s + bonus > best):
            best = s + bonus
    return best


def parse_range_text(raw: str) -> tuple[int | None, int | None, str | None, str | None]:
    # Relative days/months are reevaluated each run; since/until are absolute.
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
        # Months and years are calendar windows, not fixed day counts.
        if unit == "d":
            return amount, None, None, None
        return None, amount * (12 if unit == "y" else 1), None, None

    # Bare numbers mean days; four digits remain a year.
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
    # Include this month and n-1 previous whole month buckets, independent of today's day.
    base = today or datetime.now()
    year, month0 = divmod(base.year * 12 + (base.month - 1) - (n - 1), 12)
    return datetime(year, month0 + 1, 1).strftime("%Y-%m-%d")
