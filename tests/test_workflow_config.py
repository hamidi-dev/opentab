from opentab.persistence.workflow_config import DEFAULT_STATES, load_workflow_config

from tests._support import isolated_annotations as isolated


def test_work_switch_independent_and_defaults_only_enabled():
    with isolated({"version": 1, "tags": {"enabled": True}}):
        config = load_workflow_config()
        assert config.tags and not config.work_items and config.states == ()
    with isolated({"version": 1, "work_items": {"enabled": True}}):
        config = load_workflow_config()
        assert not config.tags and config.work_items and config.states == DEFAULT_STATES


def test_custom_states_validate_and_fail_independently():
    state = {"id": "pending", "label": "Pending", "short": "P", "color": "cyan", "done": False}
    for definitions, good in (
        ([state], True),
        ([], False),
        ([state, state], False),
        ([{**state, "done": 1}], False),
        ([{**state, "color": "invalid"}], False),
        ([{**state, "label": "bad\n"}], False),
    ):
        config = {
            "version": 1,
            "tags": {"enabled": True},
            "work_items": {"enabled": True, "states": definitions},
        }
        with isolated(config):
            loaded = load_workflow_config()
            assert loaded.tags and loaded.work_items == good
            assert bool(loaded.states) == good
            assert bool(loaded.error) != good


def test_work_errors_exclude_tag_and_file_problems():
    from opentab.work_items import WorkItems

    for config, items_error in (
        ({"version": 1, "tags": {"enabled": "yes"}}, False),
        ({"version": 1, "tags": {"enabled": "yes"}, "work_items": {"enabled": True}}, False),
        ({"version": 2, "work_items": {"enabled": True}}, False),
        ({"version": 1, "work_items": {"enabled": True, "states": []}}, True),
    ):
        with isolated(config):
            loaded = load_workflow_config()
            assert loaded.error and bool(loaded.items_error) == items_error
            model = WorkItems()
            model.refresh()
            assert model.error == loaded.items_error
