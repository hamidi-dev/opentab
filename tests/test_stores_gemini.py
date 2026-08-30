import hashlib
import json
import os
import tempfile

import opentab as ot

from tests._support import (
    GEMINI_SID,
    _gemini_args,
    _gemini_meta,
    _gemini_registry,
    _gemini_turn,
    _gemini_user,
    _gemini_write,
    _gemini_write_subagent,
)

KID = "aaaa1111-bbbb-4ccc-8ddd-222222222222"
KID2 = "aaaa2222-bbbb-4ccc-8ddd-333333333333"


def _store(root):
    return ot.GeminiStore(root, _gemini_args())


def _repo(tmp, name="repo"):
    path = os.path.join(tmp, name)
    os.makedirs(path, exist_ok=True)
    return path


# --- token accounting ---------------------------------------------------------------


def test_prompt_tokens_are_split_into_uncached_input_and_cache_read():
    # promptTokenCount includes the cache read; counting both would bill it twice.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_turn("gemini-2.5-pro", 12000, 800, cached=9000),
            ],
            project=repo,
        )
        store = _store(root)
        store.workflows()
        row = store.model_breakdown()[0]
        assert row["input"] == 3000
        assert row["cache_read"] == 9000
        assert row["output"] == 800
        assert row["tokens_total"] == 12800  # 12000 prompt + 800 output, counted once


def test_thinking_tokens_are_additive_and_land_in_the_reasoning_column():
    # thoughtsTokenCount sits OUTSIDE candidatesTokenCount (unlike OpenAI's reasoning
    # detail), and bills at the output rate -- so it is counted, never folded or dropped.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_turn("gemini-2.5-pro", 1000, 200, thoughts=500),
            ],
            project=repo,
        )
        store = _store(root)
        store.workflows()
        row = store.model_breakdown()[0]
        assert row["output"] == 200
        assert row["reasoning"] == 500
        assert row["tokens_total"] == 1700


def test_tool_prompt_tokens_ride_with_input_rather_than_being_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 900, 100, tool=300)],
            project=repo,
        )
        store = _store(root)
        store.workflows()
        row = store.model_breakdown()[0]
        assert row["input"] == 1200  # 900 prompt + 300 tool-use prompt
        assert row["tokens_total"] == 1300


def test_a_total_that_excludes_the_cache_read_keeps_the_recorded_input():
    # The subtraction is driven by which identity the recorded total closes, not by a
    # blanket rule -- a response reporting an exclusive prompt count must stay verbatim.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                # total counts the cache read on top of the prompt: 1000+200+400
                _gemini_turn("gemini-2.5-pro", 1000, 200, cached=400, total=1600),
            ],
            project=repo,
        )
        store = _store(root)
        store.workflows()
        row = store.model_breakdown()[0]
        assert row["input"] == 1000
        assert row["cache_read"] == 400


def test_a_response_with_no_total_still_splits_the_cache_read_out_of_input():
    # The inclusive prompt count is Gemini's documented shape, so it is the DEFAULT.
    # Keeping `input` whole here would bill those 400 tokens twice — once as input and
    # again as a cache read — inflating both the token column and the `$` estimate.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        rec = _gemini_turn("gemini-2.5-pro", 1000, 200, cached=400)
        del rec["tokens"]["total"]
        _gemini_write(root, "repo", [_gemini_meta(GEMINI_SID, repo), rec], project=repo)
        store = _store(root)
        store.workflows()
        row = store.model_breakdown()[0]
        assert row["input"] == 600
        assert row["cache_read"] == 400
        assert row["tokens_total"] == 1200


def test_a_streamed_message_reappended_under_one_id_is_counted_once():
    # The tokens arrive on a later append of the SAME id, so the id is an update key.
    # Treating it as a duplicate would drop the usage; appending twice would double it.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        first = _gemini_turn("gemini-2.5-pro", 0, 0, mid="m1", tools=["write_file"])
        del first["tokens"]
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                first,
                _gemini_turn("gemini-2.5-pro", 1000, 200, mid="m1", tools=["write_file"]),
            ],
            project=repo,
        )
        store = _store(root)
        w = store.workflows()[0]
        assert w.total_tokens == 1200
        assert store.model_breakdown()[0]["runs"] == 1
        assert len(store.message_timeline(GEMINI_SID)) == 1


def test_a_superseded_turn_is_unwound_from_the_model_it_was_recorded_under():
    # A re-append may name a different model; backing the old row out against its own
    # model is what keeps the per-model totals equal to the turns on record.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_turn("gemini-2.5-flash", 500, 100, mid="m1"),
                _gemini_turn("gemini-2.5-pro", 1000, 200, mid="m1"),
            ],
            project=repo,
        )
        store = _store(root)
        store.workflows()
        rows = store.model_breakdown()
        assert [r["model_name"] for r in rows] == ["google/gemini-2.5-pro"]
        assert rows[0]["tokens_total"] == 1200


def test_models_are_provider_prefixed_so_the_providers_rollup_sees_a_route():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 100, 20)],
            project=repo,
        )
        store = _store(root)
        store.workflows()
        assert store.model_breakdown()[0]["model_name"] == "google/gemini-2.5-pro"


def test_every_token_is_unpriced_so_the_dollar_view_can_estimate_it():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_turn("gemini-2.5-pro", 1000, 200, cached=400, thoughts=50),
            ],
            project=repo,
        )
        store = _store(root)
        w = store.workflows()[0]
        assert store.records_cost is False
        assert w.total_cost == 0.0
        assert w.unpriced_tokens == w.total_tokens


