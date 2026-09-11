"""Synthetic-only coverage for the developer recall benchmark."""

import json
import sqlite3
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from scripts import recall_benchmark as benchmark


def _case(path, harness="opencode", case_id="case-1"):
    return {
        "id": case_id,
        "question": "Which fruit was selected?",
        "tags": ["synthetic"],
        "expected": [
            {
                "harness": harness,
                "session_id": "session-1",
                "message_id": "message-2",
                "quote": "selected pear",
                "source": {"path": str(path), "part_id": "part-2", "message_ordinal": 2},
            }
        ],
    }


def _hit(session_id="session-1", message_id="message-2", text="We selected pear.", **extra):
    return {
        "harness": "opencode",
        "session_id": session_id,
        "passages": [{"message_id": message_id, "text": text}],
        "read_text": text,
        **extra,
    }


def _response(cases, **row):
    return {
        "supported_harnesses": ["opencode"],
        "profiles": ["recall"],
        "observations": [
            {"profile": "recall", "case_id": case["id"], "status": "ok", "hits": [], **row}
            for case in cases
        ],
    }


@contextmanager
def _rejects(message):
    try:
        yield
    except ValueError as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError("Expected ValueError containing " + repr(message))


@contextmanager
def _opencode_source(role="assistant", part_type="text", synthetic=False):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "source.sqlite"
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
                "time_created INTEGER, data TEXT);"
                "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, data TEXT);"
            )
            # Reverse insertion order and tied timestamps exercise the ID tie-breaker.
            connection.executemany(
                "INSERT INTO message VALUES (?, ?, ?, ?)",
                [
                    ("message-2", "session-1", 10, json.dumps({"role": role})),
                    ("message-1", "session-1", 10, json.dumps({"role": "user"})),
                    ("message-0", "other-session", 0, json.dumps({"role": "user"})),
                ],
            )
            connection.executemany(
                "INSERT INTO part VALUES (?, ?, ?)",
                [
                    (
                        "part-2",
                        "message-2",
                        json.dumps(
                            {"type": part_type, "text": "We selected pear.", "synthetic": synthetic}
                        ),
                    ),
                    ("part-1", "message-1", json.dumps({"type": "text", "text": "No fruit yet."})),
                ],
            )
            connection.commit()
        finally:
            connection.close()
        yield _case(path)


@contextmanager
def _jsonl_source(harness, content=None, role="assistant"):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / (harness + ".jsonl")
        if content is None:
            content = [
                {
                    "type": "text" if harness == "claude" else "output_text",
                    "text": "We selected pear.",
                }
            ]
        body = {"role": role, "content": content}
        if harness == "claude":
            body["id"] = "message-2"
            records = [{"sessionId": "session-1", "uuid": "uuid-2", "type": role, "message": body}]
            line = 1
        else:
            records = [
                {"type": "session_meta", "payload": {"id": "session-1"}},
                {"type": "response_item", "payload": {"type": "message", **body}},
            ]
            line = 2
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        case = _case(path, harness)
        case["expected"][0]["source"] = {"path": str(path), "line": line}
        if harness == "codex":
            case["expected"][0]["message_id"] = "line:2"
        yield case


def test_load_cases_combines_files_and_preserves_adapter_owned_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        first = _case(Path(tmp) / "unused.sqlite")
        first["adapter_args"] = {
            "profile": "natural-language",
            "options": {"directory": "fixture-project", "strategy": "adapter-owned"},
        }
        second = _case(Path(tmp) / "unused.jsonl", "claude", "case-2")
        paths = [Path(tmp) / "first.json", Path(tmp) / "second.json"]
        for path, case in zip(paths, (first, second)):
            path.write_text(json.dumps([case]), encoding="utf-8")
        assert benchmark.load_cases(paths) == [first, second]


def test_load_cases_rejects_invalid_json_nonarrays_and_empty_sets():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cases.json"
        for text, error in (
            ("{", ""),
            ("{}", "JSON arrays"),
            ("null", "JSON arrays"),
            ("[null]", "JSON objects"),
            ("[]", "Empty evaluation set"),
        ):
            path.write_text(text, encoding="utf-8")
            with _rejects(error):
                benchmark.load_cases([path])
        with _rejects("Empty evaluation set"):
            benchmark.load_cases([])


