"""Claude Code JSONL transcript backend."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re

from opentab.demo import demo_config, scramble_node, scramble_workflow
from opentab.formatting import _clean_prompt, iso_to_epoch, iso_to_local, worked_seconds
from opentab.models import Workflow
from opentab.pricing import api_equivalent_cost
from opentab.util import (
    ATTACHMENT_EST_TOKENS,
    context_add,
    context_rows,
    est_tokens,
    git_root,
    read_files_parallel,
    safe_int,
    tool_rows_from_turns,
)


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
        # Demo mode: which categories to scramble (titles/turns/spend) and the
        # hidden magnitude factor (1.0 unless spend is scrambled). See demo_config.
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._sessions: dict[str, dict] | None = None  # parsed lazily / on reload
        self._one: tuple[str, dict] | None = None  # last single-transcript parse (_session)
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
            # The 1-hour-TTL SUBSET of cache_write (never added to tokens_total -- it is
            # part of cache_write, not extra tokens). Anthropic bills a 1h write at 2.00x
            # input against the 5m tier's 1.25x, and the catalog only carries the 5m rate,
            # so without this every long write is undercharged. See pricing.
            "cache_write_1h": 0,
            "tokens_total": 0,
        }

    @staticmethod
    def _int(value) -> int:
        # A usage field is whatever the transcript says it is, and a bare int() takes
        # the WHOLE backend down on a string, a nested object, or a number JSON allows
        # and float cannot hold. util.safe_int is the one rule the file backends coerce
        # through; Claude's usage read had no coercion at all.
        return safe_int(value)

    @classmethod
    def _add_usage(cls, acc: dict[str, int], u: dict) -> None:
        i = cls._int(u.get("input_tokens", 0) or 0)
        o = cls._int(u.get("output_tokens", 0) or 0)
        cr = cls._int(u.get("cache_read_input_tokens", 0) or 0)
        cw = cls._int(u.get("cache_creation_input_tokens", 0) or 0)  # cache creation == write
        # usage.cache_creation splits that same total by TTL
        # ({ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}, summing to the flat
        # field). Only the 1h half is kept: the 5m half is the remainder, and deriving it
        # means a transcript that omits the split (or a future third tier) still prices at
        # the old 5m rate rather than losing tokens.
        cc = u.get("cache_creation")
        cw1h = cls._int(cc.get("ephemeral_1h_input_tokens", 0) or 0) if isinstance(cc, dict) else 0
        acc["runs"] += 1
        acc["input"] += i
        acc["output"] += o
        acc["cache_read"] += cr
        acc["cache_write"] += cw
        acc["cache_write_1h"] += min(cw1h, cw)  # a subset, whatever the log claims
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
            acc.get("cache_write_1h", 0),
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

    # The Context tab's "injected context" sub-buckets: fold each wrapper tag to a
    # readable kind so the composition tree shows "system reminders" instead of
    # eleven raw tag spellings.
    _INJECTED_KINDS = (
        ("<system-reminder", "system reminders"),
        ("<bash-", "bash in/output"),
        ("<user-memory-input", "memory"),
        ("<local-command", "slash commands"),
        ("<command-", "slash commands"),
    )

    @classmethod
    def _injected_kind(cls, text: str) -> str | None:
        # The injected-context bucket for a wrapper-tagged user text, or None for a
        # genuine prompt (the composition twin of _prompt_text's wrapper skip).
        if not text.startswith(cls._WRAPPER_TAGS):
            return None
        return next((kind for tag, kind in cls._INJECTED_KINDS if text.startswith(tag)), "other")

    @classmethod
    def _prompt_text(cls, message) -> str | None:
        # A *real* user prompt's full text (the Turns tab can expand it; the
        # session-title fallback caps it at its use site). Returns None for empty
        # content or an injected wrapper (see above), so the caller keeps scanning
        # to the next user message.
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
        return text

    # --- parsing -------------------------------------------------------------
    def cache_inputs(self) -> list[str]:
        # Files whose (size, mtime) fingerprint the warm-start cache (CachedStore).
        return self._files()

    def _files(self) -> list[str]:
        return glob.glob(os.path.join(self.root_dir, "**", "*.jsonl"), recursive=True)

    # Claude Code writes a subagent's transcript to a sidecar file under
    # <project-slug>/<sessionId>/subagents/, and the records inside it carry the
    # PARENT's sessionId -- so a sidecar's file name is not a session id at all.
    _SIDECAR_DIR = "subagents"

    @classmethod
    def _sidecar_owner(cls, path: str) -> str | None:
        # The session a sidecar transcript belongs to, or None for a main transcript.
        # Keyed off the directory layout rather than the "agent-" file-name prefix,
        # which is Claude Code's to change.
        holder = os.path.dirname(path)
        if os.path.basename(holder) != cls._SIDECAR_DIR:
            return None
        return os.path.basename(os.path.dirname(holder)) or None

    def _transcripts(self, session_id: str) -> list[str]:
        # A session's transcript is <project-slug>/<sessionId>.jsonl. Resuming the
        # session from another directory can leave the same id under a second slug,
        # so glob for every copy -- plus the sidecars holding its subagents' turns,
        # whose records carry this session's id and whose tokens are part of its
        # subtree. Without them the single-transcript paths (_parse_one, and so the
        # cold status_nodes) priced the main thread alone and undercounted every
        # session that delegated. _parse() must NOT come here: it reads every file
        # through _files() already, and would count the sidecars twice.
        main = [
            p
            for p in glob.glob(
                os.path.join(self.root_dir, "**", session_id + ".jsonl"), recursive=True
            )
            # A sidecar whose file name happens to equal the id we were asked about is
            # not that session's transcript; keeping it would let a phantom
            # "agent-<hex>" id confirm itself in root_of.
            if self._sidecar_owner(p) is None
        ]
        sidecars = glob.glob(
            os.path.join(self.root_dir, "**", session_id, self._SIDECAR_DIR, "*.jsonl"),
            recursive=True,
        )
        return list(dict.fromkeys(main + sidecars))

    # How much of a transcript's TAIL to scan for a sessionKind marker. A background
    # session replays its parent's history verbatim into its own file -- same message
    # ids, same requestIds, same uuids and timestamps, only sessionId rewritten -- so
    # parent and child both claim the same API calls and the (message.id, requestId)
    # dedup below has to pick one. Measured on a real corpus, the marker first appears
    # ~157KB in (the replayed prefix carries none, and that prefix is as long as the
    # history it replays), so a head scan misses it; the tail always carries it,
    # because the background session is the one still writing.
    _SESSION_KIND_TAIL_BYTES = 8192

    def _replays_history(self, path: str) -> bool:
        # Whether this transcript belongs to a marked session kind -- one that may be
        # replaying another session's records rather than having made those calls.
        #
        # The window widens only when it holds no complete record: one transcript's
        # final line can be megabytes (a pasted prompt -- 1.37MB is the largest in a
        # real corpus, 141 records over 1MiB), and the marker sits near that record's
        # START, so a fixed tail would read the middle of it and see nothing. A newline
        # means we have seen a whole record and the marker really isn't on it, so the
        # common no costs exactly one 8KB read (measured: 364 files in 10ms, and only
        # 22 of them needed a second read).
        #
        # There is deliberately NO byte ceiling on the widening. One would reopen the
        # exact hole this loop closes, and it would protect nothing: the only caller
        # that scans every transcript is _parse(), which is about to read all of them
        # in full anyway, so the scan can never cost more than the parse behind it.
        # Termination is `window >= size` -- x8 growth reaches any file in a few steps.
        try:
            size = os.path.getsize(path)
            window = self._SESSION_KIND_TAIL_BYTES
            with open(path, "rb") as fh:
                while True:
                    fh.seek(max(0, size - window))
                    chunk = fh.read()
                    if b'"sessionKind"' in chunk:
                        return True
                    if b"\n" in chunk.rstrip(b"\n") or window >= size:
                        return False
                    window *= 8
        except OSError:
            return False

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        # Replay-capable transcripts go LAST, because the dedup credits the FIRST
        # claimer of a key: the session that actually made the calls must be parsed
        # before any session that merely replays them. Left to glob order the winner
        # was alphabetical luck -- measured, a parent session showed 0 tokens and an
        # empty Turns tab while its background child held all 96 of its turns. Totals
        # are unaffected either way (the calls are counted once); this decides WHICH
        # session they are counted under. A stable sort, so files keep glob order
        # within each group, and ~10ms of tail reads over a 370-file corpus.
        files = sorted(self._files(), key=self._replays_history)
        self._sessions = self._parse_texts(text for _path, text in read_files_parallel(files))
        return self._sessions

    def _parse_one(self, workflow_id: str, paths: list[str] | None = None) -> dict | None:
        # Parse only this session's own transcript(s) -- the --status fast path, so
        # pricing one session never pays for the whole ~/.claude/projects tree. A
        # transcript can replay resumed/forked history under other sessionIds; the
        # grouping lands those rows under their own ids and we return just ours.
        if paths is None:
            paths = self._transcripts(workflow_id)
        if not paths:
            return None
        return self._parse_texts(text for _path, text in read_files_parallel(paths)).get(
            workflow_id
        )

    def _session(self, workflow_id: str, fallback: bool = True) -> dict | None:
        # One session's parsed state for the per-session extras (subagent tree, Turns,
        # Tools, Context), off the single-transcript parse when the whole corpus hasn't
        # been read yet. Those four used to call _parse() each, so opening ONE session
        # read every transcript under ~/.claude/projects: 2.2s on a 367-file corpus, and
        # paid even with the warm-start cache HOT -- CachedStore serves workflows() from
        # disk without ever parsing, so the drill-in was the first thing that did, which
        # made `--goto` (a tmux popup that lands straight in a session) slow no matter
        # what. _parse_one reads just this session's own transcripts + sidecars: ~5ms.
        #
        # fallback=False is the --status contract (status_nodes): never widen to the
        # whole tree.
        if self._sessions is not None:
            return self._sessions.get(workflow_id)  # already parsed: nothing to save
        if self._one is not None and self._one[0] == workflow_id:
            return self._one[1]
        paths = self._transcripts(workflow_id)
        # A replay-capable transcript is the one case that CANNOT be read alone: it
        # holds its parent's records as well as its own, and the marker tags the whole
        # session rather than the replayed rows (measured: all 257 replayed records and
        # all 29 own-new ones carry it alike), so nothing in the file separates them.
        # Only the corpus parse can, by letting the parent claim its keys first.
        if paths and not any(self._replays_history(p) for p in paths):
            s = self._parse_one(workflow_id, paths)
            if s is not None:
                # Single-entry, not a growing map: App.prefetch_session_data asks for
                # all four extras of the same session back to back (that burst is the
                # whole point), while browsing N sessions in turn must not accumulate a
                # second copy of the corpus in memory.
                self._one = (workflow_id, s)
                return s
        if not fallback:
            # --status must answer without reading the tree, so it keeps the
            # single-transcript answer even for a replaying session (where that
            # over-reports by the history it replayed -- what --status already did
            # before this fast path existed).
            return self._parse_one(workflow_id, paths) if paths else None
        # Either a replaying transcript (above), or _parse_one came up empty while the
        # session still exists: a resumed/forked transcript replays records under their
        # ORIGINAL sessionId, so a session whose own file was since deleted or rotated
        # survives only inside another session's file -- _transcripts() can't find it by
        # name, the corpus parse still groups it.
        return self._parse().get(workflow_id)

    # A record whose JSON string holds a LITERAL control character is not the record the
    # writer meant to emit, and the plain `except ValueError: continue` dropped it with
    # no trace. Two shapes, one cause:
    #
    #   - a literal NEWLINE splits it across two or more physical lines, so each half
    #     fails json.loads on its own. Measured on a real corpus: 345 files / 96k lines,
    #     one dropped record -- a `user` one carrying no usage, so 0 tokens lost and at
    #     worst a missing Turns-tab prompt header.
    #   - a literal TAB (or a lone \r) keeps the record on one line but still fails.
    #
    # `strict=False` is exactly the rule that rejects a literal control character inside
    # a string, so parsing non-strict throughout recovers the second shape outright --
    # and it is used for EVERY line, not just for a rejoin, so that the arming signal
    # below cannot depend on WHICH control character split the record. Strict, a half
    # ending `..."a` fails with "Unterminated string" but one ending `..."a\r` fails
    # with "Invalid control character"; non-strict, both say "Unterminated string".
    # (`util._read_text` reads in text mode, so \r\n and a lone \r are already \n by the
    # time they get here -- this keeps _records correct for a caller that doesn't, which
    # is worth more than assuming every future one will.)
    #
    # The relaxation is narrowed to the three characters the recovery is ABOUT (tab, LF,
    # CR), because "tolerate control characters" is wider than "tolerate the ones that
    # split a record". Strict parsing rejects every literal byte below 0x20; the
    # difference is a literal ESC, backspace or NUL, none of which a JSON writer emits
    # unescaped, all of which end up in a title, and one of which curses acts on -- a
    # title of "AB\x08C" paints as "AC", and a run of backspaces walks back over the
    # column beside it. So a line carrying any OTHER literal control is refused exactly
    # as before. (This is only about LITERAL bytes: `\b`/`` written properly is
    # ordinary JSON that strict mode has always accepted and still does, so the
    # rendering question that raises is a separate one, unchanged by any of this.)
    _BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

    # The rejoin is bounded on both axes, because the recovery must not cost more than
    # it saves: the buffer is armed only by that "Unterminated string" failure (garbage
    # fails with "Expecting value" and never arms it) and dropped the moment it stops
    # looking like one or outgrows either cap. Without that, a stray text file with a
    # quote in it would accumulate to EOF and re-parse a growing buffer per line.
    _SPLIT_MAX_LINES = 64
    _SPLIT_MAX_BYTES = 1 << 20

    @staticmethod
    def _unterminated(exc: ValueError) -> bool:
        # "this text is a record cut off inside a string", the one failure a rejoin can
        # fix. Read off JSONDecodeError.msg, which is the stable half of the message
        # (str(exc) appends a line/column that varies).
        return str(getattr(exc, "msg", "")).startswith("Unterminated string")

    @classmethod
    def _one(cls, raw: str) -> tuple:
        # (record, cut-off?) for ONE physical line. The record is None when the line is
        # not a complete one; `cut-off` says the failure was "Unterminated string", the
        # only one a rejoin can fix.
        #
        # Judged on the STRIPPED line, because that is what the plain parser judged and
        # the recovery may not lose what it kept: `str.strip()` removes \x0b, \x0c and
        # \x1c..\x1f as whitespace, so a record padded with a form feed parsed fine
        # before -- and would be refused here by a control scan over the raw line, which
        # cannot tell padding from content. Inside the line those same bytes still
        # count, and a REJOIN buffers the raw line instead, where every character is
        # part of the string being reassembled.
        #
        # Parsed STRICT first, relaxed only after that fails, so the recovery costs
        # nothing on the ~100% of lines that are simply valid: running the
        # control-character scan on every line instead cost 1.6s on a 650MB corpus
        # (2.1s -> 3.7s, measured) -- the whole warm-start budget, spent on a record
        # that turns up once in 96k lines.
        line = raw.strip()
        if not line:
            return None, False
        try:
            return json.loads(line), False
        except ValueError:
            pass
        if cls._BAD_CONTROL.search(line):
            return None, False  # refused exactly as strict refused it
        try:
            return json.loads(line, strict=False), False
        except ValueError as exc:
            return None, cls._unterminated(exc)

    @classmethod
    def _records(cls, text: str):
        # Every JSON *object* in one NDJSON blob. Non-objects are skipped, not ingested:
        # `[]`, `"x"` and `0` are all valid JSON that survive the except and would then
        # raise AttributeError out of .get(), taking down the whole backend rather than
        # the one line. Lines are fed unstripped so a rejoin keeps the string's own
        # characters; json.loads ignores surrounding whitespace anyway.
        pending: list[str] = []
        size = 0
        for raw in text.split("\n"):
            # A line that is a complete RECORD is one -- never a continuation, whatever
            # an open buffer would have made of it. That ordering is the whole safety
            # property: the recovery may only ever ADD records the old parser dropped,
            # never absorb one it kept. Found by fuzzing, because the counter-example
            # is not the obvious one -- a following `{"type":…}` closes the buffer's
            # dangling string on its first quote and fails, so it falls out safely, but
            # a record with NO quote in it (`{}`) just extends that string and vanished
            # into the buffer (10 losses in 120k blobs).
            #
            # A complete non-DICT is deliberately not authoritative: `2`, `null` and
            # `"x"` are valid JSON that this parser skips anyway, so letting one break
            # the buffer would cost the recovery for nothing -- a prompt whose second
            # line reads `2` splits into a middle line that is a perfectly good JSON
            # number.
            obj, cut_off = cls._one(raw)
            if isinstance(obj, dict):
                pending, size = [], 0  # the buffer was never going to close
                yield obj
                continue
            if pending:
                if cls._BAD_CONTROL.search(raw):
                    pending, size = [], 0  # can neither join nor stand on its own
                    continue
                pending.append(raw)
                size += len(raw) + 1
                try:
                    joined = json.loads("\n".join(pending), strict=False)
                except ValueError as exc:
                    if (
                        len(pending) < cls._SPLIT_MAX_LINES
                        and size < cls._SPLIT_MAX_BYTES
                        and cls._unterminated(exc)
                    ):
                        continue  # still inside the split string -- keep joining
                    pending, size = [], 0  # give up on it
                else:
                    pending, size = [], 0
                    if isinstance(joined, dict):
                        yield joined
                continue
            if cut_off and not cls._BAD_CONTROL.search(raw):
                # Armed on the RAW line, and only when the raw line is clean: _one
                # judged it stripped (padding is not content), but a buffer keeps every
                # character, so a trailing \x0c that was mere padding on a standalone
                # line becomes string content once something is joined onto it -- the
                # refused byte back in through the side door. A padded line that is
                # already a whole record never reaches here; one that isn't was dropped
                # by the plain parser anyway, so refusing to rejoin it loses nothing.
                pending, size = [raw], len(raw)

    def _parse_texts(self, texts) -> dict[str, dict]:
        sessions: dict[str, dict] = {}
        seen: set = set()  # dedupe resumed/forked overlap on (message.id, requestId)
        for text in texts:
            for obj in self._records(text):
                self._ingest(obj, sessions, seen)
        for sid, s in sessions.items():
            self._finalize(sid, s)
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
                "ts_max": None,
                "title_ai": None,
                "title_custom": None,
                "title_prompt": None,
                "models": {},  # model_name -> {"total": acc, "root": acc}
                "uuid_parent": {},  # uuid -> parentUuid (for grouping sidechains)
                "side_uuids": set(),  # uuids flagged isSidechain
                "side_usage": {},  # sidechain-assistant uuid -> (model_name, acc)
                "turns": [],  # per-message rows for the Turns tab (chronological)
                "prompts": [],  # {ts,title,id} per real user prompt, for Turns grouping
                "event_ts": [],  # every record's raw ISO ts, for worked_seconds
                "context": {},  # (category, kind) -> [count, est_tokens], Context tab
                "pending_tools": {},  # tool_use id -> name, consumed by its tool_result
                "ctx_seen": set(),  # record uuids already composed (replay dedup)
                "turn_by_key": {},  # (message.id, requestId) -> index into turns
            }
        cwd = o.get("cwd")
        if cwd and not s["cwd"]:
            s["cwd"] = cwd
        ts = o.get("timestamp")
        if ts and (s["ts_min"] is None or ts < s["ts_min"]):
            s["ts_min"] = ts
        if ts and (s["ts_max"] is None or ts > s["ts_max"]):
            s["ts_max"] = ts  # ISO strings order lexicographically
        if ts:
            s["event_ts"].append(ts)  # an activity point for worked_seconds
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
                    s["title_prompt"] = text[:80]  # the session-title fallback stays short
        if typ == "user" and o.get("isSidechain") is not True:
            # Sidechain (Task) content runs in the subagent's own context window, so
            # only main-thread content counts toward this session's composition.
            # Same record-uuid replay guard as the assistant side: a session resumed
            # under a second project slug replays these records verbatim, and usage
            # dedup alone would leave the user-side composition double-counted.
            if uuid is None or uuid not in s["ctx_seen"]:
                if uuid is not None:
                    s["ctx_seen"].add(uuid)
                self._ingest_user_context(o, s)
        if typ != "assistant":
            return
        msg = o.get("message")
        if not isinstance(msg, dict):
            return
        usage, model = msg.get("usage"), msg.get("model")
        if not isinstance(usage, dict) or not model or model == "<synthetic>":
            return  # nothing priceable on this row
        side = o.get("isSidechain") is True
        # A streamed assistant message lands as SEVERAL records -- one content
        # block each, same (message.id, requestId), each echoing the full usage.
        # The `seen` dedup below keeps usage single-counted, but content must be
        # walked on every record or later blocks (typically the tool_use after a
        # thinking block) vanish. `fresh` is the record-uuid replay guard: a
        # session resumed under a second project slug replays these records
        # verbatim, and both the composition walk and the tool-name fold below
        # must count each record exactly once.
        fresh = uuid is None or uuid not in s["ctx_seen"]
        if fresh and uuid is not None:
            s["ctx_seen"].add(uuid)
        if fresh and not side:
            self._ingest_assistant_context(msg, s)
        # The tool_use blocks this step invoked (duplicates kept: two Bash calls =
        # two calls, two shares) -- tool_breakdown splits the turn's tokens across
        # them, the Store.tool_breakdown attribution.
        tools = [
            c.get("name")
            for c in (msg.get("content") or [])
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name")
        ]
        key = (msg.get("id"), o.get("requestId"))
        if all(key):
            if key in seen:
                # A later content-block record of the same message: its usage is the
                # echo (skip), but its tool calls belong to the turn the first record
                # opened -- fold them in so the Tools tab sees the whole step.
                if tools and fresh:
                    idx = s["turn_by_key"].get(key)
                    if idx is not None:
                        s["turns"][idx]["tools"].extend(tools)
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
        i = self._int(usage.get("input_tokens", 0) or 0)
        out_t = self._int(usage.get("output_tokens", 0) or 0)
        cr = self._int(usage.get("cache_read_input_tokens", 0) or 0)
        cw = self._int(usage.get("cache_creation_input_tokens", 0) or 0)
        _cc = usage.get("cache_creation")
        cw1h = (
            min(self._int(_cc.get("ephemeral_1h_input_tokens", 0) or 0), cw)
            if isinstance(_cc, dict)
            else 0
        )
        if all(key):
            s["turn_by_key"][key] = len(s["turns"])  # later block records fold in here
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
                "cache_write_1h": cw1h,  # subset of cache_write; long-TTL rate under $
                "tokens_total": i + out_t + cr + cw,
                "tools": tools,
            }
        )
        if o.get("isSidechain") is True:
            acc = self._new_acc()
            self._add_usage(acc, usage)
            s["side_usage"][uuid or len(s["side_usage"])] = (model_name, acc)
        else:
            self._add_usage(entry["root"], usage)

    # --- context composition (the Context tab) --------------------------------
    # What the session's context window filled up with, estimated at chars/4
    # (util.est_tokens) since transcripts record content but not per-block token
    # counts. The system prompt and tool/MCP schemas are NOT in any transcript --
    # they exist only in the live request -- so they can never appear here; the
    # renderer surfaces them as the measured first-turn baseline instead.

    def _ingest_user_context(self, o: dict, s: dict) -> None:
        msg = o.get("message")
        if not isinstance(msg, dict):
            return
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(blocks, list):
            return
        ctx = s["context"]
        for b in blocks:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                text = (b.get("text") or "").strip()
                if not text:
                    continue
                if o.get("isCompactSummary") is True:
                    context_add(ctx, "compaction summaries", "", est_tokens(text))
                    continue
                kind = self._injected_kind(text)
                if kind is None and o.get("isMeta") is True:
                    kind = "other"
                if kind is not None:
                    context_add(ctx, "injected context", kind, est_tokens(text))
                else:
                    context_add(ctx, "user prompts", "", est_tokens(text))
            elif bt == "tool_result":
                name = s["pending_tools"].pop(b.get("tool_use_id"), "") or "(unknown)"
                context_add(ctx, "tool results", name, self._est_result_tokens(b.get("content")))
            elif bt == "image":
                context_add(ctx, "attachments", "image", ATTACHMENT_EST_TOKENS["image"])

    def _ingest_assistant_context(self, msg: dict, s: dict) -> None:
        ctx = s["context"]
        for c in msg.get("content") or []:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text":
                context_add(ctx, "assistant text", "", est_tokens(c.get("text") or ""))
            elif ct in ("thinking", "redacted_thinking"):
                blob = c.get("thinking") or c.get("data") or ""
                context_add(ctx, "reasoning", "", est_tokens(blob))
            elif ct == "tool_use":
                name = c.get("name") or "(unknown)"
                if c.get("id"):
                    s["pending_tools"][c["id"]] = name
                try:
                    params = json.dumps(c.get("input") or {})
                except (TypeError, ValueError):
                    params = str(c.get("input") or "")
                context_add(ctx, "tool call params", name, est_tokens(params))

    @staticmethod
    def _est_result_tokens(content) -> int:
        # A tool_result's content is a bare string or a list of text/image/
        # tool_reference blocks; an embedded image costs its flat attachment guess.
        if isinstance(content, str):
            return est_tokens(content)
        total = 0
        if isinstance(content, list):
            for x in content:
                if not isinstance(x, dict):
                    total += est_tokens(str(x))
                elif x.get("type") == "image":
                    total += ATTACHMENT_EST_TOKENS["image"]
                else:
                    total += est_tokens(x.get("text") or "")
        return total

    def context_breakdown(self, workflow_id: str) -> list[dict]:
        # Estimated composition rows for the Context tab (what filled the window),
        # flattened by util.context_rows; the measured growth curve comes from the
        # turn rows, not from here.
        s = self._session(workflow_id)
        return context_rows(s["context"]) if s else []

    def supports_context(self, workflow_id: str) -> bool:
        # Transcripts always carry full message content, so composition applies to
        # every session.
        return True

    def _finalize(self, sid: str, s: dict) -> None:
        s["title"] = s["title_custom"] or s["title_ai"] or s["title_prompt"] or "(untitled)"
        s["directory"] = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"
        s["created_at"] = iso_to_local(s["ts_min"])
        s["ended_at"] = iso_to_local(s["ts_max"]) if s["ts_max"] else ""
        # Active working time: every record is an activity point; the real user
        # prompts (already filtered of tool-result "user" messages) mark the idle
        # gaps -- the wait before each is you composing, not the agent working.
        s["worked_seconds"] = worked_seconds(
            [iso_to_epoch(t) for t in s["event_ts"]],
            [iso_to_epoch(p["ts"]) for p in s["prompts"]],
        )
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
                    "cache_write_1h": tot["cache_write_1h"],  # subset of cache_write
                    "output": tot["output"],
                    "unpriced_input": tot["input"],
                    "unpriced_reasoning": tot["reasoning"],
                    "unpriced_cache_read": tot["cache_read"],
                    "unpriced_cache_write": tot["cache_write"],
                    # The 1h-TTL subset of the cache_write above, so App._compute_api_costs
                    # can bill it at the long-TTL rate instead of the 5m one. A subset, so
                    # nothing that sums or displays cache_write has to know about it.
                    "unpriced_cache_write_1h": tot["cache_write_1h"],
                    "unpriced_output": tot["output"],
                    "root_unpriced_input": root["input"],
                    "root_unpriced_reasoning": root["reasoning"],
                    "root_unpriced_cache_read": root["cache_read"],
                    "root_unpriced_cache_write": root["cache_write"],
                    "root_unpriced_cache_write_1h": root["cache_write_1h"],
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
            # Subset of tokens_cache_write, so _price_root (--status) and the Subagents
            # tab bill long-TTL writes at the long-TTL rate. Readers that don't know the
            # key see the total and price it the old way.
            "tokens_cache_write_1h": acc["cache_write_1h"],
            "tokens_total": acc["tokens_total"],
        }

    # --- Store interface -----------------------------------------------------
    def workflows(self) -> list[Workflow]:
        self._sessions = None  # reload (r) re-reads fresh; model methods reuse cache
        self._one = None  # ... and so must the single-transcript memo behind _session
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
                    ended_at=s["ended_at"],
                    worked_seconds=s["worked_seconds"],
                )
            )
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens), reverse=True)
        return rows

    def _demo_workflow(self, w: Workflow) -> Workflow:
        # Mirror Store._demo_workflow: anonymize, backfill a synthetic price for the
        # (all-unpriced) tokens so the demo shows plausible spend, then scale.
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

    # How much of a transcript head to scan for its "cwd" -- every Claude Code
    # record carries one, so the first complete line normally answers; the budget
    # only bounds the pathological transcript that opens with megabytes of pasted
    # prompt, keeping the --status poll cheap.
    _CWD_HEAD_BYTES = 262144

    def _transcript_cwd(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                remaining = self._CWD_HEAD_BYTES
                while remaining > 0:
                    line = fh.readline()
                    if not line:
                        break
                    remaining -= len(line)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    cwd = obj.get("cwd") if isinstance(obj, dict) else None
                    if cwd:
                        return cwd
        except OSError:
            pass
        return "(unknown)"

    def recent_roots(self) -> list[dict]:
        # Root sessions newest-activity-first -- the cheap sibling of
        # Store.recent_roots for the one-shot --status command. No parse: Claude
        # Code appends every message (sidechains included) to the session's own
        # transcript, so the file mtime IS the subtree's last activity, and the
        # session id is the file name. "directory" is read lazily from the file
        # head (_TranscriptRoot), so a scan stops paying at the row that matches.
        newest: dict[str, _TranscriptRoot] = {}
        main_mtime: dict[str, int] = {}  # mtime of the main transcript each row reads cwd from
        for path in self._files():
            # A sidecar is named for the subagent, not the session, and its records
            # carry the parent's id -- so fold it into the session that owns it. Taking
            # the file name would mint a phantom root that no parse can resolve, and
            # since sidecars are usually the freshest files it would sort to the top and
            # be exactly what --status priced. Folding (rather than skipping) is what
            # keeps a busy subagent bumping its real root to the front.
            owner = self._sidecar_owner(path)
            sid = owner or os.path.basename(path)[: -len(".jsonl")]
            try:
                last_active = int(os.stat(path).st_mtime * 1000)  # ms, like Store's
            except OSError:
                continue  # deleted mid-scan
            row = newest.get(sid)
            if row is None:
                newest[sid] = _TranscriptRoot(self, path, sid, last_active)
                if owner is None:
                    main_mtime[sid] = last_active
                continue
            if last_active > row["last_active"]:
                row["last_active"] = last_active
            # Read "directory" from a main transcript when the session has one (a sidecar
            # only stands in for a session whose own file we haven't reached yet, or that
            # no longer exists) -- and from the NEWEST such copy. Resuming from another
            # directory leaves the same id under a second project slug with a different
            # cwd, so taking whichever copy the scan happened to see last could report a
            # directory the session has since moved away from, and a --status <dir> there
            # would miss it.
            if owner is None and last_active > main_mtime.get(sid, -1):
                row.prefer(path)
                main_mtime[sid] = last_active
        return sorted(newest.values(), key=lambda r: r["last_active"], reverse=True)

    def root_of(self, session_id: str) -> str | None:
        # A Claude session id is already its root -- sidechain (Task) messages live
        # inside the same transcript -- so this only confirms the transcript exists.
        return session_id if self._transcripts(session_id) else None

    def workflow_nodes(self, workflow_id: str) -> list[dict]:
        s = self._session(workflow_id)
        if not s:
            return []
        return self._nodes(workflow_id, s)

    def status_nodes(self, workflow_id: str) -> list[dict]:
        # workflow_nodes for the --status one-shot: identical rows off the same
        # single-transcript parse, minus the full-corpus fallback. _price_root only
        # asks after root_of confirmed the transcript, so nothing here means "not this
        # backend's session" -- and a status poll must answer that without reading
        # the tree.
        s = self._session(workflow_id, fallback=False)
        if not s:
            return []
        return self._nodes(workflow_id, s)

    def _nodes(self, workflow_id: str, s: dict) -> list[dict]:
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
        s = self._session(workflow_id)
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
            r["time"] = iso_to_local(r.pop("ts"))
            r["prompt_id"] = cur_id
            r["prompt_title"] = cur_title
            r["prompt_full"] = cur_full
            out.append(r)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        return True

    def tool_breakdown(self, workflow_id: str) -> list[dict]:
        # Per-(tool, model) token attribution for the Tools tab, the
        # Store.tool_breakdown semantics off the in-memory turn rows: each
        # assistant message is one LLM step whose recorded tokens are split evenly
        # across the tool_use blocks it invoked (sidechain steps included -- the
        # subtree is one transcript). Cost stays $0 (recorded); the "$" view
        # reprices per (tool, model) row like every other Claude panel.
        s = self._session(workflow_id)
        return tool_rows_from_turns(s["turns"]) if s else []

    def supports_tools(self, workflow_id: str) -> bool:
        # Claude Code transcripts always record tool_use blocks, so the tab applies
        # to every session; one without tool calls shows the honest empty message.
        return True

    def _demo_node(self, n: dict) -> dict:
        return scramble_node(n, self.demo_scale, self.demo_cats)


class _TranscriptRoot(dict):
    """A recent_roots() row over one transcript file. Id and last-active come free
    from the file name and mtime; "directory" (the session's cwd) needs the file
    head, so it is read only on first access -- a --status project scan walks rows
    newest-first and stops reading files at the row that matches."""

    def __init__(self, store: ClaudeStore, path: str, sid: str, last_active: int):
        super().__init__(id=sid, last_active=last_active)
        self._store = store
        self._path = path

    def prefer(self, path: str) -> None:
        # Read the cwd from this file instead. Called while recent_roots is still
        # building the rows, i.e. before anything can have cached "directory".
        self._path = path

    def __getitem__(self, key):
        if key == "directory" and "directory" not in self:
            self["directory"] = self._store._transcript_cwd(self._path)
        return super().__getitem__(key)
