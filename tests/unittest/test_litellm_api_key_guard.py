"""Tests for request-local LiteLLM provider credentials and endpoints."""

import asyncio
import atexit
import inspect
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import litellm
import openai
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
import pr_agent.algo.ai_handlers.litellm_helpers as litellm_helpers
from pr_agent.algo.ai_handlers.litellm_ai_handler import DUMMY_LITELLM_API_KEY, LiteLLMAIHandler


def _make_settings(overrides=None):
    overrides = overrides or {}
    return type("Settings", (), {
        "config": type("Config", (), {
            "reasoning_effort": None,
            "ai_timeout": 30,
            "custom_reasoning_model": False,
            "max_model_tokens": 32000,
            "verbosity_level": 0,
            "seed": -1,
            "get": lambda self, key, default=None: default,
        })(),
        "litellm": type("LiteLLM", (), {
            "extra_headers": overrides.get("LITELLM.EXTRA_HEADERS"),
            "get": lambda self, key, default=None: default,
        })(),
        "get": lambda self, key, default=None: overrides.get(key, default),
    })()


def _mock_response():
    mock = MagicMock()
    mock.__getitem__ = lambda self, key: {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
    }[key]
    mock.dict.return_value = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    return mock


@pytest.fixture(autouse=True)
def isolate_provider_state(monkeypatch):
    anthropic_get_api_key = inspect.getattr_static(litellm_handler.AnthropicModelInfo, "get_api_key")
    anthropic_get_auth_token = inspect.getattr_static(litellm_handler.AnthropicModelInfo, "get_auth_token")
    module_get_api_key = litellm_handler._anthropic_get_api_key
    module_get_auth_token = litellm_handler._anthropic_get_auth_token
    bedrock_mantle_auth_mixin = litellm_handler.BedrockMantleAuthMixin
    module_bedrock_mantle_sign_request = litellm_handler._bedrock_mantle_sign_request
    module_bedrock_mantle_resolve_bearer_token = litellm_handler._bedrock_mantle_resolve_bearer_token
    if bedrock_mantle_auth_mixin is not None:
        bedrock_mantle_sign_request = inspect.getattr_static(
            bedrock_mantle_auth_mixin,
            "sign_request",
        )
        bedrock_mantle_resolve_bearer_token = inspect.getattr_static(
            bedrock_mantle_auth_mixin,
            "_resolve_bearer_token",
        )
    provider_env_vars = {
        variable
        for variables in litellm_handler.PROVIDER_API_KEY_ENV_VARS.values()
        for variable in variables
    } | {
        config.api_key_env
        for provider in litellm_handler.JSONProviderRegistry.list_providers()
        if (
            (config := litellm_handler.JSONProviderRegistry.get(provider)) is not None
            and config.api_key_env
        )
    } | {
        variable
        for variables in litellm_handler.PROVIDER_API_BASE_ENV_VARS.values()
        for variable in variables
    } | {
        variable
        for provider_variables in litellm_handler.PROVIDER_ROUTING_ENV_VARS.values()
        for variables in provider_variables.values()
        for variable in variables
    } | {
        config.api_base_env
        for provider in litellm_handler.JSONProviderRegistry.list_providers()
        if (
            (config := litellm_handler.JSONProviderRegistry.get(provider)) is not None
            and config.api_base_env
        )
    } | set(litellm_handler.AWS_CREDENTIAL_CHAIN_ENV_VARS) | {
        "AWS_USE_IMDS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION_NAME",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_BEDROCK_RUNTIME_ENDPOINT",
        "BEDROCK_MANTLE_REGION",
        "ANTHROPIC_AUTH_TOKEN",
        "MOONSHOT_API_BASE",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "AZURE_API_BASE",
        "AZURE_API_VERSION",
        "AZURE_AD_TOKEN",
        "AZURE_OPENAI_AD_TOKEN",
        "OPENROUTER_API_BASE",
        "VERTEXAI_PROJECT",
        "VERTEXAI_LOCATION",
        "VERTEXAI_CREDENTIALS",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENAI_PROJECT_ID",
        "EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER",
    }
    for variable in provider_env_vars:
        monkeypatch.delenv(variable, raising=False)
    provider_global_names = {
        name
        for names in litellm_handler.PROVIDER_API_KEY_GLOBALS.values()
        for name in names
    }
    for name in provider_global_names | {
        "api_key",
        "openai_key",
        "api_base",
        "api_version",
        "headers",
        "organization",
        "vertex_project",
        "vertex_location",
    }:
        monkeypatch.setattr(litellm, name, None, raising=False)
    monkeypatch.setattr(openai, "api_key", None)
    monkeypatch.setattr(litellm_handler, "get_settings", _make_settings)
    yield
    litellm_handler.AnthropicModelInfo.get_api_key = anthropic_get_api_key
    litellm_handler.AnthropicModelInfo.get_auth_token = anthropic_get_auth_token
    litellm_handler._anthropic_get_api_key = module_get_api_key
    litellm_handler._anthropic_get_auth_token = module_get_auth_token
    if bedrock_mantle_auth_mixin is not None:
        bedrock_mantle_auth_mixin.sign_request = bedrock_mantle_sign_request
        bedrock_mantle_auth_mixin._resolve_bearer_token = bedrock_mantle_resolve_bearer_token
    litellm_handler._bedrock_mantle_sign_request = module_bedrock_mantle_sign_request
    litellm_handler._bedrock_mantle_resolve_bearer_token = module_bedrock_mantle_resolve_bearer_token


async def _call(handler, model):
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        await handler.chat_completion(model=model, system="sys", user="usr")
    return mock_call.call_args.kwargs


def test_provider_environment_tables_cover_known_or_legacy_transports():
    # LiteLLM retains explicit transports for these providers outside provider_list.
    legacy_transports = {"aleph_alpha", "anyscale"}
    known_providers = set(litellm.provider_list) | set(litellm_handler.JSONProviderRegistry.list_providers())
    for table in (litellm_handler.PROVIDER_API_KEY_ENV_VARS, litellm_handler.PROVIDER_API_BASE_ENV_VARS):
        assert set(table) <= known_providers | legacy_transports


@pytest.mark.parametrize("provider", ("aleph_alpha", "anyscale"))
@pytest.mark.parametrize("initial_key", (None, "handler-key"))
@pytest.mark.asyncio
async def test_legacy_provider_transport_uses_request_local_credentials(monkeypatch, provider, initial_key):
    from litellm.litellm_core_utils import logging_worker

    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    monkeypatch.setattr(openai, "organization", None)
    settings = _make_settings()
    settings.litellm.custom_llm_provider = provider
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    key_variable = "ALEPH_ALPHA_API_KEY" if provider == "aleph_alpha" else "ANYSCALE_API_KEY"
    base_variable = "ALEPH_ALPHA_API_BASE" if provider == "aleph_alpha" else "ANYSCALE_API_BASE"
    api_base = "https://handler.example/v1"
    monkeypatch.setenv(base_variable, api_base)
    if initial_key:
        monkeypatch.setenv(key_variable, initial_key)
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(key_variable, "another-handler-key")
    monkeypatch.setenv(base_variable, "https://another-handler.example/v1")
    monkeypatch.setattr(litellm, "api_key", "another-global-key")
    monkeypatch.setattr(litellm, "aleph_alpha_key", "another-aleph-key")
    captured = []

    def respond(request):
        captured.append(request)
        if provider == "aleph_alpha":
            payload = {"completions": [{"completion": "ok", "finish_reason": "stop"}]}
        else:
            payload = {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(200, request=request, json=payload)

    def post(url, *, headers, data, stream):
        return respond(httpx.Request("POST", url, headers=headers, content=data))

    async def send(client, request, **kwargs):
        return respond(request)

    # Keep PR-Agent and LiteLLM dispatch/authentication real; intercept only HTTP.
    monkeypatch.setattr(litellm.module_level_client, "post", post)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    try:
        content, finish_reason = await handler.chat_completion(model="model", system="sys", user="usr")
    finally:
        try:
            # LiteLLM schedules the task that enqueues logging after completion.
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()

    assert (content, finish_reason) == ("ok", "stop")
    assert len(captured) == 1
    expected_url = api_base if provider == "aleph_alpha" else f"{api_base}/chat/completions"
    assert str(captured[0].url) == expected_url
    assert captured[0].headers["authorization"] == f"Bearer {initial_key or DUMMY_LITELLM_API_KEY}"


@pytest.mark.asyncio
async def test_keyless_openai_placeholder_is_request_local(monkeypatch):
    handler = LiteLLMAIHandler()

    kwargs = await _call(handler, "gpt-4o")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY
    assert litellm.api_key is None
    assert litellm.openai_key is None
    assert openai.api_key is None


def test_keyless_registry_provider_does_not_read_a_missing_environment_name(monkeypatch):
    provider_config = type("ProviderConfig", (), {"api_key_env": None})()
    registry = MagicMock()
    registry.list_providers.return_value = ["keyless"]
    registry.get.return_value = provider_config
    monkeypatch.setattr(litellm_handler, "JSONProviderRegistry", registry)

    handler = LiteLLMAIHandler()

    assert handler._provider_environment_api_keys == {}
    assert litellm_handler._has_live_provider_api_key_environment("keyless") is False


@pytest.mark.asyncio
async def test_native_openai_key_is_forwarded_explicitly(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "native-openai-key")
    monkeypatch.setattr(litellm, "api_key", "another-request-key")
    handler = LiteLLMAIHandler()

    kwargs = await _call(handler, "gpt-4o")

    assert kwargs["api_key"] == "native-openai-key"
    assert os.environ["OPENAI_API_KEY"] == "native-openai-key"
    assert litellm.api_key == "another-request-key"


@pytest.mark.asyncio
async def test_native_openai_like_key_is_not_shadowed_by_placeholder(monkeypatch):
    monkeypatch.setenv("OPENAI_LIKE_API_KEY", "native-openai-like-key")
    monkeypatch.setattr(litellm, "api_key", "another-request-key")

    kwargs = await _call(LiteLLMAIHandler(), "openai_like/my-model")

    assert kwargs["api_key"] == "native-openai-like-key"
    assert litellm.api_key == "another-request-key"


@pytest.mark.asyncio
async def test_native_openai_key_does_not_override_openai_like_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "native-openai-key")
    monkeypatch.setenv("OPENAI_LIKE_API_KEY", "native-openai-like-key")

    kwargs = await _call(LiteLLMAIHandler(), "openai_like/my-model")

    assert kwargs["api_key"] == "native-openai-like-key"


