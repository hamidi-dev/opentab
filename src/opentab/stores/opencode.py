"""OpenCode SQLite backend (read-only)."""
from __future__ import annotations

import argparse
import os
import random
import re
import sqlite3
from urllib.parse import quote

from opentab.demo import demo_cost, demo_dir, demo_model, demo_title
from opentab.formatting import _clean_prompt
from opentab.models import Workflow
from opentab.util import normalize_project_path

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

# Per-message model attribution from message.data JSON. The session.model column
# is only populated for newer sessions and holds a single model, so it can't
# represent multi-model sessions; the message table is the accurate source.
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


class Store:
    # OpenCode records real per-message dollar cost; records_cost=False marks sources
    # (Claude Code) whose cost is $0 until "$" reprices their tokens, driving a header
    # hint. source_name labels the active backend; combined is set only by CombinedStore.
    records_cost = True
    combined = False
    source_name = "OpenCode"

    def __init__(self, db: str, args: argparse.Namespace):
        self.db = db
        self.args = args
        self.demo = getattr(args, "demo", False)
        # Demo mode multiplies every cost and token count by one hidden factor so a
        # screenshot or recording can't be reverse-engineered into real spend --
        # tokens x list price would otherwise recover the actual dollars. Log-uniform
        # around 1 (~0.33x..3x) so the direction of scaling is hidden too; drawn once
        # per process and unseeded, so it stays stable across redraws but differs every
        # run and isn't recoverable from the (open) source. 1.0 outside demo.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        # Open read-only (URI mode) so opentab physically cannot modify the
        # OpenCode database it reads -- the "never writes" promise, enforced.
        uri = "file:" + quote(os.path.abspath(db)) + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        self._tune(self.conn)
        self.session_columns = self._table_columns("session")
        # The Tools tab attributes tokens to individual tool calls, which live in the
        # `part` table (one row per tool invocation). Older OpenCode schemas predate
        # it, so probe once: without it the tab is simply not offered.
        self.supports_tool_breakdown = self._table_exists("part")
        # The Turns tab lists every assistant message (one LLM step) chronologically.
        # It only needs the message table, which every OpenCode schema has, but probe
        # so a degenerate DB without it simply omits the tab instead of crashing.
        self.supports_message_timeline = self._table_exists("message")

    @staticmethod
    def _tune(conn: sqlite3.Connection) -> None:
        # The startup cost is dominated by scanning the (potentially gigabyte-scale)
        # message table and json_extract-ing blobs out of it. Memory-mapping the DB
        # avoids buffered read() syscalls over all that JSON -- a big win on slower
        # disks / cold caches -- and keeping GROUP BY temp b-trees in RAM trims the
        # rest. Read-only, so none of this can touch the user's data.
        for pragma in (
            "mmap_size = 2147483648",  # up to 2 GiB memory-mapped (capped at file size)
            "cache_size = -131072",  # 128 MiB page cache
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
                "select 1 from sqlite_master where type='table' and name=?", [table]
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

    def workflows(self) -> list[Workflow]:
        # Load every root session; the App filters by the active range in memory
        # so the range can be changed live without re-querying.
        token_exprs = self._token_exprs()
        cost_expr = self._cost_expr()
        title_expr = self._session_text_expr("root", ["title"], "'(untitled)'")
        directory_expr = self._session_text_expr("root", ["directory", "path"], "'(unknown)'")
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
            sum(case when node_cost = 0 then tokens_total else 0 end) as unpriced_tokens
          from nodes
          group by root_id
        )
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
          rollup.unpriced_tokens
        from rollup
        join session root on root.id = rollup.root_id
        order by rollup.total_cost desc, rollup.total_tokens desc
        """
        rows = [Workflow(**dict(row)) for row in self.conn.execute(sql)]
        for w in rows:
            w.source = self.source_name
            # OpenCode stores forward-slash Windows paths (C:/DEV/app); fold them to
            # the native C:\DEV\app spelling so a project shared with a backslash
            # backend (Pi, Claude, ...) groups as one, not two (issue #4).
            w.directory = normalize_project_path(w.directory)
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        return rows

    def _demo_money(self, value: float) -> float:
        return round(value * self.demo_scale, 4)

    def _demo_tokens(self, value: float) -> int:
        return int(round(value * self.demo_scale))

    def _demo_workflow(self, w: Workflow) -> Workflow:
        w.title = demo_title(w.id)
        w.directory = demo_dir(w.id)
        if w.unpriced_tokens > 0:
            add = demo_cost(w.unpriced_tokens, w.id)
            w.total_cost += add
            if w.root_cost == 0:
                w.root_cost += add
            w.unpriced_tokens = 0
        # Scale magnitudes by the hidden factor so the figures on screen don't trace
        # back to real spend; counts (subagents, model_count) stay structural.
        w.total_cost = self._demo_money(w.total_cost)
        w.root_cost = self._demo_money(w.root_cost)
        w.total_tokens = self._demo_tokens(w.total_tokens)
        return w

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

    def workflow_nodes(self, workflow_id: str) -> list[sqlite3.Row]:
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
            select {MSG_MODEL_EXPR}
            from message m
            where m.session_id = s.id and json_extract(m.data, '$.role') = 'assistant'
            group by {MSG_MODEL_EXPR}
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
        out = []
        for r in rows:
            d = dict(r)
            d["title"] = demo_title(d["id"])
            d["model_name"] = demo_model(d["model_name"])
            if d["cost"] == 0:
                d["cost"] = demo_cost(d["tokens_total"], d["id"])
            d["cost"] = self._demo_money(d["cost"])
            for f in (
                "tokens_input",
                "tokens_output",
                "tokens_reasoning",
                "tokens_cache_read",
                "tokens_cache_write",
                "tokens_total",
            ):
                d[f] = self._demo_tokens(d[f])
            out.append(d)
        return out

    def model_breakdown(self) -> list[sqlite3.Row]:
        # Per-(root session, model) cost/token attribution for EVERY root, in one
        # pass. Computed from per-message data (accurate for multi-model and older
        # sessions). The App caches this and slices it per session/day/month, so we
        # never run a query per workflow.
        sql = f"""
        with recursive tree(root_id, id, depth) as (
          select id, id, 0 from session where parent_id is null
          union all
          select tree.root_id, child.id, tree.depth + 1
          from session child join tree on child.parent_id = tree.id
        )
        select
          tree.root_id as root_id,
          {MSG_MODEL_EXPR} as model_name,
          count(*) as runs,
          sum(coalesce(json_extract(m.data, '$.cost'), 0)) as cost,
          sum(case when tree.depth = 0 then coalesce(json_extract(m.data, '$.cost'), 0) else 0 end) as root_cost,
          sum({MSG_TOKEN_TOTAL_EXPR}) as tokens_total,
          sum(coalesce(json_extract(m.data, '$.tokens.input'), 0)) as input,
          sum(coalesce(json_extract(m.data, '$.tokens.reasoning'), 0)) as reasoning,
          sum(coalesce(json_extract(m.data, '$.tokens.cache.read'), 0)) as cache_read,
          sum(coalesce(json_extract(m.data, '$.tokens.cache.write'), 0)) as cache_write,
          sum(coalesce(json_extract(m.data, '$.tokens.output'), 0)) as output,
          sum(case when coalesce(json_extract(m.data, '$.cost'), 0) = 0
              then coalesce(json_extract(m.data, '$.tokens.input'), 0) else 0 end) as unpriced_input,
          sum(case when coalesce(json_extract(m.data, '$.cost'), 0) = 0
              then coalesce(json_extract(m.data, '$.tokens.reasoning'), 0) else 0 end) as unpriced_reasoning,
          sum(case when coalesce(json_extract(m.data, '$.cost'), 0) = 0
              then coalesce(json_extract(m.data, '$.tokens.cache.read'), 0) else 0 end) as unpriced_cache_read,
          sum(case when coalesce(json_extract(m.data, '$.cost'), 0) = 0
              then coalesce(json_extract(m.data, '$.tokens.cache.write'), 0) else 0 end) as unpriced_cache_write,
          sum(case when coalesce(json_extract(m.data, '$.cost'), 0) = 0
              then coalesce(json_extract(m.data, '$.tokens.output'), 0) else 0 end) as unpriced_output,
          sum(case when tree.depth = 0 and coalesce(json_extract(m.data, '$.cost'), 0) = 0
              then coalesce(json_extract(m.data, '$.tokens.input'), 0) else 0 end) as root_unpriced_input,
          sum(case when tree.depth = 0 and coalesce(json_extract(m.data, '$.cost'), 0) = 0
              then coalesce(json_extract(m.data, '$.tokens.reasoning'), 0) else 0 end) as root_unpriced_reasoning,
          sum(case when tree.depth = 0 and coalesce(json_extract(m.data, '$.cost'), 0) = 0
              then coalesce(json_extract(m.data, '$.tokens.cache.read'), 0) else 0 end) as root_unpriced_cache_read,
          sum(case when tree.depth = 0 and coalesce(json_extract(m.data, '$.cost'), 0) = 0
              then coalesce(json_extract(m.data, '$.tokens.cache.write'), 0) else 0 end) as root_unpriced_cache_write,
          sum(case when tree.depth = 0 and coalesce(json_extract(m.data, '$.cost'), 0) = 0
              then coalesce(json_extract(m.data, '$.tokens.output'), 0) else 0 end) as root_unpriced_output
        from message m
        join tree on tree.id = m.session_id
        where json_extract(m.data, '$.role') = 'assistant'
        group by tree.root_id, model_name
        """
        # Subscription/credit rows (Copilot, Codex, Claude Code) carry real runs
        # AND real token counts but cost 0 in the message JSON. Demo mode reconciles
        # them to each session's synthetic total; the "$" toggle prices their tokens
        # at API list prices -- both in App._load_model_cache.
        return list(self.conn.execute(sql))

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
        sql = f"""
        with recursive tree(id) as (
          select id from session where id = ?
          union all
          select child.id from session child join tree on child.parent_id = tree.id
        ),
        session_parts as (
          select message_id,
                 json_extract(data, '$.type') as ptype,
                 json_extract(data, '$.tool') as tool
          from part
          where session_id in (select id from tree)
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
          {MSG_MODEL_EXPR} as model_name,
          count(*) as calls,
          sum(({MSG_TOKEN_TOTAL_EXPR}) * 1.0 / tc.n) as tokens_total,
          sum(coalesce(json_extract(m.data, '$.tokens.input'), 0) * 1.0 / tc.n) as input,
          sum(coalesce(json_extract(m.data, '$.tokens.output'), 0) * 1.0 / tc.n) as output,
          sum(coalesce(json_extract(m.data, '$.tokens.reasoning'), 0) * 1.0 / tc.n) as reasoning,
          sum(coalesce(json_extract(m.data, '$.tokens.cache.read'), 0) * 1.0 / tc.n) as cache_read,
          sum(coalesce(json_extract(m.data, '$.tokens.cache.write'), 0) * 1.0 / tc.n) as cache_write,
          sum(coalesce(json_extract(m.data, '$.cost'), 0) * 1.0 / tc.n) as cost
        from tools t
        join message m on m.id = t.message_id
        join tool_counts tc on tc.message_id = t.message_id
        group by t.tool, model_name
        order by cost desc, tokens_total desc
        """
        return list(self.conn.execute(sql, [workflow_id]))

    def supports_tools(self, workflow_id: str) -> bool:
        # Per-session capability gate for the Tools tab. A single OpenCode DB is
        # uniform (every session is backed by the part table or none is), so the id
        # is ignored here; CombinedStore overrides this to route by owning backend so
        # only OpenCode sessions in a merged view offer the tab.
        return self.supports_tool_breakdown

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
        if not self.supports_message_timeline:
            return []
        agent_expr = self._session_text_expr("s", ["agent"], "'-'")
        # The per-message wall-clock time lives in the JSON ($.time.created, epoch ms),
        # present regardless of whether the message table carries a time_created
        # column, so sort/format off that and fall back to rowid for untimed rows.
        # Return the full localtime datetime (like created_at) and let the renderer
        # pick the display width -- a session can span days, so the date matters.
        ts_expr = "json_extract(m.data, '$.time.created')"
        summary_title = "nullif(json_extract(m.data, '$.summary.title'), '')"
        if self.supports_tool_breakdown:  # the raw prompt text lives in the part table
            part_text = (
                "(select json_extract(p.data, '$.text') from part p "
                "where p.message_id = m.id and json_extract(p.data, '$.type') = 'text' "
                "order by p.rowid limit 1)"
            )
            title_expr = (
                f"case when json_extract(m.data, '$.role') = 'user' "
                f"then coalesce({summary_title}, {part_text}) end"
            )
        else:
            title_expr = (
                f"case when json_extract(m.data, '$.role') = 'user' then {summary_title} end"
            )
        sql = f"""
        with recursive tree(id, depth) as (
          select id, 0 from session where id = ?
          union all
          select child.id, tree.depth + 1
          from session child join tree on child.parent_id = tree.id
        )
        select
          json_extract(m.data, '$.role') as role,
          m.id as mid,
          datetime({ts_expr} / 1000, 'unixepoch', 'localtime') as time,
          tree.depth as depth,
          {agent_expr} as agent,
          {MSG_MODEL_EXPR} as model_name,
          coalesce(json_extract(m.data, '$.cost'), 0) as cost,
          coalesce(json_extract(m.data, '$.tokens.input'), 0) as input,
          coalesce(json_extract(m.data, '$.tokens.output'), 0) as output,
          coalesce(json_extract(m.data, '$.tokens.reasoning'), 0) as reasoning,
          coalesce(json_extract(m.data, '$.tokens.cache.read'), 0) as cache_read,
          coalesce(json_extract(m.data, '$.tokens.cache.write'), 0) as cache_write,
          ({MSG_TOKEN_TOTAL_EXPR}) as tokens_total,
          {title_expr} as prompt_title
        from message m
        join tree on tree.id = m.session_id
        join session s on s.id = m.session_id
        where json_extract(m.data, '$.role') in ('user', 'assistant')
        order by {ts_expr}, m.rowid
        """
        out = []
        cur_id, cur_title = "", ""
        for r in self.conn.execute(sql, [workflow_id]):
            d = dict(r)
            if d["role"] == "user":  # opens/owns the following assistant turns
                cur_id = d["mid"] or ""
                cur_title = _clean_prompt(d["prompt_title"])
                continue
            # A turn that recorded neither tokens nor cost (an aborted/errored step) is
            # noise on a "how the money accrued" timeline -- drop it.
            if not (d["tokens_total"] or d["cost"]):
                continue
            d["time"] = d["time"] or ""
            d["prompt_id"] = cur_id
            d["prompt_title"] = cur_title
            del d["role"], d["mid"]
            out.append(d)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        # Per-session gate for the Turns tab. Like supports_tools, a single OpenCode DB
        # is uniform so the id is ignored; CombinedStore routes by owning backend.
        return self.supports_message_timeline
