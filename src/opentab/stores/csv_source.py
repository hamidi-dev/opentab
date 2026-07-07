"""Logged-API-request CSV backend."""
from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import datetime, timezone

from opentab.demo import demo_cost, demo_dir, demo_model, demo_title
from opentab.formatting import iso_to_local
from opentab.models import Workflow
from opentab.util import git_root


class CsvStore:
    """Read a CSV of logged API requests (e.g. GitHub Copilot inside IntelliJ) behind
    the same interface App expects from Store: workflows(), summary(), workflow_nodes(),
    model_breakdown(), plus the .demo/.demo_scale attributes -- like the JSONL backends.

    CSV is already tabular, so this is the simplest backend: one row per API request
    (pre-aggregated rows work too), rolled up by session. Column names are matched
    case-insensitively with aliases, so the exporter has latitude. Required: a timestamp,
    a model, and input/output token counts. Everything else is optional:

        timestamp  time|date|created_at|ts        ISO-8601 or epoch (s/ms/us)
        model      model_id|model_name            e.g. claude-sonnet-4, gpt-4o, gemini-2.5-pro
        input      input_tokens|prompt_tokens     as logged (may include the cached read)
        output     output_tokens|completion_tokens includes reasoning (priced once at output)
        cached     cached_tokens|cache_read        cached portion of input (default 0)
        session    session_id|conversation_id      groups requests into one session
        project    repo|workspace|directory|cwd    path -> git root; bare name as-is
        title      name|label|prompt               session label (default first prompt)
        cost       cost_usd|credits                credits x $0.01; presence -> metered

    A logged API request carries no dollar cost (Copilot's usage-based credit billing is
    settled server-side), so a Copilot row is treated like an OpenCode *subscription* row:
    recorded cost $0, every token unpriced, and the normal "$" machinery reprices it at API
    list rates.
    But cost is handled per-row like HermesStore: if the CSV carries a cost_usd/credits
    column with positive values those rows price as real spend, so records_cost is a
    per-instance attr (True iff any row has a recorded cost), probed cheaply in __init__.

    Models are mixed-provider, so each id is provider-prefixed (claude->anthropic/,
    gpt|o3->openai/, gemini->google/) for pricing and the Providers rollup. OpenAI-style
    accounting: input_tokens includes the cached read, so input is split into uncached +
    cache_read (cache_write stays 0). No subagent tree -- every session is one depth-0
    node. Sessions with no recorded token usage are dropped.
    """

    combined = False
    source_name = "CSV"

    # Accepted header (normalized: lowercased, spaces -> underscores) -> canonical field.
    _FIELD_ALIASES = {
        "timestamp": "timestamp",
        "time": "timestamp",
        "date": "timestamp",
        "created_at": "timestamp",
        "datetime": "timestamp",
        "ts": "timestamp",
        "model": "model",
        "model_id": "model",
        "model_name": "model",
        "input_tokens": "input",
        "prompt_tokens": "input",
        "input": "input",
        "output_tokens": "output",
        "completion_tokens": "output",
        "output": "output",
        "cached_tokens": "cached",
        "cache_read": "cached",
        "cache_read_tokens": "cached",
        "cached": "cached",
        "session_id": "session",
        "conversation_id": "session",
        "session": "session",
        "conversation": "session",
        "project": "project",
        "repo": "project",
        "repository": "project",
        "workspace": "project",
        "directory": "project",
        "dir": "project",
        "cwd": "project",
        "folder": "project",
        "path": "project",
        "title": "title",
        "name": "title",
        "label": "title",
        "prompt": "title",
        "cost_usd": "cost",
        "cost": "cost",
        "credits": "cost",
        "credit": "cost",
    }

    def __init__(self, csv_path: str, args: argparse.Namespace):
        self.csv_path = csv_path
        self.args = args
        self.demo = getattr(args, "demo", False)
        # Same hidden per-process factor Store/CodexStore use; 1.0 outside demo.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._sessions: dict[str, dict] | None = None  # parsed lazily / on reload
        self._git_root_cache: dict[str, str] = {}
        self.records_cost = self._probe_records_cost()

    # --- header / value parsing ---------------------------------------------
    @classmethod
    def _resolve_headers(cls, fieldnames) -> tuple[dict[str, str], bool]:
        # Map canonical field -> the actual CSV header for it (first alias wins), plus a
        # flag for whether the matched cost column is a credits column (-> x $0.01).
        mapping: dict[str, str] = {}
        cost_is_credits = False
        for actual in fieldnames or []:
            norm = (actual or "").strip().lower().replace(" ", "_")
            canon = cls._FIELD_ALIASES.get(norm)
            if canon and canon not in mapping:
                mapping[canon] = actual
                if canon == "cost" and norm in ("credits", "credit"):
                    cost_is_credits = True
        return mapping, cost_is_credits

    @staticmethod
    def _to_int(raw) -> int:
        if raw is None:
            return 0
        s = str(raw).strip().replace(",", "")
        if not s:
            return 0
        try:
            return max(0, int(float(s)))
        except ValueError:
            return 0

    @staticmethod
    def _to_float(raw) -> float:
        if raw is None:
            return 0.0
        s = str(raw).strip().replace(",", "").replace("$", "")
        if not s:
            return 0.0
        try:
            return max(0.0, float(s))
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_ts(raw) -> str:
        # Canonical local "YYYY-MM-DD HH:MM:SS" (matches Store/the JSONL backends). ISO
        # goes through iso_to_local; a bare number is an epoch (seconds/ms/us by scale).
        raw = (raw or "").strip()
        if not raw:
            return ""
        try:
            val = float(raw)
        except ValueError:
            return iso_to_local(raw)
        if val > 1e14:  # microseconds
            val /= 1e6
        elif val > 1e11:  # milliseconds
            val /= 1e3
        try:
            return (
                datetime.fromtimestamp(val, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except (OverflowError, OSError, ValueError):
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
    def _prefix_model(cls, model: str) -> str:
        # Mixed-provider ids: prefix the inferred provider so model_price() strips it the
        # same way and the Providers tab rolls up under anthropic/openai/google.
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

    def _resolve_dir(self, project: str) -> str:
        # A path folds to its git root (subdir launches roll up to the repo); a bare repo
        # name is used verbatim. Empty -> "(unknown)".
        project = (project or "").strip()
        if not project:
            return "(unknown)"
        if os.sep in project or project.startswith(("~", ".")):
            return self._git_root(project)
        return project

    def _probe_records_cost(self) -> bool:
        # True iff the CSV has a cost column with any positive value. Cheap pass so it is
        # safe in __init__ (CombinedStore reads records_cost before workflows()).
        try:
            with open(self.csv_path, newline="", encoding="utf-8", errors="replace") as fh:
                reader = csv.DictReader(fh)
                mapping, is_credits = self._resolve_headers(reader.fieldnames)
                col = mapping.get("cost")
                if not col:
                    return False
                for row in reader:
                    if self._to_float(row.get(col)) > 0:
                        return True
        except OSError:
            return False
        return False

    # --- accumulation --------------------------------------------------------
    @staticmethod
    def _new_acc() -> dict:
        return {
            "runs": 0,
            "input": 0,  # uncached input (the logged input minus the cached read)
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,  # OpenAI-style: no separate cache-write bill
            "tokens_total": 0,
            "cost": 0.0,
        }

    @staticmethod
    def _new_session() -> dict:
        return {"created_at": "", "title": None, "project": "", "models": {}}

    # --- parsing -------------------------------------------------------------
    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        try:
            with open(self.csv_path, newline="", encoding="utf-8", errors="replace") as fh:
                reader = csv.DictReader(fh)
                mapping, cost_is_credits = self._resolve_headers(reader.fieldnames)
                for row in reader:
                    try:
                        self._parse_row(row, mapping, cost_is_credits, sessions)
                    except (ValueError, TypeError):
                        continue  # one bad row never sinks the file
        except OSError:
            self._sessions = {}
            return self._sessions
        for sid, s in sessions.items():
            self._finalize(sid, s)
        # Drop sessions with no recorded token usage (mirrors CodexStore).
        self._sessions = {sid: s for sid, s in sessions.items() if s["model_rows"]}
        return self._sessions

    def _parse_row(self, row, mapping, cost_is_credits, sessions: dict[str, dict]) -> None:
        def g(field):
            col = mapping.get(field)
            return row.get(col) if col else None

        inp = self._to_int(g("input"))
        out = self._to_int(g("output"))
        cached = self._to_int(g("cached"))
        if inp == 0 and out == 0 and cached == 0:
            return  # nothing to attribute (header echo, blank line, metadata-only row)
        ts = self._parse_ts(g("timestamp"))
        project = (g("project") or "").strip()
        sid = (g("session") or "").strip()
        if not sid:
            # No session id: one synthetic session per (date, project) keeps the list
            # meaningful. Stable so reloads/merges don't churn ids.
            sid = "csv:" + (ts[:10] or "?") + "|" + (project or "?")
        s = sessions.setdefault(sid, self._new_session())
        if ts and (not s["created_at"] or ts < s["created_at"]):
            s["created_at"] = ts
        if not s["project"] and project:
            s["project"] = project
        if s["title"] is None:
            title = (g("title") or "").strip()
            if title:
                s["title"] = " ".join(title.split())[:80]

        model = self._prefix_model(g("model") or "")
        cost = self._to_float(g("cost"))
        if cost_is_credits:
            cost *= 0.01
        acc = s["models"].get(model)
        if acc is None:
            acc = s["models"][model] = self._new_acc()
        uncached = max(0, inp - cached)
        acc["runs"] += 1
        acc["input"] += uncached
        acc["cache_read"] += cached
        acc["output"] += out
        acc["cost"] += cost
        acc["tokens_total"] += uncached + cached + out

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title"] or "(untitled)"
        s["directory"] = self._resolve_dir(s["project"])
        rows: list[dict] = []
        for model_name, acc in s["models"].items():
            # Per-model priced/unpriced split (HermesStore pattern): a recorded cost > 0
            # prices the row as real spend (unpriced_* zeroed); $0 leaves the full counts
            # in the unpriced_* split so the "$" machinery estimates them at list price.
            # No subagents, so root == total.
            priced = acc["cost"] > 0
            u_in = 0 if priced else acc["input"]
            u_out = 0 if priced else acc["output"]
            u_cr = 0 if priced else acc["cache_read"]
            u_cw = 0 if priced else acc["cache_write"]
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
        s["unpriced_tokens"] = sum(r["tokens_total"] for r in rows if r["cost"] <= 0)

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
            "tokens_reasoning": 0,
            "tokens_cache_read": acc["cache_read"],
            "tokens_cache_write": acc["cache_write"],
            "tokens_total": acc["tokens_total"],
        }

    # --- Store interface -----------------------------------------------------
    def cache_inputs(self) -> list[str]:
        # The single CSV file whose (size, mtime) fingerprints the warm-start cache.
        return [self.csv_path]

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
                    subagents=0,  # CSV has no subagent tree
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
        # Mirror CodexStore._demo_workflow: anonymize, backfill a synthetic price for the
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
        # Single depth-0 node. cost is the recorded total ($0 for the subscription case);
        # _priced_nodes reprices a $0 node from its token columns under "$".
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
