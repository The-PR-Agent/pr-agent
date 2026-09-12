import copy
import sys
import tomllib
from contextlib import suppress
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.algo.cli_args import CliArgs
from pr_agent.algo.review_model_selection import (
    ReviewModelSelection,
    ReviewModelSelectionConfig,
    ReviewModelSelectionError,
    parse_review_model_selection,
)
from pr_agent.config_loader import get_settings
from pr_agent.servers.help import HelpMessage


class _Settings(ReviewModelSelectionConfig):
    def __init__(self, *, enabled=True, aliases=None):
        super().__init__(
            enabled=enabled,
            aliases=aliases if aliases is not None else {
                "fable": "anthropic/claude-fable-5",
                "terra": "gpt-5.6-terra",
            },
        )


def _snapshot_sections(*names):
    settings_dict = get_settings().as_dict()
    return {name: copy.deepcopy(settings_dict.get(name)) for name in names}


def _restore_sections(snapshot):
    settings = get_settings()
    for name, value in snapshot.items():
        with suppress(KeyError):
            settings.unset(name, force=True)
        if value is not None:
            settings.set(name, value, merge=False)


def _replace_section_values(section_name, **values):
    settings = get_settings()
    section = copy.deepcopy(settings.as_dict().get(section_name, {}))
    for key, value in values.items():
        for stored_key in list(section):
            if stored_key.lower() == key.lower():
                section.pop(stored_key)
        section[key.lower()] = value
    with suppress(KeyError):
        settings.unset(section_name, force=True)
    settings.set(section_name, section, merge=False)


def test_shipped_aliases_match_the_trusted_default_mapping():
    config_path = Path(__file__).parents[2] / "pr_agent" / "settings" / "configuration.toml"
    with config_path.open("rb") as config_file:
        configuration = tomllib.load(config_file)

    assert configuration["pr_reviewer"]["enable_command_model_aliases"] is False
    assert configuration["pr_reviewer"]["command_model_aliases"] == {
        "fable": "anthropic/claude-fable-5",
        "opus": "anthropic/claude-opus-5",
        "sonnet": "anthropic/claude-sonnet-5",
        "sol": "gpt-5.6-sol",
        "terra": "gpt-5.6-terra",
        "luna": "gpt-5.6-luna",
    }


def test_review_without_selector_does_not_read_alias_configuration():
    class _UnusableSettings:
        def __getattribute__(self, _name):
            raise AttributeError("ordinary /review must not inspect alias configuration")

    selection, remaining_args = parse_review_model_selection(["-i", "legacy-arg"], _UnusableSettings())

    assert selection is None
    assert remaining_args == ["-i", "legacy-arg"]


@pytest.mark.parametrize("settings", [_Settings(enabled=False), _Settings()])
def test_non_selector_plus_tokens_keep_their_historical_meaning(settings):
    args = ["please", "check", "C++", "a+b", "https://example.test/C+++guidelines"]

    selection, remaining_args = parse_review_model_selection(args, settings)

    assert selection is None
    assert remaining_args == args


@pytest.mark.parametrize(
    "arg",
    [
        "cost+low",
        "docs+minimal",
        "https://example.test/search?q=cost+low",
    ],
)
def test_disabled_aliases_preserve_unconfigured_plus_effort_text(arg):
    selection, remaining_args = parse_review_model_selection([arg], _Settings(enabled=False))

    assert selection is None
    assert remaining_args == [arg]


def test_one_selector_resolves_model_and_effort():
    selection, remaining_args = parse_review_model_selection(
        ["fable+high", "-i"], _Settings()
    )

    assert selection == ReviewModelSelection(
        alias="fable",
        model="anthropic/claude-fable-5",
        reasoning_effort="high",
    )
    assert remaining_args == ["-i"]


@pytest.mark.parametrize(
    ("args", "settings", "message"),
    [
        (["fable+high"], _Settings(enabled=False), "aliases are disabled"),
        (["unknown+high"], _Settings(), "Unknown model alias"),
        (["fable++high"], _Settings(), "Malformed model selector"),
        (["fable+extreme"], _Settings(), "Unsupported reasoning effort"),
        (["anthropic/claude-fable-5+high"], _Settings(), "Raw model identifier"),
        (["fable+high", "terra+low"], _Settings(), "Only one model selector"),
        (
            ["fable+high"],
            _Settings(aliases={"BAD ALIAS!": "anthropic/claude-fable-5"}),
            "invalid",
        ),
    ],
)
def test_invalid_selectors_are_rejected(args, settings, message):
    with pytest.raises(ReviewModelSelectionError, match=message):
        parse_review_model_selection(args, settings)


