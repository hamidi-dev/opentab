"""Per-session *worked* time: the agent's active bursts with the idle waits (you
composing the next prompt) removed -- the helper, every backend that can measure it,
the backends that honestly can't, and the sort/column/detail/web that surface it."""

import json
import os
import sqlite3
import tempfile

import opentab as ot
from opentab.formatting import iso_to_epoch, worked_seconds

from tests._support import (
    _claude_msg,
    _hermes_db_full,
    _usage,
    _write_csv,
    _write_jsonl,
    _write_opencode_db_with_turns,
    app_with,
    box_cells,
    workflow,
)


def _args():
    return type("Args", (), {"demo": False})()


# --- the helper ---------------------------------------------------------------------


def test_worked_seconds_drops_the_gap_into_each_human_prompt():
    # prompt@0, agent works to 40 and 45, you think for 12 min, follow-up@765,
    # agent works to 855. Worked = (40) + (5) + (90) = 135; the 720s wait is gone.
    events = [0, 40, 45, 765, 855]
    prompts = [0, 765]
    assert worked_seconds(events, prompts) == 135.0


def test_worked_seconds_starts_a_fresh_burst_after_30_minutes_of_silence():
    # Resume metadata can be the first event after a session sat untouched for days.
    # With no evidence of activity in between, that silence is not worked time. Exactly
    # 30 minutes remains part of the burst; only a gap beyond the limit is dropped.
    assert worked_seconds([0, 60, 1_860, 3_661, 3_721], [0]) == 1_920.0


def test_worked_seconds_unknown_below_two_points_and_dupe_safe():
    assert worked_seconds([], []) is None
    assert worked_seconds([5], [5]) is None  # a single activity point measures nothing
    assert worked_seconds([0, 0, 40, 40], [0]) == 40.0  # replayed dupes are 0-gaps
    # No prompts to bound idle -> every gap counts (a caller with no human turns must
    # instead leave worked unknown; that policy lives in the backends, not here).
    assert worked_seconds([0, 100, 300], []) == 300.0


def test_worked_seconds_drops_non_finite_epochs():
    # A stray inf/nan from a malformed stamp is dropped, never propagated -- an inf
    # worked would crash human_duration's int() at render time.
    assert worked_seconds([0.0, float("inf"), 60.0], [0.0]) == 60.0
    assert worked_seconds([float("nan"), 10.0], []) is None  # only one finite point left


def test_iso_to_epoch_is_absolute_and_difference_stable():
    # Parses the "...Z"/millis/naive ISO forms to a tz-absolute epoch; only differences
    # are used downstream, so the naive-read-as-UTC offset never matters.
    assert iso_to_epoch("2026-06-10T19:30:00.000Z") - iso_to_epoch("2026-06-10T18:00:00Z") == 5400
    assert iso_to_epoch("") is None and iso_to_epoch("nope") is None


# --- the model ----------------------------------------------------------------------


def test_worked_seconds_defaults_to_unknown():
    w = workflow("a", "2026-06-01 12:00:00")
    assert w.worked_seconds is None  # a bare row measures nothing until a backend fills it


# --- backends that CAN measure it ---------------------------------------------------


def _claude_user(session, text, *, uuid, cwd, ts, parent=None):
    return {
        "type": "user",
        "sessionId": session,
        "cwd": cwd,
        "timestamp": ts,
        "uuid": uuid,
        "parentUuid": parent,
        "message": {"role": "user", "content": text},
    }


def test_claude_worked_excludes_the_wait_for_a_follow_up():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        rows = [
            _claude_user("s1", "start", uuid="u0", cwd="/tmp/p", ts="2026-06-10T18:00:00.000Z"),
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(100, 50),
                uuid="a1",
                cwd="/tmp/p",
                parent="u0",
                ts="2026-06-10T18:00:40.000Z",
            ),
            # 12 min of you reading/typing -- not work
            _claude_user(
                "s1",
                "now do Y",
                uuid="u1",
                cwd="/tmp/p",
                parent="a1",
                ts="2026-06-10T18:12:40.000Z",
            ),
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(40, 10),
                uuid="a2",
                cwd="/tmp/p",
                parent="u1",
                ts="2026-06-10T18:14:10.000Z",
            ),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), list(reversed(rows)))  # order-proof
        (w,) = ot.ClaudeStore(os.path.join(tmp, "projects"), _args()).workflows()
        assert w.worked_seconds == 130.0  # 40s + 90s, NOT the 850s elapsed span


