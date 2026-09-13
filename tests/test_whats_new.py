import json
from unittest.mock import patch

import opentab as ot
from opentab.presentation import whats_new


def _release(version=ot.__version__, title="Fixed"):
    return {
        "version": version,
        "release_url": whats_new.RELEASES_URL + "/tag/v" + version,
        "sections": [{"title": title, "items": [{"text": "A user-visible change."}]}],
    }


def _resource(*releases):
    return json.dumps({"releases": list(releases)})


def test_bundled_release_history_matches_installed_version_and_is_well_ordered():
    history = whats_new.load_release_history(ot.__version__)
    assert history is not None
    assert {"1.18.0", "1.19.0", "1.20.0", "1.21.0", "1.22.0"} <= {
        note["version"] for note in history
    }
    assert history[0]["version"] == ot.__version__
    versions = [tuple(int(part) for part in note["version"].split(".")) for note in history]
    assert versions == sorted(versions, reverse=True)
    assert len(versions) == len(set(versions))
    for note in history:
        assert note["sections"]
        assert note["release_url"].endswith("/tag/v" + note["version"])
    assert whats_new.load_release_notes(ot.__version__) == history[0]


def test_history_sorts_numerically_and_never_offers_versions_after_the_install():
    releases = [_release("1.9.0"), _release("1.11.0"), _release("1.10.0")]
    with patch.object(whats_new, "_resource_text", return_value=_resource(*releases)):
        history = whats_new.load_release_history("1.10.0")
    assert [note["version"] for note in history or []] == ["1.10.0", "1.9.0"]


def test_release_history_fails_closed_for_missing_malformed_duplicate_and_mismatched_data():
    bad_resources = (
        "{broken",
        "[]",
        "{}",
        json.dumps({"releases": []}),
        _resource(_release(), _release()),
        _resource(dict(_release(), release_url=whats_new.RELEASES_URL + "/tag/v1.21.0")),
        _resource(dict(_release(), sections=[])),
    )
    for raw in bad_resources:
        with patch.object(whats_new, "_resource_text", return_value=raw):
            assert whats_new.load_release_history(ot.__version__) is None
    with patch.object(whats_new, "_resource_text", side_effect=FileNotFoundError):
        assert whats_new.load_release_history(ot.__version__) is None
    assert whats_new.load_release_history("9.9.9") is None


def test_upgrade_detection_is_numeric_stable_and_requires_matching_notes():
    notes = {"version": "1.10.0"}
    assert whats_new.should_announce("1.9.0", "1.10.0", notes)
    for stored, installed in (
        (None, "1.10.0"),
        ("broken", "1.10.0"),
        ("1.10.0", "1.10.0"),
        ("1.11.0", "1.10.0"),
        ("1.9.0rc1", "1.10.0"),
        ("1.9.0", "1.10.0rc1"),
    ):
        assert not whats_new.should_announce(stored, installed, notes)
    assert not whats_new.should_announce("1.9.0", "1.10.0", None)
    assert not whats_new.should_announce("1.9.0", "1.10.0", {"version": "1.9.0"})


def test_release_notes_allow_one_nonempty_section_and_validate_every_item():
    for title in ("New", "Improved", "Fixed"):
        release = _release(title=title)
        with patch.object(whats_new, "_resource_text", return_value=_resource(release)):
            assert whats_new.load_release_notes() == release

    fixed = {"title": "Fixed", "items": [{"text": "Corrected an estimate."}]}
    bad_sections = (
        None,
        [],
        {},
        [None],
        [fixed, fixed],
        [{"title": [], "items": fixed["items"]}],
        [{"title": "Unknown", "items": fixed["items"]}],
        [{"title": "New", "items": []}],
        [{"title": "Fixed", "items": [None]}],
        [{"title": "Fixed", "items": [{"text": " "}]}],
        [{"title": "Fixed", "items": [{"text": "a", "availability": []}]}],
        [{"title": "Fixed", "items": [{"text": "a", "hint": "bad"}]}],
        [{"title": "Fixed", "items": [{"text": "a", "hint": {"text": "b", "binding": {}}}]}],
    )
    for sections in bad_sections:
        release = dict(_release(), sections=sections)
        with patch.object(whats_new, "_resource_text", return_value=_resource(release)):
            assert whats_new.load_release_notes() is None


def test_stable_version_rejects_malformed_types_and_oversized_digits():
    for value in (None, 1, [], {}, ["1.2.3"], "1.2", "01.2.3", "1.2.3.4"):
        assert whats_new.stable_version(value) is None
    assert whats_new.stable_version("9" * 10_000 + ".2.3") is None


def test_marker_merge_never_replaces_a_newer_valid_disk_value():
    assert whats_new.marker_to_save("1.22.0", "1.21.0") == "1.22.0"
    assert whats_new.marker_to_save("1.20.0", "1.21.0") == "1.21.0"
    assert whats_new.marker_to_save("broken", "1.21.0") == "1.21.0"
    assert whats_new.marker_to_save("1.22.0", "broken") == "1.22.0"


def test_public_payload_carries_history_or_degrades_to_the_releases_page():
    payload = whats_new.public_payload(ot.__version__)
    assert payload["version"] == ot.__version__
    assert payload["releases"] == whats_new.load_release_history()
    with patch.object(whats_new, "load_release_history", return_value=None):
        payload = whats_new.public_payload("1.21.0")
    assert payload == {
        "version": "1.21.0",
        "unavailable": True,
        "release_url": whats_new.RELEASES_URL,
    }


def test_disabled_hints_stay_quiet_and_history_browsing_never_regresses_acknowledgement():
    from tests._support import app_with

    app = app_with([])
    app.last_announced_version = "1.20.0"
    app.configure_whats_new_hint(ot.__version__, enabled=False)
    assert not app._whats_new_hint_pending and not app.notice
    assert app.whats_new_marker_to_save is None
    app.open_whats_new()
    assert app.whats_new and app.whats_new_marker_to_save == ot.__version__
    app.step_whats_new(1)
    viewed = app.whats_new_notes
    assert viewed is not None and viewed["version"] != ot.__version__
    assert app.whats_new_marker_to_save == ot.__version__
    app.configure_whats_new_hint(ot.__version__, enabled=True)
    assert not app._whats_new_hint_pending
    assert app.whats_new_marker_to_save == ot.__version__

    fresh = app_with([])
    fresh.last_announced_version = "1.20.0"
    fresh.whats_new_index = 1
    fresh.configure_whats_new_hint(ot.__version__, enabled=True)
    assert fresh._whats_new_hint_pending
    assert fresh.whats_new_marker_to_save == "1.20.0"
