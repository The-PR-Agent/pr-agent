"""Focused tests for /improve filtering and quality-guard helpers.

These tests exercise pure-Python helpers on PRCodeSuggestions without
invoking any LLM or git provider network calls. The tool is constructed
via ``__new__`` and only the attributes touched by each helper are set.
"""
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import _REFLECTION_FAILURE_SCORE_WHY, PRCodeSuggestions
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


# ---------------------------------------------------------------------------
# _truncate_if_needed
# ---------------------------------------------------------------------------

def test_truncate_if_needed_noop_when_threshold_disabled():
    settings = get_settings()
    snapshot = snapshot_settings(TRUNCATION_SETTINGS)
    settings.set("pr_code_suggestions.max_code_suggestion_length", 0)
    try:
        suggestion = _valid_suggestion(improved_code="x" * 5000)
        result = PRCodeSuggestions._truncate_if_needed(suggestion)
        assert result["improved_code"] == "x" * 5000
    finally:
        restore_settings(snapshot)


def test_truncate_if_needed_truncates_and_appends_message():
    settings = get_settings()
    snapshot = snapshot_settings(TRUNCATION_SETTINGS)
    settings.set("pr_code_suggestions.max_code_suggestion_length", 10)
    settings.set("pr_code_suggestions.suggestion_truncation_message", "[...truncated]")
    try:
        suggestion = _valid_suggestion(improved_code="abcdefghijKLMNOP")
        result = PRCodeSuggestions._truncate_if_needed(suggestion)
        assert result["improved_code"].startswith("abcdefghij")
        assert "[...truncated]" in result["improved_code"]
        # Truncated body is exactly the first max_code_suggestion_length chars
        assert result["improved_code"].split("\n")[0] == "abcdefghij"
    finally:
        restore_settings(snapshot)


def test_truncate_if_needed_keeps_short_code_unchanged():
    settings = get_settings()
    snapshot = snapshot_settings(TRUNCATION_SETTINGS)
    settings.set("pr_code_suggestions.max_code_suggestion_length", 1000)
    try:
        suggestion = _valid_suggestion(improved_code="short()", _is_truncated=True)
        result = PRCodeSuggestions._truncate_if_needed(suggestion)
        assert result["improved_code"] == "short()"
        assert "_is_truncated" not in result
    finally:
        restore_settings(snapshot)


# ---------------------------------------------------------------------------
# validate_one_liner_suggestion_not_repeating_code (stale-suggestion guard)
# ---------------------------------------------------------------------------

def _patch_files(base_file, head_file, filename="app.py"):
    return [FilePatchInfo(base_file=base_file, head_file=head_file, patch="", filename=filename)]


@pytest.mark.asyncio
@pytest.mark.parametrize("indent", ["", "    ", "\t"])
async def test_inline_suggestion_preserves_unicode_separator_through_publication(indent):
    original = 'VALUE = "part\u2028one"'
    improved = 'VALUE = "better\u2028one"'
    prefix = "if True:\n" if indent else ""
    line_number = 2 if indent else 1
    git_provider = MagicMock()
    git_provider.diff_files = _patch_files(
        base_file=f"{prefix}{indent}{original}\n",
        head_file=f"{prefix}{indent}{original}\n",
    )
    git_provider.supports_code_suggestions_artifact.return_value = False
    git_provider.publish_code_suggestions.return_value = True
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        existing_code=original,
        improved_code=improved,
        relevant_lines_start=line_number,
        relevant_lines_end=line_number,
    )

    await tool.push_inline_code_suggestions({"code_suggestions": [suggestion]}, include_coverage_footer=False)

    published = git_provider.publish_code_suggestions.call_args.args[0]
    assert len(published) == 1
    assert f"```suggestion\n{indent}{improved}\n```" in published[0]["body"]
    assert published[0]["original_suggestion"]["improved_code"] == f"{indent}{improved}"


@pytest.mark.asyncio
@pytest.mark.parametrize("indent,proposal_indent", [("    ", ""), ("\t", ""), ("", "  ")])
async def test_inline_suggestion_keeps_separator_only_line_during_reindent(indent, proposal_indent):
    existing = "before\n\u2028\nafter"
    improved = f"{proposal_indent}one\n{proposal_indent}\u2028\n{proposal_indent}two"
    original_file = f"{indent}before\n{indent}\u2028\n{indent}after\n"
    git_provider = MagicMock()
    git_provider.diff_files = _patch_files(original_file, original_file, filename="notes.txt")
    git_provider.supports_code_suggestions_artifact.return_value = False
    git_provider.publish_code_suggestions.return_value = True
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        relevant_file="notes.txt", relevant_lines_start=1, relevant_lines_end=3,
        existing_code=existing, improved_code=improved,
    )

    await tool.push_inline_code_suggestions({"code_suggestions": [suggestion]}, include_coverage_footer=False)

    published = git_provider.publish_code_suggestions.call_args.args[0]
    assert len(published) == 1
    assert f"```suggestion\n{indent}one\n{indent}\u2028\n{indent}two\n```" in published[0]["body"]
    assert published[0]["original_suggestion"]["improved_code"] == f"{indent}one\n{indent}\u2028\n{indent}two"


