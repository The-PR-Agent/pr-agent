from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.comment_identity import (
    PRCodeSuggestionsIdentity,
    add_comment_identity,
    comment_matches_identity,
    format_pr_code_suggestions_header,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


@pytest.fixture
def restore_suggestions_heading():
    snapshot = snapshot_settings(["pr_code_suggestions.suggestions_heading"])
    yield
    restore_settings(snapshot)


def test_custom_suggestions_heading_changes_presentation_only(restore_suggestions_heading):
    get_settings().set(
        "pr_code_suggestions.suggestions_heading",
        "Guideline Improvement Suggestions",
    )

    header = format_pr_code_suggestions_header()

    assert header == "## Guideline Improvement Suggestions ✨"
    assert "<!-- pr-agent:improve" not in header


def test_suggestions_heading_is_trimmed(restore_suggestions_heading):
    get_settings().set("pr_code_suggestions.suggestions_heading", "  Team Suggestions  ")

    header = format_pr_code_suggestions_header()

    assert header == "## Team Suggestions ✨"


def test_default_suggestions_heading_is_unchanged():
    assert format_pr_code_suggestions_header() == "## PR Code Suggestions ✨"


@pytest.mark.parametrize(
    "invalid_heading",
    [None, "", "  ", "first\nsecond", "first\rsecond", "first\u2028second", 42],
)
def test_invalid_suggestions_heading_falls_back_to_default(invalid_heading, restore_suggestions_heading):
    get_settings().set("pr_code_suggestions.suggestions_heading", invalid_heading)
    header = format_pr_code_suggestions_header()

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


def _persistent_publish(prev_comment_bodies):
    provider = MagicMock()
    comments = [SimpleNamespace(body=body) for body in prev_comment_bodies]
    provider.get_issue_comments.return_value = comments
    provider.get_issue_comments_newest_first.return_value = list(reversed(comments))
    GitProvider.publish_persistent_comment_full(
        provider,
        "## PR Code Suggestions ✨\n\n<table>new</table>",
        initial_header="## PR Code Suggestions ✨",
        update_header=False,
        name="suggestions",
        final_update_message=False,
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header="## PR Code Suggestions ✨",
    )
    return provider


@pytest.mark.parametrize(
    "other_identity",
    [PRCodeSuggestionsIdentity.UNANCHORED.value, PRCodeSuggestionsIdentity.NO_SUGGESTIONS.value],
)
def test_persistent_summary_never_overwrites_another_suggestions_comment(other_identity):
    provider = _persistent_publish([f"## PR Code Suggestions ✨\n\n{other_identity}\n\nfallback body"])

    provider.edit_comment.assert_not_called()
    provider.publish_comment.assert_called_once()
    assert PRCodeSuggestionsIdentity.SUMMARY.value in provider.publish_comment.call_args.args[0]


def test_persistent_summary_still_migrates_an_unmarked_legacy_comment():
    provider = _persistent_publish(["## PR Code Suggestions ✨\n\n<table>old</table>"])

    provider.edit_comment.assert_called_once()
    provider.publish_comment.assert_not_called()


def test_persistent_summary_updates_the_marked_summary_comment():
    provider = _persistent_publish([
        f"## PR Code Suggestions ✨\n\n{PRCodeSuggestionsIdentity.UNANCHORED.value}\n\nfallback body",
        f"## PR Code Suggestions ✨\n\n{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n<table>old</table>",
    ])

    provider.edit_comment.assert_called_once()
    assert provider.edit_comment.call_args.args[0].body.startswith(
        f"## PR Code Suggestions ✨\n\n{PRCodeSuggestionsIdentity.SUMMARY.value}"
    )
    provider.publish_comment.assert_not_called()
