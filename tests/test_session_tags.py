from __future__ import annotations

import json
import os
from unittest.mock import patch

from opentab.accounting.models import SessionRef
from opentab.annotations import MATCH_ANY, UNTAGGED, SessionTags, TagFilter
from opentab.persistence import session_tags
from opentab.persistence.session_tags import TagError
from opentab.persistence.workflow_config import load_workflow_config

from tests._support import isolated_annotations as isolated

A = SessionRef("box", "claude-code", "same-native-id").encode()
B = SessionRef("box", "codex", "same-native-id").encode()
C = SessionRef("other-box", "claude-code", "same-native-id").encode()


ON = {"version": 1, "tags": {"enabled": True}}


def _raw() -> dict:
    with open(session_tags.tags_path(), encoding="utf-8") as fh:
        return json.load(fh)


def _raises(code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except TagError as exc:
        assert exc.code == code, exc.code
        return exc
    raise AssertionError(f"expected TagError {code}")


def test_switch_defaults_off_and_doubt_never_enables():
    cases = [
        (None, False, ""),
        ({"version": 1}, False, ""),
        ({"version": 1, "tags": {"enabled": False}}, False, ""),
        (ON, True, ""),
        ({"version": 1, "tags": {"enabled": "yes"}}, False, "tags.enabled"),
        ({"version": 1, "tags": True}, False, "tags must be an object"),
        ({"version": 2, "tags": {"enabled": True}}, False, "newer format"),
        ({"version": True, "tags": {"enabled": True}}, False, "version"),
        ({"tags": {"enabled": True}}, False, "version"),
        ("{not json", False, "cannot read"),
        ("[]", False, "object"),
        # A broken work-items section does not take tags down with it.
        ({"version": 1, "tags": {"enabled": True}, "work_items": 7}, True, "work_items"),
    ]
    for config, enabled, error in cases:
        with isolated(config):
            loaded = load_workflow_config()
            assert loaded.tags is enabled, config
            assert error in loaded.error and (not error) == (not loaded.error), (config, loaded)
            assert loaded.work_items is False


def test_disabled_tags_read_nothing_and_create_no_files():
    for config, allowed in (
        (None, True),
        ({"version": 1, "tags": {"enabled": False}}, True),
        (ON, False),
    ):
        with isolated(config) as tmp:
            os.makedirs(os.path.join(tmp, "data", "opentab"))
            with open(session_tags.tags_path(), "w") as fh:
                fh.write("{ broken")  # must not even be read, let alone reported
            tags = SessionTags(allowed=allowed)
            tags.refresh()
            assert not tags.enabled and tags.error == "" and tags.tag_ids(A) == ()
            _raises("disabled", tags.set_tags, A, [], 0, create=["x"])
            assert sorted(os.listdir(os.path.join(tmp, "data", "opentab"))) == ["session-tags.json"]


def test_enabled_without_a_file_is_empty_and_writes_only_on_edit():
    with isolated(ON) as tmp:
        tags = SessionTags()
        tags.refresh()
        assert tags.enabled and tags.editable and tags.snapshot.revision == 0
        assert not os.path.exists(os.path.join(tmp, "data"))
        tags.set_tags(A, [], 0, create=["  #Performance ", "bug fix"])
        assert tags.labels(A) == ["bug fix", "Performance"]
        assert tags.revision(A) == 1


def test_equal_native_ids_on_other_harnesses_or_machines_stay_distinct():
    with isolated(ON):
        tags = SessionTags()
        tags.refresh()
        tags.set_tags(A, [], 0, create=["alpha"])
        tags.set_tags(B, [], 0, create=["beta"])
        assert tags.labels(A) == ["alpha"]
        assert tags.labels(B) == ["beta"]
        assert tags.labels(C) == []


def test_labels_normalize_and_case_insensitive_duplicates_are_rejected():
    with isolated(ON):
        assert session_tags.normalize_label("  ##Café ") == "Café"
        # NFD input is stored in NFC, so the two spellings are one tag.
        assert session_tags.normalize_label("Café") == "Café"
        for bad in ("", "  # ", "a\nb", "a​b", "x" * 41, 7):
            _raises("invalid_label", session_tags.normalize_label, bad)
        tag_id, _ = session_tags.create_tag("Release 1.30")
        _raises("duplicate", session_tags.create_tag, "release 1.30")
        other, snap = session_tags.create_tag("perf")
        _raises("duplicate", session_tags.rename_tag, other, "RELEASE 1.30", 1)
        # Creating while assigning reuses the existing tag instead of duplicating it.
        snap = session_tags.update_session_tags(A, 0, create=["RELEASE 1.30"])
        assert snap.tags_of(A) == (tag_id,) and len(snap.labels) == 2


def test_rename_keeps_membership_and_checks_the_tag_revision():
    with isolated(ON):
        tags = SessionTags()
        tags.refresh()
        tags.set_tags(A, [], 0, create=["perf"])
        tag_id = tags.tag_ids(A)[0]
        tags.rename(tag_id, "performance")
        assert tags.labels(A) == ["performance"] and tags.tag_ids(A) == (tag_id,)
        # A second frontend holding the old revision is refused with the current truth.
        exc = _raises("stale", session_tags.rename_tag, tag_id, "speed", 1)
        assert exc.current == {"label": "performance", "revision": 2}


def test_stale_same_session_edits_conflict_but_other_sessions_merge():
    with isolated(ON):
        first, second = SessionTags(), SessionTags()
        first.refresh()
        second.refresh()
        first.set_tags(A, [], 0, create=["one"])
        # The second window never saw that edit; its edit of B still lands...
        second.set_tags(B, [], 0, create=["two"])
        assert {second.labels(A)[0], second.labels(B)[0]} == {"one", "two"}
        # ...but a stale edit of A is refused and reports what is there now.
        stale = SessionTags()
        stale.refresh()
        first.set_tags(A, [], first.revision(A))  # remove all
        exc = _raises("stale", stale.set_tags, A, [], 1, create=["resurrect"])
        assert exc.current == {"tag_ids": [], "revision": 2}
        # The cleared record is a tombstone, so the revision never resets to zero.
        assert _raw()["assignments"][A] == {"tag_ids": [], "revision": 2}
        _raises("stale", stale.set_tags, A, [], 0, create=["resurrect"])


def test_unknown_fields_and_unresolved_ids_survive_writes():
    with isolated(ON):
        os.makedirs(os.path.dirname(session_tags.tags_path()))
        document = {
            "version": 1,
            "revision": 3,
            "future": {"keep": True},
            "tags": {
                "t1": {"label": "kept", "revision": 1, "colour": "teal"},
                "tbad": {"label": 5},
            },
            "assignments": {
                A: {"tag_ids": ["t1", "tgone"], "revision": 1, "pinned": True},
                C: {"tag_ids": ["t1"], "revision": 4},  # a session not loaded anywhere
                "junk": "not a record",
            },
        }
        with open(session_tags.tags_path(), "w") as fh:
            json.dump(document, fh)
        tags = SessionTags()
        tags.refresh()
        assert tags.labels(A) == ["kept", "?tgone"]
        assert tags.unresolved([A, C]) == ["tgone"]
        # Keeping an unresolved ID is fine; adding one the session lacks is not.
        tags.set_tags(A, ["tgone"], 1)
        _raises("unknown_tag", tags.set_tags, B, ["tgone"], 0)
        raw = _raw()
        assert raw["future"] == {"keep": True} and raw["revision"] == 4
        assert raw["tags"]["t1"]["colour"] == "teal" and raw["tags"]["tbad"] == {"label": 5}
        assert raw["assignments"][A] == {"tag_ids": ["tgone"], "revision": 2, "pinned": True}
        assert raw["assignments"][C] == {"tag_ids": ["t1"], "revision": 4}
        assert raw["assignments"]["junk"] == "not a record"


def test_broken_or_newer_documents_are_reported_and_never_overwritten():
    for text, code in (
        ("{ broken", "unreadable"),
        ('{"version": 1, "tags": []}', "malformed"),
        ('{"version": 2, "tags": {}, "assignments": {}}', "newer_version"),
        ('{"tags": {}, "assignments": {}}', "malformed"),
    ):
        with isolated(ON):
            os.makedirs(os.path.dirname(session_tags.tags_path()))
            with open(session_tags.tags_path(), "w") as fh:
                fh.write(text)
            tags = SessionTags()
            tags.refresh()
            assert tags.enabled and not tags.readable and not tags.editable
            assert tags.error.startswith("tags: ")
            _raises("unavailable", tags.set_tags, A, [], 0, create=["x"])
            _raises(code, session_tags.update_session_tags, A, 0, create=["x"])
            with open(session_tags.tags_path()) as fh:
                assert fh.read() == text


def test_a_malformed_target_record_refuses_only_that_session():
    with isolated(ON):
        os.makedirs(os.path.dirname(session_tags.tags_path()))
        with open(session_tags.tags_path(), "w") as fh:
            json.dump({"version": 1, "tags": {}, "assignments": {A: {"tag_ids": "x"}}}, fh)
        _raises("malformed_record", session_tags.update_session_tags, A, 0, create=["x"])
        session_tags.update_session_tags(B, 0, create=["x"])
        assert _raw()["assignments"][A] == {"tag_ids": "x"}


def test_failed_writes_and_missing_locks_keep_authored_data():
    with isolated(ON):
        session_tags.update_session_tags(A, 0, create=["kept"])
        before = _raw()
        with patch.object(session_tags.os, "replace", side_effect=OSError("disk full")):
            _raises("unwritable", session_tags.update_session_tags, A, 1, create=["lost"])
        assert _raw() == before
        assert [
            n for n in os.listdir(os.path.dirname(session_tags.tags_path())) if n.endswith(".tmp")
        ] == []
        # Without fcntl (native Windows) editing is refused, never run unlocked.
        with patch.object(session_tags, "fcntl", None):
            tags = SessionTags()
            tags.refresh()
            assert tags.labels(A) == ["kept"] and not tags.editable
            assert "Windows" in tags.edit_refusal()
            _raises("unsupported", session_tags.update_session_tags, A, 1, create=["lost"])
        assert _raw() == before


def test_invalid_keys_and_limits_are_refused():
    with isolated(ON):
        for key in ("plain-id", "ot1_!!", SessionRef("m", "h", "x").encode() + "=="):
            _raises("invalid_session", session_tags.update_session_tags, key, 0, create=["x"])
        _raises("invalid", session_tags.update_session_tags, A, -1, create=["x"])
        _raises("invalid", session_tags.update_session_tags, A, True, create=["x"])
        many = [f"tag {i}" for i in range(session_tags.MAX_CREATE_PER_EDIT + 1)]
        _raises("limit", session_tags.update_session_tags, A, 0, create=many)
        ids = [
            session_tags.create_tag(f"t{i}")[0]
            for i in range(session_tags.MAX_TAGS_PER_SESSION + 1)
        ]
        _raises("limit", session_tags.update_session_tags, A, 0, replace=ids)
        assert A not in _raw()["assignments"]


def test_concurrent_writers_keep_every_session_and_detect_same_session_races():
    with isolated(ON):
        keys = [SessionRef("box", "codex", f"s{i}").encode() for i in range(8)]
        kids = []
        for i, key in enumerate(keys):
            pid = os.fork()
            if pid == 0:
                try:
                    session_tags.update_session_tags(key, 0, create=[f"tag {i}"])
                finally:
                    os._exit(0)
            kids.append(pid)
        for pid in kids:
            os.waitpid(pid, 0)
        snap = session_tags.load_snapshot()
        assert [snap.labels[snap.tags_of(key)[0]] for key in keys] == [f"tag {i}" for i in range(8)]
        assert snap.revision == 8
        # Eight processes all expecting revision 0 of one session: exactly one wins.
        kids = []
        for i in range(8):
            pid = os.fork()
            if pid == 0:
                code = 1
                try:
                    session_tags.update_session_tags(A, 0, create=[f"race {i}"])
                    code = 0
                except TagError:
                    code = 3
                finally:
                    os._exit(code)
            kids.append(pid)
        codes = sorted(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) for pid in kids)
        assert codes == [0] + [3] * 7
        assert session_tags.load_snapshot().revision_of(A) == 1


