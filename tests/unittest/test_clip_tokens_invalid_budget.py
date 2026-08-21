"""A non-numeric token budget must be reported, not silently ignored."""
from pr_agent.algo.utils import clip_tokens
from pr_agent.log import get_logger

LONG_TEXT = "word " * 500


def _capture(call):
    import io
    buffer = io.StringIO()
    handler_id = get_logger().add(buffer, level="DEBUG", format="{message}", colorize=False)
    try:
        result = call()
    finally:
        get_logger().remove(handler_id)
    return result, buffer.getvalue()


def test_accept_a_quoted_budget():
    """A quoted number is a valid budget and must actually clip."""
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
    """Short text is still returned untouched."""
    assert clip_tokens("short", 1000) == "short"
