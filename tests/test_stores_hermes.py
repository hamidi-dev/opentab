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
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "started_at": 1750000000.0, "inp": 10, "out": 5}])
        args = type("Args", (), {"demo": False})()
        (w,) = ot.HermesStore(db, args).workflows()
        assert w.ended_at == ""
        assert w.worked_seconds is None


def test_hermes_store_rolls_grandchild_session_into_subtotal():
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


def _call_line(
    ts, sid, n, model="gpt-5.6-sol", provider="openai-codex", inp=0, out=0, cache=None, ms=605
):
    total = inp + out
    line = (
        f"{ts},{ms:03d} INFO [{sid}] agent.conversation_loop: API call #{n}: "
        f"model={model} provider={provider} in={inp} out={out} total={total} latency=3.2s"
    )
    if cache is not None:
        pct = round(cache / inp * 100) if inp else 0
        line += f" cache={cache}/{inp} ({pct}%)"
    return line


def test_hermes_turns_come_from_the_agent_log():
    # Hermes leaves messages.token_count empty; per-call usage comes from the agent log.
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
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10}])
        st = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert st.supports_turns("s1") is False
        assert st.message_timeline("s1") == []


def test_hermes_turn_counter_resets_on_resume_are_all_kept():
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
    # Hermes logging.Formatter timestamps are local wall time, not UTC.
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


def _hermes_messages(db, rows):
    """(session_id, role, content, epoch) rows in the `messages` table the prompts
    for the Turns tab's ▸ grouping are read from."""
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages "
        "(session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_hermes_subtree_turns_exclude_archived_descendants():
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(
            db,
            [
                {"id": "root", "inp": 10, "title": "the root"},
                {"id": "gone", "parent_id": "root", "inp": 999, "title": "archived kid"},
                {"id": "grand", "parent_id": "gone", "inp": 999, "title": "its child"},
                {"id": "live", "parent_id": "root", "inp": 10, "title": "explore"},
            ],
        )
        conn = sqlite3.connect(db)
        conn.execute("UPDATE sessions SET archived = 1 WHERE id = 'gone'")
        conn.commit()
        conn.close()
        _hermes_log(
            root,
            [
                _call_line("2026-08-02 07:00:00", "root", 1, inp=100, out=1),
                _call_line("2026-08-02 07:00:10", "gone", 1, inp=1000, out=10),
                _call_line("2026-08-02 07:00:15", "grand", 1, inp=1000, out=10),
                _call_line("2026-08-02 07:00:20", "live", 1, inp=200, out=2),
            ],
        )
        st = ot.HermesStore(db, type("Args", (), {"demo": False})())
        rows = st.message_timeline("root")
        assert [r["agent"] for r in rows] == ["-", "explore"]
        assert sum(r["tokens_total"] for r in rows) == 101 + 202  # never the archived 1010s
        # Neither the archived child nor anything under it is in the root's subtree,
        # and the tab's gate is asked of that same set.
        assert [sid for sid, _, _ in st._subtree_ids("root")] == ["root", "live"]


def test_hermes_turn_rows_bound_a_hostile_token_count():
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10}])
        huge = "9" * 400
        _hermes_log(
            root,
            [
                f"2026-08-02 07:00:00,605 INFO [s1] agent.conversation_loop: API call #1: "
                f"model=gpt-5.6-sol provider=openai-codex in={huge} out={huge} total={huge}",
                _call_line("2026-08-02 07:00:10", "s1", 2, inp=100, out=10),
            ],
        )
        rows = ot.HermesStore(db, type("Args", (), {"demo": False})()).message_timeline("s1")
        assert [r["tokens_total"] for r in rows] == [0, 110]
        # The point of the bound: every field survives being priced.
        for r in rows:
            ot.api_equivalent_cost(
                r["model_name"],
                r["input"],
                r["output"],
                r["reasoning"],
                r["cache_read"],
                r["cache_write"],
            )


