"""The Codex CLI rollout backend: cumulative-delta tokens, spawned threads (stores/codex.py)."""

import json
import os
import tempfile

import opentab as ot
from opentab.formatting import iso_to_local

from tests._support import (
    FakeStore,
    _claude_msg,
    _codex_meta,
    _codex_tokens,
    _codex_turn,
    _empty_opencode_db,
    _usage,
    _write_jsonl,
    workflow,
)

# --- Codex CLI rollout helpers (~/.codex/sessions/**/rollout-*.jsonl) ---------
CODEX_SID = "0199aa8e-1b9e-7912-bcd4-9b00c8733ea6"


def _codex_user(text, ts="2025-10-03T14:51:05.000Z"):
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text, "kind": "plain"},
    }


def _codex_call(name, ts="2025-10-03T14:51:15.000Z", kind="function_call"):
    # A tool-call response_item; it belongs to the turn whose token_count follows.
    payload = {"type": kind, "call_id": "c1"}
    if name is not None:
        payload["name"] = name
    return {"timestamp": ts, "type": "response_item", "payload": payload}


def _codex_rollout(root, sid, rows):
    # Codex files are named rollout-<ts>-<uuid>.jsonl; the uuid is the session id.
    _write_jsonl(os.path.join(root, f"rollout-2025-10-03T16-51-03-{sid}.jsonl"), rows)


def test_codex_store_dedupes_echo_attributes_models_and_rolls_up_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions", "2025", "10", "03")
        os.makedirs(root)
        # cwd is <repo>/sub but the repo root (.git) is <repo> -> must roll up.
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        # Two turns on gpt-5-codex, then one on gpt-5.5, each as a *cumulative* total.
        # Codex echoes the prior turn's count after each turn_context (equal total ->
        # must be skipped) and writes an info=null count first (no usage -> skipped).
        rows = [
            _codex_meta(CODEX_SID, sub),
            _codex_user("optimize the date formatter"),
            _codex_turn("gpt-5-codex", sub),
            {
                "timestamp": "t",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": None},
            },
            _codex_tokens(1000, 100, 800, 1100),  # turn 1 (delta = itself)
            _codex_turn("gpt-5-codex", sub),
            _codex_tokens(1000, 100, 800, 1100),  # echo of turn 1 -> skipped
            _codex_tokens(2200, 160, 1700, 2360),  # turn 2 (delta vs turn 1)
            _codex_turn("gpt-5.5", sub),
            _codex_tokens(2200, 160, 1700, 2360),  # echo of turn 2 -> skipped
            _codex_tokens(2700, 200, 1900, 2900),  # turn 3 on gpt-5.5
        ]
        _codex_rollout(root, CODEX_SID, rows)

        args = type("Args", (), {"demo": False})()
        store = ot.CodexStore(os.path.join(tmp, "sessions"), args)
        workflows = store.workflows()

        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == CODEX_SID
        assert w.title == "optimize the date formatter"  # first plain user message
        assert w.directory == repo  # folded to the git root, not the bare "sub"
        assert w.source == "Codex"
        assert w.subagents == 0  # Codex has no subagent tree
        assert w.total_cost == 0.0 and w.root_cost == 0.0  # recorded cost; $ reprices
        # the accepted deltas sum back to the final cumulative total (2900)
        assert w.total_tokens == 2900 and w.unpriced_tokens == 2900

        rows_out = {r["model_name"]: r for r in store.model_breakdown()}
        assert set(rows_out) == {"openai/gpt-5-codex", "openai/gpt-5.5"}  # provider-prefixed
        codex = rows_out["openai/gpt-5-codex"]
        assert codex["runs"] == 2  # the echo + null count did not inflate the count
        # OpenAI's input_tokens includes the cached read; we split it into uncached +
        # cache_read. turn1 (1000/800) + turn2 delta (1200/900): uncached 200+300=500.
        assert codex["unpriced_input"] == 500
        assert codex["unpriced_cache_read"] == 800 + 900
        assert codex["unpriced_output"] == 100 + 60
        # no subagents, so the root split equals the total split
        assert codex["root_unpriced_input"] == codex["unpriced_input"]
        five_five = rows_out["openai/gpt-5.5"]
        assert five_five["runs"] == 1
        assert (five_five["unpriced_input"], five_five["unpriced_cache_read"]) == (300, 200)

        # the (all-unpriced) usage reprices to a positive list-price estimate under $
        est = ot.api_equivalent_cost("openai/gpt-5-codex", 500, 160, 0, 1700, 0)
        assert est > 0

        # one flat depth-0 node; its model is the most-used one (gpt-5-codex, 2 runs)
        nodes = store.workflow_nodes(CODEX_SID)
        assert len(nodes) == 1
        assert nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["model_name"] == "openai/gpt-5-codex"
        assert nodes[0]["tokens_total"] == 2900  # root aggregates both models
        assert nodes[0]["cost"] == 0.0


