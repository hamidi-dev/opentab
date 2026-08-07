"""OpenCode SQLite backend (read-only)."""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from urllib.parse import quote

from opentab.demo import demo_config, scramble_node, scramble_workflow
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
# The per-message wall-clock timestamp, shared by the Turns queries (per-session and
# the whole-corpus batch); epoch ms in the message JSON, present whether or not the
# table carries a time_created column.
_TL_TS = "json_extract(m.data, '$.time.created')"

# What makes a database this Store can actually open, as opposed to one that merely has
# tables by these names -- the columns EVERY query path uses unconditionally. It lives
# here, beside the SQL that uses them, so the schema check in `sources` can never drift
# from what the queries need. Deliberately short: everything else this Store touches
# (cost, tokens_*, time_updated, title, directory, agent, part) is probed and has a
# fallback -- see `_has_session_token_columns` and friends -- so requiring any of THOSE
# would reject a real OpenCode database that works today.
REQUIRED_SCHEMA = {
    "session": ("id", "parent_id", "time_created"),
    "message": ("id", "session_id", "data"),
}


def _process_timeline(rows: list[dict], tools: dict[str, list[str]] | None = None) -> list[dict]:
    # Turn the time-ordered (user + assistant) rows of ONE session into the Turns tab's
    # assistant-turn rows, each tagged with the prompt that triggered it: the most recent
    # user message owns every assistant turn until the next one. Shared by
    # message_timeline (one session) and message_timeline_all (per group). `rows` must be
    # a single session's messages in chronological order.
    #
    # `tools` maps message id -> the tool names that step invoked, joined here rather
    # than selected inline: see _timeline_tools for why this is a separate scan.
    out: list[dict] = []
    cur_id, cur_title, cur_full = "", "", ""
    for d in rows:
        if d["role"] == "user":  # opens/owns the following assistant turns
            cur_id = d["mid"] or ""
            cur_title = _clean_prompt(d["summary_title"] or d["prompt_text"])
            # The expandable full text is the raw prompt itself (uncapped, line breaks
            # kept); the generated summary only stands in when no text part was recorded.
            cur_full = str(d["prompt_text"] or d["summary_title"] or "").strip()
            continue
        # A turn that recorded neither tokens nor cost (an aborted/errored step) is noise
        # on a "how the money accrued" timeline -- drop it.
        if not (d["tokens_total"] or d["cost"]):
            continue
        d["time"] = d["time"] or ""
        d["prompt_id"] = cur_id
        d["prompt_title"] = cur_title
        d["prompt_full"] = cur_full
        # The tools this step called, in call order -- the same `part` rows
        # tool_breakdown attributes tokens to, carried onto the turn row so the Turns
        # tab can name them without a second per-session fetch. The file backends build
        # this list in their parser; OpenCode joins it here.
        d["tools"] = (tools or {}).get(d["mid"], [])
        for k in ("role", "mid", "summary_title", "prompt_text"):
            del d[k]
        out.append(d)
    return out


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
        # Demo mode: which categories to scramble (titles/turns/spend) and the hidden
        # magnitude factor -- one log-uniform draw (~0.33x..3x) so a screenshot can't be
        # reverse-engineered into real spend (tokens x list price would recover dollars),
        # or 1.0 when spend isn't scrambled. See demo.demo_config.
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        # Open read-only (URI mode) so opentab physically cannot modify the
        # OpenCode database it reads -- the "never writes" promise, enforced.
        uri = "file:" + quote(os.path.abspath(db)) + "?mode=ro"
        # check_same_thread=False lets CombinedStore run workflows()/model_breakdown() on
        # a worker thread (parse the backends in parallel). Each Store owns its own
        # connection and it is only ever touched by ONE thread at a time -- never
        # concurrently -- so no cross-thread locking is needed. Read-only regardless.
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
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

    def workflows(self) -> list[Workflow]:
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
        # in time order and sum each gap EXCEPT the one landing on a HUMAN prompt --
        # that gap is the user composing the follow-up. Only a depth-0 (root) `user`
        # message is a human turn; a subagent's `user` message is the agent-authored
        # task, so a gap into it is work. On a timestamp tie, human rows are ordered
        # first (is_human desc) so the gap-into-a-prompt is the one that drops -- which
        # keeps this SQL identical to formatting.worked_seconds' epoch-membership rule.
        # Needs the message table (per-message times); without it, worked stays null.
        if self.supports_message_timeline:
            worked_cte = f"""
        , msg_events as (
          select tree.root_id as root_id, {_TL_TS} as t_ms,
                 (json_extract(m.data, '$.role') = 'user' and tree.depth = 0) as is_human,
                 m.rowid as rid
          from message m
          join tree on tree.id = m.session_id
          where json_extract(m.data, '$.role') in ('user', 'assistant')
            and {_TL_TS} is not null
        ), worked as (
          select root_id,
                 sum(case when is_human then 0 else t_ms - prev_ms end) as worked_ms
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
        return [scramble_node(dict(r), self.demo_scale, self.demo_cats) for r in rows]

    def model_breakdown(self) -> list[sqlite3.Row]:
        # Per-(root session, model) cost/token attribution for EVERY root, in one
        # pass. Computed from per-message data (accurate for multi-model and older
        # sessions). The App caches this and slices it per session/day/month, so we
        # never run a query per workflow.
        # Pull each scalar out of the message JSON ONCE per row (the `msg` CTE), then
        # aggregate from those plain columns -- instead of ~35 json_extract(m.data, ...)
        # calls spread across the SELECT, each of which RE-PARSES the whole data blob.
        # The MATERIALIZED hint is what forces the single-pass extraction (without it the
        # planner inlines the CTE straight back into the aggregate and re-parses); it
        # needs SQLite 3.35+, so on older builds we drop the hint and fall back to the
        # original behaviour (identical results, just not sped up). ~40% faster on a
        # 44k-message DB; a big cut to the one heavy startup scan.
        mat = "materialized" if sqlite3.sqlite_version_info >= (3, 35, 0) else ""
        sql = f"""
        with recursive tree(root_id, id, depth) as (
          select id, id, 0 from session where parent_id is null
          union all
          select tree.root_id, child.id, tree.depth + 1
          from session child join tree on child.parent_id = tree.id
        ),
        msg as {mat} (
          select
            tree.root_id as root_id,
            tree.depth as depth,
            {MSG_MODEL_EXPR} as model_name,
            coalesce(json_extract(m.data, '$.cost'), 0) as cost,
            coalesce(json_extract(m.data, '$.tokens.input'), 0) as input,
            coalesce(json_extract(m.data, '$.tokens.output'), 0) as output,
            coalesce(json_extract(m.data, '$.tokens.reasoning'), 0) as reasoning,
            coalesce(json_extract(m.data, '$.tokens.cache.read'), 0) as cache_read,
            coalesce(json_extract(m.data, '$.tokens.cache.write'), 0) as cache_write
          from message m
          join tree on tree.id = m.session_id
          where json_extract(m.data, '$.role') = 'assistant'
        )
        select
          root_id,
          model_name,
          count(*) as runs,
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
        from msg
        group by root_id, model_name
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
            part_text = (
                "(select json_extract(p.data, '$.text') from part p "
                "where p.message_id = m.id and json_extract(p.data, '$.type') = 'text' "
                "order by p.rowid limit 1)"
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
        if not self.supports_message_timeline:
            return []
        sql = f"""
        with recursive tree(id, depth) as (
          select id, 0 from session where id = ?
          union all
          select child.id, tree.depth + 1
          from session child join tree on child.parent_id = tree.id
        )
        select {self._timeline_columns()}
        from message m
        join tree on tree.id = m.session_id
        join session s on s.id = m.session_id
        where json_extract(m.data, '$.role') in ('user', 'assistant')
        order by {_TL_TS}, m.rowid
        """
        rows = [dict(r) for r in self.conn.execute(sql, [workflow_id])]
        return _process_timeline(rows, self._timeline_tools(workflow_id))

    def _timeline_tools(self, workflow_id: str | None = None) -> dict[str, list[str]]:
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
        if workflow_id is None:
            sql, params = (
                (
                    "select message_id, json_extract(data, '$.tool') as tool from part "
                    "where json_extract(data, '$.type') = 'tool' order by rowid"
                ),
                [],
            )
        else:
            sql, params = (
                (
                    """
                with recursive tree(id) as (
                  select id from session where id = ?
                  union all
                  select child.id from session child join tree on child.parent_id = tree.id
                )
                select message_id, json_extract(data, '$.tool') as tool
                from part
                where session_id in (select id from tree)
                  and json_extract(data, '$.type') = 'tool'
                order by rowid
                """
                ),
                [workflow_id],
            )
        out: dict[str, list[str]] = {}
        for mid, tool in self.conn.execute(sql, params):
            if tool:
                out.setdefault(mid, []).append(tool)
        return out

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
        order by tree.root_id, {_TL_TS}, m.rowid
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
