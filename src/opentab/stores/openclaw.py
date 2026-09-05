"""OpenClaw gateway JSONL backend."""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import datetime, timezone

from opentab.demo import demo_config, scramble_node, scramble_workflow
from opentab.formatting import _clean_prompt, worked_seconds
from opentab.models import Workflow
from opentab.util import (
    TRACE_OUTPUT_CAP,
    TRACE_TEXT_CAP,
    TraceContent,
    read_files_parallel,
    safe_float,
    safe_int,
    tool_rows_from_turns,
)


class OpenClawStore:
    """Read OpenClaw gateway NDJSON sessions.

    Assistant-message ids deduplicate live and archived files; the parallel trace schema
    is ignored. Input is already uncached and cache tokens are separate. OpenClaw records
    list-price cost for all routes, so OAuth/plan usage remains unpriced while metered
    usage counts as spend. ``openclaw.json`` is read only for auth mode and provider.
    """

    combined = False
    records_reasoning = True
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
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._sessions: dict[str, dict] | None = None
        self._oauth_providers = self._load_oauth_providers()
        self._records_cost: bool | None = None  # resolved lazily (records_cost property)

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

    def _auth_path(self) -> str:
        # The file that decides the oauth-vs-metered split. cache_inputs() fingerprints
        # it alongside the transcripts (see there for why).
        return os.path.join(self.root_dir, "openclaw.json")

    def _load_oauth_providers(self) -> set[str]:
        path = self._auth_path()
        out: set[str] = set()
        try:
            with open(path, encoding="utf-8") as fh:
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
            "ts_max": None,  # latest record (epoch seconds)
            "ts_meta": None,  # the `session` record's timestamp, preferred for created_at
            "title_prompt": None,
            "models": {},
            "seen_msgs": set(),  # record ids already counted (resume/archive dedup)
            "turns": [],  # one per assistant message, for the Turns tab
            "prompts": [],  # user messages, for the Turns tab's ▸ grouping
            "paths": [],
        }

    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore) --
        # the transcripts PLUS openclaw.json, because the oauth/metered split lives
        # there rather than in the JSONL: switch a profile between an API key and a
        # plan login and every cost in the rollup changes (and records_cost with it,
        # which drives the "$"/ESTIMATED framing) while no transcript is touched. Left
        # out, the fingerprint still matches, so the warm start serves the pre-login
        # split and `r` re-fingerprints to the same value -- it never self-corrects.
        # Paths that don't exist are skipped by CachedStore._fingerprint's stat().
        return self._files() + [self._auth_path()]

    def _files(self) -> list[str]:
        out = []
        for path in glob.glob(os.path.join(self.root_dir, "agents", "*", "sessions", "*")):
            if os.path.isfile(path) and self._is_session_file(os.path.basename(path)):
                out.append(path)
        return out

    def _session_files(self, session_id: str) -> list[str]:
        # A session's live file plus its .reset./.deleted. archives, under whichever
        # agent owns it -- the id is the filename stem before ".jsonl".
        out = []
        pattern = os.path.join(
            self.root_dir, "agents", "*", "sessions", glob.escape(session_id) + ".jsonl*"
        )
        for path in glob.glob(pattern):
            if os.path.isfile(path) and self._is_session_file(os.path.basename(path)):
                out.append(path)
        return out

    def recent_roots(self) -> list[dict]:
        # Root sessions newest-activity-first, the cheap sibling of
        # Store.recent_roots for the one-shot --status command (file mtime = last
        # activity; archives count, collapsed onto their session id). "directory"
        # is the agent's ABSOLUTE directory, not the TUI's bare agent name: the
        # --status caller folds directories through git_root, and a bare name like
        # "finance-os" would resolve against the caller's own cwd. OpenClaw
        # sessions carry no user cwd, so a directory target matches only a pane
        # actually inside agents/<agent>; session-id targets are the reliable route.
        newest: dict[str, dict] = {}
        for path in self._files():
            sid = self._session_id(path)
            try:
                last_active = int(os.stat(path).st_mtime * 1000)  # ms, like Store's
            except OSError:
                continue  # deleted mid-scan
            row = newest.get(sid)
            if row is None or last_active > row["last_active"]:
                newest[sid] = {
                    "id": sid,
                    "last_active": last_active,
                    "directory": os.path.dirname(os.path.dirname(path)),  # .../agents/<agent>
                }
        return sorted(newest.values(), key=lambda r: r["last_active"], reverse=True)

    def root_of(self, session_id: str) -> str | None:
        # An OpenClaw session id is already its root (no subagent tree), so this
        # only confirms a session file carries the id -- the cheap membership
        # answer the --status backend probe relies on.
        return session_id if self._session_files(session_id) else None

    def status_nodes(self, workflow_id: str) -> list[dict]:
        # workflow_nodes for the --status one-shot: the identical row, but off a
        # parse of just this session's own live + archived files when nothing is
        # loaded yet (deduped by record id, as always) -- a status poll must never
        # trigger the full-tree parse.
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
                    if not isinstance(o, dict):
                        continue  # a valid-JSON non-object (`["cost"]`) has no .get()
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
        if path not in s["paths"]:
            s["paths"].append(path)
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
            if ts is not None and (s["ts_max"] is None or ts > s["ts_max"]):
                s["ts_max"] = ts
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
            if mts is not None and (s["ts_max"] is None or mts > s["ts_max"]):
                s["ts_max"] = mts
            role = msg.get("role")
            mid = o.get("id") or msg.get("idempotencyKey")
            rts = mts if mts is not None else ts
            if role == "user":
                txt = self._user_text(msg.get("content"))
                if txt.strip():
                    if not s["title_prompt"]:
                        s["title_prompt"] = " ".join(txt.split())[:80]
                    if mid is None or mid not in s["seen_msgs"]:
                        if mid is not None:
                            s["seen_msgs"].add(mid)
                        s["prompts"].append(
                            {"ts": rts or 0.0, "id": str(mid or rts or ""), "title": txt.strip()}
                        )
                continue
            if role != "assistant" or not isinstance(msg.get("usage"), dict):
                continue
            if mid is not None:
                if mid in s["seen_msgs"]:
                    continue  # same assistant step in a resumed/archived file
                s["seen_msgs"].add(mid)
            key = f"{sid}:{mid}" if mid is not None else ""
            self._apply_usage(s, msg, current_model, current_provider, rts, key)

    def _apply_usage(
        self,
        s: dict,
        msg: dict,
        current_model,
        current_provider,
        ts=None,
        content_key: str = "",
    ) -> None:
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
        tools = self._tool_names(msg)
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
        metered = cost > 0 and not self._is_subscription(provider, msg.get("api"))
        if metered:
            acc["cost"] += cost  # metered route with real spend -> tokens stay priced
        else:
            # Subscription/plan route (cost is a list-price estimate, not spend) OR no recorded
            # cost -> mark these tokens unpriced so the "$" view estimates them.
            acc["u_input"] += inp
            acc["u_output"] += out
            acc["u_cache_read"] += cr
            acc["u_cache_write"] += cw
        # One Turns row per assistant message; a subscription turn stays $0 so the
        # tab's "$" view reprices it from the token columns (as the rollups do).
        s["turns"].append(
            {
                "ts": ts or 0.0,  # epoch seconds; sorts numerically
                "depth": 0,  # OpenClaw has no subagent tree
                "agent": "-",
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
                "has_text": self._has_content(msg, "text", "text"),
                "has_reasoning": self._has_content(msg, "thinking", "thinking"),
            }
        )

    @staticmethod
    def _has_content(msg: dict, kind: str, field: str) -> bool:
        content = msg.get("content")
        return isinstance(content, list) and any(
            isinstance(block, dict)
            and block.get("type") == kind
            and isinstance(block.get(field), str)
            and bool(block[field].strip())
            for block in content
        )

    @staticmethod
    def _trace_result(content) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return "" if content is None else str(content)
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
            elif block.get("type") == "image":
                parts.append("(image)")
            elif isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)

    def turn_content(
        self, workflow_id: str, content_key: str | None = None
    ) -> dict[str, list[dict]]:
        session = self._parse().get(workflow_id)
        if not session:
            return {}
        trace = TraceContent(content_key)
        seen = set()
        calls: dict[str, dict] = {}
        for _path, text in read_files_parallel(session.get("paths") or []):
            for line in text.split("\n"):
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                msg = (
                    o.get("message") if isinstance(o, dict) and o.get("type") == "message" else None
                )
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                mid = o.get("id") or msg.get("idempotencyKey")
                if role == "user":
                    if self._user_text(msg.get("content")).strip() and mid is not None:
                        seen.add(mid)
                    continue
                if role == "toolResult":
                    event = calls.pop(str(msg.get("toolCallId") or ""), None)
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
                    if mid in seen:
                        continue
                    seen.add(mid)
                total = sum(
                    self._int(usage.get(k)) for k in ("input", "output", "cacheRead", "cacheWrite")
                )
                total = max(total, self._int(usage.get("totalTokens")))
                if total == 0:
                    continue
                key = f"{workflow_id}:{mid}" if mid is not None else ""
                if not key or not trace.accepts(key):
                    for block in msg.get("content") if isinstance(msg.get("content"), list) else []:
                        if isinstance(block, dict) and block.get("type") == "toolCall":
                            calls.pop(str(block.get("id") or ""), None)
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
                        args = block.get("arguments")
                        if args is None:
                            args = block.get("input")
                        head, params = trace.arguments(args)
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
                            # Reused ids are legal. The newest still-open call owns the
                            # next result; an unmatched older occurrence remains outputless.
                            calls[str(block["id"])] = event
                if events:
                    trace[key] = events
        return trace

    def supports_turn_content(self, workflow_id: str) -> bool:
        return bool((self._parse().get(workflow_id) or {}).get("paths"))

    @staticmethod
    def _tool_names(msg: dict) -> list[str]:
        # The tools this step invoked, in call order with duplicates kept (two `bash`
        # calls = two calls, two shares), read off the assistant message's `toolCall`
        # content blocks. Verified against a real corpus: 4,106 such blocks over 6,489
        # assistant messages, in three key shapes -- {arguments,id,name,partialJson},
        # {arguments,id,input,name} and {arguments,id,name} -- so `name` is the one
        # field always present, and the only one read here.
        #
        # OpenClaw also writes a parallel TRACE schema in separate files whose records
        # carry no `type:"message"`; those never reach this method, so a tool cannot be
        # counted twice. Non-string names are dropped rather than trusted: the whole
        # `tools` field is gated by util.tool_names downstream, and a malformed entry
        # reaching a dict key would raise in the middle of a paint.
        content = msg.get("content")
        if not isinstance(content, list):
            return []
        return [
            b["name"]
            for b in content
            if isinstance(b, dict)
            and b.get("type") == "toolCall"
            and isinstance(b.get("name"), str)
            and b["name"]
        ]

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title_prompt"] or "(untitled)"
        s["directory"] = s["agent"] or "(unknown)"
        s["created_at"] = self._fmt_epoch(s["ts_meta"] or s["ts_min"])
        s["ended_at"] = self._fmt_epoch(s["ts_max"]) if s["ts_max"] is not None else ""
        # Active working time: assistant turns + user prompts are the activity points
        # (epoch seconds already); the user prompts mark the idle waits. A missing 0.0
        # stamp is treated as unknown, not the 1970 epoch.
        prompt_epochs = [p["ts"] or None for p in s["prompts"]]
        s["worked_seconds"] = worked_seconds(
            [r["ts"] or None for r in s["turns"]] + prompt_epochs,
            prompt_epochs,
        )
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

    def workflows(self) -> list[Workflow]:
        self._sessions = None  # reload (r) re-reads fresh; model methods reuse cache
        # Re-read the login state too: `r` exists to pick up changes, and a profile
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
                    subagents=0,  # OpenClaw has no subagent tree
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
        # Mirror PiStore._demo_workflow: anonymize, backfill a synthetic price for any
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
        # Chronological per-turn rows for the Turns tab (the ClaudeStore pattern,
        # on epoch-seconds timestamps): walking the two time-sorted streams in
        # lockstep tags each turn with the latest prompt at ts <= the turn's ts.
        # Real rows -- App._scale_demo_turns hides magnitudes in demo.
        s = self._parse().get(workflow_id)
        if not s:
            return []
        prompts = sorted(s["prompts"], key=lambda p: p["ts"])
        out = []
        pi, cur_id, cur_title, cur_full = 0, "", "", ""
        for t in sorted(s["turns"], key=lambda r: r["ts"]):
            while pi < len(prompts) and prompts[pi]["ts"] <= t["ts"]:
                cur_id, cur_full = prompts[pi]["id"], prompts[pi]["title"]
                cur_title = _clean_prompt(cur_full)
                pi += 1
            r = dict(t)
            r["time"] = self._fmt_epoch(r.pop("ts") or None)
            r["prompt_id"] = cur_id
            r["prompt_title"] = cur_title
            r["prompt_full"] = cur_full
            out.append(r)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        return True

    def tool_breakdown(self, workflow_id: str) -> list[dict]:
        # Per-(tool, model) token attribution for the Tools tab, off the in-memory turn
        # rows (the pi/zaly shape): each assistant message is one LLM step whose tokens
        # -- and, on a metered route, its real cost -- split evenly across the toolCall
        # blocks it made. A subscription step stays $0 so the "$" view reprices it per
        # (tool, model), which is what every OpenClaw session is in practice.
        s = self._parse().get(workflow_id)
        return tool_rows_from_turns(s["turns"]) if s else []

    def supports_tools(self, workflow_id: str) -> bool:
        # OpenClaw records every step's toolCall blocks, so the tab applies to every
        # session; one that never called a tool shows the honest empty message rather
        # than being hidden -- the pi rule.
        return True
