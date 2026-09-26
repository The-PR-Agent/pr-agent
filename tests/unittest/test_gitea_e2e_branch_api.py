import importlib.util
import json
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from requests import Response
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    ContentDecodingError,
    HTTPError,
    Timeout,
)


def _setup_gitea_case(monkeypatch):
    """Return an isolated Gitea E2E module, mocked HTTP client, and logger."""
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
    monkeypatch.setattr(gitea_e2e, "setup_logger", MagicMock())
    monkeypatch.setattr(gitea_e2e, "get_logger", lambda: test_logger)
    monkeypatch.setattr(gitea_e2e, "requests", http)
    return gitea_e2e, http, test_logger


def _logged_errors(logger):
    return "\n".join(call.args[0] for call in logger.error.call_args_list if call.args)


def _assert_logged_error(logger, message):
    assert message in _logged_errors(logger)


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


def _streaming_json_response(payload):
    """Build a mocked streaming JSON response."""

    def iter_content(chunk_size=8192):
        value = payload() if callable(payload) else payload
        return [json.dumps(value).encode()]

    response = MagicMock()
    response.iter_content.side_effect = iter_content
    return response


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_gitea_e2e_does_not_delete_branch_when_creation_is_rejected(monkeypatch, status):
    """Do not delete a branch after Gitea definitively rejects its creation."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    response = Response()
    response.status_code = status
    creation_failure = HTTPError("branch creation rejected", response=response)
    http.post.return_value.raise_for_status.side_effect = creation_failure

    with pytest.raises(HTTPError) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is creation_failure
    _assert_native_create_contract(http)
    connect_timeout, read_timeout = http.post.call_args.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    assert http.post.call_args.kwargs["stream"] is True
    http.post.return_value.close.assert_called_once_with()
    assert http.post.call_count == 1
    http.get.assert_not_called()
    http.put.assert_not_called()
    http.patch.assert_not_called()
    http.delete.assert_not_called()


def test_gitea_e2e_branch_creation_uses_bounded_streaming_request(monkeypatch):
    """Bound branch creation across connect/read phases and close the unused response body."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    post_creation_failure = RuntimeError("file update failed")
    http.put.side_effect = post_creation_failure

    with pytest.raises(RuntimeError):
        gitea_e2e.test_e2e_run_gitea_app()

    create_call = http.post.call_args_list[0]
    connect_timeout, read_timeout = create_call.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    assert create_call.kwargs["stream"] is True
    http.post.return_value.close.assert_called_once_with()