@pytest.mark.asyncio
async def test_configured_openai_key_does_not_override_openai_like_key(monkeypatch):
    overrides = {
        "OPENAI.KEY": "configured-openai-key",
        "OPENAI.API_BASE": "https://gateway.example/v1",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    monkeypatch.setenv("OPENAI_LIKE_API_KEY", "native-openai-like-key")

    kwargs = await _call(LiteLLMAIHandler(), "openai_like/my-model")

    assert kwargs["api_base"] == "https://gateway.example/v1"
    assert kwargs["api_key"] == "native-openai-like-key"


@pytest.mark.asyncio
async def test_configured_openai_key_is_not_sent_to_native_openai_like_endpoint(monkeypatch):
    overrides = {"OPENAI.KEY": "configured-openai-key"}
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    monkeypatch.setenv("OPENAI_LIKE_API_BASE", "https://openai-like.example/v1")

    kwargs = await _call(LiteLLMAIHandler(), "openai_like/my-model")

    assert kwargs["api_base"] == "https://openai-like.example/v1"
    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.asyncio
async def test_configured_openai_key_reaches_same_native_openai_like_endpoint(monkeypatch):
    overrides = {
        "OPENAI.KEY": "configured-openai-key",
        "OPENAI.API_BASE": "https://gateway.example/v1",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    monkeypatch.setenv("OPENAI_LIKE_API_BASE", "https://gateway.example/v1")

    kwargs = await _call(LiteLLMAIHandler(), "openai_like/my-model")

    assert kwargs["api_base"] == "https://gateway.example/v1"
    assert kwargs["api_key"] == "configured-openai-key"


@pytest.mark.asyncio
async def test_keyless_openai_like_does_not_receive_placeholder():
    kwargs = await _call(LiteLLMAIHandler(), "openai_like/my-model")

    assert "api_key" not in kwargs


@pytest.mark.parametrize("fallback", ("litellm_api_key", "litellm_openai_key", "openai_environment"))
@pytest.mark.asyncio
async def test_openai_like_blocks_unrelated_openai_fallback(monkeypatch, fallback):
    if fallback == "litellm_api_key":
        monkeypatch.setattr(litellm, "api_key", "unrelated-openai-key")
    elif fallback == "litellm_openai_key":
        monkeypatch.setattr(litellm, "openai_key", "unrelated-openai-key")
    else:
        monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")

    kwargs = await _call(LiteLLMAIHandler(), "openai_like/my-model")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.parametrize(("model", "environment_variable"), (
    ("amazon_nova/model", "AMAZON_NOVA_API_KEY"),
    ("aleph_alpha/model", "ALEPHALPHA_API_KEY"),
    ("anthropic/claude-x", "ANTHROPIC_API_KEY"),
    ("azure/gpt-4o", "AZURE_API_KEY"),
    ("azure_ai/gpt-4o", "AZURE_AI_API_KEY"),
    ("bedrock_mantle/openai.gpt-oss-120b", "AWS_BEARER_TOKEN_BEDROCK"),
    ("codestral/codestral-latest", "CODESTRAL_API_KEY"),
    ("cohere/command-r", "CO_API_KEY"),
    ("cohere_chat/command-r", "COHERE_API_KEY"),
    ("cloudflare/model", "CLOUDFLARE_API_KEY"),
    ("compactifai/model", "COMPACTIFAI_API_KEY"),
    ("dashscope/qwen-max", "DASHSCOPE_API_KEY"),
    ("databricks/model", "DATABRICKS_API_KEY"),
    ("datarobot/model", "DATAROBOT_API_TOKEN"),
    ("deepinfra/model", "DEEPINFRA_API_KEY"),
    ("deepseek/deepseek-chat", "DEEPSEEK_API_KEY"),
    ("gemini/gemini-2.5-pro", "GEMINI_API_KEY"),
    ("gradient_ai/model", "GRADIENT_AI_API_KEY"),
    ("groq/model", "GROQ_API_KEY"),
    ("heroku/model", "HEROKU_API_KEY"),
    ("huggingface/model", "HF_TOKEN"),
    ("langflow/model", "LANGFLOW_API_KEY"),
    ("langgraph/model", "LANGGRAPH_API_KEY"),
    ("mistral/model", "MISTRAL_API_KEY"),
    ("moonshot/model", "MOONSHOT_API_KEY"),
    ("minimax/model", "MINIMAX_API_KEY"),
    ("lemonade/model", "LEMONADE_API_KEY"),
    ("ollama/model", "OLLAMA_API_KEY"),
    ("openai/gpt-4o", "OPENAI_API_KEY"),
    ("openrouter/model", "OPENROUTER_API_KEY"),
    ("predibase/model", "PREDIBASE_API_KEY"),
    ("replicate/model", "REPLICATE_API_TOKEN"),
    ("sambanova/model", "SAMBANOVA_API_KEY"),
    ("sap/model", "AICORE_SERVICE_KEY"),
    ("text-completion-codestral/codestral-latest", "CODESTRAL_API_KEY"),
    ("text-completion-inception/model", "INCEPTION_API_KEY"),
    ("veniceai/model", "VENICE_AI_API_KEY"),
    ("watsonx/model", "WATSONX_API_KEY"),
    ("watsonx_text/model", "WX_API_KEY"),
    ("xai/model", "XAI_API_KEY"),
    ("xiaomi_mimo/model", "XIAOMI_MIMO_API_KEY"),
    ("zai/model", "ZAI_API_KEY"),
))
@pytest.mark.asyncio
async def test_native_provider_key_shadows_residual_global(monkeypatch, model, environment_variable):
    monkeypatch.setenv(environment_variable, "native-provider-key")
    monkeypatch.setattr(litellm, "api_key", "another-request-key")

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["api_key"] == "native-provider-key"
    assert litellm.api_key == "another-request-key"


@pytest.mark.asyncio
async def test_compatible_provider_key_shadows_residual_global(monkeypatch):
    monkeypatch.setenv("TOGETHERAI_API_KEY", "native-together-key")
    monkeypatch.setattr(litellm, "api_key", "another-request-key")
    resolve_provider = MagicMock(side_effect=AssertionError("explicit providers must use the environment snapshot"))
    monkeypatch.setattr(litellm, "get_llm_provider", resolve_provider)

    kwargs = await _call(LiteLLMAIHandler(), "together_ai/model")

    assert kwargs["api_key"] == "native-together-key"
    assert litellm.api_key == "another-request-key"
    resolve_provider.assert_not_called()


@pytest.mark.asyncio
async def test_keyless_compatible_provider_blocks_residual_global(monkeypatch):
    monkeypatch.setattr(litellm, "api_key", "another-request-key")

    kwargs = await _call(LiteLLMAIHandler(), "together_ai/model")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY
    assert litellm.api_key == "another-request-key"


@pytest.mark.asyncio
async def test_late_native_provider_key_does_not_bypass_handler_snapshot(monkeypatch):
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("GROQ_API_KEY", "later-provider-key")

    kwargs = await _call(handler, "groq/model")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.asyncio
async def test_ollama_native_endpoint_is_frozen_with_its_key(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "native-ollama-key")
    monkeypatch.setenv("OLLAMA_API_BASE", "https://tenant-a.example")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("OLLAMA_API_BASE", "https://tenant-b.example")

    kwargs = await _call(handler, "ollama/model")

    assert kwargs["api_key"] == "native-ollama-key"
    assert kwargs["api_base"] == "https://tenant-a.example"


@pytest.mark.asyncio
async def test_moonshot_native_endpoint_is_frozen_with_its_key(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "native-moonshot-key")
    monkeypatch.setenv("MOONSHOT_API_BASE", "https://tenant-a.example/v1")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("MOONSHOT_API_BASE", "https://tenant-b.example/v1")

    kwargs = await _call(handler, "moonshot/model")

    assert kwargs["api_key"] == "native-moonshot-key"
    assert kwargs["api_base"] == "https://tenant-a.example/v1"


@pytest.mark.asyncio
async def test_late_openai_key_does_not_bypass_openai_like_guard(monkeypatch):
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("OPENAI_API_KEY", "later-openai-key")

    kwargs = await _call(handler, "openai_like/model")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.parametrize(("model", "preferred_variable", "fallback_variable"), (
    ("azure/gpt-4o", "AZURE_OPENAI_API_KEY", "AZURE_API_KEY"),
    ("bedrock_mantle/openai.gpt-oss-120b", "BEDROCK_MANTLE_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"),
    ("cohere/command-r", "COHERE_API_KEY", "CO_API_KEY"),
    ("featherless_ai/model", "FEATHERLESS_AI_API_KEY", "FEATHERLESS_API_KEY"),
    ("fireworks_ai/model", "FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY"),
    ("fireworks_ai/model", "FIREWORKS_AI_API_KEY", "FIREWORKSAI_API_KEY"),
    ("fireworks_ai/model", "FIREWORKSAI_API_KEY", "FIREWORKS_AI_TOKEN"),
    ("friendliai/model", "FRIENDLIAI_API_KEY", "FRIENDLI_TOKEN"),
    ("gemini/gemini-2.5-pro", "GOOGLE_API_KEY", "GEMINI_API_KEY"),
    ("huggingface/model", "HF_TOKEN", "HUGGINGFACE_API_KEY"),
    ("mistral/model", "MISTRAL_AZURE_API_KEY", "MISTRAL_API_KEY"),
    ("openrouter/model", "OPENROUTER_API_KEY", "OR_API_KEY"),
    ("perplexity/model", "PERPLEXITYAI_API_KEY", "PERPLEXITY_API_KEY"),
    ("replicate/model", "REPLICATE_API_KEY", "REPLICATE_API_TOKEN"),
    ("together_ai/model", "TOGETHER_API_KEY", "TOGETHER_AI_API_KEY"),
    ("together_ai/model", "TOGETHER_AI_API_KEY", "TOGETHERAI_API_KEY"),
    ("together_ai/model", "TOGETHERAI_API_KEY", "TOGETHER_AI_TOKEN"),
    ("vercel_ai_gateway/model", "VERCEL_AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN"),
    ("watsonx/model", "WATSONX_APIKEY", "WATSONX_API_KEY"),
    ("watsonx/model", "WATSONX_API_KEY", "WX_API_KEY"),
    ("watsonx_text/model", "WATSONX_APIKEY", "WATSONX_API_KEY"),
    ("watsonx_text/model", "WATSONX_API_KEY", "WX_API_KEY"),
))
@pytest.mark.asyncio
async def test_native_provider_environment_precedence(monkeypatch, model, preferred_variable, fallback_variable):
    monkeypatch.setenv(preferred_variable, "preferred-provider-key")
    monkeypatch.setenv(fallback_variable, "fallback-provider-key")

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["api_key"] == "preferred-provider-key"


@pytest.mark.parametrize("model", ("watsonx/model", "watsonx_text/model"))
@pytest.mark.asyncio
async def test_watsonx_zen_auth_overrides_generic_api_key(monkeypatch, model):
    monkeypatch.setenv("WX_API_KEY", "iam-api-key")
    monkeypatch.setenv("WATSONX_ZENAPIKEY", "zen-api-key")

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["api_key"] == litellm_handler.DUMMY_LITELLM_API_KEY
    assert kwargs["zen_api_key"] == "zen-api-key"


@pytest.mark.asyncio
async def test_watsonx_text_zen_auth_blocks_live_api_key_fallback(monkeypatch):
    monkeypatch.setenv("WATSONX_ZENAPIKEY", "zen-api-key")

    kwargs = await _call(LiteLLMAIHandler(), "watsonx_text/model")

    assert kwargs["api_key"] == litellm_handler.DUMMY_LITELLM_API_KEY
    assert kwargs["zen_api_key"] == "zen-api-key"


@pytest.mark.asyncio
async def test_request_setting_overrides_native_provider_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "native-anthropic-key")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"ANTHROPIC.KEY": "request-anthropic-key"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), "anthropic/claude-x")

    assert kwargs["api_key"] == "request-anthropic-key"


@pytest.mark.asyncio
async def test_anthropic_auth_token_is_request_local(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "request-auth-token")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "another-request-token")

    async def completion(**kwargs):
        assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY
        assert litellm_handler.AnthropicModelInfo.get_api_key(kwargs["api_key"]) is None
        assert litellm_handler.AnthropicModelInfo.get_auth_token("another-request-token") == "request-auth-token"
        return _mock_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=completion):
        await handler.chat_completion(model="anthropic/claude-x", system="sys", user="usr")

    assert litellm_handler.AnthropicModelInfo.get_auth_token() == "another-request-token"


@pytest.mark.asyncio
async def test_keyless_anthropic_does_not_borrow_later_environment_credentials(monkeypatch):
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "another-request-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "another-request-token")

    async def completion(**kwargs):
        assert litellm_handler.AnthropicModelInfo.get_api_key(kwargs["api_key"]) is None
        assert litellm_handler.AnthropicModelInfo.get_auth_token("another-request-token") is None
        return _mock_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=completion):
        await handler.chat_completion(model="anthropic/claude-x", system="sys", user="usr")


@pytest.mark.asyncio
async def test_concurrent_anthropic_auth_tokens_stay_request_local(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tenant-a-token")
    tenant_a = LiteLLMAIHandler()
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tenant-b-token")
    tenant_b = LiteLLMAIHandler()
    captured_tokens = {}

    async def completion(**kwargs):
        await asyncio.sleep(0)
        request_id = kwargs["messages"][-1]["content"]
        captured_tokens[request_id] = litellm_handler.AnthropicModelInfo.get_auth_token()
        return _mock_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=completion):
        await asyncio.gather(
            tenant_a.chat_completion(model="anthropic/claude-x", system="sys", user="tenant-a"),
            tenant_b.chat_completion(model="anthropic/claude-x", system="sys", user="tenant-b"),
        )

    assert captured_tokens == {
        "tenant-a": "tenant-a-token",
        "tenant-b": "tenant-b-token",
    }


@pytest.mark.asyncio
async def test_unrelated_native_provider_key_is_not_forwarded(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "native-anthropic-key")

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.parametrize("model", (
    "anthropic/claude-x",
    "azure/gpt-4o",
    "bedrock_mantle/openai.gpt-oss-120b",
    "veniceai/model",
))
@pytest.mark.asyncio
async def test_provider_without_key_blocks_residual_generic_global(monkeypatch, model):
    monkeypatch.setattr(litellm, "api_key", "another-request-key")
    if model.startswith("bedrock_mantle/"):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.parametrize(("provider", "global_name"), (
    (provider, global_name)
    for provider, global_names in litellm_handler.PROVIDER_API_KEY_GLOBALS.items()
    for global_name in global_names
))
@pytest.mark.asyncio
async def test_provider_without_key_blocks_residual_provider_global(monkeypatch, provider, global_name):
    monkeypatch.setattr(litellm, global_name, "another-request-key")

    kwargs = await _call(LiteLLMAIHandler(), f"{provider}/model")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.parametrize(("provider", "global_name"), (
    ("ai21", "ai21_key"),
    ("baseten", "baseten_key"),
    ("nebius", "nebius_key"),
    ("sap", "sap_service_key"),
    ("together_ai", "togetherai_api_key"),
    ("wandb", "wandb_key"),
))
def test_provider_global_guard_matrix_is_explicit(provider, global_name):
    assert global_name in litellm_handler.PROVIDER_API_KEY_GLOBALS[provider]


@pytest.mark.parametrize("model", ("chatgpt/gpt-5", "github_copilot/gpt-4o"))
@pytest.mark.asyncio
async def test_managed_auth_provider_blocks_generic_credentials(monkeypatch, model):
    monkeypatch.setattr(litellm, "api_key", "another-request-key")
    monkeypatch.setattr(litellm, "openai_key", "another-openai-key")
    monkeypatch.setattr(litellm, "headers", {"Authorization": "Bearer another-request-token"})
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({
            "OPENAI.API_BASE": "https://gateway.example/v1",
            "OPENAI.API_VERSION": "request-version",
            "OPENAI.KEY": "request-openai-key",
            "OPENAI.ORG": "request-organization",
        }),
    )

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY
    assert isinstance(kwargs["headers"]["OpenAI-Organization"], openai.Omit)
    assert isinstance(kwargs["headers"]["OpenAI-Project"], openai.Omit)
    assert not {"api_base", "api_version", "organization"} & kwargs.keys()


