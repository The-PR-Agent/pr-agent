"""Tests for the GitLab auto-approval path (issue #4698).

The approval is pinned to the reviewed head and target revision, skipped when the
merge request moved after it was read, and withdrawn through the merge request
unapprove endpoint.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pr_agent.git_providers.gitlab_provider as gp


def _mr(head_sha="aaa111", start_sha="start111", target_branch="main"):
    return Mock(
        diff_refs={"head_sha": head_sha, "start_sha": start_sha},
        target_branch=target_branch,
    )


def make_provider(reviewed_sha="aaa111", current_sha="aaa111",
                  start_sha="start111", target_branch="main"):
    provider = gp.GitLabProvider.__new__(gp.GitLabProvider)
    provider._diff_head_sha = reviewed_sha
    provider.mr = _mr(reviewed_sha, start_sha, target_branch)
    provider._get_merge_request = Mock(return_value=_mr(current_sha, start_sha, target_branch))
    return provider


def test_auto_approve_pins_the_reviewed_head_sha():
    provider = make_provider()

    assert provider.auto_approve(body="verdict: no_concerns_found") is True
    provider.mr.approve.assert_called_once_with(sha="aaa111")


def test_auto_approve_skips_when_no_reviewed_sha_to_pin():
    provider = gp.GitLabProvider.__new__(gp.GitLabProvider)
    provider.mr = Mock()
    provider._get_merge_request = Mock()

    assert provider.auto_approve() is False
    provider.mr.approve.assert_not_called()
    provider._get_merge_request.assert_not_called()


def test_auto_approve_skips_when_head_moved():
    provider = make_provider(current_sha="bbb222")

    assert provider.auto_approve() is False
    provider.mr.approve.assert_not_called()


def test_auto_approve_skips_when_the_target_branch_moved():
    provider = make_provider()
    provider._get_merge_request = Mock(return_value=_mr("aaa111", target_branch="release"))

    assert provider.auto_approve() is False
    provider.mr.approve.assert_not_called()


def test_auto_approve_skips_when_the_target_advanced():
    provider = make_provider()
    provider._get_merge_request = Mock(return_value=_mr("aaa111", start_sha="start999"))

    assert provider.auto_approve() is False
    provider.mr.approve.assert_not_called()


def test_auto_approve_withdraws_when_a_push_lands_during_approval():
    provider = make_provider()
    provider._get_merge_request = Mock(side_effect=[_mr("aaa111"), _mr("bbb222")])

    assert provider.auto_approve() is False
    provider.mr.approve.assert_called_once_with(sha="aaa111")
    provider.mr.unapprove.assert_called_once()


def test_auto_approve_withdraws_when_a_retarget_lands_during_approval():
    provider = make_provider()
    provider._get_merge_request = Mock(
        side_effect=[_mr("aaa111"), _mr("aaa111", target_branch="release")])

    assert provider.auto_approve() is False
    provider.mr.approve.assert_called_once_with(sha="aaa111")
    provider.mr.unapprove.assert_called_once()


def test_auto_approve_withdraws_when_the_recheck_fails():
    provider = make_provider()
    provider._get_merge_request = Mock(side_effect=[_mr("aaa111"), gp.GitlabError("bad gateway")])

    assert provider.auto_approve() is False
    provider.mr.approve.assert_called_once_with(sha="aaa111")
    provider.mr.unapprove.assert_called_once()


def test_auto_approve_reports_provider_errors():
    provider = make_provider()
    provider.mr.approve.side_effect = gp.GitlabError("approvals disabled")

    assert provider.auto_approve() is False


def test_revoke_auto_approval_unapproves_the_merge_request():
    provider = make_provider()

    assert provider.revoke_auto_approval("stale") is True
    provider.mr.unapprove.assert_called_once()


def test_revoke_auto_approval_skips_when_this_account_has_not_approved():
    provider = make_provider()
    provider._get_own_user_id = Mock(return_value=7)
    provider.mr.approvals.get.return_value = SimpleNamespace(
        approved_by=[{"user": {"id": 8}}]
    )

    assert provider.revoke_auto_approval("stale") is True
    provider.mr.unapprove.assert_not_called()


def test_revoke_auto_approval_unapproves_this_accounts_approval():
    provider = make_provider()
    provider._get_own_user_id = Mock(return_value=7)
    provider.mr.approvals.get.return_value = SimpleNamespace(
        approved_by=[{"user": {"id": 7}}]
    )

    assert provider.revoke_auto_approval("stale") is True
    provider.mr.unapprove.assert_called_once()


def test_revoke_auto_approval_reports_provider_errors():
    provider = make_provider()
    provider.mr.unapprove.side_effect = gp.GitlabError("not approved")

    assert provider.revoke_auto_approval("stale") is False


def test_enable_auto_approval_is_repo_host_only():
    from pr_agent.config_security import is_repo_host_only_key

    assert is_repo_host_only_key("config", "enable_auto_approval") is True