def test_gitea_e2e_cleans_up_branch_after_confirmed_creation(monkeypatch):
    """Use the native branch DELETE after creation succeeds and a later step fails."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    post_creation_failure = RuntimeError("file update failed")
    http.put.side_effect = post_creation_failure

    with pytest.raises(RuntimeError) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is post_creation_failure
    new_branch = _assert_native_create_contract(http)
    assert http.get.call_args.kwargs["timeout"] == gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    assert http.put.call_args.kwargs["timeout"] == gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    http.patch.assert_not_called()
    http.delete.assert_called_once()
    delete_call = http.delete.call_args
    assert delete_call.args[0].endswith(f"/branches/{new_branch}")
    assert delete_call.kwargs["headers"] == _expected_headers()
    connect_timeout, read_timeout = delete_call.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
    assert delete_call.kwargs["stream"] is True
    http.delete.return_value.raise_for_status.assert_called_once_with()
    http.delete.return_value.close.assert_called_once_with()


def test_gitea_e2e_branch_cleanup_survives_pr_cleanup_failure(monkeypatch):
    """Delete the run-owned branch even when fallback PR closure fails."""
    gitea_e2e, http, test_logger = _setup_gitea_case(monkeypatch)
    pr_response = _streaming_json_response({"number": 123})
    http.post.side_effect = [MagicMock(), pr_response]
    http.get.side_effect = [MagicMock()]
    close_failure = RuntimeError("pull request cleanup failed")
    http.patch.return_value.raise_for_status.side_effect = close_failure
    monkeypatch.setattr(gitea_e2e, "NUM_MINUTES", 0)

    with pytest.raises(AssertionError):
        gitea_e2e.test_e2e_run_gitea_app()

    new_branch = _assert_native_create_contract(http)
    http.patch.assert_called_once()
    connect_timeout, read_timeout = http.patch.call_args.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
    assert http.patch.call_args.kwargs["stream"] is True
    http.patch.return_value.raise_for_status.assert_called_once_with()
    http.delete.assert_called_once()
    delete_call = http.delete.call_args
    assert delete_call.args[0].endswith(f"/branches/{new_branch}")
    assert delete_call.kwargs["headers"] == _expected_headers()
    connect_timeout, read_timeout = delete_call.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
    assert delete_call.kwargs["stream"] is True
    http.delete.return_value.raise_for_status.assert_called_once_with()
    http.delete.return_value.close.assert_called_once_with()
    _assert_logged_error(test_logger, f"Failed to clean up after test: {close_failure}")


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
    http.delete.assert_called_once()
    delete_call = http.delete.call_args
    assert delete_call.args[0].endswith(f"/branches/{new_branch}")
    assert delete_call.kwargs["headers"] == _expected_headers()
    connect_timeout, read_timeout = delete_call.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
    assert delete_call.kwargs["stream"] is True
    http.delete.return_value.raise_for_status.assert_called_once_with()
    http.delete.return_value.close.assert_called_once_with()
    _assert_logged_error(test_logger, f"Failed to clean up after test: {cleanup_failure}")


def test_gitea_e2e_does_not_repeat_successful_pr_cleanup(monkeypatch):
    """Do not close an already-closed PR again when normal branch deletion fails."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    results = MagicMock(return_value=[])
    monkeypatch.setattr(gitea_e2e, "_missing_gitea_tool_results", results)
    pr_response = _streaming_json_response({"number": 123})
    http.post.side_effect = [MagicMock(), pr_response]
    http.get.side_effect = [file_response]

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
    pr_create_call = http.post.call_args_list[1]
    connect_timeout, read_timeout = pr_create_call.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    assert pr_create_call.kwargs["stream"] is True
    pr_response.close.assert_called_once_with()
    pr_response.iter_content.assert_called_once_with(chunk_size=8192)
    assert http.put.call_args.kwargs["timeout"] == gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    assert len(http.get.call_args_list) == 1
    assert http.get.call_args_list[0].kwargs["timeout"] == gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    results.assert_called_once_with(
        "https://gitea.example.test/api/v1/repos/codiumai/pr-agent-tests", 123, _expected_headers()
    )
    http.patch.assert_called_once()
    close_call = http.patch.call_args
    assert close_call.args[0].endswith("/pulls/123")
    assert close_call.kwargs["headers"] == _expected_headers()
    assert close_call.kwargs["json"] == {"state": "closed"}
    connect_timeout, read_timeout = close_call.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
    assert close_call.kwargs["stream"] is True
    http.patch.return_value.close.assert_called_once_with()
    assert http.delete.call_count == 2
    for delete_call in http.delete.call_args_list:
        assert delete_call.args[0].endswith(f"/branches/{new_branch}")
        connect_timeout, read_timeout = delete_call.kwargs["timeout"]
        assert connect_timeout > 0
        assert read_timeout > 0
        assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
        assert delete_call.kwargs["stream"] is True
    failed_delete.close.assert_called_once_with()
    successful_fallback_delete.raise_for_status.assert_called_once_with()
    successful_fallback_delete.close.assert_called_once_with()


def test_gitea_e2e_recovers_transient_normal_pull_cleanup(monkeypatch):
    """Let finalization recover a transient normal-path pull-request close failure."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    results = MagicMock(return_value=[])
    monkeypatch.setattr(gitea_e2e, "_missing_gitea_tool_results", results)
    pr_response = _streaming_json_response({"number": 123})
    http.post.side_effect = [MagicMock(), pr_response]
    http.get.side_effect = [file_response]
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    cleanup_failure = Timeout("pull close response lost")
    close_calls = []

    def close_helper(*args, **kwargs):
        close_calls.append((args, kwargs))
        if len(close_calls) == 1:
            raise cleanup_failure

    monkeypatch.setattr(gitea_e2e, "_close_pull_request_with_retry", close_helper)

    gitea_e2e.test_e2e_run_gitea_app()

    assert len(close_calls) == 2
    assert close_calls[0][1]["suppress_errors"] is False
    assert close_calls[1][1]["suppress_errors"] is False
    http.delete.assert_called_once()


def test_gitea_e2e_propagates_branch_failure_after_pull_cleanup_recovery(monkeypatch):
    """Fail the run if branch cleanup is the only failure after PR-close recovery succeeds."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    monkeypatch.setattr(gitea_e2e, "_missing_gitea_tool_results", MagicMock(return_value=[]))
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)
    pr_response = _streaming_json_response({"number": 123})
    http.post.side_effect = [MagicMock(), pr_response]
    http.get.side_effect = [file_response]

    close_failure = Timeout("pull close response lost")
    close_helper = MagicMock(side_effect=[close_failure, None])
    branch_failure = RuntimeError("branch cleanup failed after pull recovery")
    delete_helper = MagicMock(side_effect=branch_failure)
    monkeypatch.setattr(gitea_e2e, "_close_pull_request_with_retry", close_helper)
    monkeypatch.setattr(gitea_e2e, "_delete_branch_with_retry", delete_helper)

    with pytest.raises(RuntimeError) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is branch_failure
    assert close_helper.call_count == 2
    delete_helper.assert_called_once()
    assert delete_helper.call_args.kwargs["suppress_errors"] is False


