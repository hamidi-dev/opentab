"""GitHub Copilot CLI OpenTelemetry backend."""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sqlite3
from datetime import datetime, timezone

from opentab.accounting.models import Workflow
from opentab.accounting.pricing import api_equivalent_cost, canonical_model, model_family
from opentab.accounting.tiers import attach_node_pricing, attach_pricing
from opentab.demo import demo_config, scramble_node, scramble_workflow
from opentab.stores.copilot_events import CopilotEvents, call_key, local_time, node_id
from opentab.util import git_root, read_files_parallel, tool_rows_from_turns


class CopilotStore:
    """Read GitHub Copilot CLI's opt-in OpenTelemetry file export.

    A call may appear as chat, inference, agent-turn, and summary records; retain that
    fidelity order and deduplicate by response/trace identity. Input includes cache reads/writes
    and reasoning remains inside output. Chat spans record GitHub's AI-credit charge
    (``github.copilot.nano_aiu``), kept as recorded spend; calls without it stay unpriced
    for "$". OTEL records no prompt text. Session cwd and title are enriched read-only
    from the sibling ``session-store.db``.
    """

    records_reasoning = True  # readable reasoningText/blocks when the model records them
    combined = False
    source_name = "Copilot"
    BACKGROUND_PROJECT = "(Copilot background calls)"

    # GitHub AI credits: 1e9 nano-AIU per credit, $0.01 per credit.
    _NANO_AIU_ATTR = "github.copilot.nano_aiu"
    _USD_PER_NANO_AIU = 1e-11
    _FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write", "tokens_total")
    # OTEL GenAI semantic-convention attribute names.
    _MODEL_ATTRS = ("gen_ai.response.model", "gen_ai.request.model")
    # (attribute, priority); the highest-priority present id wins, first listed breaks ties.
    _SESSION_ATTRS = (
        ("gen_ai.conversation.id", 3),
        ("copilot_chat.session_id", 3),
        ("copilot_chat.chat_session_id", 3),
        ("session.id", 3),
        ("github.copilot.interaction_id", 2),
        ("gen_ai.response.id", 1),
    )

    def __init__(self, root_dir: str, args: argparse.Namespace):
        self.root_dir = root_dir
        self.args = args
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._sessions: dict[str, dict] | None = None
        self._records_cost: bool | None = None  # resolved lazily (records_cost property)
        self._git_root_cache: dict[str, str] = {}
        self._meta: dict[str, tuple[str, str]] | None = None  # id -> (cwd, summary)
        self._meta_complete = False  # the session store was read successfully
        self._clients: dict[str, str] = {}  # id -> session_client() label
        self._links_cache: dict[str, str] | None = None  # child session -> parent session
        self._app_titles: dict[str, str] = {}  # desktop app's own session titles
        self._parts: dict[str, dict] = {}  # every parsed session, before folding
        self._root_index: dict[str, str] = {}  # folded child session -> its root
        # GitHub's documented single-file exporter target (can live outside root_dir).
        self._extra_file = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH") or ""
        # session-store.db sits beside the otel/ directory -- the only place cwd lives.
        self._db_path = os.path.join(
            os.path.dirname(os.path.normpath(root_dir)), "session-store.db"
        )
        # The desktop app's own database records which session created which.
        self._app_db_path = os.path.join(os.path.dirname(os.path.normpath(root_dir)), "data.db")
        self._events = CopilotEvents(os.path.dirname(os.path.normpath(root_dir)))

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

    @property
    def records_cost(self) -> bool:
        # True iff any kept call carries an AI-credit charge. Lazy so construction never
        # reads the export; the probe runs only when asked before any parse.
        if self._sessions is not None:
            return any(s["total_cost"] > 0 for s in self._sessions.values())
        if self._records_cost is None:
            self._records_cost = self._probe_records_cost()
        return self._records_cost

    def _probe_records_cost(self) -> bool:
        needle = f'"{self._NANO_AIU_ATTR}"'
        for path in self._files():
            try:
                fh = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if needle not in line:
                        continue
                    rec = self._loads(line)
                    attrs = rec.get("attributes") if isinstance(rec, dict) else None
                    if isinstance(attrs, dict) and self._nano_aiu_usd(attrs) > 0:
                        return True
        return False

    @classmethod
    def _nano_aiu_usd(cls, attrs: dict) -> float:
        value = attrs.get(cls._NANO_AIU_ATTR)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        if not math.isfinite(value) or value <= 0:
            return 0.0
        return value * cls._USD_PER_NANO_AIU

    @classmethod
    def _new_acc(cls) -> dict:
        acc = {
            "runs": 0,
            "cost": 0.0,  # recorded AI-credit spend in USD
            "input": 0,  # uncached input (OTEL input_tokens minus cache reads/writes)
            "output": 0,  # includes reasoning, so it is priced once, never twice
            "reasoning": 0,  # kept 0 (inside output)
            "cache_read": 0,
            "cache_write": 0,
            "tokens_total": 0,
        }
        acc.update({"unpriced_" + field: 0 for field in cls._FIELDS})
        return acc

    @classmethod
    def _add(cls, acc: dict, turn: dict) -> None:
        # Calls without a recorded charge stay unpriced so "$" estimates only those.
        acc["runs"] += 1
        acc["cost"] += turn["cost"]
        for field in cls._FIELDS:
            acc[field] += turn[field]
            if not turn["cost"]:
                acc["unpriced_" + field] += turn[field]

    @staticmethod
    def _new_session() -> dict:
        return {"cwd": None, "ts_min": None, "ts_max": None, "models": {}, "turns": []}

    @staticmethod
    def _num(value) -> int:
        # OTEL numbers arrive as ints, floats or numeric strings; non-numeric/neg -> 0.
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return int(value) if value >= 0 else 0
        if isinstance(value, str):
            try:
                n = int(float(value.strip()))
            except ValueError:
                return 0
            return n if n >= 0 else 0
        return 0

    @classmethod
    def _attr_num(cls, attrs: dict, *keys: str) -> int:
        # First strictly-positive value among the aliases.
        for k in keys:
            n = cls._num(attrs.get(k))
            if n > 0:
                return n
        return 0

    @staticmethod
    def _attr_str(attrs: dict, *keys: str) -> str | None:
        for k in keys:
            v = attrs.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    @staticmethod
    def _nested_str(rec: dict, obj: str, key: str) -> str | None:
        o = rec.get(obj)
        if isinstance(o, dict) and isinstance(o.get(key), str) and o[key].strip():
            return o[key].strip()
        return None

    @classmethod
    def _trace_id(cls, rec: dict) -> str | None:
        v = rec.get("traceId")
        if isinstance(v, str) and v.strip():
            return v.strip()
        return cls._nested_str(rec, "spanContext", "traceId")

    @classmethod
    def _span_id(cls, rec: dict) -> str | None:
        v = rec.get("spanId")
        if isinstance(v, str) and v.strip():
            return v.strip()
        return cls._nested_str(rec, "spanContext", "spanId")

    @classmethod
    def _session_attr(cls, attrs: dict) -> tuple[str | None, int]:
        best, best_prio = None, -1
        for key, prio in cls._SESSION_ATTRS:
            v = cls._attr_str(attrs, key)
            if v is not None and prio > best_prio:
                best, best_prio = v, prio
        return best, best_prio

    @staticmethod
    def _is_span(rec: dict) -> bool:
        if isinstance(rec.get("type"), str):
            return rec["type"] == "span"
        # No explicit type: a span has a name plus span-ish fields.
        return isinstance(rec.get("name"), str) and any(
            k in rec for k in ("spanId", "traceId", "startTime", "endTime", "duration", "kind")
        )

    @staticmethod
    def _body(rec: dict) -> str:
        for k in ("body", "_body"):
            v = rec.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    @classmethod
    def _classify(cls, rec: dict, attrs: dict) -> str | None:
        # Which of the four redundant OTEL shapes this record is (or None to ignore it).
        op = cls._attr_str(attrs, "gen_ai.operation.name")
        name = rec.get("name") if isinstance(rec.get("name"), str) else ""
        if cls._is_span(rec):
            if op == "chat" or name.startswith("chat "):
                return "chat"
            if op == "invoke_agent" or name.startswith("invoke_agent "):
                return "agent_summary"
            return None
        event = cls._attr_str(attrs, "event.name")
        body = cls._body(rec)
        if event == "gen_ai.client.inference.operation.details" or body.startswith(
            "GenAI inference:"
        ):
            return "inference"
        if event == "copilot_chat.agent.turn" or body.startswith("copilot_chat.agent.turn"):
            return "agent_turn"
        return None

    @classmethod
    def _record_ts_ms(cls, rec: dict) -> int | None:
        # OTEL timestamps: [seconds, nanos] pairs, epoch scalars (s/ms/us/ns), or unix ns.
        for k in ("endTime", "startTime", "hrTime", "_hrTime", "time"):
            v = rec.get(k)
            if isinstance(v, list) and len(v) >= 2:
                secs, nanos = cls._num(v[0]), cls._num(v[1])
                if secs:
                    return secs * 1000 + nanos // 1_000_000
        for k in ("timestamp", "observedTimestamp"):
            raw = cls._num(rec.get(k))
            if raw:
                if raw >= 100_000_000_000_000_000:  # nanoseconds
                    return raw // 1_000_000
                if raw >= 100_000_000_000_000:  # microseconds
                    return raw // 1_000
                if raw >= 100_000_000_000:  # milliseconds
                    return raw
                return raw * 1000  # seconds
        nano = cls._num(rec.get("timeUnixNano"))
        return nano // 1_000_000 if nano else None

    @staticmethod
    def _file_mtime_ms(path: str) -> int | None:
        try:
            return int(os.path.getmtime(path) * 1000)
        except OSError:
            return None

    @classmethod
    def _dedup_key(cls, source, rec, attrs, tid, sid, ts_ms, idx) -> str:
        span = cls._span_id(rec)
        if source in ("chat", "agent_summary"):
            if tid and span:
                return f"{tid}:{span}"
            return f"span:{sid}:{ts_ms}:{idx}"
        if source == "inference":
            if tid and span:
                return f"log:{tid}:{span}"
            return f"log:{sid}:{ts_ms}:{idx}"
        turn = cls._attr_num(attrs, "turn.index", "copilot_chat.turn.index")
        turn_s = str(turn) if turn else f"idx-{idx}"
        return f"agent-turn:{tid}:{turn_s}" if tid else f"agent-turn:{sid}:{turn_s}:{idx}"

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

    def _load_meta(self) -> dict[str, tuple[str, str]]:
        if self._meta is not None:
            return self._meta
        meta: dict[str, tuple[str, str]] = {}
        self._meta_complete = False
        if os.path.exists(self._db_path):
            try:
                con = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
                try:
                    for sid, cwd, summary in con.execute("SELECT id, cwd, summary FROM sessions"):
                        meta[sid] = (cwd or "", " ".join((summary or "").split())[:80])
                    self._meta_complete = True
                finally:
                    con.close()
            except sqlite3.Error:
                meta = {}  # foreign/locked/old schema -> just skip enrichment
        self._meta = meta
        return meta

    def _workspace(self, sid: str) -> dict[str, str]:
        """Top-level scalars from ``session-state/<id>/workspace.yaml``, read-only."""
        path = self._events.path(sid)
        if path is None:
            return {}
        workspace = os.path.join(os.path.dirname(path), "workspace.yaml")
        if os.path.realpath(workspace) != os.path.abspath(workspace):
            return {}
        out = {}
        block = None  # key collecting an indented block scalar (`name: |-` + lines)
        try:
            fh = open(workspace, encoding="utf-8", errors="replace")
        except OSError:
            return {}
        with fh:
            for line in fh:
                line = line.rstrip("\n")
                if block is not None and (not line.strip() or line[:1] in " \t"):
                    out[block] = f"{out[block]}\n{line.strip()}" if out[block] else line.strip()
                    continue
                block = None
                key, sep, value = line.partition(":")
                if not sep or not key.isidentifier() or (value and value[0] != " "):
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                elif value[:1] in ("|", ">") and value.lstrip("|>").strip("+-0123456789") == "":
                    block, value = key, ""
                out[key] = value
        return {k: v.strip() for k, v in out.items()}

    # client_name -> label. The CLI runtime also serves the desktop app and IDE plugins.
    _CLIENTS = {
        "github/cli": "Copilot CLI",
        "github/autopilot": "GitHub Copilot app",
        "copilot-intellij": "JetBrains IDE",
        "vscode-agent-host": "VS Code (Copilot CLI)",
    }

    def session_client(self, workflow_id: str) -> str:
        """Which Copilot front end ran the session, or "" when not recorded."""
        # Painted with the overview, so memoized; never forces the ledger parse.
        if self.demo:
            return ""
        if workflow_id not in self._clients:
            raw = self._workspace(workflow_id).get("client_name", "")
            self._clients[workflow_id] = self._CLIENTS.get(raw, raw)
        return self._clients[workflow_id]

    def _retained(self, sid: str) -> bool:
        """False only when the session store is readable and neither it nor
        session-state kept this session: the client deleted it after use."""
        if not self._meta_complete or sid in self._load_meta():
            return True
        path = self._events.path(sid)
        return path is None or os.path.isdir(os.path.dirname(path))

    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore).
        # session-store.db belongs here even though it holds no usage: _finalize reads
        # each session's title and cwd from it, so a rollup parsed before the CLI wrote
        # it was served from cache forever -- reload re-fingerprints, still matches,
        # still hits -- freezing those sessions at "(untitled)" / "(unknown)", which
        # also keeps them out of their project in Projects mode.
        # New rows land in the WAL long before a checkpoint touches the main file.
        files = self._files()
        files.extend(
            p
            for p in (
                self._db_path,
                self._db_path + "-wal",
                self._app_db_path,  # parent links fold children into their root
                self._app_db_path + "-wal",
            )
            if os.path.isfile(p)
        )
        files.extend(glob.glob(os.path.join(self._events.root, "*", "events.jsonl")))
        files.extend(glob.glob(os.path.join(self._events.root, "*", "workspace.yaml")))
        return files

    def _files(self) -> list[str]:
        files = glob.glob(os.path.join(self.root_dir, "**", "*.jsonl"), recursive=True)
        # Add the env-var export file, but only if it isn't already one of the globbed
        # ones -- the fallback dedup keys are index-based, so reading the same file
        # twice would double-count records that lack trace/span ids.
        if self._extra_file and os.path.isfile(self._extra_file):
            seen = {os.path.realpath(f) for f in files}
            if os.path.realpath(self._extra_file) not in seen:
                files.append(self._extra_file)
        return files

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        # OTEL exporters write spans and logs to DIFFERENT files, so trace context, the
        # dedup coverage sets, and the seen keys must span all files: one call logged as
        # a chat span in file A and an inference log in file B is still one call. Two
        # passes -- collect every file's candidates first, then emit.
        per_file = [
            (path, self._file_records(text.split("\n")))
            for path, text in read_files_parallel(self._files())
        ]
        # Cross-file trace context: a model / session id seen anywhere on a trace fills
        # in for records on that trace that omit it.
        trace_ctx: dict[str, dict] = {}
        for _path, records in per_file:
            for rec in records:
                tid = self._trace_id(rec)
                if not tid:
                    continue
                attrs = rec["attributes"]
                ctx = trace_ctx.setdefault(tid, {"model": None, "session": None, "prio": -1})
                if ctx["model"] is None:
                    ctx["model"] = self._attr_str(attrs, *self._MODEL_ATTRS)
                sid, prio = self._session_attr(attrs)
                if sid is not None and prio > ctx["prio"]:
                    ctx["session"], ctx["prio"] = sid, prio
        candidates: list[dict] = []
        idx = 0  # global running index keeps the fallback dedup keys unique across files
        for path, records in per_file:
            for rec in records:
                c = self._to_candidate(rec, idx, trace_ctx, path)
                idx += 1
                if c is not None:
                    candidates.append(c)
        # Dedup: a chat span is the source of truth; an inference log is dropped when a
        # chat covers its trace/response, an agent-turn log when a chat or inference does,
        # an agent-summary span when any of the three do.
        levels = ("chat", "inference", "agent_turn")
        traces = {lvl: set() for lvl in levels}
        resps = {lvl: set() for lvl in levels}
        # Traces the level logged *without* a response id: the only case where a trace
        # may still stand in for one, since there is no per-call id to compare (see _emit).
        bare_traces = {lvl: set() for lvl in levels}
        for c in candidates:
            if c["source"] in traces:
                if c["trace_id"]:
                    traces[c["source"]].add(c["trace_id"])
                    if not c["response_id"]:
                        bare_traces[c["source"]].add(c["trace_id"])
                if c["response_id"]:
                    resps[c["source"]].add(c["response_id"])
        seen: set[str] = set()
        emitted: dict[str, list[dict]] = {}
        for c in candidates:
            if not self._emit(c, traces, resps, bare_traces) or c["dedup_key"] in seen:
                continue
            seen.add(c["dedup_key"])
            emitted.setdefault(c["session_id"], []).append(c)
        for sid, calls in emitted.items():
            if self._doubled(sid, calls):
                for c in calls:
                    self._halve(c)
            for c in calls:
                self._fold(sessions, c)
        for sid, s in sessions.items():
            self._finalize(sid, s)
        # Drop sessions with no recorded usage (non-LLM-only files): they would only add
        # $0 / 0-token rows to a spend browser.
        self._parts = {sid: s for sid, s in sessions.items() if s["model_rows"]}
        self._sessions = self._fold_children(self._parts)
        return self._sessions

    def _links(self) -> dict[str, str]:
        """Child session -> the session that created it, from the desktop app's database.

        ``create_session`` gives a child its own workspace; ``workspace_parent_links``
        nests it under the creator's workspace. Chat sessions use ``chat_parent_links``.
        Missing tables or an unreadable database mean no links, never an error.
        """
        if self._links_cache is not None:
            return self._links_cache
        links: dict[str, str] = {}
        titles: dict[str, str] = {}
        queries = (
            "SELECT cw.session_id, COALESCE(pw.session_id, l.creator_session_id)"
            " FROM workspace_parent_links l JOIN workspaces cw ON cw.id = l.child_workspace_id"
            " LEFT JOIN workspaces pw ON pw.id = l.parent_workspace_id",
            "SELECT session_id, parent_session_id FROM chat_parent_links",
        )
        if os.path.isfile(self._app_db_path):
            try:
                con = sqlite3.connect(f"file:{self._app_db_path}?mode=ro", uri=True)
            except sqlite3.Error:
                con = None
            if con is not None:
                try:
                    for query in queries:
                        try:
                            rows = con.execute(query).fetchall()
                        except sqlite3.Error:
                            continue  # an older app without this table
                        for child, parent in rows:
                            if (
                                isinstance(child, str)
                                and isinstance(parent, str)
                                and child
                                and parent
                                and child != parent
                            ):
                                links.setdefault(child, parent)
                    try:
                        rows = con.execute("SELECT id, title FROM sessions").fetchall()
                    except sqlite3.Error:
                        rows = []
                    for sid, title in rows:
                        if isinstance(sid, str) and isinstance(title, str) and title.strip():
                            titles[sid] = " ".join(title.split())[:80]
                finally:
                    con.close()
        self._links_cache, self._app_titles = links, titles
        return links

    def _tree_links(self) -> dict[str, str]:
        """The links minus every cycle member's own: cycle members become roots.

        Accounting and the conversation/change readers both start from these, so
        they agree on membership whatever usage the sessions recorded."""
        links = self._links()

        def cyclic(sid: str) -> bool:
            seen, parent = {sid}, links.get(sid)
            while parent is not None and parent not in seen:
                seen.add(parent)
                parent = links.get(parent)
            return parent == sid

        return {child: parent for child, parent in links.items() if not cyclic(child)}

    def session_family(self, root_id: str) -> list[tuple[str, str | None]]:
        """``root_id`` then every recorded descendant with its parent, pre-order.

        Read from the links alone, so the conversation and change readers can use it
        without the usage ledger. A descendant without a session file is skipped, and
        its own descendants attach to the nearest ancestor that has one."""
        children: dict[str, list[str]] = {}
        for child, parent in self._tree_links().items():
            children.setdefault(parent, []).append(child)
        family: list[tuple[str, str | None]] = [(root_id, None)]
        seen = {root_id}

        def walk(node: str, parent: str) -> None:
            for child in sorted(children.get(node, ())):
                if child in seen:
                    continue
                seen.add(child)
                path = self._events.path(child)
                if path is None or not os.path.isfile(path):
                    walk(child, parent)
                    continue
                family.append((child, parent))
                walk(child, child)

        walk(root_id, root_id)
        return family

    def _fold_children(self, parts: dict[str, dict]) -> dict[str, dict]:
        """Fold each child session into its root, as subagent nodes beneath it.

        A link to a session without usage is spliced to its nearest parsed ancestor;
        a cycle leaves its members as roots (``_tree_links``), so none remain."""
        links = self._tree_links()
        parent_of: dict[str, str] = {}
        for sid in parts:
            parent = links.get(sid)
            while parent is not None and parent not in parts:
                parent = links.get(parent)
            if parent is not None:
                parent_of[sid] = parent
        children: dict[str, list[str]] = {}
        for child, parent in parent_of.items():
            children.setdefault(parent, []).append(child)
        for kids in children.values():
            kids.sort(key=lambda c: (parts[c]["ts_min"] or 0, c))

        def walk(parent: str, depth: int, family: list[tuple[str, int]]) -> None:
            for child in children.get(parent, ()):
                family.append((child, depth))
                walk(child, depth + 1, family)

        roots: dict[str, dict] = {}
        self._root_index = {}
        for sid, s in parts.items():
            if sid in parent_of:
                continue
            family: list[tuple[str, int]] = []
            walk(sid, 1, family)
            roots[sid] = self._merge(sid, s, family, parts) if family else dict(s, family=[])
            self._root_index.update((child, sid) for child, _depth in family)
        return roots

    def _merge(self, sid: str, root: dict, family: list[tuple[str, int]], parts: dict) -> dict:
        s = dict(root, family=family)
        turns = list(root["turns"])
        models = {name: dict(acc) for name, acc in root["models"].items()}
        for child, depth in family:
            c = parts[child]
            for t in c["turns"]:
                turns.append(
                    dict(
                        t,
                        depth=t["depth"] + depth,
                        agent=t["agent"] if t["agent"] != "-" else "session",
                    )
                )
            for name, acc in c["models"].items():
                total = models.setdefault(name, self._new_acc())
                for key, value in acc.items():
                    total[key] += value
            s["subagents"] += 1 + c["subagents"]
            s["event_backed"] = s["event_backed"] or c["event_backed"]
        members = [root] + [parts[child] for child, _depth in family]
        starts = [m["ts_min"] for m in members if m["ts_min"] is not None]
        ends = [m["ts_max"] for m in members if m["ts_max"] is not None]
        s["ts_min"] = min(starts) if starts else None
        s["ts_max"] = max(ends) if ends else None
        s["created_at"] = self._ms_to_local(s["ts_min"]) if s["ts_min"] is not None else ""
        s["ended_at"] = self._ms_to_local(s["ts_max"]) if s["ts_max"] is not None else ""
        s["turns"], s["models"] = turns, models
        self._totals(sid, s)  # the root's own share stays root_models, its own calls only
        return s

    def _owner(self, root_id: str, execution: str) -> str | None:
        """The folded session that holds this node, or None."""
        s = self._parse().get(root_id)
        if not s:
            return None
        for sid in [root_id] + [child for child, _depth in s["family"]]:
            part = self._parts.get(sid)
            if part is not None and (
                execution == sid or any(a["id"] == execution for a in part["agents"].values())
            ):
                return sid
        return None

    _USAGE_FIELDS = ("input", "output", "cache_read", "cache_write")

    def _doubled(self, sid: str, calls: list[dict]) -> bool:
        """Whether the exporter reported every usage value twice over.

        The JetBrains plugin (1.19) does: all of its token counts are even, its charges
        reproduce from those counts at list rates, yet the runtime's own running total
        (``session.usage_checkpoint`` in session-state) and GitHub's quota both move by
        half. The CLI and desktop app match their checkpoints exactly. Both signals are
        required, so a fixed exporter or an unrelated session is never halved. Only the
        JetBrains client qualifies: a running CLI session's earlier checkpoint can sit in
        the same window, and its counts may happen to be all even.
        """
        if self._workspace(sid).get("client_name") != "copilot-intellij":
            return False
        charged = sum(c["cost"] for c in calls)
        if charged <= 0 or any(c[k] % 2 for c in calls for k in self._USAGE_FIELDS):
            return False
        checkpoint = None
        for event in self._events.records(sid, {"session.usage_checkpoint"}):
            value = event["data"].get("totalNanoAiu")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                checkpoint = value * self._USD_PER_NANO_AIU
        # Exactly half, or a little over when the export also dropped a call outright.
        return checkpoint is not None and 0.45 <= checkpoint / charged <= 0.75

    @classmethod
    def _halve(cls, c: dict) -> None:
        for k in cls._USAGE_FIELDS:
            c[k] //= 2
        if c["context_tokens"] is not None:
            c["context_tokens"] //= 2
        c["cost"] /= 2
        # The doubled context can cross a long-context price tier the real call never
        # reached, so a swapped model pair is matched again against the true charge.
        attrs, fallback = c["model_attrs"]
        inp = c["input"] + c["cache_read"] + c["cache_write"]
        model = cls._call_model(
            attrs, inp, c["output"], c["cache_read"], c["cache_write"], charged=c["cost"]
        )
        c["model"] = cls._prefix_model(model or fallback or "unknown")

    @classmethod
    def _file_records(cls, lines: list[str]) -> list[dict]:
        return [
            o
            for line in lines
            if '"attributes"' in line
            for o in (cls._loads(line),)
            if isinstance(o, dict)
            and isinstance(o.get("attributes"), dict)
            and not cls._vscode_extension(o)
        ]

    _VSCODE_EXTENSION = "copilot-chat"

    @classmethod
    def _vscode_extension(cls, rec: dict) -> bool:
        """VS Code's Copilot Chat extension exports into the same file when the shell sets
        the path. Its chats belong to the VS Code harness, which reads them from VS Code's
        own store; counting them here too would double them, and its helper calls would
        surface as background calls."""
        scope = rec.get("instrumentationScope")
        if isinstance(scope, dict) and scope.get("name") == cls._VSCODE_EXTENSION:
            return True
        res = rec.get("resource")
        if not isinstance(res, dict):
            return False
        attrs = res.get("attributes")
        if not isinstance(attrs, dict):
            raw = res.get("_rawAttributes")
            attrs = (
                dict(p for p in raw if isinstance(p, list) and len(p) == 2)
                if isinstance(raw, list)
                else {}
            )
        return attrs.get("service.name") == cls._VSCODE_EXTENSION

    @staticmethod
    def _loads(line: str):
        try:
            return json.loads(line)
        except ValueError:
            return None

    @staticmethod
    def _emit(c: dict, traces: dict, resps: dict, bare_traces: dict) -> bool:
        tid, rid, src = c["trace_id"], c["response_id"], c["source"]

        def covered(lvl: str) -> bool:
            # A response id names ONE LLM call; a trace id groups every operation in a
            # turn, and an agentic turn makes several calls on one trace. Treating a
            # matching trace as proof of a duplicate therefore let a single chat span
            # suppress every *other* call logged under it -- real tokens, silently gone.
            # So when this record names its response, only the same response counts...
            if rid is not None:
                if rid in resps[lvl]:
                    return True
                # ...except against a record of that level on the same trace that named
                # no response at all: nothing can tell the two apart, and OTel makes
                # gen_ai.response.id recommended rather than required, so assume the
                # conservative reading (same call) instead of counting it twice.
                # Known limit: one response-less record then covers EVERY named call on
                # its trace, so a bare chat span beside two named inference logs still
                # keeps only the span. Which named call it duplicates is not knowable,
                # and guessing one would drop an arbitrary set of tokens instead of a
                # defensible one -- the conventions put response.id on the chat span, so
                # that shape needs an exporter that omits it exactly where it matters.
                return tid is not None and tid in bare_traces[lvl]
            return tid is not None and tid in traces[lvl]

        if src == "chat":
            return True
        if src == "inference":
            return not covered("chat")
        if src == "agent_turn":
            return not covered("chat") and not covered("inference")
        return not covered("chat") and not covered("inference") and not covered("agent_turn")

    @classmethod
    def _call_model(
        cls,
        attrs: dict,
        inp: int,
        out: int,
        cache_read: int,
        cache_write: int,
        charged: float | None = None,
    ) -> str | None:
        resp = cls._attr_str(attrs, "gen_ai.response.model")
        req = cls._attr_str(attrs, "gen_ai.request.model")
        if not (resp and req) or resp == req:
            return resp or req
        # The JetBrains plugin cross-wires these between a main agent and the subagents it
        # runs in parallel: the call that starts them gets the subagent's response model,
        # every later main-agent call its request model. Neither attribute is reliable, but
        # GitHub bills at its list rates, so exactly one candidate reproduces the charge
        # (verified on every swapped call, Claude/GPT and GPT/GPT pairs alike).
        if charged is None:
            charged = cls._nano_aiu_usd(attrs)
        if charged > 0:
            fits = [
                m
                for m in (resp, req)
                if abs(
                    api_equivalent_cost(
                        f"github-copilot/{m.rsplit('/', 1)[-1]}",
                        inp - cache_read - cache_write,
                        out,
                        0,
                        cache_read,
                        cache_write,
                        context_tokens=inp,
                    )
                    - charged
                )
                <= charged * 0.01
            ]
            if len(fits) == 1:
                return fits[0]
        # Uncharged or unpriced: an Anthropic message id still names the vendor.
        if (cls._attr_str(attrs, "gen_ai.response.id") or "").startswith("msg_"):
            claude = [m for m in (resp, req) if model_family(m) == "anthropic"]
            if len(claude) == 1:
                return claude[0]
        return resp

    def _to_candidate(self, rec: dict, idx: int, trace_ctx: dict, path: str) -> dict | None:
        attrs = rec["attributes"]
        source = self._classify(rec, attrs)
        if source is None:
            return None
        inp = self._attr_num(attrs, "gen_ai.usage.input_tokens")
        out = self._attr_num(attrs, "gen_ai.usage.output_tokens")
        cache_read = self._attr_num(attrs, "gen_ai.usage.cache_read.input_tokens")
        cache_write = self._attr_num(
            attrs,
            "gen_ai.usage.cache_write.input_tokens",
            "gen_ai.usage.cache_creation.input_tokens",
        )
        reasoning = self._attr_num(
            attrs, "gen_ai.usage.reasoning.output_tokens", "gen_ai.usage.reasoning_tokens"
        )
        total = self._attr_num(attrs, "gen_ai.usage.total_tokens", "gen_ai.usage.total.token_count")
        # Copilot's input_tokens includes BOTH cache reads and writes. Its shutdown
        # tokenDetails confirms the disjoint split (17984 input, 17981 writes -> 3
        # uncached); adding writes on top almost doubled a cold request's usage.
        cache_read = min(inp, cache_read)
        cache_write = min(inp - cache_read, cache_write)
        uncached = inp - cache_read - cache_write
        # output_tokens already includes reasoning: GitHub's AI-credit charge reproduces
        # from output alone, never output + reasoning. A bare reasoning count still counts.
        out = max(out, reasoning)
        # Some exporters log only a grand total; back-fill the gap as output.
        out += max(0, total - (uncached + out + cache_write + cache_read))
        if uncached + out + cache_write + cache_read == 0:
            return None  # a non-usage record (metric, tool span, ...)
        tid = self._trace_id(rec)
        ctx = trace_ctx.get(tid) if tid else None
        fallback = ctx["model"] if ctx else None
        model = self._call_model(attrs, inp, out, cache_read, cache_write) or fallback
        # Take the trace's best session id when it outranks this record's own. Keeping
        # only the local one meant a record whose sole session attribute is the per-call
        # gen_ai.response.id (priority 1) held onto it even when a sibling on the same
        # trace named the real conversation (priority 3) -- so one conversation shattered
        # into a session per LLM call, each of them missing the title and cwd that
        # session-store.db enriches by session id.
        sid, prio = self._session_attr(attrs)
        if ctx and ctx["session"] is not None and (sid is None or ctx["prio"] > prio):
            sid = ctx["session"]
        if sid is None:
            sid = tid or "unknown-session"
        ts_ms = self._record_ts_ms(rec)
        if ts_ms is None:
            ts_ms = self._file_mtime_ms(path)
        return {
            "source": source,
            "trace_id": tid,
            "response_id": self._attr_str(attrs, "gen_ai.response.id"),
            "model": self._prefix_model(model or "unknown"),
            "model_attrs": (attrs, fallback),
            "session_id": sid,
            "ts_ms": ts_ms,
            "input": uncached,
            # Fallback agent totals can span several calls. Only inference records
            # with a reported inclusive input count establish one request's size.
            "context_tokens": inp
            if source in ("chat", "inference") and "gen_ai.usage.input_tokens" in attrs
            else None,
            "output": out,
            "cost": self._nano_aiu_usd(attrs),
            "cache_read": cache_read,
            "cache_write": cache_write,
            "effort": self._attr_str(attrs, "gen_ai.request.reasoning.level") or "",
            "dedup_key": self._dedup_key(source, rec, attrs, tid, sid, ts_ms, idx),
        }

    def _fold(self, sessions: dict[str, dict], c: dict) -> None:
        s = sessions.setdefault(c["session_id"], self._new_session())
        ts = c["ts_ms"]
        if ts is not None and (s["ts_min"] is None or ts < s["ts_min"]):
            s["ts_min"] = ts
        if ts is not None and (s["ts_max"] is None or ts > s["ts_max"]):
            s["ts_max"] = ts
        # One Turns row per kept call. OTEL captures no prompt content by default, so
        # the rows are headerless (empty prompt_*); a call without a recorded charge
        # stays $0 and the tab's "$" view reprices it from its token columns.
        turn = {
            "ts": ts if ts is not None else 0,  # epoch ms; sorts numerically
            "depth": 0,
            "agent": "-",
            "content_key": call_key(c["session_id"], c["response_id"]) if c["response_id"] else "",
            "effort": c["effort"],
            "model_name": c["model"],
            "cost": c["cost"],
            "input": c["input"],
            "context_tokens": c["context_tokens"],
            "output": c["output"],
            "reasoning": 0,
            "cache_read": c["cache_read"],
            "cache_write": c["cache_write"],
            "tokens_total": c["input"] + c["cache_read"] + c["cache_write"] + c["output"],
            "session": c["session_id"],  # owner once child sessions fold into a root
        }
        s["turns"].append(turn)
        acc = s["models"].get(c["model"])
        if acc is None:
            acc = s["models"][c["model"]] = self._new_acc()
        self._add(acc, turn)

    def _finalize(self, sid: str, s: dict) -> None:
        cwd, summary = self._load_meta().get(sid, ("", ""))
        self._links()
        # The desktop app's own title is what its user sees; the session store's
        # summary of an app session is often just its first prompt.
        summary = self._app_titles.get(sid) or summary
        if not (cwd and summary):
            # Desktop and IDE sessions reach the session store late, if at all.
            workspace = self._workspace(sid)
            cwd = cwd or workspace.get("cwd", "")
            summary = summary or " ".join(workspace.get("name", "").split())[:80]
        s["title"] = summary or "(untitled)"
        s["directory"] = self._git_root(cwd) if cwd else "(unknown)"
        if not cwd and not summary and not self._retained(sid):
            # E.g. the desktop app's session-naming call: a throwaway session it
            # deletes after use. Its charge is real, its parent is not recorded.
            s["title"] = "(background call)"
            s["directory"] = self.BACKGROUND_PROJECT
        s["created_at"] = self._ms_to_local(s["ts_min"]) if s["ts_min"] is not None else ""
        s["ended_at"] = self._ms_to_local(s["ts_max"]) if s["ts_max"] is not None else ""
        # Only response/execution identities are retained here. Bodies and tools
        # remain lazy session-detail reads, never part of cached model/workflow rows.
        agents, ownership = self._events.metadata(sid)
        s["agents"] = agents
        s["event_backed"] = False
        root_models = {}
        for turn in s["turns"]:
            identity = ownership.get(turn["content_key"])
            aid = ""
            if identity is not None:
                candidate, model = identity
                if not model or model == canonical_model(turn["model_name"]):
                    aid = candidate
                    s["event_backed"] = True
            turn["execution_id"] = node_id(sid, aid)
            if aid:
                turn["depth"] = agents[aid]["depth"]
                turn["agent"] = agents[aid]["name"]
            else:
                self._add(root_models.setdefault(turn["model_name"], self._new_acc()), turn)
        s["root_models"] = root_models
        s["subagents"] = len(agents)
        self._totals(sid, s)

    def _totals(self, sid: str, s: dict) -> None:
        rows: list[dict] = []
        root_models = s["root_models"]
        for model_name, acc in s["models"].items():
            # Recorded cost is the AI-credit charge; uncharged calls stay unpriced.
            # Root shares exclude identified children.
            root = root_models.get(model_name, self._new_acc())
            rows.append(
                {
                    "root_id": sid,
                    "model_name": model_name,
                    "runs": acc["runs"],
                    "cost": round(acc["cost"], 6),
                    "root_cost": round(root["cost"], 6),
                    "tokens_total": acc["tokens_total"],
                    "input": acc["input"],
                    "reasoning": acc["reasoning"],
                    "cache_read": acc["cache_read"],
                    "cache_write": acc["cache_write"],
                    "output": acc["output"],
                    "unpriced_input": acc["unpriced_input"],
                    "unpriced_reasoning": acc["unpriced_reasoning"],
                    "unpriced_cache_read": acc["unpriced_cache_read"],
                    "unpriced_cache_write": acc["unpriced_cache_write"],
                    "unpriced_output": acc["unpriced_output"],
                    "root_tokens_total": root["tokens_total"],
                    "root_input": root["input"],
                    "root_output": root["output"],
                    "root_reasoning": root["reasoning"],
                    "root_cache_read": root["cache_read"],
                    "root_cache_write": root["cache_write"],
                    "root_unpriced_input": root["unpriced_input"],
                    "root_unpriced_reasoning": root["unpriced_reasoning"],
                    "root_unpriced_cache_read": root["unpriced_cache_read"],
                    "root_unpriced_cache_write": root["unpriced_cache_write"],
                    "root_unpriced_output": root["unpriced_output"],
                }
            )
        attach_pricing(rows, s["turns"])
        s["model_rows"] = rows
        s["total_tokens"] = sum(r["tokens_total"] for r in rows)
        s["total_cost"] = round(sum(acc["cost"] for acc in s["models"].values()), 6)
        s["root_cost"] = round(sum(acc["cost"] for acc in root_models.values()), 6)
        s["unpriced_tokens"] = sum(acc["unpriced_tokens_total"] for acc in s["models"].values())

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
        self._meta = None
        self._clients = {}
        self._links_cache = None
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
                    root_cost=s["root_cost"],
                    total_cost=s["total_cost"],
                    subagents=s["subagents"],
                    model_count=0,  # filled by App._load_model_cache
                    total_tokens=sum(r["tokens_total"] for r in model_rows),
                    unpriced_tokens=s["unpriced_tokens"],
                    source=self.source_name,
                    ended_at=s["ended_at"],
                )
            )
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return rows

    def _demo_workflow(self, w: Workflow) -> Workflow:
        # Mirror CodexStore._demo_workflow: anonymize, backfill a synthetic price for the
        # (all-unpriced) tokens, then scale by the hidden per-process factor.
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

    def recent_roots(self) -> list[dict]:
        # Unlike one-file-per-session harnesses, Copilot can share a single OTEL
        # exporter across all sessions. Membership/recency require its ledger scan.
        return sorted(
            [
                {"id": sid, "directory": s["directory"], "last_active": s["ts_max"] or 0}
                for sid, s in self._parse().items()
            ],
            key=lambda row: row["last_active"],
            reverse=True,
        )

    def root_of(self, session_id: str) -> str | None:
        sessions = self._parse()
        if session_id in sessions:
            return session_id
        if session_id in self._root_index:
            return self._root_index[session_id]
        roots = [
            root
            for root, s in sessions.items()
            for sid in [root] + [child for child, _depth in s["family"]]
            if any(session_id in (aid, a["id"]) for aid, a in self._parts[sid]["agents"].items())
        ]
        return roots[0] if len(roots) == 1 else None

    def status_nodes(self, workflow_id: str) -> list[dict]:
        # The cost path must price every recorded model, not each execution's
        # dominant-model label; model switches otherwise price the whole node wrong.
        s = self._parse().get(workflow_id)
        if not s:
            return []
        models = {row["model_name"]: row for row in s["model_rows"]}
        return [
            dict(
                self._node(
                    workflow_id, 0, "-", s["title"], s["created_at"], model, acc["cost"], acc
                ),
                # The status path covers the whole tree once per model. Reuse its
                # reconciled buckets rather than the execution's dominant label.
                model_pricing=[models[model]],
            )
            for model, acc in s["models"].items()
        ]

    def supports_conversation(self, workflow_id: str) -> bool:
        if self.demo:
            return False
        path = self._events.path(workflow_id)
        return path is not None and os.path.isfile(path)

    def conversation_source(self, root_id: str, execution_id: str | None = None) -> dict:
        from opentab.conversations.copilot import read_source

        return read_source(self, root_id, execution_id)

    def conversation_manifest(self, root_id: str):
        from opentab.conversations.copilot import manifest

        return manifest(self, root_id)

    def supports_changes(self, root_id: str) -> bool:
        return self.supports_conversation(root_id)

    def change_request(self, root_id: str, change_key: str | None = None):
        if not self.supports_changes(root_id):
            return None
        from opentab.stores.copilot_changes import ChangeRequest

        return ChangeRequest(self.root_dir, root_id, change_key)

    def session_change_files(self, root_id: str) -> dict:
        from opentab.stores.copilot_changes import read_changes

        return read_changes(self, root_id)

    def session_change_diff(self, root_id: str, change_key: str) -> dict | None:
        from opentab.conversations.reader import ConversationError
        from opentab.stores.copilot_changes import read_changes

        try:
            return read_changes(self, root_id, change_key)
        except ConversationError:
            return None

    def workflow_nodes(self, workflow_id: str) -> list[dict]:
        s = self._parse().get(workflow_id)
        if not s:
            return []
        executions = [(workflow_id, 0, "-", s["title"], s["created_at"])]

        # Pre-order rather than flattening concurrently running or nested children.
        def children(agents, parent, offset):
            for aid, agent in agents.items():
                if agent["parent"] == parent:
                    executions.append(
                        (
                            agent["id"],
                            agent["depth"] + offset,
                            agent["name"],
                            agent["name"],
                            local_time(agent["created"]),
                        )
                    )
                    children(agents, aid, offset)

        children(s["agents"], "", 0)
        for child, depth in s["family"]:
            part = self._parts[child]
            executions.append((child, depth, "session", part["title"], part["created_at"]))
            children(part["agents"], "", depth)
        nodes = []
        for execution, depth, agent, title, created in executions:
            acc = self._new_acc()
            models = {}
            for turn in s["turns"]:
                if turn["execution_id"] != execution:
                    continue
                self._add(acc, turn)
                models[turn["model_name"]] = models.get(turn["model_name"], 0) + 1
            best = max(models, key=models.get) if models else "unknown (not recorded)"
            nodes.append(
                self._node(execution, depth, agent, title, created, best, acc["cost"], acc)
            )
            if not self.demo:
                attach_node_pricing(
                    nodes[-1], (t for t in s["turns"] if t["execution_id"] == execution)
                )
        if self.demo:
            nodes = [self._demo_node(n) for n in nodes]
        return nodes

    def _demo_node(self, n: dict) -> dict:
        return scramble_node(n, self.demo_scale, self.demo_cats)

    def message_timeline(self, workflow_id: str) -> list[dict]:
        return self._timeline(workflow_id)

    def _timeline(self, workflow_id: str, execution: str | None = None) -> list[dict]:
        s = self._parse().get(workflow_id)
        if not s:
            return []
        turns = sorted(s["turns"], key=lambda r: r["ts"])
        if execution is not None:
            turns = [t for t in turns if t["execution_id"] == execution]
        if not self.demo:
            # Each folded session's events describe only its own calls.
            groups: dict[str, list[dict]] = {}
            for t in turns:
                groups.setdefault(t["session"], []).append(t)
            detailed: dict[int, dict] = {}
            for sid, group in groups.items():
                rows, _trace = self._events.details(sid, group, execution=execution)
                detailed.update(zip(map(id, group), rows))
            turns = [detailed[id(t)] for t in turns]
        out = []
        for t in turns:
            r = dict(t)
            r.pop("session", None)
            r["time"] = self._ms_to_local(r.pop("ts"))
            for field in ("prompt_id", "prompt_title", "prompt_full"):
                r.setdefault(field, "")
            if execution is not None:
                r["depth"] = 0
            out.append(r)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        return True

    def supports_turn_content(self, workflow_id: str) -> bool:
        return not self.demo and bool(self._parse().get(workflow_id, {}).get("event_backed"))

    def turn_content(self, workflow_id: str, content_key: str | None = None) -> dict:
        if self.demo:
            return {}
        s = self._parse().get(workflow_id)
        if not s:
            return {}
        groups: dict[str, list[dict]] = {}
        for t in s["turns"]:
            groups.setdefault(t["session"], []).append(t)
        merged = None
        for sid, group in groups.items():
            if content_key is not None and all(t["content_key"] != content_key for t in group):
                continue
            _rows, trace = self._events.details(sid, group, content_key=content_key, trace=True)
            if merged is None:
                merged = trace
            else:
                merged.update(trace)
        return merged if merged is not None else {}

    def tool_breakdown(self, workflow_id: str) -> list[dict]:
        return tool_rows_from_turns(self.message_timeline(workflow_id))

    def supports_tools(self, workflow_id: str) -> bool:
        return self.supports_turn_content(workflow_id)

    def context_breakdown(self, workflow_id: str) -> list[dict]:
        if self.demo or workflow_id not in self._parse():
            return []
        return self._events.context(workflow_id)

    def supports_context(self, workflow_id: str) -> bool:
        # Capability checks run while painting; composition itself stays lazy.
        return self.supports_turn_content(workflow_id)

    def node_prompt(self, root_id: str, execution: str) -> str | None:
        if self.demo or root_id == execution:
            return None
        owner = self._owner(root_id, execution)
        if owner is None:
            return None
        if owner == execution:
            # A child session's kickoff is its first prompt, sent by its creator.
            for event in self._events.records(owner, {"user.message"}):
                text = event["data"].get("content")
                if isinstance(text, str) and text.strip():
                    return text
            return None
        return self._events.prompt(owner, execution)

    def node_timeline(self, root_id: str, execution: str) -> list[dict] | None:
        if self.demo or self._owner(root_id, execution) is None:
            return None
        return self._timeline(root_id, execution)

    def node_turn_content(
        self, root_id: str, execution: str, content_key: str | None = None
    ) -> dict:
        rows = self.node_timeline(root_id, execution)
        if rows is None:
            return {}
        keys = {r["content_key"] for r in rows if r.get("content_key")}
        if content_key is not None and content_key not in keys:
            return {}
        return {
            key: events
            for key, events in self.turn_content(root_id, content_key).items()
            if key in keys
        }
