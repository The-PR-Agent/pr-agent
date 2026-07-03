from unittest.mock import MagicMock

import pytest

from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

TRUNCATION_SETTINGS = (
    "pr_code_suggestions.max_code_suggestion_length",
    "pr_code_suggestions.suggestion_truncation_message",
)


def _make_tool(git_provider=None):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = git_provider or MagicMock()
    tool.progress_response = None
    return tool


def _suggestion(**overrides):
    base = {
        "one_sentence_summary": "Use the shared helper",
        "label": "maintainability",
        "relevant_file": "app.py",
        "relevant_lines_start": 2,
        "relevant_lines_end": 2,
        "suggestion_content": "Use the shared helper.",
        "existing_code": "return old()",
        "improved_code": "return new()",
        "score": 7,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _truncate_if_needed
# ---------------------------------------------------------------------------

def test_truncate_if_needed_appends_message_when_over_limit():
    settings = get_settings()
    snapshot = snapshot_settings(TRUNCATION_SETTINGS)
    settings.set("pr_code_suggestions.max_code_suggestion_length", 10)
    settings.set("pr_code_suggestions.suggestion_truncation_message", "[truncated]")
    try:
        suggestion = _suggestion(improved_code="a" * 50)
        out = PRCodeSuggestions._truncate_if_needed(suggestion)
        # Truncated content + truncation message on a new line
        assert out["improved_code"].startswith("a" * 10)
        assert out["improved_code"].endswith("\n[truncated]")
        assert "a" * 11 not in out["improved_code"]
    finally:
        restore_settings(snapshot)


def test_truncate_if_needed_noop_when_under_limit_or_disabled():
    settings = get_settings()
    snapshot = snapshot_settings(TRUNCATION_SETTINGS)
    settings.set("pr_code_suggestions.max_code_suggestion_length", 100)
    settings.set("pr_code_suggestions.suggestion_truncation_message", "[truncated]")
    try:
        short = _suggestion(improved_code="short()")
        out = PRCodeSuggestions._truncate_if_needed(short)
        assert out["improved_code"] == "short()"

        # Disabled (0) leaves long content untouched
        settings.set("pr_code_suggestions.max_code_suggestion_length", 0)
        long_suggestion = _suggestion(improved_code="x" * 500)
        out = PRCodeSuggestions._truncate_if_needed(long_suggestion)
        assert out["improved_code"] == "x" * 500
        assert "[truncated]" not in out["improved_code"]
    finally:
        restore_settings(snapshot)


def test_prepare_pr_code_suggestions_applies_truncation_inline():
    settings = get_settings()
    snapshot = snapshot_settings(TRUNCATION_SETTINGS)
    settings.set("pr_code_suggestions.max_code_suggestion_length", 5)
    settings.set("pr_code_suggestions.suggestion_truncation_message", "[cut]")
    try:
        tool = _make_tool()
        prediction = """
code_suggestions:
  - one_sentence_summary: Inline truncation
    label: maintainability
    relevant_file: app.py
    suggestion_content: Trim me.
    existing_code: old()
    improved_code: aaaaaaaaaaaaaaaaaaaa
"""
        data = tool._prepare_pr_code_suggestions(prediction)
        assert len(data["code_suggestions"]) == 1
        improved = data["code_suggestions"][0]["improved_code"]
        assert improved.startswith("aaaaa")
        assert improved.endswith("\n[cut]")
    finally:
        restore_settings(snapshot)


# ---------------------------------------------------------------------------
# push_inline_code_suggestions: rendered body shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_push_inline_renders_body_with_score_and_label():
    git_provider = MagicMock()
    git_provider.diff_files = [
        FilePatchInfo(
            base_file="",
            head_file="def f():\n    return old()\n",
            patch="",
            filename="app.py",
        )
    ]
    git_provider.publish_code_suggestions.return_value = True
    tool = _make_tool(git_provider)
    data = {"code_suggestions": [_suggestion(score=8)]}

    await tool.push_inline_code_suggestions(data)

    args = git_provider.publish_code_suggestions.call_args.args[0]
    assert len(args) == 1
    body = args[0]["body"]
    assert body.startswith("**Suggestion:** Use the shared helper.")
    assert "[maintainability, importance: 8]" in body
    assert "```suggestion\n    return new()\n```" in body
    # original_suggestion is the unmodified dict
    assert args[0]["original_suggestion"]["one_sentence_summary"] == "Use the shared helper"


@pytest.mark.asyncio
async def test_push_inline_renders_body_without_score_when_missing_or_zero():
    git_provider = MagicMock()
    git_provider.diff_files = [
        FilePatchInfo(
            base_file="",
            head_file="def f():\n    return old()\n",
            patch="",
            filename="app.py",
        )
    ]
    git_provider.publish_code_suggestions.return_value = True
    tool = _make_tool(git_provider)
    suggestion = _suggestion()
    suggestion.pop("score")
    data = {"code_suggestions": [suggestion]}

    await tool.push_inline_code_suggestions(data)

    body = git_provider.publish_code_suggestions.call_args.args[0][0]["body"]
    assert "[maintainability]" in body
    assert "importance" not in body


@pytest.mark.asyncio
async def test_push_inline_publishes_no_suggestions_comment_when_empty():
    git_provider = MagicMock()
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": []})

    git_provider.publish_comment.assert_called_once_with(
        "No suggestions found to improve this PR."
    )
    git_provider.publish_code_suggestions.assert_not_called()


# ---------------------------------------------------------------------------
# generate_summarized_suggestions
# ---------------------------------------------------------------------------

def test_generate_summarized_suggestions_empty_returns_placeholder():
    tool = _make_tool()
    out = tool.generate_summarized_suggestions({"code_suggestions": []})
    assert "PR Code Suggestions" in out
    assert "No suggestions found to improve this PR." in out
    # No table is rendered when empty
    assert "<table>" not in out


def test_generate_summarized_suggestions_renders_table_and_sorts_by_score():
    git_provider = MagicMock()
    git_provider.get_line_link.return_value = "https://example.test/app.py#L2"
    tool = _make_tool(git_provider)
    settings = get_settings()
    snapshot = snapshot_settings(["pr_code_suggestions.new_score_mechanism"])
    settings.set("pr_code_suggestions.new_score_mechanism", False)
    try:
        low = _suggestion(one_sentence_summary="Lower scored tweak", score=3, label="maintainability")
        high = _suggestion(
            one_sentence_summary="Higher scored tweak",
            score=9,
            label="security",
            relevant_file="auth.py",
        )
        out = tool.generate_summarized_suggestions({"code_suggestions": [low, high]})

        assert "<table>" in out and "</table>" in out
        assert "<thead>" in out
        # Labels are capitalized in the rendered category column
        assert "Security" in out
        assert "Maintainability" in out
        # Higher score group appears before lower score group
        assert out.index("Security") < out.index("Maintainability")
        # Both suggestion summaries appear
        assert "Higher scored tweak" in out
        assert "Lower scored tweak" in out
        # Numeric score shown (new_score_mechanism disabled)
        assert ">9\n\n" in out
        assert ">3\n\n" in out
        # Diff block is rendered
        assert "```diff" in out
    finally:
        restore_settings(snapshot)


def test_generate_summarized_suggestions_uses_score_string_when_new_mechanism_enabled():
    git_provider = MagicMock()
    git_provider.get_line_link.return_value = ""
    tool = _make_tool(git_provider)
    settings = get_settings()
    snapshot = snapshot_settings(["pr_code_suggestions.new_score_mechanism"])
    settings.set("pr_code_suggestions.new_score_mechanism", True)
    try:
        out = tool.generate_summarized_suggestions({
            "code_suggestions": [_suggestion(score=9, one_sentence_summary="High one")]
        })
        # The new mechanism replaces numeric score with bucket label
        assert "High" in out
        # Plain numeric "9" should not be shown in the impact column
        assert ">9\n\n" not in out
    finally:
        restore_settings(snapshot)


def test_generate_summarized_suggestions_escapes_angle_bracket_strings_in_summary():
    git_provider = MagicMock()
    git_provider.get_line_link.return_value = ""
    tool = _make_tool(git_provider)
    suggestion = _suggestion(one_sentence_summary="Replace '<old_name>' with new_name")
    out = tool.generate_summarized_suggestions({"code_suggestions": [suggestion]})
    # The "'<...>'" pattern is rewritten with backticks, which replace_code_tags
    # then turns into an HTML <code> span with escaped angle brackets so it isn't
    # parsed as an HTML tag.
    assert "'<old_name>'" not in out
    assert "<code>&lt;old_name&gt;</code>" in out


def test_generate_summarized_suggestions_includes_score_why_block_when_present():
    git_provider = MagicMock()
    git_provider.get_line_link.return_value = ""
    tool = _make_tool(git_provider)
    suggestion = _suggestion(score_why="Catches a real bug.")
    out = tool.generate_summarized_suggestions({"code_suggestions": [suggestion]})
    assert "Suggestion importance[1-10]: 7" in out
    assert "Why: Catches a real bug." in out


# ---------------------------------------------------------------------------
# Stale one-liner validation
# ---------------------------------------------------------------------------

def test_validate_one_liner_zeroes_score_when_change_already_applied():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = [
        FilePatchInfo(
            base_file="def f():\n    return old()\n",
            head_file="def f():\n    return new()\n",
            patch="",
            filename="app.py",
        )
    ]
    tool = _make_tool(git_provider)
    suggestion = _suggestion(score=8, existing_code="return old()", improved_code="return new()")

    out = tool.validate_one_liner_suggestion_not_repeating_code(suggestion)

    assert out["score"] == 0


def test_validate_one_liner_keeps_score_when_existing_code_still_present():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = [
        FilePatchInfo(
            base_file="def f():\n    return old()\n",
            head_file="def f():\n    return old()\n",
            patch="",
            filename="app.py",
        )
    ]
    tool = _make_tool(git_provider)
    suggestion = _suggestion(score=8, existing_code="return old()", improved_code="return new()")

    out = tool.validate_one_liner_suggestion_not_repeating_code(suggestion)

    assert out["score"] == 8


def test_validate_one_liner_skips_when_existing_code_contains_ellipsis():
    git_provider = MagicMock()
    # Provide a diff_files target that would otherwise trigger the stale guard,
    # to confirm the early-return for "..." takes precedence.
    git_provider.get_diff_files.return_value = [
        FilePatchInfo(
            base_file="def f():\n    return old()\n",
            head_file="def f():\n    return new()\n",
            patch="",
            filename="app.py",
        )
    ]
    tool = _make_tool(git_provider)
    suggestion = _suggestion(
        score=8,
        existing_code="...\nreturn old()\n...",
        improved_code="return new()",
    )

    out = tool.validate_one_liner_suggestion_not_repeating_code(suggestion)

    # Score must remain untouched because the ellipsis early-return runs first.
    assert out["score"] == 8


# ---------------------------------------------------------------------------
# get_score_str thresholds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "score,expected",
    [(10, "High"), (9, "High"), (8, "Medium"), (7, "Medium"), (6, "Low"), (0, "Low")],
)
def test_get_score_str_returns_bucket_for_default_thresholds(score, expected):
    settings = get_settings()
    snapshot = snapshot_settings([
        "pr_code_suggestions.new_score_mechanism_th_high",
        "pr_code_suggestions.new_score_mechanism_th_medium",
    ])
    settings.set("pr_code_suggestions.new_score_mechanism_th_high", 9)
    settings.set("pr_code_suggestions.new_score_mechanism_th_medium", 7)
    try:
        tool = _make_tool()
        assert tool.get_score_str(score) == expected
    finally:
        restore_settings(snapshot)
