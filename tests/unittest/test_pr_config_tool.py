"""Unit tests for pr_agent/tools/pr_config.py (PRConfig).

The git provider is stubbed out (no network); the interesting logic is
``_prepare_pr_configs`` — which sections/keys of the settings are echoed into
the PR comment and, critically, which secret-looking keys are masked out.
"""

from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_config import PRConfig
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class FakeGitProvider:
    def __init__(self, pr_url, *args, **kwargs):
        self.pr_url = pr_url
        self.publish_comment = MagicMock()
        self.remove_initial_comment = MagicMock()


@pytest.fixture
def pr_config(monkeypatch):
    monkeypatch.setattr(
        "pr_agent.tools.pr_config.get_git_provider",
        lambda: FakeGitProvider,
    )
    return PRConfig("https://github.com/owner/repo/pull/1")


@pytest.fixture
def settings_guard():
    keys = (
        "config.publish_output",
        "config.github_token",
        "config.webhook_secret",
        "config.app_id",
        "config.fake_custom_flag",
        "pr_reviewer.my_api_secret",
        "openai.some_flag",
    )
    saved = snapshot_settings(keys)
    try:
        yield get_settings()
    finally:
        restore_settings(saved)


class TestPreparePrConfigs:
    def test_output_structure(self, pr_config):
        out = pr_config._prepare_pr_configs()
        assert out.startswith("<details>")
        assert "PR-Agent Configurations" in out
        assert "```yaml" in out
        assert out.rstrip().endswith("</details>")

    def test_contains_config_section_and_known_keys(self, pr_config):
        out = pr_config._prepare_pr_configs()
        # section headers keep Dynaconf's uppercase casing
        assert "==================== CONFIG ====================" in out
        # a few stable, non-secret keys from configuration.toml
        assert "config.git_provider = " in out
        assert "config.publish_output = " in out

    def test_contains_pr_tool_sections(self, pr_config):
        out = pr_config._prepare_pr_configs()
        assert "==================== PR_REVIEWER ====================" in out
        assert "==================== PR_DESCRIPTION ====================" in out

    def test_non_pr_non_config_sections_are_excluded(self, pr_config, settings_guard):
        # 'openai' does not start with "pr_"/"config", so it must never be echoed
        settings_guard.set("openai.some_flag", True)
        out = pr_config._prepare_pr_configs()
        assert "openai" not in out.lower()

    def test_partial_skip_masks_token_like_keys(self, pr_config, settings_guard):
        settings_guard.set("config.github_token", "SUPER_SECRET_TOKEN_VALUE")
        out = pr_config._prepare_pr_configs()
        assert "github_token" not in out.lower()
        assert "SUPER_SECRET_TOKEN_VALUE" not in out

    def test_partial_skip_masks_secret_like_keys_in_pr_sections(self, pr_config, settings_guard):
        settings_guard.set("pr_reviewer.my_api_secret", "HUSH_HUSH_VALUE")
        out = pr_config._prepare_pr_configs()
        assert "my_api_secret" not in out.lower()
        assert "HUSH_HUSH_VALUE" not in out

    def test_full_skip_list_masks_exact_keys(self, pr_config, settings_guard):
        # 'app_id' is in the exact skip list (not caught by the partial list)
        settings_guard.set("config.app_id", "APP_ID_1234567")
        settings_guard.set("config.webhook_secret", "WEBHOOK_SECRET_VALUE")
        out = pr_config._prepare_pr_configs()
        assert "app_id" not in out.lower()
        assert "APP_ID_1234567" not in out
        assert "WEBHOOK_SECRET_VALUE" not in out

    def test_non_secret_custom_key_is_echoed(self, pr_config, settings_guard):
        settings_guard.set("config.fake_custom_flag", True)
        out = pr_config._prepare_pr_configs()
        assert "config.fake_custom_flag = True" in out

    def test_string_values_are_repr_quoted(self, pr_config, settings_guard):
        # string values must be repr()-ed (quoted) in the yaml block
        settings_guard.set("config.fake_custom_flag", "hello world")
        out = pr_config._prepare_pr_configs()
        assert "config.fake_custom_flag = 'hello world'" in out


class TestRun:
    async def test_run_publishes_comment_when_publish_output(self, pr_config, settings_guard):
        settings_guard.set("config.publish_output", True)
        result = await pr_config.run()
        assert result == ""
        pr_config.git_provider.publish_comment.assert_called_once()
        published = pr_config.git_provider.publish_comment.call_args[0][0]
        assert "PR-Agent Configurations" in published
        pr_config.git_provider.remove_initial_comment.assert_called_once()

    async def test_run_does_not_publish_when_publish_output_disabled(self, pr_config, settings_guard):
        settings_guard.set("config.publish_output", False)
        result = await pr_config.run()
        assert result == ""
        pr_config.git_provider.publish_comment.assert_not_called()
        pr_config.git_provider.remove_initial_comment.assert_not_called()