def test_hermes_child_turns_group_under_the_childs_own_prompts():
    from datetime import datetime as _dt

    def local(h, m, s):
        return _dt(2026, 8, 2, h, m, s).timestamp()

    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(
            db,
            [
                {"id": "root", "inp": 10, "title": "the root"},
                {"id": "kid", "parent_id": "root", "inp": 10, "title": "explore"},
            ],
        )
        _hermes_messages(
            db,
            [
                ("root", "user", "ship the release", local(7, 0, 0)),
                ("kid", "user", "find the callers", local(7, 0, 5)),
                ("root", "user", "now write it up", local(7, 0, 25)),
            ],
        )
        _hermes_log(
            root,
            [
                _call_line("2026-08-02 07:00:01", "root", 1, inp=100, out=1),
                _call_line("2026-08-02 07:00:10", "kid", 1, inp=200, out=2),
                _call_line("2026-08-02 07:00:30", "root", 2, inp=300, out=3),
                # ...and a late child call, AFTER a root prompt it never saw.
                _call_line("2026-08-02 07:00:40", "kid", 2, inp=400, out=4),
            ],
        )
        st = ot.HermesStore(db, type("Args", (), {"demo": False})())
        rows = st.message_timeline("root")
        assert [r["prompt_title"] for r in rows] == [
            "ship the release",
            "find the callers",
            "now write it up",
            "find the callers",  # the child's own prompt still owns its later call
        ]
        # Per-session prompt ids, so the ▸ groups never collide across the subtree.
        assert [r["prompt_id"] for r in rows] == ["root:0", "kid:0", "root:1", "kid:0"]


def test_hermes_demo_hides_the_child_session_title_in_the_agent_column():
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(
            db,
            [
                {"id": "root", "inp": 10, "title": "the root"},
                {"id": "kid", "parent_id": "root", "inp": 10, "title": "SECRET customer audit"},
            ],
        )
        _hermes_log(
            root,
            [
                _call_line("2026-08-02 07:00:00", "root", 1, inp=100, out=1),
                _call_line("2026-08-02 07:00:10", "kid", 1, inp=200, out=2),
            ],
        )
        st = ot.HermesStore(db, type("Args", (), {"demo": True})())
        rows = st.message_timeline("root")
        agents = [r["agent"] for r in rows]
        assert "SECRET customer audit" not in agents
        assert agents[0] == "-" and agents[1] not in ("", "-")
        # Seeded off the child's id, so the Turns tab and the Subagents tab agree on
        # the fake name for that node.
        node = next(n for n in st.workflow_nodes("root") if n["depth"])
        assert agents[1] == node["title"]


