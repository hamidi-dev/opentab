import os
import tempfile
from unittest.mock import patch

import opentab as ot

from tests._support import (
    OMP_SID,
    _omp_agent_db,
    _omp_args,
    _omp_assistant,
    _omp_session,
    _omp_thinking_level,
    _omp_title_record,
    _omp_user,
    _omp_write,
    _omp_write_subagent,
)


def test_omp_node_prompt_uses_native_uuid_and_own_prompt_not_title_without_usage():
    child = "019fa4fd-aaaa-7000-a6e9-c9e0c7ce25fc"
    sibling = "019fa4fd-bbbb-7000-a6e9-c9e0c7ce25fc"
    nested = "019fa4fd-cccc-7000-a6e9-c9e0c7ce25fc"
    empty = "019fa4fd-dddd-7000-a6e9-c9e0c7ce25fc"
    outside = "019fa4fd-eeee-7000-a6e9-c9e0c7ce25fc"
    prompt = (
        "  Implement exactly the following detailed request.\n\n"
        + ("  Preserve this indented instruction in full.\n" * 12)
        + "Verify all of it.\n"
    )
    ts_prefix = "2026-07-27T19-11-52-093Z"
    with tempfile.TemporaryDirectory() as tmp:
        _omp_write(
            tmp, "project", OMP_SID, [_omp_session(OMP_SID, tmp), _omp_user("Root instruction")]
        )
        _omp_write(
            tmp, "project", outside, [_omp_session(outside, tmp), _omp_user("Outside instruction")]
        )
        for sid, nickname, text, chain in (
            (child, "Scout", prompt, ()),
            (sibling, "Reviewer", "Sibling\nprompt", ()),
            (nested, "Worker", "Nested prompt", ("Scout",)),
            (empty, "Empty", " \n\t", ()),
        ):
            _omp_write_subagent(
                tmp,
                "project",
                ts_prefix,
                OMP_SID,
                nickname,
                [
                    _omp_session(sid, tmp, title="Misleading session title"),
                    _omp_title_record("Misleading summary", changed=True),
                    _omp_user("\n\t", mid="blank"),
                    _omp_user(text),
                    *([] if sid == empty else [_omp_user("Later prompt", mid="later")]),
                ],
                chain=chain,
            )
        store = ot.OmpStore(tmp, _omp_args())
        assert store.workflows() == []
        assert store.node_prompt(OMP_SID, child) == prompt
        assert store.node_prompt(OMP_SID, sibling) == "Sibling\nprompt"
        assert store.node_prompt(OMP_SID, nested) == "Nested prompt"
        assert store.node_timeline(OMP_SID, empty) == []
        assert store.node_turn_content(OMP_SID, empty) == {}
        for workflow_id, node_id in (
            (OMP_SID, OMP_SID),
            (OMP_SID, outside),
            (OMP_SID, empty),
            (OMP_SID, "Scout"),
            (OMP_SID, child[:8]),
            (OMP_SID, "missing"),
            ("missing", child),
            (child, sibling),
        ):
            assert store.node_prompt(workflow_id, node_id) is None
        assert store.workflows() == []
        store.demo = True
        with patch.object(store, "_parse", side_effect=AssertionError("demo read content")):
            assert store.node_prompt(OMP_SID, child) is None
            assert store.node_timeline(OMP_SID, child) is None
            assert store.node_turn_content(OMP_SID, child) == {}


def test_omp_node_prompt_refuses_a_uuid_recorded_under_different_roots():
    other = "019fa4fd-eeee-7000-a6e9-c9e0c7ce25fc"
    child = "019fa4fd-aaaa-7000-a6e9-c9e0c7ce25fc"
    with tempfile.TemporaryDirectory() as tmp:
        for i, root in enumerate((OMP_SID, other)):
            _omp_write(tmp, "project", root, [_omp_session(root, tmp)])
            _omp_write_subagent(
                tmp,
                "project",
                "2026-07-27T19-11-52-093Z",
                root,
                "Scout",
                [
                    _omp_session(child, tmp),
                    _omp_user(f"Private prompt from root {i}"),
                ],
            )
        store = ot.OmpStore(tmp, _omp_args())
        assert store.node_prompt(OMP_SID, child) is None
        assert store.node_prompt(other, child) is None
        for root in (OMP_SID, other):
            assert store.node_timeline(root, child) is None
            assert store.node_turn_content(root, child) == {}


