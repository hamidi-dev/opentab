"""parse_args, --status and --goto: session/directory resolution across backends (cli.py)."""

import contextlib
import io
import json
import os
import re
import sqlite3
import sys
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


def test_status_batch_prints_a_table_keyed_by_the_target_asked_for():
    # `status --batch` exists because the interpreter+import start (~90ms) dwarfs the
    # per-target pricing (~20ms), so a shell loop pays the start once per pane. The
    # output is keyed BY TARGET so the caller reads it straight into a map instead of
    # tracking which reply was whose; unpriceable targets are omitted, which is
    # --status's own contract (an empty segment, never an error).
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(
            db,
            [
                ("ses_a", None, "/work/alpha", 1760000000000, 1760000100000, 5.0, 10),
                ("ses_b", None, "/work/beta", 1760000200000, 1760000300000, 2.0, 10),
            ],
        )
        args = ot.cli.parse_args(["status", "--batch", "-"])
        args.db, args.demo = db, False
        args.status_targets = ["ses_b", "ses_gone", "ses_a"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert ot.cli.status_command(args) == 0
        # asked-for order kept, ses_gone omitted rather than given an empty price
        assert out.getvalue() == "ses_b\t$2.00\nses_a\t$5.00\n"


def test_status_batch_prices_a_shared_root_only_once():
    # The win a fan-out of separate processes cannot have: several panes on ONE
    # session (a split, or a subagent pane whose id walks up to the same root) parse
    # that session once. Two ids and a directory all resolving to ses_root must cost
    # exactly one node walk.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(
            db,
            [
                ("ses_root", None, "/work/repo", 1760000000000, 1760000100000, 5.0, 10),
                ("ses_kid", "ses_root", "/work/repo", 1760000001000, 1760000090000, 0.5, 5),
            ],
        )
        store = ot.Store(db, type("A", (), {"demo": False})())
        priced = []
        walk = store.workflow_nodes
        store.workflow_nodes = lambda wid: (priced.append(wid), walk(wid))[1]

        pricer = ot.cli._StatusPricer([store])
        assert pricer.line("ses_root") == "$5.50"  # subtree included
        assert pricer.line("ses_kid") == "$5.50"  # subagent id -> the same root
        assert pricer.line("/work/repo") == "$5.50"  # project's latest -> the same root
        assert priced == ["ses_root"]  # one walk for all three targets


def test_status_batch_targets_keep_the_callers_exact_strings():
    # The list arrives from a shell pipeline, so blank lines are skipped and ONE
    # trailing \r is dropped (a list from a Windows-side tool would otherwise key
    # rows nothing matches). Everything else is left exactly as asked:
    #  - no .strip(): leading/trailing spaces are legal in a Unix path, and eating
    #    them would price a different directory than the caller named;
    #  - no dedup: the output is keyed by the exact string asked for, and asking
    #    twice is free because the pricer memoizes the resolved root.
    assert ot.cli._batch_targets(["ses_a", "", "ses_b\r", "ses_a", "  /w/ dir "]) == [
        "ses_a",
        "ses_b",
        "ses_a",
        "  /w/ dir ",
    ]
    # A tab or NUL can't be represented in a TSV keyed by the target, so it's dropped
    # rather than emitted as a row that won't parse.
    assert ot.cli._batch_targets(["/work/has\ttab", "ses_ok", "nul\0here"]) == ["ses_ok"]
    # `-` takes the whole list from stdin; mixing it with literal targets is a
    # usage mistake, not a merge.
    try:
        ot.cli._batch_targets(["ses_a", "-"])
        raise AssertionError("expected a usage error")
    except ValueError as exc:
        assert "don't mix it" in str(exc)


def test_status_batch_reports_an_incomplete_table_with_a_nonzero_exit():
    # The difference between "these are all the prices" and "these are some of
    # them". A backend that raises mid-sweep still lets the other targets print,
    # but the exit code must say the table is short: the tmux collector replaces
    # its cached prices only on a zero exit, so this is what keeps the previous
    # COMPLETE table instead of publishing one missing live sessions. (Legacy
    # --status deliberately does the opposite -- see status_command.)
    class _Boom:
        def recent_roots(self):
            return []

        def root_of(self, target):
            if target == "ses_bad":
                raise OSError("transcript vanished mid-sweep")
            return target if target == "ses_ok" else None

        def workflow_nodes(self, wid):
            return [{"cost": 1.0, "tokens_total": 0, "model_name": "m"}]

    args = ot.cli.parse_args(["status", "--batch", "-"])
    real = ot.cli._status_stores
    ot.cli._status_stores = lambda a: [_Boom()]
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ot.cli._status_batch(args, ["ses_ok", "ses_bad"])
        assert out.getvalue() == "ses_ok\t$1.00\n"  # the good target still printed
        assert rc == 1  # ...but the table is incomplete, and says so
    finally:
        ot.cli._status_stores = real


