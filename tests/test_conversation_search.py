"""Synthetic data only: this module never discovers or opens harness sources."""

import importlib
import json
import os
import sqlite3
import stat
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from unittest.mock import patch

from opentab import conversation_search as search
from opentab.conversation import ConversationError


@contextmanager
def _error(code):
    try:
        yield
    except ConversationError as exc:
        assert exc.code == code, (exc.code, code)
        assert exc.message == str(exc)
        assert "SECRET" not in str(exc)
        assert exc.__cause__ is None
        assert exc.__context__ is None or exc.__suppress_context__
    else:
        raise AssertionError(f"expected {code}")


def _metadata(key="root", title="Synthetic conversation"):
    return {
        "session_key": key,
        "native_id": "native:" + key,
        "harness": "opencode",
        "machine": "synthetic",
        "project": "/synthetic/project",
        "title": title,
        "source_id": "synthetic-source",
    }


def _record(text, index=0, timestamp="2026-09-11T12:00:00Z"):
    return {
        "id": f"opaque:{index}",
        "message_id": f"native:{index}",
        "record_id": f"not-the-anchor:{index}",
        "role": "assistant",
        "parts": [{"id": "part:0", "text": text}],
        "timestamp": timestamp,
        "source": {"file": "synthetic.jsonl", "line": index + 1},
        "billed_tokens": 0,
        "tool_text": "NEVER_INDEX_TOOL_SENTINEL",
    }


def _source(*records, execution="root", snapshot="snapshot:1", limitations=None):
    return {
        "records": list(records),
        "execution_id": execution,
        "executions": [{"id": "root"}, {"id": "child"}],
        "snapshot": snapshot,
        "limitations": limitations if limitations is not None else ["retained-history-only"],
        "ordering": "source",
    }


@contextmanager
def _index():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "private" / "index.sqlite3"
        with search.ConversationIndex(path, write=True) as index:
            yield index, path


def _legacy_index(path):
    with search.ConversationIndex(path, write=True) as index:
        index.replace_root(_metadata(), [_source(_record("retained migration needle"))])
        index._db.execute("UPDATE roots SET fingerprint='legacy-unversioned'")
        index._db.execute("DROP TABLE source_manifests")
        index._db.execute("PRAGMA user_version=1")


def test_missing_read_and_clear_do_not_create_anything():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "absent" / "private" / "index.sqlite3"
        with _error("index_not_built"):
            search.ConversationIndex(path)
        status = search.index_status(path)
        assert status == {
            "exists": False,
            "schema_version": None,
            "roots": 0,
            "passages": 0,
            "updated_at": None,
        }
        assert search.clear_index(path) == status
        assert list(Path(directory).iterdir()) == []


def test_default_path_is_dynamic_and_import_has_no_sqlite_or_path_side_effects():
    with patch.object(
        search.sqlite3, "connect", side_effect=AssertionError("unexpected SQLite")
    ), patch.object(search.paths, "cache_dir", side_effect=AssertionError("unexpected lookup")):
        importlib.reload(search)
    with tempfile.TemporaryDirectory() as directory:
        first, second = Path(directory) / "first", Path(directory) / "second"
        for cache in (first, second):
            with patch.object(search.paths, "cache_dir", return_value=str(cache)):
                assert not search.index_status()["exists"]
                with search.ConversationIndex(write=True) as index:
                    assert index.path == cache / "conversations" / "index.sqlite3"
        assert first.exists() and second.exists()


def test_private_modes_pragmas_and_no_readonly_side_effects():
    with _index() as (index, path):
        index.replace_root(_metadata(), [_source(_record("private retained answer"))])
        assert index._db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert index._db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert index._db.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert index._db.execute("PRAGMA secure_delete").fetchone()[0] == 1
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        original = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        with search.ConversationIndex(path) as reader:
            assert reader.roots()[0]["session_key"] == "root"
            assert reader.candidates("answer", ["root"])["hits"]
            assert reader.status()["passages"] == 1
            for operation in (
                lambda: reader.replace_root(_metadata(), []),
                lambda: reader.remove_root("root"),
                reader.clear,
            ):
                with _error("index_read_only"):
                    operation()
        assert path.read_bytes() == original and path.stat().st_mtime_ns == stamp
        assert [p.name for p in path.parent.iterdir()] == ["index.sqlite3"]


def test_symlinks_are_rejected_even_when_targets_are_missing():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        target = base / "target"
        target.mkdir(mode=0o700)
        linked_parent = base / "linked"
        linked_parent.symlink_to("target", target_is_directory=True)
        linked_db = target / "index.sqlite3"
        linked_db.symlink_to("missing.sqlite3")
        for path in (linked_parent / "other.sqlite3", linked_db):
            for write in (False, True):
                with _error("index_unsafe_path"):
                    search.ConversationIndex(path, write=write)
        assert not (target / "missing.sqlite3").exists()


def test_permissions_reject_insecure_existing_locations_without_chmod():
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        parent = base / "shared"
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        for write in (False, True):
            with _error("index_unsafe_path"):
                search.ConversationIndex(parent / "index.sqlite3", write=write)
        assert stat.S_IMODE(parent.stat().st_mode) == 0o755
        assert not list(parent.iterdir())
        # Ancestors outside the private index directory are not chmodded.
        path = parent / "private" / "index.sqlite3"
        with search.ConversationIndex(path, write=True):
            pass
        assert stat.S_IMODE(parent.stat().st_mode) == 0o755
        path.chmod(0o644)
        with _error("index_unsafe_path"):
            search.ConversationIndex(path, write=True)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_literal_identifiers_punctuation_and_payload_are_preserved():
    text = '  ses_abc-123 src/widget.ts C++ "alpha" OR omega <b>literal</b> [match]  '
    with _index() as (index, _):
        record = _record(text)
        index.replace_root(_metadata(), [_source(record, _record("alpha omega", 1))])
        for query in ("ses_abc-123", "src/widget.ts", "C++", '"alpha" OR omega', "<b>literal</b>"):
            result = index.candidates(query, ["root"])
            assert result["mode"] == "all_terms"
            hit = result["hits"][0]
            assert hit["record_id"] == record["id"]
            assert hit["message_id"] == record["message_id"]
            assert hit["source"] == record["source"]
            assert hit["excerpt"] == text
            assert hit["timestamp"] == record["timestamp"]
            assert hit["snapshot"] == "snapshot:1"
        assert not index.candidates("NEVER_INDEX_TOOL_SENTINEL", ["root"])["hits"]