def test_omp_node_turns_and_content_exclude_siblings_and_grandchildren():
    child, sibling, nested = (f"019fa4fd-{n * 4}-7000-a6e9-c9e0c7ce25fc" for n in "abc")
    with tempfile.TemporaryDirectory() as tmp:
        for sid, name, chain in (
            (OMP_SID, None, ()),
            (child, "Scout", ()),
            (sibling, "Sibling", ()),
            (nested, "Nested", ("Scout",)),
        ):
            rows = [
                _omp_session(sid, tmp),
                _omp_user(sid),
                _omp_assistant("gpt-5.6-sol", 100, 10, tools=["read"]),
                {
                    "type": "message",
                    "id": "result",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "a1-t0",
                        "content": [{"type": "text", "text": sid}],
                    },
                },
            ]
            if name:
                _omp_write_subagent(
                    tmp, "project", "2026-07-27T19-11-52-093Z", OMP_SID, name, rows, chain=chain
                )
            else:
                _omp_write(tmp, "project", sid, rows)
        store = ot.OmpStore(tmp, _omp_args())
        workflows = store.workflows()
        before = store.message_timeline(OMP_SID)
        for sid in (OMP_SID, child, sibling, nested):
            rows = store.node_timeline(OMP_SID, sid)
            assert len(rows) == 1 and rows[0]["depth"] == 0
            assert rows[0]["prompt_full"] == sid
            key = rows[0]["content_key"]
            assert key in {r["content_key"] for r in before}
            trace = store.node_turn_content(OMP_SID, sid)
            assert set(trace) == {key} and trace[key][0]["output"] == sid
            assert store.node_turn_content(OMP_SID, sid, key) == trace
        for sid in (OMP_SID, sibling, nested):
            key = store.node_timeline(OMP_SID, sid)[0]["content_key"]
            assert store.node_turn_content(OMP_SID, child, key) == {}
        for owner, node in ((child, sibling), (OMP_SID, "missing"), ("missing", child)):
            assert store.node_timeline(owner, node) is None
            assert store.node_turn_content(owner, node) == {}
        assert store.node_turn_content(OMP_SID, child, "") == {}
        assert store.message_timeline(OMP_SID) == before
        assert store.workflows() == workflows


def test_omp_subagent_transcript_is_never_silently_dropped():
    # omp subagent filenames carry labels; their session UUID is inside the transcript.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        ts_prefix = "2026-07-27T19-11-52-093Z"
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_user("explain the repo using a subagent"),
                _omp_assistant(
                    "gpt-5.6-sol", 1000, 100, cache_read=200, provider="openai-codex", cost=0.05
                ),
            ],
            ts_prefix=ts_prefix,
        )
        _omp_write_subagent(
            root,
            "--proj--",
            ts_prefix,
            OMP_SID,
            "RepoPurposeScout",
            [
                _omp_session("019fa4fd-aaaa-7000-a6e9-c9e0c7ce25fc", cwd),
                _omp_assistant(
                    "gpt-5.6-sol",
                    5000,
                    300,
                    cache_read=1000,
                    provider="openai-codex",
                    cost=0.25,
                    mid="c1",
                ),
            ],
        )
        w = ot.OmpStore(root, _omp_args()).workflows()[0]
        # Root alone would be 1300 (1000+100+200). If this reads 1300, the subagent
        # file got dropped -- exactly the bug this backend exists to prevent.
        assert w.total_tokens == 1300 + 6300, (
            "subagent transcript's tokens went missing -- RepoPurposeScout.jsonl's own "
            "filename carries no uuid, so a naive pi-shaped parser (_id_from_name -> "
            "None) silently drops the whole file; OmpStore must key it by its "
            "containing directory's uuid instead"
        )


def test_omp_root_session_folds_subagent_into_totals_and_root_cost_is_its_own_share():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        child_sid = "019fa4fd-bbbb-7000-a6e9-c9e0c7ce25fc"
        ts_prefix = "2026-07-27T19-11-52-093Z"
        # "anthropic" is neither an oauth provider (no agent.db here) nor a
        # _SUBSCRIPTION_MARKERS substring -> metered, real recorded spend, so root vs
        # subtree cost is a meaningful (non-zero-vs-zero) comparison.
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_user("explain the repo using a subagent"),
                _omp_assistant("claude-opus-4-6", 1000, 100, provider="anthropic", cost=0.03),
            ],
            ts_prefix=ts_prefix,
        )
        _omp_write_subagent(
            root,
            "--proj--",
            ts_prefix,
            OMP_SID,
            "RepoPurposeScout",
            [
                _omp_session(child_sid, cwd),
                _omp_user("  Received child task\n\n  Keep this indentation.\n"),
                _omp_assistant(
                    "claude-opus-4-6", 5000, 300, provider="anthropic", cost=0.15, mid="c1"
                ),
            ],
        )
        store = ot.OmpStore(root, _omp_args())
        assert store.records_cost is True
        wfs = store.workflows()
        assert len(wfs) == 1  # the subagent never appears as its own row
        assert child_sid not in [w.id for w in wfs]
        w = wfs[0]
        assert w.id == OMP_SID
        assert w.subagents == 1
        assert w.total_cost == 0.18  # root 0.03 + child 0.15 -- covers the whole subtree
        assert w.root_cost == 0.03  # the root's OWN share, not the subtree total
        assert w.total_tokens == 1100 + 5300  # (1000+100) + (5000+300)

        nodes = store.workflow_nodes(OMP_SID)
        assert len(nodes) == 2  # depth 0 (root) + depth 1 (the one subagent)
        assert nodes[0]["depth"] == 0 and nodes[0]["agent"] == "-"
        assert nodes[0]["cost"] == 0.03  # the root node shows its OWN share, not 0.18
        assert nodes[1]["depth"] == 1 and nodes[1]["agent"] == "RepoPurposeScout"
        assert nodes[1]["id"] == child_sid
        assert nodes[1]["cost"] == 0.15  # the child's own (leaf) total
        assert all("prompt" not in n and "prompt_full" not in n for n in nodes)
        with patch.object(store, "_parse", side_effect=AssertionError("unnecessary reparse")):
            assert store.node_prompt(OMP_SID, child_sid) == (
                "  Received child task\n\n  Keep this indentation.\n"
            )