def test_status_batch_reads_stdin_but_refuses_a_terminal():
    # `-` means "targets on stdin" (the `export -` convention). A tty would hang
    # waiting for EOF, which from a status-bar hook looks exactly like opentab
    # freezing -- so it's a loud usage error instead. Loud is right here: batch is
    # called by a script being written, not polled by a status bar mid-session.
    class _Pipe(io.StringIO):
        def isatty(self):
            return False

    stdin = sys.stdin
    try:
        sys.stdin = _Pipe("ses_a\n\nses_b\n")
        assert ot.cli._batch_targets(["-"]) == ["ses_a", "ses_b"]

        sys.stdin = type("T", (io.StringIO,), {"isatty": lambda self: True})("")
        args = ot.cli.parse_args(["status", "--batch", "-"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert ot.cli.status_command(args) == 2
        assert "stdin is a terminal" in err.getvalue()
    finally:
        sys.stdin = stdin


def test_status_subcommand_shape_follows_arity_and_keeps_the_legacy_flag():
    # One target keeps the bare line every existing status-bar hook already parses;
    # several switch to the table; --batch forces the table so a script's parsing
    # doesn't change with the number of targets it happened to collect. The old
    # --status flag stays single-target -- it's the deprecated alias, not the surface
    # new callers should reach for.
    one = ot.cli.parse_args(["status", "ses_a"])
    assert (one.status, one.status_targets, one.status_batch) == ("ses_a", ["ses_a"], False)

    two = ot.cli.parse_args(["status", "ses_a", "ses_b"])
    assert (two.status_targets, two.status_batch) == (["ses_a", "ses_b"], True)

    forced = ot.cli.parse_args(["status", "--batch", "ses_a"])
    assert (forced.status_targets, forced.status_batch) == (["ses_a"], True)

    bare = ot.cli.parse_args(["status"])
    assert (bare.status, bare.status_targets, bare.status_batch) == ("", [], False)

    legacy = ot.cli.parse_args(["--status", "ses_a"])
    assert (legacy.status, legacy.status_batch) == ("ses_a", False)


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


# --- subcommands: `opentab web`, `pull`, `remote`, `export`, `forget` ---------
# The verbs are a thin front door: each maps onto the SAME legacy args.* field the
# old flag set, so main() keeps one dispatch path. Bare `opentab` / a path / any
# legacy flag stay the implicit `tui` command (the other tests here still pass them).


def test_bare_and_flags_are_the_implicit_tui_command():
    # No verb named -> tui, with every legacy field present (main() reads them freely).
    assert _parse([]).command == "tui"
    assert _parse(["--web"]).command == "tui"  # a legacy flag doesn't name a subcommand
    a = _parse([])
    assert a.web is False and a.pull is None and a.status is None  # seeded on every namespace


def test_web_subcommand_maps_onto_serve_and_web_fields():
    bare = _parse(["web"])
    assert bare.command == "web" and bare.web is True and bare.serve is False and bare.html is None
    headless = _parse(["web", "--headless"])
    assert headless.web is False and headless.serve is True and headless.html is None
    static = _parse(["web", "--html"])  # bare --html -> the default file
    assert static.web is False and static.serve is False and static.html == "opentab-report.html"
    named = _parse(["web", "--html", "r.html"])
    assert named.html == "r.html" and named.web is False


def test_web_subcommand_takes_the_shared_globals_after_the_verb():
    # parents=[globals]: modifiers land AFTER the verb, e.g. `opentab web --demo`.
    a = _parse(["web", "--demo", "--theme", "nord", "--port", "9000"])
    assert a.command == "web" and a.demo == "all" and a.theme == "nord" and a.port == 9000


def test_pull_subcommand_maps_hosts_onto_the_pull_field():
    assert _parse(["pull"]).pull == []  # bare: refresh the saved machines (== bare --pull)
    assert _parse(["pull", "laptop", "mo@box"]).pull == ["laptop", "mo@box"]
    assert _parse(["pull"]).command == "pull"


def test_remote_export_forget_subcommands_map_onto_legacy_fields():
    assert _parse(["remote"]).remote is True
    assert _parse(["export"]).export == "-"  # stdout by default
    assert _parse(["export", "box.json"]).export == "box.json"
    assert _parse(["forget", "laptop", "old"]).forget == ["laptop", "old"]


def test_explicit_tui_verb_still_reads_legacy_flags():
    # `opentab tui --web ...` is the same as the bare legacy invocation.
    a = _parse(["tui", "--goto", "abc", "--source", "claude"])
    assert a.command == "tui" and a.goto == "abc" and a.source == "claude"


def _subparser_help(name):
    import argparse

    parser = ot.cli._build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices[name].format_help()


def test_verb_help_is_focused_but_globals_still_parse():
    # `opentab pull -h` must not recite every backend path / the theme list -- only the
    # globals that matter to it. But the hidden ones are SUPPRESSED from help, NOT removed:
    # they still parse. (And per-verb hiding must not leak across verbs -- the shared-action
    # trap: forget must still show --remotes even though web/pull hid other globals.)
    pull_help = _subparser_help("pull")
    assert "--remotes" in pull_help and "--demo" in pull_help  # kept
    assert "--claude-dir" not in pull_help and "--theme" not in pull_help  # hidden
    assert "--port" not in pull_help and "--csv" not in pull_help
    assert "--remotes" in _subparser_help("forget")  # no cross-verb leak
    assert "--label" in _subparser_help("export") and "--harness" not in _subparser_help("export")
    assert "--theme" in _subparser_help("web")  # web keeps its relevant ones
    # tui stays the full reference -- nothing hidden there.
    tui_help = _subparser_help("tui")
    assert "--claude-dir" in tui_help and "--theme" in tui_help and "--web" in tui_help
    # Hidden != gone: a suppressed global still parses on that verb.
    assert _parse(["pull", "--no-cache", "host"]).no_cache is True
    assert _parse(["export", "--harness", "claude"]).source == "claude"


def test_version_stays_order_independent_through_the_tui_prepend():
    # --version rides on the shared parent, not just the top level, so it still prints
    # and exits 0 after another flag or a path (the flat parser did; the prepend of the
    # implicit `tui` must not turn it into "unrecognized arguments").
    import contextlib
    import io

    for argv in (["--version"], ["--source", "claude", "--version"], ["/dev/null", "--version"]):
        buf = io.StringIO()
        code = "did-not-exit"
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                _parse(argv)
        except SystemExit as exc:  # the version action exits
            code = exc.code
        assert code == 0, (argv, code)
        assert f"opentab {ot.__version__}" in buf.getvalue(), argv  # never goes stale on a release


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


def test_goto_target_probes_local_backends_under_source_remote():
    # --source remote is the fleet view whose live box IS the local backends;
    # available_sources() never yields "remote", so goto must probe the locals rather
    # than pin to =="remote" (which would empty the key list and never find the session,
    # so --goto/--tab could not open a session in the fleet view).
    sid = "77777777-7777-7777-7777-777777777777"
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        projects = os.path.join(tmp, "projects")
        _write_claude_status_session(projects, sid, repo, 1760000900, _usage(1000, 50))
        db = os.path.join(tmp, "absent-opencode.db")  # not written: only claude present
        args = type(
            "A",
            (),
            {"demo": False, "db": db, "claude_dir": projects, "goto": sid, "source": "remote"},
        )()
        assert ot.cli._goto_target(args) == ("claude", sid)


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


def test_tab_flag_parses_and_stays_out_of_goto_until_main():
    assert _parse([]).tab is None
    assert _parse(["--tab", "context"]).tab == "context"
    # --tab alone leaves goto None at parse time; main() derives the bare --goto.
    assert _parse(["--tab", "context"]).goto is None
    assert _parse(["--goto", "abc", "--tab", "turns"]).goto == "abc"


def test_goto_session_lands_on_the_named_tab_case_insensitively():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert app.goto_session("a", tab="SubAgents") is True
    assert app.view == "session" and app.current_tabs()[app.tab] == "Subagents"


def test_demo_flag_takes_optional_categories():
    assert _parse([]).demo is None  # off
    assert _parse(["--demo"]).demo == "all"  # bare = everything (stays truthy for use_state)
    assert _parse(["--demo", "titles,spend"]).demo == "titles,spend"
    # bare --demo disables state persistence (not args.demo must be False)
    assert not (not _parse(["--demo"]).demo)


def test_goto_session_unknown_tab_keeps_overview_and_names_the_real_ones():
    # A backend without a Context curve (FakeStore) must not error on --tab context:
    # land in the session on Overview and say which tabs it does have -- the tmux
    # popup this was built for must never flash-close over a bad tab name.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    assert app.goto_session("a", tab="context") is True
    assert app.view == "session" and app.current_tabs()[app.tab] == "Overview"
    assert "context" in app.notice and "overview" in app.notice


def test_goto_missing_tab_notice_survives_a_range_clear():
    # Regression: a target hidden by a restored range AND a tab its backend lacks --
    # the range-clear retry must not clobber the "no 'context' tab here" explanation.
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.set_range_from_text("2020-01-01..2020-01-31")  # hides the 2026 session
    assert app.goto_session("a", tab="context") is True
    assert app.view == "session" and app.current_tabs()[app.tab] == "Overview"
    assert "context" in app.notice and "range cleared" not in app.notice


def test_goto_hint_distinguishes_a_fresh_directory_from_an_unknown_id():
    with tempfile.TemporaryDirectory() as tmp:
        assert "no session yet" in ot.cli._goto_hint(tmp)
    hint = ot.cli._goto_hint("99999999-9999-9999-9999-999999999999")
    assert "not found" in hint and "99999999" in hint


def test_goto_miss_opens_the_plain_tui_with_a_hint_instead_of_exiting():
    # A --goto that resolves to nothing (agent just launched, no turn recorded
    # yet) must not exit -- that flash-closes the tmux popup the flag was made
    # for. main opens the ordinary browse view and toasts why it didn't jump.
    import contextlib
    import io
    import sys as _sys

    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(
            db,
            [("ses_oc", None, os.path.join(tmp, "other"), 1760000000000, 1760000500000, 2.0, 10)],
        )
        captured = {}

        class _FakeCurses:
            @staticmethod
            def wrapper(fn):
                captured["app"] = fn.__self__

        argv, real_curses = _sys.argv, ot.cli.curses
        _sys.argv = [
            "opentab",
            "--source",
            "opencode",
            "--db",
            db,
            "--goto",
            repo,
            "--no-state",
            "--no-cache",
        ]
        ot.cli.curses = _FakeCurses
        try:
            with contextlib.redirect_stderr(io.StringIO()):  # the loading hint
                assert ot.cli.main() == 0
        finally:
            _sys.argv, ot.cli.curses = argv, real_curses
        app = captured["app"]
        assert app.view == "browse"
        assert "no session yet" in app.notice
        assert app.toasts[-1].kind == "error"


def test_goto_miss_hint_never_buries_the_notes_warning():
    # refresh_notes' contract: a broken notes.json outranks any caller's own
    # message -- toasts set before the first frame collapse onto the last, so
    # the miss hint must be skipped, leaving the warning on screen.
    import contextlib
    import io
    import sys as _sys

    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, "xdg")
        os.makedirs(os.path.join(cfg, "opentab"))
        with open(os.path.join(cfg, "opentab", "notes.json"), "w") as fh:
            fh.write("{this is not json")
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(
            db,
            [("ses_oc", None, os.path.join(tmp, "other"), 1760000000000, 1760000500000, 2.0, 10)],
        )
        captured = {}

        class _FakeCurses:
            @staticmethod
            def wrapper(fn):
                captured["app"] = fn.__self__

        argv, real_curses = _sys.argv, ot.cli.curses
        xdg = os.environ.get("XDG_DATA_HOME")
        _sys.argv = ["opentab", "--source", "opencode", "--db", db, "--goto", repo, "--no-cache"]
        ot.cli.curses = _FakeCurses
        os.environ["XDG_DATA_HOME"] = cfg  # notes.json lives here, not the suite's dir
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                assert ot.cli.main() == 0
        finally:
            _sys.argv, ot.cli.curses = argv, real_curses
            if xdg is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = xdg
        app = captured["app"]
        assert "unreadable" in app.notice  # the warning is what survived
        assert not any("no session yet" in t.text for t in app.toasts)


def test_web_command_carries_a_saved_group_by_activity_through():
    # The web page now has its own `A` toggle (webpage.py) that re-buckets/re-scopes
    # client-side, so a saved group_by_activity=True should carry into the app this
    # command builds -- both the exported range and the page's own default grouping
    # (meta.groupByActivity) must agree on ended_at, the same coherence the TUI's
    # `A` guarantees.
    import sys as _sys

    with tempfile.TemporaryDirectory() as tmp:
        xdg = os.path.join(tmp, "xdg")
        os.makedirs(os.path.join(xdg, "opentab"))
        with open(os.path.join(xdg, "opentab", "state.json"), "w") as fh:
            json.dump({"group_by_activity": True}, fh)
        db = os.path.join(tmp, "opencode.db")
        _write_status_db(db, [("s1", None, "/work/repo", 1760000000000, 1760099999000, 1.0, 10)])
        out = os.path.join(tmp, "out.html")
        captured = {}

        def _fake_html_command(app, args):
            captured["app"] = app
            return 0

        argv = _sys.argv
        old_state_home = os.environ.get("XDG_STATE_HOME")
        real_html_command = ot.web.html_command
        _sys.argv = ["opentab", "--source", "opencode", "--db", db, "--html", out, "--no-cache"]
        os.environ["XDG_STATE_HOME"] = xdg  # state.json lives under $XDG_STATE_HOME/opentab
        ot.web.html_command = _fake_html_command
        try:
            assert ot.cli.main() == 0
        finally:
            _sys.argv = argv
            ot.web.html_command = real_html_command
            if old_state_home is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_state_home
        app = captured["app"]
        assert app.group_by_activity is True  # the saved True now takes effect here too


# --- --pull / --remote / --forget: consolidating machines (cli.py) ------------


@contextlib.contextmanager
def _remotes_env(cfg_path, fetch=None):
    # Point remotes.json at a temp file (the config path is XDG-global, shared across
    # tests) and optionally stub the SSH/HTTP fetch, restoring both after.
    o_path, o_fetch = ot.cli.remotes_config_path, ot.cli._fetch_summary
    ot.cli.remotes_config_path = lambda: cfg_path
    if fetch is not None:
        ot.cli._fetch_summary = fetch
    try:
        yield
    finally:
        ot.cli.remotes_config_path, ot.cli._fetch_summary = o_path, o_fetch


def _fake_summary_text(label, ids):
    return json.dumps(
        {
            "opentab_export": 1,
            "label": label,
            "records_cost": True,
            "workflows": [
                {
                    "id": i,
                    "title": i,
                    "directory": "/p",
                    "created_at": "2026-07-15 10:00:00",
                    "root_cost": 1.0,
                    "total_cost": 1.0,
                    "subagents": 0,
                    "model_count": 1,
                    "total_tokens": 100,
                    "unpriced_tokens": 0,
                }
                for i in ids
            ],
            "model_breakdown": [],
        }
    )


def test_pull_and_remote_flags_parse():
    assert _parse([]).pull is None and _parse([]).remote is False and _parse([]).forget is None
    assert _parse(["--pull"]).pull == []  # bare: refresh the saved machines
    assert _parse(["--pull", "a", "b"]).pull == ["a", "b"]
    assert _parse(["--remote"]).remote is True
    assert _parse(["--forget", "x", "y"]).forget == ["x", "y"]


def test_remote_entry_parses_hosts_urls_and_named_specs():
    assert ot.cli._remote_entry("box") == ("box", {"ssh": "box"})
    assert ot.cli._remote_entry("mo@host.local") == ("host.local", {"ssh": "mo@host.local"})
    assert ot.cli._remote_entry("build=mo@10.0.0.5") == ("build", {"ssh": "mo@10.0.0.5"})
    name, entry = ot.cli._remote_entry("http://100.64.0.5:8321")
    assert name == "100.64.0.5" and entry == {"url": "http://100.64.0.5:8321"}


def test_remotes_config_round_trips():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "remotes.json")
        with _remotes_env(cfg):
            assert ot.cli._load_remotes() == {}  # missing file is empty, never fatal
            ot.cli._save_remotes({"box": {"ssh": "box"}})
            assert ot.cli._load_remotes() == {"box": {"ssh": "box"}}