@pytest.mark.parametrize(
    "fallback",
    ("litellm_api_key", "litellm_openai_key", "openai_environment", "openai_sdk_key"),
)
@pytest.mark.asyncio
async def test_unknown_provider_blocks_residual_openai_credentials(monkeypatch, fallback):
    if fallback == "litellm_api_key":
        monkeypatch.setattr(litellm, "api_key", "another-request-key")
    elif fallback == "litellm_openai_key":
        monkeypatch.setattr(litellm, "openai_key", "another-request-key")
    elif fallback == "openai_environment":
        monkeypatch.setenv("OPENAI_API_KEY", "another-request-key")
    else:
        monkeypatch.setattr(openai, "api_key", "another-request-key")

    kwargs = await _call(LiteLLMAIHandler(), "future_provider/model")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.asyncio
async def test_unknown_provider_blocks_openai_environment_added_after_handler_creation(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.API_BASE": "https://gateway.example/v1"}),
    )
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("OPENAI_API_KEY", "another-request-key")

    kwargs = await _call(handler, "future_provider/model")

    assert kwargs["api_base"] == "https://gateway.example/v1"
    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.asyncio
async def test_keyless_gateway_gets_request_local_placeholder(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.API_BASE": "https://gateway.example/v1"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), "future_provider/model")

    assert kwargs["api_base"] == "https://gateway.example/v1"
    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.parametrize("model", ("hosted_vllm/model", "lm_studio/model"))
@pytest.mark.asyncio
async def test_keyless_compatible_gateway_gets_request_local_placeholder(monkeypatch, model):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.API_BASE": "https://gateway.example/v1"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["api_base"] == "https://gateway.example/v1"
    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.parametrize("model", ("hosted_vllm/model", "lm_studio/model"))
@pytest.mark.asyncio
async def test_compatible_gateway_forwards_request_local_openai_environment_key(monkeypatch, model):
    monkeypatch.setenv("OPENAI_API_KEY", "gateway-key")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.API_BASE": "https://gateway.example/v1"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["api_base"] == "https://gateway.example/v1"
    assert kwargs["api_key"] == "gateway-key"


@pytest.mark.parametrize(("model", "environment_variable"), (
    ("hosted_vllm/model", "HOSTED_VLLM_API_BASE"),
    ("lm_studio/model", "LM_STUDIO_API_BASE"),
))
@pytest.mark.asyncio
async def test_compatible_native_endpoint_is_frozen_with_keyless_placeholder(monkeypatch, model, environment_variable):
    monkeypatch.setenv(environment_variable, "https://tenant-a.example/v1")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "https://tenant-b.example/v1")

    kwargs = await _call(handler, model)

    assert kwargs["api_base"] == "https://tenant-a.example/v1"
    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.parametrize(("model", "api_base_variable", "api_key_variable"), (
    ("hosted_vllm/model", "HOSTED_VLLM_API_BASE", "HOSTED_VLLM_API_KEY"),
    ("lm_studio/model", "LM_STUDIO_API_BASE", "LM_STUDIO_API_KEY"),
))
@pytest.mark.asyncio
async def test_compatible_native_endpoint_preserves_native_key(
    monkeypatch,
    model,
    api_base_variable,
    api_key_variable,
):
    monkeypatch.setenv(api_base_variable, "https://native.example/v1")
    monkeypatch.setenv(api_key_variable, "native-key")

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["api_base"] == "https://native.example/v1"
    assert kwargs["api_key"] == "native-key"


@pytest.mark.parametrize(("model", "environment_variable"), (
    ("hosted_vllm/model", "HOSTED_VLLM_API_BASE"),
    ("lm_studio/model", "LM_STUDIO_API_BASE"),
))
@pytest.mark.asyncio
async def test_compatible_native_endpoint_overrides_openai_gateway(monkeypatch, model, environment_variable):
    monkeypatch.setenv(environment_variable, "https://native.example/v1")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.API_BASE": "https://gateway.example/v1"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["api_base"] == "https://native.example/v1"


@pytest.mark.parametrize(("model", "environment_variable"), (
    ("hosted_vllm/model", "HOSTED_VLLM_API_BASE"),
    ("lm_studio/model", "LM_STUDIO_API_BASE"),
))
@pytest.mark.asyncio
async def test_late_compatible_native_endpoint_is_rejected(monkeypatch, model, environment_variable):
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "https://another-request.example/v1")

    with pytest.raises(ValueError, match="Refusing live api_base environment fallback"):
        await _call(handler, model)


@pytest.mark.asyncio
async def test_known_unmapped_provider_blocks_residual_generic_global(monkeypatch):
    monkeypatch.setattr(litellm, "api_key", "another-request-key")

    kwargs = await _call(LiteLLMAIHandler(), "vllm/model")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.asyncio
async def test_keyless_custom_openai_placeholder_is_request_local(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.API_BASE": "https://gateway.example/v1"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), "custom_openai/my-model")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY
    assert kwargs["api_base"] == "https://gateway.example/v1"
    assert litellm.api_key is None
    assert litellm.openai_key is None


@pytest.mark.asyncio
async def test_custom_openai_key_is_forwarded_without_gateway_base(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.KEY": "openai-key"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), "custom_openai/my-model")

    assert kwargs["api_key"] == "openai-key"
    assert kwargs["api_base"] == litellm_handler.OPENAI_DEFAULT_API_BASE


@pytest.mark.parametrize("model", ("gpt-4o", "custom_openai/my-model"))
@pytest.mark.parametrize("environment_variable", ("OPENAI_BASE_URL", "OPENAI_API_BASE"))
@pytest.mark.asyncio
async def test_openai_endpoint_added_after_handler_does_not_redirect_request(
    monkeypatch,
    model,
    environment_variable,
):
    monkeypatch.setenv("OPENAI_API_KEY", "request-openai-key")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "https://another-request.example/v1")

    kwargs = await _call(handler, model)

    assert kwargs["api_key"] == "request-openai-key"
    assert kwargs["api_base"] == litellm_handler.OPENAI_DEFAULT_API_BASE


@pytest.mark.asyncio
async def test_native_custom_openai_key_shadows_residual_global(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "native-openai-key")
    monkeypatch.setattr(litellm, "api_key", "another-request-key")

    kwargs = await _call(LiteLLMAIHandler(), "custom_openai/my-model")

    assert kwargs["api_key"] == "native-openai-key"
    assert litellm.api_key == "another-request-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ("openai_like/my-model", "custom_openai/my-model"))
async def test_openai_gateway_credentials_reach_compatible_aliases(monkeypatch, model):
    overrides = {
        "OPENAI.KEY": "openai-key",
        "OPENAI.API_BASE": "https://gateway.example/v1",
        "OPENAI.API_VERSION": "2026-01-01",
        "OPENAI.ORG": "openai-org",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["api_base"] == "https://gateway.example/v1"
    assert kwargs["api_key"] == "openai-key"
    assert "api_version" not in kwargs
    assert "organization" not in kwargs


@pytest.mark.parametrize("global_name", ("api_key", "openai_key"))
@pytest.mark.asyncio
async def test_residual_litellm_global_key_is_not_reused(monkeypatch, global_name):
    monkeypatch.setattr(litellm, global_name, "another-request-key")

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY
    assert getattr(litellm, global_name) == "another-request-key"


@pytest.mark.parametrize(
    ("model", "global_name", "global_value", "error"),
    (
        ("chatgpt/gpt-5", "api_base", "https://another-request.example/v1", "API base"),
        ("github_copilot/gpt-4o", "api_base", "https://another-request.example/v1", "API base"),
        ("azure/gpt-4o", "api_version", "another-request-version", "API version"),
        ("vertex_ai/gemini-2.5-pro", "vertex_project", "another-request-project", "vertex_project"),
        ("vertex_ai/gemini-2.5-pro", "vertex_location", "another-request-location", "vertex_location"),
    ),
)
@pytest.mark.asyncio
async def test_residual_litellm_global_routing_is_rejected(
    monkeypatch,
    model,
    global_name,
    global_value,
    error,
):
    handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, global_name, global_value)

    with pytest.raises(ValueError, match=error):
        await _call(handler, model)

    assert getattr(litellm, global_name) == global_value


@pytest.mark.parametrize("provider", sorted(litellm_handler.LITELLM_GLOBAL_FIRST_API_BASE_PROVIDERS))
def test_request_api_base_does_not_bypass_global_first_litellm_routing_guard(monkeypatch, provider):
    monkeypatch.setattr(litellm, "api_base", "https://another-request.example/v1")

    with pytest.raises(ValueError, match="API base"):
        litellm_handler._guard_request_routing_globals(
            provider,
            {"api_base": "https://request.example/v1"},
        )


@pytest.mark.parametrize(
    ("model", "environment_variable", "environment_value"),
    (
        ("anthropic/claude-x", "ANTHROPIC_API_BASE", "https://another-request.example"),
        ("anthropic/claude-x", "ANTHROPIC_BASE_URL", "https://another-request.example"),
        ("azure_ai/model", "AZURE_AI_API_BASE", "https://another-request.example"),
        ("cohere_chat/model", "COHERE_API_BASE", "https://another-request.example/v2/chat"),
        ("databricks/endpoint", "DATABRICKS_API_BASE", "https://another-request.example"),
        ("gemini/model", "GEMINI_API_BASE", "https://another-request.example"),
        ("groq/model", "GROQ_API_BASE", "https://another-request.example/v1"),
        ("huggingface/model", "HF_API_BASE", "https://another-request.example/v1"),
        ("huggingface/model", "HUGGINGFACE_API_BASE", "https://another-request.example/v1"),
        ("mistral/model", "MISTRAL_AZURE_API_BASE", "https://another-request.example/v1"),
        ("mistral/model", "MISTRAL_API_BASE", "https://another-request.example/v1"),
        ("moonshot/model", "MOONSHOT_API_BASE", "https://another-request.example/v1"),
        ("ollama/model", "OLLAMA_API_BASE", "https://another-request.example"),
        ("openai_like/model", "OPENAI_LIKE_API_BASE", "https://another-request.example/v1"),
        ("openrouter/model", "OPENROUTER_API_BASE", "https://another-request.example/v1"),
        ("text-completion-codestral/codestral-latest", "CODESTRAL_API_BASE", "https://another-request.example/v1"),
        ("azure/gpt-4o", "AZURE_API_BASE", "https://another-request.example"),
        ("azure/gpt-4o", "AZURE_API_VERSION", "another-request-version"),
        ("vertex_ai/gemini-2.5-pro", "VERTEXAI_PROJECT", "another-request-project"),
        ("vertex_ai/gemini-2.5-pro", "VERTEXAI_LOCATION", "another-request-location"),
        ("vertex_ai/gemini-2.5-pro", "VERTEXAI_API_BASE", "https://another-request.example"),
        ("vertex_ai/gemini-2.5-pro", "VERTEX_API_BASE", "https://another-request.example"),
        ("publicai/model", "PUBLICAI_API_BASE", "https://another-request.example/v1"),
    ),
)
@pytest.mark.asyncio
async def test_late_provider_routing_environment_is_rejected(
    monkeypatch,
    model,
    environment_variable,
    environment_value,
):
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, environment_value)

    with pytest.raises(ValueError, match="environment fallback"):
        await _call(handler, model)


@pytest.mark.parametrize("environment_variable", ("ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL"))
@pytest.mark.asyncio
async def test_anthropic_endpoint_is_snapshotted_with_its_api_key(monkeypatch, environment_variable):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "tenant-a-key")
    monkeypatch.setenv(environment_variable, "https://tenant-a.example")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "https://tenant-b.example")

    kwargs = await _call(handler, "anthropic/claude-x")

    assert kwargs["api_key"] == "tenant-a-key"
    assert kwargs["api_base"] == "https://tenant-a.example"


@pytest.mark.parametrize(
    ("model", "api_key_environment", "api_base_environment"),
    (
        ("azure_ai/model", "AZURE_AI_API_KEY", "AZURE_AI_API_BASE"),
        ("cohere_chat/model", "COHERE_API_KEY", "COHERE_API_BASE"),
        ("gemini/model", "GEMINI_API_KEY", "GEMINI_API_BASE"),
        ("groq/model", "GROQ_API_KEY", "GROQ_API_BASE"),
        ("huggingface/model", "HF_TOKEN", "HF_API_BASE"),
        ("huggingface/model", "HF_TOKEN", "HUGGINGFACE_API_BASE"),
        ("mistral/model", "MISTRAL_AZURE_API_KEY", "MISTRAL_AZURE_API_BASE"),
        ("mistral/model", "MISTRAL_API_KEY", "MISTRAL_API_BASE"),
        ("openai_like/model", "OPENAI_LIKE_API_KEY", "OPENAI_LIKE_API_BASE"),
        ("text-completion-codestral/codestral-latest", "CODESTRAL_API_KEY", "CODESTRAL_API_BASE"),
    ),
)
@pytest.mark.asyncio
async def test_native_provider_endpoint_is_snapshotted_with_its_api_key(
    monkeypatch,
    model,
    api_key_environment,
    api_base_environment,
):
    monkeypatch.setenv(api_key_environment, "tenant-a-key")
    monkeypatch.setenv(api_base_environment, "https://tenant-a.example/v1")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(api_base_environment, "https://tenant-b.example/v1")

    kwargs = await _call(handler, model)

    assert kwargs["api_key"] == "tenant-a-key"
    assert kwargs["api_base"] == "https://tenant-a.example/v1"


