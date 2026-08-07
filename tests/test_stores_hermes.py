"""The Hermes Agent SQLite backend: mixed subscription/metered cost (stores/hermes.py)."""

import os
import sqlite3
import tempfile

import opentab as ot

from tests._support import FakeStore, _empty_opencode_db, _hermes_db_full, workflow

# --- Hermes Agent database helpers (~/.hermes/state.db) ----------------------


def _hermes_db(path, rows):
    """Create a minimal Hermes state.db with only the columns HermesStore reads."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            cwd TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r["id"],
                r.get("title", r["id"]),
                r.get("model", "gpt-5"),
                r.get("cwd", ""),
                r.get("parent_id"),
                r.get("started_at", 1750000000.0),
                r.get("inp", 0),
                r.get("out", 0),
                r.get("cr", 0),
                r.get("cw", 0),
                r.get("reasoning", 0),
                r.get("archived", 0),
            )
            for r in rows
        ],
    )
    conn.commit()
    conn.close()


def test_hermes_store_loads_tokens_and_rolls_up_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        _hermes_db(
            db,
            [
                {
                    "id": "sess1",
                    "title": "Fix the bug",
                    "model": "gpt-5.5",
                    "cwd": sub,
                    "started_at": 1750000000.0,
                    "inp": 1000,
                    "out": 500,
                    "cr": 200,
                    "cw": 50,
                }
            ],
        )

        args = type("Args", (), {"demo": False})()
        store = ot.HermesStore(db, args)
        workflows = store.workflows()

        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == "sess1"
        assert w.title == "Fix the bug"
        assert w.directory == repo  # folded to git root, not bare "sub"
        assert w.source == "Hermes"
        assert w.subagents == 0
        assert w.total_cost == 0.0 and w.root_cost == 0.0  # subscription; $ reprices
        assert w.total_tokens == 1000 + 500 + 200 + 50
        assert w.unpriced_tokens == w.total_tokens
        assert len(w.created_at) == 19  # YYYY-MM-DD HH:MM:SS

        rows = store.model_breakdown()
        assert len(rows) == 1
        r = rows[0]
        assert r["root_id"] == "sess1"
        assert r["model_name"] == "openai/gpt-5.5"  # provider-prefixed
        assert r["cost"] == 0.0
        assert r["tokens_total"] == 1750
        assert r["unpriced_input"] == 1000
        assert r["unpriced_output"] == 500
        assert r["unpriced_cache_read"] == 200
        assert r["unpriced_cache_write"] == 50
        # no subagents -> root_unpriced_* equals the total
        assert r["root_unpriced_input"] == 1000
        assert r["root_unpriced_output"] == 500

        nodes = store.workflow_nodes("sess1")
        assert len(nodes) == 1
        assert nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["model_name"] == "openai/gpt-5.5"
        assert nodes[0]["tokens_total"] == 1750
        assert nodes[0]["cost"] == 0.0

        # tokens are unpriced -> list-price estimate under $ is positive
        est = ot.api_equivalent_cost("openai/gpt-5.5", 1000, 500, 0, 200, 50)
        assert est > 0


def test_hermes_untitled_sessions_fall_back_to_first_user_prompt():
    # Hermes never titles api_server/voice sessions; the first real user prompt
    # becomes the title. Injected "[ ... ]" note blocks are stripped, a voice
    # turn's quoted transcript is mined out of its block, and a note-only first
    # message falls through to the next user message.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(
            db,
            [
                {"id": "s1", "title": "", "inp": 10, "out": 5},
                {"id": "s2", "title": "", "inp": 10, "out": 5},
                {"id": "s3", "title": "", "inp": 10, "out": 5},
                {"id": "titled", "title": "Real Title", "inp": 10, "out": 5},
            ],
        )
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL
            )"""
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
            [
                ("s1", "user", "[Note: model was just switched.]\n\nhow do i configure this", 1.0),
                ("s1", "assistant", "like so", 2.0),
                (
                    "s2",
                    "user",
                    '[The user sent a voice message~ Here\'s what they said: "Hallo, kannst du mich verstehen?"]',
                    1.0,
                ),
                ("s3", "user", "[CONTEXT COMPACTION -- REFERENCE]", 1.0),
                ("s3", "user", "  real question here  ", 2.0),
                ("titled", "user", "must not be used", 1.0),
            ],
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {"demo": False})()
        titles = {w.id: w.title for w in ot.HermesStore(db, args).workflows()}
        assert titles["s1"] == "how do i configure this"
        assert titles["s2"] == "Hallo, kannst du mich verstehen?"
        assert titles["s3"] == "real question here"
        assert titles["titled"] == "Real Title"


