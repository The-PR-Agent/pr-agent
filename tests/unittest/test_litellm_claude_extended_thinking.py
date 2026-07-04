from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo import CLAUDE_EXTENDED_THINKING_MODELS


def settings_with_claude_override(override):
    return SimpleNamespace(
        config=SimpleNamespace(
            verbosity_level=0,
            get=lambda key, default=None: (
                override if key == "claude_extended_thinking_models_override" else default
            ),
        ),
        litellm=SimpleNamespace(get=lambda key, default=None: default),
        get=lambda key, default=None: default,
    )


@pytest.fixture
def logger():
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.get_logger") as get_logger:
        logger = MagicMock()
        get_logger.return_value = logger
        yield logger


@pytest.mark.parametrize(
    "override",
    [
        "claude-3-7-sonnet-latest",
        ["claude-3-7-sonnet-latest", 123],
        [""],
    ],
)
def test_invalid_claude_extended_thinking_override_falls_back_to_built_in_models(
    monkeypatch,
    logger,
    override,
):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings_with_claude_override(override))

    handler = litellm_handler.LiteLLMAIHandler()

    assert handler.claude_extended_thinking_models == CLAUDE_EXTENDED_THINKING_MODELS
    logger.warning.assert_called_once()


def test_valid_claude_extended_thinking_override_replaces_built_in_models(monkeypatch, logger):
    override = ["custom-claude-model"]
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings_with_claude_override(override))

    handler = litellm_handler.LiteLLMAIHandler()

    assert handler.claude_extended_thinking_models == ["custom-claude-model"]
    assert handler.claude_extended_thinking_models is not override
    logger.warning.assert_not_called()


def test_claude_extended_thinking_override_entries_are_stripped(monkeypatch, logger):
    # Entries with surrounding whitespace must be stored stripped so exact model matches succeed.
    override = ["  custom-claude-model  ", "another-model\n"]
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings_with_claude_override(override))

    handler = litellm_handler.LiteLLMAIHandler()

    assert handler.claude_extended_thinking_models == ["custom-claude-model", "another-model"]
    logger.warning.assert_not_called()
