"""Bahulam Code JSONL transcript backend."""
from __future__ import annotations

import argparse
import glob
import json
import os

from opentab.demo import demo_config, scramble_node, scramble_workflow
from opentab.formatting import _clean_prompt, iso_to_epoch, iso_to_local, worked_seconds
from opentab.models import Workflow
from opentab.pricing import api_equivalent_cost, model_family
from opentab.util import git_root, read_files_parallel, safe_int, tool_rows_from_turns


class BahulamStore:
    """Read Bahulam Code transcripts from ``~/.bahulam/projects/**/*.jsonl``.

    Bahulam records per-turn token usage and total_cost in every ``complete``
    event.  The wire format uses ``bahulam_event`` as the top-level type; all
    payload fields live under ``event.data.*``.
    """

    records_cost = True  # Bahulam records total_cost in every usage block
    combined = False
    source_name = "Bahulam Code"

    def __init__(self, root_dir: str, args: argparse.Namespace):
        """Store constructor.

        Args:
            root_dir: Directory tree to scan for ``.jsonl`` session files.
            args: CLI arguments, used for demo config and optional overrides.
        """
        self.root_dir = root_dir
        self.args = args
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._sessions: dict[str, dict] | None = None
        self._one: tuple[str, dict] | None = None  # single-session memo for detail tabs
        self._git_root_cache: dict[str, str] = {}

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _new_acc() -> dict:
        """Return a zeroed accumulator dict with all billing counters."""
        return {
            "runs": 0,
            "cost": 0.0,
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "cache_write_1h": 0,
            "tokens_total": 0,
            "u_input": 0,
            "u_output": 0,
            "u_reasoning": 0,
            "u_cache_read": 0,
            "u_cache_write": 0,
            "u_cache_write_1h": 0,
        }

    @staticmethod
    def _int(value) -> int:
        """Safely cast *value* to int, returning 0 on failure."""
        return safe_int(value)

    @staticmethod
    def _float(value) -> float:
        """Safely cast *value* to float, returning 0.0 on failure."""
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _first_value(*values):
        """Return the first non-empty value from *values*, or 0."""
        for value in values:
            if value not in (None, ""):
                return value
        return 0

    def _primary_model(self, models_map: dict) -> str:
        """Return the execution model from a session_info models map."""
        for key in ("coder", "main", "executor", "orchestrator", "planning"):
            model_name = models_map.get(key)
            if model_name:
                return self._qualified_model(model_name)
        return ""

    @classmethod
    def _reported_cost(cls, usage: dict) -> tuple[bool, float]:
        """Return ``(has_reported_cost, cost)`` for a raw usage dict."""
        for key in ("cost", "cost_usd", "total_cost", "total_cost_usd"):
            value = usage.get(key)
            if value in (None, ""):
                continue
            cost = cls._float(value)
            if cost >= 0:
                return True, cost
        return False, 0.0

    @classmethod
    def _add_usage(cls, acc: dict, usage: dict, *, hosted_unpriced: bool = False) -> None:
        """Accumulate counters from a usage dict into *acc*.

        Every Bahulam usage shape — top-level aggregate, per-model row, or
        sub-agent rollup — reports ``input_tokens``/``total_input_tokens``
        **inclusive of cache reads and cache creations**, matching the
        anthropic-style convention. The previous ``additive_input`` flag
        double-counted cache reads by ~2× on models[] rows; removed.

        hosted_unpriced=True: the parent session is Bahulam-hosted and the
        user was not charged (`is_byok=false` and `credits_charged=0`). The
        provider list cost is still available on the raw event but is NOT
        user spend, so we drop it into the unpriced buckets to keep the
        cost column honest. Metered/BYOK sessions keep the provider cost.
        """
        total_in = cls._int(usage.get("total_input_tokens", 0))
        out = cls._int(usage.get("total_output_tokens", 0))
        cr = cls._int(usage.get("cache_read_input_tokens", 0))
        cw = cls._int(usage.get("cache_creation_input_tokens", 0))
        reasoning = cls._int(usage.get("reasoning_tokens", 0))
        has_cost, cost = cls._reported_cost(usage)
        cc = usage.get("cache_creation")
        cw1h = cls._int(cc.get("ephemeral_1h_input_tokens", 0) or 0) if isinstance(cc, dict) else 0
        inp = max(0, total_in - cr - cw)
        acc["runs"] += 1
        acc["input"] += inp
        acc["output"] += out
        acc["reasoning"] += reasoning
        acc["cache_read"] += cr
        acc["cache_write"] += cw
        acc["cache_write_1h"] += min(cw1h, cw)
        acc["tokens_total"] += inp + out + reasoning + cr + cw
        if has_cost and not hosted_unpriced:
            acc["cost"] += cost
        else:
            # Missing cost OR hosted-unpriced: bucket tokens as unpriced.
            acc["u_input"] += inp
            acc["u_output"] += out
            acc["u_reasoning"] += reasoning
            acc["u_cache_read"] += cr
            acc["u_cache_write"] += cw
            acc["u_cache_write_1h"] += min(cw1h, cw)

    @staticmethod
    def _price(model_name: str, acc: dict) -> float:
        """Return the API-equivalent cost for a given model and accumulator."""
        return api_equivalent_cost(
            model_name,
            acc["input"],
            acc["output"],
            acc["reasoning"],
            acc["cache_read"],
            acc["cache_write"],
            acc.get("cache_write_1h", 0),
        )

    @staticmethod
    def _provider_prefix(model_name: str) -> str:
        """Infer the provider prefix for a bare model name (e.g. ``deepseek-v4-pro`` -> ``deepseek/``).

        Returns the prefix string with trailing slash, or ``""`` if the family is unknown.
        """
        fam = model_family(model_name)
        if not fam or fam == "unknown":
            return ""
        return fam + "/"

    def _qualified_model(self, model_name: str) -> str:
        """Return *model_name* with its provider prefix prepended if it is bare."""
        if not model_name or "/" in model_name:
            return model_name
        return self._provider_prefix(model_name) + model_name

    def _git_root(self, cwd: str) -> str:
        """Return the git root for a working directory, cached per directory."""
        if cwd not in self._git_root_cache:
            self._git_root_cache[cwd] = git_root(cwd)
        return self._git_root_cache[cwd]

    # ── file discovery ────────────────────────────────────────────────────

    def cache_inputs(self) -> list[str]:
        """Return all session file paths (cache input contract)."""
        return self._files()

    def _files(self) -> list[str]:
        """Glob every ``.jsonl`` under ``root_dir`` recursively."""
        return glob.glob(os.path.join(self.root_dir, "**", "*.jsonl"), recursive=True)

    # ── single-session fast path ──────────────────────────────────────────

    def _parse_one(self, workflow_id: str) -> dict | None:
        """Parse a single session by ID without loading all files.

        Returns the session dict, or ``None`` if not found.
        """
        for path in self._files():
            if os.path.splitext(os.path.basename(path))[0] == workflow_id:
                items = [(path, _read_text(path))]
                sessions = self._parse_texts(items)
                return sessions.get(workflow_id)
        return None

    def _session(self, workflow_id: str, fallback: bool = True) -> dict | None:
        """Return a session dict by ID, using fast-path lookups when possible.

        Args:
            workflow_id: The session ID (filename stem).
            fallback: When ``True``, fall back to a full parse if not found in
                the single-session fast path.

        Returns the session dict or ``None``.
        """
        if self._sessions is not None:
            return self._sessions.get(workflow_id)
        if self._one is not None and self._one[0] == workflow_id:
            return self._one[1]
        s = self._parse_one(workflow_id)
        if s is not None:
            self._one = (workflow_id, s)
            return s
        if not fallback:
            return None
        return self._parse().get(workflow_id)

    # ── parsing ───────────────────────────────────────────────────────────

    def _parse(self) -> dict[str, dict]:
        """Parse all session files, memoised.

        Returns ``session_id -> session dict``.
        """
        if self._sessions is not None:
            return self._sessions
        self._sessions = self._parse_texts(read_files_parallel(self._files()))
        return self._sessions

    def _parse_texts(self, items) -> dict[str, dict]:
        """Parse a stream of ``(path, text)`` tuples into session dicts.

        Each path yields one session keyed by its filename stem.
        """
        sessions: dict[str, dict] = {}
        for path, text in items:
            session_id = os.path.splitext(os.path.basename(path))[0]
            s = sessions.setdefault(session_id, self._new_session())
            s["files"].add(path)
            self._parse_file(text, session_id, s, path)
        for sid, s in sessions.items():
            self._finalize(sid, s)
        return sessions

    @staticmethod
    def _new_session() -> dict:
        """Return a fresh, empty session dict with all fields initialised."""
        return {
            "cwd": None,
            "ts_min": None,
            "ts_max": None,
            "title_prompt": None,
            "model": None,
            "models": {},  # model_name -> {"total": acc, "root": acc}
            "turns": [],
            "prompts": [],
            "event_ts": [],
            "pending_tools": [],  # tool names queued before the next complete event
            "active_subagents": {},
            "subagent_runs": [],
            "files": set(),
            # Billing split — Bahulam ships two modes:
            #   is_byok=True  (BYOK): user pays the LLM provider directly.
            #                          provider `cost` == real user spend.
            #   is_byok=False (hosted): Bahulam pays the provider and charges
            #                          the user via `credits_charged` (often
            #                          $0 on free-tier or promo). Provider
            #                          `cost` is Bahulam's COGS, NOT user
            #                          spend, and must be treated as unpriced
            #                          in the normal cost column.
            #   is_byok=None  (unknown / legacy transcripts): fall back to
            #                          provider cost so old transcripts still
            #                          render sensibly.
            "is_byok": None,
            "credits_charged": 0.0,
        }

    @staticmethod
    def _is_root_role(role: str) -> bool:
        """Return ``True`` when a usage role belongs to the main/root agent."""
        return role in ("", "coder", "main", "executor", "orchestrator")

    def _model_usage(self, usage: dict) -> dict:
        """Normalize a per-model or sub-agent usage dict for ``_add_usage``."""
        return {
            "total_input_tokens": usage.get("input_tokens", 0),
            "total_output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_tokens", 0),
            "reasoning_tokens": usage.get("reasoning_tokens", 0) or 0,
        }

    @staticmethod
    def _usage_cost(usage: dict):
        """Return a per-model cost value, preserving missing vs reported zero."""
        return next(
            (usage.get(key) for key in ("cost", "cost_usd") if usage.get(key) not in (None, "")),
            None,
        )

    def _record_subagent_start(self, s: dict, data: dict, ts: str | None) -> None:
        """Remember a sub-agent's start metadata until its complete event arrives."""
        key = data.get("task_id") or f"{data.get('type') or 'subagent'}:{len(s['subagent_runs'])}"
        s["active_subagents"][key] = {
            "type": data.get("type") or "",
            "model": data.get("model") or "",
            "query": data.get("query") or "",
            "ts": ts or "",
        }

    def _record_subagent_complete(self, s: dict, data: dict, ts: str | None) -> None:
        """Record one completed Bahulam sub-agent for the Subagents tab."""
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        key = data.get("task_id") or f"{data.get('type') or 'subagent'}:{len(s['subagent_runs'])}"
        start = s["active_subagents"].pop(key, {})
        role = str(usage.get("role") or data.get("type") or start.get("type") or "subagent")
        model_name = self._qualified_model(
            data.get("model") or usage.get("model") or start.get("model") or ""
        )
        acc = self._new_acc()
        hosted_unpriced = s["is_byok"] is False
        if usage:
            normalized = self._model_usage(usage)
            cost = self._usage_cost(usage)
            if cost is not None:
                normalized["cost"] = cost
            self._add_usage(acc, normalized, hosted_unpriced=hosted_unpriced)
        title = (
            data.get("result_summary")
            or start.get("query")
            or data.get("query")
            or f"{role} sub-agent"
        )
        s["subagent_runs"].append(
            {
                "id": key,
                "role": role,
                "agent": role,
                "model_name": model_name or "unknown",
                "title": _clean_prompt(str(title)),
                "ts": start.get("ts") or ts or "",
                "acc": acc,
                # Hosted sessions never have provider cost as user spend, so
                # mark cost_assigned=True to short-circuit later distribution
                # attempts even when the raw event carried a provider cost.
                "cost_assigned": hosted_unpriced or self._usage_cost(usage) is not None,
            }
        )

    def _assign_subagent_cost(
        self, s: dict, role: str, model_name: str, usage: dict, ts: str | None,
        *, hosted_unpriced: bool = False,
    ) -> None:
        """Attach aggregate per-role cost from ``complete`` to matching sub-agent runs.

        Skipped entirely for hosted sessions — provider cost is not user
        spend, so there is nothing to distribute.
        """
        if hosted_unpriced:
            return
        has_cost, cost = self._reported_cost(usage)
        if not has_cost:
            return
        matches = [
            r
            for r in s["subagent_runs"]
            if not r.get("cost_assigned")
            and r.get("role") == role
            and r.get("model_name") == model_name
        ]
        if not matches:
            already_recorded = any(
                r.get("role") == role and r.get("model_name") == model_name
                for r in s["subagent_runs"]
            )
            if already_recorded:
                return
            acc = self._new_acc()
            self._add_usage(acc, usage)
            acc["cost"] = cost
            matches = [
                {
                    "id": f"{role}:{len(s['subagent_runs'])}",
                    "role": role,
                    "agent": role,
                    "model_name": model_name or "unknown",
                    "title": f"{role} sub-agent",
                    "ts": ts or "",
                    "acc": acc,
                    "cost_assigned": True,
                }
            ]
            s["subagent_runs"].extend(matches)
            return
        denom = sum(r["acc"]["tokens_total"] for r in matches)
        for r in matches:
            share = cost / len(matches) if denom <= 0 else cost * r["acc"]["tokens_total"] / denom
            r["acc"]["cost"] += share
            r["cost_assigned"] = True

    def _parse_file(self, text: str, session_id: str, s: dict, path: str = "") -> None:
        """Decode a single JSONL file body and ingest every line into *s*."""
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            self._ingest(obj, s, path)

    def _ingest(self, o: dict, s: dict, path: str = "") -> None:
        """Ingest a single JSON record *o* into the session dict *s*.

        Handles user messages and ``bahulam_event`` records (session_info,
        complete, tool_call), and extracts timestamps / cwd.
        """
        if path:
            s["files"].add(path)

        ts = o.get("timestamp")
        if ts:
            if s["ts_min"] is None or ts < s["ts_min"]:
                s["ts_min"] = ts
            if s["ts_max"] is None or ts > s["ts_max"]:
                s["ts_max"] = ts
            s["event_ts"].append(ts)

        # Top-level cwd — every record carries it
        cwd = o.get("cwd")
        if cwd and not s["cwd"]:
            s["cwd"] = cwd

        typ = o.get("type")

        # User messages — seed title from the first real prompt
        if typ in ("user",):
            msg = o.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                text = None
                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = (block.get("text") or "").strip()
                            break
                if text and not s["title_prompt"]:
                    s["title_prompt"] = text[:80]
                if text:
                    s["prompts"].append({"ts": ts or "", "title": text})

        # Event records carry their payload under ``event.data.*``.
        if typ != "bahulam_event":
            return
        event = o.get("event")
        if not isinstance(event, dict):
            return

        event_type = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}

        # session_info — carries the model map + billing mode
        if event_type == "session_info":
            if "is_byok" in data and s["is_byok"] is None:
                s["is_byok"] = bool(data.get("is_byok"))
            models_map = data.get("models")
            if isinstance(models_map, dict):
                primary = self._primary_model(models_map)
                if primary and not s["model"]:
                    s["model"] = primary
                for model_name in models_map.values():
                    qualified = self._qualified_model(model_name) if model_name else ""
                    if qualified and qualified not in s["models"]:
                        s["models"].setdefault(
                            qualified,
                            {"total": self._new_acc(), "root": self._new_acc()},
                        )

        elif event_type == "sub_agent_start":
            self._record_subagent_start(s, data, ts)

        elif event_type == "sub_agent_complete":
            self._record_subagent_complete(s, data, ts)

        # complete — carries per-LLM-step token usage and cost
        elif event_type == "complete":
            usage = data.get("usage") or {}
            if not usage:
                return

            # Hosted sessions expose provider list cost via `usage.cost*` but
            # bill the user via `data.credits_charged` (often 0). Only when
            # is_byok is EXPLICITLY False do we treat provider cost as
            # unpriced — unknown/legacy transcripts keep prior behavior.
            hosted_unpriced = s["is_byok"] is False
            credits_charged = self._float(
                data.get("credits_charged", usage.get("credits_charged", 0))
            )
            s["credits_charged"] += credits_charged

            reasoning = self._int(usage.get("reasoning_tokens", 0))

            # Per-model token breakdown (the wire uses ``cache_read_tokens``
            # and ``cache_creation_tokens`` at per-model granularity, with
            # ``input_tokens`` INCLUSIVE of cache — matches anthropic).
            models_usage = usage.get("models")
            if isinstance(models_usage, list) and models_usage:
                for m in models_usage:
                    if not isinstance(m, dict):
                        continue
                    model_name = self._qualified_model(m.get("model", ""))
                    if not model_name:
                        continue
                    entry = s["models"].get(model_name)
                    if entry is None:
                        entry = s["models"][model_name] = {
                            "total": self._new_acc(),
                            "root": self._new_acc(),
                        }
                    per_model = self._model_usage(m)
                    cost = self._usage_cost(m)
                    if cost is not None:
                        per_model["cost"] = cost
                    self._add_usage(entry["total"], per_model, hosted_unpriced=hosted_unpriced)
                    role = str(m.get("role") or "")
                    if self._is_root_role(role):
                        self._add_usage(entry["root"], per_model, hosted_unpriced=hosted_unpriced)
                    else:
                        self._assign_subagent_cost(
                            s, role, model_name, per_model, ts, hosted_unpriced=hosted_unpriced,
                        )
            else:
                model_name = self._qualified_model(
                    data.get("model") or usage.get("model") or s["model"] or ""
                )
                if model_name:
                    entry = s["models"].get(model_name)
                    if entry is None:
                        entry = s["models"][model_name] = {
                            "total": self._new_acc(),
                            "root": self._new_acc(),
                        }
                    self._add_usage(entry["total"], usage, hosted_unpriced=hosted_unpriced)
                    self._add_usage(entry["root"], usage, hosted_unpriced=hosted_unpriced)

            # Aggregate totals for the turn row
            total_in = self._int(usage.get("total_input_tokens", 0))
            out = self._int(usage.get("total_output_tokens", 0))
            cr = self._int(usage.get("cache_read_input_tokens", 0))
            cw = self._int(usage.get("cache_creation_input_tokens", 0))
            inp = max(0, total_in - cr - cw)
            _has_total_cost, provider_cost = self._reported_cost(usage)
            # Hosted turn: user paid credits_charged for THIS turn; provider
            # cost is Bahulam COGS. BYOK / unknown: keep provider cost.
            turn_cost = credits_charged if hosted_unpriced else provider_cost

            # Turn row for the Turns tab
            first_model = "unknown"
            if isinstance(models_usage, list) and models_usage:
                first = models_usage[0] if isinstance(models_usage[0], dict) else {}
                first_model = self._qualified_model(first.get("model", "unknown"))
            elif s["model"]:
                first_model = s["model"]
            turn_tools = list(s["pending_tools"])
            s["pending_tools"].clear()
            s["turns"].append(
                {
                    "ts": ts or "",
                    "depth": 0,
                    "agent": "-",
                    "effort": "",
                    "model_name": first_model,
                    "cost": float(turn_cost),
                    "input": inp,
                    "output": out,
                    "reasoning": reasoning,
                    "cache_read": cr,
                    "cache_write": cw,
                    "cache_write_1h": 0,
                    "tokens_total": inp + out + reasoning + cr + cw,
                    "tools": turn_tools,
                }
            )

        # tool_call / tool_request — queue the tool name for the next turn
        elif event_type in ("tool_call", "tool_request"):
            tool_name = data.get("tool") or data.get("name") or ""
            if tool_name:
                s["pending_tools"].append(tool_name)

    # ── finalization ──────────────────────────────────────────────────────

    def _finalize(self, sid: str, s: dict) -> None:
        """Derive computed fields (title, directory, times, model-rows, subagents).

        Called once per session after all lines have been ingested.
        """
        s["title"] = s["title_prompt"] or "(untitled)"
        s["directory"] = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"
        s["created_at"] = iso_to_local(s["ts_min"]) if s["ts_min"] else ""
        s["ended_at"] = iso_to_local(s["ts_max"]) if s["ts_max"] else ""
        s["worked_seconds"] = worked_seconds(
            [iso_to_epoch(t) for t in s["event_ts"]],
            [iso_to_epoch(p["ts"]) for p in s["prompts"]],
        )
        rows: list[dict] = []
        for model_name, e in s["models"].items():
            tot, root = e["total"], e["root"]
            rows.append(
                {
                    "root_id": sid,
                    "model_name": model_name,
                    "runs": tot["runs"],
                    "cost": tot["cost"],
                    "root_cost": root["cost"],
                    "tokens_total": tot["tokens_total"],
                    "input": tot["input"],
                    "reasoning": tot["reasoning"],
                    "cache_read": tot["cache_read"],
                    "cache_write": tot["cache_write"],
                    "cache_write_1h": tot["cache_write_1h"],
                    "output": tot["output"],
                    "unpriced_input": tot["u_input"],
                    "unpriced_reasoning": tot["u_reasoning"],
                    "unpriced_cache_read": tot["u_cache_read"],
                    "unpriced_cache_write": tot["u_cache_write"],
                    "unpriced_cache_write_1h": tot["u_cache_write_1h"],
                    "unpriced_output": tot["u_output"],
                    "root_unpriced_input": root["u_input"],
                    "root_unpriced_reasoning": root["u_reasoning"],
                    "root_unpriced_cache_read": root["u_cache_read"],
                    "root_unpriced_cache_write": root["u_cache_write"],
                    "root_unpriced_cache_write_1h": root["u_cache_write_1h"],
                    "root_unpriced_output": root["u_output"],
                }
            )
        s["model_rows"] = rows
        s["unpriced_tokens"] = sum(
            r["unpriced_input"]
            + r["unpriced_output"]
            + r["unpriced_reasoning"]
            + r["unpriced_cache_read"]
            + r["unpriced_cache_write"]
            for r in rows
        )
        s["subagents"] = self._build_subagents(s)

    def _build_subagents(self, s: dict) -> list[dict]:
        """Build depth-1 nodes from Bahulam sub-agent completion events."""
        nodes = []
        for idx, run in enumerate(s["subagent_runs"], start=1):
            acc = run["acc"]
            if acc["tokens_total"] <= 0 and acc["cost"] <= 0:
                continue
            nodes.append(
                self._node(
                    str(run.get("id") or f"subagent-{idx}"),
                    1,
                    str(run.get("agent") or "subagent"),
                    str(run.get("title") or "sub-agent run"),
                    iso_to_local(run.get("ts")) if run.get("ts") else s["created_at"],
                    str(run.get("model_name") or "unknown"),
                    acc["cost"],
                    acc,
                )
            )
        return nodes

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
        """Build a node dict for the UI graph from a session and its accumulator."""
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
            "tokens_cache_write_1h": acc["cache_write_1h"],
            "tokens_total": acc["tokens_total"],
        }

    def _nodes(self, workflow_id: str, s: dict) -> list[dict]:
        """Build the node list including root and all subagents for a session."""
        root_tot = self._new_acc()
        best, best_runs = "unknown (not recorded)", -1
        for model_name, e in s["models"].items():
            r = e["root"]
            for k in root_tot:
                root_tot[k] += r[k]
            if r["runs"] > best_runs:
                best_runs, best = r["runs"], model_name
        nodes = [
            self._node(
                workflow_id, 0, "-", s["title"], s["created_at"], best, root_tot["cost"], root_tot
            )
        ]
        nodes.extend(dict(n) for n in s["subagents"])
        if self.demo:
            nodes = [self._demo_node(n) for n in nodes]
        return nodes

    @staticmethod
    def sort_workflows(rows: list[Workflow]) -> list[Workflow]:
        """Stable two-pass sort: alpha by id, then descending by cost then tokens."""
        rows = sorted(rows, key=lambda w: w.id)
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return rows

    def _workflow_rows(self, sessions: dict[str, dict]) -> list[Workflow]:
        """Convert session dicts into a sorted list of ``Workflow`` rows.

        Hosted sessions contribute their `credits_charged` sum as the user-
        facing cost; BYOK/unknown sessions contribute the model_rows cost
        (which is the provider cost). Both branches produce a single
        `total_cost` column so the UI can display them uniformly.
        """
        rows = []
        for sid, s in sessions.items():
            model_rows = s["model_rows"]
            model_cost = sum(r["cost"] for r in model_rows)
            root_cost = sum(r["root_cost"] for r in model_rows)
            total_cost = model_cost + float(s.get("credits_charged", 0.0))
            rows.append(
                Workflow(
                    id=sid,
                    title=s["title"],
                    directory=s["directory"],
                    created_at=s["created_at"],
                    root_cost=root_cost,
                    total_cost=total_cost,
                    subagents=len(s["subagents"]),
                    model_count=0,
                    total_tokens=sum(r["tokens_total"] for r in model_rows),
                    unpriced_tokens=s["unpriced_tokens"],
                    source=self.source_name,
                    ended_at=s["ended_at"],
                    worked_seconds=s["worked_seconds"],
                )
            )
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        return self.sort_workflows(rows)

    def _demo_workflow(self, w: Workflow) -> Workflow:
        """Anonymise a workflow row for demo mode."""
        return scramble_workflow(w, self.demo_scale, self.demo_cats)

    def _demo_node(self, n: dict) -> dict:
        """Anonymise a node dict for demo mode."""
        return scramble_node(n, self.demo_scale, self.demo_cats)

    # ── public interface ──────────────────────────────────────────────────

    def workflows(self) -> list[Workflow]:
        """Return all sessions as a sorted ``Workflow`` list.

        Clears any cached parse so the returned data is fresh.
        """
        self._sessions = None
        self._one = None
        return self._workflow_rows(self._parse())

    def model_breakdown(self) -> list[dict]:
        """Return a flat list of per-model rows across all sessions."""
        return self._model_rows(self._parse())

    @staticmethod
    def _model_rows(sessions: dict[str, dict]) -> list[dict]:
        """Flatten all per-session ``model_rows`` into a single list."""
        out: list[dict] = []
        for s in sessions.values():
            out.extend(s["model_rows"])
        return out

    def summary(self, workflows: list[Workflow]) -> dict[str, int | float]:
        """Aggregate totals across all *workflows* for the summary banner."""
        return {
            "workflows": len(workflows),
            "cost": sum(w.total_cost for w in workflows),
            "tokens": sum(w.total_tokens for w in workflows),
            "subagents": sum(w.subagents for w in workflows),
            "unpriced_tokens": sum(w.unpriced_tokens for w in workflows),
            "paid_workflows": sum(1 for w in workflows if w.total_cost > 0),
        }

    def workflow_nodes(self, workflow_id: str) -> list[dict]:
        """Return the graph nodes for a session, loading it on demand."""
        s = self._session(workflow_id)
        if not s:
            return []
        return self._nodes(workflow_id, s)

    def status_nodes(self, workflow_id: str) -> list[dict]:
        """Return graph nodes for a session without falling back to a full parse.

        Used by status/info commands that should stay fast.
        """
        s = self._session(workflow_id, fallback=False)
        if not s:
            return []
        return self._nodes(workflow_id, s)

    def message_timeline(self, workflow_id: str) -> list[dict]:
        """Return interleaved turn+prompt rows for the timeline view.

        Each row carries ``prompt_id``, ``prompt_title``, and ``prompt_full``
        representing the prompt active at that point in the conversation.
        """
        s = self._session(workflow_id)
        if not s:
            return []
        prompts = sorted(s["prompts"], key=lambda p: p["ts"])
        out = []
        pi, cur_title, cur_full = 0, "", ""
        for t in sorted(s["turns"], key=lambda r: r["ts"]):
            while pi < len(prompts) and prompts[pi]["ts"] <= t["ts"]:
                cur_full = prompts[pi]["title"]
                cur_title = _clean_prompt(cur_full)
                pi += 1
            r = dict(t)
            r["time"] = iso_to_local(r.pop("ts"))
            r["prompt_id"] = f"p{pi}"
            r["prompt_title"] = cur_title
            r["prompt_full"] = cur_full
            out.append(r)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        """Indicate whether turn detail is available (always ``True``)."""
        return True

    def supports_context_curve(self, workflow_id: str) -> bool:
        """Bahulam ``complete`` events aggregate multiple internal LLM calls
        (and often sub-agent rollups) into a single turn row. The
        ``tokens_total`` on a turn is therefore not a per-request context
        window snapshot, and rendering it as a context curve would mislead.
        Opt out globally until Bahulam ships a per-request telemetry stream.
        """
        return False

    def tool_breakdown(self, workflow_id: str) -> list[dict]:
        """Return tool call rows for the tools tab."""
        s = self._session(workflow_id)
        if not s:
            return []
        return tool_rows_from_turns(s["turns"])

    def supports_tools(self, workflow_id: str) -> bool:
        """Indicate whether tool data is available (always ``True``)."""
        return True

    def recent_roots(self) -> list[dict]:
        """Return recently modified session roots for the project picker.

        Each root is a ``_TranscriptRoot`` dict that lazily resolves its
        ``directory`` key from the file header.
        """
        newest: dict[str, _TranscriptRoot] = {}
        for path in self._files():
            sid = os.path.splitext(os.path.basename(path))[0]
            try:
                last_active = int(os.stat(path).st_mtime * 1000)
            except OSError:
                continue
            if sid not in newest or last_active > newest[sid]["last_active"]:
                newest[sid] = _TranscriptRoot(self, path, sid, last_active)
        return sorted(newest.values(), key=lambda r: r["last_active"], reverse=True)

    _CWD_HEAD_BYTES = 262144

    def _transcript_cwd(self, path: str) -> str:
        """Read the first ``cwd`` field from a JSONL file without parsing it fully."""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                remaining = self._CWD_HEAD_BYTES
                while remaining > 0:
                    line = fh.readline()
                    if not line:
                        break
                    remaining -= len(line)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(obj, dict):
                        continue
                    # Top-level cwd is on every record
                    cwd = obj.get("cwd")
                    if cwd:
                        return cwd
        except OSError:
            pass
        return "(unknown)"

    def root_of(self, session_id: str) -> str | None:
        """Return the session ID if a file for that ID exists, otherwise ``None``."""
        for path in self._files():
            if os.path.splitext(os.path.basename(path))[0] == session_id:
                return session_id
        return None

    def cache_provenance(self) -> dict[str, list[str]]:
        """Return a ``session_id -> [file paths]`` map for cache-backed sessions."""
        if not self._sessions:
            return {}
        return {sid: sorted(s["files"]) for sid, s in self._sessions.items()}

    def parse_subset(self, paths: list[str]) -> tuple[list[Workflow], list[dict], dict] | None:
        """Reparse a specific set of files and return (workflows, model-rows, provenance).

        Returns ``None`` if any file in *paths* is missing.
        """
        wanted = set(paths)
        files = [p for p in self._files() if p in wanted]
        if len(files) != len(wanted):
            return None
        self._one = None
        self._sessions = None
        read: list = []

        def tally(stream):
            for path, text in stream:
                read.append(path)
                yield path, text

        sessions = self._parse_texts(tally(read_files_parallel(files)))
        if len(read) != len(files):
            return None
        return (
            self._workflow_rows(sessions),
            self._model_rows(sessions),
            {sid: sorted(s["files"]) for sid, s in sessions.items()},
        )


class _TranscriptRoot(dict):
    """A ``recent_roots()`` row that reads ``directory`` lazily from the file head."""

    def __init__(self, store: BahulamStore, path: str, sid: str, last_active: int):
        """Lazy root with id, mtime, and deferred directory resolution."""
        super().__init__(id=sid, last_active=last_active)
        self._store = store
        self._path = path

    def __getitem__(self, key):
        """Resolve ``directory`` on first access by reading the file header."""
        if key == "directory" and "directory" not in self:
            self["directory"] = self._store._transcript_cwd(self._path)
        return super().__getitem__(key)


def _read_text(path: str) -> str:
    """Read a file's text content, returning ``""`` on any I/O error."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""
