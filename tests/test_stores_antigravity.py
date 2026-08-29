import os
import sqlite3
import tempfile

import opentab as ot
from opentab.stores.antigravity import pb_fields, pb_get, pb_str

from tests._support import (
    ANTIGRAVITY_SID,
    _ag_gen_metadata,
    _ag_step_meta,
    _ag_step_payload,
    _ag_subagent_step,
    _ag_tool_step,
    _ag_usage,
    _antigravity_args,
    _antigravity_db,
    _pb_bytes,
    _pb_int,
    _pb_msg,
)

CREATED = 1788006149  # 2026-08-29 14:22:29 UTC, the real session's created-at


def _store(root):
    return ot.AntigravityStore(root, _antigravity_args())


def _repo(tmp, name="repo"):
    path = os.path.join(tmp, name)
    os.makedirs(path, exist_ok=True)
    return path


# --- the wire-format reader ----------------------------------------------------------


def test_the_reader_walks_nested_fields_by_number():
    blob = _pb_msg(1, _pb_int(4, 7), _pb_bytes(19, "gemini-3.7-flash"))
    assert pb_get(blob, 1, 4) == 7
    assert pb_str(blob, 1, 19) == "gemini-3.7-flash"
    assert pb_get(blob, 1, 99) is None
    assert pb_get(blob, 42, 1) is None


def test_the_reader_stops_at_a_byte_it_cannot_read_instead_of_raising():
    # These blobs are read without a schema, so an unreadable field must cost the caller
    # the rest of that message, never the whole backend.
    good = _pb_int(1, 5)
    assert list(pb_fields(good + b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff")) == [(1, 0, 5)]
    assert list(pb_fields(b"\x0a\xff")) == []  # length runs past the end
    assert list(pb_fields(b"")) == []


def test_the_reader_rejects_a_zero_field_number():
    assert list(pb_fields(b"\x00\x01")) == []


# --- token accounting ----------------------------------------------------------------


def _one_call_db(root, repo, **usage):
    usage.setdefault("response_id", "resp-1")
    return _antigravity_db(
        root,
        ANTIGRAVITY_SID,
        workspace=repo,
        created=CREATED,
        steps=[(14, _ag_step_meta(CREATED), _ag_step_payload("you there?"))],
        gens=[_ag_gen_metadata(_ag_usage(**usage))],
    )


def test_the_system_prompt_block_is_billed_as_input():
    # It has no column of its own, and it is prompt tokens like any other.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, system=1298, fresh=16041, output=87, thinking=18)
        store = _store(root)
        store.workflows()
        row = store.model_breakdown()[0]
        assert row["input"] == 17339
        assert row["output"] == 87
        assert row["reasoning"] == 18
        assert row["tokens_total"] == 17444


def test_thinking_is_additive_to_the_text_output_not_a_slice_of_it():
    # The recorded total output (#3) equals text (#9) plus thinking (#10), so counting
    # thinking in the reasoning column is what makes the columns sum back to it.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, fresh=100, output=87, thinking=18)
        store = _store(root)
        store.workflows()
        row = store.model_breakdown()[0]
        assert (row["output"], row["reasoning"]) == (87, 18)
        assert row["tokens_total"] == 100 + 87 + 18


def test_cache_reads_are_split_out_of_input():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, fresh=500, cache_read=9000, output=20)
        store = _store(root)
        store.workflows()
        row = store.model_breakdown()[0]
        assert row["input"] == 500
        assert row["cache_read"] == 9000


def test_nothing_is_priced_so_every_token_is_left_for_the_dollar_view():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, system=1298, fresh=100, output=20, thinking=5)
        store = _store(root)
        w = store.workflows()[0]
        assert store.records_cost is False
        assert w.total_cost == 0.0
        assert w.unpriced_tokens == w.total_tokens


def test_models_are_provider_prefixed_so_the_providers_rollup_sees_a_route():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, fresh=100, output=20)
        store = _store(root)
        store.workflows()
        assert store.model_breakdown()[0]["model_name"] == "google/gemini-3.7-flash"


# --- the auxiliary call a generation table never records -----------------------------


