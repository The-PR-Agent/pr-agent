from unittest.mock import AsyncMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler


def create_mock_settings(
    custom_llm_provider=None,
    force_streaming_custom_llm_provider="openai",
    force_streaming_api_base_substrings=None,
):
    if force_streaming_api_base_substrings is None:
        force_streaming_api_base_substrings = ["snowflakecomputing.com"]

    litellm_settings = type("", (), {"get": lambda self, key, default=None: default})()
    if custom_llm_provider is not None:
        litellm_settings.custom_llm_provider = custom_llm_provider
    litellm_settings.force_streaming_custom_llm_provider = force_streaming_custom_llm_provider
    litellm_settings.force_streaming_api_base_substrings = force_streaming_api_base_substrings

    def get_value(key, default=None):
        values = {
            "LITELLM.CUSTOM_LLM_PROVIDER": custom_llm_provider,
            "litellm.custom_llm_provider": custom_llm_provider,
            "LITELLM.FORCE_STREAMING_CUSTOM_LLM_PROVIDER": force_streaming_custom_llm_provider,
            "litellm.force_streaming_custom_llm_provider": force_streaming_custom_llm_provider,
            "LITELLM.FORCE_STREAMING_API_BASE_SUBSTRINGS": force_streaming_api_base_substrings,
            "litellm.force_streaming_api_base_substrings": force_streaming_api_base_substrings,
        }
        return values.get(key, default)

    return type(
        "",
        (),
        {
            "config": type(
                "",
                (),
                {
                    "ai_timeout": 120,
                    "custom_reasoning_model": False,
                    "verbosity_level": 0,
                    "get": lambda self, key, default=None: default,
                },
            )(),
            "litellm": litellm_settings,
            "get": staticmethod(get_value),
        },
    )()


def create_mock_acompletion_response():
    response_payload = {
        "choices": [{"message": {"content": "test"}, "finish_reason": "stop"}]
    }

    class MockCompletionResponse(dict):
        def dict(self):
            return dict(self)

    return MockCompletionResponse(response_payload)


@pytest.mark.asyncio
async def test_custom_llm_provider_is_forwarded_without_rewriting_model(monkeypatch):
    fake_settings = create_mock_settings(" OpenAI ")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as mock_completion:
        mock_completion.return_value = create_mock_acompletion_response()

        handler = LiteLLMAIHandler()
        await handler.chat_completion(
            model="claude-sonnet-4-5",
            system="test system",
            user="test user",
        )

        call_kwargs = mock_completion.call_args[1]
        assert call_kwargs["model"] == "claude-sonnet-4-5"
        assert call_kwargs["custom_llm_provider"] == "openai"


@pytest.mark.asyncio
async def test_custom_llm_provider_is_omitted_when_unset(monkeypatch):
    fake_settings = create_mock_settings()
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as mock_completion:
        mock_completion.return_value = create_mock_acompletion_response()

        handler = LiteLLMAIHandler()
        await handler.chat_completion(
            model="claude-sonnet-4-5",
            system="test system",
            user="test user",
        )

        call_kwargs = mock_completion.call_args[1]
        assert "custom_llm_provider" not in call_kwargs


@pytest.mark.asyncio
async def test_openai_compatible_endpoint_calls_force_streaming(monkeypatch):
    fake_settings = create_mock_settings("openai")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with (
        patch(
            "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
            new_callable=AsyncMock,
        ) as mock_completion,
        patch(
            "pr_agent.algo.ai_handlers.litellm_ai_handler._handle_streaming_response",
            new_callable=AsyncMock,
        ) as mock_stream_handler,
    ):
        mock_stream_handler.return_value = ("test", "stop")
        handler = LiteLLMAIHandler()
        await handler._get_completion(
            model="claude-sonnet-4-5",
            messages=[],
            timeout=120,
            api_base="https://example-account.snowflakecomputing.com/api/v2/cortex/v1",
            custom_llm_provider="openai",
        )

        call_kwargs = mock_completion.call_args[1]
        assert call_kwargs["stream"] is True


