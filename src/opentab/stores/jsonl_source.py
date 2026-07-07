"""Logged-API-request JSONL (NDJSON) backend."""
from __future__ import annotations

import argparse
import json
import random

from opentab.formatting import _clean_prompt
from opentab.stores.csv_source import CsvStore


class JsonlStore(CsvStore):
    """Read an NDJSON file of logged API requests (one JSON object per line) behind the
    same interface App expects -- the per-line twin of CsvStore. It inherits CsvStore's
    OpenAI-style token accounting, mixed per-row cost (records_cost is a per-instance
    attr), provider-prefixed models, synthetic-session fallback, the priced/unpriced
    split and demo handling; only the parser (NDJSON instead of csv.DictReader) differs.

    What JSONL adds *over* CSV is per-request structure, so this backend also implements
    the Turns tab (message_timeline/supports_turns) -- something CSV can't do well. Each
    line is one API request = one LLM step ("turn"); the optional per-line `prompt`
    groups turns under the user message that triggered them, the way OpenCode/Claude do.

    One JSON object per line (UTF-8, NDJSON -- *not* a JSON array). Keys are matched by a
    small alias set; required are a timestamp, a model, and input/output token counts.
    Everything else is optional:

        timestamp   timestamp|time|ts|date|created_at   ISO-8601 or epoch (s/ms/us)
        model       model|model_id|model_name           e.g. gpt-4o, claude-sonnet-4
        input       input_tokens|input|prompt_tokens    as logged (may include cached read)
        output      output_tokens|output|completion_tokens  includes reasoning (priced once)
        cached      cached_tokens|cached|cache_read      cached portion of input (default 0)
        session     session_id|session|conversation_id  groups requests into one session
        request     request_id|id|req_id                stable per-request id (dedup)
        prompt      prompt|prompt_text|user_prompt       the user message -> Turns grouping
        prompt_id   prompt_id                            stable id for a prompt (optional)
        project     project|repo|workspace|cwd|...       path -> git root; bare name as-is
        title       title|name|label                     session label (default first prompt)
        cost        cost_usd|cost (USD) | credits|credit (x $0.01)   presence -> metered

    A logged Copilot request carries no dollar cost (usage-based credits settle
    server-side), so a $0/absent-cost row is a *subscription* row: every token unpriced,
    repriced at list rates under "$". A populated cost_usd/credits column prices those
    rows as real spend (records_cost True, unpriced_* zeroed) -- the HermesStore pattern.
    Requests with a stable `request_id` dedupe across a regenerated/appended file; with
    no `session_id`, requests group into one synthetic session per (date, project). No
    subagent tree -- every turn is depth 0. Sessions with no token usage are dropped.
    """

    source_name = "JSONL"

    # canonical field -> the JSON keys accepted for it (first present, non-empty wins).
    _KEYS = {
        "timestamp": ("timestamp", "time", "ts", "date", "created_at", "datetime"),
        "model": ("model", "model_id", "model_name"),
        "input": ("input_tokens", "input", "prompt_tokens"),
        "output": ("output_tokens", "output", "completion_tokens"),
        "cached": ("cached_tokens", "cached", "cache_read", "cache_read_tokens"),
        "session": ("session_id", "session", "conversation_id", "conversation"),
        "request": ("request_id", "id", "req_id"),
        "prompt": ("prompt", "prompt_text", "user_prompt"),
        "prompt_id": ("prompt_id",),
        "project": (
            "project",
            "repo",
            "repository",
            "workspace",
            "directory",
            "dir",
            "cwd",
            "folder",
        ),
        "title": ("title", "name", "label"),
    }

    def __init__(self, path: str, args: argparse.Namespace):
        self.path = path
        self.args = args
        self.demo = getattr(args, "demo", False)
        # Same hidden per-process factor Store/CsvStore use; 1.0 outside demo.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._sessions: dict[str, dict] | None = None
        self._git_root_cache: dict[str, str] = {}
        self._records_cost: bool | None = None  # resolved lazily (records_cost property)

    def cache_inputs(self) -> list[str]:
        # The single JSONL file whose (size, mtime) fingerprints the warm-start cache.
        return [self.path]

    # --- value access --------------------------------------------------------
    @classmethod
    def _get(cls, obj: dict, field: str):
        for k in cls._KEYS[field]:
            v = obj.get(k)
            if v not in (None, ""):
                return v
        return None

    def _row_cost(self, obj: dict) -> float:
        # USD if present, else credits x $0.01 (Copilot/IntelliJ style), else $0.
        for k in ("cost_usd", "cost"):
            if obj.get(k) not in (None, ""):
                return self._to_float(obj.get(k))
        for k in ("credits", "credit"):
            if obj.get(k) not in (None, ""):
                return self._to_float(obj.get(k)) * 0.01
        return 0.0

    def _probe_records_cost(self) -> bool:
        # True iff any line records a positive cost. Early-exits so it stays cheap; only
        # run when records_cost (the lazy CsvStore property) is read before any parse.
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and self._row_cost(obj) > 0:
                        return True
        except OSError:
            return False
        return False

    # --- accumulation --------------------------------------------------------
    @staticmethod
    def _new_session() -> dict:
        s = CsvStore._new_session()
        s["turns"] = []  # one per request, for the Turns tab (chronological)
        s["seen"] = set()  # request ids already counted (regenerate/append dedup)
        return s

    # --- parsing -------------------------------------------------------------
    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue  # one bad line never sinks the file
                    if not isinstance(obj, dict):
                        continue
                    try:
                        self._ingest(obj, sessions)
                    except (ValueError, TypeError):
                        continue
        except OSError:
            self._sessions = {}
            return self._sessions
        for sid, s in sessions.items():
            self._finalize(sid, s)
        # Drop sessions with no recorded token usage (mirrors CsvStore/CodexStore).
        self._sessions = {sid: s for sid, s in sessions.items() if s["model_rows"]}
        return self._sessions

    def _ingest(self, obj: dict, sessions: dict[str, dict]) -> None:
        inp = self._to_int(self._get(obj, "input"))
        out = self._to_int(self._get(obj, "output"))
        cached = self._to_int(self._get(obj, "cached"))
        cost = self._row_cost(obj)
        # A cost-only line (no token counts) is still real spend; only lines with
        # neither tokens nor cost are skipped (metadata-only / malformed line).
        if inp == 0 and out == 0 and cached == 0 and cost <= 0:
            return
        ts = self._parse_ts(self._get(obj, "timestamp"))
        project = str(self._get(obj, "project") or "").strip()
        sid = str(self._get(obj, "session") or "").strip()
        if not sid:
            # No session id: one synthetic session per (date, project), stable across
            # reloads/merges -- same fallback CsvStore uses.
            sid = "jsonl:" + (ts[:10] or "?") + "|" + (project or "?")
        s = sessions.setdefault(sid, self._new_session())

        rid = str(self._get(obj, "request") or "").strip()
        if rid:
            if rid in s["seen"]:
                return  # regenerated/appended overlap -- count each request once
            s["seen"].add(rid)

        if ts and (not s["created_at"] or ts < s["created_at"]):
            s["created_at"] = ts
        if not s["project"] and project:
            s["project"] = project

        model = self._prefix_model(str(self._get(obj, "model") or ""))
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

        prompt = self._get(obj, "prompt")
        prompt = _clean_prompt(prompt) if isinstance(prompt, str) else ""
        pid = str(self._get(obj, "prompt_id") or "").strip()
        if s["title"] is None:  # title precedence: explicit title > first prompt
            title = str(self._get(obj, "title") or "").strip()
            s["title"] = " ".join(title.split())[:80] if title else (prompt[:80] or None)

        s["turns"].append(
            {
                "ts": ts or "",
                "depth": 0,  # logged requests have no subagent tree
                "agent": "-",
                "model_name": model,
                "cost": round(cost, 6),
                "input": uncached,
                "output": out,
                "reasoning": 0,
                "cache_read": cached,
                "cache_write": 0,
                "tokens_total": uncached + cached + out,
                "prompt": prompt,
                "prompt_id": pid,
            }
        )

    # --- Turns tab opt-in ----------------------------------------------------
    def message_timeline(self, workflow_id: str) -> list[dict]:
        # Chronological per-turn rows. ISO/canonical "YYYY-MM-DD HH:MM:SS" timestamps
        # sort in time order; a turn's prompt_id (the explicit id, else the prompt text)
        # groups consecutive same-prompt turns under one "▸" header, like the other
        # backends. App._scale_demo_turns hides magnitudes in demo, like Tools.
        s = self._parse().get(workflow_id)
        if not s:
            return []
        out = []
        for t in sorted(s["turns"], key=lambda r: r["ts"]):
            r = dict(t)
            r["time"] = r.pop("ts")  # already canonical "YYYY-MM-DD HH:MM:SS" (local)
            prompt = r.pop("prompt", "")
            explicit = r.pop("prompt_id", "")
            r["prompt_id"] = explicit or prompt  # group consecutive same-prompt turns
            r["prompt_title"] = prompt
            out.append(r)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        return True
