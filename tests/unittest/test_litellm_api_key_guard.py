"""Tests for request-local LiteLLM provider credentials and endpoints."""

import asyncio
import atexit
import inspect
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote, urlparse

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
    from google.auth import pluggable
    from litellm.llms.azure_ai.chat.transformation import AzureAIStudioConfig
    from litellm.llms.openai.chat.gpt_transformation import OpenAIGPTConfig
    from litellm.llms.ragflow.chat.transformation import RAGFlowConfig
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase
    from litellm.llms.xai.chat.transformation import XAIChatConfig

    for config in (OpenAIGPTConfig, AzureAIStudioConfig, RAGFlowConfig, XAIChatConfig):
        monkeypatch.setattr(config, "validate_environment", config.validate_environment)
    executable_descriptors = {
        name: pluggable.Credentials.__dict__.get(name)
        for name in ("from_info", "refresh", "expired", "token_state")
    }
    monkeypatch.setattr(VertexBase, "load_auth", VertexBase.load_auth)
    monkeypatch.setattr(VertexBase, "_credentials_from_service_account", VertexBase._credentials_from_service_account)
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
        "ARK_API_KEY",
        "SNOWFLAKE_ACCOUNT_ID",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION_NAME",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_BEDROCK_RUNTIME_ENDPOINT",
        "BEDROCK_MANTLE_REGION",
        "ANTHROPIC_AUTH_TOKEN",
        "PALM_API_KEY",
        "MOONSHOT_API_BASE",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "AZURE_API_BASE",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_API_VERSION",
        "AZURE_AD_TOKEN",
        "AZURE_OPENAI_AD_TOKEN",
        "OPENROUTER_API_BASE",
        "VERTEXAI_PROJECT",
        "VERTEXAI_LOCATION",
        "VERTEXAI_CREDENTIALS",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUDSDK_CONFIG",
        "CLOUDSDK_CORE_PROJECT",
        "CLOUDSDK_ACTIVE_CONFIG_NAME",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "APPENGINE_RUNTIME",
        "OPENAI_PROJECT_ID",
        "OPENAI_ORG_ID",
        "OPENAI_CUSTOM_HEADERS",
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
    for name, descriptor in executable_descriptors.items():
        if descriptor is not None:
            setattr(pluggable.Credentials, name, descriptor)
        elif name in pluggable.Credentials.__dict__:
            delattr(pluggable.Credentials, name)
    litellm_handler.AnthropicModelInfo.get_api_key = anthropic_get_api_key
    litellm_handler.AnthropicModelInfo.get_auth_token = anthropic_get_auth_token
    litellm_handler._anthropic_get_api_key = module_get_api_key
    litellm_handler._anthropic_get_auth_token = module_get_auth_token
    if bedrock_mantle_auth_mixin is not None:
        bedrock_mantle_auth_mixin.sign_request = bedrock_mantle_sign_request
        bedrock_mantle_auth_mixin._resolve_bearer_token = bedrock_mantle_resolve_bearer_token
    litellm_handler._bedrock_mantle_sign_request = module_bedrock_mantle_sign_request
    litellm_handler._bedrock_mantle_resolve_bearer_token = module_bedrock_mantle_resolve_bearer_token


