from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import litellm.utils as litellm_utils
import pytest
from litellm.litellm_core_utils.get_model_cost_map import GetModelCostMap
from litellm.utils import get_optional_params

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler


def create_mock_settings(reasoning_effort_value):
    """Create a fake settings object with configurable reasoning_effort."""
    return type('', (), {
        'config': type('', (), {
            'reasoning_effort': reasoning_effort_value,
            'ai_timeout': 120,
            'custom_reasoning_model': False,
            'max_model_tokens': 32000,
            'verbosity_level': 0,
            'get': lambda self, key, default=None: default
        })(),
        'litellm': type('', (), {
            'get': lambda self, key, default=None: default
        })(),
        'get': lambda self, key, default=None: default
    })()


def create_mock_acompletion_response():
    """Create a properly structured mock response for acompletion."""
    mock_response = MagicMock()
    mock_response.__getitem__ = lambda self, key: {
        "choices": [{"message": {"content": "test"}, "finish_reason": "stop"}]
    }[key]
    mock_response.dict.return_value = {"choices": [{"message": {"content": "test"}, "finish_reason": "stop"}]}
    return mock_response


@pytest.fixture
def mock_logger():
    """Mock logger to capture info and warning calls."""
    with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.get_logger') as mock_log:
        mock_log_instance = MagicMock()
        mock_log.return_value = mock_log_instance
        yield mock_log_instance