# --- the rewind record --------------------------------------------------------------


def test_a_rewound_turn_still_counts_because_the_call_was_billed():
    # $rewindTo drops those turns from Gemini's own resumed history, but the API calls
    # happened. Hiding them would under-report exactly the sessions someone edited
    # their way through.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_turn("gemini-2.5-pro", 1000, 100, mid="m1"),
                _gemini_turn("gemini-2.5-pro", 2000, 300, mid="m2"),
                {"$rewindTo": "m2"},
                _gemini_turn("gemini-2.5-pro", 2400, 400, mid="m3"),
            ],
            project=repo,
        )
        w = _store(root).workflows()[0]
        assert w.total_tokens == 1100 + 2300 + 2800


def test_a_metadata_patch_supplies_the_session_title():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_user("fix the footer"),
                _gemini_turn("gemini-2.5-pro", 100, 20),
                {"$set": {"summary": "Fix footer spacing with flexbox"}},
            ],
            project=repo,
        )
        assert _store(root).workflows()[0].title == "Fix footer spacing with flexbox"


def test_the_first_prompt_titles_a_session_with_no_summary():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_user("convert this script to python"),
                _gemini_turn("gemini-2.5-pro", 100, 20),
            ],
            project=repo,
        )
        assert _store(root).workflows()[0].title == "convert this script to python"


# --- project attribution ------------------------------------------------------------


def test_the_project_root_marker_names_the_directory():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
        _gemini_write(
            root,
            "some-slug",
            [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 100, 20)],
            project=repo,
        )
        assert _store(root).workflows()[0].directory == repo


def test_the_registry_names_a_directory_when_no_marker_was_written():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_registry(root, {repo: "my-slug"})
        _gemini_write(
            root,
            "my-slug",
            [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 100, 20)],
        )
        assert _store(root).workflows()[0].directory == repo


def test_a_legacy_hash_named_directory_resolves_through_the_recorded_project_hash():
    # Before the short-id registry the directory WAS sha256(project). The hash is
    # one-way, but the registry's own paths are the candidate set to match it against.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()
        _gemini_registry(root, {repo: "unrelated-slug"})
        _gemini_write(
            root,
            digest,
            [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 100, 20)],
        )
        assert _store(root).workflows()[0].directory == repo


def test_an_unresolvable_project_reads_unknown_rather_than_a_slug():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, ".gemini")
        _gemini_write(
            root,
            "orphan-slug",
            [
                _gemini_meta(GEMINI_SID, os.path.join(tmp, "gone")),
                _gemini_turn("gemini-2.5-pro", 100, 20),
            ],
        )
        assert _store(root).workflows()[0].directory == "(unknown)"


def test_a_workspace_folds_to_its_git_root():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
        nested = os.path.join(repo, "packages", "api")
        os.makedirs(nested)
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, nested), _gemini_turn("gemini-2.5-pro", 100, 20)],
            project=nested,
        )
        assert _store(root).workflows()[0].directory == repo


# --- the subagent tree --------------------------------------------------------------


def _tree_corpus(root, repo):
    _gemini_write(
        root,
        "repo",
        [
            _gemini_meta(GEMINI_SID, repo, summary="Refactor the auth middleware"),
            _gemini_user("refactor auth", ts="2026-08-20T09:15:01.000Z"),
            _gemini_turn(
                "gemini-2.5-pro", 1000, 200, mid="m1", ts="2026-08-20T09:15:09.000Z", tools=["grep"]
            ),
        ],
        project=repo,
    )
    _gemini_write_subagent(
        root,
        "repo",
        GEMINI_SID,
        KID,
        [
            _gemini_meta(KID, repo, kind="subagent", summary="search the codebase"),
            _gemini_turn(
                "gemini-2.5-flash",
                400,
                60,
                mid="k1",
                ts="2026-08-20T09:15:30.000Z",
                tools=["glob"],
            ),
        ],
    )


def test_a_nested_subagent_transcript_folds_into_its_parents_totals():
    # The child is a whole transcript one directory deeper. A parser that only accepts
    # the flat chats/<file> layout silently drops every subagent's tokens.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        rows = _store(root).workflows()
        assert len(rows) == 1  # the child never gets a row of its own
        w = rows[0]
        assert w.subagents == 1
        assert w.total_tokens == 1200 + 460
        assert w.id == GEMINI_SID


def test_the_root_keeps_its_own_share_separate_from_the_subtree_total():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        store = _store(root)
        store.workflows()
        rows = {r["model_name"]: r for r in store.model_breakdown()}
        assert rows["google/gemini-2.5-pro"]["root_unpriced_input"] == 1000
        # the child's model contributes to the subtree but nothing to the root's share
        assert rows["google/gemini-2.5-flash"]["unpriced_input"] == 400
        assert rows["google/gemini-2.5-flash"]["root_unpriced_input"] == 0


def test_the_subagent_becomes_a_depth_one_node_under_the_root():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        store = _store(root)
        store.workflows()
        nodes = store.workflow_nodes(GEMINI_SID)
        assert [n["depth"] for n in nodes] == [0, 1]
        assert nodes[1]["id"] == KID
        assert nodes[1]["agent"] == "subagent"
        assert nodes[1]["tokens_total"] == 460
        assert nodes[0]["tokens_total"] == 1200  # the root's OWN usage, not the subtree