async def _assert_native_azure_ad_auth(
    monkeypatch, transport, entrypoint, source, token_variable, *, captured_key=None, auth_headers=None,
    expected_authorization="Bearer request-ad-token", expected_api_key=None, sdk_alias=None, alias_mutation="replace",
    request_count=1, refresh_tokens=False, concurrent_handlers=False,
):
    from litellm.caching.caching import DualCache
    from litellm.caching.llm_caching_handler import LLMClientCache
    from litellm.litellm_core_utils import logging_worker
    from litellm.llms.azure import common_utils as azure_common

    endpoint = "https://request.openai.azure.com"
    if transport == "cloudflare":
        endpoint = "https://gateway.ai.cloudflare.com/v1/account/gateway/azure-openai/resource"
    settings = _make_settings({
        "OPENAI.API_TYPE": "azure",
        "OPENAI.KEY": captured_key,
        "OPENAI.API_BASE": endpoint,
        "OPENAI.API_VERSION": "v1" if transport == "v1" else "2024-06-01",
        "AZURE_AD.CLIENT_ID": "request-client" if source == "client_credential" else None,
        "LITELLM.EXTRA_HEADERS": json.dumps({"x-request-header": "owned", **(auth_headers or {})}),
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    monkeypatch.setattr(litellm_handler, "_get_azure_ad_credential", lambda settings: object())
    tokens = iter(f"request-ad-token-{index}" for index in range(request_count))
    monkeypatch.setattr(
        litellm_handler, "_get_azure_ad_token",
        lambda credential: next(tokens) if refresh_tokens else "request-ad-token",
    )
    if token_variable:
        monkeypatch.setenv(token_variable, "oidc/request" if source == "oidc" else "request-ad-token")
    if sdk_alias is not None:
        monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", sdk_alias)
    monkeypatch.setenv("AZURE_CLIENT_ID", "request-client")
    monkeypatch.setenv("AZURE_TENANT_ID", "request-tenant")
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com")
    monkeypatch.setenv("AZURE_SCOPE", "https://cognitiveservices.azure.com/.default")
    monkeypatch.setattr(azure_common, "azure_ad_cache", DualCache())
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", LLMClientCache())
    exchanged = []
    assertions = []

    def assertion(selector):
        assert selector == "oidc/request"
        assertions.append(selector)
        return "request-assertion"

    def exchange(url, *, data):
        assert url == "https://login.microsoftonline.com/request-tenant/oauth2/v2.0/token"
        assert data["client_id"] == "request-client"
        assert data["client_assertion"] == "request-assertion"
        assert data["scope"] == "https://cognitiveservices.azure.com/.default"
        exchanged.append(data)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "access_token": "request-ad-token", "expires_in": 3600,
        })

    monkeypatch.setattr(azure_common, "get_secret_str", assertion)
    monkeypatch.setattr(litellm.module_level_client, "post", exchange)
    handler = LiteLLMAIHandler()
    second_handler = None
    if concurrent_handlers:
        monkeypatch.setenv(token_variable, "request-ad-token-second")
        second_handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "api_key", "foreign-global-key")
    monkeypatch.setenv("AZURE_AD_TOKEN", "later-ad-token")
    if alias_mutation == "remove":
        monkeypatch.delenv("AZURE_OPENAI_AD_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "later-alias-token")
    captured = []
    both_started = asyncio.Event()

    async def send(client, request, **kwargs):
        captured.append(request)
        if concurrent_handlers:
            if len(captured) == 2:
                both_started.set()
            await both_started.wait()
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        async def invoke(selected):
            if entrypoint == "chat":
                await selected.chat_completion("azure/gpt-4o", "sys", "usr")
            else:
                await selected.probe_completion("azure/gpt-4o")

        if concurrent_handlers:
            async with asyncio.TaskGroup() as group:
                group.create_task(invoke(handler))
                group.create_task(invoke(second_handler))
        else:
            for _ in range(request_count):
                await invoke(handler)
    finally:
        await asyncio.sleep(0)
        try:
            await worker.flush()
        finally:
            await worker.stop()
    assert len(captured) == request_count
    if transport == "v1":
        expected_url = f"{endpoint}/openai/v1/chat/completions"
    elif transport == "cloudflare":
        expected_url = f"{endpoint}/gpt-4o/chat/completions?api-version=2024-06-01"
    else:
        expected_url = f"{endpoint}/openai/deployments/gpt-4o/chat/completions?api-version=2024-06-01"
    for index, request in enumerate(captured):
        assert str(request.url) == expected_url
        authorization = f"Bearer request-ad-token-{index}" if refresh_tokens else expected_authorization
        if concurrent_handlers:
            assert request.headers.get("Authorization") in {"Bearer request-ad-token", "Bearer request-ad-token-second"}
        else:
            assert request.headers.get("Authorization") == authorization
        assert request.headers.get("api-key") == expected_api_key
        assert request.headers["x-request-header"] == "owned"
        assert b"request-ad-token" not in request.content
        assert b"oidc/request" not in request.content
    assert len(exchanged) == (1 if source == "oidc" else 0)
    # Native SDK reuse bypasses exchange; the Cloudflare header path resolves
    # the assertion per call while retaining the native token exchange cache.
    assertion_count = request_count if transport == "cloudflare" else 1
    assert len(assertions) == (assertion_count if source == "oidc" else 0)
    if concurrent_handlers:
        assert {request.headers.get("Authorization") for request in captured} == {
            "Bearer request-ad-token", "Bearer request-ad-token-second",
        }
    assert litellm.api_key == "foreign-global-key"


@pytest.mark.parametrize("transport", ("classic", "v1", "cloudflare"))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize(("source", "token_variable"), (
    ("token", "AZURE_AD_TOKEN"),
    ("token", "AZURE_OPENAI_AD_TOKEN"),
    ("oidc", "AZURE_AD_TOKEN"),
    ("oidc", "AZURE_OPENAI_AD_TOKEN"),
    ("client_credential", None),
))
@pytest.mark.asyncio
async def test_native_azure_ad_auth_survives_guard_key(monkeypatch, transport, entrypoint, source, token_variable):
    await _assert_native_azure_ad_auth(monkeypatch, transport, entrypoint, source, token_variable)


@pytest.mark.parametrize("transport", ("classic", "v1", "cloudflare"))
@pytest.mark.parametrize("captured_key", ("request-api-key", DUMMY_LITELLM_API_KEY))
@pytest.mark.parametrize("token_variable", ("AZURE_AD_TOKEN", "AZURE_OPENAI_AD_TOKEN"))
@pytest.mark.asyncio
async def test_native_azure_real_api_key_is_not_a_guard(monkeypatch, transport, captured_key, token_variable):
    authorization, api_key = "Bearer request-ad-token", None
    if transport == "v1":
        authorization = f"Bearer {captured_key}"
    elif transport == "cloudflare" and token_variable == "AZURE_AD_TOKEN":
        authorization, api_key = None, captured_key
    await _assert_native_azure_ad_auth(
        monkeypatch, transport, "chat", "token", token_variable, captured_key=captured_key,
        expected_authorization=authorization, expected_api_key=api_key,
    )


@pytest.mark.parametrize("token_variable", ("AZURE_AD_TOKEN", "AZURE_OPENAI_AD_TOKEN"))
@pytest.mark.parametrize("transport", ("classic", "v1", "cloudflare"))
@pytest.mark.parametrize("auth_headers", (
    {"authorization": "Bearer explicit-token"},
    {"API-Key": "explicit-key"},
    {"authorization": "Bearer explicit-token", "API-Key": "explicit-key"},
))
@pytest.mark.asyncio
async def test_native_azure_explicit_auth_headers(monkeypatch, token_variable, transport, auth_headers):
    authorization = auth_headers.get("authorization")
    api_key = auth_headers.get("API-Key")
    if authorization is None and (transport != "cloudflare" or token_variable == "AZURE_OPENAI_AD_TOKEN"):
        authorization = "Bearer request-ad-token"
    await _assert_native_azure_ad_auth(
        monkeypatch, transport, "chat", "token", token_variable, auth_headers=auth_headers,
        expected_authorization=authorization, expected_api_key=api_key,
    )


@pytest.mark.parametrize("captured_key", (None, "request-api-key"))
@pytest.mark.parametrize("sdk_alias", (None, "initial-sdk-token"))
@pytest.mark.parametrize("alias_mutation", ("replace", "remove"))
@pytest.mark.parametrize("auth_headers", (
    {},
    {"authorization": "Bearer explicit-token"},
    {"API-Key": "explicit-key"},
    {"authorization": "Bearer explicit-token", "API-Key": "explicit-key"},
))
@pytest.mark.asyncio
async def test_native_cloudflare_initial_auth_choice(
    monkeypatch, captured_key, sdk_alias, alias_mutation, auth_headers,
):
    authorization = auth_headers.get("authorization")
    api_key = auth_headers.get("API-Key")
    if captured_key is None and api_key is None:
        authorization = authorization or "Bearer request-ad-token"
    else:
        authorization = authorization or (f"Bearer {sdk_alias}" if sdk_alias else None)
        api_key = api_key or (None if sdk_alias else captured_key)
    await _assert_native_azure_ad_auth(
        monkeypatch, "cloudflare", "probe", "token", "AZURE_AD_TOKEN", captured_key=captured_key,
        auth_headers=auth_headers, expected_authorization=authorization, expected_api_key=api_key,
        sdk_alias=sdk_alias, alias_mutation=alias_mutation,
    )


@pytest.mark.parametrize("transport", ("classic", "v1", "cloudflare"))
@pytest.mark.parametrize("source", ("client_credential", "oidc"))
@pytest.mark.asyncio
async def test_native_azure_ad_refresh_and_oidc_cache(monkeypatch, transport, source):
    await _assert_native_azure_ad_auth(
        monkeypatch, transport, "probe", source, "AZURE_AD_TOKEN" if source == "oidc" else None,
        request_count=2, refresh_tokens=source == "client_credential",
    )


@pytest.mark.parametrize("transport", ("classic", "v1", "cloudflare"))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.asyncio
async def test_native_concurrent_azure_ad_tokens(monkeypatch, transport, entrypoint):
    await _assert_native_azure_ad_auth(
        monkeypatch, transport, entrypoint, "token", "AZURE_AD_TOKEN", request_count=2, concurrent_handlers=True,
    )


@pytest.mark.parametrize("model", ("azure_text/gpt-4o", "azure/responses/gpt-4o", "openai/gpt-4o"))
@pytest.mark.asyncio
async def test_azure_header_adapter_does_not_touch_other_transports(model):
    handler = LiteLLMAIHandler()
    completion = AsyncMock(return_value=_mock_response())
    kwargs = {"model": model, "api_key": DUMMY_LITELLM_API_KEY, "azure_ad_token": "request-token"}
    await handler._acompletion(_completion=completion, **kwargs)
    assert completion.call_args.kwargs == kwargs


@pytest.mark.asyncio
async def test_azure_oidc_exchange_failure_cannot_use_guard_key(monkeypatch, native_azure_oidc):
    state = native_azure_oidc
    handler = LiteLLMAIHandler()
    exchange = MagicMock(side_effect=RuntimeError("exchange failed"))
    monkeypatch.setattr(litellm.module_level_client, "post", exchange)
    with pytest.raises(litellm.APIError, match="exchange failed"):
        await state.invoke(handler)
    exchange.assert_called_once()
    assert state.sent == []
    assert litellm.api_key == "foreign-global-key"


@pytest.fixture
async def native_azure_oidc(monkeypatch):
    from litellm.caching.caching import DualCache
    from litellm.caching.llm_caching_handler import LLMClientCache
    from litellm.litellm_core_utils import logging_worker
    from litellm.llms.azure import common_utils as azure_common

    litellm_handler._azure_oidc_entra_provider.cache_clear()
    inputs = {
        "AZURE_CLIENT_ID": "owned-client",
        "AZURE_TENANT_ID": "owned-tenant",
        "AZURE_AUTHORITY_HOST": "https://owned-authority.example",
        "AZURE_SCOPE": "https://cognitiveservices.azure.com/.default",
    }
    for name in ("AZURE_CLIENT_SECRET", "AZURE_USERNAME", "AZURE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    for name, value in inputs.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AZURE_AD_TOKEN", "oidc/env/PR_AGENT_TEST_OIDC_ASSERTION")
    monkeypatch.setenv("PR_AGENT_TEST_OIDC_ASSERTION", "owned-assertion")
    settings = _make_settings({
        "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": "https://owned.openai.azure.com",
        "OPENAI.API_VERSION": "2024-06-01",
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    cache = DualCache()
    monkeypatch.setattr(azure_common, "azure_ad_cache", cache)
    # Preserve warm reuse within a case without inheriting another test's SDK client.
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", LLMClientCache())
    native = azure_common.get_azure_ad_token_from_oidc
    cache_methods = (cache.get_cache.__func__, cache.set_cache.__func__)
    state = SimpleNamespace(inputs=inputs, cache=cache, exchanged=[], sent=[], status=200, ttl=3600, ambient=None)
    state.expected_api_key = None
    state.expected_url = (
        "https://owned.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-06-01"
    )
    state.response_kind = "chat"
    state.stream = False

    def exchange(url, *, data):
        if state.ambient is not None:
            assert dict(os.environ) == state.ambient
        assert azure_common.azure_ad_cache is cache
        assert (cache.get_cache.__func__, cache.set_cache.__func__) == cache_methods
        state.exchanged.append((url, dict(data)))
        return httpx.Response(state.status, request=httpx.Request("POST", url), json={
            "access_token": f"exchanged-token-{len(state.exchanged)}", "expires_in": state.ttl,
        })

    async def send(client, request, **kwargs):
        state.sent.append(request)
        expected_keys = [] if state.expected_api_key is None else [state.expected_api_key]
        assert request.headers.get_list("api-key") == expected_keys
        assert str(request.url) == state.expected_url
        assert "oidc/" not in str(request.headers)
        assert "assertion" not in str(request.headers)
        assert b"oidc/" not in request.content
        assert b"assertion" not in request.content
        assert b"exchanged-token" not in request.content
        if state.response_kind == "responses":
            return httpx.Response(200, request=request, json={
                "id": "resp_test", "object": "response", "created_at": 0, "model": "gpt-4o",
                "status": "completed", "error": None, "incomplete_details": None,
                "output": [{"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            })
        if state.response_kind == "text":
            payload = {
                "id": "test", "object": "text_completion", "created": 0, "model": "gpt-3.5-turbo-instruct",
                "choices": [{"index": 0, "text": "ok", "logprobs": None, "finish_reason": "stop"}],
            }
            if state.stream:
                return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                      content=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode())
            return httpx.Response(200, request=request, json=payload)
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    async def invoke(handler, entrypoint="probe", guard_key=True, model="azure/gpt-4o"):
        # Cover native keyless dispatch as well as the request-local guard used
        # to prevent a process-wide API key from replacing OIDC authentication.
        ambient_key = "foreign-global-key" if guard_key else None
        monkeypatch.setattr(litellm, "api_key", ambient_key)
        environment = dict(os.environ)
        state.ambient = environment
        try:
            if entrypoint == "chat":
                await handler.chat_completion(model, "sys", "usr")
            else:
                await handler.probe_completion(model)
        finally:
            assert dict(os.environ) == environment
            assert azure_common.azure_ad_cache is cache
            assert (cache.get_cache.__func__, cache.set_cache.__func__) == cache_methods
            assert litellm.api_key == ambient_key
            assert litellm_handler._azure_oidc_request.get() is None
            assert litellm_handler._azure_ad_responses_request.get() is None
        return state.sent[-1].headers.get("Authorization")

    state.invoke = invoke
    monkeypatch.setattr(litellm.module_level_client, "post", exchange)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        yield state
    finally:
        litellm_handler._azure_oidc_entra_provider.cache_clear()
        assert native.__globals__["os"] is os
        await asyncio.sleep(0)
        try:
            await worker.flush()
        finally:
            await worker.stop()


@pytest.mark.parametrize("field", ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_AUTHORITY_HOST", "AZURE_SCOPE"))
@pytest.mark.parametrize("mutation", ("changed", "removed", "absent", "empty"))
@pytest.mark.parametrize("guard_key", (False, True))
@pytest.mark.asyncio
async def test_native_azure_oidc_outer_snapshot(monkeypatch, native_azure_oidc, field, mutation, guard_key):
    state = native_azure_oidc
    captured = dict(state.inputs)
    if field == "AZURE_SCOPE":
        captured[field] = "owned-scope"
        monkeypatch.setenv(field, captured[field])
    if mutation == "absent":
        monkeypatch.delenv(field)
        captured[field] = None
    elif mutation == "empty":
        monkeypatch.setenv(field, "")
        captured[field] = ""
    handler = LiteLLMAIHandler()
    if mutation == "removed":
        monkeypatch.delenv(field)
    else:
        monkeypatch.setenv(field, "https://foreign.example" if "HOST" in field else "foreign-value")
    if mutation == "absent" and field in {"AZURE_CLIENT_ID", "AZURE_TENANT_ID"}:
        with pytest.raises(litellm.BadRequestError, match="AZURE_CLIENT_ID and AZURE_TENANT_ID must be set"):
            await state.invoke(handler, guard_key=guard_key)
        assert state.exchanged == state.sent == []
        return
    assert await state.invoke(handler, guard_key=guard_key) == "Bearer exchanged-token-1"
    authority = captured["AZURE_AUTHORITY_HOST"]
    if authority is None:
        authority = "https://login.microsoftonline.com"
    scope = captured["AZURE_SCOPE"]
    if scope is None:
        scope = "https://cognitiveservices.azure.com/.default"
    assert state.exchanged == [(f"{authority}/{captured['AZURE_TENANT_ID']}/oauth2/v2.0/token", {
        "client_id": captured["AZURE_CLIENT_ID"], "grant_type": "client_credentials", "scope": scope,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": "owned-assertion",
    })]


@pytest.mark.asyncio
async def test_native_azure_oidc_thread_boundary_snapshot(monkeypatch, native_azure_oidc):
    state = native_azure_oidc
    handler = LiteLLMAIHandler()
    loop = asyncio.get_running_loop()
    run_in_executor = loop.run_in_executor
    boundaries = []

    def mutate_then_dispatch(executor, func, *args):
        # LiteLLM dispatches native completion through this executor boundary.
        if not boundaries:
            boundaries.append(func)
            for name in state.inputs:
                monkeypatch.setenv(name, "https://foreign.example" if "HOST" in name else "foreign-value")
            state.ambient = dict(os.environ)
        return run_in_executor(executor, func, *args)

    monkeypatch.setattr(loop, "run_in_executor", mutate_then_dispatch)
    monkeypatch.setattr(litellm, "api_key", "foreign-global-key")
    # This test deliberately changes the environment at dispatch, outside the adapter.
    await handler.chat_completion("azure/gpt-4o", "sys", "usr")
    assert len(boundaries) == 1
    url, data = state.exchanged[0]
    assert url == "https://owned-authority.example/owned-tenant/oauth2/v2.0/token"
    assert data["client_id"] == "owned-client"
    assert data["scope"] == state.inputs["AZURE_SCOPE"]
    assert state.sent[0].headers["Authorization"] == "Bearer exchanged-token-1"
    assert all("foreign" in os.environ[name] for name in state.inputs)


@pytest.mark.parametrize("transport", ("classic", "v1", "responses", "text", "cloudflare", "cloudflare_text"))
@pytest.mark.parametrize("token_kind", ("ordinary", "oidc"))
@pytest.mark.parametrize("key", (None, "owned-key", "dummy_key"))
@pytest.mark.parametrize("late_key", (None, "api_key", "azure_key", "AZURE_API_KEY", "AZURE_OPENAI_API_KEY"))
@pytest.mark.asyncio
async def test_native_azure_token_executor_boundary(
    monkeypatch, native_azure_oidc, transport, token_kind, key, late_key,
):
    state = native_azure_oidc
    if token_kind == "ordinary":
        monkeypatch.setenv("AZURE_AD_TOKEN", "owned-ad-token")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", False)
    endpoint = "https://owned.openai.azure.com"
    if transport.startswith("cloudflare"):
        endpoint = "https://gateway.ai.cloudflare.com/v1/account/gateway/azure-openai/resource"
    version = "v1" if transport == "v1" else "2024-06-01"
    settings = _make_settings({
        "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": endpoint,
        "OPENAI.API_VERSION": version, "OPENAI.KEY": key,
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    model = "azure/gpt-4o"
    if transport in ("text", "cloudflare_text"):
        model = "azure_text/gpt-3.5-turbo-instruct"
        state.response_kind = "text"
    elif transport == "responses":
        model = "azure/responses/gpt-4o"
        state.response_kind = "responses"
    if transport == "v1":
        state.expected_url = f"{endpoint}/openai/v1/chat/completions"
    elif transport == "responses":
        state.expected_url = f"{endpoint}/openai/responses?api-version={version}"
    elif transport.startswith("cloudflare"):
        suffix = "gpt-3.5-turbo-instruct/completions" if transport.endswith("text") else "gpt-4o/chat/completions"
        state.expected_url = f"{endpoint}/{suffix}?api-version={version}"
    elif transport == "text":
        state.expected_url = f"{endpoint}/openai/deployments/gpt-3.5-turbo-instruct/completions?api-version={version}"
    expected = "Bearer owned-ad-token" if token_kind == "ordinary" else "Bearer exchanged-token-1"
    if key and (transport == "responses" or transport.startswith("cloudflare")):
        expected, state.expected_api_key = None, key
    elif key and transport == "v1":
        expected = f"Bearer {key}"
    handler = LiteLLMAIHandler()
    loop = asyncio.get_running_loop()
    run_in_executor = loop.run_in_executor
    boundaries = []

    def mutate_then_dispatch(executor, function, *args):
        if not boundaries:
            boundaries.append(function)
            if late_key in ("api_key", "azure_key"):
                monkeypatch.setattr(litellm, late_key, "foreign-key")
            elif late_key:
                monkeypatch.setenv(late_key, "foreign-key")
            state.ambient = dict(os.environ)
        return run_in_executor(executor, function, *args)

    monkeypatch.setattr(loop, "run_in_executor", mutate_then_dispatch)
    await handler.probe_completion(model)
    assert boundaries
    assert len(state.sent) == 1
    assert state.sent[0].headers.get("Authorization") == expected
    assert litellm_handler._azure_oidc_request.get() is None
    assert litellm_handler._azure_ad_responses_request.get() is None


@pytest.mark.parametrize("identity", ("same", "client", "tenant", "authority", "scope", "default", "empty", "legacy"))
@pytest.mark.asyncio
async def test_native_azure_oidc_scope_cache(monkeypatch, native_azure_oidc, identity):
    state = native_azure_oidc
    if identity == "default":
        monkeypatch.delenv("AZURE_SCOPE")
    native_key = json.dumps({
        "azure_client_id": "owned-client", "azure_tenant_id": "owned-tenant",
        "azure_authority_host": "https://owned-authority.example", "oidc_token": "owned-assertion",
    })
    if identity == "legacy":
        state.cache.set_cache(native_key, "old-unnamespaced-token", ttl=3600)
    first = LiteLLMAIHandler()
    assert await state.invoke(first) == "Bearer exchanged-token-1"
    fields = {"client": "AZURE_CLIENT_ID", "tenant": "AZURE_TENANT_ID", "authority": "AZURE_AUTHORITY_HOST"}
    if identity in fields:
        monkeypatch.setenv(fields[identity], "https://second.example" if identity == "authority" else "second")
    elif identity in {"scope", "empty", "default"}:
        monkeypatch.setenv("AZURE_SCOPE", {
            "scope": "second-scope", "empty": "", "default": state.inputs["AZURE_SCOPE"],
        }[identity])
    second = LiteLLMAIHandler()
    count = 1 if identity in {"same", "default", "legacy"} else 2
    assert await state.invoke(second) == f"Bearer exchanged-token-{count}"
    assert len(state.exchanged) == count
    # An older handler keeps its identity even after a second handler is captured.
    assert await state.invoke(first) == "Bearer exchanged-token-1"
    assert len(state.exchanged) == count
    namespaced_key = json.dumps(["pr-agent.azure-oidc.v1", state.inputs["AZURE_SCOPE"], native_key])
    assert state.cache.get_cache(namespaced_key) == "exchanged-token-1"
    assert state.cache.get_cache(native_key) == ("old-unnamespaced-token" if identity == "legacy" else None)


@pytest.mark.parametrize("refresh", ("unchanged", "assertion", "expiry"))
@pytest.mark.asyncio
async def test_native_azure_oidc_assertion_rotation(monkeypatch, native_azure_oidc, refresh):
    state = native_azure_oidc
    # Responses executes token selection per request, so this exercises the
    # token cache independently of native SDK client reuse (covered below).
    state.response_kind = "responses"
    state.expected_url = "https://owned.openai.azure.com/openai/responses?api-version=2024-06-01"
    model = "azure/responses/gpt-4o"
    if refresh == "expiry":
        state.ttl = -1
    handler = LiteLLMAIHandler()
    assert await state.invoke(handler, "chat", model=model) == "Bearer exchanged-token-1"
    monkeypatch.setenv("AZURE_AD_TOKEN", "oidc/env/PR_AGENT_TEST_FOREIGN_ASSERTION")
    monkeypatch.setenv("PR_AGENT_TEST_FOREIGN_ASSERTION", "foreign-assertion")
    if refresh == "assertion":
        monkeypatch.setenv("PR_AGENT_TEST_OIDC_ASSERTION", "rotated-assertion")
    count = 1 if refresh == "unchanged" else 2
    assert await state.invoke(handler, "chat", model=model) == f"Bearer exchanged-token-{count}"
    expected = ["owned-assertion"]
    if count == 2:
        expected.append("rotated-assertion" if refresh == "assertion" else "owned-assertion")
    assert [data["client_assertion"] for _, data in state.exchanged] == expected


@pytest.mark.parametrize("failure", (
    "assertion", "exchange", "interface", "interface_kind", "interface_default", "snapshot",
))
@pytest.mark.asyncio
async def test_native_azure_oidc_fail_closed(monkeypatch, native_azure_oidc, failure):
    from litellm.llms.azure import common_utils as azure_common

    state = native_azure_oidc
    handler = LiteLLMAIHandler()
    if failure == "assertion":
        monkeypatch.delenv("PR_AGENT_TEST_OIDC_ASSERTION")
        error, message = litellm.APIError, "PR_AGENT_TEST_OIDC_ASSERTION not found"
    elif failure == "exchange":
        state.status = 401
        error, message = litellm.AuthenticationError, "exchanged-token"
    elif failure == "snapshot":
        del handler._azure_oidc_environment["AZURE_SCOPE"]
        error, message = RuntimeError, "Azure OIDC exchange snapshot is incomplete"
    elif failure in {"interface_kind", "interface_default"}:
        # Mutate the native interface, not a previously installed bridge wrapper.
        native = inspect.unwrap(azure_common.get_azure_ad_token_from_oidc)
        signature = inspect.signature(native)
        parameters = list(signature.parameters.values())
        parameter = parameters[-1]
        parameters[-1] = (
            parameter.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            if failure == "interface_kind" else parameter.replace(default="changed-default")
        )
        monkeypatch.setattr(native, "__signature__", signature.replace(parameters=parameters), raising=False)
        error, message = litellm.APIError, "Azure OIDC exchange interface is incompatible"
    else:
        # A callable with an incompatible native signature must never be invoked.
        monkeypatch.setattr(azure_common, "get_azure_ad_token_from_oidc", lambda selector: "unsafe-token")
        error, message = RuntimeError, "(?i)azure|oidc|incompatible"
    with pytest.raises(error, match=message):
        await state.invoke(handler)
    assert len(state.exchanged) == (1 if failure == "exchange" else 0)
    assert state.sent == []


@pytest.mark.parametrize("mutation", ("changed", "removed"))
@pytest.mark.parametrize("guard_key", (False, True))
@pytest.mark.asyncio
async def test_native_azure_oidc_bridge_responses(monkeypatch, native_azure_oidc, mutation, guard_key):
    from litellm.llms.azure import common_utils as azure_common

    state = native_azure_oidc
    state.response_kind = "responses"
    state.expected_url = "https://owned.openai.azure.com/openai/responses?api-version=2024-06-01"
    handler = LiteLLMAIHandler()
    for field in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID"):
        if mutation == "removed":
            monkeypatch.delenv(field)
        else:
            monkeypatch.setenv(field, "foreign-identity")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", True)
    provider = MagicMock(side_effect=AssertionError("OIDC must not create an additional credential provider"))
    monkeypatch.setattr(azure_common, "get_azure_ad_token_provider", provider)
    assert await state.invoke(handler, guard_key=guard_key, model="azure/responses/gpt-4o") == "Bearer exchanged-token-1"
    assert len(state.exchanged) == 1
    url, data = state.exchanged[0]
    assert url == "https://owned-authority.example/owned-tenant/oauth2/v2.0/token"
    assert data["client_id"] == "owned-client"
    provider.assert_not_called()


@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("key", (None, "owned-key", "dummy_key"))
@pytest.mark.parametrize("ambient", ("none", "global", "azure", "environment"))
@pytest.mark.asyncio
async def test_native_azure_ad_responses_guard(monkeypatch, native_azure_oidc, entrypoint, key, ambient):
    state = native_azure_oidc
    state.response_kind = "responses"
    state.expected_url = "https://owned.openai.azure.com/openai/responses?api-version=2024-06-01"
    state.expected_api_key = key
    monkeypatch.setenv("AZURE_AD_TOKEN", "owned-ad-token")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", False)
    settings = _make_settings({
        "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": "https://owned.openai.azure.com",
        "OPENAI.API_VERSION": "2024-06-01", "OPENAI.KEY": key,
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    handler = LiteLLMAIHandler()
    if ambient == "azure":
        monkeypatch.setattr(litellm, "azure_key", "foreign-key")
    elif ambient == "environment":
        monkeypatch.setenv("AZURE_API_KEY", "foreign-key")
    token = await state.invoke(handler, entrypoint=entrypoint, guard_key=ambient == "global",
                               model="azure/responses/gpt-4o")
    assert token == (None if key else "Bearer owned-ad-token")
    assert len(state.sent) == 1
    assert state.exchanged == []


@pytest.mark.parametrize("source", ("client_secret", "password"))
@pytest.mark.parametrize("timing", ("initial", "late"))
@pytest.mark.parametrize("guard_key", (False, True))
@pytest.mark.parametrize("refresh", (False, True))
@pytest.mark.asyncio
async def test_native_azure_ad_responses_companion(monkeypatch, native_azure_oidc, source, timing, guard_key, refresh):
    import azure.identity

    state = native_azure_oidc
    state.response_kind = "responses"
    state.expected_url = "https://owned.openai.azure.com/openai/responses?api-version=2024-06-01"
    monkeypatch.setenv("AZURE_AD_TOKEN", "owned-ad-token")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", refresh)
    fields = {"AZURE_CLIENT_SECRET": "owned-secret"} if source == "client_secret" else {
        "AZURE_USERNAME": "owned-user", "AZURE_PASSWORD": "owned-password",
    }
    if timing == "initial":
        for name, value in fields.items():
            monkeypatch.setenv(name, value)
    handler = LiteLLMAIHandler()
    for name in fields:
        monkeypatch.setenv(name, "foreign-value")
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://foreign-authority.example")
    credential_name = "ClientSecretCredential" if source == "client_secret" else "UsernamePasswordCredential"
    constructor = MagicMock(wraps=getattr(azure.identity, credential_name))
    monkeypatch.setattr(azure.identity, credential_name, constructor)
    provider = MagicMock(return_value=lambda: "provider-token")
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", provider)
    if refresh and timing == "late":
        with pytest.raises(litellm.APIConnectionError, match="implicit Azure AD credential discovery"):
            await state.invoke(handler, model="azure/responses/gpt-4o", guard_key=guard_key)
        constructor.assert_not_called()
        provider.assert_not_called()
        assert state.sent == state.exchanged == []
        return
    expected = "provider-token" if timing == "initial" else "owned-ad-token"
    assert await state.invoke(handler, model="azure/responses/gpt-4o", guard_key=guard_key) == f"Bearer {expected}"
    assert state.exchanged == []
    if timing == "initial":
        constructor.assert_called_once()
        provider.assert_called_once()
        assert constructor.call_args.kwargs["authority"] == "https://owned-authority.example"
        if source == "client_secret":
            assert constructor.call_args.args == ("owned-tenant", "owned-client", "owned-secret")
        else:
            assert constructor.call_args.kwargs["username"] == "owned-user"
            assert constructor.call_args.kwargs["password"] == "owned-password"
    else:
        constructor.assert_not_called()
        provider.assert_not_called()


@pytest.mark.parametrize("transport", ("classic", "v1"))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("source", ("client_secret", "password", "both", "incomplete", "password_without_tenant"))
@pytest.mark.parametrize("timing", ("initial", "late"))
@pytest.mark.parametrize("guard_key", (False, True))
@pytest.mark.parametrize("key", (None, "owned-key", "dummy_key"))
@pytest.mark.parametrize("refresh", (False, True))
@pytest.mark.asyncio
async def test_native_azure_ad_sdk_companion(
    monkeypatch, native_azure_oidc, transport, entrypoint, source, timing, guard_key, key, refresh,
):
    import azure.identity

    state = native_azure_oidc
    monkeypatch.setenv("AZURE_AD_TOKEN", "owned-ad-token")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", refresh)
    version = "v1" if transport == "v1" else "2024-06-01"
    settings = _make_settings({
        "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": "https://owned.openai.azure.com",
        "OPENAI.API_VERSION": version, "OPENAI.KEY": key,
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    if transport == "v1":
        state.expected_url = "https://owned.openai.azure.com/openai/v1/chat/completions"
    fields = {}
    if source in ("client_secret", "both", "incomplete", "password_without_tenant"):
        fields["AZURE_CLIENT_SECRET"] = "owned-secret"
    if source in ("password", "both", "password_without_tenant"):
        fields.update(AZURE_USERNAME="owned-user", AZURE_PASSWORD="owned-password")
    if source in ("incomplete", "password_without_tenant"):
        monkeypatch.delenv("AZURE_TENANT_ID")
    if timing == "initial":
        for name, value in fields.items():
            monkeypatch.setenv(name, value)
    handler = LiteLLMAIHandler()
    for name in fields:
        monkeypatch.setenv(name, "foreign-value")
    monkeypatch.setenv("AZURE_TENANT_ID", "foreign-tenant")
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://foreign-authority.example")
    constructors = {}
    for name in ("ClientSecretCredential", "UsernamePasswordCredential"):
        constructors[name] = MagicMock(wraps=getattr(azure.identity, name))
        monkeypatch.setattr(azure.identity, name, constructors[name])
    token_provider = MagicMock(return_value="companion-token")
    provider_factory = MagicMock(return_value=token_provider)
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", provider_factory)
    if refresh and not key and (timing == "late" or source == "incomplete"):
        with pytest.raises(Exception, match="implicit Azure AD credential discovery"):
            await state.invoke(handler, entrypoint, guard_key=guard_key)
        for constructor in constructors.values():
            constructor.assert_not_called()
        provider_factory.assert_not_called()
        assert state.sent == []
        assert state.exchanged == []
        return
    expected = "owned-ad-token"
    if transport == "v1":
        expected = key or ("companion-token" if timing == "initial" and source != "incomplete" else expected)
    assert await state.invoke(handler, entrypoint, guard_key=guard_key) == f"Bearer {expected}"
    if expected == "companion-token":
        token_provider.assert_called()
    selected = None
    if timing == "initial":
        if source in ("client_secret", "both") and not key:
            selected = "ClientSecretCredential"
        elif source in ("password", "both", "password_without_tenant"):
            selected = "UsernamePasswordCredential"
    for name, constructor in constructors.items():
        if name != selected:
            constructor.assert_not_called()
            continue
        constructor.assert_called_once()
        assert constructor.call_args.kwargs["authority"] == "https://owned-authority.example"
        if name == "ClientSecretCredential":
            assert constructor.call_args.args == ("owned-tenant", "owned-client", "owned-secret")
        else:
            assert constructor.call_args.kwargs["username"] == "owned-user"
            assert constructor.call_args.kwargs["password"] == "owned-password"
    assert state.exchanged == []


@pytest.mark.parametrize("transport", ("classic", "v1"))
@pytest.mark.parametrize("guard_key", (False, True))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.asyncio
async def test_native_azure_ad_sdk_rejects_implicit_refresh(monkeypatch, native_azure_oidc, transport, guard_key, entrypoint):
    import azure.identity

    state = native_azure_oidc
    monkeypatch.setenv("AZURE_AD_TOKEN", "owned-ad-token")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", True)
    settings = _make_settings({
        "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": "https://owned.openai.azure.com",
        "OPENAI.API_VERSION": "v1" if transport == "v1" else "2024-06-01",
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    if transport == "v1":
        state.expected_url = "https://owned.openai.azure.com/openai/v1/chat/completions"
    handler = LiteLLMAIHandler()
    for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET"):
        monkeypatch.setenv(name, "foreign-identity")
    constructor = MagicMock()
    provider_factory = MagicMock(return_value=lambda: "foreign-token")
    monkeypatch.setattr(azure.identity, "ClientSecretCredential", constructor)
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", provider_factory)
    with pytest.raises(Exception, match="implicit Azure AD credential discovery"):
        await state.invoke(handler, entrypoint, guard_key=guard_key)
    constructor.assert_not_called()
    provider_factory.assert_not_called()
    assert state.sent == []
    assert state.exchanged == []


@pytest.mark.parametrize("guard_key", (False, True))
@pytest.mark.asyncio
async def test_native_azure_ad_sdk_reuses_safe_client_when_refresh_enabled(monkeypatch, native_azure_oidc, guard_key):
    from litellm.llms.azure import common_utils as azure_common

    state = native_azure_oidc
    monkeypatch.setenv("AZURE_AD_TOKEN", "owned-ad-token")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", False)
    settings = _make_settings({
        "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": "https://owned.openai.azure.com", "OPENAI.API_VERSION": "v1",
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    state.expected_url = "https://owned.openai.azure.com/openai/v1/chat/completions"
    handler = LiteLLMAIHandler()
    assert await state.invoke(handler, guard_key=guard_key) == "Bearer owned-ad-token"
    factory = MagicMock(side_effect=AssertionError("Implicit discovery must not run for a cached client"))
    monkeypatch.setattr(azure_common, "get_azure_ad_token_provider", factory)
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", True)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "foreign-secret")
    assert await state.invoke(handler, guard_key=guard_key) == "Bearer owned-ad-token"
    factory.assert_not_called()
    assert len(state.sent) == 2


@pytest.fixture
async def native_companion_auth(monkeypatch, native_azure_oidc):
    from azure.core.pipeline.transport import RequestsTransport
    from litellm.llms.azure import common_utils

    caches = [litellm.in_memory_llm_clients_cache]
    common_utils._cached_entra_id_token_provider.cache_clear()

    def deny_auth_http(*args, **kwargs):
        raise AssertionError("Unexpected authentication HTTP")

    monkeypatch.setattr(RequestsTransport, "send", deny_auth_http)
    monkeypatch.setattr(httpx.Client, "send", deny_auth_http)
    try:
        yield caches
    finally:
        common_utils._cached_entra_id_token_provider.cache_clear()
        clients = {id(value): value for cache in caches for value in cache.cache_dict.values()}
        for client in clients.values():
            result = client.close()
            if inspect.isawaitable(result):
                await result


@pytest.mark.parametrize("transport", (
    "classic", "v1", "responses", "text", "raw", "raw_key_host", "sdk_alias", "responses_alias",
))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("source", ("client_secret", "password"))
@pytest.mark.parametrize("key", (None, "owned-key", "dummy_key"))
@pytest.mark.parametrize("guard_key", (False, True))
@pytest.mark.asyncio
async def test_native_tokenless_azure_companion(
    monkeypatch, native_azure_oidc, native_companion_auth, transport, entrypoint, source, key, guard_key,
):
    import azure.identity
    from litellm.caching.llm_caching_handler import LLMClientCache

    state = native_azure_oidc
    monkeypatch.delenv("AZURE_AD_TOKEN")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", False)
    if source == "client_secret":
        monkeypatch.setenv("AZURE_CLIENT_SECRET", "owned-secret")
    else:
        monkeypatch.setenv("AZURE_USERNAME", "owned-user")
        monkeypatch.setenv("AZURE_PASSWORD", "owned-password")
    version = "v1" if transport == "v1" else "2024-06-01"
    endpoint = "https://owned.openai.azure.com"
    settings = _make_settings({
        "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": endpoint,
        "OPENAI.API_VERSION": version, "OPENAI.KEY": key,
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    model = "azure/gpt-4o"
    if transport == "v1":
        state.expected_url = f"{endpoint}/openai/v1/chat/completions"
    elif transport == "responses":
        model = "azure/responses/gpt-4o"
        state.response_kind = "responses"
        state.expected_url = f"{endpoint}/openai/responses?api-version={version}"
    elif transport == "text":
        model = "azure_text/gpt-3.5-turbo-instruct"
        state.response_kind = "text"
        state.expected_url = f"{endpoint}/openai/deployments/gpt-3.5-turbo-instruct/completions?api-version={version}"
    elif transport.startswith("raw"):
        # OpenAI model names on azure_ai normalize to the Azure SDK instead.
        model = "azure_ai/test-model"
        endpoint = "https://owned.services.ai.azure.com" if transport == "raw_key_host" else "https://owned.example"
        monkeypatch.setenv("AZURE_AI_API_BASE", endpoint)
        if key:
            monkeypatch.setenv("AZURE_AI_API_KEY", key)
        settings = _make_settings()
        suffix = "/models/chat/completions" if transport == "raw_key_host" else "/chat/completions"
        state.expected_url = endpoint + suffix
    elif transport.endswith("_alias"):
        model = "azure_ai/responses/gpt-4o" if transport == "responses_alias" else "azure_ai/gpt-4o"
        if transport == "responses_alias":
            state.response_kind = "responses"
            state.expected_url = f"{endpoint}/openai/v1/responses?api-version=preview"
        monkeypatch.setenv("AZURE_AI_API_BASE", endpoint)
        monkeypatch.setenv("AZURE_API_VERSION", version)
        if key:
            monkeypatch.setenv("AZURE_AI_API_KEY", key)
        settings = _make_settings()
    state.expected_api_key = key if transport not in ("v1", "raw") else None
    factory = MagicMock(return_value=lambda: "companion-token")
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", factory)
    # Compare native auth selection, not a reimplementation of its precedence.
    monkeypatch.setattr(litellm, "api_key", None)
    await litellm.acompletion(
        model=model, messages=[{"role": "user", "content": "usr"}], api_base=endpoint, api_key=key,
        **({} if transport.startswith("raw") or transport == "responses_alias" else {"api_version": version}),
    )
    if key is None:
        assert state.sent[-1].headers["Authorization"] == "Bearer companion-token"
        factory.assert_called()
    cache = LLMClientCache()
    native_companion_auth.append(cache)
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", cache)
    monkeypatch.setattr(litellm, "api_key", "foreign-global-key" if guard_key else None)
    handler = LiteLLMAIHandler()
    await state.invoke(handler, entrypoint, model=model, guard_key=guard_key)
    assert len(state.sent) == 2
    for name in ("Authorization", "api-key"):
        assert state.sent[1].headers.get_list(name) == state.sent[0].headers.get_list(name)


@pytest.mark.parametrize("transport", ("sdk", "responses", "raw"))
@pytest.mark.parametrize("failure", (False, True))
@pytest.mark.asyncio
async def test_tokenless_azure_nested_contexts(monkeypatch, native_azure_oidc, transport, failure):
    monkeypatch.delenv("AZURE_AD_TOKEN")
    handlers = []
    for secret in ("first-secret", "second-secret"):
        monkeypatch.setenv("AZURE_CLIENT_SECRET", secret)
        handlers.append((LiteLLMAIHandler(), secret))
    monkeypatch.delenv("AZURE_CLIENT_SECRET")
    inner = LiteLLMAIHandler()
    model = {"sdk": "azure/gpt-4o", "responses": "azure/responses/gpt-4o", "raw": "azure_ai/test-model"}[transport]
    inner_model = "azure/responses/gpt-4o" if transport == "sdk" else "azure/gpt-4o"
    context_var = (litellm_handler._azure_oidc_request if transport == "sdk"
                   else litellm_handler._azure_ad_responses_request)
    barrier = asyncio.Event()
    arrived = 0

    async def run(handler, secret):
        async def nested(**kwargs):
            assert litellm_handler._azure_oidc_request.get() is None
            assert litellm_handler._azure_ad_responses_request.get() is None

        async def completion(**kwargs):
            nonlocal arrived
            context = context_var.get()
            assert context["dispatch_token"] is None
            assert context["auth_environment"]["AZURE_CLIENT_SECRET"] == secret
            arrived += 1
            if arrived == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=5)
            await inner._acompletion(_completion=nested, model=inner_model)
            assert context_var.get() is context
            if failure:
                raise RuntimeError("synthetic companion failure")

        try:
            await handler._acompletion(
                _completion=completion, model=model, api_key=DUMMY_LITELLM_API_KEY,
                _azure_ad_guard=True, _raw_api_key_guard=True,
            )
        except RuntimeError as error:
            assert failure and str(error) == "synthetic companion failure"
        else:
            assert not failure
        assert litellm_handler._azure_oidc_request.get() is None
        assert litellm_handler._azure_ad_responses_request.get() is None

    await asyncio.gather(*(run(handler, secret) for handler, secret in handlers))


@pytest.mark.parametrize("host", ("https://owned.example", "https://owned.services.ai.azure.com"))
@pytest.mark.parametrize("headers", ({}, {"api-key": "gateway-key"}, {"Authorization": "Basic explicit"}))
@pytest.mark.asyncio
async def test_native_raw_azure_companion_snapshot(monkeypatch, native_azure_oidc, native_companion_auth, host, headers):
    import azure.identity

    state = native_azure_oidc
    monkeypatch.delenv("AZURE_AD_TOKEN")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "owned-secret")
    monkeypatch.setenv("AZURE_AI_API_BASE", host)
    settings = _make_settings({"LITELLM.EXTRA_HEADERS": json.dumps(headers)})
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", False)
    handler = LiteLLMAIHandler()
    for name in ("AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_AI_API_KEY"):
        monkeypatch.setenv(name, "foreign-value")
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://foreign.example")
    monkeypatch.setenv("AZURE_AD_TOKEN", "oidc/env/FOREIGN_ASSERTION")
    constructor = MagicMock(wraps=azure.identity.ClientSecretCredential)
    monkeypatch.setattr(azure.identity, "ClientSecretCredential", constructor)
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", lambda *args: lambda: "owned-companion-token")
    state.expected_api_key = headers.get("api-key")
    hostname = urlparse(host).hostname or ""
    suffix = "/models/chat/completions" if hostname.endswith(".services.ai.azure.com") else "/chat/completions"
    state.expected_url = host + suffix
    expected = None if "api-key" in headers else "Bearer owned-companion-token"
    assert await state.invoke(handler, model="azure_ai/test-model") == expected
    if "api-key" in headers:
        constructor.assert_not_called()
    else:
        assert constructor.call_args.args == ("owned-tenant", "owned-client", "owned-secret")
        assert constructor.call_args.kwargs["authority"] == "https://owned-authority.example"
    assert state.exchanged == []
    assert handler._request_headers == headers


@pytest.mark.parametrize("source", ("azure_key", "AZURE_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_AD_TOKEN"))
@pytest.mark.asyncio
async def test_native_raw_azure_companion_preserves_initial_auth(
    monkeypatch, native_azure_oidc, native_companion_auth, source,
):
    import azure.identity

    state = native_azure_oidc
    monkeypatch.delenv("AZURE_AD_TOKEN")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "owned-secret")
    monkeypatch.setenv("AZURE_AI_API_BASE", "https://owned.example")
    monkeypatch.setattr(litellm_handler, "get_settings", _make_settings)
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", False)
    if source == "azure_key":
        monkeypatch.setattr(litellm, source, "initial-key")
    else:
        monkeypatch.setenv(source, "initial-token" if source == "AZURE_AD_TOKEN" else "initial-key")
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", lambda *args: lambda: "companion-token")
    state.expected_url = "https://owned.example/chat/completions"
    state.expected_api_key = None if source == "AZURE_AD_TOKEN" else "initial-key"
    handler = LiteLLMAIHandler()
    assert not handler._uses_captured_azure_companion_auth("azure_ai")
    assert await state.invoke(handler, model="azure_ai/test-model", guard_key=False) == (
        "Bearer companion-token" if source == "AZURE_AD_TOKEN" else None
    )


@pytest.mark.parametrize("field", ("AZURE_CLIENT_SECRET", "AZURE_AUTHORITY_HOST", "AZURE_SCOPE"))
@pytest.mark.parametrize("token", (None, "owned-ad-token"))
@pytest.mark.parametrize("guard_key", (False, True))
@pytest.mark.parametrize("refresh", (False, True))
@pytest.mark.asyncio
async def test_native_azure_ad_sdk_cache_and_refresh(monkeypatch, native_azure_oidc, field, token, guard_key, refresh):
    import azure.identity

    state = native_azure_oidc
    if token is None:
        monkeypatch.delenv("AZURE_AD_TOKEN")
    else:
        monkeypatch.setenv("AZURE_AD_TOKEN", token)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "owned-secret")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", refresh)
    settings = _make_settings({
        "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": "https://owned.openai.azure.com", "OPENAI.API_VERSION": "v1",
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    state.expected_url = "https://owned.openai.azure.com/openai/v1/chat/completions"
    identities, calls = [], []

    def credential(tenant, client, secret, *, authority):
        assert (tenant, client) == ("owned-tenant", "owned-client")
        return SimpleNamespace(secret=secret, authority=authority)

    def provider(credential, scope):
        index = len(identities)
        identities.append((credential.secret, credential.authority, scope))
        calls.append(0)

        def token():
            calls[index] += 1
            return f"identity-{index}-token-{calls[index]}"

        return token

    monkeypatch.setattr(azure.identity, "ClientSecretCredential", credential)
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", provider)
    first = LiteLLMAIHandler()
    assert (await state.invoke(first, guard_key=guard_key)).startswith("Bearer identity-0-token-")
    first_identity = identities[0]
    changed = "second-secret" if field == "AZURE_CLIENT_SECRET" else "https://second.example"
    monkeypatch.setenv(field, changed)
    second = LiteLLMAIHandler()
    for variable in ("AZURE_CLIENT_SECRET", "AZURE_AUTHORITY_HOST", "AZURE_SCOPE"):
        monkeypatch.setenv(variable, "foreign-value")
    assert (await state.invoke(second, guard_key=guard_key)).startswith("Bearer identity-1-token-")
    assert (await state.invoke(first, guard_key=guard_key)).startswith("Bearer identity-0-token-")
    expected_identity = list(first_identity)
    expected_identity[("AZURE_CLIENT_SECRET", "AZURE_AUTHORITY_HOST", "AZURE_SCOPE").index(field)] = changed
    assert identities == [first_identity, tuple(expected_identity)]
    before = list(calls)
    await asyncio.gather(state.invoke(first, guard_key=guard_key), state.invoke(second, guard_key=guard_key))
    assert {request.headers["Authorization"].split("-token-")[0] for request in state.sent[-2:]} == {
        "Bearer identity-0", "Bearer identity-1",
    }
    assert all(after > previous for after, previous in zip(calls, before, strict=True))
    clients = [value for value in litellm.in_memory_llm_clients_cache.cache_dict.values()
               if isinstance(value, openai.AsyncOpenAI)]
    assert len(clients) == 2
    assert state.exchanged == []


@pytest.mark.asyncio
async def test_ordinary_azure_sdk_context_does_not_select_oidc(monkeypatch, native_azure_oidc):
    from litellm.llms.azure import common_utils as azure_common

    litellm_handler._install_azure_oidc_bridge()
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", False)
    monkeypatch.setattr(azure_common, "get_secret_str", lambda name: "outside-token" if name == "AZURE_AD_TOKEN" else None)
    context = litellm_handler._azure_oidc_request.set({
        "selector": None, "dispatch_token": "owned-token", "generated_guard": True,
    })
    try:
        assert azure_common.get_azure_ad_token({}) == "outside-token"
        params = azure_common.GenericLiteLLMParams(api_key=DUMMY_LITELLM_API_KEY, azure_ad_token="owned-token")
        assert azure_common.BaseAzureLLM._base_validate_azure_environment({}, params) == {
            "api-key": DUMMY_LITELLM_API_KEY,
        }
    finally:
        litellm_handler._azure_oidc_request.reset(context)


@pytest.mark.parametrize("concurrent", (False, True))
@pytest.mark.asyncio
async def test_native_azure_ad_responses_context(monkeypatch, native_azure_oidc, concurrent):
    from litellm.llms.azure import common_utils as azure_common

    state = native_azure_oidc
    state.response_kind = "responses"
    state.expected_url = "https://owned.openai.azure.com/openai/responses?api-version=2024-06-01"
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", False)
    handlers = []
    for token in ("first-token", "second-token"):
        monkeypatch.setenv("AZURE_AD_TOKEN", token)
        handlers.append(LiteLLMAIHandler())
    monkeypatch.setattr(litellm, "api_key", "foreign-key")
    barrier = asyncio.Event()
    arrived = 0
    native_send = httpx.AsyncClient.send

    async def send(client, request, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            barrier.set()
        if concurrent:
            await barrier.wait()
        return await native_send(client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    if concurrent:
        await asyncio.gather(*(handler.probe_completion("azure/responses/gpt-4o") for handler in handlers))
    else:
        for handler in handlers:
            await handler.probe_completion("azure/responses/gpt-4o")
    assert sorted(request.headers["authorization"] for request in state.sent) == [
        "Bearer first-token", "Bearer second-token",
    ]
    assert litellm_handler._azure_ad_responses_request.get() is None
    params = azure_common.GenericLiteLLMParams(azure_ad_token="outside-token")
    assert azure_common.BaseAzureLLM._base_validate_azure_environment({}, params) == {"api-key": "foreign-key"}


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_key", (False, True))
async def test_native_azure_ad_responses_rejects_implicit_discovery(monkeypatch, native_azure_oidc, guard_key):
    from litellm.llms.azure import common_utils as azure_common

    state = native_azure_oidc
    monkeypatch.setenv("AZURE_AD_TOKEN", "owned-ad-token")
    handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", True)
    provider = MagicMock(side_effect=AssertionError("Implicit discovery must not run"))
    monkeypatch.setattr(azure_common, "get_azure_ad_token_provider", provider)
    with pytest.raises(litellm.APIConnectionError, match="implicit Azure AD credential discovery"):
        await state.invoke(handler, model="azure/responses/gpt-4o", guard_key=guard_key)
    provider.assert_not_called()
    assert state.sent == []
    assert litellm_handler._azure_ad_responses_request.get() is None


@pytest.mark.parametrize("guard_key", (False, True))
@pytest.mark.asyncio
async def test_native_azure_ad_responses_propagates_companion_failure(monkeypatch, native_azure_oidc, guard_key):
    import azure.identity

    state = native_azure_oidc
    monkeypatch.setenv("AZURE_AD_TOKEN", "owned-ad-token")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "owned-secret")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", True)
    handler = LiteLLMAIHandler()
    provider = MagicMock(side_effect=RuntimeError("synthetic captured provider failure"))
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", lambda *args: provider)
    with pytest.raises(litellm.APIConnectionError, match="synthetic captured provider failure"):
        await state.invoke(handler, model="azure/responses/gpt-4o", guard_key=guard_key)
    provider.assert_called_once()
    assert state.sent == state.exchanged == []
    assert litellm_handler._azure_ad_responses_request.get() is None


@pytest.mark.parametrize("transport", ("chat", "v1", "responses"))
@pytest.mark.parametrize("source", ("client_secret", "password"))
@pytest.mark.parametrize("timing", ("late", "initial"))
@pytest.mark.parametrize("guard_key", (False, True))
@pytest.mark.asyncio
async def test_native_azure_oidc_auth_sources(monkeypatch, native_azure_oidc, transport, source, timing, guard_key):
    import azure.identity

    state = native_azure_oidc
    if transport == "v1":
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({
            "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": "https://owned.openai.azure.com",
            "OPENAI.API_VERSION": "v1",
        }))
        state.expected_url = "https://owned.openai.azure.com/openai/v1/chat/completions"
    if timing == "initial":
        if source == "client_secret":
            monkeypatch.setenv("AZURE_CLIENT_SECRET", "owned-secret")
        else:
            monkeypatch.setenv("AZURE_USERNAME", "owned-user")
            monkeypatch.setenv("AZURE_PASSWORD", "owned-password")
    handler = LiteLLMAIHandler()
    if source == "client_secret":
        monkeypatch.setenv("AZURE_CLIENT_SECRET", "foreign-secret")
        credential_name = "ClientSecretCredential"
    else:
        monkeypatch.setenv("AZURE_USERNAME", "foreign-user")
        monkeypatch.setenv("AZURE_PASSWORD", "foreign-password")
        credential_name = "UsernamePasswordCredential"
    constructor = MagicMock(wraps=getattr(azure.identity, credential_name))
    monkeypatch.setattr(azure.identity, credential_name, constructor)
    provider = MagicMock(return_value=lambda: "provider-token")
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", provider)
    model = "azure/gpt-4o"
    if transport == "responses":
        state.response_kind = "responses"
        state.expected_url = "https://owned.openai.azure.com/openai/responses?api-version=2024-06-01"
        model = "azure/responses/gpt-4o"
    expected = "provider-token" if timing == "initial" and transport in {"v1", "responses"} else "exchanged-token-1"
    assert await state.invoke(handler, guard_key=guard_key, model=model) == f"Bearer {expected}"
    if timing == "late":
        provider.assert_not_called()
        constructor.assert_not_called()
    else:
        provider.assert_called_once()
        constructor.assert_called_once()
        if source == "client_secret":
            assert constructor.call_args.args == ("owned-tenant", "owned-client", "owned-secret")
        else:
            assert constructor.call_args.kwargs["username"] == "owned-user"
            assert constructor.call_args.kwargs["password"] == "owned-password"


@pytest.mark.parametrize("transport", ("v1", "responses"))
@pytest.mark.parametrize("source", ("client_secret", "password"))
@pytest.mark.parametrize("warm", (False, True))
@pytest.mark.asyncio
async def test_native_azure_oidc_companion_authority(monkeypatch, native_azure_oidc, transport, source, warm):
    from azure.core.credentials import AccessTokenInfo
    from azure.identity import ClientSecretCredential, UsernamePasswordCredential
    from litellm.llms.azure import common_utils as azure_common

    state = native_azure_oidc
    client_id = f"owned-client-{transport}-{source}-{warm}"
    monkeypatch.setenv("AZURE_CLIENT_ID", client_id)
    if source == "client_secret":
        monkeypatch.setenv("AZURE_CLIENT_SECRET", "owned-secret")
    else:
        monkeypatch.setenv("AZURE_USERNAME", "owned-user")
        monkeypatch.setenv("AZURE_PASSWORD", "owned-password")
    selected_authorities = []

    def acquire_token(credential, *scopes, **kwargs):
        # Keep native factories, credential construction and bearer-token policy;
        # intercept acquisition only after Azure Identity has selected authority.
        selected_authorities.append(credential._authority)
        # Force policy refresh so a reused provider must retain its authority.
        return AccessTokenInfo(f"provider-{credential._authority.split('//')[-1]}", 0)

    monkeypatch.setattr(ClientSecretCredential, "get_token_info", acquire_token)
    monkeypatch.setattr(UsernamePasswordCredential, "get_token_info", acquire_token)
    if warm and source == "client_secret":
        monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://foreign-authority.example")
        # Seed the real native provider cache outside the request bridge.
        outside_provider = azure_common.get_azure_ad_token_from_entra_id(
            "owned-tenant", client_id, "owned-secret", state.inputs["AZURE_SCOPE"],
        )
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", state.inputs["AZURE_AUTHORITY_HOST"])
    model = "azure/gpt-4o"
    if transport == "v1":
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({
            "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": "https://owned.openai.azure.com",
            "OPENAI.API_VERSION": "v1",
        }))
        state.expected_url = "https://owned.openai.azure.com/openai/v1/chat/completions"
    else:
        state.response_kind = "responses"
        state.expected_url = "https://owned.openai.azure.com/openai/responses?api-version=2024-06-01"
        model = "azure/responses/gpt-4o"
    first = LiteLLMAIHandler()
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://foreign-authority.example")
    assert await state.invoke(first, model=model) == "Bearer provider-owned-authority.example"
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://second-authority.example")
    second = LiteLLMAIHandler()
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://foreign-authority.example")
    assert await state.invoke(second, model=model) == "Bearer provider-second-authority.example"
    assert await state.invoke(first, model=model) == "Bearer provider-owned-authority.example"
    assert set(selected_authorities) == {"https://owned-authority.example", "https://second-authority.example"}
    if warm and source == "client_secret":
        assert azure_common.get_azure_ad_token_from_entra_id(
            "owned-tenant", client_id, "owned-secret", state.inputs["AZURE_SCOPE"],
        ) is outside_provider


@pytest.mark.parametrize("source", ("client_secret", "password"))
@pytest.mark.parametrize("captured", (None, ""))
@pytest.mark.asyncio
async def test_native_azure_oidc_companion_authority_default(monkeypatch, native_azure_oidc, source, captured):
    from azure.core.credentials import AccessTokenInfo
    from azure.identity import ClientSecretCredential, UsernamePasswordCredential

    state = native_azure_oidc
    if captured is None:
        monkeypatch.delenv("AZURE_AUTHORITY_HOST")
    else:
        monkeypatch.setenv("AZURE_AUTHORITY_HOST", captured)
    if source == "client_secret":
        monkeypatch.setenv("AZURE_CLIENT_SECRET", "owned-secret")
    else:
        monkeypatch.setenv("AZURE_USERNAME", "owned-user")
        monkeypatch.setenv("AZURE_PASSWORD", "owned-password")
    authorities = []

    def acquire_token(credential, *scopes, **kwargs):
        authorities.append(credential._authority)
        return AccessTokenInfo("public-cloud-provider", 4102444800)

    monkeypatch.setattr(ClientSecretCredential, "get_token_info", acquire_token)
    monkeypatch.setattr(UsernamePasswordCredential, "get_token_info", acquire_token)
    state.response_kind = "responses"
    state.expected_url = "https://owned.openai.azure.com/openai/responses?api-version=2024-06-01"
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://foreign-authority.example")
    if captured == "":
        with pytest.raises(litellm.APIConnectionError, match="nonempty captured authority"):
            await state.invoke(handler, model="azure/responses/gpt-4o")
        assert authorities == state.sent == state.exchanged == []
    else:
        assert await state.invoke(handler, model="azure/responses/gpt-4o") == "Bearer public-cloud-provider"
        assert authorities == ["https://login.microsoftonline.com"]


@pytest.mark.parametrize("identity", ("same", "authority", "scope"))
@pytest.mark.parametrize("version", ("2024-06-01", "v1"))
@pytest.mark.asyncio
async def test_native_azure_oidc_bridge_sdk_cache(monkeypatch, native_azure_oidc, identity, version):
    from litellm.llms.azure import common_utils as azure_common

    state = native_azure_oidc
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", True)
    provider = MagicMock(side_effect=AssertionError("OIDC must not create an additional credential provider"))
    monkeypatch.setattr(azure_common, "get_azure_ad_token_provider", provider)
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({
        "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": "https://owned.openai.azure.com",
        "OPENAI.API_VERSION": version,
    }))
    if version == "v1":
        state.expected_url = "https://owned.openai.azure.com/openai/v1/chat/completions"
    first = LiteLLMAIHandler()
    assert await state.invoke(first, guard_key=False) == "Bearer exchanged-token-1"
    client_types = (openai.AsyncAzureOpenAI, openai.AsyncOpenAI)
    clients = [value for value in litellm.in_memory_llm_clients_cache.cache_dict.values()
               if isinstance(value, client_types)]
    assert len(clients) == 1
    if identity == "same":
        monkeypatch.delenv("PR_AGENT_TEST_OIDC_ASSERTION")
    else:
        field = "AZURE_AUTHORITY_HOST" if identity == "authority" else "AZURE_SCOPE"
        monkeypatch.setenv(field, "https://second.example")
    second = LiteLLMAIHandler()
    expected_count = 1 if identity == "same" else 2
    assert await state.invoke(second, guard_key=False) == f"Bearer exchanged-token-{expected_count}"
    assert await state.invoke(first, guard_key=False) == "Bearer exchanged-token-1"
    cached = [value for value in litellm.in_memory_llm_clients_cache.cache_dict.values()
              if isinstance(value, client_types)]
    assert len(cached) == expected_count
    assert any(value is clients[0] for value in cached)
    assert len(state.exchanged) == expected_count
    if identity == "authority":
        assert state.exchanged[1][0] == "https://second.example/owned-tenant/oauth2/v2.0/token"
    elif identity == "scope":
        assert state.exchanged[1][1]["scope"] == "https://second.example"
    provider.assert_not_called()


@pytest.mark.parametrize("stream", (False, True))
@pytest.mark.asyncio
async def test_native_azure_oidc_bridge_text(monkeypatch, native_azure_oidc, stream):
    state = native_azure_oidc
    state.response_kind, state.stream = "text", stream
    model = "azure_text/gpt-3.5-turbo-instruct"
    state.expected_url = (
        "https://owned.openai.azure.com/openai/deployments/gpt-3.5-turbo-instruct/completions?api-version=2024-06-01"
    )
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AZURE_TENANT_ID", "foreign-tenant")
    params = handler._get_provider_request_params(model)
    try:
        response = await handler._acompletion(
            model=params.pop("model", model), messages=[{"role": "user", "content": "test"}], stream=stream, **params,
        )
    finally:
        assert litellm_handler._azure_oidc_request.get() is None
        assert len(state.exchanged) == 1
        assert state.exchanged[0][0] == "https://owned-authority.example/owned-tenant/oauth2/v2.0/token"
        assert state.exchanged[0][1]["client_id"] == "owned-client"
    if stream:
        monkeypatch.setenv("AZURE_CLIENT_ID", "foreign-after-return")
        assert [chunk async for chunk in response]
    assert len(state.sent) == len(state.exchanged) == 1
    assert state.sent[0].headers.get_list("authorization") == ["Bearer exchanged-token-1"]
    assert litellm_handler._azure_oidc_request.get() is None


@pytest.mark.parametrize(("transport", "key_source"), (
    ("cloudflare", "setting"), ("cloudflare", "header"), ("responses", "setting"),
))
@pytest.mark.parametrize("api_key", ("owned-key", "dummy_key"))
@pytest.mark.asyncio
async def test_native_azure_oidc_bridge_api_key_winner(monkeypatch, native_azure_oidc, transport, key_source, api_key):
    state = native_azure_oidc
    endpoint = "https://owned.openai.azure.com"
    model = "azure/responses/gpt-4o"
    if transport == "cloudflare":
        endpoint = "https://gateway.ai.cloudflare.com/v1/account/gateway/azure-openai/resource"
        model = "azure/gpt-4o"
        state.expected_url = f"{endpoint}/gpt-4o/chat/completions?api-version=2024-06-01"
    else:
        state.response_kind = "responses"
        state.expected_url = f"{endpoint}/openai/responses?api-version=2024-06-01"
    settings = {
        "OPENAI.API_TYPE": "azure", "OPENAI.API_BASE": endpoint, "OPENAI.API_VERSION": "2024-06-01",
        "OPENAI.KEY": api_key if key_source == "setting" else None,
        "LITELLM.EXTRA_HEADERS": json.dumps({"api-key": api_key}) if key_source == "header" else None,
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(settings))
    handler = LiteLLMAIHandler()
    monkeypatch.delenv("PR_AGENT_TEST_OIDC_ASSERTION")
    state.expected_api_key = api_key
    assert await state.invoke(handler, guard_key=key_source == "header" and transport == "cloudflare",
                              model=model) is None
    assert len(state.sent) == 1
    assert state.sent[0].headers.get_list("authorization") == []
    assert state.exchanged == []


@pytest.mark.parametrize("action", ("outside", "alias", "error"))
@pytest.mark.asyncio
async def test_native_azure_oidc_bridge_context(monkeypatch, native_azure_oidc, action):
    from litellm.llms.azure import azure as azure_module
    from litellm.llms.azure import common_utils as azure_common

    state = native_azure_oidc
    handler = LiteLLMAIHandler()
    assert await state.invoke(handler, guard_key=False) == "Bearer exchanged-token-1"
    assert litellm_handler._azure_oidc_request.get() is None
    if action == "outside":
        monkeypatch.setenv("AZURE_TENANT_ID", "outside-tenant")
        state.ambient = dict(os.environ)
        token = await asyncio.to_thread(
            azure_common.get_azure_ad_token_from_oidc, "oidc/env/PR_AGENT_TEST_OIDC_ASSERTION",
            "outside-client", None, "outside-scope",
        )
        assert token == "exchanged-token-2"
        assert state.exchanged[1][0] == "https://owned-authority.example/outside-tenant/oauth2/v2.0/token"
        assert state.exchanged[1][1]["client_id"] == "outside-client"
        assert state.exchanged[1][1]["scope"] == "outside-scope"
    elif action == "alias":
        monkeypatch.setattr(azure_module, "get_azure_ad_token_from_oidc", lambda *args, **kwargs: "unsafe")
        with pytest.raises(RuntimeError, match="(?i)replaced|incompatible"):
            await state.invoke(handler, guard_key=False)
        assert len(state.exchanged) == 1
    else:
        monkeypatch.setenv("AZURE_SCOPE", "second-scope")
        second = LiteLLMAIHandler()
        state.status = 401
        with pytest.raises(litellm.AuthenticationError):
            await state.invoke(second, guard_key=False)
        assert len(state.exchanged) == 2
    assert len(state.sent) == 1
    assert litellm_handler._azure_oidc_request.get() is None


async def _call(handler, model):
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        await handler.chat_completion(model=model, system="sys", user="usr")
    return mock_call.call_args.kwargs


@pytest.mark.parametrize(("module_name", "symbol"), (
    ("litellm.llms.anthropic.common_utils", "AnthropicModelInfo"),
    ("litellm.llms.openai_like.json_loader", "JSONProviderRegistry"),
    ("litellm.utils", "_get_model_info_helper"),
))
@pytest.mark.asyncio
async def test_missing_private_import_preserves_module_and_fails_closed(monkeypatch, module_name, symbol):
    import builtins
    import importlib.util

    spec = importlib.util.spec_from_file_location("_isolated_litellm_handler", litellm_handler.__file__)
    module = importlib.util.module_from_spec(spec)
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == module_name and symbol in fromlist and (globals or {}).get("__name__") == spec.name:
            raise ImportError("Unavailable private interface")
        return original_import(name, globals, locals, fromlist, level)

    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", guarded_import)
        spec.loader.exec_module(module)
    assert getattr(module, symbol) is None
    monkeypatch.setattr(module, "get_settings", lambda: _make_settings())
    if symbol == "JSONProviderRegistry":
        with pytest.raises(RuntimeError, match="JSONProviderRegistry.*request isolation"):
            module.LiteLLMAIHandler()
        return
    handler = module.LiteLLMAIHandler()
    completion = AsyncMock(return_value=_mock_response())
    monkeypatch.setattr(module, "acompletion", completion)
    if symbol == "AnthropicModelInfo":
        with pytest.raises(RuntimeError, match="AnthropicModelInfo.*request isolation"):
            await handler.probe_completion("anthropic/claude-sonnet-4")
        completion.assert_not_called()
        await handler.probe_completion("gpt-4o")
    else:
        with pytest.raises(RuntimeError, match="_get_model_info_helper.*request isolation"):
            await handler.probe_completion("gpt-4o")
        completion.assert_not_called()
        assert module._uses_openai_responses_transport("openai/responses/gpt-4o", "openai")
        assert not module._uses_openai_responses_transport("ft:babbage-002:example", "openai")
        await handler.probe_completion("anthropic/claude-sonnet-4")


@pytest.mark.parametrize("method", ("list_providers", "get", "exists"))
def test_incomplete_registry_fails_before_snapshot(monkeypatch, method):
    registry = type("Registry", (), {
        name: staticmethod(lambda *args: ()) for name in ("list_providers", "get", "exists")
    })
    setattr(registry, method, None)
    with monkeypatch.context() as scoped:
        scoped.setattr(litellm_handler, "JSONProviderRegistry", registry)
        settings = MagicMock(side_effect=AssertionError("Do not start constructing snapshots"))
        scoped.setattr(litellm_handler, "get_settings", settings)
        with pytest.raises(RuntimeError, match="JSONProviderRegistry"):
            LiteLLMAIHandler()
        settings.assert_not_called()


@pytest.mark.parametrize("method", ("get_api_key", "get_auth_token"))
def test_incomplete_anthropic_interface_does_not_install_partial_bridge(monkeypatch, method):
    info = type("ModelInfo", (), {
        name: staticmethod(lambda value=None: value) for name in ("get_api_key", "get_auth_token")
    })
    setattr(info, method, None)
    originals = (info.get_api_key, info.get_auth_token)
    with monkeypatch.context() as scoped:
        scoped.setattr(litellm_handler, "AnthropicModelInfo", info)
        with pytest.raises(RuntimeError, match="AnthropicModelInfo"):
            litellm_handler._install_anthropic_auth_token_bridge()
        assert (info.get_api_key, info.get_auth_token) == originals


def test_noncallable_model_info_helper_does_not_silently_choose_transport(monkeypatch):
    monkeypatch.setattr(litellm_handler, "_get_model_info_helper", object())
    with pytest.raises(RuntimeError, match="_get_model_info_helper"):
        litellm_handler._uses_openai_responses_transport("gpt-4o", "openai")


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


@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("extra_headers", (None, {"X-Request": "handler-value"}))
@pytest.mark.asyncio
async def test_together_transport_preserves_request_headers(monkeypatch, entrypoint, extra_headers):
    from litellm.litellm_core_utils import logging_worker

    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    settings = _make_settings({"LITELLM.EXTRA_HEADERS": json.dumps(extra_headers) if extra_headers else None})
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    monkeypatch.setenv("TOGETHERAI_API_KEY", "handler-key")
    monkeypatch.setenv("TOGETHER_AI_API_BASE", "https://together-handler.example/v1")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("TOGETHERAI_API_KEY", "another-handler-key")
    monkeypatch.setenv("TOGETHER_AI_API_BASE", "https://another-handler.example/v1")
    captured = []

    async def send(client, request, **kwargs):
        captured.append(request)
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    # Exercise native header assembly: SDK Omit values must never reach httpx.
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    model = "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo"
    try:
        if entrypoint == "chat":
            assert await handler.chat_completion(model, "sys", "usr") == ("ok", "stop")
        else:
            await handler.probe_completion(model)
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://together-handler.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer handler-key"
    assert "openai-organization" not in request.headers
    assert "openai-project" not in request.headers
    assert request.headers.get("x-request") == ("handler-value" if extra_headers else None)


@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("extra_headers", (None, {"X-Request": "handler-value"}, {
    "authorization": "Bearer explicit-handler-token", "editor-version": "request-editor",
}))
@pytest.mark.asyncio
async def test_copilot_transport_preserves_native_headers(monkeypatch, tmp_path, entrypoint, extra_headers):
    from litellm.litellm_core_utils import logging_worker
    from litellm.llms.github_copilot.authenticator import Authenticator
    from litellm.llms.github_copilot.common_utils import EDITOR_PLUGIN_VERSION

    monkeypatch.setenv("GITHUB_COPILOT_TOKEN_DIR", str(tmp_path / "copilot"))
    monkeypatch.setenv("GITHUB_COPILOT_API_BASE", "https://copilot-handler.example/v1")
    monkeypatch.setattr(Authenticator, "get_api_key", lambda self: "handler-copilot-token")
    monkeypatch.setattr(Authenticator, "get_api_base", lambda self: "https://copilot-handler.example/v1")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    settings = _make_settings({"LITELLM.EXTRA_HEADERS": json.dumps(extra_headers) if extra_headers else None})
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    handler = LiteLLMAIHandler()
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    captured = []

    async def send(client, request, **kwargs):
        captured.append(request)
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    # Keep native Copilot defaults and SDK assembly; stub token acquisition and HTTP only.
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    try:
        if entrypoint == "chat":
            assert await handler.chat_completion("github_copilot/gpt-4o", "sys", "usr") == ("ok", "stop")
        else:
            await handler.probe_completion("github_copilot/gpt-4o")
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://copilot-handler.example/v1/chat/completions"
    explicit = extra_headers or {}
    assert request.headers.get_list("authorization") == [
        explicit.get("authorization", "Bearer handler-copilot-token")
    ]
    assert request.headers["copilot-integration-id"] == "vscode-chat"
    assert request.headers["editor-version"] == explicit.get("editor-version", "vscode/1.95.0")
    assert request.headers["editor-plugin-version"] == EDITOR_PLUGIN_VERSION
    assert request.headers["openai-intent"] == "conversation-panel"
    assert request.headers.get("x-request") == explicit.get("X-Request")


@pytest.mark.parametrize("provider", ("openai", "vercel_ai_gateway"))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize(
    "scenario", (
        "initial_accounts", "late_accounts", "initial_headers", "late_headers", "warm", "warm_accounts", "retry",
        "custom_accounts", "custom_accounts_over_env", "explicit_headers",
    ),
)
@pytest.mark.asyncio
async def test_native_sdk_header_snapshot(monkeypatch, provider, entrypoint, scenario):
    from litellm.litellm_core_utils import logging_worker
    from litellm.llms.openai.openai import OpenAIChatCompletion

    endpoint = f"https://{provider.replace('_', '-')}-{entrypoint}-{scenario.replace('_', '-')}.example/v1"
    overrides = {"OPENAI.KEY": "handler-key", "OPENAI.API_BASE": endpoint} if provider == "openai" else {}
    if provider == "vercel_ai_gateway":
        monkeypatch.setenv("VERCEL_AI_GATEWAY_API_KEY", "handler-key")
        monkeypatch.setenv("VERCEL_AI_GATEWAY_API_BASE", endpoint)
    if scenario == "explicit_headers":
        overrides["LITELLM.EXTRA_HEADERS"] = json.dumps({
            "authorization": "Bearer explicit-key", "openai-organization": "explicit-org",
            "openai-project": "explicit-project", "X-Owned": "explicit-value",
        })
        monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer native-key\nX-Owned: native-value")
        monkeypatch.setenv("OPENAI_ORG_ID", "native-org")
        monkeypatch.setenv("OPENAI_PROJECT_ID", "native-project")
    settings = _make_settings(overrides)
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    if scenario in ("initial_accounts", "custom_accounts_over_env"):
        monkeypatch.setenv("OPENAI_ORG_ID", "handler-org")
        monkeypatch.setenv("OPENAI_PROJECT_ID", "handler-project")
    if scenario == "initial_headers":
        monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "authorization: Bearer handler-header\nX-Owned: value")
    if scenario in ("custom_accounts", "custom_accounts_over_env"):
        monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "openai-organization: custom-org\nOpenAI-Project: custom-project")
    handler = LiteLLMAIHandler()
    if scenario in ("late_headers", "warm", "retry"):
        monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer another-handler\nX-Foreign: value")
    if scenario in ("late_accounts", "warm_accounts"):
        monkeypatch.setenv("OPENAI_ORG_ID", "another-org")
        monkeypatch.setenv("OPENAI_PROJECT_ID", "another-project")
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    captured, clients = [], []
    native_factory = OpenAIChatCompletion._get_openai_client

    def observe_client(self, *args, **kwargs):
        client = native_factory(self, *args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(OpenAIChatCompletion, "_get_openai_client", observe_client)

    async def send(client, request, **kwargs):
        captured.append(request)
        if scenario == "retry" and len(captured) == 1:
            return httpx.Response(500, request=request, json={"error": {"message": "retry", "type": "server_error"}})
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    try:
        for _ in range(2 if scenario in ("warm", "warm_accounts") else 1):
            if entrypoint == "chat":
                await handler.chat_completion(f"{provider}/gpt-4o", "sys", "usr")
            else:
                await handler.probe_completion(f"{provider}/gpt-4o")
            if scenario == "warm":
                monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
            if scenario == "warm_accounts":
                monkeypatch.delenv("OPENAI_ORG_ID", raising=False)
                monkeypatch.delenv("OPENAI_PROJECT_ID", raising=False)
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()

    assert len(captured) == (2 if scenario in ("warm", "warm_accounts", "retry") else 1)
    for request in captured:
        assert str(request.url) == f"{endpoint}/chat/completions"
        assert request.headers.get_list("authorization") == [
            "Bearer explicit-key" if scenario == "explicit_headers"
            else "Bearer handler-header" if scenario == "initial_headers" else "Bearer handler-key"
        ]
        custom_accounts = scenario in ("custom_accounts", "custom_accounts_over_env")
        assert request.headers.get("openai-organization") == (
            "explicit-org" if scenario == "explicit_headers"
            else "custom-org" if custom_accounts else "handler-org" if scenario == "initial_accounts" else None
        )
        assert request.headers.get("openai-project") == (
            "explicit-project" if scenario == "explicit_headers"
            else "custom-project" if custom_accounts else "handler-project" if scenario == "initial_accounts" else None
        )
        assert request.headers.get("x-owned") == (
            "explicit-value" if scenario == "explicit_headers" else "value" if scenario == "initial_headers" else None
        )
        assert "x-foreign" not in request.headers
        assert litellm_handler._SDK_HEADER_MARKER not in request.headers
    if scenario == "warm":
        assert clients[0] is clients[1]
        # The cached client's foreign defaults stay untouched; only request headers are isolated.
        assert clients[0]._custom_headers["Authorization"] == "Bearer another-handler"
    if scenario == "warm_accounts":
        assert clients[0] is clients[1]
        assert clients[0].organization == "another-org"
        assert clients[0].project == "another-project"


@pytest.mark.parametrize("azure", (False, True))
@pytest.mark.parametrize("stream", (False, True))
@pytest.mark.parametrize("experimental", (False, True))
@pytest.mark.asyncio
async def test_native_text_sdk_headers_survive_delayed_stream(monkeypatch, azure, stream, experimental):
    from litellm.litellm_core_utils import logging_worker

    settings = _make_settings({
        "OPENAI.KEY": "handler-key", "OPENAI.API_BASE": "https://text-handler.example/v1",
        "OPENAI.API_TYPE": "azure" if azure else None, "OPENAI.API_VERSION": "2024-02-15-preview",
        "LITELLM.EXTRA_HEADERS": '{"X-Request": "handler-value"}',
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    if experimental:
        monkeypatch.setenv("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", "true")
    monkeypatch.setenv("OPENAI_ORG_ID", "handler-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "handler-project")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer foreign-token\nX-Foreign: value")
    monkeypatch.setenv("OPENAI_ORG_ID", "foreign-org")
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    captured = []

    async def send(client, request, **kwargs):
        captured.append(request)
        payload = {
            "id": "test", "object": "text_completion", "created": 0, "model": "gpt-3.5-turbo-instruct",
            "choices": [{"index": 0, "text": "ok", "logprobs": None, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        if stream:
            # No stream_options.include_usage was requested for this text stream.
            payload.pop("usage")
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  content=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode())
        return httpx.Response(200, request=request, json=payload)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    model = ("azure_text/" if azure else "text-completion-openai/") + "gpt-3.5-turbo-instruct"
    try:
        params = handler._get_provider_request_params(model)
        request_model = params.pop("model", model)
        response = await handler._acompletion(
            model=request_model, messages=[{"role": "user", "content": "test"}], stream=stream, **params,
        )
        assert litellm_handler._sdk_request_headers.get() is None
        if stream:
            monkeypatch.setenv("OPENAI_PROJECT_ID", "foreign-project-after-return")
            chunks = [chunk async for chunk in response]
            assert chunks
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()

    assert len(captured) == 1
    request = captured[0]
    assert request.url.host == "text-handler.example"
    assert request.url.path.endswith("/completions")
    assert request.headers.get("api-key" if azure else "authorization") == (
        "handler-key" if azure else "Bearer handler-key"
    )
    assert request.headers["openai-organization"] == "handler-org"
    assert request.headers["openai-project"] == "handler-project"
    assert request.headers["x-request"] == "handler-value"
    assert "x-foreign" not in request.headers
    assert litellm_handler._SDK_HEADER_MARKER not in request.headers


@pytest.mark.asyncio
async def test_concurrent_sdk_requests_isolate_cached_default_headers(monkeypatch):
    from litellm.litellm_core_utils import logging_worker
    from litellm.llms.openai.openai import OpenAIChatCompletion

    settings = _make_settings({
        "OPENAI.KEY": "shared-handler-key", "OPENAI.API_BASE": "https://concurrent-sdk.example/v1",
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    handlers = []
    for identity in ("one", "two"):
        monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", f"authorization: Bearer {identity}\nX-Owner: {identity}")
        handlers.append(LiteLLMAIHandler())
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer foreign\nX-Owner: foreign")
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    captured, clients = [], []
    entered = asyncio.Event()
    native_factory = OpenAIChatCompletion._get_openai_client

    def observe_client(self, *args, **kwargs):
        client = native_factory(self, *args, **kwargs)
        clients.append(client)
        return client

    async def send(client, request, **kwargs):
        captured.append(request)
        if len(captured) == 2:
            entered.set()
        await asyncio.wait_for(entered.wait(), 5)
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(OpenAIChatCompletion, "_get_openai_client", observe_client)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    try:
        await asyncio.gather(*(handler.probe_completion("openai/gpt-4o") for handler in handlers))
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()
    assert len(captured) == 2
    assert {tuple(request.headers.get_list("authorization")) for request in captured} == {
        ("Bearer one",), ("Bearer two",),
    }
    for request in captured:
        assert request.headers["authorization"] == f"Bearer {request.headers['x-owner']}"
        assert litellm_handler._SDK_HEADER_MARKER not in request.headers
    assert clients[0] is clients[1]
    assert litellm_handler._sdk_request_headers.get() is None


@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("api_version", ("2024-02-15-preview", "v1"))
@pytest.mark.parametrize("ad_auth", (False, True))
@pytest.mark.asyncio
async def test_azure_deployment_selects_sdk_header_boundary(monkeypatch, entrypoint, api_version, ad_auth):
    from litellm.litellm_core_utils import logging_worker

    settings = _make_settings({
        "OPENAI.KEY": None if ad_auth else "handler-key", "OPENAI.API_TYPE": "azure",
        "OPENAI.API_BASE": "https://deployment-handler.example", "OPENAI.API_VERSION": api_version,
        "OPENAI.DEPLOYMENT_ID": "custom-deployment",
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    if ad_auth:
        monkeypatch.setenv("AZURE_AD_TOKEN", "handler-ad-token")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer foreign\nX-Foreign: value")
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    captured = []

    async def send(client, request, **kwargs):
        captured.append(request)
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "custom-deployment",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    try:
        if entrypoint == "chat":
            await handler.chat_completion("azure/gpt-5-pro", "sys", "usr")
        else:
            await handler.probe_completion("azure/gpt-5-pro")
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()
    assert len(captured) == 1
    request = captured[0]
    assert request.url.host == "deployment-handler.example"
    assert request.url.path.endswith("/chat/completions")
    if ad_auth or api_version == "v1":
        assert request.headers.get_list("authorization") == [
            "Bearer handler-ad-token" if ad_auth else "Bearer handler-key"
        ]
    else:
        assert request.headers["api-key"] == "handler-key"
        assert "authorization" not in request.headers
    assert "x-foreign" not in request.headers
    assert litellm_handler._SDK_HEADER_MARKER not in request.headers


@pytest.mark.parametrize("custom_value", ("custom-account", ""))
@pytest.mark.parametrize("override", (None, "explicit-account", "omit"))
def test_sdk_account_defaults_preserve_explicit_override_and_omit(custom_value, override):
    litellm_handler._install_sdk_header_bridge()
    captured = []
    headers = {"OpenAI-Organization": custom_value, "OpenAI-Project": custom_value}
    explicit = {} if override is None else {
        name.lower(): openai.Omit() if override == "omit" else override for name in headers
    }
    snapshot = {
        "organization": "env-org", "project": "env-project", "custom_headers": headers,
        "explicit_headers": explicit,
    }
    request_headers = {
        name: litellm_handler._CapturedSDKHeader() for name in headers
    }
    request_headers[litellm_handler._SDK_HEADER_MARKER] = litellm_handler._SDKHeaderSnapshot(snapshot)

    def send(request):
        captured.append(request)
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "gpt-4o", "choices": [],
        })

    with httpx.Client(transport=httpx.MockTransport(send)) as http_client:
        with openai.OpenAI(api_key="handler-key", http_client=http_client) as client:
            client.chat.completions.create(model="gpt-4o", messages=[], extra_headers=request_headers)
    assert len(captured) == 1
    for name in headers:
        expected = [] if override == "omit" else [custom_value if override is None else override]
        assert captured[0].headers.get_list(name) == expected
    assert litellm_handler._SDK_HEADER_MARKER not in captured[0].headers


def test_unmarked_sdk_request_keeps_native_headers_inside_handler_context(monkeypatch):
    litellm_handler._install_sdk_header_bridge()
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer callback-token")
    token = litellm_handler._sdk_request_headers.set({"custom_headers": {"Authorization": "Bearer handler-token"}})
    captured = []

    def send(request):
        captured.append(request)
        return httpx.Response(200, request=request, json={
            "id": "callback", "object": "chat.completion", "created": 0, "model": "gpt-4o", "choices": [],
        })

    try:
        with httpx.Client(transport=httpx.MockTransport(send)) as http_client:
            with openai.OpenAI(api_key="callback-key", http_client=http_client) as client:
                client.chat.completions.create(model="gpt-4o", messages=[])
    finally:
        litellm_handler._sdk_request_headers.reset(token)
    assert len(captured) == 1
    assert captured[0].headers.get_list("authorization") == ["Bearer callback-token"]


def test_sdk_header_logging_proxy_does_not_publish_or_mutate_marker():
    marker = litellm_handler._SDKHeaderSnapshot({"custom_headers": {"Authorization": "synthetic-secret"}})
    data = {"extra_headers": {litellm_handler._SDK_HEADER_MARKER: marker, "X-Request": "value"}}
    logger = MagicMock()
    proxy = litellm_handler._SDKHeaderLoggingProxy(logger)
    proxy.pre_call(input="test", additional_args={"complete_input_dict": data})
    logged = logger.pre_call.call_args.kwargs["additional_args"]["complete_input_dict"]
    assert logged == {"extra_headers": {"X-Request": "value"}}
    assert data["extra_headers"][litellm_handler._SDK_HEADER_MARKER] is marker
    proxy._deferred_stream_complete_args = ("response", False)
    assert logger._deferred_stream_complete_args == ("response", False)
    assert "synthetic-secret" not in repr(marker)
    assert "synthetic-secret" not in str(marker)


@pytest.mark.asyncio
async def test_sdk_header_context_resets_after_cancellation():
    async def cancelled(**kwargs):
        assert litellm_handler._sdk_request_headers.get() is not None
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await LiteLLMAIHandler()._acompletion(_completion=cancelled, model="openai/gpt-4o")
    assert litellm_handler._sdk_request_headers.get() is None


def test_sdk_header_marker_fails_before_http_without_bridge(monkeypatch):
    from openai._base_client import BaseClient

    litellm_handler._install_sdk_header_bridge()
    native = BaseClient._build_headers._pr_agent_sdk_headers_original
    monkeypatch.setattr(BaseClient, "_build_headers", native)
    captured = []

    def send(request):
        captured.append(request)
        raise AssertionError("An unconsumed marker must fail before HTTP")

    marker = litellm_handler._SDKHeaderSnapshot({"custom_headers": {"Authorization": "synthetic-secret"}})
    assert "synthetic-secret" not in repr(marker)
    with httpx.Client(transport=httpx.MockTransport(send)) as http_client:
        with openai.OpenAI(api_key="handler-key", http_client=http_client, max_retries=0) as client:
            with pytest.raises(TypeError, match="Header value must be str or bytes"):
                client.chat.completions.create(model="gpt-4o", messages=[], extra_headers={
                    litellm_handler._SDK_HEADER_MARKER: marker,
                })
    assert not captured


@pytest.mark.parametrize("initial_key", (None, "handler-key"))
@pytest.mark.asyncio
async def test_manus_responses_transport_uses_request_local_credentials(monkeypatch, initial_key):
    from litellm.litellm_core_utils import logging_worker

    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    monkeypatch.delenv("MANUS_API_KEY", raising=False)
    if initial_key:
        monkeypatch.setenv("MANUS_API_KEY", initial_key)
    monkeypatch.setenv("MANUS_API_BASE", "https://handler.example/v1")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("MANUS_API_KEY", "another-handler-key")
    captured = []

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        captured.append(request)
        raise TransportReached

    # Keep native completion-to-Responses dispatch and authentication intact.
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    model = "manus/responses/manus-1.6"
    try:
        with pytest.raises(TransportReached):
            await handler._acompletion(
                model=model, messages=[{"role": "user", "content": "test"}],
                **handler._get_provider_request_params(model),
            )
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()
    assert len(captured) == 1
    assert str(captured[0].url) == "https://handler.example/v1/responses"
    assert captured[0].headers["api_key"] == (initial_key or DUMMY_LITELLM_API_KEY)


@pytest.mark.parametrize("initial_key", (None, "handler-palm-key"))
@pytest.mark.asyncio
async def test_gemini_palm_transport_uses_request_local_credentials(monkeypatch, initial_key):
    from litellm.litellm_core_utils import logging_worker

    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    for variable in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "PALM_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(litellm, "api_key", None)
    if initial_key:
        monkeypatch.setenv("PALM_API_KEY", initial_key)
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("PALM_API_KEY", "another-handler-palm-key")
    captured = []

    async def send(client, request, **kwargs):
        captured.append(request)
        return httpx.Response(200, request=request, json={
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": "ok"}]},
                "finishReason": "STOP",
                "index": 0,
            }],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1, "totalTokenCount": 3},
        })

    # Keep native Gemini dispatch and authentication; intercept only HTTP.
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    try:
        result = await handler.chat_completion(model="gemini/gemini-2.5-pro", system="sys", user="usr")
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()

    assert result == ("ok", "stop")
    assert len(captured) == 1
    assert captured[0].headers["x-goog-api-key"] == (initial_key or DUMMY_LITELLM_API_KEY)


@pytest.mark.parametrize("model", ("gemini-2.5-pro", "gemini-3-flash-preview"))
@pytest.mark.parametrize("entrypoint", ("chat", "probe", "stream"))
@pytest.mark.parametrize("initial_key", (None, "owned-key"))
@pytest.mark.parametrize("initial_base", (None, "https://owned.example/v1"))
@pytest.mark.parametrize(("source", "header_key"), (
    ("environment", None), ("global", None), ("environment", "explicit-header-key"),
))
@pytest.mark.asyncio
async def test_native_gemini_executor_boundary(
    monkeypatch, model, entrypoint, initial_key, initial_base, source, header_key,
):
    from litellm.litellm_core_utils import logging_worker

    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    if initial_key:
        monkeypatch.setenv("GEMINI_API_KEY", initial_key)
    if initial_base:
        monkeypatch.setenv("GEMINI_API_BASE", initial_base)
    if header_key:
        settings = _make_settings({"LITELLM.EXTRA_HEADERS": json.dumps({"x-goog-api-key": header_key})})
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    handler = LiteLLMAIHandler()
    captured, boundaries = [], []
    loop = asyncio.get_running_loop()
    run_in_executor = loop.run_in_executor

    def mutate_then_dispatch(executor, function, *args):
        if not boundaries:
            boundaries.append(function)
            if source == "environment":
                monkeypatch.setenv("GEMINI_API_KEY", "foreign-key")
                monkeypatch.setenv("GEMINI_API_BASE", "https://foreign.example/v1")
            else:
                monkeypatch.setattr(litellm, "api_key", "foreign-key")
                monkeypatch.setattr(litellm, "api_base", "https://foreign.example/v1")
        return run_in_executor(executor, function, *args)

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        captured.append(request)
        raise TransportReached

    # Mutate only after the adapter's guards; retain native dispatch and URL/auth
    # selection and stop at HTTP before response parsing or streaming cleanup.
    monkeypatch.setattr(loop, "run_in_executor", mutate_then_dispatch)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    if entrypoint == "stream":
        monkeypatch.setattr(handler, "_requires_streaming", lambda model: True)
    try:
        with pytest.raises(TransportReached):
            if entrypoint == "probe":
                await handler.probe_completion(f"gemini/{model}")
            else:
                await handler.chat_completion(f"gemini/{model}", "sys", "usr")
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()
    assert len(boundaries) == len(captured) == 1
    version = "v1alpha" if "gemini-3" in model else "v1beta"
    base = initial_base or f"https://generativelanguage.googleapis.com/{version}"
    endpoint = "streamGenerateContent?alt=sse" if entrypoint == "stream" else "generateContent"
    assert str(captured[0].url) == f"{base}/models/{model}:{endpoint}"
    assert captured[0].headers["x-goog-api-key"] == (header_key or initial_key or DUMMY_LITELLM_API_KEY)


@pytest.mark.parametrize(("initial_key", "initial_account", "api_base"), (
    (None, "handler-account", None),
    ("handler-key", "handler-account", None),
    ("pat/handler-pat", "handler-account", None),
    (None, None, None),
    ("handler-key", None, None),
    ("handler-key", "", None),
    ("handler-key", "invalid account", None),
    ("handler-key", None, "https://explicit.example"),
))
@pytest.mark.parametrize("timing", (None, "before_params", "after_params"))
@pytest.mark.parametrize("model_name", ("llama3.1-8b", "claude-3-5-sonnet"))
@pytest.mark.asyncio
async def test_snowflake_transport_keeps_credential_and_account_sources(
    monkeypatch, initial_key, initial_account, api_base, timing, model_name,
):
    from litellm.litellm_core_utils import logging_worker

    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    if initial_key is not None:
        monkeypatch.setenv("SNOWFLAKE_JWT", initial_key)
    if initial_account is not None:
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT_ID", initial_account)
    handler = LiteLLMAIHandler()
    if api_base:
        # Defensive precedence check: Snowflake has no API base setting today.
        handler._provider_request_params["snowflake"] = {"api_base": api_base}
    model = f"snowflake/{model_name}"
    captured = []

    class TransportReached(BaseException):
        pass

    def send(client, request, **kwargs):
        captured.append(request)
        raise TransportReached

    async def async_send(client, request, **kwargs):
        return send(client, request, **kwargs)

    def mutate():
        monkeypatch.setenv("SNOWFLAKE_JWT", "another-handler-key")
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT_ID", "another-account")

    # Keep both native Snowflake endpoint/auth paths; stop only at HTTP transport.
    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    if timing == "before_params":
        mutate()
    resolve_params = handler._get_provider_request_params

    def request_params(*args, **kwargs):
        params = resolve_params(*args, **kwargs)
        if timing == "after_params":
            mutate()
        return params

    monkeypatch.setattr(handler, "_get_provider_request_params", request_params)
    valid_request = initial_key and (api_base or initial_account == "handler-account")
    try:
        if not initial_key:
            error, match = ValueError, "Snowflake JWT was not resolved for this request"
        elif not api_base and not initial_account:
            error, match = ValueError, "Snowflake account was not resolved for this request"
        else:
            error = TransportReached if valid_request else litellm.APIConnectionError
            match = None if valid_request else "Invalid account_id format"
        with pytest.raises(error, match=match):
            await handler.chat_completion(model=model, system="system", user="test")
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()
    if not valid_request:
        assert not captured
        return
    assert len(captured) == 1
    endpoint = "messages" if model_name.startswith("claude") else "chat/completions"
    base = api_base or "https://handler-account.snowflakecomputing.com"
    assert str(captured[0].url) == f"{base}/api/v2/cortex/v1/{endpoint}"
    expected_key = initial_key
    assert captured[0].headers["authorization"] == f"Bearer {expected_key.removeprefix('pat/')}"
    assert captured[0].headers["x-snowflake-authorization-token-type"] == (
        "PROGRAMMATIC_ACCESS_TOKEN" if expected_key.startswith("pat/") else "KEYPAIR_JWT"
    )
    assert "account_id" not in json.loads(captured[0].content)


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
    ("gemini/gemini-2.5-pro", "PALM_API_KEY"),
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
    ("gemini/gemini-2.5-pro", "GEMINI_API_KEY", "PALM_API_KEY"),
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


@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize(("source", "api_key", "bearer"), (
    ("settings", DUMMY_LITELLM_API_KEY, None),
    ("environment", DUMMY_LITELLM_API_KEY, None),
    ("settings", DUMMY_LITELLM_API_KEY, "owned-bearer"),
    ("environment", DUMMY_LITELLM_API_KEY, "owned-bearer"),
    ("settings", "ordinary-key", "owned-bearer"),
    ("environment", "ordinary-key", None),
    ("keyless", None, None),
    ("keyless", None, "owned-bearer"),
))
@pytest.mark.asyncio
async def test_native_anthropic_captured_literal_key(monkeypatch, entrypoint, source, api_key, bearer):
    from litellm.litellm_core_utils import logging_worker

    if entrypoint == "probe":
        # The minimal probe is system-only; let native LiteLLM add its user message
        # so this test reaches authentication/HTTP without changing probe production code.
        monkeypatch.setattr(litellm, "modify_params", True)
    overrides = {"ANTHROPIC.KEY": api_key} if source == "settings" else {}
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    if source == "environment":
        monkeypatch.setenv("ANTHROPIC_API_KEY", api_key)
    if bearer:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", bearer)
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "foreign-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "foreign-bearer")
    captured = []

    async def send(client, request, **kwargs):
        captured.append(request)
        return httpx.Response(200, request=request, json={
            "id": "test", "type": "message", "role": "assistant", "model": "claude-sonnet-4",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        async def invoke():
            if entrypoint == "chat":
                await handler.chat_completion("anthropic/claude-sonnet-4", "sys", "usr")
            else:
                await handler.probe_completion("anthropic/claude-sonnet-4")

        if api_key is None and bearer is None:
            with pytest.raises(litellm.AuthenticationError):
                await invoke()
            assert not captured
        else:
            await invoke()
            assert len(captured) == 1
            assert str(captured[0].url) == "https://api.anthropic.com/v1/messages"
            assert captured[0].headers.get_list("x-api-key") == ([api_key] if api_key else [])
            assert captured[0].headers.get_list("authorization") == ([] if api_key else [f"Bearer {bearer}"])
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()
    assert litellm_handler._anthropic_request_auth_token.get() is None
    assert litellm_handler.AnthropicModelInfo.get_api_key() == "foreign-key"
    assert litellm_handler.AnthropicModelInfo.get_auth_token() == "foreign-bearer"


@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("scenario", (
    "initial", "changed", "removed", "late_only", "missing", "configured", "configured_fallback", "azure_base",
))
@pytest.mark.asyncio
async def test_native_azure_endpoint_alias_snapshot(monkeypatch, entrypoint, scenario):
    from litellm.litellm_core_utils import logging_worker

    endpoint = f"https://owned-{entrypoint}-{scenario.replace('_', '-')}.openai.azure.com"
    overrides = {"OPENAI.API_VERSION": "2024-06-01"}
    monkeypatch.setenv("AZURE_API_KEY", "owned-key")
    if scenario not in ("late_only", "missing"):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    if scenario in ("configured", "configured_fallback", "azure_base"):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://lower-priority.openai.azure.com")
        if scenario == "azure_base":
            monkeypatch.setenv("AZURE_API_BASE", endpoint)
        else:
            overrides["OPENAI.API_BASE"] = endpoint
            if scenario == "configured":
                overrides["OPENAI.API_TYPE"] = "azure"
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    handler = LiteLLMAIHandler()
    if scenario == "removed":
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT")
    elif scenario != "initial" and scenario != "missing":
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://foreign.openai.azure.com")
    captured = []

    async def send(client, request, **kwargs):
        captured.append(request)
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        async def invoke():
            if entrypoint == "chat":
                await handler.chat_completion("azure/gpt-4o", "sys", "usr")
            else:
                await handler.probe_completion("azure/gpt-4o")

        if scenario == "late_only":
            with pytest.raises(ValueError, match="Refusing live api_base environment fallback for provider azure"):
                await invoke()
            assert not captured
        elif scenario == "missing":
            with pytest.raises((ValueError, litellm.APIError)):
                await invoke()
            assert not captured
        else:
            await invoke()
            assert len(captured) == 1
            assert str(captured[0].url) == (
                f"{endpoint}/openai/deployments/gpt-4o/chat/completions?api-version=2024-06-01"
            )
            assert captured[0].headers.get_list("api-key") == ["owned-key"]
            assert "authorization" not in captured[0].headers
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()


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
    if provider == "gdc":
        monkeypatch.setenv("GDC_API_BASE", "https://owned.example/v1/projects/owned/locations/local/chat/completions")
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
        ("groq/model", "GROQ_API_BASE", "https://another-request.example/v1"),
        ("huggingface/model", "HF_API_BASE", "https://another-request.example/v1"),
        ("huggingface/model", "HUGGINGFACE_API_BASE", "https://another-request.example/v1"),
        ("mistral/model", "MISTRAL_AZURE_API_BASE", "https://another-request.example/v1"),
        ("moonshot/model", "MOONSHOT_API_BASE", "https://another-request.example/v1"),
        ("ollama/model", "OLLAMA_API_BASE", "https://another-request.example"),
        ("openai_like/model", "OPENAI_LIKE_API_BASE", "https://another-request.example/v1"),
        ("openrouter/model", "OPENROUTER_API_BASE", "https://another-request.example/v1"),
        ("text-completion-codestral/codestral-latest", "CODESTRAL_API_BASE", "https://another-request.example/v1"),
        ("azure/gpt-4o", "AZURE_API_BASE", "https://another-request.example"),
        ("azure/gpt-4o", "AZURE_API_VERSION", "another-request-version"),
        ("vertex_ai/gemini-2.5-pro", "VERTEXAI_PROJECT", "another-request-project"),
        ("vertex_ai/gemini-2.5-pro", "GOOGLE_CLOUD_PROJECT", "another-request-project"),
        ("vertex_ai/gemini-2.5-pro", "GCLOUD_PROJECT", "another-request-project"),
        ("vertex_ai/gemini-2.5-pro", "VERTEXAI_LOCATION", "another-request-location"),
        ("vertex_ai/gemini-2.5-pro", "VERTEX_LOCATION", "another-request-location"),
        ("vertex_ai/gemini-2.5-pro", "VERTEXAI_API_BASE", "https://another-request.example"),
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


@pytest.mark.parametrize(("model", "version"), (("gemini-2.5-pro", "v1beta"), ("gemini-3-flash-preview", "v1alpha")))
@pytest.mark.asyncio
async def test_late_gemini_endpoint_keeps_native_default(monkeypatch, model, version):
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("GEMINI_API_BASE", "https://another-request.example")
    kwargs = await _call(handler, f"gemini/{model}")
    assert kwargs["api_base"] == f"https://generativelanguage.googleapis.com/{version}"
    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


def test_gemini_default_url_interface_fails_closed(monkeypatch):
    from litellm.llms.vertex_ai import common_utils

    monkeypatch.setattr(common_utils, "_get_gemini_url", lambda **kwargs: ("https://unexpected.example", "changed"))
    with pytest.raises(RuntimeError, match="Gemini default URL is incompatible"):
        LiteLLMAIHandler()._get_provider_request_params("gemini/gemini-2.5-pro")


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
        ("mistral/model", "MISTRAL_API_KEY", "MISTRAL_AZURE_API_BASE"),
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


@pytest.mark.asyncio
async def test_late_bedrock_mantle_environment_endpoint_is_rejected(monkeypatch):
    monkeypatch.setenv("BEDROCK_MANTLE_API_KEY", "request-key")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("BEDROCK_MANTLE_API_BASE", "https://another-request.example/v1")

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
    assert kwargs["api_key"] == DUMMY_LITELLM_API_KEY


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


@pytest.mark.parametrize("location_variable", ("VERTEXAI_LOCATION", "VERTEX_LOCATION"))
@pytest.mark.asyncio
async def test_vertex_environment_routing_is_snapshotted(monkeypatch, location_variable):
    monkeypatch.setenv("VERTEXAI_PROJECT", "tenant-a-project")
    monkeypatch.setenv(location_variable, "tenant-a-location")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("VERTEXAI_PROJECT", "tenant-b-project")
    monkeypatch.setenv("VERTEXAI_LOCATION", "tenant-b-location")

    kwargs = await _call(handler, "vertex_ai/gemini-2.5-pro")

    assert kwargs["vertex_project"] == "tenant-a-project"
    assert kwargs["vertex_location"] == "tenant-a-location"


@pytest.mark.asyncio
async def test_vertex_endpoint_is_snapshotted_with_environment_routing(monkeypatch):
    monkeypatch.setenv("VERTEXAI_API_BASE", "https://tenant-a.example/v1")
    monkeypatch.setenv("VERTEXAI_PROJECT", "tenant-a-project")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("VERTEXAI_API_BASE", "https://tenant-b.example/v1")
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

    assert kwargs["vertex_credentials"] == handler._vertex_gac_adc["cache_key"]
    assert handler._vertex_gac_adc["info"]["client_email"] == "tenant-a@example.com"


def _impersonated_vertex_adc(tenant, quota_project=None):
    return {
        "type": "impersonated_service_account",
        "service_account_impersonation_url": (
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{tenant}@example.iam.gserviceaccount.com:generateAccessToken"
        ),
        "source_credentials": {
            "type": "authorized_user",
            "client_id": f"{tenant}-client",
            "client_secret": f"{tenant}-secret",
            "refresh_token": f"{tenant}-refresh",
        },
        "quota_project_id": quota_project,
    }


@pytest.mark.parametrize("use_async", (False, True))
@pytest.mark.parametrize("quota_project", (None, "snapshot-quota"))
@pytest.mark.parametrize("project_environment", ("VERTEXAI_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"))
@pytest.mark.parametrize("credential_source", ("GOOGLE_APPLICATION_CREDENTIALS", "VERTEXAI_CREDENTIALS", "inline"))
@pytest.mark.asyncio
async def test_vertex_impersonated_adc_retains_snapshot_through_load_and_refresh(
    monkeypatch, tmp_path, use_async, quota_project, project_environment, credential_source,
):
    import google.auth
    from google.auth import impersonated_credentials
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    monkeypatch.setenv(project_environment, "same-project")
    no_default = MagicMock(side_effect=AssertionError("Ambient ADC must not be loaded"))
    monkeypatch.setattr(google.auth, "default", no_default)
    credentials_path = tmp_path / "impersonated-adc.json"
    handlers = []
    snapshots = []
    for tenant in ("tenant-a", "tenant-b"):
        snapshot = json.dumps(_impersonated_vertex_adc(tenant, quota_project))
        credentials_path.write_text(snapshot)
        if credential_source == "inline":
            monkeypatch.setenv("VERTEXAI_CREDENTIALS", snapshot)
        else:
            monkeypatch.setenv(credential_source, str(credentials_path))
        handler = LiteLLMAIHandler()
        handlers.append(handler)
        assert json.loads(handler._vertex_credentials) == json.loads(snapshot)
        snapshots.append(handler._provider_request_params["vertex_ai"]["vertex_credentials"])
    credentials_path.unlink()
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "late-credentials.json")
    monkeypatch.setenv("VERTEXAI_CREDENTIALS", "late-credentials.json")
    monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", "late-quota")
    for variable in ("VERTEXAI_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        monkeypatch.setenv(variable, "late-project")
    vertex = VertexBase()
    refreshed = []

    def refresh(credentials, request):
        assert isinstance(credentials, impersonated_credentials.Credentials)
        assert credentials.quota_project_id == quota_project
        refreshed.append(credentials.service_account_email)
        credentials.token = f"{credentials.service_account_email}:{credentials._source_credentials.refresh_token}"
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(impersonated_credentials.Credentials, "refresh", refresh)
    observed = []

    async def completion(**kwargs):
        snapshot = kwargs["vertex_credentials"]
        if use_async:
            token, project = await vertex.get_access_token_async(snapshot, kwargs["vertex_project"])
        else:
            token, project = vertex.get_access_token(snapshot, kwargs["vertex_project"])
        observed.append((snapshot, token, project))
        return _mock_response()

    monkeypatch.setattr(litellm_handler, "acompletion", completion)
    for probe in (False, True, False):
        if probe:
            await asyncio.gather(*(
                handler.probe_completion("vertex_ai/gemini-2.5-pro", _completion=completion)
                for handler in handlers
            ))
            for credentials, _ in vertex._credentials_project_mapping.values():
                credentials.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        else:
            await asyncio.gather(*(
                handler.chat_completion(model="vertex_ai/gemini-2.5-pro", system="sys", user="usr")
                for handler in handlers
            ))
    for snapshot, token, project in observed:
        tenant = "tenant-a" if snapshot == snapshots[0] else "tenant-b"
        assert snapshot in snapshots
        assert token == f"{tenant}@example.iam.gserviceaccount.com:{tenant}-refresh"
        assert project == "same-project"
    assert len(observed) == 6
    assert len(vertex._credentials_project_mapping) == 2
    assert len(refreshed) == 4
    no_default.assert_not_called()
    assert litellm_handler._vertex_request_credentials.get() is None


@pytest.mark.parametrize(("setting", "environment", "expected"), (
    ("configured-project", {"VERTEXAI_PROJECT": "vertex", "GOOGLE_CLOUD_PROJECT": "google", "GCLOUD_PROJECT": "legacy"},
     "configured-project"),
    (None, {"VERTEXAI_PROJECT": "vertex", "GOOGLE_CLOUD_PROJECT": "google", "GCLOUD_PROJECT": "legacy"}, "vertex"),
    (None, {"GOOGLE_CLOUD_PROJECT": "google", "GCLOUD_PROJECT": "legacy"}, "google"),
    (None, {"GCLOUD_PROJECT": "legacy"}, "legacy"),
    (None, {"GOOGLE_CLOUD_PROJECT": "", "GCLOUD_PROJECT": "legacy"}, None),
))
@pytest.mark.asyncio
async def test_vertex_project_snapshot_preserves_explicit_project_precedence(monkeypatch, setting, environment, expected):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({"VERTEXAI.VERTEX_PROJECT": setting}))
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)

    params = await _call(LiteLLMAIHandler(), "vertex_ai/gemini-2.5-pro")

    assert params.get("vertex_project") == expected


@pytest.mark.parametrize("failure", ("source", "project", "refresh"))
@pytest.mark.asyncio
async def test_vertex_impersonated_adc_failures_do_not_fall_back(monkeypatch, failure):
    import google.auth
    from google.auth.exceptions import InvalidType, RefreshError
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    info = _impersonated_vertex_adc("tenant-a")
    if failure == "source":
        info["source_credentials"]["type"] = "unsupported"
    no_default = MagicMock(side_effect=AssertionError("Ambient ADC must not be loaded"))
    monkeypatch.setattr(google.auth, "default", no_default)
    vertex = VertexBase()
    refresh = MagicMock(side_effect=RefreshError("refresh failed") if failure == "refresh" else None)
    monkeypatch.setattr(vertex, "refresh_auth", refresh)

    async def completion(**kwargs):
        return vertex.load_auth(kwargs["vertex_credentials"], None if failure == "project" else "project")

    expected = {"source": InvalidType, "project": ValueError, "refresh": RefreshError}[failure]
    outer = {"outer": "context"}
    token = litellm_handler._vertex_request_credentials.set(outer)
    try:
        with pytest.raises(expected):
            await LiteLLMAIHandler()._acompletion(
                _completion=completion, model="vertex_ai/gemini-2.5-pro", vertex_credentials=json.dumps(info),
            )
        assert litellm_handler._vertex_request_credentials.get() is outer
    finally:
        litellm_handler._vertex_request_credentials.reset(token)
    no_default.assert_not_called()


@pytest.mark.parametrize("context", (None, {"type": "impersonated_service_account", "other": "request"}))
def test_vertex_impersonated_adc_bridge_rejects_unscoped_or_mismatched_input(monkeypatch, context):
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    original = MagicMock(return_value="original")
    # A real function avoids MagicMock fabricating the install marker.
    def load_original(self, json_obj, scopes):
        return original(json_obj, scopes)

    monkeypatch.setattr(VertexBase, "_credentials_from_service_account", load_original)
    litellm_handler._install_vertex_impersonated_credentials_bridge()
    installed = VertexBase._credentials_from_service_account
    litellm_handler._install_vertex_impersonated_credentials_bridge()
    assert VertexBase._credentials_from_service_account is installed
    info = _impersonated_vertex_adc("tenant-a")
    token = litellm_handler._vertex_request_credentials.set(context)
    try:
        assert VertexBase()._credentials_from_service_account(info, ["scope"]) == "original"
    finally:
        litellm_handler._vertex_request_credentials.reset(token)
    original.assert_called_once_with(info, ["scope"])


@pytest.mark.parametrize("credential_type", ("service_account", "authorized_user", "external_account"))
def test_vertex_impersonated_adc_bridge_preserves_other_loaders(monkeypatch, credential_type):
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    vertex = VertexBase()
    credentials = MagicMock(project_id="project")
    loader = MagicMock(return_value=credentials)
    method = {
        "service_account": "_credentials_from_service_account",
        "authorized_user": "_credentials_from_authorized_user",
        "external_account": "_credentials_from_identity_pool",
    }[credential_type]
    if credential_type == "service_account":
        def load_original(self, json_obj, scopes):
            return loader(json_obj, scopes)
        monkeypatch.setattr(VertexBase, method, load_original)
    else:
        monkeypatch.setattr(vertex, method, loader)
    monkeypatch.setattr(vertex, "refresh_auth", MagicMock())
    litellm_handler._install_vertex_impersonated_credentials_bridge()
    info = {"type": credential_type}
    token = litellm_handler._vertex_request_credentials.set(info)
    try:
        loaded, project = vertex.load_auth(json.dumps(info), "project")
    finally:
        litellm_handler._vertex_request_credentials.reset(token)
    assert loaded is credentials
    assert project == "project"
    loader.assert_called_once()


def _external_vertex_adc(project_number, kind, quota_project):
    info = {
        "type": "external_account",
        "audience": (
            f"//iam.googleapis.com/projects/{project_number}/locations/global/"
            "workloadIdentityPools/pool/providers/provider"
        ),
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "credential_source": {"file": "/unused-subject-token"},
    }
    if quota_project:
        info["quota_project_id"] = quota_project
    if kind in ("aws", "explicit_aws"):
        info["subject_token_type"] = "urn:ietf:params:aws:token-type:aws4_request"
        info["credential_source"] = {
            "environment_id": "aws1",
            "region_url": "http://169.254.169.254/latest/meta-data/placement/availability-zone",
            "url": "http://169.254.169.254/latest/meta-data/iam/security-credentials",
            "regional_cred_verification_url": "https://sts.{region}.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15",
        }
        if kind == "explicit_aws":
            info.update(aws_access_key_id="snapshot-access", aws_secret_access_key="snapshot-secret",
                        aws_region_name="us-east-1")
    elif kind == "executable":
        info["credential_source"] = {"executable": {"command": "never-execute-in-tests"}}
    return info


@pytest.mark.parametrize("source", ("inline", "vertex_file", "adc_file"))
@pytest.mark.parametrize("explicit_project", (None, "configured-project"))
@pytest.mark.parametrize("entrypoint", ("probe", "completion"))
@pytest.mark.asyncio
async def test_vertex_executable_credentials_fail_before_dispatch(
    monkeypatch, tmp_path, source, explicit_project, entrypoint,
):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({
        "VERTEXAI.VERTEX_PROJECT": explicit_project,
    }))
    info = json.dumps(_external_vertex_adc("123", "executable", None))
    if source == "inline":
        monkeypatch.setenv("VERTEXAI_CREDENTIALS", info)
    else:
        path = tmp_path / "executable-adc.json"
        path.write_text(info)
        variable = "VERTEXAI_CREDENTIALS" if source == "vertex_file" else "GOOGLE_APPLICATION_CREDENTIALS"
        monkeypatch.setenv(variable, str(path))
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES", "1")
    completion = AsyncMock(return_value=_mock_response())
    monkeypatch.setattr(litellm_handler, "acompletion", completion)
    expected_error = ValueError if entrypoint == "probe" else openai.APIError
    with pytest.raises(expected_error, match="executable.*request isolation"):
        if entrypoint == "probe":
            await handler.probe_completion("vertex_ai/gemini-2.5-pro")
        else:
            await handler.chat_completion("vertex_ai/gemini-2.5-pro", "sys", "usr")
    completion.assert_not_called()
    assert litellm_handler._vertex_request_credentials.get() is None
    await _call(handler, "gpt-4o")


@pytest.mark.parametrize("explicit_project", (None, "configured-project"))
@pytest.mark.asyncio
async def test_vertex_executable_dict_fails_before_loading_credentials(monkeypatch, explicit_project):
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    loader = MagicMock(side_effect=AssertionError("Do not load executable credentials"))
    monkeypatch.setattr(VertexBase, "load_auth", loader)
    completion = AsyncMock(return_value=_mock_response())
    with pytest.raises(ValueError, match="executable.*request isolation"):
        await LiteLLMAIHandler()._acompletion(
            _completion=completion, model="vertex_ai/gemini-2.5-pro", vertex_project=explicit_project,
            vertex_credentials=_external_vertex_adc("123", "executable", None),
        )
    loader.assert_not_called()
    completion.assert_not_called()
    assert litellm_handler._vertex_request_credentials.get() is None


@pytest.mark.parametrize("source", ("cloud_sdk", "direct_path"))
@pytest.mark.parametrize("use_async", (False, True))
@pytest.mark.parametrize("project", (None, "configured-project"))
@pytest.mark.asyncio
async def test_vertex_executable_default_adc_and_path_are_rejected(monkeypatch, tmp_path, source, use_async, project):
    from google.auth import _cloud_sdk, pluggable
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    path = tmp_path / "application_default_credentials.json"
    path.write_text(json.dumps(_external_vertex_adc("123", "executable", None)))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    monkeypatch.setenv("GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES", "1")
    monkeypatch.setattr(_cloud_sdk, "get_project_id", lambda: "sdk-project")
    helper = MagicMock(side_effect=AssertionError("Executable helper must not run"))
    monkeypatch.setattr(pluggable.subprocess, "run", helper)
    vertex = VertexBase()

    async def completion(**kwargs):
        credentials = kwargs.get("vertex_credentials")
        if use_async:
            return await vertex.get_access_token_async(credentials, project)
        return vertex.get_access_token(credentials, project)

    kwargs = {"vertex_credentials": str(path)} if source == "direct_path" else {}
    with pytest.raises(ValueError, match="executable.*request isolation"):
        await LiteLLMAIHandler()._acompletion(
            _completion=completion, model="vertex_ai/gemini-2.5-pro", **kwargs,
        )
    helper.assert_not_called()
    assert not vertex._credentials_project_mapping
    assert litellm_handler._vertex_request_credentials.get() is None


@pytest.mark.parametrize("use_async", (False, True))
@pytest.mark.parametrize("cache_shape", ("tuple", "legacy"))
@pytest.mark.parametrize("expiry_minutes", (60, 1, -1))
@pytest.mark.asyncio
async def test_vertex_cached_executable_credentials_are_rejected(monkeypatch, use_async, cache_shape, expiry_minutes):
    from google.auth import pluggable
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    credentials = pluggable.Credentials.from_info(_external_vertex_adc("123", "executable", None))
    credentials.token = "previous-identity-token"
    credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=expiry_minutes)
    vertex = VertexBase()
    entry = (credentials, "project") if cache_shape == "tuple" else credentials
    vertex._credentials_project_mapping[(None, "project")] = entry
    refresh = MagicMock(side_effect=AssertionError("Do not refresh executable credentials"))
    monkeypatch.setattr(vertex, "refresh_auth", refresh)

    async def completion(**kwargs):
        if use_async:
            return await vertex.get_access_token_async(None, "project")
        return vertex.get_access_token(None, "project")

    with pytest.raises(ValueError, match="executable.*request isolation"):
        await LiteLLMAIHandler()._acompletion(_completion=completion, model="vertex_ai/gemini-2.5-pro")
    refresh.assert_not_called()
    assert not vertex._background_refresh_tasks
    assert vertex._credentials_project_mapping[(None, "project")] is entry
    assert litellm_handler._vertex_request_credentials.get() is None


@pytest.mark.asyncio
async def test_vertex_executable_guard_delegates_and_restores_context(monkeypatch):
    from google.auth import pluggable
    from google.auth.credentials import TokenState

    refresh = MagicMock(return_value="original-refresh")

    def original_refresh(credentials, request):
        return refresh(credentials, request)

    pluggable.Credentials.refresh = original_refresh
    litellm_handler._install_vertex_executable_guard()
    descriptors = {name: inspect.getattr_static(pluggable.Credentials, name)
                   for name in ("from_info", "refresh", "expired", "token_state")}
    litellm_handler._install_vertex_executable_guard()
    assert all(inspect.getattr_static(pluggable.Credentials, name) is value
               for name, value in descriptors.items())

    class ExecutableCredentials(pluggable.Credentials):
        pass

    credentials = ExecutableCredentials.from_info(_external_vertex_adc("123", "executable", None))
    assert isinstance(credentials, ExecutableCredentials)
    credentials.token = "outside-request-token"
    assert credentials.expired is False
    assert credentials.token_state is TokenState.FRESH
    request = object()
    assert credentials.refresh(request) == "original-refresh"
    refresh.assert_called_once_with(credentials, request)
    refresh.reset_mock()

    async def completion(**kwargs):
        assert litellm_handler._vertex_request_active.get()
        with pytest.raises(ValueError, match="executable.*request isolation"):
            await asyncio.to_thread(credentials.refresh, request)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await LiteLLMAIHandler()._acompletion(_completion=completion, model="vertex_ai/gemini-2.5-pro")
    refresh.assert_not_called()
    assert not litellm_handler._vertex_request_active.get()
    assert litellm_handler._vertex_request_credentials.get() is None
    assert credentials.token_state is TokenState.FRESH


@pytest.mark.asyncio
async def test_vertex_executable_cache_replacement_after_lock_wait_is_rejected(monkeypatch):
    from google.auth import pluggable
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    credentials = pluggable.Credentials.from_info(_external_vertex_adc("123", "executable", None))
    credentials.token = "replacement-identity-token"
    vertex = VertexBase()
    entered = asyncio.Event()
    release = asyncio.Event()

    class WaitingLock:
        async def __aenter__(self):
            entered.set()
            await release.wait()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(vertex, "_acquire_async_refresh_lock", lambda key: WaitingLock())

    async def completion(**kwargs):
        return await vertex.get_access_token_async(None, "project")

    task = asyncio.create_task(LiteLLMAIHandler()._acompletion(
        _completion=completion, model="vertex_ai/gemini-2.5-pro",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        vertex._credentials_project_mapping[(None, "project")] = (credentials, "project")
        release.set()
        with pytest.raises(ValueError, match="executable.*request isolation"):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not litellm_handler._vertex_request_active.get()


@pytest.fixture
def vertex_native_adc_http(monkeypatch):
    from urllib.parse import parse_qs

    from google.auth.transport.requests import Request

    calls = []

    def request(self, url, method="GET", body=None, headers=None, **kwargs):
        assert method == "POST"
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        calls.append((url, headers, body))
        if url.endswith("/fake@example.iam.gserviceaccount.com:generateAccessToken"):
            assert headers["authorization"] == "Bearer fake-source-token"
            payload = {
                "accessToken": "fake-iam-" + headers["x-goog-user-project"],
                "expireTime": (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        else:
            assert url in ("https://sts.googleapis.com/v1/token", "https://sts.googleapis.com/v1/oauth/token")
            fields = parse_qs(body.decode() if isinstance(body, bytes) else body)
            if url.endswith("/oauth/token"):
                assert fields["refresh_token"] == ["fake-refresh"]
            else:
                assert fields["subject_token"] == ["fake-subject-token"]
            payload = {
                "access_token": "fake-source-token", "expires_in": 3600, "token_type": "Bearer",
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            }
        return type("Response", (), {"status": 200, "data": json.dumps(payload).encode(), "headers": {}})()

    # Keep credential factories, refresh, and header application native; intercept only HTTP.
    monkeypatch.setattr(Request, "__call__", request)
    return calls


@pytest.mark.parametrize("entrypoint", ("chat_completion", "probe_completion"))
@pytest.mark.asyncio
async def test_vertex_gac_workforce_public_native_refresh(monkeypatch, tmp_path, vertex_native_adc_http, entrypoint):
    from google.auth import external_account_authorized_user
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    path = tmp_path / "non-sdk-adc.json"
    path.write_text(json.dumps({
        "type": "external_account_authorized_user", "client_id": "fake-client", "client_secret": "fake-secret",
        "refresh_token": "fake-refresh", "quota_project_id": "file-quota",
        "audience": "//iam.googleapis.com/locations/global/workforcePools/fake/providers/fake",
        "token_url": "https://sts.googleapis.com/v1/oauth/token",
    }))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({
        "VERTEXAI.VERTEX_PROJECT": "resource-project",
    }))
    handler = LiteLLMAIHandler()
    vertex = VertexBase()
    observed = []

    async def completion(**kwargs):
        observed.append(await vertex.get_access_token_async(
            kwargs.get("vertex_credentials"), kwargs.get("vertex_project"),
        ))
        return _mock_response()

    monkeypatch.setattr(litellm_handler, "acompletion", completion)
    for _ in range(2):
        if entrypoint == "chat_completion":
            assert await handler.chat_completion("vertex_ai/gemini-2.5-pro", "sys", "usr") == ("ok", "stop")
        else:
            await handler.probe_completion("vertex_ai/gemini-2.5-pro", _completion=completion)
    assert observed == [("fake-source-token", "resource-project")] * 2
    assert len(vertex_native_adc_http) == 1
    cached = list(vertex._credentials_project_mapping.values())
    assert cached
    assert all(
        isinstance(credentials, external_account_authorized_user.Credentials)
        and credentials is cached[0][0] and project == "resource-project"
        for credentials, project in cached
    )
    assert not litellm_handler._vertex_request_active.get()


@pytest.mark.parametrize("entrypoint", ("chat_completion", "probe_completion"))
@pytest.mark.parametrize("source", ("gac", "sdk_gac", "vertex"))
@pytest.mark.asyncio
async def test_vertex_adc_public_native_iam_quota_snapshot(
    monkeypatch, tmp_path, vertex_native_adc_http, entrypoint, source,
):
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "resource-project")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({
        "VERTEXAI.VERTEX_PROJECT": "resource-project",
    }))
    subject = tmp_path / "subject-token"
    subject.write_text("fake-subject-token")
    info = _external_vertex_adc("123", "file", "file-quota")
    info["credential_source"] = {"file": str(subject)}
    info["service_account_impersonation_url"] = (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        "fake@example.iam.gserviceaccount.com:generateAccessToken"
    )
    path = tmp_path / ("application_default_credentials.json" if source == "sdk_gac" else "non-sdk-adc.json")
    path.write_text(json.dumps(info))
    monkeypatch.setenv("VERTEXAI_CREDENTIALS" if source == "vertex" else "GOOGLE_APPLICATION_CREDENTIALS", str(path))
    handlers = []
    for quota in ("initial-quota-a", "initial-quota-b"):
        monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", quota)
        handlers.append(LiteLLMAIHandler())
    monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", "late-quota")
    vertex = VertexBase()
    observed = []

    async def completion(**kwargs):
        observed.append(await vertex.get_access_token_async(
            kwargs.get("vertex_credentials"), kwargs.get("vertex_project"),
        ))
        return _mock_response()

    monkeypatch.setattr(litellm_handler, "acompletion", completion)
    for _ in range(2):
        for handler in handlers:
            if entrypoint == "chat_completion":
                assert await handler.chat_completion("vertex_ai/gemini-2.5-pro", "sys", "usr") == ("ok", "stop")
            else:
                await handler.probe_completion("vertex_ai/gemini-2.5-pro", _completion=completion)
    expected_quotas = ["file-quota"] if source == "vertex" else ["initial-quota-a", "initial-quota-b"]
    iam_calls = [headers for url, headers, _ in vertex_native_adc_http if url.endswith(":generateAccessToken")]
    # Assert the actual IAM refresh header, not just a field on the credential object.
    assert [headers.get("x-goog-user-project") for headers in iam_calls] == expected_quotas
    assert len(vertex_native_adc_http) == 2 * len(expected_quotas)
    per_handler_quotas = ["file-quota"] * 2 if source == "vertex" else expected_quotas
    assert observed == [("fake-iam-" + quota, "resource-project") for quota in per_handler_quotas] * 2
    cached = list(vertex._credentials_project_mapping.values())
    assert {credentials.quota_project_id for credentials, _ in cached} == set(expected_quotas)
    assert len({id(credentials) for credentials, _ in cached}) == len(expected_quotas)
    assert all(project == "resource-project" for _, project in cached)
    assert not litellm_handler._vertex_request_active.get()


@pytest.fixture(scope="module")
def vertex_test_service_account_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()


@pytest.mark.parametrize("credential_type", ("authorized_user", "service_account"))
@pytest.mark.parametrize("source", ("gac", "vertex"))
@pytest.mark.parametrize("quota_override", (None, "", "initial-quota"))
@pytest.mark.asyncio
async def test_vertex_gac_native_factory_and_quota_precedence(
    monkeypatch, tmp_path, vertex_test_service_account_key, credential_type, source, quota_override,
):
    from google.auth.transport.requests import Request
    from google.oauth2 import credentials as user_credentials
    from google.oauth2 import service_account
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    info = {"type": credential_type, "quota_project_id": "file-quota"}
    if credential_type == "authorized_user":
        info.update(client_id="fake-client", client_secret="fake-secret", refresh_token="fake-refresh")
        project, credential_class = "resource-project", user_credentials.Credentials
    else:
        info.update(
            client_email="fake@example.iam.gserviceaccount.com", private_key=vertex_test_service_account_key,
            token_uri="https://oauth2.googleapis.com/token", project_id="credential-project",
        )
        project, credential_class = None, service_account.Credentials
    path = tmp_path / "non-sdk-adc.json"
    path.write_text(json.dumps(info))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS" if source == "gac" else "VERTEXAI_CREDENTIALS", str(path))
    if quota_override is not None:
        monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", quota_override)
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({"VERTEXAI.VERTEX_PROJECT": project}))
    handler = LiteLLMAIHandler()
    path.unlink()
    monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", "late-quota")
    auth_requests = []

    def request(self, url, method="GET", **kwargs):
        assert url == "https://oauth2.googleapis.com/token" and method == "POST"
        auth_requests.append(url)
        return type("Response", (), {
            "status": 200, "headers": {},
            "data": b'{"access_token":"fake-token","expires_in":3600,"token_type":"Bearer"}',
        })()

    monkeypatch.setattr(Request, "__call__", request)
    vertex = VertexBase()

    async def completion(**kwargs):
        assert await vertex.get_access_token_async(kwargs["vertex_credentials"], kwargs.get("vertex_project")) == (
            "fake-token", project or "credential-project",
        )
        return _mock_response()

    for _ in range(2):
        await handler.probe_completion("vertex_ai/gemini-2.5-pro", _completion=completion)
    # Service-account constructors ignore a JSON quota field; only ADC applies
    # the nonempty captured environment override. Explicit Vertex does not.
    expected_quota = "file-quota" if credential_type == "authorized_user" else None
    if source == "gac" and quota_override:
        expected_quota = quota_override
    assert len(auth_requests) == 1
    assert vertex._credentials_project_mapping
    for credentials, resolved_project in vertex._credentials_project_mapping.values():
        assert isinstance(credentials, credential_class)
        assert credentials.quota_project_id == expected_quota
        assert resolved_project == (project or "credential-project")


def _write_vertex_sdk_adc(tmp_path, quota_project):
    path = tmp_path / "application_default_credentials.json"
    path.write_text(json.dumps({
        "type": "authorized_user", "client_id": "test-client", "client_secret": "test-secret",
        "refresh_token": "test-refresh", "quota_project_id": quota_project,
    }))
    (tmp_path / "configurations").mkdir()
    project_config = tmp_path / "configurations" / "config_default"
    project_config.write_text("[core]\nproject = sdk-project\n")
    return path, project_config


@pytest.mark.parametrize("use_async", (False, True))
@pytest.mark.parametrize("explicit_sdk_path", (False, True))
@pytest.mark.parametrize("quota_project", (None, "billing-project"))
@pytest.mark.asyncio
async def test_vertex_non_executable_default_adc_preserves_refresh_and_cache(
    monkeypatch, tmp_path, use_async, explicit_sdk_path, quota_project,
):
    from google.auth import pluggable
    from google.oauth2 import credentials as user_credentials
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    path, project_config = _write_vertex_sdk_adc(tmp_path, quota_project)
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    if explicit_sdk_path:
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    refreshed = []

    def refresh(credentials, request):
        assert credentials.quota_project_id == quota_project
        refreshed.append(credentials)
        credentials.token = "authorized-user-token"
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(user_credentials.Credentials, "refresh", refresh)
    vertex = VertexBase()
    unrelated = pluggable.Credentials.from_info(_external_vertex_adc("123", "executable", None))
    vertex._credentials_project_mapping[("unrelated", "other-project")] = (unrelated, "other-project")

    async def completion(**kwargs):
        if use_async:
            return await vertex.get_access_token_async(kwargs["vertex_credentials"], None)
        return vertex.get_access_token(kwargs["vertex_credentials"], None)

    handler = LiteLLMAIHandler()
    project_config.write_text("[core]\nproject = later-sdk-project\n")
    for _ in range(2):
        assert await handler._acompletion(
            _completion=completion, model="vertex_ai/gemini-2.5-pro",
            **handler._provider_request_params["vertex_ai"],
        ) == (
            "authorized-user-token", "sdk-project",
        )
    assert len(refreshed) == 1
    assert not litellm_handler._vertex_request_active.get()


@pytest.mark.parametrize("entrypoint", ("chat_completion", "probe_completion"))
@pytest.mark.parametrize("quota_project", (None, "billing-project"))
@pytest.mark.asyncio
async def test_vertex_explicit_sdk_adc_public_entrypoints_preserve_project_and_cache(
    monkeypatch, tmp_path, entrypoint, quota_project,
):
    from google.oauth2 import credentials as user_credentials
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    path, project_config = _write_vertex_sdk_adc(tmp_path, quota_project)
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    handler = LiteLLMAIHandler()
    project_config.write_text("[core]\nproject = later-sdk-project\n")
    refreshed = []

    def refresh(credentials, request):
        assert credentials.quota_project_id == quota_project
        refreshed.append(credentials)
        credentials.token = "authorized-user-token"
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(user_credentials.Credentials, "refresh", refresh)
    vertex = VertexBase()
    observed = []

    async def completion(**kwargs):
        observed.append(await vertex.get_access_token_async(
            kwargs["vertex_credentials"], kwargs.get("vertex_project"),
        ))
        return _mock_response()

    monkeypatch.setattr(litellm_handler, "acompletion", completion)
    for _ in range(2):
        if entrypoint == "chat_completion":
            assert await handler.chat_completion(
                model="vertex_ai/gemini-2.5-pro", system="sys", user="usr",
            ) == ("ok", "stop")
        else:
            await handler.probe_completion("vertex_ai/gemini-2.5-pro", _completion=completion)
    assert observed == [("authorized-user-token", "sdk-project")] * 2
    assert len(refreshed) == 1
    # LiteLLM caches aliases with both the requested and resolved project.
    assert vertex._credentials_project_mapping
    assert all(
        credentials is refreshed[0] and project == "sdk-project"
        for credentials, project in vertex._credentials_project_mapping.values()
    )
    assert not litellm_handler._vertex_request_active.get()


@pytest.mark.parametrize("entrypoint", ("chat_completion", "probe_completion"))
@pytest.mark.parametrize("change", ("late_same_sdk_path", "different_google_path", "late_vertex_credentials"))
@pytest.mark.asyncio
async def test_vertex_explicit_sdk_adc_public_entrypoints_reject_changed_sources(
    monkeypatch, tmp_path, entrypoint, change,
):
    path, _ = _write_vertex_sdk_adc(tmp_path, "billing-project")
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    if change != "late_same_sdk_path":
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    handler = LiteLLMAIHandler()
    if change == "late_same_sdk_path":
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    elif change == "different_google_path":
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "other-adc.json"))
    else:
        monkeypatch.setenv("VERTEXAI_CREDENTIALS", '{"type": "authorized_user", "client_id": "other-client"}')
    completion = AsyncMock(return_value=_mock_response())
    monkeypatch.setattr(litellm_handler, "acompletion", completion)

    try:
        with pytest.raises(ValueError, match="Refusing live Vertex credential environment fallback"):
            if entrypoint == "chat_completion":
                await handler.chat_completion(model="vertex_ai/gemini-2.5-pro", system="sys", user="usr")
            else:
                await handler.probe_completion("vertex_ai/gemini-2.5-pro", _completion=completion)
    finally:
        completion.assert_not_called()


@pytest.mark.parametrize("selector", ("vertex_inline", "vertex_file", "non_sdk_file", "literal_parent_path"))
@pytest.mark.asyncio
async def test_vertex_explicit_sdk_adc_selector_precedence(monkeypatch, tmp_path, selector):
    path, _ = _write_vertex_sdk_adc(tmp_path, "billing-project")
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    explicit_info = {"type": "authorized_user", "client_id": "explicit-client"}
    explicit_file = tmp_path / "explicit-adc.json"
    explicit_file.write_text(json.dumps(explicit_info))
    if selector == "vertex_inline":
        monkeypatch.setenv("VERTEXAI_CREDENTIALS", json.dumps(explicit_info))
    elif selector == "vertex_file":
        monkeypatch.setenv("VERTEXAI_CREDENTIALS", str(explicit_file))
    elif selector == "non_sdk_file":
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(explicit_file))
    else:
        # Google Auth compares strings, not canonical paths or file identity.
        literal_path = str(tmp_path / "configurations" / ".." / path.name)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", literal_path)
        explicit_info = json.loads(path.read_text())
    handler = LiteLLMAIHandler()
    kwargs = await _call(handler, "vertex_ai/gemini-2.5-pro")

    if selector in ("non_sdk_file", "literal_parent_path"):
        assert kwargs["vertex_credentials"] == handler._vertex_gac_adc["cache_key"]
        assert handler._vertex_gac_adc["info"] == explicit_info
        assert handler._vertex_gac_adc["sdk_project"] is None
        assert handler._vertex_gac_adc["sdk_project_error"] is None
    else:
        assert json.loads(kwargs["vertex_credentials"]) == explicit_info
        assert handler._vertex_gac_adc is None
    assert handler._vertex_default_adc is None


@pytest.mark.asyncio
async def test_vertex_default_adc_does_not_adopt_another_cloud_sdk_identity(monkeypatch, tmp_path):
    from google.auth import _cloud_sdk
    from google.oauth2 import credentials as user_credentials
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({
        "VERTEXAI.VERTEX_PROJECT": "shared-project",
    }))
    monkeypatch.setattr(_cloud_sdk, "get_project_id", lambda: "shared-project")
    handlers = []
    for tenant in ("tenant-a", "tenant-b"):
        directory = tmp_path / tenant
        directory.mkdir()
        (directory / "application_default_credentials.json").write_text(json.dumps({
            "type": "authorized_user", "client_id": tenant, "client_secret": "test-secret",
            "refresh_token": f"refresh-{tenant}",
        }))
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(directory))
        handlers.append(LiteLLMAIHandler())

    def refresh(credentials, request):
        credentials.token = f"token-{credentials.client_id}"
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(user_credentials.Credentials, "refresh", refresh)
    vertex = VertexBase()
    observed = []

    async def completion(**kwargs):
        token, _ = await vertex.get_access_token_async(
            kwargs.get("vertex_credentials"), kwargs.get("vertex_project"),
        )
        observed.append(token)
        return _mock_response()

    for _ in range(2):
        for handler in handlers:
            await handler.probe_completion("vertex_ai/gemini-2.5-pro", _completion=completion)
    assert observed == ["token-tenant-a", "token-tenant-b"] * 2


@pytest.mark.parametrize("project_source", ("default", "active_file", "active_environment", "project_environment"))
@pytest.mark.asyncio
async def test_vertex_default_adc_captures_resource_project_separately_from_quota(
    monkeypatch, tmp_path, project_source,
):
    from google.oauth2 import credentials as user_credentials
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    directory = tmp_path / "sdk"
    directory.mkdir()
    (directory / "configurations").mkdir()
    (directory / "configurations" / "config_default").write_text("[core]\nproject = default-project\n")
    (directory / "configurations" / "config_named").write_text("[core]\nproject = named-project\n")
    if project_source == "active_file":
        (directory / "active_config").write_text("named")
    elif project_source == "active_environment":
        monkeypatch.setenv("CLOUDSDK_ACTIVE_CONFIG_NAME", "named")
    elif project_source == "project_environment":
        monkeypatch.setenv("CLOUDSDK_CORE_PROJECT", "environment-project")
    path = directory / "application_default_credentials.json"
    path.write_text(json.dumps({
        "type": "authorized_user", "client_id": "original-client", "client_secret": "test-secret",
        "refresh_token": "test-refresh", "quota_project_id": "file-quota",
    }))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(directory))
    monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", "snapshot-quota")
    handler = LiteLLMAIHandler()
    path.unlink()
    (directory / "configurations" / "config_default").write_text("[core]\nproject = late-project\n")
    (directory / "configurations" / "config_named").write_text("[core]\nproject = late-project\n")
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "other-sdk"))
    monkeypatch.setenv("CLOUDSDK_CORE_PROJECT", "late-project")
    monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", "late-quota")

    def refresh(credentials, request):
        assert credentials.client_id == "original-client"
        assert credentials.quota_project_id == "snapshot-quota"
        credentials.token = "original-token"
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(user_credentials.Credentials, "refresh", refresh)
    vertex = VertexBase()
    expected_project = {
        "default": "default-project", "active_file": "named-project",
        "active_environment": "named-project", "project_environment": "environment-project",
    }[project_source]

    async def completion(**kwargs):
        return await vertex.get_access_token_async(kwargs["vertex_credentials"], None)

    assert await handler._acompletion(_completion=completion, model="vertex_ai/gemini-2.5-pro") == (
        "original-token", expected_project,
    )


@pytest.mark.parametrize(("configuration", "legacy", "expected"), (
    (None, None, "named-project"),
    ("", None, "named-project"),
    ("invalid config", None, "named-project"),
    ("NONE", None, "installation-project"),
    ("NONE", "[core]\nproject = legacy-project\n", "installation-project"),
    (None, "[core]\nproject = legacy-project\n", "legacy-project"),
    ("", "[core]\nproject = legacy-project\n", "named-project"),
    ("invalid config", "[core]\nproject = legacy-project\n", "legacy-project"),
    (None, "# This properties file has been superseded by named configurations.\n"
     "# Editing it will have no effect.\n\n[core]\nproject = legacy-project\n", "named-project"),
))
def test_cloud_sdk_project_snapshot_matches_configuration_precedence(
    monkeypatch, tmp_path, configuration, legacy, expected,
):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / ".install").mkdir()
    (sdk / "properties").write_text("[core]\nproject = installation-project\n")
    monkeypatch.setattr(litellm_handler.shutil, "which", lambda command: str(sdk / "bin" / "gcloud"))
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "configurations").mkdir()
    (directory / "configurations" / "config_default").write_text(
        "[core]\nresource = named-project\nproject = %(resource)s\n",
    )
    if configuration is not None:
        (directory / "active_config").write_text(configuration)
    if legacy is not None:
        (directory / "properties").write_text(legacy)
    assert litellm_handler._snapshot_cloud_sdk_project(str(directory)) == expected


@pytest.mark.parametrize(("value", "expected"), ((" example-project ", "example-project"), (" \t ", None)))
def test_cloud_sdk_project_snapshot_normalizes_environment_output(monkeypatch, tmp_path, value, expected):
    monkeypatch.setenv("CLOUDSDK_CORE_PROJECT", value)
    assert litellm_handler._snapshot_cloud_sdk_project(str(tmp_path)) == expected


@pytest.mark.parametrize("source", ("file", "environment"))
@pytest.mark.parametrize(("value", "expected"), (
    ("named", "named-project"), ("named\n", None), ("NONE", None),
    (" named ", "default-project"), ("NONE\n", "default-project"), ("\n", "default-project"),
))
def test_cloud_sdk_project_snapshot_preserves_native_activator_names(monkeypatch, tmp_path, source, value, expected):
    monkeypatch.setattr(litellm_handler.shutil, "which", lambda command: None)
    (tmp_path / "configurations").mkdir()
    (tmp_path / "configurations" / "config_default").write_text("[core]\nproject = default-project\n")
    (tmp_path / "configurations" / "config_named").write_text("[core]\nproject = named-project\n")
    if source == "file":
        (tmp_path / "active_config").write_text(value)
    else:
        monkeypatch.setenv("CLOUDSDK_ACTIVE_CONFIG_NAME", value)
        if value in (" named ", "NONE\n", "\n"):
            # Deliberately reject malformed environment selectors rather than
            # opening arbitrary paths; the SDK's raw-env behavior is not emulated.
            with pytest.raises(ValueError, match="Invalid Cloud SDK configuration name"):
                litellm_handler._snapshot_cloud_sdk_project(str(tmp_path))
            return
    assert litellm_handler._snapshot_cloud_sdk_project(str(tmp_path)) == expected
    if value == "named\n":
        # SDK 583 accepts the terminal newline but keeps it in the filename.
        (tmp_path / "configurations" / "config_named\n").write_text("[core]\nproject = literal-name-project\n")
        assert litellm_handler._snapshot_cloud_sdk_project(str(tmp_path)) == "literal-name-project"


@pytest.mark.parametrize("error_source", ("json", "directory"))
@pytest.mark.asyncio
async def test_vertex_default_adc_snapshot_error_cannot_adopt_a_repaired_file(monkeypatch, tmp_path, error_source):
    from google.auth import _default
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    path = tmp_path / "application_default_credentials.json"
    if error_source == "json":
        path.write_text("{")
    else:
        path.mkdir()
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    handler = LiteLLMAIHandler()
    if error_source == "directory":
        path.rmdir()
    path.write_text(json.dumps({
        "type": "authorized_user", "client_id": "late-client", "client_secret": "late-secret",
        "refresh_token": "late-refresh",
    }))
    discover = MagicMock(side_effect=AssertionError("Must not fall back to managed ADC"))
    monkeypatch.setattr(_default, "_get_gce_credentials", discover)
    vertex = VertexBase()

    async def completion(**kwargs):
        return await vertex.get_access_token_async(kwargs["vertex_credentials"], "project")

    for _ in range(2):
        with pytest.raises(ValueError, match="Unable to snapshot default Vertex credentials"):
            await handler._acompletion(_completion=completion, model="vertex_ai/gemini-2.5-pro")
    discover.assert_not_called()
    assert not vertex._credentials_project_mapping
    assert litellm_handler._vertex_request_default_adc.get() is None
    assert await handler._acompletion(_completion=AsyncMock(return_value="ok"), model="openai/gpt-4o") == "ok"


@pytest.mark.parametrize("explicit_project", (None, "explicit-project"))
@pytest.mark.asyncio
async def test_vertex_default_adc_keeps_project_snapshot_error(monkeypatch, tmp_path, explicit_project):
    from google.oauth2 import credentials as user_credentials
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    (tmp_path / "application_default_credentials.json").write_text(json.dumps({
        "type": "authorized_user", "client_id": "original-client", "client_secret": "test-secret",
        "refresh_token": "test-refresh",
    }))
    (tmp_path / "configurations").mkdir()
    config = tmp_path / "configurations" / "config_default"
    config.write_text("invalid configuration")
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    handler = LiteLLMAIHandler()
    config.write_text("[core]\nproject = late-project\n")

    def refresh(credentials, request):
        credentials.token = "original-token"
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(user_credentials.Credentials, "refresh", refresh)
    vertex = VertexBase()

    async def completion(**kwargs):
        return await vertex.get_access_token_async(kwargs["vertex_credentials"], explicit_project)

    if explicit_project:
        assert await handler._acompletion(_completion=completion, model="vertex_ai/gemini-2.5-pro") == (
            "original-token", explicit_project,
        )
    else:
        with pytest.raises(ValueError, match="Unable to snapshot Cloud SDK project"):
            await handler._acompletion(_completion=completion, model="vertex_ai/gemini-2.5-pro")
        assert not vertex._credentials_project_mapping
    assert litellm_handler._vertex_request_default_adc.get() is None


@pytest.mark.asyncio
async def test_vertex_absent_default_adc_does_not_adopt_a_later_file(monkeypatch, tmp_path):
    from google.auth import _default
    from google.oauth2 import credentials as user_credentials
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    handler = LiteLLMAIHandler()
    (tmp_path / "application_default_credentials.json").write_text(json.dumps({
        "type": "authorized_user", "client_id": "late-client", "client_secret": "late-secret",
        "refresh_token": "late-refresh",
    }))
    metadata_credentials = user_credentials.Credentials("managed-token")
    metadata_credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    discover = MagicMock(return_value=(metadata_credentials, "managed-project"))
    monkeypatch.setattr(_default, "_get_gce_credentials", discover)
    monkeypatch.setattr(user_credentials.Credentials, "refresh", lambda self, request: None)
    vertex = VertexBase()

    async def completion(**kwargs):
        return await vertex.get_access_token_async(kwargs["vertex_credentials"], None)

    assert await handler._acompletion(_completion=completion, model="vertex_ai/gemini-2.5-pro") == (
        "managed-token", "managed-project",
    )
    discover.assert_called_once()
    assert litellm_handler._vertex_request_default_adc.get() is None


@pytest.mark.parametrize("explicit_project", (None, "shared-project"))
@pytest.mark.parametrize("impersonate", (False, True))
@pytest.mark.parametrize("credential_source", ("explicit", "cloud_sdk", "cloud_sdk_no_project"))
@pytest.mark.asyncio
async def test_vertex_aws_wif_binds_signing_identity_across_cache_and_refresh(
    monkeypatch, tmp_path, explicit_project, impersonate, credential_source,
):
    from google.auth.transport import requests as google_requests
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({
        "VERTEXAI.VERTEX_PROJECT": explicit_project,
    }))
    info = _external_vertex_adc("123", "aws", None)
    impersonation_url = (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/test@example.com:generateAccessToken"
    )
    if impersonate:
        info["service_account_impersonation_url"] = impersonation_url
    if credential_source == "explicit":
        monkeypatch.setenv("VERTEXAI_CREDENTIALS", json.dumps(info))
    else:
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        (tmp_path / "application_default_credentials.json").write_text(json.dumps(info))
        if credential_source == "cloud_sdk":
            monkeypatch.setenv("CLOUDSDK_CORE_PROJECT", "shared-project")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    handlers = []
    for tenant in ("tenant-a", "tenant-b"):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", tenant)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", f"secret-{tenant}")
        monkeypatch.setenv("AWS_SESSION_TOKEN", f"session-{tenant}")
        handlers.append(LiteLLMAIHandler())

    signed_identities = []

    def request(url, method="GET", headers=None, body=None, **kwargs):
        if url == "https://sts.googleapis.com/v1/token":
            from urllib.parse import parse_qs

            form = parse_qs(body.decode() if isinstance(body, bytes) else body)
            subject = json.loads(unquote(form["subject_token"][0]))
            signed_headers = {item["key"].lower(): item["value"] for item in subject["headers"]}
            authorization = signed_headers["authorization"]
            tenant = authorization.split("Credential=", 1)[1].split("/", 1)[0]
            assert signed_headers["x-amz-security-token"] == f"session-{tenant}"
            signed_identities.append(tenant)
            response = {"access_token": f"token-{tenant}", "expires_in": 3600, "token_type": "Bearer"}
        elif url == impersonation_url:
            response = {
                "accessToken": headers["authorization"].removeprefix("Bearer "),
                "expireTime": (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        else:
            assert not explicit_project and credential_source != "cloud_sdk", "Project discovery is unnecessary"
            assert url == "https://cloudresourcemanager.googleapis.com/v1/projects/123"
            response = {"projectId": "shared-project"}
        return type("Response", (), {"status": 200, "data": json.dumps(response).encode()})()

    monkeypatch.setattr(google_requests, "Request", lambda: request)
    vertex = VertexBase()
    observed = []

    async def completion(**kwargs):
        token, project = await vertex.get_access_token_async(
            kwargs["vertex_credentials"], kwargs.get("vertex_project"),
        )
        assert project == "shared-project"
        observed.append(token)
        return _mock_response()

    for round_number in range(3):
        if round_number == 2:
            for credentials, _ in vertex._credentials_project_mapping.values():
                credentials.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        for handler in handlers:
            await handler.probe_completion("vertex_ai/gemini-2.5-pro", _completion=completion)
    assert observed == ["token-tenant-a", "token-tenant-b"] * 3
    assert set(signed_identities) == {"tenant-a", "tenant-b"}
    assert len({id(entry[0]) for entry in vertex._credentials_project_mapping.values()}) == 2


@pytest.mark.parametrize("source", ("explicit_wif", "cloud_sdk", "absent"))
def test_vertex_equivalent_source_snapshots_reuse_cache_identity(monkeypatch, tmp_path, source):
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    info = json.dumps(_external_vertex_adc("123", "aws", None))
    if source == "explicit_wif":
        monkeypatch.setenv("VERTEXAI_CREDENTIALS", info)
    elif source == "cloud_sdk":
        (tmp_path / "application_default_credentials.json").write_text(info)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "snapshot-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "snapshot-secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    snapshots = {
        LiteLLMAIHandler()._get_provider_request_params("vertex_ai/gemini-2.5-pro")["vertex_credentials"]
        for _ in range(20)
    }
    assert len(snapshots) == 1


@pytest.mark.parametrize("as_bytes", (False, True))
def test_vertex_aws_wif_metadata_supplier_keeps_source_and_refresh(monkeypatch, as_bytes):
    info = _external_vertex_adc("123", "aws", None)
    source = info["credential_source"]
    source["imdsv2_session_token_url"] = "http://169.254.169.254/latest/api/token"
    source["regional_cred_verification_url"] = "https://sts.{region}.amazonaws.com/custom-verification"
    credentials = litellm_handler._vertex_aws_credentials_from_snapshot(info, {}, ["scope-a"])
    credentials = credentials.with_scopes(["scope-b"]).with_quota_project("quota-project")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "late-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "late-secret")
    monkeypatch.setenv("AWS_REGION", "late-region")
    access_key = "metadata-access-a"
    requests_seen = []

    def request(url, method="GET", headers=None, **kwargs):
        requests_seen.append((url, method))
        if url == source["imdsv2_session_token_url"]:
            assert method == "PUT"
            assert headers == {"X-aws-ec2-metadata-token-ttl-seconds": "300"}
            body = "metadata-session"
        else:
            assert headers == {"X-aws-ec2-metadata-token": "metadata-session"}
            if url == source["region_url"]:
                body = "us-east-1b"
            elif url == source["url"]:
                body = "bound-role"
            else:
                assert url == source["url"] + "/bound-role"
                body = json.dumps({"AccessKeyId": access_key, "SecretAccessKey": "metadata-secret", "Token": "token"})
        return type("Response", (), {"status": 200, "data": body.encode() if as_bytes else body})()

    for access_key in ("metadata-access-a", "metadata-access-b"):
        subject = json.loads(unquote(credentials.retrieve_subject_token(request)))
        headers = {item["key"].lower(): item["value"] for item in subject["headers"]}
        assert f"Credential={access_key}/" in headers["authorization"]
        assert subject["url"] == "https://sts.us-east-1.amazonaws.com/custom-verification"
    assert requests_seen.count((source["url"] + "/bound-role", "GET")) == 2


def test_vertex_aws_wif_snapshot_preserves_native_source_validation():
    info = _external_vertex_adc("123", "aws", None)
    info["credential_source"]["environment_id"] = "aws2"
    with pytest.raises(ValueError, match="aws version"):
        litellm_handler._vertex_aws_credentials_from_snapshot(info, {}, ["scope"])


@pytest.mark.parametrize("kind", ("identity_pool", "aws", "explicit_aws"))
@pytest.mark.parametrize("use_async", (False, True))
@pytest.mark.parametrize("explicit_project", (None, "configured-project"))
@pytest.mark.asyncio
async def test_vertex_wif_snapshot_resolves_project_and_preserves_cache(
    monkeypatch, tmp_path, kind, use_async, explicit_project,
):
    import google.auth
    from google.auth import aws, external_account, identity_pool
    from google.auth.transport import requests as google_requests
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({
        "VERTEXAI.VERTEX_PROJECT": explicit_project,
    }))
    no_default = MagicMock(side_effect=AssertionError("Ambient ADC must not be loaded"))
    monkeypatch.setattr(google.auth, "default", no_default)
    path = tmp_path / "wif-adc.json"
    handlers = []
    snapshots = []
    for number in ("123", "456"):
        path.write_text(json.dumps(_external_vertex_adc(number, kind, None)))
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
        handler = LiteLLMAIHandler()
        handlers.append(handler)
        assert handler._vertex_gac_adc["info"] == _external_vertex_adc(number, kind, None)
        snapshots.append(handler._provider_request_params["vertex_ai"]["vertex_credentials"])
    path.unlink()
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "late-adc.json")
    monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", "late-quota")
    vertex = VertexBase()
    refreshed = []
    discovered = []

    def refresh(credentials, request):
        assert isinstance(credentials, external_account.Credentials)
        assert credentials.quota_project_id is None
        refreshed.append(credentials.project_number)
        credentials.token = f"token-{credentials.project_number}"
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    def request(url, method, headers, **kwargs):
        number = url.rsplit("/", 1)[-1]
        assert url == f"https://cloudresourcemanager.googleapis.com/v1/projects/{number}"
        assert number in ("123", "456") and method == "GET"
        assert headers["authorization"] == f"Bearer token-{number}"
        assert "x-goog-user-project" not in headers
        discovered.append(number)
        return type("Response", (), {"status": 200, "data": json.dumps({"projectId": f"project-{number}"}).encode()})()

    for credentials_type in (identity_pool.Credentials, aws.Credentials):
        monkeypatch.setattr(credentials_type, "refresh", refresh)
    monkeypatch.setattr(google_requests, "Request", lambda: request)
    observed = []

    async def completion(**kwargs):
        snapshot = kwargs["vertex_credentials"]
        project = kwargs.get("vertex_project")
        if use_async:
            token, resolved_project = await vertex.get_access_token_async(snapshot, project)
        else:
            token, resolved_project = vertex.get_access_token(snapshot, project)
        observed.append((snapshot, token, resolved_project))
        return _mock_response()

    for round_number in range(3):
        if round_number == 2:
            for credentials, _ in vertex._credentials_project_mapping.values():
                credentials.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        await asyncio.gather(*(
            handler.probe_completion("vertex_ai/gemini-2.5-pro", _completion=completion)
            for handler in handlers
        ))
    for snapshot, token, project in observed:
        number = "123" if snapshot == snapshots[0] else "456"
        assert snapshot in snapshots
        assert token == f"token-{number}"
        assert project == (explicit_project or f"project-{number}")
    assert len(observed) == 6
    assert sorted(discovered) == ([] if explicit_project else ["123", "456"])
    assert len({id(entry[0]) for entry in vertex._credentials_project_mapping.values()}) == 2
    assert len(refreshed) == (4 if explicit_project else 6)
    no_default.assert_not_called()
    assert litellm_handler._vertex_request_credentials.get() is None


@pytest.mark.parametrize("case", ("no_context", "other_type", "different_snapshot", "invalid_json", "explicit_project"))
def test_vertex_wif_bridge_delegates_unowned_or_explicit_requests(monkeypatch, case):
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    original = MagicMock(return_value=("original-credentials", "original-project"))

    def load_original(self, credentials, project_id):
        return original(self, credentials, project_id)

    monkeypatch.setattr(VertexBase, "load_auth", load_original)
    info = _external_vertex_adc("123", "identity_pool", None)
    context = None if case == "no_context" else dict(info)
    credentials = json.dumps(info)
    project = "explicit-project" if case == "explicit_project" else None
    if case == "other_type":
        context["type"] = "service_account"
    elif case == "different_snapshot":
        context["audience"] = "different-request"
    elif case == "invalid_json":
        credentials = "not-json"
    litellm_handler._install_vertex_wif_project_bridge()
    bridge = VertexBase.load_auth
    litellm_handler._install_vertex_wif_project_bridge()
    assert VertexBase.load_auth is bridge
    vertex = VertexBase()
    token = litellm_handler._vertex_request_credentials.set(context)
    try:
        assert vertex.load_auth(credentials, project) == ("original-credentials", "original-project")
    finally:
        litellm_handler._vertex_request_credentials.reset(token)
    original.assert_called_once_with(vertex, credentials, project)


@pytest.mark.parametrize("failure", ("missing", "wrong_type", "discovery", "refresh"))
@pytest.mark.asyncio
async def test_vertex_wif_failure_does_not_cache_or_fallback(monkeypatch, failure):
    import google.auth
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    info = _external_vertex_adc("123", "identity_pool", None)
    credentials = MagicMock()
    credentials.get_project_id.return_value = None if failure == "missing" else 123 if failure == "wrong_type" else "project"
    if failure == "discovery":
        credentials.get_project_id.side_effect = RuntimeError("discovery failed")
    vertex = VertexBase()
    monkeypatch.setattr(vertex, "_credentials_from_identity_pool", MagicMock(return_value=credentials))
    refresh = MagicMock(side_effect=RuntimeError("refresh failed") if failure == "refresh" else None)
    monkeypatch.setattr(vertex, "refresh_auth", refresh)
    no_default = MagicMock(side_effect=AssertionError("Ambient ADC must not be loaded"))
    monkeypatch.setattr(google.auth, "default", no_default)

    async def completion(**kwargs):
        return await vertex.get_access_token_async(kwargs["vertex_credentials"], None)

    expected = {"missing": ValueError, "wrong_type": TypeError, "discovery": RuntimeError, "refresh": RuntimeError}[failure]
    with pytest.raises(expected):
        await LiteLLMAIHandler()._acompletion(
            _completion=completion, model="vertex_ai/gemini-2.5-pro", vertex_credentials=json.dumps(info),
        )
    assert not vertex._credentials_project_mapping
    no_default.assert_not_called()
    assert litellm_handler._vertex_request_credentials.get() is None


@pytest.mark.asyncio
async def test_vertex_wif_cancelled_load_keeps_worker_context_isolated(monkeypatch):
    import threading

    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    info = _external_vertex_adc("123", "identity_pool", None)
    credentials = MagicMock()
    observed_context = []

    def discover(request):
        entered.set()
        assert release.wait(5)
        observed_context.append(litellm_handler._vertex_request_credentials.get())
        return "project"

    credentials.get_project_id.side_effect = discover
    vertex = VertexBase()
    monkeypatch.setattr(vertex, "_credentials_from_identity_pool", MagicMock(return_value=credentials))
    monkeypatch.setattr(vertex, "refresh_auth", MagicMock())

    def load_and_signal(credentials, project_id):
        try:
            return VertexBase.load_auth(vertex, credentials, project_id)
        finally:
            finished.set()

    monkeypatch.setattr(vertex, "load_auth", load_and_signal)

    async def completion(**kwargs):
        return await vertex.get_access_token_async(kwargs["vertex_credentials"], None)

    task = asyncio.create_task(LiteLLMAIHandler()._acompletion(
        _completion=completion, model="vertex_ai/gemini-2.5-pro", vertex_credentials=json.dumps(info),
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert litellm_handler._vertex_request_credentials.get() is None
        assert not vertex._credentials_project_mapping
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert observed_context == [info]


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


@pytest.mark.parametrize("initial_base", (None, "https://handler.example/v1"))
@pytest.mark.asyncio
async def test_image_wait_preserves_native_endpoint_isolation(monkeypatch, initial_base):
    from litellm.litellm_core_utils import logging_worker

    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({"GROQ.KEY": "handler-key"}))
    if initial_base:
        monkeypatch.setenv("GROQ_API_BASE", initial_base)
    captured = []

    def image_wait(*args, **kwargs):
        monkeypatch.setenv("GROQ_API_BASE", "https://another-handler.example/v1")
        return MagicMock(status_code=200)

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        captured.append(request)
        raise TransportReached

    # Run real LiteLLM dispatch/authentication; intercept only image I/O and HTTP transport.
    monkeypatch.setattr(litellm_handler.requests, "head", image_wait)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    handler = LiteLLMAIHandler()
    try:
        if initial_base:
            with pytest.raises(TransportReached):
                await handler.chat_completion(
                    model="groq/llama-3.3-70b-versatile", system="sys", user="usr",
                    img_path="https://image.example/test.png",
                )
            assert len(captured) == 1
            assert str(captured[0].url) == f"{initial_base}/chat/completions"
            assert captured[0].headers["authorization"] == "Bearer handler-key"
            assert json.loads(captured[0].content)["messages"][1]["content"][1] == {
                "type": "image_url", "image_url": {"url": "https://image.example/test.png"},
            }
        else:
            with pytest.raises(ValueError, match="Refusing live api_base environment fallback for provider groq"):
                await handler.chat_completion(
                    model="groq/llama-3.3-70b-versatile", system="sys", user="usr",
                    img_path="https://image.example/test.png",
                )
            assert not captured
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()


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


@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("route", ("global", "captured", "host"))
@pytest.mark.parametrize("timing", ("before", "after"))
@pytest.mark.asyncio
async def test_native_gdc_rejects_uncaptured_routing(monkeypatch, entrypoint, route, timing):
    from litellm.litellm_core_utils import logging_worker

    endpoint = "https://owned.example/v1/projects/owned/locations/local/chat/completions"
    monkeypatch.setenv("GDC_API_KEY", "owned-gdc-token")
    if route == "captured":
        monkeypatch.setenv("GDC_API_BASE", endpoint)
    elif route == "host":
        monkeypatch.setenv("GDC_API_BASE", "https://owned.example")
    def set_foreign_route():
        monkeypatch.setattr(
            litellm, "gdc_api_base",
            "https://foreign.example/v1/projects/foreign/locations/other/chat/completions",
        )
        monkeypatch.setattr(litellm, "vertex_project", "foreign")
        monkeypatch.setattr(litellm, "vertex_location", "other")

    if timing == "before":
        set_foreign_route()
    handler = LiteLLMAIHandler()
    if timing == "after":
        set_foreign_route()
    if route == "captured":
        monkeypatch.setenv("GDC_API_BASE", "https://changed.example")
    sent = []

    async def send(client, request, **kwargs):
        sent.append(request)
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "gemini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        async def invoke():
            if entrypoint == "chat":
                await handler.chat_completion("gdc/gemini", "sys", "usr")
            else:
                await handler.probe_completion("gdc/gemini")

        if route == "captured":
            await invoke()
            assert len(sent) == 1
            assert str(sent[0].url) == endpoint
            assert sent[0].headers["Authorization"] == "Bearer owned-gdc-token"
            assert sent[0].headers["x-goog-user-project"] == "projects/owned"
        else:
            with pytest.raises(ValueError, match="(?i)gdc|routing|process-wide"):
                await invoke()
            assert sent == []
    finally:
        await asyncio.sleep(0)
        try:
            await worker.flush()
        finally:
            await worker.stop()


@pytest.mark.parametrize("provider", ("mistral", "deepseek", "groq"))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("captured_key", (None, "owned-key", DUMMY_LITELLM_API_KEY))
@pytest.mark.parametrize(("header_name", "authorization"), (
    ("Authorization", "Bearer owned-header"), ("aUtHoRiZaTiOn", "Basic owned-header"),
))
@pytest.mark.asyncio
async def test_native_raw_guard_preserves_explicit_authorization(
    monkeypatch, provider, entrypoint, captured_key, header_name, authorization,
):
    from litellm.litellm_core_utils import logging_worker

    settings = _make_settings({
        f"{provider.upper()}.KEY": captured_key,
        "LITELLM.EXTRA_HEADERS": json.dumps({header_name: authorization}),
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "api_key", "foreign-key")
    sent = []

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        sent.append(request)
        raise TransportReached

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        with pytest.raises(TransportReached):
            if entrypoint == "chat":
                await handler.chat_completion(f"{provider}/test-model", "sys", "usr")
            else:
                await handler.probe_completion(f"{provider}/test-model")
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()
    assert len(sent) == 1
    if captured_key:
        # A real key retains native precedence, including a literal dummy key.
        assert f"Bearer {captured_key}" in sent[0].headers.get_list("authorization")
    else:
        assert sent[0].headers.get_list("authorization") == [authorization]
    assert "foreign-key" not in str(sent[0].headers)
    assert litellm_handler._raw_api_key_guard_provider.get() is None


@pytest.mark.parametrize("control", ("active", "no-header", "changed-key", "provider", "override", "outside"))
@pytest.mark.asyncio
async def test_raw_guard_bridge_boundaries(monkeypatch, control):
    from litellm.llms.openai.chat.gpt_transformation import OpenAIGPTConfig

    litellm_handler._install_raw_api_key_guard_bridge()

    class InheritedConfig(OpenAIGPTConfig):
        pass

    class OverrideConfig(OpenAIGPTConfig):
        def validate_environment(self, *args, **kwargs):
            return super().validate_environment(*args, **kwargs)

    config = OverrideConfig() if control == "override" else InheritedConfig()
    key = "native-key" if control == "changed-key" else DUMMY_LITELLM_API_KEY

    async def completion(**kwargs):
        assert "_raw_api_key_guard" not in kwargs
        return config.validate_environment(
            headers={} if control == "no-header" else {"authorization": "Basic owned"},
            model="test-model", messages=[], optional_params={},
            litellm_params={"custom_llm_provider": "deepseek" if control == "provider" else "mistral"},
            api_key=key,
        )

    result = await LiteLLMAIHandler()._acompletion(
        _completion=completion, _raw_api_key_guard=control != "outside", model="mistral/test-model",
        api_key=DUMMY_LITELLM_API_KEY, headers={"authorization": "Basic owned"},
    )
    if control == "active":
        assert result == {"authorization": "Basic owned", "Content-Type": "application/json"}
    else:
        assert result["Authorization"] == f"Bearer {key}"
    assert litellm_handler._raw_api_key_guard_provider.get() is None


@pytest.mark.asyncio
async def test_raw_guard_nested_and_concurrent_contexts(monkeypatch):
    from litellm.llms.openai.chat.gpt_transformation import OpenAIGPTConfig

    handler = LiteLLMAIHandler()
    both_started = asyncio.Event()
    started = []

    async def inner(**kwargs):
        assert litellm_handler._raw_api_key_guard_provider.get() is None
        return OpenAIGPTConfig().validate_environment(
            headers={"Authorization": "Basic inner"}, model="test-model", messages=[], optional_params={},
            litellm_params={"custom_llm_provider": "mistral"}, api_key=DUMMY_LITELLM_API_KEY,
        )

    async def outer(**kwargs):
        started.append(kwargs["headers"]["Authorization"])
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        assert litellm_handler._raw_api_key_guard_provider.get() == "mistral"
        result = await handler._acompletion(
            _completion=inner, model="mistral/test-model", api_key=DUMMY_LITELLM_API_KEY,
        )
        assert result["Authorization"] == f"Bearer {DUMMY_LITELLM_API_KEY}"
        assert litellm_handler._raw_api_key_guard_provider.get() == "mistral"
        return OpenAIGPTConfig().validate_environment(
            headers=dict(kwargs["headers"]), model="test-model", messages=[], optional_params={},
            litellm_params={"custom_llm_provider": "mistral"}, api_key=DUMMY_LITELLM_API_KEY,
        )

    results = await asyncio.gather(*(
        handler._acompletion(
            _completion=outer, _raw_api_key_guard=True, model="mistral/test-model",
            api_key=DUMMY_LITELLM_API_KEY, headers={"Authorization": value},
        )
        for value in ("Basic first", "Basic second")
    ))
    assert [result["Authorization"] for result in results] == ["Basic first", "Basic second"]
    assert litellm_handler._raw_api_key_guard_provider.get() is None


@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("source", ("settings", "environment"))
@pytest.mark.parametrize("complete_url", (False, True))
@pytest.mark.asyncio
async def test_native_gdc_captured_host_routing(monkeypatch, entrypoint, source, complete_url):
    from litellm.litellm_core_utils import logging_worker

    endpoint = "https://owned.example/v1/projects/owned/locations/local/chat/completions"
    monkeypatch.setenv("GDC_API_BASE", endpoint if complete_url else "https://owned.example")
    monkeypatch.setenv("GDC_API_KEY", "owned-token")
    project = "ignored" if complete_url else "owned"
    if source == "settings":
        settings = _make_settings({
            "VERTEXAI.VERTEX_PROJECT": project, "VERTEXAI.VERTEX_LOCATION": "local",
        })
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    else:
        monkeypatch.setenv("VERTEXAI_PROJECT", project)
        monkeypatch.setenv("VERTEX_LOCATION", "local")
    handler = LiteLLMAIHandler()
    sent, boundaries = [], []
    loop = asyncio.get_running_loop()
    executor = loop.run_in_executor

    def dispatch(pool, function, *args):
        if not boundaries:
            boundaries.append(function)
            monkeypatch.setattr(litellm, "vertex_project", "foreign")
            monkeypatch.setattr(litellm, "vertex_location", "other")
            monkeypatch.setattr(litellm, "gdc_api_base", "https://foreign.example")
        return executor(pool, function, *args)

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        sent.append(request)
        raise TransportReached

    monkeypatch.setattr(loop, "run_in_executor", dispatch)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        with pytest.raises(TransportReached):
            if entrypoint == "chat":
                await handler.chat_completion("gdc/gemini", "sys", "usr")
            else:
                await handler.probe_completion("gdc/gemini")
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()
    assert len(boundaries) == len(sent) == 1
    assert str(sent[0].url) == endpoint
    assert sent[0].headers["authorization"] == "Bearer owned-token"
    assert sent[0].headers["x-goog-user-project"] == "projects/owned"
    assert "vertex_" not in sent[0].content.decode()


@pytest.mark.parametrize(("base", "project", "location"), (
    (None, "owned", "local"), ("https://owned.example", None, "local"),
    ("https://owned.example", "owned", None), ("https://owned.example", "bad/project", "local"),
    ("https://owned.example", "owned", "bad/location"),
))
@pytest.mark.asyncio
async def test_gdc_invalid_host_routing_fails_before_auth(monkeypatch, base, project, location):
    from litellm.llms.gdc.chat.transformation import GDCGeminiConfig

    if base:
        monkeypatch.setenv("GDC_API_BASE", base)
    monkeypatch.setenv("GDC_API_KEY", json.dumps({"type": "gdch_service_account"}))
    settings = _make_settings({"VERTEXAI.VERTEX_PROJECT": project, "VERTEXAI.VERTEX_LOCATION": location})
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "gdc_api_base", "https://foreign.example")
    monkeypatch.setattr(litellm, "vertex_project", "foreign")
    monkeypatch.setattr(litellm, "vertex_location", "other")
    auth = MagicMock(side_effect=AssertionError("invalid routing must not resolve credentials"))
    monkeypatch.setattr(GDCGeminiConfig, "_load_creds_from_key", auth)
    with pytest.raises((ValueError, litellm.AuthenticationError)):
        await handler.probe_completion("gdc/gemini")
    auth.assert_not_called()


@pytest.mark.parametrize("configured", (None, "settings-location"))
@pytest.mark.asyncio
async def test_vertex_location_alias_precedence(monkeypatch, configured):
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    settings = _make_settings({"VERTEXAI.VERTEX_LOCATION": configured})
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    monkeypatch.setenv("VERTEXAI_LOCATION", "primary-location")
    monkeypatch.setenv("VERTEX_LOCATION", "secondary-location")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("VERTEXAI_LOCATION", "foreign-primary")
    monkeypatch.setenv("VERTEX_LOCATION", "foreign-secondary")
    params = await _call(handler, "vertex_ai/gemini-2.5-pro")
    expected = configured or "primary-location"
    assert VertexBase.get_vertex_ai_location(dict(params)) == expected
    assert VertexBase.safe_get_vertex_ai_location(params) == expected


@pytest.mark.asyncio
async def test_gdc_handlers_keep_separate_routing(monkeypatch):
    monkeypatch.setenv("GDC_API_BASE", "https://owned.example")
    monkeypatch.setenv("GDC_API_KEY", "owned-token")
    monkeypatch.setenv("VERTEXAI_PROJECT", "first")
    monkeypatch.setenv("VERTEXAI_LOCATION", "local")
    first = LiteLLMAIHandler()
    monkeypatch.setenv("VERTEXAI_PROJECT", "second")
    second = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "vertex_project", "foreign")
    for handler, project in ((first, "first"), (second, "second"), (first, "first")):
        params = await _call(handler, "gdc/gemini")
        assert params["api_base"] == f"https://owned.example/v1/projects/{project}/locations/local/chat/completions"
        assert "vertex_credentials" not in params


@pytest.mark.parametrize("complete_url", (False, True))
@pytest.mark.asyncio
async def test_native_gdc_service_account_host_routing(monkeypatch, complete_url):
    import requests
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from google.auth import jwt
    from litellm.litellm_core_utils import logging_worker

    private_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()
    endpoint = "https://owned.example/v1/projects/routing-project/locations/local/chat/completions"
    monkeypatch.setenv("GDC_API_BASE", endpoint if complete_url else "https://owned.example")
    monkeypatch.setenv("GDC_API_KEY", json.dumps({
        "type": "gdch_service_account", "format_version": "1", "private_key_id": "test-key",
        "private_key": private_key, "name": "test-service", "project": "identity-project",
        "token_uri": "https://identity.example/authenticate",
    }))
    settings = _make_settings({
        "VERTEXAI.VERTEX_PROJECT": "routing-project", "VERTEXAI.VERTEX_LOCATION": "local",
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    handler = LiteLLMAIHandler()
    exchanged, sent = [], []

    def token_send(session, request, **kwargs):
        assert request.url == "https://identity.example/authenticate"
        body = json.loads(request.body)
        assert body["audience"] == "https://owned.example"
        claims = jwt.decode(body["subject_token"], verify=False)
        assert claims["iss"] == "system:serviceaccount:identity-project:test-service"
        assert claims["aud"] == request.url
        exchanged.append(body)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"access_token": "exchanged-token", "expires_in": 3600}).encode()
        return response

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        sent.append(request)
        raise TransportReached

    monkeypatch.setattr(requests.Session, "send", token_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        with pytest.raises(TransportReached):
            await handler.probe_completion("gdc/gemini")
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()
    assert len(exchanged) == len(sent) == 1
    assert str(sent[0].url) == endpoint
    assert sent[0].headers["authorization"] == "Bearer exchanged-token"
    assert sent[0].headers["x-goog-user-project"] == "projects/routing-project"


@pytest.fixture
def header_only_native_auth(monkeypatch):
    from azure.core.pipeline.transport import RequestsTransport

    for name in (*litellm_handler.AZURE_OIDC_ENV_VARS, *litellm_handler.AZURE_OIDC_AUTH_ENV_VARS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", False)
    for name in ("callbacks", "success_callback", "failure_callback", "input_callback",
                 "_async_success_callback", "_async_failure_callback"):
        monkeypatch.setattr(litellm, name, [], raising=False)

    def unexpected_auth_http(*args, **kwargs):
        raise AssertionError("Unexpected Azure auth HTTP in header-only test")

    monkeypatch.setattr(RequestsTransport, "send", unexpected_auth_http)


@pytest.mark.parametrize("route", (
    "neosantara", "tensormesh", "parasail", "meta", "pinstripes",
    "xai", "ragflow", "ragflow_agent", "azure_ai", "azure_ai_key",
))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize("captured_key", (None, "owned-key", DUMMY_LITELLM_API_KEY))
@pytest.mark.parametrize(("header_name", "authorization"), (
    ("Authorization", "Bearer owned-header"), ("authorization", "Basic owned-header"),
    ("aUtHoRiZaTiOn", "Bearer owned-header"),
))
@pytest.mark.asyncio
async def test_native_guard_header_only_equivalence(
    monkeypatch, header_only_native_auth, route, entrypoint, captured_key, header_name, authorization,
):
    from litellm.litellm_core_utils import logging_worker

    if route.startswith("ragflow"):
        kind = "agent" if route == "ragflow_agent" else "chat"
        model = f"ragflow/{kind}/owned/test-model"
        base_variable, base, key_variable = "RAGFLOW_API_BASE", "https://owned.example", "RAGFLOW_API_KEY"
        endpoint = f"{base}/api/v1/{kind}s_openai/owned/chat/completions"
    elif route.startswith("azure_ai"):
        model = "azure_ai/test-model"
        base_variable, key_variable = "AZURE_AI_API_BASE", "AZURE_AI_API_KEY"
        base = "https://owned.services.ai.azure.com" if route == "azure_ai_key" else "https://owned.example"
        endpoint = base + ("/models/chat/completions" if route == "azure_ai_key" else "/chat/completions")
    elif route == "xai":
        model = "xai/test-model"
        base_variable, base, key_variable = "XAI_API_BASE", "https://owned.example/v1", "XAI_API_KEY"
        endpoint = base + "/chat/completions"
    else:
        config = litellm_handler.JSONProviderRegistry.get(route)
        model = f"{route}/responses/test-model"
        base_variable, base, key_variable = config.api_base_env, "https://owned.example/v1", config.api_key_env
        endpoint = base + "/responses"
    monkeypatch.setenv(base_variable, base)
    if captured_key:
        monkeypatch.setenv(key_variable, captured_key)
    settings = _make_settings({"LITELLM.EXTRA_HEADERS": json.dumps({header_name: authorization})})
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    handler = LiteLLMAIHandler()
    snapshot = dict(handler._request_headers)
    # Unlike api_key, the base handler did not forward this unrelated key.
    monkeypatch.setattr(litellm, "openai_key", "foreign-key")
    sent = []

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        sent.append(request)
        raise TransportReached

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        # Keep native dispatch/authentication as the oracle; stop only at HTTP.
        with pytest.raises(TransportReached):
            await litellm.acompletion(
                model=model, messages=[{"role": "user", "content": "usr"}],
                extra_headers={header_name: authorization},
            )
        with pytest.raises(TransportReached):
            if entrypoint == "chat":
                await handler.chat_completion(model, "sys", "usr")
            else:
                await handler.probe_completion(model)
    finally:
        try:
            await asyncio.sleep(0)
            await worker.flush()
        finally:
            await worker.stop()
    assert len(sent) == 2
    for request in sent:
        assert str(request.url) == endpoint
        assert json.loads(request.content)["model"] == "test-model"
        assert "foreign-key" not in str(request.headers)
    for name in ("authorization", "api-key"):
        assert sent[1].headers.get_list(name) == sent[0].headers.get_list(name)
    if captured_key is None:
        assert sent[1].headers.get_list("authorization") == [authorization]
        assert sent[1].headers.get_list("api-key") == []
    assert handler._request_headers == snapshot
    assert litellm_handler._raw_api_key_guard_provider.get() is None
    assert litellm_handler._raw_api_key_guard_auth.get() is None


def test_json_responses_native_config_contract():
    from litellm.llms.openai_like.dynamic_config import create_responses_config_class
    from litellm.utils import ProviderConfigManager

    registry = litellm_handler.JSONProviderRegistry
    providers = {name for name in registry.list_providers() if registry.supports_responses_api(name)}
    assert providers == {"neosantara", "tensormesh", "parasail", "empiriolabs", "meta", "pinstripes"}
    # Pinned native HTTP client selection rejects this registry-only provider
    # before authentication; do not claim transport coverage for that path.
    assert providers - {provider.value for provider in litellm.LlmProviders} == {"empiriolabs"}
    for provider in providers:
        # The native manager checks Python overrides before the JSON registry.
        selected = ProviderConfigManager.get_provider_responses_api_config(provider, model="test-model")
        expected = create_responses_config_class(registry.get(provider))
        assert type(selected) is expected
        assert type(selected).validate_environment is expected.validate_environment


@pytest.mark.parametrize("control", (
    "active", "initial-generic", "initial-empty", "no-guard", "changed-key", "multiple",
    "chat", "non-json", "no-header",
))
@pytest.mark.asyncio
async def test_json_guard_normalization_boundaries(monkeypatch, header_only_native_auth, control):
    if control in ("initial-generic", "initial-empty"):
        monkeypatch.setattr(litellm, "api_key", "initial-key" if control == "initial-generic" else "")
    handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "api_key", "late-key")
    headers = {} if control == "no-header" else {"authorization": "Basic owned"}
    if control == "multiple":
        headers["AUTHORIZATION"] = "Basic duplicate"
    initial_headers = dict(headers)
    model = "parasail/responses/test-model"
    if control == "chat":
        model = "parasail/test-model"
    elif control == "non-json":
        model = "openai/responses/test-model"

    async def completion(**kwargs):
        assert "_raw_api_key_guard" not in kwargs
        return kwargs["headers"]

    result = await handler._acompletion(
        _completion=completion, _raw_api_key_guard=control != "no-guard", model=model,
        api_key="real-key" if control == "changed-key" else DUMMY_LITELLM_API_KEY, headers=headers,
    )
    assert result == ({"Authorization": "Basic owned"} if control in ("active", "initial-empty") else initial_headers)
    assert headers == initial_headers


@pytest.mark.parametrize("raise_error", (False, True))
@pytest.mark.asyncio
async def test_raw_guard_auth_snapshot_nested_concurrency(monkeypatch, header_only_native_auth, raise_error):
    first = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "api_key", "initial-key")
    second = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "api_key", "late-key")
    started = []
    both_started = asyncio.Event()

    class ExpectedError(Exception):
        pass

    async def inner(**kwargs):
        assert litellm_handler._raw_api_key_guard_auth.get() is None
        assert litellm_handler._raw_api_key_guard_provider.get() is None

    async def invoke(handler, expected):
        async def completion(**kwargs):
            state = litellm_handler._raw_api_key_guard_auth.get()
            assert state["generic_key"] is expected
            started.append(expected)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            await handler._acompletion(_completion=inner, model="xai/test-model", api_key=DUMMY_LITELLM_API_KEY)
            assert litellm_handler._raw_api_key_guard_auth.get() is state
            if raise_error:
                raise ExpectedError
            return state["generic_key"]

        try:
            return await handler._acompletion(
                _completion=completion, _raw_api_key_guard=True, model="xai/test-model",
                api_key=DUMMY_LITELLM_API_KEY, headers={"Authorization": "Basic owned"},
            )
        except ExpectedError:
            return expected
        finally:
            # Check cleanup inside the owning task, not only its parent.
            assert litellm_handler._raw_api_key_guard_auth.get() is None
            assert litellm_handler._raw_api_key_guard_provider.get() is None

    assert await asyncio.gather(invoke(first, False), invoke(second, True)) == [False, True]


@pytest.mark.parametrize("provider", ("ragflow", "xai", "azure_ai"))
@pytest.mark.parametrize("control", (
    "active", "initial-generic", "changed-key", "no-guard", "provider-mismatch", "subclass", "multiple",
))
@pytest.mark.asyncio
async def test_raw_override_guard_boundaries(monkeypatch, header_only_native_auth, provider, control):
    from functools import wraps

    from litellm.llms.azure_ai.chat.transformation import AzureAIStudioConfig
    from litellm.llms.ragflow.chat.transformation import RAGFlowConfig
    from litellm.llms.xai.chat.transformation import XAIChatConfig

    config = {"azure_ai": AzureAIStudioConfig, "ragflow": RAGFlowConfig, "xai": XAIChatConfig}[provider]
    native = config.validate_environment
    signature = inspect.signature(native)
    observed_keys = []

    @wraps(native)
    def tracked(self, *args, **kwargs):
        observed_keys.append(signature.bind(self, *args, **kwargs).arguments["api_key"])
        return native(self, *args, **kwargs)

    monkeypatch.setattr(config, "validate_environment", tracked)
    if control == "initial-generic":
        monkeypatch.setattr(litellm, "api_key", "initial-key")
    handler = LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "api_key", "late-key")
    headers = {"authorization": "Basic owned-header"}
    if control == "multiple":
        headers["AUTHORIZATION"] = "Basic duplicate"
    initial_headers = dict(headers)
    model = "ragflow/chat/owned/test-model" if provider == "ragflow" else f"{provider}/test-model"
    key = "changed-key" if control == "changed-key" else DUMMY_LITELLM_API_KEY
    results = []

    async def completion(**kwargs):
        assert "_raw_api_key_guard" not in kwargs
        selected = type("DerivedConfig", (config,), {}) if control == "subclass" else config
        params = {"custom_llm_provider": "other" if control == "provider-mismatch" else provider}
        results.append(selected().validate_environment(
            headers=kwargs["headers"], model=model, messages=[], optional_params={},
            litellm_params=params, api_key=kwargs["api_key"], api_base="https://owned.example",
        ))

    await handler._acompletion(
        _completion=completion, _raw_api_key_guard=control != "no-guard",
        model=model, api_key=key, headers=headers,
    )
    assert observed_keys == [key]
    if control == "active":
        assert httpx.Headers(results[0]).get_list("authorization") == ["Basic owned-header"]
        assert headers == initial_headers
    else:
        assert f"Bearer {key}" in httpx.Headers(results[0]).get_list("authorization")
    litellm_handler._install_raw_api_key_guard_override_bridge(provider)
    installed = config.validate_environment
    litellm_handler._install_raw_api_key_guard_override_bridge(provider)
    assert config.validate_environment is installed
    assert litellm_handler._raw_api_key_guard_provider.get() is None
    assert litellm_handler._raw_api_key_guard_auth.get() is None


@pytest.mark.parametrize(("provider", "environment", "params", "initial_global", "refresh", "expected"), (
    ("ragflow", {}, {}, None, False, True),
    ("xai", {}, {}, None, False, True),
    ("xai", {}, {"use_xai_oauth": True}, None, False, False),
    ("xai", {}, {"use_xai_oauth": "false"}, None, False, False),
    ("xai", {}, {}, "xai_key", False, False),
    ("azure_ai", {}, {}, None, False, True),
    ("azure_ai", {}, {}, "azure_key", False, False),
    ("azure_ai", {}, {}, None, True, False),
    ("azure_ai", {"AZURE_AD_TOKEN": "initial-token"}, {}, None, False, False),
    ("azure_ai", {"AZURE_OPENAI_AD_TOKEN": "sdk-only-alias"}, {}, None, False, True),
    ("azure_ai", {"AZURE_API_KEY": "initial-key"}, {}, None, False, False),
    ("azure_ai", {"AZURE_OPENAI_API_KEY": "initial-key"}, {}, None, False, False),
    ("azure_ai", {}, {"azure_ad_token": "request-token"}, None, False, False),
    ("azure_ai", {}, {"azure_ad_token_provider": object()}, None, False, False),
    ("azure_ai", {"AZURE_TENANT_ID": "tenant"}, {}, None, False, True),
    ("azure_ai", {"AZURE_CLIENT_ID": "client"}, {}, None, False, True),
    ("azure_ai", {"AZURE_TENANT_ID": "tenant", "AZURE_CLIENT_ID": "client"},
     {"client_secret": "secret"}, None, False, False),
    ("azure_ai", {"AZURE_CLIENT_SECRET": "secret"},
     {"tenant_id": "tenant", "client_id": "client"}, None, False, False),
    ("azure_ai", {"AZURE_USERNAME": "user", "AZURE_PASSWORD": "password"},
     {"client_id": "client"}, None, False, False),
    ("azure_ai", {"AZURE_CLIENT_ID": "client"},
     {"azure_username": "user", "azure_password": "password"}, None, False, False),
))
def test_raw_guard_initial_auth_selection(
    monkeypatch, header_only_native_auth, provider, environment, params, initial_global, refresh, expected,
):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    if initial_global:
        monkeypatch.setattr(litellm, initial_global, "initial-key")
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", refresh)
    handler = LiteLLMAIHandler()
    for name in environment:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(litellm, "enable_azure_ad_token_refresh", not refresh)
    token = litellm_handler._raw_api_key_guard_auth.set(handler._raw_guard_auth_snapshot)
    try:
        assert bool(litellm_handler._raw_guard_has_header_only_auth(
            provider, params, {"Authorization": "Basic owned"},
        )) is expected
        if provider == "azure_ai":
            for name in ("api-key", "API-KEY"):
                assert not litellm_handler._raw_guard_has_header_only_auth(
                    provider, params, {"Authorization": "Basic owned", name: ""},
                )
    finally:
        litellm_handler._raw_api_key_guard_auth.reset(token)


@pytest.fixture
async def native_endpoint_runtime(monkeypatch):
    from litellm.caching.llm_caching_handler import LLMClientCache
    from litellm.litellm_core_utils import logging_worker

    cache = LLMClientCache()
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", cache)
    monkeypatch.setenv("LITELLM_RUST", "false")
    for name in ("callbacks", "input_callback", "success_callback", "failure_callback",
                 "_async_success_callback", "_async_failure_callback"):
        monkeypatch.setattr(litellm, name, [])

    def deny_sync(*args, **kwargs):
        raise AssertionError("Unexpected synchronous HTTP")

    monkeypatch.setattr(httpx.Client, "send", deny_sync)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        yield
    finally:
        await asyncio.sleep(0)
        try:
            await worker.flush()
        finally:
            await worker.stop()
            for client in {id(value): value for value in cache.cache_dict.values()}.values():
                result = client.close()
                if inspect.isawaitable(result):
                    await result


@pytest.mark.usefixtures("native_endpoint_runtime")
@pytest.mark.parametrize(("model", "ignored", "effective", "default_url"), (
    ("mistral/mistral-small-latest", "MISTRAL_API_BASE", "MISTRAL_AZURE_API_BASE",
     "https://api.mistral.ai/v1/chat/completions"),
    ("baseten/Qwen3-Coder", "BASETEN_API_BASE", None,
     "https://inference.baseten.co/v1/chat/completions"),
    ("baseten/abc12345", "BASETEN_API_BASE", None,
     "https://model-abc12345.api.baseten.co/environments/production/sync/v1/chat/completions"),
    ("volcengine/doubao-pro-32k", "ARK_API_BASE", "VOLCENGINE_API_BASE",
     "https://ark.cn-beijing.volces.com/api/v3/chat/completions"),
    ("volcengine/responses/doubao-pro-32k", "ARK_API_BASE", "VOLCENGINE_API_BASE",
     "https://ark.cn-beijing.volces.com/api/v3/responses"),
))
@pytest.mark.parametrize("selection", ("absent", "ignored", "effective", "both", "ignored_late"))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.asyncio
async def test_native_initial_endpoint(monkeypatch, model, ignored, effective, default_url, selection, entrypoint):
    if effective is None and selection in ("effective", "both"):
        pytest.skip("No separate effective environment endpoint for this native provider")
    provider = model.split("/", 1)[0]
    monkeypatch.setenv(provider.upper() + "_API_KEY", "owned-key")
    if selection in ("ignored", "both"):
        monkeypatch.setenv(ignored, "https://ignored.example/v1")
    if selection in ("effective", "both"):
        monkeypatch.setenv(effective, "https://effective.example/v1")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings())
    seen = []

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        seen.append(request)
        raise TransportReached

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with pytest.raises(TransportReached):
        await litellm.acompletion(model=model, messages=[{"role": "user", "content": "usr"}])
    suffix = "/api/v3/responses" if default_url.endswith("/responses") else "/chat/completions"
    expected = "https://effective.example/v1" + suffix if selection in ("effective", "both") else default_url
    assert str(seen[0].url) == expected
    assert seen[0].headers["Authorization"] == "Bearer owned-key"
    handler = LiteLLMAIHandler()
    if selection == "ignored_late":
        monkeypatch.setenv(ignored, "https://ignored.example/v1")
    with pytest.raises(TransportReached):
        if entrypoint == "chat":
            await handler.chat_completion(model, "sys", "usr")
        else:
            await handler.probe_completion(model)
    assert len(seen) == 2
    assert str(seen[1].url) == str(seen[0].url)
    for header in ("Authorization", "api-key"):
        assert seen[1].headers.get_list(header) == seen[0].headers.get_list(header)


