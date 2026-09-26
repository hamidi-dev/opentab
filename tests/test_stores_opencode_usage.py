import argparse
import io
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from opentab import diagnostics as debug
from opentab.stores.cached import CachedStore
from opentab.stores.opencode import Store
from opentab.stores.opencode_usage import _read_usage, compact_usage, compact_usage_stream
from opentab.tui.app import App
from opentab.web.report import build_payload

from tests.test_stores_opencode_v2 import _message, _populate_v2, _session, _v2_db


def test_opencode_debug_explains_hit_refresh_and_detail_without_changing_results():
    with tempfile.TemporaryDirectory() as tmp, _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        args = argparse.Namespace(demo=False, since=None, until=None, days=None)
        expected = _reference(store.db)
        filename = str(Path(tmp) / "debug.jsonl")
        with debug.session(filename=filename):
            cache = CachedStore(store, "opencode|" + store.db, args)
            assert _read(cache) == expected
            assert _read(cache) == expected  # exact fingerprint hit
            app = App(cache, args)
            app.prefetch_session_data("root")
            app.prefetch_session_data("root")  # memos, not a second detail read
            writer.execute(
                "insert into session_message values (?,?,?,?,?,?,?)",
                _message(
                    "private-native-id",
                    "root",
                    "assistant",
                    1000,
                    {
                        "model": {"id": "private-model"},
                        "tokens": {"input": 7},
                        "content": ["secret-body"],
                    },
                ),
            )
            writer.commit()
            assert _read(cache) == _reference(store.db)
            assert cache.served_incrementally
        text = Path(filename).read_text()
        for secret in (
            store.db,
            "secret-body",
            "private-native-id",
            "private-model",
            "patch input",
            "patch result",
        ):
            assert secret not in text
        records = [json.loads(line) for line in text.splitlines()]
        decisions = [r.get("result") for r in records if r["event"] == "cache.decision"]
        assert {"miss", "parsed", "incremental_accounting"} <= set(decisions)
        assert any(r["event"] == "cache.input_changed" and r["mtime_changed"] for r in records)
        assert any(
            r["event"] == "usage.native_summary" and r["decoded"] == 1 and r["reused"] > 0
            for r in records
        )
        summaries = [r for r in records if r["event"] == "usage.native_summary"]
        assert all(
            r["decoded"] == r["sql_projected"] + r["streamed"] + r["python_full_decode"]
            for r in summaries
        )
        if store._usage_cache._sql_projection:
            assert any(r["sql_projected"] > 0 for r in summaries)
        assert sum(r["event"] == "app.session_turns.start" for r in records) == 1
        assert any(r["event"] == "app.session_memos" and r["turns"] for r in records)
        assert any(
            r["event"] == "opencode.timeline_messages.end" and "sql_ms" in r for r in records
        )


def _read(store):
    return (
        sorted((asdict(w) for w in store.workflows()), key=lambda w: w["id"]),
        sorted(
            (dict(r) for r in store.model_breakdown()),
            key=lambda r: (r["root_id"], r["model_name"]),
        ),
    )


def _reference(path):
    raw = Store(path, argparse.Namespace(demo=False))
    try:
        # Existing SQL projection is the reference, over the same stopped fixture.
        raw._usage_cache = None
        return _read(raw)
    finally:
        raw.conn.close()


def test_opencode_usage_streaming_projection_preserves_sqlite_semantics():
    examples = [
        {
            "content": ['quotes: \\"', {"nested": [True, False, None, -1.25e10]}],
            "tokens": {"input": 7},
        },
        {"model": {"providerID": "p", "id": "m"}, "time": None, "text": "\x00\n\U0001f600"},
        {"time": [], "cost": 0, "content": []},
        {},
    ]
    connection = sqlite3.connect(":memory:")
    try:
        for value in examples:
            text = json.dumps(value)
            compact = compact_usage(text)
            for field in ("tokens", "model", "time", "cost"):
                query = "select json_extract(?, ?), json_extract(?, ?)"
                row = connection.execute(
                    query, (text, "$." + field, compact, "$." + field)
                ).fetchone()
                assert row[0] == row[1], (text, field)
        # SQLite keeps first occurrences, including nested duplicate keys.
        text = '{"tokens":{"input":7,"input":8},"tokens":{"input":9},"content":[]}'
        assert (
            connection.execute(
                "select json_extract(?, '$.tokens.input')", [compact_usage(text)]
            ).fetchone()[0]
            == 7
        )
        for bad in (
            '{"content":[1,]}',
            '{"content":{"a":}}',
            '{"content":"bad\\q"}',
            '{"content":NaN}',
            '{"tokens":{},}',
            '{"content":[01]}',
            '{"content":[true false]}',
            '{"content":[1]} trailing',
            '{"content":"\n"}',
            '{"content":"\\u12xy"}',
        ):
            assert compact_usage(bad) == "null", bad
    finally:
        connection.close()


