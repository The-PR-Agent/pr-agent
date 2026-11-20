"""Boolean settings must never reach a command as a string (#5047).

A quoted "false" in a comment argument or in a repository's .pr_agent.toml
arrives as the string "false", which a plain truthiness read treats as on.
#4823 fixed this for config.publish_output only; _normalize_boolean_settings
generalises it to every key whose default in configuration.toml is a boolean.
"""

import copy
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette_context import request_cycle_context

import pr_agent.agent.pr_agent as agent
from pr_agent.agent.pr_agent import _boolean_setting_defaults, _normalize_boolean_settings
from pr_agent.algo.cli_args import CliArgs
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers import _GIT_PROVIDERS

URL = "https://example.test/owner/repo/pull/1"
TOML_PATH = Path(agent.__file__).resolve().parent.parent / "settings" / "configuration.toml"


def _boolean_defaults() -> dict[str, bool]:
    """Boolean-default keys straight from configuration.toml, as Dynaconf paths."""
    with open(TOML_PATH, "rb") as config_file:
        configuration = tomllib.load(config_file)
    return {
        f"{section}.{key}".upper(): value
        for section, values in configuration.items()
        if isinstance(values, dict)
        for key, value in values.items()
        if isinstance(value, bool)
    }


def _settings():
    # Dynaconf loads lazily: deepcopying global_settings before any access
    # yields an empty settings object, so touch it first.
    _ = global_settings.as_dict()
    settings = copy.deepcopy(get_settings(use_context=False))
    # Other tests in the full suite mutate global_settings (e.g. leaving
    # CONFIG.IS_AUTO_COMMAND True); reset every boolean key to its TOML
    # default so the stub command starts from a known state.
    for key, default in _boolean_defaults().items():
        settings.set(key, default)
    settings.set("CONFIG.GIT_PROVIDER", "boolean-settings-test")
    settings.set("CONFIG.RESPONSE_LANGUAGE", "en-us")
    settings.set("CONFIG.REPO_CONTEXT_FILES", [])
    settings.set("CONFIG.PROPAGATE_COMMAND_ERRORS", False)
    settings.set("SKILLS.ENABLED", False)
    return settings


@pytest.fixture
def providers(monkeypatch):
    instances = {}

    def get_provider(url):
        if url not in instances:
            provider = Mock(pr=SimpleNamespace(title=url), pr_url=url)
            provider.get_repo_settings.return_value = ""
            provider.get_languages.return_value = {"Python": 1}
            provider.get_files.return_value = []
            provider.get_num_of_files.return_value = 0
            provider.get_pr_id.return_value = url
            provider.get_pr_branch.return_value = "feature"
            provider.get_commit_messages.return_value = ""
            provider.get_user_description.return_value = "Description"
            provider.get_pr_description.side_effect = lambda **kwargs: (
                ("Description", []) if kwargs.get("split_changes_walkthrough") else "Description"
            )
            provider.get_issue_comments_newest_first.return_value = []
            provider.supports_review_finding_state.return_value = False
            provider.supports_code_suggestion_state.return_value = False
            provider.supports_threaded_pr_questions.return_value = False
            provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
            instances[url] = provider
        return instances[url]

    monkeypatch.setitem(_GIT_PROVIDERS, "boolean-settings-test", get_provider)
    return get_provider


def _assert_no_string_booleans(captured, defaults):
    """Every boolean key reads False or its TOML default, never a string."""
    assert captured, "stub command never ran"
    not_false = {
        key: value for key, value in captured.items()
        if value is not False and value != defaults[key]
    }
    assert not not_false, f"boolean keys did not read False: {not_false}"
    strings = {key: value for key, value in captured.items() if isinstance(value, str)}
    assert not strings, f"boolean keys reached the command as strings: {strings}"