@pytest.mark.usefixtures("native_endpoint_runtime")
@pytest.mark.parametrize("selection", ("absent", "ignored", "effective", "both", "ignored_late"))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.asyncio
async def test_native_vertex_initial_endpoint(monkeypatch, tmp_path, selection, entrypoint):
    import importlib

    import google.auth
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import VertexLLM

    credentials = Credentials("owned-token")
    monkeypatch.setattr(credentials, "refresh", lambda request: None)
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    monkeypatch.setattr(google.auth, "default", lambda *args, **kwargs: (credentials, "owned-project"))
    monkeypatch.setattr(litellm_handler, "_load_vertex_default_adc", lambda snapshot, project: (credentials, project))
    monkeypatch.setattr(importlib.import_module("litellm.main"), "vertex_chat_completion", VertexLLM())

    class UnexpectedAuthHTTP(BaseException):
        pass

    def deny_auth(*args, **kwargs):
        raise UnexpectedAuthHTTP("Unexpected Google authentication HTTP")

    monkeypatch.setattr(Request, "__call__", deny_auth)
    monkeypatch.setenv("VERTEXAI_PROJECT", "owned-project")
    monkeypatch.setenv("VERTEXAI_LOCATION", "us-central1")
    if selection in ("ignored", "both"):
        monkeypatch.setenv("VERTEX_API_BASE", "https://ignored.example")
    if selection in ("effective", "both"):
        monkeypatch.setenv("VERTEXAI_API_BASE", "https://effective.example")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings())
    seen = []

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        seen.append(request)
        raise TransportReached

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    model = "vertex_ai/gemini-2.5-flash"
    with pytest.raises(TransportReached):
        await litellm.acompletion(model=model, messages=[{"role": "user", "content": "usr"}])
    host = "effective.example" if selection in ("effective", "both") else "us-central1-aiplatform.googleapis.com"
    assert seen[0].url.host == host
    assert seen[0].url.path == (
        "/v1/projects/owned-project/locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent"
    )
    assert seen[0].headers["Authorization"] == "Bearer owned-token"
    handler = LiteLLMAIHandler()
    if selection == "ignored_late":
        monkeypatch.setenv("VERTEX_API_BASE", "https://ignored.example")
    with pytest.raises(TransportReached):
        if entrypoint == "chat":
            await handler.chat_completion(model, "sys", "usr")
        else:
            await handler.probe_completion(model)
    assert len(seen) == 2
    assert seen[1].url == seen[0].url
    assert seen[1].headers.get_list("Authorization") == seen[0].headers.get_list("Authorization")