def test_opencode_usage_does_not_decode_or_keep_large_inline_output():
    raw = (
        '{"content":[{"text":"'
        + "x" * (16 * 1024 * 1024)
        + '"}],"tokens":{"input":13},"model":{"id":"m"}}'
    )
    loads = json.loads
    lengths = []

    def track(text, *args, **kwargs):
        lengths.append(len(text))
        return loads(text, *args, **kwargs)

    with patch("opentab.stores.opencode_usage.json.loads", side_effect=track):
        compact = compact_usage(raw)
    assert len(compact) < 100
    assert max(lengths) < 30
    assert loads(compact)["tokens"]["input"] == 13


def test_opencode_usage_sql_projection_preserves_values_and_skips_inline_python_decode():
    conn = sqlite3.connect(":memory:")
    conn.execute("create table session_message(data)")
    raw_cases = [
        '{"content":[{"private":"' + "x" * 100000 + '"}],"tokens":{"input":7},"cost":1.5}',
        '{"tokens":{"input":null,"input":8},"tokens":{"input":9},"model":{"id":"first","id":"last"}}',
        '{"mod\\u0065l":{"id":"escaped"},"cost":-0.0}',
        '{"model":{"id":"a\\u0000b","providerID":"\\ud83d\\ude00"},"tokens":{"input":9007199254740993}}',
        '{"cost":1e999,"tokens":{"input":7}}',
        '{"tokens":{"input":true,"cache":{"read":false}},"time":[null,1]}',
        '{"content":NaN,"tokens":{"input":5}}',
        '{"content":[1,],"cost":5}',
        "{}",
        "[]",
        "null",
        "42",
        None,
        b'{"tokens":{"input":5}}',
    ]
    loads = json.loads
    lengths = []

    def track(text, *args, **kwargs):
        lengths.append(len(text))
        return loads(text, *args, **kwargs)

    try:
        escaped_keys = (
            conn.execute("select json_extract(?, '$.model')", ['{"\\u006dodel":1}']).fetchone()[0]
            == 1
        )
        for raw in raw_cases:
            rid = conn.execute("insert into session_message values(?)", [raw]).lastrowid
            expected = _read_usage(conn, rid, False)[0]
            lengths.clear()
            with patch("opentab.stores.opencode_usage.json.loads", side_effect=track):
                actual, _, _, streamed, projected = _read_usage(conn, rid, False, escaped_keys)
            for path in ("$.tokens.input", "$.tokens.cache.read", "$.model", "$.time", "$.cost"):
                left, right = conn.execute(
                    "select json_extract(?,?),json_extract(?,?)", [expected, path, actual, path]
                ).fetchone()
                assert left == right, (raw, path, left, right)
            assert (
                conn.execute("select json_type(?, '$.time')", [expected]).fetchone()
                == conn.execute("select json_type(?, '$.time')", [actual]).fetchone()
            )
            if raw is raw_cases[0] and hasattr(conn, "blobopen") and escaped_keys:
                assert projected and not streamed
                assert max(lengths) < 1000, lengths
    finally:
        conn.close()


def test_opencode_usage_streaming_validates_skipped_depth_and_delimiters():
    with patch("opentab.stores.opencode_usage._DECODE_LIMIT", 0), patch(
        "opentab.stores.opencode_usage._MAX_DEPTH", 3
    ):
        for content in ("[[1]]", '{"a":{"b":1}}', "[{},[]]", '"escaped\\\\\\" quote"'):
            raw = '{"content":' + content + ',"tokens":{"input":7}}'
            assert json.loads(compact_usage(raw))["tokens"]["input"] == 7, content
        for content in ("[[[1]]]", '{"a":{"b":{}}}', "[1,]", '{"a":1,}', "[}", '"bad\\q"'):
            assert compact_usage('{"content":' + content + ',"cost":1}') == "null", content


