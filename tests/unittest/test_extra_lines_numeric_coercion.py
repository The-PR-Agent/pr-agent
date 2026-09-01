import pytest

from pr_agent.algo.pr_processing import MAX_EXTRA_LINES, cap_and_log_extra_lines


def test_accept_a_quoted_number():
    """Accept a quoted number, which is what TOML yields for `patch_extra_lines_before = "3"`."""
    assert cap_and_log_extra_lines("3", "before") == 3


def test_cap_a_quoted_number_above_the_maximum():
    """Cap a quoted number using the same limit as a numeric one."""
    assert cap_and_log_extra_lines(str(MAX_EXTRA_LINES + 5), "after") == MAX_EXTRA_LINES


@pytest.mark.parametrize("value", ["abc", None, [3]])
def test_fall_back_to_zero_for_an_unreadable_value(value):
    """Fall back to no extra lines rather than raising when the value cannot be read."""
    assert cap_and_log_extra_lines(value, "before") == 0


def test_numeric_values_are_unchanged():
    """Keep the existing behaviour for genuinely numeric settings."""
    assert cap_and_log_extra_lines(3, "before") == 3
    assert cap_and_log_extra_lines(MAX_EXTRA_LINES + 1, "before") == MAX_EXTRA_LINES


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_fall_back_for_a_non_finite_value(value):
    """Fall back to no extra lines for a non-finite float, which int() cannot convert."""
    assert cap_and_log_extra_lines(value, "before") == 0
