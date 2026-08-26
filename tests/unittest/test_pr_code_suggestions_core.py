from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import (PRCodeSuggestionsHeader,
                                 PRCodeSuggestionsIdentity)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider, IncrementalPR
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


def _make_tool(git_provider=None):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = git_provider or MagicMock()
    tool.progress_response = None
    return tool


def _valid_suggestion(**overrides):
    suggestion = {
        "one_sentence_summary": "Avoid duplicated work",
        "label": "maintainability",
        "relevant_file": "app.py",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
        "suggestion_content": "Use the shared helper.",
        "existing_code": "old()",
        "improved_code": "new()",
    }
    suggestion.update(overrides)
    return suggestion


def test_prepare_pr_code_suggestions_filters_duplicates_and_missing_required_fields():
    tool = _make_tool()
    prediction = """
code_suggestions:
  - one_sentence_summary: Avoid duplicated work
    label: maintainability
    relevant_file: app.py
    suggestion_content: Use the shared helper.
    existing_code: old()
    improved_code: new()
  - one_sentence_summary: Avoid duplicated work
    label: maintainability
    relevant_file: app.py
    suggestion_content: Duplicate summary.
    existing_code: old()
    improved_code: newer()
  - one_sentence_summary: Missing label
    relevant_file: app.py
    suggestion_content: Missing label should be skipped.
    existing_code: old()
    improved_code: new()
"""

    data = tool._prepare_pr_code_suggestions(prediction)

    assert len(data["code_suggestions"]) == 1
    assert data["code_suggestions"][0]["one_sentence_summary"] == "Avoid duplicated work"
    assert data["code_suggestions"][0]["improved_code"] == "new()"


def test_prepare_pr_code_suggestions_renames_critical_label_when_focusing_only_on_problems():
    settings = get_settings()
    original_focus = settings.get("pr_code_suggestions.focus_only_on_problems", False)
    settings.set("pr_code_suggestions.focus_only_on_problems", True)
    tool = _make_tool()
    prediction = """
code_suggestions:
  - one_sentence_summary: Fix unsafe behavior
    label: critical issue
    relevant_file: app.py
    suggestion_content: Guard this path.
    existing_code: old()
    improved_code: new()
"""

    try:
        data = tool._prepare_pr_code_suggestions(prediction)

        assert data["code_suggestions"][0]["label"] == "possible issue"
    finally:
        settings.set("pr_code_suggestions.focus_only_on_problems", original_focus)


@pytest.mark.asyncio
async def test_analyze_self_reflection_response_merges_scores_and_zeroes_invalid_ranges():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = []
    tool = _make_tool(git_provider)
    settings = get_settings()
    original_publish_output = settings.config.publish_output
    settings.config.publish_output = False
    suggestion = _valid_suggestion()
    suggestion.pop("relevant_lines_start")
    suggestion.pop("relevant_lines_end")
    data = {"code_suggestions": [suggestion]}
    response_reflect = """
code_suggestions:
  - suggestion_score: 9
    why: Great suggestion, but line range is missing.
    relevant_lines_start: -1
    relevant_lines_end: -1
"""

    try:
        await tool.analyze_self_reflection_response(data, response_reflect)

        assert data["code_suggestions"][0]["score"] == 0
        assert data["code_suggestions"][0]["score_why"] == "Great suggestion, but line range is missing."
        assert data["code_suggestions"][0]["relevant_lines_start"] == -1
        assert data["code_suggestions"][0]["relevant_lines_end"] == -1
    finally:
        settings.config.publish_output = original_publish_output


def test_dedent_code_matches_target_file_indentation():
    git_provider = MagicMock()
    git_provider.diff_files = [
        FilePatchInfo(
            base_file="",
            head_file="def f():\n    return old()\n",
            patch="",
            filename="app.py",
        )
    ]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 2, "return new()") == "    return new()"


def test_dedent_code_uses_patch_when_file_content_is_unavailable():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="",
        patch="@@ -1,2 +1,2 @@\n def f():\n-    return older()\n+    return old()\n",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 2, "return new()") == "    return new()"


