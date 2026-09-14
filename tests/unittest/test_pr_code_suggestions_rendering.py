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


# A hunk whose new-file side covers lines 1-6 of the file.
DEFAULT_PATCH = (
    "@@ -1,5 +1,6 @@\n"
    " def f():\n"
    "-    return old()\n"
    "+    return new()\n"
    "     extra\n"
    "     more\n"
    "     lines\n"
    "+    added line\n"
)


def _diff_file(filename="app.py", patch=DEFAULT_PATCH):
    return FilePatchInfo(
        base_file="def f():\n    return old()\n    extra\n    more\n    lines\n",
        head_file="def f():\n    return new()\n    extra\n    more\n    lines\n    added line\n",
        patch=patch,
        filename=filename,
    )


def _provider_with_diff_files(*filenames):
    git_provider = MagicMock()
    git_provider.diff_files = [_diff_file(filename) for filename in filenames]
    git_provider.get_line_link.return_value = ""
    return git_provider


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


@pytest.mark.parametrize(
    "suggestion_kwargs",
    [
        {"relevant_lines_start": None, "relevant_lines_end": 5},
        {"relevant_lines_start": -1, "relevant_lines_end": -1},
        {"relevant_lines_start": 0, "relevant_lines_end": 0},
        {"relevant_lines_start": -3, "relevant_lines_end": 1},
        {"relevant_lines_start": 5, "relevant_lines_end": 2},
        {"relevant_lines_start": "10", "relevant_lines_end": "bad"},
        {"relevant_lines_start": float("inf"), "relevant_lines_end": 5},
        {"relevant_lines_start": float("-inf"), "relevant_lines_end": 5},
        {"relevant_lines_start": 2, "relevant_lines_end": float("inf")},
        {"relevant_lines_start": float("nan"), "relevant_lines_end": 5},
        {"relevant_lines_start": 2, "relevant_lines_end": float("nan")},
        {"relevant_lines_start": 1.5, "relevant_lines_end": 5},
        {"relevant_lines_start": 2, "relevant_lines_end": 3.5},
        {"relevant_lines_start": True, "relevant_lines_end": True},
    ],
)
def test_is_suggestion_line_range_valid_rejects_unanchorable_ranges(suggestion_kwargs):
    tool = _make_tool()
    bad = _suggestion(**suggestion_kwargs)

    assert tool._is_suggestion_line_range_valid(bad) is False


def test_is_suggestion_line_range_valid_normalizes_valid_range():
    tool = _make_tool(_provider_with_diff_files("app.py"))
    good = _suggestion(relevant_lines_start="2", relevant_lines_end="4")

    assert tool._is_suggestion_line_range_valid(good) is True
    assert good["relevant_lines_start"] == 2
    assert good["relevant_lines_end"] == 4


def test_is_suggestion_line_range_valid_rejects_missing_keys():
    tool = _make_tool()
    suggestion = _suggestion()
    suggestion.pop("relevant_lines_start")
    suggestion.pop("relevant_lines_end")

    assert tool._is_suggestion_line_range_valid(suggestion) is False


def test_is_suggestion_line_range_valid_rejects_file_not_in_diff():
    tool = _make_tool(_provider_with_diff_files("app.py"))
    suggestion = _suggestion(relevant_file="other.py")

    assert tool._is_suggestion_line_range_valid(suggestion) is False


def test_is_suggestion_line_range_valid_rejects_range_outside_diff():
    tool = _make_tool(_provider_with_diff_files("app.py"))
    suggestion = _suggestion(relevant_lines_start=100, relevant_lines_end=100)

    assert tool._is_suggestion_line_range_valid(suggestion) is False


def test_is_suggestion_line_range_valid_follows_the_head_file_when_available():
    # Extended context outside the raw hunk is anchorable within the head file.
    diff_file = _diff_file()
    git_provider = MagicMock()
    git_provider.diff_files = [diff_file]
    tool = _make_tool(git_provider)
    suggestion = _suggestion(relevant_lines_start=5, relevant_lines_end=9)

    assert tool._is_suggestion_line_range_valid(suggestion) is False

    diff_file.head_file += "    trailing\n    context\n    lines\n"

    assert tool._is_suggestion_line_range_valid(suggestion) is True