def _hermes_db_0206(path, rows):
    """A Hermes 0.20.6-shaped sessions table, as a compatibility record.

    Carries the columns that version added and HermesStore must tolerate:
    `hidden` (sidebar presentation state, NOT an accounting signal) and
    `title_source` (provenance of `title`, NOT a validity flag)."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            title_source TEXT,
            model TEXT,
            cwd TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0,
            profile_name TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r["id"],
                r.get("title", r["id"]),
                r.get("title_source"),
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
                r.get("hidden", 0),
                r.get("profile_name"),
            )
            for r in rows
        ],
    )
    conn.commit()
    conn.close()


def test_hermes_hidden_sessions_still_count():
    # `hidden` hides a session in Hermes' own sidebar; it says nothing about spend.
    # Only `archived` may drop a session from the rollup -- treating the two as
    # synonyms would silently delete real spend from the dashboard.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db_0206(
            db,
            [
                {"id": "seen", "inp": 100, "out": 10, "hidden": 0},
                {"id": "tucked", "inp": 200, "out": 20, "hidden": 1},
                {"id": "gone", "inp": 400, "out": 40, "archived": 1},
            ],
        )
        got = {w.id: w for w in ot.HermesStore(db, type("Args", (), {"demo": False})()).workflows()}
        assert set(got) == {"seen", "tucked"}
        assert got["tucked"].total_tokens == 220  # 200 in + 20 out, nothing dropped


def test_hermes_own_title_wins_whatever_its_provenance():
    # Precedence is on the VALUE, never on title_source: that column is provenance,
    # not validity, and is NULL on every session written before 0.20.6. Switching on
    # it would throw away legitimate historical titles.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db_0206(
            db,
            [
                {"id": "named", "title": "Nur OK sagen", "title_source": "llm", "inp": 10},
                {"id": "legacy", "title": "an older title", "title_source": None, "inp": 10},
                {"id": "bare", "title": "", "title_source": None, "inp": 10},
            ],
        )
        _hermes_messages(
            db,
            [
                ("named", "user", "must not be used", 1.0),
                ("legacy", "user", "must not be used either", 1.0),
                ("bare", "user", "the first real prompt", 1.0),
            ],
        )
        titles = {
            w.id: w.title
            for w in ot.HermesStore(db, type("Args", (), {"demo": False})()).workflows()
        }
        assert titles["named"] == "Nur OK sagen"
        assert titles["legacy"] == "an older title"
        assert titles["bare"] == "the first real prompt"


def test_hermes_whitespace_only_title_falls_back_to_the_prompt():
    # `row["title"] or ""` treats "   " as a real title, which would show a blank row.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "title": "   ", "inp": 10, "out": 5}])
        _hermes_messages(db, [("s1", "user", "the real question", 1.0)])
        (w,) = ot.HermesStore(db, type("Args", (), {"demo": False})()).workflows()
        assert w.title == "the real question"


def test_hermes_subagent_label_matches_the_rollup_title():
    # _parse() gives an untitled session its first user prompt, but _subtree_ids()
    # read the raw column -- so one child showed its prompt in Subagents and "-" in
    # the Turns agent column. Both paths must apply the same precedence.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(
            db,
            [
                {"id": "root", "inp": 10, "title": "the root"},
                {"id": "kid", "parent_id": "root", "inp": 10, "title": ""},
            ],
        )
        _hermes_messages(db, [("kid", "user", "dig through the logs", 1.0)])
        _hermes_log(root, [_call_line("2026-08-02 07:00:10", "kid", 1, inp=200, out=2)])
        rows = ot.HermesStore(db, type("Args", (), {"demo": False})()).message_timeline("root")
        assert [r["agent"] for r in rows] == ["dig through the logs"]


def test_hermes_offers_the_context_curve_wherever_it_offers_turns():
    # The Context column rides on Turns: a store with per-request prompt sizes gets the
    # curve from the app default unless it opts out. Hermes' turns carry input +
    # cache_read, which reconstructs the real prompt size, so it qualifies -- and the
    # capability must track Turns exactly, including the rotating-log gate.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
        _hermes_log(root, [_call_line("2026-08-02 07:00:00", "s1", 1, inp=100, out=1)])
        st = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert st.supports_turns("s1") is True
        # No opt-out attribute: the app's default grants the curve (tui/app.py).
        assert getattr(st, "supports_context_curve", None) is None

        # ...and a session whose calls have rotated out of the log offers neither.
        _hermes_log(root, [_call_line("2026-08-02 07:00:00", "other", 1, inp=100, out=1)])
        st2 = ot.HermesStore(db, type("Args", (), {"demo": False})())
        assert st2.supports_turns("s1") is False


def _hermes_usage(db, rows):
    """Hermes 0.20.6 `session_model_usage`: per (session, model, provider, task) usage.

    task='' is the main agent loop (already summed into the sessions row); any other
    task is an auxiliary call Hermes deliberately keeps OUT of that row."""
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE session_model_usage (
            session_id TEXT NOT NULL,
            model TEXT NOT NULL,
            billing_provider TEXT NOT NULL DEFAULT '',
            billing_base_url TEXT NOT NULL DEFAULT '',
            billing_mode TEXT NOT NULL DEFAULT '',
            task TEXT NOT NULL DEFAULT '',
            api_call_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            actual_cost_usd REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (session_id, model, billing_provider, billing_base_url,
                         billing_mode, task)
        )"""
    )
    conn.executemany(
        "INSERT INTO session_model_usage "
        "(session_id, model, billing_provider, task, api_call_count, input_tokens,"
        " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
        " estimated_cost_usd, actual_cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r["id"],
                r.get("model", "gpt-5"),
                r.get("provider", ""),
                r.get("task", ""),
                r.get("calls", 1),
                r.get("inp", 0),
                r.get("out", 0),
                r.get("cr", 0),
                r.get("cw", 0),
                r.get("reasoning", 0),
                r.get("estimated_cost_usd", 0.0),
                r.get("actual_cost_usd", 0.0),
            )
            for r in rows
        ],
    )
    conn.commit()
    conn.close()