def test_pull_learns_hosts_writes_summaries_and_survives_a_failure():
    # Parallel fetch: two machines succeed, one is unreachable -- the failure is
    # reported but never sinks the others, and all three are learned for a later retry.
    def fetch(name, entry, timeout=60.0):
        if name == "broken":
            raise RuntimeError("Connection refused")
        return _fake_summary_text(name, [name + "-s1"])

    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "remotes.json")
        rdir = os.path.join(d, "remotes")
        args = _parse(["--pull", "laptop", "mo@server", "broken", "--remotes", rdir])
        err = io.StringIO()
        with _remotes_env(cfg, fetch), contextlib.redirect_stderr(err):
            ot.cli.pull_command(args)
        machines = json.load(open(cfg, encoding="utf-8"))["machines"]
        assert set(machines) == {"laptop", "server", "broken"}
        assert machines["server"] == {"ssh": "mo@server"}  # name derived, target kept
        assert sorted(os.listdir(rdir)) == ["laptop.json", "server.json"]  # broken wrote nothing
        out = err.getvalue()
        assert "✓ laptop" in out and "✗ broken" in out and "Pulled 2/3" in out


def test_make_refresh_fn_repulls_named_machines_over_the_pull_workers():
    # The in-app refresh (the TUI F key / the web /api/refresh) reuses the same fetch +
    # save as --pull, but only for the requested remotes keys, and returns per-machine
    # results the UI turns into a toast.
    def fetch(name, entry, timeout=60.0):
        return _fake_summary_text(name, [name + "-a", name + "-b"])

    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "remotes.json")
        rdir = os.path.join(d, "remotes")
        with _remotes_env(cfg, fetch):
            ot.cli._save_remotes({"server": {"ssh": "server"}, "desktop": {"ssh": "root@desktop"}})
            fn = ot.cli._make_refresh_fn(_parse(["--remote", "--remotes", rdir]))
            results = dict((n, (c, e)) for n, c, e in fn(["server"]))
            assert results == {"server": (2, "")}  # only server, its 2 sessions
            assert os.listdir(rdir) == ["server.json"]  # desktop was not touched
            # The written summary reads straight back through RemoteStore.
            store = ot.RemoteStore(rdir, _parse([]))
            assert {w.id for w in store.workflows()} == {"server-a", "server-b"}


