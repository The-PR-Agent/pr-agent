from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.inline_comment_dedup import (
    body_with_markers,
    get_inline_comment_store,
    key_issue_fingerprint,
)
from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.tools.pr_reviewer import PRReviewer


def _make_reviewer(git_provider=None):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = git_provider or MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    return reviewer


def _make_prediction_reviewer(git_provider=None):
    reviewer = _make_reviewer(git_provider)
    reviewer.token_handler = MagicMock()
    reviewer.remaining_files_list = []
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.prediction = None
    return reviewer


@pytest.mark.asyncio
async def test_prepare_prediction_requests_remaining_files_and_preserves_tuple_result():
    reviewer = _make_prediction_reviewer()
    reviewer._get_prediction = AsyncMock(return_value="prediction")

    with patch(
        "pr_agent.tools.pr_reviewer.get_pr_diff",
        return_value=("diff", ["src/one.py", "docs/two.md"]),
    ) as get_pr_diff:
        await reviewer._prepare_prediction("model")

    get_pr_diff.assert_called_once_with(
        reviewer.git_provider,
        reviewer.token_handler,
        "model",
        add_line_numbers_to_hunks=True,
        disable_extra_lines=False,
        return_remaining_files=True,
    )
    assert reviewer.patches_diff == "diff"
    assert reviewer.remaining_files_list == ["src/one.py", "docs/two.md"]
    assert reviewer.prediction == "prediction"


@pytest.mark.asyncio
async def test_prepare_prediction_accepts_full_diff_string_when_token_budget_is_sufficient():
    reviewer = _make_prediction_reviewer()
    reviewer._get_prediction = AsyncMock(return_value="prediction")

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value="diff"):
        await reviewer._prepare_prediction("model")

    assert reviewer.patches_diff == "diff"
    assert reviewer.remaining_files_list == []
    assert reviewer.prediction == "prediction"


@pytest.mark.asyncio
async def test_prepare_prediction_keeps_incremental_review_compatible_with_tuple_result():
    reviewer = _make_prediction_reviewer()
    reviewer.incremental = SimpleNamespace(is_incremental=True)
    reviewer._get_prediction = AsyncMock(return_value="prediction")

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["skipped.py"])):
        await reviewer._prepare_prediction("model")

    assert reviewer.patches_diff == "diff"
    assert reviewer.remaining_files_list == ["skipped.py"]
    assert reviewer.prediction == "prediction"


def _render_review(reviewer, remaining_files, supports_gfm_markdown=False):
    reviewer.prediction = "review: {}"
    reviewer.remaining_files_list = remaining_files
    reviewer.git_provider.get_diff_files.return_value = []
    reviewer.git_provider.is_supported.return_value = supports_gfm_markdown
    reviewer.set_review_labels = MagicMock()

    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value={"review": {}}),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch("pr_agent.tools.pr_reviewer.convert_to_markdown_v2", return_value="original review"),
    ):
        return reviewer._prepare_pr_review()


def test_prepare_pr_review_appends_complete_coverage_footer():
    reviewer = _make_prediction_reviewer()
    settings = get_settings()
    original_enable_review_coverage_footer = settings.pr_reviewer.enable_review_coverage_footer

    try:
        settings.pr_reviewer.enable_review_coverage_footer = True
        review = _render_review(reviewer, ["src/one.py", "nested/two.md"])
    finally:
        settings.pr_reviewer.enable_review_coverage_footer = original_enable_review_coverage_footer

    assert review.startswith("original review")
    assert "⚠️ **Review coverage:**" in review
    assert "- `src/one.py`" in review
    assert "- `nested/two.md`" in review
    assert "\n\n<hr>\n\n" in review
    assert "\n\n---\n\n" not in review


def test_prepare_pr_review_hides_coverage_footer_when_disabled():
    reviewer = _make_prediction_reviewer()
    settings = get_settings()
    original_enable_review_coverage_footer = settings.pr_reviewer.enable_review_coverage_footer

    try:
        settings.pr_reviewer.enable_review_coverage_footer = False
        review = _render_review(reviewer, ["skipped.py"])
    finally:
        settings.pr_reviewer.enable_review_coverage_footer = original_enable_review_coverage_footer

    assert review == "original review"
    assert "Review coverage" not in review


def test_prepare_pr_review_places_coverage_footer_before_help_text():
    reviewer = _make_prediction_reviewer()
    settings = get_settings()
    original_enable_review_coverage_footer = settings.pr_reviewer.enable_review_coverage_footer
    original_enable_help_text = settings.pr_reviewer.enable_help_text

    try:
        settings.pr_reviewer.enable_review_coverage_footer = True
        settings.pr_reviewer.enable_help_text = True
        with patch("pr_agent.tools.pr_reviewer.HelpMessage.get_review_usage_guide", return_value="help text"):
            review = _render_review(reviewer, ["skipped.py"], supports_gfm_markdown=True)
    finally:
        settings.pr_reviewer.enable_review_coverage_footer = original_enable_review_coverage_footer
        settings.pr_reviewer.enable_help_text = original_enable_help_text

    assert review.index("⚠️ **Review coverage:**") < review.index("help text")