@pytest.mark.asyncio
async def test_openai_compatible_endpoint_normalizes_custom_provider_for_streaming(monkeypatch):
    fake_settings = create_mock_settings(" OpenAI ")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with (
        patch(
            "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
            new_callable=AsyncMock,
        ) as mock_completion,
        patch(
            "pr_agent.algo.ai_handlers.litellm_ai_handler._handle_streaming_response",
            new_callable=AsyncMock,
        ) as mock_stream_handler,
    ):
        mock_stream_handler.return_value = ("test", "stop")
        handler = LiteLLMAIHandler()
        await handler._get_completion(
            model="claude-sonnet-4-5",
            messages=[],
            timeout=120,
            api_base="https://example-account.snowflakecomputing.com/api/v2/cortex/v1",
            custom_llm_provider=" OpenAI ",
        )

        call_kwargs = mock_completion.call_args[1]
        assert call_kwargs["stream"] is True


@pytest.mark.asyncio
async def test_openai_compatible_endpoint_ignores_non_string_api_base(monkeypatch):
    fake_settings = create_mock_settings("openai")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as mock_completion:
        mock_completion.return_value = create_mock_acompletion_response()

        handler = LiteLLMAIHandler()
        await handler._get_completion(
            model="claude-sonnet-4-5",
            messages=[],
            timeout=120,
            api_base=123,
            custom_llm_provider="openai",
        )

        call_kwargs = mock_completion.call_args[1]
        assert "stream" not in call_kwargs


@pytest.mark.asyncio
async def test_force_streaming_is_settings_driven(monkeypatch):
    fake_settings = create_mock_settings(
        "openai",
        force_streaming_custom_llm_provider="openai",
        force_streaming_api_base_substrings=["example-gateway.local"],
    )
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as mock_completion:
        mock_completion.return_value = create_mock_acompletion_response()

        handler = LiteLLMAIHandler()
        await handler._get_completion(
            model="claude-sonnet-4-5",
            messages=[],
            timeout=120,
            api_base="https://example-account.snowflakecomputing.com/api/v2/cortex/v1",
            custom_llm_provider="openai",
        )

        call_kwargs = mock_completion.call_args[1]
        assert "stream" not in call_kwargs


@pytest.mark.asyncio
async def test_force_streaming_requires_non_empty_provider_setting(monkeypatch):
    fake_settings = create_mock_settings(
        "openai",
        force_streaming_custom_llm_provider="",
        force_streaming_api_base_substrings=["snowflakecomputing.com"],
    )
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as mock_completion:
        mock_completion.return_value = create_mock_acompletion_response()

        handler = LiteLLMAIHandler()
        await handler._get_completion(
            model="claude-sonnet-4-5",
            messages=[],
            timeout=120,
            api_base="https://example-account.snowflakecomputing.com/api/v2/cortex/v1",
            custom_llm_provider="",
        )

        call_kwargs = mock_completion.call_args[1]
        assert "stream" not in call_kwargs


@pytest.mark.asyncio
async def test_force_streaming_ignores_non_collection_substring_setting(monkeypatch):
    fake_settings = create_mock_settings(
        "openai",
        force_streaming_custom_llm_provider="openai",
        force_streaming_api_base_substrings="snowflakecomputing.com",
    )
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as mock_completion:
        mock_completion.return_value = create_mock_acompletion_response()

        handler = LiteLLMAIHandler()
        await handler._get_completion(
            model="claude-sonnet-4-5",
            messages=[],
            timeout=120,
            api_base="https://example-account.snowflakecomputing.com/api/v2/cortex/v1",
            custom_llm_provider="openai",
        )

        call_kwargs = mock_completion.call_args[1]
        assert "stream" not in call_kwargs


@pytest.mark.asyncio
async def test_force_streaming_warns_on_invalid_substring_setting(monkeypatch):
    fake_settings = create_mock_settings(
        "openai",
        force_streaming_custom_llm_provider="openai",
        force_streaming_api_base_substrings="snowflakecomputing.com",
    )
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with (
        patch(
            "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
            new_callable=AsyncMock,
        ) as mock_completion,
        patch("pr_agent.algo.ai_handlers.litellm_ai_handler.get_logger") as mock_logger,
    ):
        mock_completion.return_value = create_mock_acompletion_response()
        handler = LiteLLMAIHandler()
        await handler._get_completion(
            model="claude-sonnet-4-5",
            messages=[],
            timeout=120,
            api_base="https://example-account.snowflakecomputing.com/api/v2/cortex/v1",
            custom_llm_provider="openai",
        )

        mock_logger.return_value.warning.assert_called_once_with(
            "LITELLM.FORCE_STREAMING_API_BASE_SUBSTRINGS must be a list, tuple, or set. "
            "Ignoring invalid value."
        )