class TestLiteLLMReasoningEffort:
    """
    Comprehensive test suite for GPT-5 reasoning_effort configuration handling.

    Tests cover:
    - Valid reasoning_effort values for GPT-5 models
    - Invalid reasoning_effort values with warning logging
    - Model detection (GPT-5 vs non-GPT-5)
    - Model suffix handling (_thinking vs regular)
    - Default fallback logic
    - Logging behavior (info and warning messages)
    - thinking_kwargs_gpt5 structure validation
    """

    # ========== Group 1: Valid Configuration Tests ==========

    @pytest.mark.asyncio
    async def test_gpt5_valid_reasoning_effort_none(self, monkeypatch, mock_logger):
        """Test GPT-5 with valid reasoning_effort='none' from config."""
        fake_settings = create_mock_settings("none")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        # Mock acompletion to capture kwargs
        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            # Verify the call was made with correct reasoning_effort
            assert mock_completion.called
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "none"
            assert "reasoning_effort" in call_kwargs["allowed_openai_params"]

            # Verify info log
            mock_logger.info.assert_any_call("Using reasoning_effort='none' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_valid_reasoning_effort_low(self, monkeypatch, mock_logger):
        """Test GPT-5 with valid reasoning_effort='low' from config."""
        fake_settings = create_mock_settings("low")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "low"
            assert "reasoning_effort" in call_kwargs["allowed_openai_params"]
            mock_logger.info.assert_any_call("Using reasoning_effort='low' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_valid_reasoning_effort_medium(self, monkeypatch, mock_logger):
        """Test GPT-5 with valid reasoning_effort='medium' from config."""
        fake_settings = create_mock_settings("medium")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"
            assert "reasoning_effort" in call_kwargs["allowed_openai_params"]
            mock_logger.info.assert_any_call("Using reasoning_effort='medium' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_valid_reasoning_effort_high(self, monkeypatch, mock_logger):
        """Test GPT-5 with valid reasoning_effort='high' from config."""
        fake_settings = create_mock_settings("high")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "high"
            assert "reasoning_effort" in call_kwargs["allowed_openai_params"]
            mock_logger.info.assert_any_call("Using reasoning_effort='high' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_valid_reasoning_effort_xhigh(self, monkeypatch, mock_logger):
        """Test GPT-5 with valid reasoning_effort='xhigh' from config."""
        fake_settings = create_mock_settings("xhigh")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5.2",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "xhigh"
            assert "reasoning_effort" in call_kwargs["allowed_openai_params"]
            mock_logger.info.assert_any_call("Using reasoning_effort='xhigh' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_valid_reasoning_effort_max(self, monkeypatch, mock_logger):
        """Test GPT-5 with valid reasoning_effort='max' from config."""
        fake_settings = create_mock_settings("max")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch(
            'pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion',
            new_callable=AsyncMock,
        ) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5.6",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "max"
            assert "reasoning_effort" in call_kwargs["allowed_openai_params"]
            mock_logger.info.assert_any_call("Using reasoning_effort='max' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_valid_reasoning_effort_minimal(self, monkeypatch, mock_logger):
        """Test GPT-5 with valid reasoning_effort='minimal' from config."""
        fake_settings = create_mock_settings("minimal")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "minimal"
            assert "reasoning_effort" in call_kwargs["allowed_openai_params"]
            mock_logger.info.assert_any_call("Using reasoning_effort='minimal' for GPT-5 model")

    # ========== Group 2: Invalid Configuration Tests ==========

    @pytest.mark.asyncio
    async def test_gpt5_invalid_reasoning_effort_with_warning(self, monkeypatch, mock_logger):
        """Test GPT-5 with invalid reasoning_effort logs warning and uses default."""
        fake_settings = create_mock_settings("extreme")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            # Should default to 'medium'
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"

            # Verify warning logged
            mock_logger.warning.assert_called_once()
            warning_call = mock_logger.warning.call_args[0][0]
            assert "Invalid reasoning_effort 'extreme' in config" in warning_call
            assert "Valid values:" in warning_call

            # Verify info log
            mock_logger.info.assert_any_call("Using reasoning_effort='medium' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_invalid_reasoning_effort_thinking_model(self, monkeypatch, mock_logger):
        """Test GPT-5 _thinking model with invalid reasoning_effort defaults to 'medium'."""
        fake_settings = create_mock_settings("invalid_value")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07_thinking",
                system="test system",
                user="test user"
            )

            # Should default to 'medium' (no special handling for _thinking models)
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"

            # Verify warning logged
            mock_logger.warning.assert_called_once()

            # Verify info log
            mock_logger.info.assert_any_call("Using reasoning_effort='medium' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_none_config_defaults_to_medium(self, monkeypatch, mock_logger):
        """Test GPT-5 with None config defaults to 'medium' without warning."""
        fake_settings = create_mock_settings(None)
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            # Should default to 'medium'
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"

            # No warning should be logged
            mock_logger.warning.assert_not_called()

            # Info log should show effort
            mock_logger.info.assert_any_call("Using reasoning_effort='medium' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_none_config_thinking_model_defaults_to_medium(self, monkeypatch, mock_logger):
        """Test GPT-5 _thinking model with None config defaults to 'medium' without warning."""
        fake_settings = create_mock_settings(None)
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07_thinking",
                system="test system",
                user="test user"
            )

            # Should default to 'medium' (no special handling for _thinking models)
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"

            # No warning should be logged
            mock_logger.warning.assert_not_called()

            # Info log
            mock_logger.info.assert_any_call("Using reasoning_effort='medium' for GPT-5 model")

    # ========== Group 3: Model Detection Tests ==========

    @pytest.mark.asyncio
    async def test_gpt5_model_detection_various_versions(self, monkeypatch, mock_logger):
        """Test various GPT-5 model version strings trigger the reasoning_effort logic."""
        fake_settings = create_mock_settings("medium")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        gpt5_models = [
            "gpt-5-2025-08-07",
            "gpt-5.1",
            "gpt-5.4",
            "gpt-5.4-nano",
            "gpt-5.4-nano-2026-03-17",
            "gpt-5.4-mini",
            "gpt-5.4-mini-2026-03-17",
            "gpt-5.4-2026-03-05",
            "gpt-5.5",
            "gpt-5.5-2026-04-23",
            "gpt-5.6",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5-turbo",
            "gpt-5.1-codex",
            "gpt-5.3-codex",
        ]

        for model in gpt5_models:
            with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
                mock_completion.return_value = create_mock_acompletion_response()

                handler = LiteLLMAIHandler()
                await handler.chat_completion(
                    model=model,
                    system="test system",
                    user="test user"
                )

                # All should trigger GPT-5 logic
                call_kwargs = mock_completion.call_args[1]
                assert call_kwargs["reasoning_effort"] == "medium"
                assert "reasoning_effort" in call_kwargs["allowed_openai_params"]

    @pytest.mark.asyncio
    async def test_non_gpt5_model_no_thinking_kwargs(self, monkeypatch, mock_logger):
        """Test non-GPT-5 models do not trigger reasoning_effort logic."""
        fake_settings = create_mock_settings("high")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        non_gpt5_models = ["gpt-4o", "gpt-4-turbo", "claude-3-5-sonnet"]

        for model in non_gpt5_models:
            with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
                mock_completion.return_value = create_mock_acompletion_response()

                handler = LiteLLMAIHandler()
                await handler.chat_completion(
                    model=model,
                    system="test system",
                    user="test user"
                )

                # Should not have reasoning_effort in kwargs
                call_kwargs = mock_completion.call_args[1]
                assert "reasoning_effort" not in call_kwargs

    @pytest.mark.asyncio
    async def test_gpt5_suffix_removal(self, monkeypatch, mock_logger):
        """Test that _thinking suffix is properly removed from model name."""
        fake_settings = create_mock_settings("low")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5_thinking",
                system="test system",
                user="test user"
            )

            # Model should be transformed to openai/gpt-5
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["model"] == "openai/gpt-5"

    # ========== Group 4: Model Suffix Handling Tests ==========

    @pytest.mark.asyncio
    async def test_gpt5_thinking_suffix_default_medium(self, monkeypatch, mock_logger):
        """Test _thinking suffix models default to 'medium' when config is None."""
        fake_settings = create_mock_settings(None)
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07_thinking",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"
            mock_logger.info.assert_any_call("Using reasoning_effort='medium' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_regular_suffix_default_medium(self, monkeypatch, mock_logger):
        """Test regular GPT-5 models default to 'medium' when config is None."""
        fake_settings = create_mock_settings(None)
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"
            mock_logger.info.assert_any_call("Using reasoning_effort='medium' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_thinking_suffix_config_overrides_default(self, monkeypatch, mock_logger):
        """Test that config overrides the default for _thinking models."""
        fake_settings = create_mock_settings("high")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07_thinking",
                system="test system",
                user="test user"
            )

            # Should use 'high' from config, not 'medium' default
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "high"
            mock_logger.info.assert_any_call("Using reasoning_effort='high' for GPT-5 model")

    # ========== Group 5: Logging Behavior Tests ==========

    @pytest.mark.asyncio
    async def test_gpt5_info_logging_configured_value(self, monkeypatch, mock_logger):
        """Test info log when using configured value."""
        fake_settings = create_mock_settings("low")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            # Verify log
            mock_logger.info.assert_any_call("Using reasoning_effort='low' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_info_logging_default_value(self, monkeypatch, mock_logger):
        """Test info log when using default value."""
        fake_settings = create_mock_settings(None)
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            # Verify log
            mock_logger.info.assert_any_call("Using reasoning_effort='medium' for GPT-5 model")

    @pytest.mark.asyncio
    async def test_gpt5_warning_only_for_invalid_non_none(self, monkeypatch, mock_logger):
        """Test warning logged only for invalid non-None values."""
        # Test None - should not warn
        fake_settings = create_mock_settings(None)
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            # No warning for None
            mock_logger.warning.assert_not_called()

        # Reset mock
        mock_logger.reset_mock()

        # Test invalid string - should warn
        fake_settings = create_mock_settings("ultra")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            # Warning should be logged for invalid value
            mock_logger.warning.assert_called_once()

    # ========== Group 6: Structure Validation Tests ==========

    @pytest.mark.asyncio
    async def test_thinking_kwargs_gpt5_structure(self, monkeypatch, mock_logger):
        """Test that thinking_kwargs_gpt5 has correct structure."""
        fake_settings = create_mock_settings("medium")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]

            # Verify structure
            assert "reasoning_effort" in call_kwargs
            assert call_kwargs["reasoning_effort"] == "medium"
            assert "allowed_openai_params" in call_kwargs
            assert isinstance(call_kwargs["allowed_openai_params"], list)
            assert "reasoning_effort" in call_kwargs["allowed_openai_params"]

    @pytest.mark.asyncio
    async def test_thinking_kwargs_not_created_for_non_gpt5(self, monkeypatch, mock_logger):
        """Test that thinking_kwargs_gpt5 is not created for non-GPT-5 models."""
        fake_settings = create_mock_settings("high")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-4o",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]

            # Should not have reasoning_effort keys
            assert "reasoning_effort" not in call_kwargs
            assert call_kwargs.get("allowed_openai_params") is None or "reasoning_effort" not in call_kwargs.get("allowed_openai_params", [])

    # ========== Group 7: Edge Cases ==========

    @pytest.mark.asyncio
    async def test_empty_string_reasoning_effort(self, monkeypatch, mock_logger):
        """Test empty string reasoning_effort is treated as invalid."""
        fake_settings = create_mock_settings("")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            # Should default to 'medium' and log warning
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"
            mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_case_sensitive_reasoning_effort(self, monkeypatch, mock_logger):
        """Test that reasoning_effort validation is case-sensitive."""
        fake_settings = create_mock_settings("LOW")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            # Should treat uppercase as invalid and default to 'medium'
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"
            mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_whitespace_reasoning_effort(self, monkeypatch, mock_logger):
        """Test that reasoning_effort with whitespace is treated as invalid."""
        fake_settings = create_mock_settings(" low ")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5-2025-08-07",
                system="test system",
                user="test user"
            )

            # Should treat value with whitespace as invalid
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"
            mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_gpt5_prefix_match_only(self, monkeypatch, mock_logger):
        """Test that model.startswith('gpt-5') matching behavior.

        Note: The current logic uses startswith('gpt-5'), which means
        models like 'gpt-50' will also match (since 'gpt-50'.startswith('gpt-5') is True).
        This test documents the current behavior.
        """
        fake_settings = create_mock_settings("medium")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

        # Test gpt-50 (will match due to startswith logic)
        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-50",
                system="test system",
                user="test user"
            )

            # Due to startswith('gpt-5'), gpt-50 will match and have reasoning_effort
            call_kwargs = mock_completion.call_args[1]
            assert "reasoning_effort" in call_kwargs

        # Reset mock
        mock_logger.reset_mock()

        # Test gpt-5 (should match)
        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="gpt-5",
                system="test system",
                user="test user"
            )

            # Should have reasoning_effort
            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"

    # ========== Group 8: Provider Prefix Handling ==========

    @pytest.mark.asyncio
    async def test_gpt5_with_openai_prefix_triggers_reasoning_effort(self, monkeypatch, mock_logger):
        """Regression: model="openai/gpt-5*" must enter the GPT-5 reasoning_effort path.

        Before the fix, startswith('gpt-5') was False for prefixed names, so the handler
        sent temperature=0.2 to litellm and the request failed with UnsupportedParamsError
        for gpt-5 codex models.
        """
        fake_settings = create_mock_settings("medium")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
        # Isolate from runner env: LiteLLMAIHandler.__init__ branches on these vars.
        for _var in ("AWS_USE_IMDS", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                     "AWS_SESSION_TOKEN", "AWS_REGION_NAME", "OPENAI_API_KEY"):
            monkeypatch.delenv(_var, raising=False)

        prefixed_models = [
            "openai/gpt-5",
            "openai/gpt-5.1-codex",
            "openai/gpt-5.1-codex-max",
            "openai/gpt-5.4-mini",
        ]

        for model in prefixed_models:
            with patch(
                'pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion',
                new_callable=AsyncMock,
            ) as mock_completion:
                mock_completion.return_value = create_mock_acompletion_response()

                handler = LiteLLMAIHandler()
                await handler.chat_completion(
                    model=model,
                    system="test system",
                    user="test user"
                )

                call_kwargs = mock_completion.call_args[1]
                # GPT-5 path must trigger and drop temperature in favor of reasoning_effort
                assert call_kwargs["reasoning_effort"] == "medium", f"failed for {model}"
                assert "reasoning_effort" in call_kwargs["allowed_openai_params"], f"failed for {model}"
                assert "temperature" not in call_kwargs, f"temperature leaked for {model}"
                # Model name passed to litellm must keep the openai/ prefix exactly once
                assert call_kwargs["model"] == model, f"model double-prefixed: {call_kwargs['model']}"

    @pytest.mark.asyncio
    async def test_gpt5_with_openai_prefix_strips_thinking_suffix(self, monkeypatch, mock_logger):
        """Prefixed _thinking models must have the suffix removed without double-prefixing."""
        fake_settings = create_mock_settings("low")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
        # Isolate from runner env: LiteLLMAIHandler.__init__ branches on these vars.
        for _var in ("AWS_USE_IMDS", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                     "AWS_SESSION_TOKEN", "AWS_REGION_NAME", "OPENAI_API_KEY"):
            monkeypatch.delenv(_var, raising=False)

        with patch(
            'pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion',
            new_callable=AsyncMock,
        ) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(
                model="openai/gpt-5_thinking",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["model"] == "openai/gpt-5"
            assert call_kwargs["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_gpt5_with_explicit_azure_prefix_preserves_routing(self, monkeypatch, mock_logger):
        """Explicit `azure/` prefix in user config must be preserved (not silently rewritten to openai/)."""
        fake_settings = create_mock_settings("medium")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
        # Isolate from runner env: LiteLLMAIHandler.__init__ branches on these vars.
        for _var in ("AWS_USE_IMDS", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                     "AWS_SESSION_TOKEN", "AWS_REGION_NAME", "OPENAI_API_KEY"):
            monkeypatch.delenv(_var, raising=False)

        with patch(
            'pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion',
            new_callable=AsyncMock,
        ) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            # self.azure is False by default (no Azure AD creds in test env)
            await handler.chat_completion(
                model="azure/gpt-5.1-codex-max",
                system="test system",
                user="test user"
            )

            call_kwargs = mock_completion.call_args[1]
            assert call_kwargs["reasoning_effort"] == "medium"
            assert "temperature" not in call_kwargs
            # Provider prefix from user config must be preserved verbatim
            assert call_kwargs["model"] == "azure/gpt-5.1-codex-max"

    @pytest.mark.asyncio
    async def test_gpt5_in_azure_mode_does_not_stack_prefixes(self, monkeypatch, mock_logger):
        """Azure mode must produce exactly one `azure/` prefix, even if user config also has a prefix."""
        fake_settings = create_mock_settings("medium")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
        # Isolate from runner env: LiteLLMAIHandler.__init__ branches on these vars.
        for _var in ("AWS_USE_IMDS", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                     "AWS_SESSION_TOKEN", "AWS_REGION_NAME", "OPENAI_API_KEY"):
            monkeypatch.delenv(_var, raising=False)

        # Cases: bare name, openai/-prefixed config, azure/-prefixed config — all in azure mode
        cases = [
            ("gpt-5.1-codex", "azure/gpt-5.1-codex"),
            ("openai/gpt-5.1-codex", "azure/gpt-5.1-codex"),
            ("azure/gpt-5.1-codex", "azure/gpt-5.1-codex"),
        ]

        for input_model, expected in cases:
            with patch(
                'pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion',
                new_callable=AsyncMock,
            ) as mock_completion:
                mock_completion.return_value = create_mock_acompletion_response()

                handler = LiteLLMAIHandler()
                handler.azure = True  # simulate Azure-mode handler
                await handler.chat_completion(
                    model=input_model,
                    system="test system",
                    user="test user"
                )

                call_kwargs = mock_completion.call_args[1]
                # GPT-5 path must trigger
                assert call_kwargs["reasoning_effort"] == "medium", f"failed for {input_model}"
                assert "temperature" not in call_kwargs, f"temperature leaked for {input_model}"
                # Exactly one azure/ prefix, no stacked/duplicated provider segments
                assert call_kwargs["model"] == expected, (
                    f"wrong routing for {input_model}: got {call_kwargs['model']}, expected {expected}"
                )


class TestLiteLLMReasoningEffortGemini:
    """Gemini 2.5 reasoning_effort handling via the SUPPORT_REASONING_EFFORT_MODELS path.

    Gemini 2.5 exposes a thinking budget that LiteLLM maps from reasoning_effort. The
    membership test in chat_completion matches bare and provider-prefixed ids such as
    "vertex_ai/gemini-2.5-pro". OpenRouter models use extra_body.reasoning instead and
    are covered by test_litellm_openrouter_controls.py.
    """

    def _isolate_env(self, monkeypatch):
        # LiteLLMAIHandler.__init__ branches on these; clear them for a deterministic handler.
        for _var in ("AWS_USE_IMDS", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                     "AWS_SESSION_TOKEN", "AWS_REGION_NAME", "OPENAI_API_KEY"):
            monkeypatch.delenv(_var, raising=False)

    @pytest.mark.asyncio
    async def test_gemini_prefixed_forms_get_reasoning_effort(self, monkeypatch, mock_logger):
        """Bare and provider-prefixed Gemini 2.5 ids all receive the configured reasoning_effort."""
        fake_settings = create_mock_settings("low")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
        self._isolate_env(monkeypatch)

        gemini_models = [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini/gemini-2.5-pro",
            "vertex_ai/gemini-2.5-pro",
        ]

        for model in gemini_models:
            with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
                mock_completion.return_value = create_mock_acompletion_response()

                handler = LiteLLMAIHandler()
                await handler.chat_completion(model=model, system="test system", user="test user")

                call_kwargs = mock_completion.call_args[1]
                assert call_kwargs["reasoning_effort"] == "low", f"failed for {model}"
                # Gemini keeps temperature (it supports it) — unlike the GPT-5 path.
                assert call_kwargs["model"] == model, f"model mutated for {model}: {call_kwargs['model']}"

    @pytest.mark.asyncio
    async def test_non_listed_gemini_gets_no_reasoning_effort(self, monkeypatch, mock_logger):
        """A Gemini model not in the support list (e.g. 1.5) must not receive reasoning_effort."""
        fake_settings = create_mock_settings("low")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
        self._isolate_env(monkeypatch)

        for model in ("openrouter/google/gemini-1.5-pro", "gemini-1.5-flash"):
            with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
                mock_completion.return_value = create_mock_acompletion_response()

                handler = LiteLLMAIHandler()
                await handler.chat_completion(model=model, system="test system", user="test user")

                call_kwargs = mock_completion.call_args[1]
                assert "reasoning_effort" not in call_kwargs, f"unexpected reasoning_effort for {model}"

    @pytest.mark.asyncio
    async def test_suffix_match_does_not_overmatch(self, monkeypatch, mock_logger):
        """endswith('/' + id) must not match a model whose id is a substring without the slash boundary."""
        fake_settings = create_mock_settings("low")
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
        self._isolate_env(monkeypatch)

        # "my-gemini-2.5-pro" is not equal to and does not end with "/gemini-2.5-pro".
        with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion', new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()

            handler = LiteLLMAIHandler()
            await handler.chat_completion(model="my-gemini-2.5-pro", system="test system", user="test user")

            call_kwargs = mock_completion.call_args[1]
            assert "reasoning_effort" not in call_kwargs


class TestLiteLLMReasoningEffortGrok:
    """Cover Grok-specific reasoning levels without duplicating generic OpenRouter tests."""

    def _isolate_env(self, monkeypatch):
        for variable in (
            "AWS_USE_IMDS",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_REGION_NAME",
            "OPENAI_API_KEY",
        ):
            monkeypatch.delenv(variable, raising=False)

    async def _run(self, monkeypatch, model, global_effort="medium", openrouter=None, custom_llm_provider=""):
        fake_settings = create_mock_settings(global_effort)
        monkeypatch.setattr(
            fake_settings.litellm,
            "custom_llm_provider",
            custom_llm_provider,
            raising=False,
        )
        if openrouter is not None:
            monkeypatch.setattr(
                fake_settings,
                "get",
                lambda key, default=None: {"openrouter": openrouter}.get(key, default),
            )
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
        self._isolate_env(monkeypatch)

        with patch(
            "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
            new_callable=AsyncMock,
        ) as mock_completion:
            mock_completion.return_value = create_mock_acompletion_response()
            handler = LiteLLMAIHandler()
            await handler.chat_completion(model=model, system="test system", user="test user")
            return mock_completion.call_args[1]

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("grok-4.5", {"low", "medium", "high"}),
            ("xai/grok-4.5-latest", {"low", "medium", "high"}),
            ("xai/grok-build-latest", {"low", "medium", "high"}),
            ("xai/grok-4.6", {"low", "medium", "high", "xhigh"}),
            ("openrouter/x-ai/grok-4.6:nitro", {"low", "medium", "high", "xhigh"}),
            ("xai/grok-3-mini", None),
        ],
    )
    def test_grok_reasoning_levels_for(self, model, expected):
        assert LiteLLMAIHandler._grok_reasoning_levels_for(model) == expected

    @pytest.mark.parametrize(
        ("model", "configured", "expected"),
        [
            ("xai/grok-4.6", "none", "low"),
            ("xai/grok-4.6", "minimal", "low"),
            ("xai/grok-4.6", "max", "xhigh"),
            ("xai/grok-4.6", "xhigh", "xhigh"),
            ("xai/grok-4.5", "none", "low"),
            ("xai/grok-4.5", "max", "high"),
            ("xai/grok-4.5", "xhigh", "high"),
            ("xai/grok-4.5", "extreme", "extreme"),
            ("openrouter/google/gemini-2.5-pro", "xhigh", "xhigh"),
        ],
    )
    def test_clamp_grok_reasoning_effort(self, model, configured, expected):
        assert LiteLLMAIHandler._clamp_grok_reasoning_effort(model, configured) == expected

    @pytest.mark.parametrize(
        ("model", "effort", "requires_allowlist"),
        [
            ("grok-4.5", "low", False),
            ("grok-4.5-latest", "low", False),
            ("grok-build-latest", "low", True),
            ("grok-4.6", "xhigh", False),
        ],
    )
    def test_xai_grok_litellm_reasoning_param_support(self, monkeypatch, model, effort, requires_allowlist):
        """Pin LiteLLM 1.98 capability gaps so upgrades expose removable workarounds."""
        monkeypatch.setattr(litellm, "drop_params", False)
        bundled_model_cost = GetModelCostMap.load_local_model_cost_map()
        pinned_model_cost = dict(litellm.model_cost)
        for model_key in (model, f"xai/{model}"):
            if model_key in bundled_model_cost:
                pinned_model_cost[model_key] = bundled_model_cost[model_key]
            else:
                pinned_model_cost.pop(model_key, None)
        try:
            with monkeypatch.context() as registry_patch:
                registry_patch.setattr(litellm, "model_cost", pinned_model_cost)
                litellm_utils._invalidate_model_cost_lowercase_map()
                if requires_allowlist:
                    with pytest.raises(litellm.UnsupportedParamsError):
                        get_optional_params(
                            model=model,
                            custom_llm_provider="xai",
                            reasoning_effort=effort,
                        )

                allowed = ["reasoning_effort"] if requires_allowlist else None
                params = get_optional_params(
                    model=model,
                    custom_llm_provider="xai",
                    reasoning_effort=effort,
                    allowed_openai_params=allowed,
                )
                assert params["reasoning_effort"] == effort
        finally:
            litellm_utils._invalidate_model_cost_lowercase_map()

    def test_openai_gateway_grok_litellm_reasoning_param_support(self, monkeypatch):
        monkeypatch.setattr(litellm, "drop_params", False)
        with pytest.raises(litellm.UnsupportedParamsError):
            get_optional_params(
                model="x-ai/grok-4.6",
                custom_llm_provider="openai",
                reasoning_effort="xhigh",
            )

        params = get_optional_params(
            model="x-ai/grok-4.6",
            custom_llm_provider="openai",
            reasoning_effort="xhigh",
            allowed_openai_params=["reasoning_effort"],
        )
        assert params["reasoning_effort"] == "xhigh"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model",
        [
            "xai/grok-4.5",
            "xai/grok-4.5-latest",
            "xai/grok-build-latest",
            "xai/grok-4.6",
        ],
    )
    async def test_xai_grok_forwards_reasoning_effort(self, monkeypatch, mock_logger, model):
        monkeypatch.setattr(
            litellm,
            "get_supported_openai_params",
            lambda **kwargs: [] if kwargs["model"].endswith("grok-build-latest") else ["reasoning_effort"],
        )
        call_kwargs = await self._run(monkeypatch, model, global_effort="low")

        assert call_kwargs["model"] == model
        assert call_kwargs["reasoning_effort"] == "low"
        if model.endswith("grok-build-latest"):
            assert call_kwargs["allowed_openai_params"] == ["reasoning_effort"]
        else:
            assert "allowed_openai_params" not in call_kwargs
        assert "temperature" in call_kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "configured", "expected"),
        [
            ("xai/grok-4.5", "xhigh", "high"),
            ("xai/grok-4.6", "max", "xhigh"),
        ],
    )
    async def test_xai_grok_clamps_reasoning_effort(self, monkeypatch, mock_logger, model, configured, expected):
        monkeypatch.setattr(litellm, "get_supported_openai_params", lambda **kwargs: ["reasoning_effort"])
        call_kwargs = await self._run(monkeypatch, model, global_effort=configured)

        assert call_kwargs["reasoning_effort"] == expected

    @pytest.mark.asyncio
    async def test_openai_gateway_grok_allows_reasoning_effort(self, monkeypatch, mock_logger):
        monkeypatch.setattr(litellm, "get_supported_openai_params", lambda **kwargs: [])
        call_kwargs = await self._run(monkeypatch, "openai/x-ai/grok-4.6", global_effort="max")

        assert call_kwargs["reasoning_effort"] == "xhigh"
        assert call_kwargs["allowed_openai_params"] == ["reasoning_effort"]

    @pytest.mark.asyncio
    async def test_custom_openai_provider_grok_allows_reasoning_effort(self, monkeypatch, mock_logger):
        probe = MagicMock(return_value=[])
        monkeypatch.setattr(litellm, "get_supported_openai_params", probe)
        call_kwargs = await self._run(
            monkeypatch,
            "grok-4.6",
            global_effort="max",
            custom_llm_provider="openai",
        )

        assert call_kwargs["reasoning_effort"] == "xhigh"
        assert call_kwargs["allowed_openai_params"] == ["reasoning_effort"]
        assert call_kwargs["custom_llm_provider"] == "openai"
        probe.assert_called_once_with(model="grok-4.6", custom_llm_provider="openai")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "configured", "expected"),
        [
            ("openrouter/x-ai/grok-4.5", "xhigh", "high"),
            ("openrouter/x-ai/grok-4.6", "xhigh", "xhigh"),
            ("openrouter/x-ai/grok-4.6:nitro", "max", "xhigh"),
            ("openrouter/x-ai/grok-4.6", "none", "low"),
        ],
    )
    async def test_openrouter_grok_clamps_final_effort(
        self, monkeypatch, mock_logger, model, configured, expected
    ):
        call_kwargs = await self._run(
            monkeypatch,
            model,
            global_effort="medium",
            openrouter={"reasoning_effort": configured},
        )

        assert call_kwargs["model"] == model
        assert "reasoning_effort" not in call_kwargs
        assert call_kwargs["extra_body"]["reasoning"] == {"effort": expected}

    @pytest.mark.asyncio
    async def test_openrouter_grok_budget_overrides_clamped_none(self, monkeypatch, mock_logger):
        call_kwargs = await self._run(
            monkeypatch,
            "openrouter/x-ai/grok-4.6",
            global_effort="high",
            openrouter={"reasoning_effort": "none", "reasoning_max_tokens": 8000},
        )

        assert call_kwargs["extra_body"]["reasoning"] == {"max_tokens": 8000}

    @pytest.mark.asyncio
    async def test_openrouter_grok_invalid_override_falls_back_to_global(self, monkeypatch, mock_logger):
        call_kwargs = await self._run(
            monkeypatch,
            "openrouter/x-ai/grok-4.6",
            global_effort="high",
            openrouter={"reasoning_effort": "extreme"},
        )

        assert call_kwargs["extra_body"]["reasoning"] == {"effort": "high"}