def test_load_cases_rejects_malformed_case_fields_and_anchors():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cases.json"
        valid = _case(Path(tmp) / "unused.sqlite")
        invalid = []
        for field, values, error in (
            ("id", [None, "", 1], "Case IDs"),
            ("question", [None, "", " \t", 1], "question"),
            ("tags", [None, [], [1], "synthetic", [""], ["decision", "decision"]], "tags"),
            ("expected", [None, []], "evidence anchor"),
        ):
            for value in values:
                invalid.append(({**valid, field: value}, error))
            if field in ("id", "question", "tags", "expected"):
                missing = deepcopy(valid)
                del missing[field]
                invalid.append((missing, error))
        for field in ("harness", "session_id", "message_id", "quote"):
            for value in (None, "", " \t", 1):
                case = deepcopy(valid)
                case["expected"][0][field] = value
                invalid.append((case, "nonempty " + field))
        case = deepcopy(valid)
        case["expected"][0]["source"]["path"] = "relative.sqlite"
        invalid.append((case, "explicit absolute paths"))
        for case, error in invalid:
            path.write_text(json.dumps([case]), encoding="utf-8")
            with _rejects(error):
                benchmark.load_cases([path])


def test_load_cases_rejects_duplicate_ids_within_and_across_files():
    with tempfile.TemporaryDirectory() as tmp:
        case = _case(Path(tmp) / "unused.sqlite")
        first, second = Path(tmp) / "first.json", Path(tmp) / "second.json"
        first.write_text(json.dumps([case, case]), encoding="utf-8")
        with _rejects("unique"):
            benchmark.load_cases([first])
        for path in (first, second):
            path.write_text(json.dumps([case]), encoding="utf-8")
        with _rejects("unique"):
            benchmark.load_cases([first, second])


def test_verify_sources_opencode_exact_quote_roles_ordinal_and_read_only():
    for role in ("user", "assistant"):
        with _opencode_source(role=role) as case:
            path = Path(case["expected"][0]["source"]["path"])
            before = path.read_bytes()
            assert benchmark.verify_sources([case]) == 1
            assert path.read_bytes() == before
            del case["expected"][0]["source"]["message_ordinal"]
            assert benchmark.verify_sources([case]) == 1


def test_verify_sources_opencode_rejects_wrong_part_message_session_and_file():
    with _opencode_source() as valid, _opencode_source() as other:
        for field, value in (
            ("message_id", "message-1"),
            ("message_id", "missing"),
            ("session_id", "other-session"),
        ):
            case = deepcopy(valid)
            case["expected"][0][field] = value
            with _rejects("missing or mismatched source record"):
                benchmark.verify_sources([case])
        case = deepcopy(valid)
        case["expected"][0]["source"]["part_id"] = "part-1"
        with _rejects("missing or mismatched source record"):
            benchmark.verify_sources([case])
        other_path = Path(other["expected"][0]["source"]["path"])
        connection = sqlite3.connect(other_path)
        try:
            connection.execute("DELETE FROM part WHERE id = 'part-2'")
            connection.commit()
        finally:
            connection.close()
        case = deepcopy(valid)
        case["expected"][0]["source"]["path"] = str(other_path)
        with _rejects("missing or mismatched source record"):
            benchmark.verify_sources([case])


def test_verify_sources_opencode_rejects_nontext_synthetic_and_nonconversation_roles():
    for options, error in (
        ({"part_type": "tool"}, "recorded text"),
        ({"part_type": "reasoning"}, "recorded text"),
        ({"synthetic": True}, "recorded text"),
        ({"role": "system"}, "unsupported source role"),
        ({"role": "tool"}, "unsupported source role"),
    ):
        with _opencode_source(**options) as case, _rejects(error):
            benchmark.verify_sources([case])


def test_verify_sources_opencode_rejects_changed_ordinal_and_inexact_quote():
    with _opencode_source() as valid:
        for ordinal in (0, 1, 3):
            case = deepcopy(valid)
            case["expected"][0]["source"]["message_ordinal"] = ordinal
            with _rejects("ordinal changed"):
                benchmark.verify_sources([case])
        for quote in ("Selected pear", "selected  pear", "No fruit yet."):
            case = deepcopy(valid)
            case["expected"][0]["quote"] = quote
            with _rejects("quote no longer matches"):
                benchmark.verify_sources([case])


def test_verify_sources_jsonl_accepts_claude_ids_and_codex_line_identity():
    for harness in ("claude", "codex"):
        for role in ("user", "assistant"):
            with _jsonl_source(harness, role=role) as case:
                path = Path(case["expected"][0]["source"]["path"])
                before = path.read_bytes()
                assert benchmark.verify_sources([case]) == 1
                assert path.read_bytes() == before
                if harness == "claude":
                    for message_id in ("uuid-2", "line:1"):
                        case["expected"][0]["message_id"] = message_id
                        assert benchmark.verify_sources([case]) == 1


def test_verify_sources_jsonl_accepts_string_and_typed_text_content():
    for harness in ("claude", "codex"):
        for content in (
            "We selected pear.",
            [{"type": "input_text", "text": "We selected pear."}],
            [{"type": "text", "text": "We selected"}, {"type": "output_text", "text": "pear."}],
        ):
            with _jsonl_source(harness, content=content) as case:
                if isinstance(content, list) and len(content) == 2:
                    case["expected"][0]["quote"] = "selected\npear"
                assert benchmark.verify_sources([case]) == 1