def test_gitea_e2e_recovers_retryable_normal_branch_cleanup(monkeypatch):
    """Let finalization recover a retryable normal-path branch deletion failure."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    results = MagicMock(return_value=[])
    monkeypatch.setattr(gitea_e2e, "_missing_gitea_tool_results", results)
    pr_response = _streaming_json_response({"number": 123})
    http.post.side_effect = [MagicMock(), pr_response]
    http.get.side_effect = [file_response]
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    rate_limited_response = Response()
    rate_limited_response.status_code = 429
    cleanup_failure = HTTPError("branch delete rate limited", response=rate_limited_response)
    cleanup_calls = []

    def delete_helper(*args, **kwargs):
        cleanup_calls.append((args, kwargs))
        if len(cleanup_calls) == 1:
            raise cleanup_failure

    monkeypatch.setattr(gitea_e2e, "_delete_branch_with_retry", delete_helper)

    gitea_e2e.test_e2e_run_gitea_app()

    assert len(cleanup_calls) == 2
    first_args, first_kwargs = cleanup_calls[0]
    second_args, second_kwargs = cleanup_calls[1]
    assert first_args[3] == second_args[3]
    assert first_kwargs["branch_creation_uncertain"] is False
    assert first_kwargs["suppress_errors"] is False
    assert second_kwargs["suppress_errors"] is False
    assert first_kwargs["delete_outcome_state"] is second_kwargs["delete_outcome_state"]
    assert second_kwargs["delete_outcome_state"]["uncertain"] is False
    http.patch.assert_called_once()


def test_gitea_e2e_recovers_ambiguous_normal_branch_cleanup(monkeypatch):
    """Let finalization confirm an ambiguous normal-path branch deletion."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    results = MagicMock(return_value=[])
    monkeypatch.setattr(gitea_e2e, "_missing_gitea_tool_results", results)
    pr_response = _streaming_json_response({"number": 123})
    http.post.side_effect = [MagicMock(), pr_response]
    http.get.side_effect = [file_response]
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    cleanup_failure = Timeout("branch delete response lost")
    cleanup_calls = []

    def delete_helper(*args, **kwargs):
        cleanup_calls.append((args, kwargs))
        if len(cleanup_calls) == 1:
            kwargs["delete_outcome_state"]["uncertain"] = True
            raise cleanup_failure

    monkeypatch.setattr(gitea_e2e, "_delete_branch_with_retry", delete_helper)

    gitea_e2e.test_e2e_run_gitea_app()

    assert len(cleanup_calls) == 2
    first_args, first_kwargs = cleanup_calls[0]
    second_args, second_kwargs = cleanup_calls[1]
    assert first_args[3] == second_args[3]
    assert first_kwargs["branch_creation_uncertain"] is False
    assert first_kwargs["suppress_errors"] is False
    assert second_kwargs["suppress_errors"] is False
    assert first_kwargs["delete_outcome_state"] is second_kwargs["delete_outcome_state"]
    assert second_kwargs["delete_outcome_state"]["uncertain"] is True
    http.patch.assert_called_once()


def _pull_request(number, base_branch="main", head_branch="run-branch"):
    """Build a pull-request list item for uncertain-creation recovery tests."""
    return {
        "number": number,
        "base": {"ref": base_branch},
        "head": {"ref": head_branch},
    }


