"""Report a token budget that cannot be read, instead of silently ignoring it."""
import io

import pytest

from pr_agent.algo.utils import clip_tokens
from pr_agent.log import get_logger

LONG_TEXT = "word " * 500


def _capture(call):
    buffer = io.StringIO()
    handler_id = get_logger().add(buffer, level="DEBUG", format="{message}", colorize=False)
    try:
        result = call()
    finally:
        get_logger().remove(handler_id)
    return result, buffer.getvalue()


def test_accept_a_quoted_budget():
    """Clip against a quoted number, which is a valid budget."""
    out = clip_tokens(LONG_TEXT, "50")

    assert len(out) < len(LONG_TEXT)


def test_warn_when_the_budget_cannot_be_read():
    """Warn instead of silently returning the text at full length."""
    out, logged = _capture(lambda: clip_tokens(LONG_TEXT, "not-a-number"))

    assert out == LONG_TEXT
    assert "non-numeric max_tokens" in logged


def test_numeric_budget_is_unchanged():
    """Keep the existing behaviour for a genuinely numeric budget."""
    assert len(clip_tokens(LONG_TEXT, 50)) < len(LONG_TEXT)


def test_text_within_budget_is_returned_whole():
    """Return short text untouched."""
    assert clip_tokens("short", 1000) == "short"


@pytest.mark.parametrize("budget", [float("inf"), float("-inf"), float("nan")])
def test_a_non_finite_budget_returns_the_text(budget):
    """Return the text for a non-finite budget, which int() raises on, instead of crashing."""
    assert clip_tokens(LONG_TEXT, budget) == LONG_TEXT


def test_an_unreadable_budget_is_reported_for_empty_text():
    """Report the budget whatever the text is, so the warning does not depend on content."""
    _, logged = _capture(lambda: clip_tokens("", "not-a-number"))

    assert "not-a-number" in logged
