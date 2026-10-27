import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from pr_agent.algo.comment_identity import PRDescriptionIdentity
from pr_agent.algo.run_details import command_failed
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import (
    IncompleteBitbucketPullRequestFilesError,
    IncompletePullRequestFilesError,
)
from pr_agent.tools import pr_description as pr_description_module
from pr_agent.tools.pr_description import PRDescription
from tests.unittest._settings_helpers import (
    restore_settings,
    snapshot_settings,
)

_TRACKED_SETTINGS = (
    "config.publish_output",
    "config.is_auto_command",
    "config.propagate_tool_errors",
    "pr_description.enable_help_comment",
    "pr_description.enable_help_text",
    "pr_description.enable_semantic_files_types",
    "pr_description.final_update_message",
    "pr_description.generate_ai_title",
    "pr_description.publish_description_as_comment",
    "pr_description.publish_description_as_comment_persistent",
    "pr_description.publish_labels",
    "pr_description.use_description_markers",
)


def _make_description(provider):
    description = PRDescription.__new__(PRDescription)
    description.pr_id = "1"
    description.git_provider = provider
    description.vars = {}
    description.prediction = None
    description.data = None
    description.file_label_dict = None
    return description


def _configure_published_run():
    settings = get_settings()
    settings.config.publish_output = True
    settings.config.is_auto_command = False
    settings.config.propagate_tool_errors = False