def test_hermes_untitled_without_messages_table_stays_untitled():
    # An old/partial state.db without a messages table must not crash the parse.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "title": "", "inp": 10, "out": 5}])
        args = type("Args", (), {"demo": False})()
        (w,) = ot.HermesStore(db, args).workflows()
        assert w.title == "(untitled)"


def test_hermes_store_rolls_child_session_into_parent_subtotal():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "project")
        os.makedirs(cwd)
        _hermes_db(
            db,
            [
                # root session
                {
                    "id": "root1",
                    "title": "Root task",
                    "model": "gpt-5.5",
                    "cwd": cwd,
                    "started_at": 1750000000.0,
                    "inp": 100,
                    "out": 50,
                    "cr": 0,
                    "cw": 0,
                },
                # child session (subagent)
                {
                    "id": "child1",
                    "title": "Subagent run",
                    "model": "gpt-5.5",
                    "cwd": cwd,
                    "parent_id": "root1",
                    "started_at": 1750001000.0,
                    "inp": 400,
                    "out": 200,
                    "cr": 100,
                    "cw": 0,
                },
            ],
        )

        args = type("Args", (), {"demo": False})()
        store = ot.HermesStore(db, args)
        workflows = store.workflows()

        # only the root surfaces as a top-level workflow
        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == "root1"
        assert w.subagents == 1
        # total = root (100+50) + child (400+200+100) = 850
        assert w.total_tokens == 850
        assert w.unpriced_tokens == 850

        # model_breakdown: root_unpriced_* is the root's own tokens only
        rows = store.model_breakdown()
        assert len(rows) == 1
        r = rows[0]
        assert r["tokens_total"] == 850
        assert r["unpriced_input"] == 100 + 400  # root + child
        assert r["root_unpriced_input"] == 100  # root only
        assert r["unpriced_output"] == 50 + 200
        assert r["root_unpriced_output"] == 50

        # workflow_nodes: depth-0 root + depth-1 child
        nodes = store.workflow_nodes("root1")
        assert len(nodes) == 2
        root_node, child_node = nodes
        assert root_node["depth"] == 0 and root_node["agent"] == "-"
        assert root_node["tokens_total"] == 150  # root's own tokens only
        assert child_node["depth"] == 1 and child_node["agent"] == "subagent"
        assert child_node["tokens_total"] == 700  # child's tokens
        assert child_node["title"] == "Subagent run"
        assert root_node["cost"] == 0.0 and child_node["cost"] == 0.0


