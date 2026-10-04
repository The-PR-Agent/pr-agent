from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import (
    IncompleteBitbucketPullRequestFilesError,
    IncompletePullRequestFilesError,
)
from pr_agent.tools.pr_generate_labels import PRGenerateLabels


def _tool(provider):
    """``__init__`` builds a provider and a token handler, so wire up only what ``run`` reads."""
    tool = PRGenerateLabels.__new__(PRGenerateLabels)
    tool.git_provider = provider
    tool.pr_id = "owner/repo/1"
    tool.prediction = "labels: []"
    tool.data = {"labels": []}
    tool._prepare_data = MagicMock()
    tool._prepare_labels = MagicMock(return_value=["Bug fix"])
    return tool


def _provider(supports_labels):
    provider = MagicMock()
    provider.is_supported.side_effect = lambda capability: supports_labels
    if not supports_labels:
        # Gerrit and the local provider raise instead of returning an empty list.
        provider.get_pr_labels.side_effect = NotImplementedError("Getting labels is not implemented")
    else:
        provider.get_pr_labels.return_value = []
    return provider


@pytest.mark.asyncio
@patch("pr_agent.tools.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.tools.pr_generate_labels.get_settings")
async def test_labels_are_published_as_a_comment_when_the_provider_has_no_label_api(
        mock_get_settings, _mock_retry):
    """Regression for #2259: ``get_pr_labels`` was called before the ``get_labels`` capability
    check, so ``/generate_labels`` died on Gerrit and the local provider instead of falling
    back to the comment path."""
    mock_get_settings.return_value.config.publish_output = True
    provider = _provider(supports_labels=False)
    tool = _tool(provider)

    await tool.run()

    provider.get_pr_labels.assert_not_called()
    provider.publish_labels.assert_not_called()
    published = [call.args[0] for call in provider.publish_comment.call_args_list]
    assert any("PR Labels" in body and "Bug fix" in body for body in published)
    provider.remove_initial_comment.assert_called_once()


@pytest.mark.asyncio
@patch("pr_agent.tools.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.tools.pr_generate_labels.get_settings")
async def test_user_labels_are_still_preserved_when_the_provider_supports_labels(
        mock_get_settings, _mock_retry):
    mock_get_settings.return_value.config.publish_output = True
    provider = _provider(supports_labels=True)
    provider.get_pr_labels.return_value = ["Review effort 3/5"]
    tool = _tool(provider)

    with patch("pr_agent.tools.pr_generate_labels.get_user_labels", return_value=["Review effort 3/5"]):
        await tool.run()

    provider.get_pr_labels.assert_called_once()
    provider.publish_labels.assert_called_once_with(["Bug fix", "Review effort 3/5"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "incomplete_error_class", [IncompleteBitbucketPullRequestFilesError, IncompletePullRequestFilesError]
)
async def test_incomplete_provider_diff_is_re_raised_after_temporary_comment_cleanup(
    monkeypatch, incomplete_error_class
):
    async def fail_with_incomplete_diff(*_args, **_kwargs):
        raise incomplete_error_class("incomplete aggregate diff")

    provider = _provider(supports_labels=True)
    tool = _tool(provider)
    settings = get_settings()
    previous = {
        "publish_output": settings.config.publish_output,
        "propagate_tool_errors": settings.config.get("propagate_tool_errors", False),
    }
    settings.config.publish_output = True
    settings.set("config.propagate_tool_errors", False)
    monkeypatch.setattr("pr_agent.tools.pr_generate_labels.retry_with_fallback_models", fail_with_incomplete_diff)
    try:
        with pytest.raises(incomplete_error_class):
            await tool.run()
    finally:
        settings.config.publish_output = previous["publish_output"]
        settings.set("config.propagate_tool_errors", previous["propagate_tool_errors"])

    provider.remove_initial_comment.assert_called_once()
