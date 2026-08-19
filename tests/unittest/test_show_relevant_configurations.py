import copy

import pytest

from pr_agent.algo.utils import show_relevant_configurations
from pr_agent.config_loader import get_settings


def _rendered_keys(section):
    return [line.split(":")[0] for line in show_relevant_configurations(section).splitlines()
            if line and not line.startswith((" ", "#", "<", "*", "`"))]


@pytest.fixture
def restore_config():
    """Snapshot and restore the whole CONFIG section, so a test cannot leak settings."""
    settings = get_settings(use_context=False)
    original = copy.deepcopy(settings.get("CONFIG", None))
    yield settings
    if original is not None:
        settings.set("CONFIG", original)


def test_hide_the_keys_listed_in_config_skip_keys(restore_config):
    """Hide the keys listed in the documented `config.skip_keys` setting from the
    published configuration block."""
    restore_config.set("config.skip_keys", ["model", "temperature"])

    keys = _rendered_keys("pr_reviewer")

    assert "model" not in keys
    assert "temperature" not in keys


@pytest.mark.parametrize("key", ["analytics_folder", "app_name"])
def test_default_skip_keys_are_matched_case_insensitively(restore_config, key):
    """The default skip list spells some entries in upper case (ANALYTICS_FOLDER,
    APP_NAME, ALLOWED_REPOS) while Dynaconf yields section keys lower-cased, so the
    comparison must be case-insensitive or those keys leak into the PR comment."""
    restore_config.set(f"config.{key}", "should-not-be-rendered")

    assert key not in _rendered_keys("pr_reviewer")