def test_prepare_pr_review_leaves_original_content_unchanged_without_remaining_files():
    reviewer = _make_prediction_reviewer()

    review = _render_review(reviewer, [])

    assert review == "original review"
    assert "Review coverage" not in review


def test_prepare_pr_review_limits_coverage_footer_to_50_files():
    reviewer = _make_prediction_reviewer()
    remaining_files = [f"file_{index}.py" for index in range(51)]

    review = _render_review(reviewer, remaining_files)

    assert review.count("- `file_") == 50
    assert "- `file_0.py`" in review
    assert "- `file_49.py`" in review
    assert "- `file_50.py`" not in review


def test_prepare_pr_review_reports_number_of_files_beyond_coverage_limit():
    reviewer = _make_prediction_reviewer()
    remaining_files = [f"file_{index}.py" for index in range(53)]

    review = _render_review(reviewer, remaining_files)

    assert "... and 3 more" in review
    assert "- `file_50.py`" not in review


def _key_issue(**overrides):
    issue = {
        "relevant_file": "app.py",
        "issue_header": "Possible Issue",
        "issue_content": "The new branch never releases the lock.",
        "start_line": 2,
        "end_line": 3,
    }
    issue.update(overrides)
    return issue


def _reviewer_with_findings(*issues, head_file="one\ntwo\nthree\nfour\n"):
    git_provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    git_provider.azure_devops_client = MagicMock()
    git_provider.azure_devops_client.get_threads.return_value = []
    git_provider.repo_slug = "repo"
    git_provider.workspace_slug = "project"
    git_provider.pr_num = 1
    git_provider.get_diff_files = MagicMock()
    git_provider.get_diff_files.return_value = [
        FilePatchInfo(base_file="", head_file=head_file, patch="", filename="app.py")
    ]
    git_provider.publish_code_suggestions = MagicMock(return_value=True)
    git_provider.max_comment_chars = None
    git_provider._inline_comment_store = None
    reviewer = _make_reviewer(git_provider)
    reviewer._published_inline_key_issue_fingerprints = MagicMock(
        side_effect=lambda _store, fingerprints: fingerprints
    )
    return reviewer, {"review": {"key_issues_to_review": list(issues)}}


def _published_comment(git_provider):
    published = git_provider.publish_code_suggestions.call_args_list[0].args[0]
    assert len(published) == 1
    return published[0]


def test_key_issues_are_published_on_their_lines_and_leave_the_summary():
    reviewer, data = _reviewer_with_findings(_key_issue())

    result = reviewer._publish_key_issues_as_inline_comments(data)

    comment = _published_comment(reviewer.git_provider)
    assert comment["relevant_file"] == "app.py"
    assert comment["relevant_lines_start"] == 2
    assert comment["relevant_lines_end"] == 3
    assert "The new branch never releases the lock." in comment["body"]
    assert "```suggestion" not in comment["body"]
    assert "key_issues_to_review" not in result["review"]
    assert len(data["review"]["key_issues_to_review"]) == 1


def test_prepare_pr_review_does_not_publish_key_issues_inline_by_default():
    reviewer = _make_prediction_reviewer()

    review = _render_review(reviewer, [])

    assert review == "original review"
    reviewer.git_provider.publish_code_suggestions.assert_not_called()


def test_prepare_pr_review_publishes_key_issues_inline_when_enabled():
    reviewer = _make_prediction_reviewer()
    settings = get_settings()
    original_inline_key_issues = settings.pr_reviewer.get("inline_key_issues", False)
    reviewer._publish_key_issues_as_inline_comments = MagicMock(return_value={"review": {}})

    try:
        settings.pr_reviewer.inline_key_issues = True
        _render_review(reviewer, [])
    finally:
        settings.pr_reviewer.inline_key_issues = original_inline_key_issues

    reviewer._publish_key_issues_as_inline_comments.assert_called_once()


@pytest.mark.parametrize("issue", [
    _key_issue(relevant_file="not_in_the_diff.py"),
    _key_issue(start_line=0, end_line=0),
    _key_issue(start_line=3, end_line=2),
    _key_issue(start_line=40, end_line=41),
    _key_issue(issue_content=""),
])
def test_unanchorable_key_issue_stays_in_the_summary(issue):
    reviewer, data = _reviewer_with_findings(issue)

    result = reviewer._publish_key_issues_as_inline_comments(data)

    reviewer.git_provider.publish_code_suggestions.assert_not_called()
    assert result["review"]["key_issues_to_review"] == [issue]


