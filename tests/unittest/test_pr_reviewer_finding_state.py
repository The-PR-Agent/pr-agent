from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.review_finding_state import reconcile_review_findings, serialize_review_state
from pr_agent.algo.utils import PRReviewHeader
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


def _reviewer(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.pr_url = "https://example.test/pull/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.prediction = "review: {}"
    reviewer.set_review_labels = MagicMock()
    return reviewer


def _finding(body="The lock is never released."):
    return {"body": body, "path": "app.py", "line_start": 2, "line_end": 2}


def _settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.config, "is_auto_command", False, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setattr(settings.pr_reviewer, "persistent_finding_state", True, raising=False)
    monkeypatch.setattr(settings.pr_reviewer, "inline_key_issues", False)
    monkeypatch.setattr(settings.pr_reviewer, "publish_output_no_suggestions", False)
    return settings


def test_prepare_review_reconciles_previous_state_and_renders_resolved_section(monkeypatch):
    settings = _settings(monkeypatch)
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    old_body = (
        f"{PRReviewHeader.REGULAR.value} 🔍\n\nold review\n\n"
        f"{serialize_review_state(previous)}"
    )
    provider = MagicMock()
    provider.get_issue_comments.return_value = [SimpleNamespace(body=old_body)]
    provider.get_diff_files.return_value = []
    provider.is_supported.side_effect = lambda capability: capability == "get_issue_comments"
    reviewer = _reviewer(provider)

    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value={"review": {"key_issues_to_review": []}}),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch("pr_agent.tools.pr_reviewer.convert_to_markdown_v2", return_value="No major issues detected"),
    ):
        review = reviewer._prepare_pr_review()

    assert "<summary>✅ Resolved findings</summary>" in review
    assert "The lock is never released." in review
    assert reviewer._review_state_result.resolved_ids == (previous["findings"][0]["finding_id"],)
    assert settings.pr_reviewer.persistent_finding_state is True


@pytest.mark.asyncio
async def test_run_publishes_state_transition_even_when_review_has_no_suggestions(monkeypatch):
    settings = _settings(monkeypatch)
    provider = MagicMock()
    provider.get_files.return_value = ["app.py"]
    provider.should_publish_review_as_thread.return_value = False
    reviewer = _reviewer(provider)
    reviewer.vars = {}
    reviewer._prepare_prediction = AsyncMock()
    reviewer._prepare_pr_review = MagicMock(return_value="No major issues detected")
    reviewer._review_state_result = SimpleNamespace(changed=True)
    reviewer._review_state_blocked = False

    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", fake_extract_tickets)
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.retry_with_fallback_models", fake_retry)

    await reviewer.run()

    provider.publish_persistent_comment_full.assert_called_once()
    assert provider.publish_persistent_comment_full.call_args.kwargs["fallback_on_error"] is False
    assert provider.publish_persistent_comment_full.call_args.args[0] == "No major issues detected"
    provider.publish_persistent_comment.assert_not_called()
    assert settings.pr_reviewer.publish_output_no_suggestions is False


@pytest.mark.asyncio
async def test_invalid_history_does_not_update_persistent_comment(monkeypatch):
    _settings(monkeypatch)
    provider = MagicMock()
    provider.get_files.return_value = ["app.py"]
    provider.should_publish_review_as_thread.return_value = False
    reviewer = _reviewer(provider)
    reviewer.vars = {}
    reviewer._prepare_prediction = AsyncMock()
    reviewer._prepare_pr_review = MagicMock(return_value="A major issue was detected")
    reviewer._review_state_result = None
    reviewer._review_state_blocked = True

    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", fake_extract_tickets)
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.retry_with_fallback_models", fake_retry)

    await reviewer.run()

    provider.publish_persistent_comment.assert_not_called()
    provider.publish_comment.assert_any_call("A major issue was detected")
