from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.run_details import command_failed, init_run_details
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
@pytest.mark.parametrize(
    ("initial_labels", "updated_labels"),
    [
        (["Review effort 3/5"], ["Review effort 3/5", "triaged"]),
        (["Review effort 3/5", "triaged"], ["Review effort 3/5"]),
    ],
    ids=["user-added-label", "user-removed-label"],
)
@patch("pr_agent.tools.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.tools.pr_generate_labels.get_settings")
async def test_user_label_changes_during_prediction_are_preserved(
        mock_get_settings, mock_retry, initial_labels, updated_labels):
    mock_get_settings.return_value.config.publish_output = True
    provider = _provider(supports_labels=True)
    current_labels = initial_labels

    async def change_user_labels(*_args, **_kwargs):
        nonlocal current_labels
        current_labels = updated_labels

    mock_retry.side_effect = change_user_labels
    provider.get_pr_labels.side_effect = lambda update=False: current_labels if update else initial_labels
    tool = _tool(provider)

    with patch("pr_agent.tools.pr_generate_labels.get_user_labels", side_effect=lambda labels: labels):
        await tool.run()

    provider.publish_labels.assert_called_once_with(["Bug fix", *updated_labels])
    provider.get_pr_labels.assert_called_once_with(update=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "write"])
@pytest.mark.parametrize("propagate_errors", [False, True])
@patch("pr_agent.tools.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.tools.pr_generate_labels.get_settings")
async def test_primary_labels_failure_records_status_and_cleans_up(
        mock_get_settings, _mock_retry, failure, propagate_errors):
    mock_get_settings.return_value.config.publish_output = True
    mock_get_settings.return_value.config.get.return_value = propagate_errors
    provider = _provider(supports_labels=True)
    if failure == "read":
        provider.get_pr_labels.return_value = None
    else:
        provider.publish_labels.return_value = False
    init_run_details()

    if propagate_errors:
        with pytest.raises(RuntimeError, match="labels"):
            await _tool(provider).run()
    else:
        assert await _tool(provider).run() == ""

    assert command_failed() is True
    provider.get_pr_labels.assert_called_once_with(update=True)
    if failure == "read":
        provider.publish_labels.assert_not_called()
    else:
        provider.publish_labels.assert_called_once_with(["Bug fix"])
    provider.remove_initial_comment.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledgement", [True, None, 0])
@patch("pr_agent.tools.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.tools.pr_generate_labels.get_settings")
async def test_only_exact_false_label_acknowledgement_records_failure(
        mock_get_settings, _mock_retry, acknowledgement):
    mock_get_settings.return_value.config.publish_output = True
    mock_get_settings.return_value.config.get.return_value = True
    provider = _provider(supports_labels=True)
    provider.publish_labels.return_value = acknowledgement
    init_run_details()

    assert await _tool(provider).run() == ""

    assert command_failed() is False
    provider.publish_labels.assert_called_once_with(["Bug fix"])
    provider.remove_initial_comment.assert_called_once()


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


@pytest.mark.asyncio
async def test_run_skips_without_recording_failure_when_all_changed_files_are_filtered(monkeypatch):
    async def prepare_once(prepare_fn, **_kwargs):
        await prepare_fn("primary-model")

    provider = _provider(supports_labels=True)
    tool = _tool(provider)
    tool.ai_handler = MagicMock()
    tool.vars = {"diff": ""}
    tool._get_prediction = AsyncMock()
    monkeypatch.setattr("pr_agent.tools.pr_generate_labels.retry_with_fallback_models", prepare_once)
    monkeypatch.setattr("pr_agent.tools.pr_generate_labels.set_custom_labels", lambda *_args: None)
    monkeypatch.setattr(
        "pr_agent.tools.pr_generate_labels.AttemptTokenBudget.for_prompt_attempt",
        lambda *_args, **_kwargs: MagicMock(),
    )
    # A lockfile-only or asset-only PR has no diff left after file-type filtering.
    monkeypatch.setattr("pr_agent.tools.pr_generate_labels.get_pr_diff", lambda *_args, **_kwargs: "")
    settings = get_settings()
    previous = {
        "publish_output": settings.config.publish_output,
        "propagate_tool_errors": settings.config.get("propagate_tool_errors", False),
    }
    settings.config.publish_output = True
    settings.set("config.propagate_tool_errors", False)
    init_run_details()
    try:
        await tool.run()
    finally:
        settings.config.publish_output = previous["publish_output"]
        settings.set("config.propagate_tool_errors", previous["propagate_tool_errors"])

    assert command_failed() is False
    tool._get_prediction.assert_not_awaited()
    provider.publish_comment.assert_called_once_with("Preparing PR labels...", is_temporary=True)
    provider.publish_labels.assert_not_called()
    provider.remove_initial_comment.assert_called_once()
