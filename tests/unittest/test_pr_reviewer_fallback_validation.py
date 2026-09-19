from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from pr_agent.algo.pr_processing import retry_with_fallback_models
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = (
    "config.model",
    "config.fallback_models",
    "config.publish_output",
    "config.is_auto_command",
    "config.propagate_tool_errors",
    "openai.deployment_id",
    "openai.fallback_deployments",
    "pr_reviewer.persistent_comment",
)

_VALID_REVIEW = """review:
  summary: fallback review
"""


@pytest.fixture
def fallback_models():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    settings = get_settings()
    settings.set("config.model", "primary-model")
    settings.set("config.fallback_models", ["fallback-model"])
    settings.set("openai.deployment_id", None)
    settings.set("openai.fallback_deployments", [])
    yield
    restore_settings(snapshot)


def _make_reviewer(git_provider=None):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = git_provider or MagicMock()
    reviewer.token_handler = MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.prediction = None
    reviewer.vars = {}
    return reviewer


@pytest.mark.asyncio
async def test_malformed_primary_review_uses_fallback_model(fallback_models):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=["not valid review yaml", _VALID_REVIEW])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value="diff"),
        patch(
            "pr_agent.tools.pr_reviewer.fit_related_tickets_to_prompt_budget",
            side_effect=lambda _pr, raw_vars, _system, _user, _model, **_kwargs: (
                raw_vars,
                reviewer.token_handler,
            ),
        ),
    ):
        await retry_with_fallback_models(reviewer._prepare_prediction, git_provider=reviewer.git_provider)

    assert reviewer._get_prediction.await_args_list == [call("primary-model"), call("fallback-model")]
    assert reviewer.prediction == _VALID_REVIEW


@pytest.mark.asyncio
async def test_empty_primary_diff_uses_fallback_model(fallback_models):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=_VALID_REVIEW)

    def get_pr_diff(_provider, _handler, model, **_kwargs):
        if model == "primary-model":
            return "", ["src/too-large.py"]
        return "fallback diff", []

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", side_effect=get_pr_diff),
        patch(
            "pr_agent.tools.pr_reviewer.fit_related_tickets_to_prompt_budget",
            side_effect=lambda _pr, raw_vars, _system, _user, _model, **_kwargs: (
                raw_vars,
                reviewer.token_handler,
            ),
        ),
    ):
        await retry_with_fallback_models(
            reviewer._prepare_prediction,
            git_provider=reviewer.git_provider,
        )

    reviewer._get_prediction.assert_awaited_once_with("fallback-model")
    assert reviewer.patches_diff == "fallback diff"
    assert reviewer.prediction == _VALID_REVIEW


@pytest.mark.asyncio
async def test_malformed_primary_then_valid_fallback_publishes_review_without_failure(
    monkeypatch, fallback_models
):
    progress_comment = MagicMock()
    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    git_provider.publish_comment.return_value = progress_comment
    git_provider.should_publish_review_as_thread.return_value = False
    git_provider.supports_review_comment_identity.return_value = False
    reviewer = _make_reviewer(git_provider)
    reviewer._get_prediction = AsyncMock(side_effect=["not yaml", _VALID_REVIEW])
    reviewer._prepare_pr_review = MagicMock(return_value="rendered fallback review")

    settings = get_settings()
    settings.set("config.publish_output", True)
    settings.set("config.is_auto_command", False)
    settings.set("config.propagate_tool_errors", False)
    settings.set("pr_reviewer.persistent_comment", False)

    monkeypatch.setattr("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(
        "pr_agent.tools.pr_reviewer.fit_related_tickets_to_prompt_budget",
        lambda _pr, raw_vars, _system, _user, _model, **_kwargs: (raw_vars, reviewer.token_handler),
    )

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value="diff"):
        await reviewer.run()

    assert reviewer._get_prediction.await_args_list == [call("primary-model"), call("fallback-model")]
    assert git_provider.publish_comment.call_args_list == [
        call("Preparing review...", is_temporary=True),
        call("rendered fallback review"),
    ]
    assert all("Failed to review PR" not in str(args) for args in git_provider.publish_comment.call_args_list)
    git_provider.remove_comment.assert_called_once_with(progress_comment)


@pytest.mark.asyncio
async def test_all_malformed_models_publish_one_failure_result(monkeypatch, fallback_models):
    progress_comment = MagicMock()
    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    git_provider.publish_comment.return_value = progress_comment
    reviewer = _make_reviewer(git_provider)
    reviewer._get_prediction = AsyncMock(side_effect=["not yaml", "review: {}"])

    settings = get_settings()
    settings.set("config.publish_output", True)
    settings.set("config.is_auto_command", False)
    settings.set("config.propagate_tool_errors", False)

    monkeypatch.setattr("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(
        "pr_agent.tools.pr_reviewer.fit_related_tickets_to_prompt_budget",
        lambda _pr, raw_vars, _system, _user, _model, **_kwargs: (raw_vars, reviewer.token_handler),
    )

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value="diff"):
        await reviewer.run()

    assert reviewer._get_prediction.await_args_list == [call("primary-model"), call("fallback-model")]
    assert git_provider.publish_comment.call_args_list == [
        call("Preparing review...", is_temporary=True),
        call("Failed to review PR"),
    ]
    git_provider.remove_comment.assert_called_once_with(progress_comment)