def test_codex_title_takes_any_user_message_kind_and_collapses_newlines():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # Older rollouts omit "kind" on user_message; the title must still be picked up,
        # and a multi-line prompt (@file mentions) collapses to a single-line title.
        um = {
            "timestamp": "2025-10-03T14:51:05.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "fix\n@a.py:1\nthe bug"},
        }
        rows = [
            _codex_meta(CODEX_SID, cwd),
            um,
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(10, 5, 0, 15),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        assert store.workflows()[0].title == "fix @a.py:1 the bug"


def test_codex_store_treats_a_shrinking_total_as_a_compaction_reset():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # The running total grows, then *shrinks* (context compaction): the smaller
        # total is fresh post-reset usage, not a duplicate -- so it is counted, added
        # on top of the pre-reset peak.
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(1000, 100, 800, 1100),  # peak
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(400, 30, 100, 430),  # reset: fresh usage of (400,30)
        ]
        _codex_rollout(root, CODEX_SID, rows)

        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        w = store.workflows()[0]
        # pre-reset 1100 + post-reset 430 (the reset block counts in full)
        assert w.total_tokens == 1100 + 430
        r = store.model_breakdown()[0]
        assert r["runs"] == 2
        assert r["unpriced_input"] == (1000 - 800) + (400 - 100)  # uncached, both blocks
        assert r["unpriced_cache_read"] == 800 + 100


def test_codex_joins_the_source_cycle_and_builds_a_resume_command():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _empty_opencode_db(db)
        cdir = os.path.join(tmp, "claude", "slug")
        os.makedirs(cdir)
        _write_jsonl(
            os.path.join(cdir, "s.jsonl"),
            [_claude_msg("s", "claude-opus-4-8", _usage(1, 1, 0, 0), uuid="u", cwd=tmp)],
        )
        xdir = os.path.join(tmp, "codex", "2025")
        os.makedirs(xdir)
        _codex_rollout(
            xdir,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, tmp),
                _codex_turn("gpt-5-codex", tmp),
                _codex_tokens(10, 5, 0, 15),
            ],
        )
        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": db,
                "claude_dir": os.path.join(tmp, "claude"),
                "codex_dir": os.path.join(tmp, "codex"),
                "demo": False,
            },
        )()
        # all three present -> the cycle is opencode / claude / codex / all
        assert ot.available_sources(args) == ["opencode", "claude", "codex"]
        assert ot.sources.source_cycle(args) == ["opencode", "claude", "codex", "all"]
        # the c key walks through Codex on the way to the merged view
        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "claude"
        assert app.next_source_name() == "Codex"
        app.source_key = "codex"
        assert app.next_source_name() == "all"

        # L copies a `codex resume <id>` command for a Codex session
        wf = workflow("0199-id", "2026-06-01 12:00:00", title="t", directory="/tmp/proj")
        wf.source = "Codex"
        assert app.resume_command(wf) == "cd /tmp/proj && codex resume 0199-id"


def test_codex_turns_timeline_from_cumulative_deltas():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_user("write the parser", ts="2025-10-03T14:51:05.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:51:10.000Z"),
            _codex_tokens(1000, 200, 100, 1200, ts="2025-10-03T14:51:20.000Z"),
            _codex_user("now add tests", ts="2025-10-03T14:52:00.000Z"),
            _codex_tokens(2500, 500, 600, 3000, ts="2025-10-03T14:52:30.000Z"),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        store.workflows()
        assert store.supports_turns(CODEX_SID)
        t = store.message_timeline(CODEX_SID)
        assert len(t) == 2  # one row per accepted cumulative delta
        assert [r["prompt_title"] for r in t] == ["write the parser", "now add tests"]
        assert t[0]["input"] == 900 and t[0]["cache_read"] == 100 and t[0]["output"] == 200
        assert t[1]["input"] == 1000 and t[1]["cache_read"] == 500 and t[1]["output"] == 300
        assert all(r["cost"] == 0.0 for r in t)  # Codex records none; "$" estimates
        assert t[0]["model_name"] == "openai/gpt-5-codex"