def test_opencode_usage_persistent_cache_reads_only_changed_native_messages():
    with _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        args = argparse.Namespace(demo=False)
        cache_id = "opencode|" + store.db
        cached = CachedStore(store, cache_id, args)
        assert _read(cached) == _reference(store.db)
        assert "content" not in json.dumps(cached._accounting_payload())
        writer.execute(
            "insert into session_message values (?,?,?,?,?,?,?)",
            _message(
                "new",
                "root",
                "assistant",
                1000,
                {
                    "model": {"providerID": "other", "id": "model"},
                    "tokens": {"input": 11},
                    "content": ["new body"],
                },
            ),
        )
        writer.commit()
        restarted = Store(store.db, args)
        try:
            fresh = CachedStore(restarted, cache_id, args)
            with patch("opentab.stores.opencode_usage._read_usage", wraps=_read_usage) as parse:
                actual = _read(fresh)
            assert parse.call_count == 1
            assert fresh.served_incrementally
            assert actual == _reference(store.db)
            assert "new body" not in json.dumps(fresh._disk)
            # Revisions, removals and topology changes all replace, never add deltas.
            writer.execute(
                "update session_message set data=?,time_updated=2001 where id='new'",
                [json.dumps({"model": {"id": "changed"}, "tokens": {"input": 15}})],
            )
            writer.execute("delete from session_message where id='a-root'")
            writer.execute("update session_v2 set parent_id=null where id='child'")
            writer.commit()
            with patch("opentab.stores.opencode_usage._read_usage", wraps=_read_usage) as parse:
                actual = _read(fresh)
            assert parse.call_count == 1
            assert actual == _reference(store.db)
            assert (
                len(fresh._accounting_payload()["rows"])
                == writer.execute("select count(*) from session_message").fetchone()[0]
            )
        finally:
            restarted.conn.close()


def test_opencode_usage_stream_handles_every_chunk_boundary_and_rejects_malformed():
    values = [
        '{"content":[{"text":"quotes: \\" \\u1234 \\\\ / 😀"},[true,false,null,-1.25e+3]],"tokens":{"input":13}}',
        '{"tokens":{"input":7,"input":9},"tokens":{"input":99},"cost":1e309}',
        "{}",
    ]
    invalid = [
        '{"content":[1,]}',
        '{"content":{"x":}}',
        '{"content":"\\q"}',
        '{"content":"\\u1xyz"}',
        '{"content":[01]}',
        '{"content":[truefalse]}',
        '{"cost":1,}',
        '{"content":"unterminated}',
        '{"cost":1} trailing',
        '{"content":[[[1]]]}',
    ]
    with patch("opentab.stores.opencode_usage._MAX_DEPTH", 3):
        for text in values + invalid:
            for chunk in range(1, 17):
                data = io.BytesIO(text.encode())
                actual = compact_usage_stream(
                    lambda n, data=data, chunk=chunk: data.read(min(chunk, n))
                )
                if text in invalid:
                    assert actual == "null", (text, chunk, actual)
                else:
                    # Compare via SQLite, including first duplicate keys and infinity.
                    c = sqlite3.connect(":memory:")
                    try:
                        for field in ("tokens", "cost"):
                            assert (
                                c.execute(
                                    "select json_extract(?,?)", [actual, "$." + field]
                                ).fetchone()
                                == c.execute(
                                    "select json_extract(?,?)", [text, "$." + field]
                                ).fetchone()
                            )
                    finally:
                        c.close()


def test_opencode_usage_large_blob_streaming_keeps_results_without_full_payload_fetch():
    if not hasattr(sqlite3.Connection, "blobopen"):
        return  # Python 3.9/3.10 exercise the bounded-decoder fallback above.
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        raw = json.dumps(
            {
                "content": [{"text": ('escaped " quote\\\n😀' * 10000)}],
                "model": {"id": "large"},
                "tokens": {"input": 42},
            }
        )
        writer.execute("update session_message set data=? where id='a-root'", [raw])
        writer.commit()
        expected = _reference(store.db)
        queries = []
        store.conn.set_trace_callback(queries.append)
        with patch("opentab.stores.opencode_usage._DECODE_LIMIT", 1024):
            assert _read(store) == expected
        target = writer.execute("select rowid from session_message where id='a-root'").fetchone()[0]
        assert not any(
            q.endswith("where rowid=" + str(target)) and q.startswith("select data")
            for q in queries
        )