def test_pull_with_no_hosts_refreshes_every_saved_machine():
    def fetch(name, entry, timeout=60.0):
        return _fake_summary_text(name, [name + "-s1"])

    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "remotes.json")
        rdir = os.path.join(d, "remotes")
        with _remotes_env(cfg):
            ot.cli._save_remotes({"laptop": {"ssh": "laptop"}, "server": {"ssh": "mo@server"}})
        args = _parse(["--pull", "--remotes", rdir])  # bare --pull
        with _remotes_env(cfg, fetch), contextlib.redirect_stderr(io.StringIO()):
            ot.cli.pull_command(args)
        assert sorted(os.listdir(rdir)) == ["laptop.json", "server.json"]


def test_forget_removes_a_machine_and_its_cached_summary():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "remotes.json")
        rdir = os.path.join(d, "remotes")
        os.makedirs(rdir)
        with open(os.path.join(rdir, "laptop.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        with _remotes_env(cfg):
            ot.cli._save_remotes({"laptop": {"ssh": "laptop"}, "server": {"ssh": "server"}})
        args = _parse(["--forget", "laptop", "--remotes", rdir])
        with _remotes_env(cfg), contextlib.redirect_stderr(io.StringIO()):
            assert ot.cli.forget_command(args) == 0
        assert set(json.load(open(cfg, encoding="utf-8"))["machines"]) == {"server"}
        assert not os.path.exists(os.path.join(rdir, "laptop.json"))


def test_summary_filename_encodes_distinct_names_without_collision():
    # `a/b` and `a_b` must not map to the same cache file (else a pull overwrites, and
    # --forget deletes the wrong machine's summary).
    assert ot.cli._summary_filename("a/b") != ot.cli._summary_filename("a_b")
    assert "/" not in ot.cli._summary_filename("a/b")  # no separator escapes the dir


def test_load_remotes_drops_malformed_entries_and_pull_never_sinks():
    # A hand-edited null entry must not crash the parallel pull: _load_remotes filters
    # it, and even reaching _fetch_summary with a bad entry is caught, not raised.
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "remotes.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "machines": {"ok": {"ssh": "ok"}, "bad": None}}, fh)
        with _remotes_env(cfg):
            assert set(ot.cli._load_remotes()) == {"ok"}  # null "bad" dropped
        count, err = ot.cli._pull_one("bad", None, d)  # even if it slipped through
        assert count == 0 and err  # a failure line, not an exception


def test_pull_relearn_swaps_url_target_to_ssh():
    def fetch(name, entry, timeout=60.0):
        return _fake_summary_text(name, [name + "-s1"])

    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "remotes.json")
        rdir = os.path.join(d, "remotes")
        with _remotes_env(cfg):
            ot.cli._save_remotes({"box": {"url": "http://old:8321"}})
        args = _parse(["--pull", "box=newhost", "--remotes", rdir])
        with _remotes_env(cfg, fetch), contextlib.redirect_stderr(io.StringIO()):
            ot.cli.pull_command(args)
        entry = json.load(open(cfg, encoding="utf-8"))["machines"]["box"]
        assert entry == {"ssh": "newhost"}  # old url dropped, not merged


def test_summary_filename_is_never_a_hidden_file():
    # RemoteStore globs "*.json" (skips dotfiles), so a "."-leading name must not write
    # a hidden summary the remote view then can't see.
    assert not ot.cli._summary_filename(".box").startswith(".")
    assert not ot.cli._summary_filename("..").startswith(".")


def test_pull_repairs_a_cmd_only_saved_entry():
    # A hand-edited entry with a cmd but no ssh/url would be unreachable; a bare
    # `--pull box` must fold in an ssh target derived from the name, not reuse it as-is.
    calls = []

    def fetch(name, entry, timeout=60.0):
        calls.append((name, dict(entry)))
        return _fake_summary_text(name, [name + "-s1"])

    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "remotes.json")
        rdir = os.path.join(d, "remotes")
        with _remotes_env(cfg):
            ot.cli._save_remotes({"box": {"cmd": "/opt/opentab --export -"}})
        args = _parse(["--pull", "box", "--remotes", rdir])
        with _remotes_env(cfg, fetch), contextlib.redirect_stderr(io.StringIO()):
            ot.cli.pull_command(args)
        assert calls and calls[0][1].get("ssh") == "box"
        saved = json.load(open(cfg, encoding="utf-8"))["machines"]["box"]
        assert saved.get("ssh") == "box" and saved.get("cmd") == "/opt/opentab --export -"


