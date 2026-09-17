from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.algo.inline_comment_dedup import (
    InlineCommentStore,
    can_verify_inline_comment_publication,
    key_issue_body_with_markers,
    key_issue_fingerprint,
    key_issue_location_fingerprint,
)
from pr_agent.git_providers.github_provider import GithubProvider


def _provider(comment_bodies):
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = MagicMock()
    provider.pr.get_comments.return_value = [SimpleNamespace(body=body) for body in comment_bodies]
    return provider


def test_github_exposes_inline_dedup_capabilities():
    provider = _provider(["old inline finding"])

    assert can_verify_inline_comment_publication(provider)
    assert provider.get_persistent_comment_bodies() == ["old inline finding"]
    assert provider.get_recent_inline_comment_bodies() == ["old inline finding"]


def test_store_loads_github_persistent_bodies_without_failure():
    provider = _provider(["finding\n\n<!-- pr-agent-dedup: abcdef123456 -->"])
    store = InlineCommentStore(provider)
    store.load()
    assert not store.load_failed
    assert store.seen("abcdef123456")


def test_github_recent_bodies_include_a_finding_published_this_run():
    provider = _provider(["first run finding"])

    assert provider.get_recent_inline_comment_bodies() == ["first run finding"]

    provider.pr.get_comments.return_value.append(
        SimpleNamespace(body="this run finding"))

    assert provider.get_recent_inline_comment_bodies() == [
        "first run finding", "this run finding"]


def test_github_key_issue_verification_reads_the_published_comment():
    # Mirrors PRReviewer._published_inline_key_issue_fingerprints: after the
    # review comment is accepted, its markers must surface through the reader.
    provider = _provider([])
    store = InlineCommentStore(provider)
    store.load()
    body_fp = key_issue_fingerprint("file.py", "a finding")
    location_fp = key_issue_location_fingerprint(body_fp, 10, 12)
    published = key_issue_body_with_markers(
        "**Possible Issue**\n\na finding", body_fp, location_fp)
    provider.pr.get_comments.return_value.append(SimpleNamespace(body=published))

    for body in provider.get_recent_inline_comment_bodies():
        store.add_body(body)

    assert store.seen(body_fp)
    assert store.seen(location_fp)


def test_github_reader_skips_empty_bodies():
    provider = _provider([None, "", "valid"])

    assert provider.get_persistent_comment_bodies() == ["valid"]
    assert provider.get_recent_inline_comment_bodies() == ["valid"]