def test_key_issue_that_fails_to_publish_stays_in_the_summary():
    issue = _key_issue()
    reviewer, data = _reviewer_with_findings(issue)
    reviewer.git_provider.publish_code_suggestions.return_value = False
    reviewer._published_inline_key_issue_fingerprints.side_effect = None
    reviewer._published_inline_key_issue_fingerprints.return_value = set()

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert result["review"]["key_issues_to_review"] == [issue]


def test_key_issue_that_cannot_be_verified_stays_in_the_summary():
    issue = _key_issue()
    reviewer, data = _reviewer_with_findings(issue)
    reviewer._published_inline_key_issue_fingerprints.side_effect = None
    reviewer._published_inline_key_issue_fingerprints.return_value = set()

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert result["review"]["key_issues_to_review"] == [issue]


def test_key_issue_without_file_content_stays_in_the_summary():
    issue = _key_issue()
    reviewer, data = _reviewer_with_findings(issue, head_file="")

    result = reviewer._publish_key_issues_as_inline_comments(data)

    reviewer.git_provider.publish_code_suggestions.assert_not_called()
    assert result["review"]["key_issues_to_review"] == [issue]


def test_key_issue_is_not_published_when_the_provider_cannot_verify_it():
    issue = _key_issue()
    reviewer, data = _reviewer_with_findings(issue)
    reviewer._can_verify_inline_key_issue_publication = MagicMock(return_value=False)

    result = reviewer._publish_key_issues_as_inline_comments(data)

    reviewer.git_provider.publish_code_suggestions.assert_not_called()
    assert result is data


def test_same_key_issue_on_different_lines_is_published_at_each_location():
    first = _key_issue(start_line=1, end_line=1)
    second = _key_issue(start_line=3, end_line=3)
    reviewer, data = _reviewer_with_findings(first, second)

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert reviewer.git_provider.publish_code_suggestions.call_count == 1
    assert len(reviewer.git_provider.publish_code_suggestions.call_args.args[0]) == 2
    assert "key_issues_to_review" not in result["review"]


def test_duplicate_key_issue_is_published_once():
    issue = _key_issue()
    reviewer, data = _reviewer_with_findings(issue, issue.copy())

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert len(reviewer.git_provider.publish_code_suggestions.call_args.args[0]) == 1
    assert "key_issues_to_review" not in result["review"]


def test_key_issues_that_diverge_after_eighty_characters_are_both_published():
    prefix = "x" * 100
    first = _key_issue(issue_content=f"{prefix}a")
    second = _key_issue(issue_content=f"{prefix}b")
    reviewer, data = _reviewer_with_findings(first, second)

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert len(reviewer.git_provider.publish_code_suggestions.call_args.args[0]) == 2
    assert "key_issues_to_review" not in result["review"]


def test_unverified_duplicate_key_issue_stays_in_the_summary():
    issue = _key_issue()
    duplicate = issue.copy()
    reviewer, data = _reviewer_with_findings(issue, duplicate)
    reviewer._published_inline_key_issue_fingerprints.side_effect = None
    reviewer._published_inline_key_issue_fingerprints.return_value = set()

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert result is data
    assert result["review"]["key_issues_to_review"] == [issue, duplicate]


def test_batch_publish_failure_keeps_unverified_findings_in_the_summary():
    failing = _key_issue(issue_content="This one raises.")
    working = _key_issue(issue_content="This one publishes.", start_line=1, end_line=1)
    reviewer, data = _reviewer_with_findings(failing, working)
    reviewer.git_provider.publish_code_suggestions.side_effect = RuntimeError("API rejected the comments")
    reviewer._published_inline_key_issue_fingerprints.side_effect = None
    reviewer._published_inline_key_issue_fingerprints.return_value = set()

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert reviewer.git_provider.publish_code_suggestions.call_count == 1
    assert result["review"]["key_issues_to_review"] == [failing, working]


def test_existing_comment_load_failure_skips_inline_publishing():
    issue = _key_issue()
    reviewer, data = _reviewer_with_findings(issue)
    reviewer.git_provider.azure_devops_client.get_threads.side_effect = RuntimeError("request failed")

    result = reviewer._publish_key_issues_as_inline_comments(data)

    reviewer.git_provider.publish_code_suggestions.assert_not_called()
    assert result is data


def test_publish_without_a_success_record_keeps_findings_in_the_summary():
    issue = _key_issue()
    reviewer, data = _reviewer_with_findings(issue)
    reviewer.git_provider.azure_devops_client.get_threads.return_value = []
    reviewer._published_inline_key_issue_fingerprints = (
        PRReviewer._published_inline_key_issue_fingerprints.__get__(reviewer)
    )

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert result["review"]["key_issues_to_review"] == [issue]


