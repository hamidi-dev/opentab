"""Shared fixtures for the test suite: fake stores/screens and per-backend builders."""

import json
import os
import sqlite3

import opentab as ot


def workflow(id, created_at, title=None, cost=1.0, tokens=100, directory="/tmp/project"):
    return ot.Workflow(
        id=id,
        title=title or id,
        directory=directory,
        created_at=created_at,
        root_cost=cost,
        total_cost=cost,
        subagents=0,
        model_count=1,
        total_tokens=tokens,
        unpriced_tokens=0,
    )


class FakeStore:
    demo = False
    records_cost = True
    source_name = "OpenCode"

    def __init__(self, workflows):
        self._workflows = workflows

    def workflows(self):
        return list(self._workflows)

    def model_breakdown(self):
        return []

    def summary(self, workflows):
        return {
            "workflows": len(workflows),
            "cost": sum(w.total_cost for w in workflows),
            "tokens": sum(w.total_tokens for w in workflows),
            "subagents": sum(w.subagents for w in workflows),
            "unpriced_tokens": sum(w.unpriced_tokens for w in workflows),
        }


def app_with(workflows, since=None, until=None, days=None):
    args = type("Args", (), {"since": since, "until": until, "days": days})()
    return ot.App(FakeStore(workflows), args)


class FakeScreen:
    # Just enough curses surface for the self-painting draw_* methods (which only
    # addstr onto a sized grid) plus the addch/hline/vline the box frame is drawn
    # with. Records every glyph by (y, x) so a test can read back what was painted;
    # ignores attributes (color is irrelevant to text checks).
    def __init__(self, height=24, width=80):
        self.height, self.width = height, width
        self.cells = {}

    def getmaxyx(self):
        return (self.height, self.width)

    def addstr(self, y, x, text, attr=0):
        for i, ch in enumerate(text):
            self.cells[(y, x + i)] = ch

    def addch(self, y, x, ch, attr=0):
        self.cells[(y, x)] = ch

    def hline(self, y, x, ch, n, attr=0):
        self._line(y, x, ch, n, 0)

    def vline(self, y, x, ch, n, attr=0):
        self._line(y, x, ch, n, 1)

    def _line(self, y, x, ch, n, down):
        # Real hline/vline take a chtype -- a single *byte* -- and raise OverflowError
        # on a multibyte glyph (addch/addstr take the wide-character path instead). Keep
        # that limit here, so a Unicode frame drawn through the wrong call fails in the
        # suite the way it does on a real screen.
        if isinstance(ch, str) and len(ch.encode()) > 1:
            raise OverflowError("byte doesn't fit in chtype")
        for i in range(n):
            self.cells[(y + i * down, x + i * (1 - down))] = ch


class AttrScreen(FakeScreen):
    # FakeScreen that also remembers the attribute each glyph was painted with, so a
    # test can assert on color/bold/dim rather than just the text.
    def __init__(self, height=24, width=80):
        super().__init__(height, width)
        self.attrs = {}

    def addstr(self, y, x, text, attr=0):
        super().addstr(y, x, text, attr)
        for i in range(len(text)):
            self.attrs[(y, x + i)] = attr


def screen_text(screen):
    # Flatten the painted cells back into newline-joined rows (gaps become spaces).
    rows = {}
    for (y, x), ch in screen.cells.items():
        rows.setdefault(y, {})[x] = ch
    lines = []
    for y in sorted(rows):
        cols = rows[y]
        lines.append("".join(cols.get(x, " ") for x in range(min(cols), max(cols) + 1)))
    return "\n".join(lines)


def _model_row(model_name, cost, tokens):
    return {
        "model_name": model_name,
        "runs": 1,
        "cost": cost,
        "tokens_total": tokens,
        "cache_read": 0,
        "cache_write": 0,
        "output": 0,
    }


def _price_sort_app():
    # Spend order (gpt-5-mini > haiku > opus) is deliberately the reverse of the
    # list-price order (opus > haiku > gpt-5-mini) so a column sort visibly reorders.
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app._model_by_root = {
        "a": [
            _model_row("anthropic/claude-opus-4-8", 1.0, 10),  # priciest, least spend
            _model_row("openai/gpt-5-mini", 9.0, 10),  # cheapest, most spend
            _model_row("anthropic/claude-haiku-4-5", 5.0, 10),
        ]
    }
    return app


