"""VS Code Copilot Chat backend (chatSessions session files)."""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
from datetime import datetime, timezone
from urllib.parse import unquote

from opentab.demo import demo_cost, demo_dir, demo_model, demo_title
from opentab.formatting import _clean_prompt
from opentab.models import Workflow
from opentab.util import git_root, read_files_parallel, windows_to_wsl_path


class VscodeStore:
    """Read GitHub Copilot Chat usage from VS Code's own chat-session store behind the
    same interface App expects from Store: workflows(), summary(), workflow_nodes(),
    model_breakdown(), plus the .demo/.demo_scale attributes and the Turns opt-in
    (message_timeline/supports_turns) -- every chat request is one recorded LLM turn.

    Where the data lives (per VS Code variant -- Code, Code - Insiders, VSCodium):

        <User>/workspaceStorage/<hash>/chatSessions/<session>.jsonl   (journal, current)
        <User>/workspaceStorage/<hash>/chatSessions/<session>.json    (plain, older)
        <User>/globalStorage/emptyWindowChatSessions/<session>.json[l] (no-folder windows)

    The journal format is NDJSON of patches replayed into one session object: kind 0
    (snapshot -> v), kind 1 (set v at path k), kind 2 (append list v at path k, default
    ["requests"]). The older plain .json is that final object directly; both shapes are
    read (a migrated session present in both dedupes by request id, journal first).

    Token accounting comes from VS Code core (chatModel.ts), which serializes per
    request: `completionTokens` -- accumulated across *all* tool-call rounds of the turn
    (setUsage sums per-round usage) -- and `promptTokens` plus the Copilot extension's
    `result.metadata.{promptTokens,outputTokens,resolvedModel}` from the last round. So
    output takes max(completionTokens, metadata.outputTokens) (the fuller figure; the
    metadata one is a single round and undercounts agentic turns), input takes
    max(metadata.promptTokens, promptTokens) -- the final round's full context. Caveats
    recorded nowhere in this store: per-round prompts are not summed (a many-round turn
    bills more input than recorded) and there is no cache read/write split, so input
    stays one bucket. Requests with no recorded tokens (canceled/queued/errored) are
    skipped; sessions with none at all are dropped.

    VS Code records *no* dollar cost here (`copilotCredits` counts premium requests, a
    quota unit, not USD) -> a token-only *subscription* backend like ClaudeStore:
    records_cost = False, recorded cost $0, every token lands in the unpriced_* splits
    and the "$" what-if reprices at API list rates. Models are mixed-provider
    (gpt-4.1, claude-sonnet, gemini -- resolvedModel covers the "auto" router), so ids
    are provider-prefixed for pricing (the CsvStore pattern). The project comes from the
    workspace's workspace.json folder/workspace URI folded to its git root (file:// and
    vscode-remote:// both handled; under WSL a Windows drive path folds onto its /mnt
    mount first so the walk can reach it); empty-window sessions group under
    "(no workspace)". Title precedence: customTitle -> computedTitle -> first real user
    prompt -> "(untitled)". No subagent tree.
    """

    records_cost = False  # cost is $0 until "$" reprices the (all-unpriced) tokens
    combined = False
    source_name = "VS Code"

    def __init__(self, user_dirs: str | list[str], args: argparse.Namespace):
        self.user_dirs = [user_dirs] if isinstance(user_dirs, str) else list(user_dirs)
        self.args = args
        self.demo = getattr(args, "demo", False)
        # Same hidden per-process factor Store/CopilotStore use; 1.0 outside demo.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._sessions: dict[str, dict] | None = None  # parsed lazily / on reload
        self._git_root_cache: dict[str, str] = {}
        self._project_cache: dict[str, str] = {}  # workspaceStorage hash dir -> directory

    # --- mixed-provider model ids (mirrors CsvStore/CopilotStore) -------------
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
        model = (model or "").strip()
        # VS Code qualifies the picker id with its own vendor ("copilot/gpt-4.1");
        # strip that so the real model family prices, then provider-prefix the rest.
        if model.lower().startswith("copilot/"):
            model = model[len("copilot/") :]
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

    @staticmethod
    def _new_acc() -> dict:
        return {
            "runs": 0,
            "input": 0,  # the final round's full context (no cache split recorded)
            "output": 0,  # accumulated across the turn's tool-call rounds
            "reasoning": 0,  # kept 0 (not recorded separately)
            "cache_read": 0,
            "cache_write": 0,
            "tokens_total": 0,
        }

    @staticmethod
    def _new_session() -> dict:
        return {
            "directory": "(unknown)",
            "title": None,
            "ts_min": None,
            "models": {},
            "turns": [],
        }

    @staticmethod
    def _num(value) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return int(value) if value >= 0 else 0
        return 0

    @staticmethod
    def _ms_to_local(ms: int) -> str:
        try:
            return (
                datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except (OverflowError, OSError, ValueError):
            return ""

    # --- discovery -------------------------------------------------------------
    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore).
        return [path for path, _ in self._session_files()]

    def _session_files(self) -> list[tuple[str, str | None]]:
        # (path, workspaceStorage hash dir or None for empty-window sessions), journal
        # .jsonl before plain .json within each directory so the fresher shape wins the
        # per-request dedup when a migrated session exists in both.
        out: list[tuple[str, str | None]] = []
        for user_dir in self.user_dirs:
            for hash_dir in sorted(glob.glob(os.path.join(user_dir, "workspaceStorage", "*"))):
                found = self._chat_files(os.path.join(hash_dir, "chatSessions"))
                out.extend((path, hash_dir) for path in found)
            empty = os.path.join(user_dir, "globalStorage", "emptyWindowChatSessions")
            out.extend((path, None) for path in self._chat_files(empty))
            # Also accept being pointed straight at a chatSessions-style directory.
            out.extend((path, None) for path in self._chat_files(user_dir))
        return out

    @staticmethod
    def _chat_files(directory: str) -> list[str]:
        files = glob.glob(os.path.join(directory, "*.jsonl"))
        files += glob.glob(os.path.join(directory, "*.json"))
        return files

    def _project_dir(self, hash_dir: str | None) -> str:
        # The workspace behind a workspaceStorage hash: workspace.json holds a
        # folder/workspace file:// URI; fold it to the git root like every backend.
        if hash_dir is None:
            return "(no workspace)"
        if hash_dir not in self._project_cache:
            self._project_cache[hash_dir] = self._resolve_project(hash_dir)
        return self._project_cache[hash_dir]

    def _resolve_project(self, hash_dir: str) -> str:
        try:
            with open(
                os.path.join(hash_dir, "workspace.json"), encoding="utf-8", errors="replace"
            ) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return "(unknown)"
        if not isinstance(meta, dict):
            return "(unknown)"
        for key in ("folder", "workspace"):
            uri = meta.get(key)
            if not isinstance(uri, str):
                continue
            path = self._uri_to_path(uri)
            if key == "workspace":  # a .code-workspace file -> its parent dir
                path = os.path.dirname(path)
            if path:
                return self._git_root(path)
        return "(unknown)"

    @staticmethod
    def _uri_to_path(uri: str) -> str:
        # A local workspace is a file:// URI; a Remote workspace (WSL / SSH / dev
        # container) is vscode-remote://<authority>/<path>. For wsl+<distro> that
        # path is directly usable when opentab runs inside the distro (the common
        # read-Windows-from-WSL case); for other authorities it still beats
        # "(unknown)" as a label -- the git-root walk falls back to it unchanged.
        if uri.startswith("file://"):
            path = unquote(uri[len("file://") :]).rstrip("/")
        elif uri.startswith("vscode-remote://"):
            _, sep, rest = uri[len("vscode-remote://") :].partition("/")
            if not sep:
                return ""
            path = "/" + unquote(rest).rstrip("/")
        else:
            return ""
        # A file URI on Windows is /C:/Users/... -- drop the leading slash, then
        # under WSL fold the drive path onto its mount (/mnt/c/...) so the git-root
        # walk can reach Windows-side workspaces.
        if re.match(r"^/[A-Za-z]:[/\\]", path):
            path = path[1:]
        return windows_to_wsl_path(path) or path

    # --- journal replay ----------------------------------------------------------
    @classmethod
    def _replay(cls, lines) -> dict:
        # Rebuild the session object from the journal: kind 0 snapshot, kind 1 set at
        # path, kind 2 append at path (default ["requests"]). Unknown/malformed entries
        # are skipped -- one bad patch never sinks the session.
        root: dict = {}
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if kind == 0:
                if isinstance(entry.get("v"), dict):
                    root = entry["v"]
            elif kind == 1:
                cls._apply_set(root, entry.get("k"), entry.get("v"))
            elif kind == 2:
                path = entry.get("k", ["requests"])
                items = entry.get("v")
                if isinstance(items, list):
                    cls._apply_append(root, path, items)
        return root

    @staticmethod
    def _walk(root: dict, path) -> tuple[object, object] | None:
        # Resolve a journal path to (parent container, final segment), creating
        # intermediate containers (list when the next segment is an int, else dict).
        if not isinstance(path, list) or not path:
            return None
        if not all(isinstance(seg, (str, int)) and not isinstance(seg, bool) for seg in path):
            return None
        cur: object = root
        for i, seg in enumerate(path[:-1]):
            nxt = path[i + 1]
            child = None
            if isinstance(cur, dict):
                child = cur.get(seg) if isinstance(seg, str) else None
            elif isinstance(cur, list) and isinstance(seg, int) and 0 <= seg < len(cur):
                child = cur[seg]
            if not isinstance(child, (dict, list)):
                child = [] if isinstance(nxt, int) else {}
                if isinstance(cur, dict) and isinstance(seg, str):
                    cur[seg] = child
                elif isinstance(cur, list) and isinstance(seg, int) and seg == len(cur):
                    cur.append(child)
                else:
                    return None  # unaddressable (int key into a dict, gap in a list)
            cur = child
        return cur, path[-1]

    @classmethod
    def _apply_set(cls, root: dict, path, value) -> None:
        target = cls._walk(root, path)
        if target is None:
            return
        parent, last = target
        if isinstance(parent, dict) and isinstance(last, str):
            parent[last] = value
        elif isinstance(parent, list) and isinstance(last, int):
            if 0 <= last < len(parent):
                parent[last] = value
            elif last == len(parent):
                parent.append(value)

    @classmethod
    def _apply_append(cls, root: dict, path, items: list) -> None:
        target = cls._walk(root, path)
        if target is None:
            return
        parent, last = target
        arr = None
        if isinstance(parent, dict) and isinstance(last, str):
            arr = parent.get(last)
            if not isinstance(arr, list):
                arr = parent[last] = []
        elif isinstance(parent, list) and isinstance(last, int) and 0 <= last < len(parent):
            arr = parent[last]
            if not isinstance(arr, list):
                arr = parent[last] = []
        if arr is not None:
            arr.extend(items)

    # --- parsing -------------------------------------------------------------
    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        seen: set[tuple[str, str]] = set()  # (session id, request id) across files
        pairs = self._session_files()
        texts = dict(read_files_parallel(p for p, _ in pairs))  # concurrent reads
        for path, hash_dir in pairs:
            text = texts.get(path)
            if text is None:
                continue
            root = self._load_session(path, text)
            if root is not None:
                self._ingest(root, path, self._project_dir(hash_dir), sessions, seen)
        for sid, s in sessions.items():
            self._finalize(sid, s)
        # Drop sessions with no recorded usage (an opened-and-abandoned chat panel
        # leaves an empty session file behind): nothing to attribute.
        self._sessions = {sid: s for sid, s in sessions.items() if s["model_rows"]}
        return self._sessions

    def _load_session(self, path: str, text: str) -> dict | None:
        # Content already read (concurrently) by _parse; split on "\n" to match the
        # journal's one-JSON-object-per-line shape that _replay expects.
        try:
            if path.endswith(".jsonl"):
                return self._replay(text.split("\n"))
            obj = json.loads(text)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    def _ingest(
        self,
        root: dict,
        path: str,
        directory: str,
        sessions: dict[str, dict],
        seen: set[tuple[str, str]],
    ) -> None:
        raw_sid = root.get("sessionId")
        sid = (
            raw_sid.strip()
            if isinstance(raw_sid, str) and raw_sid.strip()
            else os.path.splitext(os.path.basename(path))[0]
        )
        requests = root.get("requests")
        if not isinstance(requests, list):
            return
        s = sessions.setdefault(sid, self._new_session())
        if s["directory"] in ("(unknown)", "(no workspace)") and directory not in ("(unknown)",):
            s["directory"] = directory
        title = root.get("customTitle") or root.get("computedTitle")
        if isinstance(title, str) and title.strip() and s["title"] is None:
            s["title"] = " ".join(title.split())[:80]
        created = self._num(root.get("creationDate"))
        for i, req in enumerate(requests):
            if isinstance(req, dict):
                self._ingest_request(req, i, sid, created, s, seen)

    def _ingest_request(
        self,
        req: dict,
        idx: int,
        sid: str,
        session_created_ms: int,
        s: dict,
        seen: set[tuple[str, str]],
    ) -> None:
        result = req.get("result")
        md = result.get("metadata") if isinstance(result, dict) else None
        md = md if isinstance(md, dict) else {}
        # chatModel.ts accumulates completionTokens across the turn's tool-call rounds,
        # while the extension metadata carries a single round -- take the fuller figure.
        inp = max(self._num(md.get("promptTokens")), self._num(req.get("promptTokens")))
        out = max(self._num(req.get("completionTokens")), self._num(md.get("outputTokens")))
        if inp == 0 and out == 0:
            return  # canceled/queued/errored request -- nothing recorded
        raw_rid = req.get("requestId")
        rid = raw_rid.strip() if isinstance(raw_rid, str) and raw_rid.strip() else f"request-{idx}"
        if (sid, rid) in seen:
            return  # same session in journal + legacy shape -- count each request once
        seen.add((sid, rid))

        resolved = md.get("resolvedModel")
        model_id = resolved if isinstance(resolved, str) and resolved.strip() else None
        if model_id is None:
            raw_model = req.get("modelId")
            model_id = raw_model if isinstance(raw_model, str) else ""
        model = self._prefix_model(model_id)

        ts_ms = self._num(req.get("timestamp")) or session_created_ms
        if ts_ms and (s["ts_min"] is None or ts_ms < s["ts_min"]):
            s["ts_min"] = ts_ms

        message = req.get("message")
        if isinstance(message, dict):
            message = message.get("text")
        prompt = _clean_prompt(message) if isinstance(message, str) else ""
        if s["title"] is None and prompt:
            s["title"] = prompt[:80]

        acc = s["models"].get(model)
        if acc is None:
            acc = s["models"][model] = self._new_acc()
        acc["runs"] += 1
        acc["input"] += inp
        acc["output"] += out
        acc["tokens_total"] += inp + out

        s["turns"].append(
            {
                "ts": self._ms_to_local(ts_ms) if ts_ms else "",
                "depth": 0,  # VS Code chat has no subagent tree
                "agent": "-",
                "model_name": model,
                "cost": 0.0,  # nothing recorded; "$" reprices from the token columns
                "input": inp,
                "output": out,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "tokens_total": inp + out,
                "prompt": prompt,
                "prompt_id": rid,  # one chat request == one user prompt == one turn
            }
        )

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title"] or "(untitled)"
        s["created_at"] = self._ms_to_local(s["ts_min"]) if s["ts_min"] else ""
        rows: list[dict] = []
        for model_name, acc in s["models"].items():
            # Recorded cost is $0 (VS Code logs none); every token is "unpriced", so the
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
        s["total_tokens"] = sum(r["tokens_total"] for r in rows)
        s["unpriced_tokens"] = s["total_tokens"]  # all of it

    # --- Store interface -----------------------------------------------------
    def workflows(self) -> list[Workflow]:
        self._sessions = None  # reload (r) re-reads fresh; model methods reuse cache
        self._project_cache = {}
        sessions = self._parse()
        rows = []
        for sid, s in sessions.items():
            rows.append(
                Workflow(
                    id=sid,
                    title=s["title"],
                    directory=s["directory"],
                    created_at=s["created_at"],
                    root_cost=0.0,  # recorded cost is $0; "$" reprices the tokens
                    total_cost=0.0,
                    subagents=0,  # VS Code chat has no subagent tree
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
        # Mirror CopilotStore._demo_workflow: anonymize, backfill a synthetic price for
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
        nodes = [
            {
                "id": workflow_id,
                "depth": 0,
                "agent": "-",
                "title": s["title"],
                "created_at": s["created_at"],
                "cost": 0.0,  # _priced_nodes reprices from the token columns under "$"
                "model_name": best,
                "tokens_input": root["input"],
                "tokens_output": root["output"],
                "tokens_reasoning": root["reasoning"],
                "tokens_cache_read": root["cache_read"],
                "tokens_cache_write": root["cache_write"],
                "tokens_total": root["tokens_total"],
            }
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

    # --- Turns tab opt-in ----------------------------------------------------
    def message_timeline(self, workflow_id: str) -> list[dict]:
        # Chronological per-turn rows, one per chat request; the request id doubles as
        # prompt_id so each user prompt heads its own "▸" group (a VS Code turn is
        # exactly one prompt). App._scale_demo_turns hides magnitudes in demo.
        s = self._parse().get(workflow_id)
        if not s:
            return []
        out = []
        for t in sorted(s["turns"], key=lambda r: r["ts"]):
            r = dict(t)
            r["time"] = r.pop("ts")  # already canonical "YYYY-MM-DD HH:MM:SS" (local)
            prompt = r.pop("prompt", "")
            r["prompt_id"] = r["prompt_id"] or prompt
            r["prompt_title"] = prompt
            out.append(r)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        return True
