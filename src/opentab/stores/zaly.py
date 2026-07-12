"""Zaly session JSONL backend."""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
from datetime import datetime, timezone

from opentab.demo import demo_cost, demo_dir, demo_model, demo_title
from opentab.formatting import _clean_prompt
from opentab.models import Workflow
from opentab.util import (
    ATTACHMENT_EST_TOKENS,
    LazyStatusRoot,
    context_add,
    context_rows,
    est_tokens,
    git_root,
    read_files_parallel,
    tool_rows_from_turns,
)


def default_zaly_data_dir() -> str:
    # Mirror zaly's envPaths resolution for its DATA dir (which holds sessions/):
    # $ZALY_DATA -> $ZALY_ROOT/data -> the platform default (XDG on POSIX/macOS --
    # zaly does NOT use ~/Library/Application Support -- LOCALAPPDATA on Windows).
    env = (os.environ.get("ZALY_DATA") or "").strip()
    if env:
        return env
    root = (os.environ.get("ZALY_ROOT") or "").strip()
    if root:
        return os.path.join(root, "data")
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(local, "zaly", "Data")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "zaly")


def _default_zaly_state_dir() -> str:
    # Same resolution for the STATE dir, where zaly keeps auth.json (the OAuth-vs-API-key
    # signal the billing split reads). Deliberately independent of --zaly-dir: the two
    # trees are unrelated on a default install (~/.local/share vs ~/.local/state).
    env = (os.environ.get("ZALY_STATE") or "").strip()
    if env:
        return env
    root = (os.environ.get("ZALY_ROOT") or "").strip()
    if root:
        return os.path.join(root, "state")
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(local, "zaly", "State")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "zaly")


