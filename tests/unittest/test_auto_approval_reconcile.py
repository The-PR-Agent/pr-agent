"""PRReviewer reconciles the auto-approval once per /review run (issue #4698)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.commands import pr_reviewer as pr_reviewer_module
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.config_loader import get_settings


def _reviewer(verdict="no_concerns_found", model_fields=("no_concerns_found", "low"), provider=None):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider or MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer._merge_recommendation_status = verdict
    reviewer._approval_model_fields = model_fields
    return reviewer


@pytest.fixture
def approval_on(monkeypatch):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", True)
    monkeypatch.setitem(get_settings().config, "publish_output", True)


def test_flag_off_touches_nothing(monkeypatch):
    monkeypatch.setitem(get_settings().config, "enable_auto_approval", False)
    reviewer = _reviewer()

    reviewer.auto_approve_logic()

    reviewer.git_provider.auto_approve.assert_not_called()
    reviewer.git_provider.revoke_auto_approval.assert_not_called()
    reviewer.git_provider.publish_comment.assert_not_called()


def test_clean_run_approves_with_the_verdict_in_the_body(approval_on):
    reviewer = _reviewer()
    reviewer.git_provider.auto_approve.return_value = True

    reviewer.auto_approve_logic()

    reviewer.git_provider.auto_approve.assert_called_once()
    assert "no_concerns_found" in reviewer.git_provider.auto_approve.call_args.kwargs["body"]
    reviewer.git_provider.revoke_auto_approval.assert_not_called()


@pytest.mark.parametrize(
    ("verdict", "model_fields", "partial_error"),
    [
        ("needs_review", ("no_concerns_found", "low"), None),
        (None, ("no_concerns_found", "low"), None),
        ("no_concerns_found", (None, "low"), None),
        ("no_concerns_found", ("no_concerns_found", "low"), RuntimeError("fallbacks exhausted")),
    ],
)
def test_non_clean_run_dismisses_an_earlier_approval(approval_on, verdict, model_fields, partial_error):
    reviewer = _reviewer(verdict=verdict, model_fields=model_fields)

    reviewer.auto_approve_logic(partial_error)

    reviewer.git_provider.auto_approve.assert_not_called()
    reviewer.git_provider.revoke_auto_approval.assert_called_once()


def test_refused_approval_dismisses(approval_on):
    reviewer = _reviewer()
    reviewer.git_provider.auto_approve.return_value = False

    reviewer.auto_approve_logic()

    reviewer.git_provider.revoke_auto_approval.assert_called_once()


def test_provider_error_is_logged_not_raised(approval_on):
    reviewer = _reviewer(verdict="needs_review")
    reviewer.git_provider.revoke_auto_approval.side_effect = RuntimeError("boom")

    reviewer.auto_approve_logic()


@pytest.mark.asyncio
async def test_run_reconciles_once_on_the_path_that_does_not_publish(monkeypatch, approval_on):
    provider = MagicMock()
    provider.get_files.return_value = ["app.py"]
    provider.auto_approve.return_value = True
    reviewer = _reviewer(provider=provider)
    reviewer.vars = {}
    reviewer.prediction = None

    def prepare():
        reviewer._merge_recommendation_status = "no_concerns_found"
        reviewer._approval_model_fields = ("no_concerns_found", "low")
        return "No major issues detected"

    reviewer._prepare_pr_review = prepare

    async def fake_retry(prepare_fn, model_type=None, git_provider=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", fake_retry)
    monkeypatch.setitem(get_settings().config, "is_auto_command", False)
    monkeypatch.setitem(get_settings().pr_reviewer, "publish_output_no_suggestions", False)

    await reviewer.run()

    provider.auto_approve.assert_called_once()
    provider.revoke_auto_approval.assert_not_called()


@pytest.mark.asyncio
async def test_failed_run_does_not_touch_approvals(monkeypatch, approval_on):
    provider = MagicMock()
    provider.get_files.side_effect = RuntimeError("provider down")
    reviewer = _reviewer(provider=provider)
    reviewer.vars = {}
    monkeypatch.setitem(get_settings().pr_reviewer, "publish_review_failure_comment", False)

    await reviewer.run()

    provider.auto_approve.assert_not_called()
    provider.revoke_auto_approval.assert_not_called()
