"""Codex CLI rollout backend."""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re

from opentab.demo import demo_cost, demo_dir, demo_model, demo_title
from opentab.formatting import _clean_prompt, iso_to_local
from opentab.models import Workflow
from opentab.util import LazyStatusRoot, git_root, read_files_parallel, tool_rows_from_turns


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

    Codex's collab / multi-agent mode writes each spawned thread as its own rollout
    file whose session_meta.source carries the parent thread id (_spawn_source), so
    those fold into a subagent tree under their parent (_link_subagents): the child
    leaves the workflows list, the root's totals cover the subtree (root_* keeps its
    own share -- the ClaudeStore accounting), workflow_nodes lists the children with
    their agent nickname/role, and Turns/Tools cover the whole tree. A plain session
    stays a single depth-0 node. Sessions with no recorded token usage
    (legacy/aborted) are dropped.
    Implements the **Turns** opt-in (message_timeline/supports_turns): every accepted
    token_count delta is one turn row, grouped under the ▸ user_message that triggered
    it (the ClaudeStore lockstep pattern); cost stays $0, so "$" estimates each row.
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
        self._head_meta_cache: dict[str, dict | None] = {}  # path -> head metadata

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
            "models": {},  # model_name -> acc (a session's own usage; trees fold later)
            "turns": [],  # one per accepted token_count delta, for the Turns tab
            "prompts": [],  # user_message events, for the Turns tab's ▸ grouping
            "parent_id": None,  # ThreadSpawn.parent_thread_id for a spawned thread
            "agent": None,  # the spawned thread's nickname/role, for the tree label
        }

    @staticmethod
    def _spawn_source(src) -> tuple[str, str] | None:
        # session_meta.source for a thread Codex spawned as a subagent (its
        # collab / multi-agent mode): serde's external tagging of
        # SessionSource::SubAgent(SubAgentSource::ThreadSpawn{..}) gives
        #   {"subagent": {"thread_spawn": {"parent_thread_id": ...,
        #                 "agent_nickname"/"agent_role": ...}}}
        # -> (parent thread id, agent label). Review/compact subagents
        # ({"subagent": "review"}) carry no parent id and stay standalone roots.
        sub = src.get("subagent") if isinstance(src, dict) else None
        spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
        parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
        if not parent:
            return None
        agent = spawn.get("agent_nickname") or spawn.get("agent_role") or "subagent"
        return str(parent), str(agent)

    # --- parsing -------------------------------------------------------------
    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore).
        return self._files()

    def _files(self) -> list[str]:
        return glob.glob(os.path.join(self.root_dir, "**", "*.jsonl"), recursive=True)

    def _session_files(self, session_id: str) -> list[str]:
        # A session's rollout is rollout-<timestamp>-<uuid>.jsonl somewhere in the
        # YYYY/MM/DD tree; glob for the uuid so an id resolves without a parse.
        pattern = os.path.join(self.root_dir, "**", "*" + glob.escape(session_id) + ".jsonl")
        return glob.glob(pattern, recursive=True)

    def _head_meta(self, path: str) -> dict | None:
        # The session_meta at a rollout's head (or the rare legacy bare blob --
        # _parse_file's two shapes) without parsing the file: {id, cwd, parent_id,
        # agent}, the currency of the --status trio. cwd can be missing from the
        # meta and appear on the first turn_context instead, so scanning continues
        # (within the byte budget) until one supplies it. Memoized per path: one
        # status call touches the same heads from recent_roots, root_of, and
        # status_nodes.
        if path in self._head_meta_cache:
            return self._head_meta_cache[path]
        meta = None
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
                    if not isinstance(o, dict):
                        continue
                    typ = o.get("type")
                    p = o.get("payload") if isinstance(o.get("payload"), dict) else None
                    m = p if typ == "session_meta" else (o if typ is None and "git" in o else None)
                    if m is not None and meta is None:
                        spawn = self._spawn_source(m.get("source"))
                        meta = {
                            "id": m.get("id"),
                            "cwd": m.get("cwd"),
                            "parent_id": spawn[0] if spawn else None,
                            "agent": spawn[1] if spawn else None,
                        }
                        if meta["cwd"]:
                            break
                    elif meta is not None and typ == "turn_context" and p and p.get("cwd"):
                        meta["cwd"] = p["cwd"]
                        break
        except OSError:
            pass
        self._head_meta_cache[path] = meta
        return meta

    def _walk_root(self, session_id: str) -> str | None:
        # Resolve a session id to the root of its spawned-thread tree by following
        # session_meta parent ids while the parent's rollout exists on disk (an
        # orphaned child stays its own root, matching _link_subagents). None when
        # the id has no rollout at all -- the cheap membership answer the --status
        # backend probe relies on.
        paths = self._session_files(session_id)
        if not paths:
            return None
        cur, seen = session_id, {session_id}
        while True:
            meta = None
            for path in paths:
                meta = self._head_meta(path)
                if meta:
                    break
            pid = (meta or {}).get("parent_id")
            if not pid or pid in seen:
                return cur
            paths = self._session_files(pid)
            if not paths:
                return cur  # parent rollout deleted or in another dir -> child stands alone
            seen.add(pid)
            cur = pid

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        for path, text in read_files_parallel(self._files()):
            self._parse_file(path, text.split("\n"), sessions)
        for sid, s in sessions.items():
            self._finalize(sid, s)
        # Drop sessions with no recorded token usage (legacy rollouts, aborted runs):
        # they would only add a pile of $0 / 0-token rows to a spend browser.
        self._sessions = {sid: s for sid, s in sessions.items() if s["model_rows"]}
        self._link_subagents(self._sessions)
        return self._sessions

    def _link_subagents(self, sessions: dict[str, dict]) -> None:
        # Fold spawned threads (collab / multi-agent mode) under their parent: a
        # child rollout is its own file whose session_meta carries the parent
        # thread id (_spawn_source). Children leave the top-level workflows list;
        # each root's model_rows are rebuilt to cover the whole subtree (total)
        # while the root_* splits keep the root's own usage -- the ClaudeStore
        # root-vs-total accounting. A child whose parent isn't on disk (deleted,
        # different --codex-dir) stays a standalone root.
        for s in sessions.values():
            s["children"] = []
            s["is_child"] = False
        for sid, s in sessions.items():
            pid = s["parent_id"]
            if pid and pid != sid and pid in sessions:
                sessions[pid]["children"].append(sid)
                s["is_child"] = True
        for sid, s in sessions.items():
            if s["is_child"] or not s["children"]:
                continue  # a child, or a flat root whose _finalize rows already fit
            self._fold_tree_rows(sid, s, sessions)

    @staticmethod
    def _descendants(sessions: dict[str, dict], sid: str) -> list[tuple[str, int]]:
        # (child sid, depth) breadth-first below sid; a cycle (corrupt metadata)
        # is cut by the seen-set rather than looping.
        out: list[tuple[str, int]] = []
        queue, seen = [(sid, 0)], {sid}
        while queue:
            cur, depth = queue.pop(0)
            for child in sessions[cur]["children"]:
                if child in seen:
                    continue
                seen.add(child)
                out.append((child, depth + 1))
                queue.append((child, depth + 1))
        return out

    def _fold_tree_rows(self, sid: str, s: dict, sessions: dict[str, dict]) -> None:
        # Rebuild the root's model_rows so `total` covers root + every descendant
        # while `root_*` keeps only the root's own usage (all of it unpriced --
        # Codex records no cost -- so unpriced_* mirrors total and root_unpriced_*
        # the root's own share, exactly like ClaudeStore._finalize).
        total: dict[str, dict] = {}
        own: dict[str, dict] = {}

        def add(bucket: dict[str, dict], model: str, acc: dict) -> None:
            t = bucket.setdefault(model, self._new_acc())
            for k in t:
                t[k] += acc[k]

        for model, acc in s["models"].items():
            add(total, model, acc)
            add(own, model, acc)
        for child, _depth in self._descendants(sessions, sid):
            for model, acc in sessions[child]["models"].items():
                add(total, model, acc)
        rows: list[dict] = []
        for model, acc in total.items():
            r = own.get(model, self._new_acc())
            rows.append(
                {
                    "root_id": sid,
                    "model_name": model,
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
                    "root_unpriced_input": r["input"],
                    "root_unpriced_reasoning": r["reasoning"],
                    "root_unpriced_cache_read": r["cache_read"],
                    "root_unpriced_cache_write": r["cache_write"],
                    "root_unpriced_output": r["output"],
                }
            )
        s["model_rows"] = rows
        s["unpriced_tokens"] = sum(r["tokens_total"] for r in rows)

    def _parse_file(self, path: str, lines: list[str], sessions: dict[str, dict]) -> None:
        sid = self._id_from_name(path)
        s = sessions.setdefault(sid, self._new_session()) if sid else None
        cur_model: str | None = None
        prev = (0, 0, 0, 0)  # cumulative (input, output, cached, total) seen so far
        pending_tools: list[str] = []  # tool calls since the last accepted turn delta
        for line in lines:
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
                    if s["parent_id"] is None:
                        spawn = self._spawn_source(meta.get("source"))
                        if spawn:
                            s["parent_id"], s["agent"] = spawn
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
            elif typ == "response_item" and p.get("type") in (
                "function_call",
                "custom_tool_call",
                "local_shell_call",
            ):
                # A tool call belongs to the turn whose token_count closes it; queue
                # it for the next accepted delta (the duplicate echo doesn't consume).
                pending_tools.append(
                    p.get("name") or ("shell" if p["type"] == "local_shell_call" else p["type"])
                )
            elif typ == "event_msg" and p.get("type") == "user_message":
                # First user prompt = session title. Take any user_message (older
                # rollouts omit kind; only "plain" appears on newer ones), and
                # collapse whitespace since Codex prompts often span lines with
                # @file mentions that would otherwise break the one-line title cell.
                txt = p.get("message")
                if isinstance(txt, str) and txt.strip():
                    if not s["title_prompt"]:
                        s["title_prompt"] = " ".join(txt.split())[:80]
                    # Every prompt is kept (raw, line breaks intact) for the Turns
                    # tab's ▸ grouping; the record timestamp doubles as its id.
                    s["prompts"].append({"ts": ts or "", "id": ts or txt, "title": txt.strip()})
            elif typ == "event_msg" and p.get("type") == "token_count":
                prev = self._apply_token_count(s, p.get("info"), cur_model, prev, ts, pending_tools)

    def _apply_token_count(
        self,
        s: dict,
        info,
        cur_model: str | None,
        prev: tuple,
        ts=None,
        pending_tools: list[str] | None = None,
    ) -> tuple:
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
        uncached = max(0, d_in - d_cached)
        acc["runs"] += 1
        acc["input"] += uncached  # input_tokens includes the cached read
        acc["cache_read"] += max(0, d_cached)
        acc["output"] += max(0, d_out)
        acc["tokens_total"] += max(0, d_in) + max(0, d_out)
        # One Turns row per accepted delta -- the per-turn slice of the cumulative
        # total the block above just attributed. Cost stays $0 (Codex records none);
        # the Turns tab's "$" view reprices each row from its token columns.
        # An accepted delta consumes the tool calls queued since the last one --
        # they're the calls this turn made, and tool_breakdown splits the turn's
        # tokens across them.
        tools = list(pending_tools) if pending_tools else []
        if pending_tools:
            pending_tools.clear()
        s["turns"].append(
            {
                "ts": ts or "",
                "depth": 0,  # Codex has no subagent tree
                "agent": "-",
                "model_name": model_name,
                "cost": 0.0,
                "input": uncached,
                "output": max(0, d_out),
                "reasoning": 0,
                "cache_read": max(0, d_cached),
                "cache_write": 0,
                "tokens_total": max(0, d_in) + max(0, d_out),
                "tools": tools,
            }
        )
        return cur

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title_prompt"] or "(untitled)"
        s["directory"] = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"
        s["created_at"] = iso_to_local(s["ts_meta"] or s["ts_min"])
        rows: list[dict] = []
        for model_name, acc in s["models"].items():
            # Recorded cost is $0 (Codex logs none); every token is "unpriced", so the
            # unpriced_* splits carry the full counts. root == total here: a session
            # that turns out to have spawned children gets these rows rebuilt by
            # _fold_tree_rows once the tree is linked.
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
            if s["is_child"]:
                continue  # a spawned thread rolls up into its parent's row
            model_rows = s["model_rows"]
            rows.append(
                Workflow(
                    id=sid,
                    title=s["title"],
                    directory=s["directory"],
                    created_at=s["created_at"],
                    root_cost=0.0,  # recorded cost is $0; "$" reprices the tokens
                    total_cost=0.0,
                    subagents=len(self._descendants(sessions, sid)),
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
            if s["is_child"]:
                continue  # its usage is already inside the root's folded rows
            out.extend(s["model_rows"])
        return out

    @staticmethod
    def _session_rollup(s: dict) -> tuple[dict, str]:
        # One session's own usage rolled across models -> (acc, busiest model).
        acc_out = CodexStore._new_acc()
        best, best_runs = "unknown (not recorded)", -1
        for model_name, acc in s["models"].items():
            for k in acc_out:
                acc_out[k] += acc[k]
            if acc["runs"] > best_runs:
                best_runs, best = acc["runs"], model_name
        return acc_out, best

    def recent_roots(self) -> list[dict]:
        # Root sessions newest-activity-first, the cheap sibling of
        # Store.recent_roots for the one-shot --status command. No parse: Codex
        # appends every record to the session's own rollout, so the file mtime IS
        # the session's last activity and the uuid in its name is the id. The two
        # lazy fields pay a file-head read only when accessed: "directory" is the
        # session_meta cwd, and "id" walks a spawned thread up to its root -- so a
        # child rollout still streaming surfaces its parent workflow, matching the
        # subtree-activity ordering of Store.recent_roots.
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
        rows = []
        for sid, (last_active, path) in newest.items():
            rows.append(
                LazyStatusRoot(
                    {"last_active": last_active},
                    {
                        "id": lambda s=sid: self._walk_root(s) or s,
                        "directory": lambda p=path: (self._head_meta(p) or {}).get("cwd")
                        or "(unknown)",
                    },
                )
            )
        rows.sort(key=lambda r: r["last_active"], reverse=True)
        return rows

    def root_of(self, session_id: str) -> str | None:
        # Resolve any session id to its root: a spawned thread's id walks up its
        # parent chain; None when no rollout carries the id (the id belongs to
        # some other backend, or the file is gone).
        return self._walk_root(session_id)

    def status_nodes(self, workflow_id: str) -> list[dict]:
        # workflow_nodes for the --status one-shot: identical rows, but off a
        # parse of just this session's subtree when nothing is loaded yet -- a
        # status poll must never trigger the full-tree parse. Children reference
        # their parent (not vice versa), so candidate descendants -- rollouts whose
        # filename timestamp is at or after the root's own (a thread is spawned
        # after its parent started) -- have their heads scanned for a thread_spawn
        # source pointing into the subtree; only the matched files are parsed.
        # Walking candidates in filename order guarantees a parent's file is
        # admitted before any of its children's.
        if self._sessions is not None:
            return self.workflow_nodes(workflow_id)
        own = self._session_files(workflow_id)
        if not own:
            return []
        root_base = min(os.path.basename(p) for p in own)
        chosen = dict.fromkeys(own)  # ordered set of files to parse
        subtree = {workflow_id}
        for path in sorted(self._files(), key=os.path.basename):
            if path in chosen or os.path.basename(path) < root_base:
                continue
            sid = self._id_from_name(path)
            if not sid or sid in subtree:
                continue
            meta = self._head_meta(path)
            if meta and meta.get("parent_id") in subtree:
                subtree.add(sid)
                chosen[path] = None
        sessions: dict[str, dict] = {}
        for path, text in read_files_parallel(list(chosen)):
            self._parse_file(path, text.split("\n"), sessions)
        for sid, s in sessions.items():
            self._finalize(sid, s)
        # Keep the target even when usage-less (unlike _parse's drop): a root that
        # only spawned threads must still price its children's subtree.
        sessions = {sid: s for sid, s in sessions.items() if s["model_rows"] or sid == workflow_id}
        self._link_subagents(sessions)
        return self._nodes_from(sessions, workflow_id)

    def workflow_nodes(self, workflow_id: str) -> list[dict]:
        return self._nodes_from(self._parse(), workflow_id)

    def _nodes_from(self, sessions: dict[str, dict], workflow_id: str) -> list[dict]:
        s = sessions.get(workflow_id)
        if not s:
            return []
        # cost 0 (recorded); _priced_nodes reprices from the token columns under "$".
        acc, best = self._session_rollup(s)
        nodes = [self._node(workflow_id, 0, "-", s["title"], s["created_at"], best, 0.0, acc)]
        for child, depth in self._descendants(sessions, workflow_id):
            cs = sessions[child]
            acc, best = self._session_rollup(cs)
            nodes.append(
                self._node(
                    child,
                    depth,
                    cs["agent"] or "subagent",
                    cs["title"],
                    cs["created_at"],
                    best,
                    0.0,
                    acc,
                )
            )
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

    def _subtree_turns(self, workflow_id: str) -> list[dict]:
        # The session's own turn rows plus every spawned descendant's, the child
        # rows tagged with their depth/agent so the Turns tab marks them like
        # Claude's sidechains (and tool_breakdown covers the whole tree).
        sessions = self._parse()
        s = sessions.get(workflow_id)
        if not s:
            return []
        turns = list(s["turns"])
        for child, depth in self._descendants(sessions, workflow_id):
            cs = sessions[child]
            agent = cs["agent"] or "subagent"
            for t in cs["turns"]:
                turns.append({**t, "depth": depth, "agent": agent})
        return turns

    # --- Turns tab opt-in ----------------------------------------------------
    def message_timeline(self, workflow_id: str) -> list[dict]:
        # Chronological per-turn rows for the Turns tab (the ClaudeStore pattern):
        # ISO timestamps sort lexicographically, and walking the two time-sorted
        # streams in lockstep tags each turn with the latest prompt at ts <= the
        # turn's ts. Spawned threads' turns are interleaved by time, tagged with
        # their agent (prompts stay the root's own -- a child's user_message is
        # the spawn instruction, not something the user typed). Real rows --
        # App._scale_demo_turns hides magnitudes in demo.
        s = self._parse().get(workflow_id)
        if not s:
            return []
        prompts = sorted(s["prompts"], key=lambda p: p["ts"])
        out = []
        pi, cur_id, cur_title, cur_full = 0, "", "", ""
        for t in sorted(self._subtree_turns(workflow_id), key=lambda r: r["ts"]):
            while pi < len(prompts) and prompts[pi]["ts"] <= t["ts"]:
                cur_id, cur_full = prompts[pi]["id"], prompts[pi]["title"]
                cur_title = _clean_prompt(cur_full)
                pi += 1
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
        # Per-(tool, model) token attribution for the Tools tab: each accepted
        # token_count delta is one turn, and the function_call/custom_tool_call
        # records since the previous delta are the calls that turn made -- its
        # tokens split evenly across them (Store.tool_breakdown semantics),
        # spawned descendants included. Cost stays $0 (Codex records none); the
        # "$" view reprices per row.
        return tool_rows_from_turns(self._subtree_turns(workflow_id))

    def supports_tools(self, workflow_id: str) -> bool:
        # Rollouts always record the turn's tool calls, so the tab applies to every
        # session; one without tool calls shows the honest empty message.
        return True
