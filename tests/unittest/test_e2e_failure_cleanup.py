"""E2E harness cleanup must not obscure the failure that triggered it."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests import HTTPError


@pytest.fixture
def bitbucket_e2e():
    pytest.importorskip("atlassian.bitbucket")
    from tests.e2e_tests import test_bitbucket_app

    return test_bitbucket_app


@pytest.fixture
def github_e2e():
    pytest.importorskip("github")
    from tests.e2e_tests import test_github_app

    return test_github_app


@pytest.fixture
def gitlab_e2e():
    pytest.importorskip("gitlab")
    from tests.e2e_tests import test_gitlab_webhook

    return test_gitlab_webhook


def _stub_settings(monkeypatch, module):
    settings = SimpleNamespace(
        config=SimpleNamespace(git_provider=None),
        gitlab=SimpleNamespace(PERSONAL_ACCESS_TOKEN="test-token"),
        get=lambda *_args: "test-value",
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)


def test_bitbucket_setup_error_is_not_replaced_by_cleanup(monkeypatch, bitbucket_e2e):
    _stub_settings(monkeypatch, bitbucket_e2e)
    original = RuntimeError("Bitbucket client unavailable")
    monkeypatch.setattr(bitbucket_e2e, "Cloud", Mock(side_effect=original))

    with pytest.raises(AssertionError) as failure:
        bitbucket_e2e.test_e2e_run_bitbucket_app()

    assert failure.value.__cause__ is original


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_bitbucket_failed_upload_stops_before_pr_creation_and_cleans_branch(monkeypatch, bitbucket_e2e, cleanup_fails):
    _stub_settings(monkeypatch, bitbucket_e2e)
    client = Mock()
    repo = client.workspaces.get.return_value.repositories.get.return_value
    monkeypatch.setattr(bitbucket_e2e, "Cloud", Mock(return_value=client))
    original = HTTPError("test upload rejected")
    response = Mock()
    response.raise_for_status.side_effect = original
    request = Mock(return_value=response)
    monkeypatch.setattr(bitbucket_e2e.requests, "request", request)
    logger = Mock()
    monkeypatch.setattr(bitbucket_e2e, "logger", logger)
    if cleanup_fails:
        repo.branches.delete.side_effect = RuntimeError("branch deletion unavailable")

    with pytest.raises(AssertionError) as failure:
        bitbucket_e2e.test_e2e_run_bitbucket_app()

    assert failure.value.__cause__ is original
    request.assert_called_once()
    assert request.call_args.kwargs["timeout"] == 30
    repo.pullrequests.create.assert_not_called()
    repo.branches.delete.assert_called_once()
    if cleanup_fails:
        logger.exception.assert_called_once_with("Failed to delete the Bitbucket test branch during cleanup")


def test_bitbucket_decline_failure_preserves_original_error_and_deletes_branch(monkeypatch, bitbucket_e2e):
    _stub_settings(monkeypatch, bitbucket_e2e)
    client = Mock()
    repo = client.workspaces.get.return_value.repositories.get.return_value
    pr = repo.pullrequests.create.return_value
    original = RuntimeError("test comments unavailable")
    pr.comments.side_effect = original
    pr.decline.side_effect = RuntimeError("PR decline unavailable")
    monkeypatch.setattr(bitbucket_e2e, "Cloud", Mock(return_value=client))
    monkeypatch.setattr(bitbucket_e2e.requests, "request", Mock(return_value=Mock()))
    monkeypatch.setattr(bitbucket_e2e.time, "sleep", Mock())
    logger = Mock()
    monkeypatch.setattr(bitbucket_e2e, "logger", logger)

    with pytest.raises(AssertionError) as failure:
        bitbucket_e2e.test_e2e_run_bitbucket_app()

    assert failure.value.__cause__ is original
    pr.decline.assert_called_once()
    repo.branches.delete.assert_called_once()
    logger.exception.assert_called_once_with("Failed to decline the Bitbucket test PR during cleanup")


def test_github_branch_creation_error_does_not_delete_nonexistent_branch(monkeypatch, github_e2e):
    _stub_settings(monkeypatch, github_e2e)
    provider = Mock()
    repo = provider.github_client.get_repo.return_value
    original = RuntimeError("test branch creation failed")
    repo.create_git_ref.side_effect = original
    monkeypatch.setattr(github_e2e, "get_git_provider", lambda: lambda: provider)

    with pytest.raises(AssertionError) as failure:
        github_e2e.test_e2e_run_github_app()

    assert failure.value.__cause__ is original
    repo.get_git_ref.assert_not_called()


def test_gitlab_branch_creation_error_does_not_delete_nonexistent_branch(monkeypatch, gitlab_e2e):
    _stub_settings(monkeypatch, gitlab_e2e)
    client = Mock()
    project = client.projects.get.return_value
    original = RuntimeError("test branch creation failed")
    project.branches.create.side_effect = original
    monkeypatch.setattr(gitlab_e2e.gitlab, "Gitlab", Mock(return_value=client))

    with pytest.raises(AssertionError) as failure:
        gitlab_e2e.test_e2e_run_github_app()

    assert failure.value.__cause__ is original
    project.branches.delete.assert_not_called()


def test_github_cleanup_failure_preserves_original_error(monkeypatch, github_e2e):
    _stub_settings(monkeypatch, github_e2e)
    provider = Mock()
    repo = provider.github_client.get_repo.return_value
    original = RuntimeError("test file lookup failed")
    repo.get_contents.side_effect = original
    repo.get_git_ref.return_value.delete.side_effect = RuntimeError("branch deletion unavailable")
    monkeypatch.setattr(github_e2e, "get_git_provider", lambda: lambda: provider)
    logger = Mock()
    monkeypatch.setattr(github_e2e, "logger", logger)

    with pytest.raises(AssertionError) as failure:
        github_e2e.test_e2e_run_github_app()

    assert failure.value.__cause__ is original
    repo.get_git_ref.return_value.delete.assert_called_once()
    logger.exception.assert_called_once_with("Failed to delete the GitHub test branch during cleanup")


def test_gitlab_cleanup_failure_preserves_original_error(monkeypatch, gitlab_e2e):
    _stub_settings(monkeypatch, gitlab_e2e)
    client = Mock()
    project = client.projects.get.return_value
    original = RuntimeError("test file lookup failed")
    project.files.get.side_effect = original
    project.branches.delete.side_effect = RuntimeError("branch deletion unavailable")
    monkeypatch.setattr(gitlab_e2e.gitlab, "Gitlab", Mock(return_value=client))
    logger = Mock()
    monkeypatch.setattr(gitlab_e2e, "logger", logger)

    with pytest.raises(AssertionError) as failure:
        gitlab_e2e.test_e2e_run_github_app()

    assert failure.value.__cause__ is original
    project.branches.delete.assert_called_once()
    logger.exception.assert_called_once_with("Failed to delete the GitLab test branch during cleanup")