def test_tag_filters_match_all_any_and_untagged():
    both = TagFilter(ids=frozenset({"x", "y"}))
    either = TagFilter(MATCH_ANY, frozenset({"x", "y"}))
    untagged = TagFilter(UNTAGGED)
    assert both.matches(["x", "y", "z"]) and not both.matches(["x"])
    assert either.matches(["y"]) and not either.matches(["z"])
    assert untagged.matches([]) and not untagged.matches(["gone"])


def test_bulk_patches_only_selected_memberships_and_reports_every_conflict():
    with isolated(ON):
        x, _ = session_tags.create_tag("x")
        y, _ = session_tags.create_tag("y")
        z, _ = session_tags.create_tag("z")
        session_tags.update_session_tags(A, 0, add=[x, z])
        session_tags.update_session_tags(B, 0, add=[z])
        before = _raw()
        exc = _raises(
            "stale", session_tags.bulk_update_session_tags, {A: 0, B: 0, C: 0}, add=[y], remove=[x]
        )
        assert set(exc.current["conflicts"]) == {A, B}
        assert exc.current["conflicts"][A] == {"tag_ids": [x, z], "revision": 1}
        assert _raw() == before  # even the nonconflicting C must not change
        snap = session_tags.bulk_update_session_tags({A: 1, B: 1}, add=[y], remove=[x])
        assert snap.tags_of(A) == (z, y) and snap.tags_of(B) == (z, y)
        assert snap.revision == before["revision"] + 1
        assert snap.revision_of(A) == snap.revision_of(B) == 2
        before = _raw()
        session_tags.bulk_update_session_tags({A: 2, B: 2}, add=[y], remove=[x])
        assert _raw() == before  # no-op does not create phantom revisions