def _hermes_one(db):
    return ot.HermesStore(db, type("Args", (), {"demo": False})()).workflows()[0]


def test_hermes_auxiliary_spend_is_added_when_the_main_rows_reconcile():
    # The sessions row carries main-loop tokens only; aux tasks live beside it.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "model": "gpt-5", "inp": 100, "out": 50}])
        _hermes_usage(
            db,
            [
                {"id": "s1", "task": "", "inp": 100, "out": 50},
                {"id": "s1", "task": "title_generation", "inp": 10, "out": 5},
            ],
        )
        assert _hermes_one(db).total_tokens == 165  # 150 main + 15 aux


def test_hermes_skewed_usage_rows_never_double_count():
    # A future Hermes (or a mid-commit read) whose sessions row ALREADY folds aux in.
    # Adding the aux rows again would inflate the dashboard, so reconciliation fails
    # and the summary row stands alone.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "model": "gpt-5", "inp": 110, "out": 55}])
        _hermes_usage(
            db,
            [
                {"id": "s1", "task": "", "inp": 100, "out": 50},
                {"id": "s1", "task": "title_generation", "inp": 10, "out": 5},
            ],
        )
        assert _hermes_one(db).total_tokens == 165  # the sessions row, not 180


def test_hermes_partially_written_usage_rows_fall_back_to_the_session_row():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "model": "gpt-5", "inp": 100, "out": 50}])
        _hermes_usage(
            db,
            [
                {"id": "s1", "task": "", "inp": 90, "out": 50},  # short by 10
                {"id": "s1", "task": "approval", "inp": 10, "out": 5},
            ],
        )
        assert _hermes_one(db).total_tokens == 150  # aux withheld, main untouched


def test_hermes_session_with_only_auxiliary_usage_still_appears():
    # Sessions with no usage are dropped from the rollup; an aux-only session has to
    # gain its tokens BEFORE that test or it vanishes from the dashboard entirely.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "model": "gpt-5", "inp": 0, "out": 0}])
        _hermes_usage(
            db,
            [
                {"id": "s1", "task": "", "inp": 0, "out": 0},
                {"id": "s1", "task": "vision", "inp": 10, "out": 5},
            ],
        )
        assert _hermes_one(db).total_tokens == 15


def test_hermes_model_switch_is_attributed_per_model():
    # The sessions row pins one model; the usage rows know which model earned what.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "model": "gpt-5", "inp": 300, "out": 150}])
        _hermes_usage(
            db,
            [
                {"id": "s1", "model": "gpt-5", "task": "", "inp": 100, "out": 50},
                {"id": "s1", "model": "gpt-4o", "task": "", "inp": 200, "out": 100},
            ],
        )
        store = ot.HermesStore(db, type("Args", (), {"demo": False})())
        rows = {r["model_name"]: r for r in store.model_breakdown()}
        assert set(rows) == {"openai/gpt-5", "openai/gpt-4o"}
        assert rows["openai/gpt-5"]["tokens_total"] == 150
        assert rows["openai/gpt-4o"]["tokens_total"] == 300
        # The rollup total is unchanged: attribution moved, nothing was invented.
        assert store.workflows()[0].total_tokens == 450
        assert sum(r["tokens_total"] for r in store.model_breakdown()) == 450