def test_a_subagents_turns_interleave_into_the_parents_timeline_by_time():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        store = _store(root)
        store.workflows()
        rows = store.message_timeline(GEMINI_SID)
        assert [r["depth"] for r in rows] == [0, 1]
        assert rows[1]["agent"] == "subagent"
        # both turns belong to the prompt the human typed before either ran
        assert all(r["prompt_title"] == "refactor auth" for r in rows)


def test_tool_rows_cover_the_whole_subtree():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        store = _store(root)
        store.workflows()
        rows = {r["tool"]: r for r in store.tool_breakdown(GEMINI_SID)}
        assert rows["grep"]["tokens_total"] == 1200
        assert rows["glob"]["tokens_total"] == 460


def test_a_usage_less_parent_does_not_orphan_a_child_that_did_spend():
    # Launching gemini and delegating immediately leaves the root with no usage of its
    # own; the child must still be reachable rather than vanishing with its parent.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, repo), _gemini_user("delegate this")],
            project=repo,
        )
        _gemini_write_subagent(
            root,
            "repo",
            GEMINI_SID,
            KID,
            [
                _gemini_meta(KID, repo, kind="subagent"),
                _gemini_turn("gemini-2.5-flash", 400, 60, mid="k1"),
            ],
        )
        rows = _store(root).workflows()
        assert len(rows) == 1
        assert rows[0].total_tokens == 460


# --- discovery, dropping, and the legacy shape --------------------------------------


def test_a_session_with_no_recorded_usage_is_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, repo), _gemini_user("hello")],
            project=repo,
        )
        assert _store(root).workflows() == []


def test_the_legacy_single_document_json_chat_is_read():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        d = os.path.join(root, "tmp", "repo", "chats")
        os.makedirs(d)
        with open(os.path.join(root, "tmp", "repo", ".project_root"), "w") as fh:
            fh.write(repo)
        doc = dict(_gemini_meta(GEMINI_SID, repo))
        doc["messages"] = [
            _gemini_user("convert this script"),
            _gemini_turn("gemini-2.5-flash", 2200, 700),
        ]
        with open(os.path.join(d, f"{GEMINI_SID}.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        w = _store(root).workflows()[0]
        assert w.total_tokens == 2900
        assert w.directory == repo


def test_a_preserved_unreadable_backup_is_not_counted_a_second_time():
    # A rewrite renames the old file to <name>.unreadable-<ms> and writes the new one
    # beside it; reading both would double every token in that session.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        rows = [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 1000, 200)]
        _gemini_write(root, "repo", rows, project=repo)
        _gemini_write(root, "repo", rows, name="session-x.jsonl.unreadable-1755000000000")
        assert _store(root).workflows()[0].total_tokens == 1200


def test_a_torn_final_line_does_not_lose_the_records_before_it():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 1000, 200)],
            project=repo,
        )
        path = os.path.join(root, "tmp", "repo", "chats", "session-2026-08-20T09-15-3f9a1c22.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"id": "m2", "type": "gem')
        assert _store(root).workflows()[0].total_tokens == 1200


def test_the_source_is_detected_only_when_a_chat_transcript_exists():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, ".gemini")
        os.makedirs(os.path.join(root, "tmp", "repo", "chats"))
        assert ot.sources._gemini_available(root) is False
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, _repo(tmp)), _gemini_turn("gemini-2.5-pro", 100, 20)],
        )
        assert ot.sources._gemini_available(root) is True


def test_a_subagent_only_tree_is_still_detected():
    # The nested layout is a directory deeper than the flat glob reaches.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, ".gemini")
        _gemini_write_subagent(
            root,
            "repo",
            GEMINI_SID,
            KID,
            [
                _gemini_meta(KID, _repo(tmp), kind="subagent"),
                _gemini_turn("gemini-2.5-flash", 400, 60),
            ],
        )
        assert ot.sources._gemini_available(root) is True


# --- the --status one-shot ----------------------------------------------------------


def test_recent_roots_keys_a_subagent_file_to_its_parent():
    # A root whose child is mid-burst must read as active, not idle.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        rows = _store(root).recent_roots()
        assert [r["id"] for r in rows] == [GEMINI_SID]
        assert rows[0]["directory"] == repo


def test_root_of_walks_a_subagent_id_up_to_its_parent():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        store = _store(root)
        assert store.root_of(GEMINI_SID) == GEMINI_SID
        assert store.root_of(KID) == GEMINI_SID
        assert store.root_of("not-a-session") is None


def test_status_nodes_match_the_full_parse_without_reading_the_corpus():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        # an unrelated session that a status poll must not have to parse
        _gemini_write(
            root,
            "other",
            [
                _gemini_meta("other-session-id", repo),
                _gemini_turn("gemini-2.5-pro", 9999, 9999, mid="o1"),
            ],
            project=repo,
        )
        warm = _store(root)
        warm.workflows()
        expected = warm.workflow_nodes(GEMINI_SID)
        cold = _store(root)
        assert cold.status_nodes(GEMINI_SID) == expected
        assert cold._sessions is None  # never triggered the full parse


# --- cache inputs and reload --------------------------------------------------------


def test_cache_inputs_name_the_transcripts_and_the_project_registry():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _tree_corpus(root, repo)
        _gemini_registry(root, {repo: "repo"})
        inputs = _store(root).cache_inputs()
        assert os.path.join(root, "projects.json") in inputs
        assert sum(1 for p in inputs if p.endswith(".jsonl")) == 2