@pytest.mark.parametrize(
    ("model", "api_base_environment"),
    (
        ("github_copilot/gpt-4o", "GITHUB_COPILOT_API_BASE"),
    ),
)
@pytest.mark.asyncio
async def test_managed_auth_endpoint_is_snapshotted(monkeypatch, model, api_base_environment):
    monkeypatch.setenv(api_base_environment, "https://tenant-a.example/v1")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(api_base_environment, "https://tenant-b.example/v1")

    kwargs = await _call(handler, model)

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY
    assert kwargs["api_base"] == "https://tenant-a.example/v1"


@pytest.mark.parametrize("api_base_environment", ("CHATGPT_API_BASE", "OPENAI_CHATGPT_API_BASE"))
@pytest.mark.asyncio
async def test_chatgpt_endpoint_must_remain_unchanged(monkeypatch, api_base_environment):
    monkeypatch.setenv(api_base_environment, "https://tenant-a.example/v1")
    handler = LiteLLMAIHandler()

    kwargs = await _call(handler, "chatgpt/gpt-5")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY
    assert kwargs["api_base"] == "https://tenant-a.example/v1"


@pytest.mark.parametrize("api_base_environment", ("CHATGPT_API_BASE", "OPENAI_CHATGPT_API_BASE"))
@pytest.mark.parametrize("late_value", ("https://tenant-b.example/v1", None))
@pytest.mark.asyncio
async def test_changed_chatgpt_endpoint_is_rejected(monkeypatch, api_base_environment, late_value):
    monkeypatch.setenv(api_base_environment, "https://tenant-a.example/v1")
    handler = LiteLLMAIHandler()
    if late_value is None:
        monkeypatch.delenv(api_base_environment)
    else:
        monkeypatch.setenv(api_base_environment, late_value)

    with pytest.raises(ValueError, match="Refusing changed live api_base environment for provider chatgpt"):
        await _call(handler, "chatgpt/gpt-5")


@pytest.mark.asyncio
async def test_cloudflare_account_is_snapshotted_with_its_api_key(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "tenant-a-key")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "tenant-a-account")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "tenant-b-account")

    kwargs = await _call(handler, "cloudflare/model")

    assert kwargs["api_key"] == "tenant-a-key"
    assert kwargs["api_base"] == "https://api.cloudflare.com/client/v4/accounts/tenant-a-account/ai/v1"


@pytest.mark.asyncio
async def test_cloudflare_api_base_is_snapshotted_with_its_api_key(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "tenant-a-key")
    monkeypatch.setenv("CLOUDFLARE_API_BASE", "https://tenant-a.example/v1")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("CLOUDFLARE_API_BASE", "https://tenant-b.example/v1")

    kwargs = await _call(handler, "cloudflare/model")

    assert kwargs["api_key"] == "tenant-a-key"
    assert kwargs["api_base"] == "https://tenant-a.example/v1"


@pytest.mark.asyncio
async def test_late_cloudflare_account_is_rejected(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "request-key")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "another-request-account")

    with pytest.raises(ValueError, match="Refusing live api_base environment fallback"):
        await _call(handler, "cloudflare/model")


@pytest.mark.parametrize("provider", ("bedrock", "bedrock_mantle"))
@pytest.mark.asyncio
async def test_late_bedrock_runtime_endpoint_is_rejected(monkeypatch, provider):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({
            "aws.AWS_ACCESS_KEY_ID": "request-key",
            "aws.AWS_SECRET_ACCESS_KEY": "request-secret",
            "aws.AWS_REGION_NAME": "us-east-1",
        }),
    )
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_BEDROCK_RUNTIME_ENDPOINT", "https://another-request.example")

    with pytest.raises(ValueError, match="Refusing live aws_bedrock_runtime_endpoint environment fallback"):
        await _call(handler, f"{provider}/model")


@pytest.mark.parametrize("model", ("watsonx/model", "watsonx_text/model"))
@pytest.mark.asyncio
async def test_watsonx_environment_routing_is_snapshotted(monkeypatch, model):
    monkeypatch.setenv("WATSONX_APIKEY", "tenant-a-key")
    monkeypatch.setenv("WATSONX_URL", "https://tenant-a.example")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "tenant-a-project")
    monkeypatch.setenv("WATSONX_SPACE_ID", "tenant-a-space")
    monkeypatch.setenv("WATSONX_REGION", "tenant-a-region")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("WATSONX_URL", "https://tenant-b.example")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "tenant-b-project")
    monkeypatch.setenv("WATSONX_SPACE_ID", "tenant-b-space")
    monkeypatch.setenv("WATSONX_REGION", "tenant-b-region")

    kwargs = await _call(handler, model)

    assert kwargs["api_key"] == "tenant-a-key"
    assert kwargs["api_base"] == "https://tenant-a.example"
    assert kwargs["project_id"] == "tenant-a-project"
    assert kwargs["space_id"] == "tenant-a-space"
    assert kwargs["region_name"] == "tenant-a-region"


@pytest.mark.parametrize("model", ("watsonx/model", "watsonx_text/model"))
@pytest.mark.parametrize(
    ("environment_variable", "parameter"),
    (
        ("WATSONX_TOKEN", "token"),
        ("WATSONX_ZENAPIKEY", "zen_api_key"),
    ),
)
@pytest.mark.asyncio
async def test_watsonx_auth_environment_is_snapshotted(monkeypatch, model, environment_variable, parameter):
    monkeypatch.setenv(environment_variable, "tenant-a-credential")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "tenant-b-credential")

    kwargs = await _call(handler, model)

    if environment_variable == "WATSONX_TOKEN":
        assert kwargs["api_key"] == litellm_handler.DUMMY_LITELLM_API_KEY
        assert kwargs["headers"]["Authorization"] == "Bearer tenant-a-credential"
        assert "token" not in kwargs
    else:
        assert kwargs["api_key"] == litellm_handler.DUMMY_LITELLM_API_KEY
        assert kwargs[parameter] == "tenant-a-credential"


@pytest.mark.parametrize("model", ("watsonx/model", "watsonx_text/model"))
@pytest.mark.parametrize("authorization_header", ("Authorization", "authorization"))
@pytest.mark.asyncio
async def test_watsonx_request_header_overrides_snapshotted_token(monkeypatch, model, authorization_header):
    monkeypatch.setenv("WATSONX_TOKEN", "environment-token")
    monkeypatch.setenv("WATSONX_ZENAPIKEY", "environment-zen-key")
    handler = LiteLLMAIHandler()
    handler._request_headers = {authorization_header: "Bearer request-token"}

    kwargs = await _call(handler, model)

    assert kwargs["headers"] == {"Authorization": "Bearer request-token"}
    assert kwargs["api_key"] == litellm_handler.DUMMY_LITELLM_API_KEY
    assert "token" not in kwargs
    assert "zen_api_key" not in kwargs


@pytest.mark.parametrize("model", ("watsonx/model", "watsonx_text/model"))
@pytest.mark.asyncio
async def test_watsonx_request_authorization_blocks_generic_api_key(monkeypatch, model):
    monkeypatch.setenv("WATSONX_APIKEY", "environment-api-key")
    handler = LiteLLMAIHandler()
    handler._request_headers = {"Authorization": "Bearer request-token"}

    kwargs = await _call(handler, model)

    assert kwargs["headers"] == {"Authorization": "Bearer request-token"}
    assert kwargs["api_key"] == litellm_handler.DUMMY_LITELLM_API_KEY


@pytest.mark.parametrize("model", ("watsonx/model", "watsonx_text/model"))
@pytest.mark.asyncio
async def test_watsonx_token_takes_precedence_without_forwarding_zen_key(monkeypatch, model):
    monkeypatch.setenv("WATSONX_TOKEN", "request-token")
    monkeypatch.setenv("WATSONX_ZENAPIKEY", "request-zen-key")

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["headers"]["Authorization"] == "Bearer request-token"
    assert "token" not in kwargs
    assert "zen_api_key" not in kwargs


@pytest.mark.parametrize("model", ("watsonx/model", "watsonx_text/model"))
@pytest.mark.parametrize(
    ("environment_variable", "environment_value"),
    (
        ("WATSONX_URL", "https://another-request.example"),
        ("WATSONX_PROJECT_ID", "another-request-project"),
        ("WATSONX_SPACE_ID", "another-request-space"),
        ("WATSONX_REGION", "another-request-region"),
        ("WATSONX_TOKEN", "another-request-token"),
        ("WATSONX_ZENAPIKEY", "another-request-zen-key"),
    ),
)
@pytest.mark.asyncio
async def test_late_watsonx_routing_environment_is_rejected(
    monkeypatch,
    model,
    environment_variable,
    environment_value,
):
    monkeypatch.setenv("WATSONX_APIKEY", "request-key")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, environment_value)

    with pytest.raises(ValueError, match="Refusing live .* environment fallback"):
        await _call(handler, model)


@pytest.mark.asyncio
async def test_bedrock_bearer_token_is_snapshotted(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "tenant-a-token")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({
            "aws.AWS_ACCESS_KEY_ID": "request-key",
            "aws.AWS_SECRET_ACCESS_KEY": "request-secret",
            "aws.AWS_REGION_NAME": "us-east-1",
        }),
    )
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "tenant-b-token")

    kwargs = await _call(handler, "bedrock/model")

    assert kwargs["api_key"] == "tenant-a-token"
    assert kwargs["aws_region_name"] == "us-east-1"
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


@pytest.mark.asyncio
async def test_bedrock_bearer_token_does_not_require_sigv4_credentials(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "request-bearer-token")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")

    kwargs = await _call(LiteLLMAIHandler(), "bedrock/model")

    assert kwargs["api_key"] == "request-bearer-token"
    assert kwargs["aws_region_name"] == "us-east-1"
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


@pytest.mark.asyncio
async def test_late_bedrock_bearer_token_is_rejected(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({
            "aws.AWS_ACCESS_KEY_ID": "request-key",
            "aws.AWS_SECRET_ACCESS_KEY": "request-secret",
            "aws.AWS_REGION_NAME": "us-east-1",
        }),
    )
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "another-request-token")

    with pytest.raises(ValueError, match="Refusing process-wide Bedrock bearer token fallback"):
        await _call(handler, "bedrock/model")


@pytest.mark.asyncio
async def test_late_bedrock_bearer_region_is_rejected(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "request-bearer-token")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_DEFAULT_REGION", "another-request-region")

    with pytest.raises(ValueError, match="Refusing live aws_region_name environment fallback"):
        await _call(handler, "bedrock/model")


@pytest.mark.parametrize("provider", sorted(litellm_handler.AWS_REQUEST_PROVIDERS))
@pytest.mark.parametrize("selector", litellm_handler.LITELLM_AWS_CREDENTIAL_SELECTOR_ENV_VARS)
@pytest.mark.asyncio
async def test_aws_provider_rejects_ambient_litellm_credential_selector(monkeypatch, provider, selector):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({
            "aws.AWS_ACCESS_KEY_ID": "request-key",
            "aws.AWS_SECRET_ACCESS_KEY": "request-secret",
            "aws.AWS_REGION_NAME": "us-east-1",
        }),
    )
    monkeypatch.setenv(selector, "another-request-selector")

    with pytest.raises(ValueError, match=f"Refusing ambient LiteLLM AWS credential selector for provider {provider}"):
        await _call(LiteLLMAIHandler(), f"{provider}/model")


@pytest.mark.parametrize("provider", ("bedrock", "bedrock_mantle"))
@pytest.mark.asyncio
async def test_bedrock_bearer_does_not_use_ambient_litellm_credential_selector(monkeypatch, provider):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "request-bearer-token")
    monkeypatch.setenv("AWS_PROFILE_NAME", "unrelated-profile")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")

    kwargs = await _call(LiteLLMAIHandler(), f"{provider}/model")

    assert kwargs["api_key"] == "request-bearer-token"
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


@pytest.mark.asyncio
async def test_bedrock_mantle_environment_routing_is_snapshotted(monkeypatch):
    monkeypatch.setenv("BEDROCK_MANTLE_API_KEY", "request-key")
    monkeypatch.setenv("BEDROCK_MANTLE_API_BASE", "https://tenant-a.example/v1")
    monkeypatch.setenv("BEDROCK_MANTLE_REGION", "us-east-1")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("BEDROCK_MANTLE_API_BASE", "https://tenant-b.example/v1")
    monkeypatch.setenv("BEDROCK_MANTLE_REGION", "us-west-2")

    kwargs = await _call(handler, "bedrock_mantle/model")

    assert kwargs["api_base"] == "https://tenant-a.example/v1"
    assert kwargs["aws_region_name"] == "us-east-1"