@pytest.mark.parametrize(("route", "resource"), (
    ("", "foundation-model/anthropic.claude-3-haiku-20240307-v1:0"),
    ("invoke/", "foundation-model/anthropic.claude-3-haiku-20240307-v1:0"),
) + tuple(
    (prefix + spec + "/", "imported-model/example")
    for prefix in ("", "invoke/")
    for spec in ("llama", "deepseek_r1", "openai", "qwen2", "qwen3", "moonshot", "nova-2", "nova")
))
@pytest.mark.usefixtures("native_endpoint_runtime")
@pytest.mark.asyncio
async def test_arn_routing_region(monkeypatch, route, resource):
    for name in ("AWS_REGION_NAME", "AWS_REGION", "AWS_DEFAULT_REGION", "DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    credentials = {
        "aws_access_key_id": "owned-key", "aws_secret_access_key": "owned-secret",
        "aws_session_token": "owned-session",
    }
    for key, value in credentials.items():
        monkeypatch.setenv(key.upper(), value)
    monkeypatch.setenv("AWS_USE_IMDS", "false")
    settings = _make_settings()
    # Imported OpenAI reasoning models reject the default temperature parameter.
    settings.config.custom_reasoning_model = route == "invoke/openai/"
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    seen = []

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        seen.append(request)
        raise TransportReached

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    handler = LiteLLMAIHandler()
    original_credentials = dict(handler._aws_active_creds)
    for region in ("eu-west-1", "us-east-1"):
        account = "" if resource.startswith("foundation-model/") else "123456789012"
        model = f"bedrock/{route}arn:aws:bedrock:{region}:{account}:{resource}"
        with pytest.raises(TransportReached):
            await litellm.acompletion(model=model, messages=[{"role": "user", "content": "usr"}], **credentials)
        native = seen[-1]
        assert native.url.host == f"bedrock-runtime.{region}.amazonaws.com"
        assert native.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "Credential=owned-key/" in native.headers["Authorization"]
        assert f"/{region}/bedrock/aws4_request" in native.headers["Authorization"]
        assert native.headers["X-Amz-Security-Token"] == "owned-session"
        with pytest.raises(TransportReached):
            await handler.chat_completion(model, "sys", "usr")
        actual = seen[-1]
        assert actual.url == native.url
        assert actual.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert f"/{region}/bedrock/aws4_request" in actual.headers["Authorization"]
        assert "Credential=owned-key/" in actual.headers["Authorization"]
        assert actual.headers["X-Amz-Security-Token"] == "owned-session"
        assert handler._aws_active_creds == original_credentials
    assert len(seen) == 4