def test_gitea_e2e_uses_created_pull_number_without_lookup(monkeypatch):
    """Use the successful creation response without an extra pull-list lookup."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    pr_response = _streaming_json_response({"number": 123})
    http.post.side_effect = [MagicMock(), pr_response]
    http.get.side_effect = [file_response]
    monkeypatch.setattr(gitea_e2e, "NUM_MINUTES", 0)

    with pytest.raises(AssertionError):
        gitea_e2e.test_e2e_run_gitea_app()

    assert len(http.get.call_args_list) == 1
    pr_response.iter_content.assert_called_once_with(chunk_size=8192)
    pr_response.close.assert_called_once_with()
    http.patch.assert_called_once()
    assert http.patch.call_args.args[0].endswith("/pulls/123")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"number": None},
        {"number": "123"},
        [],
    ],
)
def test_gitea_e2e_invalid_creation_body_keeps_recovery_enabled(monkeypatch, payload):
    """Recover an already-created PR when a successful response has an unusable schema."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    invalid_creation = _streaming_json_response(payload)
    recovered_page = _streaming_json_response(
        lambda: [_pull_request(123, head_branch=http.post.call_args_list[0].kwargs["json"]["new_branch_name"])]
    )
    http.post.side_effect = [MagicMock(), invalid_creation]
    http.get.side_effect = [file_response, recovered_page]
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    with pytest.raises(gitea_e2e._TransientGiteaResponseError):
        gitea_e2e.test_e2e_run_gitea_app()

    invalid_creation.close.assert_called_once_with()
    http.patch.assert_called_once()
    assert http.patch.call_args.args[0].endswith("/pulls/123")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [None],
        [{"number": None, "base": {"ref": "main"}, "head": {"ref": "run-branch"}}],
        [{"number": 123, "base": None, "head": {"ref": "run-branch"}}],
    ],
)
def test_gitea_e2e_uncertain_lookup_retries_invalid_schema(monkeypatch, payload):
    """Retry a syntactically valid pull-list body whose JSON shape is unusable."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    invalid_page = _streaming_json_response(payload)
    recovered_page = _streaming_json_response(
        lambda: [_pull_request(456, head_branch=http.post.call_args_list[0].kwargs["json"]["new_branch_name"])]
    )
    creation_failure = Timeout("pull request creation response lost")
    http.post.side_effect = [MagicMock(), creation_failure]
    http.get.side_effect = [file_response, invalid_page, recovered_page]
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    with pytest.raises(Timeout) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is creation_failure
    assert len(http.get.call_args_list) == 3
    invalid_page.close.assert_called_once_with()
    recovered_page.close.assert_called_once_with()
    http.patch.assert_called_once()
    assert http.patch.call_args.args[0].endswith("/pulls/456")


def test_gitea_e2e_recovers_pull_request_after_lost_creation_response(monkeypatch):
    """Resolve and close a pull request whose creation response was lost."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    empty_page = _streaming_json_response([])
    recovered_page = _streaming_json_response(
        lambda: [_pull_request(123, head_branch=http.post.call_args_list[0].kwargs["json"]["new_branch_name"])]
    )

    identifier = UUID(int=3)
    monkeypatch.setattr(gitea_e2e, "uuid4", lambda: identifier)
    new_branch = f"gitea_app_e2e_test-{identifier.hex}"
    creation_failure = Timeout("pull request creation response lost")
    http.post.side_effect = [MagicMock(), creation_failure]
    http.get.side_effect = [file_response, empty_page, recovered_page]
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    with pytest.raises(Timeout) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is creation_failure
    _assert_native_create_contract(http)

    pr_create_call = http.post.call_args_list[1]
    assert pr_create_call.args[0].endswith("/pulls")
    connect_timeout, read_timeout = pr_create_call.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS

    recovery_calls = http.get.call_args_list[1:]
    assert len(recovery_calls) == 2
    for recovery_call in recovery_calls:
        assert recovery_call.args[0].endswith("/pulls")
        assert recovery_call.kwargs["params"] == {
            "state": "open",
            "base_branch": "main",
            "page": 1,
            "limit": 50,
        }
        connect_timeout, read_timeout = recovery_call.kwargs["timeout"]
        assert connect_timeout > 0
        assert read_timeout > 0
        assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
        assert recovery_call.kwargs["stream"] is True

    http.patch.assert_called_once()
    close_call = http.patch.call_args
    assert close_call.args[0] == "https://gitea.example.test/api/v1/repos/codiumai/pr-agent-tests/pulls/123"
    assert close_call.kwargs["headers"] == _expected_headers()
    assert close_call.kwargs["json"] == {"state": "closed"}
    assert close_call.kwargs["stream"] is True
    connect_timeout, read_timeout = close_call.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
    http.delete.assert_called_once()
    assert http.delete.call_args.args[0].endswith(f"/branches/{new_branch}")