def test_codex_turns_carry_the_reasoning_effort_in_force_at_each_turn():
    # Codex writes a turn_context per turn, so effort is a RUNNING value: a /reasoning
    # switch mid-session applies from that turn on, and every earlier turn keeps the
    # level it actually ran at. Measured on 138 real rollouts, one switched.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_user("write the parser", ts="2025-10-03T14:51:05.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:51:10.000Z", effort="high"),
            _codex_tokens(1000, 200, 100, 1200, ts="2025-10-03T14:51:20.000Z"),
            _codex_user("now add tests", ts="2025-10-03T14:52:00.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:52:10.000Z", effort="low"),
            _codex_tokens(2500, 500, 600, 3000, ts="2025-10-03T14:52:30.000Z"),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        store.workflows()
        assert [r["effort"] for r in store.message_timeline(CODEX_SID)] == ["high", "low"]

    # A rollout whose turn_context records no effort (an older Codex) ships "", which is
    # what drops the column rather than drawing a stripe of dashes.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, cwd),
                _codex_turn("gpt-5-codex", cwd),
                _codex_tokens(1000, 200, 100, 1200),
            ],
        )
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        store.workflows()
        assert [r["effort"] for r in store.message_timeline(CODEX_SID)] == [""]


def test_codex_tool_breakdown_attributes_turn_deltas_to_pending_calls():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_turn("gpt-5-codex", cwd),
            # Turn 1 calls two tools; its 1200-token delta splits 600/600.
            _codex_call("shell_command", ts="2025-10-03T14:51:12.000Z"),
            _codex_call("apply_patch", ts="2025-10-03T14:51:15.000Z", kind="custom_tool_call"),
            _codex_tokens(1000, 200, 100, 1200, ts="2025-10-03T14:51:20.000Z"),
            # The duplicate echo must not consume the next turn's pending calls.
            _codex_tokens(1000, 200, 100, 1200, ts="2025-10-03T14:51:21.000Z"),
            _codex_call("update_plan", ts="2025-10-03T14:52:10.000Z"),
            _codex_tokens(2500, 500, 600, 3000, ts="2025-10-03T14:52:30.000Z"),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        store.workflows()
        assert store.supports_tools(CODEX_SID)
        rows = {r["tool"]: r for r in store.tool_breakdown(CODEX_SID)}
        assert set(rows) == {"shell_command", "apply_patch", "update_plan"}
        assert rows["shell_command"]["tokens_total"] == 600
        assert rows["apply_patch"]["tokens_total"] == 600
        assert rows["update_plan"]["tokens_total"] == 1800  # turn 2's whole delta
        assert rows["update_plan"]["model_name"] == "openai/gpt-5-codex"