def test_verify_sources_jsonl_rejects_mismatched_identity_line_role_and_quote():
    for harness in ("claude", "codex"):
        with _jsonl_source(harness) as valid:
            for field, value in (("session_id", "wrong-session"), ("message_id", "wrong-message")):
                case = deepcopy(valid)
                case["expected"][0][field] = value
                with _rejects("identity mismatch"):
                    benchmark.verify_sources([case])
            case = deepcopy(valid)
            case["expected"][0]["source"]["line"] = 99
            with _rejects("source line missing"):
                benchmark.verify_sources([case])
            case = deepcopy(valid)
            case["expected"][0]["quote"] = "selected apple"
            with _rejects("quote no longer matches"):
                benchmark.verify_sources([case])
        with _jsonl_source(harness, role="tool") as case, _rejects("conversation text"):
            benchmark.verify_sources([case])
        with _jsonl_source(
            harness, content=[{"type": "tool_result", "text": "selected pear"}]
        ) as case:
            with _rejects("quote no longer matches"):
                benchmark.verify_sources([case])


def test_verify_sources_counts_anchors_across_harnesses_and_rejects_unknown_harness():
    with _opencode_source() as opencode, _jsonl_source("claude") as claude, _jsonl_source(
        "codex"
    ) as codex:
        assert benchmark.verify_sources([opencode, claude, codex]) == 3
        opencode["expected"][0]["harness"] = "unsupported"
        with _rejects("not implemented for unsupported"):
            benchmark.verify_sources([opencode])


def test_score_separates_session_hits_from_attributed_evidence():
    cases = [_case("unused")]
    for hit in (
        _hit(text="unrelated passage"),
        _hit(message_id="wrong-message"),
        _hit(passages=[]),
        _hit(text="Selected pear"),
    ):
        report = benchmark.score(cases, _response(cases, hits=[_hit("distractor"), hit]))
        row = report["cases"][0]
        summary = report["summaries"]["recall"]["all"]
        assert row["session_rank"] == 2 and row["evidence_rank"] is None
        assert summary["session_hit_at_5"] == 1
        assert summary["evidence_hit_at_5"] == 0
        assert summary["mrr_at_5"] == 0.5
    report = benchmark.score(cases, _response(cases, hits=[_hit("distractor"), _hit()]))
    assert report["cases"][0]["evidence_rank"] == 2
    assert report["summaries"]["recall"]["all"]["evidence_hit_at_5"] == 1


def test_score_requires_matching_harness_and_session_for_evidence():
    cases = [_case("unused")]
    for hit in (_hit("wrong-session"), _hit(harness="claude")):
        row = benchmark.score(cases, _response(cases, hits=[hit]))["cases"][0]
        assert row["session_rank"] is None and row["evidence_rank"] is None


def test_score_session_and_evidence_can_have_different_ranks():
    case = _case("unused")
    second = {**case["expected"][0], "session_id": "session-2"}
    case["expected"].append(second)
    report = benchmark.score(
        [case], _response([case], hits=[_hit(text="unrelated"), _hit("session-2")])
    )
    assert report["cases"][0]["session_rank"] == 1
    assert report["cases"][0]["evidence_rank"] == 2


def test_score_k_cutoff_limits_hits_reads_errors_and_candidates():
    cases = [_case("unused")]
    hits = [_hit("distractor-" + str(index), read_text="x") for index in range(5)]
    hits.append(_hit(read_text="excluded", error="read failed"))
    response = _response(cases, hits=hits, lookup_text="q")
    report = benchmark.score(cases, response)
    row = report["cases"][0]
    assert report["k"] == 5
    assert row["session_rank"] is None and row["evidence_rank"] is None
    assert row["returned_chars"] == 6 and row["read_errors"] == 0
    assert row["candidate_ids"] == [["opencode", "distractor-" + str(index)] for index in range(5)]
    for k in (-1, 0, 1, 6):
        with _rejects("fixes k at 5"):
            benchmark.score(cases, response, k=k)


def test_score_unsupported_cases_count_in_all_but_not_supported():
    cases = [_case("unused"), _case("unused", "claude", "case-2")]
    response = _response(cases)
    response["observations"][0]["hits"] = [_hit()]
    response["observations"][1]["status"] = "unsupported"
    report = benchmark.score(cases, response)
    groups = report["summaries"]["recall"]
    assert groups["all"]["cases"] == 2 and groups["supported"]["cases"] == 1
    assert groups["all"]["session_hit_at_5"] == 0.5
    assert groups["all"]["evidence_hit_at_5"] == 0.5
    assert groups["supported"]["evidence_hit_at_5"] == 1
    assert groups["tag:synthetic"]["cases"] == 2
    assert groups["all"]["lookup_errors"] == 0
    assert report["cases"][1]["supported"] is False
    assert report["cases"][1]["oracle_evidence"] is None
    response["supported_harnesses"] = []
    report = benchmark.score(cases, response)
    assert report["summaries"]["recall"]["all"]["cases"] == 2
    assert "supported" not in report["summaries"]["recall"]


