from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _setup_gitea_case(monkeypatch):
    """Return an isolated Gitea E2E module, mocked HTTP client, and logger."""
    import pr_agent.log as log_module

    monkeypatch.setattr(log_module, "setup_logger", MagicMock())
    from tests.e2e_tests import test_gitea_app as gitea_e2e

    settings = SimpleNamespace(
        config=SimpleNamespace(git_provider=None),
        get={
            "GITEA.URL": "https://gitea.example.test",
            "GITEA.TOKEN": "test-token",
        }.get,
    )
    http = MagicMock(spec=["get", "post", "put", "patch", "delete"])
    test_logger = MagicMock()
    monkeypatch.setattr(gitea_e2e, "get_settings", lambda: settings)
    monkeypatch.setattr(gitea_e2e, "requests", http)
    monkeypatch.setattr(gitea_e2e, "logger", test_logger)
    return gitea_e2e, http, test_logger


def _assert_native_create_contract(http):
    """Assert that the first POST uses Gitea's native branch API."""
    assert http.post.call_count >= 1
    create_call = http.post.call_args_list[0]
    assert create_call.args[0] == "https://gitea.example.test/api/v1/repos/codiumai/pr-agent-tests/branches"
    assert create_call.kwargs["json"]["old_ref_name"] == "main"
    new_branch = create_call.kwargs["json"]["new_branch_name"]
    assert new_branch.startswith("gitea_app_e2e_test-")
    assert set(create_call.kwargs["json"]) == {"new_branch_name", "old_ref_name"}
    return new_branch


def _expected_headers():
    """Return the headers used by the isolated Gitea E2E test."""
    return {
        "Authorization": "token test-token",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def test_gitea_e2e_does_not_delete_branch_when_creation_fails(monkeypatch):
    """Do not delete a branch that this test never confirmed it created."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    creation_failure = RuntimeError("branch creation rejected")
    http.post.return_value.raise_for_status.side_effect = creation_failure

    with pytest.raises(RuntimeError) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is creation_failure
    _assert_native_create_contract(http)
    assert http.post.call_count == 1
    http.get.assert_not_called()
    http.put.assert_not_called()
    http.patch.assert_not_called()
    http.delete.assert_not_called()


def test_gitea_e2e_cleans_up_branch_after_confirmed_creation(monkeypatch):
    """Use the native branch DELETE after creation succeeds and a later step fails."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    post_creation_failure = RuntimeError("file update failed")
    http.put.side_effect = post_creation_failure

    with pytest.raises(RuntimeError) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is post_creation_failure
    new_branch = _assert_native_create_contract(http)
    http.patch.assert_not_called()
    http.delete.assert_called_once_with(
        f"https://gitea.example.test/api/v1/repos/codiumai/pr-agent-tests/branches/{new_branch}",
        headers=_expected_headers(),
    )
    http.delete.return_value.raise_for_status.assert_called_once_with()


def test_gitea_e2e_branch_cleanup_survives_pr_cleanup_failure(monkeypatch):
    """Delete the run-owned branch even when fallback PR closure fails."""
    gitea_e2e, http, test_logger = _setup_gitea_case(monkeypatch)
    pr_response = MagicMock()
    pr_response.json.return_value = {"number": 123}
    http.post.side_effect = [MagicMock(), pr_response]
    close_failure = RuntimeError("pull request cleanup failed")
    http.patch.return_value.raise_for_status.side_effect = close_failure
    monkeypatch.setattr(gitea_e2e, "NUM_MINUTES", 0)

    with pytest.raises(AssertionError):
        gitea_e2e.test_e2e_run_gitea_app()

    new_branch = _assert_native_create_contract(http)
    http.patch.assert_called_once()
    http.patch.return_value.raise_for_status.assert_called_once_with()
    http.delete.assert_called_once_with(
        f"https://gitea.example.test/api/v1/repos/codiumai/pr-agent-tests/branches/{new_branch}",
        headers=_expected_headers(),
    )
    http.delete.return_value.raise_for_status.assert_called_once_with()
    test_logger.error.assert_any_call(f"Failed to clean up after test: {close_failure}")


def test_gitea_e2e_reports_branch_cleanup_http_failure(monkeypatch):
    """Log a fallback branch DELETE failure surfaced by raise_for_status()."""
    gitea_e2e, http, test_logger = _setup_gitea_case(monkeypatch)
    post_creation_failure = RuntimeError("file update failed")
    cleanup_failure = RuntimeError("branch cleanup failed")
    http.put.side_effect = post_creation_failure
    http.delete.return_value.raise_for_status.side_effect = cleanup_failure

    with pytest.raises(RuntimeError) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is post_creation_failure
    new_branch = _assert_native_create_contract(http)
    http.delete.assert_called_once_with(
        f"https://gitea.example.test/api/v1/repos/codiumai/pr-agent-tests/branches/{new_branch}",
        headers=_expected_headers(),
    )
    http.delete.return_value.raise_for_status.assert_called_once_with()
    test_logger.error.assert_any_call(f"Failed to clean up after test: {cleanup_failure}")


def test_gitea_e2e_does_not_repeat_successful_pr_cleanup(monkeypatch):
    """Do not close an already-closed PR again when normal branch deletion fails."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.json.return_value = {"sha": "file-sha"}
    comments_response = MagicMock()
    comments_response.json.return_value = [
        {"body": "## PR Reviewer Guide 🔍"},
        {"body": "comment 2"},
        {"body": "comment 3"},
        {"body": "comment 4"},
        {"body": "comment 5"},
    ]
    http.get.side_effect = [file_response, comments_response]

    pr_response = MagicMock()
    pr_response.json.return_value = {"number": 123}
    http.post.side_effect = [MagicMock(), pr_response]

    cleanup_failure = RuntimeError("normal branch cleanup failed")
    failed_delete = MagicMock()
    failed_delete.raise_for_status.side_effect = cleanup_failure
    successful_fallback_delete = MagicMock()
    http.delete.side_effect = [failed_delete, successful_fallback_delete]
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is cleanup_failure
    new_branch = _assert_native_create_contract(http)
    http.patch.assert_called_once_with(
        "https://gitea.example.test/api/v1/repos/codiumai/pr-agent-tests/pulls/123",
        headers=_expected_headers(),
        json={"state": "closed"},
    )
    assert http.delete.call_count == 2
    assert http.delete.call_args_list[0].args[0].endswith(f"/branches/{new_branch}")
    assert http.delete.call_args_list[1].args[0].endswith(f"/branches/{new_branch}")
    successful_fallback_delete.raise_for_status.assert_called_once_with()