def _select_session(app, session_id):
    # Park the app on one selected session — the context `b`, `n` and `L` all need.
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    app.workflow_index = next(i for i, w in enumerate(app.current_sessions()) if w.id == session_id)
    return app


def _app_on_session(sessions, session_id):
    return _select_session(app_with(sessions), session_id)


def _write_opencode_db_with_tools(db):
    # Minimal OpenCode-shaped DB exercising the `part` table the Tools tab reads.
    # One subscription ($0) step calls TWO tools in parallel; one priced ($6) step
    # calls one tool. Token totals are chosen so even-split attribution is visible.
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table session (
          id text primary key, parent_id text, title text, directory text,
          time_created integer, cost real default 0 not null,
          tokens_input integer default 0 not null, tokens_output integer default 0 not null,
          tokens_reasoning integer default 0 not null, tokens_cache_read integer default 0 not null,
          tokens_cache_write integer default 0 not null
        );
        create table message (id text primary key, session_id text, data text);
        create table part (id text primary key, message_id text, session_id text, data text);
        """
    )
    conn.execute(
        "insert into session values (?,?,?,?,?,?,?,?,?,?,?)",
        ("s1", None, "Root", "/work/repo", 1760000000000, 6.0, 0, 0, 0, 0, 0),
    )
    conn.executemany(
        "insert into message values (?,?,?)",
        [
            (
                "m1",
                "s1",
                '{"role":"assistant","providerID":"anthropic","modelID":"claude-haiku-4.5",'
                '"cost":0,"tokens":{"input":2000000,"output":0}}',
            ),
            (
                "m2",
                "s1",
                '{"role":"assistant","providerID":"anthropic","modelID":"claude-haiku-4.5",'
                '"cost":6.0,"tokens":{"input":6000000,"output":0}}',
            ),
        ],
    )
    conn.executemany(
        "insert into part values (?,?,?,?)",
        [
            ("p1", "m1", "s1", '{"type":"step-start"}'),  # non-tool parts are ignored
            ("p2", "m1", "s1", '{"type":"tool","tool":"bash"}'),
            ("p3", "m1", "s1", '{"type":"tool","tool":"serena_read_file"}'),
            ("p4", "m2", "s1", '{"type":"tool","tool":"bash"}'),
        ],
    )
    conn.commit()
    conn.close()


def _write_opencode_db_with_turns(db):
    # OpenCode-shaped DB for the Turns tab: a root session s1 with two assistant
    # messages and a subagent child s2 with one. Messages are inserted out of time
    # order (and carry $.time.created) so the timeline must sort them chronologically;
    # one priced ($3) step plus two $0 (subscription) steps exercise the "$" reprice.
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table session (
          id text primary key, parent_id text, title text, directory text, agent text,
          time_created integer
        );
        create table message (id text primary key, session_id text, data text);
        create table part (id text primary key, message_id text, session_id text, data text);
        """
    )
    conn.executemany(
        "insert into session values (?,?,?,?,?,?)",
        [
            ("s1", None, "Root", "/work/repo", None, 1760000000000),
            ("s2", "s1", "Explore", "/work/repo", "explore", 1760000000000),
        ],
    )

    def msg(model, cost, created, inp):
        return (
            f'{{"role":"assistant","providerID":"anthropic","modelID":"{model}",'
            f'"cost":{cost},"time":{{"created":{created}}},"tokens":{{"input":{inp},"output":0}}}}'
        )

    def user(created, title):
        return f'{{"role":"user","time":{{"created":{created}}},"summary":{{"title":"{title}"}}}}'

    conn.executemany(
        "insert into message values (?,?,?)",
        [
            # inserted last-first to prove the query orders by time, not rowid
            ("m2", "s1", msg("claude-sonnet-4-5", 3.0, 2000, 500000)),  # priced, t=2000
            ("m1", "s1", msg("claude-haiku-4.5", 0, 1000, 1000000)),  # $0, t=1000
            ("m3", "s2", msg("claude-haiku-4.5", 0, 1500, 2000000)),  # subagent $0, t=1500
            # two user prompts: u1 owns m1+m3 (t<=1500), u2 owns m2 (t=2000)
            ("u1", "s1", user(500, "Add feature X")),
            ("u2", "s1", user(1800, "Fix the bug")),
        ],
    )
    conn.commit()
    conn.close()