def test_claude_user_controls_and_attachments_start_fresh_work_bursts():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        rows = [
            _claude_user("s1", "start", uuid="u0", cwd="/tmp/p", ts="2026-06-10T18:00:00Z"),
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(100, 50),
                uuid="a1",
                cwd="/tmp/p",
                parent="u0",
                ts="2026-06-10T18:01:00Z",
            ),
            {
                "type": "user",
                "sessionId": "s1",
                "cwd": "/tmp/p",
                "timestamp": "2026-06-10T18:11:00Z",
                "uuid": "compact",
                "message": {"role": "user", "content": "<command-name>/compact</command-name>"},
            },
            {
                "type": "system",
                "subtype": "local_command",
                "sessionId": "s1",
                "cwd": "/tmp/p",
                "timestamp": "2026-06-10T18:21:00Z",
                "uuid": "model",
                "content": "<command-name>/model</command-name>",
            },
            {
                "type": "attachment",
                "sessionId": "s1",
                "cwd": "/tmp/p",
                "timestamp": "2026-06-10T18:31:00Z",
                "uuid": "image",
            },
            {
                "type": "system",
                "subtype": "away_summary",
                "sessionId": "s1",
                "cwd": "/tmp/p",
                "timestamp": "2026-06-10T18:41:00Z",
                "uuid": "away",
                "content": "Summary after returning",
            },
            # A sidechain control is agent-authored activity, not the human returning.
            {
                "type": "system",
                "subtype": "local_command",
                "sessionId": "s1",
                "cwd": "/tmp/p",
                "timestamp": "2026-06-10T18:51:00Z",
                "uuid": "side-control",
                "isSidechain": True,
                "content": "<command-name>/model</command-name>",
            },
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(40, 10),
                uuid="a2",
                cwd="/tmp/p",
                parent="image",
                ts="2026-06-10T18:52:00Z",
            ),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows)
        (w,) = ot.ClaudeStore(os.path.join(tmp, "projects"), _args()).workflows()
        assert w.worked_seconds == 720.0


def _codex_user(text, ts):
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text, "kind": "plain"},
    }


def test_codex_worked_excludes_the_wait_between_prompts():
    from tests._support import _codex_meta, _codex_tokens, _codex_turn

    sid = "0199aa8e-1b9e-7912-bcd4-9b00c8733ea6"
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(sid, cwd, ts="2025-10-03T14:51:03.000Z"),
            _codex_user("do X", "2025-10-03T14:51:05.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:51:06.000Z"),
            _codex_tokens(1000, 100, 800, 1100, ts="2025-10-03T14:51:10.000Z"),
            # 4 min wait for the human's next prompt
            _codex_user("then Y", "2025-10-03T14:55:10.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:55:11.000Z"),
            _codex_tokens(2200, 160, 1700, 2360, ts="2025-10-03T14:55:20.000Z"),
        ]
        _write_jsonl(os.path.join(root, f"rollout-2025-10-03T16-51-03-{sid}.jsonl"), rows)
        (w,) = ot.CodexStore(root, _args()).workflows()
        # 03->05 (into a prompt) and 10->55:10 (the wait) drop; 1+4 + 1+9 = 15s remain.
        assert w.worked_seconds == 15.0


def test_opencode_worked_spans_the_subtree_and_drops_the_wait():
    # The shared turns DB: root s1 (m1@1.0s, m2@2.0s) + subagent s2 (m3@1.5s), with
    # user prompts u1@0.5s and u2@1.8s. Worked walks the whole tree in time order and
    # drops only the 0.3s gap into u2: (1.0-0.5)+(1.5-1.0)+(2.0-1.8) = 1.2s.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        store = ot.Store(db, _args())
        (w,) = store.workflows()
        assert w.worked_seconds == 1.2
        assert w.subagents == 1  # the subagent's own turn was folded into the span


