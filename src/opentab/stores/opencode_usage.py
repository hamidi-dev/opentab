"""Revision-keyed numeric accounting, with bounded decoding of inline tool output.

Only CachedStore persists these rows, in OpenTab's own scalar sidecar. Source tables
remain read-only. Metadata is cheap to scan even when data lives on overflow pages.
"""
from __future__ import annotations

import codecs
import json
import os
import re
import sqlite3
import time

from opentab import diagnostics as debug
from opentab.stores.opencode_v2 import usage_columns

# Consume escapes in C, in bounded batches. A Python iteration for every escaped
# quote in an inline transcript can take tens of seconds. An unbounded repeated
# regex instead retains backtracking state proportional to that whole transcript.
_STRING_CHUNK = re.compile(r'(?:[^"\\\x00-\x1f]+|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})){1,4096}')
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_SPACE = re.compile(r"[ \t\r\n]*")
_KEEP = {"role", "providerID", "modelID", "model", "time", "tokens", "cost"}
_FIELDS = ("role", "providerID", "modelID", "model", "time", "tokens", "cost")
_SCALAR_SQL = (
    "select case when typeof(data)='text' and json_valid(data) and json_type(data)='object' "
    "then json_extract(data,"
    + ",".join("'$." + key + "'" for key in _FIELDS)
    + ") end, json_type(case when json_valid(data) then data else '{}' end, '$.time') "
    "from main.session_message where rowid=?"
)
_DECODE_LIMIT = 8 * 1024 * 1024
_MAX_DEPTH = 1000 if sqlite3.sqlite_version_info >= (3, 42, 0) else 2000


def _first_keys(pairs):
    result = {}
    for key, value in pairs:
        if key not in result:
            result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(value)


def _string_end(text: str, start: int) -> int:
    pos = start + 1
    while True:
        match = _STRING_CHUNK.match(text, pos)
        if match is not None:
            pos = match.end()
        if pos < len(text) and text[pos] == '"':
            return pos + 1
        if match is None:
            raise ValueError("invalid string")


def _value_end(text: str, start: int) -> int:
    """Validate one JSON value with O(depth) memory, including skipped content."""
    states = ["value"]
    pos = start
    depth = 1  # The enclosing top-level message object is already open.
    while states:
        pos = _SPACE.match(text, pos).end()
        if pos >= len(text):
            raise ValueError("missing value")
        state = states.pop()
        char = text[pos]
        if state in ("object", "key"):
            if char == "}" and state == "object":
                pos += 1
                depth -= 1
                continue
            if char != '"':
                raise ValueError("missing key")
            pos = _string_end(text, pos)
            pos = _SPACE.match(text, pos).end()
            if text[pos : pos + 1] != ":":
                raise ValueError("missing colon")
            pos += 1
            states.extend(("object-end", "value"))
        elif state in ("object-end", "array-end"):
            end = "}" if state == "object-end" else "]"
            if char == end:
                pos += 1
                depth -= 1
            elif char == ",":
                pos += 1
                states.extend(("array-end", "value") if end == "]" else ("key",))
            else:
                raise ValueError("missing separator")
        elif state == "array" and char == "]":
            pos += 1
            depth -= 1
        else:
            if state == "array":
                states.append("array-end")
            if char == "{":
                states.append("object")
                depth += 1
                pos += 1
            elif char == "[":
                states.append("array")
                depth += 1
                pos += 1
            elif char == '"':
                pos = _string_end(text, pos)
            elif text.startswith(("true", "null"), pos):
                pos += 4
            elif text.startswith("false", pos):
                pos += 5
            else:
                match = _NUMBER.match(text, pos)
                if match is None:
                    raise ValueError("invalid value")
                pos = match.end()
        if depth > _MAX_DEPTH:
            raise ValueError("JSON nesting limit")
    return pos


