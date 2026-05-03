from unittest.mock import MagicMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo import CLAUDE_EXTENDED_THINKING_MODELS
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler


def create_mock_settings(override):
    """Create a fake settings object with configurable Claude extended-thinking override."""
    return type('', (), {
        'config': type('', (), {
            'verbosity_level': 0,
            'get': lambda self, key, default=None: override
            if key == "claude_extended_thinking_models_override"
            else default,
        })(),
        'litellm': type('', (), {
            'get': lambda self, key, default=None: default
        })(),
        'get': lambda self, key, default=None: default
    })()


@pytest.fixture
def mock_logger():
    """Mock logger to capture warning calls."""
    with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.get_logger') as mock_log:
        mock_log_instance = MagicMock()
        mock_log.return_value = mock_log_instance
        yield mock_log_instance


def test_claude_extended_thinking_override_invalid_type_warns_and_uses_default(monkeypatch, mock_logger):
    fake_settings = create_mock_settings("claude-3-7-sonnet-latest")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    handler = LiteLLMAIHandler()

    assert handler.claude_extended_thinking_models == CLAUDE_EXTENDED_THINKING_MODELS
    mock_logger.warning.assert_called_once()
    warning_call = mock_logger.warning.call_args[0][0]
    assert "Invalid claude_extended_thinking_models_override" in warning_call
    assert "expected a list" in warning_call


def test_claude_extended_thinking_override_invalid_model_names_warns_and_uses_default(monkeypatch, mock_logger):
    fake_settings = create_mock_settings(["claude-3-7-sonnet-latest", 123])
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    handler = LiteLLMAIHandler()

    assert handler.claude_extended_thinking_models == CLAUDE_EXTENDED_THINKING_MODELS
    mock_logger.warning.assert_called_once()
    warning_call = mock_logger.warning.call_args[0][0]
    assert "Invalid claude_extended_thinking_models_override" in warning_call
    assert "expected a list of model name strings" in warning_call


def test_claude_extended_thinking_override_valid_list_replaces_default(monkeypatch, mock_logger):
    override = ["custom-claude-model"]
    fake_settings = create_mock_settings(override)
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    handler = LiteLLMAIHandler()

    assert handler.claude_extended_thinking_models == override
    assert handler.claude_extended_thinking_models is not override
    mock_logger.warning.assert_not_called()