def _opencode_db(db, messages):
    # messages: (id, session_id, role, time_ms). role 'assistant' rows carry tokens so
    # the session has usage; 'user' rows are prompts. s2 is a subagent child of s1.
    conn = sqlite3.connect(db)
    conn.executescript(
        "create table session (id text primary key, parent_id text, title text,"
        " directory text, agent text, time_created integer);"
        "create table message (id text primary key, session_id text, data text);"
        "create table part (id text primary key, message_id text, session_id text, data text);"
    )
    conn.executemany(
        "insert into session values (?,?,?,?,?,?)",
        [("s1", None, "Root", "/p", None, 0), ("s2", "s1", "Sub", "/p", "explore", 0)],
    )
    for mid, sess, role, t in messages:
        if role == "assistant":
            data = json.dumps(
                {
                    "role": "assistant",
                    "providerID": "anthropic",
                    "modelID": "claude-opus-4-5",
                    "cost": 0,
                    "time": {"created": t},
                    "tokens": {"input": 1000, "output": 0},
                }
            )
        else:
            data = json.dumps({"role": "user", "time": {"created": t}, "summary": {"title": "t"}})
        conn.execute("insert into message values (?,?,?)", (mid, sess, data))
    conn.commit()
    conn.close()


def test_opencode_subagent_task_message_counts_as_work_not_a_wait():
    # A subagent's `user` message is the agent-authored task, NOT a human turn, so the
    # gap into it is work. Only the root's u2@100s is a wait. root-u@0, root-a@10,
    # sub-u(task)@20, sub-a@30, root-u@100, root-a@110 -> 10+10+10 + (drop 70) + 10 = 40.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _opencode_db(
            db,
            [
                ("m1", "s1", "user", 0),
                ("m2", "s1", "assistant", 10_000),
                ("m3", "s2", "user", 20_000),
                ("m4", "s2", "assistant", 30_000),
                ("m5", "s1", "user", 100_000),
                ("m6", "s1", "assistant", 110_000),
            ],
        )
        (w,) = ot.Store(db, _args()).workflows()
        assert w.worked_seconds == 40.0  # the sub-task gap (10->20) is NOT dropped


def test_opencode_worked_starts_a_fresh_burst_after_30_minutes_of_silence():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _opencode_db(
            db,
            [
                ("m1", "s1", "user", 0),
                ("m2", "s1", "assistant", 60_000),
                ("m3", "s1", "assistant", 1_860_000),
                ("m4", "s1", "assistant", 3_661_000),
                ("m5", "s1", "assistant", 3_721_000),
            ],
        )
        (w,) = ot.Store(db, _args()).workflows()
        assert w.worked_seconds == 1_920.0


def test_hermes_worked_from_message_roles_drops_the_wait():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db_full(db, [{"id": "s1", "started_at": 1_750_000_000.0, "inp": 100}])
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE messages (session_id TEXT, timestamp REAL, role TEXT, content TEXT)"
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?)",
            [
                ("s1", 1_750_000_000.0, "user", "hi"),
                ("s1", 1_750_000_060.0, "assistant", "…"),  # +60s work
                ("s1", 1_750_000_660.0, "user", "more"),  # +10min wait
                ("s1", 1_750_000_720.0, "assistant", "…"),  # +60s work
            ],
        )
        conn.commit()
        conn.close()
        (w,) = ot.HermesStore(db, _args()).workflows()
        assert w.worked_seconds == 120.0  # 60 + 60, the 600s wait excluded