def compact_usage(text: str) -> str:
    """Keep accounting fields; validate oversized content without decoding it.

    Preserve the first duplicate key, like SQLite's json_extract. The streaming
    path keeps value slices so SQLite retains scalar/type semantics.
    """
    if isinstance(text, str) and len(text) <= _DECODE_LIMIT:
        try:
            data = json.loads(text, object_pairs_hook=_first_keys, parse_constant=_invalid_constant)
            if not isinstance(data, dict):
                return "null"
            return json.dumps(
                {key: value for key, value in data.items() if key in _KEEP}, allow_nan=False
            )
        except (ValueError, RecursionError):
            # The streaming validator also handles valid numbers outside Python's
            # finite float range, retaining their original spelling for SQLite.
            pass
    try:
        pos = _SPACE.match(text).end()
        if text[pos : pos + 1] != "{":
            return "null"
        pos += 1
        fields = {}
        first = True
        while True:
            pos = _SPACE.match(text, pos).end()
            if text[pos : pos + 1] == "}" and first:
                pos += 1
                break
            if text[pos : pos + 1] != '"':
                raise ValueError("missing key")
            end = _string_end(text, pos)
            key = json.loads(text[pos:end])
            pos = _SPACE.match(text, end).end()
            if text[pos : pos + 1] != ":":
                raise ValueError("missing colon")
            start = _SPACE.match(text, pos + 1).end()
            pos = _value_end(text, start)
            if key in _KEEP and key not in fields:
                fields[key] = text[start:pos]
            pos = _SPACE.match(text, pos).end()
            if text[pos : pos + 1] == "}":
                pos += 1
                break
            if text[pos : pos + 1] != ",":
                raise ValueError("missing separator")
            pos += 1
            first = False
        if _SPACE.match(text, pos).end() != len(text):
            raise ValueError("trailing data")
        return "{" + ",".join(json.dumps(k) + ":" + v for k, v in fields.items()) + "}"
    except (ValueError, TypeError, RecursionError):
        return "null"


class _JSONStream:
    """Bounded UTF-8 input and JSON validation; skipped strings are never decoded.

    SQLite's read-only Blob API (Python 3.11+) keeps a giant TEXT cell out of the
    Python/SQLite result buffers. The same validator also accepts small byte chunks
    in tests, including boundaries inside escapes and multibyte characters.
    """

    def __init__(self, read):
        self.read = read
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.text = ""
        self.pos = 0
        self.eof = False
        self.capture = None

    def fill(self, minimum=1):
        if len(self.text) - self.pos >= minimum or self.eof:
            return
        self.text = self.text[self.pos :]
        self.pos = 0
        while len(self.text) < minimum and not self.eof:
            data = self.read(65536)
            self.eof = not data
            self.text += self.decoder.decode(data, final=self.eof)

    def take(self, count):
        if self.capture is not None:
            self.capture.append(self.text[self.pos : self.pos + count])
        self.pos += count

    def peek(self):
        self.fill()
        return self.text[self.pos : self.pos + 1]

    def space(self):
        while True:
            self.fill()
            end = _SPACE.match(self.text, self.pos).end()
            self.take(end - self.pos)
            if end < len(self.text) or self.eof:
                return

    def string(self):
        if self.peek() != '"':
            raise ValueError("missing string")
        self.take(1)
        while True:
            self.fill(6)  # a split Unicode escape needs at most six characters
            match = _STRING_CHUNK.match(self.text, self.pos)
            if match is not None:
                self.take(match.end() - self.pos)
            char = self.peek()
            if char == '"':
                self.take(1)
                return
            if match is None:
                raise ValueError("invalid string")

    def value(self):
        states = ["value"]
        depth = 1
        while states:
            self.space()
            char = self.peek()
            state = states.pop()
            if not char:
                raise ValueError("missing value")
            if state in ("object", "key"):
                if char == "}" and state == "object":
                    self.take(1)
                    depth -= 1
                    continue
                self.string()
                self.space()
                if self.peek() != ":":
                    raise ValueError("missing colon")
                self.take(1)
                states.extend(("object-end", "value"))
            elif state in ("object-end", "array-end"):
                end = "}" if state == "object-end" else "]"
                if char == end:
                    self.take(1)
                    depth -= 1
                elif char == ",":
                    self.take(1)
                    states.extend(("array-end", "value") if end == "]" else ("key",))
                else:
                    raise ValueError("missing separator")
            elif state == "array" and char == "]":
                self.take(1)
                depth -= 1
            else:
                if state == "array":
                    states.append("array-end")
                if char in ("{", "["):
                    states.append("object" if char == "{" else "array")
                    depth += 1
                    self.take(1)
                elif char == '"':
                    self.string()
                elif char in ("t", "f", "n"):
                    literal = {"t": "true", "f": "false", "n": "null"}[char]
                    self.fill(len(literal))
                    if not self.text.startswith(literal, self.pos):
                        raise ValueError("invalid literal")
                    self.take(len(literal))
                else:
                    # Numbers are uncommon in discarded tool output; retain a token
                    # only until its delimiter so exponent/leading-zero rules hold.
                    number = []
                    while self.peek() and self.peek() in "-+0123456789.eE":
                        number.append(self.peek())
                        self.take(1)
                    if not _NUMBER.fullmatch("".join(number)):
                        raise ValueError("invalid number")
            if depth > _MAX_DEPTH:
                raise ValueError("JSON nesting limit")


