import copy

import pytest

from pr_agent.algo.run_output import show_relevant_configurations
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_config import PRConfig


def _rendered_keys(section):
    return [line.split(":")[0] for line in show_relevant_configurations(section).splitlines()
            if line and not line.startswith((" ", "#", "<", "*", "`"))]


@pytest.fixture
def restore_config():
    """Snapshot and restore rendered configuration sections, so tests cannot leak settings."""
    settings = get_settings(use_context=False)
    original = {
        section: copy.deepcopy(settings.get(section, None))
        for section in ("CONFIG", "PR_REVIEWER")
    }
    yield settings
    for section, value in original.items():
        if value is not None:
            settings.set(section, value)


def test_hide_the_keys_listed_in_config_skip_keys(restore_config):
    """Hide the keys listed in the documented `config.skip_keys` setting from the
    published configuration block."""
    restore_config.set("config.skip_keys", ["model", "temperature"])

    keys = _rendered_keys("pr_reviewer")

    assert "model" not in keys
    assert "temperature" not in keys


@pytest.mark.parametrize("key", ["analytics_folder", "app_name"])
def test_match_the_default_skip_keys_case_insensitively(restore_config, key):
    """Match the default skip list case-insensitively: it spells some entries in upper case
    while Dynaconf yields section keys lower-cased."""
    restore_config.set(f"config.{key}", "should-not-be-rendered")

    assert key not in _rendered_keys("pr_reviewer")


def test_tolerate_a_non_string_entry_in_config_skip_keys(restore_config):
    """Tolerate a non-string entry in the user-supplied skip list rather than raising
    AttributeError while lower-casing it."""
    restore_config.set("config.skip_keys", ["model", 123, None])

    assert "model" not in _rendered_keys("pr_reviewer")


def test_hide_host_only_and_credential_like_keys(restore_config):
    restore_config.set("config.extra_config_url", "https://user:password@example.com/config.toml")
    restore_config.set("config.global_settings_repo", "private/config")
    restore_config.set("config.api_token", "secret")
    restore_config.set("pr_reviewer.publish_error_details", True)
    restore_config.set("pr_reviewer.secret_key", "secret")

    keys = _rendered_keys("pr_reviewer")

    assert "extra_config_url" not in keys
    assert "global_settings_repo" not in keys
    assert "api_token" not in keys
    assert "publish_error_details" not in keys
    assert "secret_key" not in keys


def test_hide_host_only_and_credential_like_keys_from_config_output(restore_config):
    restore_config.set("config.extra_config_url", "https://user:password@example.com/config.toml")
    restore_config.set("config.global_settings_repo", "private/config")
    restore_config.set("config.api_token", "secret")

    config_output = PRConfig.__new__(PRConfig)._prepare_pr_configs()

    assert "config.extra_config_url" not in config_output
    assert "config.global_settings_repo" not in config_output
    assert "config.api_token" not in config_output
