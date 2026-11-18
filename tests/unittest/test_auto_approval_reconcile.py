"""PRReviewer reconciles the auto-approval once per /review run (issue #4698)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
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


@pytest.mark.parametrize(
    ("overrides", "expected_verdict"),
    [
        pytest.param({}, "no_concerns_found", id="clean"),
        pytest.param({"ci": "failed"}, "needs_review", id="failed-ci"),
        pytest.param({"ci": None}, "needs_review", id="unknown-ci"),
        pytest.param({"stale": True}, "needs_review", id="stale-head"),
        pytest.param({"human": False}, "needs_review", id="bot-author"),
        pytest.param({"filename": ".github/workflows/ci.yml"}, "needs_review", id="sensitive-path"),
        pytest.param({"patch": "@@ -1 +0,0 @@\n-    def run(self):"}, "needs_review", id="removed-method"),
        pytest.param({"edit_type": EDIT_TYPE.DELETED}, "needs_review", id="deleted-file"),
        pytest.param({"added": 101}, "needs_review", id="oversized-diff"),
        pytest.param({"recommendation": "needs_review"}, "needs_review", id="model-needs-review"),
        pytest.param({"risk": "high"}, "changes_required", id="model-high-risk"),
        pytest.param({"enable_approval": False}, "no_concerns_found", id="approval-off"),
        pytest.param({"publish": False}, "no_concerns_found", id="dry-run"),
        pytest.param({"require_verdict": False}, None, id="verdict-off"),
    ],
)
@pytest.mark.asyncio
async def test_run_reconciles_auto_approval_from_computed_verdict(monkeypatch, overrides, expected_verdict):
    settings = get_settings()
    publish = overrides.get("publish", True)
    enable_approval = overrides.get("enable_approval", True)
    require_verdict = overrides.get("require_verdict", True)
    monkeypatch.setitem(settings.config, "publish_output", publish)
    monkeypatch.setitem(settings.config, "enable_auto_approval", enable_approval)
    monkeypatch.setitem(settings.config, "is_auto_command", True)
    monkeypatch.setitem(settings, "data", {})
    monkeypatch.setitem(settings.pr_reviewer, "require_merge_recommendation", require_verdict)
    monkeypatch.setitem(settings.pr_reviewer, "max_changed_lines", 100)
    monkeypatch.setitem(settings.pr_reviewer, "persistent_comment", False)
    monkeypatch.setitem(settings.pr_reviewer, "publish_output_no_suggestions", True)
    monkeypatch.setitem(settings.pr_reviewer, "publish_merge_recommendation_status", False)
    monkeypatch.setitem(settings.pr_reviewer, "require_ticket_analysis_review", False)

    file = FilePatchInfo(
        base_file="",
        head_file="",
        patch=overrides.get("patch", "@@ -0,0 +1 @@\n+value = 1"),
        filename=overrides.get("filename", "app.py"),
        edit_type=overrides.get("edit_type", EDIT_TYPE.MODIFIED),
        num_plus_lines=overrides.get("added", 1),
        num_minus_lines=0,
    )
    provider = MagicMock()
    provider.get_files.return_value = [file.filename]
    provider.get_pr_file_paths.return_value = [file.filename]
    provider.get_diff_files.return_value = [file]
    provider.get_verdict_diff_files.return_value = [file]
    provider.get_head_ci_status.return_value = overrides.get("ci", "passed")
    provider.is_head_stale_against_base.return_value = overrides.get("stale", False)
    provider.are_all_authors_human.return_value = overrides.get("human", True)
    provider.should_publish_review_as_thread.return_value = False
    provider.auto_approve.return_value = True
    provider.revoke_auto_approval.return_value = True
    reviewer = _reviewer(verdict=None, model_fields=(None, None), provider=provider)
    reviewer.vars = {"require_merge_recommendation": require_verdict}
    reviewer.remaining_files_list = []

    async def reply(*args, **kwargs):
        reviewer.prediction = (
            "review:\n"
            f"  merge_recommendation: {overrides.get('recommendation', 'no_concerns_found')}\n"
            f"  risk_level: {overrides.get('risk', 'low')}\n"
            "  key_issues_to_review: []\n"
        )

    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", reply)

    await reviewer.run()

    assert reviewer._review_done
    assert reviewer._merge_recommendation_status == expected_verdict
    assert reviewer._review_published is publish
    writes = [
        name for name, *_ in provider.mock_calls
        if name in {"publish_comment", "auto_approve", "revoke_auto_approval"}
    ]
    if not publish:
        assert writes == []
    elif not enable_approval:
        assert writes == ["publish_comment"]
    elif expected_verdict == "no_concerns_found":
        assert writes == ["revoke_auto_approval", "publish_comment", "auto_approve"]
        assert "no_concerns_found" in provider.auto_approve.call_args.kwargs["body"]
    else:
        assert writes == ["revoke_auto_approval", "publish_comment", "revoke_auto_approval"]


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