@pytest.mark.asyncio
async def test_bedrock_mantle_bearer_uses_configured_aws_region(monkeypatch):
    monkeypatch.setenv("BEDROCK_MANTLE_API_KEY", "request-key")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"aws.AWS_REGION_NAME": "ap-northeast-1"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), "bedrock_mantle/model")

    assert kwargs["aws_region_name"] == "ap-northeast-1"


@pytest.mark.asyncio
async def test_bedrock_mantle_sigv4_preserves_mantle_region(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MANTLE_REGION", "us-west-2")

    kwargs = await _call(LiteLLMAIHandler(), "bedrock_mantle/model")

    assert kwargs["aws_region_name"] == "us-west-2"
    assert kwargs["aws_access_key_id"] == "request-key"


@pytest.mark.asyncio
async def test_bedrock_mantle_sigv4_derives_region_from_api_base(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
    monkeypatch.setenv("BEDROCK_MANTLE_REGION", "us-west-2")
    monkeypatch.setenv("BEDROCK_MANTLE_API_BASE", "https://bedrock-mantle.eu-west-1.api.aws/v1")

    kwargs = await _call(LiteLLMAIHandler(), "bedrock_mantle/model")

    assert kwargs["api_base"] == "https://bedrock-mantle.eu-west-1.api.aws/v1"
    assert kwargs["aws_region_name"] == "eu-west-1"


@pytest.mark.parametrize(
    ("environment_variable", "environment_value"),
    (
        ("BEDROCK_MANTLE_API_BASE", "https://another-request.example/v1"),
        ("BEDROCK_MANTLE_REGION", "us-west-2"),
        ("AWS_REGION_NAME", "us-west-2"),
        ("AWS_REGION", "us-west-2"),
        ("AWS_DEFAULT_REGION", "us-west-2"),
    ),
)
@pytest.mark.asyncio
async def test_late_bedrock_mantle_environment_routing_is_rejected(
    monkeypatch,
    environment_variable,
    environment_value,
):
    monkeypatch.setenv("BEDROCK_MANTLE_API_KEY", "request-key")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, environment_value)

    with pytest.raises(ValueError, match="Refusing live .* environment fallback"):
        await _call(handler, "bedrock_mantle/model")


@pytest.mark.asyncio
async def test_openai_default_endpoint_ignores_residual_litellm_global(monkeypatch):
    monkeypatch.setattr(litellm, "api_base", "https://another-request.example/v1")

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert kwargs["api_base"] == litellm_handler.OPENAI_DEFAULT_API_BASE
    assert litellm.api_base == "https://another-request.example/v1"


@pytest.mark.asyncio
async def test_azure_environment_routing_is_snapshotted(monkeypatch):
    monkeypatch.setenv("AZURE_API_BASE", "https://azure-a.example")
    monkeypatch.setenv("AZURE_API_VERSION", "tenant-a-version")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AZURE_API_BASE", "https://azure-b.example")
    monkeypatch.setenv("AZURE_API_VERSION", "tenant-b-version")

    kwargs = await _call(handler, "azure/gpt-4o")

    assert kwargs["api_base"] == "https://azure-a.example"
    assert kwargs["api_version"] == "tenant-a-version"


@pytest.mark.parametrize(
    ("api_type", "azure_api_base", "expected_api_base", "expected_api_version"),
    (
        (None, "https://azure.example", "https://azure.example", "azure-version"),
        (None, None, "https://configured.example", "configured-version"),
        ("azure", "https://azure.example", "https://configured.example", "configured-version"),
    ),
)
@pytest.mark.asyncio
async def test_azure_routing_precedence_depends_on_azure_mode(
    monkeypatch,
    api_type,
    azure_api_base,
    expected_api_base,
    expected_api_version,
):
    overrides = {
        "OPENAI.API_BASE": "https://configured.example",
        "OPENAI.API_VERSION": "configured-version",
    }
    if api_type:
        overrides["OPENAI.API_TYPE"] = api_type
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    if azure_api_base:
        monkeypatch.setenv("AZURE_API_BASE", azure_api_base)
    monkeypatch.setenv("AZURE_API_VERSION", "azure-version")

    kwargs = await _call(LiteLLMAIHandler(), "azure/gpt-4o")

    assert kwargs["api_base"] == expected_api_base
    assert kwargs["api_version"] == expected_api_version


@pytest.mark.asyncio
async def test_azure_environment_base_keeps_environment_version_in_azure_mode(monkeypatch):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.API_VERSION": "configured-version",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    monkeypatch.setenv("AZURE_API_BASE", "https://azure-environment.example")
    monkeypatch.setenv("AZURE_API_VERSION", "azure-environment-version")

    kwargs = await _call(LiteLLMAIHandler(), "azure/gpt-4o")

    assert kwargs["api_base"] == "https://azure-environment.example"
    assert kwargs["api_version"] == "azure-environment-version"


@pytest.mark.asyncio
async def test_azure_ad_endpoint_keeps_configured_api_version(monkeypatch):
    overrides = {
        "AZURE_AD.CLIENT_ID": "client-id",
        "AZURE_AD.API_BASE": "https://azure-ad.example",
        "OPENAI.API_VERSION": "configured-version",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_credential", lambda settings: object())
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_token", lambda credential: "azure-ad-token")
    monkeypatch.setenv("AZURE_API_BASE", "https://azure-environment.example")
    monkeypatch.setenv("AZURE_API_VERSION", "azure-environment-version")

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert kwargs["api_base"] == "https://azure-ad.example"
    assert kwargs["api_version"] == "configured-version"


@pytest.mark.parametrize("environment_variable", litellm_handler.AZURE_AD_TOKEN_ENV_VARS)
@pytest.mark.asyncio
async def test_azure_ad_token_environment_is_snapshotted(monkeypatch, environment_variable):
    monkeypatch.setenv(environment_variable, "tenant-a-token")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "tenant-b-token")

    kwargs = await _call(handler, "azure/gpt-4o")

    assert kwargs["azure_ad_token"] == "tenant-a-token"
    assert "api_key" not in kwargs


@pytest.mark.parametrize("environment_variable", litellm_handler.AZURE_AD_TOKEN_ENV_VARS)
@pytest.mark.asyncio
async def test_azure_ad_token_added_after_handler_is_rejected(monkeypatch, environment_variable):
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "another-request-token")

    with pytest.raises(ValueError, match="Azure AD token added after handler initialization"):
        await _call(handler, "azure/gpt-4o")


@pytest.mark.asyncio
async def test_litellm_azure_ad_token_precedes_openai_sdk_environment(monkeypatch):
    monkeypatch.setenv("AZURE_AD_TOKEN", "litellm-token")
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "openai-sdk-token")

    kwargs = await _call(LiteLLMAIHandler(), "azure/gpt-4o")

    assert kwargs["azure_ad_token"] == "litellm-token"


@pytest.mark.asyncio
async def test_vertex_environment_routing_is_snapshotted(monkeypatch):
    monkeypatch.setenv("VERTEXAI_PROJECT", "tenant-a-project")
    monkeypatch.setenv("VERTEXAI_LOCATION", "tenant-a-location")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("VERTEXAI_PROJECT", "tenant-b-project")
    monkeypatch.setenv("VERTEXAI_LOCATION", "tenant-b-location")

    kwargs = await _call(handler, "vertex_ai/gemini-2.5-pro")

    assert kwargs["vertex_project"] == "tenant-a-project"
    assert kwargs["vertex_location"] == "tenant-a-location"


@pytest.mark.parametrize("api_base_environment", ("VERTEXAI_API_BASE", "VERTEX_API_BASE"))
@pytest.mark.asyncio
async def test_vertex_endpoint_is_snapshotted_with_environment_routing(monkeypatch, api_base_environment):
    monkeypatch.setenv(api_base_environment, "https://tenant-a.example/v1")
    monkeypatch.setenv("VERTEXAI_PROJECT", "tenant-a-project")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(api_base_environment, "https://tenant-b.example/v1")
    monkeypatch.setenv("VERTEXAI_PROJECT", "tenant-b-project")

    kwargs = await _call(handler, "vertex_ai/gemini-2.5-pro")

    assert kwargs["api_base"] == "https://tenant-a.example/v1"
    assert kwargs["vertex_project"] == "tenant-a-project"


@pytest.mark.asyncio
async def test_vertex_application_credentials_are_snapshotted(monkeypatch, tmp_path):
    credentials_path = tmp_path / "vertex-credentials.json"
    credentials_path.write_text('{"type": "service_account", "client_email": "tenant-a@example.com"}')
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials_path))
    handler = LiteLLMAIHandler()
    credentials_path.write_text('{"type": "service_account", "client_email": "tenant-b@example.com"}')

    kwargs = await _call(handler, "vertex_ai/model")

    assert json.loads(kwargs["vertex_credentials"])["client_email"] == "tenant-a@example.com"


@pytest.mark.parametrize("environment_variable", ("VERTEXAI_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS"))
@pytest.mark.asyncio
async def test_late_vertex_credentials_are_rejected(monkeypatch, environment_variable):
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "another-request-credentials")

    with pytest.raises(ValueError, match="Refusing live Vertex credential environment fallback"):
        await _call(handler, "vertex_ai/model")


@pytest.mark.parametrize("environment_variable", ("VERTEXAI_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS"))
@pytest.mark.asyncio
async def test_stale_vertex_credentials_only_fail_vertex_requests(monkeypatch, tmp_path, environment_variable):
    credentials_path = tmp_path / "missing-vertex-credentials.json"
    monkeypatch.setenv(environment_variable, str(credentials_path))
    handler = LiteLLMAIHandler()

    await _call(handler, "anthropic/claude-x")
    with pytest.raises(ValueError, match="Unable to snapshot explicit Vertex credentials: FileNotFoundError"):
        await _call(handler, "vertex_ai/model")


@pytest.mark.asyncio
async def test_openai_sdk_global_does_not_suppress_litellm_placeholder(monkeypatch):
    monkeypatch.setattr(openai, "api_key", "openai-sdk-only-key")

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.asyncio
async def test_openai_request_settings_are_forwarded(monkeypatch):
    overrides = {
        "OPENAI.KEY": "request-openai-key",
        "OPENAI.API_BASE": "https://openai.example/v1",
        "OPENAI.API_VERSION": "2026-01-01",
        "OPENAI.ORG": "org-request",
        "LITELLM.EXTRA_HEADERS": (
            '{"openai-organization": "extra-org", "openai-project": "project-request"}'
        ),
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert kwargs["api_key"] == "request-openai-key"
    assert kwargs["api_base"] == "https://openai.example/v1"
    assert kwargs["api_version"] == "2026-01-01"
    assert kwargs["organization"] == "org-request"
    assert kwargs["headers"]["OpenAI-Organization"] == "org-request"
    assert "openai-organization" not in kwargs["headers"]
    assert kwargs["headers"]["OpenAI-Project"] == "project-request"
    assert "openai-project" not in kwargs["headers"]


@pytest.mark.parametrize(
    "model",
    ("gpt-4o", "azure/gpt-4o", "deepinfra/model"),
)
@pytest.mark.asyncio
async def test_openai_sdk_request_blocks_residual_organization_and_headers(monkeypatch, model):
    handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "organization", "another-request-organization")
    monkeypatch.setattr(litellm, "headers", {"X-Another-Request": "secret"})
    monkeypatch.setenv("OPENAI_PROJECT_ID", "another-request-project")

    kwargs = await _call(handler, model)

    assert isinstance(kwargs["headers"]["OpenAI-Organization"], openai.Omit)
    assert isinstance(kwargs["headers"]["OpenAI-Project"], openai.Omit)
    assert "X-Another-Request" not in kwargs["headers"]
    assert litellm.organization == "another-request-organization"
    assert litellm.headers == {"X-Another-Request": "secret"}


@pytest.mark.parametrize(
    "model",
    (
        "aiohttp_openai/gpt-4o",
        "openai_like/model",
        "openrouter/model",
        "deepseek/model",
        "groq/model",
        "gpt-5-pro",
        "azure/codex-mini",
        "azure/responses/custom-deployment",
        "openai/responses/gpt-4o",
        "chatgpt/gpt-5.2",
        "github/gpt-5-pro",
        "litellm_proxy/gpt-5-pro",
        "perplexity/openai/gpt-5.2",
        "perplexity/perplexity/glm-5.2",
    ),
)
@pytest.mark.asyncio
async def test_raw_http_request_rejects_residual_headers_without_omit_values(monkeypatch, model):
    handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "headers", {"X-Another-Request": "secret"})
    monkeypatch.setenv("OPENAI_PROJECT_ID", "another-request-project")

    with pytest.raises(ValueError, match="Refusing process-wide LiteLLM headers fallback"):
        await _call(handler, model)