def _fleet_wf(id, machine, source, cost=1.0, tokens=100):
    # A workflow tagged with a machine + harness, for the --timings fleet breakdown.
    w = workflow(id, "2026-07-15 10:00:00", cost=cost, tokens=tokens)
    w.machine = machine
    w.source = source
    return w


class _RemoteSub:
    # Stands in for RemoteStore in a --timings backend row: the one sub-store that
    # answers machine_stats() (per-box bytes), so _fleet_timing_tables treats it as the
    # pulled read and everything else as a live-machine harness.
    def __init__(self, stats):
        self._stats = stats

    def machine_stats(self):
        return list(self._stats)


class _MetaStore:
    def __init__(self, meta):
        self.machine_meta = meta


def test_fleet_aggregate_rolls_up_by_machine_and_harness():
    wfs = [
        _fleet_wf("a", "laptop", "opencode", cost=5.0, tokens=1000),
        _fleet_wf("b", "laptop", "claude", cost=0.0, tokens=2000),
        _fleet_wf("c", "server", "opencode", cost=2.0, tokens=500),
    ]
    by_machine, by_harness, cell = ot.cli._fleet_aggregate(wfs)
    # sessions, tokens, cost, est -- est defaults to cost with no estimate map
    assert by_machine["laptop"] == [2, 3000, 5.0, 5.0]
    assert by_harness["opencode"] == [2, 1500, 7.0, 7.0]  # spans two machines
    assert cell["server"]["opencode"] == [1, 500, 2.0, 2.0]


