"""OpenClaw gateway JSONL backend."""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
from datetime import datetime, timezone

from opentab.demo import demo_cost, demo_dir, demo_model, demo_title
from opentab.models import Workflow
from opentab.util import read_files_parallel


class OpenClawStore:
    """Read OpenClaw gateway sessions (~/.openclaw/agents/<agent>/sessions/*.jsonl, or the
    root named by $OPENCLAW_DIR / --openclaw-dir) behind the same four methods App expects
    from Store, plus the .demo/.demo_scale attributes -- like the other JSONL backends.

    OpenClaw is a self-hosted, multi-provider agent gateway (openai-codex, github-copilot,
    anthropic, ollama, ...). Like pi-agent it writes a per-message `usage.cost.total`, but
    that figure is a *list-price* estimate for **every** provider, including
    subscription/OAuth routes (a ChatGPT-plan openai-codex turn, a Copilot turn) whose
    marginal cost is actually $0. So the recorded cost is real spend only on **metered**
    routes (a direct Anthropic API key, OpenRouter); a subscription route's cost is a
    what-if estimate, not spend. Mirroring `PiStore`/`HermesStore`'s billing split, a message
    is classed **subscription** when its provider's auth profile is an OAuth login
    (`openclaw.json` -> auth.profiles[*].mode == "oauth", read read-only) or matches a known
    plan marker (`_SUBSCRIPTION_MARKERS`: codex/copilot/ollama/...) -- those tokens are left
    **unpriced** (the "$" view estimates them) while metered messages with a real cost price
    as spend. The two accumulate independently per message, so a session (even one model)
    mixing both routes is split correctly. Cost is therefore mixed, so -- exactly like
    `CsvStore`/`HermesStore`/`PiStore` -- **`records_cost` is a per-instance attr** (True iff
    any *metered* message has a cost), set by a cheap early-exit probe in `__init__` so
    `CombinedStore` can read it before `workflows()`.

    Parsing: each session file is newline-delimited JSON,
    and only `type:"message"` records with `message.role == "assistant"` and a `message.usage`
    object carry usage; `type:"model_change"` (and `type:"custom"` + customType
    "model-snapshot") records set the current model/provider for following messages when a
    message omits its own. OpenClaw also writes a parallel *trace* schema
    (session.started/model.completed/...) in **separate** files; those hold no `type:"message"`
    record, so reading only assistant messages never double-counts.
    Token accounting is **Anthropic-style**: `input` is already the *uncached* prompt
    (cacheRead/cacheWrite are tracked separately, never folded in); total =
    input + output + cacheRead + cacheWrite (a record carrying only `totalTokens` back-fills
    the gap as output). Models are recorded bare (gpt-5.3-codex, claude-opus-4-6), so they're
    provider-prefixed by inferred family (the `CsvStore` pattern) for pricing and the
    Providers rollup. The **project is the agent** (finance-os, homelab, ...) -- the directory
    under agents/ -- which is far more useful than OpenClaw's generic gateway cwd. Assistant
    messages are deduped by their stable record `id` across a session's live + archived
    (.jsonl.reset./.jsonl.deleted.) files. No subagent tree (every session is one depth-0
    node); sessions with no recorded usage are dropped.
    """

    combined = False
    source_name = "OpenClaw"

    # Provider/api substrings that mark a subscription (plan-included) route even when
    # openclaw.json is unavailable -- their recorded cost is a list-price estimate, not spend.
    # github-copilot authenticates with a static token (not "oauth") yet is a Copilot plan, so
    # it is caught here rather than by the oauth probe; ollama is local/free; "openclaw" tags
    # the gateway's own internal turns (delivery-mirror, gateway-injected).
    _SUBSCRIPTION_MARKERS = (
        "codex",
        "copilot",
        "chatgpt",
        "claude-code",
        "claude-max",
        "claude-pro",
        "ollama",
        "openclaw",
        "gateway",
    )

    def __init__(self, root_dir: str, args: argparse.Namespace):
        self.root_dir = root_dir
        self.args = args
        self.demo = getattr(args, "demo", False)
        # Same hidden per-process factor Store/CodexStore use; 1.0 outside demo.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._sessions: dict[str, dict] | None = None  # parsed lazily / on reload
        # openclaw.json (at the root, beside agents/) records, per auth profile, whether a
        # provider logs in via "oauth" (a consumer plan, subscription) or a static "token"
        # (a metered API key). Read-only; we read only the mode + provider name.
        self._oauth_providers = self._load_oauth_providers()
        self.records_cost = self._probe_records_cost()

    # --- mixed-provider model ids (mirrors CsvStore) -------------------------
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
    def _prefix_model(cls, model: str) -> str:
        # Mixed-provider ids: prefix the inferred provider so model_price() strips it the same
        # way and the Providers tab rolls up under anthropic/openai/google.
        model = (model or "").strip()
        if not model:
            return "unknown"
        if "/" in model:
            return model  # already provider-qualified
        prov = cls._infer_provider(model)
        return f"{prov}/{model}" if prov else model

    # --- billing classification ----------------------------------------------
    def _load_oauth_providers(self) -> set[str]:
        path = os.path.join(self.root_dir, "openclaw.json")
        out: set[str] = set()
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return out
        profiles = data.get("auth", {}).get("profiles", {}) if isinstance(data, dict) else {}
        if isinstance(profiles, dict):
            for info in profiles.values():
                if isinstance(info, dict) and str(info.get("mode", "")).lower() == "oauth":
                    prov = info.get("provider")
                    if isinstance(prov, str) and prov:
                        out.add(prov.lower())
        return out

    def _is_subscription(self, provider, api) -> bool:
        prov = (provider or "").lower()
        if prov and prov in self._oauth_providers:
            return True
        text = prov + " " + (api or "").lower()
        return any(marker in text for marker in self._SUBSCRIPTION_MARKERS)

    # --- small helpers -------------------------------------------------------
    @staticmethod
    def _is_session_file(name: str) -> bool:
        # Live "<id>.jsonl" plus OpenClaw's archived snapshots ("<id>.jsonl.reset.<ts>",
        # "<id>.jsonl.deleted.<ts>"); skip locks.
        i = name.find(".jsonl")
        if i < 0:
            return False
        suffix = name[i:]
        return (
            suffix == ".jsonl"
            or suffix.startswith(".jsonl.reset.")
            or suffix.startswith(".jsonl.deleted.")
        )

    @staticmethod
    def _session_id(path: str) -> str:
        # The id is the filename stem before the first ".jsonl", so a session's live file and
        # its .reset./.deleted. archives all key to one session and merge (deduped by msg id).
        name = os.path.basename(path)
        i = name.find(".jsonl")
        return (name[:i] or name) if i > 0 else name

    @staticmethod
    def _is_model_change(o: dict) -> bool:
        t = o.get("type")
        return t == "model_change" or (t == "custom" and o.get("customType") == "model-snapshot")

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
        # A user message's content is a bare string or a list of {type, text} parts.
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
    def _epoch(value) -> float | None:
        # OpenClaw timestamps come as epoch milliseconds (ints) or ISO-8601 strings; normalize
        # both to epoch seconds so a session's earliest record sorts uniformly.
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            v = float(value)
            if v <= 0:
                return None
            if v > 1e14:  # microseconds
                v /= 1e6
            elif v > 1e11:  # milliseconds
                v /= 1e3
            return v
        if isinstance(value, str) and value.strip():
            try:
                dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                try:
                    dt = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        return None

    @staticmethod
    def _fmt_epoch(sec) -> str:
        if sec is None:
            return ""
        try:
            return (
                datetime.fromtimestamp(sec, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except (OverflowError, OSError, ValueError):
            return ""

    @staticmethod
    def _new_acc() -> dict:
        return {
            "runs": 0,
            "input": 0,  # already uncached (Anthropic-style; cacheRead is separate)
            "output": 0,
            "reasoning": 0,  # OpenClaw folds reasoning into output; kept for the row schema
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
            "agent": None,  # the directory under agents/ -> the project
            "ts_min": None,  # earliest record (epoch seconds)
            "ts_meta": None,  # the `session` record's timestamp, preferred for created_at
            "title_prompt": None,
            "models": {},
            "seen_msgs": set(),  # record ids already counted (resume/archive dedup)
        }

    # --- parsing -------------------------------------------------------------
    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore).
        return self._files()

    def _files(self) -> list[str]:
        out = []
        for path in glob.glob(os.path.join(self.root_dir, "agents", "*", "sessions", "*")):
            if os.path.isfile(path) and self._is_session_file(os.path.basename(path)):
                out.append(path)
        return out

    def _probe_records_cost(self) -> bool:
        # True iff any *metered* (non-subscription) assistant message records a positive
        # cost. Early-exits so it stays cheap (safe in __init__; CombinedStore reads it
        # before workflows()). A subscription-only setup -> False (every cost is estimated).
        for path in self._files():
            try:
                fh = open(path, errors="replace")
            except OSError:
                continue
            current_provider = None
            with fh:
                for line in fh:
                    if '"provider"' in line and self._is_model_change_line(line):
                        try:
                            o = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(o, dict) and self._is_model_change(o):
                            src = o.get("data") if isinstance(o.get("data"), dict) else o
                            p = src.get("provider")
                            if isinstance(p, str) and p:
                                current_provider = p
                        continue
                    if '"cost"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    msg = o.get("message") if o.get("type") == "message" else None
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    if self._cost_total(usage) > 0 and not self._is_subscription(
                        msg.get("provider") or current_provider, msg.get("api")
                    ):
                        return True
        return False

    @staticmethod
    def _is_model_change_line(line: str) -> bool:
        return '"model_change"' in line or '"model-snapshot"' in line

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
        sid = self._session_id(path)
        agent = os.path.basename(os.path.dirname(os.path.dirname(path)))
        s = sessions.setdefault(sid, self._new_session())
        if not s["agent"]:
            s["agent"] = agent
        current_model = None
        current_provider = None
        for line in lines:
            if '"type"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            ts = self._epoch(o.get("timestamp"))
            if ts is not None and (s["ts_min"] is None or ts < s["ts_min"]):
                s["ts_min"] = ts
            if self._is_model_change(o):
                src = o.get("data") if isinstance(o.get("data"), dict) else o
                m = src.get("modelId") or src.get("model")
                if isinstance(m, str) and m:
                    current_model = m
                p = src.get("provider")
                if isinstance(p, str) and p:
                    current_provider = p
                continue
            typ = o.get("type")
            if typ == "session":
                if ts is not None and s["ts_meta"] is None:
                    s["ts_meta"] = ts
                continue
            if typ != "message":
                continue
            msg = o.get("message")
            if not isinstance(msg, dict):
                continue
            mts = self._epoch(msg.get("timestamp"))
            if mts is not None and (s["ts_min"] is None or mts < s["ts_min"]):
                s["ts_min"] = mts
            role = msg.get("role")
            if role == "user":
                if not s["title_prompt"]:
                    txt = self._user_text(msg.get("content"))
                    if txt.strip():
                        s["title_prompt"] = " ".join(txt.split())[:80]
                continue
            if role != "assistant" or not isinstance(msg.get("usage"), dict):
                continue
            mid = o.get("id") or msg.get("idempotencyKey")
            if mid is not None:
                if mid in s["seen_msgs"]:
                    continue  # same assistant step in a resumed/archived file
                s["seen_msgs"].add(mid)
            self._apply_usage(s, msg, current_model, current_provider)

    def _apply_usage(self, s: dict, msg: dict, current_model, current_provider) -> None:
        usage = msg["usage"]
        inp = self._int(usage.get("input"))
        out = self._int(usage.get("output"))
        cr = self._int(usage.get("cacheRead"))
        cw = self._int(usage.get("cacheWrite"))
        total = self._int(usage.get("totalTokens"))
        out += max(0, total - (inp + out + cr + cw))  # only `totalTokens` -> back-fill output
        if inp + out + cr + cw == 0:
            return
        raw = msg.get("model") or msg.get("modelId") or current_model or "unknown"
        if not isinstance(raw, str) or not raw:
            raw = "unknown"
        model = self._prefix_model(raw)
        provider = msg.get("provider") or current_provider
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
        if cost > 0 and not self._is_subscription(provider, msg.get("api")):
            acc["cost"] += cost  # metered route with real spend -> tokens stay priced
        else:
            # Subscription/plan route (cost is a list-price estimate, not spend) OR no recorded
            # cost -> mark these tokens unpriced so the "$" view estimates them.
            acc["u_input"] += inp
            acc["u_output"] += out
            acc["u_cache_read"] += cr
            acc["u_cache_write"] += cw

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title_prompt"] or "(untitled)"
        s["directory"] = s["agent"] or "(unknown)"
        s["created_at"] = self._fmt_epoch(s["ts_meta"] or s["ts_min"])
        rows: list[dict] = []
        for model_name, acc in s["models"].items():
            # Per-model priced/unpriced split (HermesStore pattern): metered messages
            # contribute real cost (and stay out of the unpriced split); subscription messages
            # contribute the unpriced tokens the "$" view estimates. No subagents: root == total.
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
                    subagents=0,  # OpenClaw has no subagent tree
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
        # Mirror PiStore._demo_workflow: anonymize, backfill a synthetic price for any
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