def test_existing_azure_thread_removes_finding_from_summary():
    reviewer, data = _reviewer_with_findings(_key_issue())
    body = "**Possible Issue**\n\nThe new branch never releases the lock."
    fingerprint = key_issue_fingerprint("app.py", body)
    reviewer.git_provider.azure_devops_client.get_threads.return_value = [
        SimpleNamespace(
            thread_context=SimpleNamespace(
                file_path="app.py",
                right_file_start=SimpleNamespace(line=2),
            ),
            comments=[SimpleNamespace(content=body_with_markers(body, fingerprint, None))],
        )
    ]

    result = reviewer._publish_key_issues_as_inline_comments(data)

    reviewer.git_provider.publish_code_suggestions.assert_not_called()
    assert "key_issues_to_review" not in result["review"]


def test_recent_azure_post_is_verified_before_thread_listing_catches_up():
    reviewer, data = _reviewer_with_findings(_key_issue())
    reviewer.git_provider.publish_code_suggestions = (
        AzureDevopsProvider.publish_code_suggestions.__get__(reviewer.git_provider)
    )
    reviewer._published_inline_key_issue_fingerprints = (
        PRReviewer._published_inline_key_issue_fingerprints.__get__(reviewer)
    )

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert reviewer.git_provider.azure_devops_client.create_thread.call_count == 1
    assert "key_issues_to_review" not in result["review"]


def test_recent_azure_post_does_not_reload_threads_for_verification():
    reviewer, data = _reviewer_with_findings(_key_issue())
    reviewer.git_provider.publish_code_suggestions = (
        AzureDevopsProvider.publish_code_suggestions.__get__(reviewer.git_provider)
    )
    reviewer._published_inline_key_issue_fingerprints = (
        PRReviewer._published_inline_key_issue_fingerprints.__get__(reviewer)
    )

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert reviewer.git_provider.azure_devops_client.get_threads.call_count == 1
    assert "key_issues_to_review" not in result["review"]


def test_same_finding_at_failed_location_stays_in_the_summary():
    published = _key_issue(start_line=1, end_line=1)
    failed = _key_issue(start_line=3, end_line=3)
    reviewer, data = _reviewer_with_findings(published, failed)
    reviewer.git_provider.publish_code_suggestions = (
        AzureDevopsProvider.publish_code_suggestions.__get__(reviewer.git_provider)
    )
    reviewer.git_provider.azure_devops_client.create_thread.side_effect = [MagicMock(), RuntimeError("failed")]
    reviewer._published_inline_key_issue_fingerprints = (
        PRReviewer._published_inline_key_issue_fingerprints.__get__(reviewer)
    )

    result = reviewer._publish_key_issues_as_inline_comments(data)

    assert reviewer.git_provider.azure_devops_client.create_thread.call_count == 2
    assert result["review"]["key_issues_to_review"] == [failed]


def test_key_issue_already_anchored_on_the_pr_is_not_published_again():
    issue = _key_issue()
    reviewer, data = _reviewer_with_findings(issue)
    store = get_inline_comment_store(reviewer.git_provider)
    store.add(key_issue_fingerprint(
        "app.py", "**Possible Issue**\n\nThe new branch never releases the lock."
    ))

    result = reviewer._publish_key_issues_as_inline_comments(data)

    reviewer.git_provider.publish_code_suggestions.assert_not_called()
    assert "key_issues_to_review" not in result["review"]


def test_key_issue_path_without_leading_slash_uses_azure_diff_path():
    reviewer, data = _reviewer_with_findings(_key_issue())
    reviewer.git_provider.get_diff_files.return_value[0].filename = "/app.py"

    reviewer._publish_key_issues_as_inline_comments(data)

    assert _published_comment(reviewer.git_provider)["relevant_file"] == "/app.py"


def test_key_issue_suggestion_fence_is_published_as_plain_code():
    issue = _key_issue(issue_content="Use this code:\n```suggestion\nlock.release()\n```")
    reviewer, data = _reviewer_with_findings(issue)

    reviewer._publish_key_issues_as_inline_comments(data)

    body = _published_comment(reviewer.git_provider)["body"]
    assert "```suggestion" not in body
    assert "```text" in body


def test_should_publish_review_no_suggestions_respects_config():
    reviewer = _make_reviewer()
    settings = get_settings()
    original_publish_no_suggestions = settings.pr_reviewer.publish_output_no_suggestions

    try:
        settings.pr_reviewer.publish_output_no_suggestions = False
        assert reviewer._should_publish_review_no_suggestions("No major issues detected") is False
        assert reviewer._should_publish_review_no_suggestions("A major issue was detected") is True

        settings.pr_reviewer.publish_output_no_suggestions = True
        assert reviewer._should_publish_review_no_suggestions("No major issues detected") is True
    finally:
        settings.pr_reviewer.publish_output_no_suggestions = original_publish_no_suggestions


