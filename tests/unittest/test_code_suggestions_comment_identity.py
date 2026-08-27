from unittest.mock import MagicMock

import pytest

from pr_agent.algo.utils import (PRCodeSuggestionsIdentity,
                                 add_comment_identity,
                                 comment_matches_identity,
                                 format_pr_code_suggestions_header)
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from tests.unittest._settings_helpers import (restore_settings,
                                              snapshot_settings)


def test_custom_suggestions_heading_changes_presentation_only():
    snapshot = snapshot_settings(["pr_code_suggestions.suggestions_heading"])
    try:
        get_settings().set(
            "pr_code_suggestions.suggestions_heading",
            "Guideline Improvement Suggestions",
        )

        header = format_pr_code_suggestions_header()
    finally:
        restore_settings(snapshot)

    assert header == "## Guideline Improvement Suggestions ✨"
    assert "<!-- pr-agent:improve" not in header


def test_suggestions_heading_is_trimmed():
    snapshot = snapshot_settings(["pr_code_suggestions.suggestions_heading"])
    try:
        get_settings().set("pr_code_suggestions.suggestions_heading", "  Team Suggestions  ")

        header = format_pr_code_suggestions_header()
    finally:
        restore_settings(snapshot)

    assert header == "## Team Suggestions ✨"


def test_default_suggestions_heading_is_unchanged():
    assert format_pr_code_suggestions_header() == "## PR Code Suggestions ✨"


@pytest.mark.parametrize(
    "invalid_heading",
    [None, "", "  ", "first\nsecond", "first\rsecond", "first\u2028second", 42],
)
def test_invalid_suggestions_heading_falls_back_to_default(invalid_heading):
    snapshot = snapshot_settings(["pr_code_suggestions.suggestions_heading"])
    try:
        get_settings().set("pr_code_suggestions.suggestions_heading", invalid_heading)
        header = format_pr_code_suggestions_header()
    finally:
        restore_settings(snapshot)

    assert header == "## PR Code Suggestions ✨"


def test_suggestions_identity_is_inserted_after_heading_and_is_idempotent():
    comment = "## Team Suggestions ✨\n\n<table>suggestions</table>"
    marker = PRCodeSuggestionsIdentity.SUMMARY.value

    marked = add_comment_identity(comment, marker)

    assert marked.startswith(
        "## Team Suggestions ✨\n\n"
        "<!-- pr-agent:improve:summary -->\n\n"
    )
    assert add_comment_identity(marked, marker) == marked


def test_suggestions_identity_is_bounded_and_does_not_match_quoted_marker():
    marker = PRCodeSuggestionsIdentity.SUMMARY.value
    valid = f"## Team Suggestions ✨\n\n{marker}\n\nbody"
    quoted = f"## Human comment\n\n> {marker}\n\nbody"
    late = "## Human comment\n\na\nb\nc\nd\n" + marker

    assert comment_matches_identity(valid, marker)
    assert not comment_matches_identity(quoted, marker)
    assert not comment_matches_identity(late, marker)


def test_summarized_artifact_uses_custom_heading_without_identity():
    snapshot = snapshot_settings(["pr_code_suggestions.suggestions_heading"])
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    try:
        get_settings().set("pr_code_suggestions.suggestions_heading", "Team Suggestions")

        artifact = tool.generate_summarized_suggestions({"code_suggestions": []})
    finally:
        restore_settings(snapshot)

    assert artifact.startswith("## Team Suggestions ✨\n\n")
    assert "<!-- pr-agent:improve" not in artifact


@pytest.mark.asyncio
async def test_no_suggestions_uses_custom_heading_with_separate_result_identity():
    snapshot = snapshot_settings(
        [
            "config.publish_output",
            "pr_code_suggestions.publish_output_no_suggestions",
            "pr_code_suggestions.suggestions_heading",
        ]
    )
    provider = MagicMock()
    provider.supports_code_suggestions_artifact.return_value = False
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = provider
    tool.progress_response = None
    try:
        get_settings().set("config.publish_output", True)
        get_settings().set("pr_code_suggestions.publish_output_no_suggestions", True)
        get_settings().set("pr_code_suggestions.suggestions_heading", "Team Suggestions")

        await tool.publish_no_suggestions()
    finally:
        restore_settings(snapshot)

    published = provider.publish_comment.call_args.args[0]
    assert published.startswith(
        "## Team Suggestions ✨\n\n"
        f"{PRCodeSuggestionsIdentity.NO_SUGGESTIONS.value}\n\n"
    )
    assert PRCodeSuggestionsIdentity.SUMMARY.value not in published


def test_github_check_run_receives_configured_presentation_without_identity():
    snapshot = snapshot_settings(["github.publish_as_check_run"])
    provider = MagicMock()
    provider._publish_check_run.return_value = True
    comment = "## Team Suggestions ✨\n\n<table>suggestions</table>"
    try:
        get_settings().set("github.publish_as_check_run", True)

        PRCodeSuggestions.publish_persistent_comment_with_history(
            provider,
            comment,
            initial_header="## Team Suggestions ✨",
            name="suggestions",
            identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
            legacy_initial_header="## PR Code Suggestions ✨",
        )
    finally:
        restore_settings(snapshot)

    provider._publish_check_run.assert_called_once_with(comment, "suggestions")
    provider.get_issue_comments.assert_not_called()