@pytest.mark.parametrize("head_file", ["", "context\n" * 9])
def test_is_suggestion_line_range_valid_uses_hunk_without_complete_head_file(head_file):
    diff_file = _diff_file()
    diff_file.head_file = head_file
    diff_file.head_file_is_complete = False
    git_provider = MagicMock()
    git_provider.diff_files = [diff_file]
    tool = _make_tool(git_provider)

    assert tool._is_suggestion_line_range_valid(
        _suggestion(relevant_lines_start=2, relevant_lines_end=6)) is True
    assert tool._is_suggestion_line_range_valid(
        _suggestion(relevant_lines_start=5, relevant_lines_end=9)) is False


def test_get_patch_range_lines_rejects_oversized_span_without_enumerating_it():
    # A parseable but absurdly large span must be rejected by length comparison
    # before any range(start, end) enumeration would iterate over it.
    patch = DEFAULT_PATCH

    assert PRCodeSuggestions._get_patch_range_lines(patch, 1, 1_000_000_000) is None
    assert PRCodeSuggestions._get_patch_range_lines(patch, 2, 6) == [
        "    return new()",
        "    extra",
        "    more",
        "    lines",
        "    added line",
    ]
    assert PRCodeSuggestions._get_patch_range_lines(patch, 2, 2) == ["    return new()"]


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
async def test_push_inline_publishes_partial_coverage_notice():
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
    git_provider.supports_code_suggestions_artifact.return_value = False
    tool = _make_tool(git_provider)
    tool.failed_chunk_count = 1
    tool.total_chunk_count = 2

    await tool.push_inline_code_suggestions({"code_suggestions": [_suggestion()]})

    git_provider.publish_code_suggestions.assert_called_once()
    coverage_comment = git_provider.publish_comment.call_args.args[0]
    assert "1 of 2 analysis chunks failed" in coverage_comment
    assert "successful chunks only" in coverage_comment


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

    result = await tool.push_inline_code_suggestions({"code_suggestions": []})

    assert result is None
    git_provider.publish_comment.assert_called_once_with(
        "No suggestions found to improve this PR."
    )
    git_provider.publish_code_suggestions.assert_not_called()


@pytest.mark.asyncio
async def test_push_inline_qualifies_empty_partial_results():
    git_provider = MagicMock()
    tool = _make_tool(git_provider)
    tool.failed_chunk_count = 1
    tool.total_chunk_count = 3

    await tool.push_inline_code_suggestions({"code_suggestions": []})

    body = git_provider.publish_comment.call_args.args[0]
    assert "successfully analyzed chunks" in body
    assert "1 of 3 analysis chunks failed" in body
    assert "could not be analyzed" in body


# ---------------------------------------------------------------------------
# publish_no_suggestions
# ---------------------------------------------------------------------------

NO_SUGGESTIONS_SETTINGS = (
    "config.publish_output",
    "pr_code_suggestions.publish_output_no_suggestions",
    "config.output_run_details",
)