@pytest.mark.asyncio
async def test_run_removes_its_progress_comment_when_quiet_output_suppresses_review(monkeypatch):
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    progress_comment = MagicMock()
    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    git_provider.publish_comment.return_value = progress_comment
    reviewer = _make_reviewer(git_provider)
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}
    reviewer.prediction = None
    reviewer._prepare_pr_review = lambda: "No major issues detected"

    async def fake_retry(prepare_fn, model_type=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", fake_retry)

    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "publish_output_no_suggestions": settings.pr_reviewer.publish_output_no_suggestions,
        "is_auto_command": settings.config.get("is_auto_command", False),
    }
    try:
        settings.config.publish_output = True
        settings.config.is_auto_command = False
        settings.pr_reviewer.publish_output_no_suggestions = False

        await reviewer.run()
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.config.is_auto_command = original["is_auto_command"]
        settings.pr_reviewer.publish_output_no_suggestions = original["publish_output_no_suggestions"]

    git_provider.publish_comment.assert_called_once_with("Preparing review...", is_temporary=True)
    git_provider.remove_comment.assert_called_once_with(progress_comment)
    git_provider.remove_initial_comment.assert_not_called()
    git_provider.publish_persistent_comment.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("propagate_tool_errors", [False, True])
async def test_run_removes_its_progress_comment_when_review_generation_fails(
        monkeypatch, propagate_tool_errors):
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    progress_comment = MagicMock()
    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    git_provider.publish_comment.return_value = progress_comment
    reviewer = _make_reviewer(git_provider)
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}
    reviewer.prediction = None

    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    review_error = RuntimeError("model unavailable")
    monkeypatch.setattr(
        pr_reviewer_module,
        "retry_with_fallback_models",
        AsyncMock(side_effect=review_error),
    )

    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "is_auto_command": settings.config.get("is_auto_command", False),
        "propagate_tool_errors": settings.config.get("propagate_tool_errors", False),
    }
    try:
        settings.config.publish_output = True
        settings.config.is_auto_command = False
        settings.config.propagate_tool_errors = propagate_tool_errors

        if propagate_tool_errors:
            with pytest.raises(RuntimeError, match="model unavailable") as exc_info:
                await reviewer.run()
            assert exc_info.value is review_error
        else:
            await reviewer.run()
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.config.is_auto_command = original["is_auto_command"]
        settings.config.propagate_tool_errors = original["propagate_tool_errors"]

    assert git_provider.publish_comment.call_args_list == [
        (("Preparing review...",), {"is_temporary": True}),
        (("Failed to review PR",), {}),
    ]
    git_provider.remove_comment.assert_called_once_with(progress_comment)
    git_provider.remove_initial_comment.assert_not_called()


@pytest.mark.asyncio
async def test_run_publishes_failure_result_when_progress_comment_has_no_handle(monkeypatch):
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    git_provider.publish_comment.side_effect = [None, MagicMock()]
    reviewer = _make_reviewer(git_provider)
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}
    reviewer.prediction = None

    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(
        pr_reviewer_module,
        "retry_with_fallback_models",
        AsyncMock(side_effect=RuntimeError("model unavailable")),
    )

    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "is_auto_command": settings.config.get("is_auto_command", False),
        "propagate_tool_errors": settings.config.get("propagate_tool_errors", False),
    }
    try:
        settings.config.publish_output = True
        settings.config.is_auto_command = False
        settings.config.propagate_tool_errors = False

        await reviewer.run()
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.config.is_auto_command = original["is_auto_command"]
        settings.config.propagate_tool_errors = original["propagate_tool_errors"]

    assert git_provider.publish_comment.call_args_list == [
        (("Preparing review...",), {"is_temporary": True}),
        (("Failed to review PR",), {}),
    ]
    git_provider.remove_comment.assert_not_called()
    git_provider.remove_initial_comment.assert_not_called()


