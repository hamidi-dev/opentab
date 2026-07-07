"""pi-agent JSONL backend."""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re

from opentab.demo import demo_cost, demo_dir, demo_model, demo_title
from opentab.formatting import iso_to_local
from opentab.models import Workflow
from opentab.util import git_root, read_files_parallel


class PiStore:
    """Read pi-agent sessions (~/.pi/agent/sessions/<project>/*.jsonl, or the dir named by
    $PI_AGENT_DIR / --pi-dir) behind the same interface App expects from Store:
    workflows(), summary(), workflow_nodes(), model_breakdown(), plus the
    .demo/.demo_scale attributes -- like the other JSONL backends.

    pi-agent writes a per-message `usage.cost.total` -- but, crucially, it writes a
    *list-price* figure for **every** provider, including subscription/OAuth routes
    (e.g. openai-codex on a ChatGPT plan) whose marginal cost is actually $0. So the cost
    is trustworthy only for **metered** routes (OpenRouter, a direct API key); a
    subscription route's cost is a what-if estimate, not spend. Like `HermesStore`'s
    billing_mode split, a message is classed **subscription** when its provider is an OAuth
    login (`auth.json` type "oauth", read read-only) or matches a known plan marker
    (`_SUBSCRIPTION_MARKERS`: codex/copilot/claude-code/...) -- those tokens are left
    **unpriced** (the "$" view estimates them), while metered messages with a real cost
    price as spend. The two accumulate independently per message, so a session (even one
    model) mixing both routes is split correctly. Cost is therefore mixed, so -- exactly
    like `CsvStore`/`HermesStore` -- **`records_cost` is a per-instance attr** (True iff any
    *metered* message has a cost), set by a cheap early-exit probe in `__init__` so
    `CombinedStore` can read it before `workflows()`.

    Each session file is newline-delimited JSON: a `session` record carries the canonical
    id + **cwd** (so directories fold to the **git root**, no path-decoding the project
    dir name), `message`/`user` records give the title (first user text), and
    `message`/`assistant` records carry `usage`. Token accounting is **Anthropic-style**:
    `input` is already the *uncached* prompt (cacheRead/cacheWrite are tracked separately,
    never folded in), so input is used as-is with no cache subtraction; total =
    input + output + cacheRead + cacheWrite (a record carrying only `totalTokens` back-fills
    the gap as output). Models are recorded already provider-qualified (e.g.
    `moonshotai/kimi-k2.6`), so they're used verbatim for pricing and the Providers rollup.
    Assistant messages are deduped by their stable `id` (resumed/forked files overlap). No
    subagent tree (every session is one depth-0 node); sessions with no recorded usage are
    dropped.
    """

    combined = False
    source_name = "Pi"

    def __init__(self, root_dir: str, args: argparse.Namespace):
        self.root_dir = root_dir
        self.args = args
        self.demo = getattr(args, "demo", False)
        # Same hidden per-process factor Store/CodexStore use; 1.0 outside demo.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._sessions: dict[str, dict] | None = None  # parsed lazily / on reload
        self._git_root_cache: dict[str, str] = {}
        # pi writes a list-price `cost` for *every* provider, even subscription/OAuth routes
        # (e.g. openai-codex on a ChatGPT plan) where the marginal cost is actually $0. So
        # only metered routes count as real spend; subscription routes are unpriced and the
        # "$" view estimates them -- exactly like HermesStore's billing_mode split. The
        # signal: auth.json marks plan logins as type "oauth"; plus a few provider markers.
        self._oauth_providers = self._load_oauth_providers()
        self.records_cost = self._probe_records_cost()

    # --- helpers -------------------------------------------------------------
    def _git_root(self, cwd: str) -> str:
        if cwd not in self._git_root_cache:
            self._git_root_cache[cwd] = git_root(cwd)
        return self._git_root_cache[cwd]

    # Provider/api substrings that mark a subscription (plan-included) route even when
    # auth.json is unavailable -- their recorded cost is a list-price estimate, not spend.
    _SUBSCRIPTION_MARKERS = (
        "codex",
        "copilot",
        "claude-code",
        "claude-max",
        "claude-pro",
        "chatgpt",
    )

    def _load_oauth_providers(self) -> set[str]:
        # auth.json (beside the sessions dir) maps provider -> auth info; type "oauth" means
        # a consumer-plan login (subscription), not a metered API key. Read-only; we only
        # read each provider's "type", never the tokens.
        path = os.path.join(os.path.dirname(os.path.normpath(self.root_dir)), "auth.json")
        out: set[str] = set()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for prov, info in data.items():
                    if isinstance(info, dict) and str(info.get("type", "")).lower() == "oauth":
                        out.add(prov.lower())
        except (OSError, ValueError):
            pass
        return out

    def _is_subscription(self, provider, api) -> bool:
        prov = (provider or "").lower()
        if prov and prov in self._oauth_providers:
            return True
        text = prov + " " + (api or "").lower()
        return any(marker in text for marker in self._SUBSCRIPTION_MARKERS)

    @staticmethod
    def _id_from_name(path: str) -> str | None:
        # Files are <timestamp>_<uuid>.jsonl; the uuid is the session id (== the `session`
        # record's id), so the filename keys even resumed files (same uuid, new timestamp).
        m = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            os.path.basename(path),
        )
        return m.group(1) if m else None

    @staticmethod
    def _int(value) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _cost_total(usage: dict) -> float:
        cost = usage.get("cost")
        if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
            return max(0.0, float(cost["total"]))
        return 0.0

    @staticmethod
    def _user_text(content) -> str:
        # A user message's content is a list of {type, text} parts (or a bare string).
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                c["text"]
                for c in content
                if isinstance(c, dict)
                and c.get("type") == "text"
                and isinstance(c.get("text"), str)
            ]
            return " ".join(p for p in parts if p.strip())
        return ""

    @staticmethod
    def _new_acc() -> dict:
        return {
            "runs": 0,
            "input": 0,  # already uncached (Anthropic-style; cacheRead is separate)
            "output": 0,
            "reasoning": 0,  # pi records none; kept for the shared row schema
            "cache_read": 0,
            "cache_write": 0,
            "tokens_total": 0,
            "cost": 0.0,  # real spend: metered (non-subscription) messages only
            # tokens from subscription routes -> unpriced, so the "$" view estimates them
            "u_input": 0,
            "u_output": 0,
            "u_cache_read": 0,
            "u_cache_write": 0,
        }

    @staticmethod
    def _new_session() -> dict:
        return {
            "cwd": None,
            "ts_min": None,
            "ts_meta": None,  # the `session` record's timestamp, preferred for created_at
            "title_prompt": None,
            "models": {},
            "seen_msgs": set(),  # assistant ids already counted (resume/fork dedup)
        }

    # --- parsing -------------------------------------------------------------
    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore).
        return self._files()

    def _files(self) -> list[str]:
        return glob.glob(os.path.join(self.root_dir, "**", "*.jsonl"), recursive=True)

    def _probe_records_cost(self) -> bool:
        # True iff any *metered* (non-subscription) assistant message records a positive
        # cost. Early-exits so it stays cheap (safe in __init__; CombinedStore reads it
        # before workflows()). A subscription-only setup -> False (every cost is estimated).
        for path in self._files():
            try:
                fh = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"cost"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    msg = o.get("message") if o.get("type") == "message" else None
                    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                        continue
                    if self._cost_total(msg["usage"]) > 0 and not self._is_subscription(
                        msg.get("provider"), msg.get("api")
                    ):
                        return True
        return False

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        for path, text in read_files_parallel(self._files()):
            self._parse_file(path, text.split("\n"), sessions)
        for sid, s in sessions.items():
            self._finalize(sid, s)
        # Drop sessions with no recorded usage (a stub with only session/model_change rows).
        self._sessions = {sid: s for sid, s in sessions.items() if s["model_rows"]}
        return self._sessions

    def _parse_file(self, path: str, lines: list[str], sessions: dict[str, dict]) -> None:
        sid = self._id_from_name(path)
        if not sid:
            return
        s = sessions.setdefault(sid, self._new_session())
        for line in lines:
            if '"type"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            typ = o.get("type")
            ts = o.get("timestamp")
            if ts and (s["ts_min"] is None or ts < s["ts_min"]):
                s["ts_min"] = ts
            if typ == "session":
                if o.get("cwd") and not s["cwd"]:
                    s["cwd"] = o["cwd"]
                if o.get("timestamp") and not s["ts_meta"]:
                    s["ts_meta"] = o["timestamp"]
                continue
            if typ != "message":
                continue
            msg = o.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "user":
                if not s["title_prompt"]:
                    txt = self._user_text(msg.get("content"))
                    if txt.strip():
                        s["title_prompt"] = " ".join(txt.split())[:80]
                continue
            if role != "assistant" or not isinstance(msg.get("usage"), dict):
                continue
            mid = o.get("id")
            if mid is not None:
                if mid in s["seen_msgs"]:
                    continue  # same assistant step in a resumed/forked file
                s["seen_msgs"].add(mid)
            self._apply_usage(s, msg)

    def _apply_usage(self, s: dict, msg: dict) -> None:
        usage = msg["usage"]
        inp = self._int(usage.get("input"))
        out = self._int(usage.get("output"))
        cr = self._int(usage.get("cacheRead"))
        cw = self._int(usage.get("cacheWrite"))
        total = self._int(usage.get("totalTokens"))
        out += max(0, total - (inp + out + cr + cw))  # only `totalTokens` -> back-fill output
        if inp + out + cr + cw == 0:
            return
        model = (
            msg.get("model")
            if isinstance(msg.get("model"), str) and msg.get("model")
            else "unknown"
        )
        acc = s["models"].get(model)
        if acc is None:
            acc = s["models"][model] = self._new_acc()
        acc["runs"] += 1
        acc["input"] += inp
        acc["output"] += out
        acc["cache_read"] += cr
        acc["cache_write"] += cw
        acc["tokens_total"] += inp + out + cr + cw
        cost = self._cost_total(usage)
        if cost > 0 and not self._is_subscription(msg.get("provider"), msg.get("api")):
            acc["cost"] += cost  # metered route with real spend -> tokens stay priced
        else:
            # Subscription/plan route (its cost is a list-price estimate, not spend) OR no
            # recorded cost at all -> mark these tokens unpriced so the "$" view estimates them.
            acc["u_input"] += inp
            acc["u_output"] += out
            acc["u_cache_read"] += cr
            acc["u_cache_write"] += cw

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title_prompt"] or "(untitled)"
        s["directory"] = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"
        stamp = s["ts_meta"] or s["ts_min"]
        s["created_at"] = iso_to_local(stamp) if stamp else ""
        rows: list[dict] = []
        for model_name, acc in s["models"].items():
            # Per-model priced/unpriced split (HermesStore pattern): metered messages
            # contribute real cost (and stay out of the unpriced split); subscription
            # messages contribute the unpriced tokens the "$" view estimates. The two
            # accumulate independently per message, so a model mixing both is split
            # correctly. No subagents: root == total.
            u_in = acc["u_input"]
            u_out = acc["u_output"]
            u_cr = acc["u_cache_read"]
            u_cw = acc["u_cache_write"]
            rows.append(
                {
                    "root_id": sid,
                    "model_name": model_name,
                    "runs": acc["runs"],
                    "cost": round(acc["cost"], 6),
                    "root_cost": round(acc["cost"], 6),
                    "tokens_total": acc["tokens_total"],
                    "input": acc["input"],
                    "reasoning": 0,
                    "cache_read": acc["cache_read"],
                    "cache_write": acc["cache_write"],
                    "output": acc["output"],
                    "unpriced_input": u_in,
                    "unpriced_reasoning": 0,
                    "unpriced_cache_read": u_cr,
                    "unpriced_cache_write": u_cw,
                    "unpriced_output": u_out,
                    "root_unpriced_input": u_in,
                    "root_unpriced_reasoning": 0,
                    "root_unpriced_cache_read": u_cr,
                    "root_unpriced_cache_write": u_cw,
                    "root_unpriced_output": u_out,
                }
            )
        s["model_rows"] = rows
        s["total_cost"] = round(sum(r["cost"] for r in rows), 6)
        s["total_tokens"] = sum(r["tokens_total"] for r in rows)
        # Only the subscription-route tokens are unpriced (a model can mix both routes).
        s["unpriced_tokens"] = sum(
            r["unpriced_input"]
            + r["unpriced_output"]
            + r["unpriced_cache_read"]
            + r["unpriced_cache_write"]
            for r in rows
        )

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

    # --- Store interface -----------------------------------------------------
    def workflows(self) -> list[Workflow]:
        self._sessions = None  # reload (r) re-reads fresh; model methods reuse cache
        sessions = self._parse()
        rows = []
        for sid, s in sessions.items():
            rows.append(
                Workflow(
                    id=sid,
                    title=s["title"],
                    directory=s["directory"],
                    created_at=s["created_at"],
                    root_cost=s["total_cost"],  # flat: root == total
                    total_cost=s["total_cost"],
                    subagents=0,  # pi has no subagent tree
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
        # Mirror CsvStore._demo_workflow: anonymize, backfill a synthetic price for any
        # unpriced tokens, then scale by the hidden per-process factor.
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
        root = self._new_acc()
        best, best_runs = "unknown (not recorded)", -1
        for model_name, acc in s["models"].items():
            for k in root:
                root[k] += acc[k]
            if acc["runs"] > best_runs:
                best_runs, best = acc["runs"], model_name
        # Single depth-0 node; cost is the recorded total. _priced_nodes reprices a $0
        # node from its token columns under "$".
        nodes = [
            self._node(
                workflow_id, 0, "-", s["title"], s["created_at"], best, s["total_cost"], root
            )
        ]
        if self.demo:
            nodes = [self._demo_node(n) for n in nodes]
        return nodes

    def _demo_node(self, n: dict) -> dict:
        n["title"] = demo_title(n["id"])
        n["model_name"] = demo_model(n["model_name"])
        if n["cost"] == 0:  # backfill a synthetic price for the unpriced tokens
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