def test_opencode_usage_split_cache_is_lazy_and_skips_unchanged_scalar_write():
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        args = argparse.Namespace(demo=False)
        key = "opencode|" + store.db
        cached = CachedStore(store, key, args)
        expected = _read(cached)
        sidecar = Path(cached._path + ".usage.sqlite3")
        before = sidecar.read_bytes()
        before_stat = sidecar.stat().st_mtime_ns
        restarted = Store(store.db, args)
        try:
            with patch.object(
                CachedStore, "_accounting_payload", side_effect=AssertionError("not lazy")
            ):
                warm = CachedStore(restarted, key, args)
                assert _read(warm) == expected
            # Session fields may change without any native message changes.
            writer.execute("update session_v2 set title='renamed' where id='root'")
            writer.commit()
            warm = CachedStore(restarted, key, args)
            assert _read(warm) == _reference(store.db)
            assert sidecar.read_bytes() == before
            assert sidecar.stat().st_mtime_ns == before_stat
            # A malformed/missing sidecar must rebuild, never hide accounting.
            sidecar.write_text("broken")
            writer.execute("update session_v2 set title='again' where id='root'")
            writer.commit()
            assert _read(warm) == _reference(store.db)
        finally:
            restarted.conn.close()


def test_opencode_tools_numeric_metadata_matches_original_detail_queries():
    with _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        reference = Store(store.db, argparse.Namespace(demo=False))
        reference._usage_cache = None
        try:
            for root in ("root", "other", "child"):
                expected = [dict(r) for r in reference.tool_breakdown(root)]
                queries = []
                store.conn.set_trace_callback(queries.append)
                actual = [dict(r) for r in store.tool_breakdown(root)]
                assert actual == expected, (actual, expected)
                tool_query = queries[-1]
                assert "opentab_message_usage" in tool_query
                assert "group_concat" not in tool_query
                assert "json_set" not in tool_query
                assert not store.conn.in_transaction
            # A source commit invalidates the shared name/readability memo and
            # the numeric snapshot; no TEMP-table transaction may pin old data.
            writer.execute(
                "update session_message set data=?,time_updated=9999 where id='a-root'",
                [
                    json.dumps(
                        {
                            "model": {"id": "new"},
                            "tokens": {"input": 19},
                            "content": [{"type": "tool", "name": "changed"}],
                        }
                    )
                ],
            )
            writer.commit()
            assert [dict(r) for r in store.tool_breakdown("root")] == [
                dict(r) for r in reference.tool_breakdown("root")
            ]
        finally:
            reference.conn.close()


def test_opencode_usage_indexed_sidecar_loads_only_selected_executions():
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        args = argparse.Namespace(demo=False)
        key = "opencode|" + store.db
        _read(CachedStore(store, key, args))
        restarted = Store(store.db, args)
        try:
            cached = CachedStore(restarted, key, args)
            payload = cached._accounting_payload(["root", "child"])
            assert len(payload["rows"]) == 4
            with patch(
                "opentab.stores.opencode_usage._read_usage",
                side_effect=AssertionError("decoded cached data"),
            ):
                for root in ("root", "other", "root"):
                    cached.workflow_nodes(root)
            assert len(restarted._usage_cache.rows) == 4
        finally:
            restarted.conn.close()


def test_opencode_usage_legacy_edits_and_migration_keep_accounting_and_worked_time():
    with _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        writer.execute("insert into session values ('legacy',null,'old','/repo',null,1,40)")
        writer.execute(
            "insert into message values ('old','legacy',?)",
            [
                json.dumps(
                    {
                        "role": "assistant",
                        "time": {"created": 20},
                        "tokens": {"input": 7},
                        "modelID": "old",
                    }
                )
            ],
        )
        writer.commit()
        cached = CachedStore(store, "opencode|" + store.db, argparse.Namespace(demo=False))
        assert _read(cached) == _reference(store.db)
        writer.execute(
            "update message set data=? where id='old'",
            [json.dumps({"role": "assistant", "tokens": {"input": 99}})],
        )
        writer.commit()
        assert _read(cached) == _reference(store.db)
        writer.execute(
            "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _session("legacy")
        )
        writer.commit()
        assert _read(cached) == _reference(store.db)


