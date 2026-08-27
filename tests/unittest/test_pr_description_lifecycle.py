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
