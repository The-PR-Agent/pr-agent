"""A transient GitHub failure on the first read of a pull request is retried, and a webhook that still cannot read it
is dropped with one warning rather than an unhandled traceback."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from github import GithubException
from requests.exceptions import ConnectionError as RequestsConnectionError

from pr_agent.git_providers import github_provider as gp
from pr_agent.servers import github_app

PR_URL = "https://github.com/org/repo/pull/7"
API_URL = "https://api.github.com/repos/org/repo/pulls/7"


@pytest.fixture
def lookup(monkeypatch):
    settings = deepcopy(gp.get_settings())
    settings.set("github.api_retries", 0)
    monkeypatch.setattr(gp, "get_settings", lambda: settings)
    sleep = Mock()
    monkeypatch.setattr(gp, "_pr_lookup_sleep", sleep)
    logger = Mock()
    monkeypatch.setattr(gp, "get_logger", lambda: logger)
    provider = gp.GithubProvider.__new__(gp.GithubProvider)
    provider.repo, provider.pr_num = None, None
    provider._get_pr = Mock()
    return SimpleNamespace(provider=provider, settings=settings, sleep=sleep, logger=logger)


def test_a_transient_5xx_is_retried_until_the_pr_is_read(lookup):
    pr = object()
    lookup.provider._get_pr.side_effect = [GithubException(500), GithubException(502), pr]

    lookup.provider.set_pr(PR_URL)

    assert lookup.provider.pr is pr
    assert lookup.sleep.call_args_list == [call(1), call(2)]
    assert lookup.logger.warning.call_count == 2
    assert "HTTP 500" in lookup.logger.warning.call_args_list[0].args[0]


def test_a_connection_error_is_retried(lookup):
    pr = object()
    lookup.provider._get_pr.side_effect = [RequestsConnectionError("reset"), pr]

    lookup.provider.set_pr(PR_URL)

    assert lookup.provider.pr is pr
    assert lookup.sleep.call_args_list == [call(1)]


def test_the_last_failure_is_raised_once_the_retries_run_out(lookup):
    errors = [GithubException(503) for _ in range(4)]
    lookup.provider._get_pr.side_effect = errors

    with pytest.raises(GithubException) as caught:
        lookup.provider.set_pr(PR_URL)

    assert caught.value is errors[-1]
    assert lookup.sleep.call_args_list == [call(1), call(2), call(4)]


@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_a_client_error_is_raised_at_once(lookup, status):
    lookup.provider._get_pr.side_effect = GithubException(status)

    with pytest.raises(GithubException):
        lookup.provider.set_pr(PR_URL)

    assert lookup.provider._get_pr.call_count == 1
    lookup.sleep.assert_not_called()


def test_no_extra_retries_when_api_retries_already_retries_every_request(lookup):
    lookup.settings.set("github.api_retries", 3)
    lookup.provider._get_pr.side_effect = GithubException(500)

    with pytest.raises(GithubException):
        lookup.provider.set_pr(PR_URL)

    assert lookup.provider._get_pr.call_count == 1
    lookup.sleep.assert_not_called()


@pytest.fixture
def unreadable_pr(monkeypatch):
    def fail(api_url):
        try:
            raise GithubException(500)
        except GithubException as e:
            raise ValueError(f"Failed to get git provider for {api_url}") from e

    monkeypatch.setattr(github_app, "apply_repo_settings", fail)
    logger = Mock()
    monkeypatch.setattr(github_app, "get_logger", lambda: logger)
    monkeypatch.setattr(
        github_app, "_check_pull_request_event", lambda action, body, log_context: ({"number": 7}, API_URL)
    )
    settings = SimpleNamespace(github_app=SimpleNamespace(handle_pr_actions=["opened"]))
    monkeypatch.setattr(github_app, "get_settings", lambda: settings)
    perform = Mock()
    monkeypatch.setattr(github_app, "_perform_auto_commands_github", perform)
    return SimpleNamespace(logger=logger, perform=perform)


def test_new_pr_handler_logs_one_warning_and_reports_failure(unreadable_pr):
    result = asyncio.run(
        github_app.handle_new_pr_opened({}, "pull_request", "alice", "1", "opened", {}, agent=None)
    )

    assert result is False
    unreadable_pr.perform.assert_not_called()
    assert unreadable_pr.logger.warning.call_count == 1
    message = unreadable_pr.logger.warning.call_args.args[0]
    assert API_URL in message and "500" in message


def test_review_handler_logs_one_warning_and_reports_failure(unreadable_pr):
    result = asyncio.run(
        github_app.handle_pull_request_review_submitted(
            {}, "pull_request_review", "alice", "1", "User", "submitted", {}, agent=None
        )
    )

    assert result is False
    unreadable_pr.perform.assert_not_called()
    assert unreadable_pr.logger.warning.call_count == 1


def test_push_handler_logs_one_warning_and_reports_failure(unreadable_pr):
    result = asyncio.run(
        github_app.handle_push_trigger_for_new_commits(
            {"before": "a", "after": "b"}, "pull_request", "alice", "1", "synchronize", {}, agent=None
        )
    )

    assert result is False
    unreadable_pr.perform.assert_not_called()
    assert unreadable_pr.logger.warning.call_count == 1
