from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pr_agent.algo.inline_comment_dedup import (
    InlineCommentStore,
    can_verify_inline_comment_publication,
    key_issue_body_with_markers,
)
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider


def _provider(comment_bodies):
    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider._published_inline_comment_bodies = []
    provider.pr = MagicMock()
    provider.pr.comments.return_value = [SimpleNamespace(raw=body) for body in comment_bodies]
    return provider


def test_bitbucket_cloud_exposes_inline_dedup_capabilities():
    provider = _provider(["old inline finding"])

    assert can_verify_inline_comment_publication(provider)
    assert provider.get_persistent_comment_bodies() == ["old inline finding"]
    assert provider.get_recent_inline_comment_bodies() == []


def test_store_loads_bitbucket_persistent_bodies_without_failure():
    provider = _provider(["[pr-agent-dedup: abcdef123456]: https://github.com/The-PR-Agent/pr-agent"])
    store = InlineCommentStore(provider)
    store.load()
    assert not store.load_failed
    assert store.seen("abcdef123456")


def test_bitbucket_cloud_dedup_bodies_include_published_comments():
    provider = _provider([])
    provider._published_inline_comment_bodies.append("new inline finding")

    assert provider.get_persistent_comment_bodies() == ["new inline finding"]
    assert provider.get_recent_inline_comment_bodies() == ["new inline finding"]


def test_bitbucket_publish_records_body_and_preserves_markers():
    provider = _provider([])
    provider.headers = {}
    provider.bitbucket_comment_api_url = "https://bitbucket.example/comments"
    provider.max_comment_length = 31000
    body = key_issue_body_with_markers(
        "x" * 31_500, "abcdef123456", "123456abcdef", 31_000, provider
    )
    with patch("pr_agent.git_providers.bitbucket_provider.requests.request") as request:
        request.return_value.raise_for_status.return_value = None
        assert provider.publish_inline_comment(body, "file.py", 10)
    published = provider.get_recent_inline_comment_bodies()
    assert published and "pr-agent-key-issue-location: 123456abcdef" in published[0]
    assert provider.get_persistent_comment_bodies() == published


def test_bitbucket_cloud_skips_comments_without_raw_body():
    provider = _provider([])
    provider.pr.comments.return_value = [SimpleNamespace(raw=None), SimpleNamespace(raw="valid")]

    assert provider.get_persistent_comment_bodies() == ["valid"]
