import asyncio
import inspect
from unittest.mock import AsyncMock, patch

import openai
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler


def create_mock_settings(
    custom_llm_provider=None,
    force_streaming_custom_llm_provider="openai",
    force_streaming_api_base_substrings=None,
    openai_key=None,
    anthropic_key=None,
    deployment_id=None,
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
            "OPENAI.KEY": openai_key,
            "ANTHROPIC.KEY": anthropic_key,
            "OPENAI.DEPLOYMENT_ID": deployment_id,
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
                    "reasoning_effort": None,
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


@pytest.fixture(autouse=True)
def restore_anthropic_bridge():
    anthropic_get_api_key = inspect.getattr_static(litellm_handler.AnthropicModelInfo, "get_api_key")
    anthropic_get_auth_token = inspect.getattr_static(litellm_handler.AnthropicModelInfo, "get_auth_token")
    module_get_api_key = litellm_handler._anthropic_get_api_key
    module_get_auth_token = litellm_handler._anthropic_get_auth_token
    yield
    litellm_handler.AnthropicModelInfo.get_api_key = anthropic_get_api_key
    litellm_handler.AnthropicModelInfo.get_auth_token = anthropic_get_auth_token
    litellm_handler._anthropic_get_api_key = module_get_api_key
    litellm_handler._anthropic_get_auth_token = module_get_auth_token


@pytest.mark.parametrize("model", ("claude-sonnet-4-5", "gpt-5.1"))
@pytest.mark.asyncio
async def test_custom_llm_provider_is_forwarded_without_rewriting_model(monkeypatch, model):
    fake_settings = create_mock_settings(" OpenAI ")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as mock_completion:
        mock_completion.return_value = create_mock_acompletion_response()

        handler = LiteLLMAIHandler()
        handler.azure = True
        await handler.chat_completion(
            model=model,
            system="test system",
            user="test user",
        )

        call_kwargs = mock_completion.call_args[1]
        assert call_kwargs["model"] == model
        assert call_kwargs["custom_llm_provider"] == "openai"


@pytest.mark.parametrize("custom_llm_provider", ("azure", "azure_text"))
@pytest.mark.asyncio
async def test_custom_azure_text_model_preserves_deployment_routing(monkeypatch, custom_llm_provider):
    fake_settings = create_mock_settings(custom_llm_provider, deployment_id="custom-deployment")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as mock_completion:
        mock_completion.return_value = create_mock_acompletion_response()
        handler = LiteLLMAIHandler()
        handler.azure = True

        await handler.chat_completion(
            model="azure_text/gpt-35-turbo-instruct",
            system="test system",
            user="test user",
        )

    call_kwargs = mock_completion.call_args.kwargs
    assert call_kwargs["model"] == "azure_text/custom-deployment"
    assert call_kwargs["custom_llm_provider"] == custom_llm_provider
    assert "deployment_id" not in call_kwargs


@pytest.mark.asyncio
async def test_bare_custom_azure_text_model_uses_deployment_as_model(monkeypatch):
    fake_settings = create_mock_settings("azure_text", deployment_id="custom-deployment")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as mock_completion:
        mock_completion.return_value = create_mock_acompletion_response()
        await LiteLLMAIHandler().chat_completion(
            model="gpt-35-turbo-instruct",
            system="test system",
            user="test user",
        )

    call_kwargs = mock_completion.call_args.kwargs
    assert call_kwargs["model"] == "azure_text/custom-deployment"
    assert call_kwargs["custom_llm_provider"] == "azure_text"
    assert "deployment_id" not in call_kwargs


@pytest.mark.asyncio
async def test_custom_llm_provider_selects_request_credentials(monkeypatch):
    fake_settings = create_mock_settings(
        "openai",
        openai_key="request-openai-key",
        anthropic_key="unrelated-anthropic-key",
    )
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai-key")

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
        assert call_kwargs["api_key"] == "request-openai-key"
        assert call_kwargs["custom_llm_provider"] == "openai"


@pytest.mark.asyncio
async def test_custom_llm_provider_is_isolated_between_handlers(monkeypatch):
    openai_settings = create_mock_settings("openai", openai_key="openai-key")
    anthropic_settings = create_mock_settings("anthropic", anthropic_key="anthropic-key")
    active_settings = openai_settings
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: active_settings)

    openai_handler = LiteLLMAIHandler()
    active_settings = anthropic_settings
    anthropic_handler = LiteLLMAIHandler()

    calls = {}
    both_started = asyncio.Event()
    started = 0

    async def completion(**kwargs):
        nonlocal started
        calls[kwargs["messages"][1]["content"]] = kwargs
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return create_mock_acompletion_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=completion):
        await asyncio.gather(
            openai_handler.chat_completion(model="hosted-model", system="sys", user="openai"),
            anthropic_handler.chat_completion(model="hosted-model", system="sys", user="anthropic"),
        )

    assert calls["openai"]["custom_llm_provider"] == "openai"
    assert calls["openai"]["api_key"] == "openai-key"
    assert calls["anthropic"]["custom_llm_provider"] == "anthropic"
    assert calls["anthropic"]["api_key"] == "anthropic-key"


