"""Hermes Agent SQLite backend."""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import datetime

from opentab.demo import demo_cost, demo_dir, demo_model, demo_title
from opentab.models import Workflow
from opentab.util import git_root


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
        self.demo = getattr(args, "demo", False)
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._sessions: dict[str, dict] | None = None
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

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        if "id" not in self._cols:  # unreadable / unexpected schema -> empty, never crash
            self._sessions = {}
            return self._sessions
        conn = self._connect()
        try:
            rows = conn.execute(self._select_sql()).fetchall()
        finally:
            conn.close()

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
                "title": row["title"] or "(untitled)",
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
            }

        # Map root_id -> list of direct children
        children: dict[str, list[str]] = {}
        for sid, s in flat.items():
            pid = s["parent_id"]
            if pid and pid in flat:
                children.setdefault(pid, []).append(sid)

        result: dict[str, dict] = {}
        for sid, s in flat.items():
            if s["parent_id"]:
                continue  # handled as a subagent node under its root
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
            subagent_nodes: list[dict] = []
            # model_name -> {runs, cost, r_cost, inp/out/cr/cw, u_* (unpriced), ru_* (root unpriced)}
            model_acc: dict[str, dict] = {}

            bfs_queue: list[tuple[str, int, bool]] = [(sid, 0, True)]
            while bfs_queue:
                node_id, depth, is_root = bfs_queue.pop(0)
                node = flat[node_id]
                tot_total += node["tokens_total"]
                tot_cost += node["cost"]
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

            result[sid] = {
                "title": s["title"],
                "directory": directory,
                "created_at": created_at,
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
        return [self.db_path, self.db_path + "-wal", self.db_path + "-shm"]

    def workflows(self) -> list[Workflow]:
        self._sessions = None  # reload on `r`
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
                )
            )
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return rows

    def _demo_workflow(self, w: Workflow) -> Workflow:
        w.title = demo_title(w.id)
        w.directory = demo_dir(w.id)
        if w.unpriced_tokens > 0:
            add = demo_cost(w.unpriced_tokens, w.id)
            w.total_cost += add
            w.root_cost += add
            w.unpriced_tokens = 0
        w.total_cost = round(w.total_cost * self.demo_scale, 4)
        w.root_cost = round(w.root_cost * self.demo_scale, 4)
        w.total_tokens = int(round(w.total_tokens * self.demo_scale))
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

    def model_breakdown(self) -> list[dict]:
        out: list[dict] = []
        for s in self._parse().values():
            out.extend(s["model_rows"])
        return out

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
        n["title"] = demo_title(n["id"])
        n["model_name"] = demo_model(n["model_name"])
        if n["cost"] == 0:
            n["cost"] = demo_cost(n["tokens_total"], n["id"])
        n["cost"] = round(n["cost"] * self.demo_scale, 4)
        for f in (
            "tokens_input",
            "tokens_output",
            "tokens_reasoning",
            "tokens_cache_read",
            "tokens_cache_write",
            "tokens_total",
        ):
            n[f] = int(round(n[f] * self.demo_scale))
        return n
