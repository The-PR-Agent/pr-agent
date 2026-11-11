"""PRReviewer reconciles the auto-approval once per /review run (issue #4698)."""

import asyncio
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

    reviewer.auto_approve_logic(review_completed=True)

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

    reviewer.auto_approve_logic(partial_error, review_completed=True)

    reviewer.git_provider.auto_approve.assert_not_called()
    reviewer.git_provider.revoke_auto_approval.assert_called_once()


def test_refused_approval_dismisses(approval_on):
    reviewer = _reviewer()
    reviewer.git_provider.auto_approve.return_value = False

    reviewer.auto_approve_logic(review_completed=True)

    reviewer.git_provider.revoke_auto_approval.assert_called_once()


def test_approval_error_still_dismisses(approval_on):
    reviewer = _reviewer()
    reviewer.git_provider.auto_approve.side_effect = RuntimeError("unexpected")

    reviewer.auto_approve_logic(review_completed=True)

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

    # The earlier approval is withdrawn when the run starts, and the new one is given only at its end.
    assert [name for name, *_ in provider.mock_calls
            if name in ("revoke_auto_approval", "auto_approve")] == ["revoke_auto_approval", "auto_approve"]
    assert "a new review started" in provider.revoke_auto_approval.call_args.args[0]


def test_files_left_out_of_the_review_dismiss(approval_on):
    reviewer = _reviewer()
    reviewer.remaining_files_list = ["big.py"]

    reviewer.auto_approve_logic(review_completed=True)

    reviewer.git_provider.auto_approve.assert_not_called()
    reviewer.git_provider.revoke_auto_approval.assert_called_once()


def test_dry_run_writes_nothing(monkeypatch, approval_on):
    monkeypatch.setitem(get_settings().config, "publish_output", False)
    reviewer = _reviewer(verdict="needs_review")

    reviewer.auto_approve_logic(review_completed=True)

    reviewer.git_provider.auto_approve.assert_not_called()
    reviewer.git_provider.revoke_auto_approval.assert_not_called()


@pytest.mark.asyncio
async def test_failed_run_withdraws_an_earlier_approval(monkeypatch, approval_on):
    provider = MagicMock()
    provider.get_files.side_effect = RuntimeError("provider down")
    reviewer = _reviewer(provider=provider)
    reviewer.vars = {}
    monkeypatch.setitem(get_settings().pr_reviewer, "publish_review_failure_comment", False)

    await reviewer.run()

    provider.auto_approve.assert_not_called()
    # Once when the run starts, and again when it ends without a clean verdict.
    assert provider.revoke_auto_approval.call_count == 2


@pytest.mark.asyncio
async def test_cancelled_publish_does_not_approve(monkeypatch, approval_on):
    provider = MagicMock()
    provider.get_files.return_value = ["app.py"]
    provider.should_publish_review_as_thread.return_value = False
    provider.publish_comment.side_effect = asyncio.CancelledError()
    reviewer = _reviewer(provider=provider)
    reviewer.vars = {}
    reviewer.prediction = None

    def prepare():
        reviewer._merge_recommendation_status = "no_concerns_found"
        reviewer._approval_model_fields = ("no_concerns_found", "low")
        return "A major issue was detected"

    reviewer._prepare_pr_review = prepare
    reviewer._should_publish_review_no_suggestions = lambda _: True

    async def fake_retry(prepare_fn, model_type=None, git_provider=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", fake_retry)
    monkeypatch.setitem(get_settings().pr_reviewer, "persistent_comment", False)

    with pytest.raises(asyncio.CancelledError):
        await reviewer.run()

    provider.auto_approve.assert_not_called()
    assert provider.revoke_auto_approval.call_count == 2