@pytest.mark.parametrize("auth", ("sigv4", "late_foreign", "captured_bearer"))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.usefixtures("native_endpoint_runtime")
@pytest.mark.asyncio
async def test_mantle_http_auth(monkeypatch, auth, entrypoint):
    settings = _make_settings({
        "aws.AWS_ACCESS_KEY_ID": "owned-key", "aws.AWS_SECRET_ACCESS_KEY": "owned-secret",
        "aws.AWS_SESSION_TOKEN": "owned-session", "aws.AWS_REGION_NAME": "us-east-1",
    })
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    if auth == "captured_bearer":
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "owned-bearer")
    handler = LiteLLMAIHandler()
    if auth != "sigv4":
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "foreign-bearer")
        monkeypatch.setattr(litellm, "api_key", "foreign-global")
    seen = []

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        seen.append(request)
        raise TransportReached

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    model = "bedrock_mantle/openai.gpt-oss-120b"
    with pytest.raises(TransportReached):
        if entrypoint == "chat":
            await handler.chat_completion(model, "sys", "usr")
        else:
            await handler.probe_completion(model)
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions"
    authorization = request.headers["Authorization"]
    if auth == "captured_bearer":
        assert authorization == "Bearer owned-bearer"
    else:
        assert authorization.startswith("AWS4-HMAC-SHA256 ")
        assert "Credential=owned-key/" in authorization
        assert "/us-east-1/bedrock/aws4_request" in authorization
        assert request.headers["X-Amz-Security-Token"] == "owned-session"
    assert "foreign" not in str(request.headers)
    assert litellm_handler.DUMMY_LITELLM_API_KEY not in authorization
    import json

    body = json.loads(request.content)
    assert not set(litellm_handler.BEDROCK_MANTLE_REQUEST_BODY_EXCLUDED_KEYS).intersection(body)
    assert all(secret.encode() not in request.content for secret in ("owned-secret", "owned-session", "foreign"))
    assert litellm_handler._bedrock_mantle_request_credentials.get() is None
    assert litellm_handler._bedrock_mantle_block_bearer.get() is False