def test_score_counts_lookup_read_and_oracle_errors_separately():
    cases = [_case("unused"), _case("unused", case_id="case-2")]
    response = _response(cases)
    response["observations"][0].update(
        status="error",
        hits=[_hit("a", error="failed"), _hit("b", error=""), _hit("c", error=True)],
        oracle=[_hit(error="oracle failed"), _hit("d", error=None)],
    )
    response["observations"][1]["hits"] = [_hit("e", error="failed")]
    report = benchmark.score(cases, response)
    assert report["cases"][0]["read_errors"] == 2
    assert report["cases"][0]["oracle_errors"] == 1
    summary = report["summaries"]["recall"]["all"]
    assert summary["lookup_errors"] == 1 and summary["read_errors"] == 3


def test_score_duplicate_tags_never_weight_a_case_twice():
    cases = [_case("unused"), _case("unused", case_id="case-2")]
    cases[0]["tags"] = ["synthetic", "synthetic"]
    response = _response(cases)
    response["observations"][0]["hits"] = [_hit()]
    group = benchmark.score(cases, response)["summaries"]["recall"]["tag:synthetic"]
    assert group["cases"] == 2 and group["evidence_hit_at_5"] == 0.5


def test_score_counts_logical_unicode_chars_and_utf8_bytes_without_oracle():
    cases = [_case("unused")]
    # Escapes keep the fixture ASCII while exercising multibyte and combining text.
    lookup, read = "\u00e9\U0001f350", "e\u0301\n"
    response = _response(
        cases,
        lookup_text=lookup,
        hits=[_hit("a", read_text=read), _hit("b", read_text=read)],
        oracle=[_hit(read_text="\U0001f350" * 100)],
        elapsed_ms=12.5,
    )
    report = benchmark.score(cases, response)
    row = report["cases"][0]
    assert row["returned_chars"] == 8
    assert row["returned_utf8_bytes"] == 14
    assert row["elapsed_ms"] == 12.5
    assert report["summaries"]["recall"]["all"]["mean_returned_chars"] == 8


def test_score_oracle_evidence_does_not_inflate_retrieval_scores_or_budget():
    cases = [_case("unused")]
    response = _response(cases, lookup_text="miss", oracle=[_hit()])
    report = benchmark.score(cases, response)
    row = report["cases"][0]
    assert row["oracle_evidence"] is True
    assert row["session_rank"] is None and row["evidence_rank"] is None
    assert row["candidate_ids"] == []
    assert row["returned_chars"] == row["returned_utf8_bytes"] == 4
    summary = report["summaries"]["recall"]["all"]
    assert summary["session_hit_at_5"] == summary["evidence_hit_at_5"] == summary["mrr_at_5"] == 0
    response["observations"][0]["oracle"] = [_hit(message_id="wrong-message")]
    assert benchmark.score(cases, response)["cases"][0]["oracle_evidence"] is False


def test_score_requires_exactly_one_observation_per_case_and_profile():
    cases = [_case("unused"), _case("unused", case_id="case-2")]
    valid = _response(cases)
    valid["profiles"].append("search")
    valid["observations"] += [{**row, "profile": "search"} for row in valid["observations"]]
    assert len(benchmark.score(cases, valid)["cases"]) == 4
    response = deepcopy(valid)
    response["observations"].pop()
    with _rejects("exactly one observation"):
        benchmark.score(cases, response)
    for field, value in (("profile", "unknown-profile"), ("case_id", "unknown-case")):
        response = deepcopy(valid)
        response["observations"][0][field] = value
        with _rejects("exactly one observation"):
            benchmark.score(cases, response)
    response = deepcopy(valid)
    response["observations"].append(deepcopy(response["observations"][0]))
    with _rejects("Duplicate adapter observation"):
        benchmark.score(cases, response)


def test_score_rejects_empty_duplicate_profiles_and_duplicate_sessions():
    cases = [_case("unused")]
    for profiles in ([], ["recall", "recall"]):
        response = _response(cases)
        response["profiles"] = profiles
        with _rejects("profiles must be nonempty and unique"):
            benchmark.score(cases, response)
    with _rejects("duplicate sessions"):
        benchmark.score(cases, _response(cases, hits=[_hit(), _hit()]))
    report = benchmark.score(cases, _response(cases, hits=[_hit(), _hit(harness="claude")]))
    assert len(report["cases"][0]["candidate_ids"]) == 2