def test_gitea_e2e_uncertain_pull_lookup_retries_transient_failure(monkeypatch):
    """Retry a transient pull lookup before closing the recovered pull request."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    recovered_page = _streaming_json_response(
        lambda: [_pull_request(456, head_branch=http.post.call_args_list[0].kwargs["json"]["new_branch_name"])]
    )

    identifier = UUID(int=4)
    monkeypatch.setattr(gitea_e2e, "uuid4", lambda: identifier)
    creation_failure = Timeout("pull request creation response lost")
    lookup_failure = Timeout("lookup timed out")
    http.post.side_effect = [MagicMock(), creation_failure]
    http.get.side_effect = [file_response, lookup_failure, recovered_page]
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    with pytest.raises(Timeout) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is creation_failure
    assert len(http.get.call_args_list) == 3
    http.patch.assert_called_once()
    assert http.patch.call_args.args[0].endswith("/pulls/456")


def test_gitea_e2e_uncertain_pull_lookup_retries_404(monkeypatch):
    """Retry a transient 404 before closing the recovered pull request."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    not_visible = MagicMock()
    not_visible.status_code = 404
    lookup_failure = HTTPError("pull request not visible yet", response=not_visible)
    not_visible.raise_for_status.side_effect = lookup_failure
    recovered_page = _streaming_json_response(
        lambda: [_pull_request(457, head_branch=http.post.call_args_list[0].kwargs["json"]["new_branch_name"])]
    )

    identifier = UUID(int=7)
    monkeypatch.setattr(gitea_e2e, "uuid4", lambda: identifier)
    creation_failure = Timeout("pull request creation response lost")
    http.post.side_effect = [MagicMock(), creation_failure]
    http.get.side_effect = [file_response, not_visible, recovered_page]
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    with pytest.raises(Timeout) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is creation_failure
    assert len(http.get.call_args_list) == 3
    not_visible.close.assert_called_once_with()
    recovered_page.close.assert_called_once_with()
    http.patch.assert_called_once()
    assert http.patch.call_args.args[0].endswith("/pulls/457")


def test_gitea_e2e_uncertain_pull_lookup_handles_pagination(monkeypatch):
    """Find a matching pull request beyond the first filtered result page."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    first_page = _streaming_json_response([_pull_request(i, head_branch=f"other-{i}") for i in range(1, 4)])
    second_page = _streaming_json_response(
        lambda: [_pull_request(789, head_branch=http.post.call_args_list[0].kwargs["json"]["new_branch_name"])]
    )

    identifier = UUID(int=5)
    monkeypatch.setattr(gitea_e2e, "uuid4", lambda: identifier)
    creation_failure = Timeout("pull request creation response lost")
    http.post.side_effect = [MagicMock(), creation_failure]
    http.get.side_effect = [file_response, first_page, second_page]

    with pytest.raises(Timeout):
        gitea_e2e.test_e2e_run_gitea_app()

    assert http.get.call_args_list[1].kwargs["params"]["page"] == 1
    assert http.get.call_args_list[2].kwargs["params"]["page"] == 2
    http.patch.assert_called_once()
    assert http.patch.call_args.args[0].endswith("/pulls/789")


@pytest.mark.parametrize("failure_kind", ["chunked", "content_decoding", "json", "unicode"])
def test_gitea_e2e_uncertain_pull_lookup_retries_body_failure(monkeypatch, failure_kind):
    """Retry transient response-body failures while resolving an uncertain pull request."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}

    identifier = UUID(int=6)
    monkeypatch.setattr(gitea_e2e, "uuid4", lambda: identifier)
    new_branch = f"gitea_app_e2e_test-{identifier.hex}"
    failed_lookup = MagicMock()
    if failure_kind == "chunked":
        failed_lookup.iter_content.side_effect = ChunkedEncodingError("truncated response")
    elif failure_kind == "content_decoding":
        failed_lookup.iter_content.side_effect = ContentDecodingError("compressed response decoding failed")
    elif failure_kind == "json":
        failed_lookup.iter_content.return_value = [b"{"]
    else:
        failed_lookup.iter_content.return_value = [b"\xff"]

    recovered_page = _streaming_json_response([_pull_request(654, head_branch=new_branch)])
    creation_failure = Timeout("pull request creation response lost")
    http.post.side_effect = [MagicMock(), creation_failure]
    http.get.side_effect = [file_response, failed_lookup, recovered_page]
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    with pytest.raises(Timeout) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is creation_failure
    assert len(http.get.call_args_list) == 3
    failed_lookup.close.assert_called_once_with()
    recovered_page.close.assert_called_once_with()
    http.patch.assert_called_once()
    assert http.patch.call_args.args[0].endswith("/pulls/654")


