from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.git_providers.bitbucket_server_provider import (
    MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH,
    BitbucketServerProvider,
)


def _provider():
    provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
    provider.workspace_slug = "AAA"
    provider.repo_slug = "my-repo"
    provider.pr_num = 1
    provider.bitbucket_client = MagicMock()
    return provider


def test_publish_comment_caps_a_body_over_the_api_limit():
    # Comment.getText() is @RequiredString(size=32768). Validation rejects a
    # longer comment (HTTP 400), so an overlong review would fail the publish.
    provider = _provider()
    body = "A" * (MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH + 100)

    provider.publish_comment(body)

    published = provider.bitbucket_client.add_pull_request_comment.call_args.args[3]
    assert len(published) == MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH
    assert published.endswith("...")
    assert published.startswith("A")


def test_publish_comment_keeps_a_body_within_the_limit():
    provider = _provider()
    body = "A" * MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH

    provider.publish_comment(body)

    assert provider.bitbucket_client.add_pull_request_comment.call_args.args[3] == body


def test_publish_comment_skips_temporary_comments():
    provider = _provider()

    assert provider.publish_comment("temporary", is_temporary=True) is None
    provider.bitbucket_client.add_pull_request_comment.assert_not_called()


def test_edit_comment_caps_a_body_over_the_api_limit():
    provider = _provider()
    comment = SimpleNamespace(id=9, version=3)
    body = "B" * (MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH + 50)

    assert provider.edit_comment(comment, body) is True

    published = provider.bitbucket_client.update_pull_request_comment.call_args.args[4]
    assert len(published) == MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH
    assert published.endswith("...")
    assert published.startswith("B")


def test_publish_inline_comment_caps_a_body_over_the_api_limit():
    provider = _provider()
    provider._get_pr_comments_path = MagicMock(return_value="/comments")
    body = "C" * (MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH + 20)

    assert provider.publish_inline_comment(body, "src/app.py", 12) is True

    published = provider.bitbucket_client.post.call_args.kwargs["data"]["text"]
    assert len(published) == MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH
    assert published.endswith("...")
    assert published.startswith("C")