@pytest.mark.asyncio
async def test_push_inline_code_suggestions_falls_back_to_individual_publish_calls():
    git_provider = MagicMock()
    git_provider.diff_files = [
        FilePatchInfo(
            base_file="",
            head_file="def f():\n    return old()\n",
            patch="",
            filename="app.py",
        ),
        FilePatchInfo(
            base_file="",
            head_file="def work():\n    return old_worker()\n",
            patch="",
            filename="worker.py",
        ),
    ]
    git_provider.publish_code_suggestions.side_effect = [False, True, True]
    tool = _make_tool(git_provider)
    data = {"code_suggestions": [
        _valid_suggestion(
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old()",
            improved_code="return new()",
            score=8,
        ),
        _valid_suggestion(
            relevant_file="worker.py",
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old_worker()",
            improved_code="return new_worker()",
            suggestion_content="Keep the worker result fresh.",
        ),
    ]}

    await tool.push_inline_code_suggestions(data)

    assert git_provider.publish_code_suggestions.call_count == 3
    batch_call = git_provider.publish_code_suggestions.call_args_list[0].args[0]
    first_retry = git_provider.publish_code_suggestions.call_args_list[1].args[0]
    second_retry = git_provider.publish_code_suggestions.call_args_list[2].args[0]
    assert len(batch_call) == 2
    assert first_retry == [batch_call[0]]
    assert second_retry == [batch_call[1]]
    assert first_retry[0]["relevant_file"] == "app.py"
    assert first_retry[0]["relevant_lines_start"] == 2
    assert first_retry[0]["relevant_lines_end"] == 2
    assert "```suggestion\n    return new()" in first_retry[0]["body"]
    assert second_retry[0]["relevant_file"] == "worker.py"
    assert second_retry[0]["relevant_lines_start"] == 2
    assert second_retry[0]["relevant_lines_end"] == 2
    assert "```suggestion\n    return new_worker()" in second_retry[0]["body"]


@pytest.fixture
def publish_output_no_suggestions():
    settings = get_settings()
    original = settings.get("pr_code_suggestions.publish_output_no_suggestions", True)

    def _set(value):
        settings.set("pr_code_suggestions.publish_output_no_suggestions", value)

    yield _set
    _set(original)


@pytest.mark.asyncio
async def test_publish_no_suggestions_removes_the_progress_comment_when_quiet(publish_output_no_suggestions):
    publish_output_no_suggestions(False)
    git_provider = MagicMock()
    tool = _make_tool(git_provider)
    tool.progress_response = MagicMock()

    await tool.publish_no_suggestions()

    git_provider.remove_comment.assert_called_once_with(tool.progress_response)
    git_provider.edit_comment.assert_not_called()
    git_provider.publish_comment.assert_not_called()


def _provider_with_file(head_file, filename="app.py"):
    git_provider = MagicMock()
    git_provider.diff_files = [
        FilePatchInfo(base_file="", head_file=head_file, patch="", filename=filename)
    ]
    git_provider.publish_code_suggestions.return_value = True
    return git_provider


def _published_suggestion(git_provider):
    published = git_provider.publish_code_suggestions.call_args_list[0].args[0]
    assert len(published) == 1
    return published[0]


@pytest.mark.asyncio
async def test_suggestion_covering_the_anchored_range_is_published_as_committable():
    git_provider = _provider_with_file("def f():\n    return old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old()",
            improved_code="return new()",
            score=8,
        )
    ]})

    assert "```suggestion\n    return new()\n```" in _published_suggestion(git_provider)["body"]


@pytest.mark.asyncio
async def test_suggestion_rewriting_more_lines_than_it_replaces_is_published_as_a_plain_comment():
    git_provider = _provider_with_file("def f():\n    return old(\n        arg)\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old(\n    arg)",
            improved_code="return new(arg)",
            score=8,
        )
    ]})

    body = _published_suggestion(git_provider)["body"]
    assert "```suggestion" not in body
    assert "return new(arg)" in body


@pytest.mark.asyncio
async def test_suggestion_anchored_outside_the_file_is_published_as_a_plain_comment():
    git_provider = _provider_with_file("def f():\n    return old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(relevant_lines_start=40, relevant_lines_end=41, score=8)
    ]})

    git_provider.publish_code_suggestions.assert_not_called()
    body = git_provider.publish_comment.call_args.args[0]
    assert "```suggestion" not in body
    assert "`app.py:40-41`" in body
    assert "because the anchored range is outside the file" in body


@pytest.mark.asyncio
async def test_suggestion_with_reversed_range_is_published_as_a_pr_comment():
    git_provider = _provider_with_file("def f():\n    return old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(relevant_lines_start=2, relevant_lines_end=1, score=8)
    ]})

    git_provider.publish_code_suggestions.assert_not_called()
    body = git_provider.publish_comment.call_args.args[0]
    assert "`app.py:2-1`" in body
    assert "because the anchored range is outside the file" in body


