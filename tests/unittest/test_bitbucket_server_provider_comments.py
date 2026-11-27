from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.algo.review_finding_state import (
    append_review_state,
    parse_review_state,
    reconcile_review_findings,
    serialize_review_state,
)
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


def _provider_from_init():
    bitbucket_client = MagicMock()
    bitbucket_client.get.return_value = {"version": "8.16"}
    bitbucket_client.get_pull_request.return_value = {}
    return BitbucketServerProvider(
        "https://git.example.com/projects/AAA/repos/my-repo/pull-requests/1",
        bitbucket_client=bitbucket_client,
    )


def test_init_sets_max_comment_chars_so_a_state_marker_survives_the_api_cap():
    # The reviewer fits its hidden state marker inside max_comment_chars. If
    # that attribute is missing, publish_comment caps the tail and the marker
    # is lost.
    provider = _provider_from_init()
    assert provider.max_comment_chars == MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH

    state = reconcile_review_findings(
        None,
        [{"body": "issue", "path": "app.py", "line_start": 1, "line_end": 1}],
        allow_resolution=True,
        head_sha="head",
        timestamp="2026-01-01T00:00:00Z",
    ).state
    marker = serialize_review_state(state)
    long_review = "A" * (MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH + 200)

    prepared = append_review_state(long_review, state, max_chars=provider.max_comment_chars)
    assert len(prepared) <= provider.max_comment_chars
    assert marker in prepared
    parsed = parse_review_state(prepared)
    assert parsed.valid is True
    assert parsed.state == state

    provider.publish_comment(prepared)
    published = provider.bitbucket_client.add_pull_request_comment.call_args.args[3]
    assert len(published) <= MAX_PR_COMMENT_BITBUCKET_SERVER_LENGTH
    published_parsed = parse_review_state(published)
    assert published_parsed.valid is True
    assert published_parsed.state == state
    assert marker in published