def test_an_auxiliary_call_recorded_only_on_a_step_is_still_counted():
    # Measured on a real conversation: a routing/availability check carries its own
    # response id and usage on the STEP, and appears nowhere in gen_metadata. Reading
    # only that table drops it — real tokens, silently missing.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        main = _ag_usage("resp-main", system=1298, fresh=16041, output=87, thinking=18)
        aux = _ag_usage("resp-aux", fresh=95, output=119, thinking=3)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                (14, _ag_step_meta(CREATED), _ag_step_payload("you there?")),
                # the auxiliary call sits at a different field number than an ordinary
                # assistant step's usage, which is why the search is not a fixed path
                (23, _ag_step_meta(CREATED, _pb_msg(2, aux), at_field=28), None),
                (15, _ag_step_meta(CREATED + 1, main), None),
            ],
            gens=[_ag_gen_metadata(main)],
        )
        store = _store(root)
        w = store.workflows()[0]
        assert w.total_tokens == 17444 + 217
        assert store.model_breakdown()[0]["runs"] == 2


def test_the_same_generation_in_both_tables_is_counted_once():
    # A generation is written to gen_metadata AND to its own step; the response id is
    # what keeps reading both from doubling it.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        usage = _ag_usage("resp-1", system=1298, fresh=16041, output=87, thinking=18)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[(15, _ag_step_meta(CREATED, usage), None)],
            gens=[_ag_gen_metadata(usage)],
        )
        store = _store(root)
        assert store.workflows()[0].total_tokens == 17444
        assert store.model_breakdown()[0]["runs"] == 1


def test_a_message_reusing_those_field_numbers_is_not_read_as_usage():
    # The blobs carry no schema, so a candidate has to prove itself: a response id AND
    # the #3 == #9 + #10 identity. Without that check an unrelated sub-message would
    # invent tokens out of whatever varints it happened to carry.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        real = _ag_usage("resp-1", fresh=100, output=20, thinking=5)
        # same field numbers, arithmetic that does not close
        impostor = _ag_usage("resp-x", fresh=999, output=7, thinking=1, out_total=4242)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[(23, _ag_step_meta(CREATED, _pb_msg(2, impostor), at_field=28), None)],
            gens=[_ag_gen_metadata(real)],
        )
        store = _store(root)
        assert store.workflows()[0].total_tokens == 125
        assert store.model_breakdown()[0]["runs"] == 1


def test_a_usage_message_with_no_response_id_is_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        anonymous = b"".join([_pb_int(2, 500), _pb_int(3, 20), _pb_int(9, 20)])
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[(15, _ag_step_meta(CREATED, anonymous), None)],
            gens=[],
        )
        assert _store(root).workflows() == []


# --- model attribution ---------------------------------------------------------------


def test_an_auxiliary_call_inherits_the_conversations_only_model():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        aux = _ag_usage("resp-aux", fresh=95, output=119, thinking=3)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[(23, _ag_step_meta(CREATED, _pb_msg(2, aux), at_field=28), None)],
            gens=[_ag_gen_metadata(_ag_usage("resp-main", fresh=100, output=20))],
        )
        store = _store(root)
        store.workflows()
        rows = store.model_breakdown()
        assert [r["model_name"] for r in rows] == ["google/gemini-3.7-flash"]
        assert rows[0]["runs"] == 2


def test_a_conversation_that_switched_models_keeps_both_of_them():
    # Each generation names its own model. Collapsing a mixed conversation to one label
    # would file real, explicitly recorded usage under the wrong rates.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[],
            gens=[
                _ag_gen_metadata(_ag_usage("r1", fresh=100, output=20), model="gemini-3.7-flash"),
                _ag_gen_metadata(_ag_usage("r2", fresh=200, output=30), model="gemini-3.7-pro"),
            ],
        )
        store = _store(root)
        store.workflows()
        rows = {r["model_name"]: r["tokens_total"] for r in store.model_breakdown()}
        assert rows == {"google/gemini-3.7-flash": 120, "google/gemini-3.7-pro": 230}