def test_normalize_boolean_settings_coerces_every_string_value():
    """Every boolean-default key set to "false" reads False afterwards."""
    defaults = _boolean_defaults()
    assert len(defaults) > 100, "boolean enumeration is unexpectedly small"
    assert defaults == _boolean_setting_defaults(), "implementation reads the same TOML source"
    with request_cycle_context({"settings": _settings()}):
        settings = get_settings()
        for key in defaults:
            settings.set(key, "false")
        _normalize_boolean_settings()
        failures = {key: settings.get(key) for key in defaults if settings.get(key) is not False}
        assert not failures, f"boolean keys still not False: {failures}"


def test_normalize_boolean_settings_keeps_true_strings_and_non_strings():
    """True-like strings coerce to True; non-boolean values stay untouched."""
    with request_cycle_context({"settings": _settings()}):
        settings = get_settings()
        settings.set("CONFIG.PUBLISH_OUTPUT", "true")
        settings.set("PR_REVIEWER.REQUIRE_TESTS_REVIEW", "yes")
        settings.set("PR_DESCRIPTION.PUBLISH_LABELS", "on")
        settings.set("PR_QUESTIONS.USE_CONVERSATION_HISTORY", "1")
        settings.set("PR_REVIEWER.REQUIRE_SCORE_REVIEW", 1)  # not a string: untouched
        settings.set("CONFIG.MODEL", "gpt-4o")  # not a boolean key: untouched
        settings.set("GITLAB.SSL_VERIFY", "/etc/ssl/ca.pem")  # no boolean default: untouched
        _normalize_boolean_settings()
        assert settings.config.publish_output is True
        assert settings.pr_reviewer.require_tests_review is True
        assert settings.pr_description.publish_labels is True
        assert settings.pr_questions.use_conversation_history is True
        assert settings.pr_reviewer.require_score_review == 1
        assert settings.config.model == "gpt-4o"
        assert settings.gitlab.ssl_verify == "/etc/ssl/ca.pem"


async def test_comment_args_false_strings_read_false_in_stub_command(monkeypatch, providers):
    """Issue repro: "false" through comment args reads False in the command."""
    defaults = _boolean_defaults()
    # Comment arguments cannot set host-only keys; the command would be
    # rejected outright, so only the valid subset travels this path. Host-only
    # keys must stay at their TOML default instead.
    settable = {}
    for key in defaults:
        section, leaf = key.lower().split(".", 1)
        valid, _ = CliArgs.validate_user_args([f"--{section}.{leaf}"])
        if valid:
            settable[key] = defaults[key]
    assert settable, "no boolean key is comment-settable"
    assert len(settable) < len(defaults), "expected some host-only boolean keys"

    captured = {}

    async def run(command):
        captured.update({key: get_settings().get(key) for key in defaults})

    monkeypatch.setattr(agent.PRReviewer, "run", run)
    request = "/review " + " ".join(
        f'--{key.lower().split(".", 1)[0]}.{key.lower().split(".", 1)[1]}="false"' for key in settable
    )
    with request_cycle_context({"settings": _settings()}):
        assert await agent.PRAgent().handle_request(URL, request) is True
    _assert_no_string_booleans(captured, defaults)


async def test_repo_settings_false_strings_read_false_in_stub_command(monkeypatch, providers):
    """Issue repro: "false" through .pr_agent.toml reads False in the command."""
    defaults = _boolean_defaults()
    by_section = {}
    for key in defaults:
        section, leaf = key.lower().split(".", 1)
        by_section.setdefault(section, []).append(leaf)
    repo_toml = "".join(
        f"[{section}]\n" + "".join(f'{leaf} = "false"\n' for leaf in leaves)
        for section, leaves in sorted(by_section.items())
    )
    providers(URL).get_repo_settings.return_value = repo_toml

    captured = {}

    async def run(command):
        captured.update({key: get_settings().get(key) for key in defaults})

    monkeypatch.setattr(agent.PRReviewer, "run", run)
    with request_cycle_context({"settings": _settings()}):
        assert await agent.PRAgent().handle_request(URL, "/review") is True
    _assert_no_string_booleans(captured, defaults)
