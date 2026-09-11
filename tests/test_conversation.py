import base64
import io
import json
import os
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from opentab import conversation as conv


@contextmanager
def _error(code):
    try:
        yield
    except conv.ConversationError as exc:
        assert exc.code == code, (exc.code, code)
        assert exc.message == str(exc)
        assert "SECRET" not in str(exc)
        assert exc.__cause__ is None
        if exc.__context__ is not None and not exc.__suppress_context__:
            assert "SECRET" not in str(exc.__context__)
    else:
        raise AssertionError(f"expected {code}")


def _record(index, *texts, **extra):
    return {
        "id": f"source:{index}",
        "message_id": f"native:{index}",
        "record_id": f"record:{index}",
        "role": "user" if index % 2 == 0 else "assistant",
        "parts": [{"id": f"part:{index}:{i}", "text": text} for i, text in enumerate(texts)],
        **extra,
    }


def _source(records=None):
    return {
        "records": [_record(i, f"message {i}") for i in range(5)] if records is None else records,
        "execution_id": "execution:root",
        "executions": [{"id": "execution:root"}, {"id": "execution:child"}],
        "snapshot": "synthetic-snapshot",
        "ordering": "source",
        "limitations": ["synthetic-retained-history"],
    }


def _window(source, **options):
    return conv.window(source, root_key="machine:harness:root", **options)


def _encode(token):
    return base64.urlsafe_b64encode(json.dumps(token).encode()).decode().rstrip("=")


def _token(source):
    cursor = _window(source, limit=1)["next_cursor"]
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def test_source_key_is_stable_normalized_and_path_distinct():
    with tempfile.TemporaryDirectory() as directory:
        first = Path(directory) / "one" / "SECRET.jsonl"
        second = Path(directory) / "two" / "SECRET.jsonl"
        key = conv.source_key(first)
        assert key == conv.source_key(first)
        assert key == conv.source_key(first.parent / ".." / "one" / first.name)
        assert key != conv.source_key(second)
        assert "SECRET" not in key


def test_read_jsonl_preserves_physical_location_verbatim_unicode_and_order():
    text = "  Gr\u00fc\u00dfe \U0001f680 e\u0301\n\tend  "
    first_record = {"text": text, "nested": {"items": [1, None, True]}}
    with tempfile.TemporaryDirectory() as directory:
        first, second = (Path(directory) / name for name in ("first.jsonl", "second.jsonl"))
        first.write_bytes(
            b"\n \t\r\n" + json.dumps(first_record, ensure_ascii=False).encode() + b"\r\n\n"
        )
        second.write_bytes(b'{"text":"last, without newline"}')
        records, snapshot, limitations = conv.read_jsonl([first, second])
        assert records == [(first, 3, first_record), (second, 1, {"text": "last, without newline"})]
        assert limitations == []
        assert snapshot == conv.read_jsonl([first, second])[1]
        assert len(snapshot) == 64


def test_read_jsonl_skips_malformed_lines_with_one_sanitized_warning():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "SECRET.jsonl"
        path.write_bytes(
            b'{"text":"before"}\nSECRET malformed\n["SECRET"]\nnull\n42\n'
            b'"SECRET"\n{"text":"\xffSECRET"}\n{"text":"after"}\n'
        )
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            records, _, limitations = conv.read_jsonl([path])
        assert records == [(path, 1, {"text": "before"}), (path, 8, {"text": "after"})]
        assert limitations == ["malformed_jsonl_records_skipped"]
        assert output.getvalue() == ""


def test_read_jsonl_empty_sources_and_blank_files_have_no_records():
    assert conv.read_jsonl([])[::2] == ([], [])
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "empty.jsonl"
        path.write_bytes(b"\n \t\r\n")
        records, snapshot, limitations = conv.read_jsonl([path])
        assert records == [] and limitations == []
        assert snapshot != conv.read_jsonl([])[1]