@pytest.mark.asyncio
async def test_provider_diff_failure_is_not_treated_as_a_malformed_suggestion():
    git_provider = MagicMock()
    git_provider.diff_files = None
    git_provider.get_diff_files.side_effect = RuntimeError("provider failed")
    tool = _make_tool(git_provider)

    with pytest.raises(RuntimeError, match="provider failed"):
        await tool.push_inline_code_suggestions({"code_suggestions": [_valid_suggestion(score=8)]})

    git_provider.publish_code_suggestions.assert_not_called()
    git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
async def test_run_tracks_non_gfm_progress_comment_when_quiet(publish_output_no_suggestions):
    publish_output_no_suggestions(False)
    settings = get_settings()
    original_publish_output = settings.config.publish_output
    original_publish_output_progress = settings.config.publish_output_progress
    original_is_auto_command = settings.config.get("is_auto_command", False)
    settings.config.publish_output = True
    settings.config.publish_output_progress = True
    settings.config.is_auto_command = False
    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    git_provider.is_supported.return_value = False
    progress_comment = MagicMock()
    git_provider.publish_comment.return_value = progress_comment
    tool = _make_tool(git_provider)
    tool.pr_url = "https://example.test/pull/1"
    tool.progress = "Preparing suggestions..."
    tool.prepare_prediction_main = AsyncMock()

    try:
        with (patch("pr_agent.tools.pr_code_suggestions.init_run_details"),
              patch("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                    AsyncMock(return_value={"code_suggestions": []}))):
            await tool.run()
    finally:
        settings.config.publish_output = original_publish_output
        settings.config.publish_output_progress = original_publish_output_progress
        settings.config.is_auto_command = original_is_auto_command

    git_provider.publish_comment.assert_called_once_with("Preparing suggestions...", is_temporary=True)
    git_provider.remove_comment.assert_called_once_with(progress_comment)


@pytest.mark.asyncio
async def test_publish_no_suggestions_does_not_remove_unrelated_temporary_comments(publish_output_no_suggestions):
    publish_output_no_suggestions(False)
    git_provider = MagicMock()
    tool = _make_tool(git_provider)

    await tool.publish_no_suggestions()

    git_provider.remove_initial_comment.assert_not_called()
    git_provider.remove_comment.assert_not_called()
    git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
async def test_publish_no_suggestions_still_overwrites_the_progress_comment_when_publishing(
        publish_output_no_suggestions):
    publish_output_no_suggestions(True)
    git_provider = MagicMock()
    tool = _make_tool(git_provider)
    tool.progress_response = MagicMock()

    await tool.publish_no_suggestions()

    _, kwargs = git_provider.edit_comment.call_args
    assert "No code suggestions found for the PR." in kwargs["body"]
    git_provider.remove_comment.assert_not_called()


def test_setup_incremental_scope_calls_provider_when_supported():
    git_provider = MagicMock()
    git_provider.supports_incremental_kind.return_value = True
    tool = _make_tool(git_provider)
    tool.incremental = IncrementalPR(True)

    tool._setup_incremental_scope()

    git_provider.supports_incremental_kind.assert_called_once_with("suggestions")
    git_provider.get_incremental_commits.assert_called_once_with(tool.incremental, kind="suggestions")
    assert tool.incremental.is_incremental is True


def test_setup_incremental_scope_falls_back_when_unsupported():
    git_provider = MagicMock()
    git_provider.supports_incremental_kind.return_value = False
    tool = _make_tool(git_provider)
    tool.incremental = IncrementalPR(True)

    tool._setup_incremental_scope()

    git_provider.get_incremental_commits.assert_not_called()
    assert tool.incremental.is_incremental is False


def test_setup_incremental_scope_noop_without_incremental_flag():
    git_provider = MagicMock()
    tool = _make_tool(git_provider)
    tool.incremental = IncrementalPR(False)

    tool._setup_incremental_scope()

    git_provider.supports_incremental_kind.assert_not_called()
    git_provider.get_incremental_commits.assert_not_called()


def test_supports_incremental_kind_defaults_to_false_on_base_provider():
    # The base-class default must be "no support" so tools fall back to a full run
    # on providers that never implemented kind-aware incremental anchoring.
    assert GitProvider.supports_incremental_kind(MagicMock(), "suggestions") is False


@pytest.mark.asyncio
async def test_malformed_suggestion_does_not_stop_later_suggestions():
    git_provider = _provider_with_file("old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(relevant_file=123, score=8),
        _valid_suggestion(score=8),
    ]})

    published = git_provider.publish_code_suggestions.call_args.args[0]
    assert len(published) == 1
    assert published[0]["relevant_file"] == "app.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_code", [123, ["old()"], {"code": "old()"}])