def test_codex_spawned_threads_fold_into_a_subagent_tree():
    # Codex's collab mode writes a spawned thread as its own rollout whose
    # session_meta.source carries the parent thread id; it must fold under the
    # parent (out of the workflows list, into its totals/nodes/Turns/Tools)
    # instead of showing as an unrelated session.
    parent_sid = "11111111-1111-1111-1111-111111111111"
    child_sid = "22222222-2222-2222-2222-222222222222"
    spawn = {
        "subagent": {
            "thread_spawn": {
                "parent_thread_id": parent_sid,
                "depth": 1,
                "agent_nickname": "researcher",
            }
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            parent_sid,
            [
                _codex_meta(parent_sid, cwd),
                _codex_user("plan the feature", ts="2025-10-03T14:51:05.000Z"),
                _codex_turn("gpt-5-codex", cwd),
                _codex_call("update_plan", ts="2025-10-03T14:51:12.000Z"),
                _codex_tokens(1000, 200, 0, 1200, ts="2025-10-03T14:51:20.000Z"),
            ],
        )
        _codex_rollout(
            root,
            child_sid,
            [
                _codex_meta(child_sid, cwd, source=spawn),
                _codex_turn("gpt-5-codex", cwd, ts="2025-10-03T14:52:00.000Z"),
                _codex_call("shell_command", ts="2025-10-03T14:52:05.000Z"),
                _codex_tokens(400, 100, 0, 500, ts="2025-10-03T14:52:10.000Z"),
            ],
        )
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        rows = store.workflows()
        assert len(rows) == 1  # the spawned thread folded away
        w = rows[0]
        assert w.id == parent_sid and w.subagents == 1
        assert w.total_tokens == 1200 + 500  # subtree total
        nodes = store.workflow_nodes(parent_sid)
        assert [(n["depth"], n["agent"]) for n in nodes] == [(0, "-"), (1, "researcher")]
        assert nodes[1]["id"] == child_sid and nodes[1]["tokens_total"] == 500
        # model rows: total covers the subtree, root_* only the parent's own share
        mrow = [r for r in store.model_breakdown() if r["root_id"] == parent_sid]
        assert len(mrow) == 1 and mrow[0]["tokens_total"] == 1700
        assert mrow[0]["unpriced_output"] == 300 and mrow[0]["root_unpriced_output"] == 200
        # Turns interleave the child's turn (agent-tagged); Tools cover the subtree.
        t = store.message_timeline(parent_sid)
        assert [(r["agent"], r["tokens_total"]) for r in t] == [("-", 1200), ("researcher", 500)]
        tools = {r["tool"]: r["tokens_total"] for r in store.tool_breakdown(parent_sid)}
        assert tools == {"update_plan": 1200, "shell_command": 500}


def test_codex_ended_at_reflects_the_latest_activity_in_a_spawned_thread():
    # A spawned collab thread logging activity after the parent's own last record
    # must bump the parent's ended_at -- the subtree-wide rollup, same as
    # ClaudeStore's sidechain-inclusive ts_max.
    parent_sid = "11111111-1111-1111-1111-111111111111"
    child_sid = "22222222-2222-2222-2222-222222222222"
    spawn = {
        "subagent": {
            "thread_spawn": {
                "parent_thread_id": parent_sid,
                "agent_nickname": "researcher",
            }
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            parent_sid,
            [
                _codex_meta(parent_sid, cwd, ts="2026-06-10T18:46:00.000Z"),
                _codex_turn("gpt-5-codex", cwd, ts="2026-06-10T18:46:05.000Z"),
                _codex_tokens(1000, 200, 0, 1200, ts="2026-06-10T18:46:10.000Z"),
            ],
        )
        _codex_rollout(
            root,
            child_sid,
            [
                _codex_meta(child_sid, cwd, ts="2026-06-10T18:47:00.000Z", source=spawn),
                _codex_turn("gpt-5-codex", cwd, ts="2026-06-10T19:10:00.000Z"),
                _codex_tokens(400, 100, 0, 500, ts="2026-06-10T19:10:05.000Z"),
            ],
        )
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        w = store.workflows()[0]

        # iso_to_local renders in the system's local TZ, so compare against its own
        # conversion of each raw UTC timestamp rather than a hardcoded wall-clock string.
        assert w.id == parent_sid
        assert w.created_at == iso_to_local("2026-06-10T18:46:00.000Z")
        assert w.ended_at == iso_to_local("2026-06-10T19:10:05.000Z")
        assert w.ended_at > w.created_at


def test_codex_ended_at_falls_back_to_created_at_when_nothing_later():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _codex_meta(CODEX_SID, cwd, ts="2026-06-10T18:46:00.000Z"),
            _codex_turn("gpt-5-codex", cwd, ts="2026-06-10T18:46:00.000Z"),
            _codex_tokens(10, 5, 0, 15, ts="2026-06-10T18:46:00.000Z"),
        ]
        _codex_rollout(root, CODEX_SID, rows)
        store = ot.CodexStore(root, type("Args", (), {"demo": False})())
        w = store.workflows()[0]
        assert w.ended_at == w.created_at


def test_codex_survives_a_valid_json_line_that_is_not_an_object():
    # `[]`, `"hello"` and `0` are all valid JSON: they pass the `except ValueError` and
    # then raise AttributeError out of .get() -- taking down the WHOLE backend, not the
    # one rollout. The sibling JSONL backends all guard with an isinstance check.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions", "2025", "10", "03")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        path = os.path.join(root, f"rollout-2025-10-03T16-51-03-{CODEX_SID}.jsonl")
        rows = [
            _codex_meta(CODEX_SID, cwd),
            _codex_turn("gpt-5-codex", cwd),
            _codex_tokens(100, 50, 0, 150),
        ]
        with open(path, "w") as fh:
            fh.write("[]\n" + '"hello"\n' + "0\n")
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        w = ot.CodexStore(
            os.path.join(tmp, "sessions"), type("Args", (), {"demo": False})()
        ).workflows()
        assert len(w) == 1 and w[0].total_tokens == 150


def test_codex_survives_a_token_count_json_parses_as_infinity():
    # A cumulative total is whatever the rollout says. `1e400` is valid JSON that json
    # maps to inf, and a bare int(inf) raises OverflowError -- an ArithmeticError, so it
    # isn't caught as a ValueError and the whole backend dies at workflows().
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions", "2025", "10", "03")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        path = os.path.join(root, f"rollout-2025-10-03T16-51-03-{CODEX_SID}.jsonl")
        with open(path, "w") as fh:
            for row in (_codex_meta(CODEX_SID, cwd), _codex_turn("gpt-5-codex", cwd)):
                fh.write(json.dumps(row) + "\n")
            fh.write(
                '{"timestamp": "2025-10-03T14:51:20.000Z", "type": "event_msg", "payload": '
                '{"type": "token_count", "info": {"total_token_usage": {"input_tokens": '
                '1e400, "output_tokens": 50, "cached_input_tokens": 0, "total_tokens": '
                "150}}}}\n"
            )
        store = ot.CodexStore(os.path.join(tmp, "sessions"), type("Args", (), {"demo": False})())
        w = store.workflows()
        # The backend survives, which is the point. The RECORD is skipped whole rather
        # than counted off its surviving components: every figure in it is cumulative,
        # so a half-trusted one becomes the baseline every later turn is measured
        # against (see test_codex_a_malformed_total_does_not_inflate_the_next_turn).
        # Nothing is really lost by skipping -- the next valid record's delta spans the
        # gap -- except when the bad record is the session's ONLY one, as here, which
        # then has no recorded usage at all and drops like any other usage-less session.
        assert w == [] and store.model_breakdown() == []


def test_codex_a_malformed_total_does_not_inflate_the_next_turn():
    # Codex logs a CUMULATIVE total per turn and opentab takes deltas off it, so a
    # record that can't be trusted must not become the subtrahend. Coercing its junk to
    # 0 reads as a compaction reset and then bills the NEXT turn the whole running
    # total: on [100, junk, 400] the last turn came out 400 instead of 300.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions", "2025", "10", "03")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _codex_rollout(
            root,
            CODEX_SID,
            [
                _codex_meta(CODEX_SID, cwd),
                _codex_turn("gpt-5-codex", cwd),
                _codex_tokens(80, 20, 0, 100, ts="2025-10-03T14:51:20.000Z"),
                # A component JSON allows and no coercion can honour, mid-stream.
                _codex_tokens("nonsense", 100, 0, 250, ts="2025-10-03T14:51:30.000Z"),
                _codex_tokens(300, 100, 0, 400, ts="2025-10-03T14:51:40.000Z"),
            ],
        )
        store = ot.CodexStore(os.path.join(tmp, "sessions"), type("Args", (), {"demo": False})())
        (w,) = store.workflows()
        assert w.total_tokens == 400  # the deltas still close on the final total
        turns = store.message_timeline(w.id)
        assert [t["tokens_total"] for t in turns] == [100, 300]


