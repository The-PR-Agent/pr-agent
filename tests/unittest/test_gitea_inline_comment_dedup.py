import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.algo.inline_comment_dedup import (
    InlineCommentStore,
    can_verify_inline_comment_publication,
    key_issue_body_with_markers,
)
from pr_agent.algo.types import FilePatchInfo
from pr_agent.git_providers.gitea_provider import GiteaProvider, RepoApi
from pr_agent.tools.pr_reviewer import PRReviewer


def _provider(issue_comment_bodies=(), review_comment_bodies=()):
    provider = GiteaProvider.__new__(GiteaProvider)
    provider.logger = MagicMock()
    provider.owner = "owner"
    provider.repo = "repo"
    provider.pr_number = 4
    provider.enabled_pr = True
    provider.enabled_issue = False
    provider.last_commit = SimpleNamespace(sha="head-sha")
    provider.max_comment_chars = 65000
    provider._published_inline_comment_bodies = []
    provider.repo_api = MagicMock()
    provider.repo_api.list_all_comments.return_value = [
        SimpleNamespace(body=body) for body in issue_comment_bodies
    ]
    provider.repo_api.list_pull_review_comment_bodies.return_value = list(review_comment_bodies)
    return provider


def _page(items):
    return SimpleNamespace(data=BytesIO(json.dumps(items).encode("utf-8")))


def test_gitea_exposes_inline_dedup_capabilities():
    provider = _provider(["summary comment"], ["old inline finding"])

    assert can_verify_inline_comment_publication(provider)
    assert provider.get_persistent_comment_bodies() == ["summary comment", "old inline finding"]
    assert provider.get_recent_inline_comment_bodies() == []
    provider.repo_api.list_pull_review_comment_bodies.assert_called_once_with("owner", "repo", 4)


def test_store_sees_marker_in_an_existing_review_comment():
    """Inline findings are review comments, which Gitea's issue-comments endpoint leaves out,
    so a marker that only exists there must still stop the next run from posting it again."""
    provider = _provider([], ["finding\n\n<!-- pr-agent-dedup: abcdef123456 -->"])

    store = InlineCommentStore(provider)
    store.load()

    assert not store.load_failed
    assert store.seen("abcdef123456")


def test_store_load_fails_when_review_comments_cannot_be_listed():
    """A listing failure must not pass as "no existing comments", or the reviewer would
    publish findings that are already on the PR."""
    provider = _provider()
    provider.repo_api.list_pull_review_comment_bodies.side_effect = RuntimeError("API unavailable")

    store = InlineCommentStore(provider)
    store.load()

    assert store.load_failed


def test_gitea_publish_records_body_and_preserves_markers():
    provider = _provider()
    body = key_issue_body_with_markers("finding", "abcdef123456", "123456abcdef", 65000, provider)

    assert provider.publish_code_suggestions(
        [{"body": body, "relevant_file": "a.py", "relevant_lines_start": 3}]) is True

    published = provider.get_recent_inline_comment_bodies()
    assert published == [body]
    assert "pr-agent-key-issue-location: 123456abcdef" in published[0]
    assert provider.get_persistent_comment_bodies() == published


def test_gitea_failed_inline_publish_does_not_record_body():
    """The reviewer drops a finding from the summary once its body is recorded, so a
    rejected comment must stay unrecorded and keep the finding in the summary."""
    provider = _provider()
    provider.repo_api.create_inline_comment.side_effect = RuntimeError("500 Internal Server Error")

    assert provider.publish_inline_comments([{"body": "not posted", "path": "a.py", "new_position": 3}]) is False
    assert provider.get_recent_inline_comment_bodies() == []


def test_gitea_persistent_bodies_include_recent_posts_only_once():
    provider = _provider(["summary comment"], ["posted this run"])
    provider._published_inline_comment_bodies.append("posted this run")

    assert provider.get_persistent_comment_bodies() == ["posted this run", "summary comment"]


def test_reviewer_publishes_key_issue_inline_on_gitea():
    provider = _provider()
    provider.get_diff_files = MagicMock(return_value=[
        FilePatchInfo(base_file="", head_file="one\ntwo\nthree\n", patch="", filename="app.py")
    ])
    provider._inline_comment_store = None
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    issue = {"relevant_file": "app.py", "issue_header": "Possible Issue",
             "issue_content": "The new branch never releases the lock.", "start_line": 2, "end_line": 2}

    result = reviewer._publish_key_issues_as_inline_comments({"review": {"key_issues_to_review": [issue]}})

    assert "key_issues_to_review" not in result["review"]
    payload = provider.repo_api.create_inline_comment.call_args.kwargs["comments"]
    assert payload[0]["path"] == "app.py"
    assert payload[0]["old_position"] == 0
    assert payload[0]["new_position"] == 2


class TestRepoApiReviewCommentBodies:
    @staticmethod
    def _repo_api(responses):
        repo_api = RepoApi(MagicMock())
        repo_api.logger = MagicMock()
        repo_api.api_client.call_api.side_effect = responses
        return repo_api

    def test_collects_comment_bodies_across_review_pages(self):
        repo_api = self._repo_api([
            _page([{"id": 1, "comments_count": 1}, {"id": 2, "comments_count": 0}]),
            _page([{"id": 3, "comments_count": 2}]),
            _page([]),
            _page([{"body": "first"}]),
            _page([{"body": "second"}, {"body": "third"}]),
        ])

        bodies = repo_api.list_pull_review_comment_bodies("owner", "repo", 7)

        assert bodies == ["first", "second", "third"]
        requested = [call.args[0] for call in repo_api.api_client.call_api.call_args_list]
        # Review 2 has no comments, so its comments are not requested.
        assert requested[3:] == ["/repos/owner/repo/pulls/7/reviews/1/comments",
                                 "/repos/owner/repo/pulls/7/reviews/3/comments"]
