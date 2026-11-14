"""Verify request-scoped protection of documentation suggestions in GitHub fallback logs."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException
from requests.exceptions import RequestException

import pr_agent.git_providers.github_provider as github_module
from pr_agent.algo.ai_handlers.litellm_ai_handler import withhold_prompt_logging

SENSITIVE_BODY = "Sensitive repository instructions echoed by generated documentation"


@pytest.mark.parametrize("withhold", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_github_fallback_logs_withhold_suggestion_bodies(monkeypatch, withhold, fail):
    logger = MagicMock()
    monkeypatch.setattr(github_module, "get_logger", lambda: logger)
    monkeypatch.setattr(
        github_module,
        "get_settings",
        lambda: SimpleNamespace(github=SimpleNamespace(try_fix_invalid_inline_comments=True)),
    )
    comment = {"path": "src/example.py", "line": 1, "body": SENSITIVE_BODY}
    invalid = [(comment, RequestException("invalid anchor"))]
    publish = MagicMock(side_effect=RequestException(SENSITIVE_BODY)) if fail else MagicMock(return_value=True)
    provider = SimpleNamespace(
        _verify_code_comments=MagicMock(return_value=([], invalid)),
        _try_fix_invalid_inline_comments=MagicMock(return_value=[comment]),
        publish_inline_comments=publish,
    )

    with withhold_prompt_logging(withhold):
        count = github_module.GithubProvider._publish_inline_comments_fallback_with_verification(provider, [comment])

    assert count == (0 if fail else 1)
    publish.assert_called_once_with([comment], disable_fallback=True)
    log_text = str(logger.mock_calls)
    assert (SENSITIVE_BODY in log_text) is not withhold
    if fail:
        logger.error.assert_called_once()
    else:
        logger.info.assert_called_once()


@pytest.mark.parametrize("withhold", [False, True])
def test_github_batch_fallback_error_withholds_generated_content(monkeypatch, withhold):
    logger = MagicMock()
    monkeypatch.setattr(github_module, "get_logger", lambda: logger)
    monkeypatch.setattr(
        github_module, "get_settings", lambda: SimpleNamespace(get=lambda _key, default=None: default)
    )
    provider = github_module.GithubProvider.__new__(github_module.GithubProvider)
    provider.pr = SimpleNamespace(create_review=MagicMock(side_effect=GithubException(422, "invalid anchor")))
    provider.last_commit_id = "test-commit"
    provider._publish_inline_comments_fallback_with_verification = MagicMock(
        side_effect=RuntimeError(SENSITIVE_BODY)
    )
    comment = {"path": "src/example.py", "line": 1, "body": SENSITIVE_BODY}

    with withhold_prompt_logging(withhold):
        with pytest.raises(RuntimeError, match=SENSITIVE_BODY):
            provider.publish_inline_comments([comment])

    provider._publish_inline_comments_fallback_with_verification.assert_called_once_with([comment])
    assert (SENSITIVE_BODY in str(logger.mock_calls)) is not withhold
    if withhold:
        assert "RuntimeError at " in str(logger.mock_calls)
        assert "details withheld" in str(logger.mock_calls)
