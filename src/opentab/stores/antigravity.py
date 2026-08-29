"""Google Antigravity conversation backend (SQLite + protobuf blobs)."""
from __future__ import annotations

import argparse
import glob
import os
import re
import sqlite3
from urllib.parse import unquote, urlparse

from opentab.demo import demo_config, scramble_node, scramble_workflow
from opentab.formatting import _clean_prompt, worked_seconds
from opentab.models import Workflow
from opentab.util import LazyStatusRoot, git_root, safe_int, tool_rows_from_turns

# Antigravity keeps each conversation in its own SQLite file under the Gemini home. The
# CLI and the desktop build disagree on the directory name, so both are scanned.
CONVERSATION_DIRS = ("antigravity-cli", "antigravity")

# The header a subagent's result message carries back to its parent.
_AGENT_HEADER = re.compile(r"^message from (.+?)(?:\s*\(self\))?$", re.IGNORECASE)

# What a call whose model nothing records is filed under.
UNKNOWN_MODEL = "unknown (not recorded)"


def default_antigravity_dir() -> str:
    home = (os.environ.get("GEMINI_CLI_HOME") or "").strip() or os.path.expanduser("~")
    return os.path.join(home, ".gemini")


# --- protobuf wire format ---------------------------------------------------------
# Every payload column in those databases is a serialized protobuf and opentab ships no
# schema for them, so this is a reader for the wire format itself: enough to walk fields
# by number and pull the handful the token accounting needs. Runtime stays stdlib-only.


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while i < len(buf):
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7
        if shift > 63:  # a value wider than 64 bits is not a varint we can trust
            break
    raise ValueError("truncated varint")


def pb_fields(buf: bytes):
    """Yield ``(field number, wire type, value)`` for one protobuf message.

    Stops at the first byte it cannot read rather than raising: these blobs are read
    without a schema, so a field this reader does not understand must cost the caller
    the rest of that message, never the whole backend.
    """
    i = 0
    while i < len(buf):
        try:
            key, i = _varint(buf, i)
        except ValueError:
            return
        number, wire = key >> 3, key & 7
        if number == 0:
            return
        try:
            if wire == 0:
                value, i = _varint(buf, i)
            elif wire == 1:
                if i + 8 > len(buf):
                    return  # a truncated fixed64 would otherwise read as a real number
                value, i = int.from_bytes(buf[i : i + 8], "little"), i + 8
            elif wire == 2:
                length, i = _varint(buf, i)
                if length < 0 or i + length > len(buf):
                    return
                value, i = buf[i : i + length], i + length
            elif wire == 5:
                if i + 4 > len(buf):
                    return  # likewise: four stray bytes can fabricate a plausible epoch
                value, i = int.from_bytes(buf[i : i + 4], "little"), i + 4
            else:  # group wire types are obsolete and never appear here
                return
        except ValueError:
            return
        yield number, wire, value


def pb_get(buf: bytes, *path: int):
    """Walk nested field numbers, returning the last field's value (or None)."""
    current = buf
    for number in path[:-1]:
        nxt = None
        for field, wire, value in pb_fields(current):
            if field == number and wire == 2:
                nxt = value
                break
        if nxt is None:
            return None
        current = nxt
    for field, _wire, value in pb_fields(current):
        if field == path[-1]:
            return value
    return None


def pb_str(buf: bytes, *path: int) -> str:
    value = pb_get(buf, *path)
    if not isinstance(value, bytes):
        return ""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return ""