def test_bulk_validation_and_write_failure_never_partially_write():
    with isolated(ON):
        ids = [
            session_tags.create_tag(f"tag{i}")[0]
            for i in range(session_tags.MAX_TAGS_PER_SESSION + 1)
        ]
        session_tags.update_session_tags(B, 0, add=ids[:-1])
        before = _raw()
        _raises("limit", session_tags.bulk_update_session_tags, {A: 0, B: 1}, add=[ids[-1]])
        _raises("unknown_tag", session_tags.bulk_update_session_tags, {A: 0, B: 1}, add=["absent"])
        _raises("invalid", session_tags.bulk_update_session_tags, {A: True}, add=ids[:1])
        _raises("invalid", session_tags.bulk_update_session_tags, {A: 0}, add=[{}])
        _raises("invalid_session", session_tags.bulk_update_session_tags, {"bare": 0})
        many = {
            SessionRef("box", "codex", str(i)).encode(): 0
            for i in range(session_tags.MAX_BULK_SESSIONS + 1)
        }
        _raises("limit", session_tags.bulk_update_session_tags, many)
        with patch.object(session_tags.os, "replace", side_effect=OSError("disk full")):
            _raises(
                "unwritable",
                session_tags.bulk_update_session_tags,
                {A: 0, B: 1},
                add=ids[:1],
                remove=ids[1:2],
            )
        assert _raw() == before


