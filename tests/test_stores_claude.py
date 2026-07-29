"""The Claude Code transcript backend (stores/claude.py)."""

import os
import tempfile

import opentab as ot
from opentab.formatting import iso_to_local

from tests._support import _claude_msg, _usage, _write_jsonl


def test_claude_workflow_last_active_reflects_the_latest_sidechain_activity():
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
        # last_active must reflect it, since the subtree is still active.
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
        assert w.last_active == iso_to_local("2026-06-10T19:10:00.000Z")


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
    # The Turns tab can unfold a prompt, so the timeline keeps its whole text: the
    # one-line group title stays capped, prompt_full is the raw prompt (line breaks
    # kept), and the session-title fallback stays short.
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
    # A session started by a slash command opens with Claude Code's injected
    # messages (meta caveat, <command-name> wrapper). With no ai-title, the title
    # must fall through to the first *real* user prompt, not the scaffolding.
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
    # When the only real user message is "ok" (a continuation/resume stub) and there
    # is no ai-title, opentab honestly shows "ok" rather than inventing a title.
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
    # One streamed assistant message = several records (same message.id/requestId,
    # one content block each): composition must walk every record, the tool_result
    # must resolve its tool name through the pending tool_use id, and the later
    # records' tool calls must fold into the turn the first record opened (the
    # Tools tab fix). Wrapper/meta/compact user messages land in their own buckets.
    # The transcript is written under TWO project slugs (a resumed session's
    # replay): every count below must stay single -- user records need the same
    # record-uuid replay guard as the assistant side.
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
    # Claude Code records cache writes twice: the flat cache_creation_input_tokens, and
    # usage.cache_creation splitting that SAME total into 5-minute and 1-hour TTL halves.
    # opentab read only the flat field, so every 1h write was billed at the 5m rate --
    # 1.25x input where 2.00x is owed. On a real corpus 91% of cache-write tokens were 1h,
    # understating Claude Code spend by 7.5%.
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