@pytest.mark.usefixtures("native_endpoint_runtime")
@pytest.mark.parametrize("auth", ("sigv4", "bearer", "bearer_only"))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.parametrize(("selection", "environment", "expected_region", "expected_base"), (
    ("absent", {}, "us-east-1", None),
    ("default", {"AWS_DEFAULT_REGION": "eu-west-1"}, "us-east-1", None),
    ("default_late", {}, "us-east-1", None),
    ("region_late", {}, "us-east-1", None),
    ("region_name_late", {}, "us-east-1", None),
    ("mantle_late", {}, "us-east-1", None),
    ("region", {"AWS_DEFAULT_REGION": "eu-west-1", "AWS_REGION": "us-west-2"}, "us-west-2", None),
    ("region_name", {"AWS_REGION": "us-west-2", "AWS_REGION_NAME": "eu-west-1"}, "eu-west-1", None),
    ("mantle", {"AWS_REGION_NAME": "eu-west-1", "BEDROCK_MANTLE_REGION": "us-east-1"}, "us-east-1", None),
    ("settings", {"AWS_DEFAULT_REGION": "eu-west-1"}, "us-west-2", None),
    ("host", {"BEDROCK_MANTLE_API_BASE": "https://bedrock-mantle.eu-west-1.api.aws/v1"},
     "eu-west-1", "https://bedrock-mantle.eu-west-1.api.aws/v1"),
    ("custom", {"BEDROCK_MANTLE_API_BASE": "https://mantle.example/v1", "AWS_REGION": "us-west-2"},
     "us-west-2", "https://mantle.example/v1"),
))
@pytest.mark.asyncio
async def test_native_mantle_region(monkeypatch, auth, entrypoint, selection, environment, expected_region, expected_base):
    for name in ("AWS_REGION_NAME", "AWS_REGION", "AWS_DEFAULT_REGION", "DEFAULT_REGION", "BEDROCK_MANTLE_REGION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_USE_IMDS", "false")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    credentials = {"aws_access_key_id": "owned-key", "aws_secret_access_key": "owned-secret",
                   "aws_session_token": "owned-session"}
    if auth != "bearer_only":
        for name, value in credentials.items():
            monkeypatch.setenv(name.upper(), value)
    native_params = dict(credentials)
    if auth != "sigv4":
        monkeypatch.setenv("BEDROCK_MANTLE_API_KEY", "owned-bearer")
        native_params = {"api_key": "owned-bearer"}
    settings = _make_settings({"aws.AWS_REGION_NAME": expected_region} if selection == "settings" else {})
    if selection == "settings":
        # The core deliberately supports settings-only region for Mantle.
        native_params["aws_region_name"] = expected_region
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    seen = []

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        seen.append(request)
        raise TransportReached

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    model = "bedrock_mantle/openai.gpt-oss-120b"
    with pytest.raises(TransportReached):
        await litellm.acompletion(model=model, messages=[{"role": "user", "content": "usr"}], **native_params)
    base = expected_base or f"https://bedrock-mantle.{expected_region}.api.aws/v1"
    assert str(seen[0].url) == base + "/chat/completions"

    def assert_auth(request):
        if auth != "sigv4":
            assert request.headers["Authorization"] == "Bearer owned-bearer"
        else:
            assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
            assert "Credential=owned-key/" in request.headers["Authorization"]
            assert f"/{expected_region}/bedrock/aws4_request" in request.headers["Authorization"]
            assert request.headers["X-Amz-Security-Token"] == "owned-session"

    assert_auth(seen[0])
    handler = LiteLLMAIHandler()
    captured = dict(handler._aws_active_creds)
    late_variable = {
        "default_late": "AWS_DEFAULT_REGION", "region_late": "AWS_REGION",
        "region_name_late": "AWS_REGION_NAME", "mantle_late": "BEDROCK_MANTLE_REGION",
    }.get(selection)
    if late_variable:
        monkeypatch.setenv(late_variable, "eu-west-1")
    with pytest.raises(TransportReached):
        if entrypoint == "chat":
            await handler.chat_completion(model, "sys", "usr")
        else:
            await handler.probe_completion(model)
    assert len(seen) == 2
    assert seen[1].url == seen[0].url
    assert_auth(seen[1])
    assert handler._aws_active_creds == captured