@pytest.mark.asyncio
async def test_inline_suggestion_keeps_trailing_unicode_separator():
    existing = 'VALUE = "part"\u2028'
    improved = 'VALUE = "better"\u2028'
    git_provider = MagicMock()
    git_provider.diff_files = _patch_files(existing + "\n", existing + "\n", filename="notes.txt")
    git_provider.supports_code_suggestions_artifact.return_value = False
    git_provider.publish_code_suggestions.return_value = True
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        relevant_file="notes.txt", existing_code=existing, improved_code=improved,
    )

    await tool.push_inline_code_suggestions({"code_suggestions": [suggestion]}, include_coverage_footer=False)

    published = git_provider.publish_code_suggestions.call_args.args[0]
    assert f"```suggestion\n{improved}\n```" in published[0]["body"]
    assert published[0]["original_suggestion"]["improved_code"] == improved


def test_validate_one_liner_marks_stale_suggestion_as_score_zero():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = _patch_files(
        base_file="return old()\n", head_file="return new()\n"
    )
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        existing_code="return old()",
        improved_code="return new()",
        score=8,
    )

    result = tool.validate_one_liner_suggestion_not_repeating_code(suggestion)

    assert result["score"] == 0


def test_validate_one_liner_skips_when_existing_code_uses_ellipsis():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = _patch_files(
        base_file="return old()\n", head_file="return new()\n"
    )
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        existing_code="... old() ...",
        improved_code="return new()",
        score=8,
    )

    result = tool.validate_one_liner_suggestion_not_repeating_code(suggestion)

    # '...' short-circuits the check; original score is preserved.
    assert result["score"] == 8


def test_validate_one_liner_preserves_score_when_code_not_yet_applied():
    git_provider = MagicMock()
    # head still contains the old code: the patch hasn't applied the new code yet.
    git_provider.get_diff_files.return_value = _patch_files(
        base_file="return old()\n", head_file="return old()\n"
    )
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        existing_code="return old()",
        improved_code="return new()",
        score=8,
    )

    result = tool.validate_one_liner_suggestion_not_repeating_code(suggestion)

    assert result["score"] == 8


def test_validate_one_liner_preserves_score_when_filename_not_in_diff():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = _patch_files(
        base_file="return old()\n",
        head_file="return new()\n",
        filename="other.py",
    )
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        existing_code="return old()",
        improved_code="return new()",
        score=8,
    )

    result = tool.validate_one_liner_suggestion_not_repeating_code(suggestion)

    assert result["score"] == 8


def test_validate_one_liner_handles_empty_head_file_gracefully():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = _patch_files(
        base_file="return old()\n", head_file=""
    )
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        existing_code="return old()",
        improved_code="return new()",
        score=8,
    )

    result = tool.validate_one_liner_suggestion_not_repeating_code(suggestion)

    assert result["score"] == 8


# ---------------------------------------------------------------------------
# remove_line_numbers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_remove_line_numbers_strips_leading_digits_and_separator(newline):
    tool = _make_tool()
    tool.patches_diff_list = [
        "## File: app.py\n"
        "1 def f():\n"
        "2     return old()\n"
        "10    return other()\n"
    ]
    tool.patches_diff_list = [diff.replace("\n", newline) for diff in tool.patches_diff_list]

    result = tool.remove_line_numbers(tool.patches_diff_list)

    assert len(result) == 1
    lines = result[0].splitlines()
    # Header (no leading digit) is preserved unchanged.
    assert lines[0] == "## File: app.py"
    # Each numbered code line has the "<digits><sep>" prefix removed.
    assert lines[1] == "def f():"
    assert lines[2] == "    return old()"
    assert lines[3] == "   return other()"


def test_remove_line_numbers_clears_pure_numeric_lines():
    tool = _make_tool()
    tool.patches_diff_list = ["42\nkeep me\n7\ntail"]

    result = tool.remove_line_numbers(tool.patches_diff_list)

    lines = result[0].splitlines()
    assert lines == ["", "keep me", "", "tail"]


def test_remove_line_numbers_preserves_blank_lines():
    tool = _make_tool()
    tool.patches_diff_list = ["1 alpha\n\n2 beta"]

    result = tool.remove_line_numbers(tool.patches_diff_list)

    lines = result[0].splitlines()
    assert lines == ["alpha", "", "beta"]


