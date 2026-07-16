"""The Zaly session backend (stores/zaly.py)."""

import json
import os
import tempfile

import opentab as ot

from tests._support import (
    FakeStore,
    _zaly_assistant,
    _zaly_settings,
    _zaly_store,
    _zaly_user,
    _zaly_write,
    workflow,
)

ZALY_SID = "019f4c95-ffa0-7e39-875c-2e9b34958e7f"


def test_zaly_store_sums_cost_components_and_folds_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "zaly")
        # Session ran in <repo>/sub; the settings workspace folds to the git root.
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        # A direct-Anthropic-key turn: provider isn't OAuth/plan -> metered, real spend.
        # zaly's cost is per-component; the store sums whatever components exist.
        rows = [
            _zaly_settings(ZALY_SID, sub),
            _zaly_user("summarize the budget"),
            _zaly_assistant(
                "anthropic/claude-opus-4-6",
                1660,
                55,
                cache_read=108928,
                cost={"input": 0.0083, "output": 0.004125, "cacheRead": 0.0326784},
            ),
        ]
        # The dir name differs from settings.sessionId -> the settings id wins.
        _zaly_write(root, "+tmp+repo+sub", "0000-dir-name-uuid", rows)
        store = _zaly_store(root)
        assert store.records_cost is True  # a metered cost -> real spend
        wfs = store.workflows()
        assert len(wfs) == 1
        w = wfs[0]
        assert w.id == ZALY_SID  # settings.sessionId over the directory name
        assert w.source == "Zaly"
        assert w.subagents == 0
        assert w.directory == repo  # folded to the git root, not bare "sub"
        assert w.title == "summarize the budget"
        assert w.created_at.startswith("2026-07-10")  # epoch-ms ts
        assert w.total_cost == 0.045103  # 0.0083 + 0.004125 + 0.0326784, rounded to 6dp
        assert w.total_tokens == 110643  # 1660 + 55 + 108928 (input not reduced)
        assert w.unpriced_tokens == 0  # priced -> nothing left for "$" to estimate

        row = next(r for r in store.model_breakdown() if r["root_id"] == ZALY_SID)
        assert row["model_name"] == "anthropic/claude-opus-4-6"  # verbatim (already prefixed)
        assert row["input"] == 1660 and row["cache_read"] == 108928
        assert row["unpriced_input"] == 0  # priced row -> unpriced split zeroed

        nodes = store.workflow_nodes(ZALY_SID)
        assert len(nodes) == 1 and nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["cost"] == 0.045103


def test_zaly_store_codex_route_is_subscription_without_auth_json():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "zaly")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # A ChatGPT-plan login (openai-codex): zaly still writes a list-price cost, but
        # it is NOT what the user pays -- the "codex" provider marker catches it even
        # with no auth.json readable.
        rows = [
            _zaly_settings(ZALY_SID, cwd, model="openai-codex/gpt-5.6-sol"),
            _zaly_user("hi there"),
            _zaly_assistant(
                "openai-codex/gpt-5.6-sol",
                13583,
                13,
                cache_read=1536,
                cost={"input": 0.067915, "output": 0.00039, "cacheRead": 0.000768},
            ),
        ]
        _zaly_write(root, "+tmp+repo", ZALY_SID, rows)
        store = _zaly_store(root)
        assert store.records_cost is False  # subscription-only setup -> nothing metered
        w = store.workflows()[0]
        assert w.total_cost == 0.0  # the list-price cost is not real spend
        assert w.total_tokens == w.unpriced_tokens == 15132  # all estimable under "$"
        row = next(r for r in store.model_breakdown() if r["root_id"] == ZALY_SID)
        assert row["cost"] == 0.0 and row["unpriced_input"] == 13583
        assert ot.api_equivalent_cost("openai-codex/gpt-5.6-sol", 13583, 13, 0, 1536, 0) > 0