def test_omp_model_breakdown_keeps_root_vs_subtree_split_under_subscription():
    # The subtree and root-only unpriced splits must remain distinct.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        child_sid = "019fa4fd-6666-7000-a6e9-c9e0c7ce25fc"
        ts_prefix = "2026-07-27T19-11-52-093Z"
        _omp_agent_db(os.path.join(tmp, "agent.db"), [("openai-codex", "oauth")])
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_assistant(
                    "gpt-5.6-sol", 1000, 100, cache_read=200, provider="openai-codex", cost=0.05
                ),
            ],
            ts_prefix=ts_prefix,
        )
        _omp_write_subagent(
            root,
            "--proj--",
            ts_prefix,
            OMP_SID,
            "RepoPurposeScout",
            [
                _omp_session(child_sid, cwd),
                _omp_assistant(
                    "gpt-5.6-sol",
                    5000,
                    300,
                    cache_read=1000,
                    provider="openai-codex",
                    cost=0.25,
                    mid="c1",
                ),
            ],
        )
        store = ot.OmpStore(root, _omp_args())
        assert store.records_cost is False  # the only route is an oauth (plan) login
        w = store.workflows()[0]
        assert w.total_cost == 0.0 and w.root_cost == 0.0
        row = next(
            r
            for r in store.model_breakdown()
            if r["root_id"] == OMP_SID and r["model_name"] == "openai-codex/gpt-5.6-sol"
        )
        assert row["cost"] == 0.0 and row["root_cost"] == 0.0
        assert row["unpriced_input"] == 6000 and row["root_unpriced_input"] == 1000
        assert row["unpriced_output"] == 400 and row["root_unpriced_output"] == 100
        assert row["unpriced_cache_read"] == 1200 and row["root_unpriced_cache_read"] == 200
        assert row["tokens_total"] == 7600  # 1300 (root) + 6300 (subtree child)


def test_omp_sqlite_auth_marks_oauth_credential_type_as_subscription_and_others_as_metered():
    # acme-cloud deliberately bypasses the inherited subscription markers.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _omp_agent_db(os.path.join(tmp, "agent.db"), [("acme-cloud", "oauth")])
        rows = [
            _omp_session(OMP_SID, cwd),
            _omp_user("hi"),
            _omp_assistant("foo-model", 1000, 100, provider="acme-cloud", cost=0.05),
        ]
        _omp_write(root, "--proj--", OMP_SID, rows)
        store = ot.OmpStore(root, _omp_args())
        assert store.records_cost is False  # oauth credential -> a plan login, not spend
        w = store.workflows()[0]
        assert w.total_cost == 0.0
        assert w.total_tokens == w.unpriced_tokens == 1100

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        # Same provider, same recorded cost -- but its credential is an api key, not
        # oauth, so it's a metered route: the $0.05 is real spend.
        _omp_agent_db(os.path.join(tmp, "agent.db"), [("acme-cloud", "api-key")])
        rows = [
            _omp_session(OMP_SID, cwd),
            _omp_user("hi"),
            _omp_assistant("foo-model", 1000, 100, provider="acme-cloud", cost=0.05),
        ]
        _omp_write(root, "--proj--", OMP_SID, rows)
        store = ot.OmpStore(root, _omp_args())
        assert store.records_cost is True
        w = store.workflows()[0]
        assert w.total_cost == 0.05
        assert w.unpriced_tokens == 0


def test_omp_missing_or_corrupt_agent_db_degrades_to_marker_heuristic_without_raising():
    for corrupt in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "sessions")
            cwd = os.path.join(tmp, "repo")
            os.makedirs(cwd)
            if corrupt:
                with open(os.path.join(tmp, "agent.db"), "wb") as fh:
                    fh.write(b"not a sqlite file at all")
            # "openai-codex" IS a _SUBSCRIPTION_MARKERS substring, so the heuristic
            # alone must still catch it with no usable database.
            rows = [
                _omp_session(OMP_SID, cwd),
                _omp_user("hi"),
                _omp_assistant("gpt-5.6-sol", 1000, 100, provider="openai-codex", cost=0.05),
            ]
            _omp_write(root, "--proj--", OMP_SID, rows)
            store = ot.OmpStore(root, _omp_args())  # must not raise
            assert store.records_cost is False, f"corrupt={corrupt}"
            w = store.workflows()[0]
            assert w.total_cost == 0.0, f"corrupt={corrupt}"


def test_omp_model_label_qualifies_bare_model_with_provider_and_avoids_double_prefixing():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _omp_session(OMP_SID, cwd),
            _omp_user("hi"),
            # provider + bare model -> joined with "/".
            _omp_assistant("gpt-5.6-sol", 100, 10, provider="openai-codex", cost=0.01, mid="a1"),
            # a model that already carries the provider prefix must not be re-prefixed.
            _omp_assistant(
                "openai-codex/gpt-5.6-sol",
                200,
                20,
                provider="openai-codex",
                cost=0.02,
                mid="a2",
                ts="2026-07-27T19:13:00.000Z",
            ),
        ]
        _omp_write(root, "--proj--", OMP_SID, rows)
        store = ot.OmpStore(root, _omp_args())
        rows_out = [r for r in store.model_breakdown() if r["root_id"] == OMP_SID]
        # Both messages must fold into the SAME model row -- if the second one were
        # double-prefixed ("openai-codex/openai-codex/gpt-5.6-sol") it would show up
        # as a second, spurious row instead of merging with the first.
        assert {r["model_name"] for r in rows_out} == {"openai-codex/gpt-5.6-sol"}
        assert rows_out[0]["runs"] == 2


