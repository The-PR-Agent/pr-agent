import pytest

from pr_agent.algo.token_handler import TokenHandler
from pr_agent.config_loader import get_settings


@pytest.fixture
def restore_config():
    import copy
    settings = get_settings(use_context=False)
    original = copy.deepcopy(settings.get("CONFIG", None))
    yield settings
    if original is None:
        settings.set("CONFIG", {})
    else:
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


@pytest.mark.parametrize("value", ["inf", "-infinity", "nan", float("inf"), float("nan")])
def test_fall_back_for_a_non_finite_factor(restore_config, value):
    """Fall back to a factor of 1 for a non-finite value, which float() happily accepts."""
    restore_config.set("config.model_token_count_estimate_factor", value)
    handler = _handler()

    assert handler._apply_estimation_factor("some-model", 100) == 100


@pytest.mark.parametrize("value", [True, False])
def test_fall_back_for_a_boolean_factor(restore_config, value):
    """Treat a boolean as unusable rather than letting float(True) become a factor of 2."""
    restore_config.set("config.model_token_count_estimate_factor", value)
    handler = _handler()

    assert handler._apply_estimation_factor("some-model", 100) == 100


@pytest.mark.parametrize("value", [1e308, "1e308"])
def test_fall_back_when_the_factor_overflows_the_estimate(restore_config, value):
    """Fall back to the raw estimate when the multiplication overflows to infinity."""
    restore_config.set("config.model_token_count_estimate_factor", value)
    handler = _handler()

    assert handler._apply_estimation_factor("some-model", 100) == 100


@pytest.mark.parametrize("value", [-2, "-3.5"])
def test_fall_back_for_a_negative_factor(restore_config, value):
    """Fall back to a factor of 1 rather than reporting a negative token count."""
    restore_config.set("config.model_token_count_estimate_factor", value)
    handler = _handler()

    assert handler._apply_estimation_factor("some-model", 100) == 100