@pytest.mark.asyncio
async def test_run_publishes_failure_result_when_review_fails_before_progress_comment():
    review_error = RuntimeError("files unavailable")
    git_provider = MagicMock()
    git_provider.get_files.side_effect = review_error
    reviewer = _make_reviewer(git_provider)

    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "is_auto_command": settings.config.get("is_auto_command", False),
        "propagate_tool_errors": settings.config.get("propagate_tool_errors", False),
    }
    try:
        settings.config.publish_output = True
        settings.config.is_auto_command = False
        settings.config.propagate_tool_errors = False

        await reviewer.run()
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.config.is_auto_command = original["is_auto_command"]
        settings.config.propagate_tool_errors = original["propagate_tool_errors"]

    git_provider.publish_comment.assert_called_once_with("Failed to review PR")
    git_provider.remove_comment.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("publish_output", "is_auto_command", "propagate_tool_errors"),
    [
        (False, False, False),
        (False, False, True),
        (True, True, False),
        (True, True, True),
    ],
)
async def test_run_does_not_publish_failure_result_when_output_disabled_or_auto(
        monkeypatch, publish_output, is_auto_command, propagate_tool_errors):
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    reviewer = _make_reviewer(git_provider)
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}
    reviewer.prediction = None

    review_error = RuntimeError("model unavailable")
    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(
        pr_reviewer_module,
        "retry_with_fallback_models",
        AsyncMock(side_effect=review_error),
    )

    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "is_auto_command": settings.config.get("is_auto_command", False),
        "propagate_tool_errors": settings.config.get("propagate_tool_errors", False),
    }
    try:
        settings.config.publish_output = publish_output
        settings.config.is_auto_command = is_auto_command
        settings.config.propagate_tool_errors = propagate_tool_errors

        if propagate_tool_errors:
            with pytest.raises(RuntimeError, match="model unavailable") as exc_info:
                await reviewer.run()
            assert exc_info.value is review_error
        else:
            await reviewer.run()
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.config.is_auto_command = original["is_auto_command"]
        settings.config.propagate_tool_errors = original["propagate_tool_errors"]

    git_provider.publish_comment.assert_not_called()
    git_provider.remove_comment.assert_not_called()
    git_provider.remove_initial_comment.assert_not_called()


@pytest.mark.asyncio
async def test_run_publishes_failure_result_when_progress_cleanup_fails(monkeypatch):
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    progress_comment = MagicMock()
    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    git_provider.publish_comment.return_value = progress_comment
    git_provider.remove_comment.side_effect = RuntimeError("cleanup failed")
    reviewer = _make_reviewer(git_provider)
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}
    reviewer.prediction = None

    review_error = RuntimeError("model unavailable")
    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(
        pr_reviewer_module,
        "retry_with_fallback_models",
        AsyncMock(side_effect=review_error),
    )

    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "is_auto_command": settings.config.get("is_auto_command", False),
        "propagate_tool_errors": settings.config.get("propagate_tool_errors", False),
    }
    try:
        settings.config.publish_output = True
        settings.config.is_auto_command = False
        settings.config.propagate_tool_errors = True

        with pytest.raises(RuntimeError, match="model unavailable") as exc_info:
            await reviewer.run()
        assert exc_info.value is review_error
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.config.is_auto_command = original["is_auto_command"]
        settings.config.propagate_tool_errors = original["propagate_tool_errors"]

    assert git_provider.publish_comment.call_args_list == [
        (("Preparing review...",), {"is_temporary": True}),
        (("Failed to review PR",), {}),
    ]
    git_provider.remove_comment.assert_called_once_with(progress_comment)


@pytest.mark.asyncio
@pytest.mark.parametrize("propagate_tool_errors", [False, True])
async def test_run_failure_result_publication_does_not_mask_review_error(
        monkeypatch, propagate_tool_errors):
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    progress_comment = MagicMock()
    publication_error = RuntimeError("comment unavailable")
    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    git_provider.publish_comment.side_effect = [progress_comment, publication_error]
    reviewer = _make_reviewer(git_provider)
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}
    reviewer.prediction = None

    review_error = RuntimeError("model unavailable")
    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(
        pr_reviewer_module,
        "retry_with_fallback_models",
        AsyncMock(side_effect=review_error),
    )

    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "is_auto_command": settings.config.get("is_auto_command", False),
        "propagate_tool_errors": settings.config.get("propagate_tool_errors", False),
    }
    try:
        settings.config.publish_output = True
        settings.config.is_auto_command = False
        settings.config.propagate_tool_errors = propagate_tool_errors

        if propagate_tool_errors:
            with pytest.raises(RuntimeError, match="model unavailable") as exc_info:
                await reviewer.run()
            assert exc_info.value is review_error
        else:
            await reviewer.run()
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.config.is_auto_command = original["is_auto_command"]
        settings.config.propagate_tool_errors = original["propagate_tool_errors"]

    assert git_provider.publish_comment.call_args_list == [
        (("Preparing review...",), {"is_temporary": True}),
        (("Failed to review PR",), {}),
    ]
    git_provider.remove_comment.assert_called_once_with(progress_comment)


