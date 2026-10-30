from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from tenacity import TryAgain

from pr_agent.git_providers import github_provider as gp


@pytest.fixture
def retry_context(monkeypatch):
    settings = deepcopy(gp.get_settings())
    settings.set("github.ratelimit_retries", 5)
    monkeypatch.setattr(gp, "get_settings", lambda: settings)
    sleep = Mock()
    uniform = Mock(return_value=2)
    logger = Mock()
    monkeypatch.setattr(gp.time, "sleep", sleep)
    monkeypatch.setattr(gp.random, "uniform", uniform)
    monkeypatch.setattr(gp, "get_logger", lambda: logger)
    provider = gp.GithubProvider.__new__(gp.GithubProvider)
    provider._get_diff_files = Mock()
    return SimpleNamespace(provider=provider, settings=settings, sleep=sleep, uniform=uniform, logger=logger)


@pytest.mark.parametrize("tries", [1, 2, 5, 1.0, 2.0, 5.0, "1", "2", "5", " 2 "])
def test_attempt_limit_reraises_original_final_exception(retry_context, tries):
    ctx = retry_context
    ctx.settings.set("github.ratelimit_retries", tries)
    attempts = int(tries)
    errors = [gp.RateLimitExceeded(f"limited {index}") for index in range(attempts)]
    ctx.provider._get_diff_files.side_effect = errors

    with pytest.raises(gp.RateLimitExceeded) as caught:
        ctx.provider.get_diff_files()

    assert caught.value is errors[-1]
    assert ctx.provider._get_diff_files.call_count == attempts
    assert ctx.sleep.call_count == attempts - 1
    assert ctx.uniform.call_args_list == [call(1, 3)] * (attempts - 1)
    assert ctx.logger.warning.call_count == attempts - 1


@pytest.mark.parametrize("failures", [0, 1, 4])
@pytest.mark.parametrize("tries", [5, "5"])
def test_success_returns_original_result(retry_context, failures, tries):
    ctx = retry_context
    ctx.settings.set("github.ratelimit_retries", tries)
    result = []
    ctx.provider._get_diff_files.side_effect = [gp.RateLimitExceeded("limited")] * failures + [result]

    assert ctx.provider.get_diff_files() is result
    assert ctx.provider._get_diff_files.call_count == failures + 1
    assert ctx.sleep.call_count == failures
    assert ctx.uniform.call_count == failures


@pytest.mark.parametrize("error", [ValueError("invalid"), RuntimeError("broken"), TryAgain("unrelated")])
def test_unrelated_errors_are_not_retried(retry_context, error):
    ctx = retry_context
    ctx.provider._get_diff_files.side_effect = error

    with pytest.raises(type(error)) as caught:
        ctx.provider.get_diff_files()

    assert caught.value is error
    ctx.provider._get_diff_files.assert_called_once_with()
    ctx.sleep.assert_not_called()
    ctx.uniform.assert_not_called()
    ctx.logger.warning.assert_not_called()


@pytest.mark.parametrize("tries", [0, 0.0, "0"])
def test_zero_attempts_does_not_call_provider(retry_context, tries):
    ctx = retry_context
    ctx.settings.set("github.ratelimit_retries", tries)

    assert ctx.provider.get_diff_files() is None
    ctx.provider._get_diff_files.assert_not_called()
    ctx.sleep.assert_not_called()
    ctx.uniform.assert_not_called()
    ctx.logger.warning.assert_not_called()


@pytest.mark.parametrize("tries", [-1, -7, -1.0, -7.0, "-1", "-7"])
def test_negative_attempts_continue_until_success(retry_context, tries):
    ctx = retry_context
    ctx.settings.set("github.ratelimit_retries", tries)
    result = []
    failures = 12
    ctx.provider._get_diff_files.side_effect = [gp.RateLimitExceeded("limited")] * failures + [result]

    assert ctx.provider.get_diff_files() is result
    assert ctx.provider._get_diff_files.call_count == failures + 1
    assert ctx.sleep.call_count == failures