def compact_usage_stream(read) -> str:
    stream = _JSONStream(read)
    fields = {}
    try:
        stream.space()
        if stream.peek() != "{":
            return "null"
        stream.take(1)
        stream.space()
        if stream.peek() != "}":
            while True:
                stream.capture = []
                stream.string()
                key = json.loads("".join(stream.capture))
                stream.capture = None
                stream.space()
                if stream.peek() != ":":
                    raise ValueError("missing colon")
                stream.take(1)
                keep = key in _KEEP and key not in fields
                stream.capture = [] if keep else None
                stream.value()
                if keep:
                    fields[key] = "".join(stream.capture)
                stream.capture = None
                stream.space()
                if stream.peek() != ",":
                    break
                stream.take(1)
                stream.space()
        if stream.peek() != "}":
            raise ValueError("missing object end")
        stream.take(1)
        stream.space()
        if stream.peek():
            raise ValueError("trailing data")
        return "{" + ",".join(json.dumps(k) + ":" + v for k, v in fields.items()) + "}"
    except (ValueError, UnicodeError):
        return "null"


def _read_usage(conn, rowid, tracing, sql_projection=False):
    """Fetch/project one row; modern runtimes stream oversized TEXT via Blob."""
    started = time.perf_counter() if tracing else 0
    blob = None
    if hasattr(conn, "blobopen"):
        try:
            blob = conn.blobopen("session_message", "data", rowid, readonly=True)
        except sqlite3.OperationalError:
            pass  # NULL/non-blob values retain the SQL projection's old behavior.
    if blob is not None:
        with blob:
            size = len(blob)
            if size > _DECODE_LIMIT:
                fetched = time.perf_counter() if tracing else 0
                with debug.span(
                    "usage.large_row",
                    row=debug.identity(str(rowid)),
                    input_size=size,
                    size_unit="bytes",
                    streamed=True,
                ):
                    compact = compact_usage_stream(blob.read)
                return compact, size, (fetched - started) * 1000, True, False
        if sql_projection:
            # A single multi-path extraction keeps discarded content out of Python.
            # Only bounded cells use SQLite's JSON parser; oversized cells retain
            # streaming validation. Preserve missing versus explicit-null time:
            # only an absent time object allows the native timestamp fallback.
            try:
                raw, time_type = conn.execute(_SCALAR_SQL, [rowid]).fetchone()
                fetched = time.perf_counter() if tracing else 0
                if raw is None:
                    compact = "null"
                else:
                    fields = dict(
                        zip(
                            _FIELDS,
                            json.loads(
                                raw,
                                object_pairs_hook=_first_keys,
                                parse_constant=_invalid_constant,
                            ),
                        )
                    )
                    if time_type is None:
                        fields.pop("time")
                    compact = json.dumps(fields, allow_nan=False)
                return compact, size, (fetched - started) * 1000, False, True
            except (sqlite3.OperationalError, ValueError, UnicodeError, RecursionError):
                # Non-finite numbers and runtime-specific JSON/UTF handling keep
                # the established full-read validator rather than losing usage.
                pass
    data = conn.execute("select data from main.session_message where rowid=?", [rowid]).fetchone()[
        0
    ]
    fetched = time.perf_counter() if tracing else 0
    size = len(data) if isinstance(data, (str, bytes)) else 0
    if size > _DECODE_LIMIT:
        with debug.span(
            "usage.large_row",
            row=debug.identity(str(rowid)),
            input_size=size,
            size_unit="characters" if isinstance(data, str) else "bytes",
            streamed=False,
        ):
            compact = compact_usage(data)
    else:
        compact = compact_usage(data)
    return compact, size, (fetched - started) * 1000, False, False


