import json
import os
import random
import tempfile

import opentab as ot
from opentab.formatting import iso_to_local

from tests._support import _claude_msg, _usage, _write_jsonl


def test_claude_retention_reads_the_user_setting_and_its_documented_default():
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CLAUDE_CONFIG_DIR"] = tmp
        try:
            assert ot.claude_projects_dir() == os.path.join(tmp, "projects")
            missing = ot.claude_retention()
            assert (missing.days, missing.source, missing.needs_warning) == (30, "default", True)

            path = os.path.join(tmp, "settings.json")
            for value in (True, 0, -1, "30", []):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"cleanupPeriodDays": value}, fh)
                invalid = ot.claude_retention()
                assert invalid.days is None and invalid.source == "invalid"

            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"cleanupPeriodDays": 365}, fh)
            short = ot.claude_retention()
            assert short.days == 365 and short.needs_warning

            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"cleanupPeriodDays": 3650}, fh)
            safe = ot.claude_retention()
            assert safe.days == 3650 and safe.source == "configured" and not safe.needs_warning
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old


def test_claude_workflow_ended_at_reflects_the_latest_sidechain_activity():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        main = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(100, 50, 0, 0),
            uuid="u0",
            cwd=cwd,
            ts="2026-06-10T18:46:00.000Z",
        )
        # a subagent (sidechain) turn logged AFTER the main thread's last message --
        # ended_at must reflect it, since the subtree is still active.
        side = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(40, 10, 0, 0),
            uuid="u1",
            cwd=cwd,
            parent="u0",
            side=True,
            ts="2026-06-10T19:10:00.000Z",
        )
        _write_jsonl(os.path.join(root, "s1.jsonl"), [main, side])

        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        w = store.workflows()[0]

        # iso_to_local renders in the system's local TZ, so compare against its own
        # conversion of each raw UTC timestamp rather than a hardcoded wall-clock string.
        assert w.created_at == iso_to_local("2026-06-10T18:46:00.000Z")
        assert w.ended_at == iso_to_local("2026-06-10T19:10:00.000Z")


def test_claude_message_timeline_orders_by_time_and_marks_sidechain():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        # main thread at :02, a sidechain (subagent) turn at :01 -> the sidechain must
        # sort first by time even though it's logged second, and be marked depth 1.
        main = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(100, 50, 0, 0),
            uuid="u0",
            cwd=cwd,
            ts="2026-06-10T18:46:02.000Z",
        )
        side = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(40, 10, 0, 0),
            uuid="u1",
            cwd=cwd,
            parent="u0",
            side=True,
            ts="2026-06-10T18:46:01.000Z",
        )
        _write_jsonl(os.path.join(root, "s1.jsonl"), [main, side])

        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        store.workflows()  # parse
        rows = store.message_timeline("s1")
        assert store.supports_turns("s1") is True
        assert [r["depth"] for r in rows] == [1, 0]  # sidechain (earlier) first
        assert rows[0]["agent"] == "subagent" and rows[1]["agent"] == "-"
        assert rows[0]["tokens_total"] == 50 and rows[1]["tokens_total"] == 150
        assert rows[0]["cost"] == 0.0 and rows[1]["cost"] == 0.0  # recorded; $ reprices
        assert rows[0]["time"] < rows[1]["time"]  # "HH:MM:SS" display, in order


def test_claude_turns_carry_the_reasoning_effort_the_call_ran_at():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        _write_jsonl(
            os.path.join(root, "s1.jsonl"),
            [
                _claude_msg(
                    "s1",
                    "claude-opus-4-8",
                    _usage(100, 50, 0, 0),
                    uuid="u0",
                    cwd=cwd,
                    ts="2026-06-10T18:46:00.000Z",
                    effort="xhigh",
                ),
                _claude_msg(
                    "s1",
                    "claude-opus-4-8",
                    _usage(40, 10, 0, 0),
                    uuid="u1",
                    cwd=cwd,
                    ts="2026-06-10T18:46:10.000Z",
                ),
            ],
        )
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        store.workflows()
        assert [r["effort"] for r in store.message_timeline("s1")] == ["xhigh", ""]


def test_claude_message_timeline_groups_turns_by_owning_user_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")

        def user(text, ts, uuid):
            return {
                "type": "user",
                "sessionId": "s1",
                "cwd": cwd,
                "timestamp": ts,
                "uuid": uuid,
                "message": {"role": "user", "content": text},
            }

        # two prompts; each assistant turn belongs to the most recent earlier prompt
        rows_in = [
            user("first question", "2026-06-10T18:46:00.000Z", "ua"),
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(100, 50),
                uuid="a1",
                cwd=cwd,
                ts="2026-06-10T18:46:05.000Z",
            ),
            user("second question", "2026-06-10T18:47:00.000Z", "ub"),
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(20, 5),
                uuid="a2",
                cwd=cwd,
                ts="2026-06-10T18:47:05.000Z",
            ),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows_in)

        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        store.workflows()
        rows = store.message_timeline("s1")
        assert [r["prompt_title"] for r in rows] == ["first question", "second question"]
        assert rows[0]["prompt_id"] == "ua" and rows[1]["prompt_id"] == "ub"


def test_claude_turns_carry_the_full_prompt_uncapped():
    long_prompt = ("please refactor the frobnicator carefully " * 6).strip() + "\nkeep tests green"
    assert len(long_prompt) > 200
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        rows_in = [
            {
                "type": "user",
                "sessionId": "s1",
                "cwd": cwd,
                "timestamp": "2026-06-10T18:46:00.000Z",
                "uuid": "ua",
                "message": {"role": "user", "content": long_prompt},
            },
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(100, 50),
                uuid="a1",
                cwd=cwd,
                ts="2026-06-10T18:46:05.000Z",
            ),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows_in)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        w = store.workflows()[0]
        assert w.title == long_prompt[:80]  # the session-title fallback stays short
        rows = store.message_timeline("s1")
        assert rows[0]["prompt_full"] == long_prompt  # uncapped, newline kept
        assert rows[0]["prompt_title"] == " ".join(long_prompt.split())[:160]


def test_claude_store_prices_tokens_dedupes_and_rolls_up_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        # cwd is <repo>/sub but the repo root (.git) is <repo> -> a session started
        # in a subdir must roll up to the repo, not the bare basename "sub".
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        m1 = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(1000, 500, 2000, 300),
            uuid="u1",
            cwd=sub,
            mid="m1",
            req="r1",
        )
        m2 = _claude_msg(
            "s1", "claude-opus-4-8", _usage(10, 20, 100, 0), uuid="u2", cwd=sub, mid="m2", req="r2"
        )
        dup = dict(m1)  # same (message.id, requestId) -> must be deduped, not double-counted
        _write_jsonl(os.path.join(root, "s1.jsonl"), [m1, dup, m2])

        args = type("Args", (), {"demo": False})()
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), args)
        workflows = store.workflows()

        assert len(workflows) == 1
        w = workflows[0]
        # tokens summed across the two distinct messages (dup ignored)
        assert w.total_tokens == (1000 + 500 + 2000 + 300) + (10 + 20 + 100)
        # recorded cost is $0 (Claude logs none); all of it is "unpriced" until $
        assert w.total_cost == 0.0 and w.root_cost == 0.0
        assert w.unpriced_tokens == w.total_tokens
        assert w.subagents == 0
        assert w.source == "Claude Code"
        assert w.directory == repo  # folded to the git root
        assert w.created_at.startswith("2026-06") and len(w.created_at) == 19

        rows = store.model_breakdown()
        assert len(rows) == 1
        r = rows[0]
        assert r["runs"] == 2  # dup deduped
        assert r["model_name"] == "anthropic/claude-opus-4-8"
        assert r["cost"] == 0.0
        # the unpriced split carries the full token counts so "$" can reprice them
        assert (r["unpriced_input"], r["unpriced_output"], r["unpriced_cache_read"]) == (
            1010,
            520,
            2100,
        )
        expected = ot.api_equivalent_cost("anthropic/claude-opus-4-8", 1010, 520, 0, 2100, 300)
        assert abs(expected - round(expected, 6)) < 1e-9 and expected > 0