def test_an_unnamed_call_in_a_mixed_conversation_is_left_unattributed():
    # Only an AUXILIARY call names no model. With one model in the file that is safe to
    # inherit; with two there is nothing to inherit, so it fails closed rather than guess.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        aux = _ag_usage("resp-aux", fresh=95, output=119, thinking=3)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[(23, _ag_step_meta(CREATED, _pb_msg(2, aux), at_field=28), None)],
            gens=[
                _ag_gen_metadata(_ag_usage("r1", fresh=100, output=20), model="gemini-3.7-flash"),
                _ag_gen_metadata(_ag_usage("r2", fresh=200, output=30), model="gemini-3.7-pro"),
            ],
        )
        store = _store(root)
        store.workflows()
        rows = {r["model_name"]: r["tokens_total"] for r in store.model_breakdown()}
        assert rows == {
            "google/gemini-3.7-flash": 120,
            "google/gemini-3.7-pro": 230,
            "unknown (not recorded)": 217,
        }


# --- session metadata ----------------------------------------------------------------


def test_the_workspace_uri_resolves_to_the_projects_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
        nested = os.path.join(repo, "packages", "api")
        os.makedirs(nested)
        _one_call_db(root, nested, fresh=100, output=20)
        assert _store(root).workflows()[0].directory == repo


def test_a_percent_encoded_workspace_uri_is_decoded():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, ".gemini")
        spaced = _repo(tmp, "my project")
        _one_call_db(root, spaced, fresh=100, output=20)
        assert _store(root).workflows()[0].directory == spaced


def test_the_first_typed_message_titles_the_conversation():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, fresh=100, output=20)
        assert _store(root).workflows()[0].title == "you there?"


def test_a_conversation_with_no_typed_message_reads_untitled():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[],
            gens=[_ag_gen_metadata(_ag_usage("r1", fresh=100, output=20))],
        )
        assert _store(root).workflows()[0].title == "(untitled)"


def test_the_created_at_comes_from_the_trajectory_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, fresh=100, output=20)
        assert _store(root).workflows()[0].created_at.startswith("2026-08-29")


def test_an_out_of_range_timestamp_is_refused_rather_than_read_as_1970():
    # These field numbers are read without a schema; an unrelated varint becoming a date
    # would silently open every range filter.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=42,  # far outside the plausible window
            steps=[],
            gens=[_ag_gen_metadata(_ag_usage("r1", fresh=100, output=20))],
        )
        assert _store(root).workflows()[0].created_at == ""


# --- turns ---------------------------------------------------------------------------


def test_turns_are_chronological_and_carry_the_prompt_in_force():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                (14, _ag_step_meta(CREATED), _ag_step_payload("first question")),
                (15, _ag_step_meta(CREATED + 1, _ag_usage("r1", fresh=100, output=20)), None),
                (14, _ag_step_meta(CREATED + 10), _ag_step_payload("second question")),
                (15, _ag_step_meta(CREATED + 11, _ag_usage("r2", fresh=200, output=30)), None),
            ],
            gens=[],
        )
        store = _store(root)
        store.workflows()
        rows = store.message_timeline(ANTIGRAVITY_SID)
        assert [r["tokens_total"] for r in rows] == [120, 230]
        assert [r["prompt_title"] for r in rows] == ["first question", "second question"]


def test_a_conversation_that_called_no_tool_keeps_the_tab_and_shows_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, fresh=100, output=20)
        store = _store(root)
        store.workflows()
        assert store.supports_turns(ANTIGRAVITY_SID) is True
        assert store.supports_tools(ANTIGRAVITY_SID) is True
        assert store.tool_breakdown(ANTIGRAVITY_SID) == []


# --- discovery, status, robustness ---------------------------------------------------


def test_both_directory_names_are_scanned():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        for variant, sid in (("antigravity", "aaaa"), ("antigravity-cli", "bbbb")):
            _antigravity_db(
                root,
                sid,
                workspace=repo,
                created=CREATED,
                steps=[],
                gens=[_ag_gen_metadata(_ag_usage(f"r-{sid}", fresh=100, output=20))],
                variant=variant,
            )
        assert sorted(w.id for w in _store(root).workflows()) == ["aaaa", "bbbb"]
        assert ot.sources._antigravity_available(root) is True


def test_detection_is_false_without_a_conversation_database():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, ".gemini")
        os.makedirs(os.path.join(root, "antigravity", "conversations"))
        assert ot.sources._antigravity_available(root) is False