def test_reload_picks_up_a_project_renamed_in_the_registry():
    # workflows() is what `r` calls; the registry is re-read there because a rename
    # changes a session's project without touching a single transcript byte.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        moved = _repo(tmp, "moved")
        _gemini_registry(root, {repo: "slug"})
        _gemini_write(
            root,
            "slug",
            [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 100, 20)],
        )
        store = _store(root)
        assert store.workflows()[0].directory == repo
        _gemini_registry(root, {moved: "slug"})
        assert store.workflows()[0].directory == moved


def test_demo_mode_anonymizes_titles_and_scales_magnitudes():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo, summary="Refactor the auth middleware"),
                _gemini_turn("gemini-2.5-pro", 1000, 200),
            ],
            project=repo,
        )
        store = ot.GeminiStore(root, type("Args", (), {"demo": True})())
        w = store.workflows()[0]
        assert w.title != "Refactor the auth middleware"
        assert w.directory != repo


def test_a_nested_transcript_is_keyed_by_its_filename_not_its_metadata():
    # The filename is what root_of resolves and _session_files globs, so a child whose
    # metadata names some other session must not be filed under that id -- and a child
    # naming its own PARENT would otherwise merge the two and double the root's tokens.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 1000, 200, mid="m1")],
            project=repo,
        )
        _gemini_write_subagent(
            root,
            "repo",
            GEMINI_SID,
            KID2,
            [
                _gemini_meta(GEMINI_SID, repo, kind="subagent"),  # names the parent
                _gemini_turn("gemini-2.5-flash", 400, 60, mid="k1"),
            ],
        )
        store = _store(root)
        rows = store.workflows()
        assert len(rows) == 1
        assert rows[0].subagents == 1
        assert rows[0].total_tokens == 1200 + 460
        assert [n["id"] for n in store.workflow_nodes(GEMINI_SID)] == [GEMINI_SID, KID2]
        assert store.root_of(KID2) == GEMINI_SID


# --- fixes found against the real gemini-cli corpus and by adversarial review --------


def test_a_checkpoint_set_messages_record_is_ingested_not_dropped():
    # Real transcripts open with {"$set": {"messages": [...]}}. Gemini's own loader
    # rebuilds its map from that array; dropping it loses whatever only appears there.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                {
                    "$set": {
                        "lastUpdated": "2026-08-20T09:15:00.000Z",
                        "messages": [
                            _gemini_user("resumed prompt", mid="u9"),
                            _gemini_turn("gemini-2.5-pro", 1000, 200, mid="m9"),
                        ],
                    }
                },
            ],
            project=repo,
        )
        w = _store(root).workflows()[0]
        assert w.total_tokens == 1200
        assert w.title == "resumed prompt"


def test_a_checkpoint_does_not_double_count_a_turn_it_replays():
    # Merging (rather than clearing, as Gemini's loader does) is only safe because the
    # replay re-ingests by id — the same turn must update in place, not accumulate.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        turn = _gemini_turn("gemini-2.5-pro", 1000, 200, mid="m1")
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                turn,
                {"$set": {"messages": [turn]}},
            ],
            project=repo,
        )
        store = _store(root)
        assert store.workflows()[0].total_tokens == 1200
        assert store.model_breakdown()[0]["runs"] == 1


def test_the_injected_session_context_never_becomes_the_title_or_a_prompt():
    # Every real session opens with a <session_context> block recorded as a `user`
    # message. Gemini's own isIgnoredUserContent skips it; so must we, or it becomes the
    # session title and the first ▸ header in the Turns tab.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_user("<session_context>\nThis is the Gemini CLI.\n</session_context>"),
                _gemini_user("/clear", mid="u2"),
                _gemini_user("hi there? what is this project about", mid="u3"),
                _gemini_turn("gemini-2.5-pro", 1000, 200),
            ],
            project=repo,
        )
        store = _store(root)
        w = store.workflows()[0]
        assert w.title == "hi there? what is this project about"
        rows = store.message_timeline(GEMINI_SID)
        assert [r["prompt_title"] for r in rows] == ["hi there? what is this project about"]


def test_a_tool_result_user_record_is_not_treated_as_a_human_prompt():
    # A tool result comes back as type "user" whose content is a functionResponse part.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        result = _gemini_user("", mid="u2")
        result["content"] = [
            {"functionResponse": {"id": "c1", "name": "read_file", "response": {"output": "..."}}}
        ]
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_user("read the file", mid="u1"),
                _gemini_turn("gemini-2.5-pro", 1000, 200, mid="m1", tools=["read_file"]),
                result,
                _gemini_turn("gemini-2.5-pro", 1100, 100, mid="m2"),
            ],
            project=repo,
        )
        store = _store(root)
        store.workflows()
        rows = store.message_timeline(GEMINI_SID)
        assert [r["prompt_title"] for r in rows] == ["read the file", "read the file"]


def test_a_repeated_user_id_replaces_rather_than_opening_a_second_group():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_user("do the thing", mid="u1"),
                _gemini_user("do the thing", mid="u1"),
                _gemini_turn("gemini-2.5-pro", 1000, 200),
            ],
            project=repo,
        )
        store = _store(root)
        store.workflows()
        assert len(store._parse()[GEMINI_SID]["prompts"]) == 1


def test_a_zero_token_reappend_unwinds_the_turn_it_supersedes():
    # The map is keyed by id and the last record wins, so an aborted retry that lands
    # with no usage must take the earlier attribution with it.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        empty = _gemini_turn("gemini-2.5-pro", 0, 0, mid="m1")
        del empty["tokens"]
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_turn("gemini-2.5-pro", 1000, 200, mid="m1"),
                _gemini_turn("gemini-2.5-pro", 500, 50, mid="m2"),
                empty,
            ],
            project=repo,
        )
        store = _store(root)
        w = store.workflows()[0]
        assert w.total_tokens == 550  # only m2 survives
        rows = store.message_timeline(GEMINI_SID)
        assert len(rows) == 1
        assert rows[0]["tokens_total"] == 550


