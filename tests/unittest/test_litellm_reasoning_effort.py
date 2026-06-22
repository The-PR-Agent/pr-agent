from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

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