def test_the_status_trio_answers_off_one_conversation_without_a_full_parse():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, fresh=100, output=20)
        _antigravity_db(
            root,
            "other-conversation",
            workspace=repo,
            created=CREATED,
            steps=[],
            gens=[_ag_gen_metadata(_ag_usage("r-other", fresh=9999, output=9999))],
        )
        warm = _store(root)
        warm.workflows()
        expected = warm.workflow_nodes(ANTIGRAVITY_SID)
        cold = _store(root)
        assert cold.root_of(ANTIGRAVITY_SID) == ANTIGRAVITY_SID
        assert cold.root_of("not-a-conversation") is None
        assert cold.status_nodes(ANTIGRAVITY_SID) == expected
        assert cold._sessions is None
        assert [r["directory"] for r in cold.recent_roots()] == [repo, repo]


def test_a_file_that_is_not_a_database_is_skipped_rather_than_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, fresh=100, output=20)
        junk = os.path.join(root, "antigravity", "conversations", "broken.db")
        with open(junk, "w", encoding="utf-8") as fh:
            fh.write("not a database at all")
        rows = _store(root).workflows()
        assert [w.id for w in rows] == [ANTIGRAVITY_SID]


def test_a_database_missing_a_table_is_skipped_rather_than_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, fresh=100, output=20)
        path = os.path.join(root, "antigravity", "conversations", "partial.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE unrelated (x integer)")
        con.commit()
        con.close()
        assert [w.id for w in _store(root).workflows()] == [ANTIGRAVITY_SID]


def test_cache_inputs_name_the_wal_sidecar_as_well_as_the_database():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        path = _one_call_db(root, repo, fresh=100, output=20)
        inputs = _store(root).cache_inputs()
        assert path in inputs and path + "-wal" in inputs


def test_demo_mode_anonymizes_the_title_and_the_project():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _one_call_db(root, repo, fresh=100, output=20)
        store = ot.AntigravityStore(root, type("Args", (), {"demo": True})())
        w = store.workflows()[0]
        assert w.title != "you there?"
        assert w.directory != repo


# --- tool calls ----------------------------------------------------------------------


def test_a_tool_call_step_rides_on_the_generation_that_asked_for_it():
    # The observed order is generation → its calls → the generation that reads the
    # results, and a tool-call step records no usage of its own.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                (14, _ag_step_meta(CREATED), _ag_step_payload("list the files")),
                (15, _ag_step_meta(CREATED + 1, _ag_usage("r1", fresh=1000, output=20)), None),
                _ag_tool_step(CREATED + 2, "list_dir"),
                (15, _ag_step_meta(CREATED + 3, _ag_usage("r2", fresh=500, output=10)), None),
            ],
            gens=[],
        )
        store = _store(root)
        store.workflows()
        assert store.supports_tools(ANTIGRAVITY_SID) is True
        rows = store.tool_breakdown(ANTIGRAVITY_SID)
        assert [(r["tool"], r["calls"], r["tokens_total"]) for r in rows] == [("list_dir", 1, 1020)]


def test_parallel_tool_calls_split_their_generations_tokens_evenly():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                (15, _ag_step_meta(CREATED, _ag_usage("r1", fresh=980, output=20)), None),
                _ag_tool_step(CREATED + 1, "list_dir", "c1"),
                _ag_tool_step(CREATED + 1, "grep_search", "c2"),
                _ag_tool_step(CREATED + 1, "run_command", "c3"),
                _ag_tool_step(CREATED + 1, "find_by_name", "c4"),
            ],
            gens=[],
        )
        store = _store(root)
        store.workflows()
        rows = store.tool_breakdown(ANTIGRAVITY_SID)
        assert sorted(r["tool"] for r in rows) == [
            "find_by_name",
            "grep_search",
            "list_dir",
            "run_command",
        ]
        assert all(r["tokens_total"] == 250 for r in rows)  # 1000 split four ways


def test_a_tool_call_before_any_generation_is_dropped_rather_than_misattributed():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                _ag_tool_step(CREATED, "list_dir"),
                (15, _ag_step_meta(CREATED + 1, _ag_usage("r1", fresh=1000, output=20)), None),
            ],
            gens=[],
        )
        store = _store(root)
        store.workflows()
        assert store.tool_breakdown(ANTIGRAVITY_SID) == []


# --- the subagent tree ---------------------------------------------------------------

CHILD_SID = "8b6dcb04-f6c0-4b0b-b66b-70124b9fc3c7"


