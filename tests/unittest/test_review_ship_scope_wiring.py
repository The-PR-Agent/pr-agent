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
    "pr_reviewer.low_priority_max_tokens_per_file",
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
    reviewer._ship_scope_summary_paths = []
    reviewer._ship_scope_ignore_footer = ""
    reviewer.set_review_labels = MagicMock()
    return reviewer


def _render_review(reviewer):
    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value={"review": {"summary": "test"}}),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch(
            "pr_agent.tools.pr_reviewer.convert_to_markdown_v2",
            return_value="## PR Reviewer Guide 🔍\n\nbody text",
        ),
    ):
        return reviewer._prepare_pr_review()


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
    # The per-file cap is exercised by its own tests; keep it out of the way here.
    get_settings().set("pr_reviewer.low_priority_max_tokens_per_file", 0)
    yield
    restore_settings(snapshot)


@pytest.mark.asyncio
async def test_chunked_review_orders_high_priority_first_and_summarizes_without_ignore(
    ship_scope_settings,
):
    """Summarized-only remaining low-priority files get a one-line list, not an [ignore] snippet."""
    design = _file("design/a.html")
    lib = _file("lib/a.dart")
    reviewer = _make_reviewer([design, lib])
    reviewer._get_review_data = AsyncMock(
        return_value=("raw", {"review": {"score": "80", "key_issues_to_review": []}}, 0)
    )

    plans = [
        ChunkPlan(diff="lib-diff-a", files=("lib/a.dart",), clipped=()),
        ChunkPlan(diff="lib-diff-b", files=("lib/a.dart",), clipped=()),
    ]

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["design/a.html"])),
        patch(
            "pr_agent.tools.pr_reviewer.get_pr_multi_diffs_with_files",
            return_value=(plans, ["design/a.html"]),
        ) as get_multi,
    ):
        await reviewer._prepare_prediction("model")

    assert get_multi.call_args.kwargs.get("preserve_order") is True
    passed = get_multi.call_args.kwargs.get("diff_files")
    assert [f.filename for f in passed] == ["lib/a.dart", "design/a.html"]
    assert reviewer.coverage.files["design/a.html"].status == "low_priority_summary"

    review = _render_review(reviewer)
    assert "design/a.html" in review
    assert "(low-priority file, not reviewed)" in review
    assert "[ignore]" not in review


@pytest.mark.asyncio
async def test_ignore_proposal_only_when_low_priority_file_was_in_a_reviewed_chunk(
    ship_scope_settings,
):
    design = _file("design/a.html")
    lib = _file("lib/a.dart")
    reviewer = _make_reviewer([design, lib])
    reviewer._get_review_data = AsyncMock(
        return_value=("raw", {"review": {"score": "80", "key_issues_to_review": []}}, 0)
    )

    plans = [
        ChunkPlan(diff="lib-diff", files=("lib/a.dart",), clipped=()),
        ChunkPlan(diff="design-diff", files=("design/a.html",), clipped=()),
    ]

    with (
        # Non-empty remaining from the single-call probe is what opens the chunked path.
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["design/a.html"])),
        patch(
            "pr_agent.tools.pr_reviewer.get_pr_multi_diffs_with_files",
            return_value=(plans, []),
        ),
    ):
        await reviewer._prepare_prediction("model")

    review = _render_review(reviewer)
    assert "[ignore]" in review
    assert 'glob = ["design/**"]' in review


@pytest.fixture
def per_file_cap(ship_scope_settings):
    """Ship-scope settings with the per-file low-priority cap at 50 tokens."""
    get_settings().set("pr_reviewer.low_priority_max_tokens_per_file", 50)
    yield


@pytest.mark.asyncio
async def test_the_per_file_cap_keeps_an_oversized_low_priority_file_out_of_the_chunks(per_file_cap):
    """R-9's target: a large design file is summarized even though the budget never bound, so it
    cannot take a share of the run's tokens for output that does not ship."""
    design = _file("design/a.html")
    lib = _file("lib/a.dart")
    reviewer = _make_reviewer([design, lib])
    reviewer.token_handler.count_tokens.side_effect = (
        lambda patch, *a, **kw: 900 if patch is design.patch else 10
    )
    reviewer._get_review_data = AsyncMock(
        return_value=("raw", {"review": {"score": "80", "key_issues_to_review": []}}, 0)
    )
    plans = [
        ChunkPlan(diff="lib-diff-a", files=("lib/a.dart",), clipped=()),
        ChunkPlan(diff="lib-diff-b", files=("lib/a.dart",), clipped=()),
    ]

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["lib/a.dart"])),
        patch(
            "pr_agent.tools.pr_reviewer.get_pr_multi_diffs_with_files",
            return_value=(plans, []),
        ) as get_multi,
    ):
        await reviewer._prepare_prediction("model")

    assert [f.filename for f in get_multi.call_args.kwargs["diff_files"]] == ["lib/a.dart"]
    assert reviewer.coverage.files["design/a.html"].status == "low_priority_summary"

    review = _render_review(reviewer)
    assert "design/a.html" in review
    assert "(low-priority file, not reviewed)" in review


@pytest.mark.asyncio
async def test_the_per_file_cap_also_applies_on_the_single_call_path(per_file_cap):
    """The single-call path is what a medium PR takes, and what the chunked path falls back to
    when the diff turns out to fit one chunk; the cap has to hold there too."""
    design = _file("design/a.html")
    lib = _file("lib/a.dart")
    reviewer = _make_reviewer([design, lib])
    reviewer.token_handler.count_tokens.side_effect = (
        lambda patch, *a, **kw: 900 if patch is design.patch else 10
    )
    reviewer._get_review_data = AsyncMock(
        return_value=("raw", {"review": {"score": "80", "key_issues_to_review": []}}, 0)
    )

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])) as get_pr_diff:
        await reviewer._prepare_prediction("model")

    assert [f.filename for f in get_pr_diff.call_args.kwargs["diff_files"]] == ["lib/a.dart"]
    assert reviewer.coverage.files["design/a.html"].status == "low_priority_summary"
    assert reviewer._ship_scope_summary_paths == ["design/a.html"]


@pytest.mark.asyncio
async def test_a_capped_file_is_still_reported_when_chunking_falls_back_to_one_call(per_file_cap):
    """`len(plans) < 2` sends the review back to the single-call flow. The capped file is out of
    that diff too, so its ledger mark and footer line have to survive the fallback - "nothing is
    excluded without being reported" is the invariant the whole cap rests on."""
    design = _file("design/a.html")
    lib = _file("lib/a.dart")
    reviewer = _make_reviewer([design, lib])
    reviewer.token_handler.count_tokens.side_effect = (
        lambda patch, *a, **kw: 900 if patch is design.patch else 10
    )
    reviewer._get_review_data = AsyncMock(
        return_value=("raw", {"review": {"score": "80", "key_issues_to_review": []}}, 0)
    )

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["lib/a.dart"])),
        patch(
            "pr_agent.tools.pr_reviewer.get_pr_multi_diffs_with_files",
            return_value=([ChunkPlan(diff="only-chunk", files=("lib/a.dart",), clipped=())], []),
        ),
    ):
        await reviewer._prepare_prediction("model")

    assert reviewer.review_chunk_count == 1  # the single-call flow ran
    assert reviewer.coverage.files["design/a.html"].status == "low_priority_summary"
    review = _render_review(reviewer)
    assert "design/a.html" in review
    assert "(low-priority file, not reviewed)" in review
