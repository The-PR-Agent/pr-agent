from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from loguru import logger as loguru_logger
from requests import Response
from requests.exceptions import ConnectionError, HTTPError, Timeout

from tests.e2e_tests import test_gitea_app as gitea_e2e


class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _http_error_response(status):
    response = Response()
    response.status_code = status
    error = HTTPError(f"branch cleanup returned HTTP {status}", response=response)
    result = MagicMock()
    result.raise_for_status.side_effect = error
    return result, error


def _logged_errors(logger):
    return "\n".join(call.args[0] for call in logger.error.call_args_list if call.args)


def _assert_logged_error(logger, message):
    assert message in _logged_errors(logger)


def _cleanup_context(monkeypatch, delete):
    clock = _FakeClock()
    logger = MagicMock()
    monkeypatch.setattr(gitea_e2e, "requests", SimpleNamespace(delete=delete))
    monkeypatch.setattr(gitea_e2e, "time", SimpleNamespace(sleep=clock.sleep))
    monkeypatch.setattr(gitea_e2e, "monotonic", clock.monotonic)
    return clock, logger


def _pull_cleanup_context(monkeypatch, patch):
    clock = _FakeClock()
    logger = MagicMock()
    monkeypatch.setattr(gitea_e2e, "requests", SimpleNamespace(patch=patch))
    monkeypatch.setattr(gitea_e2e, "time", SimpleNamespace(sleep=clock.sleep))
    monkeypatch.setattr(gitea_e2e, "monotonic", clock.monotonic)
    return clock, logger


def _run_uncertain_cleanup(logger):
    gitea_e2e._delete_branch_with_retry(
        "https://gitea.example.test",
        "owner",
        "repo",
        "run-specific-branch",
        {"Authorization": "token test"},
        logger,
        True,
    )


@pytest.mark.parametrize(
    "error_kind",
    ["http_404", "http_408", "http_429", "http_500", "timeout", "connection"],
)
def test_uncertain_branch_cleanup_retries_transient_failure(monkeypatch, error_kind):
    successful_delete = MagicMock()
    if error_kind.startswith("http_"):
        status = int(error_kind.removeprefix("http_"))
        first_delete, _ = _http_error_response(status)
        delete = MagicMock(side_effect=[first_delete, successful_delete])
    else:
        failure = Timeout("delete timed out") if error_kind == "timeout" else ConnectionError("response lost")
        delete = MagicMock(side_effect=[failure, successful_delete])

    clock, logger = _cleanup_context(monkeypatch, delete)
    _run_uncertain_cleanup(logger)

    assert delete.call_count == 2
    for call in delete.call_args_list:
        assert call.args[0].endswith("/branches/run-specific-branch")
        connect_timeout, read_timeout = call.kwargs["timeout"]
        assert connect_timeout > 0
        assert read_timeout > 0
        assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
        assert call.kwargs["stream"] is True
    successful_delete.raise_for_status.assert_called_once_with()
    successful_delete.close.assert_called_once_with()
    assert clock.sleeps == [gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS]
    logger.exception.assert_not_called()


def test_uncertain_branch_cleanup_retry_is_bounded_by_shared_deadline(monkeypatch):
    not_found, cleanup_error = _http_error_response(404)
    delete = MagicMock(return_value=not_found)
    clock, logger = _cleanup_context(monkeypatch, delete)

    _run_uncertain_cleanup(logger)

    assert clock.now < gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_WINDOW_SECONDS
    assert len(clock.sleeps) > 1
    assert all(seconds <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS for seconds in clock.sleeps)
    logger.exception.assert_not_called()
    logger.opt.assert_not_called()
    _assert_logged_error(logger, f"Failed to clean up after test: {cleanup_error}")
    assert "Traceback (most recent call last)" in _logged_errors(logger)


def test_uncertain_branch_cleanup_request_time_consumes_deadline(monkeypatch):
    clock = _FakeClock()
    logger = MagicMock()
    failure = Timeout("delete timed out")
    request_timeouts = []

    def delete(*args, **kwargs):
        request_timeouts.append(kwargs["timeout"])
        clock.now += sum(kwargs["timeout"])
        raise failure

    monkeypatch.setattr(gitea_e2e, "requests", SimpleNamespace(delete=delete))
    monkeypatch.setattr(gitea_e2e, "time", SimpleNamespace(sleep=clock.sleep))
    monkeypatch.setattr(gitea_e2e, "monotonic", clock.monotonic)

    _run_uncertain_cleanup(logger)

    assert clock.now == gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_WINDOW_SECONDS
    assert len(request_timeouts) > 1
    assert len(clock.sleeps) > 0
    for timeout in request_timeouts:
        assert sum(timeout) <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
    assert all(seconds <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS for seconds in clock.sleeps)
    logger.exception.assert_not_called()
    logger.opt.assert_not_called()
    _assert_logged_error(logger, "Failed to clean up after test: Timed out while deleting Gitea branch")
    assert "Traceback (most recent call last)" in _logged_errors(logger)


