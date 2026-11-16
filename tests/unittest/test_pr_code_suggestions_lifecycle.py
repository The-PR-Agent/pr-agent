import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo.comment_identity import PRCodeSuggestionsHeader, PRCodeSuggestionsIdentity
from pr_agent.algo.review_finding_state import ParsedReviewState, reconcile_review_findings
from pr_agent.algo.run_details import command_failed
from pr_agent.algo.types import FilePatchInfo
from pr_agent.commands import pr_code_suggestions as pr_code_suggestions_module
from pr_agent.commands.pr_code_suggestions import PRCodeSuggestions
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import (
    IncompleteBitbucketPullRequestFilesError,
    IncompletePullRequestFilesError,
)
from pr_agent.git_providers.plain_diff_provider import PlainDiffGitProvider
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_SETTINGS = (
    "config.publish_output",
    "config.publish_output_progress",
    "config.is_auto_command",
    "config.propagate_command_errors",
    "pr_code_suggestions.committable_code_suggestions",
    "pr_code_suggestions.dual_publishing_score_threshold",
    "pr_code_suggestions.persistent_comment",
    "pr_code_suggestions.publish_failure_comment",
    "pr_code_suggestions.max_history_len",
    "github.publish_as_check_run",
)

# A hunk whose new-file side covers lines 1-3, so a suggestion anchored at 1-1
# passes the diff-hunk range validation in the summarized publish path.
_ANCHORED_HUNK_PATCH = (
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "+line2-changed\n"
    " line3\n"
)


def _anchored_suggestion(**overrides):
    suggestion = {
        "score": 1,
        "relevant_file": "app.py",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
        "label": "maintainability",
        "one_sentence_summary": "Anchored suggestion",
    }
    suggestion.update(overrides)
    return suggestion


def _provider_with_anchored_diff(provider):
    provider.diff_files = [
        FilePatchInfo(
            base_file="line1\nline2\nline3\n",
            head_file="line1\nline2-changed\nline3\n",
            patch=_ANCHORED_HUNK_PATCH,
            filename="app.py",
        )
    ]
    return provider


def _make_command(provider):
    # A bare MagicMock returns a truthy mock here, which would thread every publish call.
    provider.should_publish_improve_as_thread.return_value = False
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)
    command.git_provider = provider
    command.pr_url = "https://example.invalid/pull/1"
    command.progress_response = None
    command.incremental = SimpleNamespace(is_incremental=False)
    return command


def _configure_published_run():
    settings = get_settings()
    settings.config.publish_output = True
    settings.config.publish_output_progress = True
    settings.config.is_auto_command = False