@pytest.mark.usefixtures("native_endpoint_runtime")
@pytest.mark.parametrize(("model", "selection"), (
    ("volcengine/doubao-pro-32k", "volcengine"),
    ("volcengine/doubao-pro-32k", "both"),
    ("volcengine/responses/doubao-pro-32k", "volcengine"),
    ("volcengine/responses/doubao-pro-32k", "both"),
    ("volcengine/responses/doubao-pro-32k", "ark"),
    ("volcengine/responses/doubao-pro-32k", "ark_gateway"),
    ("volcengine/responses/doubao-pro-32k", "gateway_late"),
))
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.asyncio
async def test_native_volcengine_key_precedence(monkeypatch, model, selection, entrypoint):
    if selection in ("volcengine", "both"):
        monkeypatch.setenv("VOLCENGINE_API_KEY", "owned-volcengine-key")
    if selection in ("both", "ark", "ark_gateway"):
        monkeypatch.setenv("ARK_API_KEY", "owned-ark-key")
    settings = _make_settings({
        "OPENAI.KEY": "gateway-key", "OPENAI.API_BASE": "https://gateway.example/v1",
    } if selection in ("ark_gateway", "gateway_late") else {})
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    seen = []

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        seen.append(request)
        raise TransportReached

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    native_params = (
        {"api_key": "gateway-key", "api_base": "https://gateway.example/v1"}
        if selection == "gateway_late" else {}
    )
    with pytest.raises(TransportReached):
        await litellm.acompletion(model=model, messages=[{"role": "user", "content": "usr"}], **native_params)
    expected = "owned-volcengine-key" if selection in ("volcengine", "both") else "owned-ark-key"
    if selection == "gateway_late":
        expected = "gateway-key"
    assert seen[0].headers["Authorization"] == f"Bearer {expected}"
    assert seen[0].url.host == ("gateway.example" if selection == "gateway_late" else "ark.cn-beijing.volces.com")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("VOLCENGINE_API_KEY", "foreign-volcengine-key")
    monkeypatch.setenv("ARK_API_KEY", "foreign-ark-key")
    with pytest.raises(TransportReached):
        if entrypoint == "chat":
            await handler.chat_completion(model, "sys", "usr")
        else:
            await handler.probe_completion(model)
    assert len(seen) == 2
    assert seen[1].url == seen[0].url
    assert seen[1].headers.get_list("Authorization") == seen[0].headers.get_list("Authorization")