def _tree_corpus(root, repo):
    _antigravity_db(
        root,
        ANTIGRAVITY_SID,
        workspace=repo,
        created=CREATED,
        steps=[
            (14, _ag_step_meta(CREATED), _ag_step_payload("tell me 10 jokes")),
            (15, _ag_step_meta(CREATED + 1, _ag_usage("p1", fresh=1000, output=20)), None),
            _ag_tool_step(CREATED + 2, "invoke_subagent"),
            _ag_subagent_step(CREATED + 9, CHILD_SID, ANTIGRAVITY_SID),
        ],
        gens=[],
    )
    _antigravity_db(
        root,
        CHILD_SID,
        workspace=repo,
        created=CREATED + 2,
        steps=[
            (14, _ag_step_meta(CREATED + 2), _ag_step_payload("Please generate 10 jokes")),
            (15, _ag_step_meta(CREATED + 3, _ag_usage("c1", fresh=400, output=60)), None),
        ],
        gens=[],
    )


def test_a_spawned_conversation_folds_into_the_one_that_spawned_it():
    # A subagent runs in its own database; left alone it stands as a phantom root while
    # the parent under-reports the cost of the work it delegated.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        rows = _store(root).workflows()
        assert [w.id for w in rows] == [ANTIGRAVITY_SID]
        assert rows[0].subagents == 1
        assert rows[0].total_tokens == 1020 + 460


def test_the_root_keeps_its_own_share_separate_from_the_subtree():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        store = _store(root)
        store.workflows()
        row = store.model_breakdown()[0]
        assert row["tokens_total"] == 1480  # the whole subtree
        assert row["root_unpriced_input"] == 1000  # the root's own share only


def test_the_subagent_is_a_depth_one_node_labelled_with_its_agent_name():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        store = _store(root)
        store.workflows()
        nodes = store.workflow_nodes(ANTIGRAVITY_SID)
        assert [(n["depth"], n["agent"], n["tokens_total"]) for n in nodes] == [
            (0, "-", 1020),
            (1, "Joke Writer", 460),
        ]


def test_an_unparsable_agent_header_still_leaves_the_row_its_identity():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        # rewrite the parent with a header this build does not word the same way
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                (15, _ag_step_meta(CREATED + 1, _ag_usage("p1", fresh=1000, output=20)), None),
                _ag_subagent_step(CREATED + 9, CHILD_SID, ANTIGRAVITY_SID, agent=""),
            ],
            gens=[],
        )
        store = _store(root)
        store.workflows()
        nodes = store.workflow_nodes(ANTIGRAVITY_SID)
        assert nodes[1]["agent"] == "subagent"
        assert nodes[1]["tokens_total"] == 460


def test_the_subagents_turns_interleave_into_the_parents_timeline():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        store = _store(root)
        store.workflows()
        rows = store.message_timeline(ANTIGRAVITY_SID)
        assert [(r["depth"], r["agent"]) for r in rows] == [(0, "-"), (1, "Joke Writer")]
        assert rows[0]["tools"] == ["invoke_subagent"]


def test_tool_rows_cover_the_whole_subtree():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                (15, _ag_step_meta(CREATED + 1, _ag_usage("p1", fresh=1000, output=20)), None),
                _ag_tool_step(CREATED + 2, "invoke_subagent"),
                _ag_subagent_step(CREATED + 9, CHILD_SID, ANTIGRAVITY_SID),
            ],
            gens=[],
        )
        _antigravity_db(
            root,
            CHILD_SID,
            workspace=repo,
            created=CREATED + 2,
            steps=[
                (15, _ag_step_meta(CREATED + 3, _ag_usage("c1", fresh=400, output=60)), None),
                _ag_tool_step(CREATED + 4, "send_message"),
            ],
            gens=[],
        )
        store = _store(root)
        store.workflows()
        rows = {r["tool"]: r["tokens_total"] for r in store.tool_breakdown(ANTIGRAVITY_SID)}
        assert rows == {"invoke_subagent": 1020, "send_message": 460}


def test_a_child_whose_parent_is_gone_stays_a_root_of_its_own():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            CHILD_SID,
            workspace=repo,
            created=CREATED,
            steps=[(15, _ag_step_meta(CREATED, _ag_usage("c1", fresh=400, output=60)), None)],
            gens=[],
        )
        store = _store(root)
        assert [w.id for w in store.workflows()] == [CHILD_SID]
        assert store.root_of(CHILD_SID) == CHILD_SID