def test_prepare_review_publishes_provider_neutral_structured_data(monkeypatch):
    git_provider = MagicMock()
    git_provider.is_supported.return_value = False
    git_provider.get_diff_files.return_value = []
    reviewer = _make_prediction_reviewer(git_provider)
    reviewer.prediction = """review:
  key_issues_to_review: []
  security_concerns: no
"""
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.set_review_labels = MagicMock()
    monkeypatch.setattr(
        "pr_agent.tools.pr_reviewer.convert_to_markdown_v2",
        lambda *args, **kwargs: "## Review",
    )

    from pr_agent.algo.run_details import add_token_usage, init_run_details

    init_run_details()
    add_token_usage({"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42})

    reviewer._prepare_pr_review()

    git_provider.publish_structured_review.assert_called_once_with({
        "review": {
            "key_issues_to_review": [],
            "security_concerns": False,
        },
        "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
    })
    # Assert key order to prove the snapshot is isolated: _prepare_pr_review moves
    # key_issues_to_review to the end of its own dict after the hook fires, so an
    # aliased snapshot ends with it while a deep copy keeps the original order.
    # (assert_called_once_with cannot catch this: dict equality ignores key order.)
    published = git_provider.publish_structured_review.call_args[0][0]
    assert list(published["review"].keys()) == ["key_issues_to_review", "security_concerns"]


def test_can_run_incremental_review_skips_auto_mode_without_new_commit():
    reviewer = _make_reviewer()
    reviewer.is_auto = True
    reviewer.incremental = SimpleNamespace(first_new_commit_sha=None)

    assert reviewer._can_run_incremental_review() is False


def test_set_review_labels_replaces_stale_review_labels_and_keeps_user_labels():
    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "require_estimate_effort_to_review": settings.pr_reviewer.require_estimate_effort_to_review,
        "require_security_review": settings.pr_reviewer.require_security_review,
        "enable_review_labels_effort": settings.pr_reviewer.enable_review_labels_effort,
        "enable_review_labels_security": settings.pr_reviewer.enable_review_labels_security,
    }
    settings.config.publish_output = True
    settings.pr_reviewer.require_estimate_effort_to_review = True
    settings.pr_reviewer.require_security_review = True
    settings.pr_reviewer.enable_review_labels_effort = True
    settings.pr_reviewer.enable_review_labels_security = True
    git_provider = MagicMock()
    git_provider.get_pr_labels.return_value = ["Review effort 1/5", "Possible security concern", "keep-me"]
    reviewer = _make_reviewer(git_provider)
    data = {
        "review": {
            "estimated_effort_to_review_[1-5]": "3, moderate",
            "security_concerns": "yes",
        }
    }

    try:
        reviewer.set_review_labels(data)

        git_provider.publish_labels.assert_called_once_with([
            "Review effort 3/5",
            "Possible security concern",
            "keep-me",
        ])
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.pr_reviewer.require_estimate_effort_to_review = original["require_estimate_effort_to_review"]
        settings.pr_reviewer.require_security_review = original["require_security_review"]
        settings.pr_reviewer.enable_review_labels_effort = original["enable_review_labels_effort"]
        settings.pr_reviewer.enable_review_labels_security = original["enable_review_labels_security"]


def test_get_user_answers_collects_question_and_answer_from_issue_comments():
    git_provider = MagicMock()
    git_provider.get_issue_comments.return_value = [
        SimpleNamespace(body="Unrelated"),
        SimpleNamespace(body="Questions to better understand the PR:\n- Why?"),
        SimpleNamespace(body="/answer Because it fixes production."),
    ]
    reviewer = _make_reviewer(git_provider)
    reviewer.is_answer = True

    question, answer = reviewer._get_user_answers()

    assert question == "Questions to better understand the PR:\n- Why?"
    assert answer == "/answer Because it fixes production."


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("thread_enabled", [True, False])
async def test_run_threads_only_the_final_review_comment(monkeypatch, persistent, thread_enabled):
    """`as_thread` is forwarded to the review's final publish call only when the provider opts in
    (should_publish_review_as_thread), and is omitted entirely otherwise - other providers'
    publish methods don't accept it. Status/progress comments are never threaded.
    """
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    progress_comment = MagicMock()
    git_provider = MagicMock()
    git_provider.should_publish_review_as_thread.return_value = thread_enabled
    git_provider.publish_comment.return_value = progress_comment
    reviewer = _make_reviewer(git_provider)
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}
    reviewer.prediction = None
    review_text = "## PR Reviewer Guide 🔍\n\nsome findings"
    reviewer._prepare_pr_review = lambda: review_text

    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", fake_extract_tickets)
    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", fake_retry)

    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "persistent_comment": settings.pr_reviewer.persistent_comment,
        "is_auto_command": settings.config.get("is_auto_command", False),
    }
    try:
        settings.config.publish_output = True
        settings.config.is_auto_command = False
        settings.pr_reviewer.persistent_comment = persistent

        await reviewer.run()
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.config.is_auto_command = original["is_auto_command"]
        settings.pr_reviewer.persistent_comment = original["persistent_comment"]

    if persistent:
        publish = git_provider.publish_persistent_comment
        publish.assert_called_once()
    else:
        publish = git_provider.publish_comment
    assert publish.call_args.args[0] == review_text
    if thread_enabled:
        assert publish.call_args.kwargs.get("as_thread") is True
    else:
        assert "as_thread" not in publish.call_args.kwargs
    # The temporary progress comment is published without as_thread regardless of the flag.
    git_provider.publish_comment.assert_any_call("Preparing review...", is_temporary=True)
    git_provider.remove_comment.assert_called_once_with(progress_comment)
    git_provider.remove_initial_comment.assert_not_called()