class AntigravityStore:
    """Read Antigravity's per-conversation SQLite databases.

    Usage is a protobuf ``usage`` message whose input is split between a fixed
    system-prompt count and the newly-processed tokens, with thinking additive to the
    text output. No cost is recorded, so every token is estimated under ``$``.
    """

    combined = False
    source_name = "Antigravity"
    # Antigravity records tokens but never a price -- the Claude Code / Codex / Gemini
    # subscription shape: $0 in normal mode, a list-price estimate under "$".
    records_cost = False

    # Field numbers inside the `usage` message, reverse-engineered from real databases
    # and validated per row by the arithmetic in `_usage_of`.
    _U_SYSTEM = 1  # fixed system-prompt tokens; absent on auxiliary calls
    _U_INPUT = 2  # newly-processed (uncached) input
    _U_OUT_TOTAL = 3  # text output + thinking
    _U_CACHE_READ = 5
    _U_OUTPUT = 9  # text output only
    _U_THINKING = 10  # additive, billed at the output rate
    _U_RESPONSE_ID = 11  # the dedup key

    # Step shapes, likewise reverse-engineered. A step's kind is its `step_type`
    # column, and the payload it carries depends on that kind.
    _STEP_USER = 14  # a message the user typed
    _STEP_GENERATION = 15  # a model turn; the only kind that asks for tools
    _STEP_AUXILIARY = 23  # a routing or availability call beside the conversation
    _STEP_TOOL = 132  # one tool invocation, carrying no usage of its own
    _STEP_SUBAGENT = 101  # a spawned conversation reporting back
    _TOOL_NAME = (4, 2)  # metadata: the tool a tool-call step invoked
    _SUBAGENT_CHILD = (114, 4, 3)  # payload: the conversation id a subagent ran in
    _SUBAGENT_HEADER = (114, 2, 1)  # payload: "Message from <agent> (self)"
    _PROMPT_TEXT = (19, 2)  # payload: the text of a typed message

    def __init__(self, root_dir: str, args: argparse.Namespace):
        self.root_dir = root_dir
        self.args = args
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._sessions: dict[str, dict] | None = None
        self._git_root_cache: dict[str, str] = {}

    def _git_root(self, cwd: str) -> str:
        if cwd not in self._git_root_cache:
            self._git_root_cache[cwd] = git_root(cwd)
        return self._git_root_cache[cwd]

    # ---- file discovery ------------------------------------------------------

    def _files(self) -> list[str]:
        out: list[str] = []
        for name in CONVERSATION_DIRS:
            out.extend(glob.glob(os.path.join(self.root_dir, name, "conversations", "*.db")))
        return sorted(out)

    def cache_inputs(self) -> list[str]:
        # SQLite may hold a conversation's newest rows in the -wal sidecar while the main
        # file's size and mtime never move, so fingerprint it too (the OmpStore rule).
        # Never -shm: it is rewritten on every open, including opentab's own read.
        return [p for path in self._files() for p in (path, path + "-wal")]

    def _session_files(self, session_id: str) -> list[str]:
        if not session_id or "/" in session_id or "\\" in session_id:
            return []
        return [p for p in self._files() if os.path.splitext(os.path.basename(p))[0] == session_id]

    @classmethod
    def is_conversation(cls, path: str) -> bool:
        """True when this file really is a conversation database.

        Detection borrows it so a stray or truncated `*.db` cannot advertise the source
        and then produce nothing -- and `recent_roots` borrows it so the status path
        never offers an id `workflows()` has no row for.
        """
        try:
            con = cls._connect(path)
        except sqlite3.Error:
            return False
        try:
            con.execute("SELECT idx FROM steps LIMIT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
        finally:
            con.close()

    @staticmethod
    def _connect(path: str):
        # Read-only, like every other SQLite backend: opentab never writes a harness's DB.
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    # ---- usage extraction ----------------------------------------------------

    @classmethod
    def _usage_of(cls, message: bytes) -> dict | None:
        """Read one candidate ``usage`` message, or None when it is not one.

        The blobs carry no schema, so a candidate is accepted only when it *proves*
        itself: it must carry a response id and satisfy ``#3 == #9 + #10`` (total output
        equals text plus thinking). That arithmetic is what separates a real usage
        message from an unrelated sub-message that happens to reuse these field numbers
        -- which matters because the record does not sit in one fixed place (see
        `_usage_rows`).
        """
        varints = {f: v for f, wire, v in pb_fields(message) if wire == 0}
        response_id = pb_str(message, cls._U_RESPONSE_ID)
        if not response_id.strip():
            return None
        output = safe_int(varints.get(cls._U_OUTPUT, 0))
        thinking = safe_int(varints.get(cls._U_THINKING, 0))
        total_out = varints.get(cls._U_OUT_TOTAL)
        if total_out is None or safe_int(total_out) != output + thinking:
            return None
        if cls._U_OUTPUT not in varints and cls._U_THINKING not in varints:
            # With both output fields absent the identity reads 0 == 0 + 0 and proves
            # nothing, so any message carrying a string at #11 would be accepted as a
            # billed call. Every usage record in a real corpus (13 of 13) carries both,
            # so requiring one costs nothing and closes the hole.
            return None
        system = safe_int(varints.get(cls._U_SYSTEM, 0))
        fresh = safe_int(varints.get(cls._U_INPUT, 0))
        cache_read = safe_int(varints.get(cls._U_CACHE_READ, 0))
        if system + fresh + cache_read + output + thinking == 0:
            return None
        return {
            "response_id": response_id,
            # The fixed system-prompt block is charged as input like any other prompt
            # token; it has no column of its own, so it rides with the fresh input.
            "input": system + fresh,
            "output": output,
            "reasoning": thinking,
            "cache_read": cache_read,
            "cache_write": 0,
        }

    @classmethod
    def _usage_rows(cls, blob: bytes, depth: int = 0) -> list[dict]:
        """Every usage message reachable inside one blob, at any nesting.

        Deliberately a search rather than a fixed path. The record does not live in one
        place: a generation puts it at ``#1.#4``, an assistant step at ``#9``, and an
        auxiliary step at ``#28.#2`` -- all observed in a single real conversation. A
        hardcoded path list silently drops whichever shape it was not written for, and
        those are exactly the rows nothing else records.
        """
        found: list[dict] = []
        if depth > 4:
            return found
        row = cls._usage_of(blob)
        if row is not None:
            found.append(row)
        for _field, wire, value in pb_fields(blob):
            if wire == 2 and value:
                found.extend(cls._usage_rows(value, depth + 1))
        return found

    # ---- parsing -------------------------------------------------------------

    @staticmethod
    def _new_acc() -> dict:
        return {
            "runs": 0,
            "input": 0,
            "output": 0,
            "reasoning": 0,  # thinking is additive to the text output, so it is counted
            "cache_read": 0,
            "cache_write": 0,  # Antigravity records no cache write
            "tokens_total": 0,
            "cost": 0.0,  # always 0: no price is recorded
        }

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        parsed: dict[str, dict] = {}
        for path in self._files():
            session = self._parse_file(path)
            if session:
                parsed[session["sid"]] = session
        # Drop conversations with no recorded usage -- but KEEP one that spawned a
        # subagent which did spend. The status path reads the parent link off the file
        # and would keep naming that parent as the root, so dropping it here would have
        # `opentab cost` price $0 for a session the browser lists under its child's id.
        self._sessions = self._assemble(parsed)
        return self._sessions

    def _assemble(self, parsed: dict[str, dict]) -> dict[str, dict]:
        """Drop the conversations with nothing to show, then link what is left.

        Shared by the parser and by `status_nodes`, so the browser and `opentab cost`
        cannot disagree about which conversations exist or which one is a subtree's root.
        A conversation with no usage of its own is dropped -- UNLESS it spawned a
        subagent that did spend: the status path reads the parent link off the file and
        would keep naming that parent, so dropping it here would price $0 for a session
        the browser lists under its child's tokens.
        """
        spenders = {sid for sid, s in parsed.items() if s["models"]}
        keep = set(spenders)
        for parent, s in parsed.items():
            for child, _label in s["children"]:
                if child in spenders and parent in parsed:
                    keep.add(parent)
        sessions = {sid: s for sid, s in parsed.items() if sid in keep}
        self._link_subagents(sessions)
        return sessions

    def _link_subagents(self, sessions: dict[str, dict]) -> None:
        """Fold each spawned conversation into the one that spawned it.

        A subagent runs in its own conversation database, so without this it stands in
        the browser as a root of its own -- the parent under-reporting its real cost
        while a phantom session sits beside it holding the difference. The link is the
        child's id, which the parent records on the step that carries the subagent's
        reply back.
        """
        for s in sessions.values():
            s["is_child"] = False
            s["parent_id"] = None
        for sid, s in sessions.items():
            for child_id, label in s["children"]:
                child = sessions.get(child_id)
                if child is None or child_id == sid or child["is_child"]:
                    continue  # not in this batch, self-referential, or already claimed
                if self._is_ancestor(sessions, child_id, sid):
                    # Closing the loop would leave every conversation in it a child, so
                    # `workflows()` would emit none of them and the cycle's whole spend
                    # would vanish from the browser while `recent_roots` still offered
                    # its ids. Refusing the link keeps one of them a root.
                    continue
                child["is_child"] = True
                child["parent_id"] = sid
                child["agent"] = label
        for sid, s in sessions.items():
            if not s["is_child"] and self._descendants(sessions, sid):
                self._fold_tree_rows(sid, s, sessions)

    @staticmethod
    def _is_ancestor(sessions: dict[str, dict], candidate: str, sid: str) -> bool:
        # Walk up from `sid`; `seen` bounds it against a loop already in place.
        current, seen = sid, {sid}
        while current is not None:
            if current == candidate:
                return True
            current = sessions[current]["parent_id"] if current in sessions else None
            if current in seen:
                return False
            seen.add(current)
        return False

    @staticmethod
    def _descendants(sessions: dict[str, dict], sid: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        queue, seen = [(sid, 0)], {sid}
        while queue:
            current, depth = queue.pop(0)
            for child_id, _label in sessions[current]["children"]:
                child = sessions.get(child_id)
                # Follow the RESOLVED link, not the recorded one. Two conversations can
                # each name the same child (a re-run, or a shared helper), and only one
                # of them owns it -- walking the raw list would fold its tokens into
                # both and count them twice.
                if child is None or child_id in seen or child.get("parent_id") != current:
                    continue
                seen.add(child_id)
                out.append((child_id, depth + 1))
                queue.append((child_id, depth + 1))
        return out

    def _fold_tree_rows(self, sid: str, s: dict, sessions: dict[str, dict]) -> None:
        # cost/tokens cover the whole subtree while root_* keeps the root's own share --
        # CodexStore's root-vs-total shape. Nothing is priced here, so the unpriced split
        # simply mirrors the token columns.
        total: dict[str, dict] = {}
        for model, acc in s["models"].items():
            self._merge(total, model, acc)
        for child_id, _depth in self._descendants(sessions, sid):
            for model, acc in sessions[child_id]["models"].items():
                self._merge(total, model, acc)
        own = s["models"]
        s["model_rows"] = [
            self._model_row(sid, model, acc, own.get(model, self._new_acc()))
            for model, acc in total.items()
        ]
        self._roll_totals(s)

    @classmethod
    def _merge(cls, bucket: dict[str, dict], model: str, acc: dict) -> None:
        target = bucket.setdefault(model, cls._new_acc())
        for key in target:
            target[key] += acc[key]

    def _parse_file(self, path: str) -> dict | None:
        sid = os.path.splitext(os.path.basename(path))[0]
        try:
            con = self._connect(path)
        except sqlite3.Error:
            return None  # locked, truncated, or not a database at all
        try:
            return self._read(con, sid, path)
        except sqlite3.Error:
            return None  # a schema this version does not have; skip the file, never crash
        finally:
            con.close()

    def _read(self, con, sid: str, path: str) -> dict:
        meta = self._table_blob(con, "SELECT data FROM trajectory_metadata_blob LIMIT 1")
        created = self._timestamp(pb_get(meta, 2)) if meta else None
        workspace = self._workspace(meta) if meta else ""
        session = {
            "sid": sid,
            "cwd": workspace,
            "created_epoch": created,
            "models": {},
            "turns": [],
            "prompts": [],
            "children": [],  # conversation ids this one spawned as subagents
            "agent": "",  # the label its parent gave it, filled in when linked
            "path": path,
        }
        # Each generation names its own model, so read those first: they are what lets a
        # conversation that switched models keep both, instead of collapsing to one label.
        by_response, fallback_model = self._models_by_response(con)
        # Every usage record in the file, whichever blob carries it, collected by response
        # id: the same generation is written to `gen_metadata` AND to its step, while an
        # auxiliary call (a routing or availability check) appears only on the step.
        # Reading one table alone drops the calls the other holds.
        calls: dict[str, tuple[dict, float | None]] = {}
        tool_calls: list[tuple[str, str]] = []  # (response id of the asking turn, tool)
        pending = ""  # the generation the next tool-call steps belong to
        for step_type, stamp, metadata, payload in self._steps(con):
            for row in self._usage_rows(metadata):
                self._keep_richer(calls, row, stamp)
                if step_type == self._STEP_GENERATION:
                    # Only a real generation asks for tools. An auxiliary call runs beside
                    # the conversation, so letting it claim the following calls would hand
                    # them its own much smaller token count.
                    pending = row["response_id"]
            if step_type == self._STEP_TOOL:
                # A tool-call step follows the generation that ASKED for it, and several
                # can share one: the observed order is generation → its calls → the
                # generation that reads their results. Recording the ASKING RESPONSE ID
                # rather than a position keeps the link correct however the turns are
                # later ordered or merged; `tool_rows_from_turns` then splits that turn's
                # tokens across them. Tool steps carry no usage of their own.
                tool = pb_str(metadata, *self._TOOL_NAME).strip()
                if tool and pending:
                    tool_calls.append((pending, tool))
            elif step_type == self._STEP_SUBAGENT:
                child = pb_str(payload, *self._SUBAGENT_CHILD).strip()
                if child and child != sid:
                    session["children"].append((child, self._agent_label(payload)))
            elif step_type == self._STEP_USER:
                text = pb_str(payload, *self._PROMPT_TEXT)
                if text.strip():
                    session["prompts"].append(
                        {
                            "ts": stamp if stamp is not None else created,
                            "title": " ".join(text.split()),
                        }
                    )
        for blob in self._generations(con):
            for row in self._usage_rows(blob):
                # These rows record no wall-clock time this build can read, so they carry
                # None rather than the session's created-at: asserting a start time for a
                # call that may belong to the last prompt would file it under the first.
                self._keep_richer(calls, row, None)
        turns_by_response: dict[str, dict] = {}
        for response_id, (row, stamp) in calls.items():
            model = by_response.get(response_id) or fallback_model
            turns_by_response[response_id] = self._add_turn(session, row, model, stamp)
        for response_id, tool in tool_calls:
            turn = turns_by_response.get(response_id)
            if turn is not None:
                turn["tools"].append(tool)
        self._finalize(session)
        return session

    @classmethod
    def _keep_richer(cls, calls: dict, row: dict, stamp) -> None:
        """Record one call, preferring the more complete copy when a response id repeats.

        A generation is written twice -- to its step and to `gen_metadata` -- and the two
        copies are identical in practice. First-one-wins is still the wrong tiebreak: a
        copy missing the fixed system-prompt block would silently win over the one that
        has it, reporting a fraction of the tokens actually billed. The richer record is
        the safe direction, and a timestamp is kept from whichever copy had one.
        """
        response_id = row["response_id"]
        previous = calls.get(response_id)
        if previous is None:
            calls[response_id] = (row, stamp)
            return
        old_row, old_stamp = previous
        keep = row if cls._token_sum(row) > cls._token_sum(old_row) else old_row
        calls[response_id] = (keep, old_stamp if old_stamp is not None else stamp)

    @staticmethod
    def _token_sum(row: dict) -> int:
        return row["input"] + row["output"] + row["reasoning"] + row["cache_read"]

    @staticmethod
    def _table_blob(con, sql: str) -> bytes:
        row = con.execute(sql).fetchone()
        return row[0] if row and isinstance(row[0], (bytes, bytearray)) else b""

    def _steps(self, con):
        """(step type, timestamp, metadata, payload) for every step, in file order.

        Order is what makes tool attribution possible, so this stays a single ordered
        walk rather than one query per thing the caller wants out of it.
        """
        for _idx, step_type, metadata, payload in self._rows(
            con, "SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY idx"
        ):
            blob = metadata if isinstance(metadata, (bytes, bytearray)) else b""
            body = payload if isinstance(payload, (bytes, bytearray)) else b""
            yield step_type, self._timestamp(pb_get(blob, 1)), blob, body

    def _generations(self, con):
        """The generation blobs, read after the steps.

        A generation is normally written to its step as well, so the response-id merge
        makes this pass a backstop for anything the step walk did not carry. No timestamp
        is yielded: the field that held one is an unset sentinel on this build, and the
        session's created-at is not a stand-in for it (see `_keep_richer`).
        """
        for _idx, data in self._rows(con, "SELECT idx, data FROM gen_metadata ORDER BY idx"):
            if isinstance(data, (bytes, bytearray)) and data:
                yield data

    @staticmethod
    def _rows(con, sql: str):
        try:
            cursor = con.execute(sql)
        except sqlite3.Error:
            return  # an older or newer schema without this table
        yield from cursor

    def _models_by_response(self, con) -> tuple[dict[str, str], str]:
        """(`{response id: model}`, the fallback for calls that name none).

        Each generation records its own model, so a conversation that switched models
        keeps both -- collapsing them to one conversation-wide label would file real,
        explicitly recorded usage under the wrong rates. Only an AUXILIARY call names no
        model, and it inherits the conversation's only one; where the file records
        several there is nothing to inherit, so those rows stay unattributed rather than
        being guessed, the way HermesStore's auxiliary buckets fail closed.
        """
        by_response: dict[str, str] = {}
        names: set[str] = set()
        for _idx, data in self._rows(con, "SELECT idx, data FROM gen_metadata ORDER BY idx"):
            if not isinstance(data, (bytes, bytearray)) or not data:
                continue  # SQLite permits a non-blob here even in a blob-typed column
            name = pb_str(data, 1, 19).strip()
            if not name:
                continue
            names.add(name)
            for row in self._usage_rows(data):
                by_response[row["response_id"]] = self._model_label(name)
        fallback = self._model_label(names.pop()) if len(names) == 1 else UNKNOWN_MODEL
        return by_response, fallback

    @staticmethod
    def _model_label(model: str) -> str:
        # Antigravity records a bare id ("gemini-3.7-flash"); provider-prefix it so the
        # Providers rollup, which groups on the "/" prefix, sees a route.
        model = (model or "").strip()
        if not model:
            return UNKNOWN_MODEL
        return model if "/" in model else "google/" + model

    @classmethod
    def _agent_label(cls, payload: bytes) -> str:
        """The subagent's name, from the header its result message carries.

        Observed as ``Message from Joke Writer (self)``. Parsed leniently and falling
        back to a bare "subagent": the label is a nicety on one tree row, and a build
        that words it differently must not cost the row its identity.
        """
        header = pb_str(payload, *cls._SUBAGENT_HEADER).strip()
        match = _AGENT_HEADER.match(header)
        name = (match.group(1) if match else "").strip()
        return name or "subagent"

    @staticmethod
    def _timestamp(message) -> float | None:
        # A protobuf Timestamp: {#1: seconds, #2: nanos}. Range-checked, because these
        # field numbers are read without a schema and an unrelated varint must not become
        # a date -- the 1970 epoch would silently open every range filter.
        if not isinstance(message, (bytes, bytearray)):
            return None
        seconds = pb_get(message, 1)
        if not isinstance(seconds, int) or not 1_000_000_000 < seconds < 4_000_000_000:
            return None
        nanos = pb_get(message, 2)
        frac = nanos / 1e9 if isinstance(nanos, int) and 0 <= nanos < 1_000_000_000 else 0.0
        return seconds + frac

    def _add_turn(self, session: dict, row: dict, model: str, stamp) -> dict:
        acc = session["models"].get(model)
        if acc is None:
            acc = session["models"][model] = self._new_acc()
        total = row["input"] + row["output"] + row["reasoning"] + row["cache_read"]
        acc["runs"] += 1
        for key in ("input", "output", "reasoning", "cache_read"):
            acc[key] += row[key]
        acc["tokens_total"] += total
        session["turns"].append(
            {
                "ts": stamp or 0.0,
                "depth": 0,
                "agent": "-",
                "effort": "",
                "model_name": model,
                "cost": 0.0,  # no price recorded; "$" estimates from the token columns
                "input": row["input"],
                "output": row["output"],
                "reasoning": row["reasoning"],
                "cache_read": row["cache_read"],
                "cache_write": 0,
                "tokens_total": total,
                "tools": [],  # filled in from the tool-call steps this turn asked for
            }
        )
        return session["turns"][-1]

    @staticmethod
    def _workspace(meta: bytes) -> str:
        # trajectory_metadata_blob.#1.#1 is a file:// URI for the workspace root.
        uri = pb_str(meta, 1, 1)
        if not uri:
            return ""
        if not uri.startswith("file:"):
            return uri
        parsed = urlparse(uri)
        return unquote(parsed.path or "")

    def _finalize(self, s: dict) -> None:
        s["title"] = _clean_prompt(s["prompts"][0]["title"], 80) if s["prompts"] else "(untitled)"
        s["directory"] = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"
        stamps = [t["ts"] for t in s["turns"] if t["ts"]] + [
            p["ts"] for p in s["prompts"] if p["ts"]
        ]
        if s["created_epoch"]:
            stamps.append(s["created_epoch"])
        s["ts_min"] = min(stamps) if stamps else None
        s["ts_max"] = max(stamps) if stamps else None
        s["created_at"] = self._fmt(s["ts_min"])
        s["ended_at"] = self._fmt(s["ts_max"])
        prompt_epochs = [p["ts"] or None for p in s["prompts"]]
        s["worked_seconds"] = worked_seconds(
            [t["ts"] or None for t in s["turns"]] + prompt_epochs, prompt_epochs
        )
        s["model_rows"] = [
            self._model_row(s["sid"], model_name, acc, acc)
            for model_name, acc in s["models"].items()
        ]
        self._roll_totals(s)

    @staticmethod
    def _model_row(sid: str, model_name: str, acc: dict, own: dict) -> dict:
        # Nothing is priced, so cost is 0 and every token lands in the unpriced split the
        # "$" view estimates from -- the ClaudeStore/CodexStore subscription shape.
        return {
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
            "root_unpriced_input": own["input"],
            "root_unpriced_reasoning": own["reasoning"],
            "root_unpriced_cache_read": own["cache_read"],
            "root_unpriced_cache_write": own["cache_write"],
            "root_unpriced_output": own["output"],
        }

    @staticmethod
    def _roll_totals(s: dict) -> None:
        rows = s["model_rows"]
        s["total_cost"] = 0.0
        s["root_cost"] = 0.0
        s["total_tokens"] = sum(r["tokens_total"] for r in rows)
        s["unpriced_tokens"] = sum(
            r["unpriced_input"]
            + r["unpriced_output"]
            + r["unpriced_reasoning"]
            + r["unpriced_cache_read"]
            + r["unpriced_cache_write"]
            for r in rows
        )

    @staticmethod
    def _fmt(epoch) -> str:
        if not epoch:
            return ""
        from datetime import datetime, timezone

        try:
            return (
                datetime.fromtimestamp(epoch, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except (OverflowError, OSError, ValueError):
            return ""

    # ---- status one-shot -----------------------------------------------------

    def recent_roots(self) -> list[dict]:
        # One database per conversation, appended in place, so its mtime is the session's
        # last activity -- no parse, and the filename is the id.
        links = self._parent_links()
        conversations = [p for p in self._files() if self.is_conversation(p)]
        known = {os.path.splitext(os.path.basename(p))[0] for p in conversations}
        newest: dict[str, tuple[int, str]] = {}
        for path in conversations:
            # A stray or truncated *.db is not a session the browser can open, so it must
            # not be offered as one here either.
            sid = self._walk_to_root(os.path.splitext(os.path.basename(path))[0], links, known)
            last_active = self._last_active(path)
            if last_active is None:
                continue  # deleted mid-scan
            previous = newest.get(sid)
            if previous is None or last_active > previous[0]:
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

    @staticmethod
    def _last_active(path: str) -> int | None:
        """Newest mtime of the database OR its WAL sidecar, in ms.

        New activity can sit entirely in `-wal` while the main file's mtime never moves
        -- which `cache_inputs` already accounts for. Reading only the main file here
        would order a busy conversation as stale and hand `--status` the wrong one.
        """
        newest = None
        for candidate in (path, path + "-wal"):
            try:
                stamp = int(os.stat(candidate).st_mtime * 1000)  # ms, like Store's
            except OSError:
                continue
            newest = stamp if newest is None else max(newest, stamp)
        return newest

    def _head_cwd(self, path: str) -> str:
        # One small row read, not a parse: the workspace URI lives in a single blob.
        try:
            con = self._connect(path)
        except sqlite3.Error:
            return "(unknown)"
        try:
            meta = self._table_blob(con, "SELECT data FROM trajectory_metadata_blob LIMIT 1")
        except sqlite3.Error:
            return "(unknown)"
        finally:
            con.close()
        cwd = self._workspace(meta) if meta else ""
        return self._git_root(cwd) if cwd else "(unknown)"

    def _parent_links(self) -> dict[str, str]:
        """`{child conversation id: parent id}` from one small query per database.

        Unlike a nested-transcript layout, nothing in a path says who spawned whom -- the
        link lives inside a step's payload. This still avoids a full parse: it reads only
        the step payloads and pulls the one field, so a status poll pays a bounded scan
        rather than decoding every usage blob in the corpus.
        """
        claims: list[tuple[str, str]] = []
        for path in self._files():
            sid = os.path.splitext(os.path.basename(path))[0]
            try:
                con = self._connect(path)
            except sqlite3.Error:
                continue
            try:
                for _idx, payload in self._rows(
                    con,
                    "SELECT idx, step_payload FROM steps WHERE step_type = "
                    f"{self._STEP_SUBAGENT}",
                ):
                    if not isinstance(payload, (bytes, bytearray)) or not payload:
                        continue
                    child = pb_str(payload, *self._SUBAGENT_CHILD).strip()
                    if child and child != sid:
                        claims.append((sid, child))
            except sqlite3.Error:
                continue
            finally:
                con.close()
        return self._resolve_links(claims)

    @staticmethod
    def _resolve_links(claims: list[tuple[str, str]]) -> dict[str, str]:
        """Turn recorded (parent, child) claims into one acyclic `{child: parent}` map.

        The same resolution `_link_subagents` applies during a parse, so the status path
        and the browser cannot disagree about who the root is: a child is claimed once
        (the first parent wins, and the file order is sorted), and a claim that would
        close a loop is refused so every cycle keeps exactly one root.
        """
        links: dict[str, str] = {}
        for parent, child in claims:
            if child in links:
                continue  # already claimed
            ancestor, seen = parent, {child}
            while ancestor is not None and ancestor not in seen:
                seen.add(ancestor)
                ancestor = links.get(ancestor)
            if ancestor is not None:
                continue  # walking up from the parent came back to the child
            links[child] = parent
        return links

    @staticmethod
    def _walk_to_root(sid: str, links: dict[str, str], known: set[str]) -> str:
        # Stop at the last id that still has a database: a parent whose conversation was
        # deleted leaves the child a root of its own, which is what `_parse` does too.
        seen = {sid}
        while sid in links:
            parent = links[sid]
            if parent in seen or parent not in known:
                break
            sid = parent
            seen.add(sid)
        return sid

    def _subtree_files(self, workflow_id: str) -> list[str]:
        links = self._parent_links()
        known = {os.path.splitext(os.path.basename(p))[0] for p in self._files()}
        return [
            path
            for path in self._files()
            if self._walk_to_root(os.path.splitext(os.path.basename(path))[0], links, known)
            == workflow_id
        ]

    def root_of(self, session_id: str) -> str | None:
        # A spawned conversation resolves to the one that spawned it, at any depth: that
        # is the row --status has to price, not the branch below it.
        if not self._session_files(session_id):
            return None
        links = self._parent_links()
        known = {os.path.splitext(os.path.basename(p))[0] for p in self._files()}
        return self._walk_to_root(session_id, links, known)

    def status_nodes(self, workflow_id: str) -> list[dict]:
        # workflow_nodes for --status, off a read of just this conversation's database.
        if self._sessions is not None and workflow_id in self._sessions:
            return self.workflow_nodes(workflow_id)
        parsed: dict[str, dict] = {}
        for path in self._subtree_files(workflow_id):
            session = self._parse_file(path)
            if session:
                parsed[session["sid"]] = session
        return self._tree_nodes(self._assemble(parsed), workflow_id)

    # ---- public contract -----------------------------------------------------

    def workflows(self) -> list[Workflow]:
        self._sessions = None  # reload (r) re-reads fresh; model methods reuse the cache
        sessions = self._parse()
        rows = []
        for sid, s in sessions.items():
            if s["is_child"]:
                continue  # a subagent rolls up into the conversation that spawned it
            kids = self._descendants(sessions, sid)
            ended = max([s["ended_at"]] + [sessions[k]["ended_at"] for k, _d in kids], default="")
            worked = worked_seconds(
                [t["ts"] or None for t in self._subtree_turns_of(sessions, sid)]
                + [p["ts"] or None for p in s["prompts"]],
                [p["ts"] or None for p in s["prompts"]],
            )
            rows.append(
                Workflow(
                    id=sid,
                    title=s["title"],
                    directory=s["directory"],
                    created_at=s["created_at"],
                    root_cost=0.0,  # flat: no subagent tree, and nothing is priced
                    total_cost=0.0,
                    subagents=len(kids),
                    model_count=0,  # filled by App._load_model_cache
                    total_tokens=s["total_tokens"],
                    unpriced_tokens=s["unpriced_tokens"],
                    source=self.source_name,
                    ended_at=ended or s["ended_at"],
                    worked_seconds=worked,
                )
            )
        if self.demo:
            rows = [scramble_workflow(w, self.demo_scale, self.demo_cats) for w in rows]
        # Every row costs $0, so the order rides entirely on tokens; break ties by id so
        # it cannot reshuffle between launches (the ClaudeStore.sort_workflows rule).
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens, w.id), reverse=True)
        return rows

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

    def workflow_nodes(self, workflow_id: str) -> list[dict]:
        return self._tree_nodes(self._parse(), workflow_id)

    def _tree_nodes(self, sessions: dict[str, dict], workflow_id: str) -> list[dict]:
        s = sessions.get(workflow_id)
        if not s:
            return []
        nodes = self._nodes_from(workflow_id, s, demo=False)
        for child_id, depth in self._descendants(sessions, workflow_id):
            child = sessions[child_id]
            nodes.extend(self._nodes_from(child_id, child, depth=depth, demo=False))
        if self.demo:
            nodes = [scramble_node(n, self.demo_scale, self.demo_cats) for n in nodes]
        return nodes

    def _nodes_from(self, workflow_id: str, s: dict, depth: int = 0, demo=None) -> list[dict]:
        acc = self._new_acc()
        best, best_runs = UNKNOWN_MODEL, -1
        for model_name, m in s["models"].items():
            for key in acc:
                acc[key] += m[key]
            if m["runs"] > best_runs:
                best_runs, best = m["runs"], model_name
        nodes = [
            {
                "id": workflow_id,
                "depth": depth,
                "agent": s["agent"] or "-" if depth else "-",
                "title": s["title"],
                "created_at": s["created_at"],
                "cost": 0.0,
                "model_name": best,
                "tokens_input": acc["input"],
                "tokens_output": acc["output"],
                "tokens_reasoning": acc["reasoning"],
                "tokens_cache_read": acc["cache_read"],
                "tokens_cache_write": acc["cache_write"],
                "tokens_total": acc["tokens_total"],
            }
        ]
        if self.demo if demo is None else demo:
            nodes = [scramble_node(n, self.demo_scale, self.demo_cats) for n in nodes]
        return nodes

    def _subtree_turns_of(self, sessions: dict[str, dict], workflow_id: str) -> list[dict]:
        s = sessions.get(workflow_id)
        if not s:
            return []
        turns = list(s["turns"])
        for child_id, depth in self._descendants(sessions, workflow_id):
            child = sessions[child_id]
            agent = child["agent"] or "subagent"
            turns.extend({**t, "depth": depth, "agent": agent} for t in child["turns"])
        return turns

    def _subtree_turns(self, workflow_id: str) -> list[dict]:
        return self._subtree_turns_of(self._parse(), workflow_id)

    def tool_breakdown(self, workflow_id: str) -> list[dict]:
        # Per-(tool, model) attribution over the whole subtree: each generation's tokens
        # split evenly across the calls it requested, the Store.tool_breakdown semantics.
        return tool_rows_from_turns(self._subtree_turns(workflow_id))

    def message_timeline(self, workflow_id: str) -> list[dict]:
        # Chronological per-call rows for the Turns tab, each tagged with the prompt in
        # force at its timestamp. A generation whose own stamp this build does not record
        # inherits the session's created-at, so it sorts before the replies it preceded
        # rather than landing at the epoch.
        s = self._parse().get(workflow_id)
        if not s:
            return []
        prompts = sorted(s["prompts"], key=lambda p: p["ts"] or 0.0)
        out = []
        index, cur_title, cur_full = 0, "", ""
        for turn in sorted(self._subtree_turns(workflow_id), key=lambda r: r["ts"] or 0.0):
            while index < len(prompts) and (prompts[index]["ts"] or 0.0) <= (turn["ts"] or 0.0):
                cur_full = prompts[index]["title"]
                cur_title = _clean_prompt(cur_full)
                index += 1
            row = dict(turn)
            row["time"] = self._fmt(row.pop("ts") or None)
            row["prompt_id"] = cur_title
            row["prompt_title"] = cur_title
            row["prompt_full"] = cur_full
            out.append(row)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        return True

    def supports_tools(self, workflow_id: str) -> bool:
        # A tool-call step records the tool it invoked, which rides on the generation
        # that asked for it; a conversation that called nothing shows the honest empty
        # message rather than losing the tab.
        return True