def test_the_status_trio_walks_a_child_up_to_its_parent():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        warm = _store(root)
        warm.workflows()
        expected = warm.workflow_nodes(ANTIGRAVITY_SID)
        cold = _store(root)
        assert cold.root_of(CHILD_SID) == ANTIGRAVITY_SID
        assert cold.root_of(ANTIGRAVITY_SID) == ANTIGRAVITY_SID
        assert [r["id"] for r in cold.recent_roots()] == [ANTIGRAVITY_SID]
        assert cold.status_nodes(ANTIGRAVITY_SID) == expected
        assert cold._sessions is None


def _linked(root, repo, sid, child=None, tokens=100):
    steps = [(15, _ag_step_meta(CREATED, _ag_usage(f"r-{sid}", fresh=tokens, output=10)), None)]
    if child:
        steps.append(_ag_subagent_step(CREATED + 1, child, sid))
    _antigravity_db(root, sid, workspace=repo, created=CREATED, steps=steps, gens=[])


def test_a_cycle_between_conversations_still_leaves_one_root_and_loses_no_tokens():
    # Closing the loop would leave every conversation in it a child, so workflows() would
    # emit none of them and the whole cycle's spend would disappear from the browser.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _linked(root, repo, "aaaa", child="bbbb")
        _linked(root, repo, "bbbb", child="aaaa", tokens=200)
        rows = _store(root).workflows()
        assert len(rows) == 1
        assert rows[0].total_tokens == 110 + 210
        assert rows[0].subagents == 1


def test_a_longer_cycle_is_broken_too():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _linked(root, repo, "aaaa", child="bbbb")
        _linked(root, repo, "bbbb", child="cccc")
        _linked(root, repo, "cccc", child="aaaa")
        rows = _store(root).workflows()
        assert len(rows) == 1
        assert rows[0].total_tokens == 330
        assert rows[0].subagents == 2


def test_a_conversation_naming_itself_as_its_own_subagent_stays_a_plain_root():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _linked(root, repo, "aaaa", child="aaaa")
        rows = _store(root).workflows()
        assert [(w.id, w.subagents, w.total_tokens) for w in rows] == [("aaaa", 0, 110)]


def test_a_child_named_by_two_parents_is_claimed_once():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _linked(root, repo, "aaaa", child="cccc")
        _linked(root, repo, "bbbb", child="cccc")
        _linked(root, repo, "cccc")
        rows = _store(root).workflows()
        assert sorted(w.id for w in rows) == ["aaaa", "bbbb"]
        assert sum(w.subagents for w in rows) == 1
        assert sum(w.total_tokens for w in rows) == 330  # counted once, never twice


# --- the second review round ---------------------------------------------------------


def test_a_message_with_no_output_fields_is_not_read_as_a_billed_call():
    # With both output fields absent the identity reads 0 == 0 + 0 and proves nothing, so
    # any message carrying a string at #11 would be accepted as a call out of thin air.
    from opentab.stores.antigravity import AntigravityStore

    impostor = _pb_int(2, 500) + _pb_int(3, 0) + _pb_bytes(11, "ordinary-id")
    assert AntigravityStore._usage_of(impostor) is None
    # a real record carries #9 (13 of 13 in the author's corpus) and is still accepted
    assert AntigravityStore._usage_of(_ag_usage("r1", fresh=500, output=20)) is not None


def test_a_truncated_fixed_width_field_is_refused_rather_than_read_as_a_number():
    # Four stray bytes can fabricate a plausible epoch, which _timestamp would accept.
    assert list(pb_fields(_pb_int(1, 5) + b"\x0d\x01\x02")) == [(1, 0, 5)]  # fixed32, short
    assert list(pb_fields(_pb_int(1, 5) + b"\x09\x01\x02\x03")) == [(1, 0, 5)]  # fixed64


def test_the_richer_copy_of_a_repeated_response_id_wins():
    # A generation is written to its step and to gen_metadata. First-one-wins would let a
    # copy missing the system-prompt block beat the one that has it.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[(15, _ag_step_meta(CREATED, _ag_usage("r1", fresh=100, output=20)), None)],
            gens=[_ag_gen_metadata(_ag_usage("r1", system=1298, fresh=100, output=20))],
        )
        store = _store(root)
        assert store.workflows()[0].total_tokens == 1418
        assert store.model_breakdown()[0]["runs"] == 1