def test_gitea_e2e_expired_body_deadline_closes_response(monkeypatch):
    """Close a successful response if its body deadline expired before reading starts."""
    gitea_e2e, _, _ = _setup_gitea_case(monkeypatch)
    response = MagicMock()
    monkeypatch.setattr(gitea_e2e, "monotonic", lambda: 31)

    with pytest.raises(Timeout, match="Timed out while reading Gitea pull-request creation response"):
        gitea_e2e._read_json_response_with_deadline(
            response,
            30,
            "Timed out while reading Gitea pull-request creation response",
        )

    response.close.assert_called_once_with()
    response.iter_content.assert_not_called()


def test_gitea_e2e_late_reader_skips_response_after_caller_timeout(monkeypatch):
    """Do not let a late reader reclaim a response after caller-side timeout cleanup."""
    gitea_e2e, _, _ = _setup_gitea_case(monkeypatch)
    response = MagicMock()
    captured = {}

    def timeout_before_reader(operation, deadline, timeout_message):
        captured["operation"] = operation
        raise Timeout(timeout_message)

    monkeypatch.setattr(gitea_e2e, "_run_with_deadline", timeout_before_reader)

    with pytest.raises(Timeout, match="Timed out while reading Gitea pull-request creation response"):
        gitea_e2e._read_json_response_with_deadline(
            response,
            30,
            "Timed out while reading Gitea pull-request creation response",
        )

    response.close.assert_called_once_with()
    response.iter_content.assert_not_called()

    assert captured["operation"]() is None
    response.close.assert_called_once_with()
    response.iter_content.assert_not_called()


def test_gitea_e2e_timeout_does_not_close_reader_owned_response(monkeypatch):
    """Return on timeout without closing a response already owned by the reader worker."""
    gitea_e2e, _, _ = _setup_gitea_case(monkeypatch)
    response = MagicMock()
    reader_claimed = Event()
    release_reader = Event()
    worker_done = Event()
    captured = {}

    def blocking_load(response_arg, deadline, timeout_message):
        assert response_arg is response
        reader_claimed.set()
        assert release_reader.wait(1)
        return {}

    def timeout_after_reader_claims(operation, deadline, timeout_message):
        def run_operation():
            try:
                operation()
            finally:
                worker_done.set()

        worker = Thread(target=run_operation, daemon=True)
        captured["worker"] = worker
        worker.start()
        assert reader_claimed.wait(1)
        raise Timeout(timeout_message)

    monkeypatch.setattr(gitea_e2e, "_load_json_response", blocking_load)
    monkeypatch.setattr(gitea_e2e, "_run_with_deadline", timeout_after_reader_claims)

    with pytest.raises(Timeout, match="Timed out while reading Gitea pull-request creation response"):
        gitea_e2e._read_json_response_with_deadline(
            response,
            30,
            "Timed out while reading Gitea pull-request creation response",
        )

    response.close.assert_not_called()
    assert not worker_done.is_set()
    assert captured["worker"].is_alive()

    release_reader.set()
    captured["worker"].join(timeout=1)
    assert worker_done.is_set()
    assert not captured["worker"].is_alive()
    response.close.assert_called_once_with()


def test_gitea_e2e_stream_reader_stops_at_absolute_deadline(monkeypatch):
    """Stop a continuously progressing response body once its absolute deadline is reached."""
    gitea_e2e, _, _ = _setup_gitea_case(monkeypatch)
    response = MagicMock()
    response.iter_content.return_value = [b"{", b"}"]
    monotonic = MagicMock(side_effect=[29.5, 30.0])
    monkeypatch.setattr(gitea_e2e, "monotonic", monotonic)

    with pytest.raises(Timeout, match="Timed out while reading Gitea pull-request creation response"):
        gitea_e2e._load_json_response(
            response,
            30,
            "Timed out while reading Gitea pull-request creation response",
        )

    response.iter_content.assert_called_once_with(chunk_size=8192)