def _whatif_msg(session, provider, model, cost, tokens_in):
    return (
        session,
        '{"role":"assistant","providerID":"%s","modelID":"%s","cost":%s,"tokens":{"input":%d,"output":0}}'
        % (provider, model, cost, tokens_in),
    )


def _whatif_app(tmp, sessions, messages, demo=False):
    # A minimal OpenCode DB + headless App, for the `w` what-if tests. `sessions` rows are
    # (id, parent_id, title, dir, time, cost, tokens_input); the messages carry the
    # per-model usage the breakdown reads.
    db = os.path.join(tmp, "opencode.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table session (
          id text primary key,
          parent_id text,
          title text,
          directory text,
          time_created integer,
          cost real default 0 not null,
          tokens_input integer default 0 not null,
          tokens_output integer default 0 not null,
          tokens_reasoning integer default 0 not null,
          tokens_cache_read integer default 0 not null,
          tokens_cache_write integer default 0 not null
        );
        create table message (session_id text, data text);
        """
    )
    conn.executemany(
        "insert into session values (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0)",
        sessions,
    )
    conn.executemany("insert into message values (?, ?)", messages)
    conn.commit()
    conn.close()
    store = ot.Store(db, type("Args", (), {"demo": demo})())
    app = ot.App(store, type("Args", (), {"since": None, "until": None, "days": None})())
    app._ensure_models()
    return app


def _whatif_db(tmp, demo=False, costs=(1.5, 0.44), solo=False):
    # An OpenCode DB shaped like the question `w` answers: an expensive main agent
    # (1M Opus input, $1.50) that delegated the grunt work to a cheap subagent (2M
    # Haiku input, $0.44). Session-table cost/token columns AND the messages carry the
    # same usage, so workflows()/workflow_nodes() and model_breakdown() agree.
    # costs=(0, 0) makes it a *subscription* session -- same tokens, nothing recorded.
    sessions = [
        ("root", None, "Root", "/tmp/project", 1760000000000, costs[0], 1_000_000),
        ("kid", "root", "Docs", "/tmp/project", 1760000001000, costs[1], 2_000_000),
    ]
    messages = [
        _whatif_msg("root", "anthropic", "claude-opus-4.5", costs[0], 1_000_000),
        _whatif_msg("kid", "anthropic", "claude-haiku-4.5", costs[1], 2_000_000),
    ]
    if solo:  # a session that delegated nothing: root only, no tree to table
        sessions, messages = sessions[:1], messages[:1]
    return _whatif_app(tmp, sessions, messages, demo=demo)


def _whatif_baseline(app, workflow_id):
    # The exact baseline, spelled out independently of App.whatif_session_totals: every
    # model row of the session, its own tokens at its own list rates.
    return sum(
        ot.api_equivalent_cost(
            m["model_name"],
            m["input"],
            m["output"],
            m["reasoning"],
            m["cache_read"],
            m["cache_write"],
        )
        for m in app._model_by_root[workflow_id]
    )


def _claude_msg(
    session,
    model,
    usage,
    *,
    uuid,
    cwd,
    parent=None,
    side=False,
    mid=None,
    req=None,
    ts=None,
    tools=None,
):
    message = {
        "id": mid or (uuid + "-id"),
        "model": model,
        "role": "assistant",
        "usage": usage,
    }
    if tools:  # the step's tool_use blocks, for the Tools tab
        message["content"] = [
            {"type": "tool_use", "id": f"{uuid}-t{i}", "name": t} for i, t in enumerate(tools)
        ]
    return {
        "type": "assistant",
        "sessionId": session,
        "cwd": cwd,
        "timestamp": ts or "2026-06-10T18:46:00.000Z",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": side,
        "requestId": req or (uuid + "-req"),
        "message": message,
    }


def _usage(inp=0, out=0, cr=0, cw=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
    }


def _write_jsonl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _codex_meta(sid, cwd, ts="2025-10-03T14:51:03.966Z", source=None):
    payload = {"id": sid, "timestamp": ts, "cwd": cwd, "git": {"branch": "main"}}
    if source is not None:  # e.g. the spawned-thread {"subagent": {"thread_spawn": ...}}
        payload["source"] = source
    return {"timestamp": ts, "type": "session_meta", "payload": payload}


def _codex_turn(model, cwd, ts="2025-10-03T14:51:10.000Z"):
    return {"timestamp": ts, "type": "turn_context", "payload": {"cwd": cwd, "model": model}}


def _codex_tokens(inp, out, cached, total, ts="2025-10-03T14:51:20.000Z"):
    # A token_count event carrying the *cumulative* running total (Codex's shape).
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cached_input_tokens": cached,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total,
                }
            },
        },
    }


def _hermes_db_full(path, rows):
    """Hermes state.db superset that also carries the billing/cost columns
    (billing_provider, billing_mode, estimated_cost_usd, actual_cost_usd) so
    metered routes can be exercised. Mirrors the real ~/.hermes/state.db."""
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
            billing_provider TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            archived INTEGER NOT NULL DEFAULT 0
        )"""
    )
    cols = (
        "id",
        "title",
        "model",
        "cwd",
        "parent_session_id",
        "started_at",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "billing_provider",
        "billing_mode",
        "estimated_cost_usd",
        "actual_cost_usd",
        "archived",
    )
    conn.executemany(
        f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
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
                r.get("provider"),
                r.get("billing_mode"),
                r.get("estimated_cost_usd"),
                r.get("actual_cost_usd"),
                r.get("archived", 0),
            )
            for r in rows
        ],
    )
    conn.commit()
    conn.close()