def test_omp_title_precedence_prefers_title_change_over_title_record_and_session_title():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _omp_session(OMP_SID, cwd, title="session title"),
            _omp_title_record("title record", ts="2026-07-27T19:11:53.000Z"),
            _omp_title_record("changed title", ts="2026-07-27T19:11:54.000Z", changed=True),
            _omp_user("first user prompt"),
            _omp_assistant("gpt-5.6-sol", 10, 5, provider="openai-codex"),
        ]
        _omp_write(root, "--proj--", OMP_SID, rows)
        w = ot.OmpStore(root, _omp_args()).workflows()[0]
        assert w.title == "changed title"


def test_omp_title_falls_back_through_title_record_session_title_prompt_and_untitled():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        sid_b = "019fa4fd-1111-7000-a6e9-c9e0c7ce25fc"
        sid_c = "019fa4fd-2222-7000-a6e9-c9e0c7ce25fc"
        sid_d = "019fa4fd-3333-7000-a6e9-c9e0c7ce25fc"
        sid_e = "019fa4fd-4444-7000-a6e9-c9e0c7ce25fc"
        # B: a title record + session.title + a prompt, but no title_change -> the
        # title record wins over session.title.
        _omp_write(
            root,
            "--proj--",
            sid_b,
            [
                _omp_session(sid_b, cwd, title="session title B"),
                _omp_title_record("title record B"),
                _omp_user("prompt B"),
                _omp_assistant("gpt-5.6-sol", 10, 5, provider="openai-codex", mid="b1"),
            ],
        )
        # C: session.title + a prompt, no title records at all -> session.title wins.
        _omp_write(
            root,
            "--proj--",
            sid_c,
            [
                _omp_session(sid_c, cwd, title="session title C"),
                _omp_user("prompt C"),
                _omp_assistant("gpt-5.6-sol", 10, 5, provider="openai-codex", mid="c1"),
            ],
        )
        # D: no title anywhere -> falls back to the first user prompt, like pi.
        _omp_write(
            root,
            "--proj--",
            sid_d,
            [
                _omp_session(sid_d, cwd),
                _omp_user("prompt D"),
                _omp_assistant("gpt-5.6-sol", 10, 5, provider="openai-codex", mid="d1"),
            ],
        )
        # E: no title AND no user prompt -> "(untitled)".
        _omp_write(
            root,
            "--proj--",
            sid_e,
            [
                _omp_session(sid_e, cwd),
                _omp_assistant("gpt-5.6-sol", 10, 5, provider="openai-codex", mid="e1"),
            ],
        )
        titles = {w.id: w.title for w in ot.OmpStore(root, _omp_args()).workflows()}
        assert titles[sid_b] == "title record B"
        assert titles[sid_c] == "session title C"
        assert titles[sid_d] == "prompt D"
        assert titles[sid_e] == "(untitled)"


def test_omp_root_of_resolves_a_subagents_own_uuid_up_to_its_parent_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        child_sid = "019fa4fd-cccc-7000-a6e9-c9e0c7ce25fc"
        ts_prefix = "2026-07-27T19-11-52-093Z"
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_assistant("gpt-5.6-sol", 100, 10, provider="openai-codex", cost=0.01),
            ],
            ts_prefix=ts_prefix,
        )
        _omp_write_subagent(
            root,
            "--proj--",
            ts_prefix,
            OMP_SID,
            "RepoPurposeScout",
            [
                _omp_session(child_sid, cwd),
                _omp_assistant(
                    "gpt-5.6-sol", 500, 30, provider="openai-codex", cost=0.05, mid="c1"
                ),
            ],
        )
        store = ot.OmpStore(root, _omp_args())
        assert store.root_of(OMP_SID) == OMP_SID  # a root's own uuid is a filename hit
        # The subagent's OWN uuid appears in no filename anywhere -- only inside its
        # own `session` record -- so this must read the file's head, not glob for it.
        assert store.root_of(child_sid) == OMP_SID
        assert store.root_of("00000000-0000-0000-0000-000000000000") is None


def test_omp_status_nodes_returns_the_subtree_without_a_full_parse():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        child_sid = "019fa4fd-dddd-7000-a6e9-c9e0c7ce25fc"
        ts_prefix = "2026-07-27T19-11-52-093Z"
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_assistant("gpt-5.6-sol", 100, 10, provider="openai-codex", cost=0.01),
            ],
            ts_prefix=ts_prefix,
        )
        _omp_write_subagent(
            root,
            "--proj--",
            ts_prefix,
            OMP_SID,
            "RepoPurposeScout",
            [
                _omp_session(child_sid, cwd),
                _omp_assistant(
                    "gpt-5.6-sol", 500, 30, provider="openai-codex", cost=0.05, mid="c1"
                ),
            ],
        )
        # An unrelated session elsewhere under the same root -- status_nodes must
        # answer without ever needing to read it.
        other_sid = "019fa4fd-eeee-7000-a6e9-c9e0c7ce25fc"
        _omp_write(
            root,
            "--other-proj--",
            other_sid,
            [
                _omp_session(other_sid, cwd),
                _omp_assistant("gpt-5.6-sol", 999999, 999999, provider="openai-codex", cost=99),
            ],
        )
        store = ot.OmpStore(root, _omp_args())
        assert store._sessions is None  # nothing parsed yet
        nodes = store.status_nodes(OMP_SID)
        # A status poll must never trigger the full-tree parse -- still nothing cached.
        assert store._sessions is None
        assert {(n["depth"], n["agent"]) for n in nodes} == {(0, "-"), (1, "RepoPurposeScout")}
        assert any(n["id"] == child_sid for n in nodes)


