"""Opt-in SSH reads of one trace, separate from portable fleet summaries."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass

from opentab import paths
from opentab.models import API_SCHEMA_VERSION, SessionRef

TIMEOUT = 30.0
MAX_STDOUT = 16 * 1024 * 1024
MAX_STDERR = 64 * 1024
MAX_TURNS = 100000
MAX_EVENTS = 10000
MAX_KEY = 16384

# Explicit opt-in: a new/unknown harness must never fall through to OpenCode.
TRACE_HARNESSES = {
    "opencode": "opencode",
    "claude": "claude",
    "claude code": "claude",
    "codex": "codex",
    "hermes": "hermes",
    "pi": "pi",
    "omp": "omp",
    "openclaw": "openclaw",
    "zaly": "zaly",
    "gemini": "gemini",
}

# Only fields shared by the portable summary and service.session_turns. In
# particular, neither prompt_full nor cache_write_1h is in the service timeline.
_FIELDS = (
    ("time", "time", str),
    ("model_name", "model", str),
    ("agent", "agent", str),
    ("depth", "depth", int),
    ("effort", "effort", str),
    ("prompt_id", "prompt_id", str),
    ("prompt_title", "prompt_title", str),
    ("cost", "recorded_cost_usd", float),
    ("tokens_total", "tokens", int),
    ("input", "input_tokens", int),
    ("output", "output_tokens", int),
    ("reasoning", "reasoning_tokens", int),
    ("cache_read", "cache_read_tokens", int),
    ("cache_write", "cache_write_tokens", int),
)


class RemoteTraceError(ValueError):
    """Safe to display: never includes remote stderr, payloads, or command arguments."""


class TraceJob:
    """One cancelable transport worker; the UI alone adopts its completed result."""

    def __init__(self, request):
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.content = {}
        self.error = ""

        def run():
            try:
                self.content = request(self.cancelled)
            except RemoteTraceError as exc:
                self.error = str(exc)
            except Exception:  # noqa: BLE001 -- never leak remote data through diagnostics
                self.error = "Could not read remote trace."
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, name="opentab-trace", daemon=True)
        self.thread.start()

    def cancel(self):
        self.cancelled.set()


def trace_preview(events):
    """Apply the local reader's preview budgets without retaining another full copy."""
    from opentab.util import (
        TRACE_ARG_CAP,
        TRACE_ARGS_CAP,
        TRACE_EVENTS_CAP,
        TRACE_OUTPUT_CAP,
        TRACE_TEXT_CAP,
        _fit,
        clip_text,
    )

    preview = []
    for event in events[:TRACE_EVENTS_CAP]:
        row = dict(event)
        if row["kind"] in ("text", "reasoning"):
            row["text"], dropped = clip_text(row.get("text"), TRACE_TEXT_CAP, strip=False)
            row["dropped"] = row.get("dropped", 0) + dropped
        else:
            row["output"], dropped = clip_text(row.get("output"), TRACE_OUTPUT_CAP, strip=False)
            row["output_dropped"] = row.get("output_dropped", 0) + dropped
            # The wire already separates the headline from named arguments.
            row["args"] = _fit(row.get("args", ""), TRACE_ARG_CAP)
            params, budget = [], TRACE_ARGS_CAP
            for name, value in row.get("params", []):
                if budget <= 0:
                    break
                name = name[: min(TRACE_ARG_CAP, budget)]
                value = _fit(value, min(TRACE_ARG_CAP, budget - len(name)))
                params.append((name, value))
                budget -= len(name) + len(value)
            if len(params) < len(row.get("params", [])):
                params.append(("...", "more arguments; expand the turn"))
            row["params"] = params
        preview.append(row)
    return preview


def _json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def invalid_constant(_value):
        raise ValueError("non-finite number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite number")
        return number

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(
            raw, object_pairs_hook=pairs, parse_constant=invalid_constant, parse_float=finite_float
        )
    except (ValueError, UnicodeError, RecursionError):
        raise RemoteTraceError("Remote trace returned invalid JSON.") from None


@dataclass(frozen=True)
class TraceConnection:
    target: str
    command: tuple[str, ...]