def test_a_tool_step_after_an_auxiliary_call_still_bills_the_real_generation():
    # An auxiliary call runs beside the conversation and asks for nothing; letting it
    # claim the following tool would hand it that call's much smaller token count.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        aux = _ag_usage("r-aux", fresh=95, output=119, thinking=3)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                (15, _ag_step_meta(CREATED, _ag_usage("r-gen", fresh=1000, output=20)), None),
                (23, _ag_step_meta(CREATED + 1, _pb_msg(2, aux), at_field=28), None),
                _ag_tool_step(CREATED + 2, "list_dir"),
            ],
            gens=[],
        )
        store = _store(root)
        store.workflows()
        rows = store.tool_breakdown(ANTIGRAVITY_SID)
        assert [(r["tool"], r["tokens_total"]) for r in rows] == [("list_dir", 1020)]


def test_a_tool_name_on_some_other_step_type_is_not_a_tool_call():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        decoy = _ag_step_meta(CREATED + 1) + _pb_msg(4, _pb_bytes(2, "not_a_tool"))
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                (15, _ag_step_meta(CREATED, _ag_usage("r1", fresh=1000, output=20)), None),
                (23, decoy, None),
            ],
            gens=[],
        )
        store = _store(root)
        store.workflows()
        assert store.tool_breakdown(ANTIGRAVITY_SID) == []


def test_a_child_id_on_some_other_step_type_does_not_fold_a_conversation():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _, decoy_payload = 0, _ag_subagent_step(CREATED, CHILD_SID, ANTIGRAVITY_SID)[2]
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                (15, _ag_step_meta(CREATED, _ag_usage("r1", fresh=100, output=20)), decoy_payload),
            ],
            gens=[],
        )
        _linked(root, repo, CHILD_SID)
        rows = _store(root).workflows()
        assert sorted(w.id for w in rows) == sorted([ANTIGRAVITY_SID, CHILD_SID])


def test_a_usage_less_parent_of_a_spending_child_stays_the_root_status_names():
    # The status path reads the link off the file, so dropping the parent here would have
    # `opentab cost` price $0 for a session the browser lists under the child's id.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _antigravity_db(
            root,
            ANTIGRAVITY_SID,
            workspace=repo,
            created=CREATED,
            steps=[
                (14, _ag_step_meta(CREATED), _ag_step_payload("delegate this")),
                _ag_subagent_step(CREATED + 1, CHILD_SID, ANTIGRAVITY_SID),
            ],
            gens=[],
        )
        _linked(root, repo, CHILD_SID, tokens=400)
        store = _store(root)
        rows = store.workflows()
        assert [w.id for w in rows] == [ANTIGRAVITY_SID]
        assert rows[0].total_tokens == 410
        assert store.root_of(CHILD_SID) == ANTIGRAVITY_SID
        assert [r["id"] for r in store.recent_roots()] == [ANTIGRAVITY_SID]
        assert _store(root).status_nodes(ANTIGRAVITY_SID) == store.workflow_nodes(ANTIGRAVITY_SID)


def test_the_status_path_breaks_a_cycle_the_same_way_the_parser_does():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _linked(root, repo, "aaaa", child="bbbb")
        _linked(root, repo, "bbbb", child="aaaa")
        store = _store(root)
        root_id = store.workflows()[0].id
        assert store.root_of("aaaa") == root_id
        assert store.root_of("bbbb") == root_id
        assert [r["id"] for r in store.recent_roots()] == [root_id]


def test_a_stray_database_neither_advertises_the_source_nor_becomes_a_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, ".gemini")
        directory = os.path.join(root, "antigravity", "conversations")
        os.makedirs(directory)
        with open(os.path.join(directory, "junk.db"), "w", encoding="utf-8") as fh:
            fh.write("not a database at all")
        assert ot.sources._antigravity_available(root) is False
        assert _store(root).recent_roots() == []


def test_activity_that_lands_only_in_the_wal_sidecar_still_reads_as_recent():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        path = _one_call_db(root, repo, fresh=100, output=20)
        os.utime(path, (1, 1))  # the main file looks ancient
        with open(path + "-wal", "w", encoding="utf-8") as fh:
            fh.write("")
        store = _store(root)
        assert store.recent_roots()[0]["last_active"] > 1000
