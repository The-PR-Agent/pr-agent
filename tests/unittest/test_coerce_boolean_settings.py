import copy

import pytest
from starlette_context import context, request_cycle_context

from pr_agent.agent.pr_agent import (
    _get_boolean_setting_defaults,
    _normalize_boolean_settings,
    _normalize_publish_output_setting,
    prepare_command,
)
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import global_settings
from pr_agent.git_providers import utils as git_utils


class FakeGitProvider:
    def __init__(self, repo_settings: bytes):
        self._repo_settings = repo_settings

    def get_repo_settings(self):
        return self._repo_settings

    def is_supported(self, feature):
        return False

    def publish_comment(self, body):
        pass

    def publish_persistent_comment(self, *args, **kwargs):
        pass


@pytest.fixture
def fresh_global_settings():
    """Restore module-level global_settings after each test."""
    snapshot = copy.deepcopy(global_settings.as_dict())
    yield
    for section in set(global_settings.as_dict().keys()) - set(snapshot.keys()):
        global_settings.unset(section)
    for section, contents in snapshot.items():
        global_settings.unset(section)
        global_settings.set(section, copy.deepcopy(contents), merge=False)


def test_boolean_setting_defaults_loaded():
    """Verify that boolean setting defaults are extracted from configuration.toml."""
    defaults = _get_boolean_setting_defaults()
    assert len(defaults) > 100
    assert defaults["config.publish_output"] is True
    assert defaults["config.use_extra_bad_extensions"] is False
    assert defaults["pr_reviewer.require_tests_review"] is True
    assert defaults["pr_reviewer.require_score_review"] is False
    assert defaults["pr_description.publish_labels"] is False
    assert defaults["pr_questions.use_conversation_history"] is True
    # Keys without a boolean default must not be present
    assert "gitlab.ssl_verify" not in defaults
    assert "config.response_language" not in defaults


def test_direct_normalize_boolean_coercion(fresh_global_settings):
    """Verify that _normalize_boolean_settings coerces string representations to bools."""
    with request_cycle_context({}):
        settings = copy.deepcopy(global_settings)
        context["settings"] = settings

        # Set string values for boolean settings
        settings.set("PR_REVIEWER.REQUIRE_TESTS_REVIEW", "false")
        settings.set("PR_REVIEWER.REQUIRE_SCORE_REVIEW", "true")
        settings.set("PR_DESCRIPTION.PUBLISH_LABELS", "0")
        settings.set("PR_DESCRIPTION.GENERATE_AI_TITLE", "1")
        settings.set("PR_QUESTIONS.USE_CONVERSATION_HISTORY", "no")
        settings.set("PR_CODE_SUGGESTIONS.FOCUS_ONLY_ON_PROBLEMS", "off")

        # Non-boolean setting should not be touched
        settings.set("GITLAB.SSL_VERIFY", "/etc/ssl/certs/custom-ca.pem")
        settings.set("CONFIG.RESPONSE_LANGUAGE", "de")

        _normalize_boolean_settings()

        assert settings.pr_reviewer.require_tests_review is False
        assert settings.pr_reviewer.require_score_review is True
        assert settings.pr_description.publish_labels is False
        assert settings.pr_description.generate_ai_title is True
        assert settings.pr_questions.use_conversation_history is False
        assert settings.pr_code_suggestions.focus_only_on_problems is False

        # Untouched
        assert settings.gitlab.ssl_verify == "/etc/ssl/certs/custom-ca.pem"
        assert settings.config.response_language == "de"


def test_normalize_publish_output_setting_alias(fresh_global_settings):
    """Verify backwards-compatibility alias _normalize_publish_output_setting."""
    with request_cycle_context({}):
        settings = copy.deepcopy(global_settings)
        context["settings"] = settings

        settings.set("CONFIG.PUBLISH_OUTPUT", "false")
        settings.set("PR_REVIEWER.REQUIRE_TESTS_REVIEW", "false")

        _normalize_publish_output_setting()

        assert settings.config.publish_output is False
        assert settings.pr_reviewer.require_tests_review is False


