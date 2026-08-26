import pytest

from pr_agent.algo import MAX_TOKENS
from pr_agent.algo.utils import get_max_tokens
from pr_agent.config_loader import get_settings

KNOWN_MODEL = next(iter(MAX_TOKENS))
UNKNOWN_MODEL = "vendor/model-not-in-max-tokens"


@pytest.fixture
def restore_config():
    import copy
    settings = get_settings(use_context=False)
    original = copy.deepcopy(settings.get("CONFIG", None))
    yield settings
    if original is not None:
        settings.set("CONFIG", original)


def test_accept_a_quoted_custom_model_max_tokens(restore_config):
    """Accept a quoted number for custom_model_max_tokens, which is what TOML yields for
    `custom_model_max_tokens = "8000"`."""
    restore_config.set("config.custom_model_max_tokens", "8000")
    restore_config.set("config.max_model_tokens", 0)

    assert get_max_tokens(UNKNOWN_MODEL) == 8000


def test_accept_a_quoted_max_model_tokens(restore_config):
    """Apply max_model_tokens even when it arrives as a quoted number."""
    restore_config.set("config.max_model_tokens", "1000")

    assert get_max_tokens(KNOWN_MODEL) == 1000


def test_ignore_an_unparseable_max_model_tokens(restore_config):
    """Fall back to the model limit when max_model_tokens cannot be read as a number."""
    restore_config.set("config.max_model_tokens", "not-a-number")

    assert get_max_tokens(KNOWN_MODEL) == MAX_TOKENS[KNOWN_MODEL]


def test_numeric_settings_still_work(restore_config):
    """Keep the existing behaviour for genuinely numeric settings."""
    restore_config.set("config.custom_model_max_tokens", 4096)
    restore_config.set("config.max_model_tokens", 0)

    assert get_max_tokens(UNKNOWN_MODEL) == 4096