# --- CSV adapter (a CSV of logged API requests, e.g. GitHub Copilot) ---------


def _write_csv(path, header, rows):
    import csv as _csv

    with open(path, "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def _jsonl_args():
    return type("Args", (), {"demo": False})()


def _parse(argv):
    import sys as _sys

    old = _sys.argv
    _sys.argv = ["opentab"] + list(argv)
    try:
        return ot.parse_args()
    finally:
        _sys.argv = old


PI_SID = "019e2a8c-dfcc-77f3-a956-c3ee1862aca3"


def _pi_args():
    return type("Args", (), {"demo": False})()


def _pi_session(sid, cwd, ts="2026-05-15T07:32:15.949Z"):
    return {"type": "session", "version": 3, "id": sid, "timestamp": ts, "cwd": cwd}


def _pi_user(text, mid="u1", ts="2026-05-15T07:32:34.188Z"):
    return {
        "type": "message",
        "id": mid,
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _pi_assistant(
    model,
    inp,
    out,
    cache_read=0,
    cache_write=0,
    total=None,
    cost=None,
    provider=None,
    api=None,
    mid="a1",
    ts="2026-05-15T07:32:36.257Z",
    tools=None,
):
    usage = {"input": inp, "output": out, "cacheRead": cache_read, "cacheWrite": cache_write}
    usage["totalTokens"] = total if total is not None else inp + out + cache_read + cache_write
    if cost is not None:
        usage["cost"] = {"total": cost}
    message = {"role": "assistant", "model": model, "usage": usage}
    if provider is not None:
        message["provider"] = provider
    if api is not None:
        message["api"] = api
    if tools:  # the step's toolCall blocks, for the Tools tab
        message["content"] = [
            {"type": "toolCall", "id": f"{mid}-t{i}", "name": t, "arguments": {}}
            for i, t in enumerate(tools)
        ]
    return {"type": "message", "id": mid, "timestamp": ts, "message": message}


def _pi_write(root, project, sid, rows, ts_prefix="2026-05-15T07-32-15-949Z"):
    d = os.path.join(root, project)
    os.makedirs(d, exist_ok=True)
    _write_jsonl(os.path.join(d, f"{ts_prefix}_{sid}.jsonl"), rows)


OCL_SID = "01998b2c-7d41-7a90-bf03-2b6e1c9f04aa"


def _ocl_args():
    return type("Args", (), {"demo": False})()


def _ocl_user(text, mid="u1", ts="2026-04-27T16:00:00.000Z"):
    return {
        "type": "message",
        "id": mid,
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _ocl_msg(
    model,
    inp,
    out,
    cache_read=0,
    cache_write=0,
    total=None,
    cost=None,
    provider=None,
    api=None,
    mid="a1",
    ts="2026-04-27T16:00:16.401Z",
):
    usage = {"input": inp, "output": out, "cacheRead": cache_read, "cacheWrite": cache_write}
    usage["totalTokens"] = total if total is not None else inp + out + cache_read + cache_write
    if cost is not None:
        usage["cost"] = {"total": cost}  # OpenClaw records cost as an object; only .total read
    message = {"role": "assistant", "usage": usage}
    if model is not None:
        message["model"] = model
    if provider is not None:
        message["provider"] = provider
    if api is not None:
        message["api"] = api
    return {"type": "message", "id": mid, "timestamp": ts, "message": message}


def _ocl_write(root, agent, sid, rows, suffix=".jsonl"):
    d = os.path.join(root, "agents", agent, "sessions")
    os.makedirs(d, exist_ok=True)
    _write_jsonl(os.path.join(d, f"{sid}{suffix}"), rows)


def _zaly_args():
    return type("Args", (), {"demo": False})()


def _zaly_store(root, state_dir=None):
    # Pin $ZALY_STATE while constructing (auth.json is read in __init__) so the test
    # never picks up the developer's real ~/.local/state/zaly/auth.json.
    old = os.environ.get("ZALY_STATE")
    os.environ["ZALY_STATE"] = state_dir or os.path.join(root, "state-none")
    try:
        return ot.ZalyStore(root, _zaly_args())
    finally:
        if old is None:
            os.environ.pop("ZALY_STATE", None)
        else:
            os.environ["ZALY_STATE"] = old


def _zaly_settings(sid, workspace, model="anthropic/claude-opus-4-6", ts=1783696388553):
    return {
        "type": "session-settings",
        "uuid": "n-settings",
        "ts": ts,
        "settings": {
            "version": 2,
            "sessionId": sid,
            "cwd": workspace,
            "workspace": workspace,
            "modelId": model,
        },
    }


def _zaly_user(text, mid="u1", ts=1783696388555):
    return {
        "type": "message",
        "uuid": f"n-{mid}",
        "ts": ts,
        "message": {"role": "user", "content": text, "id": mid, "ts": ts},
    }


def _zaly_assistant(
    model,
    inp,
    out,
    cache_read=0,
    cache_write=0,
    reasoning=0,
    cost=None,
    mid="a1",
    ts=1783696394242,
    tools=None,
):
    # Usage lives on message.meta (unlike pi/OpenClaw); `cost` is a per-component USD
    # object mirroring the token fields -- there is no .total.
    usage = {
        "input": inp,
        "output": out,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "reasoning": reasoning,
    }
    if cost is not None:
        usage["cost"] = cost
    content = [{"type": "text", "text": "ok"}]
    if tools:
        content += [
            {"type": "tool-call", "id": f"{mid}-t{i}", "name": t, "params": {}}
            for i, t in enumerate(tools)
        ]
    meta = {"finishReason": "stop", "usage": usage}
    if model is not None:
        meta["modelId"] = model
    return {
        "type": "message",
        "uuid": f"n-{mid}",
        "ts": ts,
        "message": {"role": "assistant", "content": content, "meta": meta, "id": mid, "ts": ts},
    }


def _zaly_write(root, slug, dir_id, rows):
    d = os.path.join(root, "sessions", slug, dir_id)
    os.makedirs(d, exist_ok=True)
    _write_jsonl(os.path.join(d, "session.jsonl"), rows)