@pytest.mark.asyncio
async def test_probe_uses_custom_llm_provider_captured_at_handler_creation(monkeypatch):
    openai_settings = create_mock_settings("openai", openai_key="openai-key")
    anthropic_settings = create_mock_settings("anthropic", anthropic_key="anthropic-key")
    active_settings = openai_settings
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: active_settings)
    handler = LiteLLMAIHandler()
    active_settings = anthropic_settings
    completion = AsyncMock(return_value=create_mock_acompletion_response())

    await handler.probe_completion("hosted-model", _completion=completion)

    assert completion.call_args.kwargs["custom_llm_provider"] == "openai"
    assert completion.call_args.kwargs["api_key"] == "openai-key"


@pytest.mark.parametrize("experimental_chat_handler", (False, True))
@pytest.mark.asyncio
async def test_text_completion_openai_custom_provider_blocks_global_headers(monkeypatch, experimental_chat_handler):
    fake_settings = create_mock_settings("text-completion-openai", openai_key="request-openai-key")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(litellm_handler.litellm, "headers", {"Authorization": "Bearer stale-key"})
    monkeypatch.delenv("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", raising=False)
    if experimental_chat_handler:
        monkeypatch.setenv("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", "true")

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as mock_completion:
        mock_completion.return_value = create_mock_acompletion_response()

        await LiteLLMAIHandler().chat_completion(
            model="gpt-3.5-turbo-instruct",
            system="test system",
            user="test user",
        )

    call_kwargs = mock_completion.call_args.kwargs
    assert call_kwargs["api_key"] == "request-openai-key"
    assert call_kwargs["custom_llm_provider"] == "text-completion-openai"
    assert set(call_kwargs["headers"]) == {"OpenAI-Organization", "OpenAI-Project"}
    assert isinstance(call_kwargs["headers"]["OpenAI-Organization"], openai.Omit)
    assert isinstance(call_kwargs["headers"]["OpenAI-Project"], openai.Omit)


@pytest.mark.asyncio
async def test_custom_anthropic_provider_uses_request_local_auth_token(monkeypatch):
    fake_settings = create_mock_settings("anthropic")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: fake_settings)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "request-auth-token")

    async def completion(**kwargs):
        assert kwargs["api_key"] == litellm_handler.DUMMY_LITELLM_API_KEY
        assert litellm_handler.AnthropicModelInfo.get_api_key(kwargs["api_key"]) is None
        assert litellm_handler.AnthropicModelInfo.get_auth_token() == "request-auth-token"
        return create_mock_acompletion_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=completion):
        await LiteLLMAIHandler().chat_completion(
            model="hosted-model",
            system="test system",
            user="test user",
        )


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
        mock_stream_handler.return_value = ("test", "stop", None)
        handler = LiteLLMAIHandler()
        result = await handler._get_completion(
            model="claude-sonnet-4-5",
            messages=[],
            timeout=120,
            api_base="https://example-account.snowflakecomputing.com/api/v2/cortex/v1",
            custom_llm_provider="openai",
        )

        assert result == ("test", "stop", None)
        call_kwargs = mock_completion.call_args[1]
        assert call_kwargs["stream"] is True
        assert call_kwargs["stream_options"] == {"include_usage": True}
        assert mock_stream_handler.call_args.kwargs["model"] == "claude-sonnet-4-5"


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
        mock_stream_handler.return_value = ("test", "stop", None)
        handler = LiteLLMAIHandler()
        result = await handler._get_completion(
            model="claude-sonnet-4-5",
            messages=[],
            timeout=120,
            api_base="https://example-account.snowflakecomputing.com/api/v2/cortex/v1",
            custom_llm_provider=" OpenAI ",
        )

        assert result == ("test", "stop", None)
        call_kwargs = mock_completion.call_args[1]
        assert call_kwargs["stream"] is True
        assert call_kwargs["stream_options"] == {"include_usage": True}


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
