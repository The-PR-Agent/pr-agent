import pytest

from pr_agent.algo.utils import show_relevant_configurations
from pr_agent.config_loader import get_settings


def _rendered_keys(section):
    return [line.split(":")[0] for line in show_relevant_configurations(section).splitlines()
            if line and not line.startswith((" ", "#", "<", "*", "`"))]


@pytest.fixture
def restore_config():
    settings = get_settings(use_context=False)
    saved = {}
    yield settings, saved
    for key, value in saved.items():
        settings.set(f"config.{key}", value)


def test_config_skip_keys_hides_the_listed_keys(restore_config):
    """`config.skip_keys` is a documented setting; keys listed in it must not be rendered
    into the published configuration block."""
    settings, saved = restore_config
    saved["skip_keys"] = settings.config.get("skip_keys", [])
    settings.set("config.skip_keys", ["model", "temperature"])

    keys = _rendered_keys("pr_reviewer")

    assert "model" not in keys
    assert "temperature" not in keys