def test_claude_store_groups_sidechain_subagents_into_tree():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        main = _claude_msg("s1", "claude-opus-4-8", _usage(100, 50, 0, 0), uuid="u0", cwd=cwd)
        # two sidechain messages chained off the main thread -> one subagent run
        s1 = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(40, 10, 0, 0),
            uuid="u1",
            cwd=cwd,
            parent="u0",
            side=True,
        )
        s2 = _claude_msg(
            "s1", "claude-opus-4-8", _usage(20, 5, 0, 0), uuid="u2", cwd=cwd, parent="u1", side=True
        )
        _write_jsonl(os.path.join(root, "s1.jsonl"), [main, s1, s2])

        args = type("Args", (), {"demo": False})()
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), args)
        w = store.workflows()[0]
        nodes = store.workflow_nodes("s1")

        assert w.subagents == 1  # the two sidechain msgs collapse to one run
        assert w.total_tokens == 150 + 50 + 25
        assert w.total_cost == 0.0 and w.root_cost == 0.0  # recorded cost; $ reprices

        # the root vs subagent split lives in the (un)priced token fields
        r = store.model_breakdown()[0]
        assert r["root_unpriced_input"] == 100  # main thread only
        assert r["unpriced_input"] == 100 + 40 + 20  # main + both sidechain msgs

        assert len(nodes) == 2
        assert nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[1]["depth"] == 1 and nodes[1]["agent"] == "subagent"
        assert nodes[1]["tokens_total"] == (40 + 10) + (20 + 5)
        assert nodes[0]["cost"] == 0.0 and nodes[1]["cost"] == 0.0  # recorded; $ reprices


def _claude_user(text, *, cwd, meta=False, side=False, uuid="u"):
    return {
        "type": "user",
        "sessionId": "s1",
        "cwd": cwd,
        "timestamp": "2026-06-10T18:46:00.000Z",
        "uuid": uuid,
        "isMeta": meta,
        "isSidechain": side,
        "message": {"role": "user", "content": text},
    }


def test_claude_title_skips_injected_command_and_meta_messages():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        rows = [
            _claude_user("<local-command-caveat>Caveat: ...", cwd=repo, meta=True, uuid="u0"),
            _claude_user("<command-name>/clear</command-name>", cwd=repo, uuid="u1"),
            _claude_user("the real prompt about heat maps", cwd=repo, uuid="u2"),
            _claude_msg("s1", "claude-opus-4-8", _usage(10, 20, 30, 0), uuid="ua", cwd=repo),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        assert store.workflows()[0].title == "the real prompt about heat maps"


def test_claude_title_keeps_genuine_short_first_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        rows = [
            _claude_user("ok", cwd=repo, uuid="u0"),
            _claude_msg("s1", "claude-opus-4-8", _usage(10, 20, 30, 0), uuid="ua", cwd=repo),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        assert store.workflows()[0].title == "ok"


def test_claude_shows_zero_in_normal_mode_and_estimate_under_dollar():
    with tempfile.TemporaryDirectory() as tmp:
        cdir = os.path.join(tmp, "projects", "slug")
        os.makedirs(cdir)
        msg = _claude_msg("s1", "claude-opus-4-8", _usage(1000, 500, 200, 50), uuid="u1", cwd=tmp)
        _write_jsonl(os.path.join(cdir, "s1.jsonl"), [msg])

        args = type(
            "Args",
            (),
            {"demo": False, "no_worktrees": True, "since": None, "until": None, "days": None},
        )()
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), args)
        app = ot.App(store, args)
        app._load_model_cache()  # the deferred per-model scan

        # Claude records no cost, so the app starts in the $ estimate view
        # (tokens repriced at list rates), not on a wall of $0.00
        assert app.show_api_prices
        expected = ot.api_equivalent_cost("anthropic/claude-opus-4-8", 1000, 500, 0, 200, 50)
        assert expected > 0
        assert abs(app.range_cost_total() - expected) < 1e-6
        # "$" flips to the recorded numbers: $0 (Claude logs none)
        app.toggle_api_prices()
        assert app.range_cost_total() == 0.0
        # and back to the estimate
        app.toggle_api_prices()
        assert abs(app.range_cost_total() - expected) < 1e-6
        # and the model mix reflects the same flip
        assert (
            app.model_mix("s1")[0]["cost"] == round(expected, 6)
            or abs(app.model_mix("s1")[0]["cost"] - expected) < 1e-6
        )


def test_claude_tool_breakdown_splits_steps_across_tool_use_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        rows_in = [
            # One step calling two tools in parallel: its 150 tokens split 75/75.
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(100, 50),
                uuid="a1",
                cwd=cwd,
                tools=["Bash", "Read"],
            ),
            # An MCP step; and a tool-less step that must not appear at all.
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(40, 10),
                uuid="a2",
                cwd=cwd,
                tools=["mcp__linear__create_issue"],
            ),
            _claude_msg("s1", "claude-opus-4-8", _usage(30, 5), uuid="a3", cwd=cwd),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows_in)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        store.workflows()
        assert store.supports_tools("s1")
        rows = {r["tool"]: r for r in store.tool_breakdown("s1")}
        assert set(rows) == {"Bash", "Read", "mcp__linear__create_issue"}
        assert rows["Bash"]["tokens_total"] == 75 and rows["Read"]["tokens_total"] == 75
        assert rows["Bash"]["calls"] == 1 and rows["Bash"]["model_name"] == (
            "anthropic/claude-opus-4-8"
        )
        assert rows["mcp__linear__create_issue"]["tokens_total"] == 50
        assert all(r["cost"] == 0.0 for r in rows.values())  # recorded $0; "$" reprices