def test_uncertain_branch_cleanup_logs_last_failure_when_sleep_overshoots_deadline(monkeypatch):
    not_found, cleanup_error = _http_error_response(404)
    delete = MagicMock(return_value=not_found)
    clock, logger = _cleanup_context(monkeypatch, delete)

    def oversleep(seconds):
        clock.sleeps.append(seconds)
        clock.now = gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_WINDOW_SECONDS + 1

    monkeypatch.setattr(gitea_e2e, "time", SimpleNamespace(sleep=oversleep))

    _run_uncertain_cleanup(logger)

    delete.assert_called_once()
    assert clock.sleeps == [gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS]
    logger.exception.assert_not_called()
    logger.opt.assert_not_called()
    _assert_logged_error(logger, f"Failed to clean up after test: {cleanup_error}")
    assert "Traceback (most recent call last)" in _logged_errors(logger)


def test_uncertain_pull_lookup_preserves_last_traceback_after_oversleep(monkeypatch):
    """Preserve the last lookup traceback when retry sleep crosses the deadline."""
    clock = _FakeClock()
    logger = MagicMock()
    failure = Timeout("pull lookup timed out")
    get = MagicMock(side_effect=failure)

    def oversleep(seconds):
        clock.sleeps.append(seconds)
        clock.now = gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS + 1

    monkeypatch.setattr(gitea_e2e, "requests", SimpleNamespace(get=get))
    monkeypatch.setattr(gitea_e2e, "time", SimpleNamespace(sleep=oversleep))
    monkeypatch.setattr(gitea_e2e, "monotonic", clock.monotonic)

    result = gitea_e2e._resolve_uncertain_pull_request(
        "https://gitea.example.test",
        "owner",
        "repo",
        "main",
        "run-specific-branch",
        {"Authorization": "token test"},
        logger,
    )

    assert result is None
    get.assert_called_once()
    assert clock.sleeps == [gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS]
    logger.exception.assert_not_called()
    logger.opt.assert_not_called()
    _assert_logged_error(logger, f"Failed to resolve uncertain pull request during cleanup: {failure}")
    assert "Traceback (most recent call last)" in _logged_errors(logger)


def test_cleanup_traceback_does_not_expose_gitea_token(monkeypatch):
    """Keep Authorization values out of cleanup tracebacks even with Loguru diagnostics enabled."""
    synthetic_token = "SYNTHETIC_GITEA_TOKEN_SHOULD_NOT_APPEAR"
    delete = MagicMock(side_effect=RuntimeError("branch cleanup failed"))
    monkeypatch.setattr(gitea_e2e, "requests", SimpleNamespace(delete=delete))

    output = StringIO()
    sink_id = loguru_logger.add(
        output,
        format="{message}\n{exception}",
        diagnose=True,
        backtrace=True,
    )
    try:
        gitea_e2e._delete_branch_with_retry(
            "https://gitea.example.test",
            "owner",
            "repo",
            "run-specific-branch",
            {"Authorization": f"token {synthetic_token}"},
            loguru_logger,
            False,
        )
    finally:
        loguru_logger.remove(sink_id)

    logs = output.getvalue()
    assert "branch cleanup failed" in logs
    assert "Traceback (most recent call last)" in logs
    assert synthetic_token not in logs


def test_pull_lookup_deadline_without_prior_error_is_logged():
    """Report deadline exhaustion even when pagination never raised an earlier error."""
    logger = MagicMock()

    gitea_e2e._log_last_pull_lookup_error(logger, None, None)

    _assert_logged_error(
        logger,
        "Failed to resolve uncertain pull request during cleanup: Timed out while resolving Gitea pull request",
    )


@pytest.mark.parametrize("helper_kind", ["branch", "pull"])
def test_cleanup_retry_delay_never_becomes_negative_at_final_budget(monkeypatch, helper_kind):
    """Reuse one remaining-time sample so a clock tick cannot make sleep negative."""
    logger = MagicMock()
    transient = Timeout("transient cleanup timeout")
    runner = MagicMock(side_effect=[transient, None])
    monotonic = MagicMock(side_effect=[0.0, 0.0, 28.999, 29.001])
    sleeps = []

    def sleep(seconds):
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        sleeps.append(seconds)

    monkeypatch.setattr(gitea_e2e, "_run_with_deadline", runner)
    monkeypatch.setattr(gitea_e2e, "monotonic", monotonic)
    monkeypatch.setattr(gitea_e2e.time, "sleep", sleep)

    if helper_kind == "branch":
        gitea_e2e._delete_branch_with_retry(
            "https://gitea.example.test",
            "owner",
            "repo",
            "run-specific-branch",
            {"Authorization": "token test"},
            logger,
            True,
        )
    else:
        gitea_e2e._close_pull_request_with_retry(
            "https://gitea.example.test",
            "owner",
            "repo",
            123,
            {"Authorization": "token test"},
            logger,
        )

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.001)
    assert sleeps[0] >= 0