def test_hermes_subagent_task_message_counts_as_work_not_a_wait():
    # A subagent (child) session's `user` message is the agent's task, not a human
    # wait -- the gap into it is work. root-u@0, root-a@10, sub-u(task)@20, sub-a@30.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db_full(
            db,
            [
                {"id": "s1", "started_at": 1_750_000_000.0, "inp": 100},
                {"id": "s2", "parent_id": "s1", "started_at": 1_750_000_020.0, "inp": 50},
            ],
        )
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE messages (session_id TEXT, timestamp REAL, role TEXT, content TEXT)"
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?)",
            [
                ("s1", 1_750_000_000.0, "user", "human"),
                ("s1", 1_750_000_010.0, "assistant", "…"),
                ("s2", 1_750_000_020.0, "user", "agent-authored task"),
                ("s2", 1_750_000_030.0, "assistant", "…"),
            ],
        )
        conn.commit()
        conn.close()
        (w,) = ot.HermesStore(db, _args()).workflows()
        assert w.worked_seconds == 30.0  # every gap is work; the sub-task is not a wait


def test_csv_worked_uses_prompt_groups_to_find_the_idle():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "session", "prompt_id"],
            [
                ["2026-06-01T10:00:00", "gpt-5", 100, 10, "s1", "p1"],
                ["2026-06-01T10:00:30", "gpt-5", 100, 10, "s1", "p1"],  # same burst +30s
                ["2026-06-01T10:05:00", "gpt-5", 100, 10, "s1", "p2"],  # new prompt (wait before)
                ["2026-06-01T10:06:00", "gpt-5", 100, 10, "s1", "p2"],  # +60s
            ],
        )
        (w,) = ot.CsvStore(path, _args()).workflows()
        assert w.worked_seconds == 90.0  # 30 + 60; the 4.5-min gap into p2 dropped


def test_csv_blank_prompt_rows_are_not_human_turns():
    # Blank prompt cells interleaved with tagged ones: a blank row is a continuation
    # (a tool-loop retry), NOT a fresh human turn, so only the p1->p2 change bounds the
    # idle. Worked = 30 + 60 = 90; the blanks must not each open a group (that gave 0s).
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "session", "prompt_id"],
            [
                ["2026-06-01T10:00:00", "gpt-5", 100, 10, "s1", "p1"],
                ["2026-06-01T10:00:30", "gpt-5", 100, 10, "s1", ""],  # blank: same burst
                ["2026-06-01T10:05:00", "gpt-5", 100, 10, "s1", "p2"],  # new prompt (wait before)
                ["2026-06-01T10:06:00", "gpt-5", 100, 10, "s1", ""],  # blank: same burst
            ],
        )
        (w,) = ot.CsvStore(path, _args()).workflows()
        assert w.worked_seconds == 90.0


def test_csv_untimestamped_prompt_reports_unknown():
    # The only prompt row has no timestamp (dropped as an activity point), and the rest
    # are blank: no usable prompt boundary survives, so worked is unknown -- NOT the
    # full 1h span of the two blank rows.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "session", "prompt_id"],
            [
                ["", "gpt-5", 100, 10, "s1", "p1"],  # a prompt, but no clock on it
                ["2026-06-01T12:00:00", "gpt-5", 100, 10, "s1", ""],
                ["2026-06-01T13:00:00", "gpt-5", 100, 10, "s1", ""],
            ],
        )
        (w,) = ot.CsvStore(path, _args()).workflows()
        assert w.worked_seconds is None


def test_csv_unparseable_prompt_timestamp_reports_unknown():
    # The only prompt row has an UNPARSEABLE timestamp (not just empty): its epoch is
    # None, so no usable boundary survives and worked is unknown -- not the full span.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "session", "prompt_id"],
            [
                ["not-a-real-timestamp", "gpt-5", 100, 10, "s1", "p1"],
                ["2026-06-01T12:00:00", "gpt-5", 100, 10, "s1", ""],
                ["2026-06-01T13:00:00", "gpt-5", 100, 10, "s1", ""],
            ],
        )
        (w,) = ot.CsvStore(path, _args()).workflows()
        assert w.worked_seconds is None


