"""Claude Code JSONL transcript backend."""
from __future__ import annotations

import argparse
import glob
import json
import os
import random

from opentab.demo import demo_cost, demo_dir, demo_model, demo_title
from opentab.formatting import _clean_prompt, iso_to_local
from opentab.models import Workflow
from opentab.pricing import api_equivalent_cost
from opentab.util import git_root, read_files_parallel


class ClaudeStore:
    """Read Claude Code transcripts (~/.claude/projects/**/*.jsonl) behind the same
    interface App expects from Store: workflows(), summary(), workflow_nodes(),
    model_breakdown(), plus the .demo/.demo_scale attributes.

    Claude Code records no per-message dollar cost -- only token usage. So a Claude
    session is exactly an OpenCode *subscription* session: every message has a real
    recorded cost of $0, and its tokens are reported as "unpriced". That lets the
    normal real-vs-"$" machinery work unchanged -- normal mode shows $0, and "$"
    (what-if) reprices the tokens at API list rates via the same _compute_api_costs /
    api_equivalent_cost path Store uses. records_cost = False just drives a header
    hint that $0 means "not recorded, press $".

    A "workflow" is one sessionId; depth-0 is its main thread (isSidechain == False)
    and each group of sidechain messages (Task subagents) becomes a depth-1 node, so
    the subagent tree and root/total cost split mirror Store's recursive CTE.
    """

    records_cost = False  # cost is $0 until "$" reprices the (all-unpriced) tokens
    combined = False
    source_name = "Claude Code"

    def __init__(self, root_dir: str, args: argparse.Namespace):
        self.root_dir = root_dir
        self.args = args
        self.demo = getattr(args, "demo", False)
        # Same hidden per-process factor Store uses so a demo screenshot can't be
        # multiplied back into real spend; 1.0 outside demo. See Store.__init__.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._sessions: dict[str, dict] | None = None  # parsed lazily / on reload
        self._git_root_cache: dict[str, str] = {}

    # --- token accumulation helpers ------------------------------------------
    @staticmethod
    def _new_acc() -> dict[str, int]:
        return {
            "runs": 0,
            "input": 0,
            "output": 0,
            "reasoning": 0,  # thinking tokens are already counted in output_tokens
            "cache_read": 0,
            "cache_write": 0,
            "tokens_total": 0,
        }

    @staticmethod
    def _add_usage(acc: dict[str, int], u: dict) -> None:
        i = int(u.get("input_tokens", 0) or 0)
        o = int(u.get("output_tokens", 0) or 0)
        cr = int(u.get("cache_read_input_tokens", 0) or 0)
        cw = int(u.get("cache_creation_input_tokens", 0) or 0)  # cache creation == write
        acc["runs"] += 1
        acc["input"] += i
        acc["output"] += o
        acc["cache_read"] += cr
        acc["cache_write"] += cw
        acc["tokens_total"] += i + o + cr + cw

    @staticmethod
    def _price(model_name: str, acc: dict[str, int]) -> float:
        return api_equivalent_cost(
            model_name,
            acc["input"],
            acc["output"],
            acc["reasoning"],
            acc["cache_read"],
            acc["cache_write"],
        )

    def _git_root(self, cwd: str) -> str:
        if cwd not in self._git_root_cache:
            self._git_root_cache[cwd] = git_root(cwd)
        return self._git_root_cache[cwd]

    # Claude Code injects its own "user" messages around slash commands and hooks
    # (the local-command caveat, the <command-name> wrapper, bash stdout, system
    # reminders). They're scaffolding, not the user's prompt, so they must never
    # become a session title -- skipping them lets the first *real* prompt win.
    _WRAPPER_TAGS = (
        "<local-command",
        "<command-name",
        "<command-message",
        "<command-args",
        "<command-stdout",
        "<command-contents",
        "<system-reminder",
        "<bash-input",
        "<bash-stdout",
        "<bash-stderr",
        "<user-memory-input",
    )

    @classmethod
    def _prompt_text(cls, message) -> str | None:
        # First *real* user prompt, as a last-resort session title when no aiTitle
        # exists. Returns None for empty content or an injected wrapper (see above),
        # so the caller keeps scanning to the next user message.
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        text = None
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    break
                if isinstance(block, str):
                    text = block.strip()
                    break
        if not text or text.startswith(cls._WRAPPER_TAGS):
            return None
        return text[:80]

    # --- parsing -------------------------------------------------------------
    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore).
        return self._files()

    def _files(self) -> list[str]:
        return glob.glob(os.path.join(self.root_dir, "**", "*.jsonl"), recursive=True)

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        seen: set = set()  # dedupe resumed/forked overlap on (message.id, requestId)
        for _path, text in read_files_parallel(self._files()):
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                self._ingest(obj, sessions, seen)
        for sid, s in sessions.items():
            self._finalize(sid, s)
        self._sessions = sessions
        return sessions

    def _ingest(self, o: dict, sessions: dict[str, dict], seen: set) -> None:
        sid = o.get("sessionId")
        if not sid:
            return
        s = sessions.get(sid)
        if s is None:
            s = sessions[sid] = {
                "cwd": None,
                "ts_min": None,
                "title_ai": None,
                "title_custom": None,
                "title_prompt": None,
                "models": {},  # model_name -> {"total": acc, "root": acc}
                "uuid_parent": {},  # uuid -> parentUuid (for grouping sidechains)
                "side_uuids": set(),  # uuids flagged isSidechain
                "side_usage": {},  # sidechain-assistant uuid -> (model_name, acc)
                "turns": [],  # per-message rows for the Turns tab (chronological)
                "prompts": [],  # {ts,title,id} per real user prompt, for Turns grouping
            }
        cwd = o.get("cwd")
        if cwd and not s["cwd"]:
            s["cwd"] = cwd
        ts = o.get("timestamp")
        if ts and (s["ts_min"] is None or ts < s["ts_min"]):
            s["ts_min"] = ts
        uuid = o.get("uuid")
        if uuid:
            s["uuid_parent"][uuid] = o.get("parentUuid")
            if o.get("isSidechain") is True:
                s["side_uuids"].add(uuid)
        typ = o.get("type")
        if typ == "ai-title":
            s["title_ai"] = o.get("aiTitle") or o.get("title") or s["title_ai"]
        elif typ == "custom-title":
            s["title_custom"] = o.get("title") or o.get("customTitle") or s["title_custom"]
        elif typ == "user" and not o.get("isMeta") and o.get("isSidechain") is not True:
            # A real (non-meta, non-sidechain) user prompt -- _prompt_text further
            # skips command/system wrappers and tool-result messages (no text block),
            # so only genuine prompts pass. Record every one (for the Turns tab's
            # per-prompt grouping); the first also seeds the session title.
            text = self._prompt_text(o.get("message"))
            if text:
                s["prompts"].append(
                    {"ts": ts or "", "title": text, "id": uuid or f"p{len(s['prompts'])}"}
                )
                if not s["title_prompt"]:
                    s["title_prompt"] = text
        if typ != "assistant":
            return
        msg = o.get("message")
        if not isinstance(msg, dict):
            return
        usage, model = msg.get("usage"), msg.get("model")
        if not isinstance(usage, dict) or not model or model == "<synthetic>":
            return  # nothing priceable on this row
        key = (msg.get("id"), o.get("requestId"))
        if all(key):
            if key in seen:
                return
            seen.add(key)
        # Claude Code models are bare ("claude-opus-4-8"); prefix the provider so
        # model_price strips it the same way and the Providers tab can roll up.
        model_name = model if "/" in model else "anthropic/" + model
        entry = s["models"].get(model_name)
        if entry is None:
            entry = s["models"][model_name] = {"total": self._new_acc(), "root": self._new_acc()}
        self._add_usage(entry["total"], usage)
        # One assistant message = one LLM step ("turn"). Record it for the Turns tab
        # with its own timestamp; sidechain turns are depth-1 so the renderer marks
        # them, mirroring the subagent split. Cost is $0 (recorded) -- the "$" view
        # reprices from the token columns, like every other Claude panel.
        i = int(usage.get("input_tokens", 0) or 0)
        out_t = int(usage.get("output_tokens", 0) or 0)
        cr = int(usage.get("cache_read_input_tokens", 0) or 0)
        cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
        side = o.get("isSidechain") is True
        s["turns"].append(
            {
                "ts": o.get("timestamp") or "",
                "depth": 1 if side else 0,
                "agent": "subagent" if side else "-",
                "model_name": model_name,
                "cost": 0.0,
                "input": i,
                "output": out_t,
                "reasoning": 0,
                "cache_read": cr,
                "cache_write": cw,
                "tokens_total": i + out_t + cr + cw,
            }
        )
        if o.get("isSidechain") is True:
            acc = self._new_acc()
            self._add_usage(acc, usage)
            s["side_usage"][uuid or len(s["side_usage"])] = (model_name, acc)
        else:
            self._add_usage(entry["root"], usage)

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title_custom"] or s["title_ai"] or s["title_prompt"] or "(untitled)"
        s["directory"] = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"
        s["created_at"] = iso_to_local(s["ts_min"])
        rows: list[dict] = []
        for model_name, e in s["models"].items():
            tot, root = e["total"], e["root"]
            # Recorded cost is $0 (Claude logs none); every token is "unpriced", so
            # the unpriced_* / root_unpriced_* splits carry the full counts and "$"
            # reprices them at list rates through App._compute_api_costs.
            rows.append(
                {
                    "root_id": sid,
                    "model_name": model_name,
                    "runs": tot["runs"],
                    "cost": 0.0,
                    "root_cost": 0.0,
                    "tokens_total": tot["tokens_total"],
                    "input": tot["input"],
                    "reasoning": tot["reasoning"],
                    "cache_read": tot["cache_read"],
                    "cache_write": tot["cache_write"],
                    "output": tot["output"],
                    "unpriced_input": tot["input"],
                    "unpriced_reasoning": tot["reasoning"],
                    "unpriced_cache_read": tot["cache_read"],
                    "unpriced_cache_write": tot["cache_write"],
                    "unpriced_output": tot["output"],
                    "root_unpriced_input": root["input"],
                    "root_unpriced_reasoning": root["reasoning"],
                    "root_unpriced_cache_read": root["cache_read"],
                    "root_unpriced_cache_write": root["cache_write"],
                    "root_unpriced_output": root["output"],
                }
            )
        s["model_rows"] = rows
        s["unpriced_tokens"] = sum(r["tokens_total"] for r in rows)  # all of it is unpriced
        s["subagents"] = self._build_subagents(sid, s)

    def _build_subagents(self, sid: str, s: dict) -> list[dict]:
        # Group sidechain assistant messages into distinct subagent runs: a run is a
        # maximal chain of sidechain uuids, so walking parentUuid up while still
        # inside side_uuids lands on the run's outermost message. Best-effort -- this
        # user has none -- but keeps the subagent tree correct where they exist.
        if not s["side_usage"]:
            return []
        parent, side = s["uuid_parent"], s["side_uuids"]

        def run_root(u: str) -> str:
            seen, cur = set(), u
            while True:
                p = parent.get(cur)
                if p in side and p not in seen:
                    seen.add(p)
                    cur = p
                else:
                    return cur

        groups: dict[str, dict[str, dict]] = {}  # run -> model_name -> acc
        for u, (model_name, acc) in s["side_usage"].items():
            run = run_root(u) if isinstance(u, str) else u
            by_model = groups.setdefault(run, {})
            ga = by_model.get(model_name)
            if ga is None:
                ga = by_model[model_name] = self._new_acc()
            for k in ga:
                ga[k] += acc[k]
        nodes = []
        for run, by_model in groups.items():
            tot = self._new_acc()
            best, best_runs = "unknown", -1
            for model_name, acc in by_model.items():
                for k in tot:
                    tot[k] += acc[k]
                if acc["runs"] > best_runs:
                    best_runs, best = acc["runs"], model_name
            # cost 0 (recorded); _priced_nodes reprices from the token columns in "$".
            nodes.append(
                self._node(
                    str(run)[:8], 1, "subagent", "subagent run", s["created_at"], best, 0.0, tot
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
                    subagents=len(s["subagents"]),
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
        # Mirror Store._demo_workflow: anonymize, backfill a synthetic price for the
        # (all-unpriced) tokens so the demo shows plausible spend, then scale.
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
        root_tot = self._new_acc()
        best, best_runs = "unknown (not recorded)", -1
        for model_name, e in s["models"].items():
            r = e["root"]
            for k in root_tot:
                root_tot[k] += r[k]
            if r["runs"] > best_runs:
                best_runs, best = r["runs"], model_name
        # cost 0 (recorded); _priced_nodes reprices from the token columns under "$".
        nodes = [self._node(workflow_id, 0, "-", s["title"], s["created_at"], best, 0.0, root_tot)]
        nodes.extend(dict(n) for n in s["subagents"])
        if self.demo:
            nodes = [self._demo_node(n) for n in nodes]
        return nodes

    def message_timeline(self, workflow_id: str) -> list[dict]:
        # Chronological per-turn rows for the Turns tab. ISO-8601 "Z" timestamps sort
        # lexicographically in time order, so a plain sort is correct; the renderer
        # gets the full localtime "YYYY-MM-DD HH:MM:SS" and picks the display width.
        # Walking the two time-sorted streams in lockstep, the latest prompt with
        # ts <= the turn's ts owns it -- so each turn is tagged with the prompt that
        # triggered it (sidechain turns inherit the main thread's current prompt).
        # Real rows -- App._scale_demo_turns hides magnitudes in demo, like Tools.
        s = self._parse().get(workflow_id)
        if not s:
            return []
        prompts = sorted(s["prompts"], key=lambda p: p["ts"])
        out = []
        pi, cur_id, cur_title = 0, "", ""
        for t in sorted(s["turns"], key=lambda r: r["ts"]):
            while pi < len(prompts) and prompts[pi]["ts"] <= t["ts"]:
                cur_id, cur_title = prompts[pi]["id"], _clean_prompt(prompts[pi]["title"])
                pi += 1
            r = dict(t)
            r["time"] = iso_to_local(r.pop("ts"))
            r["prompt_id"] = cur_id
            r["prompt_title"] = cur_title
            out.append(r)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        return True

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