def test_claude_context_breakdown_composes_split_records_and_matches_tools():
    # Streamed blocks share message/request ids; the resumed copy repeats their UUIDs.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        root2 = os.path.join(tmp, "projects", "slug2")
        os.makedirs(root)
        os.makedirs(root2)
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        prompt = _claude_user("please fix the parser", cwd=repo, uuid="u1")
        rec1 = _claude_msg(
            "s1", "claude-opus-4-8", _usage(100, 50), uuid="a1", cwd=repo, mid="m1", req="r1"
        )
        rec1["message"]["content"] = [{"type": "thinking", "thinking": "x" * 80}]
        rec2 = _claude_msg(
            "s1", "claude-opus-4-8", _usage(100, 50), uuid="a2", cwd=repo, mid="m1", req="r1"
        )
        rec2["message"]["content"] = [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la"}}
        ]
        result = _claude_user("", cwd=repo, uuid="u2")
        result["message"]["content"] = [
            {"type": "tool_result", "tool_use_id": "t1", "content": "y" * 400}
        ]
        reminder = _claude_user("<system-reminder>injected</system-reminder>", cwd=repo, uuid="u3")
        compacted = _claude_user("summary " * 50, cwd=repo, uuid="u4")
        compacted["isCompactSummary"] = True
        rows_out = [prompt, rec1, rec2, result, reminder, compacted]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows_out)
        _write_jsonl(os.path.join(root2, "s1.jsonl"), rows_out)  # the resumed copy

        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        store.workflows()
        rows = {(r["category"], r["kind"]): r for r in store.context_breakdown("s1")}
        assert store.supports_context("s1")
        assert rows[("user prompts", "")]["count"] == 1
        assert rows[("reasoning", "")]["est_tokens"] == 20  # 80 chars / 4, replay-deduped
        assert rows[("tool results", "Bash")]["est_tokens"] == 100  # matched via t1
        assert rows[("tool results", "Bash")]["count"] == 1  # not doubled by the replay
        assert ("tool call params", "Bash") in rows
        assert ("injected context", "system reminders") in rows
        assert ("compaction summaries", "") in rows
        # usage is still single-counted, and the folded turn carries the tool call
        turns = store.message_timeline("s1")
        assert len(turns) == 1 and turns[0]["tools"] == ["Bash"]
        assert sum(r["calls"] for r in store.tool_breakdown("s1")) == 1


def test_claude_subagent_sidecars_belong_to_their_parent_session():
    """Claude Code writes a subagent's turns to <slug>/<sessionId>/subagents/agent-*.jsonl,
    and the records inside carry the PARENT's sessionId. The file name is therefore not a
    session id: recent_roots must fold the sidecar into its owner (a phantom "agent-<hex>"
    root sorts first, since sidecars are the freshest files, and --status would price it at
    $0.00), root_of must refuse the phantom, and the single-transcript status path must
    still see the subagent's tokens."""
    with tempfile.TemporaryDirectory() as tmp:
        projects = os.path.join(tmp, "projects")
        slug = os.path.join(projects, "slug")
        side_dir = os.path.join(slug, "s1", "subagents")
        os.makedirs(side_dir)
        cwd = os.path.join(tmp, "repo")
        main = _claude_msg("s1", "claude-opus-4-8", _usage(100, 10, 0, 0), uuid="u0", cwd=cwd)
        # The subagent's turn: its own file, but recorded under the parent's session id.
        sub = _claude_msg(
            "s1",
            "claude-opus-4-8",
            _usage(200, 20, 0, 0),
            uuid="u1",
            cwd=cwd,
            parent="u0",
            side=True,
        )
        _write_jsonl(os.path.join(slug, "s1.jsonl"), [main])
        sidecar = os.path.join(side_dir, "agent-deadbeef.jsonl")
        _write_jsonl(sidecar, [sub])
        os.utime(os.path.join(slug, "s1.jsonl"), (1000, 1000))
        os.utime(sidecar, (2000, 2000))  # the subagent is the most recent writer

        args = type("A", (), {"demo": False})()
        store = ot.ClaudeStore(projects, args)
        roots = store.recent_roots()
        assert [r["id"] for r in roots] == ["s1"]  # no phantom "agent-deadbeef" root
        assert roots[0]["last_active"] == 2000 * 1000  # the sidecar still bumps its owner
        assert roots[0]["directory"] == cwd  # read from the parent's own transcript
        assert store.root_of("agent-deadbeef") is None
        assert store.root_of("s1") == "s1"

        # The cold status path (no full parse yet) must count the subagent's tokens too.
        cold = ot.ClaudeStore(projects, args)
        assert sum(n["tokens_total"] for n in cold.status_nodes("s1")) == 330
        assert sum(n["tokens_total"] for n in store.workflow_nodes("s1")) == 330


def test_recent_roots_reads_the_directory_from_the_newest_resumed_copy():
    """Resuming a session from another directory leaves the same id under a second project
    slug with a different cwd. The row must take its directory from the NEWEST copy, not
    whichever the scan saw last, or a --status <dir> in the directory the session actually
    moved to would not find it."""
    with tempfile.TemporaryDirectory() as tmp:
        projects = os.path.join(tmp, "projects")
        for slug, cwd, mtime in (("A", "/repo/A", 200), ("B", "/repo/B", 100)):
            os.makedirs(os.path.join(projects, slug))
            path = os.path.join(projects, slug, "s.jsonl")
            _write_jsonl(
                path,
                [
                    _claude_msg(
                        "s", "claude-opus-4-8", _usage(10, 5, 0, 0), uuid="u" + slug, cwd=cwd
                    )
                ],
            )
            os.utime(path, (mtime, mtime))
        store = ot.ClaudeStore(projects, type("A", (), {"demo": False})())
        (row,) = store.recent_roots()
        assert row["last_active"] == 200 * 1000  # newest copy's mtime
        assert row["directory"] == "/repo/A"  # ...and ITS cwd, not the older copy's


def test_claude_reads_the_cache_write_ttl_split_and_prices_long_writes_higher():
    # The TTL split must sum to the flat cache_creation_input_tokens total.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        _write_jsonl(
            os.path.join(root, "s1.jsonl"),
            [
                _claude_msg(
                    "s1",
                    "claude-opus-4-5",
                    _usage(0, 0, 0, 1_000_000, cw1h=750_000),
                    uuid="u0",
                    cwd=cwd,
                    ts="2026-06-10T18:46:02.000Z",
                )
            ],
        )
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), _claude_args())
        rows = store.model_breakdown()
        assert len(rows) == 1
        row = rows[0]
        # The TOTAL keeps its meaning -- the subset never inflates a token count, so every
        # column, sum and export reads exactly as before.
        assert row["cache_write"] == 1_000_000
        assert row["tokens_total"] == 1_000_000
        # ...and the 1h subset rides beside it, in all three places $ reprices from.
        assert row["cache_write_1h"] == 750_000
        assert row["unpriced_cache_write_1h"] == 750_000
        assert row["root_unpriced_cache_write_1h"] == 750_000

        # The estimate: 250k at the 5m rate + 750k at the 1h rate, NOT 1M at the 5m rate.
        inp = ot.model_price("anthropic/claude-opus-4-5")[0]
        expected = (250_000 * inp * 1.25 + 750_000 * inp * 2.0) / 1e6
        assert (
            abs(
                ot.api_equivalent_cost("anthropic/claude-opus-4-5", 0, 0, 0, 0, 1_000_000, 750_000)
                - expected
            )
            < 1e-9
        )
        # and it really is more than the old, single-rate answer
        assert expected > (1_000_000 * inp * 1.25) / 1e6

        # The node rows --status prices carry it too.
        node = store.workflow_nodes("s1")[0]
        assert node["tokens_cache_write"] == 1_000_000
        assert node["tokens_cache_write_1h"] == 750_000

    # A transcript with NO cache_creation block (an older Claude Code) must price exactly
    # as it always did: subset 0, everything at the 5m rate.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        _write_jsonl(
            os.path.join(root, "s2.jsonl"),
            [
                _claude_msg(
                    "s2",
                    "claude-opus-4-5",
                    _usage(0, 0, 0, 1_000_000),  # no cw1h -> no cache_creation block
                    uuid="u0",
                    cwd=os.path.join(tmp, "repo"),
                    ts="2026-06-10T18:46:02.000Z",
                )
            ],
        )
        row = ot.ClaudeStore(os.path.join(tmp, "projects"), _claude_args()).model_breakdown()[0]
        assert row["cache_write"] == 1_000_000 and row["cache_write_1h"] == 0


