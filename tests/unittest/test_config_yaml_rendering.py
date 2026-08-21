"""The configuration block is published inside a ```yaml fence, so it must be valid YAML."""
import copy

import pytest
import yaml

from pr_agent.algo.utils import show_relevant_configurations
from pr_agent.config_loader import get_settings


@pytest.fixture
def restore_config():
    settings = get_settings(use_context=False)
    original = copy.deepcopy(settings.get("CONFIG", None))
    yield settings
    if original is not None:
        settings.set("CONFIG", original)


def _config_block(section="pr_reviewer"):
    text = show_relevant_configurations(section)
    return text.split("```yaml", 1)[1].split("```", 1)[0]


def test_a_dict_valued_setting_renders_as_yaml(restore_config):
    """A nested value must not be emitted as a Python repr."""
    restore_config.set("config.nested_setting", {"inner": "value"})

    block = _config_block()

    assert "{'inner': 'value'}" not in block


def test_the_rendered_block_parses_as_yaml(restore_config):
    """The whole block must round-trip through a YAML parser."""
    restore_config.set("config.nested_setting", {"inner": "value"})
    restore_config.set("config.list_setting", ["a", "b"])

    parsed = yaml.safe_load(_config_block())

    assert parsed["nested_setting"] == {"inner": "value"}
    assert parsed["list_setting"] == ["a", "b"]


def test_scalar_settings_are_unchanged(restore_config):
    """Plain values keep their existing simple rendering."""
    restore_config.set("config.scalar_setting", "plain")

    assert "scalar_setting: plain" in _config_block()