@pytest.mark.parametrize(
    "arg",
    [
        "--pr_reviewer.enable_command_model_aliases=true",
        '--pr_reviewer.command_model_aliases={"expensive": "provider/model"}',
        "--pr_reviewer__enable_command_model_aliases=true",
        "--pr_reviewer__command_model_aliases.expensive=provider/model",
    ],
)
def test_comment_configuration_cannot_change_operator_alias_controls(arg):
    is_valid, forbidden_arg = CliArgs.validate_user_args([arg])

    assert is_valid is False
    assert forbidden_arg


def test_help_documents_one_selector_without_a_custom_fallback_chain():
    help_text = HelpMessage.get_review_usage_guide()

    assert "/review fable+high" in help_text
    assert "one `alias+effort` selector" in help_text
    assert "later selectors are fallbacks" not in help_text


@pytest.mark.asyncio
async def test_review_without_selector_uses_existing_constructor_and_settings(monkeypatch):
    snapshot = _snapshot_sections("CONFIG")
    reviewer_calls = []

    class _Reviewer:
        def __init__(self, pr_url, ai_handler, args):
            reviewer_calls.append((pr_url, ai_handler, args))

        async def run(self):
            pass

    try:
        _replace_section_values(
            "CONFIG",
            RESPONSE_LANGUAGE="en-us",
            MODEL="configured/model",
            REASONING_EFFORT="medium",
        )
        monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda _pr_url: None)
        monkeypatch.setitem(pr_agent_module.command2class, "review", _Reviewer)

        handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
            "https://example/pr/1", "/review -i"
        )

        assert handled is True
        assert reviewer_calls == [("https://example/pr/1", "fake-ai", ["-i"])]
        assert get_settings().config.model == "configured/model"
        assert get_settings().config.reasoning_effort == "medium"
    finally:
        _restore_sections(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "constructor", "run"])
