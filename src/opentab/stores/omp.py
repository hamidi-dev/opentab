"""omp (Oh My Pi) JSONL backend."""
from __future__ import annotations

import glob
import json
import os
import sqlite3

from opentab.formatting import _clean_prompt, iso_to_epoch, iso_to_local, worked_seconds
from opentab.models import Workflow
from opentab.stores.pi import PiStore
from opentab.util import LazyStatusRoot, read_files_parallel, tool_rows_from_turns


class OmpStore(PiStore):
    """Read omp (`@oh-my-pi/pi-coding-agent`, `~/.omp/agent/sessions`, or the dir named
    by $OMP_AGENT_DIR / --omp-dir) sessions. omp is a fork/rename of pi-agent and writes
    the *identical* JSONL record schema (verified against the real corpus), so this is a
    `PiStore` subclass, not a new parser -- everything not listed below (token
    accounting, dedup by assistant `id`, the metered/subscription cost split itself, the
    Turns/Tools opt-ins) is inherited unchanged. Four things differ:

    1. **Data directory** -- `~/.omp/agent/sessions`; resolved by the caller (like pi's
       own `--pi-dir`), not by this class.
    2. **Subscription detection reads SQLite, not `auth.json`.** omp has no `auth.json`;
       login state lives in `~/.omp/agent/agent.db`'s `auth_credentials` table
       (`provider`, `credential_type`, plus a `data` column holding LIVE OAuth
       access/refresh tokens). `_load_oauth_providers` opens it **read-only** and
       selects **only** `provider, credential_type` -- never `data`, never logged --
       degrading to the inherited `_SUBSCRIPTION_MARKERS` heuristic on any error
       (missing file, locked WAL, corrupt schema).
    3. **Model labels are BARE, not provider-qualified.** pi records
       `model: "moonshotai/kimi-k2.6"`; omp records `provider: "openai-codex"` and
       `model: "gpt-5.6-sol"` as separate fields. `_model_label` (the seam pi.py
       exposes for exactly this) joins them to `"openai-codex/gpt-5.6-sol"` -- matching
       how `ZalyStore` spells the same model -- so the Providers rollup (which splits on
       the `/` prefix) and the models.dev price lookup (which keys on the bare last
       segment either way) both see a normal qualified id.
    4. **omp has a real subagent tree; pi has none.** A session that delegates to the
       `task` tool writes each child as its own full transcript (own `session` record,
       own uuid, own usage) in a **directory named exactly like the spawning
       transcript minus `.jsonl`** -- e.g. `<ts>_<uuid>.jsonl` spawns children under
       `<ts>_<uuid>/<AgentName>.jsonl`. That transcript's own filename carries no uuid
       at all (`PiStore._id_from_name` returns None for it), so a naive port would
       silently drop it via the same code path pi drops a stub session -- measured on
       the real corpus, that undercounts one session by 3.5x (255,528 recorded vs.
       906,397 actual subtree tokens).

       **Parentage is derived from the PATH, not from a uuid** (`_parent_path`), because
       the nesting is **recursive**: omp builds a child's file as
       `resolve(<spawning transcript minus ".jsonl">, "<child>.jsonl")` at every level
       and walks up to 8 levels to find a root, so a *grandchild* lives at
       `<ts>_<uuid>/Agent/Grandchild.jsonl` -- whose immediate directory is an agent
       nickname carrying no uuid. Reading the parent off the path instead handles every
       depth with one rule, makes a parent cycle structurally impossible (`dirname()`
       strictly shortens), and answers the orphan case for free: a transcript whose
       parent file is absent (deleted, rotated, or outside the batch) is simply its own
       root, consistently in `workflows()` *and* in the `root_of`/`status_nodes` pair.

       Otherwise the fold mirrors
       `CodexStore._link_subagents`/`_fold_tree_rows`/`_descendants`: a child's own id
       comes from its own `session` record (its filename is instead the **agent
       label**), and folded sessions keep pi's root-vs-total split per model
       (`cost`/`unpriced_*` cover the whole subtree, `root_cost`/`root_unpriced_*` the
       root's own share) rather than Codex's cost-is-always-0 shape, since omp -- like
       pi -- mixes metered and subscription routes.

    Also: **title precedence.** pi has no title records and falls back to the first
    user prompt. omp writes a `session.title` plus dedicated `title` and `title_change`
    records; the seam `_extra_record` (a no-op in pi.py) captures all three so
    `_finalize` can prefer, in order, the latest `title_change` > a `title` record >
    `session.title` > the first user prompt > "(untitled)".

    `reasoning` stays 0 like pi's, and this is verified, not a carried-over
    approximation: omp's `usage.reasoningTokens` is OpenAI's `reasoning_tokens` detail,
    a SUBSET of `output` -- proven by `input + output + cacheRead + cacheWrite ==
    totalTokens` closing exactly *without* adding it in. Counting it again would
    double-bill under "$", the same reasoning `CodexStore`/`ZalyStore` already document.
    """

    combined = False
    source_name = "Omp"

    # How deep a spawn chain may nest. omp's own root-walk gives up after 8 hops, so
    # matching it keeps a corrupted/looping layout bounded here too.
    _MAX_SPAWN_DEPTH = 8

    @staticmethod
    def _parent_path(path: str) -> str:
        # **omp's own rule**, read off its source: a spawned child's transcript is
        # `resolve(<parent session file minus ".jsonl">, "<child>.jsonl")`. Inverted,
        # a transcript's parent is its containing directory + ".jsonl" -- and because
        # that applies at *every* level, a grandchild sits at
        # `<ts>_<uuid>/Agent/Grandchild.jsonl` under its delegating `Agent.jsonl`.
        # Deriving the parent from the path (rather than hunting a uuid in the
        # immediate dirname, which only ever matches depth 1) is what makes nesting
        # work: at depth 2+ the dirname is an agent nickname carrying no uuid, so the
        # uuid rule dropped the whole transcript. It also makes a cycle structurally
        # impossible -- dirname() strictly shortens the path -- and answers the orphan
        # case for free: no parent file on disk means this transcript IS a root.
        return os.path.dirname(path) + ".jsonl"

    def _root_path(self, path: str) -> str:
        # Walk up the spawn chain to the transcript nothing spawned -- omp's own
        # `for (let f = 0; f < 8; f++)` root-walk, same bound.
        for _ in range(self._MAX_SPAWN_DEPTH):
            parent = self._parent_path(path)
            if not os.path.isfile(parent):
                return path
            path = parent
        return path

    # --- divergence 2: SQLite-backed oauth detection --------------------------
    def _auth_db_path(self) -> str:
        # agent.db sits beside the sessions dir, the same relative move pi makes
        # for auth.json.
        return os.path.join(os.path.dirname(os.path.normpath(self.root_dir)), "agent.db")

    def _load_oauth_providers(self) -> set[str]:
        # omp has no auth.json; the same "which providers are a plan login, not a
        # metered key" signal lives in agent.db's auth_credentials table. The `data`
        # column carries LIVE OAuth tokens -- SELECT only the two columns that answer
        # the question, and never touch it. Read-only URI open, per the repo's
        # read-only hard constraint; any failure (missing/locked/corrupt db) degrades
        # to the inherited marker heuristic rather than crashing.
        path = self._auth_db_path()
        out: set[str] = set()
        try:
            con = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
        except (sqlite3.Error, OSError):
            return out
        try:
            cur = con.execute("SELECT provider, credential_type FROM auth_credentials")
            for provider, credential_type in cur:
                if isinstance(provider, str) and str(credential_type or "").lower() == "oauth":
                    out.add(provider.lower())
        except sqlite3.Error:
            pass
        finally:
            con.close()
        return out

    # --- divergence 3: provider-qualified model label -------------------------
    def _model_label(self, msg: dict) -> str:
        model = msg.get("model")
        model = model if isinstance(model, str) and model else None
        provider = msg.get("provider")
        provider = provider if isinstance(provider, str) and provider else None
        if provider and model and not model.startswith(provider + "/"):
            return f"{provider}/{model}"
        if model:
            return model
        return "unknown"

    # --- title precedence (session.title / title / title_change) -------------
    def _extra_record(self, typ: str, o: dict, s: dict) -> None:
        if typ not in ("session", "title", "title_change"):
            return
        title = o.get("title")
        if not isinstance(title, str) or not title.strip():
            return
        s.setdefault("title_hints", {})[typ] = title.strip()

    # --- new session shape (parent/agent/children, for the subagent fold) ----
    @staticmethod
    def _new_session() -> dict:
        s = PiStore._new_session()
        s["paths"] = []  # every transcript carrying this id (a resume adds one)
        s["parent_paths"] = set()  # the transcripts that spawned them (_parent_path)
        s["parent_id"] = None  # resolved from parent_path once every file is parsed
        s["agent"] = None  # this session's own agent label, if it's a subagent
        s["children"] = []  # filled by _link_subagents
        s["is_child"] = False
        return s

    # --- parsing: subagent transcripts don't key by filename ------------------
    def _parse_file(self, path: str, lines: list[str], sessions: dict[str, dict]) -> None:
        # A transcript's id comes from its filename when it has one (a top-level
        # session), else from its own `session` record (a spawned child is named by
        # agent nickname). Parentage is decided by the PATH, not the id, so it works
        # identically at every nesting level -- see _parent_path.
        sid = self._id_from_name(path)
        agent = None
        if sid is None:
            sid = self._first_session_id(lines)
            if not sid:
                return  # not a session transcript -- drop rather than guess an id
            agent = os.path.splitext(os.path.basename(path))[0]
        s = sessions.setdefault(sid, self._new_session())
        # A RESUMED session legitimately spans several files under one id (pi's
        # _session_files globs for exactly that), and each of them can have spawned
        # its own children -- so every path is kept. Keying the parent lookup off a
        # single "the" path would strand every child spawned from any resume but the
        # last, turning them into standalone roots.
        s["paths"].append(path)
        s["parent_paths"].add(self._parent_path(path))
        if agent:
            s["agent"] = agent
        self._parse_lines(s, lines)

    @staticmethod
    def _resolve_parents(sessions: dict[str, dict]) -> None:
        # Turn each session's parent *paths* into a parent *id*, now that every
        # transcript in this batch has been parsed and can be looked up. A parent
        # path nothing parsed to (deleted, rotated, or simply outside this batch)
        # leaves parent_id None, so the session stays a root of its own rather than
        # pointing at an id that was never loaded.
        by_path = {p: sid for sid, s in sessions.items() for p in s["paths"]}
        for sid, s in sessions.items():
            s["parent_id"] = next(
                (
                    parent
                    for parent in (by_path.get(p) for p in sorted(s["parent_paths"]))
                    if parent and parent != sid
                ),
                None,
            )

    @staticmethod
    def _first_session_id(lines: list[str]) -> str | None:
        for line in lines:
            if '"type"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict) and o.get("type") == "session" and o.get("id"):
                return o["id"]
        return None

    def _finalize(self, sid: str, s: dict) -> None:
        super()._finalize(sid, s)
        hints = s.get("title_hints") or {}
        s["title"] = (
            hints.get("title_change")
            or hints.get("title")
            or hints.get("session")
            or s["title_prompt"]
            or "(untitled)"
        )

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        for path, text in read_files_parallel(self._files()):
            self._parse_file(path, text.split("\n"), sessions)
        for sid, s in sessions.items():
            self._finalize(sid, s)
            s["root_cost"] = s["total_cost"]  # flat default; overwritten below for a
            # root that turns out to have subagents
        # Resolve parentage across ALL sessions first, then drop the usage-less ones
        # (a stub with only session/model_change rows) exactly like pi. Order matters:
        # resolving after the drop would strand the children of a usage-less
        # INTERMEDIATE -- a subagent that only delegated further -- turning its whole
        # branch into standalone roots and leaving the status tree disagreeing with
        # workflows(). Splicing instead re-parents them onto the nearest surviving
        # ancestor, so a stub is transparent rather than a cut.
        self._resolve_parents(sessions)
        self._sessions = self._splice_usage_less(sessions)
        self._link_subagents(self._sessions)
        return self._sessions

    @staticmethod
    def _splice_usage_less(
        sessions: dict[str, dict], keep: frozenset = frozenset()
    ) -> dict[str, dict]:
        # Keep only sessions with recorded usage, re-pointing each survivor at its
        # nearest surviving ancestor. A chain of stubs collapses in one walk, and the
        # loop is bounded by the number of sessions so corrupt parentage can't hang.
        # `keep` protects ids that must survive regardless (the --status target, which
        # may itself be a usage-less root that only delegated). It has to be applied
        # HERE rather than by re-inserting afterwards: the walk below re-points the
        # survivors, so a target added back after the fact would have already had its
        # children promoted past it and would render as a lone empty node.
        kept = {sid: s for sid, s in sessions.items() if s["model_rows"] or sid in keep}
        for s in kept.values():
            parent = s["parent_id"]
            seen = set()
            while parent is not None and parent not in kept and parent not in seen:
                seen.add(parent)
                parent = sessions[parent]["parent_id"] if parent in sessions else None
            s["parent_id"] = parent if parent in kept else None
        return kept

    # --- the subagent fold (CodexStore._link_subagents's shape, pi's cost split) --
    def _link_subagents(self, sessions: dict[str, dict]) -> None:
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
                continue  # a child, or a flat root whose own rows already fit
            self._fold_tree_rows(sid, s, sessions)

    @staticmethod
    def _descendants(sessions: dict[str, dict], sid: str) -> list[tuple[str, int]]:
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
        # Rebuild the root's model_rows so cost/tokens cover the whole subtree while
        # root_cost/root_unpriced_* keep only the root's own share -- CodexStore's
        # root-vs-total shape, but keeping pi's per-message metered/subscription split
        # on BOTH sides (omp mixes routes; Codex records no cost at all).
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
                    "cost": round(acc["cost"], 6),
                    "root_cost": round(r["cost"], 6),
                    "tokens_total": acc["tokens_total"],
                    "input": acc["input"],
                    "reasoning": 0,
                    "cache_read": acc["cache_read"],
                    "cache_write": acc["cache_write"],
                    "output": acc["output"],
                    "unpriced_input": acc["u_input"],
                    "unpriced_reasoning": 0,
                    "unpriced_cache_read": acc["u_cache_read"],
                    "unpriced_cache_write": acc["u_cache_write"],
                    "unpriced_output": acc["u_output"],
                    "root_unpriced_input": r["u_input"],
                    "root_unpriced_reasoning": 0,
                    "root_unpriced_cache_read": r["u_cache_read"],
                    "root_unpriced_cache_write": r["u_cache_write"],
                    "root_unpriced_output": r["u_output"],
                }
            )
        s["model_rows"] = rows
        s["total_cost"] = round(sum(row["cost"] for row in rows), 6)
        s["root_cost"] = round(sum(row["root_cost"] for row in rows), 6)
        s["total_tokens"] = sum(row["tokens_total"] for row in rows)
        s["unpriced_tokens"] = sum(
            row["unpriced_input"]
            + row["unpriced_output"]
            + row["unpriced_cache_read"]
            + row["unpriced_cache_write"]
            for row in rows
        )

    # --- Store interface, subagent-aware ---------------------------------------
    def _auth_paths(self) -> list[str]:
        # What pi fingerprints as auth.json is agent.db here (PiStore.cache_inputs adds
        # these to the transcripts, and says why the login state has to be in there at
        # all). agent.db is in **WAL** mode (verified on a real install), so a
        # login can land entirely in the -wal sidecar while the main db's size and mtime
        # never move -- fingerprint that too, or the change is invisible until a
        # checkpoint. NOT -shm: it is shared-memory index state that SQLite rewrites on
        # every open, including opentab's own read, so including it changes the
        # fingerprint on every launch and the warm start could never hit (measured).
        db = self._auth_db_path()
        return [db, db + "-wal"]

    def workflows(self) -> list[Workflow]:
        self._sessions = None  # reload (r) re-reads fresh; model methods reuse cache
        # Re-read the login state too: `r` exists to pick up changes, and a provider
        # that switched to an oauth plan since launch must stop counting as spend.
        self._oauth_providers = self._load_oauth_providers()
        sessions = self._parse()
        rows = []
        for sid, s in sessions.items():
            if s["is_child"]:
                continue  # a subagent rolls up into its parent's row
            kids = self._descendants(sessions, sid)
            ends = [s["ts_max"]] + [sessions[k]["ts_max"] for k, _d in kids]
            ended = max((t for t in ends if t), default=None)
            # Worked time over the folded tree: the root's own turns plus every
            # descendant's (a subagent still writing is the agent working, and can
            # outlive the root's last message). Only the root's own prompts mark
            # idle -- a child's "user" message is the task instruction it was
            # spawned with, not something a human typed and walked away from.
            kid_turn_ts = [t["ts"] for k, _d in kids for t in sessions[k]["turns"]]
            worked = worked_seconds(
                [iso_to_epoch(t["ts"]) for t in s["turns"]]
                + [iso_to_epoch(ts) for ts in kid_turn_ts]
                + [iso_to_epoch(p["ts"]) for p in s["prompts"]],
                [iso_to_epoch(p["ts"]) for p in s["prompts"]],
            )
            rows.append(
                Workflow(
                    id=sid,
                    title=s["title"],
                    directory=s["directory"],
                    created_at=s["created_at"],
                    root_cost=s["root_cost"],
                    total_cost=s["total_cost"],
                    subagents=len(kids),
                    model_count=0,  # filled by App._load_model_cache
                    total_tokens=s["total_tokens"],
                    unpriced_tokens=s["unpriced_tokens"],
                    source=self.source_name,
                    ended_at=iso_to_local(ended) if ended else s["ended_at"],
                    worked_seconds=worked,
                )
            )
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return rows

    def model_breakdown(self) -> list[dict]:
        out: list[dict] = []
        for s in self._parse().values():
            if s["is_child"]:
                continue  # its usage is already inside the root's folded rows
            out.extend(s["model_rows"])
        return out

    def workflow_nodes(self, workflow_id: str) -> list[dict]:
        return self._tree_nodes(self._parse(), workflow_id)

    def _session_acc(self, s: dict) -> tuple[dict, str]:
        # A session's OWN usage rolled across its models (never the folded
        # subtree) -> (acc, busiest model) -- what a single tree node shows.
        acc = self._new_acc()
        best, best_runs = "unknown (not recorded)", -1
        for model_name, m in s["models"].items():
            for k in acc:
                acc[k] += m[k]
            if m["runs"] > best_runs:
                best_runs, best = m["runs"], model_name
        return acc, best

    def _tree_nodes(self, sessions: dict[str, dict], workflow_id: str) -> list[dict]:
        # Deliberately NOT named _nodes_from: pi's helper of that name takes
        # (workflow_id, session) and a tree needs the whole session map, so
        # reusing the name would leave PiStore's two call sites silently passing
        # the wrong arguments if either ever stopped being overridden here.
        s = sessions.get(workflow_id)
        if not s:
            return []
        acc, best = self._session_acc(s)
        # cost is the root's own share; _priced_nodes reprices a $0 node from its
        # token columns under "$", same as every other node-emitting backend.
        nodes = [
            self._node(workflow_id, 0, "-", s["title"], s["created_at"], best, s["root_cost"], acc)
        ]
        for child, depth in self._descendants(sessions, workflow_id):
            cs = sessions[child]
            cacc, cbest = self._session_acc(cs)
            nodes.append(
                self._node(
                    child,
                    depth,
                    cs["agent"] or "subagent",
                    cs["title"],
                    cs["created_at"],
                    cbest,
                    cs["total_cost"],
                    cacc,
                )
            )
        if self.demo:
            nodes = [self._demo_node(n) for n in nodes]
        return nodes

    # --- the one-shot --status trio, subagent-aware ---------------------------
    def recent_roots(self) -> list[dict]:
        # Same freshness signal as PiStore (file mtime, no parse), but a subagent
        # transcript's filename carries no uuid (it's named by agent nickname), so
        # the inherited version silently drops it -- a directory poll would then
        # show a session as idle while its subagent is mid-burst. Key a subagent
        # file by the PARENT uuid parsed from its containing directory instead, so
        # the freshest file anywhere in the subtree sets last_active.
        newest: dict[str, tuple[int, str]] = {}
        for path in self._files():
            root_path = self._root_path(path)
            # The root's own filename normally carries the uuid; only an ORPHANED
            # spawn (its parent transcript deleted or rotated away) is its own root
            # while being named by agent nickname, and only that case pays a head
            # read -- and it must, or the row would key an id no file can answer for.
            sid = self._id_from_name(root_path) or self._head_session_id(root_path)
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

    def _subagent_candidates(self) -> list[str]:
        # Every file that could be a spawned transcript: no uuid in its own name.
        # Cheap (no parse) pre-filter for root_of's scan.
        return [path for path in self._files() if self._id_from_name(path) is None]

    def root_of(self, session_id: str) -> str | None:
        # A top-level session's uuid is a filename hit, exactly like pi. A SPAWNED
        # session's uuid (what a caller passes after reading it off the Subagents
        # tab, or a tmux binding on a pane that cd'd nowhere useful) has no filename
        # hit at all -- its id lives only inside its own `session` record -- so read
        # the head of each nickname-named file until one matches, then answer with
        # the id of the transcript at the TOP of its spawn chain, at any depth.
        if self._session_files(session_id):
            return session_id
        for path in self._subagent_candidates():
            if self._head_session_id(path) == session_id:
                root_path = self._root_path(path)
                # The top of the chain is normally a uuid-named file. When it is an
                # ORPHAN (nothing spawned it on disk) it is nickname-named, so read
                # its id out of its own `session` record -- NOT the id we were asked
                # about, which for a nested descendant is the child's, and would make
                # status price a sub-branch while workflows() shows the orphan root.
                return (
                    self._id_from_name(root_path) or self._head_session_id(root_path) or session_id
                )
        return None

    def _head_session_id(self, path: str) -> str | None:
        # Bounded head read for the child's own uuid, the root_of/status_nodes
        # twin of PiStore._head_cwd (same budget, different field).
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
                    if isinstance(o, dict) and o.get("type") == "session" and o.get("id"):
                        return o["id"]
        except OSError:
            pass
        return None

    def status_nodes(self, workflow_id: str) -> list[dict]:
        # workflow_nodes for the --status one-shot, off a parse of just this root's
        # own file(s) plus its subagent directory -- never the full-tree parse. The
        # directory relationship is exact (unlike Codex's filename-timestamp
        # heuristic), so this only ever reads the files that can possibly belong to
        # this one root.
        if self._sessions is not None:
            return self.workflow_nodes(workflow_id)
        own = self._session_files(workflow_id)
        if not own:
            # An ORPHANED spawn is a root that workflows() lists but no filename
            # carries: its id lives only in its own `session` record. root_of hands
            # such an id back unchanged, so resolving it here is what keeps the two
            # agreeing -- without this, `status` prices $0 for a real session.
            own = [
                p for p in self._subagent_candidates() if self._head_session_id(p) == workflow_id
            ]
        if not own:
            return []
        paths = list(own)
        for root_path in own:
            sub_dir = os.path.splitext(root_path)[0]
            if os.path.isdir(sub_dir):
                # Recursive: a spawn chain nests (see _parent_path), so a one-level
                # glob would price a delegating subagent's own children at zero.
                paths.extend(glob.glob(os.path.join(sub_dir, "**", "*.jsonl"), recursive=True))
        sessions: dict[str, dict] = {}
        for path, text in read_files_parallel(paths):
            self._parse_file(path, text.split("\n"), sessions)
        for sid, s in sessions.items():
            self._finalize(sid, s)
            s["root_cost"] = s["total_cost"]
        # Keep the target even when usage-less (unlike _parse's drop): a root that
        # only spawned subagents must still price its children's subtree.
        # Same order as _parse: resolve, then splice, so a usage-less intermediate
        # doesn't cut its descendants out of the priced subtree. The target itself is
        # kept even when usage-less -- a root that only spawned must still price them.
        self._resolve_parents(sessions)
        sessions = self._splice_usage_less(sessions, frozenset([workflow_id]))
        self._link_subagents(sessions)
        target = sessions.get(workflow_id)
        if target is None or (not target["model_rows"] and not target["children"]):
            # Nothing to price: the target recorded no usage and delegated none
            # either. Report that as an empty segment (the --status contract for an
            # unpriceable target) rather than a $0.00 row for a session that has no
            # figures at all.
            return []
        return self._tree_nodes(sessions, workflow_id)

    # --- Turns/Tools, subtree-aware --------------------------------------------
    def _subtree_turns(self, workflow_id: str) -> list[dict]:
        # The root's own turn rows plus every descendant's, the child rows tagged
        # with their depth/agent -- the Turns tab's Claude/Codex sidechain pattern.
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

    def message_timeline(self, workflow_id: str) -> list[dict]:
        s = self._parse().get(workflow_id)
        if not s:
            return []
        prompts = sorted(s["prompts"], key=lambda p: p["ts"])
        out = []
        pi_, cur_id, cur_title, cur_full = 0, "", "", ""
        for t in sorted(self._subtree_turns(workflow_id), key=lambda r: r["ts"]):
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

    def tool_breakdown(self, workflow_id: str) -> list[dict]:
        return tool_rows_from_turns(self._subtree_turns(workflow_id))
