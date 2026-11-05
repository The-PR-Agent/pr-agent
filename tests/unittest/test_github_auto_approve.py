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
    return provider


def test_auto_approve_pins_the_reviewed_head_sha():
    provider = make_provider(reviewed_sha="aaa111", current_sha="aaa111")

    assert provider.auto_approve() is True
    provider.pr.create_review.assert_called_once_with(event="APPROVE", commit="aaa111")


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
