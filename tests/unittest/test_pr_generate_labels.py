import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.review_json_output import review_json_output_path
from pr_agent.algo.run_details import command_failed, init_run_details
from pr_agent.commands.pr_generate_labels import PRGenerateLabels
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import (
    IncompleteBitbucketPullRequestFilesError,
    IncompletePullRequestFilesError,
)


def _command(provider):
    """``__init__`` builds a provider and a token handler, so wire up only what ``run`` reads."""
    command = PRGenerateLabels.__new__(PRGenerateLabels)
    command.git_provider = provider
    command.pr_id = "owner/repo/1"
    command.prediction = "labels: []"
    command.data = {"labels": []}
    command._prepare_data = MagicMock()
    command._prepare_labels = MagicMock(return_value=["Bug fix"])
    return command


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
@patch("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.commands.pr_generate_labels.get_settings")
async def test_labels_are_published_as_a_comment_when_the_provider_has_no_label_api(
        mock_get_settings, _mock_retry):
    """Regression for #2259: ``get_pr_labels`` was called before the ``get_labels`` capability
    check, so ``/generate_labels`` died on Gerrit and the local provider instead of falling
    back to the comment path."""
    mock_get_settings.return_value.config.publish_output = True
    provider = _provider(supports_labels=False)
    command = _command(provider)

    await command.run()

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
@patch("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.commands.pr_generate_labels.get_settings")
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
    command = _command(provider)

    with patch("pr_agent.commands.pr_generate_labels.get_user_labels", side_effect=lambda labels: labels):
        await command.run()

    provider.publish_labels.assert_called_once_with(["Bug fix", *updated_labels])
    provider.get_pr_labels.assert_called_once_with(update=True)


@pytest.mark.asyncio
@patch("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.commands.pr_generate_labels.get_settings")
async def test_json_contains_generated_labels_without_existing_user_labels(
        mock_get_settings, _mock_retry, tmp_path):
    mock_get_settings.return_value.config.publish_output = True
    provider = _provider(supports_labels=True)
    provider.get_pr_labels.return_value = ["triaged"]
    command = _command(provider)
    command.data = {"labels": ["Bug fix", "invalid-custom-label"]}
    init_run_details()
    output = tmp_path / "labels.json"
    token = review_json_output_path.set(str(output))
    try:
        with patch("pr_agent.commands.pr_generate_labels.get_user_labels", return_value=["triaged"]):
            await command.run()
    finally:
        review_json_output_path.reset(token)

    assert json.loads(output.read_text(encoding="utf-8")) == {"labels": ["Bug fix"], "usage": {}}
    provider.publish_labels.assert_called_once_with(["Bug fix", "triaged"])


@pytest.mark.asyncio
@patch("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.commands.pr_generate_labels.get_settings")
async def test_json_writes_empty_labels_when_generated_values_are_rejected(
        mock_get_settings, _mock_retry, tmp_path):
    mock_get_settings.return_value.config.publish_output = True
    provider = _provider(supports_labels=True)
    command = _command(provider)
    command.data = {"labels": ["unrecognized-label"]}
    command._prepare_labels.return_value = []
    output = tmp_path / "labels.json"
    token = review_json_output_path.set(str(output))
    try:
        await command.run()
    finally:
        review_json_output_path.reset(token)

    assert json.loads(output.read_text(encoding="utf-8")) == {"labels": [], "usage": {}}
    provider.get_pr_labels.assert_not_called()
    provider.publish_labels.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "write"])
@patch("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.commands.pr_generate_labels.get_settings")
async def test_json_is_available_when_provider_label_publication_fails(
        mock_get_settings, _mock_retry, failure, tmp_path):
    mock_get_settings.return_value.config.publish_output = True
    provider = _provider(supports_labels=True)
    if failure == "read":
        provider.get_pr_labels.return_value = None
    else:
        provider.publish_labels.return_value = False
    output = tmp_path / "labels.json"
    init_run_details()
    token = review_json_output_path.set(str(output))
    try:
        with pytest.raises(RuntimeError, match="labels"):
            await _command(provider).run()
    finally:
        review_json_output_path.reset(token)

    assert json.loads(output.read_text(encoding="utf-8")) == {"labels": ["Bug fix"], "usage": {}}
    assert command_failed() is True
    provider.remove_initial_comment.assert_called_once()