def _claude_args():
    return type("A", (), {"demo": False})()


def _replay_corpus(tmp):
    # A background session that replays its parent's history: same message ids,
    # requestIds and uuids, only sessionId rewritten and sessionKind added -- the
    # shape Claude Code writes. The bg file is named so it sorts FIRST, because the
    # dedup credits the first claimer and glob order must not be what decides.
    root = os.path.join(tmp, "projects", "slug")
    os.makedirs(root)
    cwd = os.path.join(tmp, "repo")
    parent = [
        _claude_msg(
            "b-parent",
            "claude-opus-4-8",
            _usage(100, 50, 0, 0),
            uuid=f"u{i}",
            cwd=cwd,
            mid=f"m{i}",
            req=f"r{i}",
            ts=f"2026-06-10T18:4{i}:00.000Z",
        )
        for i in range(2)
    ]
    replay = []
    for rec in parent:  # the same calls, re-logged under the background session
        copy = dict(rec, sessionId="a-bg")
        copy["sessionKind"] = "bg"
        replay.append(copy)
    own = _claude_msg(
        "a-bg",
        "claude-opus-4-8",
        _usage(7, 3, 0, 0),
        uuid="u9",
        cwd=cwd,
        mid="m9",
        req="r9",
        ts="2026-06-10T18:49:00.000Z",
    )
    own["sessionKind"] = "bg"
    _write_jsonl(os.path.join(root, "a-bg.jsonl"), replay + [own])
    _write_jsonl(os.path.join(root, "b-parent.jsonl"), parent)
    return os.path.join(tmp, "projects")


def _adverse_order(store, root):
    # Force the file order that BREAKS the tie the wrong way, so the test actually
    # guards _parse()'s sort. Neither write order nor file name controls it for real:
    # _files() is a glob, and a directory listing is neither alphabetical nor creation
    # order (APFS returns a stable hash order), so a fixture can only hope to get the
    # adverse order by luck -- and this test passed with the sort removed until it
    # stated the order outright.
    slug = os.path.join(root, "slug")
    store._files = lambda: [
        os.path.join(slug, "a-bg.jsonl"),  # the replay, first: it must NOT win
        os.path.join(slug, "b-parent.jsonl"),
    ]
    return store


def test_claude_replayed_calls_are_credited_to_the_session_that_made_them():
    with tempfile.TemporaryDirectory() as tmp:
        root = _replay_corpus(tmp)
        store = _adverse_order(ot.ClaudeStore(root, _claude_args()), root)
        rows = {w.id: w for w in store.workflows()}
        assert rows["b-parent"].total_tokens == 300  # both of its own calls
        assert rows["a-bg"].total_tokens == 10  # only the one it actually made
        # Counted once overall, whichever way the tie went.
        assert sum(w.total_tokens for w in rows.values()) == 310

        # And the sort is what did it: with _replays_history blind, the replay -- first
        # in this order -- claims the parent's calls and the parent drops to 0.
        blind = _adverse_order(ot.ClaudeStore(root, _claude_args()), root)
        blind._replays_history = lambda _path: False
        blind_rows = {w.id: w.total_tokens for w in blind.workflows()}
        assert blind_rows["b-parent"] == 0 and blind_rows["a-bg"] == 310