def test_hermes_one_model_across_tasks_stays_one_model_row():
    # Buckets merge by model, so model_count counts models -- not model-task pairs.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "model": "gpt-5", "inp": 100, "out": 50}])
        _hermes_usage(
            db,
            [
                {"id": "s1", "task": "", "inp": 100, "out": 50},
                {"id": "s1", "task": "title_generation", "inp": 10, "out": 5},
                {"id": "s1", "task": "approval", "inp": 20, "out": 10},
            ],
        )
        rows = ot.HermesStore(db, type("Args", (), {"demo": False})()).model_breakdown()
        assert len(rows) == 1
        assert rows[0]["model_name"] == "openai/gpt-5"
        assert rows[0]["tokens_total"] == 195  # 150 main + 15 title + 30 approval


def test_hermes_auxiliary_auto_provider_merges_into_the_real_model():
    # Hermes writes billing_provider='auto' on auxiliary rows -- its own unresolved
    # config placeholder, not a vendor. Taken literally it splits one model into
    # "openai/gpt-5.6-sol" and "auto/gpt-5.6-sol", inflating the model count and
    # pricing half the tokens against a vendor that does not exist.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "model": "gpt-5.6-sol", "inp": 100, "out": 50}])
        _hermes_usage(
            db,
            [
                {
                    "id": "s1",
                    "model": "gpt-5.6-sol",
                    "provider": "openai-codex",
                    "task": "",
                    "inp": 100,
                    "out": 50,
                },
                {
                    "id": "s1",
                    "model": "gpt-5.6-sol",
                    "provider": "auto",
                    "task": "title_generation",
                    "inp": 10,
                    "out": 5,
                },
            ],
        )
        rows = ot.HermesStore(db, type("Args", (), {"demo": False})()).model_breakdown()
        assert [r["model_name"] for r in rows] == ["openai/gpt-5.6-sol"]
        assert rows[0]["tokens_total"] == 165


def test_hermes_usage_table_can_be_the_only_metered_cost_truth():
    # Hermes 0.20.6's sessions table has no cost columns; the per-model table can still
    # carry real metered spend. Mixed main buckets must keep their own priced split.
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db_0206(db, [{"id": "s1", "model": "gpt-5", "inp": 300, "out": 150}])
        _hermes_usage(
            db,
            [
                {
                    "id": "s1",
                    "model": "gpt-5",
                    "task": "",
                    "inp": 100,
                    "out": 50,
                    "actual_cost_usd": 0.30,
                },
                {"id": "s1", "model": "gpt-4o", "task": "", "inp": 200, "out": 100},
                {"id": "s1", "model": "gpt-4o", "task": "approval", "inp": 10, "out": 5},
            ],
        )
        store = _store(db)
        assert store.records_cost is True
        (workflow,) = store.workflows()
        assert workflow.total_tokens == 465
        assert workflow.total_cost == 0.30
        assert workflow.unpriced_tokens == 315
        rows = {r["model_name"]: r for r in store.model_breakdown()}
        assert rows["openai/gpt-5"]["cost"] == 0.30
        assert rows["openai/gpt-5"]["unpriced_input"] == 0
        assert rows["openai/gpt-4o"]["unpriced_input"] == 210


def test_hermes_usage_schema_defaults_each_missing_optional_column():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db_0206(db, [{"id": "s1", "model": "gpt-5", "inp": 100}])
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT, "
            "input_tokens INTEGER, actual_cost_usd REAL)"
        )
        conn.executemany(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?)",
            [("s1", "gpt-5", "", 100, 0.20), ("s1", "gpt-5", "vision", 10, 0.10)],
        )
        conn.commit()
        conn.close()
        store = _store(db)
        assert store.records_cost is True
        (workflow,) = store.workflows()
        assert workflow.total_tokens == 110
        assert workflow.total_cost == 0.30
        (row,) = store.model_breakdown()
        assert row["runs"] == 2  # api_call_count is absent, so each bucket defaults to one


