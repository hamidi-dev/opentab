"""Logged-API-request JSONL (NDJSON) backend."""
from __future__ import annotations

import argparse
import json

from opentab.demo import demo_config
from opentab.formatting import _clean_prompt
from opentab.stores.csv_source import CsvStore


class JsonlStore(CsvStore):
    """Read one logged API request per NDJSON object.

    This is CsvStore's per-line twin and inherits its accounting, synthetic-session,
    pricing, Turns, and Tools rules. Timestamp, model, input, and output are required;
    supported optional fields are:

        timestamp   timestamp|time|ts|date|created_at   ISO-8601 or epoch (s/ms/us)
        model       model|model_id|model_name           e.g. gpt-4o, claude-sonnet-4
        input       input_tokens|input|prompt_tokens    as logged (includes cache reads/writes)
        output      output_tokens|output|completion_tokens  includes reasoning (priced once)
        cached      cached_tokens|cached|cache_read      cached portion of input (default 0)
        cache_write cache_write_tokens|cache_write       written portion of input (default 0)
        session     session_id|session|conversation_id  groups requests into one session
        request     request_id|id|req_id                stable per-request id (dedup)
        prompt      prompt|prompt_text|user_prompt       the user message -> Turns grouping
        prompt_id   prompt_id                            stable id for a prompt (optional)
        tool        tool|tool_name|tools                 tool call(s) this request made: a
                                                         list, or "Bash" / "Bash;Read" -> Tools tab
        project     project|repo|workspace|cwd|...       path -> git root; bare name as-is
        title       title|name|label                     session label (default first prompt)
        cost        cost_usd|cost (USD) | credits|credit (x $0.01)   presence -> metered

    Stable request ids deduplicate appended logs. Malformed lines are skipped.
    """

    source_name = "JSONL"

    # Own prefix for the synthetic (date, project) ids: CsvStore's context-curve gate
    # keys off it, so sharing the parent's "csv:" would leave the gate dead here.
    SYNTHETIC_ID_PREFIX = "jsonl:"

    # canonical field -> the JSON keys accepted for it (first present, non-empty wins).
    _KEYS = {
        "timestamp": ("timestamp", "time", "ts", "date", "created_at", "datetime"),
        "model": ("model", "model_id", "model_name"),
        "input": ("input_tokens", "input", "prompt_tokens"),
        "output": ("output_tokens", "output", "completion_tokens"),
        "cached": ("cached_tokens", "cached", "cache_read", "cache_read_tokens"),
        "cache_write": (
            "cache_write_tokens",
            "cache_write_input_tokens",
            "cache_write",
        ),
        "session": ("session_id", "session", "conversation_id", "conversation"),
        "request": ("request_id", "id", "req_id"),
        "prompt": ("prompt", "prompt_text", "user_prompt"),
        "prompt_id": ("prompt_id",),
        "tool": ("tool", "tool_name", "tools"),
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
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._sessions: dict[str, dict] | None = None
        self._git_root_cache: dict[str, str] = {}
        self._records_cost: bool | None = None  # resolved lazily (records_cost property)

    def cache_inputs(self) -> list[str]:
        # The single JSONL file whose (size, mtime) fingerprints the warm-start cache.
        return [self.path]

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
        cache_write = self._to_int(self._get(obj, "cache_write"))
        cost = self._row_cost(obj)
        cached = min(cached, inp)
        cache_write = min(cache_write, inp - cached)
        # A cost-only line (no token counts) is still real spend; only lines with
        # neither tokens nor cost are skipped (metadata-only / malformed line).
        if inp == 0 and out == 0 and cached == 0 and cache_write == 0 and cost <= 0:
            return
        ts = self._parse_ts(self._get(obj, "timestamp"))
        ts_epoch = self._parse_ts_epoch(self._get(obj, "timestamp"))  # absolute, for worked
        project = str(self._get(obj, "project") or "").strip()
        sid = str(self._get(obj, "session") or "").strip()
        synthetic = not sid
        if synthetic:
            # No session id: one synthetic session per (date, project), stable across
            # reloads/merges -- same fallback CsvStore uses.
            sid = self.SYNTHETIC_ID_PREFIX + (ts[:10] or "?") + "|" + (project or "?")
        s = sessions.setdefault(sid, self._new_session())
        # What was minted vs what was logged, remembered rather than re-read off the id
        # prefix -- see CsvStore._parse_row; supports_context_curve reads this.
        s["synthetic"] = s["synthetic"] or synthetic

        rid = str(self._get(obj, "request") or "").strip()
        if rid:
            if rid in s["seen"]:
                return  # regenerated/appended overlap -- count each request once
            s["seen"].add(rid)

        if ts and (not s["created_at"] or ts < s["created_at"]):
            s["created_at"] = ts
        if ts and ts > s["ended_at"]:
            s["ended_at"] = ts  # the canonical local format sorts lexicographically
        if not s["project"] and project:
            s["project"] = project

        model = self._prefix_model(str(self._get(obj, "model") or ""))
        acc = s["models"].get(model)
        if acc is None:
            acc = s["models"][model] = self._new_acc()
        uncached = inp - cached - cache_write
        self._accumulate(acc, uncached, cached, cache_write, out, cost)

        raw_prompt = self._get(obj, "prompt")
        full = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
        prompt = _clean_prompt(full)
        pid_raw = self._get(obj, "prompt_id")  # keep a falsy-but-present id (e.g. 0)
        pid = "" if pid_raw is None else str(pid_raw).strip()
        if s["title"] is None:  # title precedence: explicit title > first prompt
            title = str(self._get(obj, "title") or "").strip()
            s["title"] = " ".join(title.split())[:80] if title else (prompt[:80] or None)

        s["turns"].append(
            {
                "ts": ts or "",
                "ts_epoch": ts_epoch,  # absolute epoch (DST-proof), for worked-time
                "depth": 0,  # logged requests have no subagent tree
                "agent": "-",
                "model_name": model,
                "cost": round(cost, 6),
                "input": uncached,
                "output": out,
                "reasoning": 0,
                "cache_read": cached,
                "cache_write": cache_write,
                "tokens_total": uncached + cached + cache_write + out,
                "prompt": prompt,
                "prompt_full": full,  # uncapped; the Turns tab can expand it
                "prompt_id": pid,
                "tools": self._row_tools(obj),
            }
        )

    def _row_tools(self, obj: dict) -> list[str]:
        # The optional per-request tool call(s): a JSON list of names, or a string
        # ("Bash" / "Bash;Read") handled by the CsvStore splitter.
        raw = self._get(obj, "tool")
        if isinstance(raw, list):
            return [str(t).strip() for t in raw if str(t).strip()]
        return self._split_tools(raw)