@pytest.mark.parametrize(
    "model",
    (
        "aiohttp_openai/gpt-4o",
        "openai_like/model",
        "openrouter/model",
        "deepseek/model",
        "groq/model",
        "gpt-5-pro",
        "azure/codex-mini",
        "azure/responses/custom-deployment",
        "openai/responses/gpt-4o",
        "chatgpt/gpt-5.2",
        "github/gpt-5-pro",
        "litellm_proxy/gpt-5-pro",
        "perplexity/openai/gpt-5.2",
        "perplexity/perplexity/glm-5.2",
    ),
)
@pytest.mark.asyncio
async def test_raw_http_request_does_not_forward_omit_headers(model):
    kwargs = await _call(LiteLLMAIHandler(), model)

    assert "headers" not in kwargs


@pytest.mark.asyncio
async def test_custom_aiohttp_openai_provider_does_not_forward_omit_headers(monkeypatch):
    settings = _make_settings()
    settings.litellm.custom_llm_provider = "aiohttp_openai"
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert kwargs["custom_llm_provider"] == "aiohttp_openai"
    assert "headers" not in kwargs


@pytest.mark.parametrize("model", ("huggingface/model", "ollama/model", "lemonade/model"))
@pytest.mark.asyncio
async def test_non_openai_request_does_not_inspect_responses_model_info(monkeypatch, model):
    get_model_info = MagicMock(side_effect=AssertionError("unexpected model-info lookup"))
    monkeypatch.setattr(litellm_handler, "_get_model_info_helper", get_model_info)

    await _call(LiteLLMAIHandler(), model)

    get_model_info.assert_not_called()


@pytest.mark.parametrize("model", ("gpt-4o", "cerebras/model"))
@pytest.mark.asyncio
async def test_experimental_openai_raw_http_handler_does_not_forward_omit_headers(monkeypatch, model):
    monkeypatch.setenv("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", "true")

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert "headers" not in kwargs


@pytest.mark.parametrize("value", ("1", "yes"))
@pytest.mark.asyncio
async def test_unrecognized_experimental_handler_values_keep_sdk_omit_headers(monkeypatch, value):
    monkeypatch.setenv("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", value)

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert isinstance(kwargs["headers"]["OpenAI-Organization"], openai.Omit)
    assert isinstance(kwargs["headers"]["OpenAI-Project"], openai.Omit)


@pytest.mark.asyncio
async def test_text_completion_openai_prefix_ignores_experimental_chat_handler(monkeypatch):
    monkeypatch.setenv("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", "true")
    monkeypatch.setattr(litellm, "headers", {"X-Another-Request": "secret"})

    kwargs = await _call(LiteLLMAIHandler(), "text-completion-openai/gpt-3.5-turbo-instruct")

    assert isinstance(kwargs["headers"]["OpenAI-Organization"], openai.Omit)
    assert isinstance(kwargs["headers"]["OpenAI-Project"], openai.Omit)
    assert "X-Another-Request" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_bare_text_completion_model_ignores_experimental_chat_handler(monkeypatch):
    monkeypatch.setenv("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", "true")
    monkeypatch.setattr(litellm, "headers", {"X-Another-Request": "secret"})

    kwargs = await _call(LiteLLMAIHandler(), "gpt-3.5-turbo-instruct")

    assert isinstance(kwargs["headers"]["OpenAI-Organization"], openai.Omit)
    assert isinstance(kwargs["headers"]["OpenAI-Project"], openai.Omit)
    assert "X-Another-Request" not in kwargs["headers"]


@pytest.mark.parametrize("model", ("ft:babbage-002:acme::abc", "ft:davinci-002:acme::abc"))
@pytest.mark.asyncio
async def test_fine_tuned_text_completion_model_ignores_experimental_chat_handler(monkeypatch, model):
    monkeypatch.setenv("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", "true")
    monkeypatch.setattr(litellm, "headers", {"X-Another-Request": "secret"})

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert isinstance(kwargs["headers"]["OpenAI-Organization"], openai.Omit)
    assert isinstance(kwargs["headers"]["OpenAI-Project"], openai.Omit)
    assert "X-Another-Request" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_route_all_openai_to_responses_does_not_forward_omit_headers(monkeypatch):
    monkeypatch.setattr(litellm, "route_all_chat_openai_to_responses", True)

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert "headers" not in kwargs


@pytest.mark.asyncio
async def test_route_all_openai_to_responses_keeps_text_completion_sdk_headers(monkeypatch):
    monkeypatch.setattr(litellm, "route_all_chat_openai_to_responses", True)

    kwargs = await _call(LiteLLMAIHandler(), "gpt-3.5-turbo-instruct")

    assert isinstance(kwargs["headers"]["OpenAI-Organization"], openai.Omit)
    assert isinstance(kwargs["headers"]["OpenAI-Project"], openai.Omit)


@pytest.mark.asyncio
async def test_openai_responses_request_forwards_only_explicit_organization(monkeypatch):
    overrides = {"OPENAI.ORG": "org-request"}
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    monkeypatch.setattr(litellm, "headers", {"X-Another-Request": "secret"})

    kwargs = await _call(LiteLLMAIHandler(), "gpt-5-pro")

    assert kwargs["headers"] == {"OpenAI-Organization": "org-request"}


@pytest.mark.asyncio
async def test_openai_responses_request_rejects_residual_organization(monkeypatch):
    handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "organization", "another-request-organization")

    with pytest.raises(ValueError, match="Refusing process-wide LiteLLM organization fallback"):
        await _call(handler, "gpt-5-pro")


@pytest.mark.asyncio
async def test_openai_responses_request_snapshots_environment_organization(monkeypatch):
    monkeypatch.setenv("OPENAI_ORGANIZATION", "request-organization")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("OPENAI_ORGANIZATION", "another-request-organization")

    kwargs = await _call(handler, "gpt-5-pro")

    assert kwargs["organization"] == "request-organization"
    assert kwargs["headers"] == {"OpenAI-Organization": "request-organization"}


@pytest.mark.asyncio
async def test_openai_responses_request_rejects_late_environment_organization(monkeypatch):
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("OPENAI_ORGANIZATION", "another-request-organization")

    with pytest.raises(ValueError, match="Refusing live organization environment fallback"):
        await _call(handler, "gpt-5-pro")


@pytest.mark.asyncio
async def test_unmapped_azure_responses_model_keeps_sdk_headers():
    kwargs = await _call(LiteLLMAIHandler(), "azure/codex-mini-latest")

    assert isinstance(kwargs["headers"]["OpenAI-Organization"], openai.Omit)
    assert isinstance(kwargs["headers"]["OpenAI-Project"], openai.Omit)


@pytest.mark.asyncio
async def test_azure_deployment_id_selects_sdk_header_guard(monkeypatch):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.DEPLOYMENT_ID": "custom-deployment",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    monkeypatch.setattr(litellm, "headers", {"X-Another-Request": "secret"})

    kwargs = await _call(LiteLLMAIHandler(), "gpt-5-pro")

    assert kwargs["deployment_id"] == "custom-deployment"
    assert isinstance(kwargs["headers"]["OpenAI-Organization"], openai.Omit)
    assert isinstance(kwargs["headers"]["OpenAI-Project"], openai.Omit)
    assert "X-Another-Request" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_azure_deployment_id_selects_responses_header_guard(monkeypatch):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.DEPLOYMENT_ID": "gpt-5-pro",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "custom-deployment")

    assert kwargs["deployment_id"] == "gpt-5-pro"
    assert "headers" not in kwargs


@pytest.mark.asyncio
async def test_azure_deployment_id_matches_responses_model_case_insensitively(monkeypatch):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.DEPLOYMENT_ID": "GPT-5-Pro",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "custom-deployment")

    assert kwargs["deployment_id"] == "GPT-5-Pro"
    assert "headers" not in kwargs


@pytest.mark.asyncio
async def test_stacked_azure_openai_gpt5_prefix_selects_responses_header_guard():
    kwargs = await _call(LiteLLMAIHandler(), "azure/openai/gpt-5-pro")

    assert kwargs["model"] == "azure/gpt-5-pro"
    assert "headers" not in kwargs


@pytest.mark.asyncio
async def test_openai_request_merges_explicit_extra_headers(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({
            "LITELLM.EXTRA_HEADERS": (
                '{"openai-organization": "request-organization", '
                '"OPENAI-PROJECT": "request-project", "X-Request": "request-value"}'
            ),
        }),
    )

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert kwargs["headers"]["OpenAI-Organization"] == "request-organization"
    assert kwargs["headers"]["OpenAI-Project"] == "request-project"
    assert "openai-organization" not in kwargs["headers"]
    assert "OPENAI-PROJECT" not in kwargs["headers"]
    assert kwargs["headers"]["X-Request"] == "request-value"
    assert "extra_headers" not in kwargs


@pytest.mark.parametrize("model", ("anthropic/claude-x", "cohere/command-r"))
@pytest.mark.asyncio
async def test_non_openai_request_rejects_residual_global_headers(monkeypatch, model):
    monkeypatch.setattr(litellm, "headers", {"Authorization": "Bearer another-request-token"})

    with pytest.raises(ValueError, match="Refusing process-wide LiteLLM headers fallback"):
        await _call(LiteLLMAIHandler(), model)


@pytest.mark.asyncio
async def test_non_openai_request_uses_snapshotted_extra_headers(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"LITELLM.EXTRA_HEADERS": '{"X-Request": "request-value"}'}),
    )
    handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "headers", {"Authorization": "Bearer another-request-token"})

    kwargs = await _call(handler, "anthropic/claude-x")

    assert kwargs["headers"] == {"X-Request": "request-value"}
    assert litellm.headers == {"Authorization": "Bearer another-request-token"}


@pytest.mark.parametrize("key_source", ("settings", "environment"))
@pytest.mark.asyncio
async def test_openai_gateway_base_fallback_forwards_request_local_credentials(monkeypatch, key_source):
    overrides = {
        "OPENAI.API_BASE": "https://gateway.example/v1",
        "OPENAI.API_VERSION": "2026-01-01",
        "OPENAI.ORG": "openai-org",
    }
    if key_source == "settings":
        overrides["OPENAI.KEY"] = "gateway-key"
    else:
        monkeypatch.setenv("OPENAI_API_KEY", "gateway-key")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "meta-llama/model")

    assert kwargs["api_base"] == "https://gateway.example/v1"
    assert kwargs["api_key"] == "gateway-key"
    assert "api_version" not in kwargs
    assert "organization" not in kwargs


@pytest.mark.asyncio
async def test_openai_gateway_base_does_not_redirect_native_provider_credentials(monkeypatch):
    overrides = {
        "OPENAI.KEY": "gateway-key",
        "OPENAI.API_BASE": "https://gateway.example/v1",
        "OPENAI.API_VERSION": "2026-01-01",
        "OPENAI.ORG": "openai-org",
        "ANTHROPIC.KEY": "anthropic-key",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "anthropic/claude-x")

    assert "api_base" not in kwargs
    assert kwargs["api_key"] == "anthropic-key"
    assert "api_version" not in kwargs
    assert "organization" not in kwargs


@pytest.mark.parametrize(
    ("model", "overrides", "expected_params"),
    (
        (
            "bedrock/anthropic.claude-v2",
            {
                "aws.AWS_ACCESS_KEY_ID": "request-access-key",
                "aws.AWS_SECRET_ACCESS_KEY": "request-secret-key",
                "aws.AWS_REGION_NAME": "us-east-1",
            },
            {
                "aws_access_key_id": "request-access-key",
                "aws_secret_access_key": "request-secret-key",
                "aws_region_name": "us-east-1",
            },
        ),
        (
            "vertex_ai/gemini-2.5-pro",
            {
                "VERTEXAI.VERTEX_PROJECT": "request-project",
                "VERTEXAI.VERTEX_LOCATION": "us-central1",
            },
            {
                "vertex_project": "request-project",
                "vertex_location": "us-central1",
            },
        ),
    ),
)
@pytest.mark.asyncio
async def test_openai_gateway_base_does_not_redirect_native_provider_context(
    monkeypatch,
    model,
    overrides,
    expected_params,
):
    overrides = {**overrides, "OPENAI.API_BASE": "https://gateway.example/v1"}
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert "api_base" not in kwargs
    assert expected_params.items() <= kwargs.items()


@pytest.mark.asyncio
async def test_openai_gateway_credentials_reach_compatible_provider(monkeypatch):
    overrides = {
        "OPENAI.KEY": "gateway-key",
        "OPENAI.API_BASE": "https://gateway.example/v1",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "together_ai/model")

    assert kwargs["api_key"] == "gateway-key"
    assert kwargs["api_base"] == "https://gateway.example/v1"


@pytest.mark.asyncio
async def test_mosaico_gateway_credentials_reach_openrouter(monkeypatch):
    overrides = {
        "OPENAI.KEY": "gateway-key",
        "OPENAI.API_BASE": "https://openrouter.ai/api/v1",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "openrouter/mistralai/devstral-small")

    assert kwargs["api_key"] == "gateway-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_openai_gateway_credentials_reach_json_provider(monkeypatch):
    overrides = {
        "OPENAI.KEY": "gateway-key",
        "OPENAI.API_BASE": "https://gateway.example/v1",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "xiaomi_mimo/mimo-v2-flash")

    assert kwargs["api_key"] == "gateway-key"
    assert kwargs["api_base"] == "https://gateway.example/v1"


