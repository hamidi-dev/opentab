"""Logged-API-request CSV backend."""
from __future__ import annotations

import argparse
import csv
import math
import os
from datetime import datetime, timezone

from opentab.demo import demo_config, scramble_node, scramble_workflow
from opentab.formatting import _clean_prompt, iso_to_epoch, iso_to_local, worked_seconds
from opentab.models import Workflow
from opentab.util import git_root, safe_float, safe_int, tool_rows_from_turns


class CsvStore:
    """Read a CSV of logged API requests (e.g. GitHub Copilot inside IntelliJ) behind
    the same interface App expects from Store: workflows(), summary(), workflow_nodes(),
    model_breakdown(), plus the .demo/.demo_scale attributes -- like the JSONL backends.

    CSV is already tabular, so this is the simplest backend: one row per API request
    (pre-aggregated rows work too), rolled up by session. Column names are matched
    case-insensitively with aliases, so the exporter has latitude. Required: a timestamp,
    a model, and input/output token counts. Everything else is optional:

        timestamp  time|date|created_at|ts        ISO-8601 or epoch (s/ms/us)
        model      model_id|model_name            e.g. claude-sonnet-4, gpt-4o, gemini-2.5-pro
        input      input_tokens|prompt_tokens     as logged (may include the cached read)
        output     output_tokens|completion_tokens includes reasoning (priced once at output)
        cached     cached_tokens|cache_read        cached portion of input (default 0)
        session    session_id|conversation_id      groups requests into one session
        request    request_id|req_id|id            stable per-request id (dedup)
        prompt     prompt_text|user_prompt          the user message -> Turns grouping
        prompt_id  prompt_id                        stable id for a prompt (optional)
        project    repo|workspace|directory|cwd    path -> git root; bare name as-is
        title      name|label                      session label (default first prompt)
        cost       cost_usd|credits                credits x $0.01; presence -> metered

    A logged API request carries no dollar cost (Copilot's usage-based credit billing is
    settled server-side), so a Copilot row is treated like an OpenCode *subscription* row:
    recorded cost $0, every token unpriced, and the normal "$" machinery reprices it at API
    list rates.
    But cost is handled per-row like HermesStore: if the CSV carries a cost_usd/credits
    column with positive values those rows price as real spend, so records_cost is a
    per-instance property (True iff any row has a recorded cost), resolved lazily --
    derived from a parse when one has run, else probed on first read (never in __init__,
    so the warm-start cache can answer it without touching the file).

    Models are mixed-provider, so each id is provider-prefixed (claude->anthropic/,
    gpt|o3->openai/, gemini->google/) for pricing and the Providers rollup. OpenAI-style
    accounting: input_tokens includes the cached read, so input is split into uncached +
    cache_read (cache_write stays 0). No subagent tree -- every session is one depth-0
    node. Sessions with no recorded token usage are dropped. Implements the **Turns**
    opt-in (message_timeline/supports_turns): each row is one turn, the optional
    `prompt` column grouping them under ▸ headers -- and the **Tools** opt-in
    (tool_breakdown/supports_tools, gated per session on the optional `tool`
    column being used): the row's tokens/cost split evenly across the tools it
    names ("Bash;Read"). JsonlStore, the per-line twin, inherits this machinery.
    """

    combined = False
    source_name = "CSV"

    # Accepted header (normalized: lowercased, spaces -> underscores) -> canonical field.
    _FIELD_ALIASES = {
        "timestamp": "timestamp",
        "time": "timestamp",
        "date": "timestamp",
        "created_at": "timestamp",
        "datetime": "timestamp",
        "ts": "timestamp",
        "model": "model",
        "model_id": "model",
        "model_name": "model",
        "input_tokens": "input",
        "prompt_tokens": "input",
        "input": "input",
        "output_tokens": "output",
        "completion_tokens": "output",
        "output": "output",
        "cached_tokens": "cached",
        "cache_read": "cached",
        "cache_read_tokens": "cached",
        "cached": "cached",
        "session_id": "session",
        "conversation_id": "session",
        "session": "session",
        "conversation": "session",
        "project": "project",
        "repo": "project",
        "repository": "project",
        "workspace": "project",
        "directory": "project",
        "dir": "project",
        "cwd": "project",
        "folder": "project",
        "path": "project",
        "title": "title",
        "name": "title",
        "label": "title",
        "prompt": "prompt",
        "prompt_text": "prompt",
        "user_prompt": "prompt",
        "prompt_id": "prompt_id",
        "tool": "tool",
        "tool_name": "tool",
        "tools": "tool",
        "request_id": "request",
        "req_id": "request",
        "id": "request",
        "cost_usd": "cost",
        "cost": "cost",
        "credits": "cost",
        "credit": "cost",
    }

    def __init__(self, csv_path: str, args: argparse.Namespace):
        self.csv_path = csv_path
        self.args = args
        # Demo mode: which categories to scramble (titles/turns/spend) and the
        # hidden magnitude factor (1.0 unless spend is scrambled). See demo_config.
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._sessions: dict[str, dict] | None = None  # parsed lazily / on reload
        self._git_root_cache: dict[str, str] = {}
        self._records_cost: bool | None = None  # resolved lazily (records_cost property)

    # --- header / value parsing ---------------------------------------------
    @classmethod
    def _resolve_headers(cls, fieldnames) -> tuple[dict[str, str], bool]:
        # Map canonical field -> the actual CSV header for it (first alias wins), plus a
        # flag for whether the matched cost column is a credits column (-> x $0.01).
        mapping: dict[str, str] = {}
        cost_is_credits = False
        for actual in fieldnames or []:
            norm = (actual or "").strip().lower().replace(" ", "_")
            canon = cls._FIELD_ALIASES.get(norm)
            if canon and canon not in mapping:
                mapping[canon] = actual
                if canon == "cost" and norm in ("credits", "credit"):
                    cost_is_credits = True
        return mapping, cost_is_credits

    @staticmethod
    def _to_int(raw) -> int:
        # Via float() because a cell may spell a count "1.0" or "1e3", then through the
        # one shared bound (util.safe_int/safe_float) the JSONL backends use -- a cell of
        # "1e400" is inf and "1e308" is a finite number that still poisons every sum it
        # reaches. The string cleaning above it is CSV's own.
        if raw is None:
            return 0
        s = str(raw).strip().replace(",", "")
        return safe_int(safe_float(s)) if s else 0

    @staticmethod
    def _to_float(raw) -> float:
        if raw is None:
            return 0.0
        s = str(raw).strip().replace(",", "").replace("$", "")
        return max(0.0, safe_float(s)) if s else 0.0

    @staticmethod
    def _split_tools(raw) -> list[str]:
        # The optional per-request tool call(s): "Bash", or several as "Bash;Read"
        # ("|" works too -- "," is the CSV delimiter). Duplicates kept: two Bash
        # calls in one request = two calls, two shares in tool_breakdown.
        if raw is None:
            return []
        return [p.strip() for p in str(raw).replace("|", ";").split(";") if p.strip()]

    @staticmethod
    def _parse_ts(raw) -> str:
        # Canonical local "YYYY-MM-DD HH:MM:SS" (matches Store/the JSONL backends). ISO
        # goes through iso_to_local; a bare number is an epoch (seconds/ms/us by scale).
        raw = (raw or "").strip()
        if not raw:
            return ""
        try:
            val = float(raw)
        except ValueError:
            return iso_to_local(raw)
        if val > 1e14:  # microseconds
            val /= 1e6
        elif val > 1e11:  # milliseconds
            val /= 1e3
        try:
            return (
                datetime.fromtimestamp(val, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except (OverflowError, OSError, ValueError):
            return ""

    @staticmethod
    def _parse_ts_epoch(raw) -> float | None:
        # The ABSOLUTE POSIX epoch for a raw timestamp (ISO or numeric epoch s/ms/us),
        # for worked-time arithmetic -- unlike _parse_ts's offset-free local string, a
        # gap computed from these survives a DST fall-back. None when empty/unparseable.
        if raw is None:
            return None
        s = str(raw).strip()
        if not s:
            return None
        try:
            val = float(s)
        except ValueError:
            return iso_to_epoch(s)
        if not math.isfinite(val):
            return None  # "inf"/"nan" parse as floats but would crash human_duration's int()
        if val > 1e14:  # microseconds
            val /= 1e6
        elif val > 1e11:  # milliseconds
            val /= 1e3
        return val

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
        # Mixed-provider ids: prefix the inferred provider so model_price() strips it the
        # same way and the Providers tab rolls up under anthropic/openai/google.
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

    def _resolve_dir(self, project: str) -> str:
        # A path folds to its git root (subdir launches roll up to the repo); a bare repo
        # name is used verbatim. Empty -> "(unknown)".
        project = (project or "").strip()
        if not project:
            return "(unknown)"
        if os.sep in project or project.startswith(("~", ".")):
            return self._git_root(project)
        return project

    @property
    def records_cost(self) -> bool:
        # True iff any row records a positive cost. Lazy so construction never reads the
        # file (the warm-start cache answers a hit without reaching here): after a parse
        # it derives from the accumulated per-model costs; the full-file probe runs only
        # when it is read before any parse.
        if self._sessions is not None:
            return any(
                acc["cost"] > 0 for s in self._sessions.values() for acc in s["models"].values()
            )
        if self._records_cost is None:
            self._records_cost = self._probe_records_cost()
        return self._records_cost

    def _probe_records_cost(self) -> bool:
        # True iff the CSV has a cost column with any positive value. Early-exits so it
        # stays cheap.
        try:
            with open(self.csv_path, newline="", encoding="utf-8", errors="replace") as fh:
                reader = csv.DictReader(fh)
                mapping, is_credits = self._resolve_headers(reader.fieldnames)
                col = mapping.get("cost")
                if not col:
                    return False
                for row in reader:
                    if self._to_float(row.get(col)) > 0:
                        return True
        except OSError:
            return False
        return False

    # --- accumulation --------------------------------------------------------
    @staticmethod
    def _new_acc() -> dict:
        return {
            "runs": 0,
            "input": 0,  # uncached input (the logged input minus the cached read)
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,  # OpenAI-style: no separate cache-write bill
            "tokens_total": 0,
            "cost": 0.0,
            # The unpriced half of the split, accumulated per row (see _accumulate).
            "u_input": 0,
            "u_output": 0,
            "u_cache_read": 0,
            "u_cache_write": 0,
        }

    @staticmethod
    def _accumulate(acc: dict, uncached: int, cached: int, out: int, cost: float) -> None:
        # Fold one request row into its (session, model) bucket. The priced/unpriced
        # split is decided PER ROW -- the PiStore pattern -- because a log legitimately
        # mixes routes: a row with a recorded cost is real spend, a $0 row leaves its
        # tokens in the unpriced_* split so the "$" view estimates them at list price.
        # Deciding it per bucket (from the summed cost) instead would let a single
        # metered row mark every subscription row sharing its model as priced, so those
        # tokens would land in neither the recorded spend nor the estimate and "$" could
        # not see them at all. JsonlStore shares this, so the two can never drift.
        acc["runs"] += 1
        acc["input"] += uncached
        acc["cache_read"] += cached
        acc["output"] += out
        acc["tokens_total"] += uncached + cached + out
        if cost > 0:
            acc["cost"] += cost
        else:
            acc["u_input"] += uncached
            acc["u_cache_read"] += cached
            acc["u_output"] += out

    @staticmethod
    def _new_session() -> dict:
        return {
            "created_at": "",
            "ended_at": "",
            "title": None,
            "project": "",
            "models": {},
            "turns": [],  # one per request row, for the Turns tab (chronological)
            "seen": set(),  # request ids already counted (regenerate/append dedup)
            "synthetic": False,  # minted (date, project) bucket, not a logged session id
        }

    # --- parsing -------------------------------------------------------------
    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        try:
            with open(self.csv_path, newline="", encoding="utf-8", errors="replace") as fh:
                reader = csv.DictReader(fh)
                mapping, cost_is_credits = self._resolve_headers(reader.fieldnames)
                for row in reader:
                    try:
                        self._parse_row(row, mapping, cost_is_credits, sessions)
                    except (ValueError, TypeError):
                        continue  # one bad row never sinks the file
        except OSError:
            self._sessions = {}
            return self._sessions
        for sid, s in sessions.items():
            self._finalize(sid, s)
        # Drop sessions with no recorded token usage (mirrors CodexStore).
        self._sessions = {sid: s for sid, s in sessions.items() if s["model_rows"]}
        return self._sessions

    def _parse_row(self, row, mapping, cost_is_credits, sessions: dict[str, dict]) -> None:
        def g(field):
            col = mapping.get(field)
            return row.get(col) if col else None

        inp = self._to_int(g("input"))
        out = self._to_int(g("output"))
        cached = self._to_int(g("cached"))
        cost = self._to_float(g("cost"))
        if cost_is_credits:
            cost *= 0.01
        # A cost-only row (no token counts) is still real spend; only rows with neither
        # tokens nor cost are skipped (header echo, blank line, metadata-only row).
        if inp == 0 and out == 0 and cached == 0 and cost <= 0:
            return
        ts = self._parse_ts(g("timestamp"))
        ts_epoch = self._parse_ts_epoch(g("timestamp"))  # absolute, for worked-time
        project = (g("project") or "").strip()
        sid = (g("session") or "").strip()
        synthetic = not sid
        if synthetic:
            # No session id: one synthetic session per (date, project) keeps the list
            # meaningful. Stable so reloads/merges don't churn ids.
            sid = self.SYNTHETIC_ID_PREFIX + (ts[:10] or "?") + "|" + (project or "?")
        s = sessions.setdefault(sid, self._new_session())
        # Remembered, not re-derived from the id: the id prefix is a naming convention
        # a real log is free to collide with (a `session_id` column literally reading
        # "csv:production"), and everything that keys off "is this a bucket of unrelated
        # conversations?" -- the Context curve, and now the Turns tab's compaction
        # markers -- would then be silently off for that user's real session. Sticky:
        # if ANY row landed here without an id, the session IS a bucket.
        s["synthetic"] = s["synthetic"] or synthetic
        rid = (g("request") or "").strip()
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
        raw_prompt = g("prompt")
        full = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
        prompt = _clean_prompt(full)
        pid = (g("prompt_id") or "").strip()
        if s["title"] is None:  # title precedence: explicit title > first prompt
            title = (g("title") or "").strip()
            s["title"] = " ".join(title.split())[:80] if title else (prompt[:80] or None)

        model = self._prefix_model(g("model") or "")
        acc = s["models"].get(model)
        if acc is None:
            acc = s["models"][model] = self._new_acc()
        uncached = max(0, inp - cached)
        self._accumulate(acc, uncached, cached, out, cost)

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
                "cache_write": 0,
                "tokens_total": uncached + cached + out,
                "prompt": prompt,
                "prompt_full": full,  # uncapped; the Turns tab can expand it
                "prompt_id": pid,
                "tools": self._split_tools(g("tool")),
            }
        )

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title"] or "(untitled)"
        s["directory"] = self._resolve_dir(s["project"])
        # Active working time: every timestamped request row is an activity point; a row
        # that opens a new prompt group is a fresh human turn, so the gap before it is an
        # idle wait. The boundary key column is decided ONCE per session: if any row
        # carries a stable prompt_id, that id is authoritative (a blank id is a
        # continuation, never a new turn); otherwise the key is the FULL prompt text
        # (not the 160-char-capped display prompt, which would merge two long prompts
        # sharing a prefix). Mixing the two per row turned a blank-id continuation into a
        # false human turn. A new group is a non-empty key differing from the last; a
        # blank key never opens one. Left unknown, per session, whenever no usable prompt
        # boundary survives: no prompt info at all, all rows blank, or the only prompt
        # row had no timestamp -- blank beats reporting the elapsed-with-idle span.
        # Arithmetic is on the ABSOLUTE ts_epoch (a row without one is dropped), never
        # the local string, so a session straddling a DST fall-back still measures true
        # gaps -- matching the ISO backends, which already work off absolute epochs.
        turns = sorted(
            (t for t in s["turns"] if t["ts_epoch"] is not None), key=lambda t: t["ts_epoch"]
        )
        use_id = any(t["prompt_id"] for t in turns)
        prompt_epochs, prev_key = [], None
        for t in turns:
            key = t["prompt_id"] if use_id else t["prompt_full"]
            if key and key != prev_key:
                prompt_epochs.append(t["ts_epoch"])
                prev_key = key
        s["worked_seconds"] = (
            worked_seconds([t["ts_epoch"] for t in turns], prompt_epochs) if prompt_epochs else None
        )
        rows: list[dict] = []
        for model_name, acc in s["models"].items():
            # The priced/unpriced split was decided per row in _accumulate (a log mixes
            # metered and subscription routes freely), so the row just reports what it
            # accumulated. No subagents, so root == total.
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
        # Sum the per-row unpriced split, not "every token of a $0-cost row": a bucket
        # holding both metered and subscription rows has a cost > 0 yet still carries
        # unpriced tokens, and counting it as fully priced would hide them.
        s["unpriced_tokens"] = sum(
            r["unpriced_input"]
            + r["unpriced_output"]
            + r["unpriced_reasoning"]
            + r["unpriced_cache_read"]
            + r["unpriced_cache_write"]
            for r in rows
        )
        # Whether any request logged a tool -- the per-session Tools tab gate, computed
        # once here so supports_tools stays O(1) per frame.
        s["has_tools"] = any(t.get("tools") for t in s["turns"])

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
            "tokens_reasoning": 0,
            "tokens_cache_read": acc["cache_read"],
            "tokens_cache_write": acc["cache_write"],
            "tokens_total": acc["tokens_total"],
        }

    # --- Store interface -----------------------------------------------------
    def cache_inputs(self) -> list[str]:
        # The single CSV file whose (size, mtime) fingerprints the warm-start cache.
        return [self.csv_path]

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
                    subagents=0,  # CSV has no subagent tree
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
        # Mirror CodexStore._demo_workflow: anonymize, backfill a synthetic price for the
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
        root = self._new_acc()
        best, best_runs = "unknown (not recorded)", -1
        for model_name, acc in s["models"].items():
            for k in root:
                root[k] += acc[k]
            if acc["runs"] > best_runs:
                best_runs, best = acc["runs"], model_name
        # Single depth-0 node. cost is the recorded total ($0 for the subscription case);
        # _priced_nodes reprices a $0 node from its token columns under "$".
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

    # --- Turns tab opt-in ----------------------------------------------------
    def message_timeline(self, workflow_id: str) -> list[dict]:
        # Chronological per-turn rows. Canonical "YYYY-MM-DD HH:MM:SS" timestamps
        # sort in time order; a turn's prompt_id (the explicit id, else the prompt
        # text) groups consecutive same-prompt turns under one "▸" header, like the
        # other backends. App._scale_demo_turns hides magnitudes in demo, like Tools.
        s = self._parse().get(workflow_id)
        if not s:
            return []
        out = []
        for t in sorted(s["turns"], key=lambda r: r["ts"]):
            r = dict(t)
            r["time"] = r.pop("ts")  # already canonical "YYYY-MM-DD HH:MM:SS" (local)
            prompt = r.pop("prompt", "")
            explicit = r.pop("prompt_id", "")
            r["prompt_id"] = explicit or prompt  # group consecutive same-prompt turns
            r["prompt_title"] = prompt
            r["prompt_full"] = r.get("prompt_full") or prompt
            out.append(r)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        return True

    # Prefix of the synthetic (date, project) session ids minted when the log carries
    # no session column. JsonlStore overrides it -- supports_context_curve keys off
    # this, so a subclass that mints a different prefix must say so or the gate goes
    # dead for every synthetic session it creates.
    SYNTHETIC_ID_PREFIX = "csv:"

    def supports_context_curve(self, workflow_id: str) -> bool:
        # The Context tab's growth curve -- and the Turns tab's compaction markers, which
        # ride on this same gate -- need the rows to be one *conversation's* consecutive
        # requests. A log row with a real session_id qualifies; a synthetic (date, project)
        # bucket interleaves unrelated conversations, so a "curve" over it would be noise
        # and fake compactions. Answered from what the parser recorded (see _parse_row),
        # with the prefix only as the fallback for an id this store never saw.
        s = self._parse().get(str(workflow_id))
        if s is not None:
            return not s.get("synthetic")
        return not str(workflow_id).startswith(self.SYNTHETIC_ID_PREFIX)

    def tool_breakdown(self, workflow_id: str) -> list[dict]:
        # Per-(tool, model) token/cost attribution for the Tools tab: each request
        # row's tokens (and recorded cost, when the log carries one) split evenly
        # across the tools its optional `tool` column names -- the
        # Store.tool_breakdown semantics off the in-memory turn rows.
        s = self._parse().get(workflow_id)
        return tool_rows_from_turns(s["turns"]) if s else []

    def supports_tools(self, workflow_id: str) -> bool:
        # Gated per session on the optional `tool` column actually being used --
        # a log without it hides the tab rather than showing it empty
        # (JsonlStore inherits this with its per-line `tool` key).
        s = self._parse().get(workflow_id)
        return bool(s and s.get("has_tools"))
