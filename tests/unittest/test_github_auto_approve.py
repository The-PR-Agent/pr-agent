"""Tests for the GitHub auto-approval path (issues #4693 and #4698).

- The approval review is pinned to the reviewed head SHA (``commit`` argument).
- The approval is skipped when the PR head moved after it was read.
- ``config.enable_auto_approval`` is host-only: a reviewed repository's
  ``.pr_agent.toml`` cannot switch the approval action on.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pr_agent.git_providers.github_provider as gp
from pr_agent.config_security import is_repo_host_only_key


def make_provider(reviewed_sha, current_sha, review_result=None, current_base_sha="base-sha"):
    provider = gp.GithubProvider.__new__(gp.GithubProvider)
    provider._pr_revision_loaded = (reviewed_sha, "base-sha", 3)
    review = SimpleNamespace(state="APPROVED")
    provider.pr = Mock()
    provider.pr.create_review = Mock(return_value=review_result or review)
    provider._get_pr = Mock(return_value=SimpleNamespace(
        head=SimpleNamespace(sha=current_sha), base=SimpleNamespace(sha=current_base_sha)
    ))
    provider._get_repo = Mock()
    provider._get_repo.return_value.get_commit = Mock(return_value=Mock())
    provider.is_comment_authored_by_pr_agent = Mock(return_value=True)
    return provider


def test_auto_approve_pins_the_reviewed_head_sha():
    provider = make_provider(reviewed_sha="aaa111", current_sha="aaa111")

    assert provider.auto_approve() is True
    provider._get_repo.return_value.get_commit.assert_called_once_with("aaa111")
    provider.pr.create_review.assert_called_once_with(
        event="APPROVE", commit=provider._get_repo.return_value.get_commit.return_value,
        body="<!-- pr-agent:review:auto-approval -->",
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


def test_auto_approve_passes_the_body():
    provider = make_provider(reviewed_sha="aaa111", current_sha="aaa111")

    assert provider.auto_approve(body="verdict: no_concerns_found") is True
    assert provider.pr.create_review.call_args.kwargs["body"] == (
        "<!-- pr-agent:review:auto-approval -->\nverdict: no_concerns_found"
    )


def test_auto_approve_dismisses_an_approval_it_cannot_attribute():
    review = Mock(state="APPROVED")
    provider = make_provider(reviewed_sha="aaa111", current_sha="aaa111", review_result=review)
    provider.is_comment_authored_by_pr_agent = Mock(return_value=False)

    assert provider.auto_approve() is False
    review.dismiss.assert_called_once()


def test_auto_approve_dismisses_when_identity_cannot_be_verified():
    review = Mock(state="APPROVED")
    provider = make_provider(reviewed_sha="aaa111", current_sha="aaa111", review_result=review)
    provider.is_comment_authored_by_pr_agent = Mock(side_effect=RuntimeError("GitHub identity cannot be verified"))

    assert provider.auto_approve() is False
    review.dismiss.assert_called_once()


def _provider_with_reviews(reviews, authored):
    provider = gp.GithubProvider.__new__(gp.GithubProvider)
    provider.pr = Mock()
    provider.pr.get_reviews = Mock(return_value=reviews)
    provider.is_comment_authored_by_pr_agent = Mock(side_effect=authored)
    return provider


MARKER = "<!-- pr-agent:review:auto-approval -->"


def test_revoke_dismisses_only_pr_agent_auto_approvals():
    own = Mock(state="APPROVED", body=f"{MARKER}\nApproved")
    other_bot = Mock(state="APPROVED", body=f"{MARKER}\nApproved")
    own_comment = Mock(state="COMMENTED", body=MARKER)
    authored = {id(own): True, id(other_bot): False, id(own_comment): True}
    provider = _provider_with_reviews([own, other_bot, own_comment], lambda review: authored[id(review)])

    assert provider.revoke_auto_approval("stale") is True
    own.dismiss.assert_called_once_with("stale")
    other_bot.dismiss.assert_not_called()
    own_comment.dismiss.assert_not_called()


def test_revoke_keeps_a_manual_approval_from_the_same_login():
    # With a personal token or the shared Actions identity, the token's login also approves by hand.
    manual = Mock(state="APPROVED", body="LGTM")
    provider = _provider_with_reviews([manual], lambda review: True)

    assert provider.revoke_auto_approval("stale") is True
    manual.dismiss.assert_not_called()


def test_revoke_logs_and_reports_a_forbidden_dismissal():
    import github

    own = Mock(state="APPROVED", body=MARKER)
    own.dismiss.side_effect = github.GithubException(403, {"message": "Forbidden"}, None)
    provider = _provider_with_reviews([own], lambda review: True)

    assert provider.revoke_auto_approval("stale") is False


def test_revoke_reports_failure_when_identity_cannot_be_verified():
    provider = _provider_with_reviews([Mock(state="APPROVED", body=MARKER)], RuntimeError("cannot verify"))

    assert provider.revoke_auto_approval("stale") is False


def test_real_create_review_accepts_a_body():
    pull_request, commit = _real_pull_request()

    review = pull_request.create_review(event="APPROVE", commit=commit, body="approved")
    assert review.state == "APPROVED"


def test_revoke_continues_past_a_review_it_cannot_attribute():
    unverifiable = Mock(state="APPROVED", body=MARKER)
    own = Mock(state="APPROVED", body=MARKER)

    def authored(review):
        if review is unverifiable:
            raise RuntimeError("cannot verify")
        return True

    provider = _provider_with_reviews([unverifiable, own], authored)

    assert provider.revoke_auto_approval("stale") is False
    own.dismiss.assert_called_once_with("stale")


def test_auto_approve_skips_when_the_base_moved():
    provider = make_provider(reviewed_sha="aaa111", current_sha="aaa111", current_base_sha="retargeted")

    assert provider.auto_approve() is False
    provider.pr.create_review.assert_not_called()


def _pr_at(head, base="base-sha"):
    return SimpleNamespace(head=SimpleNamespace(sha=head), base=SimpleNamespace(sha=base))


def test_auto_approve_withdraws_when_a_push_lands_during_approval():
    review = Mock(state="APPROVED")
    provider = make_provider(reviewed_sha="aaa111", current_sha="aaa111", review_result=review)
    provider._get_pr = Mock(side_effect=[_pr_at("aaa111"), _pr_at("bbb222")])

    assert provider.auto_approve() is False
    review.dismiss.assert_called_once()


def test_auto_approve_withdraws_when_the_recheck_fails():
    review = Mock(state="APPROVED")
    provider = make_provider(reviewed_sha="aaa111", current_sha="aaa111", review_result=review)
    provider._get_pr = Mock(side_effect=[_pr_at("aaa111"), gp.GithubException(502, "bad gateway", None)])

    assert provider.auto_approve() is False
    review.dismiss.assert_called_once()
