from unittest.mock import MagicMock

import pytest

from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
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
    assert "```suggestion\n    new()" in first_retry[0]["body"]
    assert second_retry[0]["relevant_file"] == "worker.py"
    assert second_retry[0]["relevant_lines_start"] == 2
    assert second_retry[0]["relevant_lines_end"] == 2
    assert "```suggestion\n    return new_worker()" in second_retry[0]["body"]