def test_zaly_store_oauth_route_cost_is_not_real_spend():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "zaly")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # <state>/auth.json marks anthropic as an OAuth (Claude-plan) login -- a provider
        # no marker catches, so this exercises the auth.json probe itself.
        state = os.path.join(tmp, "state")
        os.makedirs(state)
        with open(os.path.join(state, "auth.json"), "w") as fh:
            json.dump({"anthropic": {"type": "oauth", "token": {"access": "x"}}}, fh)
        rows = [
            _zaly_settings(ZALY_SID, cwd),
            _zaly_user("go"),
            _zaly_assistant(
                "anthropic/claude-opus-4-6", 8000, 300, cost={"input": 0.04, "output": 0.0225}
            ),
        ]
        _zaly_write(root, "+tmp+repo", ZALY_SID, rows)
        store = _zaly_store(root, state_dir=state)
        assert store.records_cost is False  # OAuth route -> nothing metered
        w = store.workflows()[0]
        assert w.total_cost == 0.0
        assert w.total_tokens == w.unpriced_tokens == 8300
        row = next(r for r in store.model_breakdown() if r["root_id"] == ZALY_SID)
        assert row["cost"] == 0.0 and row["unpriced_input"] == 8000


def test_zaly_store_dedupes_by_message_id_and_drops_settings_only_sessions():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "zaly")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        a = _zaly_assistant(
            "anthropic/claude-sonnet-4-5", 100, 50, cost={"input": 0.008, "output": 0.002}, mid="d"
        )
        rows = [_zaly_settings(ZALY_SID, cwd), _zaly_user("go"), a, dict(a)]  # same id twice
        _zaly_write(root, "+tmp+repo", ZALY_SID, rows)
        # Launching zaly writes a settings-only session.jsonl -> no usage -> dropped.
        stub = "019f4c94-a66f-753e-9185-0667ca572317"
        _zaly_write(root, "+tmp+repo", stub, [_zaly_settings(stub, cwd)])
        store = _zaly_store(root)
        wfs = store.workflows()
        assert len(wfs) == 1 and wfs[0].id == ZALY_SID  # the stub never surfaces
        row = next(r for r in store.model_breakdown() if r["root_id"] == ZALY_SID)
        assert row["runs"] == 1  # the duplicate assistant step was not double-counted
        assert row["tokens_total"] == 150
        assert abs(row["cost"] - 0.01) < 1e-9


def test_zaly_store_reasoning_stays_inside_output():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "zaly")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # zaly's `reasoning` is OpenAI's reasoning_tokens detail -- a SUBSET of output
        # (219 includes the 49). opentab's reasoning column is additive, so it must stay
        # 0 or the "$" estimate would bill those 49 tokens twice.
        rows = [
            _zaly_settings(ZALY_SID, cwd, model="openai-codex/gpt-5.6-sol"),
            _zaly_user("what do you know about me"),
            _zaly_assistant("openai-codex/gpt-5.6-sol", 15144, 219, reasoning=49),
        ]
        _zaly_write(root, "+tmp+repo", ZALY_SID, rows)
        row = next(r for r in _zaly_store(root).model_breakdown() if r["root_id"] == ZALY_SID)
        assert row["output"] == 219 and row["reasoning"] == 0
        assert row["tokens_total"] == 15363  # 15144 + 219; the 49 never added on top


