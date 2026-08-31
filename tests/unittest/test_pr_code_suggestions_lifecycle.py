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
    # A bare MagicMock returns a truthy mock here, which would thread every publish call.
    provider.should_publish_improve_as_thread.return_value = False
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
@pytest.mark.parametrize("persistent_comment", [False, True])
async def test_run_does_not_publish_failure_after_successful_summary(monkeypatch, persistent_comment):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        provider.get_latest_commit_url.return_value = "https://example.invalid/commit/abcdef1234567890"
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
        settings.pr_code_suggestions.commitable_code_suggestions = False
        settings.pr_code_suggestions.dual_publishing_score_threshold = "invalid"
        settings.pr_code_suggestions.persistent_comment = persistent_comment

        await tool.run()

        provider.edit_comment.assert_called_once()
        if "body" in provider.edit_comment.call_args.kwargs:
            published_body = provider.edit_comment.call_args.kwargs["body"]
        else:
            published_body = provider.edit_comment.call_args.args[1]
        assert "final summary" in published_body
        provider.publish_comment.assert_called_once_with("progress body")
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_does_not_publish_failure_after_successful_inline_suggestions(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.get_files.return_value = [object()]
        provider.supports_code_suggestions_artifact.return_value = False
        provider.publish_code_suggestions.return_value = True

        def publish_comment(body, **_kwargs):
            if body == "Failed to generate code suggestions for PR":
                return MagicMock()
            raise RuntimeError("fallback comment rejected")

        provider.publish_comment.side_effect = publish_comment
        tool = _make_tool(provider)
        tool._validate_suggestion = MagicMock(
            side_effect=[
                (True, "", True),
                (False, "the target range is outside the diff", False),
            ]
        )
        tool.dedent_code = MagicMock(side_effect=lambda _file, _line, code: code)
        fallback_suggestion = {
            "relevant_file": "src/missing.py",
            "relevant_lines_start": 5,
            "relevant_lines_end": 5,
            "suggestion_content": "Keep the fallback visible.",
            "existing_code": "old_value",
            "improved_code": "new_value",
            "label": "bug",
        }
        suggestions = [
            {
                **fallback_suggestion,
                "relevant_file": "src/example.py",
                "relevant_lines_start": 1,
                "relevant_lines_end": 1,
            },
            fallback_suggestion,
        ]
        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": suggestions}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.is_auto_command = True
        settings.pr_code_suggestions.commitable_code_suggestions = True
        settings.pr_code_suggestions.dual_publishing_score_threshold = 0

        await tool.run()

        provider.publish_code_suggestions.assert_called_once()
        published_comments = [call.args[0] for call in provider.publish_comment.call_args_list]
        assert published_comments == [
            "**Suggestion:** Keep the fallback visible. [bug]\n\n"
            "Proposed code (not offered as a committable change because the target range is outside the diff):\n"
            "```\nnew_value\n```\n\nLocation: `src/missing.py:5-5`"
        ]
        provider.remove_initial_comment.assert_called_once()
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_publishes_failure_when_inline_suggestions_never_publish(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.get_files.return_value = [object()]
        tool = _make_tool(provider)
        tool.push_inline_code_suggestions = AsyncMock(side_effect=RuntimeError("inline publish failed"))
        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [{"score": 1}]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.is_auto_command = True
        settings.pr_code_suggestions.commitable_code_suggestions = True

        await tool.run()

        provider.publish_comment.assert_called_once_with("Failed to generate code suggestions for PR")
        assert provider.remove_initial_comment.call_count == 2
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
