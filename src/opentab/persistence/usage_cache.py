"""OpenTab-owned, indexed scalar sidecar; never opens a source database.

Detail readers load only selected sessions. Refreshes write changed rows in a
transaction. Loaded scalars still undergo source identity/revision validation.
"""
from __future__ import annotations

import json
import os
import sqlite3
from urllib.parse import quote

from opentab import diagnostics as debug

_COLUMNS = (
    "rid",
    "id",
    "session_id",
    "kind",
    "created",
    "updated",
    "seq",
    "usage_rid",
    "usage_id",
    "usage_session_id",
    "role",
    "event_time",
    "model",
    "cost",
    "input",
    "output",
    "reasoning",
    "cache_read",
    "cache_write",
)


@debug.timed("usage.sidecar_read")
def read(path: str, scope=None):
    debug.event(
        "usage.sidecar_scope",
        scope="all" if scope is None else "subtree",
    )
    conn = sqlite3.connect("file:" + quote(path, safe="/") + "?mode=ro", uri=True, timeout=1)
    try:
        conn.execute("begin")
        row = conn.execute("select data from metadata where id=1").fetchone()
        if row is None:
            debug.event("usage.sidecar_rejected", reason="missing_metadata")
            return None
        payload = json.loads(row[0])
        if not isinstance(payload, dict) or payload.get("version") != 1:
            debug.event("usage.sidecar_rejected", reason="metadata_version_or_shape")
            return None
        rows = []
        with debug.span("usage.sidecar_rows") as info:
            if scope is None:
                rows = conn.execute("select * from usage").fetchall()
            else:
                sessions = sorted(set(scope))
                info["sessions"] = len(sessions)
                for sid in sessions:
                    rows.extend(conn.execute("select * from usage where session_id=?", [sid]))
            info["rows"] = len(rows)
        with debug.span("usage.sidecar_decode") as info:
            payload["rows"] = [(r[:7], r[7:]) for r in rows]
            info["rows"] = len(rows)
        return payload
    finally:
        conn.close()


@debug.timed("usage.sidecar_write")
def write(path: str, payload: dict) -> None:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    conn = sqlite3.connect(path, timeout=1)
    try:
        with conn:
            conn.execute("create table if not exists metadata(id integer primary key, data text)")
            conn.execute(
                "create table if not exists usage(rid integer primary key,"
                + ",".join(_COLUMNS[1:])
                + ")"
            )
            conn.execute("create index if not exists usage_session on usage(session_id)")
            conn.execute("create temp table retained(rid integer primary key)")
            conn.executemany(
                "insert into retained values(?)", ((stamp[0],) for stamp, values in payload["rows"])
            )
            deleted = conn.execute(
                "delete from usage where rid not in (select rid from retained)"
            ).rowcount
            changed = " or ".join(f"usage.{c} is not excluded.{c}" for c in _COLUMNS[1:])
            updates = ",".join(f"{c}=excluded.{c}" for c in _COLUMNS[1:])
            updated = conn.executemany(
                "insert into usage values(" + ",".join("?" for _ in _COLUMNS) + ") "
                "on conflict(rid) do update set " + updates + " where " + changed,
                (tuple(stamp) + tuple(values) for stamp, values in payload["rows"]),
            ).rowcount
            meta = {k: payload[k] for k in ("version", "identity", "built_at")}
            conn.execute("insert or replace into metadata values(1,?)", [json.dumps(meta)])
        debug.event(
            "usage.sidecar_written",
            rows=len(payload["rows"]),
            upserted=updated,
            deleted=deleted,
            unchanged=len(payload["rows"]) - updated,
        )
    finally:
        conn.close()
