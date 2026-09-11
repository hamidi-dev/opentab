"""Opt-in retained-text index. No source discovery, reads, or permission orchestration.

Snapshots control incremental *writes*, not source reads. Chunks are sensitive text;
deleting them does not promise erasure from storage snapshots or backups.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from . import paths
from .conversation import ConversationError

SCHEMA_VERSION = 2
_LEGACY_SCHEMA_VERSION = 1
_FIELDS = ("session_key", "native_id", "harness", "machine", "project", "title", "source_id")
_SCHEMA = (
    "CREATE TABLE roots (session_key TEXT PRIMARY KEY, metadata TEXT NOT NULL, "
    "fingerprint TEXT NOT NULL, limitations TEXT NOT NULL)",
    "CREATE TABLE chunks (id INTEGER PRIMARY KEY, session_key TEXT NOT NULL "
    "REFERENCES roots(session_key), execution_id TEXT NOT NULL, record_id TEXT NOT NULL, "
    "message_id TEXT, snapshot TEXT NOT NULL, timestamp TEXT NOT NULL, source TEXT NOT NULL, "
    "message_date TEXT, chunk_start INTEGER NOT NULL, chunk_end INTEGER NOT NULL, "
    "text TEXT NOT NULL, title TEXT NOT NULL)",
    "CREATE INDEX chunks_root ON chunks(session_key)",
    "CREATE TABLE index_state (id INTEGER PRIMARY KEY CHECK(id=1), updated_at TEXT)",
    "CREATE TABLE source_manifests (session_key TEXT PRIMARY KEY REFERENCES roots(session_key), manifest TEXT NOT NULL)",
    "CREATE VIRTUAL TABLE passages USING fts5(text, title, content='chunks', "
    "content_rowid='id', tokenize='unicode61 remove_diacritics 2')",
)
_OBJECTS = ("roots", "chunks", "chunks_root", "index_state", "source_manifests", "passages")
_LEGACY_OBJECTS = ("roots", "chunks", "chunks_root", "index_state", "passages")


@contextmanager
def _errors():
    try:
        yield
    except (sqlite3.Error, OSError, ValueError):
        raise ConversationError(
            "index_unavailable",
            "Conversation index is unavailable; check storage and SQLite FTS5 support.",
        ) from None


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _path(path):
    return (
        Path(path)
        if path is not None
        else Path(paths.cache_dir()) / "conversations" / "index.sqlite3"
    )


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError
    return date.fromisoformat(value)


def _validate(query, since, until, limit, max_chars):
    if not isinstance(query, str) or not 1 <= len(query) <= 1000:
        raise ConversationError("invalid_arguments", "query must contain 1 to 1000 characters.")
    for name, value, maximum in (("limit", limit, 100), ("max_chars", max_chars, 120000)):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ConversationError(
                "invalid_arguments", f"{name} must be an integer from 1 to {maximum}."
            )
    try:
        start = _date(since) if since is not None else None
        end = _date(until) if until is not None else None
        if start is not None and end is not None and end < start:
            raise ValueError
    except ValueError:
        raise ConversationError(
            "invalid_arguments", "Use valid YYYY-MM-DD dates with until >= since."
        ) from None
    # Let the actual tokenizer define terms, including its Unicode version and accents.
    # This database is memory-only and contains only the bounded query, never sources.
    with _errors():
        db = sqlite3.connect(":memory:")
        try:
            db.execute(
                "CREATE VIRTUAL TABLE tokens USING fts5(text, tokenize='unicode61 remove_diacritics 2')"
            )
            db.execute("CREATE VIRTUAL TABLE vocabulary USING fts5vocab(tokens, 'instance')")
            db.execute("INSERT INTO tokens(text) VALUES (?)", (query,))
            terms = [row[0] for row in db.execute("SELECT term FROM vocabulary ORDER BY offset")]
        finally:
            db.close()
    if not 1 <= len(terms) <= 64:
        raise ConversationError("invalid_arguments", "query must contain 1 to 64 searchable terms.")
    return list(dict.fromkeys(terms))


def validate_search(query, *, since=None, until=None, limit=10, max_chars=6000) -> None:
    _validate(query, since, until, limit, max_chars)


def _message_date(value):
    try:
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            stamp = datetime.fromtimestamp(value / 1000, timezone.utc)
        elif isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # No timezone means no known UTC day; do not infer the machine's timezone.
            if stamp.tzinfo is None:
                return None
        else:
            return None
        return stamp.astimezone(timezone.utc).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _token_char(char):
    return not char.isascii() or char.isalnum()


def _chunk_spans(text):
    """Bounded source slices with up to 200 chars of token-safe overlap.

    Only ASCII nonalphanumeric characters are known-safe separators. Python's
    Unicode tables differ from unicode61, so every non-ASCII character stays in
    its conservative span, even if SQLite treats it as a separator.
    """
    spans, omitted = [], False
    start = covered = 0
    while start < len(text):
        end = min(start + 2000, len(text))
        if end < len(text) and _token_char(text[end - 1]) and _token_char(text[end]):
            while end > start and _token_char(text[end - 1]):
                end -= 1
        if end <= covered and start < covered:
            # Drop overlap when it would prevent the next complete token fitting.
            start = covered
            continue
        if end == start:
            # This conservative span exceeds the budget. Skip it, not fragments.
            end = start + 2000
            while end < len(text) and _token_char(text[end]):
                end += 1
            omitted = True
            start = covered = end
            continue
        spans.append((start, end))
        covered = end
        if end == len(text):
            break
        start = max(start + 1, end - 200)
        while start < end and _token_char(text[start - 1]) and _token_char(text[start]):
            start += 1
    return spans, omitted


def _matched_span(text, marked, opening, closing):
    # Trust offsets only when removing well-formed markers reproduces the entire
    # source exactly. A literal lookup could select an earlier non-token substring.
    plain, offset, position, length = [], 0, None, 0
    while opening in marked:
        before, _, marked = marked.partition(opening)
        literal, separator, marked = marked.partition(closing)
        if closing in before or not separator or not literal or opening in literal:
            return None, 0
        plain.extend((before, literal))
        offset += len(before)
        if position is None:
            position, length = offset, len(literal)
        offset += len(literal)
    plain.append(marked)
    if closing in marked or "".join(plain) != text:
        return None, 0
    return position, length


class ConversationIndex:
    """Private SQLite handle; sources must be fresh text-only conversation_source results.

    roots() adds merged source limitations to the seven supplied metadata fields.
    status()/clear() return exists, schema_version, roots, passages, updated_at.
    Candidate offsets address newline-joined message parts, not original files.
    Ranking is lexical, not confidence: title-only hits need not match the excerpt.
    AND applies within a chunk (including its title), not across a whole message.
    Conservative boundaries may reduce overlap; spans over 2000 chars are omitted
    with an explicit root limitation, without changing source records. Root mode
    caps a fair pool of execution heads: one per root before more executions, then
    returns the admitted pool in rank order. Record mode caps record heads; none
    caps chunks. Each hit carries its transaction's metadata.
    match_offset/match_length locate one visible body-match span in the excerpt;
    absent body evidence uses None/0. Long matches may be clipped to 600 chars.
    """

    def __init__(self, path=None, *, write=False):
        self.path = _path(path).absolute()
        self.write = write
        self._db = None
        with _errors():
            self._check_paths()
            if not self.path.exists() and not write:
                raise ConversationError("index_not_built", "Conversation index has not been built.")
            created = False
            if write:
                missing = []
                parent = self.path.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir(mode=0o700, exist_ok=True)
                self._check_paths()
                try:
                    fd = os.open(
                        self.path,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
                    created = True
            self._check_paths()
            try:
                self._db = sqlite3.connect(
                    self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                    uri=True,
                    isolation_level=None,
                )
                self._db.row_factory = sqlite3.Row
                self._db.execute("PRAGMA busy_timeout=5000")
                self._db.execute("PRAGMA temp_store=MEMORY")
                self._db.execute("PRAGMA foreign_keys=ON")
                if created:
                    with self._transaction():
                        for statement in _SCHEMA:
                            self._db.execute(statement)
                        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                        self._db.execute("INSERT INTO index_state VALUES (1, NULL)")
                    self._check_schema()
                elif write:
                    self._migrate()
                else:
                    self._check_schema()
                if write:
                    if self._db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                        raise ConversationError(
                            "index_unavailable", "Conversation index requires DELETE journaling."
                        )
                    self._db.execute("PRAGMA secure_delete=ON")
            except BaseException:
                if self._db is not None:
                    self._db.close()
                raise

    def _check_paths(self):
        for path, directory in ((self.path.parent, True), (self.path, False)):
            if path.is_symlink():
                raise ConversationError(
                    "index_unsafe_path", "Conversation index paths must not be symlinks."
                )
            if not path.exists():
                continue
            info = path.stat()
            if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                raise ConversationError(
                    "index_unsafe_path",
                    "Conversation index requires a private directory and regular file.",
                )
            if os.name == "posix" and (
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ConversationError(
                    "index_unsafe_path", "Conversation index directory and file must be owner-only."
                )

    def _schema_objects(self):
        return {
            row["name"]: row["sql"]
            for row in self._db.execute(
                "SELECT name, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
            )
        }

    @staticmethod
    def _expected_objects(names):
        return set(names) | {
            "passages_data",
            "passages_idx",
            "passages_docsize",
            "passages_config",
        }

    def _is_schema(self, version, names, statements, objects=None):
        objects = self._schema_objects() if objects is None else objects
        return (
            self._db.execute("PRAGMA user_version").fetchone()[0] == version
            and set(objects) == self._expected_objects(names)
            and all(objects.get(name) == sql for name, sql in zip(names, statements))
        )

    def _migrate(self):
        # Serialize the version decision with the migration itself. A second writer
        # waits here, then observes v2 instead of racing into the same CREATE TABLE.
        with self._transaction():
            objects = self._schema_objects()
            if self._is_schema(
                _LEGACY_SCHEMA_VERSION,
                _LEGACY_OBJECTS,
                (_SCHEMA[0], _SCHEMA[1], _SCHEMA[2], _SCHEMA[3], _SCHEMA[5]),
                objects,
            ):
                self._db.execute(_SCHEMA[4])
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._check_schema()

    def _check_schema(self):
        db = self._db
        own_transaction = not db.in_transaction
        if own_transaction:
            db.execute("BEGIN")
        try:
            objects = self._schema_objects()
            current = self._is_schema(SCHEMA_VERSION, _OBJECTS, _SCHEMA, objects)
            legacy = not self.write and self._is_schema(
                _LEGACY_SCHEMA_VERSION,
                _LEGACY_OBJECTS,
                (_SCHEMA[0], _SCHEMA[1], _SCHEMA[2], _SCHEMA[3], _SCHEMA[5]),
                objects,
            )
            if not current and not legacy:
                raise ConversationError(
                    "index_incompatible",
                    "Conversation index schema is incompatible; no changes were made.",
                )
            # Ordinary opens check compatibility, not every data page. Corruption that
            # SQLite encounters on the probe or later queries is translated by _errors.
            if db.execute("SELECT count(*) FROM index_state WHERE id=1").fetchone()[0] != 1:
                raise ConversationError(
                    "index_incompatible", "Conversation index state is missing."
                )
            db.execute(
                "SELECT rowid FROM passages WHERE passages MATCH 'opentab_schema_probe' LIMIT 1"
            ).fetchall()
        finally:
            if own_transaction:
                db.execute("ROLLBACK")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        with _errors():
            self._db.close()

    @contextmanager
    def _transaction(self):
        if not self.write:
            raise ConversationError("index_read_only", "Conversation index was opened read-only.")
        with _errors():
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _touch(self):
        self._db.execute(
            "UPDATE index_state SET updated_at=? WHERE id=1",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def roots(self) -> list:
        with _errors():
            return [
                {**json.loads(row["metadata"]), "limitations": json.loads(row["limitations"])}
                for row in self._db.execute(
                    "SELECT metadata, limitations FROM roots ORDER BY session_key"
                )
            ]

    def manifests(self) -> dict:
        with _errors():
            if "source_manifests" not in self._schema_objects():
                return {}
            return {
                row["session_key"]: json.loads(row["manifest"])
                for row in self._db.execute("SELECT session_key, manifest FROM source_manifests")
            }

    def replace_root(self, metadata, sources, source_manifest=None) -> dict:
        from .conversation import CONVERSATION_READER_VERSION

        metadata = {key: metadata[key] for key in _FIELDS}
        fingerprint = hashlib.sha256(
            _json(
                [
                    CONVERSATION_READER_VERSION,
                    metadata,
                    [
                        {
                            key: source.get(key)
                            for key in (
                                "execution_id",
                                "snapshot",
                                "executions",
                                "limitations",
                                "ordering",
                            )
                        }
                        for source in sources
                    ],
                ]
            ).encode()
        ).hexdigest()
        key = metadata["session_key"]
        with self._transaction():
            previous = self._db.execute(
                "SELECT fingerprint FROM roots WHERE session_key=?", (key,)
            ).fetchone()
            if previous is not None and previous[0] == fingerprint:
                if source_manifest is not None:
                    self._db.execute(
                        "INSERT OR REPLACE INTO source_manifests VALUES (?, ?)",
                        (key, _json(source_manifest)),
                    )
                count = self._db.execute(
                    "SELECT count(*) FROM chunks WHERE session_key=?", (key,)
                ).fetchone()[0]
                return {"changed": False, "passages": count}
            self._delete_root(key)
            limitations = list(
                dict.fromkeys(item for source in sources for item in source["limitations"])
            )
            self._db.execute(
                "INSERT INTO roots VALUES (?, ?, ?, ?)",
                (key, _json(metadata), fingerprint, _json(limitations)),
            )
            if source_manifest is not None:
                self._db.execute(
                    "INSERT INTO source_manifests VALUES (?, ?)",
                    (key, _json(source_manifest)),
                )
            count = 0
            for source in sources:
                for record in source["records"]:
                    text = "\n".join(part["text"] for part in record["parts"])
                    spans, omitted = _chunk_spans(text)
                    if omitted and "overlong_tokens_omitted" not in limitations:
                        limitations.append("overlong_tokens_omitted")
                    for start, end in spans:
                        row = self._db.execute(
                            "INSERT INTO chunks (session_key, execution_id, record_id, message_id, snapshot, timestamp, source, message_date, chunk_start, chunk_end, text, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                key,
                                source["execution_id"],
                                record["id"],
                                record.get("message_id"),
                                source["snapshot"],
                                _json(record.get("timestamp")),
                                _json(record.get("source")),
                                _message_date(record.get("timestamp")),
                                start,
                                end,
                                text[start:end],
                                metadata["title"] or "",
                            ),
                        )
                        self._db.execute(
                            "INSERT INTO passages(rowid, text, title) SELECT id, text, title FROM chunks WHERE id=?",
                            (row.lastrowid,),
                        )
                        count += 1
            self._db.execute(
                "UPDATE roots SET limitations=? WHERE session_key=?", (_json(limitations), key)
            )
            self._touch()
        return {"changed": True, "passages": count}

    def _delete_root(self, key):
        # External-content FTS must see the old chunk text while removing its terms.
        self._db.execute(
            "DELETE FROM passages WHERE rowid IN (SELECT id FROM chunks WHERE session_key=?)",
            (key,),
        )
        self._db.execute("DELETE FROM chunks WHERE session_key=?", (key,))
        self._db.execute("DELETE FROM source_manifests WHERE session_key=?", (key,))
        self._db.execute("DELETE FROM roots WHERE session_key=?", (key,))

    def remove_root(self, session_key) -> None:
        with self._transaction():
            self._delete_root(session_key)
            self._touch()

    def status(self) -> dict:
        with _errors():
            row = self._db.execute(
                "SELECT (SELECT count(*) FROM roots) AS roots, (SELECT count(*) FROM chunks) AS passages, updated_at FROM index_state WHERE id=1"
            ).fetchone()
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            return {"exists": True, "schema_version": version, **dict(row)}

    def clear(self) -> dict:
        with self._transaction():
            self._db.execute("DELETE FROM passages")
            self._db.execute("DELETE FROM chunks")
            self._db.execute("DELETE FROM source_manifests")
            self._db.execute("DELETE FROM roots")
            self._db.execute("INSERT INTO passages(passages) VALUES ('rebuild')")
            self._touch()
            status = self.status()
        # Keep the inode: unlinking would split concurrent readers/writers. VACUUM is
        # compaction, not secure erasure; a concurrent reader may prevent it.
        try:
            self._db.execute("VACUUM")
        except sqlite3.Error:
            pass
        return status

    def candidates(
        self, query, allowed_keys, *, since=None, until=None, limit=1000, group_by="none"
    ) -> dict:
        terms = _validate(query, since, until, 10, 6000)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ConversationError(
                "invalid_arguments", "candidate limit must be an integer from 1 to 1000."
            )
        if group_by not in ("none", "root", "record"):
            raise ConversationError("invalid_arguments", "group_by must be none, root, or record.")
        marker = uuid.uuid4().hex
        opening, closing = marker + ":start:", marker + ":end:"
        with _errors():
            self._db.execute(
                "CREATE TEMP TABLE IF NOT EXISTS allowed_roots (session_key TEXT PRIMARY KEY)"
            )
            self._db.execute("DELETE FROM allowed_roots")
            self._db.executemany(
                "INSERT OR IGNORE INTO allowed_roots VALUES (?)", ((key,) for key in allowed_keys)
            )
            self._db.execute(
                "CREATE TEMP TABLE IF NOT EXISTS ranked (id INTEGER PRIMARY KEY, session_key TEXT, execution_id TEXT, record_id TEXT, rank REAL)"
            )
            self._db.execute("DELETE FROM ranked")
            # One read transaction covers AND and fallback: eligibility cannot change
            # between them when another connection atomically replaces a root.
            self._db.execute("BEGIN")
            try:
                rows = []
                mode = "all_terms"
                for operator in (" AND ", " OR "):
                    mode = "all_terms" if operator == " AND " else "any_term"
                    expression = operator.join(
                        '"' + term.replace('"', '""') + '"' for term in terms
                    )
                    # bm25 must execute in FTS context. Materialize only identities
                    # and scores, never all matching passage bodies, before grouping.
                    self._db.execute("DELETE FROM ranked")
                    self._db.execute(
                        "INSERT INTO ranked SELECT c.id, c.session_key, c.execution_id, c.record_id, bm25(passages, 1.0, 0.2) "
                        "FROM passages JOIN chunks c ON c.id=passages.rowid JOIN allowed_roots a ON a.session_key=c.session_key "
                        "JOIN roots r ON r.session_key=c.session_key "
                        "WHERE passages MATCH ? AND (? IS NULL OR c.message_date>=?) AND (? IS NULL OR c.message_date<=?)",
                        (expression, since, since, until, until),
                    )
                    if self._db.execute("SELECT 1 FROM ranked LIMIT 1").fetchone():
                        break
                if group_by == "none":
                    selection = "SELECT id, rank, 0 AS wave FROM ranked ORDER BY rank, id LIMIT ?"
                elif group_by == "root":
                    selection = (
                        "SELECT id, rank, row_number() OVER (PARTITION BY session_key ORDER BY rank, id) AS wave "
                        "FROM (SELECT id, session_key, rank, row_number() OVER "
                        "(PARTITION BY session_key, execution_id ORDER BY rank, id) AS position FROM ranked) "
                        "WHERE position=1 ORDER BY wave, rank, id LIMIT ?"
                    )
                else:
                    selection = (
                        "SELECT id, rank, 0 AS wave FROM (SELECT id, rank, row_number() OVER "
                        "(PARTITION BY session_key, execution_id, record_id ORDER BY rank, id) AS position FROM ranked) "
                        "WHERE position=1 ORDER BY rank, id LIMIT ?"
                    )
                rows = self._db.execute(
                    "WITH chosen AS (" + selection + ") "
                    "SELECT c.*, r.metadata, chosen.rank, highlight(passages, 0, ?, ?) AS marked, "
                    "highlight(passages, 1, ?, ?) AS marked_title "
                    "FROM chosen JOIN chunks c ON c.id=chosen.id JOIN roots r ON r.session_key=c.session_key "
                    "JOIN passages ON passages.rowid=c.id WHERE passages MATCH ? ORDER BY chosen.wave, chosen.rank, chosen.id",
                    (limit + 1, opening, closing, opening, closing, expression),
                ).fetchall()
            finally:
                self._db.execute("ROLLBACK")
        hits = []
        with _errors():
            # Admit the fair pool before rank sorting: sorting the lookahead row
            # first could evict a lower-ranked root in favor of another execution.
            for row in sorted(rows[:limit], key=lambda row: (row["rank"], row["id"])):
                text = row["text"]
                position, length = _matched_span(text, row["marked"], opening, closing)
                if position is None and "\x00" in text:
                    # NUL can corrupt SQLite's highlight rendering. Spaces are the
                    # same-length unicode61 separators; probe this chunk only, in
                    # memory. OR handles queries whose other terms match the title.
                    normalized = text.replace("\x00", " ")
                    self._db.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS temp.body_probe USING fts5(text, tokenize='unicode61 remove_diacritics 2')"
                    )
                    try:
                        self._db.execute("INSERT INTO body_probe(text) VALUES (?)", (normalized,))
                        probe = self._db.execute(
                            "SELECT highlight(body_probe, 0, ?, ?) FROM body_probe WHERE body_probe MATCH ?",
                            (
                                opening,
                                closing,
                                " OR ".join('"' + term.replace('"', '""') + '"' for term in terms),
                            ),
                        ).fetchone()
                        if probe is not None:
                            position, length = _matched_span(normalized, probe[0], opening, closing)
                    finally:
                        self._db.execute("DELETE FROM body_probe")
                title_position, _ = _matched_span(
                    row["title"], row["marked_title"], opening, closing
                )
                fields = (["text"] if position is not None else []) + (
                    ["title"] if title_position is not None else []
                )
                match_offset, match_length = None, 0
                if position is not None:
                    context = (600 - min(length, 600)) // 2
                    start = max(0, min(position - context, len(text) - 600))
                    excerpt = text[start : start + 600]
                    match_offset = position - start
                    match_length = min(length, len(excerpt) - match_offset)
                elif title_position is not None and opening not in row["marked"]:
                    excerpt = text[:600]
                else:
                    # A ranked candidate is not proof of a recoverable body span.
                    excerpt = ""
                hits.append(
                    {
                        **{
                            key: row[key]
                            for key in (
                                "session_key",
                                "execution_id",
                                "record_id",
                                "message_id",
                                "snapshot",
                                "chunk_start",
                                "chunk_end",
                                "rank",
                            )
                        },
                        "metadata": json.loads(row["metadata"]),
                        "timestamp": json.loads(row["timestamp"]),
                        "source": json.loads(row["source"]),
                        "excerpt": excerpt,
                        "match_fields": fields,
                        "match_offset": match_offset,
                        "match_length": match_length,
                    }
                )
        return {"mode": mode, "hits": hits, "limited": len(rows) > limit}


def index_status(path=None) -> dict:
    try:
        with ConversationIndex(path) as index:
            return index.status()
    except ConversationError as exc:
        if exc.code != "index_not_built":
            raise
        return {
            "exists": False,
            "schema_version": None,
            "roots": 0,
            "passages": 0,
            "updated_at": None,
        }


def clear_index(path=None) -> dict:
    # Probe without creating anything. Service owns the opt-in write gate.
    status = index_status(path)
    if not status["exists"]:
        return status
    with ConversationIndex(path, write=True) as index:
        return index.clear()