def test_fleet_aggregate_adds_the_list_price_estimate_per_session():
    # The est column = real cost + the session's unpriced tokens at list rates. The b
    # session (a $0 subscription row) gets a $3 estimate; a's real $5 has nothing to add.
    wfs = [
        _fleet_wf("a", "laptop", "opencode", cost=5.0, tokens=1000),
        _fleet_wf("b", "laptop", "claude", cost=0.0, tokens=2000),
    ]
    by_machine, by_harness, _cell = ot.cli._fleet_aggregate(wfs, {"b": 3.0})
    assert by_machine["laptop"] == [2, 3000, 5.0, 8.0]  # est adds b's $3
    assert by_harness["claude"] == [1, 2000, 0.0, 3.0]  # $0 real -> $3 estimated


def test_fleet_estimated_costs_prices_unpriced_tokens():
    # The per-root estimate = the row's unpriced tokens at list rates, summed per root_id.
    # Mirrors App._compute_api_costs, so assert it equals a direct api_equivalent_cost call.
    from opentab.pricing import api_equivalent_cost

    row = {
        "root_id": "s1",
        "model_name": "anthropic/claude-sonnet-5",
        "cost": 0.0,
        "unpriced_input": 1000,
        "unpriced_output": 500,
        "unpriced_reasoning": 0,
        "unpriced_cache_read": 2000,
        "unpriced_cache_write": 0,
    }
    backends = [["Claude Code", 1, 10.0, False, object(), [], [row]]]
    est = ot.cli._fleet_estimated_costs(backends)
    expect = api_equivalent_cost("anthropic/claude-sonnet-5", 1000, 500, 0, 2000, 0)
    assert est == {"s1": expect}
    assert est["s1"] > 0  # a priced model -> a real estimate
    # A backend row without model rows (older fixture) contributes nothing, never crashes.
    assert ot.cli._fleet_estimated_costs([["OpenCode", 1, 5.0, False, object(), []]]) == {}


def test_fleet_timings_show_the_list_price_estimate_column():
    # Two subscription ($0) Claude boxes: the real cost column is all $0, but the est
    # column reprices their unpriced tokens at list rates -- the fleet's "$" view.
    laptop = [_fleet_wf("s1", "laptop", "claude", cost=0.0, tokens=3000)]
    server = [_fleet_wf("s2", "server", "claude", cost=0.0, tokens=1000)]

    def mrow(sid, cr):
        return {
            "root_id": sid,
            "model_name": "anthropic/claude-sonnet-5",
            "cost": 0.0,
            "unpriced_input": 500,
            "unpriced_output": 200,
            "unpriced_reasoning": 0,
            "unpriced_cache_read": cr,
            "unpriced_cache_write": 0,
        }

    store = _MetaStore({"laptop": {"live": True}, "server": {"live": False, "exported_at": ""}})
    remote = _RemoteSub([{"label": "server", "sessions": 1, "bytes": 2048}])
    backends = [
        ["Claude Code", 1, 100.0, False, object(), laptop, [mrow("s1", 3000)]],
        ["remote", 1, 0.1, False, remote, server, [mrow("s2", 800)]],
    ]
    text = "\n".join(ot.cli._fleet_timing_tables(store, backends, uni=True))
    assert "est $" in text  # the estimate column is present
    assert "list-price estimate" in text  # and its footnote
    assert "$0.00" in text  # real cost stays $0 for subscription rows