def test_hermes_usage_message_count_uses_api_call_count():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db(db, [{"id": "s1", "model": "gpt-5", "inp": 100, "out": 50}])
        _hermes_usage(
            db,
            [
                {"id": "s1", "task": "", "calls": 7, "inp": 100, "out": 50},
                {"id": "s1", "task": "title_generation", "calls": 2, "inp": 10, "out": 5},
            ],
        )
        (row,) = _store(db).model_breakdown()
        assert row["runs"] == 9


def test_hermes_archived_usage_does_not_advertise_recorded_cost():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db_0206(
            db,
            [{"id": "live", "inp": 10}, {"id": "gone", "inp": 20, "archived": 1}],
        )
        _hermes_usage(
            db,
            [
                {"id": "live", "task": "", "inp": 10},
                {"id": "gone", "task": "", "inp": 20, "actual_cost_usd": 0.50},
            ],
        )
        assert _store(db).records_cost is False


def _at(local_str):
    """Epoch for a local wall-clock stamp, so fixtures line up with the log's strings."""
    from datetime import datetime as _dt

    return _dt.strptime(local_str, "%Y-%m-%d %H:%M:%S.%f").timestamp()


def _hermes_messages_tc(db, rows):
    """`messages` including tool_calls: (session_id, role, tool_calls_json, epoch)."""
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages "
        "(session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, timestamp REAL)"
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
        "VALUES (?,?,?,?,?)",
        [(sid, role, "", tc, ts) for sid, role, tc, ts in rows],
    )
    conn.commit()
    conn.close()


def _tc(*names):
    import json as _json

    return _json.dumps([{"function": {"name": n, "arguments": "{}"}} for n in names])


def _store(db):
    return ot.HermesStore(db, type("Args", (), {"demo": False})())


def test_hermes_tool_breakdown_attributes_the_calling_turns_tokens():
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
        _hermes_log(
            root,
            [
                _call_line("2026-08-24 14:00:00", "s1", 1, inp=100, out=10, ms=100),
                _call_line("2026-08-24 14:00:05", "s1", 2, inp=200, out=20, ms=100),
            ],
        )
        _hermes_messages_tc(
            db,
            [
                ("s1", "assistant", _tc("terminal"), _at("2026-08-24 14:00:00.101")),
                ("s1", "tool", None, _at("2026-08-24 14:00:01.000")),
                ("s1", "assistant", None, _at("2026-08-24 14:00:05.101")),
            ],
        )
        st = _store(db)
        assert st.supports_tools("s1") is True
        rows = st.tool_breakdown("s1")
        assert [r["tool"] for r in rows] == ["terminal"]
        # Only the tool-calling turn's tokens, never the answering turn's.
        assert rows[0]["tokens_total"] == 110
        assert rows[0]["calls"] == 1


def test_hermes_rejected_call_does_not_donate_its_tokens_to_the_retry():
    # Hermes logs usage BEFORE it validates the response, so a call can be logged and
    # then rejected with no assistant row (invalid/truncated tool calls have their own
    # retry path). Measured: 12 of 118 real sessions log more calls than they persist
    # assistant messages. "Next assistant wins" would pay the retry's tools with the
    # rejected call's tokens; a newer call must supersede the older unmatched one.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
        _hermes_log(
            root,
            [
                _call_line("2026-08-24 14:00:00", "s1", 1, inp=900, out=90, ms=100),
                _call_line("2026-08-24 14:00:02", "s1", 2, inp=100, out=10, ms=100),
            ],
        )
        _hermes_messages_tc(
            db, [("s1", "assistant", _tc("terminal"), _at("2026-08-24 14:00:02.101"))]
        )
        rows = _store(db).tool_breakdown("s1")
        assert [r["tool"] for r in rows] == ["terminal"]
        assert rows[0]["tokens_total"] == 110  # call #2's tokens, not the rejected 990


