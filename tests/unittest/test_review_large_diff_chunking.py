"""The opt-in gate and wiring of the chunked `/review` flow.

The merge rules themselves live in tests/unittest/test_review_chunk_merge.py; what is
covered here is when chunking runs at all, what it does with a chunk that fails, and what
the published review says about having been assembled from several calls.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = ("pr_reviewer.enable_large_pr_chunking", "pr_reviewer.max_number_of_calls")

CHUNK_A = """review:
  score: "90"
  key_issues_to_review:
    - relevant_file: |
        a.py
      issue_header: |
        Possible Issue
      issue_content: |
        the index is never checked
      start_line: 3
      end_line: 4
  security_concerns: |
    No
"""

CHUNK_B = """review:
  score: "40"
  key_issues_to_review: []
  security_concerns: |
    SQL injection: the query is built by string concatenation
"""


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
def chunking_enabled():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    get_settings().set("pr_reviewer.enable_large_pr_chunking", True)
    get_settings().set("pr_reviewer.max_number_of_calls", 3)
    yield
    restore_settings(snapshot)


@pytest.mark.asyncio
async def test_chunking_is_off_by_default_even_when_the_token_budget_truncated_the_diff():
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=CHUNK_A)

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["left_out.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs") as get_pr_multi_diffs,
    ):
        await reviewer._prepare_prediction("model")

    get_pr_multi_diffs.assert_not_called()
    assert reviewer.prediction == CHUNK_A
    assert reviewer.prediction_data is None
    assert reviewer.review_chunk_count == 1


@pytest.mark.asyncio
async def test_a_diff_that_fits_is_never_chunked(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=CHUNK_A)

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs") as get_pr_multi_diffs,
    ):
        await reviewer._prepare_prediction("model")

    get_pr_multi_diffs.assert_not_called()
    assert reviewer.review_chunk_count == 1


@pytest.mark.asyncio
async def test_a_truncated_diff_is_reviewed_chunk_by_chunk_and_merged(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, CHUNK_B])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b"], ["still_left_out.py"])) as get_pr_multi_diffs,
    ):
        await reviewer._prepare_prediction("model")

    get_pr_multi_diffs.assert_called_once_with(
        reviewer.git_provider,
        reviewer.token_handler,
        "model",
        max_calls=3,
        add_line_numbers=True,
        return_remaining_files=True,
    )
    assert [call.args[1] for call in reviewer._get_prediction.await_args_list] == ["chunk-a", "chunk-b"]

    review = reviewer.prediction_data["review"]
    assert review["score"] == "40"  # the worst chunk sets the score
    assert [issue["relevant_file"].strip() for issue in review["key_issues_to_review"]] == ["a.py"]
    assert review["security_concerns"].startswith("SQL injection:")
    assert reviewer.review_chunk_count == 2
    assert reviewer.review_failed_chunk_count == 0
    # the coverage footer keeps reporting what even chunking could not fit
    assert reviewer.remaining_files_list == ["still_left_out.py"]


@pytest.mark.asyncio
async def test_max_number_of_calls_bounds_the_number_of_chunks(chunking_enabled):
    get_settings().set("pr_reviewer.max_number_of_calls", 7)
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, CHUNK_B])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b"], [])) as get_pr_multi_diffs,
    ):
        await reviewer._prepare_prediction("model")

    assert get_pr_multi_diffs.call_args.kwargs["max_calls"] == 7


@pytest.mark.asyncio
async def test_a_diff_that_fits_in_one_chunk_is_reviewed_by_the_single_call_flow(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=CHUNK_A)

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs", return_value=(["only-chunk"], [])),
    ):
        await reviewer._prepare_prediction("model")

    reviewer._get_prediction.assert_awaited_once_with("model")
    assert reviewer.prediction == CHUNK_A
    assert reviewer.prediction_data is None
    assert reviewer.review_chunk_count == 1
    assert reviewer.remaining_files_list == ["b.py"]


@pytest.mark.asyncio
async def test_a_chunk_that_fails_does_not_lose_the_chunks_that_succeeded(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("model refused"), CHUNK_B])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b"], [])),
    ):
        await reviewer._prepare_prediction("model")

    assert reviewer.prediction_data["review"]["score"] == "40"
    assert reviewer.review_chunk_count == 2
    assert reviewer.review_failed_chunk_count == 1


@pytest.mark.asyncio
async def test_a_review_where_every_chunk_failed_raises_so_a_fallback_model_is_tried(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("model refused"),
                                                      RuntimeError("model refused again")])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b"], [])),
        pytest.raises(RuntimeError, match="model refused"),
    ):
        await reviewer._prepare_prediction("model")


@pytest.mark.asyncio
async def test_chunks_that_answer_nothing_parsable_fall_back_to_a_single_call_review(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=["not yaml at all", "nor is this", CHUNK_A])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b"], [])),
    ):
        await reviewer._prepare_prediction("model")

    assert reviewer._get_prediction.await_count == 3
    assert reviewer.prediction == CHUNK_A
    assert reviewer.prediction_data is None


def _render_review(reviewer):
    reviewer.prediction = "review: {}"
    reviewer.git_provider.get_diff_files.return_value = []
    reviewer.git_provider.is_supported.return_value = False
    reviewer.set_review_labels = MagicMock()

    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value={"review": {}}),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch("pr_agent.tools.pr_reviewer.convert_to_markdown_v2", return_value="original review"),
    ):
        return reviewer._prepare_pr_review()


def test_a_chunked_review_says_how_many_chunks_it_was_built_from():
    reviewer = _make_reviewer()
    reviewer.review_chunk_count = 3

    review = _render_review(reviewer)

    assert review.startswith("original review")
    assert "ℹ️ **Chunked review:**" in review
    assert "reviewed in 3 chunks" in review
    assert "failed" not in review


def test_a_chunked_review_reports_the_chunks_that_failed():
    reviewer = _make_reviewer()
    reviewer.review_chunk_count = 3
    reviewer.review_failed_chunk_count = 1

    review = _render_review(reviewer)

    assert "1 chunk(s) failed and are not covered by this review." in review


def test_a_single_call_review_says_nothing_about_chunks():
    review = _render_review(_make_reviewer())

    assert review == "original review"


def test_the_chunk_note_comes_before_the_review_coverage_footer():
    reviewer = _make_reviewer()
    reviewer.review_chunk_count = 2
    reviewer.remaining_files_list = ["left_out.py"]
    snapshot = snapshot_settings(("pr_reviewer.enable_review_coverage_footer",))
    try:
        get_settings().set("pr_reviewer.enable_review_coverage_footer", True)
        review = _render_review(reviewer)
    finally:
        restore_settings(snapshot)

    assert review.index("Chunked review:") < review.index("⚠️ **Review coverage:**")
    assert "- `left_out.py`" in review