def test_fleet_timings_hide_the_estimate_when_everything_is_metered():
    # A fully metered fleet (real cost == estimate) omits the est column -- it would just
    # duplicate the cost column. Here the rows have no unpriced tokens.
    laptop = [_fleet_wf("s1", "laptop", "opencode", cost=5.0, tokens=3000)]
    server = [_fleet_wf("s2", "server", "opencode", cost=2.0, tokens=1000)]
    store = _MetaStore({"laptop": {"live": True}, "server": {"live": False, "exported_at": ""}})
    remote = _RemoteSub([{"label": "server", "sessions": 1, "bytes": 2048}])

    # model rows carry real cost and zero unpriced tokens -> estimate == cost
    def priced(sid, c):
        return {"root_id": sid, "model_name": "anthropic/claude-sonnet-5", "cost": c}

    backends = [
        ["OpenCode", 1, 100.0, False, object(), laptop, [priced("s1", 5.0)]],
        ["remote", 1, 0.1, False, remote, server, [priced("s2", 2.0)]],
    ]
    text = "\n".join(ot.cli._fleet_timing_tables(store, backends, uni=True))
    assert "est $" not in text and "list-price estimate" not in text


def test_box_table_is_a_bordered_grid_with_a_titled_top_rule():
    lines = ot.cli._box_table(
        "Cap", ["name", "n"], [["x", "1"], ["total", "9"]], aligns="lr", rule_before_last=True
    )
    assert lines[0].startswith("  ┌") and lines[0].endswith("┐") and "Cap" in lines[0]  # titled top
    assert lines[-1].startswith("  └") and lines[-1].endswith("┘")  # bottom border
    assert any(ln.startswith("  ├") for ln in lines)  # a mid rule (header + before total)
    body = [ln for ln in lines if ln.startswith("  │")]
    assert body[0].split("│")[1].strip() == "name"  # header cell
    assert body[-1].split("│")[1].strip() == "total"  # total row last
    # the ASCII fallback swaps the glyphs for a non-UTF-8 terminal
    ascii_lines = ot.cli._box_table("Cap", ["name"], [["x"]], aligns="l", uni=False)
    assert ascii_lines[0].startswith("  +") and "│" not in "\n".join(ascii_lines)


def test_fleet_timings_break_down_by_machine_harness_and_grid():
    # The --timings fleet breakdown: By machine (live box first, pulled boxes with size),
    # By harness (across the fleet), and the machine x harness session grid. Load time is
    # the live machine's parse (300 + 1500 ms) -- pulled boxes arrive pre-rolled. uni=True
    # pins the UTF-8 glyph set so the assertions don't depend on the test host's locale.
    laptop_oc = [_fleet_wf("a", "laptop", "opencode", cost=5.0, tokens=1000)]
    laptop_cc = [_fleet_wf("b", "laptop", "claude", cost=0.0, tokens=2000)]
    omv_oc = [_fleet_wf("c", "server", "opencode", cost=2.0, tokens=500)]
    store = _MetaStore({"laptop": {"live": True}, "server": {"live": False, "exported_at": ""}})
    remote = _RemoteSub([{"label": "server", "sessions": 1, "bytes": 2048}])
    backends = [
        ["OpenCode", 3, 300.0, False, object(), laptop_oc],
        ["Claude Code", 5, 1500.0, False, object(), laptop_cc],
        ["remote", 1, 0.1, False, remote, omv_oc],
    ]
    text = "\n".join(ot.cli._fleet_timing_tables(store, backends, uni=True))
    assert "By machine" in text and "By harness" in text and "machine × harness" in text
    assert "● laptop" in text and "○ server" in text  # live vs pulled markers
    assert "OpenCode" in text and "Claude Code" in text
    assert "1800.0 ms" in text  # the live box's summed harness parse
    assert "2 KB" in text  # server's summary size
    assert "Σ" in text  # the grid's totals row/column
    assert "┌" in text and "├" in text and "└" in text  # rendered as ruled boxes


def test_fleet_timings_include_a_zero_session_pulled_box():
    # A valid empty export (a box with opentab but no usage yet) has no workflow rows, so
    # it never reaches the rollup -- seed it from machine_meta/machine_stats so it still
    # shows as an idle member and doesn't collapse the whole breakdown below the fleet
    # threshold (here: live box + one empty pulled box, a single harness).
    laptop = [_fleet_wf("a", "laptop", "opencode", cost=5.0, tokens=1000)]
    store = _MetaStore({"laptop": {"live": True}, "idle": {"live": False, "exported_at": ""}})
    remote = _RemoteSub([{"label": "idle", "sessions": 0, "bytes": 512}])
    backends = [
        ["OpenCode", 3, 300.0, False, object(), laptop],
        ["remote", 1, 0.1, False, remote, []],  # idle contributed no workflows
    ]
    text = "\n".join(ot.cli._fleet_timing_tables(store, backends, uni=True))
    assert "By machine" in text  # the breakdown didn't collapse to nothing
    assert "idle" in text and "512 B" in text  # the empty box still appears, with its size


def test_fleet_timings_use_ascii_glyphs_on_a_non_utf8_terminal():
    # A non-UTF-8 locale gets the ASCII fallback everywhere -- borders AND the content
    # glyphs (markers/dot/sigma/times), so nothing lands as a garbage byte.
    laptop_oc = [_fleet_wf("a", "laptop", "opencode", cost=5.0)]
    omv_oc = [_fleet_wf("c", "server", "claude", cost=0.0)]
    store = _MetaStore({"laptop": {"live": True}, "server": {"live": False, "exported_at": ""}})
    remote = _RemoteSub([{"label": "server", "sessions": 1, "bytes": 2048}])
    backends = [
        ["OpenCode", 3, 300.0, False, object(), laptop_oc],
        ["remote", 1, 0.1, False, remote, omv_oc],
    ]
    text = "\n".join(ot.cli._fleet_timing_tables(store, backends, uni=False))
    assert text.isascii()  # not a single multibyte glyph slips through
    assert "* laptop" in text and "- server" in text and "machine x harness" in text