class UsageCache:
    """One bounded set of numeric rows, replaced on each refresh (never raw text)."""

    def __init__(self, db: str):
        self.db = db
        self.rows: dict[int, tuple] = {}
        self.ready = False
        self.scope = None
        self.data_version = None
        self.built_at = 0
        self.reused = 0
        self.changed = True
        self._sql_projection = None

    def identity(self) -> list:
        stat = os.stat(self.db)
        return [os.path.realpath(self.db), stat.st_dev, stat.st_ino]

    @debug.timed("usage.restore")
    def restore(self, payload) -> None:
        # A rejected disk snapshot must not evict a newer in-process projection.
        # A subsequent refresh still validates every retained row's revision.
        if not isinstance(payload, dict) or payload.get("version") != 1:
            debug.event("usage.restore_rejected", reason="missing_or_version")
            return
        if payload.get("identity") != self.identity():
            debug.event("usage.restore_rejected", reason="source_identity_changed")
            return
        if not isinstance(payload.get("built_at"), (int, float)):
            debug.event("usage.restore_rejected", reason="invalid_build_time")
            return
        try:
            rows = {}
            for stamp, values in payload["rows"]:
                if len(stamp) != 7 or len(values) != 12 or not isinstance(stamp[0], int):
                    debug.event("usage.restore_rejected", reason="row_shape")
                    return
                if any(not isinstance(x, (str, int, float, type(None))) for x in stamp + values):
                    debug.event("usage.restore_rejected", reason="row_types")
                    return
                if list(stamp[:3]) != list(values[:3]) or not isinstance(values[5], str):
                    debug.event("usage.restore_rejected", reason="row_identity")
                    return
                if stamp[0] in rows:
                    debug.event("usage.restore_rejected", reason="duplicate_row")
                    return
                rows[stamp[0]] = (tuple(stamp), tuple(values))
            if not self.ready or self.scope is not None:
                self.rows = rows
                self.built_at = payload["built_at"]
            debug.event("usage.restored", rows=len(rows))
        except (KeyError, TypeError, ValueError):
            debug.event("usage.restore_rejected", reason="invalid_payload")
            return

    def export(self) -> dict:
        return {
            "version": 1,
            "identity": self.identity(),
            "built_at": self.built_at,
            "rows": list(self.rows.values()),
        }

    @debug.timed("usage.prepare")
    def prepare(
        self, conn: sqlite3.Connection, legacy: bool, refresh: bool = False, scope=None
    ) -> None:
        scope = None if scope is None else frozenset(scope)
        version = conn.execute("pragma data_version").fetchone()[0]
        if (
            self.ready
            and not refresh
            and self.data_version == version
            and (self.scope is None or self.scope == scope)
        ):
            debug.event("usage.decision", result="memory_hit", rows=len(self.rows))
            return
        debug.event(
            "usage.decision",
            result="refresh",
            force=refresh,
            ready=self.ready,
            data_version_changed=self.data_version != version,
            scope="all" if scope is None else "subtree",
            scope_sessions=len(scope) if scope is not None else None,
            previous_rows=len(self.rows),
        )
        tracing = debug.enabled()
        if self._sql_projection is None:
            # Older JSON1 engines cannot match escaped object keys as Python does.
            # Probe the actual library, not the Python version or a new minimum.
            self._sql_projection = (
                hasattr(conn, "blobopen")
                and conn.execute(
                    "select json_extract(?, '$.model')", ['{"\\u006dodel":1}']
                ).fetchone()[0]
                == 1
            )
        debug.event(
            "usage.decode_strategy",
            streamed_oversized=hasattr(conn, "blobopen"),
            compact_threshold=_DECODE_LIMIT,
            legacy_reread=legacy,
            sql_scalar_projection=self._sql_projection,
        )
        fetch_ms = decode_ms = project_ms = insert_ms = 0.0
        decoded = large = legacy_rows = 0
        streamed_rows = unusable_rows = projected_rows = 0
        reread_reasons = {} if tracing else None
        started = time.time() * 1000
        cutoff = min(started, self.built_at) - 2000
        params = sorted(scope) if scope is not None else []
        scoped = (
            " where session_id in (" + ",".join("?" for _ in params) + ")"
            if scope is not None
            else ""
        )
        # One read snapshot covers revisions and the changed payloads they identify.
        conn.execute("savepoint opentab_usage_refresh")
        try:
            with debug.span("usage.reset_table"):
                if self.ready:
                    conn.execute("delete from temp.opentab_message_usage")
                else:
                    conn.execute("drop view temp.opentab_message_usage")
                    conn.execute(
                        "create temp table opentab_message_usage (rowid, id, session_id, role, "
                        "time_created, model_name, cost, input, output, reasoning, cache_read, cache_write)"
                    )
            fresh = {}
            pending = []
            reused = 0
            projection = (
                "select "
                + ",".join(usage_columns(True))
                + " from (select ? as rowid, ? as id, ? as session_id, ? as type, ? as time_created, ? as data) m"
            )
            # No data/JSON/length(data) here: don't visit overflow pages on a hit.
            for row in debug.query_rows(
                conn,
                "select rowid,id,session_id,type,time_created,time_updated,seq from main.session_message"
                + scoped,
                params,
                label="usage.native_metadata",
            ):
                stamp = tuple(row)
                old = self.rows.get(stamp[0])
                if (
                    old is not None
                    and old[0] == stamp
                    and isinstance(stamp[5], (int, float))
                    and 0 < stamp[5] < cutoff
                ):
                    values = old[1]
                    reused += 1
                else:
                    if tracing:
                        reason = (
                            "not_cached"
                            if old is None
                            else "revision_changed"
                            if old[0] != stamp
                            else "unreliable_revision"
                            if not isinstance(stamp[5], (int, float)) or stamp[5] <= 0
                            else "recent_or_future_revision"
                        )
                        reread_reasons[reason] = reread_reasons.get(reason, 0) + 1
                    tick = time.perf_counter() if tracing else 0
                    compact, size, fetched_ms, streamed, projected = _read_usage(
                        conn, stamp[0], tracing, self._sql_projection
                    )
                    if tracing:
                        now = time.perf_counter()
                        elapsed = (now - tick) * 1000
                        fetch_ms += fetched_ms
                        decode_ms += elapsed - fetched_ms
                        if size > _DECODE_LIMIT or elapsed >= 100:
                            debug.event(
                                "usage.slow_row",
                                row=debug.identity(str(stamp[0])),
                                input_size=size,
                                streamed=streamed,
                                sql_projected=projected,
                                fetch_ms=round(fetched_ms, 3),
                                decode_ms=round(elapsed - fetched_ms, 3),
                            )
                        tick = now
                        large += size > _DECODE_LIMIT
                        streamed_rows += streamed
                        projected_rows += projected
                        unusable_rows += compact == "null"
                    values = tuple(conn.execute(projection, (*stamp[:5], compact)).fetchone())
                    if tracing:
                        project_ms += (time.perf_counter() - tick) * 1000
                        decoded += 1
                fresh[stamp[0]] = (stamp, values)
                pending.append(values)
                if len(pending) == 1024:
                    tick = time.perf_counter() if tracing else 0
                    conn.executemany(
                        "insert into temp.opentab_message_usage values (?,?,?,?,?,?,?,?,?,?,?,?)",
                        pending,
                    )
                    pending.clear()
                    if tracing:
                        insert_ms += (time.perf_counter() - tick) * 1000
                if tracing:
                    if len(fresh) % 1024 == 0:
                        debug.event(
                            "usage.progress", rows=len(fresh), reused=reused, decoded=decoded
                        )
            tick = time.perf_counter() if tracing else 0
            conn.executemany(
                "insert into temp.opentab_message_usage values (?,?,?,?,?,?,?,?,?,?,?,?)", pending
            )
            if tracing:
                insert_ms += (time.perf_counter() - tick) * 1000
                debug.event(
                    "usage.native_summary",
                    reread_reasons=reread_reasons,
                    rows=len(fresh),
                    reused=reused,
                    decoded=decoded,
                    oversized=large,
                    streamed=streamed_rows,
                    sql_projected=projected_rows,
                    python_full_decode=decoded - streamed_rows - projected_rows,
                    unusable=unusable_rows,
                    payload_fetch_ms=round(fetch_ms, 3),
                    decode_ms=round(decode_ms, 3),
                    project_ms=round(project_ms, 3),
                    insert_ms=round(insert_ms, 3),
                )
            if legacy:
                # Old schemas may lack revision columns; never reuse their JSON by guesswork.
                projection = (
                    "select "
                    + ",".join(usage_columns(False))
                    + " from (select ? as rowid, ? as id, ? as session_id, ? as data) m"
                )
                for row in debug.query_rows(
                    conn,
                    "select m.rowid,m.id,m.session_id,m.data from main.message m "
                    "where not exists (select 1 from main.session_v2 v where v.id=m.session_id)"
                    + (
                        " and m.session_id in (" + ",".join("?" for _ in params) + ")"
                        if scope is not None
                        else ""
                    ),
                    params,
                    label="usage.legacy_rows",
                ):
                    legacy_rows += 1
                    values = tuple(
                        conn.execute(
                            projection, (*tuple(row)[:3], compact_usage(row[3]))
                        ).fetchone()
                    )
                    conn.execute(
                        "insert into temp.opentab_message_usage values (?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
            if not self.ready:
                # Source schemas remain read-only. Index only our compact temp
                # rows so 23 subagents don't each scan all 55k accounting rows.
                with debug.span("usage.index_table"):
                    conn.execute(
                        "create index temp.opentab_usage_session on opentab_message_usage(session_id)"
                    )
                    conn.execute("create index temp.opentab_usage_id on opentab_message_usage(id)")
            conn.execute("release opentab_usage_refresh")
            self.changed = (
                self.built_at <= 0 or reused != len(fresh) or len(fresh) != len(self.rows)
            )
            self.rows = fresh
            self.ready = True
            self.scope = scope
            self.data_version = version
            self.built_at = started
            self.reused = reused
            debug.event(
                "usage.ready",
                native_rows=len(fresh),
                reused=reused,
                legacy_rows=legacy_rows,
                accounting_changed=self.changed,
                scope="all" if scope is None else "subtree",
            )
        except BaseException:
            debug.event("usage.rollback", reason="refresh_failed")
            conn.execute("rollback to opentab_usage_refresh")
            conn.execute("release opentab_usage_refresh")
            raise
