"""An unparsable review must fail the model, not the whole run.

load_yaml returns {} for output its repair heuristics cannot rescue. Before this was raised
inside the retried callable, the empty result only surfaced in _prepare_pr_review - long after
retry_with_fallback_models had returned - so a single malformed response discarded the review
without any fallback model being tried. Models that struggle with structured output fail this
way rather than by erroring, so the transport-level retry never covered it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.tools.pr_reviewer import PRReviewer

VALID_REVIEW = 'review:\n  score: "90"\n'


def _make_reviewer():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.token_handler = MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.prediction = None
    return reviewer


@pytest.mark.parametrize("unparsable", [
    "not yaml at all",
    "review: {}",          # parses, but carries no review
    "review: a string",    # parses, but review is not a mapping
    "",
])
@pytest.mark.asyncio
async def test_a_prediction_that_cannot_be_parsed_raises_so_the_fallback_chain_gets_a_turn(unparsable):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=unparsable)

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])),
        pytest.raises(ValueError, match="Failed to parse the review"),
    ):
        await reviewer._prepare_prediction("model")


@pytest.mark.asyncio
async def test_a_parsable_prediction_is_parsed_once_and_handed_on():
    """The single call parses here, so _prepare_pr_review does not parse the same text again."""
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=VALID_REVIEW)

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])):
        await reviewer._prepare_prediction("model")

    assert reviewer.prediction == VALID_REVIEW
    assert reviewer.prediction_data == reviewer._load_review_yaml(VALID_REVIEW)
    assert reviewer.review_chunk_count == 1


@pytest.mark.asyncio
async def test_an_empty_diff_is_not_treated_as_an_unparsable_review():
    """No diff means no prediction to validate; that path must not raise."""
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=VALID_REVIEW)

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("", [])):
        await reviewer._prepare_prediction("model")

    assert reviewer.prediction is None
    reviewer._get_prediction.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_retry_does_not_inherit_the_previous_models_chunk_bookkeeping():
    reviewer = _make_reviewer()
    reviewer.prediction_data = {"review": {"score": "10"}}
    reviewer.review_chunk_count = 4
    reviewer.review_failed_chunk_count = 2
    reviewer._get_prediction = AsyncMock(return_value=VALID_REVIEW)

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])):
        await reviewer._prepare_prediction("model")

    assert reviewer.prediction_data == reviewer._load_review_yaml(VALID_REVIEW)
    assert reviewer.review_chunk_count == 1
    assert reviewer.review_failed_chunk_count == 0
    assert reviewer.review_vote_dropped_count == 0


@pytest.mark.asyncio
async def test_the_raise_leaves_persistent_finding_state_untouched(monkeypatch):
    """Raising before the publish block is what preserves prior findings.

    The old path reached _prepare_pr_review, which set _review_state_blocked to keep the stored
    state; now the failure propagates out of run()'s try, so the persistent publish never runs at
    all. Nothing is written either way - this pins that, because a write here would resolve real
    findings on the strength of a review that was never parsed.
    """
    from pr_agent.config_loader import get_settings
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    reviewer = _make_reviewer()
    reviewer.git_provider = git_provider
    reviewer.vars = {}

    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(
        pr_reviewer_module,
        "retry_with_fallback_models",
        AsyncMock(side_effect=ValueError("Failed to parse the review produced by model")),
    )

    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "is_auto_command": settings.config.get("is_auto_command", False),
        "propagate_tool_errors": settings.config.get("propagate_tool_errors", False),
        "persistent_comment": settings.pr_reviewer.get("persistent_comment", True),
        "persistent_finding_state": settings.pr_reviewer.get("persistent_finding_state", True),
    }
    try:
        settings.config.publish_output = True
        settings.config.is_auto_command = False
        settings.config.propagate_tool_errors = False
        settings.pr_reviewer.persistent_comment = True
        settings.pr_reviewer.persistent_finding_state = True

        await reviewer.run()
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.config.is_auto_command = original["is_auto_command"]
        settings.config.propagate_tool_errors = original["propagate_tool_errors"]
        settings.pr_reviewer.persistent_comment = original["persistent_comment"]
        settings.pr_reviewer.persistent_finding_state = original["persistent_finding_state"]

    git_provider.publish_persistent_comment.assert_not_called()
    git_provider.publish_persistent_comment_full.assert_not_called()