def test_fleet_timings_are_empty_for_a_single_source_local_run():
    # One box, one harness -> nothing to break down; --timings prints only its usual table.
    backends = [["OpenCode", 3, 300.0, False, object(), [_fleet_wf("a", "", "opencode")]]]
    assert ot.cli._fleet_timing_tables(_MetaStore({}), backends) == []


def test_pull_with_timings_refreshes_the_fleet_before_profiling():
    # main() returns from the --timings branch BEFORE it reaches the --pull step, so
    # `opentab --pull --timings` would profile a stale fleet and the machine ages never
    # move off "Nh ago". timings_command must run the pull itself. The heavy store build
    # is stubbed -- the point is only that the pull fetched and rewrote the summaries.
    def fetch(name, entry, timeout=60.0):
        return _fake_summary_text(name, [name + "-s1"])

    class _Stub:
        source_name = "stub"

        def workflows(self):
            return []

        def model_breakdown(self):
            return []

    saved = (
        ot.cli.sources.available_sources,
        ot.cli.resolve_source,
        ot.cli.sources.make_store,
        ot.cli._fleet_timing_tables,
    )
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "remotes.json")
        rdir = os.path.join(d, "remotes")
        with _remotes_env(cfg):
            ot.cli._save_remotes({"server": {"ssh": "server"}})
        ot.cli.sources.available_sources = lambda args: []
        ot.cli.resolve_source = lambda args, state: "remote"
        ot.cli.sources.make_store = lambda args, key: (_Stub(), "")
        ot.cli._fleet_timing_tables = lambda *a, **k: []
        try:
            args = _parse(["--pull", "--timings", "--remotes", rdir])
            with _remotes_env(cfg, fetch), contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    assert ot.cli.timings_command(args) == 0
        finally:
            (
                ot.cli.sources.available_sources,
                ot.cli.resolve_source,
                ot.cli.sources.make_store,
                ot.cli._fleet_timing_tables,
            ) = saved
        assert os.listdir(rdir) == ["server.json"]  # the pull fetched and wrote the summary


def test_remote_timings_without_pull_never_fetches():
    # Without --pull, `opentab --remote --timings` must only READ the cached summaries --
    # profiling shouldn't trigger a network fetch. A fetch here would raise and fail.
    def fetch(name, entry, timeout=60.0):
        raise AssertionError("--remote --timings must not pull")

    class _Stub:
        source_name = "stub"

        def workflows(self):
            return []

        def model_breakdown(self):
            return []

    saved = (
        ot.cli.sources.available_sources,
        ot.cli.resolve_source,
        ot.cli.sources.make_store,
        ot.cli._fleet_timing_tables,
    )
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "remotes.json")
        rdir = os.path.join(d, "remotes")
        with _remotes_env(cfg):
            ot.cli._save_remotes({"server": {"ssh": "server"}})
        ot.cli.sources.available_sources = lambda args: []
        ot.cli.resolve_source = lambda args, state: "remote"
        ot.cli.sources.make_store = lambda args, key: (_Stub(), "")
        ot.cli._fleet_timing_tables = lambda *a, **k: []
        try:
            args = _parse(["--remote", "--timings", "--remotes", rdir])
            with _remotes_env(cfg, fetch), contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    assert ot.cli.timings_command(args) == 0  # no fetch, no raise
        finally:
            (
                ot.cli.sources.available_sources,
                ot.cli.resolve_source,
                ot.cli.sources.make_store,
                ot.cli._fleet_timing_tables,
            ) = saved


def test_demo_rejects_a_value_that_is_not_a_category():
    """--demo takes an OPTIONAL value, so argparse eats the next positional: `--demo
    requests.csv` bound the path to --demo, and `export --demo out.json` wrote the summary
    to stdout while out.json was never created. parse_demo_cats drops unknown names on
    purpose (an empty result means "everything"), which hides the swallowed filename -- so
    the command line rejects it instead."""
    for argv in (["--demo", "requests.csv"], ["export", "--demo", "out.json"], ["--demo", "turnz"]):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                ot.cli.parse_args(argv)
                raise AssertionError(f"{argv} should have been rejected")
            except SystemExit:
                pass
        assert "--demo: unknown categor" in err.getvalue()

    # The real category specs still work, bare --demo still means everything, and a path
    # can still be passed either side of the flag.
    assert ot.cli.parse_args(["--demo", "titles,spend"]).demo == "titles,spend"
    assert ot.cli.parse_args(["--demo"]).demo == "all"


def test_export_under_demo_does_not_leak_the_real_hostname():
    """--export pairs with --demo for a shareable summary, so the machine label -- a real
    hostname, i.e. identity exactly like a title or a path -- must be scrambled with the
    rest, behind the same `titles` gate RemoteStore uses at display time."""
    import socket
    import sys as _sys

    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "r.csv")
        with open(log, "w") as fh:
            fh.write("timestamp,model,input_tokens,output_tokens,project\n")
            fh.write("2026-07-01T10:00:00Z,gpt-4o,100000,5000,/tmp/proj\n")

        def export(*extra):
            argv = _sys.argv
            _sys.argv = ["opentab", "export", "-", "--csv", log, "--source", "csv", *extra]
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    ot.cli.main()
            finally:
                _sys.argv = argv
            return json.loads(out.getvalue())

        assert export()["label"] == (socket.gethostname() or "machine")  # real run: real name
        assert export("--demo")["label"] != socket.gethostname()
        # `titles` off means names stay real everywhere, so the label must follow.
        assert export("--demo", "spend")["label"] == (socket.gethostname() or "machine")
