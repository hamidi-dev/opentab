"""Hermes Agent SQLite backend."""
from __future__ import annotations

import argparse
import glob
import os
import re
import sqlite3
from datetime import datetime, timezone

from opentab.demo import demo_config, demo_title, scramble_node, scramble_workflow
from opentab.formatting import worked_seconds
from opentab.models import Workflow
from opentab.util import git_root, safe_float


class HermesStore:
    """Read Hermes Agent sessions (~/.hermes/state.db) behind the same interface
    App expects from Store: workflows(), summary(), workflow_nodes(),
    model_breakdown(), plus the .demo/.demo_scale attributes.

    Token model is provider-agnostic. Hermes works with any provider (OpenAI,
    Anthropic, Google, OpenRouter, Nous, local, ...) but normalizes every one to
    a single canonical shape *before* writing the row (see hermes-agent's
    usage_pricing.canonicalize_usage): input_tokens is the **uncached** prompt
    (cache_read_tokens / cache_write_tokens are tracked separately, never folded
    in), and output_tokens already **includes** reasoning_tokens as a subset
    (OpenAI convention, preserved for all providers). So total = input + output +
    cache_read + cache_write, reasoning is priced once via output, and there is no
    per-provider special-casing to do here.

    Cost is mixed. Subscription routes (e.g. openai-codex) record billing_mode
    'subscription_included' and $0, so their tokens are "unpriced" and the "$"
    what-if machinery reprices them at list rates. Metered routes (OpenRouter,
    Nous, direct API keys) DO record a per-session cost in estimated_cost_usd /
    actual_cost_usd; per session a recorded cost (actual preferred, else
    estimated) is trusted as real/priced and shown in normal mode. records_cost is
    True iff any live session carries a recorded cost (computed once at init,
    since CombinedStore reads it before workflows()).

    Titles: sessions.title when Hermes set one; otherwise the first real user
    prompt from the messages table (api_server/voice sessions are never titled by
    Hermes). Hermes wraps injected context in leading "[ ... ]" blocks ("[Note:
    model was just switched...]", "[CONTEXT COMPACTION ...]"), which are stripped
    -- except a voice turn, whose whole prompt lives inside one such block as
    '[The user sent a voice message~ Here's what they said: "..."]'; that quoted
    transcript is the title.

    Turns are assembled from BOTH halves of the install, because neither half holds
    the whole story: `messages.token_count` exists but is never populated (0 of
    2,474 rows on a real DB), so per-call usage is read from the agent LOG
    (~/.hermes/logs/agent.log*, one "API call #N: ... in= out= total= cache=" line
    per call, carrying the session id it belongs to), while the PROMPT text the tab
    groups by comes from the messages table, which stores content but no tokens.
    Because that log rotates, `supports_turns` is genuinely per-session -- a session
    older than the retained window keeps its rollup and simply offers no tab.

    Sessions with a parent_session_id form a subagent tree; HermesStore rolls
    child tokens/cost up into the root's totals. cwd is resolved to the git repo
    root. Archived sessions are excluded. The schema is probed (Store-style) so
    the backend degrades gracefully if optional columns are absent.
    """

    combined = False
    source_name = "Hermes"

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
        # Demo mode: which categories to scramble (titles/turns/spend) and the
        # hidden magnitude factor (1.0 unless spend is scrambled). See demo_config.
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
        if not cost_cols:
            return False
        clauses = ["archived = 0"] if "archived" in self._cols else []
        clauses.append("(" + " OR ".join(f"COALESCE({c}, 0) > 0" for c in cost_cols) + ")")
        sql = f"SELECT EXISTS(SELECT 1 FROM sessions WHERE {' AND '.join(clauses)})"
        try:
            conn = self._connect()
        except sqlite3.Error:
            return False
        try:
            return bool(conn.execute(sql).fetchone()[0])
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _select_sql(self) -> str:
        parts = ["id"]
        for name, default in self._COLS:
            parts.append(name if name in self._cols else f"{default} AS {name}")
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

    def _fallback_titles(self, conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
        # First real user prompt for sessions Hermes left untitled (api_server /
        # voice sessions never get one) -- the ClaudeStore title precedence. One
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
                    "title": row["title"] or "",
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
                if node["cost"] <= 0:
                    tot_unpriced += node["tokens_total"]
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
        m = model_acc.get(node["model"])
        if m is None:
            m = model_acc[node["model"]] = {
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
        i, o, cr, cw, cost = node["inp"], node["out"], node["cr"], node["cw"], node["cost"]
        m["runs"] += 1
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

    # --- Turns, from the agent log ---------------------------------------------
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
        r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+\s+\w+\s+"
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

    @classmethod
    def _log_turn_row(cls, m: re.Match) -> dict:
        total = int(m.group("total"))
        raw_in, out_t = int(m.group("in")), int(m.group("out"))
        cache_read = min(int(m.group("cache") or 0), raw_in)  # a subset of `in`
        inp = max(0, raw_in - cache_read)  # ...so the uncached remainder is what's left
        return {
            "ts": m.group("ts"),  # naive UTC; _log_local() converts at read time
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
        best: dict[str, list] = {}  # root id -> [last_active ms, directory]
        for r in rows:
            active = max(self._epoch_ms(last_msg.get(r["id"])), self._epoch_ms(r["started_at"]))
            cur, seen = r["id"], {r["id"]}
            while True:  # walk to the root (cycle-guarded); a busy child bumps its root
                parent = info[cur]["parent_session_id"]
                if not parent or parent not in info or parent in seen:
                    break
                seen.add(parent)
                cur = parent
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
            cur, seen = session_id, set()
            while cur not in seen:
                seen.add(cur)
                row = conn.execute(
                    f"SELECT {parent_col} AS parent FROM sessions WHERE id = ?{where}", [cur]
                ).fetchone()
                if row is None:
                    # unknown id -- or a parent pointer whose session is gone, in
                    # which case the last session that did exist is the root
                    return None if cur == session_id else cur
                if not row["parent"]:
                    return cur
                cur = row["parent"]
            return cur  # cyclic parent metadata: stop where the walk closed
        except sqlite3.Error:
            return None
        finally:
            conn.close()

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
        """
        try:
            conn = self._connect()
        except sqlite3.Error:
            return [(workflow_id, 0, "-")]
        try:
            rows = conn.execute(
                """
                WITH RECURSIVE tree(id, depth) AS (
                  SELECT id, 0 FROM sessions WHERE id = ?
                  UNION ALL
                  SELECT c.id, tree.depth + 1 FROM sessions c
                  JOIN tree ON c.parent_session_id = tree.id
                )
                SELECT tree.id, tree.depth, COALESCE(s.title, '') FROM tree
                JOIN sessions s ON s.id = tree.id
                """,
                [workflow_id],
            ).fetchall()
        except sqlite3.Error:
            return [(workflow_id, 0, "-")]  # no parent column on an older schema
        finally:
            conn.close()
        if not rows:
            return [(workflow_id, 0, "-")]
        return [(r[0], int(r[1]), (r[2] or "-") if r[1] else "-") for r in rows]

    def supports_turns(self, workflow_id: str) -> bool:
        # PER SESSION, unlike every other backend's blanket True: the turns come from a
        # rotating log, so only sessions inside the retained window have any. An older
        # session keeps its rollup and simply shows no tab -- which beats a tab that
        # opens empty and looks like a parsing bug. Asked of the whole SUBTREE, since
        # that is what the tab draws.
        turns = self._log_turns()
        return any(turns.get(sid) for sid, _, _ in self._subtree_ids(workflow_id))

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
        turns = self._log_turns()
        rows: list[dict] = []
        for sid, depth, agent in self._subtree_ids(workflow_id):
            for r in turns.get(sid, ()):
                rows.append(dict(r, depth=depth, agent=agent if depth else "-"))
        if not rows:
            return []
        prompts = self._session_prompts(workflow_id)
        out: list[dict] = []
        pi, cur_id, cur_title = 0, "", ""
        # The stamps are local wall clock in a fixed-width format, so they sort
        # lexicographically in time order -- and against the prompts' _ts_to_local
        # strings, which are the same clock and the same format.
        for r in sorted(rows, key=lambda x: x["ts"]):
            local = self._log_local(r["ts"])
            while pi < len(prompts) and prompts[pi]["time"] <= local:
                cur_id, cur_title = prompts[pi]["id"], prompts[pi]["title"]
                pi += 1
            row = dict(r)
            row["time"] = local
            row.pop("ts", None)
            row["prompt_id"] = cur_id
            row["prompt_title"] = cur_title
            row["prompt_full"] = cur_title
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

    def _session_prompts(self, workflow_id: str) -> list[dict]:
        """The session's user prompts, oldest first, for the Turns tab's ▸ grouping.

        Read straight from `messages` rather than from _parse()'s rollup: this is a
        per-session drill-in, so it must not drag the whole corpus parse in behind it.
        """
        try:
            conn = self._connect()
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute(
                "select timestamp, content from messages "
                "where session_id = ? and role = 'user' and content is not null "
                "order by timestamp",
                [workflow_id],
            ).fetchall()
        except sqlite3.Error:
            return []  # an older schema without the table/columns simply has no headers
        finally:
            conn.close()
        out = []
        for i, r in enumerate(rows):
            title = self._title_from_content(str(r["content"] or ""))
            if not title:
                continue
            out.append(
                {
                    "id": f"{workflow_id}:{i}",
                    "time": self._ts_to_local(safe_float(r["timestamp"])),
                    "title": title,
                }
            )
        return out
