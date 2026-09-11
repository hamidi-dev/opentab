#!/usr/bin/env python3
"""Developer-only, evidence-grounded recall evaluation. Never writes source records."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def load_cases(paths):
    cases = []
    seen = set()
    for path in paths:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(values, list):
            raise ValueError("Case files must contain JSON arrays")
        for case in values:
            if not isinstance(case, dict):
                raise ValueError("Cases must be JSON objects")
            if not isinstance(case.get("id"), str) or not case["id"] or case["id"] in seen:
                raise ValueError("Case IDs must be nonempty and unique")
            seen.add(case["id"])
            if not isinstance(case.get("question"), str) or not case["question"].strip():
                raise ValueError("Every case needs a question")
            if (
                not isinstance(case.get("tags"), list)
                or not case["tags"]
                or not all(isinstance(tag, str) and tag for tag in case["tags"])
            ):
                raise ValueError("Every case needs tags")
            if len(set(case["tags"])) != len(case["tags"]):
                raise ValueError("Case tags must be unique")
            if not case.get("expected"):
                raise ValueError("Every case needs at least one verified evidence anchor")
            for anchor in case["expected"]:
                for key in ("harness", "session_id", "message_id", "quote"):
                    if not isinstance(anchor.get(key), str) or not anchor[key].strip():
                        raise ValueError("Evidence anchors need nonempty " + key)
                source = Path(anchor["source"]["path"])
                if not source.is_absolute():
                    raise ValueError("Sources must be explicit absolute paths")
            cases.append(case)
    if not cases:
        raise ValueError("Empty evaluation set")
    return cases


def verify_sources(cases):
    """Recheck gold quotes against exact source records, not search results."""
    verified = 0
    for case in cases:
        for anchor in case["expected"]:
            source = anchor["source"]
            path = Path(source["path"])
            if anchor["harness"] == "opencode":
                connection = sqlite3.connect(path.as_uri() + "?mode=ro")
                try:
                    row = connection.execute(
                        "SELECT p.data, m.data, m.session_id FROM part p "
                        "JOIN message m ON m.id = p.message_id "
                        "WHERE p.id = ? AND m.id = ?",
                        (source["part_id"], anchor["message_id"]),
                    ).fetchone()
                    if not row or row[2] != anchor["session_id"]:
                        raise ValueError(case["id"] + ": missing or mismatched source record")
                    part, message = json.loads(row[0]), json.loads(row[1])
                    if part.get("type") != "text" or part.get("synthetic"):
                        raise ValueError(case["id"] + ": evidence must be recorded text")
                    if message.get("role") not in ("user", "assistant"):
                        raise ValueError(case["id"] + ": unsupported source role")
                    text = part.get("text", "")
                    if "message_ordinal" in source:
                        ids = [
                            item[0]
                            for item in connection.execute(
                                "SELECT id FROM message WHERE session_id = ? ORDER BY time_created, id",
                                (anchor["session_id"],),
                            )
                        ]
                        if ids.index(anchor["message_id"]) + 1 != source["message_ordinal"]:
                            raise ValueError(case["id"] + ": source message ordinal changed")
                finally:
                    connection.close()
            elif anchor["harness"] in ("claude", "codex"):
                text = None
                session_id = None
                with path.open(encoding="utf-8") as stream:
                    for number, line in enumerate(stream, 1):
                        record = json.loads(line)
                        if record.get("type") == "session_meta":
                            session_id = record.get("payload", {}).get("id")
                        if number != source["line"]:
                            continue
                        session_id = record.get("sessionId", session_id)
                        body = record.get("message", record.get("payload", {}))
                        ids = (
                            body.get("id"),
                            record.get("uuid"),
                            record.get("id"),
                            "line:" + str(number),
                        )
                        if session_id != anchor["session_id"] or anchor["message_id"] not in ids:
                            raise ValueError(case["id"] + ": source identity mismatch")
                        if body.get("role") not in ("user", "assistant"):
                            raise ValueError(case["id"] + ": evidence must be conversation text")
                        content = body.get("content", [])
                        text = (
                            content
                            if isinstance(content, str)
                            else "\n".join(
                                part.get("text", "")
                                for part in content
                                if part.get("type") in ("text", "input_text", "output_text")
                            )
                        )
                        break
                if text is None:
                    raise ValueError(case["id"] + ": source line missing")
            else:
                raise ValueError("Source verification not implemented for " + anchor["harness"])
            if anchor["quote"] not in text:
                raise ValueError(case["id"] + ": evidence quote no longer matches source")
            verified += 1
    return verified


def has_evidence(anchor, hit):
    if hit.get("error"):
        return False
    if (anchor["harness"], anchor["session_id"]) != (hit["harness"], hit["session_id"]):
        return False
    # A right session alone is not an answer. The anchor must be in an attributed passage.
    return any(
        passage["message_id"] == anchor["message_id"] and anchor["quote"] in passage["text"]
        for passage in hit.get("passages", [])
    )


def score(cases, response, k=5):
    if k != 5:
        raise ValueError("This baseline fixes k at 5")
    supported = set(response["supported_harnesses"])
    profiles = response["profiles"]
    if not profiles or len(set(profiles)) != len(profiles):
        raise ValueError("Adapter profiles must be nonempty and unique")
    observations = {}
    for row in response["observations"]:
        key = (row["profile"], row["case_id"])
        if key in observations:
            raise ValueError("Duplicate adapter observation")
        observations[key] = row
    expected_keys = {(profile, case["id"]) for profile in profiles for case in cases}
    if set(observations) != expected_keys:
        raise ValueError("Adapter must return exactly one observation per case/profile")
    scored = []
    for profile in profiles:
        for case in cases:
            row = observations[profile, case["id"]]
            eligible = any(a["harness"] in supported for a in case["expected"])
            hits = row.get("hits", [])[:k]
            keys = [(hit["harness"], hit["session_id"]) for hit in hits]
            if len(set(keys)) != len(keys):
                raise ValueError("Adapter returned duplicate sessions")
            rank = evidence_rank = None
            for index, hit in enumerate(hits, 1):
                for anchor in case["expected"]:
                    if (anchor["harness"], anchor["session_id"]) == keys[index - 1]:
                        rank = rank or index
                    if has_evidence(anchor, hit):
                        evidence_rank = evidence_rank or index
            oracle = row.get("oracle", [])
            # These are logical returned characters, even when a reader caches repeated calls.
            texts = [row.get("lookup_text", "")] + [hit.get("read_text", "") for hit in hits]
            scored.append(
                {
                    "id": case["id"],
                    "profile": profile,
                    "tags": case["tags"],
                    "supported": eligible,
                    "status": row["status"],
                    "lookup_error": row.get("lookup_error"),
                    "session_rank": rank,
                    "evidence_rank": evidence_rank,
                    "oracle_evidence": any(
                        has_evidence(a, hit) for a in case["expected"] for hit in oracle
                    )
                    if eligible
                    else None,
                    "returned_chars": sum(len(text) for text in texts),
                    "returned_utf8_bytes": sum(len(text.encode("utf-8")) for text in texts),
                    "elapsed_ms": row.get("elapsed_ms"),
                    "read_errors": sum(bool(hit.get("error")) for hit in hits),
                    "oracle_errors": sum(bool(hit.get("error")) for hit in oracle),
                    "read_error_kinds": [hit["error"] for hit in hits if hit.get("error")],
                    "oracle_error_kinds": [hit["error"] for hit in oracle if hit.get("error")],
                    "candidate_ids": [list(key) for key in keys],
                }
            )
    summaries = {}
    for profile in profiles:
        groups = defaultdict(list)
        for row in scored:
            if row["profile"] != profile:
                continue
            groups["all"].append(row)
            if row["supported"]:
                groups["supported"].append(row)
            for tag in sorted(set(row["tags"])):
                groups["tag:" + tag].append(row)
        summaries[profile] = {}
        for name, rows in groups.items():
            count = len(rows)
            summaries[profile][name] = {
                "cases": count,
                "session_hit_at_5": sum(r["session_rank"] is not None for r in rows) / count,
                "evidence_hit_at_5": sum(r["evidence_rank"] is not None for r in rows) / count,
                "mrr_at_5": sum(1 / r["session_rank"] if r["session_rank"] else 0 for r in rows)
                / count,
                "mean_returned_chars": sum(r["returned_chars"] for r in rows) / count,
                "lookup_errors": sum(r["status"] == "error" for r in rows),
                "read_errors": sum(r["read_errors"] for r in rows),
                "oracle_evidence_cases": sum(r["oracle_evidence"] is True for r in rows),
                "oracle_errors": sum(r["oracle_errors"] for r in rows),
            }
    return {"k": k, "summaries": summaries, "cases": scored}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "run"))
    parser.add_argument("--cases", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--adapter", nargs=argparse.REMAINDER, help="Trusted local executable; must be last"
    )
    args = parser.parse_args()
    cases = load_cases(args.cases)
    anchors = verify_sources(cases)
    if args.action == "verify":
        print(f"Verified {anchors} source anchors across {len(cases)} cases")
        return
    if not args.adapter or not args.output:
        parser.error("run requires --output and --adapter")
    # Refuse accidental overwrites; explicit output is private, even without raw passages.
    if args.output.exists() or not args.output.parent.is_dir():
        parser.error("output must be a new file in an existing private directory")
    result = subprocess.run(
        args.adapter,
        input=json.dumps({"cases": cases, "k": 5}),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=args.timeout,
        check=False,
    )
    if result.returncode:
        # Do not dump potentially private adapter stderr into a terminal/CI log.
        raise RuntimeError(f"Adapter failed (exit {result.returncode}); output was not saved")
    response = json.loads(result.stdout)
    report = score(cases, response)
    report.update(
        schema_version=1,
        generated_at=datetime.now(timezone.utc).isoformat(),
        dataset_sha256=hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        verified_anchors=anchors,
        adapter=response.get("metadata", {}),
    )
    verify_sources(cases)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
    for profile, groups in report["summaries"].items():
        for scope in ("all", "supported"):
            if scope not in groups:
                continue
            group = groups[scope]
            print(
                f"{profile}/{scope}: n={group['cases']}, "
                f"session@5={group['session_hit_at_5']:.1%}, "
                f"evidence@5={group['evidence_hit_at_5']:.1%}, "
                f"MRR@5={group['mrr_at_5']:.3f}, "
                f"mean chars={group['mean_returned_chars']:.0f}"
            )


if __name__ == "__main__":
    main()
