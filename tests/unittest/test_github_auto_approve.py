"""Tests for hardening the GitHub auto-approval path (issue #4693).

- The approval review is pinned to the reviewed head SHA (``commit`` argument).
- The approval is skipped when the PR head moved after it was read.
- ``config.enable_auto_approval`` is host-only: a reviewed repository's
  ``.pr_agent.toml`` cannot switch the approval action on.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pr_agent.git_providers.github_provider as gp
from pr_agent.config_security import is_repo_host_only_key


def make_provider(reviewed_sha, current_sha, review_result=None):
    provider = gp.GithubProvider.__new__(gp.GithubProvider)
    provider._pr_revision_loaded = (reviewed_sha, "base-sha", 3)
    review = SimpleNamespace(state="APPROVED")
    provider.pr = Mock()
    provider.pr.create_review = Mock(return_value=review_result or review)
    provider._get_pr = Mock(return_value=SimpleNamespace(head=SimpleNamespace(sha=current_sha)))
    provider._get_repo = Mock()
    provider._get_repo.return_value.get_commit = Mock(return_value=Mock())
    return provider


def test_auto_approve_pins_the_reviewed_head_sha():
    provider = make_provider(reviewed_sha="aaa111", current_sha="aaa111")

    assert provider.auto_approve() is True
    provider._get_repo.return_value.get_commit.assert_called_once_with("aaa111")
    provider.pr.create_review.assert_called_once_with(
        event="APPROVE", commit=provider._get_repo.return_value.get_commit.return_value
    )


def test_auto_approve_skips_when_no_snapshot_to_pin():
    provider = gp.GithubProvider.__new__(gp.GithubProvider)
    provider._pr_revision_loaded = ()
    provider.pr = Mock()
    provider._get_pr = Mock(return_value=SimpleNamespace(head=SimpleNamespace(sha="aaa111")))

    assert provider.auto_approve() is False
    provider.pr.create_review.assert_not_called()


def test_auto_approve_skips_when_head_moved():
    provider = make_provider(reviewed_sha="aaa111", current_sha="bbb222")

    assert provider.auto_approve() is False
    provider.pr.create_review.assert_not_called()


def test_auto_approve_returns_false_when_review_not_approved():
    provider = make_provider(
        reviewed_sha="aaa111", current_sha="aaa111", review_result=SimpleNamespace(state="PENDING")
    )

    assert provider.auto_approve() is False


def test_enable_auto_approval_is_repo_host_only():
    assert is_repo_host_only_key("config", "enable_auto_approval") is True


def _real_pull_request():
    """Drive PyGithub's real ``create_review`` with a mocked requester."""
    import github.Commit
    import github.PullRequest
    from github.Requester import Requester

    requester = Requester(
        auth=None,
        base_url="https://api.github.com",
        timeout=10,
        user_agent="test",
        per_page=30,
        verify=True,
        retry=None,
        pool_size=None,
    )
    requester.requestJsonAndCheck = Mock(return_value=({}, {"id": 1, "state": "APPROVED", "body": None}))
    pull_request = github.PullRequest.PullRequest(
        requester, {}, {"id": 1, "url": "https://api.github.com/repos/octocat/demo/pulls/1"}, completed=True
    )
    commit = github.Commit.Commit(requester, {}, {"sha": "aaa111"}, completed=True)
    return pull_request, commit


def test_real_create_review_accepts_commit_object():
    pull_request, commit = _real_pull_request()

    # Must not raise: PyGithub's is_optional(commit, Commit) type check passes.
    review = pull_request.create_review(event="APPROVE", commit=commit)
    assert review.state == "APPROVED"


def test_real_create_review_rejects_sha_string():
    pull_request, _ = _real_pull_request()

    # Documents why the provider must convert the SHA to a Commit object.
    import pytest

    with pytest.raises(AssertionError):
        pull_request.create_review(event="APPROVE", commit="aaa111")