def test_init_maps_user_question_and_answer_to_correct_prompt_vars(monkeypatch):
    """Behavioral regression for the swapped-unpacking bug (#2496).

    The bug lived in ``PRReviewer.__init__``: ``_get_user_answers()`` returns
    ``(question, answer)`` but the tuple was unpacked as ``answer, question``,
    so the review prompt rendered the user's answer under ``{{ question_str }}``
    and the question under ``{{ answer_str }}``. This drives the real ``__init__``
    (external collaborators stubbed) and asserts each value lands in ``self.vars``
    under the correct key — so it fails if the unpack is ever swapped again,
    regardless of how the line is formatted.
    """
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    provider = MagicMock()
    provider.is_supported.return_value = True
    provider.get_languages.return_value = {}
    provider.get_files.return_value = []
    provider.get_issue_comments.return_value = [
        SimpleNamespace(body="Questions to better understand the PR:\n- Why?"),
        SimpleNamespace(body="/answer Because it fixes production."),
    ]
    provider.get_pr_description.return_value = ("desc", [])

    monkeypatch.setattr(pr_reviewer_module, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(pr_reviewer_module, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", MagicMock())

    reviewer = PRReviewer(
        "https://example/pr/1",
        is_answer=True,
        ai_handler=lambda: SimpleNamespace(main_pr_language=None),
    )

    assert reviewer.vars["question_str"] == "Questions to better understand the PR:\n- Why?"
    assert reviewer.vars["answer_str"] == "/answer Because it fixes production."


def _build_answer_mode_reviewer(monkeypatch, issue_comments):
    """Drive the real ``PRReviewer.__init__`` in answer mode over ``issue_comments``."""
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    provider = MagicMock()
    provider.is_supported.return_value = True
    provider.get_languages.return_value = {}
    provider.get_files.return_value = []
    provider.get_issue_comments.return_value = issue_comments
    provider.get_pr_description.return_value = ("desc", [])

    monkeypatch.setattr(pr_reviewer_module, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(pr_reviewer_module, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", MagicMock())

    return PRReviewer(
        "https://example/pr/1",
        is_answer=True,
        ai_handler=lambda: SimpleNamespace(main_pr_language=None),
    )


def test_answer_mode_reads_comments_from_a_non_list_iterable(monkeypatch):
    """GitHub hands back a PyGithub ``PaginatedList``, GitLab a plain list.

    Answer mode used to reach for the PyGithub-only ``.reversed`` property, which meant
    it could only ever consume the GitHub shape. Any lazily-paginated iterable must work.
    """

    class _Paginated:
        def __init__(self, items):
            self._items = items

        def __iter__(self):
            return iter(self._items)

    reviewer = _build_answer_mode_reviewer(monkeypatch, _Paginated([
        SimpleNamespace(body="Questions to better understand the PR:\n- Why?"),
        SimpleNamespace(body="/answer Because it fixes production."),
    ]))

    assert reviewer.vars["question_str"] == "Questions to better understand the PR:\n- Why?"
    assert reviewer.vars["answer_str"] == "/answer Because it fixes production."


def test_answer_mode_uses_the_lazy_reversed_view_when_the_provider_offers_one(monkeypatch):
    """PyGithub reverses a PaginatedList lazily, walking pages from the end.

    Materialising it instead would page the whole thread just to read the last exchange,
    so the lazy view must win when it exists.
    """

    class _LazyPaginated:
        def __init__(self, items):
            self._items = items

        @property
        def reversed(self):
            return list(reversed(self._items))

        def __iter__(self):
            raise AssertionError("the lazy reversed view should have been used")

    reviewer = _build_answer_mode_reviewer(monkeypatch, _LazyPaginated([
        SimpleNamespace(body="Questions to better understand the PR:\n- Why?"),
        SimpleNamespace(body="/answer Because it fixes production."),
    ]))

    assert reviewer.vars["question_str"] == "Questions to better understand the PR:\n- Why?"
    assert reviewer.vars["answer_str"] == "/answer Because it fixes production."


def test_answer_mode_prefers_the_newest_question_and_answer(monkeypatch):
    """Comments arrive oldest-first, so the walk must run newest-first to pick the latest exchange."""
    reviewer = _build_answer_mode_reviewer(monkeypatch, [
        SimpleNamespace(body="Questions to better understand the PR:\n- Stale question?"),
        SimpleNamespace(body="/answer Stale answer."),
        SimpleNamespace(body="Questions to better understand the PR:\n- Current question?"),
        SimpleNamespace(body="/answer Current answer."),
    ])

    assert reviewer.vars["question_str"] == "Questions to better understand the PR:\n- Current question?"
    assert reviewer.vars["answer_str"] == "/answer Current answer."
