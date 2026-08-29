"""Hermes Agent SQLite backend."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

from opentab.demo import demo_config, demo_title, scramble_node, scramble_workflow
from opentab.formatting import worked_seconds
from opentab.models import Workflow
from opentab.util import git_root, safe_float, safe_int, tool_names, tool_rows_from_turns


class HermesStore:
    """Read Hermes sessions from its SQLite state and rotating agent logs.

    Hermes stores uncached input separately from cache reads/writes and includes
    reasoning in output. Positive actual cost wins over estimated cost; zero-cost
    subscription usage remains unpriced. Session parents form the subagent tree.
    Per-turn usage comes from logs because ``messages.token_count`` is unpopulated.
    Optional database columns are probed before use.
    """

    combined = False
    source_name = "Hermes"

    # Cycle guard for the recursive subtree walk; far deeper than any real nesting.
    _MAX_TREE_DEPTH = 64

    # Columns read when present; absent ones fall back to a SQL default so the
    # SELECT never references a column this Hermes version doesn't have.
    _COLS: tuple[tuple[str, str], ...] = (
        ("title", "''"),
        ("model", "''"),
        ("cwd", "''"),
        ("parent_session_id", "NULL"),
        ("started_at", "0"),
        ("input_tokens", "0"),
        ("output_tokens", "0"),
        ("cache_read_tokens", "0"),
        ("cache_write_tokens", "0"),
        ("billing_provider", "''"),
        ("estimated_cost_usd", "NULL"),
        ("actual_cost_usd", "NULL"),
    )

    # Hermes billing_provider names -> the price-table / display prefix. model_price()
    # only reads the bare id after the last "/", so this is for display + local
    # detection; an unmapped provider is used verbatim, and an empty one is inferred.
    _PROVIDER_ALIASES = {
        "openai-codex": "openai",
        "openai_codex": "openai",
        "openai": "openai",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "google": "google",
        "google-ai": "google",
        "gemini": "google",
        "vertex": "google",
        "vertex-ai": "google",
        "openrouter": "openrouter",
        "nous": "nous",
        "xai": "xai",
        "groq": "groq",
    }

    def __init__(self, db_path: str, args: argparse.Namespace):
        self.db_path = db_path
        self.args = args
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._sessions: dict[str, dict] | None = None
        self._turns_by_session: dict[str, list[dict]] | None = None
        self._turns_stamp: tuple = ()
        self._git_root_cache: dict[str, str] = {}
        self._cols = self._probe_columns()
        self.records_cost = self._probe_records_cost()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _probe_columns(self) -> set[str]:
        try:
            conn = self._connect()
        except sqlite3.Error:
            return set()
        try:
            return {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        except sqlite3.Error:
            return set()
        finally:
            conn.close()

    def _probe_records_cost(self) -> bool:
        # True iff any live session has a recorded (metered) cost. Cheap EXISTS
        # query so it's safe to call at construction, before _parse().
        cost_cols = [c for c in ("actual_cost_usd", "estimated_cost_usd") if c in self._cols]
        try:
            conn = self._connect()
        except sqlite3.Error:
            return False
        try:
            if cost_cols:
                clauses = ["archived = 0"] if "archived" in self._cols else []
                clauses.append("(" + " OR ".join(f"COALESCE({c}, 0) > 0" for c in cost_cols) + ")")
                sql = f"SELECT EXISTS(SELECT 1 FROM sessions WHERE {' AND '.join(clauses)})"
                if bool(conn.execute(sql).fetchone()[0]):
                    return True
            # Auxiliary spend never reaches the sessions row, so a corpus whose only
            # metered dollars are aux would read as "no recorded cost" and flip the
            # header to estimate wording. Ask the usage table too, but only for live
            # sessions that can actually appear in the rollup.
            usage_cols = {r[1] for r in conn.execute("PRAGMA table_info(session_model_usage)")}
            usage_cost_cols = [
                c for c in ("actual_cost_usd", "estimated_cost_usd") if c in usage_cols
            ]
            if not usage_cost_cols:
                return False
            live = " AND s.archived = 0" if "archived" in self._cols else ""
            return bool(
                conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM session_model_usage u "
                    "JOIN sessions s ON s.id = u.session_id WHERE ("
                    + " OR ".join(f"COALESCE(u.{c}, 0) > 0" for c in usage_cost_cols)
                    + f"){live})"
                ).fetchone()[0]
            )
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _select_sql(self) -> str:
        parts = ["id"]
        for name, default in self._COLS:
            parts.append(name if name in self._cols else f"{default} AS {name}")
        # Only `archived` is excluded. Hermes' `hidden` flag (0.20.6) hides a session
        # from its own sidebar and is NOT an accounting signal -- hidden spend is still
        # spend, so it stays in the rollup. See test_hermes_hidden_sessions_still_count.
        where = " WHERE archived = 0" if "archived" in self._cols else ""
        order = " ORDER BY started_at" if "started_at" in self._cols else ""
        return f"SELECT {', '.join(parts)} FROM sessions{where}{order}"

    @staticmethod
    def _recorded_cost(actual, estimated) -> float:
        # Trust a positive reconciled (actual) cost, else the
        # provider's estimate, else $0 (subscription -> stays unpriced).
        for v in (actual, estimated):
            if v is not None and v > 0:
                return float(v)
        return 0.0

    def _git_root(self, cwd: str) -> str:
        if cwd not in self._git_root_cache:
            self._git_root_cache[cwd] = git_root(cwd)
        return self._git_root_cache[cwd]

    @staticmethod
    def _ts_to_local(ts: float) -> str:
        if not ts:
            return ""
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return ""

    @staticmethod
    def _ts_to_local_ms(ts: float) -> str:
        # _ts_to_local at millisecond precision, the resolution the tool join needs.
        if not ts:
            return ""
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        except (OSError, OverflowError, ValueError):
            return ""

    @staticmethod
    def _epoch_ms(value) -> int:
        # Epoch milliseconds for the cross-backend recent_roots contract. Hermes
        # writes epoch seconds (started_at, messages.timestamp), but be liberal --
        # milliseconds already, or an ISO string -- since the schema is probed,
        # never guaranteed. 0 when unreadable, so max() just ignores it.
        if isinstance(value, bool) or value is None:
            return 0
        if isinstance(value, (int, float)):
            v = float(value)
            if v <= 0:
                return 0
            return int(v if v > 1e11 else v * 1000)
        if isinstance(value, str) and value.strip():
            try:
                dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return 0
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        return 0

    @staticmethod
    def _infer_provider(model: str) -> str:
        m = model.lower()
        if m.startswith(("claude-", "claude/")):
            return "anthropic"
        if m.startswith(("gpt", "chatgpt", "o1", "o3", "o4")):
            return "openai"
        if m.startswith(("gemini-", "gemini/")):
            return "google"
        return ""  # leave bare; model_price() handles bare ids fine

    @classmethod
    def _prefix_model(cls, model: str, provider: str = "") -> str:
        model = (model or "").strip()
        if not model:
            return "unknown"
        if "/" in model:
            return model  # already provider-qualified
        prov = (provider or "").strip().lower()
        if prov == "auto":
            # Hermes' auxiliary provider setting is the literal string "auto" until it
            # resolves, and that placeholder is what lands in session_model_usage rows.
            # It is not a provider: let the model name infer one, so an aux row merges
            # into its model's bucket instead of splitting the same model off under a
            # second "auto/..." name (which would also price against an unknown vendor).
            prov = ""
        prefix = cls._PROVIDER_ALIASES.get(prov, prov or cls._infer_provider(model))
        return f"{prefix}/{model}" if prefix else model

    # The transcript a voice turn carries inside its bracketed note block.
    _VOICE_RE = re.compile(r'said:\s*"(.*?)"', re.S)

    @classmethod
    def _title_from_content(cls, text: str) -> str:
        # A user message's visible words, for the title fallback. Hermes prepends
        # injected context as "[ ... ]" blocks; drop them -- but a voice turn is
        # one such block whose quoted transcript IS the prompt, so mine that.
        text = (text or "").strip()
        while text.startswith("["):
            end = text.find("]")
            if end < 0:
                return ""
            block, text = text[: end + 1], text[end + 1 :].lstrip()
            if "voice message" in block:
                m = cls._VOICE_RE.search(block)
                if m and m.group(1).strip():
                    return m.group(1).strip()[:80]
        for line in text.splitlines():
            if line.strip():
                return line.strip()[:80]  # the title fallback stays short (ClaudeStore)
        return ""

    # Token classes compared when reconciling a session against its usage rows.
    _RECONCILE = ("inp", "out", "cr", "cw")

    def _load_usage_buckets(self, conn: sqlite3.Connection, flat: dict[str, dict]) -> None:
        """Split each session's usage per model and fold in auxiliary-task spend.

        Hermes 0.20.6 records every LLM call in `session_model_usage`, keyed by
        (session, model, provider, billing mode, task). `task=''` is the main agent
        loop -- the figures the `sessions` summary row already carries -- and the
        other tasks are auxiliary calls (title_generation, approval, vision,
        compression, ...) that Hermes deliberately keeps OUT of that row, because the
        gateway overwrites it with absolute main-loop totals. So the summary row
        understates a session by its auxiliary spend, and pins every token to one
        model even when the session used several.

        Applied per session and ONLY when its main rows reconcile with the summary
        row exactly. A partially written, version-skewed, or mid-commit table would
        otherwise be added on top of totals that already contain it. Failing closed
        costs a session its (small) aux tokens; failing open would double-count its
        (large) main ones, so the gate is the whole point.

        `reasoning_tokens` is read but never added: on this backend reasoning is a
        subset of `output_tokens`, so counting it would double-count -- the same rule
        the log path follows.
        """
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(session_model_usage)")}
        except sqlite3.Error:
            return
        if not {"session_id", "model", "task", "input_tokens"} <= cols:
            return  # no table, or too old to carry the task dimension
        selected = []
        for name, default in (
            ("billing_provider", "''"),
            ("api_call_count", "1"),
            ("output_tokens", "0"),
            ("cache_read_tokens", "0"),
            ("cache_write_tokens", "0"),
            ("actual_cost_usd", "0"),
            ("estimated_cost_usd", "0"),
        ):
            selected.append(name if name in cols else f"{default} AS {name}")
        try:
            rows = conn.execute(
                f"""SELECT session_id, model, task, input_tokens,
                           {', '.join(selected)}
                    FROM session_model_usage"""
            ).fetchall()
        except sqlite3.Error:
            return

        per: dict[str, dict[str, list]] = {}
        for row in rows:
            sid = row["session_id"]
            if sid not in flat:
                continue
            per.setdefault(sid, {"main": [], "aux": []})
            bucket = {
                "model": self._prefix_model(row["model"] or "", row["billing_provider"] or ""),
                "runs": safe_int(row["api_call_count"]),
                "inp": safe_int(row["input_tokens"]),
                "out": safe_int(row["output_tokens"]),
                "cr": safe_int(row["cache_read_tokens"]),
                "cw": safe_int(row["cache_write_tokens"]),
                "cost": self._recorded_cost(row["actual_cost_usd"], row["estimated_cost_usd"]),
            }
            per[sid]["aux" if (row["task"] or "") else "main"].append(bucket)

        for sid, split in per.items():
            node, main, aux = flat[sid], split["main"], split["aux"]
            if not main:
                continue  # aux with no main rows: cannot prove the summary excludes it
            if any(sum(b[k] for b in main) != node[k] for k in self._RECONCILE):
                continue  # skewed against the summary row -- trust the summary alone
            main_cost = sum(b["cost"] for b in main)
            if {"actual_cost_usd", "estimated_cost_usd"} & self._cols:
                if abs(main_cost - node["cost"]) > 1e-6:
                    continue
            else:
                # Hermes 0.20.6 can keep the only cost truth in the usage table.
                node["cost"] = main_cost
            node["usage"] = main + aux
            for b in aux:
                for k in self._RECONCILE:
                    node[k] += b[k]
                node["cost"] += b["cost"]
                node["tokens_total"] += b["inp"] + b["out"] + b["cr"] + b["cw"]
            node["unpriced_tokens"] = sum(
                b["inp"] + b["out"] + b["cr"] + b["cw"] for b in node["usage"] if b["cost"] <= 0
            )

    def _fallback_titles(self, conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
        # First real user prompt for sessions Hermes left untitled -- the ClaudeStore
        # title precedence. Hermes names its own sessions from 0.20.6 on, the
        # api_server/voice path included, so this now covers older rows and any
        # session whose own titling did not run rather than a whole platform. One
        # query over the untitled ids; per session the first usable prompt wins.
        out: dict[str, str] = {}
        if not ids:
            return out
        marks = ",".join("?" * len(ids))
        sql = (
            "SELECT session_id, substr(content, 1, 400) FROM messages "
            f"WHERE role = 'user' AND session_id IN ({marks}) "
            "AND content IS NOT NULL AND content != '' ORDER BY timestamp"
        )
        try:
            rows = conn.execute(sql, ids).fetchall()
        except sqlite3.Error:
            return out  # no messages table (old/partial DB): titles stay bare
        for sid, content in rows:
            if sid in out:
                continue
            title = self._title_from_content(content)
            if title:
                out[sid] = title
        return out

    def _load_message_events(self, conn: sqlite3.Connection, flat: dict[str, dict]) -> None:
        # Populate each node's msg_events [(epoch_s, is_user)] and its last-activity
        # end from the messages table. Raises sqlite3.Error (caught by the caller) when
        # the table or its role column is absent, so an old/partial DB falls back to a
        # grouped-MAX end and leaves worked unknown.
        for sid, ts, role in conn.execute("SELECT session_id, timestamp, role FROM messages"):
            s = flat.get(sid)
            if s is None or ts is None:
                continue
            e = self._epoch_ms(ts) / 1000.0
            s["msg_events"].append((e, role == "user"))
            if e > s.get("ended_ts", 0.0):
                s["ended_ts"] = e
            s["msg_end"] = True  # a directly observed end

    @staticmethod
    def _reachable(starts: list[str], children: dict[str, list[str]]) -> set[str]:
        # Every session reachable from `starts` by walking the child map, cycle-guarded
        # so malformed parent metadata can't spin forever.
        seen: set[str] = set()
        queue = list(starts)
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(children.get(node, []))
        return seen

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        if "id" not in self._cols:  # unreadable / unexpected schema -> empty, never crash
            self._sessions = {}
            return self._sessions
        conn = self._connect()
        try:
            rows = conn.execute(self._select_sql()).fetchall()

            # Flat map of all sessions. Each carries a recorded cost: $0 for
            # subscription routes (-> unpriced, the "$" view estimates it) or the
            # metered cost for paid routes (-> priced, shown as real spend).
            flat: dict[str, dict] = {}
            for row in rows:
                inp = row["input_tokens"] or 0
                out = row["output_tokens"] or 0
                cr = row["cache_read_tokens"] or 0
                cw = row["cache_write_tokens"] or 0
                flat[row["id"]] = {
                    "id": row["id"],
                    "title": (row["title"] or "").strip(),
                    "model": self._prefix_model(row["model"] or "", row["billing_provider"] or ""),
                    "cwd": row["cwd"] or "",
                    "parent_id": row["parent_session_id"],
                    "started_at": row["started_at"] or 0.0,
                    "inp": inp,
                    "out": out,
                    "cr": cr,
                    "cw": cw,
                    "cost": self._recorded_cost(row["actual_cost_usd"], row["estimated_cost_usd"]),
                    "tokens_total": inp + out + cr + cw,
                    "msg_events": [],  # (epoch_s, is_user) per message, for worked_seconds
                }

            # Untitled sessions (roots and subagent nodes alike) fall back to
            # their first real user prompt, while the connection is still open.
            # Per-model / auxiliary usage first: it can give an otherwise empty
            # session real tokens, and the roots pass below drops sessions with none.
            self._load_usage_buckets(conn, flat)

            untitled = [sid for sid, s in flat.items() if not s["title"]]
            fallbacks = self._fallback_titles(conn, untitled)
            for sid in untitled:
                flat[sid]["title"] = fallbacks.get(sid) or "(untitled)"
            # Per-message activity: each row's timestamp is an activity point, and its
            # role tells a human turn (idle boundary) from an agent turn -- the raw
            # material for both the last-activity end and worked_seconds. Same guarded
            # probe as recent_roots; an old/partial DB without a messages table (or
            # without a role column) just leaves started_at as each node's end and
            # worked unknown. Read whole rows rather than a grouped MAX so worked can
            # see every gap; the DB is personal-scale.
            try:
                self._load_message_events(conn, flat)
            except sqlite3.Error:
                try:  # no role column: still salvage the end from a grouped MAX
                    for sid, ts in conn.execute(
                        "SELECT session_id, MAX(timestamp) FROM messages GROUP BY session_id"
                    ):
                        if sid in flat:
                            flat[sid]["ended_ts"] = self._epoch_ms(ts) / 1000.0
                            flat[sid]["msg_end"] = True  # a directly observed end
                except sqlite3.Error:
                    pass
        finally:
            conn.close()
        for s in flat.values():
            s["ended_ts"] = max(s.get("ended_ts", 0.0), s["started_at"] or 0.0)

        # Map root_id -> list of direct children
        children: dict[str, list[str]] = {}
        for sid, s in flat.items():
            pid = s["parent_id"]
            if pid and pid in flat:
                children.setdefault(pid, []).append(sid)

        # Roots are the sessions with no parent -- plus any whose parent_id doesn't
        # resolve here, because the parent was archived (the query above filters those
        # out) or pruned from the table. An unresolvable parent makes the child its own
        # root, which is the rule root_of and recent_roots already follow; treating it as
        # a subagent instead left it reachable from nowhere, so its tokens and its real
        # metered spend dropped out of every view while records_cost still advertised
        # recorded cost.
        roots = [sid for sid, s in flat.items() if not s["parent_id"] or s["parent_id"] not in flat]
        # Anything still unreachable sits in a parent cycle (every member points at
        # another member, so none of them qualified above). Promote them in id order, so
        # a cycle yields a deterministic tree instead of vanishing.
        reached = self._reachable(roots, children)
        for sid in sorted(flat):
            if sid not in reached:
                roots.append(sid)
                reached |= self._reachable([sid], children)

        result: dict[str, dict] = {}
        for sid in roots:
            s = flat[sid]
            if s["tokens_total"] == 0 and s["cost"] == 0 and not children.get(sid):
                continue  # no recorded usage or cost

            created_at = self._ts_to_local(s["started_at"])
            directory = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"

            # Aggregate totals (root + all descendants, any depth) via BFS. Track
            # per-model buckets so model_rows attribute tokens/cost to the model
            # that produced them; the unpriced_* split holds only the $0
            # (subscription) tokens so the "$" reprice touches just those.
            tot_total = 0
            tot_unpriced = 0
            tot_cost = 0.0
            ended_ts = 0.0  # latest activity anywhere in the subtree
            end_observed = False  # any node with a real message end (vs start-only)
            tree_events: list[tuple[float, bool]] = []  # (epoch_s, is_user) subtree-wide
            subagent_nodes: list[dict] = []
            # model_name -> {runs, cost, r_cost, inp/out/cr/cw, u_* (unpriced), ru_* (root unpriced)}
            model_acc: dict[str, dict] = {}

            bfs_queue: list[tuple[str, int, bool]] = [(sid, 0, True)]
            walked: set[str] = set()
            while bfs_queue:
                node_id, depth, is_root = bfs_queue.pop(0)
                if node_id in walked:
                    continue  # cyclic parent metadata: never walk (or count) a node twice
                walked.add(node_id)
                node = flat[node_id]
                tot_total += node["tokens_total"]
                tot_cost += node["cost"]
                ended_ts = max(ended_ts, node["ended_ts"])
                end_observed = end_observed or node.get("msg_end", False)
                # Only the root's `user` messages are human turns (idle boundaries); a
                # subagent's are the agent-authored task, so a gap into one is work.
                if is_root:
                    tree_events.extend(node["msg_events"])
                else:
                    tree_events.extend((e, False) for e, _u in node["msg_events"])
                tot_unpriced += node.get(
                    "unpriced_tokens", node["tokens_total"] if node["cost"] <= 0 else 0
                )
                self._add_model(model_acc, node, is_root)
                if not is_root:
                    subagent_nodes.append(
                        self._node(
                            node_id,
                            depth,
                            "subagent",
                            node["title"],
                            self._ts_to_local(node["started_at"]),
                            node["model"],
                            node["cost"],
                            self._node_acc(node),
                        )
                    )
                for child_id in children.get(node_id, []):
                    bfs_queue.append((child_id, depth + 1, False))

            root_acc = self._node_acc(s)  # root node's own tokens (depth-0 node + root split)
            model_rows = []
            for mname, m in model_acc.items():
                model_rows.append(
                    {
                        "root_id": sid,
                        "model_name": mname,
                        "runs": m["runs"],
                        "cost": round(m["cost"], 6),
                        "root_cost": round(m["r_cost"], 6),
                        "tokens_total": m["inp"] + m["out"] + m["cr"] + m["cw"],
                        "input": m["inp"],
                        "output": m["out"],
                        "reasoning": 0,
                        "cache_read": m["cr"],
                        "cache_write": m["cw"],
                        "unpriced_input": m["u_inp"],
                        "unpriced_output": m["u_out"],
                        "unpriced_reasoning": 0,
                        "unpriced_cache_read": m["u_cr"],
                        "unpriced_cache_write": m["u_cw"],
                        "root_unpriced_input": m["ru_inp"],
                        "root_unpriced_output": m["ru_out"],
                        "root_unpriced_reasoning": 0,
                        "root_unpriced_cache_read": m["ru_cr"],
                        "root_unpriced_cache_write": m["ru_cw"],
                    }
                )

            # An end that only restates the start (no messages table, a session with
            # no message rows, no later-started child) is not knowledge -- blank it
            # so the UI shows "unknown", never a fake 0s on a long session.
            if ended_ts <= (s["started_at"] or 0.0) and not end_observed:
                ended_at = ""
            else:
                ended_at = self._ts_to_local(ended_ts)
            # Worked time over the whole subtree's messages: user turns mark the idle
            # gaps. None (unknown) when there's no per-message stream to measure.
            worked = worked_seconds([e for e, _u in tree_events], [e for e, u in tree_events if u])
            result[sid] = {
                "title": s["title"],
                "directory": directory,
                "created_at": created_at,
                "ended_at": ended_at,
                "worked_seconds": worked,
                "total_tokens": tot_total,
                "unpriced_tokens": tot_unpriced,
                "total_cost": round(tot_cost, 6),
                "root_cost": round(s["cost"], 6),
                "model": s["model"],
                "root_acc": root_acc,
                "model_rows": model_rows,
                "subagents": subagent_nodes,
            }

        self._sessions = result
        return result

    @staticmethod
    def _node_acc(node: dict) -> dict:
        # The per-node token accumulator shape _node() expects, from one session row.
        return {
            "runs": 1,
            "input": node["inp"],
            "output": node["out"],
            "reasoning": 0,
            "cache_read": node["cr"],
            "cache_write": node["cw"],
            "tokens_total": node["tokens_total"],
        }

    @staticmethod
    def _add_model(model_acc: dict[str, dict], node: dict, is_root: bool) -> None:
        # Fold one session into its model's bucket. Tokens go to the running
        # totals (inp/out/cr/cw); $0 (subscription) tokens also land in the
        # unpriced (u_*) split so the "$" view reprices only those, and the
        # root's own contribution is mirrored into the root (ru_*) split.
        # A session reconciled against session_model_usage carries one bucket per
        # (model, task); fold each into ITS model so a model-switched or aux-using
        # session is attributed correctly. Buckets merge by model name here, which is
        # what keeps model_count a count of models rather than of model-task pairs.
        for b in node.get("usage") or ():
            HermesStore._add_bucket(model_acc, b["model"], b, is_root)
        if node.get("usage"):
            return
        HermesStore._add_bucket(model_acc, node["model"], node, is_root)

    @staticmethod
    def _add_bucket(model_acc: dict[str, dict], model_name: str, src: dict, is_root: bool) -> None:
        m = model_acc.get(model_name)
        if m is None:
            m = model_acc[model_name] = {
                "runs": 0,
                "cost": 0.0,
                "r_cost": 0.0,
                "inp": 0,
                "out": 0,
                "cr": 0,
                "cw": 0,
                "u_inp": 0,
                "u_out": 0,
                "u_cr": 0,
                "u_cw": 0,
                "ru_inp": 0,
                "ru_out": 0,
                "ru_cr": 0,
                "ru_cw": 0,
            }
        i, o, cr, cw, cost = src["inp"], src["out"], src["cr"], src["cw"], src["cost"]
        m["runs"] += src.get("runs", 1)
        m["inp"] += i
        m["out"] += o
        m["cr"] += cr
        m["cw"] += cw
        m["cost"] += cost
        unpriced = cost <= 0
        if unpriced:
            m["u_inp"] += i
            m["u_out"] += o
            m["u_cr"] += cr
            m["u_cw"] += cw
        if is_root:
            m["r_cost"] += cost
            if unpriced:
                m["ru_inp"] += i
                m["ru_out"] += o
                m["ru_cr"] += cr
                m["ru_cw"] += cw

    @staticmethod
    def _node(
        node_id: str,
        depth: int,
        agent: str,
        title: str,
        created_at: str,
        model_name: str,
        cost: float,
        acc: dict,
    ) -> dict:
        return {
            "id": node_id,
            "depth": depth,
            "agent": agent,
            "title": title,
            "created_at": created_at,
            "cost": round(cost, 6),
            "model_name": model_name,
            "tokens_input": acc["input"],
            "tokens_output": acc["output"],
            "tokens_reasoning": acc["reasoning"],
            "tokens_cache_read": acc["cache_read"],
            "tokens_cache_write": acc["cache_write"],
            "tokens_total": acc["tokens_total"],
        }

    def cache_inputs(self) -> list[str]:
        # The DB file whose (size, mtime) fingerprints the warm-start cache, plus its
        # WAL sidecars: if Hermes runs SQLite in WAL mode, new sessions land in
        # <db>-wal and the main .db's mtime doesn't move until a checkpoint, so
        # fingerprinting the .db alone would let a reload serve a stale cache. Missing
        # sidecars (a non-WAL DB) are simply skipped by the fingerprint's stat().
        #
        # The agent logs are NOT listed. They are the Turns source, and Turns is a lazy
        # per-session drill-in that CachedStore never intercepts (it wraps only
        # workflows/model_breakdown) -- so a log that grew since the last parse is
        # re-read on the next drill anyway, while adding it here would bust the whole
        # rollup cache on every line the gateway writes.
        return [self.db_path, self.db_path + "-wal", self.db_path + "-shm"]

    # Hermes' `messages` table HAS a `token_count` column and NEVER populates it
    # (0 of 2,474 rows on a real DB, verified 2026-08-07), which is why this backend
    # had no Turns tab for so long. The per-call usage does exist, but only in the
    # agent log, one line per API call:
    #
    #   2026-08-02 07:44:55,605 INFO [cron_1aa6af6a8637_20260802_074432]
    #   agent.conversation_loop: API call #3: model=gpt-5.6-sol provider=openai-codex
    #   in=34209 out=56 total=34265 latency=3.2s cache=32256/34209 (94%)
    #
    # The bracketed id is the session id and joins straight to `sessions.id` (56 of 59
    # log ids matched on a real machine; the rest are sessions since deleted).
    #
    # Two readings of that line are load-bearing, both verified rather than assumed:
    # `in` INCLUDES the cache read (it is the denominator of the cache=x/y fraction,
    # and in + out == total exactly), so the uncached input is in - cache_read -- which
    # is the shape the rest of this store already reports (input uncached, cache_read
    # separate). Getting that backwards would double-count the cached tokens under "$".
    _LOG_CALL_RE = re.compile(
        r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.](?P<ms>\d+)\s+\w+\s+"
        r"\[(?P<sid>[^\]]+)\]\s+agent\.conversation_loop:\s+API call #(?P<n>\d+):\s+"
        r"model=(?P<model>\S+)\s+provider=(?P<provider>\S+)\s+"
        r"in=(?P<in>\d+)\s+out=(?P<out>\d+)\s+total=(?P<total>\d+)"
        r"(?:.*?\bcache=(?P<cache>\d+)/\d+)?"
    )

    def _log_dir(self) -> str:
        # The logs live beside the DB (~/.hermes/state.db -> ~/.hermes/logs).
        return os.path.join(os.path.dirname(os.path.abspath(self.db_path)), "logs")

    def _log_files(self) -> list[str]:
        # agent.log plus its rotations. Sorted oldest-last by NAME (agent.log,
        # agent.log.1, agent.log.2 ...) is the wrong order for reading -- .1 is older
        # than the live file -- but order does not matter here: every row carries its
        # own timestamp and message_timeline sorts on it.
        return sorted(glob.glob(os.path.join(self._log_dir(), "agent.log*")))

    def _log_turns(self) -> dict[str, list[dict]]:
        """session id -> its per-API-call turn rows, parsed once and memoized.

        Only the retained log window has turns (rotation drops the oldest), so
        `supports_turns` is genuinely PER SESSION here -- an older session keeps its
        rollup and simply offers no tab, which is exactly what the per-session gate in
        the App<->store contract is for.
        """
        # Keyed on the LOGS' own fingerprint, not merely memoized: the gateway appends
        # to agent.log while opentab is open, and relying on workflows() to clear this
        # is not enough, because CachedStore serves a warm rollup WITHOUT calling
        # through to workflows() at all. Worse, the case where the log grows but the DB
        # does not is a real one here -- it is exactly the resume gap that makes log
        # turns exceed the session counters -- so a DB-fingerprinted cache can miss
        # calls the log already has. Stat is cheap; the parse behind it is not.
        stamp = self._log_stamp()
        if self._turns_by_session is not None and self._turns_stamp == stamp:
            return self._turns_by_session
        out: dict[str, list[dict]] = {}
        for path in self._log_files():
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        # Cheap reject before the regex: these files are megabytes of
                        # lines that are not API-call records.
                        if "API call #" not in line:
                            continue
                        m = self._LOG_CALL_RE.match(line)
                        if m:
                            out.setdefault(m.group("sid"), []).append(self._log_turn_row(m))
            except OSError:
                continue  # an unreadable rotation costs its window, never the tab
        self._turns_by_session = out
        self._turns_stamp = stamp
        return out

    def _log_stamp(self) -> tuple:
        # (path, size, mtime_ns) per log file -- the CachedStore fingerprint shape,
        # applied in-process. A rotation changes the set, an append changes a size.
        out = []
        for path in self._log_files():
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append((path, st.st_size, st.st_mtime_ns))
        return tuple(out)

    def _db_stamp(self) -> tuple:
        # Tool attribution joins log rows to the live SQLite state. Hermes writes the
        # log first and the assistant message shortly afterward, so the DB half must
        # invalidate independently even when no newer log line arrives.
        out = []
        for path in (self.db_path, self.db_path + "-wal"):
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append((path, st.st_size, st.st_mtime_ns))
        return tuple(out)

    @classmethod
    def _log_turn_row(cls, m: re.Match) -> dict:
        # Through util.safe_int like every other backend's usage fields, not bare int().
        # The regex only guarantees DIGITS, not a sane magnitude: a corrupt line carrying
        # a 400-digit count parses fine here and then raises OverflowError a layer away,
        # the moment pricing multiplies it by a float rate ("$" on the Turns tab) -- one
        # bad log line taking down the render of every session in the window.
        total = safe_int(m.group("total"))
        raw_in, out_t = safe_int(m.group("in")), safe_int(m.group("out"))
        cache_read = min(safe_int(m.group("cache") or 0), raw_in)  # a subset of `in`
        inp = max(0, raw_in - cache_read)  # ...so the uncached remainder is what's left
        return {
            "ts": m.group("ts"),  # local already -- _log_local() validates, never converts
            # Same stamp at millisecond precision. The tool join needs it: an assistant
            # message persists ~1 ms after its call's log line, so truncating to whole
            # seconds makes the two indistinguishable and the causal order a coin flip.
            "ts_ms": m.group("ts") + "." + (m.group("ms") + "000")[:3],
            "depth": 0,  # the tree is sessions, not turns -- a turn is never a subagent
            "agent": "-",
            "model_name": cls._prefix_model(m.group("model"), m.group("provider")),
            # Hermes records cost per SESSION, never per call, and every session on a
            # real machine is billing_mode='subscription_included' at $0 -- so 0 here is
            # the recorded truth, and "$" estimates these tokens at list price like any
            # other subscription backend. A metered session would show its real spend in
            # the rollups and $0 per turn; that is honest (the split is not recorded)
            # rather than an invented per-turn attribution.
            "cost": 0.0,
            "input": inp,
            "output": out_t,
            "reasoning": 0,  # folded into `out` by the provider; never counted twice
            "cache_read": cache_read,
            "cache_write": 0,  # the log carries no write figure
            "tokens_total": total,
            "tools": [],  # the log names no tools; the DB's are not per-call
        }

    @staticmethod
    def _log_local(ts: str) -> str:
        """A log stamp as opentab's localtime string -- validated, never converted.

        `%(asctime)s` under a plain logging.Formatter is **local** time: the stdlib
        formatter uses time.localtime and Hermes installs no `converter = gmtime`
        (checked in its hermes_logging.py). The log is written by the gateway on the
        same host that opentab reads it from, so its clock IS the reader's clock and
        the string is already in the target format.

        This shipped as a UTC->local conversion, which is invisible on the box it was
        developed against (that host runs UTC, so the shift is zero) and wrong by the
        offset anywhere else: a call logged 07:45 in Berlin became 09:45 and jumped
        ahead of prompts it actually preceded, mis-grouping the whole tab. Converting
        also broke monotonicity across a fall-back DST hour, where two different local
        stamps map to one wall time.
        """
        try:
            datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return ""
        return ts

    def workflows(self) -> list[Workflow]:
        self._sessions = None  # reload on `r`
        # ...and the log-derived turns with them. The gateway appends to agent.log while
        # opentab is open, so a memo held across a reload would keep serving the calls
        # that existed at launch -- the Turns tab silently frozen while every rollup
        # around it moved. App clears its own per-session turn cache on reload; this is
        # the store-side half, and without it that clear just re-fetches the stale map.
        self._turns_by_session = None
        sessions = self._parse()
        rows = []
        for sid, s in sessions.items():
            rows.append(
                Workflow(
                    id=sid,
                    title=s["title"],
                    directory=s["directory"],
                    created_at=s["created_at"],
                    root_cost=s["root_cost"],
                    total_cost=s["total_cost"],
                    subagents=len(s["subagents"]),
                    model_count=0,  # filled by App._load_model_cache
                    total_tokens=s["total_tokens"],
                    unpriced_tokens=s["unpriced_tokens"],
                    source=self.source_name,
                    ended_at=s["ended_at"],
                    worked_seconds=s["worked_seconds"],
                )
            )
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return rows

    def _demo_workflow(self, w: Workflow) -> Workflow:
        return scramble_workflow(w, self.demo_scale, self.demo_cats)

    def summary(self, workflows: list[Workflow]) -> dict[str, int | float]:
        return {
            "workflows": len(workflows),
            "cost": sum(w.total_cost for w in workflows),
            "tokens": sum(w.total_tokens for w in workflows),
            "subagents": sum(w.subagents for w in workflows),
            "unpriced_tokens": sum(w.unpriced_tokens for w in workflows),
            "paid_workflows": sum(1 for w in workflows if w.total_cost > 0),
        }

    def model_breakdown(self) -> list[dict]:
        out: list[dict] = []
        for s in self._parse().values():
            out.extend(s["model_rows"])
        return out

    def recent_roots(self) -> list[dict]:
        # Root sessions newest-activity-first for the one-shot --status command
        # (the Store.recent_roots contract): activity is the newest message
        # anywhere in the subtree, falling back to started_at -- state.db has no
        # per-session updated column. Two plain SELECTs and a Python rollup
        # instead of an adaptive recursive CTE: the sessions table is small and
        # the schema is probed, not guaranteed. No status_nodes is needed on top
        # -- workflow_nodes' backing parse is these same queries, not a file scan.
        if "id" not in self._cols:
            return []
        try:
            conn = self._connect()
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute(self._select_sql()).fetchall()
            last_msg: dict[str, object] = {}
            try:
                for sid, ts in conn.execute(
                    "SELECT session_id, MAX(timestamp) FROM messages GROUP BY session_id"
                ):
                    last_msg[sid] = ts
            except sqlite3.Error:
                pass  # no messages table -> started_at carries the ordering
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        info = {r["id"]: r for r in rows}
        parents = {sid: row["parent_session_id"] for sid, row in info.items()}
        best: dict[str, list] = {}  # root id -> [last_active ms, directory]
        for r in rows:
            active = max(self._epoch_ms(last_msg.get(r["id"])), self._epoch_ms(r["started_at"]))
            cur = self._root_from_parents(r["id"], parents) or r["id"]
            row = best.setdefault(cur, [0, info[cur]["cwd"] or "(unknown)"])
            row[0] = max(row[0], active)
        out = [{"id": rid, "last_active": v[0], "directory": v[1]} for rid, v in best.items()]
        out.sort(key=lambda r: r["last_active"], reverse=True)
        return out

    def root_of(self, session_id: str) -> str | None:
        # Resolve any session id to its root by walking parent_session_id upward
        # (the Store.root_of contract); None when the id is unknown or archived --
        # also the cheap membership probe --status uses to find which backend a
        # bare id belongs to.
        if "id" not in self._cols:
            return None
        parent_col = "parent_session_id" if "parent_session_id" in self._cols else "NULL"
        where = " AND archived = 0" if "archived" in self._cols else ""
        try:
            conn = self._connect()
        except sqlite3.Error:
            return None
        try:
            rows = conn.execute(
                f"SELECT id, {parent_col} AS parent FROM sessions WHERE 1 = 1{where}"
            ).fetchall()
            return self._root_from_parents(session_id, {r["id"]: r["parent"] for r in rows})
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    @staticmethod
    def _root_from_parents(session_id: str, parents: dict[str, str | None]) -> str | None:
        """Resolve a root consistently for normal, orphaned, and cyclic metadata."""
        if session_id not in parents:
            return None
        path: list[str] = []
        positions: dict[str, int] = {}
        cur = session_id
        while True:
            if cur in positions:
                # _parse promotes the lexically first member of an unreachable cycle.
                return min(path[positions[cur] :])
            positions[cur] = len(path)
            path.append(cur)
            parent = parents.get(cur)
            if not parent or parent not in parents:
                return cur
            cur = parent

    def workflow_nodes(self, workflow_id: str) -> list[dict]:
        s = self._parse().get(workflow_id)
        if not s:
            return []
        nodes = [
            self._node(
                workflow_id,
                0,
                "-",
                s["title"],
                s["created_at"],
                s["model"],
                s["root_cost"],
                s["root_acc"],
            )
        ]
        nodes.extend(dict(n) for n in s["subagents"])
        if self.demo:
            nodes = [self._demo_node(n) for n in nodes]
        return nodes

    def _demo_node(self, n: dict) -> dict:
        return scramble_node(n, self.demo_scale, self.demo_cats)

    def _subtree_ids(self, workflow_id: str) -> list[tuple[str, int, str]]:
        """(session id, depth, agent label) for a root and every session under it.

        A Hermes root's ROLLUP already includes its descendants (parent_session_id
        forms the subagent tree and workflows() folds children into the root's
        totals), so its Turns must cover them too or the tab reports less than the
        header above it -- and a root whose own calls have rotated out of the log
        while a child's survive would hide the tab on a session that has turns.

        ARCHIVED sessions are excluded at every step, exactly as _select_sql excludes
        them from the rollup: a subtree wider than the totals above it is the same bug
        in the other direction, and the more damaging one -- the Turns tab would sum
        past its own header with no way to see why. Skipping an archived session also
        stops the recursion there, which is what _parse already does: a child whose
        parent is archived does not resolve, so it becomes its OWN root (see the roots
        comment there) and its turns belong to that session's tab, not to this one.
        """
        try:
            conn = self._connect()
        except sqlite3.Error:
            return [(workflow_id, 0, "-")]
        archived = "archived" in self._cols
        root_live = " AND archived = 0" if archived else ""
        child_live = " AND c.archived = 0" if archived else ""
        try:
            # The depth bound is a cycle guard, not a shape assumption. _parse() walks
            # the child map with a visited set, but a parent cycle it breaks can still
            # be promoted to a workflow root and arrive here, where UNION ALL has no
            # such guard and would recurse until SQLite's own limit.
            rows = conn.execute(
                f"""
                WITH RECURSIVE tree(id, depth) AS (
                  SELECT id, 0 FROM sessions WHERE id = ?{root_live}
                  UNION ALL
                  SELECT c.id, tree.depth + 1 FROM sessions c
                  JOIN tree ON c.parent_session_id = tree.id
                  WHERE tree.depth < {self._MAX_TREE_DEPTH}{child_live}
                )
                SELECT tree.id, tree.depth, COALESCE(s.title, '') FROM tree
                JOIN sessions s ON s.id = tree.id
                """,
                [workflow_id],
            ).fetchall()
            # The depth bound prevents an infinite CTE, but a cycle still emits the
            # same ids repeatedly at deeper levels. Keep the first occurrence so Turns
            # and Tools never multiply one session's usage.
            unique = []
            seen = set()
            for row in rows:
                if row[0] not in seen:
                    unique.append(row)
                    seen.add(row[0])
            rows = unique
            # Subagent labels take the SAME precedence as the rollup: Hermes' own
            # title first, else the session's first real user prompt. Reading the raw
            # column here would label an untitled child "-" in the Turns agent column
            # while the Subagents view shows it under its prompt-derived title.
            untitled = [r[0] for r in rows if r[1] and not (r[2] or "").strip()]
            fallbacks = self._fallback_titles(conn, untitled) if untitled else {}
        except sqlite3.Error:
            return [(workflow_id, 0, "-")]  # no parent column on an older schema
        finally:
            conn.close()
        if not rows:
            return [(workflow_id, 0, "-")]
        return [
            (r[0], int(r[1]), ((r[2] or "").strip() or fallbacks.get(r[0]) or "-") if r[1] else "-")
            for r in rows
        ]

    @staticmethod
    def _parse_tool_calls(blob) -> list[str]:
        """Tool names out of a Hermes `messages.tool_calls` blob, or nothing.

        The stored shape is OpenAI's: a JSON list of
        ``{"function": {"name": ..., "arguments": ...}}``. `arguments` is never
        parsed -- it is irrelevant here and can be large or malformed.

        ALL-OR-NOTHING on purpose. tool_names() would drop one bad entry and keep the
        rest, but the attribution downstream splits a turn's tokens evenly across the
        names returned: dropping one of three hands the survivors 50% each instead of
        33%, silently overstating them. A blob we cannot fully trust yields no tools,
        so the turn keeps its tokens and simply reports none -- the same fail-closed
        rule the usage-row reconciliation uses.
        """
        if not blob:
            return []
        try:
            data = json.loads(blob) if isinstance(blob, (str, bytes, bytearray)) else blob
        except (ValueError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        names: list[str] = []
        for entry in data:
            if not isinstance(entry, dict):
                return []
            fn = entry.get("function")
            if not isinstance(fn, dict):
                return []
            name = fn.get("name")
            if not isinstance(name, str) or not name:
                return []
            names.append(name)  # duplicates and order are kept: each is one real call
        return tool_names(names)  # defence in depth; the shape is already validated

    def _tool_events(self, ids: list[str]) -> dict[str, list[tuple[str, str, list[str]]]]:
        # (local ms stamp, role, tool names) per session, in time order. EVERY message
        # is returned, not just the tool-calling ones: the non-tool rows are the
        # landmarks that stop a pending call from binding across an intervening event.
        out: dict[str, list[tuple[str, str, list[str]]]] = {}
        if not ids:
            return out
        try:
            conn = self._connect()
        except sqlite3.Error:
            return out
        try:
            marks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT session_id, role, tool_calls, timestamp FROM messages "
                f"WHERE session_id IN ({marks}) ORDER BY timestamp",
                ids,
            ).fetchall()
        except sqlite3.Error:
            return out  # no messages table, or no tool_calls column: no tools, no error
        finally:
            conn.close()
        for sid, role, blob, ts in rows:
            stamp = self._ts_to_local_ms(safe_float(ts))
            if stamp:
                out.setdefault(sid, []).append((stamp, role or "", self._parse_tool_calls(blob)))
        return out

    def _enriched_turns(self, subtree: list[tuple]) -> dict[str, list[dict]]:
        """Log turns with their tool names attached, by causal sequence.

        The two halves of a Hermes turn live in different stores: the tokens in the
        rotating log, the tool names in the DB. Hermes logs a call's usage BEFORE it
        validates and persists the assistant response, so a call can be logged and then
        rejected (invalid or truncated tool calls have their own retry path) leaving no
        assistant row at all. Measured on a real corpus: 12 of 118 logged sessions have
        more logged calls than assistant messages.

        So this is NOT "each call takes the next assistant message" -- that hands the
        rejected call's tokens to the retry's tools. It is a merge of both event streams
        in time order where a newer call SUPERSEDES an older unmatched one, and any
        non-assistant event in between cancels the pending match. A call that never
        pairs keeps its tokens and reports no tools; it never shifts the rest.

        Returns copies: _log_turns() memoizes on the LOG's fingerprint alone, and
        writing DB-derived state into those rows would poison that cache.
        """
        ids = [sid for sid, _, _ in subtree]
        # message_timeline, tool_breakdown and supports_tools all ask for the same
        # subtree while one session is open. Both sources are fingerprinted: the log
        # can grow, and its matching DB message can be persisted a moment later.
        key = (self._log_stamp(), self._db_stamp(), tuple(ids))
        hit = getattr(self, "_enriched_cache", None)
        if hit is not None and hit[0] == key:
            return hit[1]
        turns = self._log_turns()
        out = {sid: [dict(t) for t in turns.get(sid, ())] for sid in ids}
        if not any(out.values()):
            return out
        events = self._tool_events(ids)
        for sid, rows in out.items():
            msgs = events.get(sid)
            if not msgs or not rows:
                continue
            # Rank 0 before rank 1 on an equal stamp: the log line is written first.
            merged = [(r.get("ts_ms") or r["ts"], 0, r) for r in rows]
            merged += [(stamp, 1, (role, tools)) for stamp, role, tools in msgs]
            merged.sort(key=lambda e: (e[0], e[1]))
            pending: dict | None = None
            for _, rank, payload in merged:
                if rank == 0 and isinstance(payload, dict):
                    pending = payload  # supersedes any earlier call still unmatched
                    continue
                if not isinstance(payload, tuple):
                    continue
                role, tools = payload
                if role == "assistant":
                    if pending is not None and tools:
                        pending["tools"] = list(tools)
                    pending = None
                else:
                    pending = None  # an intervening event breaks the association
        self._enriched_cache = (key, out)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        # PER SESSION, unlike every other backend's blanket True: the turns come from a
        # rotating log, so only sessions inside the retained window have any. An older
        # session keeps its rollup and simply shows no tab -- which beats a tab that
        # opens empty and looks like a parsing bug. Asked of the whole SUBTREE, since
        # that is what the tab draws.
        turns = self._log_turns()
        return any(turns.get(sid) for sid, _, _ in self._subtree_ids(workflow_id))

    def tool_breakdown(self, workflow_id: str) -> list[dict]:
        # Per-(tool, model) attribution over the whole SUBTREE, so the tab agrees with
        # the root's descendant-inclusive rollup exactly as its Turns tab does. Tools
        # are a strict projection of the very rows message_timeline draws -- both read
        # _enriched_turns -- so no token can appear here that the Turns tab does not
        # also show, and a turn that matched no assistant message contributes nothing.
        rows = [
            t
            for turns in self._enriched_turns(self._subtree_ids(workflow_id)).values()
            for t in turns
        ]
        return tool_rows_from_turns(rows)

    def supports_tools(self, workflow_id: str) -> bool:
        # PER SESSION, like supports_turns and unlike the blanket True other backends
        # return: the names live in the DB but the TOKENS come from the rotating log,
        # so a session aged out of the log has tool calls on record and nothing to
        # attribute to them. Gate on a turn that actually matched, never on the DB
        # alone -- otherwise the tab opens empty and reads as a parsing bug.
        return any(
            t.get("tools")
            for turns in self._enriched_turns(self._subtree_ids(workflow_id)).values()
            for t in turns
        )

    def message_timeline(self, workflow_id: str) -> list[dict]:
        # One row per API call, chronological, each tagged with the prompt that
        # triggered it. The usage comes from the log; the PROMPTS come from the DB's
        # `messages` table (which stores content but no tokens -- the two halves of a
        # turn live in different places on this backend), joined in the lockstep the
        # other file backends use: the latest user message at ts <= the call owns it.
        #
        # Over the whole SUBTREE, interleaved by time: a Hermes root's rollup folds its
        # children in, so its Turns must too, each child row tagged depth/agent the way
        # every hierarchical backend marks a subagent.
        #
        # The lockstep runs PER SESSION, not once over the root's prompts. A Hermes
        # subagent is a session in its own right and holds its own user messages (the
        # delegated task and its follow-ups -- 9 of them on the one real child in the
        # corpus this was checked against), so walking every row against the root's
        # prompt list files the child's calls under whichever root prompt happened to
        # precede them: work attributed to a request that never asked for it, and the
        # child's own prompts listed nowhere. A session with no prompts of its own
        # leaves its rows headerless (the Copilot shape), which is honest -- better an
        # unlabelled group than a wrong label.
        subtree = self._subtree_ids(workflow_id)
        turns = self._enriched_turns(subtree)
        rows: list[dict] = []
        for sid, depth, agent in subtree:
            # Demo the AGENT here, where the child's session id is still in hand: the
            # label is a real session TITLE on this backend (not a nickname or a role,
            # as on Codex/omp), so it is exactly the kind of text demo mode exists to
            # keep off the screen -- and seeding it off the child's id gives the same
            # fake the Subagents tab shows for that node.
            label = "-"
            if depth:
                label = demo_title(sid) if self.demo and "titles" in self.demo_cats else agent
            for r in turns.get(sid, ()):
                rows.append(dict(r, sid=sid, depth=depth, agent=label))
        if not rows:
            return []
        prompts = self._session_prompts([sid for sid, _, _ in subtree])
        # (session id -> its cursor into that session's prompts, and the prompt in force)
        state: dict[str, list] = {sid: [0, "", ""] for sid, _, _ in subtree}
        out: list[dict] = []
        # The stamps are local wall clock in a fixed-width format, so they sort
        # lexicographically in time order. Keep milliseconds for ownership: a prompt
        # written later in the same second must not claim an earlier API call.
        for r in sorted(rows, key=lambda x: x.get("ts_ms") or x["ts"]):
            local = self._log_local(r["ts"])
            local_ms = r.get("ts_ms") or local + ".000"
            row = dict(r)
            sid = row.pop("sid", workflow_id)
            own = prompts.get(sid, [])
            cur = state.setdefault(sid, [0, "", ""])
            while cur[0] < len(own) and own[cur[0]]["time_ms"] <= local_ms:
                cur[1], cur[2] = own[cur[0]]["id"], own[cur[0]]["title"]
                cur[0] += 1
            row["time"] = local
            row.pop("ts", None)
            row.pop("ts_ms", None)
            row["prompt_id"] = cur[1]
            row["prompt_title"] = cur[2]
            row["prompt_full"] = cur[2]
            out.append(row)
        if self.demo:
            out = [self._demo_turn(r) for r in out]
        return out

    def _demo_turn(self, r: dict) -> dict:
        # Titles are the only sensitive field on a turn row (magnitudes are scaled by
        # App._scale_demo_turns, as for every other backend). A real prompt on an
        # anonymised screen is exactly what demo exists to prevent.
        r = dict(r)
        if "titles" in self.demo_cats:
            r["prompt_title"] = r["prompt_full"] = demo_title(r.get("prompt_id") or "noprompt")
        return r

    def _session_prompts(self, ids: list[str]) -> dict[str, list[dict]]:
        """Each session's user prompts, oldest first, for the Turns tab's ▸ grouping.

        Read straight from `messages` rather than from _parse()'s rollup: this is a
        per-session drill-in, so it must not drag the whole corpus parse in behind it.
        Takes the whole subtree in ONE query rather than a query per session -- the
        drill-in is on the paint path, and a root with a dozen subagents would otherwise
        open the DB a dozen times to build one tab.
        """
        if not ids:
            return {}
        try:
            conn = self._connect()
        except sqlite3.Error:
            return {}
        try:
            rows = conn.execute(
                "select session_id, timestamp, content from messages "
                f"where session_id in ({','.join('?' * len(ids))}) "
                "and role = 'user' and content is not null "
                "order by session_id, timestamp",
                list(ids),
            ).fetchall()
        except sqlite3.Error:
            return {}  # an older schema without the table/columns simply has no headers
        finally:
            conn.close()
        out: dict[str, list[dict]] = {}
        for r in rows:
            sid = str(r["session_id"])
            title = self._title_from_content(str(r["content"] or ""))
            if not title:
                continue
            own = out.setdefault(sid, [])
            own.append(
                {
                    # The ordinal is per SESSION, so a child's prompts group under their
                    # own headers instead of colliding with the root's ids.
                    "id": f"{sid}:{len(own)}",
                    "time": self._ts_to_local(safe_float(r["timestamp"])),
                    "time_ms": self._ts_to_local_ms(safe_float(r["timestamp"])),
                    "title": title,
                }
            )
        return out