def test_claude_session_kind_marker_survives_an_oversized_final_record():
    # The oversized final record pushes the marker outside the initial tail window.
    with tempfile.TemporaryDirectory() as tmp:
        root = _replay_corpus(tmp)
        bg = os.path.join(root, "slug", "a-bg.jsonl")
        with open(bg, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        huge = json.loads(lines[-1])
        # Past 4MiB on purpose: an earlier version capped the widening there, which
        # re-opened the hole for exactly the biggest records. There is no ceiling.
        huge["bigPaste"] = "x" * (5 << 20)
        lines[-1] = json.dumps(huge)
        with open(bg, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        store = _adverse_order(ot.ClaudeStore(root, _claude_args()), root)
        assert store._replays_history(bg) is True
        assert {w.id: w.total_tokens for w in store.workflows()}["b-parent"] == 300


def test_claude_drill_in_reads_one_transcript_without_parsing_the_corpus():
    with tempfile.TemporaryDirectory() as tmp:
        store = ot.ClaudeStore(_replay_corpus(tmp), _claude_args())
        rows = store.message_timeline("b-parent")
        assert store._sessions is None  # no corpus parse
        assert [r["tokens_total"] for r in rows] == [150, 150]
        assert len(store.workflow_nodes("b-parent")) == 1
        assert store._sessions is None  # still not, and the memo served the second call


def test_claude_drill_in_widens_to_the_corpus_for_a_replaying_transcript():
    with tempfile.TemporaryDirectory() as tmp:
        store = ot.ClaudeStore(_replay_corpus(tmp), _claude_args())
        rows = store.message_timeline("a-bg")
        assert store._sessions is not None  # widened, deliberately
        assert [r["tokens_total"] for r in rows] == [10]


def test_claude_status_nodes_never_widens_to_the_corpus():
    with tempfile.TemporaryDirectory() as tmp:
        store = ot.ClaudeStore(_replay_corpus(tmp), _claude_args())
        assert len(store.status_nodes("a-bg")) == 1
        assert store._sessions is None
        assert store.status_nodes("nope-not-a-session") == []
        assert store._sessions is None


def test_claude_survives_a_valid_json_line_that_is_not_an_object():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        msg = _claude_msg("s1", "claude-opus-4-8", _usage(inp=10, out=5), uuid="u1", cwd=cwd)
        with open(os.path.join(root, "s1.jsonl"), "w") as fh:
            fh.write("[]\n" + '"hello"\n' + "0\n" + json.dumps(msg) + "\n")
        w = ot.ClaudeStore(os.path.join(tmp, "projects"), _claude_args()).workflows()
        assert len(w) == 1 and w[0].total_tokens == 15


def test_claude_survives_a_token_count_json_parses_as_infinity():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        with open(os.path.join(root, "s1.jsonl"), "w") as fh:
            fh.write(
                '{"type": "assistant", "sessionId": "s1", "cwd": %s, "timestamp": '
                '"2026-06-10T18:46:00.000Z", "uuid": "u1", "parentUuid": null, '
                '"requestId": "r1", "message": {"id": "m1", "model": "claude-opus-4-8", '
                '"role": "assistant", "usage": {"input_tokens": 1e400, "output_tokens": 5, '
                '"cache_read_input_tokens": "twelve"}}}\n' % json.dumps(cwd)
            )
        w = ot.ClaudeStore(os.path.join(tmp, "projects"), _claude_args()).workflows()
        assert len(w) == 1 and w[0].total_tokens == 5  # both bad fields drop to 0


def test_claude_rejoins_a_record_split_by_a_literal_newline():
    # A literal newline splits one JSON record across physical lines.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        prompt = "first line\nsecond line\n\nfourth"  # splits into FOUR physical lines
        user = {
            "type": "user",
            "sessionId": "s1",
            "cwd": cwd,
            "timestamp": "2026-06-10T18:45:00.000Z",
            "uuid": "u0",
            "parentUuid": None,
            "message": {"role": "user", "content": prompt},
        }
        msg = _claude_msg("s1", "claude-opus-4-8", _usage(inp=10, out=5), uuid="u1", cwd=cwd)
        with open(os.path.join(root, "s1.jsonl"), "w") as fh:
            fh.write(json.dumps(user).replace("\\n", "\n") + "\n")  # unescape -> split
            fh.write(json.dumps(msg) + "\n")
        w = ot.ClaudeStore(os.path.join(tmp, "projects"), _claude_args()).workflows()
        assert len(w) == 1 and w[0].title == prompt  # the whole prompt, breaks intact


def test_claude_keeps_a_record_whose_string_holds_a_literal_control_character():
    # A literal tab stays on one physical line but still needs non-strict parsing.
    assert list(ot.ClaudeStore._records('{"a": "x\r\ny"}\r\n{"b": 2}\r\n')) == [
        {"a": "x\r\ny"},
        {"b": 2},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        user = {
            "type": "user",
            "sessionId": "s1",
            "cwd": cwd,
            "timestamp": "2026-06-10T18:45:00.000Z",
            "uuid": "u0",
            "message": {"role": "user", "content": "first\tsecond"},
        }
        msg = _claude_msg("s1", "claude-opus-4-8", _usage(inp=10, out=5), uuid="u1", cwd=cwd)
        with open(os.path.join(root, "s1.jsonl"), "w") as fh:
            fh.write(json.dumps(user).replace("\\t", "\t") + "\n")  # unescape the tab
            fh.write(json.dumps(msg) + "\n")
        w = ot.ClaudeStore(os.path.join(tmp, "projects"), _claude_args()).workflows()
        assert len(w) == 1 and w[0].title == "first\tsecond"


def test_claude_still_refuses_a_literal_control_character_it_cannot_be_split_by():
    for ctrl in ("\x08", "\x1b", "\x00", "\x0b", "\x1f"):
        text = '{"a": "x%sy"}\n{"b": 2}\n' % ctrl
        assert list(ot.ClaudeStore._records(text)) == [{"b": 2}], ctrl
    # ...including one riding inside an otherwise-recoverable split.
    assert list(ot.ClaudeStore._records('{"a": "one\nA\x08B"}\n{"b": 2}\n')) == [{"b": 2}]

    # But a line is judged STRIPPED, because that is what the plain parser judged:
    # str.strip() eats \x0b, \x0c and \x1c..\x1f as whitespace, so a record padded with
    # a form feed has always parsed, and a control scan over the RAW line -- which
    # cannot tell padding from content -- would refuse it. Interior bytes still count.
    rec = '{"type": "assistant", "sessionId": "s1"}'
    for pad in ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", "\t", " "):
        blob = pad + rec + pad + "\n"
        assert list(ot.ClaudeStore._records(blob)) == [json.loads(rec)], repr(pad)
        # and the same, arriving while a rejoin buffer is open
        assert list(ot.ClaudeStore._records('{"cut": "x\n' + blob)) == [json.loads(rec)]
    assert list(ot.ClaudeStore._records('{"a": "x\x0cy"}\n{"b": 2}\n')) == [{"b": 2}]

    # ...and it may not come back in through the rejoin, which buffers the RAW line:
    # a trailing \x0c that was mere padding on a standalone line becomes string content
    # the moment something is joined onto it, so a line is only ever ARMED when its raw
    # form is clean. (A padded whole record never reaches the arming branch; a padded
    # fragment was dropped by the plain parser anyway, so refusing it loses nothing.)
    for pad in ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", "\x08", "\x1b", "\x00"):
        assert list(ot.ClaudeStore._records('{"a":"x%s\ny"}\n' % pad)) == [], repr(pad)

    # Exhaustive rather than sampled, because "which bytes does str.strip() eat" is
    # exactly the question a hand-picked list gets wrong: every byte 0x00..0x20, in
    # every placement, must still yield what the plain parser yielded.
    for code in range(0x00, 0x21):
        ch = chr(code)
        for blob in (
            ch + rec + ch + "\n",  # padded both ends
            ch + rec + "\n",  # leading
            rec + ch + "\n",  # trailing
            ch + "\n" + rec + "\n",  # a line of nothing but the byte
            '{"cut": "x\n' + ch + rec + ch + "\n",  # padded, with a buffer open
        ):
            kept = [
                o
                for o in (_json_or_none(x.strip()) for x in blob.split("\n") if x.strip())
                if isinstance(o, dict)
            ]
            got = list(ot.ClaudeStore._records(blob))
            assert not [r for r in kept if r not in got], (hex(code), blob)
    # An ESCAPED control is ordinary JSON that strict mode always took, and still is --
    # this is only ever about LITERAL bytes.
    assert list(ot.ClaudeStore._records('{"a": "AB\\u0008C"}\n')) == [{"a": "AB\x08C"}]


def test_claude_record_recovery_never_absorbs_a_record_the_plain_parser_kept():
    # Recovery may add dropped records but must preserve every complete object line.
    plain = lambda blob: [  # noqa: E731 - the pre-recovery parser, verbatim
        o
        for o in (_json_or_none(line.strip()) for line in blob.split("\n") if line.strip())
        if isinstance(o, dict)
    ]
    assert list(ot.ClaudeStore._records('":}\n{}\n{"c": 3}\n')) == [{}, {"c": 3}]
    assert list(ot.ClaudeStore._records('{"a":"1\n2\n3"}\n')) == [{"a": "1\n2\n3"}]
    assert list(ot.ClaudeStore._records('{"a":"1\nnull\n3"}\n')) == [{"a": "1\nnull\n3"}]

    rng = random.Random(99)
    fill = ("abc ", '"', "{", "}", ":", ",", "\t", "\r", "\x08", "\x1b", "\n", "\\", "0", "2")
    lost = recovered = 0
    for _ in range(4000):
        parts = []
        for _ in range(rng.randrange(1, 4)):
            if rng.random() < 0.5:  # a real record, sometimes with its escapes undone
                body = "".join(rng.choice(fill) for _ in range(rng.randrange(0, 12)))
                one = json.dumps({"k": body})
                if rng.random() < 0.5:
                    one = one.replace("\\n", "\n").replace("\\t", "\t")
                parts.append(one)
            else:
                parts.append("".join(rng.choice(fill) for _ in range(rng.randrange(1, 20))))
        blob = "\n".join(parts) + "\n"
        before, after = plain(blob), list(ot.ClaudeStore._records(blob))
        assert not [r for r in before if r not in after], blob
        lost += 0
        recovered += len(after) > len(before)
    assert recovered > 100  # ...and the recovery really is firing on this corpus


def _json_or_none(line):
    try:
        return json.loads(line)
    except ValueError:
        return None


def test_claude_does_not_accumulate_a_buffer_over_a_file_of_unterminated_garbage():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        with open(os.path.join(root, "junk.jsonl"), "w") as fh:
            for i in range(5000):
                fh.write('he said "hello %d and this line never terminates\n' % i)
        msg = _claude_msg("s1", "claude-opus-4-8", _usage(inp=10, out=5), uuid="u1", cwd=cwd)
        _write_jsonl(os.path.join(root, "s1.jsonl"), [msg])
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), _claude_args())
        w = store.workflows()
        assert len(w) == 1 and w[0].total_tokens == 15


def _incremental_corpus(tmp):
    # Two ordinary sessions plus a subagent sidecar, which is the case that makes
    # provenance non-trivial: the sidecar's records carry the PARENT's sessionId, so one
    # session is fed by two files whose names share nothing.
    root = os.path.join(tmp, "projects", "slug")
    os.makedirs(os.path.join(root, "s1", "subagents"))
    cwd = os.path.join(tmp, "repo")
    _write_jsonl(
        os.path.join(root, "s1.jsonl"),
        [_claude_msg("s1", "claude-opus-4-8", _usage(100, 50, 0, 0), uuid="a0", cwd=cwd)],
    )
    _write_jsonl(
        os.path.join(root, "s1", "subagents", "agent-x.jsonl"),
        [
            _claude_msg(
                "s1",
                "claude-opus-4-8",
                _usage(40, 10, 0, 0),
                uuid="a1",
                cwd=cwd,
                parent="a0",
                side=True,
            )
        ],
    )
    _write_jsonl(
        os.path.join(root, "s2.jsonl"),
        [_claude_msg("s2", "claude-opus-4-8", _usage(7, 3, 0, 0), uuid="b0", cwd=cwd)],
    )
    return os.path.join(tmp, "projects")


def test_claude_cache_provenance_names_every_file_a_session_was_built_from():
    with tempfile.TemporaryDirectory() as tmp:
        store = ot.ClaudeStore(_incremental_corpus(tmp), type("A", (), {"demo": False})())
        assert store.cache_provenance() == {}  # nothing parsed yet: nothing to claim
        store.workflows()
        prov = store.cache_provenance()
        assert set(prov) == {"s1", "s2"}
        assert [os.path.basename(p) for p in prov["s1"]] == ["s1.jsonl", "agent-x.jsonl"]
        assert [os.path.basename(p) for p in prov["s2"]] == ["s2.jsonl"]


def test_claude_parse_subset_matches_a_full_parse_for_the_files_it_read():
    with tempfile.TemporaryDirectory() as tmp:
        args = type("A", (), {"demo": False})()
        store = ot.ClaudeStore(_incremental_corpus(tmp), args)
        full = {w.id: w for w in store.workflows()}
        full_models = {r["root_id"]: r for r in store.model_breakdown()}

        sliced = ot.ClaudeStore(store.root_dir, args)
        rows, models, prov = sliced.parse_subset(sorted(store.cache_provenance()["s1"]))
        assert [w.id for w in rows] == ["s1"]  # only the sessions living in those files
        assert rows[0] == full["s1"]  # ... and byte-identical to the corpus parse
        assert models[0] == full_models["s1"]
        assert set(prov) == {"s1"}
        # A slice must NOT claim to be the corpus: _session would then read a map holding
        # one session as the whole tree and report every other session as missing.
        assert sliced._sessions is None


def test_claude_parse_subset_refuses_a_replay_capable_transcript():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        rec = _claude_msg("s9", "claude-opus-4-8", _usage(10, 5, 0, 0), uuid="c0", cwd=cwd)
        rec["sessionKind"] = "background"  # the tail marker _replays_history looks for
        _write_jsonl(os.path.join(root, "s9.jsonl"), [rec])
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        assert store.parse_subset([os.path.join(root, "s9.jsonl")]) is None


def test_claude_workflow_order_breaks_ties_deterministically():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        for sid in ("zzz", "aaa"):
            _write_jsonl(
                os.path.join(root, sid + ".jsonl"),
                [_claude_msg(sid, "claude-opus-4-8", _usage(5, 5, 0, 0), uuid=sid, cwd=cwd)],
            )
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        rows = store.workflows()
        assert [w.id for w in rows] == ["aaa", "zzz"]
        assert [w.id for w in store.sort_workflows(list(reversed(rows)))] == ["aaa", "zzz"]


def test_claude_parse_subset_drops_the_single_transcript_memo():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        path = os.path.join(root, "s1.jsonl")
        _write_jsonl(
            path, [_claude_msg("s1", "claude-opus-4-8", _usage(10, 5, 0, 0), uuid="a0", cwd=cwd)]
        )
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        assert len(store.message_timeline("s1")) == 1  # drill in: memoized
        assert store._one is not None
        _write_jsonl(
            path,
            [
                _claude_msg("s1", "claude-opus-4-8", _usage(10, 5, 0, 0), uuid="a0", cwd=cwd),
                _claude_msg("s1", "claude-opus-4-8", _usage(20, 5, 0, 0), uuid="a1", cwd=cwd),
            ],
        )
        store.parse_subset([path])
        assert store._one is None
        assert len(store.message_timeline("s1")) == 2  # re-read, not served stale


def test_claude_parse_subset_refuses_the_slice_that_would_double_count():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        made = _claude_msg(
            "parent", "claude-opus-4-8", _usage(100, 50, 0, 0), uuid="p0", cwd=cwd, mid="m1"
        )
        _write_jsonl(os.path.join(root, "parent.jsonl"), [made])
        replayed = dict(made, sessionId="child", sessionKind="background")
        _write_jsonl(os.path.join(root, "child.jsonl"), [replayed])

        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        full = {w.id: w.total_tokens for w in store.workflows()}
        assert full["parent"] == 150  # the maker claims the call ...
        assert full.get("child", 0) == 0  # ... and the replayer gets nothing

        # A slice over the replaying transcript would report those same 150 tokens again.
        assert store.parse_subset([os.path.join(root, "child.jsonl")]) is None
        # Its parent alone is fine: a first claimer never depends on who replays it.
        rows, _models, _prov = store.parse_subset([os.path.join(root, "parent.jsonl")])
        assert [(w.id, w.total_tokens) for w in rows] == [("parent", 150)]


def test_claude_parse_subset_reads_files_in_the_order_a_full_parse_would():
    # cwd takes the first file while custom/AI titles take the last.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        for name, cwd, title, ts in (
            ("z.jsonl", os.path.join(tmp, "zrepo"), "from-z", "2026-01-01T00:00:00.000Z"),
            ("a.jsonl", os.path.join(tmp, "arepo"), "from-a", "2026-01-02T00:00:00.000Z"),
        ):
            _write_jsonl(
                os.path.join(root, name),
                [
                    _claude_msg("s1", "claude-opus-4-8", _usage(10, 5, 0, 0), uuid=name, cwd=cwd),
                    {
                        "type": "custom-title",
                        "sessionId": "s1",
                        "cwd": cwd,
                        "timestamp": ts,
                        "title": title,
                    },
                ],
            )
        # _files() order is pinned by hand rather than taken from the filesystem: APFS
        # hands back directory entries already sorted, so on macOS glob order and
        # sorted() coincide and this bug is INVISIBLE -- it only shows on a filesystem
        # whose readdir is hash-ordered (ext4). A test that read the real order would
        # pass here and let the regression through on Linux.
        args = type("A", (), {"demo": False})()
        order = [os.path.join(root, "z.jsonl"), os.path.join(root, "a.jsonl")]

        def store():
            st = ot.ClaudeStore(os.path.join(tmp, "projects"), args)
            st._files = lambda: list(order)
            return st

        full = store().workflows()[0]
        rows, _models, _prov = store().parse_subset(list(reversed(order)))
        assert (rows[0].title, rows[0].directory) == (full.title, full.directory)
        assert rows[0] == full


