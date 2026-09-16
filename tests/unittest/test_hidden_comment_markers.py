from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.utils import (
    PRCodeSuggestionsIdentity,
    PRReviewIdentity,
    add_comment_identity,
    comment_carries_other_identity,
    comment_matches_identity,
    hidden_marker_forms,
    render_hidden_marker,
)
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.tools.pr_reviewer import PRReviewer


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


@pytest.mark.parametrize("stored", hidden_marker_forms(PRCodeSuggestionsIdentity.SUMMARY.value))
def test_suggestions_history_strips_both_forms_and_updates_existing_comment(stored):
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    provider = BitbucketProvider.__new__(BitbucketProvider)
    comment = SimpleNamespace(body=f"## Suggestions\n\n{stored}\n\nold")
    provider.get_issue_comments_newest_first = MagicMock(return_value=[comment])
    provider.edit_comment = MagicMock(return_value=True)
    provider.publish_comment = MagicMock()
    provider.get_latest_commit_url = MagicMock(return_value="https://example.com/commit/abc1234")
    provider.get_comment_url = MagicMock(return_value="https://example.com/comment/1")
    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider, f"## Suggestions\n\n{stored}\n\nnew", initial_header="## Suggestions",
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value, max_previous_comments=1,
    )
    assert result is comment
    body = provider.edit_comment.call_args.args[1]
    marker = render_hidden_marker(PRCodeSuggestionsIdentity.SUMMARY.value, provider)
    assert body.count(marker) == 1
    assert PRCodeSuggestionsIdentity.SUMMARY.value not in body
    assert "new" in body
    provider.publish_comment.assert_not_called()
