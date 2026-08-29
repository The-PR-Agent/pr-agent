import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_code_suggestions as pr_code_suggestions_module
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_SETTINGS = (
    "config.publish_output",
    "config.publish_output_progress",
    "config.is_auto_command",
    "pr_code_suggestions.commitable_code_suggestions",
    "pr_code_suggestions.dual_publishing_score_threshold",
    "pr_code_suggestions.persistent_comment",
    "github.publish_as_check_run",
)


def _make_tool(provider):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = provider
    tool.pr_url = "https://example.invalid/pull/1"
    tool.progress_response = None
    tool.incremental = SimpleNamespace(is_incremental=False)
    return tool


def _configure_published_run():
    settings = get_settings()
    settings.config.publish_output = True
    settings.config.publish_output_progress = True
    settings.config.is_auto_command = False


@pytest.mark.parametrize(
    ("supports_gfm", "progress_body", "progress_kwargs"),
    [
        (False, "Preparing suggestions...", {"is_temporary": True}),
        (True, "progress body", {}),
    ],
)
@pytest.mark.asyncio
async def test_run_removes_progress_comment_when_cancelled(
    monkeypatch, supports_gfm, progress_body, progress_kwargs
):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = supports_gfm
        provider.publish_comment.return_value = progress_comment
        tool = _make_tool(provider)
        tool.progress = progress_body

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
        _configure_published_run()

        with pytest.raises(asyncio.CancelledError):
            await tool.run()

        provider.publish_comment.assert_called_once_with(
            progress_body, **progress_kwargs
        )
        provider.remove_comment.assert_called_once_with(progress_comment)
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_does_not_remove_final_summary_when_cancelled_during_dual_publishing(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        tool = _make_tool(provider)
        tool.progress = "progress body"
        tool.generate_summarized_suggestions = MagicMock(return_value="final summary")
        tool.dual_publishing = AsyncMock(side_effect=asyncio.CancelledError())

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [{"score": 1}]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.pr_code_suggestions.commitable_code_suggestions = False
        settings.pr_code_suggestions.dual_publishing_score_threshold = 1
        settings.pr_code_suggestions.persistent_comment = False

        with pytest.raises(asyncio.CancelledError):
            await tool.run()

        provider.edit_comment.assert_called_once()
        assert provider.edit_comment.call_args.args[0] is progress_comment
        assert "final summary" in provider.edit_comment.call_args.kwargs["body"]
        provider.remove_comment.assert_not_called()
        assert tool.progress_response is None
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_does_not_remove_persistent_summary_when_cancelled_during_dual_publishing(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        provider.get_issue_comments.return_value = []
        provider.get_latest_commit_url.return_value = "https://example.invalid/commit/abcdef1234567890"
        tool = _make_tool(provider)
        tool.progress = "progress body"
        tool.generate_summarized_suggestions = MagicMock(return_value="final summary")
        tool.dual_publishing = AsyncMock(side_effect=asyncio.CancelledError())

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [{"score": 1}]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.pr_code_suggestions.commitable_code_suggestions = False
        settings.pr_code_suggestions.dual_publishing_score_threshold = 1
        settings.pr_code_suggestions.persistent_comment = True

        with pytest.raises(asyncio.CancelledError):
            await tool.run()

        provider.edit_comment.assert_called_once()
        assert provider.edit_comment.call_args.args[0] is progress_comment
        assert "final summary" in provider.edit_comment.call_args.args[1]
        provider.remove_comment.assert_not_called()
        assert tool.progress_response is None
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_preserves_cancellation_when_progress_cleanup_fails(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        provider.remove_comment.side_effect = RuntimeError("delete unavailable")
        tool = _make_tool(provider)
        tool.progress = "progress body"

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
        _configure_published_run()

        with pytest.raises(asyncio.CancelledError):
            await tool.run()

        provider.edit_comment.assert_called_once_with(
            progress_comment, "Code suggestions generation cancelled."
        )
        provider.remove_comment.assert_called_once_with(progress_comment)
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_cleans_up_progress_comment_on_check_run_publish(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        provider.get_latest_commit_url.return_value = "https://example.invalid/commit/abcdef1234567890"
        provider._publish_check_run = MagicMock(return_value=True)
        tool = _make_tool(provider)
        tool.progress = "progress body"
        tool.generate_summarized_suggestions = MagicMock(return_value="final summary")

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [{"score": 1}]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.github.publish_as_check_run = True
        settings.pr_code_suggestions.commitable_code_suggestions = False
        settings.pr_code_suggestions.persistent_comment = True

        await tool.run()

        provider._publish_check_run.assert_called_once()
        provider.edit_comment.assert_called_once_with(
            progress_comment, "Code suggestions published in the persistent thread above."
        )
        provider.remove_comment.assert_called_once_with(progress_comment)
        assert tool.progress_response is None
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_retains_progress_handle_when_check_run_cleanup_fails(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        provider.get_latest_commit_url.return_value = "https://example.invalid/commit/abcdef1234567890"
        provider._publish_check_run = MagicMock(return_value=True)
        provider.remove_comment.side_effect = RuntimeError("delete unavailable")
        tool = _make_tool(provider)
        tool.progress = "progress body"
        tool.generate_summarized_suggestions = MagicMock(return_value="final summary")

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [{"score": 1}]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.github.publish_as_check_run = True
        settings.pr_code_suggestions.commitable_code_suggestions = False
        settings.pr_code_suggestions.persistent_comment = True

        await tool.run()

        provider._publish_check_run.assert_called_once()
        provider.edit_comment.assert_called_once_with(
            progress_comment, "Code suggestions published in the persistent thread above."
        )
        provider.remove_comment.assert_called_once_with(progress_comment)
        # The handle is retained so a later cancellation or error handler can retry removal
        assert tool.progress_response is progress_comment
    finally:
        restore_settings(settings_snapshot)