@pytest.mark.asyncio
async def test_publish_no_suggestions_resolves_thread_instead_of_replacing_it():
    # The progress comment was published as a resolvable thread; the no-suggestions
    # status isn't actionable, so the thread should be resolved rather than dropped.
    snapshot = snapshot_settings(NO_SUGGESTIONS_SETTINGS)
    try:
        get_settings().set("config.publish_output", True)
        get_settings().pr_code_suggestions.publish_output_no_suggestions = True
        get_settings().set("config.output_run_details", False)

        git_provider = MagicMock()
        git_provider.should_publish_improve_as_thread.return_value = True
        progress_comment = MagicMock(id=7)
        tool = _make_tool(git_provider)
        tool.progress_response = progress_comment

        await tool.publish_no_suggestions()

        git_provider.edit_comment.assert_called_once()
        assert git_provider.edit_comment.call_args.args[0] is progress_comment
        git_provider.remove_comment.assert_not_called()
        git_provider.resolve_comment_thread.assert_called_once_with(7)
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_publish_no_suggestions_does_not_resolve_when_not_threaded():
    snapshot = snapshot_settings(NO_SUGGESTIONS_SETTINGS)
    try:
        get_settings().set("config.publish_output", True)
        get_settings().pr_code_suggestions.publish_output_no_suggestions = True
        get_settings().set("config.output_run_details", False)

        git_provider = MagicMock()
        git_provider.should_publish_improve_as_thread.return_value = False
        tool = _make_tool(git_provider)
        tool.progress_response = MagicMock(id=7)

        await tool.publish_no_suggestions()

        git_provider.edit_comment.assert_called_once()
        git_provider.resolve_comment_thread.assert_not_called()
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_publish_no_suggestions_resolves_freshly_published_thread():
    # No progress comment was published (publish_output_progress is off); the
    # no-suggestions note is published directly as a thread and must be resolved too.
    snapshot = snapshot_settings(NO_SUGGESTIONS_SETTINGS)
    try:
        get_settings().set("config.publish_output", True)
        get_settings().pr_code_suggestions.publish_output_no_suggestions = True
        get_settings().set("config.output_run_details", False)

        git_provider = MagicMock()
        git_provider.should_publish_improve_as_thread.return_value = True
        git_provider.publish_comment.return_value = MagicMock(id=11)
        tool = _make_tool(git_provider)
        tool.progress_response = None

        await tool.publish_no_suggestions()

        git_provider.publish_comment.assert_called_once()
        assert git_provider.publish_comment.call_args.kwargs.get("as_thread") is True
        git_provider.resolve_comment_thread.assert_called_once_with(11)
    finally:
        restore_settings(snapshot)


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
    git_provider = _provider_with_diff_files("app.py", "auth.py")
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
    git_provider = _provider_with_diff_files("app.py")
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
    git_provider = _provider_with_diff_files("app.py")
    tool = _make_tool(git_provider)
    suggestion = _suggestion(one_sentence_summary="Replace '<old_name>' with new_name")
    out = tool.generate_summarized_suggestions({"code_suggestions": [suggestion]})
    # The "'<...>'" pattern is rewritten with backticks, which replace_code_tags
    # then turns into an HTML <code> span with escaped angle brackets so it isn't
    # parsed as an HTML tag.
    assert "'<old_name>'" not in out
    assert "<code>&lt;old_name&gt;</code>" in out


def test_generate_summarized_suggestions_includes_score_why_block_when_present():
    git_provider = _provider_with_diff_files("app.py")
    tool = _make_tool(git_provider)
    suggestion = _suggestion(score_why="Catches a real bug.")
    out = tool.generate_summarized_suggestions({"code_suggestions": [suggestion]})
    assert "Suggestion importance[1-10]: 7" in out
    assert "Why: Catches a real bug." in out


def test_generate_summarized_suggestions_skips_anchorless_but_keeps_rest():
    """A suggestion without resolved line anchors is skipped instead of failing the whole table."""
    git_provider = _provider_with_diff_files("app.py")
    tool = _make_tool(git_provider)
    anchored = _suggestion(one_sentence_summary="Keep me")
    anchorless = _suggestion(one_sentence_summary="Drop me", relevant_file="other.py")
    anchorless.pop("relevant_lines_start")
    anchorless.pop("relevant_lines_end")

    out = tool.generate_summarized_suggestions({"code_suggestions": [anchorless, anchored]})

    assert "<table>" in out
    assert "Keep me" in out
    assert "Drop me" not in out


def test_generate_summarized_suggestions_skips_positive_range_outside_diff():
    """A positive, ordered range outside the file's diff hunks is dropped per-suggestion."""
    git_provider = _provider_with_diff_files("app.py")
    tool = _make_tool(git_provider)
    in_diff = _suggestion(one_sentence_summary="In diff")
    out_diff = _suggestion(
        one_sentence_summary="Hallucinated range",
        relevant_lines_start=100,
        relevant_lines_end=100,
    )

    out = tool.generate_summarized_suggestions({"code_suggestions": [in_diff, out_diff]})

    assert "<table>" in out
    assert "In diff" in out
    assert "Hallucinated range" not in out


