"""pi-agent JSONL backend."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re

from opentab.demo import demo_config, scramble_node, scramble_workflow
from opentab.formatting import _clean_prompt, iso_to_epoch, iso_to_local, worked_seconds
from opentab.models import Workflow
from opentab.util import (
    TRACE_OUTPUT_CAP,
    TRACE_TEXT_CAP,
    LazyStatusRoot,
    TraceContent,
    git_root,
    read_files_parallel,
    safe_float,
    safe_int,
    tool_rows_from_turns,
)


class PiStore:
    """Read pi-agent NDJSON sessions.

    Input is already uncached; cache reads/writes are separate and reasoning is included
    in output. Stable assistant ids deduplicate resumed files. pi records list-price cost
    for every route, so OAuth/plan usage remains unpriced while metered-route cost counts
    as spend. ``auth.json`` is read only for provider authentication type.
    """

    combined = False
    records_reasoning = True
    source_name = "Pi"

    def __init__(self, root_dir: str, args: argparse.Namespace):
        self.root_dir = root_dir
        self.args = args
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._sessions: dict[str, dict] | None = None
        self._git_root_cache: dict[str, str] = {}
        self._oauth_providers = self._load_oauth_providers()
        self._records_cost: bool | None = None  # resolved lazily (records_cost property)

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

    def _auth_paths(self) -> list[str]:
        # A list-shaped seam lets OmpStore include its SQLite WAL.
        return [os.path.join(os.path.dirname(os.path.normpath(self.root_dir)), "auth.json")]

    def _load_oauth_providers(self) -> set[str]:
        # Read only provider auth types; credential values are irrelevant.
        path = self._auth_paths()[0]
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
        # util.safe_int is the one rule every backend coerces usage through -- see there
        # for the three ways an untrusted number takes a whole backend down.
        return safe_int(value)

    @staticmethod
    def _cost_total(usage: dict) -> float:
        cost = usage.get("cost")
        if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
            return max(0.0, safe_float(cost["total"]))
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
            "sid": None,
            "cwd": None,
            "ts_min": None,
            "ts_max": None,
            "ts_meta": None,  # the `session` record's timestamp, preferred for created_at
            "title_prompt": None,
            "models": {},
            "seen_msgs": set(),  # assistant ids already counted (resume/fork dedup)
            "turns": [],  # one per assistant message, for the Turns tab
            "prompts": [],  # user messages, for the Turns tab's ▸ grouping
            "paths": [],
        }

    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore) --
        # the transcripts PLUS the auth file, because the oauth/metered split lives
        # there rather than in the JSONL: switch a provider between an API key and a
        # plan login and every cost in the rollup changes (and records_cost with it,
        # which drives the "$"/ESTIMATED framing) while no transcript is touched. Left
        # out, the fingerprint still matches, so the warm start serves the pre-login
        # split and `r` re-fingerprints to the same value -- it never self-corrects.
        # Paths that don't exist are skipped by CachedStore._fingerprint's stat().
        return self._files() + self._auth_paths()

    def _files(self) -> list[str]:
        return glob.glob(os.path.join(self.root_dir, "**", "*.jsonl"), recursive=True)

    def _session_files(self, session_id: str) -> list[str]:
        # A session's file is <timestamp>_<uuid>.jsonl; a resumed session leaves
        # several files with the same uuid, so glob for every copy (the leading *
        # also matches a bare "<uuid>.jsonl" spelling).
        pattern = os.path.join(self.root_dir, "**", "*" + glob.escape(session_id) + ".jsonl")
        return glob.glob(pattern, recursive=True)

    def _head_cwd(self, path: str) -> str:
        # The `session` record at the file head carries the cwd; a bounded read
        # (ClaudeStore's _transcript_cwd pattern) so a recent_roots scan stops
        # paying at the row that matches.
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                remaining = 65536
                while remaining > 0:
                    line = fh.readline()
                    if not line:
                        break
                    remaining -= len(line)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(o, dict) and o.get("type") == "session" and o.get("cwd"):
                        return o["cwd"]
        except OSError:
            pass
        return "(unknown)"

    def recent_roots(self) -> list[dict]:
        # Root sessions newest-activity-first, the cheap sibling of
        # Store.recent_roots for the one-shot --status command. No parse: pi
        # appends every record to the session's own file, so the mtime IS the
        # last activity (a resume writes a new file with the same uuid -- the
        # newest copy wins) and the uuid in the name is the id; "directory" (the
        # session record's cwd) is read lazily from the file head.
        newest: dict[str, tuple[int, str]] = {}
        for path in self._files():
            sid = self._id_from_name(path)
            if not sid:
                continue
            try:
                last_active = int(os.stat(path).st_mtime * 1000)  # ms, like Store's
            except OSError:
                continue  # deleted mid-scan
            prev = newest.get(sid)
            if prev is None or last_active > prev[0]:
                newest[sid] = (last_active, path)
        rows = [
            LazyStatusRoot(
                {"id": sid, "last_active": last_active},
                {"directory": lambda p=path: self._head_cwd(p)},
            )
            for sid, (last_active, path) in newest.items()
        ]
        rows.sort(key=lambda r: r["last_active"], reverse=True)
        return rows

    def root_of(self, session_id: str) -> str | None:
        # A pi session id is already its root (no subagent tree), so this only
        # confirms a file carries the id -- the cheap membership answer the
        # --status backend probe relies on.
        return session_id if self._session_files(session_id) else None

    def status_nodes(self, workflow_id: str) -> list[dict]:
        # workflow_nodes for the --status one-shot: the identical row, but off a
        # parse of just this session's own file(s) when nothing is loaded yet --
        # a status poll must never trigger the full-tree parse.
        if self._sessions is not None:
            return self.workflow_nodes(workflow_id)
        sessions: dict[str, dict] = {}
        for path, text in read_files_parallel(self._session_files(workflow_id)):
            self._parse_file(path, text.split("\n"), sessions)
        s = sessions.get(workflow_id)
        if not s:
            return []
        self._finalize(workflow_id, s)
        if not s["model_rows"]:
            return []
        return self._nodes_from(workflow_id, s)

    @property
    def records_cost(self) -> bool:
        # True iff any *metered* (non-subscription) message records real spend. Lazy so
        # construction never reads the corpus (the warm-start cache answers a hit without
        # reaching here): after a parse it derives from the accumulated per-model costs;
        # the full-file probe runs only when it is read before any parse.
        if self._sessions is not None:
            return any(
                acc["cost"] > 0 for s in self._sessions.values() for acc in s["models"].values()
            )
        if self._records_cost is None:
            self._records_cost = self._probe_records_cost()
        return self._records_cost

    def _probe_records_cost(self) -> bool:
        # True iff any *metered* (non-subscription) assistant message records a positive
        # cost. Early-exits so it stays cheap. A subscription-only setup -> False (every
        # cost is estimated).
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
                    if not isinstance(o, dict):
                        continue  # a valid-JSON non-object (`["cost"]`) has no .get()
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
        s["sid"] = sid
        s["paths"].append(path)
        self._parse_lines(s, lines, os.path.basename(path))

    @staticmethod
    def _content_key(s: dict, prefix: str, ordinal: int, mid) -> str:
        return f"{s.get('sid')}:{mid}" if mid is not None else f"{prefix}:{ordinal}"

    def _parse_lines(self, s: dict, lines: list[str], key_prefix: str = "message") -> None:
        # Factored out of _parse_file so OmpStore can feed it a session dict keyed
        # by content (a subagent transcript's id lives in its own `session` record,
        # never its filename) instead of one keyed by the caller's filename-derived
        # id -- the loop body itself is untouched.
        for ordinal, line in enumerate(lines):
            if '"type"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue  # a valid-JSON non-object (`["type"]`) has no .get()
            typ = o.get("type")
            ts = o.get("timestamp")
            if ts and (s["ts_min"] is None or ts < s["ts_min"]):
                s["ts_min"] = ts
            if ts and (s["ts_max"] is None or ts > s["ts_max"]):
                s["ts_max"] = ts  # ISO strings order lexicographically
            if typ == "session":
                if o.get("cwd") and not s["cwd"]:
                    s["cwd"] = o["cwd"]
                if o.get("timestamp") and not s["ts_meta"]:
                    s["ts_meta"] = o["timestamp"]
                self._extra_record(typ, o, s)
                continue
            if typ != "message":
                self._extra_record(typ, o, s)
                continue
            msg = o.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            mid = o.get("id")
            if role == "user":
                txt = self._user_text(msg.get("content"))
                if txt.strip():
                    if not s["title_prompt"]:
                        s["title_prompt"] = " ".join(txt.split())[:80]
                    if mid is None or mid not in s["seen_msgs"]:
                        if mid is not None:
                            s["seen_msgs"].add(mid)
                        s["prompts"].append(
                            {"ts": ts or "", "id": str(mid or ts or ""), "title": txt.strip()}
                        )
                continue
            if role != "assistant" or not isinstance(msg.get("usage"), dict):
                continue
            if mid is not None:
                if mid in s["seen_msgs"]:
                    continue  # same assistant step in a resumed/forked file
                s["seen_msgs"].add(mid)
            self._apply_usage(s, msg, ts, self._content_key(s, key_prefix, ordinal, mid))

    def _extra_record(self, typ: str, o: dict, s: dict) -> None:
        # Hook for a subclass that reacts to a record type PiStore itself has
        # nothing to do with (e.g. omp's dedicated title/title_change records,
        # or its own `session` record's title). A no-op here, so pi's own parse
        # is unchanged.
        pass

    def _model_label(self, msg: dict) -> str:
        # pi records models already provider-qualified (e.g. "moonshotai/kimi-k2.6"),
        # so the label is just the recorded string. Factored out so a fork writing
        # bare model ids alongside a separate provider field (omp) can qualify the
        # label without duplicating _apply_usage.
        model = msg.get("model")
        return model if isinstance(model, str) and model else "unknown"

    def _apply_usage(self, s: dict, msg: dict, ts=None, content_key: str = "") -> None:
        usage = msg["usage"]
        inp = self._int(usage.get("input"))
        out = self._int(usage.get("output"))
        cr = self._int(usage.get("cacheRead"))
        cw = self._int(usage.get("cacheWrite"))
        total = self._int(usage.get("totalTokens"))
        out += max(0, total - (inp + out + cr + cw))  # only `totalTokens` -> back-fill output
        if inp + out + cr + cw == 0:
            return
        model = self._model_label(msg)
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
        metered = cost > 0 and not self._is_subscription(msg.get("provider"), msg.get("api"))
        if metered:
            acc["cost"] += cost  # metered route with real spend -> tokens stay priced
        else:
            # Subscription/plan route (its cost is a list-price estimate, not spend) OR no
            # recorded cost at all -> mark these tokens unpriced so the "$" view estimates them.
            acc["u_input"] += inp
            acc["u_output"] += out
            acc["u_cache_read"] += cr
            acc["u_cache_write"] += cw
        # One Turns row per assistant message. A subscription turn's cost is $0 (its
        # recorded figure is a list-price estimate, not spend) so the tab's "$" view
        # reprices it from the token columns, exactly like the session rollups.
        # The toolCall blocks this step invoked feed tool_breakdown (duplicates
        # kept: two bash calls = two calls, two shares).
        content = msg.get("content")
        parts = content if isinstance(content, list) else []
        tools = [
            c.get("name")
            for c in parts
            if isinstance(c, dict) and c.get("type") == "toolCall" and c.get("name")
        ]
        s["turns"].append(
            {
                "ts": ts or "",
                "depth": 0,  # pi has no subagent tree
                "agent": "-",
                # The reasoning effort in force for this call. pi records none, so this
                # stays "" and the column simply doesn't appear; omp writes
                # `thinking_level_change` records and keeps this current from
                # _extra_record, which is why the field is read off the session rather
                # than the message.
                "effort": s.get("effort") or "",
                "model_name": model,
                "cost": round(cost, 6) if metered else 0.0,
                "input": inp,
                "output": out,
                "reasoning": 0,
                "cache_read": cr,
                "cache_write": cw,
                "tokens_total": inp + out + cr + cw,
                "tools": tools,
                "content_key": content_key,
                "has_text": any(
                    isinstance(p, dict)
                    and p.get("type") == "text"
                    and isinstance(p.get("text"), str)
                    and bool(p["text"].strip())
                    for p in parts
                ),
                "has_reasoning": any(
                    isinstance(p, dict)
                    and p.get("type") == "thinking"
                    and isinstance(p.get("thinking"), str)
                    and bool(p["thinking"].strip())
                    for p in parts
                ),
            }
        )

    @staticmethod
    def _trace_result(content) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return "" if content is None else str(content)
        parts = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
            elif item.get("type") == "image":
                parts.append("(image)")
            elif isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)

    def _trace_sessions(self, workflow_id: str) -> list[tuple[str, dict]]:
        session = self._parse().get(workflow_id)
        return [(workflow_id, session)] if session else []

    def _trace_lines(
        self,
        sid: str,
        path: str,
        lines: list[str],
        trace: TraceContent,
        seen: set,
        calls: dict[tuple[str, str], dict],
    ) -> None:
        session = {"sid": sid}
        prefix = os.path.basename(path)
        for ordinal, line in enumerate(lines):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            msg = o.get("message") if isinstance(o, dict) and o.get("type") == "message" else None
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            mid = o.get("id")
            if role == "user":
                if self._user_text(msg.get("content")).strip() and mid is not None:
                    seen.add((sid, mid))
                continue
            if role == "toolResult":
                event = calls.pop((sid, str(msg.get("toolCallId") or "")), None)
                if event is not None:
                    output, dropped = trace.clip(
                        self._trace_result(msg.get("content")), TRACE_OUTPUT_CAP
                    )
                    event["output"] = output
                    event["output_dropped"] = dropped
                    event["status"] = "error" if msg.get("isError") else "completed"
                continue
            usage = msg.get("usage")
            if role != "assistant" or not isinstance(usage, dict):
                continue
            if mid is not None:
                marker = (sid, mid)
                if marker in seen:
                    continue
                seen.add(marker)
            total = sum(
                self._int(usage.get(k)) for k in ("input", "output", "cacheRead", "cacheWrite")
            )
            total = max(total, self._int(usage.get("totalTokens")))
            if total == 0:
                continue
            key = self._content_key(session, prefix, ordinal, mid)
            if not trace.accepts(key):
                for block in msg.get("content") if isinstance(msg.get("content"), list) else []:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        calls.pop((sid, str(block.get("id") or "")), None)
                continue
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            events: list[dict] = []
            for block in blocks:
                if not isinstance(block, dict) or len(events) >= trace.event_limit:
                    continue
                kind = block.get("type")
                if kind in ("text", "thinking"):
                    field = "text" if kind == "text" else "thinking"
                    value, dropped = trace.clip(block.get(field), TRACE_TEXT_CAP)
                    if value:
                        events.append(
                            {
                                "kind": "text" if kind == "text" else "reasoning",
                                "text": value,
                                "dropped": dropped,
                            }
                        )
                elif kind == "toolCall":
                    head, params = trace.arguments(block.get("arguments"))
                    event = {
                        "kind": "tool",
                        "name": block.get("name") or "(unknown)",
                        "args": head,
                        "params": params,
                        "output": "",
                        "output_dropped": 0,
                    }
                    events.append(event)
                    if block.get("id"):
                        calls[(sid, str(block["id"]))] = event
            if events:
                trace[key] = events

    def turn_content(
        self, workflow_id: str, content_key: str | None = None
    ) -> dict[str, list[dict]]:
        trace = TraceContent(content_key)
        seen = set()
        calls: dict[tuple[str, str], dict] = {}
        for sid, session in self._trace_sessions(workflow_id):
            for path, text in read_files_parallel(session.get("paths") or []):
                self._trace_lines(sid, path, text.split("\n"), trace, seen, calls)
        return trace

    def supports_turn_content(self, workflow_id: str) -> bool:
        return bool(self._trace_sessions(workflow_id))

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title_prompt"] or "(untitled)"
        s["directory"] = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"
        stamp = s["ts_meta"] or s["ts_min"]
        s["created_at"] = iso_to_local(stamp) if stamp else ""
        s["ended_at"] = iso_to_local(s["ts_max"]) if s["ts_max"] else ""
        # Active working time: assistant turns + user prompts are the activity points;
        # the user prompts mark the idle gaps (you composing the next message).
        prompt_epochs = [iso_to_epoch(p["ts"]) for p in s["prompts"]]
        s["worked_seconds"] = worked_seconds(
            [iso_to_epoch(r["ts"]) for r in s["turns"]] + prompt_epochs,
            prompt_epochs,
        )
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

    def workflows(self) -> list[Workflow]:
        self._sessions = None  # reload (r) re-reads fresh; model methods reuse cache
        # Re-read the login state too: `r` exists to pick up changes, and a provider
        # that switched to an oauth plan since launch must stop counting as spend.
        self._oauth_providers = self._load_oauth_providers()
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
                    ended_at=s["ended_at"],
                    worked_seconds=s["worked_seconds"],
                )
            )
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return rows

    def _demo_workflow(self, w: Workflow) -> Workflow:
        # Mirror CsvStore._demo_workflow: anonymize, backfill a synthetic price for any
        # unpriced tokens, then scale by the hidden per-process factor.
        return scramble_workflow(w, self.demo_scale, self.demo_cats)

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
        return self._nodes_from(workflow_id, s)

    def _nodes_from(self, workflow_id: str, s: dict) -> list[dict]:
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
        return scramble_node(n, self.demo_scale, self.demo_cats)

    def message_timeline(self, workflow_id: str) -> list[dict]:
        # Chronological per-turn rows for the Turns tab (the ClaudeStore pattern):
        # ISO timestamps sort lexicographically, and walking the two time-sorted
        # streams in lockstep tags each turn with the latest prompt at ts <= the
        # turn's ts. Real rows -- App._scale_demo_turns hides magnitudes in demo.
        s = self._parse().get(workflow_id)
        if not s:
            return []
        prompts = sorted(s["prompts"], key=lambda p: p["ts"])
        out = []
        pi_, cur_id, cur_title, cur_full = 0, "", "", ""
        for t in sorted(s["turns"], key=lambda r: r["ts"]):
            while pi_ < len(prompts) and prompts[pi_]["ts"] <= t["ts"]:
                cur_id, cur_full = prompts[pi_]["id"], prompts[pi_]["title"]
                cur_title = _clean_prompt(cur_full)
                pi_ += 1
            r = dict(t)
            r["time"] = iso_to_local(r.pop("ts"))
            r["prompt_id"] = cur_id
            r["prompt_title"] = cur_title
            r["prompt_full"] = cur_full
            out.append(r)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        return True

    def tool_breakdown(self, workflow_id: str) -> list[dict]:
        # Per-(tool, model) token attribution for the Tools tab: each assistant
        # message is one LLM step whose tokens (and, on a metered route, real cost)
        # are split evenly across its toolCall blocks -- the Store.tool_breakdown
        # semantics off the in-memory turn rows. A subscription row stays $0 so the
        # "$" view reprices it per (tool, model).
        s = self._parse().get(workflow_id)
        return tool_rows_from_turns(s["turns"]) if s else []

    def supports_tools(self, workflow_id: str) -> bool:
        # pi records every step's toolCall blocks, so the tab applies to every
        # session; one without tool calls shows the honest empty message.
        return True