def test_a_malformed_toolcalls_value_does_not_take_the_backend_down():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        rec = _gemini_turn("gemini-2.5-pro", 1000, 200)
        rec["toolCalls"] = True
        _gemini_write(root, "repo", [_gemini_meta(GEMINI_SID, repo), rec], project=repo)
        store = _store(root)
        assert store.workflows()[0].total_tokens == 1200  # usage kept, tools discarded
        assert store.tool_breakdown(GEMINI_SID) == []


# --- multi-level nesting (a subagent that spawns its own) ----------------------------


def _grandchild_corpus(root, repo):
    _gemini_write(
        root,
        "repo",
        [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 100, 10, mid="m1")],
        project=repo,
    )
    _gemini_write_subagent(
        root,
        "repo",
        GEMINI_SID,
        KID,
        [
            _gemini_meta(KID, repo, kind="subagent"),
            _gemini_turn("gemini-2.5-flash", 100, 10, mid="k1"),
        ],
    )
    # the grandchild lives under ITS OWN parent's directory, not the root's
    _gemini_write_subagent(
        root,
        "repo",
        KID,
        KID2,
        [
            _gemini_meta(KID2, repo, kind="subagent"),
            _gemini_turn("gemini-2.5-flash", 100, 10, mid="g1"),
        ],
    )


def test_a_grandchild_subagent_folds_all_the_way_up_to_the_root():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _grandchild_corpus(root, repo)
        rows = _store(root).workflows()
        assert len(rows) == 1
        assert rows[0].subagents == 2
        assert rows[0].total_tokens == 330


def test_the_status_trio_agrees_with_the_full_parse_across_two_levels():
    # `opentab cost` must price the whole tree, not the branch below the nearest parent.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _grandchild_corpus(root, repo)
        warm = _store(root)
        warm.workflows()
        expected = warm.workflow_nodes(GEMINI_SID)
        cold = _store(root)
        assert cold.root_of(KID2) == GEMINI_SID
        assert cold.root_of(KID) == GEMINI_SID
        assert [r["id"] for r in cold.recent_roots()] == [GEMINI_SID]
        nodes = cold.status_nodes(GEMINI_SID)
        assert sum(n["tokens_total"] for n in nodes) == 330
        assert nodes == expected


def test_a_usage_less_root_is_kept_so_the_browser_and_cost_name_one_session():
    # A session that only delegated has no tokens of its own. Dropping it would promote
    # the child to root in the browser while recent_roots/root_of keep naming the parent.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, repo), _gemini_user("delegate this")],
            project=repo,
        )
        _gemini_write_subagent(
            root,
            "repo",
            GEMINI_SID,
            KID,
            [
                _gemini_meta(KID, repo, kind="subagent"),
                _gemini_turn("gemini-2.5-flash", 400, 60, mid="k1"),
            ],
        )
        store = _store(root)
        rows = store.workflows()
        assert [w.id for w in rows] == [GEMINI_SID]
        assert rows[0].total_tokens == 460
        assert rows[0].root_cost == 0.0
        assert store.root_of(KID) == GEMINI_SID
        assert [r["id"] for r in store.recent_roots()] == [GEMINI_SID]


def test_availability_ignores_a_tree_holding_only_unreadable_backups():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, ".gemini")
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, _repo(tmp)), _gemini_turn("gemini-2.5-pro", 100, 20)],
            name="session-x.jsonl.unreadable-1755000000000",
        )
        assert ot.sources._gemini_available(root) is False


def test_a_usage_less_session_in_the_MIDDLE_of_a_chain_keeps_it_intact():
    # A pure delegator between two spending sessions. Dropping it would sever the
    # grandchild from the root; keeping it shows an honest zero-token node.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 100, 10, mid="m1")],
            project=repo,
        )
        _gemini_write_subagent(
            root, "repo", GEMINI_SID, KID, [_gemini_meta(KID, repo, kind="subagent")]
        )
        _gemini_write_subagent(
            root,
            "repo",
            KID,
            KID2,
            [
                _gemini_meta(KID2, repo, kind="subagent"),
                _gemini_turn("gemini-2.5-flash", 200, 20, mid="g1"),
            ],
        )
        store = _store(root)
        rows = store.workflows()
        assert [(w.id, w.total_tokens, w.subagents) for w in rows] == [(GEMINI_SID, 330, 2)]
        nodes = store.workflow_nodes(GEMINI_SID)
        assert [(n["id"], n["depth"], n["tokens_total"]) for n in nodes] == [
            (GEMINI_SID, 0, 110),
            (KID, 1, 0),
            (KID2, 2, 220),
        ]
        assert store.root_of(KID2) == GEMINI_SID
        assert _store(root).status_nodes(GEMINI_SID) == nodes


# --- round-two fixes ----------------------------------------------------------------