def saved_connections() -> dict[str, TraceConnection]:
    """Read only OpenTab's own config, without importing CLI or migrating files."""
    try:
        with open(os.path.join(paths.config_dir(), "remotes.json"), "rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return {}
        data = _json(raw)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        return {}
    machines = data.get("machines")
    if not isinstance(machines, dict):
        return {}
    out = {}
    for name, entry in machines.items():
        if not isinstance(entry, dict) or "url" in entry:
            continue
        target = entry.get("ssh")
        if (
            not isinstance(target, str)
            or not target
            or len(target) > 4096
            or target.startswith("-")
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in target)
            or ("://" in target and not target.startswith("ssh://"))
        ):
            continue
        command = entry.get("trace_cmd")
        if command is None and "trace_cmd" not in entry and "cmd" not in entry:
            command = ["opentab"]
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 64
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)
            or sum(len(arg) for arg in command) > MAX_KEY
            or command[0].startswith("-")
        ):
            continue
        out[name] = TraceConnection(target, tuple(command))
    return out


def turn_identity(row: dict, *, service: bool = False) -> tuple:
    if not isinstance(row, dict):
        raise RemoteTraceError("Remote trace returned an invalid timeline.")
    values = []
    for local, remote, kind in _FIELDS:
        value = row.get(remote if service else local)
        valid = (
            isinstance(value, str)
            if kind is str
            else (isinstance(value, int) and not isinstance(value, bool) and value >= 0)
        )
        if kind is float:
            try:
                valid = (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value >= 0
                )
            except OverflowError:
                valid = False
        if not valid:
            raise RemoteTraceError("Remote trace returned an invalid timeline.")
        values.append(value)
    tools = row.get("tools")
    if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
        raise RemoteTraceError("Remote trace returned an invalid timeline.")
    return (*values, tuple(tools))


@dataclass(frozen=True)
class TraceSnapshot:
    source_path: str
    connection_key: str
    harness: str
    native_id: str
    rows: tuple[tuple, ...]
    digest: str

    def content_key(self, index: int) -> str:
        return f"remote:{self.digest}:{index}"


def trace_snapshot(source_path, connection_key, source, native_id, rows):
    harness = TRACE_HARNESSES.get(str(source).lower())
    if not harness or not rows or len(rows) > MAX_TURNS:
        return None
    try:
        identities = tuple(turn_identity(row) for row in rows)
        encoded = json.dumps(
            [source_path, connection_key, harness, native_id, identities],
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ValueError, OverflowError, RecursionError):
        return None
    return TraceSnapshot(
        source_path,
        connection_key,
        harness,
        native_id,
        identities,
        hashlib.sha256(encoded).hexdigest(),
    )


def _check_cancel(cancel_event, deadline):
    if cancel_event.is_set():
        raise RemoteTraceError("Remote trace cancelled.")
    if time.monotonic() >= deadline:
        raise RemoteTraceError("Remote trace timed out.")


def _ssh_json(connection: TraceConnection, arguments, cancel_event, deadline):
    """Bound both pipes while polling cancellation, including when SSH is silent."""
    _check_cancel(cancel_event, deadline)
    argv = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ConnectionAttempts=1",
        "--",
        connection.target,
        shlex.join((*connection.command, *arguments)),
    ]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except (OSError, ValueError):
        raise RemoteTraceError("Could not start SSH for remote trace.") from None
    oversized = threading.Event()
    failed = threading.Event()
    buffers = [bytearray(), bytearray()]
    finished = [threading.Event(), threading.Event()]

    def drain(index, pipe, limit):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                room = limit - len(buffers[index])
                buffers[index].extend(chunk[:room])
                if len(chunk) > room:
                    oversized.set()
                    break
        except (OSError, ValueError):
            failed.set()
        finally:
            finished[index].set()

    threads = [
        threading.Thread(target=drain, args=(0, process.stdout, MAX_STDOUT), daemon=True),
        threading.Thread(target=drain, args=(1, process.stderr, MAX_STDERR), daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
        while True:
            _check_cancel(cancel_event, deadline)
            if oversized.is_set():
                raise RemoteTraceError("Remote trace response exceeded the size limit.")
            if failed.is_set():
                raise RemoteTraceError("Could not read remote trace response.")
            if process.poll() is not None and all(event.is_set() for event in finished):
                break
            cancel_event.wait(0.025)
        if process.returncode != 0:
            raise RemoteTraceError(
                "Remote trace command failed; check SSH and remote OpenTab setup."
            )
        payload = bytes(buffers[0])
    finally:
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    process.wait(timeout=1.0)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=0.25)
    _check_cancel(cancel_event, deadline)
    return _json(payload)


def _response(payload, snapshot):
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != API_SCHEMA_VERSION
        or payload.get("ok") is not True
        or not isinstance(payload.get("data"), dict)
    ):
        raise RemoteTraceError("Remote trace returned an invalid or unsuccessful response.")
    data = payload["data"]
    key = data.get("session_key")
    try:
        if not isinstance(key, str) or len(key) > MAX_KEY:
            raise ValueError()
        ref = SessionRef.decode(key)
        if ref.harness != snapshot.harness or ref.native_id != snapshot.native_id:
            raise ValueError()
    except (ValueError, RecursionError):
        raise RemoteTraceError("Remote trace session identity did not match.") from None
    return data