def test_omp_turns_and_tools_tag_subagent_rows_with_depth_and_agent_name():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        child_sid = "019fa4fd-ffff-7000-a6e9-c9e0c7ce25fc"
        ts_prefix = "2026-07-27T19-11-52-093Z"
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_user("explain the repo using a subagent", ts="2026-07-27T19:11:55.000Z"),
                _omp_assistant(
                    "gpt-5.6-sol",
                    100,
                    10,
                    provider="openai-codex",
                    cost=0.01,
                    mid="a1",
                    ts="2026-07-27T19:12:00.000Z",
                    tools=["task"],
                ),
            ],
            ts_prefix=ts_prefix,
        )
        _omp_write_subagent(
            root,
            "--proj--",
            ts_prefix,
            OMP_SID,
            "RepoPurposeScout",
            [
                _omp_session(child_sid, cwd, ts="2026-07-27T19:12:05.000Z"),
                _omp_assistant(
                    "gpt-5.6-sol",
                    500,
                    30,
                    provider="openai-codex",
                    cost=0.05,
                    mid="a1",  # same as the root: trace identity must stay session-qualified
                    ts="2026-07-27T19:12:10.000Z",
                    tools=["grep"],
                ),
            ],
        )
        store = ot.OmpStore(root, _omp_args())
        store.workflows()
        assert store.supports_turns(OMP_SID) and store.supports_tools(OMP_SID)
        turns = store.message_timeline(OMP_SID)
        assert [(t["depth"], t["agent"]) for t in turns] == [(0, "-"), (1, "RepoPurposeScout")]
        assert turns[0]["content_key"] != turns[1]["content_key"]
        trace = store.turn_content(OMP_SID)
        assert [trace[t["content_key"]][0]["name"] for t in turns] == ["task", "grep"]
        key = turns[1]["content_key"]
        assert store.turn_content(OMP_SID, content_key=key) == {key: trace[key]}
        own = store.node_timeline(OMP_SID, child_sid)
        assert len(own) == 1 and own[0]["prompt_full"] == ""  # no prompt is still supported
        assert store.node_turn_content(OMP_SID, child_sid, key) == {key: trace[key]}
        tools = {r["tool"]: r for r in store.tool_breakdown(OMP_SID)}
        assert set(tools) == {"task", "grep"}
        assert tools["grep"]["tokens_total"] == 530  # the subagent's own step


def test_omp_turns_carry_the_thinking_level_in_force_at_each_message():
    # omp writes the level as its own `thinking_level_change` record rather than on each
    # message, so it is a RUNNING value the turn rows read off the session -- the seam
    # pi.py leaves for exactly this (PiStore records no level at all and ships "").
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_thinking_level("medium"),
                _omp_user("go", ts="2026-07-27T19:11:55.000Z"),
                _omp_assistant(
                    "gpt-5.6-sol",
                    100,
                    50,
                    provider="openai-codex",
                    cost=0.01,
                    mid="a1",
                    ts="2026-07-27T19:11:58.000Z",
                ),
                _omp_thinking_level("high", ts="2026-07-27T19:12:00.000Z", mid="tl2"),
                _omp_assistant(
                    "gpt-5.6-sol",
                    200,
                    60,
                    provider="openai-codex",
                    cost=0.02,
                    mid="a2",
                    ts="2026-07-27T19:12:10.000Z",
                ),
            ],
        )
        store = ot.OmpStore(root, _omp_args())
        store.workflows()
        assert [t["effort"] for t in store.message_timeline(OMP_SID)] == ["medium", "high"]


def test_omp_dedupes_assistant_messages_by_id():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        a = _omp_assistant("gpt-5.6-sol", 100, 50, provider="openai-codex", cost=0.01, mid="dupe")
        rows = [_omp_session(OMP_SID, cwd), _omp_user("go"), a, dict(a)]  # same id twice
        _omp_write(root, "--proj--", OMP_SID, rows)
        row = next(
            r for r in ot.OmpStore(root, _omp_args()).model_breakdown() if r["root_id"] == OMP_SID
        )
        assert row["runs"] == 1  # the duplicate assistant step was not double-counted
        assert row["tokens_total"] == 150


def test_omp_drops_sessions_with_no_recorded_usage():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        dead_sid = "019fa4fb-0000-7000-a6e9-c9e0c7ce25fc"
        # A stub: an assistant message whose usage is all zeros -- exactly the shape
        # of the 2 sessions the real corpus dropped (usage present, but empty).
        _omp_write(
            root,
            "--proj--",
            dead_sid,
            [
                _omp_session(dead_sid, cwd),
                _omp_assistant("gpt-5.6-sol", 0, 0, provider="openai-codex"),
            ],
        )
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_assistant("gpt-5.6-sol", 100, 10, provider="openai-codex", cost=0.01),
            ],
        )
        ids = {w.id for w in ot.OmpStore(root, _omp_args()).workflows()}
        assert ids == {OMP_SID}  # the usage-less stub never surfaces


