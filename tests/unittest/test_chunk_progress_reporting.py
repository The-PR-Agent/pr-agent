"""In-place chunk progress reporting for the `/review` and `/improve` chunked flows.

The placeholder both commands publish is frozen today: `/review` writes `"Preparing review..."`
once and `/improve` writes the animated work-in-progress body, then neither is touched until
the final output replaces it. A chunked run makes several parallel model calls plus retries
that can take minutes, so both now rewrite the placeholder as chunks settle. These tests cover
the counts, the capability and configuration gates that keep providers and auto commands on
today's behavior, and that a failing edit never affects the result.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.pr_processing import PackedPRDiffs
from pr_agent.commands.pr_description import DESCRIBE_PROGRESS_COMMENT, PRDescription
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.commands.progress_comment import (
    ChunkProgressReporter,
    chunk_progress_line,
    edit_comment_safely,
    supports_editable_progress_comment,
)
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_REVIEW_KEYS = (
    "config.publish_output",
    "config.publish_output_progress",
    "config.is_auto_command",
    "pr_reviewer.enable_large_pr_chunking",
    "pr_reviewer.max_number_of_calls",
    "pr_reviewer.persistent_comment",
)

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


def _review_progress_editor(**overrides):
    """Return a recording provider whose `edit_comment` calls can be asserted on."""
    calls = []
    provider = MagicMock()
    provider.get_files.return_value = [object()]
    provider.get_diff_files.return_value = []
    provider.should_publish_review_as_thread.return_value = False
    provider.unreviewed_files_map = None
    provider.publish_comment.return_value = SimpleNamespace(id=1, body="progress")
    provider.edit_comment.side_effect = lambda comment, body: (
        calls.append(body) or overrides.get("edit_result", True))
    provider.is_supported.side_effect = overrides.get(
        "is_supported", lambda capability: capability in {"edit_comment", "remove_comment"})
    return provider, calls


def _stub_ticket_budget_fit(reviewer):
    """Bypass the ticket-budget fit, which would render the real review prompt.

    The fit is not what this module exercises, and rendering the prompt needs every template
    variable present. Returning the vars unchanged keeps the reviewer on the same code path
    from `_prepare_prediction` onward.
    """
    return patch(
        "pr_agent.commands.pr_reviewer.fit_related_tickets_to_prompt_budget",
        side_effect=lambda _pr, variables, _system, _user, _model, **_kwargs:
        (variables, reviewer.token_handler),
    )


def _make_reviewer(provider, chunk_predictions):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.ai_handler = MagicMock()
    # Keep the chunks on plain gather: these tests target progress reporting, and a
    # truthy mock would turn on the staggered first chunk (#4876).
    reviewer.ai_handler.prompt_cache_active = MagicMock(return_value=False)
    reviewer.token_handler = MagicMock()
    reviewer.token_handler.prompt_tokens = 0
    reviewer.token_handler.count_tokens.side_effect = len
    reviewer.pr_url = "https://example/invalid/pull/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.prediction = None
    reviewer.pr = SimpleNamespace(title="t", number=1)
    reviewer.vars = {"title": "t", "diff": "", "related_tickets": []}
    reviewer._prepare_pr_review = lambda: "### PR Reviewer Guide\n\ntext"

    remaining = list(chunk_predictions)

    async def fake_get_prediction(model, patches_diff=None, **_kwargs):
        # A later attempt with no scripted chunk left reuses the last value, so a chunk
        # scripted to fail keeps failing across retries instead of silently succeeding.
        value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(value, BaseException):
            raise value
        return value

    reviewer._get_prediction = fake_get_prediction
    return reviewer


async def _run_chunked_review(reviewer, chunk_count=2):
    with (
        patch("pr_agent.algo.token_budget.get_max_tokens", return_value=10000),
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch(
            "pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
            return_value=PackedPRDiffs([f"chunk-{index}" for index in range(chunk_count)], [], []),
        ),
        patch("pr_agent.commands.pr_reviewer.extract_and_cache_pr_tickets", AsyncMock()),
        _stub_ticket_budget_fit(reviewer),
    ):
        await reviewer.run()


@pytest.fixture
def published_review():
    snapshot = snapshot_settings(_REVIEW_KEYS)
    settings = get_settings()
    settings.set("config.publish_output", True)
    settings.set("config.publish_output_progress", True)
    settings.set("config.is_auto_command", False)
    settings.set("pr_reviewer.enable_large_pr_chunking", True)
    settings.set("pr_reviewer.max_number_of_calls", 3)
    settings.set("pr_reviewer.persistent_comment", False)
    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=10000):
        yield
    restore_settings(snapshot)


def test_chunk_progress_line_counts_completed_and_failed_chunks():
    assert chunk_progress_line(2, 3) == "analyzed 2 of 3 chunks"
    assert chunk_progress_line(2, 3, 1) == "analyzed 2 of 3 chunks, 1 chunk failed"
    assert chunk_progress_line(2, 3, 2) == "analyzed 2 of 3 chunks, 2 chunks failed"


def test_chunk_progress_line_needs_a_total_and_never_exceeds_it():
    assert chunk_progress_line(0, 0) == ""
    assert chunk_progress_line(4, 3) == "analyzed 3 of 3 chunks"


def test_edit_comment_safely_swallows_provider_failures():
    provider = MagicMock()
    provider.edit_comment.side_effect = RuntimeError("comment is gone")

    assert edit_comment_safely(provider, object(), "body", label="review") is False

    provider.edit_comment.side_effect = None
    provider.edit_comment.return_value = False
    assert edit_comment_safely(provider, object(), "body", label="review") is False

    provider.edit_comment.return_value = True
    assert edit_comment_safely(provider, object(), "body", label="review") is True


def test_capability_gate_requires_both_edit_and_remove():
    provider = MagicMock()
    provider.is_supported.side_effect = lambda capability: capability == "edit_comment"

    assert supports_editable_progress_comment(provider) is False

    provider.is_supported.side_effect = None
    provider.is_supported.return_value = True
    assert supports_editable_progress_comment(provider) is True


def test_no_reporter_without_an_editable_comment():
    provider = MagicMock()
    provider.is_supported.return_value = True
    # MagicMock auto-creates any attribute, so drop the check-run sink explicitly:
    # this provider has no progress channel at all.
    del provider.update_check_run_progress

    assert ChunkProgressReporter.create(provider, None, "body", total=2, body_builder=str) is None
    assert ChunkProgressReporter.create(provider, object(), "", total=2, body_builder=str) is None
    assert ChunkProgressReporter.create(provider, object(), "body", total=0, body_builder=str) is None


@pytest.mark.asyncio
async def test_reporter_skips_a_write_that_would_not_change_the_body():
    provider = MagicMock()
    provider.is_supported.return_value = True
    reporter = ChunkProgressReporter.create(
        provider, object(), "base", total=1, body_builder=lambda line: f"base {line}")
    await reporter.record_settled()
    calls_after_settle = provider.edit_comment.call_count

    await reporter.set_failed(0)

    assert provider.edit_comment.call_count == calls_after_settle


@pytest.mark.asyncio
async def test_reporter_serializes_concurrent_updates():
    """Parallel chunks settle under a lock, so the comment never shows a count that moved back."""
    recorded = []

    def record_body(_comment, body):
        recorded.append(body)
        return True

    provider = MagicMock()
    provider.is_supported.return_value = True
    provider.edit_comment.side_effect = record_body
    reporter = ChunkProgressReporter.create(
        provider, object(), "base", total=2, body_builder=lambda line: f"base {line}")

    await asyncio.gather(reporter.record_settled(), reporter.record_settled())

    assert recorded == ["base analyzed 1 of 2 chunks", "base analyzed 2 of 2 chunks"]


@pytest.mark.asyncio
async def test_review_progress_comment_reports_each_settled_chunk(published_review):
    provider, edits = _review_progress_editor()
    reviewer = _make_reviewer(provider, [CHUNK_A, CHUNK_B])

    await _run_chunked_review(reviewer)

    assert edits == [
        "Preparing review... analyzed 1 of 2 chunks",
        "Preparing review... analyzed 2 of 2 chunks",
    ]
    # The placeholder stays temporary and is still removed before the review is published.
    provider.publish_comment.assert_any_call("Preparing review...", is_temporary=True)
    provider.remove_comment.assert_called_once()


@pytest.mark.asyncio
async def test_review_progress_comment_reports_a_failed_chunk(published_review):
    provider, edits = _review_progress_editor()
    reviewer = _make_reviewer(provider, [RuntimeError("model refused"), CHUNK_B])

    await _run_chunked_review(reviewer)

    assert reviewer.review_failed_chunk_count == 1
    assert edits[-1] == "Preparing review... analyzed 2 of 2 chunks, 1 chunk failed"
    assert reviewer.review_chunk_count == 2


@pytest.mark.asyncio
async def test_review_progress_counts_include_chunks_an_earlier_attempt_finished(published_review):
    """A retry attempt reviews only the pending chunks, so the reported X of N must not restart."""
    provider, edits = _review_progress_editor()
    reviewer = _make_reviewer(provider, [CHUNK_A, CHUNK_B])
    reviewer._progress_response = SimpleNamespace(id=1, body="progress")
    reviewer._chunked_results = {0: (CHUNK_A, {"review": {}}, "model")}

    with (
        patch("pr_agent.algo.token_budget.get_max_tokens", return_value=10000),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-0", "chunk-1"], [], [])),
    ):
        await reviewer._prepare_chunked_prediction("model")

    # One pending chunk remained, so the single new edit already reads as the full count
    # rather than restarting at 1.
    assert edits == ["Preparing review... analyzed 2 of 2 chunks"]


@pytest.mark.asyncio
async def test_review_progress_never_moves_backward_across_a_fallback(published_review):
    """A failed attempt is cleared before the retry, so the visible count cannot rewind."""
    provider, edits = _review_progress_editor()
    reviewer = _make_reviewer(provider, [CHUNK_A, RuntimeError("model refused"), CHUNK_B])
    reviewer._progress_response = SimpleNamespace(id=1, body="progress")

    with (
        patch("pr_agent.algo.token_budget.get_max_tokens", return_value=10000),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-0", "chunk-1"], [], [])),
    ):
        with pytest.raises(RuntimeError):
            await reviewer._prepare_chunked_prediction("model-a")
        await reviewer._prepare_chunked_prediction("model-b")

    reported = [body.removeprefix("Preparing review... ").strip() for body in edits]
    counts = [
        (int(parts[1]), int(parts[3]))
        for parts in (line.split() for line in reported)
        if parts and parts[0] == "analyzed"
    ]
    assert counts == sorted(counts)
    # The retry starts clean instead of showing the dead attempt's "2 of 2, 1 failed".
    assert "Preparing review..." in edits
    assert reviewer.review_failed_chunk_count == 0


@pytest.mark.asyncio
async def test_review_progress_is_skipped_when_the_provider_cannot_edit(published_review):
    provider, edits = _review_progress_editor(
        is_supported=lambda capability: capability == "gfm_markdown")
    reviewer = _make_reviewer(provider, [CHUNK_A, CHUNK_B])

    await _run_chunked_review(reviewer)

    assert edits == []
    assert reviewer.review_chunk_count == 2


@pytest.mark.asyncio
async def test_review_progress_is_skipped_for_an_auto_command(published_review):
    get_settings().set("config.is_auto_command", True)
    provider, edits = _review_progress_editor()
    reviewer = _make_reviewer(provider, [CHUNK_A, CHUNK_B])

    await _run_chunked_review(reviewer)

    assert edits == []
    assert all(
        call.args[:1] != ("Preparing review...",)
        for call in provider.publish_comment.call_args_list
    )


@pytest.mark.asyncio
async def test_review_progress_is_skipped_when_progress_output_is_off(published_review):
    get_settings().set("config.publish_output_progress", False)
    provider, edits = _review_progress_editor()
    reviewer = _make_reviewer(provider, [CHUNK_A, CHUNK_B])

    await _run_chunked_review(reviewer)

    assert edits == []
    # The placeholder itself is unchanged; only its in-place updates are gated.
    provider.publish_comment.assert_any_call("Preparing review...", is_temporary=True)


@pytest.mark.asyncio
async def test_a_failing_progress_edit_does_not_affect_the_review_result(published_review):
    provider = MagicMock()
    provider.get_files.return_value = [object()]
    provider.get_diff_files.return_value = []
    provider.should_publish_review_as_thread.return_value = False
    provider.unreviewed_files_map = None
    provider.publish_comment.return_value = SimpleNamespace(id=1, body="progress")
    provider.edit_comment.side_effect = RuntimeError("rate limited")
    provider.is_supported.side_effect = lambda capability: capability in {
        "edit_comment", "remove_comment"}
    reviewer = _make_reviewer(provider, [CHUNK_A, CHUNK_B])

    await _run_chunked_review(reviewer)

    assert reviewer.prediction_data["review"]["score"] == 40
    assert reviewer.review_chunk_count == 2
    provider.publish_comment.assert_any_call("### PR Reviewer Guide\n\ntext")


_DESCRIBE_KEYS = (
    "config.publish_output",
    "config.publish_output_progress",
    "config.is_auto_command",
    "pr_description.use_description_markers",
    "pr_description.enable_large_pr_handling",
    "pr_description.async_ai_calls",
)

_DESCRIBE_CHUNK = (
    "pr_files:\n"
    "  - filename: |\n"
    "        a.py\n"
    "    changes_title: |\n"
    "        Adds the handler\n"
    "    changes_summary: |\n"
    "        Adds a request handler\n"
    "    label: |\n"
    "        Bug fix\n"
)


def _make_describe_command(provider, chunk_predictions):
    command = PRDescription.__new__(PRDescription)
    command.git_provider = provider
    command.pr_url = "https://example.invalid/pull/1"
    command.pr_id = 1
    command.ai_handler = MagicMock()
    # Keep the chunks on plain gather; see the reviewer factory above (#4876).
    command.ai_handler.prompt_cache_active = MagicMock(return_value=False)
    command.vars = {"title": "t", "diff": "", "include_file_summary_changes": True}
    command.user_description = ""
    command.keys_fix = []
    command.progress_response = SimpleNamespace(id=1, body=DESCRIBE_PROGRESS_COMMENT)
    command._chunk_progress = None

    async def extend_uncovered(walkthrough):
        return walkthrough

    command.extend_uncovered_files = extend_uncovered

    remaining = list(chunk_predictions)

    async def fake_get_prediction(model, patches_diff, prompt=""):
        if prompt != "pr_description_only_files_prompts":
            return "title: PR Walker\ndescription: walkthrough"
        value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(value, BaseException):
            raise value
        return value

    command._get_prediction = fake_get_prediction
    return command


async def _run_chunked_describe(command, chunk_count=2, **overrides):
    settings = get_settings()
    settings.set("config.publish_output", True)
    settings.set("config.publish_output_progress", overrides.get("publish_output_progress", True))
    settings.set("config.is_auto_command", overrides.get("is_auto_command", False))
    settings.set("pr_description.use_description_markers", False)
    settings.set("pr_description.enable_large_pr_handling", True)
    settings.set("pr_description.async_ai_calls", overrides.get("async_ai_calls", True))
    with (
        patch("pr_agent.algo.token_budget.get_max_tokens", return_value=10000),
        patch("pr_agent.commands.pr_description.get_pr_diff", return_value=("", [])),
        patch(
            "pr_agent.commands.pr_description.get_pr_diff_multiple_patchs",
            return_value=(
                [f"chunk-{index}" for index in range(chunk_count)],
                [1] * chunk_count,
                [],
                [],
                {},
                [[f"file-{index}.py"] for index in range(chunk_count)],
            ),
        ),
        patch(
            "pr_agent.commands.pr_description.fit_related_tickets_to_prompt_budget",
            side_effect=lambda _pr, variables, _system, _user, _model, **_kwargs:
            (variables, SimpleNamespace(prompt_tokens=0, count_tokens=len)),
        ),
    ):
        await command._prepare_prediction("gpt-4o")


@pytest.fixture
def published_describe():
    snapshot = snapshot_settings(_DESCRIBE_KEYS)
    yield
    restore_settings(snapshot)


@pytest.mark.asyncio
async def test_describe_progress_comment_reports_each_settled_chunk(published_describe):
    provider, edits = _review_progress_editor()
    provider.get_filtered_diff_file_names.return_value = []
    command = _make_describe_command(provider, [_DESCRIBE_CHUNK, _DESCRIBE_CHUNK])

    await _run_chunked_describe(command)

    assert edits == [
        "Preparing PR description... analyzed 1 of 2 chunks",
        "Preparing PR description... analyzed 2 of 2 chunks",
    ]


@pytest.mark.asyncio
async def test_describe_progress_comment_reports_a_failed_chunk(published_describe):
    provider, edits = _review_progress_editor()
    provider.get_filtered_diff_file_names.return_value = []
    command = _make_describe_command(provider, [RuntimeError("model refused"), _DESCRIBE_CHUNK])

    # One chunk fails and one succeeds; /describe retains the successful chunks,
    # so the run completes and the progress line reports the failed count.
    await _run_chunked_describe(command)

    assert edits
    assert any("1 chunk failed" in body for body in edits)
    assert command.description_failed_chunk_count == 1


@pytest.mark.asyncio
async def test_describe_progress_is_skipped_when_the_provider_cannot_edit(published_describe):
    provider, edits = _review_progress_editor(is_supported=lambda _capability: False)
    del provider.update_check_run_progress
    provider.get_filtered_diff_file_names.return_value = []
    command = _make_describe_command(provider, [_DESCRIBE_CHUNK, _DESCRIBE_CHUNK])

    await _run_chunked_describe(command)

    assert edits == []
    assert command._chunk_progress is None


@pytest.mark.asyncio
async def test_describe_progress_falls_back_to_the_check_run_sink(published_describe):
    """Without an editable comment, an in-progress check run serves the progress alone."""
    provider, edits = _review_progress_editor(is_supported=lambda _capability: False)
    check_run_lines = []
    provider.update_check_run_progress.side_effect = (
        lambda line: check_run_lines.append(line) or True)
    provider.get_filtered_diff_file_names.return_value = []
    command = _make_describe_command(provider, [_DESCRIBE_CHUNK, _DESCRIBE_CHUNK])

    await _run_chunked_describe(command)

    assert edits == []
    assert command._chunk_progress is not None
    assert check_run_lines == ["analyzed 1 of 2 chunks", "analyzed 2 of 2 chunks"]


@pytest.mark.asyncio
async def test_review_progress_without_a_comment_serves_the_check_run(published_review):
    """Automatic commands publish no comment; their check run is the only progress channel."""
    provider, edits = _review_progress_editor()
    provider.publish_comment.return_value = None  # no progress comment object exists
    check_run_lines = []
    provider.update_check_run_progress.side_effect = (
        lambda line: check_run_lines.append(line) or True)
    reviewer = _make_reviewer(provider, [CHUNK_A, CHUNK_B])

    await _run_chunked_review(reviewer)

    assert edits == []
    assert check_run_lines == ["analyzed 1 of 2 chunks", "analyzed 2 of 2 chunks"]


@pytest.mark.asyncio
async def test_review_progress_writes_the_comment_and_the_check_run_together(published_review):
    """With both channels available, each settled chunk updates both sinks."""
    provider, edits = _review_progress_editor()
    check_run_lines = []
    provider.update_check_run_progress.side_effect = (
        lambda line: check_run_lines.append(line) or True)
    reviewer = _make_reviewer(provider, [CHUNK_A, CHUNK_B])

    await _run_chunked_review(reviewer)

    assert edits == [
        "Preparing review... analyzed 1 of 2 chunks",
        "Preparing review... analyzed 2 of 2 chunks",
    ]
    assert check_run_lines == ["analyzed 1 of 2 chunks", "analyzed 2 of 2 chunks"]


@pytest.mark.asyncio
async def test_a_failing_check_run_update_never_breaks_the_run(published_review):
    provider, _edits = _review_progress_editor()
    provider.update_check_run_progress.side_effect = RuntimeError("api down")
    reviewer = _make_reviewer(provider, [CHUNK_A, CHUNK_B])

    await _run_chunked_review(reviewer)

    assert reviewer.prediction_data["review"]["score"] == 40


@pytest.mark.asyncio
async def test_describe_progress_is_skipped_when_progress_output_is_off(published_describe):
    provider, edits = _review_progress_editor()
    provider.get_filtered_diff_file_names.return_value = []
    command = _make_describe_command(provider, [_DESCRIBE_CHUNK, _DESCRIBE_CHUNK])

    await _run_chunked_describe(command, publish_output_progress=False)

    assert edits == []
    assert command._chunk_progress is None


@pytest.mark.asyncio
async def test_describe_progress_reports_sync_chunk_settlement(published_describe):
    provider, edits = _review_progress_editor()
    provider.get_filtered_diff_file_names.return_value = []
    command = _make_describe_command(provider, [_DESCRIBE_CHUNK, _DESCRIBE_CHUNK])

    await _run_chunked_describe(command, async_ai_calls=False)

    assert edits == [
        "Preparing PR description... analyzed 1 of 2 chunks",
        "Preparing PR description... analyzed 2 of 2 chunks",
    ]


@pytest.mark.asyncio
async def test_describe_fallback_attempt_restores_the_placeholder(published_describe):
    provider, edits = _review_progress_editor()
    provider.get_filtered_diff_file_names.return_value = []
    error = RuntimeError("model refused")
    command = _make_describe_command(provider, [error, error, _DESCRIBE_CHUNK, _DESCRIBE_CHUNK])

    with pytest.raises(RuntimeError):
        await _run_chunked_describe(command)
    await _run_chunked_describe(command)

    assert edits[3:] == [
        DESCRIBE_PROGRESS_COMMENT,
        "Preparing PR description... analyzed 1 of 2 chunks",
        "Preparing PR description... analyzed 2 of 2 chunks",
    ]


@pytest.mark.asyncio
async def test_describe_progress_is_skipped_for_a_single_chunk(published_describe):
    provider, edits = _review_progress_editor()
    provider.get_filtered_diff_file_names.return_value = []
    command = _make_describe_command(provider, [_DESCRIBE_CHUNK])

    await _run_chunked_describe(command, chunk_count=1)

    assert edits == []
