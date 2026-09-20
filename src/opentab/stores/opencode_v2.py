"""Connection-local OpenCode v2 compatibility views.

The views preserve the v1 store's query shapes without copying either generation's
records. SQLite expands their JSON only when a detail query actually reads it.
"""
from __future__ import annotations

import re
import sqlite3

REQUIRED_SCHEMA_V2 = {
    "session_v2": (
        "id",
        "parent_id",
        "cost",
        "tokens_input",
        "tokens_output",
        "tokens_reasoning",
        "tokens_cache_read",
        "tokens_cache_write",
        "time_created",
    ),
    "session_message": (
        "id",
        "session_id",
        "type",
        "seq",
        "time_created",
        "time_updated",
        "data",
    ),
}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return []
    return [row[1] for row in conn.execute(f"pragma main.table_info({table})")]


def has_v2(conn: sqlite3.Connection) -> bool:
    return all(
        set(required) <= set(_columns(conn, table))
        for table, required in REQUIRED_SCHEMA_V2.items()
    )


def _projection(columns: list[str], available: set[str], alias: str) -> str:
    return ", ".join(
        f'{alias}."{name}"' if name in available else f'null as "{name}"' for name in columns
    )


def _legacy_session_projection(columns: list[str], available: set[str]) -> str:
    paths = {
        "cost": "$.cost",
        "tokens_input": "$.tokens.input",
        "tokens_output": "$.tokens.output",
        "tokens_reasoning": "$.tokens.reasoning",
        "tokens_cache_read": "$.tokens.cache.read",
        "tokens_cache_write": "$.tokens.cache.write",
    }
    out = []
    for name in columns:
        if name in available:
            out.append(f's."{name}"')
        elif name in paths:
            out.append(
                f"coalesce((select sum(coalesce(json_extract(case when json_valid(m.data) "
                f"then m.data else '{{}}' end, '{paths[name]}'), 0)) "
                "from main.message m where m.session_id = s.id "
                "and json_extract(case when json_valid(m.data) then m.data else '{}' end, "
                "'$.role') = 'assistant'), 0) "
                f'as "{name}"'
            )
        else:
            out.append(f'null as "{name}"')
    return ", ".join(out)