def test_hermes_intervening_event_cancels_a_pending_tool_match():
    # A user message between a call and the next assistant means that assistant does
    # not answer that call; binding across it would be a guess.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
        _hermes_log(root, [_call_line("2026-08-24 14:00:00", "s1", 1, inp=100, out=10, ms=100)])
        _hermes_messages_tc(
            db,
            [
                ("s1", "user", None, _at("2026-08-24 14:00:00.500")),
                ("s1", "assistant", _tc("terminal"), _at("2026-08-24 14:00:00.900")),
            ],
        )
        st = _store(db)
        assert st.tool_breakdown("s1") == []
        assert st.supports_tools("s1") is False


def test_hermes_malformed_tool_calls_are_rejected_whole():
    # All-or-nothing: keeping the valid half of a blob would hand the survivors a
    # bigger share of the turn than they earned.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
        _hermes_log(root, [_call_line("2026-08-24 14:00:00", "s1", 1, inp=100, out=10, ms=100)])
        _hermes_messages_tc(
            db,
            [
                (
                    "s1",
                    "assistant",
                    '[{"function":{"name":"terminal"}},{"function":{}}]',
                    _at("2026-08-24 14:00:00.101"),
                )
            ],
        )
        assert _store(db).tool_breakdown("s1") == []

    for blob in ("not json", "{}", '["bare"]', '[{"function":{"name":""}}]', None):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, "state.db")
            _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
            _hermes_log(root, [_call_line("2026-08-24 14:00:00", "s1", 1, inp=100, out=10, ms=100)])
            _hermes_messages_tc(db, [("s1", "assistant", blob, _at("2026-08-24 14:00:00.101"))])
            assert _store(db).tool_breakdown("s1") == [], blob


def test_hermes_parallel_tool_calls_split_the_turn_evenly():
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
        _hermes_log(root, [_call_line("2026-08-24 14:00:00", "s1", 1, inp=90, out=10, ms=100)])
        _hermes_messages_tc(
            db,
            [
                (
                    "s1",
                    "assistant",
                    _tc("terminal", "read_file", "terminal"),
                    _at("2026-08-24 14:00:00.101"),
                )
            ],
        )
        rows = {r["tool"]: r for r in _store(db).tool_breakdown("s1")}
        # Three calls, two names: the repeat takes two of the three shares.
        assert rows["terminal"]["calls"] == 2
        assert rows["read_file"]["calls"] == 1
        assert abs(rows["terminal"]["tokens_total"] - 200 / 3) < 1e-6
        assert abs(rows["read_file"]["tokens_total"] - 100 / 3) < 1e-6
        assert abs(sum(r["tokens_total"] for r in rows.values()) - 100) < 1e-6


def test_hermes_tools_are_gated_per_session_like_turns():
    # DB tool names with no retained log = names but no tokens: no tab, not an empty one.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
        _hermes_log(root, [_call_line("2026-08-24 14:00:00", "other", 1, inp=100, out=10)])
        _hermes_messages_tc(
            db, [("s1", "assistant", _tc("terminal"), _at("2026-08-24 14:00:00.101"))]
        )
        st = _store(db)
        assert st.supports_turns("s1") is False
        assert st.supports_tools("s1") is False
        assert st.tool_breakdown("s1") == []


def test_hermes_root_tools_include_a_subagents_tools():
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(
            db,
            [
                {"id": "root", "inp": 10, "title": "root"},
                {"id": "kid", "parent_id": "root", "inp": 10, "title": "kid"},
            ],
        )
        _hermes_log(root, [_call_line("2026-08-24 14:00:00", "kid", 1, inp=100, out=10, ms=100)])
        _hermes_messages_tc(
            db, [("kid", "assistant", _tc("search_files"), _at("2026-08-24 14:00:00.101"))]
        )
        st = _store(db)
        assert st.supports_tools("root") is True
        assert [r["tool"] for r in st.tool_breakdown("root")] == ["search_files"]


