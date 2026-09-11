"""Ship-scope wiring: priority-ordered chunking, low_priority_summary ledger marks, footer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.pr_processing import ChunkPlan
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = (
    "pr_reviewer.enable_large_pr_chunking",
    "pr_reviewer.max_number_of_calls",
    "pr_reviewer.enable_review_coverage_footer",
    "pr_reviewer.low_priority_globs",
    "pr_reviewer.low_priority_summarize_when_over_budget",
)

_PATCH = """diff --git a/f b/f
--- a/f
+++ b/f
@@ -1 +1 @@
-old
+new
"""


def _file(name: str) -> FilePatchInfo:
    return FilePatchInfo(
        base_file="old\n",
        head_file="new\n",
        patch=_PATCH,
        filename=name,
        edit_type=EDIT_TYPE.MODIFIED,
        num_plus_lines=1,
        num_minus_lines=1,
    )


def _make_reviewer(files):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.git_provider.get_diff_files.return_value = files
    reviewer.git_provider.get_languages.return_value = {}
    reviewer.git_provider.is_supported.return_value = False
    reviewer.token_handler = MagicMock()
    reviewer.token_handler.count_tokens.return_value = 100
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.prediction = "review:\n  summary: test"
    reviewer.prediction_data = {"review": {"summary": "test"}}
    reviewer.coverage = None
    reviewer.chunk_plans = []
    reviewer.review_chunk_count = 1
    reviewer.review_failed_chunk_count = 0
    reviewer.review_vote_dropped_count = 0
    reviewer._review_state_result = None
    reviewer.set_review_labels = MagicMock()
    return reviewer


@pytest.fixture
def ship_scope_settings():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    get_settings().set("pr_reviewer.enable_large_pr_chunking", True)
    get_settings().set("pr_reviewer.max_number_of_calls", 3)
    get_settings().set("pr_reviewer.enable_review_coverage_footer", True)
    get_settings().set(
        "pr_reviewer.low_priority_globs",
        ["docs/**", "design/**", "mockups/**", "**/fixtures/**", "**/*.md"],
    )
    get_settings().set("pr_reviewer.low_priority_summarize_when_over_budget", True)
    yield
    restore_settings(snapshot)


@pytest.mark.asyncio
async def test_chunked_review_orders_high_priority_first_and_summarizes_low_priority_remaining(
    ship_scope_settings,
):
    design = _file("design/a.html")
    lib = _file("lib/a.dart")
    reviewer = _make_reviewer([design, lib])
    reviewer._get_prediction = AsyncMock(return_value="review:\n  score: \"80\"\n  key_issues_to_review: []\n")
    reviewer._get_review_data = AsyncMock(
        return_value=("raw", {"review": {"score": "80", "key_issues_to_review": []}}, 0)
    )

    # Chunking only engages when there are at least two plans.
    plans = [
        ChunkPlan(diff="lib-diff", files=("lib/a.dart",), clipped=()),
        ChunkPlan(diff="other-diff", files=("lib/a.dart",), clipped=()),
    ]

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["design/a.html"])),
        patch(
            "pr_agent.tools.pr_reviewer.get_pr_multi_diffs_with_files",
            return_value=(plans, ["design/a.html"]),
        ) as get_multi,
    ):
        await reviewer._prepare_prediction("model")

    passed = get_multi.call_args.kwargs.get("diff_files")
    assert passed is not None
    assert [f.filename for f in passed] == ["lib/a.dart", "design/a.html"]
    assert reviewer.coverage.files["design/a.html"].status == "low_priority_summary"

    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value={"review": {"summary": "test"}}),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch(
            "pr_agent.tools.pr_reviewer.convert_to_markdown_v2",
            return_value="## PR Reviewer Guide 🔍\n\nbody text",
        ),
    ):
        review = reviewer._prepare_pr_review()

    assert "design/a.html" in review
    assert "(mockup, not reviewed)" in review
    assert "[ignore]" in review
