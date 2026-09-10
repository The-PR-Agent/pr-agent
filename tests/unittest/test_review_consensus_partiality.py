"""A finding the consensus vote discarded makes the review partial.

The vote drops a finding that too few samples agreed on. That finding is absent from the review
without having been fixed, so two things must follow, and neither did: the persistent finding
state must not resolve anything on this run, and the published review must say the review was
filtered rather than reading like a clean pass.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from pr_agent.algo.review_finding_state import ParsedReviewState, reconcile_review_findings
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


def _issue(file="app.py", start=10, end=12, content="unchecked index"):
    return {"relevant_file": file, "issue_header": "Possible Issue", "issue_content": content,
            "start_line": start, "end_line": end}


def _sample(*issues):
    return yaml.safe_dump({"review": {"key_issues_to_review": list(issues)}}, sort_keys=False)


def _make_reviewer():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.token_handler = MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.prediction = None
    return reviewer


@pytest.fixture
def sampling():
    settings = get_settings()
    keys = ("pr_reviewer.num_samples", "pr_reviewer.min_votes", "config.temperature",
            "pr_reviewer.num_max_findings", "pr_reviewer.persistent_finding_state")
    saved = {key: settings.get(key, None) for key in keys}
    settings.set("pr_reviewer.num_samples", 3)
    settings.set("pr_reviewer.min_votes", 2)
    settings.set("pr_reviewer.num_max_findings", 3)
    settings.set("config.temperature", 0.4)
    yield settings
    for key, value in saved.items():
        settings.set(key, value)


async def _run_vote(reviewer, samples):
    reviewer._get_prediction = AsyncMock(side_effect=samples)
    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])):
        await reviewer._prepare_prediction("model")


@pytest.mark.asyncio
async def test_an_outvoted_finding_is_counted_as_dropped(sampling):
    reviewer = _make_reviewer()
    await _run_vote(reviewer, [_sample(_issue(), _issue(file="lonely.py", start=1)),
                               _sample(_issue()), _sample(_issue())])
    assert reviewer.review_vote_dropped_count == 1


@pytest.mark.asyncio
async def test_a_unanimous_vote_drops_nothing(sampling):
    reviewer = _make_reviewer()
    await _run_vote(reviewer, [_sample(_issue())] * 3)
    assert reviewer.review_vote_dropped_count == 0


@pytest.mark.asyncio
async def test_a_dropped_finding_stops_the_run_resolving_the_previous_one(sampling):
    """The regression this branch exists for: an outvoted finding was published as fixed.

    Both the reported issues and the current findings are read from the post-vote review, so a
    finding that lost the vote appeared in neither - indistinguishable from one that was fixed.
    """
    reviewer = _make_reviewer()
    await _run_vote(reviewer, [_sample(_issue(), _issue(file="lonely.py", start=1)),
                               _sample(_issue()), _sample(_issue())])

    previous = reconcile_review_findings(
        None,
        [{"path": "lonely.py", "body": "a real bug", "line_start": 1, "line_end": 1}],
        allow_resolution=False,
        head_sha="head-1",
        timestamp="2026-09-09T00:00:00+00:00",
    ).state
    reviewer._review_finding_state_enabled = lambda: True
    reviewer._load_review_finding_state = lambda: ParsedReviewState(previous, present=True, valid=True)
    reviewer._review_head_sha = lambda: "head-2"
    reviewer._review_run_id = lambda: "run-2"

    reviewer._prepare_review_finding_state(reviewer.prediction_data)

    assert reviewer._review_state_result.resolved_ids == ()
    assert reviewer._review_state_result.state["findings"][0]["state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_a_run_that_dropped_nothing_still_resolves(sampling):
    """The guard has to be the drop, not the mere use of sampling."""
    reviewer = _make_reviewer()
    await _run_vote(reviewer, [_sample(_issue())] * 3)

    previous = reconcile_review_findings(
        None,
        [{"path": "gone.py", "body": "was fixed", "line_start": 1, "line_end": 1}],
        allow_resolution=False,
        head_sha="head-1",
        timestamp="2026-09-09T00:00:00+00:00",
    ).state
    reviewer._review_finding_state_enabled = lambda: True
    reviewer._load_review_finding_state = lambda: ParsedReviewState(previous, present=True, valid=True)
    reviewer._review_head_sha = lambda: "head-2"
    reviewer._review_run_id = lambda: "run-2"

    reviewer._prepare_review_finding_state(reviewer.prediction_data)

    assert len(reviewer._review_state_result.resolved_ids) == 1


def _render_review(reviewer):
    reviewer.prediction = "review:\n  summary: test"
    reviewer.git_provider.get_diff_files.return_value = []
    reviewer.git_provider.is_supported.return_value = False
    reviewer.set_review_labels = MagicMock()
    reviewer._review_finding_state_enabled = lambda: False
    reviewer._prepare_review_finding_state = lambda data: None
    reviewer._review_state_result = None

    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value={"review": {"summary": "test"}}),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch("pr_agent.tools.pr_reviewer.convert_to_markdown_v2", return_value="original review"),
    ):
        return reviewer._prepare_pr_review()


def test_the_review_says_when_the_vote_filtered_findings():
    """Otherwise a filtered review is indistinguishable from a clean PR."""
    reviewer = _make_reviewer()
    reviewer.prediction_data = None
    reviewer.review_vote_dropped_count = 2

    review = _render_review(reviewer)

    assert "ℹ️ **Consensus review:**" in review
    assert "2 candidate finding(s)" in review
    assert "min_votes" in review


def test_a_review_that_dropped_nothing_carries_no_consensus_footer():
    reviewer = _make_reviewer()
    reviewer.prediction_data = None

    assert "Consensus review" not in _render_review(reviewer)
