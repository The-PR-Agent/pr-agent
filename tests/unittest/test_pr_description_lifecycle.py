import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from pr_agent.config_loader import get_settings
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
    "pr_description.publish_labels",
    "pr_description.use_description_markers",
)


def _make_description(provider):
    description = PRDescription.__new__(PRDescription)
    description.pr_id = "1"
    description.git_provider = provider
    description.vars = {}
    description.prediction = None
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
@pytest.mark.parametrize("propagate_tool_errors", [False, True])
async def test_run_reports_description_publication_failure(monkeypatch, propagate_tool_errors):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.publish_comment.return_value = progress_comment
        provider.publish_description.side_effect = RuntimeError("permission denied")
        provider.is_supported.return_value = False
        description = _make_description(provider)
        description.prediction = "generated"
        description._prepare_data = MagicMock()
        description._prepare_pr_answer = MagicMock(return_value=("AI title", "Description", "", []))

        monkeypatch.setattr(pr_description_module, "extract_and_cache_pr_tickets", AsyncMock())
        monkeypatch.setattr(pr_description_module, "retry_with_fallback_models", AsyncMock())
        _configure_published_run()
        settings = get_settings()
        settings.config.propagate_tool_errors = propagate_tool_errors
        settings.pr_description.enable_help_comment = False
        settings.pr_description.enable_help_text = False
        settings.pr_description.enable_semantic_files_types = False
        settings.pr_description.final_update_message = True
        settings.pr_description.generate_ai_title = True
        settings.pr_description.publish_description_as_comment = False
        settings.pr_description.publish_labels = False
        settings.pr_description.use_description_markers = False

        if propagate_tool_errors:
            with pytest.raises(RuntimeError, match="permission denied"):
                await description.run()
        else:
            assert await description.run() == ""

        provider.publish_description.assert_called_once()
        assert call("Failed to update PR description") in provider.publish_comment.call_args_list
        assert not any("updated to latest commit" in str(published) for published in provider.publish_comment.call_args_list)
        provider.remove_comment.assert_called_once_with(progress_comment)
    finally:
        restore_settings(settings_snapshot)