def test_claude_parse_subset_drops_the_corpus_memo_so_a_reload_shows_the_edit():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        path = os.path.join(root, "s1.jsonl")
        rows = [_claude_msg("s1", "claude-opus-4-8", _usage(10, 5, 0, 0), uuid="a0", cwd=cwd)]
        _write_jsonl(path, rows)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        store.workflows()  # cold start: the corpus memo is populated
        assert store._sessions is not None
        assert len(store.message_timeline("s1")) == 1

        rows.append(_claude_msg("s1", "claude-opus-4-8", _usage(20, 5, 0, 0), uuid="a1", cwd=cwd))
        _write_jsonl(path, rows)
        store.parse_subset([path])  # what reload (r) now does instead of workflows()
        assert store._sessions is None and store._one is None
        assert len(store.message_timeline("s1")) == 2


def test_claude_parse_subset_refuses_when_a_requested_file_vanished():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        kept = os.path.join(root, "s1.jsonl")
        _write_jsonl(
            kept, [_claude_msg("s1", "claude-opus-4-8", _usage(10, 5, 0, 0), uuid="a0", cwd=cwd)]
        )
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        assert store.parse_subset([kept]) is not None
        assert store.parse_subset([kept, os.path.join(root, "gone.jsonl")]) is None


def test_claude_parse_subset_refuses_when_a_file_vanishes_mid_read():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(os.path.join(root, "s1", "subagents"))
        cwd = os.path.join(tmp, "repo")
        main = os.path.join(root, "s1.jsonl")
        side = os.path.join(root, "s1", "subagents", "agent-x.jsonl")
        _write_jsonl(
            main, [_claude_msg("s1", "claude-opus-4-8", _usage(100, 50, 0, 0), uuid="a0", cwd=cwd)]
        )
        _write_jsonl(
            side,
            [
                _claude_msg(
                    "s1",
                    "claude-opus-4-8",
                    _usage(40, 10, 0, 0),
                    uuid="a1",
                    cwd=cwd,
                    parent="a0",
                    side=True,
                )
            ],
        )
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        assert store.parse_subset([main, side]) is not None

        # The glob lists both, but the sidecar is gone by the time it is read.
        real_files = store._files()
        os.remove(side)
        store._files = lambda: list(real_files)
        assert store.parse_subset([main, side]) is None