class ZalyStore:
    """Read Zaly sessions (<data>/sessions/<encoded-workspace>/<uuid>/session.jsonl, data
    resolved like zaly's own envPaths: $ZALY_DATA / $ZALY_ROOT/data / $XDG_DATA_HOME/zaly,
    default ~/.local/share/zaly, or --zaly-dir) behind the same four methods App expects
    from Store, plus the .demo/.demo_scale attributes -- like the other JSONL backends.

    Zaly (folke's conversational coding agent) persists each session as an append-only
    JSONL DAG: `session-settings` nodes snapshot `{sessionId, cwd, workspace, modelId}`,
    `message` nodes carry the conversation, and resume/fork append to the *same* file (so
    there is no cross-file dedup problem; assistant messages still dedupe by `message.id`
    since a regenerated branch keeps its abandoned siblings -- each was a real API call and
    counts). Assistant messages record `meta.usage`: **Anthropic-normalized** tokens
    (`input` is already the *uncached* prompt -- zaly's OpenAI adapter subtracts
    `cached_tokens` itself -- cacheRead/cacheWrite separate; total = input + output +
    cacheRead + cacheWrite) with `reasoning` a *subset of output* (OpenAI's
    `reasoning_tokens` detail) -- reported as 0 here like the other folded backends
    (Codex/Copilot), since opentab's reasoning column is an *additive* token class and
    counting the subset would double-bill it under "$". `meta.usage.cost` is a
    per-component USD object
    ({input, output, cacheRead, ...}, no .total) summed here -- but zaly computes it from
    its model catalog for **every** route, including subscription/OAuth ones (a
    ChatGPT-plan openai-codex login) whose marginal cost is actually $0. So the same
    metered-vs-subscription split as `PiStore`/`OpenClawStore`: a message is
    **subscription** when its provider (the `meta.modelId` prefix before "/") is an OAuth
    login in zaly's auth.json (`<state>/auth.json`, `{provider: {type: "oauth"|"api-key"}}`,
    read read-only) or matches a plan/local marker (`_SUBSCRIPTION_MARKERS`:
    codex/copilot/ollama/...) -- those tokens stay **unpriced** (the "$" view estimates
    them) while metered messages price as real spend; **`records_cost` is a per-instance
    property** (True iff any metered cost), resolved lazily so construction stays free for
    the warm-start cache. Models arrive provider-qualified (`openai-codex/gpt-5.6-sol`) and
    are used verbatim for pricing and the Providers rollup.

    The workspace (settings `workspace`, falling back to `cwd`) folds to the **git root**;
    the session id prefers settings `sessionId` over the directory name. `compact` nodes
    (their `summary` is a system message) and role "system"/meta records carry no usage and
    are skipped. **No subagent tree**: zaly writes subagent transcripts to tmpdir (outside
    the sessions tree) and does not fold their usage into the parent's `meta.usage`, so
    subagent spend is not recorded anywhere durable -- a latent undercount if zaly ever
    persists them. Sessions with no recorded usage are dropped (merely launching zaly
    writes a settings-only file). Implements **Turns** (one row per assistant message,
    ▸-grouped by user prompt, epoch-ms timestamps) and **Tools** (the step's `tool-call`
    content parts, split evenly like the other turn-based backends).
    """

    combined = False
    source_name = "Zaly"

    # Provider/api substrings that mark a subscription (plan-included) or local/free route
    # even when auth.json is unavailable -- their recorded cost is a list-price estimate,
    # not spend. ollama/lm-studio are zaly's local-model plugins (no marginal cost).
    _SUBSCRIPTION_MARKERS = (
        "codex",
        "copilot",
        "chatgpt",
        "claude-code",
        "claude-max",
        "claude-pro",
        "ollama",
        "lm-studio",
        "lmstudio",
    )

    def __init__(self, root_dir: str, args: argparse.Namespace):
        self.root_dir = root_dir
        self.args = args
        self.demo = getattr(args, "demo", False)
        # Same hidden per-process factor Store/CodexStore use; 1.0 outside demo.
        self.demo_scale = 3.0 ** random.uniform(-1.0, 1.0) if self.demo else 1.0
        self._sessions: dict[str, dict] | None = None  # parsed lazily / on reload
        self._git_root_cache: dict[str, str] = {}
        # zaly's auth.json lives in its STATE dir (not beside the data dir): providers with
        # type "oauth" are consumer-plan logins whose recorded cost is not spend.
        self._oauth_providers = self._load_oauth_providers()
        self._records_cost: bool | None = None  # resolved lazily (records_cost property)

    # --- helpers -------------------------------------------------------------
    def _git_root(self, cwd: str) -> str:
        if cwd not in self._git_root_cache:
            self._git_root_cache[cwd] = git_root(cwd)
        return self._git_root_cache[cwd]

    def _load_oauth_providers(self) -> set[str]:
        # <state>/auth.json maps provider -> {type: "oauth" | "api-key", ...}; "oauth"
        # means a consumer-plan login (subscription), not a metered API key. Read-only; we
        # read only each provider's "type", never the tokens.
        path = os.path.join(_default_zaly_state_dir(), "auth.json")
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

    def _is_subscription(self, provider) -> bool:
        prov = (provider or "").lower()
        if prov and prov in self._oauth_providers:
            return True
        return any(marker in prov for marker in self._SUBSCRIPTION_MARKERS)

    @staticmethod
    def _provider_of(model: str) -> str:
        # zaly model ids are provider-qualified ("openai-codex/gpt-5.6-sol"); the prefix is
        # the auth-provider name auth.json is keyed by.
        return model.split("/", 1)[0] if "/" in model else ""

    @staticmethod
    def _int(value) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _cost_total(usage: dict) -> float:
        # zaly's cost is a per-component USD object mirroring TokenCount ({input, output,
        # cacheRead, cacheWrite, reasoning}, no .total) -- sum whatever components exist.
        cost = usage.get("cost")
        if not isinstance(cost, dict):
            return 0.0
        total = 0.0
        for v in cost.values():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                total += float(v)
        return total

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
        # zaly timestamps are epoch milliseconds; normalize to epoch seconds.
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
            "output": 0,  # includes reasoning (zaly's `reasoning` field is a subset of it)
            "reasoning": 0,  # stays 0: counting the subset would double-bill under "$"
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
    def _new_session(sid: str) -> dict:
        return {
            "sid": sid,  # dir-name id; settings.sessionId overrides when present
            "cwd": None,  # settings workspace (or cwd) -> git root
            "model_setting": None,  # latest session-settings modelId (per-message fallback)
            "ts_min": None,  # earliest record (epoch seconds)
            "title_prompt": None,
            "models": {},
            "seen_msgs": set(),  # assistant message.ids already counted (branch dedup)
            "turns": [],  # one per assistant message, for the Turns/Tools tabs
            "prompts": [],  # user messages, for the Turns tab's ▸ grouping
            "context": {},  # (category, kind) -> [count, est_tokens], Context tab
        }

    # --- parsing -------------------------------------------------------------
    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore).
        return self._files()

    def _files(self) -> list[str]:
        return glob.glob(os.path.join(self.root_dir, "sessions", "*", "*", "session.jsonl"))

    def _session_files(self, session_id: str) -> list[str]:
        # A session lives at sessions/<encoded-workspace>/<uuid>/session.jsonl; the
        # uuid directory names it on disk. (settings.sessionId can override the
        # canonical id for the browser, but the dir uuid is what an id target can
        # name without parsing -- status_nodes tolerates the mismatch.)
        return glob.glob(
            os.path.join(self.root_dir, "sessions", "*", glob.escape(session_id), "session.jsonl")
        )

    def _head_cwd(self, path: str) -> str:
        # The session-settings snapshot at the file head carries workspace/cwd; a
        # bounded read (ClaudeStore's _transcript_cwd pattern) so a recent_roots
        # scan stops paying at the row that matches.
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
                    if not isinstance(o, dict) or o.get("type") != "session-settings":
                        continue
                    settings = o.get("settings")
                    if isinstance(settings, dict):
                        cwd = settings.get("workspace") or settings.get("cwd")
                        if isinstance(cwd, str) and cwd:
                            return cwd
        except OSError:
            pass
        return "(unknown)"

    def recent_roots(self) -> list[dict]:
        # Root sessions newest-activity-first, the cheap sibling of
        # Store.recent_roots for the one-shot --status command. No parse: resume
        # and fork append to the same session.jsonl, so its mtime IS the last
        # activity and the uuid directory names the session; "directory" (the
        # settings workspace) is read lazily from the file head.
        rows = []
        for path in self._files():
            sid = os.path.basename(os.path.dirname(path))
            try:
                last_active = int(os.stat(path).st_mtime * 1000)  # ms, like Store's
            except OSError:
                continue  # deleted mid-scan
            rows.append(
                LazyStatusRoot(
                    {"id": sid, "last_active": last_active},
                    {"directory": lambda p=path: self._head_cwd(p)},
                )
            )
        rows.sort(key=lambda r: r["last_active"], reverse=True)
        return rows

    def root_of(self, session_id: str) -> str | None:
        # A zaly session id is already its root (no durable subagent tree), so
        # this only confirms the uuid directory exists -- the cheap membership
        # answer the --status backend probe relies on.
        return session_id if self._session_files(session_id) else None

    def status_nodes(self, workflow_id: str) -> list[dict]:
        # workflow_nodes for the --status one-shot: the identical row, but off a
        # parse of just this session's own file when nothing is loaded yet -- a
        # status poll must never trigger the full-tree parse. The canonical id can
        # differ from the <uuid> directory name (settings.sessionId wins), so take
        # the file's single parsed session whatever it keyed to.
        if self._sessions is not None and workflow_id in self._sessions:
            return self.workflow_nodes(workflow_id)
        sessions: dict[str, dict] = {}
        for path, text in read_files_parallel(self._session_files(workflow_id)):
            self._parse_file(path, text.split("\n"), sessions)
        for s in sessions.values():
            self._finalize(s)
            if s["model_rows"]:
                return self._nodes_from(workflow_id, s)
        return []

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
                    if not isinstance(o, dict) or o.get("type") != "message":
                        continue
                    msg = o.get("message")
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    meta = msg.get("meta")
                    usage = meta.get("usage") if isinstance(meta, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    model = meta.get("modelId") or ""
                    if self._cost_total(usage) > 0 and not self._is_subscription(
                        self._provider_of(model if isinstance(model, str) else "")
                    ):
                        return True
        return False

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        for path, text in read_files_parallel(self._files()):
            self._parse_file(path, text.split("\n"), sessions)
        out: dict[str, dict] = {}
        for s in sessions.values():
            self._finalize(s)
            # Drop sessions with no recorded usage (launching zaly writes a settings-only
            # file); key by the canonical id (settings.sessionId over the dir name).
            if s["model_rows"]:
                out[s["sid"]] = s
        self._sessions = out
        return self._sessions

    def _parse_file(self, path: str, lines: list[str], sessions: dict[str, dict]) -> None:
        # One file per session (resume appends in place), keyed by the <uuid> dir name.
        dir_id = os.path.basename(os.path.dirname(path))
        s = sessions.setdefault(dir_id, self._new_session(dir_id))
        for line in lines:
            if '"type"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            ts = self._epoch(o.get("ts"))
            if ts is not None and (s["ts_min"] is None or ts < s["ts_min"]):
                s["ts_min"] = ts
            typ = o.get("type")
            if typ == "session-settings":
                settings = o.get("settings")
                if isinstance(settings, dict):
                    sid = settings.get("sessionId")
                    if isinstance(sid, str) and sid:
                        s["sid"] = sid
                    cwd = settings.get("workspace") or settings.get("cwd")
                    if isinstance(cwd, str) and cwd and not s["cwd"]:
                        s["cwd"] = cwd
                    m = settings.get("modelId")
                    if isinstance(m, str) and m:
                        s["model_setting"] = m
                continue
            if typ != "message":
                continue
            msg = o.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            mid = msg.get("id")
            mts = self._epoch(msg.get("ts"))
            rts = mts if mts is not None else ts
            if role == "user":
                txt = self._user_text(msg.get("content"))
                if txt.strip() and not s["title_prompt"]:
                    s["title_prompt"] = " ".join(txt.split())[:80]
                if mid is None or mid not in s["seen_msgs"]:
                    if mid is not None:
                        s["seen_msgs"].add(mid)
                    if txt.strip():
                        s["prompts"].append(
                            {"ts": rts or 0.0, "id": str(mid or rts or ""), "title": txt.strip()}
                        )
                    self._ctx_content(s["context"], msg.get("content"), "user")
                continue
            if role == "system":
                # zaly's compaction summaries and session-start snapshots ride in as
                # system messages (meta parts) -- context, but never the user's words.
                if mid is None or mid not in s["seen_msgs"]:
                    if mid is not None:
                        s["seen_msgs"].add(mid)
                    self._ctx_content(s["context"], msg.get("content"), "system")
                continue
            if role != "assistant":
                continue
            meta = msg.get("meta")
            if not isinstance(meta, dict) or not isinstance(meta.get("usage"), dict):
                continue
            if mid is not None:
                if mid in s["seen_msgs"]:
                    continue  # same assistant step re-read (defensive; one file per session)
                s["seen_msgs"].add(mid)
            self._apply_usage(s, msg, meta, rts)

    def _apply_usage(self, s: dict, msg: dict, meta: dict, ts=None) -> None:
        usage = meta["usage"]
        inp = self._int(usage.get("input"))
        out = self._int(usage.get("output"))
        cr = self._int(usage.get("cacheRead"))
        cw = self._int(usage.get("cacheWrite"))
        # zaly's `reasoning` is a subset of `output` (OpenAI's reasoning_tokens detail);
        # opentab's reasoning column is additive, so it stays 0 and output carries it all.
        if inp + out + cr + cw == 0:
            return
        model = meta.get("modelId") or s["model_setting"] or "unknown"
        if not isinstance(model, str) or not model:
            model = "unknown"
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
        metered = cost > 0 and not self._is_subscription(self._provider_of(model))
        if metered:
            acc["cost"] += cost  # metered route with real spend -> tokens stay priced
        else:
            # Subscription/plan route (cost is a list-price estimate, not spend) OR no
            # recorded cost -> mark these tokens unpriced so the "$" view estimates them.
            acc["u_input"] += inp
            acc["u_output"] += out
            acc["u_cache_read"] += cr
            acc["u_cache_write"] += cw
        # One Turns row per assistant message; a subscription turn stays $0 so the tab's
        # "$" view reprices it from the token columns (as the rollups do). The step's
        # tool-call content parts feed tool_breakdown (duplicates kept: two bash calls =
        # two calls, two shares).
        tools = [
            c.get("name")
            for c in (msg.get("content") or [])
            if isinstance(c, dict) and c.get("type") == "tool-call" and c.get("name")
        ]
        self._ctx_content(s["context"], msg.get("content"), "assistant")
        s["turns"].append(
            {
                "ts": ts or 0.0,  # epoch seconds; sorts numerically
                "depth": 0,  # zaly's subagent transcripts live in tmpdir, not the tree
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
            }
        )

    # --- context composition (the Context tab) --------------------------------
    # Estimated at chars/4, mirroring zaly's own /context command (its
    # estimatePart walks the same part types with the same flat attachment
    # guesses), so opentab's Zaly composition stays comparable to zaly's. The
    # system prompt and tool schemas are rebuilt live by zaly and never persisted,
    # so they can't appear here -- the renderer shows the measured first-turn
    # baseline for that.

    def _ctx_content(self, ctx: dict, content, role: str) -> None:
        if isinstance(content, str):
            if role == "assistant":
                context_add(ctx, "assistant text", "", est_tokens(content))
            elif role == "system":  # e.g. a compaction summary -- never the user's words
                context_add(ctx, "injected context", "system", est_tokens(content))
            else:
                context_add(ctx, "user prompts", "", est_tokens(content.strip()))
            return
        if isinstance(content, list):
            for p in content:
                if isinstance(p, dict):
                    self._ctx_part(ctx, p, role)

    def _ctx_part(self, ctx: dict, p: dict, role: str) -> None:
        pt = p.get("type")
        if pt == "text":
            if role == "assistant":
                context_add(ctx, "assistant text", "", est_tokens(p.get("text") or ""))
            elif role == "system":
                context_add(ctx, "injected context", "system", est_tokens(p.get("text") or ""))
            else:
                context_add(ctx, "user prompts", "", est_tokens((p.get("text") or "").strip()))
        elif pt == "reasoning":
            context_add(ctx, "reasoning", "", est_tokens(p.get("text") or ""))
        elif pt == "tool-call":
            name = p.get("name") or "(unknown)"
            try:
                params = json.dumps(p.get("params") or {})
            except (TypeError, ValueError):
                params = str(p.get("params") or "")
            context_add(ctx, "tool call params", name, est_tokens(params))
        elif pt == "tool-result":
            name = p.get("name") or "(unknown)"
            body = p.get("content") if p.get("content") is not None else p.get("result")
            context_add(ctx, "tool results", name, self._est_content_tokens(body))
        elif pt in ATTACHMENT_EST_TOKENS:
            context_add(ctx, "attachments", pt, ATTACHMENT_EST_TOKENS[pt])
        elif pt in ("meta", "error"):
            kind = str(p.get("tag") or pt)
            data = p.get("data")
            try:
                blob = json.dumps(data) if data is not None else (p.get("text") or "")
            except (TypeError, ValueError):
                blob = str(data)
            context_add(ctx, "injected context", kind, est_tokens(blob))

    @classmethod
    def _est_content_tokens(cls, content) -> int:
        # A tool-result body: bare string, or nested parts (zaly's estimateContent).
        if isinstance(content, str):
            return est_tokens(content)
        total = 0
        if isinstance(content, list):
            for x in content:
                if not isinstance(x, dict):
                    total += est_tokens(str(x))
                elif x.get("type") in ATTACHMENT_EST_TOKENS:
                    total += ATTACHMENT_EST_TOKENS[x["type"]]
                else:
                    total += est_tokens(x.get("text") or "")
        elif isinstance(content, dict):
            try:
                total += est_tokens(json.dumps(content))
            except (TypeError, ValueError):
                total += est_tokens(str(content))
        return total

    def context_breakdown(self, workflow_id: str) -> list[dict]:
        # Estimated composition rows for the Context tab; the measured growth curve
        # comes from the turn rows, not from here.
        s = self._parse().get(workflow_id)
        return context_rows(s["context"]) if s else []

    def supports_context(self, workflow_id: str) -> bool:
        # session.jsonl always carries full message content, so composition applies
        # to every session.
        return True

    def _finalize(self, s: dict) -> None:
        sid = s["sid"]
        s["title"] = s["title_prompt"] or "(untitled)"
        s["directory"] = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"
        s["created_at"] = self._fmt_epoch(s["ts_min"])
        rows: list[dict] = []
        for model_name, acc in s["models"].items():
            # Per-model priced/unpriced split (HermesStore pattern): metered messages
            # contribute real cost (and stay out of the unpriced split); subscription
            # messages contribute the unpriced tokens the "$" view estimates. The two
            # accumulate independently per message, so a model mixing both routes is split
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
                    "reasoning": acc["reasoning"],
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

    # --- Store interface -----------------------------------------------------
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
                    subagents=0,  # subagent transcripts are ephemeral (tmpdir), not folded
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
        # Mirror PiStore._demo_workflow: anonymize, backfill a synthetic price for any
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

    # --- Turns/Tools tab opt-ins ----------------------------------------------
    def message_timeline(self, workflow_id: str) -> list[dict]:
        # Chronological per-turn rows for the Turns tab (the ClaudeStore pattern, on
        # epoch-seconds timestamps): walking the two time-sorted streams in lockstep tags
        # each turn with the latest prompt at ts <= the turn's ts. Real rows --
        # App._scale_demo_turns hides magnitudes in demo.
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
        # Per-(tool, model) token attribution for the Tools tab: each assistant message is
        # one LLM step whose tokens (and, on a metered route, real cost) are split evenly
        # across its tool-call parts -- the Store.tool_breakdown semantics off the
        # in-memory turn rows. A subscription row stays $0 so "$" reprices it.
        s = self._parse().get(workflow_id)
        return tool_rows_from_turns(s["turns"]) if s else []

    def supports_tools(self, workflow_id: str) -> bool:
        # zaly records every step's tool-call parts, so the tab applies to every session;
        # one without tool calls shows the honest empty message.
        return True