def test_remove_line_numbers_returns_original_on_exception():
    tool = _make_tool()
    # Exercise the broad ``except`` fallback by putting invalid data in the
    # instance list itself (``None`` has no ``split``), and assert the
    # parameter object is returned untouched.
    tool.patches_diff_list = [None]
    original_input = ["1 alpha"]

    result = tool.remove_line_numbers(original_input)

    assert result is original_input


# ---------------------------------------------------------------------------
# analyze_self_reflection_response (reflection-mismatch / invalid output)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_self_reflection_length_mismatch_applies_fallback_score():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = []
    tool = _make_tool(git_provider)
    settings = get_settings()
    original_publish_output = settings.config.publish_output
    settings.config.publish_output = False
    try:
        data = {"code_suggestions": [_valid_suggestion(), _valid_suggestion(one_sentence_summary="Second")]}
        # One feedback item for two suggestions is a mismatch, so neither was vetted.
        response_reflect = """
code_suggestions:
  - suggestion_score: 9
    why: only one feedback entry
"""

        await tool.analyze_self_reflection_response(data, response_reflect)

        for suggestion in data["code_suggestions"]:
            assert suggestion["score"] == 7
            assert suggestion["score_why"] == _REFLECTION_FAILURE_SCORE_WHY
    finally:
        settings.config.publish_output = original_publish_output


@pytest.mark.asyncio
async def test_analyze_self_reflection_non_mapping_applies_fallback_score():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = []
    tool = _make_tool(git_provider)
    settings = get_settings()
    original_publish_output = settings.config.publish_output
    settings.config.publish_output = False
    try:
        data = {"code_suggestions": [_valid_suggestion()]}
        # A YAML sequence parses to a list, never the expected mapping.
        response_reflect = "- suggestion_score: 9\n- why: top-level list\n"

        await tool.analyze_self_reflection_response(data, response_reflect)

        suggestion = data["code_suggestions"][0]
        assert suggestion["score"] == 7
        assert suggestion["score_why"] == _REFLECTION_FAILURE_SCORE_WHY
    finally:
        settings.config.publish_output = original_publish_output


@pytest.mark.asyncio
async def test_analyze_self_reflection_non_list_feedback_applies_fallback_score():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = []
    tool = _make_tool(git_provider)
    settings = get_settings()
    original_publish_output = settings.config.publish_output
    settings.config.publish_output = False
    try:
        data = {"code_suggestions": [_valid_suggestion()]}
        # A mapping where a list is expected cannot be indexed per suggestion.
        response_reflect = "code_suggestions:\n  suggestion_score: 9\n  why: not a list\n"

        await tool.analyze_self_reflection_response(data, response_reflect)

        suggestion = data["code_suggestions"][0]
        assert suggestion["score"] == 7
        assert suggestion["score_why"] == _REFLECTION_FAILURE_SCORE_WHY
    finally:
        settings.config.publish_output = original_publish_output


@pytest.mark.asyncio
async def test_analyze_self_reflection_invalid_feedback_assigns_default_score_seven():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = []
    tool = _make_tool(git_provider)
    settings = get_settings()
    original_publish_output = settings.config.publish_output
    settings.config.publish_output = False
    try:
        data = {"code_suggestions": [_valid_suggestion()]}
        # Missing required keys ('suggestion_score', 'why'): apply the configured
        # fallback score and explain why it was not model-assigned.
        response_reflect = """
code_suggestions:
  - irrelevant_key: 1
"""

        await tool.analyze_self_reflection_response(data, response_reflect)

        assert data["code_suggestions"][0]["score"] == 7
        assert data["code_suggestions"][0]["score_why"] == _REFLECTION_FAILURE_SCORE_WHY
    finally:
        settings.config.publish_output = original_publish_output