@pytest.mark.parametrize("improved_code", ["new()", ""])
async def test_non_string_existing_code_does_not_stop_later_suggestions(existing_code, improved_code):
    git_provider = _provider_with_file("old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(existing_code=existing_code, improved_code=improved_code, score=8),
        _valid_suggestion(score=8),
    ]})

    published = git_provider.publish_code_suggestions.call_args.args[0]
    assert len(published) == 1
    assert published[0]["original_suggestion"]["existing_code"] == "old()"


@pytest.mark.asyncio
async def test_advice_only_suggestion_is_published_instead_of_being_dropped():
    git_provider = _provider_with_file("def f():\n    return old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(relevant_lines_start=2, relevant_lines_end=2, improved_code="", score=8)
    ]})

    body = _published_suggestion(git_provider)["body"]
    assert "```suggestion" not in body
    assert "Use the shared helper." in body


@pytest.mark.asyncio
async def test_advice_only_suggestion_with_unverified_anchor_is_published_as_a_pr_comment():
    git_provider = MagicMock()
    git_provider.diff_files = None
    git_provider.get_diff_files.return_value = []
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(improved_code="", score=8)
    ]})

    git_provider.get_diff_files.assert_called_once_with()
    git_provider.publish_code_suggestions.assert_not_called()
    assert "Use the shared helper." in git_provider.publish_comment.call_args.args[0]


@pytest.mark.asyncio
async def test_dual_publishing_keeps_suggestions_without_replacement_code():
    settings = get_settings()
    original_threshold = settings.get("pr_code_suggestions.dual_publishing_score_threshold")
    settings.set("pr_code_suggestions.dual_publishing_score_threshold", 5)
    git_provider = _provider_with_file("def f():\n    return old()\n")
    tool = _make_tool(git_provider)

    try:
        await tool.dual_publishing({"code_suggestions": [
            _valid_suggestion(relevant_lines_start=2, relevant_lines_end=2, improved_code="", score=8)
        ]})

        assert "Use the shared helper." in _published_suggestion(git_provider)["body"]
    finally:
        settings.set("pr_code_suggestions.dual_publishing_score_threshold", original_threshold)


def test_is_applicable_suggestion_rejects_a_range_in_an_empty_file():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(base_file="", head_file="", patch="", filename="app.py")]
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 1, 1, "old()") is False


def test_is_applicable_suggestion_rejects_when_existing_code_does_not_cover_the_anchor():
    git_provider = _provider_with_file("def f():\n    first()\n    second()\n")
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 2, 3, "first()") is False


def test_is_applicable_suggestion_rejects_when_file_content_is_unavailable():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(base_file="", head_file=None, patch="", filename="app.py")]
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 1, 1, "old()") is False
    assert tool._suggestion_applyability("app.py", 1, 1, "old()")[1] == "the file content is unavailable"


def test_is_applicable_suggestion_uses_patch_when_file_content_is_unavailable():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="",
        patch="@@ -1,2 +1,2 @@\n def f():\n-    return older()\n+    return old()\n",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 2, 2, "return old()") is True


def test_is_applicable_suggestion_rejects_a_range_missing_from_the_patch():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="",
        patch="@@ -1 +1 @@\n-old()\n+new()\n",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 2, 2, "other()") is False


def test_get_diff_file_does_not_refetch_an_empty_cache():
    git_provider = MagicMock()
    git_provider.diff_files = []
    tool = _make_tool(git_provider)

    assert tool._get_diff_file("app.py") is None
    git_provider.get_diff_files.assert_not_called()


def test_is_applicable_suggestion_preserves_blank_line_positions():
    git_provider = _provider_with_file("first()\n\nsecond()\n")
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 1, 3, "first()\nsecond()") is False
    assert tool.is_applicable_suggestion("app.py", 1, 3, "first()\n\nsecond()") is True


def test_is_applicable_suggestion_preserves_relative_indentation():
    git_provider = _provider_with_file("if ready:\n    run()\n")
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 1, 2, "    if ready:\n        run()") is True
    assert tool.is_applicable_suggestion("app.py", 1, 2, "if ready:\nrun()") is False


def test_is_applicable_suggestion_uses_absolute_patch_lines_for_partial_head_content():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="first()\nlater()\n",
        head_file="first()\nlater()\n",
        patch=("@@ -10 +10 @@\n-old_first()\n+first()\n"
               "@@ -100 +100 @@\n-old_later()\n+later()\n"),
        filename="app.py",
        head_file_is_complete=False,
    )]
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 100, 100, "later()") is True