def test_pull_cleanup_retries_first_404(monkeypatch):
    """Retry an initial 404 because a known PR can be temporarily invisible."""
    not_found, _ = _http_error_response(404)
    successful_close = MagicMock()
    patch = MagicMock(side_effect=[not_found, successful_close])
    clock, logger = _pull_cleanup_context(monkeypatch, patch)

    gitea_e2e._close_pull_request_with_retry(
        "https://gitea.example.test",
        "owner",
        "repo",
        123,
        {"Authorization": "token test"},
        logger,
        suppress_errors=False,
    )

    assert patch.call_count == 2
    not_found.raise_for_status.assert_called_once_with()
    not_found.close.assert_called_once_with()
    successful_close.raise_for_status.assert_called_once_with()
    successful_close.close.assert_called_once_with()
    assert clock.sleeps == [gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS]
    logger.exception.assert_not_called()


def test_pull_cleanup_retries_404_after_ambiguous_close(monkeypatch):
    """Keep retrying 404 after a lost close response because the PR should still exist."""
    not_found, _ = _http_error_response(404)
    successful_close = MagicMock()
    patch = MagicMock(side_effect=[Timeout("close response lost"), not_found, successful_close])
    clock, logger = _pull_cleanup_context(monkeypatch, patch)

    gitea_e2e._close_pull_request_with_retry(
        "https://gitea.example.test",
        "owner",
        "repo",
        123,
        {"Authorization": "token test"},
        logger,
        suppress_errors=False,
    )

    assert patch.call_count == 3
    not_found.raise_for_status.assert_called_once_with()
    not_found.close.assert_called_once_with()
    successful_close.raise_for_status.assert_called_once_with()
    successful_close.close.assert_called_once_with()
    assert clock.sleeps == [
        gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS,
        gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS,
    ]
    logger.exception.assert_not_called()


def test_pull_cleanup_propagates_persistent_404_on_normal_path(monkeypatch):
    """Do not silently accept a known PR that remains missing for the cleanup window."""
    not_found, cleanup_error = _http_error_response(404)
    patch = MagicMock(return_value=not_found)
    clock, logger = _pull_cleanup_context(monkeypatch, patch)

    with pytest.raises(HTTPError) as caught:
        gitea_e2e._close_pull_request_with_retry(
            "https://gitea.example.test",
            "owner",
            "repo",
            123,
            {"Authorization": "token test"},
            logger,
            suppress_errors=False,
        )

    assert caught.value is cleanup_error
    assert patch.call_count > 1
    assert clock.sleeps
    assert clock.now < gitea_e2e._RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    logger.exception.assert_not_called()


@pytest.mark.parametrize("error_kind", ["http_408", "http_429", "http_503", "timeout", "connection"])
def test_confirmed_branch_cleanup_retries_transient_failure(monkeypatch, error_kind):
    successful_delete = MagicMock()
    if error_kind.startswith("http_"):
        status = int(error_kind.removeprefix("http_"))
        first_delete, _ = _http_error_response(status)
        delete = MagicMock(side_effect=[first_delete, successful_delete])
    else:
        failure = Timeout("delete timed out") if error_kind == "timeout" else ConnectionError("response lost")
        delete = MagicMock(side_effect=[failure, successful_delete])

    clock, logger = _cleanup_context(monkeypatch, delete)
    gitea_e2e._delete_branch_with_retry(
        "https://gitea.example.test",
        "owner",
        "repo",
        "run-specific-branch",
        {"Authorization": "token test"},
        logger,
        False,
    )

    assert delete.call_count == 2
    for call in delete.call_args_list:
        connect_timeout, read_timeout = call.kwargs["timeout"]
        assert connect_timeout > 0
        assert read_timeout > 0
        assert connect_timeout + read_timeout <= gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS
        assert call.kwargs["stream"] is True
    successful_delete.raise_for_status.assert_called_once_with()
    successful_delete.close.assert_called_once_with()
    assert clock.sleeps == [gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS]
    logger.exception.assert_not_called()