def test_hermes_ended_at_reflects_latest_message_in_subtree():
    # ended_at tracks the newest message anywhere in the subtree (root or
    # subagent alike), not just the root's own started_at.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "project")
        os.makedirs(cwd)
        _hermes_db(
            db,
            [
                {
                    "id": "root1",
                    "title": "Root task",
                    "cwd": cwd,
                    "started_at": 1750000000.0,
                    "inp": 10,
                    "out": 5,
                },
                {
                    "id": "child1",
                    "parent_id": "root1",
                    "cwd": cwd,
                    "started_at": 1750001000.0,
                    "inp": 20,
                    "out": 10,
                },
            ],
        )
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL
            )"""
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
            [
                ("root1", "user", "go", 1750000000.0),
                ("child1", "user", "subagent work", 1750005000.0),  # newest, subtree-wide
            ],
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {"demo": False})()
        (w,) = ot.HermesStore(db, args).workflows()
        assert w.created_at == ot.HermesStore._ts_to_local(1750000000.0)
        assert w.ended_at == ot.HermesStore._ts_to_local(1750005000.0)
        assert w.ended_at != w.created_at


def test_hermes_ended_at_is_blank_without_messages_table():
    # An old/partial state.db without a messages table has no evidence of activity
    # beyond the start -- ended_at goes blank (not a same-as-created_at value), so
    # the "last_activity" sort's own empty-string check is what falls it back to
    # created_at, not a value HermesStore fabricates here.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "started_at": 1750000000.0, "inp": 10, "out": 5}])
        args = type("Args", (), {"demo": False})()
        (w,) = ot.HermesStore(db, args).workflows()
        assert w.ended_at == ""
        assert w.worked_seconds is None


def test_hermes_store_rolls_grandchild_session_into_subtotal():
    """Depth-2+ sessions must be included in aggregate totals and node list."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "project")
        os.makedirs(cwd)
        _hermes_db(
            db,
            [
                {"id": "root1", "model": "gpt-5", "cwd": cwd, "inp": 10, "out": 5},
                {
                    "id": "child1",
                    "parent_id": "root1",
                    "model": "gpt-5",
                    "cwd": cwd,
                    "inp": 20,
                    "out": 10,
                },
                {
                    "id": "grand1",
                    "parent_id": "child1",
                    "model": "gpt-5",
                    "cwd": cwd,
                    "inp": 40,
                    "out": 20,
                },
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        workflows = store.workflows()
        assert len(workflows) == 1
        w = workflows[0]
        assert w.total_tokens == 10 + 5 + 20 + 10 + 40 + 20  # all three sessions
        assert w.subagents == 2  # child + grandchild

        rows = store.model_breakdown()
        assert len(rows) == 1
        assert rows[0]["tokens_total"] == w.total_tokens

        nodes = store.workflow_nodes("root1")
        assert len(nodes) == 3
        assert nodes[0]["depth"] == 0
        assert nodes[1]["depth"] == 1
        assert nodes[2]["depth"] == 2


def test_hermes_store_splits_model_rows_by_child_model():
    """Tokens from a child using a different model must appear in a separate model_row."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "project")
        os.makedirs(cwd)
        _hermes_db(
            db,
            [
                {"id": "root1", "model": "gpt-5", "cwd": cwd, "inp": 100, "out": 50},
                {
                    "id": "child1",
                    "parent_id": "root1",
                    "model": "gpt-4o",
                    "cwd": cwd,
                    "inp": 200,
                    "out": 100,
                },
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        rows = store.model_breakdown()
        assert len(rows) == 2  # one row per distinct model
        by_model = {r["model_name"]: r for r in rows}
        assert "openai/gpt-5" in by_model
        assert "openai/gpt-4o" in by_model

        gpt5 = by_model["openai/gpt-5"]
        assert gpt5["unpriced_input"] == 100
        assert gpt5["root_unpriced_input"] == 100  # root session used this model

        gpt4o = by_model["openai/gpt-4o"]
        assert gpt4o["unpriced_input"] == 200
        assert gpt4o["root_unpriced_input"] == 0  # root did not use gpt-4o


def test_hermes_store_excludes_archived_sessions():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(
            db,
            [
                {"id": "live", "inp": 100, "out": 50},
                {"id": "archived", "inp": 200, "out": 100, "archived": 1},
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        ids = {w.id for w in store.workflows()}
        assert ids == {"live"}
        assert "archived" not in ids


def test_hermes_metered_session_uses_recorded_cost():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "proj")
        os.makedirs(cwd)
        _hermes_db_full(
            db,
            [
                {
                    "id": "m1",
                    "model": "claude-sonnet-4",
                    "provider": "anthropic",  # billing_provider -> display prefix
                    "billing_mode": "official_docs_snapshot",
                    "cwd": cwd,
                    "inp": 1000,
                    "out": 500,
                    "cr": 200,
                    "cw": 50,
                    "estimated_cost_usd": 0.12,
                    "actual_cost_usd": 0.34,  # reconciled actual is preferred
                }
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert store.records_cost is True  # a metered session exists

        w = store.workflows()[0]
        assert w.total_cost == 0.34 and w.root_cost == 0.34
        assert w.unpriced_tokens == 0  # priced -> "$" must not reprice it
        assert w.total_tokens == 1750

        r = store.model_breakdown()[0]
        assert r["model_name"] == "anthropic/claude-sonnet-4"  # from billing_provider
        assert r["cost"] == 0.34 and r["root_cost"] == 0.34
        assert r["input"] == 1000 and r["tokens_total"] == 1750  # tokens still in full
        assert r["unpriced_input"] == 0 and r["root_unpriced_input"] == 0

        assert store.workflow_nodes("m1")[0]["cost"] == 0.34


def test_hermes_estimated_cost_used_when_actual_absent():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "proj")
        os.makedirs(cwd)
        _hermes_db_full(
            db,
            [
                {
                    "id": "e1",
                    "model": "gpt-5.5",
                    "provider": "openrouter",
                    "cwd": cwd,
                    "inp": 1000,
                    "out": 500,
                    "estimated_cost_usd": 0.21,
                    "actual_cost_usd": None,
                }
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert store.records_cost is True
        w = store.workflows()[0]
        assert w.total_cost == 0.21  # falls back to estimated_cost_usd
        assert w.unpriced_tokens == 0


def test_hermes_subscription_session_stays_unpriced():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "proj")
        os.makedirs(cwd)
        _hermes_db_full(
            db,
            [
                {
                    "id": "s1",
                    "model": "gpt-5.5",
                    "provider": "openai-codex",
                    "billing_mode": "subscription_included",
                    "cwd": cwd,
                    "inp": 1000,
                    "out": 500,
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": None,
                }
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert store.records_cost is False  # no recorded cost anywhere

        w = store.workflows()[0]
        assert w.total_cost == 0.0
        assert w.unpriced_tokens == 1500  # all tokens are unpriced -> "$" estimates them

        r = store.model_breakdown()[0]
        assert r["model_name"] == "openai/gpt-5.5"  # openai-codex -> openai
        assert r["cost"] == 0.0 and r["unpriced_input"] == 1000


def test_hermes_mixed_subscription_and_metered():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "proj")
        os.makedirs(cwd)
        _hermes_db_full(
            db,
            [
                {
                    "id": "sub",
                    "model": "gpt-5.5",
                    "provider": "openai-codex",
                    "cwd": cwd,
                    "inp": 1000,
                    "out": 500,
                    "estimated_cost_usd": 0.0,
                },
                {
                    "id": "paid",
                    "model": "claude-opus-4",
                    "provider": "anthropic",
                    "cwd": cwd,
                    "inp": 2000,
                    "out": 800,
                    "actual_cost_usd": 1.50,
                },
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert store.records_cost is True  # at least one metered session

        by_id = {w.id: w for w in store.workflows()}
        assert by_id["sub"].total_cost == 0.0 and by_id["sub"].unpriced_tokens == 1500
        assert by_id["paid"].total_cost == 1.50 and by_id["paid"].unpriced_tokens == 0


def test_hermes_subtree_prices_each_session_independently():
    """A metered root with a $0 subscription child: only the child stays unpriced."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        cwd = os.path.join(tmp, "proj")
        os.makedirs(cwd)
        _hermes_db_full(
            db,
            [
                {
                    "id": "root",
                    "model": "claude-opus-4",
                    "provider": "anthropic",
                    "cwd": cwd,
                    "inp": 100,
                    "out": 50,
                    "actual_cost_usd": 0.40,
                },
                {
                    "id": "child",
                    "parent_id": "root",
                    "model": "gpt-5.5",
                    "provider": "openai-codex",
                    "cwd": cwd,
                    "inp": 400,
                    "out": 200,
                    "estimated_cost_usd": 0.0,
                },
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        w = store.workflows()[0]
        assert w.id == "root"
        assert w.total_cost == 0.40  # root metered + child $0
        assert w.root_cost == 0.40
        assert w.unpriced_tokens == 600  # only the subscription child's tokens

        rows = {r["model_name"]: r for r in store.model_breakdown()}
        assert rows["anthropic/claude-opus-4"]["cost"] == 0.40
        assert rows["anthropic/claude-opus-4"]["unpriced_input"] == 0
        assert rows["openai/gpt-5.5"]["cost"] == 0.0
        assert rows["openai/gpt-5.5"]["unpriced_input"] == 400

        nodes = store.workflow_nodes("root")
        assert nodes[0]["cost"] == 0.40  # root node
        assert nodes[1]["cost"] == 0.0  # subscription child node


def test_hermes_tolerates_minimal_schema():
    """A Hermes version missing optional columns must not crash (schema-adaptive)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        conn = sqlite3.connect(db)
        # No cwd / parent / started_at / cache / billing / cost / archived columns.
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT,"
            " input_tokens INTEGER, output_tokens INTEGER)"
        )
        conn.execute("INSERT INTO sessions VALUES ('a', 'gpt-5', 100, 50)")
        conn.commit()
        conn.close()

        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert store.records_cost is False  # no cost columns -> not cost-recording

        workflows = store.workflows()
        assert len(workflows) == 1
        w = workflows[0]
        assert w.total_tokens == 150
        assert w.total_cost == 0.0
        assert w.directory == "(unknown)"  # no cwd column

        r = store.model_breakdown()[0]
        assert r["model_name"] == "openai/gpt-5"  # inferred from bare model name
        assert r["tokens_total"] == 150


def test_hermes_joins_the_source_cycle_and_builds_a_resume_command():
    with tempfile.TemporaryDirectory() as tmp:
        oc_db = os.path.join(tmp, "opencode.db")
        _empty_opencode_db(oc_db)
        hermes_db = os.path.join(tmp, "hermes_state.db")
        cwd = os.path.join(tmp, "project")
        os.makedirs(cwd)
        _hermes_db(hermes_db, [{"id": "h1", "inp": 100, "cwd": cwd}])

        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": oc_db,
                "claude_dir": os.path.join(tmp, "no-claude"),
                "codex_dir": os.path.join(tmp, "no-codex"),
                "hermes_db": hermes_db,
                "demo": False,
            },
        )()

        assert ot.available_sources(args) == ["opencode", "hermes"]
        assert ot.sources.source_cycle(args) == ["opencode", "hermes", "all"]

        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "opencode"
        assert app.next_source_name() == "Hermes"
        app.source_key = "hermes"
        assert app.next_source_name() == "all"

        wf = workflow("h1-sess", "2026-06-01 12:00:00", title="t", directory="/tmp/proj")
        wf.source = "Hermes"
        assert app.resume_command(wf) == "cd /tmp/proj && hermes --resume h1-sess"


def _hermes_log(root, lines):
    """Write ~/.hermes/logs/agent.log next to a state.db, in the real line format."""
    d = os.path.join(root, "logs")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "agent.log"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _call_line(ts, sid, n, model="gpt-5.6-sol", provider="openai-codex", inp=0, out=0, cache=None):
    total = inp + out
    line = (
        f"{ts},605 INFO [{sid}] agent.conversation_loop: API call #{n}: "
        f"model={model} provider={provider} in={inp} out={out} total={total} latency=3.2s"
    )
    if cache is not None:
        pct = round(cache / inp * 100) if inp else 0
        line += f" cache={cache}/{inp} ({pct}%)"
    return line


def test_hermes_turns_come_from_the_agent_log():
    # Hermes' messages table HAS token_count and never populates it (0 of 2,474 rows
    # on a real DB), so the per-call usage lives only in the agent log. The bracketed
    # id joins straight to sessions.id.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 100, "out": 10, "cr": 900}])
        _hermes_log(
            root,
            [
                "2026-08-02 07:44:50,100 INFO [s1] agent.other: not an API call line",
                _call_line("2026-08-02 07:44:55", "s1", 1, inp=1000, out=10, cache=900),
                _call_line("2026-08-02 07:45:10", "s1", 2, inp=2000, out=20, cache=1500),
                _call_line("2026-08-02 07:45:30", "other-session", 1, inp=50, out=5),
            ],
        )
        st = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert st.supports_turns("s1") is True
        rows = st.message_timeline("s1")
        assert len(rows) == 2

        # `in` INCLUDES the cache read (it is the denominator of cache=x/y, and
        # in + out == total), so the UNCACHED input is in - cache_read. Getting this
        # backwards double-counts the cached tokens under "$".
        assert rows[0]["input"] == 100 and rows[0]["cache_read"] == 900
        assert rows[0]["output"] == 10 and rows[0]["tokens_total"] == 1010
        assert rows[1]["input"] == 500 and rows[1]["cache_read"] == 1500
        # The identity the whole reading rests on.
        assert all(r["input"] + r["cache_read"] + r["output"] == r["tokens_total"] for r in rows)

        # provider+model are labelled the way model_breakdown labels them, so the
        # Turns tab and the Models tab name the same model.
        assert rows[0]["model_name"] == "openai/gpt-5.6-sol"
        # Every real Hermes session is subscription_included at $0, so a turn records
        # no cost and "$" estimates it from the tokens, like any subscription backend.
        assert all(r["cost"] == 0.0 for r in rows)
        assert all(r["cache_write"] == 0 and r["reasoning"] == 0 for r in rows)
        assert rows[0]["time"] < rows[1]["time"]  # chronological


def test_hermes_turns_are_gated_per_session_by_the_rotating_log():
    # Unlike every other backend's blanket True, this gate is per session: the log
    # rotates, so a session older than the retained window has no turns and must show
    # no tab rather than an empty one that looks like a parsing bug.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "fresh", "inp": 10}, {"id": "aged_out", "inp": 10}])
        _hermes_log(root, [_call_line("2026-08-02 07:44:55", "fresh", 1, inp=10, out=1)])
        st = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert st.supports_turns("fresh") is True
        assert st.supports_turns("aged_out") is False
        assert st.message_timeline("aged_out") == []
        assert st.supports_turns("never-existed") is False


def test_hermes_turns_survive_a_missing_or_unreadable_log():
    # No logs dir at all (a fresh install, or a DB copied elsewhere) must mean "no
    # Turns tab", never an exception on drill-in.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10}])
        st = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert st.supports_turns("s1") is False
        assert st.message_timeline("s1") == []


def test_hermes_turn_counter_resets_on_resume_are_all_kept():
    # A resumed session restarts "API call #1", so the ordinal is NOT unique within a
    # session -- measured on a real one: 231 log lines over 6 resume segments, with
    # call #1..#7 appearing twice at different times. Deduping by the ordinal would
    # silently drop a whole resumed segment's spend.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10}])
        _hermes_log(
            root,
            [
                _call_line("2026-08-02 07:00:00", "s1", 1, inp=100, out=1),
                _call_line("2026-08-02 07:00:10", "s1", 2, inp=100, out=1),
                _call_line("2026-08-02 08:00:00", "s1", 1, inp=100, out=1),  # resumed
            ],
        )
        rows = ot.HermesStore(db, type("Args", (), {"demo": False})()).message_timeline("s1")
        assert len(rows) == 3
        assert sum(r["tokens_total"] for r in rows) == 303


def test_hermes_reload_re_reads_the_growing_log():
    # The gateway appends to agent.log while opentab is open, so `r` (which calls
    # workflows()) has to drop the turn memo too. Without that the Turns tab keeps
    # serving the calls that existed at launch while every rollup around it moves --
    # frozen in a way that looks like the log stopped being written.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10}])
        _hermes_log(root, [_call_line("2026-08-02 07:00:00", "s1", 1, inp=100, out=1)])
        st = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert len(st.message_timeline("s1")) == 1

        _hermes_log(
            root,
            [
                _call_line("2026-08-02 07:00:00", "s1", 1, inp=100, out=1),
                _call_line("2026-08-02 07:05:00", "s1", 2, inp=200, out=2),
            ],
        )
        # Picked up WITHOUT waiting for a reload, because the memo is keyed on the
        # logs' own (size, mtime) rather than merely held. Clearing it from workflows()
        # is not enough on its own: CachedStore serves a warm rollup without ever
        # calling through to it, and "the log grew but the DB did not" is a real state
        # here -- it is the same resume gap that makes log turns exceed the counters.
        assert len(st.message_timeline("s1")) == 2
        st.workflows()  # the `r` path stays correct too
        assert len(st.message_timeline("s1")) == 2


def test_hermes_turn_times_are_local_not_utc():
    # `%(asctime)s` under a plain logging.Formatter is LOCAL time (the stdlib uses
    # time.localtime and Hermes installs no gmtime converter), and the gateway writes
    # that log on the machine opentab reads it from -- so the stamp is already the
    # reader's clock and must not be converted. Shipping it as a UTC->local conversion
    # is invisible on a UTC host and wrong by the offset everywhere else: the turn
    # jumps ahead of prompts it actually followed and the ▸ grouping shifts by one.
    #
    # The prompt epoch is built from a LOCAL wall time, so this test asserts the same
    # thing in every timezone the suite might run in.
    from datetime import datetime as _dt

    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10}])
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
        )
        prompt_epoch = _dt(2026, 8, 2, 7, 0, 0).timestamp()  # 07:00:00 LOCAL
        conn.execute("INSERT INTO messages VALUES ('s1','user','ask the thing',?)", [prompt_epoch])
        conn.commit()
        conn.close()
        # ...and the call is logged 30 seconds later on that same local clock.
        _hermes_log(root, [_call_line("2026-08-02 07:00:30", "s1", 1, inp=100, out=1)])
        (row,) = ot.HermesStore(db, type("Args", (), {"demo": False})()).message_timeline("s1")
        assert row["time"] == "2026-08-02 07:00:30"  # verbatim, never shifted
        assert row["prompt_title"] == "ask the thing"
        assert row["prompt_id"] == "s1:0"


def test_hermes_turns_fold_the_subagent_subtree_under_its_root():
    # A Hermes root's ROLLUP includes its children (parent_session_id is the subagent
    # tree and workflows() folds them in), so its Turns must too -- otherwise the tab
    # totals less than the header above it. And a root whose own calls have rotated out
    # while a child's survive still HAS turns, so the tab must not hide.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(
            db,
            [
                {"id": "root", "inp": 10, "title": "the root"},
                {"id": "kid", "parent_id": "root", "inp": 10, "title": "explore"},
            ],
        )
        _hermes_log(
            root,
            [
                _call_line("2026-08-02 07:00:00", "root", 1, inp=100, out=1),
                _call_line("2026-08-02 07:00:10", "kid", 1, inp=200, out=2),
                _call_line("2026-08-02 07:00:20", "root", 2, inp=300, out=3),
            ],
        )
        st = ot.HermesStore(db, type("Args", (), {"demo": False})())
        rows = st.message_timeline("root")
        # Interleaved by time, the child tagged depth/agent like any subagent turn.
        assert [r["depth"] for r in rows] == [0, 1, 0]
        assert [r["agent"] for r in rows] == ["-", "explore", "-"]
        assert sum(r["tokens_total"] for r in rows) == 101 + 202 + 303

        # The child alone keeps the tab alive when the root's own calls have rotated out.
        _hermes_log(root, [_call_line("2026-08-02 07:00:10", "kid", 1, inp=200, out=2)])
        st2 = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert st2.supports_turns("root") is True
        assert [r["depth"] for r in st2.message_timeline("root")] == [1]