@pytest.mark.asyncio
async def test_json_provider_environment_base_is_request_local(monkeypatch):
    monkeypatch.setenv("PUBLICAI_API_BASE", "https://request.example/v1")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.API_BASE": "https://gateway.example/v1"}),
    )
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("PUBLICAI_API_BASE", "https://another-request.example/v1")

    kwargs = await _call(handler, "publicai/model")

    assert kwargs["api_base"] == "https://request.example/v1"


@pytest.mark.asyncio
async def test_json_provider_blocks_unrelated_openai_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")

    kwargs = await _call(LiteLLMAIHandler(), "xiaomi_mimo/mimo-v2-flash")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.asyncio
async def test_native_compatible_provider_environment_ignores_openai_gateway(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "native-deepseek-key")
    overrides = {
        "OPENAI.KEY": "gateway-key",
        "OPENAI.API_BASE": "https://gateway.example/v1",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "deepseek/deepseek-chat")

    assert kwargs["api_key"] == "native-deepseek-key"
    assert "api_base" not in kwargs


@pytest.mark.asyncio
async def test_native_compatible_provider_setting_ignores_openai_gateway(monkeypatch):
    overrides = {
        "DEEPSEEK.KEY": "configured-deepseek-key",
        "OPENAI.KEY": "gateway-key",
        "OPENAI.API_BASE": "https://gateway.example/v1",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "deepseek/deepseek-chat")

    assert kwargs["api_key"] == "configured-deepseek-key"
    assert "api_base" not in kwargs


@pytest.mark.asyncio
async def test_openai_gateway_base_does_not_override_compatible_provider_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "native-openai-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "native-deepseek-key")
    overrides = {"OPENAI.API_BASE": "https://gateway.example/v1"}
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "deepseek/deepseek-chat")

    assert kwargs["api_key"] == "native-deepseek-key"
    assert "api_base" not in kwargs


@pytest.mark.parametrize("openrouter_api_base", (None, ""))
@pytest.mark.asyncio
async def test_empty_openrouter_api_base_uses_openrouter_default(monkeypatch, openrouter_api_base):
    overrides = {
        "OPENROUTER.KEY": "openrouter-key",
        "OPENROUTER.API_BASE": openrouter_api_base,
        "OPENAI.API_BASE": "https://gateway.example/v1",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "openrouter/model")

    assert kwargs["api_key"] == "openrouter-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_native_openrouter_key_uses_openrouter_default(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "native-openrouter-key")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.API_BASE": "https://gateway.example/v1"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), "openrouter/model")

    assert kwargs["api_key"] == "native-openrouter-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_openrouter_without_key_ignores_openai_gateway(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.API_BASE": "https://gateway.example/v1"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), "openrouter/model")

    assert "api_base" not in kwargs


@pytest.mark.parametrize("model", ("anthropic/claude-x", "openai_like/model", "openrouter/model"))
@pytest.mark.asyncio
async def test_azure_endpoint_is_not_used_for_non_azure_fallback(monkeypatch, model):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.KEY": "azure-key",
        "OPENAI.API_BASE": "https://azure.example/v1",
        "OPENAI.DEPLOYMENT_ID": "azure-deployment",
        "ANTHROPIC.KEY": "anthropic-key",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert "api_base" not in kwargs
    assert "deployment_id" not in kwargs
    if model.startswith("anthropic/"):
        assert kwargs["api_key"] == "anthropic-key"
    else:
        assert "api_key" not in kwargs


@pytest.mark.parametrize("model", ("gpt-4o", "openai/gpt-4o"))
@pytest.mark.asyncio
async def test_deployment_id_follows_the_current_fallback_attempt(monkeypatch, model):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.DEPLOYMENT_ID": "primary-deployment",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    handler = LiteLLMAIHandler()
    overrides["OPENAI.DEPLOYMENT_ID"] = "fallback-deployment"

    kwargs = await _call(handler, model)

    assert kwargs["model"] == "azure/gpt-4o"
    assert kwargs["deployment_id"] == "fallback-deployment"


@pytest.mark.parametrize("deployment_id", ("fallback-deployment", ""))
@pytest.mark.asyncio
async def test_empty_deployment_id_is_not_forwarded(monkeypatch, deployment_id):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.DEPLOYMENT_ID": "primary-deployment",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    handler = LiteLLMAIHandler()
    overrides["OPENAI.DEPLOYMENT_ID"] = deployment_id

    kwargs = await _call(handler, "gpt-4o")

    if deployment_id:
        assert kwargs["deployment_id"] == deployment_id
    else:
        assert "deployment_id" not in kwargs


@pytest.mark.asyncio
async def test_azure_deployment_id_is_not_used_for_non_azure_probe(monkeypatch):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.DEPLOYMENT_ID": "azure-deployment",
        "ANTHROPIC.KEY": "anthropic-key",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    handler = LiteLLMAIHandler()
    completion = AsyncMock(return_value=_mock_response())

    await handler.probe_completion("anthropic/claude-x", _completion=completion)

    assert completion.call_args.kwargs["model"] == "anthropic/claude-x"
    assert "deployment_id" not in completion.call_args.kwargs


@pytest.mark.parametrize(("setting_path", "model", "expected_key"), (
    ("ANTHROPIC.KEY", "anthropic/claude-sonnet-4-5", "anthropic-key"),
    ("COHERE.KEY", "cohere/command-r", "cohere-key"),
    ("GROQ.KEY", "groq/llama-3.3-70b-versatile", "groq-key"),
    ("SAMBANOVA.KEY", "sambanova/Meta-Llama-3.3-70B-Instruct", "sambanova-key"),
    ("REPLICATE.KEY", "replicate/meta/model", "replicate-key"),
    ("XAI.KEY", "xai/grok-4", "xai-key"),
    ("GOOGLE_AI_STUDIO.GEMINI_API_KEY", "gemini/gemini-2.5-pro", "gemini-key"),
    ("DEEPSEEK.KEY", "deepseek/deepseek-chat", "deepseek-key"),
    ("ZAI.KEY", "zai/glm-4.5", "zai-key"),
    ("DASHSCOPE.KEY", "dashscope/qwen3.8-max", "dashscope-key"),
    ("XIAOMI_MIMO.KEY", "xiaomi_mimo/mimo-v2-flash", "xiaomi-key"),
    ("DEEPINFRA.KEY", "deepinfra/meta-llama/model", "deepinfra-key"),
    ("MISTRAL.KEY", "mistral/mistral-large-latest", "mistral-key"),
    ("CODESTRAL.KEY", "codestral/codestral-latest", "codestral-key"),
))
@pytest.mark.asyncio
async def test_provider_key_is_forwarded_only_for_matching_model(monkeypatch, setting_path, model, expected_key):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({setting_path: expected_key}),
    )
    handler = LiteLLMAIHandler()

    matching_kwargs = await _call(handler, model)
    openai_kwargs = await _call(handler, "gpt-4o")

    assert matching_kwargs["api_key"] == expected_key
    assert openai_kwargs["api_key"] == DUMMY_LITELLM_API_KEY


@pytest.mark.asyncio
async def test_provider_specific_endpoints_do_not_cross_models(monkeypatch):
    overrides = {
        "MOONSHOT.KEY": "moonshot-key",
        "MOONSHOT.API_BASE": "https://api.moonshot.cn/v1",
        "OLLAMA.API_KEY": "ollama-key",
        "OLLAMA.API_BASE": "http://ollama-a:11434",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    handler = LiteLLMAIHandler()

    moonshot_kwargs = await _call(handler, "moonshot/kimi-k3")
    ollama_kwargs = await _call(handler, "ollama/llama3")

    assert moonshot_kwargs["api_key"] == "moonshot-key"
    assert moonshot_kwargs["api_base"] == "https://api.moonshot.cn/v1"
    assert ollama_kwargs["api_key"] == "ollama-key"
    assert ollama_kwargs["api_base"] == "http://ollama-a:11434"


@pytest.mark.asyncio
async def test_sequential_handlers_keep_request_credentials_isolated(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"MOONSHOT.KEY": "tenant-a", "MOONSHOT.API_BASE": "https://a.example/v1"}),
    )
    tenant_a = LiteLLMAIHandler()
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"MOONSHOT.KEY": "tenant-b", "MOONSHOT.API_BASE": "https://b.example/v1"}),
    )
    tenant_b = LiteLLMAIHandler()

    tenant_a_kwargs = await _call(tenant_a, "moonshot/kimi-k3")
    tenant_b_kwargs = await _call(tenant_b, "moonshot/kimi-k3")

    assert (tenant_a_kwargs["api_key"], tenant_a_kwargs["api_base"]) == ("tenant-a", "https://a.example/v1")
    assert (tenant_b_kwargs["api_key"], tenant_b_kwargs["api_base"]) == ("tenant-b", "https://b.example/v1")


@pytest.mark.asyncio
async def test_concurrent_handlers_keep_request_credentials_isolated(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENROUTER.KEY": "tenant-a", "OPENROUTER.API_BASE": "https://a.example/v1"}),
    )
    tenant_a = LiteLLMAIHandler()
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENROUTER.KEY": "tenant-b", "OPENROUTER.API_BASE": "https://b.example/v1"}),
    )
    tenant_b = LiteLLMAIHandler()
    calls = []

    async def capture_call(**kwargs):
        await asyncio.sleep(0)
        calls.append(kwargs)
        return _mock_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=capture_call):
        await asyncio.gather(
            tenant_a.chat_completion(model="openrouter/openai/gpt-4o", system="sys", user="usr"),
            tenant_b.chat_completion(model="openrouter/openai/gpt-4o", system="sys", user="usr"),
        )

    credentials = {(call["api_key"], call["api_base"]) for call in calls}
    assert credentials == {("tenant-a", "https://a.example/v1"), ("tenant-b", "https://b.example/v1")}


@pytest.mark.asyncio
async def test_vertex_routing_is_request_local(monkeypatch):
    overrides = {
        "VERTEXAI.VERTEX_PROJECT": "request-project",
        "VERTEXAI.VERTEX_LOCATION": "asia-east1",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    monkeypatch.setattr(litellm, "api_key", "another-request-key")

    kwargs = await _call(LiteLLMAIHandler(), "vertex_ai/gemini-2.5-pro")

    assert kwargs["vertex_project"] == "request-project"
    assert kwargs["vertex_location"] == "asia-east1"
    assert "api_key" not in kwargs


@pytest.mark.parametrize(("model", "setting_path", "expected_key"), (
    ("anthropic_text/claude-2", "ANTHROPIC.KEY", "anthropic-key"),
    ("azure_text/gpt-35-turbo-instruct", "OPENAI.KEY", "azure-key"),
    ("ollama_chat/llama3", "OLLAMA.API_KEY", "ollama-key"),
    ("text-completion-openai/gpt-3.5-turbo-instruct", "OPENAI.KEY", "openai-key"),
    ("vertex_ai_beta/gemini-2.5-pro", "VERTEXAI.VERTEX_PROJECT", "vertex-project"),
))
@pytest.mark.asyncio
async def test_provider_alias_uses_canonical_request_settings(monkeypatch, model, setting_path, expected_key):
    overrides = {setting_path: expected_key}
    if model.startswith("azure_text/"):
        overrides["OPENAI.API_TYPE"] = "azure"
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), model)

    parameter = "vertex_project" if setting_path == "VERTEXAI.VERTEX_PROJECT" else "api_key"
    assert kwargs[parameter] == expected_key


@pytest.mark.asyncio
async def test_explicit_azure_model_uses_native_key_outside_azure_mode(monkeypatch):
    monkeypatch.setenv("AZURE_API_KEY", "native-azure-key")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.KEY": "openai-key"}),
    )

    kwargs = await _call(LiteLLMAIHandler(), "azure/gpt-4o")

    assert kwargs["api_key"] == "native-azure-key"


@pytest.mark.asyncio
async def test_bare_model_provider_alias_uses_canonical_request_settings(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({"ANTHROPIC.KEY": "anthropic-key"}))
    monkeypatch.setattr(litellm, "get_llm_provider", lambda model: (model, "anthropic_text", None, None))

    kwargs = await _call(LiteLLMAIHandler(), "claude-2")

    assert kwargs["api_key"] == "anthropic-key"


@pytest.mark.asyncio
async def test_bare_model_provider_resolution_is_cached_per_handler(monkeypatch):
    resolve_provider = MagicMock(return_value=("gpt-4o", "openai", None, None))
    monkeypatch.setattr(litellm, "get_llm_provider", resolve_provider)
    handler = LiteLLMAIHandler()

    await _call(handler, "gpt-4o")
    await _call(handler, "gpt-4o")

    resolve_provider.assert_called_once_with(model="gpt-4o")


@pytest.mark.asyncio
async def test_bare_model_provider_resolution_only_falls_back_for_bad_requests(monkeypatch):
    bad_request = litellm.BadRequestError(
        message="provider not found",
        model="custom-model",
        llm_provider="",
    )
    resolve_provider = MagicMock(side_effect=bad_request)
    monkeypatch.setattr(litellm, "get_llm_provider", resolve_provider)

    kwargs = await _call(LiteLLMAIHandler(), "custom-model")

    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY

    resolve_provider.side_effect = RuntimeError("provider resolution failed")
    with pytest.raises(RuntimeError, match="provider resolution failed"):
        await _call(LiteLLMAIHandler(), "another-custom-model")