def test_hermes_turns_and_tools_never_disagree():
    # Tools is a strict projection of the rows the Turns tab draws: same source, so no
    # token can reach the Tools tab that Turns does not also show.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
        _hermes_log(
            root,
            [
                _call_line("2026-08-24 14:00:00", "s1", 1, inp=100, out=10, ms=100),
                _call_line("2026-08-24 14:00:05", "s1", 2, inp=200, out=20, ms=100),
            ],
        )
        _hermes_messages_tc(
            db,
            [
                ("s1", "assistant", _tc("terminal"), _at("2026-08-24 14:00:00.101")),
                ("s1", "assistant", _tc("memory"), _at("2026-08-24 14:00:05.101")),
            ],
        )
        st = _store(db)
        from opentab.util import tool_names

        turn_tools = sorted(
            t for r in st.message_timeline("s1") for t in tool_names(r.get("tools"))
        )
        assert turn_tools == ["memory", "terminal"]
        rows = st.tool_breakdown("s1")
        assert sum(r["tokens_total"] for r in rows) == 110 + 220


def test_hermes_tool_cache_invalidates_when_the_db_message_arrives_later():
    # Hermes writes the log before validating and persisting the assistant message.
    # Caching the first half on the log fingerprint alone makes tools stay absent.
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
        _hermes_log(root, [_call_line("2026-08-24 14:00:00", "s1", 1, inp=100, out=10)])
        st = _store(db)
        assert st.supports_tools("s1") is False

        _hermes_messages_tc(
            db, [("s1", "assistant", _tc("terminal"), _at("2026-08-24 14:00:00.606"))]
        )
        assert st.supports_tools("s1") is True
        assert [r["tool"] for r in st.tool_breakdown("s1")] == ["terminal"]


def test_hermes_parent_cycle_never_duplicates_turns():
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(
            db,
            [
                {"id": "a", "parent_id": "b", "inp": 10, "title": "a"},
                {"id": "b", "parent_id": "a", "inp": 20, "title": "b"},
            ],
        )
        _hermes_log(
            root,
            [
                _call_line("2026-08-24 14:00:00", "a", 1, inp=100, out=10),
                _call_line("2026-08-24 14:00:01", "b", 1, inp=200, out=20),
            ],
        )
        st = _store(db)
        assert st.root_of("a") == st.root_of("b") == "a"
        assert [r["id"] for r in st.recent_roots()] == ["a"]
        assert [sid for sid, _, _ in st._subtree_ids("a")] == ["a", "b"]
        rows = st.message_timeline("a")
        assert len(rows) == 2
        assert sum(r["tokens_total"] for r in rows) == 110 + 220


def test_hermes_prompt_later_in_the_same_second_does_not_claim_the_call():
    with tempfile.TemporaryDirectory() as root:
        db = os.path.join(root, "state.db")
        _hermes_db_full(db, [{"id": "s1", "inp": 10, "title": "t"}])
        _hermes_messages(
            db,
            [
                ("s1", "user", "old prompt", _at("2026-08-24 13:59:59.000")),
                ("s1", "user", "future prompt", _at("2026-08-24 14:00:00.900")),
            ],
        )
        _hermes_log(root, [_call_line("2026-08-24 14:00:00", "s1", 1, inp=100, out=10, ms=100)])
        (row,) = _store(db).message_timeline("s1")
        assert row["prompt_title"] == "old prompt"


def test_hermes_live_child_of_archived_parent_is_its_own_root_everywhere():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "state.db")
        _hermes_db_full(
            db,
            [
                {"id": "gone", "inp": 20, "title": "gone"},
                {"id": "child", "parent_id": "gone", "inp": 10, "title": "child"},
            ],
        )
        conn = sqlite3.connect(db)
        conn.execute("UPDATE sessions SET archived = 1 WHERE id = 'gone'")
        conn.commit()
        conn.close()
        st = _store(db)
        assert [w.id for w in st.workflows()] == ["child"]
        assert st.root_of("child") == "child"
        assert [r["id"] for r in st.recent_roots()] == ["child"]
