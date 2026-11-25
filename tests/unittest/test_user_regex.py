import re
import time

import pytest

from pr_agent.algo import user_regex
from pr_agent.algo.user_regex import UserRegexBudget, compile_user_regex


class _SlowPattern:
    """Stand in for a backtracking pattern: every match runs until its timeout and then raises."""

    def __init__(self, pattern="slow"):
        self.pattern = pattern
        self.calls = 0

    def _run(self, string, timeout=None):
        self.calls += 1
        time.sleep(timeout)
        raise TimeoutError("regex timed out")

    match = search = finditer = _run


def test_compile_keeps_re_error_for_invalid_patterns():
    with pytest.raises(re.error):
        compile_user_regex("(")


def test_compile_rejects_non_string_patterns_like_re():
    with pytest.raises(TypeError):
        compile_user_regex(123)


@pytest.mark.parametrize("method, expected", [("match", "ab"), ("search", "ab"), ("finditer", ["ab", "ab"])])
def test_run_returns_the_normal_result(method, expected):
    budget = UserRegexBudget()
    result = budget.run(compile_user_regex("ab"), method, "ab-ab", "test.setting")
    if method == "finditer":
        assert [m.group(0) for m in result] == expected
    else:
        assert result.group(0) == expected
    assert budget.timed_out is False


@pytest.mark.parametrize("method", ["match", "search", "finditer"])
def test_pattern_that_times_out_counts_as_no_match(method):
    budget = UserRegexBudget()
    pattern = _SlowPattern()
    start = time.monotonic()
    assert budget.run(pattern, method, "text", "test.setting") is None
    assert time.monotonic() - start < 1
    assert pattern.calls == 1
    assert budget.timed_out is True


def test_timed_out_pattern_is_skipped_for_the_rest_of_the_decision():
    budget = UserRegexBudget()
    pattern = _SlowPattern()
    for _ in range(50):
        assert budget.run(pattern, "search", "text", "test.setting") is None
    assert pattern.calls == 1
    assert budget.run(compile_user_regex("a"), "search", "a", "test.setting").group(0) == "a"


def test_matches_share_one_budget(monkeypatch):
    monkeypatch.setattr(user_regex, "USER_REGEX_TIMEOUT_SECONDS", 0.05)
    budget = UserRegexBudget(seconds=0.2)
    patterns = [_SlowPattern(f"slow{index}") for index in range(50)]
    start = time.monotonic()
    for pattern in patterns:
        assert budget.run(pattern, "search", "text", "test.setting") is None
    assert time.monotonic() - start < 1
    assert sum(pattern.calls for pattern in patterns) <= 5


def test_spent_budget_runs_nothing_and_warns_once_per_pattern():
    budget = UserRegexBudget(seconds=0)
    warnings = []
    handler = user_regex.get_logger().add(warnings.append, level="WARNING", format="{message}")
    try:
        for _ in range(3):
            assert budget.run(compile_user_regex("a"), "search", "a", "test.setting") is None
    finally:
        user_regex.get_logger().remove(handler)
    assert len([w for w in warnings if "test.setting" in w]) == 1
    assert budget.timed_out is True
