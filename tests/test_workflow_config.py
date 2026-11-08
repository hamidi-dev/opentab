from opentab.persistence.workflow_config import DEFAULT_STATES, load_workflow_config

from tests._support import isolated_annotations as isolated


def test_work_switch_defaults_off_and_ignores_legacy_tags_section():
    with isolated({"version": 1, "tags": {"enabled": False}}):
        config = load_workflow_config()
        assert not config.work_items and config.states == () and not config.error
    with isolated({"version": 1, "work_items": {"enabled": True}}):
        config = load_workflow_config()
        assert config.work_items and config.states == DEFAULT_STATES
    with isolated(None):
        config = load_workflow_config()
        assert not config.work_items and not config.error


def test_custom_states_validate():
    state = {"id": "pending", "label": "Pending", "short": "P", "color": "cyan", "done": False}
    for definitions, good in (
        ([state], True),
        ([], False),
        ([state, state], False),
        ([{**state, "done": 1}], False),
        ([{**state, "color": "invalid"}], False),
        ([{**state, "label": "bad\n"}], False),
    ):
        config = {"version": 1, "work_items": {"enabled": True, "states": definitions}}
        with isolated(config):
            loaded = load_workflow_config()
            assert loaded.work_items == good
            assert bool(loaded.states) == good
            assert bool(loaded.error) != good


def test_file_and_section_errors_surface_as_work_errors():
    from opentab.work_items import WorkItems

    for config, error in (
        ({"version": 1, "tags": {"enabled": "yes"}}, ""),
        ({"version": 1, "work_items": {"enabled": "yes"}}, "work_items.enabled"),
        ({"version": 1, "work_items": 7}, "must be an object"),
        ({"version": 2, "work_items": {"enabled": True}}, "newer format"),
        ("{not json", "cannot read"),
        ({"version": 1, "work_items": {"enabled": True, "states": []}}, "state"),
    ):
        with isolated(config):
            loaded = load_workflow_config()
            assert error in loaded.error and bool(error) == bool(loaded.error), (config, loaded)
            model = WorkItems()
            model.refresh()
            assert model.error == loaded.error and not model.enabled
