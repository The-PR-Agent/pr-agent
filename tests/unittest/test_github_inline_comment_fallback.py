from unittest.mock import MagicMock

import pytest

from pr_agent.git_providers.github_provider import GithubProvider


class _Status422Error(Exception):
    """Mimics a GithubException carrying an HTTP 422 status, which triggers the
    verification fallback in ``publish_inline_comments``."""
    status = 422


def _make_provider(create_review_side_effect):
    """Build a GithubProvider without running __init__ and stub the PyGithub PR
    object so only ``create_review`` behaviour is under test."""
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = MagicMock()
    provider.last_commit_id = MagicMock()
    provider.pr.create_review.side_effect = create_review_side_effect
    return provider


def test_fallback_propagates_when_verified_bulk_publish_fails(monkeypatch):
    """Regression for #2261: when the fallback bulk-publishes the verified
    comments and that GitHub call fails (rate limit / network / 5xx), the error
    must propagate instead of being silently swallowed."""
    comments = [{"body": "x", "path": "a.py", "line": 1, "side": "RIGHT"}]
    # 1st create_review (initial bulk) -> 422 to enter the fallback path
    # 2nd create_review (verified bulk inside fallback) -> transient failure
    provider = _make_provider([_Status422Error("invalid"), RuntimeError("rate limited")])
    # All comments verify as valid; avoids the real verification API + sleep(1).
    monkeypatch.setattr(provider, "_verify_code_comments", lambda c: (list(c), []))

    with pytest.raises(RuntimeError):
        provider.publish_inline_comments(comments)


def test_publish_code_suggestions_returns_false_so_retry_triggers(monkeypatch):
    """The contract the bug breaks: publish_code_suggestions must return False
    when comments were not actually published, so the one-by-one retry in
    pr_code_suggestions runs instead of reporting success."""
    provider = _make_provider([_Status422Error("invalid"), RuntimeError("rate limited")])
    provider.validate_comments_inside_hunks = lambda cs: cs  # passthrough
    monkeypatch.setattr(provider, "_verify_code_comments", lambda c: (list(c), []))

    suggestions = [{
        "body": "**Suggestion:** use the helper",
        "relevant_file": "a.py",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
    }]

    assert provider.publish_code_suggestions(suggestions) is False
