from unittest.mock import MagicMock, call

import pytest

from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider


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