def test_gitea_e2e_json_reader_rejects_oversized_body(monkeypatch):
    """Reject an unexpectedly large streamed JSON body and close the response."""
    gitea_e2e, _, _ = _setup_gitea_case(monkeypatch)
    response = MagicMock()
    response.iter_content.return_value = [b"{}", b"xx"]
    monkeypatch.setattr(gitea_e2e, "_MAX_GITEA_JSON_RESPONSE_BYTES", 3)
    monkeypatch.setattr(gitea_e2e, "monotonic", lambda: 0)
    monkeypatch.setattr(
        gitea_e2e,
        "_run_with_deadline",
        lambda operation, deadline, timeout_message: operation(),
    )

    with pytest.raises(gitea_e2e._TransientGiteaResponseError, match="maximum allowed size"):
        gitea_e2e._read_json_response_with_deadline(
            response,
            30,
            "Timed out while reading Gitea pull-request creation response",
        )

    response.close.assert_called_once_with()


def test_gitea_e2e_deadline_stops_waiting_for_blocked_operation(monkeypatch):
    """Stop waiting at the deadline without claiming the daemon worker was cancelled."""
    gitea_e2e, _, _ = _setup_gitea_case(monkeypatch)
    done = MagicMock()
    done.wait.return_value = False
    worker = MagicMock()
    thread_factory = MagicMock(return_value=worker)
    operation = MagicMock()

    monkeypatch.setattr(gitea_e2e, "monotonic", lambda: 0)
    monkeypatch.setattr(gitea_e2e, "Event", lambda: done)
    monkeypatch.setattr(gitea_e2e, "Thread", thread_factory)

    with pytest.raises(Timeout, match="Timed out while resolving Gitea pull request"):
        gitea_e2e._run_with_deadline(
            operation,
            30,
            "Timed out while resolving Gitea pull request",
        )

    thread_factory.assert_called_once()
    assert thread_factory.call_args.kwargs["daemon"] is True
    worker.start.assert_called_once_with()
    done.wait.assert_called_once_with(30)


def test_gitea_e2e_deadline_rechecks_clock_after_worker_completion(monkeypatch):
    """Reject a worker result when caller resumption occurs at or after the absolute deadline."""
    gitea_e2e, _, _ = _setup_gitea_case(monkeypatch)
    done = MagicMock()
    done.wait.return_value = True
    worker = MagicMock()
    thread_factory = MagicMock(return_value=worker)
    monotonic = MagicMock(side_effect=[0, 30])

    monkeypatch.setattr(gitea_e2e, "monotonic", monotonic)
    monkeypatch.setattr(gitea_e2e, "Event", lambda: done)
    monkeypatch.setattr(gitea_e2e, "Thread", thread_factory)

    with pytest.raises(Timeout, match="Timed out while resolving Gitea pull request"):
        gitea_e2e._run_with_deadline(
            MagicMock(),
            30,
            "Timed out while resolving Gitea pull request",
        )

    done.wait.assert_called_once_with(30)
    worker.start.assert_called_once_with()