def test_a_subagents_summary_is_a_result_payload_not_a_title():
    # Gemini's own summariser skips kind == "subagent" outright, so anything in that
    # field on a child came from elsewhere. Measured on a real subagent: it holds the
    # complete_task RESULT, which titled the Subagents row with a JSON blob.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [_gemini_meta(GEMINI_SID, repo), _gemini_turn("gemini-2.5-pro", 100, 10, mid="m1")],
            project=repo,
        )
        _gemini_write_subagent(
            root,
            "repo",
            GEMINI_SID,
            KID,
            [
                _gemini_meta(KID, repo, kind="subagent"),
                _gemini_user("Please tell a short, clean joke.", mid="k0"),
                _gemini_turn("gemini-2.5-flash", 200, 20, mid="k1"),
                {"$set": {"summary": '{\n  "response": "Why do programmers wear glasses?"\n}'}},
            ],
        )
        store = _store(root)
        store.workflows()
        child = store.workflow_nodes(GEMINI_SID)[1]
        assert child["title"] == "Please tell a short, clean joke."


def test_a_main_sessions_summary_is_still_its_title():
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_user("do a thing"),
                _gemini_turn("gemini-2.5-pro", 100, 10),
                {"$set": {"summary": "Refactor the auth middleware"}},
            ],
            project=repo,
        )
        assert _store(root).workflows()[0].title == "Refactor the auth middleware"


def test_a_vanished_parent_does_not_become_the_root_the_status_path_reports():
    # The parent transcript was deleted or rotated; _parse promotes the orphan to a root
    # of its own, so root_of/recent_roots must agree or `opentab cost` prices an id
    # nothing can resolve — $0 for a session the browser lists with real usage.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write_subagent(
            root,
            "repo",
            "parent-that-is-gone",
            KID,
            [
                _gemini_meta(KID, repo, kind="subagent"),
                _gemini_turn("gemini-2.5-flash", 400, 60, mid="k1"),
            ],
        )
        store = _store(root)
        rows = store.workflows()
        assert [w.id for w in rows] == [KID]
        assert store.root_of(KID) == KID
        assert [r["id"] for r in store.recent_roots()] == [KID]
        assert _store(root).status_nodes(KID) == store.workflow_nodes(KID)


def test_a_cycle_on_disk_still_leaves_one_root_and_loses_no_tokens():
    # Not spellable by Gemini, but spellable on disk. Every member being a child would
    # make workflows() emit none of them and the tokens vanish from the browser.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        a, b = "cycle-aaa", "cycle-bbb"
        _gemini_write_subagent(
            root,
            "repo",
            b,
            a,
            [
                _gemini_meta(a, repo, kind="subagent"),
                _gemini_turn("gemini-2.5-flash", 100, 10, mid="a1"),
            ],
        )
        _gemini_write_subagent(
            root,
            "repo",
            a,
            b,
            [
                _gemini_meta(b, repo, kind="subagent"),
                _gemini_turn("gemini-2.5-flash", 200, 20, mid="b1"),
            ],
        )
        rows = _store(root).workflows()
        assert len(rows) == 1
        assert rows[0].total_tokens == 330  # both sessions still counted


def test_a_checkpoint_that_edits_the_opening_prompt_retitles_the_session():
    # The title is derived from the prompt list at finalize, not latched at first sight.
    with tempfile.TemporaryDirectory() as tmp:
        root, repo = os.path.join(tmp, ".gemini"), _repo(tmp)
        _gemini_write(
            root,
            "repo",
            [
                _gemini_meta(GEMINI_SID, repo),
                _gemini_user("old title", mid="u1"),
                _gemini_turn("gemini-2.5-pro", 100, 10),
                {"$set": {"messages": [_gemini_user("new title", mid="u1")]}},
            ],
            project=repo,
        )
        store = _store(root)
        assert store.workflows()[0].title == "new title"
        assert [r["prompt_title"] for r in store.message_timeline(GEMINI_SID)] == ["new title"]


def SR(retention):
    return {"general": {"sessionRetention": retention}}


