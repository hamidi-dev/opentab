"""OpenCode SQLite backend (read-only)."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import ntpath
import os
import posixpath
import re
import sqlite3
import threading
import time
from contextlib import closing
from urllib.parse import quote

from opentab import diagnostics as debug
from opentab.accounting.models import Workflow
from opentab.demo import demo_config, scramble_node, scramble_workflow
from opentab.presentation.formatting import WORKED_BURST_GAP_SECONDS, _clean_prompt
from opentab.stores.opencode_usage import UsageCache
from opentab.stores.opencode_v2 import REQUIRED_SCHEMA_V2 as REQUIRED_SCHEMA_V2
from opentab.stores.opencode_v2 import install_views, scoped_detail_sql
from opentab.util import (
    TRACE_OUTPUT_CAP,
    TRACE_TEXT_CAP,
    TraceContent,
    normalize_project_path,
)

MODEL_EXPR = """
case
  when s.model is null or s.model = '' then 'unknown (not recorded)'
  else coalesce(json_extract(s.model, '$.providerID'), 'unknown') || '/' || coalesce(json_extract(s.model, '$.id'), 'unknown') ||
    case
      when coalesce(json_extract(s.model, '$.variant'), 'default') not in ('', 'default')
      then ' (' || json_extract(s.model, '$.variant') || ')'
      else ''
    end
