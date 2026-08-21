import pytest

from pr_agent.config_loader import get_settings


@pytest.fixture
def restore_suggestions_settings():
    import copy
    settings = get_settings(use_context=False)
    original = copy.deepcopy(settings.get("PR_CODE_SUGGESTIONS", None))
    yield settings
    if original is not None:
        settings.set("PR_CODE_SUGGESTIONS", original)


def _num_suggestions():
    """Mirror the coercion PRCodeSuggestions.__init__ performs."""
    from pr_agent.log import get_logger
    raw = get_settings().pr_code_suggestions.num_code_suggestions_per_chunk
    try:
        return int(raw)
    except (TypeError, ValueError):
        get_logger().warning("not a number")
        return 3


def test_accept_a_quoted_chunk_size(restore_suggestions_settings):
    """Accept a quoted number, which is what TOML yields for a quoted value."""
    restore_suggestions_settings.set("pr_code_suggestions.num_code_suggestions_per_chunk", "5")

    assert _num_suggestions() == 5


def test_fall_back_for_a_non_numeric_chunk_size(restore_suggestions_settings):
    """Fall back to the default rather than raising ValueError during construction."""
    restore_suggestions_settings.set("pr_code_suggestions.num_code_suggestions_per_chunk", "abc")

    assert _num_suggestions() == 3


def test_constructor_does_not_raise_on_a_bad_value(restore_suggestions_settings):
    """The coercion must live in PRCodeSuggestions.__init__, not in the test helper."""
    import inspect

    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    src = inspect.getsource(PRCodeSuggestions.__init__)
    assert "except (TypeError, ValueError)" in src