@pytest.mark.parametrize("initial_values_present", [True, False])
async def test_selector_settings_are_scoped_to_one_command(monkeypatch, failure, initial_values_present):
    snapshot = _snapshot_sections("CONFIG", "PR_REVIEWER", "MODEL_ROUTING")
    reviewer_calls = []

    selected_values = []
    expected_keys = (
        "CONFIG.MODEL", "CONFIG.REASONING_EFFORT",
        "MODEL_ROUTING.ENABLE", "CONFIG.ENABLE_CLAUDE_ADAPTIVE_THINKING",
    )

    class _Reviewer:
        def __init__(self, pr_url, ai_handler, args):
            reviewer_calls.append((pr_url, ai_handler, args))
            selected_values.append([get_settings().get(key) for key in expected_keys])
            if failure == "constructor":
                raise RuntimeError("constructor failed")

        async def run(self):
            if failure == "run":
                raise RuntimeError("run failed")

    class _LaterCommand:
        def __init__(self, pr_url, ai_handler, args):
            selected_values.append([get_settings().get(key) for key in expected_keys])

        async def run(self):
            pass

    try:
        _replace_section_values(
            "CONFIG",
            RESPONSE_LANGUAGE="en-us",
            MODEL="configured/model",
            REASONING_EFFORT="medium",
            ENABLE_CLAUDE_ADAPTIVE_THINKING=False,
        )
        _replace_section_values(
            "PR_REVIEWER",
            ENABLE_COMMAND_MODEL_ALIASES=True,
            COMMAND_MODEL_ALIASES={"fable": "anthropic/claude-fable-5"},
            EXTRA_INSTRUCTIONS="before",
        )
        _replace_section_values("MODEL_ROUTING", ENABLE=True)
        if not initial_values_present:
            for key in expected_keys:
                section, name = key.split(".")
                section_values = get_settings().get(section)
                for stored_key in list(section_values):
                    if stored_key.upper() == name:
                        del section_values[stored_key]
            get_settings().unset("MODEL_ROUTING", force=True)
        originals = [get_settings().get(key) for key in expected_keys]
        monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda _pr_url: None)
        monkeypatch.setitem(pr_agent_module.command2class, "review", _Reviewer)

        handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
            "https://example/pr/1",
            "/review fable+high -i --pr_reviewer.extra_instructions=focused",
        )

        assert handled is (failure is None)
        assert reviewer_calls == [("https://example/pr/1", "fake-ai", ["-i"])]
        assert selected_values == [["anthropic/claude-fable-5", "high", False, True]]
        assert [get_settings().get(key) for key in expected_keys] == originals
        if not initial_values_present:
            assert "MODEL_ROUTING" not in get_settings().as_dict()
            for key in expected_keys:
                section, name = key.split(".")
                assert name not in get_settings().get(section, {})
        assert get_settings().pr_reviewer.extra_instructions == "focused"
        monkeypatch.setitem(pr_agent_module.command2class, "describe", _LaterCommand)
        assert await pr_agent_module.PRAgent()._handle_request("https://example/pr/1", "/describe")
        assert selected_values[-1] == originals
    finally:
        _restore_sections(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("include_error_details", [True, False])
async def test_invalid_selector_publishes_error_without_constructing_reviewer(monkeypatch, include_error_details):
    snapshot = _snapshot_sections("CONFIG", "PR_REVIEWER", "OTEL")
    published_comments = []

    class _Provider:
        def publish_comment(self, body):
            published_comments.append(body)

    class _Reviewer:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("invalid selectors must not construct the reviewer")

    try:
        _replace_section_values("CONFIG", RESPONSE_LANGUAGE="en-us")
        _replace_section_values(
            "PR_REVIEWER",
            ENABLE_COMMAND_MODEL_ALIASES=True,
            COMMAND_MODEL_ALIASES={"fable": "anthropic/claude-fable-5"},
        )
        monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda _pr_url: None)
        monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: _Provider())
        monkeypatch.setitem(pr_agent_module.command2class, "review", _Reviewer)

        _replace_section_values("OTEL", INCLUDE_ERROR_DETAILS=include_error_details)
        span = MagicMock()
        handled = await pr_agent_module.PRAgent()._run_command(
            "https://example/pr/1", "/review fable+high fable+low", None, span
        )
        span.set_status.assert_called_once_with(pr_agent_module.StatusCode.ERROR)
        span.set_attribute.assert_any_call("error.type", "invalid_model_selector")
        error_messages = [call.args[1] for call in span.set_attribute.call_args_list
                          if call.args[0] == "error.message"]
        assert bool(error_messages) is include_error_details
        if include_error_details:
            assert "Only one model selector" in error_messages[0]

        assert handled is False
        assert len(published_comments) == 1
        assert "Only one model selector" in published_comments[0]
    finally:
        _restore_sections(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "publish", "notify"])
async def test_selector_rejection_integration_failures_preserve_exception(monkeypatch, failure):
    snapshot = _snapshot_sections("CONFIG", "PR_REVIEWER")
    integration_error = RuntimeError("integration failed")
    logged_exceptions = []
    logger = MagicMock()
    logger.exception.side_effect = lambda *args, **kwargs: logged_exceptions.append((sys.exc_info(), kwargs))

    def fail(*args, **kwargs):
        raise integration_error

    provider = MagicMock()
    if failure == "publish":
        provider.publish_comment.side_effect = fail
    try:
        _replace_section_values("CONFIG", RESPONSE_LANGUAGE="en-us")
        _replace_section_values("PR_REVIEWER", ENABLE_COMMAND_MODEL_ALIASES=True,
                                COMMAND_MODEL_ALIASES={"fable": "anthropic/claude-fable-5"})
        monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda _pr_url: None)
        monkeypatch.setattr(pr_agent_module, "get_logger", lambda: logger)
        monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context",
                            fail if failure == "provider" else lambda _pr_url: provider)
        handled = await pr_agent_module.PRAgent()._run_command(
            "https://example/pr/1", "/review fable+high fable+low",
            fail if failure == "notify" else None, MagicMock(),
        )
        assert handled is False
        assert len(logged_exceptions) == 1
        exception_info, kwargs = logged_exceptions[0]
        assert exception_info[1] is integration_error
        assert exception_info[2] is not None
        if failure != "notify":
            assert kwargs["artifact"]["error"] is integration_error
    finally:
        _restore_sections(snapshot)