end
"""

# session.model is incomplete and cannot represent model switches; use message data.
MSG_MODEL_EXPR = (
    "coalesce(json_extract(m.data, '$.providerID'), 'unknown') || '/' || "
    "coalesce(json_extract(m.data, '$.modelID'), 'unknown')"
)
MSG_TOKEN_TOTAL_EXPR = " + ".join(
    [
        "coalesce(json_extract(m.data, '$.tokens.input'), 0)",
        "coalesce(json_extract(m.data, '$.tokens.output'), 0)",
        "coalesce(json_extract(m.data, '$.tokens.reasoning'), 0)",
        "coalesce(json_extract(m.data, '$.tokens.cache.read'), 0)",
        "coalesce(json_extract(m.data, '$.tokens.cache.write'), 0)",
    ]
)
# Epoch milliseconds available across schema versions.
_TL_TS = "json_extract(m.data, '$.time.created')"

# Only require columns used unconditionally; optional schema features are probed.
REQUIRED_SCHEMA = {
    "session": ("id", "parent_id", "time_created"),
    "message": ("id", "session_id", "data"),
}

CONVERSATION_TEXT_BUDGET = 256 * 1024 * 1024

CHANGE_SUMMARY_LIMIT = 2000
CHANGE_SUMMARY_BYTES = 1024 * 1024
CHANGE_METADATA_BYTES = 4096
CHANGE_DIFF_BYTES = 1024 * 1024
CHANGE_LEGACY_INPUT_BYTES = 256 * 1024
CHANGE_LEGACY_LINE_LIMIT = 2000
CHANGE_LIMITATIONS = [
    "Tool records and snapshots are both retained; snapshots may include concurrent edits.",
    "Reverted changes remain in retained history.",
    "Shell/formatter changes and missing metadata may not be captured.",
]


def _process_timeline(
    rows: list[dict],
    tools: dict[str, list[str]] | None = None,
    reads: dict[str, tuple[bool, bool]] | None = None,
) -> list[dict]:
    # Assign each assistant row to the latest root user prompt. Tools arrive from the
    # separate grouped part-table scan; see _timeline_tools.
    out: list[dict] = []
    cur_id, cur_title, cur_full = "", "", ""
    for d in rows:
        if d["role"] == "user":
            # Subagent user rows are task instructions, not human prompt boundaries.
            if d["depth"]:
                continue
            cur_id = d["mid"] or ""
            cur_title = _clean_prompt(d["summary_title"] or d["prompt_text"])
            # Preserve raw prompt text for expansion; summary is fallback only.
            cur_full = str(d["prompt_text"] or d["summary_title"] or "").strip()
            continue
        message_tools = (tools or {}).get(d["mid"], [])
        # Keep recorded tool calls visible even if their step has no usage; otherwise
        # Tools rankings count calls that disappear from the per-call drill.
        if not (d["tokens_total"] or d["cost"] or message_tools):
            continue
        d["time"] = d["time"] or ""
        d["prompt_id"] = cur_id
        d["prompt_title"] = cur_title
        d["prompt_full"] = cur_full
        d["tools"] = message_tools
        # The message id is what the part table joins on, so it is also the turn's
        # identity for the trace. Set before mid is dropped below.
        d["content_key"] = d["mid"] or ""
        d["has_text"], d["has_reasoning"] = (reads or {}).get(d["mid"], (False, False))
        for k in ("role", "mid", "summary_title", "prompt_text"):
            del d[k]
        out.append(d)
    return out


class _ChangeRequest:
    """A frozen worker request that owns every SQLite connection it opens."""

    def __init__(self, db: str, root_id: str, change_key: str | None):
        self.db = db
        self.root_id = root_id
        self.change_key = change_key

    @debug.timed("opencode.changes_worker")
    def __call__(self, cancelled: threading.Event):
        debug.event(
            "opencode.changes_request",
            session=debug.identity(self.root_id),
            kind="files" if self.change_key is None else "diff",
        )
        if cancelled.is_set():
            debug.event("opencode.changes_outcome", result="cancelled_before_open")
            return None
        store = Store(self.db, argparse.Namespace(demo=False))
        store._change_cancelled = cancelled
        store.conn.set_progress_handler(cancelled.is_set, 1000)
        try:
            if self.change_key is None:
                result = store.session_change_files(self.root_id)
            else:
                result = store.session_change_diff(self.root_id, self.change_key)
            debug.event(
                "opencode.changes_outcome",
                result="cancelled"
                if cancelled.is_set()
                else "ready"
                if result is not None
                else "unavailable",
            )
            return result
        except Exception as exc:
            debug.event(
                "opencode.changes_outcome",
                result="cancelled" if cancelled.is_set() else "error",
                error_type=type(exc).__name__,
            )
            raise
        finally:
            store.conn.close()


class Store:
    has_v2 = False
    _usage_cache = None
    records_cost = True
    combined = False
    source_name = "OpenCode"
    # OpenCode is the one backend of the three that keeps reasoning PROSE rather
    # than an empty block: 19,324 of 23,298 reasoning parts non-empty (measured).
    records_reasoning = True

    @debug.timed("opencode.open")
    def __init__(self, db: str, args: argparse.Namespace):
        self.db = db
        self.args = args
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._change_cancelled: threading.Event | None = None
        # Enforce the read-only database contract at connection level.
        uri = "file:" + quote(os.path.abspath(db)) + "?mode=ro"
        # CombinedStore may move this connection between threads, never use it concurrently.
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._tune(self.conn)
        self.has_v2 = install_views(self.conn)
        self._usage_cache = UsageCache(db) if self.has_v2 else None
        self.session_columns = self._table_columns("session")
        self.message_columns = self._table_columns("message")
        self.supports_tool_breakdown = self._table_exists("part")
        self.part_columns = self._table_columns("part") if self.supports_tool_breakdown else set()
        self._legacy_part_columns = (
            {row["name"] for row in self.conn.execute("pragma main.table_info(part)")}
            if self.has_v2
            else set()
        )
        self.supports_message_timeline = self._table_exists("message")
        self.supports_session_changes = {"id", "parent_id"} <= self.session_columns and {
            "id",
            "session_id",
            "data",
        } <= self.message_columns
        if debug.enabled():
            debug.event(
                "opencode.schema",
                source=debug.identity(os.path.abspath(db)),
                v2=self.has_v2,
                legacy_usage=self._legacy_usage_available() if self.has_v2 else True,
                streamed_usage=hasattr(self.conn, "blobopen"),
                changes=self.supports_session_changes,
            )

    @staticmethod
    def _tune(conn: sqlite3.Connection) -> None:
        # Bound source pages per reader instead of retaining gigabytes of inline
        # output through mappings/page caches. V2 refreshes reuse compact numeric
        # rows; the remaining GROUP BY temp b-trees can stay in memory. These are
        # connection-local read settings, not changes to the source database.
        for pragma in (
            "mmap_size = 67108864",  # bound resident source mappings across long-lived readers
            "cache_size = -16384",  # 16 MiB; numeric rollups don't need a raw-JSON page cache
            "temp_store = memory",
        ):
            try:
                conn.execute(f"pragma {pragma}")
            except sqlite3.Error:
                pass  # best-effort; a missing pragma must never block launch

    def _table_columns(self, table: str) -> set[str]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError(f"invalid table name: {table}")
        return {row["name"] for row in self.conn.execute(f"pragma table_info({table})")}

    def _table_exists(self, table: str) -> bool:
        return bool(
            self.conn.execute(
                "select 1 from sqlite_master where type='table' and name=? "
                "union all select 1 from sqlite_temp_master where type='view' and name=? limit 1",
                [table, table],
            ).fetchone()
        )

    def _has_session_token_columns(self) -> bool:
        return {
            "tokens_input",
            "tokens_output",
            "tokens_reasoning",
            "tokens_cache_read",
            "tokens_cache_write",
        }.issubset(self.session_columns)

    def _has_session_cost_column(self) -> bool:
        return "cost" in self.session_columns

    def _needs_message_usage(self) -> bool:
        return not self._has_session_token_columns() or not self._has_session_cost_column()

    def _message_usage_cte(self) -> str:
        if not self._needs_message_usage():
            return ""
        return """
        , msg_usage as (
          select
            session_id,
            sum(coalesce(json_extract(data, '$.tokens.input'), 0)) as tokens_input,
            sum(coalesce(json_extract(data, '$.tokens.output'), 0)) as tokens_output,
            sum(coalesce(json_extract(data, '$.tokens.reasoning'), 0)) as tokens_reasoning,
            sum(coalesce(json_extract(data, '$.tokens.cache.read'), 0)) as tokens_cache_read,
            sum(coalesce(json_extract(data, '$.tokens.cache.write'), 0)) as tokens_cache_write,
            sum(
              coalesce(json_extract(data, '$.tokens.input'), 0) +
              coalesce(json_extract(data, '$.tokens.output'), 0) +
              coalesce(json_extract(data, '$.tokens.reasoning'), 0) +
              coalesce(json_extract(data, '$.tokens.cache.read'), 0) +
              coalesce(json_extract(data, '$.tokens.cache.write'), 0)
            ) as tokens_total,
            sum(coalesce(json_extract(data, '$.cost'), 0)) as cost
          from message
          where json_extract(data, '$.role') = 'assistant'
          group by session_id
        )
        """

    def _message_usage_join(self) -> str:
        if not self._needs_message_usage():
            return ""
        return "left join msg_usage mu on mu.session_id = s.id"

    def _cost_expr(self, session_alias: str = "s", aggregate_alias: str = "mu") -> str:
        if self._has_session_cost_column():
            return f"coalesce({session_alias}.cost, 0)"
        return f"coalesce({aggregate_alias}.cost, 0)"

    def _token_exprs(self, session_alias: str = "s", aggregate_alias: str = "mu") -> dict[str, str]:
        names = (
            "tokens_input",
            "tokens_output",
            "tokens_reasoning",
            "tokens_cache_read",
            "tokens_cache_write",
        )
        if self._has_session_token_columns():
            exprs = {name: f"coalesce({session_alias}.{name}, 0)" for name in names}
            exprs["tokens_total"] = " + ".join(exprs[name] for name in names)
            return exprs
        exprs = {name: f"coalesce({aggregate_alias}.{name}, 0)" for name in names}
        exprs["tokens_total"] = f"coalesce({aggregate_alias}.tokens_total, 0)"
        return exprs

    def _session_text_expr(self, alias: str, columns: list[str], fallback: str) -> str:
        parts = [
            f"nullif({alias}.{column}, '')" for column in columns if column in self.session_columns
        ]
        if not parts:
            return fallback
        return f"coalesce({', '.join(parts)}, {fallback})"

    def cache_inputs(self) -> list[str]:
        # The DB file whose (size, mtime) fingerprints the warm-start cache, plus its
        # WAL sidecars. OpenCode runs SQLite in WAL mode, so new sessions land in
        # <db>-wal and the main .db's size/mtime don't move until a checkpoint -- so
        # fingerprinting the .db alone made a reload (r, or the browser's refresh) serve
        # the stale cache and never show sessions written since. The sidecars move on
        # every commit; a read-only connection still reads them, so a re-parse sees the
        # new rows. Missing sidecars (a non-WAL DB, or a checkpoint that removed them)
        # are simply skipped by the fingerprint's stat().
        db = os.path.abspath(self.db)
        return [db, db + "-wal", db + "-shm"]

    @debug.timed("opencode.workflows")
    def workflows(self) -> list[Workflow]:
        if self._usage_cache is not None:
            self._usage_cache.prepare(
                self.conn,
                self._legacy_usage_available(),
                refresh=True,
            )
        # Load every root session; the App filters by the active range in memory
        # so the range can be changed live without re-querying.
        token_exprs = self._token_exprs()
        cost_expr = self._cost_expr()
        title_expr = self._session_text_expr("root", ["title"], "'(untitled)'")
        directory_expr = self._session_text_expr("root", ["directory", "path"], "'(unknown)'")
        # Last activity anywhere in the subtree (a subagent still streaming bumps its
        # root) -- the same coalesce recent_roots uses, over the tree this query
        # already walks. Without a time_updated column the latest child *creation*
        # still beats nothing.
        if "time_updated" in self.session_columns:
            ended_expr = "coalesce(s.time_updated, s.time_created)"
        else:
            ended_expr = "s.time_created"
        # Active working time (idle excluded): walk the tree's user+assistant messages
        # in time order and sum each gap EXCEPT one landing on a HUMAN prompt or beyond
        # the shared activity window. Only a depth-0 (root) `user` message is a human
        # turn; a subagent's `user` message is the agent-authored task, so a gap into it
        # is work. On a timestamp tie, human rows are ordered first (is_human desc) so
        # the gap-into-a-prompt is the one that drops. Keep this SQL identical to
        # formatting.worked_seconds.
        # Needs the message table (per-message times); without it, worked stays null.
        if self.supports_message_timeline:
            message_table = "opentab_message_usage" if self.has_v2 else "message"
            event_time = "m.time_created" if self.has_v2 else _TL_TS
            event_role = "m.role" if self.has_v2 else "json_extract(m.data, '$.role')"
            worked_cte = f"""
        , msg_events as (
          select tree.root_id as root_id, {event_time} as t_ms,
                 ({event_role} = 'user' and tree.depth = 0) as is_human,
                 m.rowid as rid
          from {message_table} m
          join tree on tree.id = m.session_id
          where {event_role} in ('user', 'assistant')
            and {event_time} is not null
        ), worked as (
          select root_id,
                 sum(case
                       when is_human or t_ms - prev_ms > {WORKED_BURST_GAP_SECONDS * 1000}
                       then 0 else t_ms - prev_ms
                     end) as worked_ms
          from (
            select root_id, t_ms, is_human,
                   lag(t_ms) over (
                     partition by root_id order by t_ms, is_human desc, rid
                   ) as prev_ms
            from msg_events
          )
          where prev_ms is not null
          group by root_id
        )"""
            worked_select = "worked.worked_ms / 1000.0 as worked_seconds"
            worked_join = "left join worked on worked.root_id = rollup.root_id"
        else:
            worked_cte = ""
            worked_select = "null as worked_seconds"
            worked_join = ""
        sql = f"""
        with recursive roots(id) as (
          select root.id
          from session root
          where root.parent_id is null
        ), tree(root_id, id, depth) as (
          select id, id, 0 from roots
          union all
          select tree.root_id, child.id, tree.depth + 1
          from session child
          join tree on child.parent_id = tree.id
        )
        {self._message_usage_cte()}
        , nodes as (
          select
            tree.root_id,
            tree.depth,
            s.*,
            {ended_expr} as node_ended,
            {token_exprs['tokens_total']} as tokens_total,
            {cost_expr} as node_cost
          from session s
          join tree on tree.id = s.id
          {self._message_usage_join()}
        ), rollup as (
          select
            root_id,
            sum(node_cost) as total_cost,
            sum(case when depth = 0 then node_cost else 0 end) as root_cost,
            sum(tokens_total) as total_tokens,
            sum(case when depth > 0 then 1 else 0 end) as subagents,
            sum(case when node_cost = 0 then tokens_total else 0 end) as unpriced_tokens,
            max(node_ended) as ended_ms
          from nodes
          group by root_id
        ){worked_cte}
        select
          root.id,
          {title_expr} as title,
          {directory_expr} as directory,
          datetime(root.time_created / 1000, 'unixepoch', 'localtime') as created_at,
          rollup.root_cost,
          rollup.total_cost,
          rollup.subagents,
          0 as model_count,  -- filled in by App._load_model_cache from model_breakdown
          rollup.total_tokens,
          rollup.unpriced_tokens,
          coalesce(datetime(rollup.ended_ms / 1000, 'unixepoch', 'localtime'), '') as ended_at,
          {worked_select}
        from rollup
        join session root on root.id = rollup.root_id
        {worked_join}
        order by rollup.total_cost desc, rollup.total_tokens desc
        """
        rows = [Workflow(**dict(row)) for row in self.conn.execute(sql)]
        if "time_updated" not in self.session_columns:
            # Legacy schema: the end was inferred from creation times alone. A tree's
            # latest child creation still teaches something; a flat session's doesn't
            # -- blank it so the UI shows "unknown", never a fake 0s.
            for w in rows:
                if w.ended_at == w.created_at:
                    w.ended_at = ""
        for w in rows:
            w.source = self.source_name
            # OpenCode stores forward-slash Windows paths (C:/DEV/app); fold them to
            # the native C:\DEV\app spelling so a project shared with a backslash
            # backend (Pi, Claude, ...) groups as one, not two (issue #4).
            w.directory = normalize_project_path(w.directory)
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        return rows

    def _demo_workflow(self, w: Workflow) -> Workflow:
        # guard_root: OpenCode's root_cost is really priced, so only backfill it when
        # it was $0 (the all-unpriced backends have no such guard). See scramble_workflow.
        return scramble_workflow(w, self.demo_scale, self.demo_cats, guard_root=True)

    def summary(self, workflows: list[Workflow]) -> dict[str, int | float]:
        return {
            "workflows": len(workflows),
            "cost": sum(w.total_cost for w in workflows),
            "tokens": sum(w.total_tokens for w in workflows),
            "subagents": sum(w.subagents for w in workflows),
            "unpriced_tokens": sum(w.unpriced_tokens for w in workflows),
            "paid_workflows": sum(1 for w in workflows if w.total_cost > 0),
        }

    def recent_roots(self) -> list[sqlite3.Row]:
        # Root sessions newest-activity-first, where activity is the latest update
        # anywhere in the subtree (a subagent still streaming bumps its root).
        # Feeds the one-shot `--status` command, which wants "the current session"
        # without the full workflows() rollup; directories are returned raw -- the
        # caller folds them to git roots.
        directory_expr = self._session_text_expr("root", ["directory", "path"], "'(unknown)'")
        if "time_updated" in self.session_columns:
            ts_expr = "coalesce(s.time_updated, s.time_created)"
        else:
            ts_expr = "s.time_created"
        sql = f"""
        with recursive tree(root_id, id) as (
          select id, id from session where parent_id is null
          union all
          select tree.root_id, child.id
          from session child join tree on child.parent_id = tree.id
        )
        select
          tree.root_id as id,
          {directory_expr} as directory,
          max({ts_expr}) as last_active
        from tree
        join session s on s.id = tree.id
        join session root on root.id = tree.root_id
        group by tree.root_id
        order by last_active desc
        """
        return list(self.conn.execute(sql))

    def root_of(self, session_id: str) -> str | None:
        # Resolve any session id to its root by walking parent_id upward -- so a
        # caller holding a subagent's id (e.g. a tmux plugin that saw a subagent's
        # busy event) still prices the whole workflow. None when the id is unknown.
        sql = """
        with recursive up(id, parent_id) as (
          select id, parent_id from session where id = ?
          union all
          select s.id, s.parent_id from session s join up on s.id = up.parent_id
        )
        select id from up where parent_id is null limit 1
        """
        row = self.conn.execute(sql, [session_id]).fetchone()
        return row["id"] if row else None

    @debug.timed("opencode.nodes")
    def workflow_nodes(self, workflow_id: str) -> list[sqlite3.Row]:
        self._prepare_session_usage(workflow_id)
        message_table = "opentab_message_usage" if self.has_v2 else "message"
        message_model = "m.model_name" if self.has_v2 else MSG_MODEL_EXPR
        message_role = "m.role" if self.has_v2 else "json_extract(m.data, '$.role')"
        # Keep the uncorrelated tree filter inside message reads. Join/correlation
        # keys alone need not push down into the mixed v1/v2 UNION view, causing
        # SQLite to normalize the whole message corpus for a single session.
        token_exprs = self._token_exprs()
        cost_expr = self._cost_expr()
        agent_expr = self._session_text_expr("s", ["agent"], "'-'")
        title_expr = self._session_text_expr("s", ["title"], "'(untitled)'")
        sql = f"""
        with recursive tree(id, depth) as (
          select id, 0 from session where id = ?
          union all
          select child.id, tree.depth + 1
          from session child
          join tree on child.parent_id = tree.id
        )
        {self._message_usage_cte()}
        select
          s.id,
          tree.depth,
          {agent_expr} as agent,
          {title_expr} as title,
          datetime(s.time_created / 1000, 'unixepoch', 'localtime') as created_at,
          {cost_expr} as cost,
          {token_exprs['tokens_input']} as tokens_input,
          {token_exprs['tokens_output']} as tokens_output,
          {token_exprs['tokens_reasoning']} as tokens_reasoning,
          {token_exprs['tokens_cache_read']} as tokens_cache_read,
          {token_exprs['tokens_cache_write']} as tokens_cache_write,
          {token_exprs['tokens_total']} as tokens_total,
          coalesce((
            select {message_model}
            from {message_table} m
            where m.session_id = s.id and {message_role} = 'assistant'
              and m.session_id in (select id from tree)
            group by {message_model}
            order by count(*) desc
            limit 1
          ), 'unknown (not recorded)') as model_name
        from session s
        join tree on tree.id = s.id
        {self._message_usage_join()}
        order by tree.depth, s.time_created
        """
        rows = list(self.conn.execute(sql, [workflow_id]))
        if not self.demo:
            return rows
        return [scramble_node(dict(r), self.demo_scale, self.demo_cats) for r in rows]

    @debug.timed("opencode.models")
    def model_breakdown(self) -> list[sqlite3.Row | dict]:
        if self._usage_cache is not None:
            self._usage_cache.prepare(self.conn, self._legacy_usage_available())
        # Per-(root session, model) cost/token attribution for EVERY root, in one
        # pass. Computed from per-message data (accurate for multi-model and older
        # sessions). The App caches this and slices it per session/day/month, so we
        # never run a query per workflow.
        # V2 reads the prepared numeric table. For legacy JSON, pull each scalar
        # out ONCE per row (the `msg` CTE), then
        # aggregate from those plain columns -- instead of ~35 json_extract(m.data, ...)
        # calls spread across the SELECT, each of which RE-PARSES the whole data blob.
        # The MATERIALIZED hint is what forces the single-pass extraction (without it the
        # planner inlines the CTE straight back into the aggregate and re-parses); it
        # needs SQLite 3.35+, so on older builds we drop the hint and fall back to the
        # original behaviour (identical results, just not sped up). ~40% faster on a
        # 44k-message DB; a big cut to the one heavy startup scan.
        mat = "materialized" if sqlite3.sqlite_version_info >= (3, 35, 0) else ""
        message_table = "opentab_message_usage" if self.has_v2 else "message"
        role = "m.role" if self.has_v2 else "json_extract(m.data, '$.role')"
        model = "m.model_name" if self.has_v2 else MSG_MODEL_EXPR
        usage = {
            name: f"m.{name}" if self.has_v2 else f"coalesce(json_extract(m.data, '{path}'), 0)"
            for name, path in (
                ("cost", "$.cost"),
                ("input", "$.tokens.input"),
                ("output", "$.tokens.output"),
                ("reasoning", "$.tokens.reasoning"),
                ("cache_read", "$.tokens.cache.read"),
                ("cache_write", "$.tokens.cache.write"),
            )
        }
        residual_cte = self._v2_model_residual_cte() if self.has_v2 else ""
        attributed = "attributed" if self.has_v2 else "msg"
        sql = f"""
        with recursive tree(root_id, id, depth) as (
          select id, id, 0 from session where parent_id is null
          union all
          select tree.root_id, child.id, tree.depth + 1
          from session child join tree on child.parent_id = tree.id
        ),
        msg as {mat} (
          select
            m.session_id as session_id,
            tree.root_id as root_id,
            tree.depth as depth,
            1 as runs,
            {model} as model_name,
            {usage['cost']} as cost,
            {usage['input']} as input,
            {usage['output']} as output,
            {usage['reasoning']} as reasoning,
            {usage['cache_read']} as cache_read,
            {usage['cache_write']} as cache_write
          from {message_table} m
          join tree on tree.id = m.session_id
          where {role} = 'assistant'
        ){residual_cte}
        select
          root_id,
          model_name,
          sum(runs) as runs,
          sum(cost) as cost,
          sum(case when depth = 0 then cost else 0 end) as root_cost,
          sum(input + output + reasoning + cache_read + cache_write) as tokens_total,
          sum(input) as input,
          sum(reasoning) as reasoning,
          sum(cache_read) as cache_read,
          sum(cache_write) as cache_write,
          sum(output) as output,
          sum(case when cost = 0 then input else 0 end) as unpriced_input,
          sum(case when cost = 0 then reasoning else 0 end) as unpriced_reasoning,
          sum(case when cost = 0 then cache_read else 0 end) as unpriced_cache_read,
          sum(case when cost = 0 then cache_write else 0 end) as unpriced_cache_write,
          sum(case when cost = 0 then output else 0 end) as unpriced_output,
          sum(case when depth = 0 and cost = 0 then input else 0 end) as root_unpriced_input,
          sum(case when depth = 0 and cost = 0 then reasoning else 0 end) as root_unpriced_reasoning,
          sum(case when depth = 0 and cost = 0 then cache_read else 0 end) as root_unpriced_cache_read,
          sum(case when depth = 0 and cost = 0 then cache_write else 0 end) as root_unpriced_cache_write,
          sum(case when depth = 0 and cost = 0 then output else 0 end) as root_unpriced_output
        from {attributed}
        group by root_id, model_name
        """
        # Subscription/credit rows (Copilot, Codex, Claude Code) carry real runs
        # AND real token counts but cost 0 in the message JSON. Demo mode reconciles
        # them to each session's synthetic total; the "$" toggle prices their tokens
        # at API list prices -- both in App._load_model_cache.
        return list(self.conn.execute(sql))

    @staticmethod
    def _v2_model_residual_cte() -> str:
        # Reuse the materialized numeric rows from the deferred model scan rather
        # than reparsing every inline tool output to check native aggregate gaps.
        # Synthetic gap rows carry zero runs, never turns.
        return """
        , usage as (
          select m.session_id,
                 sum(m.cost) as cost,
                 sum(m.input) as input,
                 sum(m.output) as output,
                 sum(m.reasoning) as reasoning,
                 sum(m.cache_read) as cache_read,
                 sum(m.cache_write) as cache_write
          from msg m
          group by m.session_id
        ), residuals as (
        select s.id as session_id, tree.root_id, tree.depth, 0 as runs,
               'unknown (session aggregate)' as model_name,
               max(0, coalesce(s.cost, 0) - coalesce(usage.cost, 0)) as cost,
               max(0, coalesce(s.tokens_input, 0) - coalesce(usage.input, 0)) as input,
               max(0, coalesce(s.tokens_output, 0) - coalesce(usage.output, 0)) as output,
               max(0, coalesce(s.tokens_reasoning, 0) - coalesce(usage.reasoning, 0)) as reasoning,
               max(0, coalesce(s.tokens_cache_read, 0) - coalesce(usage.cache_read, 0)) as cache_read,
               max(0, coalesce(s.tokens_cache_write, 0) - coalesce(usage.cache_write, 0)) as cache_write
        from tree join main.session_v2 s on s.id = tree.id
        left join usage on usage.session_id = s.id
        ), attributed as (
          select * from msg
          union all
          select * from residuals
          where cost != 0 or input + output + reasoning + cache_read + cache_write != 0
        )
        """

    def _legacy_usage_available(self) -> bool:
        return {"id", "session_id", "data"} <= {
            row["name"] for row in self.conn.execute("pragma main.table_info(message)")
        }

    def restore_accounting_cache(self, payload) -> None:
        if self._usage_cache is not None:
            self._usage_cache.restore(payload)

    def set_accounting_cache_loader(self, loader) -> None:
        self._accounting_cache_loader = loader

    @debug.timed("opencode.session_usage")
    def _prepare_session_usage(self, workflow_id: str) -> None:
        if self._usage_cache is None:
            return
        loader = getattr(self, "_accounting_cache_loader", None)
        ids = [
            row[0]
            for row in debug.query_rows(
                self.conn,
                "with recursive tree(id) as (select id from session where id=? union all "
                "select s.id from session s join tree on s.parent_id=tree.id) select id from tree",
                [workflow_id],
                label="opencode.usage_scope",
            )
        ]
        debug.event(
            "opencode.usage_scope_selected",
            session=debug.identity(workflow_id),
            sessions=len(ids),
            sidecar_loader=loader is not None,
        )
        if loader is not None and (
            not self._usage_cache.ready
            or self._usage_cache.scope is not None
            and self._usage_cache.scope != frozenset(ids)
        ):
            self.restore_accounting_cache(loader(ids))
        self._usage_cache.prepare(self.conn, self._legacy_usage_available(), scope=ids)

    def accounting_cache(self):
        return self._usage_cache.export() if self._usage_cache is not None else None

    @property
    def accounting_cache_reused(self) -> bool:
        return self._usage_cache is not None and self._usage_cache.reused > 0

    @property
    def accounting_cache_changed(self) -> bool:
        return self._usage_cache is not None and self._usage_cache.changed

    def _detail_sql(
        self,
        sql: str,
        conn: sqlite3.Connection | None = None,
        *,
        part_row: int | None = None,
        part_metadata: bool = False,
        change_candidates: bool = False,
    ) -> str:
        return (
            scoped_detail_sql(
                conn or self.conn,
                sql,
                part_row=part_row,
                part_metadata=part_metadata,
                change_candidates=change_candidates,
            )
            if self.has_v2
            else sql
        )

    @debug.timed("opencode.tools")
    def tool_breakdown(self, workflow_id: str) -> list[sqlite3.Row]:
        # Per-(tool, model) token/cost attribution for ONE session tree (root +
        # subagents). Each assistant message is exactly one LLM step whose recorded
        # tokens/cost live on the message; the tools it invoked that step are its
        # `part` rows. We attribute the message's tokens/cost to those tools, split
        # evenly when a step calls several in parallel (so the per-tool figures sum
        # back to the tokens of every tool-calling step). Grouping also by model lets
        # the "$" view reprice $0 (subscription) rows at that model's list price.
        #
        # Restricting `part` to the session tree FIRST (part_session_idx) keeps this a
        # ~per-session scan -- cheap enough to run lazily on drill-in rather than as a
        # whole-table scan at startup, unlike model_breakdown.
        if not self.supports_tool_breakdown:
            return []
        numeric = self._usage_cache is not None
        if numeric:
            self._prepare_session_usage(workflow_id)
            parts = self._session_part_metadata(workflow_id)
            self.conn.execute("savepoint opentab_tool_parts")
            try:
                self.conn.execute(
                    "create temp table if not exists opentab_tool_parts(message_id, ptype, tool)"
                )
                self.conn.execute("delete from temp.opentab_tool_parts")
                self.conn.executemany(
                    "insert into temp.opentab_tool_parts values(?,?,?)",
                    [(mid, kind, tool) for mid, kind, tool, readable in parts if kind == "tool"],
                )
                self.conn.execute("release opentab_tool_parts")
            except BaseException:
                self.conn.execute("rollback to opentab_tool_parts")
                self.conn.execute("release opentab_tool_parts")
                raise
        model = "m.model_name" if numeric else MSG_MODEL_EXPR
        fields = {
            name: f"m.{name}" if numeric else f"coalesce(json_extract(m.data, '{path}'), 0)"
            for name, path in (
                ("input", "$.tokens.input"),
                ("output", "$.tokens.output"),
                ("reasoning", "$.tokens.reasoning"),
                ("cache_read", "$.tokens.cache.read"),
                ("cache_write", "$.tokens.cache.write"),
                ("cost", "$.cost"),
            )
        }
        total = " + ".join(
            fields[k] for k in ("input", "output", "reasoning", "cache_read", "cache_write")
        )
        part_source = (
            "select message_id, ptype, tool from temp.opentab_tool_parts"
            if numeric
            else """
          select message_id,
                 json_extract(data, '$.type') as ptype,
                 json_extract(data, '$.tool') as tool
          from part
          where session_id in (select id from tree)
        """
        )
        sql = f"""
        with recursive tree(id) as (
          select id from session where id = ?
          union all
          select child.id from session child join tree on child.parent_id = tree.id
        ),
        session_parts as (
          {part_source}
        ),
        tool_counts as (  -- how many tools each step called (the even-split divisor)
          select message_id, count(*) as n
          from session_parts where ptype = 'tool' group by message_id
        ),
        tools as (
          select message_id, tool from session_parts where ptype = 'tool'
        )
        select
          t.tool as tool,
          {model} as model_name,
          count(*) as calls,
          sum(({total}) * 1.0 / tc.n) as tokens_total,
          sum({fields['input']} * 1.0 / tc.n) as input,
          sum({fields['output']} * 1.0 / tc.n) as output,
          sum({fields['reasoning']} * 1.0 / tc.n) as reasoning,
          sum({fields['cache_read']} * 1.0 / tc.n) as cache_read,
          sum({fields['cache_write']} * 1.0 / tc.n) as cache_write,
          sum({fields['cost']} * 1.0 / tc.n) as cost
        from tools t
        join {'opentab_message_usage' if numeric else 'message'} m on m.id = t.message_id
        join tool_counts tc on tc.message_id = t.message_id
        where m.session_id in (select id from tree)
        group by t.tool, model_name
        order by cost desc, tokens_total desc
        """
        return list(
            debug.query_rows(
                self.conn,
                self._detail_sql(sql, part_metadata=True),
                [workflow_id],
                label="opencode.tools_query",
            )
        )

    def supports_tools(self, workflow_id: str) -> bool:
        # Per-session capability gate for the Tools tab. A single OpenCode DB is
        # uniform (every session is backed by the part table or none is), so the id
        # is ignored here; CombinedStore overrides this to route by owning backend so
        # only OpenCode sessions in a merged view offer the tab.
        return self.supports_tool_breakdown

    def _timeline_columns(self) -> str:
        # The SELECT column list shared by message_timeline (one session) and
        # message_timeline_all (whole corpus). The per-message wall-clock time lives in
        # the JSON ($.time.created, epoch ms), present regardless of whether the message
        # table carries a time_created column, so sort/format off that. Return the full
        # localtime datetime and let the renderer pick the display width -- a session can
        # span days, so the date matters.
        agent_expr = self._session_text_expr("s", ["agent"], "'-'")
        summary_title = "nullif(json_extract(m.data, '$.summary.title'), '')"
        if self.supports_tool_breakdown:  # the raw prompt text lives in the part table
            part_order = "p.part_index, p.rowid" if "part_index" in self.part_columns else "p.rowid"
            part_table = "part"
            part_columns = self.part_columns
            if self.has_v2:
                # Native v2 prompts live on the message. Query legacy parts directly
                # for legacy prompts: a correlated lookup through the UNION view can
                # materialize every session's parts before applying the join keys.
                part_table = "main.part"
                part_order = "p.rowid"
                part_columns = self._legacy_part_columns
            ownership = "and p.session_id = m.session_id " if "session_id" in part_columns else ""
            part_text = (
                f"(select json_extract(p.data, '$.text') from {part_table} p "
                f"where p.message_id = m.id {ownership}"
                "and json_extract(p.data, '$.type') = 'text' "
                f"order by {part_order} limit 1)"
            )
            if self.has_v2:
                if not {"id", "message_id", "data"} <= part_columns:
                    part_text = "null"
                part_text = (
                    "case when json_extract(m.data, '$.__opentab_v2') = 1 "
                    "then case when json_type(m.data, '$.text') = 'text' "
                    "then json_extract(m.data, '$.text') end else " + part_text + " end"
                )
        else:
            part_text = "null"
        # Summary title and raw prompt as separate columns: the one-line group title
        # prefers the generated summary, the expandable full text the raw prompt.
        title_expr = f"case when json_extract(m.data, '$.role') = 'user' then {summary_title} end"
        prompt_expr = f"case when json_extract(m.data, '$.role') = 'user' then {part_text} end"
        return f"""
          json_extract(m.data, '$.role') as role,
          m.id as mid,
          datetime({_TL_TS} / 1000, 'unixepoch', 'localtime') as time,
          tree.depth as depth,
          {agent_expr} as agent,
          -- The reasoning level this call ran at. OpenCode calls it the model VARIANT
          -- ("high"/"medium"/"xhigh"/"low"/"none"), written per assistant message, so it
          -- follows a mid-session switch exactly like Codex's turn_context does. Recorded
          -- only where the provider exposes one: measured on a 41,857-message corpus it
          -- is present on 11,965 (every openai/gpt-5.x row) and absent on every Anthropic
          -- row, which is what leaves the column off a Claude-only session.
          --
          -- The MESSAGE's variant, deliberately, though `session.model` carries one too
          -- ({"id","providerID","variant"}). That one is the session's CURRENT setting,
          -- so back-filling a message that recorded none would invent a level for 200
          -- messages on this corpus -- 131 of them Claude rows, which have no variant
          -- concept at all, plus local ollama/mlx models. A fabricated level is worse
          -- than an absent one here: it feeds the cache-miss verdict, so an invented
          -- switch would print a ⚙ marker blaming a decision nobody made.
          coalesce(json_extract(m.data, '$.variant'), '') as effort,
          {MSG_MODEL_EXPR} as model_name,
          coalesce(json_extract(m.data, '$.cost'), 0) as cost,
          coalesce(json_extract(m.data, '$.tokens.input'), 0) as input,
          coalesce(json_extract(m.data, '$.tokens.output'), 0) as output,
          coalesce(json_extract(m.data, '$.tokens.reasoning'), 0) as reasoning,
          coalesce(json_extract(m.data, '$.tokens.cache.read'), 0) as cache_read,
          coalesce(json_extract(m.data, '$.tokens.cache.write'), 0) as cache_write,
          ({MSG_TOKEN_TOTAL_EXPR}) as tokens_total,
          {title_expr} as summary_title,
          {prompt_expr} as prompt_text"""

    def message_timeline(self, workflow_id: str) -> list[dict]:
        # Every assistant message (one LLM step = one "turn") in the session tree,
        # ordered chronologically -- the raw material for the Turns tab's
        # cost-over-time view. Like tool_breakdown this restricts the scan to the
        # session subtree first, so it's a cheap per-session query fetched lazily on
        # drill-in, not the whole-table model_breakdown scan. Subagent turns
        # (depth > 0) are interleaved by time with the root's, each tagged with its
        # depth/agent so the renderer can mark them. Recorded $0 (subscription) rows
        # keep their token columns so the "$" view can reprice them at list price.
        #
        # We also pull the `user` messages (not just `assistant`) so each turn can be
        # tagged with the prompt that triggered it: walking the time-ordered stream,
        # the most recent user message owns every assistant turn until the next one.
        # A user message's title is OpenCode's generated `summary.title`, falling back
        # to its first text part (the raw prompt) when that's empty.
        return self._message_timeline(workflow_id)

    @debug.timed("opencode.timeline")
    def _message_timeline(self, workflow_id: str, *, own: bool = False) -> list[dict]:
        if not self.supports_message_timeline:
            return []
        message_columns = getattr(self, "message_columns", set())
        seq_order = "m.seq" if "seq" in message_columns else _TL_TS
        v2_marker = (
            "json_extract(m.data, '$.__opentab_v2') = 1" if "seq" in message_columns else "0"
        )
        sql = f"""
        with recursive tree(id, depth) as (
          select id, 0 from session where id = ?
          union all
          select child.id, tree.depth + 1
          from session child join tree on child.parent_id = tree.id
          where not ?
        )
        select {self._timeline_columns()}
        from message m
        join tree on tree.id = m.session_id
        join session s on s.id = m.session_id
        where m.session_id in (select id from tree)
          and json_extract(m.data, '$.role') in ('user', 'assistant')
        order by case
          when (select count(*) from tree) = 1 and {v2_marker}
          then {seq_order} else {_TL_TS}
        end, m.rowid
        """
        rows = [
            dict(r)
            for r in debug.query_rows(
                self.conn,
                self._detail_sql(sql),
                [workflow_id, own],
                label="opencode.timeline_messages",
            )
        ]
        return _process_timeline(
            rows,
            self._timeline_tools(workflow_id, own=own),
            self._timeline_reads(workflow_id, own=own),
        )

    @debug.timed("opencode.timeline_read_markers")
    def _timeline_reads(
        self, workflow_id: str, *, own: bool = False
    ) -> dict[str, tuple[bool, bool]]:
        """message id -> (has narration, has reasoning), for the Turns drill's marker.

        A GROUPED aggregate, not a row scan: it answers one boolean pair per message
        instead of materializing every text and reasoning part, which is what keeps the
        marker free of a content fetch (28 ms on the largest real session, the same order
        as _timeline_tools beside it). Deliberately NOT computed by message_timeline_all:
        that feeds the fleet export, where the content itself never travels, and a marker
        promising something to read that the remote machine cannot open is worse than no
        marker at all.
        """
        if not self.supports_tool_breakdown:
            return {}
        if self.has_v2:
            out = {}
            try:
                for mid, kind, _tool, readable in self._session_part_metadata(workflow_id, own=own):
                    if mid and readable:
                        text, reason = out.get(mid, (False, False))
                        out[mid] = (text or kind == "text", reason or kind == "reasoning")
            except sqlite3.Error:
                return {}
            return out
        sql = """
        with recursive tree(id) as (
          select id from session where id = ?
          union all
          select child.id from session child join tree on child.parent_id = tree.id
          where not ?
        )
        select p.message_id,
               max(json_extract(p.data, '$.type') = 'text'),
               max(json_extract(p.data, '$.type') = 'reasoning')
        from part p
        where p.session_id in (select id from tree)
          and json_extract(p.data, '$.type') in ('text', 'reasoning')
          -- Trimmed with an explicit character set, matching the trace's own strip():
          -- SQLite's one-argument trim() removes ASCII SPACES only, so a part holding
          -- A tab/newline-only part would mark the row readable and open onto nothing.
          -- every ASCII whitespace character Python strips; the residual is a part whose
          -- entire content is a UNICODE space (NBSP, em space), which no writer emits as
          -- a whole message and which SQLite cannot express without a LIKE-per-codepoint.
          -- The set is verified against str.strip() over the whole ASCII range, the
          -- four control SEPARATORS (28-31) included -- Python strips those too.
          and length(
              trim(
                  coalesce(json_extract(p.data, '$.text'), ''),
                  char(32) || char(9) || char(10) || char(11) || char(12) || char(13)
                  || char(28) || char(29) || char(30) || char(31)
              )
          ) > 0
        group by p.message_id
        """
        try:
            rows = self.conn.execute(
                self._detail_sql(sql, part_metadata=True), [workflow_id, own]
            ).fetchall()
        except sqlite3.Error:
            return {}  # an older schema simply shows no marker, never an error
        return {mid: (bool(text), bool(reason)) for mid, text, reason in rows if mid}

    @debug.timed("opencode.timeline_tools")
    def _timeline_tools(
        self, workflow_id: str | None = None, *, own: bool = False
    ) -> dict[str, list[str]]:
        """message id -> the tool names that step called, in call order.

        A SEPARATE grouped scan, deliberately, rather than a correlated subquery in
        _timeline_columns: the columns are shared with message_timeline_all, and a
        per-row `(select ... from part where p.message_id = m.id)` there costs the
        whole-corpus export 300ms -> 3,819ms (measured, 46,785 messages / 182,133
        parts) -- a 12x regression on the exact path message_timeline_all exists to
        keep fast. One grouped scan grouped in Python is 909ms for the corpus and
        ~27ms for the largest single session, and is the shape message_timeline_all
        already uses for the messages themselves.

        `workflow_id` restricts to that session tree (part_session_idx); None scans
        every tool part for the export.
        """
        if not self.supports_tool_breakdown:
            return {}
        if self.has_v2 and workflow_id is not None:
            out = {}
            for mid, kind, tool, _readable in self._session_part_metadata(workflow_id, own=own):
                if kind == "tool" and tool:
                    out.setdefault(mid, []).append(tool)
            return out
        part_order = (
            "message_id, part_index, rowid" if "part_index" in self.part_columns else "rowid"
        )
        if workflow_id is None:
            sql, params = (
                (
                    "select message_id, json_extract(data, '$.tool') as tool from part "
                    f"where json_extract(data, '$.type') = 'tool' order by {part_order}"
                ),
                [],
            )
        else:
            sql, params = (
                (
                    f"""
                with recursive tree(id) as (
                  select id from session where id = ?
                  union all
                  select child.id from session child join tree on child.parent_id = tree.id
                  where not ?
                )
                select message_id, json_extract(data, '$.tool') as tool
                from part
                where session_id in (select id from tree)
                  and json_extract(data, '$.type') = 'tool'
                order by {part_order}
                """
                ),
                [workflow_id, own],
            )
        out: dict[str, list[str]] = {}
        if workflow_id is not None:
            sql = self._detail_sql(sql, part_metadata=True)
        for mid, tool in self.conn.execute(sql, params):
            if tool:
                out.setdefault(mid, []).append(tool)
        return out

    @debug.timed("opencode.part_metadata")
    def _session_part_metadata(self, workflow_id: str, *, own: bool = False) -> list:
        """Share names/readability across Turns and Tools; never memoize bodies."""
        version = self.conn.execute("pragma data_version").fetchone()[0]
        key = (version, workflow_id, own)
        memo = getattr(self, "_part_metadata_memo", None)
        if memo is not None and memo[0] == key:
            debug.event("opencode.part_metadata_cache", result="hit", rows=len(memo[1]))
            return memo[1]
        sql = """
        with recursive tree(id) as (
          select id from session where id=? union all
          select child.id from session child join tree on child.parent_id=tree.id where not ?
        )
        select message_id, json_extract(data, '$.type'), json_extract(data, '$.tool'),
          case when json_extract(data, '$.type') in ('text','reasoning') then
            length(trim(coalesce(json_extract(data, '$.text'), ''),
              char(32)||char(9)||char(10)||char(11)||char(12)||char(13)||char(28)||char(29)||char(30)||char(31))) > 0
          else 0 end
        from part where session_id in (select id from tree)
        order by message_id, part_index, rowid
        """
        rows = list(
            debug.query_rows(
                self.conn,
                self._detail_sql(sql, part_metadata=True),
                [workflow_id, own],
                label="opencode.part_metadata_query",
            )
        )
        self._part_metadata_memo = (key, rows)
        debug.event(
            "opencode.part_metadata_cache",
            result="miss",
            rows=len(rows),
            reason="empty"
            if memo is None
            else "source_revision"
            if memo[0][0] != version
            else "scope_changed",
        )
        return rows

    @debug.timed("opencode.turn_content")
    def turn_content(
        self, workflow_id: str, content_key: str | None = None
    ) -> dict[str, list[dict]]:
        """Trace events per turn: narration, reasoning, and each call's arguments.

        One grouped scan over the session subtree's parts -- the _timeline_tools shape,
        for the same reason (a correlated subquery per turn is what made the export 12x
        slower). OpenCode is the one backend of the three that records real reasoning
        PROSE rather than an empty block: measured 19,324 of 23,298 reasoning parts
        non-empty, averaging 224 characters.
        """
        return self._turn_content(workflow_id, content_key)

    def _turn_content(
        self, workflow_id: str, content_key: str | None, *, own: bool = False
    ) -> dict:
        if not self.supports_tool_breakdown:
            return {}
        part_order = (
            "p.message_id, p.part_index, p.rowid"
            if "part_index" in self.part_columns
            else "p.rowid"
        )
        sql = f"""
        with recursive tree(id) as (
          select id from session where id = ?
          union all
          select child.id from session child join tree on child.parent_id = tree.id
          where not ?
        )
        select p.message_id, p.data from part p
        join message m on m.id = p.message_id
        where p.session_id in (select id from tree)
          and m.session_id in (select id from tree)
          and m.session_id = p.session_id
          and json_extract(m.data, '$.role') = 'assistant'
          and json_extract(p.data, '$.type') in ('text', 'reasoning', 'tool')
        order by {part_order}
        """
        out = TraceContent(content_key)
        # Joined to the message rather than filtered afterwards: a USER message's text
        # part is the prompt, which the tab already shows as the group header -- keyed
        # here it would be content no turn claims, carried for the session's lifetime.
        for mid, blob in self.conn.execute(self._detail_sql(sql), [workflow_id, own]):
            if not mid or not out.accepts(mid):
                continue
            events = out.setdefault(mid, [])
            if len(events) >= out.event_limit:
                continue
            try:
                part = json.loads(blob)
            except (TypeError, ValueError):
                continue
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in ("text", "reasoning"):
                text, dropped = out.clip(part.get("text"), TRACE_TEXT_CAP)
                if text:
                    events.append({"kind": kind, "text": text, "dropped": dropped})
                continue
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            head, params = out.arguments(state.get("input"))
            status = state.get("status") or ""
            raw = state.get("error") if status == "error" else state.get("output")
            output, out_dropped = out.clip(raw, TRACE_OUTPUT_CAP)
            events.append(
                {
                    "kind": "tool",
                    "name": part.get("tool") or "(unknown)",
                    "args": head,
                    "params": params,
                    "output": output,
                    "output_dropped": out_dropped,
                    "status": status,
                }
            )
        return out

    def supports_turn_content(self, workflow_id: str) -> bool:
        # Same gate as the tool breakdown: both read the part table.
        return bool(self.supports_tool_breakdown)

    def supports_changes(self, root_id: str) -> bool:
        """Schema capability only; retained summaries and ownership are checked on read."""
        return not self.demo and self.supports_session_changes

    def change_request(self, root_id: str, change_key: str | None = None):
        """Freeze a worker-owned change read without sharing this store's connection."""
        if self.demo or not self.supports_session_changes:
            return None
        return _ChangeRequest(self.db, root_id, change_key)

    @debug.timed("opencode.changes_open")
    def _change_connection(self, uri: str) -> sqlite3.Connection:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        self._tune(conn)
        install_views(conn)
        cancelled = getattr(self, "_change_cancelled", None)
        if cancelled is not None:
            conn.set_progress_handler(cancelled.is_set, 1000)
        return conn

    @staticmethod
    def _change_key(root_id: str, revision, row: dict) -> str:
        identity = {
            "root": root_id,
            # Current schemas revise the selected message/part on update. Unrelated
            # live sessions must not expire a historical selection; old schemas need
            # the database token instead.
            "revision": None if not Store._change_uses_global_revision(row) else revision,
            "source": row["source"],
            "message_id": row["message_id"],
            "execution_id": row["execution_id"],
            "message_row": row["message_row"] if row["source"] == "snapshot" else None,
            "message_created": row["message_created"] if row["source"] == "snapshot" else None,
            "message_updated": row["message_updated"] if row["source"] == "snapshot" else None,
            "message_bytes": row["message_bytes"] if row["source"] == "snapshot" else None,
            "index": row["diff_index"],
            "file": row["file"],
            "status": row["status"],
            "additions": row["additions"],
            "deletions": row["deletions"],
            "patch_bytes": row["patch_bytes"],
            "before_bytes": row["before_bytes"],
            "after_bytes": row["after_bytes"],
            "part_row": row["part_row"],
            "part_id": row["part_id"],
            "part_created": row["part_created"],
            "part_updated": row["part_updated"],
            "part_bytes": row["part_bytes"],
            "tool_message_row": row["tool_message_row"],
            "tool_message_id": row["tool_message_id"],
            "tool_message_updated": row["tool_message_updated"],
            "from_file": row["from_file"],
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        row_id = row["message_row"] if row["source"] == "snapshot" else row["part_row"]
        return (
            f"occhg1:{row['source']}:{row_id}:{row['diff_index']}:"
            + hashlib.sha256(encoded).hexdigest()
        )

    @staticmethod
    def _change_uses_global_revision(row: dict) -> bool:
        if row["source"] == "snapshot":
            return row["message_updated"] is None
        return row["part_updated"] is None or row["tool_message_updated"] is None

    @staticmethod
    def _change_count(value, kind) -> int | None:
        return value if kind == "integer" and isinstance(value, int) and value >= 0 else None

    @staticmethod
    def _clip_utf8(text: str, limit: int) -> tuple[str, bool]:
        raw = text.encode("utf-8")
        if len(raw) <= limit:
            return text, False
        return raw[:limit].decode("utf-8", "ignore"), True

    @staticmethod
    def _change_path(path, directory, root_directory) -> str | None:
        if not isinstance(path, str) or not path:
            return None
        if not isinstance(directory, str) or not directory:
            return path
        try:
            # Resolve against the execution, but display relative to the root. Two
            # child worktrees' src/a.py must never become the same changed file.
            paths = ntpath if ntpath.splitdrive(directory)[0] else posixpath
            normalized = paths.normpath(paths.join(directory, path))
            base = paths.normpath(root_directory) if isinstance(root_directory, str) else ""
            if paths.isabs(normalized) and paths.isabs(base):
                if paths.commonpath((normalized, base)) == base:
                    return paths.relpath(normalized, base)
            return normalized
        except (OSError, ValueError):
            return path

    @debug.timed("opencode.changes_metadata", count_rows=False)
    def _change_rows(
        self, conn: sqlite3.Connection, root_id: str, locator: tuple[str, int, int] | None = None
    ) -> tuple[list[dict], object]:
        if not isinstance(root_id, str) or not root_id:
            debug.event("opencode.changes_rejected", reason="invalid_root")
            return [], None
        root_directory = "directory" if "directory" in self.session_columns else "null"
        roots = conn.execute(
            f"select parent_id, {root_directory} from session where id = ? limit 2", [root_id]
        ).fetchall()
        if len(roots) != 1 or roots[0][0] is not None:
            debug.event("opencode.changes_rejected", reason="missing_or_ambiguous_root")
            return [], None
        root_directory = roots[0][1]
        tree = """with recursive tree(id) as (
          select id from session where id = ?
          union
          select child.id from session child join tree on child.parent_id = tree.id
        ) """
        ambiguous = conn.execute(
            tree + "select tree.id from tree join session s on s.id = tree.id "
            "group by tree.id having count(*) != 1 limit 1",
            [root_id],
        ).fetchone()
        if ambiguous:
            debug.event("opencode.changes_rejected", reason="ambiguous_tree")
            return [], None
        revision = self._conversation_database_manifest()
        safe = "case when json_valid(m.data) then m.data else '{}' end"
        created = (
            "m.time_created"
            if "time_created" in self.message_columns
            else f"json_extract({safe}, '$.time.created')"
        )
        updated = "m.time_updated" if "time_updated" in self.message_columns else "null"
        directory = (
            f"case when length(cast(s.directory as blob)) <= {CHANGE_METADATA_BYTES} "
            "then s.directory end"
            if "directory" in self.session_columns
            else "null"
        )
        diff = "case when d.type = 'object' then d.value else '{}' end"
        cap = CHANGE_METADATA_BYTES + 1
        snapshot_filter = "and m.rowid = ? and d.key = ?" if locator else ""
        # CROSS JOIN keeps the tree outside v1 indexed reads. The explicit IN
        # filters also push through mixed v1/v2 UNION views; join keys alone can
        # materialize the entire corpus, including in uniqueness subqueries.
        snapshot_sql = f"""
        {tree}
        select
          'snapshot' as source,
          coalesce({created}, 0) as event_created,
          m.rowid as event_order,
          m.rowid as message_row,
          substr(cast(m.id as text), 1, ?) as message_id,
          length(cast(cast(m.id as text) as blob)) as message_id_bytes,
          substr(cast(m.session_id as text), 1, ?) as execution_id,
          length(cast(cast(m.session_id as text) as blob)) as execution_id_bytes,
          {created} as message_created,
          {updated} as message_updated,
          length(cast(m.data as blob)) as message_bytes,
          {directory} as directory,
          d.key as diff_index,
          substr(json_extract({diff}, '$.file'), 1, ?) as file,
          length(cast(json_extract({diff}, '$.file') as blob)) as file_bytes,
          substr(json_extract({diff}, '$.status'), 1, ?) as status,
          length(cast(json_extract({diff}, '$.status') as blob)) as status_bytes,
          case when json_type({diff}, '$.additions') = 'integer'
            then json_extract({diff}, '$.additions') end as additions,
          json_type({diff}, '$.additions') as additions_type,
          case when json_type({diff}, '$.deletions') = 'integer'
            then json_extract({diff}, '$.deletions') end as deletions,
          json_type({diff}, '$.deletions') as deletions_type,
          json_type({diff}, '$.patch') as patch_type,
          coalesce(length(cast(json_extract({diff}, '$.patch') as blob)), 0) as patch_bytes,
          json_type({diff}, '$.before') as before_type,
          coalesce(length(cast(json_extract({diff}, '$.before') as blob)), 0) as before_bytes,
          json_type({diff}, '$.after') as after_type,
          coalesce(length(cast(json_extract({diff}, '$.after') as blob)), 0) as after_bytes,
          null as from_file,
          0 as from_file_bytes,
          null as part_row,
          null as part_id,
          null as part_id_bytes,
          null as part_created,
          null as part_updated,
          null as part_bytes,
          null as tool_message_row,
          null as tool_message_id,
          null as tool_message_id_bytes,
          null as tool_message_updated
        from tree
        cross join message m on m.session_id = tree.id
        join session s on s.id = m.session_id
        join json_each({safe}, '$.summary.diffs') d
        where json_extract({safe}, '$.role') = 'user'
          and m.session_id in (select id from tree)
          and json_type({safe}, '$.summary.diffs') = 'array'
          and d.type = 'object'
          and json_type({diff}, '$.file') = 'text'
          and length(json_extract({diff}, '$.file')) > 0
          {snapshot_filter}
        order by event_created, event_order, cast(d.key as integer)
        limit ?
        """
        params = [root_id, cap, cap, cap, cap]
        if locator:
            params.extend(locator[1:])
        params.append(CHANGE_SUMMARY_LIMIT + 1)
        rows = (
            [
                dict(row)
                for row in debug.query_rows(
                    conn,
                    self._detail_sql(snapshot_sql, conn),
                    params,
                    label="opencode.changes_snapshots",
                    explain=True,
                )
            ]
            if locator is None or locator[0] == "snapshot"
            else []
        )
        if locator and locator[0] == "snapshot" and not rows:
            return [], revision

        required_part = {"id", "message_id", "session_id", "data"}
        if required_part <= self.part_columns and (locator is None or locator[0] != "snapshot"):
            native_filter = "and p.rowid = ?" if locator else ""
            psafe = "case when json_valid(p.data) then p.data else '{}' end"
            msafe = "case when json_valid(tm.data) then tm.data else '{}' end"
            prompt_id = f"json_extract({msafe}, '$.parentID')"
            if self.has_v2:
                prompt_id = f"coalesce(tm.parent_id, {prompt_id})"
            materialized = "materialized" if sqlite3.sqlite_version_info >= (3, 35, 0) else ""
            debug.event(
                "opencode.changes_strategy",
                schema="v2" if self.has_v2 else "legacy",
                keyed=locator is not None,
                materialization_requested=bool(materialized),
                candidate_filter=self.has_v2,
                message_projection="metadata" if self.has_v2 else "legacy",
            )
            part_source = "part"
            candidate_cte = ""
            message_filter = ""
            duplicate_part_filter = ""
            if self.has_v2:
                # Freeze each candidate projection once, before the ownership and
                # metadata predicates can cause repeated inline-output normalization.
                candidate_cte = f""", candidate_parts as {materialized} (
                  select p.* from part p
                  where session_id in (select id from tree) {"and rowid = ?" if locator else ""}
                )"""
                part_source = "candidate_parts"
                native_filter = ""
            if self.has_v2 and locator:
                # A keyed read needs only its candidate's tool/prompt messages.
                # Session filters alone still normalize every message in a large
                # selected session. The locator narrows candidates, not ownership:
                # uniqueness and the full live occurrence key remain checked below.
                message_filter = f"""
                  and tm.id in (select message_id from candidate_parts)
                  and pm.id in (
                    select {prompt_id} from message tm
                    where tm.session_id in (select id from tree)
                      and tm.id in (select message_id from candidate_parts)
                  )"""
                duplicate_part_filter = (
                    "and x.message_id in (select message_id from candidate_parts)"
                )
            pcreated = (
                "p.time_created"
                if "time_created" in self.part_columns
                else f"json_extract({msafe}, '$.time.created')"
            )
            pupdated = "p.time_updated" if "time_updated" in self.part_columns else "null"
            tmupdated = "tm.time_updated" if "time_updated" in self.message_columns else "null"
            prompt_created = (
                "pm.time_created"
                if "time_created" in self.message_columns
                else "json_extract(case when json_valid(pm.data) then pm.data else '{}' end, '$.time.created')"
            )
            native_diff = "case when d.type = 'object' then d.value else '{}' end"
            native_path = f"""case when json_extract({native_diff}, '$.type') = 'move'
              then coalesce(json_extract({native_diff}, '$.movePath'), json_extract({native_diff}, '$.relativePath'))
              else coalesce(json_extract({native_diff}, '$.filePath'), json_extract({native_diff}, '$.relativePath'),
                            json_extract({native_diff}, '$.file')) end"""
            native_write_path = "coalesce(json_extract(n.data, '$.state.metadata.filepath'), json_extract(n.data, '$.state.input.path'))"
            # Reuse validated edits across the three metadata projections. Without
            # this hint SQLite can expand/normalize the selected session's parts
            # again for every UNION branch. Keep unrelated tools and the unused
            # full assistant message out of this transient relation. Prompt bytes
            # only bind snapshot keys; tool keys use the prompt ID and tool revision.
            native_sql = f"""
            {tree}{candidate_cte}, native as {materialized} (
              select p.id, p.data, p.rowid as part_row, tm.rowid as tool_message_row, tm.id as tool_message_id,
                     pm.rowid as prompt_row,
                     pm.id as prompt_id,
                     pm.session_id as execution_id, {directory} as directory,
                     {pcreated} as part_created, {pupdated} as part_updated,
                     {tmupdated} as tool_message_updated, {prompt_created} as prompt_created,
                     {'pm.time_updated' if 'time_updated' in self.message_columns else 'null'} as prompt_updated
              from tree
              cross join {part_source} p on p.session_id = tree.id
              join message tm on tm.id = p.message_id and tm.session_id = p.session_id
              join message pm
                on pm.id = {prompt_id}
               and pm.session_id = tm.session_id
              join session s on s.id = p.session_id
              where json_extract({psafe}, '$.type') = 'tool'
                and json_extract({psafe}, '$.tool') in ('apply_patch', 'patch', 'edit', 'write')
                and p.session_id in (select id from tree)
                and tm.session_id in (select id from tree)
                and pm.session_id in (select id from tree)
                {native_filter}
                {message_filter}
                and json_extract({psafe}, '$.state.status') = 'completed'
                and json_extract({msafe}, '$.role') = 'assistant'
                and json_extract(case when json_valid(pm.data) then pm.data else '{{}}' end, '$.role') = 'user'
                and (select count(*) from message x
                     where x.id = p.message_id and x.session_id = p.session_id
                       and x.session_id in (select id from tree)) = 1
                and (select count(*) from message x
                     where x.id = {prompt_id}
                       and x.session_id = p.session_id
                       and x.session_id in (select id from tree)) = 1
                and (select count(*) from part x
                     where x.id = p.id and x.message_id = p.message_id
                       and x.session_id = p.session_id
                       {duplicate_part_filter}
                       and x.session_id in (select id from tree)) = 1
            ), projected as (
              select
                case json_extract(n.data, '$.tool')
                  when 'patch' then 'patch' when 'edit' then 'edit' else 'apply_patch' end as source,
                coalesce(n.part_created, n.prompt_created, 0) as event_created,
                n.part_row as event_order,
                n.prompt_row as message_row,
                substr(cast(n.prompt_id as text), 1, ?) as message_id,
                length(cast(cast(n.prompt_id as text) as blob)) as message_id_bytes,
                substr(cast(n.execution_id as text), 1, ?) as execution_id,
                length(cast(cast(n.execution_id as text) as blob)) as execution_id_bytes,
                n.prompt_created as message_created,
                n.prompt_updated as message_updated,
                null as message_bytes,
                n.directory,
                d.key as diff_index,
                substr({native_path}, 1, ?) as file,
                length(cast({native_path} as blob)) as file_bytes,
                coalesce(case json_extract({native_diff}, '$.status')
                    when 'added' then 'added' when 'modified' then 'modified'
                    when 'deleted' then 'deleted' when 'moved' then 'moved' end,
                  case json_extract({native_diff}, '$.type')
                    when 'add' then 'added' when 'update' then 'modified'
                    when 'delete' then 'deleted' when 'move' then 'moved'
                  end) as status,
                8 as status_bytes,
                case when json_type({native_diff}, '$.additions') = 'integer'
                  then json_extract({native_diff}, '$.additions') end as additions,
                json_type({native_diff}, '$.additions') as additions_type,
                case when json_type({native_diff}, '$.deletions') = 'integer'
                  then json_extract({native_diff}, '$.deletions') end as deletions,
                json_type({native_diff}, '$.deletions') as deletions_type,
                json_type({native_diff}, '$.patch') as patch_type,
                coalesce(length(cast(json_extract({native_diff}, '$.patch') as blob)), 0) as patch_bytes,
                null as before_type, 0 as before_bytes, null as after_type, 0 as after_bytes,
                substr(case when json_extract({native_diff}, '$.type') = 'move'
                  then json_extract({native_diff}, '$.filePath') end, 1, ?) as from_file,
                coalesce(length(cast(case when json_extract({native_diff}, '$.type') = 'move'
                  then json_extract({native_diff}, '$.filePath') end as blob)), 0) as from_file_bytes,
                n.part_row,
                substr(cast(n.id as text), 1, ?) as part_id,
                length(cast(cast(n.id as text) as blob)) as part_id_bytes,
                n.part_created, n.part_updated, length(cast(n.data as blob)) as part_bytes,
                n.tool_message_row,
                substr(cast(n.tool_message_id as text), 1, ?) as tool_message_id,
                length(cast(cast(n.tool_message_id as text) as blob)) as tool_message_id_bytes,
                n.tool_message_updated
              from native n
              join json_each(case when json_valid(n.data) then n.data else '{{}}' end,
                             '$.state.metadata.files') d
              where json_extract(n.data, '$.tool') in ('apply_patch', 'patch', 'edit')
                and json_type(n.data, '$.state.metadata.files') = 'array'
                and d.type = 'object'
                and typeof({native_path}) = 'text'
              union all
              select
                'edit', coalesce(n.part_created, n.prompt_created, 0), n.part_row,
                n.prompt_row, substr(cast(n.prompt_id as text), 1, ?),
                length(cast(cast(n.prompt_id as text) as blob)),
                substr(cast(n.execution_id as text), 1, ?),
                length(cast(cast(n.execution_id as text) as blob)),
                n.prompt_created, n.prompt_updated, null, n.directory,
                0, substr(json_extract(n.data, '$.state.metadata.filediff.file'), 1, ?),
                length(cast(json_extract(n.data, '$.state.metadata.filediff.file') as blob)),
                'modified', 8,
                case when json_type(n.data, '$.state.metadata.filediff.additions') = 'integer'
                  then json_extract(n.data, '$.state.metadata.filediff.additions') end,
                json_type(n.data, '$.state.metadata.filediff.additions'),
                case when json_type(n.data, '$.state.metadata.filediff.deletions') = 'integer'
                  then json_extract(n.data, '$.state.metadata.filediff.deletions') end,
                json_type(n.data, '$.state.metadata.filediff.deletions'),
                json_type(n.data, '$.state.metadata.filediff.patch'),
                coalesce(length(cast(json_extract(n.data, '$.state.metadata.filediff.patch') as blob)), 0),
                null, 0, null, 0, null, 0,
                n.part_row, substr(cast(n.id as text), 1, ?),
                length(cast(cast(n.id as text) as blob)), n.part_created, n.part_updated,
                length(cast(n.data as blob)), n.tool_message_row,
                substr(cast(n.tool_message_id as text), 1, ?),
                length(cast(cast(n.tool_message_id as text) as blob)), n.tool_message_updated
              from native n
              where json_extract(n.data, '$.tool') = 'edit'
                and coalesce(json_type(n.data, '$.state.metadata.files'), '') != 'array'
                and json_type(n.data, '$.state.metadata.filediff') = 'object'
                and json_type(n.data, '$.state.metadata.filediff.file') = 'text'
              union all
              select
                'write', coalesce(n.part_created, n.prompt_created, 0), n.part_row,
                n.prompt_row, substr(cast(n.prompt_id as text), 1, ?),
                length(cast(cast(n.prompt_id as text) as blob)),
                substr(cast(n.execution_id as text), 1, ?),
                length(cast(cast(n.execution_id as text) as blob)),
                n.prompt_created, n.prompt_updated, null, n.directory,
                 0, substr({native_write_path}, 1, ?),
                 length(cast({native_write_path} as blob)),
                 case json_type(n.data, '$.state.metadata.exists')
                   when 'true' then 'modified' when 'false' then 'added' end,
                 case when json_type(n.data, '$.state.metadata.exists') in ('true', 'false') then 8 else 0 end,
                 null, null, null, null, null, 0, null, 0, null, 0, null, 0,
                n.part_row, substr(cast(n.id as text), 1, ?),
                length(cast(cast(n.id as text) as blob)), n.part_created, n.part_updated,
                length(cast(n.data as blob)), n.tool_message_row,
                substr(cast(n.tool_message_id as text), 1, ?),
                length(cast(cast(n.tool_message_id as text) as blob)), n.tool_message_updated
              from native n
              where json_extract(n.data, '$.tool') = 'write'
                and typeof({native_write_path}) = 'text'
                and length({native_write_path}) > 0
            )
            select * from projected
            {"where source = ? and diff_index = ?" if locator else ""}
            order by event_created, event_order, cast(diff_index as integer)
            limit ?
            """
            native_params: list[object] = [root_id]
            if locator:
                native_params.append(locator[1])
            native_params += [cap] * 6
            native_params += [cap] * 5
            native_params += [cap] * 5
            if locator:
                native_params.extend((locator[0], locator[2]))
            native_params.append(CHANGE_SUMMARY_LIMIT + 1)
            rows.extend(
                dict(row)
                for row in debug.query_rows(
                    conn,
                    self._detail_sql(
                        native_sql,
                        conn,
                        part_row=locator[1] if locator else None,
                        change_candidates=True,
                    ),
                    native_params,
                    label="opencode.changes_native",
                    explain=True,
                )
            )

        for row in rows:
            row["root_directory"] = root_directory
            row["file"] = self._change_path(row["file"], row["directory"], root_directory)
            row["from_file"] = self._change_path(row["from_file"], row["directory"], root_directory)
        rows.sort(
            key=lambda row: (
                row["event_created"] if isinstance(row["event_created"], (int, float)) else 0,
                row["event_order"] if isinstance(row["event_order"], int) else 0,
                int(row["diff_index"]) if str(row["diff_index"]).isdigit() else 0,
                row["source"],
            )
        )
        debug.event(
            "opencode.changes_metadata_ready",
            rows=len(rows),
            keyed=locator is not None,
            limit_reached=len(rows) > CHANGE_SUMMARY_LIMIT,
        )
        return rows[: CHANGE_SUMMARY_LIMIT + 1], revision

    @debug.timed("opencode.change_files")
    def session_change_files(self, root_id: str) -> dict:
        result = {"files": [], "limitations": list(CHANGE_LIMITATIONS), "truncated": False}
        if self.demo or not self.supports_session_changes:
            debug.event("opencode.changes_rejected", reason="demo_or_unsupported")
            return result
        uri = "file:" + quote(os.path.abspath(self.db)) + "?mode=ro"
        try:
            before = self._conversation_database_manifest()
            with closing(self._change_connection(uri)) as conn:
                conn.execute("begin")
                rows, revision = self._change_rows(conn, root_id)
            if any(self._change_uses_global_revision(row) for row in rows) and (
                before is None
                or revision != before
                or self._conversation_database_manifest() != before
            ):
                result["limitations"].append("The source changed while summaries were read.")
                result["truncated"] = True
                debug.event("opencode.changes_rejected", reason="source_changed")
                return result
        except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError) as exc:
            debug.event("opencode.changes_read_failed", error_type=type(exc).__name__)
            raise ValueError("Could not read recorded change summaries.") from None

        if len(rows) > CHANGE_SUMMARY_LIMIT:
            rows = rows[:CHANGE_SUMMARY_LIMIT]
            result["truncated"] = True
            result["limitations"].append("The retained change occurrence limit was reached.")
        files = {}
        evidence = {}
        metadata_bytes = 0
        for row in rows:
            bounded_names = ["message_id_bytes", "execution_id_bytes", "file_bytes"]
            if row["source"] != "snapshot":
                bounded_names.extend(("part_id_bytes", "tool_message_id_bytes"))
            if row["from_file"] is not None:
                bounded_names.append("from_file_bytes")
            if any(
                not isinstance(row[name], int) or row[name] > CHANGE_METADATA_BYTES
                for name in bounded_names
            ) or (
                isinstance(row["status_bytes"], int) and row["status_bytes"] > CHANGE_METADATA_BYTES
            ):
                result["truncated"] = True
                continue
            message_id, execution_id, path = row["message_id"], row["execution_id"], row["file"]
            if not all(
                isinstance(value, str) and value for value in (message_id, execution_id, path)
            ):
                continue
            status = (
                row["status"] if isinstance(row["status"], str) and row["status"] else "unknown"
            )
            additions = self._change_count(row["additions"], row["additions_type"])
            deletions = self._change_count(row["deletions"], row["deletions_type"])
            available = (row["patch_type"] == "text" and row["patch_bytes"] > 0) or (
                row["before_type"] == "text" and row["after_type"] == "text"
            )
            key = self._change_key(root_id, revision, row)
            size = sum(
                len(value.encode("utf-8"))
                for value in (
                    key,
                    message_id,
                    execution_id,
                    path,
                    status,
                    row["source"],
                    row["from_file"] or "",
                )
            )
            if metadata_bytes + size > CHANGE_SUMMARY_BYTES:
                result["truncated"] = True
                result["limitations"].append("The retained change metadata byte limit was reached.")
                break
            metadata_bytes += size
            edit = {
                "key": key,
                "message_id": message_id,
                "execution_id": execution_id,
                "file": path,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "available": available,
                "source": row["source"],
            }
            if row["from_file"]:
                edit["from_file"] = row["from_file"]
            item = files.get(path)
            if item is None:
                item = {
                    "file": path,
                    "status": status,
                    "additions": None,
                    "deletions": None,
                    "edits": [],
                }
                files[path] = item
            elif item["status"] != status:
                item["status"] = "mixed"
            for name, count in (("additions", additions), ("deletions", deletions)):
                if not item["edits"]:
                    item[name] = count
                elif item[name] is not None and count is not None:
                    item[name] += count
                else:
                    item[name] = None
            item["edits"].append(edit)
            for recorded_path in (path, row["from_file"]):
                if recorded_path:
                    group = evidence.setdefault(
                        (execution_id, message_id, recorded_path),
                        {"snapshot": set(), "tool": set()},
                    )
                    group["snapshot" if row["source"] == "snapshot" else "tool"].add(path)
        # A snapshot can contain both a tool edit and later shell changes. Keep
        # both records, but do not pretend their line counts can be added safely.
        for group in evidence.values():
            if group["snapshot"] and group["tool"]:
                for path in group["snapshot"] | group["tool"]:
                    files[path].update(additions=None, deletions=None, counts_overlap=True)
        if any(item.get("counts_overlap") for item in files.values()):
            result["limitations"].append(
                "Overlapping snapshot/tool records have unknown (?) file totals; individual record counts remain available."
            )
        if result["truncated"] and not any("limit" in text for text in result["limitations"]):
            result["limitations"].append("Some oversized change metadata was omitted.")
        result["files"] = list(files.values())
        if debug.enabled():
            debug.event(
                "opencode.changes_files_ready",
                files=len(files),
                edits=sum(len(f["edits"]) for f in files.values()),
                available=sum(e["available"] for f in files.values() for e in f["edits"]),
                metadata_bytes=metadata_bytes,
                truncated=result["truncated"],
            )
        return result

    @debug.timed("opencode.change_diff")
    def session_change_diff(self, root_id: str, change_key: str) -> dict | None:
        if (
            self.demo
            or not self.supports_session_changes
            or not isinstance(change_key, str)
            or len(change_key) > 160
        ):
            debug.event("opencode.change_diff_rejected", reason="demo_unsupported_or_key_shape")
            return None
        match = re.fullmatch(
            r"occhg1:(snapshot|apply_patch|patch|edit|write):(-?[0-9]{1,19}):([0-9]{1,19}):[0-9a-f]{64}",
            change_key,
        )
        if match is None:
            debug.event("opencode.change_diff_rejected", reason="key_format")
            return None
        source, row_id, index = match.groups()
        locator = (source, int(row_id), int(index))
        if not -(2**63) <= locator[1] < 2**63 or locator[2] >= 2**63:
            debug.event("opencode.change_diff_rejected", reason="locator_range")
            return None
        uri = "file:" + quote(os.path.abspath(self.db)) + "?mode=ro"
        try:
            initial = self._conversation_database_manifest()
            with closing(self._change_connection(uri)) as conn:
                conn.execute("begin")
                # The locator only narrows the query. Rebuild the identity from
                # owned live rows and compare the full key before reading a body.
                rows, revision = self._change_rows(conn, root_id, locator)
                selected = next(
                    (
                        row
                        for row in rows[:CHANGE_SUMMARY_LIMIT]
                        if self._change_key(root_id, revision, row) == change_key
                    ),
                    None,
                )
                if selected is None or (
                    self._change_uses_global_revision(selected)
                    and (initial is None or revision != initial)
                ):
                    debug.event("opencode.change_diff_rejected", reason="stale_or_unowned_key")
                    return None
                if selected["source"] == "snapshot":
                    safe = "case when json_valid(m.data) then m.data else '{}' end"
                    diff = "case when d.type = 'object' then d.value else '{}' end"
                    body = debug.query_one(
                        conn,
                        f"""select
                          json_extract({diff}, '$.file') as file,
                          json_type({diff}, '$.patch') as patch_type,
                          substr(json_extract({diff}, '$.patch'), 1, ?) as patch,
                          coalesce(length(cast(json_extract({diff}, '$.patch') as blob)), 0) as patch_bytes,
                          json_type({diff}, '$.before') as before_type,
                          substr(json_extract({diff}, '$.before'), 1, ?) as before,
                          coalesce(length(cast(json_extract({diff}, '$.before') as blob)), 0) as before_bytes,
                          json_type({diff}, '$.after') as after_type,
                          substr(json_extract({diff}, '$.after'), 1, ?) as after,
                          coalesce(length(cast(json_extract({diff}, '$.after') as blob)), 0) as after_bytes
                        from message m join json_each({safe}, '$.summary.diffs') d
                        where m.rowid = ? and m.id = ? and m.session_id = ? and d.key = ?
                          and d.type = 'object'""",
                        [
                            CHANGE_DIFF_BYTES + 1,
                            CHANGE_LEGACY_INPUT_BYTES + 1,
                            CHANGE_LEGACY_INPUT_BYTES + 1,
                            selected["message_row"],
                            selected["message_id"],
                            selected["execution_id"],
                            selected["diff_index"],
                        ],
                        label="opencode.changes_snapshot_body",
                    )
                elif selected["source"] in ("apply_patch", "patch", "edit"):
                    files_path = f"$.state.metadata.files[{int(selected['diff_index'])}].patch"
                    patch_paths = (
                        (files_path, "$.state.metadata.filediff.patch")
                        if selected["source"] == "edit"
                        else (files_path, files_path)
                    )
                    body = debug.query_one(
                        conn,
                        """select
                          ? as file,
                          coalesce(json_type(p.data, ?), json_type(p.data, ?)) as patch_type,
                          substr(coalesce(json_extract(p.data, ?), json_extract(p.data, ?)), 1, ?) as patch,
                          coalesce(length(cast(coalesce(json_extract(p.data, ?), json_extract(p.data, ?)) as blob)), 0) as patch_bytes,
                          null as before_type, null as before, 0 as before_bytes,
                          null as after_type, null as after, 0 as after_bytes
                        from part p
                        where p.rowid = ? and p.id = ? and p.message_id = ? and p.session_id = ?""",
                        [
                            selected["file"],
                            *patch_paths,
                            *patch_paths,
                            CHANGE_DIFF_BYTES + 1,
                            *patch_paths,
                            selected["part_row"],
                            selected["part_id"],
                            selected["tool_message_id"],
                            selected["execution_id"],
                        ],
                        label="opencode.changes_tool_body",
                    )
                else:
                    body = None
            if body is None or (
                self._change_uses_global_revision(selected)
                and self._conversation_database_manifest() != initial
            ):
                debug.event(
                    "opencode.change_diff_rejected", reason="missing_body_or_source_changed"
                )
                return None
        except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError) as exc:
            debug.event(
                "opencode.change_diff_rejected", reason="read_error", error_type=type(exc).__name__
            )
            return None

        path = (
            self._change_path(body["file"], selected["directory"], selected["root_directory"])
            if selected["source"] == "snapshot"
            else body["file"]
        )
        if not isinstance(path, str) or path != selected["file"]:
            debug.event("opencode.change_diff_rejected", reason="path_mismatch")
            return None
        if body["patch_type"] == "text" and body["patch_bytes"] > 0:
            patch, clipped = self._clip_utf8(body["patch"], CHANGE_DIFF_BYTES)
            debug.event(
                "opencode.change_diff_ready",
                generated=False,
                source_bytes=body["patch_bytes"],
                truncated=clipped or body["patch_bytes"] > CHANGE_DIFF_BYTES,
            )
            return {
                "file": path,
                "patch": patch,
                "truncated": clipped or body["patch_bytes"] > CHANGE_DIFF_BYTES,
                "limitation": (
                    f"Recorded {selected['source']} patch; content was truncated to the output limit."
                    if clipped or body["patch_bytes"] > CHANGE_DIFF_BYTES
                    else (
                        "Native per-prompt snapshot patch; it may include concurrent or later-reverted edits."
                        if selected["source"] == "snapshot"
                        else (
                            "Recorded patch."
                            if selected["source"] == "patch"
                            else f"Recorded {selected['source']} patch."
                        )
                    )
                ),
            }
        if body["before_type"] != "text" or body["after_type"] != "text":
            debug.event("opencode.change_diff_rejected", reason="no_recorded_content")
            return None
        before_text, before_clipped = self._clip_utf8(body["before"], CHANGE_LEGACY_INPUT_BYTES)
        after_text, after_clipped = self._clip_utf8(body["after"], CHANGE_LEGACY_INPUT_BYTES)
        before_lines = before_text.splitlines(keepends=True)
        after_lines = after_text.splitlines(keepends=True)
        lines_clipped = (
            len(before_lines) > CHANGE_LEGACY_LINE_LIMIT
            or len(after_lines) > CHANGE_LEGACY_LINE_LIMIT
        )
        before_lines = before_lines[:CHANGE_LEGACY_LINE_LIMIT]
        after_lines = after_lines[:CHANGE_LEGACY_LINE_LIMIT]
        chunks = difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="a/" + path,
            tofile="b/" + path,
        )
        out, used, output_clipped = [], 0, False
        for chunk in chunks:
            # difflib preserves missing EOF newlines, but does not insert Git's
            # marker; joining these chunks verbatim would glue -old and +new.
            if not chunk.endswith("\n"):
                chunk += "\n\\ No newline at end of file\n"
            raw = chunk.encode("utf-8")
            if used + len(raw) > CHANGE_DIFF_BYTES:
                room = CHANGE_DIFF_BYTES - used
                if room > 0:
                    out.append(raw[:room].decode("utf-8", "ignore"))
                output_clipped = True
                break
            out.append(chunk)
            used += len(raw)
        truncated = (
            before_clipped
            or after_clipped
            or lines_clipped
            or output_clipped
            or body["before_bytes"] > CHANGE_LEGACY_INPUT_BYTES
            or body["after_bytes"] > CHANGE_LEGACY_INPUT_BYTES
        )
        debug.event(
            "opencode.change_diff_ready",
            generated=True,
            source_bytes=body["before_bytes"] + body["after_bytes"],
            truncated=truncated,
        )
        return {
            "file": path,
            "patch": "".join(out),
            "truncated": truncated,
            "limitation": (
                "Generated from retained before/after snapshots; source or output limits truncated it."
                if truncated
                else "Generated from retained before/after snapshots; it may include concurrent or later-reverted edits."
            ),
        }

    @staticmethod
    def _conversation_columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
        required = {
            "session": {"id", "parent_id"},
            "message": {"id", "session_id", "data"},
            "part": {"id", "message_id", "data"},
        }
        columns = {
            table: {row[1] for row in conn.execute(f"pragma table_info({table})")}
            for table in required
        }
        return columns if all(required[t] <= columns[t] for t in required) else {}

    def supports_conversation(self, sid: str) -> bool:
        """Schema capability only; retained text and exact ownership are checked on read."""
        if self.demo:
            return False
        try:
            return bool(self._conversation_columns(self.conn))
        except sqlite3.Error:
            return False

    def conversation_manifest(self, root_id: str):
        """Fingerprint one execution tree's row revisions, never its raw text."""
        if self.demo or not isinstance(root_id, str) or not root_id:
            return None
        if (
            self.has_v2
            and self.conn.execute(
                "select 1 from main.session_v2 where id = ?", [root_id]
            ).fetchone()
        ):
            return self._v2_conversation_manifest(root_id)
        uri = "file:" + quote(os.path.abspath(self.db)) + "?mode=ro"
        try:
            initial = os.stat(self.db)
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                conn.execute("begin")
                install_views(conn)
                inspected_ms = time.time_ns() // 1_000_000
                schema = {
                    table: list(conn.execute(f"pragma table_info({table})"))
                    for table in ("session", "message", "part", "event_sequence")
                }
                columns = {table: {row[1] for row in rows} for table, rows in schema.items()}
                required = {
                    "session": {"id", "parent_id"},
                    "message": {"id", "session_id", "time_created", "time_updated", "data"},
                    "part": {
                        "id",
                        "session_id",
                        "message_id",
                        "time_created",
                        "time_updated",
                        "data",
                    },
                }
                if any(
                    not names <= columns[table]
                    or [row[1] for row in schema[table] if row[5]] != ["id"]
                    for table, names in required.items()
                ):
                    # Missing revisions or ambiguous IDs cannot support a root-local shortcut.
                    return self._conversation_database_manifest()
                tree = """with recursive tree(id) as (
                  select id from session where id = ?
                  union
                  select s.id from session s join tree on s.parent_id = tree.id
                ) """
                executions = list(
                    conn.execute(
                        tree
                        + "select id, parent_id from session where id in (select id from tree) order by id",
                        [root_id],
                    )
                )
                if not executions:
                    return None
                digest = hashlib.sha256()
                digest.update(json.dumps(executions, separators=(",", ":")).encode())
                for table in ("message", "part"):
                    extra = (
                        ", r.message_id"
                        if table == "part"
                        else (", r.parent_id" if "parent_id" in columns[table] else "")
                    )
                    # IN bounds the scan by session before sorting; a join can make
                    # SQLite walk the entire ID index once for every root instead.
                    rows = list(
                        conn.execute(
                            tree
                            + f"select r.id, r.session_id, r.time_created, r.time_updated{extra} "
                            f"from {table} r where r.session_id in (select id from tree) order by r.id",
                            [root_id],
                        )
                    )
                    # Do not bless a revision that can still collide with another write
                    # in the same millisecond. Unknown/future clocks also take the full read.
                    if any(
                        not isinstance(row[3], int) or row[3] >= inspected_ms - 1 for row in rows
                    ):
                        return None
                    digest.update(table.encode())
                    digest.update(json.dumps(rows, separators=(",", ":")).encode())
                if {"aggregate_id", "seq"} <= columns["event_sequence"]:
                    rows = list(
                        conn.execute(
                            tree + "select e.aggregate_id, e.seq from event_sequence e "
                            "where e.aggregate_id in (select id from tree) order by e.aggregate_id, e.seq",
                            [root_id],
                        )
                    )
                    digest.update(json.dumps(rows, separators=(",", ":")).encode())
                current = os.stat(self.db)
                identity = (initial.st_dev, initial.st_ino)
                if identity != (current.st_dev, current.st_ino):
                    return None
                return [
                    "opencode-root-v1",
                    *identity,
                    "parent_id" in columns["message"],
                    digest.hexdigest(),
                ]
        except (sqlite3.Error, OSError, ValueError, TypeError):
            return None

    def _v2_conversation_manifest(self, root_id: str):
        """Fingerprint one v2 tree from keyed metadata, excluding message JSON."""
        uri = "file:" + quote(os.path.abspath(self.db)) + "?mode=ro"
        try:
            initial = os.stat(self.db)
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                conn.execute("begin")
                inspected_ms = time.time_ns() // 1_000_000
                schema = {
                    table: list(conn.execute(f"pragma main.table_info({table})"))
                    for table in ("session_v2", "session_message")
                }
                required = {
                    "session_v2": {"id", "parent_id"},
                    "session_message": {
                        "id",
                        "session_id",
                        "type",
                        "seq",
                        "time_created",
                        "time_updated",
                    },
                }
                if any(
                    not names <= {row[1] for row in schema[table]}
                    or [row[1] for row in schema[table] if row[5]] != ["id"]
                    for table, names in required.items()
                ):
                    return self._conversation_database_manifest()
                tree = """with recursive tree(id) as (
                  select id from main.session_v2 where id = ?
                  union
                  select s.id from main.session_v2 s join tree on s.parent_id = tree.id
                ) """
                legacy_columns = {row[1] for row in conn.execute("pragma main.table_info(session)")}
                if {"id", "parent_id"} <= legacy_columns and conn.execute(
                    tree
                    + "select 1 from main.session s where s.parent_id in (select id from tree) "
                    "and not exists (select 1 from main.session_v2 v where v.id = s.id) limit 1",
                    [root_id],
                ).fetchone():
                    return self._conversation_database_manifest()
                executions = list(
                    conn.execute(
                        tree + "select id, parent_id from main.session_v2 "
                        "where id in (select id from tree) order by id",
                        [root_id],
                    )
                )
                if not executions:
                    return None
                rows = list(
                    conn.execute(
                        tree
                        + "select m.id, m.session_id, m.type, m.seq, m.time_created, m.time_updated "
                        + "from main.session_message m where m.session_id in (select id from tree) "
                        + "order by m.session_id, m.seq, m.id",
                        [root_id],
                    )
                )
                if any(not isinstance(row[5], int) or row[5] >= inspected_ms - 1 for row in rows):
                    return None
                digest = hashlib.sha256()
                digest.update(json.dumps(executions, separators=(",", ":")).encode())
                digest.update(json.dumps(rows, separators=(",", ":")).encode())
                event_columns = {
                    row[1] for row in conn.execute("pragma main.table_info(event_sequence)")
                }
                if {"aggregate_id", "seq"} <= event_columns:
                    events = list(
                        conn.execute(
                            tree + "select aggregate_id, seq from main.event_sequence "
                            "where aggregate_id in (select id from tree) order by aggregate_id",
                            [root_id],
                        )
                    )
                    digest.update(json.dumps(events, separators=(",", ":")).encode())
                current = os.stat(self.db)
                identity = (initial.st_dev, initial.st_ino)
                if identity != (current.st_dev, current.st_ino):
                    return None
                return ["opencode-root-v2", *identity, digest.hexdigest()]
        except (sqlite3.Error, OSError, ValueError, TypeError):
            return None

    def _conversation_database_manifest(self):
        from opentab.conversations.reader import source_manifest

        # The duplicated WAL-index header publishes the committed WAL snapshot. Hash
        # only that stable prefix: locks and reader marks later in -shm churn on reads.
        db = os.path.abspath(self.db)
        paths = [db] + ([db + "-wal"] if os.path.exists(db + "-wal") else [])
        files = source_manifest(paths)
        if files is None:
            return None
        shm = db + "-shm"
        try:
            with open(shm, "rb") as stream:
                initial = os.fstat(stream.fileno())
                header = stream.read(96)
                final = os.fstat(stream.fileno())
            current = os.stat(shm)
        except FileNotFoundError:
            wal_header = None
        except OSError:
            return None
        else:

            def stamp(info):
                return (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )

            if (
                len(header) != 96
                or header[:48] != header[48:]
                or stamp(initial) != stamp(final)
                or stamp(initial) != stamp(current)
            ):
                return None
            wal_header = hashlib.sha256(header[:48]).hexdigest()
        return [files, wal_header]

    def conversation_source(self, root_id: str, execution_id: str | None = None) -> dict:
        """Read original retained messages for one exact execution, never accounting rows."""
        from opentab.conversations.reader import ConversationError

        if self.demo:
            raise ConversationError("conversation_unavailable", "Conversation is disabled in demo.")
        selected = root_id if execution_id is None else execution_id
        if (
            not isinstance(root_id, str)
            or not root_id
            or not isinstance(selected, str)
            or not selected
        ):
            raise ConversationError(
                "conversation_unavailable", "Conversation execution is unavailable."
            )
        uri = "file:" + quote(os.path.abspath(self.db)) + "?mode=ro"
        try:
            # A separate connection sees commits since startup and cannot read an unlinked
            # database through the accounting connection's still-open file descriptor.
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("begin")
                install_views(conn)
                columns = self._conversation_columns(conn)
                if not columns:
                    raise ConversationError(
                        "conversation_unavailable", "Conversation schema is unavailable."
                    )
                executions = [
                    dict(row)
                    for row in conn.execute(
                        """with recursive tree(id) as (
                      select id from session where id = ?
                      union
                      select s.id from session s join tree on s.parent_id = tree.id
                    ) select s.id, s.parent_id from session s join tree on s.id = tree.id
                    order by s.id""",
                        [root_id],
                    )
                ]
                parents = {}
                for execution in executions:
                    sid = execution["id"]
                    parent = execution["parent_id"]
                    if (
                        not isinstance(sid, str)
                        or not sid
                        or sid in parents
                        or (parent is not None and not isinstance(parent, str))
                    ):
                        raise ConversationError(
                            "conversation_unavailable", "Conversation ownership is ambiguous."
                        )
                    parents[sid] = parent
                if root_id not in parents or selected not in parents:
                    raise ConversationError(
                        "conversation_unavailable", "Conversation execution is unavailable."
                    )
                resolved = set()
                for sid in parents:
                    path = set()
                    while sid in parents and sid not in resolved:
                        if sid in path:
                            raise ConversationError(
                                "conversation_unavailable", "Conversation ownership is ambiguous."
                            )
                        path.add(sid)
                        sid = parents[sid]
                    resolved.update(path)

                # Without part.session_id the message ID is the entire ownership key.
                # Reject duplicate IDs even on schemas without primary-key constraints.
                duplicate = conn.execute(
                    "select id from message where id in "
                    "(select id from message where session_id = ?) "
                    "group by id having count(*) > 1 limit 1",
                    [selected],
                ).fetchone()
                if duplicate:
                    raise ConversationError(
                        "conversation_unavailable", "Conversation message identity is ambiguous."
                    )
                message_order = (
                    "m.seq, m.id"
                    if "seq" in columns["message"]
                    else (
                        "m.time_created, m.id"
                        if "time_created" in columns["message"]
                        else f"{_TL_TS}, m.id"
                    )
                )
                part_order = (
                    "p.part_index, p.id"
                    if "part_index" in columns["part"]
                    else ("p.time_created, p.id" if "time_created" in columns["part"] else "p.id")
                )
                timestamp = (
                    f"coalesce({_TL_TS}, m.time_created)"
                    if "time_created" in columns["message"]
                    else _TL_TS
                )
                parent = (
                    "m.parent_id"
                    if "parent_id" in columns["message"]
                    else "json_extract(m.data, '$.parentID')"
                )
                records = []
                by_id = {}
                for row in conn.execute(
                    f"select m.id, json_extract(m.data, '$.role') as role, "
                    f"{timestamp} as timestamp, {parent} as parent_id from message m "
                    "where m.session_id = ? and json_extract(m.data, '$.role') in ('user', 'assistant') "
                    "and coalesce(json_extract(m.data, '$.__opentab_type'), '') != 'compaction' "
                    f"order by {message_order}",
                    [selected],
                ):
                    mid = row["id"]
                    if not isinstance(mid, str) or not mid:
                        raise ConversationError(
                            "conversation_unavailable",
                            "Conversation message identity is unavailable.",
                        )
                    record = {
                        "id": "oc:" + mid,
                        "message_id": mid,
                        "record_id": mid,
                        "parent_id": row["parent_id"],
                        "execution_id": selected,
                        "role": row["role"],
                        "timestamp": row["timestamp"],
                        "origin": "recorded-message",
                        "source": {"message_id": mid},
                        "parts": [],
                    }
                    records.append(record)
                    by_id[mid] = record
                ownership = (
                    "and p.session_id = m.session_id" if "session_id" in columns["part"] else ""
                )
                text_rows = (
                    f"from part p join message m on p.message_id = m.id {ownership} "
                    "where m.session_id = ? and json_extract(m.data, '$.role') in ('user', 'assistant') "
                    "and coalesce(json_extract(m.data, '$.__opentab_type'), '') != 'compaction' "
                    "and json_extract(p.data, '$.type') = 'text' "
                    "and coalesce(json_extract(p.data, '$.synthetic'), 0) in (0, '') "
                    "and json_type(p.data, '$.text') = 'text'"
                )
                # Preflight UTF-8 bytes inside SQLite; neither oversized text nor raw
                # tool/reasoning/attachment blobs cross the SQL result boundary.
                size = conn.execute(
                    "select coalesce(sum(length(cast(json_extract(p.data, '$.text') as blob))), 0) "
                    + text_rows,
                    [selected],
                ).fetchone()[0]
                if size > CONVERSATION_TEXT_BUDGET:
                    raise ConversationError(
                        "conversation_too_large", "Conversation exceeds the source text budget."
                    )
                part_ids = set()
                for row in conn.execute(
                    "select p.id, p.message_id, json_extract(p.data, '$.text') as text "
                    + text_rows
                    + f" order by {part_order}",
                    [selected],
                ):
                    pid = row["id"]
                    if not isinstance(pid, str) or not pid or pid in part_ids:
                        raise ConversationError(
                            "conversation_unavailable", "Conversation part identity is ambiguous."
                        )
                    part_ids.add(pid)
                    by_id[row["message_id"]]["parts"].append(
                        {
                            "id": pid,
                            "text": row["text"],
                            "source": {"part_id": pid},
                        }
                    )
                result = {
                    "records": records,
                    "execution_id": selected,
                    "executions": executions,
                    "limitations": [
                        "retained_messages_only",
                        "synthetic_text_excluded",
                        "non_text_parts_excluded",
                    ],
                    "ordering": f"messages: {message_order}; parts: {part_order}",
                }
                digest = hashlib.sha256()
                for chunk in json.JSONEncoder(sort_keys=True, separators=(",", ":")).iterencode(
                    {"root_id": root_id, **result}
                ):
                    digest.update(chunk.encode("utf-8"))
                result["snapshot"] = digest.hexdigest()
                return result
        except (sqlite3.Error, OSError, UnicodeError):
            raise ConversationError(
                "conversation_unavailable", "Conversation source is unavailable."
            ) from None

    def _owns_node(self, root_id: str, node_id: str) -> bool:
        if self.demo or not root_id or not node_id:
            return False
        return (
            self.conn.execute(
                """with recursive tree(id) as (
              select id from session where id = ?
              union
              select child.id from session child join tree on child.parent_id = tree.id
            ) select 1 from tree where id = ?""",
                [root_id, node_id],
            ).fetchone()
            is not None
        )

    def node_timeline(self, root_id: str, node_id: str) -> list[dict] | None:
        """Own execution turns; None means unsupported or unproven membership."""
        if not self.supports_message_timeline or not self._owns_node(root_id, node_id):
            return None
        return self._message_timeline(node_id, own=True)

    def node_turn_content(self, root_id: str, node_id: str, content_key: str | None = None) -> dict:
        if not self.supports_tool_breakdown or not self._owns_node(root_id, node_id):
            return {}
        # Assistant message ids are the timeline keys, never user-message ids.
        keys = {
            r[0]
            for r in self.conn.execute(
                "select id from message where session_id = ? "
                "and json_extract(data, '$.role') = 'assistant'",
                [node_id],
            )
        }
        if content_key is not None and content_key not in keys:
            return {}
        return self._turn_content(node_id, content_key, own=True)

    def node_prompt(self, workflow_id: str, node_id: str) -> str | None:
        """Read the exact child's first user text, independently of billed turns."""
        if self.demo or node_id == workflow_id or not self.supports_tool_breakdown:
            return None
        message_order = "m.seq" if "seq" in self.message_columns else _TL_TS
        part_order = "p.part_index" if "part_index" in self.part_columns else "p.rowid"
        sql = f"""
        with recursive tree(id) as (
          select id from session where id = ?
          union
          select child.id from session child join tree on child.parent_id = tree.id
        )
        select m.id, json_extract(p.data, '$.text') as text
        from message m join part p on p.message_id = m.id and p.session_id = m.session_id
        where m.session_id = ? and m.session_id in (select id from tree)
          and json_extract(m.data, '$.role') = 'user'
          and json_extract(p.data, '$.type') = 'text'
          and json_type(p.data, '$.text') = 'text'
        order by {message_order}, m.rowid, {part_order}, p.rowid
        """
        current = None
        parts: list[str] = []
        for mid, text in self.conn.execute(sql, [workflow_id, node_id]):
            if mid != current:
                if any(part.strip() for part in parts):
                    return "\n\n".join(parts)
                current, parts = mid, []
            parts.append(text)
        return "\n\n".join(parts) if any(part.strip() for part in parts) else None

    def message_timeline_all(self) -> dict[str, list[dict]]:
        # The whole-corpus Turns for `--export`: every root session's timeline in ONE
        # grouped scan, keyed by root id. The per-session message_timeline restricts to
        # one subtree via a recursive CTE and re-scans the message table each call, so an
        # export that walks every session is O(sessions x messages) -- measured at
        # ~200ms/session, 138s over 689 sessions. This maps every session to its root in
        # a single recursive CTE (the workflows() `roots`/`tree` shape) and scans the
        # message table once, then groups in Python -- ~100x faster on a big DB. The TUI
        # keeps the lazy per-session path (drill-in pays for the one session you open).
        if not self.supports_message_timeline:
            return {}
        message_columns = getattr(self, "message_columns", set())
        seq_order = "m.seq" if "seq" in message_columns else _TL_TS
        v2_marker = (
            "json_extract(m.data, '$.__opentab_v2') = 1" if "seq" in message_columns else "0"
        )
        sql = f"""
        with recursive roots(id) as (
          select id from session where parent_id is null
        ), tree(root_id, id, depth) as (
          select id, id, 0 from roots
          union all
          select tree.root_id, child.id, tree.depth + 1
          from session child join tree on child.parent_id = tree.id
        )
        select tree.root_id as root_id, {self._timeline_columns()}
        from message m
        join tree on tree.id = m.session_id
        join session s on s.id = m.session_id
        where json_extract(m.data, '$.role') in ('user', 'assistant')
        order by tree.root_id,
          case when (select count(*) from tree size where size.root_id = tree.root_id) = 1
                    and {v2_marker}
               then {seq_order} else {_TL_TS} end,
          m.rowid
        """
        groups: dict[str, list[dict]] = {}
        for r in self.conn.execute(sql):
            d = dict(r)
            groups.setdefault(d.pop("root_id"), []).append(d)
        tools = self._timeline_tools()  # one scan for the whole corpus, not one per root
        return {rid: _process_timeline(rows, tools) for rid, rows in groups.items()}

    def supports_turns(self, workflow_id: str) -> bool:
        # Per-session gate for the Turns tab. Like supports_tools, a single OpenCode DB
        # is uniform so the id is ignored; CombinedStore routes by owning backend.
        return self.supports_message_timeline
