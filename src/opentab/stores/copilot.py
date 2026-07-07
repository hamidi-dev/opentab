"""GitHub Copilot CLI OpenTelemetry backend."""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sqlite3
from datetime import datetime, timezone

from opentab.demo import demo_cost, demo_dir, demo_model, demo_title
from opentab.models import Workflow
from opentab.util import git_root, read_files_parallel


class CopilotStore:
    """Read GitHub Copilot CLI usage from its OpenTelemetry file export
    (~/.copilot/otel/**/*.jsonl, plus the file named by $COPILOT_OTEL_FILE_EXPORTER_PATH)
    behind the same interface App expects from Store: workflows(), summary(),
    workflow_nodes(), model_breakdown(), plus the .demo/.demo_scale attributes -- like
    the other JSONL backends.

    The Copilot CLI records *no* token usage in its session transcripts or its
    session-store.db; the only place tokens land is the OTEL export, which is opt-in (set
    COPILOT_OTEL_FILE_EXPORTER_PATH before launching/resuming a Copilot session, or point
    --copilot-dir at the export). With export off there is simply nothing to read and the
    source never appears. The OTEL export is the only place these tokens are recorded.

    The OTEL export carries token usage but no dollar/credit cost. Since June 2026 Copilot
    bills usage-based -- tokens (input/output/cached) x the listed per-model API rates,
    converted to AI credits at 1 credit = 1 cent -- so that list-price figure is ~the real
    bill and is exactly what the "$" what-if computes. With no recorded cost to read, a
    Copilot session takes the same unpriced -> "$"-estimate path as the other token-only
    backends: recorded cost is $0, every token is "unpriced", the normal "$" machinery
    reprices it at API list rates, and records_cost = False drives the same header hints
    (see ClaudeStore).

    OTEL follows the GenAI semantic conventions, where one LLM call can be logged up to
    four times -- a `chat` span, a `gen_ai.client.inference...` log, a
    `copilot_chat.agent.turn` log, and an `invoke_agent` summary span -- so naively
    summing would multi-count. We keep the highest-fidelity record per call (chat span >
    inference log > agent-turn log > agent-summary span) and drop the rest by matching
    trace id / response id. Token accounting is
    OpenAI-style (gen_ai.usage.input_tokens *includes* the cached read, so input is split
    into uncached + cache_read; reasoning is folded into output and never priced twice).
    Models are mixed-provider (gpt-5.x, claude-sonnet, gemini), so each id is
    provider-prefixed for pricing and the Providers rollup. OTEL carries no working
    directory, so each session's cwd/title is enriched -- read-only, best effort -- from
    the sibling session-store.db. No subagent tree (every session is one depth-0 node);
    sessions with no recorded usage are dropped.
    """

    records_cost = False  # cost is $0 until "$" reprices the (all-unpriced) tokens
    combined = False
    source_name = "Copilot"

    # OTEL GenAI semantic-convention attribute names.
    _MODEL_ATTRS = ("gen_ai.response.model", "gen_ai.request.model")
    # (attribute, priority); the highest-priority present id wins, first listed breaks ties.
    _SESSION_ATTRS = (
        ("gen_ai.conversation.id", 3),
        ("copilot_chat.session_id", 3),
        ("copilot_chat.chat_session_id", 3),
        ("session.id", 3),
        ("github.copilot.interaction_id", 2),
        ("gen_ai.response.id", 1),
    )

    def __init__(self, root_dir: str, args: argparse.Namespace):
        self.root_dir = root_dir
        self.args = args
        self.demo = getattr(args, "demo", False)
        # Same hidden per-process factor Store/CodexStore use; 1.0 outside demo.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._sessions: dict[str, dict] | None = None  # parsed lazily / on reload
        self._git_root_cache: dict[str, str] = {}
        self._meta: dict[str, tuple[str, str]] | None = None  # id -> (cwd, summary)
        # GitHub's documented single-file exporter target (can live outside root_dir).
        self._extra_file = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH") or ""
        # session-store.db sits beside the otel/ directory -- the only place cwd lives.
        self._db_path = os.path.join(
            os.path.dirname(os.path.normpath(root_dir)), "session-store.db"
        )

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
        model = (model or "").strip()
        if not model:
            return "unknown"
        if "/" in model:
            return model  # already provider-qualified
        prov = cls._infer_provider(model)
        return f"{prov}/{model}" if prov else model

    def _git_root(self, cwd: str) -> str:
        if cwd not in self._git_root_cache:
            self._git_root_cache[cwd] = git_root(cwd)
        return self._git_root_cache[cwd]

    @staticmethod
    def _new_acc() -> dict:
        return {
            "runs": 0,
            "input": 0,  # uncached input (OTEL input_tokens minus the cached read)
            "output": 0,  # reasoning folded in so it is priced once, never twice
            "reasoning": 0,  # kept 0 (folded into output)
            "cache_read": 0,
            "cache_write": 0,
            "tokens_total": 0,
        }

    @staticmethod
    def _new_session() -> dict:
        return {"cwd": None, "ts_min": None, "models": {}}

    # --- OTEL attribute helpers ----------------------------------------------
    @staticmethod
    def _num(value) -> int:
        # OTEL numbers arrive as ints, floats or numeric strings; non-numeric/neg -> 0.
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return int(value) if value >= 0 else 0
        if isinstance(value, str):
            try:
                n = int(float(value.strip()))
            except ValueError:
                return 0
            return n if n >= 0 else 0
        return 0

    @classmethod
    def _attr_num(cls, attrs: dict, *keys: str) -> int:
        # First strictly-positive value among the aliases.
        for k in keys:
            n = cls._num(attrs.get(k))
            if n > 0:
                return n
        return 0

    @staticmethod
    def _attr_str(attrs: dict, *keys: str) -> str | None:
        for k in keys:
            v = attrs.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    @staticmethod
    def _nested_str(rec: dict, obj: str, key: str) -> str | None:
        o = rec.get(obj)
        if isinstance(o, dict) and isinstance(o.get(key), str) and o[key].strip():
            return o[key].strip()
        return None

    @classmethod
    def _trace_id(cls, rec: dict) -> str | None:
        v = rec.get("traceId")
        if isinstance(v, str) and v.strip():
            return v.strip()
        return cls._nested_str(rec, "spanContext", "traceId")

    @classmethod
    def _span_id(cls, rec: dict) -> str | None:
        v = rec.get("spanId")
        if isinstance(v, str) and v.strip():
            return v.strip()
        return cls._nested_str(rec, "spanContext", "spanId")

    @classmethod
    def _session_attr(cls, attrs: dict) -> tuple[str | None, int]:
        best, best_prio = None, -1
        for key, prio in cls._SESSION_ATTRS:
            v = cls._attr_str(attrs, key)
            if v is not None and prio > best_prio:
                best, best_prio = v, prio
        return best, best_prio

    @staticmethod
    def _is_span(rec: dict) -> bool:
        if isinstance(rec.get("type"), str):
            return rec["type"] == "span"
        # No explicit type: a span has a name plus span-ish fields.
        return isinstance(rec.get("name"), str) and any(
            k in rec for k in ("spanId", "traceId", "startTime", "endTime", "duration", "kind")
        )

    @staticmethod
    def _body(rec: dict) -> str:
        for k in ("body", "_body"):
            v = rec.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    @classmethod
    def _classify(cls, rec: dict, attrs: dict) -> str | None:
        # Which of the four redundant OTEL shapes this record is (or None to ignore it).
        op = cls._attr_str(attrs, "gen_ai.operation.name")
        name = rec.get("name") if isinstance(rec.get("name"), str) else ""
        if cls._is_span(rec):
            if op == "chat" or name.startswith("chat "):
                return "chat"
            if op == "invoke_agent" or name.startswith("invoke_agent "):
                return "agent_summary"
            return None
        event = cls._attr_str(attrs, "event.name")
        body = cls._body(rec)
        if event == "gen_ai.client.inference.operation.details" or body.startswith(
            "GenAI inference:"
        ):
            return "inference"
        if event == "copilot_chat.agent.turn" or body.startswith("copilot_chat.agent.turn"):
            return "agent_turn"
        return None

    @classmethod
    def _record_ts_ms(cls, rec: dict) -> int | None:
        # OTEL timestamps: [seconds, nanos] pairs, epoch scalars (s/ms/us/ns), or unix ns.
        for k in ("endTime", "startTime", "hrTime", "_hrTime", "time"):
            v = rec.get(k)
            if isinstance(v, list) and len(v) >= 2:
                secs, nanos = cls._num(v[0]), cls._num(v[1])
                if secs:
                    return secs * 1000 + nanos // 1_000_000
        for k in ("timestamp", "observedTimestamp"):
            raw = cls._num(rec.get(k))
            if raw:
                if raw >= 100_000_000_000_000_000:  # nanoseconds
                    return raw // 1_000_000
                if raw >= 100_000_000_000_000:  # microseconds
                    return raw // 1_000
                if raw >= 100_000_000_000:  # milliseconds
                    return raw
                return raw * 1000  # seconds
        nano = cls._num(rec.get("timeUnixNano"))
        return nano // 1_000_000 if nano else None

    @staticmethod
    def _file_mtime_ms(path: str) -> int | None:
        try:
            return int(os.path.getmtime(path) * 1000)
        except OSError:
            return None

    @classmethod
    def _dedup_key(cls, source, rec, attrs, tid, sid, ts_ms, idx) -> str:
        span = cls._span_id(rec)
        if source in ("chat", "agent_summary"):
            if tid and span:
                return f"{tid}:{span}"
            return f"span:{sid}:{ts_ms}:{idx}"
        if source == "inference":
            if tid and span:
                return f"log:{tid}:{span}"
            return f"log:{sid}:{ts_ms}:{idx}"
        turn = cls._attr_num(attrs, "turn.index", "copilot_chat.turn.index")
        turn_s = str(turn) if turn else f"idx-{idx}"
        return f"agent-turn:{tid}:{turn_s}" if tid else f"agent-turn:{sid}:{turn_s}:{idx}"

    @staticmethod
    def _ms_to_local(ms: int) -> str:
        try:
            return (
                datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except (OverflowError, OSError, ValueError):
            return ""

    # --- session-store.db enrichment (cwd/title; OTEL has neither) ------------
    def _load_meta(self) -> dict[str, tuple[str, str]]:
        if self._meta is not None:
            return self._meta
        meta: dict[str, tuple[str, str]] = {}
        if os.path.exists(self._db_path):
            try:
                con = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
                try:
                    for sid, cwd, summary in con.execute("SELECT id, cwd, summary FROM sessions"):
                        meta[sid] = (cwd or "", " ".join((summary or "").split())[:80])
                finally:
                    con.close()
            except sqlite3.Error:
                meta = {}  # foreign/locked/old schema -> just skip enrichment
        self._meta = meta
        return meta

    # --- parsing -------------------------------------------------------------
    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore).
        return self._files()

    def _files(self) -> list[str]:
        files = glob.glob(os.path.join(self.root_dir, "**", "*.jsonl"), recursive=True)
        # Add the env-var export file, but only if it isn't already one of the globbed
        # ones -- the fallback dedup keys are index-based, so reading the same file
        # twice would double-count records that lack trace/span ids.
        if self._extra_file and os.path.isfile(self._extra_file):
            seen = {os.path.realpath(f) for f in files}
            if os.path.realpath(self._extra_file) not in seen:
                files.append(self._extra_file)
        return files

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        # OTEL exporters write spans and logs to DIFFERENT files, so trace context, the
        # dedup coverage sets, and the seen keys must span all files: one call logged as
        # a chat span in file A and an inference log in file B is still one call. Two
        # passes -- collect every file's candidates first, then emit.
        per_file = [
            (path, self._file_records(text.split("\n")))
            for path, text in read_files_parallel(self._files())
        ]
        # Cross-file trace context: a model / session id seen anywhere on a trace fills
        # in for records on that trace that omit it.
        trace_ctx: dict[str, dict] = {}
        for _path, records in per_file:
            for rec in records:
                tid = self._trace_id(rec)
                if not tid:
                    continue
                attrs = rec["attributes"]
                ctx = trace_ctx.setdefault(tid, {"model": None, "session": None, "prio": -1})
                if ctx["model"] is None:
                    ctx["model"] = self._attr_str(attrs, *self._MODEL_ATTRS)
                sid, prio = self._session_attr(attrs)
                if sid is not None and prio > ctx["prio"]:
                    ctx["session"], ctx["prio"] = sid, prio
        candidates: list[dict] = []
        idx = 0  # global running index keeps the fallback dedup keys unique across files
        for path, records in per_file:
            for rec in records:
                c = self._to_candidate(rec, idx, trace_ctx, path)
                idx += 1
                if c is not None:
                    candidates.append(c)
        # Dedup: a chat span is the source of truth; an inference log is dropped when a
        # chat covers its trace/response, an agent-turn log when a chat or inference does,
        # an agent-summary span when any of the three do.
        levels = ("chat", "inference", "agent_turn")
        traces = {lvl: set() for lvl in levels}
        resps = {lvl: set() for lvl in levels}
        for c in candidates:
            if c["source"] in traces:
                if c["trace_id"]:
                    traces[c["source"]].add(c["trace_id"])
                if c["response_id"]:
                    resps[c["source"]].add(c["response_id"])
        seen: set[str] = set()
        for c in candidates:
            if not self._emit(c, traces, resps) or c["dedup_key"] in seen:
                continue
            seen.add(c["dedup_key"])
            self._fold(sessions, c)
        for sid, s in sessions.items():
            self._finalize(sid, s)
        # Drop sessions with no recorded usage (non-LLM-only files): they would only add
        # $0 / 0-token rows to a spend browser.
        self._sessions = {sid: s for sid, s in sessions.items() if s["model_rows"]}
        return self._sessions

    @classmethod
    def _file_records(cls, lines: list[str]) -> list[dict]:
        return [
            o
            for line in lines
            if '"attributes"' in line
            for o in (cls._loads(line),)
            if isinstance(o, dict) and isinstance(o.get("attributes"), dict)
        ]

    @staticmethod
    def _loads(line: str):
        try:
            return json.loads(line)
        except ValueError:
            return None

    @staticmethod
    def _emit(c: dict, traces: dict, resps: dict) -> bool:
        tid, rid, src = c["trace_id"], c["response_id"], c["source"]

        def covered(lvl: str) -> bool:
            return (tid is not None and tid in traces[lvl]) or (
                rid is not None and rid in resps[lvl]
            )

        if src == "chat":
            return True
        if src == "inference":
            return not covered("chat")
        if src == "agent_turn":
            return not covered("chat") and not covered("inference")
        return not covered("chat") and not covered("inference") and not covered("agent_turn")

    def _to_candidate(self, rec: dict, idx: int, trace_ctx: dict, path: str) -> dict | None:
        attrs = rec["attributes"]
        source = self._classify(rec, attrs)
        if source is None:
            return None
        inp = self._attr_num(attrs, "gen_ai.usage.input_tokens")
        out = self._attr_num(attrs, "gen_ai.usage.output_tokens")
        cache_read = self._attr_num(attrs, "gen_ai.usage.cache_read.input_tokens")
        cache_write = self._attr_num(
            attrs,
            "gen_ai.usage.cache_write.input_tokens",
            "gen_ai.usage.cache_creation.input_tokens",
        )
        reasoning = self._attr_num(
            attrs, "gen_ai.usage.reasoning.output_tokens", "gen_ai.usage.reasoning_tokens"
        )
        total = self._attr_num(attrs, "gen_ai.usage.total_tokens", "gen_ai.usage.total.token_count")
        uncached = max(0, inp - min(inp, cache_read))  # input_tokens includes the cached read
        # Some exporters log only a grand total; back-fill the gap (as output, else reasoning).
        missing = max(0, total - (uncached + out + cache_write + cache_read + reasoning))
        if missing:
            if out == 0:
                out = missing
            else:
                reasoning += missing
        if uncached + out + cache_write + cache_read + reasoning == 0:
            return None  # a non-usage record (metric, tool span, ...)
        tid = self._trace_id(rec)
        ctx = trace_ctx.get(tid) if tid else None
        model = self._attr_str(attrs, *self._MODEL_ATTRS) or (ctx["model"] if ctx else None)
        sid, _ = self._session_attr(attrs)
        if sid is None:
            sid = (ctx["session"] if ctx else None) or tid or "unknown-session"
        ts_ms = self._record_ts_ms(rec)
        if ts_ms is None:
            ts_ms = self._file_mtime_ms(path)
        return {
            "source": source,
            "trace_id": tid,
            "response_id": self._attr_str(attrs, "gen_ai.response.id"),
            "model": self._prefix_model(model or "unknown"),
            "session_id": sid,
            "ts_ms": ts_ms,
            "input": uncached,
            "output": out + reasoning,  # fold reasoning into output (priced once at output)
            "cache_read": cache_read,
            "cache_write": cache_write,
            "dedup_key": self._dedup_key(source, rec, attrs, tid, sid, ts_ms, idx),
        }

    def _fold(self, sessions: dict[str, dict], c: dict) -> None:
        s = sessions.setdefault(c["session_id"], self._new_session())
        ts = c["ts_ms"]
        if ts is not None and (s["ts_min"] is None or ts < s["ts_min"]):
            s["ts_min"] = ts
        acc = s["models"].get(c["model"])
        if acc is None:
            acc = s["models"][c["model"]] = self._new_acc()
        acc["runs"] += 1
        acc["input"] += c["input"]
        acc["cache_read"] += c["cache_read"]
        acc["cache_write"] += c["cache_write"]
        acc["output"] += c["output"]
        acc["tokens_total"] += c["input"] + c["cache_read"] + c["cache_write"] + c["output"]

    def _finalize(self, sid: str, s: dict) -> None:
        cwd, summary = self._load_meta().get(sid, ("", ""))
        s["title"] = summary or "(untitled)"
        s["directory"] = self._git_root(cwd) if cwd else "(unknown)"
        s["created_at"] = self._ms_to_local(s["ts_min"]) if s["ts_min"] is not None else ""
        rows: list[dict] = []
        for model_name, acc in s["models"].items():
            # Recorded cost is $0 (OTEL logs none); every token is "unpriced", so the
            # unpriced_* splits carry the full counts. No subagents, so root == total.
            rows.append(
                {
                    "root_id": sid,
                    "model_name": model_name,
                    "runs": acc["runs"],
                    "cost": 0.0,
                    "root_cost": 0.0,
                    "tokens_total": acc["tokens_total"],
                    "input": acc["input"],
                    "reasoning": acc["reasoning"],
                    "cache_read": acc["cache_read"],
                    "cache_write": acc["cache_write"],
                    "output": acc["output"],
                    "unpriced_input": acc["input"],
                    "unpriced_reasoning": acc["reasoning"],
                    "unpriced_cache_read": acc["cache_read"],
                    "unpriced_cache_write": acc["cache_write"],
                    "unpriced_output": acc["output"],
                    "root_unpriced_input": acc["input"],
                    "root_unpriced_reasoning": acc["reasoning"],
                    "root_unpriced_cache_read": acc["cache_read"],
                    "root_unpriced_cache_write": acc["cache_write"],
                    "root_unpriced_output": acc["output"],
                }
            )
        s["model_rows"] = rows
        s["total_tokens"] = sum(r["tokens_total"] for r in rows)
        s["unpriced_tokens"] = s["total_tokens"]  # all of it

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
        self._meta = None
        sessions = self._parse()
        rows = []
        for sid, s in sessions.items():
            model_rows = s["model_rows"]
            rows.append(
                Workflow(
                    id=sid,
                    title=s["title"],
                    directory=s["directory"],
                    created_at=s["created_at"],
                    root_cost=0.0,  # recorded cost is $0; "$" reprices the tokens
                    total_cost=0.0,
                    subagents=0,  # Copilot CLI has no subagent tree
                    model_count=0,  # filled by App._load_model_cache
                    total_tokens=sum(r["tokens_total"] for r in model_rows),
                    unpriced_tokens=s["unpriced_tokens"],
                    source=self.source_name,
                )
            )
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return rows

    def _demo_workflow(self, w: Workflow) -> Workflow:
        # Mirror CodexStore._demo_workflow: anonymize, backfill a synthetic price for the
        # (all-unpriced) tokens, then scale by the hidden per-process factor.
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
        # cost 0 (recorded); _priced_nodes reprices from the token columns under "$".
        nodes = [self._node(workflow_id, 0, "-", s["title"], s["created_at"], best, 0.0, root)]
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