def test_jsonl_numeric_zero_prompt_id_is_a_real_prompt():
    # prompt_id 0 (integer) is a present, stable id -- `or ""` must not drop it to blank,
    # which would leave a genuinely-prompted session unknown. Both rows share id 0.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-01T10:00:00",
                    "model": "gpt-5",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "session": "a",
                    "prompt_id": 0,
                },
                {
                    "timestamp": "2026-06-01T10:00:30",
                    "model": "gpt-5",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "session": "a",
                    "prompt_id": 0,
                },
            ],
        )
        (w,) = ot.JsonlStore(path, _args()).workflows()
        assert w.worked_seconds == 30.0


def test_csv_two_long_prompts_sharing_a_prefix_are_distinct_turns():
    # No prompt_id, so the group key is the prompt text -- but it must be the FULL text,
    # not the 160-char-capped display prompt. Two long prompts that share their first
    # 160 chars are still different human turns, so the 9.5-min gap between them is a
    # wait, not work: 30 + 30 = 60, not 630.
    pre = "x" * 200  # longer than the 160-char cap
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "session", "prompt"],
            [
                ["2026-06-01T10:00:00", "gpt-5", 100, 10, "s1", pre + "A"],
                ["2026-06-01T10:00:30", "gpt-5", 100, 10, "s1", pre + "A"],
                ["2026-06-01T10:10:00", "gpt-5", 100, 10, "s1", pre + "B"],  # distinct prompt
                ["2026-06-01T10:10:30", "gpt-5", 100, 10, "s1", pre + "B"],
            ],
        )
        (w,) = ot.CsvStore(path, _args()).workflows()
        assert w.worked_seconds == 60.0  # the 9.5-min wait between A and B is excluded


def test_csv_mixed_prompt_id_availability_keys_on_the_id_alone():
    # The session HAS prompt_ids, so the id is authoritative: a continuation row that
    # drops the id (but repeats the prompt text) must NOT fall back to the text and be
    # read as a new human turn. p1, blank, p2, blank -> boundaries at p1 and p2 only,
    # so the 9.5-min wait between them is excluded: 30 + 30 = 60, not 0.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.csv")
        _write_csv(
            path,
            [
                "timestamp",
                "model",
                "input_tokens",
                "output_tokens",
                "session",
                "prompt_id",
                "prompt",
            ],
            [
                ["2026-06-01T10:00:00", "gpt-5", 100, 10, "s1", "p1", "one"],
                ["2026-06-01T10:00:30", "gpt-5", 100, 10, "s1", "", "one"],  # continuation
                ["2026-06-01T10:10:00", "gpt-5", 100, 10, "s1", "p2", "two"],
                ["2026-06-01T10:10:30", "gpt-5", 100, 10, "s1", "", "two"],  # continuation
            ],
        )
        (w,) = ot.CsvStore(path, _args()).workflows()
        assert w.worked_seconds == 60.0


def test_csv_non_finite_timestamp_does_not_crash_or_poison_worked():
    # A malformed "inf"/"nan" timestamp must be rejected (like _parse_ts does), not
    # parsed as a float -- otherwise worked becomes inf and human_duration's int()
    # crashes the render. Here the only real boundary is p1@10:00, its partner is inf,
    # so no measurable second boundary survives -> unknown, and no crash.
    from opentab.formatting import human_duration

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "session", "prompt_id"],
            [
                ["2026-06-01T10:00:00Z", "gpt-5", 100, 10, "s1", "p1"],
                ["inf", "gpt-5", 100, 10, "s1", "p1"],
                ["nan", "gpt-5", 100, 10, "s1", "p1"],
            ],
        )
        (w,) = ot.CsvStore(path, _args()).workflows()
        assert w.worked_seconds is None  # the inf/nan rows are dropped; one boundary is not enough
        human_duration(w.worked_seconds or 0)  # render path must not raise