def test_global_delete_preserves_fields_missing_roots_and_revision_tombstones():
    with isolated(ON):
        session_tags.update_session_tags(A, 0, create=["x", "y"])
        snap = session_tags.load_snapshot()
        x, y = snap.tags_of(A)
        session_tags.update_session_tags(C, 0, add=[x])  # unloaded retained root
        raw = _raw()
        raw["future"] = {"keep": True}
        raw["tags"][x]["colour"] = "teal"
        raw["assignments"][A]["pinned"] = True
        raw["assignments"]["malformed-unrelated"] = {"future": [1, 2]}
        with open(session_tags.tags_path(), "w") as fh:
            json.dump(raw, fh)
        tags = SessionTags()
        tags.refresh()
        assert tags.affected_count(x) == 2
        old_revision = tags.snapshot.revision
        snap = tags.delete(x, 1, old_revision)
        assert x not in snap.labels and snap.tag_revisions[x] == 2
        assert snap.tags_of(A) == (y,) and snap.tags_of(C) == ()
        raw = _raw()
        assert raw["tags"][x] == {"label": "x", "revision": 2, "colour": "teal", "deleted": True}
        assert raw["assignments"][A]["pinned"] is True
        assert raw["assignments"][A]["revision"] == 2
        assert raw["assignments"][C] == {"tag_ids": [], "revision": 2}
        assert raw["assignments"]["malformed-unrelated"] == {"future": [1, 2]}
        assert raw["future"] == {"keep": True}
        _raises("unknown_tag", session_tags.rename_tag, x, "resurrect", 1)
        _raises("unknown_tag", session_tags.delete_tag, x, 1, snap.revision)
        _raises("stale", session_tags.update_session_tags, C, 1, add=[y])
        _raises("unknown_tag", session_tags.bulk_update_session_tags, {C: 2}, add=[x])
        new_x, _ = session_tags.create_tag("x")
        assert new_x != x  # a fresh label never reuses the deleted identity