def test_unicode61_accents_decomposed_names_and_multilingual_terms():
    text = "Jos\u00e9 Mu\u0308ller \u1ed9 Stra\u00dfe \u0391\u03b8\u03ae\u03bd\u03b1 \u041c\u043e\u0441\u043a\u0432\u0430 \u6771\u4eac \u0645\u0631\u062d\u0628\u0627"
    with _index() as (index, _):
        index.replace_root(_metadata(), [_source(_record(text))])
        for query in (
            "Jose",
            "Muller",
            "o",
            "STRA\u1e9eE",
            "\u0391\u03b8\u03ae\u03bd\u03b1",
            "\u043c\u043e\u0441\u043a\u0432\u0430",
            "\u6771\u4eac",
            "\u0645\u0631\u062d\u0628\u0627",
        ):
            hits = index.candidates(query, ["root"])["hits"]
            assert hits, query
            assert hits[0]["excerpt"] == text


def test_nonascii_chunk_boundaries_agree_with_full_sqlite_fts_not_python_unicode():
    with _index() as (index, _), sqlite3.connect(":memory:") as reference:
        reference.execute(
            "CREATE VIRTUAL TABLE original USING fts5(text, tokenize='unicode61 remove_diacritics 2')"
        )
        for char in ("\U0001fae0", "\U0001f9d1", "\u0378"):
            word = "alpha" + char + "betical"
            for offset in (1795, 1995, 3795):
                text = " " * offset + word
                reference.execute("DELETE FROM original")
                reference.execute("INSERT INTO original VALUES (?)", (text,))
                index.replace_root(
                    _metadata(), [_source(_record(text), snapshot=f"{char}:{offset}")]
                )
                for query in ("alpha", "betical", word):
                    expected = bool(
                        reference.execute(
                            "SELECT rowid FROM original WHERE original MATCH ?",
                            ('"' + query + '"',),
                        ).fetchall()
                    )
                    assert bool(index.candidates(query, ["root"])["hits"]) == expected, (
                        char,
                        offset,
                        query,
                    )
                rows = index._db.execute("SELECT text FROM chunks").fetchall()
                assert any(word in row[0] for row in rows)


def test_long_nonascii_separated_conservative_span_is_omitted_with_limitation():
    text = "before " + "alpha\u2014" * 400 + " after"
    with _index() as (index, _):
        source = _source(_record(text))
        index.replace_root(_metadata(), [source])
        assert not index.candidates("alpha", ["root"])["hits"]
        assert index.candidates("before after", ["root"])["mode"] == "any_term"
        assert "overlong_tokens_omitted" in index.roots()[0]["limitations"]
        assert source["records"][0]["parts"][0]["text"] == text


def test_and_first_and_or_fallback_only_after_eligible_hits_are_empty():
    with _index() as (index, _):
        index.replace_root(_metadata("hidden"), [_source(_record("alpha beta"))])
        index.replace_root(_metadata("visible"), [_source(_record("alpha"), _record("beta", 1))])
        assert index.candidates("alpha beta", ["hidden", "visible"])["mode"] == "all_terms"
        result = index.candidates("alpha beta", ["visible"])
        assert result["mode"] == "any_term" and len(result["hits"]) == 2
        assert {hit["session_key"] for hit in result["hits"]} == {"visible"}
        result = index.candidates("alpha beta", [])
        assert result == {"mode": "any_term", "hits": [], "limited": False}
        index.replace_root(
            _metadata("old"), [_source(_record("alpha beta", timestamp="2020-01-01T00:00:00Z"))]
        )
        assert (
            index.candidates("alpha beta", ["old", "visible"], since="2026-01-01")["mode"]
            == "any_term"
        )


def test_whitelist_exceeds_bind_limit_and_treats_keys_as_data():
    with _index() as (index, _):
        key = "root'); DROP TABLE roots; --"
        index.replace_root(_metadata(key), [_source(_record("needle"))])
        keys = [f"absent:{i}" for i in range(4000)] + [key, key]
        if hasattr(index._db, "setlimit"):
            index._db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
        hits = index.candidates("needle", keys)["hits"]
        assert len(hits) == 1 and hits[0]["session_key"] == key
        assert index.candidates("needle", keys, group_by="root")["hits"] == hits
        assert index.roots()[0]["session_key"] == key
        assert not index.candidates("needle", ["other"])["hits"]


def test_date_filters_use_utc_message_days_not_root_dates():
    stamps = [
        "2026-09-12T00:30:00+02:00",
        "2026-09-10T23:30:00-02:00",
        int(datetime(2026, 9, 11, 12, tzinfo=timezone.utc).timestamp() * 1000),
        "2026-09-11T23:00:00-02:00",
        None,
        "unknown",
        "2026-09-11T12:00:00",
        True,
        1e100,
    ]
    with _index() as (index, _):
        index.replace_root(
            _metadata(), [_source(*[_record("needle", i, value) for i, value in enumerate(stamps)])]
        )
        hits = index.candidates("needle", ["root"], since="2026-09-11", until="2026-09-11")["hits"]
        assert {hit["record_id"] for hit in hits} == {"opaque:0", "opaque:1", "opaque:2"}
        assert {json.dumps(hit["timestamp"]) for hit in hits} == {
            json.dumps(value) for value in stamps[:3]
        }
        assert len(index.candidates("needle", ["root"])["hits"]) == len(stamps)
        assert len(index.candidates("needle", ["root"], since="2026-09-12")["hits"]) == 1


def test_bm25_prioritizes_body_over_title_and_does_not_sort_by_date():
    with _index() as (index, _):
        index.replace_root(
            _metadata("body", "neutral"),
            [_source(_record("needle filler", timestamp="2020-01-01T00:00:00Z"))],
        )
        index.replace_root(
            _metadata("title", "needle"),
            [_source(_record("neutral filler", timestamp="2026-09-11T00:00:00Z"))],
        )
        hits = index.candidates("needle", ["body", "title"])["hits"]
        assert [hit["session_key"] for hit in hits] == ["body", "title"]
        assert hits[0]["rank"] < hits[1]["rank"]


def test_title_only_hit_is_a_discovery_candidate_not_a_body_match():
    text = "Unrelated retained message. " * 30
    with _index() as (index, _):
        index.replace_root(_metadata(title="ses_abc-123"), [_source(_record(text))])
        result = index.candidates("ses_abc-123", ["root"])
        assert result["mode"] == "all_terms" and len(result["hits"]) == 1
        hit = result["hits"][0]
        assert hit["excerpt"] == text[:600]
        assert "ses_abc-123" not in hit["excerpt"]
        assert hit["record_id"] == "opaque:0"
        assert hit["chunk_start"] == 0 and hit["chunk_end"] == len(text)
        # Weighting is not confidence or a match percentage; do not pin a score.
        assert isinstance(hit["rank"], float)
        assert hit["match_fields"] == ["title"]
        assert hit["match_offset"] is None and hit["match_length"] == 0