def test_generate_summarized_suggestions_all_anchorless_returns_placeholder():
    """When no suggestion has resolvable anchors, publish a truthful message instead of an empty table."""
    git_provider = MagicMock()
    git_provider.get_line_link.return_value = ""
    tool = _make_tool(git_provider)
    suggestion = _suggestion()
    suggestion.pop("relevant_lines_start")
    suggestion.pop("relevant_lines_end")

    out = tool.generate_summarized_suggestions({"code_suggestions": [suggestion]})

    assert "No suggestions found to improve this PR." in out
    assert "<table>" not in out


@pytest.mark.parametrize(
    "start,end",
    [
        (-1, -1),  # unresolved -1 sentinel written by self-reflection
        (0, 0),
        (-3, 1),
        (5, 2),  # reversed range
    ],
)
def test_generate_summarized_suggestions_skips_invalid_line_ranges(start, end):
    """Unresolved sentinels, non-positive lines and reversed ranges are omitted per-suggestion."""
    git_provider = _provider_with_diff_files("app.py")
    tool = _make_tool(git_provider)
    bad = _suggestion(one_sentence_summary="Bad range", relevant_lines_start=start, relevant_lines_end=end)
    good = _suggestion(one_sentence_summary="Good range")

    out = tool.generate_summarized_suggestions({"code_suggestions": [bad, good]})

    assert "<table>" in out
    assert "Good range" in out
    assert "Bad range" not in out


@pytest.mark.parametrize("start,end", [(-1, -1), (0, 0), (5, 2)])
def test_generate_summarized_suggestions_all_invalid_ranges_returns_placeholder(start, end):
    git_provider = MagicMock()
    git_provider.get_line_link.return_value = ""
    tool = _make_tool(git_provider)
    bad = _suggestion(relevant_lines_start=start, relevant_lines_end=end)

    out = tool.generate_summarized_suggestions({"code_suggestions": [bad]})

    assert "No suggestions found to improve this PR." in out
    assert "<table>" not in out


@pytest.mark.asyncio
async def test_analyze_self_reflection_mismatched_count_does_not_crash():
    """Feedback covering a different suggestion count than generated leaves anchors unresolved."""
    tool = _make_tool()
    suggestion = _suggestion()
    suggestion.pop("relevant_lines_start")
    suggestion.pop("relevant_lines_end")
    data = {"code_suggestions": [suggestion, _suggestion(one_sentence_summary="Second")]}

    await tool.analyze_self_reflection_response(
        data, "code_suggestions:\n- suggestion_score: 8\n  why: fast\n"
    )

    assert "relevant_lines_start" not in data["code_suggestions"][0]
    assert data["code_suggestions"][1]["relevant_lines_start"] == 2


@pytest.mark.asyncio
async def test_analyze_self_reflection_null_feedback_does_not_crash():
    """A non-null response with `code_suggestions: null` must not abort suggestions."""
    tool = _make_tool()
    suggestion = _suggestion()
    suggestion.pop("relevant_lines_start")
    suggestion.pop("relevant_lines_end")
    data = {"code_suggestions": [suggestion]}

    await tool.analyze_self_reflection_response(data, "code_suggestions: null")

    assert "relevant_lines_start" not in data["code_suggestions"][0]


@pytest.mark.asyncio
async def test_analyze_self_reflection_non_mapping_feedback_does_not_crash():
    """A top-level list (not a mapping) from reflection must not abort suggestions."""
    tool = _make_tool()
    suggestion = _suggestion()
    suggestion.pop("relevant_lines_start")
    suggestion.pop("relevant_lines_end")
    data = {"code_suggestions": [suggestion]}

    await tool.analyze_self_reflection_response(data, "- one\n- two")

    assert "relevant_lines_start" not in data["code_suggestions"][0]


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