def _events(value):
    if not isinstance(value, list) or len(value) > MAX_EVENTS:
        raise RemoteTraceError("Remote trace returned invalid content.")
    for event in value:
        if not isinstance(event, dict):
            raise RemoteTraceError("Remote trace returned invalid content.")
        kind = event.get("kind")
        if kind in ("text", "reasoning"):
            strings, numbers = ("text",), ("dropped",)
            allowed = {"kind", "text", "dropped"}
        elif kind == "tool":
            strings, numbers = ("name", "args", "output", "status"), ("output_dropped",)
            allowed = {"kind", "name", "args", "output", "status", "params", "output_dropped"}
            params = event.get("params", [])
            if not isinstance(params, list) or any(
                not isinstance(pair, list)
                or len(pair) != 2
                or any(not isinstance(item, str) for item in pair)
                for pair in params
            ):
                raise RemoteTraceError("Remote trace returned invalid tool arguments.")
        else:
            raise RemoteTraceError("Remote trace returned an unknown event kind.")
        if (
            set(event) - allowed
            or any(not isinstance(event.get(key, ""), str) for key in strings)
            or any(
                not isinstance(event.get(key, 0), int)
                or isinstance(event.get(key, 0), bool)
                or event.get(key, 0) < 0
                for key in numbers
            )
            or (kind in ("text", "reasoning") and "text" not in event)
            or (kind == "tool" and "name" not in event)
        ):
            raise RemoteTraceError("Remote trace returned invalid event fields.")
    return value


def trace_request(snapshot: TraceSnapshot, connection: TraceConnection, content_key: str):
    """Build a network-free worker from frozen config and accounting identities."""
    if not isinstance(content_key, str) or not content_key.startswith(f"remote:{snapshot.digest}:"):
        raise RemoteTraceError("Remote trace key is stale or invalid; reload the session.")
    try:
        index = int(content_key.rsplit(":", 1)[1])
        if not 0 <= index < len(snapshot.rows) or content_key != snapshot.content_key(index):
            raise ValueError()
    except ValueError:
        raise RemoteTraceError(
            "Remote trace key is stale or invalid; reload the session."
        ) from None
    identity = snapshot.rows[index]
    if not identity[0] or identity[1] == "unknown" or snapshot.rows.count(identity) != 1:
        raise RemoteTraceError("Remote trace turn identity is missing or ambiguous.")

    def request(cancel_event: threading.Event) -> dict:
        deadline = time.monotonic() + TIMEOUT
        flags = ("--source", snapshot.harness, "--allow-raw-content", "--no-state")
        turns = _response(
            _ssh_json(
                connection,
                ("sessions", "turns", *flags, "--include-content-keys", "--", snapshot.native_id),
                cancel_event,
                deadline,
            ),
            snapshot,
        )
        rows = turns.get("turns")
        if not isinstance(rows, list) or len(rows) > MAX_TURNS:
            raise RemoteTraceError("Remote trace returned an invalid timeline.")
        matches = [row for row in rows if turn_identity(row, service=True) == identity]
        if len(matches) != 1:
            raise RemoteTraceError("Remote trace turn is stale or ambiguous; refresh the summary.")
        key = matches[0].get("content_key")
        if not isinstance(key, str) or not key or len(key) > MAX_KEY or "\0" in key:
            raise RemoteTraceError("Remote trace content is unavailable for this turn.")
        if sum(row.get("content_key") == key for row in rows) != 1:
            raise RemoteTraceError("Remote trace content key is ambiguous.")
        _check_cancel(cancel_event, deadline)
        content = _response(
            _ssh_json(
                connection,
                ("sessions", "content", *flags, "--", turns["session_key"], key),
                cancel_event,
                deadline,
            ),
            snapshot,
        )
        if content.get("session_key") != turns["session_key"] or content.get("content_key") != key:
            raise RemoteTraceError("Remote trace content identity did not match.")
        events = _events(content.get("content"))
        _check_cancel(cancel_event, deadline)
        return {content_key: events}

    return request
