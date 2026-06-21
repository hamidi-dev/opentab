"""Codex CLI rollout backend."""
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
from opentab.util import git_root


class CodexStore:
    """Read Codex CLI rollout transcripts (~/.codex/sessions/**/rollout-*.jsonl) behind
    the same interface App expects from Store: workflows(), summary(), workflow_nodes(),
    model_breakdown(), plus the .demo/.demo_scale attributes -- exactly like ClaudeStore.

    Like Claude Code, Codex records token usage but no per-message dollar cost, so a
    Codex session is an OpenCode *subscription* session: recorded cost is $0, every
    token is "unpriced", and the normal "$" what-if machinery reprices it at API list
    rates. records_cost = False drives the same header hints (see ClaudeStore).

    Token accounting differs from Anthropic's in two ways and both are handled here:
      * each turn logs a *cumulative* total_token_usage (running session total) in a
        token_count event, and Codex emits each turn's token_count twice (once as the
        turn result, once echoed right after the next turn_context). So we drive
        per-turn deltas off the monotonic cumulative total: a strictly larger total is
        a new turn (delta = total - prev), an equal total is the duplicate echo (skip),
        and a smaller total is a context-compaction reset (the new total is fresh
        usage). The accepted deltas sum back to the final authoritative total, and each
        is attributed to the model active at that turn (the latest turn_context.model).
      * OpenAI's input_tokens *includes* the cached_input_tokens and there is no
        cache-write concept, so we split input into uncached (priced at the input rate)
        + cache_read (the cached discount rate); cache_write stays 0. reasoning_output
        is already counted inside output_tokens, so -- as in ClaudeStore -- it is folded
        into output and never priced twice.

    Codex has no Task-subagent tree, so every session is a single depth-0 node (root
    == total). Sessions with no recorded token usage (legacy/aborted) are dropped.
    """

    records_cost = False  # cost is $0 until "$" reprices the (all-unpriced) tokens
    combined = False
    source_name = "Codex"

    def __init__(self, root_dir: str, args: argparse.Namespace):
        self.root_dir = root_dir
        self.args = args
        self.demo = getattr(args, "demo", False)
        # Same hidden per-process factor Store/ClaudeStore use; 1.0 outside demo.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._sessions: dict[str, dict] | None = None  # parsed lazily / on reload
        self._git_root_cache: dict[str, str] = {}

    # --- token accumulation helpers (mirror ClaudeStore) ---------------------
    @staticmethod
    def _new_acc() -> dict[str, int]:
        return {
            "runs": 0,
            "input": 0,  # uncached input (OpenAI's input_tokens minus cached)
            "output": 0,
            "reasoning": 0,  # folded into output; kept 0 so it is never priced twice
            "cache_read": 0,
            "cache_write": 0,  # OpenAI has no separate cache-write bill
            "tokens_total": 0,
        }

    def _git_root(self, cwd: str) -> str:
        if cwd not in self._git_root_cache:
            self._git_root_cache[cwd] = git_root(cwd)
        return self._git_root_cache[cwd]

    @staticmethod
    def _id_from_name(path: str) -> str | None:
        # rollout-<timestamp>-<uuid>.jsonl -- the uuid is the session id (verified to
        # match session_meta.id), so the filename is a reliable key even for the rare
        # legacy file whose first record is an unwrapped session-meta blob.
        m = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            os.path.basename(path),
        )
        return m.group(1) if m else None

    @staticmethod
    def _new_session() -> dict:
        return {
            "cwd": None,
            "ts_min": None,
            "ts_meta": None,  # session_meta.timestamp, preferred for created_at
            "title_prompt": None,
            "models": {},  # model_name -> acc (no root/sub split: Codex is flat)
        }

    # --- parsing -------------------------------------------------------------
    def _files(self) -> list[str]:
        return glob.glob(os.path.join(self.root_dir, "**", "*.jsonl"), recursive=True)

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        for path in self._files():
            self._parse_file(path, sessions)
        for sid, s in sessions.items():
            self._finalize(sid, s)
        # Drop sessions with no recorded token usage (legacy rollouts, aborted runs):
        # they would only add a pile of $0 / 0-token rows to a spend browser.
        self._sessions = {sid: s for sid, s in sessions.items() if s["model_rows"]}
        return self._sessions

    def _parse_file(self, path: str, sessions: dict[str, dict]) -> None:
        try:
            fh = open(path, errors="replace")
        except OSError:
            return
        sid = self._id_from_name(path)
        s = sessions.setdefault(sid, self._new_session()) if sid else None
        cur_model: str | None = None
        prev = (0, 0, 0, 0)  # cumulative (input, output, cached, total) seen so far
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                typ = o.get("type")
                p = o.get("payload") if isinstance(o.get("payload"), dict) else None
                # session metadata: wrapped (type session_meta) or a rare legacy file
                # whose first record is the bare {id, timestamp, git, ...} blob.
                meta = p if typ == "session_meta" else (o if typ is None and "git" in o else None)
                if meta is not None:
                    if s is None and meta.get("id"):
                        sid = meta["id"]
                        s = sessions.setdefault(sid, self._new_session())
                    if s is not None:
                        if meta.get("cwd") and not s["cwd"]:
                            s["cwd"] = meta["cwd"]
                        if meta.get("timestamp") and not s["ts_meta"]:
                            s["ts_meta"] = meta["timestamp"]
                if s is None:
                    continue  # nothing to attribute usage to yet
                ts = o.get("timestamp")
                if ts and (s["ts_min"] is None or ts < s["ts_min"]):
                    s["ts_min"] = ts
                if p is None:
                    continue
                if typ == "turn_context":
                    if p.get("model"):
                        cur_model = p["model"]
                    if p.get("cwd") and not s["cwd"]:
                        s["cwd"] = p["cwd"]
                elif typ == "event_msg" and p.get("type") == "user_message":
                    # First user prompt = session title. Take any user_message (older
                    # rollouts omit kind; only "plain" appears on newer ones), and
                    # collapse whitespace since Codex prompts often span lines with
                    # @file mentions that would otherwise break the one-line title cell.
                    if not s["title_prompt"]:
                        txt = p.get("message")
                        if isinstance(txt, str) and txt.strip():
                            s["title_prompt"] = " ".join(txt.split())[:80]
                elif typ == "event_msg" and p.get("type") == "token_count":
                    prev = self._apply_token_count(s, p.get("info"), cur_model, prev)

    def _apply_token_count(self, s: dict, info, cur_model: str | None, prev: tuple) -> tuple:
        # Returns the new cumulative tuple; folds one turn's delta into the session.
        if not isinstance(info, dict):
            return prev
        tt = info.get("total_token_usage")
        if not isinstance(tt, dict):
            return prev
        cur = (
            int(tt.get("input_tokens", 0) or 0),
            int(tt.get("output_tokens", 0) or 0),
            int(tt.get("cached_input_tokens", 0) or 0),
            int(tt.get("total_tokens", 0) or 0),
        )
        if cur[3] > prev[3]:  # new turn: usage is the growth in the running total
            d_in, d_out, d_cached = cur[0] - prev[0], cur[1] - prev[1], cur[2] - prev[2]
        elif cur[3] < prev[3]:  # context compaction reset: the new total is fresh usage
            d_in, d_out, d_cached = cur[0], cur[1], cur[2]
        else:  # equal total == the duplicate echo Codex writes after each turn_context
            return prev
        model = cur_model or "unknown"
        # Bare Codex models ("gpt-5-codex"); prefix the provider so model_price strips
        # it the same way and the Providers tab rolls up under "openai".
        model_name = model if "/" in model else "openai/" + model
        acc = s["models"].get(model_name)
        if acc is None:
            acc = s["models"][model_name] = self._new_acc()
        acc["runs"] += 1
        acc["input"] += max(0, d_in - d_cached)  # input_tokens includes the cached read
        acc["cache_read"] += max(0, d_cached)
        acc["output"] += max(0, d_out)
        acc["tokens_total"] += max(0, d_in) + max(0, d_out)
        return cur

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title_prompt"] or "(untitled)"
        s["directory"] = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"
        s["created_at"] = iso_to_local(s["ts_meta"] or s["ts_min"])
        rows: list[dict] = []
        for model_name, acc in s["models"].items():
            # Recorded cost is $0 (Codex logs none); every token is "unpriced", so the
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
        s["unpriced_tokens"] = sum(r["tokens_total"] for r in rows)  # all of it

    @staticmethod
    def _node(
        node_id: str,
        depth: int,
        agent: str,
        title: str,
        created_at: str,
        model_name: str,
        cost: float,
        acc: dict[str, int],
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
            model_rows = s["model_rows"]
            rows.append(
                Workflow(
                    id=sid,
                    title=s["title"],
                    directory=s["directory"],
                    created_at=s["created_at"],
                    root_cost=0.0,  # recorded cost is $0; "$" reprices the tokens
                    total_cost=0.0,
                    subagents=0,  # Codex has no subagent tree
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
        # Mirror ClaudeStore._demo_workflow: anonymize, backfill a synthetic price for
        # the (all-unpriced) tokens, then scale by the hidden per-process factor.
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
