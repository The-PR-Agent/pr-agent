import io
from unittest.mock import MagicMock, call

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.log import get_logger


def _suggestion(start: int = 2, end: int = 2) -> dict:
    return {
        "body": "```suggestion\nnew\n```",
        "relevant_file": "app.py",
        "relevant_lines_start": start,
        "relevant_lines_end": end,
    }


def test_publish_code_suggestions_runs_the_shared_template():
    provider = BitbucketProvider.__new__(BitbucketProvider)
    suggestions = [_suggestion(), _suggestion(-1), _suggestion(4, 5)]
    prepared = [_suggestion(), _suggestion(-1), _suggestion(4, 5)]
    payload = {"body": "first", "path": "app.py", "line": 2, "side": "RIGHT"}
    provider._prepare_code_suggestions = MagicMock(return_value=prepared)
    provider._prepare_code_suggestion = MagicMock(side_effect=[prepared[0], prepared[1], None])
    provider._build_code_suggestion_payload = MagicMock(return_value=payload)
    provider._log_invalid_code_suggestion = MagicMock()
    provider.publish_inline_comments = MagicMock(return_value=object())

    result = GitProvider.publish_code_suggestions(provider, suggestions)

    assert result is True
    provider._prepare_code_suggestions.assert_called_once_with(suggestions)
    assert provider._prepare_code_suggestion.call_args_list == [
        call(prepared[0]),
        call(prepared[1]),
        call(prepared[2]),
    ]
    provider._build_code_suggestion_payload.assert_called_once_with(prepared[0])
    provider.publish_inline_comments.assert_called_once_with([payload])
    provider._log_invalid_code_suggestion.assert_called_once_with(
        "Failed to publish code suggestion, relevant_lines_start is -1"
    )


def test_publish_code_suggestions_uses_the_provider_error_policy():
    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider._prepare_code_suggestions = MagicMock(return_value=[_suggestion()])
    provider._build_code_suggestion_payload = MagicMock(return_value={"body": "payload"})
    provider.publish_inline_comments = MagicMock(side_effect=RuntimeError("network down"))
    provider._code_suggestion_publish_exceptions = (RuntimeError,)
    provider._log_code_suggestion_publish_error = MagicMock()

    result = GitProvider.publish_code_suggestions(provider, [_suggestion()])

    assert result is False
    provider._log_code_suggestion_publish_error.assert_called_once()
    assert str(provider._log_code_suggestion_publish_error.call_args.args[0]) == "network down"


@pytest.mark.parametrize("provider_type", [GithubProvider, BitbucketProvider, BitbucketServerProvider])
def test_target_providers_inherit_publish_code_suggestions(provider_type: type[GitProvider]):
    assert "publish_code_suggestions" not in provider_type.__dict__


@pytest.mark.parametrize(
    "provider_type", [GithubProvider, BitbucketProvider, BitbucketServerProvider]
)
def test_publish_error_is_reported_at_default_verbosity(provider_type: type[GitProvider]):
    """A publish failure has no signal other than this log line.

    `publish_code_suggestions` returns False and the run carries on, so a gated error
    leaves a reader with no trace that the suggestion set was never published, which
    looks the same as the model finding nothing. GithubProvider is listed because it
    does not override the helper, so it exercises the base implementation.

    verbosity_level is pinned to 0 rather than asserted to be 0, so this pins the
    behaviour at the quietest setting whatever the shipped default is.

    The sink is at INFO rather than DEBUG so the assertion is about the level the
    call uses: a debug() call would not reach it, so downgrading the log from
    error() to debug() fails here. That is about the call site, not about
    log_level, which ships as DEBUG and is a separate setting from the
    verbosity_level this change is about.
    """
    settings = get_settings(use_context=False)
    original = settings.get("config.verbosity_level", 0)
    settings.set("config.verbosity_level", 0)
    try:
        provider = provider_type.__new__(provider_type)
        buffer = io.StringIO()
        handler_id = get_logger().add(buffer, level="INFO", format="{message}", colorize=False)
        try:
            # Bound, so the provider's own override is what runs.
            provider._log_code_suggestion_publish_error(RuntimeError("network down"))
        finally:
            get_logger().remove(handler_id)
    finally:
        settings.set("config.verbosity_level", original)

    assert "network down" in buffer.getvalue()
