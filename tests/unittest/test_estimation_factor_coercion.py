import pytest

from pr_agent.algo.token_handler import TokenHandler
from pr_agent.config_loader import get_settings


@pytest.fixture
def restore_config():
    import copy
    settings = get_settings(use_context=False)
    original = copy.deepcopy(settings.get("CONFIG", None))
    yield settings
    if original is not None:
        settings.set("CONFIG", original)


def _handler():
    return TokenHandler(system="system", user="user")


def test_accept_a_quoted_factor(restore_config):
    """Accept a quoted number, which is what TOML yields for
    `model_token_count_estimate_factor = "0.3"`."""
    restore_config.set("config.model_token_count_estimate_factor", "0.3")

    assert _handler()._apply_estimation_factor("m", 100) == 130


def test_fall_back_to_no_inflation_for_an_unreadable_factor(restore_config):
    """Fall back to a factor of 1 rather than raising when the value cannot be read."""
    restore_config.set("config.model_token_count_estimate_factor", "abc")

    assert _handler()._apply_estimation_factor("m", 100) == 100


def test_numeric_factor_is_unchanged(restore_config):
    """Keep the existing behaviour for a genuinely numeric factor."""
    restore_config.set("config.model_token_count_estimate_factor", 0.5)

    assert _handler()._apply_estimation_factor("m", 100) == 150