def test_gitea_e2e_retries_transient_pull_cleanup_failure(monkeypatch):
    """Retry a transient fallback PR-close failure within the cleanup window."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    pr_response = _streaming_json_response({"number": 321})
    http.get.side_effect = [file_response]
    http.post.side_effect = [MagicMock(), pr_response]
    monkeypatch.setattr(gitea_e2e, "NUM_MINUTES", 0)
    monkeypatch.setattr(gitea_e2e.time, "sleep", lambda _: None)

    transient = Timeout("PR cleanup timed out")
    successful_close = MagicMock()
    http.patch.side_effect = [transient, successful_close]

    with pytest.raises(AssertionError):
        gitea_e2e.test_e2e_run_gitea_app()

    assert http.patch.call_count == 2
    successful_close.raise_for_status.assert_called_once_with()
    successful_close.close.assert_called_once_with()


def test_gitea_e2e_finalizer_keeps_cleanup_steps_independent(monkeypatch):
    """Attempt branch cleanup even if an unexpected PR-cleanup helper error escapes."""
    gitea_e2e, http, test_logger = _setup_gitea_case(monkeypatch)
    file_response = MagicMock()
    file_response.status_code = 200
    file_response.json.return_value = {"sha": "file-sha"}
    pr_response = _streaming_json_response({"number": 123})
    http.post.side_effect = [MagicMock(), pr_response]
    http.get.side_effect = [file_response]
    monkeypatch.setattr(gitea_e2e, "NUM_MINUTES", 0)

    close_failure = RuntimeError("unexpected close helper failure")
    close_helper = MagicMock(side_effect=close_failure)
    delete_helper = MagicMock()
    monkeypatch.setattr(gitea_e2e, "_close_pull_request_with_retry", close_helper)
    monkeypatch.setattr(gitea_e2e, "_delete_branch_with_retry", delete_helper)

    with pytest.raises(AssertionError):
        gitea_e2e.test_e2e_run_gitea_app()

    close_helper.assert_called_once()
    delete_helper.assert_called_once()
    _assert_logged_error(test_logger, f"Failed to clean up after test: {close_failure}")


def test_gitea_e2e_import_does_not_configure_logging(monkeypatch):
    """Keep a fresh E2E module import from reconfiguring process-wide logging."""
    import pr_agent.log as log_module
    from tests.e2e_tests import test_gitea_app as gitea_e2e

    setup_logger = MagicMock()
    monkeypatch.setattr(log_module, "setup_logger", setup_logger)
    spec = importlib.util.spec_from_file_location("_gitea_e2e_import_probe", gitea_e2e.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    setup_logger.assert_not_called()


def test_gitea_e2e_settings_read_failure_preserves_original_error(monkeypatch):
    """Preserve a settings-read failure that occurs before cleanup state is initialized."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    failure = RuntimeError("settings read failed")
    settings = SimpleNamespace(
        config=SimpleNamespace(git_provider=None),
        get=MagicMock(side_effect=failure),
    )
    monkeypatch.setattr(gitea_e2e, "get_settings", lambda: settings)

    with pytest.raises(RuntimeError) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is failure
    assert http.mock_calls == []


@pytest.mark.parametrize("error_kind", ["timeout", "connection", "http_408", "http_429", "http_500"])
def test_gitea_e2e_uncertain_branch_creation_attempts_cleanup(monkeypatch, error_kind):
    """Clean up a UUID branch when its creation may have succeeded without a response."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    if error_kind.startswith("http_"):
        status = int(error_kind.removeprefix("http_"))
        response = Response()
        response.status_code = status
        failure = HTTPError(f"branch creation returned HTTP {status}", response=response)
        http.post.return_value.raise_for_status.side_effect = failure
    else:
        failure = Timeout("creation timed out") if error_kind == "timeout" else ConnectionError("response lost")
        http.post.side_effect = failure

    with pytest.raises(type(failure)) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is failure
    new_branch = _assert_native_create_contract(http)
    if error_kind.startswith("http_"):
        connect_timeout, read_timeout = http.post.call_args.kwargs["timeout"]
        assert connect_timeout > 0
        assert read_timeout > 0
        assert connect_timeout + read_timeout <= gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
        assert http.post.call_args.kwargs["stream"] is True
        http.post.return_value.close.assert_called_once_with()
    http.patch.assert_not_called()
    http.delete.assert_called_once()
    delete_call = http.delete.call_args
    assert delete_call.args[0].endswith(f"/branches/{new_branch}")
    connect_timeout, read_timeout = delete_call.kwargs["timeout"]
    assert connect_timeout > 0
    assert read_timeout > 0
    assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
    assert delete_call.kwargs["stream"] is True
    http.delete.return_value.raise_for_status.assert_called_once_with()
    http.delete.return_value.close.assert_called_once_with()


def test_gitea_e2e_branch_names_use_fresh_uuid_per_run(monkeypatch):
    """Use a fresh full UUID for every branch creation attempt."""
    gitea_e2e, http, _ = _setup_gitea_case(monkeypatch)
    identifiers = [UUID(int=1), UUID(int=2)]
    uuid_factory = MagicMock(side_effect=identifiers)
    monkeypatch.setattr(gitea_e2e, "uuid4", uuid_factory)
    http.post.side_effect = Timeout("response lost")

    for _ in identifiers:
        with pytest.raises(Timeout, match="response lost"):
            gitea_e2e.test_e2e_run_gitea_app()

    expected_names = [f"gitea_app_e2e_test-{identifier.hex}" for identifier in identifiers]
    assert uuid_factory.call_count == 2
    assert [call.kwargs["json"]["new_branch_name"] for call in http.post.call_args_list] == expected_names
    actual_names = [call.args[0].rsplit("/branches/", 1)[1] for call in http.delete.call_args_list]
    assert actual_names == expected_names