@pytest.mark.asyncio
@patch("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.commands.pr_generate_labels.get_settings")
async def test_json_write_failure_does_not_publish_permanent_labels(
        mock_get_settings, _mock_retry, tmp_path):
    mock_get_settings.return_value.config.publish_output = True
    provider = _provider(supports_labels=True)
    output = tmp_path / "missing" / "labels.json"
    init_run_details()
    token = review_json_output_path.set(str(output))
    try:
        with pytest.raises(OSError):
            await _command(provider).run()
    finally:
        review_json_output_path.reset(token)

    provider.get_pr_labels.assert_not_called()
    provider.publish_labels.assert_not_called()
    provider.publish_comment.assert_called_once_with("Preparing PR labels...", is_temporary=True)
    provider.remove_initial_comment.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "write"])
@pytest.mark.parametrize("propagate_errors", [False, True])
@patch("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.commands.pr_generate_labels.get_settings")
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
            await _command(provider).run()
    else:
        assert await _command(provider).run() == ""

    assert command_failed() is True
    provider.get_pr_labels.assert_called_once_with(update=True)
    if failure == "read":
        provider.publish_labels.assert_not_called()
    else:
        provider.publish_labels.assert_called_once_with(["Bug fix"])
    provider.remove_initial_comment.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledgement", [True, None, 0])
@patch("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", new_callable=AsyncMock)
@patch("pr_agent.commands.pr_generate_labels.get_settings")
async def test_only_exact_false_label_acknowledgement_records_failure(
        mock_get_settings, _mock_retry, acknowledgement):
    mock_get_settings.return_value.config.publish_output = True
    mock_get_settings.return_value.config.get.return_value = True
    provider = _provider(supports_labels=True)
    provider.publish_labels.return_value = acknowledgement
    init_run_details()

    assert await _command(provider).run() == ""

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
    command = _command(provider)
    settings = get_settings()
    previous = {
        "publish_output": settings.config.publish_output,
        "propagate_command_errors": settings.config.get("propagate_command_errors", False),
    }
    settings.config.publish_output = True
    settings.set("config.propagate_command_errors", False)
    monkeypatch.setattr("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", fail_with_incomplete_diff)
    try:
        with pytest.raises(incomplete_error_class):
            await command.run()
    finally:
        settings.config.publish_output = previous["publish_output"]
        settings.set("config.propagate_command_errors", previous["propagate_command_errors"])

    provider.remove_initial_comment.assert_called_once()


@pytest.mark.asyncio
async def test_run_skips_without_recording_failure_when_all_changed_files_are_filtered(monkeypatch, tmp_path):
    async def prepare_once(prepare_fn, **_kwargs):
        await prepare_fn("primary-model")

    provider = _provider(supports_labels=True)
    command = _command(provider)
    command.ai_handler = MagicMock()
    command.vars = {"diff": ""}
    command._get_prediction = AsyncMock()
    monkeypatch.setattr("pr_agent.commands.pr_generate_labels.retry_with_fallback_models", prepare_once)
    monkeypatch.setattr("pr_agent.commands.pr_generate_labels.set_custom_labels", lambda *_args: None)
    monkeypatch.setattr(
        "pr_agent.commands.pr_generate_labels.AttemptTokenBudget.for_prompt_attempt",
        lambda *_args, **_kwargs: MagicMock(),
    )
    # A lockfile-only or asset-only PR has no diff left after file-type filtering.
    monkeypatch.setattr("pr_agent.commands.pr_generate_labels.get_pr_diff", lambda *_args, **_kwargs: "")
    settings = get_settings()
    previous = {
        "publish_output": settings.config.publish_output,
        "propagate_command_errors": settings.config.get("propagate_command_errors", False),
    }
    settings.config.publish_output = True
    settings.set("config.propagate_command_errors", False)
    init_run_details()
    output = tmp_path / "labels.json"
    token = review_json_output_path.set(str(output))
    try:
        await command.run()
    finally:
        review_json_output_path.reset(token)
        settings.config.publish_output = previous["publish_output"]
        settings.set("config.propagate_command_errors", previous["propagate_command_errors"])

    assert command_failed() is False
    assert json.loads(output.read_text(encoding="utf-8")) == {"labels": [], "usage": {}}
    command._get_prediction.assert_not_awaited()
    provider.publish_comment.assert_called_once_with("Preparing PR labels...", is_temporary=True)
    provider.publish_labels.assert_not_called()
    provider.remove_initial_comment.assert_called_once()