def test_delete_confirmation_and_captured_rename_revisions_refuse_races():
    with isolated(ON):
        tags = SessionTags()
        tags.refresh()
        x = tags.create("x")
        captured = tags.snapshot.revision
        session_tags.update_session_tags(C, 0, add=[x])
        before = _raw()
        _raises("stale", tags.delete, x, 1, captured)
        assert _raw() == before
        tags.refresh()
        tags.rename(x, "renamed", 1)
        _raises("stale", tags.rename, x, "stale dialog", 1)
        before = _raw()
        _raises("stale", tags.delete, x, 1, tags.snapshot.revision)
        assert _raw() == before


def test_bulk_and_delete_recheck_disabled_capability():
    with isolated(ON) as tmp:
        tags = SessionTags()
        tags.refresh()
        x = tags.create("x")
        before = _raw()
        with open(os.path.join(tmp, "config", "opentab", "workflow.json"), "w") as fh:
            json.dump({"version": 1, "tags": {"enabled": False}}, fh)
        _raises("disabled", tags.bulk_update, {A: 0}, add=[x])
        _raises("disabled", tags.delete, x, 1, 1)
        _raises("disabled", tags.rename, x, "renamed", 1)
        assert _raw() == before


def test_concurrent_bulk_writers_cannot_partially_land_overlapping_batches():
    with isolated(ON):
        x, _ = session_tags.create_tag("x")
        y, _ = session_tags.create_tag("y")
        kids = []
        for targets, tag in (({A: 0, B: 0}, x), ({B: 0, C: 0}, y)):
            pid = os.fork()
            if pid == 0:
                try:
                    session_tags.bulk_update_session_tags(targets, add=[tag])
                except TagError as exc:
                    os._exit(3 if exc.code == "stale" else 1)
                except Exception:
                    os._exit(1)
                os._exit(0)
            kids.append(pid)
        codes = sorted(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) for pid in kids)
        assert codes == [0, 3]
        snap = session_tags.load_snapshot()
        if snap.tags_of(B) == (x,):
            assert snap.tags_of(A) == (x,) and C not in snap.assignments
        else:
            assert snap.tags_of(B) == snap.tags_of(C) == (y,) and A not in snap.assignments
        assert snap.revision == 3


def test_global_delete_refuses_malformed_affected_record_without_loss():
    with isolated(ON):
        x, _ = session_tags.create_tag("x")
        session_tags.update_session_tags(A, 0, add=[x])
        raw = _raw()
        raw["assignments"][C] = {"tag_ids": [x], "revision": "bad", "future": True}
        with open(session_tags.tags_path(), "w") as fh:
            json.dump(raw, fh)
        _raises("malformed_record", session_tags.delete_tag, x, 1, raw["revision"])
        assert _raw() == raw


def test_bulk_preserves_unknown_fields_unresolved_ids_and_malformed_other_records():
    with isolated(ON):
        x, _ = session_tags.create_tag("x")
        raw = _raw()
        raw["future"] = ["retained"]
        raw["tags"][x]["future"] = 17
        raw["assignments"] = {
            A: {"tag_ids": ["unresolved"], "revision": 3, "future": 18},
            C: {"tag_ids": [x], "revision": 4, "future": 19},
            "malformed": {"tag_ids": "opaque"},
        }
        with open(session_tags.tags_path(), "w") as fh:
            json.dump(raw, fh)
        snap = session_tags.bulk_update_session_tags({A: 3}, add=[x])
        after = _raw()
        assert snap.tags_of(A) == ("unresolved", x)
        assert after["assignments"][A]["future"] == 18
        assert after["assignments"][C] == raw["assignments"][C]
        assert after["assignments"]["malformed"] == raw["assignments"]["malformed"]
        assert after["future"] == raw["future"] and after["tags"] == raw["tags"]


def test_concurrent_global_delete_and_membership_add_obey_confirmation_guard():
    with isolated(ON):
        x, initial = session_tags.create_tag("x")
        kids = []
        for delete in (True, False):
            pid = os.fork()
            if pid == 0:
                try:
                    if delete:
                        session_tags.delete_tag(x, 1, initial.revision)
                    else:
                        session_tags.bulk_update_session_tags({A: 0, B: 0}, add=[x])
                except TagError as exc:
                    os._exit(3 if exc.code in ("stale", "unknown_tag") else 1)
                except Exception:
                    os._exit(1)
                os._exit(0)
            kids.append(pid)
        codes = sorted(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) for pid in kids)
        assert codes == [0, 3]
        snap = session_tags.load_snapshot()
        assert snap.revision == 2
        if x in snap.labels:
            assert snap.tags_of(A) == snap.tags_of(B) == (x,)
        else:
            assert not snap.assignments and snap.tag_revisions[x] == 2
