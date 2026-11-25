from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.comment_identity import (
    PRCodeSuggestionsIdentity,
    PRReviewIdentity,
    add_comment_identity,
    comment_carries_other_identity,
    comment_matches_identity,
    escape_hidden_markers,
    hidden_marker_forms,
    render_hidden_marker,
)
from pr_agent.commands.pr_help_docs import format_markdown_q_and_a_response
from pr_agent.commands.pr_questions import PRQuestions
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.git_provider import GitProvider


@pytest.mark.parametrize("identity", list(PRReviewIdentity) + list(PRCodeSuggestionsIdentity))
def test_marker_forms_are_interchangeable_but_bounded(identity):
    html = identity.value
    reference = f"[{html[5:-4]}]: https://github.com/The-PR-Agent/pr-agent"
    assert hidden_marker_forms(html) == (html, reference)
    provider = BitbucketProvider.__new__(BitbucketProvider)
    assert render_hidden_marker(html, provider) == reference
    assert render_hidden_marker(reference) == html
    for stored in (html, reference):
        body = f"## Review\n\n{stored}\n\nbody"
        for query in (html, reference):
            assert comment_matches_identity(body, query)
            assert not comment_matches_identity(f"## Review\n\n> {stored}", query)
            assert not comment_matches_identity(f"a\nb\nc\nd\ne\n{stored}", query)
            assert not comment_carries_other_identity(body, query)
            assert add_comment_identity(body, query, provider) == body
    assert add_comment_identity("## Review\n\nbody", html, provider) == f"## Review\n\n{reference}\n\nbody"


@pytest.mark.parametrize("stored", hidden_marker_forms(PRReviewIdentity.REGULAR.value))
def test_bitbucket_rerun_updates_both_marker_forms_in_place(stored):
    provider = BitbucketProvider.__new__(BitbucketProvider)
    comment = SimpleNamespace(body=f"## Review\n\n{stored}\n\nold")
    provider.get_issue_comments = MagicMock(return_value=[comment])
    provider.get_issue_comments_newest_first = MagicMock(return_value=[comment])
    provider.edit_comment = MagicMock(return_value=True)
    provider.publish_comment = MagicMock()
    provider.get_latest_commit_url = MagicMock(return_value="https://example.com/commit/123")
    provider.get_comment_url = MagicMock(return_value="https://example.com/comment/1")
    result = GitProvider.publish_persistent_comment_full(
        provider, "## Review\n\nnew", initial_header="## Review",
        identity_marker=PRReviewIdentity.REGULAR.value, final_update_message=False,
    )
    assert result is comment
    provider.publish_comment.assert_not_called()
    body = provider.edit_comment.call_args.args[1]
    assert "<!-- pr-agent:" not in body
    assert render_hidden_marker(PRReviewIdentity.REGULAR.value, provider) in body
    assert "Review updated until commit https://example.com/commit/123" in body


def test_other_reference_identity_is_not_a_legacy_review():
    body = add_comment_identity("## Review\n\nbody", PRReviewIdentity.INCREMENTAL.value,
                                BitbucketProvider.__new__(BitbucketProvider))
    assert comment_carries_other_identity(body, PRReviewIdentity.REGULAR.value)


@pytest.mark.parametrize("identity", list(PRReviewIdentity))
def test_standalone_review_strips_both_forms(identity):
    for marker in hidden_marker_forms(identity.value):
        result = PRReviewer._as_non_authoritative_review(f"## Review\n\n{marker}\n\nbody")
        assert marker not in result
        assert "body" in result


def test_bitbucket_character_budget_reserves_rendered_marker():
    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.max_comment_length = 1000
    provider.get_latest_commit_url = MagicMock(return_value="commit")
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    suffix = "\n\n#### (Review updated until commit commit)\n"
    marker = render_hidden_marker(PRReviewIdentity.REGULAR.value, provider)
    assert reviewer._review_comment_max_chars() == 1000 - len(suffix) - len(marker) - 2


@pytest.mark.parametrize("stored", hidden_marker_forms(PRReviewIdentity.REGULAR.value))
def test_escape_hidden_markers_removes_identity_and_keeps_other_text(stored):
    text = f"why?\r\n  {stored} \r\nkeep {stored} inline\n{stored}"
    escaped = escape_hidden_markers(text)
    assert escaped == f"why?\r\n  `{stored}` \r\nkeep {stored} inline\n`{stored}`"
    assert not comment_matches_identity(f"## Heading\n{escaped}", PRReviewIdentity.REGULAR.value)
    assert escape_hidden_markers("plain\n<!-- note -->\n") == "plain\n<!-- note -->\n"


@pytest.mark.parametrize("stored", hidden_marker_forms(PRReviewIdentity.REGULAR.value))
def test_echoed_questions_carry_no_identity(stored):
    question = PRQuestions.__new__(PRQuestions)
    question.git_provider = MagicMock()
    question.question_str = f"what?\n{stored}"
    question.prediction = f"answer\n{stored}"
    ask_comment = question._prepare_pr_answer()
    help_comment = format_markdown_q_and_a_response(f"what?\n{stored}", f"answer\n{stored}", [], [".md"], "")
    for comment in (ask_comment, help_comment):
        assert f"`{stored}`" in comment
        assert not comment_matches_identity(comment, PRReviewIdentity.REGULAR.value)