def test_csv_worked_survives_a_dst_fallback():
    # Two same-prompt requests 20 min apart across the Europe/Berlin autumn DST
    # fall-back (03:00 -> 02:00 local). Worked must be 20 min: the arithmetic uses the
    # ABSOLUTE epoch, not the offset-free local string (which reads the gap as 40 min).
    import time

    if not hasattr(time, "tzset"):
        return  # Windows has no tzset; the fix is platform-independent regardless
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "req.csv")
            _write_csv(
                path,
                ["timestamp", "model", "input_tokens", "output_tokens", "session", "prompt_id"],
                [
                    ["2026-10-25T00:50:00Z", "gpt-5", 100, 10, "s1", "p1"],
                    ["2026-10-25T01:10:00Z", "gpt-5", 100, 10, "s1", "p1"],
                ],
            )
            (w,) = ot.CsvStore(path, _args()).workflows()
            assert w.worked_seconds == 1200.0  # 20 min, not the 40 min a local round-trip gives
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_csv_worked_is_gated_per_session_not_per_file():
    # The file HAS a prompt_id column, but session s2's rows leave it blank: s2 can't
    # tell work from waiting even though its sibling s1 is fully tagged, so s2 is
    # unknown (not a full-span "worked") while s1 still measures.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "session", "prompt_id"],
            [
                ["2026-06-01T10:00:00", "gpt-5", 100, 10, "s1", "p1"],
                ["2026-06-01T10:00:30", "gpt-5", 100, 10, "s1", "p1"],
                ["2026-06-01T12:00:00", "gpt-5", 100, 10, "s2", ""],  # no prompt info
                ["2026-06-01T13:00:00", "gpt-5", 100, 10, "s2", ""],  # 1h apart
            ],
        )
        by_id = {w.id: w for w in ot.CsvStore(path, _args()).workflows()}
        assert by_id["s1"].worked_seconds == 30.0
        assert by_id["s2"].worked_seconds is None  # NOT 3600 -- blank beats a fake span


def test_jsonl_worked_is_gated_per_session_not_per_file():
    # Same per-session gate for the NDJSON twin: one session carries a prompt, the
    # other doesn't, and the promptless one stays unknown.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.jsonl")
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-06-01T10:00:00",
                    "model": "gpt-5",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "session": "a",
                    "prompt": "do the thing",
                },
                {
                    "timestamp": "2026-06-01T10:00:30",
                    "model": "gpt-5",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "session": "a",
                    "prompt": "do the thing",
                },
                {
                    "timestamp": "2026-06-01T12:00:00",
                    "model": "gpt-5",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "session": "b",
                },
                {
                    "timestamp": "2026-06-01T13:00:00",
                    "model": "gpt-5",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "session": "b",
                },
            ],
        )
        by_id = {w.id: w for w in ot.JsonlStore(path, _args()).workflows()}
        assert by_id["a"].worked_seconds == 30.0
        assert by_id["b"].worked_seconds is None


# --- backends that honestly CAN'T measure it -> unknown, never a fake span -----------


def test_csv_without_a_prompt_column_reports_unknown():
    # No prompt/prompt_id column: every row is an opaque API call, so work can't be
    # told from waiting. Blank beats reporting the elapsed-with-idle span as "worked".
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "req.csv")
        _write_csv(
            path,
            ["timestamp", "model", "input_tokens", "output_tokens", "session"],
            [
                ["2026-06-01T10:00:00", "gpt-5", 100, 10, "s1"],
                ["2026-06-01T10:45:00", "gpt-5", 100, 10, "s1"],
            ],
        )
        (w,) = ot.CsvStore(path, _args()).workflows()
        assert w.worked_seconds is None


def test_hermes_without_a_messages_table_reports_unknown():
    # An old/partial DB has no messages table: nothing records the per-message stream,
    # so worked (and the last-activity end) stay unknown rather than a fake 0s.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db_full(db, [{"id": "s1", "started_at": 1_750_000_000.0, "inp": 100}])
        (w,) = ot.HermesStore(db, _args()).workflows()
        assert w.worked_seconds is None and w.ended_at == ""


def test_opencode_without_per_message_times_reports_unknown():
    # A DB whose messages carry no $.time.created (the what-if fixture's shape): there
    # are activity rows but no clock on them, so worked can't be measured -> unknown.
    from tests._support import _whatif_app, _whatif_msg

    with tempfile.TemporaryDirectory() as tmp:
        sessions = [("solo", None, "Solo", "/tmp/p", 1760000000000, 1.0, 1000)]
        app = _whatif_app(
            tmp, sessions, [_whatif_msg("solo", "anthropic", "claude-opus-4-5", 1.0, 1000)]
        )
        (w,) = app.loaded
        assert w.worked_seconds is None


