"""Logged-API-request JSONL (NDJSON) backend."""
from __future__ import annotations

import argparse
import hashlib
import json

from opentab.accounting.tiers import request_context
from opentab.demo import demo_config
from opentab.presentation.formatting import _clean_prompt
from opentab.stores.csv_source import CsvStore
from opentab.util import TRACE_TEXT_CAP, TraceContent, safe_float


class JsonlStore(CsvStore):
    """Read one logged API request per NDJSON object.

    This is CsvStore's per-line twin and inherits its accounting, synthetic-session,
    pricing, Turns, and Tools rules. Cost-only rows need no tokens; explicit events
    retain zero-usage detail and duration snapshots replace cumulative totals.
    See docs/sources.md for the duration/trace schema. Request fields are:

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
        "context_tokens": ("context_tokens",),
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
        snapshots = {}
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                for ordinal, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue  # one bad line never sinks the file
                    if not isinstance(obj, dict):
                        continue
                    obj = {str(k).lower(): v for k, v in obj.items()}
                    obj["_content_key"] = self._content_key(ordinal, line)
                    obj["_has_content"] = bool(obj.get("response") or obj.get("details"))
                    # Raw content is read again only when the user opens its trace.
                    obj.pop("response", None)
                    obj.pop("details", None)
                    try:
                        if obj.get("record_type") == "usage_snapshot":
                            sid, rid = self._get(obj, "session"), self._get(obj, "request")
                            if (
                                not isinstance(sid, str)
                                or not sid
                                or not isinstance(rid, str)
                                or not rid
                            ):
                                continue
                            seconds = safe_float(obj.get("duration_seconds"), -1)
                            rate = safe_float(obj.get("rate_per_minute"), -1)
                            if (
                                seconds < 0
                                or rate < 0
                                or obj.get("usage_status")
                                not in ("running", "unconfirmed", "confirmed")
                            ):
                                continue
                            model = self._get(obj, "model")
                            if (
                                not isinstance(model, str)
                                or not model.strip()
                                or self._parse_ts_epoch(self._get(obj, "timestamp")) is None
                            ):
                                continue
                            marker = (sid, rid)
                            previous = snapshots.get(marker)
                            # A confirmed final result cannot be displaced by a late
                            # running/unconfirmed snapshot (or replay of one).
                            if (
                                previous
                                and previous.get("usage_status") == "confirmed"
                                and obj.get("usage_status") != "confirmed"
                            ):
                                continue
                            snapshots[marker] = obj
                            continue
                        self._ingest(obj, sessions)
                    except (ValueError, TypeError):
                        continue
        except OSError:
            self._sessions = {}
            return self._sessions
        for obj in snapshots.values():
            try:
                self._ingest(obj, sessions)
            except (ValueError, TypeError):
                continue
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
        duration = self._to_float(obj.get("duration_seconds"))
        rate = self._to_float(obj.get("rate_per_minute"))
        estimated = safe_float(duration * rate / 60) if not cost else 0.0
        event = obj.get("record_type") in ("event", "usage_snapshot")
        cached = min(cached, inp)
        cache_write = min(cache_write, inp - cached)
        # A cost-only line (no token counts) is still real spend; only lines with
        # neither tokens nor cost are skipped (metadata-only / malformed line).
        if (
            inp == 0
            and out == 0
            and cached == 0
            and cache_write == 0
            and cost <= 0
            and not event
            and not estimated
        ):
            return
        ts = self._parse_ts(str(self._get(obj, "timestamp") or ""))
        ts_epoch = self._parse_ts_epoch(self._get(obj, "timestamp"))  # absolute, for worked
        project = str(self._get(obj, "project") or "").strip()
        sid = str(self._get(obj, "session") or "").strip()
        if event and (ts_epoch is None or not sid or not self._get(obj, "model")):
            return
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
        # Event rows are timeline detail, not model invocations.
        if obj.get("record_type") == "event":
            acc["runs"] -= 1
        acc["estimated_cost"] = acc.get("estimated_cost", 0.0) + estimated
        if "duration_seconds" in obj:
            s["usage_seconds"] = s.get("usage_seconds", 0.0) + duration
        s["duration_based"] = s.get("duration_based", False) or "duration_seconds" in obj

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
                "agent": str(obj.get("role") or obj.get("event_kind") or "-"),
                "model_name": model,
                "cost": round(cost, 6),
                "input": uncached,
                "context_tokens": request_context(obj),
                "output": out,
                "reasoning": 0,
                "cache_read": cached,
                "cache_write": cache_write,
                "tokens_total": uncached + cached + cache_write + out,
                "prompt": prompt,
                "prompt_full": full,  # uncapped; the Turns tab can expand it
                "prompt_id": pid,
                "tools": self._row_tools(obj),
                "estimated_cost": estimated,
                "duration_seconds": duration if "duration_seconds" in obj else None,
                "usage_status": str(obj.get("usage_status") or ""),
                "event_kind": str(obj.get("event_kind") or ""),
                "content_key": obj.get("_content_key", ""),
                "has_text": bool(obj.get("_has_content")),
            }
        )

    @staticmethod
    def _content_key(ordinal: int, line: str) -> str:
        return f"jsonl:{ordinal}:" + hashlib.sha256(line.strip().encode()).hexdigest()

    def _finalize(self, sid: str, s: dict) -> None:
        super()._finalize(sid, s)
        for row in s["model_rows"]:
            estimate = s["models"][row["model_name"]].get("estimated_cost", 0.0)
            row["estimated_cost"] = row["root_estimated_cost"] = estimate
        if s.get("duration_based"):
            # Billed connected time is not an agent's active working time.
            s["worked_seconds"] = None
            statuses = {t.get("usage_status") for t in s["turns"] if t.get("usage_status")}
            s["usage_status"] = next(
                (
                    status
                    for status in ("unconfirmed", "running", "confirmed")
                    if status in statuses
                ),
                "",
            )

    def workflows(self):
        rows = super().workflows()
        if not self.demo:
            for row in rows:
                session = self._parse()[row.id]
                row.usage_seconds = session.get("usage_seconds")
                row.usage_status = session.get("usage_status", "")
        return rows

    def message_timeline(self, workflow_id: str) -> list[dict]:
        rows = super().message_timeline(workflow_id)
        # Speech offsets can share the same displayed second. Preserve true
        # timestamp order, including full-duplex overlapping speaker groups.
        rows.sort(key=lambda row: row.get("ts_epoch") or 0)
        if self.demo:
            for row in rows:
                for field in (
                    "estimated_cost",
                    "duration_seconds",
                    "usage_status",
                    "event_kind",
                    "content_key",
                    "has_text",
                ):
                    row.pop(field, None)
        return rows

    def workflow_nodes(self, workflow_id: str) -> list[dict]:
        rows = super().workflow_nodes(workflow_id)
        if rows and not self.demo:
            rows[0]["estimated_cost"] = sum(
                r.get("estimated_cost", 0.0)
                for r in self._parse().get(workflow_id, {}).get("model_rows", [])
            )
        return rows

    def supports_context_curve(self, workflow_id: str) -> bool:
        return not self._parse().get(workflow_id, {}).get(
            "duration_based"
        ) and super().supports_context_curve(workflow_id)

    def supports_turn_content(self, workflow_id: str) -> bool:
        return not self.demo and any(
            t.get("has_text") for t in self._parse().get(workflow_id, {}).get("turns", [])
        )

    def turn_content(self, workflow_id: str, content_key: str | None = None) -> dict:
        trace = TraceContent(content_key)
        if self.demo:
            return trace
        owned = {
            t["content_key"]
            for t in self._parse().get(workflow_id, {}).get("turns", [])
            if t.get("has_text")
        }
        if content_key is not None and content_key not in owned:
            return trace
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                for ordinal, line in enumerate(fh):
                    key = self._content_key(ordinal, line)
                    if key not in owned or not trace.accepts(key):
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    obj = {str(k).lower(): v for k, v in obj.items()}
                    events = []
                    for value in (
                        obj.get("response"),
                        "```json\n"
                        + json.dumps(obj["details"], ensure_ascii=False, indent=2)
                        + "\n```"
                        if obj.get("details")
                        else "",
                    ):
                        text, dropped = trace.clip(value, TRACE_TEXT_CAP)
                        if text:
                            events.append({"kind": "text", "text": text, "dropped": dropped})
                    if events:
                        trace[key] = events
        except OSError:
            pass
        return trace

    def _row_tools(self, obj: dict) -> list[str]:
        # The optional per-request tool call(s): a JSON list of names, or a string
        # ("Bash" / "Bash;Read") handled by the CsvStore splitter.
        raw = self._get(obj, "tool")
        if isinstance(raw, list):
            return [str(t).strip() for t in raw if str(t).strip()]
        return self._split_tools(raw)