@pytest.mark.asyncio
async def test_run_removes_progress_comment_when_description_generation_fails(
    monkeypatch,
):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.publish_comment.return_value = progress_comment
        description = _make_description(provider)

        monkeypatch.setattr(
            pr_description_module,
            "extract_and_cache_pr_tickets",
            AsyncMock(),
        )
        monkeypatch.setattr(
            pr_description_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=RuntimeError("model unavailable")),
        )
        _configure_published_run()

        await description.run()

        provider.publish_comment.assert_called_once_with(
            "Preparing PR description...", is_temporary=True
        )
        provider.remove_comment.assert_called_once_with(progress_comment)
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "incomplete_error_class", [IncompleteBitbucketPullRequestFilesError, IncompletePullRequestFilesError]
)
async def test_run_re_raises_incomplete_provider_diff_after_progress_cleanup(monkeypatch, incomplete_error_class):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.publish_comment.return_value = progress_comment
        description = _make_description(provider)

        monkeypatch.setattr(pr_description_module, "extract_and_cache_pr_tickets", AsyncMock())
        incomplete_diff_error = incomplete_error_class("incomplete aggregate diff")
        monkeypatch.setattr(
            pr_description_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=incomplete_diff_error),
        )
        _configure_published_run()

        with pytest.raises(incomplete_error_class) as exc_info:
            await description.run()

        assert exc_info.value is incomplete_diff_error
        provider.remove_comment.assert_called_once_with(progress_comment)
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_removes_progress_comment_when_cancelled(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.publish_comment.return_value = progress_comment
        description = _make_description(provider)

        monkeypatch.setattr(
            pr_description_module,
            "extract_and_cache_pr_tickets",
            AsyncMock(),
        )
        monkeypatch.setattr(
            pr_description_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
        _configure_published_run()

        with pytest.raises(asyncio.CancelledError):
            await description.run()

        provider.remove_comment.assert_called_once_with(progress_comment)
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_neutralizes_progress_comment_before_delete(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.publish_comment.return_value = progress_comment
        provider.remove_comment.side_effect = RuntimeError(
            "delete unavailable"
        )
        description = _make_description(provider)

        monkeypatch.setattr(
            pr_description_module,
            "extract_and_cache_pr_tickets",
            AsyncMock(),
        )
        monkeypatch.setattr(
            pr_description_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=RuntimeError("model unavailable")),
        )
        _configure_published_run()
        get_settings().config.propagate_tool_errors = True

        with pytest.raises(RuntimeError, match="model unavailable"):
            await description.run()

        provider.edit_comment.assert_called_once_with(
            progress_comment, "PR description generation finished."
        )
        provider.remove_comment.assert_called_once_with(progress_comment)
        assert provider.method_calls[-2:] == [
            call.edit_comment(
                progress_comment, "PR description generation finished."
            ),
            call.remove_comment(progress_comment),
        ]
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_appends_partial_description_coverage_to_output(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.is_supported.return_value = False
        description = _make_description(provider)
        description.prediction = "generated"
        description.description_failed_chunk_count = 1
        description.description_total_chunk_count = 2
        description.description_failed_files = ["src/file2.py"]
        description._prepare_data = MagicMock()
        description._prepare_pr_answer = MagicMock(return_value=("AI title", "Description", ""))

        monkeypatch.setattr(pr_description_module, "extract_and_cache_pr_tickets", AsyncMock())
        monkeypatch.setattr(pr_description_module, "retry_with_fallback_models", AsyncMock())
        settings = get_settings()
        settings.config.publish_output = False
        settings.pr_description.enable_help_comment = False
        settings.pr_description.enable_help_text = False
        settings.pr_description.enable_semantic_files_types = False
        settings.pr_description.publish_labels = False
        settings.pr_description.use_description_markers = False

        await description.run()

        artifact = get_settings().data["artifact"]
        assert "Description coverage" in artifact
        assert "1 of 2 file-description chunks failed" in artifact
        assert "`src/file2.py`" in artifact
    finally:
        restore_settings(settings_snapshot)


@pytest.fixture
def published_description(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.publish_comment.return_value = progress_comment
        provider.is_supported.return_value = False
        provider.get_latest_commit_url.return_value = "https://example.test/commit/123"
        provider.get_pr_url.return_value = "https://example.test/pull/1"
        description = _make_description(provider)
        description.prediction = "generated"
        description._prepare_data = MagicMock()
        description._prepare_pr_answer = MagicMock(return_value=("AI title", "Description", ""))

        monkeypatch.setattr(pr_description_module, "extract_and_cache_pr_tickets", AsyncMock())
        monkeypatch.setattr(pr_description_module, "retry_with_fallback_models", AsyncMock())
        _configure_published_run()
        settings = get_settings()
        settings.pr_description.enable_help_comment = False
        settings.pr_description.enable_help_text = False
        settings.pr_description.enable_semantic_files_types = False
        settings.pr_description.final_update_message = True
        settings.pr_description.generate_ai_title = True
        settings.pr_description.publish_description_as_comment = False
        settings.pr_description.publish_labels = False
        settings.pr_description.use_description_markers = False
        yield description, provider, progress_comment
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("propagate_tool_errors", [False, True])
async def test_run_reports_description_publication_failure(published_description, propagate_tool_errors):
    description, provider, progress_comment = published_description
    provider.publish_description.side_effect = RuntimeError("permission denied")
    get_settings().config.propagate_tool_errors = propagate_tool_errors

    if propagate_tool_errors:
        with pytest.raises(RuntimeError, match="permission denied"):
            await description.run()
    else:
        assert await description.run() == ""

    provider.publish_description.assert_called_once()
    assert command_failed()
    assert call("Failed to update PR description") in provider.publish_comment.call_args_list
    provider.get_latest_commit_url.assert_not_called()
    provider.remove_comment.assert_called_once_with(progress_comment)


@pytest.mark.asyncio
@pytest.mark.parametrize("propagate_tool_errors", [False, True])
@pytest.mark.parametrize("failed_step", ["get_latest_commit_url", "get_pr_url", "publish_comment"])
async def test_run_keeps_description_success_when_final_feedback_fails(
    published_description, propagate_tool_errors, failed_step
):
    description, provider, progress_comment = published_description
    get_settings().config.propagate_tool_errors = propagate_tool_errors
    error = RuntimeError("optional feedback failed")
    if failed_step == "publish_comment":
        # The final comment may have reached the provider before the error; never retry it.
        provider.publish_comment.side_effect = [progress_comment, error]
    else:
        getattr(provider, failed_step).side_effect = error

    assert await description.run() == ""

    provider.publish_description.assert_called_once()
    assert not command_failed()
    assert call("Failed to update PR description") not in provider.publish_comment.call_args_list
    assert provider.publish_comment.call_count == (2 if failed_step == "publish_comment" else 1)
    provider.edit_comment.assert_called_once_with(progress_comment, "PR description generation finished.")
    provider.remove_comment.assert_called_once_with(progress_comment)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["disabled", "automatic", "no_commit_url"])
async def test_run_skips_optional_feedback_without_changing_description_success(published_description, gate):
    description, provider, _ = published_description
    settings = get_settings()
    settings.pr_description.final_update_message = gate != "disabled"
    settings.config.is_auto_command = gate == "automatic"
    if gate == "no_commit_url":
        provider.get_latest_commit_url.return_value = None

    assert await description.run() == ""

    provider.publish_description.assert_called_once()
    assert not command_failed()
    provider.get_pr_url.assert_not_called()
    if gate != "no_commit_url":
        provider.get_latest_commit_url.assert_not_called()
    assert provider.publish_comment.call_count == (0 if gate == "automatic" else 1)


@pytest.mark.asyncio
async def test_run_propagates_cancellation_after_description_update(published_description):
    description, provider, progress_comment = published_description
    provider.get_latest_commit_url.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await description.run()

    provider.publish_description.assert_called_once()
    assert not command_failed()
    provider.get_pr_url.assert_not_called()
    provider.remove_comment.assert_called_once_with(progress_comment)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_run_keeps_description_comment_delivery_failures_strict(published_description, persistent):
    description, provider, progress_comment = published_description
    settings = get_settings()
    settings.config.propagate_tool_errors = True
    settings.pr_description.publish_description_as_comment = True
    settings.pr_description.publish_description_as_comment_persistent = persistent
    error = RuntimeError("description comment failed")
    if persistent:
        provider.publish_persistent_comment.side_effect = error
    else:
        provider.publish_comment.side_effect = [progress_comment, error]

    with pytest.raises(RuntimeError, match="description comment failed"):
        await description.run()

    assert command_failed()
    provider.publish_description.assert_not_called()
    provider.get_latest_commit_url.assert_not_called()
    provider.remove_comment.assert_called_once_with(progress_comment)
    if persistent:
        kwargs = provider.publish_persistent_comment.call_args.kwargs
        assert kwargs["final_update_message"] is False
        assert kwargs["identity_marker"] == PRDescriptionIdentity.PERSISTENT.value
        assert kwargs["require_agent_authorship"] is True
        provider.publish_persistent_comment_full.assert_not_called()


@pytest.mark.asyncio
async def test_run_always_includes_walkthrough_in_the_description_body(monkeypatch):
    # Regression guard for #3116: a provider advertising inline-comment capability support used
    # to strip the File Walkthrough from the description body and publish nothing in its place.
    # The walkthrough must always be appended on the non-marker rendering path.
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.is_supported.return_value = True
        description = _make_description(provider)
        description.prediction = "generated"
        description._prepare_data = MagicMock()
        description._prepare_pr_answer = MagicMock(return_value=("AI title", "Base body", "WALKTHROUGH"))

        monkeypatch.setattr(pr_description_module, "extract_and_cache_pr_tickets", AsyncMock())
        monkeypatch.setattr(pr_description_module, "retry_with_fallback_models", AsyncMock())
        _configure_published_run()
        settings = get_settings()
        settings.pr_description.enable_help_comment = False
        settings.pr_description.enable_help_text = False
        settings.pr_description.enable_semantic_files_types = False
        settings.pr_description.final_update_message = False
        settings.pr_description.generate_ai_title = True
        settings.pr_description.publish_description_as_comment = False
        settings.pr_description.publish_labels = False
        settings.pr_description.use_description_markers = False

        await description.run()

        provider.publish_description.assert_called_once()
        title, body = provider.publish_description.call_args.args
        assert title == "AI title"
        assert "WALKTHROUGH" in body
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_skips_without_recording_failure_when_all_changed_files_are_filtered(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.publish_comment.return_value = progress_comment
        provider.is_supported.return_value = False
        description = _make_description(provider)

        async def fake_retry(prepare_fn, model_type=None):
            await prepare_fn("primary-model")

        monkeypatch.setattr(pr_description_module, "extract_and_cache_pr_tickets", AsyncMock())
        monkeypatch.setattr(pr_description_module, "retry_with_fallback_models", fake_retry)
        # A lockfile-only or asset-only PR has no diff left after file-type filtering.
        monkeypatch.setattr(pr_description_module, "get_pr_diff", MagicMock(return_value=("", [])))
        monkeypatch.setattr(
            pr_description_module,
            "get_pr_diff_multiple_patchs",
            MagicMock(return_value=([], [], [], [], {}, [])),
        )
        monkeypatch.setattr(
            pr_description_module,
            "fit_related_tickets_to_prompt_budget",
            lambda _pr, raw_vars, _system, _user, _model, **_kwargs: (raw_vars, MagicMock()),
        )
        _configure_published_run()

        await description.run()

        assert not command_failed()
        provider.publish_comment.assert_called_once_with(
            "Preparing PR description...", is_temporary=True
        )
        provider.remove_comment.assert_called_once_with(progress_comment)
        provider.publish_description.assert_not_called()
    finally:
        restore_settings(settings_snapshot)