def test_nul_highlight_recovers_matched_literal_in_original_chunk():
    text = "a " * 400 + "\x00" + "b " * 400 + " needle tail"
    assert text.index("\x00") == 800 and text.index("needle") == 1602
    with _index() as (index, _):
        index.replace_root(_metadata(), [_source(_record(text))])
        hit = index.candidates("needle", ["root"])["hits"][0]
        assert "needle" in hit["excerpt"] and hit["excerpt"] in text
        offset, length = hit["match_offset"], hit["match_length"]
        assert hit["excerpt"][offset : offset + length] == "needle"
        assert hit["match_fields"] == ["text"]
        assert index._db.execute("SELECT text FROM chunks").fetchone()[0] == text
        index._db.execute("INSERT INTO passages(passages, rank) VALUES ('integrity-check', 1)")
        index.remove_root("root")
        assert not index.candidates("needle", ["root"])["hits"]


def test_highlight_alignment_selects_tokens_not_earlier_literal_substrings():
    with _index() as (index, _):
        for number, (prefix, word, query) in enumerate(
            (
                ("alphabetical", "alpha", "alpha"),
                ("caf\u00e9ine", "caf\u00e9", "cafe"),
                ("cafe\u0301ine", "cafe\u0301", "cafe"),
            )
        ):
            for padding in (" ", " filler" * 100 + " "):
                text = prefix + padding + word + " after" * 50
                expected = len(prefix) + len(padding)
                index.replace_root(
                    _metadata(), [_source(_record(text), snapshot=f"{number}:{len(padding)}")]
                )
                hit = index.candidates(query, ["root"])["hits"][0]
                excerpt, offset, length = hit["excerpt"], hit["match_offset"], hit["match_length"]
                assert excerpt[offset : offset + length] == word
                assert text.index(excerpt) + offset == expected
                # Consumers can recenter smaller budgets on the actual token,
                # without falling back to a substring in the earlier longer word.
                for budget in (length, 20, 60):
                    start = max(0, min(offset - (budget - length) // 2, len(excerpt) - budget))
                    small = excerpt[start : start + budget]
                    assert small[offset - start : offset - start + length] == word
                    assert text.index(excerpt) + start + offset - start == expected


def test_matched_span_requires_complete_well_formed_highlight_alignment():
    text = "alphabetical alpha tail alpha"
    marked = "alphabetical <s>alpha</s> tail <s>alpha</s>"
    assert search._matched_span(text, marked, "<s>", "</s>") == (13, 5)
    for malformed in (
        "alphabetical <s>alpha</s> changed suffix",
        "alphabetical <s>alpha tail alpha",
        "alphabetical <s><s>alpha</s> tail alpha",
        "alphabetical <s>alpha</s> tail alpha</s>",
        "alphabetical <s></s>alpha tail alpha",
    ):
        assert search._matched_span(text, malformed, "<s>", "</s>") == (None, 0)


def test_nul_probe_uses_or_terms_and_preserves_original_excerpt_and_small_offsets():
    text = "alphabetical " + " " * 790 + "\x00" + " filler" * 100 + " alpha \x00 tail"
    expected = text.index(" alpha ") + 1
    with _index() as (index, path):
        index.replace_root(_metadata(title="titleonly"), [_source(_record(text))])
        before = path.read_bytes()
        with search.ConversationIndex(path) as reader:
            statements = []
            reader._db.set_trace_callback(statements.append)
            result = reader.candidates("alpha titleonly", ["root"])
            hit = result["hits"][0]
            assert result["mode"] == "all_terms" and hit["match_fields"] == ["text", "title"]
            excerpt, offset, length = hit["excerpt"], hit["match_offset"], hit["match_length"]
            assert "\x00" in excerpt and excerpt in text
            assert text.index(excerpt) + offset == expected
            assert excerpt[offset : offset + length] == "alpha"
            for budget in (5, 12, 50):
                start = max(0, min(offset - (budget - length) // 2, len(excerpt) - budget))
                assert (
                    excerpt[start : start + budget][offset - start : offset - start + length]
                    == "alpha"
                )
            assert any("body_probe MATCH" in sql and " OR " in sql for sql in statements)
            assert reader._db.execute("SELECT count(*) FROM body_probe").fetchone()[0] == 0
            assert reader._db.execute("SELECT text FROM chunks").fetchone()[0] == text
        assert path.read_bytes() == before


def test_nul_probe_fails_closed_when_normalized_alignment_cannot_be_proven():
    with _index() as (index, _):
        index.replace_root(_metadata(), [_source(_record("alphabetical \x00 alpha"))])
        with patch.object(search, "_matched_span", return_value=(None, 0)):
            hit = index.candidates("alpha", ["root"])["hits"][0]
        assert hit["excerpt"] == "" and hit["match_fields"] == []
        assert hit["match_offset"] is None and hit["match_length"] == 0
        assert index._db.execute("SELECT count(*) FROM body_probe").fetchone()[0] == 0


def test_match_offsets_bound_the_visible_literal_and_missing_spans_fail_closed():
    with _index() as (index, _):
        for number, text in enumerate(
            ("\x00 needle \x00 tail", "prefix " + "q" * 1000 + " suffix")
        ):
            query = "needle" if number == 0 else "q" * 1000
            index.replace_root(_metadata(), [_source(_record(text), snapshot=str(number))])
            hit = index.candidates(query, ["root"])["hits"][0]
            offset, length = hit["match_offset"], hit["match_length"]
            assert 0 <= offset < len(hit["excerpt"]) <= 600
            assert 0 < length <= len(hit["excerpt"]) - offset
            assert query.startswith(hit["excerpt"][offset : offset + length])
            assert hit["excerpt"] in text
        assert search._matched_span("original", "<start>absent<end>", "<start>", "<end>") == (
            None,
            0,
        )
        with patch.object(search, "_matched_span", return_value=(None, 0)):
            hit = index.candidates("q" * 1000, ["root"])["hits"][0]
        assert hit["excerpt"] == "" and hit["match_fields"] == []
        assert hit["match_offset"] is None and hit["match_length"] == 0


def test_root_grouping_precedes_cap_and_does_not_materialize_all_matching_text():
    with _index() as (index, _):
        index.replace_root(_metadata("A"), [_source(*[_record("needle", i) for i in range(1001)])])
        index.replace_root(_metadata("B"), [_source(_record("needle filler"))])
        ungrouped = index.candidates("needle", ["A", "B"])
        assert ungrouped["limited"] and len(ungrouped["hits"]) == 1000
        assert {hit["session_key"] for hit in ungrouped["hits"]} == {"A"}
        grouped = index.candidates("needle", ["A", "B"], group_by="root")
        assert not grouped["limited"]
        assert [hit["session_key"] for hit in grouped["hits"]] == ["A", "B"]
        assert grouped["hits"][0]["record_id"] == "opaque:0"
        assert index.candidates("needle", ["A", "B"], group_by="root", limit=1)["limited"]
        assert [row[1] for row in index._db.execute("PRAGMA temp.table_info(ranked)")] == [
            "id",
            "session_key",
            "execution_id",
            "record_id",
            "rank",
        ]
        assert index._db.execute("SELECT count(*) FROM ranked").fetchone()[0] == 0


def test_root_pool_keeps_rank_ordered_execution_alternatives_for_live_verification():
    with _index() as (index, _):
        index.replace_root(
            _metadata("A"),
            [
                _source(_record("needle"), snapshot="stale-root"),
                _source(_record("needle filler"), execution="child", snapshot="fresh-child"),
            ],
        )
        index.replace_root(_metadata("B"), [_source(_record("needle filler filler"))])
        result = index.candidates("needle", ["A", "B"], group_by="root", limit=3)
        hits = result["hits"]
        assert not result["limited"]
        assert [(hit["session_key"], hit["execution_id"]) for hit in hits] == [
            ("A", "root"),
            ("A", "child"),
            ("B", "root"),
        ]
        assert [hit["rank"] for hit in hits] == sorted(hit["rank"] for hit in hits)
        # The parent may reject the first execution's stale snapshot without
        # losing this root's independently fresh child from the candidate pool.
        snapshots = {"root": "new-root", "child": "fresh-child"}
        verified = next(
            hit
            for hit in hits
            if hit["session_key"] == "A" and hit["snapshot"] == snapshots[hit["execution_id"]]
        )
        assert verified["execution_id"] == "child"
        assert verified["metadata"] == _metadata("A")


def test_root_fair_pool_truncates_before_rank_sorting_and_preserves_lower_ranked_root():
    with _index() as (index, _):
        index.replace_root(
            _metadata("A"),
            [
                _source(_record("needle"), execution=f"exec:{i}", snapshot=f"snapshot:{i}")
                for i in range(1001)
            ],
        )
        index.replace_root(_metadata("B"), [_source(_record("needle filler"))])
        result = index.candidates("needle", ["A", "B"], group_by="root")
        hits = result["hits"]
        assert result["limited"] and len(hits) == 1000
        assert sum(hit["session_key"] == "A" for hit in hits) == 999
        assert hits[-1]["session_key"] == "B"
        assert {hit["execution_id"] for hit in hits if hit["session_key"] == "A"} == {
            f"exec:{i}" for i in range(999)
        }
        assert [hit["rank"] for hit in hits] == sorted(hit["rank"] for hit in hits)
        short = index.candidates("needle", ["A", "B"], group_by="root", limit=2)
        assert short["limited"] and [hit["session_key"] for hit in short["hits"]] == ["A", "B"]


def test_record_grouping_uses_root_execution_and_opaque_record_before_cap():
    with _index() as (index, _):
        index.replace_root(
            _metadata("A"),
            [
                _source(_record("needle " * 700), _record("needle filler", 1)),
                _source(_record("needle filler filler"), execution="child", snapshot="child"),
            ],
        )
        index.replace_root(_metadata("B"), [_source(_record("needle filler filler filler"))])
        result = index.candidates("needle", ["A", "B"], group_by="record")
        identities = {
            (hit["session_key"], hit["execution_id"], hit["record_id"]) for hit in result["hits"]
        }
        assert identities == {
            ("A", "root", "opaque:0"),
            ("A", "root", "opaque:1"),
            ("A", "child", "opaque:0"),
            ("B", "root", "opaque:0"),
        }
        assert len(result["hits"]) == 4 and not result["limited"]
        for hit in result["hits"]:
            alternatives = [
                row
                for row in index.candidates("needle", ["A", "B"])["hits"]
                if (row["session_key"], row["execution_id"], row["record_id"])
                == (hit["session_key"], hit["execution_id"], hit["record_id"])
            ]
            assert hit == alternatives[0]
        limited = index.candidates("needle", ["A", "B"], group_by="record", limit=2)
        assert limited["limited"] and len(limited["hits"]) == 2
        assert len({(hit["execution_id"], hit["record_id"]) for hit in limited["hits"]}) == 2


def test_grouping_respects_eligible_fallback_and_rejects_untrusted_modes():
    with _index() as (index, _):
        index.replace_root(_metadata("hidden"), [_source(_record("alpha beta"))])
        index.replace_root(
            _metadata("visible"),
            [
                _source(
                    _record("alpha"),
                    _record("beta", 1),
                    _record("alpha beta", 2, "2020-01-01T00:00:00Z"),
                )
            ],
        )
        for group_by in ("none", "root", "record"):
            result = index.candidates(
                "alpha beta", ["visible"], since="2026-01-01", group_by=group_by
            )
            assert result["mode"] == "any_term"
            assert len(result["hits"]) == (1 if group_by == "root" else 2)
            assert index.candidates("alpha beta", [], group_by=group_by)["hits"] == []
        for value in (None, True, 1, [], "ROOT", "root; DROP TABLE roots; --"):
            with _error("invalid_arguments"):
                index.candidates("alpha", ["visible"], group_by=value)


def test_candidate_metadata_is_current_transaction_data_not_prior_roots_hint():
    with _index() as (index, path):
        old = _metadata()
        index.replace_root(old, [_source(_record("needle old"))])
        hint = index.roots()[0]
        updated = {
            **old,
            "source_id": "replacement-domain",
            "title": "new title",
            "project": "/synthetic/new",
        }
        with search.ConversationIndex(path, write=True) as writer:
            assert writer.replace_root(updated, [_source(_record("needle new"), snapshot="new")])[
                "changed"
            ]
        hit = index.candidates("needle", [hint["session_key"]], group_by="root")["hits"][0]
        assert hit["metadata"] == updated and hit["metadata"] != old
        assert hit["snapshot"] == "new" and hit["excerpt"] == "needle new"
        hit["metadata"]["source_id"] = "caller mutation"
        assert index.roots()[0]["source_id"] == "replacement-domain"
        source_only = {**updated, "source_id": "third-domain"}
        assert index.replace_root(source_only, [_source(_record("needle new"), snapshot="new")])[
            "changed"
        ]
        assert index.candidates("needle", ["root"])["hits"][0]["metadata"] == source_only


def test_grouped_candidates_keep_metadata_and_chunks_in_one_read_transaction():
    with _index() as (index, path):
        old = _metadata()
        updated = {**old, "source_id": "new-domain", "title": "new title"}
        index.replace_root(old, [_source(_record("needle old"))])
        ready, begin, changed = Event(), Event(), Event()
        waited = []

        def writer_task():
            with search.ConversationIndex(path, write=True) as writer:
                ready.set()
                assert begin.wait(10)
                touch = writer._touch

                def touched():
                    touch()
                    changed.set()

                with patch.object(writer, "_touch", touched):
                    return writer.replace_root(
                        updated, [_source(_record("needle new"), snapshot="new")]
                    )

        def after_ranking(sql):
            if sql == "SELECT 1 FROM ranked LIMIT 1":
                begin.set()
                waited.append(changed.wait(10))

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer_task)
            assert ready.wait(10)
            index._db.set_trace_callback(after_ranking)
            try:
                hit = index.candidates("needle", ["root"], group_by="root")["hits"][0]
            finally:
                index._db.set_trace_callback(None)
                begin.set()
            assert future.result(timeout=10)["changed"]
        assert waited == [True]
        assert hit["metadata"] == old and hit["snapshot"] == "snapshot:1"
        assert hit["excerpt"] == "needle old"
        latest = index.candidates("needle", ["root"], group_by="root")["hits"][0]
        assert latest["metadata"] == updated and latest["snapshot"] == "new"
        assert latest["excerpt"] == "needle new"


def test_literal_identifier_crossing_chunk_end_is_found_in_overlap():
    identifier = "ses_ABC-123/widget.ts"
    text = " " * 1995 + identifier + " tail"
    with _index() as (index, _):
        index.replace_root(_metadata(), [_source(_record(text))])
        result = index.candidates(identifier, ["root"])
        assert result["mode"] == "all_terms" and len(result["hits"]) == 1
        hit = result["hits"][0]
        assert 0 < hit["chunk_start"] <= 1995
        assert identifier in hit["excerpt"]
        assert hit["excerpt"] in text


def test_chunk_edges_never_match_prefix_or_suffix_fragments_of_source_words():
    with _index() as (index, _):
        for word, fragments in (
            ("alphabetical", ("alpha", "betical")),
            ("alpha\u0301betical", ("alpha", "betical")),
            ("alpha\ue000betical", ("alpha", "betical", "\ue000betical")),
            ("alpha123betical", ("alpha", "123betical")),
        ):
            for offset in (0, 1795, 1995):
                text = " " * offset + word + " padding " * 100
                source = _source(_record(text), snapshot=f"{word}:{offset}")
                index.replace_root(_metadata(), [source])
                # Includes the former repro: alphabetical at 1995 matched alpha.
                for fragment in fragments:
                    assert not index.candidates(fragment, ["root"])["hits"], (
                        word,
                        offset,
                        fragment,
                    )
                hits = index.candidates(word, ["root"])["hits"]
                assert hits, (word, offset)
                for hit in hits:
                    assert hit["excerpt"] in text
                    assert word in text[hit["chunk_start"] : hit["chunk_end"]]
                index._db.execute(
                    "INSERT INTO passages(passages, rank) VALUES ('integrity-check', 1)"
                )
        index.remove_root("root")
        assert not index.candidates("alpha123betical", ["root"])["hits"]


def test_chunk_geometry_preserves_token_boundaries_coverage_and_bounded_overlap():
    def token_char(char):
        return not char.isascii() or char.isalnum()

    texts = ["", "alphabetical", "\u0301leading", "punctuation-/_: " * 300]
    for word in (
        "alphabetical",
        "e\u0301clair",
        "\ue000word\ue001",
        "x\u0903y",
        "z\u20ddt",
        "123456789",
        "\u6771\u4eac",
        "q" * 2000,
    ):
        texts.extend(" " * offset + word + "!tail;" * 500 for offset in (0, 1795, 1995))
    for text in texts:
        spans, omitted = search._chunk_spans(text)
        assert not omitted
        covered = set()
        for i, (start, end) in enumerate(spans):
            assert 0 <= start < end <= len(text) and end - start <= 2000
            assert start == 0 or not (token_char(text[start - 1]) and token_char(text[start]))
            assert end == len(text) or not (token_char(text[end - 1]) and token_char(text[end]))
            if i:
                assert start > spans[i - 1][0] and end > spans[i - 1][1]
                assert 0 <= spans[i - 1][1] - start <= 200
            covered.update(range(start, end))
        assert len(covered) == len(text)
    # Whitespace permits the full intended overlap; long tokens can reduce it.
    assert search._chunk_spans(" " * 4000) == ([(0, 2000), (1800, 3800), (3600, 4000)], False)


def test_overlong_tokens_are_omitted_whole_without_losing_surrounding_source_offsets():
    with _index() as (index, _):
        for number, token in enumerate(
            ("q" * 2001, "q" * 100000, "e\u0301" * 1500, "\ue000" * 2001)
        ):
            for prefix in ("", "before " + " " * 1990):
                suffix = " after"
                text = prefix + token + suffix
                source = _source(_record(text), snapshot=f"{number}:{len(prefix)}")
                original = deepcopy(source)
                result = index.replace_root(_metadata(), [source])
                rows = list(
                    index._db.execute(
                        "SELECT chunk_start, chunk_end, text FROM chunks ORDER BY chunk_start"
                    )
                )
                assert 1 <= len(rows) <= 3 and result["passages"] == len(rows)
                token_start, token_end = len(prefix), len(prefix) + len(token)
                covered = set()
                for row in rows:
                    start, end = row["chunk_start"], row["chunk_end"]
                    assert row["text"] == text[start:end] and 0 < end - start <= 2000
                    assert end <= token_start or start >= token_end
                    covered.update(range(start, end))
                assert covered == set(range(token_start)) | set(range(token_end, len(text)))
                assert index.candidates("after", ["root"])["hits"]
                assert not index.candidates(token[:999], ["root"])["hits"]
                assert index.roots()[0]["limitations"] == [
                    "retained-history-only",
                    "overlong_tokens_omitted",
                ]
                assert source == original
                index._db.execute(
                    "INSERT INTO passages(passages, rank) VALUES ('integrity-check', 1)"
                )
        index.remove_root("root")
        assert not index.candidates("after", ["root"])["hits"]


def test_token_size_threshold_empty_roots_and_atomic_omission_limitations():
    with _index() as (index, path):
        metadata = _metadata()
        exact = _source(_record("q" * 2000), snapshot="exact")
        assert index.replace_root(metadata, [exact]) == {"changed": True, "passages": 1}
        assert "overlong_tokens_omitted" not in index.roots()[0]["limitations"]
        assert not index.candidates("q" * 1000, ["root"])["hits"]
        overlong = _source(
            _record("q" * 2001),
            execution="child",
            snapshot="overlong",
            limitations=["child-limitation"],
        )
        before = index.status()
        with patch.object(
            index, "_touch", side_effect=sqlite3.OperationalError("SECRET injected failure")
        ):
            with _error("index_unavailable"):
                index.replace_root(metadata, [exact, overlong])
        assert index.status() == before
        assert index.roots()[0]["limitations"] == ["retained-history-only"]
        assert index.replace_root(metadata, [exact, overlong])["passages"] == 1
        limitations = ["retained-history-only", "child-limitation", "overlong_tokens_omitted"]
        assert index.roots()[0]["limitations"] == limitations
        with search.ConversationIndex(path) as reader:
            assert reader.roots()[0]["limitations"] == limitations
        changes = index._db.total_changes
        assert index.replace_root(metadata, [exact, overlong]) == {"changed": False, "passages": 1}
        assert index._db.total_changes == changes
        assert index.replace_root(metadata, [overlong]) == {"changed": True, "passages": 0}
        assert index.roots()[0]["limitations"] == ["child-limitation", "overlong_tokens_omitted"]
        assert index.replace_root(metadata, [exact])["passages"] == 1
        assert index.roots()[0]["limitations"] == ["retained-history-only"]
        index.clear()
        index._db.execute("INSERT INTO passages(passages, rank) VALUES ('integrity-check', 1)")


def test_and_is_chunk_scoped_and_does_not_claim_whole_message_matching():
    text = "alpha " + "filler " * 500 + " omega"
    with _index() as (index, _):
        index.replace_root(_metadata(), [_source(_record(text))])
        result = index.candidates("alpha omega", ["root"])
        assert result["mode"] == "any_term" and len(result["hits"]) == 2
        assert {hit["record_id"] for hit in result["hits"]} == {"opaque:0"}
        assert all(
            not ("alpha" in hit["excerpt"] and "omega" in hit["excerpt"]) for hit in result["hits"]
        )


def test_chunks_join_parts_overlap_and_center_late_answer_without_losing_payload():
    first = "filler " * 1300
    second = '  LATEANSWER "quoted" [brackets] <b>literal</b>  ' + "after " * 200
    record = _record(first)
    record["parts"].append({"id": "part:1", "text": second})
    text = first + "\n" + second
    with _index() as (index, _):
        count = index.replace_root(_metadata(), [_source(record)])["passages"]
        rows = list(
            index._db.execute(
                "SELECT text, chunk_start, chunk_end FROM chunks ORDER BY chunk_start"
            )
        )
        assert count == len(rows) > 4
        for i, row in enumerate(rows):
            assert row["text"] == text[row["chunk_start"] : row["chunk_end"]]
            assert len(row["text"]) <= 2000
            if i:
                assert 0 <= rows[i - 1]["chunk_end"] - row["chunk_start"] <= 200
        assert rows[-1]["chunk_end"] == len(text)
        hits = index.candidates("LATEANSWER", ["root"])["hits"]
        assert hits
        for hit in hits:
            assert len(hit["excerpt"]) <= 600
            assert 'LATEANSWER "quoted" [brackets] <b>literal</b>' in hit["excerpt"]
            assert hit["excerpt"] in text
            assert hit["chunk_start"] > 6000


def test_candidate_limit_is_explicit_and_does_not_trigger_or_fallback():
    with _index() as (index, _):
        index.replace_root(
            _metadata(),
            [_source(*[_record("alpha beta", i) for i in range(5)], _record("alpha", 6))],
        )
        result = index.candidates("alpha beta", ["root"], limit=2)
        assert result["limited"] and len(result["hits"]) == 2 and result["mode"] == "all_terms"
        assert not index.candidates("alpha beta", ["root"], limit=5)["limited"]
        for value in (True, 0, -1, 1001, 1.5, "2"):
            with _error("invalid_arguments"):
                index.candidates("alpha", ["root"], limit=value)


def test_incremental_writes_metadata_limitations_execution_snapshots_and_reopen():
    with _index() as (index, path):
        metadata = _metadata()
        sources = [
            _source(_record("first root")),
            _source(
                _record("child answer"),
                execution="child",
                snapshot="child:snapshot",
                limitations=["child-only", "retained-history-only"],
            ),
        ]
        original = deepcopy((metadata, sources))
        assert index.replace_root(metadata, sources) == {"changed": True, "passages": 2}
        stamp = index.status()["updated_at"]
        writes = index._db.total_changes
        assert index.replace_root(metadata, sources) == {"changed": False, "passages": 2}
        assert index._db.total_changes == writes
        assert index.status()["updated_at"] == stamp
        assert (metadata, sources) == original
        assert index.roots() == [
            {**metadata, "limitations": ["retained-history-only", "child-only"]}
        ]
        hit = index.candidates("child", ["root"])["hits"][0]
        assert hit["execution_id"] == "child" and hit["snapshot"] == "child:snapshot"
        metadata["title"] = "changed title"
        assert index.replace_root(metadata, sources)["changed"]
        sources[1] = _source(_record("replacement"), execution="child", snapshot="child:new")
        assert index.replace_root(metadata, sources)["changed"]
        assert not index.candidates("answer", ["root"])["hits"]
        with search.ConversationIndex(path) as reader:
            assert reader.status() == index.status()
            assert reader.candidates("replacement", ["root"])["hits"][0]["snapshot"] == "child:new"


def test_snapshot_contract_only_skips_writes_not_fresh_source_reading():
    with _index() as (index, _):
        source = _source(_record("original"))
        index.replace_root(_metadata(), [source])
        source["records"][0]["parts"][0]["text"] = "changed"
        assert not index.replace_root(_metadata(), [source])["changed"]
        assert index.candidates("original", ["root"])["hits"]
        source["snapshot"] = "fresh"
        assert index.replace_root(_metadata(), [source])["changed"]
        assert not index.candidates("original", ["root"])["hits"]


def test_root_replacement_rolls_back_sqlite_and_input_failures():
    with _index() as (index, _):
        index.replace_root(_metadata(), [_source(_record("oldtext"))])
        status, roots = index.status(), index.roots()
        with patch.object(
            index, "_touch", side_effect=sqlite3.OperationalError("SECRET SQL excerpt")
        ):
            with _error("index_unavailable"):
                index.replace_root(
                    _metadata("root", "new title"), [_source(_record("newtext"), snapshot="new")]
                )
        broken = _source(_record(42), snapshot="broken")
        try:
            index.replace_root(_metadata(), [broken])
        except TypeError:
            pass
        else:
            raise AssertionError("expected malformed input rejection")
        assert index.status() == status and index.roots() == roots
        assert index.candidates("oldtext", ["root"])["hits"]
        assert not index.candidates("newtext", ["root"])["hits"]


def test_remove_clear_rebuild_keep_inode_and_existing_reader_usable():
    with _index() as (index, path):
        index.replace_root(_metadata("one"), [_source(_record("alpha"))])
        index.replace_root(_metadata("two"), [_source(_record("beta"))])
        index.remove_root("one")
        index.remove_root("absent")
        assert index.status()["roots"] == 1
        assert not index.candidates("alpha", ["one", "two"])["hits"]
        inode = path.stat().st_ino
        with search.ConversationIndex(path) as reader:
            status = search.clear_index(path)
            assert status["exists"] and status["roots"] == status["passages"] == 0
            assert status["updated_at"]
            assert reader.roots() == []
            assert not reader.candidates("beta", ["two"])["hits"]
            assert path.stat().st_ino == inode
            index.replace_root(_metadata("three"), [_source(_record("gamma"))])
            assert reader.candidates("gamma", ["three"])["hits"]
        index._db.execute("INSERT INTO passages(passages, rank) VALUES ('integrity-check', 1)")


def test_clear_vacuum_failure_is_best_effort_after_committed_deletion():
    with _index() as (index, path):
        index.replace_root(_metadata(), [_source(_record("answer"))])

        # Inject only the optional compaction failure; clearing must still commit.
        class NoVacuum:
            def execute(self, sql, *args):
                if sql == "VACUUM":
                    raise sqlite3.OperationalError("SECRET compaction failure")
                return original.execute(sql, *args)

        original = index._db
        with patch.object(index, "_db", NoVacuum()):
            assert index.clear()["passages"] == 0
        assert path.exists() and not index.roots()


def test_corrupt_empty_unrelated_version_and_missing_fts_are_not_overwritten():
    with tempfile.TemporaryDirectory() as directory:
        for case in (
            "corrupt",
            "empty",
            "unrelated",
            "version",
            "missing_fts",
            "trigger",
            "sqlite_prefix",
        ):
            path = Path(directory) / f"{case}.sqlite3"
            if case in ("version", "missing_fts", "trigger", "sqlite_prefix"):
                with search.ConversationIndex(path, write=True) as index:
                    index.replace_root(_metadata(), [_source(_record("SECRET retained text"))])
                    sql = {
                        "version": "PRAGMA user_version=999",
                        "missing_fts": "DROP TABLE passages",
                        "trigger": "CREATE TRIGGER unwanted AFTER DELETE ON roots BEGIN DELETE FROM chunks; END",
                        "sqlite_prefix": "CREATE TABLE sqliteXunrelated (value TEXT)",
                    }[case]
                    index._db.execute(sql)
            elif case == "unrelated":
                with sqlite3.connect(path) as db:
                    db.execute("CREATE TABLE SECRET_unrelated (value TEXT)")
                    db.execute("INSERT INTO SECRET_unrelated VALUES ('SECRET payload')")
            else:
                path.write_bytes(b"SECRET not SQLite" if case == "corrupt" else b"")
            path.chmod(0o600)
            original = path.read_bytes()
            for write in (False, True):
                with _error("index_unavailable" if case == "corrupt" else "index_incompatible"):
                    search.ConversationIndex(path, write=write)
                assert path.read_bytes() == original


def test_shipped_v1_index_is_readable_and_migrates_additively_on_write():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "index.sqlite3"
        _legacy_index(path)
        with search.ConversationIndex(path) as legacy:
            assert legacy.candidates("migration", ["root"])["hits"]
            assert legacy.manifests() == {}
        with search.ConversationIndex(path, write=True) as migrated:
            assert migrated.candidates("migration", ["root"])["hits"]
            assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 2
            assert migrated.manifests() == {}
            source = [_source(_record("retained migration needle"))]
            manifest = [1, [["source", 1]]]
            assert migrated.replace_root(
                _metadata(),
                source,
                manifest,
            )["changed"]
            assert not migrated.replace_root(_metadata(), source, manifest)["changed"]
            assert migrated.manifests() == {"root": manifest}


def test_two_v1_migration_writers_serialize_the_version_decision_and_preserve_data():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "index.sqlite3"
        _legacy_index(path)
        inspected_under_lock, release = Event(), Event()
        original = search.ConversationIndex._schema_objects

        def pause_first_locked_inspection(index):
            objects = original(index)
            if index.write and index._db.in_transaction and not inspected_under_lock.is_set():
                inspected_under_lock.set()
                assert release.wait(10)
            return objects

        def migrate():
            with search.ConversationIndex(path, write=True) as index:
                return bool(index.candidates("migration", ["root"])["hits"])

        with patch.object(
            search.ConversationIndex,
            "_schema_objects",
            autospec=True,
            side_effect=pause_first_locked_inspection,
        ), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(migrate)
            assert inspected_under_lock.wait(10)
            second = pool.submit(migrate)
            release.set()
            assert first.result(timeout=10) and second.result(timeout=10)
        with search.ConversationIndex(path) as index:
            assert index.status()["schema_version"] == 2
            assert index.candidates("migration", ["root"])["hits"]


def test_v1_read_schema_snapshot_stays_coherent_while_writer_migrates():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "index.sqlite3"
        _legacy_index(path)
        reader_inspected, release_reader, writer_inspected = Event(), Event(), Event()
        original = search.ConversationIndex._schema_objects

        def coordinate(index):
            objects = original(index)
            if not index.write and not reader_inspected.is_set():
                reader_inspected.set()
                assert release_reader.wait(10)
            elif index.write:
                writer_inspected.set()
            return objects

        def read_legacy():
            with search.ConversationIndex(path) as index:
                return bool(index.candidates("migration", ["root"])["hits"])

        def migrate():
            with search.ConversationIndex(path, write=True) as index:
                return index.status()["schema_version"]

        with patch.object(
            search.ConversationIndex,
            "_schema_objects",
            autospec=True,
            side_effect=coordinate,
        ), ThreadPoolExecutor(max_workers=2) as pool:
            reader = pool.submit(read_legacy)
            assert reader_inspected.wait(10)
            writer = pool.submit(migrate)
            assert writer_inspected.wait(10)
            # DELETE journaling keeps the migration commit behind the reader's schema snapshot.
            assert not writer.done()
            release_reader.set()
            assert reader.result(timeout=10)
            assert writer.result(timeout=10) == 2


def test_missing_fts_support_and_sqlite_failures_use_safe_original_error():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "index.sqlite3"
        with patch.object(
            search.sqlite3,
            "connect",
            side_effect=sqlite3.OperationalError("SECRET no such module: fts5"),
        ):
            with _error("index_unavailable"):
                search.ConversationIndex(path, write=True)
            with _error("index_unavailable"):
                search.validate_search("SECRET query")


def test_corrupt_data_page_fails_safely_when_queried_without_full_open_scan():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "index.sqlite3"
        with search.ConversationIndex(path, write=True) as index:
            index.replace_root(_metadata(), [_source(_record("SECRET retained needle"))])
            page_size = index._db.execute("PRAGMA page_size").fetchone()[0]
            root_page = index._db.execute(
                "SELECT rootpage FROM sqlite_master WHERE name='chunks'"
            ).fetchone()[0]
        # Damage only this synthetic table's b-tree page header, not sqlite_master
        # or FTS. Compatibility/probe checks can succeed without reading this page.
        with path.open("r+b") as stream:
            stream.seek((root_page - 1) * page_size)
            stream.write(b"\x00")
        original = path.read_bytes()
        with search.ConversationIndex(path) as reader:
            with _error("index_unavailable"):
                reader.candidates("needle", ["root"])
        assert path.read_bytes() == original


def test_ordinary_open_and_candidate_queries_do_not_run_full_integrity_checks():
    with _index() as (index, path):
        index.replace_root(_metadata(), [_source(_record("needle"))])
        statements = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        with patch.object(search.sqlite3, "connect", side_effect=traced_connect):
            with search.ConversationIndex(path) as reader:
                assert reader.candidates("needle", ["root"])["hits"]
                assert not reader.candidates("absent", ["root"])["hits"]
        sql = "\n".join(statements).lower()
        assert "opentab_schema_probe" in sql
        assert "user_version" in sql
        assert "quick_check" not in sql
        assert "integrity_check" not in sql and "integrity-check" not in sql


def test_fts_creation_failure_rolls_back_new_schema_without_raw_errors():
    connect = sqlite3.connect

    class WithoutFTS(sqlite3.Connection):
        def execute(self, sql, *args):
            if "USING fts5" in sql:
                raise sqlite3.OperationalError("SECRET no such module: fts5")
            return super().execute(sql, *args)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "index.sqlite3"
        with patch.object(
            search.sqlite3,
            "connect",
            side_effect=lambda *args, **kw: connect(*args, factory=WithoutFTS, **kw),
        ):
            with _error("index_unavailable"):
                search.ConversationIndex(path, write=True)
        with connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        # Failure is not silently repaired on a subsequent write open.
        with _error("index_incompatible"):
            search.ConversationIndex(path, write=True)


def test_status_does_not_create_temp_fts_tables_or_expose_text():
    with _index() as (index, path):
        index.replace_root(_metadata(title="SECRET title"), [_source(_record("SECRET text"))])
        with search.ConversationIndex(path) as reader:
            statements = []
            reader._db.set_trace_callback(statements.append)
            status = reader.status()
            assert "SECRET" not in json.dumps(status)
            assert not any("CREATE" in sql.upper() for sql in statements)
            assert reader._db.execute("SELECT count(*) FROM sqlite_temp_master").fetchone()[0] == 0
        connect = sqlite3.connect

        class NoCreate(sqlite3.Connection):
            def execute(self, sql, *args):
                assert "CREATE" not in sql.upper()
                return super().execute(sql, *args)

        with patch.object(
            search.sqlite3,
            "connect",
            side_effect=lambda *args, **kw: connect(*args, factory=NoCreate, **kw),
        ):
            assert search.index_status(path) == status


def test_two_writers_serialize_and_readers_never_see_partial_root_replacement():
    with _index() as (index, path):
        index.replace_root(_metadata(), [_source(_record("oldtext"))])
        deleted, release, ready, start_second = Event(), Event(), Event(), Event()

        def first_writer():
            with search.ConversationIndex(path, write=True) as writer:
                original = writer._delete_root

                def paused(key):
                    original(key)
                    deleted.set()
                    assert release.wait(10)

                with patch.object(writer, "_delete_root", paused):
                    return writer.replace_root(
                        _metadata(title="first"), [_source(_record("firsttext"), snapshot="first")]
                    )

        def second_writer():
            with search.ConversationIndex(path, write=True) as writer:
                ready.set()
                assert start_second.wait(10)
                return writer.replace_root(
                    _metadata(title="second"), [_source(_record("secondtext"), snapshot="second")]
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            second = pool.submit(second_writer)
            assert ready.wait(10)
            first = pool.submit(first_writer)
            try:
                assert deleted.wait(10)
                start_second.set()
                assert index.roots()[0]["title"] == "Synthetic conversation"
                assert index.candidates("oldtext", ["root"])["hits"]
                assert not index.candidates("firsttext", ["root"])["hits"]
            finally:
                start_second.set()
                release.set()
            assert first.result(timeout=10)["changed"]
            assert second.result(timeout=10)["changed"]
        assert index.roots()[0]["title"] == "second"
        assert index.status()["passages"] == 1
        assert index.candidates("secondtext", ["root"])["hits"]
        assert not index.candidates("oldtext firsttext", ["root"])["hits"]


def test_busy_writer_error_is_safe_and_preserves_root():
    with _index() as (index, path):
        index.replace_root(_metadata(), [_source(_record("original"))])
        with search.ConversationIndex(path, write=True) as writer:
            writer._db.execute("PRAGMA busy_timeout=1")
            index._db.execute("BEGIN IMMEDIATE")
            try:
                with _error("index_unavailable"):
                    writer.replace_root(
                        _metadata(), [_source(_record("replacement"), snapshot="new")]
                    )
            finally:
                index._db.execute("ROLLBACK")
        assert index.candidates("original", ["root"])["hits"]


def test_validation_rejects_empty_operators_only_excess_terms_and_bad_bounds():
    for query in (None, True, 42, [], "", "   ", '"*():_-', "x" * 1001, "x " * 65):
        with _error("invalid_arguments"):
            search.validate_search(query)
    for name, maximum in (("limit", 100), ("max_chars", 120000)):
        for value in (True, False, 0, -1, maximum + 1, 1.0, "1", None):
            with _error("invalid_arguments"):
                search.validate_search("needle", **{name: value})
    for value in (
        True,
        20260101,
        "2026-9-01",
        "2026-02-30",
        "2026-09-11T00:00:00Z",
        "2026-09-11 ",
        "",
        "0000-01-01",
    ):
        for name in ("since", "until"):
            with _error("invalid_arguments"):
                search.validate_search("needle", **{name: value})
    with _error("invalid_arguments"):
        search.validate_search("needle", since="2026-09-12", until="2026-09-11")
    assert search.validate_search("x" * 1000, limit=100, max_chars=120000) is None
    assert (
        search.validate_search(
            "x " * 64, since="2024-02-29", until="2024-02-29", limit=1, max_chars=1
        )
        is None
    )