def install_views(conn: sqlite3.Connection) -> bool:
    """Shadow the legacy tables with a v2-first union on this connection."""
    if not has_v2(conn):
        return False

    v2_session = set(_columns(conn, "session_v2"))
    legacy_session = set(_columns(conn, "session"))
    legacy_message = set(_columns(conn, "message"))
    if (
        not {"id", "parent_id", "time_created"} <= legacy_session
        or not {"id", "session_id", "data"} <= legacy_message
    ):
        legacy_session = set()
        legacy_message = set()
    _install_usage_view(conn, bool(legacy_message))
    session_columns = list(v2_session | legacy_session)
    session_columns.sort()
    session_sql = (
        "select " + _projection(session_columns, v2_session, "s") + " from main.session_v2 s"
    )
    if legacy_session:
        session_sql += (
            " union all select "
            + _legacy_session_projection(session_columns, legacy_session)
            + " from main.session s where not exists "
            + "(select 1 from main.session_v2 v where v.id = s.id)"
        )
    conn.execute("create temp view session as " + session_sql)

    legacy_message_sql = ""
    if {"id", "session_id", "data"} <= legacy_message:
        legacy_safe = "case when json_valid(m.data) then m.data else '{}' end"
        legacy_message_sql = f"""
        union all
        select m.rowid as rowid, m.id, m.session_id,
               {('m.time_created' if 'time_created' in legacy_message else f"json_extract({legacy_safe}, '$.time.created')")} as time_created,
               {('m.time_updated' if 'time_updated' in legacy_message else 'null')} as time_updated,
               {('m.parent_id' if 'parent_id' in legacy_message else f"json_extract({legacy_safe}, '$.parentID')")} as parent_id,
               coalesce({('m.time_created' if 'time_created' in legacy_message else f"json_extract({legacy_safe}, '$.time.created')")}, m.rowid) as seq,
               json_extract({legacy_safe}, '$.role') as type,
               m.data
        from main.message m
        where not exists (select 1 from main.session_v2 v where v.id = m.session_id)
        """
    conn.execute(
        "create temp view message as "
        + """
        select m.rowid as rowid, m.id, m.session_id, m.time_created, m.time_updated,
               (select u.id from main.session_message u
                where u.session_id = m.session_id and u.type = 'user' and u.seq < m.seq
                order by u.seq desc limit 1) as parent_id,
                m.seq, m.type,
                json_set(
                  case when json_valid(m.data) then m.data else '{}' end,
                  '$.role', case when not json_valid(m.data) then null
                                 when m.type = 'compaction' then 'assistant' else m.type end,
                  '$.providerID', json_extract(case when json_valid(m.data) then m.data else '{}' end, '$.model.providerID'),
                  '$.modelID', json_extract(case when json_valid(m.data) then m.data else '{}' end, '$.model.id'),
                  '$.variant', json_extract(case when json_valid(m.data) then m.data else '{}' end, '$.model.variant'),
                  '$.time.created', coalesce(json_extract(case when json_valid(m.data) then m.data else '{}' end, '$.time.created'), m.time_created),
                  '$.__opentab_v2', 1,
                  '$.__opentab_type', m.type,
                  '$.__opentab_seq', m.seq
                ) as data
        from main.session_message m
        """
        + legacy_message_sql
    )

    legacy_part = set(_columns(conn, "part"))
    legacy_part_sql = ""
    if {"id", "message_id", "data"} <= legacy_part and {
        "id",
        "session_id",
        "data",
    } <= legacy_message:
        legacy_part_sql = f"""
        union all
        select p.rowid as rowid, p.id, p.message_id,
               {('p.session_id' if 'session_id' in legacy_part else 'm.session_id')} as session_id,
               {('p.time_created' if 'time_created' in legacy_part else "json_extract(case when json_valid(m.data) then m.data else '{}' end, '$.time.created')")} as time_created,
               {('p.time_updated' if 'time_updated' in legacy_part else 'null')} as time_updated,
               p.rowid as part_index, p.data
        from main.part p
        join main.message m on m.id = p.message_id
        where not exists (select 1 from main.session_v2 v where v.id = m.session_id)
        """
    conn.execute(
        "create temp view part as "
        + """
        select -m.rowid as rowid,
               case when json_extract(c.value, '$.type') = 'tool'
                    then coalesce(json_extract(c.value, '$.id'), m.id || ':' || c.key)
                    else m.id || ':' || c.key end as id,
               m.id as message_id, m.session_id,
               coalesce(json_extract(c.value, '$.time.created'), m.time_created) as time_created,
               m.time_updated, cast(c.key as integer) as part_index,
                case when json_extract(c.value, '$.type') = 'tool' then
                  json_set(c.value,
                    '$.tool', json_extract(c.value, '$.name'),
                    '$.__opentab_v2', 1,
                   '$.state.output', coalesce((
                      select group_concat(json_extract(
                        case when output.type = 'object' then output.value else '{}' end,
                        '$.text'), char(10))
                      from json_each(c.value, '$.state.content') output
                      where json_extract(
                        case when output.type = 'object' then output.value else '{}' end,
                        '$.type') = 'text'
                   ), ''),
                   '$.state.error', coalesce(json_extract(c.value, '$.state.error.message'),
                                             json_extract(c.value, '$.state.error'))
                 )
               else c.value end as data
        from main.session_message m
        join json_each(case when json_valid(m.data) then m.data else '{}' end, '$.content') c
        where m.type = 'assistant' and c.type = 'object'
        union all
        select -m.rowid as rowid, m.id || ':user', m.id, m.session_id,
               m.time_created, m.time_updated, -1,
               json_object('type', 'text', 'text',
                 json_extract(case when json_valid(m.data) then m.data else '{}' end, '$.text'),
                 'synthetic', 0)
        from main.session_message m
        where m.type = 'user'
          and json_type(case when json_valid(m.data) then m.data else '{}' end, '$.text') = 'text'
        """
        + legacy_part_sql
    )
    return True


def _install_usage_view(conn: sqlite3.Connection, legacy: bool) -> None:
    """Scalar-only accounting projection; never serialize inline content for rollups."""
    safe = "case when json_valid(m.data) then m.data else '{}' end"
    branches = []
    for native in (True, False) if legacy else (True,):
        role = (
            "case when not json_valid(m.data) then null "
            "when json_type(m.data) != 'object' then null "
            "when m.type = 'compaction' then 'assistant' else m.type end"
            if native
            else f"json_extract({safe}, '$.role')"
        )
        provider = "$.model.providerID" if native else "$.providerID"
        model = "$.model.id" if native else "$.modelID"
        timestamp = f"json_extract({safe}, '$.time.created')"
        if native:
            # Match json_set's fallback: it can create a missing time object,
            # but cannot insert a created field into an existing scalar/array.
            timestamp = (
                f"case when json_type({safe}, '$.time') is null "
                f"or json_type({safe}, '$.time') = 'object' "
                f"then coalesce({timestamp}, m.time_created) end"
            )
        columns = [
            "m.rowid as rowid",
            "m.id",
            "m.session_id",
            f"{role} as role",
            f"{timestamp} as time_created",
            f"coalesce(json_extract({safe}, '{provider}'), 'unknown') || '/' || "
            f"coalesce(json_extract({safe}, '{model}'), 'unknown') as model_name",
        ]
        for name, path in (
            ("cost", "$.cost"),
            ("input", "$.tokens.input"),
            ("output", "$.tokens.output"),
            ("reasoning", "$.tokens.reasoning"),
            ("cache_read", "$.tokens.cache.read"),
            ("cache_write", "$.tokens.cache.write"),
        ):
            columns.append(f"coalesce(json_extract({safe}, '{path}'), 0) as {name}")
        table = "main.session_message" if native else "main.message"
        sql = "select " + ", ".join(columns) + " from " + table + " m"
        if not native:
            sql += " where not exists (select 1 from main.session_v2 v where v.id = m.session_id)"
        branches.append(sql)
    conn.execute("create temp view opentab_message_usage as " + " union all ".join(branches))
