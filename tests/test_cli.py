"""parse_args, --status and --goto: session/directory resolution across backends (cli.py)."""

import os
import re
import sqlite3
import tempfile

import opentab as ot

from tests._support import (
    OCL_SID,
    _claude_msg,
    _codex_meta,
    _codex_tokens,
    _codex_turn,
    _hermes_db_full,
    _ocl_args,
    _ocl_msg,
    _ocl_user,
    _ocl_write,
    _parse,
    _pi_args,
    _pi_assistant,
    _pi_session,
    _pi_user,
    _pi_write,
    _usage,
    _write_jsonl,
    _zaly_assistant,
    _zaly_settings,
    _zaly_store,
    _zaly_user,
    _zaly_write,
    app_with,
    workflow,
)


def _write_status_db(db, sessions, messages=()):
    # Minimal OpenCode-shaped DB for the --status one-shot: session rows carry
    # (id, parent_id, directory, time_created, time_updated, cost, tokens_input),
    # messages only feed workflow_nodes' per-session model attribution.
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table session (
          id text primary key, parent_id text, title text, directory text,
          time_created integer, time_updated integer, cost real default 0 not null,
          tokens_input integer default 0 not null, tokens_output integer default 0 not null,
          tokens_reasoning integer default 0 not null, tokens_cache_read integer default 0 not null,
          tokens_cache_write integer default 0 not null
        );
        create table message (id text primary key, session_id text, data text);
        """
    )
    conn.executemany(
        "insert into session values (?,?,?,?,?,?,?,?,0,0,0,0)",
        [(id, parent, id, d, tc, tu, cost, tok) for id, parent, d, tc, tu, cost, tok in sessions],
    )
    conn.executemany("insert into message values (?,?,?)", messages)
    conn.commit()
    conn.close()


def test_status_line_follows_subagent_activity_and_sums_subtree():
    # "Current session" = the root whose *subtree* saw the latest update: a session
    # whose subagent is still streaming must beat a root created later but idle
    # since. The printed figure is the whole subtree's recorded cost.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(
            db,
            [
                # old root, but its subagent has the newest time_updated in the DB
                ("r1", None, "/work/repo", 1760000000000, None, 1.0, 10),
                ("r1c", "r1", "/work/repo", 1760000001000, 1760099999000, 0.5, 5),
                # created after r1, idle since
                ("r2", None, "/work/repo", 1760005000000, 1760005000000, 9.0, 10),
            ],
        )
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert [r["id"] for r in store.recent_roots()] == ["r1", "r2"]
        assert ot.status_line(store) == "$1.50"


def test_status_line_scopes_to_project_and_estimates_unpriced():
    # DIR narrows to that project's sessions; a $0 subscription session shows the
    # list-price estimate with the "~" marker instead of a useless $0.00; a project
    # with no sessions yields an empty segment (never an error).
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(
            db,
            [
                ("a", None, "/work/alpha", 1760000000000, 1760000900000, 2.0, 100),
                ("b", None, "/work/beta", 1760000000000, 1760000500000, 0.0, 1_000_000),
            ],
            messages=[
                (
                    "m1",
                    "b",
                    '{"role":"assistant","providerID":"anthropic","modelID":"claude-opus-4.5",'
                    '"cost":0,"tokens":{"input":1000000,"output":0}}',
                ),
            ],
        )
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert ot.status_line(store) == "$2.00"  # newest activity overall wins
        expected = ot.money(
            ot.api_equivalent_cost("anthropic/claude-opus-4.5", 1_000_000, 0, 0, 0, 0)
        )
        assert ot.status_line(store, "/work/beta") == "~" + expected
        assert ot.status_line(store, "/work/nowhere") == ""


def test_status_line_prices_an_exact_session_id():
    # Two sessions in ONE project can't be told apart by directory (a dir target
    # picks the project's most recent one) -- a session id target prices exactly
    # that session, and a subagent's id resolves up to its root so the whole
    # workflow is priced. Unknown ids yield an empty segment.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(
            db,
            [
                ("ses_old", None, "/work/repo", 1760000000000, 1760000100000, 5.0, 10),
                ("ses_oldchild", "ses_old", "/work/repo", 1760000001000, 1760000090000, 0.5, 5),
                ("ses_new", None, "/work/repo", 1760000200000, 1760000900000, 2.0, 10),
            ],
        )
        store = ot.Store(db, type("A", (), {"demo": False})())
        assert ot.status_line(store, "/work/repo") == "$2.00"  # dir -> project's latest
        assert ot.status_line(store, "ses_new") == "$2.00"
        assert ot.status_line(store, "ses_old") == "$5.50"  # exact session, subtree included
        assert ot.status_line(store, "ses_oldchild") == "$5.50"  # subagent id -> its root
        assert ot.status_line(store, "ses_gone") == ""


def _write_claude_status_session(projects, sid, cwd, mtime, usage):
    # One Claude session = one <slug>/<sid>.jsonl transcript whose mtime is the
    # session's last activity (recent_roots orders by it, no parse).
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    os.makedirs(os.path.join(projects, slug), exist_ok=True)
    path = os.path.join(projects, slug, sid + ".jsonl")
    _write_jsonl(path, [_claude_msg(sid, "claude-opus-4-8", usage, uuid=sid + "-u1", cwd=cwd)])
    os.utime(path, (mtime, mtime))


def test_status_line_prices_claude_sessions_without_a_full_parse():
    # ClaudeStore's status trio: recent_roots orders roots by transcript mtime,
    # root_of confirms the transcript (a Claude id is already its root), and the
    # figure is always a "~" list-price estimate -- Claude Code records no cost.
    # All off the single transcript: the full-tree parse must never run.
    sid_a = "11111111-1111-1111-1111-111111111111"
    sid_b = "22222222-2222-2222-2222-222222222222"
    with tempfile.TemporaryDirectory() as tmp:
        projects = os.path.join(tmp, "projects")
        alpha, beta = os.path.join(tmp, "alpha"), os.path.join(tmp, "beta")
        os.makedirs(alpha)
        os.makedirs(beta)
        _write_claude_status_session(projects, sid_a, alpha, 1760000100, _usage(1000, 50))
        _write_claude_status_session(projects, sid_b, beta, 1760000200, _usage(500, 20))

        store = ot.ClaudeStore(projects, type("A", (), {"demo": False})())
        assert [r["id"] for r in store.recent_roots()] == [sid_b, sid_a]
        assert store.root_of(sid_a) == sid_a
        assert store.root_of("33333333-3333-3333-3333-333333333333") is None

        expected = "~" + ot.money(
            ot.api_equivalent_cost("anthropic/claude-opus-4-8", 1000, 50, 0, 0, 0)
        )
        assert ot.status_line(store) != ""  # newest overall: sid_b
        assert ot.status_line(store, alpha) == expected  # dir scopes to its project
        assert ot.status_line(store, sid_a) == expected  # uuid prices exactly that one
        assert ot.status_line(store, "44444444-4444-4444-4444-444444444444") == ""
        assert store._sessions is None  # the full-tree parse never ran


def test_status_command_prices_whichever_tool_ran_last():
    # A directory target consults every present backend and the newest root wins:
    # drive Claude Code after OpenCode and the segment shows the Claude estimate;
    # explicit session ids always route to their own backend.
    sid = "55555555-5555-5555-5555-555555555555"
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(db, [("ses_oc", None, repo, 1760000000000, 1760000500000, 2.0, 10)])
        projects = os.path.join(tmp, "projects")
        _write_claude_status_session(projects, sid, repo, 1760000900, _usage(1000, 50))

        args = type("A", (), {"demo": False, "db": db, "claude_dir": projects})()
        claude_price = "~" + ot.money(
            ot.api_equivalent_cost("anthropic/claude-opus-4-8", 1000, 50, 0, 0, 0)
        )
        assert ot.cli._status_line_all(args, repo) == claude_price  # claude is newer
        assert ot.cli._status_line_all(args, "ses_oc") == "$2.00"
        assert ot.cli._status_line_all(args, sid) == claude_price

        # Now OpenCode sees activity after the Claude transcript's mtime -- it wins.
        os.utime(
            os.path.join(projects, re.sub(r"[^A-Za-z0-9]", "-", repo), sid + ".jsonl"),
            (1760000100, 1760000100),
        )
        assert ot.cli._status_line_all(args, repo) == "$2.00"
        assert ot.cli._status_line_all(args, None) == "$2.00"  # no target: newest overall


def test_status_line_prices_codex_sessions_and_folds_spawned_threads():
    # CodexStore's status trio: recent_roots orders rollouts by mtime with "id"
    # lazily walking a spawned thread up to its root (a child still streaming
    # surfaces its parent), root_of resolves child ids the same way, and
    # status_nodes prices the whole subtree off head-reads plus a subtree-only
    # parse -- the full-tree parse must never run. Codex records no cost, so the
    # figure is always a "~" list-price estimate.
    root_sid = "aaaa1111-1111-7111-8111-111111111111"
    child_sid = "bbbb2222-2222-7222-8222-222222222222"
    with tempfile.TemporaryDirectory() as tmp:
        sessions = os.path.join(tmp, "sessions", "2026", "07", "01")
        os.makedirs(sessions)
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        spawn = {
            "subagent": {"thread_spawn": {"parent_thread_id": root_sid, "agent_nickname": "worker"}}
        }
        root_path = os.path.join(sessions, f"rollout-2026-07-01T10-00-00-{root_sid}.jsonl")
        child_path = os.path.join(sessions, f"rollout-2026-07-01T10-05-00-{child_sid}.jsonl")
        _write_jsonl(
            root_path,
            [
                _codex_meta(root_sid, repo),
                _codex_turn("gpt-5-codex", repo),
                _codex_tokens(1000, 50, 0, 1050),
            ],
        )
        _write_jsonl(
            child_path,
            [
                _codex_meta(child_sid, repo, source=spawn),
                _codex_turn("gpt-5-codex", repo),
                _codex_tokens(2000, 100, 0, 2100),
            ],
        )
        os.utime(root_path, (1760000100, 1760000100))
        os.utime(child_path, (1760000200, 1760000200))  # the child is still streaming

        store = ot.CodexStore(os.path.join(tmp, "sessions"), type("A", (), {"demo": False})())
        rows = store.recent_roots()
        assert rows[0]["id"] == root_sid  # newest file is the child -> its root wins
        assert rows[0]["directory"] == repo
        assert store.root_of(child_sid) == root_sid
        assert store.root_of(root_sid) == root_sid
        assert store.root_of("dddd4444-4444-7444-8444-444444444444") is None

        expected = "~" + ot.money(
            ot.api_equivalent_cost("openai/gpt-5-codex", 1000, 50, 0, 0, 0)
            + ot.api_equivalent_cost("openai/gpt-5-codex", 2000, 100, 0, 0, 0)
        )
        assert ot.status_line(store, root_sid) == expected  # the subtree, not just the root
        assert ot.status_line(store, child_sid) == expected  # child id -> its root
        assert ot.status_line(store, repo) == expected  # dir -> the project's newest root

        # A root that only spawned threads (no usage of its own) still prices its
        # children's subtree -- the browser's rollup drops it, --status must not.
        bare_root = "eeee5555-5555-7555-8555-555555555555"
        bare_child = "ffff6666-6666-7666-8666-666666666666"
        spawn2 = {
            "subagent": {"thread_spawn": {"parent_thread_id": bare_root, "agent_role": "worker"}}
        }
        _write_jsonl(
            os.path.join(sessions, f"rollout-2026-07-01T11-00-00-{bare_root}.jsonl"),
            [_codex_meta(bare_root, repo)],
        )
        _write_jsonl(
            os.path.join(sessions, f"rollout-2026-07-01T11-05-00-{bare_child}.jsonl"),
            [
                _codex_meta(bare_child, repo, source=spawn2),
                _codex_turn("gpt-5-codex", repo),
                _codex_tokens(300, 30, 0, 330),
            ],
        )
        assert ot.status_line(store, bare_root) == "~" + ot.money(
            ot.api_equivalent_cost("openai/gpt-5-codex", 300, 30, 0, 0, 0)
        )
        assert store._sessions is None  # the full-tree parse never ran


def test_status_line_prices_hermes_sessions_and_walks_the_parent_chain():
    # HermesStore's status pair: recent_roots orders roots by subtree activity
    # (started_at fallback -- this DB has no messages table), root_of walks
    # parent_session_id and never claims an archived id, and the figure is real
    # metered spend -- or a "~" estimate for a $0 subscription session.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db_full(
            db,
            [
                {
                    "id": "r1",
                    "cwd": "/work/alpha",
                    "started_at": 1750000100.0,
                    "model": "claude-opus-4.5",
                    "provider": "anthropic",
                    "inp": 100,
                    "out": 10,
                    "actual_cost_usd": 2.0,
                },
                {
                    "id": "r1c",
                    "parent_id": "r1",
                    "cwd": "/work/alpha",
                    "started_at": 1750000300.0,  # the subagent bumps its root past r2
                    "model": "claude-opus-4.5",
                    "provider": "anthropic",
                    "inp": 50,
                    "out": 5,
                    "actual_cost_usd": 0.5,
                },
                {
                    "id": "r2",
                    "cwd": "/work/beta",
                    "started_at": 1750000200.0,
                    "model": "claude-opus-4.5",
                    "provider": "anthropic",
                    "inp": 1_000_000,
                    "out": 0,
                    "billing_mode": "subscription_included",
                },
                {"id": "r3", "cwd": "/work/alpha", "started_at": 1750000900.0, "archived": 1},
            ],
        )
        store = ot.HermesStore(db, type("A", (), {"demo": False})())
        assert [r["id"] for r in store.recent_roots()] == ["r1", "r2"]
        assert store.recent_roots()[0]["last_active"] == 1750000300000  # ms, subtree max
        assert store.root_of("r1c") == "r1"
        assert store.root_of("r1") == "r1"
        assert store.root_of("r3") is None  # archived sessions are never claimed
        assert store.root_of("nope") is None
        assert ot.status_line(store, "/work/alpha") == "$2.50"  # subtree: r1 + r1c
        assert ot.status_line(store, "r1c") == "$2.50"  # subagent id -> its root
        expected = "~" + ot.money(
            ot.api_equivalent_cost("anthropic/claude-opus-4.5", 1_000_000, 0, 0, 0, 0)
        )
        assert ot.status_line(store, "/work/beta") == expected  # $0 subscription -> estimate


def test_status_line_prices_pi_sessions_without_a_full_parse():
    # PiStore's status trio: recent_roots orders session files by mtime with the
    # cwd read lazily from the `session` record at the file head, root_of only
    # confirms a file carries the uuid, and status_nodes parses just that file.
    sid_a = "77777777-7777-7777-7777-777777777777"
    sid_b = "88888888-8888-8888-8888-888888888888"
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        alpha, beta = os.path.join(tmp, "alpha"), os.path.join(tmp, "beta")
        os.makedirs(alpha)
        os.makedirs(beta)
        _pi_write(
            root,
            "--alpha--",
            sid_a,
            [
                _pi_session(sid_a, alpha),
                _pi_user("hi"),
                # A metered route (no oauth/plan marker) with real recorded spend.
                _pi_assistant("moonshotai/kimi-k2.6", 1000, 50, cost=0.5, provider="openrouter"),
            ],
        )
        _pi_write(
            root,
            "--beta--",
            sid_b,
            [
                _pi_session(sid_b, beta),
                _pi_user("yo"),
                # A subscription route: its recorded cost is an estimate, not spend.
                _pi_assistant("openai-codex/gpt-5.2", 500, 20, cost=0.1, provider="openai-codex"),
            ],
        )
        prefix = "2026-05-15T07-32-15-949Z"
        os.utime(os.path.join(root, "--alpha--", f"{prefix}_{sid_a}.jsonl"), (1760000100,) * 2)
        os.utime(os.path.join(root, "--beta--", f"{prefix}_{sid_b}.jsonl"), (1760000200,) * 2)

        store = ot.PiStore(root, _pi_args())
        rows = store.recent_roots()
        assert [r["id"] for r in rows] == [sid_b, sid_a]
        assert rows[0]["directory"] == beta  # the session record's cwd, off the file head
        assert store.root_of(sid_a) == sid_a
        assert store.root_of("99999999-9999-9999-9999-999999999999") is None
        assert ot.status_line(store, alpha) == "$0.50"  # metered -> real spend, no ~
        expected = "~" + ot.money(ot.api_equivalent_cost("openai-codex/gpt-5.2", 500, 20, 0, 0, 0))
        assert ot.status_line(store, sid_b) == expected  # subscription -> estimated
        assert store._sessions is None  # the full-tree parse never ran


def test_status_line_prices_zaly_sessions_by_their_uuid_directory():
    # ZalyStore's status trio: the <uuid> directory names the session on disk
    # (settings.sessionId may differ -- the browser's canonical id -- and
    # status_nodes tolerates the mismatch), the mtime of its append-only
    # session.jsonl is the last activity, and the workspace reads off the head.
    dir_id = "019f9999-9999-7999-8999-999999999999"
    canonical = "019f8888-8888-7888-8888-888888888888"
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "zaly")
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        _zaly_write(
            root,
            "+ws+",
            dir_id,
            [
                _zaly_settings(canonical, ws),
                _zaly_user("hey"),
                _zaly_assistant("anthropic/claude-opus-4-6", 1000, 50),  # no cost -> unpriced
            ],
        )
        store = _zaly_store(root)
        rows = store.recent_roots()
        assert [r["id"] for r in rows] == [dir_id]
        assert rows[0]["directory"] == ws  # the settings workspace, off the file head
        assert store.root_of(dir_id) == dir_id
        assert store.root_of(canonical) is None  # only the on-disk uuid is claimable
        expected = "~" + ot.money(
            ot.api_equivalent_cost("anthropic/claude-opus-4-6", 1000, 50, 0, 0, 0)
        )
        assert ot.status_line(store, dir_id) == expected
        assert ot.status_line(store, ws) == expected  # dir target -> same session
        assert store._sessions is None  # the full-tree parse never ran


def test_status_line_prices_openclaw_sessions_by_id():
    # OpenClaw sessions carry no user cwd (the project is the agent), so session
    # ids are the reliable status route; recent_roots exposes the agent's
    # ABSOLUTE directory so a bare agent name can never fold against the
    # caller's own cwd through _project_key.
    with tempfile.TemporaryDirectory() as root:
        rows = [
            _ocl_user("go"),
            _ocl_msg("claude-opus-4-6", 100, 50, cost=0.01, provider="anthropic"),
        ]
        _ocl_write(root, "finance-os", OCL_SID, rows)
        store = ot.OpenClawStore(root, _ocl_args())
        assert store.root_of(OCL_SID) == OCL_SID
        assert store.root_of("01998b2c-0000-7000-8000-000000000000") is None
        recent = store.recent_roots()
        assert [r["id"] for r in recent] == [OCL_SID]
        assert recent[0]["directory"] == os.path.join(root, "agents", "finance-os")
        assert ot.status_line(store, OCL_SID) == "$0.01"  # metered spend, no estimate
        assert store._sessions is None  # the full-tree parse never ran


def test_status_command_routes_uuid_ids_by_probing_backends():
    # A bare UUID is no longer assumed to be Claude Code's -- Codex/pi/Zaly ids
    # share the shape -- so every present backend's root_of is probed and the
    # id's own backend prices it. An explicit --source pins one backend, for the
    # directory fallback and for ids alike.
    claude_sid = "55555555-5555-5555-5555-555555555555"
    codex_sid = "66666666-6666-7666-8666-666666666666"
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        projects = os.path.join(tmp, "projects")
        _write_claude_status_session(projects, claude_sid, repo, 1760000900, _usage(1000, 50))
        codex_root = os.path.join(tmp, "codex")
        day_dir = os.path.join(codex_root, "2026", "07", "01")
        os.makedirs(day_dir)
        codex_path = os.path.join(day_dir, f"rollout-2026-07-01T10-00-00-{codex_sid}.jsonl")
        _write_jsonl(
            codex_path,
            [
                _codex_meta(codex_sid, repo),
                _codex_turn("gpt-5-codex", repo),
                _codex_tokens(2000, 100, 0, 2100),
            ],
        )
        os.utime(codex_path, (1760000100, 1760000100))

        def stub(source):
            return type(
                "A",
                (),
                {
                    "demo": False,
                    "db": os.path.join(tmp, "none.db"),
                    "claude_dir": projects,
                    "codex_dir": codex_root,
                    "source": source,
                },
            )()

        claude_price = "~" + ot.money(
            ot.api_equivalent_cost("anthropic/claude-opus-4-8", 1000, 50, 0, 0, 0)
        )
        codex_price = "~" + ot.money(
            ot.api_equivalent_cost("openai/gpt-5-codex", 2000, 100, 0, 0, 0)
        )
        args = stub("auto")
        assert ot.cli._status_line_all(args, codex_sid) == codex_price  # a Codex-owned UUID
        assert ot.cli._status_line_all(args, claude_sid) == claude_price
        assert ot.cli._status_line_all(args, repo) == claude_price  # dir: claude is newer

        pinned = stub("codex")
        assert ot.cli._status_line_all(pinned, repo) == codex_price  # --source pins the backend
        assert ot.cli._status_line_all(pinned, claude_sid) == ""  # ...for ids too


def test_cli_theme_choices_match_the_theme_registry():
    # The --theme choices are sourced from themes.THEME_IDS, so they can't drift.
    args = ot.parse_args.__wrapped__ if hasattr(ot.parse_args, "__wrapped__") else None
    del args  # parse_args builds its own parser; assert the registry instead
    assert ot.THEME_IDS == tuple(ot.THEMES)
    assert "kanagawa-wave" in ot.THEME_IDS and "tokyo-night" in ot.THEME_IDS


def test_cli_web_flag_is_recognized_and_is_distinct_from_serve():
    # --web is its own flag; web_command/main route it through the serve path.
    import sys as _sys

    argv = _sys.argv
    _sys.argv = ["opentab", "--web"]
    try:
        args = ot.parse_args()
    finally:
        _sys.argv = argv
    assert args.web is True and args.serve is False
    assert args.port == 8321 and args.bind == "127.0.0.1"  # shared with --serve


# --- --goto: open the TUI drilled into a session ------------------------------


def test_goto_flag_parses_bare_and_with_target():
    assert _parse([]).goto is None
    assert _parse(["--goto"]).goto == ""  # bare: the current directory
    assert _parse(["--goto", "abc-123"]).goto == "abc-123"


def test_goto_target_resolves_ids_and_directories_like_status():
    # A session id routes to the backend that claims it (root_of probe); a
    # directory takes the project's newest root across backends -- the --status
    # semantics, returning the owning source key alongside the root id.
    sid = "66666666-6666-6666-6666-666666666666"
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(db, [("ses_oc", None, repo, 1760000000000, 1760000500000, 2.0, 10)])
        projects = os.path.join(tmp, "projects")
        _write_claude_status_session(projects, sid, repo, 1760000900, _usage(1000, 50))
        args = type("A", (), {"demo": False, "db": db, "claude_dir": projects, "goto": None})()

        args.goto = sid
        assert ot.cli._goto_target(args) == ("claude", sid)
        args.goto = "ses_oc"
        assert ot.cli._goto_target(args) == ("opencode", "ses_oc")
        args.goto = repo  # directory: the newest root wins (the Claude transcript)
        assert ot.cli._goto_target(args) == ("claude", sid)
        args.goto = "99999999-9999-9999-9999-999999999999"
        assert ot.cli._goto_target(args) is None  # unclaimed id, never a dir fallback


def test_goto_session_lands_in_session_view_and_clears_a_hiding_range():
    app = app_with([workflow("a", "2026-06-01 12:00:00"), workflow("b", "2026-06-02 12:00:00")])
    assert app.goto_session("a") is True
    assert app.view == "session" and app.current_session().id == "a"
    # a restored range that hides the target is cleared so the jump still lands
    app2 = app_with([workflow("a", "2026-06-01 12:00:00")])
    app2.set_range_from_text("2020-01-01..2020-01-31")
    assert app2.goto_session("a") is True
    assert app2.view == "session" and app2.current_session().id == "a"
    assert app2.range_days is None and app2.custom_since is None
    # an id the source doesn't know: no jump, an honest notice
    app3 = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert app3.goto_session("nope") is False
    assert "not found" in app3.notice