def test_zaly_turns_timeline_groups_by_prompt_and_feeds_tools():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "zaly")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _zaly_settings(ZALY_SID, cwd, model="openai-codex/gpt-5.6-sol"),
            _zaly_user("hi there", mid="u1", ts=1783696388555),
            _zaly_assistant("openai-codex/gpt-5.6-sol", 13583, 13, mid="a1", ts=1783696394242),
            _zaly_user("now grep the repo", mid="u2", ts=1783696405836),
            _zaly_assistant(
                "openai-codex/gpt-5.6-sol",
                15144,
                219,
                mid="a2",
                ts=1783696414305,
                tools=["bash", "read"],
            ),
        ]
        _zaly_write(root, "+tmp+repo", ZALY_SID, rows)
        store = _zaly_store(root)
        assert store.supports_turns(ZALY_SID) and store.supports_tools(ZALY_SID)
        tl = store.message_timeline(ZALY_SID)
        assert [t["prompt_title"] for t in tl] == ["hi there", "now grep the repo"]
        assert tl[0]["cost"] == 0.0  # subscription turn -> "$" estimates it
        assert tl[1]["tools"] == ["bash", "read"]
        tools = {r["tool"]: r for r in store.tool_breakdown(ZALY_SID)}
        assert set(tools) == {"bash", "read"}  # only the tool-using turn contributes
        assert tools["bash"]["calls"] == 1
        assert tools["bash"]["tokens_total"] == tools["read"]["tokens_total"] == 15363 / 2


def test_zaly_joins_the_source_cycle_and_builds_a_resume_command():
    with tempfile.TemporaryDirectory() as tmp:
        oc_db = os.path.join(tmp, "opencode.db")
        open(oc_db, "w").close()
        root = os.path.join(tmp, "zaly")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _zaly_write(
            root,
            "+tmp+repo",
            ZALY_SID,
            [
                _zaly_settings(ZALY_SID, cwd),
                _zaly_user("go"),
                _zaly_assistant("anthropic/claude-sonnet-4-5", 10, 5),
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
                "db": oc_db,
                "claude_dir": os.path.join(tmp, "no-claude"),
                "codex_dir": os.path.join(tmp, "no-codex"),
                "hermes_db": os.path.join(tmp, "no-hermes.db"),
                "zaly_dir": root,
                "demo": False,
            },
        )()

        assert ot.available_sources(args) == ["opencode", "zaly"]
        assert ot.sources.source_cycle(args) == ["opencode", "zaly", "all"]

        app = ot.App(FakeStore([workflow("a", "2026-06-01 12:00:00")]), args)
        app.source_key = "opencode"
        assert app.next_source_name() == "Zaly"

        wf = workflow(ZALY_SID, "2026-06-01 12:00:00", title="t", directory="/tmp/proj")
        wf.source = "Zaly"
        assert app.resume_command(wf) == f"cd /tmp/proj && zaly --session {ZALY_SID}"


def test_zaly_context_breakdown_mirrors_its_own_estimator():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "zaly")
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        assistant = _zaly_assistant("openai-codex/gpt-5.6", 100, 50, tools=["bash"])
        assistant["message"]["content"].append({"type": "reasoning", "text": "z" * 40})
        # A string-content system message (e.g. a compaction summary) is injected
        # context, never the user's words.
        sysmsg = {
            "type": "message",
            "uuid": "n-sys",
            "ts": 1783696394300,
            "message": {
                "role": "system",
                "content": "compaction summary text",
                "id": "sys1",
                "ts": 1783696394300,
            },
        }
        rows = [
            _zaly_settings("sid-1", repo),
            _zaly_user("hello there friend"),
            sysmsg,
            assistant,
        ]
        _zaly_write(root, "slug", "sid-1", rows)
        store = _zaly_store(root)
        store.workflows()
        got = {(r["category"], r["kind"]): r for r in store.context_breakdown("sid-1")}
        assert store.supports_context("sid-1")
        assert got[("user prompts", "")]["est_tokens"] == 5  # 18 chars / 4, rounded up
        assert got[("assistant text", "")]["count"] == 1  # the "ok" text part
        assert got[("reasoning", "")]["est_tokens"] == 10  # 40 chars / 4
        assert ("tool call params", "bash") in got
        assert got[("injected context", "system")]["est_tokens"] == 6  # 23 chars / 4
        assert got[("user prompts", "")]["count"] == 1  # the system text stayed out