def test_opencode_usage_status_stays_scoped_and_model_reads_refresh_after_writes():
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        with patch("opentab.stores.opencode_usage._read_usage", wraps=_read_usage) as parse:
            nodes = store.workflow_nodes("root")
        assert {row["id"] for row in nodes} == {"root", "child"}
        assert parse.call_count == 4  # two messages in each of these executions
        assert _read(store) == _reference(store.db)  # scoped -> full is not a false hit
        writer.execute(
            "update session_message set data=?,time_updated=9999 where id='a-root'",
            [json.dumps({"model": {"id": "switched"}, "tokens": {"input": 33}})],
        )
        writer.commit()
        # model_breakdown can be called without a preceding workflows refresh.
        actual = sorted(
            (dict(r) for r in store.model_breakdown()),
            key=lambda r: (r["root_id"], r["model_name"]),
        )
        assert actual == _reference(store.db)[1]


def test_opencode_usage_unknown_and_inflight_revisions_are_not_reused():
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        writer.execute("update session_message set time_updated=null where id='a-root'")
        writer.execute("update session_message set time_updated=9999999999999 where id='a-child'")
        writer.commit()
        _read(store)
        writer.execute(
            "update session_message set data=? where id in ('a-root','a-child')",
            [json.dumps({"tokens": {"input": 42}})],
        )
        writer.commit()
        with patch("opentab.stores.opencode_usage._read_usage", wraps=_read_usage) as parse:
            assert _read(store) == _reference(store.db)
        assert parse.call_count == 2


def test_opencode_usage_malformed_persistent_projection_rebuilds_without_losing_rows():
    with _v2_db() as (writer, store):
        _populate_v2(writer)
        expected = _read(store)
        payload = store.accounting_cache()
        payload["rows"] = [[[], []]]
        store.restore_accounting_cache(payload)
        with patch("opentab.stores.opencode_usage._read_usage", wraps=_read_usage) as parse:
            assert _read(store) == expected
        assert parse.call_count == 0  # malformed disk payload cannot erase valid live rows
        fresh = Store(store.db, argparse.Namespace(demo=False))
        try:
            fresh.restore_accounting_cache(payload)
            with patch("opentab.stores.opencode_usage._read_usage", wraps=_read_usage) as parse:
                assert _read(fresh) == expected
            assert parse.call_count == 7  # a fresh process rejects rather than trusting it
        finally:
            fresh.conn.close()


def test_opencode_usage_tui_and_web_preserve_deferred_models_and_exact_usage():
    with _v2_db(legacy=True) as (writer, store):
        _populate_v2(writer)
        args = argparse.Namespace(demo=False, since=None, until=None, days=None)
        cached = CachedStore(store, "opencode|" + store.db, args)
        with patch.object(cached, "model_breakdown", wraps=cached.model_breakdown) as models:
            app = App(cached, args)
            assert models.call_count == 0
            payload = build_payload(app)
            assert models.call_count == 1
        reference = Store(store.db, args)
        reference._usage_cache = None
        try:
            expected = build_payload(App(reference, args))
            for key in ("workflows", "models", "nodes"):
                assert payload[key] == expected[key], key
        finally:
            reference.conn.close()


def test_old_runtime_large_accounting_keeps_tokens_and_handles_nontext():
    class OlderConnection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, *args):
            assert "substr(" not in args[0].lower()
            return self.conn.execute(*args)

    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(os.path.join(tmp, "source.db"))
        try:
            conn.execute("create table session_message(data text)")
            conn.execute(
                "insert into session_message values(?)",
                [
                    json.dumps(
                        {
                            "content": "z" * (8 * 1024 * 1024),
                            "tokens": {"input": 23},
                        }
                    )
                ],
            )
            compact, size, _, streamed, _ = _read_usage(OlderConnection(conn), 1, False)
            assert size > 8 * 1024 * 1024 and not streamed
            assert json.loads(compact)["tokens"]["input"] == 23
            for value in (b"\xffinvalid", None, 42):
                conn.execute("update session_message set data=?", [value])
                compact, _, _, _, _ = _read_usage(OlderConnection(conn), 1, False)
                assert compact == "null"
        finally:
            conn.close()