def test_omp_folds_cwd_to_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        repo = os.path.join(tmp, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, sub),
                _omp_assistant("gpt-5.6-sol", 100, 10, provider="openai-codex", cost=0.01),
            ],
        )
        w = ot.OmpStore(root, _omp_args()).workflows()[0]
        assert w.directory == repo  # folded to the git root, not the bare "sub" cwd


def test_omp_reasoning_tokens_are_a_subset_of_output_and_never_double_counted():
    # reasoningTokens is a detail of output, not an additive token category.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_assistant(
                    "gpt-5.6-sol",
                    15317,
                    276,
                    cache_read=9984,
                    cache_write=0,
                    total=25577,
                    provider="openai-codex",
                    cost=0.089857,
                    reasoning_tokens=54,
                ),
            ],
        )
        row = next(
            r for r in ot.OmpStore(root, _omp_args()).model_breakdown() if r["root_id"] == OMP_SID
        )
        assert row["tokens_total"] == 25577  # NOT 25577 + 54
        assert row["reasoning"] == 0  # reasoningTokens never surfaces as an add-on


def test_omp_child_of_a_usage_less_root_surfaces_as_its_own_standalone_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        child_sid = "019fa4fd-5555-7000-a6e9-c9e0c7ce25fc"
        ts_prefix = "2026-07-27T19-11-52-093Z"
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_assistant("gpt-5.6-sol", 0, 0, provider="openai-codex"),  # no usage at all
            ],
            ts_prefix=ts_prefix,
        )
        _omp_write_subagent(
            root,
            "--proj--",
            ts_prefix,
            OMP_SID,
            "RepoPurposeScout",
            [
                _omp_session(child_sid, cwd),
                _omp_assistant(
                    "gpt-5.6-sol", 500, 30, provider="openai-codex", cost=0.05, mid="c1"
                ),
            ],
        )
        wfs = ot.OmpStore(root, _omp_args()).workflows()
        assert [w.id for w in wfs] == [child_sid]  # never lost, just re-labeled as a root
        assert wfs[0].subagents == 0


def test_omp_recent_roots_takes_freshness_from_the_whole_subtree():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        ts_prefix = "2026-07-27T19-11-52-093Z"
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_user("delegate this"),
                _omp_assistant("gpt-5.6-sol", 1000, 100, provider="openai-codex", cost=0.05),
            ],
            ts_prefix=ts_prefix,
        )
        _omp_write_subagent(
            root,
            "--proj--",
            ts_prefix,
            OMP_SID,
            "RepoPurposeScout",
            [
                _omp_session("019fa4fd-aaaa-7000-a6e9-c9e0c7ce25fc", cwd),
                _omp_assistant(
                    "gpt-5.6-sol", 5000, 300, provider="openai-codex", cost=0.25, mid="c1"
                ),
            ],
        )
        parent = os.path.join(root, "--proj--", f"{ts_prefix}_{OMP_SID}.jsonl")
        child = os.path.join(root, "--proj--", f"{ts_prefix}_{OMP_SID}", "RepoPurposeScout.jsonl")
        # The root went quiet an hour ago; the subagent wrote a second ago.
        os.utime(parent, (1_800_000_000, 1_800_000_000))
        os.utime(child, (1_800_003_600, 1_800_003_600))

        rows = ot.OmpStore(root, _omp_args()).recent_roots()
        assert [r["id"] for r in rows] == [OMP_SID]  # keyed by the ROOT, not the child
        # The child's mtime, not the root's -- a busy subtree is not an idle session.
        assert rows[0]["last_active"] == 1_800_003_600 * 1000
        assert rows[0]["directory"] == cwd  # still resolved lazily off the file head


def test_omp_folds_a_nested_grandchild_subagent_at_full_depth():
    # Parentage follows the recursively nested transcript path, not directory UUIDs.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        ts = "2026-07-27T19-11-52-093Z"
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_user("build it"),
                _omp_assistant("gpt-5.6-sol", 100, 10, provider="openai-codex", cost=0.01),
            ],
            ts_prefix=ts,
        )
        _omp_write_subagent(
            root,
            "--proj--",
            ts,
            OMP_SID,
            "Impl",
            [
                _omp_session("019fa4fd-bbbb-7000-a6e9-c9e0c7ce25fc", cwd),
                _omp_assistant(
                    "gpt-5.6-sol", 200, 20, provider="openai-codex", cost=0.02, mid="c1"
                ),
            ],
        )
        # The grandchild: spawned BY "Impl", so it nests one level deeper.
        _omp_write_subagent(
            root,
            "--proj--",
            ts,
            OMP_SID,
            "Helper",
            [
                _omp_session("019fa4fd-cccc-7000-a6e9-c9e0c7ce25fc", cwd),
                _omp_assistant(
                    "gpt-5.6-sol", 400, 40, provider="openai-codex", cost=0.04, mid="g1"
                ),
            ],
            chain=("Impl",),
        )
        store = ot.OmpStore(root, _omp_args())
        wfs = store.workflows()
        assert [w.id for w in wfs] == [OMP_SID]  # both descendants roll up, neither is a row
        # 110 root + 220 child + 440 grandchild. Reading 330 means the grandchild
        # transcript was dropped -- the depth-1-only bug this test exists to catch.
        assert wfs[0].total_tokens == 110 + 220 + 440, "nested grandchild transcript was dropped"
        assert wfs[0].subagents == 2  # both descendants counted, not just the direct child

        nodes = store.workflow_nodes(OMP_SID)
        assert [(n["depth"], n["agent"]) for n in nodes] == [
            (0, "-"),
            (1, "Impl"),
            (2, "Helper"),  # parented to Impl, NOT flattened onto the root
        ]
        # The status one-shot must agree with the full parse, including at depth 2.
        fresh = ot.OmpStore(root, _omp_args())
        assert sum(n["tokens_total"] for n in fresh.status_nodes(OMP_SID)) == 110 + 220 + 440
        assert store.root_of("019fa4fd-cccc-7000-a6e9-c9e0c7ce25fc") == OMP_SID