def test_invalid_string_boolean_setting_falls_back_to_default(fresh_global_settings):
    """Invalid string values should fall back to their configuration.toml default."""
    with request_cycle_context({}):
        settings = copy.deepcopy(global_settings)
        context["settings"] = settings

        # Defaults: require_tests_review is True, require_score_review is False
        settings.set("PR_REVIEWER.REQUIRE_TESTS_REVIEW", "not_a_valid_bool")
        settings.set("PR_REVIEWER.REQUIRE_SCORE_REVIEW", "unknown_val")

        _normalize_boolean_settings()

        assert settings.pr_reviewer.require_tests_review is True
        assert settings.pr_reviewer.require_score_review is False


def test_comment_arguments_boolean_coercion(fresh_global_settings):
    """Quoted comment arguments passed to update_settings_from_args should be coerced."""
    with request_cycle_context({}):
        settings = copy.deepcopy(global_settings)
        context["settings"] = settings

        args = [
            '--pr_reviewer.require_tests_review="false"',
            '--pr_description.publish_labels="false"',
            '--pr_code_suggestions.focus_only_on_problems="false"',
            '--pr_questions.use_conversation_history="false"',
            '--pr_reviewer.require_score_review="true"',
        ]
        update_settings_from_args(args)
        _normalize_boolean_settings()

        assert settings.pr_reviewer.require_tests_review is False
        assert settings.pr_description.publish_labels is False
        assert settings.pr_code_suggestions.focus_only_on_problems is False
        assert settings.pr_questions.use_conversation_history is False
        assert settings.pr_reviewer.require_score_review is True


def test_prepare_command_normalizes_boolean_settings(fresh_global_settings):
    """prepare_command should coerce boolean argument settings."""
    with request_cycle_context({}):
        settings = copy.deepcopy(global_settings)
        context["settings"] = settings

        command = '/review --pr_reviewer.require_tests_review="false" --pr_reviewer.require_score_review="true"'
        prepare_command(command)

        assert settings.pr_reviewer.require_tests_review is False
        assert settings.pr_reviewer.require_score_review is True


def test_repo_config_file_boolean_coercion(fresh_global_settings, monkeypatch):
    """A .pr_agent.toml file containing quoted string booleans should be coerced."""
    repo_toml = b"""
[pr_reviewer]
require_tests_review = "false"
require_score_review = "true"

[pr_description]
publish_labels = "false"
generate_ai_title = "true"

[pr_code_suggestions]
focus_only_on_problems = "false"
enable_help_text = "true"

[pr_questions]
use_conversation_history = "false"
enable_help_text = "true"

[extra_section]
custom_text = "kept_as_is"
"""
    provider = FakeGitProvider(repo_toml)
    monkeypatch.setattr(
        "pr_agent.git_providers.utils.get_git_provider_with_context",
        lambda url: provider,
    )

    with request_cycle_context({}):
        settings = copy.deepcopy(global_settings)
        context["settings"] = settings

        # Precondition check: defaults in configuration.toml
        assert settings.pr_reviewer.require_tests_review is True
        assert settings.pr_reviewer.require_score_review is False

        # Apply repo settings followed by normalization (matching PRAgent._run_command)
        git_utils.apply_repo_settings("https://git.example/projects/A/pull-requests/1")
        _normalize_boolean_settings()

        # Verified coerced booleans
        assert settings.pr_reviewer.require_tests_review is False
        assert settings.pr_reviewer.require_score_review is True
        assert settings.pr_description.publish_labels is False
        assert settings.pr_description.generate_ai_title is True
        assert settings.pr_code_suggestions.focus_only_on_problems is False
        assert settings.pr_code_suggestions.enable_help_text is True
        assert settings.pr_questions.use_conversation_history is False
        assert settings.pr_questions.enable_help_text is True

        # Non-boolean setting in custom section preserved as string
        assert settings.get("extra_section.custom_text") == "kept_as_is"
