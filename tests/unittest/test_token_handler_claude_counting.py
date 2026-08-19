import sys
import types

import pytest

from pr_agent.algo import MAX_TOKENS
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.config_loader import get_settings


@pytest.fixture
def restore_model():
    settings = get_settings(use_context=False)
    original = settings.config.model
    yield settings
    settings.set("config.model", original)


@pytest.fixture
def anthropic_client_unavailable(monkeypatch):
    """Force the failure path deterministically, without touching the network."""
    fake = types.ModuleType("anthropic")

    def _unavailable(*args, **kwargs):
        raise RuntimeError("anthropic client unavailable")

    fake.Anthropic = _unavailable
    monkeypatch.setitem(sys.modules, "anthropic", fake)


def test_unlisted_claude_model_falls_back_to_the_local_estimate(
        restore_model, anthropic_client_unavailable):
    model = "anthropic/claude-model-not-in-max-tokens"
    assert model not in MAX_TOKENS
    restore_model.set("config.model", model)

    handler = TokenHandler(system="system", user="user")

    assert handler._calc_claude_tokens("some patch", default_estimate=123) == 123


def test_listed_claude_model_still_falls_back_to_its_max_tokens(
        restore_model, anthropic_client_unavailable):
    model = next(m for m in MAX_TOKENS if "claude" in m)
    restore_model.set("config.model", model)

    handler = TokenHandler(system="system", user="user")

    assert handler._calc_claude_tokens("some patch", default_estimate=123) == MAX_TOKENS[model]