def test_omp_orphaned_subagent_is_its_own_root_consistently_across_workflows_and_status():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        orphan = "019fa4fd-dddd-7000-a6e9-c9e0c7ce25fc"
        # Note: no _omp_write for OMP_SID -- the directory exists, the parent file
        # does not.
        _omp_write_subagent(
            root,
            "--proj--",
            "2026-07-27T19-11-52-093Z",
            OMP_SID,
            "Scout",
            [
                _omp_session(orphan, cwd),
                _omp_assistant(
                    "gpt-5.6-sol", 300, 30, provider="openai-codex", cost=0.03, mid="o1"
                ),
            ],
        )
        store = ot.OmpStore(root, _omp_args())
        wfs = store.workflows()
        assert [w.id for w in wfs] == [orphan]  # data preserved under its own id
        assert wfs[0].total_tokens == 330
        assert store.root_of(orphan) == orphan  # NOT the parent uuid that has no file
        fresh = ot.OmpStore(root, _omp_args())
        assert sum(n["tokens_total"] for n in fresh.status_nodes(orphan)) == 330
        # recent_roots must key the same id, or a directory-scoped status poll would
        # name a root that status_nodes cannot price.
        assert [r["id"] for r in ot.OmpStore(root, _omp_args()).recent_roots()] == [orphan]


def test_omp_a_resumed_root_keeps_children_spawned_from_every_transcript():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        first, second = "2026-07-27T19-00-00-000Z", "2026-07-27T20-00-00-000Z"
        for ts, mid in ((first, "a1"), (second, "a2")):
            _omp_write(
                root,
                "--proj--",
                OMP_SID,
                [
                    _omp_session(OMP_SID, cwd),
                    _omp_assistant(
                        "gpt-5.6-sol", 100, 10, provider="openai-codex", cost=0.01, mid=mid
                    ),
                ],
                ts_prefix=ts,
            )
        # One subagent under EACH resume's directory.
        for ts, sid, mid in (
            (first, "019fa4fd-1111-7000-a6e9-c9e0c7ce25fc", "c1"),
            (second, "019fa4fd-2222-7000-a6e9-c9e0c7ce25fc", "c2"),
        ):
            _omp_write_subagent(
                root,
                "--proj--",
                ts,
                OMP_SID,
                "Scout",
                [
                    _omp_session(sid, cwd),
                    _omp_assistant(
                        "gpt-5.6-sol", 200, 20, provider="openai-codex", cost=0.02, mid=mid
                    ),
                ],
            )
        wfs = ot.OmpStore(root, _omp_args()).workflows()
        # Both children must fold in: 2x110 own + 2x220 children. A child stranded by
        # the older resume path would show up as a second row and be missing here.
        assert [w.id for w in wfs] == [OMP_SID]
        assert wfs[0].subagents == 2
        assert wfs[0].total_tokens == 220 + 440


