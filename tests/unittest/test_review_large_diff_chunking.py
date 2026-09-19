"""The opt-in gate and wiring of the chunked `/review` flow.

The merge rules themselves live in tests/unittest/test_review_chunk_merge.py; what is
covered here is when chunking runs at all, what it does with a chunk that fails, and what
the published review says about having been assembled from several calls.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.pr_processing import PreparedPRDiff
from pr_agent.algo.review_finding_state import ParsedReviewState, reconcile_review_findings
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = ("pr_reviewer.enable_large_pr_chunking", "pr_reviewer.max_number_of_calls")

CHUNK_A = """review:
  score: 90
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
  score: 40
  key_issues_to_review: []
  security_concerns: |
    SQL injection: the query is built by string concatenation
"""


def _make_reviewer():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.ai_handler = MagicMock()
    reviewer.token_handler = MagicMock()
    reviewer.token_handler.prompt_tokens = 0
    reviewer.token_handler.count_tokens.side_effect = len
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
    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=10000):
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
        output_token_reserve=reviewer.ai_handler.get_output_token_reserve,
    )
    assert [call.args[1] for call in reviewer._get_prediction.await_args_list] == ["chunk-a", "chunk-b"]

    review = reviewer.prediction_data["review"]
    assert review["score"] == 40  # the worst chunk sets the score
    assert [issue["relevant_file"].strip() for issue in review["key_issues_to_review"]] == ["a.py"]
    assert review["security_concerns"].startswith("SQL injection:")
    assert reviewer.review_chunk_count == 2
    assert reviewer.review_failed_chunk_count == 0
    # the coverage footer keeps reporting what even chunking could not fit
    assert reviewer.remaining_files_list == ["still_left_out.py"]


@pytest.mark.asyncio
async def test_final_fit_clipping_marks_a_review_chunk_failed(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._raw_prompt_vars = None
    reviewer.vars = {"diff": ""}
    reviewer.ai_handler.chat_completion = AsyncMock(return_value=(CHUNK_A, "stop"))

    class SelectiveBudget:
        def fit_prompt_variable(self, _variables, _name, optional_text, **_kwargs):
            fitted_text = optional_text[:-1] if optional_text == "chunk-b" else optional_text
            return SimpleNamespace(
                optional_text=fitted_text,
                system_prompt="system",
                user_prompt="user",
            )

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch(
            "pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
            return_value=(["chunk-a", "chunk-b"], []),
        ),
        patch(
            "pr_agent.tools.pr_reviewer.AttemptTokenBudget.for_attempt",
            return_value=SelectiveBudget(),
        ),
        pytest.raises(ValueError, match="complete packed review diff"),
    ):
        await reviewer._prepare_prediction("model")

    assert list(reviewer._chunked_results) == [0]
    assert reviewer.prediction is None
    reviewer.ai_handler.chat_completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_chunked_review_reuses_the_prepared_diff_for_the_same_model_attempt(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, CHUNK_B])
    prepared = PreparedPRDiff(
        diff="first compressed diff",
        remaining_files_list=["b.py"],
        file_dict={"a.py": {"patch": "chunk-a", "tokens": 10}},
    )

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=prepared) as get_pr_diff,
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b"], ["still_left_out.py"])) as get_pr_multi_diffs,
    ):
        await reviewer._prepare_prediction("model")

    assert get_pr_diff.call_args.kwargs["return_prepared"] is True
    assert get_pr_multi_diffs.call_args.kwargs["prepared_diff"] is prepared
    assert get_pr_diff.call_args.kwargs["output_token_reserve"] is reviewer.ai_handler.get_output_token_reserve
    assert get_pr_multi_diffs.call_args.kwargs["output_token_reserve"] is reviewer.ai_handler.get_output_token_reserve
    assert reviewer.review_chunk_count == 2
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
        pytest.raises(RuntimeError, match="model refused"),
    ):
        await reviewer._prepare_prediction("model")

    reviewer._get_prediction.side_effect = [CHUNK_A]
    await reviewer._prepare_chunked_prediction("model")

    assert reviewer.prediction_data["review"]["score"] == 40
    assert reviewer.review_chunk_count == 2
    assert reviewer.review_failed_chunk_count == 0
    assert [call.args[1] for call in reviewer._get_prediction.await_args_list] == [
        "chunk-a", "chunk-b", "chunk-a",
    ]


@pytest.mark.asyncio
async def test_a_failed_chunk_recovery_preserves_finding_state_lifecycle(chunking_enabled):
    """Verify that a recovered chunked review participates in the finding-state lifecycle."""
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("model refused"), CHUNK_B])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b"], [])),
        pytest.raises(RuntimeError, match="model refused"),
    ):
        await reviewer._prepare_prediction("model")

    reviewer._get_prediction.side_effect = [CHUNK_A]
    await reviewer._prepare_chunked_prediction("model")

    previous_state = reconcile_review_findings(
        None,
        [{"path": "a.py", "body": "old finding", "line_start": 3, "line_end": 4}],
        allow_resolution=False,
        head_sha="head-1",
        timestamp="2026-09-09T00:00:00+00:00",
    ).state
    reviewer._review_finding_state_enabled = lambda: True
    reviewer._load_review_finding_state = lambda: ParsedReviewState(
        previous_state, present=True, valid=True
    )
    reviewer._review_head_sha = lambda: "head-2"
    reviewer._review_run_id = lambda: "run-2"

    reviewer._prepare_review_finding_state(reviewer.prediction_data)

    assert reviewer._review_state_result is not None
    assert reviewer._review_state_result.state["last_run"]["complete"] is True
    assert reviewer._review_state_result.state["findings"][0]["state"] == "RESOLVED"


@pytest.mark.asyncio
async def test_a_malformed_chunk_is_retried_without_repeating_successful_chunks(chunking_enabled):
    reviewer = _make_reviewer()
    chunk_c = CHUNK_A.replace("a.py", "c.py").replace("the index is never checked", "the value is never checked")
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, "review: {}", chunk_c])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b", "chunk-c"], [])),
        pytest.raises(ValueError, match="non-empty review"),
    ):
        await reviewer._prepare_prediction("model")

    assert reviewer._get_prediction.await_count == 3
    assert reviewer.prediction_data is None

    reviewer._get_prediction.side_effect = [CHUNK_B]
    await reviewer._prepare_chunked_prediction("model")

    assert reviewer.review_chunk_count == 3
    assert reviewer.review_failed_chunk_count == 0
    assert reviewer.prediction_data["review"]["score"] == 40
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [issue["relevant_file"].strip() for issue in issues] == ["a.py", "c.py"]
    assert [call.args[1] for call in reviewer._get_prediction.await_args_list] == [
        "chunk-a", "chunk-b", "chunk-c", "chunk-b",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_successful_chunks", [True, False])
async def test_exhausted_fallbacks_publish_partial_review_only_when_chunks_succeeded(
    chunking_enabled, has_successful_chunks,
):
    reviewer = _make_reviewer()
    reviewer.vars = {}
    reviewer.git_provider.should_publish_review_as_thread.return_value = False
    reviewer.git_provider.supports_review_comment_identity.return_value = False
    chunk_c = CHUNK_A.replace("a.py", "c.py").replace("the index is never checked", "the value is never checked")
    primary = [CHUNK_A, "review: {}", chunk_c] if has_successful_chunks else ["review: {}"] * 3
    fallback = [RuntimeError("context limit exceeded")] * (1 if has_successful_chunks else 3)
    reviewer._get_prediction = AsyncMock(side_effect=primary + fallback)
    settings_values = {
        "config.model": "primary",
        "config.fallback_models": ["fallback"],
        "config.publish_output": True,
        "config.is_auto_command": False,
        "config.propagate_tool_errors": False,
        "pr_reviewer.persistent_comment": False,
        "pr_reviewer.publish_error_details": False,
    }
    snapshot = snapshot_settings(settings_values)
    try:
        for key, value in settings_values.items():
            get_settings().set(key, value)
        with (
            patch("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", new_callable=AsyncMock),
            patch("pr_agent.tools.pr_reviewer.fit_related_tickets_to_prompt_budget",
                  return_value=({}, reviewer.token_handler)),
            patch("pr_agent.algo.pr_processing.route_primary_model", return_value=None),
            patch("pr_agent.algo.pr_processing._get_all_deployments", return_value=[None, None]),
            patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
            patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
                  return_value=(["chunk-a", "chunk-b", "chunk-c"], [])),
            patch.object(reviewer, "_prepare_pr_review", side_effect=lambda: _render_review(reviewer)) as render,
        ):
            # Exercise the real fallback chain and run()'s terminal publication path.
            await reviewer.run()
    finally:
        restore_settings(snapshot)

    calls = [call.args for call in reviewer._get_prediction.await_args_list]
    assert calls[:3] == [("primary", "chunk-a"), ("primary", "chunk-b"), ("primary", "chunk-c")]
    published = reviewer.git_provider.publish_comment.call_args.args[0]
    if not has_successful_chunks:
        render.assert_not_called()
        assert published == "Failed to review PR"
        assert reviewer.prediction_data is None
        assert calls[3:] == [("fallback", chunk) for chunk in ("chunk-a", "chunk-b", "chunk-c")]
        return

    assert calls[3:] == [("fallback", "chunk-b")]
    assert reviewer.review_chunk_count == 3
    assert reviewer.review_failed_chunk_count == 1
    assert reviewer.remaining_files_list == []
    assert "1 chunk(s) failed and are not covered by this review." in published
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [issue["relevant_file"].strip() for issue in issues] == ["a.py", "c.py"]

    previous_state = reconcile_review_findings(
        None, [{"path": "b.py", "body": "old finding", "line_start": 3, "line_end": 4}],
        allow_resolution=False,
    ).state
    reviewer._review_finding_state_enabled = lambda: True
    reviewer._load_review_finding_state = lambda: ParsedReviewState(previous_state, present=True, valid=True)
    reviewer._review_head_sha = lambda: "head-2"
    reviewer._review_run_id = lambda: "run-2"
    reviewer._prepare_review_finding_state(reviewer.prediction_data)
    assert reviewer._review_state_result.state["last_run"]["complete"] is False
    assert reviewer._review_state_result.state["findings"][0]["state"] != "RESOLVED"


@pytest.mark.asyncio
@pytest.mark.parametrize("propagate_tool_errors", [True, False])
async def test_exhausted_fallbacks_propagate_partial_review_failure_when_configured(
    chunking_enabled, propagate_tool_errors,
):
    reviewer = _make_reviewer()
    reviewer.vars = {}
    reviewer.git_provider.should_publish_review_as_thread.return_value = False
    reviewer.git_provider.supports_review_comment_identity.return_value = False
    chunk_c = CHUNK_A.replace("a.py", "c.py").replace("the index is never checked", "the value is never checked")
    reviewer._get_prediction = AsyncMock(side_effect=[
        CHUNK_A, "review: {}", chunk_c, RuntimeError("context limit exceeded"),
    ])
    settings_values = {
        "config.model": "primary",
        "config.fallback_models": ["fallback"],
        "config.publish_output": True,
        "config.is_auto_command": False,
        "config.propagate_tool_errors": propagate_tool_errors,
        "pr_reviewer.persistent_comment": False,
        "pr_reviewer.publish_error_details": False,
    }
    snapshot = snapshot_settings(settings_values)
    try:
        for key, value in settings_values.items():
            get_settings().set(key, value)
        with (
            patch("pr_agent.tools.pr_reviewer.extract_and_cache_pr_tickets", new_callable=AsyncMock),
            patch("pr_agent.tools.pr_reviewer.fit_related_tickets_to_prompt_budget",
                  return_value=({}, reviewer.token_handler)),
            patch("pr_agent.algo.pr_processing.route_primary_model", return_value=None),
            patch("pr_agent.algo.pr_processing._get_all_deployments", return_value=[None, None]),
            patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
            patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
                  return_value=(["chunk-a", "chunk-b", "chunk-c"], [])),
            patch.object(reviewer, "_prepare_pr_review", side_effect=lambda: _render_review(reviewer)) as render,
        ):
            if propagate_tool_errors:
                with pytest.raises(Exception, match="Failed to generate prediction with any model"):
                    await reviewer.run()
            else:
                await reviewer.run()
    finally:
        restore_settings(snapshot)

    published = reviewer.git_provider.publish_comment.call_args.args[0]
    assert "1 chunk(s) failed and are not covered by this review." in published
    assert reviewer.review_failed_chunk_count == 1
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [issue["relevant_file"].strip() for issue in issues] == ["a.py", "c.py"]
    render.assert_called_once()


@pytest.mark.asyncio
async def test_cached_chunks_are_used_when_fallback_diff_fits(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, RuntimeError("model refused"), CHUNK_B])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff",
              side_effect=[("diff", ["b.py"]), ("full diff", [])]),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b"], [])),
        pytest.raises(RuntimeError, match="model refused"),
    ):
        await reviewer._prepare_prediction("model")

    await reviewer._prepare_chunked_prediction("model")

    assert reviewer.prediction_data["review"]["score"] == 40
    assert [call.args[1] for call in reviewer._get_prediction.await_args_list] == [
        "chunk-a", "chunk-b", "chunk-b",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_fits", [True, False])
async def test_larger_fallback_includes_omitted_files_without_repeating_successes(chunking_enabled, fallback_fits):
    reviewer = _make_reviewer()
    reviewer.token_handler.prompt_tokens = 0
    reviewer.token_handler.count_tokens.side_effect = len
    get_settings().set("pr_reviewer.max_number_of_calls", 2)
    chunk_a = "## File: 'a.py'\n+first change\n"
    chunk_b = "## File: 'b.py'\n+second change\n"
    chunk_c = "## File: 'c.py'\n+previously omitted change\n"
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, RuntimeError("model refused"), CHUNK_B])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", side_effect=[
            (chunk_a, ["b.py", "c.py"]),
            (chunk_a + chunk_b + chunk_c, []),
        ]) as get_diff,
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=([chunk_a, chunk_b], ["c.py"])) as get_multi,
        patch("pr_agent.algo.token_budget.get_max_tokens",
              return_value=10000 if fallback_fits else 1500 + len(chunk_b)),
    ):
        with pytest.raises(RuntimeError, match="model refused"):
            await reviewer._prepare_prediction("primary")
        await reviewer._prepare_prediction("fallback")

    assert get_diff.call_count == 2
    get_multi.assert_called_once()
    calls = reviewer._get_prediction.await_args_list
    assert len(calls) == 3
    assert calls[0].args == ("primary", chunk_a)
    assert calls[1].args == ("primary", chunk_b)
    fallback_diff = calls[2].args[1]
    assert calls[2].args[0] == "fallback"
    assert chunk_a not in fallback_diff
    assert chunk_b in fallback_diff
    assert (chunk_c in fallback_diff) is fallback_fits
    assert reviewer.remaining_files_list == ([] if fallback_fits else ["c.py"])
    assert reviewer.review_chunk_count == 2
    assert reviewer.review_failed_chunk_count == 0


@pytest.mark.asyncio
async def test_a_smaller_fallback_splits_an_oversized_pending_chunk(chunking_enabled):
    reviewer = _make_reviewer()
    a_part = "## File: 'a.py'\n+change a\n"
    b_part = "## File: 'blong.py'\n+long change b\n"
    combined_chunk = a_part + b_part
    chunk_c = "## File: 'c.py'\n+change c\n"
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("model refused"), CHUNK_B])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff",
              side_effect=[(combined_chunk, ["c.py", "blong.py"]), (combined_chunk, [])]),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=([combined_chunk, chunk_c], [])),
        patch("pr_agent.algo.token_budget.get_max_tokens",
              side_effect=lambda model, **_kwargs: 10000 if model == "primary" else 1540),
    ):
        with pytest.raises(RuntimeError, match="model refused"):
            await reviewer._prepare_prediction("primary")
        reviewer._get_prediction.side_effect = [CHUNK_A, CHUNK_A]
        await reviewer._prepare_prediction("fallback")

    assert reviewer.prediction_data["review"]["score"] == 40
    assert reviewer.review_chunk_count == 3
    assert reviewer.review_failed_chunk_count == 0
    assert [call.args for call in reviewer._get_prediction.await_args_list] == [
        ("primary", combined_chunk), ("primary", chunk_c),
        ("fallback", a_part), ("fallback", b_part),
    ]


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
@pytest.mark.parametrize("chunk_predictions", [
    ["not yaml at all", "nor is this"],
    ["review: {}", "review: {}"],
])
async def test_chunks_without_nonempty_reviews_fail_the_model_attempt(chunking_enabled,
                                                                      chunk_predictions):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=chunk_predictions)

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b"], [])),
        pytest.raises(ValueError, match="non-empty review"),
    ):
        await reviewer._prepare_prediction("model")

    assert reviewer._get_prediction.await_count == 2
    assert reviewer.prediction_data is None


@pytest.mark.asyncio
async def test_invalid_chunk_emits_one_schema_warning_before_rendering(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[
        "review:\n  score: 101\n  key_issues_to_review: []",
        CHUNK_B,
    ])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs",
              return_value=(["chunk-a", "chunk-b"], [])),
        patch("pr_agent.tools.pr_reviewer.get_logger") as get_logger,
    ):
        await reviewer._prepare_prediction("model")
        reviewer._prepare_pr_review()

    warnings = get_logger.return_value.warning.call_args_list
    schema_warnings = [call for call in warnings if call.args == ("Review output failed schema validation",)]
    assert len(schema_warnings) == 1
    assert schema_warnings[0].kwargs["artifact"] == {"field": "review.score", "value": 101}
    # schema validation stays warn-only (#3372): the chunk is merged, not retried
    assert reviewer._get_prediction.await_count == 2
    assert reviewer.review_chunk_count == 2
    assert reviewer.review_failed_chunk_count == 0
    assert reviewer.prediction_data["review"]["score"] == 40


def _render_review(reviewer):
    reviewer.prediction = "review:\n  summary: test"
    reviewer.git_provider.get_diff_files.return_value = []
    reviewer.git_provider.is_supported.return_value = False
    reviewer.set_review_labels = MagicMock()

    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value={"review": {"summary": "test"}}),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch("pr_agent.tools.pr_reviewer.convert_to_markdown_v2", return_value="original review"),
    ):
        return PRReviewer._prepare_pr_review(reviewer)


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