def test_old_exports_without_worked_still_load_as_unknown():
    # A machine summary written by an older opentab has no worked_seconds key: the row
    # deserializes with the default, and the UI shows nothing rather than 0s.
    from opentab.stores import remote

    row = {
        "id": "x",
        "title": "t",
        "directory": "/p",
        "created_at": "2026-06-01 12:00:00",
        "root_cost": 1.0,
        "total_cost": 1.0,
        "subagents": 0,
        "model_count": 1,
        "total_tokens": 10,
        "unpriced_tokens": 0,
    }
    clean = {k: v for k, v in row.items() if k in remote._WF_FIELDS}
    w = ot.Workflow(**clean)
    assert w.worked_seconds is None


# --- sort, column, detail, web ------------------------------------------------------


def _session_app():
    short = workflow("short", "2026-06-01 12:00:00", cost=9.0)
    short.worked_seconds = 300.0  # 5m
    short.ended_at = "2026-06-01 12:05:00"
    long = workflow("long", "2026-06-01 13:00:00", cost=1.0)
    long.worked_seconds = 7200.0  # 2h
    long.ended_at = "2026-06-01 15:00:00"
    unknown = workflow("unknown", "2026-06-01 14:00:00", cost=5.0)  # worked_seconds None
    app = app_with([short, long, unknown])
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    return app


def test_duration_sort_puts_the_hardest_worked_first_and_unknown_last():
    app = _session_app()
    app.sort_by = "duration"
    assert [w.id for w in app.current_sessions()] == ["long", "short", "unknown"]
    assert "duration" in app.sort_options  # reaches the s picker + header clicks


def test_session_list_shows_a_worked_column_blank_when_unknown():
    app = _session_app()
    rows = box_cells(app.renderer.session_table(app.current_sessions(), 120))
    assert "Worked" in rows[0]  # the header no longer says the ambiguous "Duration"
    by_id = {r.split()[-1]: r for r in rows[1:] if not r.strip().startswith("TOTAL")}
    assert "2h" in by_id["long"]
    assert "5m" in by_id["short"]
    assert "2h" not in by_id["unknown"] and "5m" not in by_id["unknown"]


def test_narrow_panes_drop_the_worked_column_before_the_title():
    app = _session_app()
    sessions = app.current_sessions()
    _models, _proj, wide = app.renderer.session_columns(sessions, 120)
    assert wide is True
    _models, _proj, narrow = app.renderer.session_columns(sessions, 46)
    assert narrow is False  # squeezed: the titles outrank the worked cell


def test_session_detail_says_how_long_the_agent_worked():
    app = _session_app()
    w = next(x for x in app.loaded if x.id == "long")
    assert app.renderer._worked_suffix(w) == "   · worked 2h (until 15:00)"
    # rstrip: a boxed card pads its rows out to the frame's inner width.
    started = [
        c.rstrip()
        for c in box_cells(app.renderer.detail_overview(w, 100))
        if c.startswith("Started")
    ]
    assert started == ["Started:  2026-06-01 13:00:00   · worked 2h (until 15:00)"]
    w.ended_at = "2026-06-02 01:30:00"  # ran past midnight: keep the date visible
    assert "until 2026-06-02 01:30" in app.renderer._worked_suffix(w)
    w.ended_at = ""  # a backend that knows worked but not the last stamp: drop the "until"
    assert app.renderer._worked_suffix(w) == "   · worked 2h"
    unknown = next(x for x in app.loaded if x.id == "unknown")
    assert app.renderer._worked_suffix(unknown) == ""


def test_web_payload_ships_worked_seconds():
    from opentab import web

    app = _session_app()
    app._ensure_models()
    payload = web.build_payload(app)
    by_id = {r["id"]: r for r in payload["workflows"]}
    assert by_id["long"]["dur"] == 7200.0
    assert by_id["unknown"]["dur"] is None