@pytest.mark.parametrize("ambiguous_kind", ["timeout", "http_500", "http_502", "http_503", "http_504"])
def test_confirmed_branch_cleanup_accepts_404_after_ambiguous_delete(monkeypatch, ambiguous_kind):
    """Treat 404 as success only after a confirmed branch DELETE had an unknown outcome."""
    not_found, _ = _http_error_response(404)
    if ambiguous_kind == "timeout":
        first_delete = None
        delete = MagicMock(side_effect=[Timeout("delete response lost"), not_found])
    else:
        status = int(ambiguous_kind.removeprefix("http_"))
        first_delete, _ = _http_error_response(status)
        delete = MagicMock(side_effect=[first_delete, not_found])
    clock, logger = _cleanup_context(monkeypatch, delete)

    gitea_e2e._delete_branch_with_retry(
        "https://gitea.example.test",
        "owner",
        "repo",
        "run-specific-branch",
        {"Authorization": "token test"},
        logger,
        False,
        suppress_errors=False,
    )

    assert delete.call_count == 2
    if first_delete is not None:
        first_delete.raise_for_status.assert_called_once_with()
        first_delete.close.assert_called_once_with()
    not_found.raise_for_status.assert_called_once_with()
    not_found.close.assert_called_once_with()
    assert clock.sleeps == [gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS]
    logger.exception.assert_not_called()


def test_confirmed_branch_cleanup_accepts_404_from_prior_ambiguous_delete(monkeypatch):
    """Carry an ambiguous DELETE outcome across cleanup helper invocations."""
    not_found, _ = _http_error_response(404)
    delete = MagicMock(return_value=not_found)
    clock, logger = _cleanup_context(monkeypatch, delete)

    gitea_e2e._delete_branch_with_retry(
        "https://gitea.example.test",
        "owner",
        "repo",
        "run-specific-branch",
        {"Authorization": "token test"},
        logger,
        False,
        suppress_errors=False,
        delete_outcome_state={"uncertain": True},
    )

    delete.assert_called_once()
    not_found.raise_for_status.assert_called_once_with()
    not_found.close.assert_called_once_with()
    assert clock.sleeps == []
    logger.error.assert_not_called()


def test_confirmed_branch_cleanup_preserves_earlier_ambiguity_across_windows(monkeypatch):
    """Keep an earlier ambiguous DELETE outcome when a later retry ends with 429."""
    server_error, _ = _http_error_response(502)
    rate_limited, rate_limit_error = _http_error_response(429)
    not_found, _ = _http_error_response(404)
    delete = MagicMock(side_effect=[server_error, rate_limited])
    clock, logger = _cleanup_context(monkeypatch, delete)
    delete_state = {"uncertain": False}

    def near_deadline_sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now = gitea_e2e._UNCERTAIN_BRANCH_CLEANUP_WINDOW_SECONDS - 0.5

    monkeypatch.setattr(gitea_e2e, "time", SimpleNamespace(sleep=near_deadline_sleep))

    with pytest.raises(HTTPError) as caught:
        gitea_e2e._delete_branch_with_retry(
            "https://gitea.example.test",
            "owner",
            "repo",
            "run-specific-branch",
            {"Authorization": "token test"},
            logger,
            False,
            suppress_errors=False,
            delete_outcome_state=delete_state,
        )

    assert caught.value is rate_limit_error
    assert delete_state["uncertain"] is True

    delete.reset_mock()
    delete.side_effect = None
    delete.return_value = not_found
    clock.now = 0.0

    gitea_e2e._delete_branch_with_retry(
        "https://gitea.example.test",
        "owner",
        "repo",
        "run-specific-branch",
        {"Authorization": "token test"},
        logger,
        False,
        suppress_errors=False,
        delete_outcome_state=delete_state,
    )

    delete.assert_called_once()
    not_found.raise_for_status.assert_called_once_with()
    not_found.close.assert_called_once_with()


def test_confirmed_branch_cleanup_does_not_retry_missing_branch(monkeypatch):
    not_found, cleanup_error = _http_error_response(404)
    delete = MagicMock(return_value=not_found)
    clock, logger = _cleanup_context(monkeypatch, delete)

    gitea_e2e._delete_branch_with_retry(
        "https://gitea.example.test",
        "owner",
        "repo",
        "run-specific-branch",
        {"Authorization": "token test"},
        logger,
        False,
    )

    delete.assert_called_once()
    not_found.close.assert_called_once_with()
    assert clock.sleeps == []
    logger.exception.assert_not_called()
    logger.opt.assert_not_called()
    _assert_logged_error(logger, f"Failed to clean up after test: {cleanup_error}")
    assert "Traceback (most recent call last)" in _logged_errors(logger)