def test_gemini_retention_borrows_the_cli_defaults_and_its_own_validation():
    old = os.environ.get("GEMINI_CLI_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["GEMINI_CLI_HOME"] = tmp
        os.makedirs(os.path.join(tmp, ".gemini"))
        path = os.path.join(tmp, ".gemini", "settings.json")
        try:
            assert ot.gemini_settings_path() == path

            def write(obj):
                with open(path, "w", encoding="utf-8") as fh:
                    if isinstance(obj, str):
                        fh.write(obj)
                    else:
                        json.dump(obj, fh)

            def retention(obj=None):
                if obj is not None:
                    write(obj)
                return ot.gemini_retention()

            # getDefaultsFromSchema recurses into `properties`, so cleanup is on at 30d
            # even when nothing in the file mentions it -- and when the file is missing.
            missing = ot.gemini_retention()
            assert (missing.max_age, missing.source) == ("30d", "default")
            assert missing.deletes and missing.needs_warning
            for silent in ({}, {"general": {}}, {"general": {"other": 1}}):
                assert retention(silent).source == "default"

            # A non-object REPLACES the default object, and `!undefined?.enabled` then
            # disables cleanup -- so an explicit null keeps history rather than meaning
            # "unset".
            assert retention({"general": {"sessionRetention": None}}).source == "off"

            # A file Gemini cannot parse is FATAL to it (FatalConfigError), so the
            # policy is not "the default" -- it is unknown until the file is fixed.
            broken = retention("{ not json")
            assert broken.source == "unverifiable" and broken.needs_warning

            # But Gemini strips JSON comments before parsing, so a commented file is
            # perfectly valid to it and must not read as broken.
            jsonc = retention(
                '{ // keep my history\n "general": {"sessionRetention": {"enabled": false}} }'
            )
            assert jsonc.source == "off" and not jsonc.needs_warning

            off = retention({"general": {"sessionRetention": {"enabled": False}}})
            assert off.source == "off" and not off.deletes and not off.needs_warning

            short = retention({"general": {"sessionRetention": {"maxAge": "2w"}}})
            assert short.max_age_days == 14 and short.source == "configured"
            assert short.needs_warning

            safe = retention({"general": {"sessionRetention": {"maxAge": "3650d"}}})
            assert not safe.needs_warning and safe.deletes

            # A count cap deletes the oldest sessions whatever the age allows.
            capped = retention(
                {"general": {"sessionRetention": {"maxAge": "3650d", "maxCount": 50}}}
            )
            assert capped.max_count == 50 and capped.needs_warning

            # validateRetentionConfig rejects these, which disables cleanup wholesale --
            # history survives, so there is nothing to warn about.
            for inert in (
                {"maxAge": "nonsense"},  # a plain typo: Gemini rejects it too
                {"maxAge": "0d"},
                {"maxAge": "12h"},  # below the 1d minRetention floor
                {"maxAge": None, "maxCount": 0},
                {"maxAge": None},
            ):
                got = retention({"general": {"sessionRetention": inert}})
                assert got.source == "inert" and not got.deletes and not got.needs_warning

            # A raised floor is honoured the same way.
            floored = retention(
                {"general": {"sessionRetention": {"maxAge": "2d", "minRetention": "7d"}}}
            )
            assert floored.source == "inert" and not floored.needs_warning

            # Gemini's schema PREPROCESSES strings before validating, so these are real
            # policies rather than rejected ones -- reading them as Python types would
            # report history as safe while Gemini deletes it.
            assert retention(SR({"maxAge": "3650d", "maxCount": "50"})).max_count == 50
            assert retention(SR({"maxCount": 1.5})).needs_warning  # a number, and >= 1
            for spelling in ("false", "FALSE"):
                assert retention(SR({"enabled": spelling})).source == "off"

            # A value the schema REJECTS is only a warning to Gemini: it keeps the raw
            # value, and the gate then reads it for JAVASCRIPT truthiness, where [] and
            # {} are true. Python's bool() says the opposite of both.
            for truthy in ([], {}, "yes"):
                assert retention(SR({"enabled": truthy})).needs_warning
            for falsy in (0, None, ""):
                assert retention(SR({"enabled": falsy})).source == "off"

            # Anything this cannot pin down exactly fails OPEN. `$` is the one thing
            # Gemini rewrites before parsing, so it is the one string a typo check
            # cannot dismiss -- ${KEEP:-30d} is a live 30-day policy there.
            assert retention(SR({"maxAge": "${KEEP:-30d}"})).needs_warning
            assert retention(SR({"maxCount": "abc"})).needs_warning
            # ...and a safe-looking maxAge must not silence an unevaluable maxCount.
            unsure = retention(SR({"maxAge": "3650d", "maxCount": "abc"}))
            assert unsure.source == "unknown" and unsure.needs_warning

            # But only what Gemini's OWN regex would rewrite is unevaluable: a trailing
            # "$" is not a substitution, and a non-string maxAge throws in its parser.
            assert retention(SR({"maxAge": "bogus$"})).source == "inert"
            assert retention(SR({"maxAge": 30})).source == "inert"
            # `null < 1` is true in JS, so an explicit null count is rejected there.
            assert retention(SR({"maxCount": None})).source == "inert"

            # Gemini's Number() is not float(): it reads 0x10 as a live cap of 16 and
            # rejects Python's digit underscores.
            assert retention(SR({"maxAge": "3650d", "maxCount": "0x10"})).max_count == 16
            assert retention(SR({"maxAge": "3650d", "maxCount": "1_0"})).needs_warning

            # Its retention regex is anchored and does NOT trim, so a spaced floor is
            # rejected and falls back to 1d -- reading it as 7 days would make a live
            # one-day policy look like a rejected one.
            spaced = retention(SR({"maxAge": "1d", "minRetention": " 7d "}))
            assert spaced.needs_warning and spaced.max_age == "1d"

            # Validation is whole-object: one bad key un-coerces the WHOLE layer, so the
            # raw "false" reaches a gate that reads it for JS truthiness.
            assert retention(SR({"enabled": "false", "maxCount": "abc"})).needs_warning

            # strip-json-comments blanks a comment out rather than deleting it, so
            # `1/*x*/0` stays two tokens and Gemini fatals on the file.
            joined = retention('{"general":{"sessionRetention":{"maxCount":1/*x*/0}}}')
            assert joined.source == "unverifiable"

            # A DEFINITIVE rejection settles the config whatever the unevaluable parts
            # hold, so it is checked before anything fails open.
            assert retention(SR({"maxAge": "$AGE", "maxCount": 0})).source == "inert"
            # ...but an unevaluable floor is not definitive: $MIN could resolve below
            # 12h, and then a twelve-hour policy is live rather than rejected.
            assert retention(SR({"maxAge": "12h", "minRetention": "$MIN"})).source == "unknown"

            # Coerced, accepted, and yet no cap at all: `i >= Infinity` never fires.
            forever = retention(SR({"maxAge": "3650d", "maxCount": "Infinity"}))
            assert forever.max_count is None and not forever.needs_warning

            # JS String.trim() leaves the C0 separators in place, so this count is NOT a
            # number there -- which un-coerces the layer and makes "false" truthy.
            assert retention(SR({"enabled": "false", "maxCount": "\u001c1"})).needs_warning

            # Neither a huge literal nor undecodable bytes may take the launch down:
            # both are ordinary values to Gemini, and a crash here beats no warning.
            assert retention(SR({"maxAge": "3650d", "maxCount": "0x" + "f" * 1000})) is not None
            assert retention(SR({"maxAge": "9" * 400 + "d"})) is not None
            # ...but only the oversized ones clamp: a period of 5,000 ZEROS is zero to
            # parseInt, i.e. a rejected floor that leaves the 30-day default deleting.
            zeros = retention(SR({"maxAge": "30d", "minRetention": "0" * 5000 + "d"}))
            assert zeros.needs_warning
            with open(path, "wb") as fh:
                fh.write(b'{"general": "\xff\xfe"}')
            assert ot.gemini_retention().source == "unverifiable"

            # Python's \d matches Arabic-Indic digits; JavaScript's Number() does not,
            # so this is NaN there and never the rejected zero it looks like here.
            assert retention(SR({"maxCount": "\u0660"})).needs_warning
        finally:
            if old is None:
                os.environ.pop("GEMINI_CLI_HOME", None)
            else:
                os.environ["GEMINI_CLI_HOME"] = old


def test_gemini_system_settings_outrank_the_user_file():
    saved = {k: os.environ.get(k) for k in ("GEMINI_CLI_HOME", "GEMINI_CLI_SYSTEM_SETTINGS_PATH")}
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ".gemini"))
        user = os.path.join(tmp, ".gemini", "settings.json")
        system = os.path.join(tmp, "system.json")
        os.environ["GEMINI_CLI_HOME"] = tmp
        os.environ["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = system
        os.environ["GEMINI_CLI_SYSTEM_DEFAULTS_PATH"] = os.path.join(tmp, "absent.json")
        try:
            with open(user, "w", encoding="utf-8") as fh:
                json.dump(SR({"enabled": False}), fh)
            assert not ot.gemini_retention().needs_warning

            # Gemini merges the system file LAST, so it wins -- and the reported path is
            # the file to edit, not the one the user already set and thinks is in force.
            with open(system, "w", encoding="utf-8") as fh:
                json.dump(SR({"enabled": True, "maxAge": "7d"}), fh)
            enforced = ot.gemini_retention()
            assert enforced.needs_warning and enforced.max_age == "7d"
            assert enforced.settings_path == system
        finally:
            os.environ.pop("GEMINI_CLI_SYSTEM_DEFAULTS_PATH", None)
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_a_project_that_re_enables_gemini_cleanup_is_not_hidden_by_the_user_setting():
    # Gemini merges a trusted project's .gemini/settings.json ABOVE the user's, so a
    # machine-wide "off" is not proof the history is safe.
    saved = {k: os.environ.get(k) for k in ("GEMINI_CLI_HOME", "GEMINI_CLI_SYSTEM_SETTINGS_PATH")}
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ".gemini"))
        project = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(project, ".gemini"))
        os.environ["GEMINI_CLI_HOME"] = tmp
        os.environ["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = os.path.join(tmp, "system.json")
        os.environ["GEMINI_CLI_SYSTEM_DEFAULTS_PATH"] = os.path.join(tmp, "absent2.json")
        try:
            with open(os.path.join(tmp, ".gemini", "settings.json"), "w", encoding="utf-8") as fh:
                json.dump(SR({"enabled": False}), fh)
            with open(os.path.join(tmp, ".gemini", "projects.json"), "w", encoding="utf-8") as fh:
                json.dump({"projects": {project: "repo"}}, fh)
            assert not ot.gemini_retention().needs_warning  # no project override yet

            workspace = os.path.join(project, ".gemini", "settings.json")
            # A project that agrees with the user changes nothing...
            with open(workspace, "w", encoding="utf-8") as fh:
                json.dump(SR({"enabled": False, "maxAge": "7d"}), fh)
            assert not ot.gemini_retention().needs_warning
            # ...one that switches cleanup back on is the warning, and names its file.
            with open(workspace, "w", encoding="utf-8") as fh:
                json.dump(SR({"enabled": True, "maxAge": "7d"}), fh)
            override = ot.gemini_retention()
            assert override.source == "workspace" and override.needs_warning
            assert override.settings_path == workspace and override.max_age == "7d"

            # The system layer still outranks the project's, so it must be applied ON
            # TOP of it -- merging the project last reports the wrong policy, and can
            # report a safe one for a machine that deletes weekly.
            with open(workspace, "w", encoding="utf-8") as fh:
                json.dump(SR({"enabled": True, "maxAge": "3650d"}), fh)
            assert not ot.gemini_retention().needs_warning
            system = os.environ["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]
            with open(system, "w", encoding="utf-8") as fh:
                json.dump(SR({"maxAge": "7d"}), fh)
            ranked = ot.gemini_retention()
            assert ranked.needs_warning and ranked.max_age == "7d"

            # Gemini validates each FILE before merging, so a higher layer fixing an
            # unrelated key cannot restore the coercion the user's file lost -- the raw
            # "false" still reaches the JS gate, still truthy, still deleting.
            with open(workspace, "w", encoding="utf-8") as fh:
                json.dump(SR({}), fh)
            with open(os.path.join(tmp, ".gemini", "settings.json"), "w", encoding="utf-8") as fh:
                json.dump(SR({"enabled": "false", "maxCount": "abc"}), fh)
            with open(system, "w", encoding="utf-8") as fh:
                json.dump(SR({"maxCount": 50}), fh)
            assert ot.gemini_retention().needs_warning
        finally:
            os.environ.pop("GEMINI_CLI_SYSTEM_DEFAULTS_PATH", None)
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
