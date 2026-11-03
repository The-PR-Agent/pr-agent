from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.comment_identity import (
    PRCodeSuggestionsHeader,
    PRCodeSuggestionsIdentity,
    comment_authorship_available,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

HEADER = PRCodeSuggestionsHeader.SUMMARY.value
MARKER = PRCodeSuggestionsIdentity.SUMMARY.value


@pytest.fixture(params=[
    pytest.param((GithubProvider, 4), id="github-history"),
    pytest.param((GitLabProvider, 4), id="gitlab-history"),
    pytest.param((GitLabProvider, 0), id="gitlab-no-history"),
    pytest.param((AzureDevopsProvider, 4), id="azure-history"),
    pytest.param((AzureDevopsProvider, 0), id="azure-no-history"),
])
def publisher(request):
    snapshot = snapshot_settings(["github.publish_as_check_run", "azure_devops_server.agent_identity"])
    get_settings().set("github.publish_as_check_run", False)
    get_settings().set("azure_devops_server.agent_identity", "bot@example.test")
    provider_class, history_length = request.param
    provider = provider_class.__new__(provider_class)
    # Exercise the providers' real capability and authorship checks without API calls.
    provider.deployment_type = "user"
    provider.get_user_id = MagicMock(return_value="pr-agent-bot")
    provider._own_user_id = 42
    provider.get_issue_comments_newest_first = MagicMock(return_value=[])
    provider.get_latest_commit_url = MagicMock(return_value="https://example.test/commit/bbb2222")
    provider.get_comment_url = MagicMock(return_value="https://example.test/comment/1")
    provider.edit_comment = MagicMock(return_value=None)
    provider.publish_comment = MagicMock(return_value=SimpleNamespace(body="published"))
    try:
        yield provider, history_length
    finally:
        restore_settings(snapshot)


def _comment(*, own, marked=True, as_mapping=False):
    body = f"{HEADER}\n\n{MARKER if marked else ''}\n\n<!-- aaa1111 -->\n\n<table>old suggestions</table>"
    author = {
        "login": "pr-agent-bot" if own else "someone-else",
        "id": 42 if own else 99,
        "unique_name": "bot@example.test" if own else "someone@example.test",
    }
    if as_mapping:
        author["uniqueName"] = author.pop("unique_name")
        return {"body": body, "author": author}
    return SimpleNamespace(body=body, author=SimpleNamespace(**author))


def _publish(publisher, comments, *, identity_marker=MARKER):
    provider, history_length = publisher
    provider.get_issue_comments_newest_first.return_value = list(reversed(comments))
    return PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{HEADER}\n\n<table>new suggestions</table>",
        initial_header=HEADER,
        update_header=False,
        name="suggestions",
        final_update_message=False,
        max_previous_comments=history_length,
        identity_marker=identity_marker,
        legacy_initial_header=HEADER,
    )


@pytest.mark.parametrize("match_kind", ["marker", "legacy", "heading"])
def test_persistent_suggestions_skip_a_newer_comment_by_another_author(publisher, match_kind):
    provider, history_length = publisher
    own = _comment(own=True, marked=match_kind == "marker")
    foreign = _comment(own=False, marked=match_kind != "heading")
    identity_marker = None if match_kind == "heading" else MARKER

    result = _publish(publisher, [own, foreign], identity_marker=identity_marker)

    assert result is own
    provider.edit_comment.assert_called_once()
    assert provider.edit_comment.call_args.args[0] is own
    updated_body = provider.edit_comment.call_args.args[1]
    assert "<table>new suggestions</table>" in updated_body
    if history_length:
        assert "<table>old suggestions</table>" in updated_body
    if identity_marker:
        assert MARKER in updated_body
    provider.publish_comment.assert_not_called()


def test_persistent_suggestions_check_authorship_of_mapping_comments(publisher):
    provider, _ = publisher
    own = _comment(own=True, as_mapping=True)
    foreign = _comment(own=False, as_mapping=True)

    assert _publish(publisher, [own, foreign]) is own

    assert provider.edit_comment.call_args.args[0] is own
    provider.publish_comment.assert_not_called()


def test_persistent_suggestions_ignore_unrelated_comments_without_author_metadata(publisher):
    provider, _ = publisher
    own = _comment(own=True)
    unrelated = SimpleNamespace(body="An unrelated discussion")

    assert _publish(publisher, [own, unrelated]) is own

    assert provider.edit_comment.call_args.args[0] is own
    provider.publish_comment.assert_not_called()


def test_persistent_suggestions_create_a_comment_when_only_another_author_matches(publisher):
    provider, _ = publisher

    result = _publish(publisher, [_comment(own=False)])

    assert result is provider.publish_comment.return_value
    provider.edit_comment.assert_not_called()
    provider.publish_comment.assert_called_once()
    assert MARKER in provider.publish_comment.call_args.args[0]
    assert "<table>new suggestions</table>" in provider.publish_comment.call_args.args[0]


@pytest.mark.parametrize("unavailable", ["unsupported", "no-comment-support", "raises"])
def test_persistent_suggestions_keep_matching_when_authorship_is_unavailable(publisher, unavailable):
    provider, _ = publisher
    if unavailable == "unsupported":
        provider.supports_review_finding_state = MagicMock(return_value=False)
    elif unavailable == "no-comment-support":
        provider.is_supported = MagicMock(return_value=False)
    else:
        provider.supports_review_finding_state = MagicMock(side_effect=RuntimeError("unavailable"))
    provider.is_comment_authored_by_pr_agent = MagicMock(side_effect=AssertionError("unsupported check"))
    own = _comment(own=True)
    foreign = _comment(own=False)

    assert _publish(publisher, [own, foreign]) is foreign

    assert provider.edit_comment.call_args.args[0] is foreign
    provider.is_comment_authored_by_pr_agent.assert_not_called()
    provider.publish_comment.assert_not_called()


@pytest.mark.parametrize(("state_support", "comment_support", "expected"), [
    (True, True, True),
    (False, True, False),
    (True, False, False),
    # Mocks and loosely typed providers return truthy objects; only an explicit True counts.
    (MagicMock(), True, False),
    (RuntimeError("unavailable"), True, False),
])
def test_review_and_suggestions_share_the_authorship_gate(state_support, comment_support, expected):
    provider = MagicMock()
    if isinstance(state_support, Exception):
        provider.supports_review_finding_state.side_effect = state_support
    else:
        provider.supports_review_finding_state.return_value = state_support
    provider.is_supported.return_value = comment_support
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider

    assert comment_authorship_available(provider) is expected
    assert reviewer._review_comment_authorship_available() is expected


def test_authorship_gate_without_a_provider():
    assert comment_authorship_available(None) is False
    assert PRReviewer.__new__(PRReviewer)._review_comment_authorship_available() is False