def test_read_jsonl_snapshot_uses_raw_bytes_and_source_identity_not_metadata():
    with tempfile.TemporaryDirectory() as directory:
        first, second = (Path(directory) / name for name in ("first.jsonl", "second.jsonl"))
        raw = b'{"text":"a"}\n\nSECRET bad one\n'
        first.write_bytes(raw)
        second.write_bytes(raw)
        original = conv.read_jsonl([first])
        assert original[1] != conv.read_jsonl([second])[1]
        stat = first.stat()
        os.utime(first, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert conv.read_jsonl([first])[1] == original[1]
        first.write_bytes(raw.replace(b"bad one", b"bad two"))
        changed = conv.read_jsonl([first])
        assert changed[0] == original[0]
        assert changed[1] != original[1]
        first.write_bytes(raw.replace(b"\n\n", b"\n \n"))
        assert conv.read_jsonl([first])[1] not in (original[1], changed[1])
        assert conv.read_jsonl([first, second])[1] != conv.read_jsonl([second, first])[1]


def test_read_jsonl_returns_independent_fresh_records_without_writing_sources():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "source.jsonl"
        raw = b'{"nested":{"text":"original"}}\n'
        path.write_bytes(raw)
        records, snapshot, warnings = conv.read_jsonl([path])
        records[0][2]["nested"]["text"] = "changed by caller"
        warnings.append("changed by caller")
        again = conv.read_jsonl([path])
        assert again == ([(path, 1, {"nested": {"text": "original"}})], snapshot, [])
        assert path.read_bytes() == raw


def test_read_jsonl_source_budget_is_strict_and_cumulative():
    with tempfile.TemporaryDirectory() as directory:
        first, second = (Path(directory) / name for name in ("first.jsonl", "second.jsonl"))
        first.write_bytes(b"{}\n")
        second.write_bytes(b"{}\n")
        with patch.object(conv, "MAX_SOURCE_BYTES", 6):
            assert len(conv.read_jsonl([first, second])[0]) == 2
        with patch.object(conv, "MAX_SOURCE_BYTES", 5), _error("conversation_too_large"):
            conv.read_jsonl([first, second])
        with patch.object(conv, "MAX_SOURCE_BYTES", 2), _error("conversation_too_large"):
            conv.read_jsonl([first])


def test_read_jsonl_line_budget_counts_bytes_and_includes_blank_or_malformed_lines():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "SECRET.jsonl"
        raw = json.dumps({"text": "\U0001f680"}, ensure_ascii=False).encode() + b"\n"
        path.write_bytes(raw)
        with patch.object(conv, "MAX_LINE_BYTES", len(raw)):
            assert len(conv.read_jsonl([path])[0]) == 1
        with patch.object(conv, "MAX_LINE_BYTES", len(raw) - 1), _error("conversation_too_large"):
            conv.read_jsonl([path])
        for raw in (b" " * 12 + b"\n", b"SECRET malformed\n"):
            path.write_bytes(raw)
            with patch.object(conv, "MAX_LINE_BYTES", 12), _error("conversation_too_large"):
                conv.read_jsonl([path])


def test_read_jsonl_enforces_live_byte_budget_when_source_grows_after_stat():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "source.jsonl"
        path.write_bytes(b"{}\n")
        stat = path.stat()
        initial = SimpleNamespace(
            **{
                name: 0 if name == "st_size" else getattr(stat, name)
                for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            }
        )
        with patch.object(conv.os, "fstat", return_value=initial), patch.object(
            conv, "MAX_SOURCE_BYTES", 2
        ):
            with _error("conversation_too_large"):
                conv.read_jsonl([path])


def test_read_jsonl_missing_and_unreadable_sources_fail_without_raw_errors():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "SECRET-missing.jsonl"
        with _error("conversation_unavailable"):
            conv.read_jsonl([path])
        with patch.object(Path, "open", side_effect=PermissionError("SECRET access detail")):
            with _error("conversation_unavailable"):
                conv.read_jsonl([path])


def test_read_jsonl_detects_descriptor_change_during_read():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "source.jsonl"
        path.write_bytes(b"{}\n")
        initial = path.stat()
        changed = SimpleNamespace(
            **{
                name: getattr(initial, name) + (1 if name == "st_mtime_ns" else 0)
                for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            }
        )
        with patch.object(conv.os, "fstat", side_effect=[initial, changed]):
            with _error("source_changed"):
                conv.read_jsonl([path])


def test_read_jsonl_detects_path_replacement_after_descriptor_read():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "source.jsonl"
        path.write_bytes(b"{}\n")
        initial = path.stat()
        changed = SimpleNamespace(
            **{
                name: getattr(initial, name) + (1 if name == "st_ino" else 0)
                for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            }
        )
        with patch.object(Path, "stat", return_value=changed), _error("source_changed"):
            conv.read_jsonl([path])


def test_window_pages_all_records_beyond_one_hundred_without_mutation():
    source = _source([_record(i, f"message {i}") for i in range(237)])
    original = deepcopy(source)
    seen, sizes, cursor = [], [], None
    for page_number in range(3):
        page = _window(source, cursor=cursor, limit=100)
        assert page["has_earlier"] is (page_number > 0)
        assert page["has_more"] is (page_number < 2)
        assert all(record["record_complete"] for record in page["records"])
        assert all(not part["truncated"] for record in page["records"] for part in record["parts"])
        assert page["returned_chars"] == sum(
            len(part["text"]) for record in page["records"] for part in record["parts"]
        )
        seen.extend(record["id"] for record in page["records"])
        sizes.append(len(page["records"]))
        cursor = page["next_cursor"]
    assert sizes == [100, 100, 37]
    assert seen == [record["id"] for record in source["records"]]
    assert cursor is None
    assert source == original


def test_window_defaults_and_metadata_are_explicit():
    source = _source([_record(i, "x") for i in range(21)])
    page = _window(source)
    assert len(page["records"]) == 20
    assert page["limit"] == 20 and page["max_chars"] == 20000
    assert page["session_key"] == "machine:harness:root"
    for name in ("execution_id", "executions", "snapshot", "ordering", "limitations"):
        assert page[name] == source[name]
    assert page["history_completeness"] == "unknown"
    assert page["has_more"] and not page["has_earlier"]


def test_window_multipart_continuation_reconstructs_every_codepoint_and_metadata():
    source = _source(
        [
            _record(0, "A\U0001f680e\u0301BC", "", "\u65e5\u672c\u8a9e\nlast"),
            _record(1, "", "finish \U0001f680"),
            _record(2, "done"),
        ]
    )
    original = deepcopy(source)
    for budget in (1, 2, 3, 6, 7, 120000):
        reconstructed = {part["id"]: "" for record in source["records"] for part in record["parts"]}
        encountered_parts, encountered_records, cursor = set(), [], None
        for page_number in range(100):
            page = _window(source, cursor=cursor, limit=2, max_chars=budget)
            assert page["has_earlier"] is (page_number > 0)
            assert 0 <= page["returned_chars"] <= budget
            assert 0 < len(page["records"]) <= 2
            assert page["returned_chars"] == sum(
                len(part["text"]) for record in page["records"] for part in record["parts"]
            )
            for record in page["records"]:
                expected = next(item for item in source["records"] if item["id"] == record["id"])
                first_occurrence = record["id"] not in encountered_records
                if first_occurrence:
                    encountered_records.append(record["id"])
                for part in record["parts"]:
                    full = next(
                        item["text"] for item in expected["parts"] if item["id"] == part["id"]
                    )
                    offset = len(reconstructed[part["id"]])
                    assert part["text_offset"] == offset
                    assert part["text_total_chars"] == len(full)
                    assert part["text"] == full[offset : offset + len(part["text"])]
                    assert part["truncated"] is (
                        offset > 0 or offset + len(part["text"]) < len(full)
                    )
                    reconstructed[part["id"]] += part["text"]
                    encountered_parts.add(part["id"])
                finished = all(
                    part["id"] in encountered_parts and reconstructed[part["id"]] == part["text"]
                    for part in expected["parts"]
                )
                assert record["record_complete"] is (first_occurrence and finished)
            next_cursor = page["next_cursor"]
            assert page["has_more"] is (next_cursor is not None)
            if not page["has_more"]:
                break
            assert next_cursor != cursor
            cursor = next_cursor
        else:
            raise AssertionError("continuation did not terminate")
        assert encountered_records == [record["id"] for record in source["records"]]
        assert encountered_parts == set(reconstructed)
        assert reconstructed == {
            part["id"]: part["text"] for record in source["records"] for part in record["parts"]
        }
        assert source == original


def test_window_has_earlier_tracks_start_not_outgoing_cursor():
    source = _source([_record(0, "abcd", "ef"), _record(1, "last")])
    token = _token(source)
    token[2:] = [0, 0, 0]
    page = _window(source, cursor=_encode(token), max_chars=2)
    assert not page["has_earlier"]
    assert page["has_more"]
    page = _window(source, cursor=page["next_cursor"], max_chars=2)
    assert page["has_earlier"]
    page = _window(source, cursor=page["next_cursor"], max_chars=2)
    assert page["has_earlier"]
    assert page["records"][0]["parts"][0]["text_offset"] == 0
    assert not page["records"][0]["record_complete"]
    page = _window(source, cursor=page["next_cursor"])
    assert page["has_earlier"] and not page["has_more"]
    assert page["records"][0]["record_complete"]


def test_window_tail_and_anchor_preceding_context_clamp_to_available_records():
    source = _source()
    for options, expected, earlier in (
        ({"tail": True, "limit": 2}, [3, 4], True),
        ({"tail": True, "limit": 10}, [0, 1, 2, 3, 4], False),
        ({"anchor": "source:3", "before": 2, "limit": 3}, [1, 2, 3], True),
        ({"anchor": "native:1", "before": 99, "limit": 3}, [0, 1, 2], False),
        ({"anchor": "record:4", "limit": 1}, [4], True),
    ):
        page = _window(source, **options)
        assert [record["id"] for record in page["records"]] == [f"source:{i}" for i in expected]
        assert page["has_earlier"] is earlier
        assert page["has_more"] is (expected[-1] < 4)
    page = _window(source, tail=True, limit=2, max_chars=1)
    assert page["records"][0]["id"] == "source:3"
    assert page["has_earlier"] and page["has_more"]


def test_window_duplicate_native_anchors_are_ambiguous_but_source_ids_are_exact():
    source = _source(
        [
            _record(0, "first", message_id="duplicate", record_id="shared"),
            _record(1, "second", message_id="duplicate", record_id="shared"),
            _record(2, "third", message_id="source:0"),
        ]
    )
    for anchor in ("duplicate", "shared"):
        with _error("ambiguous_anchor"):
            _window(source, anchor=anchor)
    for index in (0, 1):
        page = _window(source, anchor=f"source:{index}", limit=1)
        assert page["records"][0]["id"] == f"source:{index}"
    with _error("anchor_not_found"):
        _window(source, anchor="SECRET missing anchor")


def test_window_rejects_stale_root_execution_and_snapshot_cursors():
    source = _source()
    cursor = _window(source, limit=1)["next_cursor"]
    with _error("stale_cursor"):
        conv.window(source, root_key="other-machine:harness:root", cursor=cursor)
    for key in ("execution_id", "snapshot"):
        changed = {**source, key: "different"}
        with _error("stale_cursor"):
            _window(changed, cursor=cursor)
    assert _window(source, cursor=cursor, limit=2, max_chars=1)["records"][0]["id"] == "source:1"


def test_window_rejects_invalid_argument_types_bounds_and_combinations():
    source = _source()
    for name, invalid in (
        ("limit", (True, False, None, "1", 1.0, [], {}, -1, 0, 101)),
        ("max_chars", (True, False, None, "1", 1.0, [], {}, -1, 0, 120001)),
        ("before", (True, False, None, "1", 1.0, [], {}, -1, 100)),
        ("tail", (0, 1, None, "false", [], {})),
        ("anchor", (True, False, 1, [], {}, "", "x" * 1025)),
        ("cursor", (True, False, 1, [], {}, "", "x" * 8193)),
    ):
        for value in invalid:
            with _error("invalid_arguments"):
                _window(source, **{name: value})
    for options in (
        {"before": 1},
        {"before": 1, "tail": True},
        {"before": 1, "cursor": "token"},
        {"anchor": "source:0", "cursor": "token"},
        {"anchor": "source:0", "tail": True},
        {"cursor": "token", "tail": True},
    ):
        with _error("invalid_arguments"):
            _window(source, **options)
    conv.validate_window(anchor="a" * 1024, before=99, limit=100, max_chars=120000)
    conv.validate_window(cursor="a" * 8192, limit=1, max_chars=1)


def test_window_rejects_malformed_cursor_encoding_and_shapes():
    source = _source()
    for cursor in ("!not-base64!", "a", "\u00e9", "e30", "bnVsbA", "Ww", "_w"):
        with _error("invalid_cursor"):
            _window(source, cursor=cursor)
    token = _token(source)
    for malformed in (None, True, 1, "SECRET", {}, [], token[:-1], token + [0], [0] + token[1:]):
        with _error("invalid_cursor"):
            _window(source, cursor=_encode(malformed))
    for binding in (None, True, 1, [], {}):
        with _error("invalid_cursor"):
            _window(source, cursor=_encode([1, binding, *token[2:]]))


def test_window_rejects_boolean_and_noninteger_cursor_versions():
    source = _source()
    token = _token(source)
    for version in (True, False, 1.0, "1", None, [], {}):
        with _error("invalid_cursor"):
            _window(source, cursor=_encode([version, *token[1:]]))


def test_window_rejects_cursor_position_types_bools_and_out_of_range_values():
    source = _source()
    token = _token(source)
    for index in (2, 3, 4):
        for value in (True, False, None, "0", 0.0, [], {}, -1):
            malformed = list(token)
            malformed[index] = value
            with _error("invalid_cursor"):
                _window(source, cursor=_encode(malformed))
    for position in ([5, 0, 0], [10000, 0, 0], [0, 1, 0], [0, 0, 10], [0, 10000, 0]):
        with _error("invalid_cursor"):
            _window(source, cursor=_encode(token[:2] + position))


def test_window_empty_records_and_empty_parts_always_make_progress():
    source = _source([_record(0), _record(1, ""), _record(2, "", ""), _record(3, "x"), _record(4)])
    cursor = None
    for index in range(5):
        page = _window(source, cursor=cursor, limit=1, max_chars=1)
        assert len(page["records"]) == 1
        record = page["records"][0]
        assert record["id"] == f"source:{index}"
        assert record["record_complete"]
        assert page["returned_chars"] == (1 if index == 3 else 0)
        assert page["has_earlier"] is (index > 0)
        assert page["has_more"] is (index < 4)
        assert page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    assert cursor is None
    token = _token(source)
    for position in ([0, 0, 1], [0, 1, 0], [1, 0, 1]):
        with _error("invalid_cursor"):
            _window(source, cursor=_encode(token[:2] + position))


def test_window_empty_scope_has_no_cursor_or_history_flags():
    source = _source([])
    for options in ({}, {"tail": True}):
        page = _window(source, **options)
        assert page["records"] == []
        assert page["returned_chars"] == 0
        assert page["next_cursor"] is None
        assert not page["has_more"] and not page["has_earlier"]
    with _error("anchor_not_found"):
        _window(source, anchor="SECRET missing")
    token = _token(_source())
    token[2:] = [0, 0, 0]
    with _error("invalid_cursor"):
        _window(source, cursor=_encode(token))
