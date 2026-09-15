from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.algo.inline_comment_dedup import can_verify_inline_comment_publication
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


def test_bitbucket_cloud_dedup_bodies_include_published_comments():
    provider = _provider([])
    provider._published_inline_comment_bodies.append("new inline finding")

    assert provider.get_persistent_comment_bodies() == ["new inline finding"]
    assert provider.get_recent_inline_comment_bodies() == ["new inline finding"]


def test_bitbucket_cloud_skips_comments_without_raw_body():
    provider = _provider([])
    provider.pr.comments.return_value = [SimpleNamespace(raw=None), SimpleNamespace(raw="valid")]

    assert provider.get_persistent_comment_bodies() == ["valid"]