def _trace_records(cwd, sid="s1"):
    """A turn that narrates, calls two tools, and gets both results back."""
    assistant = {
        "type": "assistant",
        "sessionId": sid,
        "cwd": cwd,
        "timestamp": "2026-06-10T18:46:00.000Z",
        "uuid": "u1",
        "requestId": "r1",
        "message": {
            "id": "m1",
            "model": "claude-opus-4-8",
            "role": "assistant",
            "usage": _usage(100, 50, 0, 0),
            "content": [
                # Claude Code writes its thinking blocks EMPTY; only the signature survives.
                {"type": "thinking", "thinking": "", "signature": "CAIS9ww"},
                {"type": "text", "text": "I'll check the diff first."},
                {
                    "type": "tool_use",
                    "id": "call_a",
                    "name": "Bash",
                    "input": {"command": "git diff --stat", "description": "the diff"},
                },
                {
                    "type": "tool_use",
                    "id": "call_b",
                    "name": "Read",
                    "input": {"file_path": "/repo/x.py", "limit": 20},
                },
            ],
        },
    }
    results = {
        "type": "user",
        "sessionId": sid,
        "cwd": cwd,
        "timestamp": "2026-06-10T18:46:02.000Z",
        "uuid": "u2",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_b", "content": "1  import os"},
                {
                    "type": "tool_result",
                    "tool_use_id": "call_a",
                    "content": [{"type": "text", "text": "3 files changed"}],
                },
            ],
        },
    }
    return assistant, results


def test_claude_turn_content_pairs_each_call_with_its_own_result():
    # The trace's whole point: the exact arguments a call ran with, and the output that
    # came back -- matched by tool_use_id, never by position, since results arrive in
    # whatever order they finished.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        _write_jsonl(os.path.join(root, "s1.jsonl"), list(_trace_records(cwd)))
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())

        assert store.supports_turn_content("s1") is True
        content = store.turn_content("s1")
        # The rows and the content meet on a key derived from the RECORD, so a row built
        # by the corpus parse still finds content produced by the (separate) trace parse.
        row = store.message_timeline("s1")[0]
        events = content[row["content_key"]]

        # An empty thinking block emits nothing: a reasoning heading with no prose under
        # it would read as a rendering bug on every Claude turn there is.
        assert [e["kind"] for e in events] == ["text", "tool", "tool"]
        assert events[0]["text"] == "I'll check the diff first."
        assert (events[1]["name"], events[1]["args"]) == ("Bash", "git diff --stat")
        assert events[1]["params"] == [("description", "the diff")]
        assert events[1]["output"] == "3 files changed"  # its own result, logged second
        assert (events[2]["name"], events[2]["args"]) == ("Read", "/repo/x.py")
        assert events[2]["output"] == "1  import os"


def test_claude_streamed_blocks_fold_into_the_one_turn_they_belong_to():
    # A streamed message lands as SEVERAL records, one content block each, all echoing
    # the same usage. The narration is usually in an earlier record than the call, so a
    # trace that read only the first record would show a turn that called nothing.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        first, _ = _trace_records(cwd)
        first["message"]["content"] = [{"type": "text", "text": "thinking about it"}]
        second = json.loads(json.dumps(first))
        second["uuid"] = "u1b"
        second["message"]["content"] = [
            {"type": "tool_use", "id": "call_a", "name": "Bash", "input": {"command": "ls"}}
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), [first, second])
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())

        rows = store.message_timeline("s1")
        assert len(rows) == 1  # one turn, as the usage dedup already sees it
        events = store.turn_content("s1")[rows[0]["content_key"]]
        assert [e["kind"] for e in events] == ["text", "tool"]
        assert events[1]["args"] == "ls"


def test_claude_trace_covers_a_subagents_turns_too():
    # Unlike the composition walk, which is main-thread only: a sidechain turn is a ROW
    # in the same tab, and a row you can select must have something behind it.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        main, _ = _trace_records(cwd)
        side = json.loads(json.dumps(main))
        side.update(uuid="u9", requestId="r9", isSidechain=True, parentUuid="u1")
        side["message"]["id"] = "m9"
        side["message"]["content"] = [
            {"type": "tool_use", "id": "c9", "name": "Grep", "input": {"pattern": "TODO"}}
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), [main, side])
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())

        content = store.turn_content("s1")
        sub = next(r for r in store.message_timeline("s1") if r["depth"])
        assert [e["args"] for e in content[sub["content_key"]]] == ["TODO"]


def test_claude_corpus_parse_carries_no_content_at_all():
    # Content is collected ONLY under the armed single-session parse. Left on, the
    # corpus parse would hold every session's tool output in memory to render a rollup
    # that shows none of it -- and parse_subset, which shares the same walk, would write
    # it into the warm-start cache.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        _write_jsonl(os.path.join(root, "s1.jsonl"), list(_trace_records(cwd)))
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        store.workflows()  # the corpus parse

        assert store._sessions is not None
        assert store._sessions["s1"]["content"] == {}
        # ...and the rows it produced still carry the key the trace parse will answer to.
        assert store.message_timeline("s1")[0]["content_key"]
        assert store.turn_content("s1")