@pytest.mark.usefixtures("native_endpoint_runtime")
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.asyncio
async def test_volcengine_responses_rejects_late_ark_key(monkeypatch, entrypoint):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings())
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("ARK_API_KEY", "foreign-ark-key")
    seen = []

    async def send(client, request, **kwargs):
        seen.append(request)
        raise AssertionError("A late ARK key must not reach HTTP transport")

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with pytest.raises(ValueError, match="Refusing Volcengine ARK API key added after handler initialization"):
        if entrypoint == "chat":
            await handler.chat_completion("volcengine/responses/doubao-pro-32k", "sys", "usr")
        else:
            await handler.probe_completion("volcengine/responses/doubao-pro-32k")
    assert not seen


@pytest.mark.usefixtures("native_endpoint_runtime")
@pytest.mark.parametrize("entrypoint", ("chat", "probe"))
@pytest.mark.asyncio
async def test_volcengine_chat_ignores_ark_only_key(monkeypatch, entrypoint):
    monkeypatch.setenv("ARK_API_KEY", "owned-ark-key")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings())
    seen = []

    async def send(client, request, **kwargs):
        seen.append(request)
        raise AssertionError("Ordinary chat must not authenticate with ARK_API_KEY")

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    model = "volcengine/doubao-pro-32k"
    with pytest.raises(litellm.InternalServerError, match="Missing credentials"):
        await litellm.acompletion(model=model, messages=[{"role": "user", "content": "usr"}])
    handler = LiteLLMAIHandler()
    assert "api_key" not in handler._get_provider_request_params(model)
    with pytest.raises(litellm.InternalServerError, match="Missing credentials"):
        if entrypoint == "chat":
            await handler.chat_completion(model, "sys", "usr")
        else:
            await handler.probe_completion(model)
    assert not seen


@pytest.mark.usefixtures("native_endpoint_runtime")
@pytest.mark.parametrize("route", ("converse", "invoke", "implicit_invoke"))
@pytest.mark.parametrize("model_source", ("plain", "model_id", "model_arn", "region_path"))
@pytest.mark.parametrize("region_source", (
    "AWS_REGION_NAME", "AWS_REGION", "AWS_DEFAULT_REGION", "settings", "static", "imds",
))
@pytest.mark.parametrize("auth", ("sigv4", "bearer"))
@pytest.mark.asyncio
async def test_native_bedrock_model_region_precedence(monkeypatch, model_source, region_source, auth, route):
    if route != "converse" and model_source == "region_path":
        pytest.skip("Native region/model syntax is only supported by Converse")
    native_model = (
        "mistral.mistral-7b-instruct-v0:2" if route == "implicit_invoke"
        else "anthropic.claude-3-haiku-20240307-v1:0"
    )
    arn = f"arn:aws:bedrock:eu-west-1:123456789012:inference-profile/{native_model}"
    model = f"bedrock/{native_model}"
    overrides = {"litellm.model_id": arn} if model_source == "model_id" else {}
    if model_source == "model_arn":
        model = f"bedrock/{arn}"
    elif model_source == "region_path":
        model = f"bedrock/eu-west-1/{native_model}"
    if route == "invoke":
        model = model.replace("bedrock/", "bedrock/invoke/", 1)
    for name in ("AWS_REGION_NAME", "AWS_REGION", "AWS_DEFAULT_REGION", "DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_USE_IMDS", "true" if region_source == "imds" else "false")
    credentials = {"aws_access_key_id": "owned-key", "aws_secret_access_key": "owned-secret",
                   "aws_session_token": "owned-session"}
    for name, value in credentials.items():
        monkeypatch.setenv(name.upper(), value)
    if region_source in ("settings", "static"):
        overrides["aws.AWS_REGION_NAME"] = "us-east-1"
        if region_source == "static":
            overrides.update({f"aws.{name.upper()}": value for name, value in credentials.items()})
    else:
        monkeypatch.setenv("AWS_REGION_NAME" if region_source == "imds" else region_source, "us-east-1")
    if region_source == "imds":
        session = MagicMock()
        session.region_name = "us-east-1"
        session.get_credentials.return_value.get_frozen_credentials.return_value = SimpleNamespace(
            access_key="owned-key", secret_key="owned-secret", token="owned-session",
        )
        monkeypatch.setattr("boto3.Session", lambda **kwargs: session)
    native_params = dict(credentials)
    if auth == "bearer":
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "owned-bearer")
        native_params = {"api_key": "owned-bearer"}
    if model_source == "model_id":
        native_params["model_id"] = arn
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))
    seen = []

    class TransportReached(BaseException):
        pass

    async def send(client, request, **kwargs):
        seen.append(request)
        raise TransportReached

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with monkeypatch.context() as native_environment:
        if region_source in ("settings", "static"):
            # The original handler exported the settings region, not a kwarg.
            native_environment.setenv("AWS_REGION_NAME", "us-east-1")
        with pytest.raises(TransportReached):
            await litellm.acompletion(model=model, messages=[{"role": "user", "content": "usr"}], **native_params)
    expected_region = (
        "eu-west-1" if model_source == "model_arn" or (route == "converse" and model_source != "plain")
        else "us-east-1"
    )

    def assert_request(request):
        assert request.url.host == f"bedrock-runtime.{expected_region}.amazonaws.com"
        if auth == "bearer":
            assert request.headers["Authorization"] == "Bearer owned-bearer"
        else:
            assert "Credential=owned-key/" in request.headers["Authorization"]
            assert f"/{expected_region}/bedrock/aws4_request" in request.headers["Authorization"]
            assert request.headers["X-Amz-Security-Token"] == "owned-session"

    assert len(seen) == 1
    assert_request(seen[0])
    handler = LiteLLMAIHandler()
    captured = dict(handler._aws_active_creds)
    with pytest.raises(TransportReached):
        await handler.chat_completion(model, "sys", "usr")
    assert len(seen) == 2
    assert seen[1].url == seen[0].url
    assert_request(seen[1])
    assert handler._aws_active_creds == captured


@pytest.mark.parametrize(
    ("api_base", "expected"),
    (
        ("https://gateway.ai.cloudflare.com/v1/account/gateway/azure-openai/resource", True),
        ("https://eu.gateway.ai.cloudflare.com/v1/account/gateway/azure-openai/resource", True),
        ("https://owned.example/?next=gateway.ai.cloudflare.com", False),
        ("https://owned.example/gateway.ai.cloudflare.com/v1/account", False),
        ("https://gateway.ai.cloudflare.com.owned.example/v1", False),
        ("https://owned.openai.azure.com", False),
        (None, False),
    ),
)
def test_cloudflare_gateway_matches_the_host_not_the_string(api_base, expected):
    assert litellm_handler._is_cloudflare_gateway(api_base) is expected