def test_improve_finding_state_tracks_float_line_suggestions():
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        settings = get_settings()
        settings.config.publish_output = True
        provider = MagicMock()
        provider.get_pr_head_sha.return_value = "head-1"
        command = _make_command(provider)
        command._suggestion_finding_state_supported = lambda: True
        command._load_suggestion_finding_state = lambda: ParsedReviewState(
            reconcile_review_findings(None, [], allow_resolution=False, head_sha="").state,
            present=False,
            valid=True,
        )
        suggestion = {
            "relevant_file": "app.py",
            "relevant_lines_start": 6.0,
            "relevant_lines_end": 6.0,
            "suggestion_content": "Handle the missing value.",
            "label": "possible bug",
            "existing_code": "return value",
            "improved_code": "return value or default",
        }

        command._prepare_suggestion_finding_state({"code_suggestions": [suggestion]})

        assert len(command._suggestion_finding_state["findings"]) == 1
        assert command._suggestion_finding_state["findings"][0]["line_start"] == 6
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_inline_suggestion_keeps_float_anchors_after_dedent():
    provider = MagicMock()
    provider.supports_code_suggestions_artifact.return_value = False
    provider.publish_code_suggestions.return_value = True
    command = _make_command(provider)
    command._validate_suggestion = MagicMock(return_value=(True, "", True))
    command.dedent_code = MagicMock(side_effect=lambda _file, _line, code: code)
    command._validate_python_replacement_syntax = MagicMock(return_value=True)

    await command.push_inline_code_suggestions(
        {
            "code_suggestions": [{
                "relevant_file": "app.py",
                "relevant_lines_start": 6.0,
                "relevant_lines_end": 6.0,
                "suggestion_content": "Handle the missing value.",
                "label": "possible bug",
                "existing_code": "return value",
                "improved_code": "return value or default",
            }]
        }
    )

    published = provider.publish_code_suggestions.call_args.args[0]
    assert len(published) == 1
    assert published[0]["relevant_lines_start"] == 6
    assert published[0]["relevant_lines_end"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(("publish_output", "supports_state"), [(False, True), (True, True), (True, False)])
async def test_run_reconciles_suggestion_threads_only_when_publishing(monkeypatch, publish_output, supports_state):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS + ("data",))
    try:
        provider = MagicMock()
        provider.get_files.return_value = ["app.py"]
        provider.supports_code_suggestion_state.return_value = supports_state
        provider.supports_code_suggestions_artifact.return_value = False
        provider.reconcile_code_suggestion_threads.return_value = 0
        command = _make_command(provider)
        prediction = AsyncMock(return_value={"code_suggestions": []})
        monkeypatch.setattr(command, "prepare_prediction_main", prediction)

        async def predict_with_fallback(callback, **kwargs):
            return await callback("test-model")

        monkeypatch.setattr(pr_code_suggestions_module, "retry_with_fallback_models", predict_with_fallback)
        settings = get_settings()
        settings.config.publish_output = publish_output
        settings.config.publish_output_progress = False

        await command.run()

        assert provider.reconcile_code_suggestion_threads.call_count == int(publish_output and supports_state)
        prediction.assert_awaited_once_with("test-model")
        assert command_failed() is False
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("history_len", [0, 4])
@pytest.mark.parametrize("warning_result", ["comment", "none", "error"])
async def test_run_records_failed_persistent_update_without_duplicate_summary(
    monkeypatch, history_len, warning_result
):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        existing = SimpleNamespace(
            body=(
                f"{PRCodeSuggestionsHeader.SUMMARY.value}\n"
                f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n<table>old</table>"
            )
        )
        provider = _provider_with_anchored_diff(MagicMock())
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.supports_code_suggestion_state.return_value = history_len == 0
        provider.get_issue_comments.return_value = [existing]
        provider.get_issue_comments_newest_first.return_value = [existing]
        provider.get_latest_commit_url.return_value = "https://example.invalid/commit/deadbee"
        provider.get_comment_url.return_value = "https://example.invalid/comment/1"
        provider.edit_comment.return_value = False
        if warning_result == "error":
            provider.publish_comment.side_effect = RuntimeError("warning unavailable")
        elif warning_result == "none":
            provider.publish_comment.return_value = None
        command = _make_command(provider)
        command.generate_summarized_suggestions = MagicMock(return_value="new suggestions")
        monkeypatch.setattr(
            pr_code_suggestions_module, "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [_anchored_suggestion()]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.publish_output_progress = False
        settings.github.publish_as_check_run = False
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.persistent_comment = True
        settings.pr_code_suggestions.max_history_len = history_len

        await command.run()

        assert command_failed() is True
        assert command._output_published is True
        assert "<table>old</table>" in existing.body
        provider.edit_comment.assert_called_once()
        provider.publish_comment.assert_called_once()
        warning = provider.publish_comment.call_args.args[0]
        assert "update could not be confirmed" in warning
        assert PRCodeSuggestionsIdentity.SUMMARY.value not in warning
        assert "Failed to generate code suggestions" not in warning
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_chunk_progress_does_not_hide_failed_persistent_summary_update(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        original_body = (
            f"{PRCodeSuggestionsHeader.SUMMARY.value}\n{PRCodeSuggestionsIdentity.SUMMARY.value}\n<table>old</table>"
        )
        existing = SimpleNamespace(body=original_body)
        progress = SimpleNamespace(body="Preparing suggestions...")
        provider = _provider_with_anchored_diff(MagicMock())
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.supports_code_suggestion_state.return_value = False
        provider.get_issue_comments.return_value = [existing]
        provider.get_issue_comments_newest_first.return_value = [existing]
        provider.get_latest_commit_url.return_value = "https://example.invalid/commit/deadbee"
        provider.get_comment_url.return_value = "https://example.invalid/comment/1"
        provider.publish_comment.side_effect = [progress, SimpleNamespace(body="warning")]
        progress_updates = []

        def edit_comment(comment, body):
            if comment is existing:
                return False
            assert comment is progress
            progress_updates.append(body)
            progress.body = body
            return True

        provider.edit_comment.side_effect = edit_comment
        command = _make_command(provider)
        command.progress = progress.body
        command.generate_summarized_suggestions = MagicMock(return_value="new suggestions")

        async def generate_with_chunk_progress(*args, **kwargs):
            reporter = command._chunk_progress_reporter(2)
            assert reporter is not None
            await reporter.record_settled()
            await reporter.record_settled()
            return {"code_suggestions": [_anchored_suggestion()]}

        monkeypatch.setattr(pr_code_suggestions_module, "retry_with_fallback_models", generate_with_chunk_progress)
        _configure_published_run()
        settings = get_settings()
        settings.github.publish_as_check_run = False
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.persistent_comment = True
        settings.pr_code_suggestions.max_history_len = 4

        await command.run()

        assert "analyzed 1 of 2 chunks" in progress_updates[0]
        assert "analyzed 2 of 2 chunks" in progress_updates[1]
        assert "update could not be confirmed" in progress_updates[2]
        assert command_failed() is True
        assert existing.body == original_body
        assert sum(call.args[0] is existing for call in provider.edit_comment.call_args_list) == 1
        provider.remove_comment.assert_called_once_with(progress)
        assert provider.publish_comment.call_count == 2
        warning = provider.publish_comment.call_args.args[0]
        assert "update could not be confirmed" in warning
        assert PRCodeSuggestionsIdentity.SUMMARY.value not in warning
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_prefetches_diff_off_the_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    fetch_thread_ids = []

    def fetch_diff():
        fetch_thread_ids.append(threading.get_ident())
        started.set()
        assert release.wait(10)
        return []

    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.get_files.return_value = ["app.py"]
        provider.get_diff_files.side_effect = fetch_diff
        provider.supports_code_suggestion_state.return_value = False
        provider.is_supported.return_value = False
        command = _make_command(provider)

        monkeypatch.setattr(pr_code_suggestions_module, "retry_with_fallback_models", AsyncMock())
        get_settings().config.publish_output = False
        get_settings().config.publish_output_progress = False

        run = asyncio.create_task(command.run())
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 10)
            assert not run.done()
        finally:
            release.set()
            await asyncio.wait_for(run, 10)

        assert len(fetch_thread_ids) == 1
        assert fetch_thread_ids[0] != threading.get_ident()
        provider.get_diff_files.assert_called_once_with()
    finally:
        restore_settings(settings_snapshot)


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
        command = _make_command(provider)
        command.progress = progress_body

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
        _configure_published_run()

        with pytest.raises(asyncio.CancelledError):
            await command.run()

        provider.publish_comment.assert_called_once_with(
            progress_body, **progress_kwargs
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
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = False
        provider.publish_comment.return_value = progress_comment
        command = _make_command(provider)
        command.progress = "progress body"
        incomplete_diff_error = incomplete_error_class("incomplete aggregate diff")

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=incomplete_diff_error),
        )
        _configure_published_run()

        with pytest.raises(incomplete_error_class) as exc_info:
            await command.run()

        assert exc_info.value is incomplete_diff_error
        provider.remove_comment.assert_called_once_with(progress_comment)
        provider.publish_comment.assert_called_once_with(
            "Preparing suggestions...", is_temporary=True
        )
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_preserves_incomplete_bitbucket_diff_when_progress_cleanup_fails(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = False
        provider.publish_comment.return_value = progress_comment
        provider.remove_comment.side_effect = RuntimeError("delete unavailable")
        command = _make_command(provider)
        command.progress = "progress body"
        incomplete_diff_error = IncompleteBitbucketPullRequestFilesError("incomplete aggregate diff")

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=incomplete_diff_error),
        )
        _configure_published_run()

        with pytest.raises(IncompleteBitbucketPullRequestFilesError) as exc_info:
            await command.run()

        assert exc_info.value is incomplete_diff_error
        provider.remove_comment.assert_called_once_with(progress_comment)
        provider.publish_comment.assert_called_once_with("Preparing suggestions...", is_temporary=True)
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_does_not_remove_final_summary_when_cancelled_during_dual_publishing(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = _provider_with_anchored_diff(MagicMock())
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        command = _make_command(provider)
        command.progress = "progress body"
        command.generate_summarized_suggestions = MagicMock(return_value="final summary")
        command.dual_publishing = AsyncMock(side_effect=asyncio.CancelledError())

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [_anchored_suggestion()]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.dual_publishing_score_threshold = 1
        settings.pr_code_suggestions.persistent_comment = False

        with pytest.raises(asyncio.CancelledError):
            await command.run()

        provider.edit_comment.assert_called_once()
        call = provider.edit_comment.call_args
        assert call.args[0] is progress_comment
        edited_body = call.kwargs.get("body", call.args[1])
        assert "final summary" in edited_body
        provider.remove_comment.assert_not_called()
        assert command.progress_response is None
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent_comment", [False, True])
async def test_run_does_not_publish_failure_after_successful_summary(monkeypatch, persistent_comment):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = _provider_with_anchored_diff(MagicMock())
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        provider.get_latest_commit_url.return_value = "https://example.invalid/commit/abcdef1234567890"
        command = _make_command(provider)
        command.progress = "progress body"
        command.generate_summarized_suggestions = MagicMock(return_value="final summary")

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [_anchored_suggestion()]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.dual_publishing_score_threshold = "invalid"
        settings.pr_code_suggestions.persistent_comment = persistent_comment

        await command.run()

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

        provider.publish_comment.side_effect = RuntimeError("fallback comment rejected")
        command = _make_command(provider)
        command._validate_suggestion = MagicMock(
            side_effect=[
                (True, "", True),
                (False, "the target range is outside the diff", False),
            ]
        )
        command.dedent_code = MagicMock(side_effect=lambda _file, _line, code: code)
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
        settings.pr_code_suggestions.committable_code_suggestions = True
        settings.pr_code_suggestions.dual_publishing_score_threshold = 0

        await command.run()

        provider.publish_code_suggestions.assert_called_once()
        published_comments = [call.args[0] for call in provider.publish_comment.call_args_list]
        assert published_comments == [
            "**Suggestion:** Keep the fallback visible. [bug]\n\n"
            "Proposed code (not offered as a committable change because the target range is outside the diff):\n"
            "```\nnew_value\n```\n\nLocation: `src/missing.py:5-5`"
        ]
        provider.publish_command_failure.assert_not_called()
        provider.remove_initial_comment.assert_called_once()
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_publishes_failure_when_inline_suggestions_never_publish(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.get_files.return_value = [object()]
        command = _make_command(provider)
        command.push_inline_code_suggestions = AsyncMock(side_effect=RuntimeError("inline publish failed"))
        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [
                {"score": 1, "relevant_lines_start": 1, "relevant_lines_end": 1}
            ]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.is_auto_command = True
        settings.pr_code_suggestions.committable_code_suggestions = True

        await command.run()

        provider.publish_command_failure.assert_called_once_with(
            "improve", "Failed to generate code suggestions for PR"
        )
        assert provider.remove_initial_comment.call_count == 2
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_can_suppress_failure_comment_without_changing_failure_status(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.get_files.return_value = [object()]
        command = _make_command(provider)
        command.progress = None
        original_error = RuntimeError("generation failed")
        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=original_error),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.publish_output_progress = False
        settings.config.propagate_command_errors = True
        settings.pr_code_suggestions.publish_failure_comment = False

        with pytest.raises(RuntimeError) as exc_info:
            await command.run()

        assert exc_info.value is original_error
        provider.publish_comment.assert_not_called()
        assert command_failed() is True
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_preserves_original_error_when_failure_comment_publish_fails(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.get_files.return_value = [object()]
        provider.publish_command_failure.side_effect = RuntimeError("failure comment rejected")
        command = _make_command(provider)
        original_error = RuntimeError("generation failed")

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=original_error),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.is_auto_command = True
        settings.config.propagate_command_errors = True

        with pytest.raises(RuntimeError) as exc_info:
            await command.run()

        assert exc_info.value is original_error
        provider.publish_command_failure.assert_called_once_with(
            "improve", "Failed to generate code suggestions for PR"
        )
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_does_not_remove_persistent_summary_when_cancelled_during_dual_publishing(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = _provider_with_anchored_diff(MagicMock())
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        provider.get_issue_comments.return_value = []
        provider.get_latest_commit_url.return_value = "https://example.invalid/commit/abcdef1234567890"
        command = _make_command(provider)
        command.progress = "progress body"
        command.generate_summarized_suggestions = MagicMock(return_value="final summary")
        command.dual_publishing = AsyncMock(side_effect=asyncio.CancelledError())

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [_anchored_suggestion()]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.dual_publishing_score_threshold = 1
        settings.pr_code_suggestions.persistent_comment = True

        with pytest.raises(asyncio.CancelledError):
            await command.run()

        provider.edit_comment.assert_called_once()
        assert provider.edit_comment.call_args.args[0] is progress_comment
        assert "final summary" in provider.edit_comment.call_args.args[1]
        provider.remove_comment.assert_not_called()
        assert command.progress_response is None
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
        command = _make_command(provider)
        command.progress = "progress body"

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
        _configure_published_run()

        with pytest.raises(asyncio.CancelledError):
            await command.run()

        provider.edit_comment.assert_called_once_with(progress_comment, "Code suggestions generation cancelled.")
        provider.remove_comment.assert_called_once_with(progress_comment)
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_cleans_up_progress_comment_on_check_run_publish(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = _provider_with_anchored_diff(MagicMock())
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        provider.get_latest_commit_url.return_value = "https://example.invalid/commit/abcdef1234567890"
        provider._publish_check_run = MagicMock(return_value=True)
        command = _make_command(provider)
        command.progress = "progress body"
        command.generate_summarized_suggestions = MagicMock(return_value="final summary")

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [_anchored_suggestion()]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.github.publish_as_check_run = True
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.persistent_comment = True

        await command.run()

        provider._publish_check_run.assert_called_once()
        provider.edit_comment.assert_called_once_with(
            progress_comment, "Code suggestions published in the persistent thread above."
        )
        provider.remove_comment.assert_called_once_with(progress_comment)
        assert command.progress_response is None
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_retains_progress_handle_when_check_run_cleanup_fails(monkeypatch):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = _provider_with_anchored_diff(MagicMock())
        progress_comment = MagicMock(name="progress_comment")
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = progress_comment
        provider.get_latest_commit_url.return_value = "https://example.invalid/commit/abcdef1234567890"
        provider._publish_check_run = MagicMock(return_value=True)
        provider.remove_comment.side_effect = RuntimeError("delete unavailable")
        command = _make_command(provider)
        command.progress = "progress body"
        command.generate_summarized_suggestions = MagicMock(return_value="final summary")

        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [_anchored_suggestion()]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.github.publish_as_check_run = True
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.persistent_comment = True

        await command.run()

        provider._publish_check_run.assert_called_once()
        provider.edit_comment.assert_called_once_with(
            progress_comment, "Code suggestions published in the persistent thread above."
        )
        provider.remove_comment.assert_called_once_with(progress_comment)
        # The handle is retained so a later cancellation or error handler can retry removal
        assert command.progress_response is progress_comment
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("propagate_errors", [False, True])
@pytest.mark.parametrize("show_progress", [False, True])
@pytest.mark.parametrize("supports_artifact", [False, True])
async def test_run_reports_exhausted_inline_publication_retries(
    monkeypatch, propagate_errors, show_progress, supports_artifact
):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.get_files.return_value = [object()]
        provider = _provider_with_anchored_diff(provider)
        provider.is_supported.return_value = False
        provider.supports_code_suggestions_artifact.return_value = supports_artifact
        provider.publish_code_suggestions_artifact.return_value = False
        provider.publish_code_suggestions.return_value = False
        command = _make_command(provider)
        command._validate_suggestion = MagicMock(return_value=(True, "", True))
        command.dedent_code = MagicMock(side_effect=lambda _file, _line, code: code)
        suggestion = {
            "score": 8,
            "relevant_file": "app.py",
            "relevant_lines_start": 1,
            "relevant_lines_end": 1,
            "suggestion_content": "Use the helper.",
            "existing_code": "old()",
            "improved_code": "new()",
            "label": "maintainability",
            "one_sentence_summary": "Use the helper everywhere.",
        }
        monkeypatch.setattr(
            pr_code_suggestions_module,
            "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [suggestion]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.publish_output_progress = show_progress
        settings.config.propagate_command_errors = propagate_errors
        settings.pr_code_suggestions.committable_code_suggestions = True

        await command.run()

        # Exhausted inline retries fall back to the summarized-comment path, so the
        # run succeeds and the author still receives the computed suggestions.
        assert provider.publish_code_suggestions.call_count == (1 if supports_artifact else 2)
        assert command._output_published is True
        published_comments = [call.args[0] for call in provider.publish_comment.call_args_list]
        assert "Use the helper." in published_comments[-1]
        assert "Failed to generate code suggestions for PR" not in published_comments[-1]
        if show_progress:
            assert published_comments[0] == "Preparing suggestions..."
            provider.remove_comment.assert_called_once_with(provider.publish_comment.return_value)
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("propagate_errors", [False, True])
@pytest.mark.parametrize(("supports_artifact", "fallback_kind"), [
    (False, "anchor"), (True, "anchor"), (False, "syntax"),
    (False, "truncated"), (False, "coverage"),
])
async def test_failed_inline_retries_preserve_fallback_output(
    monkeypatch, propagate_errors, supports_artifact, fallback_kind
):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS)
    try:
        provider = MagicMock()
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = False
        provider.supports_code_suggestions_artifact.return_value = supports_artifact
        provider.publish_code_suggestions_artifact.return_value = False
        provider.publish_code_suggestions.return_value = False
        command = _make_command(provider)
        command._validate_suggestion = MagicMock(side_effect=[
            (True, "", True),
            (False, "unverified range", False) if fallback_kind == "anchor" else (True, "", True),
        ])
        command._validate_python_replacement_syntax = MagicMock(side_effect=[True, False])
        command.dedent_code = MagicMock(side_effect=lambda _file, _line, code: code)
        command._get_suggestions_coverage_footer = MagicMock(
            return_value="\n\nCoverage notice" if fallback_kind == "coverage" else ""
        )
        suggestion = {
            "relevant_file": "app.py", "relevant_lines_start": 1, "relevant_lines_end": 1,
            "suggestion_content": "Inline suggestion.", "existing_code": "old()",
            "improved_code": "new()", "label": "maintainability",
        }
        suggestions = [suggestion]
        if fallback_kind != "coverage":
            fallback = {**suggestion, "suggestion_content": "Fallback suggestion."}
            if fallback_kind == "truncated":
                fallback["_is_truncated"] = True
            suggestions.append(fallback)
        monkeypatch.setattr(
            pr_code_suggestions_module, "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": suggestions}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.propagate_command_errors = propagate_errors
        settings.pr_code_suggestions.committable_code_suggestions = True

        if propagate_errors:
            with pytest.raises(RuntimeError, match="Failed to publish code suggestions"):
                await command.run()
        else:
            await command.run()

        comments = [call.args[0] for call in provider.publish_comment.call_args_list]
        assert len(comments) == 2
        assert comments[0] == "Preparing suggestions..."
        expected = "Coverage notice" if fallback_kind == "coverage" else "Fallback suggestion."
        assert expected in comments[1]
        assert "Failed to generate code suggestions" not in comments[1]
        assert command._output_published is True
        provider.remove_comment.assert_called_once_with(provider.publish_comment.return_value)
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_output_no_suggestions", [True, False])
async def test_run_routes_all_invalid_ranges_through_publish_no_suggestions(
    monkeypatch, publish_output_no_suggestions
):
    settings_snapshot = snapshot_settings(
        _TRACKED_SETTINGS + ("pr_code_suggestions.publish_output_no_suggestions",)
    )
    try:
        provider = MagicMock()
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.supports_code_suggestions_artifact.return_value = False
        command = _make_command(provider)
        command.publish_no_suggestions = AsyncMock()

        monkeypatch.setattr(
            pr_code_suggestions_module, "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [
                {"relevant_lines_start": -1, "relevant_lines_end": -1, "label": "bug",
                 "relevant_file": "app.py", "one_sentence_summary": "sentinel"},
                {"relevant_lines_start": 5, "relevant_lines_end": 2, "label": "bug",
                 "relevant_file": "app.py", "one_sentence_summary": "reversed"},
            ]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.is_auto_command = True
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.persistent_comment = False
        settings.pr_code_suggestions.publish_output_no_suggestions = publish_output_no_suggestions

        await command.run()

        command.publish_no_suggestions.assert_awaited_once()
        provider.publish_comment.assert_not_called()
        assert command._output_published is False
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_output_no_suggestions", [True, False])
async def test_run_all_invalid_ranges_honors_quiet_gate_via_real_publish(
    monkeypatch, publish_output_no_suggestions
):
    settings_snapshot = snapshot_settings(
        _TRACKED_SETTINGS + ("pr_code_suggestions.publish_output_no_suggestions",)
    )
    try:
        provider = MagicMock()
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.supports_code_suggestions_artifact.return_value = False
        provider.publish_comment.return_value = MagicMock()
        provider.should_publish_improve_as_thread.return_value = False
        command = _make_command(provider)
        command.generate_summarized_suggestions = MagicMock(return_value="table")

        monkeypatch.setattr(
            pr_code_suggestions_module, "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [
                {"relevant_lines_start": -1, "relevant_lines_end": -1, "label": "bug",
                 "relevant_file": "app.py", "one_sentence_summary": "sentinel"},
            ]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.is_auto_command = True
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.persistent_comment = False
        settings.pr_code_suggestions.publish_output_no_suggestions = publish_output_no_suggestions

        await command.run()

        command.generate_summarized_suggestions.assert_not_called()
        if publish_output_no_suggestions:
            comments = [call.args[0] for call in provider.publish_comment.call_args_list]
            assert len(comments) == 1
            assert "No code suggestions found for the PR." in comments[0]
        else:
            provider.publish_comment.assert_not_called()
            assert get_settings().data["artifact"] == ""
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
async def test_run_plain_diff_leaks_no_progress_when_all_ranges_invalid(monkeypatch, capsys):
    settings_snapshot = snapshot_settings(
        _TRACKED_SETTINGS + ("pr_code_suggestions.publish_output_no_suggestions", "plain_diff.content")
    )
    try:
        get_settings().set(
            "plain_diff.content",
            "diff --git a/foo.py b/foo.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,3 @@\n"
            " line1\n"
            "-line2\n"
            "+line2-changed\n"
            " line3\n",
        )
        provider = PlainDiffGitProvider(None)
        command = PRCodeSuggestions.__new__(PRCodeSuggestions)
        command.git_provider = provider
        command.pr_url = "https://example.invalid/pull/1"
        command.progress_response = None
        command.incremental = SimpleNamespace(is_incremental=False)
        command.progress = "## Generating PR code suggestions"

        monkeypatch.setattr(
            pr_code_suggestions_module, "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [
                {"relevant_lines_start": -1, "relevant_lines_end": -1, "label": "bug",
                 "relevant_file": "app.py", "one_sentence_summary": "sentinel"},
            ]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.is_auto_command = False
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.persistent_comment = False

        await command.run()

        out = capsys.readouterr().out
        # The output-only provider cannot edit/remove a previously published comment, so
        # the progress placeholder must never be persisted and only the final document remains.
        assert "Generating PR code suggestions" not in out
        assert out.count("## PR Code Suggestions") == 1
        assert out.count("No code suggestions found for the PR.") == 1
    finally:
        restore_settings(settings_snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_no_suggestions", [True, False])
async def test_run_valid_ranges_skip_no_suggestions_comment(monkeypatch, publish_no_suggestions):
    settings_snapshot = snapshot_settings(_TRACKED_SETTINGS + ("pr_code_suggestions.publish_output_no_suggestions",))
    try:
        provider = MagicMock()
        provider.get_files.return_value = [object()]
        provider.is_supported.return_value = True
        provider.supports_code_suggestions_artifact.return_value = False
        provider.publish_comment.return_value = MagicMock()
        _provider_with_anchored_diff(provider)
        command = _make_command(provider)
        command.publish_no_suggestions = AsyncMock()

        monkeypatch.setattr(
            pr_code_suggestions_module, "retry_with_fallback_models",
            AsyncMock(return_value={"code_suggestions": [
                {"relevant_lines_start": "2", "relevant_lines_end": "2", "label": "bug",
                 "relevant_file": "app.py", "one_sentence_summary": "valid",
                 "suggestion_content": "Content", "existing_code": "old()",
                 "improved_code": "new()", "score": 7},
            ]}),
        )
        _configure_published_run()
        settings = get_settings()
        settings.config.is_auto_command = True
        settings.pr_code_suggestions.committable_code_suggestions = False
        settings.pr_code_suggestions.persistent_comment = False
        settings.pr_code_suggestions.publish_output_no_suggestions = publish_no_suggestions

        await command.run()

        command.publish_no_suggestions.assert_not_awaited()
        assert command._output_published is True
    finally:
        restore_settings(settings_snapshot)