def test_codex_a_falsy_or_fractional_component_is_distrusted_too():
    # The three shapes a "did safe_int give up?" predicate could not see, because it had
    # to re-guess what safe_int had done: JSON `false` and `null` both collapse to a
    # valid-looking 0, and a fractional 1.5 truncates to 1. Each then becomes the
    # cumulative baseline and inflates the next turn exactly like a junk string does.
    # (Measured on the real corpus: 3,408 token_count records, every component a plain
    # int -- so none of these is a shape Codex actually writes.)
    for bad in (False, None, 1.5, -5, float("inf"), (1 << 53) + 1, "9007199254740993"):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "sessions", "2025", "10", "03")
            os.makedirs(root)
            cwd = os.path.join(tmp, "repo")
            os.makedirs(cwd)
            _codex_rollout(
                root,
                CODEX_SID,
                [
                    _codex_meta(CODEX_SID, cwd),
                    _codex_turn("gpt-5-codex", cwd),
                    _codex_tokens(80, 20, 0, 100, ts="2025-10-03T14:51:20.000Z"),
                    _codex_tokens(bad, 100, 0, 250, ts="2025-10-03T14:51:30.000Z"),
                    _codex_tokens(300, 100, 0, 400, ts="2025-10-03T14:51:40.000Z"),
                ],
            )
            store = ot.CodexStore(
                os.path.join(tmp, "sessions"), type("Args", (), {"demo": False})()
            )
            (w,) = store.workflows()
            turns = store.message_timeline(w.id)
            assert [t["tokens_total"] for t in turns] == [100, 300], bad
            assert w.total_tokens == 400, bad
    # An ABSENT component stays a valid 0 -- absence is how an older schema differs, and
    # rejecting it would drop a whole session's usage over a field that was never there.
    assert ot.CodexStore._cumulative(0) == 0 and ot.CodexStore._cumulative("12") == 12
    # The bound sits exactly where safe_int's does, on BOTH sides of it: float() is lossy
    # right at the ceiling, so measuring the converted value let 2**53+1 in as 2**53 --
    # the rounding defeating the very guard it was being fed to.
    assert ot.CodexStore._cumulative(1 << 53) == 1 << 53
    assert ot.CodexStore._cumulative((1 << 53) + 1) is None