def test_omp_a_usage_less_intermediate_subagent_is_spliced_not_cut():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        ts = "2026-07-27T19-11-52-093Z"
        router = "019fa4fd-3333-7000-a6e9-c9e0c7ce25fc"
        worker = "019fa4fd-4444-7000-a6e9-c9e0c7ce25fc"
        prompt = "  Worker's own task\n  Preserve indentation.\n"
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_assistant("gpt-5.6-sol", 100, 10, provider="openai-codex", cost=0.01),
            ],
            ts_prefix=ts,
        )
        # The intermediate: a session record and nothing else -- no usage at all.
        _omp_write_subagent(
            root,
            "--proj--",
            ts,
            OMP_SID,
            "Router",
            [_omp_session(router, cwd)],
        )
        _omp_write_subagent(
            root,
            "--proj--",
            ts,
            OMP_SID,
            "Worker",
            [
                _omp_session(worker, cwd),
                _omp_user(prompt),
                _omp_assistant(
                    "gpt-5.6-sol",
                    700,
                    70,
                    provider="openai-codex",
                    cost=0.07,
                    mid="w1",
                    tools=["read"],
                ),
                {
                    "type": "message",
                    "id": "result",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "w1-t0",
                        "content": [{"type": "text", "text": "worker output"}],
                    },
                },
            ],
            chain=("Router",),
        )
        store = ot.OmpStore(root, _omp_args())
        assert store.node_prompt(OMP_SID, worker) == prompt
        turns = store.node_timeline(OMP_SID, worker)
        assert len(turns) == 1 and turns[0]["depth"] == 0
        assert turns[0]["prompt_full"] == prompt.strip()
        key = turns[0]["content_key"]
        content = store.node_turn_content(OMP_SID, worker)
        assert content[key][0]["output"] == "worker output"
        assert store._sessions is None  # cold detail reads must not populate rollups
        wfs = store.workflows()
        assert [w.id for w in wfs] == [OMP_SID]  # the grandchild is NOT its own root
        assert wfs[0].total_tokens == 110 + 770
        assert wfs[0].total_cost == wfs[0].root_cost == 0  # subscription route
        cached = store._sessions
        assert router not in cached and OMP_SID in cached and worker in cached
        models = store.model_breakdown()
        with patch.object(store, "_parse", wraps=store._parse) as parse:
            for _ in range(3):
                assert store.node_prompt(OMP_SID, worker) == prompt
                assert store.node_timeline(OMP_SID, worker) == turns
                assert store.node_turn_content(OMP_SID, worker) == content
                assert store.node_turn_content(OMP_SID, worker, key) == content
            assert parse.call_count == 1  # share lazy full ancestry across expanded reads
            parse.assert_called_with(include_empty=True)
        assert store._sessions is cached and router not in cached
        assert store.model_breakdown() == models
        # Spliced onto the root: the usage-less Router is gone, so Worker sits at
        # depth 1 rather than dangling.
        assert [(n["depth"], n["agent"]) for n in store.workflow_nodes(OMP_SID)] == [
            (0, "-"),
            (1, "Worker"),
        ]
        # And the status one-shot must agree with that, not report a partial subtree.
        fresh = ot.OmpStore(root, _omp_args())
        assert sum(n["tokens_total"] for n in fresh.status_nodes(OMP_SID)) == 110 + 770
        assert fresh.root_of("019fa4fd-4444-7000-a6e9-c9e0c7ce25fc") == OMP_SID
        assert store.workflows() == wfs
        with patch.object(store, "_parse", wraps=store._parse) as parse:
            assert store.node_timeline(OMP_SID, worker) == turns
            parse.assert_called_once_with(include_empty=True)  # a new rollup snapshot
        # Resumed router files under different roots make the worker's ancestry ambiguous.
        other = "019fa4fd-5555-7000-a6e9-c9e0c7ce25fc"
        _omp_write(
            root,
            "--proj--",
            other,
            [
                _omp_session(other, cwd),
                _omp_assistant("gpt-5.6-sol", 10, 1),
            ],
            ts_prefix=ts,
        )
        _omp_write_subagent(root, "--proj--", ts, other, "Router", [_omp_session(router, cwd)])
        store.workflows()
        with patch.object(store, "_parse", wraps=store._parse) as parse:
            for owner in (OMP_SID, other):
                assert store.node_prompt(owner, worker) is None
                assert store.node_timeline(owner, worker) is None
                assert store.node_turn_content(owner, worker, key) == {}
            parse.assert_called_once_with(include_empty=True)


def test_omp_cache_fingerprint_covers_the_wal_but_stays_stable_across_reads():
    # Credential changes can live in -wal; volatile -shm state must not invalidate cache.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        db = os.path.join(tmp, "agent.db")
        _omp_agent_db(db, [("acme-cloud", "oauth")], wal=True)
        _omp_write(
            root,
            "--proj--",
            OMP_SID,
            [
                _omp_session(OMP_SID, cwd),
                _omp_assistant("m", 100, 10, provider="acme-cloud", cost=0.01),
            ],
        )
        inputs = ot.OmpStore(root, _omp_args()).cache_inputs()
        assert any(p.endswith("agent.db") for p in inputs)
        assert any(p.endswith("agent.db-wal") for p in inputs)
        assert not any(p.endswith("-shm") for p in inputs), (
            "-shm is rewritten by every SQLite open, so fingerprinting it would make "
            "the warm-start cache miss on every launch"
        )

        def fingerprint():
            # CachedStore._fingerprint's rule: (path, size, mtime_ns), missing skipped.
            out = []
            for p in ot.OmpStore(root, _omp_args()).cache_inputs():
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((p, st.st_size, st.st_mtime_ns))
            return sorted(out)

        # Each call above already opened the credentials db read-only; the fingerprint
        # must be identical anyway, or the cache would thrash.
        assert fingerprint() == fingerprint()


def test_omp_status_prices_the_subtree_of_a_root_that_only_delegated():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        ts = "2026-07-27T19-11-52-093Z"
        _omp_write(root, "--proj--", OMP_SID, [_omp_session(OMP_SID, cwd)], ts_prefix=ts)
        _omp_write_subagent(
            root,
            "--proj--",
            ts,
            OMP_SID,
            "Scout",
            [
                _omp_session("019fa4fd-9999-7000-a6e9-c9e0c7ce25fc", cwd),
                _omp_assistant(
                    "gpt-5.6-sol", 500, 30, provider="openai-codex", cost=0.05, mid="c1"
                ),
            ],
        )
        nodes = ot.OmpStore(root, _omp_args()).status_nodes(OMP_SID)
        assert [(n["depth"], n["agent"]) for n in nodes] == [(0, "-"), (1, "Scout")]
        assert sum(n["tokens_total"] for n in nodes) == 530, "delegated tokens lost from status"


def test_omp_status_reports_nothing_for_a_session_with_neither_usage_nor_children():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        _omp_write(root, "--proj--", OMP_SID, [_omp_session(OMP_SID, cwd)])
        assert ot.OmpStore(root, _omp_args()).status_nodes(OMP_SID) == []
