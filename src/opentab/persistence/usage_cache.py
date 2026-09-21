"""OpenTab-owned, indexed scalar sidecar; never opens a source database.

Detail readers load only selected sessions. Refreshes write changed rows in a
transaction. Loaded scalars still undergo source identity/revision validation.
"""
from __future__ import annotations

import json
import os
import sqlite3
from urllib.parse import quote

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


def read(path: str, scope=None):
    conn = sqlite3.connect("file:" + quote(path, safe="/") + "?mode=ro", uri=True, timeout=1)
    try:
        conn.execute("begin")
        row = conn.execute("select data from metadata where id=1").fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        rows = []
        if scope is None:
            rows = conn.execute("select * from usage").fetchall()
        else:
            for sid in sorted(set(scope)):
                rows.extend(conn.execute("select * from usage where session_id=?", [sid]))
        payload["rows"] = [(r[:7], r[7:]) for r in rows]
        return payload
    finally:
        conn.close()


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
            conn.execute("delete from usage where rid not in (select rid from retained)")
            changed = " or ".join(f"usage.{c} is not excluded.{c}" for c in _COLUMNS[1:])
            updates = ",".join(f"{c}=excluded.{c}" for c in _COLUMNS[1:])
            conn.executemany(
                "insert into usage values(" + ",".join("?" for _ in _COLUMNS) + ") "
                "on conflict(rid) do update set " + updates + " where " + changed,
                (tuple(stamp) + tuple(values) for stamp, values in payload["rows"]),
            )
            meta = {k: payload[k] for k in ("version", "identity", "built_at")}
            conn.execute("insert or replace into metadata values(1,?)", [json.dumps(meta)])
    finally:
        conn.close()