def test_cumulative_jitter_is_drawn_after_each_sleep(retry_context):
    ctx = retry_context
    events = []
    jitters = iter([1, 3, 2])

    def draw_jitter(low, high):
        events.append(("jitter", low, high))
        return next(jitters)

    outcomes = iter([gp.RateLimitExceeded("limited")] * 3 + [[]])

    def get_files():
        events.append("attempt")
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    ctx.provider._get_diff_files.side_effect = get_files
    ctx.sleep.side_effect = lambda delay: events.append(("sleep", delay))
    ctx.uniform.side_effect = draw_jitter

    assert ctx.provider.get_diff_files() == []
    assert events == [
        "attempt", ("sleep", 2), ("jitter", 1, 3),
        "attempt", ("sleep", 5), ("jitter", 1, 3),
        "attempt", ("sleep", 13), ("jitter", 1, 3),
        "attempt",
    ]
    assert ctx.logger.warning.call_args_list == [
        call("limited, retrying in 2 seconds..."),
        call("limited, retrying in 5 seconds..."),
        call("limited, retrying in 13 seconds..."),
    ]


def test_repeated_calls_reset_delay(retry_context):
    ctx = retry_context
    ctx.provider._get_diff_files.side_effect = [gp.RateLimitExceeded("limited"), []] * 2

    assert ctx.provider.get_diff_files() == []
    assert ctx.provider.get_diff_files() == []
    assert ctx.sleep.call_args_list == [call(2), call(2)]


def test_overlapping_calls_have_independent_delay(retry_context):
    ctx = retry_context
    error = gp.RateLimitExceeded("limited")
    ctx.provider._get_diff_files.side_effect = [error, error, [], error, []]
    ctx.uniform.side_effect = [1, 2, 3]

    def sleep_with_nested_call(_delay):
        if ctx.sleep.call_count == 1:
            assert ctx.provider.get_diff_files() == []

    ctx.sleep.side_effect = sleep_with_nested_call

    assert ctx.provider.get_diff_files() == []
    assert ctx.sleep.call_args_list == [call(2), call(2), call(6)]


def test_settings_are_read_once_per_invocation(retry_context):
    ctx = retry_context
    ctx.settings.set("github.ratelimit_retries", 1)
    error = gp.RateLimitExceeded("limited")

    def change_setting_then_fail():
        ctx.settings.set("github.ratelimit_retries", 3)
        raise error

    ctx.provider._get_diff_files.side_effect = change_setting_then_fail
    with pytest.raises(gp.RateLimitExceeded) as caught:
        ctx.provider.get_diff_files()
    assert caught.value is error
    ctx.provider._get_diff_files.assert_called_once_with()
    ctx.sleep.assert_not_called()

    ctx.provider._get_diff_files.reset_mock(side_effect=True)
    ctx.provider._get_diff_files.side_effect = [error, error, []]
    assert ctx.provider.get_diff_files() == []
    assert ctx.provider._get_diff_files.call_count == 3


@pytest.mark.parametrize("invalid", [
    "abc", "", "2.5", [], {}, None, True, False,
    float("nan"), float("inf"), float("-inf"), 2.5, -0.5,
])
def test_invalid_retry_setting_falls_back_before_first_attempt(retry_context, invalid):
    ctx = retry_context
    ctx.settings.set("github.ratelimit_retries", invalid)
    result = []
    ctx.provider._get_diff_files.return_value = result

    assert ctx.provider.get_diff_files() is result
    ctx.provider._get_diff_files.assert_called_once_with()
    ctx.sleep.assert_not_called()
    ctx.logger.warning.assert_called_once_with("Invalid GITHUB.RATELIMIT_RETRIES; using default 5 attempts.")


@pytest.mark.parametrize("invalid", ["abc", True, False, float("nan"), float("inf"), float("-inf"), 2.5, -0.5])
def test_invalid_retry_setting_uses_default_attempt_limit(retry_context, invalid):
    ctx = retry_context
    ctx.settings.set("github.ratelimit_retries", invalid)
    error = gp.RateLimitExceeded("limited")
    ctx.provider._get_diff_files.side_effect = [error] * 6 + [AssertionError("retry limit not enforced")]

    with pytest.raises(gp.RateLimitExceeded) as caught:
        ctx.provider.get_diff_files()

    assert caught.value is error
    assert ctx.provider._get_diff_files.call_count == 5
    assert ctx.sleep.call_count == 4
    assert ctx.logger.warning.call_args_list[0] == call(
        "Invalid GITHUB.RATELIMIT_RETRIES; using default 5 attempts."
    )


def test_missing_setting_defaults_to_five_total_attempts(retry_context):
    ctx = retry_context
    ctx.settings.unset("github", force=True)
    error = gp.RateLimitExceeded("limited")
    ctx.provider._get_diff_files.side_effect = error

    with pytest.raises(gp.RateLimitExceeded) as caught:
        ctx.provider.get_diff_files()

    assert caught.value is error
    assert ctx.provider._get_diff_files.call_count == 5
    assert ctx.sleep.call_count == 4