@pytest.mark.asyncio
async def test_azure_ad_token_and_endpoint_are_request_local(monkeypatch):
    overrides = {
        "AZURE_AD.CLIENT_ID": "client-id",
        "AZURE_AD.API_BASE": "https://azure.example",
        "OPENAI.DEPLOYMENT_ID": "azure-deployment",
        "OPENAI.API_VERSION": "2026-01-01",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    credential = object()
    credentials = []
    tokens = iter(("azure-ad-token-1", "azure-ad-token-2"))
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_credential", lambda settings: credential)

    def get_token(received_credential):
        credentials.append(received_credential)
        return next(tokens)

    monkeypatch.setattr(litellm_handler, "_get_azure_ad_token", get_token)
    handler = LiteLLMAIHandler()

    first_kwargs = await _call(handler, "gpt-4o")
    second_kwargs = await _call(handler, "gpt-4o")

    assert first_kwargs["model"] == "azure/gpt-4o"
    assert first_kwargs["azure_ad_token"] == "azure-ad-token-1"
    assert second_kwargs["azure_ad_token"] == "azure-ad-token-2"
    assert first_kwargs["api_base"] == "https://azure.example"
    assert first_kwargs["api_version"] == "2026-01-01"
    assert first_kwargs["deployment_id"] == "azure-deployment"
    assert credentials == [credential, credential]


def test_azure_ad_token_requires_request_local_credential():
    with pytest.raises(ValueError, match="credential is required"):
        litellm_handler._get_azure_ad_token(None)


def test_azure_ad_token_error_redacts_provider_details(monkeypatch):
    credential = MagicMock()
    credential.get_token.side_effect = RuntimeError("provider-secret")
    logger = MagicMock()
    monkeypatch.setattr(litellm_helpers, "get_logger", lambda: logger)

    with pytest.raises(RuntimeError, match="provider-secret"):
        litellm_helpers._get_azure_ad_token(credential)

    logger.error.assert_called_once_with("Failed to get Azure AD token: RuntimeError")
    assert "provider-secret" not in logger.error.call_args.args[0]


@pytest.mark.asyncio
async def test_health_probe_routes_azure_ad_token_per_request(monkeypatch):
    overrides = {
        "AZURE_AD.CLIENT_ID": "client-id",
        "AZURE_AD.API_BASE": "https://azure.example",
        "OPENAI.DEPLOYMENT_ID": "azure-deployment",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    credential = object()
    get_token = MagicMock(return_value="probe-token")
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_credential", lambda settings: credential)
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_token", get_token)
    handler = LiteLLMAIHandler()
    completion = AsyncMock(return_value=_mock_response())

    await handler.probe_completion("gpt-4o", _completion=completion)

    completion.assert_awaited_once()
    assert completion.call_args.kwargs["model"] == "azure/gpt-4o"
    assert completion.call_args.kwargs["azure_ad_token"] == "probe-token"
    assert completion.call_args.kwargs["api_base"] == "https://azure.example"
    assert completion.call_args.kwargs["deployment_id"] == "azure-deployment"
    assert get_token.call_count == 1
    assert all(token_call.args == (credential,) for token_call in get_token.call_args_list)


def test_azure_ad_credential_creation_failure_is_logged(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"AZURE_AD.CLIENT_ID": "client-id"}),
    )
    logger = MagicMock()
    monkeypatch.setattr(litellm_handler, "get_logger", lambda: logger)
    secret = "azure-client-secret"

    def fail_to_create_credential(settings):
        raise RuntimeError(f"credential setup failed with {secret}")

    monkeypatch.setattr(litellm_handler, "_get_azure_ad_credential", fail_to_create_credential)

    with pytest.raises(RuntimeError, match="credential setup failed"):
        LiteLLMAIHandler()

    logger.error.assert_called_once_with("Failed to create Azure AD credential: RuntimeError")
    assert secret not in logger.error.call_args.args[0]


@pytest.mark.asyncio
async def test_azure_ad_refresh_failure_is_not_retried(monkeypatch):
    overrides = {
        "AZURE_AD.CLIENT_ID": "client-id",
        "AZURE_AD.API_BASE": "https://azure.example",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    credential = object()
    get_token = MagicMock(side_effect=RuntimeError("refresh failed"))
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_credential", lambda settings: credential)
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_token", get_token)

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(litellm_handler.asyncio, "to_thread", run_inline)
    completion = AsyncMock()
    handler = LiteLLMAIHandler()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", completion):
        with pytest.raises(RuntimeError, match="refresh failed"):
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

    assert get_token.call_count == 1
    completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_azure_ad_uses_openai_endpoint_when_its_endpoint_is_unset(monkeypatch):
    overrides = {
        "AZURE_AD.CLIENT_ID": "client-id",
        "OPENAI.API_BASE": "https://azure.example",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    credential = object()
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_credential", lambda settings: credential)
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_token", lambda received: "azure-ad-token")

    kwargs = await _call(LiteLLMAIHandler(), "gpt-4o")

    assert kwargs["api_base"] == "https://azure.example"


@pytest.mark.asyncio
async def test_azure_ad_preserves_native_non_azure_provider_routing(monkeypatch):
    overrides = {
        "AZURE_AD.CLIENT_ID": "client-id",
        "AZURE_AD.API_BASE": "https://azure.example",
        "OPENAI.API_BASE": "https://gateway.example/v1",
        "DEEPSEEK.KEY": "deepseek-key",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_credential", lambda settings: object())

    kwargs = await _call(LiteLLMAIHandler(), "deepseek/deepseek-chat")

    assert kwargs["api_key"] == "deepseek-key"
    assert "api_base" not in kwargs


@pytest.mark.asyncio
async def test_azure_mode_preserves_explicit_non_openai_provider(monkeypatch):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.KEY": "azure-key",
        "ANTHROPIC.KEY": "anthropic-key",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "anthropic/claude-sonnet-4-5")

    assert kwargs["model"] == "anthropic/claude-sonnet-4-5"
    assert kwargs["api_key"] == "anthropic-key"


@pytest.mark.parametrize(("model", "expected_model"), (
    ("text-completion-openai/gpt-3.5-turbo-instruct", "azure_text/gpt-3.5-turbo-instruct"),
    ("azure_text/gpt-3.5-turbo-instruct", "azure_text/gpt-3.5-turbo-instruct"),
    ("aiohttp_openai/gpt-4o", "azure/gpt-4o"),
))
@pytest.mark.asyncio
async def test_azure_mode_routes_openai_aliases_to_azure(monkeypatch, model, expected_model):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({
            "OPENAI.API_TYPE": "azure",
            "OPENAI.KEY": "azure-key",
            "OPENAI.API_BASE": "https://azure.example",
            "OPENAI.API_VERSION": "2026-01-01",
        }),
    )

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["model"] == expected_model
    assert kwargs["api_key"] == "azure-key"
    assert kwargs["api_base"] == "https://azure.example"
    assert kwargs["api_version"] == "2026-01-01"


@pytest.mark.parametrize("model", (
    "text-completion-openai/gpt-3.5-turbo-instruct",
    "azure_text/gpt-3.5-turbo-instruct",
))
@pytest.mark.asyncio
async def test_azure_text_alias_uses_deployment_id_as_model(monkeypatch, model):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.KEY": "azure-key",
        "OPENAI.API_BASE": "https://azure.example",
        "OPENAI.API_VERSION": "2026-01-01",
        "OPENAI.DEPLOYMENT_ID": "azure-text-deployment",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), model)

    assert kwargs["model"] == "azure_text/azure-text-deployment"
    assert "deployment_id" not in kwargs
    assert kwargs["api_key"] == "azure-key"
    assert kwargs["api_base"] == "https://azure.example"
    assert kwargs["api_version"] == "2026-01-01"


@pytest.mark.asyncio
async def test_explicit_azure_text_uses_deployment_id_outside_azure_mode(monkeypatch):
    monkeypatch.setenv("AZURE_API_KEY", "azure-key")
    overrides = {
        "OPENAI.API_BASE": "https://azure.example",
        "OPENAI.API_VERSION": "2026-01-01",
        "OPENAI.DEPLOYMENT_ID": "azure-text-deployment",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    kwargs = await _call(LiteLLMAIHandler(), "azure_text/gpt-3.5-turbo-instruct")

    assert kwargs["model"] == "azure_text/azure-text-deployment"
    assert "deployment_id" not in kwargs
    assert kwargs["api_key"] == "azure-key"
    assert kwargs["api_base"] == "https://azure.example"
    assert kwargs["api_version"] == "2026-01-01"


@pytest.mark.asyncio
async def test_deployment_id_is_snapshotted_per_request(monkeypatch):
    monkeypatch.setenv("AZURE_API_KEY", "azure-key")
    overrides = {
        "OPENAI.API_BASE": "https://azure.example",
        "OPENAI.API_VERSION": "2026-01-01",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    handler = LiteLLMAIHandler()
    deployment_reads = 0

    def changing_deployment_id(self):
        nonlocal deployment_reads
        deployment_reads += 1
        return f"deployment-{deployment_reads}"

    monkeypatch.setattr(LiteLLMAIHandler, "deployment_id", property(changing_deployment_id))

    chat_kwargs = await _call(handler, "azure_text/gpt-3.5-turbo-instruct")
    completion = AsyncMock(return_value=_mock_response())
    await handler.probe_completion(
        "azure_text/gpt-3.5-turbo-instruct",
        _completion=completion,
    )

    assert chat_kwargs["model"] == "azure_text/deployment-1"
    assert completion.call_args.kwargs["model"] == "azure_text/deployment-2"
    assert deployment_reads == 2


@pytest.mark.asyncio
async def test_deployment_id_snapshot_is_reused_for_same_model_retry(monkeypatch):
    monkeypatch.setenv("AZURE_API_KEY", "azure-key")
    overrides = {
        "OPENAI.API_BASE": "https://azure.example",
        "OPENAI.API_VERSION": "2026-01-01",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    handler = LiteLLMAIHandler()
    deployment_reads = 0

    def changing_deployment_id(self):
        nonlocal deployment_reads
        deployment_reads += 1
        return f"deployment-{deployment_reads}"

    monkeypatch.setattr(LiteLLMAIHandler, "deployment_id", property(changing_deployment_id))
    completion = AsyncMock(side_effect=[
        openai.APIError("retry", request=httpx.Request("POST", "https://azure.example"), body=None),
        _mock_response(),
    ])

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", completion):
        await handler.chat_completion(
            model="azure_text/gpt-3.5-turbo-instruct",
            system="sys",
            user="usr",
        )

    assert [call.kwargs["model"] for call in completion.await_args_list] == [
        "azure_text/deployment-1",
        "azure_text/deployment-1",
    ]
    assert deployment_reads == 1


@pytest.mark.parametrize("model", (
    "text-completion-openai/gpt-3.5-turbo-instruct",
    "azure_text/gpt-3.5-turbo-instruct",
))
@pytest.mark.asyncio
async def test_azure_text_alias_probe_uses_deployment_id_as_model(monkeypatch, model):
    overrides = {
        "OPENAI.API_TYPE": "azure",
        "OPENAI.KEY": "azure-key",
        "OPENAI.API_BASE": "https://azure.example",
        "OPENAI.API_VERSION": "2026-01-01",
        "OPENAI.DEPLOYMENT_ID": "azure-text-deployment",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    completion = AsyncMock(return_value=_mock_response())

    await LiteLLMAIHandler().probe_completion(
        model,
        _completion=completion,
    )

    assert completion.call_args.kwargs["model"] == "azure_text/azure-text-deployment"
    assert "deployment_id" not in completion.call_args.kwargs
    assert completion.call_args.kwargs["api_key"] == "azure-key"
    assert completion.call_args.kwargs["api_base"] == "https://azure.example"
    assert completion.call_args.kwargs["api_version"] == "2026-01-01"


@pytest.mark.parametrize("model", ("gpt-5_thinking", "openai/gpt-5_thinking"))
@pytest.mark.asyncio
async def test_health_probe_normalizes_gpt5_thinking_model(monkeypatch, model):
    monkeypatch.setattr(litellm_handler, "get_settings", _make_settings)
    completion = AsyncMock(return_value=_mock_response())

    await LiteLLMAIHandler().probe_completion(model, _completion=completion)

    assert completion.call_args.kwargs["model"] == "openai/gpt-5"


@pytest.mark.asyncio
async def test_azure_ad_token_is_not_resolved_for_explicit_non_azure_provider(monkeypatch):
    overrides = {
        "AZURE_AD.CLIENT_ID": "client-id",
        "ANTHROPIC.KEY": "anthropic-key",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    credential = object()
    get_token = MagicMock(side_effect=RuntimeError("Azure AD unavailable"))
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_credential", lambda settings: credential)
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_token", get_token)

    kwargs = await _call(LiteLLMAIHandler(), "anthropic/claude-sonnet-4-5")

    assert kwargs["model"] == "anthropic/claude-sonnet-4-5"
    assert kwargs["api_key"] == "anthropic-key"
    get_token.assert_not_called()