@pytest.mark.asyncio
async def test_analyze_self_reflection_clears_existing_code_when_equal_to_improved():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = []
    tool = _make_tool(git_provider)
    settings = get_settings()
    original_publish_output = settings.config.publish_output
    snapshot = snapshot_settings(["pr_code_suggestions.committable_code_suggestions"])
    settings.config.publish_output = False
    settings.set("pr_code_suggestions.committable_code_suggestions", False)
    try:
        data = {"code_suggestions": [_valid_suggestion(existing_code="same()", improved_code="same()")]}
        response_reflect = """
code_suggestions:
  - suggestion_score: 6
    why: equal codes
"""

        await tool.analyze_self_reflection_response(data, response_reflect)

        suggestion = data["code_suggestions"][0]
        assert suggestion["score"] == 6
        # Non-committable mode clears existing_code so the rendered suggestion
        # doesn't show an identical before/after block.
        assert suggestion["existing_code"] == ""
        assert suggestion["improved_code"] == "same()"
    finally:
        settings.config.publish_output = original_publish_output
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_analyze_self_reflection_clears_improved_code_in_committable_mode():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = []
    tool = _make_tool(git_provider)
    settings = get_settings()
    original_publish_output = settings.config.publish_output
    snapshot = snapshot_settings(["pr_code_suggestions.committable_code_suggestions"])
    settings.config.publish_output = False
    settings.set("pr_code_suggestions.committable_code_suggestions", True)
    try:
        data = {"code_suggestions": [_valid_suggestion(existing_code="same()", improved_code="same()")]}
        response_reflect = """
code_suggestions:
  - suggestion_score: 6
    why: equal codes
"""

        await tool.analyze_self_reflection_response(data, response_reflect)

        suggestion = data["code_suggestions"][0]
        # committable mode keeps existing_code (used to locate the line in PR)
        # and clears improved_code instead.
        assert suggestion["existing_code"] == "same()"
        assert suggestion["improved_code"] == ""
    finally:
        settings.config.publish_output = original_publish_output
        restore_settings(snapshot)


# ---------------------------------------------------------------------------
# _prepare_pr_code_suggestions filtering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("content", "keep_suggestion"), [
    ("Use const instead of let when not reassigning.", False),
    ("Use `const` instead of `let` when not reassigning.", False),
    ("Create the client once in the constructor instead of deleting and recreating it for every request.", True),
    ("Use the constructor instead of a let binding.", True),
    ("Cache the const binding instead of deleting it on every request.", True),
])
def test_prepare_pr_code_suggestions_filters_const_and_let_words(content, keep_suggestion):
    tool = _make_tool()
    existing_code = "const client = new Client();" if keep_suggestion else "let x = 1;"
    improved_code = "const client = this.client;" if keep_suggestion else "const x = 1;"
    prediction = f"""
code_suggestions:
  - one_sentence_summary: First suggestion
    label: best practice
    relevant_file: app.js
    suggestion_content: {content}
    existing_code: {existing_code}
    improved_code: {improved_code}
  - one_sentence_summary: Keep this one
    label: maintainability
    relevant_file: app.js
    suggestion_content: Extract helper.
    existing_code: a()
    improved_code: helper()
"""

    data = tool._prepare_pr_code_suggestions(prediction)

    summaries = [s["one_sentence_summary"] for s in data["code_suggestions"]]
    expected = ["First suggestion", "Keep this one"] if keep_suggestion else ["Keep this one"]
    assert summaries == expected


def test_prepare_pr_code_suggestions_skips_suggestion_missing_improved_code():
    tool = _make_tool()
    prediction = """
code_suggestions:
  - one_sentence_summary: Missing improved_code
    label: maintainability
    relevant_file: app.py
    suggestion_content: Refactor.
    existing_code: a()
  - one_sentence_summary: Complete
    label: maintainability
    relevant_file: app.py
    suggestion_content: Refactor.
    existing_code: a()
    improved_code: b()
"""

    data = tool._prepare_pr_code_suggestions(prediction)

    assert len(data["code_suggestions"]) == 1
    assert data["code_suggestions"][0]["one_sentence_summary"] == "Complete"


def test_prepare_pr_code_suggestions_accepts_list_payload():
    tool = _make_tool()
    # Some prompt variants return a bare list rather than a mapping.
    prediction = """
- one_sentence_summary: Only suggestion
  label: maintainability
  relevant_file: app.py
  suggestion_content: Refactor.
  existing_code: a()
  improved_code: b()
"""

    data = tool._prepare_pr_code_suggestions(prediction)

    assert isinstance(data, dict)
    assert len(data["code_suggestions"]) == 1
    assert data["code_suggestions"][0]["improved_code"] == "b()"


def test_prepare_pr_code_suggestions_truncates_long_improved_code():
    settings = get_settings()
    snapshot = snapshot_settings(TRUNCATION_SETTINGS)
    settings.set("pr_code_suggestions.max_code_suggestion_length", 8)
    settings.set("pr_code_suggestions.suggestion_truncation_message", "[cut]")
    tool = _make_tool()
    prediction = """
code_suggestions:
  - one_sentence_summary: Long
    label: maintainability
    relevant_file: app.py
    suggestion_content: Refactor.
    existing_code: a()
    improved_code: ABCDEFGHIJKLMNOP
"""
    try:
        data = tool._prepare_pr_code_suggestions(prediction)
        improved = data["code_suggestions"][0]["improved_code"]
        assert improved.startswith("ABCDEFGH")
        assert "[cut]" in improved
    finally:
        restore_settings(snapshot)