def test_persistent_update_survives_progress_cleanup_failure():
    """A failing progress-note cleanup must not abort the persistent update:
    if the cleanup error propagated, the caller would fall back to publishing
    a new suggestions thread, re-creating the duplicate-thread bug."""
    initial_header = "## PR Code Suggestions"
    existing = MagicMock()
    existing.body = f"{initial_header}\n<!-- aaa1111 -->\n<table>old suggestions</table>"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [existing]
    provider.get_comment_url.return_value = "https://example.test/comment/1"
    provider.get_latest_commit_url.return_value = "https://example.test/commit/deadbee"
    # First edit updates the persistent comment and succeeds; the second edit
    # (re-labelling the progress note before deletion) fails.
    provider.edit_comment.side_effect = [None, RuntimeError("cleanup failed")]
    progress_note = MagicMock()

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider, f"{initial_header}\n<table>new suggestions</table>", initial_header,
        update_header=False, name="suggestions", final_update_message=False,
        progress_response=progress_note)

    assert result is existing
    assert provider.edit_comment.call_count == 2
    provider.remove_comment.assert_not_called()
    provider.publish_comment.assert_not_called()


def _persistent_provider(existing_comments):
    provider = MagicMock()
    provider.get_issue_comments.return_value = existing_comments
    provider.get_comment_url.return_value = "https://example.test/comment/1"
    provider.get_latest_commit_url.return_value = "https://example.test/commit/deadbee"
    return provider


def test_custom_heading_migrates_legacy_persistent_suggestions_in_place():
    legacy = MagicMock()
    legacy.body = (
        f"{PRCodeSuggestionsHeader.SUMMARY.value}\n\n"
        "<!-- aaa1111 -->\n\n<table>old suggestions</table>"
    )
    provider = _persistent_provider([legacy])
    custom_header = "## Guideline Improvement Suggestions ✨"

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{custom_header}\n\n<table>new suggestions</table>",
        initial_header=custom_header,
        name="suggestions",
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is legacy
    updated = provider.edit_comment.call_args.args[1]
    assert updated.startswith(
        f"{custom_header}\n\n"
        f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        "<!-- deadbee -->"
    )
    assert "Suggestions up to commit aaa1111" in updated
    provider.publish_comment.assert_not_called()


def test_marked_persistent_suggestions_take_precedence_over_legacy_comment():
    legacy = MagicMock()
    legacy.body = (
        f"{PRCodeSuggestionsHeader.SUMMARY.value}\n\n"
        "<!-- aaa1111 -->\n\n<table>legacy</table>"
    )
    marked = MagicMock()
    marked.body = (
        "## Previous Custom Heading ✨\n\n"
        f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        "<!-- bbb2222 -->\n\n<table>marked</table>"
    )
    provider = _persistent_provider([legacy, marked])
    custom_header = "## Latest Custom Heading ✨"

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{custom_header}\n\n<table>new suggestions</table>",
        initial_header=custom_header,
        name="suggestions",
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is marked
    assert provider.edit_comment.call_args.args[0] is marked
    assert provider.edit_comment.call_args.args[1].startswith(
        f"{custom_header}\n\n{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
    )
    provider.publish_comment.assert_not_called()


def test_persistent_suggestions_do_not_adopt_quoted_or_late_identity():
    human = MagicMock()
    human.body = (
        "## Human discussion\n\n"
        f"> {PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        f"Quoted output:\n{PRCodeSuggestionsHeader.SUMMARY.value}\n"
    )
    provider = _persistent_provider([human])
    custom_header = "## Team Suggestions ✨"

    PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{custom_header}\n\n<table>new suggestions</table>",
        initial_header=custom_header,
        name="suggestions",
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    provider.edit_comment.assert_not_called()
    published = provider.publish_comment.call_args.args[0]
    assert published.startswith(
        f"{custom_header}\n\n"
        f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        "<!-- deadbee -->\n\n"
    )


def test_legacy_heading_without_generated_shape_is_not_adopted():
    human = MagicMock()
    human.body = (
        f"{PRCodeSuggestionsHeader.SUMMARY.value}\n\n"
        "A human-authored comment using the same heading."
    )
    provider = _persistent_provider([human])
    custom_header = "## Team Suggestions ✨"

    PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{custom_header}\n\n<table>new suggestions</table>",
        initial_header=custom_header,
        name="suggestions",
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    provider.edit_comment.assert_not_called()
    published = provider.publish_comment.call_args.args[0]
    assert PRCodeSuggestionsIdentity.SUMMARY.value in published