def test_claude_trace_is_refused_for_a_transcript_that_replays_its_parent():
    # Such a file holds another session's records verbatim and cannot be read alone; the
    # corpus parse that could disentangle it collects no content. So there is no trace,
    # rather than a trace showing work this session never did.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        rows = list(_trace_records(cwd))
        rows.append({"type": "summary", "sessionId": "s1", "sessionKind": "background"})
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())

        assert store.supports_turn_content("s1") is False
        assert store.turn_content("s1") == {}


def test_claude_replay_marker_is_a_parsed_field_not_a_string_in_tool_output():
    # The marker is a TOP-LEVEL field of a tail record. A raw substring search also
    # matches it inside a tool RESULT -- a diff, a grep hit, this repo's own source
    # quoted back -- and the false positive is silent: the transcript sorts last,
    # _session widens to the corpus parse, and the trace disappears from a perfectly
    # ordinary session for mentioning a word. Measured over 566 real transcripts,
    # strict and loose agree exactly, so this removes a failure mode and no verdict.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        quoting = {
            "type": "user",
            "sessionId": "s1",
            "cwd": cwd,
            "uuid": "u9",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_a",
                        "content": "grep -n '\"sessionKind\"' src/opentab/stores/claude.py",
                    }
                ],
            },
        }
        assistant, results = _trace_records(cwd)
        _write_jsonl(os.path.join(root, "s1.jsonl"), [assistant, results, quoting])
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        assert store._replays_history(os.path.join(root, "s1.jsonl")) is False
        assert store.supports_turn_content("s1") is True

        # ...while a record that really carries the field is still caught.
        real = dict(quoting, sessionKind="background")
        _write_jsonl(os.path.join(root, "s2.jsonl"), [dict(assistant, sessionId="s2"), real])
        assert store._replays_history(os.path.join(root, "s2.jsonl")) is True


def test_claude_turn_rows_mark_which_turns_have_something_to_read():
    # Two booleans off the content walk the parse already does -- never a content fetch
    # just to mark a row. 60.2% of real Claude turns are pure tool calls, which is why
    # the marker earns a column at all.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        narrating, _ = _trace_records(cwd)
        # A second turn that only calls a tool: no narration, nothing to read.
        silent = json.loads(json.dumps(narrating))
        silent.update(uuid="u5", requestId="r5")
        silent["message"]["id"] = "m5"
        silent["message"]["content"] = [
            {"type": "tool_use", "id": "c5", "name": "Read", "input": {"file_path": "/a"}}
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), [narrating, silent])
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())

        rows = store.message_timeline("s1")
        assert [r["has_text"] for r in rows] == [True, False]
        # Claude writes its thinking blocks empty, so this is False everywhere -- the
        # same fact records_reasoning states, computed rather than assumed.
        assert [r["has_reasoning"] for r in rows] == [False, False]


def test_claude_narration_in_a_later_streamed_record_still_marks_the_turn():
    # A streamed message spans records; the narration is often in a different one from
    # the call. Marking off the first record alone reports a talking turn as silent.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        first, _ = _trace_records(cwd)
        first["message"]["content"] = [{"type": "thinking", "thinking": "", "signature": "x"}]
        second = json.loads(json.dumps(first))
        second["uuid"] = "u1b"
        second["message"]["content"] = [{"type": "text", "text": "here is what I found"}]
        _write_jsonl(os.path.join(root, "s1.jsonl"), [first, second])
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())

        rows = store.message_timeline("s1")
        assert len(rows) == 1 and rows[0]["has_text"] is True


def _kind_file(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def test_replay_detection_never_answers_no_on_a_line_it_could_not_read():
    # Parsing may only OVERRULE the substring when it positively read the line the
    # string is on. A missed replay is the dangerous direction: it sorts before its
    # source transcript and claims the duplicated API calls, so a fragment, a bad byte
    # or an unreachable record keeps the old verdict rather than clearing it.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())

        # 1. The marker record straddles the initial window and another record follows:
        #    the fragment is dropped, so the scan must WIDEN rather than answer no.
        straddle = os.path.join(root, "straddle.jsonl")
        pad = json.dumps({"type": "assistant", "sessionId": "s", "pad": "y" * 9000})
        marker = json.dumps({"type": "summary", "sessionId": "s", "sessionKind": "background"})
        _kind_file(straddle, [pad, marker, json.dumps({"type": "assistant", "sessionId": "s"})])
        assert store._replays_history(straddle) is True

        # 2. A byte that will not decode, on the marker's own line.
        bad = os.path.join(root, "bad.jsonl")
        with open(bad, "wb") as fh:
            fh.write(b'{"type":"summary","sessionKind":"background","x":"\xff\xfe"}\n')
            fh.write(b'{"type":"assistant","sessionId":"s"}\n')
        assert store._replays_history(bad) is True

        # 3. A falsy value is still the field: the old test matched the KEY, and an
        #    empty string is not evidence that this transcript replays nothing.
        falsy = os.path.join(root, "falsy.jsonl")
        _kind_file(falsy, [json.dumps({"type": "summary", "sessionKind": ""})])
        assert store._replays_history(falsy) is True

        # 4. The marker record begins BEFORE the window and only its key lands inside:
        #    the dropped fragment carries the string, so there is no verdict to give.
        boundary = os.path.join(root, "boundary.jsonl")
        _kind_file(
            boundary,
            [
                json.dumps({"type": "summary", "pad": "y" * 9000, "sessionKind": "background"}),
                json.dumps({"type": "assistant", "sessionId": "s"}),
            ],
        )
        assert store._replays_history(boundary) is True

        # 5. An undecodable byte on the marker's line. "replace" would turn it into
        #    U+FFFD, the line would parse as ordinary JSON, and a nested mention would
        #    then read as "not a replay" -- clearing a verdict the substring found.
        hidden = os.path.join(root, "hidden.jsonl")
        with open(hidden, "wb") as fh:
            fh.write(b'{"outer":{"sessionKind":"background"},"bad":"\xff"}\n')
        assert store._replays_history(hidden) is True

        # 6. ...but decodable and genuinely nested is still ordinary content.
        nested = os.path.join(root, "nested.jsonl")
        _kind_file(nested, [json.dumps({"outer": {"sessionKind": "background"}})])
        assert store._replays_history(nested) is False

        # 7. An unreadable marker line inside an ENORMOUS record settles for the
        #    substring's own verdict rather than reading the record to confirm it: a
        #    116 MiB single line otherwise cost 152 MiB of reads at ~451 MiB peak RSS.
        #    Only this path is bounded -- the widening that hunts a record BOUNDARY
        #    stays uncapped, which the oversized-final-record test above pins.
        enormous = os.path.join(root, "enormous.jsonl")
        with open(enormous, "w", encoding="utf-8") as fh:
            pad = "y" * (store._REPLAY_PARSE_MAX_BYTES + (1 << 20))
            fh.write(json.dumps({"type": "assistant", "pad": pad, "o": {"sessionKind": "x"}}))
            fh.write("\n")
        assert store._replays_history(enormous) is True

        # 8. ...while a transcript that merely QUOTES the string still reads as ordinary.
        quoted = os.path.join(root, "quoted.jsonl")
        _kind_file(
            quoted,
            [
                json.dumps({"type": "assistant", "sessionId": "s"}),
                json.dumps({"type": "user", "text": "grep -n '\"sessionKind\"' claude.py"}),
            ],
        )
        assert store._replays_history(quoted) is False
